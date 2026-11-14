"""Opening the database, and the two version refusals.

One place resolves the path, sets the pragmas and checks the version. Every
other module in this package takes a connection from here, and nothing
outside the package calls `sqlite3.connect` at all -- which is the invariant
requirement 2 states and a grep enforces.

The two refusals are asymmetric on purpose:

* **Newer than this library**: refused on open. A process that reads a shape
  it does not understand returns wrong answers quietly, and a quiet wrong
  answer is worse than a process that will not start.
* **Older than this library**: refused on write, allowed on read. A machine
  stopped halfway through an upgrade can still be inspected, and `sd today`
  keeps answering, while nothing can write rows in a shape the database does
  not have. The message names the command, because the migration never runs
  on open.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import SchemaTooNew, SchemaTooOld
from .schema import SCHEMA_VERSION, VERSION_PRAGMA

#: `~/.local/share/sd/sd.db`. Not a cache path: caches get deleted, and this
#: is the record.
DEFAULT_RELATIVE = Path(".local/share/sd/sd.db")


def local_path(home: Path | str | None = None) -> Path:
    """The database file on this machine. `$HOME` is read at call time, not at import.

    Read at call time because a test sets `HOME` for a child process and for
    itself, and a module-level constant would have frozen the real home into
    the import.
    """
    base = Path(home) if home is not None else Path(os.environ.get("HOME", "~")).expanduser()
    return base / DEFAULT_RELATIVE


def default_path(home: Path | str | None = None) -> Path:
    """Where the database lives: `local_path`, or on a satellite a `HubPath`.

    On a satellite (`~/.config/sd/hub.json` exists, `sd_db.hub`) the path is
    the same string, and its `exists()` asks the hub. `connect` treats it,
    and any path equal to it, as the default and opens a session on the hub.
    So a caller that tests `default_path().exists()` or passes the path to
    `connect` needs no edit (gap x1 of the second-machine design).
    """
    from . import hub

    base = Path(home) if home is not None else Path(os.environ.get("HOME", "~")).expanduser()
    if hub.configured(base):
        return hub.HubPath(base / DEFAULT_RELATIVE, home=base)
    return base / DEFAULT_RELATIVE


def served_by(target, home: Path | str | None = None) -> str | None:
    """The hub that serves `target`, as `host:port`, or `None` when it is local.

    `target` is a connection or a path. A remote connection names its hub.
    A path is served when `connect` would send it to a hub: the default
    path on a satellite, or any path a test opener serves. Step 6 decides
    by this, never by `PRAGMA database_list`, which answers the hub's path
    over the wire (seam 2 of the second-machine design).
    """
    from . import hub, remote

    if isinstance(target, Patient):  # sd:8721: the wrap retries; the hub is the wrapped one's
        target = target.connection
    if isinstance(target, remote.Connection):
        return f"{target.host}:{target.port}"
    if not isinstance(target, (str, os.PathLike)):
        return None
    if home is None and isinstance(target, hub.HubPath):
        home = target.sd_home
    if _same_file(Path(target), local_path(home)) and hub.configured(home):
        satellite = hub.read(home)
        if satellite is not None:
            return f"{satellite.host}:{satellite.port}"
    served = getattr(_opener, "served_by", None)
    return served(Path(target)) if served is not None else None


def refuse_hub_only(target, verb: str, home: Path | str | None = None) -> None:
    """Raise `HubOnly` naming `verb` when a hub serves `target`."""
    hub = served_by(target, home)
    if hub is not None:
        from .remote import HubOnly

        raise HubOnly(verb, hub)


def _same_file(one: Path, other: Path) -> bool:
    """Lexical first, then canonical: the pack passes `default_path()` or
    its string back, which compares equal with no filesystem access, and an
    aliased spelling (a symlink, /var against /private/var) still names the
    default. A folder the comparison cannot read makes the paths differ;
    comparing the target must not fail on it."""
    one, other = one.expanduser(), other.expanduser()
    if os.path.abspath(one) == os.path.abspath(other):
        return True
    try:
        return one.resolve() == other.resolve()
    except OSError:
        return False


def schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute(f"PRAGMA {VERSION_PRAGMA}").fetchone()[0])


def set_schema_version(connection: sqlite3.Connection, version: int) -> None:
    # `PRAGMA user_version` takes no parameter binding.
    connection.execute(f"PRAGMA {VERSION_PRAGMA} = {int(version)}")


#: How long a statement waits for a lock before it gives up. A busy database
#: is a database in use, not a failure, and five seconds is longer than any
#: write this library makes.
BUSY_TIMEOUT = 5000

#: The waits, in seconds, before a statement a locked database refused is sent again (pack sd:6588, sd:6855).
#: With each try's own `busy_timeout` they total about a minute, well under `build.STALE_SECONDS`.
LOCK_WAITS = (1, 2, 4, 8, 16)


#: The tag a lock refusal's holder report starts with, so a refusal is reported once.
HOLDERS = "holders (pid, elapsed, command):"


def lock_holders(connection) -> str:
    """Who else has the database's files open, for a write a held lock refused (pack sd:6948).

    SQLite names no lock holder and macOS `lsof` shows no fcntl lock, so
    this lists every other process with the database, its WAL or its
    shared memory open: one of them holds the write transaction, and its
    elapsed time points at the long one. A read that fails says so.
    A remote connection's lock is on the hub, whose files this machine
    cannot list, so the report names the hub instead (pack sd:7196).
    """
    hub = served_by(connection.connection if isinstance(connection, Patient) else connection)
    if hub is not None:
        return f"{HOLDERS} unread: the lock is on the hub {hub}; list them there"
    try:
        path = next(row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main")
        files = [name for name in (path, path + "-wal", path + "-shm") if path and os.path.exists(name)]
        if not files:  # `lsof` with no file lists every process
            return f"{HOLDERS} no database file"
        found = subprocess.run(["lsof", "-t", "--", *files], capture_output=True, text=True, timeout=10,
                               check=False).stdout
        pids = sorted({int(word) for word in found.split() if word.isdigit()} - {os.getpid()})
        if not pids:
            return f"{HOLDERS} no other process has it open"
        listed = subprocess.run(["ps", "-o", "pid=,etime=,command=", "-p", ",".join(map(str, pids))],
                                capture_output=True, text=True, timeout=10, check=False).stdout
    except (sqlite3.Error, StopIteration, OSError, subprocess.SubprocessError) as error:
        return f"{HOLDERS} unread: {type(error).__name__}"
    rows = [" ".join(line.split(None, 2))[:200] for line in listed.splitlines() if line.strip()]
    return f"{HOLDERS} " + "; ".join(rows[:10] or map(str, pids))


def _reported(connection, error: sqlite3.OperationalError) -> sqlite3.OperationalError:
    """`error`, with the lock holders when a lock refused it and none are named yet."""
    text = str(error)
    if HOLDERS in text or not ("locked" in text or "busy" in text):
        return error
    reported = sqlite3.OperationalError(f"{text}; {lock_holders(connection)}")
    for key in ("sqlite_errorcode", "sqlite_errorname"):
        setattr(reported, key, getattr(error, key, None))
    return reported


class Patient:
    """A connection whose statement a locked database refused outside a transaction is sent again.

    A statement refused outside a transaction (a `BEGIN`, a read, one
    autocommit write) wrote nothing, so a retry cannot write twice; one
    inside a transaction fails as it did. Each try waits its `busy_timeout`.
    A build supervisor (sd:6588) and `sd-ship` (sd:6855) hold one.
    """

    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name: str):
        return getattr(self.connection, name)

    def execute(self, sql: str, parameters=()):
        for wait in (*LOCK_WAITS, None):
            try:
                return self.connection.execute(sql, parameters)
            except sqlite3.OperationalError as error:
                if wait is None or self.connection.in_transaction or not (
                        "locked" in str(error) or "busy" in str(error)):
                    if wait is None:
                        raise _reported(self.connection, error) from error
                    raise
            time.sleep(wait)


def _configure(connection: sqlite3.Connection, *, write: bool) -> None:
    connection.row_factory = sqlite3.Row
    # WAL so a reader is never blocked by the writer: the dashboard reads
    # while the runner writes, all day.
    if write:
        connection.execute("PRAGMA journal_mode = WAL")
    else:
        connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")


def connect(
    path: Path | str | None = None,
    *,
    write: bool = True,
    home: Path | str | None = None,
    create: bool = False,
    busy_timeout: int = BUSY_TIMEOUT,
) -> sqlite3.Connection:
    """Open the database, configured and version-checked.

    `busy_timeout` is how many milliseconds a statement waits for a lock, and
    it is set before anything else this function runs, because opening is not
    free of locks either: the version read and `PRAGMA journal_mode = WAL`
    both take one. `0` makes every statement on the connection fail at once
    rather than wait, which is what a caller that must not be delayed asks
    for -- `local-jev`'s metering, whose whole promise is that it cannot cost
    the judgment anything.

    `write=False` opens SQLite in `mode=ro` and sets `query_only`, while
    skipping the older-version refusal. It never changes journal mode.
    SQLite may maintain WAL/SHM locking sidecars for a live WAL reader;
    `immutable` would avoid that but silently ignore outstanding WAL data.
    `create=True` is for `init` and `migrate` alone: every other caller
    expects the file to exist, and creating it silently is how a typo becomes
    an empty database nobody notices.
    """
    if create and not write:
        raise ValueError("create=True requires write=True; a read cannot create a database")
    from . import hub

    if home is None and isinstance(path, hub.HubPath):
        home = path.sd_home
    # Step 4: on a satellite, the default database is the hub's. An explicit
    # path equal to the default is the default too: `sd-review` and
    # `sd-ship` pass `default_path()` rebuilt as a plain path. The file is
    # read only for the default: a broken `hub.json` must not stop the Jev
    # meter or a backup target, which are not the hub's.
    # The target is compared first, so another database never touches the
    # selector's folder at all, not even a `stat` that could be refused.
    local = local_path(home)
    own = path is None or _same_file(Path(path), local)
    if own and hub.configured(home):
        satellite = hub.read(home)
        if satellite is not None:
            return satellite.open(local, write=write, create=create, busy_timeout=busy_timeout)
    target = Path(path) if path is not None else local
    if _opener is not None:
        return _opener(target, write=write, create=create, busy_timeout=busy_timeout)
    # The write fence (system sd:5986): a write to this machine's own record
    # asks whether it still holds the hub role. Another file is not the record.
    return open_local(target, write=write, create=create, busy_timeout=busy_timeout, fence=write and own)


#: The test seam `sd_db.testing.surface` and `sd_db.testing.wire` set, in
#: their own process only, to run the whole suite through a guarded or a
#: remote connection. `None` everywhere else. It is a module attribute and
#: not an environment variable on purpose: a selector a shell can leak into
#: a job is the one the design rejects (`SD_DB_HUB`).
_opener = None


def open_local(
    target: Path, *, write: bool, create: bool, busy_timeout: int = BUSY_TIMEOUT,
    cached_statements: int = 128, fence: bool = False,
) -> sqlite3.Connection:
    """The local open behind `connect`, for a resolved path.

    `sd_db.serve` calls it on the hub, so a remote session gets exactly the
    connection a local caller gets: same pragmas, same version refusals.
    It passes `cached_statements=0`, so its authorizer sees every statement;
    128 is Python's own default.

    `fence` arms the write fence (`hub.FencedConnection`): every write and
    its commit ask `hub.write_role`, and `elsewhere` raises `hub.Fenced`.
    It caches no statement either, for the same reason.
    """
    from . import hub

    if not create and not target.exists():
        raise FileNotFoundError(
            f"no database at {target}; run `bin/sd-db init` from the command pack"
        )
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
    mode = "rwc" if create else "rw" if write else "ro"
    connection = sqlite3.connect(target.resolve().as_uri() + f"?mode={mode}",
                                 uri=True, isolation_level=None,
                                 cached_statements=0 if fence else cached_statements,
                                 factory=hub.FencedConnection if fence else sqlite3.Connection)
    try:
        # First, before the version read and the journal-mode pragma: both
        # take a lock, so a timeout set after them is a timeout that was not
        # in force when it was needed.
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout)}")
        # Refuse an incompatible version before a writable open changes its
        # journal mode. URI encoding preserves literal '?' and '#' in paths.
        found = schema_version(connection)
        if found > SCHEMA_VERSION:
            raise SchemaTooNew(found, SCHEMA_VERSION)
        if write and found < SCHEMA_VERSION and not create:
            raise SchemaTooOld(found, SCHEMA_VERSION)
        _configure(connection, write=write)
        if isinstance(connection, hub.FencedConnection):
            connection.arm(target)
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One unit of work. Commits on success, rolls back on any exception.

    The connection is opened with `isolation_level=None`, so transactions are
    explicit here rather than implied by the driver's guesses about which
    statements begin one.
    """
    # Public workflows compose named writes. A savepoint lets an inner write
    # fail independently while the outer transaction still owns the commit.
    savepoint = f"sd_{uuid.uuid4().hex}" if connection.in_transaction else None
    try:
        connection.execute(f"SAVEPOINT {savepoint}" if savepoint else "BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        raise _reported(connection, error) from error
    try:
        yield connection
    except BaseException:
        # A fenced write (`hub.Fenced`) has rolled the whole transaction back already.
        if not connection.in_transaction:
            raise
        if savepoint:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
        else:
            connection.execute("ROLLBACK")
        raise
    connection.execute(f"RELEASE {savepoint}" if savepoint else "COMMIT")


def tables(connection: sqlite3.Connection) -> list[str]:
    """The tables that exist, excluding SQLite's own."""
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [row[0] for row in rows]
