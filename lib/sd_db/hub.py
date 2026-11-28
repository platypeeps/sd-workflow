"""The satellite's selector: `~/.config/sd/hub.json` (step 4).

Step 4 of `docs/work/2026-09-22-run-the-framework-from-a-second-machine/`.
A machine whose home holds this file is a satellite: `sd_db.connect` sends
every open of the default database to the hub the file names, and
`sd_db.default_path` answers with a `HubPath`, whose `exists()` asks the hub.

    {"hub": "hub.example.test", "port": 8769, "address": "100.64.0.1"}

`address`, optional, is the hub's tailnet IP: the dial uses it in place of
the name (sd:10979), as a sandbox proxy that allows only an IP literal
needs. `hub` stays the name: promote, hub-ready and `build.hub_host` read it.

The selector is a file and not an environment variable. Cron and launchd
read no shell profile, and a satellite must refuse the same way from every
caller.

Three rules, each one a test in `tests/test_hub.py`:

* No file: `connect` opens the local database, as it always did.
* The file and no reachable hub: `HubUnreachable`, naming the hub. Nothing
  is created on this machine: no database, no folder for one.
* The file beside a local database: `HubConflict`. A machine is a hub or a
  satellite, not both, and the refusal is what keeps the satellite free of
  a second writer.

An explicit path other than the default is opened locally, as before: the
Jev meter's own file, a backup target and a test's fixture are not the
record, and the hub does not hold them.

`token_file` is for loopback only: `sd-db serve --loopback` refuses a
session without the token it wrote beside its database. A hub on the
tailnet carries no token; it admits the session by its peer's Tailscale
identity (step 7), so a satellite's `hub.json` names no `token_file`.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sqlite3
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import remote, tailnet
from .errors import SchemaTooNew, SdDbError

#: Beside `runner.json` and `dashboard.json`, under the same home as the
#: database path it replaces.
CONFIG_RELATIVE = Path(".config/sd/hub.json")
#: The port the design names for `sd-db serve`.
DEFAULT_PORT = 8769
#: Seconds to wait for the hub to answer a connect. A satellite with
#: Tailscale down must fail by name in seconds, not hang for a TCP timeout.
CONNECT_TIMEOUT = 5.0
#: Seconds a satellite waits on a silent hub, per read of an answer, past
#: the session's own wait for the write lock (`busy_timeout`) (sd:3359). A
#: hub that took the session and stopped answering held its caller, and a
#: lane runner's lock with it, for as long as the socket lived.
ANSWER_TIMEOUT = 120.0


#: What `Hub.open` calls with a `BuildMismatch` before raising it (sd:4748):
#: `follow.attempt` in an entrypoint that may run again from its start, which
#: `bin/sd` installs through `sd_lib.import_sd_db`. `None` here and in every
#: other process: a server, a lane runner or a ship sees the refusal. A hook
#: that returns leaves the refusal to be raised; one that replaces the process
#: does not return.
on_mismatch: Callable[[remote.BuildMismatch], None] | None = None


class HubConfigError(remote.RemoteError):
    """`hub.json` exists and cannot be read as a hub. Never a local fallback."""


class HubConflict(remote.RemoteError):
    """This machine has both `hub.json` and a local database."""

    def __init__(self, config: Path, local: Path, host: str) -> None:
        self.config = str(config)
        self.local = str(local)
        self.host = host
        super().__init__(
            f"this machine has both {config}, which names the sd hub {host}, and a "
            f"local database at {local}; a machine is a hub or a satellite, not both. "
            f"On a satellite, move the local database away; on the hub, remove {config}"
        )


def _base(home: Path | str | None) -> Path:
    # As `database.local_path` reads it: `$HOME` at call time.
    return Path(home) if home is not None else Path(os.environ.get("HOME", "~")).expanduser()


def config_path(home: Path | str | None = None) -> Path:
    return _base(home) / CONFIG_RELATIVE


def configured(home: Path | str | None = None) -> bool:
    """Whether this home names a hub. Cheap: one `stat`, no parse."""
    return config_path(home).exists()


@dataclass(frozen=True)
class Hub:
    """The hub one `hub.json` names."""

    host: str
    port: int
    config: Path
    token_file: Path | None = None
    #: The hub's IP literal the dial uses in place of `host`, or None (sd:10979).
    address: str | None = None

    def token(self) -> str:
        """The loopback server's token, read when the socket is up."""
        if self.token_file is None:
            return ""
        try:
            return self.token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise HubConfigError(
                f"cannot read the token file {self.token_file} that {self.config} names: "
                f"{error.strerror or error}"
            ) from error

    def open(self, local: Path, *, write: bool, create: bool, busy_timeout: int) -> remote.Connection:
        """A session on the hub's own database, in place of `local`."""
        if local.exists():
            raise HubConflict(self.config, local, self.host)
        if create:
            # `init` and `migrate` are hub verbs, and a satellite holds no
            # database to create (R4).
            raise remote.HubOnly("init and migrate", f"{self.host}:{self.port}")
        try:
            return remote.connect(self.host, self.port, None, token=self.token, write=write,
                                  busy_timeout=busy_timeout, timeout=CONNECT_TIMEOUT,
                                  answer_timeout=ANSWER_TIMEOUT + busy_timeout / 1000,
                                  address=self.address)
        except remote.HubUnreachable as error:
            raise remote.HubUnreachable(
                self.host, self.port, f"{error.reason} (named in {self.config})"
            ) from error
        except remote.BuildMismatch as mismatch:
            if on_mismatch is not None:
                on_mismatch(mismatch)
            raise


def read(home: Path | str | None = None) -> Hub | None:
    """The hub this home names, or `None` when it names none.

    A file that exists and does not parse is refused by name. Falling back
    to a local open would put a second database on a satellite.
    """
    path = config_path(home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise HubConfigError(f"cannot read {path}: {error.strerror or error}") from error
    expected = f'expected {{"hub": "<host>", "port": {DEFAULT_PORT}}}'
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        host = data["hub"]
        if not isinstance(host, str) or not host.strip():
            raise ValueError('"hub" is not a host name')
        port = data.get("port", DEFAULT_PORT)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError(f'"port" {port!r} is not a TCP port')
        token_file = data.get("token_file")
        if token_file is not None and (not isinstance(token_file, str) or not token_file):
            raise ValueError('"token_file" is not a path')
        address = data.get("address")
        if address is not None and not ip_literal(address):
            raise ValueError(f'"address" {address!r} is not an IP address')
    except KeyError as missing:
        raise HubConfigError(f"{path} names no {missing.args[0]!r}; {expected}") from None
    except ValueError as problem:
        raise HubConfigError(f"{path} is not a hub config: {problem}; {expected}") from None
    return Hub(host=host.strip(), port=port, config=path,
               token_file=Path(token_file).expanduser() if token_file else None, address=address)


def ip_literal(value) -> bool:
    """`value` is an IPv4 or IPv6 address as text, nothing around it."""
    try:
        return isinstance(value, str) and str(ipaddress.ip_address(value)) == value
    except ValueError:
        return False


class HubPath(type(Path())):
    """`default_path()` on a satellite: the same path, with `exists()` asked of the hub.

    Gap x1 of the design. Pack verbs test `sd_db.default_path().exists()`
    before they connect, or pass the path to `connect`. On a satellite the
    local file does not exist, so a plain path would read "no database". This
    one compares, prints and hashes as the plain path does. `connect` treats
    it, and any path equal to it, as the default. Paths derived from it
    (`parent`, `with_name`, `/`) are plain paths.
    """

    def __init__(self, *segments, home: Path | str | None = None) -> None:
        super().__init__(*segments)
        self.sd_home = None if home is None else Path(home)

    def with_segments(self, *segments) -> Path:
        return Path(*segments)

    def exists(self, *, follow_symlinks: bool = True) -> bool:
        """Whether the hub has its database. Raises `HubUnreachable`, never guesses.

        A read session proves it. A hub whose file is newer than this
        library still has one, so `SchemaTooNew` answers `True` here and
        refuses at `connect`, where it belongs.
        """
        from .database import connect

        try:
            connection = connect(self, write=False, home=self.sd_home)
        except FileNotFoundError:
            return False
        except SchemaTooNew:
            return True
        connection.close()
        return True


# ---------------------------------------------------------------- the fence

#: The fleet setting that names the hub by its tailnet name (hub failover, D5).
HUB_NAME = "sd.hub_name"
#: Seconds a failed read of this node's name stands: `lane run` asks once per
#: repository, and a stalled daemon must not cost each one its timeout.
#: A name is never held: a rename must refuse the next check (sd:4044).
#: A blip shorter than `tailnet.ONLINE_PAUSES` raises nothing, so it is
#: never held; a node offline past them is, as a stall is (pack sd:7281).
NAME_SECONDS = 30.0

_name_failed: tuple[float, Exception] | None = None


@dataclass(frozen=True)
class Role:
    """Whether this machine holds the hub role: `holder`, `off`, `elsewhere` or `unknown`.

    `holder` is the name `sd.hub_name` holds, `None` when unset; `reason`
    is one line a refusal can quote. Callers that act off this machine refuse
    on `elsewhere` and `unknown`; a write to its own database refuses on
    `elsewhere` alone (`write_role`). `observed` is the `time.monotonic`
    reading of its oldest input, from which `Held` ages it; `None` is now.
    `cause` is `tailscale` on an `unknown` that Tailscale gave, naming no
    node, and `None` otherwise: a satellite skips only that one (pack
    sd:9543). Not a fifth state: `sd-db fence` prints the state.
    """

    state: str
    holder: str | None
    reason: str
    observed: float | None = field(default=None, compare=False)
    cause: str | None = field(default=None, compare=False)

    @property
    def refuses(self) -> bool:
        return self.state not in ("holder", "off")


def _this_name() -> str:
    """`tailnet.this_name`, except that a failure refuses again for `NAME_SECONDS` without asking."""
    global _name_failed
    if _name_failed is not None and time.monotonic() - _name_failed[0] <= NAME_SECONDS:
        raise _name_failed[1]
    try:
        return tailnet.this_name()
    except tailnet.TailnetError as error:
        _name_failed = (time.monotonic(), error)
        raise


def _matches(mine: str, holder: str) -> bool:
    """The full MagicDNS name, or its first label when `sd.hub_name` is a bare machine name."""
    holder = holder.strip().rstrip(".").lower()
    return mine == holder if "." in holder else mine.split(".")[0] == holder


def role(connection, name=None) -> Role:
    """The fence: a machine holds the hub role only while its tailnet name is `sd.hub_name`.

    `connection` is this machine's own database; `name` reads this node's
    tailnet name (`tailnet.this_name`; a failure stands for `NAME_SECONDS`). The setting
    is read first, so a machine that never set it asks Tailscale nothing,
    and read again once Tailscale answers, so the answer is no older than
    Tailscale's (pack sd:7120). `observed` is its oldest input.

    Unset is `off`: a fleet that never named its hub runs as before, and
    `promote` refuses to move the role until the setting exists, so a
    returning old hub always finds the name in its own database.
    An unreadable setting, or a Tailscale that does not answer, is `unknown`.
    """
    from . import writes

    def setting() -> tuple[float, str | None]:
        read = time.monotonic()
        # A database older than the `state` table can hold no setting: never set, not unreadable.
        held = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'state'").fetchone()
        return read, writes.read_settings(connection, [HUB_NAME]).get(HUB_NAME) if held else None

    def unset(read: float) -> Role:
        return Role("off", None, f"{HUB_NAME} is unset, so the fence is off; "
                                 f"on the hub run `sd config set {HUB_NAME} <its tailnet name>`", read)

    try:
        read, holder = setting()
    except (SdDbError, sqlite3.Error, OSError) as error:
        return Role("unknown", None, f"{HUB_NAME} does not read: {error}")
    if not holder:
        return unset(read)
    try:
        mine = (name or _this_name)()
    except tailnet.TailnetError as error:
        # A failure `_this_name` holds was seen when it was cached.
        failed = _name_failed[0] if _name_failed is not None and _name_failed[1] is error else read
        return Role("unknown", holder, f"Tailscale does not name this node, so it cannot match {holder}: {error}",
                    min(read, failed), cause="tailscale")
    told = time.monotonic()
    try:
        read, holder = setting()
    except (SdDbError, sqlite3.Error, OSError) as error:
        return Role("unknown", None, f"{HUB_NAME} does not read: {error}")
    if not holder:
        return unset(read)
    if _matches(mine, holder):
        return Role("holder", holder, f"this node is {mine}, the hub {HUB_NAME} names", told)
    return Role("elsewhere", holder, f"{HUB_NAME} names {holder}, and this node is {mine}", told)


def satellite_role(home: Path | str | None = None) -> Role | None:
    """`elsewhere` when this home names a hub (`hub.json`), else None.

    A satellite holds no database and no role, whatever the hub's setting
    says. A `hub.json` that does not parse raises `HubConfigError`, as `read`.
    """
    satellite = read(home)
    if satellite is None:
        return None
    return Role("elsewhere", satellite.host, f"this machine is a satellite of {satellite.host} (hub.json)")


def machine_role(connection, *, home: Path | str | None = None, name=None) -> Role:
    """`sd-db fence`'s answer on an open connection: `satellite_role`, else `role`.

    A `hub.json` that does not read is `unknown`, never a hub.
    """
    try:
        found = satellite_role(home)
    except HubConfigError as error:
        return Role("unknown", None, str(error))
    return found or role(connection, name)


def role_at(path: Path, name=None) -> Role:
    """`role` on a fresh read-only connection to `path`; a database that does not open is `unknown`."""
    from .database import connect

    try:
        connection = connect(path, write=False)
    except Exception as error:  # noqa: BLE001 -- no answer is unknown, named
        return Role("unknown", None, f"the database at {path} did not open: {type(error).__name__}: {error}")
    try:
        return role(connection, name)
    finally:
        connection.close()


# ------------------------------------------------------- the write fence

#: Seconds a write and its commit trust the last fence answer for one
#: database, as a tailnet frame and its commit do (`serve.FENCE_SECONDS`).
#: A transaction's opening asks again past half of it, before SQLite's write
#: lock, so a commit asks Tailscale under the lock only when its transaction
#: ran that long (pack sd:7042). A wall clock that moved `CLOCK_SLACK` more
#: than the monotonic one, which does not count a sleep, asks again.
FENCE_SECONDS = 5.0
CLOCK_SLACK = 1.0
#: SQLite's action codes for a statement that writes a database file. A
#: pragma is left out: migrate's DDL asks, and a pragma alone moves no row.
_WRITES = frozenset({
    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_ANALYZE, sqlite3.SQLITE_REINDEX, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_DROP_VTABLE,
})
#: Pragmas whose value names a table or index to read, or sets this
#: connection or process only: SQLite's pragma list less those that may
#: change the file. Any other pragma given a value writes the file, or may:
#: `user_version`, `application_id`, `journal_mode` (pack sd:6328). Some of
#: these fail inside a transaction (`synchronous`), so none may be wrapped.
_PRAGMA_READ_OR_CONNECTION = frozenset({
    # Reads that take a table, an index or a limit.
    "foreign_key_check", "foreign_key_list", "index_info", "index_list", "index_xinfo", "integrity_check",
    "quick_check", "table_info", "table_list", "table_xinfo",
    # Settings of this connection, or this process.
    "analysis_limit", "automatic_index", "busy_timeout", "cache_size", "cache_spill", "case_sensitive_like",
    "cell_size_check", "checkpoint_fullfsync", "count_changes", "defer_foreign_keys", "empty_result_callbacks",
    "foreign_keys", "full_column_names", "fullfsync", "hard_heap_limit", "ignore_check_constraints",
    "journal_size_limit", "legacy_alter_table", "locking_mode", "max_page_count", "mmap_size", "query_only",
    "read_uncommitted", "recursive_triggers", "reverse_unordered_selects", "secure_delete", "short_column_names",
    "soft_heap_limit", "synchronous", "temp_store", "temp_store_directory", "threads", "trusted_schema",
    "wal_autocheckpoint", "writable_schema",
})
#: Pragmas that write the file with no value given.
_PRAGMA_WRITES = frozenset({"incremental_vacuum", "wal_checkpoint"})
#: Write pragmas a ROLLBACK does not undo, so each asks afresh before it
#: runs, inside a transaction or not (lane review of 8a5f0f243). BEGIN and
#: SAVEPOINT open no storage transaction, so `wal_checkpoint` runs there at
#: once, and `journal_mode` may. Outside one they cannot be wrapped: they
#: fail inside BEGIN IMMEDIATE ("cannot change out of wal mode from within
#: a transaction", "database table is locked"). Each writes all it writes
#: before its one row. VACUUM is the same, authorized as SQLITE_ATTACH.
_PRAGMA_UNDONE_BY_NOTHING = frozenset({"journal_mode", "wal_checkpoint"})
#: Write pragmas that never run on the record, and why. No fence check holds
#: `incremental_vacuum`. BEGIN IMMEDIATE writes the header of an empty file,
#: so the wrapper would drop `page_size` and `auto_vacuum` without a word;
#: no caller sets them after `arm` (pack sd:6378).
_FORBIDDEN_PRAGMAS = {
    "incremental_vacuum": "refused: PRAGMA incremental_vacuum frees pages as its rows are fetched, after any "
                          "fence check and past a ROLLBACK, so it never runs on this machine's own database; "
                          "run it with sqlite3 on the hub (pack sd:6328)",
    **{name: f"refused: PRAGMA {name} takes effect only before the database exists, and this connection's "
             f"write fence would wrap it in BEGIN IMMEDIATE, which creates the file with the default; "
             f"set it with sqlite3 before the database exists (pack sd:6378)"
       for name in ("page_size", "auto_vacuum")},
}

class Held:
    """One fence answer and when it came (pack sd:7042).

    `get(ask, within)` returns the held answer while it is younger than
    `within` seconds, counted from its oldest input, and the wall clock
    moved with the monotonic one since `ask` began;
    otherwise it calls `ask` and holds what it returns. One ask runs at a
    time. A caller that finds an answer young enough takes no lock, so a
    slow Tailscale stalls only the callers that need a new answer.
    """

    def __init__(self) -> None:
        self._held: tuple[float, float, object] | None = None
        self._asking = threading.Lock()

    def _young(self, within: float) -> tuple[bool, object]:
        held = self._held
        if held is None:
            return False, None
        since, at, found = held
        ticks, wall = time.monotonic(), time.time()
        return ticks - since < within and abs((wall - at) - (ticks - since)) <= CLOCK_SLACK, found

    def get(self, ask: Callable[[], object], within: float):
        young, found = self._young(within)
        if young:
            return found
        with self._asking:
            # Another caller may have asked while this one waited.
            young, found = self._young(within)
            if young:
                return found
            ticks, wall = time.monotonic(), time.time()
            found = ask()
            # Aged from its oldest input (`Role.observed`), else from its
            # arrival: from the call's start, an answer slower than `within`
            # came stale, and the next write asked again under SQLite's
            # write lock (pack sd:7120). The wall clock keeps the start, so
            # a sleep during the call still reads as a jump.
            observed = getattr(found, "observed", None)
            since = time.monotonic() if observed is None else observed
            self._held = (since, wall + (since - ticks), found)
            return found


_answers: dict[str, Held] = {}
_answers_guard = threading.Lock()


class Fenced(remote.RemoteError):
    """This machine no longer holds the hub role (hub failover, D5).

    On the tailnet the session closes and rolls back. On a returning hub's
    own database the write refuses and its transaction rolls back (system
    sd:5986).
    """

    #: The fence's `Role.state` where this was raised, kept on the wire by
    #: `remote.describe_error`: `elsewhere` lasts (a demotion); `unknown`
    #: is a fence that could not answer, such as Tailscale naming no node
    #: (pack sd:9543). `None` from a hub older than the field, which a
    #: rebuilt error leaves at this class default: read it as lasting.
    state: str | None = None
    #: The role's `cause`: `tailscale` on the `unknown` Tailscale gave;
    #: `None` on any other, and from a hub older than the field.
    cause: str | None = None

    def __init__(self, text: str, *, state: str | None = None, cause: str | None = None) -> None:
        super().__init__(text)
        self.state = state
        self.cause = cause


def write_role(path: Path, *, within: float | None = None) -> Role:
    """`role` of the database at `path`, read from that file and held `within` seconds, `FENCE_SECONDS` by default.

    The file itself, never through `hub.json`: during demotion `hub.json`
    sits beside the database, and a `HubConflict` read as `unknown` would let
    a writer that is already open go on. `within=0` asks again whatever is
    held, as an operation no ROLLBACK undoes does.
    """
    from .database import open_local

    def ask() -> Role:
        try:
            connection = open_local(Path(path), write=False, create=False)
        except Exception as error:  # noqa: BLE001 -- no answer is unknown, named
            return Role("unknown", None, f"the database at {path} did not open: {type(error).__name__}: {error}")
        try:
            return role(connection)
        finally:
            connection.close()

    with _answers_guard:
        held = _answers.setdefault(str(path), Held())
    return held.get(ask, FENCE_SECONDS if within is None else within)


#: The raw fix for a mistyped `sd.hub_name` on the real hub: the fence stays
#: strict, so `sd config set` cannot write it (decision on #31345). The newest
#: `setting` row is the answer (`writes.read_settings`).
HUB_NAME_FIX = ("UPDATE state SET body = json_object('value', '<this node>') WHERE id = "
                f"(SELECT max(id) FROM state WHERE kind = 'setting' AND key = '{HUB_NAME}')")


def demotion(path: Path, found: Role) -> str:
    """The `Fenced` text for a local write: the holder, the two demotion steps, `hub.json` first, then the typo fix."""
    return (f"refused: this machine is not the hub, so it writes nothing to {path}: {found.reason}. "
            f"Reads still work. Demote it in this order: "
            f"1. copy ~/{CONFIG_RELATIVE} from any satellite into this home "
            f"(until step 2, every sd command refuses with HubConflict); "
            f"2. move {path} aside as {Path(path).name}.demoted-<date>; "
            f"sd commands then reach the hub, {found.holder}. "
            f"If this machine is the hub and {HUB_NAME} is mistyped, no sd command can fix it: "
            f"run sqlite3 {path} \"{HUB_NAME_FIX}\" with this node's tailnet name, "
            f"or rename this node in Tailscale to {found.holder}")


class _WriteFence:
    """SQLite's authorizer on this machine's own write connection (system sd:5986).

    Write unless fenced (#31093): only `elsewhere` refuses. `holder`, `off`
    and `unknown` write, so a Tailscale outage does not stop a healthy hub.
    Each boundary that makes a write persist asks: COMMIT, and the RELEASE
    of a savepoint opened outside a transaction. It trusts an answer up to
    `FENCE_SECONDS` old: the transaction's opening (BEGIN, or that
    savepoint) asks again before SQLite's write lock once the held answer
    passed half of that, and refuses nothing. A fresh ask at the commit held
    every other writer for a Tailscale call (pack sd:7042). A write outside a
    transaction commits when SQLite ends it, which no authorizer sees, so it
    is refused at prepare (`unwrapped`); the cursor runs it again inside
    BEGIN IMMEDIATE ... COMMIT (`_batched`), as it runs `executemany`. A
    transaction that wrote nothing commits without asking, so reads keep
    working; its opening may ask, once per half window.
    A pragma that changes the file is a write (pack sd:6328). Outside a
    transaction it runs again inside one, as other writes do:
    `incremental_vacuum` frees a page per row fetched, after execute().
    Those no ROLLBACK undoes (`_PRAGMA_UNDONE_BY_NOTHING`, VACUUM) ask
    afresh before they run, in a transaction or not; `incremental_vacuum`
    never runs, nor `page_size` or `auto_vacuum` (`_FORBIDDEN_PRAGMAS`).
    Only SQLite's own state ends a transaction here: the write flag and the
    outer savepoint clear when a statement is prepared outside one, never on
    a BEGIN, ROLLBACK or RELEASE that may yet fail (review of f716079a9).
    A write inside a transaction asks the held answer, to stop early; its
    commit decides. So a commit more than `FENCE_SECONDS` after the console
    rename, or after a sleep, rolls back.
    The connection caches no statement: Python does not authorize a cached one again.
    It holds its connection weakly, so the pair makes no cycle.
    """

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self.connection = weakref.ref(connection)
        #: The open transaction, or this autocommit statement, prepared a write.
        self.wrote = False
        #: The savepoint that opened the current transaction: its RELEASE commits.
        self.outer: str | None = None
        #: Why the last statement was denied, for the `Fenced` the connection raises.
        self.refusal: str | None = None
        #: The last statement was denied as a write outside a transaction.
        self.unwrapped = False
        #: Why the last statement was denied whatever the role, for an `SdDbError`.
        self.forbidden: str | None = None

    def _open(self) -> bool:
        """Whether a transaction is open before the statement being prepared."""
        connection = self.connection()
        return connection is not None and connection.in_transaction

    def __call__(self, action: int, arg1, arg2, database, trigger) -> int:
        opened = self._open()
        if not opened:
            self.wrote, self.outer = False, None
        if action == sqlite3.SQLITE_TRANSACTION:
            if arg1 == "BEGIN" and not opened:
                return self._open_ask()
            ends = arg1 == "COMMIT" and opened and self.wrote
            return self._ask() if ends else sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_SAVEPOINT:
            name = (arg2 or "").lower()
            if arg1 == "BEGIN" and not opened:
                self.outer = name
                return self._open_ask()
            if arg1 == "RELEASE" and opened and name == self.outer and self.wrote:
                return self._ask()
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_ATTACH:
            # VACUUM, VACUUM INTO and ATTACH: VACUUM commits itself.
            return self._ask(within=0)
        pragma = (arg1 or "").lower() if action == sqlite3.SQLITE_PRAGMA else None
        writes = action in _WRITES or pragma is not None and (
            pragma in _PRAGMA_WRITES or arg2 is not None and pragma not in _PRAGMA_READ_OR_CONNECTION)
        if writes and database != "temp":
            if pragma in _FORBIDDEN_PRAGMAS:
                self.forbidden = _FORBIDDEN_PRAGMAS[pragma]
                return sqlite3.SQLITE_DENY
            if pragma in _PRAGMA_UNDONE_BY_NOTHING:
                return self._ask(within=0)
            if not opened:
                self.unwrapped = True
                return sqlite3.SQLITE_DENY
            self.wrote = True
            return self._ask()
        return sqlite3.SQLITE_OK

    def _open_ask(self) -> int:
        """At a transaction's opening, before its write lock: renew an answer past half its window."""
        write_role(self.path, within=FENCE_SECONDS / 2)
        return sqlite3.SQLITE_OK

    def _ask(self, *, within: float | None = None) -> int:
        found = write_role(self.path, within=within)
        if found.state != "elsewhere":
            return sqlite3.SQLITE_OK
        self.refusal = demotion(self.path, found)
        return sqlite3.SQLITE_DENY


class _Unwrapped(Exception):
    """A write prepared outside a transaction: run it again inside one (`_batched`)."""


def _fenced(connection: sqlite3.Connection, run, *args):
    """`run(*args)`, with a fence denial raised as `Fenced` after its transaction rolls back."""
    fence: _WriteFence | None = getattr(connection, "fence", None)
    if fence is not None:
        fence.refusal, fence.unwrapped, fence.forbidden = None, False, None
    try:
        return run(*args)
    except sqlite3.DatabaseError as error:
        if fence is not None and fence.forbidden is not None:
            forbidden, fence.forbidden = fence.forbidden, None
            raise SdDbError(forbidden) from error
        if fence is not None and fence.unwrapped:
            fence.unwrapped = False
            raise _Unwrapped() from error
        if fence is None or fence.refusal is None:
            raise
        refusal, fence.refusal = fence.refusal, None
        if connection.in_transaction:
            # Nothing a fenced transaction wrote may commit.
            sqlite3.Connection.execute(connection, "ROLLBACK")
        # `_ask` denies on `elsewhere` alone.
        raise Fenced(refusal, state="elsewhere") from error


def _batched(connection: sqlite3.Connection, run, *args):
    """A write in its own transaction outside one, so its COMMIT asks the fence.

    In autocommit SQLite commits a write when it ends, and `executemany`
    after each parameter set, with no authorizer call between.
    """
    if connection.in_transaction:
        return _fenced(connection, run, *args)
    sqlite3.Connection.execute(connection, "BEGIN IMMEDIATE")
    try:
        cursor = _fenced(connection, run, *args)
        _fenced(connection, sqlite3.Connection.execute, connection, "COMMIT")
    except BaseException:
        if connection.in_transaction:
            sqlite3.Connection.execute(connection, "ROLLBACK")
        raise
    return cursor


class _FencedCursor(sqlite3.Cursor):
    def execute(self, *args):
        # A read runs as it is; a write outside a transaction is refused at
        # prepare, before it runs, and runs again inside one.
        try:
            return _fenced(self.connection, super().execute, *args)
        except _Unwrapped:
            return _batched(self.connection, super().execute, *args)

    def executemany(self, *args):
        return _batched(self.connection, super().executemany, *args)

    def executescript(self, *args):
        # sqlite3 commits an open transaction before a script, so none can
        # hold it, and a script half run cannot run again.
        try:
            return _fenced(self.connection, super().executescript, *args)
        except _Unwrapped:
            raise SdDbError("refused: a script's write outside a transaction commits unfenced; "
                            "put BEGIN IMMEDIATE ... COMMIT inside the script (system sd:5986)") from None


class FencedConnection(sqlite3.Connection):
    """This machine's own write connection to its default database (`database.open_local`)."""

    fence: _WriteFence | None = None

    def arm(self, path: Path) -> None:
        self.fence = _WriteFence(Path(path), self)
        super().set_authorizer(self.fence)

    def set_authorizer(self, *args, **kwargs):
        raise SdDbError("the write fence owns this connection's authorizer (system sd:5986)")

    def cursor(self, factory=_FencedCursor):
        # A plain cursor's `executemany` would commit each set unasked.
        if not (isinstance(factory, type) and issubclass(factory, _FencedCursor)):
            raise SdDbError("this connection hands out fenced cursors only (system sd:5986)")
        return super().cursor(factory)

    # The convenience methods go through `cursor()`: sqlite3's own hand back
    # a plain cursor, which a caller can reuse past the fence.
    def execute(self, *args):
        return self.cursor().execute(*args)

    def executemany(self, *args):
        return self.cursor().executemany(*args)

    def blobopen(self, *args, readonly: bool = False, name: str = "main"):
        # A blob write passes no authorizer and commits on each write outside
        # a transaction, so it is held to one whose COMMIT asks.
        if not readonly and name != "temp":
            if not self.in_transaction:
                raise SdDbError("a writable blob needs a transaction (BEGIN IMMEDIATE ... COMMIT)")
            if self.fence is not None:
                self.fence.wrote = True
        return super().blobopen(*args, readonly=readonly, name=name)

    def executescript(self, *args):
        return self.cursor().executescript(*args)

    def commit(self):
        return _fenced(self, super().commit)

    def __exit__(self, *args):
        return _fenced(self, super().__exit__, *args)
