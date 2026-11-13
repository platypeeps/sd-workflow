"""The nightly row prune: what ages out of the database, after a backup passed.

One retention table, read by one prune that `sd-db backup` runs after
its snapshot restored and compared, and never before. What the table says:

    cost rows        never.  The ledger: an assignment's budget is admitted
                     by summing its rows and the item screen reports an item
                     by them, so a hole in it is a budget spent twice or a
                     cost understated (prd C-70).
    exec notes       never.  The audit record of what the palette ran --
                     identity, entry, arguments, exit code -- and it leaves
                     only with its item (prd C-66).
    exec output      ninety days.  The output file under `<db>/executions/`
                     is removed and the note is marked `output expired`; the
                     palette then says so instead of reading nothing.
    heartbeat rows   one per key.  Schema 005 already keeps `runner` unique
                     with a partial index; every other key accumulates a row
                     per tick, and the newest is the only one any reader asks
                     for. A `session:` row older than `sessions.STALE` goes
                     whole: that session ended without its hook (sd:6024).
    checkpoint rows  archived, then dropped, by three rules (sd:3472, sd:8655).
                     `NEWEST_ONLY_CHECKPOINTS`: one row per key, once the
                     key sat a week. Every reader takes the newest row
                     (`ship.read`, `LIMIT 1`, `MAX(id)`), yet `ship.save`
                     appends one before each side effect: 24k `ship:` rows
                     held 700 MB on 2026-10-12. A key whose newest row is
                     younger than `CHECKPOINT_IDLE_AGE` keeps every row, so
                     a live ship's phases stay readable.
                     `NEWEST_AND_RESOLVED_CHECKPOINTS`: the newest row and
                     the newest resolved row, at any age: `contribution_sync`
                     and `removal` read only the newest resolved one, and
                     the one key held 408 rows, 305 MB, on 2026-11-13.
                     `FIRST_AND_NEWEST_CHECKPOINTS`: the first row and the
                     newest, once the newest is older than `LANE_IDLE_AGE`:
                     `sd_lane.enqueue_order` reads the first as the queue
                     order, the lane reads the newest, and `entry_times`
                     reads the rows between only inside its 7-day window.
                     Each dropped row goes first to a gzip JSON-lines file
                     under `<bulk root>/sd-workflow/db-archive/`, read back
                     and matched row for row; no bulk root, or an archive
                     that fails, drops none. `KEPT_CHECKPOINTS` names every
                     other checkpoint key a writer uses, and why it stays.
    clean reports    seven days in `planning`, then `done`.  `reporting.ingest`
                     opens every report as `planning`; an attention report
                     carries a followup and waits for a person, a clean one
                     carries nothing and waited for nobody -- 127 of them sat
                     in `planning` on the live database on 2026-09-12. The
                     row stays; only its status moves, by the transition
                     `acknowledge` uses, as `retention`.

                     Since sd:739 there are far fewer of them to settle: a
                     quiet tick through `reporting.ingest_log` writes a
                     heartbeat instead of an item, and since sd:3509 a
                     recovery does too, so that path files no clean report.
                     It is `ingest_log` that changed, not `ingest`: a
                     caller that files a report directly still files one
                     per run, and `prune` below is such a caller -- its own
                     successful report is clean.  This rule is unchanged
                     and still needed -- direct callers keep arriving, and
                     rows predating those changes are still here.
    backups          thirty files -- `backup --keep N`, not here. The
                     scheduled job runs `--keep all` by the September 9
                     amendment, and this module does not touch it. The
                     hourly job keeps a rolling week with `--keep-days 7`
                     and skips this prune with `--no-row-prune`, so it
                     files no report item.
    free pages       a VACUUM once `VACUUM_FREE_BYTES` are free, by the
                     backup job after this prune, never inside it.
    kept worktrees   never.  A kept clone may hold work; this prune does not
                     touch it.
    untouched P4     thirty days, then parked (sd:3007).  An open priority-4
                     item whose newest `updated_at` or note is older than the
                     age gets `parked_at` and a `comment` note by
                     `retention`. Parked items leave Today and the backlog and
                     stay readable by id and in the capture list. Writing
                     pieces keep their own park; an item with a due date
                     waits on it; P1-P3 are never touched.
                     `workflow.edit_item` clears `parked_at` to unpark one,
                     and sets it to park an item by hand (sd:5905).
    idle planning    ninety days, then parked (sd:6008).  An open item in
                     `planning` that never ran -- no `assignment` row --
                     parks by the same touch rule, any priority, with its
                     own note. An item that ran is left to its run's
                     outcome. Reports and followups wait for a person, and
                     a recurring item, like a dated one, waits on its date.

There is no request-log rule, and the prd's table no longer names one (it
said "the request log thirty days" until the 2026-09-12 amendment, sd:541).
There is no such store: the dashboard silences its HTTP log (`server.py`'s
`log_message`) and writes no request rows; the `operator-action` checkpoint
rows are accepted mutations `reporting.metrics` counts by week, not a
request log. Should a store ever be written, its rule goes in the prd's
table first and here second.

The prune writes one `report` item with the counts it removed, so the
Operations screen shows what the night took, and it refuses to run against
a backup that did not pass: not the caller's word for it, but the dated
directory verified again and its checkpoint row found in this database.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import reporting, sessions
from .backup import Snapshot, passed
from .database import refuse_hub_only, transaction
from .errors import SdDbError
from .reads import NOT_A_DOCUMENT
from .repos import item_refs
from .writes import add_note, set_item_fields, transition

#: How long an `exec` note keeps its output file.
EXEC_OUTPUT_AGE = timedelta(days=90)

#: How long a checkpoint key goes unwritten before its older rows go.
CHECKPOINT_IDLE_AGE = timedelta(days=7)

#: Checkpoint key prefixes whose every reader takes the newest row per key.
#: All but the last two are written through `ship.save` and read through
#: `ship.read`.
NEWEST_ONLY_CHECKPOINTS = (
    "ship:", "ship-review-no-item:", "ship-review-no-item-index:", "ship-adjudication:",
    "ship-adjudication-no-item:", "sd-gate-receipt:", "sd-check-receipt:", "contribution:", "provider-orders:",
    "review:", "jira-moves",
)

#: Checkpoint keys, whole keys and not prefixes, whose readers take the newest
#: row and the newest resolved row: `contribution_sync._queue` and
#: `removal._malformed`, `resolved_at IS NOT NULL ORDER BY id DESC LIMIT 1`.
#: The rows between go at any age.
NEWEST_AND_RESOLVED_CHECKPOINTS = ("contribution-refresh-queue",)

#: Checkpoint key prefixes whose readers take each key's first row and its
#: newest: `sd_lane.enqueue_order`, `today` and `build` read `MIN(id)`, the
#: lane and `reads.lane_entries` read `MAX(id)`.
FIRST_AND_NEWEST_CHECKPOINTS = ("lane:",)

#: How long a `FIRST_AND_NEWEST_CHECKPOINTS` key goes unwritten before its
#: middle rows go: far past `sd_lane.TIMES_DAYS`, the window in which
#: `entry_times` reads them.
LANE_IDLE_AGE = timedelta(days=30)

#: Checkpoint keys retention never compacts, and why. A backup run id has no
#: prefix: it is 32 hex digits, one row per run.
KEPT_CHECKPOINTS = {
    "operator-action": "`reporting.metrics` counts every row by week",
    "operations:": "each row is a request or a cancel the operator made",
    "<backup run id>": "one row per run; `backup.passed` finds it by key",
}

#: Where, under the bulk root, the prune writes the rows it drops.
ARCHIVE_FOLDER = Path("sd-workflow") / "db-archive"

#: How many ids one statement names: far under SQLite's variable limit.
CHUNK = 500

#: The free pages, in bytes, that make `vacuum` run.
VACUUM_FREE_BYTES = 50 * 1024 * 1024

#: How long, in milliseconds, `vacuum` waits for another writer's transaction.
VACUUM_BUSY_MS = 60_000

#: How long a clean run report waits in `planning` for an acknowledgement
#: before retention settles it.
CLEAN_REPORT_AGE = timedelta(days=7)

#: How long an open P4 item may go untouched before retention parks it.
P4_PARK_AGE = timedelta(days=30)

#: How long a `planning` item that never ran may go untouched before retention parks it.
PLANNING_PARK_AGE = timedelta(days=90)

#: The `who` on the status_change note a settled report carries.
RETENTION = "retention"

#: The key the note's body carries once its output is gone. The body is the
#: execution descriptor; `verify_descriptor` refuses a descriptor with an
#: extra key, which is right: an expired execution is never run again.
OUTPUT_EXPIRED = "output_expired"

#: The job name the report row is filed under.
JOB = "sd-db-prune"

#: How many ids of `planning` reports with unreadable `fields` the prune
#: report names: all of them up to this many in its text, far inside
#: `reporting.MAX_REPORT`, and fewer in the attention basis, which `ingest`
#: refuses past 2000 characters -- about 280 four-digit ids. A refused prune
#: report would fail the backup job every night after the expiry, compaction
#: and settle had already committed.
UNREADABLE_TEXT_IDS = 1000
UNREADABLE_BASIS_IDS = 20

#: How much of an archive fault the attention basis carries, beside up to
#: `UNREADABLE_BASIS_IDS` ids, inside `ingest`'s 2000 characters.
ARCHIVE_BASIS = 600

LOG_NAME = re.compile(r"[a-f0-9]{32}\.log")


class RetentionRefused(SdDbError):
    """The prune did not run, and nothing was removed."""


class ArchiveFailed(SdDbError):
    """The archive was not written or did not read back; no checkpoint row was deleted."""


class VacuumStageFailed(SdDbError):
    """The VACUUM ran, or was not due, and a later stage failed; the message names both."""


@dataclass(frozen=True)
class Pruned:
    """What one night's prune removed, and the report row that says so."""

    exec_outputs: int
    heartbeats: int
    clean_reports: int
    report: int
    parked: int = 0
    checkpoints: int = 0
    #: The checkpoint step's archive and its reason for keeping rows; not a count.
    compacted: Compacted | None = None

    @property
    def counts(self) -> dict[str, int]:
        return {"exec_outputs": self.exec_outputs, "heartbeats": self.heartbeats,
                "checkpoints": self.checkpoints, "clean_reports": self.clean_reports, "parked": self.parked}

    def __str__(self) -> str:
        return (f"{self.exec_outputs} exec output(s) expired, "
                f"{self.heartbeats} stale heartbeat row(s) removed, "
                f"{self.checkpoints} superseded checkpoint row(s) removed, "
                f"{self.clean_reports} clean report(s) settled, "
                f"{self.parked} untouched item(s) parked")


def _cutoff(now: datetime, age: timedelta) -> str:
    return (now - age).astimezone(UTC).isoformat(timespec="seconds")


def _output_files(connection, row) -> list[Path]:
    """The note's log and its receipt, only where the note says they are.

    A path that spells `<db>/executions` is not inside it when `executions`
    is itself a symlink onto somewhere else. The textual comparison passed
    that, and `_unlink` rejects a symlinked *file* but never looked at the
    directory above it, so a replaced `executions` made the prune delete
    through the link. So the component is checked as well as the spelling.

    Only that component. Resolving both sides would not do: a symlinked
    `executions` resolves to the same place from either side and compares
    equal to itself. And a link above it, in the database's own directory,
    is the operator's choice of where the store lives, not a redirection of
    what the prune may delete.
    """
    refuse_hub_only(connection, "the executions prune")
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    executions = database.parent / "executions"
    path = Path(row["output_path"] or "")
    if path.parent != executions or executions.is_symlink() or not LOG_NAME.fullmatch(path.name):
        raise RetentionRefused(f"exec note {row['id']} names output outside the executions directory: {path}")
    return [path, path.with_suffix(".receipt.json")]


def _unlink(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


def expire_exec_outputs(connection, *, now: datetime) -> int:
    """Remove output files older than `EXEC_OUTPUT_AGE`; keep every note.

    The mark is written first and the file removed second: a marked note
    whose file is still on disk is harmless (the next prune removes it), a
    removed file whose note is unmarked would fail the next backup's
    evidence check. An unfinished note is never touched, however old --
    `reconcile` needs its file to decide what happened.
    """
    cutoff = _cutoff(now, EXEC_OUTPUT_AGE)
    rows = connection.execute(
        "SELECT id, body, output_path FROM note WHERE kind='exec' AND ended IS NOT NULL AND ended < ? ORDER BY id",
        (cutoff,)).fetchall()
    stamp = now.astimezone(UTC).isoformat(timespec="seconds")
    files: list[Path] = []
    with transaction(connection):
        for row in rows:
            try:
                value = json.loads(row["body"])
            except ValueError:
                continue
            if not isinstance(value, dict) or value.get("note") != row["id"]:
                continue
            files.extend(_output_files(connection, row))
            if OUTPUT_EXPIRED not in value:
                value[OUTPUT_EXPIRED] = stamp
                connection.execute("UPDATE note SET body=? WHERE id=?", (json.dumps(value, sort_keys=True), row["id"]))
    removed = 0
    for path in files:
        if _unlink(path) and path.suffix == ".log":
            removed += 1
    return removed


def compact_heartbeats(connection, *, now: datetime | None = None) -> int:
    """One `heartbeat` row per key: the one every reader orders first; with `now`, no stale session row."""
    with transaction(connection):
        cursor = connection.execute(
            "DELETE FROM state WHERE kind='heartbeat' AND id NOT IN ("
            "  SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY key ORDER BY timestamp DESC, id DESC) AS rank"
            "                  FROM state WHERE kind='heartbeat') WHERE rank = 1)")
    stale = sessions.expire(connection, now=now.astimezone(UTC).isoformat(timespec="seconds")) if now else 0
    return cursor.rowcount + stale


def _prefixed(prefixes: tuple[str, ...]) -> tuple[str, list]:
    """A predicate true for a key that starts with one of `prefixes`, and its parameters.

    `substr`, not `LIKE`: `LIKE` folds case and reads `_` as a wildcard.
    """
    return (" OR ".join("substr(key, 1, ?) = ?" for _ in prefixes),
            [value for prefix in prefixes for value in (len(prefix), prefix)])


def checkpoint_victims(connection, *, now: datetime) -> list[int]:
    """The checkpoint rows the three rules drop, by id, oldest first.

    Newest is the highest id, the row `ship.read` takes, so the revision a
    `ship.save` compares against is never one of them. The age is the
    key's newest timestamp, as it always was.
    """
    return [row[0] for row in _victims(connection, now=now)]


def _victims(connection, *, now: datetime) -> list[tuple[int, int, int]]:
    """Each `checkpoint_victims` row as (id, its key's first id, its key's newest id), from one read.

    This read takes `resolved_at` from every checkpoint row, and that
    column sits after `body`, so it walks each body's overflow pages: run
    it outside the write lock. `_still_due` re-checks the bounds under it.
    """
    newest, newest_args = _prefixed(NEWEST_ONLY_CHECKPOINTS)
    queue, queue_args = f"key IN ({', '.join('?' for _ in NEWEST_AND_RESOLVED_CHECKPOINTS)})", [
        *NEWEST_AND_RESOLVED_CHECKPOINTS]
    lane, lane_args = _prefixed(FIRST_AND_NEWEST_CHECKPOINTS)
    sql = (
        "SELECT id, first, last FROM (SELECT id, rule, ROW_NUMBER() OVER (PARTITION BY key ORDER BY id DESC) AS rank,"
        "  MIN(id) OVER (PARTITION BY key) AS first, MAX(id) OVER (PARTITION BY key) AS last,"
        "  MAX(timestamp) OVER (PARTITION BY key) AS newest,"
        "  MAX(CASE WHEN resolved_at IS NOT NULL THEN id END) OVER (PARTITION BY key) AS resolved"
        f" FROM (SELECT id, key, timestamp, resolved_at, CASE WHEN {newest} THEN 1 WHEN {queue} THEN 2"
        f"  WHEN {lane} THEN 3 END AS rule FROM state WHERE kind='checkpoint') WHERE rule IS NOT NULL)"
        " WHERE rank > 1 AND ((rule = 1 AND newest < ?) OR (rule = 2 AND id IS NOT resolved)"
        "  OR (rule = 3 AND newest < ? AND id != first)) ORDER BY id")
    return [(row[0], row[1], row[2]) for row in connection.execute(sql, (
        *newest_args, *queue_args, *lane_args,
        _cutoff(now, CHECKPOINT_IDLE_AGE), _cutoff(now, LANE_IDLE_AGE)))]


def _still_due(connection, chunk: list[int]):
    """Rows `chunk` as (id, timestamp, resolved_at, key's first id, key's newest id), read under the lock.

    Each row is a rowid lookup and each bound an index-only read of
    `state_by_kind_key`, so no row outside `chunk` is read from the table.
    `resolved_at` is read for the chunk's rows only, whose overflow pages
    the delete frees anyway.
    """
    bound = "(SELECT {}(id) FROM state AS other WHERE other.kind = 'checkpoint' AND other.key = row.key)"
    return connection.execute(
        f"SELECT id, timestamp, resolved_at, {bound.format('MIN')} AS first, {bound.format('MAX')} AS last"
        f" FROM state AS row WHERE id IN ({','.join('?' * len(chunk))})", chunk)


@dataclass(frozen=True)
class Compacted:
    """What the checkpoint step did: rows deleted, rows archived, body bytes freed, and why it stopped."""

    deleted: int = 0
    archived: int = 0
    freed: int = 0
    path: Path | None = None
    #: Why no row was deleted though some were due; empty when the rules found none or all went.
    why: str = ""
    #: The `why` is a fault a person must look at, not the bulk root left unset.
    attention: bool = False


def _chunks(values: list, size: int = CHUNK):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _encoded(name: str, raw: bytes | None) -> dict:
    """`raw` as JSON: `name` for null or UTF-8, `<name>_b64` for any other bytes, as stored."""
    if raw is None:
        return {name: None}
    try:
        return {name: bytes(raw).decode("utf-8")}
    except UnicodeDecodeError:
        return {f"{name}_b64": base64.b64encode(raw).decode("ascii")}


def _decoded(record: dict, name: str) -> bytes | None:
    if f"{name}_b64" in record:
        return base64.b64decode(record[f"{name}_b64"], validate=True)
    value = record[name]
    return None if value is None else value.encode("utf-8")


def _digest(key: bytes | None, body: bytes | None) -> str:
    """A row's key and body bytes, with null told from empty."""
    digest = hashlib.sha256()
    for raw in (key, body):
        digest.update(b"\x00" if raw is None else b"\x01" + len(raw).to_bytes(8, "big") + bytes(raw))
    return digest.hexdigest()


def _sync(fd: int) -> None:
    """Flush `fd` with `fsync`, as the runner journal does; only that opener may import fcntl."""
    # ponytail: macOS fsync stops at the drive cache; route F_FULLFSYNC through runner_journal if a power loss ever costs an archive.
    os.fsync(fd)


def write_archive(connection, ids: list[int], folder: Path, name: str) -> list[tuple]:
    """Write rows `ids` to `folder/name`, synced, and read it back; each row's (id, timestamp, resolved_at, digest).

    The rows go to a part file first and reach `name` by rename, so a stop
    leaves no file under the final name with part of the rows in it. Raises
    `ArchiveFailed` for any write or read-back fault; a failed file is removed.
    """
    final, part = folder / name, folder / f".{name}.{os.getpid()}.part"
    written: list[tuple] = []
    renamed = False
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for stale in folder.glob(".*.part"):
            stale.unlink(missing_ok=True)  # a stopped run's; it deleted nothing
        if final.exists():
            raise ArchiveFailed(f"{final} already exists; one prune writes one archive")
        with open(part, "xb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as packed:
                for chunk in _chunks(ids):
                    for row in connection.execute(
                            "SELECT id, kind, CAST(key AS BLOB) AS key, timestamp, resolved_at, typeof(body) AS type,"
                            f" CAST(body AS BLOB) AS body FROM state WHERE id IN ({','.join('?' * len(chunk))})"
                            " ORDER BY id", chunk):
                        record = {"id": row["id"], "kind": row["kind"], "timestamp": row["timestamp"],
                                  "resolved_at": row["resolved_at"], "type": row["type"],
                                  **_encoded("key", row["key"]), **_encoded("body", row["body"])}
                        packed.write(json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n")
                        written.append((row["id"], row["timestamp"], row["resolved_at"],
                                        _digest(row["key"], row["body"]), len(row["body"] or b"")))
            raw.flush()
            _sync(raw.fileno())
        os.replace(part, final)
        renamed = True
        directory = os.open(folder, os.O_RDONLY)
        try:
            _sync(directory)
        finally:
            os.close(directory)
        found = read_archive(final)
    except ArchiveFailed:
        raise
    except (OSError, EOFError, ValueError, KeyError, TypeError, AttributeError, zlib.error) as error:
        if renamed:
            final.unlink(missing_ok=True)
        raise ArchiveFailed(f"the archive {final} was not written: {type(error).__name__}: {error}") from error
    finally:
        part.unlink(missing_ok=True)
    if found != [row[:4] for row in written]:
        final.unlink(missing_ok=True)
        raise ArchiveFailed(f"the archive {final} read back {len(found)} row(s) of {len(written)} written, "
                            "or not the rows written")
    return written


def read_archive(path: Path) -> list[tuple]:
    """Each line of the archive at `path` as (id, timestamp, resolved_at, digest of its key and body bytes)."""
    found = []
    with gzip.open(path, "rb") as packed:
        for line in packed:
            record = json.loads(line)
            found.append((record["id"], record["timestamp"], record["resolved_at"],
                          _digest(_decoded(record, "key"), _decoded(record, "body"))))
    return found


def compact_checkpoints(connection, *, now: datetime, root: Path | None, name: str | None = None,
                        unresolved: str = "") -> Compacted:
    """Archive each `checkpoint_victims` row under `root`, read it back, then delete it.

    `root` is the bulk root; None is no bulk root, and `unresolved` says
    why when it was set and could not be read. Without one, or with an
    archive that fails, no row is deleted. The victims are read before
    the write lock, since that read walks every checkpoint body. The
    delete is one transaction that re-checks each victim by `_still_due`:
    a key written or cut since that read, or a row resolved since its
    archive, keeps its rows; the archive holds a copy only.
    """
    victims = _victims(connection, now=now)
    bounds = {ident: (first, last) for ident, first, last in victims}
    ids = [ident for ident, _, _ in victims]
    if not ids:
        return Compacted()
    kept = f"{len(ids)} checkpoint row(s) kept"
    if root is None:
        if unresolved:
            return Compacted(why=f"{kept}: {unresolved}", attention=True)
        return Compacted(why=f"{kept}: sd.bulk_storage_root is unset, and no row goes unarchived")
    if not root.is_dir():
        return Compacted(why=f"{kept}: the bulk root {root} is not a folder", attention=True)
    folder = root / ARCHIVE_FOLDER
    name = name or f"{now.astimezone(UTC).date().isoformat()}.jsonl.gz"
    try:
        written = write_archive(connection, ids, folder, name)
    except ArchiveFailed as failure:
        return Compacted(why=f"{kept}: {failure}", attention=True)
    deleted = freed = 0
    with transaction(connection):
        archived = {row[0]: row for row in written if row[0] in bounds}
        for chunk in _chunks(sorted(archived)):
            same = [row["id"] for row in _still_due(connection, chunk)
                    if (row["timestamp"], row["resolved_at"]) == archived[row["id"]][1:3]
                    and (row["first"], row["last"]) == bounds[row["id"]]]
            if same:
                deleted += connection.execute(
                    f"DELETE FROM state WHERE id IN ({','.join('?' * len(same))})", same).rowcount
                freed += sum(archived[ident][4] for ident in same)
    return Compacted(deleted=deleted, archived=len(written), freed=freed, path=folder / name)


@dataclass(frozen=True)
class Vacuumed:
    """The database's logical size, `page_count * page_size`, before and after, and its WAL file after."""

    before: int
    after: int
    ran: bool
    #: The WAL checkpoint finished: False while a reader still held an older snapshot.
    checkpointed: bool = True
    wal: int = 0
    why: str = ""

    def __str__(self) -> str:
        done = f"vacuumed {self.before} bytes to {self.after} bytes" if self.ran else f"vacuum not run: {self.why}"
        if not self.checkpointed:
            done += f"; a reader held the WAL at {self.wal} bytes, so the next night checkpoints it"
        return done


def _size(connection) -> tuple[int, int]:
    """The database's logical bytes and its free bytes."""
    refuse_hub_only(connection, "the vacuum")
    page = connection.execute("PRAGMA page_size").fetchone()[0]
    return (connection.execute("PRAGMA page_count").fetchone()[0] * page,
            connection.execute("PRAGMA freelist_count").fetchone()[0] * page)


def _wal(connection) -> int:
    """The bytes of the database's WAL file; 0 when it has none."""
    refuse_hub_only(connection, "the vacuum")
    path = next((row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"), "")
    try:
        return os.stat(f"{path}-wal").st_size if path else 0
    except FileNotFoundError:
        return 0


def vacuum(connection, *, threshold: int = VACUUM_FREE_BYTES, busy_ms: int = VACUUM_BUSY_MS) -> Vacuumed:
    """VACUUM once `threshold` bytes of pages are free, waiting `busy_ms` for another writer's transaction.

    It reads the free pages, not what tonight's prune freed, so a VACUUM a
    held lock stopped runs the next night. VACUUM is all or nothing: a lock
    it never got changed nothing, and `sqlite3.OperationalError` says so to
    the caller. The WAL is then checkpointed and truncated, with the
    connection's own `busy_timeout`, since a waiting checkpoint holds off
    every writer; a reader on an older snapshot leaves it, and a WAL file
    still past `threshold` the next night is checkpointed then. A fault in
    that checkpoint or the size read after it raises `VacuumStageFailed`,
    which names the stage: the VACUUM itself did run.
    """
    refuse_hub_only(connection, "the vacuum")
    if connection.in_transaction:
        raise RetentionRefused("VACUUM cannot run inside a transaction")
    before, free = _size(connection)
    ran, wal = free >= threshold, _wal(connection)
    why = "" if ran else f"{free} free bytes, under the {threshold} that start one"
    if not ran and wal < threshold:
        return Vacuumed(before=before, after=before, ran=False, wal=wal, why=why)
    if ran:
        held = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        connection.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
        try:
            connection.execute("VACUUM")
        finally:
            connection.execute(f"PRAGMA busy_timeout = {int(held)}")
    else:
        why += f"; the WAL held {wal} bytes, so it was checkpointed"
    stage = "WAL checkpoint"
    try:
        busy = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        stage = "size read"
        after, wal = _size(connection)[0], _wal(connection)
    except (sqlite3.Error, OSError) as error:
        done = "the VACUUM ran" if ran else f"the VACUUM was not due ({why})"
        raise VacuumStageFailed(f"{done}, then the {stage} failed: {type(error).__name__}: {error}") from error
    return Vacuumed(before=before, after=after, ran=ran, checkpointed=not busy, wal=wal, why=why)


def settle_clean_reports(connection, *, now: datetime) -> int:
    """Move a clean run report nobody acknowledged to `done` after seven days.

    A clean report is `kind='report'` with `fields.attention` false and no
    *open* followup on it, so `reporting.acknowledge` -- the operator's
    verb -- is the only thing that ever moved one, and the operator has
    nothing left to review on it. A resolved followup does not hold the row:
    it was reviewed, and the review is what `resolved_at` records. An
    attention report, or any report with an open followup, is never touched
    however old: those wait for a person.
    The write is the same `transition` `acknowledge` makes, so the history
    reads `planning -> done by retention` and the row itself stays.

    The candidate read sits inside the transaction, which is `BEGIN
    IMMEDIATE`: a followup landing between the read and the write would
    otherwise be settled over, so the read takes the write lock first and a
    followup either arrives before it and is seen, or waits behind the sweep.
    `json_extract` answers NULL for a row with no `attention` key, and NULL
    `= 0` is not true: a report that never said it was clean is not settled.
    A report whose `fields` is not valid JSON is not settled either, and the
    prune report names it.

    The rule is `reporting.clean_candidates`, which the bulk acknowledge
    starts from too, so the two read one predicate.
    """
    cutoff = _cutoff(now, CLEAN_REPORT_AGE)
    with transaction(connection):
        rows = reporting.clean_candidates(connection, before=cutoff)
        for row in rows:
            transition(connection, row, "done", who=RETENTION)
    return len(rows)


#: An item's last touch: its own `updated_at` or its newest note, whichever is later. A row in a
#: Documents view is not work that went stale (pack sd:6262), so it is never parked.
_UNTOUCHED = """
SELECT id FROM item
 WHERE status != 'done' AND parked_at IS NULL AND piece IS NULL AND due IS NULL AND recurrence IS NULL
   AND max(updated_at, coalesce((SELECT max(timestamp) FROM note WHERE note.item = item.id), '')) < ?
   AND """ + NOT_A_DOCUMENT + """ AND {rule}
 ORDER BY id
"""

#: Each park rule: its age, its extra predicate and its note, P4 first.
_PARK_RULES = (
    (P4_PARK_AGE, "priority = 4", "Parked: P4 untouched for {days} days"),
    (PLANNING_PARK_AGE,
     "status = 'planning' AND kind NOT IN ('report', 'followup')"
     " AND NOT EXISTS (SELECT 1 FROM assignment WHERE assignment.item = item.id)",
     "Parked: planning, never run, untouched for {days} days"),
)


def park_untouched(connection, *, now: datetime) -> list[int]:
    """Park every open item a `_PARK_RULES` rule finds untouched; return their ids.

    Touched means the item row changed (`updated_at`) or a note landed on it.
    An item with a due date or a recurrence is waiting on its date, not
    stale, so it is never parked: parking would hide it from Today. The
    rules run in order in one transaction, so an item both find is parked
    once, by the first, with its note. The reads sit inside the `BEGIN
    IMMEDIATE` transaction, as in `settle_clean_reports`, so a note landing
    mid-sweep is seen or waits.
    """
    stamp = now.astimezone(UTC).isoformat(timespec="seconds")
    ids: list[int] = []
    with transaction(connection):
        for age, rule, note in _PARK_RULES:
            found = [row[0] for row in connection.execute(_UNTOUCHED.format(rule=rule), (_cutoff(now, age),))]
            for item in found:
                set_item_fields(connection, item, parked_at=stamp)
                add_note(connection, item, "comment", note.format(days=age.days), session=RETENTION)
            ids.extend(found)
    return ids


def _unreadable(refs: list[str], limit: int) -> str:
    """The unreadable reports, each named as `item_ref` names it (sd:5124), the first `limit` of them."""
    named = ", ".join(refs[:limit])
    more = f" and {len(refs) - limit} more" if len(refs) > limit else ""
    return f"{len(refs)} report(s) in planning have unreadable fields: {named}{more}"


def prune(connection, backup: Snapshot, *, now: datetime | None = None, bulk_root: Path | None = None,
          unresolved: str = "") -> Pruned:
    """Run the retention table against this database, after `backup` passed.

    `bulk_root` is where the dropped checkpoint rows are archived first, and
    `unresolved` why a set root could not be read; see `compact_checkpoints`.
    """
    refuse_hub_only(connection, "the retention prune")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise RetentionRefused("the prune needs an aware time; a naive one read as UTC is wrong by the offset")
    if not isinstance(backup, Snapshot) or not passed(connection, backup):
        raise RetentionRefused(
            "the prune refuses to run: the night's backup did not pass, or is not this database's; nothing was removed")
    # The report is the last write and `ingest` refuses a second one for the
    # same run id with different evidence -- so without this the second prune
    # against one snapshot expired outputs, compacted heartbeats and settled
    # reports in three committed transactions, and only then failed. Each of
    # those is irreversible and none of them is the report the operator reads.
    # One backup is one prune, so the identity is checked while nothing has
    # been removed rather than after.
    if connection.execute(
            "SELECT 1 FROM item WHERE source = 'cron-report' AND external_id = ?",
            (f"{JOB}:{backup.run_id}",)).fetchone():
        raise RetentionRefused(
            f"the prune refuses to run: backup {backup.directory.name} ({backup.run_id}) already has its "
            f"prune report; one backup is one prune, and nothing was removed")
    stamp = now.astimezone(UTC).isoformat(timespec="seconds")
    exec_outputs = expire_exec_outputs(connection, now=now)
    heartbeats = compact_heartbeats(connection, now=now)
    compacted = compact_checkpoints(connection, now=now, root=bulk_root, unresolved=unresolved,
                                    name=f"{now.astimezone(UTC).date().isoformat()}-{backup.run_id}.jsonl.gz")
    checkpoints = compacted.deleted
    clean_reports = settle_clean_reports(connection, now=now)
    parked = park_untouched(connection, now=now)
    result = Pruned(exec_outputs=exec_outputs, heartbeats=heartbeats, clean_reports=clean_reports, report=0,
                    parked=len(parked), checkpoints=checkpoints, compacted=compacted)
    text = (f"sd-db prune after backup {backup.directory.name} ({backup.run_id}): {result}; "
            f"cost rows and exec notes are never pruned.\n")
    if compacted.path is not None:
        text += (f"Archived {compacted.archived} checkpoint row(s) to {compacted.path}, read back, before "
                 f"the delete; {compacted.freed} body byte(s) freed.\n")
    if compacted.why:
        text += compacted.why + "\n"
    if parked:
        named = item_refs(connection, parked)
        text += "Parked: " + ", ".join(named[item] for item in parked) + "\n"
    # A report the settle cannot read would otherwise sit in `planning`
    # silently, where before the guard it failed the whole prune. So the
    # prune report names it and asks for a person. The predicate is the dry
    # run's own, `reporting.UNREADABLE_FIELDS`, so NULL, '' and text that is
    # not JSON are one case here as they are there (sd:873).
    unreadable = [row[0] for row in connection.execute(
        "SELECT id FROM item WHERE kind='report' AND status='planning'"
        f" AND {reporting.UNREADABLE_FIELDS} ORDER BY id")]
    bases: list[str] = []
    if unreadable:
        named = item_refs(connection, unreadable)
        refs = [named[item] for item in unreadable]
        text += _unreadable(refs, UNREADABLE_TEXT_IDS) + "\n"
        bases.append(_unreadable(refs, UNREADABLE_BASIS_IDS))
    if compacted.attention:
        # `ingest` refuses a basis past 2000 characters; the text holds the whole reason.
        bases.append(compacted.why[:ARCHIVE_BASIS])
    attention = {"attention": True, "attention_basis": "; ".join(bases)} if bases else {}
    # One backup, one prune, one report: the row's identity is the backup's
    # run id, so a second prune against the same snapshot is refused by
    # `ingest` as a report that already exists with different evidence.
    state = reporting.ingest(connection, job=JOB, run_id=backup.run_id, started=stamp, ended=stamp,
                             exit_code=0, text=text, source_path=str(backup.directory), removed=result.counts,
                             **attention)
    return Pruned(exec_outputs=exec_outputs, heartbeats=heartbeats, clean_reports=clean_reports,
                  report=state["item"]["id"], parked=len(parked), checkpoints=checkpoints, compacted=compacted)
