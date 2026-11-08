"""Contribution refresh and claimed notifications within the shadow collector."""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from datetime import datetime, timezone

from . import contribution_github as github
from .database import transaction
from .workflow import WorkflowError
from .writes import record_state, resolve_state

QUEUE = "contribution-refresh-queue"
MAX_NOTIFICATIONS = 5

#: The requests one complete, unpaginated observation can make, so the loop
#: stops before an attempt that cannot finish. A pull reads itself, its
#: reviews, comments, inline comments, timeline, check runs and statuses,
#: then itself again, plus its head's workflow runs when its check names
#: repeat (sd:1776); an issue reads itself, its comments and its timeline,
#: then itself again. A merge dependency reads its pull, a release one adds the release,
#: the tag ref, one peel, the comparison, PyPI and the ref again, and an
#: issue one reads the issue and, unless that is closed as completed, one
#: page of the pull requests referencing it. An `item` dependency is
#: resolved in-process and priced so, not left out: `_cost` refuses a kind
#: this table does not name rather than reserving nothing for it.
#: Pagination costs more; an attempt the table underestimates fails on the
#: budget, is held on its own key, and rotates to the next run.
PULL_REQUESTS = 9
ISSUE_REQUESTS = 4
DEPENDENCY_REQUESTS = {"item": 0, "merge": 1, "release": 7, "issue": 2}
#: Open pulls no run has read, taken first each run (pack sd:7844). Capped so
#: a pull whose read keeps failing cannot take the whole budget every run.
UNREAD_SLOTS = 10


def _queue(connection):
    row = connection.execute("SELECT id,body FROM state WHERE kind='checkpoint' AND key=? "
                             "AND resolved_at IS NOT NULL ORDER BY id DESC LIMIT 1", (QUEUE,)).fetchone()
    if row is None:
        return {"revision": None, "pending": []}
    body = json.loads(row["body"])
    if not isinstance(body, dict) or not isinstance(body.get("pending"), list):
        raise ValueError("contribution scheduling checkpoint is malformed")
    return {"revision": row["id"], "pending": body["pending"]}


def _save_queue(connection, pending, revision):
    with transaction(connection):
        if _queue(connection)["revision"] != revision:
            raise ValueError("contribution scheduling checkpoint changed")
        saved = record_state(connection, "checkpoint", key=QUEUE, body={"pending": pending})
        resolve_state(connection, saved)
    return saved


def _kind(entry):
    """The key prefix routes an entry: `github:` and `issue:` read a URL, `item:` reads dependencies."""
    return entry["key"].split(":", 1)[0]


def _merge(pending, additions):
    indexed = {}
    for row in [*pending, *additions]:
        kind = _kind(row)
        if kind == "github":
            url = github.canonical_url(row["url"])
            row = {**row, "url": url, "key": "github:" + url}
        elif kind == "issue":
            url = github.canonical_issue_url(row["url"])
            row = {**row, "url": url, "key": "issue:" + url}
        indexed[row["key"]] = row
    return list(indexed.values())


#: The never-read keys a run reads ahead of the rotation, newest first (sd:7840).
FRESH_SLOTS = 5


def _order(pending, fresh=frozenset(), unread=frozenset(), *, snapshots=None, observed_at=None, held=frozenset()):
    # Explicit local work gets priority, while ordinary authored work has two
    # guaranteed slots before the rest follow in queue order. The budget, not a
    # count, decides how far down the list a run gets; rotation in `refresh`
    # prevents a failing first item from starving the ones behind it.
    # `unread` keys, open pulls no run has read, come before the ordinary
    # slots (pack sd:7844): Contributions lists a pull only once read, and an
    # open one queued behind newer discoveries waited too.
    # `fresh` names the keys never read: the newest FRESH_SLOTS follow those
    # slots, so a pull found tonight is not read behind a backlog of
    # thousands (sd:7840). Only that many: the backlog itself is thousands
    # of never-read keys, and all of them ahead would starve the rotation of
    # explicit work and observed open pulls for weeks. A failed read leaves a
    # heartbeat, so the key is no longer fresh, nor unread, and rotates like
    # any other.
    # Explicit rows past the first three come next, then every row last
    # observed open, the oldest reading first: search queues every authored
    # pull, thousands, and a run reads a few dozen, so a pull that left the
    # open search was never read again and read open after it merged
    # (sd:7864, sd:8116). These are the operator's tracked open pulls, read
    # each run, not the backlog. A `held` one, whose last read failed, keeps
    # its old reading and would lead every run, so it rotates instead.
    # The unread tier takes the newest queued first (sd:8116): an open pull
    # found today sat behind 60 older unread ones and was never reached.
    from . import contributions as core

    snapshots = snapshots or {}

    def reading(row):
        """When the row was last read open, or None when it was not."""
        observation = (snapshots.get(row["key"]) or {}).get("observation") or {}
        if observation.get("state") != "open" or row["key"] in held:
            return None
        try:
            return core._moment(observation.get("observed_at"))
        except (WorkflowError, TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    explicit = [row for row in pending if row.get("explicit")]
    first = [row for row in reversed(pending) if not row.get("explicit") and row["key"] in unread][:UNREAD_SLOTS]
    taken = {row["key"] for row in first}
    ordinary = [row for row in pending if not row.get("explicit") and row["key"] not in taken]
    ordered = explicit[:3] + first + ordinary[:2]
    taken = {row["key"] for row in ordered}
    ordered += [row for row in reversed(pending) if row["key"] in fresh and row["key"] not in taken][:FRESH_SLOTS]
    taken = {row["key"] for row in ordered}
    # Every tier past the first skips the keys already taken: the fresh tier
    # can hold explicit rows, and a key read twice fails its revision check.
    ordered += [row for row in explicit[3:] if row["key"] not in taken]
    taken = {row["key"] for row in ordered}
    opened = [(when, n, row) for n, row in enumerate(pending) if row["key"] not in taken and (when := reading(row)) is not None]
    ordered += [row for _, _, row in sorted(opened, key=lambda entry: (entry[0], entry[1]))]
    taken = {row["key"] for row in ordered}
    return ordered + [row for row in pending if row["key"] not in taken]


def _fresh(connection, pending, snapshots):
    """The keys in `pending` never read: no observation and no heartbeat from an attempt.

    `snapshots` are the plan's; a key search added since is read here and not
    kept, so its attempt still checks the revision it reads then.
    """
    from . import contributions as core

    fresh = set()
    for row in pending:
        state = snapshots.get(row["key"]) or core.snapshot(connection, row["key"])
        if state["observation"] is None and connection.execute(
                "SELECT 1 FROM state WHERE kind='heartbeat' AND key=? LIMIT 1",
                ("contribution-observe:" + state["key"],)).fetchone() is None:
            fresh.add(row["key"])
    return fresh


def _unread_open(connection, issues, fresh):
    """The `fresh` keys of pulls GitHub has open, by this run's search or the
    shadow store (pack sd:7844)."""
    urls = {row["url"] for row in issues if row.get("kind") == "pull" and row.get("state") == "open"}
    urls |= {row["url"] for row in connection.execute(
        "SELECT url FROM shadow WHERE tracker='github' AND kind='pull' AND state='open'")}
    return {"github:" + github.canonical_url(url) for url in urls if github.PULL.fullmatch(url)} & set(fresh)


def _cost(entry):
    kind = _kind(entry)
    if kind == "github":
        return PULL_REQUESTS
    if kind == "issue":
        return ISSUE_REQUESTS
    if any(dependency["kind"] not in DEPENDENCY_REQUESTS for dependency in entry["depends_on"]):
        raise ValueError("unpriced dependency kind")
    return sum(DEPENDENCY_REQUESTS[dependency["kind"]] for dependency in entry["depends_on"])


def _affordable(client, entry, reserve, reserve_seconds=0):
    budget = client.budget
    return (budget.deadline - time.monotonic() > reserve_seconds
            and budget.remaining - reserve >= _cost(entry))


def _held(connection, key):
    """The reason `contributions._hold` recorded for `key`, or "" when its last heartbeat is not a hold."""
    row = connection.execute("SELECT body FROM state WHERE kind='heartbeat' AND key=? ORDER BY id DESC LIMIT 1",
                             ("contribution-observe:" + key,)).fetchone()
    try:
        body = json.loads(row["body"]) if row else {}
    except (TypeError, ValueError, RecursionError):
        return ""
    return str(body.get("reason") or "") if isinstance(body, dict) and body.get("ok") is False else ""


def _dependency_name(dependency):
    return dependency.get("url") or dependency.get("contains_pull") or dependency["kind"]


def plan(connection):
    """Read revisions before search I/O; this state schedules, never classifies."""
    from . import contributions as core

    if connection.in_transaction:
        raise ValueError("contribution collection requires no active transaction")
    queued = _queue(connection)
    known = {row["key"]: row for row in queued["pending"]}
    additions = []
    for row in core.projection(connection):
        url = row.get("url")
        key = None
        if url and github.PULL.fullmatch(url) and row.get("external_state") not in {"merged", "closed"}:
            key = "github:" + url
        elif url and github.ISSUE.fullmatch(url) and row.get("external_state") != "closed":
            key = "issue:" + url
        if key:
            why = known.get(key, {}).get("why")
            if why is None:
                why = (core.snapshot(connection, key)["observation"] or {}).get("why", ["author"])
            additions.append({"key": key, "url": url, "why": why,
                              "explicit": bool(row.get("item_id")), "blocking_labels": row.get("blocking_labels", [])})
        if row.get("item_id") and row.get("depends_on"):
            additions.append({"key": f"item:{row['item_id']}", "item": row["item_id"],
                              "depends_on": row["depends_on"], "explicit": True})
    pending = _merge(queued["pending"], additions)
    snapshots = {row["key"]: core.snapshot(connection, row["key"]) for row in pending}
    # A key `fold` moved stays in a queue saved before the fold (sd:6091); its new key is queued instead.
    pending = [row for row in pending if not snapshots[row["key"]].get("moved_to")]
    return {"pending": pending, "revision": queued["revision"],
            "snapshots": {row["key"]: snapshots[row["key"]] for row in pending}}


def fold(connection, planned, key, url):
    """Move a queued contribution to `url`, where GitHub now answers for it (sd:6091).

    `contributions.fold` moves its state; here the queue entry `planned`
    holds takes the new key, so `refresh` reads the new url this run. Both
    snapshots are dropped: the fold wrote both keys, and `refresh` reads
    them again. Returns "" or the refusal's reason, such as an item that
    names the old url.
    """
    from . import contributions as core

    try:
        moved = core.fold(connection, key, url)
    except WorkflowError as error:
        return str(error)
    entries = [{**row, "key": moved, "url": url} if row["key"] == key else row for row in planned["pending"]]
    planned["pending"] = _merge(entries, [])
    for name in (key, moved):
        planned["snapshots"].pop(name, None)
    return ""


def note_lanes(connection) -> list[str]:
    """Link the pull requests `sd-ship prepare` opened, then note each registered pull request's lane (sd:7859).

    A side observation of the sync, as the protection collector is: what it could not do is returned, one line per
    receipt or item, never raised, and never a failed sync or a held watermark (review round 2). Each pass is guarded
    on its own, so a link pass that fails still lets the lane notes run.
    """
    from . import contributions as core

    said: list[str] = []
    try:
        said += core.link_shipped(connection)[1]
    except Exception as error:  # noqa: BLE001 - a side observation never fails the sync
        said.append(f"pull-request links were not read: {type(error).__name__}: {error}")
    try:
        said += core.record_lanes(connection)[1]
    except Exception as error:  # noqa: BLE001 - same rule
        said.append(f"pull-request lane notes were not written: {type(error).__name__}: {error}")
    return said


def refresh(connection, planned, issues, *, client, observed_at, search_complete=False, reserve=0,
            reserve_seconds=0):
    """Stage discovered work durably, then collect details until the budget runs out.

    `reserve` is how many requests to leave for the collectors that follow on
    the same budget, and `reserve_seconds` how much of its time: a detail is
    not started once no more than that is left before the deadline. `errors`
    are the collector's own failures -- a queue checkpoint that moved, a
    malformed identity, no authenticated operator -- and make the run
    unsuccessful. `incomplete` names each contribution whose
    observation could not be completed this run: that is recorded on the
    contribution's own heartbeat and reported, and fails nothing else.
    """
    from . import contributions as core

    known = {row["key"]: row for row in planned["pending"]}
    invalid = any(row.get("kind") == "pull" and "author" in row.get("why", [])
                  and not github.PULL.fullmatch(row["url"]) for row in issues)
    additions = [{"key": "github:" + github.canonical_url(row["url"]), "url": github.canonical_url(row["url"]), "why": row.get("why", []),
                  "blocking_labels": [], "explicit": False} for row in issues
                 if row.get("kind") == "pull" and github.PULL.fullmatch(row["url"])
                 and ("author" in row.get("why", []) or "github:" + github.canonical_url(row["url"]) in known)]
    for index, row in enumerate(additions):
        current = known.get(row["key"])
        if current is not None:
            # Incomplete search can add observed context, but cannot prove its removal.
            why = row["why"] if search_complete is True and not invalid else sorted(set(current["why"]) | set(row["why"]))
            additions[index] = {**current, "why": why}
    pending = _merge(planned["pending"], additions)
    errors = ["search returned a malformed contribution identity"] if invalid else []
    if not pending:
        return {"errors": errors, "queued": 0, "attempted": 0, "completed_keys": [], "incomplete": [], "staged": not invalid}
    try:
        revision = _save_queue(connection, pending, planned["revision"])
    except ValueError as error:
        return {"errors": [str(error)], "queued": len(pending), "attempted": 0, "completed_keys": [], "incomplete": [], "staged": False}
    snapshots = dict(planned["snapshots"])
    fresh = _fresh(connection, pending, snapshots)
    opened = [row["key"] for row in pending if ((snapshots.get(row["key"]) or {}).get("observation") or {}).get("state") == "open"]
    ordered = _order(pending, fresh, _unread_open(connection, issues, fresh), snapshots=snapshots,
                     observed_at=observed_at, held={key for key in opened if _held(connection, key)})
    completed, incomplete, attempted, terminal = [], [], [], set()
    operator, identified = None, False
    # Pagination is not priced, so the client itself stops at the reserve: an
    # attempt that pages past it fails on the budget and is held, and the
    # collectors that follow keep their requests (sd:1219).
    held, client.floor = getattr(client, "floor", 0), reserve
    try:
        for entry in ordered:
            key = entry["key"]
            # Stop rather than start an attempt the budget cannot finish: an
            # unattempted entry keeps its place at the head of the queue, while a
            # half-read one would be held on its own key for nothing. The identity
            # read is one more request before the first detail that needs it, so
            # it is priced with that detail and never spent on a free `item`
            # entry's budget (sd:1219).
            identify = _kind(entry) != "item" and not identified
            if not _affordable(client, entry, reserve + identify, reserve_seconds):
                break
            if identify:
                identified = True
                try:
                    operator = github.identity(client.get("/user"))
                except (github.Unavailable, KeyError, TypeError) as error:
                    errors.append(str(error))
            attempted.append(entry)
            if key not in snapshots:
                snapshots[key] = core.snapshot(connection, key)
            try:
                if _kind(entry) != "item":
                    collect, observe = (github.issue, core.observe_issue) if _kind(entry) == "issue" else (github.pull, core.observe_pull)
                    try:
                        if operator is None:
                            raise github.Unavailable("authenticated GitHub identity unavailable")
                        observation = collect(client, entry["url"], operator, observed_at=observed_at,
                                              why=entry["why"], blocking_labels=entry["blocking_labels"])
                    except (github.Unavailable, KeyError, TypeError, AttributeError) as error:
                        observation = {"complete": False, "observed_at": observed_at,
                                       "reason": str(error) if isinstance(error, github.Unavailable) else "malformed GitHub contribution response"}
                    saved = observe(connection, entry["url"], observation, expected_revision=snapshots[key]["revision"])
                    complete = observation["complete"] and saved["revision"] != snapshots[key]["revision"]
                    reason = observation.get("reason", "")
                    if complete and observation["state"] in {"closed", "merged"} and not any(
                            notice["status"] == "pending" for notice in saved["notifications"].values()):
                        terminal.add(key)
                else:
                    observations = [github.dependency(client, dependency) for dependency in entry["depends_on"]
                                    if dependency["kind"] != "item"]
                    complete = all(row["state"] != "unknown" for row in observations)
                    # The dependency's own reason -- a release with no configured
                    # tag, a 503 -- is what the item's heartbeat has to say, or the
                    # operator reads "incomplete" every night with nothing to act on.
                    reason = "; ".join(f"{_dependency_name(row['dependency'])}: {row['reason']}" for row in observations
                                       if row["state"] == "unknown" and row.get("reason"))
                    saved = core.observe_dependencies(connection, entry["item"], observations, observed_at=observed_at,
                                                      expected_revision=snapshots[key]["revision"], complete=complete,
                                                      reason=reason)
                    complete = complete and saved["revision"] != snapshots[key]["revision"]
                if complete:
                    completed.append(key)
                else:
                    # A read the core held -- an unknown actor, a proof mismatch --
                    # has no collector reason; the core's own is on the heartbeat (sd:1219).
                    reason = reason or _held(connection, snapshots[key]["key"])
                    incomplete.append(f"{key}: {reason}" if reason else key)
            except (ValueError, WorkflowError) as error:
                incomplete.append(f"{key}: refused: {error}")
                # The refusal -- a revision another observer moved -- is this
                # contribution's, so its own heartbeat says so (sd:1219).
                with transaction(connection):
                    record_state(connection, "heartbeat", key="contribution-observe:" + snapshots[key]["key"],
                                 body={"ok": False, "reason": f"refused: {error}"})
    finally:
        client.floor = held
    attempted_keys = {row["key"] for row in attempted}
    remaining = [row for row in pending if row["key"] not in attempted_keys]
    rotated = remaining + [row for row in attempted if row["key"] not in terminal]
    try:
        _save_queue(connection, rotated, revision)
    except ValueError as error:
        errors.append(str(error))
    with transaction(connection):
        record_state(connection, "heartbeat", key="contribution-sync:github",
                     timestamp=datetime.now(timezone.utc).isoformat(),
                     body={"ok": not errors, "reason": "; ".join(errors), "observed_at": observed_at,
                           "queued": len(remaining), "attempted": len(attempted), "completed": len(completed),
                           "incomplete": incomplete})
    return {"errors": errors, "queued": len(remaining), "attempted": len(attempted), "completed_keys": completed,
            "incomplete": incomplete, "staged": not invalid}


def _notify(claim, *, timeout=60):
    message = claim["message"] + ("\n" + claim["url"] if claim.get("url") else "")
    argv = ["notify", "-t", claim["title"], "-k", "status", "-F", message]
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)  # nosec B603 - fixed argv
    except OSError:
        return "not_started"
    except subprocess.TimeoutExpired:
        return "uncertain"
    return "sent" if done.returncode == 0 else "uncertain"


def dispatch_pending(connection, *, notifier=None, keys=None, deadline=None):
    """Claim once, send outside the transaction, and hold ambiguous outcomes."""
    from . import contributions as core

    if connection.in_transaction:
        raise ValueError("notifications cannot run inside a writer transaction")
    owner = "shadow-sync:" + uuid.uuid4().hex
    completed: list[dict] = []
    seen = set()
    for row in core.projection(connection):
        for source in row.get("attention_sources", []):
            key = source["key"]
            if keys is not None and key not in keys:
                continue
            for event in source["event_ids"]:
                if (key, event) in seen:
                    continue
                if len(completed) >= MAX_NOTIFICATIONS or (deadline is not None and deadline <= time.monotonic()):
                    return completed
                seen.add((key, event))
                claim = core.claim_notification(connection, key, event, owner=owner)
                if claim is None:
                    continue
                # A process crash leaves 'sending' held. Never infer that no send happened.
                try:
                    outcome = notifier(claim) if notifier else _notify(claim, timeout=min(60, deadline - time.monotonic()) if deadline else 60)
                except Exception:
                    outcome = "uncertain"
                if outcome not in {"sent", "not_started", "uncertain"}:
                    outcome = "uncertain"
                core.finish_notification(connection, key, event, claim["token"], outcome=outcome)
                completed.append({"key": key, "event_id": event, "outcome": outcome})
    return completed
