"""Now: the pack's ranking on the Today page (sd:719 step 6).

The rank numbers and the band boundaries are the pack's (`dashboard/now.py`
and the `band` function of `dashboard/app.js` at 85c4fa1b), asserted as numbers so a
port that moved one is a red test and not a quieter page. The sources are
the shapes #427 and #411 built: the fleet child and the shadow table. Nothing
here opens a database of its own; the criterion 2 grep in
`tests/test_markup.py` is the check.
"""

import json
import os
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fleet_support import FleetCase
from sd_dashboard import fleet, now_screen
from sd_db import (
    contributions,
    operations,
    progress,
    reads,
    upsert_shadow,
    workflow,
    writes,
)
from sd_db.writes import snooze
from support import NOW, ScreenCase
from test_workflow_actions import BrowserSession

# The pack's numbers, copied by hand from `dashboard/now.py` at 85c4fa1b so
# a drift in the port is a red test and not a page that quietly ranks
# differently from the one it replaced.
PACK_RANKS = {"AHEAD": 3, "DIRTY": 4, "STALE": 2, "FRESH": 4, "ABANDONED": 3, "STALE_DAYS": 14}


def repo(name, *, ahead=0, dirty=0, branch="main", last="2026-09-01", error=""):
    return {"name": name, "group": ".", "path": f"/repos/{name}", "branch": branch,
            "dirty": dirty, "ahead": ahead, "behind": 0, "last": last, "last_iso": f"{last}T10:00:00+00:00",
            "subject": "a commit", "author": "someone", "web": "", "truncated": [], "error": error}


def tree(name, *, live=True):
    state = "live" if live else "abandoned"
    return {"repo": "system", "name": name, "path": f"/wt/{name}", "branch": name, "live": live, "state": state}


def fleet_document(area, repos=(), trees=()):
    if area == "repos":
        return {"root": "/repos", "rootExists": True, "repos": list(repos),
                "counts": {"repos": len(repos), "dirty": 0, "ahead": 0, "unread": 0}}
    return {"root": "/repos", "rootExists": True, "worktrees": list(trees), "processes": [],
            "processes_error": "", "abandoned": sum(1 for row in trees if not row["live"]),
            "counts": {"worktrees": len(trees), "processes": 0}}


def pull(number, *, first_seen, needs_you=True, repo="example/project"):
    row = {"tracker": "github", "repo": repo, "url": f"https://github.com/{repo}/pull/{number}",
           "number": number, "kind": "pull", "title": f"Pull {number}", "state": "open",
           "author": "someone", "first_seen": first_seen, "last_seen": NOW, "why": []}
    if needs_you is not None:
        row["needs_you"] = needs_you
    return row


class Ranking(ScreenCase):
    """The pure half: rows in, ranked rows out, the band from the rank alone."""

    def test_the_rank_numbers_and_the_band_boundaries_are_the_packs(self):
        for name, value in PACK_RANKS.items():
            self.assertEqual(getattr(now_screen, name), value, name)
        self.assertEqual(now_screen.DARK, 0)
        # The pack's `dashboard/app.js` `band`: <= 1 broken, <= 3 look, else queued.
        self.assertEqual([now_screen.band(rank) for rank in range(0, 6)],
                         ["broken", "broken", "look", "look", "queued", "queued"])
        self.assertEqual(now_screen.band(now_screen.DARK), "broken")
        self.assertEqual(now_screen.band(now_screen.STALE), "look")
        self.assertEqual(now_screen.band(now_screen.AHEAD), "look")
        self.assertEqual(now_screen.band(now_screen.ABANDONED), "look")
        # DIRTY and FRESH tie into one band on purpose (the pack's comment).
        self.assertEqual(now_screen.band(now_screen.DIRTY), "queued")
        self.assertEqual(now_screen.band(now_screen.FRESH), "queued")
        self.assertEqual(now_screen.BANDS, ("broken", "look", "queued"))

    def test_backbone_rows_fold_ahead_and_dirty_into_one_row_at_the_louder_rank(self):
        rows = now_screen.backbone_rows([
            repo("both", ahead=2, dirty=3, branch="feat/x"),
            repo("dirty-only", dirty=1),
            repo("clean"),
            repo("unread", dirty=None, ahead=None, error="git status failed (exit 128)"),
        ])
        self.assertEqual([(row["rank"], row["id"], row["source"]) for row in rows], [
            (now_screen.AHEAD, "ahead:both:2", "repos"),
            (now_screen.DIRTY, "dirty:dirty-only:1", "repos"),
        ])
        self.assertEqual(rows[0]["what"], "both has 2 unpushed commits")
        self.assertEqual(rows[0]["detail"], "feat/x · 3 dirty files")
        self.assertEqual(rows[1]["what"], "dirty-only has 1 uncommitted file")
        self.assertEqual(rows[1]["detail"], "main · last commit 2026-09-01")

    def test_a_pinned_checkout_behind_its_origin_is_a_row_naming_refresh(self):
        """sd:3539: the hub ran a merged fix only once someone remembered refresh;
        Now says the pinned checkout is behind, as repo-sync's pin_state does."""
        rows = now_screen.backbone_rows([
            {**repo("system", branch="HEAD"), "default": "main", "pinned_behind": 3},
            {**repo("level", branch="HEAD"), "default": "main", "pinned_behind": 0},
            {**repo("unread", branch="HEAD"), "default": "main", "pinned_behind": None},
            {**repo("dirty-pin", dirty=1, branch="HEAD"), "default": "main", "pinned_behind": 1},
        ])
        self.assertEqual([(row["rank"], row["id"], row["kind"]) for row in rows], [
            (now_screen.PINNED, "pinned:system:3", "pinned"),
            (now_screen.PINNED, "pinned:dirty-pin:1", "pinned"),
            (now_screen.DIRTY, "dirty:dirty-pin:1", "dirty"),
        ])
        self.assertEqual(rows[0]["what"], "system pinned 3 behind origin/main")
        # sd:9316: the detail is one plain line; the command goes to Details as `cli`.
        self.assertEqual(rows[0]["detail"], "merged commits on origin/main are not live here until the next refresh")
        self.assertEqual(rows[0]["cli"], "local-repo-sync/repo-sync.sh refresh")
        self.assertEqual(now_screen.band(now_screen.PINNED), "look")

    def test_a_pinned_count_that_is_not_a_number_darkens_the_repos_source(self):
        bad = {**repo("system", branch="HEAD"), "pinned_behind": "3"}
        with self.assertRaises(ValueError):
            now_screen._repos(lambda area: fleet_document(area, repos=[bad]))
        good = {**repo("system", branch="HEAD"), "default": "main", "pinned_behind": 2}
        self.assertEqual([row["id"] for row in now_screen._repos(lambda area: fleet_document(area, repos=[good]))],
                         ["pinned:system:2"])

    def test_pr_rows_take_the_pulls_that_need_you_and_rank_the_quiet_ones_louder(self):
        today = "2026-09-20"
        rows = now_screen.pr_rows([
            pull(1, first_seen="2026-09-01T09:00:00+00:00"),
            pull(2, first_seen="2026-09-18T09:00:00+00:00"),
            pull(3, first_seen="2026-09-01T09:00:00+00:00", needs_you=False),
            pull(4, first_seen="2026-09-01T09:00:00+00:00", needs_you=None),
            {**pull(5, first_seen="2026-09-01T09:00:00+00:00"), "kind": "issue"},
            pull(6, first_seen="not a date"),
        ], today)
        self.assertEqual([(row["rank"], row["id"]) for row in rows], [
            (now_screen.STALE, "pr:example/project#1:2"),
            (now_screen.FRESH, "pr:example/project#2:4"),
            (now_screen.FRESH, "pr:example/project#6:4"),
        ])
        self.assertEqual(rows[0]["what"], "example/project#1 open, first seen 19d ago")
        self.assertEqual(rows[1]["what"], "example/project#2 open")
        self.assertEqual(rows[0]["detail"], "Pull 1")
        self.assertTrue(all(row["source"] == "prs" for row in rows))
        # The boundary is the pack's STALE_DAYS, inclusive.
        edge = now_screen.pr_rows([pull(7, first_seen="2026-09-06T00:00:00+00:00")], today)
        self.assertEqual(edge[0]["rank"], now_screen.STALE)
        inside = now_screen.pr_rows([pull(8, first_seen="2026-09-07T00:00:00+00:00")], today)
        self.assertEqual(inside[0]["rank"], now_screen.FRESH)

    def test_session_rows_are_one_row_for_every_abandoned_worktree(self):
        self.assertEqual(now_screen.session_rows([tree("a"), tree("b")]), [])
        rows = now_screen.session_rows([tree("a", live=False), tree("b"), tree("c", live=False)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rank"], now_screen.ABANDONED)
        self.assertEqual(rows[0]["id"], "worktrees:2")
        self.assertEqual(rows[0]["what"], "2 abandoned worktrees")
        self.assertEqual(rows[0]["source"], "sessions")
        self.assertEqual(rows[0]["detail"], "worktrees whose folders are gone")
        self.assertEqual(rows[0]["cli"], "git worktree prune")

    def test_merge_sorts_on_rank_then_id_so_ties_are_stable(self):
        rows = [{"rank": 4, "id": "b"}, {"rank": 0, "id": "z"}, {"rank": 4, "id": "a"}, {"rank": 2, "id": "m"}]
        self.assertEqual([row["id"] for row in now_screen.merge(rows)], ["z", "m", "a", "b"])

    def test_a_dark_row_is_rank_zero_and_names_the_collector_and_the_reason(self):
        row = now_screen.dark_row("repos", "fleet collector exited 3: disk on fire")
        self.assertEqual(row["rank"], now_screen.DARK)
        self.assertEqual(row["id"], "dark:repos")
        self.assertEqual(row["source"], "repos")
        self.assertEqual(row["what"], "repos could not be read")
        self.assertEqual(row["detail"], "fleet collector exited 3: disk on fire")
        self.assertEqual(now_screen.band(row["rank"]), "broken")


class Document(ScreenCase):
    """The merged document: four sources, each guarded, every row banded."""

    def setUp(self):
        super().setUp()
        self.quiet = JobsBackend(self.tmp.name)

    def fixture_fleet(self, repos=(), trees=()):
        return lambda area: fleet_document(area, repos=repos, trees=trees)

    def seed_pull(self, number, *, first_seen, needs_you=True):
        url = f"https://github.com/example/project/pull/{number}"
        if needs_you:
            contributions.capture(self.connection, title=f"Patch {number}", changes={"pull_url": url}, who="operator")
            observation = {"complete": True, "observed_at": "2026-09-09T12:00:00Z",
                "operator": {"id": "1", "login": "author"}, "author": {"id": "1", "login": "author"},
                "repo": "example/project", "title": f"Upstream {number}", "state": "open", "head": "a" * 40,
                "base": "b" * 40, "draft": False, "mergeable": "mergeable", "ci": "success", "ci_head": "a" * 40,
                "ci_ids": ["check:1"], "why": ["author"], "blocking_labels": [], "labels": [], "reviews": [],
                "events": [{"id": f"comment:{number}", "at": "2026-09-09T11:00:00Z", "actor_id": "2", "kind": "comment",
                    "maintainer": True, "mentions_operator": False, "url": url + "#issuecomment-1"}]}
            contributions.observe_pull(self.connection, url, observation,
                expected_revision=contributions.snapshot(self.connection, "github:" + url)["revision"])
        upsert_shadow(self.connection, tracker="github", url=url, repo="example/project", number=number,
                      kind="pull", title=f"Upstream {number}", state="open", author="author")
        # `upsert_shadow` stamps the real clock; the fixture backdates the
        # column, which is seeding and not a second writer.
        self.connection.execute("UPDATE shadow SET first_seen = ? WHERE url = ?", (first_seen, url))
        self.connection.commit()

    def test_the_document_merges_the_three_sources_and_bands_every_row(self):
        self.seed_pull(14, first_seen="2026-08-01T09:00:00+00:00")
        self.seed_pull(15, first_seen="2026-09-05T09:00:00+00:00")
        self.seed_pull(16, first_seen="2026-08-01T09:00:00+00:00", needs_you=False)
        rows = progress.tracker_items(self.connection, tracker="github")
        self.assertEqual({row["number"]: row.get("needs_you") for row in rows}, {14: True, 15: True, 16: None})
        with patch.object(progress, "tracker_items", wraps=progress.tracker_items) as shadow:
            document = now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=self.fixture_fleet(
                repos=[repo("pushy", ahead=1), repo("messy", dirty=2)],
                trees=[tree("gone", live=False)]))
        shadow.assert_called_once_with(self.connection, tracker="github")
        self.assertEqual(set(document), {"rows", "sources", "now", "observed", "snoozed", "snooze_error", "work", "work_error", "active",
                                         "attention",
                                         "mail", "mail_more", "mail_unread", "mail_error", "problems", "problems_more",
                                         "problems_capped", "problems_error", "briefs", "briefs_more", "briefs_error", "briefs_skipped",
                                         "needs", "lane", "track", "spend", "builders", "queues", "advisories", "digest"})
        self.assertEqual(document["now"], NOW)
        self.assertEqual([(row["rank"], row["id"], row["band"]) for row in document["rows"]], [
            (2, "pr:example/project#14:2", "look"),
            (3, "ahead:pushy:1", "look"),
            (3, "worktrees:1", "look"),
            (4, "dirty:messy:2", "queued"),
            (4, "pr:example/project#15:4", "queued"),
        ])
        self.assertEqual(document["sources"], {"repos": "", "sessions": "", "prs": "", "jobs": "", "lane": ""})
        # sd:3446: the rail badge is the read's; every page shows it from Today's last read.
        self.assertEqual(document["attention"], {"state": "caution", "n": 3, "what": "rows to look at"})
        for row in document["rows"]:
            # `seen` is the snooze's fingerprint; the `problem` it reads stays on the server (sd:1896).
            # sd:9316: a row with a command carries it as `cli`, for Details.
            self.assertEqual(set(row) - {"cli"}, {"rank", "band", "kind", "id", "what", "detail", "source", "seen"})

    def test_the_badge_counts_broken_rows_else_look_rows(self):
        # sd:5781: the decisions that wait count on Decide's badge (pages/decide.py), not here. sd:5921: a report is a look
        # row of Needs you, so the badge counts it as one (test_today_needs).
        look, broken = {"band": "look"}, {"band": "broken"}
        self.assertEqual(now_screen._attention([look, broken, broken]), {"state": "warning", "n": 2, "what": "broken rows"})
        self.assertEqual(now_screen._attention([look, look]), {"state": "caution", "n": 2, "what": "rows to look at"})
        self.assertEqual(now_screen._attention([{"band": "queued"}]), {"state": "ok", "n": 0, "what": "rows"})
        self.assertFalse(hasattr(now_screen, "_waiting"))

    def test_an_empty_fleet_and_no_pulls_is_an_empty_list_with_every_source_read(self):
        document = now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=self.fixture_fleet())
        self.assertEqual(document["rows"], [])
        self.assertEqual(document["sources"], {"repos": "", "sessions": "", "prs": "", "jobs": "", "lane": ""})

    def test_a_collector_that_refuses_is_a_rank_zero_row_and_the_others_still_render(self):
        def fleet_backend(area):
            if area == "repos":
                raise ValueError("fleet collection was stopped at its budget: python ran past 12 seconds")
            return fleet_document(area, trees=[tree("gone", live=False)])

        document = now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=fleet_backend)
        self.assertEqual([(row["rank"], row["id"], row["band"]) for row in document["rows"]], [
            (0, "dark:repos", "broken"),
            (3, "worktrees:1", "look"),
        ])
        self.assertEqual(document["rows"][0]["what"], "repos could not be read")
        self.assertEqual(document["rows"][0]["detail"],
                         "fleet collection was stopped at its budget: python ran past 12 seconds")
        self.assertEqual(document["sources"]["repos"], document["rows"][0]["detail"])

    def test_an_incomplete_fleet_document_is_dark_and_never_read_as_calm(self):
        document = now_screen.document(self.connection, now=NOW, jobs=self.quiet,
                                       fleet=lambda area: {"root": "/repos", "rootExists": True})
        self.assertEqual([row["id"] for row in document["rows"]], ["dark:repos", "dark:sessions"])
        self.assertIn("incomplete", document["rows"][0]["detail"])

    def test_a_shadow_read_that_fails_is_a_rank_zero_row(self):
        with patch.object(progress, "tracker_items", side_effect=ValueError("shadow table unreadable")):
            document = now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=self.fixture_fleet())
        self.assertEqual([(row["rank"], row["id"], row["detail"]) for row in document["rows"]],
                         [(0, "dark:prs", "shadow table unreadable")])

    def test_a_snoozed_row_leaves_the_rows_until_its_time_and_then_comes_back(self):
        """sd:1896. The key is the page and the row id; Health's key for the same id hides nothing here."""
        fleet = self.fixture_fleet(repos=[repo("pushy", ahead=1), repo("messy", dirty=2)])
        seen = {row["id"]: row["seen"] for row in now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=fleet)["rows"]}
        snooze(self.connection, "today:ahead:pushy:1", "2026-09-06T15:00:00Z", seen=seen["ahead:pushy:1"], now=NOW)
        snooze(self.connection, "health:dirty:messy:2", "2026-09-06T15:00:00Z", seen=seen["dirty:messy:2"], now=NOW)
        document = now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=fleet)
        self.assertEqual([row["id"] for row in document["rows"]], ["dirty:messy:2"])
        self.assertEqual([(row["id"], row["until"], row["band"]) for row in document["snoozed"]],
                         [("ahead:pushy:1", "2026-09-06T15:00:00+00:00", "look")])
        self.assertEqual(document["snooze_error"], "")
        later = now_screen.document(self.connection, now="2026-09-06T15:00:00Z", jobs=self.quiet, fleet=fleet)
        self.assertEqual([row["id"] for row in later["rows"]], ["ahead:pushy:1", "dirty:messy:2"])
        self.assertEqual(later["snoozed"], [])
        # The id keys on the fact: one more unpushed commit is a new row the old snooze does not cover.
        grown = now_screen.document(self.connection, now=NOW, jobs=self.quiet,
                                    fleet=self.fixture_fleet(repos=[repo("pushy", ahead=2)]))
        self.assertEqual([row["id"] for row in grown["rows"]], ["ahead:pushy:2"])
        # The same id with a problem that reads otherwise is not the row the operator snoozed (sd:1896 review).
        dirtied = now_screen.document(self.connection, now=NOW, jobs=self.quiet,
                                      fleet=self.fixture_fleet(repos=[repo("pushy", ahead=1, dirty=3)]))
        self.assertEqual(([row["id"] for row in dirtied["rows"]], dirtied["snoozed"]), (["ahead:pushy:1"], []))

    def test_a_snooze_read_that_fails_shows_every_row_and_says_why(self):
        snooze(self.connection, "today:ahead:pushy:1", "2026-09-06T15:00:00Z", seen="0f0f", now=NOW)
        with patch.object(writes, "snoozed", side_effect=sqlite3.OperationalError("database is locked")):
            document = now_screen.document(self.connection, now=NOW, jobs=self.quiet,
                                           fleet=self.fixture_fleet(repos=[repo("pushy", ahead=1)]))
        self.assertEqual([row["id"] for row in document["rows"]], ["ahead:pushy:1"])
        self.assertEqual((document["snoozed"], document["snooze_error"]), ([], "database is locked"))

    def test_a_work_row_carries_the_kind_and_revision_fix_posts(self):
        """sd:3456. Today's Fix posts /api/items/<id>/fix with the revision this read gave, as Tasks and Reports do;
        `kind` stays the row's (`item`), and `item_kind` is the item's."""
        from sd_db import workflow

        task = self.item("Write the brief", kind="task", status="in_progress")
        report = self.item("nightly: needs attention", kind="report", status="in_progress")
        document = now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=self.fixture_fleet())
        self.assertEqual([(row["item"], row["item_kind"], row["revision"]) for row in document["work"]],
                         [(task, "task", workflow.item_state(self.connection, task)["revision"]),
                          (report, "report", workflow.item_state(self.connection, report)["revision"])])

    def test_a_work_item_snoozes_by_its_status_and_due_and_shows_again_when_either_moves(self):
        """sd:3271. The key is today:item:<id>; `seen` reads status and due, so a changed item is not the one snoozed."""
        fleet = self.fixture_fleet()
        first = self.item("Write the brief", status="in_progress", due="2026-09-05")
        other = self.item("Second thing", status="in_progress")
        document = now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=fleet)
        self.assertEqual([row["id"] for row in document["work"]], [f"item:{first}", f"item:{other}"])
        self.assertEqual(document["work_error"], "")
        row = document["work"][0]
        self.assertEqual((row["what"], row["detail"]), (f"#{first} Write the brief", "in_progress · due 2026-09-05"))
        self.assertNotIn("problem", row)
        snooze(self.connection, f"today:item:{first}", "2026-09-06T15:00:00Z", seen=row["seen"], now=NOW)
        hidden = now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=fleet)
        self.assertEqual([r["id"] for r in hidden["work"]], [f"item:{other}"])
        self.assertEqual([(r["id"], r["until"], r["seen"]) for r in hidden["snoozed"]],
                         [(f"item:{first}", "2026-09-06T15:00:00+00:00", row["seen"])])
        # A new title is not a new problem; a new due date or a new status is.
        self.connection.execute("UPDATE item SET title = 'Write the brief, again' WHERE id = ?", (first,))
        self.assertEqual([r["id"] for r in now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=fleet)["work"]],
                         [f"item:{other}"])
        self.connection.execute("UPDATE item SET due = '2026-09-04' WHERE id = ?", (first,))
        moved = now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=fleet)
        self.assertEqual(([r["id"] for r in moved["work"]], moved["snoozed"]), ([f"item:{first}", f"item:{other}"], []))
        self.connection.execute("UPDATE item SET due = '2026-09-05' WHERE id = ?", (first,))
        self.connection.execute("UPDATE item SET status = 'ready_to_send' WHERE id = ?", (first,))
        self.assertIn(f"item:{first}", [r["id"] for r in now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=fleet)["work"]])
        # The time passing shows it again, and Health's key for the same id hides nothing.
        self.connection.execute("UPDATE item SET status = 'in_progress' WHERE id = ?", (first,))
        later = now_screen.document(self.connection, now="2026-09-06T15:00:00Z", jobs=self.quiet, fleet=fleet)
        self.assertEqual(([r["id"] for r in later["work"]], later["snoozed"]), ([f"item:{first}", f"item:{other}"], []))
        snooze(self.connection, f"health:item:{other}", "2026-09-07T15:00:00Z", seen=row["seen"], now=NOW)
        self.assertIn(f"item:{other}", [r["id"] for r in now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=fleet)["work"]])

    def test_a_work_read_that_fails_is_said_and_leaves_the_fleet_rows(self):
        """A failed read is not an empty list: the document names the failure, and the other sources still answer."""
        with patch.object(reads, "today_items", side_effect=sqlite3.OperationalError("database is locked")):
            document = now_screen.document(self.connection, now=NOW, jobs=self.quiet,
                                           fleet=self.fixture_fleet(repos=[repo("pushy", ahead=1)]))
        self.assertEqual((document["work"], document["work_error"]), ([], "database is locked"))
        self.assertEqual([row["id"] for row in document["rows"]], ["ahead:pushy:1"])

    def test_a_snooze_read_that_fails_shows_every_work_item_too(self):
        item = self.item("Write the brief", status="in_progress")
        with patch.object(writes, "snoozed", side_effect=sqlite3.OperationalError("database is locked")):
            document = now_screen.document(self.connection, now=NOW, jobs=self.quiet, fleet=self.fixture_fleet())
        self.assertEqual(([row["id"] for row in document["work"]], document["snooze_error"]),
                         ([f"item:{item}"], "database is locked"))


class DarkCollector(FleetCase):
    """A fixture collector that exits non-zero: the row the design owes (step 6)."""

    def test_a_fleet_child_that_exits_non_zero_is_a_visible_row_naming_it(self):
        script = Path(self.tmp.name) / "refusing_fleet.py"
        script.write_text("import sys\nprint('fixture collector refused: ' + sys.argv[1], file=sys.stderr)\nsys.exit(3)\n")
        with patch.object(fleet, "FLEET", script):
            document = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name))
        self.assertEqual([(row["rank"], row["id"], row["band"]) for row in document["rows"]],
                         [(0, "dark:repos", "broken"), (0, "dark:sessions", "broken")])
        self.assertEqual(document["rows"][0]["detail"], "fixture collector refused: repos")
        self.assertEqual(document["rows"][1]["detail"], "fixture collector refused: sessions")

    def test_the_real_fleet_child_feeds_the_ranking(self):
        pushy = self.checkout("pushy")
        clone = self.root / "clone"
        self.git(self.root, "clone", "-q", str(pushy), str(clone))
        self.configure(clone)
        (clone / "README").write_text("more\n")
        self.git(clone, "commit", "-q", "-am", "ahead by one")
        messy = self.checkout("messy")
        (messy / "scratch").write_text("x\n")
        self.abandoned(messy, "gone", "feat/gone")
        document = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name))
        self.assertEqual([(row["rank"], row["id"]) for row in document["rows"]], [
            (3, "ahead:clone:1"), (3, "worktrees:1"), (4, "dirty:messy:1"),
        ])
        self.assertEqual(document["sources"], {"repos": "", "sessions": "", "prs": "", "jobs": "", "lane": ""})

    def test_a_fixture_commit_leaves_the_abandoned_registration_in_place(self):
        """sd:1272: git 2.54's auto maintenance prunes a registration with no
        directory and no index -- the one `abandoned` writes. After a fixture
        commit it ran detached, so under load it could land after
        `abandoned` and drop the `worktrees:1` row. `autoDetach` off makes
        that maintenance run inside the commit, so the race is deterministic."""
        messy = self.checkout("messy")
        self.git(messy, "config", "maintenance.autoDetach", "false")
        self.abandoned(messy, "gone", "feat/gone")
        (messy / "README").write_text("again\n")
        self.git(messy, "commit", "-q", "-am", "a later commit")
        self.assertTrue((messy / ".git" / "worktrees" / "gone").is_dir())
        document = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name))
        self.assertIn((3, "worktrees:1"), [(row["rank"], row["id"]) for row in document["rows"]])


class NowApi(BrowserSession):
    fleet_backend = staticmethod(lambda area: fleet_document(area, repos=[repo("pushy", ahead=1)]))

    def setUp(self):
        # `backend` is the server's `operations_backend`, which Now reads for jobs.
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.backend = JobsBackend(root.name, jobs=[("nightly-sync", "failed", 7, None)])
        super().setUp()

    def test_the_document_is_served_to_a_session_and_refused_without_one(self):
        status, headers, body = self.request("/api/now", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        document = json.loads(body)
        self.assertEqual([(row["id"], row["band"]) for row in document["rows"]],
                         [("job:nightly-sync:7", "broken"), ("ahead:pushy:1", "look")])
        self.assertEqual(self.request("/api/now")[0], 403)
        self.assertEqual(self.request("/api/now?x=1", headers={"Cookie": self.cookie})[0], 400)
        self.assertEqual(self.request("/api/now", headers={"Host": "evil.invalid", "Cookie": self.cookie})[0], 403)

    def test_every_page_shows_the_badge_the_last_read_carried(self):
        # sd:3446: Now runs the fleet child and launchd, so the rail never reads it; it shows Today's own last read.
        def badges():
            body = self.request("/ui/sections.js", headers={"Cookie": self.cookie})[2]
            return json.loads(re.search(r"^window\.SHELL_COUNTS = (.*);$", body, re.M).group(1))["pages"]

        self.assertNotIn("Today", badges())
        self.assertEqual(self.request("/api/now?x=1", headers={"Cookie": self.cookie})[0], 400)
        self.assertNotIn("Today", badges(), "a refused request is no read")
        self.request("/api/now", headers={"Cookie": self.cookie})
        self.assertEqual({key: value for key, value in badges()["Today"].items() if key != "at"},
                         {"state": "warning", "n": 1, "what": "broken rows"})
        with patch.object(now_screen, "document", side_effect=RuntimeError("the fleet child hung")), patch("sys.stderr"):
            try:
                self.request("/api/now", headers={"Cookie": self.cookie})
            except OSError:
                pass  # the handler's exception drops the connection; the badge is what this test reads
        self.assertEqual(badges()["Today"]["state"], "unknown")

    def test_the_read_writes_nothing(self):
        before = self.snapshot()
        self.assertEqual(self.request("/api/now", headers={"Cookie": self.cookie})[0], 200)
        self.assertEqual(self.snapshot(), before)


class JobsBackend:
    """An operations backend double: `names` and `inspect`, the shape
    `operations.inventory` reads, and the `cron_root` whose `logs/` holds
    each job's log. No launchctl call is made."""

    def __init__(self, root, jobs=(), refuse=""):
        self.cron_root = Path(root) / "local-cron-jobs"
        (self.cron_root / "logs").mkdir(parents=True, exist_ok=True)
        self.jobs = {name: (state, code, signal) for name, state, code, signal in jobs}
        self.refuse = refuse

    def names(self):
        if self.refuse:
            raise OSError(self.refuse)
        return list(self.jobs)

    def inspect(self, name):
        state, code, signal = self.jobs[name]
        return {"name": name, "label": "fixture." + name, "service": "fixture/" + name, "schedule": [],
                "state": state, "pid": 7 if state == "running" else None, "last_exit": code,
                "last_signal": signal, "runs": 1, "lifetime": None, "boot": None, "reason": "",
                "_observation": name + state}

    def log(self, name, stamp):
        path = self.cron_root / "logs" / f"{name}.log"
        path.write_text("a run\n")
        os.utime(path, (stamp, stamp))


class FailedJobs(ScreenCase):
    """Four jobs failed on 2026-09-27 and Today did not show them."""

    def fleet(self, area):
        return fleet_document(area, repos=[repo("pushy", ahead=1)])

    def test_a_failed_job_is_a_warning_row_ranked_above_every_other_source(self):
        jobs = JobsBackend(self.tmp.name, jobs=[("nightly-sync", "failed", 7, None),
                                                ("quiet", "idle", 0, None), ("busy", "running", None, None)])
        stamp = datetime(2026, 9, 27, 2, 15).timestamp()
        jobs.log("nightly-sync", stamp)
        # The default seam: Now reads the same launchd backend Operations does.
        with patch.object(operations, "LaunchdBackend", lambda: jobs):
            document = now_screen.document(self.connection, now=NOW, fleet=self.fleet)
        self.assertEqual([(row["rank"], row["id"], row["band"]) for row in document["rows"]], [
            (1, "job:nightly-sync:7", "broken"),
            (3, "ahead:pushy:1", "look"),
        ])
        row = document["rows"][0]
        self.assertEqual(row["source"], "jobs")
        self.assertEqual(row["what"], "nightly-sync failed with exit 7")
        # Retry is what the Operations Jobs area sends: a kickstart updates the
        # launchd record Now reads, and a hand-run of cron-jobs.sh does not.
        self.assertEqual(row["retry"], "launchctl kickstart fixture/nightly-sync")
        self.assertEqual(row["detail"], "log 2026-09-27 02:15 · triage: unclear, no rule matched exit 7")
        self.assertNotIn("cli", row, "the retry line is Details' own fact")
        self.assertEqual(now_screen.FAILED, 1)
        self.assertEqual(document["sources"], {"repos": "", "sessions": "", "prs": "", "jobs": "", "lane": ""})

    def test_interrupted_unloaded_and_unknown_jobs_are_look_rows_below_a_failed_one(self):
        # sd:2014. Operations lists these three under attention beside failed;
        # Now showed failed only, so a job launchd will never run said nothing.
        jobs = JobsBackend(self.tmp.name, jobs=[("nightly-sync", "failed", 7, None), ("stopped", "interrupted", None, 9),
                                                ("parked", "unloaded", None, None), ("odd", "unknown", None, None),
                                                ("quiet", "idle", 0, None), ("busy", "running", None, None)])
        jobs.log("stopped", datetime(2026, 9, 27, 3, 0).timestamp())
        rows = now_screen.document(self.connection, now=NOW, fleet=self.fleet, jobs=jobs)["rows"]
        self.assertEqual([(row["rank"], row["id"], row["band"], row.get("state")) for row in rows], [
            (1, "job:nightly-sync:7", "broken", "failed"),
            (2, "job:odd:unknown", "look", "unknown"),
            (2, "job:parked:unloaded", "look", "unloaded"),
            (2, "job:stopped:interrupted9", "look", "interrupted"),
            (3, "ahead:pushy:1", "look", None),
        ])
        by_id = {row["id"]: row for row in rows}
        stopped = by_id["job:stopped:interrupted9"]
        self.assertEqual(stopped["what"], "stopped was interrupted by SIGKILL (9)")
        self.assertEqual(stopped["detail"], "log 2026-09-27 03:00")
        self.assertEqual(stopped["retry"], "launchctl kickstart fixture/stopped")
        parked = by_id["job:parked:unloaded"]
        self.assertEqual(parked["what"], "parked is installed but not loaded")
        self.assertEqual(parked["detail"], "launchd will not run it until it is loaded")
        self.assertEqual(parked["cli"], "local-cron-jobs/cron-jobs.sh install parked")
        # Operations refuses a retry for an unloaded or unknown job, so neither row offers one.
        self.assertNotIn("retry", parked)
        odd = by_id["job:odd:unknown"]
        self.assertEqual(odd["what"], "odd: launchd state unknown")
        self.assertEqual(odd["detail"], "launchctl gave no state Operations recognises")
        self.assertEqual(odd["cli"], "launchctl print fixture/odd")
        self.assertNotIn("retry", odd)
        self.assertEqual(now_screen.ATTENTION, 2)

    def test_a_job_killed_for_cause_names_the_signal_and_a_missing_log_says_so(self):
        jobs = JobsBackend(self.tmp.name, jobs=[("crashy", "failed", None, 11)])
        rows = now_screen.document(self.connection, now=NOW, fleet=self.fleet, jobs=jobs)["rows"]
        self.assertEqual(rows[0]["id"], "job:crashy:signal11")
        self.assertEqual(rows[0]["what"], "crashy failed with SIGSEGV (11)")
        self.assertEqual(rows[0]["detail"], "no log file · triage: " + ", ".join(rows[0]["triage"][k] for k in ("class", "why")))

    def test_a_signal_is_named_even_when_launchd_also_reports_exit_zero(self):
        jobs = JobsBackend(self.tmp.name, jobs=[("crashy", "failed", 0, 11)])
        row = now_screen.document(self.connection, now=NOW, fleet=self.fleet, jobs=jobs)["rows"][0]
        self.assertEqual((row["id"], row["what"]), ("job:crashy:signal11", "crashy failed with SIGSEGV (11)"))

    def test_a_backend_without_a_cron_root_still_lists_its_failed_jobs(self):
        class NoRoot(JobsBackend):
            def __init__(self, root, jobs=()):
                super().__init__(root, jobs)
                del self.cron_root
        jobs = NoRoot(self.tmp.name, jobs=[("nightly-sync", "failed", 7, None)])
        document = now_screen.document(self.connection, now=NOW, fleet=self.fleet, jobs=jobs)
        self.assertEqual(document["sources"]["jobs"], "")
        self.assertEqual(document["rows"][0]["detail"], "log unknown · triage: unclear, no rule matched exit 7")

    def test_an_unreadable_log_is_one_rows_detail_not_a_dark_source(self):
        jobs = JobsBackend(self.tmp.name, jobs=[("nightly-sync", "failed", 7, None)])
        jobs.log("nightly-sync", datetime(2026, 9, 27, 2, 15).timestamp())
        real_stat = Path.stat

        def refusing(path, *args, **kwargs):
            if path.name == "nightly-sync.log":
                raise PermissionError(13, "Permission denied", str(path))
            return real_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", refusing):
            document = now_screen.document(self.connection, now=NOW, fleet=self.fleet, jobs=jobs)
        self.assertEqual(document["sources"]["jobs"], "")
        self.assertTrue(document["rows"][0]["detail"].startswith("log unreadable · triage: "), document["rows"][0]["detail"])

    def test_a_retryable_row_carries_the_revision_retry_job_checks_and_its_capability(self):
        """sd:3386: Today's Retry posts the row's revision; it is the job's, so a job that changed since the read refuses."""
        class Kicks(JobsBackend):
            def perform(self, action, service):
                self.sent = (action, service)
                return subprocess.CompletedProcess([], 0)

        jobs = Kicks(self.tmp.name, jobs=[("nightly-sync", "failed", 127, None), ("stopped", "interrupted", None, 9),
                                          ("parked", "unloaded", None, None)])
        by_id = {row["id"]: row for row in now_screen.document(self.connection, now=NOW, fleet=self.fleet, jobs=jobs)["rows"]}
        listed = {job["name"]: job for job in operations.inventory(self.connection, backend=jobs)["jobs"]}
        failed, stopped = by_id["job:nightly-sync:127"], by_id["job:stopped:interrupted9"]
        # The capability is the inventory's, whole: the pack adds fields to it (`label`, sd:4982).
        self.assertEqual((failed["revision"], failed["exit"], failed["capability"]["allowed"], failed["capability"]),
                         (listed["nightly-sync"]["revision"], 127, True, listed["nightly-sync"]["capabilities"]["retry"]))
        self.assertEqual((stopped["job"], stopped["revision"], stopped["exit"]), ("stopped", listed["stopped"]["revision"], None))
        # No retry, so nothing to bind: an unloaded row carries no revision.
        self.assertNotIn("revision", by_id["job:parked:unloaded"])
        # A job whose launchd record moved since the read is another subject: its old revision is refused.
        jobs.jobs["stopped"] = ("failed", 1, None)
        with self.assertRaises(workflow.StaleItem):
            operations.retry_job(self.connection, "stopped", expected_revision=stopped["revision"], who="dashboard", backend=jobs)
        self.assertFalse(hasattr(jobs, "sent"))
        answer = operations.retry_job(self.connection, "nightly-sync", expected_revision=failed["revision"], who="dashboard", backend=jobs)
        self.assertEqual((answer["request"]["status"], jobs.sent), ("accepted", ("retry", "fixture/nightly-sync")))

    def test_a_jobs_read_that_fails_is_one_dark_row_and_the_others_still_render(self):
        jobs = JobsBackend(self.tmp.name, refuse="LaunchAgents is unreadable")
        document = now_screen.document(self.connection, now=NOW, fleet=self.fleet, jobs=jobs)
        self.assertEqual([(row["rank"], row["id"], row["detail"]) for row in document["rows"]], [
            (0, "dark:jobs", "LaunchAgents is unreadable"),
            (3, "ahead:pushy:1", "main · clean tree"),
        ])
        self.assertEqual(document["sources"]["jobs"], "LaunchAgents is unreadable")


class Advisories(ScreenCase):
    """sd:10915: the Queue view's advisory filter, from the item refs and the row `dashboard.queue_advisories`."""

    def test_open_advisory_refs_are_listed_and_hidden_until_the_row_says_on(self):
        sweep = self.item("Sweep finding", source="task-ref", external_id="advisory-sweep:2026-11-23:example")
        filed = self.item("Hand-filed advisory", source="task-ref", external_id="advisory:example.test")
        done = self.item("Fixed one", kind="task", source="task-ref", external_id="advisory:closed")
        workflow.change_status(self.connection, done, "done", who="test", reason="test")
        self.item("Look-alike", source="task-ref", external_id="advisoryish:x")
        self.item("Inner match", source="task-ref", external_id="triage:advisory:x")
        self.item("Other source", source="writing-piece", external_id="advisory:x")
        found = now_screen.advisories(self.connection)
        self.assertEqual(found, {"key": "dashboard.queue_advisories", "ids": [sweep, filed], "row": None,
                                 "show": False, "error": ""})
        writes.set_setting(self.connection, "dashboard.queue_advisories", "on")
        self.assertTrue(now_screen.advisories(self.connection)["show"])
        self.assertIn("advisories", now_screen.document(self.connection, now=NOW, fleet=lambda area: {},
                                                        jobs=JobsBackend(self.tmp.name)))

    def test_a_failed_read_hides_nothing_and_says_why(self):
        with patch("sd_db.writes.read_settings", side_effect=sqlite3.OperationalError("database is locked")):
            found = now_screen.advisories(self.connection)
        self.assertEqual((found["ids"], found["show"], found["error"]), ([], True, "database is locked"))
