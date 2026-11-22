"""sd:2262. One machine-wide queue in front of the gate slots: first in, first out, and a load rule.

On 2026-10-01 about 14 gates from three sessions each ran a hand-written copy
of one rule: start `make check` only when load1 is below 40 and no other runs,
checked twice 45 s apart. When load fell to 31, several waiters read the same
quiet moment and started together, and load went back to about 125. Nothing
made the read and the start one step, and nothing ordered the waiters.

`bin/sd_gate_slots.py` now admits only the head of one queue, under one lock,
and only when the load rule holds. These tests run the real file.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SLOTS = REPO_ROOT / "bin" / "sd_gate_slots.py"
SD = REPO_ROOT / "bin" / "sd"
SD_CHECK = REPO_ROOT / "bin" / "sd-check"
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_gate_slots  # noqa: E402
import sd_lib  # noqa: E402

#: Counts itself in and out under a lock and keeps the highest count it saw.
COUNTING = """
import fcntl, sys, time
counter = sys.argv[1]
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
time.sleep(1.0)
step(-1)
"""


def slot_free(lock: pathlib.Path) -> bool:
    with open(lock, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
    return True


def wait_for(predicate, timeout: float = 60.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class QueueFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name).resolve()
        self.slots = self.tmp / "slots"
        self.processes: list[subprocess.Popen] = []
        self.addCleanup(self.reap)

    def reap(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()

    def env(self, **overrides: str) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if key not in ("CI", "GITHUB_ACTIONS") and not key.startswith("SD_GATE_")}
        env.update(SD_GATE_SLOTS_DIR=str(self.slots), SD_GATE_SLOT_POLL="0.05", SD_GATE_LOAD_MAX="0",
                   SD_GATE_SETTLE_SECONDS="0", XDG_CONFIG_HOME=str(self.tmp / "config"))
        env.update(overrides)
        return env

    def run_gate(self, env: dict[str, str], *command: str, label: str | None = None,
                 stderr=subprocess.PIPE) -> subprocess.Popen:
        argv = [sys.executable, str(SLOTS), "run"]
        if label is not None:
            argv += ["--label", label]
        process = subprocess.Popen([*argv, "--", *command], env=env, stdout=subprocess.PIPE, stderr=stderr,
                                   text=True)
        self.processes.append(process)
        return process

    def tickets(self) -> list[pathlib.Path]:
        return sorted((self.slots / "queue").glob("*.ticket")) if (self.slots / "queue").is_dir() else []

    def hold(self, index: int = 1) -> subprocess.Popen:
        """A process that holds `slot.<index>.lock` until it is killed."""
        self.slots.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl, sys, time\nh = open(sys.argv[1], 'a')\n"
                                   "fcntl.flock(h, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(600)",
                                   str(self.slots / f"slot.{index}.lock")], stdout=subprocess.PIPE, text=True)
        self.processes.append(holder)
        self.assertEqual(holder.stdout.readline(), "held\n")
        return holder


class FirstInFirstOut(QueueFixture):
    def test_waiters_start_in_the_order_they_queued(self):
        """Six `sd-check` gates queue one by one behind a held slot; they start in that order."""
        order = self.tmp / "order"
        root = self.tmp / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        holder = self.hold()
        gates = []
        for name in "abcdef":
            check = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
                f"open({str(order)!r}, 'a').write({name!r})")
            (root / sd_lib.LOCAL_FILE_NAME).write_text(
                f"{sd_lib.LOCAL_BLOCK_START}\ncheck: {check}\n{sd_lib.LOCAL_BLOCK_END}\n", encoding="utf-8")
            gate = subprocess.Popen([sys.executable, str(SD_CHECK), "--json"], cwd=root,
                                    env=self.env(SD_GATE_SLOTS="1"), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            self.processes.append(gate)
            gates.append(gate)
            # The gate read its check before it queued; the next one may now rewrite the file.
            # A start slower than the wait would run the next gate's check (sd:6988), so it must hold.
            self.assertTrue(wait_for(lambda: len(self.tickets()) == len(gates)), f"gate {name} never queued")
            time.sleep(0.3)
        holder.kill()
        holder.wait()
        for gate in gates:
            _, err = gate.communicate(timeout=120)
            self.assertEqual(gate.returncode, 0, err)
        self.assertEqual(order.read_text(), "abcdef")

    def test_a_dead_waiter_leaves_the_queue_and_does_not_block_it(self):
        holder = self.hold()
        dead = self.run_gate(self.env(SD_GATE_SLOTS="1"), sys.executable, "-c", "pass")
        self.assertTrue(wait_for(lambda: len(self.tickets()) == 1))
        dead.kill()
        dead.wait()
        behind = self.run_gate(self.env(SD_GATE_SLOTS="1"), sys.executable, "-c", "pass")
        self.assertTrue(wait_for(lambda: len(self.tickets()) == 1 and self.tickets()[0].name != "000000000001.ticket"),
                        [path.name for path in self.tickets()])
        holder.kill()
        holder.wait()
        _, err = behind.communicate(timeout=60)
        self.assertEqual(behind.returncode, 0, err)
        self.assertEqual(self.tickets(), [])


class Concurrency(QueueFixture):
    def test_n_waiters_at_once_never_run_more_than_the_slot_count(self):
        counting = self.tmp / "count.py"
        counting.write_text(COUNTING, encoding="utf-8")
        for cap in (1, 2):
            with self.subTest(slots=cap):
                counter = self.tmp / f"counter-{cap}"
                gates = [self.run_gate(self.env(SD_GATE_SLOTS=str(cap)), sys.executable, str(counting), str(counter))
                         for _ in range(5)]
                for gate in gates:
                    _, err = gate.communicate(timeout=120)
                    self.assertEqual(gate.returncode, 0, err)
                now, peak = (int(part) for part in counter.read_text().split())
                self.assertEqual((now, peak), (0, cap), f"{peak} commands ran at once under a cap of {cap}")
                self.assertTrue(all(slot_free(self.slots / f"slot.{index}.lock") for index in range(1, cap + 1)))
                self.assertEqual(self.tickets(), [])


class Run(QueueFixture):
    def test_run_exits_with_the_commands_code_and_frees_its_slot(self):
        seen = self.tmp / "seen"
        gate = self.run_gate(self.env(SD_GATE_SLOTS="1"), sys.executable, "-c",
                             f"import os, sys; open({str(seen)!r}, 'w').write(os.environ['SD_GATE_SLOTS']); sys.exit(7)")
        _, err = gate.communicate(timeout=60)
        self.assertEqual(gate.returncode, 7, err)
        self.assertEqual(seen.read_text(), "0", "the command must run with SD_GATE_SLOTS=0")
        self.assertTrue(slot_free(self.slots / "slot.1.lock"))

    def max_wait(self, seconds: str, ran: pathlib.Path) -> subprocess.Popen:
        argv = [sys.executable, str(SD), "gate", "run", "--max-wait", seconds, "--label", "bounded", "--",
                sys.executable, "-c", f"open({str(ran)!r}, 'w').close()"]
        process = subprocess.Popen(argv, env=self.env(SD_GATE_SLOTS="1"), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        return process

    def test_max_wait_gives_up_with_its_own_code_names_the_queue_and_leaves_nothing(self):
        """sd:3996. A builder's run waited over 550 s for a slot and outlived its 600 s bound; `timeout` exits 124."""
        holder = self.run_gate(self.env(SD_GATE_SLOTS="1"), sys.executable, "-c", "import time; time.sleep(600)",
                               label="holder-x")
        lock = self.slots / "slot.1.lock"
        self.assertTrue(wait_for(lambda: lock.exists() and not slot_free(lock)))
        ran = self.tmp / "ran"
        _, err = self.max_wait("1", ran).communicate(timeout=60)
        self.assertNotEqual(sd_gate_slots.RUN_NO_SLOT, 124, "timeout(1) owns 124")
        self.assertEqual(self.processes[-1].returncode, sd_gate_slots.RUN_NO_SLOT, err)
        self.assertRegex(err, r"gave up waiting for a gate slot after [0-9]+s at place 1 of 1 in the queue: "
                              r"1 of 1 in use; slot 1 held by holder-x")
        head = self.run_gate(self.env(SD_GATE_SLOTS="1"), sys.executable, "-c", "pass", label="head")
        self.assertTrue(wait_for(lambda: len(self.tickets()) == 1))
        _, err = self.max_wait("1", ran).communicate(timeout=60)
        self.assertEqual(self.processes[-1].returncode, sd_gate_slots.RUN_NO_SLOT, err)
        self.assertRegex(err, r"gave up waiting for a gate slot after [0-9]+s at place 2 of 2 in the queue; "
                              r"slot 1 held by holder-x")
        self.assertFalse(ran.exists(), "a run that gave up must not start its command")
        self.assertEqual(len(self.tickets()), 1, "only the head's ticket may stay")
        holder.kill()
        holder.wait()
        _, err = head.communicate(timeout=60)
        self.assertEqual(head.returncode, 0, err)
        self.assertEqual(self.tickets(), [])
        self.assertTrue(slot_free(lock))

    def test_a_slot_that_frees_inside_max_wait_runs_the_command(self):
        holder = self.hold()
        ran = self.tmp / "ran"
        waiter = self.max_wait("60", ran)
        self.assertTrue(wait_for(lambda: len(self.tickets()) == 1))
        holder.kill()
        holder.wait()
        _, err = waiter.communicate(timeout=60)
        self.assertEqual(waiter.returncode, 0, err)
        self.assertTrue(ran.exists())
        self.assertEqual(self.tickets(), [])

    def test_max_wait_takes_a_finite_number_and_the_old_timeout_flag_is_refused(self):
        ran = self.tmp / "ran"
        for flag in (["--max-wait", "nan"], ["--max-wait", "-1"], ["--timeout", "1"]):
            with self.subTest(flag=flag):
                argv = [sys.executable, str(SD), "gate", "run", *flag, "--", sys.executable, "-c",
                        f"open({str(ran)!r}, 'w').close()"]
                done = subprocess.run(argv, env=self.env(SD_GATE_SLOTS="1"), capture_output=True, text=True,
                                      timeout=60)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertFalse(ran.exists())

    def test_a_sigkilled_holder_frees_its_slot_for_the_next_waiter(self):
        holder = self.run_gate(self.env(SD_GATE_SLOTS="1"), sys.executable, "-c", "import time; time.sleep(600)")
        lock = self.slots / "slot.1.lock"
        self.assertTrue(wait_for(lambda: lock.exists() and not slot_free(lock)))
        waiter = self.run_gate(self.env(SD_GATE_SLOTS="1"), sys.executable, "-c", "pass")
        self.assertTrue(wait_for(lambda: len(self.tickets()) == 1))
        holder.kill()
        holder.wait()
        _, err = waiter.communicate(timeout=60)
        self.assertEqual(waiter.returncode, 0, err)

    def test_a_signal_reaches_the_command_and_frees_the_slot(self):
        started = self.tmp / "started"
        got = self.tmp / "got"
        body = (f"import signal, sys, time\n"
                f"def on(signum, frame):\n    open({str(got)!r}, 'w').write(str(signum)); sys.exit(0)\n"
                f"signal.signal(signal.SIGTERM, on)\nopen({str(started)!r}, 'w').close()\ntime.sleep(600)\n")
        gate = self.run_gate(self.env(SD_GATE_SLOTS="1"), sys.executable, "-c", body)
        self.assertTrue(wait_for(started.exists))
        gate.send_signal(signal.SIGTERM)
        _, err = gate.communicate(timeout=60)
        self.assertEqual(got.read_text(), str(int(signal.SIGTERM)))
        self.assertEqual(gate.returncode, 0, err)
        self.assertTrue(slot_free(self.slots / "slot.1.lock"))

    def test_sd_gate_run_reads_the_machine_settings(self):
        config = self.tmp / "config" / "sd-ai-command-pack" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"config": {"sd": {"gate_slots": "1", "gate_load_max": "0",
                                                        "gate_settle_seconds": "0"}}}))
        env = self.env()
        for name in ("SD_GATE_LOAD_MAX", "SD_GATE_SETTLE_SECONDS"):
            env.pop(name)
        done = subprocess.run([sys.executable, str(SD), "gate", "run", "--", sys.executable, "-c", "pass"],
                              env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.slots / "slot.1.lock").exists())
        self.assertFalse((self.slots / "slot.2.lock").exists())


class Status(QueueFixture):
    def test_status_names_holders_and_waiters_with_their_place(self):
        holder = self.run_gate(self.env(SD_GATE_SLOTS="1"), sys.executable, "-c", "import time; time.sleep(600)",
                               label="holder-gate")
        lock = self.slots / "slot.1.lock"
        self.assertTrue(wait_for(lambda: lock.exists() and not slot_free(lock)))
        self.assertTrue(wait_for(lambda: (self.slots / "slot.1.info").exists()))
        first = self.run_gate(self.env(SD_GATE_SLOTS="1"), sys.executable, "-c", "pass", label="first-waiter")
        self.assertTrue(wait_for(lambda: len(self.tickets()) == 1))
        second = self.run_gate(self.env(SD_GATE_SLOTS="1"), sys.executable, "-c", "pass", label="second-waiter")
        self.assertTrue(wait_for(lambda: len(self.tickets()) == 2))
        shown = subprocess.run([sys.executable, str(SLOTS), "status", "--json"], env=self.env(SD_GATE_SLOTS="1"),
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        report = json.loads(shown.stdout)
        self.assertEqual([(entry["slot"], entry["pid"], entry["label"]) for entry in report["holders"]],
                         [(1, holder.pid, "holder-gate")])
        self.assertEqual([(entry["place"], entry["pid"], entry["label"]) for entry in report["waiters"]],
                         [(1, first.pid, "first-waiter"), (2, second.pid, "second-waiter")])
        for entry in report["holders"] + report["waiters"]:
            self.assertRegex(entry["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        text = subprocess.run([sys.executable, str(SLOTS), "status"], env=self.env(SD_GATE_SLOTS="1"),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(text.returncode, 0, text.stderr)
        for word in ("holder-gate", "first-waiter", "second-waiter", str(holder.pid)):
            self.assertIn(word, text.stdout)
        holder.kill()
        holder.wait()
        for gate in (first, second):
            gate.communicate(timeout=60)
            self.assertEqual(gate.returncode, 0)


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


#: The module's own tick reader: LoadRule replaces it with a FakeCpu.
REAL_CPU_TICKS = sd_gate_slots.cpu_ticks


class FakeCpu:
    """CPU ticks that advance with a fake wall clock, at a busy share the test sets."""

    def __init__(self, wall: Clock) -> None:
        self.wall, self.idle, self.total = wall, 0, 0

    def run(self, seconds: float, share: float) -> None:
        self.idle += round((1 - share) * 100 * seconds)
        self.total += round(100 * seconds)
        self.wall.now += seconds

    def ticks(self) -> tuple[int, int]:
        return self.idle, self.total


class LoadRule(unittest.TestCase):
    """The admission decision itself, with a fake clock and a fake load average."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.where = pathlib.Path(tmp.name)
        self.lock = open(self.where / "slot.1.lock", "a")
        self.addCleanup(self.lock.close)
        self.queue = sd_gate_slots.Queue(self.where)
        self.wall = Clock()
        self.load = (1.0, 1.0, 1.0)
        self.cpu = FakeCpu(self.wall)
        patch = mock.patch.object(sd_gate_slots, "cpu_ticks", self.cpu.ticks)
        patch.start()
        self.addCleanup(patch.stop)

    def admit(self, ticket, rule):
        return self.queue.admit(ticket, [self.lock.fileno()], rule, loadavg=lambda: self.load, wall=self.wall,
                                 busy=lambda: None)

    def release(self) -> None:
        fcntl.flock(self.lock, fcntl.LOCK_UN)

    def test_load1_at_or_above_the_limit_admits_nobody(self):
        rule = sd_gate_slots.LoadRule(limit=40.0, settle=0.0, source="test")
        ticket = self.queue.enter("gate", "/", wall=self.wall)
        self.load = (40.0, 10.0, 10.0)
        taken, reason = self.admit(ticket, rule)
        self.assertIsNone(taken)
        self.assertIn("load 40.0", reason)
        self.load = (39.9, 10.0, 10.0)
        self.assertEqual(self.admit(ticket, rule)[0], 0)

    def busy_admit(self, ticket, share):
        """One admission under limit 24 on 16 cores with load 300, the CPU sample `share`."""
        rule = sd_gate_slots.LoadRule(limit=24.0, settle=0.0, source="test")
        with mock.patch.object(sd_gate_slots.os, "cpu_count", lambda: 16):
            return self.queue.admit(ticket, [self.lock.fileno()], rule, loadavg=lambda: (300.0, 300.0, 300.0),
                                    wall=self.wall, busy=lambda: share)

    def test_a_load_the_cpu_does_not_carry_admits(self):
        """sd:9946: a simulator held load5 at 277-458 with the CPU 37% idle; the limit reads CPU use then."""
        ticket = self.queue.enter("gate", "/", wall=self.wall)
        self.assertIn("load 300.0", self.busy_admit(ticket, None)[1], "an unread CPU keeps load5")
        self.cpu.run(60, 0.63)
        self.assertIn("load 300.0", self.busy_admit(ticket, 0.95)[1], "a busy sample now")
        self.cpu.run(60, 0.63)
        # Pinned: on 40 cores 0.63 busy counts 25.2, over the limit of 24.
        self.assertEqual(self.busy_admit(ticket, 0.63)[0], 0)

    def test_one_short_cpu_sample_does_not_drop_the_counted_load(self):
        """Lane blocker: 16 cores, limit 24, load5 300, and one 63% sample admitted at once."""
        ticket = self.queue.enter("gate", "/", wall=self.wall)
        self.assertIn("load 300.0", self.busy_admit(ticket, 0.63)[1], "one sample")
        self.cpu.run(30, 0.63)
        self.assertIn("load 300.0", self.busy_admit(ticket, 0.63)[1], "30 s of samples")
        self.cpu.run(30, 0.63)
        self.assertEqual(self.busy_admit(ticket, 0.63)[0], 0, "a minute at 63%")

    def test_two_samples_far_apart_do_not_hide_the_busy_time_between(self):
        """Review blocker: two 63% samples 119 s apart, nothing read between, counted load 300 as 10.08."""
        ticket = self.queue.enter("gate", "/", wall=self.wall)
        self.assertIn("load 300.0", self.busy_admit(ticket, 0.63)[1])
        self.cpu.run(119, 1.0)
        self.assertIn("load 300.0", self.busy_admit(ticket, 0.63)[1], "119 s fully busy, unobserved")

    def test_probes_five_minutes_apart_read_the_cpu_between(self):
        """Review blocker: a probe every five minutes discarded the last one and always read load 300."""
        ticket = self.queue.enter("gate", "/", wall=self.wall)
        self.assertIn("load 300.0", self.busy_admit(ticket, 0.63)[1])
        self.cpu.run(300, 0.63)
        self.assertEqual(self.busy_admit(ticket, 0.63)[0], 0)

    def test_the_busy_history_drops_bad_rows_a_clock_set_back_and_a_reboot(self):
        now = self.wall.now
        history = self.where / sd_gate_slots.CPU_HISTORY
        self.cpu.run(0, 0)
        history.write_text(f'[[{now - 61}, "x", 1], [{now + 5}, 0, 0], ["y", 1, 2], 7, [{now - 61}, 5, 3]]',
                           encoding="utf-8")
        self.assertIsNone(sd_gate_slots.steady_busy(self.where, 0.3, now), "no good row")
        history.write_text("not json\n", encoding="utf-8")
        self.assertIsNone(sd_gate_slots.steady_busy(self.where, 0.3, now))
        self.cpu.run(61, 0.5)
        self.assertEqual(sd_gate_slots.steady_busy(self.where, 0.3, self.wall.now), 0.5)
        self.assertIsNone(sd_gate_slots.steady_busy(self.where, None, self.wall.now), "an unread sample")
        self.assertIsNone(sd_gate_slots.steady_busy(self.where, 0.3, now - 120), "the clock went back")
        self.cpu.idle, self.cpu.total = 0, 0
        self.cpu.run(30, 0.5)
        self.assertIsNone(sd_gate_slots.steady_busy(self.where, 0.3, self.wall.now), "ticks fell: a reboot")
        self.cpu.run(sd_gate_slots.CPU_HISTORY_SECONDS + 1, 0.5)
        self.assertIsNone(sd_gate_slots.steady_busy(self.where, 0.3, self.wall.now), "every row too old")
        with mock.patch.object(sd_gate_slots, "cpu_ticks", lambda: None):
            self.cpu.run(61, 0.5)
            self.assertIsNone(sd_gate_slots.steady_busy(self.where, 0.3, self.wall.now), "unread ticks")

    def test_the_cpu_sample_waits_outside_the_queue_lock(self):
        """Review finding: a 1.1 s sample under the admission lock held every enter, leave and status."""
        rule = sd_gate_slots.LoadRule(limit=24.0, settle=0.0, source="test")
        ticket = self.queue.enter("gate", "/", wall=self.wall)
        free = []

        def busy():
            fd = os.open(self.where / sd_gate_slots.QUEUE_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                free.append(sd_gate_slots.try_lock(fd))
            finally:
                os.close(fd)
            return None
        self.queue.admit(ticket, [self.lock.fileno()], rule, loadavg=lambda: self.load, wall=self.wall,
                         busy=busy)
        self.assertEqual(free, [True])

    def test_cpu_busy_reads_two_tick_samples(self):
        samples = iter([(100, 200), (130, 300)])
        sd_gate_slots.last_cpu_sample = None
        self.assertAlmostEqual(sd_gate_slots.cpu_busy(lambda: next(samples), lambda _: None), 0.7)
        for broken in ([None], [(100, 200), (100, 200)], [(100, 200), (150, 240)]):
            sd_gate_slots.last_cpu_sample = None
            self.assertIsNone(sd_gate_slots.cpu_busy(iter(broken).__next__, lambda _: None), broken)
        sd_gate_slots.last_cpu_sample = None
        self.assertEqual(sd_gate_slots.counted_load(300.0, 0.63, cores=16), 0.63 * 16)
        self.assertEqual(sd_gate_slots.counted_load(300.0, 0.9, cores=16), 300.0)
        self.assertEqual(sd_gate_slots.counted_load(5.0, 0.5, cores=16), 5.0)
        self.assertEqual(sd_gate_slots.counted_load(300.0, float("nan"), cores=16), 300.0)

    def test_cpu_busy_spans_a_tick_update(self):
        """macOS updates its CPU ticks about once a second: a 0.25 s gap often read no change and fell
        back to load5. A poller's previous sample, 1 to 60 s old, stands in for the wait."""
        now = [0.1]

        def ticks():
            second = int(now[0])
            return 30 * second, 100 * second

        def pause(seconds):
            now[0] += seconds

        sd_gate_slots.last_cpu_sample = None
        self.assertAlmostEqual(sd_gate_slots.cpu_busy(ticks, pause, clock=lambda: now[0]), 0.7)
        waited = now[0]
        now[0] += 5
        self.assertAlmostEqual(sd_gate_slots.cpu_busy(ticks, pause, clock=lambda: now[0]), 0.7)
        self.assertEqual(now[0], waited + 5, "a 5 s old sample needs no wait")
        now[0] += 500
        self.assertAlmostEqual(sd_gate_slots.cpu_busy(ticks, pause, clock=lambda: now[0]), 0.7)
        self.assertGreater(now[0], waited + 505, "a stale sample is not reused")
        sd_gate_slots.last_cpu_sample = None

    def test_cpu_ticks_takes_the_host_port_once(self):
        """Each mach_host_self() call adds a reference to the host port; a gate wait polls for hours."""
        calls = []

        class System:
            class mach_host_self:  # noqa: N801 -- the C name
                restype = None

                def __new__(cls):
                    calls.append(1)
                    return 7

            @staticmethod
            def host_statistics(port, flavor, info, count):
                info[:] = [1, 2, 3, 4]
                return 0

        sd_gate_slots.host_port.cache_clear()
        with mock.patch.object(sd_gate_slots.sys, "platform", "darwin"), \
                mock.patch("ctypes.CDLL", lambda _: System):
            self.assertEqual(REAL_CPU_TICKS(), (3, 10))
            self.assertEqual(REAL_CPU_TICKS(), (3, 10))
        sd_gate_slots.host_port.cache_clear()
        self.assertEqual(len(calls), 1)

    def test_cpu_ticks_without_ctypes_is_unread(self):
        sd_gate_slots.host_port.cache_clear()
        with mock.patch.object(sd_gate_slots.sys, "platform", "darwin"), \
                mock.patch.dict(sys.modules, {"ctypes": None}):
            self.assertIsNone(REAL_CPU_TICKS())
        sd_gate_slots.host_port.cache_clear()

    def test_status_reports_the_counted_load(self):
        for seconds in (0, 60):
            self.cpu.run(seconds, 0.5)
            report = sd_gate_slots.snapshot(self.where, 1, sd_gate_slots.LoadRule(24.0, 0.0, "test"),
                                            loadavg=lambda: (300.0, 300.0, 300.0), busy=lambda: 0.5,
                                            wall=self.wall)
        self.assertEqual((report["cpu_busy"], report["load_counted"]),
                         (0.5, round(0.5 * (os.cpu_count() or 1), 2)))

    def test_a_falling_load1_must_stay_low_for_the_settle_time_while_load5_is_high(self):
        """The 2026-10-01 case: load1 at 31 while load5 was still far above 40."""
        rule = sd_gate_slots.LoadRule(limit=40.0, settle=45.0, source="test")
        ticket = self.queue.enter("gate", "/", wall=self.wall)
        self.load = (31.0, 90.0, 110.0)
        self.assertIsNone(self.admit(ticket, rule)[0])
        self.wall.now += 30
        self.assertIsNone(self.admit(ticket, rule)[0])
        self.load = (41.0, 85.0, 110.0)
        self.wall.now += 10
        self.assertIsNone(self.admit(ticket, rule)[0], "a sample at the limit restarts the settle time")
        self.load = (31.0, 80.0, 110.0)
        self.wall.now += 10
        self.assertIsNone(self.admit(ticket, rule)[0])
        self.wall.now += 44
        self.assertIsNone(self.admit(ticket, rule)[0])
        self.wall.now += 1
        self.assertEqual(self.admit(ticket, rule)[0], 0)

    def test_the_default_rule_admits_by_count_at_a_disk_bound_load(self):
        """sd:2607: macOS counted disk waits into a load of 124 while most cores idled; only the count holds a gate back."""
        rule = sd_gate_slots.load_rule({}, None, None)
        self.assertEqual(rule[:2], (0.0, 45.0))
        ticket = self.queue.enter("gate", "/", wall=self.wall)
        self.load = (124.0, 124.0, 110.0)
        self.assertEqual(self.admit(ticket, rule), (0, ""))
        self.release()
        second = self.queue.enter("second", "/", wall=self.wall)
        self.wall.now += 44
        self.assertIn("starts are 45s apart", self.admit(second, rule)[1], "the start spacing stays")
        self.wall.now += 1
        self.assertEqual(self.admit(second, rule)[0], 0)

    def test_an_idle_machine_admits_the_head_at_once(self):
        rule = sd_gate_slots.LoadRule(limit=40.0, settle=45.0, source="test")
        ticket = self.queue.enter("gate", "/", wall=self.wall)
        self.assertEqual(self.admit(ticket, rule)[0], 0)

    def test_two_admissions_are_at_least_the_settle_time_apart(self):
        rule = sd_gate_slots.LoadRule(limit=40.0, settle=45.0, source="test")
        first = self.queue.enter("first", "/", wall=self.wall)
        self.assertEqual(self.admit(first, rule)[0], 0)
        self.release()
        second = self.queue.enter("second", "/", wall=self.wall)
        self.wall.now += 44
        taken, reason = self.admit(second, rule)
        self.assertIsNone(taken)
        self.assertIn("apart", reason)
        self.wall.now += 1
        self.assertEqual(self.admit(second, rule)[0], 0)

    def test_only_the_head_is_admitted_even_with_a_free_slot(self):
        rule = sd_gate_slots.LoadRule(limit=0.0, settle=0.0, source="test")
        head = self.queue.enter("head", "/", wall=self.wall)
        behind = self.queue.enter("behind", "/", wall=self.wall)
        taken, reason = self.admit(behind, rule)
        self.assertIsNone(taken)
        self.assertIn("place 2 of 2", reason)
        self.assertEqual(self.admit(head, rule)[0], 0)
        self.release()
        self.assertEqual(self.admit(behind, rule)[0], 0)

    def test_a_damaged_state_file_does_not_stop_the_queue(self):
        first = self.queue.enter("first", "/", wall=self.wall)
        (self.where / sd_gate_slots.QUEUE_STATE).write_text('{"next": "x"}')
        second = self.queue.enter("second", "/", wall=self.wall)
        self.assertEqual(second.seq, first.seq + 1)
        (self.where / sd_gate_slots.QUEUE_STATE).write_text("not json")
        self.assertEqual(self.queue.enter("third", "/", wall=self.wall).seq, second.seq + 1)

    def other(self, ticket, rule):
        """A second waiter's poll, with its own rule, while `ticket` heads the queue."""
        return self.queue.admit(ticket, [self.lock.fileno()], rule, loadavg=lambda: self.load, wall=self.wall,
                                 busy=lambda: None)

    def test_a_stricter_waiter_behind_the_head_cannot_hold_it_back(self):
        """Review finding: a waiter with limit 20 kept clearing the head's low-load record at load1 30."""
        head_rule = sd_gate_slots.LoadRule(limit=40.0, settle=45.0, source="test")
        strict = sd_gate_slots.LoadRule(limit=20.0, settle=45.0, source="test")
        head = self.queue.enter("head", "/", wall=self.wall)
        behind = self.queue.enter("behind", "/", wall=self.wall)
        self.load = (30.0, 90.0, 90.0)
        self.assertIsNone(self.admit(head, head_rule)[0])
        for _ in range(9):
            self.wall.now += 5
            self.assertIsNone(self.other(behind, strict)[0])
            if self.wall.now < 1_000_045.0:
                self.assertIsNone(self.admit(head, head_rule)[0])
        self.assertEqual(self.admit(head, head_rule)[0], 0, "the head waited past its settle time")

    def test_a_waiter_with_no_load_rule_cannot_shorten_the_heads_settle_time(self):
        """Review finding: a waiter with the load rule off kept a low-load record alive through high load."""
        head_rule = sd_gate_slots.LoadRule(limit=40.0, settle=45.0, source="test")
        off = sd_gate_slots.LoadRule(limit=0.0, settle=0.0, source="test")
        head = self.queue.enter("head", "/", wall=self.wall)
        behind = self.queue.enter("behind", "/", wall=self.wall)
        self.load = (50.0, 90.0, 90.0)
        self.assertIsNone(self.admit(head, head_rule)[0])
        for _ in range(9):
            self.wall.now += 5
            self.assertIsNone(self.other(behind, off)[0])
        self.load = (30.0, 90.0, 90.0)
        self.assertIsNone(self.admit(head, head_rule)[0])
        self.wall.now += 10
        self.assertIsNone(self.admit(head, head_rule)[0], "load1 fell 10 s ago; the head must settle 45 s")

    def test_a_new_rule_at_the_head_starts_its_own_settle_time(self):
        first = sd_gate_slots.LoadRule(limit=40.0, settle=45.0, source="test")
        second = sd_gate_slots.LoadRule(limit=35.0, settle=45.0, source="test")
        ticket = self.queue.enter("gate", "/", wall=self.wall)
        self.load = (30.0, 90.0, 90.0)
        self.assertIsNone(self.admit(ticket, first)[0])
        self.wall.now += 40
        self.assertIsNone(self.admit(ticket, first)[0])
        self.wall.now += 10
        self.assertIsNone(self.admit(ticket, second)[0], "the record was kept under another rule")
        self.wall.now += 45
        self.assertEqual(self.admit(ticket, second)[0], 0)

    def test_an_old_low_sample_counts_for_nothing(self):
        rule = sd_gate_slots.LoadRule(limit=40.0, settle=45.0, source="test")
        ticket = self.queue.enter("gate", "/", wall=self.wall)
        self.load = (31.0, 90.0, 110.0)
        self.assertIsNone(self.admit(ticket, rule)[0])
        self.wall.now += 3600
        self.assertIsNone(self.admit(ticket, rule)[0], "an hour without a sample restarts the settle time")


class Settings(unittest.TestCase):
    def test_the_shell_helper_reads_the_machine_settings(self):
        """The `wait` helper `run-tests.sh` uses reads `sd.gate_*` from the machine config, stdlib only."""
        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / "sd-ai-command-pack" / "config.json"
            config.parent.mkdir()
            config.write_text(json.dumps({"config": {"sd": {"gate_load_max": "12.5", "gate_settle_seconds": "7"}}}))
            environ = {"XDG_CONFIG_HOME": tmp}
            self.assertEqual(sd_gate_slots.machine_rule(environ)[:2], (12.5, 7.0))
            self.assertEqual(sd_gate_slots.machine_rule({**environ, "SD_GATE_LOAD_MAX": "3"})[:2], (3.0, 7.0))
            config.write_text("not json")
            stream = io.StringIO()
            self.assertEqual(sd_gate_slots.machine_rule(environ, stream=stream)[:2], (0.0, 45.0))
            self.assertIn("warning", stream.getvalue())


    def test_the_variable_then_the_machine_setting_then_the_default(self):
        rule = sd_gate_slots.load_rule
        self.assertEqual(rule({"SD_GATE_LOAD_MAX": "12", "SD_GATE_SETTLE_SECONDS": "5"}, "30", "60")[:2], (12.0, 5.0))
        self.assertEqual(rule({}, "30", "60")[:2], (30.0, 60.0))
        self.assertEqual(rule({}, None, None)[:2], (0.0, 45.0))
        for bad in ("", "-1", "lots"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                rule({"SD_GATE_LOAD_MAX": bad}, None, None)

    def test_the_machine_settings_are_declared_core_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / "sd-ai-command-pack" / "config.json"
            config.parent.mkdir()
            config.write_text(json.dumps({"config": {"sd": {"gate_load_max": "32.5", "gate_settle_seconds": "30"}}}))
            self.assertEqual(sd_lib.core_setting("gate_load_max", {"XDG_CONFIG_HOME": tmp}), "32.5")
            self.assertEqual(sd_lib.core_setting("gate_settle_seconds", {"XDG_CONFIG_HOME": tmp}), "30")

    def test_a_waiter_says_why_it_waits(self):
        with tempfile.TemporaryDirectory() as tmp, open(pathlib.Path(tmp) / "slot.1.lock", "a") as mine:
            stream = io.StringIO()
            ticks = iter(range(0, 1000, 30))
            taken = sd_gate_slots.wait_for_slot(
                [mine.fileno()], pathlib.Path(tmp), poll=0.001, stream=stream, deadline=90,
                clock=lambda: float(next(ticks)), rule=sd_gate_slots.LoadRule(40.0, 0.0, "test"),
                loadavg=lambda: (55.0, 50.0, 50.0), busy=lambda: None)
        self.assertIsNone(taken)
        self.assertIn("load 55.0 is at or above 40", stream.getvalue())

    def test_a_cpu_sample_past_the_deadline_takes_no_slot(self):
        """Review finding: with --max-wait 0.5 the first 1.1 s CPU sample ran past the deadline, and a free
        slot was taken before the deadline was read."""
        with tempfile.TemporaryDirectory() as tmp, open(pathlib.Path(tmp) / "slot.1.lock", "a") as mine:
            stream, now = io.StringIO(), [0.0]

            def busy():
                now[0] += 1.1
                return 0.1
            taken = sd_gate_slots.wait_for_slot(
                [mine.fileno()], pathlib.Path(tmp), poll=0.001, stream=stream, deadline=0.5,
                clock=lambda: now[0], rule=sd_gate_slots.LoadRule(24.0, 0.0, "test"),
                loadavg=lambda: (1.0, 1.0, 1.0), busy=busy)
        self.assertIsNone(taken)
        self.assertIn("gave up waiting for a gate slot", stream.getvalue())

    def test_a_launcher_gone_during_the_cpu_sample_takes_no_slot(self):
        with tempfile.TemporaryDirectory() as tmp, open(pathlib.Path(tmp) / "slot.1.lock", "a") as mine:
            gone = [False]

            def busy():
                gone[0] = True
                return 0.1
            with self.assertRaises(sd_gate_slots.LauncherExited):
                sd_gate_slots.wait_for_slot(
                    [mine.fileno()], pathlib.Path(tmp), poll=0.001, stream=io.StringIO(), stop=lambda: gone[0],
                    rule=sd_gate_slots.LoadRule(24.0, 0.0, "test"), loadavg=lambda: (1.0, 1.0, 1.0), busy=busy)


if __name__ == "__main__":
    unittest.main()
