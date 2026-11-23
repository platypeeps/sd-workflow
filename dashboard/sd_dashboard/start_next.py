"""Tasks > Start next (sd:5107): queue a headless build of the next ready items, within the builder and load caps.

`plan` is what the dialog shows before anything is queued. The next items are the `ready` rows of
`reads.backlog_items`, in its order, less reports, items a Fix note says were started already, items a live Claude
session names (`claude_sessions`), items a Build press would refuse (`build._item`: an active assignment, a repository
that is not managed, has no remote or runs its lane elsewhere), and items whose repository is no directory on this Mac;
each one left out is in `skipped` with its reason. The room is the builder cap, `CAP` per machine, less the builders
running now (`claude_sessions.builders`), and none while `sd gate status --json`'s five-minute load is above the
builder cap, `build.load_cap`; the gate's own `load_max` is not it (sd:9094).
A count or a load that could not be read is a refusal with its reason, never room.

`run` is the press, in steps, under `STARTING` (Build step 5, pack sd:6096; design: system
`docs/work/2026-10-24-build-dispatcher/design.md`):

1. A fresh `plan`. A refusal, any item asked for that is no longer in it, or more items than the room: nothing
   is queued, and the answer says why (a 400, `WorkflowError`).
2. `build.queue` with the revisions the dialog read: one queued build assignment per item, in one transaction, or
   none and a 409 naming the item it refused. A queued item has an active assignment, so a second run's plan leaves
   it out (step 1).
3. `builds.kickstart`, so the dispatcher claims in seconds. A kickstart that fails only says so; the job's next
   slot claims the rows. Herdr starts nothing here: Fix alone launches a session (operator, note #29486).
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import threading
from datetime import datetime, timezone

from sd_db import build, paths, reads, workflow

from . import builds, claude_sessions, fix_session
from .active_sessions import Unread, _reason, elapsed
from .tasks_screen import is_scheduled

__all__ = ["CAP", "plan", "request", "run"]

#: Code-writing builders per machine: the operator's standing rule (at most 6 at once).
CAP = 6
GATE_SECONDS = 15
#: One run at a time, so two dialogs cannot both spend the same room.
STARTING = threading.Lock()
UNREAD = claude_sessions.UNREAD


def gate_status() -> dict:
    """`sd gate status --json`: the load averages."""
    sd = shutil.which("sd")
    if sd is None:
        raise Unread("sd is not on PATH, so the load was not read")
    try:
        done = subprocess.run([sd, "gate", "status", "--json"], capture_output=True, text=True, timeout=GATE_SECONDS,
                              check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Unread(f"sd gate status gave no answer in {GATE_SECONDS} s") from None
    if done.returncode != 0:
        raise Unread(f"sd gate status failed: exit {done.returncode}")
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise Unread("sd gate status answered no JSON") from None


def _load(gate) -> tuple[float, float]:
    answer = (gate or gate_status)()
    load, cap = answer.get("load") if isinstance(answer, dict) else None, build.load_cap()
    # NaN compares false with everything, so a NaN load would never be above the cap: it is unread, like inf.
    if not (isinstance(load, list) and len(load) >= 2 and all(isinstance(x, (int, float)) and math.isfinite(x) for x in load[:2])
            and isinstance(cap, (int, float)) and math.isfinite(cap)):
        raise Unread("sd gate status gave no load or load cap")
    return build.counted(answer, float(load[1])), float(cap)


def _holders(holders: list[dict]) -> str:
    """The sessions holding the slots, by state, stalled first: `. Stalled: traces-research b4211 (3h); …`."""
    if not holders:
        return ""
    groups: dict[str, list[str]] = {}
    for holder in holders:
        name = f"{holder['repo']} {holder['agent'] or 'Fix session ' + holder['session']}"
        age = f" ({elapsed(holder['age'])})" if isinstance(holder.get("age"), int) else ""
        groups.setdefault(holder["state"], []).append(name + age)
    return "".join(f". {state.capitalize()}: {', '.join(names)}" for state, names in groups.items()) + \
        ". End a session in Management > Sessions to free its slots"


def plan(connection, *, now: str, sessions=None, gate=None) -> dict:
    """What Start next would queue now: `start`, the items in order with their revisions; `room`; `refusal`.

    `sessions` is `claude_sessions.document` and `gate` is `gate_status`, the seams a test fills.
    """
    claude = (sessions or claude_sessions.document)(connection, now=now)
    running, why = claude["builders"]["running"], claude["builders"]["why"]
    named = {task["item"] for row in claude["sessions"] or [] for task in row["tasks"]}
    launched = {row["item"] for row in reads.launch_notes(connection, fix_session.NOTE.split("{", 1)[0])}
    eligible: list[dict] = []
    skipped: list[dict] = []
    for row in reads.backlog_items(connection, status="ready", now=now):
        if row["kind"] == "report" or is_scheduled(row):
            continue
        reason = ("Fix started a session for it already" if row["id"] in launched
                  else "a live Claude session works on it" if row["id"] in named else "")
        if not reason:
            try:
                build._item(connection, row["id"])  # the press's own check, so step 2 never refuses what step 1 offered
            except build.BuildRefused as problem:
                reason = str(problem)
        if not reason and not paths.expand(row["repo"]).is_dir():
            reason = f"{row['repo']} is not a directory on this machine, so a build has no checkout to start from"
        (skipped if reason else eligible).append({"id": row["id"], "title": row["title"], "repo": row["repo"]}
                                                 | ({"why": reason} if reason else {}))
    revisions = reads.revisions(connection, [entry["id"] for entry in eligible]) if eligible else {}
    for entry in eligible:
        entry["revision"] = revisions[entry["id"]]
    load = load_max = None
    refusal = ""
    try:
        load, load_max = _load(gate)
    except UNREAD as failure:
        refusal = f"the load was not read: {_reason(failure)}"
    if running is None:
        refusal = refusal or f"the running builders were not counted: {why}"
    elif running >= CAP:
        refusal = refusal or f"{running} builders run already; the cap is {CAP}" + _holders(claude["builders"].get("holders") or [])
    if load is not None and load_max is not None:
        refusal = refusal or build.over_load(load, load_max)
    if not eligible and not refusal:
        refusal = "no ready item is left to start"
    room = 0 if refusal else min(CAP - running, len(eligible))
    return {"read": now, "cap": CAP, "running": running, "load": load, "load_max": load_max, "room": room,
            "refusal": refusal, "start": eligible[:room], "eligible": eligible, "skipped": skipped,
            "holders": claude["builders"].get("holders") or [] if running is not None and running >= CAP else []}


def run(connection, *, now: str, items: list[dict], who: str, sessions=None, gate=None, start=None):
    """Steps 1 to 3 above: `queued`, each item's build assignment, and `kickstart`; or a 409 when the queue refused."""
    with STARTING:
        fresh = plan(connection, now=now, sessions=sessions, gate=gate)
        if fresh["refusal"]:
            raise workflow.WorkflowError(f"Nothing queued: {fresh['refusal']}.")
        ready = {row["id"] for row in fresh["eligible"]}
        gone = [item["id"] for item in items if item["id"] not in ready]
        if gone:
            why = {row["id"]: row["why"] for row in fresh["skipped"]}
            raise workflow.WorkflowError("Nothing queued: " + "; ".join(
                f"#{item} {why.get(item, 'is not ready')}" for item in gone) + "; read the plan again.")
        if len(items) > fresh["room"]:
            raise workflow.WorkflowError(f"Nothing queued: {fresh['room']} builders fit now, not {len(items)}; read the plan again.")
        try:
            queued = build.queue(connection, [item["id"] for item in items], who=who,
                                 expected_revisions={item["id"]: item["revision"] for item in items})
        except build.BuildRefused as refusal:
            # `build.queue` is one transaction: a refusal wrote nothing, so the page reads the plan again.
            return 409, {"error": f"Nothing queued: {refusal}.", "reload": True}
        return builds.answer(connection, queued, start or builds.kickstart)


def request(values: dict):
    """The POST's checks, before the server opens a connection: `items`, each `{id, revision}`, at most `CAP`."""
    items = values.get("items")
    if (set(values) != {"items"} or not isinstance(items, list) or not 1 <= len(items) <= CAP
            or any(not isinstance(item, dict) or set(item) != {"id", "revision"} or type(item["id"]) is not int
                   or not 1 <= item["id"] <= 9223372036854775807 or not isinstance(item["revision"], str) for item in items)
            or len({item["id"] for item in items}) != len(items)):
        raise ValueError(f"Start next takes items: 1 to {CAP} distinct {{id, revision}} pairs from its plan.")
    return lambda connection: run(connection, now=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), items=items,
                                  who="dashboard")
