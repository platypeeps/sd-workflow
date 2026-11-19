"""The hub half: serve one database to `sd_db.remote` connections.

    python -m sd_db.serve [--loopback] [--port N] [--database PATH]

Steps 2 and 7 of `docs/work/2026-09-22-run-the-framework-from-a-second-machine/`.
Each session is one TCP connection and one hub-side connection from the
library's own local open, so a remote statement meets the same pragmas, the
same version refusals and the same SQLite write lock as a local one.

One server per file. The server takes `<database>.serve.lock` beside it
through `runner_journal.lock` before it listens, and a second server over the
same file exits non-zero naming the lock. Ownership of a request id (step 5)
lives in the server's memory, so two servers over one file would be two
registries.

Two listeners, one per run:

* `--loopback` binds 127.0.0.1. Loopback is not a user boundary: every
  local account reaches it. The server writes a fresh token to
  `<database>.serve.token` beside it, mode 0600, and refuses an `open` frame
  without it. A client that sends it could read the owner's file, so it
  runs as the owner or as root.
* Without it, the server binds this node's Tailscale IPv4 address and
  carries no token (R7). It admits a session by its TCP peer, through
  `sd_db.tailnet`: the rules the dashboard's direct listener uses. A tagged
  or expired node, a login other than this node's owner, and this node's
  own addresses are refused before anything opens (R8, criterion 6). The
  last one closes the hub's second-account path: `whois` names a node's
  owner, not the account that dialed (design, Q3 = A).

The `open` frame carries the client's build (step 3): its protocol version,
its package version and its `SCHEMA_VERSION`. The server refuses any
difference from its own with `sd_db.remote.BuildMismatch`, naming the side
to upgrade, before it opens the file.

A loopback session may name the file it opens. That is what lets the
library's suite, which builds a database per test, run over the wire;
behind the token it grants nothing the owner's own processes lack. A
tailnet session opens the served database and nothing else.

The server logs, per write transaction, the longest gap between two frames
inside it, and on exit the longest across the run (gap (g) of the design).

Step 5, the unknown outcome; the operator's ruling of 2026-10-04 builds it
without the prune, so no row is ever deleted:

- `BEGIN IMMEDIATE` carries a request id R. The session owns R from before
  the statement runs, and an id seen before is refused.
- `COMMIT` from the owner inserts the `request_outcome` row on the same
  connection, then commits: the row and the writes land together or not
  at all. Each attempt checks for the row inside the transaction: a COMMIT
  a deferred foreign key failed leaves the transaction open, and a
  `ROLLBACK TO` can remove the row it inserted. A transaction that wrote
  nothing gets no row: committed or not, it wrote nothing, and the table,
  which is never pruned, does not grow on quiet ticks. "Wrote" is
  measured two ways. `total_changes` counts every row an INSERT, UPDATE
  or DELETE changed, in any database, so a replay whose UPDATE matches
  nothing stays empty. Every other action that is not a read, in any
  database, counts as a write the moment the authorizer sees it
  prepared: DDL, a pragma that sets a value, ATTACH (the third review of
  2026-10-04). ATTACH is refused: a COMMIT that spans an attached
  database would not be crash-atomic with the row in `main`. A row too many says only that the transaction committed,
  which it did. The server's row stays out of the session's
  `total_changes`. A `COMMIT` naming an R the session does not own is
  refused and not applied, and a write transaction's `COMMIT` without its
  R too.
- `ROLLBACK`, a transaction SQLite ends itself, and the socket closing
  settle ownership.
- `outcome(R)` answers `in_flight` while a session owns R, else `recorded`
  when the row exists, else `absent`. Admission, settlement and the answer
  run under one lock per id, so absence proves the transaction ended
  without committing: nothing unowned can commit. `absent` marks R seen,
  so a frame carrying R that lands later is refused.
- A statement outside a transaction that carries R (sd:2671) runs as a
  write transaction of its own: a plain `BEGIN`, the statement, and the
  `COMMIT` above, row and all. A deferred `BEGIN` locks as the autocommit
  statement would.
- A plain `BEGIN` carries no R and opens a read transaction under
  `query_only` (gap C2). Its `COMMIT` or `ROLLBACK` needs no R and writes
  no row.
- SQLite's authorizer on the session's connection refuses BEGIN, COMMIT
  and ROLLBACK unless the hub routes them, whatever the text: a comment
  or a trailing statement cannot end a write transaction without its row
  (the review of 2026-10-04). A transaction a `SAVEPOINT` opens is a read
  under `query_only`. A script runs outside any transaction the hub
  holds, carries no R, may not BEGIN, COMMIT or ROLLBACK, and must end
  the transaction a SAVEPOINT in it opens. A client
  cannot set `query_only` inside a transaction the hub holds (the second
  review of 2026-10-04); outside one it is the session's, which a read
  saves and restores. The client does not check any of this itself: a
  text `remote.statement` misses goes as a plain statement, and the
  refusal answers it.
- A session silent for `IDLE_TIMEOUT` inside an open transaction is closed,
  which rolls it back: below `database.BUSY_TIMEOUT`, so a hub writer
  waiting behind an abandoned satellite gets the lock (gap (g)).

Step 7 keeps a session's SQL inside the served file, in the same
authorizer. Besides ATTACH it refuses every VACUUM (SQLite authorizes one as
the ATTACH it runs), `load_extension`, and any PRAGMA off `PRAGMAS`, or off
`PRAGMAS_WITH_ARGUMENT` when it gives a value. A refusal goes back as
`remote.StatementRefused`, is logged, and the session keeps serving.
"""

from __future__ import annotations

import argparse
import hmac
import os
import secrets
import signal
import socket
import socketserver
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import database, hub, remote, tailnet
from .runner import RunnerRefused
from .runner_journal import lock
from .writes import now

LOOPBACK = "127.0.0.1"
#: Suffixes on the database's own file name: one lock and one token per
#: database, so two files in one folder are served side by side.
LOCK_SUFFIX = ".serve.lock"
TOKEN_SUFFIX = ".serve.token"
#: The largest first frame a session may send. An `open` frame is a few
#: hundred bytes, and a peer not yet admitted gets no larger allocation.
OPEN_LIMIT = 64 * 1024


class PeerRefused(remote.RemoteError):
    """The session's TCP peer is not one this listener admits. Nothing opened."""


#: This machine no longer holds the hub role (hub failover, D5); the session closes and rolls back.
Fenced = hub.Fenced


def fenced_by(found: hub.Role) -> Fenced:
    """The `Fenced` a session raises on a refusing role, its `state` and `cause` the role's (pack sd:9543)."""
    return Fenced(f"refused: this machine is not the hub: {found.reason}", state=found.state, cause=found.cause)


#: The PRAGMAs a session may run, from a grep for `PRAGMA` across
#: `local-sd-db/sd_db`, `local-sd-runner` and `local-project-dashboard`
#: (2026-10-04, step 7's review); the command pack's `bin/` names none.
#: `foreign_key_list` is the table-valued `pragma_foreign_key_list` in
#: `removal`. `page_count` is the one the `serialize` op runs inside
#: SQLite. `writable_schema` is read only, to see a migration left it off.
#: Every other PRAGMA is refused, its setting form above all.
PRAGMAS = frozenset({
    "busy_timeout", "data_version", "database_list", "foreign_key_check", "foreign_key_list",
    "foreign_keys", "index_info", "index_list", "integrity_check", "journal_mode", "page_count",
    "query_only", "table_info", "user_version", "writable_schema",
})
#: Those a session may give an argument: a table to describe, or a setting
#: of its own connection. Setting `journal_mode` or `user_version` changes
#: the file for every reader, so a session reads them only. `query_only`
#: is the session's outside a transaction the hub holds (`Session._authorize`).
PRAGMAS_WITH_ARGUMENT = frozenset({
    "busy_timeout", "foreign_key_check", "foreign_key_list", "foreign_keys", "index_info",
    "index_list", "integrity_check", "query_only", "table_info",
})


#: Seconds a session may stay silent inside an open transaction before the
#: server closes it, which rolls it back. Below `database.BUSY_TIMEOUT`
#: (5 s): a hub writer that starts waiting when a satellite goes silent gets
#: the lock before its own wait ends (gap (g) of the design).
IDLE_TIMEOUT = 4.0
#: Seconds the tailnet listener trusts its last fence answer (sd:4044). An
#: old hub that slept or lost its network keeps its process while the console
#: renames it, so the check at start is not enough: every frame asks again
#: past this, or past a wall clock that moved `CLOCK_SLACK` more than the
#: monotonic one, which does not count a sleep. A commit trusts it too, and
#: a write transaction's BEGIN asks again past half of it, before the write
#: lock: a fresh ask at the commit held the lock, and every frame, for a
#: Tailscale call (pack sd:7042).
#: One value with the write fence on the hub's own database (`hub.write_role`).
FENCE_SECONDS = hub.FENCE_SECONDS
CLOCK_SLACK = hub.CLOCK_SLACK
#: What `Session._authorize` counts as a read, with `load_extension` left
#: out of `SQLITE_FUNCTION` and a `PRAGMA` read only with no argument.
#: SAVEPOINT, RELEASE and ROLLBACK TO move no data: the library's nested
#: `transaction()` uses them in a write that may change nothing.
READS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                   sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT})
#: Row writes, which `total_changes` counts in every database. A DROP TABLE
#: or the REPLACE half of an upsert changes rows it does not count, but the
#: first is `SQLITE_DROP_TABLE` and the second comes with its counted insert.
ROWS = frozenset({sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})

#: A test seam, `None` everywhere else: called as `_fault(point, session)` at
#: `before-commit` (the `COMMIT` frame received, nothing recorded yet) and at
#: `after-commit` (committed, nothing answered yet). A module attribute and
#: not an environment variable, for `database._opener`'s reason.
_fault = None


class Owners:
    """Which session owns each request id, with one lock per id.

    In memory: a hub that restarts has rolled back every transaction it held
    open, so an empty registry is the truth after a restart. Ids admitted
    since the server started stay in `seen`, so an id is used once; a
    recorded id is refused from its row across restarts too.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._owners: dict[str, int] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._seen: set[str] = set()

    def lock(self, rid: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(rid, threading.RLock())

    def admit(self, rid: str, session: int) -> bool:
        """Own `rid` for `session`, unless any session ever owned it."""
        with self._guard:
            if rid in self._seen:
                return False
            self._seen.add(rid)
            self._owners[rid] = session
            return True

    def owner(self, rid: str) -> int | None:
        with self._guard:
            return self._owners.get(rid)

    def settle(self, rid: str) -> None:
        with self._guard:
            self._owners.pop(rid, None)
            self._locks.pop(rid, None)


def _request_id(rid) -> str:
    if not isinstance(rid, str) or not remote.REQUEST_ID.fullmatch(rid):
        raise remote.RemoteError(f"refused: {rid!r} is not a request id (a ULID)")
    return rid


class Log:
    """One line per event on a stream, and the run's longest gap."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.guard = threading.Lock()
        self.longest_gap = 0.0
        self.longest_where = ""
        self.transactions = 0

    def line(self, text: str) -> None:
        with self.guard:
            self.stream.write(f"sd-db serve: {text}\n")
            self.stream.flush()

    def transaction(self, session: int, frames: int, gap: float, wall: float, after: str) -> None:
        """`after` is the frame that ended the longest gap: what the body was
        doing before it sent that is what a finding names."""
        with self.guard:
            self.transactions += 1
            if gap > self.longest_gap:
                self.longest_gap = gap
                self.longest_where = after
        self.line(
            f"session {session} write transaction: {frames} frames, "
            f"longest gap {gap * 1000:.1f} ms, wall {wall * 1000:.1f} ms, before {after}"
        )

    def summary(self) -> str:
        return (
            f"longest in-transaction frame gap {self.longest_gap * 1000:.1f} ms "
            f"across {self.transactions} write transactions, before {self.longest_where}"
        )


def _answer(connection: sqlite3.Connection, cursor: sqlite3.Cursor | None) -> dict:
    answer = {"ok": True, "in_transaction": connection.in_transaction}
    if cursor is not None:
        rows = cursor.fetchall()
        answer["description"] = (
            [column[0] for column in cursor.description] if cursor.description is not None else None
        )
        answer["rows"] = [[remote.encode_value(value) for value in row] for row in rows]
        answer["lastrowid"] = cursor.lastrowid
        answer["rowcount"] = cursor.rowcount
    return answer


def _brief(frame: dict) -> str:
    """A frame in one short, quoted line, for the log."""
    text = " ".join(str(frame.get("sql") or frame.get("op")).split())
    return repr(text[:100])


class Session(socketserver.BaseRequestHandler):
    """One remote connection, from its `open` frame to its socket closing."""

    counter = 0
    counter_guard = threading.Lock()

    def setup(self) -> None:
        #: What opened the open transaction: `write` (with `rid`), `read`,
        #: or `None` for none, or one opened some other way.
        self.kind: str | None = None
        self.rid: str | None = None
        #: `total_changes` when the open write transaction began.
        self.mark = 0
        #: The open write transaction prepared a write that moves no row.
        self.wrote = False
        #: Rows the server inserted, kept out of `total_changes`.
        self.hidden = 0
        #: The connection's own `query_only`, restored when a read ends.
        self.query_only = 0
        self.traced: list[str] = []
        #: The hub runs or routes the statement now, so `_authorize` passes it.
        self.routing = False
        #: Why `_authorize` refused the statement, for the error frame.
        self.denied: str | None = None

    def _authorize(self, action: int, arg1, arg2, database_name, trigger) -> int:
        """SQLite's authorizer on the session's connection.

        SQLite calls it for each statement it prepares, with what the
        statement does, so a transaction boundary or a write is seen
        whatever its text looks like: a comment, a trailing statement, any
        case, any database. One case per action code; later rules add
        theirs here. The session caches no statement (`open_local`'s
        `cached_statements=0`): Python does not authorize a cached one
        again, and a write it reused would leave `wrote` unset.
        """
        if action == sqlite3.SQLITE_ATTACH:
            # Under WAL a COMMIT that spans an attached database is not
            # crash-atomic with `main`'s outcome row, and a TEMP trigger can
            # write one under the hub's own statements (the sixth and seventh
            # findings of 2026-10-04). Refused whoever runs it: with nothing
            # attached, no path writes outside `main` and `temp`. SQLite
            # authorizes every VACUUM as the ATTACH it runs first, so
            # `VACUUM INTO` a file, and a bare VACUUM, end here too (step 7).
            return self._deny(f"ATTACH or VACUUM would open {arg1 or 'a temporary file'} as the hub; "
                              f"the hub serves its own database only")
        if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() not in (
                PRAGMAS if arg2 is None else PRAGMAS_WITH_ARGUMENT):
            # A pragma off the allowlist could rewrite the schema, the
            # file's header or its journal for every reader (step 7).
            shown = f"PRAGMA {(arg1 or '').lower()}" + ("" if arg2 is None else f"({arg2})")
            return self._deny(f"{shown} is not served over the wire")
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
            # Off on the connection already; refused by name all the same.
            return self._deny("load_extension is not served over the wire")
        if (not self.routing and self.kind != "write" and self.server.node is not None  # type: ignore[attr-defined]
                and action not in READS and action != sqlite3.SQLITE_PRAGMA):
            # A write outside the hub's write transaction commits when it ends, after
            # any fence answer taken before it, and a batch commits each write; only
            # `_commit` asks at the commit itself (sd:4044). The client sends a
            # lone write with a request id, which `_autocommit` wraps. No allowed
            # PRAGMA with an argument writes the file.
            return self._deny("a write outside a write transaction is served on the tailnet only with a "
                              "request id, so its COMMIT asks the fence; send a batch inside "
                              "BEGIN IMMEDIATE ... COMMIT")
        if not self.routing and action not in ROWS and not (
                action in READS and (arg2 or "").lower() != "load_extension"
                or action == sqlite3.SQLITE_PRAGMA and arg2 is None):
            # Anything not a read may write, in any database; `_commit`
            # counts row writes itself. The hub's own statements run under
            # `routing` and do not count.
            self.wrote = True
        if action == sqlite3.SQLITE_TRANSACTION:
            # BEGIN, COMMIT and ROLLBACK run only as `_statement` routes
            # them, so a write transaction owns its id and records its row.
            if self.routing:
                return sqlite3.SQLITE_OK
            return self._deny(f"{arg1} outside the hub's transaction handling; send BEGIN IMMEDIATE, "
                              f"BEGIN, COMMIT or ROLLBACK as the whole statement, with no comment")
        if action == sqlite3.SQLITE_PRAGMA:
            # SQLite passes the pragma's name as `arg1` and its value as
            # `arg2`, whatever the case, spacing or schema prefix. Inside a
            # read the hub holds, `query_only` keeps the read's COMMIT or
            # RELEASE from committing writes no id owns, so inside any
            # transaction the hub holds, only the hub sets it: its own
            # statements run in `_quiet`, under `routing`. Outside one it is
            # the session's, which a read saves and restores: a backup of a
            # database waiting for `migrate` switches it off on a `mode=ro`
            # open. Reading it is always allowed.
            if (arg1 or "").lower() == "query_only" and arg2 is not None \
                    and self.kind is not None and not self.routing:
                return self._deny("query_only is the hub's inside a transaction; "
                                  "set it before BEGIN")
        return sqlite3.SQLITE_OK

    def _deny(self, reason: str) -> int:
        self.denied = f"refused: {reason}"
        return sqlite3.SQLITE_DENY

    @contextmanager
    def _routed(self):
        """A transaction statement the hub runs on purpose."""
        self.routing = True
        try:
            yield
        finally:
            self.routing = False

    def _quiet(self, connection: sqlite3.Connection, sql: str, params=()) -> sqlite3.Cursor:
        """A statement of the server's own, kept out of the session's trace."""
        mark = len(self.traced)
        try:
            with self._routed():
                return connection.execute(sql, params)
        finally:
            del self.traced[mark:]

    def _recorded(self, connection: sqlite3.Connection, rid: str) -> bool:
        # `main.`: a TEMP table of the same name would take the row (the
        # fifth finding of 2026-10-04).
        return self._quiet(connection, "SELECT 1 FROM main.request_outcome WHERE id = ?", (rid,)).fetchone() is not None

    def _statement(self, connection: sqlite3.Connection, frame: dict) -> dict:
        sql = frame["sql"]
        params = remote.decode_params(frame.get("params"))
        kind = remote.statement(sql)
        if kind == "write" and not connection.in_transaction:
            return self._begin_write(connection, frame.get("rid"), sql, params)
        if kind == "read" and not connection.in_transaction:
            return self._begin_read(connection, frame.get("rid"), sql, params)
        if kind == "commit":
            return self._commit(connection, frame.get("rid"), lambda: connection.execute(sql, params))
        if kind is None and frame.get("rid") is not None and not connection.in_transaction:
            return self._autocommit(connection, frame["rid"], sql, params)
        if kind is not None:
            # ROLLBACK, or a BEGIN inside a transaction, which SQLite refuses.
            with self._routed():
                return _answer(connection, connection.execute(sql, params))
        return _answer(connection, connection.execute(sql, params))

    def _begin_write(self, connection: sqlite3.Connection, rid, sql: str, params) -> dict:
        """Own R from before the statement runs; an id seen before is refused."""
        if rid is None:
            raise remote.RemoteError(
                f"refused: {' '.join(sql.split())!r} opens a write transaction, which needs a "
                f"request id; this client sent none"
            )
        rid = _request_id(rid)
        if self.server.node is not None:  # type: ignore[attr-defined]
            # Before BEGIN takes the write lock, so a short transaction's commit
            # trusts this answer and asks Tailscale nothing under it (sd:7042).
            fenced = self.server.fence(within=FENCE_SECONDS / 2)  # type: ignore[attr-defined]
            if fenced is not None:
                raise fenced_by(fenced)
        owners = self.server.owners  # type: ignore[attr-defined]
        with owners.lock(rid):
            if not owners.admit(rid, self.number):
                raise remote.RemoteError(f"refused: request id {rid} was used before; "
                                         f"a write transaction takes a new id")
            self.wrote = False
            try:
                # Before BEGIN: only R's owner writes R's row, so nothing can
                # add it in between, and a read inside a deferred BEGIN would
                # fix a snapshot its write then fails on (sd:2671).
                if self._recorded(connection, rid):
                    raise remote.RemoteError(f"refused: request id {rid} has committed before; "
                                             f"a write transaction takes a new id")
                with self._routed():
                    answer = _answer(connection, connection.execute(sql, params))
            except BaseException:
                if connection.in_transaction:
                    self._quiet(connection, "ROLLBACK")
                owners.settle(rid)
                raise
            self.kind, self.rid, self.mark = "write", rid, connection.total_changes
        return answer

    def _autocommit(self, connection: sqlite3.Connection, rid, sql: str, params) -> dict:
        """A write sent outside a transaction with R: its own write transaction (sd:2671)."""
        self._begin_write(connection, rid, "BEGIN", ())
        try:
            answer = _answer(connection, connection.execute(sql, params))
            self._commit(connection, rid, lambda: connection.execute("COMMIT"))
        except BaseException:
            if connection.in_transaction:
                self._quiet(connection, "ROLLBACK")
            self._settle(connection)
            raise
        answer["in_transaction"] = connection.in_transaction
        return answer

    def _begin_read(self, connection: sqlite3.Connection, rid, sql: str, params) -> dict:
        """A plain `BEGIN`: one snapshot, no id, no row, nothing written (gap C2)."""
        if rid is not None:
            raise remote.RemoteError(
                "refused: a plain BEGIN opens a read transaction, which carries no request id; "
                "a write transaction begins IMMEDIATE"
            )
        before = int(self._quiet(connection, "PRAGMA query_only").fetchone()[0])
        self._quiet(connection, "PRAGMA query_only = ON")
        try:
            with self._routed():
                answer = _answer(connection, connection.execute(sql, params))
        except BaseException:
            self._quiet(connection, f"PRAGMA query_only = {before}")
            raise
        self.kind, self.query_only = "read", before
        return answer

    def _commit(self, connection: sqlite3.Connection, rid, run) -> dict:
        """`COMMIT`: from R's owner it records R inside the transaction first."""
        if self.kind == "write":
            if rid is None:
                raise remote.RemoteError(
                    f"refused: COMMIT of write transaction {self.rid} carries no request id; "
                    f"nothing was committed"
                )
            if rid != self.rid:
                raise remote.RemoteError(
                    f"refused: COMMIT names request {rid}, which this session does not own; "
                    f"nothing was committed"
                )
            with self.server.owners.lock(rid):  # type: ignore[attr-defined]
                if _fault is not None:
                    _fault("before-commit", self)
                if self.server.node is not None:  # type: ignore[attr-defined]
                    # At most `FENCE_SECONDS` old and never across a sleep (sd:4044), as
                    # the frame's own check; asked afresh it held the write lock (sd:7042).
                    fenced = self.server.fence()  # type: ignore[attr-defined]
                    if fenced is not None:
                        raise fenced_by(fenced)
                # A transaction that changed nothing gets no row: committed
                # or not, it wrote nothing, so `absent` and its "run the verb
                # again" stay true, and a quiet tick adds no row to the hub.
                # The row is looked for at each attempt, never remembered: a
                # failed COMMIT leaves the transaction open, and a ROLLBACK TO
                # may since have removed the row it inserted.
                if (self.wrote or connection.total_changes != self.mark) and not self._recorded(connection, rid):
                    self._quiet(connection, "INSERT INTO main.request_outcome (id, committed_at) VALUES (?, ?)",
                                (rid, now()))
                    self.hidden += 1
                with self._routed():
                    answer = _answer(connection, run())
                if _fault is not None:
                    _fault("after-commit", self)
                self._settle(connection)
            return answer
        if rid is not None:
            # A COMMIT for an id this session never owned: refused whatever
            # the session holds, so only the owner can commit R.
            raise remote.RemoteError(
                f"refused: COMMIT names request {rid}, which this session does not own; "
                f"nothing was committed"
            )
        with self._routed():
            return _answer(connection, run())

    def _settle(self, connection: sqlite3.Connection | None) -> None:
        """End the session's hold once its transaction has ended, however it ended."""
        if self.kind is None or (connection is not None and connection.in_transaction):
            return
        if self.kind == "write":
            owners = self.server.owners  # type: ignore[attr-defined]
            with owners.lock(self.rid):
                owners.settle(self.rid)
        elif connection is not None:
            self._quiet(connection, f"PRAGMA query_only = {self.query_only}")
        self.kind, self.rid = None, None

    def _adopt(self, connection: sqlite3.Connection) -> None:
        """Hold a transaction the hub did not open as a read.

        Only a `SAVEPOINT` outside a transaction opens one: `_authorize`
        refuses every other way. The library reads a snapshot that way, and
        the RELEASE that ends it would commit any write without an id.
        """
        if connection.in_transaction and self.kind is None:
            self.query_only = int(self._quiet(connection, "PRAGMA query_only").fetchone()[0])
            self._quiet(connection, "PRAGMA query_only = ON")
            self.kind = "read"

    def _outcome(self, connection: sqlite3.Connection, rid) -> str:
        rid = _request_id(rid)
        if connection.in_transaction:
            raise remote.RemoteError("refused: ask for an outcome outside a transaction")
        owners = self.server.owners  # type: ignore[attr-defined]
        with owners.lock(rid):
            if owners.owner(rid) is not None:
                return remote.IN_FLIGHT
            if self._recorded(connection, rid):
                return remote.RECORDED
            # Final: a write frame still in transit is refused when it lands.
            owners.admit(rid, self.number)
            owners.settle(rid)
            return remote.ABSENT

    def handle(self) -> None:
        with Session.counter_guard:
            Session.counter += 1
            self.number = Session.counter
        server: Server = self.server  # type: ignore[assignment]
        sock: socket.socket = self.request
        connection = None
        image = b""
        traced = self.traced
        # Gap tracking for the write transaction open on this session.
        opened_at = last_answer = 0.0
        frames = 0
        gap = 0.0
        gap_before = ""
        first_sql = ""
        try:
            while True:
                # Silence inside an open transaction is bounded; outside one
                # it holds nothing a hub writer waits on.
                idle = connection is not None and connection.in_transaction
                sock.settimeout(server.idle_timeout if idle else None)
                try:
                    frame = remote.read_frame(sock, limit=OPEN_LIMIT if connection is None else remote.MAX_FRAME)
                except TimeoutError:
                    server.log.line(f"session {self.number} silent {server.idle_timeout:g} s inside a "
                                    f"transaction; closed and rolled back")
                    return
                except (EOFError, OSError):
                    return
                except remote.RemoteError as error:
                    server.log.line(f"session {self.number} dropped: {error}")
                    return
                sock.settimeout(None)
                received = time.monotonic()
                self.denied = None
                writing = connection is not None and first_sql != ""
                if writing:
                    frames += 1
                    if received - last_answer >= gap:
                        gap = received - last_answer
                        gap_before = _brief(frame)
                hang_up = False
                restart = False
                try:
                    op = frame.get("op")
                    opening = connection is None and op == "open"
                    if frame.get("v") != remote.PROTOCOL_VERSION:
                        if opening:
                            # The first frame of an older or newer client:
                            # name the side to upgrade (step 3).
                            raise remote.BuildMismatch("protocol", frame.get("v"), remote.PROTOCOL_VERSION,
                                                       hub_build=remote.build_digest())
                        raise remote.RemoteError(
                            f"protocol version {frame.get('v')!r} refused; this hub speaks "
                            f"{remote.PROTOCOL_VERSION}"
                        )
                    if server.node is not None:
                        # Every frame, open or not: a demoted hub writes nothing and serves no stale read.
                        fenced = server.fence()
                        if fenced is not None:
                            raise fenced_by(fenced)
                    if connection is None:
                        if op != "open":
                            raise remote.RemoteError(f"the first frame must be open, not {op!r}")
                        server.admit(self.client_address, frame, self.number)
                        # Step 3: the same build on both sides, or nothing
                        # opens. The file's schema is still checked against
                        # this library by `open_local` below; this checks the
                        # client's build against this library.
                        remote.check_handshake(frame)
                        target = Path(frame["path"]) if frame.get("path") else server.database
                        if frame.get("create"):
                            # init and migrate are hub verbs; a session never
                            # creates a database (gap (h)).
                            raise remote.RemoteError(
                                f"a remote session cannot create a database; run "
                                f"`bin/sd-db init` from the command pack on the hub ({target})"
                            )
                        connection = database.open_local(
                            target, write=bool(frame.get("write", True)),
                            create=bool(frame.get("create", False)),
                            busy_timeout=int(frame.get("busy_timeout", database.BUSY_TIMEOUT)),
                            cached_statements=0,
                        )
                        connection.set_authorizer(self._authorize)
                        answer = _answer(connection, None)
                    elif op == "execute":
                        sql = frame["sql"]
                        answer = self._statement(connection, frame)
                        if not writing and connection.in_transaction and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                            first_sql, frames, gap, gap_before, opened_at = sql.strip(), 0, 0.0, "", received
                    elif op == "executemany":
                        answer = _answer(connection, connection.executemany(
                            frame["sql"], [remote.decode_params(p) for p in frame["params"]]
                        ))
                    elif op == "executescript":
                        if self.kind is not None:
                            # `executescript` commits the open transaction
                            # before it runs: no row for a write, and a read
                            # left under `query_only`.
                            raise remote.RemoteError(
                                f"refused: executescript would commit the open {self.kind} "
                                f"transaction without its COMMIT; end it first"
                            )
                        # A script carries no R, like an autocommit write, and
                        # is the client's, so the hub does not route it: its
                        # BEGIN, COMMIT and ROLLBACK are refused. It must end
                        # what a SAVEPOINT opens: a transaction it left open
                        # would be one no frame holds.
                        try:
                            connection.executescript(frame["sql"])
                        finally:
                            left = connection.in_transaction
                            if left:
                                self._quiet(connection, "ROLLBACK")
                        if left:
                            raise remote.RemoteError("refused: the script left a transaction open; "
                                                     "it was rolled back, so nothing in it was written")
                        answer = _answer(connection, None)
                    elif op == "commit":
                        answer = self._commit(connection, frame.get("rid"), connection.commit)
                    elif op == "rollback":
                        with self._routed():
                            connection.rollback()
                        answer = _answer(connection, None)
                    elif op == "outcome":
                        value = self._outcome(connection, frame.get("rid"))
                        server.log.line(f"session {self.number} outcome of {frame.get('rid')}: {value}")
                        answer = {**_answer(connection, None), "value": value}
                    elif op == "trace":
                        connection.set_trace_callback(traced.append if frame.get("on") else None)
                        answer = _answer(connection, None)
                    elif op == "total_changes":
                        answer = {**_answer(connection, None),
                                  "value": connection.total_changes - self.hidden}
                    elif op == "serialize":
                        # The size now, the bytes in `chunk` answers, so no
                        # frame carries more than `remote.CHUNK` of them.
                        image = connection.serialize(name=frame.get("name", "main"))
                        answer = {**_answer(connection, None), "value": len(image)}
                    elif op == "chunk":
                        offset = int(frame["offset"])
                        length = max(1, min(int(frame.get("length", remote.CHUNK)), remote.CHUNK))
                        piece = image[offset:offset + length]
                        if offset + len(piece) >= len(image):
                            image = b""
                        answer = {**_answer(connection, None), "value": remote.encode_value(piece)}
                    elif op == "registry":
                        # Seam 7: the one file beside the database a remote
                        # session reads, staged like an image so `chunk`
                        # serves it. The satellite stage installs it (step 9).
                        from .errors import RegistryError
                        from .registry import REGISTRY_NAME

                        beside = target.parent / REGISTRY_NAME
                        try:
                            image = beside.read_bytes()
                        except FileNotFoundError:
                            raise RegistryError(f"no provider registry at {beside} on the hub") from None
                        answer = {**_answer(connection, None), "value": len(image)}
                    elif op == "iterdump":
                        answer = {**_answer(connection, None), "value": list(connection.iterdump())}
                    elif op == "close":
                        connection.close()
                        self._settle(None)
                        remote.send_frame(sock, {"v": remote.PROTOCOL_VERSION, "rid": frame.get("rid"), "ok": True, "in_transaction": False})
                        connection = None
                        return
                    else:
                        raise remote.RemoteError(f"unknown operation {op!r}")
                except Exception as error:  # every failure goes back as a frame  # noqa: F841 (ruff misreads the rebinding)
                    if self.denied is not None:
                        # SQLite says only "not authorized"; `_authorize` said why.
                        error = remote.StatementRefused(self.denied)
                        server.log.line(f"session {self.number} {error}")
                    answer = {"ok": False, "error": remote.describe_error(error)}
                    if connection is not None:
                        answer["in_transaction"] = connection.in_transaction
                    elif isinstance(error, (remote.BuildMismatch, remote.HubRestartNeeded, PeerRefused)):
                        server.log.line(f"session {self.number} refused before open: {error}")
                        hang_up = isinstance(error, PeerRefused)
                        restart = isinstance(error, remote.HubRestartNeeded)
                    if isinstance(error, Fenced):
                        # Closing the connection rolls back what the session held.
                        server.log.line(f"session {self.number} closed: this machine is no longer the hub: {error}")
                        hang_up = True
                if connection is not None:
                    # A ROLLBACK, or a transaction SQLite ended on an error.
                    self._settle(connection)
                    self._adopt(connection)
                answer["v"] = remote.PROTOCOL_VERSION
                answer["rid"] = frame.get("rid") if isinstance(frame, dict) else None
                if traced:
                    answer["trace"] = traced[:]
                    traced.clear()
                if first_sql and connection is not None and not connection.in_transaction:
                    server.log.transaction(self.number, frames, gap, time.monotonic() - opened_at, gap_before)
                    first_sql = ""
                # A peer that stops reading the answer is as silent as one
                # that stops sending: bounded inside a transaction (sd:8819).
                held = connection is not None and connection.in_transaction
                sock.settimeout(server.idle_timeout if held else None)
                try:
                    remote.send_frame(sock, answer)
                except remote.RemoteError as error:
                    # An answer too large for one frame goes back as an error,
                    # and the session stays open.
                    refused = {"v": remote.PROTOCOL_VERSION, "rid": answer.get("rid"), "ok": False,
                               "error": remote.describe_error(error)}
                    if connection is not None:
                        refused["in_transaction"] = connection.in_transaction
                    try:
                        remote.send_frame(sock, refused)
                    except OSError:
                        return
                except OSError:
                    return
                if hang_up:
                    # A refused peer gets one answer, not a second try.
                    return
                if restart and not server.restart_needed:
                    # This process refuses every session from here on, so it
                    # stops after the answer; launchd's KeepAlive starts the
                    # installed build (sd:3058).
                    server.restart_needed = True
                    server.log.line("stopping so launchd starts the installed build")
                    threading.Thread(target=server.shutdown, daemon=True).start()
                last_answer = time.monotonic()
        finally:
            if connection is not None:
                if first_sql:
                    server.log.line(f"session {self.number} closed inside a write transaction; rolled back")
                connection.close()
            # Closed, so rolled back: the id is no longer owned, and
            # `outcome` answers from the file from here on.
            self._settle(None)


class Server(socketserver.ThreadingTCPServer):
    """One listener. `node` is `None` on loopback, where the token admits a
    session; on the tailnet it is this node, and the peer admits it."""

    daemon_threads = True
    allow_reuse_address = True
    restart_needed = False

    def __init__(self, port: int, database_path: Path, log: Log, token: str, token_file: Path,
                 node: tailnet.Node | None = None, idle_timeout: float = IDLE_TIMEOUT) -> None:
        self.database = database_path
        self.log = log
        self.token = token
        self.token_file = token_file
        self.node = node
        self.idle_timeout = idle_timeout
        self.owners = Owners()
        #: The last fence answer, a `hub.Role`; see `fence`.
        self.fenced = hub.Held()
        super().__init__((LOOPBACK if node is None else str(node.address), port), Session)

    def fence(self, *, within: float | None = None) -> hub.Role | None:
        """The refusing role when this machine no longer holds the hub role, or `None`: asked
        again past `within` seconds, `FENCE_SECONDS` by default, or once the wall clock jumped
        (a sleep). Unknown refuses. A frame with a held answer waits on no ask (sd:7042)."""
        found = self.fenced.get(lambda: hub.role_at(self.database), FENCE_SECONDS if within is None else within)
        return found if found.refuses else None

    def admit(self, peer, frame: dict, session: int = 0) -> None:
        """Refuse a session before anything opens, and before any SQL runs."""
        if self.node is None:
            # Loopback is not a user boundary: another local account reaches
            # 127.0.0.1 too. The token file is owner-only, so a client that
            # read it is the owner.
            if not self.token or not hmac.compare_digest(str(frame.get("token") or ""), self.token):
                raise remote.RemoteError(
                    f"refused: the open frame lacks this server's token; read it from "
                    f"{self.token_file}"
                )
            return
        refusal = self.refusal(peer, frame)
        shown = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) and len(peer) == 2 else repr(peer)
        if refusal is not None:
            raise PeerRefused(f"refused the peer {shown}: {refusal}; this hub admits only "
                              f"{self.node.login}'s untagged nodes")
        self.log.line(f"session {session} admitted {self.node.login} from {shown}")

    def refusal(self, peer, frame: dict) -> str | None:
        """Why the tailnet listener refuses this peer, or `None`. Order matters:
        the hub's own address is refused before `whois`, which would name the
        operator for any account on this machine."""
        found = tailnet.peer_address(peer)
        if found is None:
            return "not a Tailscale IPv4 address"
        if found[0] in self.node.addresses:
            return ("it is this hub's own Tailscale address, which any account on the hub can dial; "
                    "a process on the hub opens the database directly")
        try:
            identity = tailnet.whois(peer)
        except tailnet.TailnetError as error:
            return str(error)
        if identity.refusal is not None:
            return identity.refusal
        if identity.login != self.node.login:
            return f"the login {identity.login} is not this hub's operator"
        if frame.get("path") is not None:
            return "a tailnet session opens the hub's own database, and names no path"
        return None


def _write_token(path: Path) -> str:
    """A fresh token in a file only this account can read (mode 0600)."""
    token = secrets.token_urlsafe(32)
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token + "\n")
    return token


def read_token(path: Path) -> str:
    """The token a client sends, from the server's token file."""
    return Path(path).read_text().strip()


def serve(port: int, database_path: Path, stream=sys.stderr, *, loopback: bool = True) -> int:
    log = Log(stream)
    # Resolved, so the log names the file itself and not a link to it (gap (h)).
    target = database_path.expanduser().resolve()
    if not target.exists():
        # Never create: a server under the wrong home must fail closed rather
        # than serve a second, empty database (gap (h)).
        log.line(f"no database at {target}; run `bin/sd-db init` from the command pack")
        return 1
    node = None
    if not loopback:
        try:
            node = tailnet.this_node()
        except tailnet.TailnetError as error:
            log.line(f"cannot serve on the tailnet: {error}")
            return 1
        # The fence (hub failover, D5): a returning old hub serves its stale database to nobody.
        found = hub.role_at(target)
        if found.refuses:
            log.line(f"cannot serve on the tailnet: this machine is not the hub: {found.reason}")
            return 1
    resolved = target.resolve()
    guard = resolved.with_name(resolved.name + LOCK_SUFFIX)
    token_file = resolved.with_name(resolved.name + TOKEN_SUFFIX)
    try:
        with lock(guard, blocking=False, noun="serve",
                  held=f"another server owns this database: {guard}"):
            # Bind before the token exists, so a refused port leaves no token.
            address = LOOPBACK if node is None else str(node.address)
            try:
                server = Server(port, target, log, "", token_file, node)
            except OSError as error:
                log.line(f"cannot listen on {address}:{port}: {error.strerror or error}")
                return 1
            try:
                # The build this process loaded, taken before any session
                # can ask for it (sd:1480).
                remote.build_digest()
                host, bound = server.server_address[:2]
                if node is None:
                    server.token = _write_token(token_file)
                    log.line(f"serving {target} on {host}:{bound}; token in {token_file}")
                else:
                    # No token on the tailnet (R7): the peer is the credential.
                    own = ", ".join(sorted(str(value) for value in node.addresses))
                    log.line(f"serving {target} on {host}:{bound} to {node.login}'s untagged nodes; "
                             f"refusing this node's own addresses ({own})")

                def stop(signum, _frame):
                    threading.Thread(target=server.shutdown, daemon=True).start()

                signal.signal(signal.SIGTERM, stop)
                signal.signal(signal.SIGINT, stop)
                server.serve_forever(poll_interval=0.2)
            finally:
                server.server_close()
                if node is None:
                    token_file.unlink(missing_ok=True)
                log.line(log.summary())
    except RunnerRefused as refused:
        log.line(str(refused))
        return 1
    # Non-zero: nobody asked this stop, the install did.
    return 1 if server.restart_needed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bin/sd-db serve")
    parser.add_argument("--loopback", action="store_true",
                        help="bind 127.0.0.1 and admit by the owner-only token; without it, bind "
                             "this node's Tailscale IPv4 address and admit by `tailscale whois`")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--database", type=Path, default=None)
    arguments = parser.parse_args(argv)
    # The file on this machine, never the hub-aware default: a server does
    # not serve what it would reach through `hub.json`.
    return serve(arguments.port, arguments.database or database.local_path(), loopback=arguments.loopback)


if __name__ == "__main__":
    raise SystemExit(main())
