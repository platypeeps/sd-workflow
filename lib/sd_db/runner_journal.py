"""Out-of-database ownership evidence survives restoring an older sd.db.

This directory is backed up beside the database, but restoring sd.db must
never roll this directory backwards. A run absent from the DB is a hold,
not permission to dispatch or to reconstruct invented progress.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from . import paths
from .runner import RunnerRefused


def directory(database: Path) -> Path:
    return database.parent / "runner-journal"


def restore_pending(database: Path) -> bool:
    return (database.parent / "runner-restore-intent.json").exists()


#: The columns migrations added to `runner_run` after its journal format,
#: which a record written before each lacks and reads as NULL: 018's
#: `detached_from` (sd:2581) and 042's build columns (pack sd:6093). A runner
#: run never writes the build columns, so its row holds NULL in each too.
LATER_COLUMNS = ("detached_from", "step", "heartbeat_at", "model", "session_id",
                 "log_path", "builder_pgid", "builder_start", "review_round", "final", "doubts")


def canonical(record: dict) -> dict:
    """The record with `repo` in the form migration 014 gives the row (sd:1447).

    014 rewrote `runner_run.repo` to the `~/` key and left this journal as
    history, so a record written before it names the absolute path of the
    same repository. Every comparison of a record with a row, or with another
    record, passes both sides through here, so two spellings of one
    repository agree and a different repository still differs. The file is
    not rewritten here: the next `persist` writes the row, which holds the
    key. The conversion is `paths.home_relative`, the function 014 ran, so
    both sides agree byte for byte; a path outside the home, or a record
    compared without `$HOME`, is kept as it is.
    """
    missing = [name for name in LATER_COLUMNS if name not in record]
    if missing:
        # Written before the migration that added the column: 018 or 042.
        record = {**record, **dict.fromkeys(missing)}
    repo = record.get("repo")
    try:
        keyed = paths.home_relative(repo) if isinstance(repo, str) else repo
    except paths.PathRefused:
        return record
    return record if keyed == repo else {**record, "repo": keyed}


def _bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


#: What `F_FULLFSYNC` raises on a file system that cannot take it; `fsync` is the most it has.
_NO_FULL_SYNC = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOTTY})


def full_sync(descriptor: int) -> None:
    """Flush `descriptor` past the drive's own cache: `F_FULLFSYNC` where the platform has it, else `fsync`.

    macOS `fsync` stops at the drive's write cache. It lives here because
    this module is the one fcntl importer. A file system that refuses the
    full sync gets `fsync`; any other fault is raised, not downgraded.
    """
    command = getattr(fcntl, "F_FULLFSYNC", None)
    if command is not None:
        try:
            fcntl.fcntl(descriptor, command)
            return
        except OSError as error:
            if error.errno not in _NO_FULL_SYNC:
                raise
    os.fsync(descriptor)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def lock(path: Path, *, blocking=True, noun="runner", held=None, error=RunnerRefused, wait=0.0, poll=0.5):
    """Take the lock at `path`, never through a link at its final component.

    The one lock opener both packages share. The file is opened without
    following a link at the last component, checked on the open descriptor (a
    regular file owned by this user, with one link), and the flock is taken on
    that same descriptor, so nothing can be swapped in between the check and
    the flock. Three things this does not promise. A symlinked parent
    directory is still followed: O_NOFOLLOW guards the final component alone.
    Renaming the path after the open does not break the flock, so two holders
    exist if anything moves the file; the guarantee covers the open-to-flock
    window, not the whole body. And a file this opener inherits keeps the mode
    it has, which is what makes the private 0o600 of a file it creates safe to
    add: an existing lock is never chmodded under a live holder.

    `noun` names the lock in a refusal; `held` is the message when another
    holder owns it, or a callable that returns it, read at refusal time;
    `error` is the exception raised for every refusal. A non-blocking lock
    with `wait` seconds retries every `poll` seconds until that deadline, then
    refuses as it would have at once. The lock yields its open descriptor. Only a
    link or a foreign file is reported as unsafe ownership or type. Any other
    failure to open names the system error instead, because an unwritable, a
    read-only or a full lock directory is not a verdict about the file's
    owner.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as cause:
        raise error(f"{noun} lock cannot be opened: {path}: {cause.strerror}") from cause
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as cause:
        # Two spellings of the same refusal: this machine and CI answer
        # O_NOFOLLOW on a symlink with ELOOP, a BSD kernel with EMLINK. Only a
        # mocked open can reach the second here, and one does (sd:857) --
        # without it, deleting EMLINK left the whole suite green.
        if cause.errno not in (errno.ELOOP, errno.EMLINK):
            raise error(f"{noun} lock cannot be opened: {path}: {cause.strerror}") from cause
        raise error(f"{noun} lock has unsafe ownership or type: {path}") from cause
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_nlink != 1:
            raise error(f"{noun} lock has unsafe ownership or type: {path}")
        # The clock is read only for a wait: callers that mock it see no extra reads.
        deadline = time.monotonic() + wait if wait else None
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                break
            except BlockingIOError as cause:
                remaining = 0 if deadline is None else deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(poll, remaining))
                    continue
                # The cause is kept: a caller that relabels contention (control_gate)
                # reads it to tell a held lock from a lock it could not open.
                message = held() if callable(held) else held
                raise error(message or f"runner ownership lock held: {path}") from cause
        try:
            yield descriptor
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def detached(row: dict) -> bool:
    """Whether `repo remove` detached this `runner_run` row (sd:2581): released, no repo, and the repo it had."""
    return row.get("repo") is None and bool(row.get("detached_from")) and bool(row.get("released_at"))


def against(record: dict, row: dict) -> dict:
    """`record` keyed (`canonical`), as `row` should read it once `repo remove` detached it (sd:2581).

    The remove moves the row's repository to `detached_from` in the database
    only. The journal keeps naming it and is never rewritten: a file write
    cannot roll back with the transaction. So a released record whose
    repository is the detached row's `detached_from` reads as that move, and
    then agrees with the row when all else does. Any other record is returned
    keyed and unchanged, so a journal naming a repository the row never had,
    or a row whose repo went NULL some other way, still differs.
    """
    keyed = canonical(record)
    if detached(row) and keyed.get("released_at") and keyed.get("detached_from") is None \
            and keyed.get("repo") == canonical(row).get("detached_from"):
        return {**keyed, "repo": None, "detached_from": keyed["repo"]}
    return keyed


def read(path: Path) -> dict:
    try:
        if path.is_symlink() or not re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
            raise ValueError("unsafe run journal filename")
        envelope = json.loads(path.read_text())
        record = envelope["record"]
        if not isinstance(record, dict) or type(record.get("journal_version")) is not int or record["journal_version"] < 0:
            raise ValueError("invalid journal version")
        for name in ("id", "repo", "branch", "work_path", "retained_path", "owner", "created_at"):
            if not isinstance(record.get(name), str) or not record[name]:
                raise ValueError(f"invalid {name}")
        if type(record.get("assignment")) is not int or type(record.get("run")) is not int:
            raise ValueError("invalid assignment/run counter")
        if envelope["sha256"] != hashlib.sha256(_bytes(record)).hexdigest():
            raise ValueError("checksum mismatch")
        if record["id"] != path.stem:
            raise ValueError("run identity mismatch")
        return record
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RunnerRefused(f"runner journal is unreadable: {path}: {error}") from None


def persist(database: Path, record: dict) -> Path:
    ident = record.get("id", "")
    if not re.fullmatch(r"[a-f0-9]{32}", ident):
        raise RunnerRefused("invalid durable run identity")
    root = directory(database)
    # The directory `validate_path` accepts and nothing else: a link here was
    # followed and written through, and a file raised a bare `OSError`.
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as cause:
        raise RunnerRefused(f"runner journal directory cannot be made: {root}: {cause.strerror}") from cause
    if root.is_symlink() or not root.is_dir():
        raise RunnerRefused(f"runner journal directory is not a directory: {root}")
    target = root / f"{ident}.json"
    with lock(root / f"{ident}.lock"):
        if target.exists():
            previous = read(target)
            # Both sides keyed: a record written before migration 014 names
            # the same repository by its absolute path (sd:1447).
            keyed, previous = canonical(record), canonical(previous)
            for name in ("id", "assignment", "run", "repo", "branch", "work_path", "retained_path"):
                if previous[name] != keyed[name]:
                    raise RunnerRefused(f"run journal identity changed: {name}")
            if previous.get("released_at") and not record.get("released_at"):
                raise RunnerRefused("database is older than released run journal; reconcile restore first")
            if record["journal_version"] < previous["journal_version"]:
                raise RunnerRefused("database is older than durable run journal; reconcile restore first")
            if record["journal_version"] == previous["journal_version"] and keyed != previous:
                raise RunnerRefused("same-version run journal conflict; reconcile restore first")
        temporary = target.with_suffix(".partial")
        envelope = {"record": record, "sha256": hashlib.sha256(_bytes(record)).hexdigest()}
        with temporary.open("wb") as handle:
            handle.write(_bytes(envelope) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        fsync_directory(root)
    return target


def records(database: Path) -> list[dict]:
    root = directory(database)
    return validate_path(root) if root.exists() else []


def validate_path(path: Path) -> list[dict]:
    """Validate a copied journal directory without opening or modifying sd.db."""
    if path.is_symlink() or not path.is_dir():
        raise RunnerRefused(f"runner journal directory is absent: {path}")
    for candidate in path.iterdir():
        if candidate.is_symlink() or not candidate.is_file():
            raise RunnerRefused(f"unsafe run journal entry: {candidate}")
        if not re.fullmatch(r"[a-f0-9]{32}\.(json|lock|partial)", candidate.name):
            raise RunnerRefused(f"unknown run journal entry: {candidate}")
        if candidate.suffix == ".partial":
            raise RunnerRefused(f"interrupted journal write requires reconciliation: {candidate}")
    return [read(candidate) for candidate in sorted(path.glob("*.json"))]
