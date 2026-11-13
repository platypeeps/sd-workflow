"""The v2 Management page (sd:2118).

What this slice promises: `/management` answers under the shared policy and
loads its script before `shell.js`; `/api/management` is one document built
from the reads v1 already makes (the sd-db repo table with each row's review
declaration and protection reading, the fleet's git state and
sessions, services and jobs), where a source that fails is
a reason and never an empty list; `/api/repos/<verb>` runs the two sd-db repo
verbs and refuses a stale `before`.

`management.js` runs under JavaScriptCore (osascript, as `test_v2_tasks`
runs `tasks.js`) against the same stand-in page and shell, with the document
above as its fetch answer. The browser half -- focus, the look at 375 px --
is a manual check recorded on the pull request.
"""

from __future__ import annotations

import base64
import datetime
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import management_screen, queue_screen, server, upload_link, v2
from sd_db import repos, upsert_repo, upsert_shadow, workflow, writes
from sd_db.errors import SdDbError
from support import NOW, ScreenCase
from test_now_screen import JobsBackend, fleet_document, tree
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
PAGE_JS = (V2 / "static" / "management.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")


def git_row(name, *, behind=0, dirty=0, ahead=0, branch="main"):
    """A fleet repos row with every key `repos_screen.primary` reads."""
    return {"name": name, "group": ".", "path": f"/repos/{name}", "branch": branch, "default": "main", "dirty": dirty,
            "ahead": ahead, "behind": behind, "behind_default": behind, "fetched_iso": NOW.replace("Z", "+00:00"),
            "last": "2026-09-01", "last_iso": "2026-09-01T10:00:00+00:00", "subject": "a commit", "author": "someone",
            "web": "", "truncated": [], "error": ""}


def fleet(area):
    if area == "repos":
        return {"root": "/repos", "rootExists": True, "repos": [git_row("system", behind=2), git_row("busy", behind=1, dirty=3)],
                "counts": {"repos": 2, "dirty": 1, "ahead": 0, "unread": 0}}
    return fleet_document(area, trees=[tree("gone", live=False), tree("here")])


def dark_fleet(area):
    raise ValueError("fleet collection was stopped at its budget")


def claude_row(session, name, *, status="idle", safe="yes", why=(), waiting_for="", tasks=()):
    return {"id": session, "name": name, "kind": "interactive", "pid": 100, "cwd": f"/repos/{name}", "started": "2026-09-06T11:00:00Z",
            "status": status, "waiting": status == "waiting", "waiting_for": waiting_for, "last_action": "2026-09-06T11:58:00Z",
            "age": 120, "age_text": "2m", "builder": False, "tasks": list(tasks), "agents": [], "watches": [], "checkouts": [],
            "safe": {"state": safe, "why": list(why)}}


#: What `claude_sessions.document` answers for every test here: the real one runs claude, ps and git (sd:5106).
CLAUDE = {"read": NOW, "sources": {"agents": "", "processes": "", "worktrees": ""}, "builders": {"running": 1, "why": ""},
          "sessions": [claude_row("s-wait", "hoa", status="waiting", safe="no", why=["it waits on you: permission: Bash"],
                                  waiting_for="permission: Bash",
                                  tasks=[{"item": 7, "via": "branch sd7-x", "title": "Port the page", "status": "ready"}]),
                       claude_row("s-idle", "system")]}


def claude_stub(connection, *, now, fleet=None):
    return json.loads(json.dumps(CLAUDE))


def setUpModule():
    patcher = patch("sd_dashboard.claude_sessions.document", claude_stub)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def stamp(backend, name, text):
    """The cron-jobs wrapper's `logs/.<job>.stamp` for `name` (sd:2210)."""
    (backend.cron_root / "logs" / f".{name}.stamp").write_text(text)


class NoServices:
    """A services backend double that lists nothing, so no launchctl call is made."""

    def names(self):
        return []


def seed(case):
    """A registered checkout with a review declaration, a blocked author run and a finished merge."""
    checkout = Path(case.tmp.name).resolve() / "checkout"
    checkout.mkdir(parents=True)
    upsert_repo(case.connection, str(checkout), remote="git@example.invalid:example/checkout.git")
    repos.set_declaration(case.connection, str(checkout), "review",
                          {"severity_floor": "high", "copilot_review": {"automatic_deep": False}})
    case.repo()
    item = case.item("Port the page", repo="/repos/system")
    blocked = case.assignment(item, status="blocked")
    merged = case.assignment(item, role="merge", status="done")
    return {"checkout": str(checkout), "item": item, "blocked": blocked, "merged": merged}


class TheDocument(ScreenCase):
    def setUp(self):
        super().setUp()
        self.ids = seed(self)
        self.jobs = JobsBackend(self.tmp.name, jobs=[("nightly-sync", "failed", 7, None), ("quiet", "idle", 0, None)])

    def document(self, read=fleet):
        return management_screen.document(self.connection, now=NOW, fleet=read, jobs=self.jobs, services=NoServices())

    def test_every_source_is_read_and_says_so(self):
        doc = self.document()
        # pack sd:7115: the lane's assignments moved to Queue, so the page reads no lane.
        self.assertEqual(doc["sources"], {k: "" for k in ("repos", "pulls", "deep", "git", "sessions",
                                                          "claude", "services", "jobs", "satellites", "upload")})
        self.assertEqual(doc["read"], NOW)
        # sd:3041: the runner stopped, so no archive refresh from runner.sh and no runner heartbeat.
        self.assertNotIn("archive", doc)
        self.assertNotIn("lane", doc)

    def test_pulls_are_the_open_shadow_pull_requests_by_lower_cased_slug(self):
        """sd:5100: the repo row's Adversarial review: PR picks from these; nothing calls GitHub."""
        for number, repo, kind, state in ((7, "Example/Checkout", "pull", "open"), (8, "example/checkout", "issue", "open"),
                                          (9, "example/checkout", "pull", "closed"), (3, "example/other", "pull", "open")):
            upsert_shadow(self.connection, tracker="github", url=f"https://github.com/{repo}/pull/{number}", repo=repo,
                          number=number, kind=kind, title=f"Pull {number}", state=state, author="someone")
        self.assertEqual(self.document()["pulls"], {
            "example/checkout": [{"number": 7, "title": "Pull 7", "author": "someone",
                                  "url": "https://github.com/Example/Checkout/pull/7"}],
            "example/other": [{"number": 3, "title": "Pull 3", "author": "someone",
                               "url": "https://github.com/example/other/pull/3"}]})

    def test_deep_carries_each_setting_row_or_its_default(self):
        """sd:5109: the Deep review's settings are workflow-database rows, shown beside the control."""
        writes.set_setting(self.connection, "adversarial.deep.model", "fable")
        deep = self.document()["deep"]
        self.assertEqual(deep["adversarial.deep.model"], {"value": "fable", "from": "database"})
        self.assertEqual(deep["adversarial.deep.agents"], {"value": "8", "from": "default"})
        self.assertEqual(set(deep), {"adversarial.deep.model", "adversarial.deep.effort",
                                     "adversarial.deep.bound_minutes", "adversarial.deep.agents"})

    def test_a_deep_setting_row_that_does_not_read_is_the_source_s_reason(self):
        writes.set_setting(self.connection, "adversarial.deep.agents", "many")
        doc = self.document()
        self.assertIsNone(doc["deep"])
        self.assertIn("adversarial.deep.agents wants a number of sub-agents", doc["sources"]["deep"])

    def test_satellites_carry_the_tool_s_document_and_a_bad_row_is_a_reason(self):
        """sd:5108: the Refresh satellites rows, read through `satellite_refresh.document`. What each satellite
        answers is the system checkout's to test; here the tool is the suite's fake (sd:6044)."""
        from sd_db import writes

        self.assertEqual(self.document()["satellites"], {"request": None, "satellites": []})
        sr = management_screen._satellite_refresh()
        asked = management_screen.refresh_satellites(self.connection)
        satellites = self.document()["satellites"]
        self.assertEqual(satellites, sr.document(self.connection))
        self.assertEqual(satellites["request"], asked)
        writes.set_setting(self.connection, sr.REQUEST, "not json")
        doc = self.document()
        self.assertIsNone(doc["satellites"])
        self.assertIn("Expecting value", doc["sources"]["satellites"])

    def test_a_job_carries_its_last_run_from_the_wrappers_stamp(self):
        """sd:2210: launchd keeps no run time; the cron-jobs wrapper stamps each run's start, end and exit."""
        stamp(self.jobs, "nightly-sync", "started=2026-09-06T02:15:00Z\nended=2026-09-06T02:16:30Z\nexit=7\n")
        jobs = {job["name"]: job for job in self.document()["jobs"]}
        self.assertEqual(jobs["nightly-sync"]["last_run"], {"state": "finished", "started": "2026-09-06T02:15:00Z",
                                                            "ended": "2026-09-06T02:16:30Z", "exit": 7, "reason": ""})
        quiet = jobs["quiet"]["last_run"]
        self.assertEqual((quiet["state"], quiet["started"]), ("none", None))
        self.assertIn("no run stamp", quiet["reason"])

    def test_a_stamp_with_no_end_is_open_and_a_broken_one_is_unread_with_its_reason(self):
        stamp(self.jobs, "nightly-sync", "started=2026-09-06T02:15:00Z\n")
        stamp(self.jobs, "quiet", "started=yesterday\nended=2026-09-06T02:16:30Z\nexit=0\n")
        jobs = {job["name"]: job["last_run"] for job in self.document()["jobs"]}
        self.assertEqual(jobs["nightly-sync"], {"state": "open", "started": "2026-09-06T02:15:00Z", "ended": None, "exit": None,
                                                "reason": ""})
        self.assertEqual((jobs["quiet"]["state"], jobs["quiet"]["reason"]), ("unread", "the run stamp names no start time"))
        stamp(self.jobs, "quiet", "started=2026-09-06T02:15:00Z\nended=2026-09-06T02:16:30Z\n")
        quiet = {job["name"]: job["last_run"] for job in self.document()["jobs"]}["quiet"]
        self.assertEqual((quiet["state"], quiet["reason"]), ("unread", "the run stamp has an end without an exit code"))

    def test_a_job_carries_the_end_of_its_log_from_a_whole_line(self):
        """sd:3388: the page reads a job's log as v1's Jobs card could not: the end of logs/<job>.log, whole lines only."""
        lines = [f"[nightly-sync] line {n:04d} of the run" for n in range(400)]
        (self.jobs.cron_root / "logs" / "nightly-sync.log").write_text("\n".join(lines) + "\n")
        jobs = {job["name"]: job["log"] for job in self.document()["jobs"]}
        log = jobs["nightly-sync"]
        self.assertEqual((log["state"], log["cut"], log["reason"]), ("read", True, ""))
        self.assertTrue(log["text"].endswith(lines[-1] + "\n"))
        self.assertIn(log["text"].splitlines()[0], lines)
        self.assertLessEqual(len(log["text"].encode()), management_screen.LOG_BYTES)
        self.assertEqual((jobs["quiet"]["state"], jobs["quiet"]["text"]), ("none", ""))
        self.assertIn("no log", jobs["quiet"]["reason"])

    def test_a_short_log_is_read_whole_and_a_log_that_is_not_a_file_is_unread_without_blocking(self):
        import os
        import threading

        (self.jobs.cron_root / "logs" / "nightly-sync.log").write_text("one run\nFAILED rc=7\n")
        os.mkfifo(self.jobs.cron_root / "logs" / "quiet.log")
        alone = {}
        # The connection belongs to this thread, so the bounded read is tried alone in another first.
        reader = threading.Thread(target=lambda: alone.update(management_screen.job_log(self.jobs.cron_root, "quiet")), daemon=True)
        reader.start()
        reader.join(5)
        self.assertFalse(reader.is_alive(), "reading a FIFO log blocked the document")
        got = {job["name"]: job["log"] for job in self.document()["jobs"]}
        self.assertEqual(alone, got["quiet"])
        self.assertEqual(got["nightly-sync"], {"state": "read", "text": "one run\nFAILED rc=7\n", "cut": False, "reason": ""})
        self.assertEqual((got["quiet"]["state"], got["quiet"]["reason"]), ("unread", "the log is not a regular file"))
        self.assertEqual(management_screen.job_log(None, "quiet")["reason"], "the jobs backend names no logs folder")

    def test_a_job_carries_the_last_request_and_launchctls_answer(self):
        """sd:3388: a cancel's outcome is what the next read shows, as v1's Last request line did."""
        from sd_db import operations

        class Sends(JobsBackend):
            calls = []

            def perform(self, action, service):
                self.calls.append((action, service))
                return subprocess.CompletedProcess([], 0, "", "")

        backend = Sends(self.tmp.name, jobs=[("running-fixture", "running", None, None)])
        doc = management_screen.document(self.connection, now=NOW, fleet=fleet, jobs=backend, services=NoServices())
        running = next(job for job in doc["jobs"] if job["name"] == "running-fixture")
        self.assertIsNone(running["last_request"])
        self.assertTrue(running["capabilities"]["cancel"]["allowed"])
        operations.cancel_job(self.connection, "running-fixture", expected_revision=running["revision"], who="dashboard", backend=backend)
        doc = management_screen.document(self.connection, now=NOW, fleet=fleet, jobs=backend, services=NoServices())
        request = next(job for job in doc["jobs"] if job["name"] == "running-fixture")["last_request"]
        self.assertEqual((request["action"], request["status"], request["who"]), ("cancel", "accepted", "dashboard"))
        self.assertEqual(backend.calls, [("cancel", "fixture/running-fixture")])

    def test_the_badge_counts_failed_jobs_and_an_unread_jobs_list_is_unknown(self):
        # sd:3446: the read's rule, which every page's rail shows from Management's last read.
        jobs = [{"state": "failed"}, {"state": "interrupted"}, {"state": "running"}, {"state": "failed"}]
        self.assertEqual(management_screen._attention(jobs), {"state": "warning", "n": 2, "what": "scheduled jobs failed"})
        self.assertEqual(management_screen._attention(jobs[1:3]), {"state": "ok", "n": 0, "what": "scheduled jobs failed"})
        self.assertEqual(management_screen._attention(None), {"state": "unknown", "n": 0, "what": "scheduled jobs not read"})
        self.assertEqual(self.document()["attention"], management_screen._attention(self.document()["jobs"]))

    def test_a_repo_carries_its_row_its_review_declaration_and_its_protection_reading(self):
        rows = {row["path"]: row for row in self.document()["repos"]}
        checkout = rows[self.ids["checkout"]]
        self.assertEqual((checkout["runner_merge"], checkout["managed"]), ("manual", "no"))
        self.assertEqual((checkout["review"]["severity_floor"], checkout["review"]["automatic_deep"]), ("high", False))
        self.assertIn('"severity_floor": "high"', checkout["review"]["declaration"])
        self.assertEqual(checkout["protection"]["status"], "unknown")
        # sd:5159: a row that declares none reads as none, whether or not its checkout is on disk.
        self.assertEqual(rows["/repos/system"]["review"], {"declaration": None})

    def test_the_review_setting_is_the_rows_declaration_not_the_checkout_file(self):
        """sd:5159: pack #1577 moved `.github/sd-review.json` into the repo row; a file left in a checkout is not read."""
        checkout = Path(self.ids["checkout"])
        (checkout / ".github").mkdir()
        (checkout / ".github" / "sd-review.json").write_text('{"severity_floor": "low"}')
        bare = Path(self.tmp.name).resolve() / "bare"
        (bare / ".github").mkdir(parents=True)
        (bare / ".github" / "sd-review.json").write_text('{"severity_floor": "medium"}')
        upsert_repo(self.connection, str(bare))
        rows = {row["path"]: row for row in self.document()["repos"]}
        self.assertEqual(rows[str(checkout)]["review"]["severity_floor"], "high")
        self.assertEqual(rows[str(bare)]["review"], {"declaration": None})

    def test_an_unread_declarations_column_is_an_error_not_an_absent_declaration(self):
        """sd:5159: a column that does not read is unknown; the page offers no Add for it."""
        unread = SdDbError("repo.declarations does not read as a JSON object; set it again")
        with patch.object(management_screen.repos, "declarations", side_effect=unread):
            doc = self.document()
        self.assertEqual(doc["sources"]["repos"], "")
        review = {row["path"]: row for row in doc["repos"]}[self.ids["checkout"]]["review"]
        self.assertIsNone(review["declaration"])
        self.assertTrue(review["error"].startswith("not read: "), review["error"])

    def test_an_sd_db_without_the_declarations_reader_is_an_error_not_an_absent_declaration(self):
        """sd:5159: a pack before migration 37 has no `repos.declarations`; nothing says the row declares none."""
        with patch.object(management_screen.repos, "declarations", None):
            doc = self.document()
        review = {row["path"]: row for row in doc["repos"]}[self.ids["checkout"]]["review"]
        self.assertEqual(review["declaration"], None)
        self.assertIn("update the command pack", review["error"])

    def test_a_checkout_borrowing_a_siblings_protection_row_names_the_lender(self):
        """sd:1607: the GitHub panel says which checkout's reading it shows."""
        upsert_repo(self.connection, "/repos/widget", remote="git@github.com:example/widget.git")
        upsert_repo(self.connection, "/repos/widget-copy", remote="git@github.com:example/widget.git")
        self.connection.execute("INSERT INTO repo_protection (repo, observed_at, status, default_branch, body) "
                                "VALUES ('/repos/widget', '2026-10-04T01:00:00Z', 'protected', 'main', '{}')")
        self.connection.commit()
        rows = {row["path"]: row for row in self.document()["repos"]}
        self.assertEqual(rows["/repos/widget-copy"]["protection"]["status"], "protected")
        self.assertEqual(rows["/repos/widget-copy"]["protection"]["borrowed_from"], "/repos/widget")
        self.assertIsNone(rows["/repos/widget"]["protection"]["borrowed_from"])

    def test_a_protection_row_from_an_older_library_reads_as_the_checkouts_own(self):
        """An installed `sd_db` from before sd:1607 returns rows with no `borrowed_from`."""
        from sd_db import protection
        older = [{key: value for key, value in row.items() if key != "borrowed_from"}
                 for row in protection.rows(self.connection)]
        with patch.object(protection, "rows", lambda connection: older):
            doc = self.document()
        self.assertIsNotNone(doc["repos"], doc["sources"])
        rows = {row["path"]: row for row in doc["repos"]}
        self.assertIsNone(rows[self.ids["checkout"]]["protection"]["borrowed_from"])

    def test_git_state_is_primarys_reading_with_its_remedy_or_refusal(self):
        git = {row["name"]: row for row in self.document()["git"]["repos"]}
        self.assertEqual((git["system"]["state"], git["system"]["remedy"]), ("behind", "git -C /repos/system pull --ff-only"))
        self.assertEqual(git["busy"]["remedy"], "No pull offered: 3 uncommitted files.")

    def test_the_document_carries_no_assignment_history_and_no_merge_assignments(self):
        """sd:5472: the author runner has been idle since 2026-09-27; the page lists neither its assignments nor its merges."""
        doc = self.document()
        self.assertNotIn("assignments", doc)
        self.assertNotIn("assignments", doc["sources"])
        lane = queue_screen.runner(self.connection)
        self.assertNotIn("merges", lane)
        # sd:8651: blocked builds are live work for Queue, not history.
        self.assertEqual(sorted(lane), ["blocked", "live"])
        self.assertEqual((doc["sessions"]["registered"], doc["sessions"]["abandoned"]), (2, 1))
        jobs = {job["name"]: job for job in doc["jobs"]}
        self.assertEqual((jobs["nightly-sync"]["state"], jobs["nightly-sync"]["capabilities"]["retry"]["allowed"]), ("failed", True))

    def test_sessions_carry_each_worktree_abandoned_first(self):
        """pack sd:7517: the rows Operations > Sessions listed, so Management can carry them alone."""
        unread = dict(tree("odd", live=False), state="unknown", path="")
        doc = self.document(read=lambda area: fleet(area) if area == "repos"
                            else fleet_document(area, trees=[tree("here"), unread, tree("gone", live=False)]))
        rows = doc["sessions"]["worktrees"]
        self.assertEqual([(row["name"], row["state"]) for row in rows],
                         [("gone", "abandoned"), ("odd", "unknown"), ("here", "live")])
        self.assertEqual(rows[0], {"repo": "system", "name": "gone", "branch": "gone", "path": "/wt/gone",
                                   "state": "abandoned"})
        # An unreadable registration is not known to be abandoned (fleet.STATES): Prune would not clear it.
        self.assertEqual((doc["sessions"]["registered"], doc["sessions"]["abandoned"], doc["sessions"]["unreadable"]),
                         (3, 1, 1))

    def test_sessions_say_a_cut_process_table_and_a_missing_root(self):
        """pack sd:7517: the guards Operations > Sessions kept: a cut ps table is a floor; a missing root is no count."""
        def read(area):
            if area == "repos":
                return fleet(area)
            return dict(fleet_document(area, trees=[]), processes_truncated=True, rootExists=False, root="/nowhere")
        sess = self.document(read=read)["sessions"]
        self.assertEqual((sess["processes_truncated"], sess["root_exists"], sess["root"]), (True, False, "/nowhere"))
        self.assertEqual((self.document()["sessions"]["processes_truncated"], self.document()["sessions"]["root_exists"]),
                         (False, True))

    def test_a_service_carries_its_last_request(self):
        """pack sd:7517: Operations > Services showed each service's last request; Management's Details now does."""
        from sd_db import services

        request = {"action": "restart", "status": "accepted", "at": NOW, "who": "dashboard", "message": "ok"}
        row = {"name": "svc", "label": "local.example.svc", "scope": "user", "domain": "gui/501", "category": "tool",
               "state": "running", "pid": 1, "last_exit": 0, "last_signal": None, "revision": "0" * 64,
               "capabilities": {}, "last_request": request}
        with patch.object(services, "inventory", lambda connection, backend=None: {"services": [row]}):
            doc = self.document()
        self.assertEqual(doc["services"][0]["last_request"], request)

    def test_a_source_that_fails_is_a_reason_and_the_others_still_answer(self):
        doc = self.document(read=dark_fleet)
        self.assertIsNone(doc["git"])
        self.assertIsNone(doc["sessions"])
        self.assertEqual(doc["sources"]["git"], "fleet collection was stopped at its budget")
        self.assertEqual(doc["sources"]["repos"], "")
        self.assertTrue(doc["repos"])

    def test_claude_sessions_ride_along_and_an_unlisted_claude_is_a_reason_not_an_empty_list(self):
        """sd:5106: the Sessions view's Claude sessions; claude that did not answer is unknown, not no session."""
        doc = self.document()
        self.assertEqual([row["id"] for row in doc["claude"]["sessions"]], ["s-wait", "s-idle"])
        seen = []

        def dark(connection, *, now, fleet=None):
            seen.append(fleet)
            return {"read": now, "sessions": None, "sources": {"agents": "claude agents --json gave no answer in 10 s"},
                    "builders": {"running": None, "why": "x"}}

        doc = management_screen.document(self.connection, now=NOW, fleet=fleet, jobs=self.jobs, services=NoServices(), claude=dark)
        self.assertIsNone(doc["claude"])
        self.assertEqual(doc["sources"]["claude"], "claude agents --json gave no answer in 10 s")
        self.assertEqual(seen, [fleet], "the Claude read joins the same fleet read the page makes")

    def test_a_snapshot_section_carries_when_it_was_observed_and_why_it_is_stale(self):
        def kept(area):
            return dict(fleet(area), observed="2026-09-06T11:52:00Z", **({"stale": "git did not answer"} if area == "sessions" else {}))

        doc = self.document(read=kept)
        self.assertEqual((doc["git"]["observed"], doc["git"]["stale"]), ("2026-09-06T11:52:00Z", None))
        self.assertEqual((doc["sessions"]["observed"], doc["sessions"]["stale"]), ("2026-09-06T11:52:00Z", "git did not answer"))
        self.assertNotIn("observed", self.document()["git"])

    def test_the_repo_verb_writes_and_refuses_a_stale_before(self):
        got = management_screen.set_repo(self.connection, "runner-merge", self.ids["checkout"], "auto", "manual")
        self.assertEqual((got["value"], got["before"]), ("auto", "manual"))
        with self.assertRaises(workflow.StaleItem):
            management_screen.set_repo(self.connection, "runner-merge", self.ids["checkout"], "auto", "manual")
        management_screen.set_repo(self.connection, "managed", self.ids["checkout"], "yes", "no")
        rows = {row["path"]: row for row in self.document()["repos"]}
        self.assertEqual((rows[self.ids["checkout"]]["runner_merge"], rows[self.ids["checkout"]]["managed"]), ("auto", "yes"))

    def test_a_repo_carries_its_dependabot_flag_and_the_owner_gate_and_the_verb_flips_it(self):
        """sd:3984: the flag is the row's column; a platypeeps row is the owner's and a denied one is denied."""
        checkout = self.ids["checkout"]
        upsert_repo(self.connection, "/repos/own", remote="git@github.com:platypeeps/own.git")
        upsert_repo(self.connection, "/repos/denied", remote="git@github.com:platypeeps/sd-review-test.git")
        rows = {row["path"]: row for row in self.document()["repos"]}
        self.assertEqual([(rows[path]["dependabot"], rows[path]["dependabot_gate"]) for path in
                          (checkout, "/repos/own", "/repos/denied")], [("no", None), ("no", "owner"), ("no", "denied")])
        got = management_screen.set_repo(self.connection, "dependabot", checkout, "yes", "no")
        self.assertEqual((got["field"], got["value"], got["before"]), ("dependabot", "yes", "no"))
        with self.assertRaises(workflow.StaleItem):
            management_screen.set_repo(self.connection, "dependabot", checkout, "yes", "no")
        self.assertEqual({row["path"]: row for row in self.document()["repos"]}[checkout]["dependabot"], "yes")
        from sd_db.remote import HubOnly

        with patch("sd_db.database.served_by", return_value="hub.example.test:8765"):
            with self.assertRaises(HubOnly):
                management_screen.set_repo(self.connection, "dependabot", checkout, "no", "yes")
        self.assertEqual({row["path"]: row for row in self.document()["repos"]}[checkout]["dependabot"], "yes")

    def test_a_repo_carries_its_github_issues_setting_and_the_verb_flips_it(self):
        """sd:4840, pack sd:6849: `policy.issues` reads off, read or ask and `repos.set_github_issues` writes it,
        refusing a stale before."""
        checkout = self.ids["checkout"]
        self.assertEqual({row["path"]: row for row in self.document()["repos"]}[checkout]["github_issues"], "off")
        got = management_screen.set_repo(self.connection, "github-issues", checkout, "ask", "off")
        self.assertEqual((got["field"], got["value"], got["before"]), ("github_issues", "ask", "off"))
        with self.assertRaises(workflow.StaleItem):
            management_screen.set_repo(self.connection, "github-issues", checkout, "read", "off")
        self.assertEqual({row["path"]: row for row in self.document()["repos"]}[checkout]["github_issues"], "ask")

    def test_a_repo_carries_its_issues_scope_and_the_verb_flips_it(self):
        """pack sd:7672: `policy.issues_scope` reads all or mine and `repos.set_issues_scope` writes it,
        refusing a stale before."""
        checkout = self.ids["checkout"]
        self.assertEqual({row["path"]: row for row in self.document()["repos"]}[checkout]["issues_scope"], "all")
        got = management_screen.set_repo(self.connection, "issues-scope", checkout, "mine", "all")
        self.assertEqual((got["field"], got["value"], got["before"]), ("issues_scope", "mine", "all"))
        with self.assertRaises(workflow.StaleItem):
            management_screen.set_repo(self.connection, "issues-scope", checkout, "mine", "all")
        self.assertEqual({row["path"]: row for row in self.document()["repos"]}[checkout]["issues_scope"], "mine")

    def test_the_lane_host_reads_hub_and_moves_refusing_a_stale_before_a_bad_name_and_an_unknown_path(self):
        """sd:3075, acceptances 2 and 3. NULL reads `hub`; a move writes the name."""
        checkout = self.ids["checkout"]
        lane = lambda: {row["path"]: row for row in self.document()["repos"]}[checkout]["lane_host"]  # noqa: E731
        self.assertEqual(lane(), "hub")
        got = management_screen.set_repo(self.connection, "lane-host", checkout, "build-2", "hub")
        self.assertEqual((got["field"], got["value"], got["before"]), ("lane_host", "build-2", "hub"))
        self.assertEqual(lane(), "build-2")
        with self.assertRaises(workflow.StaleItem):
            management_screen.set_repo(self.connection, "lane-host", checkout, "build-3", "hub")
        with self.assertRaises(repos.RepoRefusal) as refused:
            management_screen.set_repo(self.connection, "lane-host", checkout, "Build_2", "build-2")
        self.assertIn("not a lane host", str(refused.exception))
        with self.assertRaises(repos.RepoRefusal):
            management_screen.set_repo(self.connection, "lane-host", checkout + "-absent", "hub", "build-2")
        # sd:3075 part 1: the hub cannot take build-2's ship lock, so it cannot move the lane back (sd:3003 part 2).
        with self.assertRaises(repos.RepoRefusal) as refused:
            management_screen.set_repo(self.connection, "lane-host", checkout, "hub", "build-2")
        self.assertIn("run the move on build-2 (sd:3003 part 2)", str(refused.exception))
        self.assertEqual(lane(), "build-2")

    def test_the_channels_form_writes_a_setting_and_refuses_a_stale_before_a_bad_folder_and_a_satellite(self):
        """sd:3465: a value writes through `repos.set_channels` and answers the render remedy; an old `before`, a folder
        `channels.validate` refuses and a satellite's write each change nothing. NULL reads `none`, the defaults."""
        from sd_db.remote import HubOnly

        checkout = self.ids["checkout"]
        row = lambda: {r["path"]: r for r in self.document()["repos"]}[checkout]  # noqa: E731
        self.assertEqual((row()["channels"], row()["channels_problem"]), ("none", None))
        got = management_screen.set_repo(self.connection, "channels", checkout,
                                          {"obsidian": {"folder": "Briefs/x"}, "drive": {}}, "none")
        written = '{"drive":{},"obsidian":{"folder":"Briefs/x"}}'
        self.assertEqual((got["field"], got["value"], got["before"]), ("channels", written, "none"))
        self.assertIn("sd-research-kit render", got["remedy"])
        self.assertEqual(row()["channels"], written)
        with self.assertRaises(workflow.StaleItem):
            management_screen.set_repo(self.connection, "channels", checkout, None, "none")
        with self.assertRaises(repos.RepoRefusal) as refused:
            management_screen.set_repo(self.connection, "channels", checkout, {"notion": {"folder": "a/b"}}, written)
        self.assertIn("one page name", str(refused.exception))
        with patch("sd_db.database.served_by", return_value="hub.example.test:8765"):
            with self.assertRaises(HubOnly) as refused:
                management_screen.set_repo(self.connection, "channels", checkout, None, written)
        self.assertIn("repo channels runs on the sd hub only", str(refused.exception))
        self.assertEqual(row()["channels"], written)
        got = management_screen.set_repo(self.connection, "channels", checkout, None, written)
        self.assertEqual((got["value"], got["before"], row()["channels"]), ("none", written, "none"))

    def test_a_channels_write_answers_the_stored_value_when_the_library_keeps_a_feed(self):
        """sd:4632: a set that omits `feed` keeps the stored one, so the answer is the row read back, not the posted object:
        the form's Undo then compares against what the row holds."""
        checkout = self.ids["checkout"]
        feed = '{"feed":{"url":"https://example.test/feed.xml"}}'
        self.connection.execute("UPDATE repo SET channels = ? WHERE path = ?", (feed, checkout))
        self.connection.commit()
        stored = lambda: self.connection.execute("SELECT channels FROM repo WHERE path = ?", (checkout,)).fetchone()[0]  # noqa: E731
        got = management_screen.set_repo(self.connection, "channels", checkout, {"obsidian": {}}, feed)
        kept = '{"feed":{"url":"https://example.test/feed.xml"},"obsidian":{}}'
        self.assertEqual(stored(), kept)
        self.assertEqual((got["value"], got["before"]), (kept, feed))
        got = management_screen.set_repo(self.connection, "channels", checkout, {"obsidian": {}}, kept)
        self.assertEqual(got["value"], kept, "a set that changes nothing answers the row, still")

    def test_the_channels_answer_is_read_in_the_transaction_that_wrote_it(self):
        """sd:4632: a writer between the write and the read-back would put its setting into this form's Undo as `before`."""
        checkout = self.ids["checkout"]
        seen = []
        real = repos.row_for

        def probe(connection, path):
            seen.append(connection.in_transaction)
            return real(connection, path)

        with patch.object(management_screen.repos, "row_for", probe):
            management_screen.set_repo(self.connection, "channels", checkout, {"drive": {}}, "none")
        self.assertEqual(seen, [True])

    def test_a_stored_channels_value_the_library_refuses_is_named_not_read_as_the_defaults(self):
        # sd:3465: `repo_channels` raises on such a value rather than publish to the defaults; the form says why and offers Clear.
        self.connection.execute("UPDATE repo SET channels = ? WHERE path = ?", ('{"site": {}}', self.ids["checkout"]))
        self.connection.commit()
        got = {r["path"]: r for r in self.document()["repos"]}[self.ids["checkout"]]
        self.assertEqual(got["channels"], '{"site":{}}')
        self.assertIn("'site' is not a channel", got["channels_problem"])

    def test_a_satellite_refuses_a_lane_move_and_writes_nothing(self):
        """sd:3075 part 1: a move holds the lane host's ship lock, which only the hub's dashboard reaches."""
        from sd_db.remote import HubOnly

        with patch("sd_db.database.served_by", return_value="hub.example.test:8765"):
            with self.assertRaises(HubOnly) as refused:
                management_screen.set_repo(self.connection, "lane-host", self.ids["checkout"], "build-2", "hub")
        self.assertIn("repo lane-host runs on the sd hub only", str(refused.exception))
        self.assertIsNone(self.connection.execute(
            "SELECT lane_host FROM repo WHERE path = ?", (self.ids["checkout"],)).fetchone()[0])

    def test_a_repo_carries_its_required_checks_and_its_runtime_pins(self):
        """sd:1629: the overview's columns. Required checks are the protection reading's; runtimes are the checkout's pins."""
        checkout = Path(self.ids["checkout"])
        (checkout / ".python-version").write_text("3.14\n")
        (checkout / "package.json").write_text('{"engines": {"node": ">=22"}}')
        body = {"gaps": [], "detail": {"required_contexts": ["ci", "sd/local-gate"], "strict": True}}
        self.connection.execute("INSERT INTO repo_protection (repo, observed_at, status, default_branch, body) "
                                "VALUES (?, '2026-10-04T01:00:00Z', 'protected', 'main', ?)", (str(checkout), json.dumps(body)))
        self.connection.commit()
        rows = {row["path"]: row for row in self.document()["repos"]}
        row = rows[str(checkout)]
        self.assertEqual((row["protection"]["required"], row["protection"]["strict"]), (["ci", "sd/local-gate"], True))
        self.assertEqual(row["runtimes"], {"python": {"value": "3.14", "source": ".python-version"},
                                           "node": {"value": ">=22", "source": "package.json engines.node"}})
        # A checkout not on disk has no runtime reading, and no protection observation has no required list.
        self.assertIsNone(rows["/repos/system"]["runtimes"])
        self.assertIsNone(rows["/repos/system"]["protection"]["required"])

    def test_a_runtime_pin_comes_from_the_first_file_that_names_one(self):
        cases = (
            ({".python-version": "3.12\n", ".tool-versions": "python 3.13.1\n"}, {"value": "3.12", "source": ".python-version"}, None),
            ({".tool-versions": "nodejs 22.1.0\npython 3.13.1\n", "pyproject.toml": '[project]\nrequires-python = ">=3.11"\n'},
             {"value": "3.13.1", "source": ".tool-versions"}, {"value": "22.1.0", "source": ".tool-versions"}),
            ({"pyproject.toml": '[project]\nrequires-python = ">=3.13"\n', ".nvmrc": "v20\n"},
             {"value": ">=3.13", "source": "pyproject.toml requires-python"}, {"value": "v20", "source": ".nvmrc"}),
            ({".node-version": "\n", "README.md": "python 3.9"}, None, None),
            ({"package.json": "{not json", "pyproject.toml": "[project"},
             {"error": "pyproject.toml is not valid TOML"}, {"error": "package.json is not valid JSON"}),
        )
        for number, (files, python, node) in enumerate(cases):
            with self.subTest(files=sorted(files)):
                root = Path(self.tmp.name) / f"pins-{number}"
                root.mkdir()
                for name, text in files.items():
                    (root / name).write_text(text)
                self.assertEqual(management_screen._runtimes(str(root)), {"python": python, "node": node})

    def test_a_pin_that_is_not_a_regular_file_is_refused_without_blocking(self):
        """sd:2911: a FIFO, or a link to one, is refused before any read; reading one would hang /api/management."""
        import os
        import threading

        root = Path(self.tmp.name) / "fifo-pins"
        root.mkdir()
        os.mkfifo(root / ".python-version")
        os.symlink(root / ".python-version", root / ".nvmrc")
        got = {}
        reader = threading.Thread(target=lambda: got.update(management_screen._runtimes(str(root))), daemon=True)
        reader.start()
        reader.join(5)
        self.assertFalse(reader.is_alive(), "reading a FIFO pin blocked the document")
        self.assertEqual(got, {"python": {"error": ".python-version is not a regular file"},
                               "node": {"error": ".nvmrc is not a regular file"}})

    def test_a_pin_file_past_the_limit_is_refused_after_a_bounded_read(self):
        root = Path(self.tmp.name) / "big-pins"
        root.mkdir()
        (root / "pyproject.toml").write_text("#" * (management_screen.PIN_BYTES + 10))
        reads = []
        real = management_screen.os.read

        def counted(fd, n):
            reads.append(n)
            return real(fd, n)

        with patch.object(management_screen.os, "read", counted):
            got = management_screen._runtimes(str(root))
        self.assertEqual(got["python"], {"error": "pyproject.toml is larger than a pin file"})
        self.assertLessEqual(sum(reads), 2 * (management_screen.PIN_BYTES + 1))

    def test_a_deeply_nested_manifest_is_that_repos_error_not_the_documents(self):
        """sd:2911: tomllib raises RecursionError on deep nesting, which no FAILURES entry caught; the whole document failed."""
        checkout = Path(self.ids["checkout"])
        depth = 15000
        self.assertLess(depth * 2 + 20, management_screen.PIN_BYTES)
        (checkout / "pyproject.toml").write_text("[project]\nx = " + "[" * depth + "]" * depth + "\n")
        doc = self.document()
        self.assertEqual(doc["sources"]["repos"], "")
        rows = {row["path"]: row for row in doc["repos"]}
        self.assertEqual(rows[str(checkout)]["runtimes"]["python"], {"error": "pyproject.toml nests too deeply to read"})

    def test_a_json_parser_that_recurses_too_deep_is_a_reason_too(self):
        """The C JSON scanner takes the nesting these sizes allow; a pure-Python one raises RecursionError (sd:2911)."""
        checkout = Path(self.ids["checkout"])
        (checkout / "package.json").write_text("{}")
        real = json.loads

        def deep(text, *args, **kwargs):
            # package.json is "{}"; the row's declarations column is the text naming "review".
            if text == "{}" or (isinstance(text, str) and '"review"' in text):
                raise RecursionError("maximum recursion depth exceeded")
            return real(text, *args, **kwargs)

        with patch.object(management_screen.json, "loads", deep):
            doc = self.document()
        self.assertEqual(doc["sources"]["repos"], "")
        row = {row["path"]: row for row in doc["repos"]}[str(checkout)]
        self.assertEqual(row["runtimes"]["node"], {"error": "package.json nests too deeply to read"})
        self.assertEqual(row["review"]["error"], "nests too deeply to read")

    def test_a_setting_of_the_wrong_type_is_that_repos_error_not_a_value(self):
        """sd:1629 round 2: one checkout's `{"toString": null}` reached the page, and String() of it stopped every view."""
        checkout = Path(self.ids["checkout"])
        repos.set_declaration(self.connection, str(checkout), "review",
                              {"severity_floor": 7, "copilot_review": {"automatic_deep": {"toString": None}}})
        body = {"gaps": [], "detail": {"required_contexts": ["ci", {"toString": None}], "strict": True}}
        self.connection.execute("INSERT INTO repo_protection (repo, observed_at, status, default_branch, body) "
                                "VALUES (?, '2026-10-04T01:00:00Z', 'protected', 'main', ?)", (str(checkout), json.dumps(body)))
        self.connection.commit()
        doc = self.document()
        self.assertEqual(doc["sources"]["repos"], "")
        row = {row["path"]: row for row in doc["repos"]}[str(checkout)]
        review, guarded = row["review"], row["protection"]
        self.assertEqual((review["severity_floor"], review["automatic_deep"]), (None, None))
        self.assertEqual(review["error"], "severity_floor is not a string; copilot_review.automatic_deep is not true or false")
        self.assertEqual((guarded["required"], guarded["required_error"]), (None, "a required check is not a name"))

    def test_the_document_reads_no_retired_merge_grant(self):
        """sd:TBD: the pack retired `sd.assistant_merge` (pack sd:3013); `repo.runner_merge` is the one merge setting, so
        the page runs no `sd config get` and carries no grant source."""
        with patch("subprocess.run", side_effect=AssertionError("a process ran")):
            doc = management_screen.document(self.connection, now=NOW, fleet=fleet, jobs=self.jobs, services=NoServices())
        self.assertNotIn("grant", doc)
        self.assertNotIn("grant", doc["sources"])

    def test_a_satellite_refuses_the_repo_verb_and_writes_nothing(self):
        """sd:1629: the repo settings change on the hub; a satellite's dashboard refuses before it reads or writes the row."""
        from sd_db.remote import HubOnly

        with patch("sd_db.database.served_by", return_value="hub.example.test:8765"):
            with self.assertRaises(HubOnly) as refused:
                management_screen.set_repo(self.connection, "runner-merge", self.ids["checkout"], "auto", "manual")
        self.assertIn("repo runner-merge runs on the sd hub only", str(refused.exception))
        rows = {row["path"]: row for row in self.document()["repos"]}
        self.assertEqual(rows[self.ids["checkout"]]["runner_merge"], "manual")

    def test_the_stale_check_and_the_write_hold_one_write_transaction(self):
        """Copilot on PR #50: two requests could read the same old value, and the late one overwrote.

        While the verb reads the row, a second connection must not be able to
        take the write lock; before the fix it could, and wrote in between.
        """
        import sqlite3

        from sd_db import connect, repos

        other = connect(self.path, busy_timeout=0)
        self.addCleanup(other.close)
        seen = []
        read = repos.row_for

        def racing(connection, path):
            row = read(connection, path)
            try:
                other.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as locked:
                seen.append(str(locked))
            else:
                other.execute("UPDATE repo SET runner_merge = 'auto' WHERE path = ?", (row["path"],))
                other.execute("COMMIT")
                seen.append("the other writer got in")
            return row

        with patch.object(repos, "row_for", racing):
            management_screen.set_repo(self.connection, "runner-merge", self.ids["checkout"], "auto", "manual")
        self.assertEqual(seen, ["database is locked"])


SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


def keys(profile, **more):
    """A stand-in for `shared_upload.credentials`: no `aws` runs, nothing reaches AWS."""
    return {"AccessKeyId": "AKIDEXAMPLE", "SecretAccessKey": SECRET, **more}


def no_keys(profile):
    raise RuntimeError(f"aws configure export-credentials --profile {profile} exited 255: Unable to locate credentials")


def upload_config(case, text="SHARED_UPLOAD_BUCKET=example-bucket\nSHARED_UPLOAD_REGION=us-east-1\n"
                             "SHARED_UPLOAD_PROFILE=example-uploader\n"):
    """`<config>/shared-upload/.env` in the case's scratch config folder (support.ScreenCase points there)."""
    folder = Path(case.tmp.name) / "config" / "shared-upload"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".env").write_text(text, encoding="utf-8")
    return folder / ".env"


class TheSharedUploadDocument(ScreenCase):
    """sd:5405: the shared bucket's console link, the current upload link (one setting row) and Regenerate."""

    MOMENT = datetime.datetime(2026, 9, 6, 11, 0, tzinfo=datetime.timezone.utc)

    def document(self):
        return management_screen.document(self.connection, now=NOW, fleet=fleet, jobs=JobsBackend(self.tmp.name, jobs=[]),
                                          services=NoServices())

    def regenerate(self, credentials=keys, moment=None):
        return upload_link.regenerate(self.connection, now=moment or self.MOMENT, credentials=credentials)

    def rows(self):
        return self.connection.execute("SELECT count(*) FROM state WHERE kind = 'setting' AND key = ?",
                                       (upload_link.KEY,)).fetchone()[0]

    def test_no_config_file_is_not_set_up_and_names_the_path(self):
        doc = self.document()
        self.assertEqual(doc["sources"]["upload"], "")
        up = doc["upload"]
        path = str(Path(self.tmp.name) / "config" / "shared-upload" / ".env")
        self.assertEqual((up["set_up"], up["config"], up["link"]), (False, path, None))
        self.assertIn(path, up["remedy"])
        self.assertIn("SHARED_UPLOAD_BUCKET and SHARED_UPLOAD_REGION and SHARED_UPLOAD_PROFILE are not set", up["remedy"])
        with self.assertRaisesRegex(ValueError, "not set up"):
            self.regenerate()
        self.assertEqual(self.rows(), 0)

    def test_a_change_me_value_is_unset_and_an_exported_value_wins(self):
        upload_config(self, "SHARED_UPLOAD_BUCKET=change-me\nSHARED_UPLOAD_REGION=us-east-1\nSHARED_UPLOAD_PROFILE=p\n")
        self.assertEqual(self.document()["upload"]["remedy"].split(" is not set")[0], "SHARED_UPLOAD_BUCKET")
        with patch.dict("os.environ", {"SHARED_UPLOAD_BUCKET": "exported-bucket"}):
            up = self.document()["upload"]
        self.assertEqual((up["set_up"], up["bucket"]), (True, "exported-bucket"))

    def test_a_config_file_that_does_not_read_is_the_source_s_reason_not_unset(self):
        path = upload_config(self)
        path.write_bytes(b"SHARED_UPLOAD_BUCKET=\xff\n")
        doc = self.document()
        self.assertIsNone(doc["upload"])
        self.assertIn("utf-8", doc["sources"]["upload"])

    def test_set_up_with_no_link_shows_the_console_and_the_public_pattern(self):
        upload_config(self)
        up = self.document()["upload"]
        self.assertEqual((up["set_up"], up["bucket"], up["region"], up["prefix"]), (True, "example-bucket", "us-east-1", "inbox/"))
        self.assertEqual(up["console"], "https://s3.console.aws.amazon.com/s3/buckets/example-bucket?region=us-east-1&tab=objects")
        self.assertEqual(up["public"], "https://example-bucket.s3.us-east-1.amazonaws.com/inbox/<file name>")
        self.assertEqual((up["link_state"], up["link"], up["link_reason"]), ("none", None, ""))

    def test_regenerate_signs_with_the_tool_stores_one_row_and_answers_no_link(self):
        upload_config(self)
        calls = []
        status, answer = self.regenerate(lambda profile: calls.append(profile) or keys(profile))
        self.assertEqual(calls, ["example-uploader"])
        self.assertEqual((status, answer), (200, {"prefix": "inbox/", "expiry": "2026-09-13T11:00:00Z", "temporary": None}))
        self.assertEqual(self.rows(), 1)
        up = self.document()["upload"]
        self.assertEqual(up["link_state"], "current")
        link = up["link"]
        tool = upload_link._tool()
        url, fields, expiry = tool.presigned_post("example-bucket", "us-east-1", "inbox/", keys("p"), 7 * 24 * 3600,
                                                  500 * 1024 * 1024, self.MOMENT)
        self.assertEqual((link["url"], link["fields"], link["expiry"]), (url, fields, expiry))
        self.assertEqual(link["curl"], tool.curl_line(url, fields))
        self.assertEqual(link["public"], "https://example-bucket.s3.us-east-1.amazonaws.com/inbox/<file name>")
        policy = json.loads(base64.b64decode(fields["policy"]))
        self.assertIn(["starts-with", "$key", "inbox/"], policy["conditions"])
        self.assertIn({"bucket": "example-bucket"}, policy["conditions"])
        # The answer and the toast built from it never hold what grants the upload.
        self.assertNotIn(fields["x-amz-signature"], json.dumps(answer))
        self.assertNotIn(fields["policy"], json.dumps(answer))

    def test_a_second_regenerate_replaces_the_shown_link(self):
        upload_config(self)
        self.regenerate()
        first = self.document()["upload"]["link"]
        self.regenerate(moment=self.MOMENT + datetime.timedelta(minutes=5))
        second = self.document()["upload"]["link"]
        self.assertEqual(self.rows(), 2)
        self.assertNotEqual(first["fields"]["x-amz-signature"], second["fields"]["x-amz-signature"])
        self.assertEqual(second["expiry"], "2026-09-13T11:05:00Z")

    def test_keys_the_profile_cannot_give_are_a_502_and_write_nothing(self):
        upload_config(self)
        status, answer = self.regenerate(no_keys)
        self.assertEqual(status, 502)
        self.assertIn("No link was signed: aws configure export-credentials --profile example-uploader exited 255",
                      answer["error"])
        self.assertEqual(self.rows(), 0)
        self.assertEqual(self.document()["upload"]["link_state"], "none")

    def test_temporary_keys_say_when_the_link_stops(self):
        upload_config(self)
        status, answer = self.regenerate(lambda p: keys(p, SessionToken="token", Expiration="2026-09-06T18:00:00+00:00"))
        self.assertEqual(answer["temporary"], "2026-09-06T18:00:00+00:00")
        self.assertEqual(self.document()["upload"]["link"]["temporary"], "2026-09-06T18:00:00+00:00")

    def test_a_bad_size_cap_is_refused_before_any_keys_are_read(self):
        upload_config(self, "SHARED_UPLOAD_BUCKET=b\nSHARED_UPLOAD_REGION=us-east-1\nSHARED_UPLOAD_PROFILE=p\n"
                            "SHARED_UPLOAD_MAX_MB=lots\n")
        with self.assertRaisesRegex(ValueError, "SHARED_UPLOAD_MAX_MB"):
            self.regenerate(no_keys)
        self.assertEqual(self.rows(), 0)

    def test_a_link_signed_for_another_bucket_is_not_the_current_link(self):
        upload_config(self)
        self.regenerate()
        upload_config(self, "SHARED_UPLOAD_BUCKET=other-bucket\nSHARED_UPLOAD_REGION=us-east-1\n"
                            "SHARED_UPLOAD_PROFILE=example-uploader\n")
        up = self.document()["upload"]
        self.assertEqual((up["link_state"], up["link"]), ("other", None))
        self.assertIn("signed for example-bucket in us-east-1 under inbox/", up["link_reason"])

    def test_an_expired_link_says_so(self):
        upload_config(self)
        self.regenerate(moment=self.MOMENT - datetime.timedelta(days=8))
        up = self.document()["upload"]
        self.assertEqual((up["link_state"], up["link"]["expiry"]), ("expired", "2026-09-05T11:00:00Z"))

    def test_a_row_that_does_not_read_is_unknown_not_none_signed(self):
        upload_config(self)
        for body in ("not json", json.dumps({"bucket": "example-bucket"}), json.dumps([1])):
            writes.set_setting(self.connection, upload_link.KEY, body)
            up = self.document()["upload"]
            with self.subTest(body=body):
                self.assertEqual((up["link_state"], up["link"]), ("unread", None))
                self.assertIn("does not read; Regenerate replaces it", up["link_reason"])


class ThePage(BrowserSession):
    fleet_backend = staticmethod(fleet)

    def setUp(self):
        patcher = patch("sd_db.services.ServiceBackend", NoServices)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = JobsBackend(self.tmp.name, jobs=[("quiet", "idle", 0, None)])
        super().setUp()
        self.ids = seed(self)

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/management")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Management · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "commands.js", "management.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/management")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/management?view=lane", headers=cookie)[0], 400)
        status, _, body = self.request("/api/management", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["sources"]["services"], "")
        self.assertIn(self.ids["checkout"], [row["path"] for row in doc["repos"]])

    def test_the_repo_write_flips_once_and_refuses_the_same_before_again(self):
        body = {"path": self.ids["checkout"], "value": "auto", "before": "manual"}
        status, _, got = self.post("/api/repos/runner-merge", body)
        self.assertEqual((status, got["value"], got["before"]), (200, "auto", "manual"))
        self.assertEqual(self.post("/api/repos/runner-merge", body)[0], 409)
        self.assertEqual(self.post("/api/repos/runner-merge", {**body, "value": "sometimes"})[0], 400)
        self.assertEqual(self.post("/api/repos/managed", {"path": self.ids["checkout"], "value": "yes", "before": "yes"})[0], 400)
        self.assertEqual(self.post("/api/repos/mode", {"path": self.ids["checkout"], "value": "x", "before": "y"})[0], 404)

    def test_the_dependabot_route_flips_once_and_refuses_a_stale_before_and_a_bad_word(self):
        """sd:3984: `/api/repos/dependabot` takes yes or no and the value the page showed."""
        body = {"path": self.ids["checkout"], "value": "yes", "before": "no"}
        status, _, got = self.post("/api/repos/dependabot", body)
        self.assertEqual((status, got["field"], got["value"], got["before"]), (200, "dependabot", "yes", "no"))
        self.assertEqual(self.post("/api/repos/dependabot", body)[0], 409)
        self.assertEqual(self.post("/api/repos/dependabot", {**body, "value": "always", "before": "yes"})[0], 400)

    def test_the_github_issues_route_flips_once_and_refuses_a_stale_before_and_a_bad_word(self):
        """sd:4840: `/api/repos/github-issues` takes off, read or ask and the value the page showed."""
        body = {"path": self.ids["checkout"], "value": "read", "before": "off"}
        status, _, got = self.post("/api/repos/github-issues", body)
        self.assertEqual((status, got["field"], got["value"], got["before"]), (200, "github_issues", "read", "off"))
        self.assertEqual(self.post("/api/repos/github-issues", body)[0], 409)
        self.assertEqual(self.post("/api/repos/github-issues", {**body, "value": "on", "before": "read"})[0], 400)

    def test_the_issues_scope_route_flips_once_and_refuses_a_stale_before_and_a_bad_word(self):
        """pack sd:7672: `/api/repos/issues-scope` takes all or mine and the value the page showed."""
        body = {"path": self.ids["checkout"], "value": "mine", "before": "all"}
        status, _, got = self.post("/api/repos/issues-scope", body)
        self.assertEqual((status, got["field"], got["value"], got["before"]), (200, "issues_scope", "mine", "all"))
        self.assertEqual(self.post("/api/repos/issues-scope", body)[0], 409)
        self.assertEqual(self.post("/api/repos/issues-scope", {**body, "value": "some", "before": "mine"})[0], 400)

    def test_the_lane_host_route_moves_once_and_refuses_a_stale_before_and_a_bad_name(self):
        """sd:3075, acceptance 2: `Build_2` is a 400 naming the rule; a repeated move is a stale 409."""
        body = {"path": self.ids["checkout"], "value": "build-2", "before": "hub"}
        status, _, got = self.post("/api/repos/lane-host", body)
        self.assertEqual((status, got["value"], got["before"]), (200, "build-2", "hub"))
        self.assertEqual(self.post("/api/repos/lane-host", body)[0], 409)
        status, _, got = self.post("/api/repos/lane-host", {**body, "value": "Build_2", "before": "build-2"})
        self.assertEqual(status, 400)
        self.assertIn("not a lane host", got["error"])
        status, _, got = self.post("/api/repos/lane-host", {**body, "path": "/nowhere", "value": "hub", "before": "build-2"})
        self.assertEqual(status, 400)
        self.assertIn("not a registered repository", got["error"])
        self.assertEqual(self.post("/api/repos/lane-host", {**body, "value": "", "before": "build-2"})[0], 400)

    def test_the_channels_route_writes_once_and_refuses_an_old_before_a_bad_folder_and_a_satellite(self):
        """sd:3465 acceptance: a form changes a repo's row, and a second form posted with the old value is refused."""
        body = {"path": self.ids["checkout"], "value": {"notion": {"folder": "Example"}}, "before": "none"}
        status, _, got = self.post("/api/repos/channels", body)
        self.assertEqual((status, got["value"], got["before"]), (200, '{"notion":{"folder":"Example"}}', "none"))
        self.assertIn("sd-research-kit render", got["remedy"])
        status, _, got = self.post("/api/repos/channels", {**body, "value": {"drive": {}}})
        self.assertEqual((status, got.get("reload")), (409, True))
        status, _, got = self.post("/api/repos/channels", {**body, "value": {"drive": {"folder": "../up"}}, "before": '{"notion":{"folder":"Example"}}'})
        self.assertEqual(status, 400)
        self.assertIn("'..' segment", got["error"])
        self.assertEqual(self.post("/api/repos/channels", {**body, "value": "drive="})[0], 400)
        self.assertEqual(self.post("/api/repos/channels", {**body, "before": None})[0], 400)
        with patch("sd_db.database.served_by", return_value="hub.example.test:8765"):
            status, _, got = self.post("/api/repos/channels", {**body, "value": None, "before": '{"notion":{"folder":"Example"}}'})
        self.assertEqual(status, 400)
        self.assertIn("runs on the sd hub only", got["error"])
        status, _, got = self.post("/api/repos/channels", {**body, "value": None, "before": '{"notion":{"folder":"Example"}}'})
        self.assertEqual((status, got["value"]), (200, "none"))

    def test_end_session_is_a_route_that_refuses_a_post_without_its_evidence(self):
        """sd:5126: the page-registered write answers; a post that does not name a session id and pid ends nothing."""
        for body in ({}, {"id": "s-wait"}, {"id": "s-wait", "pid": "100"}):
            status, _, got = self.post("/api/claude-sessions/end", body)
            self.assertEqual(status, 400)
            self.assertIn("Name the session's id and the pid the page showed.", got["error"])

    def test_refresh_repos_answers_and_takes_no_values(self):
        """sd:5614: the post writes nothing; as an answered post it invalidates the fleet snapshot (sd:4955)."""
        status, _, got = self.post("/api/repos/refresh", {})
        self.assertEqual((status, got), (200, {"refreshed": True}))
        status, _, got = self.post("/api/repos/refresh", {"path": "/x"})
        self.assertEqual(status, 400)
        self.assertIn("takes no values", got["error"])

    def test_refresh_satellites_writes_a_new_request_and_takes_no_values(self):
        """sd:5108: each post is a new request id; a post that carries values is refused and writes nothing."""
        status, _, first = self.post("/api/satellites/refresh", {})
        self.assertEqual(status, 200)
        self.assertRegex(first["id"], r"^[0-9a-f]{16}$")
        status, _, second = self.post("/api/satellites/refresh", {})
        self.assertNotEqual(second["id"], first["id"])
        status, _, got = self.post("/api/satellites/refresh", {"host": "sat-a"})
        self.assertEqual(status, 400)
        self.assertIn("takes no values", got["error"])
        doc = json.loads(self.request("/api/management", headers={"Cookie": self.cookie})[2])
        self.assertEqual(doc["satellites"]["request"], second)

    def test_regenerate_signs_through_the_route_and_takes_no_values(self):
        """sd:5405: the post carries nothing; the answer holds the expiry, the document the link."""
        status, _, got = self.post("/api/shared-upload/regenerate", {})
        self.assertEqual(status, 400)
        self.assertIn("Shared upload is not set up", got["error"])
        upload_config(self)
        status, _, got = self.post("/api/shared-upload/regenerate", {"prefix": "other/"})
        self.assertEqual(status, 400)
        self.assertIn("takes no values", got["error"])
        with patch.object(upload_link._tool(), "credentials", keys):
            status, _, got = self.post("/api/shared-upload/regenerate", {})
        self.assertEqual((status, sorted(got)), (200, ["expiry", "prefix", "temporary"]))
        doc = json.loads(self.request("/api/management", headers={"Cookie": self.cookie})[2])
        self.assertEqual((doc["upload"]["link_state"], doc["upload"]["link"]["expiry"]), ("current", got["expiry"]))
        with patch.object(upload_link._tool(), "credentials", no_keys):
            status, _, got = self.post("/api/shared-upload/regenerate", {})
        self.assertEqual(status, 502)
        self.assertIn("No link was signed", got["error"])

    def test_the_repo_write_is_refused_on_a_satellite(self):
        """sd:1629: the POST passes the session and CSRF checks, then the verb refuses: a 400 naming the hub, no change."""
        body = {"path": self.ids["checkout"], "value": "yes", "before": "no"}
        with patch("sd_db.database.served_by", return_value="hub.example.test:8765"):
            status, _, got = self.post("/api/repos/managed", body)
        self.assertEqual(status, 400)
        self.assertIn("runs on the sd hub only", got["error"])
        status, _, got = self.post("/api/repos/managed", body)
        self.assertEqual((status, got["value"], got["before"]), (200, "yes", "no"))


# The Management stand-in adds what the page reads beyond Tasks': a <main> to listen on, CSS.escape, and the shell's
# row and context calls.
EXTRA = r"""
var CSS = { escape: s => s };
var MAIN = El('main');
var baseQuery = document.querySelector;
document.querySelector = sel => sel === 'main' ? MAIN : baseQuery(sel);
// A sub-view lamp's value is a child the page fills: each element answers querySelector with one child per selector.
var baseById = document.getElementById;
document.getElementById = id => { var e = baseById.call(document, id);
  if (!e.kids) { e.kids = {}; e.querySelector = sel => { if (!e.kids[sel]) { e.kids[sel] = El(id + ' ' + sel);
    e.kids[sel].after = f => { e.html = (e.html || '') + (f.html || ''); }; } return e.kids[sel]; }; } return e; };
"""
SHELL_EXTRA = r"""
var ROW = null;
window.shell.row = (...a) => a.length ? (ROW = a[0]) : ROW;
window.shell.setContext = () => {};
OUT.urls = []; OUT.qs = [];
window.shell.url = p => { OUT.urls.push({ view: VIEW_NOW, p: p.get('page'), q: p.get('q') }); OUT.qs.push(p.toString()); };
// The shell's view switch (shell.js, view:start) as management.js meets it: the hash's view, else the first; show() fires
// 'shell:view' on a change, as a tab, a v key or a lamp does.
var VIEW_NOW = null; OUT.shown = [];
window.shell.view = { current: () => VIEW_NOW || (/^#view=([a-z]+)$/.exec(location.hash || '') || [])[1] || window.PAGE_VIEWS[0].id,
  show: id => { OUT.shown.push(id); const was = window.shell.view.current(); VIEW_NOW = id;
    if (was !== id) document.dispatchEvent(new CustomEvent('shell:view', { detail: { id } })); return true; }, count() {} };
"""


class PageScript(ScreenCase):
    """management.js against the document `management_screen` builds for the seeded database."""

    def setUp(self):
        super().setUp()
        self.ids = seed(self)
        jobs = JobsBackend(self.tmp.name, jobs=[("nightly-sync", "failed", 7, None), ("quiet", "idle", 0, None)])
        stamp(jobs, "nightly-sync", "started=2026-09-06T02:15:00Z\nended=2026-09-06T02:16:30Z\nexit=7\n")
        self.doc = management_screen.document(self.connection, now=NOW, fleet=fleet, jobs=jobs, services=NoServices())

    def run_page(self, body, answer="null", doc=None, search="", extra="", hash=""):
        script = (STAND_IN + EXTRA + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + SHELL_EXTRA + READ_SHELL + extra
                  + f"\nconst DOC0 = {json.dumps(doc or self.doc)};\n"
                  + "const WRITE = " + answer + ";\n"
                  + f"location.search = {json.dumps(search)}; location.hash = {json.dumps(hash)};\n"
                  + """ANSWER = (path, body) => {
  if (path === '/api/management') return [200, DOC0];
  return WRITE ? WRITE(path, body) : [200, {}];
};\n""" + COMMANDS_JS + PAGE_JS + "\nvar R = {};\n(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out


class TheScript(PageScript):
    def test_every_command_is_registered_with_its_risk_key_and_run_path(self):
        out = self.run_page("""R.reg = REG.map(c => [c.id, c.on, c.risk, c.key || null,
  typeof c.executes === 'boolean' ? c.executes : null, typeof c.undo === 'function']);""")
        self.assertEqual(out["R"]["reg"], [
            ["repo.runner-merge", "repo", "undo", "m", True, True],
            ["repo.managed", "repo", "undo", "g", True, True],
            ["repo.standard", "repo", "confirm", None, True, False],
            ["repo.lane-host", "repo", "safe", "h", False, False],
            ["repo.pull", "repo", "confirm", "l", True, False],
            ["repo.adversarial-review", "repo", "safe", None, True, False],
            ["repo.adversarial-pr", "repo", "safe", None, True, False],
            ["repo.deep-review", "repo", "safe", None, True, False],
            ["repo.land", "repo", "confirm", "n", True, False],
            ["sddb.run", "sd-db change", "undo", "u", True, True],
            ["declaration.write", "declaration change", "undo", "p", True, True],
            ["sddb.withdraw", "sd-db change", "safe", None, False, False],
            ["declaration.withdraw", "declaration change", "safe", None, False, False],
            ["wt.prune", "worktrees", "confirm", "p", None, False],
            ["svc.restart", "service", "confirm", "t", None, False],
            ["svc.stop", "service", "confirm", "s", None, False],
            ["svc.start", "service", "safe", "a", None, False],
            ["repos.refresh", "git state", "safe", None, True, False],
            ["sats.refresh", "satellite refresh", "confirm", None, True, False],
            ["upload.regenerate", "upload link", "confirm", None, True, False],
            ["jobs.retry", "job", "safe", "t", None, False],
            ["jobs.cancel", "job", "confirm", "x", None, False],
            ["jobs.log", "job", "safe", "l", False, False],
            ["jobs.print", "job", "safe", "p", True, False],
            ["cs.end", "claude session", "confirm", "e", True, False],
        ])

    def test_land_asks_with_the_preview_previews_again_for_new_items_and_posts_its_revision(self):
        """sd:5104: the confirm shows the preview; a changed Items field reads a new preview before anything is posted."""
        answer = """(path, body) => {
  if (path.startsWith('/api/repos/land/preview')) { const items = new URLSearchParams(path.split('?')[1]).get('items');
    return [200, { spec: items === null ? '7' : items, ready: true, why: '', revision: 'r-' + (items === null ? '7' : items),
      mode: 'branch', branch: 'main', default: 'main', land_branch: 'land/sd-7', worktree: '/w', commits: [],
      groups: [{ item: 7, title: 'T', paths: ['a'] }], cli: 'sd-ship -C /w lane enqueue' }]; }
  if (path === '/api/repos/land') return [200, { ok: true, branch: 'land/sd-7', commits: [{}], lane: { status: 'pending' } }];
  return [200, {}]; }"""
        extra = """OUT.asks = []; var ASKED = ['7; 8: lib/', '7; 8: lib/'];
window.shell.ask = a => { OUT.asks.push([a.body, a.fields[0].value]); return Promise.resolve({ yes: true, values: { items: ASKED.shift(), claim: 'deliver' } }); };"""
        out = self.run_page("""const o = [...OBJ.values()].find(x => x.type === 'repo');
R.got = await cmd('repo.land').run(o); R.previews = cmd('repo.land').previews;""", answer=answer, extra=extra)
        self.assertEqual([a[1] for a in out["asks"]], ["7", "7; 8: lib/"])
        self.assertIn("land/sd-7", out["asks"][0][0])
        self.assertEqual([g for g in out["gets"] if "land" in g][1].split("items=")[1], "7%3B%208%3A%20lib%2F")
        posted = [body for path, body, _ in out["posts"] if path == "/api/repos/land"]
        self.assertEqual(posted, [{"path": posted[0]["path"], "items": "7; 8: lib/", "revision": "r-7; 8: lib/", "claim": "deliver"}])
        self.assertIn("lane pending", out["R"]["got"]["text"])
        self.assertTrue(out["R"]["previews"])

    # Standard setup (sd:5610): a preview answer with one GitHub change and one checkout pending, and the answer the
    # apply gives with each step's result. `used` is the preview's lane history, which picks the sheet's first option.
    def standard_answer(self, used=False, apply="[200, { ...PLAN, applied: true, steps: PLAN.steps.map(s => s.state === 'change' ? { ...s, result: 'changed' } : s) }]"):
        return """(() => { const PLAN = { digest: '%s', used_before: %s, managed: false, steps: [
  { key: 'D1', group: 'row', subject: 'managed', state: 'change', detail: '', before: 'no', after: 'yes' },
  { key: 'G4', group: 'github', subject: 'Delete head branches', state: 'change', detail: '', before: false, after: true },
  { key: 'G7', group: 'github', subject: 'Dependabot alerts', state: 'ok', detail: 'off: a kept choice (sd:5611); never turned on here', before: 'off', after: 'off' },
  { key: 'F1', group: 'checkout', subject: 'tracked fleet files', state: 'pending', detail: '.gitignore: a pull request carries them', before: null, after: '.gitignore' }] };
  return (path, body) => {
    if (path.startsWith('/api/repos/standard/preview')) return [200, PLAN];
    if (path === '/api/repos/standard') return %s;
    return [200, {}]; }; })()""" % ("d" * 64, "true" if used else "false", apply)

    ASKS = """OUT.asks = []; var PICK = [];
window.shell.ask = a => { OUT.asks.push({ title: a.title, body: String(a.body), ok: a.ok, keep: a.keep,
  options: (a.fields || []).map(f => (f.options || []).map(x => x.value)) });
  const next = PICK.shift() || { yes: false, values: {} }; return Promise.resolve(next); };"""

    def test_turning_managed_on_previews_the_standard_by_group_and_applies_the_digest_it_showed(self):
        """sd:5610: first manage leads with the standard; the post carries the preview's digest; the result lists each step."""
        path = self.ids["checkout"]
        out = self.run_page(f"""R.page = ELS['view-repos'].html; PICK = [{{ yes: true, values: {{ run: 'apply' }} }}];
R.got = await cmd('repo.standard').run(C.get('repo:{path}'));""", search=f"?repo={path}",
                            answer=self.standard_answer(), extra=self.ASKS)
        managed = out["R"]["page"].split('data-key="managed"')[1].split('<div class="row"')[0]
        self.assertIn('data-cmd="repo.standard"', managed)
        sheet, result = out["asks"]
        self.assertEqual(sheet["options"], [["apply", "only"]])
        self.assertEqual(sheet["title"], f"Manage {path}?")
        for line in ("Repo settings", "D1 managed: no → yes", "GitHub", "G4 Delete head branches: false → true",
                     "G7 Dependabot alerts: ok", "checkout", "F1 tracked fleet files: pending"):
            self.assertIn(line, sheet["body"])
        self.assertEqual([(p, b) for p, b, _ in out["posts"]], [("/api/repos/standard", {"path": path, "digest": "d" * 64})])
        self.assertIn("G4 Delete head branches: changed", result["body"])
        self.assertEqual(result["ok"], "Check again")
        self.assertEqual(out["R"]["got"]["text"], f"Standard applied · {path}: 2 changed")

    def test_a_repo_with_lane_history_leads_with_manage_only_which_writes_the_one_column(self):
        path = self.ids["checkout"]
        out = self.run_page(f"""PICK = [{{ yes: true, values: {{ run: 'only' }} }}];
R.got = await cmd('repo.managed').run(C.get('repo:{path}'));""", answer=self.standard_answer(used=True), extra=self.ASKS)
        self.assertEqual(out["asks"][0]["options"], [["only", "apply"]])
        self.assertEqual([(p, b) for p, b, _ in out["posts"]], [("/api/repos/managed", {"path": path, "value": "yes", "before": "no"})])
        self.assertEqual(out["R"]["got"]["text"], f"managed yes · {path}")

    def test_a_managed_repo_checks_the_standard_and_a_refused_apply_says_nothing_ran(self):
        path = self.ids["checkout"]
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["repos"] if r["path"] == path)["managed"] = "yes"
        out = self.run_page(f"""R.page = ELS['view-repos'].html; PICK = [{{ yes: true, values: {{}} }}];
R.got = await cmd('repo.standard').run(C.get('repo:{path}'));""", doc=doc, search=f"?repo={path}", extra=self.ASKS,
                            answer=self.standard_answer(apply="[409, { error: 'sd fleet standard: a ship is running; try again' }]"))
        row = out["R"]["page"].split('data-key="standard"')[1].split('<div class="row"')[0]
        self.assertIn('data-cmd="repo.standard"', row)
        self.assertEqual(len(out["asks"]), 1)
        self.assertEqual((out["asks"][0]["ok"], out["asks"][0]["options"]), ("Apply standard", []))
        self.assertEqual(out["R"]["got"]["text"], f"Nothing ran · {path}: sd fleet standard: a ship is running; try again")

    def test_a_plan_with_nothing_to_change_asks_nothing(self):
        path = self.ids["checkout"]
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["repos"] if r["path"] == path)["managed"] = "yes"
        answer = """(path, body) => path.startsWith('/api/repos/standard/preview')
  ? [200, { digest: 'e', used_before: true, steps: [{ key: 'G4', group: 'github', subject: 'x', state: 'ok' }, { key: 'G1', group: 'github', subject: 'y', state: 'skip' }] }] : [200, {}]"""
        out = self.run_page(f"R.got = await cmd('repo.standard').run(C.get('repo:{path}'));", doc=doc, answer=answer, extra=self.ASKS)
        self.assertEqual(out["asks"], [])
        self.assertEqual(out["R"]["got"]["text"], f"Standard holds · {path}: 1 ok, 1 skip")

    def test_the_page_reads_the_document_and_draws_repos_with_git_state(self):
        out = self.run_page("R.repos = ELS['view-repos'].html; R.sched = ELS['view-schedules'].html;")
        self.assertEqual(out["gets"], ["/api/management"])
        self.assertEqual(out["states"][0]["kind"], "loading")
        self.assertIsNone(out["states"][-1])
        repos = out["R"]["repos"]
        self.assertIn("2 registered", repos)
        self.assertIn("+0/−2", repos)
        self.assertIn("busy", repos)  # a checkout sd-db does not list joins the list
        self.assertIn("not registered", repos)
        self.assertIn("nightly-sync", out["R"]["sched"])
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 1, "what": "scheduled jobs failed"})

    def test_the_repos_table_has_a_refresh_git_state_command_and_no_hint_line(self):
        """sd:5614: Refresh posts /api/repos/refresh, which makes the next read wait for a fresh git reading. sd:6697: it is
        Refresh git state, a registered command whose tip says it writes nothing."""
        answer = """(p, b) => p === '/api/repos/refresh' ? [200, { refreshed: true }] : [404, { error: 'no' }]"""
        out = self.run_page("""R.html = ELS['view-repos'].html; const c = cmd('repos.refresh');
R.label = c.label; R.does = c.does; R.got = await c.run(C.get('repos:git')); await flush();""", answer=answer)
        self.assertIn('data-cmd="repos.refresh" data-obj="repos:git"', out["R"]["html"])
        self.assertIn("<span>Refresh git state</span>", out["R"]["html"])
        # pack sd:7084: the action sits in the section head's tools, not inside a tally line.
        self.assertRegex(out["R"]["html"], r'<div class="tools"><button class="btn quiet sm" type="button" data-cmd="repos.refresh"')
        self.assertEqual(out["R"]["label"], "Refresh git state")
        self.assertIn("writes nothing", out["R"]["does"])
        self.assertEqual(out["R"]["got"], "Git state refreshed")
        self.assertNotIn("nothing here fetches", out["R"]["html"])
        self.assertNotIn("opens a registered repo's settings", out["R"]["html"])
        self.assertEqual([p[:2] for p in out["posts"]], [["/api/repos/refresh", {}]])

    def test_only_the_sorted_header_carries_aria_sort_and_a_click_moves_it(self):
        """sd:2527: each column with an order sorts through its button; aria-sort is on the sorted th alone."""
        click = "ELS['view-repos'].listeners.click[0]({ target: { closest: s => s === 'button[data-sort]' ? { dataset: { sort: 'managed' } } : null } });"
        sorted_ = r'aria-sort="(\w+)"[^>]*><button class="sorter"[^>]*data-sort="(\w+)"'
        out = self.run_page(f"R.a = ELS['view-repos'].html; {click} R.b = ELS['view-repos'].html; {click} R.c = ELS['view-repos'].html;")
        for key, want in (("a", [("ascending", "name")]), ("b", [("ascending", "managed")]), ("c", [("descending", "managed")])):
            self.assertEqual(re.findall(sorted_, out["R"][key]), want, key)
            self.assertEqual(out["R"][key].count("aria-sort"), 1, key)

    def jobs(self, n):
        """The document with n more launchd jobs, idle, named job-00 onward."""
        quiet = next(j for j in self.doc["jobs"] if j["name"] == "quiet")
        return dict(self.doc, jobs=self.doc["jobs"] + [dict(quiet, name=f"job-{i:02d}") for i in range(n)])

    def test_the_pager_is_the_shells_and_keeps_page_and_size_in_the_url(self):
        """sd:2682: shell.list's pager, with sizes 25 to 200; ?page= and ?size= in the URL, and an older ?p=/?n= link still opens."""
        click = "ELS['view-schedules'].listeners.click[0]({ target: { closest: s => s === %s ? { dataset: %s } : null } });"
        out = self.run_page("R.a = ELS['view-schedules'].html;" + click % ("'.list-pager [data-page]'", "{ page: '2' }")
                            + " R.b = ELS['view-schedules'].html;" + click % ("'.list-pager [data-size]'", "{ size: '50' }")
                            + " R.c = ELS['view-schedules'].html;", doc=self.jobs(30), search="?view=schedules")
        self.assertIn('<span class="range">1–25 of 32</span>', out["R"]["a"])
        self.assertEqual(re.findall(r'data-size="(\d+)"', out["R"]["a"]), ["25", "50", "100", "200"])
        self.assertIn('<span class="range">26–32 of 32</span>', out["R"]["b"])
        self.assertIn("page=2", out["qs"])
        self.assertIn('<span class="range">1–32 of 32</span>', out["R"]["c"])
        self.assertEqual(out["qs"][-1], "size=50")
        out = self.run_page("R.a = ELS['view-schedules'].html;", doc=self.jobs(30), search="?view=schedules&p=2&n=50")
        self.assertIn('<span class="range">1–32 of 32</span>', out["R"]["a"])
        self.assertEqual(out["qs"][-1], "size=50")

    def test_the_filters_are_activitys_bar_whose_token_removes_the_text_and_clear_all_every_filter(self):
        """sd:2682, sd:5504: Repos' managed and merge filters are segments, the text a token, then Clear all (shell.list.fbar);
        the count sits on the title line while a filter hides rows, never in the bar."""
        click = "document.querySelector('main').listeners.click.forEach(f => f({ target: { closest: s => s === %s ? { dataset: %s } : null } }));"
        out = self.run_page("R.a = ELS['view-repos'].html;" + click % ("'[data-f=\"q\"]'", "{ f: 'q', v: '' }")
                            + " R.b = ELS['view-repos'].html;" + click % ("'[data-fclear]'", "{}")
                            + " R.c = ELS['view-repos'].html; R.box = (OUT.shift ?? null);", search="?view=repos&managed=yes&q=busy")
        def bar(key):
            return re.search(r'<div class="fbar".*?<div class="fsum">.*?</div></div>', out["R"][key], re.S).group(0)
        self.assertIn('data-f="managed" data-v="yes" aria-pressed="true"', bar("a"))
        self.assertEqual(re.findall(r'class="tok" data-f="(\w+)"', bar("a")), ["q"])
        self.assertTrue(bar("a").endswith('<div class="fsum"><button class="linkbtn" type="button" data-fclear>Clear all</button></div></div>'))
        self.assertNotRegex(re.sub(r"<[^>]+>", " ", bar("a")), r"\b\d+ of \d+\b", "the bar holds no count")
        self.assertRegex(re.search(r'<p class="tally">.*?</p>', out["R"]["a"]).group(0), r"<span>\d+ of \d+ repos</span>")
        self.assertNotIn('class="tok"', bar("b"))
        self.assertEqual(out["qs"][-2], "managed=yes")
        self.assertTrue(bar("c").endswith('<div class="fsum"></div></div>'))
        self.assertNotRegex(re.search(r'<p class="tally">.*?</p>', out["R"]["c"]).group(0), r"\d+ of \d+ repos")
        self.assertEqual((out["qs"][-1], out["R"]["box"]), ("", ""))

    def settings_doc(self):
        """The document with the seeded checkout managed, protected with two required checks, and pinned to Python 3.14."""
        doc = json.loads(json.dumps(self.doc))
        row = next(r for r in doc["repos"] if r["path"] == self.ids["checkout"])
        row["managed"] = "yes"
        row["protection"] = dict(row["protection"], status="protected", required=["ci", "sd/local-gate"], strict=True)
        row["runtimes"] = {"python": {"value": "3.14", "source": ".python-version"}, "node": None}
        return doc

    def test_the_settings_columns_condense_each_managed_repo_to_one_row(self):
        """sd:1629: ?cols=settings swaps the git columns for the settings; floor and Copilot are read, never edited, here."""
        out = self.run_page("R.repos = ELS['view-repos'].html;", doc=self.settings_doc(), search="?view=repos&cols=settings&managed=yes")
        repos = out["R"]["repos"]
        self.assertEqual(re.findall(r'<tr data-id="repo:([^"]+)"', repos), [self.ids["checkout"]])
        cells = dict(re.findall(r'data-k="(\w+)">(.*?)</td>', repos))
        self.assertEqual(set(cells), {"managed", "merge", "lane", "floor", "copilot", "checks", "python", "node"})
        self.assertEqual((cells["managed"], cells["merge"], cells["floor"], cells["copilot"]), ("yes", "manual", "high", "false"))
        self.assertEqual(re.findall(r"<code>([^<]+)</code>", cells["checks"]), ["ci", "sd/local-gate"])
        self.assertIn("3.14", cells["python"])
        self.assertIn("not pinned", cells["node"])
        self.assertNotIn("merge grant", repos)
        self.assertIn("change through the row's review declaration", repos)
        self.assertNotIn("data-set=", repos)
        self.assertNotIn("<select", repos)

    def test_the_column_set_is_a_control_kept_in_the_url(self):
        click = ("ELS['view-repos'].listeners.click[0]({ target: { closest: s => s === '[data-f=\"cols\"]' ? { dataset: { f: 'cols', v: '%s' } } : null } });"
                 " R.%s = ELS['view-repos'].html;")
        out = self.run_page(click % ("settings", "a") + click % ("git", "b"), doc=self.settings_doc())
        self.assertIn('data-k="floor"', out["R"]["a"])
        self.assertIn('data-f="cols" data-v="settings" aria-pressed="true"', out["R"]["a"])
        self.assertIn('data-k="branch"', out["R"]["b"])
        self.assertNotIn('data-k="floor"', out["R"]["b"])
        self.assertEqual(out["qs"][-2:], ["cols=settings", ""])

    def test_a_reading_that_failed_is_unknown_with_its_reason(self):
        doc = self.settings_doc()
        row = next(r for r in doc["repos"] if r["path"] == self.ids["checkout"])
        row["protection"] = dict(row["protection"], status="unknown", reason="not yet observed", required=None)
        row["runtimes"] = {"python": {"error": "pyproject.toml is not valid TOML"}, "node": None}
        out = self.run_page("R.repos = ELS['view-repos'].html;", doc=doc, search="?view=repos&cols=settings&managed=yes")
        repos = out["R"]["repos"]
        cells = dict(re.findall(r'data-k="(\w+)">(.*?)</td>', repos))
        self.assertIn("▨", cells["checks"])
        self.assertIn("not yet observed", cells["checks"])
        self.assertIn("pyproject.toml is not valid TOML", cells["python"])

    def test_a_source_that_failed_renders_unknown_with_its_reason(self):
        doc = dict(self.doc, git=None, sessions=None, sources=dict(self.doc["sources"], git="stopped at its budget", sessions="stopped at its budget"))
        out = self.run_page("R.repos = ELS['view-repos'].html; R.sess = ELS['view-sessions'].html;", doc=doc)
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("git, sessions", out["states"][-1]["text"])
        self.assertIn("git state not read: stopped at its budget", out["R"]["repos"])
        self.assertIn("Sessions were not read", out["R"]["sess"])

    def test_claude_sessions_list_waiting_first_light_the_lamp_and_open_their_facts(self):
        """sd:5106: task, last action, waits on you and safe to exit per session; Details say why it is not safe."""
        out = self.run_page("""R.sess = ELS['view-sessions'].html; const cell = ELS.annunciator.html.split('data-view="sessions"')[1].split('data-view=')[0];
R.lamp = [/data-state="([a-z]+)"/.exec(cell)[1], cell];
selectRow('cs:s-wait', false); R.details = ELS.details.html;""", search="?view=sessions")
        sess = out["R"]["sess"]
        self.assertLess(sess.index('data-id="cs:s-wait"'), sess.index('data-id="cs:s-idle"'))
        self.assertIn("#7 Port the page", sess)
        self.assertIn('datetime="2026-09-06T11:58:00Z"', sess)
        self.assertIn("▲</span> permission: Bash", sess)
        self.assertIn("◌</span> no · it waits on you: permission: Bash", sess)
        self.assertIn("●</span> yes", sess)
        self.assertIn("1 builder running", sess)
        self.assertEqual(out["R"]["lamp"][0], "caution")
        self.assertIn("wait", out["R"]["lamp"][1])
        self.assertIn("<h2>hoa</h2>", out["R"]["details"])
        self.assertIn("Why not", out["R"]["details"])
        self.assertIn("via branch sd7-x", out["R"]["details"])

    def test_end_session_confirms_with_what_stops_then_posts_the_id_and_pid_it_showed(self):
        """sd:5126: the row's End session names the agents that stop with it, posts the session and pid, then rereads."""
        doc = json.loads(json.dumps(self.doc))
        wait = doc["claude"]["sessions"][0]
        wait["agents"] = [{"name": "b5126", "builder": True, "state": "stalled", "items": [5126], "description": "",
                           "last": "2026-09-06T11:00:00Z", "age": 3600}]
        wait["watches"] = [{"pid": 900, "command": "sd-ship lane watch"}]
        out = self.run_page("""R.sess = ELS['view-sessions'].html; const o = C.get('cs:s-wait');
R.why = cmd('cs.end').consequence(o); R.cli = cmd('cs.end').cli(o); R.primary = [cmd('cs.end').primary(o), cmd('cs.end').primary(C.get('cs:s-idle'))];
C.run(cmd('cs.end'), o); await flush();""", doc=doc, search="?view=sessions",
                            answer="(p, b) => [200, {id: 's-wait', pid: 100, name: 'hoa', ended: 'SIGTERM sent'}]")
        claude = out["R"]["sess"].split('id="agents-h"')[0]
        self.assertIn('<th scope="col">Safe to exit</th><th scope="col" class="act">', claude)
        self.assertEqual(claude.count('<td class="act end">'), 2)
        self.assertEqual(out["confirms"], ["cs.end"])
        self.assertIn("Sends SIGTERM to hoa (PID 100). 1 agent under it stops with it: b5126 (stalled).", out["R"]["why"])
        self.assertIn("Safe to exit: no · it waits on you: permission: Bash.", out["R"]["why"])
        self.assertIn("stay; 1 lane watch it started is not signalled.", out["R"]["why"])
        self.assertEqual(out["R"]["cli"], "kill -TERM 100")
        self.assertEqual(out["R"]["primary"], [True, False])
        self.assertEqual([(p, b) for p, b, _ in out["posts"]], [("/api/claude-sessions/end", {"id": "s-wait", "pid": 100})])
        self.assertEqual(out["toasts"][0], ["End requested · hoa", False])
        self.assertEqual(out["gets"], ["/api/management", "/api/management"])

    def test_end_session_is_off_without_a_pid(self):
        doc = json.loads(json.dumps(self.doc))
        doc["claude"]["sessions"][1]["pid"] = None
        out = self.run_page("R.when = cmd('cs.end').when(C.get('cs:s-idle'));", doc=doc, search="?view=sessions")
        self.assertEqual(out["R"]["when"], "claude agents gave no pid, so there is nothing to signal")

    def test_claude_sessions_not_read_render_unknown_with_the_reason(self):
        doc = dict(self.doc, claude=None, sources=dict(self.doc["sources"], claude="claude is not on PATH"))
        out = self.run_page("""R.sess = ELS['view-sessions'].html;
R.lamp = ELS.annunciator.html.split('data-view="sessions"')[1].split('data-view=')[0];""", doc=doc, search="?view=sessions")
        self.assertIn("Claude sessions were not read", out["R"]["sess"])
        self.assertIn("claude is not on PATH", out["R"]["sess"])
        self.assertNotIn("on you", out["R"]["lamp"])

    def test_sessions_or_a_status_not_read_leave_the_lamp_unknown_not_ok(self):
        """Review class (sd:5106 round 1): a wait that was not read is not "nobody waits"."""
        quiet = dict(self.doc["sessions"], abandoned=0)
        cell = """R.lamp = ELS.annunciator.html.split('data-view="sessions"')[1].split('data-view=')[0]; R.sess = ELS['view-sessions'].html;"""
        out = self.run_page(cell, doc=dict(self.doc, sessions=quiet, claude=None), search="?view=sessions")
        self.assertIn('data-state="unknown"', out["R"]["lamp"])
        self.assertIn("Claude sessions not read", out["R"]["lamp"])
        unheard = dict(CLAUDE, sessions=[dict(claude_row("s-mute", "mute", status="unknown", safe="unknown"), waiting=None)])
        out = self.run_page(cell, doc=dict(self.doc, sessions=quiet, claude=unheard), search="?view=sessions")
        self.assertIn('data-state="unknown"', out["R"]["lamp"])
        self.assertIn("not known to wait", out["R"]["lamp"])
        self.assertIn("▨</span> not read</td>", out["R"]["sess"])
        out = self.run_page(cell, doc=dict(self.doc, sessions=quiet, claude=dict(CLAUDE, sessions=[claude_row("s-idle", "system")])),
                            search="?view=sessions")
        self.assertIn('data-state="ok"', out["R"]["lamp"])

    def test_a_snapshot_section_says_when_it_was_observed_and_a_stale_one_is_not_green(self):
        git = dict(self.doc["git"], observed="2026-09-06T11:52:00Z", stale=None)
        sess = dict(self.doc["sessions"], observed="2026-09-06T11:31:00Z", stale="git did not answer")
        out = self.run_page("""R.repos = ELS['view-repos'].html; R.sess = ELS['view-sessions'].html;
R.lamp = /data-view="sessions" data-state="(\\w+)"/.exec(ELS.annunciator.html)[1];""", doc=dict(self.doc, git=git, sessions=sess, claude=dict(self.doc["claude"], sessions=[])))
        self.assertIn("git read 11:52 UTC", out["R"]["repos"])
        self.assertNotIn("stale", out["R"]["repos"])
        self.assertIn("fleet · 11:31 UTC · <span class=\"g-unknown\">▨</span> stale: git did not answer", out["R"]["sess"])
        self.assertEqual(out["R"]["lamp"], "unknown")

    def test_pull_asks_then_posts_the_registered_path_and_is_off_for_an_unregistered_checkout(self):
        """sd:5888: Pull runs through /api/actions/run; the server checks the checkout again, so the page sends only its key."""
        path = self.ids["checkout"]
        behind = {"path": path, "state": "behind", "remedy": f"git -C {path} pull --ff-only", "branch": "main", "default": "main",
                  "dirty": 0, "ahead": 0, "behind": 2, "headline": "2 behind", "detail": "", "error": None}
        loose = dict(behind, path="/elsewhere/loose")
        doc = dict(self.doc, git=dict(self.doc["git"] or {}, repos=[behind, loose]))
        out = self.run_page(f"""const was = ANSWER; ANSWER = (p, b) => p === '/api/actions/run' ? [200, {{ action: b.action, label: 'Pull', line: 'l', exit: 0, output: 'Fast-forward', cut: false }}] : was(p, b);
R.off = cmd('repo.pull').when(C.get('repo:/elsewhere/loose')); R.on = cmd('repo.pull').when(C.get('repo:{path}'));
shellRun(cmd('repo.pull'), C.get('repo:{path}')); await flush(); R.toast = lastToast().msg;""", doc=doc)
        self.assertEqual(out["R"]["off"], "this checkout is not registered; Pull works only in a registered one")
        self.assertIs(out["R"]["on"], True)
        self.assertEqual(([b for p, b, _ in out["posts"] if p == "/api/actions/run"], out["confirms"]), ([{"action": "repo.pull", "path": path}], ["repo.pull"]))
        self.assertEqual(out["R"]["toast"], "Pull · exit 0 · the output is in Details")

    def test_a_repo_flip_posts_the_verb_with_before_toasts_after_the_write_and_undoes(self):
        path = self.ids["checkout"]
        out = self.run_page(f"""C.run(cmd('repo.runner-merge'), C.get('repo:{path}')); R.early = OUT.toasts.length; await flush();
R.toast = lastToast().msg; const undo = lastToast().undo; await undo(); await flush(); await undo(); await flush();""", answer="(p, b) => [200, {}]")
        posts = [(p, b) for p, b, _ in out["posts"]]
        self.assertEqual(out["R"]["early"], 0, "the toast came before the write landed")
        self.assertEqual(posts, [("/api/repos/runner-merge", {"path": path, "value": "auto", "before": "manual"}),
                                 ("/api/repos/runner-merge", {"path": path, "value": "manual", "before": "auto"})])
        self.assertEqual(out["R"]["toast"], f"runner_merge auto · {path}")
        self.assertEqual(out["posts"][0][2], 64)

    def test_adversarial_review_posts_the_repo_key_and_is_off_for_an_unregistered_checkout(self):
        """sd:5064: a registered repo row asks for {kind: repo, target: its path key}; Details follow the run."""
        path = self.ids["checkout"]
        answer = """(p, b) => p === '/api/adversarial' ? [202, { id: 'r1', state: 'queued', item: null, filed: null }]
  : p === '/api/adversarial/r1' ? [200, { id: 'r1', state: 'done', item: 41, filed: 'written', brief_item: 77 }] : [404, { error: 'no' }]"""
        out = self.run_page(f"""const busy = [...OBJ.values()].find(o => o.type === 'repo' && !o.row.registered);
R.off = cmd('repo.adversarial-review').when(busy); selectRow('repo:{path}');
C.run(cmd('repo.adversarial-review'), C.get('repo:{path}')); await flush(); await flush(); R.det = ELS.details.html;""", answer=answer)
        self.assertEqual(out["posts"], [["/api/adversarial", {"kind": "repo", "target": path}, 64]])
        self.assertEqual(out["R"]["off"], "register this repo first (sd-db repo add)")
        self.assertIn('<a href="/item/77">brief sd:77</a>', out["R"]["det"])

    def github_doc(self, pulls):
        doc = json.loads(json.dumps(self.doc))
        row = next(r for r in doc["repos"] if r["path"] == self.ids["checkout"])
        row["remote"] = "git@github.com:Example/Checkout.git"
        doc["pulls"] = pulls
        return doc

    def test_adversarial_review_pr_offers_the_repo_s_open_pull_requests_and_posts_the_one_picked(self):
        """sd:5100: the dialog's select lists the shadow rows for the repo's slug; the post names the repo and the number."""
        path = self.ids["checkout"]
        doc = self.github_doc({"example/checkout": [{"number": 12, "title": "Add x", "author": "a", "url": "u12"},
                                                    {"number": 9, "title": "Fix y", "author": "b", "url": "u9"}]})
        answer = """(p, b) => p === '/api/adversarial' ? [202, { id: 'r1', state: 'queued', item: null, filed: null }]
  : p === '/api/adversarial/r1' ? [200, { id: 'r1', state: 'done', item: null, filed: 'written', brief_item: 78 }] : [404, { error: 'no' }]"""
        out = self.run_page(f"""const o = C.get('repo:{path}'), c = cmd('repo.adversarial-pr');
R.fields = c.fields(o); R.on = c.when(o); selectRow('repo:{path}');
FIELD_VALUES = {{ pr: '9' }}; C.run(c, o); await flush(); await flush(); R.det = ELS.details.html;""",
                            answer=answer, doc=doc)
        self.assertEqual(out["confirms"], ["repo.adversarial-pr"])
        self.assertEqual(out["R"]["on"], True)
        self.assertEqual(out["R"]["fields"], [{"name": "pr", "label": "Pull request", "options": [
            {"value": "12", "label": "#12 Add x"}, {"value": "9", "label": "#9 Fix y"}]}])
        self.assertEqual(out["posts"], [["/api/adversarial", {"kind": "change", "target": path, "pr": 9}, 64]])
        self.assertIn("<h3>Adversarial review: PR</h3>", out["R"]["det"])
        self.assertIn('<a href="/item/78">brief sd:78</a>', out["R"]["det"])

    def test_adversarial_review_pr_is_off_with_its_reason(self):
        path = self.ids["checkout"]
        script = f"R.why = cmd('repo.adversarial-pr').when(C.get('repo:{path}'));"
        for doc, said in ((self.github_doc({}), "no open pull request as of the last GitHub sync"),
                          (self.github_doc(None), "pull requests not read: the reading carries none"),
                          (dict(self.doc, pulls={"example/checkout": [{"number": 1, "title": "t"}]}),
                           "not a GitHub repository, so it has no pull requests")):
            with self.subTest(said=said):
                self.assertEqual(self.run_page(script, doc=doc)["R"]["why"], said)

    def test_deep_review_asks_first_then_posts_the_repo_and_shows_its_progress(self):
        """sd:5100: the confirm names the bound and the read-only sub-agents; Details shows the run against its bound."""
        path = self.ids["checkout"]
        # The stand-in's setTimeout runs at once, so the first poll answers settled; the running draw is caught as it lands.
        answer = """(p, b) => p === '/api/adversarial' ? [202, { id: 'd1', state: 'running', item: null, filed: null,
    started: new Date(Date.now() - 600000).toISOString(), timeout: 5400 }]
  : p === '/api/adversarial/d1' ? [200, { id: 'd1', state: 'done', item: null, filed: 'written', brief_item: 79 }] : [404, { error: 'no' }]"""
        out = self.run_page(f"""const o = C.get('repo:{path}'), c = cmd('repo.deep-review');
R.asks = c.askFirst; R.says = c.consequence(o); R.off = c.when([...OBJ.values()].find(x => x.type === 'repo' && !x.row.registered));
document.addEventListener('adversarial:run', e => {{ if (e.detail.run.state === 'running') R.det = ELS.details.html; }});
selectRow('repo:{path}'); C.run(c, o); await flush(); await flush(); R.end = ELS.details.html;""", answer=answer)
        self.assertTrue(out["R"]["asks"])
        self.assertEqual(out["confirms"], ["repo.deep-review"])
        self.assertIn("up to 8 read-only helpers, for up to 90 minutes", out["R"]["says"])
        self.assertEqual(out["R"]["off"], "register this repo first (sd-db repo add)")
        self.assertEqual(out["posts"], [["/api/adversarial", {"kind": "deep", "target": path}, 64]])
        self.assertIn("<h3>Deep review</h3>", out["R"]["det"])
        self.assertIn("Run d1 is running, 10 of at most 90 min.", out["R"]["det"])
        self.assertIn('<a href="/item/79">brief sd:79</a>', out["R"]["end"])

    def test_deep_review_settings_show_in_details_and_point_at_settings(self):
        """sd:5109, sd:5784: Details shows each setting and where it comes from; the Settings page changes them, so
        Management offers no dialog, button or command for them."""
        path = self.ids["checkout"]
        doc = json.loads(json.dumps(self.doc))
        doc["deep"]["adversarial.deep.model"] = {"value": "fable", "from": "database"}
        out = self.run_page(f"""R.button = ELS['view-repos'].html.includes('data-deep-settings'); R.cmd = !!cmd('repo.deep-settings');
R.obj = !!C.get('deep:settings'); selectRow('repo:{path}'); R.det = ELS.details.html;""", doc=doc)
        self.assertIn("Deep review settings: model <b>fable</b> · effort <b>max</b> (default)", out["R"]["det"])
        self.assertIn("sub-agents at most <b>8</b> (default). <a href=\"/settings\">Settings</a> changes them", out["R"]["det"])
        self.assertEqual((out["R"]["button"], out["R"]["cmd"], out["R"]["obj"]), (False, False, False))

    def test_deep_review_settings_not_read_are_named_in_details(self):
        path = self.ids["checkout"]
        doc = dict(self.doc, deep=None, sources=dict(self.doc["sources"], deep="the setting adversarial.deep.agents wants x"))
        out = self.run_page(f"selectRow('repo:{path}'); R.det = ELS.details.html;", doc=doc)
        self.assertIn("Deep review settings not read: the setting adversarial.deep.agents wants x.", out["R"]["det"])

    def test_the_repo_page_shows_the_lane_host_and_move_lane_runs_the_verb_and_undoes(self):
        """sd:3075, acceptance 3: the list and the page show the host; the field offers hub and each named host;
        Run posts the move with the value the page showed, and Undo moves it back."""
        doc = json.loads(json.dumps(self.doc))
        doc["repos"].append(dict(doc["repos"][0], path="/srv/other", lane_host="build-3"))
        path = self.ids["checkout"]
        out = self.run_page("""R.page = ELS['view-repos'].html; propose('lane_host', 'build-2'); R.cmd = pending[0].cmd; R.note = pending[0].note;
C.run(cmd('sddb.run'), pending[0]); await flush(); R.toast = lastToast().msg; await lastToast().undo(); await flush();""",
                            doc=doc, search=f"?repo={path}", answer="(p, b) => [200, {}]")
        page = out["R"]["page"]
        self.assertIn('data-key="lane_host"', page)
        self.assertIn('data-set="lane_host"', page)
        self.assertEqual(re.findall(r'<option value="([^"]+)"></option>', page.split('id="lane-hosts"')[1]), ["hub", "build-3"])
        self.assertEqual(out["R"]["cmd"], f"sd-db repo lane-host '{path}' 'build-2'")
        # sd:3284: the queue lives in the hub database, so the move takes the pending entries with it.
        self.assertIn("Queued changes follow", out["R"]["note"])
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/lane-host", {"path": path, "value": "build-2", "before": "hub"}),
                          ("/api/repos/lane-host", {"path": path, "value": "hub", "before": "build-2"})])
        self.assertEqual(out["R"]["toast"], "Ran locally · Merge machine: hub → build-2")
        out = self.run_page("R.repos = ELS['view-repos'].html;", doc=doc)
        cells = re.findall(r'data-k="lane">([^<]*)</td>', out["R"]["repos"])
        self.assertIn("hub", cells)
        self.assertIn("build-3", cells)

    # The channels form's inputs: document.querySelector answers each `#ch-form [data-ch...]` selector from FORM.
    FORM = """const FORM = {}; const fill = (c, on, folder) => { FORM[`#ch-form [data-ch="${c}"]`] = { checked: on }; FORM[`#ch-form [data-chf="${c}"]`] = { value: folder }; };
const pageQuery = document.querySelector; document.querySelector = sel => FORM[sel] || pageQuery(sel);
"""

    def test_the_channels_form_shows_the_defaults_proposes_the_verb_runs_it_and_undoes(self):
        """sd:3465: a repo with no setting shows the defaults; a checkbox and a folder per channel propose the
        `sd-db repo channels` line; Run posts the value with the setting the page showed and toasts the render remedy;
        Undo writes the old setting back; a form with no channel checked proposes nothing."""
        path = self.ids["checkout"]
        name = Path(path).name
        remedy = f"Run `sd-research-kit render` in {path} to publish under it, or wait for the next commit."
        out = self.run_page(self.FORM + """R.page = ELS['view-repos'].html;
fill('obsidian', true, ' Briefs/x '); fill('notion', false, 'Ignored'); fill('drive', true, '');
propose('channels', 'on'); R.cmd = pending[0].cmd; R.title = pending[0].title; R.note = pending[0].note;
C.run(cmd('sddb.run'), pending[0]); await flush(); R.toast = lastToast().msg; await lastToast().undo(); await flush();
fill('obsidian', false, ''); fill('drive', false, ''); propose('channels', 'on'); R.left = pending.length;""",
                            search=f"?repo={path}", answer="(p, b) => [200, { value: '{\"drive\":{},\"obsidian\":{\"folder\":\"Briefs/x\"}}', remedy: " + json.dumps(remedy) + " }]")
        page = out["R"]["page"]
        self.assertIn('data-key="channels"', page)
        self.assertIn('<span class="no">none · defaults</span>', page)
        self.assertIn(f"in Briefs/{name} and {name}.", page)
        self.assertEqual(len(re.findall(r'<input type="checkbox" data-set="channels" data-ch="\w+" checked>', page)), 3)
        self.assertIn(f'data-chf="drive" value="" placeholder="Briefs/{name}"', page)
        self.assertNotIn("data-ch-clear", page, "no setting, nothing to clear")
        self.assertEqual(out["R"]["cmd"], f"sd-db repo channels '{path}' 'obsidian=Briefs/x' 'drive='")
        self.assertEqual(out["R"]["title"], "Publishing: none → obsidian=Briefs/x drive=")
        self.assertIn("sd-research-kit render", out["R"]["note"])
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/channels", {"path": path, "value": {"obsidian": {"folder": "Briefs/x"}, "drive": {}}, "before": "none"}),
                          ("/api/repos/channels", {"path": path, "value": None, "before": '{"drive":{},"obsidian":{"folder":"Briefs/x"}}'})])
        self.assertEqual(out["R"]["toast"], f"Ran locally · Publishing: none → obsidian=Briefs/x drive= · {remedy}")
        self.assertEqual(out["R"]["left"], 0)
        self.assertEqual(out["toasts"][-1], ["Not proposed: no channel is checked. Clear setting removes the setting.", False])

    FEED = '{"url":"https://example.test/feed.xml"}'

    def stored_doc(self, text):
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["repos"] if r["path"] == self.ids["checkout"])["channels"] = text
        return doc

    def test_the_channels_form_undoes_against_the_value_the_write_answered_when_a_feed_was_kept(self):
        """sd:4632: the library keeps the stored feed, so the row after the write is not the posted object. Undo posts the
        answered value as its `before` and writes back the whole old setting, feed included."""
        path = self.ids["checkout"]
        old = '{"drive":{},"feed":' + self.FEED + '}'
        kept = '{"feed":' + self.FEED + ',"obsidian":{}}'
        out = self.run_page(self.FORM + """fill('obsidian', true, ''); fill('notion', false, ''); fill('drive', false, '');
propose('channels', 'on'); C.run(cmd('sddb.run'), pending[0]); await flush(); await lastToast().undo(); await flush();""",
                            doc=self.stored_doc(old), search=f"?repo={path}",
                            answer="(p, b) => [200, { value: " + json.dumps(kept) + " }]")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/channels", {"path": path, "value": {"obsidian": {}}, "before": old}),
                          ("/api/repos/channels", {"path": path, "value": {"drive": {}, "feed": json.loads(self.FEED)}, "before": kept})])

    def test_a_form_that_matches_the_stored_channels_proposes_nothing_and_a_feed_only_row_has_no_undo(self):
        """sd:4632: the stored feed is not part of the form's value, so an unchanged form is no change; a row that held
        only a feed cannot be put back by a set, so that write offers no Undo rather than one that drops the feed."""
        path = self.ids["checkout"]
        out = self.run_page(self.FORM + """fill('obsidian', false, ''); fill('notion', false, ''); fill('drive', true, '');
propose('channels', 'on'); R.same = pending.length;""",
                            doc=self.stored_doc('{"drive":{},"feed":' + self.FEED + '}'), search=f"?repo={path}")
        self.assertEqual(out["R"]["same"], 0)
        out = self.run_page(self.FORM + """fill('obsidian', true, ''); fill('notion', false, ''); fill('drive', false, '');
propose('channels', 'on'); C.run(cmd('sddb.run'), pending[0]); await flush(); await lastToast().undo(); await flush();""",
                            doc=self.stored_doc('{"feed":' + self.FEED + '}'), search=f"?repo={path}",
                            answer="(p, b) => [200, { value: '{}' }]")
        self.assertEqual(len(out["posts"]), 1)
        self.assertIn("nothing to reverse", out["toasts"][-1][0])

    def test_the_channels_form_shows_a_stored_feed_as_text_and_offers_no_field_for_it(self):
        """sd:4192: an inbound key the row holds shows beside the channels, display only; a feed alone is no outbound
        setting, so the defaults show and every channel box stays on."""
        path = self.ids["checkout"]
        both = self.run_page("R.page = ELS['view-repos'].html;", search=f"?repo={path}",
                             doc=self.stored_doc('{"drive":{},"feed":' + self.FEED + '}'))["R"]["page"]
        alone = self.run_page("R.page = ELS['view-repos'].html;", search=f"?repo={path}",
                              doc=self.stored_doc('{"feed":' + self.FEED + '}'))["R"]["page"]
        for page in (both, alone):
            self.assertIn("Inbound feed", page)
            self.assertIn("<code>https://example.test/feed.xml</code>", page)
            self.assertNotIn('data-chf="feed"', page)
            self.assertNotIn('data-ch="feed"', page)
        self.assertIn("drive Briefs/", both)
        self.assertEqual(len(re.findall(r'data-ch="\w+" checked>', both)), 1)
        self.assertIn('<span class="no">none · defaults</span>', alone)
        self.assertEqual(len(re.findall(r'data-ch="\w+" checked>', alone)), 3)
        plain = self.run_page("R.page = ELS['view-repos'].html;", search=f"?repo={path}")["R"]["page"]
        self.assertNotIn("Inbound", plain)

    def test_the_email_aliases_field_proposes_the_verb_posts_only_email_and_undoes(self):
        """sd:4191: the repo's mail aliases are their own field. The write names only `email`, so the library keeps the
        channels and the other inbound keys; Undo writes the whole old setting back; an unchanged or emptied field
        proposes nothing, since only Clear setting removes the key."""
        path = self.ids["checkout"]
        old = '{"drive":{},"email":{"aliases":["acme"]}}'
        new = '{"drive":{},"email":{"aliases":["acme","kiln"]}}'
        out = self.run_page("""R.page = ELS['view-repos'].html; propose('mail_aliases', 'kiln, acme'); R.cmd = pending[0].cmd;
R.title = pending[0].title; C.run(cmd('sddb.run'), pending[0]); await flush(); await lastToast().undo(); await flush();
propose('mail_aliases', 'acme'); R.same = pending.length; propose('mail_aliases', ''); R.empty = pending.length;""",
                            doc=self.stored_doc(old), search=f"?repo={path}", answer="(p, b) => [200, { value: " + json.dumps(new) + " }]")
        page = out["R"]["page"]
        self.assertIn('data-key="mail_aliases"', page)
        self.assertIn('data-set="mail_aliases" value="acme"', page)
        self.assertNotIn("Inbound email", page)
        self.assertEqual(out["R"]["cmd"], f"sd-db repo channels '{path}' 'email=acme,kiln'")
        self.assertEqual(out["R"]["title"], "Email aliases: acme → acme,kiln")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/channels", {"path": path, "value": {"email": {"aliases": ["acme", "kiln"]}}, "before": old}),
                          ("/api/repos/channels", {"path": path, "value": json.loads(old), "before": new})])
        self.assertEqual((out["R"]["same"], out["R"]["empty"]), (0, 0))
        self.assertEqual(out["toasts"][-1], ["Not proposed: an alias field cannot be emptied alone. Clear setting removes the whole setting.", False])

    def test_aliases_on_a_row_with_no_setting_undo_to_none_and_a_row_without_aliases_has_no_undo(self):
        """sd:4191: a set that names `email` over NULL is undone by clearing; over a setting without aliases, writing the
        old setting back would keep the new aliases, so that write offers no Undo."""
        path = self.ids["checkout"]
        out = self.run_page("""propose('mail_aliases', 'acme'); C.run(cmd('sddb.run'), pending[0]); await flush();
await lastToast().undo(); await flush();""", search=f"?repo={path}", answer="(p, b) => [200, { value: '{\"email\":{\"aliases\":[\"acme\"]}}' }]")
        self.assertEqual([b["value"] for _, b, _ in out["posts"]], [{"email": {"aliases": ["acme"]}}, None])
        out = self.run_page("""propose('mail_aliases', 'acme'); C.run(cmd('sddb.run'), pending[0]); await flush();
await lastToast().undo(); await flush();""", doc=self.stored_doc('{"drive":{}}'), search=f"?repo={path}",
                            answer="(p, b) => [200, { value: '{}' }]")
        self.assertEqual(len(out["posts"]), 1)
        self.assertIn("nothing to reverse", out["toasts"][-1][0])

    def test_a_channels_value_the_library_refuses_shows_why_and_clear_writes_none_without_an_undo(self):
        # sd:3465: such a value is not read as the defaults; it cannot be written back, so the clear has no Undo.
        path = self.ids["checkout"]
        doc = json.loads(json.dumps(self.doc))
        row = next(r for r in doc["repos"] if r["path"] == path)
        row.update(channels='{"site":{}}', channels_problem="'site' is not a channel; expected obsidian, notion or drive")
        out = self.run_page("""R.page = ELS['view-repos'].html; propose('channels', null); R.title = pending[0].title;
C.run(cmd('sddb.run'), pending[0]); await flush(); R.toast = lastToast().msg; await lastToast().undo(); await flush();""",
                            doc=doc, search=f"?repo={path}", answer="(p, b) => [200, {}]")
        page = out["R"]["page"]
        self.assertIn("Cannot be used: &#39;site&#39; is not a channel", page)
        self.assertIn("data-ch-clear", page)
        self.assertNotIn(" checked>", page.split('id="ch-form"')[1].split("</fieldset>")[0])
        self.assertEqual(out["R"]["title"], 'Publishing: {"site":{}} → none')
        self.assertEqual([(p, b) for p, b, _ in out["posts"]], [("/api/repos/channels", {"path": path, "value": None, "before": '{"site":{}}'})])
        self.assertEqual(out["R"]["toast"], 'Ran locally · Publishing: {"site":{}} → none')
        self.assertEqual(out["toasts"][-1], ['Run not undone · Publishing: {"site":{}} → none: nothing to reverse', False])

    def test_the_repo_page_shows_github_issues_and_the_toggle_runs_the_verb_and_undoes(self):
        """sd:4840: off, read or ask, written through `sd-db repo github-issues`; an unread setting shows no toggle."""
        path = self.ids["checkout"]
        out = self.run_page("""R.page = ELS['view-repos'].html; propose('github_issues', 'ask'); R.cmd = pending[0].cmd;
C.run(cmd('sddb.run'), pending[0]); await flush(); R.toast = lastToast().msg; await lastToast().undo(); await flush();""",
                            search=f"?repo={path}", answer="(p, b) => [200, {}]")
        self.assertIn('data-set="github_issues"', out["R"]["page"])
        self.assertEqual(out["R"]["cmd"], f"sd-db repo github-issues '{path}' ask")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/github-issues", {"path": path, "value": "ask", "before": "off"}),
                          ("/api/repos/github-issues", {"path": path, "value": "off", "before": "ask"})])
        self.assertEqual(out["R"]["toast"], "Ran locally · GitHub issues: off → ask")
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["repos"] if r["path"] == path)["github_issues"] = None
        page = self.run_page("R.page = ELS['view-repos'].html;", doc=doc, search=f"?repo={path}")["R"]["page"]
        cell = page.split('data-key="github_issues"')[1].split('<div class="row"')[0]
        self.assertIn("not read", cell)
        self.assertNotIn('data-set="github_issues"', cell)

    def test_the_repo_page_shows_the_issues_scope_and_the_toggle_runs_the_verb_and_undoes(self):
        """pack sd:7672: all or mine, written through `sd-db repo issues-scope`; an unread setting shows no toggle."""
        path = self.ids["checkout"]
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["repos"] if r["path"] == path)["issues_scope"] = "all"
        out = self.run_page("""R.page = ELS['view-repos'].html; propose('issues_scope', 'mine'); R.cmd = pending[0].cmd;
R.note = pending[0].note; C.run(cmd('sddb.run'), pending[0]); await flush(); R.toast = lastToast().msg;
await lastToast().undo(); await flush();""", doc=doc, search=f"?repo={path}", answer="(p, b) => [200, {}]")
        cell = out["R"]["page"].split('data-key="issues_scope"')[1].split('<div class="row"')[0]
        self.assertIn('data-set="issues_scope"', cell)
        self.assertEqual(out["R"]["cmd"], f"sd-db repo issues-scope '{path}' mine")
        self.assertIn("assigned to you", out["R"]["note"])
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/issues-scope", {"path": path, "value": "mine", "before": "all"}),
                          ("/api/repos/issues-scope", {"path": path, "value": "all", "before": "mine"})])
        self.assertEqual(out["R"]["toast"], "Ran locally · Issue intake: all → mine")
        next(r for r in doc["repos"] if r["path"] == path)["issues_scope"] = None
        page = self.run_page("R.page = ELS['view-repos'].html;", doc=doc, search=f"?repo={path}")["R"]["page"]
        cell = page.split('data-key="issues_scope"')[1].split('<div class="row"')[0]
        self.assertIn("not read", cell)
        self.assertNotIn('data-set="issues_scope"', cell)

    def test_the_repo_page_shows_jira_project_beside_github_issues_and_the_field_runs_the_verb_and_undoes(self):
        """sd:5103: a key or off, written through `sd-db repo jira-project`; an empty field is off, a lower-case key is
        upper-cased, the shown value proposes nothing, and an unread setting shows no field. Both issue settings sit
        just before the publish channels."""
        path = self.ids["checkout"]
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["repos"] if r["path"] == path)["jira_project"] = "off"
        out = self.run_page("""R.page = ELS['view-repos'].html; propose('jira_project', 'off'); R.same = pending.length;
propose('jira_project', 'example'); R.cmd = pending[0].cmd; R.note = pending[0].note;
C.run(cmd('sddb.run'), pending[0]); await flush(); R.toast = lastToast().msg; await lastToast().undo(); await flush();""",
                            doc=doc, search=f"?repo={path}", answer="(p, b) => [200, {}]")
        page = out["R"]["page"]
        self.assertIn('data-set="jira_project"', page)
        keys = re.findall(r'data-key="(\w+)"', page)
        self.assertEqual(keys[keys.index("github_issues"):keys.index("channels") + 1], ["github_issues", "issues_scope", "jira_project", "channels"])
        self.assertEqual(out["R"]["same"], 0)
        self.assertEqual(out["R"]["cmd"], f"sd-db repo jira-project '{path}' 'EXAMPLE'")
        self.assertIn("Jira project EXAMPLE", out["R"]["note"])
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/jira-project", {"path": path, "value": "EXAMPLE", "before": "off"}),
                          ("/api/repos/jira-project", {"path": path, "value": "off", "before": "EXAMPLE"})])
        self.assertEqual(out["R"]["toast"], "Ran locally · Jira project: off → EXAMPLE")
        next(r for r in doc["repos"] if r["path"] == path)["jira_project"] = None
        page = self.run_page("R.page = ELS['view-repos'].html;", doc=doc, search=f"?repo={path}")["R"]["page"]
        cell = page.split('data-key="jira_project"')[1].split('<div class="row"')[0]
        self.assertIn("not read", cell)
        self.assertNotIn('data-set="jira_project"', cell)

    def test_the_repo_page_shows_dependabot_and_the_toggle_runs_the_verb_and_undoes(self):
        """sd:3984: yes or no for a repo outside the owner rule; an owner's or a denied repo shows the rule and no toggle."""
        path = self.ids["checkout"]
        out = self.run_page("""R.page = ELS['view-repos'].html; propose('dependabot', 'yes'); R.cmd = pending[0].cmd;
C.run(cmd('sddb.run'), pending[0]); await flush(); R.toast = lastToast().msg; await lastToast().undo(); await flush();""",
                            search=f"?repo={path}", answer="(p, b) => [200, {}]")
        self.assertIn('data-set="dependabot"', out["R"]["page"])
        self.assertEqual(out["R"]["cmd"], f"sd-db repo dependabot '{path}' yes")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]],
                         [("/api/repos/dependabot", {"path": path, "value": "yes", "before": "no"}),
                          ("/api/repos/dependabot", {"path": path, "value": "no", "before": "yes"})])
        self.assertEqual(out["R"]["toast"], "Ran locally · Auto-merge dependency updates: no → yes")
        doc = json.loads(json.dumps(self.doc))
        for gate, shown in (("owner", "always (owner)"), ("denied", "denied by name"), (None, "not read")):
            row = next(r for r in doc["repos"] if r["path"] == path)
            row["dependabot_gate"] = gate
            row["dependabot"] = "no" if gate else None
            page = self.run_page("R.page = ELS['view-repos'].html;", doc=doc, search=f"?repo={path}")["R"]["page"]
            cell = page.split('data-key="dependabot"')[1].split('<div class="row"')[0]
            self.assertIn(shown, cell)
            self.assertNotIn('data-set="dependabot"', cell)

    def test_a_refused_write_says_why_and_reads_again(self):
        # Turning managed off is the one-column flip; turning it on opens Standard setup (sd:5610).
        path = self.ids["checkout"]
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["repos"] if r["path"] == path)["managed"] = "yes"
        out = self.run_page(f"C.run(cmd('repo.managed'), C.get('repo:{path}')); await flush();", doc=doc,
                            answer="(p, b) => [409, {error: 'managed is no now, not yes'}]")
        self.assertEqual(out["toasts"][-1], [f"{path} not changed: managed is no now, not yes", False])
        self.assertEqual(out["gets"], ["/api/management", "/api/management"])

    def test_a_failed_job_and_an_idle_one_run_through_their_routes(self):
        # An idle job may run now (sd:4982), so Run now is on for both.
        revision = {j["name"]: j["revision"] for j in self.doc["jobs"]}
        out = self.run_page("""C.run(cmd('jobs.retry'), C.get('cron:nightly-sync')); await flush();
C.run(cmd('jobs.retry'), C.get('cron:quiet')); await flush();""",
                            answer="(p, b) => [200, {request: {status: 'accepted', message: 'launchctl accepted the request'}}]")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]], [("/api/jobs/nightly-sync/retry", {"revision": revision["nightly-sync"]}),
                                                               ("/api/jobs/quiet/retry", {"revision": revision["quiet"]})])
        self.assertEqual(out["toasts"], [["Run started · nightly-sync", False], ["Run started · quiet", False]])


    def test_after_exit_127_retry_asks_first_naming_the_exit_and_print_is_the_rows_act(self):
        """sd:3392, ruled 2026-09-30: the exit is the last run's, and a path fixed outside the dashboard needs Retry to test it."""
        doc = json.loads(json.dumps(self.doc))
        next(j for j in doc["jobs"] if j["name"] == "nightly-sync")["last_exit"] = 127
        out = self.run_page("""const c = cmd('jobs.retry'), o = C.get('cron:nightly-sync');
R.on = c.when(o); R.primary = !!c.primary(o); R.print = !!cmd('jobs.print').primary(o); R.ask = !!c.askFirst(o); R.says = c.consequence(o);
C.run(c, o); await flush();""", answer="(p, b) => [200, {request: {status: 'accepted', message: 'launchctl accepted the request'}}]", doc=doc)
        self.assertEqual((out["R"]["on"], out["R"]["primary"], out["R"]["print"], out["R"]["ask"]), (True, False, True, True))
        self.assertIn("exit 127", out["R"]["says"])
        self.assertEqual(out["confirms"], ["jobs.retry"])
        self.assertEqual([p for p, _, _ in out["posts"]], ["/api/jobs/nightly-sync/retry"])
        self.assertEqual(out["toasts"], [["Run started · nightly-sync", False]])

    def test_after_another_exit_retry_is_the_rows_act_and_does_not_ask(self):
        out = self.run_page("""const c = cmd('jobs.retry'), o = C.get('cron:nightly-sync');
R.got = [!!c.primary(o), !!cmd('jobs.print').primary(o), !!c.askFirst(o)];""")
        self.assertEqual(out["R"]["got"], [True, False, False])

    def test_a_retry_shows_launchctls_answer_when_it_did_not_accept(self):
        """sd:3388: a 200 carries launchctl's answer; only `accepted` started a retry."""
        cases = {
            "(p, b) => [200, {request: {status: 'accepted', message: 'launchctl accepted the request'}}]": "Run started · nightly-sync",
            "(p, b) => [200, {request: {status: 'failed', message: 'launchctl refused the request (exit 3)'}}]":
                "nightly-sync not retried: launchctl refused the request (exit 3)",
            "(p, b) => [200, {request: {status: 'unknown', message: 'launchctl timed out; the request outcome is unknown'}}]":
                "Not known whether nightly-sync was retried: launchctl timed out; the request outcome is unknown",
        }
        for answer, said in cases.items():
            with self.subTest(said=said):
                out = self.run_page("C.run(cmd('jobs.retry'), C.get('cron:nightly-sync')); await flush();", answer=answer)
                self.assertEqual(out["toasts"], [[said, False]])

class TheFindings(PageScript):
    """The Copilot and CodeQL findings on PR #50, one test each."""

    def test_a_monthly_or_yearly_calendar_has_a_next_run(self):
        out = self.run_page("""const at = (...a) => new Date(...a).toISOString().replace('.000', '');
const now = new Date(2026, 8, 30, 12, 0).getTime();
R.got = [nextRun([{ Day: 15, Hour: 3, Minute: 0 }], now), nextRun([{ Month: 1, Day: 1, Hour: 0, Minute: 5 }], now),
  nextRun([{ Month: 2, Day: 29, Hour: 0, Minute: 0 }], now), nextRun([{ Minute: 30 }], now), nextRun([{ Weekday: 7, Hour: 2, Minute: 0 }], now)];
R.want = [at(2026, 9, 15, 3, 0), at(2027, 0, 1, 0, 5), at(2028, 1, 29, 0, 0), at(2026, 8, 30, 12, 30), at(2026, 9, 4, 2, 0)];""")
        self.assertEqual(out["R"]["got"], out["R"]["want"])

    def test_a_page_past_the_last_is_clamped_before_the_slice_and_the_url_follows(self):
        out = self.run_page("R.sched = ELS['view-schedules'].html;", search="?view=schedules&p=5")
        self.assertIn("nightly-sync", out["R"]["sched"])
        self.assertTrue(out["urls"], "the clamp wrote no URL, so ?p=5 stays in the address bar")
        self.assertEqual(out["urls"][-1], {"view": "schedules", "p": None, "q": None})
        out = self.run_page("R.repos = ELS['view-repos'].html;", search="?view=repos&p=3")
        self.assertIn("busy", out["R"]["repos"])
        self.assertEqual(out["urls"][-1]["p"], None)

    def test_the_failed_state_chip_writes_the_url(self):
        out = self.run_page("""shell.view.show('schedules');
const chip = { dataset: { f: 'state', v: 'failed' } };
const target = { closest: sel => sel === '[data-f="state"]' ? chip : null };
ELS['view-schedules'].listeners.click.forEach(f => f({ target }));
R.sched = ELS['view-schedules'].html;""")
        self.assertEqual(out["urls"][-1], {"view": "schedules", "p": None, "q": "state:failed"})
        self.assertNotIn(">quiet<", out["R"]["sched"])

    def test_the_text_token_holds_the_free_words_and_its_removal_keeps_the_pressed_state(self):
        """sd:5504: the pressed failed segment already shows state:failed, so the token is the free words alone; removing it
        keeps the segment's word, and each segment keeps its visible label."""
        click = "document.querySelector('main').listeners.click.forEach(f => f({ target: { closest: s => s === '[data-f=\"q\"]' ? { dataset: { f: 'q', v: '' } } : null } }));"
        out = self.run_page("R.a = ELS['view-schedules'].html;" + click + " R.b = ELS['view-schedules'].html; R.box = (OUT.shift ?? null);",
                            search="?view=schedules&q=state:failed nightly")
        self.assertIn('<div class="fgroup"><span class="label" aria-hidden="true">State</span><div class="fseg" role="group" aria-label="State">', out["R"]["a"])
        self.assertIn('data-f="state" data-v="failed" aria-pressed="true"', out["R"]["a"])
        self.assertEqual(re.findall(r'class="tok" data-f="q" data-v="" aria-label="Remove filter: ([^"]+)"', out["R"]["a"]), ["“nightly”"])
        self.assertEqual(out["R"]["box"], "state:failed")
        self.assertIn('data-f="state" data-v="failed" aria-pressed="true"', out["R"]["b"])
        self.assertNotIn('class="tok"', out["R"]["b"])

    def test_a_view_name_from_the_url_is_one_of_five_or_repos(self):
        for name in ("constructor", "toString", "__proto__", "nope"):
            with self.subTest(name=name):
                out = self.run_page("R.view = view; R.repos = ELS['view-repos'].html;", search=f"?view={name}")
                self.assertEqual(out["R"]["view"], "repos")
                self.assertIn("busy", out["R"]["repos"])
        out = self.run_page("view = 'constructor'; try { render(); R.drew = true; } catch (e) { R.refused = e.message; }")
        self.assertEqual(out["R"], {"refused": "no such view: constructor"})

    def test_a_bulk_flip_toasts_once_after_every_write_and_undoes_only_what_landed(self):
        """Copilot 5eccfcac7af7 on PR #50: the shell's bulk group waits for each run's promise (sd:2124's contract)."""
        a, b = self.ids["checkout"], "/repos/system"
        out = self.run_page(f"""C.runBulk(cmd('repo.runner-merge'), [C.get('repo:{a}'), C.get('repo:{b}')]); R.early = OUT.toasts.length;
await flush(); R.group = OUT.toasts.map(t => t.msg); await lastUndo().undo(); await flush(); R.after = OUT.toasts.map(t => t.msg);""",
                            answer=f"(p, b) => b.path === {json.dumps(b)} ? [409, {{error: 'runner_merge is auto now'}}] : [200, {{}}]")
        self.assertEqual(out["R"]["early"], 0, "the group toast came before the writes landed")
        self.assertEqual(out["R"]["group"], ["Switch runner-merge · 1 repo · 1 of 2 refused stale, read again: /repos/system"])
        self.assertEqual([(p, body) for p, body, _ in out["posts"]], [
            ("/api/repos/runner-merge", {"path": a, "value": "auto", "before": "manual"}),
            ("/api/repos/runner-merge", {"path": b, "value": "auto", "before": "manual"}),
            ("/api/repos/runner-merge", {"path": a, "value": "manual", "before": "auto"})])
        self.assertEqual(out["R"]["after"][-1],
                         f"Switch runner-merge undone · 1 of 2 reversed · not reversed: {b} (its change did not land)")


class TheSecondRound(PageScript):
    """Copilot's review of f06cbf5 on PR #50: the review-declaration proposal and the schedule's words."""

    LAST = '{\n  "copilot_review": {\n    "automatic_deep": false\n  },\n  "severity_floor": "high"\n}\n'

    def proposal(self, text, calls):
        doc = json.loads(json.dumps(self.doc))
        row = next(r for r in doc["repos"] if r["path"] == self.ids["checkout"])
        body = json.loads(text) if text.startswith("{") and text.rstrip().endswith("}") and "!" not in text else {}
        row["review"] = {"declaration": text, "severity_floor": body.get("severity_floor"),
                         "automatic_deep": (body.get("copilot_review") or {}).get("automatic_deep")}
        return self.run_page(calls + """
R.texts = pending.map(p => p.diff.filter(d => d[0] !== 'del' && d[0] !== 'hd').map(d => d[1]).join('\\n'));
R.parsed = R.texts.map(t => { try { return JSON.parse(t); } catch (e) { return 'invalid: ' + e.message; } });""",
                             doc=doc, search=f"?repo={self.ids['checkout']}")

    def test_removing_the_last_property_leaves_valid_json(self):
        out = self.proposal(self.LAST, "propose('severity_floor', '');")
        self.assertEqual(out["R"]["parsed"], [{"copilot_review": {"automatic_deep": False}}])

    def test_adding_or_changing_the_floor_and_flipping_automatic_deep_leave_valid_json(self):
        bare = '{\n  "copilot_review": {\n    "automatic_deep": false\n  }\n}\n'
        out = self.proposal(bare, "propose('severity_floor', 'medium');")
        self.assertEqual(out["R"]["parsed"], [{"copilot_review": {"automatic_deep": False}, "severity_floor": "medium"}])
        out = self.proposal(self.LAST, "propose('severity_floor', 'low');")
        self.assertEqual(out["R"]["parsed"], [{"copilot_review": {"automatic_deep": False}, "severity_floor": "low"}])
        out = self.proposal(self.LAST, "propose('automatic_deep', 'true');")
        self.assertEqual(out["R"]["parsed"], [{"copilot_review": {"automatic_deep": True}, "severity_floor": "high"}])

    def test_the_diff_shows_only_the_lines_that_change(self):
        out = self.proposal(self.LAST, "propose('severity_floor', ''); R.diff = pending[0].diff;")
        self.assertEqual([d for d in out["R"]["diff"] if d[0] in ("add", "del")],
                         [["del", "  },"], ["del", '  "severity_floor": "high"'], ["add", "  }"]])

    def test_a_review_declaration_that_is_not_json_is_not_proposed(self):
        out = self.proposal('{\n  "severity_floor": "high",\n}\n!', "propose('severity_floor', 'low');")
        self.assertEqual(out["R"]["parsed"], [])
        self.assertEqual(out["toasts"][-1][0], "Not proposed: the review declaration is not a JSON object")

    def test_a_floor_change_gives_the_sd_db_declare_line_not_a_pull_request(self):
        """sd:5159: the policy lives on the repo row; the page drafts `sd-db repo declare`, the pack's one writer."""
        out = self.proposal(self.LAST, "propose('severity_floor', 'low'); R.p = [pending[0].type, cmd('declaration.write').cli(pending[0])];")
        path = self.ids["checkout"]
        self.assertEqual(out["R"]["p"], ["declaration change", "sd-db repo declare "
                                         f"'{path}' review '{{\"copilot_review\":{{\"automatic_deep\":false}},\"severity_floor\":\"low\"}}'"])

    def test_write_posts_the_declaration_with_the_reading_it_changes_and_undo_writes_the_reading_back(self):
        """sd:5887: Write runs what the sd-db repo declare line does; the server refuses a row that changed since `before`."""
        out = self.proposal(self.LAST, """propose('severity_floor', 'low'); C.run(cmd('declaration.write'), pending[0]); await flush();
R.toast = lastToast().msg; R.left = pending.length; await lastToast().undo(); await flush();""".replace("C.run", "OUT.posts.length = 0; C.run"))
        path, after = self.ids["checkout"], {"copilot_review": {"automatic_deep": False}, "severity_floor": "low"}
        self.assertEqual([b for p, b, _ in out["posts"] if p == "/api/actions/run"],
                         [{"action": "declaration.write", "path": path, "kind": "review", "value": after, "before": self.LAST},
                          {"action": "declaration.write", "path": path, "kind": "review", "value": json.loads(self.LAST),
                           "before": json.dumps(after, separators=(",", ":"))}])
        self.assertEqual((out["R"]["toast"], out["R"]["left"]), ("Written · Review rules · Blocking severity: low", 0))

    def test_write_sends_the_latest_reading_and_keeps_a_refused_one(self):
        """sd:5958: Write sent the declaration the proposal was made on as `before`, so after a reread the server refused it
        (409), and the proposal was already gone from the list."""
        newer = '{\n  "categories": ["x"],\n  "severity_floor": "high"\n}\n'
        path = self.ids["checkout"]
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["repos"] if r["path"] == path)["review"] = {"declaration": self.LAST, "severity_floor": "high",
                                                                         "automatic_deep": False}
        out = self.run_page("""propose('severity_floor', 'low');
DOC0.repos.find(r => r.path === repoParam).review = %s; await reread();
OUT.posts.length = 0; C.run(cmd('declaration.write'), pending[0]); await flush();
R.left = pending.map(p => p.key);""" % json.dumps({"declaration": newer, "severity_floor": "high"}),
                            doc=doc, search=f"?repo={path}",
                            answer="(p, b) => p === '/api/actions/run' ? [409, {error: 'the declaration changed since this reading'}] : [200, {}]")
        self.assertEqual([(b["before"], b["value"]) for p, b, _ in out["posts"] if p == "/api/actions/run"],
                         [(newer, {"categories": ["x"], "severity_floor": "low"})])
        self.assertEqual(out["R"]["left"], ["severity_floor"])

    # sd:5958 review, class pass: every way Write's `before` and `after` could come from two readings. Each case reads the
    # page's posts: the pair sent is always the declaration a line was drafted on and that line's result.
    def write_after(self, calls, review=None, answer="(p, b) => [200, {}]"):
        path = self.ids["checkout"]
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["repos"] if r["path"] == path)["review"] = review or {"declaration": self.LAST, "severity_floor": "high",
                                                                                   "automatic_deep": False}
        out = self.run_page(calls + "\nR.left = pending.map(p => p.key);", doc=doc, search=f"?repo={path}", hash="#view=repos",
                            answer=answer)
        return [(b["before"], b["value"]) for p, b, _ in out["posts"] if p == "/api/actions/run"], out

    def test_a_cleared_declaration_is_not_written_back(self):
        # The blocker: the cleared declaration did not parse, so the line kept its old `after`, and Write sent it with
        # before '' -- the server took it and brought back the removed categories.
        sent, out = self.write_after("""DOC0.repos.find(r => r.path === repoParam).review = { declaration: '{"categories": ["x"], "severity_floor": "high"}', severity_floor: 'high' };
await reread(); propose('severity_floor', 'low');
DOC0.repos.find(r => r.path === repoParam).review = { declaration: null }; await reread();
R.cli = cmd('declaration.write').cli(pending[0]); C.run(cmd('declaration.write'), pending[0]); await flush();""")
        self.assertEqual(sent, [("", {"severity_floor": "low"})])
        self.assertNotIn("categories", out["R"]["cli"])

    def test_a_declaration_that_stops_parsing_turns_write_off(self):
        sent, out = self.write_after("""propose('severity_floor', 'low');
DOC0.repos.find(r => r.path === repoParam).review = { declaration: '{"severity_floor": "high",}' }; await reread();
R.when = cmd('declaration.write').when(pending[0]); R.cli = cmd('declaration.write').cli(pending[0]);
C.run(cmd('declaration.write'), pending[0]); await flush();""")
        self.assertEqual(sent, [])
        self.assertIn("not a JSON object now", out["R"]["when"])
        self.assertNotIn("automatic_deep", out["R"]["cli"])
        self.assertEqual(out["R"]["left"], ["severity_floor"])

    def test_the_starter_writes_onto_no_declaration(self):
        # The starter had no `after` at all: reviewDiff refused the empty declaration, and Write threw.
        click = "ELS['view-repos'].listeners.click.forEach(f => f({ target: { closest: s => s === '[data-create-review]' ? {} : null } }));"
        sent, out = self.write_after(click + "\nC.run(cmd('declaration.write'), pending[0]); await flush();", review={"declaration": None})
        self.assertEqual(sent, [("", {"severity_floor": "high"})])
        self.assertEqual(out["R"]["left"], [])

    def test_the_second_line_writes_on_the_first_lines_reading(self):
        # The first Write lands and the page reads again; the second line is drafted on that reading and sends it.
        answer = """(p, b) => { if (p === '/api/actions/run') DOC0.repos.find(r => r.path === repoParam).review =
  { declaration: JSON.stringify(b.value, null, 2), severity_floor: b.value.severity_floor, automatic_deep: b.value.copilot_review?.automatic_deep };
  return [200, {}]; }"""
        sent, out = self.write_after("""propose('severity_floor', 'low'); propose('automatic_deep', 'true');
C.run(cmd('declaration.write'), pending.find(p => p.key === 'severity_floor')); await flush();
C.run(cmd('declaration.write'), pending[0]); await flush();""", answer=answer)
        both = {"copilot_review": {"automatic_deep": True}, "severity_floor": "low"}
        self.assertEqual(sent, [(self.LAST, both), (json.dumps(both, indent=2), both)])
        self.assertEqual(out["R"]["left"], [])

    def test_two_pending_edits_draft_lines_that_each_carry_both(self):
        """sd:5159 review: each line replaced the whole declaration from the first reading, so running the second dropped
        the first edit. Every pending line now carries every pending edit; withdrawing one drops it from the rest."""
        out = self.proposal(self.LAST, """propose('severity_floor', 'low'); propose('automatic_deep', 'true');
R.both = pending.map(p => JSON.parse(cmd('declaration.write').cli(p).split(' review ')[1].slice(1, -1)));
withdraw('severity_floor', true); R.left = pending.map(p => JSON.parse(cmd('declaration.write').cli(p).split(' review ')[1].slice(1, -1)));""")
        both = {"copilot_review": {"automatic_deep": True}, "severity_floor": "low"}
        self.assertEqual(out["R"]["both"], [both, both])
        self.assertEqual(out["R"]["left"], [{"copilot_review": {"automatic_deep": True}, "severity_floor": "high"}])

    def test_a_new_reading_redrafts_the_pending_line_on_it(self):
        """sd:5159 review, class pass: a reread while a line waits must not leave it writing back the older declaration."""
        doc = json.loads(json.dumps(self.doc))
        row = next(r for r in doc["repos"] if r["path"] == self.ids["checkout"])
        row["review"] = {"declaration": '{\n  "categories": ["x"],\n  "severity_floor": "high"\n}', "severity_floor": "high"}
        out = self.proposal(self.LAST, """propose('severity_floor', 'low');
DOC0.repos.find(r => r.path === repoParam).review = %s; await reread();
R.line = JSON.parse(cmd('declaration.write').cli(pending[0]).split(' review ')[1].slice(1, -1));""" % json.dumps(row["review"]))
        self.assertEqual(out["R"]["line"], {"categories": ["x"], "severity_floor": "low"})

    def settings_page(self, review, body):
        doc = json.loads(json.dumps(self.doc))
        next(r for r in doc["repos"] if r["path"] == self.ids["checkout"])["review"] = review
        return self.run_page(body, doc=doc, search=f"?repo={self.ids['checkout']}", hash="#view=repos")

    def test_a_row_without_a_declaration_offers_the_starter_line(self):
        click = "ELS['view-repos'].listeners.click.forEach(f => f({ target: { closest: s => s === '[data-create-review]' ? {} : null } }));"
        out = self.settings_page({"declaration": None}, f"R.html = ELS['view-repos'].html; {click} R.cli = cmd('declaration.write').cli(pending[0]);")
        self.assertIn("Add declaration", out["R"]["html"])
        self.assertNotIn("sd-review.json", out["R"]["html"])
        self.assertTrue(out["R"]["cli"].endswith(f"repo declare '{self.ids['checkout']}' review '{{\"severity_floor\":\"high\"}}'"),
                        out["R"]["cli"])

    def test_a_pending_starter_folds_into_a_declaration_a_new_reading_finds(self):
        click = "ELS['view-repos'].listeners.click.forEach(f => f({ target: { closest: s => s === '[data-create-review]' ? {} : null } }));"
        found = {"declaration": '{\n  "categories": ["x"]\n}', "severity_floor": None, "automatic_deep": None}
        out = self.settings_page({"declaration": None}, f"""{click}
DOC0.repos.find(r => r.path === repoParam).review = {json.dumps(found)}; await reread();
R.line = JSON.parse(cmd('declaration.write').cli(pending[0]).split(' review ')[1].slice(1, -1));""")
        self.assertEqual(out["R"]["line"], {"categories": ["x"], "severity_floor": "high"})

    def test_an_unread_declaration_offers_no_add(self):
        out = self.settings_page({"declaration": None, "error": "not read: the database is locked"}, "R.html = ELS['view-repos'].html;")
        self.assertIn("not read: the database is locked", out["R"]["html"])
        self.assertNotIn("Add declaration", out["R"]["html"])

    def test_a_date_pinned_calendar_names_its_date(self):
        out = self.run_page("""R.got = [human([{ Day: 15, Hour: 3, Minute: 0 }]), human([{ Month: 1, Day: 1, Hour: 0, Minute: 5 }]),
  human([{ Day: 1, Minute: 30 }]), human([{ Hour: 3, Minute: 0 }]), human([{ Minute: 30 }]), human([{ Weekday: 1, Hour: 9, Minute: 0 }]),
  human([{ Minute: 0 }, { Minute: 30 }])];""")
        self.assertEqual(out["R"]["got"], ["day 15 03:00", "Jan 1 00:05", "day 1 *:30", "daily 03:00", "hourly at :30", "Mon 09:00",
                                           "every 30 min"])


# A view's rows as the browser finds them: `#view-<v> tr[data-id]` and `#view-<v> tr[data-id="<id>"]` read the view's drawn
# markup, so the page's first-row and row-still-drawn checks run as they do in a browser.
ROWS_QUERY = r"""
var pageQuery = document.querySelector;
document.querySelector = sel => { const m = sel.match(/^#view-(\w+) tr\[data-id(?:="([^"]*)")?\]$/); if (!m) return pageQuery(sel);
  const ids = [...(((ELS['view-' + m[1]] || {}).html) || '').matchAll(/<tr data-id="([^"]*)"/g)].map(x => x[1]);
  const id = m[2] === undefined ? ids[0] : ids.find(x => x === m[2]); return id === undefined ? null : { dataset: { id } }; };
"""
# Every read of the document waits until the test answers it, newest first; a read started after a write sees the write.
HELD = r"""
const held = [], copy = d => JSON.parse(JSON.stringify(d));
let wrote = false, after = copy(DOC0);
ANSWER = (path, body) => { if (path !== '/api/management') { wrote = true; return [200, {}]; }
  const doc = wrote ? after : copy(DOC0); return new Promise(ok => held.push(() => ok([200, doc]))); };
const answerNewestFirst = async () => { while (held.length) { held.pop()(); await flush(); } };
"""


class TheSharedReader(PageScript):
    """sd:2485: the page reads through shell.read (read.js, sd:2418), so it gets the reader's guards."""

    WHAT = "repos, sessions, services and jobs"

    def test_only_the_newest_read_draws(self):
        out = self.run_page(HELD + """after.jobs = after.jobs.filter(j => j.name !== 'quiet'); wrote = true;
load(); await flush(); wrote = false; load(); await flush();
held[0](); await flush(); held[1](); await flush();
R.sched = ELS['view-schedules'].html; R.type = C.get('cron:quiet').type;""")
        self.assertIn(">quiet<", out["R"]["sched"], "the newest read lists quiet")
        self.assertEqual(out["R"]["type"], "job")
        out = self.run_page(HELD + """after.jobs = after.jobs.filter(j => j.name !== 'quiet');
load(); await flush(); wrote = true; load(); await flush();
held[1](); await flush(); held[0](); await flush();
R.sched = ELS['view-schedules'].html; R.type = C.get('cron:quiet').type;""")
        self.assertNotIn(">quiet<", out["R"]["sched"], "an older read answered last and drew over the newest")
        self.assertEqual(out["R"]["type"], "not listed")

    def test_a_row_the_read_no_longer_lists_runs_no_command(self):
        out = self.run_page("""DOC0.jobs = DOC0.jobs.filter(j => j.name !== 'quiet'); await load(); await flush();
R.obj = [C.get('cron:quiet').type, C.get('cron:quiet').label]; R.kept = C.get('cron:nightly-sync').type;""")
        self.assertEqual(out["R"]["obj"], ["not listed", "quiet (no longer listed)"])
        self.assertEqual(out["R"]["kept"], "job")

    def test_a_selection_whose_row_is_gone_moves_to_the_first_row(self):
        out = self.run_page("""selectRow('cron:quiet', false); R.before = ELS.details.html.includes('<h2>quiet</h2>');
DOC0.jobs = DOC0.jobs.filter(j => j.name !== 'quiet'); await load(); await flush();
R.details = ELS.details.html; R.row = ROW;""", search="?view=schedules", extra=ROWS_QUERY)
        self.assertTrue(out["R"]["before"])
        self.assertIn("<h2>nightly-sync</h2>", out["R"]["details"], "Details still shows a job the read no longer lists")
        self.assertEqual(out["R"]["row"], "cron:nightly-sync")

    def test_the_first_read_selects_the_linked_row_in_its_view_only(self):
        """A guard: the link's row is the reader's first-read ask; a row of another view is not selected."""
        out = self.run_page("R.details = ELS.details.html;", search="?view=schedules&row=cron:quiet",
                            extra=ROWS_QUERY + "\nROW = 'cron:quiet';\n")
        self.assertIn("<h2>quiet</h2>", out["R"]["details"])
        out = self.run_page("R.details = ELS.details.html;", search="?view=schedules&row=wt:abandoned",
                            extra=ROWS_QUERY + "\nROW = 'wt:abandoned';\n")
        self.assertIn("<h2>nightly-sync</h2>", out["R"]["details"])

    def test_a_reread_waits_for_the_read_its_write_overtook(self):
        """The write barrier: a read in flight when the write lands answers with the old row, so it must not be the last."""
        path = self.ids["checkout"]
        out = self.run_page(HELD + f"""after.repos.find(r => r.path === {json.dumps(path)}).runner_merge = 'auto';
load(); await flush();
C.run(cmd('repo.runner-merge'), C.get('repo:{path}')); await flush();
await answerNewestFirst();
R.merge = C.get('repo:{path}').row.merge; R.toasts = OUT.toasts.map(t => t.msg);""")
        self.assertEqual(out["R"]["merge"], "auto", "the page settled on a read that started before its write landed")
        self.assertEqual(out["R"]["toasts"], [f"runner_merge auto · {path}"])

    def test_a_failed_reread_after_a_write_keeps_the_rows_and_says_the_write_landed(self):
        path = self.ids["checkout"]
        out = self.run_page(f"""ANSWER = p => p === '/api/management' ? [500, {{error: 'database is busy'}}] : [200, {{}}];
C.run(cmd('repo.runner-merge'), C.get('repo:{path}')); await flush();
R.sched = ELS['view-schedules'].html; R.type = C.get('cron:quiet').type;""")
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertEqual(out["states"][-1]["text"],
                         f"The change landed; {self.WHAT} were not read again: database is busy. Reload reads them.")
        self.assertIn("nightly-sync", out["R"]["sched"])
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertEqual(out["toasts"][-1], [f"runner_merge auto · {path}", True])

    def test_a_failed_load_clears_the_rows_and_their_commands(self):
        out = self.run_page("""ANSWER = () => [500, {error: 'database is busy'}]; await load(); await flush();
R.sched = ELS['view-schedules'].html; R.repos = ELS['view-repos'].html; R.type = C.get('cron:quiet').type;
R.lamps = ['repos', 'sessions', 'schedules'].map(v => new RegExp(`data-view="${v}" data-state="([a-z]+)"`).exec(ELS.annunciator.html)[1]);""")
        self.assertEqual(out["states"][-1], {"kind": "error", "source": "/api/management",
                                             "text": "Repos, sessions, services and jobs were not read: database is busy. Reload retries it."})
        self.assertNotIn("nightly-sync", out["R"]["sched"])
        self.assertIn("database is busy", out["R"]["sched"])
        self.assertNotIn("busy</button>", out["R"]["repos"])
        self.assertEqual(out["R"]["type"], "not listed")
        self.assertEqual(out["R"]["lamps"], ["unknown"] * 3)
        self.assertEqual(out["attention"][-1]["state"], "unknown")


class TheViews(PageScript):
    """sd:5080: the five areas are the shell's views (PAGE_VIEWS); the page draws no tabs of its own."""

    def test_the_areas_are_page_views_and_the_page_has_no_tabs_of_its_own(self):
        page = (V2 / "management.html").read_text(encoding="utf-8")
        self.assertNotRegex(page, r'role="tab|class="subviews|class="sv"')
        out = self.run_page("R.views = window.PAGE_VIEWS.map(v => [v.id, v.label, v.key || null]);")
        # pack sd:7115: the Ship lane view folded into Queue (its Overview since pack sd:7217).
        self.assertEqual(out["R"]["views"], [["repos", "Repos", None], ["sessions", "Sessions", None],
                                             ["deploys", "Deploys", None], ["schedules", "Schedules", "c"],
                                             ["machine", "Machine", None]])
        self.assertNotIn('id="view-lane"', page)

    def test_a_lamp_shows_its_view_and_says_its_state(self):
        out = self.run_page("""R.before = ELS.annunciator.html;
ELS.annunciator.listeners.click.forEach(f => f({ target: { closest: s => s === 'button.cell' ? { id: '', dataset: { view: 'schedules' } } : null } }));
await flush(); R.shown = OUT.shown.slice(); R.view = view; R.after = ELS.annunciator.html;""")
        self.assertEqual(re.findall(r'data-view="(\w+)" data-state="(\w+)" aria-pressed="(\w+)"', out["R"]["before"]),
                         [("repos", "ok", "true"), ("sessions", "caution", "false"),
                          ("deploys", "unknown", "false"), ("schedules", "warning", "false"),
                          ("machine", "unknown", "false")])
        self.assertNotIn('id="refresh"', out["R"]["before"])
        self.assertEqual((out["R"]["shown"], out["R"]["view"]), (["schedules"], "schedules"))
        self.assertIn('data-view="schedules" data-state="warning" aria-pressed="true"', out["R"]["after"])

    def test_an_older_view_query_opens_its_view_and_the_address_drops_it(self):
        out = self.run_page("R.view = view;", search="?view=deploys")
        self.assertEqual((out["R"]["view"], out["shown"]), ("deploys", ["deploys"]))
        self.assertFalse(any("view=" in q for q in out["qs"]), out["qs"])
        out = self.run_page("R.view = view;", search="?view=deploys", hash="#view=sessions")
        self.assertEqual((out["R"]["view"], out["shown"]), ("sessions", []))


#: A Machine read as `reports_screen.view("toolbox")` answers it: sd_tile.py's sections and the rows that want attention.
MACHINE = {"title": "Toolbox", "rows": [{"what": "cron <script>x</script> exited 7", "detail": "daily · last log 02:15"}],
           "sections": [{"figures": [["jobs", "2"], ["failing", "1"]]}, {"heading": "Cron jobs"},
                        {"table": {"columns": [["Job", "text"], ["Exit", "num"]], "search": "filter jobs",
                                   "rows": [[{"text": "nightly-sync", "title": "Sync the fleet"}, {"text": "7", "num": True}]]}},
                        {"listing": ["<b>drift</b> in brew"]}]}


class TheMachineView(PageScript):
    """pack sd:7527: Operations > Resources > Toolbox is Management's Machine view, read when it opens."""

    ANSWER = """(path) => path === '/api/management/machine' ? (OUT.machine = (OUT.machine || 0) + 1, MACHINE_ANSWER) : [200, {}]"""

    def test_the_view_reads_the_toolbox_tile_once_and_draws_it_escaped(self):
        out = self.run_page("""R.before = OUT.machine || 0;
window.shell.view.show('machine'); await flush(); await flush();
R.html = ELS['view-machine'].html; R.reads = OUT.machine; R.lamps = ELS.annunciator.html;
window.shell.view.show('repos'); await flush(); window.shell.view.show('machine'); await flush(); R.again = OUT.machine;""",
                            answer=self.ANSWER, extra=f"var MACHINE_ANSWER = [200, {json.dumps(MACHINE)}];\n")
        html = out["R"]["html"]
        self.assertEqual((out["R"]["before"], out["R"]["reads"], out["R"]["again"]), (0, 1, 1))
        self.assertIn("<h3>Needs attention</h3>", html)
        self.assertIn("cron &lt;script&gt;x&lt;/script&gt; exited 7", html)
        self.assertNotIn("<script", html)
        self.assertIn("<dt>failing</dt><dd>1</dd>", html)
        self.assertIn('<th scope="col" class="n">Exit</th>', html)
        self.assertIn('<td title="Sync the fleet">nightly-sync</td><td class="n">7</td>', html)
        self.assertIn("<li>&lt;b&gt;drift&lt;/b&gt; in brew</li>", html)
        self.assertIn('data-view="machine" data-state="caution"', out["R"]["lamps"])

    def test_a_failed_read_says_why_and_the_address_opens_the_view(self):
        failed = [503, {"error": "Toolbox could not be observed: resource inspection was stopped at its budget"}]
        out = self.run_page("await flush(); R.html = ELS['view-machine'].html; R.lamps = ELS.annunciator.html;",
                            answer=self.ANSWER, extra=f"var MACHINE_ANSWER = {json.dumps(failed)};\n", hash="#view=machine")
        self.assertIn("The machine was not read", out["R"]["html"])
        self.assertIn("stopped at its budget", out["R"]["html"])
        self.assertIn('data-view="machine" data-state="unknown"', out["R"]["lamps"])

    def test_the_route_answers_the_toolbox_tile_or_its_refusal(self):
        page = next(entry for entry in v2.registry.PAGES if entry.section == "Management")
        route = next(api for api in page.api if api.path == "/api/management/machine")
        with patch("sd_dashboard.reports_screen.collect", return_value=MACHINE) as collect:
            self.assertEqual(route.read(None), MACHINE)
        collect.assert_called_once_with("toolbox")
        with patch("sd_dashboard.reports_screen.collect", side_effect=ValueError("budget")):
            self.assertEqual(route.read(None), (503, {"error": "Toolbox could not be observed: budget"}))


class TheResearchFilter(PageScript):
    """pack sd:7527: Operations > Resources > Research is the Repos filter ?research=yes|no."""

    ANSWER = """(path) => path === '/api/management/research' ? (OUT.research = (OUT.research || 0) + 1, RESEARCH_ANSWER) : [200, {}]"""

    def research(self, search, answer=None):
        checkout = str(Path(self.tmp.name).resolve() / "checkout")
        doc = answer or [200, {"title": "Research", "paths": [checkout], "rows": [],
                               "sections": [{"heading": "Documents"}, {"table": {"columns": [["Document", "text"]], "search": "",
                                                                                "rows": [[{"text": "<i>Reading</i>"}]]}}]}]
        out = self.run_page("await flush(); R.html = ELS['view-repos'].html; R.reads = OUT.research || 0;",
                            answer=self.ANSWER, search=search, extra=f"var RESEARCH_ANSWER = {json.dumps(doc)};\n")
        return checkout, out["R"], re.findall(r'data-repo="([^"]+)"', out["R"]["html"])

    def test_yes_keeps_the_research_checkouts_and_draws_their_documents(self):
        checkout, got, rows = self.research("?research=yes")
        self.assertEqual((rows, got["reads"]), ([checkout], 1))
        self.assertIn("<h3>Research documents</h3>", got["html"])
        self.assertIn("<td>&lt;i&gt;Reading&lt;/i&gt;</td>", got["html"])
        self.assertIn('data-f="research" data-v="yes" aria-pressed="true"', got["html"])

    def test_no_keeps_the_rest_and_a_failed_read_says_no_row_is_either(self):
        checkout, got, rows = self.research("?research=no")
        self.assertTrue(rows)
        self.assertNotIn(checkout, rows)
        self.assertNotIn("Research documents", got["html"])
        _, got, rows = self.research("?research=yes", [503, {"error": "Research could not be observed: budget"}])
        self.assertEqual(rows, [])
        self.assertIn("The research checkouts were not read", got["html"])
        self.assertIn("Research could not be observed: budget", got["html"])

    def test_without_the_filter_nothing_is_read(self):
        _, got, rows = self.research("")
        self.assertEqual(got["reads"], 0)
        self.assertGreater(len(rows), 1)

    def test_read_again_reads_the_research_checkouts_again_while_the_filter_is_on(self):
        """Lead review of 960414d82: a failed read stayed cached, so the filter stayed unusable until a reload."""
        checkout = str(Path(self.tmp.name).resolve() / "checkout")
        good = {"title": "Research", "paths": [checkout], "rows": [], "sections": []}
        answer = ("(path) => path === '/api/management/research' ? (OUT.research = (OUT.research || 0) + 1,"
                  " OUT.research === 1 ? [503, {error: 'Research could not be observed: budget'}] : [200, GOOD]) : [200, {}]")
        body = ("await flush(); R.first = ELS['view-repos'].html; window.PAGE_AGAIN.run(); await flush(); await flush();"
                " R.html = ELS['view-repos'].html; R.reads = OUT.research || 0;")
        got = self.run_page(body, answer=answer, search="?research=yes", extra=f"var GOOD = {json.dumps(good)};\n")["R"]
        self.assertIn("The research checkouts were not read", got["first"])
        self.assertEqual(got["reads"], 2)
        self.assertEqual(re.findall(r'data-repo="([^"]+)"', got["html"]), [checkout])
        self.assertIn("<h3>Research documents</h3>", got["html"])
        got = self.run_page("await flush(); window.PAGE_AGAIN.run(); await flush(); R.reads = OUT.research || 0;",
                            answer=self.ANSWER, extra=f"var RESEARCH_ANSWER = {json.dumps([200, good])};\n")["R"]
        self.assertEqual(got["reads"], 0)

    def test_the_route_answers_the_research_tile(self):
        page = next(entry for entry in v2.registry.PAGES if entry.section == "Management")
        route = next(api for api in page.api if api.path == "/api/management/research")
        with patch("sd_dashboard.reports_screen.collect", return_value={"paths": []}) as collect:
            self.assertEqual(route.read(None), {"paths": []})
        collect.assert_called_once_with("research")


class TheMinuteMarks(PageScript):
    """sd:2385 (Copilot 65f0869637eb on PR 50): 'every N min' only when the marks are evenly spaced around the hour."""

    def test_an_uneven_minute_list_names_its_marks(self):
        out = self.run_page("""R.got = [human([{ Minute: 5 }, { Minute: 30 }]), human([{ Minute: 0 }, { Minute: 15 }, { Minute: 45 }]),
  human([{ Minute: 30 }, { Minute: 0 }]), human([{ Minute: 0 }, { Minute: 20 }, { Minute: 40 }]), human([{ Minute: 10 }, { Minute: 10 }])];""")
        self.assertEqual(out["R"]["got"], ["hourly at :05, :30", "hourly at :00, :15, :45", "every 30 min", "every 20 min", "hourly at :10"])

    def test_a_job_with_no_calendar_is_manual(self):
        """sd:4965: `JOB_SCHEDULE="manual"` installs a plist with no StartCalendarInterval; it runs only when started."""
        out = self.run_page("R.got = [human([]), nextRun([])];")
        self.assertEqual(out["R"]["got"], ["manual", ""])


class TheLastRun(PageScript):
    """sd:2210 and sd:2209: Schedules shows each job's stamped last run; the lane strip shows the archive refresh."""

    def test_each_schedule_row_shows_its_stamped_last_run_or_why_not(self):
        out = self.run_page("R.sched = ELS['view-schedules'].html;", search="?view=schedules")
        sched = out["R"]["sched"]
        self.assertIn(">Last run<", sched)
        self.assertIn('data-k="last"><time class="rel" datetime="2026-09-06T02:16:30Z"', sched)
        self.assertIn("exit 7", sched)
        self.assertIn('<span class="g-unknown" aria-hidden="true">▨</span> not stamped', sched)

    def test_a_run_with_no_end_says_so_and_a_running_one_says_since(self):
        doc = json.loads(json.dumps(self.doc))
        for job in doc["jobs"]:
            job["last_run"] = {"state": "open", "started": "2026-09-06T02:15:00Z", "ended": None, "exit": None, "reason": ""}
            if job["name"] == "quiet":
                job["state"] = "running"
        out = self.run_page("R.sched = ELS['view-schedules'].html;", doc=doc, search="?view=schedules")
        sched = out["R"]["sched"]
        self.assertIn('<span class="g-caution" aria-hidden="true">▲</span> no end', sched)
        self.assertIn('<span class="g-queued" aria-hidden="true">◌</span> since', sched)

    def test_the_details_name_the_stamp_or_the_reason_there_is_none(self):
        out = self.run_page("selectRow('cron:nightly-sync', false); R.failed = ELS.details.html; selectRow('cron:quiet', false); R.quiet = ELS.details.html;",
                            search="?view=schedules")
        self.assertIn("<dt>Last run</dt>", out["R"]["failed"])
        self.assertIn('datetime="2026-09-06T02:15:00Z"', out["R"]["failed"])
        self.assertIn('datetime="2026-09-06T02:16:30Z"', out["R"]["failed"])
        self.assertIn("exit 7", out["R"]["failed"])
        self.assertIn("logs/.nightly-sync.stamp", out["R"]["failed"])
        self.assertIn("no run stamp", out["R"]["quiet"])

    def test_sessions_draw_no_runner_assignments(self):
        """sd:5472: Sessions read assignment rows the idle author runner wrote; pack sd:7115 moved the live ones to Queue."""
        out = self.run_page("R.sess = ELS['view-sessions'].html;", search="?view=sessions")
        sess = out["R"]["sess"]
        for text in ("Runner assignments", "runner-h", "data-hist", "Assignment history"):
            self.assertNotIn(text, sess)
        self.assertIn('id="claude-h"', sess)

    def test_the_merge_setting_reads_lane_merge_in_the_column_the_group_the_tally_the_chip_and_the_details(self):
        """sd:5472: the label only; the setting key runner_merge and the merge: token stay."""
        out = self.run_page("""R.repos = ELS['view-repos'].html;
selectRow('repo:' + DOC0.repos[0].path, false); R.details = ELS.details.html;""", search="?view=repos&merge=manual")
        repos = out["R"]["repos"]
        self.assertRegex(repos, r'data-sort="merge"[^>]*>Lane merge')
        self.assertIn('aria-label="Lane merge"', repos)
        self.assertIn("lane merge auto", repos)
        self.assertIn('data-f="merge" data-v="manual" aria-pressed="true"', repos)
        self.assertIn('data-f="merge"', repos)
        self.assertIn("<dt>Lane merge</dt>", out["R"]["details"])
        for key in ("repos", "details"):
            self.assertNotIn("Runner merge", out["R"][key])
            self.assertNotIn("runner merge", out["R"][key])


class TheJobLogAndCancel(PageScript):
    """sd:3388: Management reads a job's log, cancels a running job, and filters and pages services."""

    def running_doc(self):
        jobs = JobsBackend(self.tmp.name, jobs=[("busy", "running", None, None), ("quiet", "idle", 0, None)])
        (jobs.cron_root / "logs" / "busy.log").write_text("[busy] started\nstep 2 <of> 3\n")
        return management_screen.document(self.connection, now=NOW, fleet=fleet, jobs=jobs, services=NoServices())

    CANCEL = "C.run(cmd('jobs.cancel'), C.get('cron:busy')); await flush();"

    def test_a_running_job_cancels_through_its_route_with_its_revision_after_a_confirm_that_names_it(self):
        doc = self.running_doc()
        revision = next(j for j in doc["jobs"] if j["name"] == "busy")["revision"]
        out = self.run_page("R.why = cmd('jobs.cancel').consequence(C.get('cron:busy')); R.cli = cmd('jobs.cancel').cli(C.get('cron:busy'));"
                            + self.CANCEL + " C.run(cmd('jobs.cancel'), C.get('cron:quiet')); await flush();",
                            doc=doc, answer="(p, b) => [200, {request: {status: 'accepted', message: 'launchctl accepted the request'}}]")
        self.assertEqual(out["confirms"], ["jobs.cancel"])
        self.assertIn("busy (PID 7)", out["R"]["why"])
        self.assertEqual(out["R"]["cli"], "sd jobs cancel busy")
        self.assertEqual([(p, b) for p, b, _ in out["posts"]], [("/api/jobs/busy/cancel", {"revision": revision})])
        self.assertEqual(out["toasts"][0], ["Cancel sent · busy", False])
        self.assertEqual(out["toasts"][1], ["off: only a currently running job can cancel", False])
        self.assertEqual(out["gets"], ["/api/management", "/api/management"])

    def test_a_refusal_says_not_changed_and_any_other_failure_says_the_outcome_is_not_known(self):
        doc = self.running_doc()
        cases = {
            "(p, b) => [409, {error: 'state changed; refresh before trying again'}]": "busy not changed: state changed; refresh before trying again",
            "(p, b) => [400, {error: 'only a currently running job can cancel'}]": "busy not changed: only a currently running job can cancel",
            "(p, b) => [502, {error: 'the hub did not answer'}]": "Not known whether busy was cancelled: the hub did not answer.",
            "(p, b) => Promise.reject(new TypeError('Load failed'))": "Not known whether busy was cancelled: Load failed.",
            "(p, b) => [200, {request: {status: 'failed', message: 'launchctl refused the request (exit 3)'}}]":
                "busy not cancelled: launchctl refused the request (exit 3)",
            "(p, b) => [200, {request: {status: 'unknown', message: 'launchctl timed out; the request outcome is unknown'}}]":
                "Not known whether busy was cancelled: launchctl timed out; the request outcome is unknown",
        }
        for answer, said in cases.items():
            with self.subTest(said=said):
                out = self.run_page(self.CANCEL, doc=doc, answer=answer)
                self.assertTrue(out["toasts"][0][0].startswith(said), out["toasts"])
                # A 400 wrote nothing and reads nothing; every other answer reads the jobs again.
                self.assertEqual(len(out["gets"]), 1 if said.endswith("can cancel") else 2, out["gets"])

    def test_the_details_show_the_end_of_the_log_and_the_last_request(self):
        doc = self.running_doc()
        busy = next(j for j in doc["jobs"] if j["name"] == "busy")
        busy["last_request"] = {"action": "cancel", "status": "accepted", "at": "2026-09-06T02:20:00Z", "who": "dashboard",
                                "message": "launchctl accepted the request"}
        quiet = next(j for j in doc["jobs"] if j["name"] == "quiet")
        quiet["log"] = {"state": "unread", "text": "", "cut": False, "reason": "the log is not a regular file"}
        out = self.run_page("selectRow('cron:busy', false); R.busy = ELS.details.html; selectRow('cron:quiet', false); R.quiet = ELS.details.html;"
                            " R.shown = cmd('jobs.log').run(C.get('cron:busy')); R.after = ELS.details.html;", doc=doc, search="?view=schedules")
        self.assertIn('<pre class="code" tabindex="0" aria-label="busy log">[busy] started\nstep 2 &lt;of&gt; 3\n</pre>', out["R"]["busy"])
        self.assertIn("local-cron-jobs/logs/busy.log", out["R"]["busy"])
        self.assertIn("<dt>Last request</dt><dd>cancel · accepted · dashboard · 2026-09-06T02:20:00Z · launchctl accepted the request</dd>",
                      out["R"]["busy"])
        self.assertIn("The log was not read", out["R"]["quiet"])
        self.assertIn("the log is not a regular file", out["R"]["quiet"])
        self.assertIn("<dt>Last request</dt><dd>none recorded</dd>", out["R"]["quiet"])
        self.assertEqual(out["R"]["shown"], "busy's log is in Details")
        self.assertIn("busy log", out["R"]["after"])

    def services(self, n):
        """The document with n services: every third one failed, the rest running."""
        rows = [{"name": f"svc-{i:02d}", "label": f"local.example.svc-{i:02d}", "scope": "user", "domain": "gui/501",
                 "category": "tool", "state": "failed" if i % 3 == 0 else "running", "pid": None, "last_exit": None,
                 "last_signal": None, "revision": "0" * 64, "capabilities": {}} for i in range(n)]
        return dict(self.doc, services=rows)

    def test_services_page_through_the_shells_pager_and_keep_the_page_in_the_url(self):
        click = "ELS['view-deploys'].listeners.click[0]({ target: { closest: s => s === %s ? { dataset: %s } : null } });"
        out = self.run_page("R.a = ELS['view-deploys'].html;" + click % ("'.list-pager [data-page]'", "{ page: '2' }")
                            + " R.b = ELS['view-deploys'].html;", doc=self.services(30), search="?view=deploys")
        self.assertIn('<span class="range">1–25 of 30</span>', out["R"]["a"])
        self.assertIn('<span class="range">26–30 of 30</span>', out["R"]["b"])
        self.assertIn("svc-29", out["R"]["b"])
        self.assertNotIn("svc-00", out["R"]["b"])
        self.assertIn("page=2", out["qs"])

    def test_services_filter_by_a_state_chip_and_by_any_column(self):
        click = "ELS['view-deploys'].listeners.click[0]({ target: { closest: s => s === '[data-f=\"state\"]' ? { dataset: { f: 'state', v: 'failed' } } : null } });"
        out = self.run_page("R.a = ELS['view-deploys'].html;" + click + " R.b = ELS['view-deploys'].html; R.box = (OUT.shift ?? null);",
                            doc=self.services(30), search="?view=deploys")
        self.assertEqual(re.findall(r'data-f="state" data-v="(\w+)"', out["R"]["a"]), ["failed", "running"])
        self.assertIn("10 of 30", out["R"]["b"])
        self.assertEqual(out["R"]["box"], "state:failed")
        self.assertEqual(out["qs"][-1], "q=state%3Afailed")
        out = self.run_page("R.a = ELS['view-deploys'].html;", doc=self.services(30), search="?view=deploys&q=gui/501 svc-1")
        self.assertIn("10 of 30", out["R"]["a"])
        self.assertIn("svc-19", out["R"]["a"])


class TheOperationsRows(PageScript):
    """pack sd:7517: the rows Operations > Sessions and > Services showed, drawn on Management alone."""

    def test_sessions_list_each_worktree_with_its_branch_and_path_abandoned_first(self):
        out = self.run_page("R.sess = ELS['view-sessions'].html; selectRow('tree:0', false); R.det = ELS.details.html;",
                            search="?view=sessions")
        sess = out["R"]["sess"]
        self.assertIn("<dt>Path</dt><dd class=\"mono\">/wt/gone</dd>", out["R"]["det"])
        table = sess[sess.index('id="wts-h"'):]
        self.assertIn("<th scope=\"col\">Branch</th>", table)
        rows = re.findall(r'<tr data-id="tree:(\d+)">(.*?)</tr>', table)
        self.assertEqual([index for index, _ in rows], ["0", "1"])
        self.assertIn("/wt/gone", rows[0][1])
        self.assertIn("/wt/here", rows[1][1])
        self.assertIn("abandoned", rows[0][1])
        self.assertIn("system", rows[0][1])
        self.assertIn('<td class="d mono">gone</td>', rows[0][1])

    def test_a_cut_process_table_is_a_floor_and_never_says_nothing_runs(self):
        """The guard sessions_screen held (Codex review of PR #500): a cut table cannot support an absence."""
        cases = [([], "≥ 0 running", "The table was cut at 64 KB, so no command below the cut was read."),
                 ([{"pid": "4242", "elapsed": "01:02", "command": "sd-review --json"}], "≥ 1 running", "sd-review --json")]
        for procs, tally, said in cases:
            with self.subTest(rows=len(procs)):
                doc = json.loads(json.dumps(self.doc))
                doc["sessions"].update(processes=list(procs), processes_truncated=True)
                sess = self.run_page("R.sess = ELS['view-sessions'].html;", doc=doc, search="?view=sessions")["R"]["sess"]
                self.assertIn(tally, sess)
                self.assertIn(said, sess)
                self.assertNotIn("No sd-* command is running.", sess)

    def test_a_missing_root_is_said_and_counts_no_worktrees(self):
        doc = json.loads(json.dumps(self.doc))
        doc["sessions"].update(root_exists=False, root="/nowhere", registered=0, abandoned=0, unreadable=0, worktrees=[])
        sess = self.run_page("R.sess = ELS['view-sessions'].html;", doc=doc, search="?view=sessions")["R"]["sess"]
        self.assertIn("The checkout root /nowhere does not exist", sess)
        self.assertNotIn("0 live worktrees", sess)

    def test_an_unreadable_worktree_is_neither_abandoned_nor_live(self):
        doc = json.loads(json.dumps(self.doc))
        doc["sessions"].update(registered=3, abandoned=1, unreadable=1)
        sess = self.run_page("R.sess = ELS['view-sessions'].html;", doc=doc, search="?view=sessions")["R"]["sess"]
        self.assertIn("1 live worktrees", sess)
        self.assertIn("1 unreadable", sess)

    def test_a_worktree_with_no_readable_path_says_so(self):
        doc = json.loads(json.dumps(self.doc))
        doc["sessions"]["worktrees"][0]["path"] = ""
        out = self.run_page("R.sess = ELS['view-sessions'].html;", doc=doc, search="?view=sessions")
        self.assertIn("(unreadable registration)", out["R"]["sess"])

    def test_a_services_details_show_its_last_request(self):
        doc = dict(self.doc, services=[{
            "name": "svc", "label": "local.example.svc", "scope": "user", "domain": "gui/501", "category": "tool",
            "state": "running", "pid": 1, "last_exit": 0, "last_signal": None, "revision": "0" * 64, "capabilities": {},
            "last_request": {"action": "restart", "status": "accepted", "at": "2026-09-06T02:20:00Z", "who": "dashboard",
                             "message": "launchctl accepted the request"}}])
        out = self.run_page("selectRow('svc:local.example.svc', false); R.det = ELS.details.html;", doc=doc,
                            search="?view=deploys")
        self.assertIn("<dt>Last request</dt><dd>restart · accepted · dashboard · 2026-09-06T02:20:00Z"
                      " · launchctl accepted the request</dd>", out["R"]["det"])



class TheSatellites(PageScript):
    """sd:5108: Deploys lists each satellite's last refresh result and sends Refresh satellites."""

    def doc_with(self, *satellites, request=None):
        return dict(self.doc, satellites={"request": request, "satellites": list(satellites)})

    @staticmethod
    def sat(host, state, **result):
        return {"host": host, "state": state, "reason": "", "result": result or None}

    def test_deploys_draws_each_result_and_the_lamp_counts_a_failure(self):
        request = {"id": "ab12cd34ef56ab78", "at": "2026-09-06T02:00:00Z"}
        doc = self.doc_with(self.sat("sat-a", "done", request=request["id"], attempt=1, started="2026-09-06T02:01:00Z",
                                     finished="2026-09-06T02:04:00Z", exit=0, step="machine-setup", line="done: 0 failures"),
                            self.sat("sat-b", "done", request=request["id"], attempt=2, started="2026-09-06T02:01:00Z",
                                     finished="2026-09-06T02:09:00Z", exit=1, step="machine-setup", line="1 stage failed: cron"),
                            {"host": "sat-c", "state": "unread", "reason": "Expecting value", "result": None},
                            request=request)
        out = self.run_page("R.dep = ELS['view-deploys'].html; R.lamps = ELS.annunciator.html; selectRow('sat:sat-b', false); R.det = ELS.details.html;",
                            doc=doc, search="?view=deploys")
        dep = out["R"]["dep"]
        self.assertIn("3 satellites", dep)
        self.assertIn('<tr data-id="sats:request">', dep)
        self.assertIn("ab12cd34ef56ab78", dep)
        self.assertIn("machine-setup: done: 0 failures", dep)
        self.assertIn("done · attempt 2", dep)
        self.assertIn("machine-setup: 1 stage failed: cron", dep)
        self.assertIn("Expecting value", dep)
        self.assertIn('data-view="deploys" data-state="warning"', out["R"]["lamps"])
        self.assertIn("■ 1 failed", out["R"]["lamps"])
        self.assertIn("<dt>Exit</dt><dd>1</dd>", out["R"]["det"])
        self.assertIn("1 stage failed: cron", out["R"]["det"])

    def test_a_satellites_read_that_failed_is_unknown_with_its_reason(self):
        doc = dict(self.doc, satellites=None, sources=dict(self.doc["sources"], satellites="the hub did not answer"))
        out = self.run_page("R.dep = ELS['view-deploys'].html;", doc=doc, search="?view=deploys")
        self.assertIn("Satellites were not read", out["R"]["dep"])
        self.assertIn("the hub did not answer", out["R"]["dep"])

    def test_refresh_satellites_confirms_then_posts_and_toasts_after_the_write(self):
        out = self.run_page("R.why = cmd('sats.refresh').consequence(C.get('sats:request'));"
                            " C.run(cmd('sats.refresh'), C.get('sats:request')); R.early = OUT.toasts.length; await flush();",
                            doc=self.doc_with(), search="?view=deploys",
                            answer="(p, b) => [200, {id: 'ab12cd34ef56ab78', at: '2026-09-06T02:00:00Z'}]")
        self.assertEqual(out["confirms"], ["sats.refresh"])
        self.assertIn("updates to the hub's versions", out["R"]["why"])
        self.assertEqual([(p, b) for p, b, _ in out["posts"]], [("/api/satellites/refresh", {})])
        self.assertEqual(out["R"]["early"], 0, "the toast came before the write landed")
        self.assertEqual(out["toasts"][0], ["Refresh requested · ab12cd34ef56ab78", False])
        self.assertEqual(out["gets"], ["/api/management", "/api/management"])


class TheSharedUpload(PageScript):
    """sd:5405: Deploys shows the bucket's console link, the current upload link and Regenerate."""

    LINK = {"url": "https://example-bucket.s3.us-east-1.amazonaws.com/", "expiry": "2026-09-13T11:00:00Z",
            "signed": "2026-09-06T11:00:00+00:00", "temporary": None,
            "fields": {"key": "inbox/${filename}", "x-amz-signature": "f00dfeed"},
            "curl": "curl --form-string 'key=inbox/${filename}' --form-string 'x-amz-signature=f00dfeed' -F file=@/path/to/file "
                    "https://example-bucket.s3.us-east-1.amazonaws.com/",
            "public": "https://example-bucket.s3.us-east-1.amazonaws.com/inbox/<file name>"}

    def up(self, **over):
        up = {"config": "/config/shared-upload/.env", "set_up": True, "remedy": "", "prefix": "inbox/", "lifetime": "168h",
              "bucket": "example-bucket", "region": "us-east-1",
              "console": "https://s3.console.aws.amazon.com/s3/buckets/example-bucket?region=us-east-1&tab=objects",
              "public": "https://example-bucket.s3.us-east-1.amazonaws.com/inbox/<file name>",
              "link": self.LINK, "link_state": "current", "link_reason": ""}
        return dict(self.doc, upload={**up, **over})

    def test_a_current_link_shows_its_console_link_curl_line_and_expiry(self):
        out = self.run_page("R.dep = ELS['view-deploys'].html; selectRow('upload:link', false); R.det = ELS.details.html;",
                            doc=self.up(), search="?view=deploys")
        dep, det = out["R"]["dep"], out["R"]["det"]
        self.assertIn('<h2 id="up-h">Shared upload', dep)
        self.assertIn('href="https://s3.console.aws.amazon.com/s3/buckets/example-bucket?region=us-east-1&amp;tab=objects"', dep)
        self.assertIn('<tr data-id="upload:link">', dep)
        self.assertIn('<time class="rel" datetime="2026-09-13T11:00:00Z" data-future data-long></time>', dep)
        self.assertIn("Links handed out before keep working until they expire.", dep)
        self.assertIn('data-copy="curl --form-string', dep)
        self.assertIn("x-amz-signature=f00dfeed", dep)
        self.assertIn("inbox/&lt;file name&gt;", dep)
        self.assertIn("<dt>Config</dt><dd class=\"mono\">/config/shared-upload/.env</dd>", det)
        self.assertIn("deactivate the uploader's access key in IAM", det)

    def test_not_set_up_is_a_plain_line_naming_the_config_path(self):
        remedy = "SHARED_UPLOAD_BUCKET is not set. Export it, or copy local-shared-upload/.env.example to /config/shared-upload/.env and fill it in."
        out = self.run_page("R.dep = ELS['view-deploys'].html; R.has = !!C.get('upload:link');",
                            doc=self.up(set_up=False, remedy=remedy, link=None, link_state="none"), search="?view=deploys")
        self.assertIn(f'<p class="empty">Not set up. {remedy}</p>', out["R"]["dep"])
        self.assertNotIn('data-id="upload:link"', out["R"]["dep"])
        self.assertFalse(out["R"]["has"])

    def test_an_unread_link_and_an_unread_source_are_unknown(self):
        out = self.run_page("R.dep = ELS['view-deploys'].html;", search="?view=deploys",
                            doc=self.up(link=None, link_state="unread", link_reason="the dashboard.shared_upload_link row does not read; Regenerate replaces it"))
        self.assertIn('<td class="g g-unknown">▨<span class="sr">unread</span></td>', out["R"]["dep"])
        self.assertIn("row does not read; Regenerate replaces it", out["R"]["dep"])
        doc = dict(self.doc, upload=None, sources=dict(self.doc["sources"], upload="UnicodeDecodeError: utf-8"))
        out = self.run_page("R.dep = ELS['view-deploys'].html;", doc=doc, search="?view=deploys")
        self.assertIn("Shared upload was not read", out["R"]["dep"])
        self.assertIn("UnicodeDecodeError: utf-8", out["R"]["dep"])

    def test_regenerate_confirms_then_posts_and_toasts_the_expiry_not_the_link(self):
        out = self.run_page("R.why = cmd('upload.regenerate').consequence(C.get('upload:link'));"
                            " C.run(cmd('upload.regenerate'), C.get('upload:link')); R.early = OUT.toasts.length; await flush();",
                            doc=self.up(), search="?view=deploys",
                            answer="(p, b) => [200, {prefix: 'inbox/', expiry: '2026-09-13T11:05:00Z', temporary: null}]")
        self.assertEqual(out["confirms"], ["upload.regenerate"])
        self.assertIn("Links already handed out keep working until they expire.", out["R"]["why"])
        self.assertEqual([(p, b) for p, b, _ in out["posts"]], [("/api/shared-upload/regenerate", {})])
        self.assertEqual(out["R"]["early"], 0, "the toast came before the write landed")
        self.assertEqual(out["toasts"][0], ["Upload link signed · expires 2026-09-13 11:05 UTC", False])
        self.assertEqual(out["gets"], ["/api/management", "/api/management"])


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = ("management", "Management", "/management",
                                 ("/api/management", "/api/management/machine", "/api/management/research", "/api/repos/land/preview", "/api/repos/land",
                                  "/api/repos/standard/preview", "/api/repos/standard", "/api/claude-sessions/end"))
