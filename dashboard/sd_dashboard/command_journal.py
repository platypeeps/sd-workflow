"""The execution journal's dashboard writer (sd:5541): one `exec` note for each command a page runs.

Activity's Commands lamp reads the execution journal, `runner_exec.execution_journal`. Its two writers retired: the v1
palette (`runner_exec.prepare`) and the old runner (`runner.release`). The pages' buttons that start a process or call
out (Run now, Publish now, Build deck, Import to Slides, Run sweep, ...) go through one place, the server's POST, and
`COMMANDS` names them. A route that only writes the database (hide, acknowledge, a note, a setting) is no command; nor
is a chat question, which runs nothing, or a Home Assistant call, which `home_screen` already journals itself.

The record is the palette's shape, so Activity and `/api/executions/<note>` read it with no second reader: `version` 1,
`note` its own id, `command` the button's label and target, `scope` `dashboard`, and `output_path` a
`<database folder>/executions/<hex>.log` that `runner_exec.open_output` creates. In order:

1. The server takes the time, then runs the action.
2. `record`, once it answered: the log, then the note, `started` at step 1. A refusal (a 4xx, or a `ValueError` or
   library refusal raised) ran nothing and writes nothing, as the pages' "a 4xx wrote nothing" says. A command that
   answered in the POST is ended at once: exit 0 for a 2xx or 3xx, 1 for a 5xx or any other raise, `UNKNOWN` for a
   504 or an answer that says its outcome is not known. A 202 start names its run in the note (`run`) and stays open;
   `watch` looks at that run every `POLL_SECONDS`, in a thread, until `settle` reads its end. A run that has no item of its
   own (a deck, a job) is kept on one holder item, `HOLDER`, made once as a done task, since `note.item` is NOT NULL.
   A command on an item keeps its record there too, naming the item in its label: a note on the item would change
   the revision its answer handed the page, and the item's notes are its own evidence.
3. `settle`: the run's own reader (`SETTLE`). A read that fails is no end: the record stays open.
4. `resume`, when the server starts: each open record is watched again, so a restart loses no run's end.

A server stopped inside a command that answers in the POST leaves no record of it: the record is written once it
answered. A journal that cannot be written (a satellite, a full disk) does not stop the command: the server log says
why.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sd_db.errors import SdDbError

__all__ = ["COMMANDS", "DETAIL_CHARS", "HOLDER", "POLL_SECONDS", "REFUSALS", "SCOPE", "SETTLE", "SETTLE_SECONDS", "UNKNOWN",
           "command", "record", "resume", "said", "settle", "watch"]

SCOPE = "dashboard"
#: The holder item's identity, `(source, external_id)`: the schema's unique index keeps it to one row.
HOLDER = ("sd-dashboard", "command-journal")
HOLDER_TITLE = "Dashboard commands with no item of their own (the execution journal keeps them here)"
#: How often `watch` reads a detached run, and how long before it gives up and ends the record `UNKNOWN`.
POLL_SECONDS = 5.0
SETTLE_SECONDS = 4 * 3600
#: The exit code of a record whose outcome is not known: not a process's code, so it reads as no run's.
UNKNOWN = -1
#: What an action raises when it refused before it ran anything: the server answers them 400, 404 or 409.
REFUSALS = (ValueError, SdDbError)
#: The most of an answer, or of a run's own log, a record copies.
COPY_BYTES = 256 * 1024


@dataclass(frozen=True)
class Command:
    """One route that runs something. `what(match, payload)` is the label's target; `run` the `SETTLE` key of a run
    that a 202 starts and ends later; `runs(payload)` false for a request on the route that only writes the database."""

    pattern: str
    label: str
    what: Callable[[re.Match, dict], str] = lambda match, payload: ""
    run: str | None = None
    runs: Callable[[dict], bool] = lambda payload: True


def _key(*names: str) -> Callable[[re.Match, dict], str]:
    return lambda match, payload: " ".join(str(payload[name])[:120] for name in names if payload.get(name) not in (None, ""))


def _group(match: re.Match, payload: dict) -> str:
    return match[1]


def _item(match: re.Match, payload: dict) -> str:
    return f"sd:{match[1]}" + (f" {payload['kind']}" if isinstance(payload.get("kind"), str) else "")


def _items(match: re.Match, payload: dict) -> str:
    return ", ".join(f"sd:{item}" for item in payload.get("items") or [] if type(item) is int)


#: Every route whose POST starts a process or calls out, as its button names it.
COMMANDS = (
    Command(r"/api/jobs/([a-z0-9][a-z0-9_-]{0,99})/retry", "Run now", _group),
    Command(r"/api/jobs/([a-z0-9][a-z0-9_-]{0,99})/cancel", "Cancel job", _group),
    Command(r"/api/services/([A-Za-z0-9][A-Za-z0-9._-]{0,199})/start", "Start service", _group),
    Command(r"/api/services/([A-Za-z0-9][A-Za-z0-9._-]{0,199})/stop", "Stop service", _group),
    Command(r"/api/services/([A-Za-z0-9][A-Za-z0-9._-]{0,199})/restart", "Restart service", _group),
    Command(r"/api/briefs/publish-now", "Publish now", lambda match, payload: f"brief sd:{payload.get('item')}",
            run="drain"),
    Command(r"/api/documents/publish-now", "Publish now", _key("root", "file"), run="drain"),
    Command(r"/api/research/deck", "Build deck", _key("repo"), run="deck"),
    Command(r"/api/research/render", "Render", _key("repo"), run="deck"),
    Command(r"/api/research/review", "Review", _key("repo"), run="deck"),
    Command(r"/api/research/slides", "Import to Slides", _key("repo")),
    # Attach links a checkout to a page in the database; list, draft and apply start a run.
    Command(r"/api/research/experiments/act", "Experiments", _key("verb", "action", "project", "run"), run="experiment",
            runs=lambda payload: payload.get("verb") != "attach"),
    Command(r"/api/research/intake", "Add sources", _key("target")),
    Command(r"/api/adversarial", "Adversarial review", _key("kind", "target", "item"), run="review"),
    Command(r"/api/hoa/sweep", "Run sweep", lambda match, payload: "hoa", run="sweep"),
    Command(r"/api/hoa/download", "Download", _key("source"), run="download"),
    Command(r"/api/tasks/sweep", "Run sweep", _key("realm"), run="sweep"),
    Command(r"/api/tasks/start-next/run", "Start next", _items),
    Command(r"/api/shadow/sync", "Re-run collector", lambda match, payload: "shadow sync", run="shadow"),
    Command(r"/api/health/cleanup", "Clean up", _key("row")),
    Command(r"/api/repos/land", "Land", _key("path", "items")),
    Command(r"/api/today/github/merge", "Merge", _key("repo", "number")),
    Command(r"/api/today/github/close", "Close original", _key("repo", "number")),
    Command(r"/api/repos/standard", "Apply standard", _key("path")),
    Command(r"/api/claude-sessions/end", "End session", _key("id")),
    Command(r"/api/skills/intake", "Skill intake", _key("source", "scope")),
    Command(r"/api/skills/intake/tracking", "Skill tracking", _key("action", "url")),
    Command(r"/api/shared-upload/regenerate", "Regenerate upload link"),
    Command(r"/api/items/([1-9][0-9]*)/fix", "Fix session", _item),
    Command(r"/api/now/fix", "Fix session", _key("row")),
    Command(r"/api/actions/run", "Run", _key("action", "job", "path", "port", "row", "entity_id")),
    Command(r"/api/items/([1-9][0-9]*)/draft-email", "Draft email", _item),
    Command(r"/api/items/([1-9][0-9]*)/file-issue", "Draft issue", _item, run="issue"),
    Command(r"/api/issue-runs/([A-Za-z0-9-]{1,80})/confirm", "File issue", _group),
    # sd:7274: the lane verbs, the dispatcher's kickstart, GitHub and Drive.
    Command(r"/api/queue/move", "Lane", lambda match, payload: f"{str(payload.get('action'))[:20]} sd:{payload.get('item')}"),
    Command(r"/api/queue/builds/retry", "Retry build",
            lambda match, payload: f"assignment {payload.get('assignment')}" + (" with a finding" if payload.get("finding") else "")),
    Command(r"/api/tasks/build", "Build", _items),
    Command(r"/api/today/github/copilot-review", "Request Copilot", _key("repo", "number")),
    Command(r"/api/hoa/attach", "Attach", _key("asset", "name")),
    Command(r"/api/home/attach", "Attach", _key("asset", "name")),
)


#: The most of a record's end line `said` gives Activity.
DETAIL_CHARS = 300


def said(connection, note: int, code) -> str:
    """What a page command's log says of its end, for Activity's detail (sd:7274): a 202 run's end line, the first
    line `_end` appended, or a failed answer's line; "" for an answer that succeeded at once or a run still open.
    A log that does not read says so: that is not knowing, not an empty answer."""
    from sd_db import runner_exec

    try:
        found = runner_exec.read_execution(connection, note)
    except (OSError, SdDbError) as problem:
        return f"its log was not read: {problem}"[:DETAIL_CHARS]
    if found["output_expired"]:
        return f"its log was pruned on {found['output_expired']}"
    lines = found["output"].splitlines()
    if not lines:
        return "its log is empty or gone, so how it ended is not read"
    # `record` writes the label, the POST line, then what the answer said; a label is the payload's, so find the POST.
    start = next((at for at, line in enumerate(lines) if line.startswith("POST ")), None)
    answer = lines[start + 1] if start is not None and start + 1 < len(lines) else ""
    if answer.startswith("answered 202"):
        return lines[start + 2][:DETAIL_CHARS] if start + 2 < len(lines) else ""
    return answer[:DETAIL_CHARS] if code != 0 else ""


def command(path: str, payload: dict | None = None):
    """`(Command, match)` for a POST path, and payload, that runs something, else None."""
    for entry in COMMANDS:
        match = re.fullmatch(entry.pattern, path)
        if match and (payload is None or entry.runs(payload)):
            return entry, match
    return None


def _say(problem) -> None:
    print(f"sd-dashboard: command journal: {problem}", file=sys.stderr, flush=True)


def _holder(connection) -> int:
    from sd_db import writes

    found = writes.item_by_external(connection, *HOLDER)
    if found is not None:
        return int(found["id"])
    return writes.create_item(connection, kind="task", title=HOLDER_TITLE, status="done", source=HOLDER[0],
                              external_id=HOLDER[1], session=SCOPE)


def _append(path: str, text: str) -> None:
    """Add `text` to a record's log, kept under the reader's bound; the log is the one `record` created, never a link."""
    from sd_db.runner_exec import MAX_OUTPUT

    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    try:
        room = MAX_OUTPUT - os.fstat(fd).st_size
        data = text.encode(errors="replace")
        if room > 0:
            os.write(fd, data[:room])
            os.fsync(fd)
    finally:
        os.close(fd)


def record(database, connection, path: str, payload: dict, principal: str, started: str, result=None, *,
           failure: BaseException | None = None) -> int | None:
    """Steps 1 and 2: the record of a command that answered, its note id; None for a route or an answer that ran
    nothing, or a journal this machine cannot write (said on stderr)."""
    from sd_db import runner_exec, writes
    from sd_db.database import refuse_hub_only, transaction

    found = command(path, payload)
    if found is None or isinstance(failure, REFUSALS):
        return None
    status, body = (500, None) if failure is not None else result if isinstance(result, tuple) else (200, result)
    if 400 <= status < 500:
        return None
    entry, match = found
    try:
        said = (f"raised {type(failure).__name__}: {failure}" if failure is not None
                else f"answered {status}: {json.dumps(body, sort_keys=True, default=str)[:COPY_BYTES]}")
        refuse_hub_only(connection, "the runner's executions directory")
        target = entry.what(match, payload)
        label = f"{entry.label} · {target}" if target else entry.label
        database_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        value = {"version": 1, "command": label, "target": target, "scope": SCOPE, "route": path, "who": principal,
                 "output_path": str(database_path.parent / "executions" / (uuid.uuid4().hex + ".log")), "run": None}
        # An answer says its outcome is not known with a 504 (no answer in time) or `outcome` (land, skill intake).
        unknown = status == 504 or isinstance(body, dict) and body.get("outcome") == "unknown"
        # Run (sd:5886) answers 200 with its step's own exit: a step that ran and failed is no success.
        ran = body.get("exit") if path == "/api/actions/run" and isinstance(body, dict) and type(body.get("exit")) is int and status < 400 else None
        ended, code = writes.now(), UNKNOWN if unknown else ran if ran is not None else 0 if status < 400 else 1
        if failure is None and status == 202 and entry.run is not None:
            value["run"] = SETTLE[entry.run].handle(body if isinstance(body, dict) else {}, value)
            if value["run"] is None:
                said += "\nThe answer names no run to follow, so how it ended is not known."
                code = UNKNOWN
            else:
                value["run"] = {"kind": entry.run, **value["run"]}
                ended = code = None
        # The log first: a note that ends with no log would fail the next backup's evidence check.
        with runner_exec.open_output(value) as output:
            output.write(f"{label}\nPOST {path} by {principal} at {started}\n{said}\n".encode(errors="replace")[:COPY_BYTES])
        with transaction(connection):
            item = _holder(connection)
            note = writes.add_note(connection, item, "exec", "{}", session=principal, started=started, ended=ended,
                                   exit_code=code, output_path=value["output_path"])
            writes.set_note_body(connection, note, json.dumps({**value, "note": note, "item": item}, sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error) as problem:
        _say(f"{path} ran unrecorded: {problem}")
        return None
    if value["run"] is not None:
        threading.Thread(target=watch, args=(database, note), daemon=True, name=f"sd-journal {note}").start()
    return note


def _row(connection, note: int) -> dict:
    """The record's note through the journal's own reader, with none of its output (read from its end)."""
    from sd_db import runner_exec

    return runner_exec.read_execution(connection, note, offset=runner_exec.MAX_OUTPUT)


def _record(connection, note: int) -> dict:
    return json.loads(_row(connection, note)["body"])


def _end(connection, note: int, code: int, text: str) -> None:
    from sd_db import runner_exec

    value = _record(connection, note)
    _append(value["output_path"], text if text.endswith("\n") else text + "\n")
    runner_exec.complete(connection, note, exit_code=code, output_path=value["output_path"])


# -- detached runs ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Settle:
    """How one kind of detached run is read. `handle(answer, record)` keeps what names the run in the record (None: the
    answer names none); `look(handle)` is None while it runs, or `(exit code, text)` once it ended. A read that fails
    raises `OSError`: not an end."""

    handle: Callable[[dict, dict], dict | None]
    look: Callable[[dict], tuple[int, str] | None]


def _tail(path) -> str:
    try:
        with open(path, "rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - COPY_BYTES))
            return stream.read().decode(errors="replace")
    except FileNotFoundError:
        return ""


def _by_id(module: str, pending: tuple[str, ...], ok: tuple[str, ...]) -> Settle:
    """A run with a state file of its own, read by its id with the module's `status`: publish_drain's run shape."""

    def look(handle):
        from importlib import import_module

        reader = import_module(f"sd_dashboard.{module}")
        status, document = reader.status(handle["id"])
        if status == 404:
            return UNKNOWN, f"run {handle['id']}: its state file is gone, so how it ended is not known"
        if status != 200:
            raise OSError(document.get("error") or f"run {handle['id']} read {status}")
        if document["state"] in pending:
            return None
        log = _tail(reader.state_dir() / "runs" / f"{handle['id']}.log")
        reason = f": {document.get('reason')}" if document.get("reason") else ""
        return (0 if document["state"] in ok else 1), f"run {handle['id']} {document['state']}{reason}\n{log}"

    return Settle(lambda answer, record: {"id": answer["id"]} if isinstance(answer.get("id"), str) else None, look)


def _moment(text):
    """A record's time, to the second, or None for one that does not read."""
    from datetime import datetime, timezone

    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).replace(microsecond=0)


def _sweep(handle):
    from . import sweep

    realm = handle["realm"]
    out = sweep.status(Path(handle["repo"]), realm)
    if out["error"]:
        raise OSError(out["error"])
    last = out["last"] or {}
    if out["running"]:
        return None
    # The sweep's own record says when it started, after the spawn this record saw; an older one is an earlier run's.
    began, theirs = _moment(handle["started"]), _moment(last.get("started"))
    if began is None or theirs is None or theirs < began:
        return UNKNOWN, f"the {realm} sweep's last record is not the run started at {handle['started']}\n" + _tail(out["start_log"])
    reason = f": {last.get('reason')}" if last.get("reason") else ""
    code = 0 if last.get("state") == "done" else last["exit"] if type(last.get("exit")) is int and last["exit"] else 1
    return code, f"{realm} sweep {last.get('state') or 'with no record'}{reason}\n" + _tail(out["start_log"])


def _sweep_handle(answer, record):
    """The sweep's place and realm: HOA's, or the realm `/api/tasks/sweep` named, which `record` kept as the target."""
    if not isinstance(answer.get("started"), str) or not isinstance(answer.get("repo"), str):
        return None
    return {"started": answer["started"], "repo": answer["repo"], "realm": record.get("target") or "hoa"}


def _download(handle):
    """A portal download (sd:5626): its record's end once its lock is free; a record of another start is unknown."""
    from . import hoa_download

    out = hoa_download.status(handle["source"])
    if out["error"]:
        raise OSError(out["error"])
    if out["running"]:
        return None
    if _moment(out["started"]) != _moment(handle["started"]):
        return UNKNOWN, f"the {handle['source']} download's last record is not the run started at {handle['started']}\n" + _tail(out["log"])
    reason = f": {out['reason']}" if out["reason"] else ""
    code = {"done": 0, "unavailable": 3}.get(out["state"], 1)
    return code, f"{handle['source']} download {out['state']}{reason}\n" + _tail(out["log"])


def _experiment(handle):
    from . import research_experiments, sweep

    state = research_experiments.state_dir()
    run = research_experiments._run(state / "runs" / handle["run"], sweep._held(state) or "")
    if run["state"] in ("queued", "running"):
        return None
    reason = f": {run['reason']}" if run.get("reason") else ""
    return (0 if run["state"] == "done" else 1), f"run {handle['run']} {run['state']}{reason}\n" + _tail(run["log"])


def _shadow(handle):
    from .shadow_run import RUN

    state = RUN.status()
    if state["running"]:
        return None
    if state["started"] != handle["started"]:
        return UNKNOWN, f"the sync this record started at {handle['started']} is not the server's last one"
    said = json.dumps(state["trackers"], sort_keys=True, default=str)[:COPY_BYTES]
    failed = state["error"] or any(not tracker["ok"] and tracker.get("configured", True) for tracker in state["trackers"])
    reason = f": {state['error']}" if state["error"] else ""
    return (1 if failed else 0), f"shadow sync {'failed' if failed else 'done'}{reason}\n{said}"


#: `Command.run` -> how its run is read.
SETTLE = {
    "drain": _by_id("publish_drain", ("queued", "running"), ("done",)),
    "deck": _by_id("research_deck", ("queued", "running"), ("done",)),
    "review": _by_id("adversarial", ("queued", "running"), ("done",)),
    "issue": _by_id("issue_filing", ("queued", "drafting"), ("ready",)),
    "sweep": Settle(_sweep_handle, _sweep),
    "download": Settle(lambda answer, record: {"started": answer["started"], "source": record.get("target")}
                       if isinstance(answer.get("started"), str) and record.get("target") else None, _download),
    "experiment": Settle(lambda answer, record: {"run": answer["run"]} if isinstance(answer.get("run"), str) else None,
                         _experiment),
    "shadow": Settle(lambda answer, record: {"started": answer["started"]} if isinstance(answer.get("started"), str)
                     else None, _shadow),
}


def settle(connection, note: int) -> bool:
    """Step 3: one look at an open detached record; True once it has ended. A failed read leaves it open (False)."""
    row = _row(connection, note)
    if row["ended"]:
        return True
    run = json.loads(row["body"]).get("run") or {}
    try:
        seen = SETTLE[run["kind"]].look(run)
    except (OSError, ValueError, KeyError, TypeError) as problem:
        _say(f"note {note}: its run did not read, so it stays open: {problem}")
        return False
    if seen is None:
        return False
    _end(connection, note, *seen)
    return True


def watch(database, note: int, *, wait: Callable[[float], None] = time.sleep, seconds: float = SETTLE_SECONDS) -> None:
    """A thread's work: `settle` until the run ends, then stop; past `seconds` the record ends `UNKNOWN`."""
    import sd_db

    deadline = time.monotonic() + seconds
    while True:
        if database is not None and not Path(database).exists():
            return  # a database gone (a test's, a moved one) has no record left to end
        try:
            connection = sd_db.connect(database)
            try:
                if settle(connection, note):
                    return
                if time.monotonic() > deadline:
                    _end(connection, note, UNKNOWN, f"its run did not end within {seconds:.0f} s; how it ended is not known")
                    return
            finally:
                connection.close()
        except (OSError, ValueError, SdDbError, sqlite3.Error) as problem:
            _say(f"note {note}: {problem}")
        wait(POLL_SECONDS)


def resume(database) -> None:
    """Step 4, at server start: watch each open record again."""
    import sd_db
    from sd_db import reads, writes

    try:
        connection = sd_db.connect(database)
        try:
            holder = writes.item_by_external(connection, *HOLDER)
            for row in reads.item_notes(connection, holder["id"]) if holder is not None else ():
                if row["kind"] != "exec" or row["ended"] is not None:
                    continue
                try:
                    value = json.loads(row["body"])
                except ValueError:
                    continue
                if not isinstance(value, dict) or value.get("scope") != SCOPE or value.get("note") != row["id"]:
                    continue
                threading.Thread(target=watch, args=(database, row["id"]), daemon=True,
                                 name=f"sd-journal {row['id']}").start()
        finally:
            connection.close()
    except (OSError, ValueError, SdDbError, sqlite3.Error) as problem:
        _say(f"open records were not resumed: {problem}")
