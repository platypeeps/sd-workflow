"""Teammate runs: an in-session builder a lead spawned, on the build run's rows (pack sd:7221).

Design: `docs/work/2026-11-02-builder-heartbeat/design.md`.

A lead's Claude Code teammate builds in its own worktree and writes no row,
so Today could not list it beside the headless build runs. `start` writes
what a build claim writes, with no lease and no process: one `assignment`
per item (scope `teammate`, lead first, each member's `parent` the lead,
`budget_minutes` the lead's budget) and one `runner_run` on the lead, with
the worktree and branch the lead named. `beat` refreshes the run's
`heartbeat_at`; the builder calls it at its own checkpoints (a commit, a
report). `end` closes the run once the lead has the builder's report.

The assignments hold `STATUS`, not `running`: a running assignment means
the runner owns the item, so every status write, cancel and merge guard
refuses it. A teammate's item stays the session's to move.

Only the lead's `end` releases a run. Neither silence nor every item done
proves the builder stopped: it beats only at checkpoints, and may still
write after its item lands. A run silent past its budget plus
`GRACE_MINUTES` reads `overdue` on Today and stays held. Each write is one
transaction, so a stop between two leaves either the old row or the new
one.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Literal

from . import build, reads
from .database import transaction
from .errors import SdDbError
from .runner import IN_SESSION, restoration_pending
from .writes import add_note, create_assignment
from .writes import now as clock

SCOPE = "teammate"
#: A live teammate assignment's status: in `runner.OWNING`, so no pickup takes its item, and outside
#: `workflow.ACTIVE_ASSIGNMENTS`, so the session's own status writes pass.
STATUS = IN_SESSION
#: Minutes past the budget a silent run waits before Today reads it overdue; it ends nothing.
GRACE_MINUTES = 30
#: The item statuses a run may not start on.
CLOSED = ("done", "cancelled")
#: The lane entry states whose items a delivery still holds (`sd_lane`): queued, in its runner, or
#: prepared and waiting for a hand merge (lead ruling on pack sd:7288).
LANE_LIVE = ("pending", "running", "prepared")
#: Why a repository may hold a second open branch beside its open bundle (pack sd:7737): a change that moves
#: state in steps gets its own PR, and a bundle already in the lane takes no more commits; a P1 item builds
#: beside it, alone or with items of its own area (operator ruling #64744, sd:9700).
SECOND_BRANCH = ("state-move", "in-lane", "p1")


class TeammateRefused(SdDbError):
    """A start, beat or end refused, with nothing written."""


def open_runs(connection) -> list[dict]:
    """Every unreleased teammate run, oldest first, with its lead's budget."""
    return [dict(row) for row in connection.execute(
        "SELECT runner_run.*, assignment.budget_minutes FROM runner_run JOIN assignment "
        "ON assignment.id = runner_run.assignment WHERE runner_run.released_at IS NULL AND assignment.scope = ? "
        "ORDER BY runner_run.created_at, runner_run.id", (SCOPE,))]


def members(connection, lead: int) -> list[dict]:
    """The run's assignments, the lead first, each with its item."""
    return [dict(row) for row in connection.execute(
        "SELECT id, item, status FROM assignment WHERE (id = ? OR parent = ?) AND scope = ? ORDER BY id = ? DESC, id",
        (lead, lead, SCOPE, lead))]


def due(run: dict) -> str | None:
    """When the run's budget ends: its start plus the lead's `budget_minutes`; None without a budget."""
    if not run.get("budget_minutes"):
        return None
    return (datetime.fromisoformat(run["created_at"]) + timedelta(minutes=run["budget_minutes"])).isoformat()


def holding(connection, item: int) -> dict | None:
    """The open teammate run that holds `item`, as its lead's or a member's item, or None."""
    row = connection.execute(
        "SELECT runner_run.id FROM assignment holder JOIN runner_run "
        "ON runner_run.assignment = COALESCE(holder.parent, holder.id) WHERE holder.item = ? AND holder.scope = ? "
        "AND runner_run.released_at IS NULL AND runner_run.outcome IS NULL LIMIT 1", (item, SCOPE)).fetchone()
    return dict(row) if row else None


def lane_holding(connection, item: int) -> str | None:
    """Why a lane entry still delivers `item`, or None (pack sd:7288).

    A pending, running or prepared entry delivers its item and its closes
    (`reads.lane_delivered`). A merged entry that claimed delivery owes each
    of them a close until a status note on it records `delivered at` the
    merge commit, the sentence every delivering transition writes
    (`workflow.DELIVERED_AT`); a reopen after that close owes nothing. A
    failed delivering entry whose ship receipt reached `merge_dispatch` may
    have published, so it owes the close the same way, at the receipt's
    commit (`_dispatched`, pack sd:7295). An entry or a receipt that does
    not parse may deliver the item, so it refuses too.
    """
    for row in reads.lane_entries(connection):
        try:
            entry = json.loads(row["body"])
        except (TypeError, ValueError):
            entry = None
        if not isinstance(entry, dict) or not isinstance(entry.get("closes") or [], list):
            return f"lane entry {row['key']} is unreadable, so whether it delivers item {item} is unknown"
        if item not in reads.lane_delivered(entry):
            continue
        status, where = entry.get("status"), f"lane entry {entry.get('id')} of sd:{entry.get('item')}"
        if status in LANE_LIVE:
            return f"item {item} is delivered by {status} {where}; wait for it to merge or end"
        if entry.get("claim") != "deliver" or status not in ("merged", "failed"):
            continue
        commit = entry.get("merge_commit")
        if status == "failed":
            receipt = _dispatched(connection, entry)
            if receipt is None:
                continue
            if receipt is False:
                return (f"item {item}: the ship receipt of failed {where} cannot be read, so whether it "
                        "published is unknown; settle it with `sd-ship reconcile` first")
            commit = receipt.get("merge_commit")
        paid = isinstance(commit, str) and commit and connection.execute(
            "SELECT 1 FROM note WHERE item = ? AND kind = 'status_change' AND instr(body, ?) > 0 LIMIT 1",
            (item, f"delivered at {commit}")).fetchone()
        if not paid:
            return (f"item {item} has its close owed by {status} {where} at {commit or 'an unrecorded commit'}; "
                    "settle it with `sd-ship reconcile` first")
    return None


def _dispatched(connection, entry: dict) -> dict | Literal[False] | None:
    """The ship receipt of a failed `entry` that reached `merge_dispatch`, so may have published (pack sd:7295).

    None when its receipt never dispatched, or when the entry has no branch,
    which no pull request merges. False when the receipt cannot be read:
    unknown, not "never published". The lane's `check_unmerged` reads the
    same receipt and the same two phases.
    """
    # Read at the call: `ship` reaches `runner` through `ledger`.
    from . import ship  # noqa: PLC0415
    branch, repository, number = entry.get("branch"), entry.get("repository"), entry.get("item")
    if not isinstance(branch, str) or not branch:
        return None
    if not isinstance(repository, str) or type(number) is not int:
        return False
    try:
        receipt = ship.read(connection, ship.receipt_key(repository, branch, number))[1]
    except ship.WorkflowError:
        return False
    return receipt if receipt.get("phase") in ("merge_dispatch", "merged") else None


#: The tail `start` gives a side branch's start note: the run keeps its `--into` there, so runs started
#: before pack sd:7748 read the same way and the run row needs no column.
INTO = "; merges into "


def _into(connection, run: dict) -> str | None:
    """The branch an open run's side branch merges into, from its lead item's start note; None for a bundle."""
    prefix = f"Teammate run {run['id']} started by "
    row = connection.execute(
        "SELECT note.body FROM note JOIN assignment ON assignment.item = note.item WHERE assignment.id = ? "
        "AND note.kind = 'comment' AND substr(note.body, 1, ?) = ? ORDER BY note.id LIMIT 1",
        (run["assignment"], len(prefix), prefix)).fetchone()
    _, found, target = (row["body"] if row else "").rpartition(INTO)
    return target if found and target and not any(c.isspace() for c in target) else None


def start(connection, items: list[int], *, worktree: str, branch: str, budget_minutes: int, owner: str,
          into: str | None = None, reason: str | None = None) -> dict:
    """Register one teammate run for `items`, the first its lead; the run, as `build.run` reads it.

    pack sd:7737: one open bundle branch per repository. Another open run of
    the repository on another branch, a teammate's or a headless build's
    (sd:8821), refuses the start, naming its branch and
    worktree, unless `into` names that branch (a side branch that merges into
    the bundle) or `reason` is one of `SECOND_BRANCH`, which each item records.
    An open run is never in the lane: enqueue refuses an item one holds.
    pack sd:7748: runs share a bundle when their branch or `into` meet, so
    sibling side branches into one bundle are not beside each other.
    """
    if not items or len(set(items)) != len(items) or any(type(item) is not int for item in items):
        raise TeammateRefused("name one or more distinct items")
    if type(budget_minutes) is not int or budget_minutes <= 0:
        raise TeammateRefused("the budget is a positive number of minutes")
    for name, value in (("worktree", worktree), ("branch", branch), ("owner", owner)):
        if not isinstance(value, str) or not value.strip():
            raise TeammateRefused(f"a teammate run names its {name}")
    if into is not None and (not isinstance(into, str) or not into or any(c.isspace() for c in into)):
        raise TeammateRefused(f"--into names a branch, not {into!r}")
    if reason is not None and reason not in SECOND_BRANCH:
        raise TeammateRefused(f"a second open branch's reason is state-move, in-lane or p1, not {reason!r}")
    with transaction(connection):
        if restoration_pending(connection):
            raise TeammateRefused("database restore reimport is incomplete; teammate runs are held")
        repos, rows = set(), []
        for item in items:
            row = connection.execute(build._REPO, (item,)).fetchone()
            if row is None:
                raise TeammateRefused(f"no item {item}")
            rows.append(row)
            if row["repo_path"] is None:
                raise TeammateRefused(f"item {item} needs a registered repository")
            if row["status"] in CLOSED:
                raise TeammateRefused(f"item {item} is {row['status']}")
            waiting = build.waiting_on(connection, row)  # as the Build press reads it (pack sd:9570)
            if waiting:
                raise TeammateRefused(f"item {item} {waiting}")
            held = holding(connection, item)
            if held:
                raise TeammateRefused(f"item {item} is held by teammate run {held['id']}; end it first")
            retained = build._held(connection, item)  # a build run not yet released, bundle members too
            if retained:
                raise TeammateRefused(retained)
            delivering = lane_holding(connection, item)
            if delivering:
                raise TeammateRefused(delivering)
            active = connection.execute(
                f"SELECT id, status, scope FROM assignment WHERE item = ? AND status IN {build.ACTIVE} LIMIT 1",
                (item,)).fetchone()
            if active:
                raise TeammateRefused(f"item {item} has {active['status']} {active['scope']} assignment {active['id']}")
            repos.add(row["repo"])
        if len(repos) != 1:
            raise TeammateRefused("a teammate run's items share one repository")
        if reason == "p1" and not (build.beside(rows[0]) and all(build.kin(rows[0], row) for row in rows[1:])):
            raise TeammateRefused("--reason p1 takes a P1 item first, then only items of its own area")
        repo = repos.pop()
        mine = {branch, into} - {None}
        beside = []
        # A headless build holds the repository's one branch too (sd:8821).
        live = [*open_runs(connection), *({**run, "scope": build.SCOPE} for run in build.open_runs(connection))]
        for open_run in live:
            if open_run["repo"] != repo or open_run["outcome"] is not None:
                continue
            target = _into(connection, open_run)
            if not mine & ({open_run["branch"], target} - {None}):
                beside.append(open_run | {"into": target})
        if beside and reason is None:
            other = beside[0]
            bundle = other["into"] or other["branch"]
            side = f", a side branch of {other['into']}" if other["into"] else ""
            raise TeammateRefused(
                f"{repo} has open branch {other['branch']} in {other['work_path']} ({other.get('scope', SCOPE)} run {other['id']}"
                f"{side}): one open bundle branch per repository; build these items there, pass --into "
                f"{bundle} for a side branch that merges into it, or --reason state-move, "
                "--reason in-lane or --reason p1 for a second pull request")
        stamp = clock()
        lead = None
        for item in items:
            ident = create_assignment(connection, item=item, role="author", status=STATUS, lane="parallel",
                                      parent=lead, budget_minutes=budget_minutes)
            connection.execute("UPDATE assignment SET scope = ?, started = ?, run_count = 1 WHERE id = ?",
                               (SCOPE, stamp, ident))
            lead = lead or ident
        run = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, start_step, "
            "step, heartbeat_at, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'building', 'building', ?, ?, ?)",
            (run, lead, repo, branch, owner, worktree, worktree, stamp, stamp, stamp))
        for item in items:
            if beside:
                add_note(connection, item, "comment",
                         f"Second open branch {branch} of {repo} beside "
                         f"{', '.join(sorted({run['branch'] for run in beside}))}: {reason}")
            add_note(connection, item, "comment",
                     f"Teammate run {run} started by {owner}: worktree {worktree}, branch {branch}, "
                     f"budget {budget_minutes} min" + (f"{INTO}{into}" if into else ""))
        return run_state(connection, run)


def run_state(connection, ident: str) -> dict:
    """One teammate run with its lead's budget, its due time and its members, the lead first."""
    row = connection.execute(
        "SELECT runner_run.*, assignment.budget_minutes FROM runner_run JOIN assignment "
        "ON assignment.id = runner_run.assignment WHERE runner_run.id = ? AND assignment.scope = ?",
        (ident, SCOPE)).fetchone()
    if row is None:
        raise TeammateRefused(f"no teammate run {ident}")
    found = dict(row)
    return found | {"due": due(found), "members": members(connection, found["assignment"])}


def overdue(run: dict, now: str) -> str:
    """Why Today reads `run` overdue: no beat for its budget plus `GRACE_MINUTES`; '' otherwise.

    It ends nothing. A builder beats only at commits and reports, so silence
    is no proof it stopped; only the lead's `--end` ends a run. A beat
    stamped too far ahead has no age (`build._lead_age`) and reads overdue
    (sd:7986).
    """
    if not run.get("budget_minutes"):
        return ""
    stamp = run["heartbeat_at"] or run["created_at"]
    quiet = build._lead_age(stamp, now)
    if quiet is None:
        return (f"overdue: its beat {stamp} is ahead of this clock past {build.STALE_SECONDS} s, so it has "
                f"no age; check that machine's clock; its lead ends it")
    if quiet < (run["budget_minutes"] + GRACE_MINUTES) * 60:
        return ""
    return (f"overdue: no beat for {int(quiet // 60)} min, past its {run['budget_minutes']} min budget and "
            f"{GRACE_MINUTES} min grace; its lead ends it")


def _open(connection, item: int) -> str:
    held = holding(connection, item)
    if held is None:
        raise TeammateRefused(f"no open teammate run holds item {item}")
    return held["id"]


def beat(connection, item: int) -> str:
    """Refresh the beat of the open teammate run that holds `item`; the run's id."""
    with transaction(connection):
        run = _open(connection, item)
        stamp = clock()
        connection.execute("UPDATE runner_run SET heartbeat_at = ?, updated_at = ? WHERE id = ? "
                           "AND released_at IS NULL AND outcome IS NULL", (stamp, stamp, run))
        return run


def end(connection, item: int, *, reason: str = "") -> str:
    """End and release the open teammate run that holds `item`, `done`, in one write; the run's id.

    The one release: the items keep their status.
    """
    detail = reason.strip() or "ended by its lead"
    with transaction(connection):
        run = connection.execute("SELECT * FROM runner_run WHERE id = ?", (_open(connection, item),)).fetchone()
        stamp = clock()
        connection.execute("UPDATE runner_run SET outcome = 'done', detail = ?, released_at = ?, updated_at = ? "
                           "WHERE id = ?", (detail, stamp, stamp, run["id"]))
        for member in members(connection, run["assignment"]):
            connection.execute("UPDATE assignment SET status = 'done', ended = ?, result = ? WHERE id = ? AND status = ?",
                               (stamp, detail, member["id"], STATUS))
            add_note(connection, member["item"], "comment", f"Teammate run {run['id']} ended done: {detail}")
        return run["id"]
