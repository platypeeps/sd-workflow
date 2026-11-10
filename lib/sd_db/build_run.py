"""The dispatcher's tick and the supervisor of one headless builder run (pack sd:6094).

Design: system `docs/work/2026-10-24-build-dispatcher/design.md`, step 2.

`dispatch` is one tick of the `build-dispatch` job: it writes the job's
heartbeat, reaps lost runs (`build.reap`), moves each `enqueued` run whose
lane entry merged to `cleaning` or ends it `lane_failed`, claims bundles
under the caps (`build.claim`), and spawns one supervisor for each open run
that has none. A spawn leaves the job's process group (`start_new_session`),
so neither the job's timeout nor launchd's exit cleanup reaches it.

`Supervisor` walks one run's steps from its `step` column, each written
before it starts: `worktree`, `building`, `reviewing` and `fixing`,
`enqueuing`, then `enqueued`, where it exits and the lane holds the run; a
fresh supervisor does `cleaning` after the merge. A hub fault (`HUB_FAULTS`)
is no loss: the child keeps running, the beat tries again, and a build the
hub refuses for life is replaced in place by the pack on disk, the pid and
the child kept (pack sd:8182). Every child runs in its own
process group, recorded in `builder_pgid` and `builder_start` before the
child runs: the child waits on a pipe and execs only once that write landed,
so a supervisor lost between the spawn and the write leaves nothing running.
A write that changes no row stops the supervisor before its next child.

The builder is judged by its last stream line, never its exit code (probe
#28947). Nothing before `enqueuing` pushes: `sd-ship lane enqueue` publishes
the head, behind the enqueuing compare-and-set that a requested cancel fails.
An unmanaged repository's run (pack sd:7868) runs the repository's own checks
after its review, then pushes and opens a draft pull request at that step
instead; the tick's watch reads that pull request's observed state, not the lane.

The same tick reaps and claims draft runs (`sd_db.draft`, pack sd:6361), one
at a time, and `DraftSupervisor` walks one: a read-only `claude -p` in the
repository's checkout, its reply parsed and written by `draft.write`.

The hub's tick and each satellite's act only on their own lanes' runs
(`build.lane_hosts`, pack sd:7933): design
`docs/work/2026-11-06-build-per-host/design.md`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import build, contributions, draft, paths, remote, runner_journal, sessions
from .build import Probe
from .config import system_checkout
from .database import Patient, transaction
from .errors import SchemaTooNew
from .ship import this_host
from .workflow import WorkflowError
from .writes import add_note, now, record_cost, record_state

#: A child past this many seconds is ended: TERM to its group, `GRACE_SECONDS`, KILL.
TIMEOUT_SECONDS = build.BUDGET_MINUTES * 60
GRACE_SECONDS = 20
BEAT_SECONDS = 30
#: Reviews with blockers before the bundle blocks (lead, note #29482).
REVIEW_ROUNDS = 3
MAX_TURNS = 300
TITLE_MAX = 63
#: A read (`git status`, `lane list`) that takes longer is unread.
READ_SECONDS = 120
# Ignored folders that `git worktree remove` may delete: tools rebuild them (pack sd:6979).
DISPOSABLE = frozenset({"__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache"})
#: The pack checkout this module runs from: its `dashboard/dashboard.sh` is the health check.
PACK = Path(__file__).resolve().parents[2]
#: The `heartbeat` state row the tick writes first; a satellite's tick writes `<key>@<host>` (pack sd:7933).
HEARTBEAT = build.HEARTBEAT
#: The lane entry states that end an `enqueued` run without a merge: the lane's `BLOCKED` ends, which
#: `lane retry` queues again (`prepared` is a merge the lane refused), and a cancel.
LANE_ENDED = ("failed", "skipped", "prepared", "cancelled")

#: The builder's pre-approved calls: the brief's two commit forms (pack sd:6723). A prefix rule cannot pin
#: the -F file inside the worktree; the brief does.
ALLOWED = ("Bash(git commit -m:*)", "Bash(git commit -F:*)")

#: `sd_gate_slots.NOT_STARTED`: the gate's last stderr line when it started nothing; a check's own 125 lacks it.
GATE_NOT_STARTED = "sd gate run: the command did not start (exit 125)"

#: What a hub session can raise, or the hub's configuration refuse: gone, restarting, fenced, refusing this
#: process's build, serving a database newer than it, or a write whose answer was lost. None says the run moved,
#: so a supervisor keeps its child, then walks on (pack sd:8182). One that never clears stops at the child's
#: budget and `settle_seconds`. A write whose answer was lost is the hub's to say (`once`); the compare-and-sets
#: find the rest, as a retried run's do.
HUB_FAULTS = (remote.RemoteError, SchemaTooNew)
#: How a supervisor hands its state to the image it execs: the child it keeps, and what the next walk needs.
RESUME_ENV = "SD_BUILD_RESUME"
#: The once-only writes that belong to one child; every other mark lasts the run.
CHILD_MARKS = ("cost", "report")
#: On a replay, the children that ran and passed before a held one in the same step, by name: they are not run again.
PRECEDES = {"worktree": {"worktree"}, "checking-health": {"checking-sync"}, "checks": {"review", "checks"},
            "enqueue": {"lint"}, "push": {"lint"}, "pull": {"lint", "push"}}

#: The child waits for one byte on its pipe, written once its group is recorded, then execs the command.
GATE = ("import os, sys\n"
        "fd = int(sys.argv[1])\n"
        "go = os.read(fd, 1)\n"
        "os.close(fd)\n"
        "if go != b'1':\n"
        "    sys.exit(125)\n"
        "os.execvp(sys.argv[2], sys.argv[2:])\n")

BRIEF = """You are a headless builder run by the build dispatcher (pack sd:6094).

Build these items, one commit per item, in the worktree {worktree} on branch {branch}:
{items}
{findings}{denied}
Rules:
- Work only in {worktree}. Read each item with `sd task show N` and the repository's CLAUDE.md first.
- Write no file outside the worktree but the title and body below, not even in /tmp. Commit with
  `git commit -m` or `git commit -F <file inside the worktree>`; add new commits and never amend.
- The dispatcher owns every item's status: never run `sd task status`, never push, never open a pull request.
- Write a test that fails before each fix, then make it pass; skip no test.
- End the commit that delivers a whole item with a `Closes: sd:N` trailer: an item no trailer names
  stays open and is not delivered. Add no attribution lines.
- Keep every commit line, and every body line, at 72 characters or less outside a code fence:
  `sd-ship lane enqueue` refuses a longer one.
- Write the pull-request title, a Conventional Commit of {title_max} characters or less, alone to {title}.
- Write the pull-request body, with `## Summary` and `## Test plan`, to {body}; quote each fail-first line.
  The body is public, so it names no local path (the worktree, a home directory): use repo-relative paths.
  The body carries no `Closes:` line: enqueue takes items by `--item` and refuses one (sd:3704).
- Fence each failure table and any other line over 72 characters in the body in a code block.
- Use synthetic names in tests and commits (`operator`, `example.test`), never a real person, host or path:
  the pre-push leak guard refuses them.
- Before your report, check the commits since origin/main and the body for both: the supervisor runs
  the enqueue's line check and the leak guard after you, and sends each failure back to fix.
- Leave the worktree clean, with your commits on the branch.
- End only a process you started, by its pid: never `pkill` or `killall`, which end other sessions'
  gates too (sd:6815).
- An item that needs the operator: record it with `sd task note N $'<question>\\nOption: <a> (Recommended)\\nOption: <b>'
  --kind question` and skip it.

Your final message is your report: the commits, the tests run with their result lines, and anything not done.
"""

FIX = """The review of branch {branch} found these blockers. Fix each one fail-first as a new commit in
{worktree}, then update the body at {body}. Never push. Your final message is your report.

{blockers}
"""

DRAFT = """You are the headless lead, run by the build dispatcher (pack sd:6361). Draft the spec for one
captured item. You hold the read tools only: read the repository in the current directory, its CLAUDE.md
and AGENTS.md, and the code the item touches. You write nothing; the dispatcher writes your reply.

The item is sd:{item}, in {repo}:
Title: {title}
{body}

Use the existing context: the item, the repository and its rules. Ask only what would change the work and
the repository cannot answer. State the problem as an outsider would recognise it, and keep requirements
apart from assumptions. Each acceptance criterion names a check and its result ("`make test` passes with 0
failures"), never an intention. When the shape needs a design page before code, say so in the approach.

Your final message is the reply, in exactly this form and nothing else:

## Spec

Problem: <what is wrong today>
Approach: <the shape of the change, and the alternative you rejected with the reason>
Acceptance criteria:
- <a check and its result>
Risks: <what could make this wrong>

## Questions

<At most {questions}, only those whose answer changes the work. Each is its text, then two or more lines
that start "Option:", the recommended one first, ending " (Recommended)". Leave the section out when there
is none.>
"""

RESUME = """Your run was interrupted. Continue the brief in {worktree}: finish the items, commit, write the
title to {title} and the body to {body}, and leave the worktree clean. Your final message is your report.
{denied}"""


def exec_in_place(state: dict) -> None:
    """Replace this process with the pack now on disk, handing it `state` (pack sd:8182); return if it cannot.

    The hub refuses the build this process loaded for the rest of its life, and no retry inside it can pass
    (`follow`, sd:3359). `exec` keeps the pid and the process start `build.alive` reads, and the child stays this
    process's child. A new image that does not load would strand that child, so a child process proves the
    entrypoint loads first; one that fails leaves this image waiting for its next beat.
    """
    try:
        probe = subprocess.run([sys.executable, sys.argv[0], "assignments", "dispatch", "--help"], check=False,
                               capture_output=True, stdin=subprocess.DEVNULL, timeout=READ_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return
    if probe.returncode != 0:
        return
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execve(sys.executable, [sys.executable, *sys.argv], {**os.environ, RESUME_ENV: json.dumps(state)})
    except OSError:
        return


def resume_state(ident: str) -> dict | None:
    """What an earlier image of this process handed over for run `ident`; None for anything else (pack sd:8182)."""
    raw = os.environ.get(RESUME_ENV)
    try:
        state = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(state, dict) or state.get("run") != ident or state.get("pid") != os.getpid():
        return None
    return state


def start_again(ident: str, start: Callable[[], Any], *, bound: float = build.STALE_SECONDS) -> Any:
    """`start()`, tried until `bound` while this process holds a child an earlier image kept (pack sd:8182).

    `start` is every hub read from the connection to the supervisor; `run()`'s own loop takes over from there.
    Without a handed-over state it is one try. A mismatch this image cannot pass follows the build. The wait
    comes off the child's budget. Past `bound` the kept child is ended, so none outlives its supervisor, and
    the failure stands: the reap owns the run.
    """
    state = resume_state(ident)
    if state is None:
        return start()
    child = state.get("child") or {}
    begun, budget = time.monotonic(), child.get("left")
    while True:
        if budget is not None:
            child["left"] = max(budget - (time.monotonic() - begun), 0.0)
            os.environ[RESUME_ENV] = json.dumps(state)
        try:
            return start()
        except Exception as fault:
            if isinstance(fault, remote.BuildMismatch) and remote.disk_holds(fault):
                exec_in_place(state)
            if time.monotonic() - begun < bound:
                time.sleep(5.0)
                continue
            if child.get("pid") and child.get("exit") is None:
                build.kill_group(child["pid"])
            raise


class Adopted:
    """`subprocess.Popen`'s wait and poll for a child an earlier image of this process started."""

    def __init__(self, pid: int) -> None:
        self.pid: int = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None:
            try:
                reaped, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self.returncode = -1  # no longer a child of this process: a read's unknown, not a pass
            else:
                if reaped:
                    self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        stop = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if stop is not None and time.monotonic() >= stop:
                raise subprocess.TimeoutExpired(str(self.pid), timeout or 0.0)
            time.sleep(0.05)
        code = self.returncode
        assert code is not None
        return code


class Unread(Exception):
    """A read gave no answer: an unknown result, never a negative one."""


class Lost(Exception):
    """The run ended or moved under this supervisor; it writes nothing more."""


class Cancelled(Exception):
    """A cancel was asked; the active group is gone."""


class TimedOut(Exception):
    """A child ran past its timeout; its group is gone."""


class Survivor(Exception):
    """A process group still had a process after SIGKILL."""


@dataclass
class Tools:
    """The commands a supervisor runs, each an argv prefix: the seams a test fills with fakes."""
    claude: list[str]
    review: list[str]
    ship: list[str]
    lint: list[str]
    git: list[str] = field(default_factory=lambda: ["git"])
    #: `checking`'s two steps after an auto-pickup merge: `<sync> refresh <repo>`, then `<health> health`.
    sync: list[str] = field(default_factory=lambda: [str(system_checkout() / "local-repo-sync" / "repo-sync.sh")])
    health: list[str] = field(default_factory=lambda: [str(PACK / "dashboard" / "dashboard.sh")])
    #: An unmanaged repository's run (pack sd:7868): its own checks wait for a gate slot through `<gate>`,
    #: and its draft pull request opens through `<gh>`.
    gate: list[str] = field(default_factory=lambda: [sys.executable, str(PACK / "bin" / "sd_gate_slots.py"), "run"])
    gh: list[str] = field(default_factory=lambda: ["gh"])
    #: Before that push, `<guard>` reads the title, body and commits on stdin: `leak-guard.sh scan`, run whatever
    #: pre-push hook the repository has, since an unmanaged one normally has none (review of pack sd:7868).
    guard: list[str] = field(default_factory=lambda: ["sh", str(system_checkout() / "local-leak-guard" / "leak-guard.sh"),
                                                      "scan"])


def judge(lines: list[str], *, session: str | None = None) -> dict:
    """The builder's verdict from the stream-json lines one child wrote: the last line, never the exit code.

    `state` is `pass`, `denied` (with `tools`), `max_turns`, `failed` or
    `unknown`. A pass needs a `result` line of subtype `success`, `is_error`
    false and an empty `permission_denials` list, in the session the run
    recorded (`session`), else the one its `init` line named: a line of
    another session is no evidence for this run.
    """
    text = [line for line in lines if line.strip()]
    # Claude Code ends a run with a command_lifecycle line after the result.
    while text and '"type":"command_lifecycle"' in text[-1].replace(" ", ""):
        text.pop()
    init = None
    for line in text:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") == "system" and record.get("subtype") == "init":
            init = record.get("session_id")
            break
    if not text:
        return {"state": "unknown", "why": "the builder wrote no stream line"}
    try:
        final = json.loads(text[-1])
    except ValueError:
        return {"state": "unknown", "why": "the last stream line is not JSON"}
    if not isinstance(final, dict) or final.get("type") != "result":
        return {"state": "unknown", "why": "the last stream line is not a result"}
    want, named = session or init, final.get("session_id")
    if not named or (want and named != want):
        return {"state": "unknown", "why": f"the result names session {named or 'none'}, not {want or 'none'}",
                "final": final}
    result = {"final": final, "session_id": named}
    denials = final.get("permission_denials")
    if not isinstance(denials, list):
        return {**result, "state": "unknown", "why": "the result has no permission_denials list"}
    if denials:
        tools = sorted({str(row.get("tool_name") if isinstance(row, dict) else row) for row in denials})
        return {**result, "state": "denied", "tools": tools, "why": f"permission denied: {', '.join(tools)}",
                "calls": "; ".join(_denied_call(row) for row in denials)}
    if final.get("subtype") == "error_max_turns":
        return {**result, "state": "max_turns", "why": "the builder hit its turn limit"}
    if final.get("subtype") != "success" or final.get("is_error") is not False:
        return {**result, "state": "failed", "why": f"the result is {final.get('subtype')}, is_error "
                                                    f"{final.get('is_error')}"}
    return {**result, "state": "pass", "why": ""}


def _denied_call(row) -> str:
    """One denial as the retry's brief names it: the tool and its command or path."""
    if not isinstance(row, dict):
        return str(row)
    given = row.get("tool_input")
    given = given if isinstance(given, dict) else {}
    what = given.get("command") or given.get("file_path") or given.get("path")
    return f"{row.get('tool_name')} `{str(what)[:300]}`" if what else str(row.get("tool_name"))


def _read(argv: list[str], *, env: dict | None = None) -> str:
    """A short read-only command's stdout; `Unread` when it fails or gives no answer in `READ_SECONDS`."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=READ_SECONDS, check=False,
                              stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        raise Unread(f"{argv[0]} gave no answer: {error}") from None
    if done.returncode != 0:
        raise Unread(f"{' '.join(argv[:4])} failed: exit {done.returncode}: {done.stderr.strip()[:200]}")
    return done.stdout


def _read_bytes(argv: list[str], *, env: dict | None = None) -> bytes:
    """`_read` for output in no known encoding: its stdout as bytes, `Unread` the same way."""
    try:
        done = subprocess.run(argv, capture_output=True, timeout=READ_SECONDS, check=False, stdin=subprocess.DEVNULL,
                              env=env)
    except (OSError, subprocess.SubprocessError) as error:
        raise Unread(f"{argv[0]} gave no answer: {error}") from None
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip()[:200]
        raise Unread(f"{' '.join(argv[:4])} failed: exit {done.returncode}: {said}")
    return done.stdout


def kept_files(git: Collection[str], work: str, *, env: dict | None = None) -> str:
    """The status lines of `work` that `git worktree remove` would lose: changes, untracked and ignored
    files, those config hides too, less the ignored ones under a `DISPOSABLE` folder; "" when it may go."""
    lines = _read([*git, "-C", work, "status", "--porcelain", "--ignored", "--untracked-files=all"], env=env)
    return "\n".join(line for line in lines.splitlines() if line.strip() and not (
        # Folder parts only: git ends an ignored folder with "/", so a file of that name is no cache.
        line.startswith("!! ") and DISPOSABLE.intersection(line[3:].strip('"').split("/")[:-1])))


def lane_entry(ship: list[str], cwd: Path | str, items: Collection[int], *, env: dict | None = None) -> dict | None:
    """The newest lane entry of any of `items` by enqueue order, from `sd-ship lane list`; None when there is none.

    Any member: the entry's item is the first member the branch delivers.
    """
    try:
        answer = json.loads(_read([*ship, "-C", str(cwd), "lane", "list"], env=env))
    except ValueError:
        raise Unread("sd-ship lane list answered no JSON") from None
    entries = answer.get("entries") if isinstance(answer, dict) and answer.get("ok") else None
    if not isinstance(entries, list):
        raise Unread("sd-ship lane list gave no entries")
    mine = [row for row in entries if isinstance(row, dict) and row.get("item") in items]
    return max(mine, key=lambda row: row.get("enqueue_seq") or 0) if mine else None


def _lead_item(connection, ident: str) -> tuple[int, list[int]]:
    members = build.run(connection, ident)["members"]
    return members[0]["item"], [member["item"] for member in members[1:]]


def watch(connection, *, lane: Callable[[dict, list[int]], dict | None], probe=Probe,
          hosts: Collection[str | None] | None = None) -> list[dict]:
    """Step 7's start: each open `enqueued` run whose lane entry merged moves to `cleaning`; one whose entry
    failed, was skipped or was cancelled ends `lane_failed`. An unread lane changes nothing this tick.
    One that failed only on prepare's merge of main also goes back to its builder (`build.lane_failed`, sd:6940).

    A merged or failed run moves only once `verdict` reads its last supervisor and child gone, so the fresh
    supervisor or rebuild never shares the worktree. Only the runs of `hosts`
    (`build.lane_hosts` when None) are watched: another host's lane and pids
    are that host's to read (pack sd:7933).
    """
    moved = []
    for row in build.open_runs(connection, build.lane_hosts(connection) if hosts is None else hosts):
        if row["step"] != "enqueued" or row["outcome"] is not None:
            continue
        url = build.draft_pull(row)
        if url is not None:
            # An unmanaged repository's draft pull request: the owners merge it, and the contribution
            # observation of its url says when (pack sd:7868). The lane holds nothing of it.
            status = pull_state(connection, url)
            if status == "merged" and build.verdict(row, probe=probe)[0] == build.GONE:
                if build.pull_merged(connection, row, url):
                    moved.append({"run": row["id"], "step": "cleaning"})
            elif status == "closed" and build.verdict(row, probe=probe)[0] == build.GONE:
                if build.lane_failed(connection, row, f"{build.DRAFT_PULL}{url} was closed without a merge"):
                    moved.append({"run": row["id"], "outcome": "lane_failed"})
            continue
        delivered = [member["item"] for member in build.run(connection, row["id"])["members"]
                     if member["status"] == "done"]
        try:
            entry = lane(row, delivered)
        except Unread:
            continue
        status = entry.get("status") if entry else None
        if status == "merged" and build.verdict(row, probe=probe)[0] == build.GONE:
            if build.merged(connection, row):
                moved.append({"run": row["id"], "step": build.run(connection, row["id"])["step"]})
        elif (entry is not None and status in LANE_ENDED
              and build.verdict(row, probe=probe)[0] == build.GONE):  # a rebuild must not share a live worktree
            reason = f"lane entry {entry.get('id')} {status}: {entry.get('reason') or entry.get('code') or 'no reason'}"
            if build.lane_failed(connection, row, reason):
                moved.append({"run": row["id"], "outcome": "lane_failed"})
    return moved


def pull_state(connection, url: str) -> str | None:
    """The state the contribution observer last read of `url` (`open`, `closed`, `merged`), or None unread."""
    try:
        observation = contributions.snapshot(connection, "github:" + url).get("observation")
    except WorkflowError:
        return None
    return observation.get("state") if isinstance(observation, dict) else None


def sweep(connection, *, git: Collection[str] = ("git",), hosts: Collection[str | None] | None = None) -> list[str]:
    """Remove each build worktree, and its branch, that no run owns any more once every item it names is done.

    A blocked or cancelled run keeps its worktree for Retry, and a later build
    may land the same items; the lane squash-merges, so the branch reads
    unpushed forever (pack sd:6909). Kept: an unreleased run on the path, an
    item of any run on it not done, a dirty or unread worktree, one holding an
    ignored or untracked file outside a `DISPOSABLE` cache, a locked one, a
    live session other than a builder's. The paths removed.

    An item marked done is no proof its commits landed: a squash merge leaves
    no ancestry to check, and an item can be closed by hand. So the branch
    head is first kept at `refs/sd-swept/<branch>`, outside `refs/heads`, and
    a note on the lead item names it; a head that cannot be kept removes
    nothing. `git branch <name> refs/sd-swept/<name>` brings it back.

    Only the worktrees of `hosts`' runs (`build.lane_hosts` when None) are
    swept: a path another host's run names is on that host's disk (pack sd:7933).
    """
    hosts = build.lane_hosts(connection) if hosts is None else hosts
    rows = connection.execute(
        "SELECT runner_run.work_path, runner_run.repo, runner_run.branch, runner_run.session_id, runner_run.owner, "
        "runner_run.released_at, item.status, item.id AS item, "
        "assignment.id = runner_run.assignment AS lead FROM runner_run "
        "JOIN assignment ON assignment.id = runner_run.assignment OR assignment.parent = runner_run.assignment "
        "JOIN item ON item.id = assignment.item WHERE assignment.scope = ? AND runner_run.work_path IS NOT NULL",
        (build.SCOPE,)).fetchall()
    paths_: dict[str, dict] = {}
    for row in rows:
        if not build.mine(row, hosts):
            continue
        entry = paths_.setdefault(row["work_path"], {"repo": row["repo"], "branch": row["branch"], "keep": False,
                                                     "sessions": set(), "lead": row["item"]})
        entry["keep"] |= row["released_at"] is None or row["status"] != "done"
        entry["sessions"].add(row["session_id"])
        if row["lead"]:
            entry["lead"] = row["item"]
    removed = []
    for work, entry in paths_.items():
        if entry["keep"] or not Path(work).is_dir():
            continue
        try:
            if kept_files(git, work):
                continue
        except Unread:
            continue
        if sessions.holders(connection, work, machine=this_host(), exclude=tuple(filter(None, entry["sessions"]))):
            continue
        repo = str(paths.expand(entry["repo"]))
        kept = f"refs/sd-swept/{entry['branch']}"
        try:
            head = _read([*git, "-C", work, "rev-parse", "--verify", "HEAD"]).strip()
            if head != _read([*git, "-C", repo, "rev-parse", "--verify", f"refs/heads/{entry['branch']}"]).strip():
                continue  # a detached HEAD off its branch: two heads to keep, so the sweep keeps both
            _read([*git, "-C", repo, "update-ref", kept, head])
        except Unread:
            continue
        try:
            _read([*git, "-C", repo, "worktree", "remove", work])
        except Unread:
            continue
        removed.append(work)
        add_note(connection, entry["lead"], "comment",
                 f"Build worktree {work} swept: every item is done; its head {head[:12]} is kept at {kept}")
        try:
            _read([*git, "-C", repo, "branch", "-D", entry["branch"]])
        except Unread:
            pass  # the worktree is gone and the head kept; a branch left over costs nothing
    return removed


def dispatch(connection, *, owner: str, load: float | None, load_cap: float | None, interactive: list[dict] | None,
             spawn: Callable[[str], Any], lane: Callable[[dict, list[int]], dict | None], why: str = "",
             root: Path | None = None, probe=Probe, kill=build.kill_group) -> dict:
    """One tick: heartbeat, reap, watch the lane, sweep, claim, spawn; what each did.

    `load`, `load_cap` and `interactive` are read before the claim, outside
    the database; any unread (None) means no claim this tick, and `why` names
    it, as it names a reason the caller found (such as an unwritable log folder).

    Each step takes only this machine's runs and lanes (`build.lane_hosts`,
    pack sd:7933). Auto-pickup picks this host's lanes (sd:8393); its stop
    on blocks and the drafts run on the hub only.
    """
    hosts = build.lane_hosts(connection)
    hub = None in hosts
    # The beat carries why this tick claims nothing: the hub reads a satellite's beat as one that would claim a
    # press only when `why` is empty (`build.dispatcher_refusal`, review round 4).
    why = why or build.unread(load, load_cap, interactive) or (
        build.RESTORING if build.restoration_pending(connection) else "")
    record_state(connection, "heartbeat", key=HEARTBEAT if hub else f"{HEARTBEAT}@{build.this_host()}",
                 body={"pid": os.getpid(), "owner": owner, "why": why})
    reaped = build.reap(connection, probe=probe, kill=kill, hosts=hosts)
    if hub:
        reaped += draft.reap(connection, probe=probe, kill=kill, hosts=hosts)
    moved = watch(connection, lane=lane, probe=probe, hosts=hosts)
    swept = sweep(connection, hosts=hosts)
    stopped = build.stop_on_blocks(connection) if hub else None
    picked: list[int] = []
    if not why:
        picked = build.pickup(connection, build.room(connection, load=load, load_cap=load_cap,
                                                     interactive=interactive, hosts=hosts))
    claimed: list[str] = []
    while not why and len(claimed) < build.MACHINE_CAP:
        try:
            taken = build.claim(connection, owner=owner, load=load, load_cap=load_cap, interactive=interactive,
                                root=root, hosts=hosts)
        except build.BuildRefused as refusal:
            why = str(refusal)
            break
        if taken is None:
            break
        claimed.append(taken["id"])
    drafted: list[str] = []
    if not why and hub:
        # After the builds: a draft takes what machine room they leave, and no repository slot.
        available = build.room(connection, load=load, load_cap=load_cap, interactive=interactive, hosts=hosts)
        taken = draft.claim(connection, owner=owner, available=available)
        drafted += [taken["id"]] if taken else []
    spawned = []
    for row in [*build.open_runs(connection, hosts), *draft.open_runs(connection, hosts)]:
        if row["outcome"] is None and row["supervisor_pid"] is None and row["step"] != "enqueued":
            spawn(row["id"])
            spawned.append(row["id"])
    return {"reaped": reaped, "moved": moved, "swept": swept, "stopped": stopped, "picked": picked, "claimed": claimed,
            "drafted": drafted, "spawned": spawned, "why": why}


def spawn(argv: list[str], *, log: Path) -> int:
    """Start a supervisor in a new session, out of the tick's process group; its pid."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as sink:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True)
    return process.pid


def log_dir(log_root: Path, repo: str) -> Path:
    """`<root>/<repository>/build/`, the design's log folder, under the local or the bulk root."""
    return log_root / paths.expand(repo).name / "build"


def local_root(environ: dict | None = None) -> Path:
    """Where a live run writes its logs, `$XDG_STATE_HOME/sd/builds` on the system disk: the bulk root
    may be a network volume (pack sd:8246). `archive` moves an ended run's files to the bulk root."""
    env = os.environ if environ is None else environ
    state = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local/state")
    return Path(state) / "sd" / "builds"


def resume_root(ident: str, bulk: Path) -> Path:
    """The log root for run `ident`'s supervisor: the one an earlier image of this process handed over, whose
    kept child still writes there; `bulk` for an image from before pack sd:8246, which named none; else local."""
    state = resume_state(ident)
    if state is None:
        return local_root()
    return Path(state["root"]) if state.get("root") else bulk


#: A file written this recently stays local: the supervisor may still print its last line.
ARCHIVE_SETTLE_SECONDS = 60


#: How long one tick waits on the bulk root's copies: a hung network mount must not hold dispatch.
ARCHIVE_DEADLINE_SECONDS = 20


#: The lock file under the local root that the one archive copier holds.
ARCHIVE_LOCK = ".archive.lock"


def _same_copy(path: Path, target: Path) -> bool:
    """Whether `target` is a finished copy of `path`: a copy lands only by rename, and `copy2` keeps the
    size and mtime; two seconds of slack for a volume that stores coarse times."""
    try:
        source, copy = path.stat(), target.stat()
    except FileNotFoundError:
        return False
    return copy.st_size == source.st_size and abs(copy.st_mtime - source.st_mtime) <= 2


class _ArchiveLocked(Exception):
    """`runner_journal.lock` refused the archive lock: held by a live copier when its cause is BlockingIOError."""


#: The copier the last `archive` in this process started; `archive_wait` gives it the rest of the tick.
_copier: threading.Thread | None = None


def archive_wait(seconds: float) -> None:
    """Wait up to `seconds` for this process's copier, once the tick closed its connection (sd:8339).

    The tick process kills the daemon copier when it exits, so without this wait a copy longer than
    `ARCHIVE_DEADLINE_SECONDS` never landed. A copy done in the wait is collected by the next tick.
    """
    if _copier is not None and seconds > 0:
        _copier.join(seconds)


def archive(connection, local: Path, bulk: Path, *, settle: float = ARCHIVE_SETTLE_SECONDS,
            deadline: float = ARCHIVE_DEADLINE_SECONDS) -> list[str]:
    """Move each file of an ended run from `local` to the same place under `bulk`; the new paths.

    `sd-<assignment>.title` and `.body.md` move once every run of that assignment ended and was released,
    the rest once the run their name starts with was: an outcome alone leaves a blocked or abandoned run's
    groups alive until the reap releases it, and they may still write. A file no run owns, one written in the
    last `settle` seconds or one whose copy fails stays for a later tick.

    The copies run on a daemon thread the tick waits on for `deadline` seconds at most (sd:8275): a copy not
    done by then is left for a later tick. The rows then name the copy, in `log_path` and in every note body,
    run detail and assignment result that named the local file, before the local file goes.

    One copier at a time (sd:8275 lane round 1): the thread holds `ARCHIVE_LOCK` under `local` until it ends,
    and a tick that finds it held copies nothing, as a tick in another process does while an earlier one's
    copy still hangs. A copy goes to a name of its own and is renamed into place, so a late or failed copy
    never opens the archive itself; a copy an earlier tick finished is collected, not made again. A file
    with a killed copier's part file is copied after the others, so one too slow for any tick holds none.
    """
    pending, cutoff = [], time.time() - settle
    for path in sorted(local.glob("*/build/*")):
        owner = re.fullmatch(r"sd-(\d+)\.(?:title|body\.md)", path.name)
        ended = "SELECT outcome IS NOT NULL AND released_at IS NOT NULL FROM runner_run WHERE "
        rows = connection.execute(ended + "assignment = ?", (int(owner[1]),)) if owner \
            else connection.execute(ended + "id = ?", (path.name.split(".", 1)[0],))
        released = [row[0] for row in rows]
        if not released or not all(released):
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        pending.append((path, bulk / path.relative_to(local)))
    if not pending:
        return []
    held = contextlib.ExitStack()  # the copier thread closes it: the lock lasts as long as the copier
    try:
        held.enter_context(runner_journal.lock(local / ARCHIVE_LOCK, blocking=False, noun="build archive",
                                               error=_ArchiveLocked))
    except _ArchiveLocked as refused:
        if isinstance(refused.__cause__, BlockingIOError):
            return []  # an earlier copier still runs: it may hold any of these files open
        raise OSError(str(refused)) from refused
    copied: list[tuple[Path, Path]] = []

    def copy() -> None:
        try:
            fresh, killed = [], []
            for path, target in pending:
                try:
                    if _same_copy(path, target):
                        copied.append((path, target))
                    elif any(target.parent.glob(f".{target.name}.*.part")):
                        killed.append((path, target))  # outlasted a tick: the others go first (sd:8339)
                    else:
                        fresh.append((path, target))
                except OSError:
                    continue
            for path, target in fresh + killed:
                part = target.with_name(f".{target.name}.{os.getpid()}.part")
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    for stale in target.parent.glob(f".{target.name}.*.part"):
                        stale.unlink(missing_ok=True)  # a killed copier's; the lock says none runs
                    shutil.copy2(path, part)
                    os.replace(part, target)
                except OSError:
                    with contextlib.suppress(OSError):
                        part.unlink(missing_ok=True)
                    continue
                copied.append((path, target))
        finally:
            held.close()

    global _copier
    worker = _copier = threading.Thread(target=copy, name="sd-build-archive", daemon=True)
    worker.start()
    worker.join(deadline)
    moved = []
    # A copy still running past the deadline is not in this snapshot; a later tick collects it.
    for path, target in list(copied):
        old, new = str(path), str(target)
        # The local file goes only after the rows name the copy: a failed write leaves it for the next tick.
        with transaction(connection):
            connection.execute("UPDATE runner_run SET log_path = ? WHERE log_path = ?", (new, old))
            for table, column in (("note", "body"), ("runner_run", "detail"), ("assignment", "result")):
                connection.execute(f"UPDATE {table} SET {column} = replace({column}, ?, ?) "
                                   f"WHERE instr({column}, ?) > 0", (old, new, old))
        try:
            path.unlink()
        except OSError:
            continue  # the rows name the copy; the next tick copies again and removes it
        moved.append(new)
    return moved


def _environ(environ: dict | None) -> dict:
    env = dict(os.environ if environ is None else environ)
    # The token reaches the child's environment only: no row or log holds it.
    if env.get("CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = env["CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"]
    return env


class Supervisor:
    """One run's supervisor: `run()` walks its steps and returns how it ended."""

    #: While a child that asked for it runs, the groups its descendants started, apart from its own (`sd gate
    #: run` starts its command in a new group), each with its leader's start: `end_group` ends one that
    #: outlived its parent only while it is still that group. None for every other child.
    groups: dict[int, str] | None = None

    def __init__(self, connection, ident: str, *, tools: Tools, log_root: Path,
                 cited: Callable[[Path, str, str, str], Collection[int] | None], environ: dict | None = None,
                 beat_seconds: float = BEAT_SECONDS, timeout: float = TIMEOUT_SECONDS, grace: float = GRACE_SECONDS,
                 probe=Probe, checked: Callable[[Path, str, str, str, str], list[str]] | None = None,
                 checks: Callable[[Path], list[list[str]]] | None = None,
                 settle_seconds: float = build.STALE_SECONDS):
        """`cited(worktree, base, head, body)` is the items the lane reads the branch as delivering, None when unread:
        the lane's own reader, which lives beside it in `bin/`. `checked(worktree, base, head, title, body)` is
        what enqueue and its push would refuse, the line check and the leak guard (pack sd:6926); None checks
        nothing. `checks(worktree)` is the argv of each check an unmanaged repository's run runs after a clean
        review, as `sd-check` detects them, or ValueError when they do not read (pack sd:7868); None runs none."""
        self.db: Any = Patient(connection)  # duck-types the connection the writes are annotated with
        self.ident, self.tools, self.probe, self.cited, self.checked = ident, tools, probe, cited, checked
        self.checks = checks
        self.beat_seconds, self.timeout, self.grace = beat_seconds, timeout, grace
        self.row = build.run(self.db, ident)
        self.log_root, self.logs = log_root, log_dir(log_root, self.row["repo"])
        self.work = Path(self.row["work_path"])
        self.repo = paths.expand(self.row["repo"])
        self.lead, self.others = _lead_item(self.db, ident)
        lead_id = self.row["assignment"]
        self.title = self.logs / f"sd-{lead_id}.title"
        self.body = self.logs / f"sd-{lead_id}.body.md"
        self.log = self.logs / f"{ident}.jsonl"
        self.reviewed: str | None = None
        self.blockers: list[dict] = []
        self.last_beat = 0.0
        self.env = _environ(environ)
        # Claude Code's own OTel, where the machine turns it on, names the run
        # by these (pack sd:6943); the switch and endpoint are system sd:6944's.
        self.env["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(filter(None, (
            self.env.get("OTEL_RESOURCE_ATTRIBUTES"), f"sd.build_run={ident}",
            "sd.items=" + ";".join(str(item) for item in (self.lead, *self.others)))))
        self.take_resume(settle_seconds)

    # ----------------------------------------------------------- recovery (pack sd:8182)

    def take_resume(self, settle_seconds: float) -> None:
        """What an earlier image of this process handed over, off the environment so no child inherits it.

        Only this run's state, in this process, counts: anything else is ignored, so the run registers as it
        would. `last` is the last child and what it left; `held` is that record while a replay reaches it;
        `ending` is an end not yet written; `marks` are the once-only writes done since `last` started.
        """
        self.settle_seconds = settle_seconds
        self.last: dict | None = None
        self.held: dict | None = None
        self.ending: tuple | None = None
        self.marks: set[str] = set()
        self.unknown: dict[str, str] = {}
        self.hub_down = False
        self.down_since: float | None = None
        self.deadline = 0.0
        self.resumed = False
        state = resume_state(self.ident)
        os.environ.pop(RESUME_ENV, None)
        self.env.pop(RESUME_ENV, None)
        if state is None:
            return
        self.resumed = True
        self.last = self.held = state.get("child")
        self.reviewed = state.get("reviewed")
        self.ending = tuple(state["ending"]) if state.get("ending") else None
        self.marks = set(state.get("marks") or ())
        self.unknown = dict(state.get("unknown") or {})
        groups = state.get("groups")
        self.groups = None if groups is None else {int(group): start for group, start in groups.items()}

    def state(self) -> dict:
        """What the next image needs, in JSON: the child kept, and what this image holds only in memory."""
        last = self.last
        if last is not None and last["exit"] is None:
            last["left"] = max(self.deadline - time.monotonic(), 0.0)
        return {"run": self.ident, "pid": os.getpid(), "child": last, "groups": self.groups, "reviewed": self.reviewed,
                "ending": self.ending, "marks": sorted(self.marks), "unknown": self.unknown, "root": str(self.log_root)}

    def fault_seen(self, fault: Exception) -> None:
        if not self.hub_down:
            self.hub_down = True
            print(f"sd: build run {self.ident}: {fault}; keeping its child and asking again", file=sys.stderr, flush=True)

    def follow_build(self, fault: Exception) -> None:
        """Exec in place once the disk holds the build the hub refused this process for; else ask again later."""
        if isinstance(fault, remote.BuildMismatch) and remote.disk_holds(fault):
            exec_in_place(self.state())

    def outage(self, fault: Exception) -> dict | None:
        """A hub fault with no child left to keep: wait for the hub, then walk again; the run's end when it stays gone.

        The walk restarts from the step on the row, each child that already ran answering from its record, so a
        result in the window is judged once. `settle_seconds` counts from the first fault since the walk last got
        anywhere (a child started, a beat landed), so a statement that fails every time still ends: the
        supervisor exits, the row stays open for the reap, and no process waits on a hub that never comes back.
        """
        self.fault_seen(fault)
        last = self.last
        self.held = last if last is not None and last["exit"] is not None else None
        if self.down_since is None:
            self.down_since = time.monotonic()
        stop = self.down_since + self.settle_seconds
        while True:
            self.follow_build(fault)
            if time.monotonic() >= stop:
                return {"run": self.ident, "outcome": None,
                        "why": f"no answer from the hub for {self.settle_seconds:g} s: {fault}"}
            time.sleep(min(self.beat_seconds, 5.0))
            try:
                self.db.execute("SELECT 1").fetchone()
            except HUB_FAULTS as again:
                fault = again
            else:
                self.hub_down = False
                return None

    def adopt(self) -> None:
        """Wait out the child an earlier image kept; it still runs as this process's child."""
        held = self.held
        if held is None or held["exit"] is not None:
            return
        self.last = held
        held["exit"] = self.waited(Adopted(held["pid"]), held["name"], session=held["session"],
                                   deadline=time.monotonic() + held["left"])

    def replayed(self, argv: list[str], name: str, role: str | None) -> int | None:
        """A held child's exit code when the walk reaches it, 0 for one before it; None to start the child.

        The walk re-runs each step from its top, so a child that already ran must not run again. A claude child
        matches by the `role` it started as, not its prompt, which reads the session the first image wrote: a
        fault starting a fix leaves the builder held, and it must not answer for the fix. A child outside the
        held one's path means the walk took another way, so nothing is skipped from there on.
        """
        held = self.held
        if held is None:
            return None
        if held["name"] == name and (held.get("as") == role if name == "claude" else held["argv"] == argv):
            self.held, self.last = None, held
            return held["exit"]
        if name in PRECEDES.get(held["name"], ()):
            return 0
        self.held = None
        return None

    def will_replay(self, name: str, role: str | None = None) -> bool:
        """Whether the walk's next `name` child answers from the held record (`replayed`), not by running.

        Asked before a child's output is emptied or its offset taken (pack sd:8194): a held child of another name
        or role is not the one starting, so its output and lines are not this child's.
        """
        held = self.held
        if held is None:
            return False
        if held["name"] == name:
            return name != "claude" or held.get("as") == role
        return name in PRECEDES.get(held["name"], ())

    def empty(self, path: Path, name: str) -> None:
        """Empty a `name` child's output before it runs; a replay keeps what the child it replays already wrote."""
        if not self.will_replay(name):
            path.write_bytes(b"")

    def start_offset(self, role: str | None) -> int:
        """Where this claude child's stream lines start: a replayed child's own offset, else the log's end now."""
        held = self.held
        if self.will_replay("claude", role) and held is not None and held.get("offset") is not None:
            return held["offset"]
        return self.log.stat().st_size if self.log.exists() else 0

    def once(self, mark: str, write: Callable[[], Any]) -> None:
        """Run `write` unless a replay already did: a cost, a note and a round's count are not idempotent.

        A write whose answer was lost (`UnknownOutcome`) may have committed, so its request id waits here: the
        hub's answer decides on the replay. A hub that cannot say yet is one more fault, under `outage`'s bound.
        """
        if mark in self.marks:
            return
        rid = self.unknown.get(mark)
        if rid is not None:
            answer = self.db.outcome(rid)
            if answer == remote.RECORDED:
                self.marks.add(mark)
                del self.unknown[mark]
                return
            if answer != remote.ABSENT:
                raise remote.UnknownOutcome(rid, f"the hub answers {answer} for it")
            del self.unknown[mark]
        try:
            write()
        except remote.UnknownOutcome as fault:
            self.unknown[mark] = fault.rid
            raise
        self.marks.add(mark)

    def note(self, item: int, body: str) -> None:
        """A comment the walk can reach again on a replay; a comment after the walk's last step needs no guard."""
        self.once(f"note:{body}", lambda: add_note(self.db, item, "comment", body))

    # ----------------------------------------------------------- the walk

    def run(self) -> dict:
        pid = os.getpid()
        if not self.resumed:
            if not build.register(self.db, self.ident, pid=pid, pgid=os.getpgrp(), start=self.probe.started(pid)):
                return {"run": self.ident, "outcome": None, "why": "another supervisor holds the run, or it ended"}
            self.last_beat = time.monotonic()
        self.logs.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                return self.ended()
            except HUB_FAULTS as fault:
                gone = self.outage(fault)
                if gone is not None:
                    return gone

    def ended(self) -> dict:
        try:
            return self.walked()
        except Lost:
            return {"run": self.ident, "outcome": None, "why": "the run ended or moved under this supervisor"}
        except Cancelled:
            return self.finish("cancelled", self.fresh()["cancel_requested"] or "cancelled")
        except TimedOut:
            return self.finish("timed_out", f"a child ran past {self.timeout:g} s; its group was ended")
        except Survivor as survivor:
            return self.finish("blocked", str(survivor))
        except Unread as unread:
            # An unknown result: no write, so the reap finds the run lost and retries it once.
            return {"run": self.ident, "outcome": None, "why": str(unread)}

    def walked(self) -> dict:
        """The child an earlier image kept, an end waiting to be written, then the walk."""
        self.adopt()
        if self.ending is not None:
            outcome, detail, question = self.ending
            return self.finish(outcome, detail, question=question)
        return self.walk()

    def walk(self) -> dict:
        step = self.fresh()["step"]  # read now: a walk after a hub fault starts where the row is
        steps = {"claimed": self.worktree, "worktree": self.worktree, "building": self.building,
                 "reviewing": self.reviewing, "fixing": self.reviewing, "enqueuing": self.enqueuing,
                 "checking": self.checking, "cleaning": self.cleaning}
        if self.held is not None and self.held["name"] == "claude" and self.held.get("as") == self.fix_role():
            steps["fixing"] = self.fix  # a kept builder is the fix itself, not the review after it
        while step in steps:
            ended = steps[step]()
            if isinstance(ended, dict):
                return ended
            step = ended
        if step == "enqueued":
            return {"run": self.ident, "outcome": None, "step": "enqueued"}
        return self.finish("blocked", f"no supervisor step for {step}")

    def finish(self, outcome: str, detail: str, *, question: str | None = None) -> dict:
        self.ending = (outcome, detail, question)  # kept: a hub fault here must not lose how the run ended
        ended = build.end(self.db, self.ident, outcome, detail, question=question)
        return ended or {"run": self.ident, "outcome": None, "why": "the run ended or moved before its last write"}

    def fresh(self) -> dict:
        return build.run(self.db, self.ident)

    def enter(self, step: str, **fields) -> None:
        if self.fresh()["cancel_requested"] and step in build.CANCELLABLE:
            raise Cancelled
        if not build.write(self.db, self.ident, step=step, heartbeat_at=now(), **fields):
            raise Lost
        self.last_beat = time.monotonic()

    # -------------------------------------------------------------- steps

    def worktree(self) -> str | dict:
        self.enter("worktree")
        branch = self.row["branch"]
        if self.work.is_dir():
            current = _read([*self.tools.git, "-C", str(self.work), "branch", "--show-current"], env=self.env).strip()
            if current != branch:
                return self.finish("blocked", f"worktree {self.work} is on {current or 'no branch'}, not {branch}")
            return "building"
        if self.child([*self.tools.git, "-C", str(self.repo), "fetch", "-q", "origin"], cwd=self.repo,
                      name="worktree") != 0:
            return self.finish("blocked", f"git fetch in {self.repo} failed; see {self.out('worktree')}")
        try:
            _read([*self.tools.git, "-C", str(self.repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], env=self.env)
            argv = ["worktree", "add", str(self.work), branch]
        except Unread:
            argv = ["worktree", "add", "-b", branch, str(self.work), self.base()]
        if self.child([*self.tools.git, "-C", str(self.repo), *argv], cwd=self.repo, name="worktree") != 0:
            return self.finish("blocked", f"git worktree add {self.work} failed; see {self.out('worktree')}")
        return "building"

    def building(self) -> str | dict:
        self.enter("building", log_path=str(self.log))
        session = self.fresh()["session_id"]
        prompt = (RESUME if session else BRIEF).format(**self.brief())
        return self.claude(prompt, session=session, then="reviewing", role="building")

    def reviewing(self) -> str | dict:
        self.enter("reviewing")
        head = self.head()
        # What enqueue would refuse goes back to the builder before a review is spent (pack sd:6926).
        refused = self.prechecked(head)
        if refused:
            return self.blocked_round([{"path": "enqueue check", "line": "-", "summary": text} for text in refused])
        answer: dict | None = None
        for _attempt in range(2):
            out = self.out("review")
            self.empty(out, "review")
            self.child([*self.tools.review, "-C", str(self.work), "--scope", "branch", "--json"], cwd=self.work,
                       name="review")
            answer = self.review_answer(out, head)
            if answer is not None:
                break
        if answer is None:
            return self.finish("blocked", f"the review gave no answer twice at {head[:12]}; see {self.out('review')}")
        if answer["status"] in ("clean", "advisory", "skipped"):
            # `sd-review` runs no gate in an unmanaged checkout (sd:5953): the repository's own checks run here.
            failed = self.own_checks(head) if build.unmanaged(self.db, self.row["repo"]) else []
            if isinstance(failed, dict):
                return failed
            if failed:
                return self.blocked_round(failed)
            self.reviewed = head
            return "enqueuing"
        return self.blocked_round(answer["blockers"])

    def own_checks(self, head: str) -> list[dict] | dict:
        """Run an unmanaged repository's own checks at `head`, each in a gate slot; a blocker per failure.

        No check found is noted on the lead item and passes: a repository with none did not fail one.
        Entrypoints that do not read are a configuration fault, no code blocker: the run's blocked end (a
        dict), not a paid fix round; a `git` that gave no answer raises `Unread` in `checks`.
        """
        if self.checks is None:
            return []
        try:
            commands = self.checks(self.work)
        except ValueError as error:
            return self.finish("blocked", f"the repository's check entrypoints did not read, a configuration "
                                          f"fault no fix round can mend: {error}")
        if not commands:
            self.note(self.lead, f"Build run {self.ident}: no check entrypoint found in the repository; "
                                 f"no tests ran at {head[:12]}")
            return []
        passed = []
        for argv in commands:
            said = shlex.join(argv)
            for name in ("checks", "checks.err"):
                self.empty(self.out(name), "checks")
            code = self.child([*self.tools.gate, "--label", f"build {self.row['branch']} checks", "--", *argv],
                              cwd=self.work, name="checks", descendants=True)
            if code == 125 and self.refused():  # the gate started nothing: unknown, never a failure of the code
                raise Unread(f"sd gate run did not start `{said}`; see {self.out('checks.err')}")
            if code != 0:
                tail = "".join(path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
                               for path in (self.out("checks"), self.out("checks.err")))
                return [{"path": said, "line": "-", "summary": f"exit {code}: {tail.strip()[-1500:]}"}]
            # A check that rewrote a tracked file tested a tree `head` does not hold; an unread status raises Unread.
            changed = _read([*self.tools.git, "-C", str(self.work), "-c", "core.quotePath=false", "status",
                             "--porcelain", "--untracked-files=no"], env=self.env)
            if changed.strip():
                paths = ", ".join(line[3:] for line in changed.splitlines() if line.strip())
                return [{"path": said, "line": "-", "summary": f"exit 0, but it changed tracked files: {paths}; "
                         "commit or undo them, so the checked tree is the pushed head"}]
            passed.append(f"`{said}`")
        self.note(self.lead, f"Build run {self.ident}: {', '.join(passed)} passed at {head[:12]}")
        return []

    def refused(self) -> bool:
        """The gate's own refusal ends `checks.err`; a check that exits 125 itself prints no such line."""
        said = self.out("checks.err").read_text(encoding="utf-8", errors="replace").splitlines()
        return bool(said) and said[-1] == GATE_NOT_STARTED

    def prechecked(self, head: str) -> list[str]:
        if self.checked is None:
            return []
        try:
            title, body = (path.read_text(encoding="utf-8") if path.exists() else "" for path in (self.title, self.body))
        except ValueError:  # not UTF-8: the builder rewrites it, as for any other refusal
            return [f"the title at {self.title} or the body at {self.body} is not UTF-8 text"]
        return self.checked(self.work, self.base(), head, title.strip(), body)

    def blocked_round(self, blockers: list[dict]) -> str | dict:
        self.blockers = blockers
        # Once per child that blocked: a fault after this write re-walks the held review, which is the same review.
        self.once(f"round:{(self.last or {}).get('pid')}", self.count_round)
        rounds = self.fresh()["review_round"] or 0
        if rounds >= REVIEW_ROUNDS:
            return self.finish("blocked", f"{rounds} review rounds left blockers: {self.summary(self.blockers)}")
        return self.fix()

    def count_round(self) -> None:
        if not build.write(self.db, self.ident, review_round=(self.fresh()["review_round"] or 0) + 1):
            raise Lost

    def fix_role(self) -> str:
        """What this round's fix child starts as: a round's count moves only between two fixes."""
        return f"fix:{self.fresh()['review_round'] or 0}"

    def fix(self) -> str | dict:
        self.enter("fixing")
        session = self.fresh()["session_id"]
        if not session:
            return self.finish("blocked", "no builder session recorded to resume for the fix")
        prompt = FIX.format(blockers=self.summary(self.blockers, limit=None), **self.brief())
        return self.claude(prompt, session=session, then="reviewing", role=self.fix_role())

    def enqueuing(self) -> str | dict:
        if not build.enqueuing(self.db, self.ident):
            if self.fresh()["cancel_requested"]:
                raise Cancelled
            raise Lost
        self.last_beat = time.monotonic()
        head = self.head()
        unmanaged = build.unmanaged(self.db, self.row["repo"])
        # No lane gate re-checks an unmanaged head: it publishes only the head this supervisor's review and
        # checks passed, so a resumed or retried run reviews and tests again first (review of pack sd:7868).
        if head != self.reviewed and (unmanaged or self.reviewed is not None):
            if not build.write(self.db, self.ident, step="reviewing"):
                raise Lost
            return "reviewing"
        try:
            body = self.body.read_text(encoding="utf-8")
        except OSError:
            body = ""
        except ValueError:
            return self.finish("blocked", f"the body at {self.body} is not UTF-8 text")
        cited = self.cited(self.work, self.base(), head, body)
        if cited is None:
            raise Unread(f"the commits {head[:12]} adds were not read")
        # Delivery as the lane reads it: a member no `Closes:` trailer cites was skipped, and stays open.
        delivered = [item for item in [self.lead, *self.others] if item in cited]
        # A retry's branch keeps the lost run's trailers: a member this run's builder skipped with a question
        # waits on it (pack sd:6571).
        asked = [item for item in delivered if build.open_question(self.db, [item], self.row["created_at"])]
        delivered = [item for item in delivered if item not in asked]
        if not delivered and asked:
            return self.finish("blocked", f"every cited item has an open question since the run started: "
                                          f"{', '.join(f'sd:{item}' for item in asked)}")
        if asked:
            # The lane closes every open item a branch trailer cites; a `Refs:` line keeps these open.
            refs = f"Refs: {' '.join(f'sd:{item}' for item in asked)}"
            if refs not in body.splitlines():
                body = body.rstrip("\n") + f"\n\n{refs}\n"
                self.body.write_text(body, encoding="utf-8")
        if not delivered:
            return self.finish("blocked", "the branch delivers no item: no commit has a `Closes: sd:N` trailer "
                                          "for one of the run's items")
        if not unmanaged:
            entry = lane_entry(self.tools.ship, self.work, delivered, env=self.env)
            if entry and entry.get("status") != "cancelled" and entry.get("expected_head") == head:
                return self.lane_done(entry, delivered)
        try:
            title = self.title.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):  # missing or not UTF-8: refused just below
            title = ""
        if not title or len(title) > TITLE_MAX or "\n" in title:
            return self.finish("blocked", f"the title at {self.title} is missing, not UTF-8 text, or over "
                                          f"{TITLE_MAX} characters")
        errors = self.out("lint.err")
        self.empty(errors, "lint")
        # A foreign repository's docs tree follows none of this pack's rules: only the body is linted there.
        only = ["--body-only"] if unmanaged else []
        if self.child([*self.tools.lint, "--pr-body", str(self.body), *only], cwd=self.work, name="lint") != 0:
            # The lint prints its failures on stderr; its stdout holds only notes (pack sd:6402).
            try:
                failures = [line[5:] for line in errors.read_text(encoding="utf-8").splitlines()
                            if line.startswith("FAIL ")]
            except OSError:
                failures = []
            return self.finish("blocked", f"sd-docs-lint refused the body at {self.body}: "
                                          f"{'; '.join(failures[:3]) or 'no FAIL line'}; see {errors}")
        if unmanaged:
            return self.publish(head, title, delivered)
        out = self.out("enqueue")
        self.empty(out, "enqueue")
        items = [arg for item in delivered for arg in ("--item", str(item))]
        # The run is each delivered item's whole delivery, so it is the last pull request: `--deliver`.
        self.child([*self.tools.ship, "-C", str(self.work), "lane", "enqueue", *items, "--deliver", "--title", title,
                    "--body-file", str(self.body), "--expected-head", head], cwd=self.work, name="enqueue")
        try:
            answer = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            answer = None
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            reason = answer.get("error") if isinstance(answer, dict) else "no JSON answer"
            return self.finish("blocked", f"sd-ship lane enqueue refused: {reason}; see {out}")
        return self.lane_done(answer, delivered)

    def lane_done(self, entry: dict, delivered: list[int]) -> str | dict:
        if build.enqueued(self.db, self.ident, delivered):
            add_note(self.db, delivered[0], "comment",
                     f"Build run {self.ident} enqueued {self.row['branch']} in the lane"
                     f"{' as entry ' + str(entry.get('id')) if entry.get('id') else ''}")
            return "enqueued"
        if self.fresh()["cancel_requested"]:
            return self.finish("blocked", f"a cancel was asked after the lane entry was written; cancel it with "
                                          f"`sd-ship lane cancel {delivered[0]}`")
        raise Lost

    def publish(self, head: str, title: str, delivered: list[int]) -> str | dict:
        """An unmanaged repository's end (pack sd:7868): push the branch, open or find its draft pull request,
        link it on the first delivered task, then `enqueued` with the run's `detail` naming it. No lane, no merge.

        Each step finds its own state on a retry: the push of the same head is a no-op, an open pull request
        for the branch is reused, and the link of the same url changes nothing.
        """
        remote = self.db.execute(f"SELECT remote FROM repo WHERE path IN ({paths.placeholders(paths.keys(self.row['repo']))})",
                                 paths.keys(self.row["repo"])).fetchone()
        repository = build.github_repository(remote["remote"] if remote else None)
        if not repository:
            return self.finish("blocked", "the repository's remote is not on GitHub, so no draft pull request opens")
        branch = self.row["branch"]
        # The owners of a private repository already see work names; a public one, or one whose visibility did
        # not read, is scanned first, as machine-setup guards only public clones (operator ruling, pack sd:7868).
        if self.private(repository):
            self.note(self.lead, f"Build run {self.ident}: {repository} is private, so the "
                                 f"privacy-pattern scan before the push was skipped")
        else:
            leaked = self.leaks(head, title)
            if leaked:
                return self.finish("blocked", f"nothing was pushed: {leaked}")
        if self.child([*self.tools.git, "-C", str(self.work), "push", "-q", "origin", f"{head}:refs/heads/{branch}"],
                      cwd=self.work, name="push") != 0:
            return self.finish("blocked", f"git push of {branch} to origin failed; see {self.out('push.err')}")
        url = self.open_pull(repository, branch, title, head)
        if isinstance(url, dict):
            return url
        try:
            contributions.configure(self.db, delivered[0], {"pull_url": url}, who="build")
        except WorkflowError as error:
            return self.finish("blocked", f"{url} is open, but linking it on sd:{delivered[0]} failed: {error}")
        if not build.write(self.db, self.ident, detail=build.DRAFT_PULL + url):
            raise Lost
        if build.enqueued(self.db, self.ident, delivered, held_by=build.DRAFT_PULL + url):
            add_note(self.db, delivered[0], "comment",
                     f"Build run {self.ident} pushed {branch} and opened {build.DRAFT_PULL}{url}; "
                     f"its owners merge it, and the merge closes the delivered items")
            return "enqueued"
        if self.fresh()["cancel_requested"]:
            return self.finish("blocked", f"a cancel was asked after {url} opened; close it on GitHub")
        raise Lost

    def private(self, repository: str) -> bool:
        """True only when `gh repo view` reads `repository` as private; a failed or odd read is False (public)."""
        try:
            done = subprocess.run([*self.tools.gh, "repo", "view", repository, "--json", "isPrivate"],
                                  capture_output=True, text=True, timeout=READ_SECONDS, check=False,
                                  stdin=subprocess.DEVNULL, env=self.env)
            answer = json.loads(done.stdout) if done.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        return isinstance(answer, dict) and answer.get("isPrivate") is True

    def leaks(self, head: str, title: str) -> str:
        """What the privacy-pattern scan refuses in the text a publish sends; "" when it passed.

        It reads the title, the body, and each commit in base..head as `leak-guard.sh check` does: its message,
        the paths it touches and its added lines. A scan that matches, fails or has no pattern is a refusal:
        unknown is not clean before a push to someone else's repository.
        """
        # Bytes throughout, split on LF alone: a file or body that is not UTF-8 is scanned, not a crash, and a CR
        # or form feed inside a line does not cut it in two (review of ac8175265). Patterns match ASCII values.
        try:
            body = self.body.read_bytes()
        except OSError:
            body = b""
        span = f"{self.base()}..{head}"
        # A merge commit's diff is against its first parent, the branch: every byte the merge adds to the
        # branch, a conflict resolution's too, which plain `git log` leaves out (review of 5a5adca64). Beside it
        # come main's bytes, already in the target repository: a match there blocks, it never leaks. `--cc`
        # would show only the resolution, in a combined format this parse does not read. A root commit's diff
        # is forced on, whatever `log.showRoot` says.
        git = [*self.tools.git, "-C", str(self.work), "-c", "core.quotePath=false", "-c", "log.showRoot=true",
               "log", "--no-color", "--no-ext-diff", "--no-renames", "--diff-merges=first-parent"]
        messages = _read_bytes([*git, "--format=%B", span], env=self.env)
        touched = _read_bytes([*git, "--format=", "--name-only", "-z", span], env=self.env)
        diff = _read_bytes([*git, "--format=", "-p", "--unified=0", span], env=self.env)
        # A file's header runs from `diff --git` to its first `@@`: mode, index, rename, `Binary files` and the
        # `---`/`+++` lines are skipped there, and a hunk line is `+`, `-` or `\`, so `+++x` in a hunk is the
        # added line `++x`. Messages and paths come from their own reads, never from this parse.
        added, header = [], False
        for line in diff.split(b"\n"):
            if line.startswith(b"diff --git "):
                header = True
            elif line.startswith(b"@@"):
                header = False
            elif not header and line.startswith(b"+"):
                added.append(line[1:])
        parts = [("the title", [title.encode("utf-8", "surrogateescape")]), ("the body", body.split(b"\n")),
                 ("the commits", [*messages.split(b"\n"), *(name for name in touched.split(b"\0") if name), *added])]
        text = b"".join(line.replace(b"\n", b" ") + b"\n" for _part, lines in parts for line in lines)
        try:
            done = subprocess.run(self.tools.guard, input=text, capture_output=True, timeout=READ_SECONDS,
                                  check=False, env=self.env)
        except (OSError, subprocess.SubprocessError) as error:
            return f"the privacy-pattern scan did not run: {type(error).__name__}"
        if done.returncode == 0:
            return ""
        if done.returncode != 1:
            return (f"the privacy-pattern scan could not check the text (exit {done.returncode}; 3 is no pattern "
                    f"file): {done.stderr.decode('utf-8', 'replace').strip()[-300:]}")
        hits, first = set(), 1
        numbers = {int(word) for word in done.stdout.split() if word.isdigit()}
        for part, lines in parts:
            if any(first <= number < first + len(lines) for number in numbers):
                hits.add(part)
            first += len(lines)
        return f"a privacy pattern matches {', '.join(sorted(hits)) or 'the text'}; replace the value with a synthetic one"

    def open_pull(self, repository: str, branch: str, title: str, head: str) -> str | dict:
        """The open pull request for `branch`, or a new draft one; its url, or the run's blocked end.

        `gh pr list --head` matches the branch name alone, so a fork's pull request with the same name is
        listed too, and is never this run's: only one from `repository` itself counts. That one must target
        the run's base and hold the pushed `head`, or it is not linked (review of b34af431e). GitHub moves
        `headRefOid` a moment after a push, so another head is unread, for the reap's retry to read again.
        """
        listed = _read([*self.tools.gh, "pr", "list", "--repo", repository, "--head", branch, "--state", "open",
                        "--json", "url,isCrossRepository,baseRefName,headRefOid", "--limit", "5"], env=self.env)
        try:
            rows = json.loads(listed)
        except ValueError:
            raise Unread("gh pr list answered no JSON") from None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise Unread("gh pr list gave no list of pull requests")
        if not all(isinstance(row.get("url"), str) and contributions.PULL.fullmatch(row["url"])
                   and isinstance(row.get("isCrossRepository"), bool) for row in rows):
            raise Unread("gh pr list gave no pull-request urls with their head repository")
        own = [row for row in rows if row["isCrossRepository"] is False]
        if len(own) > 1:
            return self.finish("blocked", f"{len(own)} open pull requests use {branch}: "
                                          f"{', '.join(row['url'] for row in own)}")
        if own:
            url, base = own[0]["url"], self.base().removeprefix("origin/")
            if own[0].get("baseRefName") != base:
                return self.finish("blocked", f"{url} uses {branch} but targets {own[0].get('baseRefName')!r}, "
                                              f"not {base!r}; it was not linked: close it or retarget it")
            if own[0].get("headRefOid") != head:
                raise Unread(f"{url} reads head {own[0].get('headRefOid')!r}, not the pushed {head[:12]}")
            return url
        out = self.out("pull")
        self.empty(out, "pull")
        if self.child([*self.tools.gh, "pr", "create", "--repo", repository, "--draft", "--head", branch,
                       "--base", self.base().removeprefix("origin/"), "--title", title, "--body-file", str(self.body)],
                      cwd=self.work, name="pull") != 0:
            return self.finish("blocked", f"gh pr create refused the draft pull request; see {self.out('pull.err')}")
        found = [line.strip() for line in out.read_text(encoding="utf-8").splitlines()
                 if contributions.PULL.fullmatch(line.strip())]
        if not found:
            # Opened, perhaps, with its url unread: the retry's list finds it, so none opens twice.
            raise Unread(f"gh pr create printed no pull-request url; see {out}")
        return found[-1]

    def checking(self) -> str:
        """After an auto-pickup merge: refresh the repository, then the dashboard's health (design, "Auto-pickup").

        A non-zero exit turns auto-pickup off and files an item (`build.health_failed`), then the run cleans up.
        A check that gives no answer raises, so the reap retries it once; a second loss is `_health_unknown`.
        """
        self.enter("checking")
        for argv, name, said in (([*self.tools.sync, "refresh", str(self.repo)], "checking-sync", "repo-sync.sh refresh"),
                                 ([*self.tools.health, "health"], "checking-health", "dashboard.sh health")):
            code = self.child(argv, cwd=self.repo, name=name)
            if code != 0:
                build.health_failed(self.db, self.ident, f"`{said}` exit {code}; output in {self.out(name)}")
                break
        return "cleaning"

    def cleaning(self) -> dict:
        self.enter("cleaning")
        if not self.work.is_dir():
            return self.finish("done", f"worktree {self.work} was already removed")
        try:
            dirty = kept_files(self.tools.git, str(self.work), env=self.env)
        except Unread as unread:
            dirty = f"unread: {unread}"
        if dirty:
            self.note(self.lead, f"Build run {self.ident} kept worktree {self.work}: it is not clean "
                                 f"({dirty.splitlines()[0]})")
            return self.finish("done", f"worktree {self.work} kept: not clean")
        # A session other than this run's builder works in it (pack sd:6024): it stays.
        own = self.fresh()["session_id"]
        held = sessions.describe(sessions.holders(self.db, str(self.work), machine=this_host(),
                                                  exclude=(own,) if own else ()))
        if held:
            self.note(self.lead, f"Build run {self.ident} kept worktree {self.work}: {held}")
            return self.finish("done", f"worktree {self.work} kept: {held}")
        if self.child([*self.tools.git, "-C", str(self.repo), "worktree", "remove", str(self.work)], cwd=self.repo,
                      name="cleaning") != 0:
            self.note(self.lead, f"Build run {self.ident} kept worktree {self.work}: git worktree remove failed")
            return self.finish("done", f"worktree {self.work} kept: removal failed")
        return self.finish("done", f"worktree {self.work} removed")

    # ------------------------------------------------------------ helpers

    def brief(self) -> dict:
        items = [self.lead, *self.others]
        titles = {row["id"]: row["title"] for row in self.db.execute(
            f"SELECT id, title FROM item WHERE id IN ({', '.join('?' for _ in items)})", items)}
        return {"worktree": self.work, "branch": self.row["branch"], "title": self.title, "body": self.body,
                "title_max": TITLE_MAX, "items": "\n".join(f"- sd:{item} {titles.get(item, '')}" for item in items),
                "findings": self.findings(items), "denied": self.denied()}

    def findings(self, items: list[int]) -> str:
        """The Send to builder notes on `items` since the lead's first run, up to this run (pack sd:6020); "" for none.

        A bundle queued again keeps its lead, so this run resumes the branch an earlier run left.
        """
        first = self.db.execute("SELECT MIN(created_at) FROM runner_run WHERE assignment = ?",
                                (self.row["assignment"],)).fetchone()[0]
        notes = self.db.execute(
            f"SELECT item, body FROM note WHERE item IN ({', '.join('?' for _ in items)}) AND body LIKE ? "
            f"AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp, id",
            (*items, build.FINDING + "%", first, self.row["created_at"])).fetchall()
        if not notes:
            return ""
        sent = "\n".join(f"- sd:{note['item']}: {note['body'][len(build.FINDING):].strip()}" for note in notes)
        return (f"\nThe worktree already holds the commits of an earlier run on {self.row['branch']}: build on them.\n"
                f"The operator sent these findings to the branch, oldest first. Fix each one the branch does not\n"
                f"fix yet, fail-first, as a new commit:\n{sent}\n")

    def claude(self, prompt: str, *, session: str | None, then: str, role: str) -> str | dict:
        argv = [*self.tools.claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--allowedTools", *ALLOWED, "--permission-mode", "auto", "--max-turns", str(MAX_TURNS),
                "--model", self.row["model"] or build.OPUS]
        if session:
            argv += ["--resume", session]
        offset = self.start_offset(role)
        self.child(argv, cwd=self.work, name="claude", out=self.log, session=session is None, offset=offset, role=role)
        with open(self.log, "rb") as handle:
            handle.seek(offset)
            lines = handle.read().decode("utf-8", "replace").splitlines()
        verdict = judge(lines, session=session or self.fresh()["session_id"])
        final = verdict.get("final")
        if final is not None:
            fields: dict[str, Any] = {"final": json.dumps(final, sort_keys=True)[:65536]}
            if verdict.get("session_id") and not session:
                fields["session_id"] = verdict["session_id"]
            if not build.write(self.db, self.ident, **fields):
                raise Lost
            self.once("cost", lambda: self.cost(final))
        if verdict["state"] == "pass":
            if isinstance(final, dict) and isinstance(final.get("result"), str) and final["result"].strip():
                self.once("report", lambda: add_note(self.db, self.lead, "comment",
                                                     f"Build run {self.ident} report:\n{final['result'][:8000]}"))
            problem = self.unfinished()
            return self.finish("blocked", problem) if problem else then
        if verdict["state"] == "denied":
            # The first denial at a step is a loss: `_settle` requeues it once, the call in its brief (pack
            # sd:6723). One with no retry left asks the operator.
            if self.losses() < build.RETRIES:
                return self.finish("denied", f"permission denied: {verdict['calls']}"[:2000])
            return self.finish("blocked", verdict["why"], question=self.denial_question(verdict))
        if verdict["state"] == "max_turns":
            return self.finish("error_max_turns", verdict["why"])
        return self.finish("blocked", f"{verdict['why']}; log {self.log}")

    def denial_question(self, verdict: dict) -> str:
        """The question a denial asks, with its options (pack sd:6708).

        A requeue is the pick unless the run before was denied the same tools, so the denial looks
        transient; the same denial twice needs the brief changed.
        """
        before = self.db.execute("SELECT outcome, detail FROM runner_run WHERE assignment = ? AND run < ? "
                                 "ORDER BY run DESC LIMIT 1", (self.row["assignment"], self.row["run"])).fetchone()
        # A run `_settle` requeued after a denial counts as the same denial again (pack sd:6723).
        again = before is not None and (before["outcome"] == "denied" or
                                        (before["outcome"] == "blocked" and before["detail"] == verdict["why"]))
        options = (["Change the brief, then requeue (Recommended)", "Requeue as is"] if again else
                   ["Requeue as is (Recommended)", "Change the brief, then requeue"]) + ["Drop the build"]
        return (f"Build run {self.ident}: the builder was denied {', '.join(verdict['tools'])}"
                f" ({verdict['calls'][:500]}). "
                f"Allow it, or change the brief, then requeue.\n"
                + "\n".join(f"Option: {option}" for option in options))

    def losses(self) -> int:
        """The losses earlier runs of this bundle had at this run's step: `_settle` retries while under `RETRIES`."""
        marks = ", ".join("?" for _ in build.LOSSES)
        return self.db.execute(f"SELECT COUNT(*) FROM runner_run WHERE assignment = ? AND step IS ? AND id != ? "
                               f"AND outcome IN ({marks})",
                               (self.row["assignment"], self.fresh()["step"], self.ident, *build.LOSSES)).fetchone()[0]

    def denied(self) -> str:
        """The call the run before this one was denied, for the brief; "" when it was not denied."""
        row = self.db.execute("SELECT outcome, detail FROM runner_run WHERE assignment = ? AND run < ? "
                              "ORDER BY run DESC LIMIT 1", (self.row["assignment"], self.row["run"])).fetchone()
        if row is None or row["outcome"] != "denied":
            return ""
        return (f"\nThe run before this one lost to a denied call: {row['detail']}. Do that step another way:\n"
                f"write no file outside the worktree, commit with -m or with -F on a file inside it, never amend.\n")

    def cost(self, final: dict) -> None:
        usage = final.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        usd = final.get("total_cost_usd")
        record_cost(self.db, source="run", role="author", repo=self.row["repo"], assignment=self.row["assignment"],
                    pass_=self.ident, owner_pid=os.getpid(),
                    tokens_in=usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else None,
                    tokens_out=usage.get("output_tokens") if isinstance(usage.get("output_tokens"), int) else None,
                    usd=float(usd) if isinstance(usd, (int, float)) else None)

    def unfinished(self) -> str:
        """Why a passing builder's worktree is not done: dirty, or no commit ahead of the base; empty when done."""
        try:
            if _read([*self.tools.git, "-C", str(self.work), "status", "--porcelain"], env=self.env).strip():
                return f"the builder left worktree {self.work} dirty"
            ahead = _read([*self.tools.git, "-C", str(self.work), "rev-list", "--count", f"{self.base()}..HEAD"], env=self.env)
        except Unread as unread:
            return f"the worktree was not read: {unread}"
        return "" if ahead.strip().isdigit() and int(ahead) > 0 else f"the builder left no commit on {self.row['branch']}"

    def base(self) -> str:
        try:
            ref = _read([*self.tools.git, "-C", str(self.repo), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], env=self.env)
        except Unread:
            return "origin/main"
        return ref.strip() or "origin/main"

    def head(self) -> str:
        return _read([*self.tools.git, "-C", str(self.work), "rev-parse", "HEAD"], env=self.env).strip()

    def review_answer(self, out: Path, head: str) -> dict | None:
        """The review's `status` and blockers, for this head; None for an unknown answer."""
        try:
            answer = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(answer, dict):
            return None
        subject = answer.get("subject")
        if isinstance(subject, dict) and subject.get("head") and subject["head"] != head:
            return None
        status = answer.get("status")
        findings = answer.get("findings")
        findings = findings if isinstance(findings, list) else []
        # A route skip (a docs-only change) is the router's deliberate answer; any other skip stays unknown (pack sd:8291).
        if status in ("clean", "advisory") or status == "skipped" and (answer.get("route") or {}).get("tier") == "skip":
            return {"status": status, "blockers": []}
        if status == "blocking":
            return {"status": status, "blockers": [row for row in findings
                                                   if isinstance(row, dict) and row.get("disposition") == "blocking"]}
        if status == "gate_failed":  # the review cleared and the repository's gate failed: the builder fixes it
            checks = (answer.get("check") or {}).get("checks") or []
            return {"status": status, "blockers": [
                {"path": " ".join(map(str, row.get("command") or ["gate"])), "line": "-",
                 "summary": "; ".join(map(str, row.get("failed_steps") or [])) + ": "
                 + str(row.get("failure") or "")[-1500:]}
                for row in checks if isinstance(row, dict) and row.get("exit_code")] or [
                {"path": "gate", "line": "-", "summary": "the repository gate failed; see the review output"}]}
        return None

    @staticmethod
    def summary(blockers: list[dict], limit: int | None = 3) -> str:
        # `summary` is the text of an `sd-review --json` finding record.
        lines = [f"{row.get('path', '?')}:{row.get('line', '?')} {row.get('summary') or ''}".strip() for row in blockers]
        return "; ".join(lines[:limit]) if limit else "\n".join(f"- {line}" for line in lines)

    def out(self, name: str) -> Path:
        return self.logs / f"{self.ident}.{name}.out"

    # -------------------------------------------------------------- children

    def child(self, argv: list[str], *, cwd: Path, name: str, out: Path | None = None, session: bool = False,
              descendants: bool = False, offset: int = 0, role: str | None = None) -> int:
        """Run one child in its own process group, recorded before it runs; its exit code.

        The child waits on a pipe until `builder_pgid` and `builder_start`
        are written, then execs `argv`; a failed write closes the pipe and
        the child exits unrun. While it runs, the supervisor beats, stops at
        a requested cancel or a lost run, and ends it past `timeout`. Its
        group must read gone after it exits, or the run is a `Survivor`.
        The groups its descendants run in (`sd gate run` puts its command in
        one) are ended with it and must read gone too. With `descendants`,
        each is also recorded while it runs, so one that outlives its parent
        is still ended (`ours`). On a replay a child that already ran answers from its record (`replayed`).
        """
        replayed = self.replayed(argv, name, role)
        if replayed is not None:
            return replayed
        target = out or self.out(name)
        read, gate = os.pipe()
        try:
            with open(target, "ab") as sink, open(self.out(name + ".err"), "ab") as errors:
                process = subprocess.Popen([sys.executable, "-c", GATE, str(read), *argv], cwd=cwd,
                                           stdin=subprocess.DEVNULL, stdout=sink, stderr=errors, env=self.env,
                                           pass_fds=(read,), start_new_session=True)
        except OSError:
            os.close(gate)
            raise
        finally:
            os.close(read)
        try:
            try:
                recorded = build.write(self.db, self.ident, builder_pgid=process.pid,
                                       builder_start=self.probe.started(process.pid))
                if recorded:
                    os.write(gate, b"1")
            finally:
                os.close(gate)
        except HUB_FAULTS:
            process.wait()  # its gate closed unwritten: it exits unrun, and the walk starts it again
            raise
        if not recorded:
            process.wait()
            raise Lost
        self.last = {"name": name, "argv": argv, "pid": process.pid, "exit": None, "session": session, "offset": offset,
                     "as": role}
        for mark in CHILD_MARKS:
            self.marks.discard(mark)
            self.unknown.pop(mark, None)
        self.down_since = None
        self.groups = {} if descendants else None
        return self.waited(process, name, session=session, deadline=time.monotonic() + self.timeout)

    def waited(self, process, name: str, *, session: bool, deadline: float) -> int:
        """Wait out one recorded child, beating; its exit code once its group is gone.

        A hub fault leaves the child running: the beat is skipped, as at a held lock, and the next one tries
        again (pack sd:8182). One beat landing clears the outage and reads the run again.
        """
        self.deadline = deadline
        while True:
            try:
                code = process.wait(timeout=min(1.0, self.beat_seconds))
                break
            except subprocess.TimeoutExpired:
                pass
            if self.groups is not None:
                for group, start in (build.descendant_groups(process.pid) or {}).items():
                    self.groups.setdefault(group, start)
                # Only groups still the run's stay: one that ended leaves at this poll, before a pid wrap can
                # give its number to an unrelated group whose EPERM read as a Survivor (sd:7909).
                self.groups = {group: start for group, start in self.groups.items() if self.ours(group, start)}
            try:
                if session and not self.hub_down:
                    self.watch_session()
                self.beat(process)
            except Lost:
                # No write lands any more: the group goes before the supervisor does.
                self.end_group(process.pid, process)
                raise
            except HUB_FAULTS as fault:
                self.fault_seen(fault)
                self.follow_build(fault)  # a build the hub refuses: this image cannot beat, so replace it
            if time.monotonic() >= deadline:
                if not self.end_group(process.pid, process):
                    raise Survivor(f"process group {process.pid} ({name}) still had a process after SIGKILL")
                raise TimedOut
        if self.last is not None:
            self.last["exit"] = code
        if not self.end_group(process.pid, process):
            raise Survivor(f"process group {process.pid} ({name}) still had a process after SIGKILL")
        return code

    def beat(self, process: Any) -> None:
        """Beat when one is due; stop the child's group at a lost run or a requested cancel."""
        if time.monotonic() - self.last_beat < self.beat_seconds:
            return
        self.last_beat = time.monotonic()
        try:
            beaten = build.beat(self.db, self.ident)
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) and "busy" not in str(error):
                raise
            # A lock past every retry skips this beat: the next comes in `beat_seconds`, the reap waits
            # `build.STALE_SECONDS`. Raised, it killed the supervisor beside a live child (pack sd:6741).
            return
        if not beaten:
            raise Lost
        self.hub_down, self.down_since = False, None
        row = self.fresh()
        if row["cancel_requested"] and row["step"] in build.CANCELLABLE:
            if not self.end_group(process.pid, process):
                raise Survivor(f"process group {process.pid} still had a process after SIGKILL at a cancel")
            raise Cancelled

    def watch_session(self) -> None:
        """Write the builder's session id as soon as its `init` line names it, so the room counts it once."""
        if self.fresh()["session_id"] or not self.log.exists():
            return
        with open(self.log, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("type") == "system" and record.get("subtype") == "init":
                    if record.get("session_id") and not build.write(self.db, self.ident,
                                                                    session_id=record["session_id"]):
                        raise Lost
                    return

    def end_group(self, pgid: int, process: Any = None) -> bool:
        """TERM the group and the groups below it, `grace` seconds, then SIGKILL; True once ESRCH answers on each.

        The groups below are read live before each signal (`build.descendant_groups`), so each is one this
        run's processes started; `ours` adds a group the child started that outlived its parent. A `ps` that
        did not answer adds none: the groups known are signalled, and their ESRCH is True, as
        `build.kill_group` reads it. An unread `ps` once set a flag no later read cleared, so a clean
        managed run ended as a survivor (review of b34af431e).
        The leader is reaped as it dies (`process.poll`): a zombie leader is
        still a member of its group, and would read as a survivor.
        """
        groups = {pgid}

        def widen() -> None:
            groups.update(build.descendant_groups(pgid) or ())
            groups.update(group for group, start in (self.groups or {}).items() if self.ours(group, start))

        def gone() -> bool:
            ended = all(self.probe.group(group) is False for group in groups)
            if process is not None:
                process.poll()  # after the read: a leader that died before it reads gone, and is ours to reap
            return ended

        for sent, wait in ((signal.SIGTERM, self.grace), (signal.SIGKILL, 5.0)):
            widen()
            if gone():
                return True
            for group in sorted(groups):
                try:
                    os.killpg(group, sent)
                except OSError:
                    pass  # ESRCH: gone; EPERM may be a group of zombies only (sd:6406): `gone` decides
            stop = time.monotonic() + wait
            while time.monotonic() < stop:
                if gone():
                    return True
                time.sleep(0.05)
        return gone()

    def ours(self, group: int, start: str) -> bool:
        """Whether a group the child started, read with its leader's `start`, is still that group.

        It must have a live member (EPERM is another user's group: never one this run started), and a leader
        that started at `start`, or none: a pid is not reused while a group of that number has a member. So a
        number a reused pid holds now is never signalled (review of pack sd:7868).
        """
        if self.probe.group(group) is not True:
            return False
        leader = self.probe.alive(group)
        return leader is False or (leader is True and bool(start) and self.probe.started(group) == start)


class DraftSupervisor(Supervisor):
    """One draft run's supervisor (pack sd:6361): `drafting`, then `draft.write` or `draft.end`.

    It shares the build supervisor's child, beat and group handling: the
    child's group is recorded before it runs, a lost run ends it, and the
    `timeout` (`draft.BUDGET_MINUTES`) ends a hung one, which the reap
    retries once. The child holds `draft.TOOLS` only, in `dontAsk` mode and
    with no MCP server, so its one way to the database is its reply.
    """

    def __init__(self, connection, ident: str, *, tools: Tools, log_root: Path, environ: dict | None = None,
                 beat_seconds: float = BEAT_SECONDS, timeout: float = draft.BUDGET_MINUTES * 60,
                 grace: float = GRACE_SECONDS, probe=Probe, settle_seconds: float = build.STALE_SECONDS):
        self.db, self.ident, self.tools, self.probe = Patient(connection), ident, tools, probe
        self.beat_seconds, self.timeout, self.grace = beat_seconds, timeout, grace
        self.row = build.run(self.db, ident)
        self.log_root, self.logs = log_root, log_dir(log_root, self.row["repo"])
        self.repo = self.work = paths.expand(self.row["repo"])
        self.log = self.logs / f"{ident}.jsonl"
        self.item = self.db.execute("SELECT item FROM assignment WHERE id = ?",
                                       (self.row["assignment"],)).fetchone()["item"]
        self.last_beat = 0.0
        self.env = _environ(environ)
        self.reviewed: str | None = None
        self.take_resume(settle_seconds)

    def walk(self) -> dict:
        return self.drafting()

    def ended(self) -> dict:
        try:
            return self.walked()
        except (Lost, Cancelled):
            return {"run": self.ident, "outcome": None, "why": "the run ended or moved under this supervisor"}
        except TimedOut:
            return self.finish("timed_out", f"the draft ran past {self.timeout:g} s; its group was ended")
        except Survivor as survivor:
            return self.finish("blocked", str(survivor))

    def finish(self, outcome: str, detail: str, *, question: str | None = None) -> dict:
        self.ending = (outcome, detail, question)
        ended = draft.end(self.db, self.ident, outcome, detail)
        return ended or {"run": self.ident, "outcome": None, "why": "the run ended or moved before its last write"}

    def argv(self) -> list[str]:
        title, body = draft.item_text(self.db, self.item)
        prompt = DRAFT.format(item=self.item, repo=self.row["repo"], title=title, body=body.strip() or "(no body)",
                              questions=draft.QUESTIONS_MAX)
        return [*self.tools.claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--tools", *draft.TOOLS, "--permission-mode", "dontAsk", "--strict-mcp-config",
                "--max-turns", str(draft.MAX_TURNS), "--model", self.row["model"] or build.OPUS]

    def drafting(self) -> dict:
        if not build.write(self.db, self.ident, step="drafting", heartbeat_at=now(),
                           log_path=str(self.log)):
            raise Lost
        self.last_beat = time.monotonic()
        offset = self.start_offset(None)
        self.child(self.argv(), cwd=self.repo, name="claude", out=self.log, session=True, offset=offset)
        with open(self.log, "rb") as handle:
            handle.seek(offset)
            lines = handle.read().decode("utf-8", "replace").splitlines()
        verdict = judge(lines, session=self.fresh()["session_id"])
        final = verdict.get("final")
        if final is not None:
            fields: dict[str, Any] = {"final": json.dumps(final, sort_keys=True)[:65536]}
            if verdict.get("session_id"):
                fields["session_id"] = verdict["session_id"]
            if not build.write(self.db, self.ident, **fields):
                raise Lost
            self.once("cost", lambda: self.cost(final))
        if verdict["state"] != "pass":
            # A turn limit is no loss here: a fresh run has no session to resume, so it blocks as any other end.
            return self.finish("blocked", f"{verdict['why']}; nothing was written; log {self.log}")
        parsed = draft.parse(final.get("result") if isinstance(final, dict) else None)
        if parsed is None:
            return self.finish("blocked", f"the reply has no `## Spec`, or a question short of two Option: "
                                          f"lines or of a recommended one; nothing was written; log {self.log}")
        if not draft.write(self.db, self.ident, *parsed):
            raise Lost
        return {"run": self.ident, "outcome": "done", "questions": len(parsed[1])}
