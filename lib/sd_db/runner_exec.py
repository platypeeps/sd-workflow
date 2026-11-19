"""Finite command authorization and write-ahead execution records for the palette."""

import contextlib
import hashlib
import json
import os
import pwd
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import paths, registry, runner, workflow
from .database import refuse_hub_only, transaction
from .errors import SdDbError
from .writes import add_note, now, set_note_body
from .yaml_lite import load

MAX_CATALOG = 65536
MAX_OUTPUT = 2 * 1024 * 1024
#: How long the run waits for its group's watcher to end once the run is over; it ends at once unless the machine stalls.
WATCHER_REAP_SECONDS = 10
SCREENS = {"today", "backlog", "item", "writing", "skills", "operations"}
#: The one native operation left. Cancel, resume and restore went with the runner service they drove (sd:3041).
OPERATIONS = {"requeue": ("supervisor", ["sd", "runner", "requeue", "{assignment}"])}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _file(path, *, executable=False, limit=MAX_CATALOG):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise workflow.WorkflowError("command authority must be an absolute regular file")
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or details.st_uid not in {0, os.getuid()} or details.st_mode & 0o022 or details.st_size > limit:
        raise workflow.WorkflowError("command authority has unsafe ownership, permissions or size")
    if executable and not os.access(path, os.X_OK):
        raise workflow.WorkflowError("registered command is not executable")
    return path.read_bytes()


def catalog(*, path=None, home=None, screen=None):
    target = Path(path) if path else Path(home or Path.home()) / ".local/share/sd/commands.yaml"
    if not target.exists():
        raise workflow.WorkflowError(f"No command palette is configured at {target}.")
    try:
        raw = _file(target)
        data = load(raw.decode())
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise workflow.WorkflowError("command catalog could not be read safely") from error
    if not isinstance(data, dict) or set(data) - {"version", "commands", "system_item"} or type(data.get("version")) is not int or data.get("version") != 1 or not isinstance(data.get("commands"), dict):
        raise workflow.WorkflowError("command catalog needs version 1 and a commands mapping")
    if len(data["commands"]) > 100:
        raise workflow.WorkflowError("command catalog exceeds one hundred entries")
    if data.get("system_item") is not None and (type(data["system_item"]) is not int or data["system_item"] < 1):
        raise workflow.WorkflowError("standing system item must be a positive item ID")
    # One bad entry used to raise here and take every other button down
    # with it, naming only the first offender. Each entry is validated on
    # its own, so refusing it by name and keeping the rest live weakens
    # nothing: a rejected name is absent from `entries`, and `registered`
    # answers a request for it with the rule it broke.
    entries = {}
    rejected = []
    for name, entry in data["commands"].items():
        try:
            entries[name] = _entry(name, entry)
        except workflow.WorkflowError as error:
            rejected.append({"name": str(name), "reason": str(error)})
    return {"path": str(target.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
            "entries": {key: value for key, value in entries.items() if screen is None or screen in value["screens"]},
            "rejected": rejected, "system_item": data.get("system_item")}


def _entry(name, entry):
    """Validate one catalog entry; the refusal names the rule, not the entry."""
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", name) or not isinstance(entry, dict):
        raise workflow.WorkflowError("command entries need stable names and objects")
    if set(entry) - {"label", "argv", "screens", "mutates", "scope", "placeholders", "operation"} or not {"argv", "screens", "mutates", "scope"} <= set(entry):
        raise workflow.WorkflowError("argv, screens, mutates and scope are required")
    if type(entry["mutates"]) is not bool or not isinstance(entry["scope"], str) or entry["scope"] not in {"worktree", "supervisor"}:
        raise workflow.WorkflowError("invalid mutation or scope declaration")
    screens = entry["screens"]
    if not isinstance(screens, list) or not screens or any(not isinstance(value, str) or value not in SCREENS for value in screens):
        raise workflow.WorkflowError("invalid screen inventory; screens are " + ", ".join(sorted(SCREENS)))
    argv = entry["argv"]
    if not isinstance(argv, list) or not 1 <= len(argv) <= 40 or any(not isinstance(value, str) or not value or len(value) > 4096 or any(c in value for c in "\0\n\r") for value in argv):
        raise workflow.WorkflowError("command must be a bounded argument vector")
    placeholders = entry.get("placeholders", {})
    if not isinstance(placeholders, dict) or any(not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not isinstance(kind, str) or kind not in {"item", "provider", "assignment", "destination"} for key, kind in placeholders.items()):
        raise workflow.WorkflowError("only typed placeholders are supported")
    used = []
    for value in argv:
        if "{" in value or "}" in value:
            match = re.fullmatch(r"\{([a-z][a-z0-9_]*)\}", value)
            if not match:
                raise workflow.WorkflowError("placeholders occupy a complete argument")
            used.append(match[1])
    if set(used) != set(placeholders):
        raise workflow.WorkflowError("placeholder declarations do not match arguments: argv uses "
                                     + (", ".join(sorted(set(used))) or "none") + "; placeholders declare "
                                     + (", ".join(sorted(placeholders)) or "none"))
    selected = {**entry, "name": name, "label": entry.get("label", name), "placeholders": placeholders}
    if not isinstance(selected["label"], str) or len(selected["label"]) > 200:
        raise workflow.WorkflowError("invalid label")
    if entry["scope"] == "worktree":
        if entry.get("operation") or not Path(argv[0]).is_absolute() or "{" in argv[0]:
            raise workflow.WorkflowError("worktree command needs a fixed absolute executable as argv[0]")
        if Path(argv[0]).name in {"sh", "bash", "zsh", "dash", "fish", "csh", "tcsh", "env"} or any(value in {"-c", "--eval", "-e"} for value in argv[1:]):
            raise workflow.WorkflowError("shell or inline program execution is not a palette command")
        try:
            selected["executable_sha256"] = hashlib.sha256(_file(argv[0], executable=True, limit=100 * 1024 * 1024)).hexdigest()
        except OSError as error:
            raise workflow.WorkflowError("executable is unavailable") from error
    else:
        operation = entry.get("operation")
        if not isinstance(operation, str) or operation not in OPERATIONS or OPERATIONS[operation] != (entry["scope"], argv) or entry["mutates"] is not True or placeholders.get("assignment") != "assignment":
            raise workflow.WorkflowError("unsupported native supervisor operation")
    selected["entry_sha256"] = digest(selected)
    return selected


def registered(current, command, *, missing):
    """The entry `command` names in a read catalog, or the refusal: the rule it
    broke when the catalog rejected it, `missing` when it was never there."""
    entry = current["entries"].get(command)
    if entry is not None:
        return entry
    for rejection in current["rejected"]:
        if rejection["name"] == command:
            raise workflow.WorkflowError(f"{command} was rejected from the catalog: {rejection['reason']}")
    raise workflow.WorkflowError(missing)


def _shape(entry, values):
    """The typed values a registered entry takes, checked without the store.

    Its own function because it is the part of `_values` that reads neither
    the item nor anything else: the names the entry declares, and each
    value's type and form for its kind -- an int for an item or an
    assignment, a provider name, a bounded absolute path for a destination.
    `_standing` asks it ahead of a batch, and `_values` asks it again for the
    one request. One copy, so the two cannot drift. What a value resolves to
    -- this item, an enabled provider, this item's assignment, a path not
    yet taken -- is `_values`' alone.
    """
    if not isinstance(values, dict) or set(values) != set(entry["placeholders"]):
        raise workflow.WorkflowError("supply exactly the registered typed values; free text is not accepted")
    for name, kind in entry["placeholders"].items():
        value = values[name]
        if kind == "item":
            if type(value) is not int:
                raise workflow.WorkflowError("item placeholder must be this existing item")
        elif kind == "provider":
            if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", value):
                raise workflow.WorkflowError("provider placeholder must name a configured provider")
        elif kind == "assignment":
            if type(value) is not int:
                raise workflow.WorkflowError("assignment placeholder must belong to this item")
        else:
            if not isinstance(value, str) or any(c in value for c in "\0\n\r") or not Path(value).is_absolute() or len(value) > 4000:
                raise workflow.WorkflowError("destination must be a new absolute directory")


def _values(connection, entry, values, item):
    _shape(entry, values)
    checked = {}
    for name, kind in entry["placeholders"].items():
        value = values[name]
        if kind == "item":
            if value != item:
                raise workflow.WorkflowError("item placeholder must be this existing item")
            workflow.item_state(connection, value)
        elif kind == "provider":
            provider = registry.read(connection=connection).providers.get(value)
            if not provider or not provider.enabled:
                raise workflow.WorkflowError("provider placeholder does not resolve to an enabled provider")
        elif kind == "assignment":
            if runner.queue_state(connection, value)["item"] != item:
                raise workflow.WorkflowError("assignment placeholder must belong to this item")
        else:
            if Path(value).exists() or Path(value).is_symlink():
                raise workflow.WorkflowError("destination must be a new absolute directory")
        checked[name] = value
    return checked


def _descriptor(scope):
    if not isinstance(scope, str) or not scope.startswith("palette:") or len(scope) > MAX_CATALOG:
        raise workflow.WorkflowError("exec requires a finite registered palette request")
    try:
        value = json.loads(scope[8:])
    except ValueError as error:
        raise workflow.WorkflowError("invalid palette request") from error
    if not isinstance(value, dict) or value.get("version") != 1:
        raise workflow.WorkflowError("invalid palette request version")
    return value


def verify_descriptor(value):
    required = {"version", "item", "command", "registry_path", "registry_sha256", "entry_sha256", "values", "argv", "scope", "mutates", "operation", "output_path", "target", "repo", "note"}
    if not isinstance(value, dict) or set(value) != required or type(value.get("version")) is not int or value["version"] != 1 or type(value.get("item")) is not int or type(value.get("note")) is not int:
        raise workflow.WorkflowError("invalid finite execution descriptor")
    if any(not isinstance(value.get(key), str) for key in ("registry_path", "registry_sha256", "entry_sha256", "command", "output_path", "repo")) or not Path(value["registry_path"]).is_absolute():
        raise workflow.WorkflowError("invalid execution authority paths")
    current = catalog(path=value.get("registry_path"))
    entry = registered(current, value.get("command"), missing="registered command changed; review and create a new execution request")
    if current["sha256"] != value.get("registry_sha256") or entry["entry_sha256"] != value.get("entry_sha256"):
        raise workflow.WorkflowError("registered command changed; review and create a new execution request")
    if not isinstance(value.get("values"), dict) or set(value["values"]) != set(entry["placeholders"]):
        raise workflow.WorkflowError("execution typed values changed")
    for key, kind in entry["placeholders"].items():
        candidate = value["values"][key]
        if kind in {"item", "assignment"} and (type(candidate) is not int or candidate < 1):
            raise workflow.WorkflowError("execution requires positive typed IDs")
        if kind == "provider" and (not isinstance(candidate, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", candidate)):
            raise workflow.WorkflowError("execution provider is invalid")
        if kind == "destination" and (not isinstance(candidate, str) or not Path(candidate).is_absolute() or any(c in candidate for c in "\0\n\r")):
            raise workflow.WorkflowError("execution destination is invalid")
    argv = [str(value["values"][part[1:-1]]) if part.startswith("{") else part for part in entry["argv"]]
    if argv != value.get("argv") or entry["scope"] != value.get("scope") or type(value.get("mutates")) is not bool or entry["mutates"] != value.get("mutates") or entry.get("operation") != value.get("operation"):
        raise workflow.WorkflowError("execution differs from the registered command")
    return entry


def _validate(connection, items, scope, assignment=None):
    refuse_hub_only(connection, "the runner's executions directory")
    value = _descriptor(scope)
    if items != [value.get("item")] or value.get("scope") != "worktree" or value.get("mutates") is not True:
        raise workflow.WorkflowError("queued exec must be one mutating worktree command")
    entry = verify_descriptor(value)
    _values(connection, entry, value["values"], value["item"])
    current = workflow.item_state(connection, value["item"])["item"]
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    if not paths.same(current["repo"], value["repo"]) or Path(value["output_path"]).parent != database.parent / "executions":
        raise workflow.WorkflowError("execution repository or output directory differs from its database authority")
    if assignment is not None:
        held = runner.queue_state(connection, assignment)["run"]
        if held and not paths.same(held["repo"], value["repo"]):
            raise workflow.WorkflowError("execution repository differs from its owned attempt")
    row = connection.execute("SELECT * FROM note WHERE id=? AND item=? AND kind='exec'", (value.get("note"), value["item"])).fetchone()
    if not row or row["ended"] or json.loads(row["body"]) != value or row["output_path"] != value["output_path"]:
        raise workflow.WorkflowError("exec request has no matching unfinished authorization note")
    bound = connection.execute("SELECT id FROM assignment WHERE role='exec' AND scope=?", (scope,)).fetchall()
    if any(row["id"] != assignment for row in bound):
        raise workflow.WorkflowError("this execution authorization already belongs to another assignment")
    return value


def validate_enqueue(connection, items, scope):
    return _validate(connection, items, scope)


def resolve_assignment(connection, assignment):
    state = runner.queue_state(connection, assignment)
    if state["role"] != "exec":
        raise workflow.WorkflowError("only an exec assignment can resolve a palette command")
    value = _validate(connection, [state["item"]], state["scope"], assignment=assignment)
    return value


def _queues(entry):
    """Whether `prepare` puts a request for this entry in the runner's queue.

    One copy: `prepare` asks it to decide, and `_standing` asks it for a
    caller that must not be told "prepared" when nothing was queued.
    """
    return entry["scope"] == "worktree" and entry["mutates"] is True


def _standing(connection, command, values, *, expected_catalog, screen, path, home, require_queue=False):
    """Every refusal `prepare` makes alike for every row, in its order.

    One copy, called from `prepare` and from `standing_refusal`, so a caller
    that skips work on the answer cannot drift from what `prepare` will
    really do, and a refusal added here is asked by both.

    These are the whole of that set: a restore still to finish, a command
    palette that cannot be read, a palette that changed since it was read, a
    command the palette does not register on this screen, a command it
    rejected from the catalog, typed values that are not the ones its entry
    declares, and a value whose type or form is wrong for the kind its entry
    declares it -- an item placeholder declared a provider, say. For a caller
    that passes `require_queue`, also an entry `prepare` would not queue: a
    worktree command that does not mutate runs at once instead, so a batch
    that only ever queues would record an exec note for a row that never
    runs (sd:814).

    Membership is not "reads nothing", and it is not "reads no item row".
    Every one of them reads the store, because the pending restore is a
    query, and all but that first one read the filesystem: the palette file,
    and the executable a worktree entry declares. What none of them does is
    read the row, or resolve one of its values against the store or the
    filesystem, so a batch that offers every row the same kind of value gets
    the same answer for each of them. "Reads no item row" would not draw
    that line at all: the four refusals in `_values` above that
    `standing_refusal` deliberately leaves out read no item row either, and
    the ones that do are `_checked_state`'s stale or missing row and
    `runner.enqueue`'s (sd:820). The scope checks below in `prepare` read
    the item too, and a supervisor or control entry reaching a caller that
    offers a worktree entry's values is refused here by `_shape` first,
    because such an entry must declare an `assignment`.

    The order is the set's, not a detail. Every refusal here precedes
    `_checked_state` in `prepare`, so a value of the wrong type or form for
    its kind is refused on the form and never on the row, and the dashboard
    sends 400 for it where a well-typed value on the same request would have
    got 409 with `reload` for a stale row, or 404 for a row that is not
    there (`except workflow.StaleItem as problem`, in `do_POST` of
    `local-project-dashboard/sd_dashboard/server.py`). Nothing
    branches on the difference -- `dashboard.js` reads `error` alone, and the
    CLI exits 1 for any of them -- so it is recorded here rather than pinned
    by a test, which is what a reader of either code would need (sd:820).

    Returns the read catalog and the entry.
    """
    if runner.restoration_pending(connection):
        raise workflow.WorkflowError("restore recovery must finish before a new command request")
    current = catalog(path=path, home=home, screen=screen)
    if current["sha256"] != expected_catalog:
        raise workflow.StaleItem("command catalog changed; reopen the palette")
    entry = registered(current, command, missing="this command is not registered on the current screen")
    _shape(entry, values)
    if require_queue and not _queues(entry):
        raise workflow.WorkflowError(f"{command} is not a mutating worktree command, so it would not be queued")
    return current, entry


def standing_refusal(connection, command, values, *, expected_catalog, screen="item", path=None, home=None, require_queue=False):
    """The refusal `prepare` will make for every row alike, or None.

    A batch that sets a row up before it asks -- one that gives the row a
    branch writes a decision note and bumps the item's revision -- asks this
    once and skips the setup when it answers.
    Without it a refusal that stands for a hundred nights costs a hundred
    notes and a hundred revisions on a row that was never queued: first a
    pending restore (sd:786), then a `plan-item` entry the palette registers
    on some other screen (sd:805), or one whose `item` placeholder it
    declares as some other kind, or, asked with `require_queue`, one that
    does not mutate and so would never be queued (sd:814). Enumerated once here rather than special
    cased once per trigger, because a third trigger would outlive a second
    special case.

    Every refusal that reads the item, or resolves a value against the store
    or the filesystem, is deliberately absent: a stale or missing row, a
    value of the right form that does not resolve (another item, a provider
    not enabled, another item's assignment, a destination already there), a
    row with no registered repository, and all of `runner.enqueue` -- a NULL
    branch, an assignment already open, a retained lease. Those differ row by
    row, and a batch must set the next row up and try it. Only the first of
    them and `runner.enqueue` read the item row. Another item is a
    comparison; the provider, the assignment and the destination are a
    registry read, a queue read and an `exists()` in `_values`; and the
    repository is a `repo` query on the row `_checked_state` already read.
    That is why "reads no item row" is not the line between the two sets
    (sd:820).

    The answer is read, not held: a restore that arrives or clears between
    this call and a later `prepare` leaves it stale for the rows after it, in
    both directions. Ask it as late and as near the first `prepare` as the
    caller can.
    """
    try:
        _standing(connection, command, values, expected_catalog=expected_catalog,
                  screen=screen, path=path, home=home, require_queue=require_queue)
    except SdDbError as refusal:
        return refusal
    return None


def prepare(connection, item, command, values, *, expected_revision, expected_catalog, screen="item", target=None, path=None, home=None, who, require_queue=False):
    refuse_hub_only(connection, "the runner's executions directory")
    with transaction(connection):
        current, entry = _standing(connection, command, values, expected_catalog=expected_catalog,
                                   screen=screen, path=path, home=home, require_queue=require_queue)
        state = workflow._checked_state(connection, item, expected_revision)
        checked = _values(connection, entry, values, item)
        if not state["item"]["repo"] or not connection.execute("SELECT 1 FROM repo WHERE path=?", (state["item"]["repo"],)).fetchone():
            raise workflow.WorkflowError("the command needs an item with a registered repository")
        if entry["scope"] != "worktree":
            held = runner.queue_state(connection, checked["assignment"])
            if not isinstance(target, dict) or set(target) != {"assignment", "revision", "run"} or target["assignment"] != held["id"] or target["revision"] != held["revision"] or target["run"] != (held["run"]["id"] if held["run"] else None):
                raise workflow.StaleItem("the target attempt changed; reopen its controls")
            for note in connection.execute("SELECT body FROM note WHERE item=? AND kind='exec' AND ended IS NULL", (item,)):
                previous = json.loads(note["body"])
                if (previous.get("target") or {}).get("assignment") == target["assignment"]:
                    raise workflow.WorkflowError("an earlier control response is unfinished; reconcile it before another mutation")
        elif target is not None:
            raise workflow.WorkflowError("worktree commands do not accept a supervisor target")
        database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        output_path = str(database.parent / "executions" / (uuid.uuid4().hex + ".log"))
        value = {"version": 1, "item": item, "command": command, "registry_path": current["path"],
                 "registry_sha256": current["sha256"], "entry_sha256": entry["entry_sha256"], "values": checked,
                 "argv": [str(checked[part[1:-1]]) if part.startswith("{") else part for part in entry["argv"]],
                 "scope": entry["scope"], "mutates": entry["mutates"], "operation": entry.get("operation"),
                 "output_path": output_path, "target": target, "repo": state["item"]["repo"]}
        note = add_note(connection, item, "exec", "{}", session=who, started=now(), output_path=output_path)
        value["note"] = note
        set_note_body(connection, note, json.dumps(value, sort_keys=True))
        queued = None
        if _queues(entry):
            revision = workflow.item_state(connection, item)["revision"]
            queued = runner.enqueue(connection, [item], role="exec", scope="palette:" + json.dumps(value, sort_keys=True), expected_revisions={item: revision}, who=who)
        return {"execution": value, "assignments": queued or []}


def complete(connection, note, *, exit_code, output_path):
    if type(exit_code) is not int:
        raise workflow.WorkflowError("execution completion needs an actual integer exit code")
    with transaction(connection):
        row = connection.execute("SELECT * FROM note WHERE id=? AND kind='exec'", (note,)).fetchone()
        if not row or row["output_path"] != str(output_path):
            raise workflow.WorkflowError("execution output does not match its original record")
        if row["ended"]:
            if row["exit_code"] != exit_code:
                raise workflow.WorkflowError("execution already has a different completion")
            return
        connection.execute("UPDATE note SET ended=?,exit_code=? WHERE id=?", (now(), exit_code, note))


def read_execution(connection, note, *, offset=0):
    row = connection.execute("SELECT * FROM note WHERE id=? AND kind='exec'", (note,)).fetchone()
    if not row:
        raise workflow.WorkflowError("no such execution record")
    if type(offset) is not int or offset < 0:
        raise workflow.WorkflowError("output offset must be nonnegative")
    data = dict(row)
    path = Path(row["output_path"])
    refuse_hub_only(connection, "the runner's executions directory")
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    if path.parent != database.parent / "executions" or not re.fullmatch(r"[a-f0-9]{32}\.log", path.name) or path.parent.is_symlink():
        raise workflow.WorkflowError("execution output is outside its designated log directory")
    # The nightly prune (`retention`) removes the output file after ninety
    # days and marks the note; the record -- entry, arguments, exit code --
    # stays. A read then says so rather than reading nothing.
    data["output_expired"] = _expired(row)
    chunk = b""
    if path.exists() and not data["output_expired"]:
        if path.is_symlink() or not path.is_file():
            raise workflow.WorkflowError("execution output is not a regular file")
        with path.open("rb") as stream:
            stream.seek(min(offset, MAX_OUTPUT))
            chunk = stream.read(min(65536, MAX_OUTPUT - min(offset, MAX_OUTPUT)))
    data["output"] = chunk.decode(errors="replace")
    data["next_offset"] = offset + len(chunk)
    assignment = next((held for held in connection.execute("SELECT id,status,scope FROM assignment WHERE role='exec' AND item=?", (row["item"],))
                       if held["scope"].startswith("palette:") and _descriptor(held["scope"]).get("note") == note), None)
    data["state"] = "output expired" if data["output_expired"] else "finished" if row["ended"] else (assignment["status"] if assignment else "pending")
    return data


def _expired(row):
    """The `output_expired` stamp the prune wrote into the note's body, or None."""
    try:
        value = json.loads(row["body"])
    except (ValueError, TypeError):
        return None
    stamp = value.get("output_expired") if isinstance(value, dict) else None
    return stamp if isinstance(stamp, str) else None


def process_plan(value, *, home=None, cwd=None):
    """A fixed argv and minimal environment; never accept a caller command string."""
    entry = verify_descriptor(value)
    if entry["scope"] != "worktree":
        raise workflow.WorkflowError("native controls do not run as worktree processes")
    environment = {"HOME": str(Path(home or Path.home()).resolve()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                   "LANG": "en_US.UTF-8", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                   # A login-keychain generic password is read under the name in USER; without it the
                   # agent reports itself logged out and exits 0. Derived from the uid, not os.environ,
                   # so it names the user actually running and a caller cannot set it.
                   "USER": pwd.getpwuid(os.getuid()).pw_name}
    if value["mutates"]:
        marker = os.environ.get("SD_ASSIGNMENT", "")
        if not re.fullmatch(r"[a-f0-9]{32}", marker):
            raise workflow.WorkflowError("queued command needs the owned supervisor identity")
        environment["SD_ASSIGNMENT"] = marker
        if cwd is not None:
            root = Path(cwd)
            for directory in (root / ".git/sd-tmp", root / ".git/sd-cache"):
                directory.mkdir(mode=0o700, exist_ok=True)
                if directory.is_symlink() or not directory.is_dir():
                    raise workflow.WorkflowError("command scratch directory is unsafe")
            environment["TMPDIR"] = str(root / ".git/sd-tmp")
            environment["XDG_CACHE_HOME"] = str(root / ".git/sd-cache")
    return {"argv": value["argv"], "environment": environment, "output_path": value["output_path"], "note": value["note"]}


def open_output(value):
    path = Path(value["output_path"])
    if not path.is_absolute() or path.parent.name != "executions" or not re.fullmatch(r"[a-f0-9]{32}\.log", path.name):
        raise workflow.WorkflowError("execution output has an invalid location")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    details = path.parent.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise workflow.WorkflowError("execution output directory must be private and owned")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise workflow.WorkflowError("execution output already exists or is unsafe; reconcile instead of replaying") from error
    os.fsync(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return os.fdopen(fd, "wb", buffering=0)


#: The leader of an own-group command's group (sd:5808). It reads the caller's pipe until EOF, which comes
#: when the caller closes it after the run or dies by any signal, SIGKILL too, and then ends the group.
WATCHER = "import os, signal, sys\nos.read(int(sys.argv[1]), 1)\nos.killpg(0, signal.SIGKILL)\n"


def run_process(value, *, cwd, home=None, timeout=300, own_group=False):
    """Called only after authority/ownership checks; output is durable and bounded.

    With `own_group` the command joins a group that `WATCHER` leads, so the
    time limit ends its children too, and so does the caller's death. The
    dashboard calls this from a request thread, which can install no signal
    handler, and launchd's stop is a SIGTERM it does not trap (sd:5808).
    """
    plan = process_plan(value, home=home, cwd=cwd)
    with open_output(value) as output, contextlib.ExitStack() as stack:
        # Recheck after filesystem preparation, at the last point before dispatch.
        verify_descriptor(value)
        group = None
        if own_group:
            group = stack.enter_context(_watched_group(plan["environment"]))
        process = subprocess.Popen(plan["argv"], cwd=cwd, env=plan["environment"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, process_group=group)
        written = 0
        deadline = time.monotonic() + timeout
        with process.stdout, selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            # EOF can precede process exit; the deadline still owns that wait.
            while selector.get_map() or process.poll() is None:
                if time.monotonic() > deadline:
                    if group is not None:
                        # The command can exit between poll() and this signal. macOS answers
                        # EPERM for a group whose only member is an unreaped leader, and ESRCH
                        # once it is reaped; both mean nothing is left to kill (sd:2338). A
                        # live leader that changed credentials answers EPERM too, and waiting
                        # on it would outlast the deadline, so only an exited one passes.
                        try:
                            os.killpg(group, signal.SIGKILL)
                        except (PermissionError, ProcessLookupError) as error:
                            if process.poll() is None:
                                raise workflow.WorkflowError("command exceeded its time limit and refused the kill; "
                                                             "inspect its ownership before retrying") from error
                    else:
                        process.kill()
                    process.wait()
                    output.write(b"\nCommand time limit exceeded; inspect runner ownership.\n"[:max(0, MAX_OUTPUT - written)])
                    os.fsync(output.fileno())
                    raise workflow.WorkflowError("command exceeded its time limit; inspect its ownership before retrying")
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = max(0, MAX_OUTPUT - written)
                    output.write(chunk[:available])
                    written += min(len(chunk), available)
            exit_code = process.wait()
        os.fsync(output.fileno())
    receipt = {"version": 1, "note": value["note"], "execution_sha256": execution_digest(value),
               "run": plan["environment"].get("SD_ASSIGNMENT"), "log": Path(value["output_path"]).name,
               "output_sha256": hashlib.sha256(Path(value["output_path"]).read_bytes()).hexdigest(), "exit_code": exit_code}
    path = Path(value["output_path"]).with_suffix(".receipt.json")
    with path.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write((json.dumps(receipt, sort_keys=True) + "\n").encode())
        output.flush()
        os.fsync(output.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {"exit_code": exit_code, "output_path": plan["output_path"]}


@contextlib.contextmanager
def _watched_group(environment):
    """A `WATCHER` leading a new group in this session; yields its id, and ends and reaps it on the way out.

    A new group, not a session: a process joins a group only in its own session.
    """
    read, write = os.pipe()  # not inheritable: the command never holds the write end
    try:
        watcher = subprocess.Popen([sys.executable, "-I", "-c", WATCHER, str(read)], env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   pass_fds=(read,), process_group=0)
    except BaseException:
        os.close(write)
        raise
    finally:
        os.close(read)
    try:
        yield watcher.pid
    finally:
        os.close(write)  # EOF: the watcher ends what is left of the group, itself last
        with contextlib.suppress(subprocess.TimeoutExpired):
            watcher.wait(timeout=WATCHER_REAP_SECONDS)


def execution_digest(value):
    # Restore relocates authority paths; original paths remain in the snapshot manifest.
    return digest({key: part for key, part in value.items() if key not in {"registry_path", "output_path"}})


def process_result(value, *, expected_run=None):
    """Prove the exact completed process after a lost response without replaying it."""
    log = Path(value["output_path"])
    try:
        data = _file(log, limit=MAX_OUTPUT)
        receipt = json.loads(_file(log.with_suffix(".receipt.json")))
    except (OSError, ValueError, UnicodeError) as error:
        raise workflow.WorkflowError("execution has no complete durable process receipt; outcome remains uncertain") from error
    if (not isinstance(receipt, dict) or receipt.get("version") != 1 or receipt.get("note") != value["note"]
            or receipt.get("execution_sha256") != execution_digest(value) or receipt.get("run") != expected_run
            or receipt.get("log") != log.name or receipt.get("output_sha256") != hashlib.sha256(data).hexdigest()
            or type(receipt.get("exit_code")) is not int):
        raise workflow.WorkflowError("execution process receipt does not prove this exact command and attempt")
    return {"exit_code": receipt["exit_code"], "output_path": str(log)}


def _record(connection, note):
    row = connection.execute("SELECT * FROM note WHERE id=? AND kind='exec'", (note,)).fetchone()
    try:
        value = json.loads(row["body"]) if row else None
    except ValueError as error:
        raise workflow.WorkflowError("execution record is malformed") from error
    if not isinstance(value, dict) or value.get("note") != note or value.get("output_path") != row["output_path"]:
        raise workflow.WorkflowError("no matching palette execution record")
    # The read path checks the database-owned log location before any dispatch.
    read_execution(connection, note)
    return row, value


def assignment_result(connection, assignment, run):
    """Record a proven past result after the runner clears supervisor ownership."""
    state = runner.queue_state(connection, assignment)
    value = _descriptor(state["scope"])
    owned = connection.execute("SELECT * FROM runner_run WHERE id=? AND assignment=?", (run, assignment)).fetchone()
    _, recorded = _record(connection, value.get("note"))
    if (state["role"] != "exec" or state["item"] != value.get("item") or recorded != value or not owned
            or not paths.same(owned["repo"], value.get("repo"))):
        raise workflow.WorkflowError("process result does not belong to this execution assignment and run")
    result = process_result(value, expected_run=run)
    complete(connection, value["note"], **result)
    return result


def execute_immediate(connection, note, *, home=None):
    if runner.restoration_pending(connection):
        raise workflow.WorkflowError("restore recovery must finish before command dispatch")
    row, value = _record(connection, note)
    if row["ended"]:
        raise workflow.WorkflowError("this execution already ended; create a new request to run it again")
    entry = verify_descriptor(value)
    current_item = workflow.item_state(connection, value["item"])["item"]
    if not paths.same(current_item["repo"], value["repo"]):
        raise workflow.StaleItem("the item's repository changed; create a new request")
    if entry["scope"] == "worktree":
        if entry["mutates"]:
            raise workflow.WorkflowError("mutating worktree commands run only through their queued assignment")
        held = connection.execute("SELECT work_path FROM runner_run JOIN assignment ON assignment.id=runner_run.assignment WHERE assignment.item=? AND runner_run.released_at IS NULL ORDER BY runner_run.created_at DESC LIMIT 1", (value["item"],)).fetchone()
        # The key is a disk path only after `expand` (sd:1439).
        cwd = held["work_path"] if held else str(paths.expand(value["repo"]))
        result = run_process(value, cwd=cwd, home=home, timeout=30, own_group=True)
    else:
        target = value["target"]
        held = runner.queue_state(connection, target["assignment"])
        if held["item"] != value["item"] or held["revision"] != target["revision"] or (held["run"]["id"] if held["run"] else None) != target["run"]:
            raise workflow.StaleItem("the exact target attempt changed; reconcile or create a new request")
        # Refused before the dispatch marker: reconcile then closes the request unrun (sd:9269 review).
        if why := runner.requeue_refusal(held):
            raise workflow.WorkflowError(why)
        with open_output(value) as output:
            output.write((json.dumps({"event": "dispatch", "target": target, "operation": value["operation"]}) + "\n").encode())
            os.fsync(output.fileno())
            # The note and durable dispatch marker precede the native operation.
            response = runner.requeue(connection, target["assignment"], expected_revision=target["revision"], who=f"palette:{note}")
            output.write((json.dumps({"event": "response", "exit_code": 0, "response": response}, sort_keys=True) + "\n").encode())
            os.fsync(output.fileno())
        result = {"exit_code": 0, "output_path": value["output_path"]}
    complete(connection, note, **result)
    return read_execution(connection, note)


def reconcile(connection, note, *, home=None):
    """Observe the original target, never replay a command with an unknown outcome."""
    refuse_hub_only(connection, "runner controls")
    row, value = _record(connection, note)
    if row["ended"]:
        return read_execution(connection, note)
    path = Path(value["output_path"])
    if not path.exists():
        assignment = connection.execute("SELECT id FROM assignment WHERE role='exec' AND scope=?", ("palette:" + json.dumps(value, sort_keys=True),)).fetchone()
        if assignment:
            held = runner.queue_state(connection, assignment["id"])
            if held["status"] != "cancelled" or (held["run"] and not held["run"]["released_at"]):
                raise workflow.WorkflowError("queued work belongs to the runner; cancel its assignment before reconciling")
        # No dispatch marker exists: no process can have passed exclusive log creation.
        # Closing this unused authorization permits an explicit new request.
        with open_output(value) as output:
            output.write(b"Prepared request closed before dispatch. No command was replayed.\n")
            os.fsync(output.fileno())
        complete(connection, note, exit_code=125, output_path=path)
        return read_execution(connection, note)
    if value.get("scope") == "worktree":
        assignment = connection.execute("SELECT id FROM assignment WHERE role='exec' AND scope=?", ("palette:" + json.dumps(value, sort_keys=True),)).fetchone()
        held = runner.queue_state(connection, assignment["id"]) if assignment else None
        expected_run = held["run"]["id"] if held and held["run"] else None
        if value["mutates"] and expected_run is None:
            raise workflow.WorkflowError("queued execution has no matching owned attempt; outcome remains uncertain")
        result = process_result(value, expected_run=expected_run)
        complete(connection, note, **result)
        return read_execution(connection, note)
    lines = path.read_text()[:MAX_OUTPUT].splitlines()
    try:
        events = [json.loads(line) for line in lines]
    except ValueError as error:
        raise workflow.WorkflowError("control receipt is incomplete; the outcome remains uncertain") from error
    if any(isinstance(event, dict) and event.get("event") == "response" and event.get("exit_code") == 0 for event in events):
        proven = True
    else:
        target = value["target"]
        held = runner.queue_state(connection, target["assignment"])
        if held["item"] != value["item"]:
            raise workflow.WorkflowError("control target identity changed")
        marker = f"palette:{note}"
        proven = value["operation"] == "requeue" and marker in (held["result"] or "")
    if not proven:
        raise workflow.WorkflowError("the target has not proved this operation's outcome; it remains uncertain and will not be replayed")
    complete(connection, note, exit_code=0, output_path=path)
    return read_execution(connection, note)


def inventory(connection, *, screen, item=None, home=None):
    if screen not in SCREENS:
        raise workflow.WorkflowError("unknown palette screen")
    try:
        current = catalog(home=home, screen=screen)
    except workflow.WorkflowError as error:
        return {"configured": False, "message": str(error), "entries": [], "rejected": [], "items": []}
    selected = item or current["system_item"]
    items = [dict(row) for row in connection.execute("SELECT id,title,repo FROM item WHERE repo IS NOT NULL AND parked_at IS NULL ORDER BY id DESC LIMIT 200")]
    state = workflow.item_state(connection, selected) if selected else None
    assignments = [runner.queue_state(connection, row["id"]) for row in connection.execute("SELECT id FROM assignment WHERE item=? ORDER BY id DESC LIMIT 20", (selected,))] if selected else []
    try:
        providers = [name for name, provider in registry.read(connection=connection).providers.items() if provider.enabled]
    except (ValueError, OSError, SdDbError):
        providers = []
    # `configured` answers whether there is a palette to read; `rejected`
    # answers what it refused. A catalog with a bad entry is still a
    # configured palette — reporting it as unconfigured would empty the
    # dashboard and read as "no file", which is the wrong remedy.
    return {"configured": True, "sha256": current["sha256"], "entries": list(current["entries"].values()),
            "rejected": current["rejected"],
            "items": items, "item": selected, "revision": state["revision"] if state else None,
            "assignments": assignments, "providers": providers}


#: The two writers of an `exec` note (sd:2183). `prepare` writes a palette
#: descriptor, `version` 1 JSON whose `note` is its own id; retention and a
#: backup restore rewrite it in the same shape. `runner.release` writes the
#: outcome of an author, reviewer or merge run as plain text, session `runner`,
#: with the run's start and end. Anything else is counted and skipped.
RUNNER_SESSION = "runner"


def _runner_role(connection, record):
    """The assignment and role of the run `runner.release` wrote this note for.

    The note keeps no run id, but release stamps the run's `created_at` as the
    note's `started` and its own `released_at` as `ended`, on the item's
    assignment. None when no one run matches, so a pruned run still lists.
    """
    runs = connection.execute(
        "SELECT assignment.id,assignment.role FROM runner_run JOIN assignment ON assignment.id=runner_run.assignment"
        " WHERE assignment.item=? AND runner_run.created_at=? AND runner_run.released_at=?",
        (record["item"], record["started"], record["ended"])).fetchall()
    return tuple(runs[0]) if len(runs) == 1 else (None, None)


def _journal_entry(connection, record, body):
    """(entry, None) for a note a known writer wrote, else (None, reason)."""
    try:
        value = json.loads(body)
    except (ValueError, TypeError):
        value = None
    if isinstance(value, dict) and value.get("version") == 1 and value.get("note") == record["id"]:
        record["command"] = value.get("command")
        record["scope"] = value.get("scope")
        record["output_expired"] = value.get("output_expired") if isinstance(value.get("output_expired"), str) else None
        return record, None
    if record["session"] == RUNNER_SESSION and isinstance(body, str) and not isinstance(value, dict):
        if not record["ended"]:
            return None, "runner outcome without an end"
        assignment, role = _runner_role(connection, record)
        record.update(command=f"runner {role}" if role else "runner", scope="runner", output_expired=None,
                      source="runner", assignment=assignment, detail=body)
        return record, None
    if isinstance(value, dict):
        if value.get("version") != 1:
            return None, "unknown version"
        return None, "palette record for another note"
    return None, "unknown writer"


def execution_journal(connection, *, limit=100):
    """The latest `exec` notes from every writer, and the skipped ones counted by reason."""
    rows = connection.execute("SELECT note.id,note.item,note.timestamp,note.started,note.ended,note.exit_code,note.body,note.session,item.title FROM note LEFT JOIN item ON item.id=note.item WHERE note.kind='exec' ORDER BY note.id DESC LIMIT ?", (min(100, max(1, limit)),)).fetchall()
    result, skipped = [], {}
    for row in rows:
        record = dict(row)
        entry, reason = _journal_entry(connection, record, record.pop("body"))
        if entry is None:
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            result.append(entry)
    return {"executions": result, "skipped": skipped}


def executions(connection, *, limit=100):
    return execution_journal(connection, limit=limit)["executions"]
