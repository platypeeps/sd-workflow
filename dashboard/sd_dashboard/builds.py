"""The Build press on Tasks and the Builds panel on Queue (pack sd:6095).

Design: system `docs/work/2026-10-24-build-dispatcher/design.md`, step 4 and its Dashboard section.

`plan` is what the Build dialog shows before anything is written: each repository's bundles of up to
`build.BUNDLE_MAX` items with the model and the room now, and every selected item a press would refuse, with
why. `press` is the Build press: `build.queue` with the revisions the dialog read, the whole selection or none. A
refusal is a 409 and writes nothing. After the queue lands it kickstarts the `build-dispatch` job, so a claim
takes seconds, not a slot; a kickstart that fails only says so, and the next tick claims the rows.

`panel` is Queue's Builds panel: one row per open build run; under Show All, the ended build runs too, however old,
with their outcome (pack sd:6834: a parked item's note points here for Retry). `retry` is Retry on a blocked or cancelled run: each such member's item goes back to `ready` and
its assignment is queued again (`runner.requeue`), in one transaction. With a `finding` it is Send to builder (pack
sd:6020): the finding is a `build.FINDING` note on each item first, and the claim keeps the bundle under its lead, so
the next builder resumes the same worktree and its brief names the finding. Cancel on a running build is the route that
exists, `/api/assignments/<lead>/cancel` (`operations.cancel_assignment`), bound to the revision this panel read.

Fix on a failed lane entry (pack sd:8584) is Send to builder on the build run that enqueued the entry's branch
(`lane_fix_state`), named by the press: `build.lane_fix` hands the entry back to the builder and queues its bundle again
under its lead, or refuses. Retry and a plain Send to builder still refuse a `lane_failed` run.
"""

from __future__ import annotations

import functools
import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from sd_db import build, operations, reads, runner, teammate, workflow
from sd_db import repos as sd_repos
from sd_db.config import label_prefix
from sd_db.database import transaction
from sd_db.errors import SdDbError
from sd_db.writes import SettingStale, add_note, set_setting, transition

__all__ = ["ENDED_MAX", "JOB", "QUIET_SECONDS", "answer", "file_and_build", "fix_flaky", "kickstart", "lane_fix_state", "panel",
           "pickup", "plan", "press", "retry"]

#: The local-cron-jobs job that runs `sd assignments dispatch` (system, build-dispatch.job).
JOB = "build-dispatch"
#: A builder whose log has not grown this long is stalled: shown, never acted on (design, Dashboard).
QUIET_SECONDS = 15 * 60
#: The most ended runs Show All lists, newest first.
# ponytail: a flat cap; page it if the parked runs ever outnumber it.
ENDED_MAX = 500
KICKSTART_SECONDS = 5
#: The bundle statuses Retry and Send to builder take: each keeps its worktree.
AGAIN = ("blocked", "cancelled")
#: The longest finding Send to builder takes, as a note line the brief quotes.
FINDING_MAX = 4000
#: The steps a builder writes its log in; a quiet log elsewhere (a review, the lane) is not a stall.
LOGGING = ("building", "fixing")


def items_of(values) -> list[int]:
    """The selected item ids, distinct positive integers, or ValueError."""
    if (not isinstance(values, list) or not values or len(values) > 50
            or any(type(item) is not int or not 1 <= item <= 9223372036854775807 for item in values)
            or len(set(values)) != len(values)):
        raise ValueError("Select one to 50 distinct items to build.")
    return values


def _at(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds(since: str | None, now: str) -> int | None:
    start, end = _at(since), _at(now)
    return int((end - start).total_seconds()) if start and end else None


def interactive(connection, *, now: str, sessions=None) -> list[dict] | None:
    """One `{repo, session_id}` per interactive builder, as `sd assignments dispatch` counts them; None when unread.

    `sessions` is `claude_sessions.document`, the seam a test fills. A cwd in a linked worktree counts against its
    main checkout (`sd_lib.main_worktree_root`, pack sd:6112).
    """
    from . import claude_sessions, pack_module

    try:
        document = (sessions or claude_sessions.document)(connection, now=now)
        if document["builders"]["running"] is None:
            return None
        root = pack_module("sd_lib").main_worktree_root
        repos = [row["path"] for row in sd_repos.registered(connection)]
        found = []
        for session in document["sessions"] or []:
            count = sum(1 for agent in session.get("agents") or [] if agent["builder"] and agent["state"] != "idle")
            count += 1 if session.get("builder") and session.get("status") != "idle" else 0
            cwd = str(root(Path(session["cwd"]).resolve())) if session.get("cwd") else ""
            key = build.paths.key(cwd) if cwd else ""
            repo = next((path for path in repos if key and (key == path or key.startswith(path.rstrip("/") + "/"))), None)
            found += [{"repo": repo, "session_id": session.get("id")}] * count
        return found
    except Exception:  # noqa: BLE001 -- any failure is "not counted", which shows no room
        return None


def _room(connection, *, now: str, sessions=None, gate=None) -> dict:
    from .start_next import UNREAD, _load

    try:
        load, load_cap = _load(gate)
    except UNREAD:
        load = load_cap = None
    return build.room(connection, load=load, load_cap=load_cap, interactive=interactive(connection, now=now,
                                                                                        sessions=sessions))


def plan(connection, items: list[int], *, now: str, sessions=None, gate=None) -> dict:
    """What a Build press on `items` would queue: `bundles` by repository, `refused` with each reason, the `room` now.

    A refused item refuses the whole press, as `build.queue` does; the dialog shows why and offers no Build.
    `room` is informative: a press queues whatever the room, and the claim waits for room. A bundle whose lane
    runs on a satellite names that `host` and its dispatcher's `beat` age in seconds; this room is not its own
    (pack sd:7933).
    """
    taken, refused = [], []
    for item in items:
        try:
            taken.append(build._item(connection, item))  # the press's own check, so the dialog never disagrees with it
        except build.BuildRefused as refusal:
            refused.append({"id": item, "why": str(refusal)})
    revisions = reads.revisions(connection, [row["id"] for row in taken]) if taken else {}
    room = _room(connection, now=now, sessions=sessions, gate=gate)
    groups: dict[str, list[dict]] = {}
    for row in taken:
        groups.setdefault(row["repo"], []).append(row)
    bundles = []
    for repo, rows in groups.items():
        for start in range(0, len(rows), build.BUNDLE_MAX):
            chunk = rows[start:start + build.BUNDLE_MAX]
            host = build.lane_of(connection, chunk[0]["lane_host"]) or ""
            bundles.append({"repo": repo, "name": Path(repo).name, "host": host,
                            "beat": build.dispatcher_beat(connection, host) if host else None,
                            "room": 0 if host else build.room_for(room, repo),
                            "model": build._model(connection, [row["id"] for row in chunk]),
                            "branch": "build/sd-<lead assignment>",
                            "items": [{"id": row["id"], "title": row["title"], "revision": revisions[row["id"]]}
                                      for row in chunk]})
    return {"read": now, "bundles": bundles, "refused": refused,
            "room": {"machine": room["machine"], "why": room["why"], "cap": build.MACHINE_CAP,
                     "repo_cap": build.REPO_CAP},
            "budget_minutes": build.BUDGET_MINUTES}


def kickstart(*, runner=subprocess.run) -> dict:
    """Start the dispatcher's tick now: `{ok, why}`. A failure only waits for the next two-minute slot."""
    service = f"gui/{os.getuid()}/{label_prefix()}.cron.{JOB}"
    try:
        done = runner(["/bin/launchctl", "kickstart", service], capture_output=True, text=True,
                      timeout=KICKSTART_SECONDS, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as failure:
        return {"ok": False, "service": service, "why": f"launchctl kickstart did not run: {failure}"}
    if done.returncode != 0:
        why = (done.stderr or done.stdout or "").strip().splitlines()
        return {"ok": False, "service": service,
                "why": f"launchctl kickstart exit {done.returncode}" + (f": {why[-1]}" if why else "")}
    return {"ok": True, "service": service, "why": ""}


def answer(connection, queued: list[dict], start) -> dict:
    """A press's answer: each queued row with the `host` whose dispatcher claims it ("" for the hub), and the
    hub's `kickstart`, None when every row is a satellite's: the hub's tick claims none of those (pack sd:7933)."""
    hosts = {row["item"]: build.item_host(connection, row["item"]) or "" for row in queued}
    return {"queued": [{"id": row["id"], "item": row["item"], "status": row["status"], "host": hosts[row["item"]]}
                       for row in queued],
            "kickstart": start() if not all(hosts.values()) else None}


def press(payload: dict, *, start=None):
    """POST /api/tasks/build: `{items, revisions}`; the write `action_route` runs, or ValueError for a bad body."""
    if set(payload) != {"items", "revisions"} or not isinstance(payload["revisions"], dict):
        raise ValueError("Provide the items to build and the revision the dialog read for each.")
    items = items_of(payload["items"])
    revisions = payload["revisions"]
    if any(not isinstance(value, str) for value in revisions.values()):
        raise ValueError("Each revision is the text the dialog read.")

    def write(connection):
        try:
            queued = build.queue(connection, items, who="dashboard", expected_revisions=revisions)
        except build.BuildRefused as refusal:
            # A refused press wrote nothing (`build.queue`'s one transaction): a 409, so the page reads again.
            return 409, {"error": f"Nothing queued: {refusal}.", "reload": True}
        return answer(connection, queued, start or kickstart)

    return write


def file_and_build(connection, capture, *, who: str = "dashboard") -> dict:
    """Fix's file-then-build (pack sd:8113): `capture()` files the task under its ref or answers the open one
    (`existing`); a new one goes to ready and its build queues as the Build press queues it. A refused build leaves the
    item filed, ready, and names why in `build_error`; nothing retries it."""
    state = capture()
    if state.get("existing"):
        return state
    item = state["item"]["id"]
    state = workflow.change_status(connection, item, "ready", who=who, expected_revision=state["revision"])
    try:
        queued = answer(connection, build.queue(connection, [item], who=who), kickstart)
    except build.BuildRefused as refusal:
        return {**workflow.item_state(connection, item), "existing": False, "build_error": str(refusal)}
    return {**workflow.item_state(connection, item), "existing": False, **queued}


#: The ask every flaky Fix brief ends with (pack sd:8113).
FLAKY_ASK = ("Find the failing test from the logs, reproduce it under load (make test SUITE=<suite>), and fix the cause "
             "fail-first, or quarantine the test with a reason; never a retry loop.")


def _flaky_brief(row: dict) -> tuple[str, str]:
    """A flaky head's task title and body: each failed lane run with its prepare log, the passing run, the ask."""
    def run(r):
        return f"- {r['entry']} {r['status']} at {r['at']}: " + (f"{r['log']} on {r['host'] or 'the lane host'}"
                                                                   if r["log"] else "no prepare log recorded")

    title = f"Flaky test in {row['repo']} at {row['head'][:12]}: investigate and fix"
    body = "\n".join([f"The gate failed {len(row['runs'])}x at head {row['head']} in {row['repo']}, then passed at it.",
                      "", "Failed lane runs:", *map(run, row["runs"]), "", "Passing run:", run(row["pass_run"]), "",
                      FLAKY_ASK])
    return title, body


def fix_flaky(connection, rows: list[str], *, now: str, capture) -> dict:
    """`POST /api/items {"fix": [<flaky row id>, ...]}`: one task per head, each filed and built alone, so a failure on
    one names it and the rest still file. `capture(connection, ref=, title=, body=, repo=)` files or answers the open item.
    """
    from sd_db.errors import SdDbError

    from .now_screen import flaky_rows

    heads = {row["id"]: row for row in flaky_rows(connection, now)[0]}
    results, failed = [], []
    for ident in rows:
        try:
            row = heads.get(ident)
            if row is None:
                raise ValueError(f"{ident} is no longer a flaky head of the last week")
            owner = workflow.item_state(connection, row["item"])["item"] if row.get("item") else None
            if not owner or not owner.get("repo"):
                raise ValueError(f"the lane entry at {row['head'][:12]} names no repository through its item")
            title, body = _flaky_brief(row)
            with transaction(connection):
                state = file_and_build(connection, functools.partial(capture, connection, ref=ident, title=title,
                                                                     body=body, repo=owner["repo"]))
            if state.get("build_error"):
                raise ValueError(f"filed sd:{state['item']['id']}, but its build did not queue: {state['build_error']}")
            results.append({"row": ident, "item": state["item"]["id"], "existing": bool(state.get("existing"))})
        except (ValueError, SdDbError, build.BuildRefused) as error:
            failed.append({"row": ident, "error": str(error)})
    return {"filed": sum(not r["existing"] for r in results), "existing": sum(r["existing"] for r in results),
            "failed": failed, "results": results}


def _waiting(connection, run: dict, items: list[int]) -> str:
    """Why a run waits on the operator: it is held (`blocked`, `abandoned`), or an open question note asks."""
    if run["outcome"] in ("blocked", "abandoned"):
        return run["detail"] or f"the run ended {run['outcome']}"
    asked = build.open_question(connection, items, run["created_at"])
    return f"question note #{asked['id']} on sd:{asked['item']}" if asked else ""


def _log_quiet(path: str | None, now: str) -> int | None:
    """Seconds since the run's log last grew, or None when it has none or it is not read."""
    if not path:
        return None
    try:
        changed = os.stat(path).st_mtime
    except OSError:
        return None
    clock = _at(now)
    return max(0, int(clock.timestamp() - changed)) if clock else None


def _members(connection, run: dict) -> list[dict]:
    rows = build.run(connection, run["id"])["members"]
    titles = reads.items_by_ids(connection, [row["item"] for row in rows])
    return [{"item": row["item"], "assignment": row["id"], "status": row["status"],
             "title": titles[row["item"]]["title"] if row["item"] in titles else ""} for row in rows]


def _open_row(connection, run: dict, now: str) -> dict:
    members = _members(connection, run)
    step = run["step"] or "claimed"
    # A satellite stamps its beat on its own clock: one too far ahead has no age and reads lost (sd:7986).
    age = build._lead_age(run["heartbeat_at"] or run["created_at"], now)
    beat = None if age is None else int(age)
    # A satellite's run logs on its own disk: the row names the host and the path, and reads no log here (sd:7933).
    host = build.lane_of(connection, build.run_host(run["owner"])) or ""
    quiet = None if host else _log_quiet(run["log_path"], now)
    waiting = _waiting(connection, run, [member["item"] for member in members])
    lost = step != "enqueued" and run["outcome"] is None and (beat is None or beat >= build.STALE_SECONDS)
    state = ("waiting" if waiting else "lost" if lost
             else "stalled" if step in LOGGING and quiet is not None and quiet >= QUIET_SECONDS else "working")
    cancel = {"assignment": run["assignment"], "allowed": False, "reason": "", "revision": ""}
    try:
        current = operations.assignment_state(connection, run["assignment"])
        cancel.update(revision=current["revision"], allowed=current["capabilities"]["cancel"]["allowed"],
                      reason=current["capabilities"]["cancel"]["reason"])
    except (SdDbError, sqlite3.Error) as failure:
        cancel["reason"] = f"the assignment was not read: {failure}"
    if cancel["allowed"] and run["cancel_requested"]:
        cancel.update(allowed=False, reason=f"{run['cancel_requested']}; the supervisor stops at its next beat")
    elif cancel["allowed"] and step not in build.CANCELLABLE:
        cancel.update(allowed=False, reason=f"the run is at {step}; " + build.stop_step(
            connection, run["id"], members[0]["item"] if members else "<item>"))
    return {"run": run["id"], "attempt": run["run"], "repo": run["repo"], "name": Path(run["repo"]).name, "host": host,
            "items": members, "model": run["model"] or "", "step": step, "state": state, "why": waiting,
            "outcome": run["outcome"], "started": run["created_at"], "elapsed": _seconds(run["created_at"], now),
            "beat": beat, "quiet": quiet, "usd": round(build.run_spent(connection, run["id"]), 2),
            "log": run["log_path"] or "", "branch": run["branch"] or "", "worktree": run["work_path"] or "",
            "pull": build.pull_url(connection, run["repo"], run["branch"], [member["item"] for member in members]),
            "cancel": cancel}


def _teammate_row(connection, run: dict, now: str) -> dict:
    """One open teammate run (`sd_db.teammate`, pack sd:7221) in `_open_row`'s shape, `teammate` true, with its
    budget's end as `due`: `stalled` once that has passed, else `working`, and overdue (`teammate.overdue`) past
    its grace too. Its lead ends it; nothing here cancels it, and no silence ends it."""
    found = teammate.members(connection, run["assignment"])
    titles = reads.items_by_ids(connection, [row["item"] for row in found])
    members = [{"item": row["item"], "assignment": row["id"], "status": row["status"],
                "title": titles[row["item"]]["title"] if row["item"] in titles else ""} for row in found]
    due, clock = _at(teammate.due(run)), _at(now)
    late = bool(due and clock and clock >= due)
    overdue = teammate.overdue(run, now)
    first = members[0]["item"] if members else "<item>"
    return {"run": run["id"], "attempt": run["run"], "teammate": True, "repo": run["repo"],
            "name": Path(run["repo"]).name, "items": members, "model": "", "step": "building",
            "state": "stalled" if late else "working", "why": overdue or ("past its budget" if late else ""),
            "outcome": run["outcome"], "started": run["created_at"], "elapsed": _seconds(run["created_at"], now),
            "beat": _seconds(run["heartbeat_at"] or run["created_at"], now), "quiet": None, "usd": 0.0, "log": "",
            "branch": run["branch"] or "", "worktree": run["work_path"] or "", "due": teammate.due(run),
            "budget": run["budget_minutes"],
            "pull": build.pull_url(connection, run["repo"], run["branch"], [member["item"] for member in members]),
            "cancel": {"assignment": run["assignment"], "allowed": False, "revision": "",
                       "reason": f"its lead ends it: sd assignments beat {first} --end"}}


def _retry_state(connection, run: dict) -> dict:
    """Retry on an ended run: only the lead's latest run, released, whose bundle ended blocked or cancelled.

    A bundle blocks on a `blocked` outcome and on a second loss at one step, whose run keeps the loss as its
    outcome (`build.LOSSES`); the lead assignment's `blocked` status covers both.
    A cancelled bundle keeps its worktree too, so Send to builder may resume it (pack sd:6020).
    """
    current = runner.queue_state(connection, run["assignment"])
    latest = current["run"]
    reason = ""
    if latest["id"] != run["id"]:
        reason = f"run {latest['id']} is newer"
    elif latest["released_at"] is None:
        reason = "the run still holds its lease"
    elif current["status"] not in AGAIN:
        reason = f"the bundle ended {current['status']}; Retry takes a blocked or cancelled one"
    return {"assignment": run["assignment"], "revision": current["revision"], "allowed": not reason, "reason": reason,
            "bundle": current["status"]}


def lane_fix_state(connection, branch: str | None) -> dict:
    """Fix on a failed lane entry (pack sd:8584): the newest build run holding `branch`, with the lead's revision, and
    whether Fix takes it: `build.lane_fix_check`, the check its press makes. Off with the reason in every other state:
    the entry names no branch, no build run holds it (a session enqueued it), the check refuses, or a read failed."""
    if not branch:
        return {"allowed": False, "reason": "the lane entry names no branch"}
    try:
        run = connection.execute(
            "SELECT runner_run.* FROM runner_run JOIN assignment ON assignment.id = runner_run.assignment "
            "WHERE runner_run.branch = ? AND assignment.scope = ? ORDER BY runner_run.created_at DESC, runner_run.run DESC "
            "LIMIT 1", (branch, build.SCOPE)).fetchone()
        if run is None:
            return {"allowed": False, "reason": f"no build run holds branch {branch}; send the finding to the session "
                                                "that enqueued it"}
        revision = runner.queue_state(connection, run["assignment"])["revision"]
        reason = ""
        try:
            build.lane_fix_check(connection, run["id"])
        except build.BuildRefused as refusal:
            reason = str(refusal)
    except (SdDbError, sqlite3.Error) as failure:
        return {"allowed": False, "reason": f"the build runs were not read: {failure}"}
    return {"allowed": not reason, "reason": reason, "assignment": run["assignment"], "revision": revision,
            "run": run["id"]}


def panel(connection, *, now: str, show: str = "active") -> dict:
    """Queue's Builds panel: `runs`, each open build run, then each open teammate run (`_teammate_row`); `recent`, under `show` "all" only, the ended runs, newest
    first, up to `ENDED_MAX` (pack sd:6834).

    `unclaimed` maps each queued build assignment of another host's lane, whose dispatcher beats that it claims
    nothing, to "<host> claims nothing: <why>": the hub's claim leaves that row queued (review round 5).

    A read that failed is `error`, so the page says the builds were not read, never that none run.
    """
    try:
        rows = [_open_row(connection, run, now) for run in build.open_runs(connection)]
        unclaimed = build.unclaimed(connection)
        rows += [_teammate_row(connection, run, now) for run in teammate.open_runs(connection)]
        recent = []
        for row in build.ended_runs(connection, limit=ENDED_MAX) if show == "all" else []:
            members = _members(connection, row)
            recent.append({"run": row["id"], "assignment": row["assignment"], "repo": row["repo"],
                           "host": build.lane_of(connection, build.run_host(row["owner"])) or "",
                           "name": Path(row["repo"]).name, "items": members,
                           "pull": build.pull_url(connection, row["repo"], row["branch"], [m["item"] for m in members]),
                           "outcome": row["outcome"], "detail": row["detail"] or "",
                           "ended": row["released_at"], "step": row["step"] or "claimed", "model": row["model"] or "",
                           "usd": round(build.run_spent(connection, row["id"]), 2), "log": row["log_path"] or "",
                           "worktree": row["work_path"] or "", "retry": _retry_state(connection, row)})
    except (SdDbError, sqlite3.Error, ValueError, TypeError, KeyError) as failure:
        return {"runs": [], "recent": [], "error": str(failure) or "the build runs could not be read", "read": now,
                "show": show}
    return {"runs": rows, "recent": recent, "unclaimed": unclaimed, "error": "", "read": now, "quiet_seconds": QUIET_SECONDS,
            "stale_seconds": build.STALE_SECONDS, "show": show, "pickup": _pickup(connection, now)}


def _pickup(connection, now: str) -> dict:
    """Auto-pickup's state for the panel's control (pack sd:6097): `on`, `until`, or `error` when unread."""
    try:
        state = build.pickup_state(connection, now=now)
    except (SdDbError, sqlite3.Error) as failure:
        return {"on": None, "until": None, "error": str(failure)}
    return {"on": state["on"], "until": state["until"], "next": build.morning(now), "error": ""}


def pickup(payload: dict):
    """POST /api/queue/builds/pickup: `{on, until}`, `until` the end the panel read (null when off); the write
    `action_route` runs, or ValueError. On ends at the next 09:00 America/Denver; Off clears it. A value
    that moved since the read is a 409 and writes nothing."""
    if (set(payload) != {"on", "until"} or type(payload["on"]) is not bool
            or not (payload["until"] is None or isinstance(payload["until"], str))):
        raise ValueError("Auto-pickup takes on (true or false) and the end the panel read.")

    def write(connection):
        try:
            set_setting(connection, build.AUTO_PICKUP, build.morning() if payload["on"] else None,
                        before=payload["until"])
        except SettingStale:
            return 409, {"error": "Auto-pickup changed since the panel read it. Read it again.", "reload": True}
        state = build.pickup_state(connection)
        return {"on": state["on"], "until": state["until"], "kickstart": kickstart() if state["on"] else None}

    return write


def retry(connection, assignment: int, *, expected_revision: str, who: str, finding: str = "",
          run: str | None = None) -> dict:
    """Retry a blocked or cancelled build: each such member's item to `ready`, a `finding` note on it when one is
    sent (Send to builder), then its assignment queued again, in one transaction.

    The lead's revision is the one the panel read; a moved run is `StaleItem` (a 409). Each item must pass the
    press's own check once it is ready, so a retried bundle is one the next claim can take.

    `run` is Fix on a failed lane entry (pack sd:8584): the lane_failed run the page read, which must be the lead's
    latest; `build.lane_fix` hands the entry back and queues the bundle, or refuses. It never falls back to Retry.
    """
    with transaction(connection):
        current = runner.queue_state(connection, assignment)
        if current["scope"] != build.SCOPE:
            raise workflow.WorkflowError(f"assignment {assignment} is not a build")
        if current["revision"] != expected_revision:
            raise workflow.StaleItem(f"build assignment {assignment} changed; read the Builds panel again")
        if run is not None:
            try:
                if (current["run"] or {}).get("id") != run:
                    raise build.BuildRefused(f"build run {run} is not assignment {assignment}'s latest run")
                items = build.lane_fix(connection, run, finding, who=who)
            except build.BuildRefused as refusal:
                raise workflow.WorkflowError(f"Fix refused: {refusal}") from None
            return {"run": run, "queued": [{"id": member["id"], "item": member["item"], "status": "queued"}
                                           for member in build._members(connection, assignment, "queued")
                                           if member["item"] in items]}
        run = current["run"]
        state = _retry_state(connection, run) if run else {"allowed": False, "reason": "it never ran"}
        if not state["allowed"]:
            raise workflow.WorkflowError(f"Retry refused: {state['reason']}")
        members = build._members(connection, assignment, current["status"])
        if not members:
            raise workflow.WorkflowError(f"Retry refused: build run {run['id']} has no {current['status']} member")
        items = reads.items_by_ids(connection, [member["item"] for member in members])
        queued = []
        for member in members:
            item = items.get(member["item"])
            if item is not None and item["status"] == "blocked":
                transition(connection, member["item"], "ready", who=who, reason=f"Retry of build run {run['id']}")
            build.unpark(connection, member["item"])
            try:
                build._item(connection, member["item"])
            except build.BuildRefused as refusal:
                raise workflow.WorkflowError(f"Retry refused: {refusal}") from None
            if finding:
                add_note(connection, member["item"], "comment", build.FINDING + finding)
            fresh = runner.queue_state(connection, member["id"])["revision"]
            queued.append(runner.requeue(connection, member["id"], expected_revision=fresh, who=who))
            add_note(connection, member["item"], "comment",
                     f"Build assignment {member['id']} queued again by {who}: "
                     f"{'Send to builder' if finding else 'Retry'} of build run {run['id']}")
        return {"run": run["id"], "queued": [{"id": row["id"], "item": row["item"], "status": row["status"]}
                                             for row in queued]}
