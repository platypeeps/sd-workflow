#!/usr/bin/env python3
"""Machine-wide gate slots: at most N repository gates run at once (sd:1541, sd:1996).

On 2026-09-25 several gates started together, the load average reached 157,
and tests with a fixed bound failed. On 2026-09-28 nine `sd-ship` gates across
repositories reached 141 on 16 cores, and a vitest default of 5 s failed tests
that assert nothing about time. Every local gate now waits for a slot first.

A slot is a kernel `flock` on `<dir>/slot.N.lock`. The kernel drops it when the
last descriptor on that open file closes, so a holder that dies frees its slot,
and no waiter ever judges a holder dead or deletes anything. The pid written
into a lock file is a hint for a reader, never evidence (#1195 review).

One implementation, two callers. `sd-check` takes a slot around the checks it
runs; `.github/scripts/run-tests.sh` opens the lock files itself and runs the
`wait` command below on those descriptors, so the lock outlives this process
and stays with the shell. A holder sets `SD_GATE_SLOTS=0` for everything it
starts, so a gate inside a gate never waits on the slot its parent holds.

A queue stands in front of the slots (sd:2262). On 2026-10-01 about 14 gates
waited on hand-written copies of one load rule; when load fell, several read the
same quiet moment and started together, in no order. Now every waiter takes a
ticket, and only the head of the queue is admitted, under one `queue.lock`, and
only when the load rule holds. So the first to wait is the first to start, and
no two waiters share one free slot or one load reading. A ticket is `flock`ed
by its waiter, so a dead waiter's ticket is free and the next poll removes it.

`run` waits, runs one command, and frees the slot when it ends; `status` shows
who holds and who waits. `sd gate run` and `sd gate status` are the same verbs
with the machine settings read.

One pool for every caller (sd:2522). On 2026-10-03 the pack's own `make test`
counted 2 slots from its Makefile while `sd-check` counted the machine's 4, the
stdlib `run` ignored `sd.gate_slots`, and a gate that waited said how many
slots were busy but not who held them. Now every entry point takes its count
from `configured` with `sd.gate_slots` read (`count` prints it for the shell),
and a waiting gate names each holder, its directory and since when.

The count is the one limit (sd:2607). On 2026-10-03 three gates read a load
average of 124 while most cores idled: macOS counts threads waiting on the disk
in the load average, so the load rule held every waiter back while the CPU had
room. The load rule is now off unless `sd.gate_load_max` sets it; the starts
stay `sd.gate_settle_seconds` apart. A holder also exports the cap as
`SD_GATE_POOL_SIZE`, so the tests it runs size their workers to the pool.

The count does not bound one gate's CPU (sd:2726). On 2026-10-05 one Rust
gate under 2 slots drove the load to 185 on 16 cores: cargo builds and tests
on every core. A holder under a cap now sets `CARGO_BUILD_JOBS`,
`RUST_TEST_THREADS` and `NEXTEST_TEST_THREADS` (cargo-nextest reads no other,
sd:2872) to its share, the cores over the cap; a lower value the caller set wins. A gate receipt binds them (`thread_caps`), since a suite can
pass on one thread and fail on eight.
`MAKEFLAGS` gets no `-j`: a Makefile that orders prerequisites by their
listing, as the pack's own `check` does, would run them at once.

Every waiter goes in the order it queued (sd:5409). sd:5078 admitted lane
gates first and had a builder's `run` refuse, exit 69, while a lane gate held
or waited. On 2026-10-19 three builders then waited 1.5 to 2 h: a lane gate
nearly always held a slot, and each time one ended the next queued before the
builder tried again. Now a builder's `run` queues as any waiter does, bounded
by its `--max-wait`, and no later lane gate overtakes it; a lane gate that
queued first still starts first. The slot count is the load bound for both.

A lane gate is `sd-check`'s own wait, the merge gate wherever it runs; its
ticket says `role: lane`, and while it holds slot N it holds a `flock` on
`slot.N.lane`, taken in the same admission step, with its label written in it,
so `status` names it; the kernel drops both when it dies. The key is not
sd:5078's `lane: true`, which that version's admission put first: a waiter
still running that code would wait for a lane ticket this version orders
behind it, and each would wait for the other. A lane gate still on that code
puts its own ticket first until it ends; nothing here can change that.

Stdlib only: the harness runs this file from a bare fixture copy.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import functools
import json
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import time
from typing import Callable, Iterator, Mapping, NamedTuple, Sequence, TextIO

#: How many gates may run at once; `0` means no cap. A holder exports `0`.
SLOTS_VARIABLE = "SD_GATE_SLOTS"
DIRECTORY_VARIABLE = "SD_GATE_SLOTS_DIR"
POLL_VARIABLE = "SD_GATE_SLOT_POLL"
DEFAULT_POLL_SECONDS = 5.0
#: How often a queued gate says it is still queued, so it does not look hung.
REPORT_EVERY_SECONDS = 60.0
#: Cores per default slot. A repository gate runs a test worker on nearly every
#: core, so a quarter of the cores in gates keeps the load near four per core.
CORES_PER_SLOT = 4
#: Exit codes of the `wait` command.
LAUNCHER_EXITED = 3
TIMED_OUT = 4
#: Exit codes of the `run` command, beside the command's own: as `timeout(1)`, except a wait past `--max-wait`,
#: which exits `EX_TEMPFAIL` so a caller's own `timeout` keeps 124 for itself (sd:3996).
RUN_NO_SLOT = 75
RUN_NOT_STARTED = 125
RUN_NOT_FOUND = 127
#: The last stderr line of every `RUN_NOT_STARTED`: the command's own 125 passes through without it (pack sd:7899).
NOT_STARTED = "sd gate run: the command did not start (exit 125)"

#: The load rule (sd:2262): off by default since sd:2607, with 45 s between two starts.
LOAD_VARIABLE = "SD_GATE_LOAD_MAX"
SETTLE_VARIABLE = "SD_GATE_SETTLE_SECONDS"
DEFAULT_LOAD_MAX = 0.0
DEFAULT_SETTLE_SECONDS = 45.0
#: The cap a holder hands the commands it starts, beside `SD_GATE_SLOTS=0` (sd:2607).
POOL_VARIABLE = "SD_GATE_POOL_SIZE"
#: Thread counts a holder caps at its share of the cores (sd:2726): cargo, Rust's test harness and cargo-nextest
#: (sd:2872) default to every core.
CPU_VARIABLES = ("CARGO_BUILD_JOBS", "RUST_TEST_THREADS", "NEXTEST_TEST_THREADS")
#: A gap between two load samples longer than this, or than the settle time,
#: restarts the low-load record: nobody watched the load in between.
STALE_SAMPLE_SECONDS = 15.0
QUEUE_LOCK = "queue.lock"
QUEUE_STATE = "queue.state"
QUEUE_DIR = "queue"
#: The lock a lane gate holds beside `slot.N.lock` while it holds slot N (sd:5078).
LANE_MARKER = re.compile(r"slot\.([0-9]+)\.lane")
NUMBER = re.compile(r"[0-9]+(\.[0-9]+)?")


class LauncherExited(Exception):
    """The process that started the waiter exited: an orphan must not take a slot."""


def directory(environ: Mapping[str, str]) -> pathlib.Path:
    """Where the slot lock files live, shared by every gate on this machine."""
    named = environ.get(DIRECTORY_VARIABLE)
    if named:
        return pathlib.Path(named)
    state = environ.get("XDG_STATE_HOME") or str(pathlib.Path(environ.get("HOME", "~")).expanduser() / ".local" / "state")
    return pathlib.Path(state) / "sd" / "gate-slots"


def default_slots(cores: int | None = None) -> int:
    """A quarter of the cores, and at least one: 4 on a 16-core machine."""
    count = cores if cores is not None else (os.cpu_count() or CORES_PER_SLOT)
    return max(1, count // CORES_PER_SLOT)


#: A count or pid of more digits is no real one; int() refuses a string past 4,300 digits with ValueError (sd:7401).
MAX_DIGITS = 18


def parse_count(text: str, source: str) -> int:
    """A non-negative slot count, or a `ValueError` naming where it came from."""
    if not text.isascii() or not text.isdigit() or len(text.lstrip("0")) > MAX_DIGITS:
        shown = text if len(text) <= 40 else f"{text[:40]}... ({len(text)} characters)"
        raise ValueError(f"{source} must be a non-negative integer of at most {MAX_DIGITS} digits (got '{shown}')")
    return int(text.lstrip("0") or "0")


def as_time(value: object) -> float | None:
    """`value` as a time when it is a number a float holds, else None: float() of an int past 308 digits raises."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value)
    except OverflowError:
        return None


def configured(environ: Mapping[str, str], setting: str | None) -> tuple[int, str]:
    """The cap and who set it: the variable, then CI, then `sd.gate_slots`, then the default.

    CI never waits: a runner is one job's machine. `setting` is the machine's
    `sd.gate_slots`, read by the caller so this file needs nothing but stdlib.
    """
    if SLOTS_VARIABLE in environ:
        return parse_count(environ[SLOTS_VARIABLE], SLOTS_VARIABLE), SLOTS_VARIABLE
    if environ.get("CI") or environ.get("GITHUB_ACTIONS"):
        return 0, "CI"
    if setting is not None:
        return parse_count(setting, "sd.gate_slots"), "sd.gate_slots"
    return default_slots(), "default"


class LoadRule(NamedTuple):
    """Admit only while load1 is below `limit`; `settle` seconds of low load and between starts. 0 is off."""
    limit: float
    settle: float
    source: str


def parse_number(text: str, source: str) -> float:
    if not NUMBER.fullmatch(text):
        raise ValueError(f"{source} must be a non-negative number (got '{text}')")
    return float(text)


def load_rule(environ: Mapping[str, str], limit_setting: str | None = None,
              settle_setting: str | None = None) -> LoadRule:
    """The load rule: each value from its variable, then the machine setting, then the default.

    The settings are `sd.gate_load_max` and `sd.gate_settle_seconds`, read by
    the caller so this file needs nothing but stdlib.
    """
    def setting_value(variable: str, setting: str | None, name: str, default: float) -> tuple[float, str | None]:
        if variable in environ:
            return parse_number(environ[variable], variable), variable
        if setting is not None:
            return parse_number(setting, name), name
        return default, None
    limit, limit_source = setting_value(LOAD_VARIABLE, limit_setting, "sd.gate_load_max", DEFAULT_LOAD_MAX)
    settle, settle_source = setting_value(SETTLE_VARIABLE, settle_setting, "sd.gate_settle_seconds", DEFAULT_SETTLE_SECONDS)
    sources = [source for source in (limit_source, settle_source) if source]
    return LoadRule(limit, settle, ", ".join(sources) or "default")


#: At or above this CPU busy share the load average counts in full; below it a load limit reads the CPU's own
#: use, busy times cores, instead (sd:9946): on 2026-11-22 a booted simulator held load5 at 277-458 with the
#: CPU 37% idle, and the load limits paused every build claim and lane start.
CPU_BUSY_MAX = 0.9
#: The least gap between the two CPU tick samples `cpu_busy` compares: macOS updates its ticks every ~1.004 s,
#: so a 0.25 s gap often read no change and fell back to load5 (8 polls: 4 unread).
CPU_SAMPLE_SECONDS = 1.1
#: The oldest previous sample `cpu_busy` compares against instead of waiting.
CPU_SAMPLE_MAX_AGE = 60.0


@functools.lru_cache(maxsize=1)
def host_port() -> tuple[object, int]:
    """libSystem and this host's port, taken once: each `mach_host_self()` call adds a reference to the
    port, and a gate wait samples the CPU on every poll for hours."""
    import ctypes  # noqa: PLC0415 -- only the darwin path needs it
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    system.mach_host_self.restype = ctypes.c_uint
    return system, system.mach_host_self()


def cpu_ticks() -> tuple[int, int] | None:
    """(idle, total) CPU ticks since boot, or None where they cannot be read: macOS
    `host_statistics(HOST_CPU_LOAD_INFO)`, Linux `/proc/stat`. A disk wait counts as idle."""
    try:
        if sys.platform == "darwin":
            import ctypes  # noqa: PLC0415 -- only the darwin path needs it
            system, port = host_port()
            info, count = (ctypes.c_uint * 4)(), ctypes.c_uint(4)
            if system.host_statistics(port, 3, info, ctypes.byref(count)) != 0:
                return None
            user, kernel, idle, nice = info
            return idle, user + kernel + idle + nice
        fields = pathlib.Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
        if fields[0] != "cpu" or len(fields) < 6:
            return None
        ticks = [int(value) for value in fields[1:]]
        return ticks[3] + ticks[4], sum(ticks[:8])
    except (OSError, ValueError, IndexError, AttributeError, ImportError):
        return None


def cpu_busy(ticks: Callable[[], tuple[int, int] | None] = cpu_ticks,
             pause: Callable[[float], object] = time.sleep, *,
             clock: Callable[[], float] = time.monotonic) -> float | None:
    """The share of CPU time not idle since an earlier sample, 0 to 1, or None when unread.

    The earlier sample is this process's previous one when it is `CPU_SAMPLE_SECONDS` to
    `CPU_SAMPLE_MAX_AGE` old, so a gate poll waits under the queue lock only on its first
    read; otherwise `cpu_busy` takes one and waits `CPU_SAMPLE_SECONDS`.
    """
    last = cpu_busy.last
    second, at = ticks(), clock()
    if second is None:
        return None
    if last is None or not CPU_SAMPLE_SECONDS <= at - last[0] <= CPU_SAMPLE_MAX_AGE:
        first = second
        pause(CPU_SAMPLE_SECONDS)
        second, at = ticks(), clock()
        if second is None:
            return None
    else:
        first = last[1]
    cpu_busy.last = (at, second)
    idle, total = second[0] - first[0], second[1] - first[1]
    if total <= 0 or not 0 <= idle <= total:
        return None
    return 1.0 - idle / total


#: (monotonic time, ticks) of this process's previous `cpu_busy` sample.
cpu_busy.last = None  # type: ignore[attr-defined]


def counted_load(load: float, busy: float | None, cores: int | None = None) -> float:
    """The load a load limit reads (sd:9946): `load` while the CPU is at least `CPU_BUSY_MAX` busy or its
    use is unread; otherwise the CPU's own use, busy times cores, when that is lower."""
    count = cores if cores is not None else os.cpu_count()
    if busy is None or not count or not 0 <= busy < CPU_BUSY_MAX:
        return load
    return min(load, busy * count)


#: The machine settings the stdlib entry points read, with the check each value passes.
MACHINE_SETTINGS: tuple[tuple[str, Callable[[str, str], object]], ...] = (
    ("gate_slots", parse_count), ("gate_load_max", parse_number), ("gate_settle_seconds", parse_number))


def machine_settings(environ: Mapping[str, str], *, stream: TextIO | None = None) -> dict[str, str | None]:
    """`sd.gate_slots`, `sd.gate_load_max` and `sd.gate_settle_seconds` from the machine config, as text.

    For the stdlib-only entry points (`wait` and `count`, which `run-tests.sh`
    runs, and `run`/`status` here), so they read what `sd-check` and `sd gate`
    read. The path is `sd_lib.machine_config_path`'s. A file that cannot be
    read or holds a bad value gives no settings, so the defaults, with a
    warning: load control never fails a gate.
    """
    home = environ.get("XDG_CONFIG_HOME") or str(pathlib.Path(environ.get("HOME") or pathlib.Path.home()) / ".config")
    path = pathlib.Path(home) / "sd-ai-command-pack" / "config.json"
    values: dict[str, str | None] = {key: None for key, _ in MACHINE_SETTINGS}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        mine = loaded.get("config", {}).get("sd", {}) if isinstance(loaded, dict) else {}
        for key, check in MACHINE_SETTINGS:
            value = mine.get(key) if isinstance(mine, dict) else None
            if value is not None:
                text = value if isinstance(value, str) else repr(value)
                check(text, f"sd.{key}")
                values[key] = text
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as error:
        if stream is not None:
            stream.write(f"warning: cannot read the gate settings in {path} ({error}); using the defaults\n")
        values = dict.fromkeys(values)
    return values


def machine_rule(environ: Mapping[str, str], *, stream: TextIO | None = None) -> LoadRule:
    """`load_rule` with `sd.gate_load_max` and `sd.gate_settle_seconds` read by `machine_settings`."""
    values = machine_settings(environ, stream=stream)
    return load_rule(environ, values["gate_load_max"], values["gate_settle_seconds"])


def utc_stamp(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ticket:
    """One waiter's place: its sequence number, the locked ticket file, and its place as the last `admit` read it.

    `lane` marks a lane gate, which marks the slot it takes (sd:5078).
    """

    def __init__(self, seq: int, path: pathlib.Path, fd: int | None, *, label: str = "", cwd: str = "",
                 lane: bool = False) -> None:
        self.seq, self.path, self.fd = seq, path, fd
        self.label, self.cwd, self.lane = label, cwd, lane
        self.place: tuple[int, int] | None = None

    def drop(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Queue:
    """The machine-wide waiting order in front of the slots under `where`.

    Every read and change happens under `queue.lock`, so admission is one step.
    A ticket is created and locked under it, and only a process that can take
    a ticket's own lock removes it: its waiter is dead, and the kernel says so.
    """

    def __init__(self, where: pathlib.Path) -> None:
        self.where = where
        self.tickets = where / QUEUE_DIR

    @contextlib.contextmanager
    def admission_lock(self) -> Iterator[None]:
        fd = os.open(self.where / QUEUE_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def state(self) -> dict:
        try:
            loaded = json.loads((self.where / QUEUE_STATE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def write_state(self, state: dict) -> None:
        target = self.where / QUEUE_STATE
        partial = target.with_name(f"{QUEUE_STATE}.{os.getpid()}")
        partial.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(partial, target)

    def enter(self, label: str, cwd: str, *, wall: Callable[[], float] = time.time, lane: bool = False) -> Ticket:
        """Take the next place in the queue, held by this process until `leave` or death."""
        self.tickets.mkdir(parents=True, exist_ok=True)
        with self.admission_lock():
            state = self.state()
            stored = state.get("next")
            seq = stored if isinstance(stored, int) and stored > 0 else 1 + max(
                (int(path.stem) for path in self.tickets.glob("*.ticket") if path.stem.isdigit()), default=0)
            state["next"] = seq + 1
            self.write_state(state)
            path = self.tickets / f"{seq:012d}.ticket"
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, json.dumps({"seq": seq, "pid": os.getpid(), "label": label, "cwd": cwd,
                                     "since": wall(), **({"role": "lane"} if lane else {})}).encode())
        return Ticket(seq, path, fd, label=label, cwd=cwd, lane=lane)

    def leave(self, ticket: Ticket) -> None:
        if ticket.fd is None:
            return
        with self.admission_lock():
            ticket.path.unlink(missing_ok=True)
            ticket.drop()

    def waiters(self, *, prune: bool) -> list[dict]:
        """The live tickets in order. Call under `admission_lock`; `prune` removes the dead ones."""
        live: list[dict] = []
        try:
            paths = sorted(self.tickets.glob("*.ticket"))
        except OSError:
            return live
        for path in paths:
            try:
                fd = os.open(path, os.O_RDWR)
            except FileNotFoundError:
                continue
            except OSError:
                # Not probed, so outside the order as before, but a lane check cannot rule it out (sd:5078).
                live.append({"seq": int(path.stem) if path.stem.isdigit() else 0, "unreadable": True,
                             "unprobed": True})
                continue
            try:
                if try_lock(fd):
                    if prune:
                        path.unlink(missing_ok=True)
                    continue
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    entry = {"unreadable": True}
                if not isinstance(entry, dict):
                    entry = {"unreadable": True}
                entry["seq"] = int(path.stem) if path.stem.isdigit() else 0
                live.append(entry)
            finally:
                os.close(fd)
        return live

    def admit(self, ticket: Ticket, fds: Sequence[int], rule: LoadRule, *, markers: Sequence[int] = (),
              loadavg: Callable[[], Sequence[float]] = os.getloadavg,
              wall: Callable[[], float] = time.time,
              busy: Callable[[], float | None] = cpu_busy) -> tuple[int | None, str]:
        """Lock one of `fds` for `ticket` when it heads the queue and the load rule holds.

        Returns the slot's index, or None and the reason it waits. An admitted
        ticket leaves the queue in the same step, and a lane ticket locks the
        slot's `markers` entry in that step too (sd:5078).
        """
        with self.admission_lock():
            state = self.state()
            now = wall()
            try:
                ordered = admission_order(self.waiters(prune=True))
                listed = any(entry["seq"] == ticket.seq for entry in ordered)
                mine = [] if listed else [{"seq": ticket.seq}]
                order = [entry["seq"] for entry in admission_order([*ordered, *mine])]
                ticket.place = (order.index(ticket.seq) + 1, len(order))
                if order[0] != ticket.seq:
                    return None, f"place {order.index(ticket.seq) + 1} of {len(order)} in the queue"
                # Only the head samples, under its own rule: callers may run
                # different rules, and one waiter's sample must not count for another's.
                load1, load5 = (float(value) for value in tuple(loadavg())[:2])
                if rule.limit > 0:
                    share = busy()
                    load1, load5 = counted_load(load1, share), counted_load(load5, share)
                record_sample(state, rule, load1, now)
                held = load_hold(state, rule, load1, load5, now)
                if held:
                    return None, held
                for index, fd in enumerate(fds):
                    if not try_lock(fd):
                        continue
                    if ticket.lane and markers and not mark_lane(markers[index], ticket.label, ticket.cwd, now):
                        fcntl.flock(fd, fcntl.LOCK_UN)
                        return None, f"the lane marker of slot {index + 1} is held"
                    state["last_admitted"] = now
                    ticket.path.unlink(missing_ok=True)
                    ticket.drop()
                    return index, ""
                return None, f"{len(fds)} of {len(fds)} in use"
            finally:
                self.write_state(state)


def admission_order(entries: list[dict]) -> list[dict]:
    """First in, first out, lane gate or not (sd:5409).

    A ticket that could not be opened is left out: nobody can say whether its waiter lives.
    """
    return sorted((entry for entry in entries if not entry.get("unprobed")), key=lambda entry: entry["seq"])


def is_lane(entry: dict) -> bool:
    """A lane gate's ticket: `role: lane`, or sd:5078's `lane: true`, which a waiter on that code may still write."""
    return entry.get("role") == "lane" or entry.get("lane") is True


def mark_lane(fd: int, label: str, cwd: str, now: float) -> bool:
    """Lock a slot's `slot.N.lane` for the lane gate that took the slot, and write who it is (sd:5078).

    False when the lock is held: a reader's probe holds it for an instant, and
    the caller frees the slot and tries again at its next poll, so a lane gate
    never holds a slot it cannot show. The label is a hint; the lock is the evidence.
    """
    if not try_lock(fd):
        return False
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "label": label, "cwd": cwd, "since": now}).encode())
    return True


def take_free(fds: Sequence[int], markers: Sequence[int], label: str, now: float) -> tuple[int | None, str]:
    """Any free slot, for a waiter that could not queue; a lane gate takes the slot only with its marker (sd:5078)."""
    for index, fd in enumerate(fds):
        if not try_lock(fd):
            continue
        if markers and not mark_lane(markers[index], label, os.getcwd(), now):
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None, f"the lane marker of slot {index + 1} is held"
        return index, ""
    return None, f"{len(fds)} of {len(fds)} in use"


def lane_gates(where: pathlib.Path, live: list[dict]) -> list[dict]:
    """Each lane gate that holds a slot, then each that waits, in admission order; call under `queue.lock`.

    A lane gate holds slot N's `slot.N.lane` while it holds the slot, so only
    a live one counts. A marker or ticket that cannot be read is an `unknown`
    entry, never no lane gate (sd:5078).
    """
    found: list[dict] = []
    try:
        names = sorted(path.name for path in where.iterdir() if LANE_MARKER.fullmatch(path.name))
    except FileNotFoundError:
        names = []
    except OSError as error:
        return [{"state": "unknown", "reason": f"cannot list {where}: {error}"}]
    for name in sorted(names, key=lambda text: int(text.split(".")[1])):
        try:
            fd = os.open(where / name, os.O_RDONLY)
        except FileNotFoundError:
            continue
        except OSError as error:
            found.append({"state": "unknown", "reason": f"cannot read {where / name}: {error}"})
            continue
        try:
            if try_lock(fd):
                fcntl.flock(fd, fcntl.LOCK_UN)
                continue
            try:
                record = json.loads((where / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = {}
        finally:
            os.close(fd)
        record = record if isinstance(record, dict) else {}
        since = record.get("since")
        found.append({"state": "holding", "slot": int(name.split(".")[1]), "pid": record.get("pid"),
                      "label": record.get("label", ""), "cwd": record.get("cwd", ""),
                      "since": utc_stamp(since) if isinstance(since, (int, float)) else ""})
    found.extend({"state": "unknown", "reason": f"cannot read queue ticket {entry['seq']}"}
                 for entry in live if entry.get("unreadable"))
    for place, entry in enumerate(admission_order(live), start=1):
        if is_lane(entry):
            since = entry.get("since")
            found.append({"state": "waiting", "place": place, "pid": entry.get("pid"),
                          "label": entry.get("label", ""), "cwd": entry.get("cwd", ""),
                          "since": utc_stamp(since) if isinstance(since, (int, float)) else ""})
    return found


def lane_line(gate: dict) -> str:
    """One lane gate as `status` names it."""
    if gate["state"] == "unknown":
        return f"unknown: {gate['reason']}"
    where = f"holds slot {gate['slot']}" if gate["state"] == "holding" else f"waits at place {gate['place']}"
    return f"{gate['label'] or 'an unlabelled gate'} (pid {gate['pid'] or '?'}) {where} since {gate['since'] or '?'}"


def record_sample(state: dict, rule: LoadRule, load1: float, now: float) -> None:
    """Keep `low_since`: when load1 last went below the limit, in an unbroken run of samples.

    The record names the rule it was kept under; a sample under another rule
    starts it again, so no rule inherits low load another rule judged.
    """
    sampled = state.get("sampled_at")
    measured = [rule.limit, rule.settle]
    if rule.limit > 0 and load1 >= rule.limit:
        state["low_since"] = None
    elif (state.get("low_since") is None or state.get("sampled_rule") != measured
          or not isinstance(sampled, (int, float))
          or not 0 <= now - sampled <= max(rule.settle, STALE_SAMPLE_SECONDS)):
        state["low_since"] = now
    state["sampled_at"] = now
    state["sampled_rule"] = measured


def load_hold(state: dict, rule: LoadRule, load1: float, load5: float, now: float) -> str:
    """Why the head may not start now, or "" when it may."""
    if rule.limit > 0:
        if load1 >= rule.limit:
            return f"load {load1:.1f} is at or above {rule.limit:g}"
        low = now - (as_time(state.get("low_since")) or now)
        if load5 >= rule.limit and low < rule.settle:
            return (f"load {load1:.1f} has been below {rule.limit:g} for {low:.0f}s of {rule.settle:g}s "
                    f"(load5 {load5:.1f})")
    last = state.get("last_admitted")
    if rule.settle > 0 and isinstance(last, (int, float)) and 0 <= now - last < rule.settle:
        return f"the last gate started {now - last:.0f}s ago; starts are {rule.settle:g}s apart"
    return ""


def poll_seconds(environ: Mapping[str, str]) -> float:
    try:
        seconds = float(environ.get(POLL_VARIABLE, DEFAULT_POLL_SECONDS))
    except ValueError:
        return DEFAULT_POLL_SECONDS
    return seconds if seconds > 0 else DEFAULT_POLL_SECONDS


def try_lock(fd: int) -> bool:
    """True when this call took the lock on `fd`'s open file without waiting."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def parent_of(pid: int) -> int | None:
    """`pid`'s parent as `ps` reads it, or None when `ps` cannot say."""
    if pid == os.getpid():
        return os.getppid()
    try:
        shown = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                               timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return int(shown) if shown.isdigit() else None


def launcher_watch(pid: int, ppid: int) -> Callable[[], bool]:
    """True once `pid` was reparented away from `ppid`. A run detached at start has no launcher.

    An unreadable `ps` is not an exit, as in the harness's own watchdog.
    """
    def exited() -> bool:
        if ppid == 1:
            return False
        current = parent_of(pid)
        return current is not None and current != ppid
    return exited


def place_line(ticket: Ticket | None, reason: str) -> str:
    """Where a waiter stood when it gave up, and why when it headed the queue: behind the head, the place is the reason."""
    place = ticket.place if ticket is not None else None
    if place is None:
        return f"no place in the queue: {reason}"
    return f"place {place[0]} of {place[1]} in the queue" + (f": {reason}" if place[0] == 1 else "")


def wait_for_slot(fds: Sequence[int], where: pathlib.Path, *, poll: float, stream: TextIO,
                  stop: Callable[[], bool] = lambda: False, deadline: float | None = None,
                  clock: Callable[[], float] = time.monotonic, rule: LoadRule | None = None,
                  loadavg: Callable[[], Sequence[float]] = os.getloadavg, wall: Callable[[], float] = time.time,
                  label: str = "", lane: bool = False, markers: Sequence[int] = (),
                  busy: Callable[[], float | None] = cpu_busy) -> int | None:
    """Queue, then lock one of `fds` and return its index; None once `deadline` passes.

    `lane` queues as a lane gate, which locks `markers[i]` with slot i (sd:5078).

    `stop` is asked before every attempt, so a waiter whose launcher died
    takes nothing even when a slot frees during its sleep. While queued it
    says why on `stream` at once and every `REPORT_EVERY_SECONDS` after. With
    no `rule` the load is not read. Each report names who holds the slots
    (sd:2522), and so does the line that gives up, with the waiter's place
    and how long it waited (sd:3996). A queue that cannot be written falls
    back to taking any free slot, as before sd:2262.
    """
    rule = rule if rule is not None else LoadRule(0.0, 0.0, "none")
    queue = Queue(where)
    try:
        ticket: Ticket | None = queue.enter(label or f"pid {os.getpid()}", os.getcwd(), wall=wall, lane=lane)
    except OSError as error:
        stream.write(f"warning: cannot queue under {where} ({error}); taking any free slot\n")
        ticket = None
    started = clock()
    reported: float | None = None
    try:
        while True:
            if stop():
                raise LauncherExited()
            if ticket is None:
                taken, reason = take_free(fds, markers, label, wall())
            else:
                taken, reason = queue.admit(ticket, fds, rule, markers=markers, loadavg=loadavg, wall=wall,
                                             busy=busy)
            if taken is not None:
                if reported is not None:
                    stream.write(f"gate slot taken after {clock() - started:.0f}s under {where}\n")
                    stream.flush()
                return taken
            now = clock()
            if reported is None:
                stream.write(f"waiting for a gate slot: {reason} under {where}{held_by(queue)}\n")
                stream.flush()
                reported = now
            elif now - reported >= REPORT_EVERY_SECONDS:
                stream.write(f"still waiting for a gate slot after {now - started:.0f}s: {reason}{held_by(queue)}\n")
                stream.flush()
                reported = now
            if deadline is not None and now >= deadline:
                stream.write(f"gave up waiting for a gate slot after {now - started:.0f}s at "
                             f"{place_line(ticket, reason)}{held_by(queue)}\n")
                stream.flush()
                return None
            time.sleep(poll if deadline is None else max(0.0, min(poll, deadline - now)))
    finally:
        if ticket is not None:
            queue.leave(ticket)


class Slot:
    """A slot held by this process until `release`; `waited` is the seconds spent queued.

    A lane gate's `marker` is its `slot.N.lane`; it is released first, so no
    later holder of the slot finds it still locked (sd:5078).
    """

    def __init__(self, handle: TextIO | None, path: pathlib.Path | None, waited: float,
                 marker: TextIO | None = None) -> None:
        self.handle, self.path, self.waited, self.marker = handle, path, waited, marker

    def release(self) -> None:
        if self.marker is not None:
            self.marker.close()
            self.marker = None
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def write_info(lock: pathlib.Path, pid: int, label: str, cwd: str) -> None:
    """`slot.N.info` beside the lock: who holds it, for `status`. Only a hint, as the pid is."""
    info = lock.with_suffix(".info")
    try:
        partial = info.with_name(f"{info.name}.{os.getpid()}")
        partial.write_text(json.dumps({"pid": pid, "label": label, "cwd": cwd, "since": time.time()}),
                           encoding="utf-8")
        os.replace(partial, info)
    except OSError:
        pass


def open_markers(where: pathlib.Path, slots: int, stream: TextIO) -> list[TextIO]:
    """A lane gate's `slot.N.lane` for each slot (sd:5078).

    None, with a warning, when one cannot be opened: the gate still queues
    first and keeps the cap, since no gate fails for want of load control,
    but builders cannot see it hold a slot.
    """
    markers: list[TextIO] = []
    try:
        for index in range(1, slots + 1):
            markers.append(open(where / f"slot.{index}.lane", "a"))
    except OSError as error:
        for marker in markers:
            marker.close()
        stream.write(f"warning: cannot open a lane marker under {where} ({error}); "
                     "builders will not see this gate hold a slot\n")
        return []
    return markers


def kept_marker(markers: list[TextIO], taken: int | None) -> TextIO | None:
    """Close every marker but the taken slot's, and return that one."""
    for index, marker in enumerate(markers):
        if index != taken:
            marker.close()
    return markers[taken] if taken is not None and markers else None


def acquire(slots: int, environ: Mapping[str, str], *, stream: TextIO, timeout: float | None = None,
            rule: LoadRule | None = None, label: str = "", lane: bool = False) -> Slot | None:
    """Take one of `slots` slots for this process, or None when `timeout` passed first.

    `lane` is `wait_for_slot`'s: `sd-check` waits as the lane gate (sd:5078).

    `slots` of 0 takes nothing. A directory that cannot be made runs uncapped
    with a warning, as the harness always has: a cap is load control, and no
    gate fails for the want of one. The lock files are not inherited by the
    checks this process starts, so a check that outlives it frees nothing late.
    With no `rule`, the load rule comes from the variables and the defaults.
    """
    if slots <= 0:
        return Slot(None, None, 0.0)
    where = directory(environ)
    try:
        where.mkdir(parents=True, exist_ok=True)
        handles = [open(where / f"slot.{index}.lock", "a") for index in range(1, slots + 1)]
    except OSError as error:
        stream.write(f"warning: cannot use {where} ({error}); running without the gate cap\n")
        return Slot(None, None, 0.0)
    markers = open_markers(where, slots, stream) if lane else []
    started = time.monotonic()
    try:
        taken = wait_for_slot([handle.fileno() for handle in handles], where, poll=poll_seconds(environ),
                              stream=stream, stop=launcher_watch(os.getpid(), os.getppid()),
                              deadline=None if timeout is None else started + timeout,
                              rule=rule if rule is not None else load_rule(environ), label=label, lane=lane,
                              markers=[marker.fileno() for marker in markers])
    except BaseException:
        for handle in [*markers, *handles]:
            handle.close()
        raise
    for index, handle in enumerate(handles):
        if index != taken:
            handle.close()
    marker = kept_marker(markers, taken)
    if taken is None:
        return None
    held = handles[taken]
    lock = where / f"slot.{taken + 1}.lock"
    try:
        held.truncate(0)
        held.write(f"{os.getpid()}\n")
        held.flush()
    except OSError:
        pass
    write_info(lock, os.getpid(), label or f"pid {os.getpid()}", os.getcwd())
    return Slot(held, lock, time.monotonic() - started, marker)


def cpu_share(slots: int, cores: int | None = None) -> int:
    """One gate's share of the cores under a cap of `slots`, and at least one: 8 for 2 slots on 16 cores."""
    return max(1, (cores if cores is not None else (os.cpu_count() or 1)) // slots)


def thread_cap(value: str | None, share: int) -> str:
    """`value` when it is a positive integer no higher than `share`, else `share`: a lower value wins (sd:2726)."""
    if not value or not value.isascii() or not value.isdigit():
        return str(share)
    digits = value.lstrip("0")
    return value if 0 < len(digits) <= MAX_DIGITS and int(digits) <= share else str(share)


def holder_environment(environ: Mapping[str, str], slots: int) -> dict[str, str]:
    """What a holder's commands run with: no slot of their own, the cap it took one under (sd:2607),
    and under a cap, each of `CPU_VARIABLES` at most the gate's share of the cores (sd:2726)."""
    if slots <= 0:
        return {**environ, SLOTS_VARIABLE: "0"}
    share = cpu_share(slots)
    return {**environ, SLOTS_VARIABLE: "0", POOL_VARIABLE: str(slots),
            **{name: thread_cap(environ.get(name), share) for name in CPU_VARIABLES}}


def thread_caps(environ: Mapping[str, str]) -> dict[str, str]:
    """The `CPU_VARIABLES` a holder under the machine's slot count hands the checks it runs from `environ`.

    `sd_gate_receipts` binds these beside the environment, since a suite can
    pass on one test thread and fail on eight. A slot count that cannot be read gives none.
    """
    try:
        slots, _ = configured(environ, machine_settings(environ)["gate_slots"])
    except ValueError:
        return {}
    held = holder_environment(environ, slots)
    return {name: held[name] for name in CPU_VARIABLES if name in held}


def run_gated(command: Sequence[str], environ: Mapping[str, str], *, slots: int, rule: LoadRule, stream: TextIO,
        label: str = "", max_wait: float | None = None) -> int:
    """Wait for a slot, at most `max_wait` seconds, run `command` with `SD_GATE_SLOTS=0`, free the slot,
    and return its exit code; `RUN_NO_SLOT` when the wait ran out, with the command not started.
    It waits its turn beside lane gates, in the order it queued (sd:5409).

    The command runs in its own process group. SIGINT, SIGTERM and SIGHUP go
    to that whole group, and this process waits for the command to end. The
    slot lock is not passed to the command: when this process dies, even by
    SIGKILL, the kernel frees the slot (sd:1195), and a daemon the command
    started never holds one.
    """
    if not command:
        stream.write(f"error: name a command to run after --\n{NOT_STARTED}\n")
        return RUN_NOT_STARTED
    try:
        slot = acquire(slots, environ, stream=stream, timeout=max_wait, rule=rule, label=label or shlex.join(command))
    except LauncherExited:
        stream.write(f"error: the process that started this gate exited while it waited for a slot\n{NOT_STARTED}\n")
        return RUN_NOT_STARTED
    except KeyboardInterrupt:
        return 128 + signal.SIGINT
    if slot is None:
        stream.write(f"error: no gate slot came free within --max-wait {max_wait:g}s; the command did not run "
                     f"(exit {RUN_NO_SLOT})\n")
        return RUN_NO_SLOT
    forwarded = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {}
    child = None
    held: list[int] = []

    def forward(signum: int, _frame: object) -> None:
        if child is None:
            # Still inside `Popen`, whose group may already run: send it once `child` is known (sd:5801).
            held.append(signum)
            return
        try:
            os.killpg(child.pid, signum)
        except (ProcessLookupError, PermissionError):
            # A group that already exited: macOS answers EPERM, not only ESRCH (sd:2402).
            pass

    try:
        for signum in forwarded:
            previous[signum] = signal.signal(signum, forward)
        try:
            child = subprocess.Popen(list(command), env=holder_environment(environ, slots), process_group=0)
        except OSError as error:
            stream.write(f"error: cannot run {command[0]}: {error}\n")
            return RUN_NOT_FOUND
        for number in held:
            forward(number, None)
        code = child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        slot.release()
    return 128 - code if code < 0 else code


def read_holders(where: pathlib.Path) -> list[dict]:
    """Who holds each slot under `where`; call under `queue.lock`, so no admission races the probe.

    A slot is held exactly when this process cannot lock it. Its label shows
    only when `slot.N.info` names the pid the lock file names, so a holder
    that wrote no info never shows an earlier holder's label.
    """
    holders = []
    numbers = {int(match.group(1)) for path in where.glob("slot.*.lock")
               if (match := re.fullmatch(r"slot\.([0-9]+)\.lock", path.name))}
    for number in sorted(numbers):
        lock = where / f"slot.{number}.lock"
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            continue
        try:
            if try_lock(fd):
                fcntl.flock(fd, fcntl.LOCK_UN)
                continue
            text = lock.read_text(encoding="utf-8", errors="replace").split()
            pid = (int(text[0].lstrip("0") or "0") if text and text[0].isascii() and text[0].isdigit()
                   and len(text[0].lstrip("0")) <= MAX_DIGITS else None)
            try:
                info = json.loads(lock.with_suffix(".info").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = {}
            if not isinstance(info, dict) or info.get("pid") != pid:
                info = {}
            since = as_time(info.get("since"))
            if since is None:
                since = lock.stat().st_mtime
            holders.append({"slot": number, "pid": pid, "label": info.get("label", ""),
                            "cwd": info.get("cwd", ""), "since": utc_stamp(since)})
        finally:
            os.close(fd)
    return holders


def held_by(queue: Queue) -> str:
    """The holders as one clause for a waiting line, or "" when none can be read (sd:2522)."""
    try:
        with queue.admission_lock():
            holders = read_holders(queue.where)
    except OSError:
        return ""
    return "".join(f"; slot {entry['slot']} held by {entry['label'] or 'an unlabelled gate'} "
                   f"(pid {entry['pid'] or '?'}) since {entry['since']}"
                   + (f" in {entry['cwd']}" if entry["cwd"] else "") for entry in holders)


def snapshot(where: pathlib.Path, slots: int, rule: LoadRule, *,
             loadavg: Callable[[], Sequence[float]] = os.getloadavg, wall: Callable[[], float] = time.time,
             busy: Callable[[], float | None] = cpu_busy) -> dict:
    """Who holds a slot (`read_holders`), who waits and in which place, the lane gates among them (sd:5078),
    and the load rule; read under `queue.lock`."""
    queue = Queue(where)
    holders, waiters, lanes, state = [], [], [], {}
    if where.is_dir():
        with queue.admission_lock():
            state = queue.state()
            holders = read_holders(where)
            live = queue.waiters(prune=False)
            lanes = lane_gates(where, live)
            for place, entry in enumerate(admission_order(live), start=1):
                queued = as_time(entry.get("since"))
                waiters.append({"place": place, "pid": entry.get("pid"), "label": entry.get("label", ""),
                                "cwd": entry.get("cwd", ""),
                                "since": utc_stamp(queued) if queued is not None else ""})
    try:
        load = [round(float(value), 2) for value in tuple(loadavg())[:3]]
    except OSError:
        load = []
    share = busy()
    last = state.get("last_admitted")
    return {"directory": str(where), "slots": slots, "holders": holders, "waiters": waiters, "lane_gates": lanes,
            "load": load, "cpu_busy": None if share is None else round(share, 3),
            "load_counted": round(counted_load(load[1], share), 2) if len(load) > 1 else None,
            "load_max": rule.limit, "settle_seconds": rule.settle, "rule_source": rule.source,
            "last_admitted": utc_stamp(last) if isinstance(last, (int, float)) else None, "now": utc_stamp(wall())}


def machine_snapshot(environ: Mapping[str, str]) -> dict | str:
    """`snapshot` with the machine's slots and load rule, as `sd gate status` reads them, or why it could not be read.

    For `sd today` and `sd-status`: a failed read is unknown, never an idle gate.
    """
    try:
        settings = machine_settings(environ)
        slots, _ = configured(environ, settings["gate_slots"])
        rule = load_rule(environ, settings["gate_load_max"], settings["gate_settle_seconds"])
        return snapshot(directory(environ), slots, rule)
    except (OSError, ValueError) as error:
        return str(error)


def render_queue(report: dict) -> str:
    load = "/".join(f"{value:.1f}" for value in report["load"]) or "unknown"
    limit = f"{report['load_max']:g}" if report["load_max"] > 0 else "off"
    lines = [f"gate slots under {report['directory']}: {len(report['holders'])} of {report['slots'] or 'no cap'} in use; "
             f"load {load}, limit {limit}, settle {report['settle_seconds']:g}s",
             f"last start: {report['last_admitted'] or 'none recorded'}"]
    lines.append("lane: " + ("; ".join(lane_line(entry) for entry in report["lane_gates"]) or "none"))
    lines.append("holding:" if report["holders"] else "holding: none")
    for entry in report["holders"]:
        lines.append(f"  slot {entry['slot']}  pid {entry['pid']}  since {entry['since']}  "
                     f"{entry['label'] or '(no label)'}  {entry['cwd']}".rstrip())
    lines.append("waiting:" if report["waiters"] else "waiting: none")
    for entry in report["waiters"]:
        lines.append(f"  {entry['place']}. pid {entry['pid']}  since {entry['since']}  "
                     f"{entry['label'] or '(no label)'}  {entry['cwd']}".rstrip())
    return "\n".join(lines)


def wait_seconds(text: str) -> float:
    """A `--max-wait` value: a non-negative number, so never `nan`, which no deadline reaches."""
    return parse_number(text, "--max-wait")


def add_gate_verbs(verbs: argparse._SubParsersAction) -> None:
    """`run` and `status`, shared by this file and `sd gate`."""
    runner = verbs.add_parser("run", help="wait in the machine-wide gate queue, in the order it queued beside "
                              "lane gates (sd-check), run a command, free the slot")
    runner.add_argument("--label", default="", help="what `status` shows for this gate (default: the command)")
    runner.add_argument("--max-wait", type=wait_seconds, metavar="SECONDS",
                        help=f"give up waiting for a slot after this long and exit {RUN_NO_SLOT}, the command not run; "
                             "it does not bound the command itself (default: wait as long as it takes)")
    runner.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command and its arguments")
    shower = verbs.add_parser("status", help="who holds a gate slot, who waits, which of them are lane gates, "
                              "and since when")
    shower.add_argument("--json", action="store_true", help="one machine-readable object")


def gate_verb(args: argparse.Namespace, environ: Mapping[str, str], slots: int, rule: LoadRule) -> int:
    if args.command_name == "status":
        report = snapshot(directory(environ), slots, rule)
        print(json.dumps(report, indent=2) if args.json else render_queue(report))
        return 0
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    return run_gated(command, environ, slots=slots, rule=rule, stream=sys.stderr, label=args.label,
                     max_wait=args.max_wait)


def slot_command(argv: list[str] | None = None) -> int:
    """`directory` prints where the slots live; `count` prints how many there are;
    `wait --pid P --ppid Q --dir D FD...` queues, locks one inherited descriptor
    and prints its index; `run` and `status` are `sd gate run` and `sd gate
    status`. All of them read the machine settings through `machine_settings`,
    so the count is `sd-check`'s: `SD_GATE_SLOTS`, CI, `sd.gate_slots`, the default.

    The shell that opened the descriptors keeps the lock after `wait` exits.
    Exit 3 when P's launcher exited, or when this process's own parent did.
    """
    parser = argparse.ArgumentParser(prog="sd_gate_slots.py")
    commands = parser.add_subparsers(dest="command_name", required=True)
    commands.add_parser("directory", help="print the slot directory")
    commands.add_parser("count", help="print how many slots this machine has (0: no cap)")
    wait = commands.add_parser("wait", help="queue, then lock one of the inherited slot descriptors")
    wait.add_argument("--pid", type=int, required=True, help="the gate's pid")
    wait.add_argument("--ppid", type=int, required=True, help="the gate's parent when it started")
    wait.add_argument("--dir", required=True, help="the slot directory, for the waiting message")
    wait.add_argument("fds", type=int, nargs="+")
    add_gate_verbs(commands)
    args = parser.parse_args(argv)
    if args.command_name == "directory":
        print(directory(os.environ))
        return 0
    try:
        settings = machine_settings(os.environ, stream=sys.stderr)
        rule = load_rule(os.environ, settings["gate_load_max"], settings["gate_settle_seconds"])
        if args.command_name in ("count", "run", "status"):
            slots, _ = configured(os.environ, settings["gate_slots"])
            if args.command_name == "count":
                print(slots)
                return 0
            return gate_verb(args, os.environ, slots, rule)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        if args.command_name != "run":
            return 2
        print(NOT_STARTED, file=sys.stderr)
        return RUN_NOT_STARTED
    gate_gone = launcher_watch(args.pid, args.ppid)
    shell = os.getppid()
    where = pathlib.Path(args.dir)
    try:
        taken = wait_for_slot(args.fds, where, poll=poll_seconds(os.environ), stream=sys.stderr,
                              stop=lambda: os.getppid() != shell or gate_gone(), rule=rule,
                              label=f"run-tests.sh pid {args.pid}")
    except LauncherExited:
        return LAUNCHER_EXITED
    if taken is None:
        return TIMED_OUT
    write_info(where / f"slot.{taken + 1}.lock", args.pid, "run-tests.sh", os.getcwd())
    print(taken)
    return 0


if __name__ == "__main__":
    raise SystemExit(slot_command())
