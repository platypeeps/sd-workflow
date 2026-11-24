"""The local CI gate (sd:1843): `sd-check` at the exact head, posted as `sd/local-gate`.

Real git and a real `sd-check` child against a throwaway repository; the
GitHub side is a recorder, so nothing leaves the machine.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_gate_cache  # noqa: E402
import sd_gate_receipts  # noqa: E402
import sd_gate_run  # noqa: E402
import sd_gate_slots  # noqa: E402
import sd_lib  # noqa: E402
import sd_local_gate  # noqa: E402
import sd_tmpdisk  # noqa: E402
from sd_ship_remote import Refusal  # noqa: E402

from tests.clean_env import clean_environment  # noqa: E402
from tests.row_seed import seed_declaration  # noqa: E402

BLOCK_START, BLOCK_END = sd_lib.LOCAL_BLOCK_START, sd_lib.LOCAL_BLOCK_END


def git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def running(pid: int) -> bool:
    """Alive and not a zombie waiting for its parent to reap it."""
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def started(path: pathlib.Path, count: int) -> list[int]:
    """The pids a stand-in check wrote, once there are `count` of them; whatever is there for 0."""
    deadline = time.monotonic() + 30
    while count and len(path.read_text().split() if path.is_file() else []) < count:
        assert time.monotonic() < deadline, "the stand-in check never recorded its pids"
        time.sleep(0.05)
    return [int(word) for word in path.read_text().split()] if path.is_file() else []


def stop(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


class Recorder:
    """The `GitHub` calls the gate makes, answered from memory; posts land as `me`'s statuses."""

    prefix = "repos/o/r"

    def __init__(self, statuses: list | None = None) -> None:
        self.statuses = statuses or []
        self.posts: list[tuple[str, dict]] = []

    def viewer_login(self) -> str:
        return "Me"

    def pages(self, path: str, field: str | None = None) -> list:
        return list(self.statuses)

    def api(self, path: str, *, method: str = "GET", body: dict | None = None) -> dict:
        assert method == "POST", method
        self.posts.append((path, body or {}))
        self.statuses.insert(0, {**(body or {}), "sha": path.rsplit("/", 1)[1], "creator": {"login": "Me"}})
        return {"state": (body or {}).get("state")}


class Repository(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name).resolve() / "repo"
        self.root.mkdir()
        # sd:2735: a fixture gate run outside `sd gate check` must not join the machine's real gate queue.
        state = {"SD_GATE_SLOTS_DIR": str(self.root.parent / "slots"), "XDG_STATE_HOME": str(self.root.parent / "state")}
        patch = mock.patch.dict(os.environ, state)
        patch.start()
        self.addCleanup(patch.stop)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "t")

    def declare_row(self, kind: str, value: dict) -> None:
        """The fixture repository's `kind` declaration on its row (sd:5109), in a scratch database this test reads."""
        home = self.root.parent / "home"
        seed_declaration(home, self.root, kind, value)
        patch = mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")})
        patch.start()
        self.addCleanup(patch.stop)

    def commit(self, makefile: str) -> str:
        (self.root / "Makefile").write_text(makefile, encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "c")
        return git(self.root, "rev-parse", "HEAD")

    def worktrees(self) -> int:
        return git(self.root, "worktree", "list", "--porcelain").count("worktree ")


class RunCheck(Repository):
    def test_a_fixture_gate_queues_in_its_own_folder_not_the_machines(self) -> None:
        """sd:2735: run directly, this suite's gates took and waited on the real slots under `~/.local/state`."""
        head = self.commit("check:\n\t@echo ok\n")
        with mock.patch.dict(os.environ, {"SD_GATE_LOAD_MAX": "0", "SD_GATE_SETTLE_SECONDS": "0"}):
            for name in ("SD_GATE_SLOTS", "CI", "GITHUB_ACTIONS"):  # each of these takes no slot at all
                os.environ.pop(name, None)
            self.assertEqual(sd_gate_run.check_in_worktree(self.root, head)["status"], "success")
        self.assertTrue((self.root.parent / "slots").is_dir())

    def test_a_passing_check_is_a_success_at_the_head_it_ran_on(self) -> None:
        head = self.commit("check:\n\t@echo ok\n")
        result = sd_gate_run.check_in_worktree(self.root, head)
        self.assertEqual((result["head"], result["status"]), (head, "success"))
        self.assertIn("check pass", result["summary"])

    def test_a_failing_check_is_a_failure(self) -> None:
        head = self.commit("check:\n\t@false\n")
        result = sd_gate_run.check_in_worktree(self.root, head)
        self.assertEqual(result["status"], "failure")
        self.assertIn("check fail", result["summary"])

    def test_the_check_runs_in_a_clean_worktree_not_the_operators_checkout(self) -> None:
        """An untracked file in the checkout that would fail the check is not seen."""
        head = self.commit("check:\n\t@test ! -e dirty.txt\n")
        (self.root / "dirty.txt").write_text("operator's uncommitted state\n", encoding="utf-8")
        self.assertEqual(sd_gate_run.check_in_worktree(self.root, head)["status"], "success")

    def test_the_named_commit_is_checked_not_the_checkouts_head(self) -> None:
        first = self.commit("check:\n\t@false\n")
        self.commit("check:\n\t@echo ok\n")
        result = sd_gate_run.check_in_worktree(self.root, first)
        self.assertEqual((result["head"], result["status"]), (first, "failure"))

    def test_a_pythonpath_at_the_root_does_not_reach_the_child(self) -> None:
        """An editable install pointed at the dirty checkout must not be importable in the gate."""
        head = self.commit('check:\n\t@test -z "$$PYTHONPATH" && test -z "$$VIRTUAL_ENV"\n')
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(self.root), "VIRTUAL_ENV": str(self.root / ".venv")}):
            self.assertEqual(sd_gate_run.check_in_worktree(self.root, head)["status"], "success")

    def test_the_environment_drops_package_selectors_and_path_entries_in_the_checkout(self) -> None:
        inside, outside = str(self.root / ".venv/bin"), "/usr/bin"
        env = sd_gate_run.gate_environment(self.root, {
            "PATH": os.pathsep.join([inside, outside, "relative/bin"]), "PYTHONPATH": str(self.root),
            "PYTHONHOME": "/x", "VIRTUAL_ENV": inside, "CONDA_PREFIX": "/c", "__PYVENV_LAUNCHER__": "/l", "HOME": "/h"})
        self.assertEqual(env, {"PATH": outside, "HOME": "/h", "SD_LOCAL_GATE": "1", "NO_COLOR": "1",
                               "PYTHON_COLORS": "0", **sd_lib.without_fsmonitor({})})

    def test_the_operators_colour_settings_do_not_reach_the_check(self) -> None:
        """`FORCE_COLOR=3` in a terminal failed a repository's gate on ANSI-coloured output (sd:2076)."""
        env = sd_gate_run.gate_environment(self.root, {
            "PATH": "/usr/bin", "FORCE_COLOR": "3", "CLICOLOR_FORCE": "1", "PY_COLORS": "1",
            "NO_COLOR": "", "PYTHON_COLORS": "1"})
        self.assertFalse({"FORCE_COLOR", "CLICOLOR_FORCE", "PY_COLORS"} & env.keys())
        self.assertEqual((env["NO_COLOR"], env["PYTHON_COLORS"]), ("1", "0"))

    def test_the_agent_harness_session_variables_do_not_reach_the_check(self) -> None:
        """sd:1912, operator ruling D1 (2026-10-03). Two sessions differ in these
        and nothing else, so their passes must bind equal; MAKEFLAGS still binds."""
        session = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "a", "CLAUDE_PID": "1", "HERDR_PANE_ID": "p1",
                   "ITERM_SESSION_ID": "i1", "TERM_SESSION_ID": "t1", "PWD": "/one", "OLDPWD": "/x",
                   "SHLVL": "1", "_": "/usr/bin/env"}
        other = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "b", "CLAUDE_EFFORT": "high", "HERDR_TAB_ID": "t2",
                 "ITERM_PROFILE": "x", "TERM_SESSION_ID": "t2", "PWD": "/two", "SHLVL": "3", "_": "/bin/sh"}
        base = {"PATH": "/usr/bin", "HOME": "/h"}
        first = sd_gate_run.gate_environment(self.root, {**base, **session})
        self.assertEqual(first, sd_gate_run.gate_environment(self.root, {**base, **other}))
        self.assertEqual(first, sd_gate_run.gate_environment(self.root, base))
        self.assertNotEqual(first, sd_gate_run.gate_environment(self.root, {**base, **session, "MAKEFLAGS": "-k"}))

    def test_the_check_is_told_it_is_the_gate(self) -> None:
        """`SD_LOCAL_GATE=1` is the contract a repository reads to provision instead of borrow (sd:1918)."""
        head = self.commit('check:\n\t@test "$$SD_LOCAL_GATE" = 1\n')
        with mock.patch.dict(os.environ, {"SD_LOCAL_GATE": "0"}):
            self.assertEqual(sd_gate_run.check_in_worktree(self.root, head)["status"], "success")

    def test_a_virtualenv_bin_outside_the_checkout_is_dropped_from_path(self) -> None:
        """A venv's `bin` is found by the `pyvenv.cfg` beside it, wherever it lives."""
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        venv, plain = pathlib.Path(outside.name) / "venv", pathlib.Path(outside.name) / "tools"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        plain.mkdir()
        env = sd_gate_run.gate_environment(self.root, {"PATH": os.pathsep.join([str(venv / "bin"), str(plain)])})
        self.assertEqual(env["PATH"], str(plain.resolve()))  # each kept entry resolved (sd:2602)

    def test_a_path_entry_that_names_no_folder_is_dropped(self) -> None:
        """sd:2772. fnm's `cd` hook prepends a per-shell link, here to no installation, so
        `cd <checkout> && sd gate check` bound another environment than `sd-review -C <checkout>`
        and the review ran the whole check again. A folder that does not exist selects no tool."""
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        shell = pathlib.Path(outside.name) / "fnm_multishells" / "3315_1791163917161"
        shell.parent.mkdir()
        shell.symlink_to(pathlib.Path(outside.name) / "no-installation")
        plain = {"PATH": "/usr/bin", "HOME": "/h"}
        after_cd = {**plain, "PATH": os.pathsep.join([str(shell / "bin"), "/usr/bin"])}
        self.assertEqual(sd_gate_run.gate_environment(self.root, after_cd), sd_gate_run.gate_environment(self.root, plain))

    def test_the_gates_bound_is_the_per_check_timeout_sd_check_reports(self) -> None:
        """`sd-check`'s own 900 s default must not cut a gate run short; the gate's bound reaches it."""
        head = self.commit("check:\n\t@sleep 5\n")
        result = sd_gate_run.check_in_worktree(self.root, head, timeout=1)
        self.assertEqual((result["status"], result["exit_code"]), ("failure", 1))
        self.assertIn("check fail", result["summary"])

    def test_the_result_keeps_the_full_sd_check_report(self) -> None:
        """The worktree is deleted after the run, so the receipt is the only place its output survives (sd:1872)."""
        head = self.commit("check:\n\t@echo gate-output-marker; false\n")
        result = sd_gate_run.check_in_worktree(self.root, head)
        [check] = [entry for entry in result["report"]["checks"] if entry["name"] == "check"]
        self.assertEqual((result["report"]["status"], check["exit_code"]), ("fail", 2))
        self.assertIn("gate-output-marker", check["stdout"])

    def test_a_configuration_fault_keeps_sd_checks_stderr(self) -> None:
        """Exit 2 prints no report; what is wrong is on sd-check's stderr, and the receipt keeps it."""
        head = self.commit("check:\n\t@echo ok\n")
        (self.root / "CLAUDE.local.md").write_text(
            "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\ncheck: 'unterminated\n<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n",
            encoding="utf-8")
        result = sd_gate_run.check_in_worktree(self.root, head)
        self.assertEqual((result["status"], result["exit_code"], result["report"]), ("failure", 2, None))
        self.assertIn("does not parse", result["stderr"])

    def test_the_worktree_is_removed_after_the_run(self) -> None:
        head = self.commit("check:\n\t@echo ok\n")
        before = self.worktrees()
        sd_gate_run.check_in_worktree(self.root, head)
        self.assertEqual(self.worktrees(), before)

    def test_a_repositorys_fsmonitor_is_never_consulted_in_the_gates_worktree(self) -> None:
        """sd:2993: `core.fsmonitor=true` started a daemon on the fresh worktree's first index read, past git's 60 s.
        A hook path stands in for the daemon: git runs it wherever it reads the setting, the check's own git included."""
        marker, hook = self.root.parent / "fsmonitor-ran", self.root.parent / "fsmonitor.sh"
        hook.write_text(f'#!/bin/sh\necho "$PWD $*" >> "{marker}"\nexit 1\n', encoding="utf-8")
        hook.chmod(0o755)
        # ls-files-form: plain -- the check command only needs git to read the index; the fsmonitor hook is what this case tests
        head = self.commit("check:\n\t@git status --porcelain && git ls-files > /dev/null\n")
        git(self.root, "config", "core.fsmonitor", str(hook))
        self.assertEqual(sd_gate_run.check_in_worktree(self.root, head)["status"], "success")
        self.assertFalse(marker.exists(), marker.read_text(encoding="utf-8") if marker.exists() else "")

    def test_stopping_the_gate_stops_its_check_and_frees_the_slot(self) -> None:
        """sd:2978: a stopped `sd gate check` left its `sd-check` running, holding a slot on a superseded head.
        The stand-in check holds a lock as a slot holder does, and starts a group of its own as `sd-check` runs `make`."""
        pids, slot = self.root.parent / "pids", self.root.parent / "slot.lock"
        check = self.root.parent / "check.py"
        check.write_text(textwrap.dedent(f"""\
            import fcntl, os, pathlib, sys
            sys.path.insert(0, {str(REPO_ROOT / "bin")!r})
            import sd_lib
            held = os.open({str(slot)!r}, os.O_RDWR | os.O_CREAT)
            fcntl.flock(held, fcntl.LOCK_EX)
            pathlib.Path({str(pids)!r}).write_text(f"{{os.getpid()}} ")
            sd_lib.run_group(["sh", "-c", "echo $$ >> {pids}; exec sleep 60"], cwd=pathlib.Path("."), env={{"PATH": "/usr/bin:/bin"}}, timeout=60)
            """), encoding="utf-8")
        gate = textwrap.dedent(f"""\
            import pathlib, sys
            sys.path.insert(0, {str(REPO_ROOT / "bin")!r})
            import sd_gate_run
            sd_gate_run.run_child([sys.executable, {str(check)!r}], {{"PATH": "/usr/bin:/bin"}}, pathlib.Path("."), 120)
            """)
        parent = subprocess.Popen([sys.executable, "-c", gate], cwd=self.root, start_new_session=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: [stop(pid) for pid in [parent.pid, *started(pids, 0)]])
        found = started(pids, 2)
        os.kill(parent.pid, signal.SIGTERM)
        parent.wait(timeout=30)
        deadline = time.monotonic() + 10
        while (alive := [pid for pid in found if running(pid)]) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(alive, [], f"left running after the gate was stopped: {found}")
        with open(slot, "rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # BlockingIOError while the stopped check still holds it

    def test_a_slot_bound_reaches_sd_check_and_widens_the_childs_limit(self) -> None:
        """sd:2607: the queue for a slot has its own bound, so the child may live for both."""
        head = self.commit("check:\n\t@echo ok\n")
        seen: list[tuple[list[str], int]] = []
        def run(argv: list[str], env: dict[str, str], cwd: pathlib.Path, limit: int) -> tuple[int | None, str, str]:
            seen.append((argv, limit))
            return sd_gate_run.run_child(argv, env, cwd, limit)
        bounded_result = sd_gate_run.check_in_worktree(self.root, head, timeout=60, slot_timeout=300, run=run)
        self.assertEqual(bounded_result["report"]["gate_slot"]["bound_seconds"], 300)
        sd_gate_run.check_in_worktree(self.root, head, timeout=60, run=run)
        (bounded, wide), (plain, narrow) = seen
        self.assertEqual(bounded[bounded.index("--slot-timeout") + 1], "300")
        self.assertEqual(wide, 60 + 300 + sd_gate_run.REPORT_GRACE_SECONDS)
        self.assertNotIn("--slot-timeout", plain)
        self.assertEqual(narrow, 60 + sd_gate_run.REPORT_GRACE_SECONDS)


class Reading(unittest.TestCase):
    def test_only_an_exit_zero_pass_is_a_success(self) -> None:
        passed = '{"status": "pass", "checks": [{"name": "check", "status": "pass"}]}'
        self.assertEqual(sd_gate_run.check_reading(0, passed)["status"], "success")
        for code, output in ((0, '{"status": "absent", "checks": []}'), (0, '{"status": "skipped"}'),
                             (1, '{"status": "fail"}'), (2, ""), (None, "timed out"), (0, "not json")):
            with self.subTest(code=code, output=output):
                self.assertEqual(sd_gate_run.check_reading(code, output)["status"], "failure")


    def test_the_report_and_stderr_tail_are_kept_and_the_summary_stays_one_line(self) -> None:
        passed = '{"status": "pass", "checks": [{"name": "check", "status": "pass", "stdout": "ok"}]}'
        reading = sd_gate_run.check_reading(0, passed, "e" * 5000)
        self.assertEqual(reading["report"]["checks"][0]["stdout"], "ok")
        self.assertEqual(len(reading["stderr"]), sd_gate_run.STDERR_TAIL_CHARS)
        self.assertEqual(reading["summary"], "sd-check pass (check pass)")
        self.assertIsNone(sd_gate_run.check_reading(0, "not json")["report"])

    def test_a_child_past_its_deadline_keeps_what_it_wrote(self) -> None:
        """sd:3418: the timeout dropped `sd-check`'s output, so the reading could not name where it hung."""
        code, output, errors = sd_gate_run.run_child(
            ["sh", "-c", "echo 'shard x: start'; echo 'waiting for a slot' >&2; sleep 30"],
            {"PATH": "/usr/bin:/bin"}, pathlib.Path("."), 2)
        self.assertEqual((code, output), (None, "sd-check could not finish: sh: timed out after 2s"))
        self.assertEqual(errors, "shard x: start\nwaiting for a slot\n")
        reading = sd_gate_run.check_reading(code, output, errors)
        self.assertEqual(reading["summary"], output)
        self.assertIn("waiting for a slot", reading["stderr"])

    def test_a_failed_precheck_is_named_first(self) -> None:
        """sd:2604: the checks after a failed precheck did not run, so the summary names the precheck."""
        stopped = ('{"status": "fail", "precheck": {"name": "precheck", "status": "fail"},'
                   ' "checks": [{"name": "check", "status": "fail"}]}')
        self.assertEqual(sd_gate_run.check_reading(1, stopped)["summary"], "sd-check fail (precheck fail, check fail)")


    def test_a_failure_summary_names_the_failed_step(self) -> None:
        """sd:2608: the one line a lane log shows said `check fail` and no step."""
        failed = json.dumps({"status": "fail", "checks": [
            {"name": "check", "status": "fail", "failed_steps": ["shard tests.test_a: 0s exit=1", "make target test"]},
            {"name": "test", "status": "skipped"}, {"name": "lint", "status": "skipped"}]})
        summary = sd_gate_run.check_reading(1, failed)["summary"]
        self.assertEqual(summary, "sd-check fail (check fail: shard tests.test_a: 0s exit=1; make target test,"
                                  " test skipped, lint skipped)")
        kept = "/tmp/example/.git/sd-check-output/20261004T000000Z-1-check.log"
        many = json.dumps({"status": "fail", "checks": [
            {"name": "check", "status": "fail", "output_path": kept,
             "failed_steps": [f"shard tests.test_{n}: 0s exit=1" for n in range(30)]}]})
        steps, said = sd_gate_run.check_reading(1, many)["summary"].split(" whole output: ")
        # The steps keep a status description's bound; the path to the whole output is never cut.
        self.assertTrue(steps.startswith("sd-check fail (check fail: shard tests.test_0: 0s exit=1; "), steps)
        self.assertLessEqual(len(steps), sd_gate_run.DESCRIPTION_LIMIT)
        self.assertEqual(said, kept)


class Post(unittest.TestCase):
    HEAD = "a" * 40

    def test_success_is_posted_to_the_checked_head_under_the_context(self) -> None:
        api = Recorder()
        sd_local_gate.post_gate_status(api, self.HEAD, {"head": self.HEAD, "status": "success", "summary": "sd-check pass"},
                                       "0123456789ab")
        [(path, body)] = api.posts
        self.assertEqual(path, f"repos/o/r/statuses/{self.HEAD}")
        self.assertEqual((body["state"], body["context"]), ("success", "sd/local-gate"))
        self.assertTrue(body["description"].startswith(f"{self.HEAD[:12]} inputs 0123456789ab: "))
        self.assertLessEqual(len(body["description"]), 140)

    def test_a_result_for_another_sha_is_refused_and_nothing_is_posted(self) -> None:
        api = Recorder()
        with self.assertRaisesRegex(Refusal, "no status is posted for a commit that was not checked"):
            sd_local_gate.post_gate_status(api, self.HEAD, {"head": "b" * 40, "status": "success"}, "0" * 12)
        self.assertEqual(api.posts, [])

    def test_the_posted_description_leaves_out_the_local_output_path(self) -> None:
        """sd:2608. The summary names the file with the whole output for the
        lane log; a commit status is public and the path is a local one."""
        api = Recorder()
        summary = "sd-check fail (check fail: make target test) whole output: /tmp/example/.git/sd-check-output/run.log"
        sd_local_gate.post_gate_status(api, self.HEAD, {"head": self.HEAD, "status": "failure", "summary": summary},
                                       "0" * 12)
        self.assertEqual(api.posts[0][1]["description"],
                         f"{self.HEAD[:12]} inputs {'0' * 12}: sd-check fail (check fail: make target test)")

    def test_a_failure_posts_failure(self) -> None:
        api = Recorder()
        sd_local_gate.post_gate_status(api, self.HEAD, {"head": self.HEAD, "status": "failure", "summary": "x" * 300},
                                       "0" * 12)
        self.assertEqual(api.posts[0][1]["state"], "failure")
        self.assertEqual(len(api.posts[0][1]["description"]), 140)


class Gate(Repository):
    """Every merge attempt runs `sd-check` and posts a fresh status; nothing is reused."""

    def test_a_prior_matching_success_at_the_head_still_runs_the_check(self) -> None:
        head = self.commit("check:\n\t@false\n")
        inputs = sd_local_gate.gate_inputs(self.root, head)
        api = Recorder([{"context": "sd/local-gate", "state": "success", "sha": head, "creator": {"login": "Me"},
                         "description": f"{head[:12]} inputs {inputs}: sd-check pass (check pass)"}])
        result = sd_local_gate.local_gate(api, self.root, head)
        self.assertEqual((result["status"], [body["state"] for _, body in api.posts]), ("failure", ["failure"]))

    def test_a_worktree_fault_is_a_retryable_refusal_and_posts_nothing(self) -> None:
        """`sd_gate_run` raises `GateError`; the merge lane reads it as a runtime refusal, as before the split."""
        head = self.commit("check:\n\t@echo ok\n")
        api = Recorder()
        with mock.patch.object(sd_gate_run, "gate_git", side_effect=sd_gate_run.GateError("worktree refused")):
            with self.assertRaisesRegex(Refusal, "worktree refused") as caught:
                sd_local_gate.local_gate(api, self.root, head)
        self.assertEqual((caught.exception.workflow["blocker"]["code"], api.posts), ("command_failed", []))

    def test_each_attempt_posts_its_own_status_carrying_the_inputs_digest(self) -> None:
        head = self.commit("check:\n\t@echo ok\n")
        api = Recorder()
        sd_local_gate.local_gate(api, self.root, head)
        sd_local_gate.local_gate(api, self.root, head)
        inputs = sd_local_gate.gate_inputs(self.root, head)
        self.assertEqual([body["state"] for _, body in api.posts], ["success", "success"])
        self.assertTrue(api.posts[1][1]["description"].startswith(f"{head[:12]} inputs {inputs}: "))

    def test_the_merge_gate_queues_on_the_slot_bound_apart_from_its_check(self) -> None:
        """sd:2611: like `sd gate check`, so a merge queued behind other gates keeps its whole check bound."""
        head = self.commit("check:\n\t@echo ok\n")
        with mock.patch.object(sd_local_gate, "check_in_worktree", wraps=sd_local_gate.check_in_worktree) as checked:
            sd_local_gate.local_gate(Recorder(), self.root, head)
        self.assertEqual(checked.call_args.kwargs["slot_timeout"], sd_lib.GATE_SLOT_SECONDS)

    def test_the_digest_follows_the_local_block(self) -> None:
        """The parsed block, not the file's bytes: notes outside it and comments in it move nothing (sd:2854)."""
        head = self.commit("check:\n\t@echo ok\n")
        before = sd_local_gate.gate_inputs(self.root, head)
        local = self.root / "CLAUDE.local.md"
        local.write_text(f"an operator note\n{BLOCK_START}\n# a comment\n\n{BLOCK_END}\n", encoding="utf-8")
        self.assertEqual(sd_local_gate.gate_inputs(self.root, head), before)
        local.write_text(f"{BLOCK_START}\ncheck: make other\n{BLOCK_END}\n", encoding="utf-8")
        self.assertNotEqual(sd_local_gate.gate_inputs(self.root, head), before)


class ReceiptFixture(Repository):
    """A workflow database and a check that counts its real runs."""

    def setUp(self) -> None:
        super().setUp()
        from sd_db import initialise

        self.database = self.root.parent / "sd.db"
        initialise(self.database)
        self.counter = self.root.parent / "runs"

    def counted(self, recipe: str = "true") -> str:
        """A check that appends a line outside the tree each time it really runs."""
        return self.commit(f"check:\n\t@echo run >> {self.counter}; {recipe}\n")

    def runs(self) -> int:
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0

    def gate(self, head: str, **kwargs) -> dict:
        return sd_gate_run.check_in_worktree(self.root, head, database=self.database, **kwargs)

    def passing(self, during=lambda: None, mode: str = "full", argvs: list | None = None):  # type: ignore[no-untyped-def]
        """A stand-in run that calls `during` while it runs, then passes with scope `mode`; `argvs` collects its argv."""
        def run(argv, env, tree, timeout):  # type: ignore[no-untyped-def]
            during()
            (argvs if argvs is not None else []).append(argv)
            return 0, json.dumps({"status": "pass", "scope": {"mode": mode}, "checks": []}), ""
        return run


class Receipts(ReceiptFixture):
    """One passing gate per head (sd:2041, sd:1912): a matching receipt answers instead of a second run."""

    def test_a_pass_leaves_a_receipt_the_next_run_at_that_head_reuses(self) -> None:
        head = self.counted()
        first, second = self.gate(head), self.gate(head)
        self.assertEqual((first["status"], "reused" in first, second["status"]), ("success", False, "success"))
        self.assertEqual(self.runs(), 1)
        self.assertEqual(second["head"], head)
        self.assertEqual(second["summary"], "sd-check pass (check pass) (reused)")
        self.assertEqual(second["reused"]["revision"], first["receipt_revision"])

    def test_a_failure_leaves_no_receipt(self) -> None:
        head = self.counted("false")
        self.assertEqual([self.gate(head)["status"] for _ in range(2)], ["failure", "failure"])
        self.assertEqual(self.runs(), 2)

    def test_only_a_success_is_recorded_or_read(self) -> None:
        """Both halves refuse a failure: `record_pass` raises, and a failing row is never a receipt."""
        key = sd_gate_receipts.receipt_key(self.root, "a" * 40)
        with self.assertRaisesRegex(ValueError, "only a passing gate run"):
            sd_gate_receipts.record_pass(self.database, key, {"head": "a" * 40}, {"status": "failure"})
        self.assertIsNone(sd_gate_receipts.lookup(self.database, key, {"head": "a" * 40}))

    def test_another_head_runs_its_own_check(self) -> None:
        first = self.counted()
        self.gate(first)
        second = self.counted("true # another commit")
        self.assertNotIn("reused", self.gate(second))
        self.assertEqual(self.runs(), 2)

    def test_each_head_keeps_its_own_receipt(self) -> None:
        """A second head's pass does not replace the first head's: the key names the head."""
        first = self.counted()
        second = self.counted("true # another commit")
        self.gate(first)
        self.gate(second)
        self.assertIn("reused", self.gate(first))
        self.assertEqual(self.runs(), 2)

    def test_changed_gate_inputs_run_the_check_again(self) -> None:
        """A pack upgrade or a new `CLAUDE.local.md` changes `gate_inputs`, so the receipt no longer binds."""
        head = self.counted()
        self.gate(head)
        (self.root / "CLAUDE.local.md").write_text(f"{BLOCK_START}\nnote: an operator note\n{BLOCK_END}\n", encoding="utf-8")
        self.assertNotIn("reused", self.gate(head))
        with mock.patch.object(sd_gate_run, "gate_inputs", return_value="0" * 12):
            self.assertNotIn("reused", self.gate(head))
        self.assertEqual(self.runs(), 3)

    def test_a_gate_in_a_linked_worktree_reads_the_main_checkouts_local_block(self) -> None:
        """sd:2859. The review reads the main checkout's `CLAUDE.local.md`; the gate read the linked worktree's
        own path, found none, and reused its receipt across an edit to the main checkout's block."""
        head = self.counted()
        linked = self.root.parent / "linked"
        git(self.root, "worktree", "add", "-q", "--detach", str(linked), head)
        gate = lambda: sd_gate_run.check_in_worktree(linked, head, database=self.database)  # noqa: E731
        self.assertEqual(gate()["status"], "success")
        (self.root / "CLAUDE.local.md").write_text(f"{BLOCK_START}\nnote: an edit at the same head\n{BLOCK_END}\n", encoding="utf-8")
        again = gate()
        self.assertNotIn("reused", again)
        self.assertEqual((again["status"], self.runs()), ("success", 2))

    def test_the_check_tree_gets_only_the_digested_block(self) -> None:
        """sd:2854 review: the digest covers the parsed block, so the check reads nothing beyond it.
        No file reads as an empty block, so the tree gets an empty block, not no file (review round 2)."""
        head = self.counted()
        seen: list[str] = []
        def run(argv, env, tree, timeout):  # type: ignore[no-untyped-def]
            seen.append((pathlib.Path(tree) / "CLAUDE.local.md").read_text(encoding="utf-8"))
            return 0, json.dumps({"status": "pass", "scope": {"mode": "full"}, "checks": []}), ""
        self.gate(head, run=run, reuse=False, record=False)
        self.assertEqual(seen.pop(), f"{BLOCK_START}\n{BLOCK_END}\n")
        (self.root / "CLAUDE.local.md").write_text(
            f"an operator note\n{BLOCK_START}\n# a comment\nmode: full  # inline\nnote: it's \"quoted\"\n{BLOCK_END}\ntail\n", encoding="utf-8")
        self.gate(head, run=run, reuse=False, record=False)
        self.assertEqual(seen, [f"{BLOCK_START}\nmode: 'full'\nnote: \"it's \\\"quoted\\\"\"\n{BLOCK_END}\n"])
        self.assertEqual(sd_lib.parse_local_block(seen[0]), {"mode": "full", "note": 'it\'s "quoted"'})

    def test_a_tracked_dangling_link_is_left_alone_not_written_through(self) -> None:
        """sd:2854 review round 3: `exists()` follows a link, so a tracked dangling `CLAUDE.local.md` sent the write outside the tree."""
        outside = self.root.parent / "outside.md"
        (self.root / "CLAUDE.local.md").symlink_to(outside)
        git(self.root, "add", "-f", "CLAUDE.local.md")  # a global ignore may name the file
        head = self.counted()
        git(self.root, "ls-files", "--error-unmatch", "CLAUDE.local.md")  # tracked, or the git helper raises
        self.assertEqual(self.gate(head, run=self.passing(), reuse=False, record=False)["status"], "success")
        self.assertFalse(outside.exists())

    def test_another_repository_records_nothing_when_the_pack_moves_mid_run(self) -> None:
        """The child may open the moved pack, so a landing mid-run drops the pass (sd:2612 review), and says so."""
        head = self.counted()
        pack = self.root.parent / "pack"
        pack.mkdir()
        (pack / "sd-x").write_text("one\n", encoding="utf-8")
        with mock.patch.object(sd_gate_run, "BIN", pack):
            result = self.gate(head, run=self.passing(lambda: (pack / "sd-x").write_text("two\n", encoding="utf-8")))
        self.assertNotIn("receipt_revision", result)
        self.assertEqual(result["receipt_skipped"], "moved during the run: inputs")

    def test_a_pass_left_unrecorded_says_what_moved(self) -> None:
        """A pass whose binding moved during the run leaves no receipt, and the result says why."""
        head = self.counted()
        result = self.gate(head, run=self.passing(mode="docs-only"))
        self.assertEqual(result["status"], "success")
        self.assertNotIn("receipt_revision", result)
        self.assertEqual(result["receipt_skipped"], "moved during the run: scope mode")

    def test_a_pass_under_another_scope_command_is_not_recorded(self) -> None:
        """sd:5109: the child reads the check_scope row itself; a row changed and changed back during
        the run leaves the binding equal, so the command the child ran must be the binding's."""
        head = self.counted()

        def run(argv, env, tree, timeout):  # type: ignore[no-untyped-def]
            return 0, json.dumps({"status": "pass", "scope": {"mode": "full", "command": ["true"]}, "checks": []}), ""
        result = self.gate(head, run=run)
        self.assertEqual(result["status"], "success")
        self.assertNotIn("receipt_revision", result)
        self.assertEqual(result["receipt_skipped"], "moved during the run: scope command")

    def test_a_database_that_does_not_answer_fails_the_gate_unrun(self) -> None:
        """sd:5109: the gate_reuse row is unknown, so no receipt is read or written and no check runs under a guess."""
        head = self.counted()
        with mock.patch.object(sd_lib, "repo_declaration", side_effect=sd_lib.DatabaseUnreadable("down")):
            result = self.gate(head)
        self.assertEqual(result["status"], "failure")
        self.assertTrue(result["summary"].startswith("the gate_reuse declaration cannot be read: down"), result["summary"])
        self.assertNotIn("receipt_revision", result)
        self.assertEqual(self.runs(), 0)

    def test_a_different_path_runs_the_check_again(self) -> None:
        head = self.counted()
        self.gate(head)
        with mock.patch.dict(os.environ, {"PATH": os.environ["PATH"] + os.pathsep + str(self.root.parent)}):
            self.assertNotIn("reused", self.gate(head))
        self.assertEqual(self.runs(), 2)

    def tool(self, name: str, body: str) -> dict[str, str]:
        """`name` on a folder ahead of `PATH`, running `body`: the environment that puts it there."""
        folder = self.root.parent / "tools"
        folder.mkdir(exist_ok=True)
        (folder / name).write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        (folder / name).chmod(0o755)
        return {"PATH": str(folder) + os.pathsep + os.environ["PATH"]}

    def test_a_changed_tool_behind_make_runs_the_check_again(self) -> None:
        """sd:3216 review: `make check` binds `make`, but the check runs `node` through it; its bytes bind too."""
        head = self.counted()
        with mock.patch.dict(os.environ, self.tool("node", "exit 0")):
            self.gate(head)
        with mock.patch.dict(os.environ, self.tool("node", "exit 0 # another release")):
            again, third = self.gate(head), self.gate(head)
        self.assertEqual((again.get("reuse_miss"), "reused" in third, self.runs()),
                         ({"reason": "binding", "fields": ["bound_tools"]}, True, 2))

    def test_another_toolchain_behind_the_same_cargo_runs_the_check_again(self) -> None:
        """sd:3216 review: a rustup proxy's bytes name no toolchain, so `cargo -vV`'s commit-hash binds beside them."""
        head = self.counted()
        answer = self.root.parent / "cargo-vV"
        environment = self.tool("cargo", f"cat {answer}")
        with mock.patch.dict(os.environ, environment):
            for commit in ("797e8a9bca276c1c", "48a229ceaefd4985", "48a229ceaefd4985"):
                answer.write_text(f"cargo 1.98.1\nrelease: 1.98.1\ncommit-hash: {commit}\nos: Mac OS 27\n", encoding="utf-8")
                result = self.gate(head)
        self.assertEqual(("reused" in result, self.runs()), (True, 2))

    def test_a_changed_forwarded_variable_runs_the_check_again(self) -> None:
        """The gate forwards the environment whole, so the receipt binds it whole.

        Prepare with a `MAKEFLAGS` that makes the check pass must not leave a
        receipt that a merge without it reuses: there the real check fails.
        """
        head = self.commit(f'check:\n\t@echo run >> {self.counter}; test "$(MODE)" = ok\n')
        with mock.patch.dict(os.environ, {"MAKEFLAGS": "MODE=ok"}):
            self.assertEqual(self.gate(head)["status"], "success")
        with mock.patch.dict(os.environ):
            os.environ.pop("MAKEFLAGS", None)
            merged = self.gate(head)
        self.assertEqual((merged["status"], "reused" in merged, self.runs()), ("failure", False, 2))

    def test_the_cpu_cap_reaches_the_check_and_the_receipt_binds_it(self) -> None:
        """sd:2726: a suite can pass on one test thread and fail on eight, so another cap runs the check again."""
        seen = self.root.parent / "seen"
        head = self.counted(f'echo "$$CARGO_BUILD_JOBS $$RUST_TEST_THREADS $$NEXTEST_TEST_THREADS" >> {seen}')
        config = self.root.parent / "config" / "sd-ai-command-pack" / "config.json"
        config.parent.mkdir(parents=True)
        machine = {"SD_GATE_SLOTS_DIR": str(self.root.parent / "slots"), "SD_GATE_SLOT_POLL": "0.1",
                   "SD_GATE_LOAD_MAX": "0", "SD_GATE_SETTLE_SECONDS": "0", "XDG_CONFIG_HOME": str(config.parents[1])}
        with mock.patch.dict(os.environ, machine):
            # A gate running this suite hands it its own caps; a lower inherited one would win both times.
            for name in ("SD_GATE_SLOTS", "CI", "GITHUB_ACTIONS", *sd_gate_slots.CPU_VARIABLES):
                os.environ.pop(name, None)
            config.write_text(json.dumps({"config": {"sd": {"gate_slots": "1"}}}), encoding="utf-8")
            first = self.gate(head)
            config.write_text(json.dumps({"config": {"sd": {"gate_slots": "2"}}}), encoding="utf-8")
            second, third = self.gate(head), self.gate(head)
        self.assertEqual([first["status"], second["status"], "reused" in second, "reused" in third, self.runs()],
                         ["success", "success", False, True, 2])
        whole, half = (str(sd_gate_slots.cpu_share(slots)) for slots in (1, 2))  # they differ on two or more cores
        self.assertEqual(seen.read_text().splitlines(), [f"{whole} {whole} {whole}", f"{half} {half} {half}"])  # sd:2872
        with mock.patch.dict(os.environ, machine):
            for name in ("SD_GATE_SLOTS", "CI", "GITHUB_ACTIONS", *sd_gate_slots.CPU_VARIABLES):
                os.environ.pop(name, None)
            raised = self.gate(self.counted("true # another commit"), run=self.passing(
                lambda: config.write_text(json.dumps({"config": {"sd": {"gate_slots": "4"}}}), encoding="utf-8")))
        self.assertEqual(raised["receipt_skipped"], "moved during the run: threads")

    def test_the_binding_keeps_the_caller_thread_counts_the_precheck_runs_on(self) -> None:
        """sd:2726 review: the precheck gets the caller's values, so 16 and 32 bind apart though both cap to 8."""
        head = self.counted()
        with mock.patch.object(os, "cpu_count", return_value=16):
            one, two = (sd_gate_receipts.gate_binding(self.root, head, "0" * 12, None, {
                "SD_GATE_SLOTS": "2", "CARGO_BUILD_JOBS": jobs, "RUST_TEST_THREADS": jobs}) for jobs in ("16", "32"))
        assert one is not None and two is not None
        self.assertEqual((one["threads"], two["threads"]), (dict.fromkeys(sd_gate_slots.CPU_VARIABLES, "8"),) * 2)
        self.assertNotEqual(one["environment_sha256"], two["environment_sha256"])

    def test_the_binding_names_no_offload_field(self) -> None:
        """sd:3216: `environment_mode` served the satellite hand-off, which is gone; `bound_tools` keeps the tool bytes."""
        head = self.counted()
        self.gate(head)
        _, row = self.stored(head)
        self.assertEqual(sorted(set(row["binding"]) & {"offload_tools", "environment_mode", "bound_tools"}), ["bound_tools"])
        self.assertEqual(set(row["binding"]["bound_tools"]), set(sd_gate_receipts.BOUND_TOOLS))

    def test_a_receipt_written_with_the_offload_fields_is_not_reused(self) -> None:
        """sd:3216, older state: a pass the previous version stored carries both fields; it reruns once and does not crash."""
        head = self.counted()
        self.gate(head)
        revision, row = self.stored(head)
        tools = row["binding"].pop("bound_tools")
        row["binding"] = {**row["binding"], "offload_tools": tools, "environment_mode": "whole"}
        with contextlib.closing(sd_gate_receipts._connect(self.database, write=True)) as connection:
            from sd_db import ship

            ship.save(connection, sd_gate_receipts.receipt_key(self.root, head), revision, row)
        again = self.gate(head)
        self.assertEqual((again["status"], "reused" in again, again["reuse_miss"], self.runs()),
                         ("success", False, {"reason": "binding", "fields": ["bound_tools", "environment_mode", "offload_tools"]}, 2))
        self.assertIn("reused", self.gate(head))  # the rerun rebinds: one rerun per stored receipt

    def stored(self, head: str) -> tuple[int, dict]:
        with contextlib.closing(sd_gate_receipts._connect(self.database, write=False)) as connection:
            from sd_db import ship

            return ship.read(connection, sd_gate_receipts.receipt_key(self.root, head))

    def test_a_receipt_older_than_the_window_is_not_reused(self) -> None:
        """The window is one prepare-to-merge handoff: 30 minutes, not hours.

        Inputs outside the repository are not bound, so the window is what
        limits a change there; 31 minutes after the pass, the check runs.
        """
        head = self.counted()
        self.gate(head)
        now = time.time()
        with mock.patch.object(sd_gate_receipts.time, "time", return_value=now + 29 * 60):
            self.assertIn("reused", self.gate(head))
        with mock.patch.object(sd_gate_receipts.time, "time", return_value=now + 31 * 60):
            self.assertNotIn("reused", self.gate(head))
        self.assertEqual(self.runs(), 2)
        self.assertEqual(sd_gate_receipts.REUSE_WINDOW_SECONDS, 30 * 60)

    def test_without_a_database_nothing_is_read_or_written(self) -> None:
        head = self.counted()
        self.gate(head)
        self.assertNotIn("reused", sd_gate_run.check_in_worktree(self.root, head))
        self.assertEqual(self.runs(), 2)

    def test_the_merge_gate_posts_a_reused_pass_and_says_so(self) -> None:
        head = self.counted()
        self.gate(head)
        api = Recorder()
        result = sd_local_gate.local_gate(api, self.root, head, database=self.database)
        self.assertEqual((result["status"], self.runs()), ("success", 1))
        [(_, body)] = api.posts
        self.assertEqual(body["state"], "success")
        self.assertTrue(body["description"].endswith("sd-check pass (check pass) (reused)"), body["description"])

    def test_the_merge_gate_records_no_receipt(self) -> None:
        """Only prepare's pass is a receipt; a merge gate's pass does not serve a later gate."""
        head = self.counted()
        for _ in range(2):
            self.assertNotIn("reused", sd_local_gate.local_gate(Recorder(), self.root, head, database=self.database))
        self.assertEqual(self.runs(), 2)

    def test_a_receipt_for_another_repository_at_the_same_head_is_not_read(self) -> None:
        head = self.counted()
        self.assertNotEqual(sd_gate_receipts.receipt_key(self.root, head),
                            sd_gate_receipts.receipt_key(self.root.parent, head))


class GateToolsRetired(unittest.TestCase):
    def test_sd_gate_tools_is_an_unknown_verb(self) -> None:
        """sd:3216: the pinned tool copies served the offload environment, which is gone."""
        result = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "sd"), "gate", "tools", "status"],
                                capture_output=True, text=True, check=False, env=clean_environment())
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice: 'tools'", result.stderr)


class MergeReuse(ReceiptFixture):
    """`sd-ship merge` posts `sd/local-gate` from prepare's tree receipt, and says why when it cannot (sd:2602).

    Prepare's gate leaves the receipt; the merge gate (`local_gate`, which
    records none) reads it under the same key and window. A run in full keeps
    `reuse_miss`, so a merge that should have reused names what stopped it.
    """

    def declare(self, declared: bool = True) -> str:
        """A branch off `main` whose tree declares the tree key; `origin/main` is the base."""
        self.counted()
        if declared:
            self.declare_row("gate_reuse", {"schema_version": 1, "key": "tree", "reason": "the check reads no commit history"})
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.root, "checkout", "-q", "-b", "topic")
        (self.root / "src.txt").write_text("one\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "topic")
        return git(self.root, "rev-parse", "HEAD")

    def prepare(self, head: str) -> dict:
        return self.gate(head, base=sd_gate_run.base_ref("main"))

    def merge(self, head: str, api: Recorder | None = None) -> dict:
        return sd_local_gate.local_gate(api or Recorder(), self.root, head, base="main", database=self.database)

    def test_an_unchanged_tree_is_posted_from_the_receipt_with_no_run(self) -> None:
        prepared = self.declare()
        self.assertEqual(self.prepare(prepared)["status"], "success")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "attribute\n\nAuthored-with: claude/anthropic")
        head, api = git(self.root, "rev-parse", "HEAD"), Recorder()
        merged = self.merge(head, api)
        self.assertEqual((merged["status"], merged["head"], merged["reused"]["head"]), ("success", head, prepared))
        self.assertNotIn("reuse_miss", merged)
        self.assertTrue(api.posts[0][1]["description"].endswith("(reused)"), api.posts)
        self.assertEqual(self.runs(), 1)

    def test_a_changed_tree_runs_in_full_and_names_no_receipt(self) -> None:
        prepared = self.declare()
        self.prepare(prepared)
        (self.root / "src.txt").write_text("two\n", encoding="utf-8")
        git(self.root, "commit", "-q", "-am", "changed")
        merged = self.merge(git(self.root, "rev-parse", "HEAD"))
        self.assertEqual((merged["status"], "reused" in merged, self.runs()), ("success", False, 2))
        self.assertEqual(merged["reuse_miss"], {"reason": "no-receipt"})

    def test_without_the_opt_in_a_new_head_runs_in_full(self) -> None:
        self.prepare(self.declare(declared=False))
        git(self.root, "commit", "-q", "--allow-empty", "-m", "attribute")
        merged = self.merge(git(self.root, "rev-parse", "HEAD"))
        self.assertEqual((merged["status"], "reused" in merged, self.runs()), ("success", False, 2))
        self.assertEqual(merged["reuse_miss"], {"reason": "no-receipt"})

    def test_an_expired_window_runs_in_full_and_names_the_age(self) -> None:
        head = self.declare()
        self.prepare(head)
        later = time.time() + sd_gate_receipts.TREE_REUSE_WINDOW_SECONDS + 60
        with mock.patch.object(sd_gate_receipts.time, "time", return_value=later):
            merged = self.merge(head)
        self.assertEqual((merged["status"], "reused" in merged, self.runs()), ("success", False, 2))
        self.assertEqual((merged["reuse_miss"]["reason"], merged["reuse_miss"]["window_seconds"]),
                         ("expired", sd_gate_receipts.TREE_REUSE_WINDOW_SECONDS))
        self.assertGreater(merged["reuse_miss"]["age_seconds"], sd_gate_receipts.TREE_REUSE_WINDOW_SECONDS)

    def test_a_different_command_runs_in_full_and_names_the_binding_fields(self) -> None:
        """The untracked `CLAUDE.local.md` may respell `check`; the tree is equal and the command is not."""
        head = self.declare()
        self.prepare(head)
        (self.root / "CLAUDE.local.md").write_text(f"{BLOCK_START}\ncheck: make check MODE=other\n{BLOCK_END}\n", encoding="utf-8")
        merged = self.merge(head)
        self.assertEqual(("reused" in merged, self.runs()), (False, 2))
        self.assertEqual(merged["reuse_miss"]["reason"], "binding")
        self.assertIn("inputs", merged["reuse_miss"]["fields"])

    def test_a_run_whose_binding_cannot_be_named_says_so(self) -> None:
        head = self.declare()
        self.prepare(head)
        with mock.patch.object(sd_gate_receipts, "gate_binding", return_value=None):
            merged = self.merge(head)
        self.assertEqual((merged["reuse_miss"], self.runs()), ({"reason": "unbound"}, 2))

    def test_a_miss_is_kept_in_the_ship_receipt_beside_the_result(self) -> None:
        """`sd-ship merge` saves `local_gate` whole, so the miss reaches `sd-ship observe` with no new field."""
        head = self.declare()
        with mock.patch.dict(os.environ, {"MAKEFLAGS": "-s"}):
            self.prepare(head)
        with mock.patch.dict(os.environ):  # a caller that already exports MAKEFLAGS=-s must still differ
            os.environ.pop("MAKEFLAGS", None)
            merged = self.merge(head)
        self.assertEqual(merged["reuse_miss"], {"reason": "binding", "fields": ["environment_sha256"]})
        self.assertEqual(json.loads(json.dumps(merged))["reuse_miss"], merged["reuse_miss"])

    def test_a_session_with_another_home_still_misses_on_the_environment(self) -> None:
        """Local reuse binds the whole environment, `HOME` included (C-17)."""
        head = self.declare()
        homes = [self.root.parent / name for name in ("one", "two")]
        for home in homes:
            home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(homes[0])}):
            self.prepare(head)
        with mock.patch.dict(os.environ, {"HOME": str(homes[1])}):
            merged = self.merge(head)
        self.assertEqual((merged["reuse_miss"], self.runs()),
                         ({"reason": "binding", "fields": ["environment_sha256"]}, 2))

    def test_a_receipt_another_machine_wrote_is_not_reused(self) -> None:
        """sd:2796 (sd:2782 L7): a satellite's own receipts land in the hub's database under the same key when
        the login and checkout path match; the binding names the machine, so the hub never reuses one."""
        head = self.declare()
        with mock.patch.object(sd_gate_receipts.socket, "gethostname", return_value="satellite.example.test"):
            self.prepare(head)
        merged = self.merge(head)
        self.assertEqual((merged["reuse_miss"], self.runs()), ({"reason": "binding", "fields": ["machine"]}, 2))


class FnmShells(ReceiptFixture):
    """fnm gives every shell its own folder (operator ruling, 2026-10-04, sd:2602).

    `FNM_MULTISHELL_PATH` names `fnm_multishells/<pid>_<ms>`, a symlink to the
    node version in use, and `$FNM_MULTISHELL_PATH/bin` leads `PATH`. Two
    shells on one node differed in both, so a builder's receipt never bound
    the lane's prepare. The gate drops the variable and resolves each `PATH`
    entry; a different real node still differs.
    """

    def setUp(self) -> None:
        super().setUp()
        self.fnm = self.root.parent / "fnm"
        for version in ("v20", "v22"):
            (self.fnm / "node-versions" / version / "bin").mkdir(parents=True)
            node = self.fnm / "node-versions" / version / "bin" / "node"
            node.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
            node.chmod(0o755)
        (self.fnm / "multishells").mkdir()

    def shell(self, name: str, version: str = "v20") -> dict[str, str]:
        """The environment of one shell on `version`, through its own multishell folder."""
        folder = self.fnm / "multishells" / name
        folder.symlink_to(self.fnm / "node-versions" / version)
        return {**os.environ, "FNM_MULTISHELL_PATH": str(folder),
                "PATH": os.pathsep.join([str(folder / "bin"), os.environ["PATH"]])}

    def node_check(self) -> str:
        return self.counted("node")

    def merge(self, head: str, environ: dict[str, str]) -> dict:
        return sd_gate_run.check_in_worktree(self.root, head, database=self.database, environ=environ, record=False)

    def test_two_shells_on_the_same_node_bind_one_receipt(self) -> None:
        head = self.node_check()
        self.assertEqual(self.gate(head, environ=self.shell("1739_1")).get("status"), "success")
        merged = self.merge(head, self.shell("2201_2"))
        self.assertEqual(("reused" in merged, merged.get("reuse_miss"), self.runs()), (True, None, 1))

    def test_a_different_real_node_still_runs_again(self) -> None:
        head = self.node_check()
        self.gate(head, environ=self.shell("1739_1"))
        merged = self.merge(head, self.shell("2201_2", version="v22"))
        self.assertEqual(("reused" in merged, merged["reuse_miss"]["reason"], self.runs()), (False, "binding", 2))

    def test_a_path_entry_through_a_shell_folder_binds_as_the_real_folder(self) -> None:
        head = self.node_check()
        real = {**os.environ, "PATH": os.pathsep.join([str(self.fnm / "node-versions" / "v20" / "bin"), os.environ["PATH"]])}
        self.gate(head, environ=real)
        through = self.shell("2201_2")
        del through["FNM_MULTISHELL_PATH"]
        self.assertIn("reused", self.merge(head, through))
        self.assertEqual(self.runs(), 1)


class JevRunIds(ReceiptFixture):
    """`JEV_RUN` names one run's `jev` calls (sd:2954), so prepare and merge always differ in it.

    Bound, it made every merge miss prepare's receipt and run the gate a second time (sd:3013).
    """

    def test_two_runs_that_differ_only_in_jev_run_bind_one_receipt(self) -> None:
        head = self.counted()
        self.assertEqual(self.gate(head, environ={**os.environ, "JEV_RUN": "sd-ship-prepare"}).get("status"), "success")
        merged = self.gate(head, environ={**os.environ, "JEV_RUN": "sd-ship-merge"}, record=False)
        self.assertEqual(("reused" in merged, merged.get("reuse_miss"), self.runs()), (True, None, 1))


class CargoBuildCache(Repository):
    """A Rust repository's gates share warm build folders instead of compiling every dependency cold (sd:2493)."""

    def setUp(self) -> None:
        super().setUp()
        self.cache = self.root.parent / "cache"
        self.seen = self.root.parent / "seen"

    def rust(self, recipe: str = 'echo "$$CARGO_TARGET_DIR" >> {seen}') -> str:
        (self.root / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n', encoding="utf-8")
        return self.commit(f"check:\n\t@{recipe.format(seen=self.seen)}\n")

    def environ(self, **extra: str) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if key not in ("CARGO_TARGET_DIR", sd_gate_cache.CARGO_TARGETS_VARIABLE)}
        return {**env, sd_gate_cache.CACHE_VARIABLE: str(self.cache), **extra}

    def folders(self) -> list[str]:
        return self.seen.read_text(encoding="utf-8").splitlines()

    def test_two_gates_of_one_repository_build_into_the_same_folder_outside_the_worktree(self) -> None:
        head = self.rust()
        for _ in range(2):
            result = sd_gate_run.check_in_worktree(self.root, head, environ=self.environ())
            self.assertEqual(result["status"], "success", result)
        first, second = self.folders()
        self.assertEqual(first, second)
        self.assertTrue(pathlib.Path(first).is_relative_to(self.cache), first)

    def test_gates_that_run_at_once_never_share_a_folder(self) -> None:
        """A shared folder is safe in sequence only: nextest runs binaries after cargo's lock is released,
        so a second gate's build could replace them mid-run. A held folder is skipped; past the pool, cold."""
        self.rust()
        env = self.environ()
        with sd_gate_cache.cargo_target(self.root, self.root, env) as first, \
                sd_gate_cache.cargo_target(self.root, self.root, env) as second, \
                sd_gate_cache.cargo_target(self.root, self.root, env) as third:
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first, second)
            self.assertIsNone(third, f"the default pool is {sd_gate_cache.DEFAULT_CARGO_TARGETS}")
        with sd_gate_cache.cargo_target(self.root, self.root, env) as again:
            self.assertEqual(again, first, "a released folder is the first one taken again")

    def test_a_warm_folder_builds_without_incremental_state(self) -> None:
        """Each gate's worktree is new, so incremental sessions never pay off; measured on a Rust repository
        on macOS, they and their object files grew a warm folder by about 4 GB a gate, against 0.26 GB without."""
        self.rust()
        with sd_gate_cache.cargo_environment(self.root, self.root, self.environ(CARGO_INCREMENTAL="1")) as warm:
            self.assertEqual(warm["CARGO_INCREMENTAL"], "0")
        cold = self.environ(CARGO_INCREMENTAL="1", **{sd_gate_cache.CARGO_TARGETS_VARIABLE: "0"})
        with sd_gate_cache.cargo_environment(self.root, self.root, cold) as child:
            self.assertEqual(child["CARGO_INCREMENTAL"], "1", "a cold build in the worktree runs as it always did")

    def test_another_repository_gets_its_own_folder(self) -> None:
        self.rust()
        other = self.root.parent / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        (other / "Cargo.toml").write_text("", encoding="utf-8")
        git(other, "add", "Cargo.toml")
        env = self.environ()
        with sd_gate_cache.cargo_target(self.root, self.root, env) as mine, \
                sd_gate_cache.cargo_target(other, other, env) as theirs:
            self.assertNotEqual(pathlib.Path(mine).parent, pathlib.Path(theirs).parent)

    def test_the_operators_cargo_target_dir_never_reaches_the_check(self) -> None:
        """The gate borrows nothing from the operator: with the cache off it builds in its own worktree."""
        head = self.rust('test -z "$$CARGO_TARGET_DIR"')
        environ = self.environ(CARGO_TARGET_DIR=str(self.root / "target"), **{sd_gate_cache.CARGO_TARGETS_VARIABLE: "0"})
        self.assertEqual(sd_gate_run.check_in_worktree(self.root, head, environ=environ)["status"], "success")
        self.assertFalse(self.cache.exists())
        self.assertNotIn("CARGO_TARGET_DIR", sd_gate_run.gate_environment(self.root, environ))

    def test_a_repository_without_cargo_takes_no_folder(self) -> None:
        head = self.commit('check:\n\t@test -z "$$CARGO_TARGET_DIR"\n')
        environ = self.environ(CARGO_TARGET_DIR="/operator/target")
        self.assertEqual(sd_gate_run.check_in_worktree(self.root, head, environ=environ)["status"], "success")
        self.assertFalse(self.cache.exists())

    def test_a_nested_cargo_workspace_counts(self) -> None:
        (self.root / "rust").mkdir()
        (self.root / "rust" / "Cargo.toml").write_text("", encoding="utf-8")
        head = self.commit('check:\n\t@echo "$$CARGO_TARGET_DIR" >> ' + str(self.seen) + "\n")
        self.assertEqual(sd_gate_run.check_in_worktree(self.root, head, environ=self.environ())["status"], "success")
        self.assertTrue(pathlib.Path(self.folders()[0]).is_relative_to(self.cache))

    def test_a_pass_in_one_folder_is_reused_from_another(self) -> None:
        """The folder is a cache, not an input: which one a run took does not bind its receipt."""
        from sd_db import initialise

        database = self.root.parent / "sd.db"
        initialise(database)
        head = self.rust()
        env = self.environ()
        with sd_gate_cache.cargo_target(self.root, self.root, env):  # another gate holds the first folder
            first = sd_gate_run.check_in_worktree(self.root, head, environ=env, database=database)
        second = sd_gate_run.check_in_worktree(self.root, head, environ=env, database=database)
        self.assertEqual((first["status"], "reused" in second), ("success", True), second)
        self.assertEqual(len(self.folders()), 1)


class PackGatesItself(ReceiptFixture):
    """The pack gating itself runs and binds its own tree, not the checkout's `bin/` (sd:2613)."""

    def setUp(self) -> None:
        super().setUp()
        self.pack = self.root / "bin"
        self.pack.mkdir()
        (self.pack / "sd-check").write_text("#!/bin/sh\n", encoding="utf-8")
        self.declare_row("gate_reuse", {"schema_version": 1, "key": "tree", "reason": "a fixture", "tool": "tree"})
        self.head = self.counted()

    def land(self) -> None:
        """A pack merge lands in the checkout the gate runs from."""
        (self.pack / "sd-x").write_text(f"{time.time()}\n", encoding="utf-8")

    def test_the_pack_reuses_its_pass_after_the_checkout_moves(self) -> None:
        with mock.patch.object(sd_gate_run, "BIN", self.pack):
            self.gate(self.head, run=self.passing())
            self.land()
            self.assertIn("reused", self.gate(self.head, run=self.passing()))

    def test_the_pack_runs_its_own_sd_check(self) -> None:
        argvs: list = []
        with mock.patch.object(sd_gate_run, "BIN", self.pack):
            self.gate(self.head, run=self.passing(argvs=argvs))
        self.assertTrue(argvs[0][1].endswith("/tree/bin/sd-check"), argvs[0][1])

    def test_a_pack_landing_mid_run_still_records_for_the_pack(self) -> None:
        with mock.patch.object(sd_gate_run, "BIN", self.pack):
            result = self.gate(self.head, run=self.passing(self.land))
        self.assertIn("receipt_revision", result)

    def test_a_foreign_declaration_still_binds_the_checkout(self) -> None:
        """Another repository's `"tool": "tree"` is ignored: its gate runs and binds the checkout's pack."""
        foreign = self.root.parent / "pack"
        foreign.mkdir()
        (foreign / "sd-check").write_text("#!/bin/sh\n", encoding="utf-8")
        argvs: list = []
        with mock.patch.object(sd_gate_run, "BIN", foreign):
            self.gate(self.head, run=self.passing(argvs=argvs))
            (foreign / "sd-check").write_text("#!/bin/sh\n# landed\n", encoding="utf-8")
            self.assertNotIn("reused", self.gate(self.head, run=self.passing(argvs=argvs)))
        self.assertEqual(argvs[0][1], str(foreign / "sd-check"))


#: The pack row's `gate_reuse` declaration (sd:5109), as the move after merge writes it from the retired file.
PACK_GATE_REUSE = {'schema_version': 1, 'key': 'tree', 'reason': "make check reads no commit message, trailer, range or tag of this checkout: docs-lint runs --no-history (sd:2606), the suites read history only in fixture repositories (sd:2593), and a GIT_TRACE of lint, audit and docs-lint at f7a3a90f shows only rev-parse paths, ls-files and remote get-url (sd:2610); tool tree: the pack's gate runs its own bin/sd-check from the gated tree (sd:2613)", 'tool': 'tree'}


class PackDeclaresTreeReuse(unittest.TestCase):
    """The pack keys its own gate receipts by tree (sd:2610): `make check` reads no commit history.

    The declaration is the pack row's (sd:5109): seeded here in a scratch database, read through the gate's own reader.
    """

    def setUp(self) -> None:
        home = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        patch = mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")})
        patch.start()
        self.addCleanup(patch.stop)
        seed_declaration(home, sd_lib.main_worktree_root(REPO_ROOT), "gate_reuse", PACK_GATE_REUSE)

    def test_the_pack_keys_its_gate_receipts_by_tree(self) -> None:
        self.assertTrue(sd_gate_receipts.keyed_by_tree(REPO_ROOT), sd_gate_receipts.REUSE_DECLARATION)

    def test_the_pack_gates_itself(self) -> None:
        """sd:2613: the pack's gate runs and binds the pack's own tree."""
        self.assertTrue(sd_gate_receipts.gates_itself(REPO_ROOT, REPO_ROOT, sd_gate_run.BIN))

    def test_the_check_lints_docs_without_history(self) -> None:
        """The declaration rests on this: without `--no-history`, docs-lint fetches and reads `git log` (sd:2606)."""
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        recipe = makefile.split("\ndocs-lint:\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("bin/sd-docs-lint --no-history", recipe)


class CacheBound(Repository):
    """The warm folders stay under a bound: the least recently used free folder goes first (sd:2598)."""

    def setUp(self) -> None:
        super().setUp()
        self.cache = self.root.parent / "cache"

    def folder(self, name: str, used: float, size: int = 1000) -> pathlib.Path:
        """A filled warm folder whose lock file says it was last taken `used` seconds after the epoch."""
        folder = self.cache / name
        folder.mkdir(parents=True)
        (folder / "build").write_bytes(b"x" * size)
        lock = folder.parent / f"{folder.name}.lock"
        lock.touch()
        os.utime(lock, (used, used))
        return folder

    def test_over_the_bound_the_least_recently_used_folder_goes_first(self) -> None:
        old, mid, new = (self.folder(f"repo-{n}/cargo-target.1", used) for n, used in (("a", 100), ("b", 200), ("c", 300)))
        pruned = sd_gate_cache.prune(self.cache, new, 2500)
        self.assertEqual([path for path, _ in pruned], [old])
        self.assertEqual((old.exists(), mid.exists(), new.exists()), (False, True, True))

    def test_a_folder_in_use_is_never_pruned(self) -> None:
        old = self.folder("repo-a/cargo-target.1", 100)
        mid = self.folder("repo-a/cargo-target.2", 200)
        new = self.folder("repo-b/cargo-target.1", 300)
        with open(old.parent / "cargo-target.1.lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another gate is building in it
            pruned = sd_gate_cache.prune(self.cache, new, 2500)
        self.assertEqual([path for path, _ in pruned], [mid])
        self.assertTrue(old.exists())

    def test_the_held_folder_goes_last_and_only_when_the_rest_is_not_enough(self) -> None:
        other = self.folder("repo-a/cargo-target.1", 300)
        held = self.folder("repo-b/cargo-target.1", 100, size=3000)
        pruned = sd_gate_cache.prune(self.cache, held, 2500)
        self.assertEqual([path for path, _ in pruned], [other, held])

    def test_under_the_bound_or_with_no_bound_nothing_goes(self) -> None:
        old = self.folder("repo-a/cargo-target.1", 100)
        new = self.folder("repo-b/cargo-target.1", 200)
        self.assertEqual(sd_gate_cache.prune(self.cache, new, 2000), [])
        self.assertEqual(sd_gate_cache.prune(self.cache, new, 0), [])
        self.assertTrue(old.exists())

    def test_the_bound_reads_the_variable_then_the_setting_then_the_default(self) -> None:
        gib = 1024 ** 3
        home = {"XDG_CONFIG_HOME": str(self.root.parent / "config")}
        self.assertEqual(sd_gate_cache.cache_bound(home), sd_gate_cache.DEFAULT_CACHE_GB * gib)
        self.assertEqual(sd_gate_cache.cache_bound({**home, sd_gate_cache.CACHE_GB_VARIABLE: "0.5"}), gib // 2)
        self.assertEqual(sd_gate_cache.cache_bound({**home, sd_gate_cache.CACHE_GB_VARIABLE: "0"}), 0)
        with mock.patch.object(sd_lib, "core_setting", return_value="7"):
            self.assertEqual(sd_gate_cache.cache_bound(home), 7 * gib)

    def test_the_gate_names_what_it_pruned(self) -> None:
        old = self.folder("other-0123456789ab/cargo-target.1", 100)
        (self.root / "Cargo.toml").write_text("", encoding="utf-8")
        git(self.root, "add", "Cargo.toml")
        env = {key: value for key, value in os.environ.items() if key != sd_gate_cache.CARGO_TARGETS_VARIABLE}
        env.update({sd_gate_cache.CACHE_VARIABLE: str(self.cache), sd_gate_cache.CACHE_GB_VARIABLE: "0.0000005"})
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), sd_gate_cache.cargo_target(self.root, self.root, env) as target:
            self.assertIsNotNone(target)
        self.assertFalse(old.exists())
        self.assertIn(f"sd gate: pruned {old}", errors.getvalue())


class GateTemporaryFolder(Repository):
    """sd:3032: a check's temp folders land in the gate's own folder, so the gate's cleanup removes them."""

    def test_a_check_that_makes_a_temp_folder_leaves_nothing_in_the_outer_tmpdir(self) -> None:
        outer = self.root.parent / "outer"
        outer.mkdir()
        head = self.commit(f"check:\n\t@{sys.executable} -c 'import tempfile; tempfile.mkdtemp()'\n")
        with mock.patch.dict(os.environ, {"TMPDIR": str(outer)}), mock.patch.object(tempfile, "tempdir", str(outer)):
            self.assertEqual(sd_gate_run.check_in_worktree(self.root, head)["status"], "success")
        self.assertEqual(sorted(path.name for path in outer.iterdir()), [])


class TemporaryLeftovers(unittest.TestCase):
    """sd:3032: a gate start removes the sd folders of dead processes from the temp dir, and nothing else."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.temporary = pathlib.Path(tmp.name).resolve() / "T"
        self.temporary.mkdir()
        self.now = time.time()

    def entry(self, name: str, *, age: float = 2 * 86400, folder: bool = True) -> pathlib.Path:
        path = self.temporary / name
        if folder:
            path.mkdir()
            (path / "sd.db").write_text("x", encoding="utf-8")
            os.utime(path / "sd.db", (self.now - age, self.now - age))
        else:
            path.write_text("x", encoding="utf-8")
        os.utime(path, (self.now - age, self.now - age))
        return path

    def dead(self) -> int:
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        return gone.pid

    def test_nothing_goes_by_age(self) -> None:
        """Review round 2: an old entry may still be a live process's or the operator's input, so no name and no age
        removes it; the named leakers get fixed where they are made."""
        kept = [self.entry(f"{prefix}x7", age=30 * 86400) for prefix in ("sd-restore", "traces-poc", "trail-audit-",
                                                                         "sd-ship-template", "sd-ship-verify")]
        kept += [self.entry("tmpab_d1234", age=30 * 86400), self.entry("tmp.AbCdEfGh12", age=30 * 86400),
                 self.entry("tmpx_1w8w2y.yaml", age=30 * 86400, folder=False), self.entry("sd-tests-x7", age=30 * 86400),
                 self.entry("keep-me", age=30 * 86400)]
        self.assertEqual(sd_gate_cache.reap_temporary(self.temporary), 0)
        self.assertEqual([path.exists() for path in kept], [True] * len(kept))

    def test_a_pid_named_entry_goes_only_when_its_pid_is_dead_whatever_its_age(self) -> None:
        """Review round 1: a gate's or a test run's folder names its pid; a live one stays however old, a dead one goes."""
        live = [self.entry(f"{prefix}{os.getpid()}-abc", age=30 * 86400) for prefix in (sd_gate_cache.GATE_PREFIX, "sd-tests-", "sd-ship-verify-")]
        gone = [self.entry(f"{prefix}{self.dead()}-abc", age=60) for prefix in (sd_gate_cache.GATE_PREFIX, "sd-tests-", "sd-ship-verify-")]
        self.assertEqual(sd_gate_cache.reap_temporary(self.temporary), len(gone))
        self.assertEqual(([path.exists() for path in live], [path.exists() for path in gone]), ([True] * 3, [False] * 3))

    def test_the_test_package_names_its_temp_dir_with_its_pid(self) -> None:
        self.assertRegex(pathlib.Path(os.environ["TMPDIR"]).name, rf"sd-tests-{os.getpid()}-[a-z0-9_]+")

    def test_a_link_goes_and_its_target_stays(self) -> None:
        target = self.entry("keep-me")
        link = self.temporary / f"sd-tests-{self.dead()}-zz"
        link.symlink_to(target)
        self.assertEqual(sd_gate_cache.reap_temporary(self.temporary), 1)
        self.assertEqual((os.path.lexists(link), (target / "sd.db").is_file()), (False, True))

    def test_a_removal_that_fails_is_not_counted(self) -> None:
        path = self.entry(f"sd-tests-{self.dead()}-x7")
        with mock.patch.object(shutil, "rmtree"):
            self.assertEqual(sd_gate_cache.reap_temporary(self.temporary), 0)
        self.assertTrue(path.exists())

    def test_another_users_entry_stays(self) -> None:
        path = self.entry(f"sd-tests-{self.dead()}-x7")
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            self.assertEqual(sd_gate_cache.reap_temporary(self.temporary), 0)
        self.assertTrue(path.exists())

    def test_a_gate_start_reaps_its_temp_dir(self) -> None:
        old = self.entry(f"sd-tests-{self.dead()}-x7")
        with mock.patch.object(tempfile, "tempdir", str(self.temporary)), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            sd_gate_cache.worktree_place(pathlib.Path(self.temporary), "HEAD")
        self.assertFalse(old.exists())
        self.assertIn("removed 1 temp entries", errors.getvalue())


class OuterTemporaryUntouched(unittest.TestCase):
    """sd:3032: a test's gate start reaps the test run's own temp dir, never the one the run was started with.

    The reaper's line on stderr broke `test_rule_registry` leg d: it landed inside a child's `... ok` line, so the
    control read as no verdict. And a suite run outside the gate reaped the operator's real `$TMPDIR`.
    """

    def test_a_gate_start_under_the_test_package_leaves_the_outer_temp_dir_alone(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        outer = pathlib.Path(tmp.name).resolve() / "T"
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        leftover = f"sd-tests-{gone.pid}-x7"
        (outer / leftover).mkdir(parents=True)
        repo = pathlib.Path(tmp.name) / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        code = ("import sys, pathlib; sys.path.insert(0, 'bin'); import tests, sd_gate_cache; "
                f"sd_gate_cache.worktree_place(pathlib.Path({str(repo)!r}), 'HEAD')")
        done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                              env={**os.environ, "TMPDIR": str(outer)}, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((sorted(p.name for p in outer.iterdir() if p.name == leftover), done.stderr), ([leftover], ""))


def roomy(disk: pathlib.Path) -> contextlib.AbstractContextManager[object]:
    """`disk` reads `FREE` bytes free, others their real usage: the fake disk sits on the real temp dir, the
    shared 4 GiB RAM disk in a gate, which concurrent gates filled under 1 GiB and failed six tests (sd-1715)."""
    real = shutil.disk_usage
    return mock.patch.object(shutil, "disk_usage",
                             lambda path: mock.Mock(free=sd_tmpdisk.FREE) if pathlib.Path(path) == disk else real(path))


class TmpDiskProbe(unittest.TestCase):
    """sd:7557: the RAM disk is ready only when every probe passes; any failed probe reads "not ready", never an error."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.disk = pathlib.Path(tmp.name).resolve() / "sd-tmp"
        self.disk.mkdir()
        (self.disk / sd_tmpdisk.READY).write_text("", encoding="utf-8")
        self.mounted = True
        for patch in (mock.patch.object(sd_tmpdisk, "ROOT", self.disk), roomy(self.disk),
                      mock.patch.object(sd_tmpdisk, "ismount", lambda path: self.mounted and path == self.disk)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_mounted_marked_writable_disk_with_room_is_ready(self) -> None:
        self.assertTrue(sd_tmpdisk.disk_ready())

    def test_each_failed_probe_reads_not_ready(self) -> None:
        self.mounted = False
        verdicts = {"not a mount point": sd_tmpdisk.disk_ready()}
        self.mounted = True
        with mock.patch.object(os, "access", return_value=False):
            verdicts["read-only"] = sd_tmpdisk.disk_ready()
        with mock.patch.object(shutil, "disk_usage", return_value=mock.Mock(free=sd_tmpdisk.FREE - 1)):
            verdicts["under 1 GiB free"] = sd_tmpdisk.disk_ready()
        with mock.patch.object(shutil, "disk_usage", side_effect=OSError("gone")):
            verdicts["usage unreadable"] = sd_tmpdisk.disk_ready()
        (self.disk / sd_tmpdisk.READY).unlink()
        verdicts["no marker"] = sd_tmpdisk.disk_ready()
        self.disk.rmdir()
        verdicts["no folder"] = sd_tmpdisk.disk_ready()
        self.assertEqual(verdicts, dict.fromkeys(verdicts, False))

    def test_a_dir_already_on_the_disk_or_in_an_sd_folder_stays(self) -> None:
        """A check's tests nest in their gate's folder, and a test's gate in its run's (sd:3032)."""
        outer = pathlib.Path("/nonexistent-sd-7557/T")  # a path outside this run's own `sd-tests-` folder
        nested = [str(outer / f"sd-local-gate-{os.getpid()}-ab" / "t"), str(outer / f"sd-tests-{os.getpid()}-ab"),
                  str(self.disk / "x")]
        self.assertEqual([sd_tmpdisk.temporary(path) for path in nested], nested)
        self.assertEqual(sd_tmpdisk.temporary(str(outer)), str(self.disk))
        self.mounted = False
        self.assertEqual(sd_tmpdisk.temporary(str(outer)), str(outer), "not ready keeps the inherited temp dir")


class RamDiskGate(Repository):
    """sd:7557: once the RAM disk is ready, the gate's folder and a test run's go there; a Rust tree stays off it."""

    def setUp(self) -> None:
        super().setUp()
        self.disk = self.root.parent / "sd-tmp"
        self.disk.mkdir()
        (self.disk / sd_tmpdisk.READY).write_text("", encoding="utf-8")
        self.outer = self.root.parent / "outer"
        self.outer.mkdir()
        self.seen = self.root.parent / "seen"
        # The run's own `sd-tests-<pid>-` folder holds both fakes; that nesting keeps the real disk away (sd:3032).
        for patch in (mock.patch.object(sd_tmpdisk, "ROOT", self.disk), roomy(self.disk),
                      mock.patch.object(sd_tmpdisk, "ismount", lambda path: path == self.disk),
                      mock.patch.object(sd_tmpdisk, "owned_inside", lambda path: False),
                      mock.patch.dict(os.environ, {"TMPDIR": str(self.outer)}),
                      mock.patch.object(tempfile, "tempdir", str(self.outer))):
            patch.start()
            self.addCleanup(patch.stop)

    def places(self) -> list[pathlib.Path]:
        return [pathlib.Path(line) for line in self.seen.read_text(encoding="utf-8").splitlines()]

    def recipe(self) -> str:
        return f"check:\n\t@pwd >> {self.seen}; echo \"$$TMPDIR\" >> {self.seen}\n"

    def test_a_gate_builds_its_tree_and_check_tmpdir_on_a_ready_disk(self) -> None:
        head = self.commit(self.recipe())
        self.assertEqual(sd_gate_run.check_in_worktree(self.root, head)["status"], "success")
        tree, temporary = self.places()
        self.assertEqual((tree.is_relative_to(self.disk), temporary.is_relative_to(self.disk)), (True, True), tree)
        self.assertEqual((list(self.disk.iterdir()), list(self.outer.iterdir())), ([self.disk / sd_tmpdisk.READY], []))

    def test_a_disk_that_is_not_ready_keeps_the_inherited_temp_dir(self) -> None:
        (self.disk / sd_tmpdisk.READY).unlink()
        head = self.commit(self.recipe())
        self.assertEqual(sd_gate_run.check_in_worktree(self.root, head)["status"], "success")
        self.assertEqual([place.is_relative_to(self.outer) for place in self.places()], [True, True])

    def test_a_tree_that_tracks_cargo_stays_off_the_disk(self) -> None:
        """A cold build goes into the worktree's `target/`, about 4.5 GB, more than the 4 GiB disk."""
        (self.root / "crate").mkdir()
        (self.root / "crate" / "Cargo.toml").write_text('[package]\nname = "f"\nversion = "0.1.0"\n', encoding="utf-8")
        head = self.commit(self.recipe())
        env = {**os.environ, sd_gate_cache.CARGO_TARGETS_VARIABLE: "0"}
        self.assertEqual(sd_gate_run.check_in_worktree(self.root, head, environ=env)["status"], "success")
        self.assertEqual([place.is_relative_to(self.outer) for place in self.places()], [True, True])

    def test_a_head_that_cannot_be_read_counts_as_one_that_tracks_cargo(self) -> None:
        self.commit(self.recipe())
        place = sd_gate_cache.worktree_place(self.root, "0" * 40)["dir"]
        self.assertEqual(place, str(self.outer))

    def test_a_gate_start_reaps_the_disk_whatever_owner_it_reads_and_the_inherited_dir_too(self) -> None:
        """The disk mounts with ownership off, so an entry's owner is the volume's; the inherited dir keeps the
        check, and older gates left their folders there."""
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        on_disk, in_outer = self.disk / f"sd-tests-{gone.pid}-x7", self.outer / f"sd-tests-{gone.pid}-x7"
        on_disk.mkdir()
        in_outer.mkdir()
        head = self.commit(self.recipe())
        old = self.outer / f"{sd_gate_cache.GATE_PREFIX}{gone.pid}-ab"
        git(self.root, "worktree", "add", "-q", "--detach", str(old / "tree"), head)
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            place = sd_gate_cache.worktree_place(self.root, head)["dir"]
        self.assertEqual((place, on_disk.exists(), in_outer.exists(), old.exists(), self.worktrees()),
                         (str(self.disk), False, True, False, 1), errors.getvalue())

    def test_a_full_disk_of_dead_gate_folders_is_reaped_and_the_next_gate_lands_on_it(self) -> None:
        """Lead review: reaping went through the free-space probe, so a disk that dead gates filled was never reaped,
        and every later gate kept off it for good. A marked disk is reaped whatever its free space."""
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        dead = self.disk / f"{sd_gate_cache.GATE_PREFIX}{gone.pid}-ab"
        (dead / sd_gate_cache.TEMPORARY).mkdir(parents=True)
        real = shutil.disk_usage

        def usage(path: str) -> object:  # full while the dead gate's folder is there
            return mock.Mock(free=sd_tmpdisk.FREE - (1 if dead.exists() else 0)) if path == self.disk else real(path)

        head = self.commit(self.recipe())
        with mock.patch.object(shutil, "disk_usage", usage), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sd_gate_run.check_in_worktree(self.root, head)["status"], "success")
        tree, _ = self.places()
        self.assertEqual((dead.exists(), tree.is_relative_to(self.disk)), (False, True), tree)

    def nested_on_disk(self) -> pathlib.Path:
        """The inherited temp dir on the disk, as in a test run or a check's `t/` there; the system's per-user temp dir
        a fake folder."""
        nested = self.disk / f"sd-tests-{os.getpid()}-ab"
        nested.mkdir()
        self.system = self.root.parent / "system"
        self.system.mkdir()
        for patch in (mock.patch.dict(os.environ, {"TMPDIR": str(nested)}),
                      mock.patch.object(tempfile, "tempdir", str(nested)),
                      mock.patch.object(os, "confstr", lambda name: str(self.system))):
            patch.start()
            self.addCleanup(patch.stop)
        return nested

    def test_a_cargo_head_under_a_temp_dir_on_the_disk_builds_off_it(self) -> None:
        """Lane review: the Rust exclusion kept the inherited dir, and a nested run's is on the disk, so a cold
        `target/` of about 4.5 GB still landed on the 4 GiB disk."""
        self.nested_on_disk()
        (self.root / "Cargo.toml").write_text('[package]\nname = "f"\nversion = "0.1.0"\n', encoding="utf-8")
        head = self.commit(self.recipe())
        env = {**os.environ, sd_gate_cache.CARGO_TARGETS_VARIABLE: "0"}
        self.assertEqual(sd_gate_run.check_in_worktree(self.root, head, environ=env)["status"], "success")
        self.assertEqual([place.is_relative_to(self.system) for place in self.places()], [True, True])

    def test_a_head_git_cannot_list_under_a_temp_dir_on_the_disk_goes_off_it(self) -> None:
        self.nested_on_disk()
        self.commit(self.recipe())
        self.assertEqual(sd_gate_cache.worktree_place(self.root, "0" * 40)["dir"], str(self.system))

    def test_a_gate_nested_on_a_disk_that_is_not_ready_goes_off_it(self) -> None:
        """Not ready kept the inherited dir, which a nested run has on the disk itself."""
        self.nested_on_disk()
        with mock.patch.object(shutil, "disk_usage", return_value=mock.Mock(free=sd_tmpdisk.FREE - 1)):
            place = sd_gate_cache.worktree_place(self.root, self.commit(self.recipe()))["dir"]
        self.assertEqual(place, str(self.system))

    def test_a_gate_nested_on_a_ready_disk_stays_in_its_runs_folder(self) -> None:
        nested = self.nested_on_disk()
        self.assertEqual(sd_gate_cache.worktree_place(self.root, self.commit(self.recipe()))["dir"], str(nested))

    def test_the_system_temp_dir_ignores_tmpdir(self) -> None:
        """macOS names the per-user temp dir, whatever `TMPDIR` says. Without the name (no RAM disk there either), a
        gate takes no fixed path (bandit B108): the inherited dir off the disk, else tempfile's default."""
        nested = self.nested_on_disk()
        self.assertEqual(sd_tmpdisk.system_temporary(), str(self.system))
        with mock.patch.object(os, "confstr", side_effect=ValueError("unrecognized configuration name")):
            self.assertEqual((sd_tmpdisk.system_temporary(), sd_tmpdisk.gate_temporary(str(nested), rust=True),
                              sd_tmpdisk.gate_temporary(str(self.outer), rust=True)), (None, None, str(self.outer)))
        with mock.patch.object(os, "confstr", return_value=str(self.disk)):
            self.assertIsNone(sd_tmpdisk.system_temporary(), "a per-user name on the disk is refused too")

    def test_a_python_that_does_not_name_the_per_user_dir_still_reads_it_by_number(self) -> None:
        """Lane review: the hub's Python registers no `CS_DARWIN_USER_TEMP_DIR`, so the name raised, the dir read None,
        and a Cargo gate under a `TMPDIR` on the disk landed on it. `<unistd.h>` numbers it 65537."""
        self.nested_on_disk()

        def confstr(name: str | int) -> str:  # as Python answers where it does not know the name
            if name != 65537:
                raise ValueError("unrecognized configuration name")
            return str(self.system)

        (self.root / "Cargo.toml").write_text('[package]\nname = "f"\nversion = "0.1.0"\n', encoding="utf-8")
        head = self.commit(self.recipe())
        env = {**os.environ, sd_gate_cache.CARGO_TARGETS_VARIABLE: "0"}
        names = {key: value for key, value in os.confstr_names.items() if key != "CS_DARWIN_USER_TEMP_DIR"}
        with mock.patch.object(os, "confstr_names", names), mock.patch.object(os, "confstr", confstr):
            self.assertEqual(sd_tmpdisk.system_temporary(), str(self.system))
            self.assertEqual(sd_gate_run.check_in_worktree(self.root, head, environ=env)["status"], "success")
        self.assertEqual([place.is_relative_to(self.system) for place in self.places()], [True, True])

    def test_a_test_run_puts_its_temp_dir_on_a_ready_disk(self) -> None:
        code = ("import os, pathlib, sys; sys.path.insert(0, 'bin'); import sd_tmpdisk; "
                f"sd_tmpdisk.ROOT = pathlib.Path({str(self.disk)!r}); sd_tmpdisk.ismount = lambda path: True; "
                "sd_tmpdisk.owned_inside = lambda path: False; import shutil, types; real = shutil.disk_usage; "
                "shutil.disk_usage = lambda path: types.SimpleNamespace(free=sd_tmpdisk.FREE) "
                "if pathlib.Path(path) == sd_tmpdisk.ROOT else real(path); import tests; print(os.environ['TMPDIR'])")
        done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                              env={**os.environ, "TMPDIR": str(self.outer)}, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(pathlib.Path(done.stdout.strip()).parent, self.disk, done.stdout)
        self.assertEqual(list(self.disk.iterdir()), [self.disk / sd_tmpdisk.READY], "its exit removes it")


class StaleGateWorktrees(Repository):
    """sd:2739: a killed gate skips its `finally`; the next gate start removes its worktree, never a live one's."""

    def left(self, owner: str, head: str, folder: pathlib.Path | None = None) -> pathlib.Path:
        """A worktree registered as `<folder>/tree`; by default a gate's folder in the temp dir, as a killed gate leaves it."""
        if folder is None:
            folder = pathlib.Path(tempfile.mkdtemp(prefix=f"{sd_gate_cache.GATE_PREFIX}{owner}-"))
            self.addCleanup(shutil.rmtree, folder, True)
        git(self.root, "worktree", "add", "-q", "--detach", str(folder / "tree"), head)
        return folder / "tree"

    def dead(self) -> str:
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        return str(gone.pid)

    def test_a_dead_gates_worktree_is_removed_at_the_next_gate_start(self) -> None:
        head = self.commit("check:\n\t@echo ok\n")
        tree = self.left(self.dead(), head)
        (tree.parent / sd_gate_cache.TEMPORARY).mkdir()
        self.assertEqual(sd_gate_run.check_in_worktree(self.root, head)["status"], "success")
        self.assertEqual((self.worktrees(), tree.parent.exists()), (1, False), "sd:3032: its check's TMPDIR goes too")

    def test_the_check_tmpdir_leaves_room_for_a_unix_socket_name(self) -> None:
        """sd:3086: macOS caps a socket path at 104 bytes; tsx's `tsx-<uid>/<pid>.pipe` under the old name made 108."""
        parent = tempfile.mkdtemp(prefix=f"{sd_gate_cache.GATE_PREFIX}{'9' * 7}-")
        self.addCleanup(shutil.rmtree, parent, True)
        temporary = sd_gate_cache.check_temporary(parent)
        self.assertLessEqual(len(temporary) - len(tempfile.gettempdir()), 33, temporary)

    def test_a_live_gates_worktree_and_one_that_names_no_pid_stay(self) -> None:
        head = self.commit("check:\n\t@echo ok\n")
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        trees = [self.left(str(live.pid), head), self.left("abc", head)]
        self.assertEqual(sd_gate_run.check_in_worktree(self.root, head)["status"], "success")
        self.assertEqual((self.worktrees(), [tree.exists() for tree in trees]), (3, [True, True]))

    def test_a_like_named_checkout_outside_the_temp_dir_or_past_the_pid_range_stays(self) -> None:
        """Review round 2: the name alone selected a user's checkout, and a 24-digit pid raised `OverflowError`."""
        head = self.commit("check:\n\t@echo ok\n")
        trees = [self.left("", head, self.root.parent / f"{sd_gate_cache.GATE_PREFIX}{self.dead()}-x"),
                 self.left("9" * 24, head)]
        self.assertEqual(sd_gate_run.check_in_worktree(self.root, head)["status"], "success")
        self.assertEqual((self.worktrees(), [tree.exists() for tree in trees]), (3, [True, True]))


class DocsScopeGate(Repository):
    """The merge gate passes the base branch, so a declared docs-only change runs the docs command (sd:2072)."""

    def test_a_docs_only_change_posts_docs_only(self) -> None:
        self.declare_row("check_scope", {"schema_version": 1, "docs_paths": ["docs/**"], "docs_command": ["true"]})
        self.commit("check:\n\t@false\n")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.md").write_text("a\n", encoding="utf-8")
        git(self.root, "add", "docs")
        git(self.root, "commit", "-q", "-m", "docs")
        head = git(self.root, "rev-parse", "HEAD")
        api = Recorder()
        result = sd_local_gate.local_gate(api, self.root, head, base="main")
        self.assertEqual((result["status"], result["summary"]), ("success", "sd-check pass (docs-only)"))
        self.assertTrue(api.posts[0][1]["description"].endswith(": sd-check pass (docs-only)"))
        without = sd_local_gate.local_gate(Recorder(), self.root, head)
        self.assertEqual(without["status"], "failure")


class PostHead(Repository):
    """`sd gate post --head SHA` (sd:1989): the merge gate's run and post, for a repository's own merge path.

    A repository that merges by its own automation (a Dependabot merge, a
    daily script) gets no `sd/local-gate` from `sd-ship merge`, so its
    required check never reports and every merge waits.
    """

    def test_the_named_commit_is_checked_and_posted_at_its_full_sha(self) -> None:
        head = self.commit("check:\n\t@echo ok\n")
        self.commit("check:\n\t@false\n")
        api = Recorder()
        result = sd_local_gate.post_head(self.root, head[:10], api=api)
        self.assertEqual((result["head"], result["status"]), (head, "success"))
        [(path, body)] = api.posts
        self.assertEqual((path, body["context"], body["state"]), (f"repos/o/r/statuses/{head}", "sd/local-gate", "success"))

    def test_a_failing_check_posts_failure(self) -> None:
        head = self.commit("check:\n\t@false\n")
        api = Recorder()
        self.assertEqual(sd_local_gate.post_head(self.root, head, api=api)["status"], "failure")
        self.assertEqual(api.posts[0][1]["state"], "failure")

    def test_a_name_that_is_no_commit_is_refused_and_nothing_is_posted(self) -> None:
        self.commit("check:\n\t@echo ok\n")
        api = Recorder()
        with self.assertRaisesRegex(Refusal, "names no commit in this checkout"):
            sd_local_gate.post_head(self.root, "f" * 40, api=api)
        self.assertEqual(api.posts, [])

    def docs_only_head(self) -> str:
        """A docs commit on top of a head whose full check fails, under a declared docs-only scope."""
        self.declare_row("check_scope", {"schema_version": 1, "docs_paths": ["docs/**"], "docs_command": ["true"]})
        self.commit("check:\n\t@false\n")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.md").write_text("a\n", encoding="utf-8")
        git(self.root, "add", "docs")
        git(self.root, "commit", "-q", "-m", "docs")
        return git(self.root, "rev-parse", "HEAD")

    def test_without_a_base_every_check_runs(self) -> None:
        """No --base: the PR's target is unknown, so no docs-only scope is guessed from origin/HEAD.

        A head bound for a release branch can carry code main already has plus
        a docs commit; against main it reads as docs-only, and its success
        would satisfy the release branch's required check unchecked.
        """
        result = sd_local_gate.post_head(self.root, self.docs_only_head(), api=Recorder())
        self.assertEqual(result["status"], "failure")

    def test_a_named_base_applies_the_docs_only_scope(self) -> None:
        """A declared docs-only scope applies as it does in the merge gate, against the named base."""
        result = sd_local_gate.post_head(self.root, self.docs_only_head(), base="main", api=Recorder())
        self.assertEqual((result["status"], result["summary"]), ("success", "sd-check pass (docs-only)"))

    def test_a_base_the_checkout_has_not_fetched_is_refused_and_nothing_is_posted(self) -> None:
        """`sd-check --base` with a missing ref fails, and that failure would be posted as the gate's."""
        head = self.commit("check:\n\t@echo ok\n")
        api = Recorder()
        with self.assertRaisesRegex(Refusal, "refs/remotes/origin/trunk"):
            sd_local_gate.post_head(self.root, head, base="trunk", api=api)
        self.assertEqual(api.posts, [])


class PostCommand(Repository):
    """`sd gate post` end to end, with a recording `gh` on `PATH` standing in for GitHub."""

    def sd(self, *args: str) -> subprocess.CompletedProcess:
        bindir = self.root.parent / "fakebin"
        bindir.mkdir(exist_ok=True)
        self.log = self.root.parent / "gh.log"
        fake = bindir / "gh"
        fake.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            f"open({str(self.log)!r}, 'a').write(json.dumps([sys.argv[1:], sys.stdin.read()]) + '\\n')\n"
            "print(json.dumps({'state': 'success'}))\n", encoding="utf-8")
        fake.chmod(0o755)
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""))
        return subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "sd"), "gate", "post", *args],
                              cwd=self.root, env=env, capture_output=True, text=True, timeout=600)

    def test_a_pass_is_posted_and_printed_as_json(self) -> None:
        git(self.root, "remote", "add", "origin", "https://github.com/o/r.git")
        head = self.commit("check:\n\t@echo ok\n")
        done = self.sd("--head", head)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["status"], "success")
        [(argv, body)] = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(argv[:4], ["api", f"repos/o/r/statuses/{head}", "--method", "POST"])
        self.assertEqual(json.loads(body)["context"], "sd/local-gate")

    def test_a_failure_is_posted_and_exits_1(self) -> None:
        git(self.root, "remote", "add", "origin", "https://github.com/o/r.git")
        head = self.commit("check:\n\t@false\n")
        done = self.sd("--head", head)
        self.assertEqual((done.returncode, json.loads(done.stdout)["status"]), (1, "failure"))

    def test_a_refusal_prints_no_json_and_posts_nothing(self) -> None:
        git(self.root, "remote", "add", "origin", "https://github.com/o/r.git")
        self.commit("check:\n\t@echo ok\n")
        done = self.sd("--head", "f" * 40)
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertIn("names no commit in this checkout", done.stderr)
        self.assertFalse(self.log.exists())


class CiMode(unittest.TestCase):
    """`sd_lib.repo_ci` answers `github` on every doubt, so ci=github is unchanged."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name).resolve()
        from sd_db import connect, initialise, upsert_repo

        self.database = self.home / ".local/share/sd/sd.db"
        initialise(self.database)
        self.connection = connect(self.database)
        self.addCleanup(self.connection.close)
        self.root = self.home / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q")
        upsert_repo(self.connection, str(self.root), remote=None)

    def columns(self) -> set[str]:
        return {row[1] for row in self.connection.execute("PRAGMA table_info(repo)")}

    def test_a_library_without_the_reader_answers_github(self) -> None:
        import sd_db.repos

        # A schema-15 database: the pinned library creates repo.ci, so drop it.
        if "ci" in self.columns():
            self.connection.execute("ALTER TABLE repo DROP COLUMN ci")
        self.assertNotIn("ci", self.columns())
        with mock.patch.object(sd_db.repos, "repo_ci", None, create=True):
            self.assertEqual(sd_lib.repo_ci(self.connection, self.root), "github")

    def test_the_librarys_local_answer_is_returned(self) -> None:
        import sd_db.repos

        with mock.patch.object(sd_db.repos, "repo_ci", lambda connection, path: "local", create=True):
            self.assertEqual(sd_lib.repo_ci(self.connection, self.root), "local")

    def test_the_policy_is_read_and_the_column_is_not(self) -> None:
        """pack sd:7366: `policy.ci.provider` is the setting; a column a raw write moved alone says nothing."""
        self.connection.execute("UPDATE repo SET ci = 'local'")
        self.assertEqual(sd_lib.repo_ci(self.connection, self.root), "github")
        self.connection.execute("""UPDATE repo SET declarations = '{"policy":{"ci":{"provider":"local"}}}'""")
        self.assertEqual(sd_lib.repo_ci(self.connection, self.root), "local")
        self.connection.execute("UPDATE repo SET declarations = ?", (
            '{"policy":{"ci":{"provider":"jenkins","url":"https://ci.example.test","job":"j"}}}',))
        self.assertEqual(sd_lib.repo_ci(self.connection, self.root), "github")

    def test_an_unknown_value_or_a_failing_read_answers_github(self) -> None:
        import sd_db.repos

        def broken(connection, path):
            raise RuntimeError("no such column: ci")

        for reader in (lambda connection, path: "sometimes", broken):
            with self.subTest(reader=reader), mock.patch.object(sd_db.repos, "repo_ci", reader, create=True):
                self.assertEqual(sd_lib.repo_ci(self.connection, self.root), "github")

    def test_ci_mode_with_no_database_answers_github(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.home / "elsewhere")}):
            self.assertEqual(sd_lib.ci_mode(self.root), "github")


if __name__ == "__main__":
    unittest.main()
