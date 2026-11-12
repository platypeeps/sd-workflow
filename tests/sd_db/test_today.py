"""`sd_db.today`: the sections `sd today`, `sd-status` and the dashboard show (sd:5098)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from sd_db import (
    connect,
    create_assignment,
    create_item,
    hub,
    reads,
    review,
    today,
    upsert_repo,
    workflow,
)
from sd_db.migrate import initialise
from sd_db.writes import add_note, transition

from .test_wire import Served

#: A fixed instant, so windows and estimates do not depend on when the suite runs.
NOW = "2026-09-06T12:00:00Z"
WHEN = "2026-09-04T09:00:00+00:00"
APP = "~/repos/example.test/app"
LIB = "~/repos/example.test/lib"


class TodayCase(unittest.TestCase):
    SHA = "a" * 40
    OTHER = "b" * 40

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "sd.db"
        initialise(path)
        self.connection = connect(path)
        self.addCleanup(self.connection.close)
        upsert_repo(self.connection, APP, remote="https://github.com/example-owner/app")
        upsert_repo(self.connection, LIB, remote="git@github.com:example-owner/lib.git")

    def item(self, title, **fields):
        return create_item(self.connection, kind=fields.pop("kind", "task"), title=title, **fields)

    def lane(self, repository, entry, **body):
        self.connection.execute(
            "INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
            (f"{today.LANE_KEY_PREFIX}{repository}:{entry}", WHEN, json.dumps({"id": entry, **body})))


class TheSections(TodayCase):
    def test_delivered_names_the_pr_and_merge_commit_of_the_last_day(self):
        shipped = self.item("Shipped", status="in_progress")
        transition(self.connection, shipped, "done", who="sd-ship", reason=f"delivered at {self.SHA} on origin/main")
        add_note(self.connection, shipped, "comment", f"Code delivery https://github.com/o/r/pull/7 at {self.SHA}")
        old = self.item("Old", status="in_progress")
        transition(self.connection, old, "done", who="sd-ship", reason=f"delivered at {self.OTHER} on origin/main")
        self.connection.execute("UPDATE note SET timestamp = '2020-01-01T00:00:00+00:00' WHERE item = ?", (old,))
        merged = self.item("Merged, still open")
        add_note(self.connection, merged, "comment", f"Code delivery https://github.com/o/r/pull/9 at {self.OTHER}")
        by_hand = self.item("Closed by hand", status="in_progress")
        transition(self.connection, by_hand, "done", who="operator", reason="test")
        self.assertEqual([(row["id"], row["pr"], row["commit"]) for row in today.delivered(self.connection)],
                         [(merged, 9, self.OTHER), (shipped, 7, self.SHA)])
        # A hand close is a count, not a row: a bulk close would fill the table.
        self.assertEqual([row["id"] for row in today.closed_by_hand(self.connection)], [by_hand])
        found = today.sections(self.connection)
        self.assertEqual((found["summary"]["delivered"], found["summary"]["closed_by_hand"]), (2, 1))

    def test_landing_is_each_items_newest_live_entry_with_its_place(self):
        first, second, third, finished, failed, delivered = (
            self.item(title) for title in ("First", "Second", "Third", "Finished", "Failed", "Delivered"))
        transition(self.connection, delivered, "done", who="operator", reason="test")
        self.lane("o/r", "e0", item=first, status="failed", position=1)
        self.lane("o/r", "e1", item=first, status="running", position=5, step="prepare")
        self.lane("o/r", "e2", item=third, status="pending", position=7)
        self.lane("o/r", "e3", item=second, status="pending", position=6)
        self.lane("o/r", "e4", item=finished, status="merged", position=2)
        self.lane("o/s", "e5", item=failed, status="failed", position=3, step="prepare")
        self.lane("o/s", "e6", item=delivered, status="failed", position=4)
        self.lane("o/s", "e7", item=second, status="cancelled", position=8)
        found = today.landing(self.connection)
        self.assertEqual([(row["id"], row["repository"], row["status"], row["place"]) for row in found["entries"]],
                         [(first, "o/r", "running", 1), (second, "o/r", "pending", 2), (third, "o/r", "pending", 3),
                          (failed, "o/s", "failed", None)])
        self.assertIsNone(found["gate"])

    def test_landing_gives_exact_times_and_no_estimate(self):
        """The lane records no ETA: `since` is when the entry started, else when the hub queued it."""
        running, waiting = self.item("Running", repo=APP), self.item("Waiting", repo=APP)
        self.lane("example-owner/app", "r", item=running, status="running", position=10,
                  started_at="2026-09-06T11:55:00Z")
        self.lane("example-owner/app", "w", item=waiting, status="pending", position=11)
        entries = today.landing(self.connection)["entries"]
        self.assertEqual([(row["place"], row["since"], row["repo"]) for row in entries],
                         [(1, "2026-09-06T11:55:00Z", APP), (2, WHEN, APP)])
        self.assertNotIn("eta", entries[0])

    def test_landing_carries_the_gate_or_why_it_is_unknown(self):
        snapshot = {"holders": [{"slot": 1, "pid": 7, "label": "tests", "since": WHEN, "cwd": "/tmp"}],
                    "waiters": [{"place": 1, "pid": 8, "label": "lane", "since": WHEN, "cwd": "/tmp"}]}
        self.assertEqual(today.landing(self.connection, gate=snapshot)["gate"],
                         {"holders": [{"slot": 1, "label": "tests", "since": WHEN}],
                          "waiters": [{"place": 1, "label": "lane", "since": WHEN}], "error": None})
        self.assertEqual(today.landing(self.connection, gate="permission denied")["gate"]["error"], "permission denied")

    def test_queues_is_ready_items_builds_and_the_lane_in_one_list(self):
        """Pack sd:6946: the operator asked for these three lists 15+ times in one session."""
        low, high = self.item("Low", repo=APP, status="ready", priority=3), self.item("High", repo=APP, status="ready", priority=1)
        queued, stuck = self.item("Queued", repo=APP, status="ready"), self.item("Stuck", repo=LIB, status="blocked")
        for item, status in ((queued, "queued"), (stuck, "blocked")):
            ident = create_assignment(self.connection, item=item, role="author", status=status)
            self.connection.execute("UPDATE assignment SET scope = 'build', queued_at = ? WHERE id = ?", (WHEN, ident))
        landing = self.item("Landing", repo=APP)
        self.lane("example-owner/app", "e1", item=landing, status="failed", position=1, step="prepare")
        rows = today.queues(self.connection, now=NOW)
        self.assertEqual([(row["queue"], row["id"], row["status"], row["detail"]) for row in rows],
                         [("ready", high, "ready", "P1"), ("ready", low, "ready", "P3"),
                          ("build", queued, "queued", "-"), ("build", stuck, "blocked", "-"),
                          ("lane", landing, "failed", "prepare")])
        self.assertEqual([row["id"] for row in today.queues(self.connection, repos=[LIB], now=NOW)], [stuck])
        self.assertEqual(today.sections(self.connection, now=NOW)["summary"]["queues"], 5)

    def test_a_failure_is_due_for_a_push_once_and_a_success_never(self):
        """Pack sd:6946: a push only when a lane entry or a build fails or blocks, once per entry."""
        failed, merged = self.item("Failed", repo=APP), self.item("Merged", repo=APP)
        self.lane("example-owner/app", "e1", item=failed, status="failed", position=1, step="prepare")
        self.lane("example-owner/app", "e2", item=merged, status="merged", position=2)
        due = today.push_due(self.connection, now=NOW)
        self.assertEqual([(row["key"], row["title"]) for row in due],
                         [("lane:example-owner/app:e1", f"Lane failed: sd:{failed}")])
        self.assertIn("step prepare", due[0]["body"])
        self.connection.execute("INSERT INTO state(kind, key, timestamp) VALUES ('watermark', ?, ?)",
                                (today.PUSHED + due[0]["key"], WHEN))
        self.assertEqual(today.push_due(self.connection, now=NOW), [])

    def test_a_failed_health_check_is_due_for_a_push(self):
        """Pack sd:6097: a health check failed after an auto-pickup merge is a critical push, once."""
        filed = self.item(today.HEALTH + "build/sd-1", repo=APP)
        due = [row for row in today.push_due(self.connection, now=NOW) if row["key"].startswith("health:")]
        self.assertEqual([(row["key"], row["body"]) for row in due], [(f"health:{filed}", today.HEALTH + "build/sd-1")])

    def test_planned_is_open_items_due_after_today_soonest_first(self):
        later = self.item("Later", due="2026-09-30")
        sooner = self.item("Sooner", due="2026-09-07")
        self.item("Due today, so in Work", due="2026-09-06")
        self.item("Done", due="2026-09-08", status="done")
        self.item("Undated")
        self.assertEqual([row["id"] for row in today.planned(self.connection, now=NOW)], [sooner, later])

    def test_unscheduled_leaves_out_dated_laned_assigned_blocked_and_idea_items(self):
        low = self.item("Low", priority=3)
        high = self.item("High", priority=1)
        unranked = self.item("Unranked")
        self.item("Dated", due="2026-12-01")
        laned = self.item("Laned")
        self.lane("o/r", "e1", item=laned, status="pending", position=1)
        assigned = self.item("Assigned")
        create_assignment(self.connection, role="builder", status="running", item=assigned)
        finished = self.item("Assignment ended")
        create_assignment(self.connection, role="builder", status="done", item=finished)
        self.item("Blocked", status="blocked")
        self.item("An idea", kind="idea")
        self.item("Done", status="done")
        self.assertEqual([row["id"] for row in today.unscheduled(self.connection)], [high, low, unranked, finished])

    def test_suggested_is_ready_then_planned_then_unscheduled_once_each(self):
        """Pack sd:7314: what sd-next offers next, in its order. An item counts once, under its first reason; one in
        the lane or held by a build is the Queue view's merging, next, blocked or working row, so it is left out."""
        low, high = self.item("Low ready", repo=APP, status="ready", priority=3), self.item("High ready", repo=APP, status="ready", priority=1)
        later, sooner = self.item("Later", repo=APP, due="2026-09-30"), self.item("Sooner", repo=LIB, due="2026-09-07")
        dated_ready = self.item("Ready and dated", repo=APP, status="ready", due="2026-09-08")
        loose, urgent = self.item("Loose", repo=LIB, priority=4), self.item("Urgent", repo=APP, priority=0)
        laned = self.item("Laned and ready", repo=APP, status="ready")
        self.lane("example-owner/app", "e1", item=laned, status="failed", position=1)
        planned_laned = self.item("Laned and dated", repo=APP, due="2026-09-10")
        self.lane("example-owner/app", "e2", item=planned_laned, status="pending", position=2)
        building = self.item("Building", repo=APP, status="ready")
        ident = create_assignment(self.connection, item=building, role="author", status="queued")
        self.connection.execute("UPDATE assignment SET scope = 'build', queued_at = ? WHERE id = ?", (WHEN, ident))
        rows = today.suggested(self.connection, now=NOW)
        self.assertEqual([(row["id"], row["why"]) for row in rows],
                         [(high, "ready"), (low, "ready"), (dated_ready, "ready"), (sooner, "planned"), (later, "planned"),
                          (urgent, "unscheduled"), (loose, "unscheduled")])
        self.assertEqual({k: rows[3][k] for k in ("repo", "status", "due", "priority")},
                         {"repo": LIB, "status": "planning", "due": "2026-09-07", "priority": None})
        self.assertEqual(rows[5]["priority"], 0)
        queued = today.queues(self.connection, now=NOW)
        self.assertEqual(today.suggested(self.connection, now=NOW, queued=queued), rows, "a read's queues are reused")

    def test_suggested_offers_open_items_due_today_or_overdue_after_ready(self):
        """Pack sd:8238: an open item due today or before is suggested, oldest due first, after the ready ones and
        before the planned ones. An idea or a report is not; neither is a done item."""
        ready = self.item("Ready", repo=APP, status="ready")
        overdue = self.item("Overdue", repo=APP, due="2026-09-01")
        due_today = self.item("Due today", repo=LIB, due="2026-09-06", status="in_progress")
        later = self.item("Later", repo=APP, due="2026-09-30")
        self.item("Done", repo=APP, due="2026-09-01", status="done")
        self.item("An idea", repo=APP, kind="idea", due="2026-09-01")
        self.item("Alert", repo=APP, kind="report", due="2026-09-01")
        rows = today.suggested(self.connection, now=NOW)
        self.assertEqual([(row["id"], row["why"]) for row in rows],
                         [(ready, "ready"), (overdue, "due"), (due_today, "due"), (later, "planned")])
        self.assertEqual({k: rows[1][k] for k in ("repo", "status", "due")},
                         {"repo": APP, "status": "planning", "due": "2026-09-01"})

    def test_suggested_leaves_out_ready_ideas(self):
        """Sd:8281: a ready vault reference note (kind idea) is not work; a ready task still is."""
        task = self.item("Ready task", repo=APP, status="ready")
        self.item("Ready idea", repo=APP, status="ready", kind="idea")
        self.assertEqual([row["id"] for row in today.suggested(self.connection, now=NOW)], [task])

    def test_suggested_rows_carry_their_kind(self):
        """Pack sd:8532: the Queue's Build reads the kind to grey out one build._item would refuse."""
        followup = self.item("Ready followup", repo=APP, status="ready", kind="followup")
        planned = self.item("Planned", repo=APP, due="2026-09-30", kind="task")
        loose = self.item("Loose", repo=LIB, kind="followup")
        rows = {row["id"]: row["kind"] for row in today.suggested(self.connection, now=NOW)}
        self.assertEqual(rows, {followup: "followup", planned: "task", loose: "followup"})

    def test_blocked_says_why_from_the_status_change_or_a_waiting_note(self):
        vendor = self.item("Vendor")
        transition(self.connection, vendor, "blocked", who="operator", reason="Waits on the vendor\nmore detail")
        bare = self.item("Bare", status="blocked")
        waiting = self.item("Waiting")
        add_note(self.connection, waiting, "comment", "Waiting on Alex for the keys")
        chatty = self.item("Chatty")
        add_note(self.connection, chatty, "comment", "Waiting on Alex was yesterday")
        add_note(self.connection, chatty, "comment", "Alex answered")
        self.assertEqual([(row["id"], row["why"]) for row in today.blocked(self.connection)],
                         [(vendor, "Waits on the vendor"), (bare, ""), (waiting, "Waiting on Alex for the keys")])

    def test_planned_and_blocked_leave_out_the_idea_kinds(self):
        """pack sd:6743: ideas show in the Writing and Vault views only, never in a work count."""
        work = self.item("Work", due="2026-09-30")
        stuck = self.item("Stuck", status="blocked")
        for kind in ("idea", "work-idea", "personal-idea"):
            self.item(f"A dated {kind}", kind=kind, due="2026-09-30")
            self.item(f"A blocked {kind}", kind=kind, status="blocked")
        self.assertEqual([row["id"] for row in today.planned(self.connection, now=NOW)], [work])
        self.assertEqual([row["id"] for row in today.blocked(self.connection)], [stuck])

    def test_decisions_and_followups_leave_out_plain_questions_and_reports_own(self):
        asked = self.item("Asked")
        decision = add_note(self.connection, asked, "question", "Which store?\nOption: Pack (Recommended)\nOption: Rows")
        # A plain question from before pack sd:6708, which `add_note` now refuses.
        self.connection.execute("INSERT INTO note (item, timestamp, kind, body) VALUES (?, ?, 'question', ?)",
                                (asked, "2026-10-01T00:00:00+00:00", "A plain question"))
        chase = add_note(self.connection, asked, "followup", "Chase the vendor")
        report = self.item("Report", kind="report", fields={"attention": "two stale rows"})
        add_note(self.connection, report, "followup", "The report's own")
        self.assertEqual([(row["id"], row["note"], row["title"]) for row in today.decisions(self.connection)],
                         [(asked, decision, "Which store?")])
        self.assertEqual([(row["id"], row["note"]) for row in today.followups(self.connection)], [(asked, chase)])
        self.assertEqual([(row["id"], row["why"]) for row in today.attention(self.connection)],
                         [(report, "two stale rows")])

    def test_closing_an_item_keeps_its_unanswered_question_in_decisions(self):
        """Pack sd:6329: a close answers no question; the followups it owed go."""
        asked = self.item("Asked")
        decision = add_note(self.connection, asked, "question", "Move all 45?\nOption: Approve all (Recommended)\nOption: Videos only")
        add_note(self.connection, asked, "followup", "Chase the vendor")
        workflow.change_status(self.connection, asked, "done", who="operator", reason="landed")
        self.assertEqual([(row["id"], row["note"], row["status"]) for row in today.decisions(self.connection)],
                         [(asked, decision, "done")])
        self.assertEqual(today.followups(self.connection), [])

    def batch(self, name, questions=1, repo=APP):
        """A review batch filed by the review store itself, so the marker is the one it writes."""
        return review.file(self.connection, {
            "schema": 1, "queue": "labels", "batch": name, "title": f"Label {name}", "repo": repo,
            "source": "example.test@1a2b3c4", "mode": "open",
            "questions": [{"subject": f"example.test/{name}/{number}", "text": "Which tier fits?",
                           "options": ["cheap", "deep"]} for number in range(questions)]}, who="tester")["item"]

    def test_decisions_leave_review_batch_questions_to_the_review_page_as_a_count(self):
        """sd:5130: `sd-next` walks the Decisions rows; a batch's questions wait on the Review page."""
        batch = self.batch("B1", questions=2)
        asked = self.item("Asked")
        decision = add_note(self.connection, asked, "question", "Which store?\nOption: Pack (Recommended)\nOption: Rows")
        # Older state: a ref task that is no batch, and an approval's task, keep their questions as rows.
        bare = self.item("Bare ref", source="task-ref")
        unbatched = add_note(self.connection, bare, "question", "Keep it?\nOption: Yes (Recommended)\nOption: No")
        approved = self.item("Approved", source="task-ref", external_id="review-approved:1")
        followed = add_note(self.connection, approved, "question", "Ship it?\nOption: Yes (Recommended)\nOption: No")
        self.assertEqual([row["note"] for row in today.decisions(self.connection)], [decision, unbatched, followed])
        self.assertEqual(today.review_questions(self.connection), 2)
        self.assertEqual(today.sections(self.connection, now=NOW)["summary"]["review_questions"], 2)
        self.assertEqual(today.review_questions(self.connection, repos=[LIB]), 0)
        # A label resolves the question, so the count falls; the batch never becomes a row.
        state = review.read(self.connection, batch)
        review.rule(self.connection, batch, state["questions"][0]["note"], "deep",
                    expected_revision=workflow.item_state(self.connection, batch)["revision"], who="tester")
        self.assertEqual(today.review_questions(self.connection), 1)
        self.assertNotIn(batch, [row["id"] for row in today.decisions(self.connection)])

    def test_a_batch_larger_than_the_note_limit_leaves_the_decisions_whole(self):
        """The batch is left out in the query: 250 older batch questions do not fill `open_notes`' 200."""
        self.batch("Big", questions=250)
        asked = self.item("Asked")
        decision = add_note(self.connection, asked, "question", "Which store?\nOption: Pack (Recommended)\nOption: Rows")
        self.assertEqual([row["note"] for row in today.decisions(self.connection)], [decision])
        self.assertEqual(today.review_questions(self.connection), 250)

    def test_the_batch_marker_is_the_one_the_review_store_files(self):
        self.assertIn(f"item.source IS '{review.REF_SOURCE}'", reads.REVIEW_BATCH)
        self.assertIn("substr(item.external_id, 1, 7) IS 'review:'", reads.REVIEW_BATCH)

    def grill(self, ref="grill:2026-09-07", repo=APP, questions=2):
        """A weekly grill as the KISS audit files it: `sd task add --ref grill:<date>`, then question notes."""
        item = self.item("Weekly grill", repo=repo, source="task-ref", external_id=ref)
        return item, [add_note(self.connection, item, "question", f"Keep mechanism {n}?\nOption: Keep (Recommended)\nOption: Drop")
                      for n in range(questions)]

    def test_a_grills_questions_are_one_ready_line_not_decisions(self):
        """pack sd:6022: the audit's seed questions wait for the operator to say 'grill', not in every walk."""
        grilled, (first, _) = self.grill()
        asked = self.item("Asked")
        decision = add_note(self.connection, asked, "question", "Which store?\nOption: Pack (Recommended)\nOption: Rows")
        # Older state: a ref task under another prefix keeps its questions as Decisions rows.
        other = self.item("Other ref", source="task-ref", external_id="grilled:1")
        kept = add_note(self.connection, other, "question", "Keep it?\nOption: Yes (Recommended)\nOption: No")
        self.assertEqual([row["note"] for row in today.decisions(self.connection)], [decision, kept])
        self.assertEqual([(row["id"], row["questions"]) for row in today.grill(self.connection)], [(grilled, 2)])
        found = today.sections(self.connection, now=NOW)
        self.assertEqual((found["summary"]["grill"], [row["id"] for row in found["grill"]]), (2, [grilled]))
        self.assertEqual(today.grill(self.connection, repos=[LIB]), [])
        # A ruling resolves a question, so the count falls; the last one ends the line.
        workflow.resolve_item_note(self.connection, first, who="tester")
        self.assertEqual([row["questions"] for row in today.grill(self.connection)], [1])
        transition(self.connection, grilled, "done", who="operator", reason="test")
        self.assertEqual(today.grill(self.connection), [])

    def test_a_grill_with_no_open_question_is_not_ready(self):
        self.grill(questions=0)
        self.assertEqual(today.grill(self.connection), [])
        self.assertEqual(today.sections(self.connection, now=NOW)["summary"]["grill"], 0)

    def test_sections_come_in_one_order_with_their_counts(self):
        self.item("Soon", due="2026-09-08")
        self.item("Later", due="2026-10-30")
        found = today.sections(self.connection, now=NOW)
        self.assertEqual(list(found), ["summary", *today.ORDER, "closed_by_hand", "grill"])
        self.assertEqual((found["summary"]["planned"], found["summary"]["planned_this_week"]), (2, 1))

    def test_the_owning_assignment_states_are_the_workflows_and_a_teammates(self):
        """Unscheduled skips an item the runner or a teammate run owns (pack sd:7221)."""
        self.assertEqual(today.OWNING, (*workflow.ACTIVE_ASSIGNMENTS, "in_session"))


class TheOperations(TodayCase):
    """pack sd:5432: the open report lines that name this machine, every repository's, and the gate."""

    def report(self, job, basis, *, repo=None, **fields):
        return self.item(f"{job}: needs attention", kind="report", repo=repo, source="cron-report",
                         external_id=f"{job}:20261020T101505Z-1",
                         fields={"attention": fields.pop("attention", True), "report": {"attention_basis": basis},
                                 **fields})

    def test_only_the_lines_naming_the_host_are_rows_and_the_gate_rides_along(self):
        health = self.report("health-check-nightly", "\n".join((
            "health check on alpha: launchd: com.example.test.job reports a failed run",
            "health check on beta: disk: under 10% free",
            "health check on alphabet: not this machine")), repo=APP)
        store = self.report("maintenance-nightly", "\n".join((
            "maintenance on alpha: App Store updates pending:",
            "maintenance on alpha: App Store updates pending: 1 Example (1.0 -> 1.1)")), repo=LIB)
        exited = self.report("secret-scan-weekly", "job exited 2 on alpha")
        folded = self.report("brew-doctor-nightly", "brew doctor on alpha: first set",
                             repeats={"basis": "brew doctor on alpha: current set"})
        self.report("quiet-nightly", "quiet on alpha: x", attention=False)
        transition(self.connection, self.report("done-nightly", "done on alpha: x"), "done", who="operator",
                   reason="acknowledged")
        unread = self.report("broken-nightly", "broken on beta: x")
        self.connection.execute("UPDATE item SET fields = 'not json' WHERE id = ?", (unread,))
        self.item("No basis", kind="report", fields={"attention": "look"})
        snapshot = {"slots": 2, "load": [1.5, 1.0, 0.5], "load_max": 40.0,
                    "holders": [{"slot": 1, "pid": 7, "label": "tests", "since": WHEN, "cwd": "/tmp"}], "waiters": []}
        found = today.operations(self.connection, host="alpha", gate=snapshot)
        self.assertEqual(sorted((row["id"], row["job"], row["finding"]) for row in found["findings"]), sorted([
            (health, "health-check-nightly", "launchd: com.example.test.job reports a failed run"),
            (store, "maintenance-nightly", "App Store updates pending: 1 Example (1.0 -> 1.1)"),
            (exited, "secret-scan-weekly", "job exited 2"),
            (folded, "brew-doctor-nightly", "current set"),
            (unread, "broken-nightly", today.FIELDS_UNREAD),
        ]))
        self.assertEqual(found["gate"], {"holders": [{"slot": 1, "label": "tests", "since": WHEN}], "waiters": [],
                                         "error": None, "slots": 2, "load": [1.5, 1.0, 0.5], "load_max": 40.0})
        self.assertEqual(today.operations(self.connection, host="beta", gate="slots unreadable")["gate"],
                         {"holders": [], "waiters": [], "error": "slots unreadable"})
        # pack sd:6488: the filter keeps the findings of its repositories' reports; the gate stays whole.
        with mock.patch.object(today, "this_host", return_value="alpha"):
            mine = today.sections(self.connection, repos=[LIB], now=NOW, gate=snapshot)
            loose = today.sections(self.connection, repos=[None], now=NOW, gate=snapshot)
        self.assertEqual([row["id"] for row in mine["operations"]["findings"]], [store])
        self.assertEqual((mine["operations"]["gate"], mine["summary"]["operations"]), (found["gate"], 1))
        self.assertEqual(sorted(row["id"] for row in loose["operations"]["findings"]), sorted([exited, folded, unread]))

    def test_a_failed_runs_finding_keeps_its_exit_code(self):
        """pack sd:7399: `ingest_log` writes `job exited N on <host>`, graded or not, with the job's lines."""
        bare = self.report("backup-nightly", "job exited 3 on alpha: backup on alpha: the NAS is gone")
        graded = self.report("sync-nightly", "job exited 4 on alpha (data safe, one step failed: push): sync lagged")
        self.report("other-nightly", "job exited 5 on alphabet: not this machine")
        found = today.operations(self.connection, host="alpha", gate=None)["findings"]
        self.assertEqual(sorted((row["id"], row["finding"]) for row in found), sorted([
            (bare, "job exited 3: backup on alpha: the NAS is gone"),
            (graded, "job exited 4 (data safe, one step failed: push): sync lagged"),
        ]))


class TheHubRequests(TodayCase):
    """pack sd:7358 (system sd:7324 S3): an open `hub:` item is Attention on the fence holder, not elsewhere."""

    def setUp(self):
        super().setUp()
        self.state = "holder"
        self.asked = 0
        def role(connection, name=None):
            self.asked += 1
            if self.state == "fails":
                return hub.Role("unknown", "hub.example.test", "Tailscale does not name this node")
            return hub.Role(self.state, None if self.state == "off" else "hub.example.test", "test")
        for patch in (mock.patch.object(hub, "role", side_effect=role),
                      mock.patch.object(hub, "read", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    def request(self, slug="sat-mail-1", **fields):
        return self.item(fields.pop("title", f"Read the {slug} thread"), kind="followup", source="task-ref",
                         external_id=f"hub:{slug}", **{"repo": APP, **fields})

    def listed(self, **arguments):
        return [row["id"] for row in today.attention(self.connection, **arguments)]

    def test_the_hub_lists_an_open_hub_request_under_attention(self):
        asked = self.request()
        (row,) = today.attention(self.connection)
        self.assertEqual((row["id"], row["repo"], row["status"]), (asked, APP, "planning"))
        self.assertIn("hub request", row["why"])
        found = today.sections(self.connection, now=NOW)
        self.assertEqual(([row["id"] for row in found["attention"]], found["summary"]["attention"]), ([asked], 1))
        # A request names no machine's finding: Operations stays the reports'.
        self.assertEqual(today.operations(self.connection, host="alpha", gate=None)["findings"], [])

    def test_a_fence_that_is_off_lists_it_as_the_one_machine_is_the_hub(self):
        self.state = "off"
        asked = self.request()
        self.assertEqual(self.listed(), [asked])

    def test_a_satellite_lists_none(self):
        self.request()
        self.state = "elsewhere"
        self.assertEqual(self.listed(), [])
        # `sd-db fence`'s first answer: a home with `hub.json` is a satellite, whatever the setting says.
        self.state = "holder"
        with mock.patch.object(hub, "read", return_value=hub.Hub("hub.example.test", 8765, Path("hub.json"))):
            self.assertEqual(self.listed(), [])
        with mock.patch.object(hub, "read", side_effect=hub.HubConfigError("hub.json is not a hub config")):
            (row,) = today.attention(self.connection)
        self.assertIn("hub.json is not a hub config", row["why"])

    def test_an_unknown_role_lists_it_and_names_the_unknown(self):
        """A failed fence read is unknown, not a satellite: the row stays and says why."""
        self.state = "fails"
        asked = self.request()
        (row,) = today.attention(self.connection)
        self.assertEqual(row["id"], asked)
        self.assertIn("hub role unknown: Tailscale does not name this node", row["why"])

    def test_only_the_exact_hub_ref_counts(self):
        """The design's risk: a mistyped ref hides a request, so the test pins the source."""
        asked = self.request()
        for ref in ("hubs:sat-mail-2", "Hub:sat-mail-3", "job:hub:sat-mail-4", "hub-sat-mail-5"):
            self.item(f"Not {ref}", kind="followup", source="task-ref", external_id=ref)
        self.item("Other source", kind="followup", source="cron-report", external_id="hub:sat-mail-6")
        self.assertEqual(self.listed(), [asked])

    def test_a_claimed_request_older_than_a_day_still_lists(self):
        """Failure table, S3 work: the session stops after the claim; a later session takes it over."""
        asked = self.request(created_at="2026-09-01T09:00:00+00:00")
        transition(self.connection, asked, "in_progress", who="hub-session", reason="claimed")
        (row,) = today.attention(self.connection)
        self.assertEqual((row["id"], row["status"], row["at"]), (asked, "in_progress", "2026-09-01T09:00:00+00:00"))

    def test_an_answered_request_lists_until_it_is_closed(self):
        """Failure table, S3 close: the answer lands but `done` does not; the next session closes it."""
        asked = self.request()
        transition(self.connection, asked, "in_progress", who="hub-session", reason="claimed")
        add_note(self.connection, asked, "comment", "Answer: the thread says ship on Friday")
        self.assertEqual(self.listed(), [asked])
        transition(self.connection, asked, "done", who="hub-session", reason="answered in a note")
        self.assertEqual(self.listed(), [])

    def test_reports_and_requests_list_newest_first_and_follow_the_filter(self):
        report = self.item("A report", kind="report", repo=APP, fields={"attention": "look"},
                           created_at="2026-09-02T09:00:00+00:00")
        older = self.request("sat-mail-old", created_at="2026-09-01T09:00:00+00:00")
        newer = self.request("sat-mail-new", created_at="2026-09-03T09:00:00+00:00")
        other = self.request("sat-drive-lib", repo=LIB)
        self.assertEqual(self.listed(), [other, newer, report, older])
        self.assertEqual(self.listed(repos=[APP]), [newer, report, older])

    def test_no_fence_read_without_an_open_request(self):
        """`sd today` asks Tailscale nothing on a database with no open `hub:` item."""
        done = self.request()
        transition(self.connection, done, "done", who="hub-session", reason="answered")
        self.item("A report", kind="report", fields={"attention": "look"})
        today.sections(self.connection, now=NOW)
        self.assertEqual(self.asked, 0)

    def test_a_followup_note_on_a_request_stays_a_followup(self):
        """Followups leaves out a report's own followups only; a request is no report."""
        asked = self.request()
        add_note(self.connection, asked, "followup", "Ask the hub again on Friday")
        self.assertEqual([row["id"] for row in today.followups(self.connection)], [asked])


class TheRepositoryFilter(TodayCase):
    """sd:5098: `sd-status` reads the same sections for its own repository and its companions."""

    def test_every_section_keeps_only_the_named_repositories(self):
        mine, theirs, loose = self.item("Mine", repo=APP), self.item("Theirs", repo=LIB), self.item("Loose")
        for item in (mine, theirs, loose):
            add_note(self.connection, item, "followup", f"Chase {item}")
            add_note(self.connection, item, "question", f"Pick {item}\nOption: A (Recommended)\nOption: B")
        self.lane("example-owner/app", "a", item=mine, status="pending", position=1)
        self.lane("example-owner/lib", "b", item=theirs, status="pending", position=2)
        planned = [self.item(f"Due {repo}", repo=repo, due="2026-09-09") for repo in (APP, LIB)]
        stuck = [self.item(f"Stuck {repo}", repo=repo, status="blocked") for repo in (APP, LIB)]
        free = [self.item(f"Free {repo}", repo=repo) for repo in (APP, LIB)]
        found = today.sections(self.connection, repos=[APP], now=NOW)
        self.assertEqual([row["id"] for row in found["followups"]], [mine])
        self.assertEqual([row["id"] for row in found["decisions"]], [mine])
        self.assertEqual([(row["id"], row["repo"]) for row in found["landing"]["entries"]], [(mine, APP)])
        self.assertEqual([row["id"] for row in found["planned"]], [planned[0]])
        self.assertEqual([row["id"] for row in found["blocked"]], [stuck[0]])
        self.assertEqual([row["id"] for row in found["unscheduled"]], [free[0]])
        both = today.sections(self.connection, repos=[APP, LIB], now=NOW)
        self.assertEqual({row["repo"] for row in both["unscheduled"]}, {APP, LIB})
        self.assertEqual(len(today.sections(self.connection, now=NOW)["followups"]), 3)

    def test_a_repositorys_rows_list_past_the_fleets_note_cap(self):
        """pack sd:7400: the filter runs before `open_notes`' 200-row cap, so 200 older rows elsewhere hide none."""
        busy = self.item("Busy", repo=APP)
        for n in range(200):
            add_note(self.connection, busy, "followup", f"Chase {n}")
            add_note(self.connection, busy, "question", f"Pick {n}\nOption: A (Recommended)\nOption: B")
        mine = self.item("Mine", repo=LIB)
        chase = add_note(self.connection, mine, "followup", "Chase the vendor")
        decision = add_note(self.connection, mine, "question", "Which store?\nOption: Pack (Recommended)\nOption: Rows")
        self.assertEqual([row["note"] for row in today.followups(self.connection, repos=[LIB])], [chase])
        self.assertEqual([row["note"] for row in today.decisions(self.connection, repos=[LIB])], [decision])
        self.assertEqual(today.sections(self.connection, repos=[LIB], now=NOW)["summary"]["decisions"], 1)

    def test_followups_list_past_a_cap_of_a_reports_own(self):
        """pack sd:7415: a report's own followups are no rows, so 200 of them fill no cap that hides one."""
        report = self.item("Failing job", repo=APP, kind="report", fields={"attention": "look"})
        for n in range(200):
            add_note(self.connection, report, "followup", f"Chase {n}")
        mine = self.item("Mine", repo=APP)
        chase = add_note(self.connection, mine, "followup", "Chase the vendor")
        self.assertEqual([row["note"] for row in today.followups(self.connection)], [chase])
        self.assertEqual([row["note"] for row in today.followups(self.connection, repos=[APP])], [chase])

    def test_decisions_list_past_a_cap_of_plain_questions(self):
        """pack sd:7400: questions Decisions leaves out fill no cap that hides one it lists, as in #1663."""
        busy = self.item("Busy", repo=APP)
        self.connection.executemany("INSERT INTO note (item, timestamp, kind, body) VALUES (?, ?, 'question', ?)",
                                    [(busy, f"2026-09-01T00:00:{n % 60:02d}+00:00", f"Plain {n}") for n in range(200)])
        asked = self.item("Asked", repo=APP)
        decision = add_note(self.connection, asked, "question", "Which store?\nOption: Pack (Recommended)\nOption: Rows")
        self.assertEqual([row["note"] for row in today.decisions(self.connection)], [decision])

    def test_delivered_and_attention_follow_the_filter(self):
        for repo in (APP, LIB):
            shipped = self.item(f"Shipped {repo}", repo=repo, status="in_progress")
            transition(self.connection, shipped, "done", who="sd-ship", reason=f"delivered at {self.SHA} on origin/main")
            self.item(f"Report {repo}", repo=repo, kind="report", fields={"attention": "look"})
        self.assertEqual([row["repo"] for row in today.delivered(self.connection, repos=[LIB])], [LIB])
        self.assertEqual([row["repo"] for row in today.attention(self.connection, repos=[LIB])], [LIB])

    def test_none_keeps_the_rows_of_no_repository(self):
        """pack sd:6488: `sd today --only fleet` passes None, the key of an item in no repository."""
        for repo in (APP, None):
            item = self.item(f"Open {repo}", repo=repo)
            add_note(self.connection, item, "followup", f"Chase {item}")
            add_note(self.connection, item, "question", f"Pick {item}\nOption: A (Recommended)\nOption: B")
            self.item(f"Report {repo}", repo=repo, kind="report", fields={"attention": "look"})
            self.item(f"Due {repo}", repo=repo, due="2026-09-09")
            self.item(f"Stuck {repo}", repo=repo, status="blocked")
            # A lane entry of a repository no row registers has no key: it is no fleet row.
            self.lane("example-owner/unregistered", f"e{item}", item=item, status="pending", position=item)
        found = today.sections(self.connection, repos=[None], now=NOW)
        for key in ("attention", "decisions", "followups", "planned", "blocked", "unscheduled"):
            with self.subTest(section=key):
                self.assertTrue(found[key])
                self.assertEqual({row["repo"] for row in found[key]}, {None})
        self.assertEqual(found["landing"]["entries"], [])
        both = today.sections(self.connection, repos=[APP, None], now=NOW)
        self.assertEqual({row["repo"] for row in both["followups"]}, {APP, None})
        self.assertEqual(both["summary"]["followups"], 2)


if __name__ == "__main__":
    unittest.main()


class TheSatellite(unittest.TestCase):
    """pack sd:7357: a satellite's `sd today` reads the hub over the wire and lists its lanes' failure followups."""

    def test_a_satellite_lists_the_open_followup_of_its_own_lane(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        served = Served(Path(tmp.name))
        self.addCleanup(served.stop)
        hub = connect(served.database)
        try:
            upsert_repo(hub, APP, remote="https://github.com/example-owner/app")
            upsert_repo(hub, LIB, remote="https://github.com/example-owner/lib")
            hub.execute("UPDATE repo SET lane_host = 'build-2' WHERE path = ?", (APP,))
            mine = create_item(hub, kind="task", title="Mine", repo=APP)
            theirs = create_item(hub, kind="task", title="Hub's", repo=LIB)
            failed = add_note(hub, mine, "followup", "Lane failed in prepare at aaaaaaaaaaaa: a blocking finding")
            add_note(hub, theirs, "followup", "Lane failed in merge at bbbbbbbbbbbb: refused")
            closed = add_note(hub, mine, "followup", "Lane failed in prepare at cccccccccccc: an older failure")
            hub.execute("UPDATE note SET resolved_at = ? WHERE id = ?", (WHEN, closed))
        finally:
            hub.close()
        satellite: Any = served.connect()  # `sd today` takes either kind of connection
        self.addCleanup(satellite.close)
        own = today.sections(satellite, repos=[APP], now=NOW)
        self.assertEqual([(row["id"], row["note"], row["title"]) for row in own["followups"]],
                         [(mine, failed, "Lane failed in prepare at aaaaaaaaaaaa: a blocking finding")])
        self.assertEqual(own["summary"]["followups"], 1)
        # `sd today` with no `--only` reads every repository: the hub's lane and the satellite's alike.
        self.assertEqual({row["id"] for row in today.sections(satellite, now=NOW)["followups"]}, {mine, theirs})
