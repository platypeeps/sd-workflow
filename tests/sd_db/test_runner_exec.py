"""Finite palette authority, write-ahead notes, and untrusted-request refusals."""
import ast
import copy
import inspect
import json
import os
import pwd
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sd_db import runner, runner_exec, workflow
from sd_db.database import connect
from sd_db.errors import SdDbError
from sd_db.migrate import initialise
from sd_db.testing.wire import hub_only
from sd_db.writes import (
    add_note,
    create_assignment,
    create_item,
    record_state,
    upsert_repo,
)

# How long a fixture command may run before the suite stops it itself.
GUARD_SECONDS = 60

LIB = Path(__file__).resolve().parents[2] / "lib"

#: A dashboard, as far as the palette goes: a process that runs one own-group command (sd:5808).
DASHBOARD = ("import json, sys\n"
             f"sys.path.insert(0, {str(LIB)!r})\n"
             "from sd_db import runner_exec\n"
             "runner_exec.run_process(json.loads(sys.argv[1]), cwd=sys.argv[2], home=sys.argv[3],"
             " timeout=60, own_group=True)\n")


def group_program(pids):
    """A command that starts a child in its group, records both pids at `pids`, and sleeps.

    Both outlast `GUARD_SECONDS` many times over, so only a kill ends them in a test.
    The child holds no output pipe: a run reads the output until every writer closes it.
    """
    return (f"#!{sys.executable}\nimport os,subprocess,time\n"
            "child = subprocess.Popen(['/bin/sleep', '600'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"open({str(pids) + '.tmp'!r}, 'w').write('%d %d' % (os.getpid(), child.pid))\n"
            f"os.replace({str(pids) + '.tmp'!r}, {str(pids)!r})\n"
            "os.write(1, b'ready\\n')\ntime.sleep(600)\n")


def end(pid):
    """SIGKILL `pid` if it still runs."""
    if running(pid):
        os.kill(pid, signal.SIGKILL)


def running(pid):
    """Alive and not a zombie waiting for its parent to reap it."""
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


class PaletteFixture(unittest.TestCase):
    """The store, the repository, the item and the one-entry catalog.

    Its own class so a second case can take the fixture without taking
    `Palette`'s tests with it and running every one of them twice.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "sd.db"; initialise(self.path)
        self.db = connect(self.path); self.addCleanup(self.db.close)
        self.repo = self.root / "repo"; self.repo.mkdir()
        upsert_repo(self.db, str(self.repo), remote="https://example.invalid/repo.git")
        self.item = create_item(self.db, kind="task", title="Fixture", repo=str(self.repo), branch="item/one", status="ready")
        self.program = self.root / "fixture-command"
        self.program.write_text("#!/usr/bin/env python3\nprint('fixture output')\n"); self.program.chmod(0o755)
        self.file = self.root / "commands.yaml"
        self.entries = {"inspect": {"argv": [str(self.program), "{item}"], "screens": ["item"], "mutates": False,
                                  "scope": "worktree", "placeholders": {"item": "item"}}}
        self.write()

    def write(self):
        self.file.write_text("version: 1\ncommands:\n" + "".join("  " + name + ": " + json.dumps(entry) + "\n" for name, entry in self.entries.items()))

    def snapshot(self): return tuple(self.db.iterdump())

    def prepare(self, **changes):
        args = {"item": self.item, "command": "inspect", "values": {"item": self.item},
                "expected_revision": workflow.item_state(self.db, self.item)["revision"],
                "expected_catalog": runner_exec.catalog(path=self.file)["sha256"], "path": self.file, "who": "operator"}
        args.update(changes)
        return runner_exec.prepare(self.db, **args)


class Palette(PaletteFixture):
    @hub_only
    def test_readonly_prepare_records_before_any_process_and_completes_exact_log(self):
        with patch("subprocess.Popen", side_effect=AssertionError("prepare started a process")):
            prepared = self.prepare()["execution"]
        row = self.db.execute("SELECT * FROM note WHERE id=?", (prepared["note"],)).fetchone()
        self.assertEqual(row["kind"], "exec"); self.assertIsNone(row["exit_code"])
        self.assertIsNotNone(row["started"]); self.assertEqual(row["output_path"], prepared["output_path"])
        with self.assertRaises(workflow.WorkflowError):
            runner_exec.complete(self.db, prepared["note"], exit_code=0, output_path="/different")
        runner_exec.complete(self.db, prepared["note"], exit_code=0, output_path=prepared["output_path"])
        self.assertEqual(runner_exec.read_execution(self.db, prepared["note"])["exit_code"], 0)

    def test_unknown_entry_injected_values_wrong_screen_and_stale_refuse_atomically(self):
        before = self.snapshot()
        for change in ({"command": "absent"}, {"values": {"item": "1; touch /tmp/bad"}},
                       {"values": {"item": True}}, {"values": {"item": self.item, "argv": ["sh", "-c", "bad"]}},
                       {"screen": "backlog"}, {"expected_catalog": "0" * 64}, {"expected_revision": "0" * 64}):
            with self.subTest(change=change), self.assertRaises(SdDbError): self.prepare(**change)
            self.assertEqual(before, self.snapshot())

    @hub_only
    def test_missing_mutates_scope_shell_and_embedded_placeholder_reject_the_entry_by_name(self):
        original = dict(self.entries["inspect"])
        variants = [({key: value for key, value in original.items() if key != "mutates"}, "required"),
                    ({**original, "scope": "unknown"}, "scope"), ({**original, "scope": []}, "scope"),
                    ({**original, "argv": ["/bin/sh", "-c", "touch bad"], "placeholders": {}}, "shell"),
                    ({**original, "argv": [str(self.program), "--item={item}"]}, "complete argument"),
                    ({**original, "argv": "shell string"}, "argument vector"), ({**original, "screens": [["item"]]}, "screen")]
        for entry, rule in variants:
            with self.subTest(rule=rule):
                self.entries["inspect"] = entry; self.write()
                current = runner_exec.catalog(path=self.file)
                self.assertNotIn("inspect", current["entries"])
                self.assertEqual([row["name"] for row in current["rejected"]], ["inspect"])
                self.assertIn(rule, current["rejected"][0]["reason"])
                with self.assertRaisesRegex(workflow.WorkflowError, "rejected from the catalog"):
                    self.prepare(expected_catalog=current["sha256"])

    @hub_only
    def test_one_rejected_entry_leaves_the_others_registered(self):
        self.entries["broken"] = {**self.entries["inspect"], "argv": ["relative/path", "{item}"]}; self.write()
        current = runner_exec.catalog(path=self.file)
        self.assertEqual(list(current["entries"]), ["inspect"])
        self.assertEqual(current["rejected"], [{"name": "broken", "reason": "worktree command needs a fixed absolute executable as argv[0]"}])
        with patch("subprocess.Popen", side_effect=AssertionError("prepare started a process")):
            self.assertEqual(self.prepare()["execution"]["command"], "inspect")

    @hub_only
    def test_changed_entry_or_executable_invalidates_authorization(self):
        descriptor = self.prepare()["execution"]
        self.program.write_text(self.program.read_text() + "# changed\n")
        with self.assertRaisesRegex(workflow.WorkflowError, "changed"):
            runner_exec.verify_descriptor(descriptor)
        self.entries["inspect"]["argv"] = [str(self.program), "--other", "{item}"]; self.write()
        with self.assertRaisesRegex(workflow.WorkflowError, "changed"):
            runner_exec.verify_descriptor(descriptor)

    @hub_only
    def test_mutating_command_queues_only_its_recorded_descriptor(self):
        self.entries["inspect"]["mutates"] = True; self.write()
        def enqueue(connection, items, **options):
            value = runner_exec.validate_enqueue(connection, items, options["scope"])
            identity = create_assignment(connection, item=items[0], role="exec", status="queued")
            connection.execute("UPDATE assignment SET scope=? WHERE id=?", (options["scope"], identity))
            return [{"id": identity, "note": value["note"]}]
        with patch("sd_db.runner.enqueue", side_effect=enqueue):
            result = self.prepare()
        assignment = result["assignments"][0]["id"]
        descriptor = runner_exec.resolve_assignment(self.db, assignment)
        self.assertEqual(descriptor, result["execution"])
        scope = "palette:" + json.dumps(descriptor, sort_keys=True)
        with self.assertRaisesRegex(workflow.WorkflowError, "already belongs"):
            runner_exec.validate_enqueue(self.db, [self.item], scope)
        descriptor["argv"] = ["/bin/sh", "-c", "bad"]
        with self.assertRaises(workflow.WorkflowError): runner_exec.validate_enqueue(self.db, [self.item], "palette:" + json.dumps(descriptor))
        with self.assertRaises(workflow.WorkflowError): runner_exec.validate_enqueue(self.db, [self.item], "item")
        self.assertEqual(list(self.repo.iterdir()), [])

    @hub_only
    def test_foreign_target_and_uncertain_control_are_held(self):
        self.entries["requeue"] = {"argv": ["sd", "runner", "requeue", "{assignment}"], "screens": ["item"],
                                  "mutates": True, "scope": "supervisor", "operation": "requeue", "placeholders": {"assignment": "assignment"}}
        self.write()
        foreign = create_item(self.db, kind="task", title="Other", repo=str(self.repo))
        assignment = create_assignment(self.db, item=foreign, role="author", status="queued")
        with self.assertRaisesRegex(workflow.WorkflowError, "belong"):
            self.prepare(command="requeue", values={"assignment": assignment})
        assignment = create_assignment(self.db, item=self.item, role="author", status="queued")
        state = runner.queue_state(self.db, assignment)
        target = {"assignment": assignment, "revision": state["revision"], "run": None}
        self.prepare(command="requeue", values={"assignment": assignment}, target=target)
        before = self.snapshot()
        with self.assertRaisesRegex(workflow.WorkflowError, "unfinished"):
            self.prepare(command="requeue", values={"assignment": assignment}, target=target)
        self.assertEqual(before, self.snapshot())

    @hub_only
    def test_output_readback_never_reads_arbitrary_note_paths(self):
        secret = self.root / "private"; secret.write_text("must not read")
        note = add_note(self.db, self.item, "exec", "{}", output_path=str(secret))
        with self.assertRaisesRegex(workflow.WorkflowError, "designated"):
            runner_exec.read_execution(self.db, note)

    @hub_only
    def test_real_readonly_process_keeps_output_and_scrubs_environment(self):
        self.program.write_text("#!/usr/bin/python3\nimport os,sys\nprint('item=' + sys.argv[1])\nprint('secret=' + str(os.getenv('PALETTE_SECRET')))\nprint('cwd=' + os.getcwd())\nsys.exit(7)\n")
        descriptor = self.prepare()["execution"]
        with patch.dict(os.environ, {"PALETTE_SECRET": "must-not-leak"}):
            result = runner_exec.execute_immediate(self.db, descriptor["note"])
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("secret=None", result["output"])
        self.assertIn("cwd=" + str(self.repo), result["output"])
        self.assertEqual(list(self.repo.iterdir()), [])
        with self.assertRaisesRegex(workflow.WorkflowError, "already ended"):
            runner_exec.execute_immediate(self.db, descriptor["note"])

    @hub_only
    def test_malformed_descriptor_refuses_without_process(self):
        original = self.prepare()["execution"]
        for value in (None, [], {**original, "registry_path": None}, {**original, "values": []},
                      {**original, "values": {"item": "1;bad"}}, {**original, "extra": "argv"}):
            with self.assertRaises(workflow.WorkflowError): runner_exec.verify_descriptor(value)

    @hub_only
    def test_queued_process_inherits_only_owned_marker_and_clone_scratch(self):
        self.entries["inspect"]["mutates"] = True; self.write()
        with patch("sd_db.runner.enqueue", return_value=[]): value = self.prepare()["execution"]
        clone = self.root / "clone"; (clone / ".git").mkdir(parents=True)
        with patch.dict(os.environ, {"SD_ASSIGNMENT": "foreign"}), self.assertRaisesRegex(workflow.WorkflowError, "identity"):
            runner_exec.process_plan(value)
        with patch.dict(os.environ, {"SD_ASSIGNMENT": "a" * 32, "SECRET_TOKEN": "private"}):
            planned = runner_exec.process_plan(value, cwd=clone)
            self.assertEqual(planned["environment"]["SD_ASSIGNMENT"], "a" * 32)
            self.assertEqual(planned["environment"]["TMPDIR"], str(clone / ".git/sd-tmp"))
            self.assertNotIn("SECRET_TOKEN", planned["environment"])
            result = runner_exec.run_process(value, cwd=clone)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(list(self.repo.iterdir()), [])

    @hub_only
    def test_process_environment_carries_user_from_uid_not_ambient(self):
        """A keychain read keys on USER; the runner used to omit it, so the agent reported itself logged out."""
        value = self.prepare()["execution"]
        expected = pwd.getpwuid(os.getuid()).pw_name
        with patch.dict(os.environ, {"USER": "not-the-running-user"}):
            planned = runner_exec.process_plan(value)
        self.assertEqual(planned["environment"]["USER"], expected)
        self.assertNotEqual(planned["environment"]["USER"], "not-the-running-user")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runner_exec.process_plan(value)["environment"]["USER"], expected)

    @hub_only
    def test_lost_control_response_reconciles_exact_target_without_replay(self):
        self.entries["requeue"] = {"argv": ["sd", "runner", "requeue", "{assignment}"], "screens": ["item"],
                                  "mutates": True, "scope": "supervisor", "operation": "requeue", "placeholders": {"assignment": "assignment"}}
        self.write()
        assignment = create_assignment(self.db, item=self.item, role="author", status="cancelled")
        state = runner.queue_state(self.db, assignment)
        value = self.prepare(command="requeue", values={"assignment": assignment},
            target={"assignment": assignment, "revision": state["revision"], "run": None})["execution"]
        requeue = runner.requeue
        def lost(*args, **kwargs):
            requeue(*args, **kwargs)
            raise workflow.WorkflowError("lost response")
        with patch.object(runner, "requeue", side_effect=lost), self.assertRaisesRegex(workflow.WorkflowError, "lost response"):
            runner_exec.execute_immediate(self.db, value["note"])
        self.assertIsNone(runner_exec.read_execution(self.db, value["note"])["ended"])
        with patch.object(runner, "requeue", side_effect=AssertionError("replayed mutation")):
            result = runner_exec.reconcile(self.db, value["note"])
        self.assertEqual(result["exit_code"], 0)

    @hub_only
    def test_a_refused_requeue_is_rejected_before_dispatch_and_reconciles_closed(self):
        """sd:9269 review: requeue refuses a member cancelled on its own, but the palette wrote its dispatch
        marker first, so reconcile could never close the execution. The refusal now comes before dispatch."""
        self.entries["requeue"] = {"argv": ["sd", "runner", "requeue", "{assignment}"], "screens": ["item"],
                                  "mutates": True, "scope": "supervisor", "operation": "requeue", "placeholders": {"assignment": "assignment"}}
        self.write()
        lead = create_assignment(self.db, item=self.item, role="author", status="cancelled")
        child = create_assignment(self.db, item=self.item, role="author", status="cancelled", parent=lead)
        self.db.execute("UPDATE assignment SET result = ? WHERE id = ?", (runner.ALONE + "operator", child))
        state = runner.queue_state(self.db, child)
        value = self.prepare(command="requeue", values={"assignment": child},
            target={"assignment": child, "revision": state["revision"], "run": None})["execution"]
        with self.assertRaisesRegex(workflow.WorkflowError, "cancelled on its own"):
            runner_exec.execute_immediate(self.db, value["note"])
        self.assertFalse(Path(value["output_path"]).exists(), "no dispatch marker")
        self.assertEqual(runner_exec.reconcile(self.db, value["note"])["exit_code"], 125)
        self.assertEqual(runner.queue_state(self.db, child)["status"], "cancelled")

    @hub_only
    def test_output_collision_and_changed_catalog_never_launch(self):
        value = self.prepare()["execution"]
        with runner_exec.open_output(value) as output: output.write(b"prior evidence")
        with patch("subprocess.Popen", side_effect=AssertionError("launched")), self.assertRaisesRegex(workflow.WorkflowError, "reconcile"):
            runner_exec.run_process(value, cwd=self.repo)
        self.assertEqual(Path(value["output_path"]).read_text(), "prior evidence")

    @hub_only
    def test_cancelled_queued_request_closes_without_process_or_replay(self):
        self.entries["inspect"]["mutates"] = True
        self.write()
        with patch("sd_db.runner.enqueue", return_value=[]):
            value = self.prepare()["execution"]
        assignment = create_assignment(self.db, item=self.item, role="exec", status="cancelled")
        self.db.execute("UPDATE assignment SET scope=? WHERE id=?", ("palette:" + json.dumps(value, sort_keys=True), assignment))
        with patch("subprocess.Popen", side_effect=AssertionError("replayed cancelled work")):
            result = runner_exec.reconcile(self.db, value["note"])
        self.assertEqual(result["exit_code"], 125)
        self.assertEqual(runner.queue_state(self.db, assignment)["status"], "cancelled")
        with self.assertRaisesRegex(workflow.WorkflowError, "unfinished authorization"):
            runner_exec.resolve_assignment(self.db, assignment)

    @hub_only
    def test_restore_hold_blocks_preparation_and_immediate_dispatch(self):
        value = self.prepare()["execution"]
        record_state(self.db, "restore", body={"fixture": True})
        before = self.snapshot()
        with patch("subprocess.Popen", side_effect=AssertionError("dispatched during restore")):
            with self.assertRaisesRegex(workflow.WorkflowError, "restore recovery"):
                self.prepare()
            with self.assertRaisesRegex(workflow.WorkflowError, "restore recovery"):
                runner_exec.execute_immediate(self.db, value["note"])
        self.assertEqual(before, self.snapshot())

    @hub_only
    def test_output_is_bounded_for_a_real_verbose_process(self):
        self.program.write_text("#!/usr/bin/python3\nimport sys\nsys.stdout.write('x' * 3000000)\n")
        value = self.prepare()["execution"]
        result = runner_exec.execute_immediate(self.db, value["note"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(Path(value["output_path"]).stat().st_size, runner_exec.MAX_OUTPUT)

    @contextmanager
    def owned_process(self):
        processes = []
        guards = []
        popen = subprocess.Popen

        def capture(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            # Keep a regressed deadline from hanging the suite indefinitely.
            # Generous: a loaded gate can take seconds to start the interpreter.
            guard = threading.Timer(GUARD_SECONDS, lambda: process.kill() if process.poll() is None else None)
            guard.start()
            guards.append(guard)
            return process

        try:
            with patch.object(runner_exec.subprocess, "Popen", side_effect=capture):
                yield processes
        finally:
            for guard in guards:
                guard.cancel()
                guard.join()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
                if process.stdout is not None:  # an own-group command's watcher has none (sd:5808)
                    process.stdout.close()

    @contextmanager
    def expires_once_ready(self, value):
        """The time limit passes when the command has written `ready`, not on the wall clock.

        A gate at load 125 started the interpreter after a one-second limit had
        already passed, so `ready` never reached the log (sd:2333). The limit is
        arithmetic on `time.monotonic`, so the test owns that clock.
        """
        output = Path(value["output_path"])

        def clock():
            return float("inf") if b"ready\n" in output.read_bytes() else 0.0

        with patch.object(runner_exec, "time", SimpleNamespace(monotonic=clock)):
            yield

    @hub_only
    def test_closed_output_still_waits_for_normal_exit_and_receipt(self):
        self.program.write_text(
            f"#!{sys.executable}\nimport os,sys,time\nos.write(1,b'ready\\n')\n"
            "os.close(1)\nos.close(2)\ntime.sleep(0.1)\nsys.exit(7)\n"
        )
        value = self.prepare()["execution"]
        with self.owned_process() as processes:
            result = runner_exec.run_process(value, cwd=self.repo, home=self.root, timeout=GUARD_SECONDS)
            self.assertEqual(result["exit_code"], 7)
            self.assertEqual(processes[-1].returncode, 7)
            self.assertTrue(processes[-1].stdout.closed)
        self.assertEqual(Path(value["output_path"]).read_bytes(), b"ready\n")
        self.assertEqual(runner_exec.process_result(value)["exit_code"], 7)

    @hub_only
    def test_time_limit_reaps_process_with_open_or_closed_output(self):
        for close_output in (False, True):
            for own_group in (False, True):
                with self.subTest(close_output=close_output, own_group=own_group):
                    self.program.write_text(
                        f"#!{sys.executable}\nimport os,time\nos.write(1,b'ready\\n')\n"
                        + ("os.close(1)\nos.close(2)\n" if close_output else "")
                        + "time.sleep(60)\n"
                    )
                    value = self.prepare()["execution"]
                    started = time.monotonic()
                    with self.owned_process() as processes, self.expires_once_ready(value):
                        with self.assertRaisesRegex(workflow.WorkflowError, "exceeded its time limit"):
                            runner_exec.run_process(value, cwd=self.repo, home=self.root,
                                                    timeout=1, own_group=own_group)
                        # The limit stopped it, not the guard and not the sleep.
                        self.assertLess(time.monotonic() - started, GUARD_SECONDS)
                        self.assertEqual(processes[-1].returncode, -signal.SIGKILL)
                        self.assertTrue(processes[-1].stdout.closed)
                    output = Path(value["output_path"])
                    self.assertIn(b"ready\n", output.read_bytes())
                    self.assertIn(b"Command time limit exceeded", output.read_bytes())
                    self.assertFalse(output.with_suffix(".receipt.json").exists())

    def recorded(self, pids):
        """The pids `group_program` wrote, each killed at cleanup; waits for the command to start, bounded."""
        deadline = time.monotonic() + GUARD_SECONDS
        while not pids.exists():
            self.assertLess(time.monotonic(), deadline, "the command never recorded its pids")
            time.sleep(0.05)
        found = [int(word) for word in pids.read_text().split()]
        for pid in found:
            self.addCleanup(end, pid)
        return found

    def survivors(self, pids):
        """Which of `pids` still run once each has had `GUARD_SECONDS` to end."""
        deadline = time.monotonic() + GUARD_SECONDS
        while any(running(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.1)
        return [pid for pid in pids if running(pid)]

    @hub_only
    def test_a_killed_dashboard_takes_its_command_group_with_it(self):
        """sd:5808: the command ran in a session of its own from a request thread, which can hold no handler.

        launchd stops the dashboard with SIGTERM, which it does not trap, and
        its group kill missed the command's session: the command and its
        children ran on past their time limit. SIGKILL is the same.
        """
        for number in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=number.name):
                pids = self.root / f"pids-{number.name}"
                self.program.write_text(group_program(pids))
                value = self.prepare()["execution"]
                dashboard = subprocess.Popen([sys.executable, "-c", DASHBOARD, json.dumps(value), str(self.repo),
                                              str(self.root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.addCleanup(lambda dashboard=dashboard: dashboard.poll() is None and dashboard.kill())
                found = self.recorded(pids)
                dashboard.send_signal(number)
                self.assertEqual(dashboard.wait(timeout=GUARD_SECONDS), -number)
                self.assertEqual(self.survivors(found), [], "the command's group outlived the dashboard")

    @hub_only
    def test_time_limit_ends_the_commands_whole_group(self):
        """The time limit ends the command's children too, not only the command (sd:5808 keeps it)."""
        pids = self.root / "pids"
        self.program.write_text(group_program(pids))
        value = self.prepare()["execution"]
        with self.owned_process(), self.expires_once_ready(value):
            with self.assertRaisesRegex(workflow.WorkflowError, "exceeded its time limit"):
                runner_exec.run_process(value, cwd=self.repo, home=self.root, timeout=1, own_group=True)
            self.assertEqual(self.survivors(self.recorded(pids)), [], "a child outlived the time limit")

    @hub_only
    def test_a_child_left_by_a_finished_command_ends_with_its_group(self):
        """The command's exit closes the run; the watcher then ends what it left in the group (sd:5808)."""
        pids = self.root / "pids"
        self.program.write_text(group_program(pids).replace("time.sleep(600)", "os._exit(0)"))
        value = self.prepare()["execution"]
        result = runner_exec.run_process(value, cwd=self.repo, home=self.root, timeout=GUARD_SECONDS, own_group=True)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.survivors(self.recorded(pids)), [], "a child outlived its command's run")

    @hub_only
    def test_time_limit_survives_a_group_that_exited_before_its_kill(self):
        """sd:2338: the command can exit between `poll()` and `killpg`.

        macOS answers EPERM for a group whose only member is an unreaped
        leader, and ESRCH once it is reaped. Either means nothing is left to
        kill, so the caller still gets the time limit, not an OSError.
        """
        killpg = os.killpg
        for error in (PermissionError(1, "Operation not permitted"), ProcessLookupError(3, "No such process")):
            with self.subTest(error=type(error).__name__):
                self.program.write_text(f"#!{sys.executable}\nimport os,time\nos.write(1,b'ready\\n')\ntime.sleep(60)\n")
                value = self.prepare()["execution"]

                def exited_first(pid, sig, error=error):
                    killpg(pid, sig)
                    if isinstance(error, PermissionError):
                        # Exited, not reaped: the leader is a zombie when EPERM answers. Wait on the
                        # command too: run_process polls it, and it can outlive the leader (sd:6739).
                        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
                        os.waitid(os.P_PID, processes[-1].pid, os.WEXITED | os.WNOWAIT)
                    else:
                        processes[-1].wait()
                    raise error

                with self.owned_process() as processes, self.expires_once_ready(value), \
                        patch.object(runner_exec.os, "killpg", side_effect=exited_first):
                    with self.assertRaisesRegex(workflow.WorkflowError, "exceeded its time limit; inspect"):
                        runner_exec.run_process(value, cwd=self.repo, home=self.root, timeout=1, own_group=True)
                    self.assertEqual(processes[-1].returncode, -signal.SIGKILL)
                self.assertIn(b"Command time limit exceeded", Path(value["output_path"]).read_bytes())

    @hub_only
    def test_time_limit_does_not_wait_on_a_live_group_that_refuses_the_kill(self):
        """A live leader that changed credentials answers EPERM too; waiting on it outlasts the deadline."""
        self.program.write_text(f"#!{sys.executable}\nimport os,time\nos.write(1,b'ready\\n')\ntime.sleep(60)\n")
        value = self.prepare()["execution"]
        started = time.monotonic()
        with self.owned_process() as processes, self.expires_once_ready(value), \
                patch.object(runner_exec.os, "killpg", side_effect=PermissionError(1, "Operation not permitted")):
            with self.assertRaisesRegex(workflow.WorkflowError, "refused the kill"):
                runner_exec.run_process(value, cwd=self.repo, home=self.root, timeout=1, own_group=True)
            # The run did not wait on it; the group's watcher, which the patch does not reach, ends it after (sd:5808).
            self.assertEqual(processes[-1].wait(timeout=GUARD_SECONDS), -signal.SIGKILL)
        self.assertLess(time.monotonic() - started, GUARD_SECONDS)

    @hub_only
    def test_lost_process_response_reconciles_durable_exit_without_replay(self):
        value = self.prepare()["execution"]
        with patch.object(runner_exec, "complete", side_effect=workflow.WorkflowError("lost completion")), self.assertRaisesRegex(workflow.WorkflowError, "lost completion"):
            runner_exec.execute_immediate(self.db, value["note"])
        self.assertIsNone(runner_exec.read_execution(self.db, value["note"])["ended"])
        with patch("subprocess.Popen", side_effect=AssertionError("replayed command")):
            result = runner_exec.reconcile(self.db, value["note"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("fixture output", result["output"])

    @hub_only
    def test_queued_receipt_binds_exact_run_and_output_before_reconciliation(self):
        self.entries["inspect"]["mutates"] = True
        self.write()
        prepared = self.prepare()
        value = prepared["execution"]
        assignment = prepared["assignments"][0]["id"]
        held = runner.claim(self.db, assignment, owner="fixture", work_root=self.root / "work", retention_root=self.root / "retained")
        clone = Path(held["run"]["work_path"])
        (clone / ".git").mkdir(parents=True)
        with patch.dict(os.environ, {"SD_ASSIGNMENT": held["run"]["id"]}):
            runner_exec.run_process(value, cwd=clone)
        with self.assertRaisesRegex(workflow.WorkflowError, "exact command and attempt"):
            runner_exec.process_result(value, expected_run="b" * 32)
        log = Path(value["output_path"])
        original = log.read_bytes()
        log.write_bytes(b"changed output")
        with self.assertRaisesRegex(workflow.WorkflowError, "exact command and attempt"):
            runner_exec.reconcile(self.db, value["note"])
        log.write_bytes(original)
        with patch("subprocess.Popen", side_effect=AssertionError("replayed queued work")):
            result = runner_exec.reconcile(self.db, value["note"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(runner.queue_state(self.db, assignment)["run"]["released_at"], None)
        self.assertEqual(list(self.repo.iterdir()), [])
        self.program.write_text(self.program.read_text() + "# catalog executable changed after completion\n")
        self.assertEqual(runner_exec.assignment_result(self.db, assignment, held["run"]["id"])["exit_code"], 0)
        with self.assertRaisesRegex(workflow.WorkflowError, "belong"):
            runner_exec.assignment_result(self.db, assignment, "b" * 32)

    def test_catalog_unsafe_permission_refuses(self):
        self.file.chmod(0o666)
        with self.assertRaises(workflow.WorkflowError): runner_exec.catalog(path=self.file)

    @hub_only
    def test_repository_and_log_tampering_refuse_database_bound_authorization(self):
        self.entries["inspect"]["mutates"] = True; self.write()
        with patch("sd_db.runner.enqueue", return_value=[]): original = self.prepare()["execution"]
        for changed in ({**original, "repo": "/different/repo"},
                        {**original, "output_path": str(self.root / "foreign/executions" / ("a" * 32 + ".log"))}):
            self.db.execute("UPDATE note SET body=?,output_path=? WHERE id=?", (json.dumps(changed, sort_keys=True), changed["output_path"], changed["note"]))
            with self.assertRaisesRegex(workflow.WorkflowError, "database authority"):
                runner_exec.validate_enqueue(self.db, [self.item], "palette:" + json.dumps(changed, sort_keys=True))

    def test_catalog_unhashable_operation_rejects_the_entry(self):
        self.entries["inspect"].update(scope="supervisor", operation=[]); self.write()
        current = runner_exec.catalog(path=self.file)
        self.assertEqual(current["entries"], {})
        self.assertEqual([row["name"] for row in current["rejected"]], ["inspect"])
        self.assertIn("unsupported native supervisor operation", current["rejected"][0]["reason"])


class TheStandingRefusal(PaletteFixture):
    """`standing_refusal`: every refusal `prepare` makes alike for every row.

    A batch asks it once and skips its per-row setup when it answers
    (`local-sd-plan/sd_plan.py`), so both directions are its correctness. A
    refusal missing from the set is one the batch writes setup for on every
    night it stands -- a pending restore was sd:786, `inspect` off its screen
    is the shape of sd:805 -- and a row-dependent refusal wrongly inside it
    would stop the batch setting up rows it could still have queued.
    """

    def sha(self):
        return runner_exec.catalog(path=self.file)["sha256"]

    def ask(self, expected_catalog, **changes):
        args = {"command": "inspect", "values": {"item": self.item}, "screen": "item",
                "expected_catalog": expected_catalog, "path": self.file}
        args.update(changes)
        return runner_exec.standing_refusal(self.db, **args)

    def try_prepare(self, expected_catalog, expected_revision):
        """The same request through `prepare`, quoting the same catalog.

        Spelt out rather than taken from `Palette.prepare`, whose default
        reads the catalog: one of the cases below is a catalog that is gone,
        and reading it in the fixture would refuse before the code under test.
        """
        return runner_exec.prepare(self.db, self.item, "inspect", {"item": self.item},
                                   expected_revision=expected_revision,
                                   expected_catalog=expected_catalog, path=self.file, who="operator")

    # Each stage returns the catalog sha the batch would be quoting. Read
    # after the change for the cases that are about what the palette says,
    # and before it for the one case that is about the palette having changed.
    def hold_a_restore(self):
        record_state(self.db, "restore", key="fixture", body={})
        return self.sha()

    def take_the_catalog_away(self):
        was = self.sha(); self.file.unlink(); return was

    def edit_the_catalog(self):
        was = self.sha()
        self.entries["inspect"]["label"] = "edited since the batch read it"; self.write()
        return was

    def move_it_off_this_screen(self):
        self.entries["inspect"]["screens"] = ["today"]; self.write(); return self.sha()

    def break_the_entry(self):
        self.entries["inspect"]["scope"] = "nonsense"; self.write(); return self.sha()

    def change_what_it_takes(self):
        self.entries["inspect"]["argv"] = [str(self.program), "{provider}"]
        self.entries["inspect"]["placeholders"] = {"provider": "provider"}; self.write()
        return self.sha()

    def declare_the_item_a_provider(self):
        self.entries["inspect"]["placeholders"] = {"item": "provider"}; self.write(); return self.sha()

    def declare_the_item_a_destination(self):
        self.entries["inspect"]["placeholders"] = {"item": "destination"}; self.write(); return self.sha()

    #: One case per enumerated refusal, and the words `prepare` names it with.
    #: The last two keep the placeholder's name and change only its kind: the
    #: names still match, so only the value's type refuses, and an int is the
    #: wrong type for either kind whichever row it names.
    STANDING = (("a restore still to finish", "hold_a_restore", "restore recovery must finish"),
                ("a palette that cannot be read", "take_the_catalog_away", "No command palette is configured"),
                ("a palette changed since it was read", "edit_the_catalog", "command catalog changed"),
                ("a command registered on another screen", "move_it_off_this_screen", "not registered on the current screen"),
                ("a command the catalog rejected", "break_the_entry", "was rejected from the catalog"),
                ("values the entry does not declare", "change_what_it_takes", "supply exactly the registered typed values"),
                ("an item placeholder declared a provider", "declare_the_item_a_provider", "provider placeholder must name a configured provider"),
                ("an item placeholder declared a destination", "declare_the_item_a_destination", "destination must be a new absolute directory"))

    @hub_only
    def test_it_answers_every_standing_refusal_and_prepare_makes_the_very_same_one(self):
        """Each staged in turn, asked both ways, and undone before the next.

        `prepare` is asked beside it every time: the enumeration is only worth
        having while the two agree, and the pairing is what says they do.
        """
        pristine = copy.deepcopy(self.entries)
        for name, stage, words in self.STANDING:
            with self.subTest(refusal=name):
                revision = workflow.item_state(self.db, self.item)["revision"]
                catalog = getattr(self, stage)()
                before = self.snapshot()
                refusal = self.ask(catalog)
                self.assertIsNotNone(refusal, "a refusal a batch cannot see is setup it writes every night")
                self.assertIn(words, str(refusal))
                with self.assertRaises(SdDbError) as raised:
                    self.try_prepare(catalog, revision)
                self.assertIn(words, str(raised.exception))
                self.assertEqual(self.snapshot(), before, "a standing refusal writes nothing")
            self.entries = copy.deepcopy(pristine); self.write()
            self.db.execute("DELETE FROM state WHERE kind='restore'"); self.db.commit()
        self.assertIsNone(self.ask(self.sha()))

    @hub_only
    def test_it_does_not_answer_a_refusal_that_resolves_the_value(self):
        """The other direction, and the reason the set is not simply every refusal.

        Each case hands `standing_refusal` the very values `prepare` refuses,
        so moving that check into the standing set would make it answer here.
        What a value resolves to -- a row that is not there, an assignment
        that is not there, a provider registry with no such provider, a
        directory already on disk -- is one row's own turn, and a batch must set the next row up
        and try it.
        """
        taken = self.root / "already-here"; taken.mkdir()
        pristine = copy.deepcopy(self.entries)
        for name, kind, row, value, words in (
                ("an item that is not there", "item", 9999, 9999, "9999"),
                ("an assignment that is not there", "assignment", self.item, 9999, "9999"),
                ("a provider that is not configured", "provider", self.item, "not-configured", "provider registry"),
                ("a destination already on disk", "destination", self.item, str(taken), "destination must be a new absolute directory")):
            with self.subTest(refusal=name):
                self.entries["inspect"]["placeholders"] = {"item": kind}; self.write()
                with self.assertRaises(SdDbError) as raised:
                    self.prepare(item=row, values={"item": value})
                self.assertIn(words, str(raised.exception))
                self.assertIsNone(self.ask(self.sha(), values={"item": value}), "one row's refusal is not the batch's")
            self.entries = copy.deepcopy(pristine); self.write()

    def exec_notes(self):
        return self.db.execute("SELECT COUNT(*) FROM note WHERE item=? AND kind='exec'", (self.item,)).fetchone()[0]

    @hub_only
    def test_asked_for_a_queue_it_refuses_a_command_prepare_would_not_queue(self):
        """sd:814: a worktree command that does not mutate runs at once, and is never queued.

        `prepare` recorded its exec note and returned no assignment, and a
        batch that only ever queues -- the nightly's `plan-item` -- set the
        row up and said it queued it, every night. Asked with `require_queue`,
        both ways refuse alike and write nothing; a mutating worktree command
        asked the same way is not refused.
        """
        revision = workflow.item_state(self.db, self.item)["revision"]
        before = self.snapshot()
        words = "inspect is not a mutating worktree command, so it would not be queued"
        refusal = self.ask(self.sha(), require_queue=True)
        self.assertIsNotNone(refusal, "a refusal a batch cannot see is setup it writes every night")
        self.assertIn(words, str(refusal))
        with self.assertRaises(SdDbError) as raised:
            runner_exec.prepare(self.db, self.item, "inspect", {"item": self.item},
                                expected_revision=revision, expected_catalog=self.sha(),
                                path=self.file, who="operator", require_queue=True)
        self.assertIn(words, str(raised.exception))
        self.assertEqual(self.exec_notes(), 0)
        self.assertEqual(workflow.item_state(self.db, self.item)["revision"], revision)
        self.assertEqual(self.snapshot(), before, "a standing refusal writes nothing")
        self.entries["inspect"]["mutates"] = True; self.write()
        self.assertIsNone(self.ask(self.sha(), require_queue=True))

    @hub_only
    def test_not_asked_for_a_queue_a_non_mutating_command_is_still_prepared(self):
        """The default, which the dashboard and the CLI keep: record the note, queue nothing."""
        self.assertIsNone(self.ask(self.sha()))
        prepared = self.prepare()
        self.assertEqual(prepared["assignments"], [])
        self.assertEqual(self.exec_notes(), 1)

    #: One case per form check in `_shape`, with a value that only that check
    #: refuses and the kind the entry declares for it. Every one is a standing
    #: refusal: nothing here resolves a value, so the answer is the same for
    #: whichever row a batch offers. The enumeration above reaches these kinds
    #: only with an int, which the first line of each branch refuses, so the
    #: provider regex, the NUL/CR/LF scan, the two `int` tests and
    #: `is_absolute` were pinned by no test at all and mutants of all five
    #: survived the suite (sd:820).
    FORMS = (("an item that is not an int", "item", "3", "item placeholder must be this existing item"),
             ("an item that is a bool", "item", True, "item placeholder must be this existing item"),
             ("an assignment that is not an int", "assignment", "3", "assignment placeholder must belong to this item"),
             ("an assignment that is a bool", "assignment", True, "assignment placeholder must belong to this item"),
             ("a provider name with a space", "provider", "not a provider", "provider placeholder must name a configured provider"),
             ("a provider name that is empty", "provider", "", "provider placeholder must name a configured provider"),
             ("a destination with a newline", "destination", "/tmp/one\ntwo", "destination must be a new absolute directory"),
             ("a destination with a NUL", "destination", "/tmp/one\0two", "destination must be a new absolute directory"),
             ("a destination that is relative", "destination", "relative/path", "destination must be a new absolute directory"),
             ("a destination longer than its bound", "destination", "/" + "d" * 4000, "destination must be a new absolute directory"))

    @hub_only
    def test_it_answers_every_form_check_its_entry_kinds_have(self):
        """One case per check in `_shape`, staged and asked both ways.

        The two `int` cases pass a bool as well as a string: `_shape` asks
        `type(value) is not int`, and `isinstance` would let `True` through
        as the id of a row.
        """
        pristine = copy.deepcopy(self.entries)
        for name, kind, value, words in self.FORMS:
            with self.subTest(refusal=name):
                revision = workflow.item_state(self.db, self.item)["revision"]
                self.entries["inspect"]["placeholders"] = {"item": kind}; self.write()
                before = self.snapshot()
                refusal = self.ask(self.sha(), values={"item": value})
                self.assertIsNotNone(refusal, "a refusal a batch cannot see is setup it writes every night")
                self.assertIn(words, str(refusal))
                with self.assertRaises(SdDbError) as raised:
                    self.prepare(values={"item": value}, expected_revision=revision)
                self.assertIn(words, str(raised.exception))
                self.assertEqual(self.snapshot(), before, "a standing refusal writes nothing")
            self.entries = copy.deepcopy(pristine); self.write()

    #: What in `_standing` can refuse: every helper it calls that raises.
    REFUSERS = {"restoration_pending", "catalog", "registered", "_shape", "_queues"}
    #: And every refusal it raises itself, one entry per `raise`.
    RAISED = ("restore recovery must finish before a new command request",
              "command catalog changed; reopen the palette",
              "is not a mutating worktree command, so it would not be queued")
    #: The words `_standing`'s own docstring must name each refusal by.
    NAMED = ("a restore still to finish", "a command palette that cannot be read",
             "a palette that changed since it was read", "does not register on this screen",
             "rejected from the catalog", "typed values that are not the ones its entry declares",
             "whose type or form is wrong for the kind", "an entry `prepare` would not queue")

    def test_the_docstring_names_every_refusal_the_set_can_make(self):
        """The membership and the prose that describes it, pinned to each other.

        The set is documented in four places, and a refusal added to or taken
        out of it goes on being described as it was: sd:814 added one and the
        prose in `local-sd-plan` still read as it did before. This is the one
        of the four a test can reach. It reads `_standing`'s own body and no
        further: it fails when that body gains or loses a helper that refuses
        or a `raise` of its own, and when the docstring drops the words for a
        refusal it still makes. A check added inside a helper it already
        calls -- another form check in `_shape`, say -- is not seen, and nor
        is a docstring naming a refusal the code cannot make. Following the
        helpers would pin two dozen catalog messages the prose rightly names
        only as a palette that cannot be read or an entry rejected from it.

        It also holds the criterion itself. "Without reading the item" stood
        for a year and is not the line between this set and the one
        `standing_refusal` leaves out: the four refusals in `_values` that are
        deliberately absent read no item row either, so it separates none of
        them from the eight here (sd:820).
        """
        tree = ast.parse(Path(runner_exec.__file__).read_text())
        body = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_standing")
        thrown = [node.exc for node in ast.walk(body) if isinstance(node, ast.Raise)]
        raised = set(thrown)
        refusers = {node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
                    for node in ast.walk(body) if isinstance(node, ast.Call) and node not in raised}
        self.assertEqual(refusers, self.REFUSERS, "a refusal was added to or taken out of `_standing`")
        messages = [ast.unparse(node) for node in thrown]
        self.assertEqual(len(messages), len(self.RAISED), "`_standing` raises a refusal this test does not name")
        for words in self.RAISED:
            self.assertTrue(any(words in message for message in messages), words)
        # Read as prose, not as lines: a rewrap is not a change of meaning,
        # and a test that a rewrap fails is one the next editor deletes.
        docs = {name: " ".join(getattr(runner_exec, name).__doc__.split())
                for name in ("_standing", "standing_refusal")}
        for words in self.NAMED:
            self.assertIn(words, docs["_standing"], "the docstring stopped naming a refusal it makes")
        for doc in docs.values():
            self.assertNotIn("without reading the item", doc,
                             "the criterion is what the set reads alike for every row, not the item row (sd:820)")

    def test_it_is_not_given_the_row_to_ask_about(self):
        """A stale revision or another row's id cannot reach the answer at all."""
        self.assertFalse({"item", "expected_revision", "target"} & set(inspect.signature(runner_exec.standing_refusal).parameters))

    def test_prepare_asks_this_and_keeps_no_second_copy_of_it(self):
        """Pinned by reading the source, because drift is the failure to fear.

        A refusal copied back into `prepare` passes every behaviour test above
        on the day it is written and rots on the day one copy is edited. One
        copy is what makes the enumeration answer for `prepare` rather than
        for a guess about it: `prepare` calls `_standing`, calls none of the
        functions `_standing` calls, and uses the catalog it was quoted only
        to hand it on.
        """
        tree = ast.parse(Path(runner_exec.__file__).read_text())
        body = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "prepare")
        called = {node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
                  for node in ast.walk(body) if isinstance(node, ast.Call)}
        self.assertIn("_standing", called)
        self.assertFalse({"restoration_pending", "catalog", "registered", "_shape"} & called)
        quoted = [node for node in ast.walk(body) if isinstance(node, ast.Name) and node.id == "expected_catalog"]
        self.assertEqual(len(quoted), 1, "the quoted catalog is read once, by _standing")
        # Whether the entry is queued is one condition too (sd:814): `_standing`
        # refuses on it for a caller that needs a queue, and `prepare` queues
        # on it. `prepare` asks `_queues` and reads `mutates` only to record it.
        self.assertIn("_queues", called)
        mutates = [node for node in ast.walk(body) if isinstance(node, ast.Subscript)
                   and isinstance(node.slice, ast.Constant) and node.slice.value == "mutates"]
        self.assertEqual(len(mutates), 1, "prepare reads mutates once, to record it; whether it queues is _queues")


if __name__ == "__main__": unittest.main()
