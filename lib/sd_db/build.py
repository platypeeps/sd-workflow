"""The Build queue and its claim, on the retired runner's tables (pack sd:6093).

Design: system `docs/work/2026-10-24-build-dispatcher/design.md`, step 1.

A Build press queues one `assignment` per item: role `author`, lane
`parallel`, scope `build`. A claim takes up to `BUNDLE_MAX` queued builds of
one repository as a bundle, more when they are small (`bundled`): one
`runner_run` (the attempt) and one `runner_lease` on branch `build/sd-<lead>`, every member `running` with its
`parent` the lead, and every item `in_progress`. While an assignment is
queued or running, `workflow.change_status` refuses the item's status writes
(criterion 7.19); a cancel is the way out.

Every write a supervisor makes is a compare-and-set on its own run, open and
unreleased (`write`, `enqueued`), so a late old supervisor writes nothing.
`reap` ends a run that fails `alive` in three steps, each safe to stop after:
the compare-and-set of its outcome, the kill of its builder's process group,
and one transaction that releases it and retries, blocks or cancels. A tick
that dies between them leaves an ended, unreleased run, and the next `reap`
starts at the kill.

Nothing here runs a process but the kill and the liveness probe; the
dispatcher (`build_run`, step 2) reads the load and the interactive builders,
which are not in the database, and passes them in.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import hub, paths, reads
from .database import served_by, transaction
from .errors import SdDbError
from .needs_sweep import standing_ruling
from .runner import (
    ALONE,
    IN_SESSION,
    OWNING,
    RUNNABLE_KINDS,
    queue_state,
    requeue,
    restoration_pending,
)
from .ship import this_host
from .workflow import item_area, item_blocked_by
from .writes import (
    UNBUILT,
    UNCHECKED,
    add_note,
    cancel_unwanted_blocked,
    create_assignment,
    create_item,
    read_settings,
    set_item_fields,
    set_setting,
    transition,
)
from .writes import now as clock

#: The assignment scope a Build press writes.
SCOPE = "build"
#: The scope of a draft run (`sd_db.draft`): it counts against the machine cap only, as it holds no branch.
DRAFT = "draft"
#: Code-writing builders per machine, the operator's standing rule, and bundles building per repository at once.
#: One: later items wait and the next bundle takes them (sd:8158); a bundle in the lane frees it (sd:8277).
#: `REPO_CAP` is the default of the machine setting `sd.repo_cap` (`repo_cap`, sd:9172), and `MACHINE_CAP`
#: of `sd.machine_cap` (`machine_cap`, sd:9891).
MACHINE_CAP = 6
REPO_CAP = 1
#: Items per bundle, so a bundle fits one 120-minute run (lead, note #29482): the default of the machine
#: setting `sd.bundle_max` (`bundle_max`, sd:9109). A run's budget is `BUDGET_MINUTES` per `BUNDLE_MAX` slots
#: of the bundle it claimed, never less (`budget_minutes`).
BUNDLE_MAX = 3
BUDGET_MINUTES = 120
#: A P1 item builds beside its repository's bundle, alone or with the queued items of its own area, under the
#: machine cap only; every other item keeps one bundle per repository (operator ruling #64744, sd:9700).
BESIDE_PRIORITY = 1
#: A supervisor beats every 30 s; one silent this long, or unspawned this long after its claim, is lost.
#: 600, not 180: a beat a held lock skips no longer ends a long build (pack sd:6741).
STALE_SECONDS = 600
#: The `heartbeat` state row each build-dispatch tick writes first: this key on the hub, `<key>@<host>` on a
#: satellite (pack sd:7933).
HEARTBEAT = "build-dispatch"
#: Consecutive satellite beats naming a `why` after which the hub's claim blocks that lane's queued rows: one
#: unread tick passes, a read that fails every tick does not (sd:8081).
STUCK_TICKS = 3
#: Lost runs one bundle may retry before it blocks.
RETRIES = 1
# The note on an item a blocked run parked (pack sd:6132).
PARKED = "Parked: build run {run} ended blocked; Retry on the Builds panel unparks it"
# The note on an item whose retried bundle a reopened dependency held at its claim (review of sd:9570).
WAIT_PARKED = "Parked: build assignment {lead}'s bundle waits ({held}); Retry on the Builds panel unparks it"
# The note on an item a cancelled run parked: auto-pickup takes unparked items only (sd:9222).
CANCEL_PARKED = "Parked: build run {run} was cancelled; Unpark to build it again"


def beside(row) -> bool:
    """Whether a row's item is P1, so builds beside its repository's bundle (`BESIDE_PRIORITY`, sd:9700)."""
    return type(row["priority"]) is int and row["priority"] <= BESIDE_PRIORITY


#: A queued build row's bundle lead's item priority: a retried bundle takes room by its lead, as `room` counts its
#: run, never by a member raised to P1 since (review of sd:9700).
LEAD_PRIORITY = ("(SELECT lead.priority FROM assignment AS head JOIN item AS lead ON lead.id = head.item "
                 "WHERE head.id = COALESCE(assignment.parent, assignment.id)) AS lead_priority")


#: The lead item's `fields` key that keeps the slot its claim took, `beside` or `bundle`: `room` reads it, not the
#: priority now, so a priority edit during the run moves no run (review of build/sd-1306).
SLOT = "build_slot"


def _holds_slot(row) -> bool:
    """Whether a running build's lease holds its repository's slot: as its claim classified it (`SLOT`), or, for a
    run claimed before the key, by its lead's priority now."""
    return row["slot"] == "bundle" if row["slot"] in ("beside", "bundle") else not beside(row)


def _leads_beside(row) -> bool:
    """Whether a queued build row's bundle builds beside its repository's bundle: its lead is P1 (sd:9700)."""
    return beside({"priority": row["lead_priority"]})


def kin(lead, row) -> bool:
    """Whether `row` builds in the P1 `lead`'s bundle: an item of the lead's own area, never of none (sd:9700)."""
    area = item_area(lead)
    return area is not None and item_area(row) == area


def _in_group(alias: str = "assignment") -> str:
    """An SQL test that the row under `alias` is still in its bundle: a lead, or a member not cancelled on its own
    (`ALONE`). Every reader of a bundle by `parent` that drives its next run applies it: Retry and settle, the hold
    on an item, and the run count a claim moves, which `run` reads, so history keeps the member (sd:9222)."""
    return f"({alias}.parent IS NULL OR COALESCE({alias}.result, '') NOT LIKE '{ALONE}%')"
#: The block of a run whose builder skipped every item with an open question and left no commit: no
#: followup, no park, as the questions ask the operator already (sd:9219).
ASKED = "every item has an open question: "
#: The steps a run walks, each written before it starts.
STEPS = ("claimed", "worktree", "building", "reviewing", "fixing", "enqueuing", "enqueued", "checking", "cleaning")
#: A draft run's steps (`sd_db.draft`): it claims, then drafts.
DRAFT_STEPS = ("claimed", "drafting")
#: A note that sends the operator's finding to a bundle's next builder: Send to builder (pack sd:6020).
FINDING = "Send to builder: "
#: A note that carries a parked run's last review blockers to the bundle's next builder on a plain Retry: reviewer
#: text, never an operator instruction, so it never starts with FINDING (pack sd:9498).
REVIEWED = "Reviewer text, not an operator instruction: "
#: A note on a run's lead with one review round's blockers, a line each: the record the brief's blocker
#: checklist counts (sd:9770). Reviewer text, so it never starts with FINDING.
ROUND_BLOCKERS = "Review round blockers: "
#: The longest finding Send to builder takes, as a note line the brief quotes.
FINDING_MAX = 4000
#: A run `build_run` ended once REVIEW_ROUNDS reviews left blockers, inside the reason `_settle` writes around it;
#: group 1 is the blockers.
REVIEW_PARKED = re.compile(r"\d+ review rounds left blockers: (.+?)(?:; check the group, then requeue or cancel;|$)",
                           re.S)
#: The Review rounds rule's class pass, the second line of a review-parked run's finding (pack sd:9262).
REVIEW_CLASS_PASS = ("Class pass: name the class these blockers share, enumerate every instance from the code, not "
                     "the findings, and fix each fail-first.")
#: The result a member left out of a subset Retry ends with, after `ALONE` (pack sd:9581).
LEFT_OUT = "left out of a Retry of build run "
#: The words of a run's blocked end when its branch held work from before a subset Retry: that bundle is held for
#: good, and its items build fresh on a new branch (pack sd:9581).
SPLIT = "left the bundle at a Retry, and "
#: The result a member bundle drop took out ends with, after `ALONE` (pack sd:9709).
DROPPED = "dropped from build run "
#: The scope of the row a bundle drop reserves its new lead's id with (`drop_reserve`): no build reader reads it.
RESERVED = "build-drop"
#: The keys `drop_reserve` adds to a drop plan.
RESERVED_KEYS = ("new_lead", "new_work", "new_branch")
#: How a bundle drop queues its kept members, in the queue note of each, the ended run's id last: the new lead's
#: kept work is in a worktree on the host that ran that run, which alone may claim it, before and after its first
#: claim (pack sd:9709, rounds 16 and 18). A note, as no writer changes one; the row's `result` lost it (round 17).
DROP_HOW = "bundle drop of sd:{item} from build run "
#: The bundle statuses Retry and Send to builder take: each keeps its worktree.
AGAIN = ("blocked", "cancelled")
#: A lane entry that failed only because prepare's merge of origin/<base> conflicted goes back to its builder on
#: its own, under its lead, with this finding (pack sd:6940); `CONFLICT_REBUILDS` times, then the operator decides.
#: `REBUILD` is the note's stable prefix the rebuild count matches; the base it names follows it (pack sd:7008).
CONFLICT = re.compile(r"\bmerging (origin/\S+) into \S+ conflicts\b")
#: The lane entry a `lane_failed` run's detail names, as `build_run.watch` writes it: `lane entry <id> <status>: ...`.
#: `None` is a watch that read no id, not an id (pack sd:8665).
LANE_ENTRY = re.compile(r"^lane entry (?!None )(\S+) ")
REBUILD = FINDING + "the lane's merge of "
CONFLICT_REBUILDS = 2
#: The model a bundle runs on (Q60): Sonnet only when every member's `fields.work_kind` is `mechanical`.
OPUS, SONNET = "opus", "sonnet"
#: The fields a supervisor may write on its own open run.
RUN_FIELDS = frozenset({"step", "heartbeat_at", "supervisor_pid", "supervisor_pgid", "supervisor_start", "session_id",
                        "log_path", "builder_pgid", "builder_start", "review_round", "final", "detail", "model",
                        "base_head"})
#: Every run column a reap decides on. Its compare-and-set, and the settle after it, match them all against the
#: row the reap read, so a writer between the read and the write (a beat, a registration, `enqueued`, a cancel)
#: makes the reap do nothing that tick.
REAP_READS = ("step", "outcome", "heartbeat_at", "supervisor_pid", "supervisor_start", "supervisor_pgid",
              "builder_pgid", "builder_start", "cancel_requested", "released_at")
_SAME = " AND ".join(f"{name} IS ?" for name in REAP_READS)
#: Where a fresh row resumes a step lost (design, "Run ownership by step"); any other step resumes itself.
#: A fix resumes at its review, which reads the head the fix left.
RESUME = {"claimed": "worktree", "fixing": "reviewing"}
#: The outcomes that count as a loss at a step; a second one at the same step blocks the bundle.
#: `denied` is a builder refused a tool call (pack sd:6723).
LOSSES = ("interrupted", "timed_out", "error_max_turns", "denied")
#: The steps from `enqueued` on: the lease is released, the members are `done`, and step 7 releases the run.
#: Only `enqueued` has no supervisor, so only it is left out of the reap.
LANE_STEPS = ("enqueued", "checking", "cleaning")
#: An open `runner_run` at a lane step, as SQL: it holds only the members it delivered (`_holding`, sd:8960).
#: Never NULL: callers negate it, and `NOT NULL` would drop the hold of a run with no step (gate of ebce96efb).
IN_LANE = (f"(runner_run.outcome IS NULL AND runner_run.step IS NOT NULL "
           f"AND runner_run.step IN ({', '.join(repr(step) for step in LANE_STEPS)}))")
#: An unmanaged repository's run at `enqueued` names its draft pull request in `detail`, which a lane run leaves
#: unset until it ends: the tick's watch reads that pull request, not the lane (pack sd:7868).
DRAFT_PULL = "draft pull request "
#: The steps a cancel may stop: from `enqueuing` on, the lane may hold the branch (design, "Liveness").
CANCELLABLE = ("claimed", "worktree", "building", "reviewing", "fixing")
ACTIVE = ("queued", "running", "ending")
#: Auto-pickup: "on", or none for off; On stays on until Off or a safety turn-off (pack sd:8930). The old
#: build's key, `build.auto_pickup_until`, held a 09:00 end; nothing reads it, so it reads off.
AUTO_PICKUP = "build.auto_pickup"
#: Who auto-pickup queues as. Its queue note on the lead's item marks the bundle as auto-pickup's for every run
#: and retry of it, so the merge gets `checking` and the two-blocked rule counts it (pack sd:6097).
PICKUP = "auto-pickup"
#: The title prefix of the item a failed health check after an auto-pickup merge files; the push reads it.
HEALTH = "Auto-pickup off: health failed after the merge of "
#: A run whose builder hit its account's usage limit (pack sd:9907): no loss and no block. `_settle` queues
#: the bundle again and holds the run's host, the setting `HOLD` + `hold_key`, until the limit resets.
RATE_LIMITED = "rate_limited"
HOLD = "build.rate_hold."
#: How long a hold lasts with no `resetsAt` ahead, and the furthest one may read: a weekly limit, and a day.
HOLD_SECONDS, HOLD_MAX_SECONDS = 3600, 8 * 86400
#: Why a held host claims nothing: the tick's beat, `room` and the Builds panel name it with its end.
RATE_HELD = "the builder's Claude account hit its usage limit; builds on this host hold until "


class BuildRefused(SdDbError):
    """A press or a claim refused, with nothing written."""


def _repo_refusal(row, connection=None, *, hub_only: bool = False) -> str | None:
    """Why the item's repository takes no build, or None: the one repository rule `queue` and `claim` read.

    `row` carries the item's `id` and `repo`, and the repository's `repo_path`,
    `managed`, `remote` and `lane_host` (`_REPO`'s columns). A build runs on
    its lane host (pack sd:7933); `hub_only`, for drafts, refuses a lane on
    a satellite (`lane_of`, which reads `connection`), as they run on the
    hub only.
    """
    item = row["id"]
    if row["repo_path"] is None:
        return f"item {item} needs a registered repository"
    if not row["managed"]:
        # An unmanaged repository's build ends in a draft pull request the owners merge (pack sd:7868):
        # it needs GitHub to open one, and a task to track it as the item's contribution `pull_url`.
        if not github_repository(row["remote"]):
            return (f"item {item}'s repository {row['repo']} is not managed and its remote is not on GitHub, "
                    f"where its build opens a draft pull request")
        if row["kind"] != "task":
            return (f"item {item} is a {row['kind']} item; a build in the unmanaged repository {row['repo']} "
                    f"takes task items only")
        # Its draft becomes the task's one contribution. One already linked would refuse that link only after the
        # push and the draft, on every Retry (review of 3072db937). A press or a claim precedes every run, so
        # no link here is this build's own: a retry passes through neither.
        try:
            linked = json.loads(row["fields"] or "{}").get("contribution") or {}
        except (ValueError, AttributeError):
            linked = {}
        for name, what in (("issue_url", "the issue"), ("pull_url", "the pull request")):
            if isinstance(linked, dict) and linked.get(name):
                return (f"item {item} already links {what} {linked[name]}; a build in the unmanaged repository "
                        f"{row['repo']} would push and open a draft pull request it cannot link")
    if not row["remote"]:
        return f"item {item}'s repository {row['repo']} has no remote"
    if hub_only and lane_of(connection, row["lane_host"]) is not None:
        return f"item {item}'s lane runs on {row['lane_host']}; drafts run on the hub only"
    return None


def lane_hosts(connection) -> frozenset[str | None]:
    """The lane hosts this machine builds for: its own name, and None (the hub's lanes) when it is the hub.

    As `ship._lane` reads a lane: NULL is the hub's, a name is that machine's.
    A satellite is a machine with a `hub.json` whose connection the hub
    serves (pack sd:7933). The file decides, not the transport: the wire
    harness serves the hub's own tests, and those tests stay the hub's.
    """
    here = this_host()
    satellite = hub.configured() and served_by(connection) is not None
    return frozenset({here}) if satellite else frozenset({None, here})


def lane_of(connection, host: str | None) -> str | None:
    """The other machine whose dispatcher runs a lane on `host`, or None when the hub's does: the one rule for
    a press, a claim, pickup, drafts and the dashboard (review round 3).

    `host` is a `repo.lane_host` or a run's `run_host`. NULL is the hub's, and
    so is this machine's own name on the hub, as `sd_lane.ended_kind` reads
    it. A satellite reads the hub's name from `hub_host`; its own name is its
    own satellite lane (pack sd:7955).
    """
    if host is None:
        return None
    if None in lane_hosts(connection):
        return None if host == this_host() else host
    return None if host == hub_host() else host


def hub_host() -> str | None:
    """The hub's lane host name as this satellite reads it, or None when no `hub.json` names a hub.

    The first label of the host `hub.json` names, lower-cased: the hub's
    `hostname -s` (`ship.this_host`) when its tailnet name is its machine
    name, as `hub._matches` reads a bare `sd.hub_name` (pack sd:7955). An
    address, or a tailnet name that differs, matches no lane, so a lane
    named for the hub reads as another host's, as it did before. A
    `hub.json` that does not read raises `HubConfigError`, as `hub.read`.
    """
    found = hub.read()
    return found.host.rstrip(".").split(".")[0].lower() if found is not None else None


def dispatcher_gone(connection, host: str) -> str | None:
    """Why no build-dispatch tick on the satellite `host` runs now, or None: its newest beat is under
    `STALE_SECONDS`, the reap's own rule. The hub's reap asks it: a tick that beats reaps its own runs. A beat
    too far ahead to read (`dispatcher_beat`) names the skew, not a dead dispatcher (pack sd:7955)."""
    beat = dispatcher_beat(connection, host)
    if beat is not None and beat < STALE_SECONDS:
        return None
    ahead = _ahead(connection, host)
    if ahead is not None:
        return (f"the {HEARTBEAT}@{host} beat is {ahead} s ahead of this clock, past the {STALE_SECONDS} s "
                f"a beat may lead; check that machine's clock")
    return f"no {HEARTBEAT}@{host} beat is under {STALE_SECONDS} s old"


def dispatcher_refusal(connection, host: str) -> str | None:
    """Why no build-dispatch tick on the satellite `host` would claim a press, or None: `dispatcher_gone`, or
    the `why` its newest beat names (review round 4). The press asks it.

    A tick beats before it claims, so a beat alone does not say it can: one
    with no log folder or a restore pending claims nothing. An `UNREAD` why
    lasts one tick, so it refuses no press: the next counted beat claims, and
    the Builds panel names it meanwhile (`unclaimed`, pack sd:8055).
    """
    gone = dispatcher_gone(connection, host)
    if gone:
        return gone
    why = dispatcher_why(connection, host)
    # A usage-limit hold names its end, so its press queues and builds then (pack sd:9907).
    return f"{HEARTBEAT}@{host} claims nothing: {why}" if why and not why.startswith((*UNREAD, RATE_HELD)) else None


def dispatcher_stuck(connection, host: str) -> str | None:
    """Why `host`'s tick claimed nothing for its last `STUCK_TICKS` beats, or None when one of them named no `why`.

    Retention keeps one beat per key, so a compaction restarts the count:
    it delays the block by a few ticks, never blocks early (sd:8081).
    """
    rows = connection.execute("SELECT body FROM state WHERE kind = 'heartbeat' AND key = ? ORDER BY id DESC LIMIT ?",
                              (f"{HEARTBEAT}@{host}", STUCK_TICKS)).fetchall()
    whys = [_why(row) for row in rows]
    # A usage-limit hold is a tick that works and waits for a known end, not a stuck one (pack sd:9907).
    if len(whys) < STUCK_TICKS or not all(whys) or str(whys[0]).startswith(RATE_HELD):
        return None
    return f"{HEARTBEAT}@{host} claimed nothing for {STUCK_TICKS} ticks: {whys[0]}"


def item_host(connection, item: int) -> str | None:
    """`lane_of` the lane of `item`'s repository: the other machine whose dispatcher claims its build, or None."""
    row = connection.execute(_REPO, (item,)).fetchone()
    return lane_of(connection, row["lane_host"]) if row else None


def unclaimed(connection) -> dict[str, str]:
    """Each queued build assignment of another host's lane whose fresh beat names why it claims nothing, by id, to
    "<host> claims nothing: <why>": the hub's claim leaves those rows queued, and the Builds panel names the reason
    (review round 5)."""
    found = {}
    for row in connection.execute(
            "SELECT assignment.id, assignment.parent, assignment.run_count, repo.lane_host FROM assignment "
            "JOIN item ON item.id = assignment.item LEFT JOIN repo ON repo.path = item.repo "
            "WHERE assignment.scope = ? AND assignment.status = 'queued'", (SCOPE,)):
        host = _route(connection, row["lane_host"], _lead(row))
        why = dispatcher_why(connection, host) if host and not dispatcher_gone(connection, host) else None
        if why:
            found[str(row["id"])] = f"{host} claims nothing: {why}"
    return found


def dispatcher_why(connection, host: str) -> str | None:
    """The reason the newest beat of `host`'s tick names for claiming nothing, or None when it names none.

    A reason may last one tick (an unread load), so the hub's claim leaves a
    queued row queued on it and blocks only on `dispatcher_gone` or
    `dispatcher_stuck`; the Builds panel shows it on the row (review round 5).
    """
    row = _beat(connection, host)
    return _why(row) if row else None


def _why(row) -> str | None:
    try:
        why = json.loads(row["body"]).get("why")
    except (TypeError, ValueError, AttributeError):
        why = None
    return why if isinstance(why, str) and why else None


def overloaded(connection, host: str) -> bool:
    """Whether the satellite `host`'s fresh beat says its five-minute load is over its cap: the hub's claim then
    builds that lane's fresh queued rows itself when `overload_fallback` is on, and the satellite's lane still lands
    them (sd:8991, sd:10823)."""
    if dispatcher_gone(connection, host):
        return False
    row = _beat(connection, host)
    try:
        load = json.loads(row["body"]).get("load") if row else None
    except (TypeError, ValueError, AttributeError):
        load = None
    return isinstance(load, str) and bool(load)


def _checked_out(repo: str) -> bool:
    """Whether `repo` has a checkout on this machine's disk, which a build's worktree comes from (sd:8991)."""
    return (paths.expand(repo) / ".git").exists()


def _lead(row) -> int | None:
    """The lead assignment of `row`'s bundle once it ran, or None for a fresh row that never ran."""
    return row["parent"] or (row["id"] if row["run_count"] else None)


def _route(connection, lane_host: str | None, lead: int | None) -> str | None:
    """`lane_of` the host whose dispatcher claims a build of the repository whose lane is `lane_host`, for the
    bundle `lead` leads (None when it never ran): its lane now, as a retry follows `repo.lane_host`, except a bundle
    the hub built under the overload fallback for a satellite's lane, whose commits sit in the hub's worktree: that
    one builds on the hub again (sd:8991). So does a bundle drop's kept lead that has not run, whose kept work is
    in that hub worktree (`_kept_host`, sd:10319). The one rule for a claim, Retry, pickup and the Builds panel."""
    lane = lane_of(connection, lane_host)
    if lane is None or lead is None:
        return lane
    ran = connection.execute("SELECT owner FROM runner_run WHERE assignment = ? ORDER BY run DESC LIMIT 1",
                             (lead,)).fetchone()
    if ran is None:
        row = connection.execute("SELECT id, item, parent FROM assignment WHERE id = ?", (lead,)).fetchone()
        kept = _kept_host(connection, row) if row is not None else None
        return None if kept is not None and lane_of(connection, kept[1]) is None else lane
    return None if lane_of(connection, run_host(ran["owner"])) is None else lane


def run_owner(owner: str, lane_host: str | None) -> str:
    """The `runner_run.owner` a claim writes: the role on a hub lane, `<role>@<lane host>` on a satellite's."""
    return owner if lane_host is None else f"{owner}@{lane_host}"


def run_host(owner: str | None) -> str | None:
    """The host whose dispatcher owns a run, from its `owner`: the part after `@`, or None for the hub."""
    return (owner.rpartition("@")[2] or None) if owner and "@" in owner else None


def mine(row, hosts: Collection[str | None]) -> bool:
    """Whether this machine's dispatcher owns the run `row`: only it probes, signals, watches or sweeps it."""
    return run_host(row["owner"]) in hosts


def stop_step(connection, run: str, item) -> str:
    """How to stop a build run past the steps a cancel may stop: its lane entry, or, in an unmanaged repository,
    its draft pull request, which `sd-ship lane cancel` refuses there (sd:7909)."""
    row = connection.execute("SELECT repo FROM runner_run WHERE id = ?", (run,)).fetchone()
    if row is not None and unmanaged(connection, row["repo"]):
        return "close its draft pull request on GitHub"
    return f"the lane may hold its branch; cancel its lane entry with `sd-ship lane cancel {item}`"


def github_repository(remote: str | None) -> str:
    """`owner/name` of a GitHub remote, or "" for none or another host."""
    from .ship import _github_repository
    from .workflow import WorkflowError

    try:
        return _github_repository(remote) if remote else ""
    except WorkflowError:
        return ""


def unmanaged(connection, repo: str) -> bool:
    """Whether the repository row reads `managed = 0`: its builds end in a draft pull request (pack sd:7868)."""
    probe = paths.keys(repo)
    row = connection.execute(f"SELECT managed FROM repo WHERE path IN ({paths.placeholders(probe)})",
                             probe).fetchone()
    return row is not None and not row["managed"]


_REPO = ("SELECT item.*, repo.path AS repo_path, repo.remote, repo.managed, repo.lane_host "
         "FROM item LEFT JOIN repo ON repo.path = item.repo WHERE item.id = ?")


def _item(connection, item: int, *, lead: int | None = None, riding: Collection[int] = ()) -> dict:
    """The item a build may take, or the refusal that names why not; `lead` is the bundle a Retry queues again,
    whose lane `_route` reads (sd:8991); `riding`, designs of the same press that ride its bundle (`_riding`)."""
    row = connection.execute(_REPO, (item,)).fetchone()
    if row is None:
        raise BuildRefused(f"no item {item}")
    if row["kind"] not in RUNNABLE_KINDS:  # named before the status, as a press of a chore reads it
        raise BuildRefused(_press_refusal(connection, row))
    if row["status"] != "ready" and not (row["status"] == "blocked" and split_held(connection, item)):
        if row["status"] != "blocked" or (since := _rebuild_since(connection, item)) is None:
            raise BuildRefused(f"item {item} is {row['status']}; a build takes a ready item")
        _answered(connection, item, since)
    refused = _press_refusal(connection, row, lead=lead, riding=riding)
    if refused:
        raise BuildRefused(refused)
    return dict(row)


def _press_refusal(connection, row, *, lead: int | None = None, riding: Collection[int] = ()) -> str | None:
    """Why the press refuses the item `row` once it is ready, or None: `_item`'s checks past its status, a read
    only, so Build by item asks it of each member Retry would queue (`_retry_row`, class pass, review of sd:9788)."""
    item = row["id"]
    if row["kind"] not in RUNNABLE_KINDS:
        return (f"item {item} is a {row['kind']} item; a build takes only "
                f"{', '.join(RUNNABLE_KINDS[:-1])} and {RUNNABLE_KINDS[-1]} items")
    # A design its ride brings into this item's bundle holds it no more: else each waited on the other
    # forever (review of sd:10096).
    here = {other for other in riding if connection.execute(
        "SELECT repo FROM item WHERE id = ?", (other,)).fetchone()["repo"] == row["repo"]}
    waiting = waiting_on(connection, row, riding=here | _riding(connection, row["repo"]))
    if waiting:
        return f"item {item} {waiting}"
    refused = _repo_refusal(row, connection)
    if refused:
        return refused
    away = _route(connection, row["lane_host"], lead)
    gone = dispatcher_refusal(connection, away) if away else None
    if away and gone:
        # Nothing claims a satellite's press until its own dispatcher ticks, and the press locks the item. A beat
        # too far ahead names the skew alone: its dispatcher may well tick (pack sd:7955).
        dead = "" if _ahead(connection, away) is not None else ": no dispatcher there would claim it"
        return f"item {item}'s lane runs on {away}, and {gone}{dead}"
    active = connection.execute(
        f"SELECT id, status FROM assignment WHERE item = ? AND status IN {OWNING} LIMIT 1", (item,)).fetchone()
    if active:
        return f"item {item} has {active['status']} assignment {active['id']}"
    if work_kind(connection, item) == DESIGN_KIND:
        if draft_design(connection, item) is None:
            # Its design run comes first, and design runs start on the hub only (review of sd:10096).
            from . import draft  # noqa: PLC0415 -- draft imports this module
            refused = draft.repo_refusal(row, connection)
            if refused:
                return refused
    waits = design_waits(connection, item)
    if waits:
        return f"item {item} {waits}; rule on it, then Build it"
    return _held(connection, item)


def waiting_on(connection, row, *, riding: Collection[int] = ()) -> str:
    """What the item `row` waits on, as "waits on sd:M", or "" (pack sd:9570): each item its `fields.blocked_by`
    names that is not done. Read where a build is chosen, so nothing runs on a done. Fails closed: fields that cannot
    be read, or a named item gone, hold it too. `riding`: design items that ride `row`'s own bundle, whose design
    its first commit carries, so they hold no build of it (review of sd:10096)."""
    try:
        named = item_blocked_by(row)
    except ValueError as error:
        return f"may wait on an item: its {error}"
    if not named:
        return ""
    marks = ", ".join("?" for _ in named)
    found = {other["id"]: other["status"] for other in connection.execute(
        f"SELECT id, status FROM item WHERE id IN ({marks})", named)}
    gone = [other for other in named if other not in found]
    if gone:
        # `--blocked-by` replaces the set, so the fix names exactly the items that still exist: a bare
        # `--clear-blocked-by` would drop live ones too and let the item build before them (pack sd:10691).
        kept = [other for other in named if other in found]
        fix = " ".join(f"--blocked-by {other}" for other in kept) if kept else "--clear-blocked-by"
        return f"waits on sd:{gone[0]}, which no longer exists; drop it with sd task edit {row['id']} {fix}"
    held = [other for other in named if found[other] != "done" and other not in riding]
    return f"waits on {', '.join(f'sd:{other}' for other in held)}" if held else ""


#: A status change into a wait that gave its reason: `<from> -> <to> by <who>: <reason>` (writes.transition).
WAITED = re.compile(r"\w+ -> (?:planning|blocked) by [^:]*: (.+)", re.DOTALL)


def waits(connection, ids: list[int]) -> dict[int, dict]:
    """Each item's `parked_at` and `waited`, the note and reason of its latest move to planning or blocked that
    gave one, else None (pack sd:8964): the dialog shows what an item waited on before the press."""
    marks = ",".join("?" * len(ids))
    found = {row["id"]: {"parked_at": row["parked_at"], "waited": None} for row in connection.execute(
        f"SELECT id, parked_at FROM item WHERE id IN ({marks})", ids)}
    for note in connection.execute(f"SELECT id, item, body FROM note WHERE kind = 'status_change' AND item IN ({marks})"
                                   " ORDER BY id", ids):
        if moved := WAITED.fullmatch(note["body"] or ""):
            found[note["item"]]["waited"] = {"note": note["id"], "why": moved[1]}
    return found


def dispatcher_beat(connection, host: str) -> int | None:
    """Seconds since the build-dispatch tick on the satellite `host` last beat; None when it never did, or when
    its beat does not read: an unread beat is no beat.

    The satellite stamps its beat on its own clock, so a beat up to
    `STALE_SECONDS` ahead of this one is skew and reads as age 0, the same
    width staleness allows behind. A dead beat stamped ahead then reads
    fresh for at most 2 x `STALE_SECONDS` after its tick. One further
    ahead does not read: its age is unknown, not fresh, and `_ahead` names
    it (pack sd:7955).
    """
    row = _beat(connection, host)
    age = _lead_age(row["timestamp"], clock()) if row else None
    return None if age is None else int(age)


def _lead_age(stamp: str | None, now: str) -> float | None:
    """Seconds since `stamp`, written on another machine's clock, read on this one at `now`; None when unknown.

    A stamp up to `STALE_SECONDS` ahead is skew and reads as age 0, the
    width staleness allows behind. One further ahead, or one that does not
    read, has no age to trust: None, never fresh. The dispatcher beat and
    the run beat the hub strands on both take it (pack sd:7955, sd:7970).
    """
    try:
        age = _age(stamp, now) if stamp else None
    except (TypeError, ValueError):
        return None
    return None if age is None or age < -STALE_SECONDS else max(age, 0.0)


def _beat_age(connection, host: str) -> float | None:
    """Seconds since the newest build-dispatch beat of `host`, negative when stamped ahead; None when unread."""
    row = _beat(connection, host)
    try:
        return _age(row["timestamp"], clock()) if row else None
    except (TypeError, ValueError):
        return None


def _ahead(connection, host: str) -> int | None:
    """Seconds the newest beat of `host` is stamped ahead of this clock, when past `STALE_SECONDS`; else None."""
    age = _beat_age(connection, host)
    return int(-age) if age is not None and age < -STALE_SECONDS else None


def _beat_moves(connection, host: str, now: str) -> bool:
    """Whether the newest beat of `host` was recorded after the hub's own beat `STALE_SECONDS` before `now`.

    A fast clock leaves the stamp no age, so the hub reads the record: rows
    take ids in the order the hub writes them, and its own beat carries its
    own clock. With no hub beat that old there is no measure, and the beat
    counts as moving (pack sd:8001).
    """
    cutoff = (datetime.fromisoformat(now) - timedelta(seconds=STALE_SECONDS)).isoformat(timespec="seconds")
    mark = connection.execute("SELECT MAX(id) FROM state WHERE kind = 'heartbeat' AND key = ? AND timestamp <= ?",
                              (HEARTBEAT, cutoff)).fetchone()[0]
    row = connection.execute("SELECT MAX(id) FROM state WHERE kind = 'heartbeat' AND key = ?",
                             (f"{HEARTBEAT}@{host}",)).fetchone()
    return mark is None or (row[0] is not None and row[0] > mark)


def _beat(connection, host: str):
    return connection.execute("SELECT timestamp, body FROM state WHERE kind = 'heartbeat' AND key = ? "
                              "ORDER BY id DESC LIMIT 1", (f"{HEARTBEAT}@{host}",)).fetchone()


def queue(connection, items: list[int], *, who: str, expected_revisions: dict | None = None) -> list[dict]:
    """The Build press: one queued build assignment per item, or the whole selection refused."""
    if not items or len(set(items)) != len(items) or any(type(item) is not int for item in items):
        raise BuildRefused("select one or more distinct items")
    if not isinstance(who, str) or not who.strip():
        raise BuildRefused("a press names who pressed it")
    with transaction(connection):
        if restoration_pending(connection):
            raise BuildRefused(RESTORING)
        if expected_revisions is not None:
            from .workflow import item_state
            if {str(key) for key in expected_revisions} != {str(item) for item in items}:
                raise BuildRefused("the press needs one expected revision per selected item")
            for item in items:
                expected = expected_revisions.get(item, expected_revisions.get(str(item)))
                if item_state(connection, item)["revision"] != expected:
                    raise BuildRefused(f"item {item} changed; refresh the selection")
        # A written design pressed with a code item that waits on it rides that item's bundle (review of sd:10096).
        riding = {item for item in items if work_kind(connection, item) == DESIGN_KIND
                  and draft_design(connection, item) is not None and design_waits(connection, item) is None}
        for item in items:
            _item(connection, item, riding=riding)
            # Pickup read `eligible` before this transaction: an item a cancel parked since is the operator's pause,
            # so a stale pickup refuses it and the next tick reads again (review of sd:9222).
            if who == PICKUP and connection.execute("SELECT parked_at FROM item WHERE id = ?",
                                                    (item,)).fetchone()["parked_at"] is not None:
                raise BuildRefused(f"item {item} was parked after auto-pickup read it")
        created = []
        for item in items:
            if reads.items_by_ids(connection, [item])[item]["status"] == "blocked":  # `_item` took it blocked
                transition(connection, item, "ready", who=who,
                           reason="its bundle's branch is held after a split" if split_held(connection, item)
                           else "Build after its bundle ended")
            if work_kind(connection, item) == DESIGN_KIND and draft_design(connection, item) is None:
                # A design item with no design yet runs a design run first, beside the code (sd:10096).
                from . import draft  # noqa: PLC0415 -- draft imports this module
                ident = draft.queue_design(connection, item, who=who)
            else:
                ident = queue_row(connection, item, who=who, riding=riding)
            # A press of an item a cancel parked builds it unparked, as Retry does (sd:9222); pickup refused one above.
            unpark(connection, item)
            created.append(ident)
        return [queue_state(connection, ident) for ident in created]


def queue_row(connection, item: int, *, who: str, riding: Collection[int] = ()) -> int:
    """One queued build assignment of `item`, with its note, or the press's refusal (`_item`): the one writer of
    a queued build, so the ride a design run queues passes every check a press does (class pass of sd:10096)."""
    _item(connection, item, riding=riding)
    ident = create_assignment(connection, item=item, role="author", status="queued", lane="parallel",
                              budget_minutes=BUDGET_MINUTES)
    connection.execute("UPDATE assignment SET scope = ?, queued_at = ?, started = NULL WHERE id = ?",
                       (SCOPE, clock(), ident))
    add_note(connection, item, "comment", f"Build assignment {ident} queued by {who}")
    return ident


def design_waits(connection, item: int, *, delivered: bool = False) -> str | None:
    """Why the design item `item` may not ride, be claimed or be delivered, or None: a question on it that no
    standing ruling answers. The one reader of a design's rulings beside `design_rulings`: the press, auto-pickup,
    Retry, the claim, delivery and the merge all ask it, so a ruling Decide reversed or a review withdrew holds
    each of them (class pass of sd:10096). `delivered`, at a run's delivery: a standing ruling the newest brief
    did not carry holds it too, as the committed design applies the one it replaced (review of sd:10096)."""
    if work_kind(connection, item) != DESIGN_KIND:
        return None
    asked = open_question(connection, [item], "", pending=True)
    if asked:
        return f"waits on design question note #{asked['id']}"
    carried = briefed(connection, item) if delivered else None
    if carried is None:
        return None
    # Either way round: a ruling the brief lacked, or one it carried that stands no more, its question
    # reopened or not (class pass of sd:10096).
    standing = {ruling["id"] for ruling in _standing_rulings(connection, item)}
    if standing - carried:
        return f"has ruling #{min(standing - carried)}, which its build's brief did not carry"
    if carried - standing:
        return f"has ruling #{min(carried - standing)}, which its build's brief carried, standing no more"
    return None


#: The comment a build run writes on a design item as it sends the brief: the rulings the brief carries.
BRIEFED = "Design brief carries rulings:"


def note_brief(connection, item: int, ident: str) -> None:
    """Record the standing rulings a brief sent now carries for the design item `item` (`briefed`)."""
    carried = " ".join(f"#{ruling['id']}" for ruling in _standing_rulings(connection, item)) or "none"
    add_note(connection, item, "comment", f"{BRIEFED} {carried} (build run {ident})")


def briefed(connection, item: int) -> set[int] | None:
    """The rulings the newest brief carried for `item` (`note_brief`), or None when no brief recorded them.
    Note ids, never timestamps: a ruling in the claim's second is no replacement (review of sd:10096)."""
    row = connection.execute("SELECT body FROM note WHERE item = ? AND kind = 'comment' AND body LIKE ? "
                             "ORDER BY id DESC LIMIT 1", (item, f"{BRIEFED} %")).fetchone()
    if row is None:
        return None
    return {int(number) for number in re.findall(r"#([0-9]+)", row["body"].removeprefix(BRIEFED).partition("(")[0])}


def design_rulings(connection, item: int) -> list[str]:
    """The standing ruling of each question on the design item `item`, oldest question first: the newest ruling
    that no reversal or withdrawal took back (`needs_sweep.standing_ruling`), never one before it."""
    return [ruling["body"] for ruling in _standing_rulings(connection, item)]


def _standing_rulings(connection, item: int) -> list:
    """The standing ruling note of each question on `item`, oldest question first."""
    found = []
    for asked in connection.execute("SELECT id, item FROM note WHERE item = ? AND kind = 'question' ORDER BY id",
                                    (item,)).fetchall():
        ruling = standing_ruling(connection, asked)
        if ruling is not None:
            found.append(connection.execute("SELECT id, body, timestamp FROM note WHERE id = ?", (ruling,)).fetchone())
    return found


def waits_on_operator(connection, item: int, since: str) -> bool:
    """Whether `item` waits on the operator at delivery: a question since `since` (`open_question`), or a design
    whose ruling was taken back, whenever its question was asked, or replaced since its brief (`design_waits`)."""
    return (open_question(connection, [item], since) is not None
            or design_waits(connection, item, delivered=True) is not None)


def draft_design(connection, item: int) -> str | None:
    from . import draft  # noqa: PLC0415 -- draft imports this module
    return draft.design(connection, item)


def parked_finding(detail: str | None) -> str:
    """The finding for a run parked after REVIEW_ROUNDS: the last round's blockers, then the class pass; "" for any
    other run. Send to builder prefills it, and a plain Retry sends it (pack sd:9262, sd:9498)."""
    parked = REVIEW_PARKED.search(detail or "")
    if not parked:
        return ""
    return (f"The last review round left these blockers: {parked[1].strip()}"[:FINDING_MAX - len(REVIEW_CLASS_PASS) - 1]
            + "\n" + REVIEW_CLASS_PASS)


def retry_state(connection, run: dict, *, held: bool = False) -> dict:
    """Retry on an ended run: only the lead's latest run, released, whose bundle ended blocked or cancelled.

    `held` True admits a bundle held after a split (`split`): bundle drop checks each commit's citation itself and
    leaves the earlier work aside, on a fresh branch, so it may salvage one (pack sd:9863).

    A bundle blocks on a `blocked` outcome and on a second loss at one step, whose run keeps the loss as its
    outcome (`LOSSES`); the lead assignment's `blocked` status covers both. A `lane_failed` run's bundle ended
    otherwise, so it refuses. A cancelled bundle keeps its worktree too, so Send to builder may resume it (pack sd:6020).
    """
    current = queue_state(connection, run["assignment"])
    latest = current["run"]
    reason = ""
    if latest["id"] != run["id"]:
        reason = f"run {latest['id']} is newer"
    elif latest["released_at"] is None:
        reason = "the run still holds its lease"
    elif current["status"] not in AGAIN:
        reason = f"the bundle ended {current['status']}; Retry takes a blocked or cancelled one"
    elif not held and split(connection, run["assignment"]) == "held":
        reason = ("its branch holds work from before a subset Retry; Build its items to build them fresh on a "
                  "new branch")
    return {"assignment": run["assignment"], "revision": current["revision"], "allowed": not reason, "reason": reason,
            "bundle": current["status"]}


def retry(connection, assignment: int, *, who: str, finding: str = "", only: Collection[int] | None = None) -> dict:
    """Retry a blocked or cancelled build bundle under its lead `assignment`: each such member's item to `ready`, a
    `finding` note on it when one is sent (Send to builder), then its assignment queued again, in one transaction.
    A plain Retry of a run parked after REVIEW_ROUNDS notes its blockers as a REVIEWED note (`parked_finding`), so
    the next builder reads them as the reviewer's, not the operator's (pack sd:9498).
    Each item must pass the press's own check once it is ready, so a retried bundle is one the next claim can take.
    The Builds panel and `sd assignments build` share it (pack sd:8785).
    `only`, the items a retry names, retries those alone: it must name the lead, whose worktree holds the work,
    and each member it leaves out leaves the bundle (`ALONE`), its item ready and parked (pack sd:9581)."""
    with transaction(connection):
        current = queue_state(connection, assignment)
        if current["scope"] != SCOPE:
            raise BuildRefused(f"assignment {assignment} is not a build")
        last = current["run"]
        # A drop's kept lead never ran; its worktree is the split's, so a Retry of it held resumes that (review of
        # build/sd-1306). It names the ended run whose work it keeps, and it retries whole.
        resumes = None if last or current["parent"] is not None else _resumable(
            connection, assignment, current["item"], current["status"])
        if last:
            state = retry_state(connection, last)
        elif resumes and current["status"] in AGAIN and only is None:
            state = {"allowed": True}
        else:
            state = {"allowed": False, "reason": "it never ran" if not resumes or only is None else
                     "it never ran since its bundle drop; Retry it whole"}
        if not state["allowed"]:
            raise BuildRefused(f"Retry refused: {state['reason']}")
        ran = last["id"] if last else resumes[0] if resumes else ""
        members = _members(connection, assignment, current["status"])
        if not members:
            raise BuildRefused(f"Retry refused: build run {ran} has no {current['status']} member")
        # Who leaves is read from the members the retried run built (`run`), not from this filtered view: a member
        # whose item moved to done or planning (its blocked assignment cancelled, `UNBUILT`) or that is done since
        # by another build (sd:9015) is left out too, as one `only` omits, so the split guard reads the branch for
        # its earlier work. A member an operator cancelled on its own (`ALONE`) left already (sd:9222), and one
        # Retry would not end (not `AGAIN`, as a member delivered `done` beside a skipped lead) stays as it is.
        # A drop's kept lead that never ran has no run to read them from: its members in its status are what the
        # split branch holds, so one this Retry omits is marked left out too (review of build/sd-1306).
        built = [member for member in (run(connection, last["id"])["members"][1:] if last else [
                     dict(row) for row in connection.execute(
                         "SELECT id, item, status, result FROM assignment WHERE parent = ? AND status = ? "
                         "AND scope = ? ORDER BY id", (assignment, current["status"], SCOPE))])
                 if member["status"] in AGAIN and not (member["result"] or "").startswith(ALONE)]
        items = reads.items_by_ids(connection, [current["item"], *(member["item"] for member in members + built)])
        # A done lead refuses: the claim would lead with a member, on a new branch and worktree.
        if _done(items, current["item"]):
            raise BuildRefused(f"Retry refused: its lead sd:{current['item']} is done")
        if only is not None and current["item"] not in only and {member["item"] for member in members} - set(only):
            raise BuildRefused(f"Retry refused: a retry of part of the bundle names its lead sd:{current['item']}, "
                               f"whose worktree holds its work")
        members = [member for member in members if (only is None or member["item"] in only)
                   and not _done(items, member["item"])]
        stamp = clock()
        # A dependency inside the bundle (`_inside`, review of build/sd-1306): a lead that waits on its members
        # cannot retry, so they leave and build fresh on their own branch while the lead stays ended; a member
        # that waits on another item of the bundle leaves this Retry, parked.
        ahead, behind = _inside(connection, current["item"],
                                [member for member in members if _current(connection, member)])
        if ahead:
            if finding:  # these build fresh, with no builder to send it to; the lane-failed path refuses it too
                names = ", ".join("sd:" + str(member["item"]) for member in ahead)
                raise BuildRefused(f"Retry refused: its lead sd:{current['item']} waits on {names}, which would "
                                   "build fresh; send the finding once they are done")
            free = _free(connection, ahead, who=who, run=ran, lead=current["item"])
            fresh = queue(connection, free, who=who) if free else []
            return {"run": ran, "queued": [{"id": row["id"], "item": row["item"], "status": row["status"]}
                                                 for row in fresh]}
        members = [member for member in members if member not in behind]
        kept = {member["id"] for member in members}
        for member in [member for member in built if member["id"] not in kept]:
            owns = _current(connection, member)  # read before this row's own write below
            connection.execute("UPDATE assignment SET status = 'cancelled', ended = ?, result = ? WHERE id = ?",
                               (stamp, f"{ALONE}{who}: {LEFT_OUT}{ran}", member["id"]))
            if not owns:  # a newer build owns the item: its state is that build's, not this bundle's
                continue
            if member["item"] in items and items[member["item"]]["status"] == "blocked":
                transition(connection, member["item"], "ready", who=who,
                           reason=f"left out of a Retry of build run {ran}")
            park(connection, member["item"], ("ready",), stamp,
                 f"Build assignment {member['id']} left its bundle: a Retry of build run {ran} by {who} "
                 f"named other items; item parked; Build it to build it fresh")
        newer = [member for member in members if not _current(connection, member)]
        if newer:
            owner = connection.execute("SELECT id FROM assignment WHERE item = ? AND scope = ? ORDER BY id DESC "
                                       "LIMIT 1", (newer[0]["item"], SCOPE)).fetchone()["id"]
            raise BuildRefused(f"Retry refused: sd:{newer[0]['item']} has a newer build, assignment {owner}")
        parked = "" if finding or not last else parked_finding(last.get("detail"))
        how = f"{'Send to builder' if finding else 'Retry'} of build run {ran}"
        return {"run": ran, "queued": _queue_members(connection, members, items, lead=assignment, who=who,
                                                             finding=finding, parked=parked, how=how, verb="Retry")}


def _queue_members(connection, members: list[dict], items: dict, *, lead: int, who: str, finding: str, parked: str,
                   how: str, verb: str) -> list[dict]:
    """Queue each ended bundle member again under `lead`, its item ready and unparked, with the finding or the
    parked run's blockers as a note: Retry's and bundle drop's one path (pack sd:9709)."""
    queued = []
    riding = aboard(connection, [member["item"] for member in members])  # queued again together (class pass)
    for member in members:
        item = items.get(member["item"])
        if item is not None and item["status"] == "blocked":
            transition(connection, member["item"], "ready", who=who, reason=how)
        unpark(connection, member["item"])
        try:
            _item(connection, member["item"], lead=lead, riding=riding)
        except BuildRefused as refusal:
            raise BuildRefused(f"{verb} refused: {refusal}") from None
        fresh = queue_state(connection, member["id"])["revision"]
        queued.append(requeue(connection, member["id"], expected_revision=fresh, who=who))
        add_note(connection, member["item"], "comment", f"Build assignment {member['id']} queued again by {who}: {how}")
        # After the queue's note, where a lead with no earlier run starts its brief's findings (rounds 14 and 16).
        if finding:
            add_note(connection, member["item"], "comment", FINDING + finding)
        elif parked:
            add_note(connection, member["item"], "comment", REVIEWED + parked)
    return [{"id": row["id"], "item": row["item"], "status": row["status"]} for row in queued]


def drop_plan(connection, item: int) -> dict:
    """Bundle drop (pack sd:9709): what taking `item` out of its ended build bundle moves, or BuildRefused.

    The bundle must be one Retry takes. The kept members are its other open members, under a new lead on a new
    branch and worktree beside the old ones, which keep the dropped item's work and its ignored files: a drop in
    place left those for the kept bundle's next builder (round 13); `drop_reserve` reserves its id. `revision` is
    the lead's, which `drop_writes` checks again before it writes."""
    row = connection.execute("SELECT id, parent, result FROM assignment WHERE item = ? AND scope = ? "
                             "ORDER BY id DESC LIMIT 1", (item, SCOPE)).fetchone()
    if row is None:
        raise BuildRefused(f"Drop refused: sd:{item} has no build")
    if row["parent"] is not None and (row["result"] or "").startswith(ALONE):
        raise BuildRefused(f"Drop refused: sd:{item} left its bundle already; Build it to build it alone")
    lead = row["parent"] or row["id"]
    current = queue_state(connection, lead)
    last = current["run"]
    state = retry_state(connection, last, held=True) if last else {"allowed": False, "reason": "it never ran"}
    if not state["allowed"]:
        raise BuildRefused(f"Drop refused: {state['reason']}")
    members = _members(connection, lead, current["status"])
    # A done lead's own transition cancels its blocked row (`cancel_unwanted_blocked`) and leaves its members
    # blocked: the bundle ended blocked, so its members are the lead and the blocked rows. A cancelled member row
    # is not one: its item went to planning (and maybe back) after the run (lead review).
    if current["status"] == "cancelled" and _done(reads.items_by_ids(connection, [current["item"]]), current["item"]):
        # With no blocked row left, a cancelled member cannot say which way it went, so none is kept: the drop
        # refuses below and writes nothing.
        members = [member for member in members if member["id"] == lead] + _members(connection, lead, "blocked")
    if row["id"] not in {member["id"] for member in members}:
        raise BuildRefused(f"Drop refused: sd:{item} is not a {current['status']} member of build {lead}")
    items = reads.items_by_ids(connection, [member["item"] for member in members])
    kept = [member for member in members if member["id"] != row["id"]
            and (member["item"] not in items or items[member["item"]]["status"] not in UNBUILT)]
    # As Retry: a done lead kept as the parent would not lead the claim, which would start a member's own branch
    # and skip the cherry-picked one (round 2). Dropping the lead itself is the way out.
    if row["id"] != lead and _done(items, current["item"]):
        raise BuildRefused(f"Drop refused: its lead sd:{current['item']} is done")
    if not kept:
        raise BuildRefused(f"Drop refused: sd:{item} is the only open item of build {lead}; Retry or cancel it")
    # A design ships with code (`design_only_delivery`): one dropped builds alone, and a kept bundle of designs
    # alone leads one; either run blocks at delivery (review of sd:10096). A done design builds no more,
    # and dropping a done lead is the way to the kept commits once main delivers it (`done` below).
    if aboard(connection, [item]) and not _done(items, item):
        raise BuildRefused(f"Drop refused: sd:{item} is a design, which ships with its bundle's code; "
                           f"move it to planning to take it out")
    if aboard(connection, [member["item"] for member in kept]) == {member["item"] for member in kept}:
        raise BuildRefused(f"Drop refused: build {lead} keeps only design items without sd:{item}, and a design "
                           f"ships with code; Retry or cancel it")
    # As Retry: a kept member whose item a newer build owns would resume this older branch over that work.
    for member in kept:
        if not _current(connection, member):
            owner = connection.execute("SELECT id FROM assignment WHERE item = ? AND scope = ? ORDER BY id DESC "
                                       "LIMIT 1", (member["item"], SCOPE)).fetchone()["id"]
            raise BuildRefused(f"Drop refused: sd:{member['item']} has a newer build, assignment {owner}")
    # The kept work stays in the run's repository: a kept item moved since would queue where no claim resumes it,
    # as the claim blocks a member away from its bundle's work (round 24).
    for member in kept:
        now = items[member["item"]]["repo"] if member["item"] in items else None
        if now is None or not paths.same(now, last["repo"]):
            raise BuildRefused(f"Drop refused: sd:{member['item']} moved to {now or 'no repository'}, away from "
                               f"build run {last['id']}'s work in {last['repo']}; move it back first")
    # The kept work is in the new worktree on the machine that ran the run, not on the repo's lane as it reads now:
    # the drop runs there, and the new lead, which never ran, must route there (round 12). A lane moved since would
    # claim it elsewhere, fresh.
    ran_on = run_host(last["owner"])
    if ran_on not in lane_hosts(connection):
        raise BuildRefused(f"Drop refused: build run {last['id']} ran on {ran_on or 'the hub'}; "
                           f"drop it on {ran_on or 'the hub'}")
    lane = connection.execute("SELECT repo.lane_host FROM repo WHERE repo.path = ?", (last["repo"],)).fetchone()
    # Routed as the ended bundle: one the hub built for a satellite's lane builds on the hub again (sd:10319).
    claims = _route(connection, lane["lane_host"] if lane else None, lead)
    if claims != lane_of(connection, ran_on):
        raise BuildRefused(f"Drop refused: its kept items would build on {claims or 'the hub'}, but build run "
                           f"{last['id']} ran on {ran_on or 'the hub'}, which holds their work; Retry it whole")
    # Every item the bundle built that is not kept, as Retry reads who leaves from the run's membership: a member
    # cancelled out before (`ALONE`, round 3) or moved to planning (`UNBUILT`, round 8). Its commits stay on the old
    # branch, and the split must not carry them. Nor a done member's: reopened before enqueue, its replayed
    # `Closes:` would close it again (pack sd:9863 review).
    built = [one["item"] for one in connection.execute(
        "SELECT item FROM assignment WHERE (id = ? OR parent = ?) AND scope = ?", (lead, lead, SCOPE))]
    # The stored repo is a key (`~/...`); git needs the disk path, as `Supervisor` expands it (round 3).
    repo, work = paths.expand(last["repo"]), Path(last["work_path"])
    states = reads.items_by_ids(connection, built)
    return {"item": item, "assignment": row["id"], "lead": lead, "kept": kept, "run": last["id"],
            "aside": sorted(set(built) - {member["item"] for member in kept} | {item}), "built": sorted(set(built)),
            # A done member's commits stay aside only once the base delivers it (`sd_bundle_drop`, sd:10021);
            # so do a dropped done item's: a kept commit may need them (review of sd:10096).
            "done": sorted(one for one in set(built) - {member["item"] for member in kept}
                           if one in states and states[one]["status"] == "done"),
            "revision": current["revision"], "repo": str(repo), "work": str(work), "branch": last["branch"]}


def drop_reserve(connection, plan: dict) -> dict:
    """`plan` with its new lead: the drop queues the kept items as fresh rows under it, so the ended run keeps its
    members by `parent` (round 10). Its id names the branch and worktree the split makes, so it is reserved by a
    committed row before the split: a predicted `MAX(id) + 1` went to a build queued meanwhile, whose branch and
    worktree the refused drop's undo then removed (round 14). `drop_writes` makes the row the lead; a drop that
    does not commit deletes it (`drop_release`)."""
    with transaction(connection):
        new = connection.execute(
            "INSERT INTO assignment (role, status, scope, result) VALUES ('author', 'cancelled', ?, ?)",
            (RESERVED, f"reserved by a bundle drop of sd:{plan['item']}")).lastrowid
    return {**plan, "new_lead": new, "new_work": str(worktree(plan["repo"], new, root=Path(plan["work"]).parent)),
            "new_branch": branch(new)}


def drop_release(connection, plan: dict) -> None:
    """Delete the row `drop_reserve` made for a drop that did not commit."""
    with transaction(connection):
        connection.execute("DELETE FROM assignment WHERE id = ? AND scope = ?", (plan["new_lead"], RESERVED))


def drop_writes(connection, plan: dict, *, who: str, note: str, left: str) -> list[dict]:
    """Bundle drop's writes, once its branch holds the kept commits alone (pack sd:9709), in one transaction: the
    dropped member leaves its bundle (`ALONE`), the kept members queue as fresh rows under the new lead with `note`
    as the finding, and the dropped item queues alone as a fresh build, `left` noted on it. Each queued assignment,
    the kept first."""
    with transaction(connection):
        # The whole plan, read again: a kept member moved during the split (to planning and back, its assignment
        # cancelled) leaves the lead's revision as it was, and would queue again (round 10).
        if drop_plan(connection, plan["item"]) != {key: plan[key] for key in plan if key not in RESERVED_KEYS}:
            raise BuildRefused(f"Drop refused: build {plan['lead']} changed since the drop read it; drop again")
        stamp, lead, gone = clock(), plan["new_lead"], plan["assignment"]
        connection.execute("UPDATE assignment SET status = 'cancelled', ended = ?, result = ? WHERE id = ?",
                           (stamp, f"{ALONE}{who}: {DROPPED}{plan['run']}", gone))
        # The old rows stay in the ended run's bundle, cancelled out of it (`ALONE`) at the count of its last run,
        # which `run` and the sweep read; fresh rows queue under the reserved lead, and a claim takes a lead that
        # counts a run as its own bundle (`_bundle`). No run row backs the count.
        members: list[dict] = []
        for member in plan["kept"]:
            connection.execute("UPDATE assignment SET status = 'cancelled', ended = ?, result = ? WHERE id = ?",
                               (stamp, f"{ALONE}{who}: {DROPPED}{plan['run']}", member["id"]))
            if not members:  # the reserved row becomes the lead
                made = connection.execute(
                    "UPDATE assignment SET item = ?, lane = 'parallel', budget_minutes = ?, scope = ?, run_count = 1, "
                    "result = NULL WHERE id = ? AND scope = ?", (member["item"], BUDGET_MINUTES, SCOPE, lead, RESERVED))
                if made.rowcount != 1:
                    raise BuildRefused(f"Drop refused: the reserved assignment {lead} is gone; drop again")
                members.append({"id": lead, "item": member["item"]})
                continue
            cursor = connection.execute(
                "INSERT INTO assignment (item, role, status, parent, lane, budget_minutes, scope, run_count) "
                "VALUES (?, 'author', 'cancelled', ?, 'parallel', ?, ?, 1)",
                (member["item"], lead, BUDGET_MINUTES, SCOPE))
            members.append({"id": int(cursor.lastrowid), "item": member["item"]})
        items = reads.items_by_ids(connection, [plan["item"], *(member["item"] for member in members)])
        how = DROP_HOW.format(item=plan["item"]) + plan["run"]
        queued = _queue_members(connection, members, items, lead=lead, who=who, finding=note, parked="",
                                how=how, verb="Drop")
        # A done lead leaves its bundle and nothing more: there is nothing left to build (pack sd:9863).
        if _done(items, plan["item"]):
            return queued
        if items[plan["item"]]["status"] == "blocked":
            transition(connection, plan["item"], "ready", who=who, reason=how)
        unpark(connection, plan["item"])
        add_note(connection, plan["item"], "comment", left)
        alone = queue(connection, [plan["item"]], who=who)
        # Built alone, as the drop promises: a claim bundles only fresh rows (no parent, `run_count` 0) together,
        # and takes a row that counts a run as its own bundle, its lead (`_bundle`). No run row backs the count.
        connection.execute("UPDATE assignment SET run_count = 1 WHERE id = ?", (alone[0]["id"],))
        return queued + [{"id": row["id"], "item": row["item"], "status": row["status"]} for row in alone]


def drop_committed(connection, plan: dict) -> bool:
    """Whether `drop_writes` of `plan` committed: its dropped member reads cancelled out of this run's bundle. An
    interrupt after the commit must keep the branch the queued builds read (round 7)."""
    row = connection.execute("SELECT status, result FROM assignment WHERE id = ?", (plan["assignment"],)).fetchone()
    return (row is not None and row["status"] == "cancelled"
            and (row["result"] or "").endswith(f": {DROPPED}{plan['run']}"))


def left(connection, branch: str, items) -> set[int]:
    """Of `items`, each a member an operator cancelled on its own (`ALONE`) out of the build bundle whose run
    published `branch` (`branch_run`). A lane entry on that branch neither closes nor demands one, whatever its
    commits, title or body say: an older run's `Closes:` trailer may still cite it (review rounds 4 and 5 of sd:9222).
    The branch names the bundle whichever member the entry claims first, and a fresh build of the member elsewhere
    is another bundle and another branch; an entry on any other branch reads nothing here. The lead counts too: a
    bundle drop cancels it out (`ALONE`) when it leaves its branch (pack sd:9709, round 14)."""
    run = branch_run(connection, branch) if branch else None
    if run is None:
        return set()
    wanted = set(items)
    return {row["item"] for row in connection.execute(
        f"SELECT item FROM assignment WHERE (id = ? OR parent = ?) AND scope = ? "
        f"AND COALESCE(result, '') LIKE '{ALONE}%'",
        (run["assignment"], run["assignment"], SCOPE)) if row["item"] in wanted}


def left_out(connection, lead: int) -> list[tuple[int, int]]:
    """`(item, run)` of each member of build `lead` a subset Retry left out (`LEFT_OUT`), `run` the number of the
    run that Retry retried: what the branch and worktree held when the next run started may hold that item's
    work (pack sd:9581)."""
    left = []
    for row in connection.execute(
            "SELECT item, result FROM assignment WHERE parent = ? AND scope = ? AND result LIKE ? ORDER BY id",
            (lead, SCOPE, f"{ALONE}%: {LEFT_OUT}%")):
        # A run of another assignment, the ended run a drop's kept lead keeps the work of, reads as run 0: the
        # lead's first run is the first after the Retry (review of build/sd-1306).
        retried = connection.execute("SELECT run FROM runner_run WHERE id = ? AND assignment = ?",
                                     (row["result"].rsplit(LEFT_OUT, 1)[1], lead)).fetchone()
        left.append((row["item"], retried["run"] if retried else 0))
    return left


def left_mark(connection, lead: int) -> str:
    """The tail a member leaving build `lead`'s bundle on its own (`ALONE`) ends its result with, ": LEFT_OUT<run>"
    of the lead's latest run, so the next run's split check reads the branch for its commits, as after a subset
    Retry; "" when the lead never ran (round 21)."""
    ran = connection.execute("SELECT id FROM runner_run WHERE assignment = ? ORDER BY run DESC LIMIT 1",
                             (lead,)).fetchone()
    return f": {LEFT_OUT}{ran['id']}" if ran else ""


def split(connection, lead: int) -> str:
    """Where build `lead` stands after its latest subset Retry (pack sd:9581): "" when none left a member out;
    "unread" until a run after it checked its branch; "clear" once one found no earlier work there (it recorded
    its start head, `base_head`); "held" once one found some (its detail carries SPLIT). Held is for good: a rebase
    or rebuild gives the earlier changes new ids, so no later git state proves them gone."""
    left = left_out(connection, lead)
    if not left:
        return ""
    runs = connection.execute("SELECT detail, base_head FROM runner_run WHERE assignment = ? AND run > ?",
                              (lead, max(run for _, run in left))).fetchall()
    if any(SPLIT in (run["detail"] or "") for run in runs):
        return "held"
    return "clear" if any(run["base_head"] for run in runs) else "unread"


def _current(connection, member: dict) -> bool:
    """Whether no newer build of bundle `member`'s item stands, open, blocked or delivered: only then may this
    bundle write the item's status or parking; a newer build owns them otherwise (pack sd:9581). A newer one
    that ended before it ever ran (cancelled, or a refused press) owns nothing, so the older bundle, whose
    branch holds the item's work, may retry it (sd:9788); one cancelled after it ran keeps newer work in its
    worktree, and owns the item (review round 3)."""
    return connection.execute(f"SELECT 1 FROM assignment WHERE item = ? AND scope = ? AND id > ? "
                              f"AND NOT (status IN {AGAIN} AND run_count = 0)",
                              (member["item"], SCOPE, member["id"])).fetchone() is None


def split_held(connection, item: int) -> bool:
    """Whether the build bundle Build reads for `item` (`_retry_row`) is held after a split (`split`): Build takes
    it fresh, blocked as it is, on a new branch; Retry refuses that bundle (pack sd:9581). The same row Retry
    would take, not merely the newest, which may be a cancelled build (review of sd:9788)."""
    row = _retry_row(connection, item)
    return (row is not None and row["status"] in AGAIN and not _alone(connection, row["id"])
            and split(connection, row["parent"] or row["id"]) == "held")


def queued_members(connection, lead: int) -> list[tuple[int, int]]:
    """`(assignment, item)` of each queued member of a build `lead`: what a cancel of the queued lead cancels and
    parks with it, and what its confirm names (sd:9222)."""
    return [(row["id"], row["item"]) for row in connection.execute(
        "SELECT id, item FROM assignment WHERE parent = ? AND status = 'queued' AND scope = ? ORDER BY id",
        (lead, SCOPE))]


def _alone(connection, assignment: int) -> bool:
    """Whether an operator cancelled this bundle member on its own (`ALONE`)."""
    row = connection.execute("SELECT result FROM assignment WHERE id = ?", (assignment,)).fetchone()
    return row is not None and (row["result"] or "").startswith(ALONE)


def _done(items: dict, item: int) -> bool:
    return item in items and items[item]["status"] == "done"


def queue_again(connection, items: list[int], *, who: str, finding: str = "") -> list[dict]:
    """`sd assignments build`: an item whose latest build bundle ran and ended blocked or cancelled is Retry of that
    bundle (`retry`, its assignments and worktree kept); one whose lane entry failed is Fix (`lane_fix`, pack sd:8791);
    every other item is the Build press (`queue`), a `planning` one set ready first: the one verb that starts a
    build, so a task just filed builds with no status step (sd:9063). One transaction, so one refusal writes
    nothing, the ready step included (pack sd:8785).
    A `finding` is Send to builder on a Retry or a Fix; a fresh build has no builder to send it to (pack sd:8792)."""
    if not items or len(set(items)) != len(items) or any(type(item) is not int for item in items):
        raise BuildRefused("select one or more distinct items")
    finding = finding.strip()
    with transaction(connection):
        leads, fixes, fresh = [], [], []
        held: set[int] = set()
        named: dict[int, set[int]] = {}
        # Each lane-failed run this press split, and every member of its bundle: the split settled each, so a
        # later one of them is done, whatever the order (review of build/sd-1306).
        freed: dict[str, set[int]] = {}
        for item in items:
            # The split this press made settled it: `_free` queued it if it waits on nothing, and the lead stays
            # lane_failed for Fix. Read before `alone`, which the split itself sets (review of build/sd-1306).
            if any(item in done for done in freed.values()):
                continue
            row = _retry_row(connection, item)
            # A member cancelled on its own left its bundle: building it is a fresh press, not the bundle's Retry.
            alone = row is not None and row["parent"] is not None and _alone(connection, row["id"])
            lead = row and (row["parent"] or row["id"])
            state = queue_state(connection, lead) if lead else None
            # A bundle whose lane entry failed is the panel's Fix when its check passes (pack sd:8791); a state Fix
            # refuses, such as an item moved back to ready, is a fresh press with that press's own check.
            # A member that left the bundle (`alone`) is a fresh press, never the bundle's Fix (review of build/sd-1306).
            if state and not alone and (state["run"] or {}).get("outcome") == "lane_failed":
                # The members its lead waits on leave first, so they build on their own (review of build/sd-1306).
                sent = _sent_back(connection, state["run"])
                ahead = _inside(connection, sent[0]["item"], sent)[0] if sent else []
                if ahead:
                    # Refuse before `_free` writes, as `retry` does (lead triage of build/sd-1306): a finding has
                    # no builder to go to, and only the lead or a member it waits on frees what it needs.
                    lead_item = sent[0]["item"]
                    names = ", ".join("sd:" + str(member["item"]) for member in ahead)
                    if finding:
                        raise BuildRefused(f"--finding goes to a Retry or a Fix; its lead sd:{lead_item} waits on "
                                           f"{names}, which would build fresh; send the finding once they are done")
                    # The selection, not this item, decides: with the lead or a member it waits on in it, the
                    # split settles every selected member of the bundle (review of build/sd-1306).
                    if not set(items) & {lead_item, *(member["item"] for member in ahead)}:
                        raise BuildRefused(f"its lead sd:{lead_item} waits on {names}; Build sd:{lead_item} or "
                                           f"{names} to build those first, then Fix the bundle")
                    # A lane entry sent back to the lane is the lane's again: the bundle's PR may still merge, so
                    # no member leaves it, as Fix reads it (`_lane_entry`, review of build/sd-1306).
                    entry = LANE_ENTRY.match(state["run"]["detail"] or "")
                    _lane_entry(connection, state["run"]["branch"], entry.group(1) if entry else None, actor="Build")
                    fresh += _free(connection, ahead, who=who, run=state["run"]["id"], lead=lead_item)
                    freed[state["run"]["id"]] = {member["item"] for member in sent}
                    continue
                try:
                    members = lane_fix_check(connection, state["run"]["id"])[0]
                except BuildRefused:
                    if _waiting(connection, _sent_back(connection, state["run"])):
                        raise  # a Fix that waits on a dependency is no fresh press either
                    members = []
                if item in [member["item"] for member in members]:
                    fixes += [] if state["run"] in fixes else [state["run"]]
                    continue
            # A build cancelled before any claim, or one the claim refused (`_refuse`), never ran: no worktree to
            # resume, so it is a fresh press, as the dashboard's "it never ran" hint says. A bundle may end mixed
            # (`enqueued` marks a delivered member done): `retry` takes only members in the lead's status, so the
            # item's own assignment must be in it, or Retry would requeue the lead's items and not this one.
            # A skipped lead of a run still in the lane is no ended bundle: the run holds it no longer (sd:8960).
            if split_held(connection, item):  # its branch may hold a left item's work: a fresh press, new branch
                fresh.append(item)
                held.add(item)  # `queue` takes it blocked as it stands: no question holds it (lane review)
                continue
            # A drop's kept lead never ran, and its Retry resumes the split branch (`retry`, review of build/sd-1306).
            if (state and state["status"] in AGAIN and row["status"] == state["status"] and not alone
                    and (state["run"] is not None
                         or _resumable(connection, lead, state["item"], state["status"]) is not None)
                    and not (state["run"] and state["run"]["outcome"] is None and state["run"]["step"] in LANE_STEPS)
                    and not _done(reads.items_by_ids(connection, [state["item"]]), state["item"])):
                leads += [] if lead in leads else [lead]
                named.setdefault(lead, set()).add(item)
            else:
                fresh.append(item)
        if finding and fresh:
            raise BuildRefused(f"--finding goes to a Retry or a Fix; {', '.join(f'sd:{item}' for item in fresh)} "
                               "would be a fresh build, with no builder to send it to")
        # One named item is the row's Retry of its whole bundle; two or more name the part to retry (pack sd:9581).
        queued = []
        for lead in leads:
            built = retry(connection, lead, who=who, finding=finding,
                          only=named[lead] if len(named[lead]) > 1 else None)["queued"]
            # Every selected item is built or the press refuses, read from what Retry queued, not predicted: its
            # split (`_inside`) frees only the members the lead waits on, each that waits on nothing, or parks a
            # member that waits on bundle work (class pass, review of build/sd-1742). The lead alone is excused:
            # a Build of it frees what it waits on, as Retry of its row does. The refusal undoes Retry's writes.
            lead_item = queue_state(connection, lead)["item"]
            for item in sorted(named[lead] - {entry["item"] for entry in built} - {lead_item}):
                names = ", ".join(f"sd:{entry['item']}" for entry in built) or "no item now"
                raise BuildRefused(f"sd:{item} would stay unbuilt: a Retry of its bundle builds only {names}; "
                                   f"Build its lead sd:{lead_item} alone first, then Build sd:{item}")
            queued += built
        for run in fixes:
            fixed = lane_fix(connection, run["id"],
                             finding or f"the lane entry failed; fix what it names: {run['detail']}"[:2000], who=who)
            queued += [{"id": member["id"], "item": member["item"], "status": "queued"}
                       for member in _members(connection, run["assignment"], "queued") if member["item"] in fixed]
        if fresh:
            from .workflow import change_status, item_state
            for item in fresh:
                state = item_state(connection, item)
                if state["item"]["status"] == "planning":
                    change_status(connection, item, "ready", who=who, expected_revision=state["revision"])
                elif (state["item"]["status"] == "blocked" and item not in held
                      and (since := _build_blocked(connection, item)) is not None):
                    # A member its run blocked, whose bundle cannot retry (its lead done: the code shipped
                    # without it), builds fresh once its builder's question is answered (pack sd:10294): one
                    # that run raised, as delivery read it (`asked_since`), not an older unrelated one.
                    _answered(connection, item, since)
                    transition(connection, item, "ready", who=who, reason="Build after its bundle ended")
        fresh = list(dict.fromkeys(fresh))
        return queued + (queue(connection, fresh, who=who) if fresh else [])


def _build_blocked(connection, item: int) -> str | None:
    """When the build run that blocked `item` reads questions from (`asked_since`), if `item`'s block
    is that build's, which a Build lifts; else None. The block is the build's while that assignment ended
    blocked and the item's latest status change is the build's move to blocked with that assignment's
    reason. A block someone wrote after it, an operator's hold, is not the build's (review of build/sd-1867)."""
    row = connection.execute("SELECT id, parent, status, result FROM assignment WHERE item = ? AND scope = ? "
                             "ORDER BY id DESC LIMIT 1", (item, SCOPE)).fetchone()
    if row is None or row["status"] != "blocked" or not row["result"]:
        return None
    moved = connection.execute("SELECT body FROM note WHERE item = ? AND kind = 'status_change' "
                               "ORDER BY id DESC LIMIT 1", (item,)).fetchone()
    if moved is None or re.fullmatch(r"\w+ -> blocked by build: " + re.escape(row["result"]),
                                     moved["body"] or "", re.DOTALL) is None:
        return None
    lead = row["parent"] or row["id"]
    number = _blocking_run(connection, lead, row["result"])
    # A block that names no run of its bundle reads every question: no time stands in for the run.
    return asked_since(connection, lead, number) if number is not None else ""


def _rebuild_since(connection, item: int) -> str | None:
    """`_build_blocked` for an item a fresh press may take blocked: its bundle Retry no longer takes, as
    its lead ended otherwise or its lead's item is done (the code shipped without it, pack sd:10294).
    The dashboard's press and dialog read it through `_item`, as Build by item does (review of sd-1867)."""
    since = _build_blocked(connection, item)
    if since is None:
        return None
    row = connection.execute("SELECT id, parent FROM assignment WHERE item = ? AND scope = ? "
                             "ORDER BY id DESC LIMIT 1", (item, SCOPE)).fetchone()
    state = queue_state(connection, row["parent"] or row["id"])
    if state["status"] == "blocked" and not _done(reads.items_by_ids(connection, [state["item"]]), state["item"]):
        return None  # Retry of the bundle resumes its branch
    return since


def _answered(connection, item: int, since: str) -> None:
    """Refuse while a question the blocking run raised on `item` still waits on the operator."""
    if asked := open_question(connection, [item], since, pending=True):
        raise BuildRefused(f"item {item} waits on question note #{asked['id']}; answer it, then Build it")


def _blocking_run(connection, lead: int, reason: str) -> int | None:
    """The number of the run of `lead` that a build block's `reason` names, or None. Every block a run writes
    (`_block`) begins `build run <id> `, so the run is read by its id, never by time or as the newest: a
    lane Fix runs the bundle again after it, even in the block's own second, and did not build the blocked
    item, so its start would hide the question the item waits on (class pass of build/sd-1867)."""
    named = re.match(r"build run ([0-9a-f]{32}) ", reason)
    row = named and connection.execute("SELECT run FROM runner_run WHERE id = ? AND assignment = ?",
                                       (named.group(1), lead)).fetchone()
    return row["run"] if row else None


def _retry_row(connection, item: int):
    """The build assignment `sd assignments build` reads for `item`: its newest that ran, past newer ones that
    never ran (cancelled or blocked before a claim), when the newest was cancelled before it ran: the bundle that
    ran last holds the item's newest work (sd:9788, review rounds 2 and 3). A cancelled bundle that ran keeps its
    worktree, so it stays the one read, as does a member a subset Retry left out. A newest row that ran, or no
    older row that ran in a bundle ended as it did and not left on its own, reads the newest."""
    rows = connection.execute("SELECT id, item, parent, status, run_count FROM assignment WHERE item = ? "
                              "AND scope = ? ORDER BY id DESC", (item, SCOPE)).fetchall()
    if rows and rows[0]["status"] == "cancelled" and not rows[0]["run_count"]:
        ran = next((row for row in rows[1:] if row["run_count"]), None)
        # Only a bundle Retry can take: every member it would queue owns its item (`_current`), or Retry refuses the
        # whole press and Build reads a fresh one instead (class pass, review round 7); and every member in the
        # work's repository (`_in_work_repo`), or the claim refuses it and every later press reads it again (round 14).
        if ran is not None and ran["status"] in AGAIN and not _alone(connection, ran["id"]):
            lead: int = ran["parent"] or ran["id"]
            # Its claim routes where its last run ran, which holds its worktree: not a bundle a satellite ran whose
            # lane moved to another satellite or to the hub (reviews of build/sd-1378), as `_stranded` reads for
            # beat recovery (pack sd:9964).
            host = connection.execute(_REPO, (ran["item"],)).fetchone()["lane_host"]
            if (queue_state(connection, lead)["status"] == ran["status"]
                    and _in_work_repo(connection, lead, ran["status"], retry=item)
                    and _ran_on(connection, lead) == _route(connection, host, lead)
                    and _retry_takes(connection, lead, ran["status"])):
                return ran
    return rows[0] if rows else None


def _retry_takes(connection, lead: int, status: str) -> bool:
    """Whether Retry queues build `lead`'s bundle in `status`, read, not tried: its lead's item is not done, and
    each member it would queue (`_open_members`) owns its item (`_current`) and passes the press's check once
    ready (`_press_refusal`), as `retry` and its `_queue_members` refuse otherwise (class pass, review of sd:9788).
    Build by item reads a fresh press instead of a bundle whose Retry refuses.
    Retry's dependency split (`_inside`) refuses neither case: members the lead waits on leave to build on their
    own, and a member that waits on another bundle item leaves parked, so neither is press-checked (sd:10022)."""
    row = connection.execute("SELECT item FROM assignment WHERE id = ?", (lead,)).fetchone()
    if row is None or _done(reads.items_by_ids(connection, [row["item"]]), row["item"]):
        return False
    members = _open_members(connection, lead, status)
    if not members or not all(_current(connection, member) for member in members):
        return False
    ahead, behind = _inside(connection, row["item"], members)
    riding = aboard(connection, [member["item"] for member in members if member not in behind])
    return bool(ahead) or all(
        _press_refusal(connection, connection.execute(_REPO, (member["item"],)).fetchone(), lead=lead,
                       riding=riding) is None
        for member in members if member not in behind)


def eligible(connection, *, now: str | None = None) -> list[dict]:
    """The ready items a build may take, highest priority first: auto-pickup's selection.

    `reads.backlog_items` gives the unparked rows in priority order; a
    recurring row, every row `queue` would refuse and another host's lane
    are left out: each lane host's dispatcher picks for its own lanes,
    under its own caps, and its claim takes the row (sd:8393, pack sd:7933).
    A recurring row is a chore, not code: a weekly one was picked up and
    skipped (pack sd:8861); a press still takes one by hand.
    """
    hosts, found = lane_hosts(connection), []
    # An item whose own build the hub blocked on a quiet dispatcher waits for that build to queue again
    # (`_requeue_beat_blocked`): a fresh one from main drops its branch's work (sd:9774).
    # Only one recovery would queue (`_recoverable`): pickup builds any other fresh, as one stranded on a
    # satellite its lane left (round 4 of sd:9774).
    waiting = {row["item"] for row in _recoverable(connection)}
    for row in reads.backlog_items(connection, status="ready", now=now):
        if row["recurrence"] or row["id"] in waiting:  # a scheduled occurrence recurs too
            continue
        try:
            taken = _item(connection, row["id"])
        except BuildRefused:
            continue
        if lane_of(connection, taken["lane_host"]) not in hosts:
            continue
        if work_kind(connection, row["id"]) == DESIGN_KIND and connection.execute(
                "SELECT 1 FROM assignment WHERE id = (SELECT MAX(id) FROM assignment WHERE item = ? AND scope = ?) "
                "AND status = 'blocked'", (row["id"], DRAFT)).fetchone():
            # A blocked design run waits on the operator's Build, as a blocked build does; picked again, it
            # ran again every tick (review of sd:10096).
            continue
        found.append({"id": row["id"], "title": row["title"], "repo": row["repo"], "priority": row["priority"],
                      "fields": row["fields"]})
    return found


def pickup_state(connection, *, now: str | None = None) -> dict:
    """Auto-pickup now: `on`, `value` (the text stored, or None) and `since` (when the setting was last written).

    Only "on" reads on: any other value, such as an old build's end, is off, never on. `now` is unused and kept
    for the callers that pass the tick's clock."""
    value = read_settings(connection, [AUTO_PICKUP]).get(AUTO_PICKUP)
    row = connection.execute("SELECT timestamp FROM state WHERE kind = 'setting' AND key = ? ORDER BY id DESC LIMIT 1",
                             (AUTO_PICKUP,)).fetchone()
    return {"on": value == "on", "value": value, "since": row["timestamp"] if row else None}


def set_pickup(connection, on: bool, *, now: str | None = None, before: Any = UNCHECKED) -> dict:
    """On writes "on"; Off writes none. The state after. `before`, when given, is the value the caller read:
    one that moved since is `SettingStale`, and nothing is written (pack sd:10001)."""
    set_setting(connection, AUTO_PICKUP, "on" if on else None, before=before)
    return pickup_state(connection, now=now)


def picked(connection, lead: int) -> bool:
    """Whether auto-pickup queued the bundle whose lead assignment is `lead`, or the design run `lead`."""
    return connection.execute(
        "SELECT 1 FROM note JOIN assignment ON assignment.item = note.item WHERE assignment.id = ? "
        "AND note.body IN (?, ?)",
        (lead, f"Build assignment {lead} queued by {PICKUP}",
         f"Design assignment {lead} queued by {PICKUP}")).fetchone() is not None


def pickup(connection, available: dict, *, now: str | None = None) -> list[int]:
    """One tick of auto-pickup while it is on: queue `eligible` items, as `PICKUP`, to fill the room; the items.

    The press queue goes first: while any build of this host's lanes is
    queued in a repository with room, nothing is picked and the claim
    drains it. A press waiting on a full repository holds no other one: at
    one bundle building per repository it may wait a whole build (pack sd:8158).
    Each free builder slot takes one bundle (`bundled`) of
    one repository, each repository at most its `room_for`. No room, or no
    item, queues nothing.
    """
    if available["why"] or not pickup_state(connection, now=now)["on"]:
        return []
    # Only this host's presses hold it: the hub never claims a satellite's.
    hosts = lane_hosts(connection)
    with transaction(connection):  # a build blocked on a quiet beat is this host's press again (sd:9775)
        _requeue_beat_blocked(connection, hosts)
    # A ride row waits for a code bundle, not the claim, so it holds no pickup (review of sd:10096).
    if any(_route(connection, row["lane_host"], _lead(row)) in hosts
           and room_for(available, row["repo"], p1=_leads_beside(row)) > 0 and not _rides(connection, row)
           for row in connection.execute(
            f"SELECT assignment.id, assignment.item, assignment.parent, assignment.run_count, item.repo, "
            f"{LEAD_PRIORITY}, "
            "repo.lane_host "
            "FROM assignment JOIN item ON item.id = assignment.item "
            "LEFT JOIN repo ON repo.path = item.repo WHERE assignment.status = 'queued' AND assignment.scope = ?",
            (SCOPE,))):
        return []
    slots, chosen = available["machine"], []
    bundles: dict[str, list[list[int]]] = {}
    urgent: list[tuple[dict, list[int]]] = []  # each P1 bundle's lead and items, as the claim forms them
    for row in eligible(connection, now=now):
        repo = row["repo"]
        taken = bundles.setdefault(repo, [])
        near = next((items for lead, items in urgent if lead["repo"] == repo and kin(lead, row)
                     and bundled(connection, [*items, row["id"]]) > len(items)), None)
        if near is not None:
            near.append(row["id"])
        elif beside(row):
            if slots <= 0 or room_for(available, repo, p1=True) <= 0:
                continue
            urgent.append((row, [row["id"]]))
            slots -= 1
        elif taken and bundled(connection, [*taken[-1], row["id"]]) > len(taken[-1]):
            taken[-1].append(row["id"])
        elif slots > 0 and len(taken) < room_for(available, repo):
            taken.append([row["id"]])
            slots -= 1
        else:
            continue
        chosen.append(row["id"])
    if not chosen:
        return []
    try:
        queue(connection, chosen, who=PICKUP)
    except BuildRefused:
        return []  # an item moved since `eligible` read it: the next tick reads again
    return chosen


def stop_on_blocks(connection, *, now: str | None = None) -> str | None:
    """Two auto-pickup bundles in a row that end blocked, since On was written, turn auto-pickup off with no push.

    A bundle ends `done` (it reached the lane) or `blocked`; a blocked run,
    a second loss and a denied run each end it blocked. A cancel is the
    operator's and does not count, nor does a build the claim refused before
    it started (`started` NULL): a dependency wait, a moved item or a quiet
    lane is no build failure (review of pack sd:9570). The note on the
    latest one's item says why. The reason, or None.
    """
    state = pickup_state(connection, now=now)
    if not state["on"]:
        return None
    rows = connection.execute(
        "SELECT id, item, status FROM assignment WHERE scope IN (?, ?) AND parent IS NULL "
        "AND status IN ('done', 'blocked') AND started IS NOT NULL AND ended >= ? ORDER BY ended DESC, id DESC",
        (SCOPE, DRAFT, state["since"])).fetchall()
    ended = [row for row in rows if picked(connection, row["id"])][:2]
    if len(ended) < 2 or any(row["status"] != "blocked" for row in ended):
        return None
    reason = (f"auto-pickup is off: build assignments {ended[1]['id']} and {ended[0]['id']} ended blocked in a row; "
              f"turn it on again on Queue once they are looked at")
    with transaction(connection):
        set_setting(connection, AUTO_PICKUP, None)
        add_note(connection, ended[0]["item"], "comment", reason[0].upper() + reason[1:])
    return reason


def health_failed(connection, ident: str, why: str) -> int:
    """A failed health check after an auto-pickup merge: auto-pickup off first, then one item filed per branch,
    in one transaction; the tick's push reads the item (`today.push_due`). No revert. The item's id."""
    row = run(connection, ident)
    title = f"{HEALTH}{row['branch']}"
    with transaction(connection):
        set_setting(connection, AUTO_PICKUP, None)
        found = connection.execute("SELECT id FROM item WHERE title = ? AND status != 'done'", (title,)).fetchone()
        if found:
            return found["id"]  # a check run again after a kill files nothing twice
        filed = create_item(connection, kind="task", title=title, repo=row["repo"])
        add_note(connection, filed, "comment",
                 f"Build run {ident} merged {row['branch']}, and the health check after it failed: {why}. Auto-pickup "
                 f"({AUTO_PICKUP}) is off; nothing was reverted. Look at `dashboard.sh health`, then turn it on again.")
        for member in row["members"]:
            add_note(connection, member["item"], "comment",
                     f"Build run {ident}: health failed after the merge; auto-pickup is off, item {filed} filed")
        return filed


def _key(repo: str | None) -> str | None:
    return paths.key(repo) if isinstance(repo, str) and repo.startswith("/") else repo


#: `room`'s answers for an input it did not read: the tick claims nothing and says which; the tick's beat may add
#: the reason after the constant (pack sd:8606).
UNREAD = ("the load was not read", "the interactive builders were not counted")
#: Why nothing claims while a restore reimport is pending: the press, the claim and the tick's beat say it.
RESTORING = "database restore reimport is incomplete; builds are held"


def unread(load: float | None, load_cap: float | None, interactive: list[dict] | None) -> str:
    """`room`'s `UNREAD` answer for these inputs, or "": the tick reads it before its beat, `room` after."""
    if not (isinstance(load, (int, float)) and isinstance(load_cap, (int, float))
            and math.isfinite(load) and math.isfinite(load_cap)):
        return UNREAD[0]
    return UNREAD[1] if interactive is None else ""


#: Builders start only while load5 is at most this many times the cores: the builder's own cap, apart from the
#: gate's `sd.gate_load_max`, which is off since sd:2607 as macOS load counts disk waits (operator, sd:9094).
LOAD_FACTOR = 1.5


def load_cap() -> float:
    """The five-minute load builders start under, `LOAD_FACTOR` times the cores; NaN, unread, when no count."""
    cores = os.cpu_count()
    return LOAD_FACTOR * cores if cores else math.nan


def counted(answer: dict, load5: float) -> float:
    """The five-minute load a cap reads from an `sd gate status --json` answer: its `load_counted`, load5 with the
    CPU's own use standing in while the CPU is not busy (sd:9946), else `load5` where the gate gave none."""
    value = answer.get("load_counted")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return load5
    try:
        value = float(value)  # a JSON integer past the float range raises OverflowError
    except OverflowError:
        return load5
    if not math.isfinite(value) or value < 0:
        return load5
    return min(value, load5)


def over_load(load: float, cap: float) -> str:
    """Why no builder starts under this five-minute load, or "" (pack sd:6111).

    `cap` is `load_cap`; a cap of 0 or less is none.
    """
    return f"the five-minute load is {load:g}, above the cap of {cap:g}" if 0 < cap < load else ""


def hold_key(host: str) -> str:
    """The setting that holds `host`'s builds after a usage limit: `HOLD` and the host as a key segment."""
    name = re.sub(r"[^a-z0-9_]", "_", host.lower())
    return HOLD + (name if name[:1].isalpha() else f"h_{name}")


def hold_until(final: str | None, stamp: str) -> str:
    """When a hold that starts at `stamp` ends, read from the run's result line `final`: its
    `rate_limit_info.resetsAt`, epoch seconds, when that is after `stamp` and at most `HOLD_MAX_SECONDS` on;
    any other value or shape holds `HOLD_SECONDS` (pack sd:9907)."""
    start = datetime.fromisoformat(stamp)
    try:
        line = json.loads(final or "null")
    except (ValueError, RecursionError):
        line = None
    info = line.get("rate_limit_info") if isinstance(line, dict) else None
    reset = info.get("resetsAt") if isinstance(info, dict) else None
    if isinstance(reset, (int, float)) and not isinstance(reset, bool):
        try:
            at = datetime.fromtimestamp(reset, UTC)
        except (OverflowError, OSError, ValueError):
            at = None
        if at is not None and start < at <= start + timedelta(seconds=HOLD_MAX_SECONDS):
            return at.isoformat(timespec="seconds")
    return (start + timedelta(seconds=HOLD_SECONDS)).isoformat(timespec="seconds")


def rate_hold(connection, *, now: str | None = None) -> str:
    """Why this host claims nothing after a usage limit (`RATE_HELD` and its end), or "" when no hold is ahead.
    A hold that does not read holds: an unread value says nothing about the limit."""
    key = hold_key(this_host())
    try:
        until = read_settings(connection, [key]).get(key)
        if not until or datetime.fromisoformat(until) <= datetime.fromisoformat(now or clock()):
            return ""
    except (SdDbError, ValueError, TypeError) as error:
        return f"{RATE_HELD}an unknown time: {key} does not read ({error}); unset it or set an ISO time"
    return f"{RATE_HELD}{until} ({key}; unset it to build sooner)"


def room(connection, *, load: float | None, load_cap: float | None, interactive: list[dict] | None,
         hosts: Collection[str | None] | None = None) -> dict:
    """Builders that may start now: `machine`, `used` and `held` by repository, and `why` there is none.

    `load` and `load_cap` are the five-minute load and its cap from `sd gate
    status --json`, a cap of 0 or less meaning none; `interactive` is one
    `{repo, session_id}` per interactive builder, `repo` its key or None
    when its cwd names none. Either unread
    (None) is no room, never room. A headless run's own session is not counted
    twice. A repository holding an exclusive or quarantined lease of any other
    kind has no room (`held`). Only the runs of `hosts` (`lane_hosts` when
    None) count: each host's dispatcher has its own caps (pack sd:7933).
    """
    hosts = lane_hosts(connection) if hosts is None else hosts
    why = unread(load, load_cap, interactive)
    leases = connection.execute(
        "SELECT lease.repo, lease.exclusive, run.quarantine, run.session_id, run.owner, assignment.scope, "
        f"item.priority, json_extract(CASE WHEN json_valid(item.fields) THEN item.fields END, '$.{SLOT}') "
        "AS slot FROM runner_lease lease JOIN runner_run run ON run.id = lease.run "
        "LEFT JOIN assignment ON assignment.id = run.assignment LEFT JOIN item ON item.id = assignment.item "
        "WHERE lease.released_at IS NULL").fetchall()
    builds = [row for row in leases if row["scope"] == SCOPE and mine(row, hosts)]
    drafts = [row for row in connection.execute(
        "SELECT runner_run.session_id, runner_run.owner FROM runner_run JOIN assignment "
        "ON assignment.id = runner_run.assignment WHERE runner_run.released_at IS NULL AND assignment.scope = ?",
        (DRAFT,)).fetchall() if mine(row, hosts)]
    # A run at a lane step released its lease and holds no slot: the lane is serial on its own, so the next
    # bundle builds while it lands (sd:8277). Its session is still headless, not an interactive builder.
    marks = ", ".join("?" for _ in LANE_STEPS)
    lane = [row for row in connection.execute(
        f"SELECT runner_run.session_id, runner_run.owner FROM runner_run JOIN assignment "
        f"ON assignment.id = runner_run.assignment WHERE runner_run.released_at IS NULL "
        f"AND assignment.scope = ? AND runner_run.step IN ({marks})",
        (SCOPE, *LANE_STEPS)).fetchall() if mine(row, hosts)]
    headless = {row["session_id"] for row in [*builds, *drafts, *lane] if row["session_id"]}
    used: dict[str, int] = {}
    # Every host's builds hold a repository's slot: the hub may build a satellite's lane (sd:8991). A P1-led
    # run builds beside the bundle and holds none (sd:9700).
    for row in leases:
        if row["scope"] == SCOPE and _holds_slot(row):
            used[row["repo"]] = used.get(row["repo"], 0) + 1
    others = [entry for entry in interactive or [] if not (entry.get("session_id") and entry["session_id"] in headless)]
    for entry in others:
        repo = _key(entry.get("repo"))
        if repo:
            used[repo] = used.get(repo, 0) + 1
    held = sorted({row["repo"] for row in leases if row["scope"] != SCOPE and (row["exclusive"] or row["quarantine"])})
    cap = machine_cap()
    machine = cap - len(builds) - len(drafts) - len(others)
    if not why and isinstance(load, (int, float)) and isinstance(load_cap, (int, float)):
        why = over_load(load, load_cap)
    why = why or rate_hold(connection)
    if not why and machine <= 0:
        why = f"{cap - machine} builders run already; the cap is {cap}"
    return {"machine": 0 if why else machine, "used": used, "held": held, "why": why}


def room_for(available: dict, repo: str, *, p1: bool = False) -> int:
    """`room`'s answer for one repository: at most `repo_cap()` builders there, and none while it is held.

    `p1` asks for a P1 bundle, which builds beside the repository's own under the machine cap alone (sd:9700).
    """
    if available["why"] or repo in available["held"]:
        return 0
    if p1:
        return available["machine"]
    return max(0, min(available["machine"], repo_cap() - available["used"].get(repo, 0)))


#: The `fields.work_kind` of a design-only item (sd:10096): a design run writes its design, and the item then
#: rides the repository's next code bundle, which commits it; it never leads a bundle and takes no slot.
DESIGN_KIND = "design"


def work_kind(connection, item: int) -> str | None:
    """`item`'s `fields.work_kind`, or None: `mechanical` is half a slot, `design` none."""
    row = connection.execute("SELECT fields FROM item WHERE id = ?", (item,)).fetchone()
    try:
        fields = json.loads(row["fields"] or "{}") if row else {}
    except ValueError:
        fields = {}
    kind = fields.get("work_kind") if isinstance(fields, dict) else None
    return kind if isinstance(kind, str) else None


def _mechanical(connection, item: int) -> bool:
    """Whether `item`'s `fields.work_kind` is `mechanical`: the one size signal an item carries."""
    return work_kind(connection, item) == "mechanical"


#: What `sd.build_model` may hold: a model alias or id the builder's `--model` takes (sd:9942).
BUILD_MODEL = r"[a-z][a-z0-9.-]{0,63}"


def build_model(environ: dict[str, str] | None = None) -> str | None:
    """This machine's `sd.build_model`, or None when unset or malformed (sd:9942)."""
    return _machine_value("build_model", BUILD_MODEL, environ)


def _model(connection, items: list[int]) -> str:
    """The builder's model: `sd.build_model` when set; else sonnet for an all-mechanical bundle, opus otherwise."""
    return build_model() or (SONNET if all(_mechanical(connection, item) for item in items) else OPUS)


def _machine_value(key: str, pattern: str, environ: dict[str, str] | None) -> str | None:
    """This machine's `sd.<key>` from the pack's machine config when it matches `pattern`, else None.

    The machine that claims a bundle reads its own, as `sd.gate_slots` is read
    (`sd_lib.file_setting`); a value that does not match `pattern` reads as
    unset, so a bad file never stops a dispatcher.
    """
    env = os.environ if environ is None else environ
    home = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or Path.home()) / ".config")
    try:
        config = json.loads((Path(home) / "sd-ai-command-pack" / "config.json").read_text()).get("config", {})
        value = config.get("sd", {}).get(key)
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def _machine_setting(key: str, pattern: str, default: int, environ: dict[str, str] | None) -> int:
    """This machine's `sd.<key>` as a number, `default` when unset or malformed."""
    value = _machine_value(key, pattern, environ)
    return int(value) if value is not None else default


def bundle_max(environ: dict[str, str] | None = None) -> int:
    """This machine's `sd.bundle_max`, 1 to 99; `BUNDLE_MAX` when unset (sd:9109)."""
    return _machine_setting("bundle_max", "[1-9][0-9]?", BUNDLE_MAX, environ)


def machine_cap(environ: dict[str, str] | None = None) -> int:
    """This machine's `sd.machine_cap`, builders at once, 1 to 16; `MACHINE_CAP` when unset (sd:9891)."""
    return _machine_setting("machine_cap", "[1-9]|1[0-6]", MACHINE_CAP, environ)


def overload_fallback(environ: dict[str, str] | None = None) -> bool:
    """Whether this machine's `sd.overload_fallback` reads 1: the hub then builds an overloaded satellite's fresh
    rows itself (sd:8991). Unset or 0, they wait for the satellite (sd:10823)."""
    return _machine_setting("overload_fallback", "[01]", 0, environ) == 1


def repo_cap(environ: dict[str, str] | None = None) -> int:
    """This machine's `sd.repo_cap`, bundles building per repository, 1 to 6; `REPO_CAP` when unset (sd:9172)."""
    return _machine_setting("repo_cap", "[1-6]", REPO_CAP, environ)


def _slots(connection, item: int) -> float:
    kind = work_kind(connection, item)
    return 0 if kind == DESIGN_KIND else 0.5 if kind == "mechanical" else 1


def budget_minutes(connection, items: list[int]) -> int:
    """A build child's budget for the bundle `items`: `BUDGET_MINUTES` per `BUNDLE_MAX` slots, never less.

    Fixed at claim from the bundle taken, not the machine's cap, so a small cap never cuts one item below
    120 minutes and a cap changed later moves no claimed bundle (sd:9109 review).
    """
    slots = sum(_slots(connection, item) for item in items)
    return max(BUDGET_MINUTES, math.ceil(BUDGET_MINUTES * slots / BUNDLE_MAX))


def bundled(connection, items: list[int]) -> int:
    """How many of `items`, in order, one bundle takes: `bundle_max` slots, a mechanical item half of one.

    A long queue of small items so fills a bundle past `BUNDLE_MAX`, up to twice it, on the one bundle branch
    (sd:8992); the claim, auto-pickup and the Build dialog split bundles by it.
    """
    spent, cap = 0.0, bundle_max()
    for count, item in enumerate(items):
        spent += _slots(connection, item)
        if spent > cap:
            return count
    return len(items)


def branch(lead: int) -> str:
    return f"build/sd-{lead}"


def worktree(repo: str, lead: int, *, root: Path | None = None) -> Path:
    return (root or Path.home() / "worktrees") / f"{Path(repo).name}-build-{lead}"


class _Moved(Exception):
    """A member's item moved after the press; the claim's writes roll back."""


#: The claim's re-check of each member, in the UPDATE's WHERE (design, claim step 4).
def _holding(item: str) -> str:
    """The unreleased build runs that hold `item` (an SQL expression), as its lead's or a member's: one query.

    A held run (`blocked` or `abandoned`) keeps its lease while a process
    may live in its worktree; the item's status may move, but no build of it
    starts until a reap releases that run. An open run at a lane step holds
    only the members it delivered (`done`): one `enqueued` left out is free
    to build while the lane takes the rest (sd:8960).
    """
    return (f"SELECT runner_run.id, runner_run.outcome FROM assignment holder JOIN runner_run "
            f"ON runner_run.assignment = COALESCE(holder.parent, holder.id) WHERE holder.item = {item} "
            f"AND holder.scope = '{SCOPE}' AND {_in_group('holder')} AND runner_run.released_at IS NULL AND NOT ({IN_LANE} "
            f"AND holder.status IS NOT 'done')")


def _held(connection, item: int) -> str | None:
    """Why no build of `item` may start while a run holds it, or None."""
    run = connection.execute(_holding("?") + " LIMIT 1", (item,)).fetchone()
    if run is None:
        session = connection.execute("SELECT id FROM assignment WHERE item = ? AND status = ? LIMIT 1",
                                     (item, IN_SESSION)).fetchone()
        return f"item {item} has in_session assignment {session['id']}: a teammate builds it" if session else None
    return (f"item {item} is held by build run {run['id']} ({run['outcome'] or 'open'}), which releases once "
            f"its process groups read gone")


_RECHECK = (f"EXISTS (SELECT 1 FROM item WHERE item.id = assignment.item AND item.status = 'ready' "
            f"AND item.kind IN ({', '.join(repr(kind) for kind in RUNNABLE_KINDS)})) "
            f"AND NOT EXISTS (SELECT 1 FROM assignment other WHERE other.item = assignment.item "
            f"AND other.id != assignment.id AND other.status IN {OWNING}) "
            f"AND NOT EXISTS ({_holding('assignment.item')})")


def claim(connection, *, owner: str, load: float | None, load_cap: float | None, interactive: list[dict] | None,
          root: Path | None = None, hosts: Collection[str | None] | None = None) -> dict | None:
    """Claim one bundle under one `BEGIN IMMEDIATE`; None when nothing may start now.

    The repository with room whose best queued build ranks first (priority,
    then queue time) gives up to `BUNDLE_MAX` of its queued builds. A member
    whose item moved after the press fails the re-check: the claim's writes
    roll back, that member ends `blocked` with the reason, and nothing else
    changes, so the next claim takes the rest.

    Each member's `item.branch` becomes the run's branch, so a design-only
    build is not left with an empty one (pack sd:6756).

    A claim takes only the rows whose repository's lane runs on one of
    `hosts` (`lane_hosts` when None), and skips the rest, unblocked, for
    their lane host's dispatcher; the run's owner names that host
    (`run_owner`, pack sd:7933). A queued build of its own whose repository
    `queue` would now refuse (unmanaged, no remote: `_repo_refusal`) ends
    `blocked` with that reason before the bundle is picked, its item left as
    it is, and the same claim goes on to the next repository (pack sd:6103).
    """
    hosts = lane_hosts(connection) if hosts is None else hosts
    with transaction(connection):
        if restoration_pending(connection):
            raise BuildRefused(RESTORING)
        _requeue_beat_blocked(connection, hosts)
        queued = connection.execute(
            "SELECT assignment.id, assignment.item, assignment.parent, assignment.run_count, item.repo, "
            f"item.priority, item.fields, {LEAD_PRIORITY}, repo.lane_host "
            "FROM assignment JOIN item ON item.id = assignment.item "
            "LEFT JOIN repo ON repo.path = item.repo "
            "WHERE assignment.status = 'queued' AND assignment.scope = ? AND item.repo IS NOT NULL "
            "ORDER BY item.priority IS NULL, item.priority, assignment.queued_at, assignment.id", (SCOPE,)).fetchall()
        lanes = {row["id"]: _route(connection, row["lane_host"], _lead(row)) for row in queued}
        if None in hosts and overload_fallback():
            # A satellite over its load cap claims nothing: with `sd.overload_fallback` set to 1, the hub builds its
            # fresh rows of a repository checked out here, under the hub's caps (sd:8991); unset, they wait for the
            # satellite (sd:10823). A retry stays on its lane: its work is in that lane's worktree.
            over = {host for host in set(lanes.values()) - set(hosts) if host and overloaded(connection, host)}
            lanes = {row["id"]: None if (lanes[row["id"]] in over and _lead(row) is None
                                         and _checked_out(row["repo"])) else lanes[row["id"]]
                     for row in queued}
        if None in hosts:
            # The hub blocks another host's row once that host's dispatcher went quiet: a lane moved after the
            # press, or a dispatcher that died after it, would leave the item locked with no word. A fresh beat
            # that names a `why` leaves the row queued, as that reason may last one tick (review round 5), until
            # `STUCK_TICKS` beats in a row name one (sd:8081). Only once every row is routed (sd:8991).
            for row in queued:
                away = lanes[row["id"]]
                gone = (dispatcher_gone(connection, away) or dispatcher_stuck(connection, away)
                        if away is not None and away not in hosts else None)
                if gone:
                    _refuse(connection, row, f"item {row['item']}'s lane runs on {away}, and {gone}")
        available = room(connection, load=load, load_cap=load_cap, interactive=interactive, hosts=hosts)
        if available["why"]:
            return None
        # A bundle drop's new lead has no run, so it routes by the lane as it reads now; its kept work is in a
        # worktree on the host that ran the ended run. A lane moved since would build it there fresh (round 16).
        for row in queued:
            kept = _kept_host(connection, row)
            if kept is not None and lanes[row["id"]] in hosts and lanes[row["id"]] != lane_of(connection, kept[1]):
                _refuse(connection, row, f"its kept work is in a worktree on {kept[1] or 'the hub'}, which ran build "
                                         f"run {kept[0]}, but its lane moved; drop or Build its items again there")
        # A bundle a usage limit queued again resumes its worktree and session (below), which stay on the host
        # that ran it: a lane moved since would claim it where neither is, so the whole bundle blocks, saying
        # where its work is (review of build/sd-1557).
        for row in queued:
            lead = _lead(row)
            last = connection.execute("SELECT id, owner, step, outcome FROM runner_run WHERE assignment = ? "
                                      "ORDER BY run DESC LIMIT 1", (lead,)).fetchone() if lead else None
            if (last is not None and last["outcome"] == RATE_LIMITED and last["step"] not in LANE_STEPS
                    and lanes[row["id"]] in hosts and lanes[row["id"]] != lane_of(connection, run_host(last["owner"]))):
                ran_on = run_host(last["owner"]) or "the hub"
                _refuse(connection, row, f"build run {last['id']} hit the usage limit on {ran_on}, whose worktree "
                                         f"and session it resumes, but its lane moved; move its lane back to "
                                         f"{ran_on} and Retry it, or Build its items again here")
        queued = [row for row in queued if lanes[row["id"]] in hosts and not _refused_repo(connection, row)]
        # A bundle's work is on its lead's branch in the repository of the run that made it, not wherever its item
        # reads now: a lead moved since would build fresh there, and a member moved since would still be briefed and
        # delivered from the old one (round 20). The lead blocks; the member leaves the bundle (`ALONE`). Its commits
        # stay on the lead's branch: once the lead ran, it is left out of that run as a subset Retry leaves one
        # (`LEFT_OUT`), so the next run's split check holds the branch; a drop's kept lead that never ran blocks
        # below, and Build takes its items fresh (round 21).
        for row in queued:
            home = _work_repo(connection, row)
            if home is None or paths.same(home, row["repo"]):
                continue
            if row["parent"] is None:
                _refuse(connection, row, f"its work is on branch {branch(row['id'])} in {home}, but its item moved "
                                         f"to {row['repo']}; move it back, or Build it again there")
                continue
            reason = (f"{ALONE}build: its item moved to {row['repo']}, away from its bundle's work in {home}; "
                      f"Build it again there")
            reason += left_mark(connection, row["parent"])
            connection.execute("UPDATE assignment SET status = 'cancelled', ended = ?, result = ? "
                               "WHERE id = ? AND status = 'queued'", (clock(), reason, row["id"]))
            add_note(connection, row["item"], "comment", f"Build assignment {row['id']} left its bundle: {reason}")
        # A member that left a bundle with work after it queued (moved above, cancelled, refused at a claim) leaves
        # its commits on the lead's branch, which the lane would ship with the lead's: the lead blocks (round 21).
        # A done item's commits may stay: the merge closes nothing open; one left out (`LEFT_OUT`) the split check
        # reads at the run's start. A queued member is checked here against the work's repository too, as its item
        # reads now: one whose item lost its repository (round 22), or moved to one whose lane runs on another host
        # (round 23), is not in `queued`, so no check above saw it. It blocks, and counts as left.
        for row in queued:
            home = _work_repo(connection, row) if row["parent"] is None else None
            if home is None:
                continue
            found = _strays(connection, row["id"], home, ("queued",))
            for one in found:
                if one["status"] == "queued":
                    _refuse(connection, one, f"item {one['item']} has no repository; set one, then Build it again"
                            if one["repo"] is None else f"its item moved to {one['repo']}, away from its bundle's "
                            f"work in {home}; Build it again there")
            strays = [one["item"] for one in found]
            if strays:
                names = ", ".join(f"sd:{item}" for item in strays)
                _refuse(connection, row, f"{names} left the bundle after it queued, and branch {branch(row['id'])} "
                                         f"holds its commits; Build the other items again to build them fresh on "
                                         f"a new branch")
        queued = [row for row in queued if connection.execute(
            "SELECT status FROM assignment WHERE id = ?", (row["id"],)).fetchone()[0] == "queued"]
        # A member whose lead is not in this claim's group (moved to planning, refused, or the member moved to another
        # repository) would lead its own fresh branch, past the lead's branch, which holds the bundle's work, a bundle
        # drop's cherry-picks alone (rounds 15 and 19).
        leads = {row["id"]: row["repo"] for row in queued}
        for row in queued:
            if row["parent"] is not None and leads.get(row["parent"]) != row["repo"]:
                _refuse(connection, row, f"its lead, assignment {row['parent']}, is no longer queued in "
                                         f"{row['repo']}, and its branch {branch(row['parent'])} holds the "
                                         f"bundle's work")
        queued = [row for row in queued if row["parent"] is None or leads.get(row["parent"]) == row["repo"]]
        # A ride whose design was written for another repository, its item moved since, joins no bundle: it
        # would close with that repository's design (review of sd:10096). Build runs its design again here.
        for row in queued:
            if _rides(connection, row) and draft_design(connection, row["item"]) is None:
                _refuse(connection, row, f"item {row['item']} has no design written for {row['repo']}; "
                                         f"Build it again to run its design there")
        queued = [row for row in queued if connection.execute(
            "SELECT status FROM assignment WHERE id = ?", (row["id"],)).fetchone()[0] == "queued"]
        # A fresh design row rides a code bundle and never leads one (sd:10096); one that ran leads its Retry.
        best = next((row for row in queued if room_for(available, row["repo"], p1=_leads_beside(row)) > 0
                     and not _rides(connection, row)), None)
        if best is None:
            return None
        repo = best["repo"]
        others = [row for row in queued if row["repo"] == repo and row["id"] != best["id"]]
        if best["parent"] is None and not best["run_count"]:
            # A fresh P1 lead takes its own area's items; an ordinary one leaves P1 items to their own (sd:9700).
            # A ride never leads and takes no slot, so it joins either, or a P1 one waited on a P1 lead forever
            # (review of sd:10096).
            others = [row for row in others if _rides(connection, row) or (
                kin(best, row) if beside(best) else not beside(row))]
        bundle = _bundle(connection, [best, *others])
        # A dependency reopened since the press holds the build: dispatch's start reads it too (pack sd:9570).
        # So does a design member whose ruling was taken back since its Retry: a bundle that ran before claims
        # whole (`_bundle`), so it holds as one a dependency holds (class pass of sd:10096).
        # A design that rides this bundle holds none of its members (review of sd:10096).
        # Fresh or retried: a retried design row is no ride, and it is aboard all the same (class pass).
        riding = aboard(connection, [row["item"] for row in bundle])
        waiting = [(row, why) for row in bundle if (why := waiting_on(connection, connection.execute(
            "SELECT id, fields FROM item WHERE id = ?", (row["item"],)).fetchone(), riding=riding)
            or design_waits(connection, row["item"]))]
        # Ran before means work to resume: a run, a drop's kept work, or members on the lead's branch. A drop's
        # solo row counts a run with no run row (`drop_writes`); held as a retry it would park with nothing for
        # Retry to resume, so it holds as a fresh build does (review of build/sd-1306).
        resumable = (queue_state(connection, bundle[0]["id"])["run"] is not None
                     or _kept_host(connection, bundle[0]) is not None)
        retried = bool(waiting) and (resumable or any(row["parent"] is not None for row in bundle))
        held = "; ".join(f"item {row['item']} {why}" for row, why in waiting)
        stamp = clock()
        if retried:
            # A member the waiting ones need leaves the bundle, as a subset Retry leaves one out (`LEFT_OUT`):
            # held with them it could never build, and its Build would retry this bundle (review of sd:9570).
            # It leaves before any row is refused, so one that waits itself leaves too (review of build/sd-1306).
            # A drop's kept lead that never ran leaves its member too: its Retry resumes the split branch once the
            # member is done (review of build/sd-1306).
            needed = _needed(connection, [row["item"] for row, _ in waiting], riding=riding)
            # A kept lead's marker names the kept run, which `left_out` reads as run 0. Unlike a member that leaves
            # before the claim (round 21), this one may be marked done by hand, and the lead then resumes the
            # split branch: only the marker makes its first run check that branch (review of build/sd-1306).
            mark = left_mark(connection, bundle[0]["id"]) or (
                f": {LEFT_OUT}{kept[0]}" if (kept := _kept_host(connection, bundle[0])) else ": its lead waits on it")
            for row in bundle[1:]:
                if row["item"] in needed and resumable and connection.execute(
                        "UPDATE assignment SET status = 'cancelled', ended = ?, result = ? "
                        "WHERE id = ? AND status = 'queued'",
                        (stamp, f"{ALONE}build{mark}", row["id"])).rowcount:
                    add_note(connection, row["item"], "comment",
                             f"Build assignment {row['id']} left its bundle: its lead waits on it ({held}); "
                             f"Build it to build it on its own")
        for row, why in waiting:
            if not _alone(connection, row["id"]):
                _refuse(connection, row, f"item {row['item']} {why}")
        if retried:
            # A bundle that ran before holds whole: a member left queued would lead a new branch and worktree, and
            # Retry would lose the checkout it resumes (review of sd:9570).
            for row in [row for row in bundle if not _alone(connection, row["id"])]:
                _refuse(connection, row, f"its bundle waits: {held}")
                # Parked, as a blocked run parks its items: auto-pickup takes unparked items only, so no member is
                # built alone on a new branch; Retry unparks them onto this one (review of sd:9570).
                park(connection, row["item"], ("ready",), stamp, WAIT_PARKED.format(lead=bundle[0]["id"], held=held))
        if waiting:
            return None
        lead = bundle[0]["id"]
        ids = [row["id"] for row in bundle]
        marks = ", ".join("?" for _ in ids)
        try:
            with transaction(connection):
                stamp = clock()
                taken = connection.execute(
                    f"UPDATE assignment SET status = 'running', started = ?, ended = NULL, "
                    f"parent = CASE WHEN id = ? THEN NULL ELSE ? END "
                    f"WHERE id IN ({marks}) AND status = 'queued' AND scope = ? AND {_RECHECK}",
                    (stamp, lead, lead, *ids, SCOPE)).rowcount
                if taken != len(ids):
                    raise _Moved
                connection.execute("UPDATE assignment SET budget_minutes = ? WHERE id = ?",
                                   (budget_minutes(connection, [row["item"] for row in bundle]), lead))
                # A bundle a usage limit queued again resumes as a loss retry does: its step, session and round
                # (advisory of pack sd:9907, sd:9973). Any other claim starts fresh.
                last = connection.execute("SELECT step, session_id, review_round, outcome FROM runner_run "
                                          "WHERE assignment = ? ORDER BY run DESC LIMIT 1", (lead,)).fetchone()
                limited = last is not None and last["outcome"] == RATE_LIMITED and last["step"] not in LANE_STEPS
                # A limit in a fix resumes that fix, not the review after it: a review there would charge its
                # blockers another round (review of build/sd-1557).
                step = (last["step"] if last["step"] == "fixing" else RESUME.get(last["step"] or "claimed",
                        last["step"] or "claimed")) if limited else "claimed"
                ident = _open_run(connection, lead, repo=repo, owner=run_owner(owner, lanes[lead]),
                                  step=step,
                                  model=_model(connection, [row["item"] for row in bundle]),
                                  work=worktree(repo, lead, root=root), stamp=stamp,
                                  session_id=last["session_id"] if limited else None,
                                  review_round=last["review_round"] if limited else None)
                for row in bundle:
                    transition(connection, row["item"], "in_progress", who="build",
                               reason=f"build run {ident} claimed assignment {row['id']}")
                    set_item_fields(connection, row["item"], branch=branch(lead))
                # The slot this claim took, kept on the lead for `room` (`SLOT`): a P1 edit later moves no run.
                connection.execute(f"UPDATE item SET fields = json_set(COALESCE(fields, '{{}}'), '$.{SLOT}', ?) "
                                   "WHERE id = ? AND json_valid(COALESCE(fields, '{}'))",
                                   ("beside" if _leads_beside(best) else "bundle", bundle[0]["item"]))
        except _Moved:
            moved = connection.execute(
                f"SELECT assignment.id, assignment.item, item.status FROM assignment JOIN item ON item.id = assignment.item "
                f"WHERE assignment.id IN ({marks}) AND NOT ({_RECHECK})", ids).fetchall()
            for row in moved:
                _refuse(connection, row, _held(connection, row["item"])
                        or f"item {row['item']} moved to {row['status']} after the press")
            return None
        return run(connection, ident)


def queued_fresh(connection, repo: str) -> list[dict]:
    """`repo`'s queued fresh builds (no parent, never run), as `{id, title, priority, fields}` in queue order: the
    rows a claim bundles with a new press, for the Build dialog's plan (pack sd:9484)."""
    return [dict(row) for row in connection.execute(
        "SELECT item.id, item.title, item.priority, item.fields FROM assignment JOIN item ON item.id = assignment.item "
        "WHERE assignment.status = 'queued' AND assignment.scope = ? AND assignment.parent IS NULL "
        "AND assignment.run_count = 0 AND item.repo = ? ORDER BY assignment.queued_at, assignment.id",
        (SCOPE, repo))]


def _bundle(connection, rows: list) -> list:
    """The queued rows of one repository a claim takes, best first.

    A bundle that ran before claims alone, its old lead first, so its run
    gets the lead's branch and worktree back (pack sd:6020); fresh rows
    bundle with fresh rows only, as many as `bundled` takes.
    """
    best = rows[0]
    group = best["parent"] or (best["id"] if best["run_count"] else None)
    if group is None:
        # A design whose question reopened since its press waits for the ruling (sd:10096).
        fresh = [row for row in rows if row["parent"] is None and not row["run_count"] and not (
            _rides(connection, row) and design_waits(connection, row["item"]))]
        # A ride takes no slot, so the cap counts code rows only and every ride joins: one cut past the
        # cap lost a later ride its code item waits on (review of sd:10096).
        code = [row for row in fresh if not _rides(connection, row)]
        taken = {row["id"] for row in code[:bundled(connection, [row["item"] for row in code])]}
        return [row for row in fresh if row["id"] in taken or _rides(connection, row)]
    return sorted((row for row in rows if group in (row["id"], row["parent"])), key=lambda row: row["id"] != group)


def _riding(connection, repo: str | None) -> set[int]:
    """The design items of `repo` whose ride is queued and may ride (`design_waits`): the next code bundle there
    takes each, so a code item that waits on one builds with it (review of sd:10096)."""
    if repo is None:
        return set()
    return {row["item"] for row in connection.execute(
        "SELECT assignment.item, assignment.parent, assignment.run_count FROM assignment "
        "JOIN item ON item.id = assignment.item WHERE assignment.status = 'queued' AND assignment.scope = ? "
        "AND item.repo = ?", (SCOPE, repo)).fetchall()
        if _rides(connection, row) and design_waits(connection, row["item"]) is None}


def _rides(connection, row) -> bool:
    """Whether `row` is a fresh design row: it joins a code bundle, never leads one (sd:10096)."""
    return row["parent"] is None and not row["run_count"] and work_kind(connection, row["item"]) == DESIGN_KIND


def aboard(connection, items) -> set[int]:
    """The design items among `items`, the items of one bundle. The one rule every reader of a bundle's
    dependencies asks (class pass of sd:10096): a design aboard is committed with the bundle's work, so it is
    no dependency of a member there, fresh or retried; delivery holds a member whose design it leaves out
    (`design_left`)."""
    return {item for item in items if work_kind(connection, item) == DESIGN_KIND}


def design_left(connection, delivered: list[int], bundle: list[int]) -> str | None:
    """Why a delivery of `delivered` out of `bundle` may not ship, or None: a delivered item waits, directly or
    through open items, on a design of the bundle that the delivery leaves out and that is not done."""
    left = aboard(connection, bundle) - set(delivered)
    for item in delivered:
        held = sorted(_needed(connection, [item]) & left)
        if held:
            return f"sd:{item} waits on design sd:{held[0]}, which this delivery leaves out"
    return None


def _needed(connection, items: list[int], *, riding: Collection[int] = ()) -> set[int]:
    """The open items `items` wait on, directly or through other open items (pack sd:9570), but for a design
    aboard their bundle (`riding`, `aboard`): it is no dependency, though what it waits on is (review of
    sd:10096)."""
    needed: set[int] = set()
    seen: set[int] = set()
    pending = list(items)
    while pending:
        row = connection.execute("SELECT fields FROM item WHERE id = ?", (pending.pop(),)).fetchone()
        try:
            named = item_blocked_by(row) if row else []
        except ValueError:
            continue
        for other in named:
            status = connection.execute("SELECT status FROM item WHERE id = ?", (other,)).fetchone()
            if other not in seen and status is not None and status["status"] != "done":
                seen.add(other)
                pending.append(other)
                if other not in riding:
                    needed.add(other)
    return needed


def _strays(connection, lead: int, home: str, keep: tuple[str, ...]) -> list:
    """The members that left build `lead`'s bundle after it queued, with work on its branch: each whose item is
    open, not left out by a Retry (`LEFT_OUT`), and not in `keep` or away from the work's repository `home`
    (round 21). The claim keeps only queued ones; a Retry of a drop's kept lead keeps its held ones too."""
    return [one for one in connection.execute(
        "SELECT member.id, member.item, member.status, item.repo FROM assignment member "
        "JOIN item ON item.id = member.item JOIN assignment lead ON lead.id = member.parent "
        "WHERE member.parent = ? AND member.scope = ? "
        "AND member.queued_at >= lead.queued_at AND item.status != 'done' "
        "AND COALESCE(member.result, '') NOT LIKE ? ORDER BY member.id",
        (lead, SCOPE, f"%: {LEFT_OUT}%")).fetchall()
        if one["status"] not in keep or one["repo"] is None or not paths.same(home, one["repo"])]


def _resumable(connection, lead: int, item: int, status: str) -> tuple[str, str | None] | None:
    """(run, host) of the ended run a drop's kept lead `lead` that never ran resumes the split branch of, or None:
    none when a member left its bundle with work on it (`_strays`), which Build takes fresh (round 21; review of
    build/sd-1306)."""
    row = {"id": lead, "item": item, "parent": None}
    kept = _kept_host(connection, row)
    home = _work_repo(connection, row) if kept else None
    return kept if home is not None and not _strays(connection, lead, home, ("queued", status)) else None


def _kept_host(connection, row) -> tuple[str, str | None] | None:
    """(run, host) of the ended run a bundle drop's new lead `row` keeps the work of; None for any other row, or
    when that run reads gone. It holds after the lead's first claim too: a Retry rebuilds the same worktree, which
    stays on that host (round 18)."""
    if row["parent"] is not None:
        return None
    note = connection.execute(
        "SELECT body FROM note WHERE item = ? AND body LIKE ? ORDER BY timestamp, id LIMIT 1",
        (row["item"], f"Build assignment {row['id']} queued again by %: {DROP_HOW.format(item='%')}%")).fetchone()
    if note is None:
        return None
    ident = note["body"].rsplit(" ", 1)[1]
    ran = connection.execute("SELECT owner FROM runner_run WHERE id = ?", (ident,)).fetchone()
    return (ident, run_host(ran["owner"])) if ran else None


def _work_repo(connection, row) -> str | None:
    """The repository holding the work of `row`'s bundle: its lead's latest run's, or, for a bundle drop's new lead
    that has not run, the ended run's whose work it keeps (`_kept_host`); None for a fresh bundle."""
    lead = row["parent"] or row["id"]
    ran = connection.execute("SELECT repo FROM runner_run WHERE assignment = ? ORDER BY run DESC LIMIT 1",
                             (lead,)).fetchone()
    if ran is not None:
        return ran["repo"]
    held = _kept_host(connection, connection.execute("SELECT id, item, parent FROM assignment WHERE id = ?",
                                                     (lead,)).fetchone())
    ran = connection.execute("SELECT repo FROM runner_run WHERE id = ?", (held[0],)).fetchone() if held else None
    return ran["repo"] if ran else None


def _refuse(connection, row, reason: str) -> None:
    """End one queued build `blocked` with why the claim did not take it; its item stays as it is. A row no longer
    queued, such as one this claim refused already, writes no second note (pack sd:9862)."""
    reason += "; not built"
    if connection.execute("UPDATE assignment SET status = 'blocked', ended = ?, result = ? "
                          "WHERE id = ? AND status = 'queued'", (clock(), reason, row["id"])).rowcount:
        add_note(connection, row["item"], "comment", f"Build assignment {row['id']} blocked: {reason}")


#: The result `_refuse` writes on a build the hub blocked on another host's quiet dispatcher.
_BEAT_RESULT = "%'s lane runs on %, and %"
#: An item's newest build that stands (a newer one that ended before it ran owns nothing, as `_current` reads:
#: review round 5 of sd:9775), blocked with `_BEAT_RESULT`, no other assignment owning the item; binds SCOPE,
#: _BEAT_RESULT.
_BEAT_BLOCKED = (f"assignment.scope = ? AND assignment.status = 'blocked' AND assignment.result LIKE ? "
                 f"AND NOT EXISTS (SELECT 1 FROM assignment newer WHERE newer.item = assignment.item "
                 f"AND newer.scope = assignment.scope AND newer.id > assignment.id "
                 f"AND NOT (newer.status IN {AGAIN} AND newer.run_count = 0)) "
                 f"AND NOT EXISTS (SELECT 1 FROM assignment other WHERE other.item = assignment.item "
                 f"AND other.status IN {OWNING})")


def _ran_on(connection, lead: int) -> str | None:
    """`lane_of` the host whose dispatcher ran build `lead`'s latest run, which holds its worktree; None for the
    hub's, or when it never ran."""
    ran = connection.execute("SELECT owner FROM runner_run WHERE assignment = ? ORDER BY run DESC LIMIT 1",
                             (lead,)).fetchone()
    return lane_of(connection, run_host(ran["owner"])) if ran else None


def _stranded(connection, row) -> bool:
    """Whether the blocked build `row` keeps a worktree on a host other than the one its lane routes to now: its
    lead's last run's, or a bundle drop's kept lead's ended run's (`_kept_host`), whatever host the block names, so
    no host requeues it (review round 3 of sd:9775), and pickup builds its item fresh rather than wait on it
    (round 4 of sd:9774). A lane moved to the hub counts too: the hub holds no satellite's worktree (pack sd:9964).
    A fresh row, or a lead with no run row and no kept work, ran nowhere: not stranded."""
    lead = _lead(row)
    if lead is None:
        return False
    if connection.execute("SELECT 1 FROM runner_run WHERE assignment = ?", (lead,)).fetchone() is not None:
        held = _ran_on(connection, lead)
    else:
        lead_row = connection.execute("SELECT id, item, parent FROM assignment WHERE id = ?", (lead,)).fetchone()
        kept = _kept_host(connection, lead_row) if lead_row is not None else None
        if kept is None:
            return False
        held = lane_of(connection, kept[1])
    return held != _route(connection, row["lane_host"], lead)


def _in_work_repo(connection, lead: int, status: str, *, retry: int | None = None) -> bool:
    """Whether the claim resumes build `lead`'s bundle in `status` where its work is: every member's item reads the
    repository of that work (`_work_repo`), as the claim checks, which refuses the lead or drops the member
    otherwise. The rule each resume selector shares, Build by item's and recovery's (class pass, review round 15).
    With `retry`, only the members Retry keeps (`_inside`): one that waits on a bundle item leaves parked, and
    with a member the lead waits on, those leave to build fresh and none resumes (sd:10086). `retry` is the item
    Build presses, checked too when Retry would park it: that Retry queues the lead and leaves the pressed item
    unbuilt (review of sd:10086). The check reads what Retry resumes on the branch, nothing else (class pass)."""
    row = connection.execute("SELECT id, item, parent FROM assignment WHERE id = ?", (lead,)).fetchone()
    home = _work_repo(connection, row)
    if home is None:
        return True
    members = _open_members(connection, lead, status)
    if retry is not None:
        ahead, behind = _inside(connection, row["item"], [member for member in members if _current(connection, member)])
        # What Retry resumes on the branch: nothing while the lead waits on a member (it frees those to build
        # fresh where they read now, the pressed one too); else the kept members, and the pressed item.
        # The pressed item is checked whenever Retry does not build it: a member Retry neither frees nor leads
        # stays ended, so a moved one presses fresh where it reads now, not skipped (lead round 3 of sd:10086).
        members = [member for member in members if member["item"] == retry and member["id"] != lead
                   and member not in ahead] if ahead else [
                       member for member in members if member["item"] == retry or member not in behind]
    repos = [connection.execute("SELECT repo FROM item WHERE id = ?", (member["item"],)).fetchone()["repo"]
             for member in members]
    return all(repo is not None and paths.same(home, repo) for repo in repos)


def _recoverable(connection) -> list:
    """The builds blocked on a quiet beat that recovery may queue again once a beat is back, a build that ran first:
    the one reader recovery and pickup's wait share, so pickup never waits on a row recovery leaves (class pass,
    review round 7). Its item `ready` and not parked: a park is the operator's pause, as a cancel of a newer press
    leaves it; not `_stranded`. A member of a bundle that ran only with its lead: alone, the claim would make it a
    lead on a new branch."""
    rows = connection.execute(
        f"SELECT assignment.id, assignment.item, assignment.parent, assignment.run_count, assignment.result, "
        f"repo.lane_host FROM assignment "
        f"JOIN item ON item.id = assignment.item LEFT JOIN repo ON repo.path = item.repo "
        f"WHERE {_BEAT_BLOCKED} AND item.status = 'ready' AND item.parked_at IS NULL "
        f"ORDER BY assignment.run_count > 0 DESC, assignment.id DESC", (SCOPE, _BEAT_RESULT)).fetchall()
    rows = [row for row in rows if not _stranded(connection, row)
            and ((lead := _lead(row)) is None or _in_work_repo(connection, lead, "blocked"))]
    # A bundle that ran queues again whole or not at all: each blocked member the claim takes with its lead, its
    # lead first. A member left out by any filter above (parked, its item owned by another build, not ready) keeps
    # its commits on the lead's branch, so the claim would refuse the lead and replace the beat result, and no
    # later tick could recover it (class pass, review round 23). A member whose item is done leaves, as in `retry`.
    ids = {row["id"] for row in rows}
    leads: set[int] = {lead for row in rows if (lead := _lead(row)) is not None}
    whole = {lead for lead in leads
             if lead in ids and all(member["id"] in ids for member in _members(connection, lead, "blocked")
                                    if not _done(reads.items_by_ids(connection, [member["item"]]), member["item"]))}
    return [row for row in rows if (lead := _lead(row)) is None or lead in whole]


def _requeue_beat_blocked(connection, hosts: Collection[str | None]) -> None:
    """Put back to queued each build the hub's claim blocked on another host's quiet dispatcher, once that host's
    beat is fresh and not stuck again (pack sd:8745): a slow-DB stretch blocked them for good before.

    Only the item's newest build, its item still `ready`, no other assignment
    owning it, and its lane (`_route`) on the host holding its worktree, or
    any host for one that never ran (`_stranded`, pack sd:9964): an item moved on, or a build
    requeued by hand since, stays as it is. A satellite takes its own on its
    first tick back, before its pickup reads, so a hub outage that left its
    beat stale requeues its bundles as they were (sd:9775).
    """
    for row in _recoverable(connection):
        away = _route(connection, row["lane_host"], _lead(row))
        # Not stranded, a retry's lane is on the host it ran on, which holds its worktree: that host owns the block
        # whatever host it names, as one blocked while its lane was away (review round 5 of sd:9775).
        names = away is not None and (_lead(row) is not None
                                      or row["result"].startswith(f"item {row['item']}'s lane runs on {away}, "))
        # This host ticks, so its beat is back: a satellite takes the rows its own quiet beat blocked (sd:9775), not
        # one another satellite ran, whose worktree is there (review of sd:9775); a lane moved to this host takes
        # one that never ran, with no worktree to keep (review round 2).
        if away in hosts and (names or None in hosts or _lead(row) is None):
            why = f"the {HEARTBEAT}@{away} beat is fresh" if names else "its lane runs on this host now"
        elif (away is None or None not in hosts or not names
              or dispatcher_gone(connection, away) or dispatcher_stuck(connection, away)):
            continue
        else:
            why = f"the {HEARTBEAT}@{away} beat is fresh"
        # The rows were read before any write: two blocked builds of one item that never ran both pass the read,
        # so the write itself queues one per item, a build that ran first (review round 6 of sd:9775).
        if connection.execute(f"UPDATE assignment SET status = 'queued', ended = NULL, result = NULL "
                              f"WHERE id = ? AND status = 'blocked' AND NOT EXISTS (SELECT 1 FROM assignment other "
                              f"WHERE other.item = ? AND other.status IN {OWNING})",
                              (row["id"], row["item"])).rowcount:
            add_note(connection, row["item"], "comment", f"Build assignment {row['id']} queued again: {why}")


def _refused_repo(connection, row) -> bool:
    """End a queued build whose repository turned ineligible after the press (`_repo_refusal`); True if it did."""
    refused = _repo_refusal(connection.execute(_REPO, (row["item"],)).fetchone(), connection)
    if refused:
        _refuse(connection, row, f"{refused} after the press")
    return refused is not None


def _open_run(connection, lead: int, *, repo: str, owner: str, step: str, model: str, work: Path, stamp: str,
              session_id: str | None = None, review_round: int | None = None) -> str:
    """The lead's next run and its lease, numbered after every run the lead has, and each member's count moved to it."""
    number = connection.execute("SELECT MAX(COALESCE((SELECT MAX(run) FROM runner_run WHERE assignment = ?), 0), "
                                "run_count) + 1 FROM assignment WHERE id = ?", (lead, lead)).fetchone()[0]
    # A member cancelled on its own keeps the count of the last run it was in, which `run` reads (sd:9222).
    connection.execute(f"UPDATE assignment SET run_count = ? WHERE (id = ? OR parent = ?) AND scope = ? "
                       f"AND {_in_group()}", (number, lead, lead, SCOPE))
    ident = uuid.uuid4().hex
    connection.execute(
        "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, start_step, "
        "step, model, session_id, review_round, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ident, lead, number, repo, branch(lead), owner, str(work), str(work), step, step, model, session_id,
         review_round, stamp, stamp))
    if step not in LANE_STEPS:
        connection.execute("INSERT INTO runner_lease (run, repo, branch, exclusive, acquired_at) VALUES (?, ?, ?, 0, ?)",
                           (ident, repo, branch(lead), stamp))
    return ident


def _open_members(connection, lead: int, status: str) -> list[dict]:
    """`_members` but a done item's: Retry skips a delivered member, so no resume selector reads it (review of
    sd:9788)."""
    members = _members(connection, lead, status)
    items = reads.items_by_ids(connection, [member["item"] for member in members])
    return [member for member in members if not _done(items, member["item"])]


def _members(connection, lead: int, status: str = "running") -> list[dict]:
    return [dict(row) for row in connection.execute(
        f"SELECT id, item FROM assignment WHERE (id = ? OR parent = ?) AND status = ? AND scope = ? AND {_in_group()} "
        "ORDER BY id = ? DESC, id", (lead, lead, status, SCOPE, lead))]


def run(connection, ident: str) -> dict:
    """One run with its bundle: the lead first, then the members it ran with. A member cancelled on its own
    (`ALONE`) is in no open run, so no supervisor briefs or delivers it, even one resumed at a lane step; a released
    run up to the last one it was in, its `run_count`, keeps it, so history (the Builds panel, the health-failed
    notes) lists what that run built (sd:9222)."""
    row = connection.execute("SELECT * FROM runner_run WHERE id = ?", (ident,)).fetchone()
    if row is None:
        raise BuildRefused(f"no build run {ident}")
    result = dict(row)
    result["asked_since"] = asked_since(connection, row["assignment"], row["run"])
    result["budget_minutes"] = connection.execute("SELECT budget_minutes FROM assignment WHERE id = ?",
                                                  (row["assignment"],)).fetchone()[0]
    result["members"] = [dict(member) for member in connection.execute(
        f"SELECT id, item, status, result FROM assignment WHERE (id = ? OR parent = ?) AND scope = ? "
        f"AND ({_in_group()} OR (run_count >= ? AND ? IS NOT NULL)) ORDER BY id = ? DESC, id",
        (row["assignment"], row["assignment"], SCOPE, row["run"], row["released_at"], row["assignment"]))]
    return result


def asked_since(connection, lead: int, number: int) -> str:
    """When run `number` of `lead` reads open questions from: the creation of the fresh claim it resumes, past
    each loss retry and usage-limit reclaim, as a question asked before a resume still holds its item (lane
    review of build/sd-1557)."""
    since = ""
    for row in connection.execute("SELECT start_step, created_at FROM runner_run WHERE assignment = ? AND run <= ? "
                                  "ORDER BY run DESC", (lead, number)):
        since = row["created_at"]
        if row["start_step"] == "claimed":
            break
    return since


def open_runs(connection, hosts: Collection[str | None] | None = None) -> list[dict]:
    """Every unreleased build run, oldest first; with `hosts`, only the runs those hosts own (`mine`)."""
    return [dict(row) for row in connection.execute(
        "SELECT runner_run.* FROM runner_run JOIN assignment ON assignment.id = runner_run.assignment "
        "WHERE runner_run.released_at IS NULL AND assignment.scope = ? ORDER BY runner_run.created_at, runner_run.id",
        (SCOPE,)) if hosts is None or mine(row, hosts)]


def ended_runs(connection, *, limit: int) -> list[dict]:
    """The released build runs, newest first, up to `limit` (Queue's Builds panel, pack sd:6095, sd:6834)."""
    return [dict(row) for row in connection.execute(
        "SELECT runner_run.* FROM runner_run JOIN assignment ON assignment.id = runner_run.assignment "
        "WHERE assignment.scope = ? AND runner_run.released_at IS NOT NULL "
        "ORDER BY runner_run.released_at DESC, runner_run.run DESC, runner_run.rowid DESC LIMIT ?",
        (SCOPE, limit))]


def pull_url(connection, repo: str, branch: str | None, items: list[int]) -> str:
    """The pull request `sd-ship` opened for `branch`, from its ship receipt, or "" (pack sd:6020, Open PR).

    The lane names an entry by the first member the branch delivers, so each member's receipt is read in turn. Nothing
    asks GitHub: a branch never shipped, a repository with no GitHub remote or an unreadable receipt has none to show.
    """
    from . import ship
    from .workflow import WorkflowError

    probe = paths.keys(repo)  # a run row written before migration 014 holds the absolute form
    row = connection.execute(
        f"SELECT remote, managed FROM repo WHERE path IN ({paths.placeholders(probe)})", probe).fetchone()
    if not branch or row is None or not row["remote"]:
        return ""
    if not row["managed"]:
        return _contribution_pull(connection, items)
    try:
        repository = ship._github_repository(row["remote"])
        for item in items:
            receipt = ship.read(connection, ship.receipt_key(repository, branch, item))[1]
            url = (receipt.get("pull_request") or {}).get("url")
            if isinstance(url, str) and url.startswith("https://github.com/"):
                return url
    except WorkflowError:
        return ""
    return ""


def _contribution_pull(connection, items: list[int]) -> str:
    """An unmanaged repository's build links its draft pull request on the first delivered task (pack sd:7868)."""
    for item in items:
        row = connection.execute("SELECT fields FROM item WHERE id = ?", (item,)).fetchone()
        try:
            url = (json.loads(row["fields"] or "{}").get("contribution") or {}).get("pull_url") if row else None
        except (ValueError, AttributeError):
            continue
        if isinstance(url, str) and url.startswith("https://github.com/"):
            return url
    return ""


def open_question(connection, items: list[int], since: str, *, pending: bool = False) -> dict | None:
    """The first `question` note on `items` since `since` that no `Ruling on note #<n>:` decision answers.

    Two readers ask two things (review round 3 of sd:9219). By default it is
    the evidence that the builder skipped an item, which delivery and the
    class pass read: `sd note resolve` closes the note, not the skip.
    `pending` asks whether the question still waits on the operator, as the
    run's wait and the `ASKED` block read: a resolved one waits on no one."""
    if not items:
        return None
    marks = ", ".join("?" for _ in items)
    # A reopened question keeps its ruling note: Decide's reversal and a review withdrawal each take one back
    # (`needs_sweep.standing_ruling` reads both; advisory of PR #1940, sd:9973; class pass of sd:10096). The
    # skip's evidence reads it too: a resolved question still counts there, a taken-back ruling not (sd:10236).
    return next((dict(row) for row in connection.execute(
        f"SELECT id, item FROM note WHERE kind = 'question' AND item IN ({marks}) AND timestamp >= ? "
        f"{'AND resolved_at IS NULL ' if pending else ''}ORDER BY id", (*items, since)).fetchall()
        if standing_ruling(connection, row) is None), None)


def finished_seconds(connection, *, limit: int = 20) -> list[float]:
    """Claim to release, in seconds, of the last `limit` build runs that ended `done`, newest first.

    The past runs Today's builder ETA reads (sd:6003). A retried bundle's runs count each on its own.
    """
    rows = connection.execute(
        "SELECT runner_run.created_at, runner_run.released_at FROM runner_run "
        "JOIN assignment ON assignment.id = runner_run.assignment WHERE assignment.scope = ? "
        "AND runner_run.outcome = 'done' AND runner_run.released_at IS NOT NULL "
        "ORDER BY runner_run.released_at DESC, runner_run.id DESC LIMIT ?", (SCOPE, limit)).fetchall()
    return [seconds for row in rows if (seconds := _age(row["created_at"], row["released_at"])) >= 0]


def run_spent(connection, ident: str) -> float:
    """What one build run spent so far: its `run` and `bound` cost rows, which carry the run as their pass."""
    return float(connection.execute(
        "SELECT COALESCE(SUM(usd), 0.0) FROM cost WHERE pass = ? AND source IN ('run', 'bound')", (ident,)).fetchone()[0])


def write(connection, ident: str, **fields) -> bool:
    """One supervisor write on its own run; False, with nothing written, once the run ended or was released."""
    if not fields or set(fields) - RUN_FIELDS:
        raise BuildRefused(f"unsupported build run write: {sorted(set(fields) - RUN_FIELDS)}")
    if "step" in fields and fields["step"] not in (*STEPS, *DRAFT_STEPS):
        raise BuildRefused(f"no build step {fields['step']!r}")
    columns = ", ".join(f"{name} = ?" for name in fields)
    with transaction(connection):
        return connection.execute(
            f"UPDATE runner_run SET {columns}, updated_at = ? "
            f"WHERE id = ? AND released_at IS NULL AND outcome IS NULL",
            (*fields.values(), clock(), ident)).rowcount == 1


def beat(connection, ident: str) -> bool:
    return write(connection, ident, heartbeat_at=clock())


def enqueuing(connection, ident: str) -> bool:
    """The `enqueuing` step: one compare-and-set from `reviewing`, or from `enqueuing` on a resumed run, that a
    requested cancel fails. With the cancel's own step check it makes one winner: cancel or enqueue, never both."""
    with transaction(connection):
        return connection.execute(
            "UPDATE runner_run SET step = 'enqueuing', heartbeat_at = ?, updated_at = ? WHERE id = ? "
            "AND step IN ('reviewing', 'enqueuing') AND outcome IS NULL AND released_at IS NULL "
            "AND cancel_requested IS NULL", (clock(), clock(), ident)).rowcount == 1


def enqueued(connection, ident: str, delivered: Collection[int] | None = None, *, held_by: str = "the lane") -> bool:
    """The `enqueued` step: the lease released and the members `done`, the items left `in_progress` for the lane.

    `delivered` is the items the lane entry delivers; the merge leaves a
    member outside it open, and the run holds it no longer (`_holding`).
    One the builder skipped with an open question ends `blocked` with its
    item. Any other ends `cancelled`, and its item returns to `planning`
    with a note naming the run: no delivery is established, and `ready`
    would let auto-pickup requeue it unseen (sd:8960). Not `done`: each
    reader of a lane-step run's `done` members takes them as delivered.
    None delivers every member. The run stays open,
    `released_at` and `outcome` null, until step 7 ends it; `reap` skips it
    meanwhile. One compare-and-set on the run, open and not yet at a lane
    step; False, with no row changed, for a late supervisor whose run was
    reaped or retried, or one already enqueued. `held_by` names what holds
    the branch now: the lane, or an unmanaged repository's draft pull request.
    """
    marks = ", ".join("?" for _ in LANE_STEPS)
    with transaction(connection):
        stamp = clock()
        # A cancel asked first wins: the run never reaches `enqueued`, which
        # the reap skips, and the reap ends it cancelled.
        if connection.execute(
                f"UPDATE runner_run SET step = 'enqueued', updated_at = ? WHERE id = ? AND released_at IS NULL "
                f"AND outcome IS NULL AND cancel_requested IS NULL AND (step IS NULL OR step NOT IN ({marks}))",
                (stamp, ident, *LANE_STEPS)).rowcount != 1:
            return False
        run = connection.execute("SELECT assignment, run FROM runner_run WHERE id = ?", (ident,)).fetchone()
        lead = run["assignment"]
        since = asked_since(connection, lead, run["run"])
        connection.execute("UPDATE runner_lease SET released_at = ? WHERE run = ? AND released_at IS NULL", (stamp, ident))
        members = _members(connection, lead)
        skipped = [member for member in members if delivered is not None and member["item"] not in delivered]
        for member in members:
            if member not in skipped:
                connection.execute("UPDATE assignment SET status = 'done', ended = ?, result = ? WHERE id = ?",
                                   (stamp, f"build run {ident} enqueued in {held_by}", member["id"]))
        for member in skipped:
            missing = f"build run {ident} enqueued no commit with a `Closes: sd:{member['item']}` trailer"
            if waits_on_operator(connection, member["item"], since):
                _block(connection, [member], f"{missing}, so the merge leaves this item open", stamp)
            else:
                connection.execute("UPDATE assignment SET status = 'cancelled', ended = ?, result = ? WHERE id = ?",
                                   (stamp, f"{missing}, so the item returns to planning", member["id"]))
                # `_move`, as `_block` moves: an item the operator closed meanwhile stays closed (lead review).
                check = f"{missing}: check whether its work landed, then close it or queue it again"
                _move(connection, member["item"], "planning", check)
                # A followup, as `_block` leaves one: the move alone asks the operator nothing (sd:8994).
                add_note(connection, member["item"], "followup", check)
        return True


def register(connection, ident: str, *, pid: int, pgid: int, start: str | None) -> bool:
    """A supervisor takes its run: one compare-and-set on an open run no supervisor holds; False for a second one."""
    with transaction(connection):
        return connection.execute(
            "UPDATE runner_run SET supervisor_pid = ?, supervisor_pgid = ?, supervisor_start = ?, heartbeat_at = ?, "
            "updated_at = ? WHERE id = ? AND supervisor_pid IS NULL AND released_at IS NULL AND outcome IS NULL",
            (pid, pgid, start, clock(), clock(), ident)).rowcount == 1


def end(connection, ident: str, outcome: str, detail: str, *, question: str | None = None) -> dict | None:
    """The supervisor ends its own run, its groups confirmed gone; `_settle`'s answer, or None once the run moved.

    One compare-and-set from an open outcome, then `_settle` in the same
    transaction: `cancelled` ends the bundle and moves its items to ready, parked,
    a `LOSSES` outcome retries once at the `RESUME` step, `blocked` blocks
    the bundle with `detail` (held until a reap reads the supervisor gone),
    `RATE_LIMITED` queues it again and holds its host, and `done` releases
    it. `question` adds a question note on each item.
    """
    if outcome not in ("cancelled", "blocked", "done", RATE_LIMITED, *LOSSES):
        raise BuildRefused(f"no supervisor outcome {outcome!r}")
    with transaction(connection):
        if connection.execute("UPDATE runner_run SET outcome = ?, detail = ?, updated_at = ? WHERE id = ? "
                              "AND outcome IS NULL AND released_at IS NULL",
                              (outcome, detail, clock(), ident)).rowcount != 1:
            return None
        row = dict(connection.execute("SELECT * FROM runner_run WHERE id = ?", (ident,)).fetchone())
        if question:
            for member in _members(connection, row["assignment"], "done" if row["step"] in LANE_STEPS else "running"):
                add_note(connection, member["item"], "question", question)
        if outcome == "done":
            stamp = clock()
            connection.execute("UPDATE runner_run SET released_at = ?, updated_at = ? WHERE id = ?",
                               (stamp, stamp, ident))
            connection.execute("UPDATE runner_lease SET released_at = ? WHERE run = ? AND released_at IS NULL",
                               (stamp, ident))
            return {"run": ident, "outcome": "done", "retry": None, "blocked": None}
        return _settle(connection, row, block=detail if outcome == "blocked" else None)


def merged(connection, read: dict) -> bool:
    """The tick moves an `enqueued` run whose lane entry merged to `cleaning`, or to `checking` when auto-pickup
    queued it (`picked`) on the hub's lane, with a fresh beat and no supervisor. `checking` runs the hub's
    refresh and dashboard health; a satellite's run cleans, as a failed check there would turn auto-pickup off
    for every host (review of sd:8393).

    The compare-and-set matches the row read, at `enqueued` and open; the
    process columns of the supervisor that enqueued it are cleared, so the
    tick spawns a fresh one and the reap judges it as any step.
    """
    with transaction(connection):
        check = run_host(read["owner"]) is None and picked(connection, read["assignment"])
        return _after_merge(connection, read, "checking" if check else "cleaning")


def _after_merge(connection, read: dict, step: str) -> bool:
    stamp = clock()
    return connection.execute(
        f"UPDATE runner_run SET step = ?, heartbeat_at = ?, supervisor_pid = NULL, "
        f"supervisor_pgid = NULL, supervisor_start = NULL, builder_pgid = NULL, builder_start = NULL, "
        f"updated_at = ? WHERE id = ? AND step = 'enqueued' AND {_SAME}",
        (step, stamp, stamp, read["id"], *(read[name] for name in REAP_READS))).rowcount == 1


def draft_pull(read: dict) -> str | None:
    """The draft pull request an open `enqueued` run of an unmanaged repository waits on, or None (lane runs)."""
    detail = read.get("detail") or ""
    if read.get("step") != "enqueued" or read.get("outcome") is not None or not detail.startswith(DRAFT_PULL):
        return None
    return detail[len(DRAFT_PULL):].strip() or None


def pull_merged(connection, read: dict, url: str) -> bool:
    """The owners merged an unmanaged repository's draft pull request: the run goes to `cleaning`, never `checking`
    (nothing of ours deploys), and each delivered task still `in_progress` moves to `done`, in one transaction.

    The same compare-and-set as `merged`; False, with nothing written, when the run moved. A member the operator
    moved on stays as it is, and one that is no task gets a note: only a task closes by status.
    """
    with transaction(connection):
        if not _after_merge(connection, read, "cleaning"):
            return False
        for member in _members(connection, read["assignment"], "done"):
            row = connection.execute("SELECT kind, status FROM item WHERE id = ?", (member["item"],)).fetchone()
            if row is None or row["status"] != "in_progress":
                continue
            if row["kind"] != "task":
                add_note(connection, member["item"], "followup",
                         f"Build run {read['id']}: {url} merged; close this {row['kind']} item by hand")
                continue
            transition(connection, member["item"], "done", who="build",
                       reason=f"build run {read['id']}: its draft pull request {url} merged")
        return True


def lane_failed(connection, read: dict, reason: str, *, entry: str | None = None) -> bool:
    """The tick ends an `enqueued` run whose lane entry failed or was skipped: `lane_failed`, released, in one write.

    The worktree stays for Retry; the lane's own note names the step.
    A failure that is only prepare's merge of origin/main conflicting queues the bundle again (`_rebuild`), and hands
    the lane entry the watch read, `entry` by id, back to the builder (pack sd:8601).
    """
    with transaction(connection):
        stamp = clock()
        if connection.execute(
                f"UPDATE runner_run SET outcome = 'lane_failed', detail = ?, released_at = ?, updated_at = ? "
                f"WHERE id = ? AND step = 'enqueued' AND {_SAME}",
                (reason, stamp, stamp, read["id"], *(read[name] for name in REAP_READS))).rowcount != 1:
            return False
        connection.execute("UPDATE runner_lease SET released_at = ? WHERE run = ? AND released_at IS NULL",
                           (stamp, read["id"]))
        if conflict := CONFLICT.search(reason):
            _rebuild(connection, read, reason, stamp, conflict.group(1), entry)
        return True


def _rebuild(connection, read: dict, reason: str, stamp: str, base: str, entry: str | None = None) -> None:
    """Queue a bundle whose lane entry conflicted with main again under its lead, as Send to builder does, so the
    next builder merges main into the same branch (pack sd:6940). Each member still `in_progress` goes back to
    `ready`; one the operator moved stays out. A lead moved or left out of delivery, or past `CONFLICT_REBUILDS`
    on the lead's item, queues nothing: the run stays `lane_failed` for the operator.

    The branch's newest lane entry, the one the watch read (`entry`), is handed back to the builder as Fix hands
    it, so `lane retry` refuses it while the builder holds the worktree (pack sd:8601). One not read, another
    entry since, or none: no rebuild, and a note on the lead's item says why."""
    members, behind = _sendable(connection, read)
    if not members:
        return
    done = connection.execute("SELECT COUNT(*) FROM note WHERE item = ? AND body LIKE ?",
                              (members[0]["item"], REBUILD + "%")).fetchone()[0]
    if done >= CONFLICT_REBUILDS:
        return
    if waiting := _waiting(connection, members):
        add_note(connection, members[0]["item"], "comment",
                 f"Build run {read['id']}: no rebuild after the lane's merge of {base} conflicted, since {waiting}. "
                 "Fix it on Queue once that is done.")
        return
    finding = (f"{REBUILD}{base} conflicted. Merge {base} into {read['branch']}, resolve each conflict, run the tests the "
               f"conflict touched, and commit the merge; the lane said: {reason}"[:2000])
    try:
        key, found = _lane_entry(connection, read["branch"], entry, actor="the rebuild")
        if entry is not None and found.get("id") != entry:
            raise BuildRefused(f"branch {read['branch']}'s newest lane entry is {found.get('id')}, not {entry}")
    except BuildRefused as refusal:
        add_note(connection, members[0]["item"], "comment",
                 f"Build run {read['id']}: no rebuild after the lane's merge of {base} conflicted, since the lane "
                 f"entry was not handed back: {refusal}. Retry or Fix it on Queue.")
        return
    _hand_back(connection, key, found, read["id"], who="build", by="a conflict rebuild", stamp=stamp)
    _leave(connection, behind, who="build", run=read["id"], parked=True,
           note=f"left the rebuild of build run {read['id']}: it waits on an item of its bundle")
    _send_back(connection, members, finding, stamp, who="build",
               why=f"build run {read['id']}: the lane's merge of {base} conflicted", result=reason,
               said=f"rebuild {done + 1} of {CONFLICT_REBUILDS} after the lane's merge of {base} conflicted")


def _inside(connection, lead: int, members: list[dict]) -> tuple[list[dict], list[dict]]:
    """A dependency inside an ended bundle whose lead's item is `lead` (review of build/sd-1306): `ahead`, the
    members the lead waits on, directly or through other open items, and `behind`, every other member that
    waits on an item of the bundle. Neither can build in it: `ahead` leaves to build on its own (`_free`), and
    `behind` leaves the send-back, parked, as a subset Retry leaves one out."""
    bundle = {member["item"] for member in members}
    # A design aboard builds in the bundle with the members that wait on it: it is neither (class pass of sd:10096).
    riding = aboard(connection, {lead, *bundle})
    needed = _needed(connection, [lead], riding=riding)
    ahead = [member for member in members if member["item"] != lead and member["item"] in needed]
    behind = [member for member in members if member["item"] != lead and member not in ahead
              and _needed(connection, [member["item"]], riding=riding) & bundle]
    return ahead, behind


def _leave(connection, members: list[dict], *, who: str, run: str, note: str, parked: bool) -> None:
    """Each of `members` leaves its bundle, as a subset Retry leaves one out (`ALONE` with `LEFT_OUT`, so the split
    guard reads the lead's branch for its work): its item `ready`, parked or not, with `note` on it."""
    stamp = clock()
    for member in members:
        connection.execute("UPDATE assignment SET status = 'cancelled', ended = ?, result = ? WHERE id = ?",
                           (stamp, f"{ALONE}{who}: {LEFT_OUT}{run}", member["id"]))
        status = connection.execute("SELECT status FROM item WHERE id = ?", (member["item"],)).fetchone()["status"]
        if status in ("blocked", "in_progress"):
            transition(connection, member["item"], "ready", who=who, reason=note)
        if parked:
            park(connection, member["item"], ("ready",), stamp, f"Build assignment {member['id']} {note}; item parked")
        else:
            unpark(connection, member["item"])
        add_note(connection, member["item"], "comment", f"Build assignment {member['id']} {note}")


def _free(connection, ahead: list[dict], *, who: str, run: str, lead: int) -> list[int]:
    """`ahead` leaves its bundle unparked (`_leave`); the items of it free to build now, each that waits on nothing.
    One that waits stays ready, so auto-pickup or a Build takes it once its own dependency is done."""
    _leave(connection, ahead, who=who, run=run, parked=False,
           note=f"left its bundle: its lead sd:{lead} waits on it; it builds on its own")
    return [member["item"] for member in ahead if not waiting_on(connection, connection.execute(
        "SELECT id, fields FROM item WHERE id = ?", (member["item"],)).fetchone())]


def _sendable(connection, read: dict) -> tuple[list[dict], list[dict]]:
    """The members a send-back of `read`'s bundle queues again (`_sent_back`), less `behind` (`_inside`), and
    `behind`: Fix, its check and a conflict rebuild read the same split."""
    members = _sent_back(connection, read)
    behind = _inside(connection, members[0]["item"], members)[1] if members else []
    return [member for member in members if member not in behind], behind


def _waiting(connection, members: list[dict]) -> str:
    """What a send-back's members wait on, as Retry's `_item` reads it, or "": dispatch would block and park a
    bundle sent back while a dependency is open (review of pack sd:9570)."""
    riding = aboard(connection, [member["item"] for member in members])
    return "; ".join(f"item {member['item']} {why}" for member in members if (why := waiting_on(
        connection, connection.execute("SELECT id, fields FROM item WHERE id = ?", (member["item"],)).fetchone(),
        riding=riding)))


def _sent_back(connection, read: dict) -> list[dict]:
    """The members of `read`'s bundle a send-back queues again: each `done` one whose item is still `in_progress`,
    the lead first; none without the lead, since the claim would then pick a child's branch and worktree.
    A member whose item a newer build owns (`_current`) is that build's: its `in_progress` is not this bundle's,
    so Fix, a conflict rebuild and the Build path's split never move or park it (review of build/sd-1306)."""
    lead = read["assignment"]
    members = [member for member in _members(connection, lead, "done")
               if connection.execute("SELECT status FROM item WHERE id = ?",
                                     (member["item"],)).fetchone()["status"] == "in_progress"
               and _current(connection, member)]
    return members if members and members[0]["id"] == lead else []


def _send_back(connection, members: list[dict], finding: str, stamp: str, *, who: str, why: str, result: str,
               said: str) -> None:
    """Each member's item to `ready` with `finding` as its note, and its assignment queued again, as Send to builder
    does: the next builder resumes the same branch and worktree, and its brief quotes the finding (pack sd:6020)."""
    for member in members:
        transition(connection, member["item"], "ready", who=who, reason=why)
        add_note(connection, member["item"], "comment", finding)
        connection.execute("UPDATE assignment SET status = 'queued', queued_at = ?, started = NULL, ended = NULL, "
                           "result = ? WHERE id = ?", (stamp, f"requeued by {who}: {result}"[:600], member["id"]))
        add_note(connection, member["item"], "comment", f"Build assignment {member['id']} queued again by {who}: {said}")


def branch_run(connection, branch: str) -> dict | None:
    """The newest build run on `branch`, or None: the run that enqueued a lane entry on it (pack sd:8584)."""
    found = connection.execute(
        "SELECT runner_run.* FROM runner_run JOIN assignment ON assignment.id = runner_run.assignment "
        "WHERE runner_run.branch = ? AND assignment.scope = ? ORDER BY runner_run.created_at DESC, runner_run.run DESC "
        "LIMIT 1", (branch, SCOPE)).fetchone()
    return dict(found) if found else None


def lane_fix_check(connection, run: str) -> tuple[list[dict], str, dict]:
    """Whether Fix on a failed lane entry (pack sd:8584) takes build run `run`: its bundle's members to send back, and
    the lane entry's key and body to hand back. Fix's state on the page and its press both read this one check.

    Only one state passes: `run` is its lead's latest run, released, ended `lane_failed`; the lead assignment is
    `done`; a member is still `in_progress`; and the branch's newest lane entry failed (`_lane_entry`). Every other
    state is `BuildRefused` with its reason, so a blocked or cancelled rebuild is never a plain Send to builder."""
    read = connection.execute("SELECT * FROM runner_run WHERE id = ?", (run,)).fetchone()
    if read is None:
        raise BuildRefused(f"no build run {run}")
    latest = connection.execute("SELECT id FROM runner_run WHERE assignment = ? ORDER BY run DESC LIMIT 1",
                                (read["assignment"],)).fetchone()
    status = connection.execute("SELECT status FROM assignment WHERE id = ?", (read["assignment"],)).fetchone()
    if latest["id"] != run:
        raise BuildRefused(f"build run {latest['id']} is newer than {run}")
    if read["released_at"] is None:
        raise BuildRefused(f"build run {run} still holds its lease")
    if read["outcome"] != "lane_failed":
        raise BuildRefused(f"build run {run} ended {read['outcome'] or 'nothing yet'}, not lane_failed; Fix sends "
                           "back only a bundle whose lane entry failed")
    if status is None or status[0] != "done":
        raise BuildRefused(f"the bundle is {status[0] if status else 'gone'} again")
    members = _sendable(connection, dict(read))[0]
    if not members:
        raise BuildRefused("its lead item moved on since the lane failed; build it from Tasks")
    if waiting := _waiting(connection, members):
        raise BuildRefused(f"{waiting}; Fix sends the bundle back once that is done")
    named = LANE_ENTRY.match(read["detail"] or "")
    return members, *_lane_entry(connection, read["branch"], named.group(1) if named else None)


def lane_fix(connection, run: str, finding: str, *, who: str) -> list[int]:
    """Fix on a failed lane entry (pack sd:8584): the bundle whose run ended `lane_failed` goes back to its builder
    with `finding` as a Send to builder note, as a conflict's rebuild does; the items queued again. The lane entry is
    handed back to the builder first. The caller holds the transaction and checked the lead's revision. A state
    `lane_fix_check` refuses is `BuildRefused` and writes nothing."""
    members, key, entry = lane_fix_check(connection, run)
    _leave(connection, _sendable(connection, dict(connection.execute("SELECT * FROM runner_run WHERE id = ?",
                                                                     (run,)).fetchone()))[1],
           who=who, run=run, parked=True, note=f"left Fix of build run {run}: it waits on an item of its bundle")
    stamp = clock()
    _hand_back(connection, key, entry, run, who=who, by="Fix", stamp=stamp)
    said = f"Fix of build run {run}'s failed lane entry"
    _send_back(connection, members, FINDING + finding, stamp, who=who, why=said, result=said, said=said)
    return [member["item"] for member in members]


def _hand_back(connection, key: str, entry: dict, run: str, *, who: str, by: str, stamp: str) -> None:
    """The builder now owns the worktree: the lane entry at `key` is cancelled and `handed_back`, a new checkpoint
    row of its key, so `sd_lane.retry` refuses it (pack sd:8584). Fix and a conflict rebuild both write it."""
    entry.update(status="cancelled", finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 handed_back={"run": run, "by": who, "at": stamp},
                 reason=f"handed back to its builder by {by} (build run {run}); it was {entry['status']}: "
                        f"{entry.get('reason') or 'no reason kept'}"[:2000])
    connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                       (key, stamp, json.dumps(entry, sort_keys=True)))


def _lane_entry(connection, branch: str, ident: str | None = None, *, actor: str = "Fix") -> tuple[str, dict]:
    """The newest lane entry on `branch`, by the order the hub took them (`sd_lane.enqueue_order`), and its key, when
    it ended `failed` or `skipped`. Any other end is `BuildRefused`: pending or running is the lane's again (a Retry),
    and none read or one unreadable is unknown, never a failure to hand back (round 1 of pack sd:8584).

    With `ident`, the entry the watch read, only its repository's lane is read: its key names that repository, and an
    unreadable row in another repository's lane is not this branch's (pack sd:8615). Fix passes the one its run's
    detail names (pack sd:8636). Each refusal names `actor`, the step that reads it: Fix, or the rebuild."""
    ranges = [reads.LANE_KEYS]
    if ident is not None:
        suffix = f":{ident}"
        keys = [row[0] for row in connection.execute(
            "SELECT DISTINCT key FROM state WHERE kind = 'checkpoint' AND key >= ? AND key < ? AND substr(key, ?) = ?",
            (*reads.LANE_KEYS, -len(suffix), suffix))]
        if not keys:
            raise BuildRefused(f"lane entry {ident} is not stored; the lane's state is unknown, so {actor} writes nothing")
        # `lane:v1:<owner/name>:`; `;` is the character after `:`, so the two bound one repository's keys.
        ranges = [(prefix, prefix[:-1] + ";") for prefix in sorted({key[:-len(ident)] for key in keys})]
    rows = [row for low, high in ranges for row in connection.execute(
        "SELECT state.key, state.body, keys.first FROM state JOIN (SELECT key, MIN(id) AS first, MAX(id) AS last "
        "FROM state WHERE kind = 'checkpoint' AND key >= ? AND key < ? GROUP BY key) AS keys ON state.id = keys.last",
        (low, high))]
    found = []
    for row in rows:
        try:
            entry = json.loads(row["body"])
        except (TypeError, ValueError):
            raise BuildRefused(f"lane entry {row['key']} was not read; {actor} waits for a readable lane") from None
        if isinstance(entry, dict) and entry.get("branch") == branch:
            found.append((row["first"], row["key"], entry))
    if not found:
        raise BuildRefused(f"no lane entry holds branch {branch}; the lane's state is unknown, so {actor} writes "
                           "nothing")
    _, key, entry = max(found, key=lambda each: each[0])
    if entry.get("status") in ("pending", "running"):
        raise BuildRefused(f"the lane holds branch {branch} again ({entry['status']}); {actor} waits until it ends")
    if entry.get("status") not in ("failed", "skipped"):
        raise BuildRefused(f"branch {branch}'s newest lane entry is {entry.get('status')}; {actor} takes a failed one")
    return key, entry


class Probe:
    """The liveness probe: `alive` and `group` are True, False (ESRCH) or None (EPERM, unknown); `started` is `ps -o lstart=`."""

    @staticmethod
    def alive(pid: int) -> bool | None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return True

    @staticmethod
    def group(pgid: int) -> bool | None:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # macOS answers EPERM for a group of zombies only (sd:6406): one runs nothing, so it is gone.
            return False if _zombies_only(pgid) else None
        except OSError:
            return None
        return True

    @staticmethod
    def started(pid: int) -> str | None:
        try:
            done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                                  timeout=10, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        return " ".join(done.stdout.split()) or None


def _zombies_only(pgid: int) -> bool:
    """Whether `ps` reads every member of the group as a zombie (none left counts); False when unread."""
    try:
        done = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True,
                              timeout=10, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    if done.returncode != 0:
        return False
    rows = [line.split() for line in done.stdout.splitlines()]
    return all(row[1].startswith("Z") for row in rows if len(row) == 2 and row[0] == str(pgid))


def _age(stamp: str, now: str) -> float:
    return (datetime.fromisoformat(now) - datetime.fromisoformat(stamp)).total_seconds()


def alive(row: dict, *, now: str, probe=Probe) -> bool:
    """Whether a run's supervisor lives: a beat (or, before the first, the claim) under `STALE_SECONDS` old,
    and its pid answering with the process start it recorded.

    A probe that cannot answer is no verdict: the run counts as alive until
    its beat goes stale, which needs no probe.
    """
    if _age(row["heartbeat_at"] or row["created_at"], now) >= STALE_SECONDS:
        return False
    pid = row["supervisor_pid"]
    if pid is None:
        return True
    answer = probe.alive(pid)
    if answer is not True:
        return answer is None
    started = probe.started(pid)
    return started is None or started == row["supervisor_start"]


def same_process(pid: int | None, start: str | None, *, probe=Probe) -> bool:
    """Whether `pid` is the process a run recorded: alive, and started at `start`.

    Anything less is not proof: no pid or start recorded, a pid gone, a start
    that differs (the pid was reused) or one the probe cannot read. Nothing is
    signalled without it.
    """
    if pid is None or not start or probe.alive(pid) is not True:
        return False
    return probe.started(pid) == start


GONE, OURS, UNSURE = "gone", "ours", "unsure"
#: Outcomes of a run that ended unretried while a process may live: it keeps its lease until `_release`.
HELD = ("blocked", "abandoned")
#: Unsure reaps in a row before a run ends blocked.
DOUBTS = 3


def _side(pid: int | None, start: str | None, pgid: int | None, *, probe=Probe) -> str:
    """One process of a run, and its group, as gone, ours or unsure: the one probe `verdict` takes twice.

    - Gone: nothing recorded (no process was spawned), or ESRCH on the group
      with the pid dead or now started at another time.
    - Ours: the pid answers with the start the run recorded, and its group
      is recorded, so a kill can reach every child.
    - Unsure: anything else, such as a pid with no group recorded, a group
      with members while its leader is dead or reused, EPERM, an unread
      start or an unknown probe.
    """
    if pid is None and pgid is None:
        return GONE
    if pid is not None and same_process(pid, start, probe=probe):
        return OURS if pgid is not None else UNSURE
    if pgid is None or probe.group(pgid) is not False:
        return UNSURE
    if pid is None:
        return GONE
    answer = probe.alive(pid)
    if answer is False:
        return GONE
    started = probe.started(pid) if answer is True else None
    return GONE if started is not None and started != start else UNSURE


def verdict(row: dict, *, probe=Probe) -> tuple[str, list[int]]:
    """What a reap may do to an ended run's processes, and the groups to kill: every gone, release or retry asks it.

    The supervisor (`supervisor_pid`, `supervisor_pgid`) and the builder
    (`builder_pgid` and its leader) are each a `_side`; the run is `UNSURE`
    when either is, else `OURS` when either is, else `GONE`. So nothing is
    released or retried until ESRCH answers on both groups, and a paused
    supervisor, or a child of a dead one, cannot run beside a replacement.
    """
    sides = [(_side(row["supervisor_pid"], row["supervisor_start"], row["supervisor_pgid"], probe=probe),
              row["supervisor_pgid"]),
             (_side(row["builder_pgid"], row["builder_start"], row["builder_pgid"], probe=probe), row["builder_pgid"])]
    states = [state for state, _ in sides]
    state = UNSURE if UNSURE in states else OURS if OURS in states else GONE
    return state, [group for side, group in sides if side == OURS] if state == OURS else []


def descendant_groups(pgid: int) -> dict[int, str] | None:
    """The groups that descendants of `pgid`'s members run in now, apart from `pgid`, each with its leader's
    start (`ps lstart`, "" when no leader lives); None when `ps` did not answer.

    `sd gate run` puts an unmanaged run's check in a group of its own, so a kill of the recorded groups alone
    left it running beside the retry (review of pack sd:7868). Read live, every group is one the run's own
    processes started, not a recorded number a reused pid may hold now.
    """
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,lstart="], capture_output=True, text=True,
                                 timeout=10, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if listing.returncode != 0:
        return None
    children: dict[int, list[tuple[int, int]]] = {}
    starts: dict[int, str] = {}
    todo: list[int] = []
    for line in listing.stdout.splitlines():
        fields = line.split()
        if len(fields) < 3 or not all(field.isdigit() for field in fields[:3]):
            continue
        pid, parent, group = map(int, fields[:3])
        starts[pid] = " ".join(fields[3:])
        children.setdefault(parent, []).append((pid, group))
        if group == pgid:
            todo.append(pid)
    found: dict[int, str] = {}
    seen = set(todo)
    while todo:
        for pid, group in children.get(todo.pop(), []):
            if pid not in seen:
                seen.add(pid)
                todo.append(pid)
                if group not in (pgid, os.getpgrp()) and group > 1:
                    found[group] = starts.get(group, "")
    return found


def kill_group(pgid: int) -> bool:
    """SIGKILL a builder's process group and each group its members' descendants run in; True once ESRCH
    answers on every one, False while one may live.

    A `ps` that did not answer leaves only the recorded group to signal and
    read: its ESRCH is True, as `end_group` reads it. A failed read blocked
    every reap, of managed builds too, with every group gone (review of
    3072db937).
    """
    if not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp():
        return False
    below = descendant_groups(pgid)
    groups = [pgid, *sorted(below or ())]
    for group in groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass  # ESRCH: gone; EPERM may be a group of zombies only: the probe decides
    for _ in range(20):
        if all(Probe.group(group) is False for group in groups):
            return True
        time.sleep(0.1)
    return False


def reap(connection, *, now: str | None = None, probe=Probe, kill=kill_group,
         hosts: Collection[str | None] | None = None) -> list[dict]:
    """End every open build run that fails `alive`; the runs settled, each `{run, outcome, retry}`.

    1. Compare-and-set `outcome` (`interrupted`, or `cancelled` when a cancel
       was asked) where every `REAP_READS` column is the value read; a lost
       race means a writer moved the run (a beat, a registration, `enqueued`,
       a cancel), and the run is left alone this tick.
    2. Take the `verdict`. `OURS`: kill the supervisor's group, then the
       builder's; a group that still has a process after the SIGKILL ends
       the run `blocked`, not retried, unreleased with its lease until
       `_release` finds every group gone. `UNSURE`:
       the run stays ended and unreleased, its lease held, nothing killed;
       the `DOUBTS`th unsure reap in a row ends it blocked, not retried.
    3. `_settle` it in one transaction.

    A held run (`HELD`) goes to `_release` at any step, `enqueued` too. An
    open run at `enqueued` is left alone: it waits on the lane, and no
    supervisor runs until the merge; `checking` and `cleaning` run under a
    fresh supervisor and take the verdict like any step. Nothing is reaped while a restore
    reimport is pending: `abandon` ended
    every run the restore found, and a run it did not find is not this
    store's to judge.

    Only the runs of `hosts` (`lane_hosts` when None) are judged: a pid or a
    group of another machine's run means nothing here (pack sd:7933). The
    hub then blocks another host's quiet runs without a probe, their leases kept (`_strand`).
    """
    hosts = lane_hosts(connection) if hosts is None else hosts
    settled = _reap(connection, open_runs, _settle, now=now, probe=probe, kill=kill, hosts=hosts)
    if None in hosts and not restoration_pending(connection):
        settled += _strand(connection, hosts, now=now or clock())
    return settled


def _strand(connection, hosts: Collection[str | None], *, now: str) -> list[dict]:
    """The hub ends another host's open runs blocked once both its dispatcher and the run went quiet (review
    rounds 3 and 4).

    Quiet is `dispatcher_gone` for the run's host and no run beat for
    `STALE_SECONDS`, read by `_lead_age`: that host stamps the beat on its
    own clock, so one too far ahead has no age and is no beat (pack
    sd:7970). A dispatcher beat too far ahead (`_ahead`) is a fast clock,
    not a dead dispatcher: that host's ticks still reap its own runs, so
    the hub leaves them while `_beat_moves` reads new beats recorded; once
    they stop, the runs strand as any quiet host's (pack sd:8001). A missing beat does not prove that host's processes
    stopped, so the run keeps its lease: it ends `blocked` with the reason
    (a pending cancel named in it), unreleased, and no Retry or claim takes
    its items until that host's own reap reads its groups gone and releases
    it. A held run stays held. No pid or group is probed or signalled: they
    are another machine's. A run at a lane step waits on that host's lane.
    """
    settled: list[dict] = []
    for row in open_runs(connection):
        host = run_host(row["owner"])
        if mine(row, hosts) or host is None or row["outcome"] is not None or row["step"] in LANE_STEPS:
            continue
        quiet = _lead_age(row["heartbeat_at"] or row["created_at"], now)
        if quiet is not None and quiet < STALE_SECONDS:
            continue
        gone = dispatcher_gone(connection, host)
        if gone is None:
            continue
        if _ahead(connection, host) is not None:
            if _beat_moves(connection, host, now):
                continue  # a beat too far ahead is skew: that host's ticks still reap its own runs
            gone = f"{gone}, and the hub recorded no beat from it for {STALE_SECONDS} s"
        detail = f"{host}'s dispatcher went quiet ({gone}) and the run sent no beat for {STALE_SECONDS} s"
        if row["cancel_requested"]:
            detail = f"{row['cancel_requested']}, but {detail}"
        with transaction(connection):
            if connection.execute(
                    f"UPDATE runner_run SET outcome = 'interrupted', detail = ?, updated_at = ? WHERE id = ? "
                    f"AND {_SAME}", (detail, clock(), row["id"], *(row[name] for name in REAP_READS))).rowcount != 1:
                continue
        result = _settle(connection, {**row, "outcome": "interrupted", "detail": detail},
                         block=f"the hub ended it on {host}: {detail}; it releases once {host} reads its groups gone")
        if result is not None:
            settled.append(result)
    return settled


def _reap(connection, runs, settle, *, now: str | None, probe, kill,
          hosts: Collection[str | None] | None = None) -> list[dict]:
    """`reap` over `runs(connection, hosts)`, each ended run passed to `settle`: a draft run's reap is this one too."""
    now = now or clock()
    settled: list[dict] = []
    if restoration_pending(connection):
        return settled
    for row in runs(connection, lane_hosts(connection) if hosts is None else hosts):
        if row["outcome"] in HELD:
            _release(connection, row, probe=probe)
            continue
        if row["step"] == "enqueued" and row["outcome"] is None:
            continue
        if row["outcome"] is None:
            if alive(row, now=now, probe=probe):
                continue
            outcome = "cancelled" if row["cancel_requested"] else "interrupted"
            detail = (row["cancel_requested"] if outcome == "cancelled"
                      else f"supervisor lost at step {row['step']}: no live beat for {STALE_SECONDS} s")
            with transaction(connection):
                if connection.execute(
                        f"UPDATE runner_run SET outcome = ?, detail = ?, updated_at = ? WHERE id = ? AND {_SAME}",
                        (outcome, detail, clock(), row["id"], *(row[name] for name in REAP_READS))).rowcount != 1:
                    continue
            row = {**row, "outcome": outcome, "detail": detail}
        (state, groups), block = verdict(row, probe=probe), None
        if state == UNSURE:
            doubts = _doubt(connection, row["id"])
            if doubts < DOUBTS:
                continue
            block = (f"{doubts} reaps in a row could not tell whether builder group {row['builder_pgid']} "
                     f"or supervisor {row['supervisor_pid']} still runs; nothing was killed")
        elif state == OURS:
            survivors = [group for group in groups if not kill(group)]
            if survivors:
                block = f"process group {', '.join(map(str, survivors))} still had a process after SIGKILL"
        result = settle(connection, row, block=block)
        if result is not None:
            settled.append(result)
    return settled


def _release(connection, read: dict, *, probe=Probe) -> bool:
    """Release a held run (`blocked` or `abandoned`), and its lease, once `verdict` reads it gone; True if released.

    The probe alone decides: no kill, no retry, no note. Unsure or alive
    leaves the run as it is for the next tick. The write matches the row
    read, as the reap's compare-and-set does.
    """
    if verdict(read, probe=probe)[0] != GONE:
        return False
    with transaction(connection):
        stamp = clock()
        if connection.execute(f"UPDATE runner_run SET released_at = ?, updated_at = ? WHERE id = ? AND {_SAME}",
                              (stamp, stamp, read["id"], *(read[name] for name in REAP_READS))).rowcount != 1:
            return False
        connection.execute("UPDATE runner_lease SET released_at = ? WHERE run = ? AND released_at IS NULL",
                           (stamp, read["id"]))
        for member in _members(connection, read["assignment"], "blocked"):
            cancel_unwanted_blocked(connection, member["item"], stamp,
                                    f"build run {read['id']} released; item is done or in planning")
        if read["outcome"] == "blocked":
            _park(connection, read["id"], read["assignment"], stamp)
        return True


def _doubt(connection, ident: str) -> int:
    """Count one unsure reap on an ended, unreleased run; the count so far (0 when the run moved on)."""
    with transaction(connection):
        connection.execute("UPDATE runner_run SET doubts = COALESCE(doubts, 0) + 1, updated_at = ? "
                           "WHERE id = ? AND released_at IS NULL", (clock(), ident))
        row = connection.execute("SELECT doubts FROM runner_run WHERE id = ? AND released_at IS NULL",
                                 (ident,)).fetchone()
        return 0 if row is None else row["doubts"]


def _settle(connection, read: dict, *, block: str | None = None) -> dict | None:
    """Release an ended run and its lease, then retry it, block it, or end it cancelled, in one transaction.

    A retry is a fresh row: `run` + 1, the same worktree and branch, the
    `RESUME` step, the old `review_round` and `session_id`, no outcome; the
    old row's outcome stays set, so a late old supervisor writes nothing. A
    second loss at the same step blocks the bundle. A run with
    `cancel_requested` ends cancelled whatever its outcome: no fresh row,
    and each item back at ready parked, so auto-pickup leaves it (sd:9222).

    `read` is the row the reap decided on; nothing happens unless every
    `REAP_READS` column still holds its value (a cancel since then moves it).

    `block` names processes the reap could not end or could not judge: the
    bundle ends blocked, never retried, whatever the outcome.
    """
    ident = read["id"]
    with transaction(connection):
        row = connection.execute(f"SELECT * FROM runner_run WHERE id = ? AND outcome IS NOT NULL AND {_SAME}",
                                 (ident, *(read[name] for name in REAP_READS))).fetchone()
        if row is None:
            return None
        stamp = clock()
        lead = row["assignment"]
        members = _members(connection, lead, "done" if row["step"] in LANE_STEPS else "running")
        if block is not None:
            # A process may live: the run stays unreleased and keeps its
            # lease, so it still counts in `room`, until `_release` finds
            # every group gone.
            reason = _blocked(row, block)
            connection.execute("UPDATE runner_run SET outcome = 'blocked', detail = ?, updated_at = ? WHERE id = ?",
                               (reason, stamp, ident))
            _block(connection, members, reason, stamp, followup=not asked(row, reason))
            return {"run": ident, "outcome": "blocked", "retry": None, "blocked": block}
        connection.execute("UPDATE runner_run SET released_at = ?, updated_at = ? WHERE id = ?", (stamp, stamp, ident))
        connection.execute("UPDATE runner_lease SET released_at = ? WHERE run = ? AND released_at IS NULL", (stamp, ident))
        retry = None
        if row["outcome"] == "cancelled" or row["cancel_requested"]:
            for member in members:
                connection.execute("UPDATE assignment SET status = 'cancelled', ended = ?, result = ? WHERE id = ?",
                                   (stamp, row["detail"], member["id"]))
                _move(connection, member["item"], "ready", f"build run {ident} {row['detail']}; worktree {row['work_path']} kept")
                # Parked, so auto-pickup does not queue it again on its next tick (sd:9222).
                park(connection, member["item"], ("ready",), stamp, CANCEL_PARKED.format(run=ident))
        elif row["outcome"] == RATE_LIMITED:
            # No loss and no block (pack sd:9907): the account, not the work, stopped it. The bundle queues again
            # as it was, its place kept, and its host claims nothing until the limit resets (`rate_hold`).
            host = run_host(row["owner"]) or this_host()
            until = hold_until(row["final"], stamp)
            set_setting(connection, hold_key(host), until)
            for member in members:
                connection.execute("UPDATE assignment SET status = 'queued', started = NULL, ended = NULL, "
                                   "result = NULL WHERE id = ?", (member["id"],))
                _move(connection, member["item"], "ready", f"build run {ident} hit the usage limit; queued again")
                add_note(connection, member["item"], "comment",
                         f"Build run {ident} hit the builder account's usage limit at step {row['step']}; "
                         f"assignment {member['id']} queued again; {host} builds from {until}")
        elif row["outcome"] in LOSSES:
            marks = ", ".join("?" for _ in LOSSES)
            losses = connection.execute(f"SELECT COUNT(*) FROM runner_run WHERE assignment = ? AND step IS ? "
                                        f"AND outcome IN ({marks})", (lead, row["step"], *LOSSES)).fetchone()[0]
            if not members:
                # Nothing of the bundle runs any more (a hand edit, an old
                # restore): released, and nothing to retry.
                pass
            elif losses <= RETRIES:
                step = row["step"] or "claimed"
                retry = _open_run(connection, lead, repo=row["repo"], owner=row["owner"], step=RESUME.get(step, step),
                                  model=row["model"] or OPUS, work=Path(row["work_path"]), stamp=stamp,
                                  session_id=row["session_id"], review_round=row["review_round"])
                for member in members:
                    add_note(connection, member["item"], "comment",
                             f"Build run {ident} lost at step {row['step']}; retried as run {retry}")
            elif row["step"] == "checking":
                retry = _health_unknown(connection, row, members, stamp)
            elif row["step"] == "cleaning":
                # Merged, and the lane owns the items: nothing is blocked, the worktree just stays.
                connection.execute("UPDATE runner_run SET outcome = 'done', updated_at = ? WHERE id = ?",
                                   (stamp, ident))
                for member in members:
                    add_note(connection, member["item"], "comment",
                             f"Build run {ident} lost at cleaning a second time; worktree {row['work_path']} kept: "
                             f"remove it by hand once it is clean")
                return {"run": ident, "outcome": "done", "retry": None, "blocked": None}
            else:
                reason = (f"build run {ident} lost at step {row['step']} a second time; "
                          f"log {row['log_path'] or 'none'}, worktree {row['work_path']}")
                _block(connection, members, reason, stamp)
                _park(connection, ident, lead, stamp)
        return {"run": ident, "outcome": row["outcome"], "retry": retry, "blocked": block}


def _health_unknown(connection, row, members: list[dict], stamp: str) -> str:
    """A second loss at `checking`: the merge's health is unknown, so auto-pickup goes off first, an item is filed,
    and a fresh row goes on to `cleaning` (design, "Run ownership by step"); the fresh row's id."""
    set_setting(connection, AUTO_PICKUP, None)
    filed = create_item(connection, kind="task", title=f"Build run {row['id']}: health unknown after its merge",
                        repo=row["repo"])
    add_note(connection, filed, "comment",
             f"Build run {row['id']} was lost at checking a second time, so the health check after its merge has no "
             f"answer. Auto-pickup ({AUTO_PICKUP}) is off. Run `repo-sync.sh refresh` and `dashboard.sh health`, "
             f"then turn it on again. Log {row['log_path'] or 'none'}.")
    retry = _open_run(connection, row["assignment"], repo=row["repo"], owner=row["owner"], step="cleaning",
                      model=row["model"] or OPUS, work=Path(row["work_path"]), stamp=stamp,
                      session_id=row["session_id"], review_round=row["review_round"])
    for member in members:
        add_note(connection, member["item"], "comment",
                 f"Build run {row['id']} lost at checking a second time; auto-pickup is off, item {filed} filed, "
                 f"run {retry} cleans up")
    return retry


def _blocked(row, block: str) -> str:
    """The reason a blocked run `row` records for its `block`."""
    return (f"build run {row['id']} ended at step {row['step']}: {block}; check the group, then requeue or "
            f"cancel; log {row['log_path'] or 'none'}, worktree {row['work_path']}")


def asked(row, reason: str) -> bool:
    """Whether `reason` is, whole, run `row`'s reason for an `ASKED` block, its list `sd:N, sd:N` read from the
    reason itself. The recorded reason alone decides: members and notes move after the run ends (class pass of
    sd:9219), and a reason that only quotes `ASKED`, such as a review finding, is another block."""
    head, tail = _blocked(row, ASKED + "\0").split("\0")
    return re.fullmatch(re.escape(head) + r"sd:\d+(, sd:\d+)*" + re.escape(tail), reason) is not None


def _block(connection, members: list[dict], reason: str, stamp: str, *, followup: bool = True) -> None:
    """Block each member, with a followup unless `followup` is False: a run blocked by its questions (`ASKED`)
    asks the operator in them already (sd:9219)."""
    for member in members:
        connection.execute("UPDATE assignment SET status = 'blocked', ended = ?, result = ? WHERE id = ?",
                           (stamp, reason, member["id"]))
        _move(connection, member["item"], "blocked", reason)
        if followup:
            add_note(connection, member["item"], "followup",
                     f"Build blocked: {reason}. Look at the log and the worktree, then requeue or cancel.")


def _park(connection, ident: str, lead: int, stamp: str) -> None:
    """Park each item of a blocked run, once the run and its lease are released, as the nightly prune parks an idle
    one (pack sd:6132, sd:6008). A run blocked by its questions (`ASKED`) parks none: its items wait on the
    operator's answers, and a park would hide them (sd:9219)."""
    row = connection.execute("SELECT * FROM runner_run WHERE id = ?", (ident,)).fetchone()
    if asked(row, row["detail"] or ""):
        return
    for member in _members(connection, lead, "blocked"):
        park(connection, member["item"], ("blocked",), stamp, PARKED.format(run=ident))


def parks(connection, item: int, statuses: tuple[str, ...], ending: tuple[int, ...] = ()) -> bool:
    """Whether `park` parks `item` once the assignments `ending` have ended: the one rule `park` reads, and what a
    cancel's confirm reads to name the items it parks (pack sd:9290)."""
    row = connection.execute("SELECT status, parked_at FROM item WHERE id = ?", (item,)).fetchone()
    if row is None or row["status"] not in statuses or row["parked_at"] is not None:
        return False
    marks = ", ".join("?" for _ in ending)
    return connection.execute(f"SELECT 1 FROM assignment WHERE item = ? AND status IN {OWNING}"
                              + (f" AND id NOT IN ({marks})" if ending else "") + " LIMIT 1",
                              (item, *ending)).fetchone() is None


def park(connection, item: int, statuses: tuple[str, ...], stamp: str, note: str | None = None) -> bool:
    """Park `item` while its status is one of `statuses`, with `note` when given; whether it parked. An item the
    operator moved on, one parked already, or one an assignment still owns (another build queued since, sd:8960)
    stays as it is. Auto-pickup takes unparked items only (sd:9222)."""
    if not parks(connection, item, statuses):
        return False
    set_item_fields(connection, item, parked_at=stamp)
    if note:
        add_note(connection, item, "comment", note)
    return True


def unpark(connection, item: int) -> None:
    """Clear an item's park, as Retry runs it again: a parked item would stay hidden while it builds (pack sd:6132)."""
    row = connection.execute("SELECT parked_at FROM item WHERE id = ?", (item,)).fetchone()
    if row is not None and row["parked_at"] is not None:
        set_item_fields(connection, item, parked_at=None)


def abandon(connection, reason: str) -> list[str]:
    """End every unreleased build or draft run `abandoned`, never retried; the ids.

    A restore and a promotion call it in the transaction that blocks the
    assignments: the rows came from a store, or a machine, that is no longer
    this one, so nothing may signal or retry them from here. Each keeps its
    lease, as a `blocked` run does, until a reap's `_release` finds its
    groups gone: a supervisor of the old store may still run here.
    """
    with transaction(connection):
        stamp = clock()
        runs = [row["id"] for row in connection.execute(
            "SELECT runner_run.id, runner_run.outcome FROM runner_run JOIN assignment "
            "ON assignment.id = runner_run.assignment WHERE runner_run.released_at IS NULL AND assignment.scope IN (?, ?) "
            "ORDER BY runner_run.created_at, runner_run.id", (SCOPE, DRAFT)) if row["outcome"] not in HELD]
        for ident in runs:
            connection.execute("UPDATE runner_run SET outcome = 'abandoned', detail = COALESCE(detail, ?), "
                               "updated_at = ? WHERE id = ?", (reason, stamp, ident))
        return runs


def _move(connection, item: int, target: str, reason: str) -> None:
    row = connection.execute("SELECT status FROM item WHERE id = ?", (item,)).fetchone()
    if row is not None and row["status"] != target and row["status"] != "done":
        transition(connection, item, target, who="build", reason=reason)
