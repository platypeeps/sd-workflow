"""sd:1996. Every repository gate takes a machine-wide slot, not only the pack's own tests.

On 2026-09-28 nine `sd-ship` gates across repositories ran at once, the load
average reached 141 on 16 cores, and a repository's gate failed tests that
assert nothing about time. `sd-check` is where every `repo.ci = local` gate
passes, so it takes a slot from `bin/sd_gate_slots.py`, the one implementation
`.github/scripts/run-tests.sh` uses too. These run the real executable.
"""

from __future__ import annotations

import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.clean_env import clean_environment

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SD_CHECK = REPO_ROOT / "bin" / "sd-check"
SLOTS = REPO_ROOT / "bin" / "sd_gate_slots.py"
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_gate_slots  # noqa: E402
import sd_lib  # noqa: E402

PY = shlex.quote(sys.executable)

#: A check that counts itself in and out under a lock and keeps the highest
#: count it saw, so the test reads how many checks ran at once. It holds
#: until the test's release file appears, not for a fixed time: a 1.5 s
#: sleep let a load-stalled gate start after the others ended (sd:6798).
COUNTING_CHECK = """
import fcntl, os, pathlib, sys, time
counter, release = sys.argv[1], pathlib.Path(sys.argv[2])
def step(delta):
    with open(counter, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        now, peak = (int(part) for part in (handle.read().split() or ["0", "0"]))
        now += delta
        handle.seek(0)
        handle.truncate()
        handle.write(f"{now} {max(now, peak)}")
step(1)
end = time.time() + 120
while not release.exists() and time.time() < end:
    time.sleep(0.05)
step(-1)
"""

#: A check that fails unless its parent `sd-check` holds the slot and wrote its pid there.
HOLDER_CHECK = """
import fcntl, os, sys
lock = sys.argv[1]
with open(lock, "a") as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        sys.exit("the slot was free while the check ran")
if open(lock).read().split() != [str(os.getppid())]:
    sys.exit("the slot does not name the sd-check that holds it")
"""


#: A command that says it started, then holds until a release file appears.
BLOCKING = """
import pathlib, sys, time
pathlib.Path(sys.argv[1]).touch()
end = time.time() + 120
while not pathlib.Path(sys.argv[2]).exists() and time.time() < end:
    time.sleep(0.05)
"""


def wait_until(predicate, seconds: float = 60.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class Watched:
    """A process's output read as it arrives, and an event for its first waiting line (sd:2630).

    A test that releases the holder must wait for this event, not for the
    ticket: the ticket exists before the waiter reads who holds the slot, so
    a holder released on the ticket was gone by the time the line named it.
    """

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process, self.out, self.err = process, [], []
        self.waiting = threading.Event()
        self.readers = [threading.Thread(target=self.read, args=(stream, lines), daemon=True)
                        for stream, lines in ((process.stdout, self.out), (process.stderr, self.err))]
        for reader in self.readers:
            reader.start()

    def read(self, stream, lines: list[str]) -> None:
        for line in stream:
            lines.append(line)
            if lines is self.err and line.startswith("waiting for a gate slot"):
                self.waiting.set()

    def finish(self, timeout: float = 300) -> tuple[int, str, str]:
        code = self.process.wait(timeout=timeout)
        for reader in self.readers:
            reader.join(timeout)
        return code, "".join(self.out), "".join(self.err)


def slot_free(lock: pathlib.Path) -> bool:
    with open(lock, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
    return True


class GateSlotFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name).resolve()
        self.slots = self.tmp / "slots"

    def repo(self, name: str, check: str) -> pathlib.Path:
        root = self.tmp / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        (root / sd_lib.LOCAL_FILE_NAME).write_text(
            f"{sd_lib.LOCAL_BLOCK_START}\ncheck: {check}\n{sd_lib.LOCAL_BLOCK_END}\n", encoding="utf-8")
        return root

    def script(self, name: str, body: str) -> pathlib.Path:
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        return path

    def env(self, **overrides: str) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if key not in ("CI", "GITHUB_ACTIONS", sd_gate_slots.SLOTS_VARIABLE)}
        # The sd:2262 load rule is off: these tests measure the cap, not the machine's load.
        env.update(SD_GATE_SLOTS_DIR=str(self.slots), SD_GATE_SLOT_POLL="0.1", SD_GATE_LOAD_MAX="0",
                   SD_GATE_SETTLE_SECONDS="0", XDG_CONFIG_HOME=str(self.tmp / "config"))
        env.update(overrides)
        return env

    def start(self, root: pathlib.Path, env: dict[str, str], *args: str) -> subprocess.Popen[str]:
        return subprocess.Popen([sys.executable, str(SD_CHECK), "--json", *args], cwd=root, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def finish(self, process: subprocess.Popen[str]) -> tuple[int, dict, str]:
        out, err = process.communicate(timeout=300)
        return process.returncode, json.loads(out) if out.strip() else {}, err


class ConcurrentGates(GateSlotFixture):
    def test_n_concurrent_gates_never_run_more_checks_than_the_cap(self):
        counter, release = self.tmp / "counter", self.tmp / "release"
        self.addCleanup(release.touch)
        counting = self.script("count.py", COUNTING_CHECK)
        root = self.repo("repo", " ".join([PY, *(shlex.quote(str(part)) for part in (counting, counter, release))]))
        gates = [self.start(root, self.env(SD_GATE_SLOTS="2")) for _ in range(5)]
        # Two checks hold both slots and three gates queue behind them, however slow each start.
        self.assertTrue(wait_until(lambda: counter.exists() and counter.read_text().split()[:1] == ["2"]
                                   and len(list((self.slots / "queue").glob("*.ticket"))) == 3, 300),
                        "two checks never ran at once with three gates queued")
        release.touch()
        results = [self.finish(gate) for gate in gates]
        for code, report, err in results:
            self.assertEqual(code, 0, err)
            self.assertEqual(report["status"], "pass", report)
        now, peak = (int(part) for part in counter.read_text().split())
        self.assertEqual(now, 0)
        self.assertLessEqual(peak, 2, f"{peak} checks ran at once under a cap of 2")
        # Five gates on two slots: the cap was reached, so the test measured a queue.
        self.assertEqual(peak, 2)
        self.assertTrue(any("waiting for a gate slot" in err for _, _, err in results))
        self.assertTrue(all(slot_free(self.slots / f"slot.{index}.lock") for index in (1, 2)))

    def test_a_queued_gate_spends_its_timeout_waiting_and_reports_it(self):
        """The wait counts against `--timeout`, so the gate answers inside its caller's bound."""
        root = self.repo("repo", f"{PY} -c pass")
        self.slots.mkdir()
        with open(self.slots / "slot.1.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            code, report, err = self.finish(self.start(root, self.env(SD_GATE_SLOTS="1"), "--timeout", "2"))
            elapsed = time.monotonic() - started
        self.assertEqual(code, 1, err)
        self.assertIn("waiting for a gate slot", err)
        check = report["checks"][0]
        self.assertEqual((check["status"], check["exit_code"]), ("fail", None))
        self.assertEqual(check["reason"], "no gate slot came free within 2s")
        self.assertLess(elapsed, 60)

    def test_a_slot_bound_leaves_each_check_its_whole_timeout(self):
        """sd:2607: queued past `--timeout 4` under a slot bound, the checks still get their 4 s; without the bound, 1 s.

        sd:6724: the queue is a fake clock, so no load can eat the budget. The
        real queue and a real 4 s check failed twice at load 30-37: a 5 s stall
        in the check's start-up, simulated, failed it as (1, fail).
        """
        root = self.repo("repo", f"{PY} -c pass")
        loader = importlib.machinery.SourceFileLoader("sd_check_bound", str(SD_CHECK))
        check = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(check)
        args = check.build_parser().parse_args(["--json", "--timeout", "4", "--slot-timeout", "120"])
        # 4 s in the queue and a 5 s stall, none of it spent here.
        slot = mock.Mock(waited=4 + 5.0, path=None)
        budgets: list[int] = []
        with mock.patch.object(check.sd_gate_slots, "acquire", return_value=slot), \
                mock.patch.object(check, "execute", side_effect=lambda *a, **k: budgets.append(a[4]) or {}):
            result = check.gated_execute(root, sd_lib.detect_entrypoints(root), args, self.env(SD_GATE_SLOTS="1"))
        self.assertEqual(budgets, [4])
        self.assertEqual(result["gate_slot"]["waited_seconds"], 9.0)
        self.assertEqual(result["gate_slot"]["bound_seconds"], 120)
        self.assertIs(result["gate_slot"]["timed_out"], False)
        slot.release.assert_called_once_with()

    def test_a_slot_bound_ends_the_queue_on_its_own_clock(self):
        root = self.repo("repo", f"{PY} -c pass")
        self.slots.mkdir()
        with open(self.slots / "slot.1.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            started = time.monotonic()
            code, report, err = self.finish(self.start(root, self.env(SD_GATE_SLOTS="1"),
                                                       "--timeout", "600", "--slot-timeout", "2"))
            elapsed = time.monotonic() - started
        self.assertEqual(code, 1, err)
        self.assertEqual(report["checks"][0]["reason"], "no gate slot came free within 2s")
        # pack sd:7412: the lane puts back an entry whose wait ran out, so the report says it, not only the text.
        self.assertIs(report["gate_slot"]["timed_out"], True)
        self.assertLess(elapsed, 60)
        for bad in ("0", "-1", "soon"):
            with self.subTest(slot_timeout=bad):
                refused, _, err = self.finish(self.start(root, self.env(SD_GATE_SLOTS="1"), "--slot-timeout", bad))
                self.assertEqual(refused, 2, err)


class SlotOwnership(GateSlotFixture):
    def test_a_dead_holders_slot_is_reclaimed_without_a_wait(self):
        """A lock file naming a dead pid is not held: only the kernel lock counts."""
        self.slots.mkdir()
        lock = self.slots / "slot.1.lock"
        gone = subprocess.Popen(["true"])
        gone.wait()
        lock.write_text(f"{gone.pid}\n")
        holder = self.script("holder.py", HOLDER_CHECK)
        root = self.repo("repo", f"{PY} {shlex.quote(str(holder))} {shlex.quote(str(lock))}")
        code, report, err = self.finish(self.start(root, self.env(SD_GATE_SLOTS="1")))
        self.assertEqual(code, 0, f"{err}\n{report}")
        self.assertNotIn("waiting for a gate slot", err)
        self.assertEqual(report["gate_slot"]["path"], str(lock))
        self.assertTrue(slot_free(lock))

    def test_a_nested_gate_runs_inside_its_parents_slot(self):
        """The holder's checks see `SD_GATE_SLOTS=0`: a gate inside a gate never waits on its parent."""
        seen = self.tmp / "seen"
        inner = self.repo("inner", f"{PY} -c " + shlex.quote(
            f"import os; open({str(seen)!r}, 'w').write(os.environ.get('SD_GATE_SLOTS', 'unset'))"))
        outer = self.repo("outer", f"{PY} {shlex.quote(str(SD_CHECK))} -C {shlex.quote(str(inner))}")
        code, report, err = self.finish(self.start(outer, self.env(SD_GATE_SLOTS="1"), "--timeout", "120"))
        self.assertEqual(code, 0, f"{err}\n{report}")
        self.assertEqual(seen.read_text(), "0")
        self.assertEqual(report["gate_slot"]["slots"], 1)
        self.assertNotIn("waiting for a gate slot", report["checks"][0]["stderr"])

    def test_a_holder_hands_its_cap_to_the_checks_and_a_nested_gate_keeps_it(self):
        """sd:2607: the tests a gate runs size their workers to the pool, so the holder names its cap."""
        seen = self.tmp / "seen"
        inner = self.repo("inner", f"{PY} -c " + shlex.quote(
            f"import os; open({str(seen)!r}, 'w').write(os.environ.get('SD_GATE_POOL_SIZE', 'unset'))"))
        outer = self.repo("outer", f"{PY} {shlex.quote(str(SD_CHECK))} -C {shlex.quote(str(inner))}")
        code, report, err = self.finish(self.start(outer, self.env(SD_GATE_SLOTS="3"), "--timeout", "120"))
        self.assertEqual(code, 0, f"{err}\n{report}")
        self.assertEqual(seen.read_text(), "3")
        self.assertEqual(sd_gate_slots.holder_environment({"SD_GATE_POOL_SIZE": "3"}, 0),
                         {"SD_GATE_POOL_SIZE": "3", "SD_GATE_SLOTS": "0"})
        self.assertEqual(sd_gate_slots.holder_environment({}, 0), {"SD_GATE_SLOTS": "0"})

    def test_no_cap_and_a_dry_run_take_no_slot(self):
        root = self.repo("repo", f"{PY} -c pass")
        for args, env in ((("--dry-run",), self.env(SD_GATE_SLOTS="1")), ((), self.env(SD_GATE_SLOTS="0")),
                          ((), self.env(CI="true"))):
            with self.subTest(args=args):
                code, report, err = self.finish(self.start(root, env, *args))
                self.assertEqual(code, 0, err)
        self.assertFalse(self.slots.exists())


class OnePool(GateSlotFixture):
    """sd:2522. Gates from two repositories, started together, queue on one pool.

    The count is the machine's `sd.gate_slots` for every entry point: `sd-check`
    (and so every `sd-ship` gate) and the plain `run` a lane wraps around
    `make check`. No `SD_GATE_SLOTS` is set, so each reads the setting.
    """

    def setUp(self) -> None:
        super().setUp()
        config = self.tmp / "config" / "sd-ai-command-pack" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"config": {"sd": {"gate_slots": "1"}}}))
        self.started, self.release = self.tmp / "started", self.tmp / "release"
        self.blocking = self.script("blocking.py", BLOCKING)
        self.processes: list[subprocess.Popen[str]] = []
        self.addCleanup(self.reap)

    def reap(self) -> None:
        self.release.touch()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def plain_run(self, root: pathlib.Path, *command: str) -> subprocess.Popen[str]:
        process = subprocess.Popen([sys.executable, str(SLOTS), "run", "--label", "make check", "--", *command],
                                   cwd=root, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        self.processes.append(process)
        return process

    def test_a_gate_in_one_repository_waits_for_a_plain_run_in_another_and_names_it(self):
        first = self.repo("first", f"{PY} -c pass")
        ran = self.tmp / "second-ran"
        second = self.repo("second", f"{PY} -c " + shlex.quote(f"open({str(ran)!r}, 'w').close()"))
        holder = self.plain_run(first, sys.executable, str(self.blocking), str(self.started), str(self.release))
        self.assertTrue(wait_until(self.started.exists), holder.stderr)
        gate = self.start(second, self.env())
        self.processes.append(gate)
        watched = Watched(gate)
        self.assertTrue(watched.waiting.wait(60), "the second repository's gate never said it waits")
        self.assertFalse(ran.exists(), "the second gate ran while the first repository held the one slot")
        self.release.touch()
        code, out, err = watched.finish()
        report = json.loads(out) if out.strip() else {}
        self.assertEqual(code, 0, f"{err}\n{report}")
        self.assertEqual(report["gate_slot"]["slots"], 1)
        waiting = next(line for line in err.splitlines() if line.startswith("waiting for a gate slot"))
        self.assertIn(f"slot 1 held by make check (pid {holder.pid}) since ", waiting)
        self.assertIn(f" in {first}", waiting)
        self.assertRegex(waiting, r"since \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")

    def test_a_plain_run_in_one_repository_waits_for_a_gate_in_another_and_names_it(self):
        """sd:5409: an `sd-check` gate is a lane gate, and a plain run beside it waits its turn rather than refuse."""
        first = self.repo("first", f"{PY} {shlex.quote(str(self.blocking))} "
                                   f"{shlex.quote(str(self.started))} {shlex.quote(str(self.release))}")
        second = self.repo("second", f"{PY} -c pass")
        ran = self.tmp / "second-ran"
        gate = self.start(first, self.env())
        self.processes.append(gate)
        self.assertTrue(wait_until(self.started.exists), "the first repository's gate never started its check")
        watched = Watched(self.plain_run(second, sys.executable, "-c", f"open({str(ran)!r}, 'w').close()"))
        self.assertTrue(watched.waiting.wait(60), "the plain run never said it waits")
        self.assertFalse(ran.exists(), "the plain run ran while the gate held the one slot")
        self.release.touch()
        code, report, err = self.finish(gate)
        self.assertEqual(code, 0, f"{err}\n{report}")
        code, _, err = watched.finish(120)
        self.assertEqual(code, 0, err)
        self.assertTrue(ran.exists())
        waiting = next(line for line in err.splitlines() if line.startswith("waiting for a gate slot"))
        self.assertIn("slot 1 held by sd-check first", waiting)

    def test_the_shell_reads_the_count_the_gates_read(self):
        """`count` is what the Makefile hands `run-tests.sh`, so a plain `make test` joins the pool."""
        def count(**overrides: str) -> str:
            env = {**self.env(), **overrides}
            done = subprocess.run([sys.executable, str(SLOTS), "count"], env=env, capture_output=True,
                                  text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout.strip()
        self.assertEqual(count(), "1")
        self.assertEqual(count(SD_GATE_SLOTS="0"), "0")
        self.assertEqual(count(CI="true"), "0")
        self.assertEqual(count(XDG_CONFIG_HOME=str(self.tmp / "none")), str(sd_gate_slots.default_slots()))
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn('SD_GATE_SLOTS ?= $(shell "$(PYTHON)" bin/sd_gate_slots.py count', makefile)


class SlotCount(unittest.TestCase):
    def test_the_variable_then_ci_then_the_machine_setting_then_a_quarter_of_the_cores(self):
        configured = sd_gate_slots.configured
        self.assertEqual(configured({"SD_GATE_SLOTS": "3", "CI": "1"}, "7"), (3, "SD_GATE_SLOTS"))
        self.assertEqual(configured({"CI": "1"}, "7"), (0, "CI"))
        self.assertEqual(configured({}, "7"), (7, "sd.gate_slots"))
        self.assertEqual(configured({}, None), (sd_gate_slots.default_slots(), "default"))
        self.assertEqual([sd_gate_slots.default_slots(cores) for cores in (1, 4, 8, 16, 32)], [1, 1, 2, 4, 8])
        for bad in ("", "-1", "two", "٣"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                configured({"SD_GATE_SLOTS": bad}, None)

    def test_a_holder_caps_each_gate_at_its_share_of_the_cores(self):
        """sd:2726: 2 slots on 16 cores let one Rust gate drive the load to 185; the slot count bounds no gate's CPU."""
        self.assertEqual([sd_gate_slots.cpu_share(slots, 16) for slots in (1, 2, 3, 16, 32)], [16, 8, 5, 1, 1])
        share = sd_gate_slots.cpu_share(2)
        held = sd_gate_slots.holder_environment({"CARGO_BUILD_JOBS": "1", "RUST_TEST_THREADS": str(share + 1)}, 2)
        self.assertEqual((held["CARGO_BUILD_JOBS"], held["RUST_TEST_THREADS"]), ("1", str(share)))
        unset = sd_gate_slots.holder_environment({}, 2)
        self.assertEqual((unset["CARGO_BUILD_JOBS"], unset["RUST_TEST_THREADS"]), (str(share), str(share)))
        # sd:2872: cargo-nextest reads its own variable, not `RUST_TEST_THREADS`, so it ran on every core.
        self.assertEqual(sd_gate_slots.holder_environment({"NEXTEST_TEST_THREADS": "1"}, 2)["NEXTEST_TEST_THREADS"], "1")
        self.assertEqual(unset["NEXTEST_TEST_THREADS"], str(share))
        for value in ("0", "-1", "", "two", "٣", str(share)):
            with self.subTest(value=value):
                self.assertEqual(sd_gate_slots.thread_cap(value, share), value if value == str(share) else str(share))

    def test_a_number_of_any_length_never_raises_out_of_the_gate_slot_reads(self):
        """pack sd:7401: int() refuses an all-digit string past 4,300 digits with ValueError. A thread variable,
        a slot count, a pid in a lock file and a time in the queue state or a slot info file each took one."""
        share = 4
        huge = "9" * 5000
        for value in (huge, "9" * 4300, "9" * 19, "0" * 5000 + "3", "", "   ", " 3", "3 ", "0", "-3", "٣",
                      "1e3", None, "3", "4", "5"):
            with self.subTest(value=value if value is None or len(value) < 30 else f"{len(value)} chars"):
                shown = sd_gate_slots.thread_cap(value, share)
                kept = value in ("3", "4") or value == "0" * 5000 + "3"  # a lower value wins, zeros and all
                self.assertEqual(shown, value if kept else str(share))
        for value in (huge, "9" * 4300, "9" * 4301, "-1", "", " ", "٣"):
            with self.subTest(count=value[:12]), self.assertRaises(ValueError) as caught:
                sd_gate_slots.parse_count(value, "SD_GATE_SLOTS")
            self.assertIn("SD_GATE_SLOTS", str(caught.exception))
        self.assertEqual(sd_gate_slots.parse_count("0" * 5000 + "7", "x"), 7)
        for stamp in (10 ** 400, -(10 ** 400), 10 ** 4000, float("inf"), "9" * 5000, True, None, 12.5):
            seconds = sd_gate_slots.as_time(stamp)
            self.assertTrue(seconds is None or isinstance(seconds, float))
        self.assertEqual(sd_gate_slots.as_time(12), 12.0)
        self.assertIsNone(sd_gate_slots.as_time(10 ** 400))

    def test_a_lock_file_pid_or_time_past_the_float_or_digit_limit_leaves_the_holders_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            where = pathlib.Path(tmp)
            lock = where / "slot.1.lock"
            lock.write_text("9" * 5000 + "\n", encoding="utf-8")
            lock.with_suffix(".info").write_text('{"pid": 1, "since": %s, "label": "x"}' % ("9" * 400), encoding="utf-8")
            with open(lock, "r+") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                holders = sd_gate_slots.read_holders(where)
        self.assertEqual([(entry["slot"], entry["pid"]) for entry in holders], [(1, None)])
        state = {"low_since": 10 ** 400}
        rule = sd_gate_slots.LoadRule(limit=10.0, settle=60.0, source="test")
        self.assertIsInstance(sd_gate_slots.load_hold(state, rule, 1.0, 1.0, 1000.0), str)

    def test_the_machine_setting_is_a_declared_core_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / "sd-ai-command-pack" / "config.json"
            config.parent.mkdir()
            config.write_text(json.dumps({"config": {"sd": {"gate_slots": "3"}}}))
            self.assertEqual(sd_lib.core_setting("gate_slots", {"XDG_CONFIG_HOME": tmp}), "3")
            config.write_text(json.dumps({"config": {"sd": {"gate_slots": "three"}}}))
            with self.assertRaises(sd_lib.ConfigError):
                sd_lib.core_setting("gate_slots", {"XDG_CONFIG_HOME": tmp})

    def test_a_waiter_reports_while_queued_and_stops_at_its_deadline(self):
        with tempfile.TemporaryDirectory() as tmp, open(pathlib.Path(tmp) / "slot.1.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with open(pathlib.Path(tmp) / "slot.1.lock", "a") as mine:
                ticks = iter(range(0, 1000, 30))
                stream = io.StringIO()
                taken = sd_gate_slots.wait_for_slot([mine.fileno()], pathlib.Path(tmp), poll=0.001, stream=stream,
                                                    deadline=150, clock=lambda: float(next(ticks)))
        self.assertIsNone(taken)
        lines = stream.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("waiting for a gate slot: 1 of 1 in use"), lines)
        self.assertTrue(any(line.startswith("still waiting for a gate slot after") for line in lines), lines)
        self.assertTrue(lines[-1].startswith("gave up waiting for a gate slot after 150s at place 1 of 1 in the queue: "
                                             "1 of 1 in use; slot 1 held by"), lines)


class SignalForwarding(unittest.TestCase):
    def test_a_stop_signal_for_a_group_that_is_gone_is_not_an_error(self):
        """sd:2402. macOS answers `killpg` on a group that already exited with EPERM, not only ESRCH."""
        handlers: dict[int, object] = {}

        class Child:
            pid = 999999

            def wait(self) -> int:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
                return 0

        def record(signum: int, handler: object) -> object:
            handlers[signum] = handler
            return signal.SIG_DFL

        for error in (PermissionError, ProcessLookupError):
            with self.subTest(error=error.__name__), \
                    mock.patch.object(sd_gate_slots.subprocess, "Popen", return_value=Child()), \
                    mock.patch.object(sd_gate_slots.signal, "signal", side_effect=record), \
                    mock.patch.object(sd_gate_slots.os, "killpg", side_effect=error) as killpg:
                code = sd_gate_slots.run_gated(["true"], {}, slots=0, rule=sd_gate_slots.LoadRule(0, 0, "off"),
                                               stream=io.StringIO())
                self.assertEqual(code, 0)
                killpg.assert_called_once_with(Child.pid, signal.SIGTERM)


class ARefusalSaysItStartedNothing(unittest.TestCase):
    """Pack sd:7899: `run` passes the command's own exit code through, 125 too.

    A caller cannot tell a check that exited 125 from a gate that started
    nothing by the code, so every refusal ends its stderr with `NOT_STARTED`.
    """

    def run_gate(self, *argv: str, environ: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SLOTS), "run", *argv], capture_output=True, text=True,
                              env=clean_environment(**{"SD_GATE_SLOTS": "0", **(environ or {})}), timeout=60,
                              check=False, stdin=subprocess.DEVNULL)

    def test_a_run_with_no_command_says_it_started_nothing(self):
        done = self.run_gate("--")
        self.assertEqual(done.returncode, sd_gate_slots.RUN_NOT_STARTED)
        self.assertEqual(done.stderr.splitlines()[-1], sd_gate_slots.NOT_STARTED)

    def test_unreadable_settings_say_it_started_nothing(self):
        done = self.run_gate("--", "true", environ={"SD_GATE_SLOTS": "many"})
        self.assertEqual(done.returncode, sd_gate_slots.RUN_NOT_STARTED, done.stderr)
        self.assertEqual(done.stderr.splitlines()[-1], sd_gate_slots.NOT_STARTED)

    def test_a_command_that_exits_125_carries_no_refusal(self):
        done = self.run_gate("--", sys.executable, "-c", "import sys; sys.exit(125)")
        self.assertEqual(done.returncode, 125)
        self.assertNotIn(sd_gate_slots.NOT_STARTED, done.stderr)

    def test_the_build_run_reads_the_same_marker(self):
        sys.path.insert(0, str(REPO_ROOT / "lib"))
        from sd_db import build_run
        self.assertEqual(build_run.GATE_NOT_STARTED, sd_gate_slots.NOT_STARTED)


class WorkerSetting(unittest.TestCase):
    """Build review of sd:9925: a bad `sd.gate_workers` must not drop the gate's own limits."""

    def test_a_bad_worker_count_keeps_the_gate_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / "sd-ai-command-pack" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"config": {"sd": {"gate_workers": "0", "gate_slots": "1",
                                                            "gate_load_max": "4", "gate_settle_seconds": "90"}}}))
            values = sd_gate_slots.machine_settings({"XDG_CONFIG_HOME": tmp}, stream=io.StringIO())
        self.assertEqual((values["gate_slots"], values["gate_load_max"], values["gate_settle_seconds"]),
                         ("1", "4", "90"))
        self.assertIsNone(values["gate_workers"])


if __name__ == "__main__":
    unittest.main()

