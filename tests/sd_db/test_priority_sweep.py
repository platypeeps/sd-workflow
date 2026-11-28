"""The nightly priority sweep (sd:6726): rank, close at 30 days, or ask."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sd_db import connect, initialise, priority_sweep, review, workflow

NOW = datetime(2026, 10, 29, 6, 15, tzinfo=timezone.utc)


class Sweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialise(Path(self.tmp.name) / "sd.db")
        self.db = connect(Path(self.tmp.name) / "sd.db")
        self.addCleanup(self.db.close)

    def item(self, title, *, filed, priority=None, kind="task", due=None):
        item = workflow.capture_task(self.db, title=title, priority=priority, due=due, who="test")["item"]["id"]
        self.db.execute("UPDATE item SET created_at = ?, kind = ? WHERE id = ?", (filed, kind, item))
        return item

    def row(self, item):
        return self.db.execute("SELECT status, priority FROM item WHERE id = ?", (item,)).fetchone()

    def test_a_31_day_unranked_item_closes_and_a_29_day_one_is_asked(self):
        old = self.item("Old", filed="2026-09-28T09:00:00+00:00")
        young = self.item("Young", filed="2026-09-30T09:00:00+00:00")
        ranked = self.item("Ranked", filed="2026-01-01T09:00:00+00:00", priority=2)
        idea = self.item("Idea", filed="2026-01-01T09:00:00+00:00", kind="work-idea")
        done = priority_sweep.sweep(self.db, who="sweep", now=NOW)
        self.assertEqual((done["closed"], done["asked"]), ([old], 1))
        self.assertEqual(self.row(old)["status"], "done")
        note = self.db.execute("SELECT body FROM note WHERE item = ? AND kind = 'status_change' ORDER BY id DESC",
                               (old,)).fetchone()["body"]
        self.assertIn(f"priority sweep (sd:6726): no priority 30 days after filing; reopen with `sd task status {old} planning`",
                      note)
        for kept in (young, ranked, idea):
            self.assertNotEqual(self.row(kept)["status"], "done")
        batch = review.read(self.db, done["batch"])
        (question,) = batch["questions"]
        self.assertEqual((question["subject"], question["options"]), (f"sd:{young}", ["P1", "P2", "P3", "P4"]))
        self.assertEqual(question["recommendation"]["option"], "P3")
        self.assertNotIn(done["batch"], [item["id"] for item in priority_sweep.unranked(self.db)],
                         "the batch's own row is the queue, not unranked work")

    def test_a_review_finding_item_is_unranked_work_and_a_batch_is_not(self):
        # sd:10981: only a filed batch's header makes a row a batch; a `review:<slug>` finding item is work to rank.
        finding = self.item("UX terms", filed="2026-10-20T09:00:00+00:00")
        self.db.execute("UPDATE item SET source = 'task-ref', external_id = 'review:mws-ux-terms' WHERE id = ?",
                        (finding,))
        done = priority_sweep.sweep(self.db, who="sweep", now=NOW)
        unranked = [item["id"] for item in priority_sweep.unranked(self.db)]
        self.assertIn(finding, unranked)
        self.assertNotIn(done["batch"], unranked)

    def test_a_question_asked_once_is_not_asked_again_and_its_ruling_sets_the_rank(self):
        young = self.item("Young", filed="2026-10-20T09:00:00+00:00", kind="followup")
        first = priority_sweep.sweep(self.db, who="sweep", now=NOW)
        self.assertEqual(review.read(self.db, first["batch"])["questions"][0]["recommendation"]["option"], "P2")
        again = priority_sweep.sweep(self.db, who="sweep", now=NOW.replace(hour=7))
        self.assertEqual(again["asked"], 0)
        batch = review.read(self.db, first["batch"])
        note = batch["questions"][0]["note"]
        review.rule(self.db, first["batch"], note, "P1", expected_revision=workflow.item_state(self.db, first["batch"])["revision"],
                    who="operator")
        ruled = priority_sweep.sweep(self.db, who="sweep", now=NOW.replace(hour=8))
        self.assertEqual((ruled["ranked"], ruled["batches_closed"]), ([young], [first["batch"]]))
        self.assertEqual(self.row(young)["priority"], 1)

    def test_an_item_a_build_holds_stays_open_and_the_rest_still_sweep(self):
        """Review blocker: a queued or running assignment refuses the close; the sweep goes on."""
        held = self.item("Held", filed="2026-09-01T09:00:00+00:00")
        old = self.item("Old", filed="2026-09-02T09:00:00+00:00")
        young = self.item("Young", filed="2026-10-20T09:00:00+00:00")
        self.db.execute("INSERT INTO assignment (item, role, status) VALUES (?, 'author', 'running')", (held,))
        done = priority_sweep.sweep(self.db, who="sweep", now=NOW)
        self.assertEqual((done["closed"], [item for item, _ in done["held"]], done["asked"]), ([old], [held], 1))
        self.assertIn("assignment", done["held"][0][1])
        self.assertEqual((self.row(held)["status"], self.row(old)["status"]), ("planning", "done"))
        self.assertEqual(review.read(self.db, done["batch"])["questions"][0]["subject"], f"sd:{young}")

    def test_a_near_due_date_proposes_p2(self):
        self.assertEqual(priority_sweep.propose({"kind": "task", "due": "2026-11-05"}, NOW)["option"], "P2")
        self.assertEqual(priority_sweep.propose({"kind": "task", "due": "2026-12-31"}, NOW)["option"], "P3")


if __name__ == "__main__":
    unittest.main()
