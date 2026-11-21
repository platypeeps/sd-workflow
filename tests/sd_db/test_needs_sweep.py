"""The Needs you sweep (sd:9759): each close rule, and a row it cannot prove stays open."""

import contextlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sd_db import (
    connect,
    initialise,
    issue_attention,
    needs_sweep,
    pr_attention,
    reporting,
    workflow,
)
from sd_db.writes import add_note, create_item, record_state, transition

ENDED = "2026-11-20T01:00:00+00:00"
LATER = "2026-11-20T02:00:00+00:00"
Q = "Which way?\nOption: a (Recommended)\nOption: b"


class TheSweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialise(Path(self.tmp.name) / "sd.db")
        self.db = connect(Path(self.tmp.name) / "sd.db")
        self.addCleanup(self.db.close)

    def report(self, job="disk-check", host="box", basis="disk full", run="r1"):
        """Through `ingest`, the real producer, so `fields` and the ingest's followup are the cron path's."""
        return reporting.ingest(self.db, job=job, run_id=run, started=ENDED, ended=ENDED, exit_code=0, text="full\n",
                                source_path="/var/log/example.log", attention=True, attention_basis=basis,
                                host=host)["item"]["id"]

    def chain(self, job="disk-check", host="box", run="r2", ended=LATER, item=None):
        record_state(self.db, "heartbeat", key=f"{reporting.CHAIN_KEY}{job}@{host}",
                     body={"run_id": run, "ended": ended, "item": item})

    def status(self, item):
        return workflow.item_state(self.db, item)["item"]["status"]

    def note(self, note):
        return self.db.execute("SELECT * FROM note WHERE id = ?", (note,)).fetchone()

    def last_status_note(self, item):
        return self.db.execute("SELECT body FROM note WHERE item = ? AND kind = 'status_change' ORDER BY id DESC",
                               (item,)).fetchone()["body"]

    def sweep(self, apply=True):
        return needs_sweep.sweep(self.db, who="operator", apply=apply)

    def test_a_report_whose_host_ran_clean_since_closes_naming_the_run(self):
        item = self.report()
        self.chain()
        out = self.sweep()
        self.assertEqual(self.status(item), "done")
        self.assertIn(f"{reporting.RECOVERED}disk-check:r2 on box ended {LATER}; needs-you sweep",
                      self.last_status_note(item))
        self.assertEqual([row["item"] for row in out["closed"]], [item])
        self.assertIsNone(self.db.execute("SELECT 1 FROM note WHERE item = ? AND kind = 'followup'"
                                          " AND resolved_at IS NULL", (item,)).fetchone())

    def test_a_report_with_no_clean_run_since_stays_open(self):
        for name, setup in (("no run since", lambda: None),
                            ("an older run", lambda: self.chain(run="r0", ended="2026-11-19T00:00:00+00:00")),
                            ("a run that filed", lambda: self.chain(item=1)),
                            ("another host's run", lambda: self.chain(host="other"))):
            with self.subTest(name):
                self.db.execute("DELETE FROM state")
                self.db.execute("DELETE FROM note")
                self.db.execute("DELETE FROM item")
                item = self.report()
                setup()
                out = self.sweep()
                self.assertEqual(self.status(item), "planning")
                self.assertEqual(out["closed"], [])
                self.assertEqual([row["item"] for row in out["kept"]], [item])

    def test_a_report_another_hosts_newest_run_holds_stays_open(self):
        """Review round 1: as `reporting._host_reports`, a report another host's chain names is that host's finding."""
        item = self.report()
        self.chain()
        self.chain(host="other", run="r9", item=item)
        out = self.sweep()
        self.assertEqual(self.status(item), "planning")
        self.assertEqual(out["kept"][0]["why"], "another host's newest run of disk-check holds it")

    def test_a_report_with_no_host_stays_open(self):
        item = self.report(host=None)
        self.chain(host="None")
        self.assertEqual(self.sweep()["kept"][0]["why"], "no job or host stored")
        self.assertEqual(self.status(item), "planning")

    def test_a_cleared_report_with_a_persons_followup_stays_open(self):
        item = self.report()
        add_note(self.db, item, "followup", "check the disk by hand", session="operator")
        self.chain()
        out = self.sweep()
        self.assertEqual(self.status(item), "planning")
        self.assertEqual(out["kept"], [{"item": item, "kind": "report", "title": out["kept"][0]["title"],
                                        "why": "a followup someone else wrote is open"}])

    def pr_alert(self, basis="waiting_on_reviewers https://github.com/example/widget/pull/7"):
        return self.report(job=pr_attention.JOB, host=None, basis=basis, run="2026-11-20T01:00:00Z:x")

    def test_a_pr_alert_a_later_read_no_longer_sees_closes(self):
        item = self.pr_alert()
        record_state(self.db, "heartbeat", key=pr_attention.SEEN_KEY, timestamp="2099-01-01T00:00:00Z",
                     body={"seen": ["ready https://github.com/example/widget/pull/8"], "complete": True, "unread": []})
        out = self.sweep()
        self.assertEqual(self.status(item), "done")
        self.assertIn("the read at 2099-01-01T00:00:00Z no longer sees waiting_on_reviewers", self.last_status_note(item))
        self.assertEqual(len(out["closed"]), 1)

    def test_a_pr_alert_still_seen_or_not_read_since_stays_open(self):
        basis = "waiting_on_reviewers https://github.com/example/widget/pull/7"
        complete = {"complete": True, "unread": []}
        for name, stamp, body in (("still seen", "2099-01-01T00:00:00Z", {**complete, "seen": [basis]}),
                                  ("read before it was filed", "2000-01-01T00:00:00Z", {**complete, "seen": []}),
                                  ("never read", None, None),
                                  # Review round 2: absence from a read that was cut, or that skipped the alert's
                                  # state, or that names neither (a row written before the flag), proves nothing.
                                  ("a cut read", "2099-01-01T00:00:00Z", {"seen": [], "complete": False, "unread": []}),
                                  ("its state unread", "2099-01-01T00:00:00Z",
                                   {"seen": [], "complete": True, "unread": ["waiting_on_reviewers"]}),
                                  ("an older row", "2099-01-01T00:00:00Z", {"seen": []})):
            with self.subTest(name):
                self.db.execute("DELETE FROM state")
                self.db.execute("DELETE FROM note")
                self.db.execute("DELETE FROM item")
                item = self.pr_alert(basis)
                if stamp:
                    record_state(self.db, "heartbeat", key=pr_attention.SEEN_KEY, timestamp=stamp, body=body)
                self.sweep()
                self.assertEqual(self.status(item), "planning")

    def raw_heartbeat(self, key, stamp, text):
        """A heartbeat whose body is the text as stored, malformed or not: `record_state` serialises every body."""
        self.db.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('heartbeat', ?, ?, ?)", (key, stamp, text))
        self.db.commit()

    def fresh(self):
        for table in ("state", "note", "item"):
            self.db.execute(f"DELETE FROM {table}")

    def test_a_malformed_seen_row_proves_no_clear(self):
        """Review round 3: a seen row of the wrong shape kept the alert open only by luck, or closed it, or raised."""
        basis = "waiting_on_reviewers https://github.com/example/widget/pull/7"
        for name, text in (("no seen list", '{"complete": true}'),
                           ("a string seen", '{"complete": true, "unread": [], "seen": "waiting_on_reviewers x"}'),
                           ("a seen list of numbers", '{"complete": true, "unread": [], "seen": [1]}'),
                           ("no unread list", '{"complete": true, "seen": []}'),
                           ("a string unread", '{"complete": true, "unread": "", "seen": []}'),
                           ("a list body", "[1, 2]"),
                           ("a body that is no JSON", "{not json")):
            with self.subTest(name):
                self.fresh()
                item = self.pr_alert(basis)
                self.raw_heartbeat(pr_attention.SEEN_KEY, "2099-01-01T00:00:00Z", text)
                out = self.sweep()
                self.assertEqual(self.status(item), "planning")
                self.assertEqual((out["closed"], out["problems"]), ([], []))
                self.assertEqual(out["kept"][0]["why"], "the newest read is not in the shape a clear needs")

    def test_a_malformed_chain_row_proves_no_clean_run(self):
        key = f"{reporting.CHAIN_KEY}disk-check@box"
        for name, text in (("a list body", "[1]"), ("a body that is no JSON", "{oops"),
                           ("a number ended", '{"run_id": "r2", "ended": 99999999, "item": null}'),
                           ("a number run", f'{{"run_id": 2, "ended": "{LATER}", "item": null}}'),
                           ("no item field", f'{{"run_id": "r2", "ended": "{LATER}"}}')):
            with self.subTest(name):
                self.fresh()
                item = self.report()
                self.raw_heartbeat(key, LATER, text)
                out = self.sweep()
                self.assertEqual(self.status(item), "planning")
                self.assertEqual((out["closed"], out["problems"]), ([], []))
                self.assertEqual(out["kept"][0]["why"], "no clean run of disk-check on box since")

    def test_a_report_with_malformed_fields_stays_open(self):
        seen = {"seen": ["ready https://github.com/example/widget/pull/8"], "complete": True, "unread": []}
        for name, path, value, pr in (("a list job", "$.report.job", '["disk-check"]', False),
                                      ("a number host", "$.report.host", "7", False),
                                      ("a number last_ended", "$.repeats.last_ended", "1", False),
                                      ("a null basis", "$.report.attention_basis", "null", True),
                                      ("a list basis", "$.report.attention_basis", '["x"]', True)):
            with self.subTest(name):
                self.fresh()
                item = self.pr_alert() if pr else self.report()
                if pr:
                    record_state(self.db, "heartbeat", key=pr_attention.SEEN_KEY, timestamp="2099-01-01T00:00:00Z",
                                 body=seen)
                else:
                    self.chain()
                    self.db.execute("UPDATE item SET fields = json_set(fields, '$.repeats', json('{}')) WHERE id = ?"
                                    " AND json_extract(fields, '$.repeats') IS NULL", (item,))
                self.db.execute("UPDATE item SET fields = json_set(fields, ?, json(?)) WHERE id = ?", (path, value, item))
                self.db.commit()
                out = self.sweep()
                self.assertEqual(self.status(item), "planning")
                self.assertEqual((out["closed"], out["problems"]), ([], []))
                self.assertEqual(out["kept"][0]["why"], "its report fields are not in the shape a clear needs")

    def test_a_returned_finding_whose_refile_failed_keeps_its_open_report(self):
        """Review round 4: a found state whose report write failed was left out of `seen`, so the sweep read its
        absence as a clear and closed the old report that still stood for it. Issue attention shares the writer."""
        for job, key, state, labels in ((pr_attention.JOB, pr_attention.SEEN_KEY, "review_requested", pr_attention.LABELS),
                                        (issue_attention.JOB, issue_attention.SEEN_KEY, "assigned", issue_attention.LABELS)):
            with self.subTest(job):
                self.fresh()
                url = "https://github.com/example/widget/pull/7"
                basis = f"{state} {url}"
                item = self.report(job=job, host=None, basis=basis, run="2026-11-20T01:00:00Z:x")
                found = {basis: {"state": state, "repo": "example/widget", "number": 7, "title": "t", "url": url}}

                def refused(*_, **__):
                    raise sqlite3.OperationalError("database is locked")
                with mock.patch.object(reporting, "ingest", refused):
                    done = pr_attention.alert(self.db, found, True, now=datetime(2099, 1, 1, tzinfo=timezone.utc),
                                              job=job, seen_key=key, labels=labels)
                self.assertEqual(done["filed"], [])
                out = self.sweep()
                self.assertEqual(self.status(item), "planning")
                self.assertEqual(out["closed"], [])

    def test_a_report_the_operator_reopened_needs_evidence_newer_than_the_reopen(self):
        """Review round 6: a reopen outranks the evidence that closed it; only a run or a read after it clears."""
        for name, item, evidence in (
                ("a clean run", self.report(), lambda: self.chain()),
                ("a read that no longer sees it", self.pr_alert(),
                 lambda: record_state(self.db, "heartbeat", key=pr_attention.SEEN_KEY, timestamp="2099-01-01T00:00:00Z",
                                      body={"seen": [], "complete": True, "unread": []}))):
            with self.subTest(name):
                evidence()
                self.sweep()
                self.assertEqual(self.status(item), "done")
                transition(self.db, item, "planning", who="operator", reason="still broken")
                # The reopen lands after the evidence: a seen read stamped 2099 needs a later reopen.
                self.db.execute("UPDATE note SET timestamp = '2100-01-01T00:00:00+00:00' WHERE id ="
                                " (SELECT max(id) FROM note WHERE item = ? AND kind = 'status_change')", (item,))
                out = self.sweep()
                self.assertEqual(self.status(item), "planning")
                self.assertEqual([row["why"] for row in out["kept"] if row["item"] == item],
                                 ["no evidence since the operator reopened it"])

    def test_a_report_end_or_a_run_end_that_is_no_time_proves_no_clean_run(self):
        """Review round 4: a missing report end read as "" and any run sorted after it; text that is no time sorted
        after a real one. Only two real times, in order, prove the run came after the report."""
        no_run = "no clean run of disk-check on box since"
        for name, drop, run_ended, why in (("no report end", True, LATER, needs_sweep.BAD_FIELDS),
                                           ("a report end that is no time", "not-a-time", LATER, needs_sweep.BAD_FIELDS),
                                           ("a run end that is no time", None, "zzzz", no_run),
                                           ("a run end with no zone", None, "2026-11-20T02:00:00", no_run)):
            with self.subTest(name):
                self.fresh()
                item = self.report()
                self.chain(ended=run_ended)
                if drop is True:
                    self.db.execute("UPDATE item SET fields = json_remove(fields, '$.report.ended', '$.repeats.last_ended')"
                                    " WHERE id = ?", (item,))
                elif drop:
                    self.db.execute("UPDATE item SET fields = json_set(json_remove(fields, '$.repeats.last_ended'),"
                                    " '$.report.ended', ?) WHERE id = ?", (drop, item))
                self.db.commit()
                out = self.sweep()
                self.assertEqual(self.status(item), "planning")
                self.assertEqual(out["closed"], [])
                self.assertEqual(out["kept"][0]["why"], why)

    def task(self, status="planning"):
        item = create_item(self.db, kind="task", title="a task", status="planning")
        if status != "planning":
            transition(self.db, item, status, who="operator")
        return item

    def test_a_question_a_standing_ruling_answers_is_resolved_with_a_comment(self):
        item = self.task()
        asked = add_note(self.db, item, "question", Q, session="operator")
        ruled = add_note(self.db, item, "decision", f"Ruling on note #{asked}: a", session="operator")
        out = self.sweep()
        self.assertIsNotNone(self.note(asked)["resolved_at"])
        self.assertEqual(out["closed"][0]["why"], f"answered by decision #{ruled}")
        comment = self.db.execute("SELECT body FROM note WHERE item = ? AND kind = 'comment'", (item,)).fetchone()
        self.assertEqual(comment["body"], f"needs-you sweep: resolved question #{asked}: answered by decision #{ruled}")

    def test_a_question_no_ruling_answers_stays_open(self):
        """Review round 1: only `Ruling on note #N:` answers N; a mention of the number is no answer."""
        item = self.task()
        asked = add_note(self.db, item, "question", Q, session="operator")
        for body in (f"An unrelated ruling on #{asked}1", f"Operator answered question #{asked}: option a",
                     f"Landed in pull (#{asked})", f"Ruling on note #{asked}1: a", f"See Ruling on note #{asked}: a"):
            add_note(self.db, item, "decision", body, session="operator")
        after = add_note(self.db, item, "question", Q, session="operator")
        self.sweep()
        self.assertIsNone(self.note(asked)["resolved_at"])
        self.assertIsNone(self.note(after)["resolved_at"])

    def test_a_question_whose_ruling_was_reversed_or_withdrawn_stays_open(self):
        """Review round 1: Decisions' reversal and a review withdrawal reopen the question; the sweep keeps it open."""
        for undo in ("Reversed ruling #{m}: question #{q} asks again.", "Withdrawn: ruling #{m} on note #{q}",
                     "Withdrawn: ruling #{m} on note #{q}; sd:7 stays filed"):
            with self.subTest(undo):
                item = self.task()
                asked = add_note(self.db, item, "question", Q, session="operator")
                ruled = add_note(self.db, item, "decision", f"Ruling on note #{asked}: a", session="operator")
                add_note(self.db, item, "decision", undo.format(m=ruled, q=asked), session="operator")
                out = self.sweep()
                self.assertIsNone(self.note(asked)["resolved_at"])
                self.assertEqual([row for row in out["closed"] if row.get("note") == asked], [])

    def test_a_new_ruling_after_a_reversal_answers_the_question(self):
        item = self.task()
        asked = add_note(self.db, item, "question", Q, session="operator")
        first = add_note(self.db, item, "decision", f"Ruling on note #{asked}: a", session="operator")
        add_note(self.db, item, "decision", f"Reversed ruling #{first}: question #{asked} asks again.", session="operator")
        second = add_note(self.db, item, "decision", f"Ruling on note #{asked}: b", session="operator")
        out = self.sweep()
        self.assertIsNotNone(self.note(asked)["resolved_at"])
        self.assertEqual(out["closed"][0]["why"], f"answered by decision #{second}")

    def test_a_followup_on_a_done_item_stays_open(self):
        """Review round 2: a closed item proves nothing about a followup's own work; the sweep leaves followups alone.

        `done` through `workflow` resolves what was owed (sd:3505); one open on a done item was written after, or
        reached done another way, and nothing says it was addressed.
        """
        before, after = self.task(), self.task("done")
        early = add_note(self.db, before, "followup", "send the summary", session="operator")
        transition(self.db, before, "done", who="operator")
        late = add_note(self.db, after, "followup", "send the summary", session="operator")
        out = self.sweep()
        self.assertEqual((self.note(early)["resolved_at"], self.note(late)["resolved_at"]), (None, None))
        self.assertEqual([row for row in out["closed"] + out["kept"] if row["kind"] == "followup"], [])

    def racing(self, change):
        """`transaction` as the sweep sees it: `change` lands first, as a write between the check and the close."""
        real = needs_sweep.transaction

        @contextlib.contextmanager
        def late(connection):
            change()
            with real(connection) as held:
                yield held
        return mock.patch.object(needs_sweep, "transaction", late)

    def test_a_report_renewed_after_the_check_stays_open(self):
        """Review round 2: eligibility is read again inside the write that closes the row."""
        item = self.report()
        self.chain()
        with self.racing(lambda: self.chain(host="other", run="r9", item=item)):
            out = self.sweep()
        self.assertEqual(self.status(item), "planning")
        self.assertEqual((out["closed"], out["kept"][0]["why"]),
                         ([], "another host's newest run of disk-check holds it"))

    def test_a_question_whose_ruling_is_reversed_after_the_check_stays_open(self):
        item = self.task()
        asked = add_note(self.db, item, "question", Q, session="operator")
        ruled = add_note(self.db, item, "decision", f"Ruling on note #{asked}: a", session="operator")
        with self.racing(lambda: add_note(self.db, item, "decision", f"Reversed ruling #{ruled}: question #{asked}"
                                          " asks again.", session="operator")):
            out = self.sweep()
        self.assertIsNone(self.note(asked)["resolved_at"])
        self.assertEqual((out["closed"], out["kept"][0]["why"]), ([], "no standing ruling"))

    def test_a_dry_run_lists_what_would_close_and_writes_nothing(self):
        item = self.report()
        self.chain()
        task = self.task()
        asked = add_note(self.db, task, "question", Q, session="operator")
        add_note(self.db, task, "decision", f"Ruling on note #{asked}: a", session="operator")
        before = self.db.execute("SELECT COUNT(*) FROM note").fetchone()[0]
        out = self.sweep(apply=False)
        self.assertEqual([row["item"] for row in out["closed"]], [item, task])
        self.assertEqual((self.status(item), self.note(asked)["resolved_at"]), ("planning", None))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM note").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
