"""The backup, and the restore that proves it.

A live WAL database is not a file a file-level mirror can copy consistently.
So the backup is its own step:
a snapshot into a dated directory, the two configuration files copied beside
it, and then **the copy is opened and checked**, because a backup nobody has
restored is a file, not a backup.

The order is the design:

1. Write a `checkpoint` row, with this run's id, **before** the snapshot.
2. Count the source and copy it into the dated directory, in one read
   transaction (`_snapshot`).
3. Copy configuration and the complete validated publication journal beside it.
4. Open the copy. Run `PRAGMA integrity_check`. Find the checkpoint row.
   Compare every table's count against the source.
5. Write the completed backup's ownership manifest. Prune only when requested:
   by count (`keep`) or by age (`keep_days`), never both.

Step 1 before step 2 is what makes step 4 mean something. A checkpoint
written afterwards would be in the source and not in the copy, and its
absence from the copy would prove nothing; written before, it is in both, and
a copy without it is a copy of something other than what was just checked.

**Step 1 is the one conditional step, and step 4 changes with it.** A
database waiting for `migrate` is exactly the one worth snapshotting first,
and the writable open is what refuses it, so `SchemaTooOld` reopens the file
`mode=ro` and no checkpoint row is written. Step 4 then proves the copy by
integrity and counts alone, and step 5 records `checkpoint: false`.
`_owned_backup` never skips the checkpoint question, so such a snapshot is
never owned, never counted for `--keep`, never pruned, and no row prune runs
after it. It restores like any older snapshot: `restore` migrates the staged
copy up to this library's version.

**Failures of the store never depend on the store.** Every function here
raises `BackupError` and writes nothing on the way out. The caller --
`sd-db-backup` -- sends one mail through the path the cron jobs use. That is
why the mail lives in the script and not in this module: a module that
reported its own failure by writing a row would have nothing to report a
failure to write a row.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import paths
from .database import (
    BUSY_TIMEOUT,
    connect,
    local_path,
    refuse_hub_only,
    schema_version,
    tables,
    transaction,
)
from .errors import BackupError, SchemaTooOld, SdDbError
from .migrate import migrate as migrate_database
from .schema import SCHEMA_VERSION, TABLES, migrations
from .writes import add_note, record_state
from .writes import now as stamp_now


class Retained(BackupError):
    """A backup a prune keeps by design, not by a fault: a stopped run or a copy no checkpoint dates.

    `prune_older` reports these as one line per reason (sd:5065); every other
    reason to keep a backup is a fault and keeps its own line.
    """


#: Where a run writes when no destination is named: the `Backup` folder on the
#: USB disk attached to this Mac. Snapshots taken before 2026-09-25 went to
#: `~/Documents/sd-backups/` and stay there.
DEFAULT_ROOT = Path("/Volumes/local/Backup/sd-backups")

#: The volume `DEFAULT_ROOT` must be on. Detached, `/Volumes/local` is a plain
#: directory on the boot disk, so the default is refused rather than written
#: there (`require_mount`).
DEFAULT_MOUNT = Path("/Volumes/local")

#: Names the root in place of `DEFAULT_ROOT`; a relative value is taken under
#: the home. Like `--destination`, a named root is not asked about the mount.
#: The test packages set it so every injected home keeps its own root.
ROOT_VARIABLE = "SD_DB_BACKUP_ROOT"

#: What is copied beside the database. Identity the rows do not hold.
CONFIGURATION = ("providers.yaml", "commands.yaml")

#: Conventional retention count, used only when retention is requested.
KEEP = 30

#: The name `_dated_directory` gives a run: the day, then an unpadded `.N`
#: when the day already has one. Unpadded means lexical order is not the
#: order the runs happened in -- `.10` sorts before `.9` -- so retention
#: orders by `_directory_order` and never by the name as text.
DATED_NAME = re.compile(r"\d{4}-\d{2}-\d{2}(?:\.[1-9]\d*)?")
BACKUP_MANIFEST = "backup-manifest.json"

# A restore must stop with a usable current database when another process
# holds the writer lock, rather than waiting forever inside sqlite.backup.
RESTORE_SECONDS = 30

#: How long the destination has to finish a test write before a run gives up
#: on it (`probe_destination`).
PROBE_SECONDS = 30

#: The test write, run in a child of its own: open, write and remove one small
#: file in the destination, or in the nearest part of it that exists.
_PROBE = """\
import os, sys
directory = sys.argv[1]
while not os.path.exists(directory) and os.path.dirname(directory) != directory:
    directory = os.path.dirname(directory)
path = os.path.join(directory, f".sd-db-probe-{os.getpid()}")
with open(path, "xb") as handle:
    handle.write(b"probe")
os.remove(path)
"""
PUBLICATION_RESTORE_INTENT = "publication-restore-intent.json"


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_recovery_record(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _files_fingerprint(files: dict[str, bytes]) -> str:
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def _journal_files(directory: Path) -> dict[str, bytes]:
    """Read only a complete, validated publication evidence directory."""
    from .publication_journal import validate_path
    try:
        validate_path(directory)
        return {path.relative_to(directory).as_posix(): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}
    except (SdDbError, OSError, ValueError, TypeError) as error:
        raise BackupError(f"publication journal is not a verified recovery source: {error}") from error


def _copy_publications(source: Path, directory: Path, expected: dict[str, int]) -> bool:
    intent = source.parent / PUBLICATION_RESTORE_INTENT
    if intent.exists() or intent.is_symlink():
        raise BackupError("publication journal restore is incomplete; resume its verified backup before taking another")
    journal = source.parent / "publications"
    if not journal.exists() and not journal.is_symlink():
        if expected.get("publication_claim", 0):
            raise BackupError("publication claims exist but their durable journal is missing")
        return False
    _journal_files(journal)
    try:
        shutil.copytree(journal, directory / "publications", symlinks=True)
    except OSError as error:
        raise BackupError(f"publication journal was not copied: {error}") from error
    # A concurrent append may make a copy incomplete. Fail this backup rather
    # than certify a partial catalog or missing event; never change the source.
    _journal_files(directory / "publications")
    return True


#: The migrations that create the two evidence tables. A database at or past
#: the version owes the table; one before it does not have it and is whole.
PUBLICATION_CLAIM_VERSION = 4
RUNNER_RUN_VERSION = 5


def _check_publication_claims(connection: sqlite3.Connection, directory: Path) -> None:
    """Every database claim must have matching, immutable external evidence.

    A table this old library has and the copy does not is nothing to check,
    not a fault: `publication_claim` arrives in migration 004, and `run`
    snapshots a database waiting for `migrate` read-only, so a valid version
    1-3 copy reaches here without it. Unguarded, the `SELECT` left `run` with
    a bare `no such table` from sqlite3 rather than this module's documented
    `BackupError`, on the one database the read-only path exists to serve.

    The gate is the version, not the table's absence. A version 4 or later
    database without the table is malformed: `_check_restore` refuses it, so
    a snapshot of it must not pass here, take an ownership manifest and let
    retention remove a valid recovery point on its account (Codex, sd:1207).
    """
    if schema_version(connection) < PUBLICATION_CLAIM_VERSION:
        return
    if "publication_claim" not in set(tables(connection)):
        raise BackupError(f"a version {schema_version(connection)} database has no publication_claim "
                          f"table; migration {PUBLICATION_CLAIM_VERSION:03d} creates it, so the copy is malformed")
    claims = list(connection.execute("SELECT id, item, payload, state FROM publication_claim"))
    if not claims:
        return
    records = _journal_files(directory / "publications")
    for claim in claims:
        manifest = json.loads(records.get(f"{claim['id']}/manifest.json", b"{}"))
        states = [json.loads(content)["state"] for name, content in records.items()
                  if name.startswith(f"{claim['id']}/") and name != f"{claim['id']}/manifest.json"]
        if (manifest.get("item") != claim["item"] or manifest.get("payload") != json.loads(claim["payload"])
                or json.loads(claim["state"]) not in states):
            raise BackupError(f"publication claim {claim['id']} differs from the backup journal")


def _journal_union(snapshot: dict[str, bytes], live: dict[str, bytes]) -> dict[str, bytes]:
    result = dict(live)
    for name, content in snapshot.items():
        existing = result.get(name)
        if existing is not None and existing != content:
            if name != "claims.jsonl" or not (existing.startswith(content) or content.startswith(existing)):
                raise BackupError(f"publication journal conflicts with live evidence: {name}; preserve both copies")
            content = max((existing, content), key=len)
        result[name] = content
    return result


def _pending_publications(state: Path, saved: dict[str, bytes]) -> tuple[dict[str, bytes], Path] | None:
    intent = state / PUBLICATION_RESTORE_INTENT
    if not intent.exists() and not intent.is_symlink():
        return None
    try:
        if intent.is_symlink() or not intent.is_file() or intent.stat().st_size > 4096:
            raise ValueError("linked, missing, or oversized intent")
        value = json.loads(intent.read_bytes())
        relative = Path(value["material"])
        if (value["version"] != 1 or len(relative.parts) != 3
                or relative.parts[0] != "publication-recovery-evidence"
                or not relative.parts[1].startswith("restore-") or relative.parts[2] != "publications"
                or any((state / Path(*relative.parts[:index])).is_symlink() for index in range(1, 4))):
            raise ValueError("invalid material identity")
        material = state / relative
        files = _journal_files(material)
        if _files_fingerprint(files) != value["material_fingerprint"]:
            raise ValueError("restore material changed")
        if _files_fingerprint(saved) != value["source_fingerprint"]:
            raise ValueError("resume the backup matching the pending restore source")
        return files, material
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise BackupError(f"publication restore intent is unresolved: {error}; preserve its evidence") from error


def _partial_publications(journal: Path, intended: dict[str, bytes]) -> dict[str, bytes]:
    """Only the recorded restore target can prove an interrupted live prefix."""
    if not journal.exists() and not journal.is_symlink():
        return {}
    if journal.is_symlink() or not journal.is_dir():
        raise BackupError("pending publication restore has a linked or invalid journal directory")
    directories = {str(Path(name).parent) for name in intended} - {"."}
    found = {}
    for path in journal.rglob("*"):
        name = path.relative_to(journal).as_posix()
        if path.is_symlink() or (path.is_dir() and name not in directories):
            raise BackupError("pending publication restore contains unexpected live evidence")
        if path.is_dir():
            continue
        if not path.is_file() or name not in intended:
            raise BackupError("pending publication restore contains unexpected live evidence")
        data = path.read_bytes()
        if not intended[name].startswith(data):
            raise BackupError(f"pending publication restore conflicts with live evidence: {name}")
        found[name] = data
    return found


def _begin_publications_restore(saved: dict[str, bytes], combined: dict[str, bytes], state: Path) -> Path:
    evidence = state / "publication-recovery-evidence"
    evidence.mkdir(mode=0o700, exist_ok=True)
    if evidence.is_symlink() or not evidence.is_dir():
        raise BackupError("publication recovery evidence directory is linked or invalid")
    material = evidence / f"restore-{uuid.uuid4().hex}" / "publications"
    for name, data in combined.items():
        _write_recovery_record(material / name, data)
    _journal_files(material)
    _sync_directory(material.parent)
    _sync_directory(evidence)
    _sync_directory(state)
    intent = {"version": 1, "material": material.relative_to(state).as_posix(),
              "material_fingerprint": _files_fingerprint(combined), "source_fingerprint": _files_fingerprint(saved)}
    prepared_intent = material.parent / "intent.json"
    _write_recovery_record(prepared_intent, json.dumps(intent, sort_keys=True).encode())
    # Hard-linking a complete fsynced file publishes the hold atomically and
    # refuses an existing intent; a killed JSON write cannot strand recovery.
    os.link(prepared_intent, state / PUBLICATION_RESTORE_INTENT)
    _sync_directory(state)
    return material


def _prepare_publications(directory: Path, state: Path, stage: Path) -> dict[str, bytes]:
    snapshot, current = directory / "publications", state / "publications"
    saved = _journal_files(snapshot) if snapshot.exists() or snapshot.is_symlink() else {}
    pending = _pending_publications(state, saved)
    if pending is not None:
        combined, _ = pending
        _partial_publications(current, combined)
    else:
        live = _journal_files(current) if current.exists() or current.is_symlink() else {}
        combined = _journal_union(saved, live)
    if combined:
        prepared = stage / "publications"
        for name, content in combined.items():
            path = prepared / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        _journal_files(prepared)
    return saved


def _restore_publications(saved: dict[str, bytes], state: Path, target: Path) -> None:
    """Only add proven evidence. Never roll journals back with the database."""
    if not saved:
        return
    # Publication writers hold the database transaction while extending the
    # journal. Keep their catalog append serial with this merge, then release
    # the lock before SQLite's separate backup connection installs the DB.
    guard = sqlite3.connect(target, isolation_level=None, timeout=0) if target.exists() else None
    try:
        if guard is not None:
            guard.execute("BEGIN IMMEDIATE")
        journal = state / "publications"
        pending = _pending_publications(state, saved)
        if pending is not None:
            combined, material = pending
            live = _partial_publications(journal, combined)
        else:
            live = _journal_files(journal) if journal.exists() or journal.is_symlink() else {}
            combined = _journal_union(saved, live)
            material = _begin_publications_restore(saved, combined, state)
        journal.mkdir(mode=0o700, exist_ok=True)
        for name, content in sorted(combined.items(), key=lambda entry: (
                entry[0] == "claims.jsonl", str(Path(entry[0]).parent), Path(entry[0]).name != "manifest.json", entry[0])):
            path = journal / name
            if name in live:
                if content == live[name]:
                    continue
                # Usually only the catalog grows. Under a verified restore
                # intent, a killed file copy can also leave a byte prefix.
                with path.open("ab") as stream:
                    stream.write(content[len(live[name]):])
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                _write_recovery_record(path, content)
            _sync_directory(path.parent)
        _journal_files(journal)
        _sync_directory(state)
        os.replace(state / PUBLICATION_RESTORE_INTENT, material.parent / "completed-intent.json")
        _sync_directory(material.parent)
        _sync_directory(state)
    finally:
        if guard is not None:
            guard.close()


def _runner_files(directory: Path) -> dict[str, bytes]:
    from .runner_journal import validate_path
    try:
        validate_path(directory)
        return {path.name: path.read_bytes() for path in directory.glob("*.json")}
    except (SdDbError, OSError, ValueError, TypeError) as error:
        raise BackupError(f"runner journal is not a verified recovery source: {error}") from error


def _copy_runner_journal(source: Path, directory: Path, expected: dict[str, int]) -> bool:
    journal = source.parent / "runner-journal"
    intent = source.parent / "runner-restore-intent.json"
    if intent.exists() or intent.is_symlink():
        raise BackupError("runner journal restore is incomplete; resume its verified backup first")
    if not journal.exists() and not journal.is_symlink():
        if expected.get("runner_run", 0) and _journaled_runs_in(directory / source.name):
            raise BackupError("runner rows exist but their durable journal is missing")
        return False
    _runner_files(journal)
    try:
        # A lock is an empty flock file restore makes again; copied, a live
        # one can vanish under the mirror's rsync (sd:6071).
        shutil.copytree(journal, directory / "runner-journal", symlinks=True,
                        ignore=shutil.ignore_patterns("*.lock"))
    except OSError as error:
        raise BackupError(f"runner journal was not copied: {error}") from error
    _runner_files(directory / "runner-journal")
    return True


def journaled_runs() -> str:
    """The runs the journal holds: every `runner_run` a runner supervises (`runner.supervised`).

    A build run (pack sd:6093) keeps no journal: its evidence is its worktree
    and branch on disk, and a restore blocks its assignment (`recovery`). A
    draft run (pack sd:6361, sd:7282) reads the checkout and writes its spec
    through `draft.write`; a teammate run (pack sd:7221) is a lead's record of
    its in-session builder. No runner writes either, so neither has a journal.
    Imported here, not at the top: `runner` imports `ship` through `ledger`,
    and this module loads first.
    """
    from .runner import supervised  # noqa: PLC0415
    return ("SELECT runner_run.* FROM runner_run LEFT JOIN assignment "
            f"ON assignment.id = runner_run.assignment WHERE {supervised('assignment.scope')}")


def _journaled_runs_in(copy: Path) -> bool:
    connection = connect(copy, write=False)
    try:
        return connection.execute(journaled_runs() + " LIMIT 1").fetchone() is not None
    finally:
        connection.close()


def _check_runner_records(connection: sqlite3.Connection, directory: Path) -> None:
    # `runner_run` arrives in migration 005, so a read-only snapshot of a
    # version 1-4 database has no such table and nothing to check. Same
    # reason as `_check_publication_claims` above, and the same gate: the
    # version says whether the table is owed, its absence does not.
    if schema_version(connection) < RUNNER_RUN_VERSION:
        return
    if "runner_run" not in set(tables(connection)):
        raise BackupError(f"a version {schema_version(connection)} database has no runner_run table; "
                          f"migration {RUNNER_RUN_VERSION:03d} creates it, so the copy is malformed")
    rows = list(connection.execute(journaled_runs()))
    if not rows:
        return
    from .runner_journal import against, canonical
    files = _runner_files(directory / "runner-journal")
    for row in rows:
        # A record written before migration 014 names the repository by its
        # absolute path; the row names the key. Both sides keyed (sd:1447).
        # A row `repo remove` detached has no repo; its journal keeps it (sd:2581).
        record = against(json.loads(files.get(f"{row['id']}.json", b"{}" )).get("record", {}), dict(row))
        row = canonical(dict(row))
        identity = ("id", "assignment", "run", "repo", "branch", "work_path", "retained_path")
        if (any(record.get(name) != row[name] for name in identity)
                or record.get("journal_version", -1) < row["journal_version"]
                or record.get("journal_version") == row["journal_version"] and record != dict(row)):
            # Most often a repository renamed by hand: the rows moved, the journal did not (sd:3172).
            raise BackupError(f"runner row {row['id']} differs from the backup journal; after a "
                              f"repository rename, see the command pack's docs/sd-db.md, \"Renaming a repository\"")


def _compatible_runner_records(saved: dict[str, bytes], live: dict[str, bytes]) -> None:
    from .runner_journal import canonical
    for name in set(saved) & set(live):
        old, current = (json.loads(data)["record"] for data in (saved[name], live[name]))
        # A journal written before migration 014 names the repository by its
        # absolute path and one written after by its `~/` key (sd:1439).
        # One written before migration 018 has no `detached_from` (sd:2581).
        if (any(old[key] != current[key] for key in ("id", "assignment", "run", "branch", "work_path", "retained_path"))
                or not (old["repo"] == current["repo"] or paths.same(old["repo"], current["repo"]))
                or old["journal_version"] == current["journal_version"] and canonical(old) != canonical(current)):
            raise BackupError(f"runner journal conflicts with live evidence: {name}; preserve both copies")


def _prepare_runner_journal(directory: Path, state: Path) -> dict[str, bytes]:
    source, target = directory / "runner-journal", state / "runner-journal"
    saved = _runner_files(source) if source.exists() or source.is_symlink() else {}
    live = _runner_files(target) if target.exists() or target.is_symlink() else {}
    _compatible_runner_records(saved, live)
    intent = state / "runner-restore-intent.json"
    if intent.exists() or intent.is_symlink():
        _runner_restore_material(saved, state)
    return saved


def _runner_restore_material(saved: dict[str, bytes], state: Path) -> Path:
    intent = state / "runner-restore-intent.json"
    if intent.exists() or intent.is_symlink():
        try:
            if intent.is_symlink() or not intent.is_file() or intent.stat().st_size > 4096:
                raise ValueError("linked, missing, or oversized intent")
            value = json.loads(intent.read_bytes())
            relative = Path(value["material"])
            if (value["version"] != 1 or len(relative.parts) != 3
                    or relative.parts[0] != "runner-recovery-evidence" or not relative.parts[1].startswith("restore-")
                    or relative.parts[2] != "runner-journal"
                    or any((state / Path(*relative.parts[:index])).is_symlink() for index in range(1, 4))):
                raise ValueError("invalid material identity")
            material = state / relative
            if _files_fingerprint(_runner_files(material)) != value["fingerprint"] or _files_fingerprint(saved) != value["fingerprint"]:
                raise ValueError("resume the unchanged backup matching the pending restore source")
            return material
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise BackupError(f"runner restore intent is unresolved: {error}; preserve its evidence") from error
    evidence = state / "runner-recovery-evidence"
    evidence.mkdir(mode=0o700, exist_ok=True)
    if evidence.is_symlink() or not evidence.is_dir():
        raise BackupError("runner recovery evidence directory is linked or invalid")
    material = evidence / f"restore-{uuid.uuid4().hex}" / "runner-journal"
    material.mkdir(mode=0o700, parents=True)
    for name, data in saved.items():
        _write_recovery_record(material / name, data)
    _runner_files(material)
    value = {"version": 1, "material": material.relative_to(state).as_posix(), "fingerprint": _files_fingerprint(saved)}
    prepared = material.parent / "intent.json"
    _write_recovery_record(prepared, json.dumps(value, sort_keys=True).encode())
    _sync_directory(material.parent)
    _sync_directory(evidence)
    _sync_directory(state)
    os.link(prepared, intent)
    _sync_directory(state)
    return material


def _drop_install_copy(install: Path, target: Path) -> None:
    """Remove a restore's install copy, which only repeats its material record.

    A crash before the link leaves it unlinked; a crash after the link leaves
    it as the live journal file's second name. Any other link is refused.
    """
    try:
        details = install.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(details.st_mode):
        raise BackupError(f"runner restore install copy is linked or invalid: {install.name}; preserve its evidence")
    if details.st_nlink != 1:
        current = target.lstat() if target.exists() or target.is_symlink() else None
        if current is None or details.st_nlink != 2 or (details.st_dev, details.st_ino) != (current.st_dev, current.st_ino):
            raise BackupError(f"runner restore install copy has a link outside the live journal: {install.name}; preserve its evidence")
    install.unlink()
    _sync_directory(install.parent)


def _restore_runner_journal(saved: dict[str, bytes], state: Path) -> None:
    """Restore missing run identities; existing process ownership always wins."""
    if not saved:
        return
    from .runner_journal import lock
    material = _runner_restore_material(saved, state)
    journal = state / "runner-journal"
    journal.mkdir(mode=0o700, exist_ok=True)
    for name, saved_record in saved.items():
        with lock(journal / Path(name).with_suffix(".lock"), blocking=False):
            target = journal / name
            # The install copy is this record's own inode, so the live file
            # keeps one link; the material copy stays as restore evidence.
            install = material.parent / f"install-{name}"
            _drop_install_copy(install, target)
            if target.exists() or target.is_symlink():
                _compatible_runner_records({name: saved_record}, _runner_files(journal))
                # The retry after a restore killed between `os.link` below and
                # its `_sync_directory(journal)` arrives here: the link is
                # visible and not yet durable, and `_drop_install_copy` fsynced
                # only the material directory. So the journal directory is
                # fsynced on this path too, before the run is recorded
                # complete -- otherwise the intent is replaced and the
                # restore reads as finished over a link a power loss removes.
                _sync_directory(journal)
                continue
            _write_recovery_record(install, saved_record)
            # Link the complete fsynced record atomically; never leave a
            # partially copied run record for startup to misinterpret.
            os.link(install, target)
            _sync_directory(journal)
            install.unlink()
            _sync_directory(material.parent)
    _runner_files(journal)
    _sync_directory(state)
    os.replace(state / "runner-restore-intent.json", material.parent / "completed-intent.json")
    _sync_directory(material.parent)
    _sync_directory(state)


TRANSIENT_INSTALL_RE = re.compile(r"install-[0-9a-f]{32}\.json")


def _transient_install(relative: Path) -> bool:
    """Whether an archive entry is a restore's in-flight install copy.

    Exactly `restore-*/install-<record>.json`, the one name
    `_restore_runner_journal` makes and unlinks again: the record is a run id,
    which is a `uuid4().hex`. A deeper entry, or an `install-` name of any
    other shape, is evidence and is preserved rather than dropped -- a
    corrupted `install-notes.txt` beside a restore is exactly the artifact the
    evidence check exists to keep.
    """
    parts = relative.parts
    return (len(parts) == 2 and parts[0].startswith("restore-")
            and TRANSIENT_INSTALL_RE.fullmatch(parts[1]) is not None)


def _ignore_transient_installs(directory: str, names: list[str]) -> set[str]:
    """`shutil.copytree`'s filter for the same entries, by the same shape."""
    if not Path(directory).name.startswith("restore-"):
        return set()
    return {name for name in names if TRANSIENT_INSTALL_RE.fullmatch(name)}


def _copy_recovery_evidence(source: Path, directory: Path) -> list[str]:
    """Preserve diagnostic archives without treating them as active journals.

    `install-<record>.json` under a `restore-*` directory is the only entry
    here that is not evidence. `_restore_runner_journal` creates it between
    `os.link` and `install.unlink()`, and a restore that starts after
    `_copy_runner_journal`'s intent check has already passed is inside this
    walk with one on disk. Left in, it either lands in the archive copy as a
    duplicate of a live record, or -- the loud half -- vanishes between the
    hash pass and the copy and fails the whole backup with "recovery archive
    changed during backup". Neither is a fault of the store, so the transient
    is left out of both passes and out of the copy.

    This does not make backup and restore mutually exclusive; they still
    share no lock, and `_copy_runner_journal`'s intent check is the only
    ordering between them. What it does is stop the one named transient from
    deciding whether the night's backup succeeds.
    """
    copied, hashes = [], {}
    for name in ("publication-recovery-evidence", "runner-recovery-evidence"):
        archive = source.parent / name
        if not archive.exists() and not archive.is_symlink():
            continue
        try:
            if archive.is_symlink() or not archive.is_dir():
                raise BackupError(f"recovery archive is linked or invalid: {name}")
            expected = {}
            for path in archive.rglob("*"):
                if _transient_install(path.relative_to(archive)):
                    continue
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise BackupError(f"recovery archive contains a linked or invalid entry: {name}")
                if path.is_file():
                    expected[path.relative_to(archive).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            target = directory / name
            shutil.copytree(archive, target, symlinks=True, ignore=_ignore_transient_installs)
            actual = {path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in target.rglob("*")
                      if path.is_file() and not path.is_symlink()
                      and not _transient_install(path.relative_to(target))}
            if actual != expected or any(path.is_symlink() for path in target.rglob("*")):
                raise BackupError(f"recovery archive changed during backup: {name}")
            hashes[name] = expected
            copied.append(name)
        except OSError as error:
            raise BackupError(f"recovery archive was not copied: {name}: {error}") from error
    if hashes:
        _write_recovery_record(directory / "recovery-evidence-sha256.json", json.dumps(hashes, sort_keys=True).encode())
    return copied


def backup_root(home: Path | str) -> Path:
    """`ROOT_VARIABLE` when it is set, under `home` if relative; else `DEFAULT_ROOT`."""
    named = os.environ.get(ROOT_VARIABLE)
    if named:
        return Path(home) / Path(named).expanduser()
    return DEFAULT_ROOT


@dataclass
class Snapshot:
    """One completed run: where it went, and what the check found."""

    directory: Path
    run_id: str
    counts: dict[str, int] = field(default_factory=dict)
    removed: list[Path] = field(default_factory=list)
    configuration: list[str] = field(default_factory=list)
    #: False when the source could not be written -- a database waiting for
    #: `migrate` -- so no checkpoint row proves the copy is this run's. Such a
    #: snapshot is verified by integrity and counts only, is never owned by
    #: retention, and runs no prune.
    checkpointed: bool = True
    #: What `PRAGMA foreign_key_check` found in the SOURCE, as its own rows:
    #: `(table, rowid, parent, fkid)`. Empty is the normal answer, and it is
    #: reported rather than raised -- see the call in `run`. Note that `fkid`
    #: is the index of the constraint within the child table and NOT a parent
    #: rowid; reading it as one produces a confident wrong claim about which
    #: row is orphaned, which is how sd:744 was misread the first time.
    violations: list[tuple] = field(default_factory=list)


def _dated_directory(root: Path, when: datetime) -> Path:
    """A directory named for the day, with a suffix if the day is taken.

    Two runs in a day happen -- a scheduled one and one before a migration --
    and the second must not overwrite the first, because the first is the one
    with the pre-migration rows.
    """
    stem = when.strftime("%Y-%m-%d")
    candidate = root / stem
    index = 1
    while candidate.exists():
        candidate = root / f"{stem}.{index}"
        index += 1
    return candidate


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    present = set(tables(connection))
    return {
        name: int(connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0])
        for name in TABLES
        if name in present
    }


def _fingerprint(database: Path) -> dict[str, list[int] | None]:
    """What any commit to `database` changes: the inode, size and modified
    time of the file and of its write-ahead log (sd:3507).

    An empty or absent log is the same answer: any connection may create an
    empty one, and the last to close deletes it. The shared-memory file is
    left out, because readers write it.
    """
    found: dict[str, list[int] | None] = {}
    for key, path in (("database", database), ("wal", database.with_name(database.name + "-wal"))):
        try:
            details = path.stat()
        except FileNotFoundError:
            if key == "database":
                raise
            found[key] = None
            continue
        found[key] = [details.st_ino, details.st_size, details.st_mtime_ns] if details.st_size or key == "database" \
            else None
    return found


def _snapshot(connection: sqlite3.Connection, copy: Path, *,
              source: Path | None = None) -> tuple[dict[str, int], dict[str, list[int] | None] | None]:
    """Count the source and copy it to `copy` inside one read transaction.

    With `source`, also its `_fingerprint`, read while a second connection
    holds the write lock, from before the first read fixes this
    transaction's view until after: a commit before it is in the copy, and
    one after it changes the fingerprint. The lock is the point: a writer
    writes its frames to the log before it publishes the commit, so without
    it a commit whose frames are already in the fingerprint could become
    visible only after the view was fixed (round 3). Only a connection that
    writes nothing may ask: a checkpoint row, or the log copied back into the
    file on close, would change the fingerprint after the fact.

    The counts `verify` compares and the copy are then one moment of the
    source, whatever other connections commit meanwhile: under WAL a reader
    holds its snapshot and does not block a writer. `VACUUM INTO` cannot run
    inside a transaction, so on 2026-10-09 two rows committed between the
    count and the copy failed the comparison (sd:3121). The backup API
    copies in one step on the transaction already open. Its copy keeps the
    source's WAL header, so the journal is switched back: the snapshot is a
    closed single file, as `restore` expects.
    """
    connection.execute("BEGIN")
    try:
        holder = None
        if source is not None:
            # Waits for a writer mid-commit; takes no write of its own, and
            # closes while this read is open, so it is not the last connection
            # to close and copies nothing back into the file.
            try:
                holder = sqlite3.connect(f"file:{source}?mode=rw", uri=True, isolation_level=None,
                                         timeout=BUSY_TIMEOUT / 1000)
                holder.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                if holder is not None:
                    holder.close()
                raise BackupError(f"the write lock for the store's fingerprint was not taken: {error}") from None
        try:
            store = None if source is None else _fingerprint(source)
            expected = _counts(connection)
        finally:
            if holder is not None:
                holder.execute("ROLLBACK")
                holder.close()
        try:
            target = sqlite3.connect(copy, isolation_level=None)
            try:
                connection.backup(target)
                target.execute("PRAGMA journal_mode = DELETE")
            finally:
                target.close()
        except sqlite3.Error as error:
            # A full volume arrives here, as `database or disk is full`.
            raise BackupError(f"the snapshot was not written to {copy}: {error}") from None
    finally:
        connection.execute("ROLLBACK")
    return expected, store


def verify(copy: Path, *, run_id: str, expected: dict[str, int], checkpointed: bool = True) -> dict[str, int]:
    """Open the copy and prove it is the database that was just checked.

    Three questions, in the order that makes the answers useful: is the file
    a database at all, is it *this* run's database, and does it hold the same
    rows. A truncated copy fails the first or the third; a stale directory
    fails the second. A snapshot taken without a checkpoint row (`run` could
    not write the source) skips the second question and says so in its
    result; the manifest check never skips it, which is what keeps such a
    snapshot out of retention's hands.
    """
    if not copy.exists():
        raise BackupError(f"the snapshot at {copy} was not written")
    try:
        connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as error:
        raise BackupError(f"the snapshot at {copy} does not open: {error}") from None
    try:
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as error:
            raise BackupError(
                f"the snapshot at {copy} failed integrity_check: {error}"
            ) from None
        if result != "ok":
            raise BackupError(f"the snapshot at {copy} failed integrity_check: {result}")

        row = connection.execute(
            "SELECT id FROM state WHERE kind = 'checkpoint' AND key = ?", (run_id,)
        ).fetchone()
        if row is None and checkpointed:
            raise BackupError(
                f"the snapshot at {copy} holds no checkpoint row for run "
                f"{run_id}; it is a copy of some other moment"
            )

        found = _counts(connection)
        differing = {
            name: (expected[name], found.get(name))
            for name in expected
            if found.get(name) != expected[name]
        }
        if differing:
            detail = ", ".join(
                f"{name}: source {source}, snapshot {snapshot}"
                for name, (source, snapshot) in sorted(differing.items())
            )
            raise BackupError(f"the snapshot at {copy} has different counts -- {detail}")
        return found
    finally:
        connection.close()


#: What Finder leaves in a folder someone browsed on a share. A moved copy is
#: judged without them: the writer never records them and restore never reads them.
FINDER_METADATA = re.compile(r"\.DS_Store|\._.*")

#: A run's lock as a snapshot before sd:6071 listed it. A moved copy is
#: judged without it, so a mirror that skips the locks still matches.
RUNNER_LOCK = re.compile(r"runner-journal/[0-9a-f]{32}\.lock")


def _manifest_entries(entries: dict, *, moved: bool) -> dict:
    """The manifest's entries a copy is compared on: all, or for a moved copy, all but run locks."""
    return {name: digest for name, digest in entries.items() if not (moved and RUNNER_LOCK.fullmatch(name))}


def _retention_inventory(directory: Path, *, moved: bool = False, hashed: bool = True) -> dict[str, str | None]:
    """Bind every entry, including empty directories, without following links.

    Without `hashed` a file maps to "" instead of its hash: the listing alone.
    """
    inventory: dict[str, str | None] = {}
    device = directory.stat().st_dev
    for path in sorted(directory.rglob("*")):
        if moved and FINDER_METADATA.fullmatch(path.name):
            continue
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or details.st_dev != device:
            raise BackupError("backup retention refuses linked or mounted entries")
        if path == directory / BACKUP_MANIFEST:
            if not stat.S_ISREG(details.st_mode):
                raise BackupError("backup retention manifest is not a regular file")
            continue
        name = path.relative_to(directory).as_posix()
        if moved and RUNNER_LOCK.fullmatch(name):
            continue
        if stat.S_ISREG(details.st_mode):
            inventory[name] = hashlib.sha256(path.read_bytes()).hexdigest() if hashed else ""
        elif stat.S_ISDIR(details.st_mode):
            inventory[name] = None
        else:
            raise BackupError("backup retention refuses special files")
    return inventory


def _record_backup(directory: Path, source: Path, run_id: str, counts: dict[str, int], *,
                   checkpointed: bool = True, store: dict[str, list[int] | None] | None = None,
                   forensic: bool = False) -> None:
    # `checkpoint: false` is a note for the reader, not a key `_owned_backup`
    # honours: it re-verifies through the checkpoint row, so a snapshot
    # without one is never owned, never pruned, and never counted for `--keep`.
    value = {"format": "sd-db-backup", "version": 1, "directory": str(directory.resolve()),
             "database": source.name, "run_id": run_id, "counts": counts,
             "checkpoint": checkpointed, "entries": _retention_inventory(directory)}
    if store is not None:
        # `migration_backup` compares it with the live store (`_snapshot`).
        value["store"] = store
    if forensic:
        # The source had foreign-key violations, so `restore` refuses this copy;
        # `prune` keeps it but leaves it out of the kept count (sd:8817).
        value["forensic"] = True
    _write_recovery_record(directory / BACKUP_MANIFEST, (json.dumps(value, sort_keys=True) + "\n").encode())


def _check_backup(directory: Path, *, run_id: str | None = None, moved: bool = False,
                  read_only: bool = False, owned_only: bool = False) -> tuple[int, int, str]:
    """The directory's identity when it is a verified backup; else `BackupError` saying why.

    `moved` accepts a copy away from where it was written, such as the hourly
    mirror on a share (sd:4011). Its manifest still names the first directory,
    so the path proves nothing about it; what proves it is the rest: every
    file hashes to the manifest, and the database verifies against the
    manifest's run and counts.

    `read_only` accepts a snapshot whose manifest says `checkpoint: false`,
    taken before `migrate` (`migration_backup`). Retention never passes it,
    so such a snapshot is still never owned.

    `owned_only` is the age prune's question (sd:5682): is this ours to
    delete, not does it restore. The manifest and the listing of names and
    kinds must match, and the database must hold its run's checkpoint row
    (`_checkpoint`, one row; a read-only snapshot is `Retained`). No file is
    hashed and the database is not verified, so a copy over SMB costs a
    listing and one read, not minutes.
    """
    try:
        if directory.is_symlink() or not directory.is_dir() or directory.parent.is_symlink():
            raise BackupError("it is a link or not a directory")
        if not DATED_NAME.fullmatch(directory.name):
            raise BackupError("its name is not a backup's dated name")
        datetime.strptime(directory.name[:10], "%Y-%m-%d")
        # Read before the hashes and compared after them: retention hashes a
        # copy once, so a directory swapped in meanwhile was never read.
        before = directory.stat()
        manifest = directory / BACKUP_MANIFEST
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 16 * 1024 * 1024:
            raise BackupError("it holds no backup manifest")
        material = manifest.read_bytes()
        value = json.loads(material)
        database = value.get("database")
        counts = value.get("counts")
        stored = value.get("run_id")
        if run_id is not None and stored != run_id:
            raise BackupError("its manifest names another run")
        run_id = stored
        if (value.get("format") != "sd-db-backup" or type(value.get("version")) is not int
                or value["version"] != 1
                or not isinstance(database, str) or Path(database).name != database or database in ("", ".", "..")
                or not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id)
                or not isinstance(counts, dict) or "state" not in counts
                or any(type(count) is not int or count < 0 for count in counts.values())
                or not isinstance(value.get("entries"), dict)
                or database not in value["entries"] or value["entries"][database] is None):
            raise BackupError("its manifest is not a backup manifest")
        if not moved and value.get("directory") != str(directory.resolve()):
            raise BackupError("its manifest records another directory")
        if owned_only:
            listed = {name: digest is None
                      for name, digest in _manifest_entries(value["entries"], moved=moved).items()}
            if listed != {name: digest is None
                          for name, digest in _retention_inventory(directory, moved=moved, hashed=False).items()}:
                raise BackupError("its files differ from its manifest")
        elif _manifest_entries(value["entries"], moved=moved) != _retention_inventory(directory, moved=moved):
            raise BackupError("its files differ from its manifest")
        checkpointed = not (read_only and value.get("checkpoint") is False)
        if owned_only:
            _checkpoint(directory)
        elif verify(directory / database, run_id=run_id, expected=counts, checkpointed=checkpointed) != counts:
            raise BackupError("its database holds other counts than its manifest")
        details = directory.stat()
        if (details.st_dev, details.st_ino) != (before.st_dev, before.st_ino):
            raise BackupError("it was replaced while it was read")
        return details.st_dev, details.st_ino, hashlib.sha256(material).hexdigest()
    except BackupError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as error:
        raise BackupError(f"it could not be read: {error}") from None


def _owned_backup(directory: Path, *, run_id: str | None = None, moved: bool = False) -> tuple[int, int, str] | None:
    """Legacy, moved, incomplete, modified, or unrelated directories stay put.

    `run_id` asks the stronger question `passed` needs: is this directory the
    backup *that run* took. Without it the answer is only "some owned backup",
    which is not the same claim once two runs have left a directory each.
    `moved` lets a verified copy away from its first directory count (`_check_backup`).
    """
    try:
        return _check_backup(directory, run_id=run_id, moved=moved)
    except BackupError:
        return None


def passed(connection: sqlite3.Connection, snapshot: Snapshot) -> bool:
    """Whether `snapshot` is a backup of this database that held up.

    Verified again, not taken on trust: the dated directory must still be an
    owned backup **of this run**, whose copy restores to its recorded counts,
    and its run's checkpoint row must be in the database asked. The nightly
    prune runs only on a `True` here (`retention.prune`).

    The run id binds the two halves together, and without that binding they
    answered about different runs: a directory that was some owned backup,
    and a checkpoint row that was some run's. With two valid runs on one
    machine, `Snapshot(first.directory, run_id=second.run_id)` then passed --
    the first directory is owned, the second run has its row -- and the prune
    ran against a snapshot that is not the one it names.
    """
    if not isinstance(snapshot, Snapshot) or not re.fullmatch(r"[0-9a-f]{32}", snapshot.run_id or ""):
        return False
    if _owned_backup(Path(snapshot.directory), run_id=snapshot.run_id) is None:
        return False
    row = connection.execute(
        "SELECT 1 FROM state WHERE kind = 'checkpoint' AND key = ?", (snapshot.run_id,)
    ).fetchone()
    return row is not None


def migration_backup(home: Path | str) -> Path | None:
    """The snapshot that lets `sd-db migrate` run, or None when nothing is pending (sd:3507).

    The newest dated directory under `backup_root` must pass the checks
    retention and `restore_newest` make -- every file hashes to its manifest,
    and the copy verifies against its run and counts (`_check_backup`), and
    `restore` would take it (`check_restorable`) -- and be this store as it
    stands: the same schema version, the same row counts, and the fingerprint
    its manifest recorded (`unchanged_since`). Only a read-only snapshot, the
    one taken of a store waiting for `migrate`, records one; a checkpointed
    snapshot wrote to the store, so it is refused here.
    `unchanged_since` runs again under `migrate`'s write lock.
    Raises `BackupError` naming why not and both ways out.
    """
    database = local_path(home)
    if not database.exists():
        return None
    live = connect(database, write=False)
    try:
        version = schema_version(live)
        counts = _counts(live)
    finally:
        live.close()
    if version >= SCHEMA_VERSION:
        return None
    root = backup_root(home)
    try:
        dated = sorted((path for path in root.iterdir() if DATED_NAME.fullmatch(path.name)
                        and path.is_dir() and not path.is_symlink()), key=_directory_order)
    except OSError:
        dated = []
    if not dated:
        raise BackupError(f"migrate refuses: no backup under {root}; {_MIGRATE_REMEDY}")
    newest = dated[-1]
    try:
        _check_backup(newest, read_only=True)
        held = json.loads((newest / BACKUP_MANIFEST).read_bytes())["counts"]
        check = check_restorable(newest / database.name)
        if not check.accepted:
            raise BackupError(f"restore refuses it: {check.refusal}")
    except (OSError, ValueError, KeyError, BackupError) as error:
        raise BackupError(f"migrate refuses: the newest backup {newest} does not verify: {error}; "
                          f"{_MIGRATE_REMEDY}") from None
    if check.version != version:
        raise BackupError(f"migrate refuses: the newest backup {newest} is at schema version {check.version}, "
                          f"the store at {version}; {_MIGRATE_REMEDY}")
    if held != counts:
        differ = ", ".join(f"{name}: store {counts.get(name)}, backup {held.get(name)}"
                           for name in sorted(set(counts) | set(held)) if counts.get(name) != held.get(name))
        raise BackupError(f"migrate refuses: the store changed since the newest backup {newest} ({differ}); "
                          f"{_MIGRATE_REMEDY}")
    unchanged_since(home, newest)
    return newest


_MIGRATE_REMEDY = (f"run this pack's `bin/sd-db backup` first, or set {ROOT_VARIABLE} to the folder "
                   "that holds the backup")


def unchanged_since(home: Path | str, directory: Path) -> None:
    """Raise `BackupError` unless the live store's `_fingerprint` is the one
    `directory`'s manifest recorded, exactly (sd:3507).

    The fingerprint was read before the snapshot's view was fixed, so any
    commit the copy lacks changed it since. A manifest without one -- a
    checkpointed snapshot, or one an older library took -- proves nothing and
    is refused, as is a fingerprint that cannot be read.
    """
    database = local_path(home)
    try:
        recorded = json.loads((directory / BACKUP_MANIFEST).read_bytes()).get("store")
        found = _fingerprint(database)
    except (OSError, ValueError, AttributeError) as error:
        raise BackupError(f"migrate refuses: the store's fingerprint could not be read: {error}; "
                          f"{_MIGRATE_REMEDY}") from None
    if recorded is None:
        raise BackupError(f"migrate refuses: the newest backup {directory} records no fingerprint of the store, "
                          f"so it cannot show it holds every write; {_MIGRATE_REMEDY}")
    if recorded != found:
        raise BackupError(f"migrate refuses: the store was written after the newest backup {directory} "
                          f"began; {_MIGRATE_REMEDY}")


def _check_keep(keep: int | None) -> None:
    if keep is not None and (type(keep) is not int or keep < 0):
        raise BackupError("backup retention needs a nonnegative count or no deletion")


def _check_keep_days(days: int | None) -> None:
    # Zero is refused rather than meaning "today only": a retention window is
    # the operator's promise of history, and an empty one is a typo.
    if days is not None and (type(days) is not int or days < 1):
        raise BackupError("backup retention by age needs a positive number of days")


def _directory_order(path: Path) -> tuple[str, int]:
    """The day, then the run within it as a number; a bare day is run 0."""
    return path.name[:10], int(path.name.partition(".")[2] or 0)


def _retention_root(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise BackupError("backup retention root must be an existing unlinked directory")


#: A directory retention has decided to remove, renamed first: a removal
#: stopped halfway leaves this name, which nothing reads as a backup, and the
#: next prune finishes it (`_finish_removals`) instead of keeping half a snapshot.
REMOVING_NAME = re.compile(r"\.(\d{4}-\d{2}-\d{2}(?:\.[1-9]\d*)?)\.removing")


def _contained(directory: Path) -> None:
    """Refuse a linked, mounted or special entry, or a subtree that cannot be read, at or below `directory`.

    `os.walk` with `onerror`, not `rglob`: `rglob` skips a folder it cannot
    list, which would pass the check and stop `rmtree` halfway. The device is
    the parent's, so a `directory` that is itself a mount point is refused too.
    """
    def unreadable(error: OSError) -> None:
        raise BackupError(f"it could not be read: {error}")

    device = directory.parent.stat().st_dev
    if directory.lstat().st_dev != device:
        raise BackupError("it holds a linked, mounted or special entry")
    for folder, folders, files in os.walk(directory, onerror=unreadable):
        for name in (*folders, *files):
            details = os.lstat(os.path.join(folder, name))
            if details.st_dev != device or not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
                raise BackupError("it holds a linked, mounted or special entry")


def _finish_removals(root: Path, *, dry_run: bool = False, report=None) -> list[Path]:
    """Remove what an earlier removal stopped inside; a leftover that fails `_contained` stays, and is named."""
    finished = []
    for path in sorted(root.iterdir()):
        if not REMOVING_NAME.fullmatch(path.name):
            continue
        try:
            if path.is_symlink() or not path.is_dir():
                raise BackupError("it is a link or not a directory")
            _contained(path)
            if not dry_run:
                shutil.rmtree(path)
        except (BackupError, OSError) as error:
            if report is not None:
                report(f"kept {path}: {error}")
            continue
        finished.append(path)
    return finished


def _manifest_identity(directory: Path) -> tuple[int, int, str]:
    """`_check_backup`'s identity without its hashes: the same directory, holding the same manifest bytes."""
    try:
        manifest = directory / BACKUP_MANIFEST
        if directory.is_symlink() or not directory.is_dir() or manifest.is_symlink():
            raise BackupError("it is a link or not a directory")
        material = manifest.read_bytes()
        details = directory.stat()
    except OSError as error:
        raise BackupError(f"it could not be read: {error}") from None
    return details.st_dev, details.st_ino, hashlib.sha256(material).hexdigest()


def _remove_owned(path: Path, identity: tuple[int, int, str], recheck, report=None) -> Path:
    """Remove `path` if `recheck` still reads the `identity` its verification found.

    The caller verifies one directory and removes it before it opens the next,
    so a run stopped partway keeps what it removed (fleet sd:5543). `recheck`
    is the cheap read just before the rename; the hashes are not read twice.
    """
    try:
        again = recheck(path)
    except BackupError:
        again = None
    if again != identity:
        raise BackupError(f"backup changed before retention: {path}; preserved for review")
    doomed = path.with_name(f".{path.name}.removing")
    path.rename(doomed)
    shutil.rmtree(doomed)
    if report is not None:
        report(f"removed {path}")
    return path


def prune(root: Path, keep: int | None = KEEP) -> list[Path]:
    """Apply requested retention only to unchanged, verified owned backups.

    The newest `keep` owned backups are found newest first; each older one is
    then verified and removed in turn, oldest first. A forensic copy among the
    newest is kept but not counted, so it never displaces the restorable
    snapshot the keep exists for (sd:8817).
    """
    _check_keep(keep)
    if keep is None:
        return []
    _retention_root(root)
    _finish_removals(root)
    dated = sorted((path for path in root.iterdir() if DATED_NAME.fullmatch(path.name)), key=_directory_order)
    held = 0
    while dated and held < keep:
        newest = dated.pop()
        if _owned_backup(newest) is not None and not _forensic(newest):
            held += 1
    return [_remove_owned(path, identity, _manifest_identity)
            for path in dated if (identity := _owned_backup(path)) is not None]


def _forensic(directory: Path) -> bool:
    """Whether the verified backup in `directory` is a forensic copy (`run`)."""
    return json.loads((directory / BACKUP_MANIFEST).read_bytes()).get("forensic") is True


def _taken_at(directory: Path) -> datetime:
    return _checkpoint(directory)[0]


def _checkpoint(directory: Path) -> tuple[datetime, bool]:
    """When the backup in `directory` was taken: its checkpoint row's time, read from its own database.

    The flag is True for the current `{"at": ...}` body, False for the bare
    timestamp of a build before sd:3578, so a tie can prefer the current one.

    The name is not the time: a copy can be renamed, and a day's names restart
    on a hub with an empty root. `run` writes the row before the snapshot, so
    the copy holds it.
    """
    try:
        value = json.loads((directory / BACKUP_MANIFEST).read_bytes())
        database = value["database"]
        if not isinstance(database, str) or Path(database).name != database:
            raise ValueError("the manifest names no database file")
        connection = sqlite3.connect(f"file:{directory / database}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT body FROM state WHERE kind = 'checkpoint' AND key = ?",
                                     (value["run_id"],)).fetchone()
        finally:
            connection.close()
        if row is None:
            # A snapshot taken read-only before `migrate` never had a row (sd:4700); it is
            # never owned, so it is never dated or pruned either.
            if value.get("checkpoint") is False:
                raise Retained("it was taken read-only before `migrate`, so no checkpoint row dates it")
            raise BackupError("its database holds no checkpoint row for its run")
        taken, current = _checkpoint_time(row[0])
    except BackupError:
        raise
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        raise BackupError(f"its checkpoint time could not be read: {error}") from None
    if taken.tzinfo is None:
        raise BackupError("its checkpoint time names no zone")
    return taken, current


def _checkpoint_time(body: str) -> tuple[datetime, bool]:
    """A backup run's checkpoint body: `{"at": ...}` (True), or the bare timestamp a build before sd:3578 wrote."""
    try:
        return datetime.fromisoformat(json.loads(body)["at"]), True
    except json.JSONDecodeError:
        return datetime.fromisoformat(body), False


def prune_older(root: Path, days: int, *, now: datetime, moved: bool = False, dry_run: bool = False,
                unmanifested: bool = False, report=None) -> list[Path]:
    """Delete owned backups taken more than `days` days before `now`.

    Age comes from the directory's day, which `_dated_directory` takes from
    the run's own clock, so it is compared in that clock's zone. A day is
    removed only when the whole of it lies more than `days` days back:
    `D < (now - days).date()`. Every removed snapshot is therefore older than
    the window, and none younger is touched; one dated exactly `days` days
    ago is kept, until the next day. So a window of 7 holds between 7 and 8
    days of runs. Counting runs instead (`--keep 168`) would hold a week
    only while every hourly run happens; a Mac asleep overnight makes 168
    runs reach further back, and one that ran twice an hour makes it fall
    short.

    Only a directory whose day is past the window is checked -- the cheap
    name test first -- so an hourly run opens the few snapshots it may
    delete, not the whole week. The check is ownership, not restorability
    (sd:5682): the manifest and the listing must match (`owned_only`), but no
    file is hashed and the database is not verified. Hashing each copy on the
    share took minutes, so the job's bound removed 2 an hour while 24 aged
    out. Each is checked and removed before the next is opened, oldest
    first, so a run the bound stops keeps what it removed (fleet sd:5543).

    `moved` prunes copies away from where they were written, such as the
    hourly mirror on a share (sd:4011): the manifest's path proves nothing
    there, and its checkpoint time must also lie past the window, so a copy
    named for an older day than its data stays.

    A copy whose checkpoint body is the bare timestamp of a build before
    sd:3578 is dated and pruned like any other (sd:4694). A directory with no
    manifest always stays: a run that stopped before writing one leaves such a
    directory, but so can a hand-kept copy of the store, and nothing in it
    tells the two apart -- unless `unmanifested` asks otherwise, below. Every
    directory past the window that stays, `report` hears with its reason, and
    every one removed; one kept by design (`Retained`) is counted on one line
    per reason (sd:5065). `dry_run` returns what would go and removes nothing.

    `unmanifested` (sd:4917, an operator ruling) also removes a dated
    directory with no manifest whose day lies more than twice `days` back, by
    the same day rule: a copy stopped partway on a mirror is never finished
    once its source has rotated out. It is for a moved root only, and refuses
    one whose manifests do not all prove it holds copies (`_moved_root`). Such a directory
    goes only if a lookup shows it holds no manifest (`_holds_manifest`; a
    lookup that fails keeps it) and it is a real directory holding no link,
    mount or special entry (`_contained`), so nothing outside the root is
    reached.
    """
    _check_keep_days(days)
    _retention_root(root)
    if unmanifested and not moved:
        raise BackupError("the unmanifested prune takes only a moved root, such as a mirror")
    if unmanifested:
        _moved_root(root)
    try:
        cutoff = (now - timedelta(days=days)).date()
    except OverflowError:
        raise BackupError(f"a window of {days} days starts before the calendar; name a smaller one") from None
    try:
        stale = (now - timedelta(days=2 * days)).date()
    except OverflowError:
        stale = date.min  # twice the window starts before the calendar: no folder is that old
    window = now - timedelta(days=days)
    for path in _finish_removals(root, dry_run=dry_run, report=report):
        if report is not None:
            report(f"{'would finish' if dry_run else 'finished'} removing {path}, which an earlier prune stopped inside")

    def identify(path: Path) -> tuple[int, int, str] | None:
        """The identity checked again before removal; None for a directory inside the window."""
        if not _holds_manifest(path):
            if not unmanifested or datetime.strptime(path.name[:10], "%Y-%m-%d").date() >= stale:
                raise Retained("no manifest (stopped run?)")
            if path.is_symlink() or not path.is_dir():
                raise BackupError("it is a link or not a directory")
            try:
                _contained(path)
                details = path.lstat()
            except OSError as error:
                raise BackupError(f"it could not be read: {error}") from None
            return details.st_dev, details.st_ino, ""
        if moved and _taken_at(path) >= window:
            return None
        return _check_backup(path, moved=moved, owned_only=True)

    def unchanged(path: Path) -> tuple[int, int, str] | None:
        """`identify` again just before the rename, with no second hash of a copy."""
        return _manifest_identity(path) if _holds_manifest(path) else identify(path)

    removed = []
    retained: dict[str, list[Path]] = {}
    try:
        for path in sorted((path for path in root.iterdir() if DATED_NAME.fullmatch(path.name)),
                           key=_directory_order):
            try:
                day = datetime.strptime(path.name[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if day >= cutoff:
                continue
            try:
                identity = identify(path)
            except Retained as error:
                retained.setdefault(str(error), []).append(path)
                continue
            except BackupError as error:
                if report is not None:
                    report(f"kept {path}: {error}")
                continue
            if identity is not None:
                removed.append(path if dry_run else _remove_owned(path, identity, unchanged, report))
    finally:
        for reason, kept in retained.items():
            if report is not None:
                names = [path.name for path in kept]
                named = names[0] if len(names) == 1 else f"{names[0]} to {names[-1]}"
                report(f"kept {len(names)} backup{'' if len(names) == 1 else 's'} under {root}, "
                       f"named {named}: {reason}")
    return removed


def _holds_manifest(path: Path) -> bool:
    """Whether `path` holds a manifest entry; `BackupError` when a lookup cannot tell.

    Only "no such file" is an absence. `Path.exists` answers False for any
    error, such as a folder that can be listed but not searched, which would
    read a folder nobody could inspect as one with no manifest.
    """
    try:
        os.lstat(path / BACKUP_MANIFEST)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise BackupError(f"it could not be read: {error}") from None
    return True


def _moved_root(root: Path) -> None:
    """Refuse `root` unless every manifest in it proves it holds copies, not what `run` writes.

    The proof is positive and covers the whole root, not its newest copy: a
    root that once took a backup in place still holds that one's manifest.
    Every dated, unlinked folder is looked up, and each manifest must be
    readable and unlinked, with a `directory` that is an absolute path whose
    parent is not `root`. At least one manifest must exist. A failed lookup,
    a manifest naming no such directory, or a root with none is refused; an
    absence proves nothing.
    """
    def refuse(reason: str) -> BackupError:
        return BackupError(f"the unmanifested prune could not tell whether {root} holds copies: {reason}")

    try:
        here = root.resolve()
    except (OSError, RuntimeError) as error:
        raise refuse(f"it could not be resolved: {error}") from None
    seen = 0
    for path in sorted((path for path in root.iterdir() if DATED_NAME.fullmatch(path.name)), key=_directory_order):
        if path.is_symlink():
            continue  # a link names a folder elsewhere; `run` writes none
        try:
            if not _holds_manifest(path):
                continue
        except BackupError as error:
            raise refuse(f"{path}: {error}") from None
        manifest = path / BACKUP_MANIFEST
        if manifest.is_symlink():
            raise refuse(f"{manifest} is a link")
        try:
            written = json.loads(manifest.read_bytes()).get("directory")
        except (OSError, ValueError, AttributeError) as error:
            raise refuse(f"{manifest} could not be read: {error}") from None
        if not isinstance(written, str) or not written or not Path(written).is_absolute():
            raise refuse(f"{manifest} names no directory it was written in")
        try:
            origin = Path(written).resolve()
        except (OSError, RuntimeError) as error:
            raise refuse(f"{written} could not be resolved: {error}") from None
        if origin.parent == here:
            raise BackupError(f"the unmanifested prune takes only a moved root, such as a mirror; {root} holds "
                              f"backups written in place ({path} names {written})")
        seen += 1
    if not seen:
        raise refuse(f"no folder under {root} has a manifest")


def _mount_point(path: Path) -> Path:
    """The mount serving `path`: the path itself if it is one, else an ancestor."""
    point = path.resolve()
    while point != point.parent and not os.path.ismount(point):
        point = point.parent
    return point


def require_mount(mount: Path, destination: Path,
                  remedy: str = "attach the drive and run the backup again") -> None:
    """Refuse unless `mount` is mounted and `destination` is written onto it.

    `/Volumes/local` with its disk detached is an ordinary directory on the
    boot disk, and `mkdir(parents=True)` would recreate it there: the
    snapshot would then restore and compare perfectly on the disk it was
    meant to leave. So the mount is asked first, and then where the
    destination -- or the nearest part of it that exists, which is where the
    writer will create the rest -- really lands, links resolved. Nothing is
    written before this passes: no checkpoint row and no directory.
    """
    mount = Path(mount).expanduser()
    if not os.path.ismount(mount):
        raise BackupError(
            f"{mount} is not a mounted volume, so {destination} would be written onto this "
            f"machine's own disk; {remedy}"
        )
    existing = Path(destination).expanduser()
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    served = _mount_point(existing)
    if served != mount.resolve():
        raise BackupError(
            f"{destination} is served by {served}, not by the mounted volume {mount}; "
            "refusing to write the backup there"
        )


def probe_destination(root: Path) -> None:
    """Refuse unless a test write under `root` finishes within `PROBE_SECONDS`.

    On some nights macOS stops answering permission checks for launchd jobs.
    Each open then blocks for seconds and fails with EINTR, Python retries it
    (PEP 475), and the backup ran until the job's two-hour limit killed it
    with nothing said (sd:2660). The write runs in a child, so an open that
    never returns holds the child and not this process. A probe that fails at
    once is not this check's to name: the writes below fail the same way and
    say which step did.
    """
    try:
        child = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _PROBE, str(root)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise BackupError(f"the probe of backup destination {root} did not start: {error}") from None
    try:
        child.wait(timeout=PROBE_SECONDS)
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise BackupError(
            f"backup destination {root} did not answer within {PROBE_SECONDS:g} s"
        ) from None


def run(
    *,
    home: Path | str,
    database: Path | str | None = None,
    destination: Path | str | None = None,
    keep: int | None = None,
    keep_days: int | None = None,
    mount: Path | str | None = None,
    when: datetime | None = None,
) -> Snapshot:
    """Take a snapshot, restore it, and compare. Raises on anything else.

    `keep` retains that many owned backups; `keep_days` retains owned backups
    by age (`prune_older`). They are two answers to one question, so asking
    both is refused. `mount` names the volume the destination must be on,
    checked before anything is written (`require_mount`). The destination
    must then finish a test write in time (`probe_destination`).
    """
    _check_keep(keep)
    if keep == 0:
        # The snapshot this run takes is one of the `keep` it retains, so 0
        # deletes it and the row prune then refuses a backup that is gone.
        raise BackupError("backup retention keeps at least 1: the count includes this run's backup")
    _check_keep_days(keep_days)
    if keep is not None and keep_days is not None:
        raise BackupError("backup retention takes a count or an age, not both")
    home = Path(home)
    # Before the mount check and the probe: a satellite writes nothing.
    refuse_hub_only(database if database is not None else local_path(home), "the backup", home)
    when = when or datetime.now(UTC)
    root = (
        Path(destination).expanduser()
        if destination is not None
        else backup_root(home)
    )
    if mount is not None:
        require_mount(Path(mount), root)
    elif destination is None and not os.environ.get(ROOT_VARIABLE):
        # The default root is on a disk that may be detached. Falling back to
        # the home would hide that, so a run names the fix and writes nothing.
        require_mount(DEFAULT_MOUNT, root, "mount the disk or pass --destination PATH")
    probe_destination(root)
    run_id = uuid.uuid4().hex

    checkpointed = True
    try:
        try:
            connection = connect(database, home=home)
        except SchemaTooOld:
            # A database waiting for `migrate` is exactly the one to snapshot
            # first, and the writable open is what refuses it. Read-only,
            # `mode=ro` keeps every write off the source; `_snapshot` only
            # reads it. The checkpoint row is a write, so it is not taken and
            # the copy is proved by counts only.
            connection = connect(database, home=home, write=False)
            checkpointed = False
    except (OSError, sqlite3.Error, SdDbError, ValueError) as error:
        raise BackupError(f"the database did not open: {error}") from None

    try:
        source = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        if checkpointed:
            try:
                record_state(connection, "checkpoint", key=run_id, body={"at": when.isoformat()})
            except sqlite3.Error as error:
                raise BackupError(f"the checkpoint row was not written: {error}") from None
        try:
            root.mkdir(parents=True, exist_ok=True)
            directory = _dated_directory(root, when)
            directory.mkdir()
        except OSError as error:
            raise BackupError(f"the backup directory was not created: {error}") from None

        copy = directory / source.name
        # A read-only run writes nothing to the source, so its fingerprint
        # still describes the source when the run ends.
        expected, store = _snapshot(connection, copy, source=None if checkpointed else source)
    finally:
        connection.close()

    copied = []
    if _copy_publications(source, directory, expected):
        copied.append("publications")
    if _copy_runner_journal(source, directory, expected):
        copied.append("runner-journal")
    copied.extend(_copy_recovery_evidence(source, directory))
    for name in CONFIGURATION:
        beside = home / ".local/share/sd" / name
        if not beside.exists():
            continue
        try:
            shutil.copy2(beside, directory / name)
        except OSError as error:
            raise BackupError(f"{name} was not copied beside the snapshot: {error}") from None
        copied.append(name)

    from .runner_exec_backup import capture as capture_executions
    if capture_executions(source, directory):
        copied.extend(("executions", "execution-evidence.json"))

    counts = verify(copy, run_id=run_id, expected=expected, checkpointed=checkpointed)
    checked = connect(copy, write=False)
    try:
        # The referential check, and it runs on the COPY. The first reason is
        # correctness and not taste: the source's read transaction ended with
        # the copy (`_snapshot`), so a check on the source would answer for a
        # later image than the one on disk here -- a writer landing in between
        # turns a valid row into an orphan the check never saw, and the table
        # counts `verify` compares stay equal while it happens. The copy
        # cannot move. The second reason is that the copy is the image
        # `restore` would consume, so it is the one the answer is about.
        #
        # `verify` runs `integrity_check` on this same copy, but that pragma
        # asks whether the b-tree pages are well formed, not whether rows
        # point at rows that exist -- a database of nothing but orphans passes
        # it, and the snapshot copies orphans faithfully. Measured: sd:744's
        # six orphan rows survived the nightly backups of 2026-09-11, -12 and
        # -13, each of which reported "restored and compared". The check the
        # outbound path was missing already existed in this file, at
        # `_check_restore`, guarding the inbound one.
        #
        # It reports and does not raise, which is the deliberate half: a store
        # that has just gone referentially bad is the one whose bytes are most
        # worth keeping, and refusing to copy it destroys the only record of
        # what it looked like before somebody repaired it. The count reaches
        # the operator through the job's summary line instead.
        #
        # WHAT THAT SNAPSHOT IS, STATED EXACTLY, because the first version of
        # this comment called it "the recovery path" and that was wrong.
        # `restore` refuses it. `_check_restore` below runs the same pragma on
        # the candidate and raises at the `foreign_key_check` arm, so a
        # snapshot of a broken source is a FORENSIC copy and not a restorable
        # one, and it stays that way until the source is repaired and a later
        # night copies a clean store. `test_a_snapshot_of_a_broken_source_is_not
        # _restorable` pins that, so the contract is tested and not merely
        # asserted here. The value of taking it anyway is the pre-repair
        # record: the orphans are queryable in the copy after somebody deletes
        # them from the live database, which is how sd:744 was reconstructed.
        try:
            violations = [tuple(row) for row in
                          checked.execute("PRAGMA foreign_key_check")]
        except sqlite3.Error as error:
            # A foreign-key MISMATCH -- a constraint naming a column that is
            # not a key -- raises here rather than returning rows. That is a
            # schema fault, not a data one, and it is not something a summary
            # line can carry, so it takes the module's documented exit.
            raise BackupError(f"the referential check did not run: {error}") from None
        _check_publication_claims(checked, directory)
        _check_runner_records(checked, directory)
    finally:
        checked.close()

    try:
        _record_backup(directory, source, run_id, counts, checkpointed=checkpointed, store=store,
                       forensic=bool(violations))
    except OSError as error:
        raise BackupError(f"backup ownership was not recorded: {error}") from None
    try:
        if keep_days is not None:
            removed = prune_older(root, keep_days, now=when)
        else:
            removed = prune(root, keep) if keep is not None else []
    except OSError as error:
        raise BackupError(f"old snapshots were not removed: {error}") from None

    return Snapshot(
        directory=directory,
        run_id=run_id,
        counts=counts,
        removed=removed,
        configuration=copied,
        checkpointed=checkpointed,
        violations=violations,
    )


def _schema_objects(connection: sqlite3.Connection, table_names: list[str]) -> dict[tuple[str, str], tuple]:
    """Each table's indexes by shape (unique, origin, partial, key columns),
    and every trigger and view by the table it is on."""
    shape: dict[tuple[str, str], tuple] = {}
    for table in table_names:
        for _, name, unique, origin, partial in connection.execute(f"PRAGMA index_list({table})"):
            columns = tuple(row[2] for row in connection.execute(f'PRAGMA index_info("{name}")'))
            shape[("index", name)] = (table, unique, origin, partial, columns)
    for kind, name, table in connection.execute(
        "SELECT type, name, tbl_name FROM sqlite_master WHERE type IN ('trigger', 'view')"
    ):
        shape[(kind, name)] = (table,)
    return shape


def _check_restore(connection: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> dict[str, int]:
    """Check the complete candidate, including a misleading version number."""
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise BackupError(f"restore candidate failed integrity_check: {result}")
    if schema_version(connection) != version or not 1 <= version <= SCHEMA_VERSION:
        raise BackupError("restore candidate claims an unsupported schema version")
    reference = sqlite3.connect(":memory:")
    try:
        # 014 calls the functions `migrate` registers; without them the
        # reference fails with "no such function" before comparing anything.
        paths.install(reference)
        for number, path in migrations():
            if number <= version:
                reference.executescript(path.read_text(encoding="utf-8"))
        expected_tables = tables(reference)
        if set(tables(connection)) != set(expected_tables):
            raise BackupError("restore candidate has an incompatible table set")
        for table in expected_tables:
            actual = [tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")]
            expected = reference.execute(f"PRAGMA table_info({table})").fetchall()
            if actual != expected:
                raise BackupError(f"restore candidate has incompatible columns in {table}")
        # Tables and columns alone pass a store missing an index or a trigger:
        # `record_check` upserts through `runner_check`, and a trigger holds
        # the publication payload immutable.
        wanted, found = _schema_objects(reference, expected_tables), _schema_objects(connection, expected_tables)
        changed = sorted(name for kind, name in wanted.keys() | found.keys()
                         if wanted.get((kind, name)) != found.get((kind, name)))
        if changed:
            raise BackupError("restore candidate has incompatible indexes or triggers: " + ", ".join(changed))
    finally:
        reference.close()
    # A snapshot taken by `run` of a source that was already broken arrives
    # here, because `run` reports those violations and copies the store
    # anyway. Refusing is right -- restoring known-orphaned rows over a live
    # database is not a recovery -- and the message names the count and the
    # tables, or the operator cannot tell what it is looking at.
    #
    # It does NOT say the source was broken when the snapshot was taken, which
    # an earlier version did. This check sees the candidate and nothing else:
    # the same rows would appear if the file was damaged in storage or in
    # transit afterwards. Nothing in the directory records the finding `run`
    # made, so that history is unavailable here and the message stays inside
    # what was actually observed, naming the forensic case as the likely one.
    broken = connection.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        where = ", ".join(sorted({str(row[0]) for row in broken}))
        raise BackupError(
            f"restore candidate has {len(broken)} foreign key violation(s), in {where}, "
            f"so it is not a restore point; `run` copies a broken source deliberately, "
            f"so a forensic snapshot is the usual cause, but this check observes only "
            f"the candidate and cannot tell that from later damage"
        )
    return _counts(connection)


@dataclass(frozen=True)
class RestoreCheck:
    """What `restore`'s validator makes of one database file.

    `refusal` is `None` exactly when the file is a restore point, and carries
    the validator's own words otherwise -- a file that will not open included,
    because a backup that cannot be opened is refused by any other name.
    `counts` is what the caller compares against whatever manifest it holds;
    this module does not know what that manifest says.
    """

    counts: dict[str, int]
    version: int | None
    refusal: str | None

    @property
    def accepted(self) -> bool:
        return self.refusal is None


def check_restorable(database: Path | str) -> RestoreCheck:
    """Ask `restore`'s own question of one database file, and report the answer.

    `restore` stages a whole snapshot directory. A caller holding the database
    alone -- `local-mirror-sync/offsite-verify.py`, which pulls one back off
    the share -- asks the same question of the file, and asks it here because
    nothing outside this library opens the database. Keeping a second, weaker
    contract out there is the failure an unverified backup is made of: a
    verifier that passes what `restore` would refuse reports a good backup for
    a snapshot nobody can restore.

    Nothing is raised. Every refusal is an outcome this caller prints beside
    the checks it ran, not an exception that would end the run before the
    remaining ones report.

    The version is the file's own. `restore` validates a snapshot against the
    shape it claims and migrates a staged copy forward, so checking against
    today's schema instead would refuse every snapshot taken before the last
    migration -- which is most of what a share holds. A file claiming a
    version this build does not have reaches the validator against this
    build's, and is refused there for claiming it.
    """
    path = Path(database)
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as error:
        return RestoreCheck({}, None, f"the file does not open as a database: {error}")
    version: int | None = None
    try:
        try:
            version = schema_version(connection)
            counts = (_check_restore(connection, version=version) if version < SCHEMA_VERSION
                      else _check_restore(connection))
        except (BackupError, sqlite3.Error) as error:
            return RestoreCheck({}, version, str(error))
        return RestoreCheck(counts, version, None)
    finally:
        connection.close()


def _upgrade_restore_candidate(candidate: Path) -> None:
    """Validate the claimed old shape before migrating only the staged copy."""
    connection = connect(candidate, write=False)
    try:
        version = schema_version(connection)
        before = (_check_restore(connection, version=version) if version < SCHEMA_VERSION
                  else _check_restore(connection))
    finally:
        connection.close()
    if version == SCHEMA_VERSION:
        return
    migrate_database(candidate)
    connection = connect(candidate, write=False)
    try:
        after = _check_restore(connection)
        if any(after.get(table) != count for table, count in before.items()):
            raise BackupError("restore migration changed existing table counts; keep the original snapshot and inspect the migration")
    finally:
        connection.close()


class _InstalledButUnverified(BackupError):
    """The SQLite commit landed but its post-check could not be completed."""


def _install_restore(candidate: Path, target: Path, expected: dict[str, int]) -> None:
    """SQLite owns the destination and its WAL; never replace or unlink them.

    Its backup API holds one destination transaction and rolls it back when
    copying aborts before SQLITE_DONE. Existing connections therefore see a
    coherent replacement, and failure cannot strand committed WAL data.
    https://sqlite.org/c3ref/backup_finish.html
    """
    source = connect(candidate, write=False)
    try:
        destination = sqlite3.connect(target, isolation_level=None, timeout=0)
        destination.row_factory = sqlite3.Row
        try:
            deadline = time.monotonic() + RESTORE_SECONDS

            def progress(status: int, remaining: int, total: int) -> None:
                # SQLITE_DONE has already committed; never report a timeout
                # after success and imply the old database was retained.
                if status != sqlite3.SQLITE_DONE and time.monotonic() >= deadline:
                    raise BackupError("restore timed out; stop database writers and retry")

            source.backup(destination, pages=128, progress=progress, sleep=0.05)
            try:
                if _check_restore(destination) != expected:
                    raise BackupError("restored table counts differ from the validated candidate")
            except (BackupError, sqlite3.Error, OSError) as error:
                raise _InstalledButUnverified(
                    f"restore was installed but its verification failed: {error}; "
                    "keep services stopped and inspect the database"
                ) from error
        finally:
            destination.close()
    finally:
        source.close()


def _release_leases(connection: sqlite3.Connection) -> list[str]:
    """Release every open runner lease and note each run abandoned at promotion (sd:4011).

    A lease names a supervisor pid on the hub that took the snapshot; a pid
    check on this machine could match an unrelated process. The run rows stay
    as they are, so they still agree with the runner journal restored beside them.
    """
    with transaction(connection):
        # A build run's lease stays: `build.abandon` ended the run, and a
        # reap releases it once its process groups read gone (pack sd:6093).
        rows = list(connection.execute(
            "SELECT lease.run, assignment.item FROM runner_lease lease "
            "LEFT JOIN runner_run run ON run.id = lease.run LEFT JOIN assignment ON assignment.id = run.assignment "
            "WHERE lease.released_at IS NULL AND assignment.scope IS NOT 'build' ORDER BY lease.run"))
        connection.executemany("UPDATE runner_lease SET released_at = ? WHERE run = ? AND released_at IS NULL",
                               [(stamp_now(), row["run"]) for row in rows])
        for row in rows:
            if row["item"] is not None:
                add_note(connection, row["item"], "comment",
                         f"Runner run {row['run']} abandoned at promotion; its lease is released")
    return [row["run"] for row in rows]


def restore(directory: Path | str, *, home: Path | str, origin: Path | None = None,
            settle: bool = False) -> Path:
    """Put a dated directory's database and configuration back in place.

    A restored database is a record, not a permission: this writes the
    `restore` row and leaves it unresolved. While it is unresolved the runner
    dispatches nothing and the palette runs nothing -- enforced by the
    callers that read it, which is the only place that can enforce it.

    `origin` is the path the row records when `directory` is a local copy of
    it; `settle` releases the old hub's open leases (`_release_leases`).
    """
    from .operations import control_gate

    directory = Path(directory)
    home = Path(home)
    refuse_hub_only(local_path(home), "the restore", home)
    snapshot = directory / "sd.db"
    if not snapshot.exists():
        raise BackupError(f"{directory} holds no sd.db")
    # Backups produced by run() are closed single-file snapshots. A
    # nonempty WAL means somebody handed us a live/copy-in-progress database,
    # whose main file alone is not the snapshot they asked to restore.
    wal = snapshot.with_name(snapshot.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise BackupError("restore source has a nonempty WAL; take a completed backup first")
    state = home / ".local/share/sd"
    target = state / "sd.db"
    stage = Path(tempfile.mkdtemp(prefix="sd-restore-"))
    keep_stage = False
    try:
        candidate = stage / "sd.db"
        shutil.copy2(snapshot, candidate)
        _upgrade_restore_candidate(candidate)
        publications = _prepare_publications(directory, state, stage)
        runner_journal = _prepare_runner_journal(directory, state)
        connection = connect(candidate)
        try:
            _check_publication_claims(connection, directory)
            _check_runner_records(connection, directory)
            from .runner_exec_backup import prepare as prepare_executions
            executions = prepare_executions(directory, state, connection)
            from .recovery import prepare_restore
            prepare_restore(connection)
            body: dict[str, object] = {"from": str(origin or directory), "restored_at": datetime.now(UTC).isoformat()}
            if settle:
                body["released"] = _release_leases(connection)
            record_state(connection, "restore", key=directory.name, body=body)
            expected = _check_restore(connection)
        finally:
            connection.close()

        configuration = []
        for name in CONFIGURATION:
            beside = directory / name
            if not beside.exists():
                continue
            current = state / name
            if beside.is_symlink() or current.is_symlink():
                raise BackupError(f"restore refuses a configuration symlink: {name}")
            prepared = stage / f"new-{name}"
            shutil.copy2(beside, prepared)
            original = stage / f"old-{name}" if current.exists() else None
            if original is not None:
                shutil.copy2(current, original)
            configuration.append((current, prepared, original))

        # Validation above is read-only for the live state. Hold the same gate
        # as launchctl controls throughout live journal/config/database changes.
        with control_gate(target):
            for current, _, original in configuration:
                changed = current.is_symlink()
                if original is None:
                    changed = changed or current.exists()
                else:
                    changed = changed or not current.is_file() or current.read_bytes() != original.read_bytes()
                    if not changed:
                        changed = current.stat().st_mode != original.stat().st_mode
                if changed:
                    raise BackupError(f"configuration changed while staging restore: {current.name}; retry from current state")
            state.mkdir(parents=True, exist_ok=True)
            _restore_publications(publications, state, target)
            _restore_runner_journal(runner_journal, state)
            from .runner_exec_backup import install as install_executions
            install_executions(executions, state)
            installed = []
            try:
                for current, prepared, original in configuration:
                    # Stage may be on a different filesystem. Copy beside the
                    # destination before the atomic replacement.
                    local = state / f".restore-{uuid.uuid4().hex}"
                    try:
                        shutil.copy2(prepared, local)
                        os.replace(local, current)
                    finally:
                        local.unlink(missing_ok=True)
                    installed.append((current, original))
                _install_restore(candidate, target, expected)
            except _InstalledButUnverified:
                # The DB commit succeeded. Keep matching new configuration and
                # report the boundary honestly rather than roll back half of it.
                raise
            except BaseException as failure:
                try:
                    for current, original in reversed(installed):
                        if original is None:
                            current.unlink()
                        else:
                            shutil.copy2(original, current)
                except OSError as rollback:
                    keep_stage = True
                    raise BackupError(
                        f"restore failed ({failure}); configuration rollback failed ({rollback}); "
                        f"original copies retained at {stage}"
                    ) from rollback
                raise
    except (sqlite3.Error, OSError, SdDbError, ValueError) as error:
        if isinstance(error, BackupError):
            raise
        raise BackupError(f"restore refused before completion: {error}") from error
    finally:
        if not keep_stage:
            shutil.rmtree(stage)
    return target


@dataclass
class Newest:
    """What `restore_newest` restored, and what it refused on the way."""

    snapshot: Path
    taken_at: datetime
    target: Path
    refused: list[tuple[Path, str]]
    released: list[str]


@dataclass
class Restorable:
    """What `newest_restorable` found, restoring nothing (pack sd:6323).

    `snapshot` is the one `restore_newest` would restore, or `None` when none
    passes. `refused` says why each newer one failed, an unreadable root and
    a run stopped before its manifest included. `newer` is `(path, version)`
    when the walk stopped at a snapshot of a schema newer than this library,
    as `restore_newest` stops there; `snapshot` is then `None`. `released`
    is the runs whose leases a restore of `snapshot` would release.
    """

    snapshot: Path | None
    taken_at: datetime | None
    refused: list[tuple[Path, str]]
    released: list[str]
    newer: tuple[Path, int] | None = None


def newest_restorable(roots: Sequence[Path | str], *, report=None) -> Restorable:
    """The newest snapshot under `roots` that `restore_newest` would restore, judged by its checks.

    The one walk `restore_newest` runs, less the restore, so a readiness
    check reads the same answer the restore acts on: system's hub failover
    asks it for the backup age. It writes nothing outside its temporary
    copies, so it runs off the hub too. A root it cannot read is a refusal,
    not an empty root.
    """
    return _walk_newest(roots, report=report)[0]


def _walk_newest(roots: Sequence[Path | str], *, report=None,
                 install: Callable[[Path, Path], Path] | None = None) -> tuple[Restorable, Path | None]:
    """Judge the candidates under `roots`, newest checkpoint first, and stop at the first that passes.

    Each candidate is copied off its share first and judged on the copied
    bytes: its manifest's hashes and its database's checkpoint
    (`_check_backup`), then `restore`'s own validator (`check_restorable`).
    `install` gets the passing copy and its origin while the copy exists,
    and its answer is the second value; without it nothing is restored.
    """
    refused: list[tuple[Path, str]] = []

    def refuse(path: Path, reason: str) -> None:
        refused.append((path, reason))
        if report is not None:
            report(f"refused {path}: {reason}")

    candidates = []
    for root in map(Path, roots):
        try:
            _retention_root(root)
            entries = sorted(root.iterdir())
        except (BackupError, OSError) as error:
            refuse(root, str(error))
            continue
        # A folder with no manifest is one line for the lot, not one each (sd:4694).
        stopped: list[Path] = []
        for path in entries:
            if path.is_symlink() or not DATED_NAME.fullmatch(path.name):
                continue
            try:
                # On a tie the current body goes first, then the later name (sd:4694).
                taken_at, current = _checkpoint(path)
                candidates.append((taken_at, current, _directory_order(path), path))
            except BackupError as error:
                if (path / BACKUP_MANIFEST).exists() or (path / BACKUP_MANIFEST).is_symlink():
                    refuse(path, str(error))
                else:
                    stopped.append(path)
                    refused.append((path, "no manifest (stopped run?)"))
        if stopped and report is not None:
            names = sorted((path.name for path in stopped), key=lambda name: _directory_order(Path(name)))
            report(f"refused {len(stopped)} snapshot(s) under {root} with no manifest (stopped runs?), "
                   f"named {names[0]} to {names[-1]}")
    for taken_at, _, _, path in sorted(candidates, reverse=True):
        with tempfile.TemporaryDirectory(prefix="sd-newest-") as stage:
            local = Path(stage) / path.name
            try:
                shutil.copytree(path, local, symlinks=True)
                # The hashes first: every later decision, the schema stop included, reads these bytes.
                if _manifest_entries(json.loads((local / BACKUP_MANIFEST).read_bytes()).get("entries"),
                                     moved=True) != _retention_inventory(local, moved=True):
                    raise BackupError("its files differ from its manifest")
            except (BackupError, OSError, ValueError, AttributeError) as error:
                refuse(path, str(error))
                continue
            check = check_restorable(local / "sd.db")
            # Before the full manifest check: this library cannot count a newer schema's tables.
            if check.version is not None and check.version > SCHEMA_VERSION:
                return Restorable(None, None, refused, [], newer=(path, check.version)), None
            try:
                _check_backup(local, moved=True)
                if not check.accepted:
                    # Such as a forensic snapshot of a source with broken references.
                    raise BackupError(f"restore refuses it: {check.refusal}")
                raw = sqlite3.connect(f"file:{local / 'sd.db'}?mode=ro", uri=True)
                try:
                    released = [row[0] for row in raw.execute(
                        "SELECT run FROM runner_lease WHERE released_at IS NULL ORDER BY run")]
                finally:
                    raw.close()
            except (BackupError, sqlite3.Error) as error:
                refuse(path, str(error))
                continue
            found = Restorable(path, taken_at, refused, released)
            return found, (install(local, path) if install is not None else None)
    return Restorable(None, None, refused, []), None


def restore_newest(roots: Sequence[Path | str], *, home: Path | str, report=None) -> Newest:
    """Restore the newest verified snapshot under `roots`, newest checkpoint first (sd:4011).

    `newest_restorable`'s walk picks it. A refused candidate is reported and
    the next is tried. A candidate newer than this library stops the run: an
    older snapshot would lose what the newer one holds. So does any failure
    of the restore itself, which may already have changed live state, or
    conflicts with live evidence that an older snapshot would conflict with
    too.
    """
    home = Path(home)
    refuse_hub_only(local_path(home), "the restore", home)
    found, target = _walk_newest(
        roots, report=report,
        install=lambda local, origin: restore(local, home=home, origin=origin, settle=True))
    if found.newer is not None:
        path, version = found.newer
        raise BackupError(f"{path} is schema {version}, newer than this library ({SCHEMA_VERSION}); "
                          "update the pack and retry")
    if found.snapshot is None or found.taken_at is None or target is None:
        raise BackupError(f"no snapshot under {', '.join(map(str, roots))} restored ({len(found.refused)} refused)")
    return Newest(found.snapshot, found.taken_at, target, found.refused, found.released)
