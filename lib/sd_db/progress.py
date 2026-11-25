"""Stable artifact links, explicit work completion, and tracker freshness.

    Local task progress never requires a tracker call. Delivering code is a
    different operation: its exact commit is verified against the repository
    before a completion receipt is written. Cached issue state authorizes none
    of these writes.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from . import paths
from .database import transaction
from .reads import KEPT_FOLLOWUPS
from .repos import same_remote
from .shadow_sync import read_watermark
from .workflow import WorkflowError, _checked_state, _text, done_resolves, item_state
from .writes import _transition, add_note, resolve_note, set_item_fields, stamp
from .writes import now as current_time


def _work_guard(connection: sqlite3.Connection, row: dict) -> str:
    if row["kind"] != "work":
        return "this operation is for work items; ordinary tasks use task controls"
    active = connection.execute(
        "SELECT id FROM assignment WHERE item = ? AND status IN ('queued', 'running', 'ending') LIMIT 1",
        (row["id"],),
    ).fetchone()
    if active:
        return f"item {row['id']} has queued, running or ending assignment {active['id']}"
    return ""


#: The task kinds `task_guard` admits (sd:1005).
CANCELLABLE_TASK_KINDS = ("task", "followup")


def task_guard(connection: sqlite3.Connection, row: dict) -> str:
    """The guard a task or followup cancel passes to `cancel_work` (sd:1005).

    A recurring task is refused: `change_status` spawns its next occurrence
    on `done` and a cancel does not, so a cancel would end the series
    silently. Clear the recurrence first, or complete it.
    """
    if row["kind"] not in CANCELLABLE_TASK_KINDS:
        return "this operation is for task and followup items; work uses work controls"
    if row["recurrence"] is not None:
        return "a recurring task cannot be cancelled; clear its recurrence first, or complete it"
    active = connection.execute(
        "SELECT id FROM assignment WHERE item = ? AND status IN ('queued', 'running', 'ending') LIMIT 1",
        (row["id"],),
    ).fetchone()
    if active:
        return f"item {row['id']} has queued, running or ending assignment {active['id']}"
    return ""


def work_controls(connection: sqlite3.Connection, item: int) -> dict:
    """Render the same availability that the mutation checks under its lock."""
    row = item_state(connection, item)["item"]
    reason = _work_guard(connection, row)
    return {"cancel": not reason and row["status"] != "done", "relink": not reason,
            "deliver": not reason and row["status"] != "done", "reason": reason}


def _work(connection: sqlite3.Connection, item: int, expected_revision: str | None, guard=_work_guard) -> dict:
    state = _checked_state(connection, item, expected_revision)
    reason = guard(connection, state["item"])
    if reason:
        raise WorkflowError(reason)
    return state


def _relative(path: str) -> str:
    value = _text(path, "path")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts or str(parsed) in (".", "") or "\\" in value:
        raise WorkflowError("artifact path must be relative to its registered repository without '..'")
    return parsed.as_posix()


def item_for_artifact(connection: sqlite3.Connection, repo: str, path: str) -> sqlite3.Row | None:
    """Resolve exact current links first, retaining original import identity.

    An archive move with the same slug may still be found through its original
    identity. A renamed artifact is linked explicitly by item ID. Titles are
    not identities and are never compared.
    """
    path = _relative(path)
    # Every stored form of the repository: the key, and the absolute form a
    # row written before migration 014 holds (sd:1439).
    probe = paths.keys(repo)
    marks = paths.placeholders(probe)
    rows = list(connection.execute(
        f"SELECT * FROM item WHERE kind = 'work' AND repo IN ({marks}) AND path = ? ORDER BY id",
        (*probe, path),
    ))
    if len(rows) > 1:
        raise WorkflowError(f"artifact {path!r} is linked to multiple items; resolve the ambiguity by item ID")
    if rows:
        return rows[0]
    parts = PurePosixPath(path).parts
    if len(parts) < 4 or parts[:2] != ("docs", "work") or parts[-1] != "prd.md":
        return None
    identities = tuple(f"{key}::docs/work/{parts[-2]}/prd.md" for key in probe)
    rows = list(connection.execute(
        f"SELECT * FROM item WHERE kind = 'work' AND repo IN ({marks}) AND source = 'docs/work' "
        f"AND external_id IN ({marks}) ORDER BY id", (*probe, *identities),
    ))
    if len(rows) > 1:
        raise WorkflowError(f"artifact {path!r} has ambiguous original identity")
    if not rows:
        return None
    # An explicit relink is authoritative. The legacy key must not make the
    # retired old location look like a second current copy of the item.
    fields = _fields(rows[0]["fields"])
    if fields.get("artifact_link") and rows[0]["path"] != path:
        return None
    return rows[0]


#: An integer literal of more digits reads as unset (None) in a display read: int() refuses one past 4,300 digits
#: with ValueError, and json.dumps refuses to write one back (pack sd:7329).
INT_DIGITS = 4000


def small_int(digits: str) -> int | None:
    """`parse_int` for a read that shows the value: the int, or None past `INT_DIGITS` digits."""
    return int(digits) if len(digits) <= INT_DIGITS else None


def _fields(raw: str | None, *, readable: bool = False) -> dict:
    """The stored JSON object. A write path keeps the default, so a number is never rewritten as null;
    a display read passes `readable` and gets None for an integer past `INT_DIGITS` digits."""
    if raw is None:
        return {}
    try:
        value = json.loads(raw, parse_int=small_int if readable else None)
    except (TypeError, ValueError):
        raise WorkflowError("item metadata is malformed; repair it before changing progress") from None
    if not isinstance(value, dict):
        raise WorkflowError("item metadata must be an object")
    return value


def _moment(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def completion_record(row) -> dict | None:
    """A valid local receipt, bound to this completed item and repository.

    This checks the persisted evidence shape, not a second live delivery.
    Only deliver_work and cancel_work create these through user controls.
    The receipt's repository is compared in the row's keyed form, so a
    receipt written before migration 014 still binds; the stored receipt
    is returned unchanged.
    """
    try:
        value = _fields(row["fields"]).get("completion")
        # A task or followup carries a cancel receipt only, and may have no
        # repository (sd:1005); delivery stays work's alone.
        task = row["kind"] in CANCELLABLE_TASK_KINDS and isinstance(value, dict) and value.get("outcome") == "cancelled"
        if not isinstance(value, dict) or (row["kind"] != "work" and not task) or row["status"] != "done":
            return None
        if (type(value.get("item")) is not int or value["item"] != row["id"]
                or not paths.same_key(value.get("repo"), row["repo"])):
            return None
        if (not row["repo"] and not task) or _moment(value.get("at")) is None or not isinstance(value.get("who"), str) or not value["who"].strip():
            return None
        if value.get("outcome") == "cancelled":
            return value if isinstance(value.get("reason"), str) and value["reason"].strip() and row["shipped_at"] is None else None
        if value.get("outcome") != "delivered":
            return None
        if any(not isinstance(value.get(key), str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value[key]) is None
               for key in ("commit", "verified_tip")):
            return None
        if not isinstance(value.get("verified_ref"), str) or not value["verified_ref"]:
            return None
        return value if _moment(value.get("shipped_at")) is not None and row["shipped_at"] == value["shipped_at"] else None
    except (KeyError, IndexError, WorkflowError):
        return None


def relink_artifact(
    connection: sqlite3.Connection, item: int, path: str, *,
    who: str, expected_revision: str | None = None,
) -> dict:
    """Change the current artifact path while keeping the row's source key."""
    who = _text(who, "who")
    relative = _relative(path)
    with transaction(connection):
        state = _work(connection, item, expected_revision)
        row = state["item"]
        root = paths.expand(row["repo"]).resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise WorkflowError(f"artifact {relative!r} must be an existing file inside its registered "
                                f"repository {root}; give the path relative to that root")
        other = connection.execute(
            "SELECT id FROM item WHERE repo = ? AND path = ? AND id != ? LIMIT 1",
            (row["repo"], relative, item),
        ).fetchone()
        if other:
            raise WorkflowError(f"artifact is already linked to item {other['id']}; "
                                f"relink item {other['id']} to another path first")
        if relative == row["path"]:
            return state
        fields = _fields(row["fields"])
        fields["artifact_link"] = {"path": relative, "at": current_time(), "who": who}
        set_item_fields(connection, item, path=relative, fields=fields)
        add_note(connection, item, "comment",
                 f"Relinked artifact from {row['path'] or '(none)'} to {relative} by {who}", session=who)
        return item_state(connection, item)


def cancel_work(
    connection: sqlite3.Connection, item: int, *, reason: str, who: str,
    expected_revision: str | None = None, guard=_work_guard,
) -> dict:
    """Explicitly cancel an item without pretending it shipped.

    The caller names the guard (sd:1005): the work guard, the default, for
    `sd work cancel` and the dashboard's work panel; `task_guard` for a task
    or followup. The receipt and the `done` transition are the same for both.
    """
    who = _text(who, "who")
    reason = _text(reason, "reason")
    with transaction(connection):
        state = _work(connection, item, expected_revision, guard)
        row = state["item"]
        fields = _fields(row["fields"])
        previous = completion_record(row) or {}
        if row["status"] == "done":
            if isinstance(previous, dict) and previous.get("outcome") == "cancelled":
                return state
            raise WorkflowError(f"completed {row['kind']} cannot be reclassified as cancelled")
        fields["completion"] = {
            "outcome": "cancelled", "item": item, "repo": row["repo"],
            "at": current_time(), "who": who, "reason": reason,
        }
        set_item_fields(connection, item, fields=fields)
        # A cancel is the operator's answer to the item's open questions: no
        # one will do it, so none waits in Decisions (sd:7992). A delivery
        # answers none (`change_status`, pack sd:6329).
        asked = [note["id"] for note in state["notes"] if note["kind"] == "question" and not note["resolved_at"]]
        for note in asked:
            resolve_note(connection, note)
        closed = (f"; resolved open question{'s' if len(asked) > 1 else ''} " + ", ".join(map(str, asked))
                  if asked else "")
        _transition(connection, item, "done", who=who, reason=f"cancelled: {reason}{closed}")
        return item_state(connection, item)


def _git(root: Path, *args: str, input: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], input=input, capture_output=True,
            text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkflowError(f"could not verify delivery with git: {error}") from None
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise WorkflowError(f"delivery verification failed: {detail[0] if detail else 'git returned no answer'}")
    return result.stdout.strip()


def _delivery_evidence(row: dict, commit: str, *, verification_root: Path | None = None,
                       registered_remote: str | None = None, trailer: str = "delivers") -> dict:
    root = verification_root or paths.expand(row["repo"])
    if not root.is_dir():
        raise WorkflowError("registered repository is unavailable; delivery cannot be verified")
    if verification_root is not None:
        origin = _git(root, "remote", "get-url", "origin")
        if not same_remote(origin, registered_remote):
            raise WorkflowError("verification clone origin does not match the registered repository")
    remotes = _git(root, "remote").splitlines()
    if remotes:
        remote = "origin" if "origin" in remotes else remotes[0]
        head = _git(root, "ls-remote", "--symref", remote, "HEAD").splitlines()
        references = [line.split("\t", 1)[0][5:] for line in head if line.startswith("ref: ")]
        tips = [line.split("\t", 1)[0] for line in head if not line.startswith("ref: ")]
        if len(references) != 1 or len(tips) != 1 or not references[0].startswith("refs/heads/"):
            raise WorkflowError("remote default branch could not be verified")
        reference, advertised = references[0], tips[0]
        _git(root, "fetch", "--no-tags", remote, reference)
        tip = _git(root, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        if tip != advertised:
            raise WorkflowError("remote default branch changed during verification; retry against its current tip")
        verified_ref = f"{remote}/{reference}"
    else:
        # A repository without a remote still has a default branch; a feature
        # branch's unpublished delivery trailer cannot stand in for that tip.
        branch = _git(root, "symbolic-ref", "--quiet", "HEAD")
        if branch not in ("refs/heads/main", "refs/heads/master"):
            raise WorkflowError("local-only delivery must be verified on main or master")
        tip = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
        verified_ref = branch
    actual = _git(root, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if actual != commit:
        raise WorkflowError("delivery requires the exact full commit ID")
    try:
        _git(root, "merge-base", "--is-ancestor", commit, tip)
    except WorkflowError:
        raise WorkflowError("delivery commit is not reachable from the verified default branch") from None
    message = _git(root, "show", "-s", "--format=%B", commit)
    trailers = _git(root, "interpret-trailers", "--parse", input=message)
    wanted = {f"sd:{row['id']}"}
    if trailer == "item":
        # `sd-ship` writes `Item: sd:<id>` and nothing else, so the original
        # source slug a `Delivers:` may name does not apply here.
        named = {line.partition(":")[2].strip() for line in trailers.splitlines()
                 if line.partition(":")[0].lower() == "item"}
        if not named.intersection(wanted):
            raise WorkflowError(f"commit carries no Item trailer for sd:{row['id']}")
        return {"commit": commit, "verified_ref": verified_ref, "verified_tip": tip,
                "shipped_at": stamp(_git(root, "show", "-s", "--format=%cI", commit)), "trailer": "Item"}
    external = row["external_id"] or ""
    if row["source"] == "docs/work" and "::docs/work/" in external:
        original = PurePosixPath(external.split("::", 1)[1])
        if original.name == "prd.md":
            wanted.add(original.parent.name)
    delivered = {line.partition(":")[2].strip() for line in trailers.splitlines()
                 if line.partition(":")[0].lower() == "delivers"}
    if not delivered.intersection(wanted):
        raise WorkflowError(f"commit carries no Delivers trailer for sd:{row['id']} or its original source slug")
    return {"commit": commit, "verified_ref": verified_ref, "verified_tip": tip,
            "shipped_at": stamp(_git(root, "show", "-s", "--format=%cI", commit))}


def deliver_work(
    connection: sqlite3.Connection, item: int, commit: str, *, who: str,
    expected_revision: str | None = None, verification_root: Path | str | None = None,
    waived: list[dict] | None = None,
) -> dict:
    """Verify a delivered commit, then atomically record completion evidence.

    `waived` names each criterion the delivery's claim waived, with the
    decision note that waived it (sd:3704): the completion record keeps it,
    and the `status_change` reason names it, in the one transaction.
    """
    who = _text(who, "who")
    waived = _waived(waived)
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
        raise WorkflowError("delivery requires a full lowercase commit ID")
    # Network latency does not hold SQLite's writer lock. The revision read
    # here is rechecked after verification even if the CLI supplied no token.
    initial = _work(connection, item, expected_revision)
    previous = completion_record(initial["item"]) or {}
    if initial["item"]["status"] == "done":
        if isinstance(previous, dict) and previous.get("outcome") == "delivered":
            return initial
        raise WorkflowError("cancelled or previously completed work cannot be reclassified as delivered")
    evidence = _verified(connection, initial["item"], commit, verification_root)
    return _complete_delivery(connection, item, initial["revision"], evidence, who=who, waived=waived)


def _waived(waived: list[dict] | None) -> list[dict]:
    """`waived` checked: a list of `{criterion: text, decision: note id}`."""
    if waived is None:
        return []
    if not isinstance(waived, list) or any(
            not isinstance(row, dict) or set(row) != {"criterion", "decision"}
            or not isinstance(row["criterion"], str) or not row["criterion"].strip()
            or type(row["decision"]) is not int for row in waived):
        raise WorkflowError("waived must list {criterion, decision} with a decision note id")
    return [dict(row) for row in waived]


def waived_suffix(waived: list[dict]) -> str:
    """What a delivery's `status_change` reason adds for its waived criteria, or ""."""
    if not waived:
        return ""
    decisions = ", ".join(f"decision #{number}" for number in dict.fromkeys(row["decision"] for row in waived))
    return f"; waived: {len(waived)} criteria ({decisions})"


def _verified(connection: sqlite3.Connection, row: dict, commit: str,
              verification_root: Path | str | None, **trailer: str) -> dict:
    """`_delivery_evidence` from the row's checkout, or from a clone held to its registered remote."""
    if verification_root is None:
        return _delivery_evidence(row, commit, **trailer)
    registered = connection.execute("SELECT remote FROM repo WHERE path = ?", (row["repo"],)).fetchone()
    return _delivery_evidence(row, commit, verification_root=Path(verification_root).resolve(),
                              registered_remote=registered["remote"] if registered else None, **trailer)


def deliver_associated_work(
    connection: sqlite3.Connection, item: int, commit: str, *, who: str, reason: str,
    expected_revision: str | None = None, verification_root: Path | str | None = None,
) -> dict:
    """Deliver work whose whole-item merge carried `Item:` where `Delivers:` was meant.

    The after-the-fact path, taken only by name and with a reason: a merge
    prepared without `--deliver` lands `Item: sd:<id>`, which `deliver_work`
    refuses, and no later commit of the same item can be made to carry the
    trailer. It checks what `Delivers:` would have shown: the commit is on
    the verified default branch, it names this item in its trailer block,
    and the row is not completed. The receipt records `trailer: Item` and
    the reason, so it never reads as an ordinary delivery. `deliver_work`
    is unchanged and still refuses the same commit.
    """
    who = _text(who, "who")
    if not isinstance(reason, str) or not reason.strip():
        raise WorkflowError("after-the-fact delivery requires a nonempty reason")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
        raise WorkflowError("delivery requires a full lowercase commit ID")
    initial = _work(connection, item, expected_revision)
    previous = completion_record(initial["item"]) or {}
    if initial["item"]["status"] == "done":
        if previous.get("outcome") == "delivered":
            return initial
        raise WorkflowError("cancelled or previously completed work cannot be reclassified as delivered")
    evidence = _verified(connection, initial["item"], commit, verification_root, trailer="item")
    return _complete_delivery(connection, item, initial["revision"], {**evidence, "after_the_fact": reason.strip()}, who=who)


def _complete_delivery(connection: sqlite3.Connection, item: int, revision: str,
                       evidence: dict, *, who: str, waived: list[dict] | None = None) -> dict:
    """One completion primitive, after live verification or a validated runner proof."""
    with transaction(connection):
        state = _work(connection, item, revision)
        fields = _fields(state["item"]["fields"])
        fields["completion"] = {"outcome": "delivered", "item": item, "repo": state["item"]["repo"],
                                "at": current_time(), "who": who, **evidence}
        if waived:
            fields["completion"]["waived"] = waived
        # sd:9869: a delivery answers none of a person's followups, so Today keeps each open one,
        # read from `fields` as `workflow.change_status` writes it for a task. The machine's own
        # (a build's block, a lane failure) the delivery settles; a post-merge one waits on a pass (sd:10407).
        open_followups = [note for note in state["notes"] if note["kind"] == "followup" and not note["resolved_at"]]
        for note in open_followups:
            if done_resolves(note):
                resolve_note(connection, note["id"])
        kept = [note["id"] for note in open_followups if not done_resolves(note)]
        if kept:
            fields[KEPT_FOLLOWUPS] = kept
        set_item_fields(connection, item, fields=fields, shipped_at=evidence["shipped_at"])
        _transition(connection, item, "done", who=who,
                    reason=f"delivered at {evidence['commit']} on {evidence['verified_ref']}{waived_suffix(waived or [])}")
        return item_state(connection, item)


def tracker_freshness(
    connection: sqlite3.Connection, tracker: str = "github", *,
    now: datetime | None = None, max_age_seconds: int = 86400,
) -> dict:
    """Describe collector health; old unchanged shadow rows can still be current."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or type(max_age_seconds) is not int or max_age_seconds < 0:
        raise WorkflowError("freshness needs an aware time and nonnegative maximum age")
    success = read_watermark(connection, tracker)
    # A custom `JIRA_JQL` run succeeds without moving the cursor (pack sd:5376),
    # so its heartbeat counts too. CASE, because `json_type` raises on malformed JSON.
    unwindowed = connection.execute(
        "SELECT timestamp FROM state WHERE kind = 'heartbeat' AND key = ? AND CASE WHEN json_valid(body) "
        "THEN json_type(body, '$.ok') = 'true' AND json_type(body, '$.windowed') = 'false' ELSE 0 END "
        "ORDER BY timestamp DESC, id DESC LIMIT 1", (f"tracker-sync:{tracker}",),
    ).fetchone()
    ran = _moment(unwindowed["timestamp"]) if unwindowed else None
    parsed = _moment(success)
    if ran is not None and (parsed is None or ran > parsed):
        success, parsed = unwindowed["timestamp"], ran
    age = max(0, int((moment - parsed).total_seconds())) if parsed else None
    state = "never" if parsed is None else "fresh" if age <= max_age_seconds else "stale"
    latest = connection.execute(
        "SELECT timestamp, body FROM state WHERE kind = 'heartbeat' AND key = ? "
        "ORDER BY timestamp DESC, id DESC LIMIT 1", (f"tracker-sync:{tracker}",),
    ).fetchone()
    reason = ""
    attempted = None
    if latest:
        attempted = latest["timestamp"]
        attempt_time = _moment(attempted)
        try:
            body = json.loads(latest["body"] or "{}")
        except (TypeError, ValueError):
            body = {}
        if isinstance(body, dict) and body.get("ok") is False and (
            parsed is None or attempt_time is not None and attempt_time >= parsed
        ):
            state = "degraded"
            reason = body.get("reason") or "Latest tracker refresh failed"
    return {"tracker": tracker, "state": state, "last_success_at": success,
            "last_attempt_at": attempted, "age_seconds": age,
            "max_age_seconds": max_age_seconds, "reason": reason}


def tracker_items(
    connection: sqlite3.Connection, *, tracker: str = "github",
    repo: str | None = None, state: str | None = "open",
) -> list[dict]:
    """Cached external context, never a source of local task status.

    Complete contribution observations retain match reasons and attention.
    Older shadow-only entries still have no inferred match reason.
    """
    predicates = ["tracker = ?"]
    values = [tracker]
    if repo is not None:
        predicates.append("repo = ?")
        values.append(repo)
    if state is not None:
        predicates.append("LOWER(state) = LOWER(?)")
        values.append(state)
    rows = connection.execute(
        f"SELECT * FROM shadow WHERE {' AND '.join(predicates)} ORDER BY last_seen DESC, id ASC", values,
    )
    from .contributions import ISSUE, projection, snapshot

    attention = {value["url"]: value for value in projection(connection, repo=repo) if value["url"]}
    result = []
    for row in rows:
        value = {**dict(row), "why": []}
        if tracker == "github" and row["url"] in attention:
            contribution = attention[row["url"]]
            # An issue's observation lives under `issue:`; `_key` refuses it as `github:`.
            prefix = "issue:" if ISSUE.fullmatch(row["url"]) else "github:"
            observed = snapshot(connection, prefix + row["url"])["observation"] or {}
            value.update(why=observed.get("why", []), needs_you=contribution["needs_you"],
                         attention=contribution["reasons"], contribution_lane=contribution["lane"])
        result.append(value)
    return result
