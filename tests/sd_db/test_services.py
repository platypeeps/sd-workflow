"""User service controls with real plist parsing and a fake launchctl runner."""

import json
import os
import plistlib
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import connect
from sd_db.migrate import initialise
from sd_db.operations import LABEL_PREFIX, PREFIX
from sd_db.services import (
    ServiceBackend,
    inventory,
    restart_service,
    service_state,
    start_service,
    stop_service,
)
from sd_db.testing.wire import hub_only
from sd_db.workflow import StaleItem, WorkflowError
from sd_db.writes import record_state


class Services(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.agents, self.daemons = self.root / "LaunchAgents", self.root / "LaunchDaemons"
        self.agents.mkdir()
        self.daemons.mkdir()
        self.uid = os.getuid()
        self.domain = f"gui/{self.uid}"
        self.label = "com.example.server"
        self.program = self.root / "server"
        self.program.write_text("#!/bin/sh\n")
        self.program.chmod(0o755)
        self.config = {"Label": self.label, "ProgramArguments": [str(self.program), "--secret", "HIDDEN"],
                       "RunAtLoad": True, "KeepAlive": True}
        self.plist = self.agents / (self.label + ".plist")
        self.plist.write_bytes(plistlib.dumps(self.config))
        self.configs = {self.domain + "/" + self.label: self.config}
        self.runtime = {self.domain + "/" + self.label: ["running", 111, 0, 2]}
        self.calls = []
        self.hook = None
        self.disabled = False
        self.command_error = None
        self.backend = ServiceBackend(user_agents=self.agents, system_daemons=self.daemons,
                                      uid=self.uid, runner=self.run_command)
        initialise(self.root / "sd.db")
        self.db = connect(self.root / "sd.db")
        self.addCleanup(self.db.close)

    def run_command(self, argv):
        self.calls.append(argv)
        if self.hook:
            self.hook(argv)
        if argv[1] == "print-disabled":
            value = "disabled" if self.disabled else "enabled"
            return subprocess.CompletedProcess(argv, 0, f'\n\tdisabled services = {{\n\t\t"{self.label}" => {value}\n\t}}\n', "")
        if argv[1] == "print":
            service = argv[2]
            if service not in self.runtime:
                return subprocess.CompletedProcess(argv, 113, "", "SECRET diagnostic")
            # [state, pid, code, runs] or [state, pid, code, runs, signal]. A code of
            # None omits the exit-code line, which is what launchd prints after a
            # run that ended in a signal; signal is launchd's own string, e.g.
            # "Terminated: 15", copied from `launchctl print` on a SIGTERM'd agent.
            state, pid, code, runs, *rest = self.runtime[service]
            signal = rest[0] if rest else None
            config = self.configs[service]
            base = self.daemons if service.startswith("system/") else self.agents
            label = service.rsplit("/", 1)[-1]
            arguments = config.get("ProgramArguments")
            program = config.get("Program") or arguments[0]
            lines = [service + " = {", "\tpath = " + str(base / (label + ".plist")),
                     "\tprogram = " + program, "\tstate = " + state, "\truns = " + str(runs)]
            if code is not None:
                lines += ["\tlast exit code = " + str(code)]
            if signal is not None:
                lines += ["\tlast terminating signal = " + signal]
            if pid:
                lines += ["\tpid = " + str(pid)]
            if arguments:
                lines += ["\targuments = {", *("\t\t" + value for value in arguments), "\t}"]
            lines += ["\tenvironment = {", "\t\tSECRET = HIDDEN", "\t}", "}"]
            return subprocess.CompletedProcess(argv, 0, "\n".join(lines), "")
        if self.command_error:
            if isinstance(self.command_error, BaseException):
                raise self.command_error
            return subprocess.CompletedProcess(argv, self.command_error, "HIDDEN", "SECRET")
        if argv[1] == "bootout":
            self.runtime.pop(argv[2], None)
        elif argv[1] == "bootstrap":
            config = plistlib.loads(Path(argv[3]).read_bytes())
            service = argv[2] + "/" + config["Label"]
            self.configs[service] = config
            self.runtime[service] = ["not running", None, 0, 0]
        elif argv[1] == "kickstart":
            self.runtime[argv[2]] = ["running", 222, 0, 1]
        else:
            self.fail(f"unexpected command: {argv}")
        return subprocess.CompletedProcess(argv, 0, "HIDDEN", "")

    def state(self, identifier=None):
        return service_state(self.db, identifier or self.label, backend=self.backend)

    def act(self, action, before=None):
        before = before or self.state()
        return action(self.db, self.label, expected_revision=before["revision"], backend=self.backend, who="operator")

    def actions(self):
        return [call for call in self.calls if call[1] not in ("print", "print-disabled")]

    def test_inventory_has_unique_user_system_ids_safe_status_and_no_writes(self):
        (self.daemons / self.plist.name).write_bytes(self.plist.read_bytes())
        self.configs["system/" + self.label] = self.config
        self.runtime["system/" + self.label] = ["running", 999, 0, 1]
        before = self.db.total_changes
        result = inventory(self.db, backend=self.backend)
        self.assertEqual({row["id"] for row in result["services"]}, {"user:" + self.label, "system:" + self.label})
        system = self.state("system:" + self.label)
        self.assertEqual((system["scope"], system["state"]), ("system", "running"))
        self.assertTrue(all(not value["allowed"] for value in system["capabilities"].values()))
        self.assertNotIn("HIDDEN", json.dumps(result))
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(self.db.total_changes, before)
        self.assertEqual(self.actions(), [])

    def test_an_inventory_asks_print_disabled_once_and_a_control_read_asks_again(self):
        """sd:10815: an inventory of 13 agents ran `launchctl print-disabled` 13 times; one read asks once."""
        for n in range(3):
            label = f"com.example.extra{n}"
            (self.agents / f"{label}.plist").write_bytes(plistlib.dumps({**self.config, "Label": label}))
            self.configs[f"{self.domain}/{label}"] = {**self.config, "Label": label}
            self.runtime[f"{self.domain}/{label}"] = ["running", 120 + n, 0, 1]
        result = inventory(self.db, backend=self.backend)
        self.assertEqual(len(result["services"]), 4)
        asked = lambda: [call for call in self.calls if call[1] == "print-disabled"]  # noqa: E731
        self.assertEqual(asked(), [["/bin/launchctl", "print-disabled", self.domain]])
        self.assertTrue(all(row["capabilities"]["restart"]["allowed"] for row in result["services"]))
        self.disabled = True
        self.assertEqual(self.state()["capabilities"]["restart"]["reason"], "service is disabled")
        self.assertEqual(len(asked()), 2)

    @hub_only
    def test_stop_unloads_without_deleting_plist_and_start_bootstraps_then_kickstarts(self):
        original = self.plist.read_bytes()
        stopped = self.act(stop_service)
        self.assertEqual(stopped["request"]["status"], "accepted")
        self.assertEqual(stopped["service"]["state"], "unloaded")
        started = self.act(start_service, stopped["service"])
        self.assertEqual(started["service"]["state"], "running")
        self.assertEqual(self.actions(), [["/bin/launchctl", "bootout", self.domain + "/" + self.label],
            ["/bin/launchctl", "bootstrap", self.domain, str(self.plist)],
            ["/bin/launchctl", "kickstart", self.domain + "/" + self.label]])
        self.assertEqual(self.plist.read_bytes(), original)

    @hub_only
    def test_restart_verifies_stop_then_loads_and_starts_same_configuration(self):
        result = self.act(restart_service)
        self.assertEqual(result["request"]["status"], "accepted")
        self.assertEqual(result["service"]["state"], "running")
        self.assertEqual([call[1] for call in self.actions()], ["bootout", "bootstrap", "kickstart"])

    @hub_only
    def test_idle_start_only_kickstarts_and_running_start_is_refused(self):
        with self.assertRaises(WorkflowError):
            self.act(start_service)
        self.runtime[self.domain + "/" + self.label] = ["not running", None, 1, 2]
        self.assertEqual(self.act(start_service)["request"]["status"], "accepted")
        self.assertEqual([call[1] for call in self.actions()], ["kickstart"])

    @hub_only
    def test_stale_plist_and_same_second_runtime_changes_refuse_before_dispatch(self):
        before = self.state()
        self.config["ProgramArguments"].append("changed")
        self.plist.write_bytes(plistlib.dumps(self.config))
        with self.assertRaises(StaleItem):
            self.act(stop_service, before)
        before = self.state()
        self.runtime[self.domain + "/" + self.label][3] += 1
        with self.assertRaises(StaleItem):
            self.act(stop_service, before)
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_changed_loaded_identity_unknown_output_and_unsafe_files_disable_controls(self):
        self.configs[self.domain + "/" + self.label] = {**self.config, "ProgramArguments": ["/wrong/program"]}
        state = self.state()
        self.assertFalse(state["capabilities"]["stop"]["allowed"])
        with self.assertRaises(WorkflowError):
            self.act(stop_service, state)
        self.configs[self.domain + "/" + self.label] = self.config
        self.plist.chmod(0o666)
        self.assertFalse(self.state()["capabilities"]["stop"]["allowed"])
        self.assertEqual(self.actions(), [])

    def test_scheduled_startup_and_dashboard_agents_are_visible_readonly_cron_is_separate(self):
        for label, extra in (("com.example.updater", {"StartInterval": 60}),
                             ("com.example.startup", {"RunAtLoad": True}),
                             (LABEL_PREFIX + ".sd-dashboard", {"KeepAlive": True}),
                             ("homebrew.mxcl.tailscale", {"KeepAlive": True}),
                             (PREFIX + "daily", {"StartInterval": 60})):
            config = {"Label": label, "ProgramArguments": [str(self.program)], **extra}
            (self.agents / (label + ".plist")).write_bytes(plistlib.dumps(config))
        result = inventory(self.db, backend=self.backend)["services"]
        self.assertNotIn(PREFIX + "daily", [row["label"] for row in result])
        for row in result:
            if row["label"] != self.label:
                self.assertTrue(all(not value["allowed"] for value in row["capabilities"].values()))

    def test_keepalive_after_a_failure_only_is_not_a_service(self):
        # sd:5880. `{SuccessfulExit: false}` reruns a job only after a non-zero
        # exit, so a login job that exits 0 is done, not an idle service. Every
        # other KeepAlive key keeps the job up in some condition, as does a
        # SuccessfulExit false beside any other key.
        cases = (({"SuccessfulExit": False}, "startup"),
                 ({"SuccessfulExit": False, "StartInterval": 3600}, "scheduled"),
                 ({"SuccessfulExit": True}, "service"),
                 ({"SuccessfulExit": False, "Crashed": True}, "service"),
                 ({"Crashed": True}, "service"),
                 ({"Crashed": False}, "service"),
                 ({"NetworkState": True}, "service"),
                 ({"PathState": {"/tmp/example": True}}, "service"),
                 ({"OtherJobEnabled": {"com.example.other": True}}, "service"))
        for keepalive, category in cases:
            interval = keepalive.pop("StartInterval", None)
            with self.subTest(keepalive=keepalive):
                label = "com.example.prune"
                config = {"Label": label, "ProgramArguments": [str(self.program)],
                          "RunAtLoad": True, "KeepAlive": keepalive}
                if interval:
                    config["StartInterval"] = interval
                (self.agents / (label + ".plist")).write_bytes(plistlib.dumps(config))
                self.assertEqual(self.backend._installed("user:" + label)["category"], category)

    def test_inventory_inspects_services_in_parallel_in_order_and_isolates_failures(self):
        # sd:5005. Every print waits on one barrier, so a serial inventory breaks it
        # at its timeout rather than hanging. `broken` fails its read and `gone`
        # vanishes; neither may change another service's row or the order.
        labels = ["com.example.alpha", "com.example.broken", "com.example.gone", self.label, "com.example.zulu"]
        for label in labels:
            config = dict(self.config, Label=label)
            (self.agents / (label + ".plist")).write_bytes(plistlib.dumps(config))
            self.configs[self.domain + "/" + label] = config
            self.runtime[self.domain + "/" + label] = ["running", 111, 0, 2]
        barrier = threading.Barrier(len(labels), timeout=2)

        def overlapping(argv):
            if argv[1] != "print":
                return
            barrier.wait()
            if argv[2].endswith("/com.example.broken"):
                raise OSError("launchctl could not run")
            if argv[2].endswith("/com.example.gone"):
                raise WorkflowError("installed plist is unavailable")
        self.hook = overlapping
        rows = inventory(self.db, backend=self.backend)["services"]
        self.assertEqual([(row["label"], row["state"]) for row in rows],
                         [("com.example.alpha", "running"), ("com.example.broken", "unknown"),
                          ("com.example.server", "running"), ("com.example.zulu", "running")])

    @hub_only
    def test_system_ids_arbitrary_paths_and_missing_revisions_never_dispatch(self):
        for label in ("system:" + self.label, "../server", "-k", self.label + ";echo", "/tmp/evil.plist"):
            with self.assertRaises(WorkflowError):
                stop_service(self.db, label, expected_revision="x", backend=self.backend, who="operator")
        for revision in (None, "", 5):
            with self.assertRaises(WorkflowError):
                stop_service(self.db, self.label, expected_revision=revision, backend=self.backend, who="operator")
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_changed_config_between_stop_and_restart_preserves_new_config_and_does_not_bootstrap(self):
        def race(argv):
            if argv[1] == "bootout":
                changed = dict(self.config, ProgramArguments=["/newer/program"])
                self.plist.write_bytes(plistlib.dumps(changed))
        self.hook = race
        result = self.act(restart_service)
        self.assertEqual(result["request"]["status"], "failed")
        self.assertEqual([call[1] for call in self.actions()], ["bootout"])
        self.assertEqual(result["service"]["state"], "unloaded")
        self.assertIn("/newer/program", self.plist.read_text())

    @hub_only
    def test_restore_blocks_start_restart_but_allows_stop(self):
        record_state(self.db, "restore", key="fixture", body={})
        with self.assertRaisesRegex(WorkflowError, "restore"):
            self.act(restart_service)
        stopped = self.act(stop_service)
        with self.assertRaisesRegex(WorkflowError, "restore"):
            self.act(start_service, stopped["service"])

    @hub_only
    def test_disabled_agent_and_unknown_disabled_observation_refuse_start(self):
        self.runtime.clear()
        self.disabled = True
        with self.assertRaisesRegex(WorkflowError, "disabled"):
            self.act(start_service)
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_unknown_enablement_blocks_start_restart_but_allows_safe_stop(self):
        original = self.run_command
        def unknown(argv):
            if argv[1] == "print-disabled":
                return subprocess.CompletedProcess(argv, 0, "new diagnostic shape", "")
            return original(argv)
        self.backend.runner = unknown
        state = self.state()
        self.assertFalse(state["capabilities"]["restart"]["allowed"])
        stopped = self.act(stop_service, state)
        self.assertEqual(stopped["request"]["status"], "accepted")
        with self.assertRaisesRegex(WorkflowError, "enablement"):
            self.act(start_service, stopped["service"])

    def test_unrecognized_print_disables_every_control(self):
        original = self.run_command
        for output in ("unknown output", self.run_command(["/bin/launchctl", "print", self.domain + "/" + self.label]).stdout.replace("\truns = 2", "\truns = 2\n\truns = 3")):
            def unknown(argv, output=output):
                if argv[1] == "print":
                    return subprocess.CompletedProcess(argv, 0, output, "")
                return original(argv)
            self.backend.runner = unknown
            state = self.state()
            self.assertEqual(state["state"], "unknown")
            self.assertTrue(all(not value["allowed"] for value in state["capabilities"].values()))
        self.assertEqual(self.actions(), [])

    def test_signal_ended_run_without_exit_code_is_observed_not_unknown(self):
        """launchd prints `last terminating signal = Terminated: 15` and no
        `last exit code` line after SIGTERM. That is a recorded outcome, not
        an unreadable one: the service keeps its state and its controls, and
        the signal is public next to last_exit (sd:1331)."""
        service = self.domain + "/" + self.label
        self.runtime[service] = ["running", 31900, None, 6, "Terminated: 15"]
        state = self.state()
        self.assertEqual((state["state"], state["pid"], state["last_exit"], state["last_signal"], state["reason"]),
                         ("running", 31900, None, 15, ""))
        self.assertTrue(state["capabilities"]["stop"]["allowed"])
        self.assertTrue(state["capabilities"]["restart"]["allowed"])
        self.runtime[service] = ["not running", None, None, 6, "Terminated: 15"]
        state = self.state()
        self.assertEqual((state["state"], state["last_exit"], state["last_signal"], state["reason"]), ("idle", None, 15, ""))
        self.assertTrue(state["capabilities"]["start"]["allowed"])
        self.assertEqual(self.actions(), [])

    def test_crash_signal_reads_failed_with_its_name_and_keeps_controls(self):
        """A stopped service whose last run died of SIGSEGV or SIGABRT is failed,
        the reason names the signal, and its controls stay open; a running pid
        with the same history is running; an identity mismatch still outranks it."""
        service = self.domain + "/" + self.label
        self.runtime[service] = ["not running", None, None, 3, "Segmentation fault: 11"]
        state = self.state()
        self.assertEqual((state["state"], state["last_exit"], state["last_signal"], state["reason"]),
                         ("failed", None, 11, "last run ended in SIGSEGV (11)"))
        self.assertTrue(state["capabilities"]["start"]["allowed"])
        self.assertTrue(state["capabilities"]["restart"]["allowed"])
        self.runtime[service] = ["running", 111, None, 4, "Segmentation fault: 11"]
        state = self.state()
        self.assertEqual((state["state"], state["reason"]), ("running", ""))
        self.configs[service] = {**self.config, "ProgramArguments": ["/wrong/program"]}
        self.runtime[service] = ["not running", None, None, 3, "Abort trap: 6"]
        state = self.state()
        self.assertEqual((state["state"], state["last_signal"], state["reason"]),
                         ("failed", 6, "loaded service identity differs from its installed plist"))
        self.assertTrue(all(not value["allowed"] for value in state["capabilities"].values()))
        self.assertEqual(self.actions(), [])

    def test_cpu_limit_signal_reads_failed_with_its_name(self):
        """SIGXCPU is the kernel ending a run that exceeded its CPU limit: a
        failure, not a stop. Vacuity: on e9fb0d52 this read ("idle", "") because
        the failure set held only crash signals."""
        self.runtime[self.domain + "/" + self.label] = ["not running", None, None, 2, "Cputime limit exceeded: 24"]
        state = self.state()
        self.assertEqual((state["state"], state["last_exit"], state["last_signal"], state["reason"]),
                         ("failed", None, 24, "last run ended in SIGXCPU (24)"))
        self.assertTrue(state["capabilities"]["start"]["allowed"])
        self.assertEqual(self.actions(), [])

    def test_file_size_limit_signal_reads_failed_with_its_name(self):
        """SIGXFSZ is the kernel ending a run that exceeded its file-size limit:
        a failure, not a stop. Vacuity: on e9fb0d52 this read ("idle", "")."""
        self.runtime[self.domain + "/" + self.label] = ["not running", None, None, 2, "Filesize limit exceeded: 25"]
        state = self.state()
        self.assertEqual((state["state"], state["last_exit"], state["last_signal"], state["reason"]),
                         ("failed", None, 25, "last run ended in SIGXFSZ (25)"))
        self.assertTrue(state["capabilities"]["start"]["allowed"])
        self.assertEqual(self.actions(), [])

    def test_runs_without_exit_code_or_signal_is_a_deliberate_unknown(self):
        """No loaded agent on a real machine prints runs > 0 with neither line;
        if one does, the state is unknown on purpose and the reason says what
        was missing, instead of a swallowed TypeError."""
        self.runtime[self.domain + "/" + self.label] = ["not running", None, None, 6]
        state = self.state()
        self.assertEqual((state["state"], state["last_exit"], state["last_signal"]), ("unknown", None, None))
        self.assertEqual(state["reason"], "launchctl reports 6 runs with neither a last exit code nor a terminating signal")
        self.assertTrue(all(not value["allowed"] for value in state["capabilities"].values()))
        self.assertEqual(self.actions(), [])

    def test_exit_code_and_signal_lines_decide_state_and_never_raise(self):
        """Every branch of the exit-code decision, one fixture each, so the
        signal branch is proven not to move the others. Real shapes from
        `launchctl print` on this machine are marked."""
        service = self.domain + "/" + self.label
        cases = (
            (["not running", None, "(never exited)", 0], ("idle", None, None)),        # real: bootstrapped, never spawned
            (["running", 111, "(never exited)", 1], ("running", None, None)),          # real: first run still running
            (["not running", None, "(never exited)", 1], ("idle", None, None)),
            (["not running", None, 0, 3], ("idle", 0, None)),                          # real: last run exited 0
            (["not running", None, 1, 2], ("failed", 1, None)),                        # real: last run exited 1
            (["running", 111, 1, 2], ("running", 1, None)),                            # real: running after an exit 1
            (["not running", None, None, 0], ("idle", None, None)),                    # no line at all, never ran
            (["running", 111, None, 6, "Terminated: 15"], ("running", None, 15)),      # real: SIGTERM, restarted
            (["not running", None, None, 6, "Terminated: 15"], ("idle", None, 15)),
            (["not running", None, None, 2, "Killed: 9"], ("idle", None, 9)),         # an operator's kill, not a crash
            (["not running", None, None, 3, "Segmentation fault: 11"], ("failed", None, 11)),  # a crash: the kernel's verdict
            (["not running", None, None, 3, "Abort trap: 6"], ("failed", None, 6)),
            (["running", 111, None, 3, "Segmentation fault: 11"], ("running", None, 11)),  # running wins over history
            (["not running", None, None, 2, "Cputime limit exceeded: 24"], ("failed", None, 24)),  # a resource limit: a failure
            (["not running", None, None, 2, "Filesize limit exceeded: 25"], ("failed", None, 25)),
            (["not running", None, None, 2, "Hangup: 1"], ("idle", None, 1)),           # the stop path, not a failure
            (["not running", None, 0, 2, "Terminated: 15"], ("idle", 0, 15)),          # both lines: code stays the verdict
            (["not running", None, "junk", 2], ("unknown", None, None)),
            (["not running", None, None, 6, "garbage"], ("unknown", None, None)),
            (["not running", None, None, 6], ("unknown", None, None)),
        )
        for runtime, expected in cases:
            with self.subTest(runtime=runtime):
                self.runtime[service] = runtime
                state = self.state()
                self.assertEqual((state["state"], state["last_exit"], state["last_signal"]), expected)
                self.assertNotIn("HIDDEN", json.dumps(state))
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_program_only_agent_has_exact_loaded_identity(self):
        self.config = {"Label": self.label, "Program": str(self.program), "KeepAlive": True}
        self.plist.write_bytes(plistlib.dumps(self.config))
        self.configs[self.domain + "/" + self.label] = self.config
        self.assertTrue(self.state()["capabilities"]["stop"]["allowed"])
        self.assertEqual(self.act(restart_service)["service"]["state"], "running")

    @hub_only
    def test_symlink_and_malformed_plists_remain_visible_without_controls(self):
        alternate = self.root / "alternate.plist"
        alternate.write_bytes(self.plist.read_bytes())
        self.plist.unlink()
        self.plist.symlink_to(alternate)
        self.assertFalse(self.state()["capabilities"]["stop"]["allowed"])
        self.plist.unlink()
        self.plist.write_text("not a plist")
        self.assertEqual(len(inventory(self.db, backend=self.backend)["services"]), 1)
        with self.assertRaises(WorkflowError):
            self.act(stop_service)
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_accepted_stop_without_observed_unload_does_not_continue_restart(self):
        original = self.run_command
        def no_effect(argv):
            if argv[1] == "bootout":
                self.calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, "", "")
            return original(argv)
        self.backend.runner = no_effect
        result = self.act(restart_service)
        self.assertEqual((result["request"]["status"], result["request"]["phase"]), ("unknown", "bootout"))
        self.assertEqual(result["service"]["state"], "running")
        self.assertFalse(result["service"]["capabilities"]["restart"]["allowed"])
        self.assertEqual([call[1] for call in self.actions()], ["bootout"])

    @hub_only
    def test_registration_failure_keeps_observed_unloaded_state_and_phase(self):
        def fail_registration(argv):
            if argv[1] == "bootstrap":
                self.command_error = 5
        self.hook = fail_registration
        result = self.act(restart_service)
        self.assertEqual((result["request"]["status"], result["request"]["phase"]), ("failed", "bootstrap"))
        self.assertEqual(result["service"]["state"], "unloaded")
        self.assertEqual([call[1] for call in self.actions()], ["bootout", "bootstrap"])

    @hub_only
    def test_transient_spawn_after_registration_is_observed_before_kickstart(self):
        self.runtime.clear()
        original = self.run_command
        transient = 2
        def spawning(argv):
            nonlocal transient
            result = original(argv)
            if argv[1] == "print" and result.returncode == 0 and transient:
                transient -= 1
                result.stdout = result.stdout.replace("state = not running", "state = spawn scheduled")
            return result
        self.backend.runner = spawning
        result = self.act(start_service)
        self.assertEqual(result["request"]["status"], "accepted")
        self.assertEqual(result["service"]["state"], "running")
        self.assertEqual([call[1] for call in self.actions()], ["bootstrap", "kickstart"])

    @hub_only
    def test_unknown_registration_observation_times_out_without_kickstart(self):
        self.runtime.clear()
        original = self.run_command
        def unknown(argv):
            result = original(argv)
            if argv[1] == "print" and result.returncode == 0:
                result.stdout = "unrecognized launchctl response"
            return result
        self.backend.runner = unknown
        with patch("sd_db.services._SETTLE_SECONDS", 0):
            result = self.act(start_service)
        self.assertEqual(result["request"]["status"], "unknown")
        self.assertEqual(result["service"]["state"], "unknown")
        self.assertEqual([call[1] for call in self.actions()], ["bootstrap"])

    @hub_only
    def test_plist_changed_during_runtime_inspection_is_not_a_dispatchable_snapshot(self):
        before = self.state()
        def race(argv):
            if argv[1] == "print-disabled":
                self.plist.write_bytes(plistlib.dumps(dict(self.config, RunAtLoad=False)))
        self.hook = race
        with self.assertRaises(StaleItem):
            self.act(stop_service, before)
        self.assertFalse(self.state()["capabilities"]["stop"]["allowed"])
        self.assertEqual(self.actions(), [])

    @hub_only
    def test_failed_and_timed_out_requests_are_honest_and_do_not_expose_output(self):
        for error, status in ((5, "failed"), (subprocess.TimeoutExpired("launchctl", 10), "unknown")):
            self.command_error = error
            self.runtime[self.domain + "/" + self.label][3] += 1
            result = self.act(stop_service)
            self.assertEqual(result["request"]["status"], status)
            self.assertEqual(result["service"]["state"], "running")
            self.assertNotIn("HIDDEN", json.dumps(result))
            self.assertNotIn("SECRET", json.dumps(result))

    @hub_only
    def test_crash_at_dispatch_keeps_durable_request_and_rejects_stale_replay(self):
        before = self.state()
        self.command_error = SystemExit(86)
        with self.assertRaises(SystemExit):
            self.act(stop_service, before)
        current = self.state()
        self.assertEqual(current["last_request"]["status"], "requested")
        with self.assertRaises(StaleItem):
            self.act(stop_service, before)
        self.assertFalse(current["capabilities"]["stop"]["allowed"])

    @hub_only
    def test_final_recheck_after_durable_request_refuses_new_runtime(self):
        original = self.backend.inspect
        calls = 0
        def race(identifier):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.runtime[self.domain + "/" + self.label][1] = 999
            return original(identifier)
        before = self.state()
        with patch.object(self.backend, "inspect", side_effect=race):
            result = self.act(stop_service, before)
        self.assertEqual(result["request"]["status"], "failed")
        self.assertEqual(self.actions(), [])


if __name__ == "__main__":
    unittest.main()
