"""Step 5 of the second-machine plan: a lost COMMIT is an unknown outcome.

`docs/work/2026-09-22-run-the-framework-from-a-second-machine/implement.md`,
criterion 11 cases (a) to (e) and (j), built without the prune (the
operator's ruling of 2026-10-04, Q1 = B), and the read transactions of gap
C2. Each case counts rows before and after on the hub's file: the count
moves by exactly one where a commit is promised and by zero everywhere else.

The faults sit on the wire, in `Relay`, or in the hub's own process: a
server in this process whose answer, frame or session a test controls, or a
child server killed with SIGKILL at `serve._fault`.
"""

from __future__ import annotations

import io
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sd_db import (
    create_item,
    database,
    errors,
    initialise,
    reads,
    registry,
    remote,
    serve,
    usage,
)
from sd_db.database import transaction
from sd_db.serve import read_token

from . import support
from .test_registry import SHIPPED

HERE = support.LIB
#: Long enough for a loaded machine to restart a child server inside it.
RESTART_BOUND = 60.0


def count(path: Path, table: str = "probe") -> int:
    """Rows on the hub's file, read beside the server, not through it."""
    raw = sqlite3.connect(path)
    try:
        return raw.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        raw.close()


def recorded(path: Path, rid: str) -> bool:
    raw = sqlite3.connect(path)
    try:
        return raw.execute("SELECT 1 FROM request_outcome WHERE id = ?", (rid,)).fetchone() is not None
    finally:
        raw.close()


def fresh_database(root: Path) -> Path:
    path = root / "sd.db"
    initialise(path)
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    raw.commit()
    raw.close()
    return path


class InProcess:
    """`serve.Server` on 127.0.0.1 in a thread of this process."""

    def __init__(self, path: Path, *, idle_timeout: float = serve.IDLE_TIMEOUT) -> None:
        self.stream = io.StringIO()
        self.token = secrets.token_urlsafe(16)
        self.server = serve.Server(0, path, serve.Log(self.stream), self.token,
                                   path.with_name("unused.token"), idle_timeout=idle_timeout)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


class Relay:
    """A frame-aware relay between clients and a server, which spoils one COMMIT.

    `fault` decides what happens to the first `COMMIT` frame that carries a
    request id: `drop-answer` forwards it and loses the hub's answer,
    `drop-frame` loses the frame and closes both sides, and `hold` keeps the
    frame (and the hub's session) until `release`. The client's side closes
    in every case, as a broken link would. Every other frame, and every
    later session, passes through, unless `refusing` is set: then a new
    session is closed as soon as it arrives. `refuse_after` sets it when
    the fault fires: a hub that cannot be asked. `match` spoils the first
    frame it accepts instead of the first COMMIT.
    """

    def __init__(self, port: int, fault: str, *, refuse_after: bool = False, match=None) -> None:
        self.target = port
        self.fault = fault
        self.match = match
        self.armed = True
        self.refusing = False
        self.refuse_after = refuse_after
        self.held: tuple[dict, socket.socket] | None = None
        self.fired = threading.Event()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.sockets: list[socket.socket] = [self.listener]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            if self.refusing:
                client.close()
                continue
            self.sockets.append(client)
            threading.Thread(target=self._pump, args=(client,), daemon=True).start()

    def _spoils(self, frame: dict) -> bool:
        if self.match is not None:
            return self.armed and self.match(frame)
        committing = (frame.get("op") == "commit"
                      or (frame.get("op") == "execute" and remote.statement(frame.get("sql", "")) == "commit"))
        return self.armed and committing and frame.get("rid") is not None

    def _pump(self, client: socket.socket) -> None:
        hub = socket.create_connection(("127.0.0.1", self.target))
        self.sockets.append(hub)
        try:
            while True:
                frame = remote.read_frame(client)
                if self._spoils(frame):
                    self.armed = False
                    self.refusing = self.refuse_after
                    if self.fault == "drop-answer":
                        remote.send_frame(hub, frame)
                        remote.read_frame(hub)
                        hub.close()
                    elif self.fault == "drop-frame":
                        hub.close()
                    else:
                        self.held = (frame, hub)
                    client.close()
                    self.fired.set()
                    return
                remote.send_frame(hub, frame)
                remote.send_frame(client, remote.read_frame(hub))
        except (OSError, EOFError):
            client.close()
            if self.held is None or self.held[1] is not hub:
                hub.close()

    def release(self) -> dict:
        """Deliver the held `COMMIT`; the hub's answer goes nowhere."""
        frame, hub = self.held
        remote.send_frame(hub, frame)
        answer = remote.read_frame(hub)
        hub.close()
        return answer

    def stop(self) -> None:
        for each in self.sockets:
            try:
                each.close()
            except OSError:
                pass


class OutcomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = fresh_database(self.root)
        self.hub = InProcess(self.path)
        self.addCleanup(self.hub.stop)

    def connect(self, port: int | None = None, **options) -> remote.Connection:
        options.setdefault("token", self.hub.token)
        connection = remote.connect("127.0.0.1", port or self.hub.port, self.path, **options)
        self.addCleanup(connection.close)
        return connection

    def relay(self, fault: str, **options) -> Relay:
        relay = Relay(self.hub.port, fault, **options)
        self.addCleanup(relay.stop)
        return relay

    def raw(self, *, write: bool = True) -> socket.socket:
        """A session that sends frames as written, with no client logic."""
        sock = socket.create_connection(("127.0.0.1", self.hub.port))
        self.addCleanup(sock.close)
        answer = self.ask(sock, "open", path=str(self.path), write=write, create=False,
                          busy_timeout=5000, token=self.hub.token, **remote.handshake())
        self.assertTrue(answer["ok"], answer)
        return sock

    @staticmethod
    def ask(sock: socket.socket, op: str, *, rid: str | None = None, **fields) -> dict:
        remote.send_frame(sock, remote.request(op, rid=rid, **fields))
        return remote.read_frame(sock)

    def execute(self, sock: socket.socket, sql: str, *, rid: str | None = None) -> dict:
        return self.ask(sock, "execute", rid=rid, sql=sql, params=[])


class TheLostCommit(OutcomeCase):
    """Criterion 11, cases (a), (d), (e) and (j), against a server in this process."""

    def test_a_dropped_answer_is_unknown_then_recorded(self):
        """(a) The hub committed and its answer was lost: `UnknownOutcome`
        while the hub cannot be asked, then `recorded`, one row."""
        relay = self.relay("drop-answer", refuse_after=True)
        client = self.connect(relay.port)
        with mock.patch.object(remote, "OUTCOME_BOUND", 0.5), \
                self.assertRaises(remote.UnknownOutcome) as raised:
            with transaction(client):
                client.execute("INSERT INTO probe (name) VALUES ('a')")
        rid = raised.exception.rid
        self.assertIn(rid, str(raised.exception))
        self.assertTrue(remote.REQUEST_ID.fullmatch(rid))
        self.assertEqual({answer for _, answer in client.outcomes}, {"unreachable"})
        relay.refusing = False
        self.assertEqual(client.outcome(rid), remote.RECORDED)
        self.assertEqual(count(self.path), 1)
        self.assertTrue(recorded(self.path, rid))

    def test_a_dropped_answer_settles_as_the_commit_it_was(self):
        """(a) With the hub reachable, the client asks at once: `recorded`,
        and the block returns as a commit does. One row."""
        relay = self.relay("drop-answer")
        client = self.connect(relay.port)
        with transaction(client):
            client.execute("INSERT INTO probe (name) VALUES ('a')")
        self.assertTrue(relay.fired.is_set())
        [(rid, answer)] = client.outcomes
        self.assertEqual(answer, remote.RECORDED)
        self.assertFalse(client.in_transaction)
        self.assertEqual(count(self.path), 1)
        self.assertTrue(recorded(self.path, rid))

    def test_a_commit_frame_held_in_transit_is_in_flight_until_it_lands(self):
        """(d) The COMMIT frame is delayed and the client asks: `in_flight`,
        nothing committed; the frame lands: `recorded`, one row."""
        relay = self.relay("hold")
        client = self.connect(relay.port)
        failures: list[BaseException] = []

        def body():
            try:
                with transaction(client):
                    client.execute("INSERT INTO probe (name) VALUES ('d')")
            except BaseException as error:  # reported by the main thread
                failures.append(error)

        worker = threading.Thread(target=body)
        worker.start()
        deadline = time.monotonic() + 30
        while not any(answer == remote.IN_FLIGHT for _, answer in client.outcomes):
            self.assertLess(time.monotonic(), deadline, client.outcomes)
            self.assertTrue(worker.is_alive(), failures)
            time.sleep(0.01)
        self.assertEqual(count(self.path), 0)
        answer = relay.release()
        worker.join(30)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(answer["ok"], answer)
        answers = [answer for _, answer in client.outcomes]
        self.assertEqual((answers[0], answers[-1]), (remote.IN_FLIGHT, remote.RECORDED))
        self.assertEqual(count(self.path), 1)
        self.assertTrue(recorded(self.path, client.outcomes[-1][0]))

    def test_a_hub_no_ask_from_this_build_can_pass_ends_the_settle_at_once(self):
        """sd:3359. A refused build, or a database newer than the hub's: 60 s of asks could not pass.

        The process keeps the build it loaded, so the first such refusal ends the settle,
        and the outcome stays unknown: the write is not run again.
        """
        client = self.connect()
        lost = remote.HubUnreachable("127.0.0.1", self.hub.port, "[Errno 32] Broken pipe")
        for refusal in (remote.BuildMismatch("build", "0000aaaa", "1111bbbb"), errors.SchemaTooNew(99, 1)):
            with self.subTest(refusal=type(refusal).__name__), \
                    mock.patch.object(client, "outcome", side_effect=refusal):
                started = time.monotonic()
                with self.assertRaises(remote.UnknownOutcome) as raised:
                    client._settle(remote.new_request_id(), lost, "COMMIT")
                self.assertLess(time.monotonic() - started, remote.OUTCOME_BOUND / 4)
                self.assertIs(raised.exception.__cause__, refusal)
                self.assertIn(str(refusal), str(raised.exception))

    def test_an_owner_live_at_the_bound_is_an_unknown_outcome(self):
        """(d) at the bound: still `in_flight`, so `UnknownOutcome` names R
        and says a session owns it; nothing committed yet."""
        relay = self.relay("hold")
        client = self.connect(relay.port)
        with mock.patch.object(remote, "OUTCOME_BOUND", 0.3), \
                self.assertRaisesRegex(remote.UnknownOutcome, "still owns") as raised:
            with transaction(client):
                client.execute("INSERT INTO probe (name) VALUES ('d')")
        self.assertIn(raised.exception.rid, str(raised.exception))
        self.assertEqual(count(self.path), 0)
        relay.release()
        self.assertEqual(client.outcome(raised.exception.rid), remote.RECORDED)
        self.assertEqual(count(self.path), 1)

    def test_an_ask_the_hub_never_answers_ends_at_its_own_timeout(self):
        """A hub that accepts and never answers must not hold the client past
        the bound: each ask has its own timeout."""
        silent = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(silent.close)
        client = self.connect()
        client.port = silent.getsockname()[1]
        started = time.monotonic()
        with mock.patch.object(remote, "OUTCOME_ASK_TIMEOUT", 0.3), \
                self.assertRaises(remote.HubUnreachable):
            client.outcome(remote.new_request_id())
        self.assertLess(time.monotonic() - started, 5)

    def test_a_commit_for_an_id_this_session_does_not_own_is_refused(self):
        """(e) Only R's owner commits R: another session's COMMIT naming R is
        refused and not applied, with or without a transaction of its own."""
        owner = self.connect()
        owner.execute("BEGIN IMMEDIATE")
        owner.execute("INSERT INTO probe (name) VALUES ('e')")
        rid = owner._rid
        self.assertTrue(remote.REQUEST_ID.fullmatch(rid))
        other = self.raw()
        refused = self.execute(other, "COMMIT", rid=rid)
        self.assertFalse(refused["ok"])
        self.assertIn(f"COMMIT names request {rid}, which this session does not own", refused["error"]["text"])
        self.assertTrue(self.execute(other, "BEGIN")["ok"])
        refused = self.ask(other, "commit", rid=rid)
        self.assertFalse(refused["ok"])
        self.assertIn("does not own", refused["error"]["text"])
        self.assertTrue(refused["in_transaction"])
        self.assertTrue(self.execute(other, "ROLLBACK")["ok"])
        self.assertEqual(count(self.path), 0)
        self.assertEqual(owner.outcome(rid), remote.IN_FLIGHT)
        owner.execute("ROLLBACK")
        self.assertEqual(owner.outcome(rid), remote.ABSENT)
        self.assertEqual(count(self.path), 0)
        self.assertFalse(recorded(self.path, rid))

    def test_a_commit_naming_another_id_commits_and_records_nothing(self):
        """(e) A session inside its own write transaction cannot commit, or
        record, an id it does not own: not one that rolled back, not a new one."""
        sock = self.raw()
        rolled_back, mine = remote.new_request_id(), remote.new_request_id()
        self.assertTrue(self.execute(sock, "BEGIN IMMEDIATE", rid=rolled_back)["ok"])
        self.assertTrue(self.execute(sock, "ROLLBACK")["ok"])
        self.assertTrue(self.execute(sock, "BEGIN IMMEDIATE", rid=mine)["ok"])
        self.assertTrue(self.execute(sock, "INSERT INTO probe (name) VALUES ('e')")["ok"])
        for other in (rolled_back, remote.new_request_id()):
            with self.subTest(other=other):
                refused = self.execute(sock, "COMMIT", rid=other)
                self.assertFalse(refused["ok"])
                self.assertIn(f"COMMIT names request {other}, which this session does not own",
                              refused["error"]["text"])
                self.assertTrue(refused["in_transaction"])
        self.assertEqual(count(self.path), 0)
        asker = self.connect()
        self.assertEqual(asker.outcome(rolled_back), remote.ABSENT)
        self.assertTrue(self.execute(sock, "COMMIT", rid=mine)["ok"])
        self.assertEqual(count(self.path), 1)
        self.assertEqual((asker.outcome(rolled_back), asker.outcome(mine)), (remote.ABSENT, remote.RECORDED))

    def test_absent_leaves_the_block_and_the_verb_runs_again_under_a_new_id(self):
        """(j) The COMMIT never reached the hub, whose session closed: `absent`,
        so `TransactionLost(R)` leaves the block with nothing written. The
        verb run again writes one item, and its opening note is that item's."""
        relay = self.relay("drop-frame")
        first = self.connect(relay.port)
        with self.assertRaises(remote.TransactionLost) as raised:
            create_item(first, kind="work", title="lost")
        lost = raised.exception.rid
        self.assertIn(lost, str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, remote.UnknownOutcome)
        self.assertEqual(first.outcomes, [(lost, remote.ABSENT)])
        self.assertEqual((count(self.path, "item"), count(self.path, "note")), (0, 0))
        again = self.connect()
        item = create_item(again, kind="work", title="run again")
        self.assertEqual((count(self.path, "item"), count(self.path, "note")), (1, 1))
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("SELECT item FROM note").fetchone()[0], item)
        self.assertEqual(raw.execute("SELECT title FROM item WHERE id = ?", (item,)).fetchone()[0], "run again")
        [(rid,)] = raw.execute("SELECT id FROM request_outcome").fetchall()
        self.assertNotEqual(rid, lost)


def inserting(frame: dict) -> bool:
    return frame.get("op") == "execute" and frame.get("sql", "").startswith("INSERT")


class TheAutocommitWrite(OutcomeCase):
    """sd:2671: a write outside a transaction is settled as a COMMIT is."""

    def test_a_dropped_answer_settles_as_the_write_it_was(self):
        """The hub wrote and its answer was lost: `recorded`, one row, and
        the caller does not run the write again."""
        relay = self.relay("drop-answer", match=inserting)
        client = self.connect(relay.port)
        client.execute("INSERT INTO probe (name) VALUES ('a')")
        self.assertTrue(relay.fired.is_set())
        [(rid, answer)] = client.outcomes
        self.assertEqual(answer, remote.RECORDED)
        self.assertEqual(count(self.path), 1)
        self.assertTrue(recorded(self.path, rid))

    def test_a_dropped_frame_is_absent_and_the_write_runs_again(self):
        relay = self.relay("drop-frame", match=inserting)
        client = self.connect(relay.port)
        with self.assertRaises(remote.TransactionLost) as raised:
            client.execute("INSERT INTO probe (name) VALUES ('a')")
        self.assertEqual(client.outcomes, [(raised.exception.rid, remote.ABSENT)])
        self.assertEqual(count(self.path), 0)
        self.connect().execute("INSERT INTO probe (name) VALUES ('a')")
        self.assertEqual(count(self.path), 1)

    def test_a_frame_that_lands_after_absent_is_refused(self):
        """`absent` is final: the write frame held in transit lands after the
        hub said so, and the hub refuses it, so the write run again is the
        only one."""
        relay = self.relay("hold", match=inserting)
        client = self.connect(relay.port)
        with self.assertRaises(remote.TransactionLost) as raised:
            client.execute("INSERT INTO probe (name) VALUES ('a')")
        late = relay.release()
        self.assertFalse(late["ok"])
        self.assertIn(f"request id {raised.exception.rid} was used before", late["error"]["text"])
        self.assertEqual(count(self.path), 0)

    def test_the_write_answers_as_a_local_one_and_records_its_row(self):
        client = self.connect()
        before = client.total_changes
        cursor = client.execute("INSERT INTO probe (name) VALUES ('a') RETURNING id")
        self.assertEqual(([tuple(row) for row in cursor], cursor.lastrowid), ([(1,)], 1))
        self.assertFalse(client.in_transaction)
        self.assertEqual((count(self.path, "request_outcome"), client.total_changes), (1, before + 1))
        client.execute("UPDATE probe SET name = 'none' WHERE name = 'missing'")
        client.execute("SELECT * FROM probe")
        self.assertEqual(count(self.path, "request_outcome"), 1)

    def test_the_write_waits_for_a_hub_writer_as_an_autocommit_one_does(self):
        """No read before the statement: a stale snapshot would fail the
        write with SQLITE_BUSY_SNAPSHOT once the hub writer commits."""
        client = self.connect()
        local = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(local.close)
        local.execute("BEGIN IMMEDIATE")
        local.execute("INSERT INTO probe (name) VALUES ('hub')")
        failures: list[BaseException] = []

        def write():
            try:
                client.execute("INSERT INTO probe (name) VALUES ('wire')")
            except BaseException as error:  # reported by the main thread
                failures.append(error)

        writer = threading.Thread(target=write)
        writer.start()
        time.sleep(0.3)
        local.execute("COMMIT")
        writer.join(10)
        self.assertEqual(failures, [])
        self.assertEqual(count(self.path), 2)

    def test_a_failed_write_writes_nothing_and_leaves_no_transaction(self):
        client = self.connect()
        with self.assertRaises(sqlite3.IntegrityError):
            client.execute("INSERT INTO probe (name) VALUES (NULL)")
        self.assertFalse(client.in_transaction)
        self.assertEqual((count(self.path), count(self.path, "request_outcome")), (0, 0))
        client.execute("INSERT INTO probe (name) VALUES ('b')")
        self.assertEqual(count(self.path), 1)


class TheOwnership(OutcomeCase):
    """The rules behind the answers: ids, settlement and the idle timeout."""

    def test_a_write_transaction_without_an_id_is_refused(self):
        sock = self.raw()
        refused = self.execute(sock, "BEGIN IMMEDIATE")
        self.assertFalse(refused["ok"])
        self.assertIn("needs a request id", refused["error"]["text"])
        self.assertFalse(refused["in_transaction"])

    def test_an_id_is_used_once(self):
        rid = remote.new_request_id()
        sock = self.raw()
        self.assertTrue(self.execute(sock, "BEGIN IMMEDIATE", rid=rid)["ok"])
        self.assertTrue(self.execute(sock, "ROLLBACK")["ok"])
        again = self.execute(sock, "BEGIN IMMEDIATE", rid=rid)
        self.assertFalse(again["ok"])
        self.assertIn("was used before", again["error"]["text"])

    def test_a_recorded_id_is_refused_by_a_restarted_hub(self):
        client = self.connect()
        with transaction(client):
            client.execute("INSERT INTO probe (name) VALUES ('once')")
        raw = sqlite3.connect(self.path)
        [(rid,)] = raw.execute("SELECT id FROM request_outcome").fetchall()
        raw.close()
        client.close()
        self.hub.stop()
        self.hub = InProcess(self.path)
        self.addCleanup(self.hub.stop)
        refused = self.execute(self.raw(), "BEGIN IMMEDIATE", rid=rid)
        self.assertFalse(refused["ok"])
        self.assertIn("has committed before", refused["error"]["text"])
        self.assertEqual(count(self.path), 1)

    def test_a_closed_session_settles_its_id(self):
        sock = self.raw()
        rid = remote.new_request_id()
        self.assertTrue(self.execute(sock, "BEGIN IMMEDIATE", rid=rid)["ok"])
        self.assertTrue(self.execute(sock, "INSERT INTO probe (name) VALUES ('closed')")["ok"])
        asker = self.connect()
        self.assertEqual(asker.outcome(rid), remote.IN_FLIGHT)
        sock.close()
        deadline = time.monotonic() + 10
        while asker.outcome(rid) == remote.IN_FLIGHT:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertEqual(asker.outcome(rid), remote.ABSENT)
        self.assertEqual(count(self.path), 0)

    def test_a_silent_transaction_is_closed_before_a_hub_writer_gives_up(self):
        """Gap (g): silent inside BEGIN IMMEDIATE past the idle timeout, the
        session is rolled back, a hub writer gets the lock inside its wait,
        and the late COMMIT settles as `absent`."""
        self.assertLess(serve.IDLE_TIMEOUT * 1000, database.BUSY_TIMEOUT)
        self.assertGreater(remote.OUTCOME_BOUND, serve.IDLE_TIMEOUT)
        self.hub.stop()
        self.hub = InProcess(self.path, idle_timeout=0.3)
        self.addCleanup(self.hub.stop)
        client = self.connect()
        with self.assertRaises(remote.TransactionLost):
            with transaction(client):
                client.execute("INSERT INTO probe (name) VALUES ('silent')")
                local = sqlite3.connect(self.path, timeout=5, isolation_level=None)
                try:
                    started = time.monotonic()
                    local.execute("BEGIN IMMEDIATE")
                    waited = time.monotonic() - started
                    local.execute("INSERT INTO probe (name) VALUES ('hub')")
                    local.execute("COMMIT")
                finally:
                    local.close()
        self.assertLess(waited, 5)
        self.assertIn("silent 0.3 s inside a transaction; closed and rolled back", self.hub.stream.getvalue())
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        self.assertEqual([row[0] for row in raw.execute("SELECT name FROM probe")], ["hub"])

    def test_a_reply_the_peer_stops_reading_is_rolled_back_in_the_idle_timeout(self):
        """sd:8819: the reply send inside a write transaction is bounded too:
        a peer that stops reading a large answer loses the transaction, and
        a hub writer gets the lock inside its own wait."""
        self.hub.stop()
        self.hub = InProcess(self.path, idle_timeout=0.3)
        self.addCleanup(self.hub.stop)
        sock = self.raw()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.assertTrue(self.execute(sock, "BEGIN IMMEDIATE", rid=remote.new_request_id())["ok"])
        self.assertTrue(self.execute(sock, "INSERT INTO probe (name) VALUES ('held')")["ok"])
        remote.send_frame(sock, remote.request("execute", sql="SELECT zeroblob(64000000)", params=[]))
        local = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.addCleanup(local.close)
        local.execute("BEGIN IMMEDIATE")
        local.execute("INSERT INTO probe (name) VALUES ('hub')")
        local.execute("COMMIT")
        self.assertEqual([row[0] for row in local.execute("SELECT name FROM probe")], ["hub"])

    def test_executescript_inside_a_write_transaction_is_refused(self):
        client = self.connect()
        client.execute("BEGIN IMMEDIATE")
        client.execute("INSERT INTO probe (name) VALUES ('script')")
        with self.assertRaisesRegex(remote.RemoteError, "executescript would commit the open write"):
            client.executescript("INSERT INTO probe (name) VALUES ('more');")
        self.assertTrue(client.in_transaction)
        client.execute("ROLLBACK")
        self.assertEqual(count(self.path), 0)

    def test_a_local_connection_writes_no_outcome_row(self):
        local = database.open_local(self.path, write=True, create=False)
        self.addCleanup(local.close)
        with transaction(local):
            local.execute("INSERT INTO probe (name) VALUES ('local')")
        self.assertEqual((count(self.path), count(self.path, "request_outcome")), (1, 0))

    def test_a_write_transaction_that_changes_nothing_adds_no_row(self):
        # A replay that writes nothing must write nothing over the wire too:
        # no row, and `total_changes` as a local connection counts it.
        client = self.connect()
        before = client.total_changes
        with transaction(client):
            client.execute("UPDATE probe SET name = 'none' WHERE name = 'missing'")
        self.assertEqual((count(self.path, "request_outcome"), client.total_changes), (0, before))
        with transaction(client):
            client.execute("INSERT INTO probe (name) VALUES ('one')")
        self.assertEqual((count(self.path, "request_outcome"), client.total_changes), (1, before + 1))

    def test_a_write_that_moves_no_row_is_still_recorded(self):
        # DDL changes no row; it is a write all the same. Setting
        # `user_version` is refused over the wire (step 7), so DROP stands in.
        client = self.connect()
        with transaction(client):
            client.execute("CREATE TABLE extra (x)")
        with transaction(client):
            client.execute("DROP TABLE extra")
        self.assertEqual(count(self.path, "request_outcome"), 2)

    def test_request_ids_are_ulids_that_order_by_time(self):
        early, late = remote.new_request_id(1_700_000_000.0), remote.new_request_id(1_800_000_000.0)
        self.assertTrue(remote.REQUEST_ID.fullmatch(early) and remote.REQUEST_ID.fullmatch(late))
        self.assertLess(early[:10], late[:10])
        self.assertEqual(len({remote.new_request_id(1_700_000_000.0) for _ in range(1000)}), 1000)


class TheReadTransaction(OutcomeCase):
    """Gap C2: a plain BEGIN is a read transaction, with no id and no row."""

    MONTH, NOW = "2026-10", "2026-10-04T12:00:00+00:00"

    def answers(self, connection) -> tuple:
        return (
            usage.read(connection, month=self.MONTH, now=self.NOW),
            reads.usage_month(connection, month=self.MONTH, now=self.NOW),
            registry.merge(registry.parse(SHIPPED), connection).providers,
        )

    def test_the_three_snapshot_helpers_answer_as_a_local_connection(self):
        local = database.open_local(self.path, write=True, create=False)
        self.addCleanup(local.close)
        registry.ensure_seeded(local, registry.parse(SHIPPED))
        local.execute("INSERT INTO cost (call_id, timestamp, provider, bill, role, usd, source) "
                      "VALUES ('c1', '2026-10-02T00:00:00+00:00', 'kimi', 'moonshot', 'reviewer', 0.5, 'bound')")
        expected = self.answers(local)
        for write in (False, True):
            with self.subTest(write=write):
                wire = self.connect(write=write)
                self.assertEqual(self.answers(wire), expected)
                self.assertFalse(wire.in_transaction)
                # The read path leaves the connection as it found it.
                self.assertEqual(wire.execute("PRAGMA query_only").fetchone()[0], 0 if write else 1)
        self.assertEqual(count(self.path, "request_outcome"), 0)

    def test_a_write_inside_a_plain_begin_fails(self):
        wire = self.connect()
        wire.execute("BEGIN")
        with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            wire.execute("INSERT INTO probe (name) VALUES ('read')")
        wire.execute("COMMIT")
        wire.execute("INSERT INTO probe (name) VALUES ('after')")
        # The one row is the autocommit write's own (sd:2671), not the read's.
        self.assertEqual((count(self.path), count(self.path, "request_outcome")), (1, 1))

    def test_commit_without_the_id_ends_no_write_transaction(self):
        sock = self.raw()
        rid = remote.new_request_id()
        self.assertTrue(self.execute(sock, "BEGIN IMMEDIATE", rid=rid)["ok"])
        self.assertTrue(self.execute(sock, "INSERT INTO probe (name) VALUES ('w')")["ok"])
        for op in ("execute", "commit"):
            with self.subTest(op=op):
                refused = (self.execute(sock, "COMMIT") if op == "execute" else self.ask(sock, "commit"))
                self.assertFalse(refused["ok"])
                self.assertIn(f"COMMIT of write transaction {rid} carries no request id", refused["error"]["text"])
                self.assertTrue(refused["in_transaction"])
        self.assertEqual(count(self.path), 0)
        self.assertTrue(self.execute(sock, "COMMIT", rid=rid)["ok"])
        self.assertEqual(count(self.path), 1)

    def test_commit_with_an_id_ends_no_read_transaction(self):
        sock = self.raw(write=False)
        self.assertTrue(self.execute(sock, "BEGIN")["ok"])
        refused = self.execute(sock, "COMMIT", rid=remote.new_request_id())
        self.assertFalse(refused["ok"])
        self.assertIn("does not own", refused["error"]["text"])
        self.assertTrue(self.execute(sock, "COMMIT")["ok"])
        refused = self.execute(sock, "BEGIN", rid=remote.new_request_id())
        self.assertFalse(refused["ok"])
        self.assertIn("carries no request id", refused["error"]["text"])
        self.assertEqual(count(self.path, "request_outcome"), 0)


class TheTransactionBoundary(OutcomeCase):
    """The review of 2026-10-04: SQLite, not the statement's text, decides
    what ends a transaction. Each case ends with the writes and the outcome
    row both on the hub, or neither."""

    def deferred(self) -> None:
        """`child.parent` is checked at COMMIT, not at the insert."""
        raw = sqlite3.connect(self.path)
        raw.executescript(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent INTEGER NOT NULL"
            " REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED);"
        )
        raw.close()

    @staticmethod
    def commit(client: remote.Connection, op: str) -> None:
        if op == "execute":
            client.execute("COMMIT")
        else:
            client.commit()

    def test_a_commit_the_classifier_misses_is_refused_and_commits_nothing(self):
        for sql in ("COMMIT; -- done", "COMMIT /* done */", "-- done\nEND"):
            with self.subTest(sql=sql):
                client = self.connect()
                client.execute("BEGIN IMMEDIATE")
                rid = client._rid
                client.execute("INSERT INTO probe (name) VALUES ('w')")
                with self.assertRaisesRegex(remote.RemoteError, "outside the hub's transaction handling"):
                    client.execute(sql)
                self.assertTrue(client.in_transaction)
                self.assertEqual((count(self.path), recorded(self.path, rid)), (0, False))
                client.execute("COMMIT")
                self.assertEqual((count(self.path), recorded(self.path, rid)), (1, True))
                client.execute("DELETE FROM probe")

    def test_a_begin_the_classifier_misses_opens_nothing(self):
        # A commented BEGIN IMMEDIATE would own no id; a commented BEGIN
        # would escape `query_only`.
        for sql in ("/* x */ BEGIN IMMEDIATE", "BEGIN IMMEDIATE; -- x", "-- x\nBEGIN"):
            with self.subTest(sql=sql):
                client = self.connect()
                with self.assertRaisesRegex(remote.RemoteError, "outside the hub's transaction handling"):
                    client.execute(sql)
                self.assertFalse(client.in_transaction)
        self.assertEqual((count(self.path), count(self.path, "request_outcome")), (0, 0))

    def test_mixed_case_boundaries_are_routed(self):
        client = self.connect()
        client.execute("begin Immediate")
        rid = client._rid
        self.assertIsNotNone(rid)
        client.execute("INSERT INTO probe (name) VALUES ('w')")
        client.execute("Commit Transaction")
        self.assertEqual((count(self.path), recorded(self.path, rid)), (1, True))

    def test_a_transaction_a_savepoint_opens_is_a_read(self):
        # The library's snapshot reads open a transaction with SAVEPOINT; it
        # writes nothing, or its RELEASE would commit writes with no id.
        client = self.connect()
        client.execute("SAVEPOINT snap")
        with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            client.execute("INSERT INTO probe (name) VALUES ('s')")
        client.execute("RELEASE snap")
        self.assertFalse(client.in_transaction)
        self.assertEqual(client.execute("PRAGMA query_only").fetchone()[0], 0)
        self.assertEqual((count(self.path), count(self.path, "request_outcome")), (0, 0))

    def test_a_read_cannot_switch_query_only_off(self):
        # The second review of 2026-10-04: with `query_only` off, a read's
        # COMMIT or RELEASE would commit writes no id owns. The hub runs
        # `PRAGMA query_only = 0` itself first, so a statement cached from
        # that text must not pass unchecked.
        for opening, closing in (("BEGIN", "COMMIT"), ("SAVEPOINT snap", "RELEASE snap")):
            for pragma in ("PRAGMA query_only=OFF", "pragma QUERY_ONLY = 0", "PRAGMA main.query_only=false",
                           "PRAGMA query_only = 0"):
                with self.subTest(opening=opening, pragma=pragma):
                    client = self.connect()
                    client.execute(opening)
                    with self.assertRaisesRegex(remote.RemoteError, "query_only is the hub's"):
                        client.execute(pragma)
                    with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                        client.execute("INSERT INTO probe (name) VALUES ('r')")
                    self.assertEqual(client.execute("PRAGMA query_only").fetchone()[0], 1)
                    client.execute(closing)
                    self.assertFalse(client.in_transaction)
                    client.close()
        self.assertEqual((count(self.path), count(self.path, "request_outcome")), (0, 0))

    def test_query_only_is_the_hubs_inside_a_write_and_the_sessions_outside(self):
        # Outside a transaction a read saves and restores it, and a backup of
        # a database waiting for `migrate` switches it off on a `mode=ro`
        # open, so it stays the session's there.
        client = self.connect()
        client.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(remote.RemoteError, "query_only is the hub's"):
            client.execute("PRAGMA query_only = ON")
        client.execute("ROLLBACK")
        client.execute("PRAGMA query_only = ON")
        with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            client.execute("INSERT INTO probe (name) VALUES ('o')")
        client.execute("BEGIN")
        client.execute("COMMIT")
        self.assertEqual(client.execute("PRAGMA query_only").fetchone()[0], 1)
        client.execute("PRAGMA query_only = OFF")
        client.execute("INSERT INTO probe (name) VALUES ('o')")
        self.assertEqual(count(self.path), 1)

    def test_a_script_ends_what_it_opens(self):
        # A script carries no R, like an autocommit write, and the hub does
        # not route it: its BEGIN and COMMIT are refused (the seventh finding
        # of 2026-10-04). One that leaves a SAVEPOINT open is rolled back.
        client = self.connect()
        for script in ("BEGIN IMMEDIATE; INSERT INTO probe (name) VALUES ('b'); COMMIT;",
                       "BEGIN; INSERT INTO probe (name) VALUES ('b');"):
            with self.subTest(script=script):
                with self.assertRaisesRegex(remote.RemoteError, "BEGIN outside the hub's transaction handling"):
                    client.executescript(script)
                self.assertFalse(client.in_transaction)
        client.executescript("SAVEPOINT s; INSERT INTO probe (name) VALUES ('s'); RELEASE s;")
        self.assertFalse(client.in_transaction)
        with self.assertRaisesRegex(remote.RemoteError, "left a transaction open"):
            client.executescript("SAVEPOINT s; INSERT INTO probe (name) VALUES ('o');")
        self.assertFalse(client.in_transaction)
        self.assertEqual((count(self.path), count(self.path, "request_outcome")), (1, 0))

    def test_rollback_to_a_savepoint_after_a_failed_commit_keeps_the_row(self):
        # The failed COMMIT inserted the outcome row; ROLLBACK TO removes it,
        # and the next COMMIT must insert it again.
        self.deferred()
        for op in ("execute", "commit"):
            with self.subTest(op=op):
                client = self.connect()
                client.execute("BEGIN IMMEDIATE")
                rid = client._rid
                client.execute("SAVEPOINT before")
                client.execute("INSERT INTO child (parent) VALUES (7)")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
                    self.commit(client, op)
                self.assertTrue(client.in_transaction)
                client.execute("ROLLBACK TO before")
                client.execute("INSERT INTO probe (name) VALUES (?)", (op,))
                self.commit(client, op)
                self.assertEqual((count(self.path, "child"), recorded(self.path, rid)), (0, True))
        self.assertEqual(count(self.path), 2)

    def test_a_failed_commit_fixed_in_place_commits_once_with_its_row(self):
        self.deferred()
        for parent, op in ((7, "execute"), (8, "commit")):
            with self.subTest(op=op):
                client = self.connect()
                client.execute("BEGIN IMMEDIATE")
                rid = client._rid
                client.execute("INSERT INTO child (parent) VALUES (?)", (parent,))
                with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
                    self.commit(client, op)
                client.execute("INSERT INTO parent (id) VALUES (?)", (parent,))
                self.commit(client, op)
                self.assertTrue(recorded(self.path, rid))
        self.assertEqual((count(self.path, "child"), count(self.path, "request_outcome")), (2, 2))


class TheWrite(OutcomeCase):
    """The third review of 2026-10-04: a write that moves no row and neither
    of `main`'s versions still commits, so it needs its row. SQLite's
    authorizer names every statement's action, for any database."""

    def run_write(self, sql: str, op: str) -> str:
        client = self.connect()
        client.execute("BEGIN IMMEDIATE")
        rid = client._rid
        client.execute(sql)
        TheTransactionBoundary.commit(client, op)
        client.close()
        return rid

    def test_a_write_outside_the_row_count_and_mains_versions_records_its_row(self):
        # `temp` is the one database besides `main` a write may reach. A
        # pragma that sets a header field is refused over the wire (step 7),
        # so DDL in `temp` stands for it.
        for sql in ("CREATE TABLE temp.added (x)", "CREATE VIEW temp.seen AS SELECT 1"):
            for op in ("execute", "commit"):
                with self.subTest(sql=sql, op=op):
                    self.assertTrue(recorded(self.path, self.run_write(sql, op)))

    def test_the_same_write_text_twice_records_two_rows(self):
        # Python does not authorize a statement it reuses from its cache, and
        # REINDEX moves no row, so only the authorizer sees it: the hub's
        # session caches no statement.
        raw = sqlite3.connect(self.path)
        raw.execute("CREATE INDEX probe_name ON probe (name)")
        raw.close()
        client = self.connect()
        rids = []
        for _ in range(2):
            client.execute("BEGIN IMMEDIATE")
            rids.append(client._rid)
            client.execute("REINDEX probe_name")
            client.execute("COMMIT")
        self.assertEqual([recorded(self.path, rid) for rid in rids], [True, True])

    def test_a_transaction_that_only_reads_records_no_row(self):
        for op in ("execute", "commit"):
            with self.subTest(op=op):
                client = self.connect()
                client.execute("BEGIN IMMEDIATE")
                rid = client._rid
                client.execute("SELECT count(*) FROM probe").fetchall()
                client.execute("PRAGMA user_version").fetchall()
                client.execute("SAVEPOINT inner")
                client.execute("RELEASE inner")
                TheTransactionBoundary.commit(client, op)
                self.assertFalse(recorded(self.path, rid))
        self.assertEqual((count(self.path), count(self.path, "request_outcome")), (0, 0))

    def test_a_temp_table_named_request_outcome_does_not_take_the_row(self):
        # The fifth finding of 2026-10-04: an unqualified name finds a TEMP
        # table first, so the row went there and `outcome(R)` read `absent`.
        for op in ("execute", "commit"):
            with self.subTest(op=op):
                client = self.connect()
                client.execute("CREATE TEMP TABLE request_outcome (id TEXT PRIMARY KEY, committed_at TEXT)")
                client.execute("BEGIN IMMEDIATE")
                rid = client._rid
                # R in the TEMP table too: the commit's own lookup must not
                # take it for the row.
                client.execute("INSERT INTO temp.request_outcome VALUES (?, 'x')", (rid,))
                client.execute("INSERT INTO probe (name) VALUES ('t')")
                TheTransactionBoundary.commit(client, op)
                client.execute("DROP TABLE temp.request_outcome")
                self.assertEqual(client.outcome(rid), remote.RECORDED)
                client.close()

    def test_attach_is_refused_over_the_wire(self):
        # The sixth and seventh findings of 2026-10-04: a COMMIT that spans
        # an attached database is not crash-atomic with `main`'s outcome
        # row, and a TEMP trigger on the outcome table writes under the
        # hub's own insert. With nothing attached, no path writes elsewhere.
        aux = self.root / "aux.db"
        raw = sqlite3.connect(aux)
        raw.execute("CREATE TABLE t (x)")
        raw.commit()
        raw.close()
        attach = ("ATTACH DATABASE ? AS aux", (str(aux),))
        client = self.connect()
        with self.assertRaisesRegex(remote.RemoteError, "refused: ATTACH"):
            client.execute(*attach)
        with self.assertRaisesRegex(remote.RemoteError, "refused: ATTACH"):
            client.executescript(f"ATTACH DATABASE '{aux}' AS aux;")
        for begin in ("BEGIN IMMEDIATE", "BEGIN"):
            with self.subTest(begin=begin):
                client.execute(begin)
                with self.assertRaisesRegex(remote.RemoteError, "refused: ATTACH"):
                    client.execute(*attach)
                client.execute("ROLLBACK")
        # The hub's own statements, as `_commit` runs its insert.
        session = object.__new__(serve.Session)
        session.routing, session.denied, session.wrote, session.kind = True, None, False, "write"
        self.assertEqual(session._authorize(sqlite3.SQLITE_ATTACH, str(aux), None, None, None),
                         sqlite3.SQLITE_DENY)
        # The reviewer's case: the trigger's `t` names no table, attached or
        # not, so the COMMIT fails and writes nothing anywhere. A script,
        # since a write sent alone records a row (sd:2671), which fires it.
        client.executescript("CREATE TEMP TRIGGER leak AFTER INSERT ON main.request_outcome "
                             "BEGIN INSERT INTO t VALUES (1); END;")
        client.execute("BEGIN IMMEDIATE")
        client.execute("INSERT INTO probe (name) VALUES ('t')")
        with self.assertRaisesRegex(sqlite3.OperationalError, "no such table: t"):
            client.execute("COMMIT")
        client.close()
        self.assertEqual((count(aux, "t"), count(self.path), count(self.path, "request_outcome")), (0, 0, 0))

CHILD = """
import os, signal, sys, time
from pathlib import Path
from sd_db import serve

point = sys.argv[3]

def fault(where, session):
    if where == point:
        os.kill(os.getpid(), signal.SIGKILL)
        # SIGKILL to this process can land after kill() returns; this thread
        # must not send its answer in between.
        while True:
            time.sleep(1)

serve._fault = fault
raise SystemExit(serve.serve(int(sys.argv[1]), Path(sys.argv[2])))
"""


class Child:
    """A server in a child process, killed with SIGKILL at `serve._fault`."""

    def __init__(self, root: Path, path: Path, port: int, point: str) -> None:
        self.log = root / f"serve-{point}-{time.monotonic_ns()}.log"
        self.stream = open(self.log, "w")
        home = root / "home"
        self.process = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(port), str(path), point],
            stdout=subprocess.DEVNULL, stderr=self.stream, cwd=HERE,
            env={**os.environ, "PYTHONPATH": str(HERE), "HOME": str(home)},
        )
        deadline = time.monotonic() + RESTART_BOUND
        while "serving" not in self.log.read_text():
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise AssertionError(f"serve did not start: {self.log.read_text()}")
            time.sleep(0.05)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(30)
        self.stream.close()


class TheKilledHub(unittest.TestCase):
    """Criterion 11 (b) and (c): `kill -9` on the hub around the commit, a
    restart on the same port, and the client's asks across it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = fresh_database(self.root)
        with socket.create_server(("127.0.0.1", 0)) as probe:
            self.port = probe.getsockname()[1]
        self.token_file = self.path.with_name(self.path.name + serve.TOKEN_SUFFIX)

    def killed_at(self, point: str):
        """A client's write whose hub dies at `point` and comes back.

        Returns the client and what its block raised, or `None`.
        """
        first = Child(self.root, self.path, self.port, point)
        self.addCleanup(first.stop)
        # Read afresh at each session: the restarted hub writes a new token.
        client = remote.connect("127.0.0.1", self.port, self.path,
                                token=lambda: read_token(self.token_file))
        self.addCleanup(client.close)
        restarted: list[Child] = []

        def restart():
            first.process.wait(RESTART_BOUND)
            restarted.append(Child(self.root, self.path, self.port, "none"))

        helper = threading.Thread(target=restart)
        helper.start()
        raised = None
        try:
            with transaction(client):
                client.execute("INSERT INTO probe (name) VALUES ('killed')")
        except remote.RemoteError as error:
            raised = error
        helper.join(RESTART_BOUND)
        for child in restarted:
            self.addCleanup(child.stop)
        self.assertEqual(first.process.returncode, -signal.SIGKILL)
        self.assertTrue(restarted, "the hub did not restart")
        return client, raised

    def test_killed_after_the_commit_the_retry_finds_it_recorded(self):
        """(b) The commit landed and the hub died before it answered: after
        the restart the client's ask reads `recorded`. One row, and its
        `request_outcome` row exists."""
        client, raised = self.killed_at("after-commit")
        self.assertIsNone(raised)
        rid, answer = client.outcomes[-1]
        self.assertEqual(answer, remote.RECORDED)
        self.assertEqual(count(self.path), 1)
        self.assertTrue(recorded(self.path, rid))

    def test_killed_before_the_commit_the_retry_finds_it_absent(self):
        """(c) The hub died before the commit: after the restart the ask reads
        `absent`, `TransactionLost`, zero rows; the verb run again writes one."""
        client, raised = self.killed_at("before-commit")
        self.assertIsInstance(raised, remote.TransactionLost)
        self.assertEqual(client.outcomes[-1], (raised.rid, remote.ABSENT))
        self.assertEqual(count(self.path), 0)
        self.assertFalse(recorded(self.path, raised.rid))
        again = remote.connect("127.0.0.1", self.port, self.path, token=lambda: read_token(self.token_file))
        self.addCleanup(again.close)
        with transaction(again):
            again.execute("INSERT INTO probe (name) VALUES ('again')")
        self.assertEqual(count(self.path), 1)


if __name__ == "__main__":
    unittest.main()
