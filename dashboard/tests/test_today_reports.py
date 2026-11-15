"""Briefs and problem reports on Today (operator ruling 2026-10-17): what the reports view used to push by ntfy or mail.

The rows are the database's: a routine brief filed in the last week and not marked seen, and an open report that asks for a
person. Seen is a Today snooze; Acknowledge is the Reports page's route. A read that fails is its reason, never an empty list.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sd_dashboard import now_screen, server, today_reports, v2
from sd_db import reporting, routine_briefs, writes
from support import NOW, ScreenCase
from test_now_screen import JobsBackend, fleet_document

V2 = Path(v2.__file__).resolve().parent
JS = V2 / "static" / "today-reports.js"
REPO = "/repos/system"


class ReportsCase(ScreenCase):
    def setUp(self):
        super().setUp()
        self.repo(REPO)

    def brief(self, stem, text="Three things moved overnight.", at="2026-09-05T12:00:00Z"):
        return routine_briefs.add(self.connection, stem, f"---\nseries: x\n---\n\n{text}\n", repo=REPO, who="test", at=at)

    def report(self, job, run, *, exit_code=1, attention=True, basis="job exited 1 on host.example.test", text="the log tail"):
        return reporting.ingest(self.connection, job=job, run_id=run, started="2026-09-06T09:00:00Z",
                                ended="2026-09-06T09:01:00Z", exit_code=exit_code, text=text, source_path="/tmp/x.log",
                                attention=attention, attention_basis=basis)["item"]["id"]

    def read(self, snoozes=None):
        return today_reports.document(self.connection, now=NOW, snoozes=snoozes or {})

    def today(self):
        return now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name), fleet=fleet_document)


class TheProblems(ReportsCase):
    def test_an_open_attention_report_shows_with_its_reason_and_revision(self):
        item = self.report("nas-full-copy", "r1", basis="nas-full-copy: 3 skipped downloads\nsecond finding")
        doc = self.read()
        self.assertEqual((doc["problems_error"], doc["problems_more"]), ("", 0))
        [row] = doc["problems"]
        self.assertEqual((row["id"], row["item"], row["job"], row["why"], row["fields_read"]),
                         (f"report:{item}", item, "nas-full-copy", "nas-full-copy: 3 skipped downloads", True))
        self.assertEqual(row["revision"], reporting.workflow.item_state(self.connection, item)["revision"])

    def test_an_acknowledged_report_leaves_the_list(self):
        item = self.report("hoa-core-bills", "r1")
        revision = reporting.workflow.item_state(self.connection, item)["revision"]
        # The Today button posts the Reports page's route; this is the function that route runs.
        server._acknowledge_report(self.connection, item, expected_revision=revision)
        self.assertEqual(self.read()["problems"], [])

    def test_a_quiet_run_report_and_a_brief_are_not_problems(self):
        self.report("cleanup", "r1", exit_code=0, attention=False, basis="")
        self.brief("2026-09-05 - Daily Intel Brief")
        self.assertEqual(self.read()["problems"], [])

    def test_a_report_whose_fields_cannot_be_read_shows_and_says_so(self):
        item = self.report("odd", "r1")
        self.connection.execute("UPDATE item SET fields = 'not json' WHERE id = ?", (item,))
        [row] = self.read()["problems"]
        self.assertEqual((row["item"], row["fields_read"], row["why"]), (item, False, "its fields cannot be read"))

    def test_a_failed_read_is_its_reason_never_an_empty_list(self):
        with mock.patch.object(today_reports.reads, "revisions", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.read()
        self.assertEqual((doc["problems"], doc["problems_error"]), ([], "database is locked"))

    def test_a_full_read_marks_its_counts_as_at_least(self):
        for run in ("r1", "r2", "r3"):
            self.report(f"job-{run}", run)
        with mock.patch.object(today_reports, "SHOWN", 1):
            whole = self.read()
            with mock.patch.object(today_reports, "READ", 2):
                full = self.read()
        self.assertEqual((len(whole["problems"]), whole["problems_more"], whole["problems_capped"]), (1, 2, False))
        self.assertEqual((len(full["problems"]), full["problems_more"], full["problems_capped"]), (1, 1, True))

    def test_an_open_report_behind_newer_quiet_ones_still_shows(self):
        # sd:8795: the read filtered open reports after its LIMIT, so newer
        # quiet reports filling the read hid an older open one.
        item = self.report("nas-full-copy", "r1")
        self.report("cleanup", "r2", exit_code=0, attention=False, basis="")
        with mock.patch.object(today_reports, "READ", 1):
            [row] = self.read()["problems"]
        self.assertEqual(row["item"], item)

    def test_open_reports_count_in_todays_badge(self):
        self.report("nas-full-copy", "r1")
        today = self.today()
        self.assertEqual(len(today["problems"]), 1)
        self.assertEqual(today["attention"], {"state": "caution", "n": 1, "what": "rows to look at"})


class TheClaims(ReportsCase):
    """pack sd:7683: a report another session already took names who has it, so Today offers no second Fix on it.

    Taken is `in_progress`, by the status change's `who`, or a note the dashboard's Fix wrote (`fix_session.NOTE`), by
    its session. A report nobody took has no claim.
    """

    def test_an_untaken_report_has_no_claim(self):
        self.report("nas-full-copy", "r1")
        [row] = self.read()["problems"]
        self.assertIsNone(row["claim"])

    def test_an_in_progress_report_names_who_moved_it(self):
        item = self.report("nas-full-copy", "r1")
        writes.transition(self.connection, item, "in_progress", who="hub-triage", reason="triage")
        [row] = self.read()["problems"]
        self.assertEqual(row["claim"], "hub-triage has it (in_progress)")
        today = self.today()
        [needs] = [r for r in today["needs"] if r["id"] == f"report:{item}"]
        self.assertIn("hub-triage has it (in_progress)", needs["detail"])

    def test_a_fix_note_names_its_session_and_pane(self):
        from sd_dashboard import fix_session
        from sd_db import workflow

        item = self.report("nas-full-copy", "r1")
        body = fix_session.NOTE.format(session="debug", item=item, pane="w9:p1", where="~/repos/system")
        workflow.add_item_note(self.connection, item, kind="comment", who="dashboard", body=body)
        [row] = self.read()["problems"]
        self.assertEqual(row["claim"], "dashboard has it (Fix: debug session, herdr pane w9:p1)")

    def test_a_fix_note_older_than_the_claim_window_no_longer_holds_the_report(self):
        """pack sd:7733: one Fix attempt kept Fix off on Today forever; a launch note claims for `CLAIM_HOURS`."""
        from sd_dashboard import fix_session
        from sd_db import workflow

        item = self.report("nas-full-copy", "r1")
        body = fix_session.NOTE.format(session="debug", item=item, pane="w9:p1", where="~/repos/system")
        workflow.add_item_note(self.connection, item, kind="comment", who="dashboard", body=body)
        [note] = self.connection.execute("SELECT max(id) FROM note WHERE item = ?", (item,)).fetchone()
        hours = today_reports.CLAIM_HOURS
        for age, held in ((hours - 1, True), (hours + 1, False)):
            stamp = (datetime.fromisoformat(NOW.replace("Z", "+00:00")) - timedelta(hours=age)).isoformat()
            self.connection.execute("UPDATE note SET timestamp = ? WHERE id = ?", (stamp, note))
            [row] = self.read()["problems"]
            self.assertEqual(row["claim"] is not None, held, f"a Fix note {age} h old")


class TheBriefs(ReportsCase):
    def test_a_brief_from_the_last_week_shows_newest_first_and_an_older_one_does_not(self):
        self.brief("2026-09-04 - Weekly Intel Synthesis", at="2026-09-04T11:00:00Z")
        self.brief("2026-09-05 - Daily Intel Brief")
        self.brief("2026-08-20 - Daily Intel Brief", at="2026-08-20T12:00:00Z")
        doc = self.read()
        self.assertEqual(doc["briefs_error"], "")
        self.assertEqual([row["stem"] for row in doc["briefs"]],
                         ["2026-09-05 - Daily Intel Brief", "2026-09-04 - Weekly Intel Synthesis"])
        first = doc["briefs"][0]
        self.assertEqual((first["id"], first["until"], first["lead"]),
                         ("brief:2026-09-05 - Daily Intel Brief", "forever", "Three things moved overnight."))
        self.assertTrue(first["seen"])

    def test_a_brief_marked_seen_leaves_today_until_its_text_grows(self):
        self.brief("2026-09-05 - Daily Intel Brief")
        row = self.read()["briefs"][0]
        # Seen is the snooze Today already writes (POST /api/snooze), held until the brief changes (sd:5752): the place
        # pages read the same mark, and a brief that aged off Today still reads seen there.
        writes.snooze(self.connection, f"today:{row['id']}", row["until"], seen=row["seen"], now=NOW)
        self.assertEqual([r["stem"] for r in self.today()["briefs"]], [])
        routine_briefs.add(self.connection, "2026-09-05 - Daily Intel Brief",
                           "---\nseries: x\n---\n\nThree things moved overnight.\n\nA fourth.\n", repo=REPO, who="test",
                           mode="extend")
        self.assertEqual([r["stem"] for r in self.today()["briefs"]], ["2026-09-05 - Daily Intel Brief"])

    def test_a_failed_brief_read_is_its_reason_never_an_empty_list(self):
        with mock.patch.object(today_reports.briefs_screen, "_library", side_effect=ValueError("the installed sd_db has no routine_briefs")):
            doc = self.read()
        self.assertEqual((doc["briefs"], doc["briefs_error"]), ([], "the installed sd_db has no routine_briefs"))
        self.assertEqual(doc["problems_error"], "")


class ThePage(ReportsCase):
    def test_today_draws_reports_and_briefs_as_needs_rows_not_their_own_tables(self):
        # sd:5921: one list; today-reports.js and its two tables are gone.
        html = (V2 / "today.html").read_text(encoding="utf-8")
        for gone in ('<tbody id="problems">', '<tbody id="briefs">', "today-reports.js"):
            self.assertNotIn(gone, html)
        self.assertFalse(JS.exists())
        self.report("nas-full-copy", "r1")
        self.brief("2026-09-05 - Daily Intel Brief")
        needs = {row["kind"]: row for row in self.today()["needs"]}
        self.assertEqual((needs["report"]["band"], needs["report"]["lamp"]), ("look", "reports"))
        self.assertEqual((needs["brief"]["band"], needs["brief"]["source"]), ("queued", "briefs"))
