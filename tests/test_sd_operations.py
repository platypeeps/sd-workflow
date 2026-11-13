"""CLI boundary checks use a fake service and never execute launchctl."""

import argparse
import importlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import sd_db

from tests.clean_env import clean_environment

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, "path", [str(ROOT / "bin"), *sys.path]):
    cli = importlib.import_module("sd_operations")


class OperationsCLI(unittest.TestCase):
    def setUp(self):
        self.parser = argparse.ArgumentParser()
        cli.register(self.parser.add_subparsers(required=True))
        self.connection = Mock()
        self.service = types.ModuleType("sd_db.operations")
        for name in ("inventory", "job_state", "assignment_state", "retry_job", "cancel_job", "cancel_assignment"):
            setattr(self.service, name, Mock())
        self.service.job_state.return_value = {"name": "daily", "state": "failed", "revision": "fresh"}
        self.service.assignment_state.return_value = {"id": 8, "status": "queued", "revision": "fresh"}
        self.service.retry_job.return_value = {"request": {"status": "accepted"}, "job": {"state": "failed"}}
        self.service.cancel_job.return_value = {"request": {"status": "accepted"}, "job": {"state": "running"}}
        self.service.cancel_assignment.return_value = {"id": 8, "status": "cancelled"}
        self.launchd = types.ModuleType("sd_db.services")
        for name in ("inventory", "service_state", "start_service", "stop_service", "restart_service"):
            setattr(self.launchd, name, Mock())
        self.launchd.service_state.return_value = {
            "id": "user:com.example.server", "label": "com.example.server",
            "state": "running", "revision": "current-service"}
        self.connect = Mock(return_value=self.connection)
        for patcher in (patch.dict(sys.modules, {"sd_db.operations": self.service, "sd_db.services": self.launchd}),
                        patch.object(sd_db, "operations", self.service, create=True),
                        patch.object(sd_db, "services", self.launchd, create=True),
                        patch.object(cli.sd_handoff_rows, "library", return_value=sd_db),
                        patch.object(cli.sd_handoff_rows, "connect", self.connect),
                        patch.object(cli.getpass, "getuser", return_value="operator")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, *argv):
        arguments = self.parser.parse_args(argv)
        output = io.StringIO()
        with redirect_stdout(output):
            code = arguments.handler(arguments)
        return code, output.getvalue()

    def test_read_inventory_uses_read_only_connection(self):
        self.service.inventory.return_value = {"jobs": [], "assignments": []}
        code, output = self.call("jobs", "list", "--json")
        self.assertEqual((code, json.loads(output)), (0, []))
        self.connect.assert_called_once_with(sd_db, write=False)
        self.connection.close.assert_called_once()

    def test_explicit_revision_is_preserved_for_backend_stale_check(self):
        self.service.retry_job.side_effect = sd_db.SdDbError("state changed")
        with self.assertRaisesRegex(cli.WorkRefusal, "state changed"):
            self.call("jobs", "retry", "daily", "--if-revision", "inspected", "--json")
        self.service.retry_job.assert_called_once_with(
            self.connection, "daily", expected_revision="inspected", who="operator")
        self.connect.assert_called_once_with(sd_db, write=True)
        self.connection.close.assert_called_once()

    def test_default_revision_uses_current_snapshot_and_accepted_does_not_mean_running(self):
        code, output = self.call("jobs", "retry", "daily", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["job"]["state"], "failed")
        self.service.retry_job.assert_called_once_with(
            self.connection, "daily", expected_revision="fresh", who="operator")

    def test_failed_and_unknown_requests_exit_one_and_keep_observed_state(self):
        for status in ("failed", "unknown"):
            self.service.cancel_job.return_value = {"request": {"status": status}, "job": {"state": "running"}}
            code, output = self.call("jobs", "cancel", "daily", "--json")
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output)["job"]["state"], "running")

    def test_assignment_cancel_uses_integer_identity_and_shared_revision(self):
        code, output = self.call("assignments", "cancel", "8", "--json")
        self.assertEqual((code, json.loads(output)["status"]), (0, "cancelled"))
        self.service.cancel_assignment.assert_called_once_with(
            self.connection, 8, expected_revision="fresh", who="operator")

    def test_assignments_build_queues_every_item_on_a_write_connection(self):
        """pack sd:6093: the Build press from the CLI, the items as `sd:N` or bare."""
        queue = Mock(return_value=[{"id": 31, "item": 7, "status": "queued"}, {"id": 32, "item": 9, "status": "queued"}])
        builds = types.ModuleType("sd_db.build")
        builds.queue = queue
        with patch.dict(sys.modules, {"sd_db.build": builds}), patch.object(sd_db, "build", builds, create=True):
            code, output = self.call("assignments", "build", "sd:7", "9")
            with self.assertRaises(cli.WorkRefusal):
                queue.side_effect = sd_db.SdDbError("item 9 is planning; a build takes a ready item")
                self.call("assignments", "build", "9")
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines(), ["#31  queued  build  sd:7", "#32  queued  build  sd:9"])
        self.assertEqual(queue.call_args_list[0].args[1:], ([7, 9],))
        self.assertEqual(queue.call_args_list[0].kwargs, {"who": "operator"})
        self.connect.assert_called_with(sd_db, write=True)
        self.assertEqual(self.connection.close.call_count, 2)

    def test_beat_is_one_verb_with_the_leads_start_and_end_as_flags(self):
        """pack sd:7221 (ruling note #41072): `beat ITEM` beats a teammate run; `--start` and `--end` are the lead's."""
        team = types.ModuleType("sd_db.teammate")
        team.start = Mock(return_value={"id": "r1", "due": "2026-11-02T12:00:00+00:00",
                                        "members": [{"item": 7}, {"item": 9}]})
        team.beat, team.end = Mock(return_value="r1"), Mock(return_value="r1")
        with patch.dict(sys.modules, {"sd_db.teammate": team}), patch.object(sd_db, "teammate", team, create=True):
            started = self.call("assignments", "beat", "sd:7", "9", "--start", "--worktree", "/w/pack-7",
                                "--branch", "feat/x", "--budget", "90", "--json")
            beat = self.call("assignments", "beat", "7")
            ended = self.call("assignments", "beat", "7", "--end", "--reason", "reported")
            for argv, why in ((["7", "--start", "--branch", "b", "--budget", "9"], "--start needs"),
                              (["7", "--worktree", "/w"], "go with --start"), (["7", "8"], "one item"),
                              (["7", "--reason", "x"], "goes with --start or --end"),
                              (["7", "--into", "b"], "go with --start")):
                with self.subTest(argv=argv), self.assertRaisesRegex(cli.WorkRefusal, why):
                    self.call("assignments", "beat", *argv)
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                self.parser.parse_args(["assignments", "beat", "7", "--start", "--end"])
            # pack sd:7737: a second open branch of a repo names the bundle it merges into, or why it is a second PR.
            self.call("assignments", "beat", "7", "--start", "--worktree", "/w/pack-8", "--branch", "feat/y",
                      "--budget", "30", "--into", "feat/x", "--reason", "in-lane")
            side = team.start.call_args.kwargs
            self.call("assignments", "beat", "sd:7", "9", "--start", "--worktree", "/w/pack-7",
                      "--branch", "feat/x", "--budget", "90", "--json")
            team.beat.side_effect = sd_db.SdDbError("no open teammate run holds item 7")
            with self.assertRaisesRegex(cli.WorkRefusal, "no open teammate run"):
                self.call("assignments", "beat", "7")
        self.assertEqual(json.loads(started[1]), {"run": "r1", "due": "2026-11-02T12:00:00+00:00", "items": [7, 9]})
        self.assertEqual(team.start.call_args.args[1:], ([7, 9],))
        self.assertEqual(team.start.call_args.kwargs, {"worktree": str(Path("/w/pack-7").resolve()), "branch": "feat/x",
                                                       "budget_minutes": 90, "owner": "operator", "into": None,
                                                       "reason": None})
        self.assertEqual((side["into"], side["reason"]), ("feat/x", "in-lane"))
        self.assertEqual((beat, ended), ((0, "teammate run r1 beat\n"), (0, "teammate run r1 ended\n")))
        self.assertEqual(team.end.call_args.kwargs, {"reason": "reported"})
        self.connect.assert_called_with(sd_db, write=True)

    def test_the_dispatcher_is_one_verb_with_its_supervisor_and_sessions_as_flags(self):
        """pack sd:6356 (ruling #31545): `dispatch` takes `--supervise RUN` and `--sessions`; the two old verbs are
        gone, and the flags exclude each other."""
        self.assertEqual(self.parser.parse_args(["assignments", "dispatch", "--supervise", "r1"]).run, "r1")
        self.assertTrue(self.parser.parse_args(["assignments", "dispatch", "--sessions"]).sessions)
        with patch.object(cli, "supervise_build", return_value=0) as supervise:
            self.call("assignments", "dispatch", "--supervise", "r1")
        self.assertEqual(supervise.call_args.args[0].run, "r1")
        for argv in (["assignments", "supervise", "r1"], ["assignments", "sessions"],
                     ["assignments", "dispatch", "--sessions", "--supervise", "r1"]):
            with self.assertRaises(SystemExit, msg=argv), patch("sys.stderr", io.StringIO()):
                self.parser.parse_args(argv)

    def test_assignments_sessions_lists_the_live_sessions_read_only(self):
        """pack sd:6024: the registry for a cleanup outside the pack; a failed read refuses, never an empty list."""
        live = Mock(return_value=[{"session": "session-a1234", "machine": "hub", "beat": "2026-10-25T10:00:00+00:00",
                                   "worktrees": [{"path": "~/worktrees/pack-one"}]}])
        registry = types.ModuleType("sd_db.sessions")
        registry.live = live
        with patch.dict(sys.modules, {"sd_db.sessions": registry}), patch.object(sd_db, "sessions", registry,
                                                                                  create=True):
            code, output = self.call("assignments", "dispatch", "--sessions")
            _, printed = self.call("assignments", "dispatch", "--sessions", "--json")
            live.side_effect = sd_db.SdDbError("the hub is unreachable")
            with self.assertRaises(cli.WorkRefusal):
                self.call("assignments", "dispatch", "--sessions")
        self.assertEqual((code, output), (0, "hub  session-  2026-10-25T10:00:00+00:00  ~/worktrees/pack-one\n"))
        self.assertEqual(json.loads(printed)[0]["session"], "session-a1234")
        self.connect.assert_called_with(sd_db, write=False)

    def builds(self):
        """`sd_db.build` and `sd_db.build_run` fakes; `dispatch` answers an empty tick."""
        build, run = types.ModuleType("sd_db.build"), types.ModuleType("sd_db.build_run")
        run.dispatch = Mock(return_value={"reaped": [], "moved": [], "swept": [], "claimed": ["r1"], "drafted": [], "spawned": ["r1"], "why": ""})
        run.now = Mock(return_value="2026-10-25T09:00:00+00:00")
        run.paths = types.SimpleNamespace(key=lambda value: value, expand=Path)
        run.Tools = lambda **tools: types.SimpleNamespace(**tools)
        run.local_root = Mock(return_value=Path("/local"))
        run.archive = Mock(return_value=[])
        run.archive_wait = Mock()
        for patcher in (patch.dict(sys.modules, {"sd_db.build": build, "sd_db.build_run": run}),
                        patch.object(sd_db, "build", build, create=True),
                        patch.object(sd_db, "build_run", run, create=True),
                        # The tick's failure push reads the real database; these fakes have none.
                        patch.object(cli, "push_failures", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        return run

    def test_assignments_dispatch_reads_the_load_and_the_log_folder_before_the_tick(self):
        """pack sd:6094: an unread load is None, never room; an unset bulk root is a reason for no claim."""
        run = self.builds()
        with patch.object(cli, "gate_load", return_value=(None, None)), \
                patch.object(cli, "interactive_builders", return_value=([], "")), \
                patch.object(cli, "log_root", return_value=None):
            code, output = self.call("assignments", "dispatch")
        self.assertEqual(code, 0)
        kwargs = run.dispatch.call_args.kwargs
        self.assertEqual((kwargs["load"], kwargs["load_cap"], kwargs["interactive"]), (None, None, []))
        self.assertIn("bulk_storage_root is unset", kwargs["why"])
        run.archive.assert_not_called()  # no bulk root: an ended run's logs stay local
        self.assertIn("claimed 1, drafted 0, spawned 1", output)
        self.connect.assert_called_with(sd_db, write=True)
        self.connection.close.assert_called_once()

    def test_a_spawn_runs_the_supervise_verb_the_real_parser_reads(self):
        """Review round 1: the spawned argv is `sd`'s own `assignments dispatch --supervise RUN`, parsed by the real
        parser (pack sd:6356)."""
        run = self.builds()
        sys.modules["sd_db.build"].run = Mock(return_value={"repo": "/repo"})
        run.spawn, run.log_dir = Mock(), lambda root, repo: root
        run.dispatch.side_effect = lambda connection, **kwargs: (kwargs["spawn"]("r1"), {
            "reaped": [], "moved": [], "swept": [], "claimed": [], "drafted": [], "spawned": ["r1"], "why": ""})[1]
        with patch.object(cli, "gate_load", return_value=(1.0, 0.0)), \
                patch.object(cli, "interactive_builders", return_value=([], "")), \
                patch.object(cli, "log_root", return_value=Path("/bulk")), \
                patch.object(cli, "writable", return_value=True):
            self.call("assignments", "dispatch")
        argv = run.spawn.call_args.args[0]
        self.assertEqual(Path(argv[1]), ROOT / "bin" / "sd")
        arguments = self.parser.parse_args(argv[2:])
        self.assertEqual((arguments.handler, arguments.run, arguments.sessions), (cli.build_dispatcher, "r1", False))
        # pack sd:8246: a live run logs to the local folder; the tick moves an ended run's files to the bulk root.
        self.assertEqual(run.spawn.call_args.kwargs["log"], Path("/local/r1.supervisor.log"))
        run.archive.assert_called_once_with(self.connection, Path("/local"), Path("/bulk"))

    def test_the_tick_gives_the_archive_copier_the_rest_of_its_budget(self):
        """sd:8339: the tick exited after `archive`'s 20 s wait and killed its copier, so a copy that took longer
        never landed; before sd:8275 it had the tick's whole budget. It waits once the connection is closed."""
        run = self.builds()
        run.archive_wait.side_effect = lambda seconds: self.connection.close.assert_called_once()
        with patch.object(cli, "gate_load", return_value=(1.0, 40.0)), \
                patch.object(cli, "interactive_builders", return_value=([], "")), \
                patch.object(cli, "log_root", return_value=Path("/bulk")), \
                patch.object(cli, "writable", return_value=True), \
                patch.object(cli, "TICK_SECONDS", 30, create=True):
            code, _ = self.call("assignments", "dispatch")
        self.assertEqual(code, 0)
        run.archive.assert_called_once()
        (seconds,), _ = run.archive_wait.call_args
        self.assertTrue(25 < seconds <= 30, seconds)

    def tick(self, run):
        with patch.object(cli, "gate_load", return_value=(1.0, 40.0)), \
                patch.object(cli, "interactive_builders", return_value=([], "")), \
                patch.object(cli, "log_root", return_value=Path("/bulk")):
            return self.call("assignments", "dispatch")

    def test_a_locked_database_skips_the_tick_with_exit_0(self):
        """pack sd:6494: `database is locked` in the heartbeat's BEGIN IMMEDIATE printed a traceback, rc=1."""
        run = self.builds()
        run.dispatch.side_effect = sqlite3.OperationalError("database is locked")
        code, output = self.tick(run)
        self.assertEqual((code, output), (0, "skipped: database is locked\n"))
        self.connection.close.assert_called_once()

    def test_a_tick_past_its_deadline_skips_with_exit_0(self):
        """pack sd:6494: three slots ran to the job's 90 s JOB_TIMEOUT, printing nothing."""
        run = self.builds()
        run.dispatch.side_effect = lambda connection, **kwargs: time.sleep(5)
        started = time.monotonic()
        with patch.object(cli, "TICK_SECONDS", 0.2, create=True):
            code, output = self.tick(run)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((code, output), (0, "skipped: the tick ran past 0.2 s\n"))
        self.connection.close.assert_called_once()

    def test_a_hung_log_folder_probe_claims_nothing_within_its_bound(self):
        """pack sd:6749: a stat on a hung bulk volume blocked the tick before its alarm was set."""
        run = self.builds()
        with patch.object(cli, "PROBE_SECONDS", 0.2, create=True), \
                patch.object(cli.os, "access", side_effect=lambda *_: time.sleep(5)), \
                patch.object(Path, "is_dir", return_value=True):
            started = time.monotonic()
            code, _ = self.tick(run)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(code, 0)
        self.assertIn("did not answer in 0.2 s", run.dispatch.call_args.kwargs["why"])

    def test_an_unwritable_run_log_skips_its_spawn_with_exit_0(self):
        """pack sd:6494: EPERM opening a run's supervisor log failed every tick with rc=1."""
        run = self.builds()
        sys.modules["sd_db.build"].run = Mock(return_value={"repo": "/repo"})
        run.log_dir = lambda root, repo: root
        run.spawn = Mock(side_effect=PermissionError(1, "Operation not permitted"))
        run.dispatch.side_effect = lambda connection, **kwargs: (kwargs["spawn"]("r1"), {
            "reaped": [], "moved": [], "swept": [], "claimed": [], "drafted": [], "spawned": ["r1"], "why": ""})[1]
        code, output = self.tick(run)
        self.assertEqual(code, 0)
        self.assertIn("spawned 0; no claim: cannot start r1's supervisor", output)
        self.assertIn("Operation not permitted", output)

    def test_interactive_builders_count_as_the_dashboard_counts_them(self):
        """Review round 1: one entry per slot the dashboard's own `claude_sessions.builders` counts."""
        with patch.object(sys, "path", [str(ROOT / "dashboard"), *sys.path]):
            sessions_module = importlib.import_module("sd_dashboard.claude_sessions")
        def session(ident, status, builder, agents, cwd="/repos/a/src"):
            return {"id": ident, "name": ident, "cwd": cwd, "status": status, "builder": builder, "age": 5,
                    "safe": {"state": "yes", "why": []},
                    "agents": [{"name": f"{ident}-{n}", "builder": kind, "state": state, "age": 5}
                               for n, (kind, state) in enumerate(agents)]}
        listed = [session("s1", "busy", True, [(True, "running"), (False, "running"), (True, "stalled")]),
                  session("s2", "idle", True, [(True, "running")], cwd="/elsewhere"),
                  session("s3", "waiting", True, []), session("s4", "unknown", False, [(True, "unknown")])]
        document = {"sessions": listed, "builders": sessions_module.builders(listed, "")}
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE repo (path TEXT)")
        connection.execute("INSERT INTO repo VALUES ('/repos/a')")
        with patch.object(sessions_module, "document", return_value=document):
            found, why = cli.interactive_builders(connection, "2026-10-25T09:00:00+00:00", lambda path: path)
        self.assertEqual((len(found), why), (document["builders"]["running"], ""))
        # pack sd:6400: s1's stalled builder holds no slot.
        self.assertEqual([row["repo"] for row in found if row["session_id"] == "s1"], ["/repos/a"] * 2)

    def test_an_unread_session_is_one_slot_and_an_uncounted_machine_says_why(self):
        """pack sd:8606: a session whose transcript was not read made the whole count None, and the tick said only
        "the interactive builders were not counted", so a satellite claimed nothing for days with no reason on the
        hub. The count reads `builders`' holders, so an unread session is one slot against its repository; an
        uncounted machine names the reason in the tick's `why`."""
        with patch.object(sys, "path", [str(ROOT / "dashboard"), *sys.path]):
            sessions_module = importlib.import_module("sd_dashboard.claude_sessions")
        blind = {"id": "s9", "name": "s9", "cwd": "/repos/a/src", "status": "busy", "builder": False, "age": None,
                 "quiet": 60, "agents": [],
                 "safe": {"state": "unknown", "why": ["no transcript was found, so its last action and builders were not read"]}}
        document = {"sessions": [blind], "builders": sessions_module.builders([blind], "")}
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE repo (path TEXT)")
        connection.execute("INSERT INTO repo VALUES ('/repos/a')")
        with patch.object(sessions_module, "document", return_value=document):
            found = cli.interactive_builders(connection, "2026-10-25T09:00:00+00:00", lambda path: path)
        self.assertEqual(found, ([{"repo": "/repos/a", "session_id": "s9"}], ""))
        unread = {"sessions": None, "builders": sessions_module.builders(None, "claude agents --json gave no answer in 10 s")}
        with patch.object(sessions_module, "document", return_value=unread):
            found = cli.interactive_builders(connection, "2026-10-25T09:00:00+00:00", lambda path: path)
        self.assertEqual(found, (None, "claude agents --json gave no answer in 10 s"))
        run = self.builds()
        sys.modules["sd_db.build"].UNREAD = ("the load was not read", "the interactive builders were not counted")
        with patch.object(cli, "gate_load", return_value=(1.0, 40.0)), \
                patch.object(cli, "interactive_builders", return_value=found), \
                patch.object(cli, "log_root", return_value=Path("/bulk")), \
                patch.object(cli, "writable", return_value=True):
            self.call("assignments", "dispatch")
        kwargs = run.dispatch.call_args.kwargs
        self.assertEqual((kwargs["interactive"], kwargs["why"]),
                         (None, "the interactive builders were not counted: claude agents --json gave no answer in 10 s"))
        # pack sd:8615: the reason is the claim's, not the bulk root's: an uncounted machine still archives its logs.
        run.archive.assert_called_once_with(self.connection, Path("/local"), Path("/bulk"))

    def test_a_builder_in_a_linked_worktree_counts_against_its_repository(self):
        """pack sd:6112: a cwd in a worktree outside the checkout is its registered repository, by git's common dir."""
        with patch.object(sys, "path", [str(ROOT / "dashboard"), *sys.path]):
            sessions_module = importlib.import_module("sd_dashboard.claude_sessions")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            checkout, linked, plain = root / "repos" / "a", root / "worktrees" / "a-branch", root / "plain"
            plain.mkdir()
            # sd:6136: the fixture's git reads no operator config. A global
            # config that signs with a failing program and runs a failing hook
            # stands in for the operator's; the shared clean environment hides it.
            hooks = root / "hooks"
            hooks.mkdir()
            (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
            (hooks / "pre-commit").chmod(0o755)
            hostile = root / "gitconfig"
            hostile.write_text(f"[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n[core]\n\thooksPath = {hooks}\n")
            git = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test"]
            with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(hostile)}):
                cli.subprocess.run([*git, "init", "-q", str(checkout)], check=True, env=clean_environment())
                cli.subprocess.run([*git, "-C", str(checkout), "commit", "-q", "--allow-empty", "-m", "start"],
                                   check=True, env=clean_environment())
                cli.subprocess.run([*git, "-C", str(checkout), "worktree", "add", "-q", "-b", "branch", str(linked)],
                                   check=True, env=clean_environment())
            (linked / "src").mkdir()
            listed = [{"id": ident, "name": ident, "cwd": str(cwd), "status": "busy", "builder": True, "age": 5,
                       "safe": {"state": "yes", "why": []}, "agents": []}
                      for ident, cwd in (("s1", linked / "src"), ("s2", checkout), ("s3", plain))]
            document = {"sessions": listed, "builders": sessions_module.builders(listed, "")}
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute("CREATE TABLE repo (path TEXT)")
            connection.execute("INSERT INTO repo VALUES (?)", (str(checkout),))
            with patch.object(sessions_module, "document", return_value=document):
                found, _ = cli.interactive_builders(connection, "2026-10-25T09:00:00+00:00", lambda path: path)
        self.assertEqual([(row["session_id"], row["repo"]) for row in found],
                         [("s1", str(checkout)), ("s2", str(checkout)), ("s3", None)])

    def test_the_log_folder_is_the_real_bulk_storage_setting(self):
        """Review round 1: `log_root` reads `sd.bulk_storage_root` through `sd_lib`; a malformed one claims nothing."""
        sd_lib = importlib.import_module("sd_lib")
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / sd_lib.CONFIG_RELATIVE_PATH
            config.parent.mkdir(parents=True)
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": home}):
                config.write_text(json.dumps({"config": {"sd": {"bulk_storage_root": home}}}))
                self.assertEqual(cli.log_root(), Path(home))
                config.write_text(json.dumps({"config": {"sd": {"bulk_storage_root": "relative/path"}}}))
                run = self.builds()
                with patch.object(cli, "gate_load", return_value=(1.0, 0.0)), \
                        patch.object(cli, "interactive_builders", return_value=([], "")):
                    code, _ = self.call("assignments", "dispatch")
        self.assertEqual(code, 0)
        self.assertIn("sd.bulk_storage_root", run.dispatch.call_args.kwargs["why"])

    def test_gate_load_reads_the_real_gate_status(self):
        """Review round 1: the real `sd gate status --json`, on a machine with no gate settings: load off is 0."""
        with tempfile.TemporaryDirectory() as home:
            environ = {key: value for key, value in os.environ.items() if not key.startswith("SD_GATE")}
            with patch.dict("os.environ", {**environ, "HOME": home, "XDG_CONFIG_HOME": home, "XDG_STATE_HOME": home},
                            clear=True):
                load, cap = cli.gate_load()
        self.assertIsInstance(load, float)
        self.assertEqual(cap, 0.0, "the gate's default load rule is off, which `build.room` reads as no cap")

    def test_gate_load_is_unread_on_any_odd_answer(self):
        answers = [("", 1), ("{}", 0), (json.dumps({"load": [1, float("nan"), 1], "load_max": 40}), 0),
                   (json.dumps({"load": [1.0, 2.5, 3.0], "load_max": 40}), 0)]
        found = []
        for stdout, code in answers:
            with patch.object(cli.subprocess, "run", return_value=Mock(stdout=stdout, returncode=code)):
                found.append(cli.gate_load())
        self.assertEqual(found, [(None, None), (None, None), (None, None), (2.5, 40.0)])

    def test_invalid_assignment_identity_refuses_and_closes_connection(self):
        with self.assertRaises(cli.WorkRefusal):
            self.call("assignments", "get", "not-an-id", "--json")
        self.service.assignment_state.assert_not_called()
        self.connection.close.assert_called_once()

    def test_services_inventory_is_read_only_and_preserves_scope(self):
        self.launchd.inventory.return_value = {"services": [
            {"id": "system:com.example.server", "state": "running"},
            {"id": "user:com.example.server", "state": "unloaded"}]}
        code, output = self.call("services", "list")
        self.assertEqual(code, 0)
        self.assertIn("system:com.example.server  running", output)
        self.assertIn("user:com.example.server  unloaded", output)
        self.connect.assert_called_once_with(sd_db, write=False)
        self.service.inventory.assert_not_called()

    def test_service_get_preserves_system_identity(self):
        self.call("services", "get", "system:com.example.server", "--json")
        self.launchd.service_state.assert_called_once_with(self.connection, "system:com.example.server")
        self.connect.assert_called_once_with(sd_db, write=False)

    def test_service_restart_preserves_revision_and_observed_state(self):
        self.launchd.restart_service.return_value = {
            "request": {"status": "accepted", "phase": "loaded"},
            "service": {"state": "idle"}}
        code, output = self.call("services", "restart", "com.example.server",
                                 "--if-revision", "inspected", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["service"]["state"], "idle")
        self.launchd.restart_service.assert_called_once_with(
            self.connection, "com.example.server", expected_revision="inspected", who="operator")
        self.connect.assert_called_once_with(sd_db, write=True)

    def test_service_unknown_outcome_exits_one_and_keeps_phase(self):
        self.launchd.stop_service.return_value = {
            "request": {"status": "unknown", "phase": "bootout"}, "service": {"state": "unknown"}}
        code, output = self.call("services", "stop", "com.example.server", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["request"]["phase"], "bootout")
        self.launchd.stop_service.assert_called_once_with(
            self.connection, "com.example.server", expected_revision="current-service", who="operator")

    def test_service_refusal_closes_connection_without_another_action(self):
        self.launchd.start_service.side_effect = sd_db.SdDbError("service changed")
        with self.assertRaisesRegex(cli.WorkRefusal, "service changed"):
            self.call("services", "start", "com.example.server", "--if-revision", "old")
        self.launchd.stop_service.assert_not_called()
        self.launchd.restart_service.assert_not_called()
        self.connection.close.assert_called_once()

    def test_parser_excludes_unsupported_actions_and_client_paths(self):
        for arguments in (("assignments", "retry", "8"),
                          ("jobs", "retry", "daily", "--database", "/tmp/sd.db"),
                          ("jobs", "get", "daily", "--repo", "/tmp/repo"),
                          ("services", "restart", "com.example.server", "--command", "arbitrary"),
                          ("services", "enable", "com.example.server"),
                          ("jobs", "cancel")):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    self.parser.parse_args(arguments)
                self.assertEqual(error.exception.code, 2)
        self.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
