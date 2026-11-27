"""Inspect installed launchd services and control eligible user LaunchAgents.

Plists are configuration, never client-supplied commands. Stopping bootouts a
service for the current login, leaving its plist and persistent enablement alone.
Diagnostic output that cannot prove the loaded identity permits no control.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import plistlib
import re
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path
from xml.parsers.expat import ExpatError

from .database import transaction
from .operations import (
    _FAILURE_SIGNALS,
    LABEL_PREFIX,
    PREFIX,
    _digest,
    _inspect_all,
    _restoring,
    _revision,
    _run,
    _signal_name,
    control_gate,
)
from .workflow import WorkflowError, _text
from .writes import now, record_state, resolve_state

_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_ACTIONS = ("start", "stop", "restart")
_PUBLIC = ("id", "label", "name", "domain", "scope", "category", "state", "pid", "last_exit", "last_signal", "reason")
_STOP_NOTE = "Stop unloads this service for the current login; its installed plist remains and may load at next login."
# `_FAILURE_SIGNALS`, the split of launchd's terminating signals by who meant
# the stop, and `_signal_name` are imported from operations.py: the cron-job
# parser there reads the same lines, and one set keeps the two from drifting.
_SETTLE_SECONDS = 2


def _identifier(value, *, mutation=False):
    if not isinstance(value, str):
        raise WorkflowError("service must be an installed launchd label")
    scope, label = value.split(":", 1) if ":" in value else ("user", value)
    if (scope not in {"user", "system"} or not _LABEL.fullmatch(label) or
            (mutation and (scope != "user" or ":" in value))):
        raise WorkflowError("service actions require a bare installed user LaunchAgent label" if mutation
                            else "service must be an installed launchd label")
    return scope, label


class ServiceBackend:
    """Fixed production roots; optional roots and runner are isolated test seams."""

    def __init__(self, *, user_agents=None, system_daemons=None, uid=None, runner=None):
        self.user_agents = Path(user_agents) if user_agents is not None else Path.home() / "Library/LaunchAgents"
        self.system_daemons = Path(system_daemons) if system_daemons is not None else Path("/Library/LaunchDaemons")
        self.uid = os.getuid() if uid is None else uid
        self.runner = runner or _run
        #: domain -> `print-disabled`'s overrides, or None: set only on a copy `one_read` makes (sd:10815).
        self._overrides: dict | None = None
        self._overrides_lock = threading.Lock()

    def one_read(self):
        """A copy of this backend that asks `launchctl print-disabled` once per domain: an inventory ran it once per
        service, 13 times a read. Only an inventory uses it; a control inspects afresh, since it acts on the answer."""
        copied = copy.copy(self)
        copied._overrides, copied._overrides_lock = {}, threading.Lock()
        return copied

    def names(self):
        result = []
        for scope, root in (("user", self.user_agents), ("system", self.system_daemons)):
            for path in sorted(root.glob("*.plist")):
                label = path.stem
                if _LABEL.fullmatch(label) and not label.startswith(PREFIX):
                    result.append(scope + ":" + label)
        return result

    def _installed(self, identifier):
        scope, label = _identifier(identifier)
        root = self.user_agents if scope == "user" else self.system_daemons
        path = root / (label + ".plist")
        if not path.exists() and not path.is_symlink():
            raise WorkflowError(f"no installed service {scope}:{label}")
        domain = f"gui/{self.uid}" if scope == "user" else "system"
        installed = {"id": scope + ":" + label, "label": label, "name": label,
                     "scope": scope, "domain": domain, "category": "unsupported",
                     "_service": domain + "/" + label, "_plist": str(path),
                     "_reason": "installed configuration is unavailable or unsupported",
                     "_disabled": None, "_argv": None, "_program": None}
        fingerprint = {}
        try:
            before = path.lstat()
            fingerprint["stat"] = [before.st_ino, before.st_uid, before.st_mode, before.st_size,
                                   before.st_mtime_ns, before.st_ctime_ns]
            if not stat.S_ISREG(before.st_mode) or path.resolve().parent != root.resolve():
                raise WorkflowError("a regular installed plist is required")
            raw = path.read_bytes()
            after = path.lstat()
            if fingerprint["stat"] != [after.st_ino, after.st_uid, after.st_mode, after.st_size,
                                       after.st_mtime_ns, after.st_ctime_ns]:
                raise WorkflowError("installed plist changed during inspection")
            fingerprint["sha256"] = hashlib.sha256(raw).hexdigest()
            config = plistlib.loads(raw)
            if not isinstance(config, dict) or config.get("Label") != label:
                raise WorkflowError("installed plist label does not match its filename")
            argv = config.get("ProgramArguments")
            if argv is not None and (not isinstance(argv, list) or not argv or
                    any(not isinstance(arg, str) or any(char in arg for char in ("\x00", "\n", "\r")) for arg in argv)):
                raise WorkflowError("installed program arguments are unsupported")
            program = config.get("Program") or (argv[0] if argv else None)
            if not isinstance(program, str) or not Path(program).is_absolute() or any(char in program for char in ("\x00", "\n", "\r")):
                raise WorkflowError("an absolute installed program is required")
            installed.update(_argv=argv, _program=program, _disabled=config.get("Disabled", False))
            if type(installed["_disabled"]) is not bool:
                raise WorkflowError("installed disabled setting is unsupported")
            keepalive = config.get("KeepAlive")
            # `{SuccessfulExit: false}` alone reruns a job only after a failed
            # exit: a run-once job, not a service (sd:5880). Every other
            # KeepAlive key keeps the job up while its condition holds.
            after_failure = keepalive == {"SuccessfulExit": False} and keepalive["SuccessfulExit"] is False
            long_running = keepalive is True or (isinstance(keepalive, dict) and bool(keepalive) and not after_failure)
            long_running = long_running or any(isinstance(config.get(key), dict) and bool(config[key]) for key in ("Sockets", "MachServices"))
            scheduled = any(config.get(key) for key in ("StartInterval", "StartCalendarInterval", "WatchPaths", "QueueDirectories"))
            installed["category"] = "service" if long_running else "scheduled" if scheduled else "startup"
            executable = Path(program)
            executable_stat = executable.stat()
            fingerprint["program"] = [str(executable.resolve()), executable_stat.st_ino, executable_stat.st_size,
                                      executable_stat.st_mtime_ns, executable_stat.st_mode]
            if not stat.S_ISREG(executable_stat.st_mode) or not os.access(executable, os.X_OK):
                raise WorkflowError("installed program is not an executable regular file")
            if before.st_mode & 0o022 or (scope == "user" and before.st_uid != self.uid):
                raise WorkflowError("user controls require an owned plist without group or world write permission")
            installed["_reason"] = ""
        except WorkflowError as error:
            installed["_reason"] = str(error)
        except (OSError, ValueError, UnicodeError, plistlib.InvalidFileException, ExpatError):
            pass
        installed["_config"] = _digest({"fingerprint": fingerprint, "installed": installed})
        return installed

    def _disabled(self, installed):
        if installed["scope"] != "user":
            return None
        domain = installed["domain"]
        if self._overrides is None:
            overrides = self._read_overrides(domain)
        else:
            with self._overrides_lock:  # the inventory's threads wait on the first ask, never send a second
                if domain not in self._overrides:
                    self._overrides[domain] = self._read_overrides(domain)
                overrides = self._overrides[domain]
        return None if overrides is None else overrides.get(installed["label"], installed["_disabled"])

    def _read_overrides(self, domain):
        """`launchctl print-disabled`'s label -> disabled map for `domain`, or None when it did not answer whole."""
        try:
            result = self.runner(["/bin/launchctl", "print-disabled", domain])
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if result.returncode or not lines or lines[0] != "disabled services = {" or lines[-1] != "}":
                return None
            overrides = {}
            for line in lines[1:-1]:
                match = re.fullmatch(r'"([A-Za-z0-9][A-Za-z0-9._-]{0,199})" => (enabled|disabled)', line)
                if not match or match[1] in overrides:
                    return None
                overrides[match[1]] = match[2] == "disabled"
            return overrides
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return None

    @staticmethod
    def _parse(output, installed):
        lines = output.splitlines()
        if not lines or lines[0] != installed["_service"] + " = {" or lines[-1] != "}":
            return {}
        fields, arguments = {}, None
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
        if fields.get("state") not in {"running", "not running", "waiting"}:
            return {}
        try:
            runs = int(fields["runs"])
            pid = int(fields["pid"]) if "pid" in fields else None
            # launchd prints one line for the last run's outcome: `last exit code = N`
            # after an exit, `(never exited)` before any, and after a signal
            # `last terminating signal = Terminated: 15` with no exit-code line at
            # all. int(None) on that third shape read a SIGTERM'd, restarted runner
            # as unknown (sd:1331); a recorded signal is an outcome, not a gap.
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
        expected_args = installed["_argv"]
        args_match = arguments == expected_args if expected_args is not None else arguments in (None, [installed["_program"]])
        identity = (fields.get("path") == installed["_plist"] and
                    fields.get("program") == installed["_program"] and args_match)
        # A running pid wins over any history. Stopped, a non-zero exit is the
        # process's own verdict and a failure signal is the kernel's; both are
        # `failed`. A stop-path signal is somebody stopping the process: `idle`.
        killed_for_cause = not running and last_signal in _FAILURE_SIGNALS
        return {"state": "running" if running else "failed" if last_exit or killed_for_cause else "idle",
                "pid": pid, "last_exit": last_exit, "last_signal": last_signal, "_runs": runs, "_identity": identity,
                "reason": ("loaded service identity differs from its installed plist" if not identity else
                           f"last run ended in {_signal_name(last_signal)}" if killed_for_cause else "")}

    def inspect(self, identifier):
        installed = self._installed(identifier)
        observed = dict(installed, state="unknown", pid=None, last_exit=None, last_signal=None, _runs=None, _identity=None,
                        reason="launchctl state is unavailable or unrecognized")
        try:
            result = self.runner(["/bin/launchctl", "print", installed["_service"]])
            if result.returncode == 113:
                observed.update(state="unloaded", _identity=True, reason="service is installed but not loaded")
            elif result.returncode == 0:
                observed.update(self._parse(result.stdout, installed))
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass
        observed["_disabled"] = self._disabled(installed)
        # Observation involves subprocesses. Do not bind their answer to a plist
        # that changed while they were running, especially before bootstrap.
        latest = self._installed(identifier)
        if latest["_config"] != installed["_config"]:
            observed.update(latest, _identity=False, _disabled=None,
                            reason="installed configuration changed during runtime inspection")
        observed["_observation"] = _digest(observed)
        return observed

    def perform(self, verb, observed):
        if verb == "bootstrap":
            argv = ["/bin/launchctl", "bootstrap", observed["domain"], observed["_plist"]]
        elif verb in {"bootout", "kickstart"}:
            argv = ["/bin/launchctl", verb, observed["_service"]]
        else:
            raise WorkflowError("unsupported service action")
        return self.runner(argv)


def _last_request(connection, identifier):
    row = connection.execute("SELECT body FROM state WHERE kind='checkpoint' AND key=? ORDER BY id DESC LIMIT 1",
                             ("operations:service:" + identifier,)).fetchone()
    if row is None:
        return None
    try:
        body = json.loads(row["body"])
        return body if isinstance(body, dict) else None
    except (ValueError, TypeError):
        return None


def _public_request(request):
    return None if request is None else {key: request.get(key) for key in
                                        ("id", "action", "status", "at", "message", "phase", "who")}


def _capabilities(observed, request, restoring):
    reason = observed["_reason"]
    if observed["label"] in {LABEL_PREFIX + ".sd-dashboard", LABEL_PREFIX + ".project-dashboard"} or "tailscale" in observed["label"].lower():
        reason = "protected to preserve dashboard and tailnet access"
    elif observed["scope"] != "user":
        reason = "system LaunchDaemons are read-only; administrative changes are not supported"
    elif observed["label"].startswith(PREFIX):
        reason = "scheduled cron jobs are controlled in Jobs"
    elif observed["category"] != "service":
        reason = "only declared long-running user services support these controls"
    elif not observed["_identity"] or observed["state"] == "unknown":
        reason = observed["reason"]
    if request and request.get("status") in {"requested", "accepted", "unknown"} and request.get("before") == observed["_observation"]:
        reason = reason or "previous request has no observed state change yet; refresh to observe its outcome"
    result = {}
    for action in _ACTIONS:
        why = reason
        if not why and action == "start" and observed["state"] == "running":
            why = "service is already running"
        if not why and action in {"stop", "restart"} and observed["state"] == "unloaded":
            why = "service is already unloaded; use Start to load it"
        if not why and action != "stop":
            if restoring:
                why = "an unresolved restore prevents starting services"
            elif observed["_disabled"] is not False:
                why = "service is disabled" if observed["_disabled"] else "service enablement could not be verified"
        result[action] = {"allowed": not why, "reason": why}
    return result


def _service_state(connection, identifier, backend, *, observed=None):
    observed = backend.inspect(identifier) if observed is None else observed
    request = _last_request(connection, observed["id"])
    restoring = _restoring(connection)
    public = {key: observed[key] for key in _PUBLIC}
    public.update(capabilities=_capabilities(observed, request, restoring), last_request=_public_request(request),
                  control_note=_STOP_NOTE, revision=_digest({"observed": observed["_observation"], "request": request, "restore": restoring}))
    return public, observed


def service_state(connection: sqlite3.Connection, identifier: str, *, backend=None) -> dict:
    return _service_state(connection, identifier, backend or ServiceBackend())[0]


def inventory(connection: sqlite3.Connection, *, backend=None) -> dict:
    backend = backend or ServiceBackend()
    rows = []
    names = backend.names()
    # A test's backend may have no `one_read`; it inspects as before.
    reader = backend.one_read() if hasattr(backend, "one_read") else backend
    for identifier, observed in zip(names, _inspect_all(reader, names), strict=True):
        if observed is not None:  # A plist can disappear between enumeration and inspection.
            rows.append(_service_state(connection, identifier, backend, observed=observed)[0])
    return {"services": rows, "revision": _digest(rows)}


def _save_request(connection, identifier, request):
    row = record_state(connection, "checkpoint", key="operations:service:" + identifier, body=request)
    resolve_state(connection, row)


def _dispatch(backend, label, action, original, request):
    """Bounded command sequence; each dependent command needs fresh evidence."""
    def command(verb, observed):
        request["phase"] = verb
        result = backend.perform(verb, observed)
        if result.returncode:
            request.update(status="failed", message=f"launchctl refused {verb} (exit {result.returncode}); inspect current state")
            return False
        return True

    def settled(states, message):
        deadline = time.monotonic() + _SETTLE_SECONDS
        while True:
            latest = backend.inspect(label)
            if latest["_config"] != original["_config"] or latest["_reason"] or latest["_identity"] is False:
                request.update(status="failed", message="service configuration or loaded identity changed; no further action was sent")
                return None
            if latest["state"] in states and latest["_identity"]:
                return latest
            if time.monotonic() >= deadline:
                request.update(status="unknown", message=message)
                return None
            # launchd may report a transient spawn state after accepting bootstrap.
            # Wait only for observation, never repeat a mutating command.
            time.sleep(0.1)

    observed = original
    if action in {"stop", "restart"}:
        if not command("bootout", observed):
            return
        if action == "stop":
            request.update(status="accepted", message="launchctl accepted Stop; current state is observed separately")
            return
        observed = settled({"unloaded"}, "Stop was accepted but unload is not observed; restart did not continue")
        if observed is None:
            return
    if observed["_disabled"] is not False:
        request.update(status="failed", message="service enablement changed; no start was sent")
        return
    if observed["state"] == "unloaded":
        if not command("bootstrap", observed):
            return
        observed = settled({"running", "idle", "failed"},
                           "registration was accepted but a loaded service is not observed; no kickstart was sent")
        if observed is None:
            return
    if observed["state"] != "running":
        if observed["_disabled"] is not False:
            request.update(status="failed", message="service enablement changed; no kickstart was sent")
            return
        if not command("kickstart", observed):
            return
    request.update(status="accepted", message="launchctl accepted the request; current state is observed separately")


def _act(connection, label, action, *, expected_revision, who, backend):
    _identifier(label, mutation=True)
    who = _text(who, "who")
    if connection.in_transaction:
        raise WorkflowError("service actions require their own durable transaction")
    backend = backend or ServiceBackend()
    with control_gate(connection):
        observed = backend.inspect(label)
        with transaction(connection):
            current, observed = _service_state(connection, label, backend, observed=observed)
            _revision(current["revision"], expected_revision)
            capability = current["capabilities"][action]
            if not capability["allowed"]:
                raise WorkflowError(capability["reason"])
            request = {"id": uuid.uuid4().hex, "action": action, "status": "requested", "phase": "requested",
                       "at": now(), "who": who, "before": observed["_observation"],
                       "message": "request recorded; execution outcome is not yet known"}
            _save_request(connection, observed["id"], request)
        # Commit intent before launchctl. The gate serializes controls and
        # restore installation while unrelated SQL writers remain available.
        try:
            latest = backend.inspect(label)
            if latest["_observation"] != observed["_observation"]:
                request.update(status="failed", message="service changed before dispatch; no action was sent")
            elif action != "stop" and _restoring(connection):
                request.update(status="failed", message="restore prevents dispatch; no action was sent")
            else:
                _dispatch(backend, label, action, latest, request)
        except WorkflowError:
            request.update(status="failed", message="installed service is unavailable; no further action was sent")
        except subprocess.TimeoutExpired:
            request.update(status="unknown", message="launchctl timed out; the request outcome is unknown")
        except (OSError, subprocess.SubprocessError, UnicodeError):
            request.update(status="unknown", message="launchctl could not report an outcome")
        with transaction(connection):
            _save_request(connection, observed["id"], request)
    try:
        after = service_state(connection, label, backend=backend)
    except WorkflowError:
        after = dict(current, state="unknown", pid=None, revision=_digest(request), last_request=_public_request(request),
                     capabilities={key: {"allowed": False, "reason": "installed service is unavailable"} for key in _ACTIONS})
    return {"request": _public_request(request), "service": after}


def start_service(connection: sqlite3.Connection, label: str, *, expected_revision: str, who, backend=None) -> dict:
    return _act(connection, label, "start", expected_revision=expected_revision, who=who, backend=backend)


def stop_service(connection: sqlite3.Connection, label: str, *, expected_revision: str, who, backend=None) -> dict:
    return _act(connection, label, "stop", expected_revision=expected_revision, who=who, backend=backend)


def restart_service(connection: sqlite3.Connection, label: str, *, expected_revision: str, who, backend=None) -> dict:
    return _act(connection, label, "restart", expected_revision=expected_revision, who=who, backend=backend)
