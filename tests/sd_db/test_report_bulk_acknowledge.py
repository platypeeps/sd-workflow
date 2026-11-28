"""Acknowledging many clean reports in one attributable call (sd:755).

At 2026-09-13T16:49:32Z 281 reports went `planning -> done by user` in one
second, from a script that let `acknowledge` default `who`. The batch was
right; the defect was that a wrong one would have read the same. These pin
the supported path that replaces the script: a dry run that writes nothing
and issues a plan, and an apply that moves exactly that plan or nothing,
files one record of who did it, and never takes a report retention would not.
"""
import hashlib
import json
import os
import random
import re
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import sd_db
from sd_db import reporting, retention, workflow, writes
from sd_db.migrate import initialise
from sd_db.writes import (
    add_note,
    create_assignment,
    create_item,
    record_state,
    resolve_note,
)

#: The cutoff every fixture below is judged at, and the clock the dry run and
#: the apply are handed: two days after it.
CUTOFF = "2026-09-10T00:00:00+00:00"
NOW = datetime(2026, 9, 12, tzinfo=UTC)

#: When a fixture report was filed, unless it says otherwise: before the cutoff.
FILED = "2026-09-09T00:00:00+00:00"

ATTENTION = "it needs attention, which waits for a person"
FOLLOWUP = "it has an unresolved followup"
RECORD = "an operator record, not a run report"
UNREADABLE = "its fields are not valid JSON, so it cannot say it is clean"
LATE = "its run ended after the job's newest recorded tick"
IN_FLIGHT = "work on it is queued, running or ending"


class Store(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "sd.db"; initialise(self.path)
        self.db = sd_db.connect(self.path); self.addCleanup(self.db.close)

    def dump(self):
        return tuple(self.db.iterdump())

    def report(self, *, job, attention=False, status="planning", created_at=FILED, ended=None, record=None):
        """A report as `ingest` shapes its `fields`, filed at a chosen instant."""
        fields = {"attention": attention, "report": {"job": job, "ended": ended or created_at}}
        if record is not None:
            fields["record"] = record
        item = create_item(self.db, kind="report", title=f"{job}: run report", status=status,
                           source="cron-report", external_id=f"{job}:run", fields=fields, created_at=created_at)
        if attention:
            add_note(self.db, item, "followup", f"Review {job} findings.", session="cron")
        return item

    def fixture(self):
        """The seven reports of implement step 3."""
        self.clean = [self.report(job="alpha"), self.report(job="beta")]
        self.attention = self.report(job="gamma", attention=True)
        self.followed = self.report(job="delta")
        self.followup = add_note(self.db, self.followed, "followup", "look at this", session="alex")
        self.record = self.report(job="reports-acknowledge", record="reports-acknowledge")
        self.later = self.report(job="epsilon", created_at="2026-09-10T00:00:01+00:00")
        self.done = self.report(job="zeta", status="done")

    def preview(self, **kwargs):
        return reporting.clean_reports(self.db, **{"before": CUTOFF, "now": NOW, **kwargs})

    def apply(self, plan, **kwargs):
        return reporting.acknowledge_clean(self.db, **{"before": CUTOFF, "expected_plan": plan, "who": "alex",
                                                       "principal": "local", "program": "test", "now": NOW,
                                                       **kwargs})

    def status_of(self, item):
        return self.db.execute("SELECT status FROM item WHERE id=?", (item,)).fetchone()[0]

    def newest_note(self, item):
        return self.db.execute("SELECT body FROM note WHERE item=? AND kind='status_change' ORDER BY id DESC LIMIT 1",
                               (item,)).fetchone()[0]

    def revision(self, item):
        return workflow.item_state(self.db, item)["revision"]


class FieldsDocument(Store):
    """`reporting.fields_document`, the parse a page reads so its verdict is
    the store's (sd:873, the review of #395)."""

    def test_it_refuses_what_the_predicate_refuses_and_parses_the_rest(self):
        # The same three the sites pin, then the two parsers' disagreements:
        # the constants Python accepts and SQLite does not, and nesting past
        # SQLite's depth limit, which Python reads to 1100 and raises on at
        # 10000. `json_valid` is the verdict, so the parse never runs there.
        for value in (None, "", "{not json", "NaN", '{"a": Infinity}',
                      "[" * 1100 + "]" * 1100, "[" * 10000 + "]" * 10000):
            with self.subTest(fields=value[:24] if isinstance(value, str) else value):
                self.assertEqual(self.db.execute(
                    f"SELECT {reporting.UNREADABLE_FIELDS} FROM (SELECT ? AS fields)", (value,)).fetchone()[0], 1)
                with self.assertRaises(reporting.UnreadableFields):
                    reporting.fields_document(self.db, value)
        document = {"attention": False, "report": {"job": "alpha"}}
        self.assertEqual(reporting.fields_document(self.db, json.dumps(document)), document)
        # Valid JSON that is not an object is the caller's to judge, not this
        # function's to refuse: `json_valid('null')` is 1.
        self.assertIsNone(reporting.fields_document(self.db, "null"))
        self.assertEqual(reporting.fields_document(self.db, "[" * 900 + "]" * 900), json.loads("[" * 900 + "]" * 900))

    def test_an_integer_past_pythons_digit_limit_is_read_as_sqlite_reads_it(self):
        # The review of #395: `json_valid` accepts an integer token of 4300 or
        # more digits, and `json.loads` raises `ValueError` on it, so the page
        # withheld an acknowledge that retention and the bulk clean would make.
        digits = "9" * 5000
        value = '{"attention": false, "x": -' + digits + "}"
        self.assertEqual(self.db.execute(
            f"SELECT {reporting.UNREADABLE_FIELDS} FROM (SELECT ? AS fields)", (value,)).fetchone()[0], 0)
        document = reporting.fields_document(self.db, value)
        self.assertIs(document["attention"], False)
        self.assertEqual(str(document["x"]), "-" + digits)
        self.assertEqual(reporting.fields_document(self.db, '{"x": 12}'), {"x": 12})

    def test_it_raises_a_workflow_error_so_a_caller_that_catches_those_catches_it(self):
        self.assertTrue(issubclass(reporting.UnreadableFields, workflow.WorkflowError))


class TheDryRun(Store):

    def test_it_selects_the_clean_reports_and_declines_the_rest_with_a_reason(self):
        self.fixture()
        before = self.dump()
        preview = self.preview()
        self.assertEqual(before, self.dump())
        self.assertEqual([entry["id"] for entry in preview["selected"]], self.clean)
        self.assertEqual(preview["declined"], [{"id": self.attention, "why": ATTENTION},
                                               {"id": self.followed, "why": FOLLOWUP},
                                               {"id": self.record, "why": RECORD}])
        listed = {entry["id"] for entry in preview["selected"] + preview["declined"]}
        self.assertNotIn(self.later, listed)
        self.assertNotIn(self.done, listed)
        self.assertEqual((preview["count"], preview["declined_count"], preview["max_batch"], preview["before"]),
                         (2, 3, 1000, CUTOFF))
        self.assertEqual([(entry["job"], entry["created_at"], entry["revision"]) for entry in preview["selected"]],
                         [("alpha", FILED, self.revision(self.clean[0])), ("beta", FILED, self.revision(self.clean[1]))])
        pairs = sorted([item, self.revision(item)] for item in self.clean)
        self.assertEqual(preview["plan"], hashlib.sha256(
            json.dumps([CUTOFF, pairs], separators=(",", ":")).encode()).hexdigest())

    def test_a_null_record_marker_is_still_a_record(self):
        """`json_type` says `'null'` for `{"record": null}`; `json_extract ... IS NOT NULL` would select it."""
        marked = self.report(job="hand-edited")
        self.db.execute("UPDATE item SET fields=json_set(fields, '$.record', json('null')) WHERE id=?", (marked,))
        self.assertIsNone(self.db.execute("SELECT json_extract(fields, '$.record') FROM item WHERE id=?",
                                          (marked,)).fetchone()[0])
        self.assertEqual(self.preview()["declined"], [{"id": marked, "why": RECORD}])

    def test_unreadable_fields_decline_the_report_and_do_not_fail_the_read(self):
        self.fixture()
        broken = self.report(job="broken")
        self.db.execute("UPDATE item SET fields='{not json' WHERE id=?", (broken,))
        preview = self.preview()
        self.assertEqual([entry["id"] for entry in preview["selected"]], self.clean)
        self.assertIn({"id": broken, "why": UNREADABLE}, preview["declined"])

    def test_absent_empty_and_malformed_fields_are_declined_alike(self):
        """sd:873. NULL, '' and `{not json` are one case here: none can say
        the report is clean, so each is declined for a person rather than
        selected or crashed on. `json_valid(NULL)` is NULL and `json_valid('')`
        is 0, which is why the predicate is a `coalesce` and not `NOT
        json_valid`. Retention, the backlog and the page pin the same three.
        """
        self.fixture()
        for index, value in enumerate((None, "", "{not json")):
            with self.subTest(fields=value):
                broken = self.report(job=f"broken-{index}")
                self.db.execute("UPDATE item SET fields=? WHERE id=?", (value, broken))
                preview = self.preview()
                self.assertEqual([entry["id"] for entry in preview["selected"]], self.clean)
                self.assertIn({"id": broken, "why": UNREADABLE}, preview["declined"])

    def test_a_run_newer_than_its_job_s_recorded_health_is_declined(self):
        late = self.report(job="alpha", ended="2026-09-09T00:00:00+00:00")
        unwatched = self.report(job="beta", ended="2026-09-09T00:00:00+00:00")
        record_state(self.db, "heartbeat", key=reporting.HEARTBEAT_KEY + "alpha",
                     body={"healthy": True, "run_id": "old", "ended": "2026-09-08T00:00:00+00:00", "exit_code": 0})
        preview = self.preview()
        self.assertEqual(preview["declined"], [{"id": late, "why": LATE}])
        # A job with no heartbeat at all does not decline its reports.
        self.assertEqual([entry["id"] for entry in preview["selected"]], [unwatched])

    def test_the_job_s_health_is_its_newest_real_tick_not_a_rejected_stamp(self):
        """sd:10410: the tick `health` reads, the latest real `ended`. Read as the newest row by id, a
        stamp rejected as future hid that this report is later than the job's real tick."""
        late = self.report(job="alpha", ended="2026-09-09T00:00:00+00:00")
        for ended in ("2026-09-08T00:00:00+00:00", "2099-01-01T00:00:00+00:00"):
            record_state(self.db, "heartbeat", key=reporting.HEARTBEAT_KEY + "alpha",
                         body={"healthy": True, "run_id": ended, "ended": ended, "exit_code": 0})
        preview = self.preview()
        self.assertEqual(preview["selected"], [])
        self.assertEqual(preview["declined"], [{"id": late, "why": LATE}])

    def test_work_in_flight_declines_the_report(self):
        busy = self.report(job="alpha")
        create_assignment(self.db, role="author", status="queued", item=busy)
        self.assertEqual(self.preview()["declined"], [{"id": busy, "why": IN_FLIGHT}])

    def test_a_read_only_connection_gives_the_same_plan(self):
        self.fixture()
        plan = self.preview()["plan"]
        copy = self.root / "copy.db"
        target = sqlite3.connect(copy)
        self.db.backup(target)
        target.close()
        reader = sd_db.connect(copy, write=False); self.addCleanup(reader.close)
        self.assertEqual(reporting.clean_reports(reader, before=CUTOFF, now=NOW)["plan"], plan)

    def test_the_cutoff_is_a_date_meaning_midnight_utc_and_nothing_else(self):
        self.assertEqual(reporting.cutoff("2026-09-10"), CUTOFF)
        self.assertEqual(reporting.cutoff(CUTOFF), CUTOFF)
        for value in ("2026-02-30", "2026-09-10T00:00:00Z", "2026-09-10T01:00:00+00:00", "nonsense"):
            with self.subTest(value=value), self.assertRaisesRegex(workflow.WorkflowError, "YYYY-MM-DD"):
                reporting.cutoff(value)
        # A caller that skipped `cutoff` fails loudly rather than reading another instant.
        with self.assertRaisesRegex(workflow.WorkflowError, "pass the cutoff as `cutoff` stamps it"):
            reporting.clean_reports(self.db, before="2026-09-10", now=NOW)

    def test_the_dry_run_and_the_apply_take_only_the_stamp_cutoff_returns(self):
        """sd:1168: `clean_reports` passed `before` through `stamp`, which takes
        any aware instant. A library caller that skipped `cutoff` then got a
        cutoff at another time of day, and a plan over it, that neither surface
        can issue; another spelling of midnight UTC got a second plan token for
        the same selection."""
        self.fixture()
        plan = self.preview()["plan"]
        for value in ("2026-09-10T05:00:00+00:00", "2026-09-10T02:00:00+02:00", "2026-09-10T00:00:00Z",
                      "2026-09-10T00:00:00.000+00:00"):
            with self.subTest(value=value):
                with self.assertRaises(workflow.WorkflowError):
                    reporting.clean_reports(self.db, before=value, now=NOW)
                before = self.dump()
                with self.assertRaises(workflow.WorkflowError):
                    self.apply(plan, before=value)
                self.assertEqual(self.dump(), before)
        self.assertEqual(self.preview()["plan"], plan)

    def test_a_cutoff_later_than_now_is_refused(self):
        with self.assertRaises(workflow.WorkflowError):
            self.preview(now=datetime.fromisoformat(CUTOFF) - timedelta(seconds=1))
        self.assertIsNone(self.preview(now=datetime.fromisoformat(CUTOFF))["plan"])


class TheApply(Store):

    def setUp(self):
        super().setUp()
        self.fixture()
        self.plan = self.preview()["plan"]

    def batch_fields(self, result):
        return json.loads(result["item"]["fields"])

    def test_it_moves_exactly_the_previewed_reports_and_files_one_record_of_it(self):
        result = self.apply(self.plan)
        batch = result["item"]["id"]
        self.assertEqual(result["acknowledged"], self.clean)
        for item in self.clean:
            self.assertEqual(self.status_of(item), "done")
            self.assertEqual(self.newest_note(item), f"planning -> done by alex: bulk acknowledge, report #{batch}")
        for item in (self.attention, self.followed, self.record, self.later):
            self.assertEqual(self.status_of(item), "planning")
        row = self.db.execute("SELECT * FROM item WHERE id=?", (batch,)).fetchone()
        self.assertEqual((row["kind"], row["source"], row["external_id"]),
                         ("report", "cron-report", f"reports-acknowledge:{self.plan}"))
        self.assertEqual(json.loads(row["fields"])["record"], "reports-acknowledge")
        self.assertEqual(json.loads(row["body"])["text"].split(), [str(item) for item in self.clean])
        # The record is `done` from the transaction that filed it.
        self.assertEqual(row["status"], "done")
        self.assertEqual(result["item"]["status"], "done")
        self.assertEqual(self.newest_note(batch), "planning -> done by alex: bulk acknowledge record")
        self.assertEqual(result["revision"], self.revision(batch))

    def test_the_nightly_settle_never_takes_the_batch_record(self):
        """Judged at the record's own filing time plus eight days, so the record is old enough to settle."""
        batch = self.apply(self.plan)["item"]["id"]
        filed = datetime.fromisoformat(self.db.execute("SELECT created_at FROM item WHERE id=?",
                                                       (batch,)).fetchone()[0])
        later = filed + timedelta(days=8)
        self.assertLess(filed.isoformat(), retention._cutoff(later, retention.CLEAN_REPORT_AGE))
        revision = self.revision(batch)
        retention.settle_clean_reports(self.db, now=later)
        self.assertEqual(self.revision(batch), revision)

    def test_the_record_names_who_the_principal_the_program_and_the_process(self):
        actor = self.batch_fields(self.apply(self.plan))["report"]["actor"]
        self.assertEqual(set(actor), {"who", "principal", "program", "pid", "ppid", "session"})
        self.assertEqual((actor["who"], actor["principal"], actor["program"]), ("alex", "local", "test"))
        self.assertEqual((actor["pid"], actor["ppid"]), (os.getpid(), os.getppid()))

    def test_an_over_long_session_is_cut_to_two_hundred_and_the_apply_goes_on(self):
        result = self.apply(self.plan, session="x" * 250)
        self.assertEqual(self.batch_fields(result)["report"]["actor"]["session"], "x" * 200)
        self.assertEqual(result["actor"]["session"], "x" * 200)

    def assertStale(self, plan, **kwargs):
        before = self.dump()
        with self.assertRaises(workflow.StaleItem) as caught:
            self.apply(plan, **kwargs)
        self.assertIs(type(caught.exception), workflow.StaleItem)
        self.assertEqual(before, self.dump())

    def test_a_followup_after_the_preview_makes_the_plan_stale(self):
        add_note(self.db, self.clean[0], "followup", "wait", session="alex")
        self.assertStale(self.plan)

    def test_a_single_acknowledge_after_the_preview_makes_the_plan_stale(self):
        reporting.acknowledge(self.db, self.clean[0], expected_revision=self.revision(self.clean[0]), who="alex")
        self.assertStale(self.plan)

    def test_a_retention_settle_after_the_preview_makes_the_plan_stale(self):
        self.assertGreater(retention.settle_clean_reports(self.db, now=NOW + timedelta(days=8)), 0)
        self.assertStale(self.plan)

    def test_replaying_an_applied_plan_is_stale(self):
        self.apply(self.plan)
        self.assertStale(self.plan)

    def test_who_principal_and_program_must_be_named(self):
        before = self.dump()
        for kwargs in ({"who": ""}, {"who": "  "}, {"principal": ""}, {"program": ""}, {"who": "x" * 201}):
            with self.subTest(**kwargs), self.assertRaises(workflow.WorkflowError):
                self.apply(self.plan, **kwargs)
        common = {"before": CUTOFF, "expected_plan": self.plan, "program": "test", "now": NOW}
        with self.assertRaises(TypeError):
            reporting.acknowledge_clean(self.db, principal="local", **common)
        with self.assertRaises(TypeError):
            reporting.acknowledge_clean(self.db, who="alex", **common)
        self.assertEqual(before, self.dump())

    def test_a_plan_of_the_wrong_shape_is_refused_as_a_bad_request_not_a_stale_plan(self):
        before = self.dump()
        for plan in (None, "abc", "A" * 64):
            with self.subTest(plan=plan), self.assertRaises(workflow.WorkflowError) as caught:
                self.apply(plan)
            self.assertIs(type(caught.exception), workflow.WorkflowError)
        self.assertEqual(before, self.dump())

    def test_a_selection_that_became_empty_is_a_stale_plan(self):
        for item in self.clean:
            reporting.acknowledge(self.db, item, expected_revision=self.revision(item), who="alex")
        self.assertStale(self.plan)
        self.assertIsNone(self.preview()["plan"])

    def test_a_selection_past_the_batch_limit_is_a_stale_plan(self):
        with mock.patch.object(reporting, "MAX_BATCH", 1):
            self.assertStale(self.plan)
            preview = self.preview()
        self.assertIsNone(preview["plan"])
        self.assertEqual([entry["revision"] for entry in preview["selected"]], [None, None])

    def test_a_report_filed_after_the_preview_is_not_swept(self):
        arrived = self.report(job="late-arrival", created_at="2026-09-11T00:00:00+00:00")
        self.apply(self.plan)
        self.assertEqual(self.status_of(arrived), "planning")

    def test_a_report_backdated_into_the_selection_after_the_preview_makes_the_plan_stale(self):
        self.report(job="backdated", created_at="2026-09-08T00:00:00+00:00")
        self.assertStale(self.plan)

    def test_a_later_preview_lists_the_batch_record_nowhere(self):
        batch = self.apply(self.plan)["item"]["id"]
        filed = datetime.fromisoformat(self.db.execute("SELECT created_at FROM item WHERE id=?",
                                                       (batch,)).fetchone()[0])
        before = reporting.cutoff((filed + timedelta(days=1)).date().isoformat())
        preview = reporting.clean_reports(self.db, before=before, now=filed + timedelta(days=2))
        self.assertNotIn(batch, [entry["id"] for entry in preview["selected"] + preview["declined"]])


#: An `attention` key that is not there at all.
MISSING = object()


class TheSelectionIsNeverWiderThanRetention(unittest.TestCase):
    """200 seeded stores, each judged against two answers that do not share its code.

    The oracle reads the Python records the fixture was built from, not the
    store, and states retention's rule itself. Retention runs on a copy at the
    cutoff plus its seven days. `clean_reports` may refuse more than either,
    never less.
    """

    CUT = datetime(2026, 9, 10, tzinfo=UTC)
    OFFSETS = (timedelta(days=-9), timedelta(days=-1), timedelta(seconds=-1), timedelta(0),
               timedelta(seconds=1), timedelta(days=1))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.template = self.root / "template.db"; initialise(self.template)

    @classmethod
    def oracle(cls, records):
        return {record["id"] for record in records
                if record["status"] == "planning" and record["attention"] is not MISSING
                and record["attention"] is not True and record["attention"] in (False, 0)
                and record["created_at"] < cls.CUT and record["followup"] != "open"}

    def build(self, seed):
        rng = random.Random(seed)
        path = self.root / f"{seed}.db"
        path.write_bytes(self.template.read_bytes())
        connection = sd_db.connect(path)
        records = []
        for number in range(rng.randint(0, 10)):
            record = {"attention": rng.choice((True, False, MISSING, 0)), "followup": rng.choice((None, "open", "resolved")),
                      "status": rng.choice(("planning", "done")), "record": rng.choice((None, "reports-acknowledge")),
                      "created_at": self.CUT + rng.choice(self.OFFSETS)}
            stamp = record["created_at"].isoformat()
            fields = {"report": {"job": f"job-{number}", "ended": stamp}}
            if record["attention"] is not MISSING:
                fields["attention"] = record["attention"]
            if record["record"] is not None:
                fields["record"] = record["record"]
            record["id"] = create_item(connection, kind="report", title=f"job-{number}: run report",
                                       status=record["status"], fields=fields, created_at=stamp)
            if record["followup"]:
                note = add_note(connection, record["id"], "followup", "look", session="fixture")
                if record["followup"] == "resolved":
                    resolve_note(connection, note)
            records.append(record)
        return connection, records

    def test_every_selected_report_is_one_retention_would_settle(self):
        chosen = 0
        for seed in range(200):
            connection, records = self.build(seed)
            selected = {entry["id"] for entry in reporting.clean_reports(
                connection, before=self.CUT.isoformat(), now=self.CUT + timedelta(days=1))["selected"]}
            copy = sqlite3.connect(":memory:", isolation_level=None)
            connection.backup(copy)
            copy.row_factory = sqlite3.Row
            retention.settle_clean_reports(copy, now=self.CUT + retention.CLEAN_REPORT_AGE)
            moved = ({row[0] for row in copy.execute("SELECT id FROM item WHERE status='done'")}
                     - {row[0] for row in connection.execute("SELECT id FROM item WHERE status='done'")})
            copy.close()
            connection.close()
            refused = {record["id"]: record for record in records if record["id"] in selected - self.oracle(records)}
            self.assertEqual(refused, {}, f"seed {seed}: the Python oracle refuses these selected reports")
            self.assertLessEqual(selected, moved, f"seed {seed}: retention on a copy did not settle these")
            chosen += len(selected)
        # An empty selection passes both checks, so the fixtures must have selected something.
        self.assertGreater(chosen, 0)


class AReplayAfterABulkAcknowledge(Store):

    def ingest(self, run, ended, *, exit_code, text):
        log = self.root / f"{run}.log"
        if not log.exists():
            log.write_text(text)
        with mock.patch.object(writes, "now", return_value=ended):
            return reporting.ingest_log(self.db, job="probe", run_id=run, started="2026-09-10T00:00:00+00:00",
                                        ended=ended, exit_code=exit_code, log_path=log,
                                        now=datetime(2026, 9, 10, 1, tzinfo=UTC))

    def counts(self):
        return tuple(self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("item", "note"))

    def test_replaying_an_acknowledged_recovery_returns_it_done_and_writes_nothing(self):
        # A recovery report as the version before sd:3509 filed one, beside the heartbeat it wrote.
        failed = self.ingest("a", "2026-09-10T00:00:00+00:00", exit_code=1, text="boom\n")
        self.assertEqual(self.ingest("b", "2026-09-10T00:15:00+00:00", exit_code=0, text="ok\n")["why"],
                         "the job recovered")
        with mock.patch.object(writes, "now", return_value="2026-09-10T00:15:00+00:00"):
            recovered = reporting.ingest(self.db, job="probe", run_id="b", started="2026-09-10T00:00:00+00:00",
                                         ended="2026-09-10T00:15:00+00:00", exit_code=0, text="ok\n",
                                         source_path=str((self.root / "b.log").resolve()),
                                         attention_basis="not supplied by the job")
        self.assertEqual(recovered["item"]["created_at"], "2026-09-10T00:15:00+00:00")
        followup, = [note["id"] for note in failed["notes"] if note["kind"] == "followup"]
        resolve_note(self.db, followup)
        before = reporting.cutoff("2026-09-11")
        preview = reporting.clean_reports(self.db, before=before, now=NOW)
        self.assertEqual([entry["id"] for entry in preview["selected"]], [recovered["item"]["id"]])
        reporting.acknowledge_clean(self.db, before=before, expected_plan=preview["plan"], who="alex",
                                    principal="local", program="test", now=NOW)
        counts = self.counts()
        again = self.ingest("b", "2026-09-10T00:15:00+00:00", exit_code=0, text="ok\n")
        self.assertEqual((again["item"]["id"], again["item"]["status"]), (recovered["item"]["id"], "done"))
        self.assertEqual(self.counts(), counts)


class IngestTakesAnActorAndARecord(Store):
    """The two `ingest` keywords sd:754 and sd:755 share: names, placement, validation."""

    def ingest(self, **kwargs):
        arguments = {"job": "item-remove", "run_id": "one", "started": FILED, "ended": FILED, "exit_code": 0,
                     "text": "removed\n", "source_path": "test", **kwargs}
        return reporting.ingest(self.db, **arguments)

    def test_a_bad_actor_or_record_is_refused_and_writes_nothing(self):
        before = self.dump()
        for kwargs in ({"actor": "alex"}, {"actor": {f"key{number}": number for number in range(11)}},
                       {"actor": {"who": "x" * 201}}, {"record": "Bad Name"}):
            with self.subTest(kwargs=str(kwargs)[:40]), self.assertRaises(workflow.WorkflowError):
                self.ingest(**kwargs)
        self.assertEqual(before, self.dump())

    def test_they_are_stored_in_place_and_a_replay_returns_the_same_report(self):
        actor = {"who": "alex", "principal": "local", "program": "test", "pid": 1, "ppid": None,
                 "session": "s", "reason": "x" * 200}
        state = self.ingest(actor=actor, record="reports-acknowledge")
        item = state["item"]["id"]
        self.assertEqual(self.db.execute("SELECT json_extract(fields, '$.record') FROM item WHERE id=?",
                                         (item,)).fetchone()[0], "reports-acknowledge")
        self.assertEqual(json.loads(state["item"]["fields"])["report"]["actor"], actor)
        self.assertEqual(self.ingest(actor=actor, record="reports-acknowledge")["item"]["id"], item)

    def test_without_them_the_fields_are_what_they_always_were(self):
        state = self.ingest()
        self.assertEqual(state["item"]["fields"], json.dumps({"attention": False, "report": {
            "job": "item-remove", "run_id": "one", "started": FILED, "ended": FILED, "exit_code": 0,
            "source_path": "test", "truncated": False, "text_sha256": hashlib.sha256(b"removed\n").hexdigest(),
            "attention_basis": "explicit report"}}, sort_keys=True))
        self.assertIsNone(re.search(r'"(actor|record)"', state["item"]["fields"]))


if __name__ == "__main__": unittest.main()
