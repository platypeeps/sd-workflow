"""Installer safety with private files, fake launchd, and fake Tailscale state."""

import copy
import http.client
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import runtime

REAL_LIBRARY_CHECK = runtime.installed_library
REAL_HERE = runtime.HERE


ORIGIN = "https://fixture.example.ts.net:8443"
AUTHORITY = "fixture.example.ts.net:8443"
PUBLIC = {"TCP": {"443": {"HTTPS": True}},
          "Web": {"fixture.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"}}}},
          "AllowFunnel": {"fixture.example.ts.net:443": True}}


class RuntimeSafety(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dashboard-runtime-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = self.home / "dashboard.json"
        self.config.write_text(json.dumps({"origin": ORIGIN, "operator_login": "owner@example.test", "port": 8767}))
        self.target = self.home / "Library/LaunchAgents" / f"{runtime.LABEL}.plist"
        self.target.parent.mkdir(parents=True)
        self.old = plistlib.dumps({"Label": runtime.LABEL, "ProgramArguments": ["old-dashboard", "serve"]})
        self.target.write_bytes(self.old)
        self.status = copy.deepcopy(PUBLIC)
        self.calls = []
        self.loaded = True
        self.pending_removal = None
        self.info = {"python": sys.executable, "library": "/fixture/.venv/lib/python3.13/site-packages/sd_db/__init__.py", "schema": 3,
                     "library_digest": "a" * 64, "dashboard_digest": "b" * 64}
        # launchd's pid is the /bin/bash parent; the server is its child (sd:6444).
        self.health = {"ok": True, "service": "sd-dashboard", "pid": 4243, "ppid": 4242, **self.info}
        self.addCleanup(patch.stopall)
        patch.object(runtime, "installed_library", return_value=self.info).start()
        patch.object(runtime, "_run", side_effect=self.run_command).start()
        patch.object(runtime, "health_check", return_value=self.health).start()

    def run_command(self, arguments, *, check=True, timeout=20):
        self.calls.append(arguments)
        code, output = 0, ""
        if arguments[:4] == ["tailscale", "serve", "status", "--json"]:
            output = json.dumps(self.status)
        elif arguments == ["tailscale", "status", "--json"]:
            output = json.dumps({"BackendState": "Running", "Self": {"DNSName": "fixture.example.ts.net.", "UserID": 42, "TailscaleIPs": ["100.64.0.10"]},
                                 "User": {"42": {"LoginName": "owner@example.test"}}})
        elif arguments == ["tailscale", "serve", "--bg", "--https=8443", "--set-path=/", "off"]:
            self.status["TCP"].pop("8443", None)
            self.status["Web"].pop(AUTHORITY, None)
        elif arguments == ["tailscale", "serve", "--bg", "--https=8443", "--set-path=/", "http://127.0.0.1:8767"]:
            self.status["TCP"]["8443"] = {"HTTPS": True}
            self.status["Web"][AUTHORITY] = {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8767"}}}
        elif arguments[:2] == ["launchctl", "print"]:
            code = 0 if self.loaded else 113
            argv = plistlib.loads(self.target.read_bytes())["ProgramArguments"] if self.target.exists() else ["missing"]
            output = (arguments[2] + " = {\n\tpid = 4242\n\tpath = " + str(self.target) +
                      "\n\tprogram = " + argv[0] + "\n\targuments = {\n" +
                      "\n".join("\t\t" + argument for argument in argv) + "\n\t}\n}")
        elif arguments[:2] == ["launchctl", "bootout"]:
            self.loaded = False
        elif arguments[:2] == ["launchctl", "bootstrap"]:
            self.loaded = True
        else:
            self.fail(f"unexpected command: {arguments}")
        return subprocess.CompletedProcess(arguments, code, output, "")

    def delayed_removal(self, arguments, **options):
        if arguments[:2] == ["launchctl", "bootout"]:
            result = self.run_command(arguments, **options)
            self.loaded = True
            self.pending_removal = [2, self.target.read_bytes()]
            return result
        if arguments[:2] == ["launchctl", "print"] and self.pending_removal is not None:
            self.assertEqual(self.target.read_bytes(), self.pending_removal[1])
            if self.pending_removal[0]:
                self.pending_removal[0] -= 1
            else:
                self.loaded = False
                self.pending_removal = None
        if arguments[:2] == ["launchctl", "bootstrap"]:
            self.assertFalse(self.loaded, "bootstrap must wait for removal")
            self.assertIsNone(self.pending_removal)
        return self.run_command(arguments, **options)

    def plan(self):
        return runtime.preflight(self.config, home=self.home)

    def install(self, plan):
        return runtime.install(self.config, expected_fingerprint=plan["fingerprint"], home=self.home)

    def test_preflight_is_read_only_and_names_exact_replacement(self):
        before = {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        plan = self.plan()
        self.assertFalse(plan["serve_route_present"])
        self.assertEqual(plan["program"][:4], ["/bin/bash", "-c", runtime.LAUNCH_SCRIPT, str(runtime.HERE / "dashboard.sh")])
        self.assertEqual(before, {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob("*") if p.is_file()})
        self.assertEqual(self.status, PUBLIC)
        self.assertTrue(all(call[1] in ("serve", "status", "print") for call in self.calls))

    def test_install_preserves_public_funnel_and_uses_installed_interpreter(self):
        result = self.install(self.plan())
        self.assertTrue(result["ok"])
        self.assertEqual(runtime._without_dashboard(self.status, AUTHORITY), PUBLIC)
        self.assertEqual((Path(result["backup_path"]) / self.target.name).read_bytes(), self.old)
        plist = plistlib.loads(self.target.read_bytes())
        self.assertEqual(plist["EnvironmentVariables"]["SD_DASHBOARD_PYTHON"], sys.executable)
        self.assertNotIn("PYTHONPATH", plist["EnvironmentVariables"])
        self.assertEqual(plist["Label"], runtime.LABEL)
        # An interactive server: Background put it on efficiency cores and
        # low-priority I/O, and a Today render took 4-5x longer (sd:1433).
        self.assertEqual(plist["ProcessType"], "Standard")
        self.assertTrue(self.loaded)
        self.assertFalse(any("funnel" in call or "reset" in call or "set-config" in call for call in self.calls))

    def test_install_starts_dashboard_under_apples_bash(self):
        """sd:6444: /bin/bash stays the TCC responsible process, so a Python upgrade keeps disk access."""
        written = runtime._plist(self.config.resolve(), runtime.read_config(self.config), self.home)
        argv = plistlib.loads(written)["ProgramArguments"]
        self.assertEqual(argv[:2], ["/bin/bash", "-c"])
        # A trailing command stops bash -c from exec'ing its last one.
        self.assertTrue(argv[2].endswith("; exit $?"), argv[2])
        self.assertEqual(argv[3:5], [str(runtime.HERE / "dashboard.sh"), "serve"])
        self.assertTrue(self.install(self.plan())["ok"])
        self.assertEqual(self.target.read_bytes(), written)

    def test_install_starts_dashboard_under_job_launch_where_it_is_built(self):
        """system sd:6466: Apple's bash passes its grant only to Apple programs."""
        launcher = self.home / runtime.JOB_LAUNCH
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexec \"$@\"\n")
        launcher.chmod(0o755)
        written = runtime._plist(self.config.resolve(), runtime.read_config(self.config), self.home)
        argv = plistlib.loads(written)["ProgramArguments"]
        self.assertEqual(argv[:3], [str(launcher), str(runtime.HERE / "dashboard.sh"), "serve"])
        self.assertTrue(self.install(self.plan())["ok"])
        self.assertEqual(self.target.read_bytes(), written)

    def test_bash_launcher_stays_the_parent_and_passes_the_exit_code(self):
        script = self.home / "child.sh"
        script.write_text('#!/bin/sh\necho "$PPID $1"\nexit 7\n')
        script.chmod(0o755)
        child = subprocess.Popen(["/bin/bash", "-c", runtime.LAUNCH_SCRIPT, str(script), "a b"],
                                 stdout=subprocess.PIPE, text=True)
        output, _ = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 7)
        self.assertEqual(output, f"{child.pid} a b\n")

    def test_install_waits_for_delayed_launchd_removal_before_replacement(self):
        with patch.object(runtime, "_run", side_effect=self.delayed_removal), \
                patch.object(runtime.time, "sleep"):
            self.assertTrue(self.install(self.plan())["ok"])
        self.assertEqual(sum(call[1] == "bootstrap" for call in self.calls), 1)

    def test_rollback_waits_for_delayed_removal_before_restoring_and_restarting(self):
        with patch.object(runtime, "_run", side_effect=self.delayed_removal), \
                patch.object(runtime.time, "sleep"), \
                patch.object(runtime, "health_check", side_effect=runtime.RuntimeRefused("unhealthy")):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "previous service restored"):
                self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertTrue(self.loaded)
        self.assertEqual(sum(call[1] == "bootstrap" for call in self.calls), 2)

    def test_shutdown_timeout_preserves_plist_without_bootstrap_or_second_bootout(self):
        def stuck(arguments, **options):
            result = self.run_command(arguments, **options)
            if arguments[:2] == ["launchctl", "bootout"]:
                self.loaded = True
            return result
        ticks = iter(range(0, 1000, 4))
        with patch.object(runtime, "_run", side_effect=stuck), \
                patch.object(runtime.time, "monotonic", side_effect=lambda: next(ticks)), \
                patch.object(runtime.time, "sleep"):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "shutdown deadline.*rollback requires attention"):
                self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(sum(call[1] == "bootout" for call in self.calls), 1)
        self.assertFalse(any(call[1] == "bootstrap" for call in self.calls))

    def test_shutdown_wait_rejects_unknown_launchctl_status(self):
        def unknown(arguments, **options):
            result = self.run_command(arguments, **options)
            if arguments[:2] == ["launchctl", "print"] and any(call[1] == "bootout" for call in self.calls):
                result.returncode = 5
            return result
        with patch.object(runtime, "_run", side_effect=unknown):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "identity changed or is unknown.*rollback requires attention"):
                self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertFalse(any(call[1] == "bootstrap" for call in self.calls))

    def test_shutdown_wait_preserves_concurrent_plist_or_loaded_identity(self):
        for changed_plist in (False, True):
            with self.subTest(changed_plist=changed_plist):
                self.calls.clear()
                self.target.write_bytes(self.old)
                self.loaded = True
                newer = plistlib.dumps({"Label": runtime.LABEL, "ProgramArguments": ["newer-dashboard"]})
                def concurrent(arguments, *, changed_plist=changed_plist, newer=newer, **options):
                    result = self.run_command(arguments, **options)
                    if arguments[:2] == ["launchctl", "bootout"]:
                        self.loaded = True
                        if changed_plist:
                            self.target.write_bytes(newer)
                    if (arguments[:2] == ["launchctl", "print"] and not changed_plist and
                            any(call[1] == "bootout" for call in self.calls)):
                        result.stdout = result.stdout.replace("\tprogram = old-dashboard", "\tprogram = newer-dashboard")
                    return result
                with patch.object(runtime, "_run", side_effect=concurrent):
                    with self.assertRaisesRegex(runtime.RuntimeRefused, "rollback requires attention"):
                        self.install(self.plan())
                self.assertEqual(self.target.read_bytes(), newer if changed_plist else self.old)
                self.assertEqual(sum(call[1] == "bootout" for call in self.calls), 1)
                self.assertFalse(any(call[1] == "bootstrap" for call in self.calls))

    def test_ip_listener_requires_health_confirmation_and_preserves_https(self):
        config = json.loads(self.config.read_text())
        config["ip_origin"] = "http://100.64.0.10:8768"
        self.config.write_text(json.dumps(config))
        self.health["ip_origin"] = config["ip_origin"]
        result = self.install(self.plan())
        self.assertEqual(result["origin"], config["ip_origin"])
        self.assertEqual(runtime._without_dashboard(self.status, AUTHORITY), PUBLIC)
        self.assertNotIn("8768", self.status["TCP"])

    def test_missing_ip_listener_health_restores_previous_service(self):
        config = json.loads(self.config.read_text())
        config["ip_origin"] = "http://100.64.0.10:8768"
        self.config.write_text(json.dumps(config))
        with self.assertRaisesRegex(runtime.RuntimeRefused, "does not confirm.*IP listener"):
            self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertTrue(self.loaded)
        self.assertEqual(self.status, PUBLIC)

    def test_failed_health_restores_old_plist_service_and_only_owned_route(self):
        plan = self.plan()
        with patch.object(runtime, "health_check", side_effect=runtime.RuntimeRefused("unhealthy")):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "previous service restored"):
                self.install(plan)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(self.status, PUBLIC)
        self.assertTrue(self.loaded)
        self.assertIn(["tailscale", "serve", "--bg", "--https=8443", "--set-path=/", "off"], self.calls)

    def test_creation_that_applies_then_times_out_is_observed_and_rolled_back(self):
        def ambiguous(arguments, **options):
            result = self.run_command(arguments, **options)
            if arguments[-1] == "http://127.0.0.1:8767":
                raise runtime.RuntimeRefused("tailscale command timed out")
            return result
        with patch.object(runtime, "_run", side_effect=ambiguous):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "previous service restored"):
                self.install(self.plan())
        self.assertEqual(self.status, PUBLIC)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertTrue(self.loaded)

    def test_creation_failure_without_side_effect_needs_no_route_removal(self):
        def refused(arguments, **options):
            if arguments[-1] == "http://127.0.0.1:8767":
                raise runtime.RuntimeRefused("tailscale command refused")
            return self.run_command(arguments, **options)
        with patch.object(runtime, "_run", side_effect=refused):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "previous service restored"):
                self.install(self.plan())
        self.assertEqual(self.status, PUBLIC)
        self.assertFalse(any(arguments[-1] == "off" for arguments in self.calls))

    def test_unconfirmed_route_removal_reports_manual_recovery(self):
        def ineffective(arguments, **options):
            if arguments[-1] == "off":
                return subprocess.CompletedProcess(arguments, 0, "", "")
            return self.run_command(arguments, **options)
        with patch.object(runtime, "_run", side_effect=ineffective), \
                patch.object(runtime, "health_check", side_effect=runtime.RuntimeRefused("unhealthy")):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "rollback requires attention.*verify removal"):
                self.install(self.plan())
        self.assertIn(AUTHORITY, self.status["Web"])
        self.assertEqual(runtime._without_dashboard(self.status, AUTHORITY), PUBLIC)
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_final_read_refuses_new_serve_state_before_creation(self):
        plan = self.plan()
        def changed(arguments, **options):
            result = self.run_command(arguments, **options)
            if arguments[:2] == ["launchctl", "print"]:
                self.status["Web"]["new.example.ts.net:9999"] = {"Handlers": {"/": {"Text": "concurrent"}}}
            return result
        with patch.object(runtime, "_run", side_effect=changed):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "changed before route creation"):
                self.install(plan)
        self.assertFalse(any("--bg" in arguments for arguments in self.calls))
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_concurrent_plist_edit_before_replacement_is_preserved(self):
        newer = plistlib.dumps({"Label": runtime.LABEL, "ProgramArguments": ["newer-dashboard"]})
        def concurrent(arguments, **options):
            result = self.run_command(arguments, **options)
            if arguments[-1] == "http://127.0.0.1:8767":
                self.target.write_bytes(newer)
            return result
        with patch.object(runtime, "_run", side_effect=concurrent):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "changed before replacement"):
                self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), newer)
        self.assertTrue(self.loaded)
        self.assertFalse(any(call[1] in ("bootout", "bootstrap") or call[-1] == "off" for call in self.calls))

    def test_concurrent_plist_edit_during_failed_health_is_preserved(self):
        newer = plistlib.dumps({"Label": runtime.LABEL, "ProgramArguments": ["newer-dashboard"]})
        def concurrent(*args, **kwargs):
            self.target.write_bytes(newer)
            raise runtime.RuntimeRefused("new health failed")
        with patch.object(runtime, "health_check", side_effect=concurrent):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "rollback requires attention.*plist changed"):
                self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), newer)
        self.assertTrue(self.loaded)
        self.assertEqual(sum(call[1] == "bootout" for call in self.calls), 1)
        self.assertFalse(any(call[-1] == "off" for call in self.calls))

    def test_changed_loaded_service_identity_is_preserved_during_rollback(self):
        def concurrent(arguments, **options):
            result = self.run_command(arguments, **options)
            if arguments[:2] == ["launchctl", "print"] and any(call[1] == "bootstrap" for call in self.calls):
                result.stdout = result.stdout.replace("\tpath = " + str(self.target), "\tpath = /newer/operator.plist")
            return result
        with patch.object(runtime, "_run", side_effect=concurrent), \
                patch.object(runtime, "health_check", side_effect=runtime.RuntimeRefused("unhealthy")):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "rollback requires attention.*identity changed"):
                self.install(self.plan())
        self.assertTrue(self.loaded)
        self.assertEqual(sum(call[1] == "bootout" for call in self.calls), 1)

    def test_malformed_remote_config_and_node_state_fail_closed(self):
        config = {"origin": [], "operator_login": "owner@example.test"}
        with self.assertRaises(runtime.RuntimeRefused):
            runtime._check_node(config)
        bad = {"BackendState": "Running", "Self": {"DNSName": None}}
        with patch.object(runtime, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(bad), "")):
            with self.assertRaises(runtime.RuntimeRefused):
                runtime._check_node({"origin": ORIGIN, "operator_login": "owner@example.test"})

    def test_wrong_library_health_rolls_back(self):
        self.health["library"] = "/checkout/system/sd_db/__init__.py"
        with self.assertRaisesRegex(runtime.RuntimeRefused, "different schema or library"):
            self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(self.status, PUBLIC)

    def test_same_schema_different_health_build_rolls_back(self):
        for key in ("library_digest", "dashboard_digest"):
            with self.subTest(key=key):
                self.health[key] = "0" * 64
                with self.assertRaisesRegex(runtime.RuntimeRefused, "different build digests"):
                    self.install(self.plan())
                self.assertEqual(self.target.read_bytes(), self.old)
                self.assertEqual(self.status, PUBLIC)
                self.assertTrue(self.loaded)
                self.health[key] = self.info[key]

    def test_same_path_schema_library_replacement_invalidates_preview(self):
        import sd_db
        from sd_db.migrate import initialise

        pack = self.home.resolve() / "pack"
        package = pack / "lib/sd_db"
        package.mkdir(parents=True)
        source = package / "__init__.py"
        source.write_text("# first library build\n")
        database = self.home / "sd.db"
        initialise(database)
        config = json.loads(self.config.read_text())
        config["database"] = str(database)
        self.config.write_text(json.dumps(config))
        with patch.object(sd_db, "__file__", str(source)), patch.object(runtime, "pack_library", return_value=pack / "lib"), \
                patch.object(runtime, "_pack_commit", return_value=None), \
                patch.object(runtime, "installed_library", side_effect=REAL_LIBRARY_CHECK):
            before = self.plan()
            source.write_text("# second library build, unchanged schema and path\n")
            after = self.plan()
            self.assertEqual(before["runtime"]["library"], after["runtime"]["library"])
            self.assertEqual(before["runtime"]["schema"], after["runtime"]["schema"])
            self.assertNotEqual(before["runtime"]["library_digest"], after["runtime"]["library_digest"])
            self.assertNotEqual(before["fingerprint"], after["fingerprint"])
            with self.assertRaisesRegex(runtime.RuntimeRefused, "state changed"):
                self.install(before)
        self.assertFalse(any(call[1] in ("bootstrap", "bootout") or "--bg" in call for call in self.calls))
        self.assertFalse((self.home / ".local/share/sd/runtime-backups").exists())

    def test_healthy_unrelated_listener_cannot_satisfy_installation(self):
        self.health.update(pid=31337, ppid=31336)
        with self.assertRaisesRegex(runtime.RuntimeRefused, "newly loaded dashboard process"):
            self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(self.status, PUBLIC)
        self.assertTrue(self.loaded)

    def test_listener_without_its_parent_pid_cannot_satisfy_installation(self):
        """An older build's /health names no ppid; the new bash child must."""
        del self.health["ppid"]
        with self.assertRaisesRegex(runtime.RuntimeRefused, "newly loaded dashboard process"):
            self.install(self.plan())
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_unknown_or_duplicate_launchctl_pid_is_refused(self):
        service = f"gui/{os.getuid()}/{runtime.LABEL}"
        for output in ("unknown diagnostic output", service + " = {\n\tpid = 4242\n\tpid = 4242\n}"):
            with patch.object(runtime, "_run", return_value=subprocess.CompletedProcess([], 0, output, "")):
                with self.assertRaisesRegex(runtime.RuntimeRefused, "newly loaded dashboard process"):
                    runtime._verify_loaded_process(f"gui/{os.getuid()}", self.health)

    def test_stale_plan_refuses_before_any_mutation(self):
        plan = self.plan()
        self.target.write_bytes(self.old + b"\n")
        with self.assertRaisesRegex(runtime.RuntimeRefused, "changed"):
            self.install(plan)
        self.assertFalse(any(call[1] in ("bootstrap", "bootout") or "--bg" in call for call in self.calls))

    def test_conflicting_or_public_dashboard_route_is_refused(self):
        self.status["TCP"]["8443"] = {"HTTPS": True}
        self.status["Web"][AUTHORITY] = {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}
        with self.assertRaisesRegex(ValueError, "proxy"):
            self.plan()
        self.status["AllowFunnel"][AUTHORITY] = True
        with self.assertRaisesRegex(runtime.RuntimeRefused, "public through Funnel"):
            self.plan()

    def test_config_mismatch_and_reserved_origin_are_refused(self):
        with self.assertRaisesRegex(runtime.RuntimeRefused, "port differs"):
            runtime.load_frontdoor(self.config, 9999)
        data = json.loads(self.config.read_text())
        data["origin"] = "https://fixture.example.ts.net"
        self.config.write_text(json.dumps(data))
        with self.assertRaisesRegex(runtime.RuntimeRefused, ":8443 only"):
            self.plan()

    def test_wrong_node_and_operator_refuse_before_creating_a_route(self):
        config = json.loads(self.config.read_text())
        config["origin"] = "https://someone-else.example.ts.net:8443"
        self.config.write_text(json.dumps(config))
        with self.assertRaisesRegex(runtime.RuntimeRefused, "not this Tailscale node"):
            self.plan()
        config.update(origin=ORIGIN, operator_login="someone@example.test")
        self.config.write_text(json.dumps(config))
        with self.assertRaisesRegex(runtime.RuntimeRefused, "not this node's Tailscale owner"):
            self.plan()
        self.assertEqual(self.status, PUBLIC)

    def test_local_configuration_has_no_tailscale_dependency(self):
        self.config.write_text(json.dumps({"port": 8767}))
        self.assertIsNone(runtime.load_frontdoor(self.config, 8767))
        self.assertEqual(self.calls, [])

    def test_local_only_install_never_calls_tailscale(self):
        self.config.write_text(json.dumps({"port": 8767}))
        result = self.install(self.plan())
        self.assertEqual(result["origin"], "http://127.0.0.1:8767")
        self.assertEqual(self.status, PUBLIC)
        self.assertFalse(any(call[0] == "tailscale" for call in self.calls))

    def test_an_install_without_sd_or_the_vault_says_so_and_succeeds(self):
        import contextlib
        import io

        said = io.StringIO()
        with QuickNoteEnvironment.which(self, None), patch.dict(os.environ, {}), contextlib.redirect_stderr(said):
            os.environ.pop("OBSIDIAN_VAULT", None)
            result = self.install(self.plan())
        self.assertEqual(result["origin"], ORIGIN)
        self.assertEqual(said.getvalue().splitlines(), [
            "dashboard: sd is not on the installing shell's PATH; Notes cannot list or keep quick notes",
            "dashboard: OBSIDIAN_VAULT is not set; Notes cannot list or keep quick notes"])

    def test_production_path_resolves_the_platform_listener_inspector(self):
        self.assertIsNotNone(shutil.which("lsof", path=runtime.LAUNCH_PATH),
                             "the production LaunchAgent must find the installed lsof")

    def test_runtime_refuses_a_source_library_before_opening_a_database(self):
        import sd_db

        with patch.object(sd_db, "__file__", "/source/system/sd_db/__init__.py"):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "the command pack's lib/"):
                REAL_LIBRARY_CHECK(self.home / "missing.db")
        self.assertFalse((self.home / "missing.db").exists())

    def test_concurrent_route_change_is_preserved_during_rollback(self):
        def concurrent_change(*args, **kwargs):
            self.status["Web"][AUTHORITY]["Handlers"]["/new"] = {"Proxy": "http://127.0.0.1:9999"}
            raise runtime.RuntimeRefused("health failed")
        with patch.object(runtime, "health_check", side_effect=concurrent_change):
            with self.assertRaisesRegex(runtime.RuntimeRefused, "rollback requires attention"):
                self.install(self.plan())
        self.assertIn("/new", self.status["Web"][AUTHORITY]["Handlers"])
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(runtime._without_dashboard(self.status, AUTHORITY), PUBLIC)

    def test_production_launcher_ignores_source_pythonpath(self):
        # Execute the real shell and isolated bootstrap. A poison stdlib/module
        # on PYTHONPATH must not run even when the caller explicitly offers it.
        poison = self.home / "poison"
        poison.mkdir()
        marker = self.home / "poison-ran"
        (poison / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').write('unsafe')\n")
        result = subprocess.run([str(runtime.HERE / "dashboard.sh"), "install", "--help"],
                                env={**os.environ, "SD_DASHBOARD_PYTHON": sys.executable,
                                     "PYTHONPATH": str(poison)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("expected-fingerprint", result.stdout)
        self.assertFalse(marker.exists())

    def test_the_vault_probe_child_is_isolated_too(self):
        # `-I` on the exec isolates the interpreter that loads sd_tile.py and
        # nothing it starts. The probe is a second process, and it is the one
        # whose Full Disk Access is being reported, so a caller's PYTHONPATH
        # reaching it decides the answer: a sitecustomize runs before the -c
        # body and can print, raise, or read the vault in its place.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_probe_collectors", runtime.HERE / "collectors.py")
        collectors = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collectors)

        poison = self.home / "probe-poison"
        poison.mkdir()
        marker = self.home / "probe-poison-ran"
        (poison / "sitecustomize.py").write_text(
            f"open({str(marker)!r}, 'w').write('unsafe')\nprint('denied')\n")
        listed = self.home / "listed"
        listed.mkdir()

        with patch.object(collectors, "VAULT", listed), \
                patch.dict(os.environ, {"PYTHONPATH": str(poison)}):
            answer = collectors.probe_vault(sys.executable)

        self.assertFalse(marker.exists())
        self.assertEqual(answer, "ok")

    def test_the_help_names_the_resource_views_the_code_has(self):
        # Enumerated from reports_screen.VIEWS, not from a list written here,
        # so a view added or dropped tomorrow fails this without an edit. The
        # help said "the first five" of a six-name list, which took in `ports`
        # -- which has no view -- and left out `queues`, which has one.
        import ast
        source = (runtime.HERE / "sd_dashboard" / "reports_screen.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        views = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "VIEWS" for t in node.targets):
                views = [pair.elts[0].value for pair in node.value.elts]
        self.assertTrue(views, "VIEWS not found in reports_screen.py")

        help_text = subprocess.run(
            [str(runtime.HERE / "dashboard.sh"), "--help"],
            capture_output=True, text=True).stdout
        line = [b for b in help_text.split("\n\n") if "machine and vault views" in b]
        self.assertTrue(line, help_text)
        sentence = " ".join(line[0].split())
        for name in views:
            self.assertIn(name, sentence, sentence)
        self.assertNotIn("first five", sentence, sentence)

    def test_the_launcher_hands_on_the_prefix_and_vault_kept_in_shared_env(self):
        # sd:3990. The prefix and the vault sit once in <config>/shared.env;
        # runtime.py reads the prefix, and `sd store` the vault, from the
        # environment. A DASHBOARD_PYTHON stub prints what `tile` leaves in it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config/shared.env").write_text('SYSTEM_TOOLS_LABEL_PREFIX="org.example"\nOBSIDIAN_VAULT="/vaults/Shared"\n')
            stub = root / "python"
            stub.write_text('#!/bin/sh\nprintf "%s|%s" "$SYSTEM_TOOLS_LABEL_PREFIX" "$OBSIDIAN_VAULT"\n')
            stub.chmod(0o755)
            env = {"PATH": os.environ["PATH"], "HOME": directory, "SYSTEM_TOOLS_CONFIG": str(root / "config"),
                   "DASHBOARD_PYTHON": str(stub)}
            result = subprocess.run([str(runtime.HERE / "dashboard.sh"), "tile", "vault"], env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(result.stdout, "org.example|/vaults/Shared", result.stderr)

    def test_every_runtime_python_exec_is_isolated(self):
        # Enumerated from the script, not from a list written here: a
        # subcommand added tomorrow is covered without anyone editing this
        # test. `grants` was the entrypoint that shipped without -I, so a
        # caller's PYTHONPATH decided what its Full Disk Access probe ran.
        script = (runtime.HERE / "dashboard.sh").read_text(encoding="utf-8")
        execs = [line.strip() for line in script.splitlines()
                 if line.strip().startswith('exec "$runtime_python"')]
        self.assertTrue(execs, "no $runtime_python exec found in dashboard.sh")
        for line in execs:
            self.assertIn(' -I ', line, line)


class BuildIdentity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dashboard-build-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / "sd_db"
        self.dashboard = self.root / "dashboard"
        # The system checkout, whose two tools the digest covers (sd:5987).
        self.system = self.root / "system"
        self.library.mkdir()
        (self.library / "__init__.py").write_text("# package\n")
        (self.dashboard / "sd_dashboard/static").mkdir(parents=True)
        (self.dashboard / "dashboard.sh").write_text("#!/bin/sh\n")
        (self.dashboard / "sd_dashboard/server.py").write_text("# server\n")
        (self.dashboard / "sd_dashboard/bootstrap.py").write_text("# bootstrap\n")
        (self.dashboard / "sd_dashboard/static/page.css").write_text("body {}\n")

    def digests(self):
        return runtime.build_digests(library_root=self.library, dashboard_root=self.dashboard, system_root=self.system)

    def test_enumerates_added_package_data_static_assets_and_launcher(self):
        before = self.digests()
        additions = ((self.library / "future-migration.sql", "library_digest"),
                     (self.library / "new-module.py", "library_digest"),
                     (self.dashboard / "sd_dashboard/static/new.svg", "dashboard_digest"))
        for path, key in additions:
            previous = self.digests()
            path.write_text("new file\n")
            self.assertNotEqual(self.digests()[key], previous[key])
        for path in (self.dashboard / "dashboard.sh", self.dashboard / "sd_dashboard/bootstrap.py"):
            previous = self.digests()
            path.write_text(path.read_text() + "# changed\n")
            self.assertNotEqual(self.digests()["dashboard_digest"], previous["dashboard_digest"])
        self.assertNotEqual(self.digests(), before)

    def test_an_absent_launcher_is_recorded_rather_than_refused(self):
        """An installed copy has no `dashboard.sh` beside it.

        Requiring one meant `server.build` raised RuntimeRefused from
        site-packages, so the package could be installed but not started.
        """
        before = self.digests()
        (self.dashboard / "dashboard.sh").unlink()
        after = self.digests()
        self.assertNotEqual(after["dashboard_digest"], before["dashboard_digest"])

    def test_a_launcher_that_returns_changes_the_identity_back(self):
        """Absent is a recorded state, not a skipped one: removing the launcher
        and restoring identical bytes must return the same digest."""
        before = self.digests()
        body = (self.dashboard / "dashboard.sh").read_text()
        (self.dashboard / "dashboard.sh").unlink()
        without = self.digests()
        (self.dashboard / "dashboard.sh").write_text(body)
        self.assertEqual(self.digests(), before)
        self.assertNotEqual(without, before)

    def test_ignores_generated_bytecode_and_rejects_linked_sources(self):
        before = self.digests()
        (self.library / "__pycache__").mkdir()
        (self.library / "__pycache__/generated.pyc").write_bytes(b"bytecode")
        (self.dashboard / "sd_dashboard/generated.pyo").write_bytes(b"bytecode")
        self.assertEqual(self.digests(), before)
        (self.library / "linked.py").symlink_to(self.dashboard / "sd_dashboard/server.py")
        with self.assertRaisesRegex(runtime.RuntimeRefused, "regular file"):
            self.digests()

    def test_collector_dependency_addition_replacement_and_removal_change_build_identity(self):
        before = self.digests()  # Minimal fixtures intentionally omit collectors.
        for root, relative in ((self.dashboard, "collectors.py"), (self.system, "local-machine-setup/machine-setup.sh"),
                               (self.system, "local-mail-intake/mail_intake.py")):
            with self.subTest(dependency=relative):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# collector v1\n")
                added = self.digests()
                self.assertNotEqual(added["dashboard_digest"], before["dashboard_digest"])
                self.assertEqual(added["library_digest"], before["library_digest"])
                path.write_text("# collector v2\n")
                self.assertNotEqual(self.digests()["dashboard_digest"], added["dashboard_digest"])
                path.unlink()
                self.assertEqual(self.digests(), before)

    def test_a_pack_bin_module_the_queue_imports_changes_the_library_identity(self):
        # sd:TBD: the Queue imports `sd_lane` from the pack's bin/, so a pack update to it must restart the server.
        from sd_dashboard import PACK_MODULES

        library = self.root / "pack" / "lib" / "sd_db"
        library.mkdir(parents=True)
        (library / "__init__.py").write_text("# package\n")
        def digests():
            return runtime.build_digests(library_root=library, dashboard_root=self.dashboard, system_root=self.system)
        before = digests()
        for name in PACK_MODULES:
            with self.subTest(module=name):
                path = self.root / "pack" / "bin" / f"{name}.py"
                path.parent.mkdir(exist_ok=True)
                path.write_text("# v1\n")
                added = digests()
                self.assertNotEqual(added["library_digest"], before["library_digest"])
                self.assertEqual(added["dashboard_digest"], before["dashboard_digest"])
                path.write_text("# v2\n")
                self.assertNotEqual(digests()["library_digest"], added["library_digest"])
                path.unlink()
                self.assertEqual(digests(), before)

    def test_the_pack_modules_name_every_bin_module_sd_lane_and_sd_research_publish_import(self):
        # A pin that gives `sd_lane` or `sd_research_publish` (sd:5102) another bin/ import fails here, before a server
        # misses its change.
        from sd_dashboard import PACK_MODULES, pack_bin

        folder = pack_bin()
        probe = ("import json, sys; sys.path[:0] = [sys.argv[2]]; sys.path.append(sys.argv[1]); import sd_lane, sd_research_publish; "
                 "print(json.dumps(sorted(name for name, module in sys.modules.items() "
                 "if (getattr(module, '__file__', None) or '').startswith(sys.argv[1] + '/'))))")
        done = subprocess.run([sys.executable, "-I", "-c", probe, str(folder), str(folder.parent / "lib")],
                              capture_output=True, text=True, check=True, cwd=self.root)
        self.assertEqual(json.loads(done.stdout), sorted(PACK_MODULES))

    def test_health_keeps_startup_digests_and_refuses_changed_disk_build(self):
        from sd_dashboard import server
        from sd_db.migrate import initialise

        database = self.root / "sd.db"
        initialise(database)
        original = self.digests()
        with patch.object(runtime, "build_digests", return_value=original):
            listening = server.build(database, port=0)
        thread = threading.Thread(target=listening.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(listening.server_close)
        self.addCleanup(listening.shutdown)
        def health():
            client = http.client.HTTPConnection(*listening.server_address[:2], timeout=5)
            try:
                client.request("GET", "/health")
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()
        with patch.object(runtime, "build_digests", return_value=original):
            status, body = health()
            self.assertEqual(status, 200)
            self.assertFalse(body["code_changed"])
            self.assertEqual((body["pid"], body["ppid"]), (os.getpid(), os.getppid()))
        changed = dict(original, library_digest="0" * 64)
        with patch.object(runtime, "build_digests", return_value=changed):
            status, body = health()
            self.assertEqual(status, 503)
            self.assertFalse(body["ok"])
            self.assertTrue(body["code_changed"])
            self.assertEqual(body["library_digest"], original["library_digest"])
            self.assertEqual(body["dashboard_digest"], original["dashboard_digest"])

    def test_health_reports_the_commit_the_process_started_on(self):
        """sd:3528: /health names the commit captured at start, not the checkout's HEAD now."""
        from sd_dashboard import server
        from sd_db.migrate import initialise

        database = self.root / "sd.db"
        initialise(database)
        original = self.digests()
        with patch.object(runtime, "build_digests", return_value=original), \
                patch.object(runtime, "checkout_commit", return_value="a" * 40):
            listening = server.build(database, port=0)
        thread = threading.Thread(target=listening.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(listening.server_close)
        self.addCleanup(listening.shutdown)
        client = http.client.HTTPConnection(*listening.server_address[:2], timeout=5)
        try:
            with patch.object(runtime, "build_digests", return_value=original), \
                    patch.object(runtime, "checkout_commit", return_value="b" * 40):
                client.request("GET", "/health")
                response = client.getresponse()
                body = json.loads(response.read())
        finally:
            client.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["commit"], "a" * 40)

    def test_a_commit_that_cannot_be_read_still_starts_the_server(self):
        from sd_dashboard import server
        from sd_db.migrate import initialise

        database = self.root / "sd.db"
        initialise(database)
        with patch.object(runtime, "build_digests", return_value=self.digests()), \
                patch.object(runtime, "checkout_commit", side_effect=runtime.RuntimeRefused("git failed: locked")):
            listening = server.build(database, port=0, bind=False)
        self.addCleanup(listening.server_close)
        self.assertIsNone(listening.RequestHandlerClass.served_commit)

    def serve_changed_build(self, probe, **disk):
        """Start a server on this build with `probe` as its start check, then change the build on disk (sd:3018).

        `disk` patches what `build_digests` reads next; by default a changed dashboard digest.
        """
        from sd_dashboard import server
        from sd_db.migrate import initialise

        database = self.root / "sd.db"
        initialise(database)
        original = self.digests()
        with patch.object(runtime, "build_digests", return_value=original):
            listening = server.build(database, port=0)
        self.addCleanup(listening.server_close)
        listening.build_check_seconds = 0
        listening.restart_probe = probe
        ended = []
        def serve():
            try:
                listening.serve_forever(poll_interval=0.01)
            except BaseException as error:  # noqa: BLE001 - the test reads what ended it
                ended.append(error)
        changed = patch.object(runtime, "build_digests", **(disk or {"return_value": dict(original, dashboard_digest="0" * 64)}))
        changed.start()
        self.addCleanup(changed.stop)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return listening, thread, ended

    def test_server_stops_when_the_disk_build_changes_and_launchd_restarts_it(self):
        """sd:3018: a merge that moves the checkout stops the server, so KeepAlive starts the new code."""
        from sd_dashboard import server

        plist = plistlib.loads(runtime._plist(self.root / "dashboard.json", {"port": 8767}, self.root))
        self.assertIs(plist["KeepAlive"], True)
        listening, thread, ended = self.serve_changed_build([sys.executable, "-c", ""])
        thread.join(5)
        if thread.is_alive():
            listening.shutdown()
            self.fail("the server kept serving a build that changed on disk")
        self.assertIsInstance(ended[0], server.CodeChanged)

    def test_a_changed_build_that_cannot_start_leaves_the_old_server_serving(self):
        """sd:3018: a merge that also moves the pack's lib/sd_db leaves the installed library behind.

        The replacement would fail `installed_library`, and launchd would restart it in a loop. The
        probe here stands in for the new checkout's start refusing that lag. The old server keeps
        serving, says why once, and retries when the build changes again, as the pack's make setup does.
        """
        from sd_dashboard import server

        runs = self.root / "probe-runs"
        lag = ("dashboard: installed sd_db 1111111 lacks the checkout's library commit 2222222; "
               "run make setup in the pack, then start the dashboard")
        refusing = [sys.executable, "-c", "import sys; open(sys.argv[1], 'a').write('run\\n'); sys.exit(sys.argv[2])",
                    str(runs), lag]
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            listening, thread, ended = self.serve_changed_build(refusing)
            thread.join(1)
            alive = thread.is_alive()
            if alive:
                listening.restart_probe = [sys.executable, "-c", ""]
                with patch.object(runtime, "build_digests", return_value=dict(self.digests(), library_digest="1" * 64)):
                    thread.join(5)
            if thread.is_alive():
                listening.shutdown()
        self.assertTrue(alive, f"the server stopped for a build that cannot start: {ended}")
        self.assertEqual(runs.read_text(), "run\n")
        self.assertIn("serving the old build: " + lag, stderr.getvalue())
        self.assertFalse(thread.is_alive(), "a later build change did not retry the start check")
        self.assertIsInstance(ended[0], server.CodeChanged)

    def assert_keeps_serving(self, probe, logged, **disk):
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            listening, thread, ended = self.serve_changed_build(probe, **disk)
            thread.join(0.5)
            alive = thread.is_alive()
            if alive:
                listening.shutdown()
        self.assertTrue(alive, f"the server stopped: {ended}")
        self.assertEqual("serving the old build: " in stderr.getvalue(), logged)

    def test_a_checkout_mid_update_keeps_the_old_server_serving_without_a_probe(self):
        """sd:3018: a build that cannot be fingerprinted yet is checked again later, not probed."""
        runs = self.root / "probe-runs"
        self.assert_keeps_serving([sys.executable, "-c", "import sys; open(sys.argv[1], 'a').write('run\\n')", str(runs)],
                                  False, side_effect=runtime.RuntimeRefused("runtime build requires a regular file"))
        self.assertFalse(runs.exists(), "the probe ran on a build it could not fingerprint")

    def test_a_probe_that_cannot_run_keeps_the_old_server_serving(self):
        """sd:3018: a probe that cannot even start is a failed start check."""
        self.assert_keeps_serving([str(self.root / "missing-interpreter")], True)

class PackLibrary(unittest.TestCase):
    """`installed_library` accepts only the command pack checkout's own `lib/sd_db` (sd:3279).

    The pack owns the library (sd:2997) and its step 3 uninstalls the venv copy, so the
    dashboard reads the lib/ of the pack checkout it sits in (sd:5987) and proves it did. A copy in site-packages, a
    pack-built one included, is refused: on 2026-10-10 a service that ran another copy
    than the pack's made the hub refuse every satellite write for two hours.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dashboard-pack-library-")
        self.addCleanup(temporary.cleanup)
        # Resolved: macOS links /var to /private/var, and the check compares resolved paths.
        self.home = Path(temporary.name).resolve()
        self.pack = self.home / "pack"
        (self.pack / "lib/sd_db").mkdir(parents=True)
        self.source = self.pack / "lib/sd_db/__init__.py"
        self.source.write_text("# the pack's library\n")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "fixture@example.test")
        self.git("config", "user.name", "Fixture")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "library")
        self.commit = self.git("rev-parse", "HEAD")
        # `build_digests` reads the dashboard package from HERE too.
        (self.home / "dashboard/sd_dashboard").mkdir(parents=True)
        (self.home / "dashboard/sd_dashboard/__init__.py").write_text("")
        self.database = self.home / "sd.db"
        from sd_db.migrate import initialise
        initialise(self.database)
        self.addCleanup(patch.stopall)
        patch.object(runtime, "HERE", self.home / "dashboard").start()
        patch.object(runtime, "pack_library", return_value=self.pack / "lib").start()

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.pack), *arguments], check=True, capture_output=True, text=True).stdout.strip()

    def check(self, source):
        import sd_db
        with patch.object(sd_db, "__file__", str(source)):
            return runtime.installed_library(self.database)

    def copy_at(self, folder):
        package = self.home / folder / "sd_db"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("# another copy\n")
        return package / "__init__.py"

    def test_the_packs_lib_passes_and_names_the_packs_commit(self):
        result = self.check(self.source)
        self.assertEqual(result["library"], str(self.source.resolve()))
        self.assertEqual(result["library_commit"], self.commit)

    def test_a_pack_built_install_in_site_packages_is_refused(self):
        # What `make setup` installs into the pack's .venv until step 3 (sd:3280).
        prefix = self.home / "venv"
        installed = self.copy_at("venv/lib/python3.13/site-packages")
        with patch.object(sys, "prefix", str(prefix)), \
                self.assertRaisesRegex(runtime.RuntimeRefused, "the command pack's lib/") as refused:
            self.check(installed)
        self.assertIn(str(self.pack / "lib"), str(refused.exception))
        self.assertIn(str(installed), str(refused.exception))

    def test_a_source_copy_and_another_packs_lib_are_refused(self):
        for folder in ("system/sd_db", "other-pack/lib"):
            with self.subTest(folder=folder), self.assertRaisesRegex(runtime.RuntimeRefused, "the command pack's lib/"):
                self.check(self.copy_at(folder))

    def test_a_pack_that_is_not_a_git_checkout_passes_with_no_commit(self):
        shutil.rmtree(self.pack / ".git")
        self.assertIsNone(self.check(self.source)["library_commit"])
        with patch("shutil.which", return_value=None):
            self.assertIsNone(self.check(self.source)["library_commit"])

    def test_a_git_that_fails_is_refused_not_read_as_no_commit(self):
        failed = subprocess.CompletedProcess([], 128, "", "fatal: unreadable index")
        with patch.object(runtime, "_run", return_value=failed), \
                self.assertRaisesRegex(runtime.RuntimeRefused, "unreadable index"):
            self.check(self.source)

    def test_the_pack_is_the_checkout_the_dashboard_sits_in_whatever_sd_pack_root_says(self):
        # sd:5987: the dashboard and the library it serves come from one checkout.
        from sd_dashboard import pack_library
        with patch.dict(os.environ, {"SD_PACK_ROOT": str(self.pack)}):
            self.assertEqual(pack_library(), REAL_HERE.parent / "lib")

    def test_the_launcher_imports_sd_db_from_the_packs_lib(self):
        # The real dashboard.sh and bootstrap under -I, with an interpreter that holds an
        # installed sd_db (the gate's venv does): the pack's lib/ must answer first.
        marker = self.home / "pack-library-ran"
        self.source.write_text(f"open({str(marker)!r}, 'w').write(__file__)\n"
                               "raise ImportError('the fixture pack library loaded')\n")
        config = self.home / "dashboard.json"
        config.write_text(json.dumps({"port": 8787, "database": str(self.database)}))
        # A copy of the launcher and the package inside the fixture pack: the launcher reads the lib/ beside it.
        copy = self.pack / "dashboard"
        shutil.copytree(REAL_HERE / "sd_dashboard", copy / "sd_dashboard", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(REAL_HERE / "dashboard.sh", copy / "dashboard.sh")
        result = subprocess.run([str(copy / "dashboard.sh"), "preflight", "--config", str(config)],
                                env={**os.environ, "SD_DASHBOARD_PYTHON": sys.executable, "HOME": str(self.home)},
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("the fixture pack library loaded", result.stderr)
        self.assertEqual(marker.read_text(), str(self.source))

    def test_the_restart_probe_imports_sd_db_from_the_packs_lib(self):
        from unittest.mock import Mock

        from sd_dashboard import server
        marker = self.home / "probe-library-ran"
        self.source.write_text(f"open({str(marker)!r}, 'w').write(__file__)\n"
                               "raise ImportError('the fixture pack library loaded')\n")
        config = self.home / "dashboard.json"
        config.write_text(json.dumps({"port": 8787}))
        root = self.home / "dashboard"
        (root / "dashboard.sh").write_text("#!/bin/sh\nexit 0\n")
        (root / "dashboard.sh").chmod(0o755)
        (root / "sd_dashboard/server.py").write_text("import sd_db\n")
        listening = Mock(server_address=("127.0.0.1", 8787))
        with patch.object(runtime, "installed_library"), patch.object(server, "build", return_value=listening):
            server.main(["--config", str(config), "--port", "8787"])
        probe = subprocess.run(listening.restart_probe, capture_output=True, text=True, timeout=60,
                               stdin=subprocess.DEVNULL, check=False)
        self.assertIn("the fixture pack library loaded", probe.stderr)
        self.assertEqual(marker.read_text(), str(self.source))


class LaunchEnvironment(unittest.TestCase):
    """The LaunchAgent carries the installing shell's personal settings."""

    def test_set_values_pass_through_and_unset_ones_stay_out(self):
        values = {"OBSIDIAN_VAULT": "/vaults/My Vault", "REPO_ROOT": "/checkouts"}
        with patch.dict(os.environ, values, clear=False):
            os.environ.pop("SYSTEM_TOOLS_LABEL_PREFIX", None)
            environment = runtime._launch_environment()
        self.assertEqual(environment["OBSIDIAN_VAULT"], "/vaults/My Vault")
        self.assertEqual(environment["REPO_ROOT"], "/checkouts")
        self.assertNotIn("SYSTEM_TOOLS_LABEL_PREFIX", environment)
        self.assertEqual(environment["SD_DASHBOARD_PYTHON"], sys.executable)

    def test_the_config_directory_passes_through(self):
        values = {"SYSTEM_TOOLS_CONFIG": "/config/system", "CRON_JOBS_EXTRA_DIRS": "/more/jobs"}
        with patch.dict(os.environ, values, clear=False):
            environment = runtime._launch_environment()
        self.assertEqual(environment["SYSTEM_TOOLS_CONFIG"], "/config/system")
        self.assertEqual(environment["CRON_JOBS_EXTRA_DIRS"], "/more/jobs")

    def test_the_job_triage_stage_switch_passes_through(self):
        # Only the installing shell or <config>/project-dashboard/.env can switch
        # the failed-job Jev shadow off for the server (sd:2095).
        with patch.dict(os.environ, {"JEV_JOB_TRIAGE": "off"}, clear=False):
            environment = runtime._launch_environment()
        self.assertEqual(environment["JEV_JOB_TRIAGE"], "off")

    def test_the_system_root_passes_through(self):
        # sd:5987: the agent runs the system tools from the checkout the installing shell named.
        with patch.dict(os.environ, {"SYSTEM_TOOLS_ROOT": "/checkouts/system"}, clear=False):
            environment = runtime._launch_environment()
        self.assertEqual(environment["SYSTEM_TOOLS_ROOT"], "/checkouts/system")


class QuickNoteEnvironment(unittest.TestCase):
    """sd:2549: Notes runs `sd store`, so the LaunchAgent gets sd's directory and OBSIDIAN_VAULT from the installing shell."""

    def which(self, found):
        real = shutil.which
        return patch.object(runtime.shutil, "which", side_effect=lambda name, path=None: found if name == "sd" else real(name, path=path))

    def test_sd_s_directory_joins_the_path_and_the_vault_passes_through(self):
        with self.which("/opt/example/bin/sd"), patch.dict(os.environ, {"OBSIDIAN_VAULT": "/vaults/Example"}):
            environment = runtime._launch_environment()
        self.assertEqual(environment["PATH"], runtime.LAUNCH_PATH + ":/opt/example/bin")
        self.assertEqual(environment["OBSIDIAN_VAULT"], "/vaults/Example")

    def test_without_them_the_path_is_unchanged_and_each_gap_is_named(self):
        with self.which(None), patch.dict(os.environ, {}):
            os.environ.pop("OBSIDIAN_VAULT", None)
            environment = runtime._launch_environment()
            gaps = runtime.launch_gaps(environment)
        self.assertEqual(environment["PATH"], runtime.LAUNCH_PATH)
        self.assertNotIn("OBSIDIAN_VAULT", environment)
        self.assertEqual(gaps, ["sd is not on the installing shell's PATH", "OBSIDIAN_VAULT is not set"])

class HealthWait(unittest.TestCase):
    """A clock bounds the wait: a refused connection returns at once (sd:2811)."""

    def poll(self, refusals, **wait):
        now, tries = [0.0], []

        class Response:
            status = 200

            def read(self):
                return b'{"ok": true, "service": "sd-dashboard"}'

        class Connection:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args):
                tries.append(now[0])
                if len(tries) <= refusals:
                    raise ConnectionRefusedError

            def getresponse(self):
                return Response()

            def close(self):
                pass

        def sleep(seconds):
            now[0] += seconds

        with patch.object(runtime.http.client, "HTTPConnection", Connection), \
                patch.object(runtime.time, "monotonic", lambda: now[0]), \
                patch.object(runtime.time, "sleep", sleep):
            try:
                return runtime.health_check(8767, **wait), tries
            except runtime.RuntimeRefused as refusal:
                return refusal, tries

    def test_a_start_slower_than_twenty_refusals_is_still_healthy(self):
        result, tries = self.poll(40)
        self.assertEqual(result, {"ok": True, "service": "sd-dashboard"})
        self.assertEqual(len(tries), 41)

    def test_the_wait_ends_on_the_clock(self):
        result, tries = self.poll(10_000)
        self.assertIn("did not return its healthy /health response within 30s", str(result))
        self.assertGreaterEqual(tries[-1], 30)
        self.assertLess(tries[-1], 30.5)

    def test_health_refuses_a_wait_that_never_ends(self):
        for value in ("nan", "inf", "-1"):
            with self.subTest(wait=value), patch.object(sys, "argv", ["runtime", "health", "--wait", value]), \
                    patch.object(runtime, "health_check") as health, patch("sys.stderr"):
                with self.assertRaises(SystemExit) as exit:
                    runtime.main()
                self.assertEqual(exit.exception.code, 2)
                health.assert_not_called()

    def test_no_wait_tries_once(self):
        result, tries = self.poll(1, wait=0)
        self.assertIsInstance(result, runtime.RuntimeRefused)
        self.assertEqual(tries, [0.0])


class ServedCommit(unittest.TestCase):
    """sd:3528: `dashboard.sh health` names the served commit and fails when it is not the checkout's."""

    SERVED = {"ok": True, "service": "sd-dashboard", "pid": 4242}

    def health(self, served, checkout, plist=None):
        """`runtime health` against a healthy server that answers `served`, under a HOME of its own whose installed
        LaunchAgent is `plist` (bytes, or none installed); (exit code, stdout, stderr)."""
        temporary = tempfile.TemporaryDirectory(prefix="dashboard-commit-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        if plist is not None:
            (self.home / "Library/LaunchAgents").mkdir(parents=True)
            (self.home / "Library/LaunchAgents" / f"{runtime.LABEL}.plist").write_bytes(plist)
        config = Path(temporary.name) / "dashboard.json"
        config.write_text(json.dumps({"port": 8767}))
        answer = dict(self.SERVED, **({} if served is ... else {"commit": served}))
        checkout = {"side_effect": checkout} if isinstance(checkout, Exception) else {"return_value": checkout}
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["runtime", "health", "--config", str(config)]), \
                patch.object(runtime, "health_check", return_value=answer), \
                patch.object(runtime, "checkout_commit", create=True, **checkout), \
                patch.dict(os.environ, {"HOME": temporary.name}), \
                patch("sys.stdout", out), patch("sys.stderr", err):
            code = runtime.main()
        return code, out.getvalue(), err.getvalue()

    def agent(self, folder):
        """An installed LaunchAgent whose PATH is `folder`, as install wrote it before sd moved."""
        return plistlib.dumps({"Label": runtime.LABEL, "EnvironmentVariables": {"PATH": f"/usr/bin:/bin:{folder}"}})

    def test_an_installed_path_that_no_longer_finds_sd_fails_naming_the_reinstall(self):
        """sd:5288: install froze sd's folder into the agent's PATH; sd moved, Notes lost it, and health stayed green."""
        old, new = (Path(tempfile.mkdtemp(prefix=f"dashboard-{name}-")) for name in ("old", "new"))
        self.addCleanup(shutil.rmtree, old)
        self.addCleanup(shutil.rmtree, new)
        (new / "sd").write_text("#!/bin/sh\n")
        (new / "sd").chmod(0o755)
        code, out, err = self.health("a" * 40, "a" * 40, self.agent(old))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("installed LaunchAgent's PATH no longer finds sd", err)
        self.assertIn("dashboard.sh install --apply", err)
        code, _, err = self.health("a" * 40, "a" * 40, self.agent(new))
        self.assertEqual((code, err), (0, ""))

    def test_an_installed_agent_that_does_not_read_fails_too(self):
        """A plist health cannot read is not a pass: its PATH is unknown."""
        code, _, err = self.health("a" * 40, "a" * 40, b"not a plist")
        self.assertEqual(code, 1)
        self.assertIn("does not read", err)

    def test_no_config_exits_3_naming_no_dashboard_not_errno(self):
        """sd:9920: a machine with no dashboard.json says so, exit 3, not an Errno 2 line."""
        temporary = tempfile.TemporaryDirectory(prefix="dashboard-none-")
        self.addCleanup(temporary.cleanup)
        missing = Path(temporary.name) / "dashboard.json"
        for target in (missing, Path(temporary.name) / "link.json"):
            if target.name == "link.json":
                target.symlink_to(missing)
            err = io.StringIO()
            with patch.object(sys, "argv", ["runtime", "health", "--config", str(target)]), \
                    patch.object(runtime, "health_check") as probe, \
                    patch("sys.stdout", io.StringIO()), patch("sys.stderr", err):
                code = runtime.main()
            self.assertEqual(code, 3)
            self.assertIn("no dashboard on this machine", err.getvalue())
            self.assertNotIn("Errno", err.getvalue())
            probe.assert_not_called()

    def test_no_config_and_no_runtime_exits_3_from_the_shell(self):
        """sd:9916: a satellite has neither the server's runtime nor a config: exit 3, not 1."""
        temporary = tempfile.TemporaryDirectory(prefix="dashboard-satellite-")
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        script = str(REAL_HERE / "dashboard.sh")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
               "SD_DASHBOARD_PYTHON": str(home / "no-runtime")}
        for extra in ([], ["--config", str(home / "other.json")], [f"--config={home / 'other.json'}"]):
            result = subprocess.run(["/bin/bash", script, "health", *extra], env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 3, (extra, result.stderr))
            self.assertIn("no dashboard on this machine", result.stderr)
        # A config that exists with no runtime is still broken: exit 1.
        present = home / "dashboard.json"
        present.write_text("{}", encoding="utf-8")
        result = subprocess.run(["/bin/bash", script, "health", "--config", str(present)], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("missing installed runtime", result.stderr)

    def test_the_checkout_commit_passes_and_is_shown(self):
        code, out, err = self.health("a" * 40, "a" * 40)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["commit"], "a" * 40)

    def test_a_server_behind_the_checkout_fails_naming_both_and_the_kickstart(self):
        code, out, err = self.health("a" * 40, "b" * 40)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"served {'a' * 40}, checkout {'b' * 40}", err)
        self.assertIn(f"launchctl kickstart -k gui/{os.getuid()}/{runtime.LABEL}", err)

    def test_a_server_that_names_no_commit_is_not_current(self):
        """A process from before sd:3528 answers no commit: unknown is not a pass."""
        code, _, err = self.health(..., "b" * 40)
        self.assertEqual(code, 1)
        self.assertIn(f"served unknown, checkout {'b' * 40}", err)

    def test_a_checkout_commit_that_cannot_be_read_fails(self):
        code, _, err = self.health("a" * 40, runtime.RuntimeRefused("git failed: index.lock"))
        self.assertEqual(code, 1)
        self.assertIn("git failed: index.lock", err)

    def test_an_installed_copy_outside_any_checkout_passes_on_its_digests(self):
        """No git checkout on either side: nothing to compare, and /health's digests still judge the build."""
        code, out, err = self.health(None, None)
        self.assertEqual((code, err), (0, ""))
        self.assertIsNone(json.loads(out)["commit"])

    def test_checkout_commit_reads_this_checkout(self):
        expected = subprocess.run(["git", "-C", str(REAL_HERE), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(runtime.checkout_commit(), expected)

    def test_an_unhealthy_answer_names_the_commit_it_serves(self):
        """A stale process answers 503 with code_changed; the refusal says what it serves."""
        body = json.dumps({"ok": False, "service": "sd-dashboard", "code_changed": True, "commit": "a" * 40}).encode()

        class Response:
            status = 503

            def read(self):
                return body

        class Connection:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                pass

        with patch.object(runtime.http.client, "HTTPConnection", Connection):
            with self.assertRaises(runtime.RuntimeRefused) as refusal:
                runtime.health_check(8767, wait=0)
        self.assertIn(f"it serves {'a' * 40} and its build changed on disk", str(refusal.exception))



class DetachedRunners(unittest.TestCase):
    """sd:5987 lane review: every runner the server starts as a file or with `-m` imports with no `PYTHONPATH` of
    its own and no installed `sd_db`, given `runner_environment()`, as the LaunchAgent's server starts it. A usage
    line on stderr means the module imported; a missing `sd_db` stops it before then."""

    FILES = ("issue_filing", "adversarial", "publish_drain", "research_deck")
    MODULES = ("hoa_download", "research_experiments", "realm_sweep")

    def clean(self, home):
        from sd_dashboard import runner_environment

        return runner_environment({"HOME": home, "PATH": "/usr/bin:/bin"})

    def test_the_environment_names_the_packs_library_and_nothing_else(self):
        from sd_dashboard import pack_library

        with tempfile.TemporaryDirectory() as home:
            found = subprocess.run([sys.executable, "-c", "import sd_db; print(sd_db.__file__)"], cwd=home,
                                   env=self.clean(home), capture_output=True, text=True, timeout=60)
            self.assertEqual(found.returncode, 0, found.stderr)
            self.assertEqual(Path(found.stdout.strip()).parent.parent, pack_library())
            self.assertEqual(self.clean(home)["PYTHONPATH"], str(pack_library()))

    def test_every_runner_imports_in_the_environment_it_is_given(self):
        with tempfile.TemporaryDirectory() as home:
            starts = [([sys.executable, str(runtime.HERE / "sd_dashboard" / f"{name}.py")], home) for name in self.FILES]
            starts += [([sys.executable, "-m", f"sd_dashboard.{name}"], str(runtime.HERE)) for name in self.MODULES]
            for argv, place in starts:
                with self.subTest(runner=argv[-1]):
                    done = subprocess.run(argv, cwd=place, env=self.clean(home), capture_output=True, text=True,
                                          timeout=60)
                    self.assertNotIn("ModuleNotFoundError", done.stderr)
                    self.assertIn("usage:", done.stderr)


if __name__ == "__main__":
    unittest.main()
