"""CLI boundary checks use a fake service and never execute launchctl."""

import argparse
import contextlib
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
#: The wrapper itself, before a test patches it: a tick's outage must pass through it (pack sd:9541).
CONNECT = cli.sd_handoff_rows.connect
#: What a first skipped tick adds to its reason (pack sd:9541): the streak's count and its first time.
FIRST = " (skipped tick 1 in a row since 2026-11-19T00:00:00Z; tick 3 fails)"


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
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        # pack sd:9541: a tick's skip count; no test reads or clears this machine's own.
        self.stamp = Path(scratch.name) / "sd" / "dispatch-skipped"
        for patcher in (patch.object(cli, "skip_stamp", return_value=self.stamp, create=True),
                        patch.object(cli, "utc_now", return_value="2026-11-19T00:00:00Z", create=True),
                        patch.dict(sys.modules, {"sd_db.operations": self.service, "sd_db.services": self.launchd}),
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
        """pack sd:6093: the Build press from the CLI, the items as `sd:N` or bare; pack sd:8785: through
        `queue_again`, which retries a blocked or cancelled bundle instead of queuing a new one."""
        queue = Mock(return_value=[{"id": 31, "item": 7, "status": "queued"}, {"id": 32, "item": 9, "status": "queued"}])
        builds = types.ModuleType("sd_db.build")
        builds.queue_again = queue
        with patch.dict(sys.modules, {"sd_db.build": builds}), patch.object(sd_db, "build", builds, create=True):
            code, output = self.call("assignments", "build", "sd:7", "9")
            self.call("assignments", "build", "7", "--finding", "check the parser")
            self.assertEqual(queue.call_args.kwargs, {"who": "operator", "finding": "check the parser",
                                                      "expected_branch": None})
            # sd:10897: the branch the caller read reaches the press, which refuses another.
            self.call("assignments", "build", "7", "--finding", "x", "--expected-branch", "build/sd-7")
            self.assertEqual(queue.call_args.kwargs["expected_branch"], "build/sd-7")
            with self.assertRaises(cli.WorkRefusal):
                queue.side_effect = sd_db.SdDbError("item 9 is planning; a build takes a ready item")
                self.call("assignments", "build", "9")
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines(), ["#31  queued  build  sd:7", "#32  queued  build  sd:9"])
        self.assertEqual(queue.call_args_list[0].args[1:], ([7, 9],))
        self.assertEqual(queue.call_args_list[0].kwargs, {"who": "operator", "finding": "", "expected_branch": None})
        self.connect.assert_called_with(sd_db, write=True)
        self.assertEqual(self.connection.close.call_count, 4)

    def test_assignments_get_wait_blocks_until_each_build_ends(self):
        """pack sd:8793: `get N... --wait` polls on a fake clock until every assignment leaves queued, claimed or
        running, then prints each as get does: 0 when all ended, 1 at the timeout."""
        clock = [0.0]
        sleep = Mock(side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        states = {8: iter(["queued", "running", "done"]), 9: iter(["blocked"] * 3)}
        self.service.assignment_state.side_effect = (
            lambda connection, ident: {"id": ident, "status": next(states[ident], "done"), "revision": "r"})
        with patch.object(cli.time, "monotonic", lambda: clock[0]), patch.object(cli.time, "sleep", sleep):
            code, output = self.call("assignments", "get", "8", "9", "--wait", "--json")
            self.assertEqual((code, sleep.call_count), (0, 2))
            self.assertEqual([json.loads(line)["status"] for line in output.splitlines()], ["done", "blocked"])
            self.service.assignment_state.side_effect = None
            clock[0], sleep.call_count = 0.0, 0
            code, output = self.call("assignments", "get", "8", "--wait", "--timeout", "1", "--json")
            self.assertEqual((code, clock[0], json.loads(output)["status"]), (1, 60.0, "queued"))
        with self.assertRaisesRegex(cli.WorkRefusal, "--timeout goes with --wait"):
            self.call("assignments", "get", "8", "--timeout", "1")

    def test_assignments_get_until_merged_exits_1_while_the_delivery_is_pending(self):
        """Review of pack sd:9923: a lane entry merged with its delivery pending read as the build's success."""
        self.service.assignment_state.side_effect = (
            lambda connection, ident: {"id": ident, "item": ident + 100, "status": "done", "revision": "r"})
        self.service.lane_state = Mock(return_value={"id": "e1", "status": "merged", "delivery_pending": True})
        code, output = self.call("assignments", "get", "8", "--wait", "--until", "merged", "--json")
        self.assertEqual((code, json.loads(output)["lane"]["delivery_pending"]), (1, True))

    def test_assignments_get_wait_until_merged_waits_through_the_lane(self):
        """pack sd:8798: `--until merged` waits on past a done build until its item's lane entry ends; 0 only when
        each one merged, 1 when one failed, a build ended other than done, or at the timeout."""
        clock = [0.0]
        sleep = Mock(side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        self.service.assignment_state.side_effect = (
            lambda connection, ident: {"id": ident, "item": ident + 100, "status": "done", "revision": "r"})
        lanes = {108: iter(["pending", "running", "merged"]), 109: iter(["merged"])}
        self.service.lane_state = Mock(side_effect=lambda connection, item: {"id": f"e{item}",
                                                                             "status": next(lanes[item], "merged")})
        with patch.object(cli.time, "monotonic", lambda: clock[0]), patch.object(cli.time, "sleep", sleep):
            code, output = self.call("assignments", "get", "8", "9", "--wait", "--until", "merged", "--json")
            self.assertEqual((code, sleep.call_count), (0, 2))
            self.assertEqual([json.loads(line)["lane"]["status"] for line in output.splitlines()], ["merged"] * 2)
            lanes[108] = iter(["failed"])
            code, output = self.call("assignments", "get", "8", "--wait", "--until", "merged", "--json")
            self.assertEqual((code, json.loads(output)["lane"]["status"]), (1, "failed"))
            self.service.lane_state.side_effect = None
            self.service.lane_state.return_value = {"id": "e1", "status": "running"}
            clock[0] = 0.0
            code, output = self.call("assignments", "get", "8", "--wait", "--until", "merged", "--timeout", "1",
                                     "--json")
            self.assertEqual((code, clock[0]), (1, 60.0))
            self.service.assignment_state.side_effect = (
                lambda connection, ident: {"id": ident, "item": 7, "status": "blocked", "revision": "r"})
            calls = self.service.lane_state.call_count
            code, output = self.call("assignments", "get", "8", "--wait", "--until", "merged", "--json")
            self.assertEqual((code, self.service.lane_state.call_count, "lane" in json.loads(output)),
                             (1, calls, False))
        with self.assertRaisesRegex(cli.WorkRefusal, "--until goes with --wait"):
            self.call("assignments", "get", "8", "--until", "merged")

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
        from sd_db import hub, remote
        kept = (remote.HubUnreachable, remote.BuildMismatch, hub.Fenced, sqlite3.OperationalError)  # sd:9574
        self.connect.assert_called_with(sd_db, write=True, keep=kept)
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
        # pack sd:9541: the lock met the tick's work, so the skip says it began.
        self.assertEqual((code, output), (0, f"skipped (partial): database is locked{FIRST}\n"))
        self.connection.close.assert_called_once()

    def test_a_build_mismatch_skips_the_tick_without_a_follow(self):
        """pack sd:8685: a satellite tick ran `repo-sync follow` on the hub's refusal, past its 75 s budget, for
        24 min; no beat came and the hub ended a run. The tick keeps the refusal; the follow job follows."""
        import sd_lib
        from sd_db import hub, remote
        run = self.builds()
        follow = Mock()
        mismatch = remote.BuildMismatch("package", "1.0", "1.1", hub_build="hub-digest")

        def refused(*_args, **_kwargs):
            if hub.on_mismatch is not None:  # what `hub.Hub.open` does
                hub.on_mismatch(mismatch)
            raise mismatch
        self.connect.side_effect = refused
        with patch.object(hub, "on_mismatch", follow), patch.object(sd_lib, "on_mismatch", follow):
            code, output = self.tick(run)
        follow.assert_not_called()
        run.dispatch.assert_not_called()
        self.assertEqual((code, output), (0, "skipped: sd_db build mismatch; the follow job follows\n"))

    def outcome(self, run, error, where, *flags):
        """The tick's exit, as `bin/sd` gives it: a refusal is 1. `error` raises at the connect, through the real
        wrapper, or mid-tick from `dispatch`."""
        if where == "connect":
            fake = types.SimpleNamespace(connect=Mock(side_effect=error))
            self.connect.side_effect = lambda _sd_db, **kwargs: CONNECT(fake, **kwargs)
        else:
            self.connect.side_effect = None
            run.dispatch.side_effect = error
        try:
            with patch.object(cli, "gate_load", return_value=(1.0, 40.0)), \
                    patch.object(cli, "interactive_builders", return_value=([], "")), \
                    patch.object(cli, "log_root", return_value=Path("/bulk")), \
                    patch.object(cli, "writable", return_value=True):
                return self.call("assignments", "dispatch", *flags)
        except (cli.WorkRefusal, cli.sd_handoff_rows.RowsRefusal) as refusal:
            return 1, str(refusal)

    def outages(self):
        from sd_db import remote
        unreachable = remote.HubUnreachable("hub.example.test", 8769, "timed out")
        behind = remote.BuildMismatch("package", "1.0", "1.1")  # this satellite is older: its follow job catches up
        return ((unreachable, f"{unreachable}{FIRST}"), (behind, "sd_db build mismatch; the follow job follows"))

    def test_a_hub_outage_skips_the_tick_with_exit_0(self):
        """pack sd:9541: a satellite tick exited 1 when the hub timed out, or between a hub refresh and the follow;
        the next tick was clean. Each skips with one line. Mid-tick, work the tick began may have committed, so the
        line says `(partial)`."""
        for error, reason in self.outages():
            for where, label in (("connect", "skipped"), ("tick", "skipped (partial)")):
                with self.subTest(error=type(error).__name__, where=where):
                    self.stamp.unlink(missing_ok=True)  # each case is a first outage
                    run = self.builds()
                    code, output = self.outcome(run, error, where)
                    if where == "connect":
                        run.dispatch.assert_not_called()
                    self.assertEqual((code, output), (0, f"{label}: {reason}\n"))

    def test_a_skipped_tick_answers_json_under_json(self):
        """pack sd:9541: `--json` printed a skip as plain text, which no JSON reader parses."""
        for error, reason in self.outages():
            for where, partial in (("connect", False), ("tick", True)):
                with self.subTest(error=type(error).__name__, where=where):
                    self.stamp.unlink(missing_ok=True)  # each case is a first outage
                    code, output = self.outcome(self.builds(), error, where, "--json")
                    self.assertEqual((code, json.loads(output)), (0, {"skipped": reason, "partial": partial}))

    def test_a_fenced_hub_still_exits_1(self):
        """pack sd:9541 review: a demoted hub's write refusal is lasting. A `Fenced` with no `cause`, as an old hub
        sends, reads as lasting too (pack sd:9574). Neither skips."""
        from sd_db import hub, remote
        demoted = hub.Fenced(hub.demotion(Path("/db/sd.sqlite"), hub.Role(
            "elsewhere", "hub.example.test", "sd.hub_name names hub.example.test, and this node is old.example.test")))
        unnamed = remote.rebuild_error(remote.describe_error(hub.Fenced(
            "refused: this machine is not the hub: Tailscale does not name this node, so it cannot match "
            "hub.example.test: this node is not connected to the Tailscale control plane")))
        for error in (demoted, unnamed):
            for where in ("connect", "tick"):
                with self.subTest(error=str(error)[:40], where=where):
                    self.assertEqual(self.outcome(self.builds(), error, where), (1, str(error)))

    def test_a_tailscale_unnamed_fence_counts_in_the_streak(self):
        """pack sd:9574: a hub whose Tailscale names no node raised `Fenced` with `cause` `tailscale` (sd:9543),
        and each such tick exited 1 at once. It is an outage like an unreachable hub: two skip, the third exits 1."""
        from sd_db import hub, remote
        text = ("refused: this machine is not the hub: Tailscale does not name this node, so it cannot match "
                "hub.example.test: this node is not connected to the Tailscale control plane")
        unnamed = remote.rebuild_error(remote.describe_error(hub.Fenced(text, state="unknown", cause="tailscale")))
        self.assertEqual(self.outcome(self.builds(), unnamed, "connect"), (0, f"skipped: {text}{FIRST}\n"))
        code, output = self.outcome(self.builds(), unnamed, "tick")
        self.assertEqual(code, 0)
        self.assertIn("skipped (partial)", output)
        self.assertEqual(self.outcome(self.builds(), unnamed, "connect"), (1, text))

    def test_only_a_hub_behind_exits_1_with_its_remedy(self):
        """pack sd:9541 review: a mismatch whose `upgrade` names the hub needs `repo-sync.sh refresh` there, which no
        follow job runs: it exits 1 with its remedy. `satellite`, `both` (every hub refresh: the package version is
        fixed, so only the digests differ) and no `upgrade` skip, as sd:8685 decided; a lasting one is the follow
        job's alert. Read from `upgrade`, never the text; each is rebuilt as the satellite gets it from the hub."""
        from sd_db import remote

        def wired(field, satellite, hub, *, upgrade=True):
            described = remote.describe_error(remote.BuildMismatch(field, satellite, hub))
            if not upgrade:
                del described["attributes"]["upgrade"]  # a hub that sends no direction
            return remote.rebuild_error(described)
        cases = (("satellite", wired("package", "1.0", "1.1")), ("hub", wired("package", "1.1", "1.0")),
                 ("both", wired("build", "80a4eacae0c346b5", "c3866a94f3d93ddf")),
                 (None, wired("package", "1.0", "1.1", upgrade=False)))
        for upgrade, error in cases:
            self.assertEqual(getattr(error, "upgrade", None), upgrade)
            for where, label in (("connect", "skipped"), ("tick", "skipped (partial)")):
                with self.subTest(upgrade=upgrade, where=where):
                    self.stamp.unlink(missing_ok=True)  # each case is a first skip
                    expected = ((1, str(error)) if upgrade == "hub"
                                else (0, f"{label}: sd_db build mismatch; the follow job follows\n"))
                    self.assertEqual(self.outcome(self.builds(), error, where), expected)

    def unreachable(self, where="connect"):
        from sd_db import remote
        return self.outcome(self.builds(), remote.HubUnreachable("hub.example.test", 8769, "timed out"), where)

    def test_the_third_unreachable_tick_in_a_row_exits_1(self):
        """pack sd:9541 review: every unreachable tick exited 0, so a lasting outage or a wrong port never alerted.
        The first two in a row skip; the third, and each after it, exits 1 with the hub's own error."""
        error = "the sd hub at hub.example.test:8769 is unreachable: timed out"
        self.assertEqual(self.unreachable(), (0, f"skipped: {error}{FIRST}\n"))
        code, output = self.unreachable()
        self.assertEqual(code, 0)
        self.assertIn("skipped tick 2 in a row since 2026-11-19T00:00:00Z", output)
        self.assertEqual(self.unreachable(), (1, error))
        self.assertEqual(self.unreachable(), (1, error))
        self.assertEqual(json.loads(self.stamp.read_text()), {"first": "2026-11-19T00:00:00Z", "count": 4})

    def test_a_hub_that_drops_every_tick_mid_way_still_alerts(self):
        """pack sd:9541 review: the stamp was cleared at the connect, so a hub that took the connect and dropped
        each request after it never alerted. A connect is not a pass: mid-tick outages count in the same streak."""
        error = "the sd hub at hub.example.test:8769 is unreachable: timed out"
        self.assertEqual([self.unreachable("tick")[0], self.unreachable("tick")[0]], [0, 0])
        self.assertEqual(self.unreachable("tick"), (1, error))

    def test_a_completed_tick_ends_the_skip_streak(self):
        """pack sd:9541 review: only a tick that ran to its end removes the stamp; two skips of any kind, then a
        whole tick, and the next skip starts at one."""
        self.unreachable()
        run = self.builds()
        self.connect.side_effect = None
        run.dispatch.side_effect = sqlite3.OperationalError("database is locked")
        self.assertEqual(self.tick(run)[0], 0)
        self.assertEqual(json.loads(self.stamp.read_text())["count"], 2)
        self.connect.side_effect = None
        code, _ = self.tick(self.builds())
        self.assertEqual(code, 0)
        self.assertFalse(self.stamp.exists())
        code, output = self.unreachable()
        self.assertEqual(code, 0)
        self.assertIn("skipped tick 1 in a row", output)

    def test_three_ticks_past_the_deadline_in_a_row_exit_1(self):
        """pack sd:9541 review: a hub that takes the connect and then hangs trips the tick's deadline before its own
        answer timeout, and that skip counted nothing, so it never alerted. Every skip counts in one streak."""
        codes = []
        for _ in range(3):
            run = self.builds()
            run.dispatch.side_effect = lambda connection, **kwargs: time.sleep(5)
            with patch.object(cli, "TICK_SECONDS", 0.2, create=True):
                try:
                    codes.append(self.tick(run))
                except cli.WorkRefusal as refusal:
                    codes.append((1, str(refusal)))
        self.assertEqual([code for code, _ in codes], [0, 0, 1])
        self.assertEqual(codes[2][1], "the tick ran past 0.2 s")

    def test_three_mixed_skips_in_a_row_exit_1_with_the_last_reason(self):
        """pack sd:9541 review: an outage, a lock mid-tick and a lock at the open are one streak; the third fails
        with its own text. A mismatch between them neither counts nor resets."""
        from sd_db import remote
        self.assertEqual(self.unreachable()[0], 0)
        run = self.builds()
        self.connect.side_effect = None
        run.dispatch.side_effect = sqlite3.OperationalError("database is locked")
        self.assertEqual(self.tick(run)[0], 0)
        mismatch = remote.BuildMismatch("build", "80a4eacae0c346b5", "c3866a94f3d93ddf")
        self.assertEqual(self.outcome(self.builds(), mismatch, "connect")[0], 0)
        self.assertEqual(json.loads(self.stamp.read_text())["count"], 2)
        locked = sqlite3.OperationalError("database is locked")
        self.assertEqual(self.outcome(self.builds(), locked, "connect"), (1, "database is locked"))

    def test_a_lock_at_the_open_is_a_counted_skip(self):
        """pack sd:9541 review: a lock while the database opens (its version check or WAL setup) came wrapped as a
        refusal and exited 1 at once; it is the same locked-database skip as one mid-tick."""
        locked = sqlite3.OperationalError("database is locked")
        self.assertEqual(self.outcome(self.builds(), locked, "connect"),
                         (0, f"skipped: database is locked{FIRST}\n"))
        other = sqlite3.OperationalError("disk I/O error")
        self.assertEqual(self.outcome(self.builds(), other, "connect"), (1, "disk I/O error"))

    def test_a_mismatch_tick_neither_counts_nor_resets(self):
        """pack sd:9541 review: a satellite stays mismatched 5-9 min after every pack merge, so a counted mismatch
        alerted on each merge. Five in a row exit 0, and a streak already begun keeps its count (pack sd:8685)."""
        from sd_db import remote
        mismatch = remote.BuildMismatch("build", "80a4eacae0c346b5", "c3866a94f3d93ddf")
        for where in ("connect", "tick"):
            for _ in range(5):
                with self.subTest(where=where):
                    self.assertEqual(self.outcome(self.builds(), mismatch, where)[0], 0)
                    self.assertFalse(self.stamp.exists())
        self.assertEqual(self.unreachable()[0], 0)
        for _ in range(5):
            self.assertEqual(self.outcome(self.builds(), mismatch, "connect")[0], 0)
        self.assertEqual(json.loads(self.stamp.read_text())["count"], 1)

    def test_a_corrupt_stamp_starts_a_streak_never_a_pass(self):
        """pack sd:9541 review: a stamp that does not parse is one failure, rewritten whole; it is never healthy."""
        for garbage in ("not json", '{"count": "two"}', '{"count": -3}', "[]"):
            with self.subTest(garbage=garbage):
                self.stamp.parent.mkdir(parents=True, exist_ok=True)
                self.stamp.write_text(garbage)
                code, output = self.unreachable()
                self.assertEqual(code, 0)
                self.assertIn("skipped tick 1 in a row", output)
                self.assertEqual(json.loads(self.stamp.read_text())["count"], 1)

    def test_an_unwritten_stamp_exits_1(self):
        """pack sd:9541 review: a count that was not recorded is unknown, so the tick fails rather than skip."""
        self.stamp.parent.parent.mkdir(parents=True, exist_ok=True)
        self.stamp.parent.write_text("a file where the folder goes")
        code, output = self.unreachable()
        self.assertEqual(code, 1)
        self.assertIn("is unreachable: timed out", output)
        self.assertIn("was not recorded", output)

    def test_an_outage_at_the_deadline_is_handled_whole(self):
        """pack sd:9541 review: the tick's alarm stayed armed while the stamp was written, so the deadline could
        escape the skip as a traceback. The alarm ends before the outage is counted."""
        real = os.replace

        def slow(source, target):
            time.sleep(0.5)
            real(source, target)
        with patch.object(cli, "TICK_SECONDS", 0.2, create=True), patch.object(cli.os, "replace", slow):
            code, output = self.unreachable("tick")
        self.assertEqual(code, 0)
        self.assertIn("skipped tick 1 in a row", output)
        self.assertEqual(json.loads(self.stamp.read_text())["count"], 1)

    def test_a_clock_jump_does_not_reset_the_streak(self):
        """pack sd:9541 review: the count, not the clock, ends the skips: a stamp whose first failure is in the
        future (the clock went back) still makes the third tick fail."""
        self.stamp.parent.mkdir(parents=True, exist_ok=True)
        self.stamp.write_text(json.dumps({"first": "2099-01-01T00:00:00Z", "count": 2}))
        self.assertEqual(self.unreachable()[0], 1)

    def test_another_hub_error_still_exits_1(self):
        """pack sd:9541: only the outages skip; a lost commit answer or a broken wire stays a refusal."""
        from sd_db import remote
        broken = remote.RemoteError("the wire broke")
        lost = remote.UnknownOutcome("r-1", "the hub stayed unreachable")
        for error, where in ((broken, "connect"), (lost, "tick")):
            with self.subTest(error=type(error).__name__, where=where):
                self.assertEqual(self.outcome(self.builds(), error, where), (1, str(error)))

    def test_the_load_read_does_not_follow_either(self):
        """pack sd:8685: `sd gate status`, the tick's child, carries the marker that keeps the refusal."""
        from sd_db import follow
        done = types.SimpleNamespace(returncode=1, stdout="")
        with patch.object(cli.subprocess, "run", return_value=done) as spawned:
            cli.gate_load()
        self.assertEqual(spawned.call_args.kwargs["env"][follow.MARKER], "1")

    def test_a_tick_past_its_deadline_skips_with_exit_0(self):
        """pack sd:6494: three slots ran to the job's 90 s JOB_TIMEOUT, printing nothing."""
        run = self.builds()
        run.dispatch.side_effect = lambda connection, **kwargs: time.sleep(5)
        started = time.monotonic()
        with patch.object(cli, "TICK_SECONDS", 0.2, create=True):
            code, output = self.tick(run)
        self.assertLess(time.monotonic() - started, 2)
        # pack sd:9541: the deadline met the tick's work, so the skip says it began.
        self.assertEqual((code, output), (0, f"skipped (partial): the tick ran past 0.2 s{FIRST}\n"))
        self.connection.close.assert_called_once()

    def test_a_deadline_before_the_connect_is_a_plain_skip(self):
        """pack sd:9541: `partial` is the tick's own flag, set once it connected: a slow load read began nothing."""
        run = self.builds()
        run.dispatch.side_effect = AssertionError("the tick never connected")
        with patch.object(cli, "TICK_SECONDS", 0.2, create=True), \
                patch.object(cli, "gate_load", side_effect=lambda: time.sleep(5)), \
                patch.object(cli, "interactive_builders", return_value=([], "")), \
                patch.object(cli, "log_root", return_value=Path("/bulk")), \
                patch.object(cli, "writable", return_value=True):
            code, output = self.call("assignments", "dispatch", "--json")
        self.assertEqual((code, json.loads(output)), (0, {"skipped": f"the tick ran past 0.2 s{FIRST}", "partial": False}))
        self.connect.assert_not_called()

    def test_a_hung_bulk_root_probe_claims_within_its_bound_and_skips_the_archive(self):
        """pack sd:6749: a stat on a hung bulk volume blocked the tick before its alarm was set. pack sd:8845: a live
        run logs locally (sd:8246), so a hung bulk root holds only the archive, not the claim."""
        run = self.builds()
        with patch.object(cli, "PROBE_SECONDS", 0.2, create=True), \
                patch.object(cli.os, "access", side_effect=lambda *_: time.sleep(5)), \
                patch.object(Path, "is_dir", return_value=True):
            started = time.monotonic()
            code, output = self.tick(run)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(code, 0)
        self.assertEqual(run.dispatch.call_args.kwargs["why"], "")
        run.archive.assert_not_called()
        self.assertIn("no archive: the bulk root /bulk did not answer in 0.2 s", output)

    def test_an_unwritable_bulk_root_claims_and_skips_the_archive(self):
        """pack sd:8845: an ejected or read-only bulk root refused every new build, though a live run logs to the
        local folder (sd:8246); the ended runs' files wait there for a tick whose bulk root takes a write."""
        run = self.builds()
        with patch.object(cli, "writable", return_value=False):
            code, output = self.tick(run)
        self.assertEqual(code, 0)
        self.assertEqual(run.dispatch.call_args.kwargs["why"], "")
        run.archive.assert_not_called()
        self.assertIn("claimed 1", output)
        self.assertIn("no archive: the bulk root /bulk is not writable", output)

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
        self.assertEqual(cap, 1.5 * os.cpu_count(), "sd:9094: the builder cap, not the gate's rule, which is off")

    def test_gate_load_is_unread_on_any_odd_answer(self):
        answers = [("", 1), ("{}", 0), (json.dumps({"load": [1, float("nan"), 1], "load_max": 40}), 0),
                   (json.dumps({"load": [1.0, 2.5, 3.0], "load_max": 0}), 0)]
        found = []
        for stdout, code in answers:
            with patch.object(cli.subprocess, "run", return_value=Mock(stdout=stdout, returncode=code)), \
                    patch("os.cpu_count", return_value=16):
                found.append(cli.gate_load())
        # sd:9094: the gate's load rule off (0) leaves the builder cap, 1.5x the cores.
        self.assertEqual(found, [(None, None), (None, None), (None, None), (2.5, 24.0)])

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

    def test_services_list_record_stores_this_machines_reading_for_health(self):
        """sd:9757: a satellite's job runs `sd services list --record`; its Disk and Services go to the database."""
        inventory = {"services": [{"id": "user:com.example.server", "state": "running"}]}
        self.launchd.inventory.return_value = inventory
        with patch.object(cli, "record_machine") as record:
            code, output = self.call("services", "list", "--record", "900")
        self.assertEqual(code, 0)
        self.assertIn("user:com.example.server  running", output)
        record.assert_called_once_with(self.connection, 900, inventory)
        self.connect.assert_called_once_with(sd_db, write=True)

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

    def pickup_database(self):
        """A real store for `assignments pickup`: each call gets its own connection, as the CLI closes it."""
        from sd_db.database import connect
        from sd_db.migrate import initialise
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        database = Path(scratch.name) / "sd.db"
        initialise(database)
        self.connect.side_effect = lambda library, write=False: connect(database)  # the CLI closes each
        return database

    def test_assignments_pickup_reads_and_sets_auto_pickup(self):
        """pack sd:10001: `pickup` prints the state; `pickup on` writes it bound to the value read, then
        kickstarts the dispatcher as the panel does; `pickup off` clears it and kickstarts nothing."""
        build = importlib.import_module("sd_db.build")  # the module the CLI imports: setUp resets sys.modules
        from sd_db.database import connect
        database = self.pickup_database()
        kick = Mock(return_value={"ok": True, "why": ""})
        with patch.object(cli, "kickstart_dispatch", kick, create=True):
            code, output = self.call("assignments", "pickup")
            self.assertEqual((code, output.split()[:2]), (0, ["auto-pickup", "off"]))
            code, output = self.call("assignments", "pickup", "on", "--json")
            self.assertEqual((code, json.loads(output)["on"]), (0, True))
            kick.assert_called_once_with()
            with contextlib.closing(connect(database)) as reader:
                self.assertTrue(build.pickup_state(reader)["on"])
            code, output = self.call("assignments", "pickup", "off")
            self.assertEqual((code, output.split()[:2]), (0, ["auto-pickup", "off"]))
            self.assertEqual(kick.call_count, 1)
        with contextlib.closing(connect(database)) as reader:
            self.assertFalse(build.pickup_state(reader)["on"])

    def test_assignments_pickup_writes_nothing_over_a_stale_value(self):
        """pack sd:10001: a value that moved after the read is SettingStale, exit 1, and no row."""
        build = importlib.import_module("sd_db.build")  # the module the CLI imports: setUp resets sys.modules
        from sd_db.database import connect
        database = self.pickup_database()
        stale = {"on": True, "value": "on", "since": None}
        kick = Mock()
        with patch.object(cli, "kickstart_dispatch", kick, create=True), \
                patch.object(build, "pickup_state", side_effect=[stale]), redirect_stderr(io.StringIO()) as error:
            code, _ = self.call("assignments", "pickup", "off")
        self.assertEqual(code, 1)
        self.assertIn("changed since", error.getvalue())
        kick.assert_not_called()
        with contextlib.closing(connect(database)) as reader:
            rows = reader.execute("SELECT COUNT(*) FROM state WHERE kind = 'setting' AND key = ?",
                                  (build.AUTO_PICKUP,)).fetchone()[0]
        self.assertEqual(rows, 0)

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
