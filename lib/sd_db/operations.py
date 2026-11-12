"""Bounded launchd controls and local queued-assignment cancellation.

The installed plist, loaded service and trusted job files must agree. macOS
documents launchctl print as diagnostic output, not a stable API: an unknown
shape therefore disables actions. Only approved fields leave this module.
Launchctl accepting a request is distinct from observing the requested state.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import signal
import sqlite3
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from xml.parsers.expat import ExpatError

from . import build, config, runner_journal
from .database import refuse_hub_only, transaction
from .runner import RunnerRefused
from .workflow import StaleItem, WorkflowError, _identifier, _text
from .writes import add_note, now, record_state, resolve_state, update_assignment

JOB_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}\Z")
#: The launchd label prefix all system agents share. The operator sets
#: `SYSTEM_TOOLS_LABEL_PREFIX` to keep labels installed under another prefix.
LABEL_PREFIX = os.environ.get("SYSTEM_TOOLS_LABEL_PREFIX", "local.system-tools")
PREFIX = LABEL_PREFIX + ".cron."
# launchd records a signal and not a reason, so the split below is by who
# meant the process to stop. A stopped job or service whose last run ended in
# one of these is `failed`: the kernel ended the run for something the process
# did. Both launchd parsers read this one set: the cron-job one below and the
# service one in services.py, which imports it from here (sd:1331, sd:1344).
#   faults and aborts: SIGILL 4, SIGTRAP 5, SIGABRT 6, 7 (SIGBUS on Linux,
#                      SIGEMT here), SIGFPE 8, SIGBUS 10, SIGSEGV 11, SIGSYS 12
#   resource limits:   SIGXCPU 24 (CPU time), SIGXFSZ 25 (file size)
# Not here: SIGHUP 1, SIGINT 2, SIGKILL 9, SIGTERM 15, and every other number.
# Those are the stop path -- launchd's own bootout, a deploy restart, an
# operator's kill, the `cancel` below -- and say nothing about the process.
# What they say about the work differs. A stopped service reads `idle` on
# one: a service is meant to keep running, so a stop is ordinary. A stopped
# job reads `interrupted`: a job is one unit of scheduled work, and a signal
# says that unit may not have finished (the review of sd:1344 caught SIGKILL
# reading idle and leaving the attention list). The one stop with evidence
# it was meant is this module's own accepted `cancel` of that same run, which
# `_job_state` reads back as `idle`; the parser below has no request to read.
_FAILURE_SIGNALS = frozenset({4, 5, 6, 7, 8, 10, 11, 12, 24, 25})


def _signal_name(number):
    """`SIGSEGV (11)` when this platform names the number, `signal 11` otherwise."""
    try:
        return f"{signal.Signals(number).name} ({number})"
    except ValueError:
        return f"signal {number}"


@contextmanager
def control_gate(database):
    """Serialize controls and restore installation without monopolizing SQL.

    Only a lock another holder owns is reported as work in progress. A lock
    that could not be opened at all keeps the opener's own sentence, so a
    read-only or unwritable state directory does not send the operator
    looking for a competing process that does not exist.
    """
    refuse_hub_only(database, "service controls and restore")
    # A connection of any kind, not only `sqlite3.Connection`: a guarded or a
    # remote one reached `Path(database)` below and died with a TypeError.
    if not isinstance(database, (str, os.PathLike)):
        if database.in_transaction:
            raise WorkflowError("control gate must precede the database transaction")
        filename = next(row[2] for row in database.execute("PRAGMA database_list") if row[1] == "main")
        if not filename:
            raise WorkflowError("service controls require a file-backed database")
        database = Path(filename)
    path = Path(database).resolve().parent / "operation-locks" / "control.lock"
    entered = False
    try:
        with runner_journal.lock(path, blocking=False, noun="control"):
            entered = True
            yield
    except RunnerRefused as error:
        if entered:
            raise
        if not isinstance(error.__cause__, BlockingIOError):
            raise WorkflowError(str(error)) from error
        raise WorkflowError("another control or restore is in progress; refresh before retrying") from error


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _job_name(name: str) -> None:
    if not isinstance(name, str) or not JOB_NAME.fullmatch(name):
        raise WorkflowError("job must be an installed safe job name")


def _revision(actual: str, expected: str) -> None:
    if not isinstance(expected, str) or not expected:
        raise WorkflowError("expected_revision is required")
    if actual != expected:
        raise StaleItem("state changed; refresh before trying again")


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)


_BOOT = []
_BOOT_LOCK = threading.Lock()


def _boot_token():
    """The boot launchd's coalition ids were counted in: `kern.boottime` seconds,
    or None when it cannot be read. Read once per process -- a reboot ends the
    process -- and a failed read is not kept, so a later call may still answer.
    `inventory` inspects from several threads; the lock keeps the read to one."""
    with _BOOT_LOCK:
        if not _BOOT:
            try:
                result = _run(["/usr/sbin/sysctl", "-n", "kern.boottime"])
            except (OSError, subprocess.SubprocessError, UnicodeError):
                return None
            # `\bsec`: the line carries `usec` too, and `sec = ` alone reads the microseconds.
            match = re.search(r"\bsec = (\d+)", result.stdout) if result.returncode == 0 else None
            if not match:
                return None
            _BOOT.append(int(match[1]))
        return _BOOT[0]


def _inspect_all(backend, names: list[str]) -> list:
    """`backend.inspect` for each name, in order, run in parallel: launchctl is
    the wall time of an inventory (sd:5005). Only inspects run in threads; the
    caller reads the connection afterwards, on its own thread. A name that is no
    longer supported, such as one that vanished after enumeration, gives None;
    any other error propagates as the serial loop let it."""
    def inspect(name):
        try:
            return backend.inspect(name)
        except WorkflowError:
            return None
    # ponytail: at most 8 launchctl processes at once per inventory read; enough to
    # halve the wall time, bounded so a page read cannot start one per job.
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(inspect, names))


def job_dirs(environ=None) -> list[Path]:
    """Where `cron-jobs.sh` finds a job file, in its lookup order.

    Each `CRON_JOBS_EXTRA_DIRS` entry first, then this host's
    `<config>/cron-jobs/jobs/<host>`, then the shared `<config>/cron-jobs/jobs`
    (`config.cron_job_dirs` holds the rule).
    """
    return config.cron_job_dirs(environ)


class LaunchdBackend:
    """Default roots are fixed; optional roots/runner support isolated tests.

    These options are deliberately Python-only and must not be HTTP or CLI
    parameters. No job configuration is sourced or evaluated to inspect it.
    """

    def __init__(self, *, launch_agents=None, cron_root=None, jobs_dirs=None, uid=None, runner=None, boot=None):
        self.launch_agents = Path(launch_agents) if launch_agents is not None else Path.home() / "Library/LaunchAgents"
        # The checkout that holds local-cron-jobs: `$SYSTEM_TOOLS_ROOT`, else
        # `~/repos/system`. The library runs from a virtualenv, so its own
        # file path does not locate the checkout.
        root = os.environ.get("SYSTEM_TOOLS_ROOT") or str(Path.home() / "repos/system")
        self.cron_root = Path(cron_root) if cron_root is not None else Path(os.path.expanduser(root)) / "local-cron-jobs"
        # The job files are private config, not checkout content: the same
        # directories `cron-jobs.sh` reads, in its order -- each
        # `CRON_JOBS_EXTRA_DIRS` entry first, then this host's
        # `<config>/cron-jobs/jobs/<host>`, then the shared `<config>/cron-jobs/jobs`.
        self.jobs_dirs = [Path(d) for d in jobs_dirs] if jobs_dirs is not None else job_dirs()
        self.uid = os.getuid() if uid is None else uid
        self.runner = runner or _run
        self.boot = boot or _boot_token

    @staticmethod
    def _file(path: Path, root: Path) -> bytes:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise WorkflowError("job is not supported: trusted regular files are required")
        return path.read_bytes()

    def _installed(self, name: str) -> dict:
        _job_name(name)
        label = PREFIX + name
        plist = self.launch_agents / (label + ".plist")
        script = self.cron_root / "cron-jobs.sh"
        jobs_dir = next((d for d in self.jobs_dirs if (d / (name + ".job")).exists()), self.jobs_dirs[-1])
        job = jobs_dir / (name + ".job")
        try:
            raw = self._file(plist, self.launch_agents)
            config = plistlib.loads(raw)
            argv = ["/bin/bash", str(script), "exec", name]
            # system #538 starts each plist through job-launch; an older
            # install runs the bare argv. Accept exactly these two shapes.
            launch = str(Path.home() / ".local/libexec/system-tools/job-launch")
            if isinstance(config, dict) and config.get("ProgramArguments") == [launch] + argv:
                argv = [launch] + argv
            if not isinstance(config, dict) or config.get("Label") != label or config.get("ProgramArguments") != argv:
                raise WorkflowError("job is not supported: installed command does not match the trusted job runner")
            if config.get("Program", argv[0]) != argv[0]:
                raise WorkflowError("job is not supported: alternate executable")
            hashes = [hashlib.sha256(value).hexdigest() for value in
                      (raw, self._file(script, self.cron_root), self._file(job, jobs_dir))]
        except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as error:
            raise WorkflowError("job is not supported: installed configuration is unavailable or invalid") from error
        schedule = config.get("StartCalendarInterval", [])
        schedule = [schedule] if isinstance(schedule, dict) else schedule
        keys = {"Minute", "Hour", "Day", "Weekday", "Month"}
        if not isinstance(schedule, list) or any(not isinstance(entry, dict) or
                not set(entry).issubset(keys) or any(type(value) is not int for value in entry.values())
                for entry in schedule):
            schedule = []
        return {"name": name, "label": label, "service": f"gui/{self.uid}/{label}",
                "schedule": schedule, "_plist": str(plist), "_argv": argv, "_hashes": hashes}

    def names(self) -> list[str]:
        result = []
        for path in sorted(self.launch_agents.glob(PREFIX + "*.plist")):
            name = path.name[len(PREFIX):-len(".plist")]
            try:
                self._installed(name)
            except WorkflowError:
                continue
            result.append(name)
        return result

    def inspect(self, name: str) -> dict:
        installed = self._installed(name)
        state = {key: installed[key] for key in ("name", "label", "service", "schedule")}
        state.update(state="unknown", pid=None, last_exit=None, last_signal=None, runs=None, lifetime=None, boot=None,
                     reason="launchctl state is unavailable or unrecognized")
        try:
            result = self.runner(["/bin/launchctl", "print", installed["service"]])
            if result.returncode == 113:
                state.update(state="unloaded", reason="job is installed but not loaded")
            elif result.returncode == 0:
                state.update(self._parse(result.stdout, installed))
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass
        state["boot"] = self.boot()
        state["_observation"] = _digest({"installed": installed, "observed": state})
        return state

    @staticmethod
    def _parse(output: str, installed: dict) -> dict:
        lines = output.splitlines()
        if not lines or lines[0] != installed["service"] + " = {" or lines[-1] != "}":
            return {}
        fields, arguments, coalition = {}, None, None
        for index, line in enumerate(lines):
            match = re.fullmatch(r"\t([^\t=]+) = ([^\r\n]*)", line)
            if match and match[1] in {"path", "program", "state", "pid", "last exit code", "last terminating signal", "runs"}:
                if match[1] in fields:
                    return {}
                fields[match[1]] = match[2]
            if line == "\targuments = {":
                if arguments is not None:
                    return {}
                arguments = []
                for argument in lines[index + 1:]:
                    if argument == "\t}":
                        break
                    if not argument.startswith("\t\t") or argument.startswith("\t\t\t"):
                        return {}
                    arguments.append(argument[2:])
                else:
                    return {}
            if line == "\tresource coalition = {":
                # The launchd lifetime the `runs` counter belongs to. The counter
                # restarts at 1 whenever the label is bootstrapped again -- a reboot
                # does it to every label, a bootout and bootstrap to one -- and the
                # resource coalition id moves when and only when it does (measured
                # in cron-jobs.sh, whose run record holds the same pair). A label
                # bootstrapped and not yet spawned has no coalition: None, never
                # evidence. `jetsam coalition` below it has its own id; not this one.
                if coalition is not None:
                    return {}
                coalition = ""
                for entry in lines[index + 1:]:
                    if entry == "\t}":
                        break
                    found = re.fullmatch(r"\t\tID = (\d+)", entry)
                    if found:
                        coalition = found[1]
                else:
                    return {}
        if (fields.get("path") != installed["_plist"] or fields.get("program") != installed["_argv"][0] or
                arguments != installed["_argv"] or fields.get("state") not in {"running", "not running", "waiting"}):
            return {}
        try:
            runs = int(fields["runs"])
            pid = int(fields["pid"]) if "pid" in fields else None
            # launchd prints one line for the last run's outcome: `last exit code = N`
            # after an exit, `(never exited)` before any, and after a signal
            # `last terminating signal = Terminated: 15` with no exit-code line at
            # all. int(None) on that third shape -- the KeyError swallowed below --
            # read a job that `cancel` had stopped as unknown with the generic
            # reason (sd:1344, the shape services.py fixed in sd:1331); a recorded
            # signal is an outcome, not a gap.
            code, killed = fields.get("last exit code"), fields.get("last terminating signal")
            ended = None if killed is None else re.fullmatch(r"[^\r\n]*: (\d+)", killed)
            if killed is not None and ended is None:
                return {}
            last_signal = None if ended is None else int(ended[1])
            last_exit = None if code in (None, "(never exited)") else int(code)
        except (KeyError, TypeError, ValueError):
            return {}
        running = fields["state"] == "running"
        if runs < 0 or (running and (pid is None or pid <= 0)) or (not running and pid is not None):
            return {}
        if code is None and last_signal is None and runs > 0:
            # Neither line after a run has not been observed from launchd; say so
            # rather than fold it into the generic unrecognized-output reason.
            return {"state": "unknown", "reason":
                    f"launchctl reports {runs} runs with neither a last exit code nor a terminating signal"}
        # A running pid wins over any history. Stopped, a non-zero exit is the
        # process's own verdict and a failure signal is the kernel's; both are
        # `failed`. Any other signal with no exit code for that run is a stop
        # nobody here has accounted for: `interrupted`, until `_job_state` finds
        # this module's own cancel of that run. An exit code beside a signal
        # line is the last run's own verdict, and the signal is history.
        killed_for_cause = not running and last_signal in _FAILURE_SIGNALS
        interrupted = not running and code is None and last_signal is not None and not killed_for_cause
        state = ("running" if running else "failed" if last_exit or killed_for_cause else
                 "interrupted" if interrupted else "idle")
        return {"state": state, "pid": pid, "last_exit": last_exit, "last_signal": last_signal, "runs": runs,
                "lifetime": int(coalition) if coalition else None,
                "reason": f"last run ended in {_signal_name(last_signal)}" if killed_for_cause or interrupted else ""}

    def perform(self, action: str, service: str):
        if action == "retry":
            return self.runner(["/bin/launchctl", "kickstart", service])
        if action == "cancel":
            return self.runner(["/bin/launchctl", "kill", "SIGTERM", service])
        raise WorkflowError("unsupported job action")


def _last_request(connection, name):
    row = connection.execute("SELECT body FROM state WHERE kind='checkpoint' AND key=? ORDER BY id DESC LIMIT 1",
                             ("operations:job:" + name,)).fetchone()
    if row is None:
        return None
    try:
        body = json.loads(row["body"])
        return body if isinstance(body, dict) else None
    except (ValueError, TypeError):
        return None


def _request_public(request):
    return None if request is None else {key: request.get(key) for key in
            ("id", "action", "status", "at", "message", "who")}


def _restoring(connection):
    return connection.execute("SELECT 1 FROM state WHERE kind='restore' AND resolved_at IS NULL LIMIT 1").fetchone() is not None


_RUN_IDENTITY = ("runs", "lifetime", "boot")


def _cancelled(request, observed):
    """This module's own accepted cancel of the run launchd last spawned: the
    one stop with evidence it was meant. SIGTERM is what `cancel` sends, and
    `launchctl kill` escalates to nothing, so SIGKILL is never ours.

    The run is named by three fields, all recorded on the request at dispatch.
    `runs` alone is not a name: it restarts at 1 whenever the label is
    bootstrapped again, so cancel run 1, reload, and an external SIGTERM on
    the new run 1 matched the old cancel and read idle (review of round 2).
    The resource coalition id moves exactly when the counter restarts, and it
    is boot-local -- it restarts near zero at every boot, so a label loaded
    after a reboot can be handed the id an older request names -- so the boot
    it was counted in comes with it. A field unreadable on either side, or a
    request from before the three were recorded, is no evidence."""
    if request is None or request.get("action") != "cancel" or request.get("status") != "accepted":
        return False
    if observed["last_signal"] != signal.SIGTERM:
        return False
    identity = tuple(observed[key] for key in _RUN_IDENTITY)
    return None not in identity and identity == tuple(request.get(key) for key in _RUN_IDENTITY)


def _retry_label(state):
    return "Retry" if state in {"failed", "interrupted"} else "Run now"


def _job_state(connection, name, backend, *, observed=None):
    observed = backend.inspect(name) if observed is None else observed
    request = _last_request(connection, name)
    restoring = _restoring(connection)
    state = "idle" if observed["state"] == "interrupted" and _cancelled(request, observed) else observed["state"]
    capabilities = {}
    # Retry kickstarts any stopped job: a failed or interrupted run again, or
    # an idle job now, which is how a job with no calendar starts (sd:4965).
    for action, needed in (("retry", ("failed", "interrupted", "idle")), ("cancel", ("running",))):
        # The observed reason refuses an action only when the observation itself
        # does, unknown or unloaded. A failed or interrupted job's reason names
        # the signal that ended its last run: a description, not a refusal.
        blocked = observed["reason"] if state in {"unknown", "unloaded"} else ""
        refused = "the job is already running" if state == "running" else f"only a currently {' or '.join(needed)} job can {action}"
        reason = "" if state in needed else blocked or refused
        if request and request.get("status") in {"requested", "accepted", "unknown"} and request.get("before") == observed["_observation"]:
            reason = "previous request has no observed state change yet; refresh to observe its outcome"
        if action == "retry" and restoring:
            reason = "an unresolved restore prevents starting jobs"
        capabilities[action] = {"allowed": not reason, "reason": reason}
    capabilities["retry"]["label"] = _retry_label(state)
    public = {key: observed[key] for key in ("name", "label", "service", "schedule", "state", "pid", "last_exit", "last_signal")}
    # Which run launchd last recorded, by the identity a cancel is matched on;
    # `revision` moves on a request or a restore too (sd:2904).
    public.update(state=state, capabilities=capabilities, last_request=_request_public(request),
                  run=[observed.get(key) for key in _RUN_IDENTITY])
    public["revision"] = _digest({"observed": observed["_observation"], "request": request, "restore": restoring})
    return public, observed


def job_state(connection: sqlite3.Connection, job: str, *, backend=None) -> dict:
    return _job_state(connection, job, backend or LaunchdBackend())[0]


def inventory(connection: sqlite3.Connection, *, backend=None) -> dict:
    backend = backend or LaunchdBackend()
    jobs = []
    names = backend.names()
    for name, observed in zip(names, _inspect_all(backend, names), strict=True):
        if observed is not None:  # An installed job can disappear between enumeration and read.
            jobs.append(_job_state(connection, name, backend, observed=observed)[0])
    assignments = [assignment_state(connection, row[0]) for row in connection.execute(
        "SELECT id FROM assignment ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, id DESC")]
    return {"jobs": jobs, "assignments": assignments,
            "revision": _digest({"jobs": jobs, "assignments": assignments})}


def _save_request(connection, job, request):
    row = record_state(connection, "checkpoint", key="operations:job:" + job, body=request)
    resolve_state(connection, row)


def _act(connection, job, action, *, expected_revision, who, backend):
    who = _text(who, "who")
    if connection.in_transaction:
        raise WorkflowError("job actions require their own durable transaction")
    backend = backend or LaunchdBackend()
    with control_gate(connection):
        observed = backend.inspect(job)
        with transaction(connection):
            current, observed = _job_state(connection, job, backend, observed=observed)
            _revision(current["revision"], expected_revision)
            capability = current["capabilities"][action]
            if not capability["allowed"]:
                raise WorkflowError(capability["reason"])
            request = {"id": uuid.uuid4().hex, "action": action, "status": "requested", "at": now(),
                       "who": who, "before": observed["_observation"],
                       **{key: observed[key] for key in _RUN_IDENTITY},
                       "message": "request recorded; execution outcome is not yet known"}
            _save_request(connection, job, request)
        # Durable intent survives a crash. The separate gate prevents restore
        # installation or a second control crossing this final observation.
        try:
            latest = backend.inspect(job)
            if latest["_observation"] != observed["_observation"]:
                request.update(status="failed", message="job changed before dispatch; no action was sent")
            elif action == "retry" and _restoring(connection):
                request.update(status="failed", message="restore prevents dispatch; no action was sent")
            else:
                result = backend.perform(action, current["service"])
                if result.returncode == 0:
                    request.update(status="accepted", message="launchctl accepted the request; current state is observed separately")
                else:
                    request.update(status="failed", message=f"launchctl refused the request (exit {result.returncode})")
        except WorkflowError:
            request.update(status="failed", message="job is no longer supported; no action was sent")
        except subprocess.TimeoutExpired:
            request.update(status="unknown", message="launchctl timed out; the request outcome is unknown")
        except (OSError, subprocess.SubprocessError):
            request.update(status="unknown", message="launchctl could not report an outcome")
        with transaction(connection):
            _save_request(connection, job, request)
    try:
        after = job_state(connection, job, backend=backend)
    except WorkflowError:
        after = dict(current, state="unknown", pid=None, revision=_digest(request), last_request=_request_public(request),
                     capabilities={key: {"allowed": False, "reason": "job is no longer supported"} for key in ("retry", "cancel")})
        after["capabilities"]["retry"]["label"] = _retry_label("unknown")
    return {"request": _request_public(request), "job": after}


def retry_job(connection: sqlite3.Connection, job: str, *, expected_revision: str, who, backend=None) -> dict:
    return _act(connection, job, "retry", expected_revision=expected_revision, who=who, backend=backend)


def cancel_job(connection: sqlite3.Connection, job: str, *, expected_revision: str, who, backend=None) -> dict:
    return _act(connection, job, "cancel", expected_revision=expected_revision, who=who, backend=backend)


def assignment_state(connection: sqlite3.Connection, assignment: int) -> dict:
    _identifier(assignment, "assignment")
    # An unreleased runner attempt, its own or its bundle lead's, decides
    # whether a blocked row may be cancelled, so it is in the row the
    # revision hashes, as the item's status is.
    # A running build's bundle run is its lead's (`build.claim`), and the
    # cancel is asked on that run, so the run's id is in the row too; an
    # ended run the reap has not released yet takes it as well.
    row = connection.execute("SELECT assignment.*, item.title AS title, item.status AS item_status, "
                             "EXISTS (SELECT 1 FROM runner_run WHERE runner_run.assignment "
                             "IN (assignment.id, assignment.parent) AND runner_run.released_at IS NULL) AS leased, "
                             "(SELECT runner_run.id FROM runner_run WHERE runner_run.assignment="
                             "COALESCE(assignment.parent, assignment.id) AND runner_run.released_at IS NULL) "
                             "AS build_run, "
                             "(SELECT runner_run.step FROM runner_run WHERE runner_run.assignment="
                             "COALESCE(assignment.parent, assignment.id) AND runner_run.released_at IS NULL) "
                             "AS build_step "
                             "FROM assignment LEFT JOIN item ON item.id=assignment.item WHERE assignment.id=?",
                             (assignment,)).fetchone()
    if row is None:
        raise WorkflowError(f"no assignment {assignment}")
    # A blocked assignment is clutter nothing will run again (sd:2082): a
    # requeue makes a new row, and the item's status is the operator's. So
    # cancelled, which is terminal, is safe once no runner attempt holds its
    # lease, whatever the item's status: an open item's dead rows piled up
    # with no way to clear them (pack sd:8606).
    if row["status"] == "queued":
        reason = ""
    elif row["status"] == "running" and row["scope"] == "build" and row["build_run"]:
        # From `enqueuing` on the lane may hold the branch: the lane entry is what to cancel (pack sd:6094).
        reason = ("" if row["build_step"] is None or row["build_step"] in build.CANCELLABLE
                  else f"build run {row['build_run']} is at {row['build_step']}; "
                       f"{build.stop_step(connection, row['build_run'], row['item'])}")
    elif row["status"] == "running":
        reason = "running assignment has no supported cancellation backend"
    elif row["status"] != "blocked":
        reason = "only a queued or blocked assignment can be cancelled"
    elif row["leased"]:
        reason = "a runner attempt still holds this assignment's lease"
    else:
        reason = ""
    result = {key: row[key] for key in ("id", "item", "title", "role", "provider", "status", "started", "ended",
                                        "scope", "build_run")}
    result["revision"] = _digest(dict(row))
    result["capabilities"] = {"cancel": {"allowed": not reason, "reason": reason}}
    return result


def cancel_assignment(connection: sqlite3.Connection, assignment: int, *, expected_revision: str, who) -> dict:
    who = _text(who, "who")
    with transaction(connection):
        current = assignment_state(connection, assignment)
        _revision(current["revision"], expected_revision)
        if not current["capabilities"]["cancel"]["allowed"]:
            raise WorkflowError(current["capabilities"]["cancel"]["reason"])
        ended = now()
        if current["status"] == "running":
            # A running build stops at its supervisor's next beat, or at the
            # reap when no supervisor lives; either ends the whole bundle
            # `cancelled` and moves its items back to ready (pack sd:6093).
            # An ended run not yet released takes the cancel too: its settle
            # ends it cancelled instead of opening a fresh row.
            # The step is checked in the write, so a supervisor's enqueuing
            # compare-and-set and this cancel have one winner (pack sd:6094).
            marks = ", ".join("?" for _ in build.CANCELLABLE)
            if connection.execute(f"UPDATE runner_run SET cancel_requested = ?, updated_at = ? "
                                  f"WHERE id = ? AND released_at IS NULL AND (step IS NULL OR step IN ({marks}))",
                                  (f"cancelled by {who}", ended, current["build_run"], *build.CANCELLABLE)
                                  ).rowcount != 1:
                raise WorkflowError(f"build run {current['build_run']} moved past the steps a cancel may stop; "
                                    f"{build.stop_step(connection, current['build_run'], current['item'])}")
            audit = {"assignment": assignment, "run": current["build_run"], "status": "cancel_requested",
                     "who": who, "at": ended}
            row = record_state(connection, "checkpoint", key=f"operations:assignment:{assignment}", body=audit)
            resolve_state(connection, row)
            add_note(connection, current["item"], "comment",
                     f"Running build assignment {assignment} cancel requested by {who}; run "
                     f"{current['build_run']} stops with its bundle", session=who)
            return assignment_state(connection, assignment)
        update_assignment(connection, assignment, status="cancelled", ended=ended)
        audit = {"assignment": assignment, "old_status": current["status"], "status": "cancelled", "who": who, "at": ended}
        row = record_state(connection, "checkpoint", key=f"operations:assignment:{assignment}", body=audit)
        resolve_state(connection, row)
        if current["item"] is not None:
            add_note(connection, current["item"], "comment",
                     f"{current['status'].capitalize()} assignment {assignment} cancelled by {who}; item status unchanged", session=who)
        return assignment_state(connection, assignment)
