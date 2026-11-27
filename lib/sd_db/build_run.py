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
import functools
import hashlib
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
from .writes import add_note, create_item, now, record_cost, record_state

#: A child past this many seconds is ended: TERM to its group, `GRACE_SECONDS`, KILL. The floor; a supervisor
#: reads the budget its claim fixed (`build.budget_minutes`, sd:9109).
TIMEOUT_SECONDS = build.BUDGET_MINUTES * 60
GRACE_SECONDS = 20
BEAT_SECONDS = 30
#: Reviews with blockers before the class pass and park: 2, so a park comes after 3
#: reviews (operator 2026-11-23, sd:10128; sd:10111 had set 4).
REVIEW_ROUNDS = 2
#: Fix rounds past this many blocked reviews run on the strongest model at medium effort (operator
#: 2026-11-23, sd:10192), the brief asking for the blockers' class (pack sd:9264); the park stays.
ESCALATE_AFTER = 1  # with REVIEW_ROUNDS 2, the class-pass fix escalates (sd:10128)
STRONGEST, ESCALATED_EFFORT = "fable", "medium"
#: A rate-limited review waits this long, beating, then asks again, up to `RATE_LIMIT_WAITS` times before it
#: blocks: a quota window passes, and a block parked the item for a manual Retry (sd:9882).
RATE_LIMIT_WAIT_SECONDS = 900
RATE_LIMIT_WAITS = 4
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
#: `bin/sd-review`'s markers of a rate-limited reply, one vocabulary: a test pins this copy to that one.
RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "rate_limit",
    "usage limit",
    "quota exceeded",
    "too many requests",
    "http 429",
    "status 429",
    " 429 ",
)

#: The blocker classes the brief checks a change against (sd:9770): a pattern over one recorded blocker, the
#: checklist line. The brief shows the `CHECKLIST_TOP` that recorded rounds (`build.ROUND_BLOCKERS`) of the last
#: `CHECKLIST_DAYS` days matched most, so the list follows the reviews; a blocker no pattern names counts nowhere.
BLOCKER_CLASSES = (
    (re.compile(r"\b(race|concurren\w*|lock\w*|atomic\w*|toctou|interleav\w*)\b", re.I),
     "two writers or a read-then-write that another process can interleave"),
    (re.compile(r"\b(crash\w*|interrupt\w*|replay\w*|resum\w*|idempoten\w*|partial\w*|half)\b", re.I),
     "a step interrupted halfway: what state it leaves and what recovers it"),
    (re.compile(r"\b(retr(y|ies|ied)|again|twice|duplicate\w*|repeat\w*)\b", re.I),
     "a step run twice: a retry or replay that writes or charges again"),
    (re.compile(r"\b(stale|moved|outdated|newer|older)\b", re.I),
     "a value read once and used after it went stale (a head, a row, a revision)"),
    (re.compile(r"\b(untracked|ignored|uncommitted|staged|dirty)\b", re.I),
     "untracked, ignored or uncommitted files a git check does not see"),
    (re.compile(r"\b(swallow\w*|silent\w*|ignores? (the )?(error|failure|exit)|masks?|hides?)\b", re.I),
     "an error swallowed or reported as success, or as another stage's"),
    (re.compile(r"\b(json|narrow\w*|isinstance|malformed|unvalidated|type)\b", re.I),
     "external input used before its type and shape are checked"),
    (re.compile(r"\b(substring|prefix|suffix|token boundar\w*|startswith|regex|pattern)\b", re.I),
     "names matched by substring or loose pattern, not exactly"),
    (re.compile(r"\b(empty|unicode|non-ascii|escap\w*|quot\w*|newline|markup|long)\b", re.I),
     "input that is empty, long, non-ASCII or carries markup or newlines"),
    (re.compile(r"\b(clock|timestamp\w*|timezone|utc|deadline|timeout\w*)\b", re.I),
     "time: clock skew, time zones, deadlines and timeouts"),
    (re.compile(r"\b(pid|process group|pgid|kill\w*|orphan\w*|child(ren)?|signal\w*)\b", re.I),
     "processes: a kill that misses children or ends another's process"),
    (re.compile(r"\b(no test|untested|test (does not|doesn't|never)|fail-first|not (covered|tested))\b", re.I),
     "a fix or branch with no test that failed before it"),
    (re.compile(r"\b(path|symlink|outside|traversal|home directory)\b", re.I),
     "paths: symlinks, relative paths, and writes outside the intended root"),
)
CHECKLIST_TOP = 10
CHECKLIST_DAYS = 30
#: The columns a failure table's header names, in order (sd:9770).
FAILURE_COLUMNS = ("step", "state", "failure", "recovery", "test")
#: A table's header separator row, `| --- | :-: |`: no data.
TABLE_SEPARATOR = re.compile(r"[\s|:-]+")
#: The rule the precheck names when a state-moving change has no failure table (sd:9584, sd:9770).
FAILURE_TABLE_RULE = ("WORKFLOW.md, Review rounds: a change that moves state in steps carries a failure table "
                      "(step, state moved, failure, recovery, test) in its design.md or the PR body before review "
                      "round 1 (sd:9584)")

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
{findings}{checklist}{denied}
Rules:
- Work only in {worktree}. Read each item with `sd task show N` and the repository's CLAUDE.md first.
- Write no file outside the worktree but the title and body below, not even in /tmp. Commit with
  `git commit -m` or `git commit -F <file inside the worktree>`; add new commits and never amend.
- The dispatcher owns every item's status: never run `sd task status`, never push, never open a pull request.
- Write a test that fails before each fix, then make it pass; skip no test.
- Run tests in the foreground, focused on the modules you touch, 10 minutes or less: give each Bash call
  `timeout: 600000` and never `run_in_background`. Past its timeout (120 s by default) a call moves to
  the background, and no exit wakes you (sd:9183). The lane runs the full gate.
- End the commit that delivers a whole item with a `Closes: sd:N` trailer: an item no trailer names
  stays open and is not delivered. Add no attribution lines.
- Give each later fix-up of a delivered item that item's `Closes: sd:N` trailer again: a bundle drop
  reads whose a commit is from that trailer alone, and refuses a commit that cites no item (sd:10319).
- Keep every commit line, and every body line, at 72 characters or less outside a code fence:
  `sd-ship lane enqueue` refuses a longer one.
- Write the pull-request title, a Conventional Commit of {title_max} characters or less, alone to {title}.
- Write the pull-request body, with `## Summary` and `## Test plan`, to {body}; quote each fail-first line.
  The body is public, so it names no local path (the worktree, a home directory): use repo-relative paths.
  The body carries no `Closes:` line: enqueue takes items by `--item` and refuses one (sd:3704).
- A change that moves state in steps, or adds a parser, a journal or event matching, carries a failure
  table in the body before review: step, state moved, failure, recovery, test (sd:9584).
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

#: A sent finding that rejects a review finding: the lane's prepare refuses its blocker until a disposition is
#: recorded at the reviewed head, so the builder records it (sd:10010). Prepare alone checks it, at the final
#: head its catch-up merge may move (lane review of build/sd-1557).
ADJUDICATE = """A finding that rejects a review finding adds no commit; record its disposition instead, at the
reviewed head: `sd-ship -C {worktree} adjudicate --item {item} --expected-head <HEAD> --json` prints the
template; set each rejected row's `response_disposition` to `rebutted` and `reason` to the ruling's reason,
then record it with the same command plus `--dispositions-file /dev/stdin`, the filled JSON on stdin.
"""

# sd:10903: round 1 is a class pass too: single-finding fixes drew the same class again the next round.
FIX = """The review of branch {branch} found these blockers. Do a class pass, not a single-finding fix: name
the class of each blocker, enumerate every instance of it in this change, from the code, not from the
findings, and fix each one fail-first as a new commit in {worktree}, its item's `Closes: sd:N` trailer on it.
Fix the class within this change's own scope: a new reader, audit or entry kind becomes its own item, not a
commit here (sd:9584). Then update the body at {body}, each instance in its failure table if it has one.
Never push. Your final message is your report: the class, each instance and its test.

{blockers}
"""

# sd:9264: added to each fix brief past ESCALATE_AFTER rounds.
ESCALATED = """
These blockers repeat after {rounds} review rounds. Before you fix them, name the class they share and
enumerate every instance of it in this change, from the code, not from the findings; fix each instance.
"""

# sd:9263: the round after the last, once. The Review rounds rule's class pass, sent instead of another single-finding fix.
CLASS_PASS = """The review of branch {branch} still found blockers after {rounds} rounds. This is the last round
before the build parks, so do one class pass, not another single-finding fix: name the class these blockers
share, enumerate every instance of it from the code, not from the findings, and fix each one fail-first as a
new commit in {worktree}, its item's `Closes: sd:N` trailer on it. Fix the class
within this change's own scope: a new reader, audit or entry kind becomes its own item, not a commit here
(sd:9584). Add each instance to the failure table in the body at
{body}. Never push. Your final message is your report: the class, each instance and its test.

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

DESIGN = """You are the headless lead, run by the build dispatcher (sd:10096). Write the design page for one
design-only item. You hold the read tools only: read the repository in the current directory, its CLAUDE.md
and AGENTS.md, its `templates/design.md` when it has one, and the code the item touches. You write nothing;
the dispatcher writes your reply on the item, and the repository's next code bundle commits it as
`docs/work/<date>-<slug>/design.md`.

The item is sd:{item}, in {repo}:
Title: {title}
{body}

Ask only what would change the design and the repository cannot answer. Keep requirements apart from
assumptions; each acceptance criterion names a check and its result. A design that moves state in steps
carries a failure table: step, state moved, failure, recovery, test.

Your final message is the reply, in exactly this form and nothing else:

## Spec

<the design page's text, its sections as `###` headings>

## Questions

<At most {questions}, only those whose answer changes the design. Each is its text, then two or more lines
that start "Option:", the recommended one first, ending " (Recommended)". Leave the section out when there
is none.>
"""

#: Added to a bundle's brief for each design item it carries (sd:10096).
DESIGNS = """
Each design below is a design item's whole work, written by its design run. Commit it, with the rulings
under it applied, as `docs/work/<date>-<slug>/design.md` in your first commit, `<date>` today's and
`<slug>` from the item's title, with a `Closes: sd:N` trailer for its item. Each design gets a directory
of its own, even when two titles match, and its front matter names its item alone: `item: sd:N`. Change
no word of it but its headings and what a ruling changes.
{designs}
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


#: `sd-review`'s bound on a prior report (its MAX_OUTPUT_BYTES); a larger one it refuses (pack sd:9769).
PRIOR_REPORT_BYTES = 2_000_000


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

    `state` is `pass`, `denied` (with `tools`), `max_turns`,
    `rate_limited`, `failed` or `unknown`. A pass needs a `result` line of subtype `success`, `is_error`
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
    # `sd-review`'s rule for Claude's envelope (pack sd:9907): an error with status 429, or a marker in its text.
    status, reply = final.get("api_error_status"), final.get("result")
    said = reply if isinstance(reply, str) else ""
    if final.get("is_error") is True and (type(status) is int and status == 429
                                          or any(marker in said.lower() for marker in RATE_LIMIT_MARKERS)):
        return {**result, "state": "rate_limited", "why": f"the builder's account is rate limited: {said[:300]}"}
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
    """A short read-only command's stdout; `Unread` when it fails, gives undecodable output or no answer in
    `READ_SECONDS`."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=READ_SECONDS, check=False,
                              stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:  # bytes in no locale encoding: no answer
        raise Unread(f"{argv[0]} gave no answer: {error}") from None
    if done.returncode != 0:
        raise Unread(f"{' '.join(argv[:4])} failed: exit {done.returncode}: {done.stderr.strip()[:200]}")
    return done.stdout


# How far, and over how many lines naming one function, `called_from` walks back toward the changed code;
# past either bound a blocker holds. ponytail: a name-based walk, no call graph; a language-aware one if it misses.
CALL_DEPTH, CALL_HITS = 6, 200


def _grep(argv: list[str], *, env: dict | None = None) -> str:
    """`_read` for `git grep`, whose exit 1 with no output means no match: "" then (pack sd:9769)."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=READ_SECONDS, check=False,
                              stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise Unread(f"{argv[0]} gave no answer: {error}") from None
    if done.returncode == 1 and not done.stdout and not done.stderr:
        return ""
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
    lines = _read([*git, "-C", work, "-c", "core.quotePath=false", "status", "--porcelain", "--ignored",
                   "--untracked-files=all"], env=env)
    return "\n".join(line for line in lines.splitlines() if line.strip() and not (
        # Folder parts only: git ends an ignored folder with "/", so a file of that name is no cache.
        line.startswith("!! ") and DISPOSABLE.intersection(line[3:].strip('"').split("/")[:-1])))


def changed_lines(git: Collection[str], work: str, base: str, head: str, *,
                  env: dict | None = None) -> dict[str, set[int]] | None:
    """Each path `base..head` changes, with the new-side lines its hunks touch, or no lines for a change git
    gives no range (a binary file) or whose mode changes, which stands for the whole file; None when git
    cannot answer.

    A hunk that only deletes names the lines on both sides of where the deletion now sits; a deleted file
    keeps its old path and old lines (pack sd:9769). `---`/`+++` are headers only before a file's first hunk.
    Non-ASCII paths read as themselves; a path git still quotes (a quote, a control character) makes the
    delta unknown.
    """
    try:
        diff = _read([*git, "-C", work, "-c", "core.quotePath=false", "diff", "-U0", "--no-renames", "--no-color",
                      "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/", base, head],
                     env=env)  # git's own lines and prefixes, whatever the operator's diff config says
    except Unread:
        return None
    changed: dict[str, set[int]] = {}
    path = gone = None
    named = ""
    header = deleted = False
    whole: set[str] = set()  # a mode change alters the whole file, whatever hunks it also has
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            header, path, rest = True, None, line[len("diff --git "):]
            half = (len(rest) - 5) // 2  # `a/P b/P`: renames are off, so both sides name one path
            if not (rest.startswith("a/") and rest[2 + half:] == f" b/{rest[2:2 + half]}"):
                return None  # a quoted or unparseable path: the delta is unknown
            named = rest[2:2 + half]
            changed.setdefault(named, set())  # no hunk (mode, binary): the whole file changed
        elif header and line.startswith(("old mode ", "new mode ")):
            whole.add(named)
        elif header and line.startswith(('--- "', '+++ "')):
            return None
        elif header and line.startswith("--- "):
            gone = line[6:].removesuffix("\t") if line.startswith("--- a/") else None  # git's tab after a space
        elif header and line.startswith("+++ "):
            deleted = line == "+++ /dev/null"
            path = gone if deleted else line[6:].removesuffix("\t") if line.startswith("+++ b/") else None
            if path is not None:
                changed.setdefault(path, set())
        elif line.startswith("@@") and path is not None:
            header = False
            hunk = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if hunk:
                at, size = (hunk[1], hunk[2]) if deleted else (hunk[3], hunk[4])
                start, count = int(at), int(size if size is not None else 1)
                changed[path].update(range(max(start, 1), start + max(count, 1) + (count == 0)))
    for name in whole:
        changed[name] = set()
    return changed if changed or not diff.strip() else None  # output naming no file is unknown, not empty


def function_words(git: Collection[str], work: str, base: str, head: str, *,
                   env: dict | None = None) -> tuple[set[str], set[str]] | None:
    """Every identifier in the functions `base..head` changes, each read whole (`--function-context`), and the
    names those lines define, so a finding naming the changed caller, or what it calls, ties to the branch
    (pack sd:9769); None when git cannot answer. A function git cannot bound widens to more lines, never fewer.

    None too for a change outside any function, a hunk that defines no name or a file with no hunk (a config
    value, a module constant, a binary, a mode): a consumer it breaks gives its finding nothing to name.
    ponytail: a language `defined_names` misses (C) holds every blocker; a per-language reader if that costs."""
    try:
        diff = _read([*git, "-C", work, "diff", "-U0", "--function-context", "--no-renames", "--no-color",
                      "--no-ext-diff", "--no-textconv", base, head], env=env)
    except Unread:
        return None
    for section in re.split(r"^diff --git .*$", diff, flags=re.M)[1:]:
        hunks = re.split(r"^@@.*$", section, flags=re.M)[1:]
        if not hunks or any(not any(defined_names(line[1:]) for line in hunk.splitlines() if line[:1] in "+- ")
                            for hunk in hunks):
            return None
    lines = [line for line in diff.splitlines() if line[:1] in "+- " and not line.startswith(("+++", "---"))]
    # Every name on a changed line, and each name a context line defines (`defined_names`).
    defined = {name for line in lines for name in (re.findall(r"[A-Za-z_]\w*", line[1:]) if line[0] in "+-"
                                                   else defined_names(line[1:]))}
    # A quoted literal reads as `"text"`, which no identifier spells: a key an exporter writes ties it to the
    # unchanged importer that reads the same key, a format and no name (sd:10027).
    return set(re.findall(r"[A-Za-z_]\w*", diff)) | literals("\n".join(line[1:] for line in lines)), defined


def literals(text: str) -> set[str]:
    """Each quoted literal of two or more characters in `text`, as `"text"` whatever its quotes (sd:10027).
    ponytail: an apostrophe in prose pairs with the next, so it reads more literals, never fewer."""
    return {f'"{body}"' for _quote, body in re.findall(r"""(["'])((?:(?!\1)[^\\\n]){2,})\1""", text)}


# Decorators that wrap a function and leave it reached by its own name; any other may file it in a table
# (`@register("a")`, `@app.route`) its readers reach by no name, an edge the name walk misses (sd:10027).
WRAPPERS = frozenset({"property", "setter", "getter", "deleter", "staticmethod", "classmethod", "abstractmethod",
                      "cached_property", "cache", "lru_cache", "wraps", "override", "final", "dataclass",
                      "contextmanager", "asynccontextmanager", "total_ordering"})
# What a test decorator files, only a test runner reads, and the gate runs those.
TEST_DECORATORS = frozenset({"mock", "unittest", "pytest"})
# Dunders reached by name through their class (`Table()` reads the class body); any other is called by syntax:
# `table[key]`, `a == b`, `obj.missing` (sd:10027).
NAMED_DUNDERS = frozenset({"__init__", "__new__", "__post_init__"})


def definition(lines: list[str], line: int) -> tuple[int, int] | None:
    """The `(start, end)` indexes of the nearest definition whose lines hold the 1-based `line`: a line below
    a function's end is not its own; None outside every function."""
    for start in range(min(line, len(lines)) - 1, -1, -1):
        if defined_names(lines[start]):
            depth = len(lines[start]) - len(lines[start].lstrip())
            # A line that opens with `)` or `]` closes a bracket (a signature split over lines), not the
            # function. ponytail: a multiline string's column-0 text still ends it; a parser if that costs.
            end = next((index for index in range(start + 1, len(lines)) if lines[index].strip()
                        and not lines[index].lstrip().startswith((")", "]"))
                        and len(lines[index]) - len(lines[index].lstrip()) <= depth), len(lines))
            if line <= end:
                return start, end
    return None


def implicit(lines: list[str], start: int, *, named: Collection[str] = ()) -> bool:
    """Whether the definition at `lines[start]` is reached by no name: a dunder outside `named`, or one under
    a decorator not in `WRAPPERS` (sd:10027). Decorator lines sit above it at its indent; a decorator's
    arguments split over lines sit deeper or close with `)`."""
    if any(name.startswith("__") and name.endswith("__") and name not in named for name in defined_names(lines[start])):
        return True
    depth = len(lines[start]) - len(lines[start].lstrip())
    for text in reversed(lines[:start]):
        indent = len(text) - len(text.lstrip())
        if not text.strip() or indent < depth:
            return False
        if indent > depth or text.lstrip().startswith((")", "]")):
            continue
        if not text.lstrip().startswith("@"):
            return False
        parts = re.findall(r"[A-Za-z_]\w*", re.split(r"[^\w.]", text.lstrip()[1:].lstrip(), maxsplit=1)[0])
        if parts and parts[-1] not in WRAPPERS and parts[0] not in TEST_DECORATORS:
            return True
    return False


def defined_names(line: str) -> set[str]:
    """The names one source line defines, in any common form (pack sd:9769): a keyword (`def`, `function`,
    `fn`, `func`, `class`, `sub`, ...), a declaration or assignment (`const x =`, `x := func`, `x = lambda`,
    `x = (a) =>`), a shell `x() {` or a method `x(a) {`. A form it misses still counts when its line
    changed: `function_words` takes every name on a changed line.
    ponytail: a changed line's comment words count too, so prose holds more blockers, never fewer."""
    keyword = r"\b(?:def|function|func|fn|class|sub|proc|struct|interface|type|module|trait|enum)\s+\*?\s*([A-Za-z_]\w*)"
    assigned = r"\b([A-Za-z_]\w*)\s*(?::=|=(?!=)|:)\s*(?:async\s+)?(?:function\b|func\b|lambda\b|fn\b|\([^()]*\)\s*=>|[A-Za-z_]\w*\s*=>)"
    method = r"^\s*(?:(?:async|static|public|private|protected|export|def)\s+)*([A-Za-z_]\w*)\s*\([^()]*\)\s*\{"
    return {name for pattern in (keyword, assigned, method) for name in re.findall(pattern, line)}


def _has_text(path: Path) -> bool:
    """Whether `path` holds a report: more than whitespace, or a file that exists but does not read (no
    permission, not UTF-8), which the caller must not skip for an older one (lane review of build/sd-1726)."""
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


def instance_files(instances: Any) -> list[str]:
    """The file of each `path:line` in a finding's `instances`, as `sd-review` reads it; a value not a list
    names none (pack sd:9769)."""
    return [re.sub(r":[0-9]+$", "", entry.strip()) for entry in (instances if isinstance(instances, list) else [])
            if isinstance(entry, str) and entry.strip()]


def aliases(text: str) -> set[tuple[str, str]]:
    """Each `(name, alias)` a line binds: `parse as decode` (an import, a `use`) or `decode = parse`.
    ponytail: by line, so prose (`such as x`) binds more names, never fewer; no destructuring."""
    return (set(re.findall(r"\b([A-Za-z_]\w*)\s+as\s+([A-Za-z_]\w*)", text))
            | {(name, alias) for alias, name in re.findall(r"\b([A-Za-z_]\w*)\s*:?=\s*([A-Za-z_]\w*)\s*;?\s*$", text)})


def bound_names(line: str) -> set[str]:
    """The names an assignment at the start of `line` binds: `x = ...`, `a, b = ...`, `x: T = ...`,
    `const x = ...`, `x := ...`, or the name a statement mutates: `x[k] = ...`, `x.update(...)` (pack
    sd:9769). ponytail: any `x.` or `x[` start counts, so a read (`x.get(k)`) binds more, never fewer."""
    found = re.match(r"\s*(?:export\s+)?(?:(?:const|let|var)\s+)?([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)"
                     r"\s*(?::[^=]*)?:?=(?!=)", line)
    if found:
        return set(re.findall(r"[A-Za-z_]\w*", found[1]))
    # A statement that mutates a name binds what it adds: `HANDLERS["a"] = h`, `HANDLERS.update(...)`.
    mutated = re.match(r"\s*([A-Za-z_]\w*)\s*[.[]", line)
    return {mutated[1]} if mutated else set()


def statement(lines: list[str], index: int) -> list[str]:
    """The lines of the statement that holds `lines[index]`: back to the last line no bracket is open at,
    on to the line that closes the brackets left open, so a table written over lines is one statement
    (pack sd:9769). ponytail: brackets inside strings count, which only widens the statement."""
    depths, depth = [], 0
    for text in lines:
        depths.append(depth)
        depth = max(0, depth + sum(text.count(mark) for mark in "([{") - sum(text.count(mark) for mark in ")]}"))
    depths.append(depth)
    if index >= len(lines):
        return []
    start = next(row for row in range(index, -1, -1) if depths[row] == 0)
    end = next((row for row in range(index + 1, len(depths)) if depths[row] == 0), len(lines))
    return lines[start:end]


def callable_words(text: str) -> set[str]:
    """The identifiers a finding names as code: `name(`, a snake or camel name, or a backticked dotted name."""
    words = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", text))
    words |= set(re.findall(r"\b(\w*[A-Za-z]_\w*[A-Za-z0-9]|[a-z][a-z0-9]*[A-Z]\w*)\b", text))
    for span in re.findall(r"`([^`\n]+)`", text):
        words |= set(re.findall(r"[A-Za-z_]\w*", span))
    return words  # no length floor: `go()` names a function as surely as a long name does


# Git's state for an operation a worktree has not finished; each can carry commits the tree does not show.
UNFINISHED = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG")


def unfinished(git: Collection[str], work: str, *, env: dict | None = None) -> list[str]:
    """The `UNFINISHED` names present in `work`, read where Git keeps them for that worktree (`--git-path`)."""
    argv = [*git, "-C", work, "rev-parse", *(arg for name in UNFINISHED for arg in ("--git-path", name))]
    paths = _read(argv, env=env).splitlines()
    return [name for name, path in zip(UNFINISHED, paths, strict=True) if Path(work, path).exists()]


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
            if build.lane_failed(connection, row, reason, entry=entry.get("id")):
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
        build.RESTORING if build.restoration_pending(connection) else "") or build.rate_hold(connection)
    record_state(connection, "heartbeat", key=HEARTBEAT if hub else f"{HEARTBEAT}@{build.this_host()}",
                 body={"pid": os.getpid(), "owner": owner, "why": why,
                       "load": build.over_load(load, load_cap) if not build.unread(load, load_cap, []) else ""})
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
    # One start per tick: the load read before the claim does not see a builder that starts in it, so a quiet
    # load let one tick start a bundle per repository, and the load went to 300 (pack sd:8747). The next tick
    # reads the load again.
    claimed: list[str] = []
    if not why:
        try:
            taken = build.claim(connection, owner=owner, load=load, load_cap=load_cap, interactive=interactive,
                                root=root, hosts=hosts)
        except build.BuildRefused as refusal:
            why, taken = str(refusal), None
        claimed += [taken["id"]] if taken else []
    drafted: list[str] = []
    if not why and hub:
        # A draft or design run takes what machine room the builds leave, and no repository slot, so it claims
        # beside a bundle in the same tick (sd:10096); one runs at a time, so a tick starts at most two.
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


def _signature(stat: os.stat_result) -> tuple[int, int, int]:
    """What a local file's rewrite changes: its inode, size and mtime."""
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def _same_copy(path: Path, target: Path) -> tuple[int, int, int] | None:
    """The signature of `path` when `target` is a finished copy of it, else None: a copy lands only by
    rename, and `copy2` keeps the size and mtime; two seconds of slack for a volume that stores coarse times.

    It stats the bulk root, so only the bounded copier calls it (sd:8826 lane round 1)."""
    try:
        source, copy = path.stat(), target.stat()
    except FileNotFoundError:
        return None
    if copy.st_size == source.st_size and abs(copy.st_mtime - source.st_mtime) <= 2:
        return _signature(source)
    return None


def _released(connection, path: Path) -> bool:
    """Whether every run that owns `path` ended and was released: all of the assignment's for its
    `sd-N.title`, `.body.md` or `.blockers.json` and for a run's `.review.out` or `.prior.out`, which a later
    run of the assignment reads back (`Supervisor.review_files`, pack sd:10236); else the run its name starts with."""
    owner = re.fullmatch(r"sd-(\d+)\.(?:title|body\.md|blockers\.json)", path.name)
    ident = path.name.split(".", 1)[0]
    lead = int(owner[1]) if owner else None
    if path.name in (f"{ident}.review.out", f"{ident}.prior.out"):
        row = connection.execute("SELECT assignment FROM runner_run WHERE id = ?", (ident,)).fetchone()
        lead = row[0] if row else None  # no such run: the query below finds none, and the file stays
    ended = "SELECT outcome IS NOT NULL AND released_at IS NOT NULL FROM runner_run WHERE "
    rows = connection.execute(ended + "assignment = ?", (lead,)) if lead is not None \
        else connection.execute(ended + "id = ?", (ident,))
    released = [row[0] for row in rows]
    # A usage limit releases its run and queues the bundle again: the resume reads these files locally
    # (`Supervisor.round_blockers`, the body), so they wait while the last run is that one (review of build/sd-1557).
    if lead is not None and released:
        last = connection.execute("SELECT outcome FROM runner_run WHERE assignment = ? ORDER BY run DESC LIMIT 1",
                                  (lead,)).fetchone()
        if last is not None and last[0] == build.RATE_LIMITED:
            return False
    return bool(released) and all(released)


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

    `sd-<assignment>.title`, `.body.md` and `.blockers.json` move once every run of that assignment ended and was
    released and its last did not end at a usage limit, which resumes on them, as do a run's `.review.out` and
    `.prior.out`, which a resumed run of the assignment verifies (pack sd:10236); the rest once the run their name starts with was: an outcome alone leaves a blocked or abandoned run's
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
        if not _released(connection, path):
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
    copied: list[tuple[Path, Path, tuple[int, int, int]]] = []

    def copy() -> None:
        try:
            fresh, killed = [], []
            for path, target in pending:
                try:
                    if (signature := _same_copy(path, target)) is not None:
                        copied.append((path, target, signature))
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
                    before = _signature(path.stat())
                    shutil.copy2(path, part)
                    os.replace(part, target)
                    # The source's signature as the copy landed: the tick compares it locally (sd:8826).
                    signature = _same_copy(path, target)
                except OSError:
                    with contextlib.suppress(OSError):
                        part.unlink(missing_ok=True)
                    continue
                if signature == before:
                    copied.append((path, target, before))
                else:  # a source rewritten mid-copy: its torn copy goes, so no later `_same_copy` takes it (sd:8989)
                    with contextlib.suppress(OSError):
                        target.unlink(missing_ok=True)
        finally:
            held.close()

    global _copier
    worker = _copier = threading.Thread(target=copy, name="sd-build-archive", daemon=True)
    worker.start()
    worker.join(deadline)
    moved = []
    # A copy still running past the deadline is not in this snapshot; a later tick collects it.
    for path, target, signature in list(copied):
        old, new = str(path), str(target)
        # A retry may have started and rewritten an assignment's title or body since the scan (sd:8826).
        # The tick reads only the local file: a bulk root stat here ran outside the deadline (lane round 1).
        try:
            current = _signature(path.stat())
        except OSError:
            continue
        if not _released(connection, path) or current != signature:
            continue
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


def _environ(environ: dict | None, ident: str) -> dict:
    # The supervisor's `git -C` calls and the builder start here: a hook's `GIT_DIR` would point them at its own
    # repository, not the run's worktree (pack sd:9709, round 26).
    env = paths.git_env(os.environ if environ is None else environ)
    # Machine hooks tell a headless builder from a session by this (pack sd:9715).
    env["SD_BUILD_RUN"] = ident
    # The token reaches the child's environment only: no row or log holds it.
    if env.get("CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = env["CUSTOM_CLAUDE_CODE_OAUTH_TOKEN"]
    return env


def _checks_environ(env: dict) -> dict:
    """A repo's own checks may fetch private release assets with GITHUB_TOKEN (sd:8506). Only they get it
    from the PAT: elsewhere gh and its credential helper would act as the PAT, not the gh login (sd:8515)."""
    if env.get("GITHUB_TOKEN") or not env.get("GITHUB_PERSONAL_ACCESS_TOKEN"):
        return env
    return {**env, "GITHUB_TOKEN": env["GITHUB_PERSONAL_ACCESS_TOKEN"]}


class Supervisor:
    """One run's supervisor: `run()` walks its steps and returns how it ended."""

    #: While a child that asked for it runs, the groups its descendants started, apart from its own (`sd gate
    #: run` starts its command in a new group), each with its leader's start: `end_group` ends one that
    #: outlived its parent only while it is still that group. None for every other child.
    groups: dict[int, str] | None = None
    #: The files the last `deferred` judged against, the branch's own: a rebuttal lapses once one changes.
    judged: tuple[str, ...] = ()

    def __init__(self, connection, ident: str, *, tools: Tools, log_root: Path,
                 cited: Callable[[Path, str, str, str], Collection[int] | None], environ: dict | None = None,
                 beat_seconds: float = BEAT_SECONDS, timeout: float | None = None, grace: float = GRACE_SECONDS,
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
        self.beat_seconds, self.grace = beat_seconds, grace
        self.row = build.run(self.db, ident)
        claimed = self.row.get("budget_minutes") or build.BUDGET_MINUTES
        self.timeout = max(claimed * 60, TIMEOUT_SECONDS) if timeout is None else timeout
        self.log_root, self.logs = log_root, log_dir(log_root, self.row["repo"])
        self.work = Path(self.row["work_path"])
        self.repo = paths.expand(self.row["repo"])
        self.lead, self.others = _lead_item(self.db, ident)
        lead_id = self.row["assignment"]
        self.title = self.logs / f"sd-{lead_id}.title"
        self.body = self.logs / f"sd-{lead_id}.body.md"
        # The last blocking round's findings whole, for a fix a usage limit interrupted (`walk`).
        self.round_file = self.logs / f"sd-{lead_id}.blockers.json"
        self.log = self.logs / f"{ident}.jsonl"
        self.reviewed: str | None = None
        self.blockers: list[dict] = []
        self.last_beat = 0.0
        self.env = _environ(environ, ident)
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
        elif step == "fixing" and self.fresh()["start_step"] == "fixing":
            # A claim after a usage limit in a fix (`build.claim`): the fix runs again on the round's blockers,
            # its round already counted (review of build/sd-1557). A walk again after a hub fault keeps the ones
            # it read: blockers already held are no sign the fix ran (review of build/sd-1557).
            self.blockers = self.blockers or self.round_blockers()
            if self.blockers:
                steps["fixing"] = self.resume_fix
        # A run that starts past the worktree step (a usage-limit reclaim, a loss retry) is guarded as that step
        # guards a kept worktree first: the checkout may have moved while no run held it (review of build/sd-1557).
        if step in ("building", "fixing", "reviewing") and self.fresh()["start_step"] == step:
            refused = self.guarded()
            if refused is not None:
                return refused
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
            return self.guarded() or "building"
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
        return self.split_first() or "building"

    def guarded(self) -> dict | None:
        """The kept worktree's guard: the blocked end when a subset Retry's split holds it, or it is gone or on
        another branch; None when the builder may work in it."""
        branch = self.row["branch"]
        if not self.work.is_dir():
            return self.finish("blocked", f"worktree {self.work} is gone; the resumed run has no checkout")
        # The guard reads first: a stopped rebase detaches HEAD, and a block here would record no hold.
        held = self.split_first()
        if held:
            return held
        current = _read([*self.tools.git, "-C", str(self.work), "branch", "--show-current"], env=self.env).strip()
        if current != branch:
            return self.finish("blocked", f"worktree {self.work} is on {current or 'no branch'}, not {branch}")
        return None

    def split_first(self) -> dict | None:
        """The blocked end of a run whose bundle a subset Retry left a member out of while its branch or worktree
        held earlier work; None when it held none (pack sd:9581). No message, trailer, revert or merge says which
        change is whose, and a rebase gives a change new ids, so only the first run after the Retry reads the
        branch: no builder ran since, so its start is the Retry's state. Commits past the base or a worktree not
        clean hold the bundle for good (`build.split`); Build then takes its items fresh on a new branch, which
        also never pushes over a tip an unmanaged bundle published. A clean start records its head (`base_head`).
        ponytail: a left item's change in `git stash`, shared across worktrees, goes unread; no builder stashes."""
        state = build.split(self.db, self.row["assignment"])
        if state in ("", "clear"):
            return None
        named = ", ".join(f"sd:{item}" for item, _ in build.left_out(self.db, self.row["assignment"]))
        if state == "unread":
            base, head = self.base(), self.head()
            commits = _read([*self.tools.git, "-C", str(self.work), "rev-list", f"{base}..{head}"],
                            env=self.env).split()
            # Ignored files count too: a left item's local config is work a builder would inherit.
            dirty = kept_files(self.tools.git, str(self.work), env=self.env)
            # A pending merge leaves HEAD and status as they were; the next commit would take its parent.
            pending = unfinished(self.tools.git, str(self.work), env=self.env)
            # Off its branch, HEAD is not the branch's tip: the commits read above say nothing of the branch.
            on = _read([*self.tools.git, "-C", str(self.work), "branch", "--show-current"],
                       env=self.env).strip() == self.row["branch"]
            if on and not commits and not dirty and not pending:
                if not build.write(self.db, self.ident, base_head=head):
                    raise Lost
                return None
        return self.finish("blocked", f"{named} {build.SPLIT}{self.row['branch']} held work from before it, which "
                                      f"may carry that work: Build the kept items to build them fresh on a new "
                                      f"branch (WORKFLOW.md, Parallel work)")

    def building(self) -> str | dict:
        self.enter("building", log_path=str(self.log))
        session = self.fresh()["session_id"]
        if not session:
            # Before the brief reads them: a ruling between the two is one the brief may not carry (sd:10096).
            for item in [self.lead, *self.others]:
                if draft.is_design(self.db, item) and draft.design(self.db, item) is not None:
                    build.note_brief(self.db, item, self.ident)
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
        argv, prior = self.review_plan(head)
        unknown = waits = 0
        while unknown < 2:
            out = self.out("review")
            self.empty(out, "review")
            self.child(argv, cwd=self.work, name="review")
            answer = self.review_answer(out, head)
            # A delta verdict stands for the branch only on a report that covers it (sd:9769): one that covers
            # nothing (another binding, a short round) is no pass, and the branch is reviewed in full.
            # A skipped delta (docs only) read no code: it proves no prior code blocker fixed (sd:9769).
            if answer is not None and not (prior and (answer["status"] == "skipped" or answer["status"] in (
                    "clean", "advisory") and not answer["covers"])):
                break
            if answer is None and self.rate_limited(out):
                if waits >= RATE_LIMIT_WAITS:
                    return self.finish("blocked", f"the review stayed rate_limited after {waits} waits of "
                                                  f"{RATE_LIMIT_WAIT_SECONDS:g} s at {head[:12]}; see {out}")
                waits += 1
                self.note(self.lead, f"Build run {self.ident}: the review is rate_limited at {head[:12]}; "
                                     f"asking again in {RATE_LIMIT_WAIT_SECONDS:g} s ({waits} of {RATE_LIMIT_WAITS})")
                self.pause(RATE_LIMIT_WAIT_SECONDS)
                continue
            unknown += 1
            answer = None
            if prior:  # `sd-review` can refuse a verify plan (input past its bound): retry the branch in full
                argv, prior = argv[:argv.index("--verify-report")], None  # verify args close the argv
        if answer is None:
            return self.finish("blocked", f"the review gave no answer twice at {head[:12]}; see {self.out('review')}")
        # `sd-review` runs no gate in an unmanaged checkout (sd:5953): the repository's own checks run here.
        unmanaged, checked = build.unmanaged(self.db, self.row["repo"]), None
        # A short report, or one `sd-review` cleared `covers_from` on (a binding change), blocks (sd:9769).
        if prior and answer["status"] == "blocking" and answer.get("complete") and answer.get("covers"):
            new, blockers = self.deferred(answer, prior, head)
            if unmanaged and not blockers:  # before any rebuttal: a failed check means a fix round follows
                checked = self.own_checks(head)
            answer = self.file_new_findings(answer, prior, head, rebut=not checked, split=(new, blockers))
            if isinstance(checked, dict):
                return checked
        if answer["status"] in ("clean", "advisory", "skipped"):
            failed = checked if checked is not None else self.own_checks(head) if unmanaged else []
            if isinstance(failed, dict):
                return failed
            if failed:
                return self.blocked_round(failed)
            self.reviewed = head
            return "enqueuing"
        return self.blocked_round(answer["blockers"])

    def review_plan(self, head: str) -> tuple[list[str], dict | None]:
        """This round's `sd-review` argv and the prior report it verifies, if any.

        Pack sd:9769: round 1 reviews the whole branch; a later one verifies the last round's findings over
        the lines changed since its head.
        """
        held = self.held
        if held is not None and held["name"] == "review":
            # A replay after the review wrote its report: that report sits at `head` and would read as a fix
            # with no commit. The held review's own arguments decide, so it answers from its record.
            prior = self.kept_prior() if "--verify-report" in held["argv"] else None
        elif held is not None and held["name"] == "checks":
            # A replay held at an unmanaged repo's checks: the review ran and its report sits at `head`. The
            # prior it verified decides, matched by the digest `sd-review` stamped on that report (sd:9769).
            prior = self.kept_prior()
            if prior is None or self.verified_digest() != hashlib.sha256(
                    json.dumps(prior, sort_keys=True).encode()).hexdigest():
                prior = None
        else:
            prior = self.prior_review(head)
        verify = ["--verify-report", str(self.out("prior")), "--base", prior["subject"]["head"]] if prior else []
        return [*self.tools.review, "-C", str(self.work), "--scope", "branch", "--json", *verify], prior

    def verified_digest(self) -> str | None:
        """The digest of the prior report this round's review verified, from its own report; None if none."""
        try:
            answer = json.loads(self.out("review").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        digest = answer.get("verification_report_digest") if isinstance(answer, dict) else None
        return digest if isinstance(digest, str) else None

    def kept_prior(self) -> dict | None:
        """The prior report `prior_review` kept for this round's verification; None when it does not read."""
        try:
            answer = json.loads(self.out("prior").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return answer if isinstance(answer, dict) and isinstance((answer.get("subject") or {}).get("head"), str) else None

    def prior_review(self, head: str) -> dict | None:
        """The last round's review answer, kept as `prior`, when a fix round followed it; None for round 1.

        Its head must be an ancestor of `head`: the delta since it is what the next round reads (pack sd:9769).
        """
        if not (self.fresh()["review_round"] or 0):
            return None
        last, mine = self.review_files()
        kept = self.out("prior")
        for path in (last, mine):
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                return None  # a report that does not read proves nothing: review the branch in full
            if not text.strip():
                continue  # no last report: the kept one is the last round's
            # A last report that fails a check below falls through to None, never to the older kept report,
            # which omits that round's blockers (an amended fix, two JSON objects after a replay).
            try:
                answer = json.loads(text)
            except ValueError:
                if path == last:
                    return None
                continue
            reviewed = (answer.get("subject") or {}).get("head") if isinstance(answer, dict) else None
            if reviewed == head:  # a fix with no commit: an older report would omit this round's blockers
                return None
            # `sd-review` verifies only these statuses: a `skipped` round has no prior to verify (sd:2721).
            if (not isinstance(reviewed, str) or answer.get("scope") != "branch"
                    or answer.get("status") not in ("clean", "advisory", "blocking", "gate_failed")
                    or not isinstance(answer.get("findings"), list)
                    # A round short of a reader read part of the patch: its delta proves nothing (sd:9769).
                    or not 1 <= int(answer.get("requested_reviews") or 0) <= int(answer.get("completed_reviews") or 0)
                    or answer.get("review_shortfall")
                    # A round that covers no branch (patch-only, another binding): a delta since it leaves the
                    # earlier commits unreviewed (sd:9769).
                    or not answer.get("covers_from")):
                if path == last:
                    return None
                continue
            try:
                _read([*self.tools.git, "-C", str(self.work), "merge-base", "--is-ancestor", reviewed, head],
                      env=self.env)
            except Unread:
                if path == last:
                    return None
                continue
            for row in answer["findings"]:  # `sd-review` refuses an absolute prior path, even inside the checkout
                if isinstance(row, dict) and isinstance(row.get("path"), str) and os.path.isabs(row["path"]):
                    row["path"] = self.locations({"path": row["path"]})[0][0]
            # Each instance's file too: `sd-review` attaches and checks every one (sd:9769).
            if not all(self.verifiable(path, head) for row in answer["findings"]
                       if isinstance(row, dict) and row.get("disposition") == "blocking"
                       for path in (row["path"], *instance_files(row.get("instances")))):
                return None  # `sd-review` would refuse it as --verify-report: review the branch in full
            encoded = json.dumps(answer)
            if len(encoded.encode("utf-8")) > PRIOR_REPORT_BYTES:
                return None  # `sd-review` refuses it before writing any JSON: review the branch in full
            kept.write_text(encoded, encoding="utf-8")
            return answer
        return None

    def review_files(self) -> tuple[Path, Path]:
        """The last and kept review reports: this run's, or, when it holds neither, the latest earlier run's of
        its assignment that does. A usage-limit or loss resume is a new run that keeps the round, so its own
        names hold no review, and the next round would review the branch afresh (pack sd:10236). Only the runs
        it resumed: the walk stops at a fresh claim (`claimed`), whose earlier attempt's review covers another
        start (review of build/sd-1726). The files are read here, under the local root: the archive leaves them
        until every run of the assignment is released (`_released`), and while a rate_limited run is the last.
        So an earlier run that ended otherwise may have lost a report to the archive before this run claimed:
        its prior with no last report beside it, or no report at all, ends the walk with none (lane review of
        build/sd-1726): an older report omits the moved round's blockers."""
        rows = [(self.ident, self.row["start_step"], None), *((row[0], row[1], row[2]) for row in self.db.execute(
            "SELECT id, start_step, outcome FROM runner_run WHERE assignment = ? AND run < ? ORDER BY run DESC",
            (self.row["assignment"], self.row["run"])))]
        for ident, start, outcome in rows:
            pair = (self.logs / f"{ident}.review.out", self.logs / f"{ident}.prior.out")
            earlier = ident != self.ident
            if any(_has_text(path) for path in pair):
                if earlier and not os.path.lexists(pair[0]):
                    break  # its last report moved: the prior left beside it is an older round's
                return pair
            if start in ("claimed", None):  # a row with no start step reads as fresh: fail closed
                break
            if earlier and outcome != build.RATE_LIMITED:
                break  # its reports may be in the archive: a run before it reviewed an older round
        return self.out("review"), self.out("prior")

    def verifiable(self, path: Any, head: str) -> bool:
        """Whether `sd-review --verify-report` takes a prior blocker at `path`: inside the checkout and a
        tracked regular file at `head`, or absent from it (pack sd:9769)."""
        if not isinstance(path, str) or os.path.isabs(path) or ".." in Path(path).parts:
            return False
        if not os.path.realpath(self.work / path).startswith(os.path.realpath(self.work) + os.sep):
            return False
        try:
            entry = _read([*self.tools.git, "-C", str(self.work), "ls-tree", "-z", head, "--", f":(literal){path}"],
                          env=self.env)
        except Unread:
            return False
        if entry:
            return entry.split()[:2] in (["100644", "blob"], ["100755", "blob"])
        return not (os.path.lexists(self.work / path))

    def locations(self, row: dict) -> list[tuple[str, int | None]]:
        """A finding's primary place and each `path:line` instance, paths relative to the worktree (sd:9769)."""
        places: list[tuple[Any, Any]] = [(row.get("path"), row.get("line"))]
        for entry in row.get("instances") or []:
            path, _, line = str(entry).rpartition(":")
            places.append((path, int(line)) if path and line.isascii() and line.isdigit() else (str(entry), None))
        root = os.path.realpath(self.work)
        found = []
        for path, line in places:
            path = str(path or "<unknown>")
            if os.path.isabs(path) and os.path.realpath(path).startswith(root + os.sep):
                path = os.path.relpath(os.path.realpath(path), root)
            found.append((os.path.normpath(path), line if isinstance(line, int) else None))
        return found

    def file_new_findings(self, answer: dict, prior: dict, head: str, *, rebut: bool = True,
                          split: tuple[list[dict], list[dict]] | None = None) -> dict:
        """File each blocker `deferred` names as a follow-up item; the rest still block (pack sd:9769). Each
        follow-up is one transaction under its own mark, so a lost answer stands for that one write only.

        A rebuttal for the lane is written only from a round that ends the review: one that still holds a
        blocker, or whose own checks failed (`rebut` False), is followed by a fix its deferral never judged."""
        new, blockers = split or self.deferred(answer, prior, head)
        rebut = rebut and not blockers
        for index, row in enumerate(new):
            self.once(f"followup:{head}:{index}", functools.partial(self.file_follow_up, row, head, rebut=rebut))
        return {"status": "blocking" if blockers else "advisory", "blockers": blockers}

    def deferred(self, answer: dict, prior: dict, head: str) -> tuple[list[dict], list[dict]]:
        """The blockers on code neither the last round's findings nor the delta since name, at any of its
        places, and the rest (pack sd:9769). A blocker with no file (`<review-response>`) or a delta git
        cannot read stays with the rest."""
        named = {path for row in prior["findings"] if isinstance(row, dict) for path, _ in self.locations(row)}
        changed = self.branch_delta(prior["subject"]["head"], head)
        if (not 1 <= int(prior.get("requested_reviews") or 0) <= int(prior.get("completed_reviews") or 0)
                or prior.get("review_shortfall") or not prior.get("covers_from")):
            # A last round short of a reader, or one that read patches only (no `covers_from`), proves no
            # finding absent: hold every blocker.
            changed = None
        self.judged = tuple(sorted(changed or ()))
        # ponytail: location, not causation: a defect a new caller causes in unchanged code is filed, not held,
        # unless the finding's own words name a changed file. Its `evidence` is among them: a blocker's repro
        # routes the failing input through the change (pack sd:9867), so a dropped check holds (sd:9900).
        # A changed path or its file name; a name shared with an unchanged file holds more, never less.
        names = {name for path in changed or () for name in (path, os.path.basename(path)) if name}
        # The identifiers of the functions the branch changes, read whole: a caller named by function (sd:9769).
        functions = self.branch_words(head) if changed is not None else None

        sources: dict[str, list[str] | None] = {}
        # A change to a function reached by no name (a registered handler, a `__getitem__`) reaches readers no
        # walk finds: every blocker holds (sd:10027).
        if functions is not None and changed and self.implicit_change(changed, head, sources):
            functions = None

        def held(row: dict) -> bool:
            # `<...>` names no file; an absolute or `..` path is outside the checkout no delta of it can clear.
            # A file the branch changes holds at every line: a cited line can be off by a few, and a change can
            # break unchanged code in a file it touches.
            if changed is None or functions is None or any(path.startswith(("<", "/", "../")) or path == ".." or path in named
                                      or path in changed for path, _line in self.locations(row)):
                return True
            # A finding cited at unchanged code that names a changed file (a new caller) is the branch's own.
            # Each path-like word, as `./x`, `a/../x` or an absolute path spells it: whole names, not substrings.
            words = " ".join(str(value) for key, value in row.items() if key not in ("path", "line", "instances"))
            spelled = {os.path.normpath(word.rstrip(".")) for word in re.findall(r"[\w./-]+", words) if word.rstrip(".")}
            if any(word in names or os.path.basename(word) in names for word in spelled):
                return True
            # A function or method the change touches, as `name(`, `a_b`, `camelCase` or `` `x.name` ``.
            # Or a plain word that is the name of a function the change defines (`export`).
            if callable_words(words) & functions[0] or set(re.findall(r"[A-Za-z_]\w*", words)) & functions[1]:
                return True
            # A place inside a function the changed code calls, directly or through unchanged ones: a changed
            # caller can break an unchanged callee (a crash on an empty value) whatever the finding's words
            # name, so its path defers nothing.
            # Or a place whose function calls a changed one, directly or through unchanged ones: a fix that makes
            # `lookup()` return None breaks the unchanged consumer at `lookup().name`.
            # Or a place reached by no name: under a registering decorator, a dunder, or a reader of a quoted key
            # the changed code also spells (sd:10027).
            if any(self.implicit_place(path, line, head, sources, functions[0])
                   or self.called_from(path, line, head, sources, functions[0])
                   or self.calls_into(path, line, head, sources, functions[1]) for path, line in self.locations(row)):
                return True
            # A name with a space or other character no word holds (`bin/my tool`), found literally and whole.
            return any(re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w/-]|\.\w)", words)
                       for name in names if re.search(r"[^\w./-]", name))
        new = [row for row in answer["blockers"] if not held(row)]
        return new, [row for row in answer["blockers"] if row not in new]

    def implicit_place(self, path: str, line: int | None, head: str, sources: dict[str, list[str] | None],
                       words: set[str]) -> bool:
        """Whether the code at `path:line` is reached by no name (`implicit`, any dunder), or its scope quotes a
        literal the changed code quotes too; an answer git cannot give counts as reached (sd:10027)."""
        if (found := self.scope(path, line, head, sources)) is None:
            return True
        lines = sources.get(path) or []
        at = definition(lines, line) if line is not None else None
        return bool(at and implicit(lines, at[0])) or bool(literals("\n".join(found[1])) & words)

    def implicit_change(self, changed: dict[str, set[int]], head: str,
                        sources: dict[str, list[str] | None]) -> bool:
        """Whether a line the branch changes sits in a definition reached by no name, a dunder outside
        `NAMED_DUNDERS` or a registered function; a path with no lines reads whole, one git cannot read
        counts (sd:10027)."""
        for path, numbers in changed.items():
            if (found := self.scope(path, None, head, sources)) is None:
                return True
            lines = found[1]
            starts = ({at[0] for number in numbers if (at := definition(lines, number))} if numbers
                      else {index for index, text in enumerate(lines) if defined_names(text)})
            if any(implicit(lines, start, named=NAMED_DUNDERS) for start in starts):
                return True
        return False

    def called_from(self, path: str, line: int | None, head: str, sources: dict[str, list[str] | None],
                    words: set[str]) -> bool:
        """Whether the function around `path:line` is one the changed code's `words` name, or calls one of
        them through `CALL_DEPTH` unchanged callers at `head`; an answer git cannot give, or a search past
        its bounds, counts as called (pack sd:9769)."""
        frontier, seen = self.enclosing(path, line, head, sources), set[str]()
        for _depth in range(CALL_DEPTH):
            if frontier is None or frontier & words:
                return True
            frontier -= seen
            if not frontier:
                return False
            seen |= frontier
            callers: set[str] = set()
            for name in sorted(frontier):
                hits = self.naming(name, head)
                if hits is None:
                    return True
                for place, number, text in hits:
                    if (found := self.scope(place, number, head, sources)) is None:
                        return True
                    # Outside every function a reference passes on what its statement binds: users of
                    # `HANDLERS = {"a": handler_a}` reach `handler_a` (a missed edge would defer).
                    names = found[0] or {bound for row in found[1][:1] for bound in bound_names(row)}
                    # A line that binds `name` to another (`parse as decode`): callers of the alias call it too.
                    callers |= names | {alias for bound, alias in aliases(text) if bound == name}
            frontier = callers
        return True

    def calls_into(self, path: str, line: int | None, head: str, sources: dict[str, list[str] | None],
                   defined: set[str]) -> bool:
        """Whether the function around `path:line` names one the change `defined`, or reaches one through
        `CALL_DEPTH` unchanged definitions of the names it uses at `head`; an answer git cannot give, or a
        search past its bounds, counts as reaching (pack sd:9769).

        Every name counts, not only `name(`: a function passed as a value (`apply(rows, handler)`), named in
        a string (`getattr(mod, "handler")`) or held in a table is an edge too. A missed edge would defer.
        ponytail: prose and keywords count as names, so this holds more, never fewer."""
        places: list[tuple[str, int | None]] = [(path, line)]
        seen: set[str] = set()
        for _depth in range(CALL_DEPTH):
            called: set[str] = set()
            for place, number in places:
                if (found := self.scope(place, number, head, sources)) is None:
                    return True
                calls = set(re.findall(r"[A-Za-z_]\w*", "\n".join(found[1])))
                # A call through an alias the file binds (`decode()` after `parse as decode`) calls the name too.
                called |= calls | {name for text in sources.get(place) or [] for name, alias in aliases(text)
                                   if alias in calls}
            if called & defined:
                return True
            called -= seen
            if not called:
                return False
            seen |= called
            if len(seen) > CALL_HITS:
                return True
            places = []
            for name in sorted(called):
                hits = self.definitions(name, head)
                if hits is None:
                    return True
                # A definition, or an assignment that binds the name (`HANDLERS = {"a": handler_a}`).
                places += [(place, number) for place, number, text in hits
                           if name in defined_names(text) | bound_names(text)]
        return True

    def definitions(self, name: str, head: str) -> list[tuple[str, int, str]] | None:
        """`naming`, for the lines that may define `name` only: a keyword form, a `name(...) {` method, an
        assignment or a statement that mutates it, so a common name (`self`) is not a search past its bounds (pack sd:9769)."""
        return self.naming(name, head, [
            rf"(def|function|func|fn|class|sub|proc|struct|interface|type|module|trait|enum)[[:space:]]+\*?[[:space:]]*{name}([^[:alnum:]_]|$)",
            rf"(^|[^[:alnum:]_.]){name}[[:space:]]*\([^()]*\)[[:space:]]*\{{",
            rf"(^|[^[:alnum:]_.]){name}[[:space:]]*([,:][^=]*)?=([^=]|$)",
            rf"^[[:space:]]*{name}[[:space:]]*[.[]"])

    def naming(self, name: str, head: str, patterns: list[str] | None = None) -> list[tuple[str, int, str]] | None:
        """Each `(path, line, text)` at `head` that names `name` as a whole word, or matches one of the
        extended `patterns`; None when git cannot say or past `CALL_HITS` lines (pack sd:9769)."""
        search = ["-E", *(part for pattern in patterns for part in ("-e", pattern))] if patterns else ["-w", "-e", name]
        try:  # `head:path\0line\0text` per line
            hits = _grep([*self.tools.git, "-C", str(self.work), "grep", "-n", "-z", "-I", *search, head, "--"],
                         env=self.env).split("\n")
        except Unread:
            return None
        if len(hits) > CALL_HITS + 1:
            return None
        found = []
        for hit in filter(None, hits):  # `split`, not `splitlines`: a form feed is no line end
            place, number, text = (hit.split("\0", 2) + ["", ""])[:3]
            if not place.startswith(f"{head}:") or not number.isascii() or not number.isdigit():
                return None
            found.append((place[len(head) + 1:], int(number), text))
        return found

    def enclosing(self, path: str, line: int | None, head: str,
                  sources: dict[str, list[str] | None]) -> set[str] | None:
        """The names the function around `path:line` at `head` defines, or every name the file defines when the
        place has no line; none for a file `head` does not hold; None when git cannot say (lead ruling on pack
        sd:9769)."""
        found = self.scope(path, line, head, sources)
        return None if found is None else found[0]

    def scope(self, path: str, line: int | None, head: str,
              sources: dict[str, list[str] | None]) -> tuple[set[str], list[str]] | None:
        """`enclosing`'s names and the lines of that function: from its definition to the next line indented no
        deeper, the first that holds the cited line; the whole file for a place with no line, and the cited
        line's whole statement outside every function."""
        if path not in sources:
            try:
                sources[path] = _read([*self.tools.git, "-C", str(self.work), "show", f"{head}:{path}"],
                                      env=self.env).splitlines()
            except Unread:
                try:  # absent at `head` reads as no callee; any other failure is unknown, so the blocker holds
                    absent = not _read([*self.tools.git, "-C", str(self.work), "ls-tree", "-z", head, "--",
                                        f":(literal){path}"], env=self.env)
                except Unread:
                    absent = False
                sources[path] = [] if absent else None
        if (lines := sources[path]) is None:
            return None
        if line is None:
            return {name for text in lines for name in defined_names(text)}, lines
        if (found := definition(lines, line)) is not None:
            return defined_names(lines[found[0]]), lines[found[0]:found[1]]
        return set(), statement(lines, max(line - 1, 0))

    def branch_words(self, head: str) -> tuple[set[str], set[str]] | None:
        """`function_words` of the branch from its fork: its identifiers and defined names; None when unread
        or when the branch changes code outside any function (every blocker holds)."""
        try:
            fork = _read([*self.tools.git, "-C", str(self.work), "merge-base", self.base(), head], env=self.env).strip()
        except Unread:
            return None
        return function_words(self.tools.git, str(self.work), fork, head, env=self.env) if fork else None

    def branch_delta(self, since: str, head: str) -> dict[str, set[int]] | None:
        """The lines the fix changed since `since` together with every line the branch changes from its fork:
        a blocker on either is the change's own, whichever round introduced it (pack sd:9769). None when
        either delta, or the fork, does not read; a path either marks whole stays whole."""
        try:
            fork = _read([*self.tools.git, "-C", str(self.work), "merge-base", self.base(), head], env=self.env).strip()
        except Unread:
            return None
        deltas = [changed_lines(self.tools.git, str(self.work), base, head, env=self.env) for base in (since, fork)]
        if not fork or any(delta is None for delta in deltas):
            return None
        fix, branch = deltas[0] or {}, deltas[1] or {}
        return {path: set() if (path in fix and not fix[path]) or (path in branch and not branch[path])
                else fix.get(path, set()) | branch.get(path, set()) for path in fix.keys() | branch.keys()}

    def file_follow_up(self, row: dict, head: str, *, rebut: bool = True) -> None:
        """One deferred blocker as a follow-up item, its note and its rebuttal, in one transaction (pack sd:9769).

        The lane reviews the branch again; the rebuttal, in the record `sd-review` reads for this branch
        (pack sd:9270), holds back the same finding there and tells its readers where it went."""
        title = f"Review follow-up from build {self.row['branch']}: {row.get('path')}:{row.get('line')}"
        remote = self.db.execute(f"SELECT remote FROM repo WHERE path IN ({paths.placeholders(paths.keys(self.row['repo']))})",
                                 paths.keys(self.row["repo"])).fetchone()
        repository = build.github_repository(remote["remote"] if remote else None).lower()
        with transaction(self.db):
            filed = create_item(self.db, kind="task", title=f"{title} {row.get('summary') or ''}"[:200],
                                repo=self.row["repo"])
            add_note(self.db, filed, "comment", f"Build run {self.ident} round {self.fresh()['review_round'] or 0} "
                                                f"found this on code its fix did not change, so it is filed, not a "
                                                f"blocker (pack sd:9769): {self.summary([row], None)}")
            if repository and rebut:  # no GitHub origin: `sd-review` reads no rebuttal there either
                # Every instance too: the lane drops the rebuttal once any file the finding names changes.
                # And each file the branch changes: a lane gate fix to its own caller lapses it (sd:9769).
                raw = {"path": self.locations(row)[0][0], "summary": row.get("summary"), "line": row.get("line"),
                       "instances": [str(entry) for entry in row.get("instances") or []] + list(self.judged)}
                record_state(self.db, "checkpoint", key=f"ship-adjudication:build-run:{self.ident}:{filed}", body={
                    "decision": "accepted", "proposal": {
                        "bindings": {"repository": repository, "branch": self.row["branch"], "head": head},
                        "findings": [{"raw_finding": raw, "response_disposition": "rebutted",
                                      "reason": f"filed as follow-up sd:{filed}: it is on code the build's fix did "
                                                f"not change (pack sd:9769)"}]}})

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
                              cwd=self.work, name="checks", descendants=True, env=_checks_environ(self.env))
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
        try:
            title, body = (path.read_text(encoding="utf-8") if path.exists() else "" for path in (self.title, self.body))
        except ValueError:  # not UTF-8: the builder rewrites it, as for any other refusal
            return [f"the title at {self.title} or the body at {self.body} is not UTF-8 text"]
        refused = self.checked(self.work, self.base(), head, title.strip(), body) if self.checked else []
        return refused + self.untabled(head, body) + self.undesigned(head, body)

    def undesigned(self, head: str, body: str) -> list[str]:
        """The refusal for a design item the branch closes with no design.md of its own, or none (review of
        sd:10096): two designs with one title closed from one file that kept only one. A design.md the branch
        adds or changes under `docs/work` is the file of the item its front matter's `item: sd:N` line names."""
        designs = [item for item in [self.lead, *self.others] if draft.is_design(self.db, item)]
        cited = self.cited(self.work, self.base(), head, body) if designs else None
        designs = [item for item in designs if item in (cited or ())]
        if not designs:
            return []
        git = [*self.tools.git, "-C", str(self.work)]
        owned = set()
        for path in _read([*git, "diff", "--name-only", "-z", "--diff-filter=d", f"{self.base()}...{head}", "--",
                           "docs/work"], env=self.env).split("\0"):
            if re.fullmatch(r"docs/work/[^/]+/design\.md", path):
                text = _read_bytes([*git, "cat-file", "blob", f"{head}:{path}"], env=self.env).decode("utf-8", "replace")
                # The front matter's `item:` line alone: one in a Markdown example names no owner (review).
                # It ends at the first closing `---` line, an empty one too (class pass).
                lines = text.split("\n")
                front = lines[1:lines.index("---", 1)] if lines[0] == "---" and "---" in lines[1:] else []
                named = next((found for line in front if (found := re.fullmatch(r"item:\s*sd:([0-9]+)\s*", line))),
                             None)
                owned |= {int(named.group(1))} if named else set()
        missing = [item for item in designs if item not in owned]
        if not missing:
            return []
        return [f"each design item the branch closes needs a docs/work/<date>-<slug>/design.md of its own: "
                f"{', '.join(f'sd:{item}' for item in missing)} has none whose first `item:` line names it"]

    def untabled(self, head: str, body: str) -> list[str]:
        """The failure-table refusal for a change that moves state in steps, or none (sd:9770).

        `docs/work/*/design.md` exists only for such a change (docs/work/README.md): the change is one when the
        branch adds or changes a design.md there, or one there names a member (`item: sd:N`); deleting a finished
        one moves no state (review of sd:9770). It passes when each such
        design.md, or the body, holds a failure table (`failure_table`): a header and a data row.
        """
        git = [*self.tools.git, "-C", str(self.work)]
        # NUL-split, so git quotes no path (a non-ASCII one under core.quotePath; lane review of 0e395efb).
        changed = _read([*git, "diff", "--name-only", "-z", "--diff-filter=d", f"{self.base()}...{head}", "--",
                         "docs/work"], env=self.env).split("\0")
        named = re.compile(r"^item:\s*sd:(" + "|".join(str(item) for item in [self.lead, *self.others]) + r")\b", re.M)
        # The committed head, as the clean review worktree holds it: no uncommitted or ignored file counts.
        texts = {}
        for path in _read([*git, "ls-tree", "-r", "-z", "--name-only", head, "--", "docs/work"],
                          env=self.env).split("\0"):
            if re.fullmatch(r"docs/work/[^/]+/design\.md", path):
                text = _read_bytes([*git, "cat-file", "blob", f"{head}:{path}"], env=self.env).decode("utf-8", "replace")
                if path in changed or named.search(text):
                    texts[path] = text
        # Each design needs its own table; the body's covers one it names, or the only one (review of sd:9770).
        bare = [path for path, text in sorted(texts.items()) if not failure_table(text)
                and not (failure_table(body) and (len(texts) == 1 or str(Path(path).parent) in body))]
        if not bare:
            return []
        return [f"this change moves state in steps ({', '.join(bare)}), and neither that design.md nor "
                f"the body holds its failure table: {FAILURE_TABLE_RULE}. Add the table, one row per step"]

    def blocked_round(self, blockers: list[dict]) -> str | dict:
        self.blockers = blockers
        # Whole, as the fix reads them: the lead's note is cut at FINDING_MAX (review of build/sd-1557).
        part = self.round_file.with_name(f".{self.round_file.name}.{os.getpid()}.part")
        part.write_text(json.dumps(blockers))
        os.replace(part, self.round_file)
        # Once per child that blocked: a fault after this write re-walks the held review, which is the same review.
        child = (self.last or {}).get("pid")
        self.once(f"round:{child}", self.count_round)
        # The record the brief's checklist counts (sd:9770).
        self.once(f"blockers:{child}", lambda: add_note(
            self.db, self.lead, "comment",
            (build.ROUND_BLOCKERS + self.summary(blockers, limit=None))[:build.FINDING_MAX]))
        rounds = self.fresh()["review_round"] or 0
        if rounds < REVIEW_ROUNDS:
            return self.fix()
        # One class-pass round past the bound (sd:9263). None when every member waits on a question no fix answers;
        # a question on one member leaves the others to fix, as enqueue delivers them without it.
        unasked = [item for item in [self.lead, *self.others]
                   if not build.waits_on_operator(self.db, item, self.row["asked_since"])]
        if rounds == REVIEW_ROUNDS and unasked:
            return self.fix(CLASS_PASS, rounds=rounds)
        return self.finish("blocked", f"{rounds} review rounds left blockers: {self.summary(self.blockers)}")

    def round_blockers(self) -> list[dict]:
        """The last blocking round's findings as `blocked_round` kept them; [] for none or an unreadable file."""
        try:
            rows = json.loads(self.round_file.read_text())
        except (OSError, ValueError):
            return []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def resume_fix(self) -> str | dict:
        """The interrupted fix again, with its own brief: the last round's is the class pass (`blocked_round`)."""
        rounds = self.fresh()["review_round"] or 0
        return self.fix(CLASS_PASS, rounds=rounds) if rounds == REVIEW_ROUNDS else self.fix()

    def count_round(self) -> None:
        if not build.write(self.db, self.ident, review_round=(self.fresh()["review_round"] or 0) + 1):
            raise Lost

    def fix_role(self) -> str:
        """What this round's fix child starts as: a round's count moves only between two fixes."""
        return f"fix:{self.fresh()['review_round'] or 0}"

    def fix(self, brief: str = FIX, **extra: object) -> str | dict:
        self.enter("fixing")
        session = self.fresh()["session_id"]
        if not session:
            return self.finish("blocked", "no builder session recorded to resume for the fix")
        prompt = brief.format(blockers=self.summary(self.blockers, limit=None), **self.brief(), **extra)
        rounds = self.fresh()["review_round"] or 0
        if rounds <= ESCALATE_AFTER:
            return self.claude(prompt, session=session, then="reviewing", role=self.fix_role())
        self.once("escalated", lambda: self.escalate(rounds))
        return self.claude(prompt + ESCALATED.format(rounds=rounds), session=session, then="reviewing",
                           role=self.fix_role(), model=STRONGEST, effort=ESCALATED_EFFORT)

    def escalate(self, rounds: int) -> None:
        """Record on the lead item that this run's fix rounds now escalate (pack sd:9264).

        A note, not the run's `model`: a retry copies that column, and its first build should not escalate.
        """
        add_note(self.db, self.lead, "comment", f"Build run {self.ident}: fix rounds escalated to {STRONGEST} at "
                                                f"{ESCALATED_EFFORT} effort after {rounds} review rounds with blockers")

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
        # Nor does a design whose ruling was taken back since the claim deliver (class pass of sd:10096).
        asked = [item for item in delivered if build.waits_on_operator(self.db, item, self.row["asked_since"])]
        delivered = [item for item in delivered if item not in asked]
        if not delivered and asked:
            return self.finish("blocked", f"every cited item has an open question since the run started: "
                                          f"{', '.join(f'sd:{item}' for item in asked)}")
        # A member ships with the design it waits on or not at all: one left out above, or with no commit,
        # holds the delivery (class pass of sd:10096).
        left = build.design_left(self.db, delivered, [self.lead, *self.others])
        if left:
            return self.finish("blocked", f"{left}; rule on the design or commit its design.md, then Retry")
        # A design left out so still has its design.md on the branch, written under the old ruling: it would
        # land with the code while its item stays open (review of sd:10096).
        # Done or not: done says nothing of this branch's design.md, which would still land (review of sd:10096).
        stale = sorted(build.aboard(self.db, asked))
        if stale:
            return self.finish("blocked", f"design {', '.join(f'sd:{item}' for item in stale)} waits on the "
                                          f"operator again, and the branch holds its design.md written under the "
                                          f"old ruling; rule on it, then Retry")
        # Nor does a design ship without code: the lane refuses a branch of design records
        # (`design_only_delivery`), and no reader of a delivery expects one with no code item (class pass).
        if delivered and build.aboard(self.db, delivered) == set(delivered):
            return self.finish("blocked", f"the branch delivers only design "
                                          f"{', '.join(f'sd:{item}' for item in delivered)}, and a design ships "
                                          f"with code; settle what held the code items, then Retry")
        # A member the operator cancelled on its own needs no line: the lane reads it from data (`build.left`).
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
                "findings": self.findings(items) + self.designs(items), "checklist": self.checklist(), "denied": self.denied()}

    def designs(self, items: list[int]) -> str:
        """The design and rulings of each design item in `items` (sd:10096); "" for none."""
        found = []
        for item in items:
            text = draft.design(self.db, item) if draft.is_design(self.db, item) else None
            if text is None:
                continue
            rulings = build.design_rulings(self.db, item)
            found.append("\n".join([f"\n- sd:{item}:\n\n{text}", *(f"\n  {ruling}" for ruling in rulings)]))
        return DESIGNS.format(designs="\n".join(found)) if found else ""

    def checklist(self) -> str:
        """The blocker classes recorded review rounds of the last `CHECKLIST_DAYS` found most, top first; "" for
        none (sd:9770). Each recorded blocker counts once per class it matches."""
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - CHECKLIST_DAYS * 86400))
        counts = [0] * len(BLOCKER_CLASSES)
        for (body,) in self.db.execute("SELECT body FROM note WHERE body LIKE ? AND timestamp >= ?",
                                       (build.ROUND_BLOCKERS + "%", since)):
            for line in body[len(build.ROUND_BLOCKERS):].splitlines():
                for at, (pattern, _) in enumerate(BLOCKER_CLASSES):
                    counts[at] += bool(line.strip() and pattern.search(line))
        top = sorted((-count, at) for at, count in enumerate(counts) if count)[:CHECKLIST_TOP]
        if not top:
            return ""
        lines = "\n".join(f"- {BLOCKER_CLASSES[at][1]} ({-count})" for count, at in top)
        return (f"\nBefore the first review, check the change against the blocker classes recorded review rounds\n"
                f"found most in the last {CHECKLIST_DAYS} days, with their counts, and fix what applies:\n{lines}\n")

    def findings(self, items: list[int]) -> str:
        """The Send to builder notes on `items` since the lead's first run, up to this run (pack sd:6020), then the
        review blockers the latest plain Retry noted (`build.REVIEWED`, pack sd:9498, sd:9593), each under its own
        source; "" for none.

        A bundle queued again keeps its lead, so this run resumes the branch an earlier run left. Only a Send to
        builder note is the operator's; a REVIEWED note is the reviewer's text, and the brief says so.
        """
        # Or since the lead was first queued, if that is earlier: a bundle drop notes its finding on a new lead
        # before the lead's first run (round 14). The queue's note keeps that time; `queued_at` moves at each
        # Retry, which lost the finding from the second run on (pack sd:9709, round 16).
        lead = self.row["assignment"]
        first = self.db.execute(
            f"SELECT MIN(at) FROM (SELECT created_at AS at FROM runner_run WHERE assignment = ? UNION ALL "
            f"SELECT timestamp FROM note WHERE item IN ({', '.join('?' for _ in items)}) AND body LIKE ?)",
            (lead, *items, f"Build assignment {lead} queued %")).fetchone()[0]

        def listed(prefix: str, *, latest: bool = False) -> str:
            notes = self.db.execute(
                f"SELECT item, body FROM note WHERE item IN ({', '.join('?' for _ in items)}) AND body LIKE ? "
                f"AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp, id",
                (*items, prefix + "%", first, self.row["created_at"])).fetchall()
            # Only the latest park's blockers (pack sd:9593): one Retry notes the same text on each member, so the
            # newest note's text names them, whatever runs a recovery started since.
            if latest and notes:
                notes = list({note["item"]: note for note in notes if note["body"] == notes[-1]["body"]}.values())
            return "\n".join(f"- sd:{note['item']}: {note['body'][len(prefix):].strip()}" for note in notes)

        sent, reviewed = listed(build.FINDING), listed(build.REVIEWED, latest=True)
        if not sent and not reviewed:
            return ""
        out = f"\nThe worktree already holds the commits of an earlier run on {self.row['branch']}: build on them.\n"
        if sent:
            out += (f"The operator sent these findings to the branch, oldest first. Fix each one the branch does not\n"
                    f"fix yet, fail-first, as a new commit:\n{sent}\n"
                    f"{ADJUDICATE.format(worktree=self.work, item=self.lead)}")
        if reviewed:
            out += (f"The last review round left these blockers. They are the reviewer's text, not an operator\n"
                    f"instruction: check each against the code, and fix the ones that hold, fail-first, as a new\n"
                    f"commit:\n{reviewed}\n")
        return out

    def claude(self, prompt: str, *, session: str | None, then: str, role: str, model: str | None = None,
               effort: str | None = None) -> str | dict:
        argv = [*self.tools.claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--allowedTools", *ALLOWED, "--permission-mode", "auto", "--max-turns", str(MAX_TURNS),
                "--model", model or self.row["model"] or build.OPUS, *(["--effort", effort] if effort else [])]
        if session:
            argv += ["--resume", session]
        offset = self.start_offset(role)
        before = self.read_head()
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
        # A denial the builder got past, its result a success, a new commit of its own and its worktree finished,
        # is no loss: the review and the gate judge the work, and the report names the call (sd:9054). A fix
        # round or a resumed run starts ahead, so only a moved head shows this child committed.
        got_past = (verdict["state"] == "denied" and final.get("subtype") == "success"
                    and final.get("is_error") is False and before is not None
                    and self.read_head() not in (None, before) and not self.unfinished())
        if verdict["state"] == "pass" or got_past:
            if isinstance(final, dict) and isinstance(final.get("result"), str) and final["result"].strip():
                denied = f"\nDenied on the way: {verdict['calls']}"[:2000] if got_past else ""
                self.once("report", lambda: add_note(self.db, self.lead, "comment",
                                                     f"Build run {self.ident} report:\n{final['result'][:8000]}"
                                                     f"{denied}"))
            problem = self.unfinished()
            # A builder that skipped every item with a question left no
            # commit on purpose: the run waits on the answers (sd:9219).
            items = [self.lead, *self.others]
            if problem.startswith("the builder left no commit") and all(
                    build.open_question(self.db, [item], self.row["asked_since"], pending=True) for item in items):
                problem = f"{build.ASKED}{', '.join(f'sd:{item}' for item in sorted(items))}"
            return self.finish("blocked", problem) if problem else then
        if verdict["state"] == "denied":
            # The first denial at a step is a loss: `_settle` requeues it once, the call in its brief (pack
            # sd:6723). One with no retry left asks the operator.
            if self.losses() < build.RETRIES:
                return self.finish("denied", f"permission denied: {verdict['calls']}"[:2000])
            return self.finish("blocked", verdict["why"], question=self.denial_question(verdict))
        if verdict["state"] == "max_turns":
            return self.finish("error_max_turns", verdict["why"])
        if verdict["state"] == "rate_limited":
            return self.finish(build.RATE_LIMITED, f"{verdict['why']}; log {self.log}")
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

    def read_head(self) -> str | None:
        """The worktree's head, or None when it does not read."""
        try:
            return self.head()
        except Unread:
            return None

    def head(self) -> str:
        return _read([*self.tools.git, "-C", str(self.work), "rev-parse", "HEAD"], env=self.env).strip()

    @staticmethod
    def rate_limited(out: Path) -> bool:
        """Whether the review's answer is the provider's quota, which a wait outlasts (sd:9882)."""
        try:
            answer = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(answer, dict) and answer.get("status") == "rate_limited"

    def pause(self, seconds: float) -> None:
        """Wait `seconds`, beating, so the reap keeps the run and a cancel still ends it.
        ponytail: the run holds its slot while it waits; a not-before column on the queue frees it if that matters."""
        stop = time.monotonic() + seconds
        while (left := stop - time.monotonic()) > 0:
            time.sleep(min(self.beat_seconds, left))
            self.enter("reviewing")

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
            return {"status": status, "blockers": [], "covers": bool(answer.get("covers_from"))}
        if status == "blocking":
            return {"status": status, "blockers": [row for row in findings
                                                   if isinstance(row, dict) and row.get("disposition") == "blocking"],
                    "complete": 1 <= int(answer.get("requested_reviews") or 0) <= int(answer.get("completed_reviews") or 0)
                    and not answer.get("review_shortfall"), "covers": bool(answer.get("covers_from"))}
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
        lines = [f"{row.get('path', '?')}:{row.get('line', '?')} {row.get('summary') or ''}".strip()
                 + (f" (class {row.get('family')}; instances: {', '.join(map(str, row['instances']))})"
                    if isinstance(row.get("instances"), list) and row["instances"] else "") for row in blockers]
        return "; ".join(lines[:limit]) if limit else "\n".join(f"- {line}" for line in lines)

    def out(self, name: str) -> Path:
        return self.logs / f"{self.ident}.{name}.out"

    # -------------------------------------------------------------- children

    def child(self, argv: list[str], *, cwd: Path, name: str, out: Path | None = None, session: bool = False,
              descendants: bool = False, offset: int = 0, role: str | None = None, env: dict | None = None) -> int:
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
                                           stdin=subprocess.DEVNULL, stdout=sink, stderr=errors,
                                           env=self.env if env is None else env, pass_fds=(read,), start_new_session=True)
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
        if leader is False:
            return True
        if leader is not True or not start:
            return False
        began = self.probe.started(group)
        # A leader reaped between the two reads answers no start and reads gone at a second look: the group still
        # has a member, so it is the one recorded (a gate flake left a check's `sleep` beside the retry).
        return began == start or (began is None and self.probe.alive(group) is False)


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
        self.env = _environ(environ, ident)
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
        prompt = (DESIGN if draft.is_design(self.db, self.item) else DRAFT).format(item=self.item, repo=self.row["repo"], title=title, body=body.strip() or "(no body)",
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
        if verdict["state"] == "rate_limited":  # queued again, and this host held (sd:9938)
            return self.finish(build.RATE_LIMITED, f"{verdict['why']}; nothing was written; log {self.log}")
        if verdict["state"] != "pass":
            # A turn limit is no loss here: a fresh run has no session to resume, so it blocks as any other end.
            return self.finish("blocked", f"{verdict['why']}; nothing was written; log {self.log}")
        parsed = draft.parse(final.get("result") if isinstance(final, dict) else None)
        if parsed is None:
            return self.finish("blocked", f"the reply has no `## Spec`, or a question short of two Option: "
                                          f"lines or of a recommended one; nothing was written; log {self.log}")
        try:
            if not draft.write(self.db, self.ident, *parsed):
                raise Lost
        except draft.Moved as moved:
            return self.finish("blocked", f"{moved}; log {self.log}")
        return {"run": self.ident, "outcome": "done", "questions": len(parsed[1])}


def failure_table(text: str) -> bool:
    """Whether `text` holds a failure table (sd:9770): a header line whose `|`-separated cells begin with the five
    `FAILURE_COLUMNS` in order, then at least one data row in the lines of `|` right below it, with a cell for
    each column (pack sd:9866). Prose naming the words, or a header with only its separator, is no table."""
    lines = text.splitlines()
    for at, line in enumerate(lines):
        cells = [cell.strip().lower() for cell in line.strip().strip("|").split("|")]
        if len(cells) < len(FAILURE_COLUMNS) or not all(map(str.startswith, cells, FAILURE_COLUMNS)):
            continue
        for row in lines[at + 1:]:
            if "|" not in row:
                break
            if (not TABLE_SEPARATOR.fullmatch(row.strip())
                    and len(row.strip().strip("|").split("|")) >= len(FAILURE_COLUMNS)):
                return True
    return False
