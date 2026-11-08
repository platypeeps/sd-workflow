"""Retire a repo or an item row through a supported verb (sd:754).

A remove is a plan and an apply. The plan only reads. It names every row the
verb would take, every reason it must refuse, the runner journal files the
apply moves after its commit, and the files and references it leaves, and it
hashes the rows and refusal keys into the fingerprint the apply must be handed
back (`design.md` sections 1, 3 and 4). The apply (`apply`, PR 2) plans again
under `BEGIN IMMEDIATE`, files the record (`_record`, section 4.1), deletes
children first, commits, and then moves the removed runs' journal files into
quarantine (section 4, step 7). `signal_stop` is the CLI's handler set, and
`records` is how a reader finds the record of a removed row (section 4.3).

The refusal codes are the design's: G for both verbs, A1 for assignment id
reuse, I for an item, P for a repo. G4 is not a plan refusal; `check_actor`
raises it, and the plans take no actor values (review round 4, C-61). G6 is
checked after the fingerprint and is not part of it (C-64). G7 is this
module's own, from the #350 review (N-4): a child table of one of the four
parents `SCANNED` names (`item`, `repo`, `assignment`, `runner_run`; the
#399 review, sd:956) the plan has no rule for, refused on both verbs like
G1. R1-R3 are the record's size refusals (section 4.2); like G6 they are
read after the fingerprint, because the chunks are derived from the rows the
fingerprint already covers.

A plan writes no row and no file, and nothing here opens a store: a plan and
an apply read through the connection they are handed, and find the store's
directory from that connection's `main` file, never from `home`.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shlex
import signal
import sqlite3
import stat
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import operations, reporting, repos, runner_journal, workflow
from . import paths as sdpaths
from .contribution_sync import QUEUE
from .database import refuse_hub_only, transaction
from .errors import SdDbError
from .reporting import MAX_REPORT
from .sources import docs_work, index_cache, issues, register
from .writes import add_note, now, transition

#: The `sd_db.backup` module. The package binds the name `backup` to that
#: module's `run` (`sd_db/__init__.py`), so `from . import backup` and
#: `import sd_db.backup as backup` both give the function; a test patches
#: `backup.run` on the module, and the apply reads it there.
backups = importlib.import_module(f"{__package__}.backup")

#: The longest `actor` value `reporting.ingest` stores (sd:755), so no actor
#: value this module accepts can refuse inside the apply's transaction.
MAX_ACTOR = 200

#: The importers whose next run can upsert a removed row back by its
#: `(source, external_id)` key. Reported, not refused (D6). The names are the
#: source modules' own `SOURCE` constants, the `jobs/cli.py` `SOURCES`
#: enumerates, plus the `source` that `reporting.ingest` writes; a retyped
#: list drifted (the #350 review, N-1: it named `drafts`, which nothing
#: writes, and missed `docs/work`). `vault` is not here: its import is gone
#: (sd:4627), so nothing brings a removed `vault` row back.
#: `test_imported_is_every_source_module_and_the_report_writer` enumerates
#: `sources/*.py` from the filesystem, so a new importer fails it by name.
IMPORTED = (docs_work.SOURCE, register.SOURCE, issues.SOURCE, index_cache.SOURCE, "cron-report")

#: The most chunk notes one record may need (R2, `design.md` section 4.2):
#: 16 notes of `reporting.MAX_REPORT` bytes is 3.2 MB, four times the largest
#: real candidate. A guard against a mistake, not a measured limit.
MAX_RECORD_NOTES = 16

#: What a plan reserves under `MAX_REPORT` for the three manifest lines it
#: cannot know, `who:`, `reason:` and `backup:` (R3): two actor values at
#: `MAX_ACTOR` characters of four bytes each (R3 counts bytes), and a path.
MANIFEST_HEADROOM = 2 * 4 * MAX_ACTOR + 4096 + len("reason: \nwho: \nbackup: \n")

#: The four parents a runtime child-table scan covers (G7): every table the
#: plans delete rows from and `REFERENCES` names rules for. `assignment` and
#: `runner_run` rows go with their items and repo, so an unlisted child of
#: either is taken from underneath just as a child of `item` is (the #399
#: review; sd:956). Compared lower-cased, because SQLite identifiers are.
SCANNED = ("item", "repo", "assignment", "runner_run")

#: How much of one stored string a refusal quotes (the #350 review, N-5). A
#: `retained_path` of 100,001 characters used to give a 100,040-character
#: message; the `commands` list keeps the full path, the message does not.
QUOTED = 200

#: Parents first. The apply deletes in the reverse order.
INSERT_ORDER = ("repo", "repo_protection", "item", "note", "assignment", "runner_run", "runner_lease")

#: Every foreign key whose parent is `repo`, `item`, `assignment` or
#: `runner_run`, by `(child table, child column)`, with the rule that handles
#: it (`design.md` section 1). A migration that adds a child of one of the four
#: fails `test_every_foreign_key_into_the_four_parents_has_a_rule` until a rule
#: is named here and in the plans, and a store whose schema already carries
#: such a child of any of the four is refused at plan time (G7, `_guards`).
REFERENCES = {
    ("item", "repo"): "P3: refused, or removed with --with-items",
    ("repo_protection", "repo"): "removed with the repo",
    ("runner_run", "repo"): "P4 and P6: released runs removed with their items, or detached when the item moved (sd:2581)",
    ("runner_lease", "repo"): "P4: released leases removed with the repo",
    ("note", "item"): "removed with the item, each note listed",
    ("assignment", "item"): "I3: removed when done or cancelled",
    ("publication_claim", "item"): "I2: refused",
    ("publication_claim", "active_item"): "I2: refused",
    ("assignment", "after"): "I5: refused from another item",
    ("assignment", "parent"): "I5: refused from another item",
    ("cost", "assignment"): "I4: refused",
    ("runner_run", "assignment"): "I6: released runs removed",
    ("runner_lease", "run"): "I6: released leases removed",
    ("vote", "repo"): "P7: refused while a vote names the repo (pack sd:8050)",
    ("vote", "item"): "I11: refused while a vote links the item (pack sd:8050)",
}

#: The one record-marker predicate, shared with sd:755 (C-19). It tests that
#: the key exists, so a JSON `null` marker counts as a record.
RECORD_MARKER = "CASE WHEN json_valid(fields) THEN json_type(fields,'$.record') END IS NOT NULL"

SHAPE = "retained path has an unexpected shape"


class RemovalRefused(workflow.WorkflowError):
    """A remove this module will not plan or apply, and why."""


class RemovalInterrupted(SdDbError):
    """A stop request seen at one of the apply's checks, all before the commit."""


def _one_line(value) -> bool:
    """No control character and no Unicode line or paragraph separator.

    `_record` writes `who` and `reason` into the manifest as `who: ...` and
    `reason: ...` lines, and `records` finds a row by a whole manifest line,
    so a line break in either would let an operator forge a `removed:` line
    (the #336 review). `str.splitlines` also breaks on `\\x1c`-`\\x1e`,
    `\\x85`, U+2028 and U+2029; the first three are category `Cc`, the other
    two `Zl` and `Zp`. A tab is `Cc` as well: it is refused with the rest,
    because the manifest has no use for one.
    """
    return not any(unicodedata.category(character) in ("Cc", "Zl", "Zp") for character in value)


def check_actor(*, who, reason, session, principal, program) -> None:
    """G4: the five `actor` values, checked before any preview or apply work.

    `who` and `reason` must be one line with no control character, here and
    not only in the CLI: `apply` calls this first, so a library caller cannot
    put a line break into the manifest either. `principal` and `program` must
    not be blank or `None` either (the #407 review): the record stores
    `program` as its `source_path` and `principal` beside `who` (D9a), as
    `reporting.acknowledge_clean` requires of its own. Only `session` may be
    `None`.
    """
    try:
        for name, value in (("who", who), ("reason", reason), ("principal", principal), ("program", program)):
            workflow._text(value, name)
            if len(value) > MAX_ACTOR:
                raise workflow.WorkflowError(f"{name} is longer than {MAX_ACTOR} characters")
            if name in ("who", "reason") and not _one_line(value):
                raise workflow.WorkflowError(f"{name} must be one line with no control characters")
        if session is not None and (not isinstance(session, str) or len(session) > MAX_ACTOR):
            raise workflow.WorkflowError(f"session must be text of at most {MAX_ACTOR} characters")
    except workflow.WorkflowError as error:
        raise RemovalRefused(f"G4: {error}") from None


# ---------------------------------------------------------------- reading


@contextmanager
def _snapshot(connection):
    """Every read of one plan from one snapshot, with no write lock (the #350 review).

    The connection has `isolation_level=None`, so each `SELECT` would otherwise
    read the store as it stood at that statement, and a note, lease or report
    landing between two of them gives a plan whose rows, refusals and
    fingerprint never coexisted. A `SAVEPOINT` outside a transaction opens a
    deferred one: the first read fixes the snapshot and no write lock is
    taken, so it works on a read-only connection, as `reporting.clean_reports`
    reads. Inside the apply's `BEGIN IMMEDIATE` it nests and changes nothing.
    The name is unique, so a nested plan releases only its own.
    """
    name = f"sd_removal_plan_{uuid.uuid4().hex}"
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
    finally:
        connection.execute(f"RELEASE {name}")


def _refusal(code, table, key, message, commands=()):
    return {"code": code, "table": table, "key": key, "message": message, "commands": list(commands)}


def _quoted(value) -> str:
    """Stored text as a refusal shows it: the first `QUOTED` characters, and a mark where it was cut."""
    text = str(value)
    return text if len(text) <= QUOTED else text[:QUOTED] + "..."


def _rows(connection, sql, arguments=()):
    return [dict(row) for row in connection.execute(sql, arguments)]


def _marks(values):
    return ",".join("?" * len(values))


def _store_directory(connection) -> Path | None:
    """The directory of the connection's `main` file, or `None` in memory."""
    refuse_hub_only(connection, "repo and item remove")
    for row in connection.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]).parent if row[2] else None
    return None


def _retained(run) -> tuple[str | None, Path]:
    """A run's retained path, checked before anything is read or printed from it.

    The path is database text, so its shape is checked as
    `reconciliation._paths` checks it for the runner: absolute, ending in
    `<assignment>/<run>/clone`, no symlink below the retention root, and
    resolving inside it (C-24). An absent root is an unmounted volume, not
    "no clone" (D4, option a). Returns the problem, or `None`, and the path.
    """
    text = run["retained_path"]
    path = Path(text)
    if (not path.is_absolute() or str(path) != text or ".." in path.parts
            or path.parts[-3:] != (str(run["assignment"]), str(run["run"]), "clone")):
        return f"{SHAPE}: {_quoted(text)}", path
    root = path.parents[2]
    try:
        if not root.is_dir():
            return (f"retained volume is not mounted: {_quoted(root)} is absent, so the clone {_quoted(text)}"
                    f" cannot be checked"), path
        if any(part.is_symlink() for part in (path.parents[1], path.parent, path)) or not path.resolve().is_relative_to(root.resolve()):
            return f"{SHAPE}: {_quoted(text)}", path
    except OSError as error:
        # A path the filesystem will not even answer about (`ENAMETOOLONG` at
        # 100,001 characters) is a shape problem, not an unmounted volume.
        return f"{SHAPE}: {_quoted(text)} ({error.strerror})", path
    return None, path


def _clone(run) -> tuple[str, list[str]] | None:
    """What stands in the way of a released run's retained clone, if anything.

    The runner that removed retained clones is gone (sd:3041), so no command
    is printed: the operator removes the clone by hand. The message says what
    may stay: `kept.tar`, `archives/` and `ignored/` are not the clone.
    """
    problem, path = _retained(run)
    if problem:
        return problem, []
    if not os.path.lexists(path):
        return None
    return (f"retained clone still on disk: {_quoted(path)}; remove it yourself, then plan again;"
            f" kept.tar, archives and ignored outputs of assignment {int(run['assignment'])} may stay"), []


def _journal_others(store, run) -> list[str]:
    """Entries under `runner-journal` named for `run` that are not its `.json`/`.lock` pair.

    A `.partial` is what `runner_journal.persist` leaves when it stops before
    its rename, and `runner_journal.validate_path` refuses one. It is the
    runner's to reconcile, and the move after the commit leaves it and the
    pair where they are (`_listed`). Refused at plan time too (the #350
    review), so a stray entry already there holds the apply before anything
    commits instead of leaving a journal for a run with no row; `_listed`
    still checks, for one that appears between the re-plan and the move.
    """
    if store is None:
        return []
    journal = store / "runner-journal"
    pair = {f"{run}.json", f"{run}.lock"}
    try:
        return sorted(path.name for path in journal.glob(f"{run}.*") if path.name not in pair)
    except OSError:
        return []


def _run_refusals(connection, code, runs, leases):
    """I6 for an item, P4 for a repo: the same four checks on each run."""
    refusals = []
    store = _store_directory(connection)
    for run in runs:
        if run["released_at"] is None:
            refusals.append(_refusal(code, "runner_run", run["id"], f"runner run {run['id']} is not released"))
            continue
        found = _clone(run)
        if found:
            refusals.append(_refusal(code, "runner_run", run["id"], *found))
        others = _journal_others(store, run["id"])
        if others:
            refusals.append(_refusal(code, "runner_run", run["id"],
                                     f"runner-journal holds {', '.join(_quoted(name) for name in others)} beside run"
                                     f" {run['id']}'s journal pair; the runner reconciles it before the run is removed"))
    for lease in leases:
        if lease["released_at"] is None:
            refusals.append(_refusal(code, "runner_lease", lease["run"], f"runner lease on run {lease['run']} is open"))
    return refusals


def _malformed(connection):
    """I11, store-wide: rows whose JSON the I8-I10 checks would have to read."""
    refusals = [_refusal("I11", "item", row["id"], f"item {row['id']} has fields that are not valid JSON")
                for row in connection.execute(
                    "SELECT id FROM item WHERE fields IS NOT NULL AND NOT json_valid(fields) ORDER BY id")]
    queue = connection.execute("SELECT id, body FROM state WHERE kind='checkpoint' AND key=? AND resolved_at IS NOT NULL"
                               " ORDER BY id DESC LIMIT 1", (QUEUE,)).fetchone()
    pending = []
    if queue is not None:
        try:
            body = json.loads(queue["body"])
        except (TypeError, ValueError):
            refusals.append(_refusal("I11", "state", queue["id"],
                                     f"the {QUEUE} checkpoint (state {queue['id']}) is not valid JSON"))
        else:
            if isinstance(body, dict) and isinstance(body.get("pending"), list) and all(
                    isinstance(entry, dict) for entry in body["pending"]):
                pending = body["pending"]
            else:
                refusals.append(_refusal("I11", "state", queue["id"],
                                         f"the {QUEUE} checkpoint (state {queue['id']}) has no list of pending entries"))
    return refusals, (queue["id"] if queue is not None else None), pending


def _names_item(dependency, item):
    return isinstance(dependency, dict) and dependency.get("kind") == "item" and dependency.get("item") == item


def _item(connection, item, *, queue, pending):
    """One item's rows and its I1-I10 refusals. Store-wide checks are the caller's."""
    row = connection.execute("SELECT * FROM item WHERE id=?", (item,)).fetchone()
    if row is None:
        return {"item": [], "note": [], "assignment": []}, [_refusal("I1", "item", item, f"no item {item}")]
    row = dict(row)
    notes = _rows(connection, "SELECT * FROM note WHERE item=? ORDER BY id", (item,))
    assignments = _rows(connection, "SELECT * FROM assignment WHERE item=? ORDER BY id", (item,))
    ids = [assignment["id"] for assignment in assignments]
    refusals = []

    for claim in connection.execute("SELECT id, item, active_item FROM publication_claim"
                                    " WHERE item=? OR active_item=? ORDER BY id", (item, item)):
        column = "item" if claim["item"] == item else "active_item"
        refusals.append(_refusal("I2", "publication_claim", claim["id"],
                                 f"publication claim {claim['id']} names item {item} as {column}"))
    for assignment in assignments:
        if assignment["status"] not in ("done", "cancelled"):
            refusals.append(_refusal("I3", "assignment", assignment["id"],
                                     f"assignment {assignment['id']} is {assignment['status']}"))
    if ids:
        for cost in connection.execute(f"SELECT id, assignment FROM cost WHERE assignment IN ({_marks(ids)}) ORDER BY id", ids):
            refusals.append(_refusal("I4", "cost", cost["id"], f"cost {cost['id']} is on assignment {cost['assignment']}"))
        for child in connection.execute(
                f"SELECT id, after, parent FROM assignment WHERE (item IS NULL OR item != ?)"
                f" AND (after IN ({_marks(ids)}) OR parent IN ({_marks(ids)})) ORDER BY id", (item, *ids, *ids)):
            refusals.append(_refusal("I5", "assignment", child["id"],
                                     f"assignment {child['id']} on another item waits on or was spawned by this item's work"))
    for vote in _votes(connection, "SELECT key, repo FROM vote WHERE item=? ORDER BY repo, key", (item,)):
        refusals.append(_refusal("I11", "vote", vote["key"], f"vote {vote['key']} in {_quoted(vote['repo'])} links item {item}"))
    for note in notes:
        if note["kind"] in ("followup", "question") and note["resolved_at"] is None:
            refusals.append(_refusal("I7", "note", note["id"], f"note {note['id']} is an unresolved {note['kind']}"))

    if row["repo"] and row["path"] and (row["source"] == "docs/work" or row["path"].startswith("docs/work/")) \
            and (sdpaths.expand(row["repo"]) / row["path"]).exists():
        refusals.append(_refusal("I8", "item", item, f"item {item}'s file {_quoted(sdpaths.expand(row['repo']) / row['path'])} exists"))
    if row["piece"] is not None:
        refusals.append(_refusal("I8", "item", item, f"item {item} is writing piece {_quoted(row['piece'])}"))
    for name in ("contribution", "skill_review"):
        present = connection.execute(
            f"SELECT CASE WHEN json_valid(fields) THEN json_type(fields,'$.{name}') END IS NOT NULL FROM item WHERE id=?",
            (item,)).fetchone()[0]
        if present:
            refusals.append(_refusal("I8", "item", item, f"item {item} carries fields.{name}, which a routine command reads back"))
    if connection.execute(f"SELECT {RECORD_MARKER} FROM item WHERE id=?", (item,)).fetchone()[0]:
        refusals.append(_refusal("I9", "item", item, f"item {item} is a record; a record is the only copy of what it holds"))

    for other in connection.execute(
            "SELECT id, json_extract(fields,'$.contribution.depends_on') AS depends FROM item WHERE id != ?"
            " AND CASE WHEN json_valid(fields) THEN json_type(fields,'$.contribution.depends_on') END = 'array'"
            " ORDER BY id", (item,)):
        if any(_names_item(dependency, item) for dependency in json.loads(other["depends"])):
            refusals.append(_refusal("I10", "item", other["id"], f"item {other['id']}'s contribution depends on item {item}"))
    for entry in pending:
        dependencies = entry.get("depends_on") if isinstance(entry.get("depends_on"), list) else []
        # `contribution_sync.plan` writes the item under `item` and in the key;
        # `item_id` is the projection's name for it. Any of the three names it.
        if (item in (entry.get("item"), entry.get("item_id")) or entry.get("key") == f"item:{item}"
                or any(_names_item(dependency, item) for dependency in dependencies)):
            refusals.append(_refusal("I10", "state", queue,
                                     f"the {QUEUE} checkpoint has a pending entry {_quoted(entry.get('key'))} naming item {item}"))
    return {"item": [row], "note": notes, "assignment": assignments}, refusals


def _votes(connection, sql, arguments):
    """`vote` rows, or none on a store before migration 048, which a preview may read (pack sd:8050)."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vote'").fetchone() is None:
        return []
    return connection.execute(sql, arguments).fetchall()


def _runs(connection, assignments):
    ids = [assignment["id"] for assignment in assignments]
    if not ids:
        return []
    return _rows(connection, f"SELECT * FROM runner_run WHERE assignment IN ({_marks(ids)}) ORDER BY assignment, run, id", ids)


def _leases(connection, runs):
    ids = [run["id"] for run in runs]
    if not ids:
        return []
    return _rows(connection, f"SELECT * FROM runner_lease WHERE run IN ({_marks(ids)}) ORDER BY run", ids)


def _guards(connection):
    """G1, G2 and G7."""
    refusals = [_refusal("G1", row[0], row[1], f"{row[0]} row {row[1]} already violates its foreign key to {row[2]}")
                for row in connection.execute("PRAGMA foreign_key_check")]
    refusals += [_refusal("G2", "state", row["id"], f"restore {row['id']} is unresolved")
                 for row in connection.execute("SELECT id FROM state WHERE kind='restore' AND resolved_at IS NULL ORDER BY id")]
    return refusals + _unknown_children(connection)


def _unknown_children(connection):
    """G7: a table whose foreign key points at one of `SCANNED` and has no rule in `REFERENCES`.

    The apply's foreign key check cannot see an `ON DELETE CASCADE` child: the
    delete takes its rows silently, and the plan never listed them (the #350
    review, N-4). `test_every_foreign_key_into_the_four_parents_has_a_rule`
    catches a migration in CI; this catches the store the plan is reading.
    """
    # `to` is NULL when the foreign key names the parent table alone and
    # SQLite takes its primary key. `parent` is the name as the child declared
    # it, `"ITEM"` for a child of `item`, so the match lower-cases both sides.
    return [_refusal("G7", row["child"], row["column"],
                     f"{row['child']}.{row['column']} references {row['parent']}"
                     f"{'(' + row['to'] + ')' if row['to'] else ''} and this plan has no rule for it;"
                     f" add the table to removal.REFERENCES and to the plans before removing anything")
            for row in connection.execute(
                "SELECT m.name AS child, p.\"from\" AS column, p.\"table\" AS parent, p.\"to\" AS \"to\""
                " FROM sqlite_master m JOIN pragma_foreign_key_list(m.name) p"
                f" WHERE m.type='table' AND lower(p.\"table\") IN ({_marks(SCANNED)}) ORDER BY m.name, p.id, p.seq",
                [parent.lower() for parent in SCANNED])
            if (row["child"], row["column"]) not in REFERENCES]


def _reuse(connection, assignments):
    """A1: a removed assignment id must stay below the largest surviving id (C-2, C-23)."""
    ids = [assignment["id"] for assignment in assignments]
    if not ids:
        return []
    survivor = connection.execute(f"SELECT max(id) FROM assignment WHERE id NOT IN ({_marks(ids)})", ids).fetchone()[0]
    if survivor is None:
        return [_refusal("A1", "assignment", max(ids),
                         "no assignment outside this remove survives, so a new assignment can reuse a removed id")]
    if max(ids) > survivor:
        return [_refusal("A1", "assignment", max(ids),
                         f"assignment {max(ids)} is newer than every surviving assignment ({survivor}); "
                         f"the next assignment would reuse a removed id")]
    return []


def _palette(connection, key, value):
    """Whether an assignment's `palette:` scope names this item or repo (`source:local-sd-db/sd_db/runner_exec.py::_descriptor`)."""
    return connection.execute(
        f"SELECT 1 FROM assignment WHERE substr(scope, 1, 8)='palette:' AND CASE WHEN json_valid(substr(scope, 9))"
        f" THEN json_extract(substr(scope, 9), '$.{key}') END = ? LIMIT 1",
        (value,)).fetchone() is not None


def _key(table, row):
    """The key a refusal names for a row: `id`, `path` for a repo, `run` for a lease, `repo` for a protection."""
    for column in ("id", "path", "run", "repo"):
        if column in row:
            return row[column]
    return None


def _unreadable(rows):
    """I11: a stored value the record cannot hold, such as a blob in a text column.

    The guard is the `isinstance` test below, the way I11 refuses fields that
    are not valid JSON; nothing raises. The fingerprint still covers the row:
    `_finish` canonicalises the same rows with `default=repr`, so a value
    `json.dumps` cannot encode is hashed as its `repr`.
    """
    refusals = []
    for entry in rows:
        for column, value in entry["row"].items():
            if value is not None and not isinstance(value, (str, int, float)):
                refusals.append(_refusal(
                    "I11", entry["table"], _key(entry["table"], entry["row"]),
                    f"{entry['table']} column {column} holds {type(value).__name__}, which the record cannot hold"))
    return refusals


def _left(connection, store, tables, repo=None):
    """Files and references the remove leaves (`design.md` section 1, second table)."""
    left = []

    def files(*paths):
        left.extend(f"file {path}" for path in paths if os.path.lexists(path))

    for run in tables["runner_run"]:
        problem, retained = _retained(run)
        if problem is None:
            directory = retained.parent
            files(directory / "retention.json", directory / ".archive.lock", directory / "kept.tar",
                  *sorted(directory.glob(".sd-restore-*.lock")), *sorted(directory.glob("archives/*/kept.tar")),
                  *sorted(directory.glob("archives/*/manifest.json")))
        if store is not None:
            files(store / "runner-ending" / f"{run['id']}.lock")
            receipts = store / "runner-reconciliation"
            if receipts.is_dir() and not receipts.is_symlink():
                for receipt in sorted(receipts.glob("*.json")):
                    try:
                        if not receipt.is_symlink() and json.loads(receipt.read_text()).get("run") == run["id"]:
                            left.append(f"file {receipt}")
                    except (OSError, ValueError, AttributeError):
                        continue
        files(Path(run["work_path"]).parent)
    for note in tables["note"]:
        if note["kind"] == "exec" and note["output_path"]:
            output = Path(note["output_path"])
            files(output, output.with_suffix(".receipt.json"))
    removed = {run["id"] for run in tables["runner_run"]}
    for item in tables["item"]:
        ident = item["id"]
        if _palette(connection, "item", ident):
            left.append(f"reference assignment.scope item {ident}")
        for row in connection.execute("SELECT id, key FROM state WHERE key=? ORDER BY id", (f"contribution:item:{ident}",)):
            left.append(f"reference state {row['id']} {row['key']}")
        for row in connection.execute(
                "SELECT id, key FROM state WHERE key LIKE 'ship:%' AND CASE WHEN json_valid(body)"
                " THEN json_extract(body,'$.item') END = ? ORDER BY id", (ident,)):
            left.append(f"reference state {row['id']} {row['key']}")
        for row in connection.execute(
                "SELECT id FROM item WHERE id != ? AND CASE WHEN json_valid(fields)"
                " THEN json_extract(fields,'$.completion.item') END = ? ORDER BY id", (ident, ident)):
            left.append(f"reference item {row['id']} fields.completion item {ident}")
    for run in sorted(removed):
        for row in connection.execute("SELECT id, key FROM state WHERE key=? ORDER BY id", (f"runner-delivery:{run}",)):
            left.append(f"reference state {row['id']} {row['key']}")
    if repo is not None:
        if _palette(connection, "repo", repo):
            left.append(f"reference assignment.scope repo {repo}")
        for row in connection.execute("SELECT id FROM cost WHERE repo=? ORDER BY id", (repo,)):
            left.append(f"reference cost {row['id']} repo")
        for row in connection.execute("SELECT id FROM shadow WHERE repo=? ORDER BY id", (repo,)):
            left.append(f"reference shadow {row['id']} repo")
        for row in connection.execute("SELECT id, key FROM state WHERE kind='verified' AND substr(key, 1, ?)=?"
                                      " ORDER BY id", (len(repo) + 1, repo + ":")):
            left.append(f"reference state {row['id']} {row['key']}")
        files(sdpaths.expand(repo))
    return list(dict.fromkeys(left))


def _finish(connection, kind, target, tables, refusals, *, with_items=False, repo=None, detach=()):
    """Order, hash and annotate a plan. G6 is read after the fingerprint (C-64)."""
    unique = []
    for refusal in refusals:
        if refusal not in unique:
            unique.append(refusal)
    rows = [{"table": table, "row": row} for table in INSERT_ORDER for row in tables.get(table, [])]
    for refusal in _unreadable(rows):
        if refusal not in unique:
            unique.append(refusal)
    detach = list(detach)
    canonical = json.dumps({"rows": rows, "detach": detach,
                            "refusals": sorted([r["code"], r["table"], str(r["key"])] for r in unique)},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=repr)
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    put_back = _put_back(connection, kind, fingerprint)
    if put_back is not None:
        unique.append(put_back)
    store = _store_directory(connection)
    files = []
    if store is not None:
        journal = store / "runner-journal"
        files = [str(journal / f"{run['id']}{suffix}") for run in tables.get("runner_run", [])
                 for suffix in (".json", ".lock") if os.path.lexists(journal / f"{run['id']}{suffix}")]
    warnings = [f"item {item['id']} was imported from {item['source']} as {item['external_id']}; "
                f"the next import can bring it back" for item in tables.get("item", [])
                if item["source"] in IMPORTED and item["external_id"] is not None]
    plan = {"kind": kind, "target": target, "with_items": with_items, "rows": rows, "detach": detach,
            "counts": {table: len(tables[table]) for table in INSERT_ORDER if tables.get(table)},
            "refusals": unique,
            "move": {"files": files,
                     "to": str(store / "runner-recovery-evidence" / f"removed-{fingerprint}") if files else None},
            "left": _left(connection, store, {name: tables.get(name, []) for name in INSERT_ORDER}, repo),
            "warnings": warnings, "fingerprint": fingerprint}
    # R1-R3 are derived from the rows the fingerprint covers, so like G6 they
    # follow it; the preview still lists them and the apply refuses on them.
    _, chunks, sizes = _record(plan, who=None, reason=None, backup=None)
    unique += [refusal for refusal in sizes if refusal not in unique]
    plan["record"] = {"notes": len(chunks)}
    return plan


def _put_back(connection, kind, fingerprint):
    """G6: the record of an earlier remove of these same rows, which a put-back leaves behind (C-64)."""
    record = connection.execute("SELECT id FROM item WHERE source='cron-report' AND external_id=?",
                                (f"{kind}-remove:{fingerprint}",)).fetchone()
    if record is None:
        return None
    return _refusal("G6", "item", record["id"],
                    f"a removal record for these rows exists (item {record['id']}); they were put back "
                    f"after that remove, and this verb does not remove them again")


# ---------------------------------------------------------------- the record


def _line(entry) -> str:
    """One removed row as the chunk stores it: every column, the stored value, one line.

    `default=repr` is for the plan alone: a value the record cannot hold is
    an I11 refusal (`_unreadable`), and a refused plan is never applied, so
    no chunk with a `repr` in it is ever stored.
    """
    return json.dumps({"table": entry["table"], "row": entry["row"]},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=repr)


def _header(number, total, lines) -> str:
    return f"removed rows {number} of {total}, sha256 {hashlib.sha256(chr(10).join(lines).encode()).hexdigest()}"


def _record(plan, *, who, reason, backup, note_limit=None, max_notes=None):
    """The manifest, the chunk bodies, and R1-R3 (`design.md` sections 4.1 and 4.2).

    A plan passes `who`, `reason` and `backup` as `None`: the preview cannot
    know them, so their lines are left out and R3 reserves `MANIFEST_HEADROOM`
    for them instead. The apply passes the real values, and its manifest is
    the one `reporting.ingest` stores. Rows are split only between lines,
    the `n of N` header counts the notes, and each header's sha256 is of the
    row lines that follow it, joined by newlines.
    """
    note_limit = MAX_REPORT if note_limit is None else note_limit
    max_notes = MAX_RECORD_NOTES if max_notes is None else max_notes
    kind, target = plan["kind"], plan["target"]
    refusals = []
    # The header is counted at its widest, so a chunk that fits with "1 of 1"
    # still fits once the numbers have grown.
    room = note_limit - len(f"removed rows {max_notes} of {max_notes}, sha256 {'0' * 64}") - 1
    chunks, current, size = [], [], 0
    for entry in plan["rows"]:
        line = _line(entry)
        length = len(line.encode())
        if length > room:
            refusals.append(_refusal("R1", entry["table"], _key(entry["table"], entry["row"]),
                                     f"{entry['table']} row {_quoted(_key(entry['table'], entry['row']))} is"
                                     f" {length} bytes,"
                                     f" more than one record note can hold ({note_limit})"))
            continue
        if current and size + 1 + length > room:
            chunks.append(current)
            current, size = [], 0
        current.append(line)
        size += length + (1 if size else 0)
    if current:
        chunks.append(current)
    if len(chunks) > max_notes:
        refusals.append(_refusal("R2", kind, target, f"the rows need {len(chunks)} record notes, more than "
                                                     f"{max_notes}; remove the largest items first, one at a time"))
    bodies = [chr(10).join([_header(number, len(chunks), lines), *lines]) for number, lines in enumerate(chunks, 1)]

    head = f"remove {kind} {target}" + (" --with-items" if plan["with_items"] else "")
    lines = [head]
    if reason is not None:
        lines.append(f"reason: {reason}")
    if who is not None:
        lines.append(f"who: {who}")
    lines.append(f"fingerprint: {plan['fingerprint']}")
    if backup is not None:
        lines.append(f"backup: {backup}")
    lines += [f"rows: {len(plan['rows'])} in {len(chunks)} note(s)", "removed:"]
    lines += [f"{entry['table']} {_key(entry['table'], entry['row'])}" for entry in plan["rows"]]
    if plan.get("detach"):
        # The put-back line for each detached run: the repo its row named (sd:2581).
        lines += ["detached:", *(f"runner_run {entry['run']} repo {entry['repo']}" for entry in plan["detach"])]
    if plan["move"]["files"]:
        lines += ["move after commit:", *(f"file {path}" for path in plan["move"]["files"]), f"to {plan['move']['to']}"]
    if plan["left"]:
        lines += ["left:", *plan["left"]]
    manifest = chr(10).join(lines) + chr(10)
    reserved = MANIFEST_HEADROOM if None in (who, reason, backup) else 0
    if len(manifest.encode()) + reserved > note_limit:
        refusals.append(_refusal("R3", kind, target, f"the manifest is {len(manifest.encode())} bytes, more than a "
                                                     f"report can hold ({note_limit})"))
    return manifest, bodies, refusals


# ---------------------------------------------------------------- plans


def plan_item(connection: sqlite3.Connection, item: int, *, home) -> dict:
    """What `item remove` would take and why it would refuse. Reads only, from one snapshot (`_snapshot`)."""
    with _snapshot(connection):
        return _plan_item(connection, item, home=home)


def plan_repo(connection: sqlite3.Connection, path: str, *, with_items: bool, home) -> dict:
    """What `repo remove` would take, with its items when `with_items`. Reads only, from one snapshot."""
    with _snapshot(connection):
        return _plan_repo(connection, path, with_items=with_items, home=home)


def _plan_item(connection, item, *, home):
    malformed, queue, pending = _malformed(connection)
    tables, refusals = _item(connection, item, queue=queue, pending=pending)
    runs = _runs(connection, tables["assignment"])
    leases = _leases(connection, runs)
    tables = {**tables, "runner_run": runs, "runner_lease": leases}
    refusals = _guards(connection) + refusals + _run_refusals(connection, "I6", runs, leases) \
        + _reuse(connection, tables["assignment"]) + malformed
    return _finish(connection, "item", item, tables, refusals)


def _plan_repo(connection, path, *, with_items, home):
    # `repo remove` passes the path as typed; the plan names the form the row
    # holds, so a disk path and its key plan the same removal (sd:1439).
    row = repos.row_for(connection, path) if isinstance(path, str) else None
    if row is not None:
        path = row["path"]
    if row is None:
        return _finish(connection, "repo", path, {}, [_refusal("P1", "repo", path, f"no repo {_quoted(path)}")],
                       with_items=with_items)
    row = dict(row)
    refusals = _guards(connection)
    if row["pieces_source"] == "retiring":
        refusals.append(_refusal("P2", "repo", path, f"repo {_quoted(path)} has pieces_source retiring"))
    tables = {"repo": [row], "repo_protection": _rows(connection, "SELECT * FROM repo_protection WHERE repo=?", (path,)),
              "item": [], "note": [], "assignment": []}
    refusals += [_refusal("P7", "vote", key, f"vote {key} names repo {_quoted(path)}; a vote has no other copy")
                 for (key,) in _votes(connection, "SELECT key FROM vote WHERE repo=? ORDER BY key", (path,))]
    items = [ident for (ident,) in connection.execute("SELECT id FROM item WHERE repo=? ORDER BY id", (path,))]
    malformed = []
    if items and not with_items:
        refusals += [_refusal("P3", "item", ident, f"item {ident} names repo {_quoted(path)}; pass --with-items")
                     for ident in items]
    elif items:
        malformed, queue, pending = _malformed(connection)
        for ident in items:
            part, found = _item(connection, ident, queue=queue, pending=pending)
            for table in ("item", "note", "assignment"):
                tables[table] += part[table]
            refusals += found
            item_runs = _runs(connection, part["assignment"])
            refusals += _run_refusals(connection, "I6", item_runs, _leases(connection, item_runs))
    planned = {item["id"] for item in tables["item"]}
    by_assignment = _runs(connection, tables["assignment"])
    on_repo = _rows(connection, "SELECT r.*, a.item AS plan_item, i.repo AS item_repo FROM runner_run r"
                                " LEFT JOIN assignment a ON a.id = r.assignment LEFT JOIN item i ON i.id = a.item"
                                " WHERE r.repo=? ORDER BY r.assignment, r.run, r.id", (path,))
    runs = {run["id"]: run for run in by_assignment}
    detach = []
    for run in on_repo:
        owner, home_now = run.pop("plan_item"), run.pop("item_repo")
        if owner not in planned and home_now is not None and home_now != path:
            # sd:2581: the item moved to another repo (`sd task edit --belongs-to`).
            # The run stays with it and loses only its link to this repo
            # (migration 018); P4 below still holds it to a released run with
            # no clone on disk, and the record names the repo it had.
            detach.append({"run": run["id"], "repo": path, "item": owner, "item_repo": home_now})
            continue
        if owner not in planned:
            refusals.append(_refusal("P6", "runner_run", run["id"],
                                     f"runner run {run['id']} on this repo belongs to assignment {run['assignment']}, "
                                     f"whose item is not in this remove"))
        runs.setdefault(run["id"], run)
    runs = sorted(runs.values(), key=lambda run: (run["assignment"], run["run"], run["id"]))
    leases = {lease["run"]: lease for lease in _leases(connection, runs)}
    for lease in _rows(connection, "SELECT * FROM runner_lease WHERE repo=?", (path,)):
        leases.setdefault(lease["run"], lease)
    order = {run["id"]: index for index, run in enumerate(runs)}
    leases = sorted(leases.values(), key=lambda lease: (order.get(lease["run"], len(order)), lease["run"]))
    refusals += _run_refusals(connection, "P4", on_repo, [lease for lease in leases if lease["repo"] == path])
    try:
        checkouts = repos.checkouts(root=Path(os.environ["SD_REPO_ROOT"]).expanduser()
                                    if os.environ.get("SD_REPO_ROOT") else Path(home) / "repos")
    except FileNotFoundError:
        checkouts = []
    except (repos.RepoRefusal, OSError, UnicodeError) as error:
        refusals.append(_refusal("P5", "repo", path, f"the repo-sync conf cannot be read, so a seed may add it back: {error}"))
        checkouts = []
    for checkout in checkouts:
        if checkout.present and sdpaths.same(checkout.path, path):
            refusals.append(_refusal("P5", "repo", path, f"the repo-sync conf names {checkout.slug}, checked out at "
                                                         f"{checkout.path}; the next seed adds the repo back"))
    tables = {**tables, "runner_run": runs, "runner_lease": leases}
    refusals += _reuse(connection, tables["assignment"]) + malformed
    return _finish(connection, "repo", path, tables, refusals, with_items=with_items, repo=path, detach=detach)


# ---------------------------------------------------------------- the apply


def records(connection: sqlite3.Connection, table: str, key) -> list[int]:
    """The removal records whose manifest lists `<table> <key>` (`design.md` section 4.3). Reads only."""
    return [row["id"] for row in connection.execute(
        "SELECT id FROM item WHERE kind='report'"
        " AND CASE WHEN json_valid(fields) THEN json_extract(fields, '$.record') END IN ('item-remove', 'repo-remove')"
        " AND instr(char(10) || CASE WHEN json_valid(body) THEN json_extract(body, '$.text') END || char(10),"
        "           char(10) || ? || char(10)) > 0 ORDER BY id", (f"{table} {key}",))]


@contextmanager
def signal_stop():
    """Handlers for SIGINT, SIGTERM and SIGHUP that only raise a flag; yields the `stop` callable.

    The flag is a one-element list, not a `threading.Event`: `Event.set`
    takes a non-reentrant lock, and a second signal handled inside that lock
    blocks forever (review round 5, C-65). The handlers never raise; `apply`
    reads the flag at its three checks before the commit and never after it
    (C-66). The old handlers come back in `finally`, on every exit (C-69).
    `signal.signal` works in the main thread alone, and the CLI runs there.
    """
    flag = [False]

    def handler(signum, frame):
        flag[0] = True

    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, handler)
        yield lambda: flag[0]
    finally:
        for signum, old in previous.items():
            signal.signal(signum, old)


def _check(stop):
    if stop():
        raise RemovalInterrupted("interrupted before the commit; nothing was removed")


def _main_file(connection) -> Path | None:
    refuse_hub_only(connection, "repo and item remove")
    for row in connection.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


def _refused(plan) -> None:
    if plan["refusals"]:
        raise RemovalRefused("; ".join(f"{r['code']}: {r['message']}" for r in plan["refusals"]))


def _plan(connection, kind, target, *, fingerprint, home, with_items):
    """A fresh plan, refused on any refusal and on a fingerprint the preview did not give (G3)."""
    if kind == "item":
        plan = plan_item(connection, target, home=home)
    elif kind == "repo":
        plan = plan_repo(connection, target, with_items=with_items, home=home)
    else:
        raise RemovalRefused(f"unknown plan kind {kind!r}; the kinds are item and repo")
    _refused(plan)
    if plan["fingerprint"] != fingerprint:
        raise RemovalRefused(f"G3: the store changed since the preview (fingerprint {plan['fingerprint'][:12]}..., "
                             f"not {str(fingerprint)[:12]}...); preview again")
    return plan


def _delete(connection, plan) -> None:
    """Children first, each statement on the plan's explicit keys; every assignment in one statement (C-8)."""
    keys = {table: [_key(table, entry["row"]) for entry in plan["rows"] if entry["table"] == table]
            for table in INSERT_ORDER}
    columns = {"repo": "path", "repo_protection": "repo", "runner_lease": "run"}
    for table in reversed(INSERT_ORDER):
        if not keys[table]:
            continue
        column = columns.get(table, "id")
        taken = connection.execute(f"DELETE FROM {table} WHERE {column} IN ({_marks(keys[table])})", keys[table]).rowcount
        if taken != len(keys[table]):
            raise RemovalRefused(f"{table}: the plan named {len(keys[table])} rows and the delete took {taken}")


def _detach(connection, plan) -> None:
    """Move each run the plan detaches from `repo` to `detached_from` (sd:2581), and nothing else.

    `detached_from` is the provenance `runner_journal.against` checks: the
    journal is not rewritten, since a file write cannot roll back with this
    transaction, and it keeps naming that repository. `journal_version` and
    `updated_at` stay, because they say which journal record the row matches,
    and that record is unchanged; a bump with no journal write is a row newer
    than its journal, which the nightly backup refuses. A detached row cannot
    be detached again: its `repo` no longer matches.
    """
    for entry in plan.get("detach", []):
        taken = connection.execute("UPDATE runner_run SET repo = NULL, detached_from = repo"
                                   " WHERE id = ? AND repo = ? AND released_at IS NOT NULL AND detached_from IS NULL",
                                   (entry["run"], entry["repo"])).rowcount
        if taken != 1:
            raise RemovalRefused(f"runner_run {entry['run']}: the plan detached it from {entry['repo']} and the update took {taken}")


def _regular(path: Path, what: str) -> None:
    """Step 7.2 and 7.3: a regular file, not a symlink, owned by the user, with one link."""
    details = os.lstat(path)
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_nlink != 1:
        raise RemovalRefused(f"{what} {path} is not a regular file owned by this user with one link")


def _private(path: Path) -> None:
    """An existing `runner-recovery-evidence`, as `reconciliation._private_directory` takes it."""
    details = os.lstat(path)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise RemovalRefused(f"recovery directory must be private, owned and unlinked: {path}")


def _commands(files, to, moved) -> list[str]:
    """The operator's lines for a move that stopped (`design.md` section 4, step 7; C-58, C-62, C-70).

    No lines when there is nothing to move, and none when the evidence or
    quarantine path exists as anything but a private directory of the
    user's own: a link there would carry the files outside the store and a
    shared directory would show them (Copilot on #407, and its residue), and
    `move_error` already names it.
    """
    if not files or to is None:
        return []
    to = Path(to)
    lines = []
    for path in (to.parent, to):
        if not os.path.lexists(path):
            lines.append(f"mkdir -m 700 {shlex.quote(str(path))}")
        else:
            try:
                _private(path)
            except (RemovalRefused, OSError):
                return []
    for source in files:
        if source in moved:
            continue
        quoted = shlex.quote(source)
        lines.append(f"mv -n {quoted} {shlex.quote(str(to / Path(source).name))}"
                     f" && test ! -e {quoted} && test ! -L {quoted}")
    return lines


def _listed(store, plan) -> tuple[list[str], str | None]:
    """Step 7's listing: each removed run's journal pair, read under its ending lock, one lock at a time.

    `runner-ending/<run>.lock` is taken, the pair listed, and the lock let
    go before the next run's is taken, in plan order. A pair `Runner.finish`
    persisted after the re-plan is found that way, and a run whose pair is
    not there still waits for its ending sequence (the #407 residue). Held
    together, the locks were one fd per removed run for the whole move, and
    a repo with more runs than `RLIMIT_NOFILE` admits met `EMFILE` after
    the commit (sd:968). Once a run's lock has been held and released here,
    nothing persists a pair for it again: its row is gone, and the ending
    sequence and the archive refresh both read `run_state` first, under
    that lock, and refuse an absent run. So the move takes the journal
    lock alone. Returns the files and the quarantine path, `None` when
    there is nothing to move; raises what the lock or the check raises.
    """
    journal = store / "runner-journal"
    files = []
    for entry in plan["rows"]:
        if entry["table"] != "runner_run":
            continue
        run = entry["row"]["id"]
        ending = store / "runner-ending" / f"{run}.lock"
        pair = [journal / f"{run}.json", journal / f"{run}.lock"]
        with runner_journal.lock(
                ending, blocking=False, noun="runner ending", error=RemovalRefused,
                held=f"runner-ending lock is held: {ending}; an ending sequence or an archive refresh of run"
                     f" {run} holds it, and the pair stays until it lets go"):
            # Note 1966, Q3: a `.partial` or any other `<run>.*` entry is
            # the runner's to reconcile; moving the pair around it would
            # leave `validate_path`'s refusal behind for a run with no row.
            others = sorted(path.name for path in journal.glob(f"{run}.*") if path not in pair)
            if others:
                raise RemovalRefused(f"runner-journal holds {', '.join(others)} beside run {run}'s journal pair;"
                                     f" reconcile it before the pair is moved")
            files.extend(str(path) for path in pair if os.path.lexists(path))
    return files, str(store / "runner-recovery-evidence" / f"removed-{plan['fingerprint']}") if files else None


def _move(store, plan, record) -> tuple[list[str], str | None, list[str]]:
    """Step 7: each removed run's journal pair into `removed-<fingerprint>/`, after the commit.

    The pairs are what `_listed` finds under the ending locks, not what the
    plan named; each is then moved under its own journal lock. Any failure
    stops the move and is returned, never raised: a held lock, a failed
    check, an `OSError`, a `KeyboardInterrupt` (C-48). The rows are already
    gone, so the caller prints the reason and the lines `_commands` gives;
    until the listing is whole those lines name the plan's files.
    """
    if not any(entry["table"] == "runner_run" for entry in plan["rows"]):
        return [], None, []
    journal = store / "runner-journal"
    evidence = store / "runner-recovery-evidence"
    quarantine = evidence / f"removed-{plan['fingerprint']}"
    files, to = list(plan["move"]["files"]), plan["move"]["to"]
    moved = []
    try:
        files, to = _listed(store, plan)
        if not files:
            return [], None, []
        present = {}
        for source in files:
            present.setdefault(Path(source).stem, []).append(Path(source))
        prepared = False
        for run, sources in present.items():
            pair = [journal / f"{run}.json", journal / f"{run}.lock"]
            if os.path.lexists(pair[1]):
                _regular(pair[1], "journal lock")
            with runner_journal.lock(pair[1], blocking=False, noun="run journal", error=RemovalRefused,
                                     held=f"run journal lock is held: {pair[1]}"):
                if os.path.lexists(pair[0]):
                    _regular(pair[0], "journal record")
                if not prepared:
                    if os.path.lexists(evidence):
                        _private(evidence)
                    else:
                        os.mkdir(evidence, 0o700)
                    try:
                        os.mkdir(quarantine, 0o700)
                    except FileExistsError:
                        raise RemovalRefused(f"quarantine directory exists: {quarantine}; nothing in it is"
                                             f" overwritten") from None
                    receipt = {"record": record, "fingerprint": plan["fingerprint"],
                               "files": [{"source": source, "sha256": hashlib.sha256(
                                   Path(source).read_bytes()).hexdigest() if os.path.isfile(source)
                                   and not os.path.islink(source) else None} for source in files]}
                    with open(quarantine / "receipt.json", "x") as output:
                        os.fchmod(output.fileno(), 0o600)
                        json.dump(receipt, output, sort_keys=True, indent=2)
                        output.flush()
                        os.fsync(output.fileno())
                    prepared = True
                for source in sources:
                    target = quarantine / source.name
                    if os.path.lexists(target):
                        raise RemovalRefused(f"{target} exists; nothing in the quarantine is overwritten")
                    os.rename(source, target)
                    moved.append(str(source))
                if pair[1] not in sources:
                    # `lock` created this file; the receipt does not know
                    # it, so it goes, still under the flock (Copilot on #407).
                    os.unlink(pair[1])
                for directory in (quarantine, evidence, journal):
                    runner_journal.fsync_directory(directory)
    except BaseException as error:
        return moved, str(error) or type(error).__name__, _commands(files, to, moved)
    return moved, None, []


def apply(connection: sqlite3.Connection, plan_kind: str, target, *, fingerprint, who, reason, principal,
          program, home, with_items=False, session=None, stop=None) -> dict:
    """Remove what the preview planned, record it, and move the journal files (`design.md` section 4).

    The order is the design's: G4, G5 for a connection with no file, the
    read-only plan and G3, `stop()`, `control_gate`, the backup and G5,
    `stop()`, one `BEGIN IMMEDIATE` transaction that plans again (G3), files
    the record, deletes children first, checks foreign keys and reads
    `stop()` a last time, the commit, and then the move, which never raises
    and is never stopped (C-66). `stop` is the callable `signal_stop` yields;
    a true answer raises `RemovalInterrupted` and, inside the transaction,
    rolls it back (C-57).

    Around the transaction, only `KeyboardInterrupt` is caught (C-63). It is
    treated as a commit that landed only when this call's `ingest` returned
    an id, no transaction is open, and a row with that id and the record's
    `external_id` is there; then the move is skipped and its lines returned.
    Every other exception, and every refusal, propagates.
    """
    check_actor(who=who, reason=reason, session=session, principal=principal, program=program)
    database = _main_file(connection)
    if database is None:
        raise RemovalRefused("G5: the apply needs a file-backed store to back up; this connection has no main file")
    stop = stop or (lambda: False)
    started = now()
    plan = _plan(connection, plan_kind, target, fingerprint=fingerprint, home=home, with_items=with_items)
    _check(stop)
    external = f"{plan_kind}-remove:{fingerprint}"
    with operations.control_gate(connection):
        try:
            snapshot = backups.run(home=home, database=database, keep=None)
        except Exception as error:
            raise RemovalRefused(f"G5: the backup did not complete: {error}") from error
        if snapshot.violations:
            raise RemovalRefused(f"G5: the backup snapshot reports {len(snapshot.violations)} foreign key"
                                 f" violation(s); nothing was removed")
        _check(stop)
        record, chunks = None, []
        try:
            with transaction(connection):
                plan = _plan(connection, plan_kind, target, fingerprint=fingerprint, home=home, with_items=with_items)
                manifest, chunks, oversize = _record(plan, who=who, reason=reason, backup=str(snapshot.directory))
                if oversize:
                    raise RemovalRefused("; ".join(f"{r['code']}: {r['message']}" for r in oversize))
                actor = {"who": who, "principal": principal, "program": program, "pid": os.getpid(),
                         "ppid": os.getppid(), "session": session, "reason": reason}
                state = reporting.ingest(connection, job=f"{plan_kind}-remove", run_id=fingerprint, started=started,
                                         ended=now(), exit_code=0, text=manifest, source_path=program,
                                         attention=False, removed=plan["counts"], actor=actor,
                                         record=f"{plan_kind}-remove")
                record = state["item"]["id"]
                for chunk in chunks:
                    add_note(connection, record, "comment", chunk, session=who)
                transition(connection, record, "done", who=who, reason="removal record")
                _detach(connection, plan)
                _delete(connection, plan)
                violations = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
                if violations:
                    raise RemovalRefused(f"the deletes left {len(violations)} foreign key violation(s), "
                                         f"{violations[0][0]} row {violations[0][1]} first; nothing was removed")
                _check(stop)
        except KeyboardInterrupt:
            if record is not None and not connection.in_transaction and connection.execute(
                    "SELECT 1 FROM item WHERE id=? AND source='cron-report' AND external_id=?",
                    (record, external)).fetchone():
                # The lines name what the ending locks list, as the move's
                # do, so a pair persisted after the re-plan gets one (sd:968);
                # when the listing itself fails, they name the plan's files.
                try:
                    files, to = _listed(database.parent, plan)
                except BaseException:
                    files, to = plan["move"]["files"], plan["move"]["to"]
                return {"record": record, "notes": len(chunks), "removed": plan["counts"],
                        "backup": str(snapshot.directory), "moved": [], "move_error": "interrupted",
                        "move_commands": _commands(files, to, [])}
            raise
        moved, error, commands = _move(database.parent, plan, record)
    return {"record": record, "notes": len(chunks), "removed": plan["counts"], "backup": str(snapshot.directory),
            "detached": [entry["run"] for entry in plan.get("detach", [])],
            "moved": moved, "move_error": error, "move_commands": commands}
