"""The lane document and Today's Queue view (sd:2585; the Queue page until pack sd:7234).

What this slice promises: `/api/queue` draws each managed repository's lane from `sd-ship lane list`, one row per
item in one of six states (merging, next, building, blocked, cancelled, landed), with load5 and the gate slots in the header.
The only write is `/api/queue/move`: it runs `sd-ship lane move|hold|release|cancel|retry` with exactly the page's item and
place, under the session cookie, CSRF and Origin checks, and refuses a revision the queue has moved past. Retry and Approve
(`retry --manual`) show on blocked rows only when `sd-ship lane retry --help` names `--expected-head` (sd:3012); Approve
only on a manual repo. Both carry the head the row showed. Release on a running row runs `lane cancel` with the claim the
row showed as `--expected-claim` (sd:3337), and a claim that moved on since is a 409.
Re-queue on a cancelled row runs `lane retry --expected-head` with the head the row showed (sd:3398), shows only when
`lane retry --help` names a cancelled entry (pack sd:3389), and runs nothing for an item whose newest entry is not cancelled.

`sd-ship` and `sd` are stubs on PATH that answer from a fixture and log their arguments; no test reaches a real lane.
One test runs the pinned pack's own `sd-ship` on a queue under a temporary `SD_LANE_ROOT`, so the order the page asks
for is the order `lane list` then reports. `today.js` runs under JavaScriptCore (osascript) with the Today stand-in
`test_v2_today_reader` defines; `/queue` answers 301 to Today's Queue view.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from sd_dashboard import hoa_screen, now_screen, pack_bin, places, queue_screen, v2
from sd_db import upsert_repo
from sd_db.writes import create_item
from support import NOW, ScreenCase
from test_v2_registry import GROUP_BLOCK
from test_v2_today import OSASCRIPT
from test_v2_today_reader import TODAY_JS, ReaderCase
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
CLOCK = queue_screen._when(NOW)

STUB = """#!/bin/sh
# A stand-in for sd-ship and sd: log the arguments, answer from the fixture.
printf '%s\\n' "$*" >> "$QUEUE_LOG"
case "$*" in
  *"lane list") if [ -f "$QUEUE_FIXTURE.$(basename "$2")" ]; then cat "$QUEUE_FIXTURE.$(basename "$2")"; else cat "$QUEUE_FIXTURE"; fi ;;
  "lane retry --help") if [ -n "$QUEUE_NO_RETRY" ]; then echo "invalid choice: 'retry'" >&2; exit 2; fi
     if [ -n "$QUEUE_OLD_RETRY" ]; then echo "usage: sd-ship lane retry [--manual] item"; else echo "usage: sd-ship lane retry [--manual] [--expected-head EXPECTED_HEAD] item"; fi
     if [ -z "$QUEUE_OLD_REQUEUE$QUEUE_OLD_RETRY" ]; then echo "  --expected-head  ...; a cancelled entry needs it (sd:3389)"; fi ;;
  "gate status --json") printf '{"slots": 4, "holders": [{"slot": 1}], "waiters": [], "load": [1, 2, 3]}\\n' ;;
  *) if [ -n "$QUEUE_SLEEP" ]; then exec sleep "$QUEUE_SLEEP"; fi
     if [ -n "$QUEUE_CRASH" ]; then echo "Traceback: $QUEUE_CRASH" >&2; exit 1; fi
     if [ -n "$QUEUE_REFUSE" ]; then printf '{"ok": false, "error": "%s", "code": "%s"}\\n' "$QUEUE_REFUSE" "$QUEUE_CODE"; exit "${QUEUE_EXIT:-3}"; fi
     printf '{"ok": true}\\n' ;;
esac
"""


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


def numbered(entries):
    """`entries` as a pack with `enqueue_seq` lists them before any move: each one's number is its place (sd:3419)."""
    return [{**row, "enqueue_seq": seq} for seq, row in enumerate(entries, 1)]


class Lane:
    """A registered git checkout, its lane folder and a stubbed `sd-ship` on PATH that lists `entries`."""

    def setUp(self):
        super().setUp()
        work = Path(self.tmp.name)
        self.checkout = work / "system"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q", "-b", "main")
        git(self.checkout, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q", "--allow-empty",
            "-m", "Dashboard: a page (#161)")
        self.merged = git(self.checkout, "rev-parse", "HEAD")
        upsert_repo(self.connection, str(self.checkout), remote="git@example.invalid:x.git", managed=1)
        self.lane = work / "lanes" / "system" / "lane"
        (self.lane / "logs").mkdir(parents=True)
        bin_dir = work / "bin"
        bin_dir.mkdir()
        for name in ("sd-ship", "sd"):
            (bin_dir / name).write_text(STUB)
            (bin_dir / name).chmod(0o755)
        self.log, self.fixture = work / "calls.log", work / "list.json"
        self.log.write_text("")
        patch = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "QUEUE_LOG": str(self.log),
                                             "QUEUE_FIXTURE": str(self.fixture), "TZ": "UTC"})
        patch.start()
        self.addCleanup(time.tzset)
        self.addCleanup(patch.stop)
        time.tzset()
        self.list(self.entries())
        prepared = self.lane / "logs" / "prepare-3-20260906T115500.log"
        prepared.write_text("prepare output")
        os.utime(prepared, (CLOCK - 300, CLOCK - 300))
        for name, text, age in (("gate-queue-notes-1.log", "", 120), ("gate-sd1775-2.log", json.dumps(
                {"status": "success", "summary": "sd-check pass (check pass)", "head": "d" * 40}), 600),
                ("gate-stale.log", "", 7 * 3600)):
            (self.lane / name).write_text(text)
            os.utime(self.lane / name, (CLOCK - age, CLOCK - age))

    def list(self, entries):
        self.fixture.write_text(json.dumps({"ok": True, "queue": str(self.lane / "queue" / "queue.json"), "entries": entries}))

    def entries(self):
        """One entry per state, plus history the page must leave out."""
        return [
            {"item": 9, "title": "Old failure, since retried", "status": "failed", "step": "prepare", "reason": "gate failed",
             "finished_at": "2026-09-06T08:00:00Z"},
            {"item": 7, "title": "Land the hub docs", "status": "merged", "merge_commit": self.merged,
             "finished_at": "2026-09-06T10:00:00Z"},
            {"item": 6, "title": "Landed yesterday", "status": "merged", "merge_commit": self.merged,
             "finished_at": "2026-09-05T10:00:00Z"},
            {"item": 5, "title": "Prepared only", "status": "prepared", "reason": "queued without --manual; merge by hand",
             "expected_head": "5" * 40, "finished_at": "2026-09-06T09:00:00Z"},
            {"item": 4, "title": "Moved head", "status": "skipped", "reason": "the worktree's HEAD moved from the queued head",
             "expected_head": "4" * 40, "finished_at": "2026-09-06T09:30:00Z"},
            {"item": 3, "title": "Merge the slice", "status": "running", "expected_head": "a" * 40, "id": "e-3",
             "started_at": "2026-09-06T11:50:00Z", "step": "merge", "lease_until": "2026-09-06T13:20:00Z",
             "holder": {"host": "hub-1", "pid": 4242, "token": "claim-token-3"}, "enqueued_on": "hub-1",
             "log_host": "hub-1"},
            {"item": 9, "title": "Old failure, since retried", "status": "pending", "expected_head": "b" * 40,
             "speculation": {"status": "success", "summary": "sd-check pass"}},
            {"item": 12, "title": "Queue page", "status": "pending", "expected_head": "c" * 40, "held": True},
            {"item": 1, "title": "Cancelled", "status": "cancelled", "expected_head": "1" * 40, "branch": "b1",
             "finished_at": "2026-09-06T07:00:00Z"},
        ]

    def calls(self):
        return [line.split() for line in self.log.read_text().splitlines()]

    def shot_entries(self):
        """sd:6728: item 5 carries two PNGs the lane drew and one outside its logs; item 4 the warning of none."""
        drawn = self.lane / "logs" / "shots-5-20260906T090000"
        drawn.mkdir()
        (drawn / "home-dark-1440.png").write_bytes(b"\x89PNG\r\n\x1a\nshot")
        files = [str(drawn / "home-dark-1440.png"), str(drawn / "home-light-375.png"), "/elsewhere/x-dark-1440.png"]
        self.list([{**e, "shots": files} if e["item"] == 5 else {**e, "shots_warning": "shots: no Chrome found"}
                   if e["item"] == 4 else e for e in self.entries()])


class TheRows(Lane, ScreenCase):
    """`queue_screen.document` from the fixture `lane list`, the gate log and `sd gate status`."""

    def doc(self):
        return queue_screen.document(self.connection, now=NOW)

    def test_each_lane_carries_its_checkouts_realm(self):
        """pack sd:6016: the realm tab a lane shows under is places.realm_of over its checkout; unread rules give None."""
        conf = Path(self.tmp.name) / "realms.conf"
        for text, realm in ((f"realm|work|Work|{self.checkout.parent}/*\n", "work"), ("realm|work|Work|/elsewhere/*\n", "personal"),
                            (None, None)):
            conf.unlink(missing_ok=True)
            if text:
                conf.write_text(text)
            with self.subTest(realm=realm), mock.patch.object(places, "REALMS_CONFIG", conf):
                self.assertEqual([lane["realm"] for lane in queue_screen.document(self.connection, now=NOW)["lanes"]], [realm])

    def test_the_hoa_checkouts_lane_is_realm_hoa_before_a_glob_that_claims_it(self):
        """pack sd:7687: the hoa checkout's lane is HOA's, as its items are on Tasks, with or without a realm list."""
        conf = Path(self.tmp.name) / "realms.conf"
        hoa = mock.patch.object(hoa_screen, "checkout", return_value=(str(self.checkout), self.checkout, []))
        for text in (f"realm|work|Work|{self.checkout.parent}/*\n", None):
            conf.unlink(missing_ok=True)
            if text:
                conf.write_text(text)
            with self.subTest(text=text), hoa, mock.patch.object(places, "REALMS_CONFIG", conf):
                self.assertEqual([lane["realm"] for lane in queue_screen.document(self.connection, now=NOW)["lanes"]], ["hoa"])

    def test_a_row_links_each_screenshot_the_lane_drew_and_says_why_it_drew_none(self):
        self.shot_entries()
        rows = {row.get("item"): row for row in self.doc()["lanes"][0]["rows"]}
        self.assertEqual(rows[5]["shots"], [
            {"name": "home-dark-1440", "url": "/lane-shots/system/lane/logs/shots-5-20260906T090000/home-dark-1440.png"},
            {"name": "home-light-375", "url": "/lane-shots/system/lane/logs/shots-5-20260906T090000/home-light-375.png"}])
        self.assertEqual((rows[4]["shots_warning"], "shots" in rows[4], "shots" in rows[7]),
                         ("shots: no Chrome found", False, False))

    def test_each_of_the_six_states_has_its_row(self):
        rows = self.doc()["lanes"][0]["rows"]
        self.assertEqual([(row["state"], row.get("item") or row.get("builder")) for row in rows], [
            ("merging", 3), ("next", 9), ("next", 12), ("building", "queue-notes-1"), ("building", "sd1775-2"),
            ("blocked", 5), ("blocked", 4), ("cancelled", 1), ("landed", 7)])

    def worktrees(self):
        """The fixture with the worktree `lane enqueue` records on the next and blocked rows (sd:5064)."""
        self.list([{**e, "worktree": f"/work/sd-{e['item']}"} if e["status"] in ("pending", "prepared") else e
                   for e in self.entries()])

    def test_a_lane_row_carries_the_worktree_its_entry_recorded(self):
        self.worktrees()
        rows = self.doc()["lanes"][0]["rows"]
        self.assertEqual([(row["state"], row.get("worktree")) for row in rows], [
            ("merging", None), ("next", "/work/sd-9"), ("next", "/work/sd-12"), ("building", None), ("building", None),
            ("blocked", "/work/sd-5"), ("blocked", None), ("cancelled", None), ("landed", None)])

    def test_the_lane_document_names_no_attention(self):
        # sd:5921, operator answer 2: a blocked entry is a broken row of Today's Needs you, not a count of its own.
        self.assertNotIn("attention", self.doc())

    def test_merging_names_its_step_host_claim_age_and_lease(self):
        merging = self.doc()["lanes"][0]["rows"][0]
        self.assertEqual((merging["step"], merging["host"], merging["claim_age"], merging["lease_until"], merging["head"]),
                         ("merge", "hub-1", 600, "2026-09-06T13:20:00Z", "a" * 12))

    def test_the_step_comes_from_the_entry_not_a_prepare_log(self):
        # sd:3284: the entry's `step` replaces the probe for a prepare log newer than the claim. The fixture's
        # prepare log for sd:3 is newer than its claim, which the probe read as `merge`.
        entries = self.entries()
        entries[5] = {**entries[5], "step": "prepare"}
        self.list(entries)
        self.assertEqual(self.doc()["lanes"][0]["rows"][0]["step"], "prepare")

    def test_a_holder_on_another_host_is_named(self):
        # Pack sd:3283 runs an entry on any host; the hub's page shows the satellite that holds it.
        entries = self.entries()
        entries[5] = {**entries[5], "holder": {"host": "build-2", "pid": 77, "token": "t"}, "log_host": "build-2",
                      "step": "prepare"}
        self.list(entries)
        merging = self.doc()["lanes"][0]["rows"][0]
        self.assertEqual((merging["host"], merging["step"]), ("build-2", "prepare"))

    def test_a_running_entry_without_holder_or_step_reads_unknown_not_guessed(self):
        # An entry an earlier pack wrote: no holder, no step, no lease. The page shows each as not recorded.
        entries = self.entries()
        entries[5] = {key: value for key, value in entries[5].items() if key not in ("holder", "step", "lease_until", "log_host")}
        self.list(entries)
        merging = self.doc()["lanes"][0]["rows"][0]
        self.assertEqual((merging["host"], merging["step"], merging["lease_until"], merging["claim_age"]), (None, None, None, 600))

    def test_the_claim_token_reaches_the_page_only_as_the_running_rows_claim(self):
        # sd:3337: Release sends back the claim the row showed, `<holder token>:<step>`, for `--expected-claim`.
        doc = self.doc()
        self.assertEqual(doc["lanes"][0]["rows"][0]["claim"], "claim-token-3:merge")
        self.assertEqual(json.dumps(doc).count("claim-token-3"), 1)

    def test_a_running_entry_without_a_token_or_step_has_no_claim(self):
        # An entry an earlier pack wrote names no claim, so there is nothing a Release could bind to.
        for drop in ("holder", "step"):
            entries = self.entries()
            entries[5] = {key: value for key, value in entries[5].items() if key != drop}
            self.list(entries)
            self.assertIsNone(self.doc()["lanes"][0]["rows"][0]["claim"], drop)

    def test_every_managed_repository_has_a_lane_even_without_a_lane_folder(self):
        # sd:3284: the queue lives in the hub database, so a lane hosted elsewhere has no folder on this machine.
        other = Path(self.tmp.name) / "satellite-only"
        other.mkdir()
        upsert_repo(self.connection, str(other), remote="git@example.invalid:y.git", managed=1)
        self.connection.execute("UPDATE repo SET lane_host = 'build-2' WHERE path = ?", (str(other),))
        unmanaged = Path(self.tmp.name) / "not-managed"
        unmanaged.mkdir()
        upsert_repo(self.connection, str(unmanaged), remote="git@example.invalid:z.git", managed=0)
        self.connection.commit()
        Path(f"{self.fixture}.satellite-only").write_text(json.dumps({"ok": True, "queue": str(
            Path(self.tmp.name) / "lanes" / "satellite-only" / "lane" / "queue" / "queue.json"), "entries": [
            {"item": 21, "title": "Run elsewhere", "status": "pending", "expected_head": "2" * 40}]}))
        doc = self.doc()
        self.assertEqual([(lane["repo"], lane["host"]) for lane in doc["lanes"]], [("satellite-only", "build-2"), ("system", "hub")])
        self.assertEqual([row["item"] for row in doc["lanes"][0]["rows"]], [21])
        self.assertEqual(doc["managed"], 2)
        self.assertNotIn(["-C", str(unmanaged), "lane", "list"], self.calls())

    def test_a_managed_repository_with_no_checkout_here_is_named_not_dropped(self):
        gone = Path(self.tmp.name) / "gone"
        upsert_repo(self.connection, str(gone), remote="git@example.invalid:g.git", managed=1)
        self.connection.commit()
        doc = self.doc()
        self.assertEqual(([lane["repo"] for lane in doc["lanes"]], [p["repo"] for p in doc["problems"]]),
                         (["system"], [str(gone)]))

    def test_next_rows_carry_order_gate_head_and_hold(self):
        first, second = self.doc()["lanes"][0]["rows"][1:3]
        self.assertEqual((first["position"], first["gate"], first["gate_summary"], first["head"], first["held"]),
                         (1, "success", "sd-check pass", "b" * 12, False))
        self.assertEqual((second["position"], second["gate"], second["held"]), (2, "not gated", True))

    def pr_item(self, n, *lanes):
        """An item whose pull request #n has these `(at, lane)` lane notes (pack sd:7859)."""
        from sd_db import contributions
        from sd_db.writes import add_note

        url = f"https://github.com/example/system/pull/{n}"
        # Past every item the fixture lane names (1 to 12), so a fixture lane row never holds a pull request's item.
        while (self.connection.execute("SELECT max(id) FROM item").fetchone()[0] or 0) < 12:
            create_item(self.connection, kind="task", title="Filler", status="planning", priority=3)
        item = contributions.capture(self.connection, title=f"PR {n}", changes={"pull_url": url}, who="test")["item"]["id"]
        for at, lane in lanes:
            add_note(self.connection, item, "comment", f"PR lane: {lane} · open · {url}", session="contribution-observer",
                     timestamp=at)
        return item, url

    def test_an_item_whose_pull_request_waits_on_review_is_a_pr_row_split_by_who_it_waits_on(self):
        mine, url = self.pr_item(901, ("2026-09-06T09:00:00Z", "awaiting_them"), ("2026-09-06T11:00:00Z", "awaiting_you"))
        theirs, _ = self.pr_item(902, ("2026-09-06T10:00:00Z", "awaiting_them"))
        self.pr_item(903, ("2026-09-06T10:00:00Z", "awaiting_them"), ("2026-09-06T11:00:00Z", "merged"))
        prs = self.doc()["prs"]
        self.assertEqual(prs["error"], "")
        self.assertEqual([(r["item"], r["on"], r["since"], r["waited"]) for r in prs["rows"]],
                         [(mine, "you", "2026-09-06T11:00:00+00:00", 3600), (theirs, "reviewers", "2026-09-06T10:00:00+00:00", 7200)])
        self.assertEqual(prs["rows"][0]["url"], url)

    def test_an_unlinked_pull_request_says_what_the_wait_rests_on(self):
        """sd:8116: the wait runs from GitHub's newest event, marked approximate; a linked one is one row."""
        from sd_db.writes import record_state

        linked, _ = self.pr_item(910, ("2026-09-06T10:00:00Z", "awaiting_them"))
        url = "https://github.com/example/system/pull/911"
        record_state(self.connection, "checkpoint", key="contribution:github:" + url, body={"observation": {
            "operator": {"id": "1", "login": "operator"}, "author": {"id": "1", "login": "someone"},
            "requested_reviewers": [], "reviews": [], "state": "open", "draft": False, "title": "Unlinked 911",
            "observed_at": "2026-09-06T11:30:00Z", "repo": "example/system",
            "events": [{"id": "c", "actor_id": "2", "kind": "comment", "at": "2026-09-06T11:00:00Z"}]}})
        self.connection.commit()
        rows = self.doc()["prs"]["rows"]
        self.assertEqual([(r["item"], r["url"][-3:]) for r in rows], [(linked, "910"), (None, "911")])
        self.assertEqual((rows[1]["on"], rows[1]["waited"], rows[1]["approximate"], rows[1]["stale"]),
                         ("reviewers", 3600, True, False))

    def test_an_item_in_the_lane_keeps_its_lane_row_not_a_pr_row(self):
        item, _ = self.pr_item(904, ("2026-09-06T10:00:00Z", "awaiting_them"))
        self.list([*self.entries(), {"item": item, "title": "In the lane", "status": "pending", "expected_head": "e" * 40}])
        self.assertEqual(self.doc()["prs"]["rows"], [])

    def test_a_manual_merge_prepared_entry_waits_on_review_and_a_self_merging_entry_keeps_its_lane(self):
        # pack sd:7859: a manual-merge lane stops `prepared` and a person reviews the PR; an auto-merge lane merges it.
        manual, _ = self.pr_item(905, ("2026-09-06T10:00:00Z", "awaiting_them"))
        auto, _ = self.pr_item(906, ("2026-09-06T10:00:00Z", "awaiting_them"))
        self.list([{"item": manual, "title": "Manual", "status": "prepared", "reason": "queued without --manual; merge by hand",
                    "expected_head": "e" * 40, "finished_at": "2026-09-06T10:00:00Z"},
                   {"item": auto, "title": "Auto", "status": "running", "expected_head": "f" * 40, "id": "e-6",
                    "started_at": "2026-09-06T11:50:00Z", "step": "merge"}])
        self.assertEqual([(r["item"], r["on"]) for r in self.doc()["prs"]["rows"]], [(manual, "reviewers")])

    def test_a_landed_item_keeps_its_lane_row_and_a_cancelled_one_keeps_its_pr_wait(self):
        # sd:7863 (note 46123): LANED missed landed, so a just-merged item read "blocked: PR" until the next sync
        # wrote its merged lane note. A cancelled entry left the pull request open: its wait still shows (review).
        landed, _ = self.pr_item(907, ("2026-09-06T10:00:00Z", "awaiting_them"))
        cancelled, _ = self.pr_item(908, ("2026-09-06T10:00:00Z", "awaiting_them"))
        self.list([{"item": landed, "title": "Landed", "status": "merged", "finished_at": "2026-09-06T11:00:00Z"},
                   {"item": cancelled, "title": "Cancelled", "status": "cancelled", "expected_head": "e" * 40,
                    "finished_at": "2026-09-06T11:00:00Z"}])
        doc = self.doc()
        states = {row.get("item"): row["state"] for row in doc["lanes"][0]["rows"]}
        self.assertEqual((states[landed], states[cancelled]), ("landed", "cancelled"))
        self.assertEqual([row["item"] for row in doc["prs"]["rows"]], [cancelled],
                         "a cancelled entry hid the open pull request's wait")

    def test_a_malformed_item_is_said_and_the_other_pr_rows_still_show(self):
        # sd:7863 (note 46123): pr_waits read every item strictly, so one malformed item emptied the PR rows.
        from sd_db.workflow import capture_task
        from sd_db.writes import set_item_fields

        theirs, _ = self.pr_item(909, ("2026-09-06T10:00:00Z", "awaiting_them"))
        broken = capture_task(self.connection, title="Broken", priority=3, who="test")["item"]["id"]
        set_item_fields(self.connection, broken, fields={"contribution": "not an object"})
        self.connection.commit()
        prs = self.doc()["prs"]
        self.assertEqual([row["item"] for row in prs["rows"]], [theirs])
        self.assertEqual(prs["error"], f"pull-request waits skipped item {broken}: item {broken} has malformed "
                                       "contribution metadata")

    def test_a_pr_wait_read_that_failed_says_so_and_shows_no_rows(self):
        from sd_db import contributions

        with mock.patch.object(contributions, "pr_waits", side_effect=sqlite3.OperationalError("database is locked")):
            prs = self.doc()["prs"]
        self.assertEqual(prs, {"rows": [], "error": "pull-request waits were not read: OperationalError: database is locked"})

    def test_building_reads_the_builder_gate_logs(self):
        running, done = self.doc()["lanes"][0]["rows"][3:5]
        self.assertEqual((running["gate"], running["summary"]), ("running", None))
        self.assertEqual((done["gate"], done["summary"], done["head"]), ("pass", "sd-check pass (check pass)", "d" * 12))

    def test_blocked_names_who_acts_and_why(self):
        prepared, skipped = self.doc()["lanes"][0]["rows"][5:7]
        self.assertEqual((prepared["who"], prepared["reason"]), ("operator", "queued without --manual; merge by hand"))
        self.assertEqual((skipped["who"], skipped["status"]), ("builder", "skipped"))
        # sd:3012 review: Retry and Approve name the entry they act on by its full head.
        self.assertEqual((prepared["head"], prepared["expected_head"]), ("5" * 12, "5" * 40))

    def test_a_new_blocked_head_changes_the_revision(self):
        # sd:3012 review: a later run that ends blocked at another head leaves the pending order alone, so the
        # revision must read each blocked entry's head too, or Approve would grant a head the page never showed.
        before = self.doc()["lanes"][0]["revision"]
        self.list(self.entries() + [{"item": 5, "title": "Prepared only", "status": "prepared", "reason": "again",
                                     "expected_head": "f" * 40, "finished_at": "2026-09-06T11:00:00Z"}])
        after = self.doc()["lanes"][0]["revision"]
        self.assertNotEqual(before, after)
        self.assertEqual(queue_screen.lane_revision(self.entries()),
                         queue_screen.lane_revision(json.loads(self.fixture.read_text())["entries"]))

    def test_a_done_or_cancelled_items_failed_newest_entry_is_not_blocked(self):
        # sd:5847: the item closed after its newest entry failed; the lane still lists that failure.
        done = self.item("Closed", kind="task", status="done")
        cancelled = self.item("Cancelled", status="done", fields={"completion": {"outcome": "cancelled"}})
        still = self.item("Still open", kind="task")
        failed = {"title": "t", "status": "failed", "step": "merge", "reason": "gate failed",
                  "expected_head": "e" * 40, "finished_at": "2026-09-06T11:00:00Z"}
        self.list([{**failed, "item": item} for item in (done, cancelled, still)])
        doc = self.doc()
        self.assertEqual([row["item"] for row in doc["lanes"][0]["rows"] if row["state"] == "blocked"], [still])
        # Queue has no badge since sd:5921; Today's Lane lamp and Needs you count the blocked entries.
        self.assertEqual(now_screen.lane_summary(doc)["blocked"], 1)
        self.assertEqual([row["item"] for row in now_screen.lane_rows(doc["lanes"])], [still])

    def test_todays_lane_summary_names_each_checkout_whose_lane_was_not_read(self):
        # Review of pack sd:7315: Today's Retry claims a fresh read only for a lane this list leaves out.
        doc = {"lanes": [], "problems": [{"repo": "/repos/pack", "error": "sd-ship gave no answer"}, "malformed"]}
        self.assertEqual(now_screen.lane_summary(doc)["unread_paths"], ["/repos/pack"])

    # sd:3398: a cancelled entry is a row, so Re-queue can name the head the operator cancelled.
    def test_a_cancelled_entry_carries_its_head_and_when_it_was_cancelled(self):
        cancelled = next(row for row in self.doc()["lanes"][0]["rows"] if row["state"] == "cancelled")
        self.assertEqual((cancelled["item"], cancelled["head"], cancelled["expected_head"], cancelled["finished"]),
                         (1, "1" * 12, "1" * 40, "2026-09-06T07:00:00Z"))

    def test_a_cancelled_entry_that_is_not_its_items_newest_or_is_old_has_no_row(self):
        later = {"item": 1, "title": "Cancelled", "status": "pending", "expected_head": "1" * 40}
        old = {"item": 16, "title": "Cancelled last week", "status": "cancelled", "expected_head": "6" * 40,
               "finished_at": "2026-08-30T07:00:00Z"}
        self.list(self.entries() + [later, old])
        rows = self.doc()["lanes"][0]["rows"]
        self.assertEqual([row["item"] for row in rows if row["state"] == "cancelled"], [])

    def test_a_cancelled_head_is_part_of_the_page_revision(self):
        # Re-queue carries the head the row showed; a later entry cancelled at another head must change the revision.
        before = self.doc()["lanes"][0]["revision"]
        self.list(self.entries() + [{"item": 1, "title": "Cancelled", "status": "cancelled", "expected_head": "f" * 40,
                                     "finished_at": "2026-09-06T08:00:00Z"}])
        self.assertNotEqual(self.doc()["lanes"][0]["revision"], before)

    # sd:3419: `lane move` can list an item's newer entry above its older ones, so its place is not its age.
    def test_after_a_move_an_items_newest_entry_is_its_highest_enqueue_seq_not_its_last_listed(self):
        newer = [{"item": 5, "title": "Prepared only", "status": "prepared", "reason": "again", "expected_head": "f" * 40,
                  "finished_at": "2026-09-06T11:00:00Z", "enqueue_seq": 98},
                 {"item": 1, "title": "Cancelled", "status": "pending", "expected_head": "1" * 40, "enqueue_seq": 99}]
        self.list(newer + numbered(self.entries()))
        rows = self.doc()["lanes"][0]["rows"]
        self.assertEqual([(row["item"], row["expected_head"]) for row in rows if row["state"] == "blocked"],
                         [(5, "f" * 40), (4, "4" * 40)])
        self.assertEqual([row["item"] for row in rows if row["state"] == "cancelled"], [])

    def test_after_a_move_the_page_revision_reads_the_head_of_the_highest_enqueue_seq(self):
        newer = {"item": 5, "title": "Prepared only", "status": "prepared", "reason": "again", "expected_head": "f" * 40,
                 "finished_at": "2026-09-06T11:00:00Z", "enqueue_seq": 99}
        self.list([newer] + numbered(self.entries()))
        before = self.doc()["lanes"][0]["revision"]
        self.list([{**newer, "expected_head": "e" * 40}] + numbered(self.entries()))
        self.assertNotEqual(self.doc()["lanes"][0]["revision"], before)

    def test_without_enqueue_seq_on_every_entry_the_last_listed_entry_is_the_newest(self):
        # The fallback for a pack that writes no `enqueue_seq`: it lists an item's entries oldest first.
        older, newer = {"item": 1, "status": "failed"}, {"item": 1, "status": "cancelled"}
        self.assertIs(queue_screen.newest([older, newer])[1], newer)
        self.assertIs(queue_screen.newest([{**older, "enqueue_seq": 7}, newer])[1], newer)
        moved = [{**newer, "enqueue_seq": 7}, {**older, "enqueue_seq": 2}]
        self.assertIs(queue_screen.newest(moved)[1], moved[0])

    def test_a_pack_whose_retry_takes_a_cancelled_entry_offers_requeue(self):
        doc = self.doc()
        self.assertEqual((doc["requeue"], doc["requeue_off"]), (True, None))

    def test_a_pack_whose_retry_takes_no_cancelled_entry_leaves_requeue_off_with_its_reason(self):
        # Before pack sd:3389, `lane retry --help` does not name a cancelled entry.
        with mock.patch.dict(os.environ, {"QUEUE_OLD_REQUEUE": "1"}):
            doc = self.doc()
        self.assertEqual((doc["retry"], doc["requeue"]), (True, False))
        self.assertIn("sd:3389", doc["requeue_off"])

    def test_a_failed_help_probe_leaves_requeue_off_as_unknown_not_as_absent(self):
        with mock.patch.dict(os.environ, {"QUEUE_NO_RETRY": "1"}):
            doc = self.doc()
        self.assertIs(doc["requeue"], False)
        self.assertIn("exited 2", doc["requeue_off"])
        self.assertIn("invalid choice", doc["requeue_off"])

    def test_landed_today_carries_the_pull_request_and_merge_commit(self):
        landed = self.doc()["lanes"][0]["rows"][-1]
        self.assertEqual((landed["pr"], landed["commit"]), ("161", self.merged[:12]))

    def test_a_merge_commits_pull_request_is_read_from_git_once(self):
        # A commit's subject never changes, so the next read asks git nothing for it.
        run = subprocess.run
        with mock.patch.object(queue_screen.subprocess, "run", side_effect=run) as runs:
            first, second = self.doc(), self.doc()
        logs = [call for call in runs.call_args_list if call.args[0][:1] == ["git"] and "log" in call.args[0]]
        self.assertEqual(len(logs), 1)
        self.assertEqual(first["lanes"][0]["rows"][-1]["pr"], second["lanes"][0]["rows"][-1]["pr"])

    def test_a_subject_git_could_not_read_is_asked_again(self):
        missing = str(Path(self.tmp.name) / "no-checkout")
        run = subprocess.run
        with mock.patch.object(queue_screen.subprocess, "run", side_effect=run) as runs:
            self.assertEqual((queue_screen._pr(missing, self.merged), queue_screen._pr(missing, self.merged)), (None, None))
        self.assertEqual(len(runs.call_args_list), 2)

    def test_the_header_reads_load_and_gate_slots(self):
        doc = self.doc()
        self.assertEqual(doc["gates"], {"running": 1, "cap": 4, "waiting": 0})
        self.assertEqual(set(doc["load"]), {"load1", "load5", "load15", "trend"})
        self.assertIn("next item boundary", doc["edits"])

    def test_a_failed_entry_shows_its_status_step_and_the_first_line_of_its_reason(self):
        # sd:3012: older failed history drops off after `BLOCKED_DAYS`; a reason shows its first line only.
        self.list(self.entries() + [
            {"item": 14, "title": "Failed this morning", "status": "failed", "step": "merge",
             "reason": "merge refused: required check pending\n  sd/local-gate: queued", "finished_at": "2026-09-06T06:00:00Z"},
            {"item": 15, "title": "Failed four days ago", "status": "failed", "step": "prepare", "reason": "gate failed",
             "finished_at": "2026-09-02T11:00:00Z"}])
        blocked = [row for row in self.doc()["lanes"][0]["rows"] if row["state"] == "blocked"]
        self.assertEqual([row["item"] for row in blocked], [5, 4, 14])
        failed = blocked[2]
        self.assertEqual((failed["status"], failed["step"], failed["reason"]),
                         ("failed", "merge", "merge refused: required check pending"))

    def test_a_pack_with_lane_retry_offers_it_and_names_the_repos_merge_setting(self):
        doc = self.doc()
        self.assertEqual((doc["retry"], doc["lanes"][0]["runner_merge"]), (True, "manual"))
        self.assertIn(["lane", "retry", "--help"], self.calls())

    def test_a_pack_without_lane_retry_hides_it(self):
        # Before the pack carries sd:3254 its argparse refuses `retry`; the page offers neither button.
        with mock.patch.dict(os.environ, {"QUEUE_NO_RETRY": "1"}):
            self.assertIs(self.doc()["retry"], False)

    def test_a_pack_whose_retry_takes_no_expected_head_hides_it(self):
        # Before pack sd:3268, retry cannot check the head the page showed; the page offers neither button.
        with mock.patch.dict(os.environ, {"QUEUE_OLD_RETRY": "1"}):
            self.assertIs(self.doc()["retry"], False)

    def test_a_lane_that_cannot_be_listed_is_named_not_dropped(self):
        self.fixture.write_text("not json")
        doc = self.doc()
        self.assertEqual((doc["lanes"], [p["repo"] for p in doc["problems"]]), ([], [str(self.checkout)]))


class TheMove(Lane, BrowserSession):
    """`/api/queue/move` runs one lane verb, and only under the page's session, CSRF and Origin."""

    def revision(self):
        return queue_screen.revision(json.loads(self.fixture.read_text())["entries"])

    def lane_revision(self):
        return queue_screen.lane_revision(json.loads(self.fixture.read_text())["entries"])

    def move(self, action="up", item=12, head=None, **headers):
        payload = {"repo": str(self.checkout), "item": item, "action": action, "revision": self.revision()}
        if head is not None:
            payload["head"] = head
        return self.post("/api/queue/move", payload, **headers)

    def test_a_reorder_calls_exactly_lane_move_with_the_item_place_and_revision(self):
        status, _, answer = self.move("up")
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "move", "12", "up", "--expected-revision", self.lane_revision()]])

    def test_top_hold_and_release_call_their_verbs_with_the_revision(self):
        for action, verb in (("top", ["move", "12", "top"]), ("hold", ["hold", "12"]), ("release", ["release", "12"])):
            self.log.write_text("")
            status, _, _ = self.move(action)
            self.assertEqual((status, self.calls()[-1]),
                             (200, ["-C", str(self.checkout), "lane", *verb, "--expected-revision", self.lane_revision()]))

    def test_a_verb_that_refuses_a_stale_revision_asks_the_page_to_read_again(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "the queue changed since revision x", "QUEUE_CODE": "stale_revision"}):
            status, _, answer = self.move("hold")
        self.assertEqual((status, answer.get("reload")), (409, True), answer)

    def test_cancel_calls_exactly_lane_cancel_with_the_item_and_revision(self):
        # sd:3284: `lane cancel` also releases a running entry (pack sd:3282), so a Cancel the page showed on a
        # pending row must not land after the runner claimed it: the verb checks the revision under its lock.
        status, _, answer = self.move("cancel", item=9)
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "cancel"))
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "cancel", "9", "--expected-revision",
                                         self.lane_revision()]])

    def test_a_refused_cancel_is_the_answer(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:9 has no pending entry in this lane"}):
            status, _, answer = self.move("cancel", item=9)
        self.assertEqual((status, answer["error"]), (400, "sd:9 has no pending entry in this lane"))

    def test_retry_calls_exactly_lane_retry_with_the_item_and_its_head(self):
        status, _, answer = self.move("retry", item=4, head="4" * 40)
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "retry"))
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "retry", "4", "--expected-head", "4" * 40]])

    def test_approve_calls_lane_retry_with_its_head_and_manual(self):
        status, _, answer = self.move("approve", item=5, head="5" * 40)
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "approve"))
        self.assertEqual(self.calls()[-1],
                         ["-C", str(self.checkout), "lane", "retry", "5", "--expected-head", "5" * 40, "--manual"])

    def test_retry_or_approve_without_a_full_head_runs_nothing(self):
        for action, head in (("retry", None), ("approve", None), ("approve", "5" * 12), ("retry", "HEAD")):
            status, _, _ = self.move(action, item=5, head=head)
            self.assertEqual((action, head, status, self.calls()), (action, head, 400, []))
        status, _, _ = self.move("up", head="c" * 40)
        self.assertEqual((status, self.calls()), (400, []))

    def test_approve_after_a_later_run_blocked_at_another_head_is_refused_before_any_verb(self):
        # sd:3012 review: the page showed sd:5 at one head; a later run ended blocked at another, pending unchanged.
        revision = self.revision()
        self.list(self.entries() + [{"item": 5, "title": "Prepared only", "status": "prepared", "reason": "again",
                                     "expected_head": "f" * 40, "finished_at": "2026-09-06T11:00:00Z"}])
        status, _, answer = self.post("/api/queue/move", {"repo": str(self.checkout), "item": 5, "action": "approve",
                                                          "revision": revision, "head": "5" * 40})
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"]])

    def test_a_retry_the_lane_refuses_for_another_head_asks_the_page_to_read_again(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:5's last entry is at another head", "QUEUE_CODE": "stale_head"}):
            status, _, answer = self.move("approve", item=5, head="5" * 40)
        self.assertEqual((status, answer.get("reload")), (409, True), answer)

    def test_approve_on_an_auto_repo_runs_nothing(self):
        # runner_merge=auto needs no grant; the page offers no Approve there, and a stale page is refused too.
        upsert_repo(self.connection, str(self.checkout), runner_merge="auto")
        self.connection.commit()
        status, _, _ = self.move("approve", item=5, head="5" * 40)
        self.assertEqual((status, self.calls()), (400, []))

    def test_a_refused_retry_is_the_answer(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:4's last entry kept no body copy"}):
            status, _, answer = self.move("retry", item=4, head="4" * 40)
        self.assertEqual((status, answer["error"]), (400, "sd:4's last entry kept no body copy"))

    # sd:3339: only a refusal `sd-ship lane` reports wrote nothing; any other failure may come after the write landed.
    def test_a_hub_fault_an_unknown_publish_or_an_unknown_code_is_an_unknown_outcome(self):
        for code in ("hub_unavailable", "publish_unknown", "a_code_this_page_does_not_know"):
            with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "The hub database did not answer.", "QUEUE_CODE": code}):
                status, _, answer = self.move("cancel", item=9)
            self.assertEqual((code, status, answer.get("outcome")), (code, 502, "unknown"), answer)
            self.assertEqual(answer["error"], "The hub database did not answer.")

    def test_a_verb_that_runs_past_its_bound_is_an_unknown_outcome(self):
        with mock.patch.object(queue_screen, "WRITE_SECONDS", 1), mock.patch.dict(os.environ, {"QUEUE_SLEEP": "10"}):
            status, _, answer = self.move("hold")
        self.assertEqual((status, answer.get("outcome")), (502, "unknown"), answer)
        self.assertNotIn("did not run", answer["error"])

    def test_an_exit_without_a_refusal_is_an_unknown_outcome(self):
        for env in ({"QUEUE_CRASH": "KeyError: 'item'"}, {"QUEUE_REFUSE": "sd:12 is running", "QUEUE_EXIT": "1"}):
            with mock.patch.dict(os.environ, env):
                status, _, answer = self.move("up")
            self.assertEqual((env, status, answer.get("outcome")), (env, 502, "unknown"), answer)

    def test_a_refusal_that_wrote_nothing_stays_a_refusal(self):
        for code in ("", "holder_alive", "head_gone", "branch_diverged", "default_branch"):
            with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "refused before the write", "QUEUE_CODE": code}):
                status, _, answer = self.move("retry", item=4, head="4" * 40)
            self.assertEqual((code, status, answer), (code, 400, {"error": "refused before the write"}))

    # sd:3398: Re-queue runs `lane retry` on a cancelled entry with the head the row showed (pack sd:3389).
    def test_requeue_calls_exactly_lane_retry_with_the_item_and_its_head(self):
        status, _, answer = self.move("requeue", item=1, head="1" * 40)
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "requeue"), answer)
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "retry", "1", "--expected-head", "1" * 40]])

    def test_requeue_without_a_full_head_runs_nothing(self):
        for head in (None, "1" * 12, "HEAD"):
            status, _, _ = self.move("requeue", item=1, head=head)
            self.assertEqual((head, status, self.calls()), (head, 400, []))

    def test_requeue_of_an_entry_that_is_not_cancelled_runs_no_verb(self):
        # The operator's ruling: the page re-queues a cancelled entry and enqueues nothing else.
        for item, head in ((4, "4" * 40), (12, "c" * 40), (77, "7" * 40)):
            self.log.write_text("")
            status, _, answer = self.move("requeue", item=item, head=head)
            self.assertEqual((item, status, self.calls()), (item, 400, [["-C", str(self.checkout), "lane", "list"]]))
            self.assertIn("cancelled", answer["error"])

    def test_requeue_after_the_item_was_cancelled_again_at_another_head_is_refused_before_any_verb(self):
        revision = self.revision()
        self.list(self.entries() + [{"item": 1, "title": "Cancelled", "status": "cancelled", "expected_head": "f" * 40,
                                     "finished_at": "2026-09-06T08:00:00Z"}])
        status, _, answer = self.post("/api/queue/move", {"repo": str(self.checkout), "item": 1, "action": "requeue",
                                                          "revision": revision, "head": "1" * 40})
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"]])

    def test_requeue_after_a_move_reads_the_highest_enqueue_seq_as_the_newest_entry(self):
        # sd:3419: a newer cancel listed above an older failure is the newest entry, so Re-queue takes it.
        cancelled = {"item": 9, "title": "Old failure, since retried", "status": "cancelled", "expected_head": "f" * 40,
                     "finished_at": "2026-09-06T11:00:00Z", "enqueue_seq": 99}
        self.list([cancelled] + numbered([row for row in self.entries() if row["status"] != "pending" or row["item"] != 9]))
        status, _, answer = self.move("requeue", item=9, head="f" * 40)
        self.assertEqual((status, answer.get("ok")), (200, True), answer)
        self.assertEqual(self.calls()[-1], ["-C", str(self.checkout), "lane", "retry", "9", "--expected-head", "f" * 40])

    def test_requeue_after_a_move_put_a_newer_entry_above_the_cancel_runs_no_verb(self):
        self.list([{"item": 1, "title": "Cancelled", "status": "pending", "expected_head": "1" * 40, "enqueue_seq": 99}]
                  + numbered(self.entries()))
        status, _, answer = self.move("requeue", item=1, head="1" * 40)
        self.assertEqual((status, self.calls()), (400, [["-C", str(self.checkout), "lane", "list"]]), answer)
        self.assertIn("newest entry in this lane is pending", answer["error"])

    def test_a_requeue_the_lane_refuses_answers_with_the_packs_reason(self):
        # Each refusal of pack sd:3389 writes nothing: a stale head or a newer entry asks the page to read again.
        for code, want in (("stale_head", 409), ("superseded", 409), ("head_moved", 400), ("already_merged", 400)):
            with mock.patch.dict(os.environ, {"QUEUE_REFUSE": f"refused {code}; nothing was queued", "QUEUE_CODE": code}):
                status, _, answer = self.move("requeue", item=1, head="1" * 40)
            self.assertEqual((code, status), (code, want), answer)
            if code != "stale_head":
                self.assertEqual(answer["error"], f"refused {code}; nothing was queued")

    def test_a_requeue_whose_publish_is_unknown_is_an_unknown_outcome(self):
        # `publish_unknown` also follows a push that may have landed on origin; the page cannot tell which.
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "origin did not answer; nothing was queued",
                                          "QUEUE_CODE": "publish_unknown"}):
            status, _, answer = self.move("requeue", item=1, head="1" * 40)
        self.assertEqual((status, answer), (502, {"error": "origin did not answer; nothing was queued", "outcome": "unknown"}))

    def test_a_post_without_the_csrf_token_runs_nothing(self):
        status, _, _ = self.move(**{"X-SD-CSRF": "0" * 64})
        self.assertEqual((status, self.calls()), (403, []))

    def test_a_post_from_another_origin_runs_nothing(self):
        status, _, _ = self.move(Origin="https://example.test")
        self.assertEqual((status, self.calls()), (403, []))

    def test_a_stale_revision_is_refused_before_any_verb(self):
        revision = queue_screen.revision(json.loads(self.fixture.read_text())["entries"])
        self.list(self.entries()[::-1])  # the queue moved after the page read it
        status, _, answer = self.post("/api/queue/move", {"repo": str(self.checkout), "item": 12, "action": "up", "revision": revision})
        self.assertEqual((status, answer.get("reload")), (409, True))
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"]])

    def test_an_unregistered_repository_or_unknown_action_runs_nothing(self):
        status, _, _ = self.post("/api/queue/move", {"repo": self.tmp.name, "item": 12, "action": "up", "revision": "x"})
        self.assertEqual((status, self.calls()), (400, []))
        status, _, _ = self.move("delete")
        self.assertEqual((status, self.calls()), (400, []))

    def reclaim(self, claim="claim-token-3:merge", revision=None):
        payload = {"repo": str(self.checkout), "item": 3, "action": "reclaim", "revision": revision or self.revision()}
        if claim is not None:
            payload["claim"] = claim
        return self.post("/api/queue/move", payload)

    def test_release_calls_lane_cancel_with_the_claim_the_row_showed(self):
        # sd:3337: `--expected-claim` binds the release to the claim the page read, in the verb's transaction
        # (pack sd:3330). The pending order is not what Release acts on, so no `--expected-revision`.
        status, _, answer = self.reclaim()
        self.assertEqual((status, answer["ok"], answer["action"]), (200, True, "reclaim"), answer)
        self.assertEqual(self.calls(), [["-C", str(self.checkout), "lane", "list"],
                                        ["-C", str(self.checkout), "lane", "cancel", "3", "--expected-claim",
                                         "claim-token-3:merge"]])

    def test_a_claim_the_lane_refuses_as_stale_asks_the_page_to_read_again(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:3 is not running under claim claim-token-3:merge",
                                          "QUEUE_CODE": "stale_claim"}):
            status, _, answer = self.reclaim()
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertIn("moved on", answer["error"])

    def test_a_release_the_lane_refuses_is_the_answer(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:3 is running under pid 4242 on this machine; it is not stuck",
                                          "QUEUE_CODE": "holder_alive"}):
            status, _, answer = self.reclaim()
        self.assertEqual((status, answer["error"]), (400, "sd:3 is running under pid 4242 on this machine; it is not stuck"))

    def test_a_release_the_hub_did_not_answer_is_an_unknown_outcome(self):
        # sd:3339: `lane cancel` may have released the claim before the hub fault; a 400 would say nothing changed.
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "The hub database did not answer.", "QUEUE_CODE": "hub_unavailable"}):
            status, _, answer = self.reclaim()
        self.assertEqual((status, answer), (502, {"error": "The hub database did not answer.", "outcome": "unknown"}))

    def test_release_without_a_whole_claim_runs_nothing(self):
        for claim in (None, "", "claim-token-3", ":merge", "claim-token-3:", 7, "x" * 300 + ":merge"):
            status, _, _ = self.reclaim(claim=claim)
            self.assertEqual((claim, status, self.calls()), (claim, 400, []))
        payload = {"repo": str(self.checkout), "item": 9, "action": "cancel", "revision": self.revision(),
                   "claim": "claim-token-3:merge"}
        status, _, _ = self.post("/api/queue/move", payload)
        self.assertEqual((status, self.calls()), (400, []))

    def test_a_lane_refusal_is_the_answer(self):
        with mock.patch.dict(os.environ, {"QUEUE_REFUSE": "sd:12 is running; the queue changes only between items"}):
            status, _, answer = self.move("up")
        self.assertEqual((status, answer["error"]), (400, "sd:12 is running; the queue changes only between items"))

    def fetch(self, path):
        parts = urllib.parse.urlsplit(self.base)
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers={"Cookie": self.cookie})
        answer = connection.getresponse()
        return answer.status, answer.getheader("Location"), answer.read().decode("utf-8")

    def test_the_old_page_opens_todays_queue_view_and_the_document_still_answers(self):
        # pack sd:7234: Queue is Today's Queue view; its Show all is that view's Ended builds filter.
        self.assertEqual(self.fetch("/queue"), (301, "/today#view=queue", ""))
        self.assertEqual(self.fetch("/queue?show=all"), (301, "/today?show=ended#view=queue", ""))
        self.assertEqual(self.fetch("/queue?show=active")[:2], (301, "/today#view=queue"))
        status, _, body = self.request("/today", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertIn('id="view-queue"', body)
        self.assertNotIn("/ui/queue.js", body)
        status, _, body = self.request("/api/queue", headers={"Cookie": self.cookie})
        self.assertEqual((status, len(json.loads(body)["lanes"])), (200, 1))


def pinned_pack() -> Path:
    """The pack whose `sd_db` this process loaded: the gate's pin, or `SD_PACK_ROOT` in a hand run (sd:5140).

    Not the `sd-ship` on PATH: on the hub that is the serving copy, and a fixture database one library writes, the
    other may refuse.
    """
    return pack_bin().parent


#: Three pending entries, written by the pinned pack's own `sd_lane.store` into the fixture database.
SEED = """\
import json, sys
sys.path.insert(0, sys.argv[1])
import sd_lane
from sd_db.database import connect, transaction
connection = connect(sys.argv[2], write=True)
with transaction(connection):
    for entry in json.loads(sys.argv[4]):
        sd_lane.store(connection, sys.argv[3], entry)
connection.close()
"""


#: The host name the pinned pack's lane writes as a holder's `host`.
HOST = """\
import sys
sys.path.insert(0, sys.argv[1])
import sd_lane
print(sd_lane.this_host())
"""


class ThePrCheck(BrowserSession):
    """sd:8375: Read again on Queue reads each shown item's pull request on GitHub; a merged one closes its item."""

    def setUp(self):
        super().setUp()
        from sd_db import contribution_github, contributions

        self.contributions = contributions
        self.items = {}
        for n in (21, 22, 23):
            url = f"https://github.com/example/system/pull/{n}"
            self.items[n] = contributions.capture(self.connection, title=f"PR {n}", changes={"pull_url": url},
                                                  who="test")["item"]["id"]
        self.connection.commit()
        states = {21: ("merged", "9" * 40), 22: ("closed", None), 23: ("open", None)}
        self.read = []

        class Client:
            def __init__(self, budget):
                pass

            def get(self, path):
                return {"id": 1, "login": "operator"}

        def pull(client, url, operator, **_):
            n = int(url.rsplit("/", 1)[1])
            self.read.append(n)
            if n == 23:
                raise contribution_github.Unavailable("GitHub request unavailable (exit 1)")
            return {"complete": True, "observed_at": "2026-09-06T12:00:00Z", "operator": operator, "author": operator,
                    "repo": "example/system", "title": f"PR {n}", "state": states[n][0], "head": "a" * 40,
                    "base": "b" * 40, "draft": False, "mergeable": "unknown", "ci": "success", "ci_head": "a" * 40,
                    "ci_ids": ["check:1"], "why": ["author"], "blocking_labels": [], "labels": [], "reviews": [], "events": [],
                    "requested_reviewers": [], **({"merge_commit": states[n][1]} if states[n][1] else {})}

        for patch in (mock.patch.object(contribution_github, "Client", Client),
                      mock.patch.object(contribution_github, "pull", pull)):
            patch.start()
            self.addCleanup(patch.stop)

    def status(self, n):
        return self.connection.execute("SELECT status FROM item WHERE id = ?", (self.items[n],)).fetchone()[0]

    def test_a_merged_pull_closes_its_item_and_a_closed_or_unread_one_does_not(self):
        status, _, answer = self.post("/api/queue/prs/check", {"items": list(self.items.values())})
        self.assertEqual(status, 200, answer)
        self.assertEqual((answer["checked"], answer["closed"]), ([self.items[21], self.items[22]], [self.items[21]]))
        self.assertEqual(answer["unread"], [f"sd:{self.items[23]} left open: https://github.com/example/system/pull/23:"
                                            " GitHub request unavailable (exit 1)"])
        self.assertEqual([self.status(n) for n in (21, 22, 23)], ["done", "planning", "planning"])
        reason = self.connection.execute("SELECT body FROM note WHERE item = ? AND kind = 'status_change' "
                                         "ORDER BY id DESC", (self.items[21],)).fetchone()[0]
        self.assertIn(f"PR https://github.com/example/system/pull/21 merged at {'9' * 40}", reason)
        closed = self.contributions.snapshot(self.connection, "github:https://github.com/example/system/pull/22")
        self.assertEqual(closed["observation"]["state"], "closed")

    def test_an_open_item_whose_merge_is_already_recorded_stays_open_even_when_named(self):
        """Only the first merged record closes: an item kept open after its recorded merge is not closed again."""
        url = "https://github.com/example/system/pull/21"
        before = self.contributions.snapshot(self.connection, "github:" + url)
        self.contributions.observe_pull(self.connection, url, {
            "complete": True, "observed_at": "2026-09-06T11:00:00Z", "operator": {"id": "1", "login": "operator"},
            "author": {"id": "1", "login": "operator"}, "repo": "example/system", "title": "PR 21", "state": "merged",
            "head": "a" * 40, "base": "b" * 40, "draft": False, "mergeable": "unknown", "ci": "success",
            "ci_head": "a" * 40, "ci_ids": ["check:1"], "why": ["author"], "blocking_labels": [], "labels": [],
            "reviews": [], "events": [], "requested_reviewers": []}, expected_revision=before["revision"])
        self.connection.commit()
        self.connection.execute("UPDATE item SET status = 'in_progress' WHERE id = ?", (self.items[21],))
        self.connection.commit()
        status, _, answer = self.post("/api/queue/prs/check", {"items": [self.items[21]]})
        self.assertEqual((status, answer["closed"], self.status(21), self.read), (200, [], "in_progress", [21]), answer)

    def test_only_the_named_items_are_read_and_the_request_is_bounded(self):
        status, _, _ = self.post("/api/queue/prs/check", {"items": [self.items[22]]})
        self.assertEqual((status, self.read), (200, [22]))
        for payload in ({"items": []}, {"items": ["1"]}, {"items": list(range(1, queue_screen.PR_CHECK_ITEMS + 2))},
                        {"items": [1], "extra": 1}):
            status, _, _ = self.post("/api/queue/prs/check", payload)
            self.assertEqual(status, 400, payload)


class RealLane(BrowserSession):
    """The pinned pack's `sd-ship lane` on a temporary lane root, seeded with `seeds()`.

    The pack keeps the queue in the hub database (pack sd:3282), so the entries go there through its own writer:
    a `<lane>/queue/queue.json` is only an earlier version's file, which `lane list` imports by publishing each
    pending head to `origin`. The checkout's origin names a GitHub repository, as every lane's must.
    """

    #: A move runs `lane list` and then the verb, each in a real `sd-ship` under the server's own bound; past both, the
    #: server itself hangs. Under load the 5 s default erred 8 tests whose server was still in bound (sd:5780).
    http_timeout = queue_screen.LIST_SECONDS + queue_screen.WRITE_SECONDS + 10

    def setUp(self):
        super().setUp()
        self.checkout = Path(self.tmp.name) / "system"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q", "-b", "main")
        origin = "https://github.com/example/system.git"
        git(self.checkout, "remote", "add", "origin", origin)
        upsert_repo(self.connection, str(self.checkout), remote=origin, managed=1)
        self.connection.commit()
        # `sd-ship` opens `$HOME/.local/share/sd/sd.db`; the fixture database stands there.
        home = Path(self.tmp.name) / "home"
        (home / ".local/share/sd").mkdir(parents=True)
        (home / ".local/share/sd/sd.db").symlink_to(self.path)
        root = Path(self.tmp.name) / "lanes"
        (root / "system" / "lane" / "queue").mkdir(parents=True)
        pack = pinned_pack()
        ship = pack / "bin" / "sd-ship"
        self.assertTrue(ship.is_file(), f"no pinned sd-ship at {ship}")
        self.host = subprocess.run([sys.executable, "-c", HOST, str(pack / "bin")], check=True, capture_output=True,
                                   text=True, env={**os.environ, "HOME": str(home)}).stdout.strip()
        self.pack, self.home = pack, home
        self.store(self.seeds())
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "sd-ship").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ship}" "$@"\n')
        (bin_dir / "sd-ship").chmod(0o755)
        patch = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "SD_LANE_ROOT": str(root),
                                             "HOME": str(home)})
        patch.start()
        self.addCleanup(patch.stop)

    def store(self, entries):
        """Write `entries` through the pinned pack's own `sd_lane.store`, as a runner's write would land."""
        subprocess.run([sys.executable, "-c", SEED, str(self.pack / "bin"), str(self.path), "example/system",
                        json.dumps(entries)], check=True, capture_output=True, text=True,
                       env={**os.environ, "HOME": str(self.home)})

    def seeds(self):
        return [{"id": f"seed-{n}", "item": n, "title": f"item {n}", "status": "pending", "position": n,
                 "expected_head": "e" * 40, "repository": "example/system", "branch": f"b{n}"} for n in (1, 2, 3)]

    def entries(self):
        return queue_screen.lane_list(str(self.checkout))["entries"]

    def order(self):
        return [entry["item"] for entry in self.entries() if entry["status"] == "pending"]


class ThePinnedPack(unittest.TestCase):
    """The real-lane tests write, run and read with one pack: the one whose `sd_db` this process loaded (sd:5140)."""

    def test_an_sd_ship_on_path_from_another_pack_is_not_the_pinned_pack(self):
        # On the hub, PATH's `sd-ship` is the serving copy and `SD_PACK_ROOT` the checkout. When they differ, the
        # fixture written by one pack's library and read by the other's failed 5 tests and erred 7 (SchemaTooNew).
        with tempfile.TemporaryDirectory() as other:
            ship = Path(other) / "bin" / "sd-ship"
            ship.parent.mkdir()
            ship.write_text("#!/bin/sh\nexit 1\n")
            ship.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{ship.parent}:{os.environ['PATH']}"}):
                self.assertEqual(pinned_pack(), pack_bin().parent)


class TheRealLaneBound(unittest.TestCase):
    """A real-lane request waits out the server's own `sd-ship` bounds, so load fails no test (sd:5780)."""

    def waited(self, case):
        seen = []

        class Answer:
            status, headers = 200, {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b""

        def urlopen(request, timeout):
            seen.append(timeout)
            return Answer()
        session = case("run")
        session.base = "http://192.0.2.1"
        # A quiet machine, so the budget read is the case's own: `patience` scales it by the load (sd:6158).
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("os.getloadavg", return_value=(0.0, 0.0, 0.0)):
            session.request("/api/queue")
        return seen[0]

    def test_a_real_lane_request_outwaits_the_list_and_the_verb(self):
        # At load 41-54 the 5 s bound erred 8 tests, though the server's list and verb were within their own bounds.
        waited = self.waited(TheRealLane)
        self.assertGreater(waited, queue_screen.LIST_SECONDS + queue_screen.WRITE_SECONDS)
        self.assertTrue(math.isfinite(waited))
        self.assertEqual(self.waited(TheRealRelease), waited)

    def test_a_stand_in_lane_keeps_the_short_bound(self):
        # A stand-in lane runs no `sd-ship`: it keeps the base bound, not the real lane's list-and-verb one (sd:7289).
        waited = self.waited(TheMove)
        self.assertEqual(waited, BrowserSession.http_timeout)
        self.assertLess(waited, self.waited(TheRealLane))


class TheRealLane(RealLane):
    """The page's reorder is what the pinned pack's `lane list` shows."""

    def test_a_reorder_from_the_page_is_the_order_lane_list_shows(self):
        lane = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        status, _, answer = self.post("/api/queue/move", {"repo": str(self.checkout), "item": 3, "action": "top",
                                                          "revision": lane["revision"]})
        self.assertEqual((status, answer.get("ok")), (200, True), answer)
        self.assertEqual(self.order(), [3, 1, 2])
        status, _, _ = self.post("/api/queue/move", {"repo": str(self.checkout), "item": 1, "action": "down",
                                                     "revision": lane["revision"]})
        self.assertEqual((status, self.order()), (409, [3, 1, 2]))

    def test_a_write_between_the_check_and_the_verb_is_refused_and_moves_nothing(self):
        # The verb checks the revision again under the queue's lock, so a writer that lands after the page's
        # check makes it refuse; the queue keeps the other writer's order.
        lane = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        listed = queue_screen.lane_list

        def then_another_writer(path):
            answer = listed(path)
            queue_screen._run([queue_screen.command("sd-ship"), "-C", path, "lane", "move", "2", "top"], 30)
            return answer
        with mock.patch.object(queue_screen, "lane_list", then_another_writer):
            status, _, answer = self.post("/api/queue/move", {"repo": str(self.checkout), "item": 3, "action": "top",
                                                              "revision": lane["revision"]})
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertEqual(self.order(), [2, 1, 3])


class TheRealRelease(RealLane):
    """Release on the pinned pack's `lane cancel --expected-claim` (sd:3337): the claim the row showed is released, and
    a claim that moved on since the read is refused with nothing changed, whatever the page's revision says."""

    step = "prepare"

    def holder(self):
        return {"host": "build-2", "pid": 4242, "token": "claim-token"}

    def running(self, **change):
        return {**super().seeds()[0], "status": "running", "step": self.step, "started_at": "2026-09-06T11:50:00Z",
                "lease_until": "2026-09-06T13:00:00Z", "holder": self.holder(), "enqueued_on": "build-2", **change}

    def seeds(self):
        return [self.running(), *super().seeds()[1:]]

    def read(self):
        """What the page read: its lane and the running row with the claim Release sends."""
        lane = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        merging = lane["rows"][0]
        self.assertEqual((merging["state"], merging["item"]), ("merging", 1))
        return lane, merging

    def release(self, lane, merging):
        return self.post("/api/queue/move", {"repo": str(self.checkout), "item": 1, "action": "reclaim",
                                             "revision": lane["revision"], "claim": merging["claim"]})

    def refused_unchanged(self, lane, merging):
        """The release of a claim that moved on is a 409, the lane is as the runner left it and the revision held."""
        before = self.entries()
        self.assertEqual(queue_screen.revision(before), lane["revision"])  # the page's revision cannot tell
        status, _, answer = self.release(lane, merging)
        self.assertEqual((status, answer.get("reload")), (409, True), answer)
        self.assertEqual(self.entries(), before)

    def test_release_puts_a_stuck_prepare_back_to_pending(self):
        lane, merging = self.read()
        self.assertEqual(merging["claim"], "claim-token:prepare")
        status, _, answer = self.release(lane, merging)
        self.assertEqual((status, answer.get("ok")), (200, True), answer)
        self.assertEqual([(entry["item"], entry["status"]) for entry in self.entries()],
                         [(1, "pending"), (2, "pending"), (3, "pending")])

    def test_release_fails_a_stuck_merge(self):
        self.store([self.running(step="merge")])
        lane, merging = self.read()
        status, _, answer = self.release(lane, merging)
        self.assertEqual((status, answer.get("ok")), (200, True), answer)
        self.assertEqual(self.entries()[0]["status"], "failed")

    def test_a_prepare_that_reached_merge_since_the_read_is_refused(self):
        # The old Release's race (sd:3284 review): a prepare read, then merge, must not be failed.
        lane, merging = self.read()
        self.store([self.running(step="merge", lease_until="2026-09-06T14:00:00Z")])
        self.refused_unchanged(lane, merging)
        self.assertEqual((self.entries()[0]["status"], self.entries()[0]["step"]), ("running", "merge"))

    def test_a_reclaim_at_the_same_revision_is_refused(self):
        # The entry was reclaimed and claimed again at the same step: the pending order never moved, a new token holds it.
        lane, merging = self.read()
        self.store([self.running(holder={"host": "build-3", "pid": 77, "token": "another-claim"})])
        self.refused_unchanged(lane, merging)
        self.assertEqual(self.entries()[0]["holder"]["token"], "another-claim")

    def test_a_finished_entry_is_refused(self):
        lane, merging = self.read()
        done = {key: value for key, value in self.running().items() if key not in ("holder", "step", "lease_until")}
        self.store([{**done, "status": "merged", "finished_at": "2026-09-06T12:05:00Z", "merge_commit": "f" * 40}])
        self.refused_unchanged(lane, merging)
        self.assertEqual(self.entries()[0]["status"], "merged")

    def test_release_of_a_holder_alive_on_this_machine_is_refused(self):
        self.store([self.running(holder={"host": self.host, "pid": os.getpid(), "token": "claim-token"})])
        lane, merging = self.read()
        before = self.entries()
        status, _, answer = self.release(lane, merging)
        self.assertEqual(status, 400, answer)
        self.assertIn("not stuck", answer["error"])
        self.assertEqual(self.entries(), before)


class TheInProcessRead(RealLane):
    """The page reads each lane in-process through the pinned pack's own `sd_lane` (sd:TBD): no `lane list` runs per
    repository, and what it reads is what `lane list` prints -- the queue path, the revision and every entry."""

    def seeds(self):
        pending = super().seeds()
        return [*pending,
                {**pending[0], "id": "seed-run", "item": 4, "status": "running", "step": "merge", "position": 0,
                 "started_at": "2026-09-06T11:50:00Z", "lease_until": "2026-09-06T13:00:00Z", "body": "the body",
                 "holder": {"host": "build-2", "pid": 77, "token": "claim-token"}},
                {**pending[0], "id": "seed-merged", "item": 5, "status": "merged", "position": 5,
                 "merge_commit": "f" * 40, "finished_at": "2026-09-06T10:00:00Z"},
                {**pending[0], "id": "seed-failed", "item": 6, "status": "failed", "step": "prepare", "position": 6,
                 "reason": "gate failed", "finished_at": "2026-09-06T09:00:00Z"},
                {**pending[0], "id": "seed-again", "item": 6, "status": "cancelled", "position": -1,
                 "finished_at": "2026-09-06T09:30:00Z"}]

    def listed(self):
        """What the pinned pack's `sd-ship lane list` prints for the checkout, its exit status aside."""
        answer = queue_screen.lane_list(str(self.checkout))
        self.assertEqual(answer.pop("exit"), 0, answer)
        return answer

    def test_the_in_process_read_is_what_lane_list_prints(self):
        self.store([{**self.seeds()[0], "held": True}])
        read = queue_screen.in_process(self.connection, str(self.checkout), "https://github.com/example/system.git")
        self.assertEqual(read, self.listed())
        self.assertEqual([entry["item"] for entry in read["entries"]], [6, 4, 1, 2, 3, 5, 6])

    def test_the_page_runs_no_lane_list(self):
        with mock.patch.object(queue_screen, "lane_list", side_effect=AssertionError("lane list ran")):
            doc = queue_screen.document(self.connection, now=NOW)
        lane = doc["lanes"][0]
        self.assertEqual(doc["problems"], [])
        self.assertEqual([(row["state"], row.get("item")) for row in lane["rows"]],
                         [("merging", 4), ("next", 1), ("next", 2), ("next", 3), ("cancelled", 6), ("landed", 5)])
        self.assertEqual(lane["revision"], queue_screen.revision(self.entries()))
        self.assertEqual(Path(lane["lane"]), Path(self.listed()["queue"]).parent.parent)

    def test_a_lane_hosted_on_a_satellite_reads_the_same_entries(self):
        # Pack sd:3003: the queue sits in the hub database whichever machine runs the lane.
        hub = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        self.connection.execute("UPDATE repo SET lane_host = 'build-2' WHERE path = ?", (str(self.checkout),))
        self.connection.commit()
        with mock.patch.object(queue_screen, "lane_list", side_effect=AssertionError("lane list ran")):
            satellite = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        self.assertEqual(satellite["host"], "build-2")
        self.assertEqual(satellite, {**hub, "host": "build-2"})

    def test_a_queue_file_an_earlier_version_left_is_read_by_lane_list(self):
        # `lane list` imports an earlier version's queue file into the database once; the page lets it.
        queue = Path(os.environ["SD_LANE_ROOT"]) / "system" / "lane" / "queue" / "queue.json"
        queue.write_text(json.dumps({"entries": []}))
        with mock.patch.object(queue_screen, "lane_list", wraps=queue_screen.lane_list) as listing:
            lane = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        listing.assert_called_once_with(str(self.checkout))
        self.assertEqual(len(lane["rows"]), 6)

    def test_without_the_packs_bin_lane_list_answers(self):
        with mock.patch.object(queue_screen, "pack_module", side_effect=ImportError("no bin")), \
                mock.patch.object(queue_screen, "lane_list", wraps=queue_screen.lane_list) as listing:
            lane = queue_screen.document(self.connection, now=NOW)["lanes"][0]
        listing.assert_called_once_with(str(self.checkout))
        self.assertEqual(len(lane["rows"]), 6)

    def test_a_pack_without_item_rounds_answers_through_lane_list(self):
        # An earlier pack (before sd:3483) has no `item_rounds`; its own `lane list` prints what it has.
        lane_module = queue_screen.pack_module("sd_lane")
        with mock.patch.object(lane_module, "item_rounds", None), \
                mock.patch.object(queue_screen, "lane_list", return_value={"ok": False, "error": "listed"}) as listing:
            doc = queue_screen.document(self.connection, now=NOW)
        listing.assert_called_once_with(str(self.checkout))
        self.assertEqual(doc["problems"], [{"repo": str(self.checkout), "error": "listed"}])

    def test_a_failed_read_is_a_named_problem_not_an_empty_lane(self):
        lane_module = queue_screen.pack_module("sd_lane")
        with mock.patch.object(lane_module, "stored", side_effect=sqlite3.OperationalError("database is locked")):
            doc = queue_screen.document(self.connection, now=NOW)
        self.assertEqual(doc["lanes"], [])
        self.assertEqual([problem["repo"] for problem in doc["problems"]], [str(self.checkout)])
        self.assertIn("database is locked", doc["problems"][0]["error"])


class TheShotRoute(Lane, BrowserSession):
    """sd:6728: `/lane-shots/` serves a PNG under a lane's `logs/shots-*`, and nothing else under the lane root."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"SD_LANE_ROOT": str(self.lane.parent.parent)})
        patch.start()
        self.addCleanup(patch.stop)
        self.shot_entries()

    def fetch(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as answer:
                return answer.status, answer.headers["Content-Type"], answer.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, None, b""

    def test_a_drawn_png_is_served_and_any_other_file_is_not(self):
        self.assertEqual(self.fetch("/lane-shots/system/lane/logs/shots-5-20260906T090000/home-dark-1440.png"),
                         (200, "image/png", b"\x89PNG\r\n\x1a\nshot"))
        for path in ("/lane-shots/system/lane/logs/prepare-3-20260906T115500.log",
                     "/lane-shots/system/lane/logs/shots-5-20260906T090000/home-light-375.png",
                     "/lane-shots/system/lane/logs/shots-5-20260906T090000/../../logs/x.png",
                     "/lane-shots/system/lane/gate-stale.log"):
            self.assertEqual(self.fetch(path)[0], 404, path)


def as_suggested(rows):
    """`sd_db.today.suggested`'s rows for queue rows that are all ready (pack sd:7314): the reason, due and priority."""
    return [{"id": r["id"], "repo": r["repo"], "title": r["title"], "status": r["status"], "why": "ready", "due": None,
             "priority": int(r["detail"][1:]) if r["detail"].startswith("P") else None, "since": r["since"]}
            for r in rows if r["queue"] == "ready"]


class TheView(Lane, ReaderCase):
    """pack sd:7234: Today's Queue view against the lane document `queue_screen` builds from the fixture, the build runs
    and runner assignments below, and the /api/now document with a live item and ready items."""

    BUILDS = {"error": "", "read": NOW, "show": "all", "quiet_seconds": 900, "stale_seconds": 180,
              "runs": [{"run": "r1", "attempt": 1, "repo": "/repos/alpha", "name": "alpha", "model": "opus", "step": "building",
                        "state": "stalled", "why": "", "outcome": None, "started": NOW, "elapsed": 1200, "beat": 20,
                        "quiet": 960, "usd": 0.42, "log": "/logs/r1.jsonl", "branch": "build/sd-7", "worktree": "/w/r1",
                        "pull": "https://github.com/example/alpha/pull/7",
                        "items": [{"item": 7, "assignment": 7, "status": "running", "title": "Seven"}],
                        "cancel": {"assignment": 7, "allowed": True, "reason": "", "revision": "a" * 64}}],
              "recent": [{"run": "r0", "assignment": 5, "repo": "/repos/gamma", "name": "gamma", "outcome": "interrupted",
                          "detail": "lost twice", "ended": NOW, "step": "building", "model": "opus", "usd": 1.1, "log": "",
                          "worktree": "/w/r0", "items": [{"item": 50, "assignment": 5, "status": "blocked", "title": "Fifty"}],
                          "retry": {"assignment": 5, "revision": "b" * 64, "allowed": True, "reason": "", "bundle": "blocked"}}]}
    RUNNER = {"live": [{"id": 7, "item": 7, "title": "Seven", "repo": "/repos/alpha", "role": "builder", "provider": "claude",
                        "status": "running", "lane": "serial", "queued_at": NOW, "started": NOW, "ended": None},
                       {"id": 41, "item": 41, "title": "Port the page", "repo": "~/repos/sd", "role": "merge", "provider": "claude",
                        "status": "queued", "lane": "serial", "queued_at": NOW, "started": None, "ended": None}]}
    LIVE = {"item": 40, "title": "Live work", "status": "in_progress", "repo": "~/repos/example/system", "kind": "worktree",
            "session": "worktree pack-40", "started": "2026-09-06T11:30:00Z", "elapsed": "30m", "seconds": 1800,
            "via": "branch name", "verified": True, "revision": "rev40", "item_kind": "task", "lane": ""}
    READY = [{"queue": "ready", "id": n, "repo": "~/repos/example/system", "title": f"Ready {n}", "status": "ready",
              "detail": "P1", "since": NOW} for n in (60, 61, 62, 63)] + [
             {"queue": "ready", "id": 70, "repo": "~/repos/example/beta", "title": "Beta ready", "status": "ready",
              "detail": "-", "since": NOW}]

    JS = """
const open = k => ELS.queue.listeners.click[0]({ target: { closest: s => s === 'tr.repo' ? { dataset: { repo: k } } : null } });
const press = v => ELS['queue-filters'].listeners.click[0]({ target: { closest: s => s === 'button[data-f="qshow"]' ? { dataset: { v } } : null } });
const act = (sel, data) => ELS.queue.listeners.click[0]({ target: { closest: s => s === sel ? { disabled: false, dataset: data } : null } });
const repos = () => [...ELS.queue.html.matchAll(/<tr class="repo" data-repo="([^"]*)" aria-expanded="(true|false)"/g)].map(m => m[1] + ':' + m[2]);
const kids = () => [...ELS.queue.html.matchAll(/<tr class="child" data-type="([a-z]+)"/g)].map(m => m[1]);
OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
"""

    def setUp(self):
        super().setUp()
        self.doc["active"] = {"rows": [self.LIVE], "sources": {}, "error": ""}
        self.doc["queues"] = {"rows": self.READY, "suggested": as_suggested(self.READY), "error": ""}

    # pack sd:7314: with no `show` key Suggested is on; the view's tests start from every filter off, as `?show=` keeps it.
    def run_view(self, body, search="?show=", answers=None, before=""):
        answers = {"/api/queue": [200, queue_screen.document(self.connection, now=NOW)],
                   "/api/queue/builds?show=all": [200, self.BUILDS], "/api/queue/runner": [200, self.RUNNER],
                   "/api/queue/move": [200, {"ok": True, "edits": "Edits take effect at the next item boundary."}],
                   **(answers or {})}
        after = (f"\nconst QDOCS = {json.dumps(answers)};\n"
                 "ANSWER = (p, b) => p === '/api/now' ? [200, DOC0] : QDOCS[p] || [404, { error: 'no answer' }];\n" + before)
        start = ("document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'queue', first: true } }));\n"
                 "await flush(); await flush(); await flush();\n")
        return self.run_page(self.JS + start + body, search=search, after=after)

    def pr_answers(self, check=(200, {"checked": [901], "closed": [], "unread": ["sd:902 left open: not read"]})):
        doc = queue_screen.document(self.connection, now=NOW)
        row = {"title": "PR", "repo": "system", "url": "https://github.com/example/system/pull/1", "lane": "awaiting_them",
               "on": "reviewers", "since": NOW, "waited": 60, "approximate": False, "draft": False, "stale": False}
        doc["prs"] = {"rows": [{**row, "item": 901}, {**row, "item": 902}, {**row, "item": 901}, {**row, "item": None}],
                      "error": ""}
        return {"/api/queue": [200, doc], "/api/queue/prs/check": list(check)}

    # sd:8375: the post is counted against the reads so far: the page reads Queue again after it, not before.
    AT = "const A0 = ANSWER; ANSWER = (p, b) => { if (p === '/api/queue/prs/check') OUT.at = OUT.gets.length; return A0(p, b); };\n"

    def test_read_again_on_queue_checks_the_shown_prs_once_then_reads_again(self):
        out = self.run_view("window.shell.view.current = () => 'queue'; R.before = OUT.gets.length;"
                            " await window.PAGE_AGAIN.run(); await flush(); await flush(); R.note = ELS['queue-note'].textContent;",
                            answers=self.pr_answers(), before=self.AT)
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/queue/prs/check", {"items": [901, 902]}]])
        self.assertEqual(out["at"], out["R"]["before"], "the page read again before the check answered")
        self.assertIn("/api/queue", out["gets"][out["at"]:])
        self.assertIn("sd:902 left open: not read", out["R"]["note"])

    def test_read_again_on_queue_with_no_pr_row_checks_nothing_and_reads(self):
        answers = self.pr_answers()
        answers["/api/queue"][1]["prs"] = {"rows": [], "error": ""}
        out = self.run_view("window.shell.view.current = () => 'queue'; await window.PAGE_AGAIN.run(); await flush();",
                            answers=answers)
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["gets"].count("/api/queue"), 2)

    def test_read_again_off_queue_or_a_failed_check_still_reads(self):
        out = self.run_view("await window.PAGE_AGAIN.run(); await flush();", answers=self.pr_answers())
        self.assertEqual(out["posts"], [])
        out = self.run_view("window.shell.view.current = () => 'queue'; await window.PAGE_AGAIN.run(); await flush(); await flush();"
                            " R.note = ELS['queue-note'].textContent;",
                            answers=self.pr_answers((502, {"error": "gh failed"})))
        self.assertIn("Pull requests not checked on GitHub: gh failed", out["R"]["note"])
        self.assertEqual(out["gets"].count("/api/queue"), 2)

    def test_each_repo_is_one_closed_row_that_a_click_opens_and_a_second_click_closes(self):
        out = self.run_view("R.closed = [repos(), kids()]; open('system'); R.open = [repos(), kids()];"
                            " open('system'); R.again = [repos(), kids()];")
        self.assertEqual(out["R"]["closed"], [["system:false", "alpha:false", "sd:false"], []])
        self.assertEqual(out["R"]["open"][0], ["system:true", "alpha:false", "sd:false"])
        self.assertEqual(out["R"]["open"][1], ["working"] * 3 + ["merging", "next", "next", "blocked", "blocked"])
        self.assertEqual(out["R"]["again"], [["system:false", "alpha:false", "sd:false"], []])
        self.assertEqual(out["gets"], ["/api/now", "/api/queue", "/api/queue/builds?show=all", "/api/queue/runner", "/api/tasks"])

    def test_repos_order_by_merging_then_the_other_counts_then_name(self):
        # pack sd:7679 (operator 2026-11-05): the busiest repo first, compared box by box in the boxes' order (merging,
        # review, working, next, blocked, suggested; pack sd:8137), each most first; a tie goes by repo name. A row's state does not order it.
        def asg(n, repo):
            return {**self.RUNNER["live"][0], "id": n, "item": n, "repo": repo}
        runner = {"live": self.RUNNER["live"] + [asg(n, "/repos/many") for n in (81, 82, 83, 84)]
                  + [asg(n, "/repos/aaa") for n in (91, 92)]}
        out = self.run_view("R.repos = repos();", answers={"/api/queue/runner": [200, runner]})
        # system merges one and works three; many works four, aaa two; alpha (a stalled build) and sd work one each.
        self.assertEqual(out["R"]["repos"], ["system:false", "many:false", "aaa:false", "alpha:false", "sd:false"])

    def test_opening_a_repo_closes_the_one_that_was_open(self):
        out = self.run_view("open('system'); open('alpha'); R.repos = repos(); R.kids = kids(); R.html = ELS.queue.html;")
        self.assertEqual(out["R"]["repos"], ["system:false", "alpha:true", "sd:false"])
        self.assertEqual(out["R"]["kids"], ["working"])
        self.assertIn('data-run="r1"', out["R"]["html"])
        self.assertNotIn("Live work", out["R"]["html"])

    def test_each_row_carries_its_type_in_words_beside_its_state_glyph(self):
        out = self.run_view("open('system'); R.html = ELS.queue.html.split('<tr class=\"repo\" data-repo=\"system\"')[1]"
                            ".split('<tr class=\"repo\"')[0];", search="?show=suggested,ended")
        found = re.findall(r'(?:<tr class="(repo|child)"[^>]*|^ aria-expanded="true")>\s*<td class="mk"><span class="g-(\w+)" aria-hidden="true">(.)</span>'
                           r'<span class="sr">([a-z ]+) · </span>(\w+)</td>', out["R"]["html"])
        system = [f[1:] for f in found]
        self.assertEqual(system[0], ("warning", "■", "needs you", "repo"))
        self.assertEqual([f[3] for f in system[1:]], ["working"] * 3 + ["merging", "next", "next", "blocked", "blocked"]
                         + ["suggested"] * 4 + ["ended", "ended"])
        self.assertIn(("caution", "▲", "look", "next"), system, "a held next row is ▲")
        self.assertEqual([f[3] for f in system if f[3] == "blocked"], ["blocked", "blocked"])
        self.assertTrue(all(f[0] == "warning" and f[1] == "■" for f in system if f[3] == "blocked"))
        # Every row has its type in words: no row is told by its colour alone.
        self.assertEqual(len(re.findall(r'<tr class="child"', out["R"]["html"])) + 1, len(found))

    @staticmethod
    def repo_detail(html, key):
        """The detail cell of repo `key`'s parent row: its extra classes, its other attributes and its contents."""
        found = re.search(rf'<tr class="repo" data-repo="{key}".*?<td class="detail([^"]*)"([^>]*)>(.*?)</td>', html, re.S)
        return found.group(1, 2, 3)

    BOX = {"merging": "merging: claimed by the lane runner, being merged",
           "review": "review: pull requests waiting on a review, 0 on you, 0 on reviewers",
           "working": "working: live sessions, build runs and lane gates now running",
           "next": "next: queued in the lane, not started",
           "blocked": "blocked: stopped in the lane until someone acts",
           "suggested": "suggested: up to 9 items to do next: ready, due today or overdue, planned or unscheduled"}

    def test_each_box_says_its_count_and_what_it_counts_on_hover(self):
        # Pack sd:7383: the native title names the count and the meaning; the aria-label stays.
        out = self.run_view("open('system'); R.html = ELS.queue.html;")
        titles = re.findall(r'<span class="pip[^"]*" title="([^"]*)">', self.repo_detail(out["R"]["html"], "system")[2])
        self.assertEqual(titles, [f"{n} {self.BOX[k]}" for n, k in zip((1, 0, 3, 2, 2, 4), self.BOX, strict=True)])

    def test_each_repo_row_counts_working_merging_next_blocked_review_and_suggested_in_six_boxes(self):
        # pack sd:7470: merging is the leftmost box (operator 2026-11-04).
        # pack sd:7272: the lane ledger's three boxes (#1768 removed them) stand in the parent row's detail cell, in place
        # of the text tally. Pack sd:7314 adds working first and suggested last (operator 07:10 MST: a repo with five
        # working rows read 0 0 0). Each counts every row of the repo, so a filter hides no count. A box is lit only when
        # its count is not zero; blocked is the warning lamp; the label says all five in words. Suggested is off in the
        # first read and on in the second: its count is 4 in both, all of system's ready items (sd:7869).
        # Pack sd:8083 adds review after blocked: the PR waits, none here.
        out = self.run_view("R.closed = ELS.queue.html; press('ended'); press('suggested'); open('system');"
                            " R.open = ELS.queue.html; R.tally = ELS['queue-tally'].html;")
        # Pack sd:7383: each box also carries its count and meaning in its title.
        system = ' aria-label="1 merging, 0 review, 3 working, 2 next, 2 blocked, 4 suggested"', \
            (f'<span class="pip on" title="1 {self.BOX["merging"]}">1</span><span class="pip" title="0 {self.BOX["review"]}">0</span>'
             f'<span class="pip on" title="3 {self.BOX["working"]}">3</span>'
             f'<span class="pip on" title="2 {self.BOX["next"]}">2</span><span class="pip warn" title="2 {self.BOX["blocked"]}">2</span>'
             f'<span class="pip on" title="4 {self.BOX["suggested"]}">4</span>')
        alpha = ' aria-label="0 merging, 0 review, 1 working, 0 next, 0 blocked, 0 suggested"', \
            (f'<span class="pip" title="0 {self.BOX["merging"]}">0</span><span class="pip" title="0 {self.BOX["review"]}">0</span>'
             f'<span class="pip on" title="1 {self.BOX["working"]}">1</span>') + "".join(
                f'<span class="pip" title="0 {self.BOX[k]}">0</span>' for k in ("next", "blocked", "suggested"))
        for html in (out["R"]["closed"], out["R"]["open"]):
            # pack sd:8138: each box sits in its toggle button; the boxes themselves are unchanged.
            for key, want in (("system", system), ("alpha", alpha)):
                extra, attrs, body = self.repo_detail(html, key)
                self.assertEqual((extra, attrs, re.sub(r"</?button[^>]*>", "", body)), (" pipe", *want))
        self.assertEqual(re.sub(r"<[^>]*>", "", self.repo_detail(out["R"]["closed"], "system")[2]), "103224", "the text tally is gone")
        self.assertRegex(out["R"]["tally"], r"<span>\d+ working</span>", "the page's tally stays")

    def test_the_boxes_recount_on_the_30_s_read_and_on_a_read_after_a_build_starts(self):
        # pack sd:7314: a repo with five working rows read 0 0 0. Its working box counts each read's rows: the 30 s
        # timer's, and Today's read again after a write such as Build (reading.reread draws, and the draw reads the queue).
        builds = json.loads(json.dumps(self.BUILDS))

        def more(n):
            return {**builds["runs"][0], "run": f"r{n}", "items": [{"item": n, "assignment": n, "status": "running", "title": "More"}],
                    "cancel": {**builds["runs"][0]["cancel"], "assignment": n}}
        out = self.run_view(
            "const label = () => (ELS.queue.html.match(/data-repo=\"alpha\"[\\s\\S]*?class=\"detail pipe\" aria-label=\"([^\"]*)\"/) || [])[1];"
            " R.first = label(); window.shell.view.current = () => 'queue';"
            f" QDOCS['/api/queue/builds?show=all'] = [200, {json.dumps({**builds, 'runs': [*builds['runs'], more(70)]})}];"
            " TICKS[0](); await flush(); await flush(); await flush(); R.tick = label();"
            f" QDOCS['/api/queue/builds?show=all'] = [200, {json.dumps({**builds, 'runs': [*builds['runs'], more(70), more(71)]})}];"
            " refresh(); await flush(); await flush(); await flush(); await flush(); R.reread = label();",
            before="var TICKS = []; setInterval = f => TICKS.push(f);\n")
        self.assertEqual((out["R"]["first"], out["R"]["tick"], out["R"]["reread"]),
                         tuple(f"0 merging, 0 review, {n} working, 0 next, 0 blocked, 0 suggested" for n in (1, 2, 3)))

    def test_a_lane_that_was_not_read_counts_nothing_and_says_so(self):
        # A failed read is not zero (sd:7272): a repo whose lane was not read says so in place of its boxes.
        lanes = queue_screen.document(self.connection, now=NOW)
        lanes["problems"] = [{"repo": "/repos/alpha", "error": "no checkout on this machine"}]
        read = self.run_view("R.html = ELS.queue.html;", answers={"/api/queue": [200, lanes]})["R"]["html"]
        self.assertIn('<span class="pip warn" title="2 blocked', self.repo_detail(read, "system")[2])
        self.assertEqual(self.repo_detail(read, "alpha")[1:], ("", '<span class="g-unknown" aria-hidden="true">▨</span> lane not read'))
        unread = self.run_view("R.html = ELS.queue.html;", answers={"/api/queue": [502, {"error": "down"}]})["R"]["html"]
        for key in ("system", "alpha"):
            self.assertNotIn("pip", self.repo_detail(unread, key)[2], key)
            self.assertIn("lane not read", self.repo_detail(unread, key)[2], key)

    def test_suggested_adds_each_repos_ready_items_and_a_repo_that_has_only_those(self):
        out = self.run_view("R.before = repos(); press('suggested'); open('system'); R.after = repos(); R.kids = kids();"
                            " R.html = ELS.queue.html; R.filters = ELS['queue-filters'].html;")
        self.assertNotIn("beta:false", out["R"]["before"])
        self.assertIn("beta:false", out["R"]["after"])
        self.assertEqual(out["R"]["kids"].count("suggested"), 4, "all four, under the cap of nine (sd:8008)")
        self.assertIn("#60 Ready 60", out["R"]["html"])
        self.assertIn("#63 Ready 63", out["R"]["html"])
        self.assertEqual(out["urls"], [{"show": "suggested"}])
        self.assertIn('data-f="qshow" data-v="suggested" aria-pressed="true"', out["R"]["filters"])
        kept = self.run_view("R.repos = repos();", search="?show=suggested")
        self.assertIn("beta:false", kept["R"]["repos"])

    def test_suggested_is_on_with_no_show_key_and_turned_off_stays_off_on_a_reload(self):
        # pack sd:7314 (operator request): no `show` key starts Suggested on. Turning every filter off writes `show=` empty,
        # so a reload keeps them off; a key naming other filters keeps Suggested off, as before.
        out = self.run_view("R.filters = ELS['queue-filters'].html; R.before = repos(); press('suggested'); R.after = repos();",
                            search="")
        self.assertIn('data-f="qshow" data-v="suggested" aria-pressed="true"', out["R"]["filters"])
        self.assertIn("beta:false", out["R"]["before"])
        self.assertNotIn("beta:false", out["R"]["after"])
        self.assertEqual(out["urls"], [{"show": ""}])
        for search in ("?show=", "?show=ended"):
            with self.subTest(search=search):
                kept = self.run_view("R.filters = ELS['queue-filters'].html; R.repos = repos();", search=search)
                self.assertIn('data-f="qshow" data-v="suggested" aria-pressed="false"', kept["R"]["filters"])
                self.assertNotIn("beta:false", kept["R"]["repos"])

    def test_a_repo_with_ten_suggested_items_draws_nine(self):
        # sd:7869, sd:8008 (operator 2026-11-07): Suggested keeps at most nine items per repo, in sd-next's order.
        self.doc["queues"]["suggested"] = [{"id": n, "repo": "~/repos/example/system", "title": f"Next {n}", "status": "ready",
                                            "why": "ready", "due": None, "priority": None} for n in range(61, 71)]
        out = self.run_view("open('system'); R.html = ELS.queue.html;", search="?show=suggested")
        drawn = re.findall(r'<tr class="child" data-type="suggested" data-item="(\d+)"', out["R"]["html"])
        self.assertEqual(drawn, [str(n) for n in range(61, 70)])

    def test_a_suggested_item_due_today_or_overdue_says_due_and_its_status(self):
        # pack sd:8238: an open item due today or before is suggested as `due`; its detail names the date, not ready.
        self.doc["queues"]["suggested"] = [{"id": 90, "repo": "~/repos/example/system", "title": "Overdue",
                                            "status": "planning", "why": "due", "due": "2026-09-01", "priority": 2}]
        out = self.run_view("open('system'); R.html = ELS.queue.html;", search="?show=suggested")
        found = re.findall(r'<tr class="child" data-type="suggested" data-item="(\d+)".*?<td class="detail">(.*?)</td>',
                           out["R"]["html"], re.S)
        self.assertEqual(found, [("90", "due 2026-09-01 · P2 · planning")])

    def test_suggested_is_sd_nexts_order_less_live_items_nine_per_repo_each_with_why(self):
        # pack sd:7314: Suggested is what sd-next offers next, ranked server-side (sd_db.today.suggested): ready, then
        # planned, then unscheduled. The view drops a live item and keeps the first nine per repo (sd:8008). Each detail says why;
        # a row that is not ready shows its status and offers no Build.
        def row(n, why, status, priority=None, due=None):
            return {"id": n, "repo": "~/repos/example/system", "title": f"Next {n}", "status": status, "why": why,
                    "due": due, "priority": priority}
        self.doc["queues"]["suggested"] = [row(60, "ready", "ready", 1), row(40, "planned", "in_progress", due="2026-09-08"),
                                           row(80, "planned", "planning", 2, "2026-09-20"), row(81, "unscheduled", "planning", 0),
                                           row(82, "unscheduled", "ready")]
        out = self.run_view("open('system'); R.html = ELS.queue.html; R.build = cmd('item.build').when({ id: '80' });",
                            search="?show=suggested")
        found = re.findall(r'<tr class="child" data-type="suggested" data-item="(\d+)".*?<td class="detail">(.*?)</td>',
                           out["R"]["html"], re.S)
        self.assertEqual(found, [("60", "ready · P1"), ("80", "planned for 2026-09-20 · P2 · planning"),
                                 ("81", "unscheduled P0 · planning"), ("82", "unscheduled · ready")])
        self.assertIn('aria-label="1 merging, 0 review, 3 working, 2 next, 2 blocked, 4 suggested"', out["R"]["html"])
        self.assertEqual(out["R"]["build"], "a build takes a ready task; this one is planning")

    def test_suggested_leaves_out_an_item_with_no_repo_so_no_unassociated_group_shows(self):
        # pack sd:7731 (operator ruling on sd:7699): a ready item with no repo is no Suggested row; it stays on Tasks.
        def row(n, repo):
            return {"id": n, "repo": repo, "title": f"Next {n}", "status": "ready", "why": "ready", "due": None, "priority": 2}
        self.doc["queues"]["suggested"] = [row(60, "~/repos/example/system"), row(4501, None), row(4503, None), row(4504, "")]
        out = self.run_view("R.repos = repos(); open('system'); R.html = ELS.queue.html; R.filters = ELS['queue-filters'].html;",
                            search="")
        self.assertEqual([r for r in out["R"]["repos"] if r.startswith(":")], [], "no Unassociated group")
        self.assertNotIn("Unassociated", out["R"]["html"])
        self.assertIn('data-type="suggested" data-item="60"', out["R"]["html"])
        self.assertIn('Suggested <span class="n">1</span>', out["R"]["filters"])

    def test_ended_builds_adds_ended_rows_and_a_repo_that_has_only_those(self):
        out = self.run_view("R.before = repos(); press('ended'); R.after = repos(); open('gamma'); R.gamma = ELS.queue.html;"
                            " open('system'); R.kids = kids(); press('suggested');")
        self.assertNotIn("gamma:false", out["R"]["before"])
        self.assertIn("gamma:false", out["R"]["after"])
        self.assertIn('data-build="retry" data-run="r0"', out["R"]["gamma"])
        self.assertIn('data-copy="sd assignments get 5"', out["R"]["gamma"])
        self.assertEqual(out["R"]["kids"][-2:], ["ended", "ended"])
        self.assertEqual(out["urls"], [{"show": "ended"}, {"show": "suggested,ended"}])
        kept = self.run_view("R.repos = repos();", search="?show=ended")
        self.assertIn("gamma:false", kept["R"]["repos"])

    def test_a_teammate_run_is_a_working_row_with_its_worktree_due_time_and_beat(self):
        """pack sd:7221: a lead's in-session builder shows beside the build runs, by repo, with its bundle's items."""
        team = {**self.BUILDS["runs"][0], "run": "t1", "teammate": True, "state": "working", "model": "", "log": "",
                "pull": "", "worktree": "/w/alpha-8", "branch": "feat/sd-8", "due": "2026-09-06T13:00:00Z", "beat": 300,
                "items": [{"item": 8, "assignment": 8, "status": "in_session", "title": "Eight"},
                          {"item": 9, "assignment": 9, "status": "in_session", "title": "Nine"}],
                "cancel": {"assignment": 8, "allowed": False, "reason": "its lead ends it: sd assignments beat 8 --end",
                           "revision": ""}}
        builds = {**self.BUILDS, "runs": [*self.BUILDS["runs"], team]}
        out = self.run_view("open('alpha'); R.alpha = ELS.queue.html;",
                            answers={"/api/queue/builds?show=all": [200, builds]})
        row = out["R"]["alpha"].split('<tr class="child" data-type="working" data-run="t1"')[1].split("</tr>")[0]
        for text in ("#8 Eight", "#9 Nine", "<b>working</b> · teammate · worktree <code>/w/alpha-8</code>",
                     "branch feat/sd-8", "beat 5m 0s ago", 'title="its lead ends it: sd assignments beat 8 --end"'):
            self.assertIn(text, row)
        self.assertRegex(row, r"due \d\d:\d\d")

    def test_a_satellite_run_names_its_host_and_log_path_and_copies_no_tail(self):
        """Pack sd:7933: a satellite's run logs on that host; a hub's `tail -f` of its path would follow nothing."""
        run = {**self.BUILDS["runs"][0], "host": "work", "quiet": None, "state": "working"}
        out = self.run_view("open('alpha'); R.alpha = ELS.queue.html;",
                            answers={"/api/queue/builds?show=all": [200, {**self.BUILDS, "runs": [run]}]})
        self.assertIn("· on work · log <code>/logs/r1.jsonl</code>", out["R"]["alpha"])
        self.assertNotIn("tail -f", out["R"]["alpha"])

    def test_a_queued_satellite_build_names_why_its_dispatcher_claims_nothing(self):
        """Review round 5: a queued build the hub leaves queued shows the satellite's reason on its row."""
        builds = {**self.BUILDS, "unclaimed": {"41": "work claims nothing: the load was not read"}}
        out = self.run_view("open('sd'); R.sd = ELS.queue.html;", answers={"/api/queue/builds?show=all": [200, builds]})
        self.assertIn("assignment 41 · merge · since", out["R"]["sd"])
        self.assertIn(" · work claims nothing: the load was not read</td>", out["R"]["sd"])

    def test_the_working_rows_hold_live_items_build_runs_the_lane_builders_and_free_assignments(self):
        out = self.run_view("open('system'); R.system = ELS.queue.html; open('alpha'); R.alpha = ELS.queue.html;"
                            " open('sd'); R.sd = ELS.queue.html; R.count = OUT.counts.queue;")
        self.assertIn('data-id="live:40"', out["R"]["system"])
        self.assertIn("worktree pack-40", out["R"]["system"])
        self.assertIn("<code>", out["R"]["system"], "a builder's gate in the lane is a working row")
        self.assertIn("<b>stalled</b> · build r1", out["R"]["alpha"])
        self.assertIn('data-build="cancel" data-run="r1"', out["R"]["alpha"])
        self.assertIn("tail -f &#39;/logs/r1.jsonl&#39;", out["R"]["alpha"])
        self.assertIn('href="https://github.com/example/alpha/pull/7"', out["R"]["alpha"])
        # Assignment 7 is run r1's: it shows once, as the run.
        self.assertNotIn("assignment 7 ·", out["R"]["alpha"])
        self.assertIn("<b>queued</b> · assignment 41", out["R"]["sd"])
        self.assertEqual(out["R"]["count"], 10)

    def test_an_item_waiting_on_its_pull_request_is_blocked_by_it_not_working(self):
        # pack sd:7859 (operator 2026-11-06): live item 40's PR waits on the operator, item 44's on reviewers. Each is a
        # blocked row naming who it waits on and for how long; item 40 is no working row, and no suggested row either.
        lanes = queue_screen.document(self.connection, now=NOW)
        lanes["prs"] = {"error": "", "rows": [
            {"item": 40, "title": "Live work", "repo": "~/repos/example/system", "url": "https://github.com/example/system/pull/40",
             "lane": "awaiting_you", "on": "you", "since": "2026-09-06T11:00:00+00:00", "waited": 3600},
            {"item": 44, "title": "Reviewed", "repo": "~/repos/example/system", "url": "https://github.com/example/system/pull/44",
             "lane": "awaiting_them", "on": "reviewers", "since": "2026-09-06T10:00:00+00:00", "waited": 7260}]}
        self.doc["queues"]["suggested"] = [{"id": 40, "repo": "~/repos/example/system", "title": "Live work", "status": "in_progress",
                                            "why": "planned", "due": "2026-09-08", "priority": 1}]
        out = self.run_view("open('system'); R.html = ELS.queue.html; R.kids = kids();", search="?show=suggested",
                            answers={"/api/queue": [200, lanes]})
        html = out["R"]["html"]
        self.assertNotIn('data-id="live:40"', html)
        self.assertNotIn('data-type="suggested" data-item="40"', html)
        rows = dict(re.findall(r'<tr class="child" data-type="blocked" data-pr="(\d+)">(.*?)</tr>', html, re.S))
        self.assertEqual(sorted(rows), ["40", "44"])
        self.assertIn('<span class="g-warning" aria-hidden="true">■</span><span class="sr">needs you · </span>blocked', rows["40"])
        self.assertIn("<b>blocked: PR</b> · on you · waited 1h 0m", rows["40"])
        self.assertIn('href="https://github.com/example/system/pull/40"', rows["40"])
        self.assertIn('<span class="sr">waiting · </span>blocked', rows["44"])
        self.assertIn("<b>blocked: PR</b> · on reviewers · waited 2h 1m", rows["44"])
        self.assertEqual(out["R"]["kids"].count("working"), 2, "the lane builders stay; the live item is gone")
        self.assertIn('aria-label="1 merging, 2 review, 2 working, 2 next, 2 blocked, 0 suggested"', html)

    def test_an_unlinked_pull_request_row_says_approx_and_draft(self):
        """sd:8116: a draft and an approximate wait say so on the row."""
        lanes = queue_screen.document(self.connection, now=NOW)
        lanes["prs"] = {"error": "", "rows": [
            {"item": None, "title": "Unlinked", "repo": "example/system", "url": "https://github.com/example/system/pull/5",
             "lane": "awaiting_you", "on": "you", "since": "2026-09-06T11:00:00+00:00", "waited": 3600,
             "approximate": True, "draft": True, "stale": False}]}
        out = self.run_view("open('system'); R.html = ELS.queue.html;", answers={"/api/queue": [200, lanes]})
        row = re.search(r'<tr class="child" data-type="blocked" data-pr="https://github.com/example/system/pull/5">(.*?)</tr>',
                        out["R"]["html"], re.S)[1]
        self.assertIn("<b>blocked: PR</b> · on you · waited 1h 0m", row)
        self.assertIn(" · draft · approx.", row)

    def test_a_stale_pull_request_row_says_so_and_counts_in_no_box(self):
        """sd:8116 (a): a reading over a day old waits on no one: no review or blocked count and no lamp; the row says so."""
        lanes = queue_screen.document(self.connection, now=NOW)
        lanes["prs"] = {"error": "", "rows": [
            {"item": None, "title": "Old", "repo": "~/repos/example/solo", "url": "https://github.com/example/solo/pull/5",
             "lane": "awaiting_you", "on": None, "since": "2026-09-01T11:00:00+00:00", "waited": None,
             "approximate": True, "draft": False, "stale": True}]}
        out = self.run_view("R.closed = ELS.queue.html; open('solo'); R.open = ELS.queue.html; R.tally = ELS['queue-tally'].html;",
                            answers={"/api/queue": [200, lanes]})
        self.assertEqual(self.repo_detail(out["R"]["closed"], "solo")[1],
                         ' aria-label="0 merging, 0 review, 0 working, 0 next, 0 blocked, 0 suggested"')
        self.assertIn("<b>PR not read for a day</b>", out["R"]["open"])
        self.assertNotIn("PR review", out["R"]["tally"])
        self.assertIn("<span>2 blocked</span>", out["R"]["tally"], "only the fixture's two lane rows count")

    def test_pr_review_waits_count_split_on_you_and_on_reviewers(self):
        # pack sd:8005 (operator 2026-11-07): PR waits folded into the blocked box and the tally with lane failures.
        # The tally counts them apart, split by who they wait on; pack sd:8083 moves each repo's to its review box.
        lanes = queue_screen.document(self.connection, now=NOW)
        lanes["prs"] = {"error": "", "rows": [
            {"item": 40, "title": "Live work", "repo": "~/repos/example/system", "url": "https://github.com/example/system/pull/40",
             "lane": "awaiting_you", "on": "you", "since": "2026-09-06T11:00:00+00:00", "waited": 3600},
            {"item": 44, "title": "Reviewed", "repo": "~/repos/example/system", "url": "https://github.com/example/system/pull/44",
             "lane": "awaiting_them", "on": "reviewers", "since": "2026-09-06T10:00:00+00:00", "waited": 7260},
            {"item": 5, "title": "Prepared only", "repo": "~/repos/example/system", "url": "https://github.com/example/system/pull/5",
             "lane": "awaiting_them", "on": "reviewers", "since": "2026-09-06T10:00:00+00:00", "waited": 7200}]}
        out = self.run_view("R.html = ELS.queue.html; R.tally = ELS['queue-tally'].html;", answers={"/api/queue": [200, lanes]})
        self.assertIn("<span>3 PR review: 1 on you, 2 on reviewers</span>", out["R"]["tally"])
        system = re.search(r'<tr class="repo" data-repo="system".*?</tr>', out["R"]["html"], re.S).group(0)
        self.assertIn('<span class="pip caution" title="3 review: pull requests waiting on a review, 1 on you, 2 on reviewers">3</span>', system)
        self.assertIn('<td class="num"></td>', system, "the review box says it; the num cell repeats nothing")
        out = self.run_view("R.tally = ELS['queue-tally'].html;")
        self.assertNotIn("PR review", out["R"]["tally"], "no PR wait, no count")

    def test_a_pr_wait_counts_in_a_review_box_after_blocked_not_in_blocked(self):
        # pack sd:8083 (operator 2026-11-07): "still not seeing a row tile counter in the queue list for waiting on
        # a review". A repo with one PR wait on you shows a review box of 1, lit caution, and blocked 0.
        lanes = queue_screen.document(self.connection, now=NOW)
        lanes["prs"] = {"error": "", "rows": [
            {"item": 90, "title": "Waits", "repo": "~/repos/example/solo", "url": "https://github.com/example/solo/pull/90",
             "lane": "awaiting_you", "on": "you", "since": "2026-09-06T11:00:00+00:00", "waited": 3600}]}
        out = self.run_view("R.html = ELS.queue.html;", answers={"/api/queue": [200, lanes]})
        extra, attrs, body = self.repo_detail(out["R"]["html"], "solo")
        self.assertEqual(attrs, ' aria-label="0 merging, 1 review, 0 working, 0 next, 0 blocked, 0 suggested"')
        boxes = re.findall(r'<span class="pip([^"]*)" title="([^"]*)">(\d+)</span>', body)
        self.assertEqual([(c, n) for c, _, n in boxes], [("", "0"), (" caution", "1"), ("", "0"), ("", "0"), ("", "0"), ("", "0")])
        self.assertEqual(boxes[1][1], "1 review: pull requests waiting on a review, 1 on you, 0 on reviewers")
        solo = re.search(r'<tr class="repo" data-repo="solo".*?</tr>', out["R"]["html"], re.S).group(0)
        self.assertIn('<td class="num"></td>', solo, "the box says it; the num cell repeats nothing")

    def test_the_review_box_stands_after_merging_and_before_working(self):
        # pack sd:8137 (operator 2026-11-08): merging, review, working, next, blocked, suggested, in the boxes and
        # the label. The review box keeps its caution lamp when a PR waits on you (ruling #49051), and not otherwise.
        def wait(on):
            return {"item": 90, "title": "Waits", "repo": "~/repos/example/solo", "url": "https://github.com/example/solo/pull/90",
                    "lane": "awaiting_you", "on": on, "since": "2026-09-06T11:00:00+00:00", "waited": 3600}
        for on, lamp in (("you", " caution"), ("reviewers", " on")):
            with self.subTest(on=on):
                lanes = queue_screen.document(self.connection, now=NOW)
                lanes["prs"] = {"error": "", "rows": [wait(on)]}
                html = self.run_view("R.html = ELS.queue.html;", answers={"/api/queue": [200, lanes]})["R"]["html"]
                extra, attrs, body = self.repo_detail(html, "solo")
                self.assertEqual(attrs, ' aria-label="0 merging, 1 review, 0 working, 0 next, 0 blocked, 0 suggested"')
                boxes = re.findall(r'<span class="pip([^"]*)" title="\d+ (\w+):', body)
                self.assertEqual(boxes, [("", "merging"), (lamp, "review"), ("", "working"), ("", "next"), ("", "blocked"),
                                         ("", "suggested")])

    # pack sd:8138 (operator 2026-11-08): a repo row's count tiles filter its sub-rows.
    TILE = ("const tile = (repo, t, disabled = false) => ELS.queue.listeners.click[0]({ target: { closest: s =>"
            " s === 'button[data-tile]' ? { disabled, dataset: { repo, tile: t } } : null } });"
            " const showAll = repo => ELS.queue.listeners.click[0]({ target: { closest: s =>"
            " s === 'button[data-tile-clear]' ? { dataset: { tileClear: repo } } : null } });"
            " const pressed = repo => [...(ELS.queue.html.split(`data-repo=\"${repo}\" aria-expanded`)[1] || '').split('</tr>')[0]"
            ".matchAll(/data-tile=\"(\\w+)\" aria-pressed=\"(true|false)\"/g)].map(m => m[1] + ':' + m[2]);")

    def test_a_tile_click_opens_its_repo_and_shows_only_that_tiles_rows(self):
        out = self.run_view(self.TILE + " tile('system', 'next'); R.repos = repos(); R.kids = kids(); R.html = ELS.queue.html;")
        self.assertIn("system:true", out["R"]["repos"])
        self.assertEqual(out["R"]["kids"], ["next", "next"])
        self.assertRegex(out["R"]["html"], r'<button type="button" class="linkbtn" data-tile-clear="system">'
                                           r'Showing next: 2 of \d+ · Show all</button>')

    def test_a_second_click_on_the_tile_or_show_all_restores_every_row(self):
        out = self.run_view(self.TILE + " open('system'); R.all = kids(); tile('system', 'next'); tile('system', 'next');"
                            " R.again = kids(); R.repos = repos(); tile('system', 'blocked'); R.blocked = kids();"
                            " showAll('system'); R.shown = kids(); R.html = ELS.queue.html;")
        self.assertEqual(out["R"]["again"], out["R"]["all"])
        self.assertIn("system:true", out["R"]["repos"], "the second click clears the filter, not the repo")
        self.assertEqual(out["R"]["blocked"], ["blocked", "blocked"])
        self.assertEqual(out["R"]["shown"], out["R"]["all"])
        self.assertNotIn("data-tile-clear", out["R"]["html"])

    def test_aria_pressed_follows_the_one_tile_that_filters(self):
        out = self.run_view(self.TILE + " R.before = pressed('system'); tile('system', 'working'); R.working = pressed('system');"
                            " tile('system', 'next'); R.next = pressed('system'); open('system'); R.closed = pressed('system');"
                            " open('system'); R.reopened = kids();")
        boxes = ["merging", "review", "working", "next", "blocked", "suggested"]
        self.assertEqual(out["R"]["before"], [f"{b}:false" for b in boxes])
        self.assertEqual(out["R"]["working"], [f"{b}:{str(b == 'working').lower()}" for b in boxes])
        self.assertEqual(out["R"]["next"], [f"{b}:{str(b == 'next').lower()}" for b in boxes], "another tile switches")
        self.assertEqual(out["R"]["closed"], [f"{b}:false" for b in boxes], "closing the repo clears its tile")
        self.assertEqual(out["R"]["reopened"].count("working"), 3)

    def test_a_zero_tile_is_disabled_and_does_nothing(self):
        out = self.run_view(self.TILE + " R.before = ELS.queue.html; tile('alpha', 'next', true); R.after = ELS.queue.html;"
                            " R.repos = repos();")
        self.assertEqual(out["R"]["after"], out["R"]["before"])
        self.assertIn("alpha:false", out["R"]["repos"])
        self.assertRegex(out["R"]["before"], r'data-repo="alpha" data-tile="next" aria-pressed="false"[^>]* disabled>')
        self.assertNotRegex(out["R"]["before"], r'data-repo="alpha" data-tile="working" aria-pressed="false"[^>]* disabled>')

    def test_the_suggested_tile_shows_suggested_rows_with_the_suggested_filter_off(self):
        out = self.run_view(self.TILE + " tile('system', 'suggested'); R.kids = kids(); R.filters = ELS['queue-filters'].html;")
        self.assertIn('data-v="suggested" aria-pressed="false"', out["R"]["filters"])
        self.assertEqual(out["R"]["kids"], ["suggested"] * 4)

    def test_a_pressed_review_tile_keeps_its_caution_lamp_and_gets_the_ring(self):
        # Rulings #49024 and #49052: a ring around the tile, no fill change; the review tile stays caution.
        lanes = queue_screen.document(self.connection, now=NOW)
        lanes["prs"] = {"error": "", "rows": [
            {"item": 90, "title": "Waits", "repo": "~/repos/example/solo", "url": "https://github.com/example/solo/pull/90",
             "lane": "awaiting_you", "on": "you", "since": "2026-09-06T11:00:00+00:00", "waited": 3600}]}
        out = self.run_view(self.TILE + " tile('solo', 'review'); R.html = ELS.queue.html; R.kids = kids();",
                            answers={"/api/queue": [200, lanes]})
        self.assertRegex(out["R"]["html"], r'data-repo="solo" data-tile="review" aria-pressed="true"[^>]*>'
                                           r'<span class="pip caution" title="1 review:')
        self.assertEqual(out["R"]["kids"], ["blocked"])
        css = (Path(__file__).resolve().parents[1] / "sd_dashboard/v2/static/today.css").read_text()
        self.assertIn('.queue .tile[aria-pressed="true"] .pip { box-shadow: 0 0 0 2px var(--color-ink); }', css)
        self.assertRegex(css, r'@media \(forced-colors: active\) \{[^}]*\.queue \.tile\[aria-pressed="true"\] \.pip '
                              r'\{ outline: 2px solid Highlight;')
        self.assertIn(".queue .tile:focus-visible { outline: 2px solid var(--color-focus);", css)

    def test_the_count_tiles_stay_in_one_row_on_a_touch_screen(self):
        # sd:8189: shell.css makes a td's direct button a grid block on touch; the tiles must stay inline.
        static = Path(__file__).resolve().parents[1] / "sd_dashboard/v2/static"
        self.assertIn("td > button:not(.btn, .icon-btn, .chip, .help)", (static / "shell.css").read_text())
        css = (static / "today.css").read_text()
        self.assertRegex(css, r"@media \(pointer: coarse\) \{ \.queue \.pipe > \.tile \{ display: inline-grid; \} \}")

    def test_a_prepared_entry_whose_pr_waits_on_review_is_one_row_that_leads_with_the_wait(self):
        # pack sd:7859: item 5 stopped prepared in a manual-merge lane; its PR waits on reviewers. One row, Approve kept.
        lanes = queue_screen.document(self.connection, now=NOW)
        lanes["prs"] = {"error": "", "rows": [
            {"item": 5, "title": "Prepared only", "repo": "~/repos/example/system", "url": "https://github.com/example/system/pull/5",
             "lane": "awaiting_them", "on": "reviewers", "since": "2026-09-06T10:00:00+00:00", "waited": 7200}]}
        out = self.run_view("open('system'); R.html = ELS.queue.html;", answers={"/api/queue": [200, lanes]})
        rows = re.findall(r'<tr class="child" data-type="blocked"[^>]*data-pr="5">(.*?)</tr>', out["R"]["html"], re.S)
        self.assertEqual(len(rows), 1, "the lane row and the PR wait are one row")
        self.assertIn("<b>blocked: PR</b> · on reviewers · waited 2h 0m", rows[0])
        self.assertIn('<span class="sr">waiting · </span>blocked', rows[0])
        self.assertIn('data-act="approve" data-item="5"', rows[0])
        self.assertIn('href="https://github.com/example/system/pull/5"', rows[0])
        self.assertEqual(out["R"]["html"].count('data-item="5"'), 2, "Retry and Approve, on that one row")

    def observe(self, number, *, author, why, requested=(), repo="example/anvil", state="open", reviews=(),
                draft=False):
        """An open pull request observed with no item: the operator is `1`, `author` is the writer's id."""
        from sd_db import contributions

        url = f"https://github.com/{repo}/pull/{number}"
        # sd:8116: `pr_rows` reads the real clock in these tests, and a reading over a day old is no current wait.
        observed = {"complete": True, "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "operator": {"id": "1", "login": "operator"},
                    "author": {"id": author, "login": f"user{author}"}, "repo": repo, "title": f"Pull {number}",
                    "state": state, "head": "a" * 40, "base": "b" * 40, "draft": draft, "mergeable": "mergeable",
                    "ci": "pending", "ci_head": "a" * 40, "ci_ids": [], "why": list(why),
                    "requested_reviewers": list(requested), "blocking_labels": [], "labels": [], "events": [],
                    "reviews": [{"id": f"review:{n}", "actor_id": "1", "state": state_, "at": "2026-09-06T10:00:00Z"}
                                for n, state_ in enumerate(reviews)]}
        contributions.observe_pull(self.connection, url, observed, expected_revision=contributions.snapshot(
            self.connection, "github:" + url)["revision"])
        self.connection.commit()
        return url

    def test_a_pr_with_no_item_waits_in_its_repos_review_box_in_both_directions(self):
        # sd:8142 (operator 2026-11-08): every open PR that names the operator counts, not only an item's pull_url.
        # Mine waits on reviewers; another's that asks my review by name or @mentions me waits on me. A team-only
        # request, or a closed PR, is no row.
        mine = self.observe(1, author="1", why=["author"])
        asked = self.observe(2, author="2", why=["review-requested"], requested=["operator"])
        named = self.observe(3, author="2", why=["mentioned"])
        self.observe(4, author="2", why=["review-requested"], requested=[])
        self.observe(5, author="1", why=["author"], state="closed")
        # A PR the operator already reviewed waits on its author: GitHub drops them from requested_reviewers.
        self.observe(6, author="2", why=["review-requested"], requested=[], reviews=["APPROVED"])
        self.observe(7, author="2", why=["review-requested"], requested=["someone"], reviews=["CHANGES_REQUESTED"])
        rows = queue_screen.pr_rows(self.connection, [], time.time())
        self.assertEqual(rows["error"], "")
        found = {r["url"]: (r["item"], r["repo"], r["on"]) for r in rows["rows"]}
        self.assertEqual(found, {mine: (None, "example/anvil", "reviewers"), asked: (None, "example/anvil", "you"),
                                 named: (None, "example/anvil", "you")})
        out = self.run_view("open('anvil'); R.html = ELS.queue.html; R.tally = ELS['queue-tally'].html;")
        extra, attrs, body = self.repo_detail(out["R"]["html"], "anvil")
        self.assertEqual(attrs, ' aria-label="0 merging, 3 review, 0 working, 0 next, 0 blocked, 0 suggested"')
        row = re.search(r'<tr class="child" data-type="blocked" data-pr="' + re.escape(named) + r'">(.*?)</tr>',
                        out["R"]["html"], re.S).group(1)
        self.assertIn(f'<a href="{named}" target="_blank" rel="noopener noreferrer">example/anvil#3 Pull 3</a>', row)
        self.assertIn("<b>blocked: PR</b> · on you", row)
        self.assertIn("<span>3 PR review: 2 on you, 1 on reviewers</span>", out["R"]["tally"])

    def test_a_pr_the_operator_already_answered_no_longer_waits_on_them(self):
        # sd:8142 class pass (#49159): each `why` against reviewed or not. A review answers another's PR whether it
        # asked by name or @mentioned the operator (`mentions:@me` stays true for the PR's life); a request made
        # again after the review asks anew. The operator's own PR is not answered by their own review.
        table = (  # (why, author, requested, reviews, draft, on)
            (["review-requested"], "2", ["operator"], (), False, "you"),
            (["review-requested"], "2", [], ["APPROVED"], False, None),
            (["review-requested"], "2", ["operator"], ["COMMENTED"], False, "you"),
            (["mentioned"], "2", [], (), False, "you"),
            (["mentioned"], "2", [], ["APPROVED"], False, None),
            (["mentioned"], "2", [], ["COMMENTED"], False, None),
            (["mentioned", "review-requested"], "2", ["operator"], ["CHANGES_REQUESTED"], False, "you"),
            (["author"], "1", [], (), False, "reviewers"),
            (["author"], "1", [], ["COMMENTED"], False, "reviewers"),
            (["author"], "1", [], (), True, "you"),
            (["author"], "1", [], ["COMMENTED"], True, "you"),
        )
        want = {}
        for number, (why, author, requested, reviews, draft, on) in enumerate(table, start=10):
            url = self.observe(number, author=author, why=why, requested=requested, reviews=reviews, draft=draft)
            want[url] = on
        rows = queue_screen.pr_rows(self.connection, [], time.time())
        found = {r["url"]: r["on"] for r in rows["rows"]}
        for (why, _author, requested, reviews, draft, _on), url in zip(table, want, strict=True):
            with self.subTest(why=why, requested=requested, reviews=reviews, draft=draft):
                self.assertEqual(found.get(url), want[url])

    def test_an_unmanaged_builds_draft_is_blocked_on_its_pr_and_waits_on_you(self):
        # pack sd:7868 (operator 2026-11-06): a build in an unmanaged repository ends in a draft PR the operator must
        # mark ready. From the real observation, not a written row: the draft reads `awaiting_you`, so Queue draws the
        # item blocked by that PR, linked, as needing the operator.
        from sd_db import contributions
        from sd_db.writes import create_item

        repo = Path(self.tmp.name) / "anvil"
        upsert_repo(self.connection, str(repo), remote="https://github.com/example/anvil.git", managed=0)
        item = create_item(self.connection, kind="task", title="Built in a third-party repository", repo=str(repo),
                           status="in_progress")
        url = "https://github.com/example/anvil/pull/41"
        contributions.configure(self.connection, item, {"pull_url": url}, who="build")
        observed = {"complete": True, "observed_at": "2026-09-06T11:00:00Z", "operator": {"id": "1", "login": "author"},
                    "author": {"id": "1", "login": "author"}, "repo": "example/anvil", "title": "Built",
                    "state": "open", "head": "a" * 40, "base": "b" * 40, "draft": True, "mergeable": "mergeable",
                    "ci": "success", "ci_head": "a" * 40, "ci_ids": ["check:1"], "why": ["author"],
                    "blocking_labels": [], "labels": [], "reviews": [], "events": []}
        contributions.observe_pull(self.connection, url, observed, expected_revision=contributions.snapshot(
            self.connection, "github:" + url)["revision"])
        self.connection.commit()
        # pack sd:8137: a review box sorts its repo high, so open the one repo, not each in turn.
        out = self.run_view("open('anvil'); R.open = ELS.queue.html;")
        rows = dict(re.findall(r'<tr class="child" data-type="blocked" data-pr="(\d+)">(.*?)</tr>', out["R"]["open"],
                               re.S))
        self.assertIn(str(item), rows, out["R"]["open"][-3000:])
        self.assertIn("<b>blocked: PR</b> · on you", rows[str(item)])
        self.assertIn(f'href="{url}"', rows[str(item)])
        self.assertIn('<span class="sr">needs you · </span>blocked', rows[str(item)])

    def test_a_pr_wait_that_was_not_read_is_said_under_the_table(self):
        lanes = queue_screen.document(self.connection, now=NOW)
        lanes["prs"] = {"rows": [], "error": "pull-request waits were not read: OperationalError: database is locked"}
        out = self.run_view("R.note = ELS['queue-note'].textContent;", answers={"/api/queue": [200, lanes]})
        self.assertIn("pull-request waits were not read: OperationalError: database is locked", out["R"]["note"])

    def test_a_click_on_a_live_row_shows_its_details_and_keeps_its_fix_and_open_commands(self):
        out = self.run_view("open('system'); ELS.queue.listeners.click[0]({ target: { closest: s => s === 'tr[data-id]' ? "
                            "{ dataset: { id: 'live:40' } } : null } }); R.details = ELS.details.html; R.row = location.search;"
                            " R.cmds = REG.filter(c => c.on === 'live item').map(c => c.id);", search="")
        self.assertIn("Live work", out["R"]["details"])
        self.assertEqual(out["R"]["row"], "?row=live%3A40")
        self.assertEqual(sorted(out["R"]["cmds"]), ["live item.fix", "today.live.assignment", "today.live.open"])

    # pack sd:7271: a suggested row is an item row: a click selects it (?row=<id>), its Details are the Tasks page's
    # (task-details.js, from /api/tasks/<id>), and its act is Tasks' Build, which reads the plan before it queues.
    def suggested(self):
        from sd_dashboard import tasks_screen
        from sd_db import workflow
        item = self.item("Ship the pickup", kind="task", priority=2, repo=str(self.checkout))
        workflow.change_status(self.connection, item, "ready", who="test")
        self.doc["queues"]["rows"] = [{"queue": "ready", "id": item, "repo": "~/repos/example/system", "title": "Ship the pickup",
                                       "status": "ready", "detail": "P2", "since": NOW}] + self.READY
        self.doc["queues"]["suggested"] = as_suggested(self.doc["queues"]["rows"])
        return item, {f"/api/tasks/{item}": [200, tasks_screen.details(self.connection, item, now=NOW)]}

    PICK = ("C.rowActions = id => mk`<i data-acts=\"${id}\"></i>`; open('system'); R.html = ELS.queue.html;"
            " ELS.queue.listeners.click[0]({ target: { closest: s => s === 'tr[data-id]' ? { dataset: { id: 'ID' } } : null } });"
            " await flush(); await flush(); R.details = ELS.details.html; R.row = location.search;")

    def test_a_suggested_row_selects_and_shows_the_items_task_details(self):
        item, answers = self.suggested()
        out = self.run_view(self.PICK.replace("ID", str(item)), search="?show=suggested", answers=answers)
        self.assertIn(f'data-type="suggested" data-item="{item}" data-id="{item}" aria-selected=', out["R"]["html"])
        self.assertEqual(out["R"]["row"], f"?show=suggested&row={item}")
        self.assertIn(f"/api/tasks/{item}", out["gets"])
        # The Tasks page's Details: its editable fields, read from the item route.
        self.assertIn('id="det-title" class="fld" value="Ship the pickup"', out["R"]["details"])
        self.assertIn('id="det-status"', out["R"]["details"])

    def test_a_suggested_row_acts_with_build_and_build_reads_the_plan_first(self):
        item, answers = self.suggested()
        plan = {"read": NOW, "budget_minutes": 60, "refused": [], "room": {"machine": 4, "why": "", "cap": 6, "repo_cap": 1},
                "bundles": [{"repo": str(self.checkout), "name": "system", "room": 2, "model": "opus", "branch": "build/sd-1",
                             "items": [{"id": item, "title": "Ship the pickup", "revision": "c" * 64}]}]}
        answers[f"/api/tasks/build/plan?items={item}"] = [200, plan]
        answers["/api/tasks/build"] = [200, {"queued": [{"id": 9, "item": item, "status": "queued"}], "kickstart": {"ok": True}}]
        out = self.run_view(self.PICK.replace("ID", str(item))
                            + f" const o = C.get('{item}'), on = REG.filter(c => c.on === o.type && (!c.when || c.when(o) === true));"
                            " R.live = on.map(c => c.id); R.primary = (on.find(c => c.primary && c.primary(o)) || {}).id;"
                            " shellRun(cmd('item.build'), o); await flush();"
                            " const D = MADE.find(e => /id=\"build-h\"/.test(e.html || '')); R.shown = D ? D.html : null; R.before = OUT.posts.length;"
                            " if (D) { D.returnValue = 'build'; D.onclose(); await flush(); }",
                            search="?show=suggested", answers=answers)
        self.assertIn(f'<i data-acts="{item}"></i>', out["R"]["html"], "the row's act cell draws its commands")
        self.assertIn("item.build", out["R"]["live"])
        self.assertEqual(out["R"]["primary"], "item.build")
        self.assertIn(f"<code>#{item}</code> Ship the pickup", out["R"]["shown"])
        self.assertEqual(out["R"]["before"], 0, "Build posts nothing before the dialog's Build")
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/tasks/build", {"items": [item], "revisions": {str(item): "c" * 64}}]])

    def test_build_on_an_unread_suggested_row_reads_the_queue_rows_repository(self):
        """Pack sd:7275. Before Details load, Build reads the queue row: its repo path allows. A null one is no
        Suggested row since pack sd:7731, so Build finds no row and refuses."""
        item, answers = self.suggested()
        on = self.run_view(f"R.on = cmd('item.build').when({{ id: '{item}' }});", search="?show=suggested", answers=answers)
        self.assertIs(on["R"]["on"], True)
        self.doc["queues"]["suggested"][0]["repo"] = None
        off = self.run_view(f"R.off = cmd('item.build').when({{ id: '{item}' }});", search="?show=suggested", answers=answers)
        self.assertEqual(off["R"]["off"], "the task is no longer listed")

    # pack sd:7334: a planned or an unscheduled suggested row offers its next status before its Details are read, as a
    # Tasks list row does: its task is the /api/tasks row, read while Suggested shows, not only once Details open.
    def test_a_planned_and_an_unscheduled_suggestion_offer_their_next_status_unopened(self):
        from sd_dashboard import tasks_screen
        from sd_db import workflow
        planned = self.item("Due later", kind="task", priority=2, repo=str(self.checkout), due="2026-09-20")
        workflow.change_status(self.connection, planned, "in_progress", who="test", reason="test")
        loose = self.item("No date", kind="task", priority=1, repo=str(self.checkout))
        self.doc["queues"]["suggested"] = [
            {"id": planned, "repo": "~/repos/example/system", "title": "Due later", "status": "in_progress",
             "why": "planned", "due": "2026-09-20", "priority": 2},
            {"id": loose, "repo": "~/repos/example/system", "title": "No date", "status": "planning",
             "why": "unscheduled", "due": None, "priority": 1}]
        answers = {"/api/tasks": [200, tasks_screen.document(self.connection, now=NOW)]}
        def primary(n):
            return (f"(() => {{ const o = C.get('{n}'), on = REG.filter(c => c.on === o.type && (!c.when || c.when(o) === true));"
                    " return (on.find(c => c.primary && c.primary(o)) || {}).id; })()")
        out = self.run_view(f"open('system'); R.html = ELS.queue.html; R.planned = {primary(planned)}; R.loose = {primary(loose)};"
                            f" R.done = cmd('item.status.done').when({{ id: '{planned}' }});"
                            f" R.ready = cmd('item.status.ready').when({{ id: '{loose}' }});",
                            search="?show=suggested", answers=answers)
        self.assertIn(f'data-type="suggested" data-item="{loose}"', out["R"]["html"])
        self.assertEqual([g for g in out["gets"] if g.startswith("/api/tasks/")], [], "no Details were read")
        self.assertEqual(out["R"]["done"], True)
        self.assertEqual(out["R"]["ready"], True)
        self.assertEqual(out["R"]["planned"], "item.status.done")
        self.assertEqual(out["R"]["loose"], "item.status.ready")
        # Turned on from off, Suggested reads the tasks; a tasks read that failed is said, not taken as no task.
        on = self.run_view("R.before = OUT.gets.slice(); press('suggested'); await flush(); await flush(); await flush();"
                           f" R.ready = cmd('item.status.ready').when({{ id: '{loose}' }});", answers=answers)
        self.assertIn("/api/tasks", on["R"]["before"], "off, the view reads tasks for All items' count (pack sd:8127)")
        self.assertEqual(on["R"]["ready"], True)
        failed = self.run_view("R.note = ELS['queue-note'].textContent;", search="?show=suggested",
                               answers={"/api/tasks": [500, {"error": "tasks down"}]})
        self.assertIn("Suggested items' tasks not read", failed["R"]["note"])

    # pack sd:7271 (#40961): All items adds the repo's other open items, read from /api/tasks (always, pack sd:8127). Each
    # is an item row as a suggested one is, and its act is the one Tasks' list row offers: the move to its next status.
    def open_items(self):
        from sd_dashboard import tasks_screen
        from sd_db import workflow
        made = {}
        for title, status in (("Held up", "blocked"), ("Half done", "in_progress"), ("Finished", "done")):
            made[status] = self.item(title, kind="task", priority=2, repo=str(self.checkout))
            workflow.change_status(self.connection, made[status], status, who="test", reason="test")
        answers = {"/api/tasks": [200, tasks_screen.document(self.connection, now=NOW)]}
        for n in made.values():
            answers[f"/api/tasks/{n}"] = [200, tasks_screen.details(self.connection, n, now=NOW)]
        return made, answers

    def test_all_items_adds_the_repos_other_open_items_read_from_tasks(self):
        made, answers = self.open_items()
        out = self.run_view("R.before = OUT.gets.slice(); press('open'); await flush(); await flush(); open('system');"
                            " R.kids = kids(); R.html = ELS.queue.html; R.filters = ELS['queue-filters'].html;", answers=answers)
        self.assertIn("/api/tasks", out["R"]["before"], "off, the view reads tasks for the count (pack sd:8127)")
        self.assertEqual(out["urls"], [{"show": "open"}])
        self.assertIn('data-f="qshow" data-v="open" aria-pressed="true"', out["R"]["filters"])
        self.assertIn("All items", out["R"]["filters"])
        for status in ("blocked", "in_progress"):
            n = made[status]
            self.assertIn(f'data-type="open" data-item="{n}" data-id="{n}" aria-selected=', out["R"]["html"])
        self.assertNotIn("Finished", out["R"]["html"], "a done item is not open")
        self.assertIn("open", out["R"]["kids"])
        kept = self.run_view("open('system'); R.kids = kids();", search="?show=open", answers=answers)
        self.assertIn("open", kept["R"]["kids"])

    def test_all_items_leaves_ideas_out_they_live_on_writing(self):
        """pack sd:7699: an idea of any idea kind is no Queue row, with a repo or without; a task still is."""
        from sd_dashboard import tasks_screen
        made = {kind: self.item(f"A {kind}", kind=kind, priority=2, repo=str(self.checkout))
                for kind in ("task", "idea", "work-idea", "personal-idea")}
        loose = {kind: self.item(f"Loose {kind}", kind=kind, priority=2) for kind in ("idea", "work-idea")}
        answers = {"/api/tasks": [200, tasks_screen.document(self.connection, now=NOW)]}
        out = self.run_view("R.repos = repos(); open('system'); R.html = ELS.queue.html; open(''); R.none = ELS.queue.html;",
                            search="?show=open", answers=answers)
        self.assertIn(f'data-type="open" data-item="{made["task"]}"', out["R"]["html"], "a task is a Queue row")
        for kind in ("idea", "work-idea", "personal-idea"):
            self.assertNotIn(f'data-type="open" data-item="{made[kind]}"', out["R"]["html"], f"a {kind} lives on Writing")
        for n in loose.values():
            self.assertNotIn(f'data-type="open" data-item="{n}"', out["R"]["none"], "an idea with no repo is no row")
        self.assertEqual([r for r in out["R"]["repos"] if r.startswith(":")], [], "no Unassociated group")

    def test_an_open_row_selects_into_task_details_and_offers_its_next_status_move(self):
        made, answers = self.open_items()
        for status, move in (("blocked", "item.status.ready"), ("in_progress", "item.status.done")):
            n = made[status]
            out = self.run_view(self.PICK.replace("ID", str(n))
                                + f" const o = C.get('{n}'), on = REG.filter(c => c.on === o.type && (!c.when || c.when(o) === true));"
                                " R.primary = (on.find(c => c.primary && c.primary(o)) || {}).id;",
                                search="?show=open", answers=answers)
            self.assertIn(f'<i data-acts="{n}"></i>', out["R"]["html"], "the row's act cell draws its commands")
            self.assertEqual(out["R"]["row"], f"?show=open&row={n}")
            self.assertIn(f"/api/tasks/{n}", out["gets"])
            self.assertIn('id="det-status"', out["R"]["details"])
            self.assertEqual(out["R"]["primary"], move, status)

    # pack sd:7251: a read that stops listing a lane entry or an open item retires its command object, as the reader
    # retires a Needs you row: it becomes 'not listed', so no row menu or Details bar runs on it.
    def test_a_read_that_drops_an_entry_or_an_open_row_retires_its_commands(self):
        made, answers = self.open_items()
        n, gone, kept = made["blocked"], f"entry:{self.checkout}:9", f"entry:{self.checkout}:12"
        out = self.run_view(f"press('open'); await flush(); await flush(); R.before = [C.get('{gone}').type, C.get('{n}').type];"
                            " const L = QDOCS['/api/queue'][1].lanes[0]; L.rows = L.rows.filter(r => r.item !== 9);"
                            " refresh(); await flush(); await flush(); await flush();"
                            f" R.entry = [C.get('{gone}').type, C.get('{kept}').type];"
                            f" press('open'); await flush(); await flush(); R.open = C.get('{n}').type;", answers=answers)
        self.assertEqual(out["R"]["before"], ["entry", "item"])
        self.assertEqual(out["R"]["entry"], ["not listed", "entry"])
        self.assertEqual(out["R"]["open"], "not listed")

    # pack sd:7251: a filter's count counts its rows in every repo, also a repo that shows only while the filter is on.
    def test_each_filter_counts_the_rows_of_repos_it_would_add(self):
        from sd_dashboard import tasks_screen
        out = self.run_view("R.filters = ELS['queue-filters'].html;",
                            answers={"/api/tasks": [200, tasks_screen.document(self.connection, now=NOW)]})
        counts = dict(re.findall(r'data-v="(\w+)" aria-pressed="false">[^<]+<span class="n">(\d+)</span>', out["R"]["filters"]))
        self.assertEqual(counts, {"suggested": "5", "open": "0", "ended": "3"})

    # pack sd:8127: with All items off, its tile still counts the open items it would show; its rows stay hidden, and a
    # failed tasks read shows no count rather than 0.
    def test_all_items_off_counts_its_open_items_on_the_tile(self):
        made, answers = self.open_items()
        out = self.run_view("open('system'); R.kids = kids(); R.filters = ELS['queue-filters'].html;", answers=answers)
        counts = dict(re.findall(r'data-v="(\w+)" aria-pressed="false">[^<]+<span class="n">(\d+)</span>', out["R"]["filters"]))
        self.assertEqual(counts.get("open"), "2", "blocked and in progress count; done does not")
        self.assertNotIn("open", out["R"]["kids"])
        failed = self.run_view("R.filters = ELS['queue-filters'].html;", answers={"/api/tasks": [500, {"error": "tasks down"}]})
        self.assertRegex(failed["R"]["filters"], r'data-v="open" aria-pressed="false">All items</button>')

    # pack sd:7251 (#41066): with Suggested off, All items shows the ready items Suggested would; on, each shows once.
    def test_all_items_alone_shows_the_ready_items_suggested_holds(self):
        from sd_dashboard import tasks_screen
        item, answers = self.suggested()
        answers["/api/tasks"] = [200, tasks_screen.document(self.connection, now=NOW)]
        out = self.run_view("open('system'); R.alone = ELS.queue.html; press('suggested'); await flush(); await flush();"
                            " R.both = ELS.queue.html;", search="?show=open", answers=answers)
        self.assertIn(f'data-type="open" data-item="{item}"', out["R"]["alone"])
        self.assertIn(f'data-type="suggested" data-item="{item}"', out["R"]["both"])
        self.assertNotIn(f'data-type="open" data-item="{item}"', out["R"]["both"])

    # pack sd:7251 (#41066): a task selected once keeps its read row; a later read at another revision replaces it, so
    # its actions follow the fresh status, not the one it had when it was selected.
    def test_an_open_row_selected_before_acts_on_the_fresh_row_after_a_read(self):
        from sd_dashboard import tasks_screen
        from sd_db import workflow
        made, answers = self.open_items()
        n = made["blocked"]
        workflow.change_status(self.connection, n, "in_progress", who="test", reason="test")
        moved = tasks_screen.document(self.connection, now=NOW)
        click = "ELS.queue.listeners.click[0]({ target: { closest: s => s === 'tr[data-id]' ? { dataset: { id: 'ID' } } : null } }); await flush(); await flush();"
        primary = (f"(() => {{ const o = C.get('{n}'), on = REG.filter(c => c.on === o.type && (!c.when || c.when(o) === true));"
                   " return (on.find(c => c.primary && c.primary(o)) || {}).id; })()")
        out = self.run_view(f"open('system'); {click.replace('ID', str(n))} R.before = {primary}; {click.replace('ID', 'live:40')}"
                            f" QDOCS['/api/tasks'] = [200, {json.dumps(moved)}]; refresh(); await flush(); await flush(); await flush();"
                            f" R.after = {primary};", search="?show=open", answers=answers)
        self.assertEqual(out["R"]["before"], "item.status.ready")
        self.assertEqual(out["R"]["after"], "item.status.done")

    def test_a_next_row_moves_with_the_lanes_revision(self):
        out = self.run_view(f"open('system'); act('button[data-act]', {{ repo: {json.dumps(str(self.checkout))}, item: '12', act: 'up' }});"
                            " await flush(); await flush(); R.said = ELS['queue-said'].textContent;")
        revision = queue_screen.document(self.connection, now=NOW)["lanes"][0]["revision"]
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/queue/move", {"repo": str(self.checkout), "item": 12,
                                                                               "action": "up", "revision": revision}]])
        self.assertIn("sd:12 moved up.", out["R"]["said"])

    def confirmed(self, act, item, answer, **data):
        fields = json.dumps({"repo": str(self.checkout), "item": str(item), "act": act, **data})
        return self.run_view(f"window.shell.confirm = a => {{ OUT.asked = a; return Promise.resolve({json.dumps(answer)}); }};\n"
                             f"open('system'); act('button[data-act]', {fields}); await flush(); await flush();"
                             " R.said = ELS['queue-said'].textContent;")

    def test_cancel_release_and_approve_ask_first_and_a_kept_one_posts_nothing(self):
        claim = queue_screen.document(self.connection, now=NOW)["lanes"][0]["rows"][0]["claim"]
        for act, item, data, cli in (("cancel", 9, {}, f"sd-ship -C '{self.checkout}' lane cancel 9"),
                                     ("reclaim", 3, {"claim": claim}, f"sd-ship -C '{self.checkout}' lane cancel 3 --expected-claim 'claim-token-3:merge'"),
                                     ("approve", 5, {"head": "5" * 40}, f"sd-ship -C '{self.checkout}' lane retry 5 --expected-head {'5' * 40} --manual")):
            with self.subTest(act=act):
                kept = self.confirmed(act, item, False, **data)
                self.assertEqual((kept["asked"]["cli"], kept["posts"]), (cli, []))
                done = self.confirmed(act, item, True, **data)
                self.assertEqual(done["posts"][0][1]["action"], act)

    def test_retry_and_requeue_run_at_once_with_the_head_the_row_showed(self):
        for act, item, head in (("retry", 4, "4" * 40), ("requeue", 1, "1" * 40)):
            with self.subTest(act=act):
                out = self.confirmed(act, item, False, head=head)
                self.assertNotIn("asked", out)
                self.assertEqual((out["posts"][0][1]["action"], out["posts"][0][1]["head"]), (act, head))

    def test_a_refused_write_says_so_and_names_the_repo(self):
        out = self.run_view(f"open('system'); act('button[data-act]', {{ repo: {json.dumps(str(self.checkout))}, item: '12', act: 'up' }});"
                            " await flush(); await flush(); R.said = ELS['queue-said'].textContent;",
                            answers={"/api/queue/move": [400, {"error": "sd:3 is running; the queue changes only between items"}]})
        self.assertEqual(out["R"]["said"], "system: sd:12 not moved up: sd:3 is running; the queue changes only between items."
                                           " The queue is unchanged.")

    def test_a_stale_write_reads_the_lane_again_and_says_a_press_acts_on_that_read(self):
        # pack sd:7315: Queue and Today say a 409 the same way: the page already read the lane again.
        out = self.run_view(f"open('system'); act('button[data-act]', {{ repo: {json.dumps(str(self.checkout))}, item: '12', act: 'up' }});"
                            " await flush(); await flush(); R.said = ELS['queue-said'].textContent;",
                            answers={"/api/queue/move": [409, {"error": "The queue changed since the page read it. Read it"
                                                                        " again, then retry.", "reload": True}]})
        self.assertRegex(out["R"]["said"], r"^system: sd:12 not moved up: The queue changed since the page read it\. "
                                           r"Read again at \d\d:\d\d; a press now acts on that read\.$")
        self.assertEqual(out["gets"].count("/api/queue"), 2)

    def test_a_stale_write_whose_read_again_failed_claims_no_fresh_read(self):
        # A 409, then a lane read that failed, or one that left this repo out, claims no read (review of pack sd:7315).
        lost = queue_screen.document(self.connection, now=NOW)
        lost = {**lost, "lanes": [x for x in lost["lanes"] if x["path"] != str(self.checkout)],
                "problems": [{"repo": str(self.checkout), "error": "database is locked"}]}
        for name, after in (("offline", [502, {"error": "offline"}]), ("left out", [200, lost])):
            with self.subTest(name):
                out = self.run_view("const A2 = ANSWER; let moved = false;"
                                    f" ANSWER = (p, b) => {{ if (p === '/api/queue/move') moved = true;"
                                    f" return moved && p === '/api/queue' ? {json.dumps(after)} : A2(p, b); }};"
                                    f"open('system'); act('button[data-act]', {{ repo: {json.dumps(str(self.checkout))}, item: '12', act: 'up' }});"
                                    " await flush(); await flush(); await flush(); R.said = ELS['queue-said'].textContent;",
                                    answers={"/api/queue/move": [409, {"error": "The queue changed since the page read it. Read it"
                                                                                " again, then retry.", "reload": True}]})
                self.assertEqual(out["R"]["said"], "system: sd:12 not moved up: The queue changed since the page read it."
                                                   " It was not read again: Read again tries it.")

    def test_a_build_run_cancel_asks_first_and_send_to_builder_asks_for_the_finding(self):
        out = self.run_view("window.shell.confirm = a => { OUT.asked = a; return Promise.resolve(true); };"
                            "window.shell.ask = a => { OUT.ask = a; return Promise.resolve({ yes: true, values: { finding: 'Fix the label' } }); };"
                            "act('button[data-build]', { build: 'cancel', run: 'r1' }); await flush(); await flush();"
                            "act('button[data-build]', { build: 'send', run: 'r0' }); await flush(); await flush();",
                            answers={"/api/assignments/7/cancel": [200, {"ok": True}], "/api/queue/builds/retry": [200, {"run": "r0"}]})
        self.assertEqual(out["asked"]["cli"], f"sd assignments cancel 7 --if-revision {'a' * 64}")
        self.assertEqual([field["name"] for field in out["ask"]["fields"]], ["finding"])
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/assignments/7/cancel", {"revision": "a" * 64}],
                                                         ["/api/queue/builds/retry", {"assignment": 5, "revision": "b" * 64,
                                                                                      "finding": "Fix the label"}]])

    def test_a_row_links_its_screenshots_and_an_entry_offers_adversarial_review(self):
        self.shot_entries()
        out = self.run_view("open('system'); R.html = ELS.queue.html; const c = REG.find(x => x.id === 'entry.adversarial-review');"
                            f" R.off = c.when(C.get('entry:{self.checkout}:5'));")
        self.assertIn('screenshots <a href="/lane-shots/system/lane/logs/shots-5-20260906T090000/home-dark-1440.png" '
                      'target="_blank" rel="noopener">home-dark-1440</a>', out["R"]["html"])
        self.assertIn("screenshots not drawn: shots: no Chrome found", out["R"]["html"])
        self.assertEqual(out["R"]["off"], "the lane recorded no worktree for this entry")

    def test_a_source_that_was_not_read_is_named_under_the_table(self):
        out = self.run_view("R.note = ELS['queue-note'].textContent; R.hidden = ELS['queue-note'].hidden;",
                            answers={"/api/queue/runner": [502, {"error": "down"}]})
        self.assertFalse(out["R"]["hidden"])
        self.assertIn("▨ Runner assignments not read: down", out["R"]["note"])

    def test_the_view_reads_nothing_until_it_shows(self):
        out = self.run_page("R.html = ELS.queue.html || '';", after="")
        self.assertEqual(out["gets"], ["/api/now"])


class TheMovedAddresses(unittest.TestCase):
    """pack sd:7234: Today's old #view=queues and #view=motion open the Queue view; /queue redirects (TheMove)."""

    def test_an_old_queues_hash_lands_on_the_queue_view(self):
        block = re.search(r"^// moved:start\n(.*?)^// moved:end$", TODAY_JS, re.S | re.M).group(1)
        for was in ("queues", "motion"):
            script = ("var OUT = {}; var location = { hash: '#view=%s', search: '?row=x', replace: u => { OUT.replaced = u; } };\n"
                      "var history = { replaceState: (a, b, u) => { OUT.url = u; } };\n" % was
                      + block + "\nfunction run() { return JSON.stringify(OUT); }\n")
            result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True,
                                    timeout=60, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"url": "/today?row=x#view=queue"})


class TheRegistration(unittest.TestCase):
    """pack sd:7234: Today serves the lane's routes; no Queue page, rail entry or script is left."""

    def test_today_serves_the_queue_routes_and_no_queue_page_is_left(self):
        from test_v2_registry import api_paths, registry

        pages = {page.name: page for page in registry().PAGES}
        self.assertNotIn("queue", pages)
        self.assertEqual(tuple(api_paths(pages["today"])), ("/api/queue", "/api/queue/move", "/api/queue/builds",
                                                            "/api/queue/builds/retry", "/api/queue/builds/pickup", "/api/queue/runner",
                                                            "/api/queue/prs/check"))
        self.assertNotIn("Queue", v2.SECTIONS)
        self.assertNotIn("'Queue'", GROUP_BLOCK)
        for gone in ("queue.html", "static/queue.js", "static/queue.css", "pages/queue.py"):
            self.assertFalse((V2 / gone).exists(), gone)


if __name__ == "__main__":
    unittest.main()
