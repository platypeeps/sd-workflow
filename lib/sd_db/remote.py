"""A connection to a database served by `sd_db.serve`, over one TCP socket.

The satellite half of the hub/satellite design
(`docs/work/2026-09-22-run-the-framework-from-a-second-machine/`). The hub
holds the file and opens it with the library's own local open; this class
forwards each statement and rebuilds the answer. It carries exactly the
connection and cursor surface the library touches, which
`sd_db.testing.surface` names and proves against the whole suite; anything
else raises `AttributeError`.

Steps 2 to 4 and 7 of the plan. On loopback, the `open` frame carries the
owner-only token the server wrote beside the database; on the tailnet, the
hub admits the session by its TCP peer and reads no token. It also carries
the handshake of step 3: this build's package version and `SCHEMA_VERSION`,
beside the protocol version every frame carries. The hub refuses any difference with
`BuildMismatch` before it opens anything.

Step 5, the unknown outcome. Over the wire the hub can commit and its answer
can still be lost. So every write transaction carries a request id R, a
ULID this class makes when it sends `BEGIN IMMEDIATE` and sends again with
`COMMIT`; the frame's `rid` field. The hub owns R from `BEGIN` and records
it inside the transaction it commits. When the answer to `COMMIT` is lost,
this class asks the hub for R on a fresh session (`outcome`), with a
bounded backoff: `recorded` returns as a commit would, `absent` raises
`TransactionLost(R)`, and an owner still live at the bound raises
`UnknownOutcome(R)`. It keeps no copy of the statements and re-sends
nothing: the verb is run again, under a new id, by whoever chooses to. A
plain `BEGIN` carries no R; the hub runs it as a read transaction.

A write outside a transaction (sd:2671) carries an R too, and the hub runs
it as a write transaction of that one statement: a lost answer is settled
the same way. A `recorded` write returns no rows and no `lastrowid`, since
its answer is gone.

A session that drops while it holds nothing, between two units of work,
is opened again for the next one (sd:3336): a `BEGIN` or a `SELECT` sent
outside a transaction, which cannot have written anything, goes once more on
a new session. Nothing else is sent twice. `sd-ship prepare` holds its session
across a review and gate of 15 to 40 minutes, and a hub restart in that window
cost the review it was recording.

The frame is a 4-byte big-endian length and that many bytes of UTF-8 JSON.
JSON and not pickle: the hub will read frames from another machine, and a
pickle is code.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import sqlite3
import struct
import tempfile
import time
from pathlib import Path

from . import errors as _errors

#: Bumped when a frame changes shape. The server refuses any other.
PROTOCOL_VERSION = 1

#: A frame larger than this is refused on both sides: a corrupt length
#: prefix must not become a 4 GiB allocation.
MAX_FRAME = 256 * 1024 * 1024
#: Bytes of a serialized image per `chunk` answer; base64 keeps it under `MAX_FRAME`.
CHUNK = 64 * 1024 * 1024

_HEADER = struct.Struct(">I")


class RemoteError(_errors.SdDbError):
    """The hub raised something this side cannot rebuild, or the wire broke."""


class HubUnreachable(RemoteError):
    """No session could be opened, or the session ended mid-request."""

    def __init__(self, host: str, port: int, reason: str) -> None:
        self.host = host
        self.port = port
        self.reason = reason
        super().__init__(f"the sd hub at {host}:{port} is unreachable: {reason}")


class UnknownOutcome(RemoteError):
    """The answer to `COMMIT` was lost, and the hub has not said what happened.

    Raised when the outcome is still unknown at `OUTCOME_BOUND`: the hub
    stayed unreachable, or a live session still owned R. The transaction
    may have committed. Run the verb again only after the hub answers for R.
    """

    def __init__(self, rid: str, reason: str) -> None:
        self.rid = rid
        self.reason = reason
        super().__init__(
            f"the outcome of write transaction {rid} is unknown: {reason}. It may have "
            f"committed on the hub; do not run the verb again until the hub answers for {rid}"
        )


class TransactionLost(RemoteError):
    """The hub answered `absent` for R: the transaction did not commit.

    No session owns R and no `request_outcome` row names it, so nothing was
    written. It leaves the `with transaction(...)` block the way a busy
    `BEGIN IMMEDIATE` does; the verb is run again under a new id, and its
    body computes every value afresh.
    """

    def __init__(self, rid: str) -> None:
        self.rid = rid
        super().__init__(
            f"write transaction {rid} did not commit: the hub holds no record of it and no "
            f"session owns it, so nothing was written; run the verb again"
        )


#: How long a client asks `outcome(R)` before it raises `UnknownOutcome`.
#: Longer than the hub's open-transaction idle timeout (`serve.IDLE_TIMEOUT`),
#: so an abandoned owner is always settled inside it.
OUTCOME_BOUND = 60.0
#: The first wait between two asks, doubled up to `OUTCOME_LONGEST_WAIT`.
OUTCOME_FIRST_WAIT = 0.1
OUTCOME_LONGEST_WAIT = 5.0
#: Seconds one ask may take to connect or to be answered: a hub that
#: accepts and never answers must not hold the client past the bound.
OUTCOME_ASK_TIMEOUT = 5.0

#: The hub's three answers to `outcome(R)`.
RECORDED, IN_FLIGHT, ABSENT = "recorded", "in_flight", "absent"

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: A ULID: 26 characters of Crockford base32.
REQUEST_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


def new_request_id(now: float | None = None) -> str:
    """A ULID: 48 bits of milliseconds and 80 random bits.

    The random bits make two satellites' ids distinct with no coordination;
    the time prefix orders them.
    """
    millis = int((time.time() if now is None else now) * 1000) & ((1 << 48) - 1)
    value = (millis << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


_BEGIN = re.compile(r"BEGIN(?:\s+(DEFERRED|IMMEDIATE|EXCLUSIVE))?(?:\s+TRANSACTION)?")
_COMMIT = re.compile(r"(?:COMMIT|END)(?:\s+TRANSACTION)?")
_ROLLBACK = re.compile(r"ROLLBACK(?:\s+TRANSACTION)?")


#: Statements that may write, by first word: a write outside a transaction
#: carries a request id (sd:2671). A read, a PRAGMA or a SAVEPOINT does not:
#: the hub would run it inside a transaction, where a PRAGMA can do nothing
#: and a SAVEPOINT opens nothing. A text this misses goes as before, unsettled.
_WRITE = re.compile(r"\s*(INSERT|UPDATE|DELETE|REPLACE|WITH|CREATE|DROP|ALTER)\b", re.IGNORECASE)
#: A read a dropped session may send again on a new one (sd:3336).
_SELECT = re.compile(r"\s*SELECT\b", re.IGNORECASE)


def statement(sql: str) -> str | None:
    """What a statement does to the transaction: `write`, `read`, `commit`,
    `rollback`, or `None` for anything else.

    `write` opens a write transaction (`BEGIN IMMEDIATE` or `EXCLUSIVE`) and
    carries a request id; `read` is a plain or deferred `BEGIN`, which
    carries none. `ROLLBACK TO` a savepoint is not a rollback of the
    transaction. Both sides classify with this one function. A text it
    misses, such as `COMMIT; -- done`, goes as a plain statement, and the
    hub's authorizer refuses the transaction statement in it.
    """
    text = " ".join(sql.strip().rstrip(";").split()).upper()
    begun = _BEGIN.fullmatch(text)
    if begun:
        return "write" if begun.group(1) in ("IMMEDIATE", "EXCLUSIVE") else "read"
    if _COMMIT.fullmatch(text):
        return "commit"
    if _ROLLBACK.fullmatch(text):
        return "rollback"
    return None


#: How the refusal names each of the three handshake fields.
DESCRIBED = {"protocol": "protocol version", "package": "sd_db package version",
             "schema": "SCHEMA_VERSION", "build": "build digest"}


class HubOnly(RemoteError):
    """An operation that runs on the hub only, reached from a satellite.

    Step 6 of the plan. The control gate's file lock (`control_gate`) and
    every directory beside the database live on the hub; the ship lock
    follows the lane host instead (`ship.LaneElsewhere`, sd:3075). A lock held
    over a session that can drop would outlive nothing it guards, and a
    directory resolved here would be one the hub never reads. So the verb
    refuses by name before it locks or writes anything.
    """

    def __init__(self, verb: str, hub: str) -> None:
        self.verb = verb
        self.hub = hub
        super().__init__(
            f"{verb} runs on the sd hub only; this machine reaches the database on "
            f"{hub}. Run it on the hub"
        )


class BuildMismatch(RemoteError):
    """The two sides run different `sd_db` builds; the hub opened nothing.

    Raised by the hub in answer to the first frame, and rebuilt on the
    satellite with the hub's text. A schema number alone is not the check:
    two builds can share `SCHEMA_VERSION` and send different SQL, so the
    package version is compared too, and the protocol version before both.
    `upgrade` names the side whose build is older, `hub` or `satellite`,
    or `both` when the two differ without an order. `hub_build` is the
    hub's build digest. The remedy names `repo-sync.sh refresh` for the hub
    and `repo-sync.sh follow` for a satellite: each moves the pack checkout,
    and the pack's `lib/` is the one `sd_db` (sd:3280).
    """

    #: The hub's build digest, whatever field differs (sd:2802). `None` from
    #: a hub older than the field, which a rebuilt error leaves at this class
    #: default. Kept on the wire for older satellites.
    hub_build: str | None = None

    def __init__(self, field: str, satellite, hub, *, hub_build: str | None = None) -> None:
        self.field = field
        self.satellite = str(satellite)
        self.hub = str(hub)
        self.hub_build = hub_build
        self.upgrade = older(satellite, hub)
        if self.upgrade == "hub":
            remedy = f"run `repo-sync.sh refresh` on the hub for sd_db {satellite}"
        elif self.upgrade == "satellite":
            remedy = f"run `repo-sync.sh follow` on this satellite for sd_db {hub}"
        else:
            remedy = "run `repo-sync.sh refresh` on the hub, then `repo-sync.sh follow` on this satellite"
        super().__init__(
            f"sd_db build mismatch: {DESCRIBED[field]} {satellite} on this satellite, "
            f"{hub} on the hub; {remedy}. The hub ran no statement"
        )


def _numbers(version) -> tuple[int, ...] | None:
    text = str(version)
    if not re.fullmatch(r"\d+(\.\d+)*", text):
        return None
    return tuple(int(part) for part in text.split("."))


def older(satellite, hub) -> str:
    """The side to upgrade: the one whose version sorts lower."""
    ours, theirs = _numbers(satellite), _numbers(hub)
    if ours is None or theirs is None or ours == theirs:
        return "both"
    return "satellite" if ours < theirs else "hub"


_digest: str | None = None


def _hash_files() -> str:
    """This package's Python and SQL as they are on disk right now."""
    return tree_digest(Path(__file__).resolve().parent)


def tree_digest(root: Path | str) -> str:
    """The build digest of the `sd_db` package at `root`, as `build_digest` takes it.

    One algorithm for both: the running package, and a package exported
    from git that a satellite would install (sd:2802).
    """
    import hashlib

    root = Path(root)
    hashed = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.suffix in (".py", ".sql")
                       and p.is_file() and "__pycache__" not in p.parts):
        hashed.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        hashed.update(path.read_bytes() + b"\0")
    return hashed.hexdigest()[:16]


def build_digest() -> str:
    """A digest of this package's own files: the build, whatever its version.

    The package version has not been bumped per build, so two builds with
    different SQL can share it. The digest covers the package's Python and
    its SQL migrations, so any difference in what the two sides would run
    refuses. Nothing else counts, so a stray `.DS_Store` cannot.

    Computed once per process and kept: it names the code this process
    loaded. `serve` takes it before it listens (sd:1480), so an install
    under a running hub cannot change what the hub says it runs.
    """
    global _digest
    if _digest is None:
        _digest = _hash_files()
    return _digest


def disk_holds(mismatch: BuildMismatch) -> bool:
    """Whether this package's files on disk now hold the build the hub named in `mismatch` (pack sd:8182).

    The hub names its build in `hub_build`, or, from a hub older than that
    field, in `hub` when the build itself differed. No digest to compare, or
    files that did not read (a follow still moving them), is unknown: False,
    and the caller asks again at its next beat.
    """
    hub = mismatch.hub_build or (mismatch.hub if mismatch.field == "build" else None)
    if hub is None:
        return False
    try:
        return _hash_files() == hub
    except OSError:
        return False


class HubRestartNeeded(RemoteError):
    """The hub's `sd_db` was installed again after the hub started; it opened nothing.

    The running hub holds the code it loaded at start, and its files now
    hold another build. Neither digest can be compared honestly with the
    satellite's, so every session is refused until the hub restarts.
    """

    def __init__(self, running: str, installed: str) -> None:
        self.running = running
        self.installed = installed
        super().__init__(
            f"the hub's sd_db was installed after the hub started: it runs build {running}, "
            f"its files hold build {installed}; the hub stops so launchd restarts it, or restart `bin/sd-db serve` on the hub. "
            f"The hub ran no statement"
        )


#: Sessions this process opened on a hub. `follow.attempt` reads it: a
#: process past its first session is part way through a verb, and a re-run
#: would do that part twice (sd:4748).
_opened = 0


def sessions_opened() -> int:
    return _opened


def handshake() -> dict:
    """This build, as the first frame states it (step 3).

    Read at call time, not at import: the package has finished importing by
    then, and a test can stand in for another build.
    """
    from . import __version__, schema

    return {"package": __version__, "schema": schema.SCHEMA_VERSION, "build": build_digest()}


def check_handshake(frame: dict) -> None:
    """The hub's side: refuse any build but its own, protocol first.

    The package and the schema come before the digest because their order
    names the side to upgrade; the digest has no order.
    """
    if frame.get("v") != PROTOCOL_VERSION:
        raise BuildMismatch("protocol", frame.get("v"), PROTOCOL_VERSION, hub_build=build_digest())
    # Before the fields: a hub whose files moved under it would compare a
    # build it does not run, and name the wrong remedy.
    installed = _hash_files()
    if installed != build_digest():
        raise HubRestartNeeded(build_digest(), installed)
    ours = handshake()
    for field in ("package", "schema", "build"):
        if frame.get(field) != ours[field]:
            raise BuildMismatch(field, frame.get(field), ours[field], hub_build=ours["build"])


# -- frames ----------------------------------------------------------------


def encode_value(value):
    """One SQL value as JSON. Bytes are the one type JSON lacks."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def decode_value(value):
    if isinstance(value, dict) and "$b" in value:
        return base64.b64decode(value["$b"])
    return value


def encode_params(params):
    if params is None:
        return None
    if isinstance(params, dict):
        return {"$map": {key: encode_value(value) for key, value in params.items()}}
    return [encode_value(value) for value in params]


def decode_params(params):
    if params is None:
        return ()
    if isinstance(params, dict):
        return {key: decode_value(value) for key, value in params["$map"].items()}
    return [decode_value(value) for value in params]


def send_frame(sock: socket.socket, frame: dict) -> None:
    body = json.dumps(frame, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise RemoteError(f"frame of {len(body)} bytes exceeds {MAX_FRAME}")
    sock.sendall(_HEADER.pack(len(body)) + body)


def _read_exactly(sock: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = sock.recv(min(size, 1 << 20))
        if not chunk:
            raise EOFError("the peer closed the session")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def read_frame(sock: socket.socket, limit: int = MAX_FRAME) -> dict:
    (size,) = _HEADER.unpack(_read_exactly(sock, _HEADER.size))
    if size > limit:
        raise RemoteError(f"frame of {size} bytes exceeds {limit}")
    return json.loads(_read_exactly(sock, size).decode("utf-8"))


def request(op: str, *, rid: str | None = None, **fields) -> dict:
    """A request frame. Every one carries the version and the id field."""
    return {"v": PROTOCOL_VERSION, "rid": rid, "op": op, **fields}


# -- errors ----------------------------------------------------------------


def describe_error(error: BaseException) -> dict:
    """An exception as the hub sends it: enough to raise the same one here."""
    kind = type(error)
    described = {
        "module": kind.__module__,
        "name": kind.__qualname__,
        "args": [a if isinstance(a, (str, int, float, bool, type(None))) else str(a) for a in error.args],
        "text": str(error),
    }
    attributes = {
        key: value for key, value in vars(error).items()
        if isinstance(value, (str, int, float, bool, type(None)))
    }
    for key in ("sqlite_errorcode", "sqlite_errorname"):
        if hasattr(error, key):
            attributes[key] = getattr(error, key)
    if attributes:
        described["attributes"] = attributes
    return described


def _error_class(module: str, name: str):
    if module in ("builtins", "sqlite3") or module == "sd_db" or module.startswith("sd_db."):
        try:
            import importlib

            found = importlib.import_module(module)
            for part in name.split("."):
                found = getattr(found, part)
        except (ImportError, AttributeError):
            return None
        if isinstance(found, type) and issubclass(found, BaseException):
            return found
    return None


def rebuild_error(described: dict) -> BaseException:
    """The exception the hub raised, as the same class with the same text.

    `sqlite3` and builtin errors are rebuilt with their arguments. A library
    error is rebuilt without calling its `__init__`, whose arguments are not
    its message: `SchemaTooNew(found, built_for)` is rebuilt from `args` and
    its attributes. Anything else is a `RemoteError` naming the class.
    """
    kind = _error_class(described["module"], described["name"])
    attributes = described.get("attributes", {})
    if kind is None:
        return RemoteError(f"{described['module']}.{described['name']}: {described['text']}")
    if kind.__module__ in ("builtins", "sqlite3"):
        try:
            error = kind(*described["args"])
        except TypeError:
            error = kind(described["text"])
    else:
        error = kind.__new__(kind)
        error.args = tuple(described["args"])
    for key, value in attributes.items():
        try:
            setattr(error, key, value)
        except AttributeError:
            pass
    return error


class StatementRefused(RemoteError, sqlite3.DatabaseError):
    """The hub's authorizer refused a statement (`sd_db.serve.Session._authorize`):
    one that would reach past the served file, or a transaction statement
    the hub does not route. The refused statement did not run. A
    `sqlite3.DatabaseError` too, so a caller that handles SQLite's own
    "not authorized" handles this the same way."""


# -- rows ------------------------------------------------------------------

_shapes: dict[tuple[str, ...], sqlite3.Cursor] = {}


def _shape(names: tuple[str, ...]) -> sqlite3.Cursor:
    """A cursor whose description is `names`, so `sqlite3.Row` can use it.

    `sqlite3.Row(cursor, values)` takes its keys from the cursor's
    description. Building real `sqlite3.Row` objects keeps every behaviour a
    caller relies on -- `row["name"]`, `row[0]`, `keys()`, equality, `dict()` --
    identical to a local connection, instead of a look-alike class that would
    drift. The cursor belongs to a private in-memory database, not the hub's.
    """
    cursor = _shapes.get(names)
    if cursor is None:
        columns = ", ".join('NULL AS "' + name.replace('"', '""') + '"' for name in names) or "NULL"
        memory = sqlite3.connect(":memory:", check_same_thread=False)
        cursor = memory.execute(f"SELECT {columns} WHERE 0")
        _shapes[names] = cursor
    return cursor


class Cursor:
    """The answer to one statement, fetched whole by the hub."""

    arraysize = 1

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.description = None
        self.lastrowid = None
        self.rowcount = -1
        self._rows: list = []
        self._position = 0

    def _load(self, answer: dict) -> Cursor:
        described = answer.get("description")
        self.description = (
            tuple((name, None, None, None, None, None, None) for name in described)
            if described is not None else None
        )
        self.lastrowid = answer.get("lastrowid")
        self.rowcount = answer.get("rowcount", -1)
        factory = self.connection.row_factory
        rows = [tuple(decode_value(value) for value in row) for row in answer.get("rows", [])]
        if factory is sqlite3.Row and described is not None:
            shape = _shape(tuple(described))
            rows = [sqlite3.Row(shape, row) for row in rows]
        elif factory is not None:
            rows = [factory(self, row) for row in rows]
        self._rows = rows
        self._position = 0
        return self

    def execute(self, sql: str, parameters=()) -> Cursor:
        return self._load(self.connection._execute(sql, encode_params(parameters)))

    def executemany(self, sql: str, seq_of_parameters) -> Cursor:
        return self._load(self.connection._call(
            "executemany", sql=sql, params=[encode_params(p) for p in seq_of_parameters],
        ))

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchmany(self, size: int | None = None) -> list:
        size = self.arraysize if size is None else size
        rows = self._rows[self._position:self._position + size]
        self._position += len(rows)
        return rows

    def fetchall(self) -> list:
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self) -> None:
        self._rows = []
        self._position = 0


class Connection:
    """A hub-side `sqlite3.Connection`, reached over one socket.

    One session is one hub connection: `in_transaction` is the hub's, sent
    back with every answer, and closing the socket makes SQLite roll back
    whatever the session left open.

    `outcomes` lists, per lost `COMMIT` answer, each `(R, answer)` the hub
    gave while this connection asked for R; `unreachable` stands for an ask
    that reached no hub, and `refused` for one the hub refused for the build.
    """

    def __init__(self, host: str, port: int, *, path: Path | str | None, write: bool,
                 create: bool, busy_timeout: int, token,
                 timeout: float | None = None, answer_timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.row_factory = sqlite3.Row
        self.in_transaction = False
        self.outcomes: list[tuple[str, str]] = []
        self._socket = None
        self._trace = None
        # What a session that asks for an outcome opens: the same file, as
        # the same user, with the token read afresh (a restarted hub writes
        # a new one).
        self._path = None if path is None else str(path)
        self._busy_timeout = busy_timeout
        self._token = token
        self._timeout = timeout
        # What a reopened session needs to open as this one did (sd:3336).
        self._write = write
        self._create = create
        self._answer_timeout = answer_timeout
        #: The request id of the write transaction open on this session.
        self._rid: str | None = None
        #: The session ended under a request, not by `close`.
        self._lost = False
        self._open()

    def _open(self) -> None:
        """Dial the hub and send `open`, for a new connection or a reopened session."""
        token = self._token
        try:
            self._socket = socket.create_connection((self.host, self.port), timeout=self._timeout)
        except OSError as error:
            raise HubUnreachable(self.host, self.port, error.strerror or str(error)) from error
        self._socket.settimeout(self._answer_timeout)
        # The token proves the client can read the server's owner-only token
        # file, so a loopback session stays inside the owner's account. A
        # callable is read only once the socket is up: the server deletes
        # its token file when it stops, and a stopped hub is unreachable,
        # not misconfigured.
        if callable(token):
            try:
                token = token()
            except BaseException:
                self._abandon()
                raise
        self._call("open", path=self._path, write=self._write,
                   create=self._create, busy_timeout=self._busy_timeout, token=token, **handshake())
        global _opened
        _opened += 1

    def _reopen(self, lost: HubUnreachable | None) -> None:
        """A new session in place of one that dropped while it held nothing (sd:3336).

        The hub rolled back whatever the old session left, so nothing on it
        carries over except what `open` and `trace` set. A hub that cannot
        be reached raises its own `HubUnreachable`, chained to the drop.
        """
        self._abandon()
        try:
            self._open()
        except HubUnreachable as error:
            self._lost = True
            raise error from lost
        self._lost = False
        if self._trace is not None:
            self._call("trace", on=True)

    def _unit(self, sql: str, send):
        """`send()` once, and once more on a new session if this one dropped (sd:3336).

        Only a statement that starts a unit and cannot have written: a
        `BEGIN` or a `SELECT`, sent while no transaction and no request id
        is open. A lost `BEGIN IMMEDIATE` wrote nothing: the hub rolls back
        a session that closes, and `send` makes a fresh id. Every other
        statement fails as it did.
        """
        starts = statement(sql) in ("write", "read") or _SELECT.match(sql)
        if self.in_transaction or self._rid is not None or not starts:
            return send()
        if self._socket is None and self._lost:
            self._reopen(None)
            return send()
        try:
            return send()
        except HubUnreachable as lost:
            if self._socket is not None:
                raise
            self._reopen(lost)
            return send()

    def _call(self, op: str, **fields) -> dict:
        if self._socket is None:
            if self._lost:
                # A dropped session is a hub fault (`hub_unavailable` to
                # sd-ship), not a connection its caller closed.
                raise HubUnreachable(self.host, self.port, "the session dropped under an earlier request")
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        try:
            send_frame(self._socket, request(op, **fields))
            answer = read_frame(self._socket)
        except (OSError, EOFError) as error:
            self._abandon()
            self._lost = True
            raise HubUnreachable(self.host, self.port, str(error)) from error
        if "in_transaction" in answer:
            self.in_transaction = answer["in_transaction"]
        if self._trace is not None:
            for statement in answer.get("trace", ()):
                self._trace(statement)
        if not answer.get("ok"):
            if op == "open":
                self._abandon()
            raise rebuild_error(answer["error"])
        return answer

    def _execute(self, sql: str, params) -> dict:
        """One statement, with the request id a transaction statement needs."""
        kind = statement(sql)
        if kind == "write":
            # A fresh id per `BEGIN IMMEDIATE`, never reused: the hub refuses
            # an id it has seen.
            def begin() -> dict:
                rid = new_request_id()
                answer = self._call("execute", rid=rid, sql=sql, params=params)
                self._rid = rid
                return answer

            return self._unit(sql, begin)
        if kind is None and not self.in_transaction and _WRITE.match(sql):
            rid = new_request_id()
            try:
                return self._call("execute", rid=rid, sql=sql, params=params)
            except HubUnreachable as lost:
                self._settle(rid, lost, "the write")
                return {"ok": True, "in_transaction": False}
        if kind == "commit" and self._rid is not None:
            return self._commit("execute", sql=sql, params=params)
        if kind == "rollback":
            return self._rollback("execute", sql=sql, params=params)
        return self._unit(sql, lambda: self._call("execute", sql=sql, params=params))

    def _commit(self, op: str, **fields) -> dict:
        """`COMMIT` with R; a lost answer is settled by asking the hub for R."""
        rid = self._rid
        try:
            answer = self._call(op, rid=rid, **fields)
        except HubUnreachable as lost:
            self._rid = None
            self.in_transaction = False
            self._settle(rid, lost, "COMMIT")
            return {"ok": True, "in_transaction": False}
        except Exception:
            # Refused or failed on the hub: the transaction is still open
            # unless SQLite ended it, and then R is settled there too.
            if not self.in_transaction:
                self._rid = None
            raise
        if not self.in_transaction:
            self._rid = None
        return answer

    def _rollback(self, op: str, **fields) -> dict:
        self._rid = None
        if self._socket is None and self._lost:
            # The session ended under a request: the hub rolled back
            # whatever it held, and nothing unowned can commit. The error
            # that ended it is the one the caller sees.
            self.in_transaction = False
            return {"ok": True, "in_transaction": False}
        return self._call(op, **fields)

    def _settle(self, rid: str, lost: HubUnreachable, what: str) -> None:
        """Ask the hub for R until it says `recorded` or `absent`, or the bound.

        Each ask opens its own session: this one is gone. `recorded` returns,
        as the `COMMIT` would have. `absent` raises `TransactionLost`. A live
        owner (`in_flight`) or no hub at all is asked again, waiting longer
        each time, until `OUTCOME_BOUND`; then `UnknownOutcome`. A hub that
        refuses this build, or whose database is newer than it, ends it at
        once (sd:3359): this process keeps the build it loaded, so no later
        ask can pass.
        """
        deadline = time.monotonic() + OUTCOME_BOUND
        wait = OUTCOME_FIRST_WAIT
        reason = f"the answer to {what} was lost ({lost.reason})"
        while True:
            try:
                answer = self.outcome(rid)
            except (BuildMismatch, _errors.SchemaTooNew) as refused:
                self.outcomes.append((rid, "refused"))
                raise UnknownOutcome(rid, f"{reason}, and the hub refuses this build: {refused}") from refused
            except Exception as error:  # no hub, or a hub that cannot answer yet
                answer = "unreachable"
                reason = f"the answer to {what} was lost ({lost.reason}), and the hub " \
                         f"could not be asked for it ({error})"
            self.outcomes.append((rid, answer))
            if answer == RECORDED:
                return
            if answer == ABSENT:
                raise TransactionLost(rid) from UnknownOutcome(rid, reason)
            if answer == IN_FLIGHT:
                reason = (f"the answer to {what} was lost ({lost.reason}), and a session on "
                          f"the hub still owns {rid}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UnknownOutcome(rid, f"{reason}; asked for {OUTCOME_BOUND:g} s") from lost
            time.sleep(min(wait, remaining))
            wait = min(wait * 2, OUTCOME_LONGEST_WAIT)

    def outcome(self, rid: str) -> str:
        """The hub's answer for request id R: `recorded`, `in_flight` or `absent`.

        Asked on a fresh read session of the same file, so it works after
        this session is gone.
        """
        asking = Connection(self.host, self.port, path=self._path, write=False, create=False,
                            busy_timeout=self._busy_timeout, token=self._token,
                            timeout=OUTCOME_ASK_TIMEOUT, answer_timeout=OUTCOME_ASK_TIMEOUT)
        try:
            return asking._call("outcome", rid=rid)["value"]
        finally:
            asking.close()

    def _abandon(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def cursor(self) -> Cursor:
        if self._socket is None and not self._lost:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        return Cursor(self)

    def execute(self, sql: str, parameters=()) -> Cursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters) -> Cursor:
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str) -> Cursor:
        cursor = self.cursor()
        self._call("executescript", sql=sql_script)
        return cursor

    @property
    def total_changes(self) -> int:
        return self._call("total_changes")["value"]

    def iterdump(self):
        return iter(self._call("iterdump")["value"])

    def registry_bytes(self) -> bytes:
        """The hub's `providers.yaml`, beside the database this session opened.

        Seam 7 of the design: the registry is the one file beside the
        database a satellite reads. `RegistryError` when the hub has none.
        """
        import io

        size = self._call("registry")["value"]
        sink = io.BytesIO()
        self._fetch(size, sink)
        return sink.getvalue()

    def _fetch(self, size: int, sink) -> None:
        """Read the `size` bytes the hub staged into `sink`, one `chunk` at a time."""
        while sink.tell() < size:
            chunk = decode_value(self._call("chunk", offset=sink.tell(), length=CHUNK)["value"])
            if not chunk:
                raise RemoteError(f"the hub's image ended at {sink.tell()} of {size} bytes")
            sink.write(chunk)

    def set_trace_callback(self, callback) -> None:
        """Traced on the hub, where the statements run, and replayed here.

        The hub's trace is SQLite's own, so the callback sees what a local
        one sees: expanded SQL, one statement at a time.
        """
        self._trace = callback
        self._call("trace", on=callback is not None)

    def backup(self, target: sqlite3.Connection, *, pages: int = -1, progress=None,
               name: str = "main", sleep: float = 0.25) -> None:
        """Copy the hub's database into a connection on this side.

        The hub sends its image (`serialize`); it lands in a private file
        here, and SQLite's own backup copies it into `target` with the
        caller's pages, progress and sleep. A file and not `deserialize`: an
        in-memory image of a WAL database will not open, and patching its
        header would hand `target` a journal mode the hub's file lacks.
        """
        size = self._call("serialize", name=name)["value"]
        with tempfile.TemporaryDirectory() as folder:
            staged = Path(folder) / "image.db"
            # The image comes in chunks, so its size is not bounded by one
            # frame (`MAX_FRAME`).
            with open(staged, "wb") as image:
                self._fetch(size, image)
            staging = sqlite3.connect(staged)
            try:
                staging.backup(target, pages=pages, progress=progress, sleep=sleep)
            finally:
                staging.close()

    def commit(self) -> None:
        if self._rid is not None:
            self._commit("commit")
        else:
            self._call("commit")

    def rollback(self) -> None:
        self._rollback("rollback")

    def close(self) -> None:
        if self._socket is None:
            return
        try:
            self._call("close")
        except HubUnreachable:
            pass
        self._abandon()
        self.in_transaction = False

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, kind, value, traceback) -> bool:
        if kind is None:
            self.commit()
        else:
            self.rollback()
        return False

    def __del__(self) -> None:
        try:
            self._abandon()
        except Exception:
            pass


def connect(host: str, port: int, target: Path | str | None, *, token, write: bool = True,
            create: bool = False, busy_timeout: int = 5000,
            timeout: float | None = None, answer_timeout: float | None = None) -> Connection:
    """Open a session on the hub at `host:port`.

    `token` is the content of the server's `<database>.serve.token`, which
    only the server's owner can read, or a callable that reads it once the
    socket is up. A `target` of `None` opens the hub's own database.
    `timeout` bounds the connect, `answer_timeout` each wait for an answer.
    """
    return Connection(host, port, path=target, write=write, create=create,
                      busy_timeout=busy_timeout, token=token, timeout=timeout,
                      answer_timeout=answer_timeout)
