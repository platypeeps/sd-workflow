"""Management > Sessions: each Claude session on this Mac, what it works on, and whether it may exit (sd:5106).

No ledger records a session; each fact is read from what Claude Code, git and `ps` already keep:

  agents      `claude agents --json`: each live session's pid, cwd, start, id, name and `status` (`busy`, `idle` or
              `waiting`, with `waitingFor` when a permission prompt or a question holds it). `waiting` is "waits on
              the operator".
  transcripts `<claude home>/projects/*/<session id>.jsonl` and its `subagents/agent-*.jsonl` with `.meta.json`. The
              newest write of either is the age of the last action. A subagent is idle only on completion evidence:
              its last turn record is a finished answer. A tool's name or a message's shape is not that evidence
              (sd:5106 round 2). One whose last record is not is running, or stalled once
              untouched for `STALL` seconds; one whose log or last record was not read is unknown. Stalled and
              unknown still count as active (sd:5106 review). A session whose first prompt is Fix's builder prompt
              (`fix_session.BUILD`) is a builder itself.
  processes   one `ps`: an `sd-ship ... lane watch` that descends from the session's pid is its lane watch.
  git         the session's cwd and each live worktree (`fleet.collect("sessions")`) whose branch names an item its
              builders work on: uncommitted files and commits no remote-tracking ref holds. A builder whose item
              names no live worktree is unknown until that item is done.

The current task is the item the cwd's branch names, the item Fix's prompt names, and each item a builder's
description names (`sd:N`); each carries its `via`. Safe to exit is `yes` only when the session is idle, nothing
runs under it, and no checkout above holds unpushed work; a fact that could not be read makes it `unknown` with
the reason, never `yes`. Sessions on other machines are not listed: `claude agents` lists this Mac's.

`builders` counts the builders running on this Mac, for Tasks' Start next (sd:5107): every builder subagent that is
running or unknown (not idle or stalled, pack sd:6400) and every Fix session that `claude agents` does not call idle. A
session whose transcript was not read holds one unknown slot until it is not busy and quiet for `STALL` (pack sd:8606).
A session list that could not be read makes it None with the reason.

`end` is Management's End session (sd:5126): it reads `claude agents --json` and `ps` again and sends SIGTERM only
when the posted session id still holds the posted pid and that process runs claude. A subagent runs inside its
session's process and no verb stops one alone, so a stalled agent ends with its session.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sd_db import reads
from sd_db.errors import SdDbError

from . import fix_session
from .active_sessions import BRANCH_ITEM, Unread, _item_id, _reason, elapsed

__all__ = ["builders", "document", "end", "list_agents", "list_processes", "read_work"]

#: Where claude-mem runs its observer sessions.
OBSERVER_CWD = "/.claude-mem/observer-sessions/"
#: One `claude agents --json`, `ps` or git call's bound; no answer within it is unknown, not empty.
CLAUDE_SECONDS = 10
GIT_SECONDS = 10
#: A running subagent writes its transcript at each step; a test run of 10 minutes writes nothing, so the bound is wider.
STALL = 30 * 60
#: How much of a transcript's end is read for its last turn record, and the most read when one line is longer.
TAIL_BYTES = 1 << 18
TAIL_MAX = 1 << 22
#: How much of a transcript's start is read for its first prompt.
HEAD_BYTES = 1 << 16
STATUSES = ("busy", "idle", "waiting")
SESSION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{7,63}", re.ASCII)
ITEM_TEXT = re.compile(r"\bsd:(\d{1,9})\b", re.ASCII)
#: An item number in an agent's name: `b5106-sessions`, `sys-4914-4912`, `sd3456`.
NAME_ITEM = re.compile(r"(?:^|[-_/])(?:sd|b)?(\d{1,9})(?=$|[-_/])", re.ASCII)
#: Fix's builder prompt, read back: `fix_session.BUILD` up to its id.
BUILD = re.compile(re.escape(fix_session.BUILD.split("{id}", 1)[0]) + r"(\d{1,9})\.", re.ASCII)
WATCH = re.compile(r"(?:^|[\s/])sd-ship(?:\s+-C\s+\S+)?\s+lane\s+watch\b")
#: What a source's read may raise: each marks that fact unknown, and the rest still answer.
UNREAD = (ValueError, OverflowError, TypeError, KeyError, IndexError, AttributeError, OSError, sqlite3.Error, SdDbError)


# -- the readers: each is a seam a test replaces ------------------------------------------------------------------------


def _claude() -> str:
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    if fallback.is_file():
        return str(fallback)
    raise Unread("claude is not on PATH or at ~/.local/bin/claude, so no session could be listed")


def list_agents() -> list[dict]:
    """`claude agents --json`: this Mac's live sessions, interactive and background."""
    try:
        done = subprocess.run([_claude(), "agents", "--json"], capture_output=True, text=True,
                              timeout=CLAUDE_SECONDS, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Unread(f"claude agents --json gave no answer in {CLAUDE_SECONDS} s") from None
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}").splitlines()[-1]
        raise Unread(f"claude agents --json failed: {said}")
    try:
        listed = json.loads(done.stdout)
    except ValueError:
        raise Unread("claude agents --json answered no JSON") from None
    return listed


def list_processes() -> list[tuple[int, int, str]]:
    """One `ps`: every process's pid, parent pid and command line."""
    try:
        done = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True,
                              timeout=CLAUDE_SECONDS, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Unread(f"ps gave no answer in {CLAUDE_SECONDS} s") from None
    if done.returncode != 0:
        raise Unread(f"ps failed: exit {done.returncode}")
    rows = []
    for line in done.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) == 3 else ""))
    return rows


def _git(path: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=GIT_SECONDS,
                              check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Unread(f"git {args[0]} in {path} gave no answer in {GIT_SECONDS} s") from None


def read_work(path: str) -> dict | None:
    """A checkout's branch, uncommitted files and unpushed commits; None when `path` is no git checkout."""
    top = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if top.returncode != 0:
        if "not a git repository" in top.stderr:
            return None
        raise Unread(f"git rev-parse in {path} failed: {top.stderr.strip() or top.returncode}")
    status = _git(path, "status", "--porcelain")
    ahead = _git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
    if status.returncode != 0 or ahead.returncode != 0 or not ahead.stdout.strip().isdigit():
        raise Unread(f"git status or rev-list in {path} failed")
    return {"branch": top.stdout.strip(), "dirty": len(status.stdout.splitlines()), "unpushed": int(ahead.stdout.strip())}


# -- transcripts ----------------------------------------------------------------------------------------------------------


def _transcript(root: Path, session: str) -> Path | None:
    if not SESSION_ID.fullmatch(session):
        return None
    return next(iter(sorted((root / "projects").glob(f"*/{session}.jsonl"))), None)


def _records_from_end(path: Path):
    """The transcript's records, last first, from at most `TAIL_MAX` bytes of its end."""
    size = path.stat().st_size
    span = TAIL_BYTES
    while True:
        with path.open("rb") as handle:
            handle.seek(max(size - span, 0))
            chunk = handle.read(span)
        lines = chunk.split(b"\n")
        if size > span:
            lines = lines[1:]  # the first line was cut
        records = []
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        if records or size <= span or span >= TAIL_MAX:
            return records
        span *= 4


def _turn(path: Path) -> str:
    """`idle` when the last turn record is a finished answer, `running` for any other, `unknown` when none was read."""
    for record in _records_from_end(path):
        kind = record.get("type")
        if kind == "user":
            return "running"
        if kind == "assistant":
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            asks = any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content)
            return "idle" if message.get("stop_reason") == "end_turn" and not asks else "running"
    return "unknown"


def _first_prompt(path: Path) -> str:
    """The first user record's text. None within `TAIL_MAX` bytes of a longer file is unread, not "no prompt"."""
    size, span = path.stat().st_size, HEAD_BYTES
    while True:
        with path.open("rb") as handle:
            head = handle.read(span)
        found = _prompt_in(head)
        if found is not None or size <= span:
            return found or ""
        if span >= TAIL_MAX:
            raise Unread(f"no first prompt in its first {TAIL_MAX >> 20} MiB")
        span *= 4


def _prompt_in(head: bytes) -> str | None:
    for line in head.split(b"\n"):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "user":
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(block.get("text", "") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str))
        return ""
    return None


def _subagents(transcript: Path, clock: float) -> list[dict]:
    folder = transcript.with_suffix("") / "subagents"
    if not folder.is_dir():
        return []
    found = []
    for meta_path in sorted(folder.glob("agent-*.meta.json")):
        log = meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".jsonl")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise Unread(f"{meta_path.name} is not an object")
        when = log.stat().st_mtime if log.is_file() else meta_path.stat().st_mtime
        # Only a finished answer is idle: no log yet, or no record read, is unknown, and silence is not completion.
        state = _turn(log) if log.is_file() else "unknown"
        if state == "running" and clock - when >= STALL:
            state = "stalled"
        kinds = " ".join(str(meta.get(key) or "") for key in ("agentType", "customAgentType", "spawnedAgentType"))
        numbers = ITEM_TEXT.findall(str(meta.get("description") or "")) + NAME_ITEM.findall(str(meta.get("name") or ""))
        found.append({"name": str(meta.get("name") or meta.get("agentType") or log.stem), "builder": "builder" in kinds,
                      "description": str(meta.get("description") or ""), "state": state, "last": when,
                      "items": sorted({number for number in map(_item_id, numbers) if number is not None})})
    return found


# -- the join ---------------------------------------------------------------------------------------------------------------


def _iso(seconds: float | None) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if seconds is not None else ""


def _clock(now: str) -> float:
    try:
        value = datetime.fromisoformat(now.replace("Z", "+00:00"))
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    except (AttributeError, ValueError, OverflowError):
        return datetime.now(timezone.utc).timestamp()


def _descendants(rows: list[tuple[int, int, str]], pid: int) -> list[tuple[int, str]]:
    children: dict[int, list[tuple[int, str]]] = {}
    for child, parent, command in rows:
        children.setdefault(parent, []).append((child, command))
    out, todo, seen = [], [pid], {pid}
    while todo:
        for child, command in children.get(todo.pop(), []):
            if child not in seen:
                seen.add(child)
                out.append((child, command))
                todo.append(child)
    return out


def _session(raw: dict, *, root: Path, clock: float, procs, procs_error: str, trees, trees_error: str, work) -> dict:
    session = str(raw.get("sessionId") or "")
    pid = raw.get("pid") if type(raw.get("pid")) is int else None
    cwd = raw.get("cwd") if isinstance(raw.get("cwd"), str) else ""
    status = raw.get("status") if raw.get("status") in STATUSES else "unknown"
    started = raw.get("startedAt") / 1000 if isinstance(raw.get("startedAt"), (int, float)) else None
    no, unknown, tasks = [], [], []

    def task(item, via):
        if item is not None and all(t["item"] != item or t["via"] != via for t in tasks):
            tasks.append({"item": item, "via": via})

    agents, last, builder, fix_item = [], None, False, None
    transcript = _transcript(root, session) if session else None
    if transcript is None:
        unknown.append("no transcript was found, so its last action and builders were not read")
    else:
        try:
            last = transcript.stat().st_mtime
            prompt = BUILD.search(_first_prompt(transcript))
            if prompt:
                builder, fix_item = True, _item_id(prompt[1])
                task(fix_item, "Fix builder prompt")
            agents = _subagents(transcript, clock)
        except UNREAD as failure:
            unknown.append(f"its transcript could not be read: {_reason(failure)}")
    for agent in agents:
        last = max(last or 0, agent["last"])
        label = f"{'builder' if agent['builder'] else 'agent'} {agent['name']}"
        if agent["state"] == "idle":
            continue
        if agent["state"] == "running":
            no.append(f"{label} is running")
        elif agent["state"] == "stalled":
            no.append(f"{label} stalled: no write for {elapsed(int(clock - agent['last']))} and no final answer")
        else:
            unknown.append(f"{label} was not read, so it is not known to be done")
        for item in agent["items"]:
            task(item, label)
    if status == "busy":
        no.append("it is working on a turn")
    elif status == "waiting":
        no.append("it waits on you" + (f": {raw['waitingFor']}" if isinstance(raw.get("waitingFor"), str) and raw["waitingFor"] else ""))
    elif status == "unknown":
        unknown.append("claude agents gave no status, so whether it works or waits on you is not known")

    watches = []
    if procs_error:
        unknown.append(f"the process table was not read: {procs_error}")
    elif pid is None:
        unknown.append("claude agents gave no pid, so its lane watches were not read")
    else:
        watches = [{"pid": child, "command": command} for child, command in _descendants(procs, pid) if WATCH.search(command)]
        no.extend(f"lane watch pid {watch['pid']} runs under it" for watch in watches)

    # The checkouts whose unpushed work the session answers for: its cwd, and the worktrees its builders' items name.
    paths = [cwd] if cwd else []
    if not cwd:
        unknown.append("claude agents gave no cwd, so its checkout was not read")
    # A Fix session is a builder too: Fix tells it to work in a worktree, so its own item counts (sd:5106 round 2).
    items = {item for agent in agents if agent["builder"] for item in agent["items"]} | ({fix_item} if fix_item else set())
    if items and trees_error:
        unknown.append(f"its builders' worktrees were not read: {trees_error}")
    tied = set()
    for tree in trees:
        found = BRANCH_ITEM.search(tree["branch"])
        if found and _item_id(found[1]) in items:
            tied.add(_item_id(found[1]))
            if tree["path"] not in paths:
                paths.append(tree["path"])
    # A builder's worktree is found by the item its branch names; one not found was not read. `document` releases an
    # item that is done (its work landed); a builder that names no item stays unknown.
    unmatched = sorted(items - tied) if not trees_error else []
    for agent in agents:
        if agent["builder"] and not agent["items"]:
            unknown.append(f"builder {agent['name']} names no item, so its worktree was not read")
    checkouts = []
    for path in paths:
        try:
            state = work(path)
        except UNREAD as failure:
            unknown.append(f"{path} was not read: {_reason(failure)}")
            checkouts.append({"path": path, "error": _reason(failure)})
            continue
        if state is None:
            continue
        checkouts.append({"path": path, "branch": state["branch"], "dirty": state["dirty"], "unpushed": state["unpushed"]})
        if path == cwd:
            found = BRANCH_ITEM.search(state["branch"])
            task(_item_id(found[1]) if found else None, f"branch {state['branch']}")
        held = [f"{count} {words}{'' if count == 1 else 's'}"
                for count, words in ((state["dirty"], "uncommitted file"), (state["unpushed"], "unpushed commit")) if count]
        if held:
            no.append(f"{path} holds {' and '.join(held)}")
    safe = "no" if no else "unknown" if unknown else "yes"
    age = int(clock - last) if last is not None else None
    # How long nothing was written: from the last write, or from the start when no write was read (pack sd:8606).
    quiet = age if age is not None else int(clock - started) if started is not None else None
    return {"id": session, "name": str(raw.get("name") or ""), "kind": str(raw.get("kind") or ""), "pid": pid, "cwd": cwd,
            "started": _iso(started), "status": status,
            "waiting": None if status == "unknown" else status == "waiting",
            "waiting_for": raw.get("waitingFor") if isinstance(raw.get("waitingFor"), str) else "",
            "last_action": _iso(last), "age": age, "age_text": elapsed(age) if age is not None else "", "quiet": quiet,
            "builder": builder, "tasks": tasks,
            "agents": [{key: agent[key] for key in ("name", "builder", "state", "items", "description")}
                       | {"last": _iso(agent["last"]), "age": int(clock - agent["last"])}
                       for agent in agents if agent["state"] != "idle"],
            "watches": watches, "checkouts": checkouts, "unmatched": unmatched,
            "safe": {"state": safe, "why": no + unknown}}


def builders(sessions: list[dict] | None, why: str) -> dict:
    """How many builders run on this Mac: running builder subagents and busy or waiting Fix sessions; None when not known."""
    if sessions is None:
        return {"running": None, "why": why or "no session was listed", "holders": []}
    # Unknown holds its slot: only a finished answer or an idle session frees it (sd:5106 round 1). A stalled agent
    # does not (operator, pack sd:6400): the load cap guards the machine, and finished builders that never wrote a
    # final answer filled the cap. `holders` names each slot, unknown first, with the session's id and cwd.
    holders = []
    for row in sessions:
        where = {"session": row["name"] or row["id"][:8], "repo": Path(row["cwd"]).name if row["cwd"] else row["name"],
                 "session_id": row["id"], "cwd": row["cwd"]}
        # A session whose transcript was not read holds one unknown slot, never the whole count: two stale ones on a
        # satellite stopped its dispatcher for days (pack sd:8606). One not busy and quiet for `STALL` holds none, as
        # a stalled agent holds none. claude-mem's observers keep no transcript and build nothing (pack sd:8266).
        if (OBSERVER_CWD not in (row["cwd"] or "")
                and any(reason.startswith(("no transcript", "its transcript")) for reason in row["safe"]["why"])):
            quiet = row.get("quiet")
            if row["status"] == "busy" or not isinstance(quiet, int) or quiet < STALL:
                holders.append(where | {"agent": "", "state": "unknown", "age": quiet})
            continue
        holders += [where | {"agent": agent["name"], "state": agent["state"], "age": agent["age"]}
                    for agent in row["agents"] if agent["builder"] and agent["state"] not in ("idle", "stalled")]
        if row["builder"] and row["status"] != "idle":
            holders.append(where | {"agent": "", "state": "running" if row["status"] == "busy" else row["status"], "age": row["age"]})
    order = {"stalled": 0, "unknown": 1, "waiting": 2, "running": 3}
    holders.sort(key=lambda h: (order.get(h["state"], 1), -(h["age"] or 0)))
    return {"running": len(holders), "why": "", "holders": holders}


def _items(connection, numbers: list[int]) -> dict[int, tuple[str, str]] | None:
    """Each number an item holds, as (title, status), through `reads.item_by_id`; None when the database was not read."""
    try:
        return {item: (row["title"], row["status"]) for item in numbers
                if (row := reads.item_by_id(connection, item)) is not None}
    except (sqlite3.Error, SdDbError, KeyError, IndexError):
        return None


def document(connection, *, now: str, fleet=None, agents=None, processes=None, work=None, root=None) -> dict:
    """`sessions`, one per live Claude session, the ones waiting on the operator first; `sources`; `builders`.

    Every source is a seam: `fleet` is `fleet.collect`, `agents`, `processes` and `work` are `list_agents`,
    `list_processes` and `read_work`, and `root` is the Claude home (`~/.claude`).
    """
    clock = _clock(now)
    sources = {"agents": "", "processes": "", "worktrees": ""}
    root = Path(root) if root is not None else Path.home() / ".claude"
    try:
        listed = (agents or list_agents)()
        if not isinstance(listed, list) or any(not isinstance(row, dict) for row in listed):
            raise Unread("claude agents --json answered no session list")
    except UNREAD as failure:
        sources["agents"] = _reason(failure)
        return {"read": now, "sessions": None, "sources": sources, "builders": builders(None, sources["agents"])}
    procs = []
    try:
        procs = (processes or list_processes)()
    except UNREAD as failure:
        sources["processes"] = _reason(failure)
    trees = []
    try:
        if fleet is None:
            from . import fleet as fleet_module
            fleet = fleet_module.collect
        rows = fleet("sessions").get("worktrees")
        if not isinstance(rows, list):
            raise Unread("fleet collector returned no worktrees")
        trees = [tree for tree in rows if isinstance(tree, dict) and tree.get("live") is True
                 and isinstance(tree.get("path"), str) and isinstance(tree.get("branch"), str)]
    except UNREAD as failure:
        sources["worktrees"] = _reason(failure)
    sessions = [_session(raw, root=root, clock=clock, procs=procs, procs_error=sources["processes"], trees=trees,
                         trees_error=sources["worktrees"], work=work or read_work) for raw in listed]
    numbers = sorted({task["item"] for row in sessions for task in row["tasks"]})
    # A builder whose item names no live worktree: done (its work landed) releases it; anything else is unread.
    pending = {row["id"]: row.pop("unmatched") for row in sessions}
    asked = sorted({item for items in pending.values() for item in items})
    found = _items(connection, sorted(set(numbers) | set(asked))) if connection is not None else None
    titles = {item: found[item] for item in numbers if item in found} if found is not None else None
    done = {item for item in asked if found and found.get(item, ("", ""))[1] == "done"}
    for row in sessions:
        open_items = [item for item in pending[row["id"]] if item not in done]
        if open_items:
            row["safe"]["why"].append("no live worktree names " + ", ".join(f"#{item}" for item in open_items)
                                      + ", and the item is not done, so its builder's work was not read")
            if row["safe"]["state"] == "yes":
                row["safe"]["state"] = "unknown"
    for row in sessions:
        # A number no item holds is a name that only looked like one; with the items unread, each stays untitled.
        row["tasks"] = [task | {"title": "", "status": ""} if titles is None
                        else task | {"title": titles[task["item"]][0], "status": titles[task["item"]][1]}
                        for task in row["tasks"] if titles is None or task["item"] in titles]
    rank = {"waiting": 0, "unknown": 1, "busy": 2, "idle": 3}
    sessions.sort(key=lambda row: (rank[row["status"]], row["age"] if row["age"] is not None else 1 << 62, row["id"]))
    # A `--no-session-persistence` session (sd-review's reviewers) keeps no transcript and builds nothing (pack sd:8296).
    transient = {pid for pid, _, command in procs if "--no-session-persistence" in command.split()}
    return {"read": now, "sessions": sessions, "sources": sources,
            "builders": builders([row for row in sessions if row["pid"] not in transient], "")}


def _runs_claude(command: str) -> bool:
    """True when the command's program, or the script `node` runs, is claude."""
    words = command.split()[:2]
    return any(Path(word).name == "claude" or "claude-code" in word for word in words)


def end(payload: dict, *, agents=None, processes=None, kill=os.kill):
    """`POST /api/claude-sessions/end`: the page's checks; the write as `action_route` takes it (sd:5126).

    The page sends the session's id and the pid it showed. Each is evidence for the other: the write reads
    `claude agents --json` again and ends nothing unless that id still holds that pid, and `ps` says the pid runs
    claude. A read that fails refuses; it never counts as a match.
    """
    values = dict(payload)
    if (set(values) != {"id", "pid"} or not isinstance(values["id"], str) or not SESSION_ID.fullmatch(values["id"])
            or type(values["pid"]) is not int or values["pid"] <= 1):
        raise ValueError("Name the session's id and the pid the page showed.")
    session, pid = values["id"], values["pid"]

    def write(_connection):
        listed = (agents or list_agents)()
        if not isinstance(listed, list):
            raise Unread("claude agents --json answered no session list")
        row = next((raw for raw in listed if isinstance(raw, dict) and raw.get("sessionId") == session), None)
        if row is None:
            raise ValueError(f"Session {session} is no longer listed by claude agents, so nothing was ended.")
        if row.get("pid") != pid:
            raise ValueError(f"Session {session} now runs as pid {row.get('pid')}, not {pid}; reload the page.")
        command = next((command for child, _, command in (processes or list_processes)() if child == pid), None)
        if command is None:
            raise ValueError(f"Pid {pid} is not in the process table, so nothing was ended.")
        if not _runs_claude(command):
            raise ValueError(f"Pid {pid} runs {command.split()[0]}, not claude, so nothing was ended.")
        try:
            kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return {"id": session, "pid": pid, "name": str(row.get("name") or ""), "ended": "already gone"}
        return {"id": session, "pid": pid, "name": str(row.get("name") or ""), "ended": "SIGTERM sent"}

    return write
