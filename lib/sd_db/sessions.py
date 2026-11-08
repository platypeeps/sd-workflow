"""The session registry: which Claude session holds which worktree, on which machine (pack sd:6024).

Each session keeps one `heartbeat` row in `state`, key `session:<id>` (grill
Q44: the registry lives in the database, in the heartbeat rows that already
exist). Its body names the machine, the session's cwd and each linked
worktree the session touched: path, repository, branch, item and when.
`bin/sd-session-beat`, a hook, writes it; SessionEnd deletes it.

A row older than `STALE` holds nothing, and the nightly prune deletes it: a
session that crashed without its SessionEnd hook frees its worktrees then.
A path is one machine's, so a hold counts only on the machine that wrote it.
A main checkout is never held; only a linked worktree, whose `.git` is a
file, is.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import paths
from .database import transaction
from .writes import now as clock

#: The key prefix of a session's heartbeat row.
PREFIX = "session:"
#: A beat older than this holds nothing; the prune deletes it.
STALE = timedelta(hours=24)
#: A session beats at most this often unless it touches a new worktree.
BEAT_SECONDS = 60
#: The worktrees one row keeps, newest first.
MAX_WORKTREES = 24
#: Directories walked up from one path before giving up.
MAX_DEPTH = 64
#: An item number in a branch name: `session-registry-6024`, `build/sd-6024`, `b5106-sessions`.
BUILD_BRANCH = re.compile(r"build/sd-(\d{1,9})", re.ASCII)
BRANCH_ITEM = re.compile(r"(?:^|[-_/])(?:sd-?|b)?(\d{1,9})(?=$|[-_/])", re.ASCII)
#: An absolute or home path in a shell command, for one the shell lexer cannot read (an unclosed quote).
COMMAND_PATH = re.compile(r"""(?:^|(?<=[\s'"=:(]))(~?/[^\s'";|&()<>`$]+)""")
#: The paths one command adds, and the words and characters of it read: a hook pays this on each tool call.
MAX_COMMAND_PATHS, MAX_COMMAND_WORDS, MAX_COMMAND_CHARS = 32, 512, 16384
#: A word that is a script, not a path: after `-c`, `-lc`, `-e` or `eval`, holding a shell operator, quote or
#: bracket, or with a space and not starting as a path. It is read as a command again, at most this deep.
SCRIPT_FLAG, SCRIPT_MARK, MAX_SCRIPT_DEPTH = re.compile(r"-[a-z]*[ce]|eval"), re.compile(r"[;&|<>`$'\"()\n]"), 3


def _key(path: str) -> str:
    return str(paths.key(os.path.realpath(os.path.expanduser(path))))


def worktree_of(path: str) -> dict | None:
    """The linked worktree `path` lies in: `{path, repo, branch}`, or None for a main checkout or no checkout.

    Read from `.git` files alone, with no `git` call: a hook pays this on each tool call.
    """
    here = Path(os.path.realpath(os.path.expanduser(path)))
    for folder in [here, *here.parents][:MAX_DEPTH]:
        marker = folder / ".git"
        if marker.is_dir():
            return None
        if not marker.is_file():
            continue
        try:
            text = marker.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not text.startswith("gitdir:"):
            return None
        gitdir = Path(text.removeprefix("gitdir:").strip())
        gitdir = gitdir if gitdir.is_absolute() else folder / gitdir
        if gitdir.parent.name != "worktrees" or gitdir.parent.parent.name != ".git":
            return None
        try:
            head = (gitdir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            head = ""
        branch = head.removeprefix("ref: refs/heads/") if head.startswith("ref: refs/heads/") else ""
        return {"path": _key(str(folder)), "repo": _key(str(gitdir.parent.parent.parent)), "branch": branch}
    return None


def item_of(branch: str, connection=None) -> int | None:
    """The item a branch names; build/sd-<n> names its lead assignment n, so its item is that row's (pack sd:8125)."""
    build = BUILD_BRANCH.fullmatch(branch or "")
    if build:
        row = connection.execute("SELECT item FROM assignment WHERE id = ?",
                                 (int(build[1]),)).fetchone() if connection is not None else None
        return row[0] if row else None
    found = BRANCH_ITEM.findall(branch or "")
    return int(found[-1]) if found else None


def touched(payload: dict) -> list[str]:
    """The paths a hook payload names: its cwd, a tool's file path, and each path in a Bash command."""
    found = [payload.get("cwd")]
    given = payload.get("tool_input")
    tool: dict = given if isinstance(given, dict) else {}
    found += [tool.get("file_path"), tool.get("notebook_path"), tool.get("path")]
    command = tool.get("command")
    if isinstance(command, str):
        found += command_paths(command, payload.get("cwd"))
    return [entry for entry in found if isinstance(entry, str) and entry.startswith(("/", "~/"))]


def command_paths(command: str, cwd: object, depth: int = 0) -> list[str]:
    """The paths in a shell command, read as the shell splits its words (pack sd:6152).

    A quoted path keeps its spaces. A relative one (`../pack`, `lib/x`) resolves against `cwd`, or against
    the folder a `cd` before it named; without an absolute `cwd` it is dropped. `NAME=a:b` names `a` and `b`,
    and `--flag=a` names `a`. A quoted script (`sh -c '...'`, `python -c`, `eval`, a message) is read again
    for its own paths; a `cd` in it moves no later word.
    """
    lexer = shlex.shlex(command[:MAX_COMMAND_CHARS], posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(itertools.islice(lexer, MAX_COMMAND_WORDS))
    except ValueError:  # an unclosed quote: the absolute paths alone, as a plain scan finds them
        return COMMAND_PATH.findall(command)[:MAX_COMMAND_PATHS]
    base = cwd if isinstance(cwd, str) and cwd.startswith("/") else None
    found: list[str] = []
    for before, word in itertools.pairwise(["", *words]):
        if SCRIPT_FLAG.fullmatch(before) or SCRIPT_MARK.search(word) or (
                any(char.isspace() for char in word) and not word.startswith(("/", "~/"))):
            found += (command_paths(word, base, depth + 1) if depth < MAX_SCRIPT_DEPTH
                      else COMMAND_PATH.findall(word))
            continue
        for part in [] if "://" in word else re.split(r"[=:]", word)[word.startswith("-"):]:
            if part.startswith(("/", "~/")):
                path = part
            elif base and (part in (".", "..") or "/" in part):
                path = os.path.normpath(os.path.join(base, part))
            else:
                continue
            found.append(path)
            base = path if before == "cd" and path.startswith("/") else base
    return found[:MAX_COMMAND_PATHS]


def worktrees(candidates: list[str], connection=None) -> list[dict]:
    """The distinct linked worktrees the candidate paths lie in, each with its item (a build branch's needs `connection`)."""
    found: dict[str, dict] = {}
    for candidate in candidates:
        tree = worktree_of(candidate)
        if tree and tree["path"] not in found:
            found[tree["path"]] = {**tree, "item": item_of(tree["branch"], connection)}
    return list(found.values())


def _body(row) -> dict:
    try:
        body = json.loads(row["body"]) if row else {}
    except (TypeError, ValueError):
        body = {}
    return body if isinstance(body, dict) else {}


def _row(connection, session: str):
    return connection.execute("SELECT id, timestamp, body FROM state WHERE kind = 'heartbeat' AND key = ? "
                              "ORDER BY timestamp DESC, id DESC LIMIT 1", (PREFIX + session,)).fetchone()


def _age(stamp: str, now: str) -> float:
    try:
        return (datetime.fromisoformat(now) - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def beat(connection, session: str, *, machine: str, cwd: str | None, found: list[dict],
         now: str | None = None) -> bool:
    """Record that `session` is alive and holds `found`; False when the row was fresh and nothing new was held.

    One row per session: an update, else an insert, in one transaction. The
    skip is read outside it, so a throttled beat takes no write lock; the
    merge is read again inside it, so a parallel beat's worktree is kept.
    """
    if not session or not machine:
        return False
    stamp = now or clock()
    row = _row(connection, session)
    if (row is not None and _age(row["timestamp"], stamp) < BEAT_SECONDS
            and {entry["path"] for entry in found} <= set(_held(row))):
        return False
    with transaction(connection):
        held = _held(_row(connection, session))
        for entry in found:
            held[entry["path"]] = {**entry, "at": stamp}
        kept = sorted(held.values(), key=lambda entry: str(entry.get("at", "")), reverse=True)[:MAX_WORKTREES]
        newest = kept[0] if kept else {}
        text = json.dumps({"session": session, "machine": machine, "cwd": _key(cwd) if cwd else None,
                           "repo": newest.get("repo"), "item": newest.get("item"), "worktrees": kept})
        updated = connection.execute("UPDATE state SET timestamp = ?, body = ? WHERE kind = 'heartbeat' AND key = ?",
                                     (stamp, text, PREFIX + session)).rowcount
        if not updated:
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('heartbeat', ?, ?, ?)",
                               (PREFIX + session, stamp, text))
    return True


def _held(row) -> dict[str, dict]:
    return {entry["path"]: entry for entry in _body(row).get("worktrees") or []
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)}


def end(connection, session: str) -> int:
    """Delete `session`'s row: its worktrees are free. The count deleted."""
    with transaction(connection):
        return connection.execute("DELETE FROM state WHERE kind = 'heartbeat' AND key = ?",
                                  (PREFIX + session,)).rowcount


def _cutoff(now: str | None) -> str:
    moment = datetime.fromisoformat(now) if now else datetime.now(UTC)
    return (moment - STALE).astimezone(UTC).isoformat(timespec="seconds")


def live(connection, *, now: str | None = None) -> list[dict]:
    """Every session whose beat is younger than `STALE`, newest first, each body with its `beat` time."""
    rows = connection.execute(
        "SELECT key, timestamp, body FROM state WHERE kind = 'heartbeat' AND substr(key, 1, ?) = ? AND timestamp >= ? "
        "ORDER BY timestamp DESC, id DESC", (len(PREFIX), PREFIX, _cutoff(now))).fetchall()
    found, seen = [], set()
    for row in rows:
        if row["key"] in seen:
            continue
        seen.add(row["key"])
        found.append({**_body(row), "session": row["key"][len(PREFIX):], "beat": row["timestamp"]})
    return found


def holders(connection, path: str, *, machine: str, exclude: tuple[str, ...] = (), now: str | None = None) -> list[dict]:
    """The live sessions on `machine` that hold the worktree at `path`, less those named in `exclude`."""
    key = _key(path)
    return [session for session in live(connection, now=now)
            if session.get("machine") == machine and session["session"] not in exclude
            and any(isinstance(entry, dict) and entry.get("path") == key for entry in session.get("worktrees") or [])]


def describe(sessions: list[dict]) -> str:
    """'' for none; else why a worktree stays, naming each holder."""
    if not sessions:
        return ""
    named = ", ".join(f"{entry['session'][:8]} (beat {entry['beat']})" for entry in sessions)
    return f"a live session holds it: {named}"


def expire(connection, *, now: str | None = None) -> int:
    """Delete every session row older than `STALE`: the prune's half of the registry. The count deleted."""
    with transaction(connection):
        return connection.execute(
            "DELETE FROM state WHERE kind = 'heartbeat' AND substr(key, 1, ?) = ? AND timestamp < ?",
            (len(PREFIX), PREFIX, _cutoff(now))).rowcount
