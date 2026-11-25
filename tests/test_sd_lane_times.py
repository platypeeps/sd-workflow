"""Lane wait and gate times (pack sd:6006): what the Queue page shows per repository, and the question they file.

Each entry's history rows in the hub database say when it was enqueued, claimed, gated and ended. Over 7 days the page
shows the median wait and gate time and the slowest entry; a median wait past the 30 min target files the
parallel-lanes question once a week (operator ruling, grill Q27; target, pack sd:7412 Q3).
"""

from __future__ import annotations

import calendar
import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_lane  # noqa: E402

REPOSITORY = "example/pack"
NOW = calendar.timegm(time.strptime("2026-09-10T12:00:00", "%Y-%m-%dT%H:%M:%S"))


def at(minutes: float) -> str:
    """`minutes` before NOW as a lane stamp."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - minutes * 60))


class Times(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        import sd_db
        sd_db.initialise(home=self.home)
        self.connection = sd_db.connect(home=self.home)
        self.addCleanup(self.connection.close)

    def row(self, entry: str, minutes: float, **body) -> None:
        """One history row of `entry`, written `minutes` before NOW, as `sd_lane.store` writes it."""
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - minutes * 60))
        self.connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                                (f"{sd_lane.KEY_PREFIX}{REPOSITORY}:{entry}", stamp, json.dumps({"id": entry, **body})))

    def entry(self, entry: str, item: int, *, enqueued: float, claimed: float, merge: float | None, ended: float | None,
              speculated: tuple[float, float] | None = None, status: str = "merged") -> None:
        """An entry's history: enqueue, an optional speculative gate, the claim, the merge step and its end."""
        base = {"item": item, "title": f"item {item}", "enqueued_at": at(enqueued)}
        self.row(entry, enqueued, status="pending", **base)
        if speculated:
            self.row(entry, speculated[0], status="pending", speculation={"status": "running"}, **base)
            self.row(entry, speculated[1], status="pending", speculation={"status": "success"}, **base)
        running = {**base, "status": "running", "started_at": at(claimed)}
        self.row(entry, claimed, step="prepare", **running)
        if merge is not None:
            self.row(entry, merge, step="merge", **running)
        if ended is not None:
            self.row(entry, ended, **{**running, "status": status, "step": "merge", "finished_at": at(ended)})

    def test_each_read_ranges_over_the_key_index(self):
        """Each lane read finds one repository's keys by an index range, not a kind scan (pack sd:10077)."""
        self.entry("e1", 1, enqueued=50, claimed=40, merge=35, ended=30)
        self.connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                                (f"{sd_lane.KEY_PREFIX}{REPOSITORY}2:x", "2026-09-10T11:00:00+00:00", "{}"))
        statements: list[str] = []
        self.connection.set_trace_callback(statements.append)
        self.assertEqual([row["id"] for row in sd_lane.stored(self.connection, REPOSITORY)], ["e1"])
        self.assertEqual(list(sd_lane.enqueue_order(self.connection, REPOSITORY)), ["e1"])
        self.assertEqual(len(sd_lane.entry_times(self.connection, REPOSITORY, now=NOW)), 1)
        self.connection.set_trace_callback(None)
        reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(reads), 3)
        for sql in reads:
            plan = " | ".join(row[3] for row in self.connection.execute(f"EXPLAIN QUERY PLAN {sql}"))
            self.assertIn("state_by_kind_key (kind=? AND key>? AND key<?)", plan, sql)

    def test_each_entry_has_its_wait_gate_and_total(self):
        # Waited 30 min; a 20 min speculative gate, then 5 min of prepare; ended 2 min after the merge step.
        self.entry("e1", 1, enqueued=100, claimed=70, merge=65, ended=63, speculated=(95, 75))
        [found] = sd_lane.entry_times(self.connection, REPOSITORY, now=NOW)
        self.assertEqual((found["item"], found["wait"], found["gate"], found["total"]), (1, 1800, 1500, 2220))

    def test_a_running_entry_has_a_wait_but_no_gate_or_total_yet(self):
        self.entry("e1", 1, enqueued=50, claimed=10, merge=None, ended=None)
        [found] = sd_lane.entry_times(self.connection, REPOSITORY, now=NOW)
        self.assertEqual((found["wait"], found["gate"], found["total"]), (2400, None, None))

    def test_a_prepare_that_ended_short_of_the_merge_ends_its_gate(self):
        self.entry("e1", 1, enqueued=50, claimed=40, merge=None, ended=30, status="failed")
        [found] = sd_lane.entry_times(self.connection, REPOSITORY, now=NOW)
        self.assertEqual((found["gate"], found["total"]), (600, 1200))

    def test_a_reclaimed_entry_sums_the_prepare_of_each_claim(self):
        """pack sd:6281: a reclaim back to pending ended the gate at the first claim's 60 s."""
        base = {"item": 1, "title": "item 1", "enqueued_at": at(130)}
        self.row("e1", 130, status="pending", **base)
        self.row("e1", 125, status="running", step="prepare", started_at=at(125), **base)
        self.row("e1", 124, status="pending", **base)
        self.row("e1", 70, status="running", step="prepare", started_at=at(70), **base)
        self.row("e1", 10, status="running", step="merge", started_at=at(70), **base)
        self.row("e1", 9, status="merged", step="merge", started_at=at(70), finished_at=at(9), **base)
        [found] = sd_lane.entry_times(self.connection, REPOSITORY, now=NOW)
        self.assertEqual((found["wait"], found["gate"], found["total"]), (300, 3660, 7260))

    def test_a_reclaimed_entry_still_in_its_second_prepare_has_no_gate_yet(self):
        base = {"item": 1, "title": "item 1", "enqueued_at": at(130)}
        self.row("e1", 130, status="pending", **base)
        self.row("e1", 125, status="running", step="prepare", started_at=at(125), **base)
        self.row("e1", 124, status="pending", **base)
        self.row("e1", 70, status="running", step="prepare", started_at=at(70), **base)
        [found] = sd_lane.entry_times(self.connection, REPOSITORY, now=NOW)
        self.assertIsNone(found["gate"])

    def test_the_page_reads_medians_the_slowest_entry_and_seven_days_only(self):
        self.entry("e1", 1, enqueued=100, claimed=90, merge=80, ended=79)
        self.entry("e2", 2, enqueued=300, claimed=200, merge=190, ended=185)
        self.entry("e3", 3, enqueued=60, claimed=30, merge=10, ended=9)
        self.entry("old", 4, enqueued=8 * 1440 + 60, claimed=8 * 1440, merge=8 * 1440 - 10, ended=8 * 1440 - 11)
        times = sd_lane.lane_times(self.connection, REPOSITORY, now=NOW)
        self.assertEqual(times["entries"], 3)
        self.assertEqual((times["wait"], times["gate"]), (1800, 600))
        self.assertEqual(times["slowest"], {"item": 2, "title": "item 2", "total": 6900})
        self.assertFalse(times["over"])

    def test_no_entry_reads_no_median(self):
        self.assertEqual(sd_lane.lane_times(self.connection, REPOSITORY, now=NOW),
                         {"days": 7, "entries": 0, "wait": None, "gate": None, "slowest": None, "over": False})


class TheQuestion(Times):
    """A median wait past the 30 min target files the parallel-lanes question once a week (grill Q27, pack sd:7412)."""

    def setUp(self) -> None:
        super().setUp()
        import sd_db
        sd_db.upsert_repo(self.connection, "/repos/pack", remote="https://github.com/example/pack.git", managed=1)

    def slow(self) -> None:
        self.entry("e1", 1, enqueued=200, claimed=100, merge=90, ended=89)

    def questions(self) -> list:
        return self.connection.execute(
            "SELECT note.body, item.title, item.repo FROM note JOIN item ON item.id = note.item "
            "WHERE note.kind = 'question'").fetchall()

    def test_a_median_wait_past_an_hour_files_one_question(self):
        self.slow()
        note = sd_lane.wait_question(self.connection, REPOSITORY, "/repos/pack", now=NOW)
        [found] = self.questions()
        self.assertIsNotNone(note)
        self.assertEqual(found["title"], "Decide: parallel lanes for example/pack")
        self.assertIn("median lane wait in example/pack was 100 min", found["body"])
        self.assertTrue(found["body"].endswith("\nOption: Plan parallel lanes (Recommended)\nOption: Not now"))
        # The next run asks nothing while that question is open.
        self.assertIsNone(sd_lane.wait_question(self.connection, REPOSITORY, "/repos/pack", now=NOW + 600))
        self.assertEqual(len(self.questions()), 1)

    def test_an_answered_question_is_not_asked_again_within_the_week(self):
        self.slow()
        sd_lane.wait_question(self.connection, REPOSITORY, "/repos/pack", now=NOW)
        self.connection.execute("UPDATE note SET resolved_at = timestamp WHERE kind = 'question'")
        self.assertIsNone(sd_lane.wait_question(self.connection, REPOSITORY, "/repos/pack", now=NOW + 3600))

    def test_a_median_wait_past_the_30_minute_target_files_the_question(self):
        """pack sd:7412 Q3: the operator set a 30 min median wait as the target; the hour let a 59 min week pass."""
        self.entry("e1", 1, enqueued=45, claimed=10, merge=5, ended=4)
        self.assertIsNotNone(sd_lane.wait_question(self.connection, REPOSITORY, "/repos/pack", now=NOW))
        [found] = self.questions()
        self.assertIn("was 35 min", found["body"])
        self.assertIn("past the 30 min target", found["body"])

    def closed(self, days: float) -> int:
        """The question asked and answered long ago, and its item done `days` before NOW."""
        import sd_db
        self.slow()
        sd_lane.wait_question(self.connection, REPOSITORY, "/repos/pack", now=NOW)
        [item] = [row["id"] for row in self.connection.execute("SELECT id FROM item")]
        sd_db.transition(self.connection, item, "done", who="test")
        self.connection.execute("UPDATE note SET resolved_at = '2000-01-01T00:00:00+00:00', "
                                "timestamp = '2000-01-01T00:00:00+00:00' WHERE kind = 'question'")
        self.connection.execute("UPDATE note SET timestamp = ? WHERE kind = 'status_change'",
                                (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - days * 86400)),))
        return item

    def test_a_done_item_files_nothing_in_the_week_it_measures(self):
        """The week after a fix lands reads waits from before it; that week is the measurement, not a miss."""
        item = self.closed(days=6.9)
        self.assertIsNone(sd_lane.wait_question(self.connection, REPOSITORY, "/repos/pack", now=NOW))
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id = ?", (item,)).fetchone()[0], "done")

    def test_a_done_item_a_week_on_reopens_with_the_question(self):
        """pack sd:7412 Q3: a miss files an item; a done item stayed silent, so the target was never read again."""
        item = self.closed(days=7.1)
        note = sd_lane.wait_question(self.connection, REPOSITORY, "/repos/pack", now=NOW)
        self.assertIsNotNone(note)
        self.assertEqual(self.connection.execute("SELECT status FROM item WHERE id = ?", (item,)).fetchone()[0],
                         "planning")
        self.assertEqual(len(self.questions()), 2)

    def test_a_done_item_with_no_status_change_files_nothing(self):
        """When it closed is unknown, so the operator's settle stands: unknown is not a week ago."""
        self.slow()
        sd_lane.wait_question(self.connection, REPOSITORY, "/repos/pack", now=NOW)
        self.connection.execute("UPDATE item SET status = 'done'")
        self.connection.execute("DELETE FROM note WHERE kind = 'status_change'")
        self.connection.execute("UPDATE note SET resolved_at = '2000-01-01T00:00:00+00:00', "
                                "timestamp = '2000-01-01T00:00:00+00:00' WHERE kind = 'question'")
        self.assertIsNone(sd_lane.wait_question(self.connection, REPOSITORY, "/repos/pack", now=NOW))

    def test_a_short_wait_files_nothing(self):
        self.entry("e1", 1, enqueued=30, claimed=20, merge=10, ended=9)
        self.assertIsNone(sd_lane.wait_question(self.connection, REPOSITORY, "/repos/pack", now=NOW))
        self.assertEqual(self.questions(), [])

    def test_a_hub_that_does_not_answer_is_named_and_stops_nothing(self):
        refused = sd_lane.LaneError("The hub database did not answer", code=sd_lane.HUB_UNAVAILABLE)
        with mock.patch.object(sd_lane, "queue_for", return_value=mock.Mock(repository=REPOSITORY)), \
                mock.patch.object(sd_lane, "on_hub", side_effect=refused):
            asked = sd_lane.ask_wait(pathlib.Path("/repos/pack"), "/repos/pack", {})
        self.assertEqual(asked, {"path": "/repos/pack", "wait_question": None,
                                 "error": "The hub database did not answer"})


class TheFlakes(TheQuestion):
    """pack sd:6739: a gate that fails and then passes at the same head files the failed suite as a defect."""

    HEAD, OTHER = "a" * 40, "b" * 40

    def ended(self, entry: str, item: int, minutes: float, status: str, head: str, reason: str = "") -> None:
        self.row(entry, minutes, item=item, title=f"item {item}", status=status, head=head, reason=reason,
                 finished_at=at(minutes))

    def failed(self, entry: str, minutes: float, suite: str, head: str | None = None) -> None:
        self.ended(entry, 7, minutes, "failed", head or self.HEAD,
                   f"the repository gate failed: check (exit 2): failed shard {suite}: 33s exit=1\n"
                   f"failing: ERROR: test_x ({suite}.Case.test_x)\nfailing: TimeoutError: timed out")

    def flakes(self, now: float = NOW) -> list:
        sd_lane.flake_items(self.connection, REPOSITORY, "/repos/pack", now=now)
        return self.connection.execute(
            "SELECT item.id, item.title, item.status, item.priority, item.external_id, item.repo, note.body "
            "FROM item JOIN note ON note.item = item.id AND note.kind = 'comment' "
            "WHERE item.external_id LIKE 'flake:%' ORDER BY note.id").fetchall()

    def test_a_pass_at_the_failed_head_files_one_item_for_the_suite(self):
        self.failed("e1", 60, "dashboard.tests.test_email_draft")
        self.ended("e2", 7, 30, "merged", self.HEAD)
        [found] = self.flakes()
        self.assertEqual((found["title"], found["status"], found["priority"], found["external_id"], found["repo"]),
                         ("Flaky suite dashboard.tests.test_email_draft: make it hermetic", "planning", 2,
                          "flake:dashboard.tests.test_email_draft", "/repos/pack"))
        self.assertIn(f"failed at {self.HEAD[:12]}", found["body"])
        self.assertIn("TimeoutError: timed out", found["body"])
        # The next run notes nothing new for the same head.
        self.assertEqual(len(self.flakes(NOW + 600)), 1)

    def test_a_second_flake_of_the_suite_reuses_its_open_item(self):
        self.failed("e1", 90, "tests.test_sd_install")
        self.ended("e2", 7, 80, "prepared", self.HEAD)
        self.failed("e3", 60, "tests.test_sd_install", self.OTHER)
        self.ended("e4", 8, 30, "merged", self.OTHER)
        found = self.flakes()
        self.assertEqual(len(found), 2)
        self.assertEqual(len({row["id"] for row in found}), 1)

    def test_a_split_shard_flakes_on_its_suites_one_item(self):
        """Review of pack sd:9770 (sd:9866): a split module's shard is `<suite>.partNofM`; its flake goes on the
        suite's item, the one sd-ship's flake retry names by module, not one item per part."""
        self.failed("e1", 90, "tests.test_sd_install.part2of3")
        self.failed("e5", 85, "tests.test_sd_install.part3of3")
        self.ended("e2", 7, 80, "prepared", self.HEAD)
        self.failed("e3", 60, "tests.test_sd_install", self.OTHER)
        self.ended("e4", 8, 30, "merged", self.OTHER)
        found = self.flakes()
        self.assertEqual({row["external_id"] for row in found}, {"flake:tests.test_sd_install"})
        self.assertEqual(found[0]["title"], "Flaky suite tests.test_sd_install: make it hermetic")
        self.assertEqual(len(found), 2, "one note per head, whichever parts failed there")

    def test_a_done_item_reopens_when_its_suite_flakes_again(self):
        self.failed("e1", 90, "tests.test_sd_install")
        self.ended("e2", 7, 80, "merged", self.HEAD)
        [first] = self.flakes()
        self.connection.execute("UPDATE item SET status = 'done' WHERE id = ?", (first["id"],))
        self.failed("e3", 60, "tests.test_sd_install", self.OTHER)
        self.ended("e4", 8, 30, "merged", self.OTHER)
        found = self.flakes()
        self.assertEqual((len(found), found[-1]["id"], found[-1]["status"]), (2, first["id"], "planning"))

    def test_a_pass_at_a_new_head_or_a_review_stop_files_nothing(self):
        self.failed("e1", 60, "tests.test_sd_install")
        self.ended("e2", 7, 30, "merged", self.OTHER)
        self.ended("e3", 8, 50, "failed", self.OTHER, "local review blocking: 1/1 completed")
        self.ended("e4", 8, 20, "merged", self.OTHER)
        self.assertEqual(self.flakes(), [])

    def test_a_pass_before_the_failure_or_outside_the_window_files_nothing(self):
        self.ended("e1", 7, 90, "merged", self.HEAD)
        self.failed("e2", 60, "tests.test_sd_install")
        self.failed("e3", 9 * 24 * 60, "tests.test_bin_help", self.OTHER)
        self.ended("e4", 8, 8 * 24 * 60, "merged", self.OTHER)
        self.assertEqual(self.flakes(), [])

    def test_a_hub_that_does_not_answer_is_named_and_stops_nothing(self):
        refused = sd_lane.LaneError("The hub database did not answer", code=sd_lane.HUB_UNAVAILABLE)
        with mock.patch.object(sd_lane, "queue_for", return_value=mock.Mock(repository=REPOSITORY)), \
                mock.patch.object(sd_lane, "on_hub", side_effect=refused):
            filed = sd_lane.file_flakes(pathlib.Path("/repos/pack"), "/repos/pack", {})
        self.assertEqual(filed, {"path": "/repos/pack", "flakes": None, "error": "The hub database did not answer"})


if __name__ == "__main__":
    unittest.main()
