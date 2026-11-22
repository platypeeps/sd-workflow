"""Fix (sd:3456): one click on a task or a problem report starts a claude session for it in the operator's herdr session.

The operator ruled on 2026-10-12 (a note on sd:3456): Fix on a task starts a builder session for that task, and Fix on
a report (kind `report`) starts a debug and fix session for that report. Tasks, Reports and Today post
`/api/items/<id>/fix` with the item revision they read; `request` checks the body before the server opens a writer, and
`start` runs in the write, in three steps:

1. The item as it is now. Its revision must be the one the page read (`StaleItem`, a 409), so a second click from the
   same page, after the first one's note landed, starts nothing. It must be open, and its checkout a directory: a
   task's checkout is its repo, and a report's is its repo, else this checkout, which holds `local-cron-jobs`, since no
   report names its job's repo.
2. `local-herdr/herdr.sh launch <checkout> sd-<id> <prompt>`: a new herdr workspace with claude in it, given the
   prompt as typed text. The call is an argv, never a shell line, and the prompt is fixed text and the integer id,
   never the item's title. herdr.sh starts a down session first as herdr's headless server (system sd:7847), so a
   503 in herdr.sh's own words means herdr is missing or that server did not start; nothing falls back to a hidden
   process. A later step that fails says what it left: a workspace at
   its shell prompt, or claude without its prompt. No answer within `TIMEOUT` is a 504 that says whether a session
   started is not known. Either failure leaves a comment note on the item that names its cause, `Fix: launch
   failed: ...` or the 504's words (pack sd:7756): a toast fades, and the journal keeps its record elsewhere.
3. A comment note on the item names the session, the pane and the checkout. A note that cannot be written after a
   launch answers 503 naming the pane, so the operator knows a session runs without its record.

`LAUNCHING` holds one launch at a time, so two clicks that cross pass step 1 one after the other: the second then reads
the first one's note in the revision and is refused.

**Grill and Apply answers on a piece** (pack sd:6700). Writing posts `/api/items/<id>/fix` with `{"grill": "ask"}`:
step 2 starts claude on `GRILL`, which reads the draft and writes 5 to 10 question notes on the piece; the author
answers them on Writing's Details through `/api/decisions/answer`. `{"grill": "apply"}` starts claude on `ANSWERS`,
which edits the draft to follow each ruling and leaves the edit uncommitted; it is refused while a question is open or
before one is answered.

**Apply review on a piece** (pack sd:6699). Writing posts `/api/items/<id>/fix` with `{"apply": "adversarial"}` for a
piece whose `adversarial.md` digest stamp names the current draft (pack sd:6977): step 2 then starts claude on `APPLY`,
which edits the draft for each finding or rejects it with a reason, records each with `sd writing reconcile`, and
leaves the edit an uncommitted diff in the piece's repository. It commits, stages and publishes nothing.

**Fix on a Today Now row** (sd:5652). Today posts `/api/now/fix` with a Now row's id for the rows no other command
addresses: a repo ahead, dirty or pinned, the abandoned worktrees, and every dark row but the pull request collector's
(a failed job shows Run now, a pull request Open on GitHub and the pull request collector Re-run collector, each
under its own name, sd:6709). `start_row` reads that source again and launches only while the read still raises the row: a row it no longer
raises is `StaleItem` (409), and a read that failed says whether the row stands is not known (503). A dark row, whole or
in part (sd:5954), is confirmed by Today's own read, `now_screen.document`, which turns each failed read into its dark
row. A repo row starts in the checkout that read names; the rest start in this checkout.
The launch is step 2 above, on `row_prompt`'s fixed text for the row's kind; no fleet value enters the prompt. No note
is written: a Now row has no item, and herdr's workspace `fix-<kind>[-<name>]` is the record.
"""

from __future__ import annotations

import re
import shlex
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from sd_db import build, operations, paths, workflow
from sd_db.config import system_checkout
from sd_db.errors import SdDbError

__all__ = ["ANSWERS", "APPLY", "CHECKOUT", "GRILL", "HERDR", "NOTE", "TIMEOUT", "prompt", "request", "row_prompt", "row_request", "start", "start_row"]

#: The system checkout (`SYSTEM_TOOLS_ROOT`, sd:5987): a report that names no repo is fixed here.
CHECKOUT = system_checkout()
HERDR = CHECKOUT / "local-herdr" / "herdr.sh"
#: `herdr agent start` waits up to 30 s for claude to be ready; the workspace and the prompt take a few more.
TIMEOUT = 90
LAUNCHING = threading.Lock()

BUILD = ("Build sd:{id}. Follow the command pack's builder workflow: read `sd task show {id}`, work in a worktree "
         "outside ~/repos, commit the change with a pull-request body, and enqueue it through the lane "
         "(`sd-ship lane enqueue`).")
#: How a failed launch's answer begins; its note (pack sd:7756) says "launch failed" instead.
FAILED = "Fix failed: "
#: The note step 3 writes; Today's Working now (sd:3466, `active_sessions`) reads the pane back from it.
NOTE = "Fix: started a {session} session (claude) in herdr workspace sd-{item}, pane {pane}, at {where}."
GRILL = ("Grill piece sd:{id} for its author. Read `sd task show {id}` for the piece's path, then the `## Draft` of "
         "its index.md. Write 5 to 10 pointed questions, one question note each: `sd task note {id} '<question>' --kind "
         "question`. Ask about the claims the draft makes without support, the audience it is for, the one point it "
         "makes, and what to cut. Put two or more answers under each question, one per line as `Option: <answer>`, and "
         "mark the one you recommend `(Recommended)`: a question with none is refused (pack sd:6708). Edit nothing: "
         "the author picks or types an answer on the dashboard's Writing page.")
ANSWERS = ("Apply the grill answers to piece sd:{id}. Read `sd task show {id}`: each `Ruling on note #N` decision answers "
           "question note #N. Read the `## Draft` of the piece's index.md and edit it so it follows each answer. Leave "
           "the draft edit an uncommitted diff for the operator to read. Commit nothing, stage nothing and publish nothing.")
APPLY = ("Apply the adversarial review of piece sd:{id}. Read `sd task show {id}` for the piece's path, then its "
         "adversarial.md and the `## Draft` of its index.md. Check each finding at its source: where it holds, edit the "
         "draft; where it does not, reject it with a reason. Write what became of each finding, applied or rejected with "
         "its reason, to a note file and record it with `sd writing reconcile --piece <year/slug> --artifact adversarial "
         "--note-file <file>`. Leave the draft edit an uncommitted diff for the operator to read. Commit nothing, stage "
         "nothing and publish nothing.")
DEBUG = ("Use the sd-debug skill on report sd:{id}: read `sd task show {id}` and the job's log, find the root cause "
         "and fix it. Acknowledge the report (`sd reports acknowledge {id}`) only after a clean run.")


#: The Now rows Fix starts a session for, by kind (sd:5652). The session starts in the row's checkout, so no fleet value
#: enters the text; `{checkout}` is the system checkout, `{pack}` the pack checkout that holds the dashboard (sd:5987),
#: and `{source}` one of `DARK`.
ROW = {
    "ahead": ("Today's Now says this checkout has commits its remote does not have. Read `git status` and the unpushed "
              "commits, then land them as this repository's CLAUDE.md says: a branch and a pull request through the lane, "
              "never a push to the default branch. If they should not land, say why and stop."),
    "dirty": ("Today's Now says this checkout has uncommitted files. Read `git status` and `git diff`, find the work item "
              "each change belongs to, and land it as this repository's CLAUDE.md says. Discard nothing: ask about a "
              "change you cannot place."),
    "pinned": ("Today's Now says this checkout is pinned behind its origin, so merged commits are not live here. Run "
               "`{checkout}/local-repo-sync/repo-sync.sh refresh`, check that the pin moved, and report what the refresh said."),
    "worktree": ("Today's Now counts worktrees registered in a checkout's .git/worktrees whose directory is gone. Find each "
                 "in the checkouts {pack}/dashboard/sd_dashboard/fleet.py reads (`git worktree list --porcelain` "
                 "marks it prunable), run `git worktree prune -v` in each, and report what was pruned. Change nothing else."),
    "dark": ("Today's Now says its {source} collector could not be read, in whole or in part. Find why: read "
             "{pack}/dashboard/sd_dashboard/now_screen.py and the collector it calls, reproduce the failure, fix "
             "the cause, and land the fix through the lane."),
}
#: The dark rows a session is for, one per source Today lamps, and the flaky heads' partial lane row (pack sd:7706); the
#: pull request collector's Fix is its sync, in the page.
DARK = ("repos", "sessions", "jobs", "lane", "gate", "reports", "mail", "live", "flaky")
ROW_ID = re.compile(r"(?:(ahead|dirty|pinned):(.+):[1-9][0-9]{0,8}|worktrees:[1-9][0-9]{0,8}|dark:(%s))" % "|".join(DARK))
#: What a source's read may raise, as `now_screen.document` guards each one.
UNREAD = (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error)


def row_prompt(kind: str, *, source: str = "") -> str:
    """The text a Now row's session starts on: fixed per kind."""
    from . import pack_library

    return ROW[kind].format(checkout=CHECKOUT, pack=pack_library().parent, source=source)


def prompt(kind: str, item: int, *, grill: str | None = None, apply: bool = False) -> str:
    """The text the new session starts on: a piece's grill step or Apply review, a debug session for a report, else a builder."""
    text = {"ask": GRILL, "apply": ANSWERS}.get(grill) or (APPLY if apply else DEBUG if kind == "report" else BUILD)
    return text.format(id=int(item))


def checkout(row: dict) -> Path:
    """The directory the session starts in, or `WorkflowError` naming why there is none."""
    if row["repo"]:
        directory = paths.expand(row["repo"])
    elif row["kind"] == "report":
        directory = CHECKOUT
    else:
        raise workflow.WorkflowError(f"item {row['id']} names no repository, so Fix has no checkout to start in; set its repo first")
    if not directory.is_dir():
        raise workflow.WorkflowError(f"{row['repo']} is not a directory on this machine, so Fix has no checkout to start in")
    return directory


def start(connection, item: int, *, expected_revision: str, who: str, grill: str | None = None, apply: bool = False):
    """Steps 1 to 3 above: the item's state with the note, and `fix`; or `(status, {"error"})` when no session is known."""
    with LAUNCHING:
        state = workflow.item_state(connection, item)
        if state["revision"] != expected_revision:
            raise workflow.StaleItem(f"item {item} changed; reload it before applying this change")
        row = state["item"]
        if row["status"] == "done":
            raise workflow.WorkflowError(f"item {item} is done; Fix starts a session only for an open item")
        if row["kind"] == "report" and row["source"] == "brief":
            raise workflow.WorkflowError(f"item {item} is a routine brief; Fix starts no debug session for one (sd:3459)")
        if grill:
            _grillable(row, state["notes"], grill)
        if apply:
            _reviewed(connection, row)
        directory = checkout(row)
        session = ({"ask": "grill", "apply": "apply-answers"}.get(grill)
                   or ("apply-review" if apply else "debug" if row["kind"] == "report" else "builder"))
        # A builder session builds the item, so it waits as the Build press does (pack sd:9570).
        if session == "builder" and (waiting := build.waiting_on(connection, row)):
            raise workflow.WorkflowError(f"item {item} {waiting}; Fix starts no builder until it is done")
        pane = _launch(directory, f"sd-{item}", prompt(row["kind"], item, grill=grill, apply=apply))
        if isinstance(pane, tuple):
            return _noted(connection, item, who, *pane)
        where = paths.key(str(directory))
        body = NOTE.format(session=session, item=item, pane=pane, where=where)
        try:
            state = workflow.add_item_note(connection, item, kind="comment", who=who, body=body)
        except (SdDbError, sqlite3.Error) as problem:
            return 503, {"error": f"claude started in herdr pane {pane}, but the note was not recorded: {problem}. "
                                  f"Record it with: sd task note {item} {shlex.quote(body)}"}
        state["fix"] = {"session": session, "pane": pane, "checkout": where}
        return state


def _noted(connection, item: int, who: str, status: int, answer: dict):
    """A failed launch's answer, after a comment note on the item names its cause (pack sd:7756): the page's toast
    fades, and the journal keeps its record on the holder item, so without the note the item said nothing. The answer
    says where the cause is kept, or that no note holds it."""
    said = answer["error"]
    cause = f"launch failed: {said.removeprefix(FAILED)}" if said.startswith(FAILED) else said
    try:
        workflow.add_item_note(connection, item, kind="comment", who=who, body=f"Fix: {cause}")
    except (SdDbError, sqlite3.Error) as problem:
        return status, {"error": f"{said.rstrip('.')}. The item has no note of it: {problem}"}
    return status, {"error": f"{said.rstrip('.')}. A note on sd:{item} keeps the cause."}


def _grillable(row: dict, notes: list[dict], grill: str) -> None:
    """A grill step's item: a piece, and for `apply` no open question and one ruling; else `WorkflowError` saying why."""
    if row["kind"] != "idea" or row.get("piece") is None:
        raise workflow.WorkflowError(f"item {row['id']} is not a writing piece; Grill asks about a piece's draft")
    if grill != "apply":
        return
    asked = sum(1 for note in notes if note["kind"] == "question" and note["resolved_at"] is None)
    if asked:
        raise workflow.WorkflowError(f"{asked} grill question{'s are' if asked > 1 else ' is'} open; answer them first")
    if not any(note["kind"] == "decision" and note["body"].startswith("Ruling on note #") for note in notes):
        raise workflow.WorkflowError("no grill question is answered yet; answer one grill question first")


def _reviewed(connection, row: dict) -> None:
    """Apply review's item: a piece whose `adversarial.md` digest stamp names the current draft (pack sd:6977), else
    `WorkflowError` naming its state: missing, unstamped or stale."""
    from sd_db import writing

    if row["kind"] != "idea" or row.get("piece") is None:
        raise workflow.WorkflowError(f"item {row['id']} is not a writing piece; Apply review applies a piece's review")
    report = writing.piece_state(connection, row["id"])["writing"]["gates"]["companions"].get("adversarial")
    if not report or report["state"] != "current":
        said = f"{report['state']}: {report['detail']}" if report else "not readable"
        raise workflow.WorkflowError(f"{row['piece']} adversarial.md is {said}; run Refresh review first")


def _launch(directory: Path, label: str, text: str):
    """Step 2: `herdr.sh launch`, answered as the pane, or `(status, {"error"})` when no session is known."""
    try:
        done = subprocess.run([str(HERDR), "launch", str(directory), label, text],
                              capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return 504, {"error": f"herdr.sh launch gave no answer in {TIMEOUT} s, so whether a session started is not "
                              f"known. Look for workspace {label} in herdr before you press Fix again."}
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}").splitlines()[-1]
        # herdr.sh's line says what is left: nothing, a workspace at its prompt, or claude without the prompt.
        return 503, {"error": f"{FAILED}{said}"}
    found = re.match(r"launched (\S+): ", done.stdout.strip())
    return found[1] if found else "unknown"


def _row(connection, row: str, fleet, jobs):
    """The row's checkout, workspace label and prompt, from a read of its source now; see the module's last part."""
    # now_screen reads active_sessions, which reads this module's NOTE
    from . import now_screen

    found = ROW_ID.fullmatch(row)
    kind, name, source = found[1] or ("worktree" if row.startswith("worktrees:") else "dark"), found[2], found[3]
    if kind == "dark":
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            today = now_screen.document(connection, now=now, fleet=fleet, jobs=jobs or operations.LaunchdBackend())
        except UNREAD as problem:
            return 503, {"error": f"Today was not read, so whether {row} is still a Now row is not known: {problem}"}
        # A row snoozed since the page read is still raised: the snooze hides it, it does not mend it.
        if not any(raised["id"] == row and raised["kind"] == "dark" for raised in [*today["needs"], *today["snoozed"]]):
            raise workflow.StaleItem(f"the {source} source reads now, so {row} is not a Now row; read Now again")
        return CHECKOUT, f"fix-dark-{source}", row_prompt("dark", source=source)
    area = "sessions" if kind == "worktree" else "repos"
    try:
        document = fleet(area)
        rows = (now_screen._sessions if kind == "worktree" else now_screen._repos)(lambda _: document)
    except UNREAD as problem:
        return 503, {"error": f"the {area} were not read, so whether {row} is still a Now row is not known: {problem}"}
    if not any(raised["id"] == row for raised in rows):
        raise workflow.StaleItem(f"{row} is not a Now row any more; read Now again")
    if kind == "worktree":
        return CHECKOUT, "fix-worktrees", row_prompt(kind)
    # A repo row's id is its name and count, and two checkouts in two groups can share a name.
    named = [repo for repo in document["repos"] if any(raised["id"] == row for raised in now_screen.backbone_rows([repo]))]
    if len(named) != 1:
        raise workflow.WorkflowError(f"{len(named)} checkouts are named {name}, so Fix cannot tell which one {row} is")
    directory = Path(str(named[0].get("path") or ""))
    if not directory.is_absolute() or not directory.is_dir():
        raise workflow.WorkflowError(f"{named[0].get('path')} is not a directory on this machine, so Fix has no checkout to start in")
    return directory, f"fix-{kind}-{name}", row_prompt(kind)


def start_row(connection, row: str, *, fleet=None, jobs=None):
    """A session on a Now row that its source raises now: `{"fix"}`, or `(status, {"error"})` when none is known."""
    from . import fleet as fleet_module

    with LAUNCHING:
        found = _row(connection, row, fleet or fleet_module.collect, jobs)
        if isinstance(found[1], dict):
            return found
        directory, label, text = found
        pane = _launch(directory, label, text)
        if isinstance(pane, tuple):
            return pane
        return {"fix": {"pane": pane, "checkout": paths.key(str(directory)), "workspace": label}}


def row_request(values: dict, *, fleet=None, jobs=None):
    """`/api/now/fix`'s checks, before the server opens a writer: one Now row id that no other route fixes."""
    if set(values) != {"row"} or not isinstance(values["row"], str) or not ROW_ID.fullmatch(values["row"]):
        raise ValueError("Fix takes one Now row: a repo ahead, dirty or pinned, the abandoned worktrees, or a dark "
                         f"{', '.join(DARK)} row.")
    return lambda connection: start_row(connection, values["row"], fleet=fleet, jobs=jobs)


def request(number: int, values: dict, revision: str):
    """The POST's checks, before the server opens a writer; the write as `action_route` returns one."""
    if values not in ({}, {"grill": "ask"}, {"grill": "apply"}, {"apply": "adversarial"}):
        raise ValueError("Fix takes only the item revision, and on a piece grill: ask or apply, or apply: adversarial.")
    return lambda connection: start(connection, number, expected_revision=revision, who="dashboard", grill=values.get("grill"),
                                    apply="apply" in values)
