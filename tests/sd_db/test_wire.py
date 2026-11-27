"""Steps 1 and 2 of the second-machine plan: the surface, and the loopback wire.

`docs/work/2026-09-22-run-the-framework-from-a-second-machine/implement.md`.
Step 1 names the connection surface and guards the suite with it; step 2
serves a database on 127.0.0.1 and reaches it through `sd_db.remote`. The
whole suite over the wire is `sd-db.sh test --remote`, criterion 1; these
are the properties that run proves nothing about.
"""

from __future__ import annotations

import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sd_db
from sd_db import create_item, database, initialise, remote, schema
from sd_db.database import transaction
from sd_db.serve import read_token
from sd_db.testing import Stubs, surface

from . import support

HERE = support.LIB
SD_DB = support.SD_DB


def environment(root: Path) -> dict[str, str]:
    """A child's environment with `HOME` under the test's own directory.

    `sd-db.sh serve` without `--database` serves `default_path()`. Under the
    operator's `HOME` that is the live database, so no child here inherits it.
    """
    home = root / "home"
    return {**os.environ, "PYTHONPATH": str(HERE), "PYTHON": sys.executable,
            "HOME": str(home), "XDG_DATA_HOME": str(home / ".local" / "share")}


class Served:
    """One `sd-db.sh serve --loopback` over a fresh database, and its log."""

    def __init__(self, root: Path, name: str = "sd.db", *, build: Path = HERE, log: str = "",
                 program: tuple[str, ...] = (sys.executable, "-m", "sd_db.serve"), port: int = 0) -> None:
        """`build` is the folder holding the `sd_db` the server runs; another
        one is another `sd_db` build (step 3). `program` is what runs there,
        `sd-db serve` without the launcher. A database already there is
        served as it is. `port` 0 takes a free one; a hub restarted in place
        takes the port it had."""
        self.database = root / name
        self.token_file = root / f"{name}.serve.token"
        if not self.database.exists():
            initialise(self.database)
        self.log = root / f"{name}.serve{log}.log"
        self.stream = open(self.log, "w")
        self.process = subprocess.Popen(
            [*program, "--loopback", "--port", str(port), "--database", str(self.database)],
            # `python -m` puts the working directory first on `sys.path`, so
            # the build's own folder, or the child imports this one's sd_db.
            stdout=subprocess.DEVNULL, stderr=self.stream, env=environment(root), cwd=build,
        )
        try:
            self._started()
        except BaseException:
            # A setUp that fails registers no cleanup; stop the child here
            # or it outlives the run.
            self.stop()
            raise

    def _started(self) -> None:
        deadline = time.monotonic() + 30
        while True:
            found = re.search(r"on 127\.0\.0\.1:(\d+)", self.log.read_text())
            if found:
                self.port = int(found.group(1))
                self.token = read_token(self.token_file)
                return
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise AssertionError(f"serve did not start: {self.log.read_text()}")
            time.sleep(0.05)

    def connect(self, path: Path | None = None, **options) -> remote.Connection:
        options.setdefault("token", self.token)
        return remote.connect("127.0.0.1", self.port, path or self.database, **options)

    def stop(self) -> str:
        self.process.terminate()
        self.process.wait(timeout=30)
        self.stream.close()
        return self.log.read_text()


class ServedCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.served = Served(self.root)
        self.addCleanup(self.served.stop)
        local = database.connect(self.served.database)
        local.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, name TEXT NOT NULL, blob BLOB, real REAL)")
        local.close()


class TheSurface(unittest.TestCase):
    """Step 1: a connection that raises outside the traced surface."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        surface.install()
        self.addCleanup(surface.uninstall)

    def test_an_attribute_outside_the_surface_is_refused(self):
        connection = database.connect(self.path)
        self.addCleanup(connection.close)
        for name in ("create_function", "interrupt", "cursor"):
            with self.subTest(name=name), self.assertRaises(surface.SurfaceViolation):
                getattr(connection, name)
        with self.assertRaises(surface.SurfaceViolation):
            _ = connection.execute("SELECT 1").description

    def test_a_library_write_passes_inside_the_surface(self):
        connection = database.connect(self.path)
        self.addCleanup(connection.close)
        item = create_item(connection, kind="work", title="through the guard")
        self.assertFalse(connection.in_transaction)
        self.assertEqual(
            connection.execute("SELECT title FROM item WHERE id = ?", (item,)).fetchone()["title"],
            "through the guard",
        )



class TheGuardStack(unittest.TestCase):
    def test_uninstall_past_its_installs_keeps_the_outer_opener(self):
        """`sd-db.sh test --remote` installs the wire opener first; an extra
        uninstall must not drop it for the rest of the run."""

        def outer(target, **options):
            raise AssertionError("not called")

        saved, stack = database._opener, list(surface._previous)
        try:
            database._opener = outer
            surface.install()
            surface.uninstall()
            surface.uninstall()
            self.assertIs(database._opener, outer)
        finally:
            database._opener = saved
            surface._previous[:] = stack


class TheRemoteSurface(ServedCase):
    def test_the_remote_connection_carries_the_whole_surface(self):
        connection = self.served.connect()
        self.addCleanup(connection.close)
        cursor = connection.execute("SELECT 1")
        missing = [name for name in surface.CONNECTION_SURFACE if not hasattr(connection, name.rstrip("="))]
        missing += [name for name in surface.CURSOR_SURFACE if not hasattr(cursor, name)]
        self.assertEqual(missing, [])


class TheWire(ServedCase):
    """Step 2: a remote connection answers as a local one does."""

    def test_rows_values_and_counters_match_a_local_connection(self):
        local = database.connect(self.served.database)
        self.addCleanup(local.close)
        wire = self.served.connect()
        self.addCleanup(wire.close)
        answers = {}
        for name, connection in (("wire", wire), ("local", local)):
            with transaction(connection):
                inserted = connection.execute(
                    "INSERT INTO probe (name, blob, real) VALUES (?, ?, ?)", (name, b"\x00\xff", 0.25))
                self.assertTrue(connection.in_transaction)
            self.assertFalse(connection.in_transaction)
            updated = connection.execute("UPDATE probe SET real = real + 1")
            row = connection.execute("SELECT id AS n, name, blob, real FROM probe WHERE id = ?",
                                     (inserted.lastrowid,)).fetchone()
            answers[name] = (type(row), row.keys(), dict(row), updated.rowcount)
        self.assertEqual(answers["wire"][:2], answers["local"][:2])
        self.assertEqual(answers["wire"][0], sqlite3.Row)
        self.assertEqual(answers["wire"][2], {"n": 1, "name": "wire", "blob": b"\x00\xff", "real": 1.25})
        self.assertEqual(answers["local"][2], {"n": 2, "name": "local", "blob": b"\x00\xff", "real": 1.25})
        self.assertEqual((answers["wire"][3], answers["local"][3]), (1, 2))

    def test_errors_keep_their_class_and_their_text(self):
        wire = self.served.connect()
        self.addCleanup(wire.close)
        local = database.connect(self.served.database)
        self.addCleanup(local.close)
        raised = []
        for connection in (wire, local):
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                connection.execute("INSERT INTO probe (name) VALUES (NULL)")
            raised.append((str(caught.exception), caught.exception.sqlite_errorname))
        self.assertEqual(raised[0], raised[1])
        with self.assertRaisesRegex(FileNotFoundError, "no database at"):
            self.served.connect(self.root / "absent.db")
        self.assertFalse((self.root / "absent.db").exists())

    def test_a_backup_from_the_wire_is_the_backup_a_local_connection_takes(self):
        wire = self.served.connect()
        self.addCleanup(wire.close)
        wire.execute("INSERT INTO probe (name) VALUES ('copied')")
        local = database.connect(self.served.database)
        self.addCleanup(local.close)
        copies = {}
        for name, connection in (("wire", wire), ("local", local)):
            path = self.root / f"{name}.db"
            target = sqlite3.connect(path, isolation_level=None)
            connection.backup(target)
            target.close()
            reopened = sqlite3.connect(path)
            copies[name] = (path.read_bytes()[18:20], tuple(reopened.iterdump()))
            reopened.close()
        self.assertEqual(copies["wire"], copies["local"])
        self.assertIn("INSERT INTO \"probe\" VALUES(1,'copied',NULL,NULL);", copies["wire"][1])

    def test_a_backup_larger_than_one_chunk_arrives_whole(self):
        """The image comes in chunks, so no frame bounds the database's size."""
        wire = self.served.connect()
        self.addCleanup(wire.close)
        wire.execute("INSERT INTO probe (name, blob) VALUES ('large', ?)", (os.urandom(20000),))
        chunk = remote.CHUNK
        remote.CHUNK = 1024
        self.addCleanup(setattr, remote, "CHUNK", chunk)
        target = sqlite3.connect(self.root / "chunked.db", isolation_level=None)
        self.addCleanup(target.close)
        wire.backup(target)
        local = database.connect(self.served.database)
        self.addCleanup(local.close)
        self.assertEqual(tuple(target.iterdump()), tuple(local.iterdump()))

    def test_a_session_without_the_owner_token_is_refused(self):
        """Loopback is not a user boundary; the owner-only token file is."""
        self.assertEqual(self.served.token_file.stat().st_mode & 0o777, 0o600)
        for token in ("", "not-the-token"):
            with self.subTest(token=token), self.assertRaisesRegex(remote.RemoteError, "token"):
                self.served.connect(token=token)

    def test_a_session_cannot_create_a_database(self):
        with self.assertRaisesRegex(remote.RemoteError, "cannot create a database; run `bin/sd-db init`"):
            self.served.connect(self.root / "new.db", create=True)
        self.assertFalse((self.root / "new.db").exists())

    def test_every_frame_carries_the_version_and_the_request_id(self):
        with socket.create_connection(("127.0.0.1", self.served.port)) as raw:
            remote.send_frame(raw, remote.request("open", path=str(self.served.database), write=False,
                                                  create=False, busy_timeout=5000,
                                                  token=self.served.token, **remote.handshake()))
            opened = remote.read_frame(raw)
            self.assertEqual((opened["v"], opened["rid"], opened["ok"]), (1, None, True))
            remote.send_frame(raw, {**remote.request("execute", sql="SELECT 1", params=[]), "rid": "R1"})
            self.assertEqual(remote.read_frame(raw)["rid"], "R1")
            remote.send_frame(raw, {**remote.request("execute", sql="SELECT 1", params=[]), "v": 99})
            refused = remote.read_frame(raw)
        self.assertFalse(refused["ok"])
        self.assertIn("protocol version 99 refused", refused["error"]["text"])

    def test_a_dropped_session_rolls_its_transaction_back(self):
        wire = self.served.connect()
        wire.execute("BEGIN IMMEDIATE")
        wire.execute("INSERT INTO probe (name) VALUES ('dropped')")
        wire._abandon()
        other = self.served.connect(busy_timeout=10000)
        self.addCleanup(other.close)
        with transaction(other):
            count = other.execute("SELECT count(*) FROM probe WHERE name = 'dropped'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_the_log_names_the_longest_gap_inside_a_write_transaction(self):
        wire = self.served.connect()
        self.addCleanup(wire.close)
        with transaction(wire):
            wire.execute("INSERT INTO probe (name) VALUES ('slow')")
            time.sleep(0.2)
            wire.execute("SELECT 1")
        wire.close()
        log = self.served.stop()
        gaps = [float(g) for g in re.findall(r"write transaction: \d+ frames, longest gap ([\d.]+) ms", log)]
        self.assertTrue(gaps and max(gaps) >= 200, log)
        summary = re.search(r"longest in-transaction frame gap ([\d.]+) ms", log)
        self.assertTrue(summary and float(summary.group(1)) >= 200, log)


class TheConfinement(ServedCase):
    """Step 7's review: a session's SQL stays inside the served file.

    A refused statement is an error frame and the session keeps serving: the
    statement never ran, and the authorizer still guards the next one.
    """

    def wire(self) -> remote.Connection:
        wire = self.served.connect()
        self.addCleanup(wire.close)
        return wire

    def assertServing(self, wire: remote.Connection) -> None:
        self.assertEqual(wire.execute("SELECT count(*) FROM probe").fetchone()[0], 0)

    def assertRefusedEveryWay(self, wire: remote.Connection, sql: str, named: str) -> None:
        """`sql` is refused through each op that runs SQL, as `StatementRefused`
        naming `named`, and the session serves the next statement."""
        for op, run in (("execute", lambda: wire.execute(sql)),
                        ("executemany", lambda: wire.executemany(sql, [()])),
                        ("executescript", lambda: wire.executescript(sql))):
            with self.subTest(sql=sql, op=op):
                with self.assertRaisesRegex(remote.StatementRefused, re.escape(named)) as caught:
                    run()
                self.assertIsInstance(caught.exception, sqlite3.DatabaseError)
                self.assertServing(wire)

    def test_attach_is_refused_and_opens_no_file(self):
        wire = self.wire()
        other = self.root / "other.db"
        self.assertRefusedEveryWay(wire, f"ATTACH DATABASE '{other}' AS other", f"ATTACH or VACUUM would open {other}")
        self.assertFalse(other.exists())
        self.assertIn(f"refused: ATTACH or VACUUM would open {other}", self.served.stop())

    def test_detach_meets_sqlites_own_refusal_as_nothing_is_attached(self):
        wire = self.wire()
        for sql in ("DETACH DATABASE main", "DETACH DATABASE absent"):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    wire.execute(sql)
                self.assertNotIsInstance(caught.exception, remote.StatementRefused)
                self.assertServing(wire)

    def test_vacuum_into_goes_through_attach_and_creates_no_file(self):
        actions = []
        probe = sqlite3.connect(":memory:")
        self.addCleanup(probe.close)
        probe.set_authorizer(lambda action, *_: actions.append(action) or sqlite3.SQLITE_OK)
        probe.execute(f"VACUUM INTO '{self.root / 'probe-copy.db'}'")
        self.assertIn(sqlite3.SQLITE_ATTACH, actions)
        wire = self.wire()
        copy = self.root / "copy.db"
        with self.assertRaisesRegex(remote.StatementRefused, re.escape(f"ATTACH or VACUUM would open {copy}")):
            wire.execute("VACUUM INTO ?", (str(copy),))
        self.assertRefusedEveryWay(wire, f"VACUUM main INTO '{copy}'", f"ATTACH or VACUUM would open {copy}")
        self.assertRefusedEveryWay(wire, "VACUUM", "ATTACH or VACUUM would open a temporary file")
        self.assertFalse(copy.exists())

    def test_a_pragma_off_the_allowlist_is_refused(self):
        wire = self.wire()
        for sql, named in (
            ("PRAGMA writable_schema = ON", "PRAGMA writable_schema(ON)"),
            ("PRAGMA main.writable_schema = ON", "PRAGMA writable_schema(ON)"),
            ("PRAGMA user_version = 99", "PRAGMA user_version(99)"),
            ("PRAGMA application_id = 123", "PRAGMA application_id(123)"),
            ("PRAGMA journal_mode = DELETE", "PRAGMA journal_mode(DELETE)"),
            # The hub's own open ran this exact text; the authorizer still sees it.
            ("PRAGMA journal_mode = WAL", "PRAGMA journal_mode(WAL)"),
            ("PRAGMA ignore_check_constraints = ON", "PRAGMA ignore_check_constraints(ON)"),
            ("PRAGMA wal_autocheckpoint = 0", "PRAGMA wal_autocheckpoint(0)"),
            ("PRAGMA mmap_size", "PRAGMA mmap_size"),
            ("PRAGMA wal_checkpoint", "PRAGMA wal_checkpoint"),
            ("PRAGMA optimize", "PRAGMA optimize"),
            # A table-valued pragma, inside DML so `executemany` runs it too.
            ("INSERT INTO probe (name) SELECT name FROM pragma_function_list", "PRAGMA function_list"),
        ):
            self.assertRefusedEveryWay(wire, sql, named + " is not served")
        self.assertEqual(wire.execute("PRAGMA user_version").fetchone()[0], schema.SCHEMA_VERSION)
        self.assertEqual(wire.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(wire.execute("PRAGMA writable_schema").fetchone()[0], 0)
        raw = sqlite3.connect(self.served.database)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("PRAGMA application_id").fetchone()[0], 0)

    def test_query_only_is_the_sessions_outside_a_transaction(self):
        # Step 5's rule, kept: `query_only` is allowlisted with a value, and
        # refused only inside a transaction the hub holds.
        wire = self.wire()
        wire.execute("PRAGMA query_only = ON")
        self.assertEqual(wire.execute("PRAGMA query_only").fetchone()[0], 1)
        wire.execute("PRAGMA query_only = OFF")
        wire.execute("BEGIN")
        with self.assertRaisesRegex(remote.StatementRefused, "query_only is the hub's inside a transaction"):
            wire.execute("PRAGMA query_only = OFF")
        wire.execute("ROLLBACK")
        self.assertEqual(wire.execute("PRAGMA query_only").fetchone()[0], 0)

    def test_load_extension_is_refused(self):
        wire = self.wire()
        self.assertRefusedEveryWay(wire, "SELECT load_extension('absent')", "load_extension is not served")
        self.assertRefusedEveryWay(wire, "INSERT INTO probe (name) SELECT load_extension('absent')",
                                   "load_extension is not served")

    def test_allowed_pragmas_and_verb_traffic_pass(self):
        wire = self.wire()
        for sql in ("PRAGMA table_info(probe)", "SELECT * FROM pragma_table_info('probe')",
                    "PRAGMA database_list", "PRAGMA busy_timeout = 6000", "PRAGMA integrity_check"):
            with self.subTest(sql=sql):
                self.assertTrue(wire.execute(sql).fetchall())
        wire.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(wire.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(wire.execute("PRAGMA busy_timeout").fetchone()[0], 6000)
        item = create_item(wire, kind="work", title="over the confined wire")
        self.assertEqual(wire.execute("SELECT title FROM item WHERE id = ?", (item,)).fetchone()[0],
                         "over the confined wire")


def other_build(root: Path) -> Path:
    """A copy of this `local-sd-db` with the same versions and one changed file.

    The case a version check alone admits: the package version has not been
    bumped per build, so only the build digest tells the two apart.
    """
    import shutil

    build = root / "other"
    shutil.copytree(HERE, build, ignore=shutil.ignore_patterns("__pycache__", "tests", "dist", "*.egg-info"))
    with open(build / "sd_db" / "reads.py", "a") as changed:
        changed.write("\n# another build, same version\n")
    return build


def newer_build(root: Path) -> tuple[Path, str]:
    """A copy of this `local-sd-db` whose package version is one higher.

    Same `SCHEMA_VERSION`: the version differs, which is the case a schema
    check alone admits (criterion 10).
    """
    import shutil

    build = root / "newer"
    shutil.copytree(HERE, build, ignore=shutil.ignore_patterns("__pycache__", "tests", "dist", "*.egg-info"))
    parts = sd_db.__version__.split(".")
    version = ".".join([*parts[:-1], str(int(parts[-1]) + 1)])
    init = build / "sd_db" / "__init__.py"
    text = init.read_text()
    init.write_text(text.replace(f'__version__ = "{sd_db.__version__}"', f'__version__ = "{version}"'))
    assert f'__version__ = "{version}"' in init.read_text()
    return build, version


class TheHandshake(ServedCase):
    """Step 3: the first frame carries the build; any other build is refused."""

    def rows(self) -> int:
        local = database.connect(self.served.database)
        try:
            return local.execute("SELECT count(*) FROM probe").fetchone()[0]
        finally:
            local.close()

    def test_a_newer_satellite_is_refused_before_any_statement_and_told_to_upgrade_the_hub(self):
        newer = sd_db.__version__ + ".1"
        with mock.patch.object(sd_db, "__version__", newer):
            with self.assertRaises(remote.BuildMismatch) as caught:
                self.served.connect()
            with socket.create_connection(("127.0.0.1", self.served.port)) as raw:
                remote.send_frame(raw, remote.request(
                    "open", path=str(self.served.database), write=True, create=False,
                    busy_timeout=5000, token=self.served.token, **remote.handshake()))
                self.assertFalse(remote.read_frame(raw)["ok"])
                remote.send_frame(raw, remote.request("execute", sql="INSERT INTO probe (name) VALUES ('x')",
                                                      params=[]))
                refused = remote.read_frame(raw)
        self.assertEqual(caught.exception.upgrade, "hub")
        self.assertIn(f"run `repo-sync.sh refresh` on the hub for sd_db {newer}", str(caught.exception))
        self.assertFalse(refused["ok"])
        self.assertIn("the first frame must be open", refused["error"]["text"])
        self.assertEqual(self.rows(), 0)
        self.assertIn("refused before open", self.served.stop())

    def test_a_newer_hub_refuses_this_satellite_and_names_it_to_upgrade(self):
        build, version = newer_build(self.root)
        hub = Served(self.root, "newer.db", build=build)
        self.addCleanup(hub.stop)
        with self.assertRaises(remote.BuildMismatch) as caught:
            hub.connect()
        self.assertEqual((caught.exception.field, caught.exception.upgrade), ("package", "satellite"))
        self.assertIn(f"run `repo-sync.sh follow` on this satellite for sd_db {version}", str(caught.exception))

    def test_a_hub_upgraded_under_a_connected_satellite_refuses_its_next_session(self):
        wire = self.served.connect()
        self.addCleanup(wire.close)
        self.assertEqual(wire.execute("SELECT 1").fetchone()[0], 1)
        self.served.stop()
        build, version = newer_build(self.root)
        upgraded = Served(self.root, build=build, log=".upgraded")
        self.addCleanup(upgraded.stop)
        # The write's answer is lost, so it is settled (sd:2671); the
        # old hub is gone, so its outcome stays unknown.
        with mock.patch.object(remote, "OUTCOME_BOUND", 0.3), \
                self.assertRaises(remote.UnknownOutcome) as raised:
            wire.execute("INSERT INTO probe (name) VALUES ('after')")
        self.assertIsInstance(raised.exception.__cause__, remote.HubUnreachable)
        with self.assertRaisesRegex(remote.BuildMismatch, f"run `repo-sync.sh follow` on this satellite for sd_db {version}"):
            upgraded.connect()
        self.assertEqual(self.rows(), 0)

    def test_a_hub_upgraded_in_place_under_a_held_session_names_the_build_at_once(self):
        """sd:3359. A satellite's prepare held its session while the hub restarted on a newer build, same port.

        Its next write was settled by asking the new hub, which refused each ask for the build: 60 s of
        asks (`OUTCOME_BOUND`), then an error caused by the dropped session. This process keeps the build
        it loaded, so no ask can pass: the first refusal ends the settle, and the error names the build.
        """
        token = lambda: read_token(self.served.token_file)  # noqa: E731 -- a restarted hub writes a new one
        wire = self.served.connect(token=token)
        self.addCleanup(wire.close)
        self.assertEqual(wire.execute("SELECT 1").fetchone()[0], 1)
        port = self.served.port
        self.served.stop()
        build, version = newer_build(self.root)
        upgraded = Served(self.root, build=build, log=".upgraded", port=port)
        self.addCleanup(upgraded.stop)
        started = time.monotonic()
        with self.assertRaises(remote.UnknownOutcome) as raised:
            wire.execute("INSERT INTO probe (name) VALUES ('after')")
        self.assertLess(time.monotonic() - started, remote.OUTCOME_BOUND / 4)
        self.assertIsInstance(raised.exception.__cause__, remote.BuildMismatch)
        self.assertIn(f"run `repo-sync.sh follow` on this satellite for sd_db {version}", str(raised.exception))
        # A unit that cannot have written opens a new session (sd:3336), and its refusal names the build too.
        with self.assertRaisesRegex(remote.BuildMismatch, "repo-sync.sh follow"):
            with transaction(wire):
                wire.execute("INSERT INTO probe (name) VALUES ('again')")
        self.assertEqual(self.rows(), 0)

    def test_the_same_version_with_different_code_is_refused(self):
        hub = Served(self.root, "other.db", build=other_build(self.root))
        self.addCleanup(hub.stop)
        with self.assertRaises(remote.BuildMismatch) as caught:
            hub.connect()
        self.assertEqual((caught.exception.field, caught.exception.upgrade), ("build", "both"))
        self.assertIn("run `repo-sync.sh refresh` on the hub, then `repo-sync.sh follow` on this satellite",
                      str(caught.exception))

    def test_a_build_installed_under_a_running_hub_is_refused_until_it_restarts(self):
        """sd:1480. The hub states the build it runs, not the build on its disk.

        The digest was hashed from disk at the first handshake. A hub started
        on one build, then given this client's build by an install with the
        same versions, hashed the new files while running the old code, and
        let the client in.
        """
        build = other_build(self.root)
        hub = Served(self.root, "installed.db", build=build)
        self.addCleanup(hub.stop)
        # The install: the hub's files now match this client, its code does not.
        (build / "sd_db" / "reads.py").write_bytes((HERE / "sd_db" / "reads.py").read_bytes())
        with self.assertRaisesRegex(remote.RemoteError, "restart") as caught:
            hub.connect()
        self.assertEqual(type(caught.exception).__name__, "HubRestartNeeded")
        self.assertIn("refused before open", hub.stop())
        restarted = Served(self.root, "installed.db", build=build, log=".restarted")
        self.addCleanup(restarted.stop)
        wire = restarted.connect()
        self.addCleanup(wire.close)
        self.assertEqual(wire.execute("SELECT 1").fetchone()[0], 1)

    def test_a_hub_that_meets_its_own_newer_install_stops_for_launchd_to_restart_it(self):
        """sd:3058. A post-merge install under a running hub refused every satellite until a manual kickstart.

        The old process can serve nobody once its files hold another build, so the
        first such refusal also stops it, exit 1, and the LaunchAgent's KeepAlive
        starts the installed build.
        """
        build = other_build(self.root)
        hub = Served(self.root, "installed.db", build=build)
        self.addCleanup(hub.stop)
        (build / "sd_db" / "reads.py").write_bytes((HERE / "sd_db" / "reads.py").read_bytes())
        with self.assertRaises(remote.RemoteError) as caught:
            hub.connect()
        self.assertEqual(type(caught.exception).__name__, "HubRestartNeeded")
        try:
            code = hub.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("the hub kept running a build its files no longer hold")
        self.assertEqual(code, 1)
        self.assertIn("stopping so launchd starts the installed build", hub.log.read_text())

    def test_the_schema_and_the_protocol_are_checked_too(self):
        with mock.patch.object(schema, "SCHEMA_VERSION", schema.SCHEMA_VERSION + 1):
            with self.assertRaises(remote.BuildMismatch) as caught:
                self.served.connect()
        self.assertEqual((caught.exception.field, caught.exception.upgrade), ("schema", "hub"))
        with socket.create_connection(("127.0.0.1", self.served.port)) as raw:
            remote.send_frame(raw, {**remote.request("open", path=None, write=False, create=False,
                                                     busy_timeout=5000, token=self.served.token,
                                                     **remote.handshake()), "v": remote.PROTOCOL_VERSION + 1})
            refused = remote.read_frame(raw)
        self.assertEqual(refused["error"]["name"], "BuildMismatch")
        self.assertIn("protocol version", refused["error"]["text"])
        self.assertIn("run `repo-sync.sh refresh` on the hub", refused["error"]["text"])


class TheServer(ServedCase):
    def test_a_second_server_over_the_same_file_is_refused_and_the_first_keeps_serving(self):
        second = subprocess.run(
            [sys.executable, str(SD_DB), "serve", "--loopback", "--port", "0", "--database", str(self.served.database)],
            capture_output=True, text=True, env=environment(self.root), timeout=60,
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("another server owns this database", second.stderr)
        self.assertIn("serve.lock", second.stderr)
        wire = self.served.connect()
        self.addCleanup(wire.close)
        self.assertEqual(wire.execute("SELECT 1").fetchone()[0], 1)

    def test_two_databases_in_one_folder_are_served_side_by_side(self):
        """The lock and the token are per database file, not per folder."""
        other = Served(self.root, "other.db")
        self.addCleanup(other.stop)
        self.assertNotEqual(other.token, self.served.token)
        for served in (self.served, other):
            wire = served.connect()
            self.addCleanup(wire.close)
            self.assertEqual(wire.execute("SELECT 1").fetchone()[0], 1)

    def test_a_port_in_use_is_refused_and_leaves_no_token(self):
        busy = self.root / "busy.db"
        initialise(busy)
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen()
            port = holder.getsockname()[1]
            done = subprocess.run(
                [sys.executable, str(SD_DB), "serve", "--loopback", "--port", str(port), "--database", str(busy)],
                capture_output=True, text=True, env=environment(self.root), timeout=60,
            )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(f"127.0.0.1:{port}", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(sorted(p.name for p in self.root.glob("busy.db.serve.*")), ["busy.db.serve.lock"])

    def test_the_wire_harness_refuses_a_host_serve_does_not_bind(self):
        from sd_db.testing import wire

        for host in ("::1", "localhost", "192.0.2.1"):
            with self.subTest(host=host), self.assertRaises(SystemExit):
                wire.install(host, self.served.port, self.served.token)

    def test_serve_never_creates_a_database(self):
        """Without `--loopback` too: the missing file refuses before Tailscale is asked."""
        stubs = Stubs(self.root / "stubs", names=("tailscale",))
        bare = subprocess.run([sys.executable, str(SD_DB), "serve"], capture_output=True, text=True,
                              env=stubs.environment(environment(self.root)), timeout=60)
        self.assertEqual(bare.returncode, 1)
        self.assertIn("no database at", bare.stderr)
        self.assertIn("`bin/sd-db init` from the command pack", bare.stderr)
        self.assertEqual(stubs.calls("tailscale"), [])
        self.assertFalse((self.root / "home" / ".local" / "share" / "sd").exists())
        absent = self.root / "nothing" / "sd.db"
        missing = subprocess.run(
            [sys.executable, str(SD_DB), "serve", "--loopback", "--port", "0", "--database", str(absent)],
            capture_output=True, text=True, env=environment(self.root), timeout=60,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn(f"no database at {absent.resolve()}", missing.stderr)
        self.assertFalse(absent.exists())

    def test_the_suite_runs_over_the_wire(self):
        """`sd-db.sh test --remote` end to end, narrowed to one test."""
        done = subprocess.run(
            [sys.executable, "-m", "sd_db.testing.wire", f"127.0.0.1:{self.served.port}",
             str(self.served.token_file), "-k", "test_rows_values_and_counters_match_a_local_connection"],
            capture_output=True, text=True, env=environment(self.root), timeout=300,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Ran 1 test", done.stderr)
        sessions = re.search(r"(\d+) sessions over the wire", done.stderr)
        self.assertTrue(sessions and int(sessions.group(1)) > 0, done.stderr)

    def test_remote_needs_an_address_and_a_token_file(self):
        for extra in ([], [f"127.0.0.1:{self.served.port}"]):
            with self.subTest(extra=extra):
                done = subprocess.run([sys.executable, "-m", "sd_db.testing.wire", *extra], capture_output=True,
                                      text=True, env=environment(self.root), timeout=60)
                self.assertEqual(done.returncode, 1)
                self.assertIn("TOKEN_FILE", done.stderr)

    def test_the_token_file_goes_when_the_server_stops(self):
        self.assertTrue(self.served.token_file.exists())
        self.served.stop()
        self.assertFalse(self.served.token_file.exists())


class TheLog(unittest.TestCase):
    def test_each_line_carries_its_local_time_and_zone(self):
        """pack sd:10221: satellites timed out at named minutes, and the hub's log had no time on any line, so
        no stall could be matched to what the hub did then. Each line now starts with the local time and zone."""
        from io import StringIO

        from sd_db import serve

        stream = StringIO()
        try:
            with mock.patch.dict(os.environ, {"TZ": "America/Denver"}):
                time.tzset()
                with mock.patch.object(serve.time, "time", return_value=1795473000.0):
                    serve.Log(stream).transaction(7, 3, 0.25, 12.5, "'COMMIT'")
        finally:
            time.tzset()
        self.assertEqual(stream.getvalue(), "2026-11-23 15:30:00 MST sd-db serve: session 7 write transaction: "
                                            "3 frames, longest gap 250.0 ms, wall 12500.0 ms, before 'COMMIT'\n")


if __name__ == "__main__":
    unittest.main()
