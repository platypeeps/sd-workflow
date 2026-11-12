"""Job actions use only a fixture runner; never invoke the live launch agent."""

import json
import plistlib
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, create_assignment, upsert_repo
from sd_db.errors import SdDbError
from sd_db.migrate import initialise
from sd_db.operations import (
    PREFIX,
    LaunchdBackend,
    assignment_state,
    cancel_assignment,
    cancel_job,
    inventory,
    job_state,
    retry_job,
)
from sd_db.testing.wire import hub_only
from sd_db.workflow import StaleItem, WorkflowError, capture_task
from sd_db.writes import record_state, transition, update_assignment


class Operations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agents = self.root / "LaunchAgents"
        self.cron = self.root / "local-cron-jobs"
        self.agents.mkdir()
        (self.cron / "jobs").mkdir(parents=True)
        (self.cron / "cron-jobs.sh").write_text("#!/bin/sh\n")
        self.job = "safe-job"
        self.label = PREFIX + self.job
        self.service = "gui/501/" + self.label
        self.plist = self.agents / (self.label + ".plist")
        self.argv = ["/bin/bash", str(self.cron / "cron-jobs.sh"), "exec", self.job]
        self.config = {"Label": self.label, "ProgramArguments": self.argv,
                       "StartCalendarInterval": [{"Hour": 9, "Minute": 10}]}
        self.write_config()
        (self.cron / "jobs" / (self.job + ".job")).write_text('JOB_PROMPT="SECRET"\n')
        self.state, self.pid, self.runs = "not running", None, 3
        self.last_exit: int | None = 1
        # launchd prints `last terminating signal = Terminated: 15` and no exit-code
        # line after a run that a signal ended: None on last_exit omits the line,
        # and last_signal is launchd's own string, copied from `launchctl print`.
        self.last_signal: str | None = None
        # The lifetime the counter belongs to: launchd's resource coalition id,
        # which moves when the label is bootstrapped again, and the boot it was
        # counted in. None on lifetime omits the block, as launchd does for a
        # label bootstrapped and not yet spawned.
        self.lifetime, self.boot = 85948, 1789568182
        self.loaded = True
        self.calls = []
        self.action_error = None
        self.inspect_hook = None
        self.backend = LaunchdBackend(launch_agents=self.agents, cron_root=self.cron,
                                     jobs_dirs=[self.cron / "jobs"], uid=501, runner=self.run_launchctl)
        self.backend.boot = lambda: self.boot
        initialise(self.root / "sd.db")
        self.db = connect(self.root / "sd.db")
        self.addCleanup(self.db.close)

    def write_config(self):
        self.plist.write_bytes(plistlib.dumps(self.config))

    def run_launchctl(self, argv):
        self.calls.append(argv)
        if argv[1] == "print":
            if self.inspect_hook:
                self.inspect_hook()
            if not self.loaded:
                return subprocess.CompletedProcess(argv, 113, "", "not loaded SECRET")
            fields = [self.service + " = {", "\tpath = " + str(self.plist),
                      "\tprogram = /bin/bash", "\tstate = " + self.state,
                      "\truns = " + str(self.runs)]
            if self.last_exit is not None:
                fields.append("\tlast exit code = " + str(self.last_exit))
            if self.last_signal is not None:
                fields.append("\tlast terminating signal = " + self.last_signal)
            fields.append("\targuments = {")
            fields.extend("\t\t" + arg for arg in self.argv)
            fields += ["\t}", "\tenvironment = {", "\t\tSECRET = hide-me", "\t}"]
            if self.pid:
                fields.append("\tpid = " + str(self.pid))
            if self.lifetime is not None:
                fields += ["\tresource coalition = {", "\t\tID = " + str(self.lifetime), "\t\ttype = resource",
                           "\t\tname = " + self.label, "\t}",
                           "\tjetsam coalition = {", "\t\tID = " + str(self.lifetime + 1), "\t\ttype = jetsam", "\t}"]
            return subprocess.CompletedProcess(argv, 0, "\n".join(fields + ["}"]), "")
        if self.action_error:
            if isinstance(self.action_error, Exception):
                raise self.action_error
            return subprocess.CompletedProcess(argv, self.action_error, "SECRET", "SECRET")
        return subprocess.CompletedProcess(argv, 0, "SECRET", "")

    def snapshot(self):
        return job_state(self.db, self.job, backend=self.backend)

    def actions(self):
        return [call for call in self.calls if call[1] != "print"]

    def test_inventory_exposes_only_safe_fields_and_observes_without_writes(self):
        before = self.db.total_changes
        data = inventory(self.db, backend=self.backend)
        self.assertEqual(self.db.total_changes, before)
        self.assertEqual(len(data["jobs"]), 1)
        job = data["jobs"][0]
        self.assertEqual((job["state"], job["last_exit"], job["schedule"]),
                         ("failed", 1, [{"Hour": 9, "Minute": 10}]))
        self.assertTrue(job["capabilities"]["retry"]["allowed"])
        self.assertEqual(job["capabilities"]["retry"]["label"], "Retry")
        self.assertFalse(job["capabilities"]["cancel"]["allowed"])
        self.assertNotIn("SECRET", json.dumps(data))
        self.assertNotIn("hide-me", json.dumps(data))
        self.assertEqual(self.actions(), [])

    def test_job_launch_wrapped_plist_lists_and_reads(self):
        # sd:6679. system #538 starts every cron plist through job-launch;
        # the bare runner argv alone listed no job at all.
        self.argv = [str(Path.home() / ".local/libexec/system-tools/job-launch")] + self.argv
        self.config["ProgramArguments"] = self.argv
        self.write_config()
        original = self.run_launchctl
        def wrapped(argv):
            result = original(argv)
            result.stdout = result.stdout.replace("\tprogram = /bin/bash", "\tprogram = " + self.argv[0])
            return result
        self.backend.runner = wrapped
        self.assertEqual(self.backend.names(), [self.job])
        job = inventory(self.db, backend=self.backend)["jobs"][0]
        self.assertEqual((job["name"], job["state"]), (self.job, "failed"))
        self.config["ProgramArguments"] = ["/tmp/job-launch"] + self.argv[1:]
        self.write_config()
        self.assertEqual(self.backend.names(), [])

    def test_foreign_program_missing_job_symlink_and_unsafe_names_never_dispatch(self):
        self.config["ProgramArguments"] = ["/bin/bash", "-c", "SECRET"]
        self.write_config()
        with self.assertRaisesRegex(WorkflowError, "supported"):
            self.snapshot()
        self.assertEqual(inventory(self.db, backend=self.backend)["jobs"], [])
        self.config["ProgramArguments"] = self.argv
        self.write_config()
        (self.cron / "jobs" / (self.job + ".job")).unlink()
        with self.assertRaises(WorkflowError):
            self.snapshot()
        for name in ("../safe-job", "safe-job;echo", "", "-k", "a/b"):
            with self.assertRaises(WorkflowError):
                job_state(self.db, name, backend=self.backend)
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_loaded_program_must_match_installed_program_and_unknown_output_blocks(self):
        original = self.run_launchctl
        def changed(argv):
            result = original(argv)
            result.stdout = result.stdout.replace("\tprogram = /bin/bash", "\tprogram = /bin/zsh")
            return result
        self.backend.runner = changed
        state = self.snapshot()
        self.assertEqual(state["state"], "unknown")
        self.assertFalse(state["capabilities"]["retry"]["allowed"])
        with self.assertRaises(WorkflowError):
            retry_job(self.db, self.job, expected_revision=state["revision"], backend=self.backend, who="operator")
        self.assertEqual(self.actions(), [])

    def test_symlink_and_duplicate_runtime_fields_fail_closed(self):
        path = self.cron / "jobs" / (self.job + ".job")
        target = self.root / "outside.job"
        target.write_text("secret")
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(WorkflowError):
            self.snapshot()
        path.unlink()
        path.write_text("safe")
        original = self.run_launchctl
        def duplicate(argv):
            result = original(argv)
            result.stdout = result.stdout.replace("\tstate = not running", "\tstate = not running\n\tstate = running")
            return result
        self.backend.runner = duplicate
        before = self.snapshot()
        self.assertEqual(before["state"], "unknown")
        self.assertFalse(before["capabilities"]["cancel"]["allowed"])
        self.assertFalse(before["capabilities"]["retry"]["allowed"])
        self.assertEqual(self.actions(), [])

    def test_inventory_inspects_jobs_in_parallel_in_order_and_isolates_failures(self):
        # sd:5005. Every print waits on one barrier, so a serial inventory breaks it
        # at its timeout rather than hanging. `broken` fails its read and `gone`
        # vanishes; neither may change another job's row or the order.
        names = ["alpha", "broken", "gone", "safe-job", "zulu"]
        for name in names:
            config = dict(self.config, Label=PREFIX + name,
                          ProgramArguments=["/bin/bash", str(self.cron / "cron-jobs.sh"), "exec", name])
            (self.agents / (PREFIX + name + ".plist")).write_bytes(plistlib.dumps(config))
            (self.cron / "jobs" / (name + ".job")).write_text("safe")
        barrier = threading.Barrier(len(names), timeout=2)
        original = self.run_launchctl

        def overlapping(argv):
            name = argv[2].removeprefix("gui/501/" + PREFIX)
            barrier.wait()
            if name == "broken":
                raise OSError("launchctl could not run")
            if name == "gone":
                raise WorkflowError("job is not supported: installed configuration is unavailable or invalid")
            result = original(argv)
            result.stdout = result.stdout.replace(self.job, name)
            return result
        self.backend.runner = overlapping
        jobs = inventory(self.db, backend=self.backend)["jobs"]
        self.assertEqual([(job["name"], job["state"]) for job in jobs],
                         [("alpha", "failed"), ("broken", "unknown"), ("safe-job", "failed"), ("zulu", "failed")])

    def test_malformed_xml_plist_is_not_an_inventory_failure(self):
        self.plist.write_bytes(b'<?xml version="1.0"?><plist><dict><key>broken')
        self.assertEqual(inventory(self.db, backend=self.backend)["jobs"], [])
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_retry_records_request_without_claiming_running_and_prevents_replay(self):
        before = self.snapshot()
        result = retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(self.actions(), [["/bin/launchctl", "kickstart", self.service]])
        self.assertEqual(result["request"]["status"], "accepted")
        self.assertEqual(result["job"]["state"], "failed")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertFalse(result["job"]["capabilities"]["retry"]["allowed"])
        with self.assertRaises(StaleItem):
            retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(len(self.actions()), 1)
        self.runs += 1
        self.assertTrue(self.snapshot()["capabilities"]["retry"]["allowed"])

    @hub_only
    def test_cancel_running_service_uses_sigterm_never_pid_and_keeps_observation_honest(self):
        self.state, self.pid = "running", 333
        before = self.snapshot()
        result = cancel_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(self.actions(), [["/bin/launchctl", "kill", "SIGTERM", self.service]])
        self.assertEqual((result["request"]["status"], result["job"]["state"]),
                         ("accepted", "running"))

    @hub_only
    def test_changed_revision_rejects_same_second_runtime_and_job_file_changes(self):
        before = self.snapshot()
        self.runs += 1
        with self.assertRaises(StaleItem):
            retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        before = self.snapshot()
        (self.cron / "jobs" / (self.job + ".job")).write_text("changed")
        with self.assertRaises(StaleItem):
            retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_fresh_recheck_prevents_dispatch_after_durable_request(self):
        before = self.snapshot()
        count = 0
        def race():
            nonlocal count
            count += 1
            if count == 2:
                self.state, self.pid = "running", 444
        self.inspect_hook = race
        result = retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(result["request"]["status"], "failed")
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_process_exit_at_dispatch_leaves_durable_unknown_request(self):
        before = self.snapshot()
        with patch.object(self.backend, "perform", side_effect=SystemExit(86)):
            with self.assertRaises(SystemExit):
                retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        observed = self.snapshot()
        self.assertEqual(observed["last_request"]["status"], "requested")
        self.assertIn("not yet known", observed["last_request"]["message"])
        self.assertFalse(observed["capabilities"]["retry"]["allowed"])
        self.assertEqual(observed["state"], "failed")

    @hub_only
    def test_restore_appearing_at_final_check_prevents_dispatch(self):
        before = self.snapshot()
        count = 0
        def race():
            nonlocal count
            count += 1
            if count == 2:
                record_state(self.db, "restore", key="concurrent-restore", body={})
        self.inspect_hook = race
        result = retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(result["request"]["status"], "failed")
        self.assertIn("restore", result["request"]["message"])
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_failed_and_timed_out_commands_record_failure_or_unknown_without_secret(self):
        for error, expected in ((5, "failed"), (subprocess.TimeoutExpired("launchctl", 5), "unknown")):
            self.action_error = error
            self.runs += 1
            before = self.snapshot()
            result = retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
            self.assertEqual(result["request"]["status"], expected)
            self.assertNotIn("SECRET", json.dumps(result))

    @hub_only
    def test_restore_blocks_retry_but_allows_cancel_and_read(self):
        record_state(self.db, "restore", key="test", body={})
        before = self.snapshot()
        self.assertIn("restore", before["capabilities"]["retry"]["reason"])
        with self.assertRaisesRegex(WorkflowError, "restore"):
            retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.state, self.pid = "running", 333
        before = self.snapshot()
        result = cancel_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(result["request"]["status"], "accepted")

    @hub_only
    def test_unloaded_and_missing_revision_refuse_actions(self):
        self.loaded = False
        before = self.snapshot()
        self.assertEqual(before["capabilities"]["retry"], {"allowed": False, "label": "Run now",
                                                           "reason": "job is installed but not loaded"})
        with self.assertRaisesRegex(WorkflowError, "not loaded"):
            retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.loaded = True
        for revision in (None, "", 1):
            with self.assertRaises(WorkflowError):
                retry_job(self.db, self.job, expected_revision=revision, backend=self.backend, who="operator")
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_idle_job_runs_now_and_a_stale_revision_still_refuses(self):
        """sd:4965. A job with no calendar (JOB_SCHEDULE="manual") is idle
        until someone starts it, so the same action that retries a failed run
        starts an idle one, labelled for what it does. Its revision check is
        the retry's: a reading from before a run refuses."""
        del self.config["StartCalendarInterval"]
        self.write_config()
        for runs, code in ((0, None), (3, 0)):  # never ran, and ran cleanly
            with self.subTest(runs=runs):
                self.runs, self.last_exit = runs, code
                state = self.snapshot()
                self.assertEqual((state["state"], state["schedule"]), ("idle", []))
                self.assertEqual(state["capabilities"]["retry"], {"allowed": True, "reason": "", "label": "Run now"})
        stale = self.snapshot()
        self.runs += 1
        with self.assertRaises(StaleItem):
            retry_job(self.db, self.job, expected_revision=stale["revision"], backend=self.backend, who="operator")
        self.assertEqual(self.actions(), [])
        before = self.snapshot()
        result = retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(self.actions(), [["/bin/launchctl", "kickstart", self.service]])
        self.assertEqual((result["request"]["action"], result["request"]["status"]), ("retry", "accepted"))
        with self.assertRaises(StaleItem):
            retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(len(self.actions()), 1)

    @hub_only
    def test_a_job_gone_after_dispatch_keeps_the_label_on_its_answer(self):
        before = self.snapshot()
        def remove(action, service):
            (self.cron / "jobs" / (self.job + ".job")).unlink()
            return subprocess.CompletedProcess([], 0, "", "")
        with patch.object(self.backend, "perform", side_effect=remove):
            result = retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(result["job"]["capabilities"]["retry"],
                         {"allowed": False, "reason": "job is no longer supported", "label": "Run now"})

    @hub_only
    def test_running_job_refuses_run_now_with_its_reason(self):
        self.state, self.pid = "running", 333
        state = self.snapshot()
        self.assertEqual(state["capabilities"]["retry"],
                         {"allowed": False, "reason": "the job is already running", "label": "Run now"})
        with self.assertRaisesRegex(WorkflowError, "^the job is already running$"):
            retry_job(self.db, self.job, expected_revision=state["revision"], backend=self.backend, who="operator")
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_failed_and_interrupted_jobs_keep_retry_and_its_label(self):
        for code, signal, state in ((1, None, "failed"), (None, "Killed: 9", "interrupted")):
            with self.subTest(state=state):
                self.last_exit, self.last_signal = code, signal
                self.runs += 1
                before = self.snapshot()
                self.assertEqual(before["state"], state)
                self.assertEqual(before["capabilities"]["retry"], {"allowed": True, "reason": "", "label": "Retry"})
                result = retry_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend,
                                   who="operator")
                self.assertEqual(result["request"]["status"], "accepted")
        self.assertEqual(self.actions(), [["/bin/launchctl", "kickstart", self.service]] * 2)

    def test_a_job_names_its_last_run(self):
        """`run` is launchd's run counter with the lifetime and boot it was
        counted in, so a reader can tell one run from the next (sd:2904)."""
        self.assertEqual(self.snapshot()["run"], [3, 85948, 1789568182])
        self.runs += 1
        self.assertEqual(self.snapshot()["run"], [4, 85948, 1789568182])

    def test_signal_ended_run_without_a_cancel_is_interrupted_not_unknown(self):
        """launchd prints `last terminating signal = Terminated: 15` and no
        `last exit code` line after SIGTERM. That is a recorded outcome, not
        an unreadable one, but with no cancel of this run recorded here it is
        a stop nobody accounted for: the job reads interrupted with the signal
        public next to last_exit and its retry open, and a running pid with
        the same history reads running (sd:1344)."""
        self.last_exit, self.last_signal = None, "Terminated: 15"
        state = self.snapshot()
        self.assertEqual((state["state"], state["pid"], state["last_exit"], state["last_signal"]),
                         ("interrupted", None, None, 15))
        self.assertTrue(state["capabilities"]["retry"]["allowed"])
        self.assertEqual(state["capabilities"]["cancel"]["reason"], "only a currently running job can cancel")
        self.assertEqual(self.backend.inspect(self.job)["reason"], "last run ended in SIGTERM (15)")
        self.state, self.pid = "running", 333
        state = self.snapshot()
        self.assertEqual((state["state"], state["pid"], state["last_signal"]), ("running", 333, 15))
        self.assertTrue(state["capabilities"]["cancel"]["allowed"])
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_sigkill_without_cancel_evidence_stays_interrupted_and_retryable(self):
        """Vacuity: on 11d96185 this read idle and left the attention list. No
        signal number establishes a meant stop: `launchctl kill SIGTERM` is
        what cancel sends and escalates to nothing, so SIGKILL is never ours,
        and a retry accepted earlier is a request, not cancel evidence."""
        self.last_exit, self.last_signal = None, "Killed: 9"
        state = self.snapshot()
        self.assertEqual((state["state"], state["last_exit"], state["last_signal"]), ("interrupted", None, 9))
        self.assertTrue(state["capabilities"]["retry"]["allowed"])
        result = retry_job(self.db, self.job, expected_revision=state["revision"], backend=self.backend, who="operator")
        self.assertEqual((result["request"]["status"], result["job"]["state"]), ("accepted", "interrupted"))
        self.assertEqual(self.actions(), [["/bin/launchctl", "kickstart", self.service]])
        self.state, self.pid = "running", 333
        before = self.snapshot()
        cancel_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.state, self.pid = "not running", None
        self.assertEqual(self.snapshot()["state"], "interrupted")

    @hub_only
    def test_own_accepted_cancel_reads_the_sigterm_it_sent_as_idle(self):
        """The one stop with evidence it was meant: a cancel this module sent,
        launchctl accepted, and launchd then recorded as SIGTERM on that same
        run. A later run, a refused cancel, or a request from before `runs`
        was recorded on it is no evidence, and the job stays interrupted."""
        self.state, self.pid = "running", 333
        before = self.snapshot()
        result = cancel_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(result["request"]["status"], "accepted")
        self.assertNotIn("runs", result["request"])
        self.state, self.pid, self.last_exit, self.last_signal = "not running", None, None, "Terminated: 15"
        state = self.snapshot()
        self.assertEqual((state["state"], state["last_exit"], state["last_signal"]), ("idle", None, 15))
        self.assertEqual(state["capabilities"]["retry"], {"allowed": True, "reason": "", "label": "Run now"})
        self.runs += 1
        self.assertEqual(self.snapshot()["state"], "interrupted")
        self.runs -= 1
        self.assertEqual(self.snapshot()["state"], "idle")
        record_state(self.db, "checkpoint", key="operations:job:" + self.job,
                     body={"id": "old", "action": "cancel", "status": "accepted", "at": "2026-09-01T00:00:00Z",
                           "who": "operator", "before": "stale", "message": "accepted"})
        self.assertEqual(self.snapshot()["state"], "interrupted")
        self.state, self.pid, self.action_error = "running", 333, 5
        before = self.snapshot()
        result = cancel_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(result["request"]["status"], "failed")
        self.state, self.pid = "not running", None
        self.assertEqual(self.snapshot()["state"], "interrupted")

    @hub_only
    def test_crash_signal_reads_failed_with_its_name_and_opens_retry(self):
        """A job whose last run died of SIGSEGV is failed and retryable. The
        observed reason names the signal, and that reason is a description,
        not the sentence that refuses the other action (sd:1344)."""
        self.last_exit, self.last_signal = None, "Segmentation fault: 11"
        observed = self.backend.inspect(self.job)
        self.assertEqual((observed["state"], observed["last_exit"], observed["last_signal"], observed["reason"]),
                         ("failed", None, 11, "last run ended in SIGSEGV (11)"))
        state = self.snapshot()
        self.assertEqual((state["state"], state["last_signal"]), ("failed", 11))
        self.assertTrue(state["capabilities"]["retry"]["allowed"])
        self.assertEqual(state["capabilities"]["cancel"]["reason"], "only a currently running job can cancel")
        result = retry_job(self.db, self.job, expected_revision=state["revision"], backend=self.backend, who="operator")
        self.assertEqual(self.actions(), [["/bin/launchctl", "kickstart", self.service]])
        self.assertEqual(result["request"]["status"], "accepted")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_runs_without_exit_code_or_signal_is_a_deliberate_unknown(self):
        """No loaded job on a real machine prints runs > 0 with neither line;
        if one does, the state is unknown on purpose and the reason says what
        was missing, instead of a KeyError swallowed into the generic reason."""
        self.last_exit = None
        observed = self.backend.inspect(self.job)
        self.assertEqual((observed["state"], observed["last_exit"], observed["last_signal"]), ("unknown", None, None))
        self.assertEqual(observed["reason"], "launchctl reports 3 runs with neither a last exit code nor a terminating signal")
        state = self.snapshot()
        self.assertEqual(state["capabilities"]["retry"]["reason"], observed["reason"])
        self.assertFalse(state["capabilities"]["cancel"]["allowed"])
        self.assertEqual(self.actions(), [])

    def test_exit_code_and_signal_lines_decide_state_and_never_raise(self):
        """Every branch of the outcome decision, one fixture each, so the signal
        branch is proven not to move the exit-code ones. The signal set is the
        one services.py reads, so the two parsers cannot drift apart."""
        cases = (
            (("not running", None, "(never exited)", 0, None), ("idle", None, None)),  # bootstrapped, never spawned
            (("running", 111, "(never exited)", 1, None), ("running", None, None)),
            (("not running", None, 0, 3, None), ("idle", 0, None)),
            (("not running", None, 1, 2, None), ("failed", 1, None)),
            (("running", 111, 1, 2, None), ("running", 1, None)),
            (("not running", None, None, 0, None), ("idle", None, None)),              # no line at all, never ran
            (("running", 111, None, 6, "Terminated: 15"), ("running", None, 15)),     # stopped, then running again
            (("not running", None, None, 6, "Terminated: 15"), ("interrupted", None, 15)),  # no cancel recorded here
            (("not running", None, None, 2, "Killed: 9"), ("interrupted", None, 9)),  # never ours: cancel sends SIGTERM
            (("not running", None, None, 2, "Hangup: 1"), ("interrupted", None, 1)),
            (("not running", None, None, 3, "Segmentation fault: 11"), ("failed", None, 11)),  # the kernel's verdict
            (("not running", None, None, 3, "Abort trap: 6"), ("failed", None, 6)),
            (("running", 111, None, 3, "Segmentation fault: 11"), ("running", None, 11)),  # running wins over history
            (("not running", None, None, 2, "Cputime limit exceeded: 24"), ("failed", None, 24)),  # a resource limit
            (("not running", None, None, 2, "Filesize limit exceeded: 25"), ("failed", None, 25)),
            (("not running", None, 0, 2, "Terminated: 15"), ("idle", 0, 15)),         # both lines: code stays the verdict
            (("not running", None, "junk", 2, None), ("unknown", None, None)),
            (("not running", None, None, 6, "garbage"), ("unknown", None, None)),
            (("not running", None, None, 6, None), ("unknown", None, None)),
        )
        for runtime, expected in cases:
            with self.subTest(runtime=runtime):
                self.state, self.pid, self.last_exit, self.runs, self.last_signal = runtime
                state = self.snapshot()
                self.assertEqual((state["state"], state["last_exit"], state["last_signal"]), expected)
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_cancel_evidence_needs_the_lifetime_and_boot_the_counter_was_counted_in(self):
        """Review of 10076749: `runs` restarts at 1 whenever the label is
        bootstrapped again, so cancel run 1, reload, then an external SIGTERM
        on the new run 1 matched the old cancel and read idle. The resource
        coalition id moves exactly when the counter restarts and is boot-local,
        so the request carries id and boot beside the counter; a run from a
        later lifetime or boot, or an identity launchd could not supply, reads
        interrupted. Vacuity on 10076749: the reload read ('idle', False)."""
        self.runs, self.state, self.pid = 1, "running", 333
        before = self.snapshot()
        result = cancel_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
        self.assertEqual(result["request"]["status"], "accepted")
        self.assertFalse({"runs", "lifetime", "boot"} & set(result["request"]))
        self.state, self.pid, self.last_exit, self.last_signal = "not running", None, None, "Terminated: 15"
        self.assertEqual(self.snapshot()["state"], "idle")
        self.lifetime += 2  # bootout and bootstrap: a new coalition, and runs = 1 again
        state = self.snapshot()
        self.assertEqual((state["state"], state["capabilities"]["retry"]["allowed"]), ("interrupted", True))
        self.lifetime -= 2
        self.boot += 3600  # a reboot: the boot-local id can repeat, the boot cannot
        self.assertEqual(self.snapshot()["state"], "interrupted")
        self.boot -= 3600
        self.assertEqual(self.snapshot()["state"], "idle")
        for missing in ("lifetime", "boot"):
            with self.subTest(missing=missing):
                kept = getattr(self, missing)
                setattr(self, missing, None)
                self.state, self.pid, self.last_exit, self.last_signal = "running", 333, "(never exited)", None
                before = self.snapshot()
                cancel_job(self.db, self.job, expected_revision=before["revision"], backend=self.backend, who="operator")
                setattr(self, missing, kept)
                self.state, self.pid, self.last_exit, self.last_signal = "not running", None, None, "Terminated: 15"
                self.assertEqual(self.snapshot()["state"], "interrupted")
        self.assertEqual(self.actions(), [["/bin/launchctl", "kill", "SIGTERM", self.service]] * 3)

    def test_assignment_cancel_is_queued_only_atomic_and_does_not_complete_item(self):
        item = capture_task(self.db, title="Task", who="operator")["item"]["id"]
        aid = create_assignment(self.db, role="author", status="queued", item=item)
        before = assignment_state(self.db, aid)
        result = cancel_assignment(self.db, aid, expected_revision=before["revision"], who="operator")
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["ended"])
        self.assertEqual(self.db.execute("SELECT status FROM item WHERE id=?", (item,)).fetchone()[0], "planning")
        self.assertEqual(self.db.execute("SELECT count(*) FROM note WHERE item=? AND kind='comment'", (item,)).fetchone()[0], 1)
        with self.assertRaises(StaleItem):
            cancel_assignment(self.db, aid, expected_revision=before["revision"], who="operator")

    def test_assignment_stale_running_missing_and_rollback_do_not_claim_cancel(self):
        aid = create_assignment(self.db, role="author", status="queued")
        before = assignment_state(self.db, aid)
        update_assignment(self.db, aid, status="running", result="SECRET")
        with self.assertRaises(StaleItem):
            cancel_assignment(self.db, aid, expected_revision=before["revision"], who="operator")
        current = assignment_state(self.db, aid)
        self.assertIn("backend", current["capabilities"]["cancel"]["reason"])
        self.assertNotIn("SECRET", json.dumps(current))
        with self.assertRaises(WorkflowError):
            cancel_assignment(self.db, aid, expected_revision=current["revision"], who="operator")
        with self.assertRaisesRegex(WorkflowError, "^no assignment 999$"):
            assignment_state(self.db, 999)
        update_assignment(self.db, aid, status="queued")
        before = assignment_state(self.db, aid)
        with patch("sd_db.operations.record_state", side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                cancel_assignment(self.db, aid, expected_revision=before["revision"], who="operator")
        self.assertEqual(assignment_state(self.db, aid), before)

    def test_a_blocked_assignment_is_cancellable_once_no_run_holds_it(self):
        """sd:2082: a blocked assignment stayed on the board after its item
        shipped, with no way to clear it. pack sd:8606: one whose item is
        still open refused a cancel too, so each requeue left a dead row; a
        requeue makes a new row, so nothing settles the old one. Cancelled
        is terminal and never dispatches, so clearing it is safe once no
        runner attempt, its own or its bundle lead's, holds its lease."""
        item = capture_task(self.db, title="Task", who="operator")["item"]["id"]
        open_item = capture_task(self.db, title="Open", who="operator")["item"]["id"]
        dead = create_assignment(self.db, role="author", status="queued", item=open_item)
        update_assignment(self.db, dead, status="blocked")
        state = assignment_state(self.db, dead)
        self.assertTrue(state["capabilities"]["cancel"]["allowed"], state["capabilities"]["cancel"]["reason"])
        self.assertEqual(cancel_assignment(self.db, dead, expected_revision=state["revision"], who="operator")["status"],
                         "cancelled")
        self.assertEqual(self.db.execute("SELECT status FROM item WHERE id=?", (open_item,)).fetchone()[0], "planning")
        aid = create_assignment(self.db, role="author", status="queued", item=item)
        update_assignment(self.db, aid, status="blocked")
        blocked = assignment_state(self.db, aid)
        # A runner attempt that has not released its lease still owns the row,
        # so it stays blocked (sd:6996 cancels only an unheld one); a bundle
        # member's lease is its lead's run.
        upsert_repo(self.db, "/fixture/repo")
        member = create_assignment(self.db, role="author", status="queued", item=open_item)
        self.db.execute("UPDATE assignment SET parent = ?, status = 'blocked' WHERE id = ?", (aid, member))
        self.db.execute(
            "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, created_at, updated_at)"
            " VALUES ('r0', ?, 1, '/fixture/repo', 'b', 'o', '/w', '/r', 'now', 'now')", (aid,))
        held = assignment_state(self.db, member)
        self.assertFalse(held["capabilities"]["cancel"]["allowed"])
        self.assertIn("lease", held["capabilities"]["cancel"]["reason"])
        self.db.execute("DELETE FROM runner_run WHERE id = 'r0'")
        self.db.execute(
            "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, created_at, updated_at)"
            " VALUES ('r1', ?, 1, '/fixture/repo', 'b', 'o', '/w', '/r', 'now', 'now')", (aid,))
        transition(self.db, item, "done", who="operator")
        held = assignment_state(self.db, aid)
        self.assertNotEqual(held["revision"], blocked["revision"])
        self.assertFalse(held["capabilities"]["cancel"]["allowed"])
        self.assertIn("lease", held["capabilities"]["cancel"]["reason"])
        self.db.execute("UPDATE runner_run SET released_at = 'now' WHERE id = 'r1'")
        done = assignment_state(self.db, aid)
        self.assertTrue(done["capabilities"]["cancel"]["allowed"], done["capabilities"]["cancel"]["reason"])
        result = cancel_assignment(self.db, aid, expected_revision=done["revision"], who="operator")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.db.execute("SELECT status FROM item WHERE id=?", (item,)).fetchone()[0], "done")
        audit = json.loads(self.db.execute("SELECT body FROM state WHERE key=?", (f"operations:assignment:{aid}",)).fetchone()[0])
        self.assertEqual(audit["old_status"], "blocked")
        notes = [row[0] for row in self.db.execute("SELECT body FROM note WHERE item=? AND kind='comment'", (item,))]
        self.assertEqual(notes, [f"Blocked assignment {aid} cancelled by operator; item status unchanged"])

    def test_cancelled_assignment_cannot_be_claimed_or_requeued_by_stale_worker(self):
        aid = create_assignment(self.db, role="author", status="queued")
        queued = assignment_state(self.db, aid)
        cancelled = cancel_assignment(self.db, aid, expected_revision=queued["revision"], who="operator")
        for target in ("running", "queued", "done"):
            with self.assertRaisesRegex(SdDbError, "cancelled"):
                update_assignment(self.db, aid, status=target)
            self.assertEqual(assignment_state(self.db, aid), cancelled)

    def test_only_one_queued_assignment_claim_succeeds(self):
        aid = create_assignment(self.db, role="author", status="queued")
        update_assignment(self.db, aid, status="running")
        claimed = assignment_state(self.db, aid)
        with self.assertRaisesRegex(SdDbError, "not queued"):
            update_assignment(self.db, aid, status="running")
        self.assertEqual(assignment_state(self.db, aid), claimed)


class JobDirectories(unittest.TestCase):
    """The job files are private config, found where cron-jobs.sh finds them."""

    def test_extra_dirs_come_first_then_the_host_then_the_shared_dir(self):
        from sd_db.operations import job_dirs
        found = job_dirs({"SYSTEM_TOOLS_CONFIG": "/cfg", "CRON_JOBS_EXTRA_DIRS": "/a::/b",
                          "CRON_JOBS_HOST": "Mini"})
        self.assertEqual(found, [Path("/a"), Path("/b"), Path("/cfg/cron-jobs/jobs/mini"),
                                 Path("/cfg/cron-jobs/jobs")])

    def test_the_default_is_under_xdg_config_home(self):
        from sd_db.operations import job_dirs
        self.assertEqual(job_dirs({"HOME": "/h", "CRON_JOBS_HOST": "mini"}),
                         [Path("/h/.config/system/cron-jobs/jobs/mini"), Path("/h/.config/system/cron-jobs/jobs")])

    def test_the_host_defaults_to_the_short_host_name(self):
        from sd_db.operations import job_dirs
        with patch("socket.gethostname", return_value="Mini.local"):
            found = job_dirs({"SYSTEM_TOOLS_CONFIG": "/cfg"})
        self.assertEqual(found, [Path("/cfg/cron-jobs/jobs/mini"), Path("/cfg/cron-jobs/jobs")])

    def test_env_file_supplies_the_host_and_an_exported_value_wins(self):
        from sd_db.operations import job_dirs
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / "cron-jobs"; conf.mkdir()
            (conf / ".env").write_text('CRON_JOBS_HOST="mini"\nCRON_JOBS_EXTRA_DIRS=/x\n')
            env = {"SYSTEM_TOOLS_CONFIG": directory}
            self.assertEqual(job_dirs(env), [Path("/x"), conf / "jobs/mini", conf / "jobs"])
            self.assertEqual(job_dirs({**env, "CRON_JOBS_HOST": "studio"})[1], conf / "jobs/studio")

    def test_an_exported_empty_value_beats_the_file(self):
        """sd:3970. `st_source_env` keeps an exported empty value; the file's
        host and extra dirs must not come back through `or`."""
        from sd_db.operations import job_dirs
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / "cron-jobs"; conf.mkdir()
            (conf / ".env").write_text('CRON_JOBS_HOST="mini"\nCRON_JOBS_EXTRA_DIRS=/x\n')
            env = {"SYSTEM_TOOLS_CONFIG": directory, "CRON_JOBS_HOST": "", "CRON_JOBS_EXTRA_DIRS": ""}
            with patch("socket.gethostname", return_value="Studio.local"):
                self.assertEqual(job_dirs(env), [conf / "jobs/studio", conf / "jobs"])

    def test_backend_reads_a_host_job_before_the_shared_one(self):
        from sd_db.operations import job_dirs
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory) / "cron-jobs/jobs"; (jobs / "mini").mkdir(parents=True)
            (jobs / "studio").mkdir()
            for folder in (jobs, jobs / "mini", jobs / "studio"):
                (folder / "demo.job").write_text("x\n")
            (jobs / "studio" / "other.job").write_text("x\n")
            dirs = job_dirs({"SYSTEM_TOOLS_CONFIG": directory, "CRON_JOBS_HOST": "mini"})
            first = next(d for d in dirs if (d / "demo.job").exists())
            self.assertEqual(first, jobs / "mini")
            self.assertFalse(any((d / "other.job").exists() for d in dirs))


if __name__ == "__main__":
    unittest.main()
