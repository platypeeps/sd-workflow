"""Queue and lifecycle transactions. Filesystem/process effects live in the runner.

An assignment is the request, a runner_run is one attempt, and the exec note
is its output. Every active attempt holds a lease until cleanup is durable.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import ledger, paths, writes
from .database import transaction
from .errors import SdDbError
from .writes import add_note, create_assignment, now, record_cost, transition

# The result on a bundle member's row an operator cancelled on its own: it left the bundle, so no Retry of the
# bundle queues it again or clears its park (review round 2 of sd:9222).
ALONE = "cancelled on its own by "


class RunnerRefused(SdDbError):
    """An unsafe or stale queue action, with no partial selection written."""


def restoration_pending(connection) -> bool:
    return connection.execute("SELECT 1 FROM state WHERE kind='restore' AND resolved_at IS NULL LIMIT 1").fetchone() is not None


def revision(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def queue_state(connection, assignment: int) -> dict:
    row = connection.execute("SELECT * FROM assignment WHERE id = ?", (assignment,)).fetchone()
    if row is None:
        raise RunnerRefused(f"no assignment {assignment}")
    result = dict(row)
    run = connection.execute("SELECT * FROM runner_run WHERE assignment = ? ORDER BY run DESC LIMIT 1",
                             (assignment,)).fetchone()
    result["run"] = dict(run) if run else None
    result["revision"] = revision(result)
    return result


#: The item kinds an agent run may take, and the kinds the run panel
#: (`runner_screen.item_controls`) offers. `_item` refuses every other kind
#: whatever repository and branch it carries, so readiness, enqueue, claim and
#: the Today and Backlog run selection refuse it too: `create_item` accepts a
#: repository and a branch for any kind, and since sd:809 a task reclassified
#: to `followup` keeps both. `delivery_candidates` delivers these less a
#: skill review, a `task` since sd:7004 (`skills_catalog.is_review`).
RUNNABLE_KINDS = ("work", "task", "report")
#: A teammate run's assignment status (pack sd:7221): a lead's in-session builder holds the item.
IN_SESSION = "in_session"
#: The statuses that own an item against a second writer: the runner's, plus a teammate's.
#: Pickup, queueing and claim read these; a status write reads only the runner's (criterion 7.19),
#: so the session still moves its own items.
OWNING = ("queued", "running", "ending", IN_SESSION)
#: The assignment scopes whose runs no runner supervisor walks: a build run (pack sd:6093), a
#: draft run (pack sd:6361) and a teammate run (pack sd:7221). None writes a runner journal, and
#: none merges by hand, so `backup.journaled_runs` and `ship.manual_merge_guard` skip them
#: through `supervised`, the one exemption (pack sd:7282).
UNSUPERVISED_SCOPES = ("build", "draft", "teammate")


def supervised(scope: str) -> str:
    """A SQL condition, true when the assignment scope column `scope` is a runner's: none of
    `UNSUPERVISED_SCOPES`. A run with no assignment (NULL) is a runner's."""
    named = ", ".join(f"'{name}'" for name in UNSUPERVISED_SCOPES)
    return f"COALESCE({scope}, '') NOT IN ({named})"


def _item(connection, item: int, *, delivery=False) -> dict:
    kind = connection.execute("SELECT kind FROM item WHERE id = ?", (item,)).fetchone()
    if kind is not None and kind["kind"] not in RUNNABLE_KINDS:
        raise RunnerRefused(f"item {item} is a {kind['kind']} item; an agent runs only "
                            f"{', '.join(RUNNABLE_KINDS[:-1])} and {RUNNABLE_KINDS[-1]} items")
    row = connection.execute("SELECT item.*, repo.remote FROM item "
                             "JOIN repo ON repo.path = item.repo WHERE item.id = ?", (item,)).fetchone()
    if row is None:
        raise RunnerRefused(f"item {item} needs a registered repository")
    result = dict(row)
    branch = result["branch"]
    if not branch or branch.startswith("-") or re.search(r"[\s~^:?*\[\\]", branch) or ".." in branch or "@{" in branch:
        raise RunnerRefused(f"item {item} needs a valid branch")
    if not result["remote"]:
        raise RunnerRefused(f"item {item} repository has no remote")
    if result["status"] == "done" or (result["status"] == "ready_to_send" and not delivery):
        raise RunnerRefused(f"item {item} is {result['status']}; reopen it explicitly before running")
    return result


def _eligible_after(connection, assignment: dict) -> bool:
    predecessor = assignment["after"]
    if predecessor is None:
        return True
    row = connection.execute("SELECT status, item FROM assignment WHERE id = ?", (predecessor,)).fetchone()
    if row is None:
        raise RunnerRefused(f"missing predecessor {predecessor}")
    if row["status"] in {"blocked", "cancelled"}:
        connection.execute("UPDATE assignment SET status = 'blocked', result = ?, ended = ? WHERE id = ?",
                           (f"predecessor {predecessor} ended {row['status']}", now(), assignment["id"]))
        return False
    # A successful provider exit is completion; only a verified merge is delivery.
    return row["status"] == "done" and connection.execute(
        "SELECT 1 FROM assignment WHERE item = ? AND role = 'merge' AND status = 'done' AND phase = 'merged'",
        (row["item"],)).fetchone() is not None


def _acyclic(connection) -> None:
    edges = {row["id"]: row["after"] for row in connection.execute("SELECT id, after FROM assignment")}
    for start in edges:
        seen = set()
        current = start
        while current is not None:
            if current in seen:
                raise RunnerRefused(f"assignment dependency cycle at {current}")
            seen.add(current)
            if current not in edges:
                raise RunnerRefused(f"missing assignment dependency {current}")
            current = edges[current]


def enqueue(connection, items: list[int], *, parallel=False, role="author", scope="item",
            budget_minutes=90, budget_usd=None, after=None, who, expected_revisions=None) -> list[dict]:
    """Queue one assignment per item, or refuse the whole selection.

    `budget_usd` is a bound in dollars the ledger enforces, or None for none.
    It is checked before anything is written, by `ledger.budget_for_selection`:
    the money rule, an `author` or `reviewer` role, and every entry the
    registry orders for that role a `url` entry, because a `start` entry's
    session calls the vendor itself and the ledger cannot stand in front of
    it. The refusal names the entry; nothing is queued.
    """
    if not items or len(set(items)) != len(items):
        raise RunnerRefused("select one or more distinct items")
    if role not in {"author", "reviewer", "exec", "merge"} or not isinstance(scope, str) or not scope.strip():
        raise RunnerRefused("role must be author, reviewer, exec, or merge, with a nonempty scope")
    if parallel and role != "author":
        raise RunnerRefused("only author assignments may use the parallel lane")
    if isinstance(budget_minutes, bool) or not isinstance(budget_minutes, int) or budget_minutes <= 0:
        raise RunnerRefused("budget_minutes must be a positive integer")
    with transaction(connection):
        if restoration_pending(connection):
            raise RunnerRefused("database restore reimport is incomplete; queue dispatch is held")
        # Under the same write lock as the rows: the registry read may seed
        # the provider tables, and the check must see what the runner will.
        budget_usd = ledger.budget_for_selection(connection, budget_usd, role=role, items=items)
        if role == "exec":
            from .runner_exec import validate_enqueue
            validate_enqueue(connection, items, scope)
        if expected_revisions is not None:
            from .workflow import item_state
            if {str(key) for key in expected_revisions} != {str(item) for item in items}:
                raise RunnerRefused("the batch needs one expected revision per selected item")
            for item in items:
                expected = expected_revisions.get(item, expected_revisions.get(str(item)))
                if item_state(connection, item)["revision"] != expected:
                    raise RunnerRefused(f"item {item} changed; refresh the selection")
        selected = [_item(connection, item, delivery=role == "merge") for item in items]
        pairs = {}
        for item in selected:
            pair = item["repo"], item["branch"]
            if pair in pairs:
                raise RunnerRefused(f"items {pairs[pair]} and {item['id']} share {pair[0]}:{pair[1]}")
            pairs[pair] = item["id"]
            held = connection.execute("SELECT run FROM runner_lease WHERE repo = ? AND branch = ? AND released_at IS NULL", pair).fetchone()
            active = connection.execute(f"SELECT id FROM assignment WHERE item = ? AND status IN {OWNING}", (item["id"],)).fetchone()
            if held or active:
                raise RunnerRefused(f"item {item['id']} already has queued work or a retained lease")
        created = []
        for item in selected:
            ident = create_assignment(connection, item=item["id"], role=role, status="queued",
                                      lane="parallel" if parallel else "serial", budget_minutes=budget_minutes,
                                      budget_usd=budget_usd, after=after)
            connection.execute("UPDATE assignment SET scope = ?, queued_at = ?, started = NULL WHERE id = ?",
                               (scope, now(), ident))
            add_note(connection, item["id"], "comment", f"Assignment {ident} queued by {who}; {role}, {scope}, {budget_minutes} minutes"
                     + (f", budget {budget_usd:.2f} USD" if budget_usd is not None else ""))
            created.append(ident)
            if not parallel:
                after = ident
        _acyclic(connection)
        return [queue_state(connection, ident) for ident in created]


def queued(connection) -> list[dict]:
    return [dict(row) for row in connection.execute("SELECT * FROM assignment WHERE status = 'queued' ORDER BY id")]


def active_runs(connection) -> list[dict]:
    return [dict(row) for row in connection.execute(
        "SELECT runner_run.*, assignment.status, assignment.role, assignment.lane, assignment.scope, "
        "assignment.item, assignment.budget_minutes FROM runner_run JOIN assignment ON assignment.id = runner_run.assignment "
        "WHERE runner_run.released_at IS NULL ORDER BY runner_run.created_at")]


def claim(connection, assignment: int, *, owner: str, work_root: Path, retention_root: Path) -> dict | None:
    """Claim and lease under BEGIN IMMEDIATE; return None when another row has the turn."""
    with transaction(connection):
        request = queue_state(connection, assignment)
        if restoration_pending(connection):
            raise RunnerRefused("database restore reimport is incomplete; queue dispatch is held")
        _acyclic(connection)
        if request["status"] != "queued" or not _eligible_after(connection, request):
            return None
        item = _item(connection, request["item"], delivery=request["role"] == "merge")
        leases = connection.execute("SELECT runner_lease.*, runner_run.quarantine FROM runner_lease "
            "JOIN runner_run ON runner_run.id = runner_lease.run WHERE runner_lease.repo = ? "
            "AND runner_lease.released_at IS NULL", (item["repo"],)).fetchall()
        exclusive = request["lane"] != "parallel" or request["role"] != "author"
        if any(row["quarantine"] or exclusive or row["exclusive"] or row["branch"] == item["branch"] for row in leases):
            return None
        if not exclusive:
            earlier = connection.execute("SELECT assignment.* FROM assignment JOIN item ON item.id = assignment.item "
                "WHERE item.repo = ? AND assignment.status = 'queued' AND assignment.id < ? "
                "AND (assignment.lane = 'serial' OR assignment.role != 'author') ORDER BY assignment.id",
                (item["repo"], assignment)).fetchall()
            if any(_eligible_after(connection, dict(row)) for row in earlier):
                return None
        run = request["run_count"] + 1
        ident = uuid.uuid4().hex
        stamp = now()
        work = work_root / str(item["id"]) / f"{assignment}-{run}-{ident}"
        retained = retention_root / str(assignment) / str(run) / "clone"
        connection.execute("UPDATE assignment SET status = 'running', started = ?, ended = NULL, run_count = ? WHERE id = ? AND status = 'queued'",
                           (stamp, run, assignment))
        connection.execute("INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (ident, assignment, run, item["repo"], item["branch"], owner, str(work), str(retained), stamp, stamp))
        connection.execute("INSERT INTO runner_lease (run, repo, branch, exclusive, acquired_at) VALUES (?, ?, ?, ?, ?)",
                           (ident, item["repo"], item["branch"], int(exclusive), stamp))
        if request["role"] == "merge" and request["parent"]:
            previous = connection.execute("SELECT reviewed_head FROM runner_run WHERE assignment = ? ORDER BY run DESC LIMIT 1", (request["parent"],)).fetchone()
            if previous:
                connection.execute("UPDATE runner_run SET reviewed_head = ? WHERE id = ?", (previous["reviewed_head"], ident))
        if request["role"] not in {"merge", "exec"} and item["status"] != "in_progress":
            transition(connection, item["id"], "in_progress", who="runner", reason=f"assignment {assignment} claimed")
        result = queue_state(connection, assignment)
        result["item_record"] = item
        return result


def run_state(connection, ident: str) -> dict:
    row = connection.execute("SELECT * FROM runner_run WHERE id = ?", (ident,)).fetchone()
    if row is None:
        raise RunnerRefused(f"run {ident} is absent; the database may have been restored")
    return dict(row)


def recovery_snapshot(connection, ident: str, *, assignment: int | None = None) -> dict:
    """Read both sides of the ownership binding without inventing missing work."""
    row = connection.execute("SELECT * FROM runner_run WHERE id=?", (ident,)).fetchone()
    current = dict(row) if row else None
    selected = current["assignment"] if current else assignment
    try:
        request = queue_state(connection, selected) if selected is not None else None
    except RunnerRefused:
        request = None
    return {"run": current, "assignment": request}


def recover_from_journal(connection, record: dict, *, expected_snapshot: str) -> dict:
    """Restore ownership evidence forward; never infer delivery or recreate an item."""
    with transaction(connection):
        snapshot = recovery_snapshot(connection, record["id"], assignment=record["assignment"])
        if revision(snapshot) != expected_snapshot:
            raise RunnerRefused("recovery selection changed; prepare a new plan")
        current, request = snapshot["run"], snapshot["assignment"]
        if request is None:
            raise RunnerRefused("assignment is absent; restore a database backup containing its original work")
        item = connection.execute("SELECT repo,branch FROM item WHERE id=?", (request["item"],)).fetchone()
        if not item or item["branch"] != record["branch"] or not paths.same(item["repo"], record["repo"]):
            raise RunnerRefused("journal does not match the restored assignment's repository and branch")
        # A journal written before migration 014 names the absolute path; the
        # row it restores names the key the item holds now (sd:1439). One
        # written before 018 has no `detached_from`, and one before 042 no
        # build columns; each is NULL (sd:2581, pack sd:6093).
        from .runner_journal import LATER_COLUMNS
        record = {**dict.fromkeys(LATER_COLUMNS), **record, "repo": item["repo"]}
        if request["run_count"] > record["run"] or (request["run"] and request["run"]["id"] != record["id"]):
            raise RunnerRefused("another attempt belongs to this assignment; restore compatible database evidence")
        columns = [row["name"] for row in connection.execute("PRAGMA table_info(runner_run)")]
        if set(record) != set(columns):
            raise RunnerRefused("journal fields differ from the installed runner schema")
        if current:
            immutable = ("id", "assignment", "run", "repo", "branch", "owner", "work_path", "retained_path", "created_at")
            if any(current[key] != record[key] for key in immutable):
                raise RunnerRefused("journal identity conflicts with database ownership")
            if record["journal_version"] <= current["journal_version"]:
                raise RunnerRefused("recovery never replaces an equal or newer database run")
            if current["released_at"] and not record["released_at"]:
                raise RunnerRefused("recovery cannot reopen released ownership")
        exclusive = request["lane"] != "parallel" or request["role"] != "author"
        leases = connection.execute("SELECT * FROM runner_lease WHERE repo=? AND released_at IS NULL AND run!=?",
                                    (record["repo"], record["id"])).fetchall()
        if not record["released_at"] and any(exclusive or row["exclusive"] or row["branch"] == record["branch"] for row in leases):
            raise RunnerRefused("recovered ownership conflicts with an existing lease")
        own_lease = connection.execute("SELECT * FROM runner_lease WHERE run=?", (record["id"],)).fetchone()
        if own_lease and (own_lease["repo"], own_lease["branch"], own_lease["exclusive"]) != (record["repo"], record["branch"], int(exclusive)):
            raise RunnerRefused("recovered run conflicts with its recorded lease identity")
        if current:
            names = [name for name in columns if name != "id"]
            connection.execute("UPDATE runner_run SET " + ",".join(name + "=?" for name in names) + " WHERE id=?",
                               (*[record[name] for name in names], record["id"]))
        else:
            connection.execute("INSERT INTO runner_run (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
                               [record[name] for name in columns])
        connection.execute("INSERT INTO runner_lease(run,repo,branch,exclusive,acquired_at,released_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(run) DO UPDATE SET released_at=excluded.released_at",
            (record["id"], record["repo"], record["branch"], int(exclusive), record["created_at"], record["released_at"]))
        connection.execute("UPDATE assignment SET status=?,run_count=MAX(run_count,?),result=? WHERE id=?",
            ("blocked" if record["released_at"] else "ending", record["run"], "ownership recovered; operator must review and requeue", record["assignment"]))
        if not record["released_at"]:
            # The journal proves ownership. It does not prove restored item progress.
            connection.execute("UPDATE runner_run SET outcome='blocked',detail=?,end_action=NULL,delivery_proof=NULL,journal_version=journal_version+1,updated_at=? WHERE id=?",
                ("ownership recovered; operator must review and requeue", now(), record["id"]))
        add_note(connection, request["item"], "comment", f"Recovered run {record['id']} ownership from durable journal; no delivery inferred")
        return run_state(connection, record["id"])


def branch_prepared(connection, ident: str, binding: dict) -> None:
    """Consume a new-branch promise only after its owned clone published it."""
    from .writes import set_item_fields
    with transaction(connection):
        run = run_state(connection, ident)
        request = queue_state(connection, run["assignment"])
        if request["status"] != "running" or run["released_at"]:
            raise RunnerRefused("branch preparation no longer owns its active assignment")
        row = connection.execute("SELECT fields FROM item WHERE id=?", (request["item"],)).fetchone()
        fields = json.loads(row["fields"] or "{}")
        if fields.get("runner_branch") != binding:
            raise RunnerRefused("new branch binding changed during preparation")
        del fields["runner_branch"]
        set_item_fields(connection, request["item"], fields=fields)


def update_run(connection, ident: str, **fields) -> dict:
    allowed = {"start_step", "end_step", "detail", "supervisor_pid", "supervisor_pgid", "supervisor_start",
               "provider", "vendor", "base_head", "authored_head", "reviewed_head", "delivery_proof", "ignored_manifest", "quarantine"}
    if not fields or set(fields) - allowed:
        raise RunnerRefused(f"unsupported run update: {sorted(set(fields) - allowed)}")
    with transaction(connection):
        current = run_state(connection, ident)
        if current["released_at"]:
            raise RunnerRefused(f"run {ident} is already released")
        columns = ", ".join(f"{name} = ?" for name in fields)
        connection.execute(f"UPDATE runner_run SET {columns}, journal_version = journal_version + 1, updated_at = ? WHERE id = ?",
                           (*fields.values(), now(), ident))
        if fields.get("provider") and fields["provider"] != "fixture":
            connection.execute("UPDATE assignment SET provider = ? WHERE id = ?", (fields["provider"], current["assignment"]))
        return run_state(connection, ident)


def begin_ending(connection, ident: str, *, outcome: str, detail: str) -> dict:
    if outcome not in {"done", "blocked", "cancelled"}:
        raise RunnerRefused(f"unsupported outcome {outcome}")
    with transaction(connection):
        current = run_state(connection, ident)
        if current["released_at"] or current["outcome"]:
            return current
        connection.execute("UPDATE assignment SET status = 'ending' WHERE id = ?", (current["assignment"],))
        connection.execute("UPDATE runner_run SET outcome = ?, detail = ?, end_step = 'pending', journal_version = journal_version + 1, updated_at = ? WHERE id = ?",
                           (outcome, detail, now(), ident))
        return run_state(connection, ident)


def release(connection, ident: str, *, output_path: str | None = None, exit_code: int | None = None) -> dict:
    """Terminal status and lease release are one transaction, after durable cleanup."""
    with transaction(connection):
        run = run_state(connection, ident)
        if run["released_at"]:
            return run
        if run["quarantine"] or run["end_step"] != "retained" or not run["outcome"]:
            raise RunnerRefused("cannot release a run before retention or while quarantined")
        stamp = now()
        connection.execute("UPDATE runner_lease SET released_at = ? WHERE run = ?", (stamp, ident))
        connection.execute("UPDATE runner_run SET released_at = ?, end_step = 'released', journal_version = journal_version + 1, updated_at = ? WHERE id = ?", (stamp, stamp, ident))
        connection.execute("UPDATE assignment SET status = ?, ended = ?, result = CASE WHEN phase = 'merged' THEN result ELSE ? END WHERE id = ?",
                           (run["outcome"], stamp, run["detail"], run["assignment"]))
        assignment = queue_state(connection, run["assignment"])
        if run["delivery_proof"]:
            from .ship import finalize_delivery
            finalize_delivery(connection, ident, json.loads(run["delivery_proof"]))
        if assignment["item"] is not None and assignment["role"] != "exec":
            item = connection.execute("SELECT status FROM item WHERE id = ?", (assignment["item"],)).fetchone()
            target = "ready_to_send" if run["outcome"] == "done" else "blocked"
            if item["status"] != target and not (assignment["role"] == "merge" and item["status"] == "done"):
                transition(connection, assignment["item"], target, who="runner", reason=run["detail"] or run["outcome"])
            add_note(connection, assignment["item"], "exec", run["detail"] or run["outcome"], session="runner",
                     started=run["created_at"], ended=stamp, output_path=output_path, exit_code=exit_code)
        if run["end_action"] == "resume" and assignment["role"] != "exec":
            connection.execute("UPDATE assignment SET status = 'queued', queued_at = ?, started = NULL, ended = NULL WHERE id = ?",
                               (stamp, run["assignment"]))
        return run_state(connection, ident)


def request_cancel(connection, assignment: int, *, expected_revision: str, who) -> dict:
    with transaction(connection):
        current = queue_state(connection, assignment)
        if current["revision"] != expected_revision:
            raise RunnerRefused("assignment changed; refresh before cancelling")
        owned = current["run"] and not current["run"]["released_at"]
        # A `running` row no attempt owns -- written by hand, or from before
        # the runner recorded attempts -- has no process to stop, so the
        # cancel ends it, as a queued row's does (sd:991, owner note 2706).
        # "Owned" is `released_at IS NULL`, as in `workflow.change_status`.
        if current["status"] == "queued" or (current["status"] == "running" and not owned):
            connection.execute("UPDATE assignment SET status = 'cancelled', ended = ?, result = ? WHERE id = ?",
                               (now(), f"cancelled by {who}", assignment))
        elif current["status"] == "running":
            connection.execute("UPDATE runner_run SET cancel_requested = ?, journal_version = journal_version + 1, updated_at = ? WHERE id = ?",
                               (f"cancelled by {who}", now(), current["run"]["id"]))
        else:
            raise RunnerRefused("only queued work or a running owned attempt can be cancelled")
        return queue_state(connection, assignment)


def requeue_refusal(current: dict) -> str:
    """Why `requeue` refuses the assignment `queue_state` read as `current`; "" when it takes it. The palette
    asks before it writes its dispatch marker: a refusal after it leaves an execution reconcile cannot close."""
    if current["role"] == "exec":
        return "finite execution authorizations are single-use; create a new command request"
    if current["status"] not in {"blocked", "cancelled"} or (current["run"] and not current["run"]["released_at"]):
        return "requeue requires a terminal attempt with its lease released"
    # Requeue rewrites the result, which holds the ALONE marker: the
    # member would rejoin its lead's bundle (sd:9269).
    if current["parent"] is not None and (current["result"] or "").startswith(ALONE):
        return "this bundle member was cancelled on its own; press Build on its item to build it fresh"
    return ""


def requeue(connection, assignment: int, *, expected_revision: str, who) -> dict:
    with transaction(connection):
        current = queue_state(connection, assignment)
        if current["revision"] != expected_revision:
            raise RunnerRefused("assignment changed; refresh before requeuing")
        if why := requeue_refusal(current):
            raise RunnerRefused(why)
        connection.execute("UPDATE assignment SET status = 'queued', queued_at = ?, started = NULL, ended = NULL, result = ? WHERE id = ?",
                           (now(), f"requeued by {who}", assignment))
        return queue_state(connection, assignment)


def heartbeat(connection, body: dict) -> dict:
    # A writer that copies a read state back (the long-preserve beat) would store a stale derivation.
    body = {key: value for key, value in body.items() if key not in DERIVED_HEARTBEAT}
    with transaction(connection):
        connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('heartbeat', 'runner', ?, ?) "
            "ON CONFLICT(key) WHERE kind = 'heartbeat' AND key = 'runner' DO UPDATE SET timestamp = excluded.timestamp, body = excluded.body",
            (now(), json.dumps(body, sort_keys=True)))
    # The writer's answer leaves out `deployment`, which is for readers, not every pulse.
    return _stored_heartbeat(connection)


def heartbeat_state(connection) -> dict:
    """The stored heartbeat, its freshness verdict, and `deployment` (sd:1952)."""
    state = _stored_heartbeat(connection)
    return state if "timestamp" not in state else {**state, **deployment(state)}


def _stored_heartbeat(connection) -> dict:
    row = connection.execute("SELECT timestamp, body FROM state WHERE kind = 'heartbeat' AND key = 'runner'").fetchone()
    if row is None:
        return {"ok": False, "reason": "runner has never reported a heartbeat"}
    body = _heartbeat_body(row["body"])
    age = (datetime.now(UTC) - datetime.fromisoformat(row["timestamp"])).total_seconds()
    return {**body, "timestamp": row["timestamp"], "age_seconds": age,
            "ok": age <= 3 * body.get("interval_seconds", 10) and body.get("healthy", False)}


#: Heartbeat fields `deployment` derives on read; `heartbeat` never stores them.
DERIVED_HEARTBEAT = ("checkout_commit", "deploy_warning")


_OBJECT_NAME = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def checkout_head(root: Path) -> str | None:
    """The commit a checkout's HEAD names, read from its files, or None.

    Read, not asked of `git`: every heartbeat read runs this, the runtime's
    own keepalive among them, and a `git` with a timeout polls the global
    `time.sleep` and can stall under load. A checkout, a linked worktree
    (`.git` is a `gitdir:` file) and packed refs are read; anything else,
    a reftable repository among them, is None.
    """
    try:
        dotgit = root / ".git"
        if dotgit.is_dir():
            gitdir = dotgit
        elif dotgit.is_file() and (text := dotgit.read_text().strip()).startswith("gitdir:"):
            gitdir = (root / text[len("gitdir:"):].strip()).resolve()
        else:
            return None
        common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve() if (gitdir / "commondir").is_file() else gitdir
        head = (gitdir / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head if _OBJECT_NAME.fullmatch(head) else None
        ref = head[len("ref: "):]
        for base in (gitdir, common):
            if (base / ref).is_file():
                value = (base / ref).read_text().strip()
                return value if _OBJECT_NAME.fullmatch(value) else None
        if (common / "packed-refs").is_file():
            for line in (common / "packed-refs").read_text().splitlines():
                name, _, packed = line.partition(" ")
                if packed == ref and _OBJECT_NAME.fullmatch(name):
                    return name
    except (OSError, UnicodeDecodeError):
        return None
    return None


def deployment(body: dict) -> dict:
    """Whether the daemon runs its checkout's current commit (sd:1952).

    Python reads the runner's modules once, at start, so after a `git pull`
    the daemon keeps the old code until a restart; it ran 11 hours three
    merges old with nothing saying so. `serve` writes `runner_commit` and
    `runner_checkout` once, at start. This compares that commit with the
    checkout's HEAD on disk, with no fetch. A moved checkout is a
    `deploy_warning`, not an unhealthy runner. A heartbeat without the
    commit, from an older daemon or one started outside a checkout, reads
    `runner_commit unknown`.
    """
    started = body.get("runner_commit")
    if not isinstance(started, str) or not started:
        return {"deploy_warning": "runner_commit unknown"}
    checkout = body.get("runner_checkout")
    current = checkout_head(Path(checkout)) if isinstance(checkout, str) and checkout else None
    if not current:
        return {"checkout_commit": None, "deploy_warning": f"runner started at {started[:7]}; checkout HEAD unreadable"}
    if current != started:
        return {"checkout_commit": current,
                "deploy_warning": f"runner started at {started[:7]}, checkout at {current[:7]}; restart to deploy"}
    return {"checkout_commit": current}


def _heartbeat_body(stored) -> dict:
    """The heartbeat row read back into the shape its readers index.

    Only `heartbeat` writes the row, and it serialises a dict, so anything
    else in it was put there by hand. Refused here, once, as the library's
    own error rather than the TypeError a list or a string raised on the
    unpack: every reader (`runner status`, the dashboard's runner panel)
    already catches SdDbError and turns it into its documented answer,
    while none of them named TypeError (sd:934).
    """
    if stored is None:
        found = "an empty body"
    else:
        try:
            body = json.loads(stored)
        except ValueError:
            found = f"not JSON ({stored[:40]!r})"
        else:
            if isinstance(body, dict):
                return body
            kind = {list: "a JSON array", str: "a JSON string", bool: "a JSON boolean", type(None): "JSON null"}.get(type(body), "a JSON number")
            found = f"{kind} ({stored[:40]!r})"
    raise SdDbError(f"the runner heartbeat body is {found}, not the JSON object the runner writes; "
                    f"only the runner writes that row, so it was edited outside it, and the runner's next tick replaces it")


def merge_authority(connection, ident: str, expected_head: str, *, automatic=True) -> dict:
    run = run_state(connection, ident)
    request = queue_state(connection, run["assignment"])
    lease = connection.execute("SELECT * FROM runner_lease WHERE run = ? AND released_at IS NULL", (ident,)).fetchone()
    repo = connection.execute("SELECT * FROM repo WHERE path = ?", (run["repo"],)).fetchone()
    if run["released_at"] or run["quarantine"] or request["role"] != "merge" or request["status"] != "running" or not lease or not lease["exclusive"]:
        raise RunnerRefused("automatic merge requires the live exclusive merge assignment")
    merge = writes.policy_merge(repo["declarations"])  # pack sd:7366: the policy, not the column
    if (automatic and merge != "auto") or run["authored_head"] != expected_head:
        raise RunnerRefused("automatic merge policy or exact authored head does not match")
    if not automatic and request["scope"] != "reconcile":
        raise RunnerRefused("manual reconciliation requires an observation-only merge assignment")
    return {"assignment": request["id"], "item": request["item"], "repo": run["repo"], "branch": run["branch"],
            "owner": run["owner"], "work_path": run["work_path"], "runner_merge": merge}


def record_merge(connection, item: int, *, evidence: dict, run_id: str | None = None) -> int:
    """Called only after the delivery adapter reads the authoritative remote.

    This is evidence recording, never a remote merge command. Sequential work
    does not advance from a pasted URL or an author's claim of success.
    """
    required = {"url", "head", "merge_commit", "base", "repository", "observed_at"}
    if set(evidence) != required or any(not isinstance(evidence[k], str) or not evidence[k] for k in required):
        raise RunnerRefused("merged delivery requires complete remote evidence")
    with transaction(connection):
        if run_id:
            run = run_state(connection, run_id)
            request = queue_state(connection, run["assignment"])
            authority = merge_authority(connection, run_id, evidence["head"], automatic=request["scope"] != "reconcile")
            if authority["item"] != item:
                raise RunnerRefused("merge evidence belongs to a different item")
            ident = authority["assignment"]
            connection.execute("UPDATE assignment SET phase = 'merged', result = ? WHERE id = ?",
                               (json.dumps(evidence, sort_keys=True), ident))
            return ident
        old = connection.execute("SELECT id FROM assignment WHERE item = ? AND role = 'merge' AND phase = 'merged'", (item,)).fetchone()
        if old:
            return int(old["id"])
        ident = create_assignment(connection, item=item, role="merge", status="done")
        connection.execute("UPDATE assignment SET phase = 'merged', ended = ?, result = ? WHERE id = ?", (now(), json.dumps(evidence, sort_keys=True), ident))
        add_note(connection, item, "comment", f"Merge verified: {evidence['url']}; whole-item delivery remains the ship receipt's decision", session="runner")
        return ident


def delivery_candidates(connection) -> list[dict]:
    """The items a push is owed, by kind and not by exception.

    An allow-list, and it used to be `kind != 'skill-review'`. That read as "a
    to-do of another kind is not agent work because it has no repository",
    which the schema does not enforce: `create_item` accepts a repository for
    any kind, so a row of another kind with a repository and a branch was a
    delivery candidate and would have been observed by the runner.
    The kinds are `RUNNABLE_KINDS`, the ones the dashboard's run panel offers,
    so the two gates agree instead of drifting apart as kinds are added. A
    skill review is left out: the panel offers it and delivery does not, and
    since sd:7004 it is a `task` whose fields say so (`skills_catalog.is_review`).
    """
    kinds = ", ".join(f"'{kind}'" for kind in RUNNABLE_KINDS)
    return [dict(row) for row in connection.execute("SELECT item.id, item.repo, item.branch, repo.remote "
        f"FROM item JOIN repo ON repo.path = item.repo WHERE item.status = 'ready_to_send' AND item.kind IN ({kinds}) "
        "AND NOT (item.source IS 'skill-request' AND json_extract(item.fields, '$.skill_review.action') IS 'review') "
        "AND item.branch IS NOT NULL AND NOT EXISTS (SELECT 1 FROM assignment WHERE assignment.item = item.id "
        f"AND (status IN {OWNING} OR (role = 'merge' AND phase = 'merged')))")]


def queue_reconciliation(connection, item: int, observation: dict) -> dict:
    if observation.get("phase") != "merged" or not observation.get("head"):
        raise RunnerRefused("only a confirmed remote merge observation can queue reconciliation")
    with transaction(connection):
        rows = enqueue(connection, [item], role="merge", scope="reconcile", who="runner remote watcher")
        connection.execute("UPDATE assignment SET result = ? WHERE id = ?", (json.dumps(observation, sort_keys=True), rows[0]["id"]))
        return queue_state(connection, rows[0]["id"])


def attempt(connection, assignment: int, run: int | None = None) -> dict:
    if run is None:
        current = queue_state(connection, assignment)["run"]
    else:
        row = connection.execute("SELECT * FROM runner_run WHERE assignment = ? AND run = ?", (assignment, run)).fetchone()
        current = dict(row) if row else None
    if current is None:
        raise RunnerRefused("assignment has no such attempt")
    return current


def request_resume(connection, assignment: int, *, expected_revision: str, who) -> dict:
    with transaction(connection):
        current = queue_state(connection, assignment)
        if current["revision"] != expected_revision:
            raise RunnerRefused("assignment changed; refresh before resuming")
        run = current["run"]
        if current["status"] != "ending" or not run or run["end_step"] != "kept":
            raise RunnerRefused("resume requires a kept attempt")
        finite = current["role"] == "exec"
        detail = (f"kept command output resolved by {who}; authorization will not replay" if finite
                  else f"kept attempt resolved; fresh run requested by {who}")
        connection.execute("UPDATE runner_run SET outcome = ?, end_action = 'resume', detail = ?, journal_version = journal_version + 1, updated_at = ? WHERE id = ?",
                           (run["outcome"] if finite else "blocked", detail, now(), run["id"]))
        add_note(connection, current["item"], "comment", detail, session=who)
        return queue_state(connection, assignment)


# ------------------------------------------------- what the session leaves behind

#: The note kinds a session may leave for the runner to file. `exec` and
#: `status_change` are the runner's and the transition's own; `comment` is a
#: person's and the library's audit line (sd:3008). Item B's requirement 7
#: names these four as what a session records.
SESSION_NOTE_KINDS = ("followup", "decision", "proposal", "question")

#: The three hard stops item A names, in the words the row and the note carry.
HARD_STOPS = ("failing test", "blocking review finding open past the cap", "write outside the repository")


def record_session_notes(connection, ident: str, notes: list[dict]) -> list[int]:
    """File the notes a session left, one row each, on the run's item.

    The runner reads the session's file and hands the parsed lines here; this
    is the one writer. A note of a kind the session may not write, or with an
    empty body, is refused before any row is written, so a malformed file
    files nothing rather than half of itself. An `exec` row's session has no
    item to file against and gets an empty list.
    """
    for note in notes:
        if not isinstance(note, dict) or note.get("kind") not in SESSION_NOTE_KINDS:
            raise RunnerRefused(f"session note kind must be one of {', '.join(SESSION_NOTE_KINDS)}")
        if not isinstance(note.get("body"), str) or not note["body"].strip():
            raise RunnerRefused("session note body must be a non-empty string")
    with transaction(connection):
        run = run_state(connection, ident)
        assignment = queue_state(connection, run["assignment"])
        if assignment["item"] is None or assignment["role"] == "exec":
            return []
        return [add_note(connection, assignment["item"], note["kind"], note["body"].strip(), session=ident)
                for note in notes]


def session_cost(connection, ident: str) -> dict | None:
    row = connection.execute("SELECT * FROM cost WHERE call_id = ? AND source = 'run'", (f"session:{ident}",)).fetchone()
    return dict(row) if row else None


def record_session_cost(connection, ident: str, *, provider: str, bill: str | None,
                        tokens_in: int | None, tokens_out: int | None, usd: float | None) -> int:
    """One `run` cost row for a `start` session: the total it reported at its exit.

    Item B's open question 8, settled 2026-09-05: on a `start` entry the
    library is in no call's path, so whatever started the session -- the
    runner -- reads the total the session reports at its own exit and writes
    one `run` row carrying the assignment and the pass. No per-call row
    exists for such a session, and the row's cost is what the session said.
    `pass` is the attempt: one session is one pass. The call id is the
    attempt's too, so a second write for the same session changes nothing.
    """
    for name, value in (("tokens_in", tokens_in), ("tokens_out", tokens_out)):
        if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
            raise RunnerRefused(f"session {name} must be a non-negative integer SQLite can store")
    # `ledger._money`'s rule: SQLite stores `nan` as NULL, and `math.isfinite`
    # raises `OverflowError` on an `int` too large for a float.
    try:
        sound = usd is None or (type(usd) in (int, float) and math.isfinite(usd) and usd >= 0)
    except OverflowError:
        sound = False
    if not sound:
        raise RunnerRefused("session usd must be a finite, non-negative number")
    with transaction(connection):
        existing = session_cost(connection, ident)
        if existing:
            return existing["id"]
        run = run_state(connection, ident)
        assignment = queue_state(connection, run["assignment"])
        for table, name in (("provider", provider), ("bill", bill)):
            if name is not None and connection.execute(f"SELECT 1 FROM {table} WHERE name = ?", (name,)).fetchone() is None:
                raise RunnerRefused(f"session {table} {name!r} is not registered; the cost row needs its {table} row")
        return record_cost(connection, source="run", provider=provider, bill=bill, role=assignment["role"],
                           repo=run["repo"], assignment=assignment["id"], pass_=ident, call_id=f"session:{ident}",
                           tokens_in=tokens_in, tokens_out=tokens_out, usd=float(usd) if usd is not None else None)


def record_hard_stop(connection, ident: str, *, kind: str, evidence: str) -> int | None:
    """An open `followup` naming the stop, so the next session starts from it.

    The row's own record is `detail`, written by `begin_ending`; this is the
    note item B's brief shows. `exec` rows have no item and get None.
    """
    if kind not in HARD_STOPS:
        raise RunnerRefused(f"hard stop kind must be one of: {'; '.join(HARD_STOPS)}")
    with transaction(connection):
        run = run_state(connection, ident)
        assignment = queue_state(connection, run["assignment"])
        if assignment["item"] is None or assignment["role"] == "exec":
            return None
        body = f"hard stop: {kind}: {evidence}".strip()
        return add_note(connection, assignment["item"], "followup", body[:8000], session=ident)


def ship_receipt(connection, ident: str) -> dict:
    """The latest `sd-ship` receipt for the run's repository, branch and item, or `{}`.

    A remote that is not GitHub has no receipt to read, which is `{}` too:
    the fixture's bare remote is the ordinary case in tests.
    """
    from . import ship
    from .workflow import WorkflowError
    run = run_state(connection, ident)
    assignment = queue_state(connection, run["assignment"])
    if assignment["item"] is None:
        return {}
    remote = connection.execute("SELECT remote FROM repo WHERE path = ?", (run["repo"],)).fetchone()
    if remote is None or not remote["remote"]:
        return {}
    try:
        repository = ship._github_repository(remote["remote"])
    except WorkflowError:
        return {}
    return ship.read(connection, ship.receipt_key(repository, run["branch"], assignment["item"]))[1]


#: A git object name: forty hex characters, or sixty-four under SHA-256.
_OBJECT_NAME = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def record_check(connection, ident: str, *, head: str, tree: str, exit_code: int, argv: list[str],
                 checks: list | None, now: str | None = None) -> dict:
    """One `check` row per run: the repository check the runner ran in the clone.

    sd:495. `sd-ship prepare` runs sd-review, whose first step is the same
    `sd-check --json` the runner has just run on the same tree, so a clean
    row pays the gate twice. This is the record the lane will read instead:
    keyed by the run id, carrying the tree hash (`git rev-parse HEAD^{tree}`)
    the check ran against, so a commit amended after the check reads as a
    different tree and is checked again. Nothing reads it yet; the pack side
    is a later change.

    Whatever the runner hands over is recorded -- a failing check too -- and
    the reader decides what a pass is. A second record for the same run
    replaces the first, like the heartbeat: the row is the latest check of
    that run, not its history.
    """
    for name, value in (("head", head), ("tree", tree)):
        if not isinstance(value, str) or not _OBJECT_NAME.fullmatch(value):
            raise RunnerRefused(f"check {name} must be a git object name, not {value!r}")
    if type(exit_code) is not int:
        raise RunnerRefused("check exit_code must be an integer")
    if checks is not None and not isinstance(checks, list):
        raise RunnerRefused("check checks must be a list or None")
    recorded_at = now if now is not None else writes.now()
    body = {"head": head, "tree": tree, "exit_code": exit_code, "argv": [str(part) for part in argv],
            "checks": checks, "recorded_at": recorded_at}
    with transaction(connection):
        run_state(connection, ident)
        connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('check', ?, ?, ?) "
            "ON CONFLICT(key) WHERE kind = 'check' DO UPDATE SET timestamp = excluded.timestamp, body = excluded.body",
            (ident, recorded_at, json.dumps(body, sort_keys=True)))
    return check_record(connection, ident)


def check_record(connection, ident: str) -> dict | None:
    """The run's recorded check, or None when the runner recorded none."""
    row = connection.execute("SELECT body FROM state WHERE kind = 'check' AND key = ?", (ident,)).fetchone()
    return json.loads(row["body"]) if row else None
