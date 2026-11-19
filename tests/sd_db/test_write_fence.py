"""The write fence on a returning hub's own database (system sd:5986, slice 1).

A returning old hub keeps its `sd.db` and has no `hub.json`, so a session,
a job or the dashboard on it would write its stale record. Every write
through the library to this machine's own database asks the fence before it
runs and again at its commit: `elsewhere` refuses with `hub.Fenced`, naming
the holder and the two demotion steps, `hub.json` first. `holder`, `off`
and `unknown` write (#31093). Reads stay allowed. `sd-db restore` writes
before the promoted machine takes the hub's name, and passes.

`tailscale` is the harness stub on PATH; no test reaches the real daemon.
"""

from __future__ import annotations

import contextlib
import functools
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from sd_db import create_item, database, hub, initialise, writes
from sd_db.backup import restore
from sd_db.backup import run as backup
from sd_db.errors import SdDbError
from sd_db.migrate import migrate
from sd_db.schema import SCHEMA_VERSION
from sd_db.testing.store import make_store

from . import support
from .test_fence import HUB_NAME, StubCase, set_during

OLD_HUB = "old-hub.example.test."



class FenceCase(StubCase):
    """`StubCase` with this home's own database: the default path the fence guards."""

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))
        self.enterContext(mock.patch.dict(hub._answers, clear=True))
        self.path = database.local_path()
        self.path.parent.mkdir(parents=True)
        initialise(self.path)

    def name_hub(self, value: str | None) -> None:
        super().name_hub(value)
        self.later()

    def later(self):
        """Past `FENCE_SECONDS`, so the next write asks the fence again."""
        hub._answers.clear()

    def open(self) -> sqlite3.Connection:
        connection = database.connect()
        self.addCleanup(connection.close)
        return connection

    def items(self) -> int:
        connection = database.connect(write=False)
        try:
            return connection.execute("SELECT count(*) FROM item").fetchone()[0]
        finally:
            connection.close()


class TheWriteFence(FenceCase):
    def test_a_local_write_on_a_fenced_hub_refuses(self):
        self.name_hub(HUB_NAME)
        self.node(OLD_HUB)
        connection = self.open()
        with self.assertRaises(hub.Fenced) as caught:
            create_item(connection, kind="work", title="stale")
        self.assertEqual(caught.exception.state, "elsewhere", "pack sd:9543: a demotion lasts")
        text = str(caught.exception)
        self.assertIn(f"this machine is not the hub, so it writes nothing to {self.path}", text)
        self.assertIn(f"sd.hub_name names {HUB_NAME}, and this node is old-hub.example.test", text)
        self.assertIn(f"sd commands then reach the hub, {HUB_NAME}", text)
        self.assertFalse(connection.in_transaction)
        self.assertEqual(self.items(), 0)
        self.assertEqual(connection.execute("SELECT count(*) FROM item").fetchone()[0], 0, "reads still work")

    def test_the_refusal_names_hub_json_first(self):
        """Demote 2 in the design's failure table: the old order left no `hub.json` and no database."""
        self.name_hub(HUB_NAME)
        self.node(OLD_HUB)
        with self.assertRaises(hub.Fenced) as caught:
            self.open().execute("INSERT INTO state (kind, key, body, timestamp) VALUES ('x', 'y', '{}', '2026')")
        text = str(caught.exception)
        first = text.index("1. copy ~/.config/sd/hub.json from any satellite")
        second = text.index(f"2. move {self.path} aside as sd.db.demoted-<date>")
        self.assertLess(first, second)

    def test_the_refusal_names_the_raw_fix_for_a_mistyped_hub_name(self):
        """Decision on #31345: no exception for `sd.hub_name`; the refusal names the fix, and the fix works."""
        typo = "hub-typo.example.test"
        self.name_hub(typo)
        self.node()
        connection = self.open()
        with self.assertRaises(hub.Fenced) as caught:
            create_item(connection, kind="work", title="locked out")
        text = str(caught.exception)
        self.assertIn(f"If this machine is the hub and sd.hub_name is mistyped, no sd command can fix it: "
                      f"run sqlite3 {self.path} \"", text)
        self.assertIn(f"or rename this node in Tailscale to {typo}", text)
        fix = text.split(f"run sqlite3 {self.path} \"", 1)[1].split("\"", 1)[0]
        raw = sqlite3.connect(self.path)
        raw.execute(fix.replace("<this node>", HUB_NAME))
        raw.commit()
        raw.close()
        self.later()
        create_item(connection, kind="work", title="fixed")
        self.assertEqual(self.items(), 1)

    def test_the_live_hub_writes_as_today(self):
        self.name_hub(HUB_NAME)
        self.node()
        create_item(self.open(), kind="work", title="live")
        self.assertEqual(self.items(), 1)

    def test_an_unknown_fence_writes_locally(self):
        """#31093: a Tailscale outage does not stop a healthy hub, and a returning one writes only here."""
        self.name_hub(HUB_NAME)
        for state in ({"backend": "Stopped"}, {"online": False}, {"dnsname": ""}):
            with self.subTest(state=state):
                hub._name_failed = None
                self.later()
                self.node(**state)
                self.assertEqual(hub.write_role(self.path).state, "unknown")
                create_item(self.open(), kind="work", title="outage")
        self.assertEqual(self.items(), 3)

    def test_an_unset_hub_name_writes_and_asks_tailscale_nothing(self):
        self.node(OLD_HUB)
        create_item(self.open(), kind="work", title="unfenced")
        self.assertEqual(self.items(), 1)
        self.assertEqual(self.asked(), [])

    def test_a_write_open_across_the_rename_rolls_back(self):
        """The commit asks again, so a transaction open when the console renames the node writes nothing."""
        self.name_hub(HUB_NAME)
        self.node()
        connection = self.open()
        with self.assertRaises(hub.Fenced):
            with database.transaction(connection):
                create_item(connection, kind="work", title="open across the rename")
                self.node(OLD_HUB)
                self.later()
        self.assertFalse(connection.in_transaction)
        self.assertEqual(self.items(), 0)

    def test_a_raw_commit_across_the_rename_rolls_back(self):
        """`COMMIT` by hand, `commit()`, `with connection:` and the outer savepoint's RELEASE each ask."""
        self.name_hub(HUB_NAME)
        endings = {
            "COMMIT": lambda connection: connection.execute("COMMIT"),
            "commit()": lambda connection: connection.commit(),
            "with": lambda connection: connection.__exit__(None, None, None),
            "RELEASE": lambda connection: connection.execute("RELEASE outer"),
        }
        for name, end in endings.items():
            with self.subTest(end=name):
                self.later()
                self.node()
                connection = self.open()
                connection.execute("SAVEPOINT outer" if name == "RELEASE" else "BEGIN IMMEDIATE")
                create_item(connection, kind="work", title=name)
                self.node(OLD_HUB)
                self.later()
                with self.assertRaises(hub.Fenced):
                    end(connection)
                self.assertFalse(connection.in_transaction)
                self.assertEqual(self.items(), 0)

    def test_every_statement_path_refuses(self):
        self.name_hub(HUB_NAME)
        self.node(OLD_HUB)
        connection = self.open()
        insert = "INSERT INTO state (kind, key, body, timestamp) VALUES ('x', ?, '{}', '2026')"
        paths = {
            "execute": lambda: connection.execute(insert, ("a",)),
            "executemany": lambda: connection.executemany(insert, [("b",)]),
            "executescript": lambda: connection.executescript(
                "BEGIN IMMEDIATE; CREATE TABLE stale (id INTEGER); COMMIT;"),
            "cursor": lambda: connection.cursor().execute(insert, ("c",)),
            "execute's cursor": lambda: connection.execute("SELECT 1").execute(insert, ("e",)),
            "inside BEGIN": lambda: (connection.execute("BEGIN IMMEDIATE"), connection.execute(insert, ("d",))),
        }
        for name, write in paths.items():
            with self.subTest(path=name), self.assertRaises(hub.Fenced):
                write()
            self.assertFalse(connection.in_transaction)
        self.assertEqual(connection.execute("SELECT count(*) FROM state WHERE kind = 'x'").fetchone()[0], 0)

    def test_no_cursor_or_authorizer_leaves_the_fence(self):
        """Every cursor the connection hands out is fenced; nothing replaces the fence's authorizer."""
        self.name_hub(HUB_NAME)
        self.node()
        connection = self.open()
        handed = {
            "cursor": connection.cursor(),
            "execute": connection.execute("SELECT 1"),
            "executemany": connection.executemany("DELETE FROM state WHERE id = ?", []),
            "executescript": connection.executescript("SELECT 1"),
        }
        for name, cursor in handed.items():
            with self.subTest(method=name):
                self.assertIsInstance(cursor, hub._FencedCursor)
        with self.assertRaises(SdDbError):
            connection.cursor(sqlite3.Cursor)
        for authorizer in (None, lambda *args: sqlite3.SQLITE_OK):
            with self.subTest(authorizer=authorizer), self.assertRaises(SdDbError):
                connection.set_authorizer(authorizer)

    def test_reads_and_temp_tables_stay_allowed(self):
        """The old hub's dashboard still shows its stale records; a read transaction commits."""
        self.name_hub(HUB_NAME)
        self.node(OLD_HUB)
        connection = self.open()
        with database.transaction(connection):
            connection.execute("SELECT count(*) FROM item").fetchone()
        connection.execute("CREATE TEMP TABLE scratch (id INTEGER)")
        connection.execute("INSERT INTO scratch VALUES (1)")
        self.assertEqual(connection.execute("SELECT count(*) FROM scratch").fetchone()[0], 1)

    def test_another_database_is_not_the_record(self):
        """A fixture, the Jev meter's file or a backup target holds no hub role."""
        other = self.root / "other.db"
        initialise(other)
        connection = database.connect(other)
        self.addCleanup(connection.close)
        writes.set_setting(connection, hub.HUB_NAME, HUB_NAME)
        self.node(OLD_HUB)
        create_item(connection, kind="work", title="not the record")
        self.assertEqual(self.asked(), [])

    def test_writes_inside_a_transaction_share_one_probe_with_their_commit(self):
        """The cost bound: the opening asks; the writes and the commit share its answer (pack sd:7042)."""
        self.name_hub(HUB_NAME)
        self.node()
        connection = self.open()
        with database.transaction(connection):
            for title in ("one", "two", "three"):
                create_item(connection, kind="work", title=title)
        self.assertEqual(len(self.asked()), 1, "one for the opening, none for the writes or the commit")
        self.assertEqual(hub.FENCE_SECONDS, 5.0)

    def test_hub_json_beside_the_database_still_refuses_an_open_writer(self):
        """Demote 1 cut short: the fence reads the file itself, so `HubConflict` does not read as unknown."""
        self.name_hub(HUB_NAME)
        self.node(OLD_HUB)
        connection = self.open()
        config = hub.config_path()
        config.parent.mkdir(parents=True)
        config.write_text('{"hub": "hub.example.test", "port": 8769}', encoding="utf-8")
        with self.assertRaises(hub.Fenced):
            create_item(connection, kind="work", title="stale")
        with self.assertRaises(hub.HubConflict):
            database.connect()


class TheCommitBoundaries(FenceCase):
    """Every boundary that makes a write persist asks the fence.

    Lead review of 59dbc8cf0 had each ask afresh; the operator's ruling on
    pack sd:7042 (note #39256) lets one trust an answer up to
    `FENCE_SECONDS` old, as a tailnet frame does, since a fresh ask held
    SQLite's write lock for a Tailscale call. Each
    test renames the node and lets that window pass (`later`); the boundary
    must ask Tailscale again and refuse.
    """

    def begun(self, opening: str) -> sqlite3.Connection:
        self.node()
        self.name_hub(HUB_NAME)
        connection = self.open()
        connection.execute(opening)
        connection.execute("INSERT INTO state (kind, key, body, timestamp) VALUES ('x', 'held', '{}', '2026')")
        self.node(OLD_HUB)
        self.later()
        return connection

    def rows(self) -> int:
        connection = database.connect(write=False)
        try:
            return connection.execute("SELECT count(*) FROM state WHERE kind = 'x'").fetchone()[0]
        finally:
            connection.close()

    def test_a_commit_past_the_window_asks_again(self):
        endings = {
            "COMMIT": ("BEGIN IMMEDIATE", lambda connection: connection.execute("COMMIT")),
            "END": ("BEGIN", lambda connection: connection.execute("END")),
            "commit()": ("BEGIN IMMEDIATE", lambda connection: connection.commit()),
            "with": ("BEGIN IMMEDIATE", lambda connection: connection.__exit__(None, None, None)),
            "outer RELEASE": ("SAVEPOINT outer", lambda connection: connection.execute("RELEASE outer")),
            "executescript COMMIT": ("BEGIN IMMEDIATE", lambda connection: connection.executescript("COMMIT")),
        }
        for name, (opening, end) in endings.items():
            with self.subTest(end=name):
                hub._answers.clear()
                connection = self.begun(opening)
                with self.assertRaises(hub.Fenced):
                    end(connection)
                self.assertFalse(connection.in_transaction)
                self.assertEqual(self.rows(), 0)

    def test_a_library_transaction_past_the_window_rolls_back(self):
        self.name_hub(HUB_NAME)
        self.node()
        connection = self.open()
        with self.assertRaises(hub.Fenced):
            with database.transaction(connection):
                create_item(connection, kind="work", title="held across the rename")
                self.node(OLD_HUB)
                self.later()
        self.assertEqual(self.items(), 0)

    def test_an_autocommit_write_past_the_window_asks_again(self):
        """A write outside a transaction commits when it ends, so it is a boundary too."""
        self.name_hub(HUB_NAME)
        self.node()
        connection = self.open()
        insert = "INSERT INTO state (kind, key, body, timestamp) VALUES ('x', ?, '{}', '2026')"
        connection.execute(insert, ("live",))
        self.node(OLD_HUB)
        self.later()
        writes_ = {
            "execute": lambda: connection.execute(insert, ("a",)),
            "executemany": lambda: connection.executemany(insert, [("b",)]),
            "cursor": lambda: connection.cursor().execute(insert, ("c",)),
            "executescript": lambda: connection.executescript(
                "BEGIN; INSERT INTO state (kind, key, body, timestamp) VALUES ('x', 'd', '{}', '2026'); COMMIT;"),
        }
        for name, write in writes_.items():
            with self.subTest(path=name), self.assertRaises(hub.Fenced):
                write()
        self.assertEqual(self.rows(), 1, "only the write before the rename")

    def test_an_autocommit_batch_spanning_the_rename_persists_nothing(self):
        """Lead review of f349b44ce: SQLite authorizes `executemany` once, then commits each set."""
        insert = "INSERT INTO state (kind, key, body, timestamp) VALUES ('x', ?, '{}', '2026')"

        def sets():
            yield ("a",)
            self.node(OLD_HUB)
            self.later()
            yield ("b",)

        batches = {
            "connection": lambda connection: connection.executemany(insert, sets()),
            "cursor": lambda connection: connection.cursor().executemany(insert, sets()),
            # Lead review of 731a41b3f: the cursor each convenience method returns, reused.
            "execute's cursor": lambda connection: connection.execute("SELECT 1").executemany(insert, sets()),
            "executemany's cursor": lambda connection: connection.executemany(insert, []).executemany(insert, sets()),
            "executescript's cursor": lambda connection: connection.executescript("SELECT 1").executemany(
                insert, sets()),
        }
        for name, batch in batches.items():
            with self.subTest(wrapper=name):
                hub._answers.clear()
                self.node()
                self.name_hub(HUB_NAME)
                connection = self.open()
                with self.assertRaises(hub.Fenced):
                    batch(connection)
                self.assertFalse(connection.in_transaction)
                self.assertEqual(self.rows(), 0)

    def test_an_autocommit_batch_on_the_live_hub_writes_every_set(self):
        self.node()
        self.name_hub(HUB_NAME)
        connection = self.open()
        insert = "INSERT INTO state (kind, key, body, timestamp) VALUES ('x', ?, '{}', '2026')"
        connection.executemany(insert, [("a",), ("b",)])
        connection.cursor().executemany(insert, [("c",)])
        self.assertFalse(connection.in_transaction)
        self.assertEqual(self.rows(), 3)

    def test_a_batch_inside_a_transaction_commits_with_it(self):
        self.node()
        self.name_hub(HUB_NAME)
        connection = self.open()
        insert = "INSERT INTO state (kind, key, body, timestamp) VALUES ('x', ?, '{}', '2026')"
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(insert, [("a",), ("b",)])
        self.assertTrue(connection.in_transaction)
        connection.execute("ROLLBACK")
        self.assertEqual(self.rows(), 0)

    def test_a_script_write_outside_a_transaction_refuses(self):
        """sqlite3 commits an open transaction before a script, so nothing outside can hold its writes."""
        self.node()
        self.name_hub(HUB_NAME)
        connection = self.open()
        script = "".join(f"INSERT INTO state (kind, key, body, timestamp) VALUES ('x', '{key}', '{{}}', '2026');"
                         for key in ("a", "b"))
        with self.assertRaises(SdDbError) as refused:
            connection.executescript(script)
        self.assertIn("BEGIN IMMEDIATE ... COMMIT inside the script", str(refused.exception))
        self.assertEqual(self.rows(), 0)
        connection.executescript(f"BEGIN IMMEDIATE; {script} COMMIT;")
        self.assertEqual(self.rows(), 2)

    def test_a_rename_between_prepare_and_commit_persists_nothing(self):
        """Lane review of 7953e175d: the role flips while the write runs, after its prepare asked."""
        insert = "INSERT INTO state (kind, key, body, timestamp) VALUES ('x', flip(), '{}', '2026')"
        writes_ = {
            "execute": lambda connection: connection.execute(insert),
            "cursor": lambda connection: connection.cursor().execute(insert),
            "executemany": lambda connection: connection.executemany(insert, [()]),
            "executescript": lambda connection: connection.executescript(f"BEGIN; {insert}; COMMIT;"),
        }

        def flip() -> str:
            self.node(OLD_HUB)
            self.later()
            return "flipped"

        for name, write in writes_.items():
            with self.subTest(path=name):
                hub._answers.clear()
                self.node()
                self.name_hub(HUB_NAME)
                connection = self.open()
                connection.create_function("flip", 0, flip)
                with self.assertRaises(hub.Fenced):
                    write(connection)
                self.assertFalse(connection.in_transaction)
                self.assertEqual(self.rows(), 0)

    def test_a_writable_blob_outside_a_transaction_refuses(self):
        """A blob write passes no authorizer and commits on each write, so it needs a transaction."""
        self.node()
        self.name_hub(HUB_NAME)
        connection = self.open()
        connection.execute("INSERT INTO state (kind, key, body, timestamp) VALUES ('x', 'b', 'abcd', '2026')")
        row = connection.execute("SELECT max(id) FROM state").fetchone()[0]
        with self.assertRaises(SdDbError):
            connection.blobopen("state", "body", row)
        with connection.blobopen("state", "body", row, readonly=True) as blob:
            self.assertEqual(blob.read(), b"abcd")

    def test_a_blob_write_commits_only_after_an_ask(self):
        self.node()
        self.name_hub(HUB_NAME)
        connection = self.open()
        connection.execute("INSERT INTO state (kind, key, body, timestamp) VALUES ('x', 'b', 'abcd', '2026')")
        row = connection.execute("SELECT max(id) FROM state").fetchone()[0]
        connection.execute("BEGIN IMMEDIATE")
        with connection.blobopen("state", "body", row) as blob:
            blob.write(b"wxyz")
        self.node(OLD_HUB)
        self.later()
        with self.assertRaises(hub.Fenced):
            connection.execute("COMMIT")
        reader = database.connect(write=False)
        self.addCleanup(reader.close)
        self.assertEqual(reader.execute("SELECT body FROM state WHERE id = ?", (row,)).fetchone()[0], "abcd")

    def test_a_failed_nested_begin_keeps_the_commit_asking(self):
        """Lead review of f716079a9: a BEGIN that SQLite refuses inside a transaction ends nothing."""
        endings = {
            "COMMIT": ("BEGIN IMMEDIATE", "COMMIT"),
            "outer RELEASE": ("SAVEPOINT outer", "RELEASE outer"),
        }
        for name, (opening, end) in endings.items():
            with self.subTest(end=name):
                hub._answers.clear()
                connection = self.begun(opening)
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("BEGIN")
                self.assertTrue(connection.in_transaction)
                with self.assertRaises(hub.Fenced):
                    connection.execute(end)
                self.assertFalse(connection.in_transaction)
                self.assertEqual(self.rows(), 0)

    def test_a_read_only_transaction_on_a_fenced_node_commits_and_costs_one_probe_per_window(self):
        """Lead ruling on f716079a9: reads on an old hub keep working. Their commit costs no probe;
        the opening asks once per half window, before the write lock (pack sd:7042, note #39256)."""
        self.node()
        self.name_hub(HUB_NAME)
        self.node(OLD_HUB)
        connection = self.open()
        before = len(self.asked())
        reads = {
            "COMMIT": ("BEGIN", "COMMIT"),
            "outer RELEASE": ("SAVEPOINT outer", "RELEASE outer"),
        }
        for name, (opening, end) in reads.items():
            with self.subTest(end=name):
                connection.execute(opening)
                self.assertEqual(connection.execute("SELECT count(*) FROM item").fetchone()[0], 0)
                connection.execute(end)
                self.assertFalse(connection.in_transaction)
        with database.transaction(connection):
            connection.execute("SELECT count(*) FROM state").fetchone()
        self.assertEqual(len(self.asked()), before + 1)

    def test_an_inner_release_does_not_probe(self):
        """A nested savepoint's RELEASE commits nothing, so it costs no probe."""
        self.name_hub(HUB_NAME)
        self.node()
        connection = self.open()
        connection.execute("BEGIN IMMEDIATE")
        for title in ("one", "two"):
            create_item(connection, kind="work", title=title)
        self.assertEqual(len(self.asked()), 1)
        self.later()
        connection.execute("COMMIT")
        self.assertEqual(len(self.asked()), 2)
        self.assertEqual(self.items(), 2)


class TheAutocommitWindow(FenceCase):
    """Lane reviews of 0bff04f7d and 7953e175d: no write commits where the authorizer cannot see it.

    A write outside a transaction runs inside BEGIN IMMEDIATE ... COMMIT, so
    it has committed when its call returns, through a COMMIT that asked.
    """

    def test_an_autocommit_write_has_committed_when_its_call_returns(self):
        self.node()
        self.name_hub(HUB_NAME)
        connection = self.open()
        reader = database.connect(write=False)
        self.addCleanup(reader.close)
        insert = "INSERT INTO state (kind, key, body, timestamp) VALUES ('x', ?, '{}', '2026')"
        writes_ = {
            "execute": lambda: connection.execute(insert, ("a",)),
            "cursor": lambda: connection.cursor().execute(insert, ("b",)),
            "executemany": lambda: connection.executemany(insert, [("c",), ("d",)]),
            "executescript": lambda: connection.executescript(
                "BEGIN; INSERT INTO state (kind, key, body, timestamp) VALUES ('x', 'e', '{}', '2026'); COMMIT;"),
        }
        seen = 0
        for name, write in writes_.items():
            with self.subTest(path=name):
                write()
                count = reader.execute("SELECT count(*) FROM state WHERE kind = 'x'").fetchone()[0]
                self.assertGreater(count, seen, "committed before the call returned")
                seen = count

    def test_an_autocommit_returning_write_fails_closed(self):
        """A RETURNING write holds its statement open, so its wrapping COMMIT cannot run: nothing persists."""
        self.node()
        self.name_hub(HUB_NAME)
        connection = self.open()
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute("INSERT INTO state (kind, key, body, timestamp) VALUES ('x', 'r', '{}', '2026') "
                               "RETURNING id")
        self.assertFalse(connection.in_transaction)
        self.assertEqual(self.rows(), 0)

    def rows(self) -> int:
        reader = database.connect(write=False)
        try:
            return reader.execute("SELECT count(*) FROM state WHERE kind = 'x'").fetchone()[0]
        finally:
            reader.close()


class ThePragmaWrites(FenceCase):
    """pack sd:6328: a PRAGMA that changes the file is a write; one that reads or sets this connection is not.

    SQLite authorizes a pragma as SQLITE_PRAGMA, its value as `arg2`.
    """

    def header(self) -> tuple:
        reader = database.connect(write=False)
        try:
            return tuple(reader.execute(f"PRAGMA {name}").fetchone()[0]
                         for name in ("user_version", "application_id", "journal_mode"))
        finally:
            reader.close()

    def test_a_pragma_write_on_a_fenced_hub_refuses(self):
        self.name_hub(HUB_NAME)
        self.node(OLD_HUB)
        connection = self.open()
        before = self.header()
        writes_ = ("PRAGMA user_version = 7", "PRAGMA main.application_id = 7", "pragma JOURNAL_MODE = delete",
                   "PRAGMA wal_checkpoint(TRUNCATE)", "PRAGMA wal_checkpoint")
        paths: dict[str, Callable[[], object]] = {
            statement: functools.partial(connection.execute, statement) for statement in writes_}
        paths["executescript"] = lambda: connection.executescript("BEGIN IMMEDIATE; PRAGMA user_version = 8; COMMIT;")
        paths["inside BEGIN"] = lambda: (connection.execute("BEGIN IMMEDIATE"),
                                         connection.execute("PRAGMA user_version = 9"))
        for name, write in paths.items():
            with self.subTest(path=name), self.assertRaises(hub.Fenced):
                write()
            self.assertFalse(connection.in_transaction)
        self.assertEqual(self.header(), before)
        with self.assertRaises(SdDbError):
            connection.executescript("PRAGMA user_version = 8;")
        self.assertEqual(self.header(), before)

    def test_a_pragma_write_in_a_transaction_across_the_rename_rolls_back(self):
        self.name_hub(HUB_NAME)
        self.node()
        connection = self.open()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA application_id = 7")
        self.node(OLD_HUB)
        self.later()
        with self.assertRaises(hub.Fenced):
            connection.execute("COMMIT")
        self.assertFalse(connection.in_transaction)
        self.assertEqual(self.header()[1], 0)

    def test_pragma_reads_and_connection_settings_stay_allowed_with_no_probe(self):
        self.name_hub(HUB_NAME)
        self.node(OLD_HUB)
        connection = self.open()
        before = len(self.asked())
        reads = ("PRAGMA user_version", "PRAGMA journal_mode", "PRAGMA table_info(item)", "PRAGMA index_list(item)",
                 "PRAGMA foreign_key_check(item)", "PRAGMA integrity_check(1)", "PRAGMA busy_timeout = 100",
                 "PRAGMA foreign_keys = ON", "PRAGMA temp.user_version = 3")
        for statement in reads:
            with self.subTest(statement=statement):
                connection.execute(statement).fetchall()
        self.assertEqual(len(self.asked()), before)

    def test_a_pragma_write_on_the_live_hub_runs_outside_a_transaction(self):
        """`journal_mode` and `wal_checkpoint` cannot run inside one, so the fence asks at prepare instead."""
        self.name_hub(HUB_NAME)
        self.node()
        connection = self.open()
        connection.execute("PRAGMA application_id = 7")
        self.assertEqual(connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0], "delete")
        self.assertEqual(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)
        self.assertFalse(connection.in_transaction)
        self.assertEqual(self.header()[1:], (7, "wal"))

    #: SQLite's pragmas that set this connection, or this process, and write
    #: nothing to the file, from its pragma list; each with a value to set
    #: when reading it answers nothing.
    CONNECTION = {
        "analysis_limit": None, "automatic_index": None, "busy_timeout": None, "cache_size": None,
        "cache_spill": None, "case_sensitive_like": "OFF", "cell_size_check": None, "checkpoint_fullfsync": None,
        "count_changes": None, "defer_foreign_keys": None, "empty_result_callbacks": None, "foreign_keys": None,
        "full_column_names": None, "fullfsync": None, "hard_heap_limit": None, "ignore_check_constraints": None,
        "journal_size_limit": None, "legacy_alter_table": None, "locking_mode": None, "max_page_count": None,
        "mmap_size": "0", "query_only": None, "read_uncommitted": None, "recursive_triggers": None,
        "reverse_unordered_selects": None, "secure_delete": None, "short_column_names": None,
        "soft_heap_limit": None, "synchronous": None, "temp_store": None, "temp_store_directory": "''",
        "threads": None, "trusted_schema": None, "wal_autocheckpoint": None, "writable_schema": None,
    }

    def settings(self, connection: sqlite3.Connection) -> dict[str, str]:
        """Each connection pragma set to the value it holds, so setting it changes nothing."""
        found = {}
        for name, default in self.CONNECTION.items():
            row = connection.execute(f"PRAGMA {name}").fetchone()
            found[name] = str(default) if row is None else repr(row[0])
        return found

    def test_connection_pragmas_set_outside_a_transaction_with_no_probe(self):
        """Lead review of e1f565757: `synchronous` fails inside a transaction, so none may enter the wrapper."""
        for role in ("holder", "elsewhere"):
            hub._answers.clear()
            self.name_hub(HUB_NAME)
            self.node(OLD_HUB if role == "elsewhere" else HUB_NAME + ".")
            connection = self.open()
            before = len(self.asked())
            for name, value in self.settings(connection).items():
                with self.subTest(role=role, pragma=name):
                    connection.execute(f"PRAGMA {name} = {value}").fetchall()
                    self.assertFalse(connection.in_transaction)
            with self.subTest(role=role, probes=True):
                self.assertEqual(len(self.asked()), before)

    def test_the_open_paths_pragmas_run_on_an_armed_connection(self):
        """`database.connect` arms the fence after its own pragmas; the library's later ones still run."""
        self.name_hub(HUB_NAME)
        self.node(OLD_HUB)
        connection = self.open()
        self.assertIsInstance(connection, hub.FencedConnection)
        for statement in ("PRAGMA busy_timeout = 100", "PRAGMA foreign_keys = ON", "PRAGMA synchronous = NORMAL",
                          "PRAGMA query_only = ON", "PRAGMA query_only = OFF", "PRAGMA journal_mode = WAL"):
            with self.subTest(statement=statement):
                if statement.endswith("journal_mode = WAL"):
                    # It may change the file, so on a fenced node it asks and refuses.
                    with self.assertRaises(hub.Fenced):
                        connection.execute(statement)
                else:
                    connection.execute(statement).fetchall()

    def test_a_migration_with_writable_schema_runs_on_the_fenced_live_hub(self):
        """009, 018, 031 and 032 set `writable_schema` inside the script's own transaction."""
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        # Version 8 has no `setting` kind to name the hub, so the fence reads
        # `off`; the migration still runs on the armed connection.
        make_store(self.path, version=8)
        self.node()
        with mock.patch.object(hub.FencedConnection, "arm", autospec=True,
                               side_effect=hub.FencedConnection.arm) as arm:
            applied = migrate(self.path)
        arm.assert_called_once()
        self.assertEqual(applied.after, SCHEMA_VERSION)
        self.assertIn(32, applied.applied)

    def free_pages(self) -> int:
        reader = database.connect(write=False)
        try:
            return reader.execute("PRAGMA freelist_count").fetchone()[0]
        finally:
            reader.close()

    def vacuumable(self) -> int:
        """Switch the record to incremental auto-vacuum and leave free pages for `incremental_vacuum`."""
        raw = sqlite3.connect(self.path, isolation_level=None)
        try:
            raw.execute("PRAGMA auto_vacuum = INCREMENTAL")
            raw.execute("VACUUM")
            raw.execute("CREATE TABLE filler (body TEXT)")
            raw.executemany("INSERT INTO filler VALUES (?)", [("x" * 1000,) for _ in range(200)])
            raw.execute("DELETE FROM filler")
        finally:
            raw.close()
        free = self.free_pages()
        self.assertGreater(free, 1)
        return free

    def test_incremental_vacuum_never_runs_on_the_record(self):
        """Lane reviews of cae42071c and 8a5f0f243: its cursor frees pages as it is fetched, past a ROLLBACK.

        The next commit on the connection writes those pages, so no fence
        check before or after it holds; it is refused on every path.
        """
        self.name_hub(HUB_NAME)
        self.node()
        before = self.vacuumable()
        insert = "INSERT INTO state (kind, key, body, timestamp) VALUES ('x', ?, '{}', '2026')"
        openings = {"autocommit": None, "BEGIN": "BEGIN", "BEGIN IMMEDIATE": "BEGIN IMMEDIATE",
                    "SAVEPOINT": "SAVEPOINT outer"}
        for name, opening in openings.items():
            with self.subTest(opening=name):
                connection = self.open()
                cursor = connection.cursor()
                if opening is not None:
                    connection.execute(opening)
                with self.assertRaises(SdDbError) as refused:
                    cursor.execute("PRAGMA incremental_vacuum")
                self.assertIn("incremental_vacuum", str(refused.exception))
                with contextlib.suppress(sqlite3.Error):
                    cursor.fetchall()
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                connection.execute(insert, (name,))
                self.assertEqual(self.free_pages(), before)

    def wal_bytes(self) -> int:
        wal = Path(f"{self.path}-wal")
        return wal.stat().st_size if wal.exists() else -1

    def test_an_operation_no_rollback_undoes_asks_afresh_before_it_runs(self):
        """Lane review of 8a5f0f243: BEGIN and SAVEPOINT open no storage transaction, so these run at once.

        A held `holder` answer would let them run after the rename, and the
        fenced COMMIT that follows cannot undo them.
        """
        insert = "INSERT INTO state (kind, key, body, timestamp) VALUES ('x', ?, '{}', '2026')"
        operations = ("PRAGMA wal_checkpoint", "PRAGMA wal_checkpoint(TRUNCATE)", "PRAGMA journal_mode = DELETE",
                      "PRAGMA journal_mode = OFF", "VACUUM", "VACUUM main")
        for opening in (None, "BEGIN", "SAVEPOINT outer"):
            for operation in operations:
                with self.subTest(opening=opening, operation=operation):
                    hub._answers.clear()
                    self.node()
                    self.name_hub(HUB_NAME)
                    connection = self.open()
                    connection.execute(insert, ("held",))
                    if opening is not None:
                        connection.execute(opening)
                    wal = self.wal_bytes()
                    self.node(OLD_HUB)
                    if operation.startswith("VACUUM") and opening is not None:
                        # SQLite refuses it before it asks the authorizer.
                        with self.assertRaisesRegex(sqlite3.OperationalError, "cannot VACUUM from within"):
                            connection.execute(operation)
                        connection.execute("ROLLBACK")
                    else:
                        with self.assertRaises(hub.Fenced):
                            connection.execute(operation)
                    self.assertFalse(connection.in_transaction)
                    self.assertEqual(self.wal_bytes(), wal)
                    self.assertEqual(self.header()[2], "wal")
                    connection.close()

    def test_vacuum_runs_on_the_live_hub(self):
        self.name_hub(HUB_NAME)
        self.node()
        connection = self.open()
        before = len(self.asked())
        connection.execute("VACUUM")
        self.assertEqual(len(self.asked()), before + 1, "one fresh ask")

    def test_page_size_and_auto_vacuum_are_refused_not_dropped(self):
        """pack sd:6378: BEGIN IMMEDIATE creates the file's header, so the wrapper drops both settings."""
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        self.node()
        connection = database.connect(create=True)
        self.addCleanup(connection.close)
        self.assertIsInstance(connection, hub.FencedConnection)
        for statement in ("PRAGMA page_size = 8192", "PRAGMA auto_vacuum = FULL", "PRAGMA main.page_size = 8192"):
            for opening in (None, "BEGIN", "BEGIN IMMEDIATE"):
                with self.subTest(statement=statement, opening=opening):
                    if opening is not None:
                        connection.execute(opening)
                    try:
                        with self.assertRaisesRegex(SdDbError, "before the database exists"):
                            connection.execute(statement)
                    finally:
                        if connection.in_transaction:
                            connection.execute("ROLLBACK")
        self.assertEqual((connection.execute("PRAGMA page_size").fetchone()[0],
                          connection.execute("PRAGMA auto_vacuum").fetchone()[0]), (4096, 0))
        self.assertEqual(connection.execute("PRAGMA temp.page_size = 8192").fetchall(), [])

class TheLockHold(FenceCase):
    """pack sd:7042: no write waits on another writer's Tailscale call.

    A commit asked `tailscale status --json` afresh while it held SQLite's
    write lock, so under load cron writers saw "database is locked". The
    opening statement now asks, before the lock, when the held answer is
    older than half of `FENCE_SECONDS`; writes and the commit trust an answer
    up to `FENCE_SECONDS` old, never across a clock jump (operator
    ruling, note #39256).
    """

    def frozen(self) -> dict:
        clock = {"monotonic": 1000.0, "time": 2_000_000_000.0}
        self.enterContext(mock.patch.object(hub.time, "monotonic", lambda: clock["monotonic"]))
        self.enterContext(mock.patch.object(hub.time, "time", lambda: clock["time"]))
        return clock

    def test_commits_inside_the_window_share_one_tailscale_call(self):
        self.name_hub(HUB_NAME)
        self.node()
        self.frozen()
        connection = self.open()
        before = len(self.asked())
        for title in ("one", "two", "three", "four", "five"):
            create_item(connection, kind="work", title=title)
        self.assertEqual(self.items(), 5)
        self.assertLessEqual(len(self.asked()) - before, 1, "a commit asked Tailscale again inside the window")

    def slow_tailscale(self, clock: dict, *, slept: float = 0) -> list[str]:
        """`hub._this_name` as a Tailscale slower than `FENCE_SECONDS`, `slept` more on the wall clock.

        Before each call a second connection tries the write lock, waiting for
        nothing; each call notes `free` or the probe's error (pack sd:7120).
        """
        calls: list[str] = []
        name = hub._this_name

        def slow() -> str:
            probe = sqlite3.connect(self.path, timeout=0, isolation_level=None)
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.execute("ROLLBACK")
                calls.append("free")
            except sqlite3.OperationalError as error:
                calls.append(str(error))
            finally:
                probe.close()
            clock["monotonic"] += hub.FENCE_SECONDS + 1
            clock["time"] += hub.FENCE_SECONDS + 1 + slept
            return name()

        self.enterContext(mock.patch.object(hub, "_this_name", slow))
        return calls

    def test_a_slow_tailscale_does_not_hold_the_write_lock(self):
        """A Tailscale call slower than `FENCE_SECONDS` runs before the write lock, and only there.

        Its answer dated from the call's start, so it was stale when it came:
        the transaction's first write asked again under the lock (pack
        sd:7120). The probe replaces a 0.5 s sleep and a wall-clock bound that
        also timed a second writer's own fsync.
        """
        self.name_hub(HUB_NAME)
        self.node()
        calls = self.slow_tailscale(self.frozen())
        create_item(self.open(), kind="work", title="slow")
        self.assertEqual(calls, ["free"], "a Tailscale call ran under the write lock")
        self.assertEqual(self.items(), 1)

    def test_a_slow_failing_tailscale_is_called_once_before_the_lock(self):
        """A failure ages from the setting read before the call, so writes ask again, from `_name_failed` alone."""
        self.name_hub(HUB_NAME)
        self.node(backend="Stopped")
        calls = self.slow_tailscale(self.frozen())
        before = len(self.asked())
        create_item(self.open(), kind="work", title="outage")
        self.assertEqual(calls[0], "free")
        self.assertEqual(len(self.asked()) - before, 1, "Tailscale was called again under the write lock")
        self.assertEqual(self.items(), 1)

    def held_after_a_slow_ask(self, slept: float) -> list[str]:
        """The asks `Held` makes for a call slower than `FENCE_SECONDS`, then a read inside the window."""
        clock = self.frozen()
        held, asked = hub.Held(), []

        def slow() -> str:
            asked.append("slow")
            clock["monotonic"] += hub.FENCE_SECONDS + 1
            clock["time"] += hub.FENCE_SECONDS + 1 + slept
            return "slow"

        held.get(slow, hub.FENCE_SECONDS)
        held.get(lambda: asked.append("again"), hub.FENCE_SECONDS)
        return asked

    def test_an_answer_ages_from_when_it_came(self):
        self.assertEqual(self.held_after_a_slow_ask(0), ["slow"])

    def test_a_sleep_during_the_ask_asks_again(self):
        """The wall clock counts from the call's start, so a sleep inside it is still a jump."""
        self.assertEqual(self.held_after_a_slow_ask(hub.CLOCK_SLACK + 1), ["slow", "again"])

    def set_during_the_call(self, value: str | None) -> None:
        """`hub._this_name`, with `sd.hub_name` set to `value` while Tailscale answers."""
        set_during(self, self.path, value)

    def test_the_hub_name_is_read_again_after_tailscale_answers(self):
        """The answer ages from its oldest input; a name read before the call was older (pack sd:7120)."""
        self.name_hub(HUB_NAME)
        self.node()
        self.set_during_the_call(OLD_HUB)
        self.assertEqual(hub.write_role(self.path).state, "elsewhere")

    def test_a_hub_name_cleared_during_the_call_is_off(self):
        self.name_hub(HUB_NAME)
        self.node()
        self.set_during_the_call(None)
        self.assertEqual(hub.write_role(self.path).state, "off")

    def test_a_held_failure_ages_from_when_it_was_seen(self):
        """A Tailscale failure `_this_name` holds was seen when it was cached, not when it is read again."""
        self.name_hub(HUB_NAME)
        self.node()
        clock = self.frozen()
        hub._name_failed = (clock["monotonic"] - hub.FENCE_SECONDS - 1, hub.tailnet.TailnetError("down"))
        asked = []
        role = hub.role

        def counted(*args, **kwargs):
            asked.append(1)
            return role(*args, **kwargs)

        self.enterContext(mock.patch.object(hub, "role", counted))
        self.assertEqual(hub.write_role(self.path).state, "unknown")
        self.assertEqual(hub.write_role(self.path).state, "unknown")
        self.assertEqual(len(asked), 2, "an answer from a failure older than the window was trusted")

    def test_a_held_answer_is_read_while_another_caller_asks(self):
        """One slow ask stalls only the callers that need a new answer."""
        held = hub.Held()
        self.assertEqual(held.get(lambda: "first", hub.FENCE_SECONDS), "first")
        asking, release = threading.Event(), threading.Event()

        def slow() -> str:
            asking.set()
            release.wait(30)
            return "second"

        asker = threading.Thread(target=held.get, args=(slow, 0))
        asker.start()
        try:
            self.assertTrue(asking.wait(30))
            self.assertEqual(held.get(lambda: "asked again", hub.FENCE_SECONDS), "first")
        finally:
            release.set()
            asker.join(30)
        self.assertEqual(held.get(lambda: "asked again", hub.FENCE_SECONDS), "second")

    def test_a_write_with_a_held_answer_does_not_wait_on_a_slow_fresh_ask(self):
        """Another thread's fresh ask stalls in Tailscale; a write inside the window does not wait.

        Events, not a 0.5 s sleep: the read runs while the ask is known to stall (pack sd:7120).
        """
        self.name_hub(HUB_NAME)
        self.node()
        self.assertEqual(hub.write_role(self.path).state, "holder")
        asking, release = threading.Event(), threading.Event()
        name = hub._this_name

        def stalled() -> str:
            asking.set()
            release.wait(30)
            return name()

        self.enterContext(mock.patch.object(hub, "_this_name", stalled))
        asker = threading.Thread(target=hub.write_role, args=(self.path,), kwargs={"within": 0})
        found: list[hub.Role] = []
        reader = threading.Thread(target=lambda: found.append(hub.write_role(self.path)))
        asker.start()
        try:
            self.assertTrue(asking.wait(30))
            reader.start()
            # A reader that waits on the ask never ends while the ask stalls.
            reader.join(10)
            self.assertFalse(reader.is_alive(), "a held answer waited on another thread's ask")
        finally:
            release.set()
            asker.join(30)
            if reader.ident is not None:
                reader.join(30)
        self.assertEqual([role.state for role in found], ["holder"])

    def test_a_role_loss_refuses_the_first_commit_past_the_window(self):
        self.name_hub(HUB_NAME)
        self.node()
        clock = self.frozen()
        connection = self.open()
        create_item(connection, kind="work", title="live")
        self.node(OLD_HUB)
        clock["monotonic"] += hub.FENCE_SECONDS
        clock["time"] += hub.FENCE_SECONDS
        with self.assertRaises(hub.Fenced):
            create_item(connection, kind="work", title="past the window")
        self.assertEqual(self.items(), 1)

    def test_a_role_loss_inside_a_transaction_refuses_its_commit_past_the_window(self):
        """The opening asked; the rename lands; the commit runs on an answer `FENCE_SECONDS` old."""
        self.name_hub(HUB_NAME)
        self.node()
        clock = self.frozen()
        connection = self.open()
        with self.assertRaises(hub.Fenced):
            with database.transaction(connection):
                create_item(connection, kind="work", title="open across the rename")
                self.node(OLD_HUB)
                clock["monotonic"] += hub.FENCE_SECONDS
                clock["time"] += hub.FENCE_SECONDS
        self.assertFalse(connection.in_transaction)
        self.assertEqual(self.items(), 0)

    def test_a_sleep_inside_the_window_refuses_the_next_commit(self):
        """The wall clock moved an hour and the monotonic one did not: the held answer is not trusted."""
        self.name_hub(HUB_NAME)
        self.node()
        clock = self.frozen()
        connection = self.open()
        create_item(connection, kind="work", title="live")
        self.node(OLD_HUB)
        clock["time"] += 3600
        with self.assertRaises(hub.Fenced):
            create_item(connection, kind="work", title="after the sleep")
        self.assertEqual(self.items(), 1)


class TheRestore(FenceCase):
    def test_restore_writes_before_the_name_is_taken(self):
        """Promote step 5 restores the old hub's record on a machine that does not hold its name yet."""
        self.name_hub(HUB_NAME)
        self.node()
        create_item(self.open(), kind="work", title="kept")
        snapshot = backup(home=self.home)
        promoted = self.root / "promoted"
        self.node("satellite.example.test.")
        self.later()
        with mock.patch.dict(os.environ, {"HOME": str(promoted)}):
            restore(snapshot.directory, home=promoted)
            connection = database.connect()
            self.addCleanup(connection.close)
            self.assertEqual(connection.execute("SELECT count(*) FROM item").fetchone()[0], 1)
            with self.assertRaises(hub.Fenced):
                create_item(connection, kind="work", title="before the rename")
            self.node()
            self.later()
            create_item(connection, kind="work", title="after the rename")


class TheCommand(FenceCase):
    def test_a_note_on_a_fenced_hub_exits_non_zero_naming_the_demotion(self):
        """The design's first acceptance line: `sd task note 1 x` on a test hub whose fence reads `elsewhere`."""
        create_item(self.open(), kind="work", title="one")
        self.name_hub(HUB_NAME)
        self.node(OLD_HUB)
        environment = {**os.environ, "PYTHONPATH": str(support.LIB), "HOME": str(self.home)}
        done = subprocess.run([sys.executable, str(Path(support.LIB).parent / "bin" / "sd"), "task", "note", "1", "x"],
                              capture_output=True, text=True, env=environment, timeout=120)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("1. copy ~/.config/sd/hub.json from any satellite", done.stderr)
        self.assertIn(f"sd commands then reach the hub, {HUB_NAME}", done.stderr)
