"""The v2 Contributions page (sd:2113).

What this slice promises: `/contributions` answers under the shared policy and loads its script before `shell.js`, and the
old screen moves to `/classic/contributions`, which the palette lists. `/api/contributions/page` is the projection v1
renders: open rows in lane order with only the fields the page shows, settled rows counted per repository, open rows
capped at `OPEN_LIMIT` with the cut said, each row's scope from sd's repo table, and the GitHub tracker's freshness. The
page registers the design's Contributions commands; Acknowledge and Make task ask first and post, Make task a linked task, and
Open on GitHub and Re-run collector post nothing; Draft nudge drafts into Details, and Post nudge asks first, then posts
the draft through /api/actions/run (sd:5887). Nothing claims a GitHub reading.

`contributions.js` runs under JavaScriptCore (osascript) against the stand-in page and shell `test_v2_tasks` uses, with the
real reader (`read.js`, sd:2488). Focus after a redraw (sd:2426) runs against a small box stand-in that parses the drawn
buttons. The browser half -- the look at 1440 and 375 px, the confirm dialog -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import contribution_screen, server, v2
from sd_db import contributions, upsert_repo, writes
from support import NOW, ScreenCase
from test_contribution_screen import contribution, seed_registered
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_shell_shared import cell_grammar
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
PAGE_JS = (V2 / "static" / "contributions.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")

#: The design's Contributions commands (design source products/system/designs/pages/contributions.js): id, object type, label,
#: key, risk. Acknowledge and Make task are `confirm` here, not the design's `undo`: no verb reverses either.
COMMANDS = [
    ["contribution.ack", "contribution", "Acknowledge", "a", "confirm"],
    ["contribution.nudge", "contribution", "Draft nudge", "d", "safe"],
    # sd:5887: Post nudge posts the draft as the operator's comment, after a confirm that names the pull request or issue.
    ["contribution.post", "contribution", "Post nudge", "n", "confirm"],
    ["contribution.task", "contribution", "Make task", "k", "confirm"],
    ["contribution.open", "contribution", "Open on GitHub", "o", "safe"],
    ["contribution.dismiss", "contribution", "Dismiss", "x", "confirm"],
    # sd:5486: Hide and Unhide, the view state Designs' and Documents' Hide write; Undo reverses each.
    ["contribution.hide", "contribution", "Hide", "h", "undo"],
    ["contribution.unhide", "contribution", "Unhide", "h", "undo"],
    # sd:5488: Edit opens a filed ticket or issue in Jira or on GitHub, in a new tab; the dashboard writes nothing there.
    ["filed.edit", "filed", "Edit", "e", "safe"],
    ["collector.sync", "collector", "Re-run collector", "r", "safe"],
]

ISSUE = "https://github.com/example/project/issues/2"
EXTERNAL = "https://github.com/other/lib/pull/3"


def rows():
    """One row per case the page draws: each open lane, an issue with a local item, an unfiled row, and settled rows."""
    return [
        contribution(1, "newly_unblocked", title="Fix it's $HOME", observed_at="2026-09-05T10:00:00Z"),
        contribution(2, "awaiting_you", key="issue:" + ISSUE, url=ISSUE, item_id=7, event_ids=[], attention_sources=[],
                     observed_at="2026-09-04T10:00:00Z"),
        contribution(3, "awaiting_them", key="github:" + EXTERNAL, url=EXTERNAL, repo="other/lib", event_ids=[],
                     observed_at="2026-09-03T10:00:00Z", freshness={"status": "unknown", "reason": "Incomplete GitHub observation"}),
        contribution(4, "awaiting_them", key="item:4", url=None, repo=None, event_ids=[], observed_at=None,
                     freshness={"status": "unknown", "reason": "Not yet observed"}),
        contribution(5, "merged"), contribution(6, "merged"), contribution(7, "closed", repo="other/lib"),
    ]


class TheDocument(ScreenCase):
    """`contribution_screen.document` shapes the projection; it reads no GitHub and writes nothing."""

    def setUp(self):
        super().setUp()
        upsert_repo(self.connection, "/home/example/project", remote="git@github.com:Example/Project.git", managed=1)

    def doc(self, given=None):
        with mock.patch.object(contribution_screen.contributions, "projection", return_value=given if given is not None else rows()):
            return contribution_screen.document(self.connection, now=NOW)

    def test_open_rows_come_in_lane_order_newest_first_and_settled_rows_are_counted_only(self):
        doc = self.doc()
        self.assertEqual([r["key"] for r in doc["rows"]], [
            "github:https://github.com/example/project/pull/1", "issue:" + ISSUE, "github:" + EXTERNAL, "item:4"])
        self.assertEqual(doc["lanes"], {"newly_unblocked": 1, "awaiting_you": 1, "awaiting_them": 2, "merged": 2, "closed": 1})
        self.assertEqual((doc["total"], doc["open_total"], doc["truncated"], doc["read"]), (7, 4, False, NOW))
        self.assertEqual(doc["fresh"], {"current": 5, "unknown": 2})
        self.assertEqual(doc["settled"], [{"repo": "example/project", "internal": True, "merged": 2, "closed": 0},
                                          {"repo": "other/lib", "internal": False, "merged": 0, "closed": 1}])

    def test_a_row_carries_only_what_the_page_shows(self):
        row = self.doc(given=[contribution(1, evidence=[{"argv": ["/home/example/x/secret.sh"], "cwd": "/home/example/x"}],
                                           local_clone="/home/example/x", blocked_on="waiting on a private note")])["rows"][0]
        self.assertEqual(set(row), set(contribution_screen.ROW_FIELDS) | {
            "repo", "has_draft", "freshness", "internal", "why_internal", "evidence", "dependencies", "delivery", "linked", "hidden"})
        # sd:3394: evidence is shown now, but no local path: the script reads `…/secret.sh`, the directory `local: x`.
        self.assertNotIn("/home/example", json.dumps(row))
        # Review 4 of PR #72 (34daa6618266): blocked_on is free-form metadata the page never reads.
        self.assertNotIn("private note", json.dumps(row))
        self.assertNotIn("blocked_on", PAGE_JS)

    def test_scope_comes_from_the_repo_table(self):
        upsert_repo(self.connection, "/home/example/tools", remote="https://github.com/example/tools")
        doc = self.doc(given=[contribution(1), contribution(2, repo="example/tools"), contribution(3, repo="other/lib"),
                              contribution(4, repo="/home/example/tools"), contribution(5, repo=None)])
        self.assertEqual([(r["internal"], r["why_internal"]) for r in doc["rows"]],
                         [(True, "managed"), (True, "registered"), (False, None), (True, "registered"), (False, None)])

    def test_open_rows_stop_at_the_limit_and_say_so(self):
        many = [contribution(n, "awaiting_them", observed_at=f"2026-09-0{n}T00:00:00Z") for n in range(1, 4)]
        with mock.patch.object(contribution_screen, "OPEN_LIMIT", 3):
            whole = self.doc(given=many)
        with mock.patch.object(contribution_screen, "OPEN_LIMIT", 2):
            cut = self.doc(given=many)
        self.assertEqual((len(whole["rows"]), whole["truncated"]), (3, False))
        self.assertEqual((len(cut["rows"]), cut["open_total"], cut["truncated"]), (2, 3, True))
        self.assertEqual([r["key"][-1] for r in cut["rows"]], ["3", "2"], "the cut dropped a newer row")

    def test_settled_repositories_past_the_limit_fold_into_other_per_scope(self):
        settled = [contribution(n, "merged", repo=f"other/r{n}") for n in range(1, 4)] + [contribution(9, "closed")]
        with mock.patch.object(contribution_screen, "SETTLED_REPOS", 2):
            doc = self.doc(given=settled)
        self.assertEqual([e["repo"] for e in doc["settled"]], ["example/project", "other/r1"])
        self.assertEqual(doc["settled_other"], {"internal": {"merged": 0, "closed": 0}, "external": {"merged": 2, "closed": 0}})

    def test_no_field_holds_a_local_path_or_a_draft_digest(self):
        draft = {"path": "/home/example/drafts/issue.md", "sha256": "f" * 64}
        doc = self.doc(given=[contribution(1, url=None, repo="/home/example/clone", draft_path=draft),
                              contribution(2, url=None, repo="~/clone-two"), contribution(3, "merged", repo="/home/example/old")])

        def strings(value):
            if isinstance(value, dict):
                for key, inner in value.items():
                    yield key
                    yield from strings(inner)
            elif isinstance(value, list):
                for inner in value:
                    yield from strings(inner)
            elif isinstance(value, str):
                yield value
        leaked = [text for text in strings(doc) if text.startswith(("/", "~")) or "/home/example" in text or "f" * 64 in text]
        self.assertEqual(leaked, [])
        self.assertEqual([(r["repo"], r["has_draft"]) for r in doc["rows"]], [("local: clone", True), ("local: clone-two", False)])
        self.assertEqual(doc["settled"][0]["repo"], "local: old")

    def test_the_counts_cover_every_open_row_even_past_the_limit(self):
        many = [contribution(1, "newly_unblocked"), contribution(2, "awaiting_you", repo="other/lib", url=None),
                contribution(3, "awaiting_them", repo="other/lib", item_id=9, freshness={"status": "unknown", "reason": "x"})]
        with mock.patch.object(contribution_screen, "OPEN_LIMIT", 1):
            doc = self.doc(given=many)
        self.assertEqual(len(doc["rows"]), 1)
        self.assertEqual(doc["counts"], {
            "internal": {"newly_unblocked": 1, "awaiting_you": 0, "awaiting_them": 0, "unknown": 0, "linked": 0, "unfiled": 0, "github": 0,
                         "hidden": 0},
            "external": {"newly_unblocked": 0, "awaiting_you": 1, "awaiting_them": 1, "unknown": 1, "linked": 1, "unfiled": 1, "github": 0,
                         "hidden": 0}})

    def test_a_hidden_row_carries_hidden_and_leaves_every_count_and_the_badge(self):
        # sd:5486: Hide is the view store Designs and Documents write, keyed contribution:<key>. A hidden row stays in the
        # document for Show hidden, but leaves the lanes, the tallies and the badge; settled rows ignore it.
        writes.set_view(self.connection, "contribution:" + contribution(1)["key"], hidden=True)
        writes.set_view(self.connection, "contribution:" + contribution(5)["key"], hidden=True)
        self.connection.commit()
        doc = self.doc()
        self.assertEqual([(r["key"][-1:], r["hidden"]) for r in doc["rows"]], [("2", False), ("3", False), ("4", False), ("1", True)])
        self.assertEqual(doc["store"], {"available": True, "reason": ""})
        internal = doc["counts"]["internal"]
        self.assertEqual((internal["newly_unblocked"], internal["awaiting_you"], internal["hidden"]), (0, 1, 1))
        self.assertEqual(doc["attention"], {"state": "caution", "n": 1, "what": "contributions want you"})
        self.assertEqual(doc["settled"][0]["merged"], 2, "a hidden settled row left the record")

    def test_hidden_rows_go_last_so_the_limit_cuts_them_first(self):
        many = [contribution(n, "awaiting_them", observed_at=f"2026-09-0{n}T00:00:00Z") for n in range(1, 4)]
        writes.set_view(self.connection, "contribution:" + many[2]["key"], hidden=True)
        self.connection.commit()
        with mock.patch.object(contribution_screen, "OPEN_LIMIT", 2):
            cut = self.doc(given=many)
        self.assertEqual([r["key"][-1] for r in cut["rows"]], ["2", "1"])
        self.assertEqual((cut["truncated"], cut["counts"]["internal"]["hidden"]), (True, 1))

    def test_an_unread_view_store_hides_nothing_and_says_why(self):
        writes.set_view(self.connection, "contribution:" + contribution(1)["key"], hidden=True)
        with mock.patch.object(writes, "views", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.doc()
        self.assertEqual([r["hidden"] for r in doc["rows"]], [False] * 4)
        self.assertEqual(doc["store"], {"available": False, "reason": "the view store was not read: database is locked"})

    def test_settled_checkouts_named_alike_stay_two_entries(self):
        # Review 2 of PR #72 (aa5a4f3222ed): the label `local: project` keyed the fold, so two checkouts became one entry.
        doc = self.doc(given=[contribution(1, "merged", repo="/home/example/a/project"),
                              contribution(2, "merged", repo="/home/example/a/project"),
                              contribution(3, "closed", repo="/home/example/b/project")])
        self.assertEqual(doc["settled"], [{"repo": "local: project", "internal": False, "merged": 2, "closed": 0},
                                          {"repo": "local: project", "internal": False, "merged": 0, "closed": 1}])

    def test_a_row_carries_its_evidence_dependencies_and_delivery_without_a_local_path(self):
        # sd:3394: Details shows what only /classic/contributions showed, shaped so no local path leaves the server.
        check = {"argv": ["/home/example/x/run.sh", "--out=/home/example/x/report.xml", "tests/test_a.py",
                          "https://github.com/example/project/pull/1"],
                 "cwd": "/home/example/x", "exit_code": 0, "commit": "c" * 40, "artifact": "/home/example/x/report.xml",
                 "sha256": "d" * 64}
        depends = [{"kind": "item", "item": 9}, {"kind": "merge", "url": EXTERNAL}, {"kind": "issue", "url": ISSUE},
                   {"kind": "release", "repo": "other/lib", "tag": "v2.0.0", "contains_pull": EXTERNAL,
                    "package": {"name": "lib", "version": "2.0.0"}}]
        notices = {"github:" + EXTERNAL: {
            "event-1": {"status": "sent", "event": {"id": "event-1", "reason": "Maintainer approved"},
                        "acknowledged_at": "2026-09-09T13:00:00Z"},
            "event-2": {"status": "uncertain", "event": {"id": "event-2", "reason": "CI failed"}},
            "event-3": {"status": "pending", "event": {"id": "event-3", "reason": "Review requested"}}}}
        row = self.doc(given=[contribution(1, evidence=[check], evidence_verified=True, tested_commit="c" * 40,
                                           depends_on=depends, notification_state=notices)])["rows"][0]
        self.assertEqual(row["tested_commit"], "c" * 40)
        self.assertEqual(row["evidence"], {"state": "read", "verified": True, "checks": [{
            "command": "…/run.sh --out=…/report.xml tests/test_a.py https://github.com/example/project/pull/1",
            "directory": "local: x", "exit_code": 0, "commit": "c" * 40, "artifact": "local: report.xml", "sha256": "d" * 64}]})
        self.assertEqual(row["dependencies"], {"state": "read", "entries": depends})
        self.assertEqual(row["delivery"], {"state": "read", "entries": [
            {"source": "github:" + EXTERNAL, "reason": "Maintainer approved", "status": "sent", "acknowledged_at": "2026-09-09T13:00:00Z"},
            {"source": "github:" + EXTERNAL, "reason": "CI failed", "status": "uncertain", "acknowledged_at": None},
            {"source": "github:" + EXTERNAL, "reason": "Review requested", "status": "pending", "acknowledged_at": None}]})
        empty = self.doc(given=[contribution(1)])["rows"][0]
        self.assertEqual((empty["evidence"], empty["dependencies"], empty["delivery"]), (
            {"state": "read", "verified": False, "checks": []}, {"state": "read", "entries": []}, {"state": "read", "entries": []}))

    def test_a_shape_that_cannot_be_read_is_unknown_with_its_reason_never_empty(self):
        row = self.doc(given=[contribution(1, evidence={"argv": ["x"]}, depends_on="merge it",
                                           notification_state={"github:" + EXTERNAL: ["sent"]})])["rows"][0]
        self.assertEqual(row["evidence"], {"state": "unknown", "reason": "the recorded evidence is not a list"})
        self.assertEqual(row["dependencies"], {"state": "unknown", "reason": "the recorded dependencies are not a list"})
        self.assertEqual(row["delivery"], {"state": "unknown", "reason": f"the notifications for github:{EXTERNAL} are not a record"})
        mixed = self.doc(given=[contribution(1, evidence=["run it"], depends_on=[{"kind": "vote", "url": EXTERNAL}],
                                             notification_state={"github:" + EXTERNAL: {"event-1": {"event": {"reason": "Merged"}}}})])["rows"][0]
        self.assertEqual(mixed["evidence"], {"state": "unknown", "reason": "a recorded check is not a record"})
        self.assertEqual(mixed["dependencies"]["entries"], [{"kind": "unknown", "reason": "dependency kind vote is not one sd records"}])
        self.assertEqual(mixed["delivery"]["entries"], [
            {"source": "github:" + EXTERNAL, "reason": "Merged", "status": "unknown", "acknowledged_at": None}])

    def test_each_row_carries_when_github_settled_it_and_the_window_counts_every_dated_row(self):
        # sd:5301: the shadow collector records when GitHub merged or closed a row (pack sd:5303); the page dates it.
        doc = self.doc(given=[
            contribution(1, "awaiting_you", merged_at="2026-09-05T03:00:00Z", closed_at="2026-09-05T03:00:00Z"),
            contribution(2, "awaiting_them", repo="other/lib", merged_at=None, closed_at=None),
            contribution(3, "merged", merged_at="2026-09-01T10:00:00Z", closed_at="2026-09-01T10:00:00Z"),
            contribution(4, "closed", repo="other/lib", merged_at=None, closed_at="2026-08-30T10:00:00Z"),
            contribution(5, "merged", merged_at="2026-06-01T10:00:00Z", closed_at=None),
            contribution(6, "closed", merged_at=None, closed_at=None),
            contribution(7, "merged", merged_at="not a date", closed_at=None)])
        self.assertTrue(doc["github_dates"])
        self.assertEqual([(r["merged_at"], r["closed_at"]) for r in doc["rows"]],
                         [("2026-09-05T03:00:00Z", "2026-09-05T03:00:00Z"), (None, None)])
        self.assertEqual((doc["counts"]["internal"]["github"], doc["counts"]["external"]["github"]), (1, 0))
        self.assertEqual(doc["settled_recent"], [
            {"at": "2026-09-05T03:00:00Z", "how": "merged", "internal": True},
            {"at": "2026-09-01T10:00:00Z", "how": "merged", "internal": True},
            {"at": "2026-08-30T10:00:00Z", "how": "closed", "internal": False}])
        # Settled rows only: one dated before the window, two with no date GitHub gave (one unreadable).
        self.assertEqual(doc["settled_older"], {"internal": 1, "external": 0})
        self.assertEqual(doc["settled_undated"], {"internal": 2, "external": 0})

    def test_a_projection_from_a_pack_before_settle_dates_reads_them_as_unknown(self):
        # Older state: a pack before sd:5303 gives rows no `merged_at` key at all; that is unknown, never an error or "open".
        doc = self.doc()
        self.assertFalse(doc["github_dates"])
        self.assertEqual({(r["merged_at"], r["closed_at"]) for r in doc["rows"]}, {(None, None)})
        self.assertEqual(doc["settled_recent"], [])
        self.assertEqual(doc["counts"]["internal"]["github"], 0)

    def test_a_row_carries_the_issues_it_links_and_only_links_it_can_open(self):
        # sd:5308: the pack gives each row `linked`, the GitHub issues it closes and the Jira keys its title or branch name.
        linked = [{"url": ISSUE, "kind": "github", "key": "example/project#2"},
                  {"url": "https://jira.example.test/browse/ABC-12", "kind": "jira", "key": "ABC-12"},
                  {"url": "http://jira.example.test/browse/ABC-13", "kind": "jira", "key": "ABC-13"},
                  {"url": "javascript:alert(1)", "kind": "jira", "key": "ABC-14"},
                  {"url": "https://user:pw@jira.example.test/browse/ABC-15", "kind": "jira", "key": "ABC-15"},
                  {"url": EXTERNAL, "kind": "github", "key": "other/lib#3"},
                  {"url": "https://jira.example.test/browse/ABC-16", "kind": "linear", "key": "ABC-16"},
                  {"url": "https://jira.example.test/browse/ABC-17", "kind": "jira", "key": ""},
                  "ABC-18", None]
        row = self.doc(given=[contribution(1, linked=linked)])["rows"][0]
        self.assertEqual(row["linked"], linked[:2])
        # Older state: a pack before sd:5315 sends no `linked`, and a shape that does not read links nothing; neither fails.
        self.assertEqual([r["linked"] for r in self.doc(given=[contribution(1), contribution(2, linked="ABC-1")])["rows"]], [[], []])

    def test_the_collector_is_the_github_tracker_freshness(self):
        self.assertEqual(self.doc()["collector"], {"state": "never", "last_success_at": None, "reason": ""})

    def test_the_document_writes_nothing(self):
        seed_registered(self.connection)
        before = tuple(self.connection.iterdump())
        doc = contribution_screen.document(self.connection, now=NOW)
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.assertEqual(doc["rows"][0]["title"], "Ready patch <safe>")
        # The pinned pack's projection carries the settle dates (pack sd:5303), so the page reads them as recorded.
        self.assertTrue(doc["github_dates"])


JIRA = "https://jira.example.test/browse/"
FILED_ISSUE = "https://github.com/example/project/issues/9"


def seed_filed(connection):
    """One shadow row per case the Filed view tells apart (sd:5488), as `sd shadow sync` writes them (pack sd:5496)."""
    jira = {"tracker": "jira", "kind": "issue", "repo": "ABC", "state": "open", "updated_at": "2026-09-01T10:00:00Z"}
    for key, extra in (
            # Filed and assigned: `reason` names the stronger, `filed` still says filed.
            ("ABC-1", {"filed": True, "reason": "assigned", "created_at": "2026-03-02T10:00:00Z", "status": "In Review", "assignee": "Alex"}),
            ("ABC-2", {"filed": True, "reason": "filed", "created_at": "2025-06-01T10:00:00Z"}),  # last year
            ("ABC-3", {"filed": False, "reason": "watched", "created_at": "2026-04-01T10:00:00Z"}),  # someone else's
            ("ABC-4", {"reason": "filed"}),  # synced before pack sd:5496: filed by its reason, no created date yet
            ("ABC-5", {"reason": "assigned"})):  # synced before pack sd:5496: whether the operator filed it is unknown
        writes.upsert_shadow(connection, url=JIRA + key, title=f"Ticket {key}", **jira, **extra)
    writes.upsert_shadow(connection, tracker="github", url=FILED_ISSUE, repo="example/project", number=9, kind="issue",
                         title="Filed issue", state="open", author="octocat", reason="filed", filed=True,
                         created_at="2026-05-01T10:00:00Z", assignee="")
    writes.upsert_shadow(connection, tracker="github", url="https://github.com/example/project/pull/10", repo="example/project",
                         number=10, kind="pull", title="A pull request", state="open", reason="filed", filed=True,
                         created_at="2026-05-02T10:00:00Z")


class TheFiledDocument(ScreenCase):
    """sd:5488: the Filed view's rows, the Jira tickets and GitHub issues the operator filed this year, from `shadow`."""

    def filed(self):
        with mock.patch.object(contribution_screen.contributions, "projection", return_value=[]):
            return contribution_screen.document(self.connection, now=NOW)["filed"]

    def test_this_years_filed_tickets_and_issues_come_newest_first_and_an_undated_one_last(self):
        seed_filed(self.connection)
        filed = self.filed()
        self.assertEqual([row["key"] for row in filed["rows"]],
                         ["filed:github:" + FILED_ISSUE, "filed:jira:" + JIRA + "ABC-1", "filed:jira:" + JIRA + "ABC-4"])
        self.assertEqual(filed["year"], 2026)
        first, ticket, undated = filed["rows"]
        self.assertEqual({k: ticket[k] for k in ("tracker", "ref", "title", "project", "status", "created_at", "assignee", "url")},
                         {"tracker": "jira", "ref": "ABC-1", "title": "Ticket ABC-1", "project": "ABC", "status": "In Review",
                          "created_at": "2026-03-02T10:00:00Z", "assignee": "Alex", "url": JIRA + "ABC-1"})
        self.assertEqual((first["ref"], first["status"], first["assignee"]), ("example/project#9", "open", ""))
        self.assertEqual((undated["created_at"], undated["assignee"]), (None, None))
        self.assertEqual(filed["trackers"]["jira"], {"state": "never", "reason": "", "last_success_at": None, "filer_unknown": 1})
        self.assertEqual(filed["trackers"]["github"]["filer_unknown"], 0)

    def test_a_tracker_that_does_not_read_is_named_with_its_reason_and_the_other_still_lists(self):
        seed_filed(self.connection)
        from sd_db import progress

        real = progress.tracker_items

        def items(connection, *, tracker, **options):
            if tracker == "jira":
                raise sqlite3.OperationalError("database is locked")
            return real(connection, tracker=tracker, **options)

        with mock.patch.object(progress, "tracker_items", items):
            filed = self.filed()
        self.assertEqual(filed["trackers"]["jira"]["state"], "unread")
        self.assertIn("database is locked", filed["trackers"]["jira"]["reason"])
        self.assertEqual([row["tracker"] for row in filed["rows"]], ["github"])

    def test_a_pack_before_the_filed_columns_is_named_never_an_empty_list(self):
        # Older state: a pack before pack sd:5496 sends rows with no `filed` or `created_at`.
        from sd_db import progress

        old = [{"tracker": "jira", "url": JIRA + "ABC-1", "kind": "issue", "reason": "filed", "repo": "ABC", "title": "t",
                "state": "open", "author": "", "last_seen": NOW, "updated_at": None}]
        with mock.patch.object(progress, "tracker_items", return_value=old):
            filed = self.filed()
        self.assertEqual(filed["rows"], [])
        self.assertEqual({tracker: value["state"] for tracker, value in filed["trackers"].items()},
                         {"jira": "unrecorded", "github": "unrecorded"})
        self.assertIn("pack sd:5496", filed["trackers"]["jira"]["reason"])

    def test_the_filed_view_writes_nothing(self):
        seed_filed(self.connection)
        before = tuple(self.connection.iterdump())
        self.filed()
        self.assertEqual(tuple(self.connection.iterdump()), before)


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/contributions")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Contributions · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "commands.js", "contributions.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_and_the_palette_keeps_the_old_one(self):
        self.assertEqual(v2.SECTIONS.get("Contributions"), "/contributions")
        self.assertNotIn("Contributions", v2.CLASSIC)
        self.assertEqual(v2.SCREENS.get("Contributions (classic)"), "/classic/contributions")
        status, _, body = self.request("/classic/contributions")
        self.assertEqual(status, 200)
        # The classic screen draws inside the v2 shell (sd:3586): its own server-drawn main, not the page's template.
        self.assertIn("<title>Contributions · system</title>", body)
        self.assertIn('<main id="main" class="classic">', body)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        before = self.snapshot()
        self.assertEqual(self.request("/api/contributions/page")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/contributions/page?scope=internal", headers=cookie)[0], 400)
        status, _, body = self.request("/api/contributions/page", headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["rows"], [])
        self.assertEqual(self.snapshot(), before)

    def test_a_row_the_page_reads_is_one_the_acknowledge_route_accepts(self):
        seed_registered(self.connection)
        status, _, body = self.request("/api/contributions/page", headers={"Cookie": self.cookie})
        row = json.loads(body)["rows"][0]
        payload = {"key": row["key"], "revision": row["revision"], "event_ids": row["event_ids"]}
        self.assertEqual(self.post("/api/contributions/acknowledge", payload)[0], 200)
        self.assertEqual(self.post("/api/contributions/acknowledge", payload)[0], 409)


    def test_make_task_files_a_task_the_projection_links_to_the_row(self):
        # Review 2 of PR #72 (42fa0e59c1bf): a standalone followup never linked, so the row still offered Make task.
        url = "https://github.com/example/project/pull/21"
        contributions.observe_pull(self.connection, url, {"complete": True, "observed_at": "2026-09-09T12:00:00Z",
            "operator": {"id": "1", "login": "author"}, "author": {"id": "1", "login": "author"}, "repo": "example/project",
            "title": "Upstream patch", "state": "open", "head": "a" * 40, "base": "b" * 40, "draft": False,
            "mergeable": "mergeable", "ci": "success", "ci_head": "a" * 40, "ci_ids": ["check:1"], "why": ["author"],
            "blocking_labels": [], "labels": [], "reviews": [], "events": []},
            expected_revision=contributions.snapshot(self.connection, "github:" + url)["revision"])
        self.connection.commit()
        def read():
            return next(r for r in json.loads(self.request("/api/contributions/page", headers={"Cookie": self.cookie})[2])["rows"]
                            if r["url"] == url)
        row = read()
        self.assertIsNone(row["item_id"])
        status, _, out = self.post("/api/contributions/task", {"key": row["key"], "title": row["title"]})
        self.assertEqual(status, 200, out)
        self.assertEqual(read()["item_id"], out["item"]["id"])
        status, _, again = self.post("/api/contributions/task", {"key": row["key"], "title": row["title"]})
        self.assertEqual(status, 400)
        self.assertIn("already belongs", again["error"])
        self.assertEqual(self.post("/api/contributions/task", {"key": "item:4", "title": "x"})[0], 400)

    def test_dismiss_moves_a_row_with_no_item_out_of_the_open_rows(self):
        # sd:3432: a pull request whose repository was re-created 404s, so its row read "awaiting them" forever.
        url = "https://github.com/example/project/pull/22"
        contributions.observe_pull(self.connection, url, {"complete": True, "observed_at": "2026-09-09T12:00:00Z",
            "operator": {"id": "1", "login": "author"}, "author": {"id": "1", "login": "author"}, "repo": "example/project",
            "title": "Stale patch", "state": "open", "head": "a" * 40, "base": "b" * 40, "draft": False,
            "mergeable": "mergeable", "ci": "success", "ci_head": "a" * 40, "ci_ids": ["check:1"], "why": ["author"],
            "blocking_labels": [], "labels": [], "reviews": [], "events": []},
            expected_revision=contributions.snapshot(self.connection, "github:" + url)["revision"])
        self.connection.commit()
        def read():
            return json.loads(self.request("/api/contributions/page", headers={"Cookie": self.cookie})[2])
        [row] = [r for r in read()["rows"] if r["url"] == url]
        payload = {"key": row["key"], "revision": row["revision"]}
        before = self.snapshot()
        for changes in ({"revision": None}, {"key": 22}, {"all": True}):
            self.assertEqual(self.post("/api/contributions/dismiss", {**payload, **changes})[0], 400)
        self.assertEqual(self.post("/api/contributions/dismiss", payload, Origin="https://example.invalid")[0], 403)
        self.assertEqual(self.snapshot(), before)
        status, _, out = self.post("/api/contributions/dismiss", payload)
        self.assertEqual(status, 200, out)
        page = read()
        self.assertEqual([r for r in page["rows"] if r["url"] == url], [])
        self.assertEqual(page["lanes"]["closed"], 1)
        status, _, again = self.post("/api/contributions/dismiss", payload)
        self.assertEqual(status, 409)
        self.assertTrue(again["reload"])
        # A row with an item closes through its task.
        item = contributions.capture(self.connection, title="Tracked", changes={"pull_url": url.replace("22", "23")}, who="operator")
        self.connection.commit()
        key = "github:" + url.replace("22", "23")
        contributions.observe_pull(self.connection, url.replace("22", "23"), {"complete": True, "observed_at": "2026-09-09T12:00:00Z",
            "operator": {"id": "1", "login": "author"}, "author": {"id": "1", "login": "author"}, "repo": "example/project",
            "title": "Tracked", "state": "open", "head": "a" * 40, "base": "b" * 40, "draft": False,
            "mergeable": "mergeable", "ci": "success", "ci_head": "a" * 40, "ci_ids": ["check:1"], "why": ["author"],
            "blocking_labels": [], "labels": [], "reviews": [], "events": []},
            expected_revision=contributions.snapshot(self.connection, key)["revision"])
        self.connection.commit()
        [tracked] = [r for r in read()["rows"] if r["item_id"] == item["item"]["id"]]
        status, _, refused = self.post("/api/contributions/dismiss", {"key": tracked["key"], "revision": tracked["revision"]})
        self.assertEqual(status, 400)
        self.assertIn("close the item", refused["error"])

    def test_hide_writes_the_view_state_the_page_reads_back_and_unhide_clears_it(self):
        # sd:5486: Hide persists until Unhide; a key the projection does not list writes nothing.
        seed_registered(self.connection)
        def read():
            return json.loads(self.request("/api/contributions/page", headers={"Cookie": self.cookie})[2])
        [row] = read()["rows"]
        self.assertFalse(row["hidden"])
        before = self.snapshot()
        for payload in ({"key": row["key"]}, {"key": row["key"], "hidden": "yes"}, {"key": 3, "hidden": True},
                        {"key": row["key"], "hidden": True, "revision": row["revision"]}):
            self.assertEqual(self.post("/api/contributions/view", payload)[0], 400, payload)
        self.assertEqual(self.post("/api/contributions/view", {"key": row["key"], "hidden": True}, **{"X-SD-CSRF": "0" * 64})[0], 403)
        status, _, refused = self.post("/api/contributions/view", {"key": "github:https://github.com/example/gone/pull/9", "hidden": True})
        self.assertEqual((status, refused.get("reload")), (409, True))
        self.assertEqual(self.snapshot(), before)
        status, _, out = self.post("/api/contributions/view", {"key": row["key"], "hidden": True})
        self.assertEqual((status, out), (200, {"key": row["key"], "pinned": False, "hidden": True, "tags": []}))
        page = read()
        self.assertEqual([r["hidden"] for r in page["rows"]], [True])
        self.assertEqual(sum(side["hidden"] for side in page["counts"].values()), 1)
        self.assertEqual(writes.views(self.connection)["contribution:" + row["key"]]["hidden"], True)
        self.assertEqual(self.post("/api/contributions/view", {"key": row["key"], "hidden": False})[0], 200)
        self.assertEqual([r["hidden"] for r in read()["rows"]], [False])


SHELL_MORE = r"""
window.shell.row = () => null; window.shell.closePane = () => {}; OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
OUT.picks = []; C.pick = id => OUT.picks.push(id); OUT.opened = []; window.open = u => OUT.opened.push(u);
// An answer that settles after k more turns, so a test can make an older read land after a newer one.
const later = (v, k) => { let p = Promise.resolve(v); for (let i = 0; i < k; i++) p = p.then(x => x); return p; };
// The shell's view switch: show() records the view and fires shell:view, as shell.js does; count() records each tab's count.
OUT.shown = []; OUT.counts = {}; let VIEW_NOW = 'open';
window.shell.view = { show(id) { OUT.shown.push(id); VIEW_NOW = id; document.dispatchEvent(new CustomEvent('shell:view', { detail: { id } })); },
  current: () => VIEW_NOW, count: (id, n) => { OUT.counts[id] = n; } };
"""


class Script(ScreenCase):
    """contributions.js against the document `contribution_screen` builds."""

    def setUp(self):
        super().setUp()
        upsert_repo(self.connection, "/home/example/project", remote="https://github.com/example/project", managed=1)
        with mock.patch.object(contribution_screen.contributions, "projection", return_value=rows()):
            self.doc = contribution_screen.document(self.connection, now=NOW)

    def run_page(self, body, answer=None, search="", extra="", tz="America/Denver"):
        answer = answer or ("(path, body) => path === '/api/contributions/page' ? [200, DOC] : path === '/api/contributions/task'"
                            " ? [200, { item: { id: 42, title: body.title } }] : [200, { ok: true }]")
        script = (STAND_IN + f"location.search = {json.dumps(search)};\nvar DOC = {json.dumps(self.doc)};\n" + MARKUP_JS
                  + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_MORE + extra + READ_SHELL + f"\nANSWER = {answer};\n" + COMMANDS_JS + PAGE_JS
                  + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.attention = window.PAGE_ATTENTION; OUT.html = Object.fromEntries(Object.entries(ELS).map(([k, e]) => [k, e.html]));"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False, env={**os.environ, "TZ": tz})
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out


class TheScript(Script):
    """The commands, the drawing and the filters."""

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]); R.bulk = REG.filter(c => c.bulk).map(c => c.id);")
        self.assertEqual(out["R"]["reg"], COMMANDS)
        self.assertEqual(out["R"]["bulk"], ["contribution.ack"])

    def test_acknowledge_asks_first_and_posts_the_rows_exact_checkpoint(self):
        out = self.run_page("shellRun(cmd('contribution.ack'), C.get('github:https://github.com/example/project/pull/1')); await flush();")
        self.assertEqual(out["confirms"], ["contribution.ack"])
        self.assertEqual(out["posts"], [["/api/contributions/acknowledge", {
            "key": "github:https://github.com/example/project/pull/1", "revision": "a" * 64, "event_ids": ["event-1"]}, 64]])
        self.assertEqual(out["gets"], ["/api/contributions/page", "/api/contributions/page"], "the page did not reread after the write")
        self.assertEqual(out["toasts"][-1], ["Acknowledged · Fix it's $HOME", False])

    def test_a_refused_acknowledge_says_why_and_rereads(self):
        answer = "(path) => path === '/api/contributions/page' ? [200, DOC] : [409, { error: 'Events changed', reload: true }]"
        out = self.run_page("shellRun(cmd('contribution.ack'), C.get('github:https://github.com/example/project/pull/1')); await flush();", answer=answer)
        self.assertEqual(out["toasts"][-1], ["Fix it's $HOME not changed: Events changed", False])
        self.assertEqual(len(out["gets"]), 2)

    def test_make_task_asks_first_files_a_linked_task_and_is_off_for_a_tracked_row(self):
        out = self.run_page("""R.when = [cmd('contribution.task').when(C.get('issue:""" + ISSUE + """')), cmd('contribution.task').when(C.get('item:4'))];
R.cli = cmd('contribution.task').cli(C.get('github:https://github.com/example/project/pull/1'));
shellRun(cmd('contribution.task'), C.get('github:""" + EXTERNAL + """')); await flush();""")
        self.assertEqual(out["R"]["when"], ["already tracked as item #7", "not filed on GitHub"])
        self.assertEqual(out["R"]["cli"], """printf '%s\\n' '{"pull_url":"https://github.com/example/project/pull/1"}' > contribution.json"""
                                          " && sd task contribution add 'Fix it'\\''s $HOME' --file contribution.json")
        self.assertEqual(out["confirms"], ["contribution.task"])
        self.assertEqual(out["posts"], [["/api/contributions/task", {"key": "github:" + EXTERNAL, "title": "Contribution 3"}, 64]])
        self.assertEqual(out["toasts"][-1][0], "Task #42 filed · Contribution 3")

    def test_dismiss_asks_first_posts_the_rows_checkpoint_and_is_off_for_a_tracked_or_unfiled_row(self):
        out = self.run_page("""R.when = [cmd('contribution.dismiss').when(C.get('issue:""" + ISSUE + """')), cmd('contribution.dismiss').when(C.get('item:4'))];
const ext = C.get('github:""" + EXTERNAL + """'); R.cli = cmd('contribution.dismiss').cli(ext);
shellRun(cmd('contribution.dismiss'), ext); await flush(); R.off = cmd('contribution.dismiss').when(ext);""")
        self.assertEqual(out["R"]["when"], ["tracked as item #7; close the item instead", "not filed on GitHub"])
        self.assertEqual(out["R"]["cli"], f"sd task contribution dismiss 'github:{EXTERNAL}' --if-revision {'a' * 64}")
        self.assertEqual(out["confirms"], ["contribution.dismiss"])
        self.assertEqual(out["posts"], [["/api/contributions/dismiss", {"key": "github:" + EXTERNAL, "revision": "a" * 64}, 64]])
        self.assertEqual(out["gets"], ["/api/contributions/page", "/api/contributions/page"], "the page did not reread after the write")
        self.assertEqual(out["toasts"][-1], ["Dismissed · Contribution 3", False])
        self.assertEqual(out["R"]["off"], "dismissed; the page reads again")

    def hidden(self):
        """The document with the newly unblocked row hidden, as `document` sends it: out of the counts, last in the rows."""
        one = next(r for r in self.doc["rows"] if r["key"] == ONE)
        one["hidden"] = True
        self.doc["rows"] = [r for r in self.doc["rows"] if r is not one] + [one]
        self.doc["counts"]["internal"]["newly_unblocked"] -= 1
        self.doc["counts"]["internal"]["hidden"] = 1
        self.doc["attention"] = {"state": "caution", "n": 1, "what": "contributions want you"}

    #: The server's answer, with the view write landing in the document the next read returns.
    VIEWS = ("(path, body) => { if (path === '/api/contributions/view') { DOC.rows.find(r => r.key === body.key).hidden = body.hidden;"
             " return [200, { key: body.key, hidden: body.hidden }]; } return [200, DOC]; }")

    #: A click on the Open table's Open | Hidden (sd:5753, sd:6701; pack sd:7263 dropped All).
    PRESS = "ELS['open-filters'].listeners.click[0]({ target: { closest: s => s === 'button[data-aside=\"hidden\"]' ? { dataset: { v: '%s' } } : null } });"
    SHOW_HIDDEN, SHOW_OPEN = PRESS % "aside", PRESS % "open"
    SEG = r'data-aside="hidden" data-v="(\w+)" aria-pressed="(\w+)">\w+ <span class="n">(\d+)</span>'

    def test_a_hidden_row_leaves_the_open_rows_and_show_hidden_lists_it_with_unhide(self):
        # sd:5486: Open lists the unhidden rows. sd:5753, sd:6701: Hidden, in the Open table's Open | Hidden, lists
        # the hidden ones alone; the URL keeps the choice, and Open drops it. Pack sd:7263: the bar has no All.
        self.hidden()
        out = self.run_page(f"""R.rows = ELS.rows.html; R.lamps = ELS.annunciator.html; R.tally = ELS.tally.html; R.bar = ELS['hidden-seg'].html;
{self.SHOW_HIDDEN} R.hidden = ELS.rows.html; R.title = ELS['open-title'].textContent; R.htally = ELS.tally.html;
R.bar2 = ELS['hidden-seg'].html; R.url = OUT.urls[OUT.urls.length - 1];
R.off = [cmd('contribution.hide').when(C.get('{ONE}')), cmd('contribution.unhide').when(C.get('{ONE}')),
  cmd('contribution.unhide').when(C.get('github:{EXTERNAL}'))];
{self.SHOW_OPEN} R.back = ELS.rows.html; R.url2 = OUT.urls[OUT.urls.length - 1];""")
        ids = r'<tr data-id="([^"]+)"'
        self.assertNotIn(ONE, re.findall(ids, out["R"]["rows"]))
        self.assertEqual(len(re.findall(ids, out["R"]["rows"])), 3)
        self.assertIn('data-cell="newly_unblocked" data-state="ok"', out["R"]["lamps"])
        self.assertIn("▲ 1 yours", out["R"]["tally"])
        self.assertEqual(re.findall(ids, out["R"]["hidden"]), [ONE])
        self.assertEqual((out["R"]["title"], out["R"]["htally"]), ("Hidden", "1 hidden"))
        self.assertEqual(out["R"]["off"], ["already hidden", True, "not hidden"])
        self.assertEqual(re.findall(self.SEG, out["R"]["bar"]), [("open", "true", "3"), ("aside", "false", "1")])
        self.assertEqual(re.findall(self.SEG, out["R"]["bar2"]), [("open", "false", "3"), ("aside", "true", "1")])
        self.assertNotIn(">All <", out["R"]["bar"])
        self.assertEqual((out["R"]["url"], out["R"]["url2"]), ({"hidden": "1"}, {}))
        self.assertNotIn("hidden", out["counts"], "Hidden is no view, so no tab carries its count")
        self.assertNotIn(ONE, re.findall(ids, out["R"]["back"]))
        self.assertEqual(out["attention"]["n"], 1)

    def test_an_older_hidden_all_link_lands_on_open_and_drops_the_key(self):
        # Pack sd:7263: All left the bar, so ?hidden=all, which it kept, lands on Open: the unhidden rows, Open pressed.
        self.hidden()
        out = self.run_page("R.rows = ELS.rows.html; R.bar = ELS['hidden-seg'].html; R.title = ELS['open-title'].textContent;",
                            search="?hidden=all")
        ids = r'<tr data-id="([^"]+)"'
        self.assertNotIn(ONE, re.findall(ids, out["R"]["rows"]))
        self.assertEqual(len(re.findall(ids, out["R"]["rows"])), 3)
        self.assertEqual(re.findall(self.SEG, out["R"]["bar"]), [("open", "true", "3"), ("aside", "false", "1")])
        self.assertEqual(out["R"]["title"], "Open")
        self.assertNotIn("hidden", out["urls"][-1])

    def test_hide_posts_the_row_and_undo_posts_it_back(self):
        out = self.run_page(f"""shellRun(cmd('contribution.hide'), C.get('{ONE}')); await flush(); R.rows = ELS.rows.html;
await lastUndo().undo(); await flush(); R.back = ELS.rows.html;""", answer=self.VIEWS)
        self.assertEqual(out["confirms"], [])
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/contributions/view", {"key": ONE, "hidden": True}],
                                                         ["/api/contributions/view", {"key": ONE, "hidden": False}]])
        self.assertEqual(out["toasts"][0], ["Hidden · Fix it's $HOME. Hidden lists it.", True])
        self.assertNotIn(ONE, re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]))
        self.assertIn(ONE, re.findall(r'<tr data-id="([^"]+)"', out["R"]["back"]), "Undo did not bring the row back")
        self.assertEqual(out["gets"].count("/api/contributions/page"), 3, "Hide or its Undo did not read again")

    def test_unhide_posts_the_row_and_undo_hides_it_again(self):
        self.hidden()
        out = self.run_page(f"""{self.SHOW_HIDDEN} shellRun(cmd('contribution.unhide'), C.get('{ONE}')); await flush();
await lastUndo().undo(); await flush();""")
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/contributions/view", {"key": ONE, "hidden": False}],
                                                         ["/api/contributions/view", {"key": ONE, "hidden": True}]])
        self.assertEqual(out["toasts"][0], ["Unhidden · Fix it's $HOME", True])

    def test_an_emptied_show_hidden_says_nothing_is_hidden_while_a_count_lags(self):
        # Seen in the browser: after Unhide, a count from the last read still said 1 hidden, and the empty view blamed the
        # limit. Rows are left out only when the document says it cut some.
        self.hidden()
        out = self.run_page(f"""{self.SHOW_HIDDEN} shellRun(cmd('contribution.unhide'), C.get('{ONE}')); await flush();
R.rows = ELS.rows.html;""", answer=self.VIEWS)
        self.assertIn("Nothing hidden in any repositories", out["R"]["rows"])
        self.assertNotIn("left out by the limit", out["R"]["rows"])

    def test_an_unread_view_store_turns_hide_off_with_its_reason_and_show_hidden_says_so(self):
        self.doc["store"] = {"available": False, "reason": "the view store was not read: locked"}
        out = self.run_page(f"""R.off = cmd('contribution.hide').when(C.get('{ONE}')); {self.SHOW_HIDDEN} R.rows = ELS.rows.html;""")
        self.assertEqual(out["R"]["off"], "Hide is off: the view store was not read: locked")
        self.assertIn("Hidden not read: the view store was not read: locked", out["R"]["rows"])

    def test_an_address_or_an_older_hidden_view_link_opens_show_hidden_and_a_lamp_turns_it_off(self):
        # sd:5753: Hidden was a view (sd:5486); its #view=hidden link lands on Open with Show hidden on, as ?hidden=1 does.
        # A lamp counts the unhidden rows, so a press shows them.
        self.hidden()
        moved = ("location.pathname = '/contributions'; location.hash = %s; OUT.moved = null;"
                 " var history = { state: null, replaceState(s, t, u) { OUT.moved = u; const h = u.indexOf('#');"
                 " location.search = u.slice(u.indexOf('?'), h); location.hash = u.slice(h); } };")
        lamp = "ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? { dataset: { cell: 'awaiting_you' } } : null } });"
        ids = r'<tr data-id="([^"]+)"'
        for search, hash, moved_to in (("", "#view=hidden", "/contributions?hidden=1#view=open"),
                                       ("?scope=internal", "#view=hidden", "/contributions?scope=internal&hidden=1#view=open"),
                                       ("?hidden=1", "", None)):
            out = self.run_page(f"R.rows = ELS.rows.html; R.bar = ELS['hidden-seg'].html; {lamp} R.lamped = ELS.rows.html;",
                                search=search, extra=moved % json.dumps(hash))
            self.assertEqual(out["moved"], moved_to, (search, hash))
            self.assertEqual(re.findall(ids, out["R"]["rows"]), [ONE], (search, hash))
            self.assertIn('data-aside="hidden" data-v="aside" aria-pressed="true"', out["R"]["bar"])
            self.assertNotIn(ONE, re.findall(ids, out["R"]["lamped"]))
            self.assertNotIn("hidden", out["urls"][-1])
        out = self.run_page("", extra=moved % json.dumps("#view=settled"))
        self.assertIsNone(out["moved"])

    def test_a_landed_write_whose_reread_fails_still_says_it_landed_and_turns_its_command_off(self):
        # Review 2 of PR #72 (84cde549031a): the reread sat inside the write's promise, so a failed GET reported a committed
        # POST as "not changed", and Make task stayed on for a retry that files a second task.
        answer = """(() => { let n = 0; return path => path === '/api/contributions/page' ? (n++ ? [500, { error: 'gone' }] : [200, DOC])
          : path === '/api/contributions/task' ? [200, { item: { id: 42 } }] : [200, { ok: true }]; })()"""
        out = self.run_page("""const ext = C.get('github:""" + EXTERNAL + """'), one = C.get('github:https://github.com/example/project/pull/1');
shellRun(cmd('contribution.task'), ext); await flush(); shellRun(cmd('contribution.ack'), one); await flush();
R.off = [cmd('contribution.task').when(ext), cmd('contribution.ack').when(one)]; R.types = [C.get(ext.id).type, C.get(one.id).type];""", answer=answer)
        self.assertEqual([t[0] for t in out["toasts"]], ["Task #42 filed · Contribution 3", "Acknowledged · Fix it's $HOME"])
        self.assertEqual(out["R"]["off"], ["already tracked as item #42", "no attention event on this row"])
        # sd:2488: the rows stay drawn, but their objects are retired until a read lists them again.
        self.assertEqual(out["R"]["types"], ["not listed", "not listed"])
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("The change landed; the contributions were not read again: gone", out["states"][-1]["text"])

    def test_a_refused_write_whose_reread_fails_does_not_say_it_landed(self):
        # Review 4 of PR #72 (005e99951263): both rejection handlers reread, and a failed reread said the write landed.
        answer = """(() => { let n = 0; return path => path === '/api/contributions/page' ? (n++ ? [500, { error: 'gone' }] : [200, DOC])
          : [409, { error: 'Events changed', reload: true }]; })()"""
        out = self.run_page("""shellRun(cmd('contribution.ack'), C.get('github:https://github.com/example/project/pull/1')); await flush();""", answer=answer)
        self.assertEqual(out["toasts"][-1], ["Fix it's $HOME not changed: Events changed", False])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertNotIn("landed", out["states"][-1]["text"])
        self.assertIn("The contributions were not read: gone", out["states"][-1]["text"])

    def test_after_a_bulk_acknowledge_only_the_newest_reread_draws(self):
        # Review 2 of PR #72 (903bc49868b2): one reread per row, and an older answer that lands last replaced the newer one.
        ext = next(r for r in self.doc["rows"] if r["key"] == "github:" + EXTERNAL)
        ext["event_ids"] = ["event-3"]
        newer = dict(self.doc, rows=[r for r in self.doc["rows"] if r["key"] != ext["key"]])
        answer = """(() => { let n = 0; return path => path !== '/api/contributions/page' ? [200, { ok: true }]
          : (n++, n === 2 ? later([200, DOC], 60) : n === 3 ? [200, """ + json.dumps(newer) + """] : [200, DOC]); })()"""
        # Two single runs: a bulk group's rows now read again once, after the group (pack sd:7852).
        out = self.run_page("""shellRun(cmd('contribution.ack'), C.get('github:https://github.com/example/project/pull/1'));
shellRun(cmd('contribution.ack'), C.get('github:""" + EXTERNAL + """')); await flush(); R.rows = ELS.rows.html;""", answer=answer)
        self.assertEqual(len(out["gets"]), 3)
        self.assertNotIn(EXTERNAL, out["R"]["rows"])

    def test_a_row_a_reread_no_longer_lists_is_retired_and_unpicked(self):
        # Review 2 of PR #72 (c714acabd3ed): the object map kept the vanished row, so a stale Acknowledge could still act on it.
        newer = dict(self.doc, rows=[r for r in self.doc["rows"] if r["key"] != "github:" + EXTERNAL])
        answer = """(() => { let n = 0; return path => path !== '/api/contributions/page' ? [200, { ok: true }]
          : n++ ? [200, """ + json.dumps(newer) + """] : [200, DOC]; })()"""
        out = self.run_page("""document.dispatchEvent(new CustomEvent('shell:picked', { detail: ['github:""" + EXTERNAL + """'] }));
shellRun(cmd('contribution.ack'), C.get('github:https://github.com/example/project/pull/1')); await flush();
const gone = C.get('github:""" + EXTERNAL + """'); R.gone = [gone.type, gone.label, REG.filter(c => c.on === gone.type).length];""", answer=answer)
        self.assertEqual(out["R"]["gone"], ["not listed", "Contribution 3 (no longer listed)", 0])
        self.assertEqual(out["picks"], ["github:" + EXTERNAL])

    def test_draft_nudge_posts_nothing_and_open_opens_the_page(self):
        out = self.run_page("""const ext = C.get('github:""" + EXTERNAL + """');
R.exec = ['contribution.nudge', 'contribution.open'].map(id => cmd(id).executes);
R.cli = [cmd('contribution.nudge').cli(ext), cmd('contribution.open').cli(ext)];
shellRun(cmd('contribution.nudge'), ext); shellRun(cmd('contribution.open'), ext); await flush(); R.det = ELS.details.html;""")
        self.assertEqual(out["R"]["exec"], [False, False])
        self.assertEqual(out["R"]["cli"], [f"gh pr comment {EXTERNAL} --body-file nudge.md", f"gh pr view --web {EXTERNAL}"])
        self.assertEqual((out["posts"], out["confirms"]), ([], []))
        # sd:5628 reverses the copy-only ruling of PR #72's review 2: Open on GitHub opens the pull request, nudge still opens nothing.
        self.assertEqual(out["opened"], [EXTERNAL])
        # sd:5887: the draft is in Details, editable, and names where Post nudge would post it.
        self.assertIn('<textarea class="cap-in" id="nudge-text"', out["R"]["det"])
        self.assertIn(f"Nothing is posted until Post nudge, which asks first and names {EXTERNAL}.", out["R"]["det"])

    def test_post_nudge_asks_naming_the_pull_request_and_posts_the_edited_draft(self):
        out = self.run_page("""const ext = C.get('github:""" + EXTERNAL + """'), post = cmd('contribution.post');
const was = ANSWER; ANSWER = (p, b) => p === '/api/actions/run' ? [200, { action: b.action, label: 'Post nudge', line: 'l', exit: R.code || 0, output: 'HTTP 403', cut: false }] : was(p, b);
R.before = post.when(ext); shellRun(cmd('contribution.nudge'), ext); await flush(); R.after = post.when(ext); R.says = post.consequence(ext);
ELS.details.listeners.input.forEach(f => f({ target: { id: 'nudge-text', value: 'Any news on this?' } }));
R.code = 1; shellRun(post, ext); await flush(); R.code = 0; shellRun(post, ext); await flush(); R.gone = post.when(ext);""")
        self.assertEqual((out["R"]["before"], out["R"]["after"], out["R"]["gone"]), ("draft a nudge first", True, "draft a nudge first"))
        self.assertIn(f"as your comment on {EXTERNAL}", out["R"]["says"])
        nudge = {"action": "contribution.nudge", "key": "github:" + EXTERNAL, "body": "Any news on this?"}
        self.assertEqual([{k: v for k, v in b.items() if k != "revision"} for p, b, _ in out["posts"]], [nudge, nudge])
        self.assertEqual(out["confirms"], ["contribution.post", "contribution.post"])
        toasts = [t[0] for t in out["toasts"]]
        self.assertTrue(any("gh exited 1: HTTP 403" in t for t in toasts), toasts)
        self.assertEqual(toasts[-1], f"Nudge posted on {EXTERNAL}")

    def sync_answer(self, finished):
        """The page's document, a started sync, and its state: running on the first read, then `finished`."""
        return ("""(() => { let n = 0; return (path, body) => path === '/api/contributions/page' ? [200, DOC]
          : path === '/api/shadow/sync' ? [202, { running: true }] : path === '/api/shadow/state' ? (n++ ? [200, %s] : [200, { running: true }])
          : [404, { error: 'no answer' }]; })()""" % json.dumps(finished))

    def test_rerun_collector_heads_the_open_view_and_its_button_runs_the_sync(self):
        """sd:7211: Re-run collector heads the Open view, as Clean all safe heads Health's Checkouts: the collector's row
        actions in the Open head, Hidden too, so it needs no Collector pick first. Its button runs collector.sync."""
        rows = r"""C.rowActions = id => { const o = C.get(id), p = o && REG.find(c => c.on === o.type && c.primary?.(o));
  return mk`<span class="rowact">${p ? mk`<button data-cmd="${p.id}" data-obj="${id}">${p.label}</button>` : ''}<button data-menu-for="${id}">more</button></span>`; };
"""
        done = {"running": False, "error": None, "trackers": []}
        out = self.run_page(f"""R.open = ELS.tally.html; {self.SHOW_HIDDEN} R.hidden = ELS.tally.html;
const [, id, obj] = /data-cmd="([^"]+)" data-obj="([^"]+)"/.exec(R.open) || [];
R.ran = [id, obj]; if (id) {{ shellRun(cmd(id), C.get(obj)); await flush(); }}""", answer=self.sync_answer(done), extra=rows)
        for head in ("open", "hidden"):
            self.assertIn('<button data-cmd="collector.sync" data-obj="collector">Re-run collector</button>', out["R"][head], head)
        self.assertEqual(out["R"]["ran"], ["collector.sync", "collector"])
        self.assertEqual(out["posts"], [["/api/shadow/sync", {}, 64]])
        self.assertEqual(out["gets"][-1], "/api/contributions/page", "the page did not read again when the sync ended")

    def test_rerun_collector_starts_the_servers_sync_and_reads_the_page_again_when_it_ends(self):
        # sd:2207: the route runs sd shadow sync in a server thread; the page asks until it ends, then reads again. A quiet
        # success says nothing more, and an unconfigured Jira is not a problem.
        done = {"running": False, "error": None, "trackers": [
            {"tracker": "github", "ok": True, "configured": True, "reason": "", "lines": ["shadow sync: 3 row(s)"]},
            {"tracker": "jira", "ok": False, "configured": False, "reason": "JIRA_URL is not set", "lines": []}]}
        out = self.run_page("""R.exec = cmd('collector.sync').executes; R.cli = cmd('collector.sync').cli(C.get('collector'));
shellRun(cmd('collector.sync'), C.get('collector')); await flush();""", answer=self.sync_answer(done))
        self.assertIn(out["R"].get("exec"), (None, True))
        self.assertEqual(out["R"]["cli"], "sd shadow sync --max-seconds 120")
        self.assertEqual(out["posts"], [["/api/shadow/sync", {}, 64]])
        self.assertEqual(out["gets"], ["/api/contributions/page", "/api/shadow/state", "/api/shadow/state", "/api/contributions/page"])
        self.assertEqual([t[0] for t in out["toasts"]], ["Shadow sync started · Contributions reads again when it ends"])

    def test_the_toolbar_has_no_refresh_and_re_run_collector_still_syncs(self):
        """sd:5750: the operator ruled out the toolbar's Refresh (sd:5630). Read again reads; Re-run collector syncs."""
        page = (V2 / "contributions.html").read_text(encoding="utf-8")
        self.assertNotIn('id="contrib-refresh"', page)
        self.assertNotIn("contrib-refresh", PAGE_JS)
        out = self.run_page("R.title = window.PAGE_AGAIN.title; R.sync = cmd('collector.sync').label;")
        self.assertEqual(out["R"]["title"], "Refresh contributions")
        self.assertEqual(out["R"]["sync"], "Re-run collector")

    def test_a_sync_that_ends_with_a_failed_tracker_or_a_broken_run_says_why(self):
        failed = {"running": False, "error": None, "trackers": [
            {"tracker": "github", "ok": False, "configured": True, "reason": "gh: not logged in", "lines": []}]}
        out = self.run_page("shellRun(cmd('collector.sync'), C.get('collector')); await flush();", answer=self.sync_answer(failed))
        self.assertEqual(out["toasts"][-1][0], "Shadow sync ended with a problem: github: gh: not logged in")
        broken = {"running": False, "error": "OperationalError: database is locked", "trackers": []}
        out = self.run_page("shellRun(cmd('collector.sync'), C.get('collector')); await flush();", answer=self.sync_answer(broken))
        self.assertEqual(out["toasts"][-1][0], "Shadow sync ended with a problem: OperationalError: database is locked")

    # sd:8146: Refresh from GitHub in the head, beside the GitHub sync line, runs Re-run collector's sync.
    CLICK_REFRESH = ("ELS.source.listeners.click.forEach(f => f({ target: { closest: s => s === '[data-gh-refresh]' ? {} : null } }));"
                     " await flush();")

    def refresh_answer(self, start="[202, { running: true }]"):
        """A sync that reads running once, then ends; the second page read carries a newer last success."""
        return ("""(() => { let n = 0, reads = 0; OUT.during = [];
          const newer = JSON.parse(JSON.stringify(DOC)); newer.collector = { ...(newer.collector || {}), state: 'fresh', last_success_at: '2026-09-06T01:00:00Z' };
          return (path, body) => path === '/api/contributions/page' ? [200, reads++ ? newer : DOC]
          : path === '/api/shadow/sync' ? %s
          : path === '/api/shadow/state' ? (OUT.during.push(ELS.source.html), n++ ? [200, { running: false, error: null, trackers: [] }] : [200, { running: true }])
          : [404, { error: 'no answer' }]; })()""" % start)

    def test_refresh_from_github_posts_the_sync_once_and_rereads_when_it_ends(self):
        out = self.run_page("R.before = ELS.source.html; R.page = window.PAGE_COMMANDS.map(c => c.label);" + self.CLICK_REFRESH
                            + " R.after = ELS.source.html;", answer=self.refresh_answer())
        self.assertIn("Refresh from GitHub", out["R"]["page"])
        self.assertRegex(out["R"]["before"], r'GitHub sync [^<]*(<time[^>]*></time>)?</span>\s*<button class="linkish" type="button" data-gh-refresh>Refresh from GitHub</button>')
        self.assertEqual(out["posts"], [["/api/shadow/sync", {}, 64]])
        self.assertEqual(out["gets"][-1], "/api/contributions/page", "the page did not read again when the sync ended")
        self.assertIn('last success <time class="rel" datetime="2026-09-06T01:00:00Z">', out["R"]["after"])

    def test_refresh_from_github_is_off_and_says_refreshing_while_the_sync_runs(self):
        out = self.run_page(self.CLICK_REFRESH + " R.after = ELS.source.html;", answer=self.refresh_answer())
        self.assertTrue(out["during"])
        for seen in out["during"]:
            self.assertIn('data-gh-refresh disabled>Refreshing…</button>', seen)
        self.assertIn("data-gh-refresh>Refresh from GitHub</button>", out["R"]["after"])

    def test_the_palette_refresh_from_github_runs_the_same_sync(self):
        out = self.run_page("window.PAGE_COMMANDS.find(c => c.label === 'Refresh from GitHub').run(); await flush();",
                            answer=self.refresh_answer())
        self.assertEqual(out["posts"], [["/api/shadow/sync", {}, 64]])
        self.assertEqual(out["gets"][-1], "/api/contributions/page")

    def test_a_dashboard_sync_already_running_is_followed_not_an_error(self):
        # sd:8146: the 409 for the dashboard's own live run (its state reads running): the page waits on it and reads again.
        busy = "[409, { error: 'A shadow sync started at 2026-10-06T15:00:00Z is still running. Wait for it to finish.' }]"
        out = self.run_page(self.CLICK_REFRESH, answer=self.refresh_answer(busy))
        self.assertEqual([t[0] for t in out["toasts"]], ["A shadow sync is already running · Contributions reads again when it ends"])
        self.assertIn("/api/shadow/state", out["gets"])
        self.assertEqual(out["gets"][-1], "/api/contributions/page")

    def test_a_sync_held_outside_the_dashboard_is_said_not_followed(self):
        # `sd shadow sync` (a terminal, the nightly job) holds the lock: the route answers 409, but /api/shadow/state is
        # the dashboard's run only and reads idle, here with an older run's failure. The page promises no reread it cannot
        # keep, toasts no stale failure, and leaves Refresh on.
        busy = "[409, { error: 'another shadow sync is already running against this database; wait for it to finish' }]"
        idle = """(() => { OUT.during = []; return (path, body) => path === '/api/contributions/page' ? [200, DOC]
          : path === '/api/shadow/sync' ? %s
          : path === '/api/shadow/state' ? (OUT.during.push(ELS.source.html), [200, { running: false, error: 'OperationalError: old run', trackers: [] }])
          : [404, { error: 'no answer' }]; })()""" % busy
        out = self.run_page(self.CLICK_REFRESH + " R.after = ELS.source.html;", answer=idle)
        self.assertEqual([t[0] for t in out["toasts"]], [
            "A shadow sync outside the dashboard is running: another shadow sync is already running against this database;"
            " wait for it to finish · try again after it ends"])
        self.assertEqual(out["gets"], ["/api/contributions/page", "/api/shadow/state"], "the page reread before the sync ended")
        self.assertIn("data-gh-refresh>Refresh from GitHub</button>", out["R"]["after"])

    def test_off_commands_name_their_reason(self):
        out = self.run_page("""const w = (id, key) => cmd(id).when(C.get(key));
R.off = [w('contribution.ack', 'issue:""" + ISSUE + """'), w('contribution.nudge', 'item:4'), w('contribution.nudge', 'issue:""" + ISSUE + """'),
  w('contribution.open', 'item:4'), w('contribution.nudge', 'github:""" + EXTERNAL + """')];""")
        self.assertEqual(out["R"]["off"], ["no attention event on this row", "not filed on GitHub", "the next step is yours, not theirs",
                                           "not filed on GitHub", True])

    def test_the_page_draws_lanes_lamps_and_settled_counts_and_claims_no_github_reading(self):
        # The document here comes from rows without settle dates, as a pack before sd:5303 sends them.
        out = self.run_page("R.note = ELS['tp-note'].textContent; R.tally = ELS['settled-tally'].textContent;")
        rows, lamps, settled = out["html"]["rows"], out["html"]["annunciator"], out["html"]["bars"]
        self.assertEqual(re.findall(r'<tr class="lane" data-lane="(\w+)"', rows), ["newly_unblocked", "awaiting_you", "awaiting_them"])
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', rows), [r["key"] for r in self.doc["rows"]])
        self.assertEqual(re.findall(r'data-cell="(\w+)" data-state="(\w+)"', lamps),
                         [("newly_unblocked", "caution"), ("awaiting_you", "caution"), ("awaiting_them", "ok"), ("github", "unknown")])
        self.assertIn("not recorded", lamps)
        self.assertEqual(cell_grammar(self, lamps), [("button", "caution"), ("button", "caution"), ("button", "ok"), ("div", "unknown")])
        self.assertNotIn("GitHub now", rows)
        self.assertNotIn("GitHub merged", rows)
        self.assertIn('title="example/project">example/project</span><span class="v">2</span>', settled)
        self.assertIn("This version records no merge or close dates", out["R"]["note"])
        self.assertEqual(out["R"]["tally"], "3 rows · 2 merged · 1 closed")
        self.assertEqual(out["attention"], {"state": "caution", "n": 2, "what": "contributions want you"})
        self.assertEqual(out["states"][0]["kind"], "loading")
        self.assertEqual((out["states"][-1]["kind"], out["states"][-1]["title"]), ("partial", "GitHub sync not fresh"))

    def dated(self):
        """The document with settle dates: the newly unblocked row merged on GitHub, three settled rows in the window."""
        self.doc["github_dates"] = True
        for row in self.doc["rows"]:
            row.update(merged_at=None, closed_at=None)
        self.doc["rows"][0].update(merged_at="2026-09-05T03:00:00Z", closed_at="2026-09-05T03:00:00Z")
        self.doc["counts"]["internal"]["github"] = 1
        self.doc["settled_recent"] = [{"at": "2026-09-05T03:00:00Z", "how": "merged", "internal": True},
                                      {"at": "2026-09-05T01:00:00Z", "how": "closed", "internal": True},
                                      {"at": "2026-08-01T10:00:00Z", "how": "merged", "internal": False}]
        self.doc["settled_older"] = {"internal": 0, "external": 2}
        self.doc["settled_undated"] = {"internal": 1, "external": 0}

    def test_github_settle_dates_light_the_lamp_date_the_row_and_details(self):
        # sd:5301: the dates come from the shadow collector; the dashboard still makes no GitHub call.
        self.dated()
        out = self.run_page(f"R.rows = ELS.rows.html; R.lamps = ELS.annunciator.html; open('{ONE}'); R.d = ELS.details.html;")
        self.assertIn(('github', 'caution'), re.findall(r'data-cell="(\w+)" data-state="(\w+)"', out["R"]["lamps"]))
        self.assertEqual(cell_grammar(self, out["R"]["lamps"])[-1], ("button", "caution"))
        self.assertNotIn("does not read GitHub", out["R"]["lamps"])
        self.assertIn('GitHub merged <time class="rel" datetime="2026-09-05T03:00:00Z"></time>', out["R"]["rows"])
        self.assertEqual(out["R"]["rows"].count("GitHub merged"), 1)
        self.assertIn('<dt>On GitHub</dt><dd>merged <time class="rel" datetime="2026-09-05T03:00:00Z" data-long=""></time></dd>', out["R"]["d"])
        out = self.run_page("R.rows = ELS.rows.html;", search="?lane=github")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), [ONE])
        self.assertEqual(out["urls"][-1], {"lane": "github"})

    def test_settled_per_day_counts_local_days(self):
        # 03:00 and 01:00 UTC on 5 September are the evening of the 4th in Denver and the 5th in Tokyo.
        self.dated()
        body = "R.tp = ELS.tp.html; R.label = ELS.tp.attrs['aria-label']; R.note = ELS['tp-note'].textContent;"
        keep = "ELS.tp = El('tp'); ELS.tp.attrs = {}; ELS.tp.setAttribute = (k, v) => { ELS.tp.attrs[k] = v; };"
        denver, tokyo = self.run_page(body, extra=keep, tz="America/Denver"), self.run_page(body, extra=keep, tz="Asia/Tokyo")
        self.assertIn("<title>Sep 4: 1 merged, 1 closed</title>", denver["R"]["tp"])
        self.assertIn("<title>Sep 5: 1 merged, 1 closed</title>", tokyo["R"]["tp"])
        self.assertEqual(denver["R"]["tp"].count("<title>"), 28)
        self.assertEqual(denver["R"]["label"], "1 merged and 1 closed from Aug 10 to Sep 6; busiest day Sep 4 with 2")
        # The row from 1 August is outside the window: it counts as older, with the two the server counted.
        self.assertIn("3 settled before this window.", denver["R"]["note"])
        self.assertIn("1 settled row has no merge or close date.", denver["R"]["note"])
        self.assertNotIn("No merge or close dates recorded", denver["R"]["note"])

    def test_a_document_without_settle_dates_says_they_are_not_recorded(self):
        # Older state: a pack before sd:5303 sends no dates; the lamp and the chart say unknown, never zero.
        out = self.run_page("R.lamps = ELS.annunciator.html; R.tp = ELS.tp.html; R.note = ELS['tp-note'].textContent;")
        self.assertIn(('github', 'unknown'), re.findall(r'data-cell="(\w+)" data-state="(\w+)"', out["R"]["lamps"]))
        self.assertIn("not recorded", out["R"]["lamps"])
        self.assertEqual(out["R"]["tp"], "")
        self.assertIn("This version records no merge or close dates", out["R"]["note"])

    def test_each_row_links_its_issues_in_a_new_tab_and_a_row_without_shows_none(self):
        # sd:5308: GitHub as owner/repo#N, Jira as its key; a link opens its page and leaves the row unselected.
        self.doc["rows"][2]["linked"] = [{"url": ISSUE, "kind": "github", "key": "example/project#2"},
                                         {"url": "https://jira.example.test/browse/ABC-12", "kind": "jira", "key": "ABC-12"}]
        out = self.run_page(f"""R.rows = ELS.rows.html; open('github:{EXTERNAL}'); R.d = ELS.details.html; open('{ONE}');
ELS.rows.listeners.click[0]({{ target: {{ closest: s => s === 'a' ? {{}} : s === 'tr[data-id]' ? {{ dataset: {{ id: 'github:{EXTERNAL}' }} }} : null }} }});
R.after = ELS.details.html;""")
        github = f'<a class="ext" href="{ISSUE}" target="_blank" rel="noopener noreferrer">example/project#2</a>'
        jira = '<a class="ext" href="https://jira.example.test/browse/ABC-12" target="_blank" rel="noopener noreferrer">ABC-12</a>'
        self.assertIn(f'<span class="links">{github} {jira}</span>', out["R"]["rows"])
        self.assertEqual(out["R"]["rows"].count('class="links"'), 1)
        self.assertIn(f"<dt>Linked</dt><dd>{github} {jira}</dd>", out["R"]["d"])
        self.assertNotIn("Contribution 3", out["R"]["after"])

    def test_the_repo_is_its_own_column_and_the_title_leads_with_its_number(self):
        # sd:6696: Repo is a sortable column after the state; the title reads "#<n> <title>" and the meta line drops both.
        out = self.run_page("R.head = ELS['rows-head'].html; R.rows = ELS.rows.html;")
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["head"]), ["repo", "what", "at"])
        row = re.search(r'<tr data-id="github:https://github.com/example/project/pull/1".*?</tr>', out["R"]["rows"], re.S).group(0)
        self.assertIn('<td class="repo">example/project</td>', row)
        self.assertRegex(row, r'<td class="what"><button type="button">#1 [^<]+</button>')
        self.assertNotRegex(row, r'class="meta">[^<]*(example/project|#1)')
        self.assertIn('<td class="repo">local</td>', out["R"]["rows"])
        self.assertEqual(out["R"]["rows"].count('colspan="5"'), out["R"]["rows"].count('<tr class="lane"'))

    def test_a_link_the_page_cannot_open_safely_is_not_drawn(self):
        # The server filters too; the page draws only an https link it can name.
        self.doc["rows"][0]["linked"] = [{"url": "javascript:alert(1)", "kind": "jira", "key": "ABC-1"}, "ABC-2"]
        self.doc["rows"][1].pop("linked", None)
        out = self.run_page("R.rows = ELS.rows.html;")
        self.assertNotIn('class="links"', out["R"]["rows"])
        self.assertNotIn("javascript:", out["R"]["rows"])

    def test_the_scope_and_lane_filter_the_ledger(self):
        out = self.run_page("""R.all = ELS.rows.html; document.dispatchEvent(new CustomEvent('contributions:scope', { detail: 'external' }));
R.ext = ELS.rows.html; R.bars = ELS.bars.html;""")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["ext"]), ["github:" + EXTERNAL, "item:4"])
        self.assertNotIn("example/project", out["R"]["bars"])
        self.assertEqual(out["urls"][-1], {"scope": "external"})
        out = self.run_page("R.rows = ELS.rows.html;", search="?lane=awaiting_them&q=lib")
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), ["github:" + EXTERNAL])
        self.assertEqual(out["urls"][-1], {"lane": "awaiting_them", "q": "lib"})

    def test_a_header_sorts_the_rows_inside_their_lane_and_the_url_keeps_it(self):
        # sd:3455: the shell's sort header. Observed, newest first, is the server's order and the default; lanes stay.
        click = "ELS['rows-head'].listeners.click[0]({{ target: {{ closest: s => s === 'button[data-sort]' ? {{ dataset: {{ sort: '{k}' }} }} : null }} }});"
        ids = r'<tr data-id="([^"]+)"'
        out = self.run_page(f"R.head = ELS['rows-head'].html; R.rows = ELS.rows.html; {click.format(k='at')} R.up = ELS.rows.html; R.url = OUT.urls[OUT.urls.length - 1];"
                            f" {click.format(k='what')} R.what = ELS.rows.html; R.head2 = ELS['rows-head'].html;")
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["head"]), ["repo", "what", "at"])
        self.assertEqual(re.findall(r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"', out["R"]["head"]), [("descending", "at")])
        mine = ["github:https://github.com/example/project/pull/1", "issue:" + ISSUE]
        self.assertEqual(re.findall(ids, out["R"]["rows"]), mine + ["github:" + EXTERNAL, "item:4"])
        self.assertEqual(re.findall(ids, out["R"]["up"]), mine + ["item:4", "github:" + EXTERNAL])
        self.assertEqual(re.findall(r'<tr class="lane" data-lane="(\w+)"', out["R"]["up"]), ["newly_unblocked", "awaiting_you", "awaiting_them"])
        self.assertEqual(out["R"]["url"], {"sort": "at", "dir": "asc"})
        self.assertEqual(re.findall(r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"', out["R"]["head2"]), [("ascending", "what")])
        self.assertEqual(re.findall(ids, out["R"]["what"]), mine + ["github:" + EXTERNAL, "item:4"])
        out = self.run_page("R.rows = ELS.rows.html;", search="?sort=at&dir=asc")
        self.assertEqual(re.findall(ids, out["R"]["rows"]), mine + ["item:4", "github:" + EXTERNAL])

    def test_a_cut_document_says_how_many_rows_it_left_out(self):
        self.doc["truncated"], self.doc["open_total"] = True, 900
        out = self.run_page("R.f = [ELS.filtered.hidden, ELS.filtered.textContent];")
        self.assertEqual(out["R"]["f"], [False, "The document lists 4 of 900 open rows; v1 /classic/contributions lists every one."])

    def test_a_cut_document_counts_from_the_totals_not_the_rows_it_carries(self):
        self.doc["truncated"], self.doc["open_total"] = True, 904
        # Internal: 300 + 100 open, 50 unknown, 7 linked, 2 unfiled. External, from rows(): 2 awaiting them, both unknown, 1 unfiled.
        self.doc["counts"]["internal"].update(newly_unblocked=300, awaiting_you=100, unknown=50, linked=7, unfiled=2)
        self.doc["attention"] = contribution_screen._attention(self.doc["counts"])
        out = self.run_page("""R.sub = ELS.sub.html; R.lamps = ELS.annunciator.html; R.tally = ELS.tally.html; R.source = ELS.source.html;
document.dispatchEvent(new CustomEvent('contributions:scope', { detail: 'external' })); R.ext = ELS.annunciator.html;""")
        self.assertEqual(out["attention"], {"state": "caution", "n": 400, "what": "contributions want you"})
        self.assertIn('<span id="open-n">402</span>', out["R"]["sub"])
        self.assertEqual(re.findall(r"<b>(\d+)</b>", out["R"]["lamps"]), ["300", "100", "2"])
        self.assertIn("▲ 400 yours", out["R"]["tally"])
        self.assertIn("<b>52</b> of 402 open rows unknown freshness", out["R"]["source"])
        self.assertIn("7 linked items · 3 unfiled", out["R"]["source"])
        self.assertEqual(re.findall(r"<b>(\d+)</b>", out["R"]["ext"]), ["0", "0", "2"])

    def test_a_cut_document_with_no_carried_row_in_the_scope_says_the_limit_left_them_out(self):
        # Review 4 of PR #72 (9370ec932593): the ledger said "Nothing open" while the counts showed open rows.
        self.doc["truncated"], self.doc["open_total"] = True, 900
        self.doc["rows"] = [r for r in self.doc["rows"] if r["internal"]]
        out = self.run_page("""document.dispatchEvent(new CustomEvent('contributions:scope', { detail: 'external' })); R.rows = ELS.rows.html;""")
        self.assertNotIn("Nothing open", out["R"]["rows"])
        self.assertIn("2 open rows in external repositories were left out by the limit", out["R"]["rows"])

    def test_a_failed_read_and_an_empty_projection_each_say_so(self):
        out = self.run_page("", answer="() => [500, { error: 'boom' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])
        self.assertEqual(out["attention"], {"state": "unknown", "n": 0, "what": "contributions not read"})
        empty = dict(self.doc, rows=[], total=0, open_total=0, settled=[])
        out = self.run_page("", answer=f"() => [200, {json.dumps(empty)}]")
        self.assertEqual((out["states"][-1]["kind"], out["states"][-1]["title"]), ("empty", "No contributions"))

    def test_details_name_the_scope_and_link_only_github(self):
        out = self.run_page("open('github:https://github.com/example/project/pull/1'); R.a = ELS.details.html; open('item:4'); R.b = ELS.details.html;")
        self.assertIn("example/project · internal (sd manages it)", out["R"]["a"])
        self.assertIn('href="https://github.com/example/project/pull/1"', out["R"]["a"])
        self.assertIn("local · external", out["R"]["b"])
        self.assertNotIn("href=", out["R"]["b"])

    def test_details_show_dependencies_evidence_and_delivery(self):
        # sd:3394: what only /classic/contributions showed. A link only for a GitHub URL; an uncertain delivery is unknown.
        row = next(r for r in self.doc["rows"] if r["key"] == "github:" + EXTERNAL)
        row.update(tested_commit="c" * 40, evidence={"state": "read", "verified": False, "checks": [
            {"command": "…/run.sh tests", "directory": "local: x", "exit_code": 1, "commit": "c" * 40, "artifact": "local: r.xml",
             "sha256": "d" * 64}]},
            dependencies={"state": "read", "entries": [{"kind": "item", "item": 9}, {"kind": "merge", "url": ISSUE},
                {"kind": "release", "repo": "other/lib", "tag": None, "contains_pull": EXTERNAL, "package": {"name": "lib"}},
                {"kind": "unknown", "reason": "dependency kind vote is not one sd records"}]},
            delivery={"state": "read", "entries": [
                {"source": "github:" + EXTERNAL, "reason": "Maintainer approved", "status": "sent", "acknowledged_at": "2026-09-09T13:00:00Z"},
                {"source": "github:" + EXTERNAL, "reason": "CI failed", "status": "uncertain", "acknowledged_at": None}]})
        out = self.run_page(f"open('github:{EXTERNAL}'); R.d = ELS.details.html; open('item:4'); R.none = ELS.details.html;")
        d = out["R"]["d"]
        self.assertIn('<h3>Dependencies</h3>', d)
        self.assertIn('<a href="/tasks?row=9">Complete local item #9</a>', d)
        self.assertIn(f'Merge <a class="ext" href="{ISSUE}"', d)
        self.assertIn("Publish other/lib release <code>tag not selected</code> containing", d)
        self.assertIn("package lib, version not selected", d)
        self.assertIn('<span class="g-unknown" aria-hidden="true">▨</span> Not read: dependency kind vote is not one sd records', d)
        self.assertIn('<h3>Evidence</h3>', d)
        self.assertIn("Evidence identity is unverified", d)
        self.assertIn("<code>…/run.sh tests</code>", d)
        self.assertIn('<span class="g-warning" aria-hidden="true">■</span> exit 1', d)
        self.assertIn("local: r.xml", d)
        self.assertIn('<h3>Notification delivery</h3>', d)
        self.assertIn('<span class="g-ok" aria-hidden="true">●</span> Maintainer approved · sent · acknowledged', d)
        self.assertIn('<span class="g-unknown" aria-hidden="true">▨</span> CI failed · delivery uncertain: the sender did not confirm it', d)
        self.assertIn("Tested commit", d)
        none = out["R"]["none"]
        for text in ("No dependencies recorded.", "No test evidence recorded.", "No notifications recorded."):
            self.assertIn(text, none)

    def test_details_say_a_section_was_not_read_and_why(self):
        row = next(r for r in self.doc["rows"] if r["key"] == "item:4")
        why = "the recorded evidence is not a list"
        row.update(evidence={"state": "unknown", "reason": why}, dependencies={"state": "unknown", "reason": "x is not a list"},
                   delivery={"state": "unknown", "reason": "y is not a record"})
        out = self.run_page("open('item:4'); R.d = ELS.details.html;")
        for reason in (why, "x is not a list", "y is not a record"):
            self.assertIn(f'<span class="g-unknown" aria-hidden="true">▨</span> Not read: {reason}', out["R"]["d"])
        for text in ("No dependencies recorded.", "No test evidence recorded.", "No notifications recorded."):
            self.assertNotIn(text, out["R"]["d"])
        # Older state: a server from before sd:3394 sends no section, and the page says so instead of drawing it empty.
        for key in ("evidence", "dependencies", "delivery"):
            row.pop(key)
        out = self.run_page("open('item:4'); R.d = ELS.details.html;")
        self.assertIn("Not read: the server sent no evidence", out["R"]["d"])
        self.assertNotIn("No test evidence recorded.", out["R"]["d"])

    def test_an_unfiled_draft_reads_as_a_draft_from_the_flag_alone(self):
        row = next(r for r in self.doc["rows"] if r["key"] == "item:4")
        row["has_draft"] = True
        out = self.run_page("R.rows = ELS.rows.html;")
        self.assertIn('<td class="repo">local</td>', out["R"]["rows"])
        self.assertIn('<span class="meta">Unfiled issue draft</span>', out["R"]["rows"])

    def test_the_page_text_names_the_route_make_task_posts(self):
        # Review 4 of PR #72 (0435ca1e4fbb): the help and the page note still said a followup through /api/items.
        page = (V2 / "contributions.html").read_text(encoding="utf-8")
        routes = re.findall(r"post\('(/api/[^']+)'", PAGE_JS)
        self.assertEqual(routes, ["/api/contributions/view", "/api/contributions/acknowledge", "/api/actions/run", "/api/contributions/task",
                                  "/api/contributions/dismiss"])
        # Re-run collector is the shared declaration (commands.js, sd:3524); the page help still names its route.
        self.assertIn("window.commands.syncCollector(", PAGE_JS)
        for route in routes + ["/api/shadow/sync"]:
            self.assertIn(f"POST {route}", page)
        self.assertNotIn("/api/items", page)
        self.assertNotIn("followup", page)
        self.assertIn("Make task files a task linked to the contribution", page)

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        self.assertNotIn("innerHTML", PAGE_JS)
        self.assertNotIn("setAttribute('style'", PAGE_JS)
        self.assertNotRegex(PAGE_JS, r"style=\\?\"")
        # sd:5753: one replaceState, before the shell runs, moves an older #view=hidden link; every other URL write is shell.url.
        self.assertEqual(PAGE_JS.count("history.replaceState"), 1)
        self.assertLess(PAGE_JS.index("history.replaceState"), PAGE_JS.index("addEventListener('DOMContentLoaded'"))
        self.assertNotRegex(PAGE_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", PAGE_JS)
        self.assertNotIn("CONTRIBUTIONS_DATA", PAGE_JS)


#: Boxes that parse the buttons drawn into them, so a test can focus one and see where focus is after a redraw. A button is
#: attached while its box still holds the markup it came from; `document.activeElement` is the button last focused.
BOXES = r"""
OUT.focused = [];
const BUTTONS = box => { if (box.parsedFrom === box.html) return box.buttons; box.parsedFrom = box.html;
  const h = box.html || '', found = [], re = /<tr data-id="([^"]*)"|<button\b([^>]*)>/g; let m, row = null;
  while ((m = re.exec(h))) { if (m[1] !== undefined) { row = m[1]; continue; }
    const ds = {}; for (const [, k, v] of m[2].matchAll(/data-([\w-]+)="([^"]*)"/g)) ds[k.replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v;
    const b = { box, html: h, row, dataset: ds, closest: sel => sel === 'button.cell' ? b : sel === 'tr[data-id]' && b.row != null ? { dataset: { id: b.row } } : null,
      focus() { document.activeElement = b; OUT.focused.push(ds); } };
    found.push(b); }
  return box.buttons = found; };
['rows', 'filed-rows', 'details', 'annunciator', 'tally'].forEach(id => { const box = document.getElementById(id);
  box.querySelectorAll = sel => sel === 'button' ? BUTTONS(box) : []; box.contains = a => !!a && a.box === box && a.html === box.html; });
const live = id => { const o = C.get(id); return REG.filter(c => c.on === o.type && (!c.when || c.when(o) === true)); };
C.rowActions = id => { const p = live(id).find(c => c.primary?.(C.get(id)));
  return mk`<span class="rowact">${p ? mk`<button data-cmd="${p.id}" data-obj="${id}">${p.label}</button>` : ''}<button data-menu-for="${id}">more</button></span>`; };
C.bar = id => mk`<div class="actions">${live(id).map(c => mk`<button data-cmd="${c.id}" data-obj="${id}">${c.label}</button>`)}</div>`;
const button = (box, f) => BUTTONS(ELS[box]).find(f);
const where = () => { const a = document.activeElement; return a ? [a.box.id, a.dataset, a.box.contains(a)] : null; };
"""
#: Every read of the document waits until the test answers it; a read started after a write sees the write.
HELD = r"""
const held = [], copy = d => JSON.parse(JSON.stringify(d));
let wrote = false; const after = copy(DOC); after.rows.forEach(r => { r.event_ids = []; });
const HELD_ANSWER = (path, body) => { if (path !== '/api/contributions/page') { wrote = true; return [200, { ok: true }]; }
  const doc = wrote ? after : copy(DOC); return new Promise(ok => held.push(() => ok([200, doc]))); };
const answerAll = async () => { while (held.length) { held.shift()(); await flush(); } };
"""
ONE = "github:https://github.com/example/project/pull/1"


class TheSharedReader(Script):
    """sd:2488: Contributions reads through the shell's reader, read.js, not a copy of its guards."""

    def test_the_page_reads_through_the_shared_reader(self):
        self.assertIn("window.shell.read(", PAGE_JS)
        self.assertNotIn("++generation", PAGE_JS)
        self.assertNotRegex(PAGE_JS, r"function retire\(")
        # The sync's state (sd:2207) is read by the shared Re-run collector in commands.js, not by the page.
        self.assertEqual(re.findall(r"getJSON\(([^)]*)\)", PAGE_JS), [])

    def test_a_refused_write_whose_read_fails_clears_the_rows_and_their_commands(self):
        answer = """(() => { let n = 0; return path => path === '/api/contributions/page' ? (n++ ? [500, { error: 'gone' }] : [200, DOC])
          : [409, { error: 'Events changed', reload: true }]; })()"""
        out = self.run_page(f"""shellRun(cmd('contribution.ack'), C.get('{ONE}')); await flush();
R.rows = ELS.rows.html; R.type = C.get('{ONE}').type; R.bars = ELS.bars.html;""", answer=answer)
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"]), [])
        self.assertIn("Not read: gone", out["R"]["rows"])
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertEqual(out["R"]["bars"], "")
        self.assertEqual(out["attention"], {"state": "unknown", "n": 0, "what": "contributions not read"})

    def test_a_reread_waits_for_a_read_that_started_before_its_write_landed(self):
        # The write barrier (PR 73, r4167523338): the second write lands while the first write's reread is in flight.
        # Two single runs: a bulk group's rows now read again once, after the group (pack sd:7852).
        out = self.run_page(f"""held.shift()(); await flush();
shellRun(cmd('contribution.ack'), C.get('{ONE}')); shellRun(cmd('contribution.ack'), C.get('{ONE}')); await flush();
R.mid = OUT.gets.length; await answerAll(); R.end = OUT.gets.length;""", extra=HELD, answer="HELD_ANSWER")
        self.assertEqual(out["R"]["mid"], 2, "a reread joined or overtook a read started before its write landed")
        self.assertEqual(out["R"]["end"], 3)

    def test_a_selected_row_the_reread_drops_moves_to_the_first_shown_row(self):
        # A guard: before the reader the page's reconcile call did the same in the real shell.
        newer = dict(self.doc, rows=[r for r in self.doc["rows"] if r["key"] != "github:" + EXTERNAL])
        answer = """(() => { let n = 0; return path => path !== '/api/contributions/page' ? [200, { ok: true }]
          : n++ ? [200, """ + json.dumps(newer) + """] : [200, DOC]; })()"""
        out = self.run_page(f"""open('github:{EXTERNAL}'); shellRun(cmd('contribution.ack'), C.get('{ONE}')); await flush(); R.d = ELS.details.html;""",
                            answer=answer)
        self.assertIn("Fix it&#39;s $HOME", out["R"]["d"])


class TheFiledView(Script):
    """sd:5488: the Filed view, its named states, Details and Edit."""

    def filed_doc(self):
        seed_filed(self.connection)
        with mock.patch.object(contribution_screen.contributions, "projection", return_value=rows()):
            self.doc = contribution_screen.document(self.connection, now=NOW)

    def test_the_filed_view_lists_this_years_rows_and_names_an_undated_one(self):
        self.filed_doc()
        out = self.run_page("window.shell.view.show('filed'); R.rows = ELS['filed-rows'].html; R.tally = ELS['filed-tally'].html;"
                            " R.source = ELS['filed-source'].html;")
        ids = re.findall(r'<tr data-id="([^"]+)"', out["R"]["rows"])
        self.assertEqual(ids, ["filed:github:" + FILED_ISSUE, "filed:jira:" + JIRA + "ABC-1", "filed:jira:" + JIRA + "ABC-4"])
        self.assertIn("created date not synced yet", out["R"]["rows"])
        self.assertIn("In Review", out["R"]["rows"])
        self.assertEqual(out["counts"]["filed"], 3)
        self.assertIn("3 filed in 2026", out["R"]["tally"])
        self.assertIn("1 created date not synced yet", out["R"]["tally"])
        # Each tracker's sync is named, and the rows whose filer is not synced are counted, never dropped silently.
        self.assertIn("Jira sync never", out["R"]["source"])
        self.assertIn("GitHub sync never", out["R"]["source"])
        self.assertIn("1 Jira ticket: whether you filed it is not synced yet", out["R"]["source"])

    def test_the_filed_tab_counts_open_rows_only(self):
        """sd:8018: a closed or merged filed row stays listed but leaves the tab's count."""
        self.filed_doc()
        self.doc["filed"]["rows"][0]["state"] = "closed"
        self.doc["filed"]["rows"][1]["state"] = "merged"
        out = self.run_page("window.shell.view.show('filed'); R.rows = ELS['filed-rows'].html;")
        self.assertEqual(len(re.findall(r'<tr data-id=', out["R"]["rows"])), 3)
        self.assertEqual(out["counts"]["filed"], 1, "sd:8018: the tab counted a closed row")

    def test_details_show_a_filed_ticket_and_edit_opens_it_in_a_new_tab(self):
        self.filed_doc()
        key = "filed:jira:" + JIRA + "ABC-1"
        out = self.run_page(f"""window.shell.view.show('filed'); open('{key}'); R.details = ELS.details.html;
R.cli = cmd('filed.edit').cli(C.get('{key}')); shellRun(cmd('filed.edit'), C.get('{key}')); await flush();
R.undated = (open('filed:jira:{JIRA}ABC-4'), ELS.details.html);""")
        details = out["R"]["details"]
        for text in ("Ticket ABC-1", "In Review", "Alex", "ABC", "Jira ticket"):
            self.assertIn(text, details)
        # sd:5629: the Key is the link to the ticket; there is no separate Edit in Jira link.
        self.assertIn(f'<dt>Key</dt><dd><a class="ext" href="{JIRA}ABC-1" target="_blank" rel="noopener noreferrer">ABC-1', details)
        self.assertNotIn("Edit in", details)
        self.assertEqual(out["opened"], [JIRA + "ABC-1"])
        self.assertEqual(out["posts"], [], "Edit wrote something")
        self.assertEqual(out["R"]["cli"], f"open '{JIRA}ABC-1'")
        self.assertIn("created date not synced yet", out["R"]["undated"])
        self.assertIn("not synced yet", out["R"]["undated"])

    def test_a_page_opened_on_the_filed_view_selects_and_filters_the_filed_rows(self):
        # Seen in the browser: the shell fires the first shell:view before the page listens, so a page opened on
        # #view=filed, or on the remembered view, filtered and selected on the Open table.
        self.filed_doc()
        out = self.run_page("R.details = ELS.details.html;", extra="VIEW_NOW = 'filed';")
        # The reader's first pick is the first filed row; the stand-in has no rows to walk, so the browser check covers the filter.
        self.assertIn("GitHub issue · filed by you", out["R"]["details"])

    def test_edit_is_off_for_a_link_the_page_cannot_open_safely(self):
        self.filed_doc()
        self.doc["filed"]["rows"][0]["url"] = "javascript:alert(1)"
        out = self.run_page(f"R.off = cmd('filed.edit').when(C.get('filed:github:{FILED_ISSUE}'));")
        self.assertEqual(out["R"]["off"], "no https link recorded")

    def test_a_tracker_that_did_not_read_is_named_and_no_rows_never_read_as_none_filed(self):
        self.filed_doc()
        self.doc["filed"] = {"year": 2026, "rows": [], "trackers": {
            "jira": {"state": "unread", "reason": "OperationalError: database is locked", "last_success_at": None, "filer_unknown": 0},
            "github": {"state": "unrecorded", "reason": "this sd stores no filed flag", "last_success_at": None, "filer_unknown": 0}}}
        out = self.run_page("window.shell.view.show('filed'); R.rows = ELS['filed-rows'].html; R.source = ELS['filed-source'].html;")
        self.assertIn("Filed not read: Jira: OperationalError: database is locked; GitHub: this sd stores no filed flag", out["R"]["rows"])
        self.assertIn("Jira not read: OperationalError: database is locked", out["R"]["source"])

    def test_a_stale_tracker_is_named_beside_the_rows_it_last_saw(self):
        self.filed_doc()
        self.doc["filed"]["trackers"]["jira"].update(state="stale", reason="HTTP 401", last_success_at="2026-09-01T10:00:00Z")
        out = self.run_page("window.shell.view.show('filed'); R.rows = ELS['filed-rows'].html; R.source = ELS['filed-source'].html;")
        self.assertIn("Jira sync stale", out["R"]["source"])
        self.assertIn("HTTP 401", out["R"]["source"])
        self.assertEqual(len(re.findall(r'<tr data-id="', out["R"]["rows"])), 3)

    def test_an_empty_filed_view_says_so_for_a_tracker_that_read(self):
        self.doc["filed"] = {"year": 2026, "rows": [], "trackers": {
            name: {"state": "fresh", "reason": "", "last_success_at": NOW, "filer_unknown": 0} for name in ("jira", "github")}}
        out = self.run_page("window.shell.view.show('filed'); R.rows = ELS['filed-rows'].html;")
        self.assertIn("Nothing filed in 2026 in Jira or on GitHub, as the last sync saw it", out["R"]["rows"])


class TheFocus(Script):
    """sd:2426: a redraw that replaces the focused control puts focus on its replacement, or on the nearest action."""

    def test_a_lamp_keeps_focus_after_it_toggles_the_lane_filter(self):
        out = self.run_page("""const lamp = button('annunciator', b => b.dataset.cell === 'awaiting_them'); lamp.focus();
ELS.annunciator.listeners.click[0]({ target: lamp }); R.on = where(); R.html = ELS.annunciator.html;""", extra=BOXES)
        self.assertEqual(out["R"]["on"], ["annunciator", {"cell": "awaiting_them", "state": "ok"}, True])
        self.assertIn('data-cell="awaiting_them" data-state="ok" aria-pressed="true"', out["R"]["html"])
        # sd:5064: Settled is its own view and the lamps filter the Open rows, so a lamp shows Open.
        self.assertEqual(out["shown"], ["open"])

    def test_open_is_the_first_view_and_settled_the_second(self):
        page = (V2 / "contributions.html").read_text(encoding="utf-8")
        # sd:5488: Filed, the tickets and issues the operator filed this year, has its own panel. sd:5753: the hidden rows
        # are Open's Show hidden filter, not a view.
        self.assertIn("window.PAGE_VIEWS = [{ id: 'open', label: 'Open' }, { id: 'settled', label: 'Settled' }, { id: 'filed', label: 'Filed' }];", PAGE_JS)
        self.assertLess(page.index('id="view-settled"'), page.index('<section class="filed" id="view-filed"'))
        self.assertLess(page.index('<section id="view-open"'), page.index('<section class="settled" id="view-settled"'))

    def test_the_scope_is_two_filter_chips_in_the_row_above_the_sections(self):
        """Pack sd:7084: Internal and External are filter chips in the row above the section heads, after Open | Hidden |
        All; none pressed is every repository, and a second press on the pressed one clears it."""
        page = (V2 / "contributions.html").read_text(encoding="utf-8")
        self.assertLess(page.index('id="open-filters"'), page.index('<section id="view-open"'))
        self.assertLess(page.index('id="hidden-seg"'), page.index('id="scope"'))
        self.assertNotIn("repo-scope", (V2 / "static" / "contributions.css").read_text(encoding="utf-8"))
        chip = r'data-f="scope" data-v="(\w+)" aria-pressed="(\w+)"'
        press = "ELS.scope.listeners.click[0]({ target: { closest: s => s === 'button[data-f=\"scope\"]' ? { dataset: { v: 'internal' } } : null } });"
        out = self.run_page(f"R.none = ELS.scope.html; {press} R.one = ELS.scope.html; {press} R.again = ELS.scope.html;")
        self.assertEqual(re.findall(chip, out["R"]["none"]), [("internal", "false"), ("external", "false")])
        self.assertEqual(re.findall(chip, out["R"]["one"]), [("internal", "true"), ("external", "false")])
        self.assertEqual(re.findall(chip, out["R"]["again"]), [("internal", "false"), ("external", "false")])

    def test_the_head_has_no_trackers_link_and_no_second_open(self):
        # sd:5064: the Open | Settled tabs are the page's views; the old Open | Trackers switch repeated Open.
        # sd:7362: the Trackers link is gone too; Operations > Trackers is reached from Operations.
        page = (V2 / "contributions.html").read_text(encoding="utf-8")
        self.assertNotIn('id="mode"', page)
        self.assertNotIn('<a href="/contributions"', page)
        self.assertNotIn("to-trackers", page)
        self.assertNotIn("area=trackers", page)
        self.assertNotIn("to-trackers", (V2 / "static" / "contributions.css").read_text(encoding="utf-8"))

    def test_a_landed_acknowledge_puts_focus_on_the_rows_action_menu(self):
        out = self.run_page(f"""DOC.rows.forEach(r => {{ r.event_ids = []; }});
button('rows', b => b.dataset.cmd === 'contribution.ack' && b.dataset.obj === '{ONE}').focus();
shellRun(cmd('contribution.ack'), C.get('{ONE}')); await flush(); R.on = where();""", extra=BOXES)
        self.assertEqual(out["R"]["on"], ["rows", {"menuFor": ONE}, True])

    def test_a_landed_acknowledge_from_details_puts_focus_on_the_next_command(self):
        out = self.run_page(f"""DOC.rows.forEach(r => {{ r.event_ids = []; }}); open('{ONE}');
button('details', b => b.dataset.cmd === 'contribution.ack').focus();
shellRun(cmd('contribution.ack'), C.get('{ONE}')); await flush(); R.on = where();""", extra=BOXES)
        box, names, attached = out["R"]["on"]
        self.assertEqual((box, attached), ("details", True))
        self.assertNotEqual(names.get("cmd"), "contribution.ack")
        self.assertEqual(names.get("obj"), ONE)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "contributions", "Contributions", "/contributions", ("/api/contributions/page", "/api/contributions/task", "/api/contributions/dismiss",
                                                                         "/api/contributions/view", "/api/shadow/sync", "/api/shadow/state")


if __name__ == "__main__":
    unittest.main()
