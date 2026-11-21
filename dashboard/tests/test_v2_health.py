"""The v2 Health page (sd:2115).

What this slice promises: `/fleet-health` answers under the shared policy and
loads its script before `shell.js` (`/health` stays the service's own check);
`/api/health` is every area of the design, in its order, each saying whether a
reader covers it. Worktrees come from the fleet child's registrations, grouped
per checkout. An area with no
reader, or whose reader failed, is unknown on the page and names what it does
not read, never a clean lamp. Ports is Operations > Ports' reader with its
counts and warnings; Protection is `protection.rows` as a matrix, one column
per repository, with a table carrying the same cells, and an unread
repository shows no cell. Disk and Branches (sd:2202, sd:2204) read this
machine through `health_collectors`, each inside its budget: the collector
tests below run real git against fixture repositories, with `df` and `du`
stubbed where the answer depends on the machine; every other test fills both
seams with the fixtures below, so no test reads the operator's disks.

`health.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` defines, with the document above as its fetch
answer. The browser half -- focus, the look at 375 px -- is a manual check
recorded on the pull request.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import (
    health_collectors,
    health_screen,
    runtime,
    server,
    v2,
)
from sd_db import credentials, upsert_repo, writes
from sd_db.writes import record_state, snooze
from support import NOW, ScreenCase
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_shell_shared import cell_grammar
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
#: The real reader, before `Collectors` replaces it for every test.
REAL_READ_SERVICES = getattr(health_screen, "read_services", None)
HEALTH_JS = (V2 / "static" / "health.js").read_text(encoding="utf-8")
HEALTH_HTML = (V2 / "health.html").read_text(encoding="utf-8")
SNOOZE_JS = (V2 / "static" / "snooze.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")
COMMANDS_JS = (V2 / "static" / "commands.js").read_text(encoding="utf-8")

TREES = [
    {"repo": "group/alpha", "name": "a", "path": "/work/wt-a", "branch": "feat/a", "live": False, "state": "abandoned"},
    {"repo": "group/alpha", "name": "b", "path": "/work/wt-b", "branch": "fix/b", "live": False, "state": "abandoned"},
    {"repo": "beta", "name": "c", "path": "", "branch": "?", "live": False, "state": "unknown"},
    {"repo": "beta", "name": "d", "path": "/work/wt-d", "branch": "main", "live": True, "state": "live"},
]


def fleet_of(trees, root="/checkouts", exists=True):
    def read(area):
        assert area == "sessions", area
        return {"root": root, "rootExists": exists, "worktrees": trees, "processes": [], "processes_error": "",
                "processes_truncated": False, "abandoned": 0, "counts": {}}
    return read


def ports_snapshot():
    """One configured port listening, one uninspected, and one listener no service claims."""
    return {"complete": True,
            "services": [{"name": "svc-a", "ports": ["9010"], "state": "running", "mark": "+"},
                         {"name": "svc-b", "ports": ["9020"], "state": "stopped", "mark": "."}],
            "listeners": {"9010": {"state": "listening", "source": "lsof", "holder": "svc", "pid": 42},
                          "9020": {"state": "unknown"}},
            "observed": {"complete": False, "ports": {"5000": [{"pid": 7, "command": "other", "address": "*:5000"}]}},
            # sd:5840: the same docker ps's running containers, which Health > Containers lists.
            "containers": {"read": True, "reason": "", "rows": [
                {"name": "poc-db-1", "image": "ex/pgvector:pg16", "status": "Up 2 hours (healthy)", "project": "poc",
                 "workdir": "/checkouts/group/experiment",
                 "mounts": ["/host_mnt/checkouts/group/experiment/var/setup", "poc_dbdata", "/elsewhere/cache"]},
                {"name": "loose", "image": "ex/loose", "status": "Up 5 minutes", "project": "", "workdir": "", "mounts": []}]}}


def flag(check, value, flagged):
    return {"id": check, "value": value, "flagged": flagged, "gap": f"{check} sentence"}


PROTECTION = [
    {"repo": "/checkouts/alpha", "slug": "group/alpha", "status": "protected", "observed_at": "2026-09-05T08:00:00Z",
     "default_branch": "main", "reason": None, "gaps": [{"id": "reviews", "gap": "no approving review is required"}],
     "merge_settings": [flag("squash_message", "PR_TITLE / PR_BODY", False), flag("required_check", "sd/local-gate", True)]},
    {"repo": "/checkouts/beta", "slug": None, "status": "unprotected", "observed_at": "2026-09-05T08:00:00Z",
     "default_branch": "main", "reason": None,
     "gaps": [{"id": "unprotected", "gap": "main has no protection"}, {"id": "required_checks", "gap": "no check is required"}],
     "merge_settings": []},
    {"repo": "/checkouts/gamma", "slug": "group/gamma", "status": "unknown", "observed_at": "2026-09-04T08:00:00Z",
     "default_branch": None, "reason": "token does not reach it", "gaps": [], "merge_settings": [flag("rebase_merge", "allowed", True)]},
    {"repo": "/checkouts/delta", "slug": "group/delta", "status": "protected", "observed_at": "2026-09-05T08:00:00Z",
     "default_branch": "main", "reason": None, "gaps": [], "merge_settings": []},
]


# `protection.rows` since sd:2205 carries `managed` and `alerts`; none of these is managed.
PROTECTION = [{**repo, "managed": False, "alerts": None} for repo in PROTECTION]


def protection_of(rows):
    return lambda connection: rows


#: Managed repositories with alerts as the nightly sync stores them (sd:2205, sd:2206); beta is not managed, and
#: alpha-copy is a second checkout of group/alpha.
ALERTED = [
    {**PROTECTION[0], "managed": True, "alerts": {
        "dependabot": {"open": 100, "more": True, "severity": {"critical": 1, "low": 99}},
        "secret_scanning": {"visibility": "public", "setting": "enabled", "open": 1, "more": False}}},
    {**PROTECTION[0], "repo": "/checkouts/alpha-copy", "managed": True, "alerts": PROTECTION[0].get("alerts")},
    {**PROTECTION[1], "managed": False, "alerts": None},
    {**PROTECTION[2], "managed": True, "alerts": None},
    {**PROTECTION[3], "managed": True, "alerts": {
        "dependabot": {"open": 2, "more": False, "severity": {"medium": 2}},
        "secret_scanning": {"visibility": "public", "setting": "disabled"}}},
    {"repo": "/checkouts/epsilon", "slug": "group/epsilon", "status": "protected", "observed_at": "2026-09-04T08:00:00Z",
     "reason": None, "managed": True, "alerts": {
         "dependabot": {"reason": "API HTTP 403; retry on a later collection"},
         "secret_scanning": {"visibility": "public", "setting": None, "reason": "security_and_analysis not shown"}}},
    {"repo": "/checkouts/zeta", "slug": "group/zeta", "status": "protected", "observed_at": "2026-09-05T08:00:00Z",
     "reason": None, "managed": True, "alerts": {"dependabot": {"archived": True}, "secret_scanning": {"visibility": "private"}}},
]


GIB = 1024 ** 2
DISK = {
    "volumes": [
        {"filesystem": "/dev/disk3s5", "size_kb": 1000 * GIB, "used_kb": 850 * GIB, "avail_kb": 150 * GIB, "capacity": 85,
         "mount": "/System/Volumes/Data"},
        {"filesystem": "/dev/disk5s1", "size_kb": 2000 * GIB, "used_kb": 800 * GIB, "avail_kb": 1200 * GIB, "capacity": 40,
         "mount": "/Volumes/store"},
    ],
    "build": {"checked": 4, "unread": [],
              "merged": [{"path": "/work/wt-old", "head": "0" * 40, "branch": "feat/done", "repo": "/checkouts/alpha",
                          "dirs": ["target", "node_modules"], "status": ""},
                         {"path": "/work/wt-dirty", "head": "1" * 40, "branch": "feat/wip", "repo": "/checkouts/alpha",
                          "dirs": ["target"], "status": "?? notes.txt\n"}],
              "revision": "b" * 64},
    "storage": [{"root": "/Volumes/store/repo-storage", "error": "",
                 "folders": [{"path": "/Volumes/store/repo-storage/alpha", "kb": 5 * GIB},
                             {"path": "/Volumes/store/repo-storage/beta", "kb": 2 * GIB},
                             {"path": "/Volumes/store/repo-storage/gamma", "kb": GIB},
                             {"path": "/Volumes/store/repo-storage/delta", "kb": 10 * 1024}]}],
    "refused": [], "config": "/config/project-dashboard/disk.conf",
}
BRANCHES = {
    "repos": 2, "no_default": ["/checkouts/beta"], "unread": [],
    "merged": [{"repo": "/checkouts/alpha", "path": "/checkouts/alpha",
                "deletable": [["feat/one", "2026-08-01"], ["feat/two", "2026-09-01"], ["fix/three", "2026-07-15"]],
                "checked_out": [["feat/live", "/work/wt-live"]], "revision": "a" * 64}],
}
#: The doctor's lines as `parse_doctor` reads them: one FAIL, one WARN with its continuation, passes, a skip and context.
DOCTOR_OUT = """== doctor
  ok      profile recorded: hub
  FAIL    Xcode CLT missing — run: xcode-select --install
  WARN    brew doctor has warnings — run: brew doctor
  ok      fnm (v24.1.0)
  --      docker not checked (not installed, no services in this profile)
  WARN    set here but not in shell-env.example: EXAMPLE_TOKEN
          add the name (value change-me) so a rebuild gets it

  folders without a filled .env (.env.example present, .env absent):
    --      local-redis/.env  (copy .env.example)

  1 hard failure(s)
"""
DOCTOR = health_collectors.parse_doctor(DOCTOR_OUT, 1)
#: The checkout's HEAD in every test: no test reads this checkout's git (sd:3707).
CHECKOUT = "c" * 40


def service_of(label, state, *, pid=None, last_exit=None, scope="user", category="service", start="", restart="",
               reason=None, rev="1"):
    """One `sd_db.services.inventory` entry; `start` and `restart` are the reason each is off, "" when it is on."""
    return {"id": label, "label": label, "name": label.rsplit(".", 1)[-1], "domain": "gui/501" if scope == "user" else "system",
            "scope": scope, "category": category, "state": state, "pid": pid, "last_exit": last_exit, "last_signal": None,
            "reason": reason, "capabilities": {action: {"allowed": not why, "reason": why} for action, why in (
                ("start", start), ("stop", ""), ("restart", restart))},
            "last_request": None, "control_note": "", "revision": rev * 64}


#: What every test reads unless it hands `services` an inventory: one running service, so no count elsewhere moves.
QUIET_SERVICES = {"services": [service_of("com.example.alpha", "running", pid=101, start="service is already running")],
                  "revision": "0" * 64}
PROTECTED = "protected to preserve dashboard and tailnet access"
SERVICES = {"services": [
    service_of("com.example.alpha", "running", pid=101, start="service is already running"),
    service_of("com.example.beta", "unloaded", restart="service is already unloaded; use Start to load it",
               reason="service is installed but not loaded", rev="2"),
    service_of("com.example.gamma", "failed", last_exit=78, rev="3"),
    service_of(runtime.LABEL, "running", pid=202, start=PROTECTED, restart=PROTECTED, rev="4"),
    service_of("com.example.daily", "idle", category="scheduled"),
    service_of("com.example.daemon", "running", pid=1, scope="system"),
], "revision": "0" * 64}

CLEAN_DOCTOR = health_collectors.parse_doctor("== doctor\n  ok      profile recorded: hub\n\n  no hard failures\n", 0)
CLEAN_DISK = {**DISK, "volumes": [DISK["volumes"][1]], "build": {"checked": 4, "merged": [], "unread": []}}
CLEAN_BRANCHES = {"repos": 2, "merged": [], "no_default": [], "unread": []}
#: `merged_worktrees`' answer (sd:5105): one tree the lane merged that can go, and one a process works in.
WORKTREES = {"trees": [
    {"path": "/work/wt-merged", "head": "d" * 40, "branch": "feat/merged", "repo": "/checkouts/alpha",
     "repo_path": "/checkouts/alpha", "item": 5105, "lane_branch": "feat/merged", "kept": ""},
    {"path": "/work/wt-busy", "head": "f" * 40, "branch": "feat/busy", "repo": "/checkouts/alpha",
     "repo_path": "/checkouts/alpha", "item": 5104, "lane_branch": "feat/busy", "kept": "a process works in /work/wt-busy"},
], "revision": "e" * 64}
REAL_DISK_SCAN, REAL_BRANCH_SCAN, REAL_DOCTOR_SCAN = (health_collectors.disk_scan, health_collectors.branch_scan,
                                                     health_collectors.doctor_scan)


def scan_of(scan):
    return lambda connection, **_: scan


class HeldClock:
    """`time` whose `monotonic` stays at its first reading: a walk that spends no budget (sd:2667)."""

    def __init__(self):
        self.held = None

    def monotonic(self):
        if self.held is None:
            self.held = time.monotonic()
        return self.held

    def __getattr__(self, name):
        return getattr(time, name)


class Collectors:
    """Disk and Branches read the fixtures above unless a test names its own reader: no test reads this machine."""

    def setUp(self):
        super().setUp()
        # A scan left running by an earlier test, or its kept answer, is not this test's.
        health_screen._SCANS.clear()
        self.addCleanup(health_screen._SCANS.clear)
        for name, scan in (("disk_scan", DISK), ("branch_scan", BRANCHES)):
            patcher = patch.object(health_collectors, name, scan_of(scan))
            patcher.start()
            self.addCleanup(patcher.stop)
        # No test runs the machine's doctor (sd:3393): the default reader reads this fixture.
        patcher = patch.object(health_collectors, "doctor_scan", lambda **_: CLEAN_DOCTOR)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No test runs launchctl or reads this checkout's HEAD (sd:3707): a test hands `services` its inventory.
        for name, value in (("read_services", lambda connection, backend=None: backend or QUIET_SERVICES),):
            patcher = patch.object(health_screen, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(runtime, "checkout_commit", lambda: CHECKOUT)
        patcher.start()
        self.addCleanup(patcher.stop)


def git(*args, cwd=None):
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-09-05T10:00:00Z", "GIT_COMMITTER_DATE": "2026-09-05T10:00:00Z",
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.test",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.test", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(["git", *args], cwd=cwd, check=True, env=env, capture_output=True, text=True).stdout.strip()


def cloned(path: Path) -> Path:
    """A repository on main with origin/HEAD at its tip, the way a clone knows its default branch."""
    git("init", "-q", "-b", "main", str(path))
    git("-C", str(path), "commit", "-q", "--allow-empty", "-m", "first")
    git("-C", str(path), "update-ref", "refs/remotes/origin/main", "HEAD")
    git("-C", str(path), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return path


class TheDocument(Collectors, ScreenCase):
    """`health_screen.document` from the fleet child."""

    def doc(self, trees=TREES, **kwargs):
        return health_screen.document(self.connection, now=NOW, fleet=kwargs.get("fleet") or fleet_of(trees),
                                      ports=kwargs.get("ports") or ports_snapshot,
                                      protection=kwargs.get("protection") or protection_of(PROTECTION),
                                      disk=kwargs.get("disk"), branches=kwargs.get("branches"),
                                      doctor=kwargs.get("doctor"), **{key: kwargs[key] for key in ("services", "served_commit")
                                                                      if key in kwargs})

    def test_every_design_area_is_there_in_order_and_says_whether_a_reader_covers_it(self):
        areas = self.doc()["areas"]
        self.assertEqual([(a["id"], a["read"]) for a in areas],
                         [("disk", True), ("cred", True), ("wt", True),
                          ("br", True), ("dep", True), ("sec", True), ("ports", True), ("prot", True), ("svc", True),
                          ("ctr", True), ("mir", True), ("doc", True), ("aws", True), ("stat", True)])
        for area in areas:
            if area["id"] not in ("wt", "br", "ports", "prot", "svc", "ctr", "cred", "dep", "sec", "mir", "doc", "aws", "stat"):
                self.assertTrue(area["missing"], f"{area['id']} names nothing it does not read")
            if not area["read"]:
                self.assertEqual((area["rows"], area["error"], area["source"]), ([], "", None))

    def test_the_badge_is_the_loudest_state_of_every_area_s_rows(self):
        # sd:3446: the read's rule, which every page's rail shows from Health's last read.
        doc = self.doc()
        states = [row["state"] for area in doc["areas"] for row in area["rows"]]
        loudest = "warning" if "warning" in states else "caution" if "caution" in states else "ok"
        self.assertEqual((doc["attention"]["state"], doc["attention"]["n"]), (loudest, states.count(loudest) if loudest != "ok" else 0))
        areas = [{"rows": [{"state": "caution"}, {"state": "warning"}, {"state": "caution"}]}, {"rows": [{"state": "unknown"}]}]
        self.assertEqual(health_screen._attention(areas), {"state": "warning", "n": 1, "what": "findings want you"})
        self.assertEqual(health_screen._attention(areas[1:]), {"state": "ok", "n": 0, "what": "findings to watch"})

    def seen(self, row_id, doc=None):
        """The row's fingerprint as the page reads it, which the page posts with its snooze."""
        return next(row["seen"] for area in (doc or self.doc())["areas"] for row in area["rows"] if row["id"] == row_id)

    def services(self, served_commit=CHECKOUT, inventory=SERVICES):
        area = next(area for area in self.doc(services=inventory, served_commit=served_commit)["areas"] if area["id"] == "svc")
        return area, {row["id"]: row for row in area["rows"]}

    def test_services_are_the_long_running_user_agents_with_launchds_state_and_the_controls_sd_services_allows(self):
        """sd:3707: a row per KeepAlive user LaunchAgent; down is caution, a failed exit warning; scheduled and system are not here."""
        area, rows = self.services()
        self.assertEqual(area["error"], "")
        self.assertEqual(list(rows), ["svc:com.example.alpha", "svc:com.example.beta", "svc:com.example.gamma",
                                      f"svc:{runtime.LABEL}"])
        self.assertEqual([(row["state"], row["type"]) for row in rows.values()],
                         [("ok", "service"), ("caution", "service"), ("warning", "service"), ("ok", "dashboard service")])
        beta = rows["svc:com.example.beta"]
        self.assertEqual((beta["what"], beta["detail"], beta["facts"]["Label"], beta["revision"]),
                         ("beta · unloaded", "service is installed but not loaded", "com.example.beta", "2" * 64))
        self.assertEqual(beta["can"], {"start": {"allowed": True, "reason": ""},
                                       "restart": {"allowed": False, "reason": "service is already unloaded; use Start to load it"}})
        self.assertEqual(rows["svc:com.example.gamma"]["detail"], "last exit 78")
        self.assertEqual(rows["svc:com.example.alpha"]["facts"]["PID"], "101")

    def test_a_failed_run_once_or_scheduled_agent_is_a_warning_row_with_no_controls(self):
        """sd:5882: pack sd:5880 makes a run-once agent `startup`; a failed run must not vanish, a healthy one stays off."""
        killed = dict(service_of("com.example.sweep", "failed", category="scheduled", rev="6"),
                      last_signal=9, reason="last run ended in SIGKILL")
        _, rows = self.services(inventory={"services": [
            service_of("com.example.alpha", "running", pid=101, start="service is already running"),
            service_of("com.example.prune", "failed", last_exit=1, category="startup", rev="5"),
            service_of("com.example.boot", "idle", category="startup"), killed,
            service_of("com.example.daily", "idle", category="scheduled"),
            service_of("com.example.sysfail", "failed", last_exit=2, scope="system", category="startup")],
            "revision": "0" * 64})
        self.assertEqual(list(rows), ["svc:com.example.alpha", "svc:com.example.prune", "svc:com.example.sweep"])
        prune, sweep = rows["svc:com.example.prune"], rows["svc:com.example.sweep"]
        self.assertEqual((prune["state"], prune["type"], prune["what"], prune["detail"]),
                         ("warning", "launch agent", "prune · failed", "last exit 1"))
        self.assertEqual((prune["kind"], prune["facts"]["Last exit"]), ("Run-once user LaunchAgent", "1"))
        self.assertNotIn("can", prune)
        self.assertEqual((sweep["state"], sweep["kind"], sweep["detail"]),
                         ("warning", "Scheduled user LaunchAgent", "last run ended in SIGKILL"))

    def test_the_dashboard_row_is_stale_when_it_serves_another_commit_and_gives_the_kickstart_line(self):
        """sd:3707 with sd:3528: the row asks what `dashboard.sh health` asks, and its own service is copy only."""
        _, rows = self.services(served_commit="a" * 40)
        row = rows[f"svc:{runtime.LABEL}"]
        self.assertEqual((row["state"], row["what"]), ("caution", f"{runtime.LABEL.rsplit('.', 1)[-1]} · stale"))
        self.assertEqual(row["detail"], f"served {'a' * 40}, checkout {CHECKOUT}: "
                                        f"launchctl kickstart -k gui/{os.getuid()}/{runtime.LABEL}")
        self.assertEqual(row["kickstart"], f"launchctl kickstart -k gui/{os.getuid()}/{runtime.LABEL}")
        self.assertEqual(row["facts"]["Served commit"], "a" * 40)
        self.assertIn("protected", row["note"])
        # A process that names no commit is not current either.
        self.assertEqual(self.services(served_commit=None)[1][f"svc:{runtime.LABEL}"]["state"], "caution")

    def test_a_checkout_commit_that_cannot_be_read_is_unknown_not_current(self):
        with patch.object(runtime, "checkout_commit", side_effect=runtime.RuntimeRefused("git failed: index.lock")):
            row = self.services()[1][f"svc:{runtime.LABEL}"]
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["detail"], "the checkout's commit was not read: git failed: index.lock")

    def test_a_service_read_that_fails_is_the_area_error_and_no_service_at_all_is_unknown(self):
        def stuck(connection, backend=None):
            raise RuntimeError("launchctl gave no answer: timed out")
        with patch.object(health_screen, "read_services", stuck):
            area = next(area for area in self.doc()["areas"] if area["id"] == "svc")
        self.assertEqual((area["error"], area["rows"]), ("launchctl gave no answer: timed out", []))
        _, rows = self.services(inventory={"services": [], "revision": "0" * 64})
        self.assertEqual([(row["id"], row["state"]) for row in rows.values()], [("svc:none", "unknown")])

    def test_a_launchctl_that_does_not_answer_is_a_reader_error_the_area_settles(self):
        class Stuck:
            def names(self):
                raise subprocess.TimeoutExpired("launchctl", 10)
        with self.assertRaisesRegex(RuntimeError, "launchctl gave no answer"):
            REAL_READ_SERVICES(self.connection, Stuck())

    def test_a_snoozed_row_leaves_its_area_until_its_time_and_then_comes_back(self):
        """sd:1896. The key is the page and the row id; Today's key for the same id hides nothing here."""
        snooze(self.connection, "health:gone:group/alpha", "2026-09-06T15:00:00Z", seen=self.seen("gone:group/alpha"), now=NOW)
        snooze(self.connection, "today:unread:beta", "2026-09-06T15:00:00Z", seen=self.seen("unread:beta"), now=NOW)
        doc = self.doc()
        wt = doc["areas"][2]
        self.assertEqual([row["id"] for row in wt["rows"]], ["unread:beta"])
        self.assertEqual([(row["id"], row["until"], row["state"]) for row in wt["snoozed"]],
                         [("gone:group/alpha", "2026-09-06T15:00:00+00:00", "caution")])
        self.assertEqual(doc["snooze_error"], "")
        self.assertTrue(all(area["snoozed"] == [] for area in doc["areas"] if area["id"] != "wt"))
        later = health_screen.document(self.connection, now="2026-09-06T15:00:01Z", fleet=fleet_of(TREES),
                                       ports=ports_snapshot, protection=protection_of(PROTECTION))
        self.assertEqual({row["id"] for row in later["areas"][2]["rows"]}, {"gone:group/alpha", "unread:beta"})
        self.assertEqual(later["areas"][2]["snoozed"], [])

    def test_a_snoozed_row_whose_problem_changed_shows_again(self):
        """sd:1896 review: `dep:<repo>` names the repository, not its alerts. A snooze of one low alert does not
        hide the critical one that joins it, and the row hides again only if it reads as it did."""
        def alerts(severity):
            found = [{**PROTECTION[0], "managed": True, "observed_at": NOW,
                      "alerts": {"dependabot": {"open": sum(severity.values()), "severity": severity}}}]
            return self.doc(protection=lambda connection: found)
        low = alerts({"low": 1})
        snooze(self.connection, "health:dep:/checkouts/alpha", "2026-09-06T15:00:00Z",
               seen=self.seen("dep:/checkouts/alpha", low), now=NOW)
        self.assertEqual([row["id"] for row in alerts({"low": 1})["areas"][4]["snoozed"]], ["dep:/checkouts/alpha"])
        worse = alerts({"low": 1, "critical": 1})["areas"][4]
        self.assertEqual([(row["id"], row["state"]) for row in worse["rows"]], [("dep:/checkouts/alpha", "warning")])
        self.assertEqual(worse["snoozed"], [])

    def test_an_ignored_row_stays_out_at_any_time_until_its_problem_changes(self):
        """sd:5470: Ignore is a snooze with no end. It still binds to the problem: a worse one shows again."""
        def alerts(severity, now=NOW):
            found = [{**PROTECTION[0], "managed": True, "observed_at": NOW,
                      "alerts": {"dependabot": {"open": sum(severity.values()), "severity": severity}}}]
            return health_screen.document(self.connection, now=now, fleet=fleet_of(TREES), ports=ports_snapshot,
                                          protection=lambda connection: found)
        snooze(self.connection, "health:dep:/checkouts/alpha", writes.SNOOZE_FOREVER,
               seen=self.seen("dep:/checkouts/alpha", alerts({"low": 1})), now=NOW)
        a_year_on = alerts({"low": 1}, now="2027-09-06T12:00:00Z")["areas"][4]
        self.assertEqual([(row["id"], row["until"]) for row in a_year_on["snoozed"]], [("dep:/checkouts/alpha", "forever")])
        self.assertNotIn("dep:/checkouts/alpha", [row["id"] for row in a_year_on["rows"]])
        worse = alerts({"low": 1, "critical": 1})["areas"][4]
        self.assertEqual(([row["id"] for row in worse["rows"]], worse["snoozed"]), (["dep:/checkouts/alpha"], []))

    def test_a_snooze_read_that_fails_shows_every_row_and_says_why(self):
        snooze(self.connection, "health:gone:group/alpha", "2026-09-06T15:00:00Z", seen=self.seen("gone:group/alpha"), now=NOW)
        with patch.object(writes, "snoozed", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.doc()
        self.assertEqual({row["id"] for row in doc["areas"][2]["rows"]}, {"gone:group/alpha", "unread:beta"})
        self.assertEqual((doc["areas"][2]["snoozed"], doc["snooze_error"]), ([], "database is locked"))

    def test_registrations_whose_directory_is_gone_are_one_row_per_checkout(self):
        wt = self.doc()["areas"][2]
        rows = {row["id"]: row for row in wt["rows"]}
        self.assertEqual(set(rows), {"gone:group/alpha", "unread:beta"})
        gone = rows["gone:group/alpha"]
        self.assertEqual((gone["state"], gone["type"], gone["repo_path"]), ("caution", "worktree registrations", "/checkouts/group/alpha"))
        self.assertEqual(gone["what"], "group/alpha: 2 worktrees registered, directory gone")
        self.assertEqual(gone["facts"]["Registered"], "2")
        self.assertEqual(gone["list"], ["/work/wt-a (feat/a)", "/work/wt-b (fix/b)"])
        unread = rows["unread:beta"]
        self.assertEqual((unread["state"], unread["type"], unread["list"]), ("unknown", "unread registrations", ["(unnamed) (?)"]))

    def test_a_fleet_with_every_registration_present_is_one_ok_row(self):
        (row,) = self.doc(trees=[TREES[3]])["areas"][2]["rows"]
        self.assertEqual((row["id"], row["state"], row["type"]), ("wt:ok", "ok", "check"))
        self.assertIn("checked 1 registration in 1 checkout under /checkouts", row["detail"])

    def test_a_reader_that_fails_is_its_area_error_and_the_other_still_answers(self):
        def broken(area):
            raise ValueError("fleet collection was stopped at its budget: sessions")
        areas = self.doc(fleet=broken)["areas"]
        self.assertEqual((areas[2]["error"], areas[2]["rows"]), ("fleet collection was stopped at its budget: sessions", []))
        self.assertEqual((areas[6]["error"], bool(areas[6]["rows"])), ("", True))
        areas = self.doc(fleet=lambda area: {"root": "/checkouts", "worktrees": [{"repo": "x"}]})["areas"]
        self.assertEqual(areas[2]["error"], "fleet collector returned an incomplete sessions document")

    # sd:5840: Containers lists the running containers the Ports read's docker ps saw.
    def containers(self, ports=ports_snapshot):
        with patch.dict(os.environ, {"REPO_ROOT": "/checkouts"}):
            return next(area for area in self.doc(ports=ports)["areas"] if area["id"] == "ctr")

    def test_containers_name_each_running_container_and_the_checkouts_it_mounts(self):
        area = self.containers()
        self.assertEqual(health_screen.VIEW_OF["ctr"], "machine")
        rows = {row["id"]: row for row in area["rows"]}
        self.assertEqual(list(rows), ["ctr:poc-db-1", "ctr:loose"])
        db = rows["ctr:poc-db-1"]
        self.assertEqual((db["state"], db["type"], db["what"]), ("ok", "container", "poc-db-1"))
        self.assertEqual(db["facts"], {"Image": "ex/pgvector:pg16", "Status": "Up 2 hours (healthy)", "Compose project": "poc",
                                       "Working dir": "/checkouts/group/experiment", "Checkouts mounted": "1"})
        self.assertEqual(db["list"], ["/checkouts/group/experiment/var/setup"])
        self.assertEqual(db["list_head"], "Bind mounts under /checkouts")
        self.assertIn("/checkouts/group/experiment/var/setup", db["detail"])
        self.assertEqual(rows["ctr:loose"]["facts"]["Compose project"], "none")
        self.assertEqual(area["extra"], {"running": 2, "holding": 1, "root": "/checkouts"})

    def test_docker_not_read_is_the_containers_error_never_an_empty_area(self):
        def down():
            return {**ports_snapshot(), "containers": {"read": False, "rows": [],
                    "reason": "docker ps failed; the daemon is not running or not reachable"}}
        area = self.containers(ports=down)
        self.assertEqual((area["rows"], area["error"]),
                         ([], "docker ps failed; the daemon is not running or not reachable"))
        lamp = health_screen._lamp(area)
        self.assertEqual(lamp["state"], "unknown")

    def test_no_running_container_is_one_ok_row(self):
        area = self.containers(ports=lambda: {**ports_snapshot(), "containers": {"read": True, "reason": "", "rows": []}})
        self.assertEqual([(row["id"], row["state"]) for row in area["rows"]], [("ctr:none", "ok")])

    def test_ports_and_containers_share_one_collection(self):
        calls = []

        def counted():
            calls.append(1)
            return ports_snapshot()
        areas = {area["id"]: area for area in self.doc(ports=counted)["areas"]}
        self.assertEqual((bool(areas["ports"]["rows"]), bool(areas["ctr"]["rows"])), (True, True))
        self.assertEqual(len(calls), 1, "Ports and Containers ran docker ps twice")

    def test_a_checkout_root_that_does_not_exist_is_not_read_rather_than_healthy(self):
        # The collector answers a missing REPO_ROOT with no worktrees, which would read as "checked 0 registrations".
        worktrees = self.doc(fleet=fleet_of([], root="/checkouts/missing", exists=False))["areas"][2]
        self.assertEqual(worktrees["rows"], [], "a missing checkout root was reported as a reading")
        self.assertEqual(worktrees["error"], "the checkout root /checkouts/missing does not exist; REPO_ROOT names it")

    def test_ports_are_the_operations_rows_with_its_counts_and_warnings(self):
        ports = self.doc()["areas"][6]
        rows = {row["id"]: row for row in ports["rows"]}
        self.assertEqual({key: (row["state"], row["type"], row["port"]) for key, row in rows.items()},
                         {"port:observed:5000": ("queued", "port", "5000"), "port:svc-a:9010": ("ok", "port", "9010"),
                          "port:svc-b:9020": ("unknown", "port", "9020")})
        self.assertEqual(rows["port:observed:5000"]["what"], ":5000 observed only")
        self.assertEqual(rows["port:svc-a:9010"]["facts"]["Process"], "svc (pid 42)")
        self.assertEqual(rows["port:svc-b:9020"]["facts"]["Process"], "Listener inspection unavailable")
        self.assertEqual(ports["extra"]["counts"], {"configured": 2, "listening": 2, "unknown": 1})
        self.assertEqual(ports["extra"]["warnings"], [
            "Listener state is unknown for 1 configured ports.",
            "The wider TCP listener inventory is incomplete or unavailable; positive observations remain visible."])
        self.assertEqual(ports["at"], NOW)

    def test_a_port_scan_over_its_budget_is_the_area_error_not_an_empty_inventory(self):
        def refused():
            raise health_screen.ports_screen.OverBudget("lsof ran past 5 s")
        areas = self.doc(ports=refused)["areas"]
        self.assertEqual(areas[6]["rows"], [])
        self.assertIn("exceeded its collection budget and was stopped rather than waited on: lsof ran past 5 s", areas[6]["error"])
        self.assertEqual(areas[2]["rows"][0]["id"], "gone:group/alpha")
        empty = self.doc(ports=lambda: {"services": [], "complete": False})["areas"][6]
        self.assertEqual(empty["error"], "port inventory is unavailable")

    def test_protection_cells_follow_the_classic_screen_and_an_unread_repo_has_none(self):
        prot = self.doc()["areas"][7]
        rows = {row["id"]: row for row in prot["rows"]}
        self.assertEqual(prot["extra"]["columns"],
                         ["prot:/checkouts/beta", "prot:/checkouts/gamma", "prot:/checkouts/alpha", "prot:/checkouts/delta"])
        self.assertEqual(prot["extra"]["counts"], {"unprotected": 1, "unknown": 1, "protected": 2})
        self.assertEqual(prot["extra"]["reasons"], ["token does not reach it"])
        self.assertEqual(prot["at"], "2026-09-05T08:00:00Z")
        self.assertEqual(len(prot["extra"]["checks"]), 11)
        def cells(key):
            return {cell[0]: cell[1:] for cell in rows[key]["cells"]}
        alpha = cells("prot:/checkouts/alpha")
        self.assertEqual((alpha["Reviews"], alpha["Admins"], alpha["Squash message"], alpha["Required check"], alpha["Rebase merge"]),
                         (["gap", "no approving review is required"], ["ok"], ["ok"],
                          ["gap", "sd/local-gate · required_check sentence"], ["na"]))
        beta = cells("prot:/checkouts/beta")
        self.assertEqual((beta["Checks"], beta["Reviews"], beta["Admins"], beta["Bypass"]),
                         (["gap", "no check is required"], ["ok"], ["na"], ["na"]))
        # Unknown is not protected: no cell at all, not even the merge setting the body carries.
        self.assertEqual(rows["prot:/checkouts/gamma"]["cells"], [])
        self.assertEqual({key: (row["state"], row["what"]) for key, row in rows.items()}, {
            "prot:/checkouts/alpha": ("caution", "group/alpha: 2 gaps"),
            "prot:/checkouts/beta": ("warning", "/checkouts/beta: main is unprotected"),
            "prot:/checkouts/gamma": ("unknown", "group/gamma: protection not read"),
            "prot:/checkouts/delta": ("ok", "group/delta: protected, no gap")})
        self.assertEqual(rows["prot:/checkouts/beta"]["sentence"], "main has no protection")

    def test_an_empty_registry_is_one_unknown_row_not_a_protected_fleet(self):
        (row,) = self.doc(protection=protection_of([]))["areas"][7]["rows"]
        self.assertEqual((row["id"], row["state"]), ("prot:none", "unknown"))

    def test_the_default_protection_reader_shows_an_unobserved_repo_as_unknown(self):
        self.repo("/checkouts/never")
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of([]), ports=ports_snapshot)
        (row,) = doc["areas"][7]["rows"]
        self.assertEqual((row["state"], row["reason"], row["cells"]), ("unknown", "not yet observed", []))

    def test_a_checkout_borrowing_a_siblings_row_names_the_lender(self):
        """sd:1607: a second checkout of one repository reads its sibling's row, and says whose."""
        upsert_repo(self.connection, "/checkouts/widget", remote="git@github.com:example/widget.git")
        upsert_repo(self.connection, "/checkouts/widget-copy", remote="git@github.com:example/widget.git")
        self.connection.execute("INSERT INTO repo_protection (repo, observed_at, status, default_branch, body) "
                                "VALUES ('/checkouts/widget', '2026-10-04T01:00:00Z', 'protected', 'main', '{}')")
        self.connection.commit()
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of([]), ports=ports_snapshot)
        rows = {row["id"]: row for row in doc["areas"][7]["rows"]}
        self.assertEqual(rows["prot:/checkouts/widget-copy"]["status"], "protected")
        self.assertEqual(rows["prot:/checkouts/widget-copy"]["facts"]["Borrowed from"], "/checkouts/widget")
        self.assertNotIn("Borrowed from", rows["prot:/checkouts/widget"]["facts"])

    def area(self, key, **kwargs):
        return next(area for area in self.doc(**kwargs)["areas"] if area["id"] == key)

    def test_dependencies_are_a_row_per_managed_repo_with_open_alerts_and_unread_is_never_clean(self):
        """sd:2205: the stored alerts of managed repositories; a sibling checkout is not counted twice."""
        rows = {row["id"]: row for row in self.area("dep", protection=protection_of(ALERTED))["rows"]}
        self.assertEqual({key: (row["state"], row["what"]) for key, row in rows.items()}, {
            "dep:/checkouts/alpha": ("warning", "group/alpha: 100+ open Dependabot alerts"),
            "dep:/checkouts/delta": ("caution", "group/delta: 2 open Dependabot alerts"),
            "dep:unread": ("unknown", "2 managed repos: dependencies not read"),
        })
        self.assertEqual(rows["dep:/checkouts/alpha"]["detail"], "1 critical · 99 low · severity past the first page not read")
        self.assertEqual(rows["dep:/checkouts/alpha"]["cli"],
                         "gh api --paginate 'repos/group/alpha/dependabot/alerts?state=open' --jq '.[].html_url'")
        self.assertEqual(rows["dep:unread"]["list"], ["group/gamma: token does not reach it",
                                                      "group/epsilon: API HTTP 403; retry on a later collection"])
        self.assertEqual(self.area("dep", protection=protection_of(ALERTED))["at"], "2026-09-05T08:00:00Z")

    def test_a_page_cut_short_is_a_warning_since_the_rest_may_be_grave(self):
        paged = {**ALERTED[4], "alerts": {"dependabot": {"open": 100, "more": True, "severity": {"low": 100}}}}
        (row,) = self.area("dep", protection=protection_of([paged]))["rows"]
        self.assertEqual((row["state"], row["detail"]), ("warning", "100 low · severity past the first page not read"))

    def test_a_repo_not_re_read_in_48_hours_is_a_caution_even_beside_a_fresh_one(self):
        old = {**ALERTED[4], "observed_at": "2026-09-04T08:00:00Z",
               "alerts": {"dependabot": {"open": 0, "more": False}, "secret_scanning": {"visibility": "private"}}}
        fresh = {**ALERTED[0], "alerts": {"dependabot": {"open": 0, "more": False}, "secret_scanning": {"visibility": "private"}}}
        for key, name in (("dep", "dependencies"), ("sec", "security")):
            rows = {row["id"]: row for row in self.area(key, protection=protection_of([old, fresh]))["rows"]}
            self.assertEqual((rows[f"{key}:stale"]["state"], rows[f"{key}:stale"]["what"]),
                             ("caution", f"1 managed repo: {name} not re-read in 48 hours"), key)
            self.assertEqual(rows[f"{key}:stale"]["list"], ["group/delta: observed 2026-09-04T08:00:00Z"])

    def test_of_sibling_checkouts_the_newest_observation_counts(self):
        stale = {**ALERTED[0], "repo": "/checkouts/aardvark", "observed_at": "2026-09-01T08:00:00Z",
                 "alerts": {"dependabot": {"open": 0, "more": False}}}
        rows = [row["id"] for row in self.area("dep", protection=protection_of([stale, ALERTED[0]]))["rows"]]
        self.assertEqual(rows, ["dep:/checkouts/alpha"])

    def test_no_open_alert_is_an_ok_row_and_no_managed_repo_is_unknown(self):
        clean = [{**repo, "alerts": {"dependabot": {"open": 0, "more": False}}} for repo in ALERTED[:2]]
        (row,) = self.area("dep", protection=protection_of(clean))["rows"]
        self.assertEqual((row["id"], row["state"], row["what"]), ("dep:ok", "ok", "No open Dependabot alert in 1 managed repo"))
        for key in ("dep", "sec"):
            (row,) = self.area(key)["rows"]
            self.assertEqual((row["id"], row["state"]), (f"{key}:none", "unknown"))

    def test_dependabot_alerts_off_is_an_ok_row_listing_the_repos_never_unread(self):
        """sd:5595: alerts off may stay off; the area tracks which repos, beside clean, open and unread ones."""
        off = {**ALERTED[4], "alerts": {"dependabot": {"setting": "disabled"}}}
        clean = {**ALERTED[0], "alerts": {"dependabot": {"open": 0, "more": False}}}
        (row,) = self.area("dep", protection=protection_of([off]))["rows"]
        self.assertEqual((row["id"], row["state"], row["what"], row["list"]),
                         ("dep:off", "ok", "Dependabot alerts off in 1 managed repo", ["group/delta"]))
        area = self.area("dep", protection=protection_of([off, clean]))
        self.assertEqual({row["id"]: row["state"] for row in area["rows"]}, {"dep:ok": "ok", "dep:off": "ok"})
        self.assertEqual(health_screen._lamp(area)["state"], "ok")
        mixed = [*ALERTED[:4], off, *ALERTED[5:]]
        rows = {row["id"]: row for row in self.area("dep", protection=protection_of(mixed))["rows"]}
        self.assertEqual({key: row["state"] for key, row in rows.items()},
                         {"dep:/checkouts/alpha": "warning", "dep:unread": "unknown", "dep:off": "ok"})
        self.assertEqual(rows["dep:unread"]["what"], "2 managed repos: dependencies not read")
        self.assertEqual(rows["dep:off"]["list"], ["group/delta"])

    def test_an_alerts_off_repo_not_re_read_in_48_hours_is_still_stale(self):
        old = {**ALERTED[4], "observed_at": "2026-09-04T08:00:00Z", "alerts": {"dependabot": {"setting": "disabled"}}}
        rows = {row["id"]: row for row in self.area("dep", protection=protection_of([old]))["rows"]}
        self.assertEqual(rows["dep:stale"]["list"], ["group/delta: observed 2026-09-04T08:00:00Z"])
        self.assertEqual(rows["dep:off"]["state"], "ok")

    def test_an_older_library_without_the_managed_flag_is_the_area_error_not_an_empty_fleet(self):
        older = [{key: value for key, value in repo.items() if key not in ("managed", "alerts")} for repo in PROTECTION]
        for key in ("dep", "sec"):
            area = self.area(key, protection=protection_of(older))
            self.assertEqual(area["rows"], [])
            self.assertIn("reports no managed flag", area["error"])

    def test_security_is_open_secret_alerts_and_public_repos_with_scanning_off(self):
        """sd:2206: private repos are not scanned, by policy, and are counted, not flagged."""
        rows = {row["id"]: row for row in self.area("sec", protection=protection_of(ALERTED))["rows"]}
        self.assertEqual({key: (row["state"], row["what"]) for key, row in rows.items()}, {
            "sec:/checkouts/alpha": ("warning", "group/alpha: 1 open secret-scanning alert"),
            "sec:off:/checkouts/delta": ("caution", "group/delta: public, secret scanning off"),
            "sec:unread": ("unknown", "2 managed repos: security not read"),
        })
        self.assertEqual(rows["sec:off:/checkouts/delta"]["cli"],
                         "open https://github.com/group/delta/settings/security_analysis")
        odd = [{**ALERTED[4], "slug": "group/delta;$(id)"}]
        # Bundle review round 2: a slug reaches the copy line quoted.
        self.assertEqual(self.area("sec", protection=protection_of(odd))["rows"][0]["cli"],
                         "open 'https://github.com/group/delta;$(id)/settings/security_analysis'")
        self.assertEqual(rows["sec:/checkouts/alpha"]["cli"],
                         "gh api --paginate 'repos/group/alpha/secret-scanning/alerts?state=open' --jq '.[].html_url'")
        clean = [{**ALERTED[0], "alerts": {"secret_scanning": {"visibility": "public", "setting": "enabled", "open": 0}}},
                 {**ALERTED[4], "alerts": {"secret_scanning": {"visibility": "private"}}}]
        (row,) = self.area("sec", protection=protection_of(clean))["rows"]
        self.assertEqual((row["state"], row["facts"]), ("ok", {"Clean": "1", "Private": "1"}))

    def test_the_default_reader_finds_the_alerts_the_nightly_sync_stored(self):
        upsert_repo(self.connection, "/checkouts/widget", remote="git@github.com:example/widget.git")
        self.connection.execute("UPDATE repo SET managed = 1")
        body = {"alerts": {"dependabot": {"open": 1, "more": False, "severity": {"high": 1}},
                           "secret_scanning": {"visibility": "public", "setting": "disabled"}}}
        self.connection.execute("INSERT INTO repo_protection (repo, observed_at, status, default_branch, body) "
                                "VALUES ('/checkouts/widget', '2026-09-05T02:20:00Z', 'protected', 'main', ?)",
                                (json.dumps(body),))
        self.connection.commit()
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of([]), ports=ports_snapshot)
        areas = {area["id"]: area for area in doc["areas"]}
        self.assertEqual([(row["id"], row["state"]) for row in areas["dep"]["rows"]], [("dep:/checkouts/widget", "warning")])
        self.assertEqual([(row["id"], row["state"]) for row in areas["sec"]["rows"]], [("sec:off:/checkouts/widget", "caution")])

    def heartbeat(self, probes, stamp="2026-09-06T03:10:00Z"):
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, timestamp=stamp, body={"probes": probes})

    def test_credentials_light_by_presence_rejection_and_days_to_expiry(self):
        """sd:2203. NOW is 2026-09-06T12:00Z: 3 days left is a warning, 19 a caution, 86 fine."""
        self.heartbeat([
            {"id": "github_pat", "name": "GitHub PAT (GITHUB_PERSONAL_ACCESS_TOKEN)", "present": True, "valid": True,
             "expires": "2026-09-10T00:00:00Z"},
            {"id": "soon", "name": "Soon", "present": True, "valid": True, "expires": "2026-09-26T00:00:00Z"},
            {"id": "later", "name": "Later", "present": True, "valid": True, "expires": "2026-12-01T00:00:00Z"},
            {"id": "gh", "name": "gh CLI sign-in (gh auth status)", "signed_in": False},
            {"id": "ha_token", "name": "Home Assistant token (HA_TOKEN)", "present": False},
            {"id": "unreached", "name": "Unreached", "present": True, "reason": "not reached: OSError"},
            {"id": "mcp", "name": "MCP servers (claude mcp list)",
             "servers": [{"name": "github", "status": "connected"}, {"name": "slack", "status": "needs_auth"}]},
        ])
        area = self.area("cred")
        self.assertEqual(area["at"], "2026-09-06T03:10:00Z")
        self.assertEqual({row["id"]: (row["state"], row["what"]) for row in area["rows"]}, {
            "cred:github_pat": ("warning", "GitHub PAT (GITHUB_PERSONAL_ACCESS_TOKEN): expires in 3 days"),
            "cred:soon": ("caution", "Soon: expires in 19 days"),
            "cred:later": ("ok", "Later: expires in 85 days"),
            "cred:gh": ("warning", "gh CLI sign-in (gh auth status): rejected"),
            "cred:ha_token": ("warning", "Home Assistant token (HA_TOKEN): absent"),
            "cred:unreached": ("unknown", "Unreached: not read"),
            "cred:mcp": ("caution", "1 of 2 MCP servers not connected"),
        })

    def test_an_expiry_exactly_on_a_threshold_lights_that_threshold(self):
        self.heartbeat([{"id": "week", "name": "Week", "present": True, "valid": True, "expires": "2026-09-13T12:00:00Z"},
                        {"id": "month", "name": "Month", "present": True, "valid": True, "expires": "2026-10-06T12:00:00Z"}])
        self.assertEqual({row["id"]: row["state"] for row in self.area("cred")["rows"]},
                         {"cred:week": "warning", "cred:month": "caution"})

    def test_credentials_never_observed_or_gone_stale_say_so(self):
        (row,) = self.area("cred")["rows"]
        self.assertEqual((row["id"], row["state"]), ("cred:none", "unknown"))
        self.heartbeat([{"id": "gh", "name": "gh", "signed_in": True}], stamp="2026-09-01T03:10:00Z")
        rows = {row["id"]: row for row in self.area("cred")["rows"]}
        self.assertEqual((rows["cred:gh"]["state"], rows["cred:stale"]["state"]), ("ok", "caution"))
        self.assertEqual(rows["cred:stale"]["what"], "Credentials last observed 5 days ago")

    def test_an_older_library_without_the_credentials_module_is_the_area_error(self):
        with patch.dict("sys.modules", {"sd_db.credentials": None}):
            area = self.area("cred")
        self.assertEqual(area["rows"], [])
        self.assertIn("has no credentials module", area["error"])

    def test_malformed_stored_data_is_unknown_never_clean(self):
        bad = [{**ALERTED[0], "alerts": {"dependabot": ["bad"], "secret_scanning": ["bad"]}}, ALERTED[4]]
        for key in ("dep", "sec"):
            area = self.area(key, protection=protection_of(bad))
            rows = {row["id"]: row for row in area["rows"]}
            self.assertEqual(area["error"], "")
            self.assertEqual(rows[f"{key}:unread"]["list"], ["group/alpha: stored alerts are malformed"])
            self.assertEqual(len(rows), 2, rows)
        self.heartbeat([{"id": "mcp", "name": "MCP servers", "servers": [None]}, "junk"])
        self.assertEqual({row["id"]: row["state"] for row in self.area("cred")["rows"]},
                         {"cred:mcp": "caution", "cred:bad1": "unknown"})
        self.heartbeat([], stamp="2026-09-06T04:10:00Z")
        self.assertEqual([(row["id"], row["state"]) for row in self.area("cred")["rows"]], [("cred:empty", "unknown")])

    def test_a_credentials_row_without_a_probe_list_is_the_area_error(self):
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, body={"oops": 1})
        area = self.area("cred")
        self.assertEqual((area["rows"], area["error"]), ([], "the credentials heartbeat has no probe list"))

    def test_each_running_aws_resource_is_a_machine_row_with_a_console_link(self):
        """sd:6713: my running AWS resources from the nightly read; a failed read is unknown, never clean.
        Operator 2026-11-01: one row per resource, not one row listing them."""
        self.assertEqual(health_screen.VIEW_OF["aws"], "machine")
        self.assertEqual(self.area("aws")["rows"][0]["what"], "AWS resources not yet observed")
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, timestamp="2026-09-06T03:10:00Z",
                     body={"probes": []})
        self.assertEqual(self.area("aws")["rows"][0]["what"], "AWS read not configured")
        resources = [{"type": "EC2 instance", "profile": "me", "region": "us-west-2", "id": "i-1", "name": "box",
                      "since": "2026-09-01T00:00:00Z"},
                     {"type": "CloudFormation stack", "profile": "me", "region": "us-east-1",
                      "id": "arn:aws:cloudformation:us-east-1:000000000000:stack/web/x", "name": "web",
                      "since": "2026-09-06T01:00:00Z"}]
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, timestamp="2026-09-06T03:11:00Z",
                     body={"probes": [], "aws": {"resources": resources, "errors": []}})
        rows = self.area("aws")["rows"]
        self.assertEqual([(row["id"], row["state"], row["what"], row["detail"]) for row in rows], [
            ("aws:me:us-east-1:arn:aws:cloudformation:us-east-1:000000000000:stack/web/x", "ok",
             "CloudFormation stack web", "us-east-1 · running under a day"),
            ("aws:me:us-west-2:i-1", "ok", "EC2 instance box", "us-west-2 · running 5 days")])
        self.assertEqual([row["list"] for row in rows], [
            [{"text": "AWS console", "href": "https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1"
              "#/stacks/stackinfo?stackId=arn%3Aaws%3Acloudformation%3Aus-east-1%3A000000000000%3Astack%2Fweb%2Fx"}],
            [{"text": "AWS console",
              "href": "https://us-west-2.console.aws.amazon.com/ec2/home?region=us-west-2#InstanceDetails:instanceId=i-1"}]])
        self.assertEqual(rows[1]["facts"]["Region"], "us-west-2")
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, timestamp="2026-09-06T03:12:00Z",
                     body={"probes": [], "aws": {"resources": resources[:1], "errors": ["me us-east-1 EC2 instance: aws exited 255"]}})
        self.assertEqual([(row["id"], row["state"], row["what"]) for row in self.area("aws")["rows"]], [
            ("aws:me:us-west-2:i-1", "ok", "EC2 instance box"), ("aws:failed", "unknown", "1 AWS read failed")])
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, timestamp="2026-09-06T03:13:00Z",
                     body={"probes": [], "aws": {"resources": [], "errors": []}})
        self.assertEqual([(row["id"], row["state"], row["what"]) for row in self.area("aws")["rows"]],
                         [("aws", "ok", "No running AWS resources")])

    def test_two_profiles_seeing_one_resource_are_two_rows_with_their_own_ids(self):
        """sd:7117: the row ID carries the profile; one ID for both made Details open the first row's."""
        one = {"type": "EC2 instance", "region": "us-west-2", "id": "i-1", "name": "box", "since": "2026-09-01T00:00:00Z"}
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, timestamp="2026-09-06T03:10:00Z",
                     body={"probes": [], "aws": {"resources": [{**one, "profile": "dev"}, {**one, "profile": "prod"}],
                                                 "errors": []}})
        self.assertEqual([row["id"] for row in self.area("aws")["rows"]],
                         ["aws:dev:us-west-2:i-1", "aws:prod:us-west-2:i-1"])

    def test_merged_branches_are_one_row_per_repo_and_a_checked_out_one_is_its_own(self):
        br = self.doc()["areas"][3]
        rows = {row["id"]: row for row in br["rows"]}
        self.assertEqual(list(rows), ["br:/checkouts/alpha", "brs:/checkouts/alpha", "br:no_default"])
        merged = rows["br:/checkouts/alpha"]
        self.assertEqual((merged["state"], merged["type"], merged["what"]),
                         ("caution", "merged branches", "alpha: 3 merged branches not deleted"))
        self.assertEqual(merged["facts"], {"Repo": "/checkouts/alpha", "Merged": "3", "Oldest": "2026-07-15", "Newest": "2026-09-01"})
        self.assertEqual(merged["cli"], "git -C /checkouts/alpha branch -d feat/one feat/two fix/three")
        self.assertEqual(merged["list"], ["feat/one · 2026-08-01", "feat/two · 2026-09-01", "fix/three · 2026-07-15"])
        held = rows["brs:/checkouts/alpha"]
        self.assertEqual((held["state"], held["type"], held["cli"]), ("queued", "merged branches", "git -C /checkouts/alpha worktree list"))
        self.assertIn("git branch -d refuses it", held["disabled"])
        self.assertEqual((rows["br:no_default"]["state"], rows["br:no_default"]["list"]), ("unknown", ["/checkouts/beta"]))
        self.assertIn("remote set-head origin --auto", rows["br:no_default"]["cli"])

    def test_no_merged_branch_is_an_ok_check_and_no_repo_is_unknown_not_clean(self):
        clean = {"repos": 3, "merged": [], "no_default": [], "unread": []}
        (row,) = self.doc(branches=scan_of(clean))["areas"][3]["rows"]
        self.assertEqual((row["id"], row["state"], row["detail"]),
                         ("br:ok", "ok", "checked the local branches of 3 repos against origin/HEAD"))
        (row,) = self.doc(branches=scan_of({**clean, "repos": 0}))["areas"][3]["rows"]
        self.assertEqual((row["id"], row["state"]), ("br:none", "unknown"))

    def test_disk_is_a_row_per_full_volume_the_biggest_storage_folders_and_merged_build_output(self):
        disk = self.doc()["areas"][0]
        rows = {row["id"]: row for row in disk["rows"]}
        self.assertEqual(list(rows), ["vol:/System/Volumes/Data", "rs:/Volumes/store/repo-storage/alpha",
                                      "rs:/Volumes/store/repo-storage/beta", "rs:/Volumes/store/repo-storage/gamma",
                                      "build:merged"])
        volume = rows["vol:/System/Volumes/Data"]
        self.assertEqual((volume["state"], volume["what"], volume["detail"]),
                         ("caution", "Mac data is 85% full", "/System/Volumes/Data · 150.0 GiB free of 1000.0 GiB"))
        folder = rows["rs:/Volumes/store/repo-storage/alpha"]
        self.assertEqual((folder["state"], folder["type"], folder["what"]), ("queued", "storage folder", "repo-storage/alpha holds 5.0 GiB"))
        self.assertEqual(folder["cli"], "du -sh /Volumes/store/repo-storage/alpha/* | sort -h | tail -5")
        build = rows["build:merged"]
        self.assertEqual((build["state"], build["type"], build["what"]), ("caution", "build output", "2 merged worktrees keep build output"))
        # sd:3393: only a clean worktree's folders are offered; one with uncommitted changes is listed and kept.
        self.assertEqual(build["cli"], "rm -rf /work/wt-old/target\nrm -rf /work/wt-old/node_modules")
        self.assertEqual((build["remove"], build["kept"], build["revision"]),
                         (["/work/wt-old/target", "/work/wt-old/node_modules"], ["/work/wt-dirty"], "b" * 64))
        self.assertEqual(build["list"][1], "/work/wt-dirty · target/ · feat/wip · uncommitted changes: kept")
        self.assertEqual([volume["name"] for volume in disk["extra"]["volumes"]], ["Mac data", "store"])
        full = {**DISK, "volumes": [{**DISK["volumes"][1], "capacity": 90}], "build": {"checked": 2, "merged": [], "unread": []}}
        rows = {row["id"]: row for row in self.doc(disk=scan_of(full))["areas"][0]["rows"]}
        self.assertEqual(rows["vol:/Volumes/store"]["state"], "warning")
        self.assertEqual((rows["build:merged"]["state"], rows["build:merged"]["type"]), ("ok", "check"))

    def test_disk_with_no_storage_folder_configured_names_the_file_rather_than_a_clean_area(self):
        rows = {row["id"]: row for row in self.doc(disk=scan_of({**DISK, "storage": [], "refused": ["line 2: junk"]}))["areas"][0]["rows"]}
        self.assertEqual(rows["rs:none"]["state"], "unknown")
        self.assertIn("storage|<path> line in Settings, Dashboard (/config/project-dashboard/disk.conf) (1 line not understood)", rows["rs:none"]["detail"])
        unread = {**DISK, "storage": [{"root": "/Volumes/gone", "folders": [], "error": "du did not finish inside the Disk budget"}]}
        rows = {row["id"]: row for row in self.doc(disk=scan_of(unread))["areas"][0]["rows"]}
        self.assertEqual((rows["rs:/Volumes/gone"]["state"], rows["rs:/Volumes/gone"]["detail"]),
                         ("unknown", "du did not finish inside the Disk budget"))

    def test_the_branch_reader_lists_merged_branches_and_names_repos_it_could_not_read(self):
        root = Path(self.tmp.name)
        alpha = cloned(root / "alpha")
        git("-C", str(alpha), "branch", "merged-one")
        git("-C", str(alpha), "branch", "held")
        git("-C", str(alpha), "worktree", "add", "-q", str(root / "wt-held"), "held")
        git("-C", str(alpha), "checkout", "-q", "-b", "ahead")
        git("-C", str(alpha), "commit", "-q", "--allow-empty", "-m", "not merged")
        git("-C", str(alpha), "checkout", "-q", "main")
        beta = root / "beta"
        git("init", "-q", "-b", "main", str(beta))
        git("-C", str(beta), "commit", "-q", "--allow-empty", "-m", "first")
        # A folder that is no repository is unread; a registered path with no folder is skipped.
        (root / "gone").mkdir()
        for path in (alpha, beta, root / "gone", root / "elsewhere"):
            self.repo(str(path))
        scan = REAL_BRANCH_SCAN(self.connection)
        self.assertEqual(scan["repos"], 1)
        (found,) = scan["merged"]
        # main is the default branch and ahead is not merged: neither is listed.
        self.assertEqual(found["deletable"], [("merged-one", "2026-09-05")])
        self.assertEqual([branch for branch, _ in found["checked_out"]], ["held"])
        self.assertEqual(Path(found["checked_out"][0][1]).resolve(), (root / "wt-held").resolve())
        self.assertEqual((scan["no_default"], scan["unread"]), ([str(beta)], [str(root / "gone")]))

    def test_a_slow_branch_walk_is_stopped_at_its_budget_and_is_the_branches_error(self):
        stub = self.stub("git", self.stalled())
        checkouts = [str(Path(self.tmp.name) / name) for name in ("one", "two")]
        for path in checkouts:
            Path(path).mkdir()
            self.repo(path)
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}):
            doc = self.doc(branches=lambda connection: REAL_BRANCH_SCAN(
                connection, within=0.5, repo_paths=checkouts))
        br = doc["areas"][3]
        self.assertFalse(self.finished.exists(), "the page waited on the branch walk instead of stopping it")
        self.assertEqual((br["rows"], br["error"]), ([], "the merged-branch walk ran past its budget of 0.5 seconds "
                                                         "and was stopped rather than waited on"))

    @property
    def finished(self):
        return Path(self.tmp.name) / "stalled-finished"

    def stalled(self):
        """A stub body that answers after 5 s and leaves a marker only then (sd:2667).

        The budget kills the shell, the one holding the pipes; its sleep holds
        none, so the read ends at the kill. A caller that returns without the
        marker stopped the command rather than waited it out, however long
        the kill took on a loaded machine.
        """
        return f'sleep 5 >/dev/null 2>&1\ntouch "{self.finished}"\n'

    def stub(self, name, body):
        stub = Path(self.tmp.name) / "bin"
        stub.mkdir(exist_ok=True)
        (stub / name).write_text("#!/bin/sh\n" + body)
        (stub / name).chmod(0o755)
        return stub

    DF = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
          "/dev/disk3s1s1 1000 400 600 40% /\n"
          "/dev/disk3s5 1000 850 150 85% /System/Volumes/Data\n"
          "devfs 10 10 0 100% /dev\n"
          "pseudofs 0 0 0 - /Volumes/pseudo\n"
          "/dev/disk5s1 2000 800 1200 40% /Volumes/my store\n")

    def test_the_disk_reader_keeps_the_data_volume_and_volumes_and_finds_merged_build_output(self):
        root = Path(self.tmp.name)
        alpha = cloned(root / "alpha")
        git("-C", str(alpha), "branch", "done")
        git("-C", str(alpha), "branch", "plain")
        git("-C", str(alpha), "branch", "ahead")
        for branch in ("done", "plain", "ahead"):
            git("-C", str(alpha), "worktree", "add", "-q", str(root / f"wt-{branch}"), branch)
        git("-C", str(root / "wt-ahead"), "commit", "-q", "--allow-empty", "-m", "not merged")
        (root / "wt-done" / "target").mkdir()
        (root / "wt-ahead" / "node_modules").mkdir()
        self.repo(str(alpha))
        store = root / "store"
        for name, size in (("big", 64), ("small", 8)):
            (store / name).mkdir(parents=True)
            (store / name / "data").write_bytes(b"x" * size * 1024)
        config = root / "disk.conf"
        config.write_text(f"# storage folders\nstorage|{store}\nstorage|{root / 'missing'}\nvolume|/nowhere\n")
        stub = self.stub("df", f"cat <<'EOF'\n{self.DF}EOF\n")
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}):
            scan = REAL_DISK_SCAN(self.connection, listed=health_collectors.storage_list(self.connection, config))
        self.assertEqual([volume["mount"] for volume in scan["volumes"]], ["/System/Volumes/Data", "/Volumes/my store"])
        self.assertEqual(scan["volumes"][0]["capacity"], 85)
        self.assertEqual(scan["build"]["checked"], 3)
        (merged,) = scan["build"]["merged"]
        self.assertEqual((Path(merged["path"]).resolve(), merged["branch"], merged["dirs"]), ((root / "wt-done").resolve(), "done", ["target"]))
        found, missing = scan["storage"]
        self.assertEqual([Path(folder["path"]).name for folder in found["folders"]], ["big", "small"])
        self.assertEqual(missing["error"], "the folder does not exist or is not mounted")
        self.assertEqual(scan["refused"], ["line 4: volume|/nowhere"])

    def test_a_config_that_cannot_be_read_is_a_named_problem_not_an_error(self):
        """sd:3532 (review 2026-10-12, A1 F7): only a missing disk.conf was caught; a folder in its place raised."""
        config = Path(self.tmp.name) / "disk.conf"
        config.mkdir()
        text, source, problem = health_collectors.storage_list(self.connection, config)
        self.assertEqual((text, source), (None, "<config>/project-dashboard/disk.conf"))
        self.assertEqual(problem, "not read: IsADirectoryError")

    #: `df -kP <folder>` on a local drive that holds 1.5 TiB, the size du cannot walk inside the budget (sd:5879);
    #: printf format, so the percent sign is doubled.
    DRIVE = "/dev/disk6s1 2147483648 1610612736 536870912 75%% /Volumes/store"

    def stalled_du(self, drive):
        """The real reader on a local folder whose du does not finish; `drive` is what `df -kP <folder>` prints."""
        store = Path(self.tmp.name) / "store"
        store.mkdir()
        listed = (f"storage|{store}\n", "<config>/project-dashboard/disk.conf", "")
        stub = self.stub("df", f'if [ "$1" = -kP ]; then {drive}; else cat <<\'EOF\'\n{self.DF}EOF\nfi\n')
        self.stub("du", self.stalled())
        # The walk's clock holds still, so df spends none of the 1 s however
        # long it takes to start, and du alone meets its timeout (sd:2667).
        # Each command's own timeout is wall time, so only du keeps it: a
        # loaded gate started df past 1 s and failed the area.
        real_run = subprocess.run

        def run(argv, **kwargs):
            return real_run(argv, **{**kwargs, **({} if argv[0] == "du" else {"timeout": 60})})
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}), \
                patch.object(health_collectors, "time", HeldClock()), \
                patch.object(health_collectors.subprocess, "run", run):
            doc = self.doc(disk=lambda connection: REAL_DISK_SCAN(connection, within=1, listed=listed, repo_paths=[]))
        disk = doc["areas"][0]
        self.assertFalse(self.finished.exists(), "the page waited on du instead of stopping it")
        self.assertEqual(disk["error"], "")
        return store, {row["id"]: row for row in disk["rows"]}

    def test_a_du_that_does_not_finish_on_a_local_folder_shows_the_drives_use(self):
        """sd:5879: du on a 2.1 TB local SSD cannot finish in the 8 s budget; the operator ruled for the drive total."""
        store, rows = self.stalled_du(f'printf "Filesystem 1024-blocks Used Available Capacity Mounted on\\n{self.DRIVE}\\n"')
        row = rows[f"rs:{store}"]
        self.assertEqual((row["state"], row["type"]), ("queued", "volume"))
        self.assertEqual(row["what"], "store: drive 1.50 TiB used of 2.00 TiB, 512.0 GiB free")
        self.assertEqual(row["detail"], f"{store} · local drive at /Volumes/store · du did not finish inside the Disk budget; "
                                        "folders not measured")
        self.assertIn("took longer than Disk's 8 s", row["note"])
        self.assertEqual(row["cli"], f"df -h {store}")

    def test_a_du_that_did_not_finish_is_not_run_again_on_the_next_read(self):
        """sd:7898: du on the 2.1 TB drive never finished, so each Disk read spent about 6 of its 8 s on it for nothing."""
        store, ran = Path(self.tmp.name) / "store", Path(self.tmp.name) / "ran"
        store.mkdir()
        listed = (f"storage|{store}\n", "<config>/project-dashboard/disk.conf", "")
        stub = self.stub("df", 'if [ "$1" = -kP ]; then printf "Filesystem 1024-blocks Used Available Capacity Mounted on'
                               f'\\n{self.DRIVE}\\n"; else cat <<\'EOF\'\n{self.DF}EOF\nfi\n')
        self.stub("du", f'echo du >> "{ran}"\n' + self.stalled())
        self.addCleanup(lambda: getattr(health_collectors, "_DU_OVERRAN", {}).clear())
        real_run = subprocess.run

        def run(argv, **kwargs):
            return real_run(argv, **{**kwargs, **({} if argv[0] == "du" else {"timeout": 60})})

        def scan():
            return REAL_DISK_SCAN(self.connection, within=1, listed=listed, repo_paths=[])["storage"]
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}), \
                patch.object(health_collectors, "time", HeldClock()), \
                patch.object(health_collectors.subprocess, "run", run):
            first, second = scan(), scan()
            self.assertEqual(ran.read_text().split(), ["du"], "the second read ran the du that could not finish again")
            with patch.object(health_collectors, "DU_RETRY_SECONDS", 0):
                scan()
        self.assertEqual(ran.read_text().split(), ["du", "du"], "a du past its retry time was not tried again")
        # The second read says du is paused, not that it did not finish (review of sd:7898); the rest is the same.
        self.assertEqual([{**entry, "du": None} for entry in first], [{**entry, "du": None} for entry in second])
        self.assertEqual((second[0]["error"], second[0]["drive"]["mount"]), ("", "/Volumes/store"))

    def du_twice(self, du, *, step=0.0, within=1):
        """Two real Disk reads of one local folder, with `du(argv, timeout)` in place of du; each du's timeout, and the reads.

        `step` seconds pass on the walk's clock while `df -kP <folder>` runs, so du gets what is left of `within`.
        """
        store = Path(self.tmp.name) / "store"
        store.mkdir(exist_ok=True)
        listed = (f"storage|{store}\n", "<config>/project-dashboard/disk.conf", "")
        stub = self.stub("df", 'if [ "$1" = -kP ]; then printf "Filesystem 1024-blocks Used Available Capacity Mounted on'
                               f'\\n{self.DRIVE}\\n"; else cat <<\'EOF\'\n{self.DF}EOF\nfi\n')
        self.addCleanup(lambda: getattr(health_collectors, "_DU_OVERRAN", {}).clear())
        clock, calls, real_run = HeldClock(), [], subprocess.run

        def run(argv, **kwargs):
            if argv[0] == "du":
                calls.append(kwargs["timeout"])
                return du(argv, kwargs["timeout"])
            if argv[:2] == ["df", "-kP"] and argv[2] == str(store):
                clock.held = clock.monotonic() + step
            return real_run(argv, **{**kwargs, "timeout": 60})

        def scan():
            return REAL_DISK_SCAN(self.connection, within=within, listed=listed, repo_paths=[])
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}), \
                patch.object(health_collectors, "time", clock), patch.object(health_collectors.subprocess, "run", run):
            first, second = scan(), scan()
        return calls, first, second

    def test_a_du_that_could_not_start_is_tried_again_on_the_next_read(self):
        """sd:7898 review: only a du that ran out its time is paused; one that could not start says so."""
        def refused(argv, timeout):
            raise PermissionError(13, "Permission denied", argv[0])
        calls, first, _ = self.du_twice(refused)
        self.assertEqual(len(calls), 2, "a du that could not start was paused as if it had overrun")
        self.assertEqual(first["storage"][0].get("du"), "du could not run")

    def test_a_du_cut_short_by_a_spent_budget_is_tried_again_on_the_next_read(self):
        """sd:7898 review: a du that got half a second of a spent walk did not show the folder is too big."""
        def slow(argv, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)
        calls, _, _ = self.du_twice(slow, step=7.5, within=8)
        self.assertEqual(calls, [0.5, 0.5], "a du cut short by the walk's budget was paused as if it had overrun")

    def test_a_paused_du_says_until_when_not_that_it_did_not_finish(self):
        """sd:7898 review: the read that skips du says so, and when du runs again, in local time."""
        def slow(argv, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)
        calls, first, second = self.du_twice(slow)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["storage"][0].get("du"), "du did not finish inside the Disk budget")
        paused = second["storage"][0].get("du") or ""
        self.assertRegex(paused, r"^du paused after an earlier overrun until \d\d:\d\d \S+$")
        rows, _ = health_screen._disk_rows({**second, "volumes": []})
        self.assertEqual([row["detail"] for row in rows if row["id"].startswith("rs:")],
                         [f"{second['storage'][0]['root']} · local drive at /Volumes/store · {paused}; folders not measured"])

    def test_a_du_that_does_not_finish_and_no_df_is_a_row_not_a_refusal_of_the_area(self):
        store, rows = self.stalled_du("exit 1")
        self.assertEqual(rows[f"rs:{store}"]["detail"], "du did not finish inside the Disk budget")

    MOUNT = ("/dev/disk3s5 on / (apfs, local, journaled)\n"
             "//guest@nas.example.test/share on /Volumes/share (smbfs, nodev, nosuid, mounted by guest)\n"
             "nas.example.test:/export on /mnt/export type nfs4 (rw,relatime)\n"
             # Linux writes a space in a mount point as \\040.
             "nas.example.test:/other on /mnt/my\\040share type cifs (rw)\n")

    def network_scan(self, mount):
        """The real reader on stub mount, df and du; du leaves a marker for each folder it walks."""
        store = Path(self.tmp.name) / "store"
        (store / "big").mkdir(parents=True)
        (store / "big" / "data").write_bytes(b"x" * 64 * 1024)
        walked = Path(self.tmp.name) / "walked"
        listed = (f"storage|/Volumes/share/repo-storage\nstorage|/mnt/export/data\nstorage|/mnt/my share/data\nstorage|{store}\n",
                  "<config>/project-dashboard/disk.conf", "")
        self.stub("mount", mount)
        self.stub("du", f'echo "$4" >> {walked}\nprintf "64\\t$4/big\\n70\\t$4\\n"\n')
        stub = self.stub("df", 'if [ "$1" = -kP ]; then printf "Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
                               '//guest@nas.example.test/share 4000000000 3000000000 1000000000 75%% /Volumes/share\\n"; '
                               f"else cat <<'EOF'\n{self.DF}EOF\nfi\n")
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}):
            scan = REAL_DISK_SCAN(self.connection, listed=listed, repo_paths=[])
        return scan, (walked.read_text().split() if walked.exists() else []), store

    def test_a_folder_on_a_network_mount_gets_the_shares_df_and_no_du(self):
        """sd:5726: du -k -d 1 on the SMB share ran past 120 s against the 8 s Disk budget."""
        scan, walked, store = self.network_scan(f"cat <<'EOF'\n{self.MOUNT}EOF\n")
        share, export, spaced, local = scan["storage"]
        self.assertEqual(walked, [str(store)], "du walked a folder on a network mount")
        self.assertEqual((spaced["error"], spaced["network"]["type"]), ("", "cifs"))
        self.assertEqual((share["error"], share["folders"]), ("", []))
        self.assertEqual(share["network"], {"type": "smbfs", "mount": "/Volumes/share", "size_kb": 4000000000,
                                            "used_kb": 3000000000, "avail_kb": 1000000000})
        self.assertEqual(export["network"]["type"], "nfs4")
        self.assertEqual([Path(folder["path"]).name for folder in local["folders"]], ["big"])
        self.assertNotIn("network", local)

    def test_a_mount_table_that_did_not_answer_measures_no_folder(self):
        # No type is known, so no folder is known to be local: a stat or du on a share that does not answer is the hang.
        scan, walked, _ = self.network_scan("exit 1\n")
        self.assertEqual(walked, [])
        self.assertEqual({entry["error"] for entry in scan["storage"]},
                         {"not measured: mount gave no table, so a network folder cannot be told from a local one"})

    def test_a_missing_folder_on_a_share_is_dfs_refusal_not_a_size(self):
        # The share path runs no is_dir; df names a missing folder, and that is the row, never a zero.
        stub = self.stub("df", 'echo "df: $2: No such file or directory" >&2\nexit 1\n')
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}"}):
            entry = health_collectors._share(health_collectors.Walk(8, "the Disk reading"), Path("/Volumes/share/gone"),
                                             ("/Volumes/share", "smbfs"))
        self.assertEqual((entry["error"], entry["network"]), ("df: /Volumes/share/gone: No such file or directory", None))

    def test_a_network_mount_row_shows_the_shares_use_and_says_its_folders_are_not_measured(self):
        share = {"root": "/Volumes/share/repo-storage", "folders": [], "error": "",
                 "network": {"type": "smbfs", "mount": "/Volumes/share", "size_kb": 4 * 1024 ** 3, "used_kb": 3 * 1024 ** 3,
                             "avail_kb": 1024 ** 3}}
        rows = {row["id"]: row for row in self.doc(disk=scan_of({**DISK, "storage": [share]}))["areas"][0]["rows"]}
        row = rows["rs:/Volumes/share/repo-storage"]
        self.assertEqual((row["state"], row["type"]), ("queued", "volume"))
        self.assertEqual(row["what"], "repo-storage on a network mount: 3.00 TiB used of 4.00 TiB, 1.00 TiB free")
        self.assertEqual(row["detail"], "/Volumes/share/repo-storage · smbfs share at /Volumes/share · folders not measured")
        self.assertEqual(row["cli"], "df -h /Volumes/share/repo-storage")
        full = {**share, "network": {**share["network"], "used_kb": 9 * 1024 ** 3, "avail_kb": 1024 ** 3, "size_kb": 10 * 1024 ** 3}}
        rows = {row["id"]: row for row in self.doc(disk=scan_of({**DISK, "storage": [full]}))["areas"][0]["rows"]}
        self.assertEqual(rows["rs:/Volumes/share/repo-storage"]["state"], "warning")


    def page_storage(self):
        """The Disk rows the page draws with the real reader on a stub df, the old disk.conf naming a folder `old`."""
        config = Path(self.tmp.name) / "config"
        (config / "project-dashboard").mkdir(parents=True, exist_ok=True)
        (config / "project-dashboard" / "disk.conf").write_text(f"storage|{Path(self.tmp.name) / 'old'}\n")
        stub = self.stub("df", f"cat <<'EOF'\n{self.DF}EOF\n")
        with patch.dict(os.environ, {"PATH": f"{stub}{os.pathsep}{os.environ['PATH']}", "SYSTEM_TOOLS_CONFIG": str(config)}), \
                patch.object(health_collectors, "disk_scan", REAL_DISK_SCAN):
            disk = self.doc()["areas"][0]
        self.assertEqual(disk["error"], "")
        return {row["id"]: row for row in disk["rows"]}

    def test_the_storage_folders_are_the_setting_row_not_disk_conf(self):
        # sd:5109: dashboard.disk_storage wins; the page reads it before the walk starts on a thread of its own.
        store = Path(self.tmp.name) / "store"
        (store / "big").mkdir(parents=True)
        (store / "big" / "data").write_bytes(b"x" * 8 * 1024)
        writes.set_setting(self.connection, "dashboard.disk_storage", f"storage|{store}\n")
        rows = self.page_storage()
        self.assertIn(f"rs:{store / 'big'}", rows)
        self.assertNotIn(f"rs:{Path(self.tmp.name) / 'old'}", rows)

    def test_a_cleared_row_is_not_configured_and_a_failed_read_is_not_read(self):
        writes.set_setting(self.connection, "dashboard.disk_storage", None)
        none = self.page_storage()["rs:none"]
        self.assertEqual((none["state"], none["facts"]["Config"]), ("unknown", "workflow database setting dashboard.disk_storage"))
        self.assertIn("Settings", none["detail"])

        def refuse(connection, keys):
            raise sqlite3.OperationalError("database is locked")

        with patch.object(writes, "read_settings", refuse):
            rows = self.page_storage()
        self.assertNotIn("rs:none", rows)
        self.assertEqual(rows["rs:unread"]["state"], "unknown")
        self.assertIn("database is locked", rows["rs:unread"]["detail"])

    def test_the_walkers_run_at_once_so_the_page_waits_for_the_slowest_not_the_sum(self):
        # Four walkers that each wait for all four: at once they meet and
        # answer; in series the first waits alone until the page's budget
        # leaves it. A meeting, not a stopwatch: 5 x 0.8 s against a 2 s
        # bound failed under gate load with the walkers at once (sd:2667).
        # The timeout only ends a walker that waits alone, as a page that ran
        # them inline would leave it.
        met = threading.Barrier(4, timeout=10)
        self.addCleanup(met.abort)

        def slow(answer):
            def read(*args, **kwargs):
                met.wait()
                return answer(*args, **kwargs)
            return read
        doc = self.doc(fleet=slow(fleet_of(TREES)), ports=slow(ports_snapshot),
                       disk=slow(scan_of(DISK)), branches=slow(scan_of(BRANCHES)))
        self.assertEqual([(area["id"], area["error"], bool(area["rows"])) for area in doc["areas"] if area["read"]],
                         [(key, "", True) for key in ("disk", "cred", "wt", "br", "dep", "sec", "ports", "prot", "svc", "ctr", "mir", "doc", "aws", "stat")])

    def test_a_reader_past_the_page_budget_is_its_area_error_and_the_page_does_not_wait(self):
        # The reader answers only once the page has returned, so a page that
        # waited for it could not return before the reader's own 30 s: no
        # elapsed bound to lose under load (sd:2667).
        returned, answered = threading.Event(), threading.Event()
        self.addCleanup(returned.set)

        def stuck(area):
            returned.wait(30)
            answered.set()
            return fleet_of(TREES)(area)
        with patch.object(health_screen, "PAGE_SECONDS", 0.5):
            doc = self.doc(fleet=stuck)
        self.assertFalse(answered.is_set(), "the page waited on a reader past its budget")
        returned.set()
        wt = doc["areas"][2]
        self.assertEqual((wt["rows"], wt["error"]), ([], "the Worktrees reader was still running at the page's budget of "
                                                         "0.5 seconds and was left rather than waited on"))
        self.assertEqual((doc["areas"][6]["error"], bool(doc["areas"][6]["rows"])), ("", True))

    def test_the_default_walkers_get_the_registry_as_paths_not_the_connection(self):
        # A sqlite connection refuses a second thread: a default walker handed it would be its area's error.
        alpha = cloned(Path(self.tmp.name) / "alpha")
        git("-C", str(alpha), "branch", "merged-one")
        self.repo(str(alpha))
        with patch.object(health_collectors, "branch_scan", REAL_BRANCH_SCAN):
            doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of([]), ports=ports_snapshot, protection=protection_of([]), disk=scan_of(DISK))
        br = doc["areas"][3]
        self.assertEqual((br["error"], [row["id"] for row in br["rows"]]), ("", [f"br:{alpha}"]))


class TheBriefCopiesRow(Collectors, ScreenCase):
    """sd:3459: the `brief-copies` heartbeat as one Vault mirror row. Failed, unread or stale is a finding; off is quiet."""

    doc = TheDocument.doc

    def row(self, found):
        return health_screen._brief_copies_row(lambda: found, now=NOW)

    def beat(self, state, stamp="2026-09-06T11:00:00+00:00", **more):
        return stamp, {"at": stamp, "ok": state != "failed", "state": state, "folder": {"/repos/system": "Briefs/system"},
                       "previous_folder": {}, "written": 3, "refused": [], "reason": None, **more}

    def test_a_failed_and_a_missing_heartbeat_each_show(self):
        # Failure table, "add, hub: render after commit": a render that failed, or none at all, is never clean.
        mirror = next(area for area in self.doc()["areas"] if area["id"] == "mir")
        self.assertEqual([(row["id"], row["state"], row["what"]) for row in mirror["rows"]][-1],
                         ("mir:briefs", "unknown", "Brief copies not yet rendered"))
        row = self.row(self.beat("failed", reason="the vault is not a directory", refused=["2026-09-05 - Daily"]))
        self.assertEqual((row["state"], row["what"], row["detail"]),
                         ("warning", "Brief copies not written", "the vault is not a directory"))
        self.assertEqual(row["list"], ["2026-09-05 - Daily"])
        row = self.row(self.beat("failed", refused=["2026-09-05 - Daily"]))
        self.assertEqual((row["state"], row["detail"]), ("warning", "1 copies refused"))

    def test_the_real_heartbeat_reaches_the_row(self):
        from sd_db import routine_briefs

        upsert_repo(self.connection, "/repos/system", remote="git@example.invalid:x.git")
        routine_briefs.add(self.connection, "2026-09-05 - Daily", "---\nseries: x\n---\n\nBody.\n", repo="/repos/system",
                           who="test")
        routine_briefs.render(self.connection, Path(self.tmp.name) / "no vault")
        row = health_screen._brief_copies_row(lambda: health_screen.read_brief_copies(self.connection),
                                              now=writes.now())
        self.assertEqual(row["state"], "warning")
        self.assertIn("no vault", row["detail"])

    def test_off_is_quiet_ok_is_ok_and_an_old_render_is_caution(self):
        self.assertEqual((self.row(self.beat("off"))["state"], self.row(self.beat("off"))["what"]), ("ok", "Brief copies off"))
        self.assertEqual(self.row(self.beat("ok"))["state"], "ok")
        old = self.row(self.beat("ok", stamp="2026-09-03T11:00:00+00:00"))
        self.assertEqual((old["state"], old["what"]), ("caution", "Brief copies last rendered 3 days ago"))
        self.assertEqual(self.row(self.beat("ok", stamp="2026-09-04T13:00:00+00:00"))["state"], "ok")

    def test_a_heartbeat_that_cannot_be_read_is_unknown(self):
        def refuse():
            raise sqlite3.OperationalError("database is locked")
        row = health_screen._brief_copies_row(refuse, now=NOW)
        self.assertEqual((row["state"], row["detail"]), ("unknown", "database is locked"))


class TheDoctor(Collectors, ScreenCase):
    """The Doctor area (sd:3393): `machine-setup.sh doctor` lines, read in a bounded child. A stub stands in for the script."""

    def area(self, doc):
        return next(area for area in doc["areas"] if area["id"] == "doc")

    def doc(self, doctor=None, now=NOW):
        return health_screen.document(self.connection, now=now, fleet=fleet_of(TREES), ports=ports_snapshot,
                                      protection=protection_of(PROTECTION), doctor=doctor)

    def script(self, body):
        path = Path(self.tmp.name) / "machine-setup.sh"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return path

    def test_each_warn_and_fail_line_is_a_row_and_the_rest_are_one_ok_row(self):
        area = self.area(self.doc(doctor=lambda: DOCTOR))
        rows = [(row["state"], row["what"], row["kind"]) for row in area["rows"]]
        self.assertEqual(rows, [("warning", "Xcode CLT missing", "Doctor · FAIL"),
                                ("caution", "brew doctor has warnings", "Doctor · WARN"),
                                ("caution", "set here but not in shell-env.example: EXAMPLE_TOKEN", "Doctor · WARN"),
                                ("ok", "2 doctor checks pass", "Doctor")])
        self.assertEqual(area["rows"][2]["detail"], "set here but not in shell-env.example: EXAMPLE_TOKEN "
                                                    "add the name (value change-me) so a rebuild gets it")
        self.assertEqual(area["rows"][3]["detail"], "2 not checked here · 1 hard failure")
        self.assertIn("folders without a filled .env (.env.example present, .env absent):", area["rows"][3]["list"])
        self.assertEqual(area["error"], "")

    def test_the_doctor_runs_in_a_child_and_its_closing_line_and_exit_must_agree(self):
        clean = self.script("echo '== doctor'\necho '  ok      fnm (v24)'\necho\necho '  no hard failures'\n")
        found = REAL_DOCTOR_SCAN(script=clean, within=10)
        self.assertEqual((found["checks"], found["fails"]), ([{"mark": "ok", "text": "fnm (v24)", "more": []}], 0))
        for body, why in (
                ("echo '  ok      fnm'\n", "machine-setup.sh doctor exited 0 without its closing line: ok      fnm"),
                ("echo 'machine-setup: no config' >&2\nexit 1\n", "exited 1 without its closing line (machine-setup: no config)"),
                ("echo '  no hard failures'\nexit 2\n", "exited 2 without its closing line: no hard failures")):
            with self.subTest(body=body):
                area = self.area(self.doc(doctor=lambda body=body: REAL_DOCTOR_SCAN(script=self.script(body), within=10)))
                self.assertEqual(area["rows"], [])
                self.assertIn(why, area["error"])

    def test_a_doctor_past_its_deadline_is_stopped_and_unknown_not_clean(self):
        marker = Path(self.tmp.name) / "doctor-finished"
        slow = self.script(f"sleep 5 >/dev/null 2>&1\ntouch '{marker}'\necho '  no hard failures'\n")
        with patch.object(health_screen, "DOCTOR_WAIT_SECONDS", 10.0):
            area = self.area(self.doc(doctor=lambda: REAL_DOCTOR_SCAN(script=slow, within=0.5)))
        self.assertFalse(marker.exists(), "the page waited the doctor out instead of stopping it")
        self.assertEqual(area["rows"], [])
        self.assertRegex(area["error"], r"^machine-setup.sh doctor was stopped: .* and was stopped rather than waited on$")

    def test_a_doctor_still_running_is_unknown_and_the_page_does_not_wait_for_it(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def running():
            release.wait(10)
            return CLEAN_DOCTOR
        started = time.monotonic()
        with patch.object(health_screen, "DOCTOR_WAIT_SECONDS", 0.2):
            area = self.area(self.doc(doctor=running))
        self.assertLess(time.monotonic() - started, health_screen.PAGE_SECONDS)
        self.assertEqual(area["rows"], [])
        self.assertIn(f"machine-setup.sh doctor started at {NOW} and is still running", area["error"])

    def test_a_kept_answer_is_shown_at_once_while_the_next_run_goes(self):
        """sd:5064: a run takes about 10 s, so the 2 s wait always ran out; with an answer kept the page shows it, stale."""
        release = threading.Event()
        self.addCleanup(release.set)
        runs = []

        def second_blocks():
            runs.append(1)
            if len(runs) == 2:
                release.wait(10)
            return CLEAN_DOCTOR
        first = self.area(self.doc(doctor=second_blocks))
        started = time.monotonic()
        # Waited on, the run holds the page 9 s; shown at once, the rest is slack for a loaded gate (sd:6468).
        with patch.object(health_screen, "DOCTOR_WAIT_SECONDS", 9.0):
            later = self.area(self.doc(doctor=second_blocks, now="2026-09-06T12:11:00Z"))
        self.assertLess(time.monotonic() - started, 5, "the page waited on a doctor run it had an answer for")
        self.assertEqual((len(runs), later["rows"], later["at"]), (2, first["rows"], NOW))
        self.assertIn("is still running", later["stale"])
        self.assertIn(f"these rows are the read of {NOW}", later["stale"])

    def test_an_answer_stands_for_ten_minutes_and_a_failed_run_drops_it(self):
        runs, release = [], threading.Event()
        self.addCleanup(release.set)

        def counted():
            runs.append(1)
            if len(runs) == 2:
                raise ValueError("machine-setup.sh doctor exited 2 without its closing line")
            if len(runs) == 3:
                release.wait(10)
            return CLEAN_DOCTOR
        self.area(self.doc(doctor=counted))
        self.assertEqual(self.area(self.doc(doctor=counted, now="2026-09-06T12:09:00Z"))["at"], NOW)
        self.assertEqual(len(runs), 1, "a fresh answer started a second doctor")
        failed = self.area(self.doc(doctor=counted, now="2026-09-06T12:11:00Z"))
        self.assertEqual((len(runs), failed["rows"]), (2, []))
        self.assertIn("exited 2", failed["error"])
        # The next run is still going: the answer from before the failure must not stand in for it.
        with patch.object(health_screen, "DOCTOR_WAIT_SECONDS", 0.2):
            running = self.area(self.doc(doctor=counted, now="2026-09-06T12:12:00Z"))
        self.assertEqual((len(runs), running["rows"], running["stale"]), (3, [], ""))
        self.assertIn("is still running", running["error"])


class OneScanPerArea(Collectors, ScreenCase):
    """A reader past the page's budget holds one thread however often the page is read (sd:2520 review).

    The page made a thread pool per request and left a stuck reader's worker
    alive, so each timed-out refresh added a thread, and the interpreter
    joined them all at exit.
    """

    def blocked(self):
        """A fleet reader that blocks until the test ends, and the threads it ran on."""
        gate, threads = threading.Event(), []

        def read(area):
            threads.append(threading.current_thread())
            gate.wait(10)
            return fleet_of(TREES)(area)
        self.addCleanup(gate.set)
        return read, threads

    def doc(self, fleet, now=NOW):
        return health_screen.document(self.connection, now=now, fleet=fleet, ports=ports_snapshot, protection=protection_of(PROTECTION))

    def test_timed_out_requests_leave_at_most_one_live_worker_for_a_blocked_reader(self):
        read, threads = self.blocked()
        with patch.object(health_screen, "PAGE_SECONDS", 0.2):
            docs = [self.doc(read) for _ in range(4)]
        self.assertLessEqual(len([thread for thread in threads if thread.is_alive()]), 1, threads)
        self.assertEqual(len(threads), 1, "a request started a second scan while the first still ran")
        self.assertTrue(all(thread.daemon for thread in threads), "interpreter exit would wait on the scan")
        self.assertTrue(all(doc["areas"][2]["error"] for doc in docs))
        self.assertIn(f"that scan started at {NOW}, and no second one starts while it runs", docs[-1]["areas"][2]["error"])

    def test_a_reader_still_running_shows_its_last_answer_marked_stale(self):
        first = self.doc(fleet_of(TREES))["areas"][2]
        self.assertEqual((first["error"], first["stale"]), ("", ""))
        read, _ = self.blocked()
        later = "2026-09-05T12:30:00Z"
        with patch.object(health_screen, "PAGE_SECONDS", 0.2):
            wt = self.doc(read, now=later)["areas"][2]
        self.assertEqual((wt["error"], wt["rows"], wt["at"]), ("", first["rows"], NOW))
        self.assertTrue(wt["stale"].startswith("the Worktrees reader was still running at the page's budget"), wt["stale"])
        self.assertTrue(wt["stale"].endswith(f"these rows are the read of {NOW}"), wt["stale"])

    def test_a_reader_that_finishes_answers_again_on_the_next_request(self):
        self.doc(fleet_of(TREES))
        wt = self.doc(fleet_of([]), now="2026-09-05T12:30:00Z")["areas"][2]
        self.assertEqual((wt["error"], wt["stale"], wt["at"]), ("", "", "2026-09-05T12:30:00Z"))
        self.assertNotEqual(wt["rows"], self.doc(fleet_of(TREES))["areas"][2]["rows"])


    def test_the_fleet_snapshot_dates_the_worktrees_area_and_a_failed_refresh_marks_it_stale(self):
        # sd:4956: read.fleet answers its snapshot with `observed`, when its read began, and `stale` after a failed refresh.
        observed = "2026-09-05T11:58:00Z"

        def snapshot(stale=None):
            read = fleet_of([])
            return lambda area: {**read(area), "observed": observed, **({"stale": stale} if stale else {})}
        fresh = self.doc(snapshot())["areas"][2]
        self.assertEqual((fresh["id"], fresh["at"], fresh["stale"]), ("wt", observed, ""))
        self.assertEqual(health_screen._lamp(fresh)["state"], "ok")
        health_screen._SCANS.clear()
        why = "the fleet refresh failed: git worktree list timed out"
        wt = self.doc(snapshot(why))["areas"][2]
        self.assertEqual((wt["at"], wt["stale"], wt["error"]), (observed, why, ""))
        lamp = health_screen._lamp(wt)
        self.assertEqual((lamp["stale"], lamp["state"]), (why, "unknown"), "a stale Worktrees read showed a green lamp")
        # A fleet with no `observed` (a test seam) is dated by Health's own read.
        health_screen._SCANS.clear()
        self.assertEqual(self.doc(fleet_of([]))["areas"][2]["at"], NOW)


class TheViews(Collectors, ScreenCase):
    """One view's areas, each view its own read, and a summary that waits on no walker (the Health split).

    The one document waited on every walker at once, up to `PAGE_SECONDS`, and a reader that raised an error its area's
    guard does not name took the whole page with it.
    """

    def seams(self, **kwargs):
        return {"fleet": fleet_of(TREES), "ports": ports_snapshot, "protection": protection_of(PROTECTION), **kwargs}

    def test_a_view_reads_only_its_areas(self):
        called = []

        def ports():
            called.append("ports")
            return ports_snapshot()
        found = health_screen.part(self.connection, "checkouts", now=NOW, **self.seams(ports=ports))
        self.assertEqual((found["view"], [area["id"] for area in found["areas"]]), ("checkouts", ["wt", "br"]))
        self.assertEqual({area["view"] for area in found["areas"]}, {"checkouts"})
        self.assertEqual(called, [], "the Checkouts view ran the port inventory")
        self.assertEqual([row["id"] for row in found["areas"][0]["rows"]], ["gone:group/alpha", "unread:beta"])
        every = [key for _, _, keys in health_screen.VIEWS for key in keys]
        self.assertEqual(sorted(every), sorted(key for key, *_ in health_screen.AREAS), "an area is in no view, or in two")

    def test_a_view_that_raises_marks_its_areas_unknown_and_leaves_the_other_views(self):
        def broken(area):
            raise ZeroDivisionError("a reader bug no area guard names")
        seams = self.seams(fleet=broken)
        failed = health_screen.part(self.connection, "checkouts", now=NOW, **seams)
        self.assertEqual([(area["id"], area["rows"]) for area in failed["areas"]], [("wt", []), ("br", [])])
        for area in failed["areas"]:
            self.assertIn("the Checkouts read failed: ZeroDivisionError('a reader bug no area guard names')", area["error"])
        github = health_screen.part(self.connection, "github", now=NOW, **seams)
        self.assertEqual([(area["id"], area["error"]) for area in github["areas"]], [("dep", ""), ("sec", ""), ("prot", "")])
        self.assertTrue(github["areas"][2]["rows"])
        lamps = {lamp["id"]: lamp for lamp in health_screen.summary(self.connection, now=NOW, **seams)["lamps"]}
        self.assertEqual((lamps["prot"]["state"], lamps["prot"]["error"]), ("warning", ""))

    def test_the_summary_waits_on_no_walker_and_builds_no_full_document(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def blocked(area):
            release.wait(10)
            return fleet_of(TREES)(area)
        started = time.monotonic()
        with patch.object(health_screen, "document", side_effect=AssertionError("the summary built the document")):
            found = health_screen.summary(self.connection, now=NOW, **self.seams(fleet=blocked))
        self.assertLess(time.monotonic() - started, 5)  # a walker waited on holds it 10 s (sd:6468)
        lamps = {lamp["id"]: lamp for lamp in found["lamps"]}
        self.assertEqual(list(lamps), [key for key, *_ in health_screen.AREAS])
        # The database-only areas are read now; a walker no read has answered is unknown and says why.
        self.assertEqual(lamps["prot"]["state"], "warning")
        self.assertEqual((lamps["wt"]["state"], lamps["wt"]["at"]), ("unknown", None))
        self.assertEqual(lamps["wt"]["error"], f"being read since {NOW}; read Health again for its answer")
        self.assertEqual(lamps["svc"]["error"], "not read since the dashboard started; the Machine view reads it")
        self.assertEqual(set(found["attention"]), {"state", "n", "what"})
        self.assertEqual([view["id"] for view in found["views"]], ["checkouts", "machine", "github", "jobs", "status"])
        # A lamp counts the rows that want you; an area not read has no count, so its view has none either.
        prot = next(area for area in health_screen.part(self.connection, "github", now=NOW, **self.seams())["areas"] if area["id"] == "prot")
        wanting = sum(row["state"] in ("warning", "caution", "unknown") for row in prot["rows"])
        self.assertGreater(wanting, 0)
        self.assertEqual((lamps["prot"]["n"], lamps["wt"]["n"]), (wanting, None))

    def test_the_summary_shows_what_the_last_view_read_left_and_counts_its_rows(self):
        machine = health_screen.part(self.connection, "machine", now=NOW, **self.seams())
        health_screen.part(self.connection, "checkouts", now=NOW, **self.seams())
        later = "2026-09-06T12:30:00Z"
        with patch.object(health_screen, "SUMMARY_FRESH_SECONDS", 10 ** 6):
            found = health_screen.summary(self.connection, now=later, **self.seams())
        lamps = {lamp["id"]: lamp for lamp in found["lamps"]}
        self.assertEqual((lamps["svc"]["state"], lamps["svc"]["at"], lamps["svc"]["error"]), ("ok", NOW, ""))
        self.assertEqual(lamps["wt"]["value"], [{"b": "2", "t": "dir gone"}])
        disk = next(area for area in machine["areas"] if area["id"] == "disk")
        fullest = max(disk["extra"]["volumes"], key=lambda volume: volume["capacity"])
        self.assertEqual(lamps["disk"]["value"], [{"b": f"{fullest['capacity']}%", "t": "fullest"}, {"b": "", "t": fullest["name"]}])
        full = health_screen.document(self.connection, now=later, **self.seams())
        self.assertEqual(found["attention"], full["attention"], "the summary's badge differs from the document's")

    def test_a_failed_scan_is_not_replaced_by_the_answer_before_it(self):
        health_screen.part(self.connection, "checkouts", now=NOW, **self.seams())
        later = "2026-09-06T12:30:00Z"

        def broken(area):
            raise ValueError("fleet collection was stopped at its budget: sessions")
        failed = health_screen.part(self.connection, "checkouts", now=later, **self.seams(fleet=broken))
        self.assertTrue(failed["areas"][0]["error"])
        with patch.object(health_screen, "SUMMARY_FRESH_SECONDS", 10 ** 6):
            lamps = {lamp["id"]: lamp for lamp in health_screen.summary(self.connection, now=later, **self.seams())["lamps"]}
        self.assertEqual((lamps["wt"]["state"], lamps["wt"]["error"]),
                         ("unknown", "fleet collection was stopped at its budget: sessions"))

    def failing_summary(self, later):
        """A summary at `later` whose background scan of every pooled area raises, waited on until each has failed."""
        def broken(*_args, **_kwargs):
            raise OSError("the mount did not answer")
        seams = self.seams(fleet=broken, ports=broken, disk=broken, branches=broken, doctor=broken)
        health_screen.summary(self.connection, now=later, **seams)
        for key in health_screen.POOLED:
            self.assertIsInstance(health_screen._SCANS.running[key][1].exception(timeout=5), OSError, key)
        return {lamp["id"]: lamp for lamp in health_screen.summary(self.connection, now=later, **seams)["lamps"]}

    def test_a_failed_background_refresh_marks_each_pooled_areas_kept_rows_stale(self):
        # sd:4960 review: a failed summary scan dropped its answer but left the view's area, served with no marker.
        for view, *_ in health_screen.VIEWS:
            health_screen.part(self.connection, view, now=NOW, **self.seams())
        later = "2026-09-06T12:30:00Z"
        lamps = self.failing_summary(later)
        for key in health_screen.POOLED:
            with self.subTest(area=key):
                lamp = lamps[key]
                self.assertEqual((lamp["error"], lamp["at"]), ("", NOW))
                self.assertEqual(lamp["stale"], f"the refresh at {later} failed: the mount did not answer; "
                                                f"these rows are the read of {NOW}")
                self.assertNotEqual(lamp["state"], "ok", "a stale area showed a green lamp")
                self.assertIsNotNone(lamp["n"], "the kept rows were dropped")

    def test_a_failed_background_refresh_with_no_read_before_it_is_its_error(self):
        later = "2026-09-06T12:30:00Z"
        lamps = self.failing_summary(later)
        for key in health_screen.POOLED:
            with self.subTest(area=key):
                self.assertEqual((lamps[key]["state"], lamps[key]["at"], lamps[key]["error"]),
                                 ("unknown", later, f"the refresh at {later} failed: the mount did not answer"))

    def test_a_view_read_after_a_failed_refresh_clears_the_mark(self):
        for view, *_ in health_screen.VIEWS:
            health_screen.part(self.connection, view, now=NOW, **self.seams())
        self.failing_summary("2026-09-06T12:30:00Z")
        newer = "2026-09-06T12:40:00Z"
        health_screen.part(self.connection, "checkouts", now=newer, **self.seams())
        with patch.object(health_screen, "SUMMARY_FRESH_SECONDS", 10 ** 6):
            lamps = {lamp["id"]: lamp for lamp in health_screen.summary(self.connection, now=newer, **self.seams())["lamps"]}
        self.assertEqual((lamps["wt"]["stale"], lamps["wt"]["error"], lamps["wt"]["at"]), ("", "", newer))

    def test_a_scan_that_finishes_after_its_view_was_cut_short_shows_in_the_next_summary(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def slow(area):
            release.wait(10)
            return fleet_of(TREES)(area)
        with patch.object(health_screen, "PAGE_SECONDS", 0.2):
            cut = health_screen.part(self.connection, "checkouts", now=NOW, **self.seams(fleet=slow))
        self.assertIn("still running at the page's budget", cut["areas"][0]["error"])
        release.set()
        health_screen._SCANS.running["wt"][1].result(timeout=5)
        with patch.object(health_screen, "SUMMARY_FRESH_SECONDS", 10 ** 6):
            lamps = {lamp["id"]: lamp for lamp in health_screen.summary(self.connection, now="2026-09-06T12:30:00Z",
                                                                         **self.seams())["lamps"]}
        self.assertEqual((lamps["wt"]["state"], lamps["wt"]["error"], lamps["wt"]["at"]), ("caution", "", NOW))


class TheMachines(Collectors, ScreenCase):
    """sd:9757: each machine's stored reading (`sd services list --record`), chosen by host in the Machine view.

    A satellite writes its Disk and Services readings to the database as a `machine-health:<host>` heartbeat; the hub
    reads them back, never the satellite. A reading older than the interval it names is stale, never current.
    """

    def reading(self, host, stamp, interval=3600):
        record_state(self.connection, "heartbeat", key=health_screen.MACHINE_PREFIX + host, timestamp=stamp,
                     body={"interval_seconds": interval, "disk": DISK, "services": SERVICES["services"]})

    def test_a_satellite_reading_renders_under_its_host(self):
        self.reading("sat-a", "2026-09-06T11:30:00Z")
        found = health_screen.part(self.connection, "machine", now=NOW, host="sat-a")
        self.assertEqual(found["host"], "sat-a")
        self.assertEqual([(area["id"], area["at"], area["stale"], area["error"]) for area in found["areas"]],
                         [("disk", "2026-09-06T11:30:00Z", "", ""), ("svc", "2026-09-06T11:30:00Z", "", "")])
        disk, svc = (area["rows"] for area in found["areas"])
        self.assertIn("sat-a:vol:/System/Volumes/Data", [row["id"] for row in disk])
        self.assertTrue(svc)
        self.assertTrue(all(row["id"].startswith("sat-a:") for row in disk + svc), "a satellite row shares a hub row's id")
        self.assertFalse([row["id"] for row in disk if "build:" in row["id"]], "the hub's worktree rule read a satellite")
        self.assertFalse([row["id"] for row in svc if row.get("can") or row.get("kickstart")],
                         "a satellite's service offers the hub's controls")

    def test_a_reading_older_than_its_interval_is_marked_stale(self):
        self.reading("sat-a", "2026-09-06T10:00:00Z", interval=3600)
        found = health_screen.part(self.connection, "machine", now=NOW, host="sat-a")
        for area in found["areas"]:
            self.assertIn("sat-a's reading of 2026-09-06T10:00:00Z is older than its interval of 60 minutes",
                          area["stale"])
            self.assertTrue(area["rows"], "a stale reading still shows its rows")

    def test_a_host_with_no_reading_is_unknown_not_clean(self):
        found = health_screen.part(self.connection, "machine", now=NOW, host="sat-z")
        self.assertEqual([(area["id"], area["rows"]) for area in found["areas"]], [("disk", []), ("svc", [])])
        for area in found["areas"]:
            self.assertIn("no reading from sat-z", area["error"])

    def test_the_chooser_lists_the_hub_first_then_every_host_with_readings(self):
        for host in ("sat-b", "hub-a", "sat-a"):
            self.reading(host, "2026-09-06T11:30:00Z")
        with patch.object(health_screen, "this_host", lambda: "hub-a"):
            self.assertEqual(health_screen.machines(self.connection), {"hub": "hub-a", "hosts": ["hub-a", "sat-a", "sat-b"]})
            found = health_screen.part(self.connection, "machine", now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot)
        self.assertEqual(found["machines"]["hosts"], ["hub-a", "sat-a", "sat-b"])
        self.assertNotIn("host", found)

    def test_record_writes_this_machines_reading_keyed_by_its_host(self):
        with patch.object(health_collectors, "this_host", lambda: "sat-a"):
            body = health_collectors.record_reading(self.connection, interval=900, disk=DISK,
                                                    services=SERVICES)
        stamp, stored = health_screen.read_machine(self.connection, "sat-a")
        self.assertEqual((stored, body["interval_seconds"]), (body, 900))
        self.assertEqual([volume["mount"] for volume in stored["disk"]["volumes"]],
                         [volume["mount"] for volume in DISK["volumes"]])
        self.assertEqual(len(stored["services"]), len(SERVICES["services"]))


class ThePage(Collectors, BrowserSession):
    fleet_backend = staticmethod(fleet_of(TREES))

    def setUp(self):
        super().setUp()
        # No lsof or docker runs in a test: the Ports area reads a fixture.
        self.listening.RequestHandlerClass.ports_backend = staticmethod(ports_snapshot)

    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/fleet-health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Health · system</title>", body)
        self.assertRegex(body, r'<meta name="sd-csrf" content="[a-f0-9]{64}"></head>')
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "snooze.js", "commands.js", "health.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)
        # /health is the service's own check, which the runtime reads; the page does not take it over.
        status, _, body = self.request("/health")
        self.assertEqual(json.loads(body)["service"], "sd-dashboard")

    def test_the_data_route_needs_a_session_and_takes_no_query(self):
        self.assertEqual(self.request("/api/health")[0], 403)
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/health?area=wt", headers=cookie)[0], 400)
        status, _, body = self.request("/api/health", headers=cookie)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertRegex(doc["read"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual([row["id"] for row in doc["areas"][2]["rows"]], ["gone:group/alpha", "unread:beta"])
        self.assertEqual(len(doc["areas"][6]["rows"]), 3)

    def test_the_machine_view_answers_for_a_host_its_query_names(self):
        """sd:9757: `?host=` reads a satellite's stored reading; any other query, or a host name with a path in it, is a 400."""
        cookie = {"Cookie": self.cookie}
        record_state(self.connection, "heartbeat", key=health_screen.MACHINE_PREFIX + "sat-a",
                     body={"interval_seconds": 3600, "disk": DISK, "services": SERVICES["services"]})
        self.connection.commit()
        status, _, body = self.request("/api/health/machine?host=sat-a", headers=cookie)
        doc = json.loads(body)
        self.assertEqual((status, doc["host"], [area["id"] for area in doc["areas"]]), (200, "sat-a", ["disk", "svc"]))
        self.assertIn("sat-a", doc["machines"]["hosts"])
        for bad in ("/api/health/machine?host=../x", "/api/health/machine?area=disk", "/api/health/github?host=sat-a"):
            self.assertEqual(self.request(bad, headers=cookie)[0], 400, bad)

    def test_each_view_and_the_summary_answer_alone_and_another_name_is_a_404(self):
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.request("/api/health/checkouts")[0], 403)
        self.assertEqual(self.request("/api/health/checkouts?area=wt", headers=cookie)[0], 400)
        status, _, body = self.request("/api/health/checkouts", headers=cookie)
        self.assertEqual((status, [area["id"] for area in json.loads(body)["areas"]]), (200, ["wt", "br"]))
        status, _, body = self.request("/api/health/summary", headers=cookie)
        self.assertEqual((status, len(json.loads(body)["lamps"])), (200, len(health_screen.AREAS)))
        status, _, body = self.request("/api/health/nowhere", headers=cookie)
        self.assertEqual(status, 404)
        self.assertIn("Health has no view 'nowhere'", json.loads(body)["error"])

    def free_slots(self):
        """How many read slots a new read could take now (sd:7842), each given back at once."""
        slots, taken = self.listening.RequestHandlerClass.reads, 0
        while taken < server.READ_SLOTS and slots.acquire(blocking=False):
            taken += 1
        for _ in range(taken):
            slots.release()
        return taken

    def test_a_view_waiting_on_a_walker_holds_no_read_slot(self):
        # sd:7898: Disk took its whole 8 s budget on each read, and the view held a read slot all that time, so under
        # load Health's reads filled the slots and the page's other reads waited behind them.
        gate, started = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        def slow(connection, **_):
            started.set()
            gate.wait(10)
            return DISK
        answers = []
        with patch.object(health_collectors, "disk_scan", slow):
            reader = threading.Thread(target=lambda: answers.append(
                self.request("/api/health/machine", headers={"Cookie": self.cookie})))
            reader.start()
            self.assertTrue(started.wait(10), "the Disk scan never started")
            deadline = time.monotonic() + 5
            while self.free_slots() < server.READ_SLOTS and time.monotonic() < deadline:
                time.sleep(0.02)
            free = self.free_slots()
            gate.set()
            reader.join(20)
        self.assertEqual(free, server.READ_SLOTS, "the view held a read slot while it waited on the Disk walker")
        (status, _, body), = answers
        disk = next(area for area in json.loads(body)["areas"] if area["id"] == "disk")
        self.assertEqual((status, disk["error"], disk["stale"]), (200, "", ""))

    def test_the_rail_badge_comes_from_the_summary(self):
        page = next(page for page in v2.registry.PAGES if page.section == "Health")
        self.assertEqual((page.count, page.live), ("/api/health/summary", False))
        self.request("/api/health/summary", headers={"Cookie": self.cookie})
        badge = self.listening.RequestHandlerClass.badges.read()["Health"]
        self.assertIn(badge["state"], ("ok", "caution", "warning"))

    def test_the_rail_opens_health_at_its_page(self):
        self.assertEqual(v2.SECTIONS["Health"], "/fleet-health")
        self.assertNotIn("Health", v2.CLASSIC)
        # Health's "Open Sessions in Management" opens Management's Sessions view (pack sd:7517).
        self.assertIn("location.href = '/management#view=sessions'", HEALTH_JS)
        self.assertIn("{ id: 'sessions', label: 'Sessions' }", (V2 / "static" / "management.js").read_text(encoding="utf-8"))


# Extras the Health script reads from the shell that the Tasks stand-in does not carry.
HEALTH_SHELL = r"""
delete window.shell.view; // the Tasks stand-in's stub; VIEW_SHELL below brings the switch health.js meets
window.shell.shq = s => "'" + String(s).replace(/'/g, "'\\''") + "'";
window.shell.row = () => null;
window.shell.url = q => { OUT.url = String(q && q.toString ? q.toString() : q); };
window.shell.state = (s, at) => { OUT.states.push(s); (OUT.slots = OUT.slots || []).push(at || null); };
"""


SNOOZED_TYPES = ("check", "doctor check", "storage folder", "build output", "volume", "worktree registrations", "unread registrations",
                 "merged branches", "merged worktrees", "port", "branch protection", "dependabot alerts", "secret scanning", "credential", "service",
                 "dashboard service", "launch agent", "vault mirror", "service status")


def summary_of(doc):
    """What `/api/health/summary` answers for the areas in `doc`: the views, each area's lamp and the badge."""
    return {"read": doc["read"], "views": [{"id": view, "name": name, "areas": list(keys)} for view, name, keys in health_screen.VIEWS],
            "lamps": [health_screen._lamp(area) for area in doc["areas"]], "attention": doc["attention"],
            "snooze_error": doc["snooze_error"]}


def wanting(doc, view):
    """The rows of `view` in `doc` that want the operator: warning, caution or unknown."""
    return sum(row["state"] in ("warning", "caution", "unknown") for area in doc["areas"] if area["view"] == view for row in area["rows"])


# The Health reads: /api/health/summary, and /api/health/<view> as `doc`'s areas of that view. STATUS fails both.
HEALTH_ANSWER = r"""
var HEALTH_READ = path => /^\/api\/health\/[a-z]+$/.test(path) && path !== '/api/health/cleanup';
var HEALTH = path => { const v = path.slice('/api/health/'.length);
  if (STATUS >= 300) return [STATUS, DOC];
  return v === 'summary' ? [200, SUMMARY_DOC] : [200, { ...DOC, view: v, areas: DOC.areas.filter(a => a.view === v) }]; };
var history = { replaceState: (s, t, u) => { OUT.address = u; location.hash = u.slice(u.indexOf('#')); } };
ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path) : [404, { error: 'no answer' }];
var PANEL = () => ELS['view-' + window.shell.view.current()], BODY = () => ELS['body-' + window.shell.view.current()];
"""


# The shell's view switch (shell.js, view:start) as health.js meets it: shell.js loads after the page, shows the hash's view
# at once, and fires the first shell:view after the page's DOMContentLoaded listener. A test that brings its own shell.view
# keeps it.
VIEW_SHELL = r"""
if (!window.shell.view) (() => {
  const seen = new Set(); let now = null, started = false; OUT.counts = OUT.counts || {};
  const fire = id => { const first = !seen.has(id); seen.add(id);
    document.dispatchEvent(new CustomEvent('shell:view', { detail: { id, first, api: PAGE_VIEWS.find(v => v.id === id).api } })); };
  const apply = id => { if (id === now) return; now = id;
    PAGE_VIEWS.forEach(v => { document.getElementById('view-' + v.id).hidden = v.id !== id; }); if (started) fire(id); };
  const show = id => { if (!PAGE_VIEWS.some(v => v.id === id)) return false;
    if (location.hash !== '#view=' + id) { OUT.address = location.pathname + '#view=' + id; location.hash = '#view=' + id; }
    apply(id); return true; };
  window.shell.view = { current: () => now, show, count: (id, n) => { OUT.counts[id] = n; } };
  const m = /^#view=([a-z0-9-]+)$/.exec(location.hash);
  apply(m && PAGE_VIEWS.some(v => v.id === m[1]) ? m[1] : PAGE_VIEWS[0].id);
  document.addEventListener('DOMContentLoaded', () => { started = true; fire(now); });
})();
"""


class TheScript(Collectors, ScreenCase):
    """health.js against the document `health_screen` builds from the fixtures above, one view at a time."""

    def run_doc(self):
        return health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot,
                                      protection=protection_of(PROTECTION))

    def run_page(self, body, doc=None, status=200, view="checkouts", search="", pre=""):
        doc = doc if doc is not None else self.run_doc()
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + HEALTH_SHELL + READ_SHELL
                  + f"\nvar DOC = {json.dumps(doc)}, SUMMARY_DOC = {json.dumps(summary_of(doc))}, STATUS = {status};\n"
                  + f"location.pathname = '/fleet-health'; location.hash = {json.dumps(f'#view={view}' if view else '')}; location.search = {json.dumps(search)};\n"
                  + "URLSearchParams.prototype.toString = function () { return ''; };\n" + HEALTH_ANSWER + pre
                  + SNOOZE_JS + COMMANDS_JS + HEALTH_JS + VIEW_SHELL + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\n"
                  + "(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    #: The viewer's localStorage, which the stand-in page lacks: the Machine chooser keeps its pick there (sd:9757).
    STORE = "var STORE = {}; var localStorage = { getItem: k => k in STORE ? STORE[k] : null, setItem: (k, v) => { STORE[k] = String(v); } };\n"

    def machine_docs(self):
        """The hub's document with its chooser, and sat-a's stored reading as `/api/health/machine?host=sat-a` answers it."""
        record_state(self.connection, "heartbeat", key=health_screen.MACHINE_PREFIX + "sat-a", timestamp="2026-09-06T11:30:00Z",
                     body={"interval_seconds": 3600, "disk": DISK, "services": SERVICES["services"]})
        doc = {**self.run_doc(), "machines": {"hub": "hub-a", "hosts": ["hub-a", "sat-a"]}}
        with patch.object(health_screen, "this_host", lambda: "hub-a"):
            sat = health_screen.part(self.connection, "machine", now=NOW, host="sat-a")
        answer = (f"var SAT = {json.dumps(sat)};\n"
                  "ANSWER = (path, body) => path === '/api/health/machine?host=sat-a' ? [200, SAT] : HEALTH_READ(path) ? HEALTH(path) : [404, { error: 'no answer' }];\n")
        return doc, answer

    def test_the_machine_view_chooses_a_host_and_keeps_the_choice(self):
        """sd:9757: a combobox lists the hub first, then each host with readings; a pick reads that host's stored reading."""
        doc, answer = self.machine_docs()
        out = self.run_page("""R.before = BODY().html; OUT.gets = [];
PANEL().listeners.change[0]({ target: { id: 'machine-host', value: 'sat-a', closest: () => null } }); await flush();
R.after = BODY().html; R.kept = STORE['health.machine'];""", doc, view="machine", pre=self.STORE + answer)
        before, after = out["R"]["before"], out["R"]["after"]
        self.assertEqual(re.findall(r'<option value="([^"]+)"', before), ["hub-a", "sat-a"])
        self.assertRegex(before, r'<select id="machine-host"[^>]*>\s*<option value="hub-a" selected>')
        self.assertEqual(out["gets"][0], "/api/health/machine?host=sat-a")
        self.assertIn('data-id="sat-a:vol:/System/Volumes/Data"', after)
        self.assertRegex(after, r'<option value="sat-a" selected>')
        self.assertIn("Readings of sat-a", after)
        self.assertEqual(out["R"]["kept"], "sat-a")

    def test_a_kept_host_is_read_when_the_machine_view_opens(self):
        doc, answer = self.machine_docs()
        out = self.run_page("R.areas = BODY().html;", doc, view="machine",
                            pre=self.STORE + "STORE['health.machine'] = 'sat-a';\n" + answer)
        self.assertIn("/api/health/machine?host=sat-a", out["gets"])
        self.assertNotIn("/api/health/machine", out["gets"], "the hub's machine was read for a viewer who chose sat-a")
        self.assertIn('data-id="sat-a:vol:/System/Volumes/Data"', out["R"]["areas"])

    def test_every_command_is_registered_with_the_design_id_risk_and_key(self):
        out = self.run_page("""R.reg = REG.map(c => [c.id, c.on, c.risk, c.key || null, typeof c.executes === 'boolean' ? c.executes : null, !!c.bulk]);""")
        self.assertEqual(out["R"]["reg"], [
            ["check.recheck", "check", "safe", "e", True, False],
            ["doctor check.run", "doctor check", "safe", "e", True, False],
            ["doctor check.fix", "doctor check", "safe", "f", True, False],
            ["storage folder.du", "storage folder", "safe", "b", True, False],
            ["build output.rm", "build output", "confirm", "r", True, False],
            ["volume.df", "volume", "safe", "b", True, False],
            ["worktree registrations.prune", "worktree registrations", "confirm", "p", True, False],
            ["merged branches.delete", "merged branches", "confirm", "d", True, False],
            ["merged worktrees.remove", "merged worktrees", "confirm", "r", True, False],
            ["safe cleanups.clean", "safe cleanups", "confirm", "a", True, False],
            ["service.restart", "service", "confirm", "r", True, False],
            ["service.start", "service", "confirm", "s", True, False],
            ["dashboard service.kickstart", "dashboard service", "safe", "l", False, False],
            ["port.inspect", "port", "safe", "i", True, False],
            ["protection.settings", "branch protection", "safe", "o", False, False],
            ["collector.sync", "collector", "safe", "r", None, False],
            ["dependabot alerts.review", "dependabot alerts", "safe", "o", False, False],
            ["dependabot alerts.fix", "dependabot alerts", "safe", "f", True, False],
            ["secret scanning.review", "secret scanning", "safe", "o", False, False],
            ["credential.probe", "credential", "safe", "r", True, False],
            ["credential.signin", "credential", "safe", "i", True, False],
            ["service status.open", "service status", "safe", "o", False, False],
        ] + [[f"{t}.snooze", t, "undo", "z", None, True] for t in SNOOZED_TYPES]
          + [[f"{t}.ignore", t, "undo", "h", None, True] for t in SNOOZED_TYPES]
          + [["snoozed row.unsnooze", "snoozed row", "undo", "s", None, True],
             ["ignored row.unignore", "ignored row", "undo", "s", None, True]])

    def test_a_doctor_row_lists_run_doctor_and_fix_and_fix_posts_the_fix_route_for_the_filed_item(self):
        """pack sd:6684: a WARN doctor row runs the doctor, and Fix files its finding (or reuses the open item) and starts a
        session on it; the passing row runs the doctor but has nothing to fix."""
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot,
                                     protection=protection_of(PROTECTION), doctor=lambda: DOCTOR)
        rows = next(area for area in doc["areas"] if area["id"] == "doc")["rows"]
        warn = next(row for row in rows if row["state"] == "caution")
        out = self.run_page(
            "ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path) : path === '/api/items' ? [201, { item: { id: 42 }, revision: 'r42' }]"
            " : path === '/api/items/42/fix' ? [200, { fix: { session: 'builder', pane: 'p1' } }] : [404, { error: 'no answer' }];\n"
            f"var o = C.get({json.dumps(warn['id'])}), k = C.get('doc:ok');\n"
            "const on = x => REG.filter(c => c.on === x.type).filter(c => !c.id.endsWith('.snooze') && !c.id.endsWith('.ignore'))"
            ".map(c => [c.label, c.when ? c.when(x) : true]);\n"
            "R.warn = on(o); R.ok = on(k);\n"
            "shellRun(REG.find(c => c.on === o.type && c.label === 'Fix'), o); await flush();", doc=doc, view="machine")
        self.assertEqual(out["R"]["warn"], [["Run doctor", True], ["Fix", True]])
        self.assertEqual(out["R"]["ok"], [["Run doctor", True], ["Fix", "a passing doctor line has nothing to fix"]])
        self.assertEqual([post[:2] for post in out["posts"]], [
            ["/api/items", {"title": warn["what"], "body": f"Doctor: {warn['detail']}", "ref": "doctor:" + warn["id"][4:]}],
            ["/api/items/42/fix", {"revision": "r42"}]])
        self.assertEqual(out["toasts"][-1][0], "Fix started · #42 · builder session in herdr pane p1")

    def test_review_on_github_opens_the_repos_alert_page_in_a_new_tab(self):
        """sd:5628. Review on GitHub opens the alerts page, as Open branch settings does; the gh line stays for copying."""
        out = self.run_page("""R.opened = []; window.open = (u, t) => R.opened.push([u, t]);
R.said = ['dependabot alerts.review', 'secret scanning.review'].map(id => cmd(id).run({ slug: 'example/anvil', label: 'anvil' }));""")
        self.assertEqual(out["R"]["opened"], [["https://github.com/example/anvil/security/dependabot", "_blank"],
                                              ["https://github.com/example/anvil/security/secret-scanning", "_blank"]])
        self.assertEqual(out["R"]["said"], ["Opened on GitHub in a new tab"] * 2)

    def test_fix_on_a_dependabot_row_files_the_repos_item_then_posts_its_fix_route(self):
        """sd:6685: Fix files (or reuses) the repo's `dependabot:<slug>` item, then posts /api/items/<id>/fix."""
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot,
                                     protection=protection_of(ALERTED))
        out = self.run_page("""ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path)
  : path === '/api/items' ? [201, { item: { id: 77 }, revision: 'a'.repeat(64) }]
  : path === '/api/items/77/fix' ? [200, { fix: { session: 'builder', pane: 'p1' } }] : [404, { error: 'no answer' }];
const c = cmd('dependabot alerts.fix'), o = C.get('dep:/checkouts/alpha');
R.label = c.label; R.ask = !!c.askFirst; R.on = c.when(o); R.says = c.consequence(o);
shellRun(c, o); await flush();""", doc, view="github")
        self.assertEqual((out["R"]["label"], out["R"]["ask"], out["R"]["on"]), ("Fix", True, True))
        self.assertIn("group/alpha", out["R"]["says"])
        posts = [post[:2] for post in out["posts"]]
        self.assertEqual(posts[0][0], "/api/items")
        self.assertEqual({k: posts[0][1][k] for k in ("repo", "ref")},
                         {"repo": "/checkouts/alpha", "ref": "dependabot:group/alpha"})
        self.assertIn("1 critical or high", posts[0][1]["body"])
        self.assertEqual(posts[1], ["/api/items/77/fix", {"revision": "a" * 64}])
        self.assertEqual(out["toasts"][-1][0], "Fix started · #77 · builder session in herdr pane p1")

    def test_an_area_with_no_reader_is_an_unknown_lamp_that_names_what_it_does_not_read(self):
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot, protection=protection_of(PROTECTION))
        cred = next(area for area in doc["areas"] if area["id"] == "cred")
        cred.update(read=False, source=None, rows=[], at=None,
                    missing=["GitHub PAT presence and expiry", "gh CLI sign-in"])
        out = self.run_page("R.lamps = ELS.annunciator.html; R.areas = BODY().html; R.sub = ELS.subhead.html;", doc)
        self.assertEqual(out["gets"], ["/api/health/summary", "/api/health/checkouts", "/api/health/summary"])
        lamps = out["R"]["lamps"]
        self.assertRegex(lamps, r'data-area="cred" data-state="unknown"[^>]*>.*?not read')
        for area in ("dep", "sec"):
            self.assertRegex(lamps, rf'data-area="{area}" data-state="unknown"[^>]*><span class="lbl">[^<]*<svg[^>]*><use[^>]*/></svg></span><span class="val"><span class="ph">not read</span>', area)
        self.assertRegex(lamps, r'data-area="wt" data-state="caution"[^>]*>.*?<b>2</b> dir gone')
        self.assertRegex(lamps, r'data-area="disk" data-state="caution"[^>]*>.*?<b>85%</b> fullest</span> · <span class="ph">Mac data')
        self.assertRegex(lamps, r'data-area="br" data-state="caution"[^>]*>.*?<b>3</b> merged, not deleted')
        self.assertEqual({tag for tag, _ in cell_grammar(self, lamps)}, {"button"})
        areas = out["R"]["areas"]
        # The Checkouts view draws its own areas only.
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', areas), ["wt", "br"])
        self.assertNotIn("<b>Not read here:</b>", areas)
        self.assertIn('data-id="gone:group/alpha"', areas)
        self.assertIn("5 views · 1 warning row wants you · read", out["R"]["sub"])
        self.assertEqual(out["attention"][-1], {"state": "warning", "n": 1, "what": "findings want you"})
        self.assertIsNone(out["states"][-1])
        jobs = self.run_page("R.areas = BODY().html;", doc, view="jobs")["R"]["areas"]
        self.assertIn("<b>No reader yet.</b> The dashboard has no collector for this area, so nothing here is known: GitHub PAT presence and expiry", jobs)
        machine = self.run_page("R.areas = BODY().html;", doc, view="machine")["R"]["areas"]
        self.assertIn("<b>Not read here:</b> build output sizes", machine)

    def test_an_alert_lamp_keeps_a_paged_total_and_says_when_a_repo_was_not_read(self):
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot, protection=protection_of(ALERTED))
        lamps = self.run_page("R.lamps = ELS.annunciator.html;", doc)["R"]["lamps"]
        self.assertRegex(lamps, r'data-area="dep"[^>]*>.*?<b>102\+</b> open alerts</span> · <span class="ph">not all read</span>')
        self.assertRegex(lamps, r'data-area="sec"[^>]*>.*?<b>1</b> open alerts</span> · <span class="ph">not all read</span>')

    def test_a_failed_reader_is_a_partial_read_and_its_lamp_is_unknown(self):
        def broken(area):
            raise ValueError("fleet collection was stopped at its budget: sessions")
        doc = health_screen.document(self.connection, now=NOW, fleet=broken, ports=ports_snapshot,
                                     protection=protection_of([]), disk=scan_of(CLEAN_DISK), branches=scan_of(CLEAN_BRANCHES))
        out = self.run_page("R.lamps = ELS.annunciator.html; R.areas = BODY().html;", doc)
        self.assertRegex(out["R"]["lamps"], r'data-area="wt" data-state="unknown"[^>]*>.*?not read')
        self.assertIn("<b>Not read:</b> fleet collection was stopped at its budget: sessions", out["R"]["areas"])
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertEqual(out["attention"][-1], {"state": "ok", "n": 0, "what": "findings to watch"})

    def test_a_stale_area_keeps_its_rows_and_says_it_was_not_re_read(self):
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot, protection=protection_of([]), disk=scan_of(CLEAN_DISK),
                                     branches=scan_of(CLEAN_BRANCHES))
        wt = doc["areas"][2]
        wt["stale"] = "the Worktrees reader was still running; these rows are the read of " + NOW
        out = self.run_page("R.areas = BODY().html;", doc)
        self.assertIn("<b>Not re-read:</b> the Worktrees reader was still running; these rows are the read of " + NOW,
                      out["R"]["areas"])
        self.assertIn(f'data-id="{wt["rows"][0]["id"]}"', out["R"]["areas"])
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("Worktrees: the Worktrees reader was still running", out["states"][-1]["text"])

    def test_a_document_that_does_not_arrive_leaves_no_row_and_says_so(self):
        out = self.run_page("""R.before = BODY().html;
DOC = { error: 'Open a dashboard page before reading Health.' }; STATUS = 403;
window.PAGE_AGAIN.run(); await flush();
R.areas = BODY().html; R.lamps = ELS.annunciator.html;""")
        self.assertEqual(out["gets"], ["/api/health/summary", "/api/health/checkouts", "/api/health/summary",
                                       "/api/health/checkouts", "/api/health/summary"])
        self.assertIn('data-id="gone:group/alpha"', out["R"]["before"])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertIn("Open a dashboard page before reading Health.", out["states"][-1]["text"])
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "rows"})
        self.assertNotIn("data-id=", out["R"]["areas"], "rows from the last read stayed on screen")
        self.assertNotIn('data-state="ok"', out["R"]["lamps"], "a lamp from the last read stayed lit")

    def test_the_filter_finds_a_row_by_a_path_in_its_facts_or_its_list(self):
        # The shell's Shapeshift bar sends its text as shell:filter (sd:5539): a registered worktree's path is in `list`, the
        # checkout's in `facts`.
        out = self.run_page("""const filter = v => { document.dispatchEvent(new CustomEvent('shell:filter', { detail: { q: v, view: 'checkouts' } })); return BODY().html; };
R.list = filter('/work/wt-b'); R.facts = filter('/checkouts/group/alpha'); R.none = filter('/work/nowhere');""")
        self.assertIn('data-id="gone:group/alpha"', out["R"]["list"], "a path in the row's list did not match")
        self.assertIn('data-id="gone:group/alpha"', out["R"]["facts"], "a path in the row's facts did not match")
        self.assertNotIn('data-id="gone:group/alpha"', out["R"]["none"])

    def test_a_refresh_retires_the_command_objects_and_picks_of_rows_it_no_longer_lists(self):
        # The shell keeps every object put and every pick; the bulk bar must not offer a prune built from a row that is gone.
        out = self.run_page("""var PICKED = new Set();
C.pick = id => { PICKED.has(id) ? PICKED.delete(id) : PICKED.add(id); document.dispatchEvent(new CustomEvent('shell:picked', { detail: [...PICKED] })); };
const refresh = () => window.PAGE_AGAIN.run();
const live = id => !!OBJ.get(id) && REG.some(c => c.on === OBJ.get(id).type);
C.pick('gone:group/alpha'); C.pick('unread:beta');
DOC = { ...DOC, areas: DOC.areas.map(a => ({ ...a, rows: a.rows.filter(r => r.id !== 'gone:group/alpha') })) };
refresh(); await flush();
R.dropped = [live('gone:group/alpha'), PICKED.has('gone:group/alpha')]; R.kept = [live('unread:beta'), PICKED.has('unread:beta')];
DOC = { error: 'Open a dashboard page before reading Health.' }; STATUS = 403;
refresh(); await flush();
R.failed = [live('unread:beta'), PICKED.has('unread:beta'), PICKED.size];""")
        self.assertEqual(out["R"]["dropped"], [False, False], "a row the refresh dropped kept its command object or pick")
        self.assertEqual(out["R"]["kept"], [True, True], "a row the refresh still lists lost its object or pick")
        self.assertEqual(out["R"]["failed"], [False, False, 0], "a failed refresh kept a row's command object or pick")

    def test_an_older_read_that_answers_last_does_not_replace_the_newer_one(self):
        # Two re-reads overlap on the threaded server and the first one answers last: once as a failed request, once
        # as an older reading.
        out = self.run_page("""var pending = [];
ANSWER = path => path === '/api/health/summary' ? [200, SUMMARY_DOC] : new Promise((answer, fail) => pending.push({ answer, fail }));
const refresh = () => window.PAGE_AGAIN.run();
refresh(); refresh(); await flush();
pending[1].answer([200, DOC]); await flush();
pending[0].fail(new Error('an older read that failed')); await flush();
R.areas = BODY().html;
refresh(); refresh(); await flush();
pending[3].answer([200, DOC]); await flush();
pending[2].answer([200, { ...DOC, read: '2026-09-01T00:00:00Z' }]); await flush();
R.observed = document.body.dataset.observed;""")
        self.assertEqual([path for path in out["gets"] if path != "/api/health/summary"], ["/api/health/checkouts"] * 5)
        self.assertNotEqual(out["R"]["observed"], "2026-09-01T00:00:00Z", "an older answer replaced the newer reading")
        self.assertNotIn("an older read that failed", json.dumps(out["states"]), "an older reading replaced the newer one")
        self.assertIsNone(out["states"][-1])
        self.assertIn('data-id="gone:group/alpha"', out["R"]["areas"])

    def test_a_lamp_shows_its_area_only_and_a_state_chip_narrows_the_rows(self):
        click = "{el}.listeners.click[0]({{ target: {{ closest: s => s === '{sel}' ? {{ id: '', dataset: {data} }} : null }} }});"
        # Credentials is in Jobs: its lamp opens that view with Credentials alone; pressed again it shows the whole view.
        # State starts on warning, caution and unknown (sd:7207): taking out the first two leaves unknown alone.
        out = self.run_page(click.format(el="ELS.annunciator", sel="button.cell", data="{ area: 'cred' }") + "await flush();"
                            + "R.cred = BODY().html; R.note = BODY().html; R.address = OUT.address;"
                            + click.format(el="ELS.annunciator", sel="button.cell", data="{ area: 'cred' }")
                            + click.format(el="PANEL()", sel="[data-f]", data="{ f: 'state', v: 'warning' }")
                            + click.format(el="PANEL()", sel="[data-f]", data="{ f: 'state', v: 'caution' }")
                            + "R.unknown = BODY().html;")
        cred, unknown = out["R"]["cred"], out["R"]["unknown"]
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', cred), ["cred"])
        # sd:5504: Activity's bar (shell.list.fbar): the lamp's area is a token, Clear all ends the bar, and the bar holds no
        # count; an area's title line counts its rows only while a filter hides some (sd:5281). No note line repeats the filter.
        bar = re.search(r'<div class="fbar".*?<div class="fsum">.*?</div></div>', out["R"]["note"], re.S).group(0)
        self.assertIn('class="tok" data-f="area" data-v="cred"', bar)
        self.assertTrue(bar.endswith('<div class="fsum"><button class="linkbtn" type="button" data-fclear>Clear all</button></div></div>'), bar[-160:])
        self.assertNotRegex(re.sub(r"<[^>]+>", " ", bar), r"\b\d+ of \d+\b")
        self.assertNotIn(" of ", re.search(r'id="a-cred".*?</p>', cred, re.S).group(0), "the lamp hides no row of its own area")
        self.assertNotIn("Filtered to", out["R"]["note"])
        self.assertEqual(out["R"]["address"], "/fleet-health#view=jobs")
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', unknown), ["cred", "mir"])
        self.assertEqual(re.findall(r'data-id="([^"]+)"', unknown), ["cred:none", "mir", "mir:briefs"])

    def test_an_areas_title_line_counts_its_rows_while_a_filter_hides_some_and_the_bar_holds_no_count(self):
        """sd:5504: Activity's bar on Health; the count moved from the bar's end to each area's title line (sd:5281)."""
        # State starts on warning, caution and unknown (sd:7207): taking out warning and unknown leaves caution alone.
        out = self.run_page("R.before = BODY().html;"
                            "PANEL().listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f: 'state', v: 'warning' } } : null } });"
                            "PANEL().listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f: 'state', v: 'unknown' } } : null } });"
                            "R.after = BODY().html;")
        def tallies(h):
            return re.findall(r'<p class="tally"><span>(\d+ of \d+ rows?)</span>', h)
        self.assertEqual(tallies(out["R"]["before"]), [])
        self.assertEqual(tallies(out["R"]["after"]), ["1 of 2 rows", "1 of 2 rows"], "caution leaves one of each area's two drawn rows")
        bar = re.search(r'<div class="fbar".*?<div class="fsum">.*?</div></div>', out["R"]["after"], re.S).group(0)
        self.assertNotRegex(re.sub(r"<[^>]+>", " ", bar), r"\b\d+ of \d+\b", "the bar holds no count")

    def test_a_second_lamp_adds_its_area_and_pressing_one_again_takes_it_out(self):
        click = "ELS.annunciator.listeners.click[0]({{ target: {{ closest: s => s === 'button.cell' ? {{ id: '', dataset: {{ area: '{a}' }} }} : null }} }});"
        out = self.run_page(click.format(a="wt") + click.format(a="br") + "R.both = BODY().html;"
                            + click.format(a="wt") + "R.one = BODY().html;")
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', out["R"]["both"]), ["wt", "br"])
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', out["R"]["one"]), ["br"])

    def test_a_lamp_press_draws_the_rows_of_its_area(self):
        # sd:3708: the filter read r.area, which only the copies in ROWS carried, so each section drew no row.
        click = "ELS.annunciator.listeners.click[0]({ target: { closest: s => s === 'button.cell' ? { id: '', dataset: { area: 'wt' } } : null } });"
        out = self.run_page(click + "R.wt = BODY().html; R.note = BODY().html;")
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', out["R"]["wt"]), ["wt"])
        self.assertIn('data-id="gone:group/alpha"', out["R"]["wt"], "a lamp press drew no row of its area")
        self.assertIn('data-id="unread:beta"', out["R"]["wt"])
        self.assertIn('data-fclear>Clear all</button>', out["R"]["note"])

    def test_ports_show_the_counts_line_and_warnings_and_protection_has_no_lamp(self):
        out = self.run_page("R.lamps = ELS.annunciator.html; R.areas = BODY().html; R.lsof = cmd('port.inspect').cli(C.get('port:svc-a:9010'));",
                            view="machine")
        lamps, areas = out["R"]["lamps"], out["R"]["areas"]
        self.assertEqual(out["R"]["lsof"], "lsof -nP -iTCP:9010 -sTCP:LISTEN")
        self.assertRegex(lamps, r'data-area="ports" data-state="unknown"[^>]*>.*?<b>1</b> unknown</span> · <span class="ph">2 listening')
        self.assertNotIn('data-area="prot"', lamps)
        self.assertIn('<p class="counts">2 configured ports · 2 listening · 1 unknown</p>', areas)
        self.assertIn("Listener state is unknown for 1 configured ports.", areas)

    def test_the_matrix_is_one_column_per_repo_and_an_unread_column_draws_no_cell(self):
        out = self.run_page("R.areas = BODY().html;", view="github")
        areas = out["R"]["areas"]
        cols = re.findall(r'<button type="button" class="pmx-col" data-row="([^"]+)" data-status="(\w+)"[^>]*>(.*?)</button>', areas, re.S)
        self.assertEqual([(row, status) for row, status, _ in cols], [
            ("prot:/checkouts/beta", "unprotected"), ("prot:/checkouts/gamma", "unknown"),
            ("prot:/checkouts/alpha", "protected"), ("prot:/checkouts/delta", "protected")])
        svg = {row: body for row, _, body in cols}
        self.assertNotIn("<rect", svg["prot:/checkouts/gamma"])
        self.assertEqual(svg["prot:/checkouts/alpha"].count('class="c-gap"'), 2)
        self.assertEqual(svg["prot:/checkouts/beta"].count('class="c-gap"'), 1)
        self.assertEqual(svg["prot:/checkouts/delta"].count('class="c-gap"'), 0)
        self.assertIn('aria-label="/checkouts/beta: unprotected, 1 gap"', areas)
        self.assertIn('aria-label="group/gamma: unknown"', areas)
        self.assertIn("1 unknown, all for one reason: token does not reach it.", areas)
        self.assertIn('<p class="counts">2 protected · 1 unprotected · 1 unknown · ', areas)
        # The table fallback carries the same cells: GAP with its sentence, ok, and — where a check does not apply.
        table = areas[areas.index('<details class="pmx-table">'):]
        self.assertIn("<summary>Table: 4 repositories × 11 checks</summary>", table)
        self.assertIn('<a href="https://github.com/group/alpha/settings/branches" target="_blank" rel="noopener noreferrer">group/alpha</a>', table)
        self.assertIn("<b>GAP</b> <span class=\"sent\">no approving review is required</span>", table)
        self.assertIn("<th scope=\"row\">/checkouts/beta</th>", table)
        self.assertEqual(table.count("<td>token does not reach it</td>"), 1)
        # An unread repository has no cells: each of its checks reads "not read", never — (does not apply).
        gamma = re.search(r'<tr><th scope="row"><a [^>]*>group/gamma</a></th>.*?</tr>', table, re.S).group(0)
        self.assertEqual(gamma.count("<td>not read</td>"), 11, gamma)
        self.assertEqual(gamma.count("<td>—</td>"), 1, "only the Branch cell, which an unread repository has none of")
        self.assertIn("— does not apply; not read where the repository was not read.", table)

    def test_repos_shows_managed_repos_by_default_and_all_on_choice(self):
        """sd:5596. Managed is the default for the matrix and the findings; All brings the others back; a row of no repo stays."""
        for path, managed in (("/checkouts/alpha", 1), ("/checkouts/delta", 1), ("/checkouts/beta", 0), ("/checkouts/gamma", 0)):
            self.connection.execute("INSERT INTO repo (path, managed, created_at, updated_at) VALUES (?, ?, ?, ?)",
                                    (path, managed, NOW, NOW))
        self.connection.commit()
        col = r'class="pmx-col" data-row="([^"]+)"'
        pick = "PANEL().listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f: 'repos', v: '%s' } } : null } });"
        out = self.run_page("R.def = BODY().html;" + pick % "all" + "R.all = BODY().html;" + pick % "managed" + "R.back = BODY().html;",
                            view="github")
        R = out["R"]
        self.assertEqual(re.findall(col, R["def"]), ["prot:/checkouts/alpha", "prot:/checkouts/delta"])
        self.assertIn('<p class="counts">2 protected · 0 unprotected · 0 unknown · ', R["def"])
        self.assertNotIn('data-id="prot:/checkouts/beta"', R["def"], "the findings list follows the choice")
        self.assertRegex(R["def"], r'data-f="repos" data-v="managed" aria-pressed="true"[^>]*>managed <span class="n">2</span>')
        self.assertEqual(re.findall(col, R["all"]), ["prot:/checkouts/beta", "prot:/checkouts/gamma", "prot:/checkouts/alpha", "prot:/checkouts/delta"])
        self.assertIn('data-id="prot:/checkouts/beta"', R["all"])
        self.assertEqual(re.findall(col, R["back"]), ["prot:/checkouts/alpha", "prot:/checkouts/delta"])
        linked = self.run_page("R.b = BODY().html;", view="github", search="?repos=all")
        self.assertIn('data-row="prot:/checkouts/beta"', linked["R"]["b"])

    def test_a_matrix_column_opens_its_repo_with_every_check_in_details(self):
        out = self.run_page("""PANEL().listeners.click[0]({ target: { closest: s => s === '.pmx-col' ? { dataset: { row: 'prot:/checkouts/alpha' } } : null } });
R.d = ELS.details.html; R.cli = cmd('protection.settings').cli(C.get('prot:/checkouts/alpha'));
R.off = cmd('protection.settings').when(C.get('prot:/checkouts/beta')); R.sync = cmd('collector.sync').cli(C.get('collector:protection'));
PANEL().listeners.click[0]({ target: { closest: s => s === '.pmx-col' ? { dataset: { row: 'prot:/checkouts/gamma' } } : null } });
R.unread = ELS.details.html;""", view="github")
        details = out["R"]["d"]
        self.assertIn("<h2>group/alpha: 2 gaps</h2>", details)
        self.assertIn('<dt>Reviews</dt><dd class="c-gap">GAP · no approving review is required</dd>', details)
        self.assertIn('<dt>Admins</dt><dd class="c-ok">ok</dd>', details)
        self.assertIn('<dt>Rebase merge</dt><dd class="c-na">—</dd>', details)
        self.assertIn("group/alpha · settings/branches", details)
        self.assertEqual(out["R"]["cli"], "open 'https://github.com/group/alpha/settings/branches'")
        self.assertEqual(out["R"]["off"], "no github.com remote: there is no settings page")
        self.assertEqual(out["R"]["sync"], "sd shadow sync --max-seconds 120")
        self.assertIn("No cell is shown: token does not reach it.", out["R"]["unread"])
        self.assertNotIn("<dl class=\"checks\">", out["R"]["unread"])

    CLEANED = "ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path) : path === '/api/health/cleanup' ? [STATUS_W, WROTE] : [404, { error: 'no answer' }];\n"

    def cleanup(self, row, body, status=200, answer=None, view="checkouts"):
        """Run a cleanup on `row` as the page does: the confirm, then one post of the row and its revision."""
        answer = answer if answer is not None else {"ok": True, "row": row, "done": ["x"], "refused": []}
        return self.run_page(f"var STATUS_W = {status}, WROTE = {json.dumps(answer)};\n" + self.CLEANED
                             + f"var o = C.get({json.dumps(row)}); OUT.gets = [];\n" + body, view=view)

    def test_prune_confirms_with_the_names_then_posts_the_row_and_its_revision(self):
        """sd:3393: the operator ruled that Health runs the prune, behind a confirm that names what goes."""
        out = self.cleanup("gone:group/alpha", """R.cli = cmd('worktree registrations.prune').cli(o);
R.why = cmd('worktree registrations.prune').consequence(o); R.rev = o.revision;
shellRun(cmd('worktree registrations.prune'), o); await flush();""",
                           answer={"ok": True, "row": "gone:group/alpha", "done": ["a"], "refused": [{"name": "b", "why": "locked"}]})
        self.assertEqual(out["R"]["cli"], "git -C '/checkouts/group/alpha' worktree prune -v")
        self.assertEqual(out["R"]["why"], "Removes 2 worktree registrations whose directories are gone: /work/wt-a (feat/a), "
                                          "/work/wt-b (fix/b). No directory is touched.")
        self.assertRegex(out["R"]["rev"], r"^[0-9a-f]{64}$")
        self.assertEqual(out["confirms"], ["worktree registrations.prune"])
        self.assertEqual([post[:2] for post in out["posts"]],
                         [["/api/health/cleanup", {"row": "gone:group/alpha", "revision": out["R"]["rev"]}]])
        self.assertEqual(out["gets"], ["/api/health/checkouts", "/api/health/summary"])
        self.assertEqual(out["toasts"][-1][0], "group/alpha: 2 worktrees registered, directory gone: 1 done · kept 1: b (locked)")

    def test_an_expired_login_offers_sign_in_and_posts_its_probe(self):
        """sd:6722: a signed-out login's row leads with Sign in; Probe again checks that row's probe only."""
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, timestamp="2026-09-06T03:10:00Z", body={"probes": [
            {"id": "aws:dev", "name": "AWS profile (dev)", "signin": "aws sso login --profile dev", "signed_in": False},
            {"id": "gh", "name": "gh CLI sign-in (gh auth status)", "signed_in": True}]})
        self.connection.commit()
        answer = "ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path) : path === '/api/actions/run' ? [200, { action: body.action, label: 'Sign in', line: 'x', exit: 0, output: 'Started' }] : [404, { error: 'no answer' }];\n"
        out = self.run_page(answer + """var o = C.get('cred:aws:dev'), g = C.get('cred:gh'), c = cmd('credential.signin');
R.row = [o.state, o.what, o.detail]; R.on = c.when(o); R.lead = c.primary(o); R.cli = c.cli(o); R.off = c.when(g);
shellRun(c, o); await flush(); shellRun(cmd('credential.probe'), o); await flush();""", view="jobs")
        R = out["R"]
        self.assertEqual(R["row"], ["warning", "AWS profile (dev): rejected", "signed out: Sign in starts the login"])
        self.assertEqual((R["on"], R["lead"], R["cli"]), (True, True, "aws sso login --profile dev"))
        self.assertEqual(R["off"], "this credential has no sign-in")
        self.assertEqual([post[:2] for post in out["posts"]],
                         [["/api/actions/run", {"action": "credentials.signin", "probe": "aws:dev"}],
                          ["/api/actions/run", {"action": "check.run", "row": "cred:aws:dev"}]])

    def test_each_google_account_signs_in_on_its_own_row_with_its_url_to_copy(self):
        """sd:6957: one Sign in per account; Details shows that account's consent URL to copy, and opens no window."""
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, timestamp="2026-09-06T03:10:00Z", body={"probes": [
            {"id": f"google:{who}", "name": f"Google ({who})", "signin": f"workspace-mcp start_google_auth for {who}",
             "signed_in": False} for who in ("a@example.test", "b@example.test")]})
        self.connection.commit()
        answer = ("ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path) : path === '/api/actions/run' ? [200, { action: body.action, "
                  "label: 'Sign in', line: 'x', exit: 0, output: 'Open the URL', url: 'https://accounts.example.test/o?u=' + body.probe, "
                  "account: body.probe.slice(7) }] : [404, { error: 'no answer' }];\nwindow.open = (...a) => OUT.opened.push(a); OUT.opened = [];\n")
        out = self.run_page(answer + """var c = cmd('credential.signin'); R.d = [];
for (const id of ['cred:google:a@example.test', 'cred:google:b@example.test']) {
  shellRun(c, C.get(id)); await flush(); R.d.push(document.getElementById('details').html || ''); }""", view="jobs")
        self.assertEqual([post[1]["probe"] for post in out["posts"]], ["google:a@example.test", "google:b@example.test"])
        for page, who, other in zip(out["R"]["d"], ("a", "b"), ("b", "a"), strict=True):
            html = page.split(f'<section class="ran" data-obj="cred:google:{who}@example.test"')[-1].split("</section>")[0]
            url = f"https://accounts.example.test/o?u=google:{who}@example.test"
            self.assertIn(f'href="{url}"', html)
            self.assertIn(f'data-copy="{url}"', html)
            self.assertIn(f"in the browser profile for {who}@example.test:", html)
            self.assertNotIn(f"google:{other}@example.test", html)
        self.assertEqual(out["opened"], [])

    SERVICE_ANSWER = ("ANSWER =(path, body) => HEALTH_READ(path) ? HEALTH(path) : path.startsWith('/api/services/') "
                      "? [STATUS_W, WROTE] : [404, { error: 'no answer' }];\n")
    ACCEPTED = {"request": {"id": "x", "action": "restart", "status": "accepted",
                            "message": "launchctl accepted the request; current state is observed separately"}, "service": {}}

    def service_page(self, row, body, status=200, answer=None):
        """sd:3707: the page over the Services fixture, its dashboard serving another commit, with `o` the row."""
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot,
                                     protection=protection_of(PROTECTION), services=SERVICES, served_commit="a" * 40)
        return self.run_page(f"var STATUS_W = {status}, WROTE = {json.dumps(answer or self.ACCEPTED)};\n" + self.SERVICE_ANSWER
                             + f"var o = C.get({json.dumps(row)}); OUT.gets = [];\n" + body, doc=doc, view="machine")

    def test_restart_confirms_then_posts_the_sd_services_route_with_the_rows_revision(self):
        """sd:3707, ruling Q3: the control runs the code `sd services restart` runs, through the route Operations posts."""
        out = self.service_page("svc:com.example.alpha", """var c = cmd('service.restart');
R.cli = c.cli(o); R.why = c.consequence(o); R.on = c.when(o); R.primary = c.primary(o);
R.start = cmd('service.start').when(o);
shellRun(c, o); await flush();""")
        self.assertEqual(out["R"]["cli"], "sd services restart 'com.example.alpha'")
        self.assertEqual(out["R"]["why"], "Stops com.example.alpha and starts it again. Its open connections drop.")
        self.assertEqual((out["R"]["on"], out["R"]["primary"]), (True, True))
        self.assertEqual(out["R"]["start"], "service is already running")
        self.assertEqual(out["confirms"], ["service.restart"])
        self.assertEqual([post[:2] for post in out["posts"]],
                         [["/api/services/com.example.alpha/restart", {"revision": "1" * 64}]])
        self.assertEqual(out["gets"], ["/api/health/machine", "/api/health/summary"])
        self.assertEqual(out["toasts"][-1][0], "com.example.alpha: accepted · launchctl accepted the request; "
                                               "current state is observed separately")

    def test_a_down_service_offers_start_and_says_why_restart_is_off(self):
        out = self.service_page("svc:com.example.beta", """R.start = cmd('service.start').when(o); R.primary = cmd('service.start').primary(o);
R.cli = cmd('service.start').cli(o); R.restart = cmd('service.restart').when(o);
R.old = cmd('service.start').when({ ...o, revision: undefined });
shellRun(cmd('service.start'), o); await flush();""")
        self.assertEqual((out["R"]["start"], out["R"]["primary"]), (True, True))
        self.assertEqual(out["R"]["cli"], "sd services start 'com.example.beta'")
        self.assertEqual(out["R"]["restart"], "service is already unloaded; use Start to load it")
        self.assertEqual(out["R"]["old"], "the row carries no revision: read Health again")
        self.assertEqual([post[:2] for post in out["posts"]], [["/api/services/com.example.beta/start", {"revision": "2" * 64}]])

    def test_the_stale_dashboard_row_is_copy_only_with_the_kickstart_line(self):
        """Restarting the dashboard from its own page would end the request that asks; sd_db.services protects it too."""
        out = self.service_page(f"svc:{runtime.LABEL}", """var k = cmd('dashboard service.kickstart');
R.cli = k.cli(o); R.state = o.state; R.what = o.what;
shellRun(k, o); await flush();""")
        self.assertEqual(out["R"]["cli"], f"launchctl kickstart -k gui/{os.getuid()}/{runtime.LABEL}")
        self.assertEqual(out["R"]["state"], "caution")
        self.assertTrue(out["R"]["what"].endswith(" · stale"))
        self.assertEqual(out["posts"], [])
        self.assertEqual(out["confirms"], [])

    def test_a_refused_service_write_says_not_changed_and_an_unknown_one_says_so(self):
        refused = self.service_page("svc:com.example.alpha", "shellRun(cmd('service.restart'), o); await flush();",
                                    status=409, answer={"error": "The service changed since the page read it.", "reload": True})
        self.assertIn("The service changed since the page read it.", refused["toasts"][-1][0])
        self.assertEqual(refused["gets"], ["/api/health/machine", "/api/health/summary"])
        unknown = self.service_page("svc:com.example.alpha", "shellRun(cmd('service.restart'), o); await flush();",
                                    status=502, answer={"error": "bad gateway"})
        self.assertRegex(unknown["toasts"][-1][0], r"^Not known what changed · com\.example\.alpha: ")
        self.assertEqual(unknown["gets"], ["/api/health/machine", "/api/health/summary"])

    def test_a_refused_cleanup_says_not_changed_and_an_unknown_one_says_so(self):
        stale = self.cleanup("gone:group/alpha", "shellRun(cmd('worktree registrations.prune'), o); await flush();",
                             status=409, answer={"error": "The registrations changed since the page read it.", "reload": True})
        self.assertEqual(stale["toasts"][-1][0], "group/alpha: 2 worktrees registered, directory gone not changed: "
                                                 "The registrations changed since the page read it.")
        self.assertEqual(stale["gets"], ["/api/health/checkouts", "/api/health/summary"])
        unknown = self.cleanup("build:merged", "shellRun(cmd('build output.rm'), o); await flush();",
                               status=502, answer={"error": "/work/wt-old/target was removed only in part", "outcome": "unknown"},
                               view="machine")
        self.assertRegex(unknown["toasts"][-1][0], r"^Not known what changed · 2 merged worktrees keep build output: "
                                                    r"/work/wt-old/target was removed only in part\. Health was read again")
        self.assertEqual(unknown["gets"], ["/api/health/machine", "/api/health/summary"])

    def test_a_row_without_a_revision_or_with_an_unreadable_registration_runs_nothing(self):
        out = self.cleanup("gone:group/alpha", """var p = cmd('worktree registrations.prune');
R.old = p.when({ ...o, revision: undefined }); R.unread = p.when({ ...o, unreadable: 1 });
R.dirty = cmd('build output.rm').when({ revision: 'b', remove: [] });""")
        self.assertEqual(out["R"]["old"], "the row carries no revision: read Health again")
        self.assertEqual(out["R"]["unread"], "1 registration of this checkout could not be read, and prune would judge them itself")
        self.assertEqual(out["R"]["dirty"], "every merged worktree has uncommitted changes: nothing is removed")
        self.assertEqual(out["posts"], [])

    def test_snooze_posts_the_row_and_its_time_then_reads_health_again_and_undo_clears_it(self):
        """sd:1896: the server judges the time, so the page sends one it computed, and Undo writes the same key with none."""
        out = self.run_page("""ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path) : [200, { key: 'k', until: body.until }];
var o = C.get('gone:group/alpha'), t0 = Date.now(); OUT.gets = [];
FIELD_VALUES.until = 'snooze-hour'; shellRun(cmd('worktree registrations.snooze'), o); await flush();
R.ahead = Date.parse(OUT.posts[0][1].until) - t0;
OUT.toasts[OUT.toasts.length - 1].undo(); await flush();
R.off = cmd('check.snooze').when({ id: 'wt:ok', state: 'ok' }); R.seen = o.seen;""")
        # The row's own fingerprint goes with it, so the snooze holds only while the row reads the same.
        self.assertRegex(out["R"]["seen"], r"^[0-9a-f]{16}$")
        self.assertEqual([(path, {k: v for k, v in body.items() if k != "until"}) for path, body, _ in out["posts"]],
                         [("/api/snooze", {"page": "health", "row": "gone:group/alpha", "seen": out["R"]["seen"]})] * 2)
        self.assertEqual(out["posts"][1][1]["until"], None)
        self.assertTrue(3590e3 <= out["R"]["ahead"] <= 3610e3, out["R"]["ahead"])
        self.assertEqual(out["gets"], ["/api/health/checkouts", "/api/health/summary"] * 2)
        self.assertRegex(out["toasts"][-2][0], r"^Snoozed until (\w{3} \w{3} \d\d )?\d\d:\d\d · group/alpha: ")
        self.assertTrue(out["toasts"][-2][1])
        self.assertEqual(out["toasts"][-1][0], "Snooze undone · group/alpha: 2 worktrees registered, directory gone")
        self.assertEqual(out["R"]["off"], "an ok row has nothing to snooze")

    def test_a_snoozed_row_is_drawn_apart_and_unsnooze_brings_it_back(self):
        seen = next(row["seen"] for row in health_screen.document(
            self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot,
            protection=protection_of(PROTECTION))["areas"][2]["rows"] if row["id"] == "gone:group/alpha")
        snooze(self.connection, "health:gone:group/alpha", "2026-09-07T08:00:00Z", seen=seen, now=NOW)
        out = self.run_page("""ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path) : [200, { key: 'k', until: body.until }];
R.areas = BODY().html; R.snoozed = ELS.snoozed.html; var o = C.get('snoozed:gone:group/alpha'); R.type = o.type;
shellRun(cmd('snoozed row.unsnooze'), o); await flush(); OUT.toasts[OUT.toasts.length - 1].undo(); await flush();""")
        self.assertNotIn("directory gone", out["R"]["areas"])
        self.assertIn("Snoozed · 1", out["R"]["snoozed"])
        self.assertIn("group/alpha: 2 worktrees registered, directory gone", out["R"]["snoozed"])
        self.assertEqual(out["R"]["type"], "snoozed row")
        self.assertEqual([body for _, body, _ in out["posts"]],
                         [{"page": "health", "row": "gone:group/alpha", "until": None, "seen": seen},
                          {"page": "health", "row": "gone:group/alpha", "until": "2026-09-07T08:00:00+00:00", "seen": seen}])
        self.assertEqual(out["toasts"][-2][0], "Shows again · group/alpha: 2 worktrees registered, directory gone")

    def test_a_bulk_ignore_reads_health_again_once_after_the_group(self):
        """pack sd:7852: each row's Ignore read the whole page again as it landed, so a bulk Ignore of N rows started N
        full reads. The group's rows now read Health again once, after the last of them settles, as one Ignore does."""
        out = self.run_page("""ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path) : [200, { key: 'k', until: body.until }];
const reads = () => OUT.gets.filter(HEALTH_READ).length, c = cmd('worktree registrations.ignore'), o = C.get('gone:group/alpha');
let at = reads(); shellRun(c, o); await flush(); R.one = reads() - at;
at = reads(); C.runBulk(c, [o, o, o, o, o, o]); await flush(); R.six = reads() - at; R.posts = OUT.posts.length;""")
        self.assertEqual(out["R"]["posts"], 7)
        self.assertGreater(out["R"]["one"], 0)
        self.assertEqual(out["R"]["six"], out["R"]["one"])

    def test_ignore_posts_a_snooze_with_no_end_and_undo_clears_it(self):
        """sd:5470: Ignore is the snooze write with `forever`, beside Snooze; the row's fingerprint goes with it."""
        out = self.run_page("""ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path) : [200, { key: 'k', until: body.until }];
var o = C.get('gone:group/alpha'); shellRun(cmd('worktree registrations.ignore'), o); await flush();
OUT.toasts[OUT.toasts.length - 1].undo(); await flush(); R.seen = o.seen;""")
        row = {"page": "health", "row": "gone:group/alpha", "seen": out["R"]["seen"]}
        self.assertEqual([body for _, body, _ in out["posts"]], [{**row, "until": "forever"}, {**row, "until": None}])
        self.assertEqual(out["toasts"][-2], ["Ignored until it changes · group/alpha: 2 worktrees registered, directory gone", True])
        self.assertEqual(out["toasts"][-1][0], "Ignore undone · group/alpha: 2 worktrees registered, directory gone")

    def test_show_ignored_lists_the_ignored_rows_instead_and_unignore_brings_one_back(self):
        """sd:5470: an ignored row leaves its area and the Snoozed list; the Show chip "ignored" lists it, with Unignore."""
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot,
                                     protection=protection_of(PROTECTION))
        seen = {row["id"]: row["seen"] for row in doc["areas"][2]["rows"]}
        snooze(self.connection, "health:gone:group/alpha", writes.SNOOZE_FOREVER, seen=seen["gone:group/alpha"], now=NOW)
        snooze(self.connection, "health:unread:beta", "2026-09-07T08:00:00Z", seen=seen["unread:beta"], now=NOW)
        out = self.run_page("""ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path) : [200, { key: 'k', until: body.until }];
R.snoozed = ELS.snoozed.html; R.findings = BODY().html;
PANEL().listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f: 'show', v: 'ignored' } } : null } });
R.ignored = BODY().html; var o = C.get('ignored:gone:group/alpha'); R.type = o.type;
shellRun(cmd('ignored row.unignore'), o); await flush(); OUT.toasts[OUT.toasts.length - 1].undo(); await flush();
window.snooze.draw(ELS.snoozed, [{ id: 'x', what: 'today row', until: 'forever', seen: 's' }]); R.today = ELS.snoozed.html;""")
        alpha = "group/alpha: 2 worktrees registered, directory gone"
        self.assertIn("Snoozed · 1", out["R"]["snoozed"])
        self.assertNotIn(alpha, out["R"]["snoozed"])
        self.assertNotIn(alpha, out["R"]["findings"])
        self.assertRegex(out["R"]["findings"], r'data-f="show" data-v="ignored" aria-pressed="false">ignored <span class="n">1</span>')
        self.assertIn(alpha, out["R"]["ignored"])
        self.assertIn('data-f="show" data-v="ignored" aria-pressed="true"', out["R"]["ignored"])
        self.assertNotIn("data-id=", out["R"]["ignored"], "the areas' rows are not drawn beside the ignored ones")
        self.assertEqual(out["R"]["type"], "ignored row")
        self.assertEqual([body for _, body, _ in out["posts"]],
                         [{"page": "health", "row": "gone:group/alpha", "until": None, "seen": seen["gone:group/alpha"]},
                          {"page": "health", "row": "gone:group/alpha", "until": "forever", "seen": seen["gone:group/alpha"]}])
        self.assertEqual(out["toasts"][-2][0], f"Shows again · {alpha}")
        # Today shares the list: a snooze with no end there reads as ignored, not as a time.
        self.assertIn("ignored until it changes", out["R"]["today"])
        self.assertNotIn("Invalid", out["R"]["today"])

    def test_a_snooze_read_that_failed_is_a_partial_read(self):
        with patch.object(writes, "snoozed", side_effect=sqlite3.OperationalError("database is locked")):
            out = self.run_page("R.snoozed = ELS.snoozed.html;")
        self.assertEqual(out["states"][-1], {"kind": "partial", "source": "/api/health/checkouts",
                                             "text": "Snoozes were not read: database is locked. Every row shows."})
        self.assertEqual(out["R"]["snoozed"], "")

    def sync(self, finished):
        """Re-run collector on the protection collector: the run starts, reads as running once, then `finished`."""
        return self.run_page("""var n = 0; ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path)
  : path === '/api/shadow/sync' ? [202, { running: true }]
  : path === '/api/shadow/state' ? (n++ ? [200, """ + json.dumps(finished) + """] : [200, { running: true }]) : [404, { error: 'no answer' }];
OUT.gets = []; shellRun(cmd('collector.sync'), C.get('collector:protection')); await flush();""")

    def test_rerun_collector_starts_the_servers_sync_and_reads_health_again_when_it_ends(self):
        # sd:2894: the route Contributions posts (sd:2207); `sd shadow sync` writes the protection rows Health shows.
        out = self.sync({"running": False, "error": None, "trackers": [
            {"tracker": "github", "ok": True, "configured": True, "reason": "", "lines": []}]})
        self.assertEqual(out["posts"], [["/api/shadow/sync", {}, 64]])
        self.assertEqual(out["gets"], ["/api/shadow/state", "/api/shadow/state", "/api/health/checkouts", "/api/health/summary"])
        self.assertEqual([t[0] for t in out["toasts"]][-1], "Shadow sync started · Health reads again when it ends")

    def test_a_sync_that_ends_broken_says_why(self):
        out = self.sync({"running": False, "error": "OperationalError: database is locked", "trackers": []})
        self.assertEqual(out["toasts"][-1][0], "Shadow sync ended with a problem: OperationalError: database is locked")

    def test_disk_draws_a_bar_per_volume_and_remove_names_the_folders_it_deletes(self):
        out = self.cleanup("build:merged", """R.areas = BODY().html;
R.rm = cmd('build output.rm').cli(o); R.why = cmd('build output.rm').consequence(o);
R.du = cmd('storage folder.du').cli(C.get('rs:/Volumes/store/repo-storage/alpha'));
shellRun(cmd('build output.rm'), o); await flush();""", view="machine")
        areas = out["R"]["areas"]
        self.assertIn('<div class="bars" role="group" aria-label="Volume use: Mac data 85%, store 40%">', areas)
        self.assertIn('<span class="name" title="/Volumes/store">store</span>', areas)
        self.assertIn('<rect class="fill" x="0.5" y="0.5" width="85.0%" height="11" rx="2"/>', areas)
        self.assertIn('<span class="v">85% · 150.0 GiB free</span>', areas)
        self.assertEqual(out["R"]["rm"], "rm -rf /work/wt-old/target\nrm -rf /work/wt-old/node_modules")
        self.assertEqual(out["R"]["why"], "Deletes 2 build output folders in merged, clean worktrees: /work/wt-old/target, "
                                          "/work/wt-old/node_modules. Keeps 1 with uncommitted changes: /work/wt-dirty. "
                                          "Each worktree is checked again first. This cannot be undone.")
        self.assertEqual(out["R"]["du"], "du -sh /Volumes/store/repo-storage/alpha/* | sort -h | tail -5")
        self.assertEqual(out["confirms"], ["build output.rm"])
        self.assertEqual([post[:2] for post in out["posts"]], [["/api/health/cleanup", {"row": "build:merged", "revision": "b" * 64}]])

    def test_delete_merged_confirms_with_the_branch_names_and_a_checked_out_branch_says_why_git_refuses(self):
        out = self.cleanup("br:/checkouts/alpha", """var held = C.get('brs:/checkouts/alpha');
R.cli = cmd('merged branches.delete').cli(o); R.why = cmd('merged branches.delete').consequence(o);
R.on = cmd('merged branches.delete').when(o); R.held = cmd('merged branches.delete').when(held);
shellRun(cmd('merged branches.delete'), o); await flush();""")
        self.assertEqual(out["R"]["cli"], "git -C /checkouts/alpha branch -d feat/one feat/two fix/three")
        self.assertEqual(out["R"]["why"], "Deletes 3 local branches already in origin's default branch: feat/one, feat/two, "
                                          "fix/three. An unmerged branch is never deleted.")
        self.assertIs(out["R"]["on"], True)
        self.assertEqual(out["R"]["held"], "checked out in a worktree: git branch -d refuses it until the worktree is removed")
        self.assertEqual(out["confirms"], ["merged branches.delete"])
        self.assertEqual([post[:2] for post in out["posts"]],
                         [["/api/health/cleanup", {"row": "br:/checkouts/alpha", "revision": "a" * 64}]])
        self.assertEqual(out["toasts"][-1][0], "alpha: 3 merged branches not deleted: 1 done")

    def merged_page(self, body, answer):
        """The Checkouts view with two merged worktrees, one that can go, and cleanup posts answered by `answer` (JS)."""
        doc = health_screen.document(self.connection, now=NOW, fleet=fleet_of(TREES), ports=ports_snapshot,
                                     protection=protection_of(PROTECTION), branches=scan_of({**BRANCHES, "worktrees": WORKTREES}))
        return self.run_page("ANSWER = (path, body) => HEALTH_READ(path) ? HEALTH(path) : path === '/api/health/cleanup' ? ("
                             + answer + ")(body) : [404, { error: 'no answer' }];\nOUT.gets = [];\n" + body, doc=doc)

    def test_remove_merged_names_the_trees_it_takes_and_those_it_keeps_then_posts_its_revision(self):
        out = self.merged_page("""var o = C.get('wt:merged'), c = cmd('merged worktrees.remove');
R.on = c.when(o); R.why = c.consequence(o); R.cli = c.cli(o); R.none = c.when({ ...o, removable: [] });
shellRun(c, o); await flush();""", "body => [200, { ok: true, row: body.row, done: ['/work/wt-merged'], refused: [] }]")
        self.assertIs(out["R"]["on"], True)
        self.assertEqual(out["R"]["why"], "Removes 1 worktrees the lane merged, with their build output, their local branch and "
                                          "origin's: /work/wt-merged. Keeps 1: /work/wt-busy (a process works in /work/wt-busy). "
                                          "Each is checked again first. This cannot be undone.")
        self.assertEqual(out["R"]["cli"], "git -C /checkouts/alpha worktree remove /work/wt-merged && git -C /checkouts/alpha "
                                          f"update-ref -d refs/heads/feat/merged {'d' * 40}")
        self.assertEqual(out["R"]["none"], "every merged worktree has a reason to stay: Details lists each")
        self.assertEqual(out["confirms"], ["merged worktrees.remove"])
        self.assertEqual([post[:2] for post in out["posts"]], [["/api/health/cleanup", {"row": "wt:merged", "revision": "e" * 64}]])

    def test_clean_all_safe_lists_every_ready_cleanup_before_it_runs_them_in_turn(self):
        out = self.merged_page("""var o = C.get('cleanup:safe'), c = cmd('safe cleanups.clean');
R.areas = BODY().html; R.why = c.consequence(o); R.cli = c.cli(o);
shellRun(c, o); await flush();""", "body => body.row.startsWith('br:') ? [409, { error: 'The merged branches changed.', reload: true }]"
                                    " : [200, { ok: true, row: body.row, done: ['x'], refused: [] }]")
        self.assertIn('<p class="counts">3 cleanups ready in Checkouts · ', out["R"]["areas"])
        why = out["R"]["why"]
        self.assertTrue(why.startswith("Runs 3 cleanups in turn, each checked again first: "), why)
        for what in ("group/alpha: 2 worktrees registered, directory gone (prunes /work/wt-a (feat/a), /work/wt-b (fix/b))",
                     "alpha: 3 merged branches not deleted (deletes feat/one, feat/two, fix/three)",
                     "2 merged worktrees are still on disk (removes /work/wt-merged with their branches)"):
            self.assertIn(what, why)
        self.assertEqual(out["confirms"], ["safe cleanups.clean"])
        self.assertEqual([post[1]["row"] for post in out["posts"]], ["gone:group/alpha", "br:/checkouts/alpha", "wt:merged"])
        self.assertEqual(out["posts"][2][1]["revision"], "e" * 64)
        # The refused row wrote nothing and the run went on; the page reads Health again once at the end.
        self.assertRegex(out["toasts"][-1][0], r"^Clean all safe · group/alpha: .*: 1 done · alpha: 3 merged branches not "
                                               r"deleted: not changed \(The merged branches changed\.\) · 2 merged worktrees .*: 1 done$")
        self.assertEqual(out["gets"], ["/api/health/checkouts", "/api/health/summary"])

    def test_clean_all_safe_runs_only_the_rows_its_confirm_listed(self):
        # A read between the confirm and the run brings a fourth ready row; the run posts only the three it listed.
        out = self.merged_page("""var o = C.get('cleanup:safe'), c = cmd('safe cleanups.clean');
R.why = c.consequence(o);
var br = DOC.areas.find(a => a.id === 'br');
br.rows.push({ ...br.rows.find(r => r.id === 'br:/checkouts/alpha'), id: 'br:/checkouts/gamma', what: 'gamma: 1 merged branch not deleted' });
window.PAGE_COMMANDS.find(x => x.label === 'Read Health again').run(); await flush();
R.now = c.cli(o).split('\\n').length;
await c.run(o); await flush();""", "body => [200, { ok: true, row: body.row, done: ['x'], refused: [] }]")
        self.assertEqual(out["R"]["now"], 4)
        self.assertEqual([post[1]["row"] for post in out["posts"]], ["gone:group/alpha", "br:/checkouts/alpha", "wt:merged"])

    def test_clean_all_safe_stops_at_an_unknown_outcome(self):
        out = self.merged_page("shellRun(cmd('safe cleanups.clean'), C.get('cleanup:safe')); await flush();",
                               "body => body.row.startsWith('br:') ? [502, { error: 'bad gateway', outcome: 'unknown' }]"
                               " : [200, { ok: true, row: body.row, done: ['x'], refused: [] }]")
        self.assertEqual([post[1]["row"] for post in out["posts"]], ["gone:group/alpha", "br:/checkouts/alpha"])
        self.assertRegex(out["toasts"][-1][0], r"^Clean all safe stopped · not known what changed · alpha: 3 merged branches "
                                               r"not deleted: .* Before it: group/alpha: .*: 1 done\. Health was read again")

    def test_details_show_the_facts_and_the_registrations_behind_a_row(self):
        out = self.run_page("document.dispatchEvent(new CustomEvent('shell:open', { detail: 'gone:group/alpha' })); R.d = ELS.details.html;")
        details = out["R"]["d"]
        self.assertIn("<h2>group/alpha: 2 worktrees registered, directory gone</h2>", details)
        self.assertIn("<dt>Repo</dt><dd>/checkouts/group/alpha</dd>", details)
        self.assertIn("<li>/work/wt-a (feat/a)</li>", details)
        self.assertIn("<dt>Source</dt><dd>the fleet&#39;s .git/worktrees registrations</dd>", details)

    def test_an_aws_resource_in_details_links_to_its_console_page(self):
        """sd:6713: a list entry with an href is a new-tab link; a plain one stays text."""
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, timestamp="2026-09-06T03:10:00Z",
                     body={"probes": [], "aws": {"resources": [{"type": "EC2 instance", "profile": "me", "region": "us-west-2",
                                                               "id": "i-1", "name": "box", "since": "2026-09-01T00:00:00Z"}],
                                                 "errors": ["me us-east-1 CloudFormation stack: aws exited 255"]}})
        out = self.run_page("document.dispatchEvent(new CustomEvent('shell:open', { detail: 'aws:me:us-west-2:i-1' })); R.d = ELS.details.html;"
                            " document.dispatchEvent(new CustomEvent('shell:open', { detail: 'aws:failed' })); R.f = ELS.details.html;",
                            view="machine")
        details = out["R"]["d"]
        self.assertIn('<a class="ext" href="https://us-west-2.console.aws.amazon.com/ec2/home?region=us-west-2#InstanceDetails:'
                      'instanceId=i-1" target="_blank" rel="noopener noreferrer">AWS console', details)
        self.assertIn("<li>me us-east-1 CloudFormation stack: aws exited 255</li>", out["R"]["f"])

    def test_the_second_profile_row_opens_its_own_details(self):
        """sd:7117: two profiles seeing one resource are two rows; the pane shows the one asked for."""
        one = {"type": "EC2 instance", "region": "us-west-2", "id": "i-1", "name": "box", "since": "2026-09-01T00:00:00Z"}
        record_state(self.connection, "heartbeat", key=credentials.HEARTBEAT_KEY, timestamp="2026-09-06T03:10:00Z",
                     body={"probes": [], "aws": {"resources": [{**one, "profile": "dev"}, {**one, "profile": "prod"}],
                                                 "errors": []}})
        out = self.run_page("document.dispatchEvent(new CustomEvent('shell:open', { detail: 'aws:prod:us-west-2:i-1' }));"
                            " R.d = ELS.details.html;", view="machine")
        self.assertIn("<dt>Profile</dt><dd>prod</dd>", out["R"]["d"])

    def test_the_first_read_opens_the_first_row_in_details(self):
        out = self.run_page("R.d = document.getElementById('details').html || '';", view="machine")
        self.assertIn("<h2>Mac data is 85% full</h2>", out["R"]["d"],
                      "Details still say the fleet is being read after the rows arrived")

    def test_the_page_declares_the_servers_views_in_order(self):
        out = self.run_page("R.views = window.PAGE_VIEWS;")
        self.assertEqual([(v["id"], v["label"], tuple(v["areas"]), v["api"]) for v in out["R"]["views"]],
                         [(view, name, keys, f"/api/health/{view}") for view, name, keys in health_screen.VIEWS])

    def test_the_summary_draws_every_lamp_and_count_before_the_view_answers(self):
        doc = self.run_doc()
        disk = next(area for area in doc["areas"] if area["id"] == "disk")
        disk["error"] = "df did not answer in 5 s"
        out = self.run_page("""R.lamps = ELS.annunciator.html; R.areas = BODY().html;""", pre="""var pending = [];
ANSWER = path => path === '/api/health/summary' ? [200, SUMMARY_DOC] : new Promise(answer => pending.push(answer));""", doc=doc)
        self.assertEqual(out["gets"], ["/api/health/summary", "/api/health/checkouts"])
        self.assertRegex(out["R"]["lamps"], r'data-area="disk" data-state="unknown"')
        self.assertRegex(out["R"]["lamps"], r'data-area="br" data-state="caution"')
        self.assertRegex(out["R"]["lamps"], r'data-area="cred" data-state="unknown"')
        self.assertNotIn('class="area"', out["R"]["areas"] or "")
        # A view's count is the rows that want you in it; Machine's disk was not read, so Machine has no count.
        counts = out["counts"]
        self.assertEqual((counts["checkouts"], counts["jobs"]), (wanting(doc, "checkouts"), wanting(doc, "jobs")))
        self.assertIsNone(counts["machine"])

    def test_details_say_the_read_is_in_progress_until_it_settles(self):
        """sd:9312: a read in flight is not a failed read; Details said 'Health could not be read' while it loaded."""
        out = self.run_page("""R.loading = document.getElementById('details').html || ''; pending[0]([500, { error: 'boom' }]); await flush();
R.failed = document.getElementById('details').html || '';""", pre="""var pending = [];
window.shell.reconcile = a => { if (![...a.rows].length) a.clear(); }; // shell.js's: no row shown clears Details
ANSWER = path => path === '/api/health/summary' ? [200, SUMMARY_DOC] : new Promise(answer => pending.push(answer));""")
        self.assertIn("Nothing is selected: Health is still being read.", out["R"]["loading"])
        self.assertNotIn("could not be read", out["R"]["loading"])
        self.assertIn("Nothing is selected: Health could not be read.", out["R"]["failed"])

    def test_a_view_switch_reads_its_part_once(self):
        switch = "shell.view.show('{v}'); await flush();"
        out = self.run_page("OUT.gets = [];" + switch.format(v="machine") + "R.areas = BODY().html;"
                            + switch.format(v="checkouts") + "R.back = BODY().html; R.again = OUT.gets.slice();")
        self.assertEqual(out["R"]["again"], ["/api/health/machine", "/api/health/summary"], "a view opened before was read again")
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', out["R"]["areas"]), ["disk", "ports", "svc", "ctr", "doc", "aws"])
        # sd:5840: Containers heads its rows with the count and the checkouts they mount.
        self.assertRegex(out["R"]["areas"], r"<p class=\"counts\">2 containers running · \d mount a checkout under ")
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', out["R"]["back"]), ["wt", "br"])

    def test_with_no_view_named_the_first_opens_and_an_area_value_opens_its_view(self):
        first = self.run_page("", view=None)
        self.assertEqual(first["gets"], ["/api/health/summary", "/api/health/checkouts", "/api/health/summary"])
        asked = self.run_page("R.areas = BODY().html; R.address = OUT.address;", view=None, search="?area=ports")
        self.assertEqual(asked["R"]["address"], "/fleet-health?area=ports#view=machine")
        self.assertEqual(asked["gets"], ["/api/health/summary", "/api/health/machine", "/api/health/summary"])
        self.assertEqual(re.findall(r'<section class="area" id="a-(\w+)"', asked["R"]["areas"]), ["ports"])
        named = self.run_page("", view="jobs", search="?area=ports")
        self.assertEqual(named["gets"], ["/api/health/summary", "/api/health/jobs", "/api/health/summary"])

    def test_with_the_shells_view_switch_the_page_draws_none_and_reads_a_view_on_its_first_opening(self):
        # The shell's contract (sys-tuning-ui): shell.view and the shell:view event; the page's own switch stays empty.
        out = self.run_page("""const open = (id, first) => document.dispatchEvent(new CustomEvent('shell:view', { detail: { id, first, api: '/api/health/' + id } }));
open('machine', true); await flush(); R.machine = ELS['body-machine'].html;
open('checkouts', false); await flush(); R.gets = OUT.gets.slice(); R.counts = OUT.counts;
R.views = 'viewswitch' in ELS;""", view=None, pre="""OUT.counts = {}; var CUR = null;
window.shell.view = { current: () => CUR, show: id => { OUT.shown = id; }, count: (id, n) => { OUT.counts[id] = n; } };
document.addEventListener('shell:view', e => { CUR = e.detail.id; });""")
        self.assertFalse(out["R"]["views"], "the page drew its own view switch next to the shell's")
        self.assertEqual(out["R"]["gets"], ["/api/health/summary", "/api/health/machine", "/api/health/summary"])
        self.assertIn('id="a-disk"', out["R"]["machine"])
        doc = self.run_doc()
        self.assertEqual(out["R"]["counts"], {view: wanting(doc, view) for view, *_ in health_screen.VIEWS})
        self.assertGreater(min(out["R"]["counts"].values()), 0)

    def test_each_view_draws_its_reading_state_in_its_own_panel(self):
        def broken(area):
            raise ValueError("fleet collection was stopped at its budget: sessions")
        doc = health_screen.document(self.connection, now=NOW, fleet=broken, ports=ports_snapshot,
                                     protection=protection_of([]), disk=scan_of(CLEAN_DISK), branches=scan_of(CLEAN_BRANCHES))
        out = self.run_page("", doc)
        # A hidden view's late "Partial read" stays in its hidden panel, not over the open view.
        self.assertEqual((out["states"][-1]["kind"], out["slots"][-1]), ("partial", "state-checkouts"))
        self.assertEqual(set(out["slots"]), {"state-checkouts"})
        for view, *_ in health_screen.VIEWS:
            self.assertRegex(HEALTH_HTML, rf'<section id="view-{view}"[^>]*><div id="state-{view}" hidden></div><div id="body-{view}"></div></section>')

    def test_a_late_read_of_a_view_that_is_not_open_leaves_the_open_views_selection(self):
        doc = self.run_doc()
        for area in doc["areas"]:
            if area["view"] == "machine":
                area["rows"], area["snoozed"] = [], []
        out = self.run_page("""OUT.sel = []; C.select = id => OUT.sel.push(id);
window.shell.view.show('machine'); window.shell.view.show('checkouts'); await flush();
R.cur = window.shell.view.current();""", doc)
        self.assertEqual(out["R"]["cur"], "checkouts")
        self.assertIn("/api/health/machine", out["gets"])
        self.assertEqual(out["sel"], [], "the Machine read cleared the Checkouts selection")

    def test_state_starts_on_the_problem_states_and_none_pressed_lists_every_row(self):
        """sd:7207: Show has no all. State starts on warning, caution and unknown, so the ok and queued rows wait and each
        area says how many; with no State pressed every row shows. Clear all goes back to the start."""
        doc = self.run_doc()
        state = {row["id"]: row["state"] for area in doc["areas"] for row in area["rows"]}
        out = self.run_page(r"""R.start = BODY().html;
const click = (f, v) => PANEL().listeners.click[0]({ target: { closest: s => s === '[data-f]' ? { dataset: { f, v } } : null } });
[...BODY().html.matchAll(/data-f="state" data-v="(\w+)" aria-pressed="true"/g)].forEach(m => click('state', m[1]));
R.none = BODY().html;
PANEL().listeners.click[0]({ target: { closest: s => s === '[data-fclear]' ? {} : null } }); R.cleared = BODY().html;""",
                            doc=doc, view="machine")
        R = out["R"]
        ids = {name: re.findall(r'<tr data-id="([^"]+)"', R[name]) for name in ("start", "none", "cleared")}
        pressed = {name: re.findall(r'data-f="state" data-v="(\w+)" aria-pressed="true"', R[name]) for name in ("start", "none", "cleared")}
        self.assertEqual(pressed["start"], ["warning", "caution", "unknown"])
        self.assertTrue(ids["start"])
        self.assertEqual({state[i] for i in ids["start"]} - {"warning", "caution", "unknown"}, set(), "the start shows an ok row")
        self.assertRegex(R["start"], r'<p class="quiet">[^<]*\d+ ok or queued rows? not shown: press ok or queued in State to list')
        self.assertNotIn('data-f="show" data-v="all"', R["start"])
        self.assertRegex(R["start"], r'data-f="show" data-v="findings" aria-pressed="true"')
        # No State pressed: every row, the ok and queued ones too, and no line says a row is hidden.
        self.assertEqual(pressed["none"], [])
        self.assertTrue({"ok", "queued"} & {state[i] for i in ids["none"]}, "no State pressed still hid the ok and queued rows")
        self.assertLess(set(ids["start"]), set(ids["none"]))
        self.assertNotIn('class="quiet"', R["none"])
        self.assertIn("data-fclear>Clear all</button>", R["none"])
        self.assertEqual((ids["cleared"], pressed["cleared"]), (ids["start"], pressed["start"]))

    def test_an_old_show_all_link_opens_with_no_state_pressed(self):
        """sd:7207: ?show=all named the option this replaced; the link opens on every row, and ?state= still narrows."""
        every = self.run_page("R.b = BODY().html;", view="machine", search="?show=all")["R"]["b"]
        self.assertEqual(re.findall(r'data-f="state" data-v="(\w+)" aria-pressed="true"', every), [])
        self.assertRegex(every, r'data-f="show" data-v="findings" aria-pressed="true"')
        self.assertNotIn('class="quiet"', every)
        ok = self.run_page("R.b = BODY().html;", view="machine", search="?state=ok")["R"]["b"]
        self.assertEqual(re.findall(r'data-f="state" data-v="(\w+)" aria-pressed="true"', ok), ["ok"])
        empty = self.run_page("R.b = BODY().html;", view="machine", search="?state=")["R"]["b"]
        self.assertEqual(re.findall(r'<tr data-id="([^"]+)"', empty), re.findall(r'<tr data-id="([^"]+)"', every))

    def test_the_script_adds_no_sink_and_no_inline_style(self):
        self.assertNotIn("innerHTML", HEALTH_JS)
        self.assertNotRegex(HEALTH_JS, r"style=\\?\"")
        self.assertEqual(re.findall(r"window\.markup\b", re.sub(r"const \{ [\w, ]+ \} = window\.markup;", "", HEALTH_JS)), [])
        # The shell owns j / k and Esc through PAGE_LIST; a page-level handler is drift.
        self.assertNotRegex(HEALTH_JS, r"e\.key === '[jk]'")


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "health", "Health", "/fleet-health", ("/api/health", "/api/health/summary", "/api/health/cleanup")


if __name__ == "__main__":
    unittest.main()
