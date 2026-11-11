"""Durable contribution work and attention, shared by every reader.

GitHub observations are context, never authority over local task status.
Network calls and notification delivery belong outside these transactions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import stat
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import build, paths, shadow_sync, teammate
from .database import transaction
from .errors import SdDbError
from .runner import OWNING
from .workflow import (
    StaleItem,
    WorkflowError,
    _checked_state,
    _text,
    allowed_statuses,
    capture_task,
    change_status,
    item_state,
)
from .writes import (
    TransitionRefused,
    add_note,
    now,
    record_state,
    resolve_note,
    set_item_fields,
    stamp,
)

FIELDS = frozenset({"local_clone", "local_branch", "tested_commit", "pull_url", "evidence",
                    "blocked_on", "depends_on", "blocking_labels",
                    "issue_url", "target_repo", "draft_title", "draft_path"})
# `closed` is the terminal lane for an issue and for a pull request closed
# without merge -- neither awaits anyone; `merged` stays pull-request vocabulary.
LANES = {"newly_unblocked": 0, "awaiting_you": 1, "awaiting_them": 2, "merged": 3, "closed": 4}
#: An observation older than this reads stale: the projection says so, and
#: the refresh reads a stale open pull again first (sd:7864).
STALE_SECONDS = 86400
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
PULL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?\Z")
# Its own pattern: PULL also validates merge and release dependency proof,
# so widening it would let an issue URL stand in for a merged pull request.
ISSUE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?\Z")
REPO = re.compile(r"[\w.-]+/[\w.-]+")
# The observed sources a row can carry beside `item:`; each key names a URL.
OBSERVED = {"github:": "pull_url", "issue:": "issue_url"}
# One event vocabulary for both sources. `closed_completed` and
# `closed_not_planned` carry the close reason GitHub reports as a field; a
# pull request has no reason and no issue is ever converted to draft.
# `review_requested` (with the asked user's `reviewer_id`) and `pushed` (a force push) date a pull request's last
# turn (sd:8116); neither is ever attention.
EVENTS = frozenset({"comment", "label_added", "label_removed", "converted_to_draft", "ready_for_review",
                    "closed", "closed_completed", "closed_not_planned", "reopened", "review_requested", "pushed"})
ISSUE_EVENTS = EVENTS - {"converted_to_draft", "ready_for_review", "review_requested", "pushed"}
# An issue observation carries none of these; one that does was read as a pull.
#: The reason an item's open draft pull request reads `awaiting_you` (pack sd:7868).
DRAFTED = "Draft pull request: mark it ready for review"
PULL_ONLY = frozenset({"reviews", "ci", "ci_head", "ci_ids", "head", "base", "mergeable", "draft"})
#: Who writes the attention followups and the lane notes.
OBSERVER = "contribution-observer"
#: How `configure`'s note names the pull request a change linked (sd:7939).
LINKED = "; linked "
#: A lane change of an item's pull request, kept as a `comment` note (sd:7859): checkpoints get pruned, notes stay.
#: `PR lane: <lane> · <state> · <url>`, and ` (backfilled)` on one `_history` wrote at the time it happened.
LANE_NOTE = "PR lane: "
LANE_BODY = re.compile(r"PR lane: (\w+) · (\w+) · (\S+?)( \(backfilled\))?")
#: The lanes whose time is the operator's (fixes and answers) and the reviewers'; `merged` and `closed` end a wait.
ON_YOU = frozenset({"awaiting_you", "newly_unblocked"})
ON_REVIEWERS = frozenset({"awaiting_them"})
#: The item kinds a contribution may hang on: those `sd-ship --deliver` closes (its `DELIVERING_KINDS`), since a
#: lane-opened pull request delivers a work, task or followup item and each one waits on it (sd:7859).
KINDS = ("task", "work", "followup")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _url(value) -> str:
    if not isinstance(value, str) or not PULL.fullmatch(value):
        raise WorkflowError("pull_url must be a canonical GitHub pull-request URL")
    match = PULL.fullmatch(value)
    return f"https://github.com/{match[1].lower()}/{match[2].lower()}/pull/{match[3]}"


def _issue_url(value) -> str:
    if not isinstance(value, str) or not ISSUE.fullmatch(value):
        raise WorkflowError("issue_url must be a canonical GitHub issue URL")
    match = ISSUE.fullmatch(value)
    return f"https://github.com/{match[1].lower()}/{match[2].lower()}/issues/{match[3]}"


def _key(key: str) -> str:
    if isinstance(key, str) and key.startswith("github:"):
        return "github:" + _url(key[len("github:"):])
    if isinstance(key, str) and key.startswith("issue:"):
        return "issue:" + _issue_url(key[len("issue:"):])
    # sd:3500: an item reads as every `item` positional reads it, 234, sd:234 or item:234.
    found = re.fullmatch(r"(?:item:|sd:)?([1-9][0-9]*)", key) if isinstance(key, str) else None
    if found:
        return f"item:{found[1]}"
    raise WorkflowError("contribution key must be an item id (234, sd:234 or item:234), "
                        "github:PR-URL or issue:ISSUE-URL")


def _observed_url(key: str) -> str | None:
    for prefix in OBSERVED:
        if key.startswith(prefix):
            return key[len(prefix):]
    return None


def _moment(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (AttributeError, TypeError, ValueError):
        raise WorkflowError("observation time must be an ISO timestamp with timezone") from None


def _strings(value, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v or "\0" in v for v in value):
        raise WorkflowError(f"{name} must be a list of nonempty strings")
    return value


def _sha(value, name: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise WorkflowError(f"{name} must be a full commit hash")
    return value


def _metadata(row: dict) -> dict | None:
    try:
        fields = json.loads(row.get("fields") or "{}")
        value = fields.get("contribution")
        if value is not None and not isinstance(value, dict):
            raise ValueError()
        return value
    except (TypeError, ValueError, AttributeError, RecursionError):
        raise WorkflowError(f"item {row.get('id')} has malformed contribution metadata") from None


def _registered(connection, errors: list[str] | None = None) -> list[tuple[dict, dict]]:
    """Each item of `KINDS` with contribution metadata. With `errors`, a malformed one is said there and skipped."""
    result = []
    marks = ",".join("?" * len(KINDS))
    for raw in connection.execute(f"SELECT * FROM item WHERE kind IN ({marks}) ORDER BY id", KINDS):
        row = dict(raw)
        try:
            metadata = _metadata(row)
        except WorkflowError as error:
            if errors is None:
                raise
            errors.append(f"item {row['id']}: {error}")
            continue
        if metadata is not None:
            result.append((row, metadata))
    return result


def _local_proof(value: dict) -> bool:
    """Check supplied evidence bytes; never run the recorded command."""
    local = value.get("local_clone")
    branch = value.get("local_branch")
    tested = value.get("tested_commit")
    if not local and not branch and not tested and not value.get("evidence"):
        return False
    if not isinstance(local, str) or not Path(local).is_absolute() or not Path(local).is_dir():
        raise WorkflowError("local_clone must name an existing absolute directory")
    branch = _text(branch, "local_branch")
    if branch.startswith("-") or any(c in branch for c in "\n\r\0"):
        raise WorkflowError("local_branch must name a local branch")
    try:
        result = subprocess.run(["git", "-C", local, "rev-parse", "--verify", "--end-of-options",
                                 f"refs/heads/{branch}^{{commit}}"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise WorkflowError("local branch could not be inspected") from None
    if result.returncode or not SHA.fullmatch(result.stdout.strip()):
        raise WorkflowError("local_branch does not resolve to a local commit")
    if tested is not None and _sha(tested, "tested_commit") != result.stdout.strip():
        raise WorkflowError("tested_commit differs from the local branch tip")
    evidence = value.get("evidence", [])
    if not isinstance(evidence, list):
        raise WorkflowError("evidence must be a list")
    verified = bool(tested and evidence)
    for record in evidence:
        if not isinstance(record, dict) or set(record) - {"argv", "cwd", "exit_code", "commit", "artifact", "sha256"}:
            raise WorkflowError("evidence has unsupported fields")
        artifact = record.get("artifact")
        digest = record.get("sha256")
        if not isinstance(artifact, str) or not Path(artifact).is_absolute():
            raise WorkflowError("evidence artifact must be an absolute file path")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise WorkflowError("evidence needs a SHA256 digest")
        try:
            if not stat.S_ISREG(Path(artifact).stat().st_mode):
                raise WorkflowError("evidence artifact must be a regular file")
            with Path(artifact).open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
        except (OSError, ValueError):
            # ValueError: an embedded NUL, which `Path.stat` refuses before any I/O.
            raise WorkflowError("evidence artifact could not be read") from None
        if actual != digest:
            raise WorkflowError("evidence artifact hash differs from the recorded proof")
        if "argv" in record:
            _strings(record["argv"], "evidence argv")
        if "cwd" in record and (not isinstance(record["cwd"], str) or not Path(record["cwd"]).is_absolute()):
            raise WorkflowError("evidence cwd must be an absolute path")
        if record.get("exit_code") is not None and type(record["exit_code"]) is not int:
            raise WorkflowError("evidence exit_code must be an integer or null")
        if record.get("commit") is not None:
            _sha(record["commit"], "evidence commit")
        verified = verified and bool(record.get("argv")) and record.get("cwd") == local \
            and record.get("commit") == tested and record.get("exit_code") == 0
    return bool(verified)


def _draft_path(value) -> None:
    """Check the draft body's digest the way evidence artifacts are checked."""
    if not isinstance(value, dict) or set(value) != {"path", "sha256"} \
            or not isinstance(value["path"], str) or not Path(value["path"]).is_absolute() \
            or not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise WorkflowError("draft_path must be an absolute file path with a SHA256 digest")
    try:
        if not stat.S_ISREG(Path(value["path"]).stat().st_mode):
            raise WorkflowError("issue draft could not be read")
        with Path(value["path"]).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    except (OSError, ValueError):
        # ValueError: an embedded NUL, which `Path.stat` refuses before any I/O.
        raise WorkflowError("issue draft could not be read") from None
    if actual != value["sha256"]:
        raise WorkflowError("issue draft hash differs from the recorded digest")


def _identity(value: dict) -> None:
    """Exactly one of: pull request, filed issue, issue draft, unfiled branch.

    A field is present when it is not None, and a present field is checked
    by type: a truthiness test let `[]`, `{}` and `""` through unchecked.
    """
    def present(name):
        return value.get(name) is not None

    if present("pull_url"):
        value["pull_url"] = _url(value["pull_url"])
    if present("issue_url"):
        value["issue_url"] = _issue_url(value["issue_url"])
    if present("pull_url") and present("issue_url"):
        raise WorkflowError("a contribution is a pull request or an issue, not both")
    if present("target_repo") and (not isinstance(value["target_repo"], str) or not REPO.fullmatch(value["target_repo"])):
        raise WorkflowError("target_repo must be owner/repo")
    if present("draft_title"):
        _text(value["draft_title"], "draft_title")
    if present("draft_path"):
        _draft_path(value["draft_path"])
    if present("issue_url") and present("target_repo") \
            and value["target_repo"].lower() != "/".join(value["issue_url"].split("/")[3:5]):
        raise WorkflowError("target_repo does not match issue_url")
    draft = [present(name) for name in ("target_repo", "draft_title", "draft_path")]
    if present("pull_url") or present("issue_url"):
        # Filing preserves the draft fields; the URL is the identity now.
        return
    if all(draft):
        if value.get("local_clone") or value.get("local_branch"):
            raise WorkflowError("an issue draft has no clone or branch")
    elif any(draft):
        raise WorkflowError("an issue draft needs target_repo, draft_title and draft_path")
    elif not value.get("local_clone") or not value.get("local_branch"):
        raise WorkflowError("unfiled work needs local_clone and local_branch")


def _dependency(value) -> dict:
    if not isinstance(value, dict):
        raise WorkflowError("dependency must be an object")
    kind = value.get("kind")
    allowed = {"item": {"kind", "item"}, "merge": {"kind", "url"}, "issue": {"kind", "url"},
               "release": {"kind", "repo", "tag", "contains_pull", "package"}}
    if kind not in allowed or set(value) - allowed[kind]:
        raise WorkflowError("unsupported dependency kind or fields")
    if kind == "item":
        if type(value.get("item")) is not int or value["item"] < 1:
            raise WorkflowError("item dependency needs a positive item ID")
    elif kind == "merge":
        _url(value.get("url"))
    elif kind == "issue":
        _issue_url(value.get("url"))
    else:
        if not isinstance(value.get("repo"), str) or not REPO.fullmatch(value["repo"]):
            raise WorkflowError("release dependency needs owner/repo")
        _url(value.get("contains_pull"))
        if value.get("tag") is not None:
            _text(value["tag"], "release tag")
        package = value.get("package")
        if package is not None:
            if not isinstance(package, dict) or set(package) - {"name", "version"}:
                raise WorkflowError("release package needs name and optional exact version")
            _text(package.get("name"), "package name")
            if package.get("version") is not None:
                _text(package["version"], "package version")
    return value


def _configuration(connection, item: int, old: dict, changes: dict) -> dict:
    if not isinstance(changes, dict) or set(changes) - FIELDS:
        raise WorkflowError("unsupported contribution fields")
    value = {**old, **copy.deepcopy(changes)}
    _identity(value)
    if "blocked_on" in value:
        _text(value["blocked_on"], "blocked_on", blank=True)
    if "blocking_labels" in value:
        _strings(value["blocking_labels"], "blocking_labels")
    dependencies = value.get("depends_on", [])
    if not isinstance(dependencies, list):
        raise WorkflowError("depends_on must be a list")
    for dependency in dependencies:
        _dependency(dependency)
    if len({_digest(d) for d in dependencies}) != len(dependencies):
        raise WorkflowError("duplicate dependencies are not allowed")
    # Another item's malformed metadata holds no value to compare with, and failed every link (sd:7863): skip it.
    others = _registered(connection, [])
    graph = {row["id"]: metadata.get("depends_on", []) for row, metadata in others}
    graph[item] = dependencies

    def visit(node, ancestors):
        if node in ancestors:
            raise WorkflowError("contribution dependency cycle")
        for dependency in graph.get(node, []):
            if dependency["kind"] == "item":
                target = dependency["item"]
                item_state(connection, target)
                visit(target, ancestors | {node})

    visit(item, set())
    _local_proof(value)
    for row, metadata in others:
        if row["id"] == item:
            continue
        if value.get("pull_url") and metadata.get("pull_url") == value["pull_url"]:
            raise WorkflowError("this pull request already belongs to another contribution")
        if value.get("issue_url") and metadata.get("issue_url") == value["issue_url"]:
            raise WorkflowError("this issue already belongs to another contribution")
        if value.get("local_clone") and metadata.get("local_clone") \
                and value.get("local_branch") == metadata.get("local_branch") \
                and Path(value["local_clone"]).resolve() == Path(metadata["local_clone"]).resolve():
            raise WorkflowError("this local clone and branch already belong to another contribution")
        if value.get("target_repo") and value.get("draft_path") and metadata.get("target_repo") \
                and isinstance(metadata.get("draft_path"), dict) \
                and value["target_repo"].lower() == metadata["target_repo"].lower() \
                and Path(value["draft_path"]["path"]).resolve() == Path(metadata["draft_path"]["path"]).resolve():
            raise WorkflowError("this issue draft already belongs to another contribution")
    return value


def capture(connection, *, title, changes, who) -> dict:
    """Create an explicit local task; ordinary observed PRs never call this."""
    with transaction(connection):
        result = capture_task(connection, title=title, priority=3, who=who)  # a filer names a rank (sd:6726)
        return configure(connection, result["item"]["id"], changes, who=who,
                         expected_revision=result["revision"])


def configure(connection, item, changes, *, who, expected_revision=None) -> dict:
    who = _text(who, "who")
    with transaction(connection):
        current = _checked_state(connection, item, expected_revision)
        if current["item"]["kind"] not in KINDS:
            raise WorkflowError("contributions use local work, task or followup items")
        old = _metadata(current["item"]) or {}
        value = _configuration(connection, item, old, changes)
        if old != value:
            fields = json.loads(current["item"]["fields"] or "{}")
            fields["contribution"] = value
            set_item_fields(connection, item, fields=fields)
            # The item's url is the operator's word: a `fold` tombstone on it would hide it from the queue (sd:6091).
            for prefix, name in OBSERVED.items():
                state = snapshot(connection, prefix + value[name]) if value.get(name) else {}
                if state.pop("moved_to", None):
                    _save(connection, state)
            # The url a change links is named, so `link_shipped` reads when, and by whom, the link was set (sd:7939).
            linked = value.get("pull_url") if value.get("pull_url") != old.get("pull_url") else None
            add_note(connection, item, "comment", f"Updated contribution metadata by {who}"
                                                  f"{LINKED + linked if linked else ''}", session=who)
            if old.get("depends_on", []) != value.get("depends_on", []):
                key = f"item:{item}"
                state = snapshot(connection, key)
                state["observation"] = {"configured": value.get("depends_on", []), "satisfied": False}
                _publish_attention(connection, state, [], item)
                _save(connection, state)
            if old.get("blocking_labels", []) != value.get("blocking_labels", []):
                # Invalidate collectors that read the prior operator policy.
                for prefix, name in OBSERVED.items():
                    if value.get(name):
                        _save(connection, snapshot(connection, prefix + value[name]))
            _note_lane(connection, item)
        return item_state(connection, item)


def snapshot(connection, key, latest=None) -> dict:
    """`latest` is `_latest`'s checkpoints, read once for many keys; else this key is read."""
    key = _key(key)
    if latest is not None:
        row = latest.get("contribution:" + key)
    else:
        row = connection.execute("SELECT id,body FROM state WHERE kind='checkpoint' AND key=? ORDER BY id DESC LIMIT 1",
                                 ("contribution:" + key,)).fetchone()
    body = {"observation": None, "attention": [], "notifications": {}, "acknowledged": [], "ci_baseline": None}
    if row:
        try:
            stored = json.loads(row["body"])
            if not isinstance(stored, dict):
                raise ValueError()
            body.update(stored)
        except (TypeError, ValueError, RecursionError):
            raise WorkflowError("contribution checkpoint is malformed") from None
    return {**body, "key": key, "revision": _digest({"key": key, "id": row["id"] if row else None, "body": body})}


def _save(connection, state: dict) -> dict:
    body = {k: v for k, v in state.items() if k not in {"key", "revision"}}
    record_state(connection, "checkpoint", key="contribution:" + state["key"], body=body)
    return snapshot(connection, state["key"])


def _checked(connection, key, revision) -> dict:
    state = snapshot(connection, key)
    if not isinstance(revision, str) or revision != state["revision"]:
        raise StaleItem("contribution changed; reload before applying this observation or acknowledgement")
    return state


def _hold(connection, state, reason) -> dict:
    record_state(connection, "heartbeat", key="contribution-observe:" + state["key"],
                 body={"ok": False, "reason": str(reason)})
    return state


def _fresh(state, observed_at) -> None:
    moment = _moment(observed_at)
    previous = (state["observation"] or {}).get("observed_at")
    if previous and moment < _moment(previous):
        raise WorkflowError("observation is older than the last complete observation")


def _event(identifier, reason, *, kind="activity", url=None) -> dict:
    return {"id": str(identifier), "reason": reason, "kind": kind, "url": url}


def _context_ids(observation: dict, previous: dict) -> dict[str, str]:
    prior = previous.get("observation") or {}
    identifiers = {}
    for why in observation.get("why", []) if observation["state"] == "open" else []:
        if why not in {"assigned", "review-requested"}:
            continue
        identifier = "context:" + why
        if prior.get("state") == "open" and why in prior.get("why", []):
            identifier = previous.get("context_events", {}).get(why, identifier)
        elif identifier in previous["notifications"]:
            # A confirmed disappearance ends the episode, not its delivery history.
            identifier += ":" + _digest({"revision": previous["revision"], "why": why})
        identifiers[why] = identifier
    return identifiers


def _pull_attention(observation: dict, previous: dict, contexts: dict[str, str]) -> tuple[list[dict], str | None]:
    attention = []
    operator = str(observation["operator"]["id"])
    current_state = observation["state"]
    context = [_event(identifier, why.replace("-", " ").capitalize()) for why, identifier in contexts.items()]
    if str(observation["author"]["id"]) != operator:
        return context, previous.get("ci_baseline")
    # The author asking for review again answers what came before (sd:8116): a reviewer's changes request
    # before the author asked that reviewer again, and any comment before the author's newest request.
    asked = [(_moment(event["at"]), str(event.get("reviewer_id"))) for event in observation["events"]
             if event["kind"] == "review_requested" and str(event["actor_id"]) == operator]
    answered = max((at for at, _ in asked), default=None)
    latest = {}
    for position, review in enumerate(observation["reviews"]):
        actor = str(review["actor_id"])
        if actor == operator or review["state"] not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        if any(at > _moment(review["at"]) and reviewer == actor for at, reviewer in asked):
            continue
        order = (_moment(review["at"]), position)
        if actor not in latest or order > latest[actor][0]:
            latest[actor] = (order, review)
    if current_state == "open":
        for _, review in latest.values():
            if review["state"] == "CHANGES_REQUESTED":
                attention.append(_event("review:" + str(review["id"]), "Changes requested"))
    lifecycle = {}
    for position, event in enumerate(observation["events"]):
        kind = event["kind"]
        family = "label:" + str(event.get("label")) if kind in {"label_added", "label_removed"} else \
            "draft" if kind in {"converted_to_draft", "ready_for_review"} else \
            "closed" if kind in {"closed", "reopened"} else None
        if family:
            order = (_moment(event["at"]), position)
            if family not in lifecycle or order > lifecycle[family][0]:
                lifecycle[family] = (order, event)
    events = [event for event in observation["events"] if event["kind"] == "comment"]
    events.extend(value[1] for value in lifecycle.values())
    for event in events:
        if str(event["actor_id"]) == operator:
            continue
        kind = event["kind"]
        if kind == "comment" and answered and _moment(event["at"]) < answered:
            continue
        reason = None
        if kind == "comment" and (event.get("maintainer") is True or event.get("mentions_operator") is True):
            reason = "Maintainer comment" if event.get("maintainer") else "Mentioned you"
        elif kind == "label_added" and event.get("label") in observation.get("blocking_labels", []) \
                and event.get("label") in observation.get("labels", []):
            reason = "Blocking label: " + event["label"]
        elif kind == "converted_to_draft" and observation["draft"] is True:
            reason = "Maintainer converted pull request to draft"
        elif kind == "closed" and current_state == "closed":
            reason = "Closed without merge"
        if reason and current_state != "merged":
            attention.append(_event("event:" + str(event["id"]), reason, url=event.get("url")))
    baseline = previous.get("ci_baseline")
    if current_state == "open":
        ci = observation["ci"]
        prior_ci = [v for v in previous["attention"] if v["kind"] == "ci"]
        if ci == "failure" and baseline == "success":
            identity = _digest({"head": observation["head"], "checks": sorted(observation["ci_ids"])})
            attention.append(_event("ci:" + identity, "CI changed from passing to failing", kind="ci"))
        elif ci in {"failure", "pending", "unknown"}:
            attention.extend(prior_ci)
        if observation["mergeable"] == "conflicting":
            if previous.get("mergeability_baseline") == "conflicting":
                attention.extend(v for v in previous["attention"] if v["kind"] == "conflict")
            else:
                identity = _digest({"head": observation["head"], "base": observation["base"]})
                attention.append(_event("conflict:" + identity, "Pull request has merge conflicts", kind="conflict"))
        elif observation["mergeable"] == "unknown":
            attention.extend(v for v in previous["attention"] if v["kind"] == "conflict")
        attention.extend(context)
        if ci in {"success", "failure"}:
            baseline = ci
    return attention, baseline


def _publish_attention(connection, state, attention, item=None) -> None:
    unique = {event["id"]: event for event in attention}
    active = [event for identifier, event in unique.items() if identifier not in state["acknowledged"]]
    for old in state["attention"]:
        if old["id"] not in unique:
            notice = state["notifications"].get(old["id"], {})
            if notice.get("note_id"):
                resolve_note(connection, notice["note_id"])
    state["attention"] = active
    for event in active:
        if event["id"] in state["notifications"]:
            continue
        notice = {"status": "pending", "event": event, "created_at": now()}
        if item is not None:
            notice["note_id"] = add_note(connection, item, "followup", event["reason"] +
                                        ("\n" + event["url"] if event.get("url") else ""), session=OBSERVER)
        state["notifications"][event["id"]] = notice


def _undismiss(state, value) -> None:
    """A lifecycle change is news the dismissal never saw, so the row returns to its real lane."""
    if state.get("dismissed") and state["dismissed"].get("state") != value["state"]:
        del state["dismissed"]


def observe_pull(connection, url, observation, *, expected_revision) -> dict:
    """Record one complete pull-request read.

    The read that first records the pull merged writes `PR <url> merged at <sha>` on each open item linking it and
    changes no status (sd:8375): a merge is not proof the item is done, so the operator closes it. The one
    exception is `_close_merged`'s (sd:8455).
    """
    url = _url(url)
    with transaction(connection):
        state = _checked(connection, "github:" + url, expected_revision)
        if not isinstance(observation, dict) or observation.get("complete") is not True:
            reason = observation.get("reason") if isinstance(observation, dict) else None
            return _hold(connection, state, "Incomplete GitHub observation" + (f": {reason}" if isinstance(reason, str) and reason else ""))
        value = copy.deepcopy(observation)
        _fresh(state, value.get("observed_at"))
        for name in ("operator", "author"):
            if not isinstance(value.get(name), dict) or not value[name].get("id"):
                return _hold(connection, state, "GitHub actor identity is unknown")
        for name in ("head", "base"):
            _sha(value.get(name), name)
        if value.get("merge_commit") is not None:
            _sha(value["merge_commit"], "merge_commit")
        if value.get("state") not in {"open", "closed", "merged"} or type(value.get("draft")) is not bool:
            raise WorkflowError("invalid pull-request lifecycle observation")
        if value.get("mergeable") not in {"mergeable", "conflicting", "unknown"} \
                or value.get("ci") not in {"success", "failure", "pending", "unknown"}:
            raise WorkflowError("invalid mergeability or CI observation")
        _strings(value.get("ci_ids"), "ci_ids")
        if value.get("ci_head") != value["head"]:
            raise WorkflowError("CI observation does not describe the current pull-request head")
        if value["ci"] in {"success", "failure"} and not value["ci_ids"]:
            raise WorkflowError("conclusive CI needs check-run or status identities")
        for name in ("why", "blocking_labels", "labels", "requested_reviewers"):
            _strings(value.get(name, []), name)
        _text(value.get("title"), "title")
        if not isinstance(value.get("repo"), str) or value["repo"].lower() != "/".join(url.split("/")[3:5]):
            raise WorkflowError("observation repository differs from pull URL")
        value["repo"] = value["repo"].lower()
        for name in ("reviews", "events"):
            if not isinstance(value.get(name), list):
                raise WorkflowError(f"{name} must be a list")
            for event in value[name]:
                if not isinstance(event, dict) or not event.get("id") or not event.get("actor_id"):
                    return _hold(connection, state, "GitHub event identity is unknown")
                _moment(event.get("at"))
                if name == "reviews" and not isinstance(event.get("state"), str):
                    raise WorkflowError("review state is missing")
                if name == "events" and event.get("kind") not in EVENTS:
                    raise WorkflowError("unsupported contribution event kind")
        registered = next(((row, metadata) for row, metadata in _registered(connection, []) if metadata.get("pull_url") == url), None)
        item = registered[0]["id"] if registered else None
        if registered and value.get("blocking_labels", []) != registered[1].get("blocking_labels", []):
            raise StaleItem("blocking-label policy changed; recollect with the current configuration")
        if value["state"] == "merged" and (state["observation"] or {}).get("state") != "merged":
            merged = f"PR {url} merged" + (f" at {value['merge_commit']}" if value.get("merge_commit") else "")
            for row, metadata in _registered(connection, []):
                if metadata.get("pull_url") == url and row["status"] != "done":
                    _close_merged(connection, row, metadata, value, merged)
        contexts = _context_ids(value, state)
        attention, baseline = _pull_attention(value, state, contexts)
        state["context_events"] = contexts
        _undismiss(state, value)
        state["observation"] = value
        state["ci_baseline"] = baseline
        if value["mergeable"] != "unknown":
            state["mergeability_baseline"] = value["mergeable"]
        _publish_attention(connection, state, attention, item)
        result = _save(connection, state)
        record_state(connection, "heartbeat", key="contribution-observe:" + state["key"], body={"ok": True})
        if item is not None:
            _note_lane(connection, item)
        return result


#: The statuses a merge may close from; `blocked` and `ready_to_send` wait on something a merge does not give.
MERGE_CLOSES_FROM = ("planning", "ready", "in_progress")


def _close_merged(connection, row: dict, metadata: dict, value: dict, merged: str) -> None:
    """Close item `row` on the read that first records its pull merged, or note the merge (sd:8455, sd:8375).

    It closes only when the merge commit's trailer block says `Closes: sd:<item>`, the author's word that the
    item is done, as `sd-ship` reads it, and `_merge_close_refusal` finds nothing holding it; else it writes the
    sd:8375 note, with the refusal when the trailer asked for a close. Never retried: a later read sees the
    merge recorded and does nothing, so a reopen stands.
    """
    item = row["id"]
    if item not in (value.get("closes") or []):
        add_note(connection, item, "comment", merged, session=OBSERVER)
        return
    try:
        why = _merge_close_refusal(connection, row, metadata, value.get("opened_at"))
        if why is None:
            change_status(connection, item, "done", who=OBSERVER, reason=f"{merged}; its commit says Closes: sd:{item}")
            return
    except (WorkflowError, TransitionRefused) as error:
        why = str(error)
    add_note(connection, item, "comment", f"{merged}; not closed: {why}", session=OBSERVER)


def _merge_close_refusal(connection, row: dict, metadata: dict, opened_at) -> str | None:
    """Why a merge whose commit closes item `row` does not close it, or None when it may (sd:8455).

    Each check is a blocker an sd:8375 review round found: a status a merge does not end, an operator's
    `blocked_on`, an open followup (done would resolve it), a reopen since the pull opened, an assignment, run or
    lane entry holding the item, and a kind that closes only with delivery evidence.
    """
    item = row["id"]
    if row["status"] not in MERGE_CLOSES_FROM:
        return f"its status is {row['status']}"
    if (metadata.get("blocked_on") or "").strip():
        return f"it is blocked on: {metadata['blocked_on'].strip()}"
    followup = connection.execute("SELECT id FROM note WHERE item = ? AND kind = 'followup' AND resolved_at IS NULL "
                                  "ORDER BY id LIMIT 1", (item,)).fetchone()
    if followup is not None:
        return f"open followup #{followup['id']}"
    try:
        since = stamp(opened_at)
    except SdDbError:
        since = ""  # an unknown opening: any reopen counts
    if connection.execute("SELECT 1 FROM note WHERE item = ? AND kind = 'status_change' AND body LIKE 'done -> %' "
                          "AND timestamp >= ? LIMIT 1", (item, since)).fetchone() is not None:
        return "it was reopened since the pull request opened"
    marks = ",".join("?" * len(OWNING))
    held = connection.execute(f"SELECT id FROM assignment WHERE item = ? AND status IN ({marks}) LIMIT 1",
                              (item, *OWNING)).fetchone()
    if held is not None:
        return f"assignment {held['id']} holds it"
    run = teammate.holding(connection, item)
    if run is not None:
        return f"teammate run {run['id']} holds it"
    # The guards a teammate start checks: an unreleased build run, and a lane entry whose close `sd-ship` owns.
    retained = build._held(connection, item) or teammate.lane_holding(connection, item)
    if retained:
        return retained
    if row["kind"] == "work":
        return "a work item closes only with delivery evidence"
    if "done" not in allowed_statuses(connection, item):
        return f"a {row['kind']} item does not move to done through task controls"
    return None


def _issue_attention(observation: dict, previous: dict) -> list[dict]:
    """Issue events that need the operator; wider than the pull rule on comments.

    A watched issue has no review machinery to carry a maintainer's answer,
    so any comment by someone else is the signal. The label rule is the pull
    rule unchanged, and the close reason is the operator-facing string:
    "Closed without merge" is pull-request vocabulary and never appears here.
    """
    attention = []
    operator = str(observation["operator"]["id"])
    current_state = observation["state"]
    lifecycle = {}
    for position, event in enumerate(observation["events"]):
        kind = event["kind"]
        family = "label:" + str(event.get("label")) if kind in {"label_added", "label_removed"} else \
            "closed" if kind in {"closed", "closed_completed", "closed_not_planned", "reopened"} else None
        if family:
            order = (_moment(event["at"]), position)
            if family not in lifecycle or order > lifecycle[family][0]:
                lifecycle[family] = (order, event)
    events = [event for event in observation["events"] if event["kind"] == "comment"]
    events.extend(value[1] for value in lifecycle.values())
    for event in events:
        if str(event["actor_id"]) == operator:
            continue
        kind = event["kind"]
        reason = None
        if kind == "comment":
            reason = "Maintainer comment" if event.get("maintainer") is True \
                else "Mentioned you" if event.get("mentions_operator") is True else "New comment"
        elif kind == "label_added" and event.get("label") in observation.get("blocking_labels", []) \
                and event.get("label") in observation.get("labels", []):
            reason = "Blocking label: " + event["label"]
        elif kind == "closed_completed" and current_state == "closed":
            reason = "Closed as completed"
        elif kind == "closed_not_planned" and current_state == "closed":
            reason = "Closed as not planned"
        elif kind == "closed" and current_state == "closed":
            reason = "Closed"
        elif kind == "reopened" and current_state == "open":
            reason = "Reopened"
        if reason:
            attention.append(_event("event:" + str(event["id"]), reason, url=event.get("url")))
    return attention


def observe_issue(connection, url, observation, *, expected_revision) -> dict:
    """The issue counterpart of `observe_pull`: no CI, mergeability or reviews."""
    url = _issue_url(url)
    with transaction(connection):
        state = _checked(connection, "issue:" + url, expected_revision)
        if not isinstance(observation, dict) or observation.get("complete") is not True:
            reason = observation.get("reason") if isinstance(observation, dict) else None
            return _hold(connection, state, "Incomplete GitHub observation" + (f": {reason}" if isinstance(reason, str) and reason else ""))
        value = copy.deepcopy(observation)
        _fresh(state, value.get("observed_at"))
        for name in ("operator", "author"):
            if not isinstance(value.get(name), dict) or not value[name].get("id"):
                return _hold(connection, state, "GitHub actor identity is unknown")
        if PULL_ONLY & set(value):
            raise WorkflowError("issue observation carries pull-request fields")
        if value.get("state") not in {"open", "closed"} or value.get("state_reason") not in {None, "completed", "not_planned"}:
            raise WorkflowError("invalid issue lifecycle observation")
        for name in ("why", "blocking_labels", "labels", "requested_reviewers"):
            _strings(value.get(name, []), name)
        _text(value.get("title"), "title")
        if not isinstance(value.get("repo"), str) or value["repo"].lower() != "/".join(url.split("/")[3:5]):
            raise WorkflowError("observation repository differs from issue URL")
        value["repo"] = value["repo"].lower()
        if not isinstance(value.get("events"), list):
            raise WorkflowError("events must be a list")
        for event in value["events"]:
            if not isinstance(event, dict) or not event.get("id") or not event.get("actor_id"):
                return _hold(connection, state, "GitHub event identity is unknown")
            _moment(event.get("at"))
            if event.get("kind") not in ISSUE_EVENTS:
                raise WorkflowError("unsupported contribution event kind")
        registered = next(((row, metadata) for row, metadata in _registered(connection, []) if metadata.get("issue_url") == url), None)
        item = registered[0]["id"] if registered else None
        if registered and value.get("blocking_labels", []) != registered[1].get("blocking_labels", []):
            raise StaleItem("blocking-label policy changed; recollect with the current configuration")
        attention = _issue_attention(value, state)
        _undismiss(state, value)
        state["observation"] = value
        _publish_attention(connection, state, attention, item)
        result = _save(connection, state)
        record_state(connection, "heartbeat", key="contribution-observe:" + state["key"], body={"ok": True})
        if item is not None:
            _note_lane(connection, item)  # the issue is one of the lane's sources (sd:7863)
        return result


def _satisfied(dependency, proof) -> bool:
    if not isinstance(proof, dict):
        return False
    if dependency["kind"] == "merge":
        return proof.get("url") == dependency["url"] and proof.get("merged") is True \
            and isinstance(proof.get("merge_commit_sha"), str) and bool(SHA.fullmatch(proof["merge_commit_sha"]))
    if dependency["kind"] == "issue":
        # Closed as completed, or a referencing pull request merged; a
        # not-planned close resolves nothing.
        if proof.get("url") != dependency["url"]:
            return False
        if proof.get("closed") is True and proof.get("state_reason") == "completed":
            return True
        merged = proof.get("merged_pull")
        try:
            return isinstance(merged, str) and _url(merged) == merged
        except WorkflowError:
            return False
    if not dependency.get("tag"):
        return False
    if not all(proof.get(k) == dependency.get(k) for k in ("repo", "tag", "contains_pull")):
        return False
    if proof.get("merged") is not True or proof.get("published") is not True or proof.get("ancestor") is not True:
        return False
    if not all(isinstance(proof.get(k), str) and SHA.fullmatch(proof[k]) for k in ("tag_commit", "merge_commit_sha")):
        return False
    package = dependency.get("package")
    if package:
        actual = proof.get("package", {})
        if not package.get("version") or not isinstance(actual, dict) \
                or any(actual.get(k) != package[k] for k in ("name", "version")):
            return False
        files = actual.get("files", [])
        if not isinstance(files, list) or not any(isinstance(f, dict) and isinstance(f.get("filename"), str)
                and bool(f["filename"].strip()) and "\0" not in f["filename"]
                and f.get("yanked") is False and isinstance(f.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) for f in files):
            return False
    return True


def observe_dependencies(connection, item, observations, *, observed_at, expected_revision, complete=True, reason="") -> dict:
    with transaction(connection):
        state = _checked(connection, f"item:{item}", expected_revision)
        metadata = _metadata(item_state(connection, item)["item"])
        if metadata is None:
            raise WorkflowError("item is not a registered contribution")
        _fresh(state, observed_at)
        if complete is not True or not isinstance(observations, list):
            # `reason` is the collector's account of why -- it is what the
            # item's freshness shows, so a hold names the missing proof.
            return _hold(connection, state, "Incomplete dependency observation" + (f": {reason}" if reason else ""))
        dependencies = metadata.get("depends_on", [])
        supplied = {}
        for row in observations:
            if not isinstance(row, dict) or row.get("dependency") not in dependencies:
                raise WorkflowError("dependency observation does not match current configuration")
            identity = _digest(row["dependency"])
            if identity in supplied:
                raise WorkflowError("duplicate dependency observation")
            supplied[identity] = row
        verified = []
        for dependency in dependencies:
            if dependency["kind"] == "item":
                current = item_state(connection, dependency["item"])
                verified.append({"dependency": dependency, "state": "satisfied" if current["item"]["status"] == "done" else "pending",
                                 "proof": {"item": dependency["item"], "revision": current["revision"], "status": current["item"]["status"]}})
                continue
            row = supplied.get(_digest(dependency))
            if row is None or row.get("state") == "unknown":
                return _hold(connection, state, "Dependency state is unknown")
            if row.get("state") not in {"satisfied", "pending"}:
                raise WorkflowError("invalid dependency state")
            if row["state"] == "satisfied" and not _satisfied(dependency, row.get("proof")):
                return _hold(connection, state, "Dependency satisfaction lacks exact publication or merge proof")
            verified.append(row)
        for row in verified:
            if row["dependency"]["kind"] == "release" and row["state"] == "satisfied":
                merges = [m for m in verified if m["dependency"]["kind"] == "merge"
                          and m["dependency"]["url"] == row["dependency"]["contains_pull"]]
                if merges and any(m["state"] != "satisfied" or m["proof"]["merge_commit_sha"] != row["proof"]["merge_commit_sha"] for m in merges):
                    return _hold(connection, state, "Release proof does not contain the configured merged commit")
        satisfied = bool(dependencies) and all(row["state"] == "satisfied" for row in verified)
        previous = state["observation"] or {}
        attention = state["attention"] if satisfied else []
        if satisfied and previous.get("satisfied") is not True:
            identity = _digest({"configuration": dependencies, "proofs": verified})
            attention = [_event("unblocked:" + identity, "All contribution dependencies are satisfied", kind="newly_unblocked")]
        state["observation"] = {"observed_at": observed_at, "configured": dependencies, "dependencies": verified, "satisfied": satisfied}
        _publish_attention(connection, state, attention, item)
        result = _save(connection, state)
        record_state(connection, "heartbeat", key="contribution-observe:" + state["key"], body={"ok": True})
        _note_lane(connection, item)
        return result


def _latest(connection, kind, prefix) -> dict:
    """The newest state row of each key under `prefix`, in one statement (sd:2678).

    A projection read each row's checkpoint and heartbeat on its own, one
    round trip apiece over the wire. The range is the exact prefix, so the
    (kind, key, id) index serves it.
    """
    end = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    return {row["key"]: row for row in connection.execute(
        "SELECT key,id,body,timestamp FROM state WHERE id IN (SELECT max(id) FROM state WHERE kind=? AND key>=? AND key<? GROUP BY key)",
        (kind, prefix, end))}


def _freshness(heartbeats, sources) -> dict:
    times = [(source["observation"] or {}).get("observed_at") for source in sources]
    times = [value for value in times if value]
    result = {"status": "current" if times else "unknown", "reason": "" if times else "Not yet observed", "observed_at": max(times) if times else None}
    for source in sources:
        row = heartbeats.get("contribution-observe:" + source["key"])
        if row:
            body = json.loads(row["body"])
            if body.get("ok") is False:
                result.update(status="unknown", reason=body.get("reason", "Observation failed"))
    if times and (datetime.now(timezone.utc) - max(map(_moment, times))).total_seconds() > STALE_SECONDS:
        result.update(status="unknown", reason=result["reason"] or "Last complete observation is older than one day")
    return result


def _drafted(observation) -> bool:
    """Whether the observation is an open draft pull request the operator wrote (pack sd:7868)."""
    operator = str((observation.get("operator") or {}).get("id") or "")
    return observation.get("state") == "open" and observation.get("draft") is True and bool(operator) \
        and str((observation.get("author") or {}).get("id") or "") == operator


def _lane(sources, observation, url, waiting, dismissed, drafted=False) -> tuple[str, bool]:
    """The row's lane and whether it needs the operator: the one rule the projection and `record_lane` read.

    `drafted` is an item's open draft by the operator (`_drafted`): it waits on them, not on reviewers.
    """
    needs_you = any(s["attention"] for s in sources) or (not url and not waiting) or drafted
    # An issue or a pull request closed without merge awaits no one; a later
    # observation that reads `open` returns the row to live work. A dismissed
    # row (sd:3432) is closed too, until new attention or a lifecycle change.
    closed = observation.get("state") == "closed" or bool(dismissed)
    lane = "awaiting_you" if needs_you else "merged" if observation.get("state") == "merged" \
        else "closed" if closed else "awaiting_them"
    if any(e["kind"] == "newly_unblocked" for s in sources for e in s["attention"]):
        lane = "newly_unblocked"
    return lane, needs_you


def _sources(connection, item, metadata, latest=None) -> list[dict]:
    """The item's checkpoint, then one per url it names (`OBSERVED`): the one input set `_lane` reads, for the
    projection and `record_lane` alike (sd:7863). `latest` is `_latest`'s checkpoints, as `snapshot` takes them."""
    return [snapshot(connection, f"item:{item}", latest),
            *(snapshot(connection, prefix + metadata[name], latest) for prefix, name in OBSERVED.items() if metadata.get(name))]


def _shadow_times(connection, url) -> dict:
    """The shadow's `created_at`, `merged_at` and `closed_at` of pull request `url`; `{}` when none was read."""
    try:
        row = connection.execute(
            "SELECT created_at, merged_at, closed_at FROM shadow WHERE tracker = 'github' "
            "AND lower(rtrim(url, '/')) = ? ORDER BY last_seen DESC, id DESC LIMIT 1", (url,)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such column" not in str(error):
            raise
        return {}  # a store before migration 38 holds none of these
    return dict(row) if row else {}


def lane_notes(connection, item, url=None) -> list[dict]:
    """The item's PR lane notes (sd:7859), oldest first: `{id, at, lane, state, url, backfilled}`; one url's alone with `url`."""
    found = []
    for row in connection.execute("SELECT id, timestamp, body FROM note WHERE item = ? AND kind = 'comment' "
                                  "AND body LIKE ? ORDER BY timestamp, id", (item, LANE_NOTE + "%")):
        match = LANE_BODY.fullmatch(row["body"])
        if match and match[1] in LANES and (url is None or match[3] == url):
            found.append({"id": row["id"], "at": row["timestamp"], "lane": match[1], "state": match[2],
                          "url": match[3], "backfilled": bool(match[4])})
    return found


def _history(connection, item, url, state) -> tuple[list[tuple[str, str, str]], list[str]]:
    """What history still holds of the item's pull request before its first lane note: `(at, lane, state)`, oldest
    first, and each time it skipped because it names no instant (a time with no zone).

    The shadow's `created_at` opens it on reviewers; each followup the pull request's own attention raised (its
    checkpoint's `notifications`) is on you from its time, or the pull request's creation if that is later, to its
    `resolved_at`; the shadow's `merged_at`, else `closed_at`, ends it. A followup of another source or an older pull
    request is not this one's wait (sd:7908). A pull request observed in `state` merged or closed with no end time
    has no history: its open span would run to this sync, not to its end. A span the shadow never saw, or attention
    that was never a followup, is not recoverable.
    """
    times = _shadow_times(connection, url)
    events, skipped = [], []

    def instant(value):
        """`value` as an instant `add_note` takes, or None, said in `skipped`: a time with no zone is not one."""
        try:
            _moment(value)
            return stamp(value)
        except SdDbError:
            skipped.append(value)
            return None

    created = times.get("created_at") if times.get("created_at") and instant(times["created_at"]) else None
    if created:
        events.append((created, 0))
    raised = {notice.get("note_id") for notice in snapshot(connection, "github:" + url)["notifications"].values()
              if isinstance(notice, dict)}
    for note in connection.execute("SELECT id, timestamp, resolved_at FROM note WHERE item = ? AND kind = 'followup' "
                                   "AND session = ? ORDER BY id", (item, OBSERVER)):
        if note["id"] not in raised or not instant(note["timestamp"]):
            continue
        resolved = note["resolved_at"] if note["resolved_at"] and instant(note["resolved_at"]) else None
        start = note["timestamp"]
        if created and _moment(start) < _moment(created):
            if resolved and _moment(resolved) <= _moment(created):
                continue
            start = created
        events.append((start, 1))
        if resolved:
            events.append((resolved, -1))
    end = next((value for value in (times.get("merged_at"), times.get("closed_at")) if value and instant(value)), None)
    if state in ("merged", "closed") and not end:
        return [], skipped
    merged = bool(end) and end == times.get("merged_at")
    history: list[tuple[str, str, str]] = []
    open_attention = 0
    for at, step in sorted(events, key=lambda pair: (_moment(pair[0]), -pair[1])):
        if end and _moment(at) >= _moment(end):
            break
        open_attention = max(0, open_attention + step)
        lane = "awaiting_you" if open_attention else "awaiting_them"
        if not history or history[-1][1] != lane:
            history.append((at, lane, "open"))
    if end and history:
        history.append((end, "merged" if merged else "closed", "merged" if merged else "closed"))
    return history, skipped


def record_lane(connection, item) -> int | None:
    """Write the item's PR lane note when its lane or its pull request's state changed (sd:7859): the newest note's id
    it wrote, or None for none.

    The lane is the projection's (`_lane`), read from the same checkpoints; the note is `LANE_NOTE`, so a rerun that
    finds the same lane and state as the newest note writes nothing. An item with no `pull_url`, or one not yet
    observed, has no lane to record. The first note for an item and url is preceded by its `_history`, at the times it
    happened. All in one transaction: the caller's, when it holds one.
    """
    with transaction(connection):
        row = connection.execute("SELECT * FROM item WHERE id = ?", (item,)).fetchone()
        metadata = (_metadata(dict(row)) if row else None) or {}
        url = metadata.get("pull_url")
        if not url:
            return None
        sources = _sources(connection, item, metadata)
        observation = sources[1]["observation"] or {}  # the pull request's: `OBSERVED` names it first
        if observation.get("state") not in {"open", "closed", "merged"}:
            return None
        lane, _ = _lane(sources, observation, url, bool(metadata.get("blocked_on") or metadata.get("depends_on")), None,
                        _drafted(observation))
        notes, written = lane_notes(connection, item, url), None
        if not notes:
            history, skipped = _history(connection, item, url, observation["state"])
            for at, old, state in history:
                written = add_note(connection, item, "comment", f"{LANE_NOTE}{old} · {state} · {url} (backfilled)",
                                   session=OBSERVER, timestamp=at)
            if skipped:
                add_note(connection, item, "comment", f"PR lane backfill skipped {len(skipped)} time(s) with no zone: "
                         + ", ".join(repr(value) for value in skipped), session=OBSERVER)
            notes = lane_notes(connection, item, url)
        if notes and (notes[-1]["lane"], notes[-1]["state"]) == (lane, observation["state"]):
            return written
        return add_note(connection, item, "comment", f"{LANE_NOTE}{lane} · {observation['state']} · {url}",
                        session=OBSERVER)


#: The failures of one row's data that the PR-lane pass says and skips (sd:7859): a refusal, or a stored value of the
#: wrong shape. A store failure (`sqlite3.Error`) is none of these.
ROW_ERRORS = (SdDbError, ValueError, TypeError, AttributeError, KeyError)


def _note_lane(connection, item) -> None:
    """`record_lane` beside the write its note describes (sd:7859, review round 2): a side observation.

    A failure of the row's data (`ROW_ERRORS`) rolls back the note alone and leaves the write standing; the sync's lane pass,
    `record_lanes`, tries that item again and says why it could not. A store failure (`sqlite3.Error`) still undoes
    both, as it would the write alone.
    """
    try:
        record_lane(connection, item)
    except ROW_ERRORS:
        pass


def record_lanes(connection) -> tuple[list[int], list[str]]:
    """`record_lane` for every item with a `pull_url`, each in its own transaction: the refresh's pass (sd:7859).

    Returns the notes written and an error per item that failed; a failed item is said and skipped, so it stops no
    other item's note.
    """
    written: list[int] = []
    errors: list[str] = []
    for row, metadata in _registered(connection, errors):
        if metadata.get("pull_url"):
            try:
                note = record_lane(connection, row["id"])
            except ROW_ERRORS as error:
                errors.append(f"item {row['id']}: {error}")
                continue
            if note is not None:
                written.append(note)
    return written, errors


def _state(connection, url: str):
    """The observed state of the pull request at `url`, or None when none is observed."""
    return (snapshot(connection, "github:" + url)["observation"] or {}).get("state")


def link_shipped(connection) -> tuple[list[int], list[str]]:
    """Link the pull request `sd-ship prepare` opened to its item as the item's `pull_url` (sd:7859).

    The receipt holds the item and its `pull_request` url; the operator's own pull request is observed already, so
    the link makes its lane the item's. Each item reads its newest receipt alone, and items go in number order, so a
    bundle's pull request goes to its lowest item (sd:7863). A newest receipt that merged, an item that is done or
    names an issue, and a url another item holds (a bundle's second item) are left alone. An item that names a pull
    request keeps it unless that one is observed closed and the newest receipt names another: a receipt has no
    closed phase, so a re-prepare after a closed pull request is that newer receipt. The newest receipt replaces a
    closed link only when it postdates it, the link being an older receipt's, or names a pull request observed open:
    a link the operator set by hand after the newest receipt stays, an older receipt's url too (sd:7931, sd:7939). A rerun links nothing.
    Each link is `configure`'s one transaction, which notes the lane too. Returns the items linked and an error per
    receipt that could not be read or linked; a bad receipt is said and skipped, so it stops no other.
    """
    from . import ship

    errors: list[str] = []
    taken = {metadata["pull_url"] for _, metadata in _registered(connection, errors) if metadata.get("pull_url")}
    newest: dict[int, tuple[int, str, dict, str]] = {}
    shipped: dict[int, set[str]] = {}
    for key, row in _latest(connection, "checkpoint", "ship:").items():
        try:
            receipt = ship.read(connection, key)[1]
        except ROW_ERRORS as error:
            errors.append(f"ship receipt {key}: {error}")
            continue
        pull, item = receipt.get("pull_request"), receipt.get("item")
        if pull is not None and not isinstance(pull, dict):
            errors.append(f"ship receipt {key}: pull_request is not an object")
            continue
        if type(item) is int and isinstance((pull or {}).get("url"), str):
            shipped.setdefault(item, set()).add((pull or {})["url"])
        if type(item) is int and (item not in newest or row["id"] > newest[item][0]):
            newest[item] = (row["id"], key, receipt, row["timestamp"])
    linked: list[int] = []
    for item in sorted(newest):
        _, key, receipt, _ = newest[item]
        url = (receipt.get("pull_request") or {}).get("url")
        if not isinstance(url, str) or receipt.get("phase") == "merged":
            continue
        try:
            url = _url(url)
            row = connection.execute("SELECT * FROM item WHERE id = ?", (item,)).fetchone()
            if url in taken or row is None or row["kind"] not in KINDS or row["status"] == "done":
                continue
            metadata = _metadata(dict(row)) or {}
            if metadata.get("issue_url"):
                continue
            held = metadata.get("pull_url")
            if held and (_state(connection, held) != "closed" or not (
                    _state(connection, url) == "open" or _receipts_link(connection, item, held, newest[item][3])
                    and held in {_url(other) for other in shipped[item] if other != url})):
                continue
            configure(connection, item, {"pull_url": url}, who=OBSERVER)
        except ROW_ERRORS as error:
            errors.append(f"ship receipt {key}: {error}")
            continue
        taken.add(url)
        linked.append(item)
    return linked, errors


def _receipts_link(connection, item: int, held: str, receipt_at: str) -> bool:
    """Whether `held` is a receipt's link the newest receipt (written at `receipt_at`) may replace: one this observer
    set, or one set by hand before that receipt. A hand link set after it, or at the same second, stays (sd:7939).
    A link whose note names no url (set before sd:7939) reads as a receipt's."""
    for row in connection.execute("SELECT timestamp, session, body FROM note WHERE item = ? AND kind = 'comment' "
                                  "AND body LIKE 'Updated contribution metadata by %' ORDER BY id DESC", (item,)):
        if row["body"].endswith(LINKED + held):
            return row["session"] == OBSERVER or row["timestamp"] < receipt_at
    return True


def _seconds(start, end) -> int:
    return max(0, int((_moment(end) - _moment(start)).total_seconds()))


def pr_waits(connection, *, now=None, errors: list[str] | None = None) -> list[dict]:
    """Each item's pull-request wait, from its lane notes (sd:7859), newest-noted first.

    `on_you` and `on_reviewers` are seconds: the span after each note of an open pull request goes to its lane
    (`ON_YOU`, `ON_REVIEWERS`), and the last one runs to `now`; a span after the pull request closed or merged is no
    wait, whatever attention its lane still shows. `to_merge` is the first `open` note to the first `merged` one
    after it, or None: a pull request first seen merged has no time to merge (sd:7908). `open` is a pull request still
    open in a waiting lane, `on` who it waits on (`you`, `reviewers`) and `since` when that lane began. With
    `errors`, an item whose metadata is malformed is said there and skipped (sd:7863); without, it fails the read.
    `mine` is `operator_pull` of the pull request's observation: True, False, or None when not known (sd:8084).

    `stale` is a waiting pull request whose newest reading is over a day old (sd:8116): no current wait (`open` False,
    `on` None), and its last span ends at that reading. `draft` is the operator's open draft (`_drafted`).
    """
    end = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    found = []
    for row, metadata in _registered(connection, errors):
        url = metadata.get("pull_url")
        if not url:
            continue
        notes = lane_notes(connection, row["id"], url)
        if not notes:
            continue
        last = notes[-1]
        observation = snapshot(connection, "github:" + url)["observation"]
        current = last["state"] == "open" and (last["lane"] in ON_YOU or last["lane"] in ON_REVIEWERS)
        stale = current and _stale(observation or {}, end)
        cap = _reading_end(observation or {}, last["at"]) if stale else end
        spans = {"you": 0, "reviewers": 0}
        for note, after in zip(notes, [*notes[1:], None], strict=True):
            who = "you" if note["lane"] in ON_YOU else "reviewers" if note["lane"] in ON_REVIEWERS else None
            if who and note["state"] == "open":  # a closed or merged pull request waits on no one
                spans[who] += _seconds(note["at"], after["at"] if after else cap)
        opened = next((index for index, note in enumerate(notes) if note["state"] == "open"), len(notes))
        merged = next((note for note in notes[opened:] if note["lane"] == "merged" or note["state"] == "merged"), None)
        waiting = current and not stale
        found.append({"item": row["id"], "title": row["title"], "repo": row["repo"], "status": row["status"],
                      "url": url, "lane": last["lane"], "state": last["state"], "open": waiting,
                      "on": ("you" if last["lane"] in ON_YOU else "reviewers") if waiting else None,
                      "since": last["at"], "waited": _seconds(last["at"], end) if waiting else None,
                      "on_you": spans["you"], "on_reviewers": spans["reviewers"],
                      "to_merge": _seconds(notes[opened]["at"], merged["at"]) if merged else None,
                      "mine": operator_pull(observation), "approximate": False,
                      "draft": last["state"] == "open" and _drafted(observation or {}), "stale": stale})
    return sorted(found, key=lambda value: (value["since"], value["item"]), reverse=True)


def _stale(observation: dict, end: str) -> bool:
    """Whether the newest reading is older than a day at `end`; a time that does not parse is unknown, so stale."""
    observed = observation.get("observed_at")
    if not observed:
        return False
    try:
        return (_moment(end) - _moment(observed)).total_seconds() > STALE_SECONDS
    except (WorkflowError, TypeError, ValueError):
        return True


def _reading_end(observation: dict, floor: str) -> str:
    """When a stale reading's wait stops counting: the reading, or `floor` when that is later or does not parse."""
    try:
        return observation["observed_at"] if _moment(observation["observed_at"]) > _moment(floor) else floor
    except (KeyError, WorkflowError, TypeError, ValueError):
        return floor


def unlinked_pr_waits(connection, *, now=None) -> list[dict]:
    """Each open pull request no item links that names the operator (sd:8142), as `pr_waits` rows with `item` None.

    The contribution observations hold every pull `contribution_sync.refresh` admits (`OBSERVED_WHY`): the
    operator's, and another's the review-requested or mentioned sweep found, an item's or not. The operator's own
    waits on them when its lane is `ON_YOU`, else on reviewers. Another's waits on them when it asks their review
    by name now (`_asked_by_name`) or @mentions them (`why` holds `mentioned`) and they have not reviewed it; a
    team-only request, or one they already answered with a review, is no row. No lane note holds its history, so
    the wait is one span, `approximate`: `since` is the newest event its reading shows (sd:8116), else when GitHub
    opened it, as the shadow read it, else when it was observed. `mine` says whether Metrics counts it. A `stale`
    reading is no current wait, and its span ends at the reading.
    """
    end = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    linked = {metadata.get("pull_url") for _row, metadata in _registered(connection, [])}
    checkpoints = _latest(connection, "checkpoint", "contribution:")
    found = []
    for entry in connection.execute("SELECT DISTINCT key FROM state WHERE kind = 'checkpoint' AND key LIKE ?",
                                    ("contribution:github:%",)):
        key = entry["key"][len("contribution:"):]
        url = _observed_url(key)
        source = snapshot(connection, key, checkpoints)
        observation = source.get("observation") or {}
        if url in linked or source.get("moved_to") or source.get("dismissed") or observation.get("state") != "open":
            continue
        operator = str((observation.get("operator") or {}).get("id") or "")
        if operator and str((observation.get("author") or {}).get("id") or "") == operator:
            lane, _needs = _lane([source], observation, url, False, None, _drafted(observation))
            on = "you" if lane in ON_YOU else "reviewers"
        elif _asked_by_name(observation) or ("mentioned" in (observation.get("why") or [])
                                              and not _reviewed_by(observation, operator)):
            on = "you"
        else:
            continue
        times = [entry["at"] for name in ("reviews", "events") for entry in observation.get(name) or [] if entry.get("at")]
        since = max(times, key=_moment) if times else _shadow_times(connection, url).get("created_at") \
            or observation.get("observed_at") or end
        stale = _stale(observation, end)
        span = _seconds(since, _reading_end(observation, since) if stale else end)
        found.append({"item": None, "title": observation.get("title") or url, "repo": observation.get("repo") or "",
                      "status": None, "state": "open", "url": url,
                      "lane": "awaiting_you" if on == "you" else "awaiting_them", "open": not stale,
                      "on": None if stale else on, "since": stamp(since), "waited": None if stale else span,
                      "on_you": span if on == "you" else 0, "on_reviewers": span if on == "reviewers" else 0,
                      "to_merge": None, "mine": operator_pull(observation), "approximate": True,
                      "draft": _drafted(observation), "stale": stale})
    return sorted(found, key=lambda value: (value["since"], value["url"]), reverse=True)


def _reviewed_by(observation: dict, operator: str) -> bool:
    """Whether `operator` has reviewed the pull: that answers an @mention, which GitHub keeps for the pull's life."""
    return bool(operator) and any(str(review.get("actor_id")) == operator for review in observation.get("reviews") or [])


def _asked_by_name(observation: dict) -> bool:
    """Whether the operator's login is in the pull's `requested_reviewers` now (sd:8142).

    Not `operator_pull`: that counts a past review too, since GitHub drops a reviewer from the list once they
    review, and a reviewed pull waits on its author, not on the operator.
    """
    login = str((observation.get("operator") or {}).get("login") or "").lower()
    return bool(login) and login in {str(name).lower() for name in observation.get("requested_reviewers") or []}


def operator_pull(observation) -> bool | None:
    """Whether the operator wrote the pull request or was asked to review it by name (sd:8084), or None when its
    observation says neither: none, or one read before `requested_reviewers` was kept.

    GitHub drops a reviewer from `requested_reviewers` once they review, so a review by the operator counts too. A
    team request names no user, so it does not count.
    """
    observation = observation or {}
    operator = observation.get("operator") or {}
    if not operator.get("id") or not (observation.get("author") or {}).get("id"):
        return None
    if str(observation["author"]["id"]) == str(operator["id"]) \
            or any(str(review.get("actor_id")) == str(operator["id"]) for review in observation.get("reviews") or []):
        return True
    requested = observation.get("requested_reviewers")
    if requested is None:
        return None
    return str(operator.get("login") or "").lower() in {login.lower() for login in requested}


def _projection_row(heartbeats, row, metadata, sources) -> dict:
    source = next((s for s in sources if _observed_url(s["key"])), sources[0])
    observation = source["observation"] or {}
    # A task made for the URL later is the operator tracking it again, so only a row with no item reads it.
    dismissed = None if row.get("id") else source.get("dismissed")
    url = metadata.get("pull_url") or metadata.get("issue_url") or _observed_url(source["key"])
    active = [{"key": s["key"], "revision": s["revision"], "event_ids": [e["id"] for e in s["attention"]],
               "reasons": [e["reason"] for e in s["attention"]]} for s in sources if s["attention"]]
    active.sort(key=lambda a: (0 if any(e["kind"] == "newly_unblocked" for s in sources if s["key"] == a["key"] for e in s["attention"]) else 1, a["key"]))
    primary = active[0] if active else {"key": source["key"], "revision": source["revision"], "event_ids": [], "reasons": []}
    waiting = bool(metadata.get("blocked_on") or metadata.get("depends_on"))
    # An open draft the operator wrote waits on them to mark it ready, not on reviewers (pack sd:7868), linked to an
    # item or not (sd:8116).
    drafted = _drafted(observation)
    lane, needs_you = _lane(sources, observation, url, waiting, dismissed, drafted)
    try:
        verified = _local_proof(metadata)
    except WorkflowError:
        verified = False
    draft = metadata.get("draft_path")
    draft_verified = False
    if draft:
        try:
            _draft_path(draft)
            draft_verified = True
        except WorkflowError:
            pass
    reasons = list(dict.fromkeys(reason for a in active for reason in a["reasons"]))
    if drafted:
        reasons.append(DRAFTED)
    if not url and draft:
        reasons.append("Issue draft has not been filed" if draft_verified else "Issue draft has not been filed; draft body is unverified")
    elif not url:
        reasons.append("Local contribution has not been filed" if verified else "Local contribution has not been filed; test evidence is unverified")
    if waiting and not active:
        reasons.append(metadata.get("blocked_on") or "Waiting for contribution dependencies")
    freshness = _freshness(heartbeats, sources)
    target = metadata.get("target_repo")
    return {**primary, "item_id": row.get("id"), "url": url,
            "repo": observation.get("repo") or row.get("repo") or ("/".join(url.split("/")[3:5]) if url
                                                                  else target.lower() if target else metadata.get("local_clone")),
            "title": row.get("title") or observation.get("title", url or "Local contribution"),
            "local_status": row.get("status"), "external_state": observation.get("state"), "lane": lane,
            "needs_you": needs_you, "reasons": reasons, "attention_sources": active,
            "observed_at": freshness["observed_at"], "freshness": freshness, "blocked_on": metadata.get("blocked_on", ""),
            "depends_on": metadata.get("depends_on", []), "blocking_labels": metadata.get("blocking_labels", []),
            "local_clone": metadata.get("local_clone"), "local_branch": metadata.get("local_branch"),
            "tested_commit": metadata.get("tested_commit"), "evidence": metadata.get("evidence", []), "evidence_verified": verified,
            "issue_url": metadata.get("issue_url"), "target_repo": target, "draft_title": metadata.get("draft_title"),
            "draft_path": draft, "draft_verified": draft_verified, "dismissed": dismissed,
            "notification_state": {s["key"]: s["notifications"] for s in sources}}


def _shadow_facts(connection, urls) -> dict[str, dict]:
    """What the shadow collector read of each GitHub row in `urls`, by canonical url (sd:5303, sd:5315).

    `merged_at`, `closed_at`, `branch`, `linked_issues` (a list or None) and
    `title`. A store before 38 lacks these columns and so holds none of them:
    `{}`, a known answer and not a failed read. One statement reads only the
    rows `urls` name (sd:5339): the canonical form is the stored url lowered,
    less a trailing slash, so `json_each` matches it; one OR term per url
    would hit SQLite's expression depth (1000), as `jira_rows` did.
    """
    wanted = sorted({url for url in urls if url})
    if not wanted:
        return {}
    try:
        rows = connection.execute(
            "SELECT url, title, merged_at, closed_at, branch, linked_issues FROM shadow WHERE tracker = 'github' "
            "AND lower(rtrim(url, '/')) IN (SELECT value FROM json_each(?)) ORDER BY last_seen, id",
            (json.dumps(wanted),)).fetchall()
    except sqlite3.OperationalError as error:
        if "no such column" not in str(error):
            raise
        return {}
    found = {}
    for row in rows:
        try:
            linked = json.loads(row["linked_issues"]) if row["linked_issues"] is not None else None
        except (TypeError, ValueError):
            linked = None
        for canonical in (_url, _issue_url):
            try:
                found[canonical(row["url"])] = {"merged_at": row["merged_at"], "closed_at": row["closed_at"],
                                                "branch": row["branch"], "title": row["title"],
                                                "linked_issues": linked if isinstance(linked, list) else None}
                break
            except WorkflowError:
                continue
    return found


def _issue_key(url: str) -> str:
    """`owner/repo#N` of a GitHub issue url; the url itself when it is not one."""
    match = ISSUE.fullmatch(url)
    return f"{match[1]}/{match[2]}#{match[3]}" if match else url


def _with_shadow(rows: list[dict], connection) -> None:
    """Give each projected row the shadow's dates and branch, and `linked`: the issues it closes and the
    Jira rows its title or branches name."""
    facts = _shadow_facts(connection, [value["url"] for value in rows])
    named = []
    for value in rows:
        fact = facts.get(value["url"], {})
        value.update(merged_at=fact.get("merged_at"), closed_at=fact.get("closed_at"), branch=fact.get("branch"))
        value["linked"] = [{"url": url, "kind": "github", "key": _issue_key(url)} for url in fact.get("linked_issues") or []]
        named.append(shadow_sync.jira_keys(value["title"], fact.get("title"), value["branch"], value.get("local_branch")))
    # One statement for every row's keys, so the projection's statement count stays fixed (sd:2678).
    jira = shadow_sync.jira_rows(connection, [key for keys in named for key in keys])
    for value, keys in zip(rows, named, strict=True):
        value["linked"] += [{"url": jira[key]["url"], "kind": "jira", "key": key} for key in keys if key in jira]


def projection(connection, *, repo=None) -> list[dict]:
    """One ordered answer, including unfiled tasks and closed-unmerged PRs.

    Each row's `merged_at` and `closed_at` are when GitHub merged or closed
    it, as the shadow collector last read them, or None (sd:5303). `branch`
    is a pull request's head branch, or None; `linked` the issues it closes
    and the Jira rows its title or branches name, each `{url, kind, key}` with
    kind `github` or `jira`, `[]` for none (sd:5315).
    """
    if repo is not None and not isinstance(repo, (str, list, tuple)):
        raise WorkflowError("repo filter must be an identity or list of identities")
    filters = None if repo is None else {repo} if isinstance(repo, str) else set(_strings(list(repo), "repo filters"))
    if filters is not None:
        # A local path filter matches its `~/` key and its legacy absolute
        # form alike; an `owner/repo` identity probes as itself (sd:1439).
        filters = {form for value in filters for form in paths.keys(value)}
    connection.execute("SAVEPOINT contribution_projection")
    try:
        result = []
        registered_urls: set[str] = set()
        checkpoints = _latest(connection, "checkpoint", "contribution:")
        heartbeats = _latest(connection, "heartbeat", "contribution-observe:")
        for row, metadata in _registered(connection):
            registered_urls.update(metadata[name] for name in OBSERVED.values() if metadata.get(name))
            value = _projection_row(heartbeats, row, metadata, _sources(connection, row["id"], metadata, checkpoints))
            if filters is None or filters.intersection({value["repo"], row.get("repo"), metadata.get("local_clone")}):
                result.append(value)
        keys = connection.execute("SELECT DISTINCT key FROM state WHERE kind='checkpoint' AND ("
                                  + " OR ".join("key LIKE ?" for _ in OBSERVED) + ")",
                                  tuple(f"contribution:{prefix}%" for prefix in OBSERVED))
        for entry in keys:
            key = entry["key"][len("contribution:"):]
            if _observed_url(key) in registered_urls:
                continue
            source = snapshot(connection, key, checkpoints)
            if source.get("moved_to"):
                continue  # `fold` moved it; its new key is the row (sd:6091)
            observation = source.get("observation") or {}
            if key.startswith("github:") and operator_pull(observation) is False \
                    and "mentioned" not in (observation.get("why") or []):
                continue  # sd:8377: no item, authorship, review, named request or mention leaves no context
            value = _projection_row(heartbeats, {}, {}, [source])
            if filters is None or value["repo"] in filters:
                result.append(value)
        _with_shadow(result, connection)
        # A pull on an `automation/` head branch with no item is a job's: the job that opened it merges it, so no
        # person waits on it (sd:8379). The GitHub page still lists it from the shadow.
        result = [value for value in result
                  if value["item_id"] or not (value.get("branch") or "").startswith("automation/")]
        return sorted(result, key=lambda value: (LANES[value["lane"]], value["key"]))
    finally:
        connection.execute("RELEASE contribution_projection")


def claim_notification(connection, key, event_id, *, owner) -> dict | None:
    owner = _text(owner, "notification owner")
    with transaction(connection):
        state = snapshot(connection, key)
        notice = state["notifications"].get(event_id)
        if not notice or notice["status"] != "pending" or event_id not in {e["id"] for e in state["attention"]}:
            return None
        token = uuid.uuid4().hex
        notice.update(status="sending", owner=owner, token=token, claimed_at=now())
        _save(connection, state)
        return {"key": state["key"], "event_id": event_id, "token": token, "owner": owner,
                "title": "Contribution needs attention", "message": notice["event"]["reason"],
                "url": notice["event"].get("url") or _observed_url(state["key"])}


def finish_notification(connection, key, event_id, token, *, outcome) -> dict:
    if outcome not in {"sent", "not_started", "uncertain"}:
        raise WorkflowError("notification outcome must be sent, not_started, or uncertain")
    with transaction(connection):
        state = snapshot(connection, key)
        notice = state["notifications"].get(event_id)
        if not notice or notice.get("status") != "sending" or notice.get("token") != token:
            raise WorkflowError("notification claim is absent, stale, or belongs to another owner")
        notice.update(status="pending" if outcome == "not_started" else outcome, outcome=outcome, finished_at=now())
        return _save(connection, state)


def acknowledge(connection, key, event_ids, *, who, expected_revision) -> dict:
    who = _text(who, "who")
    _strings(event_ids, "event_ids")
    if not event_ids or len(set(event_ids)) != len(event_ids):
        raise WorkflowError("acknowledgement needs distinct event IDs")
    with transaction(connection):
        state = _checked(connection, key, expected_revision)
        active = {event["id"] for event in state["attention"]}
        if not set(event_ids) <= active:
            raise WorkflowError("acknowledgement includes an event that is no longer active")
        _acknowledge(connection, state, event_ids, who)
        result = _save(connection, state)
        # The lane may move from on you to on reviewers now (sd:7859).
        url = _observed_url(state["key"])
        # Another item's malformed metadata is not this acknowledgement's concern: skipped, never raised here.
        for row, metadata in _registered(connection, []):
            if state["key"] == f"item:{row['id']}" or (url and url == metadata.get("pull_url")):
                _note_lane(connection, row["id"])
        return result


def _acknowledge(connection, state, event_ids, who) -> None:
    for identifier in event_ids:
        notice = state["notifications"][identifier]
        if notice.get("note_id"):
            resolve_note(connection, notice["note_id"])
        notice["acknowledged_by"] = who
        notice["acknowledged_at"] = now()
    state["acknowledged"] = sorted(set(state["acknowledged"]) | set(event_ids))
    state["attention"] = [event for event in state["attention"] if event["id"] not in event_ids]


def fold(connection, key, url) -> str:
    """Move an observed row with no item to `url`, where GitHub now answers for it (sd:6091). Returns the new key.

    A transferred or renamed repository answers the old url under the new
    one, and the old key's reads never settle. The new key takes the old
    one's state when it has none or holds only a tombstone, so an
    acknowledged event stays acknowledged and a repository renamed back
    reactivates its first key; the old key keeps `moved_to`, which
    `projection` and the refresh queue skip. A row with an item is
    refused: its url is the operator's configuration.
    """
    key = _key(key)
    old = _observed_url(key)
    if old is None:
        raise WorkflowError("fold takes an observed github: or issue: key")
    moved = _key(key.split(":", 1)[0] + ":" + url)
    if moved == key:
        return key
    with transaction(connection):
        if any(old in (metadata.get("pull_url"), metadata.get("issue_url")) for _, metadata in _registered(connection)):
            raise WorkflowError(f"GitHub answers {old} as {_observed_url(moved)}; an item names the old url, "
                                "so set its pull_url or issue_url")
        state = snapshot(connection, key)
        state.pop("moved_to", None)
        if snapshot(connection, moved).get("moved_to") or not connection.execute(
                "SELECT 1 FROM state WHERE kind='checkpoint' AND key=?", ("contribution:" + moved,)).fetchone():
            _save(connection, {**state, "key": moved})
        state["moved_to"] = _observed_url(moved)
        _save(connection, state)
    return moved


def dismiss(connection, key, *, who, expected_revision) -> dict:
    """The operator's close for an observed row with no item (sd:3432); never automatic.

    A pull request whose repository was re-created 404s, so every observation
    holds and the row read open forever. Dismissing acknowledges what is
    active and moves the row to `closed`; `_undismiss` ends it on a lifecycle
    change. A repeat acknowledges attention that arrived since, under the
    first dismissal's record. A row with an item closes through the item.
    """
    who = _text(who, "who")
    key = _key(key)
    url = _observed_url(key)
    if url is None:
        raise WorkflowError("dismiss takes an observed github: or issue: key; close an item through its task")
    with transaction(connection):
        state = _checked(connection, key, expected_revision)
        if not connection.execute("SELECT 1 FROM state WHERE kind='checkpoint' AND key=?", ("contribution:" + key,)).fetchone():
            raise WorkflowError("no observed contribution has this key")
        if any(url in (metadata.get("pull_url"), metadata.get("issue_url")) for _, metadata in _registered(connection)):
            raise WorkflowError("this contribution belongs to an item; close the item instead")
        # A repeat acknowledges attention that arrived after the first and
        # keeps its record; with nothing new it changes nothing (sd:7398).
        if state.get("dismissed") and not state["attention"]:
            return state
        _acknowledge(connection, state, [event["id"] for event in state["attention"]], who)
        state.setdefault("dismissed", {"by": who, "at": now(), "state": (state["observation"] or {}).get("state")})
        return _save(connection, state)
