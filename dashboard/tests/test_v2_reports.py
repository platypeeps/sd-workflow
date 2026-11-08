"""The v2 Reports page (sd:2121).

What this slice promises: `/reports` answers under the shared policy and loads
its script before `shell.js`; `/api/reports` gives the newest reports v1
lists, each with its provenance, the revision the acknowledge route checks
and the followups that hold it, the launchd jobs, each job's runs per local
day from its `cron-jobs.sh` log, and the job families the
`dashboard.report_families` setting row names (sd:5109; job names are the
operator's, so the public checkout carries none). A source that fails is
`null` with its reason. The page registers the design's Reports commands with the
design's ids, labels, keys and risks; Acknowledge posts v1's route, and its
Undo posts `/api/reports/<n>/reopen` with the revision the acknowledge answered
(sd:2395). The page reads through the shared reader, `read.js` (sd:2487).

`reports.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` uses. The browser half -- the look at 375 px,
focus -- is a manual check recorded on the pull request.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest import mock

from sd_dashboard import reports_screen, server, v2
from sd_db import reporting, workflow
from support import ScreenCase
from test_now_screen import JobsBackend
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_shell_shared import cell_grammar
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
REPORTS_JS = (V2 / "static" / "reports.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")
REPORT_DETAILS_JS = (V2 / "static" / "report-details.js").read_text(encoding="utf-8")
#: A family list, as the `dashboard.report_families` row or its old file holds it (sd:5109); the job names are examples.
FAMILIES = """family|backups|Backups|hard-drive-download|db-backup-nightly,offsite-verify
family|repos|Repos & setup|git-compare|repo-sync-nightly,brew-doctor-nightly
family|host|Host health|heart-pulse|health-check-nightly,watchdog-daily
family|agents|Agent routines|bot|morning-brief,weekly-digest
"""
#: The machine's offset in the fixtures: the logs write -0600, so local days are those dates.
TZ = timezone(timedelta(hours=-6))
#: Two days after the newest fixture report.
LATER = "2026-09-10T12:00:00Z"
DAYS = ["2026-09-04", "2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"]

#: The design's Reports commands (design source products/system/designs/pages/reports.js): id, object type, label, key, risk.
COMMANDS = [
    ["report.ack", "report", "Acknowledge", "a", "undo"],  # the design's risk: reporting.reopen is its Undo (sd:2395)
    ["report.resolve", "report", "Resolve followup", "e", "confirm"],  # sd:5064: no route reopens a note, so it asks
    ["report.show", "report", "Open item", "o", "safe"],
    ["report.log", "report", "Show log", "l", "safe"],
    ["report.task", "report", "Make task", "k", "safe"],
    ["report.fix", "report", "Fix", "f", "safe"],  # sd:3456: a debug session in herdr; asks first
    ["jobs.retry", "job", "Run now", "t", "safe"],
]


class Jobs(JobsBackend):
    """`JobsBackend` with a launchd calendar per job, daily at 02:15 unless `schedules` names another, and a log written
    line by line. An empty calendar is a manual job (sd:4965)."""

    def __init__(self, root, jobs=(), schedules=None, refuse=""):
        super().__init__(root, jobs=jobs, refuse=refuse)
        self.schedules = schedules or {}

    def inspect(self, name):
        return super().inspect(name) | {"schedule": self.schedules.get(name, [{"Hour": 2, "Minute": 15}])}

    def write(self, name, lines):
        (self.cron_root / "logs" / f"{name}.log").write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def run_lines(job, day, outcome="done", at="02:15:00"):
    return [f"[{job}] {day}T{at}-0600 starting (cwd: /tmp)", f"[{job}] {day}T{at}-0600 {outcome}"]


def seed(case):
    """A failing nightly job and its report, a clean report, an acknowledged one, and the logs behind them."""
    ids = {}
    ids["failed"] = reporting.ingest(case.connection, job="nightly-sync", run_id="r1", started="2026-09-08T08:15:00Z",
                                     ended="2026-09-08T08:16:00Z", exit_code=7, text="line one\nmirror failed\n",
                                     source_path="/home/example/repos/system/local-cron-jobs/logs/nightly-sync.log",
                                     attention=True, attention_basis="job exited 7")["item"]["id"]
    ids["clean"] = reporting.ingest(case.connection, job="weekly-scan", run_id="r2", started="2026-09-06T13:00:00Z",
                                    ended="2026-09-06T13:01:00Z", exit_code=0, text="x" * 2000,
                                    source_path="sd-db scan")["item"]["id"]
    done = reporting.ingest(case.connection, job="weekly-scan", run_id="r0", started="2026-09-05T13:00:00Z",
                            ended="2026-09-05T13:01:00Z", exit_code=0, text="old", source_path="sd-db scan")
    ids["done"] = done["item"]["id"]
    # `ingest` stamps the real clock; backdate each report to its run, as the store would hold it (seeding, not a writer).
    for key, at in (("failed", "2026-09-08T08:16:00+00:00"), ("clean", "2026-09-06T13:01:00+00:00"), ("done", "2026-09-05T13:01:00+00:00")):
        case.connection.execute("UPDATE item SET created_at = ? WHERE id = ?", (at, ids[key]))
    case.connection.commit()
    reporting.acknowledge(case.connection, ids["done"], expected_revision=workflow.item_state(case.connection, ids["done"])["revision"],
                          who="fixture")
    case.jobs = Jobs(case.dir, jobs=[("nightly-sync", "failed", 7, None), ("weekly-scan", "idle", 0, None),
                                     ("quiet-one", "idle", 0, None)],
                     schedules={"weekly-scan": [{"Weekday": 0, "Hour": 7}]})  # Sundays: 2026-09-06
    case.jobs.write("nightly-sync", [line for day in DAYS[2:6] for line in run_lines("nightly-sync", day)]
                    + run_lines("nightly-sync", DAYS[6], "FAILED rc=7") + run_lines("nightly-sync", DAYS[6], "FAILED rc=7", "03:00:00"))
    case.jobs.write("weekly-scan", run_lines("weekly-scan", DAYS[2]))
    return ids


ABSENT = object()


class TheDocument(ScreenCase):
    """`reports_screen.document` gives every source the page reads, each guarded on its own."""

    def setUp(self):
        super().setUp()
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ids = seed(self)
        self.enterContext(mock.patch.object(reports_screen, "_html_folders", lambda: 3))
        self.families = self.dir / "families.conf"
        self.families.write_text(FAMILIES, encoding="utf-8")

    def doc(self, **kw):
        kw.setdefault("config", self.families)
        return reports_screen.document(self.connection, now=LATER, jobs=self.jobs, tz=TZ, **kw)

    def test_each_report_carries_its_provenance_revision_and_the_followup_that_holds_it(self):
        doc = self.doc()
        self.assertEqual(doc["sources"], {"reports": "", "jobs": "", "cadence": "", "html": ""})
        rows = {r["id"]: r for r in doc["reports"]}
        failed = rows[self.ids["failed"]]
        self.assertEqual((failed["job"], failed["exit"], failed["basis"], failed["attention"], failed["status"]),
                         ("nightly-sync", 7, "job exited 7", True, "planning"))
        self.assertEqual(failed["revision"], workflow.item_state(self.connection, self.ids["failed"])["revision"])
        notes = [n["id"] for n in workflow.item_state(self.connection, self.ids["failed"])["notes"] if n["kind"] == "followup"]
        self.assertEqual(failed["followups"], notes)
        self.assertEqual(len(notes), 1)
        clean = rows[self.ids["clean"]]
        self.assertEqual((clean["attention"], clean["followups"], clean["cut"], len(clean["body"])), (False, [], True, 1400))
        self.assertEqual(rows[self.ids["done"]]["status"], "done")
        # sd:2212: no status mail is read and none is planned, so the reading carries no mail key.
        self.assertNotIn("mail", doc)
        self.assertEqual(doc["html"], 3)
        # sd:3446: the rail badge is the read's; only the open report that asks for attention counts.
        self.assertEqual(doc["attention"], {"state": "warning", "n": 1, "what": "reports want you"})
        self.assertEqual(reports_screen._attention(None), {"state": "unknown", "n": 0, "what": "reports not read"})

    def test_truncated_reads_the_kept_body_not_the_first_run(self):
        # sd:4652: pack sd:4641 lets a later whole body replace a cut first run, and `repeats.body_truncated` says
        # what the kept body is; `report.truncated` stays the first run's and is only the fallback.
        item = self.ids["failed"]
        fields = json.loads(self.connection.execute("SELECT fields FROM item WHERE id = ?", (item,)).fetchone()[0])
        # ABSENT: no `body_truncated` key; a null one is the pack's `_kept_cut` reading, not a fallback.
        for first, kept, shown in ((True, ABSENT, True), (True, False, False), (False, True, True), (False, ABSENT, False),
                                   (True, None, False)):
            with self.subTest(first=first, kept=kept):
                fields["report"]["truncated"] = first
                fields["repeats"] = {"count": 2} if kept is ABSENT else {"count": 2, "body_truncated": kept}
                self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (json.dumps(fields), item))
                self.connection.commit()
                self.assertIs({r["id"]: r for r in self.doc()["reports"]}[item]["truncated"], shown)

    def test_the_basis_is_the_current_set_not_the_first_run(self):
        # sd:4664: pack sd:4658 folds a smaller set into the open report and keeps it in `repeats.basis`;
        # `report.attention_basis` stays the first run's and is the fallback, as the pack's CURRENT_BASIS reads.
        item = self.ids["failed"]
        fields = json.loads(self.connection.execute("SELECT fields FROM item WHERE id = ?", (item,)).fetchone()[0])
        fields["report"]["attention_basis"] = "a\nb"
        for repeats, shown in (({"count": 2}, "a\nb"), ({"count": 2, "basis": "a"}, "a"), ({"count": 2, "basis": None}, "a\nb")):
            with self.subTest(repeats=repeats):
                fields["repeats"] = repeats
                self.connection.execute("UPDATE item SET fields = ? WHERE id = ?", (json.dumps(fields), item))
                self.connection.commit()
                self.assertEqual({r["id"]: r for r in self.doc()["reports"]}[item]["basis"], shown)

    def test_the_cadence_counts_each_local_day_from_the_log_and_the_calendar_leaves_days_out(self):
        doc = self.doc()
        self.assertEqual(doc["days"], DAYS)
        sync = doc["cadence"]["nightly-sync"]
        self.assertEqual((sync["log"], sync["from"], sync["read_from"]), (True, "2026-09-06", None))
        self.assertEqual([sync["runs"][d] for d in DAYS], [[0, 0], [0, 0], [1, 0], [1, 0], [1, 0], [1, 0], [0, 2]])
        self.assertEqual(sync["scheduled"], [True] * 7)
        # Weekday 0 is Sunday: 2026-09-06 only.
        self.assertEqual(doc["cadence"]["weekly-scan"]["scheduled"], [d == "2026-09-06" for d in DAYS])
        self.assertEqual(doc["cadence"]["quiet-one"], {"log": False, "from": None, "read_from": None, "cut": False, "runs": {}, "manual": False,
                                                       "scheduled": [True] * 7, "last": {"day": DAYS[5], "read": False, "runs": [0, 0]}})
        # The last scheduled day before today, whatever the cadence, and whether the log holds it.
        self.assertEqual(sync["last"], {"day": DAYS[5], "read": True, "runs": [1, 0]})
        self.assertEqual(doc["cadence"]["weekly-scan"]["last"], {"day": "2026-09-06", "read": True, "runs": [1, 0]})

    def test_a_job_scheduled_less_often_than_weekly_names_its_last_scheduled_run_and_whether_it_was_read(self):
        logs = self.jobs.cron_root / "logs"
        (logs / "monthly.log").write_text("\n".join(run_lines("monthly", "2026-09-01", "FAILED rc=2")) + "\n", encoding="utf-8")
        (logs / "late-log.log").write_text("\n".join(run_lines("late-log", "2026-09-05")) + "\n", encoding="utf-8")
        (logs / "yearly.log").write_text("\n".join(run_lines("yearly", "2026-09-05")) + "\n", encoding="utf-8")
        jobs = [{"name": "monthly", "schedule": [{"Day": 1, "Hour": 3}]},
                {"name": "late-log", "schedule": [{"Day": 1, "Hour": 3}]},  # its log begins after its last scheduled run
                {"name": "yearly", "schedule": [{"Month": 1, "Day": 1, "Hour": 3}]},
                {"name": "never", "schedule": [{"Month": 2, "Day": 30}]},
                {"name": "no-log", "schedule": [{"Day": 1, "Hour": 3}]}]
        got = reports_screen.cadence(jobs, logs, DAYS)
        self.assertEqual(got["monthly"]["scheduled"], [False] * 7)
        self.assertEqual(got["monthly"]["last"], {"day": "2026-09-01", "read": True, "runs": [0, 1]})
        self.assertEqual(got["late-log"]["last"], {"day": "2026-09-01", "read": False, "runs": [0, 0]})
        self.assertEqual(got["yearly"]["last"], {"day": "2026-01-01", "read": False, "runs": [0, 0]})
        self.assertEqual(got["never"]["last"], {"day": None, "read": False, "runs": [0, 0]})
        self.assertEqual(got["no-log"]["last"], {"day": "2026-09-01", "read": False, "runs": [0, 0]})
        self.assertNotIn("2026-09-01", got["monthly"]["runs"])
        with mock.patch.object(reports_screen, "TAIL_BYTES", 60):
            self.assertEqual(reports_screen.cadence(jobs[:1], logs, DAYS)["monthly"]["last"]["read"], False)

    def test_a_manual_job_is_never_due_and_its_runs_still_count(self):
        """sd:4965: `JOB_SCHEDULE="manual"` installs no calendar; a day without a run is no gap, and a run still counts."""
        logs = self.jobs.cron_root / "logs"
        (logs / "by-hand.log").write_text("\n".join(run_lines("by-hand", DAYS[3])) + "\n", encoding="utf-8")
        got = reports_screen.cadence([{"name": "by-hand", "schedule": []}], logs, DAYS)["by-hand"]
        self.assertEqual((got["manual"], got["scheduled"], got["last"]), (True, [False] * 7, {"day": None, "read": False, "runs": [0, 0]}))
        self.assertEqual(got["runs"][DAYS[3]], [1, 0])
        self.assertIs(reports_screen.cadence([{"name": "by-hand", "schedule": [{"Hour": 1}]}], logs, DAYS)["by-hand"]["manual"], False)

    def test_an_outcome_marker_after_job_output_with_no_newline_still_counts(self):
        # cron-jobs.sh's own example: `JOB_COMMAND='printf error; exit 7'` writes `error[demo] ... FAILED rc=7`.
        path = self.dir / "demo.log"
        path.write_text("[demo] 2026-09-09T02:15:00-0600 starting (cwd: /tmp)\n"
                        "error[demo] 2026-09-09T02:15:01-0600 FAILED rc=7\n"
                        "[demo] 2026-09-10T02:15:00-0600 starting (cwd: /tmp)\n"
                        "all fine[demo] 2026-09-10T02:15:01-0600 done\n", encoding="utf-8")
        got = reports_screen.job_log(path, DAYS)
        self.assertEqual(got["runs"]["2026-09-09"], [0, 1])
        self.assertEqual(got["runs"]["2026-09-10"], [1, 0])

    def test_an_outcome_marker_for_another_job_in_this_jobs_output_does_not_count(self):
        # Job output is copied verbatim, so a job that prints another job's log carries that job's markers.
        path = self.dir / "demo.log"
        path.write_text("[demo] 2026-09-09T02:15:00-0600 starting (cwd: /tmp)\n"
                        "[other] 2026-09-09T01:00:00-0600 FAILED rc=1\n"
                        "copied[other] 2026-09-08T01:00:00-0600 done\n"
                        "[demo] 2026-09-09T02:15:01-0600 done\n", encoding="utf-8")
        got = reports_screen.job_log(path, DAYS)
        self.assertEqual(got["runs"]["2026-09-09"], [1, 0])
        self.assertEqual(got["runs"]["2026-09-08"], [0, 0])

    def test_a_log_that_cannot_be_read_is_no_log_with_its_reason_and_a_fifo_does_not_block(self):
        """sd:3532 (review 2026-10-12, A1 F7, F11): Reports read the log as Management does, without blocking."""
        import os
        import threading

        (self.dir / "folder.log").mkdir()
        os.mkfifo(self.dir / "pipe.log")
        got = {}
        reader = threading.Thread(target=lambda: got.update({name: reports_screen.job_log(self.dir / f"{name}.log", DAYS)
                                                             for name in ("folder", "pipe")}), daemon=True)
        reader.start()
        reader.join(5)
        self.assertFalse(reader.is_alive(), "reading a FIFO log blocked the document")
        for name in ("folder", "pipe"):
            with self.subTest(log=name):
                self.assertEqual((got[name]["log"], got[name]["runs"]), (False, {}))
                self.assertTrue(got[name]["reason"].startswith("the log is "), got[name]["reason"])

    def test_a_log_longer_than_the_read_says_where_the_read_began(self):
        lines = [f"[big] 2026-09-0{n}T01:00:00-0600 done" for n in range(4, 10) for _ in range(200)]
        path = self.dir / "big.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with mock.patch.object(reports_screen, "TAIL_BYTES", 40 * 300):
            got = reports_screen.job_log(path, DAYS)
        self.assertEqual(got["from"], "2026-09-04")
        self.assertEqual(got["read_from"], "2026-09-08")
        self.assertEqual(got["runs"]["2026-09-04"], [0, 0])
        self.assertEqual(got["runs"]["2026-09-09"], [200, 0])

    def test_a_cut_inside_one_line_longer_than_the_read_reads_no_day(self):
        # The read starts inside a line longer than TAIL_BYTES, so no whole line, and no day, was read.
        logs = self.jobs.cron_root / "logs"
        (logs / "wide.log").write_text("\n".join(run_lines("wide", "2026-09-06")) + "\n" + "y" * 500 + "\n", encoding="utf-8")
        with mock.patch.object(reports_screen, "TAIL_BYTES", 60):
            got = reports_screen.cadence([{"name": "wide", "schedule": [{"Weekday": 0, "Hour": 7}]}], logs, DAYS)["wide"]
        self.assertEqual((got["cut"], got["read_from"]), (True, None))
        self.assertEqual(got["last"], {"day": "2026-09-06", "read": False, "runs": [0, 0]})
        self.assertFalse(reports_screen.job_log(logs / "wide.log", DAYS)["cut"])

    def test_a_failed_source_is_null_with_its_reason_and_the_rest_still_count(self):
        self.jobs.refuse = "launchctl is not here"
        doc = self.doc()
        self.assertIsNone(doc["jobs"])
        self.assertIsNone(doc["cadence"])
        self.assertEqual(doc["sources"]["jobs"], "launchctl is not here")
        self.assertIn("job list was not read", doc["sources"]["cadence"])
        self.assertEqual(len(doc["reports"]), 3)

    def test_families_come_from_the_config_and_a_line_it_cannot_read_is_named(self):
        doc = self.doc()
        self.assertEqual(doc["families"]["state"], "read")
        self.assertEqual([f["key"] for f in doc["families"]["list"]], ["backups", "repos", "host", "agents"])
        missing = self.doc(config=self.dir / "report-families.conf")["families"]
        self.assertEqual(missing, {"state": "missing", "source": "<config>/project-dashboard/report-families.conf", "problems": [], "list": []})
        found, problems = reports_screen.parse_families("\n".join([
            "family|a|A|bot|one,two",
            "family|b|B|bot",
            "family|Bad Key|C|bot|three",
            "family|c|C|bot|two",
            "family|a|Again|bot|four",
            "family|d|D|bot|Not A Job",
            "family|e|E|heart-pulse|five",
            "family|f|F|bot|six,seven,six",
        ]))
        self.assertEqual([f["key"] for f in found], ["a", "e"])
        self.assertEqual([p.split(":", 1)[0] for p in problems], ["line 2", "line 3", "line 4", "line 5", "line 6", "line 8"])
        self.assertIn("two is already in the family a", problems[2])
        # A job twice within one line is refused too: the cadence table would list it twice and the lamp count it twice.
        self.assertIn("six is listed twice in the family f", problems[5])

class ThePage(BrowserSession):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.backend = Jobs(self.dir, jobs=[("quiet-one", "idle", 0, None)])
        super().setUp()
        self.enterContext(mock.patch.object(reports_screen, "_html_folders", lambda: 0))

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/reports")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Reports · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "commands.js", "report-details.js", "reports.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_rail_opens_the_new_page_and_the_old_screen_is_retired(self):
        self.assertEqual(v2.SECTIONS.get("Reports"), "/reports")
        self.assertNotIn("Reports", v2.CLASSIC)
        # sd:3402 retired the classic Reports area; Resources' views moved off Operations in pack sd:7527.
        self.assertNotIn("Reports (classic)", v2.SCREENS)

    def test_the_data_routes_need_a_session_and_the_reading_takes_no_query(self):
        self.assertEqual(self.request("/api/reports")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/reports?act=1", headers=cookie)[0], 400)
        status, _, body = self.request("/api/reports", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual((doc["reports"], [j["name"] for j in doc["jobs"]], doc["sources"]["cadence"]), ([], ["quiet-one"], ""))

    def test_reopen_takes_the_revision_the_acknowledgement_answered(self):
        """sd:2395: the Undo's route. It reopens an acknowledged report under the revision the acknowledge answered."""
        item = self.report()
        status, _, body = self.post(f"/api/reports/{item}/acknowledge", {"revision": workflow.item_state(self.connection, item)["revision"]})
        self.assertEqual(status, 200)
        answered = body["revision"]
        before = self.snapshot()
        for payload in ({}, {"revision": answered, "who": "someone"}):
            self.assertEqual(self.post(f"/api/reports/{item}/reopen", payload)[0], 400, payload)
        self.assertEqual(self.post(f"/api/reports/{item}/reopen", {"revision": "old"})[0], 409)
        self.assertEqual(self.post(f"/api/reports/{item}/reopen", {"revision": answered}, **{"X-SD-CSRF": "0" * 64})[0], 403)
        self.assertEqual(self.request(f"/api/reports/{item}/reopen")[0], 404)
        self.assertEqual(self.snapshot(), before)
        status, _, body = self.post(f"/api/reports/{item}/reopen", {"revision": answered})
        self.assertEqual((status, body["item"]["status"]), (200, "planning"))
        self.assertIn("done -> planning by dashboard: reopened",
                      [note["body"] for note in workflow.item_state(self.connection, item)["notes"]])

    def test_undo_reopens_the_followups_the_acknowledgement_resolved(self):
        """sd:4755: the pack's `reopen` restores the status only, so Undo also opens again the followups the
        acknowledgement's status note names, and no other: not one a person closed first, not another report's."""
        from sd_db.writes import add_note, resolve_note

        def failure(run):
            state = reporting.ingest(self.connection, job=f"fixture-{run}", run_id=run, started="2026-09-08T00:00:00Z", ended="2026-09-08T00:01:00Z",
                                     exit_code=1, text="Failed", source_path=f"/fixture/{run}", attention=True, host=run)
            return state["item"]["id"]

        def followups(item):
            return {note["id"]: note["resolved_at"] is None for note in workflow.item_state(self.connection, item)["notes"] if note["kind"] == "followup"}

        item, other = failure("one"), failure("two")
        (ingest,) = followups(item)
        closed = add_note(self.connection, item, "followup", "done already", session="operator")
        resolve_note(self.connection, closed)
        listed = [row["id"] for row in reporting.reports(self.connection)]
        status, _, body = self.post(f"/api/reports/{item}/acknowledge", {"revision": workflow.item_state(self.connection, item)["revision"]})
        self.assertEqual(status, 200)
        self.assertEqual(followups(item), {ingest: False, closed: False})
        status, _, body = self.post(f"/api/reports/{item}/reopen", {"revision": body["revision"]})
        self.assertEqual((status, body["item"]["status"]), (200, "planning"))
        self.assertEqual(followups(item), {ingest: True, closed: False})
        # `updated_at` keeps the Undo's stamp, not the old one; the list orders by `created_at`, so the report keeps its place.
        self.assertEqual([row["id"] for row in reporting.reports(self.connection)], listed)
        # Another subject: a status note that names another report's followup reopens nothing there.
        status, _, body = self.post(f"/api/reports/{other}/acknowledge", {"revision": workflow.item_state(self.connection, other)["revision"]})
        (theirs,) = followups(other)
        reporting.transition(self.connection, item, "done", who="fixture", reason=f"resolved followup {theirs}")
        self.assertEqual(self.post(f"/api/reports/{item}/reopen", {"revision": workflow.item_state(self.connection, item)["revision"]})[0], 200)
        self.assertEqual(followups(other), {theirs: False})

    def test_an_undo_that_fails_between_its_steps_moves_nothing(self):
        """Steps: the status moves first and the followups after, in one transaction, so a failed reopen of a
        followup leaves the report done with its followup resolved, and the same Undo then finishes."""
        from sd_db import writes

        state = reporting.ingest(self.connection, job="fixture", run_id="one", started="2026-09-08T00:00:00Z", ended="2026-09-08T00:01:00Z",
                                 exit_code=1, text="Failed", source_path="/fixture/one", attention=True)
        item = state["item"]["id"]
        status, _, body = self.post(f"/api/reports/{item}/acknowledge", {"revision": state["revision"]})
        self.assertEqual(status, 200)
        before = self.snapshot()
        with mock.patch.object(writes, "reopen_note", side_effect=OSError("disk full")):
            self.assertGreaterEqual(self.post(f"/api/reports/{item}/reopen", {"revision": body["revision"]})[0], 500)
        self.assertEqual(self.snapshot(), before)
        status, _, reopened = self.post(f"/api/reports/{item}/reopen", {"revision": body["revision"]})
        self.assertEqual((status, reopened["item"]["status"]), (200, "planning"))
        self.assertEqual([note["resolved_at"] for note in reopened["notes"] if note["kind"] == "followup"], [None])

    def test_undo_reopens_a_followup_the_older_wording_names(self):
        """Older state: a report the dashboard acknowledged before sd:4722 names `resolved the ingest's followup <id>`."""
        state = reporting.ingest(self.connection, job="fixture", run_id="one", started="2026-09-08T00:00:00Z", ended="2026-09-08T00:01:00Z",
                                 exit_code=1, text="Failed", source_path="/fixture/one", attention=True)
        item = state["item"]["id"]
        (ingest,) = [note["id"] for note in state["notes"] if note["kind"] == "followup"]
        reporting.resolve_note(self.connection, ingest)
        reporting.transition(self.connection, item, "done", who="dashboard", reason=f"resolved the ingest's followup {ingest}")
        status, _, body = self.post(f"/api/reports/{item}/reopen", {"revision": workflow.item_state(self.connection, item)["revision"]})
        self.assertEqual((status, body["item"]["status"]), (200, "planning"))
        self.assertEqual([note["resolved_at"] for note in body["notes"] if note["id"] == ingest], [None])

    def report(self):
        from sd_db import create_item

        item = create_item(self.connection, kind="report", title="nightly: run report", status="planning", source="cron-report",
                           external_id="nightly:one", fields={"attention": False}, body={"text": "all quiet"}, session="cron")
        self.connection.commit()
        return item


# The stand-in additions reports.js needs beyond test_v2_tasks: the shell's url, context and views.
SHELL_MORE = r"""
OUT.urls = []; window.shell.url = q => OUT.urls.push(q); window.shell.setContext = () => {}; OUT.picks = [];
window.shell.commands.pick = id => OUT.picks.push(id); window.shell.commands.list = () => REG;
window.shell.row = () => new URLSearchParams(location.search).get('row');
OUT.views = []; OUT.counts = {};
window.shell.view = { show: id => { OUT.views.push(id); return true; }, count: (id, n) => { OUT.counts[id] = n; }, current: () => 'reports' };
"""


class TheScript(ScreenCase):
    """reports.js against the document `reports_screen` builds from the seeded store and logs."""

    def setUp(self):
        super().setUp()
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ids = seed(self)
        self.families = self.dir / "families.conf"
        self.families.write_text(FAMILIES, encoding="utf-8")
        with mock.patch.object(reports_screen, "_html_folders", lambda: 2):
            self.doc = reports_screen.document(self.connection, now=LATER, jobs=self.jobs, tz=TZ, config=self.families)

    def served(self, tail=None):
        with mock.patch.object(reports_screen, "_html_folders", lambda: 2), \
             mock.patch.object(reports_screen, "TAIL_BYTES", tail or reports_screen.TAIL_BYTES):
            return reports_screen.document(self.connection, now=LATER, jobs=self.jobs, tz=TZ, config=self.families)

    def run_page(self, body, answer=None, doc=None, prelude=""):
        doc = doc or self.doc
        answer = answer or f"(path, body) => path === '/api/reports' ? [200, {json.dumps(doc)}] : [200, {{}}]"
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n"
                  + SHELL + SHELL_MORE + "\n" + READ_SHELL + f"\nANSWER = {answer};\n" + prelude + "\n" + COMMANDS_JS + REPORT_DETAILS_JS + REPORTS_JS
                  + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.attention = window.PAGE_ATTENTION;"
                  + " OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_the_design_commands_are_registered_with_their_ids_labels_keys_and_risks(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.label, c.key, c.risk]); R.undo = REG.filter(c => c.undo).map(c => c.id);")
        self.assertEqual(out["R"]["reg"], COMMANDS)
        # Acknowledge is the one command with an Undo: reporting.reopen (sd:2395).
        self.assertEqual(out["R"]["undo"], ["report.ack"])

    def test_open_item_goes_to_the_reports_row_on_tasks(self):
        """sd:3526 (review 2026-10-12, A2 F12): Reports opened the classic /item/<n> page; Management opens Tasks."""
        out = self.run_page("R.toast = cmd('report.show').run({ n: 42 }); R.href = location.href;")
        self.assertEqual((out["R"]["href"], out["R"]["toast"]), ("/tasks?row=42", "#42 opens in Tasks"))

    def test_show_log_prints_the_line_management_prints(self):
        """sd:3525 (review 2026-10-12, A2 F13): one cron-jobs line on every page, from commands.js; no checkout path of its own."""
        out = self.run_page("R.lines = REG.filter(c => c.label === 'Show log').map(c => [c.id, c.cli({ job: 'nightly-sync' })]);"
                            "R.shared = window.commands.showLog({ job: o => ({ name: o.job }) }).cli({ job: 'nightly-sync' });")
        self.assertTrue(out["R"]["lines"])
        for cid, line in out["R"]["lines"]:
            with self.subTest(command=cid):
                self.assertEqual(line, out["R"]["shared"])
        self.assertEqual(out["R"]["shared"], "local-cron-jobs/cron-jobs.sh logs nightly-sync")

    def test_show_log_runs_the_jobs_log_on_the_server(self):
        """sd:5886: Show log on a report posts the job's name to /api/actions/run, as every page's Show log does."""
        out = self.run_page("""const was = ANSWER; ANSWER = (p, b) => p === '/api/actions/run' ? [200, { action: b.action, label: 'Show log', line: 'l', exit: 0, output: 'o', cut: false }] : was(p, b);
const c = cmd('report.log'), o = { id: 'report:7', job: 'nightly-sync', source: { src: '/x/nightly-sync.log' } };
R.exec = c.executes; shellRun(c, o); await flush();""")
        self.assertTrue(out["R"]["exec"])
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/actions/run", {"action": "job.log", "job": "nightly-sync"}]])

    ANSWERED = "(path, body) => path.endsWith('/acknowledge') ? [200, { revision: 'after-ack' }] : path.endsWith('/reopen') ? [200, { revision: 'after-reopen' }] : path === '/api/reports' ? [200, DOC_JSON] : [200, {}]"

    def test_acknowledge_posts_the_route_with_the_revision_offers_undo_and_reads_again(self):
        n = self.ids["clean"]
        revision = workflow.item_state(self.connection, n)["revision"]
        out = self.run_page(f"shellRun(cmd('report.ack'), C.get('r{n}')); await flush();",
                            answer=self.ANSWERED.replace("DOC_JSON", json.dumps(self.doc)))
        self.assertEqual(out["posts"], [[f"/api/reports/{n}/acknowledge", {"revision": revision}, 64]])
        self.assertEqual(out["toasts"][-1], [f"Acknowledged · #{n}", True])
        self.assertEqual(out["gets"], ["/api/reports", "/api/reports"])
        # sd:5064: it asks for an optional reason; left empty, no note is written.
        self.assertEqual(out["confirms"], ["report.ack"])

    def test_a_reason_is_a_comment_written_just_before_the_acknowledge_under_the_revision_it_answered(self):
        """sd:5064: the reason lands as a comment note first; the acknowledge carries the revision that note answered."""
        n = self.ids["clean"]
        revision = workflow.item_state(self.connection, n)["revision"]
        answer = self.ANSWERED.replace("DOC_JSON", json.dumps(self.doc)).replace(
            "(path, body) => ", "(path, body) => path.endsWith('/notes') ? [200, { note: 9, revision: 'after-note' }] : ", 1)
        out = self.run_page(f"FIELD_VALUES = {{ reason: \"it's fine\" }}; R.cli = cmd('report.ack').cli(C.get('r{n}'), FIELD_VALUES);"
                            f" shellRun(cmd('report.ack'), C.get('r{n}')); await flush();", answer=answer)
        self.assertEqual(out["posts"], [[f"/api/items/{n}/notes", {"body": "it's fine", "kind": "comment", "revision": revision}, 64],
                                        [f"/api/reports/{n}/acknowledge", {"revision": "after-note"}, 64]])
        self.assertEqual(out["R"]["cli"], f"sd task note {n} 'it'\\''s fine' && sd reports acknowledge {n}")
        self.assertEqual(out["toasts"][-1], [f"Acknowledged · #{n}", True])

    def test_a_refused_reason_note_stops_the_acknowledge(self):
        n = self.ids["clean"]
        answer = self.ANSWERED.replace("DOC_JSON", json.dumps(self.doc)).replace(
            "(path, body) => ", "(path, body) => path.endsWith('/notes') ? [409, { error: 'the item changed' }] : ", 1)
        out = self.run_page(f"FIELD_VALUES = {{ reason: 'why' }}; shellRun(cmd('report.ack'), C.get('r{n}')); await flush();", answer=answer)
        self.assertEqual([p[0] for p in out["posts"]], [f"/api/items/{n}/notes"])
        self.assertIn("the item changed", out["toasts"][-1][0])

    def test_resolve_followup_asks_and_resolves_the_followup_that_holds_the_report(self):
        """sd:5064: the followup that holds a report is resolved from the page, through the notes' /resolve route."""
        n = self.ids["failed"]
        note = next(r for r in self.doc["reports"] if r["id"] == n)["followups"][0]
        revision = workflow.item_state(self.connection, n)["revision"]
        answer = f"(path, body) => path.endsWith('/resolve') ? [200, {{ revision: 'after-resolve' }}] : path === '/api/reports' ? [200, {json.dumps(self.doc)}] : [200, {{}}]"
        out = self.run_page(f"""R.off = [cmd('report.resolve').when(C.get('r{self.ids['clean']}')), cmd('report.resolve').when(C.get('r{self.ids['done']}'))];
R.cli = cmd('report.resolve').cli(C.get('r{n}')); shellRun(cmd('report.resolve'), C.get('r{n}')); await flush();""", answer=answer)
        self.assertEqual(out["R"]["off"], ["no open followup holds it", "already acknowledged"])
        self.assertEqual(out["confirms"], ["report.resolve"])
        self.assertEqual(out["posts"], [[f"/api/notes/{note}/resolve", {"revision": revision}, 64]])
        self.assertEqual(out["R"]["cli"], f"sd note resolve {note}")
        self.assertEqual(out["toasts"][-1], [f"Followup resolved · note {note}", False])

    def test_fix_asks_first_posts_the_report_revision_and_names_the_debug_session(self):
        """sd:3456. Fix on a report: a debug session, asked first; it is off once the report is acknowledged."""
        n, done = self.ids["failed"], self.ids["done"]
        revision = workflow.item_state(self.connection, n)["revision"]
        answer = ("(path, body) => path.endsWith('/fix') ? [200, { revision: 'after-fix', fix: { session: 'debug', pane: 'w9:p1' } }]"
                  f" : path === '/api/reports' ? [200, {json.dumps(self.doc)}] : [200, {{}}]")
        out = self.run_page(f"""R.what = cmd('report.fix').consequence(C.get('r{n}')); R.off = cmd('report.fix').when(C.get('r{done}'));
shellRun(cmd('report.fix'), C.get('r{n}')); await flush();""", answer=answer)
        self.assertEqual(out["confirms"], ["report.fix"])
        self.assertIn(f"Opens a Claude session on report #{n} in herdr.", out["R"]["what"])
        self.assertEqual(out["R"]["off"], "already acknowledged")
        self.assertEqual(out["posts"], [[f"/api/items/{n}/fix", {"revision": revision}, 64]])
        self.assertEqual(out["toasts"][-1], [f"Fix started · #{n} · debug session in herdr pane w9:p1", False])
        self.assertEqual(out["gets"], ["/api/reports", "/api/reports"])

    def test_a_failed_fix_launch_reads_the_reports_again_so_the_next_fix_has_the_new_revision(self):
        """pack sd:7783: a failed launch (5xx) notes its cause on the report, and a post with no answer may have noted
        it, so either moves the revision the page holds; Reports read again. A refusal that wrote nothing (400) does not."""
        n = self.ids["failed"]
        doc = json.dumps(self.doc)
        failed = ("(path, body) => path.endsWith('/fix') ? [503, { error: 'Fix failed: herdr.sh launch: session sd is not"
                  f" running. A note on sd:{n} keeps the cause.' }}] : [200, {doc}]")
        silent = f"(path, body) => {{ if (path.endsWith('/fix')) throw new TypeError('Load failed'); return [200, {doc}]; }}"
        refused = f"(path, body) => path.endsWith('/fix') ? [400, {{ error: 'item {n} is done' }}] : [200, {doc}]"
        for answer, reads in ((failed, 2), (silent, 2), (refused, 1)):
            out = self.run_page(f"shellRun(cmd('report.fix'), C.get('r{n}')); await flush(); await flush();", answer=answer)
            self.assertEqual(out["gets"], ["/api/reports"] * reads, answer)

    def test_fix_on_an_expired_login_opens_its_credentials_row_and_leads_the_bar(self):
        """sd:6964: the cold-outreach-triage run of sd:6951, its account made an example; no session can sign in, so
        Fix opens Health > Credentials on that Google login's row, asks nothing and posts nothing. The plain failed
        report keeps Acknowledge first and its debug-session Fix."""
        n = reporting.ingest(self.connection, job="cold-outreach-triage", run_id="r9", started="2026-09-08T14:04:05Z",
                             ended="2026-09-08T14:04:19Z", exit_code=1, attention=True, attention_basis="job exited 1",
                             source_path="/home/example/repos/system/local-cron-jobs/logs/cold-outreach-triage.log",
                             text="Triage did not run. The Gmail search returned \"ACTION REQUIRED: Google Authentication "
                                  "Needed\" for the work account.\nRESULT: unavailable, workspace-mcp Gmail auth expired "
                                  "for ops@example.test\nFAILED rc=1\n")["item"]["id"]
        with mock.patch.object(reports_screen, "_html_folders", lambda: 2):
            doc = reports_screen.document(self.connection, now=LATER, jobs=self.jobs, tz=TZ, config=self.families)
        plain = self.ids["failed"]
        out = self.run_page(f"""const fix = cmd('report.fix'), ack = cmd('report.ack'), o = C.get('r{n}'), p = C.get('r{plain}');
R.primary = [fix.primary(o), ack.primary(o), fix.primary(p), ack.primary(p)]; R.ask = [!!fix.askFirst(o), !!fix.askFirst(p)];
R.does = fix.does(o); shellRun(fix, o); await flush(); R.href = location.href;""", doc=doc)
        self.assertEqual(out["R"]["href"], "/health?area=cred&row=cred%3Agoogle%3Aops%40example.test")
        self.assertEqual(out["toasts"][-1], ["Health opens on google:ops@example.test", False])
        self.assertEqual((out["posts"], out["confirms"]), ([], []))
        self.assertEqual(out["R"]["primary"], [True, False, False, True])
        self.assertEqual(out["R"]["ask"], [False, True])
        self.assertEqual(out["R"]["does"], "Opens the expired login's row on Health, where Sign in is.")

    def test_undo_reopens_the_report_under_the_revision_the_acknowledgement_answered(self):
        n = self.ids["clean"]
        out = self.run_page(f"shellRun(cmd('report.ack'), C.get('r{n}')); await flush(); lastUndo().undo(); await flush();",
                            answer=self.ANSWERED.replace("DOC_JSON", json.dumps(self.doc)))
        self.assertEqual(out["posts"][1], [f"/api/reports/{n}/reopen", {"revision": "after-ack"}, 64])
        self.assertEqual(out["toasts"][-1][0], f"Acknowledge undone · {self.doc_title(n)}")
        self.assertEqual(out["gets"], ["/api/reports"] * 3)

    def test_an_undo_the_store_refuses_says_so(self):
        n = self.ids["clean"]
        answer = self.ANSWERED.replace("DOC_JSON", json.dumps(self.doc)).replace("[200, { revision: 'after-reopen' }]", "[409, { error: 'the report changed' }]")
        out = self.run_page(f"shellRun(cmd('report.ack'), C.get('r{n}')); await flush(); lastUndo().undo(); await flush();", answer=answer)
        self.assertEqual(out["toasts"][-1][0], f"Acknowledge not undone · {self.doc_title(n)}: the report changed")

    def doc_title(self, n):
        return next(r for r in self.doc["reports"] if r["id"] == n)["title"]

    def test_the_page_reads_through_the_shared_reader(self):
        # read.js holds the generation, retirement and failure rules (sd:2487); the page keeps no reading of its own.
        self.assertIn("shell.read(", REPORTS_JS)
        self.assertNotIn("getJSON('/api/reports')", REPORTS_JS)
        out = self.run_page("")
        self.assertEqual(out["states"][0]["text"], "Reading the reports, jobs and job logs. Rows appear when the read finishes.")

    def test_a_failed_read_after_a_good_one_clears_the_rows_and_runs_no_command(self):
        n = self.ids["clean"]
        out = self.run_page(f"""ANSWER = () => [500, {{ error: 'boom' }}]; await load(); await flush();
R.type = C.get('r{n}').type; R.rows = ELS.rows.html; R.cad = ELS['cad-body'].html;""")
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertNotIn(f'data-id="r{n}"', out["R"]["rows"])
        self.assertIn("were not read", out["R"]["rows"])
        self.assertIn("boom", out["states"][-1]["text"])
        self.assertEqual(out["attention"]["state"], "unknown")

    def test_the_job_filter_lists_every_job_the_reading_names_not_only_jobs_with_reports(self):
        # sd:2430: quiet-one is installed and logged no report; its cadence row sets the filter, so the filter must offer it.
        out = self.run_page("R.jobs = ELS.filters.html;")
        for job in ("nightly-sync", "weekly-scan", "quiet-one"):
            self.assertIn(f'<option value="{job}">{job}</option>', out["R"]["jobs"])

    def test_acknowledge_is_off_for_a_report_a_followup_holds_and_for_an_acknowledged_one(self):
        note = next(r for r in self.doc["reports"] if r["id"] == self.ids["failed"])["followups"]
        out = self.run_page(f"""R.when = [cmd('report.ack').when(C.get('r{self.ids['failed']}')), cmd('report.ack').when(C.get('r{self.ids['done']}')),
  cmd('report.ack').when(C.get('r{self.ids['clean']}'))];""")
        self.assertEqual(out["R"]["when"], [f"an open followup holds it: sd note resolve {note[0]}", "already acknowledged", True])

    def test_the_cadence_and_the_lamps_read_the_logs(self):
        doc = json.loads(json.dumps(self.doc))
        doc["families"]["list"] = [{"key": "sync", "label": "Sync", "icon": "bot", "jobs": ["nightly-sync"]},
                                   {"key": "scan", "label": "Scan", "icon": "bot", "jobs": ["weekly-scan"]}]
        out = self.run_page("R.cad = ELS['cad-body'].html; R.lamps = ELS.annunciator.html; R.tally = ELS['cad-tally'].html;"
                            " R.marks = DAYS.map(d => mark('nightly-sync', d)[2]); R.weekly = DAYS.map(d => mark('weekly-scan', d)[0]);",
                            doc=doc)
        self.assertEqual(out["R"]["marks"], ["no log yet", "no log yet", "ran", "ran", "ran", "ran", "2 failed"])
        self.assertEqual(out["R"]["weekly"], ["none", "none", "ok", "none", "none", "none", "none"])
        self.assertRegex(out["R"]["lamps"], r'data-fam="sync"[^>]*data-state="warning"')
        self.assertRegex(out["R"]["lamps"], r'data-fam="scan"[^>]*data-state="ok"')
        self.assertEqual(cell_grammar(self, out["R"]["lamps"]), [("button", "warning"), ("button", "ok"), ("div", "unknown")])
        self.assertIn("■ 1 failed last run", out["R"]["tally"])
        self.assertEqual(out["attention"], {"state": "warning", "n": 1, "what": "reports want you"})

    def test_a_manual_job_marks_its_runs_and_no_gap(self):
        """sd:4965: a manual job's day without a run says manual, not "no run logged"; a day it ran says so."""
        self.jobs.schedules["quiet-one"] = []
        self.jobs.write("quiet-one", run_lines("quiet-one", DAYS[3]))
        out = self.run_page("R.marks = DAYS.map(d => mark('quiet-one', d).join(' '));", doc=self.served())
        self.assertEqual(out["R"]["marks"][2:5], ["none · manual: runs when started", "ok ● ran", "none · manual: runs when started"])

    def test_a_family_lamp_without_evidence_for_a_job_is_unknown_not_all_clear(self):
        def lamps(doc, jobs=("weekly-scan",)):
            doc["families"]["list"] = [{"key": "scan", "label": "Scan", "icon": "bot", "jobs": list(jobs)}]
            return self.run_page("R.lamps = ELS.annunciator.html;", doc=doc)["R"]["lamps"]

        unread = json.loads(json.dumps(self.doc))
        unread["cadence"], unread["sources"]["cadence"] = None, "no log root"
        # The other cases are documents the server builds from the logs and calendars, so the page reads what it would.
        cut = self.served(tail=60)
        self.assertEqual(cut["cadence"]["weekly-scan"]["read_from"], "2026-09-06")
        (self.jobs.cron_root / "logs" / "weekly-scan.log").unlink()
        no_log = self.served()
        self.jobs.schedules["weekly-scan"] = [{"Day": 1, "Hour": 3}]
        idle = self.served()
        self.assertEqual(idle["cadence"]["weekly-scan"]["scheduled"], [False] * len(DAYS))
        cases = {"cadence not read": lamps(unread), "job not installed": lamps(json.loads(json.dumps(self.doc)), ("weekly-scan", "gone-job")),
                 "no log": lamps(no_log), "no log, nothing scheduled this week": lamps(idle), "its last run was not read": lamps(cut)}
        for case, html in cases.items():
            with self.subTest(case):
                self.assertRegex(html, r'data-fam="scan"[^>]*data-state="unknown"')
                self.assertNotIn("all clear", html)
                self.assertIn("not read", html)
        self.assertIn("gone-job (not read)", cases["job not installed"])
        # A job scheduled less often than weekly: the lamp follows its own last scheduled run, read or not.
        def monthly(last):
            doc = json.loads(json.dumps(self.doc))
            doc["cadence"]["monthly"] = {"log": True, "from": "2026-01-01", "read_from": None, "runs": {d: [0, 0] for d in DAYS},
                                         "scheduled": [False] * len(DAYS), "last": last}
            return lamps(doc, ("weekly-scan", "monthly"))
        self.assertRegex(monthly({"day": "2026-09-01", "read": False, "runs": [0, 0]}), r'data-fam="scan"[^>]*data-state="unknown"')
        self.assertRegex(monthly({"day": "2026-01-01", "read": False, "runs": [0, 0]}), r'data-fam="scan"[^>]*data-state="unknown"')
        self.assertRegex(monthly({"day": None, "read": False, "runs": [0, 0]}), r'data-fam="scan"[^>]*data-state="unknown"')
        self.assertRegex(monthly({"day": "2026-09-01", "read": True, "runs": [0, 1]}), r'data-fam="scan"[^>]*data-state="warning"')
        self.assertRegex(monthly({"day": "2026-09-01", "read": True, "runs": [1, 1]}), r'data-fam="scan"[^>]*data-state="caution"')
        self.assertRegex(monthly({"day": "2026-09-01", "read": True, "runs": [0, 0]}), r'data-fam="scan"[^>]*data-state="caution"')
        self.assertRegex(monthly({"day": "2026-09-01", "read": True, "runs": [1, 0]}), r'data-fam="scan"[^>]*data-state="ok"')
        # A manual job (sd:4965) is never due: no run is no gap, so it leaves the lamp as the other jobs set it.
        self.jobs.schedules["quiet-one"] = []
        manual = self.served()
        self.assertRegex(lamps(manual, ("quiet-one",)), r'data-fam="scan"[^>]*data-state="ok"')
        # With every job's log read and no failure, the lamp is still all clear.
        self.assertRegex(lamps(json.loads(json.dumps(self.doc))), r'data-fam="scan"[^>]*data-state="ok"')

    def test_the_day_a_cut_read_begins_on_is_unknown_not_clean_failed_or_missing(self):
        # A cut read can start partway through its first day, so that day was not read in full.
        doc = json.loads(json.dumps(self.doc))
        sync = doc["cadence"]["nightly-sync"]
        sync["read_from"], sync["cut"] = DAYS[4], True
        sync["runs"][DAYS[3]], sync["runs"][DAYS[4]], sync["runs"][DAYS[5]] = [0, 0], [1, 0], [0, 0]
        cases = {"clean": [1, 0], "failed": [0, 1], "no run logged": [0, 0]}
        for case, runs in cases.items():
            with self.subTest(case):
                sync["runs"][DAYS[4]] = runs
                out = self.run_page("R.marks = [3, 4, 5].map(i => mark('nightly-sync', DAYS[i]));", doc=doc)
                self.assertEqual(out["R"]["marks"][0], ["unknown", "▨", "not read"])
                self.assertEqual(out["R"]["marks"][1], ["unknown", "▨", "not read"])
                self.assertEqual(out["R"]["marks"][2], ["caution", "▲", "no run logged"])

    def test_a_cut_read_with_no_whole_line_marks_every_day_not_read(self):
        doc = json.loads(json.dumps(self.doc))
        doc["cadence"]["nightly-sync"].update(cut=True, read_from=None)
        out = self.run_page("R.marks = DAYS.map(d => mark('nightly-sync', d)[0]);", doc=doc)
        self.assertEqual(out["R"]["marks"], ["unknown"] * 7)

    def test_the_first_load_selects_a_report_the_filtered_page_shows(self):
        # The stand-in tbody answers from the rows the page drew, so a selection outside them is visible.
        rows = """var TB = document.getElementById('rows');
TB.querySelectorAll = () => [...(TB.html || '').matchAll(/<tr data-id="([^"]+)"/g)].map(m => ({ dataset: { id: m[1] }, setAttribute() {}, toggleAttribute() {} }));
TB.querySelector = () => TB.querySelectorAll()[0] || null;"""
        failed, clean = self.ids["failed"], self.ids["clean"]
        for search, want in (("?job=weekly-scan", f"r{clean}"), (f"?job=weekly-scan&row=r{failed}", f"r{clean}"),
                             (f"?row=r{clean}", f"r{clean}"), ("", f"r{failed}")):
            with self.subTest(search):
                out = self.run_page("R.sel = sel; R.drawn = TB.querySelectorAll().map(t => t.dataset.id);",
                                    prelude=f"location.search = {json.dumps(search)};\n" + rows)
                self.assertIn(out["R"]["sel"], out["R"]["drawn"])
                self.assertEqual(out["R"]["sel"], want)

    def test_only_the_newest_of_overlapping_readings_applies(self):
        failed = self.ids["failed"]
        older = json.loads(json.dumps(self.doc))
        older["reports"] = [r for r in older["reports"] if r["id"] == failed]
        newer = json.dumps(self.doc)
        overlap = f"""var CALLS = []; ANSWER = path => path === '/api/reports' ? new Promise(r => CALLS.push(r)) : [200, {{}}];
  const a = load(), b = load(); await flush();
  CALLS[1]([200, {newer}]); await flush(); CALLS[0](OLDER); await flush(); await a; await b;
  R.ids = ROWS.map(r => r.id); R.state = OUT.states[OUT.states.length - 1];"""
        for case, answer in (("older success", json.dumps([200, older])), ("older failure", json.dumps([500, {"error": "boom"}]))):
            with self.subTest(case):
                out = self.run_page(overlap.replace("OLDER", answer))
                self.assertEqual(len(out["R"]["ids"]), len(self.doc["reports"]))
                self.assertNotEqual((out["R"]["state"] or {}).get("kind"), "error")

    def test_a_job_that_has_not_run_yet_today_is_not_a_gap(self):
        doc = json.loads(json.dumps(self.doc))
        doc["cadence"]["nightly-sync"]["runs"][DAYS[6]] = [0, 0]
        out = self.run_page("R.today = mark('nightly-sync', DAYS[6]); R.last = lastState('nightly-sync');", doc=doc)
        self.assertEqual(out["R"]["today"], ["none", "·", "no run yet today"])
        self.assertEqual(out["R"]["last"], "ok")

    def test_a_rerun_on_an_unscheduled_day_supersedes_the_failed_scheduled_run(self):
        # sd:5538: a weekly job failed on its day and a hand rerun the next day passed; the lamp said failing.
        doc = json.loads(json.dumps(self.doc))
        c = doc["cadence"]["nightly-sync"]
        c["scheduled"] = [False, False, False, False, False, True, False]
        c["runs"] = {d: [0, 0] for d in DAYS}
        c["runs"][DAYS[5]], c["runs"][DAYS[6]] = [0, 1], [2, 0]
        out = self.run_page("R.today = mark('nightly-sync', DAYS[6]); R.idle = mark('nightly-sync', DAYS[4]); R.last = lastState('nightly-sync');", doc=doc)
        self.assertEqual(out["R"]["last"], "ok")
        self.assertEqual(out["R"]["today"][0], "ok")
        self.assertEqual(out["R"]["idle"], ["none", "·", "not scheduled"])

    def test_the_state_names_a_missing_family_list_and_a_failed_source(self):
        doc = json.loads(json.dumps(self.doc))
        doc["families"] = {"state": "missing", "source": "<config>/project-dashboard/report-families.conf", "problems": [], "list": []}
        doc["cadence"], doc["sources"]["cadence"] = None, "no log root"
        out = self.run_page("R.lamps = ELS.annunciator.html; R.cad = ELS['cad-body'].html;", doc=doc)
        last = out["states"][-1]
        self.assertEqual(last["kind"], "partial")
        self.assertIn("cadence (no log root)", last["text"])
        self.assertIn("No family list: set dashboard.report_families on Settings", last["text"])
        self.assertIn("no family list", out["R"]["lamps"])
        self.assertIn("The run cadence was not read: no log root.", out["R"]["cad"])
        out = self.run_page("", answer="() => [500, { error: 'boom' }]")
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("boom", out["states"][-1]["text"])

    def test_the_status_mail_notice_the_runner_lamp_and_the_batch_sections_are_gone(self):
        """sd:2212 has no mail reader and sd:1897 cancelled the message store; the quiet-run rule files no clean report."""
        self.assertNotIn("mail", self.doc)
        self.assertNotIn("Database reports only", REPORTS_JS)
        self.assertNotIn("Runner & review", REPORTS_JS)
        out = self.run_page("R.lamps = ELS.annunciator.html;")
        self.assertNotIn("Runner", out["R"]["lamps"])
        self.assertNotIn("status mail", json.dumps(out["states"]))
        page = (V2 / "reports.html").read_text(encoding="utf-8")
        for heading in ("Acknowledge clean reports", "Also on the classic Resources screen"):
            self.assertNotIn(heading, page)

    def test_the_reports_tab_counts_open_reports_only(self):
        """sd:8018: an acknowledged report stays on the list but leaves the tab's count."""
        out = self.run_page("R.counts = OUT.counts; R.shown = shown.length; R.open = shown.filter(r => r.open).length;")
        self.assertGreater(out["R"]["shown"], out["R"]["open"], "the fixture needs an acknowledged report")
        self.assertEqual(out["R"]["counts"]["reports"], out["R"]["open"], "sd:8018: the tab counted an acknowledged report")

    def test_a_job_with_a_report_that_needs_action_stays_in_the_short_cadence_table(self):
        """A job whose log shows no gap is still listed while its newest report needs action (the rows key byJob by job)."""
        doc = json.loads(json.dumps(self.doc))
        sync = doc["cadence"]["nightly-sync"]
        sync["from"], sync["runs"] = doc["days"][0], {d: [1, 0] for d in doc["days"]}
        sync["last"] = {"day": doc["days"][-2], "read": True, "runs": [1, 0]}
        out = self.run_page("R.marks = DAYS.map(d => mark('nightly-sync', d)[0]); R.listed = !!interesting(['', 'nightly-sync']);"
                            " R.quiet = !!interesting(['', 'weekly-scan']);", doc=doc)
        self.assertNotIn("caution", out["R"]["marks"])
        self.assertNotIn("warning", out["R"]["marks"])
        self.assertEqual((out["R"]["listed"], out["R"]["quiet"]), (True, False))

    def test_the_page_declares_its_views_and_a_family_lamp_or_a_cadence_row_shows_the_reports(self):
        """The shell's view switch: Reports (the list) first, Cadence (the run table) second. A lamp and a cadence row
        filter the list, so each shows the Reports view; the segments count the rows shown and the jobs listed."""
        lamp = "ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? { id: '', dataset: { fam: 'sync' } } : null } });"
        row = "ELS['cad-body'].listeners.click[0]({ target: { closest: s => s === 'tr[data-job]' ? { dataset: { job: 'weekly-scan' } } : null } });"
        doc = json.loads(json.dumps(self.doc))
        doc["families"]["list"] = [{"key": "sync", "label": "Sync", "icon": "bot", "jobs": ["nightly-sync"]}]
        out = self.run_page(f"R.decl = window.PAGE_VIEWS || null; R.before = OUT.views.slice(); {lamp} R.lamp = [OUT.views.slice(), F.fam, shown.length];"
                            f" {row} R.row = [OUT.views.slice(), F.job, shown.length]; R.counts = OUT.counts; R.jobs = (cadAll ? CAD_JOBS : CAD_JOBS.filter(interesting)).length;", doc=doc)
        self.assertEqual(out["R"]["decl"], [{"id": "reports", "label": "Reports"}, {"id": "cadence", "label": "Cadence"}])
        self.assertEqual(out["R"]["before"], [])
        self.assertEqual(out["R"]["lamp"], [["reports"], "sync", 1])
        self.assertEqual(out["R"]["row"], [["reports", "reports"], "weekly-scan", 0])
        self.assertEqual({k: v for k, v in out["R"]["counts"].items() if k != "attention"}, {"reports": 0, "cadence": out["R"]["jobs"]})  # sd:5524: the jobs the Cadence view shows
        self.assertNotIn("attention", out["R"]["counts"])
        page = (V2 / "reports.html").read_text(encoding="utf-8")
        # The lamps sit above the switch, which goes before the first panel; the list is the first view.
        self.assertLess(page.index('id="annunciator"'), page.index('id="view-reports"'))
        self.assertLess(page.index('id="view-reports"'), page.index('id="view-cadence"'))

    def test_a_reading_retires_the_reports_and_jobs_it_no_longer_lists(self):
        clean, failed = self.ids["clean"], self.ids["failed"]
        later = json.loads(json.dumps(self.doc))
        later["reports"] = [r for r in later["reports"] if r["id"] != clean]
        later["jobs"] = [j for j in later["jobs"] if j["name"] != "weekly-scan"]
        del later["cadence"]["weekly-scan"]
        answer = (f"(path, body) => path === '/api/reports' ? [200, SECOND ? {json.dumps(later)} : {json.dumps(self.doc)}] : [200, {{}}]")
        out = self.run_page(f"""document.dispatchEvent(new CustomEvent('shell:picked', {{ detail: ['r{clean}', 'job:weekly-scan', 'r{failed}'] }}));
  SECOND = true; await reload(); await flush();
  R.types = ['r{clean}', 'job:weekly-scan', 'r{failed}', 'job:nightly-sync'].map(id => C.get(id).type);
  R.label = C.get('r{clean}').label;
  R.offered = ['r{clean}', 'job:weekly-scan'].map(id => REG.filter(c => c.on === C.get(id).type).map(c => c.id));""",
                            answer="(() => { globalThis.SECOND = false; return " + answer + "; })()")
        # The picks the reading no longer lists are dropped; the one it still lists stays picked.
        self.assertEqual(out["picks"], [f"r{clean}", "job:weekly-scan"])
        self.assertEqual(out["R"]["types"], ["not listed", "not listed", "report", "job"])
        self.assertIn("(no longer listed)", out["R"]["label"])
        self.assertEqual(out["R"]["offered"], [[], []])

    def test_details_show_the_report_its_source_from_home_and_the_diff_with_the_previous_one(self):
        out = self.run_page(f"open('r{self.ids['failed']}'); R.failed = ELS.details.html; open('r{self.ids['clean']}'); R.clean = ELS.details.html;")
        self.assertIn("~/repos/system/local-cron-jobs/logs/nightly-sync.log", out["R"]["failed"])
        self.assertNotIn("/home/example", out["R"]["failed"])
        self.assertIn("An open followup", out["R"]["failed"])
        self.assertIn("Last 1400 characters shown.", out["R"]["clean"])
        self.assertIn("Since the previous report", out["R"]["clean"])
        self.assertIn('class="del">old', out["R"]["clean"])

    def test_details_show_a_multi_line_basis_as_lines(self):
        # sd:4681: since sd:4664 a basis holds one finding per line; the details keep each newline, and the page's
        # CSS shows them as lines, not one run-on line.
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["reports"] if r["id"] == self.ids["failed"])["basis"] = "finding one\nfinding two"
        out = self.run_page(f"open('r{self.ids['failed']}'); R.det = ELS.details.html;", doc=doc)
        self.assertIn("<dt>Attention evidence</dt><dd>finding one\nfinding two</dd>", out["R"]["det"])
        # sd:5636: a report's Details are report-details.js's, so their CSS is report-details.css, scoped to its wrapper.
        css = (V2 / "static" / "report-details.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"(?m)^\.report-det dd \{[^}]*white-space: pre-line;")

    def test_details_of_a_report_whose_fields_cannot_be_read_name_the_action_before_the_line(self):
        # sd:3716, after pack sd:3503: the sentence names what to do and why the page withholds it; the line is the copy.
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["reports"] if r["id"] == self.ids["clean"])["fields_read"] = False
        out = self.run_page(f"open('r{self.ids['clean']}'); R.det = ELS.details.html;", doc=doc)
        self.assertIn("This page withholds Acknowledge: one click would close a report nobody has read. "
                      f"Read its log, then acknowledge report {self.ids['clean']} in a terminal: "
                      f"<code>sd reports acknowledge {self.ids['clean']}</code>.", out["R"]["det"])

    LAUNCHCTL = ("(path, body) => path.endsWith('/retry') ? [200, ANSWER]"
                 " : path === '/api/reports' ? [200, DOC_JSON] : [200, {}]")

    def test_a_retry_shows_launchctls_answer_when_it_did_not_accept(self):
        """sd:3386, as sd:3388 words Management's: a 200 carries launchctl's answer; only `accepted` started a retry."""
        cases = {
            "{ request: { status: 'failed', message: 'launchctl refused the request (exit 3)' } }":
                "nightly-sync not retried: launchctl refused the request (exit 3)",
            "{ request: { status: 'unknown', message: 'launchctl timed out; the request outcome is unknown' } }":
                "Not known whether nightly-sync was retried: launchctl timed out; the request outcome is unknown",
            "{}": "Not known whether nightly-sync was retried: the answer named no outcome",
        }
        for answer, said in cases.items():
            with self.subTest(said=said):
                out = self.run_page("shellRun(cmd('jobs.retry'), C.get('job:nightly-sync')); await flush();",
                                    answer=self.LAUNCHCTL.replace("ANSWER", answer).replace("DOC_JSON", json.dumps(self.doc)))
                self.assertEqual(out["toasts"][-1], [said, False])
                self.assertEqual([path for path in out["gets"] if path == "/api/reports"], ["/api/reports", "/api/reports"])

    def test_after_exit_127_retry_asks_first_and_names_the_exit(self):
        """sd:3392, ruled 2026-09-30: the exit is the last run's, and a path fixed outside the dashboard needs Retry to test it."""
        doc = json.loads(json.dumps(self.doc))
        next(j for j in doc["jobs"] if j["name"] == "nightly-sync")["last_exit"] = 127
        out = self.run_page("""const c = cmd('jobs.retry'), o = C.get('job:nightly-sync');
R.on = c.when(o); R.primary = !!c.primary(o); R.ask = !!c.askFirst(o); R.says = c.consequence(o);
R.other = !!c.askFirst(C.get('job:weekly-scan')); shellRun(c, o); await flush();""", doc=doc,
                            answer=self.LAUNCHCTL.replace("ANSWER", "{ request: { status: 'accepted', message: 'launchctl accepted the request' } }").replace("DOC_JSON", json.dumps(doc)))
        self.assertEqual((out["R"]["on"], out["R"]["primary"], out["R"]["ask"], out["R"]["other"]), (True, False, True, False))
        self.assertIn("exit 127", out["R"]["says"])
        self.assertEqual(out["confirms"], ["jobs.retry"])
        self.assertEqual([p for p, _, _ in out["posts"]], ["/api/jobs/nightly-sync/retry"])
        self.assertEqual(out["toasts"][-1], ["Run started · nightly-sync", False])

    def test_a_job_is_failed_by_its_state_not_by_whether_launchd_allows_a_run(self):
        """sd:4965: the run-now pack allows a retry of an idle job too, so an allowed retry no longer means a failed run."""
        doc = json.loads(json.dumps(self.doc))
        jobs = {j["name"]: j for j in doc["jobs"]}
        jobs["weekly-scan"].update(state="idle", capabilities={**jobs["weekly-scan"].get("capabilities", {}),
                                                                "retry": {"allowed": True, "label": "Run now"}})
        jobs["nightly-sync"].update(state="interrupted")
        out = self.run_page("""const c = cmd('jobs.retry');
R.p = ['job:weekly-scan', 'job:nightly-sync'].map(id => [C.get(id).failed, !!c.primary(C.get(id))]);""", doc=doc)
        self.assertEqual(out["R"]["p"], [[False, False], [True, True]], "an idle job read as failed, or an interrupted one did not")

    def test_retry_posts_the_job_route_with_its_revision(self):
        job = next(j for j in self.doc["jobs"] if j["name"] == "nightly-sync")
        out = self.run_page("shellRun(cmd('jobs.retry'), C.get('job:nightly-sync')); await flush(); R.off = cmd('jobs.retry').when(C.get('job:weekly-scan'));",
                            answer=self.LAUNCHCTL.replace("ANSWER", "{ request: { status: 'accepted', message: 'launchctl accepted the request' } }").replace("DOC_JSON", json.dumps(self.doc)))
        self.assertEqual(out["posts"], [["/api/jobs/nightly-sync/retry", {"revision": job["revision"]}, 64]])
        self.assertEqual(out["toasts"][-1], ["Run started · nightly-sync", False])
        # An idle job may run now (sd:4982), so Run now is on for it too.
        self.assertIs(out["R"]["off"], True)

    # The list grammar from shell.list (sd:2527, sd:2528, sd:2529). Each click goes to the listener the page put on its holder.
    CLICK = "ELS['%s'].listeners.click[0]({ target: { closest: s => s === %s ? { dataset: %s } : null } });"

    def many(self, n):
        """The seeded document with n reports, a day apart, newest first."""
        doc = json.loads(json.dumps(self.doc))
        base = doc["reports"][0]
        doc["reports"] = [dict(base, id=10000 + i, title=f"report {i:03d}", at=f"2026-0{1 + i // 28 % 9}-{1 + i % 28:02d}T06:00:00+00:00")
                          for i in range(n)]
        return doc

    def test_every_column_with_an_order_sorts_and_only_the_sorted_header_carries_aria_sort(self):
        click = self.CLICK % ("rows-head", "'button[data-sort]'", "{ sort: 'what' }")
        out = self.run_page(f"R.head = ELS['rows-head'].html; R.days = ELS.rows.html.includes('class=\"day\"');"
                            f" {click} R.head2 = ELS['rows-head'].html; R.order = shown.map(r => r.what); R.days2 = ELS.rows.html.includes('class=\"day\"');"
                            f" {click} R.order2 = shown.map(r => r.what); R.url = String(OUT.urls[OUT.urls.length - 1]);")
        self.assertEqual(re.findall(r'data-sort="(\w+)"', out["R"]["head"]), ["s", "what", "detail", "at", "st"])
        self.assertEqual(re.findall(r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"', out["R"]["head"]), [("descending", "at")])
        self.assertEqual(re.findall(r'aria-sort="(\w+)"[^>]*><button[^>]*data-sort="(\w+)"', out["R"]["head2"]), [("ascending", "what")])
        self.assertEqual(out["R"]["order"], sorted(out["R"]["order"], key=str.lower))
        self.assertEqual(out["R"]["order2"], out["R"]["order"][::-1])
        # Day bands read only in time order.
        self.assertEqual((out["R"]["days"], out["R"]["days2"]), (True, False))
        self.assertIn("sort=what", out["R"]["url"])
        self.assertIn("dir=desc", out["R"]["url"])

    def test_the_pager_numbers_pages_and_keeps_page_size_and_sort_in_the_url(self):
        doc = self.many(130)
        out = self.run_page("R.pager = ELS.pager.html; R.url = String(OUT.urls[OUT.urls.length - 1]); R.first = shown.slice((L.page - 1) * L.size)[0].what;"
                            + self.CLICK % ("pager", "'.list-pager [data-page]'", "{ page: '4' }")
                            + " R.url2 = String(OUT.urls[OUT.urls.length - 1]); R.pager2 = ELS.pager.html;"
                            + self.CLICK % ("pager", "'.list-pager [data-size]'", "{ size: '100' }")
                            + " R.url3 = String(OUT.urls[OUT.urls.length - 1]); R.pager3 = ELS.pager.html;",
                            doc=doc, prelude="location.search = '?page=2&size=25&sort=what&dir=desc';")
        self.assertIn('<span class="range">26–50 of 130</span>', out["R"]["pager"])
        self.assertEqual(out["R"]["first"], "report 104")
        self.assertEqual(out["R"]["url"], "page=2&size=25&sort=what&dir=desc")
        self.assertEqual(re.findall(r'data-page="(\d+)"', out["R"]["pager"]), ["1", "2", "3", "4", "6"])
        self.assertIn('<span class="range">76–100 of 130</span>', out["R"]["pager2"])
        self.assertIn("page=4", out["R"]["url2"])
        self.assertIn('<span class="range">1–100 of 130</span>', out["R"]["pager3"])
        self.assertEqual(out["R"]["url3"], "size=100&sort=what&dir=desc")
        self.assertNotIn("Newer", (V2 / "reports.html").read_text(encoding="utf-8"))

    def test_there_is_no_needs_attention_view_and_status_needs_action_still_filters(self):
        # sd:5724: the Needs attention view is gone; Status's Needs action shows the same rows.
        out = self.run_page("R.views = window.PAGE_VIEWS.map(v => v.id); R.filter = Object.keys(window.PAGE_FILTER);"
                            + self.CLICK % ("filters", "'[data-f]'", "{ f: 'status', v: 'act' }")
                            + " R.a = [F.status, shown.every(r => r.act), shown.length];")
        self.assertEqual(out["R"]["views"], ["reports", "cadence"])
        self.assertEqual(out["R"]["filter"], ["reports"])
        self.assertEqual(out["R"]["a"][:2], ["act", True])
        self.assertGreater(out["R"]["a"][2], 0)

    def test_the_filters_are_activitys_bar_whose_tokens_remove_one_and_clear_all_removes_every_one(self):
        # sd:5463: Activity's layout from shell.list.fbar. Status is a segment with its counts, Job and Day are picks, the
        # query is a token, Clear all ends the bar, and the count sits on the title line, once. sd:5064: Needs action is a
        # Status value, not a view chip or a checkbox; an old ?act=1 link lands on it.
        out = self.run_page("R.bar = ELS.filters.html; R.tally = OUT.counts.reports; R.url = String(OUT.urls[OUT.urls.length - 1]);"
                            + self.CLICK % ("filters", "'[data-f]'", "{ f: 'q', v: '' }")
                            + " R.bar2 = ELS.filters.html; R.url2 = String(OUT.urls[OUT.urls.length - 1]);"
                            + self.CLICK % ("filters", "'[data-f]'", "{ f: 'status', v: 'act' }")
                            + " R.st2 = F.status; R.bar3 = ELS.filters.html;"
                            + "ELS.filters.listeners.click[0]({ target: { closest: s => s === '[data-fclear]' ? {} : null } });"
                            + " R.bar4 = ELS.filters.html; R.url4 = String(OUT.urls[OUT.urls.length - 1]); R.tally4 = OUT.counts.reports;"
                            + " R.F = [F.job, F.status, F.q, (OUT.shift ?? null)]; R.n = shown.length; R.all = ROWS.length; R.open = ROWS.filter(r => r.open).length;",
                            prelude="location.search = '?job=nightly-sync&act=1&q=sync';")
        R, all_ = out["R"], out["R"]["all"]
        bar = R["bar"]
        self.assertTrue(bar.startswith('<div class="fbar" role="group" aria-label="Filters"><div class="fseg" role="group" aria-label="Status">'), bar[:120])
        self.assertEqual(re.findall(r'data-f="status" data-v="(\w+)"', bar), ["act", "open", "done"])
        self.assertIn('data-v="act" aria-pressed="true" title="Needs action: ', bar)
        self.assertIn('<option value="nightly-sync" selected>nightly-sync</option>', bar)
        self.assertIn('<select id="f-day" class="fsel" data-pick="day"><option value="" selected>Any day</option>', bar)
        self.assertEqual(re.findall(r'class="tok" data-f="(\w+)"', bar), ["q"])
        self.assertTrue(bar.endswith('<div class="fsum"><button class="linkbtn" type="button" data-fclear>Clear all</button></div></div>'))
        self.assertNotIn(" of ", re.sub(r"<[^>]+>", "", bar.split('class="fsum"')[1]), "the bar holds no count")
        self.assertLess(R["tally"], all_, "sd:5524: the Reports tab carries the rows the filters leave")
        self.assertEqual(R["url"], "job=nightly-sync&status=act&q=sync")
        self.assertNotIn('class="tok"', R["bar2"])
        self.assertEqual(R["url2"], "job=nightly-sync&status=act")
        self.assertEqual(R["st2"], "", "a pressed segment lifts its filter")
        self.assertIn('data-fclear', R["bar3"], "Job still filters")
        self.assertNotIn('data-fclear', R["bar4"])
        self.assertTrue(R["bar4"].endswith('<div class="fsum"></div></div>'))
        self.assertEqual(R["url4"], "")
        self.assertEqual(R["tally4"], R["open"], "sd:8018: the tab counts the open rows shown")
        self.assertEqual(R["F"], ["", "", "", ""])
        self.assertEqual(R["n"], all_)

    def test_the_page_has_no_view_chips_and_no_needs_action_checkbox(self):
        # sd:5064: the filter bar is the one filter control, so the saved-view row and the f-act box are gone.
        page = (V2 / "reports.html").read_text(encoding="utf-8")
        self.assertNotIn('id="views"', page)
        self.assertNotIn('id="f-act"', page)
        self.assertIn("st('act', 'Needs action'", REPORTS_JS)
        self.assertNotIn("shell.views", REPORTS_JS)
        self.assertNotIn('id="chips"', page, "the bar's tokens replace the chip row")

    def test_the_script_adds_no_sink_no_inline_style_and_no_own_list_keys(self):
        for script in (REPORTS_JS, REPORT_DETAILS_JS):
            self.assertNotIn("innerHTML", script)
            self.assertNotIn("setAttribute('style'", script)
            self.assertNotRegex(script, r"style=\\?\"")
        self.assertNotIn("history.replaceState", REPORTS_JS)
        self.assertNotIn("location.reload", REPORTS_JS)
        self.assertNotRegex(REPORTS_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", REPORTS_JS)
        self.assertNotIn("REPORTS_DATA", REPORTS_JS)


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "reports", "Reports", "/reports", ("/api/reports",)


if __name__ == "__main__":
    import unittest

    unittest.main()
