"""The schema inventory and the two version refusals."""

import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import database, paths
from sd_db import schema as schema_module
from sd_db.database import connect, schema_version, set_schema_version, tables
from sd_db.errors import SchemaTooNew, SchemaTooOld
from sd_db.migrate import initialise, migrate
from sd_db.schema import SCHEMA_VERSION, TABLES

from . import support

PACKAGE_ROOT = support.LIB


class SchemaCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".local/share/sd").mkdir(parents=True)
        self.path = self.home / ".local/share/sd/sd.db"

    def migrate_through(self, version):
        """Migrate to `version` and no further. A reverse test undoes its own
        file only, so a later migration that adds a column would run twice."""
        through = [m for m in schema_module.migrations() if m[0] <= version]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            return migrate(self.path)


class TheTables(SchemaCase):
    def test_declared_tables_and_no_other(self):
        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(sorted(tables(connection)), sorted(TABLES))
        self.assertEqual(len(TABLES), 19)

    def test_report_is_an_item_kind_not_a_table_and_dep_is_neither(self):
        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertNotIn("report", tables(connection))
        self.assertNotIn("dep", tables(connection))
        connection.execute(
            "INSERT INTO item (kind, title, status, created_at, updated_at) "
            "VALUES ('report', 't', 'planning', '', '')"
        )
        with self.assertRaises(sqlite3.IntegrityError):  # 032 (sd:3510)
            connection.execute(
                "INSERT INTO item (kind, title, status, created_at, updated_at) "
                "VALUES ('dep', 't', 'planning', '', '')"
            )

    def test_the_library_inserts_into_no_table_the_document_does_not_name(self):
        """Criterion 1's grep, run against the library rather than by hand."""
        source = subprocess.run(
            # `--untracked` before the pattern: after it, git reads it as one.
            ["git", "grep", "-hIE", "--untracked",
             "INSERT (OR IGNORE )?INTO [a-z_]+", "--", "sd_db"],
            cwd=PACKAGE_ROOT, capture_output=True, text=True,
        ).stdout
        # An SQL comment is a hand-run reverse, not a library write: 025's
        # rebuilds `repo` through a scratch table (sd:3217).
        source = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("--"))
        # `main.`: the hub names its outcome table's schema (`sd_db.serve`).
        written = set(re.findall(r"INSERT (?:OR IGNORE )?INTO (?:main\.)?([a-z0-9_]+)", source))
        # A migration's guard refuses through a temporary table it drops (031 on).
        written = {name for name in written if not re.fullmatch(r"migration_\d+_guard", name)}
        self.assertTrue(written, "the grep found no inserts at all")
        self.assertEqual(written - set(TABLES), set())

    def test_no_schema_version_table(self):
        """Version bookkeeping does not need an extra table."""
        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertNotIn("schema_version", tables(connection))
        self.assertEqual(schema_version(connection), SCHEMA_VERSION)


class TheMigration(SchemaCase):
    def test_running_it_twice_is_idempotent(self):
        first = migrate(self.path)
        second = migrate(self.path)
        # Every file, not a hard-coded one: the list grows with each
        # migration, and a test naming `[1]` fails on the commit that adds
        # the second for a reason that has nothing to do with idempotency.
        self.assertEqual(first.applied, list(range(1, SCHEMA_VERSION + 1)))
        self.assertEqual(second.applied, [])
        self.assertEqual(second.before, second.after)

    def test_opening_alone_applies_nothing(self):
        raw = sqlite3.connect(self.path)
        raw.close()
        connection = connect(self.path, write=False)
        self.addCleanup(connection.close)
        self.assertEqual(schema_version(connection), 0)
        self.assertEqual(tables(connection), [])

    def test_a_migration_file_that_opens_its_own_transaction_is_refused(self):
        self.assertTrue(schema_module.migrations())
        with tempfile.TemporaryDirectory() as other:
            # One file per version, so `migrations()` sees a complete run
            # ending at SCHEMA_VERSION and gets as far as reading them. Only
            # the last one opens a transaction.
            for version in range(1, SCHEMA_VERSION + 1):
                body = (
                    "BEGIN;\nCREATE TABLE x (a);\nCOMMIT;\n"
                    if version == SCHEMA_VERSION
                    else "CREATE TABLE placeholder_%d (a);\n" % version
                )
                (Path(other) / f"{version:03d}_bad.sql").write_text(body, encoding="utf-8")
            original = schema_module.SCHEMA_DIR
            schema_module.SCHEMA_DIR = Path(other)
            self.addCleanup(setattr, schema_module, "SCHEMA_DIR", original)
            with self.assertRaises(ValueError) as raised:
                migrate(self.path)
            self.assertIn("opens its own transaction", str(raised.exception))

    def test_the_files_are_numbered_without_a_gap(self):
        versions = [version for version, _path in schema_module.migrations()]
        self.assertEqual(versions, list(range(1, SCHEMA_VERSION + 1)))

    def test_a_database_at_version_one_migrates_up_to_the_current_version(self):
        """The upgrade path, not just the fresh install.

        `initialise` applies every file at once, so a suite that only ever
        creates fresh databases never runs migration 2 against a database
        that already has migration 1's shape -- which is the only shape it
        will ever meet on a real machine.
        """
        connection = connect(self.path, create=True, write=True)
        try:
            first = schema_module.migrations()[0][1]
            connection.executescript(
                f"BEGIN;\n{first.read_text(encoding='utf-8')}\n"
                f"PRAGMA user_version = 1;\nCOMMIT;"
            )
        finally:
            connection.close()
        result = migrate(self.path)
        self.assertEqual(result.before, 1)
        self.assertEqual(result.after, SCHEMA_VERSION)
        self.assertEqual(result.applied, list(range(2, SCHEMA_VERSION + 1)))


class TheSourceCommitColumn(SchemaCase):
    """Migration 2. An item can live on a branch the default has never seen,
    and the commit it was read from is what makes that row recoverable."""

    def setUp(self):
        super().setUp()
        initialise(self.path)

    def columns(self):
        connection = connect(self.path, write=False)
        try:
            return {row[1] for row in connection.execute("PRAGMA table_info(item)")}
        finally:
            connection.close()

    def test_item_carries_source_commit(self):
        self.assertIn("source_commit", self.columns())

    def test_no_table_was_added_with_it(self):
        connection = connect(self.path, write=False)
        try:
            found = set(tables(connection))
        finally:
            connection.close()
        self.assertEqual(found, set(TABLES))


class TheShadowTrackerKey(SchemaCase):
    """Migration 8. The key becomes `(tracker, url)`, so a real machine's rows
    have to survive an index swap and a second tracker has to be able to write
    a url the first one already holds."""

    def test_rows_survive_and_a_second_tracker_may_hold_the_same_url(self):
        # Built on the version-7 shape, the only one this migration will meet
        # on a real machine, and seeded on that same connection because
        # `connect` refuses to reopen a database older than the library.
        connection = connect(self.path, create=True, write=True)
        try:
            for number, path in schema_module.migrations():
                if number > 7:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {number};\nCOMMIT;"
                )
            connection.execute(
                "INSERT INTO shadow (id, tracker, repo, url, first_seen, last_seen) "
                "VALUES (5, 'github', 'o/r', 'https://github.com/o/r/issues/1', 't1', 't2')")
            # The shape being migrated away from: one url, one row, whoever.
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO shadow (tracker, url, first_seen, last_seen) "
                    "VALUES ('jira', 'https://github.com/o/r/issues/1', 't3', 't3')")
        finally:
            connection.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (7, list(range(8, SCHEMA_VERSION + 1))))
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(
            [tuple(row) for row in connection.execute(
                "SELECT id, tracker, repo, url FROM shadow")],
            [(5, "github", "o/r", "https://github.com/o/r/issues/1")])
        self.assertEqual(
            sorted(row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'shadow'")),
            ["shadow_by_tracker_url"])
        # The other tracker's row is its own, and a repeat of either is not.
        connection.execute(
            "INSERT INTO shadow (tracker, url, first_seen, last_seen) "
            "VALUES ('jira', 'https://github.com/o/r/issues/1', 't3', 't3')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO shadow (tracker, url, first_seen, last_seen) "
                "VALUES ('jira', 'https://github.com/o/r/issues/1', 't4', 't4')")

    def test_the_collector_no_longer_takes_another_tracker_s_row(self):
        """The defect end to end, through the writer the collector calls."""
        from sd_db.writes import upsert_shadow

        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        url = "https://github.com/o/r/issues/1"
        upsert_shadow(connection, tracker="github", url=url, title="from github")
        upsert_shadow(connection, tracker="jira", url=url, title="from jira")
        self.assertEqual(
            [tuple(row) for row in connection.execute(
                "SELECT tracker, title FROM shadow ORDER BY tracker")],
            [("github", "from github"), ("jira", "from jira")])


class TheStateCheckKind(SchemaCase):
    """Migration 7. `check` joins the `state` kinds by rebuilding the table,
    so what a real machine's database holds has to come through: every row
    with its id, both indexes, and the refusal of a kind nobody declared."""

    def test_rows_survive_the_rebuild_and_the_new_kind_is_accepted(self):
        connection = connect(self.path, create=True, write=True)
        try:
            for version, path in schema_module.migrations():
                if version > 6:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;"
                )
            connection.execute("INSERT INTO state (id, kind, key, timestamp, body) VALUES (7, 'heartbeat', 'runner', 't1', '{}')")
            connection.execute("INSERT INTO state (id, kind, key, timestamp, body, resolved_at) "
                               "VALUES (9, 'restore', '2026-09-01', 't2', '{}', 't3')")
        finally:
            connection.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (6, list(range(7, SCHEMA_VERSION + 1))))
        connection = connect(self.path)
        self.addCleanup(connection.close)
        rows = [tuple(row) for row in connection.execute(
            "SELECT id, kind, key, timestamp, body, resolved_at FROM state ORDER BY id")]
        self.assertEqual(rows, [(7, "heartbeat", "runner", "t1", "{}", None),
                                (9, "restore", "2026-09-01", "t2", "{}", "t3")])
        indexes = sorted(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'state'"))
        # `state_by_kind_key` is migration 13's, applied on the way up.
        self.assertEqual(indexes, ["runner_check", "runner_heartbeat", "state_by_kind", "state_by_kind_key"])
        connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('check', 'run-1', 't4', '{}')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('check', 'run-1', 't5', '{}')")
        # 031 dropped the kind CHECK; `TheStateKindList` holds the refusal (sd:3506).
        # The heartbeat's one-row-per-key index came back with the table.
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('heartbeat', 'runner', 't7', '{}')")

    def test_the_kinds_the_library_names_are_the_kinds_the_schema_accepts(self):
        from sd_db.writes import STATE_KINDS
        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        for kind in STATE_KINDS:
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES (?, ?, 't', '{}')", (kind, f"k-{kind}"))
        self.assertIn("check", STATE_KINDS)
        self.assertIn("snooze", STATE_KINDS)
        self.assertIn("view", STATE_KINDS)


class TheLatestStateByKindAndKey(SchemaCase):
    """Migration 13 (sd:1433). The latest row for one kind and key is read
    from an index, not found by scanning the kind and sorting it. Without
    it the dashboard's Today page took 1.5 s on the live database."""

    LATEST = "SELECT id, body FROM state WHERE kind = ? AND key = ? ORDER BY id DESC LIMIT 1"

    def plan(self, sql, parameters):
        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        return " | ".join(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters))

    def test_the_lookup_uses_the_index_and_sorts_nothing(self):
        for kind in ("checkpoint", "heartbeat"):
            plan = self.plan(self.LATEST, (kind, "contribution:item:1"))
            self.assertIn("USING INDEX state_by_kind_key", plan)
            self.assertNotIn("TEMP B-TREE", plan)

    def test_reads_ordered_by_time_still_use_state_by_kind(self):
        # Why `state_by_kind` was kept: `unresolved_state` orders one kind by
        # timestamp, and the new index cannot do that without a sort.
        plan = self.plan("SELECT * FROM state WHERE kind = ? AND resolved_at IS NULL ORDER BY timestamp",
                         ("restore",))
        self.assertIn("USING INDEX state_by_kind ", plan + " ")
        self.assertNotIn("TEMP B-TREE", plan)


class TheKindsForPersonalAndFollowupWork(SchemaCase):
    """Migration 9. Four kinds join `item.kind`, and unlike migration 7 this
    table cannot be rebuilt: four foreign keys reference `item(id)`, two of
    them without an `ON DELETE` clause, so dropping the old copy is refused
    rather than cascaded, and `PRAGMA foreign_keys` cannot be turned off inside
    the transaction `migrate` wraps every file in. The CHECK text is therefore
    edited in place, which makes these the things to prove: nothing was lost,
    nothing that earlier migrations appended was dropped, the indexes are still
    there because no rebuild happened, and an undeclared kind is still refused.
    """

    #: Every kind this migration adds. `idea` is deliberately not here: it is
    #: the writing pack's article ladder, keyed on by `sd_db/writing.py` and by
    #: the dashboard's writing controls, so a personal or work idea filed as
    #: `idea` would enter the publishing pipeline.
    ADDED = ("personal", "followup", "work-idea", "personal-idea")

    def _at_version_eight(self):
        """A database holding item rows and a row in each table that
        references them, so the migration is measured against fan-in and not
        against an empty table."""
        connection = connect(self.path, create=True, write=True)
        try:
            for version, path in schema_module.migrations():
                if version > 8:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;"
                )
            connection.execute("INSERT INTO repo (path, created_at, updated_at) VALUES ('/repo', 't', 't')")
            connection.execute("INSERT INTO item (id, kind, repo, title, status, created_at, updated_at) "
                               "VALUES (4, 'work', '/repo', 'Existing work', 'planning', 't', 't')")
            connection.execute("INSERT INTO note (id, item, timestamp, kind, body) "
                               "VALUES (5, 4, 't', 'comment', 'a note')")
            connection.execute("INSERT INTO assignment (id, item, role, status) "
                               "VALUES (6, 4, 'merge', 'queued')")
            connection.commit()
        finally:
            connection.close()

    def test_rows_and_the_rows_that_reference_them_survive_and_the_kinds_land(self):
        self._at_version_eight()
        # Through 45: 46 retires `personal` (`TheRetiredItemKinds`).
        result = self.migrate_through(45)
        self.assertEqual((result.before, result.applied), (8, list(range(9, 46))))
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(
            [tuple(row) for row in connection.execute(
                "SELECT id, kind, repo, title FROM item ORDER BY id")],
            [(4, "work", "/repo", "Existing work")])
        self.assertEqual([tuple(row) for row in connection.execute(
            "SELECT id, item, kind FROM note ORDER BY id")], [(5, 4, "comment")])
        self.assertEqual([tuple(row) for row in connection.execute(
            "SELECT id, item FROM assignment ORDER BY id")], [(6, 4)])
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        for kind in self.ADDED:
            connection.execute("INSERT INTO item (kind, title, status, created_at, updated_at) "
                               "VALUES (?, ?, 'planning', 't', 't')", (kind, f"a {kind}"))
        # The CHECK is still a CHECK: widening the list did not remove it.
        for refused in ("bored", ""):
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO item (kind, title, status, created_at, updated_at) "
                                   "VALUES (?, 'x', 'planning', 't', 't')", (refused,))

    def test_the_columns_earlier_migrations_appended_are_still_there(self):
        """The in-place edit rewrites the kind list and nothing else. A
        hand-written CREATE TABLE would have dropped every column migrations 2
        through 8 added, and the rows with them."""
        self._at_version_eight()
        before = self._columns()
        migrate(self.path)
        after = self._columns()
        # Every column survives. The only new ones are those a later
        # migration appends with `ADD COLUMN`: 012's recurrence pair and 041's lead.
        self.assertLessEqual(before, after)
        self.assertEqual(after - before, {"recurrence", "recurrence_anchor", "recurrence_lead"})
        self.assertLessEqual({"source_commit", "piece", "parked_at",
                              "gate_generation", "ready_digest"}, after)

    def _columns(self):
        # Raw, not `connect`: before the migration this database is at the
        # older version and the library refuses to open it, by design.
        connection = sqlite3.connect(self.path)
        try:
            return {row[1] for row in connection.execute("PRAGMA table_info(item)")}
        finally:
            connection.close()

    def test_every_index_on_item_is_still_there_because_nothing_was_rebuilt(self):
        self._at_version_eight()
        connection = sqlite3.connect(self.path)
        before = sorted(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'item'"))
        connection.close()
        migrate(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        after = sorted(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'item'"))
        self.assertEqual(before, after)
        self.assertIn("item_by_piece", after)

    def test_the_guard_leaves_no_table_behind_and_writable_schema_goes_back_off(self):
        self._at_version_eight()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual([row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'migration_009%'")], [])
        self.assertEqual(connection.execute("PRAGMA writable_schema").fetchone()[0], 0)
        self.assertEqual(sorted(tables(connection)), sorted(TABLES))

    def _body(self):
        return dict(schema_module.migrations())[9].read_text(encoding="utf-8")

    def test_a_replay_onto_the_shape_it_produces_changes_nothing(self):
        """`migrate` never replays a file, but the restore path does: a
        snapshot can carry the new shape while claiming the older version, and
        `_upgrade_restore_candidate` then runs this migration over it. That must
        be a no-op, not a refusal."""
        self._at_version_eight()
        migrate(self.path)
        # Local, as the restore replays on the hub: over the wire a script's
        # BEGIN and COMMIT are refused.
        connection = database.open_local(self.path, write=True, create=False)
        self.addCleanup(connection.close)
        before = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0]
        items = connection.execute("SELECT count(*) FROM item").fetchone()[0]
        connection.executescript(f"BEGIN;\n{self._body()}\nCOMMIT;")
        self.assertEqual(connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0], before)
        self.assertEqual(connection.execute("SELECT count(*) FROM item").fetchone()[0], items)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("PRAGMA writable_schema").fetchone()[0], 0)

    def test_a_shape_this_migration_does_not_recognise_is_refused_untouched(self):
        """The reason the guard exists. An in-place schema edit that cannot find
        its pattern would otherwise widen nothing while `user_version` claimed it
        had, so the database would be a shape no number describes."""
        self._refuses(lambda sql: sql.replace(
            "'proposal', 'skill-review', 'dep'", "'proposal', 'skill-review'"))

    def test_a_shape_that_differs_only_in_case_is_refused_too(self):
        """`LIKE` is case-insensitive for ASCII, so this shape would have passed
        a `LIKE` guard while the case-sensitive `replace` changed nothing -- the
        migration would have bumped the version over a table that still rejected
        the new kinds. The guard uses `GLOB`."""
        self._refuses(lambda sql: sql.replace(
            "'proposal', 'skill-review', 'dep'", "'Proposal', 'Skill-Review', 'Dep'"))

    def _refuses(self, mangle):
        self._at_version_eight()
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        current = raw.execute("SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0]
        mangled = mangle(current)
        self.assertNotEqual(mangled, current, "the mangle matched nothing, so this proves nothing")
        raw.execute("PRAGMA writable_schema = ON")
        raw.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'item'", (mangled,))
        raw.execute("PRAGMA writable_schema = RESET")
        before = raw.execute("SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            raw.executescript(f"BEGIN;\n{self._body()}\nCOMMIT;")
        try:
            raw.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        self.assertEqual(raw.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'item'").fetchone()[0], before)
        # The refusal came before `writable_schema` was switched on, so an
        # aborted migration never leaves the connection able to edit the schema.
        self.assertEqual(raw.execute("PRAGMA writable_schema").fetchone()[0], 0)
        self.assertEqual(raw.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_the_guard_writes_no_row_when_it_passes(self):
        """The guard aborts by violating the CHECK, so on the happy path it must
        insert nothing at all."""
        self._at_version_eight()
        migrate(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM item WHERE kind LIKE 'migration-%'").fetchone()[0], 0)

    def test_the_new_kinds_are_not_agent_work_even_with_a_repository_and_branch(self):
        """A personal to-do must never be picked up and run by an agent.

        The repository and the branch are the point. Seeding neither proves only
        that the query's JOIN works, which is what the first version of this test
        did; the schema lets any kind carry both, and while delivery was
        `kind != 'skill-review'` a `personal` row that had them was returned.
        """
        from sd_db.runner import delivery_candidates
        self._at_version_eight()
        # Through 45: 46 retires `personal` (`TheRetiredItemKinds`).
        self.migrate_through(45)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        for kind in self.ADDED:
            connection.execute(
                "INSERT INTO item (kind, repo, branch, title, status, created_at, updated_at) "
                "VALUES (?, '/repo', ?, ?, 'ready_to_send', 't', 't')",
                (kind, f"br-{kind}", f"a {kind}"))
        connection.commit()
        self.assertEqual(delivery_candidates(connection), [])
        # The same fixture for a kind delivery does carry, so the test would fail
        # if the query stopped returning anything at all.
        connection.execute(
            "INSERT INTO item (kind, repo, branch, title, status, created_at, updated_at) "
            "VALUES ('work', '/repo', 'br-work', 'real work', 'ready_to_send', 't', 't')")
        connection.commit()
        self.assertEqual([row["branch"] for row in delivery_candidates(connection)], ["br-work"])

    def test_a_personal_idea_is_not_a_writing_piece(self):
        """`idea` stays the article ladder. The new idea kinds carry no `piece`
        and must not appear in anything the writing pack reads."""
        from sd_db import writing
        self._at_version_eight()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        for kind in ("personal-idea", "work-idea"):
            connection.execute(
                "INSERT INTO item (kind, repo, title, status, stage, created_at, updated_at) "
                "VALUES (?, '/repo', ?, 'planning', 'drafting', 't', 't')", (kind, f"an {kind}"))
        connection.commit()
        self.assertEqual(writing.list_pieces(connection), [])


class TheVersionRefusals(SchemaCase):
    def setUp(self):
        super().setUp()
        initialise(self.path)

    def _set(self, version):
        raw = sqlite3.connect(self.path, isolation_level=None)
        set_schema_version(raw, version)
        raw.close()

    def _checkout(self, *, detached):
        """A git checkout for the library to run from, on `main` or detached as the serving tree is."""
        checkout = self.home / "checkout"
        checkout.mkdir()
        for args in (["init", "-q", "-b", "main"],
                     ["-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-q", "--allow-empty",
                      "-m", "fixture"],
                     *([["checkout", "-q", "--detach"]] if detached else [])):
            subprocess.run(["git", "-C", str(checkout), *args], check=True, capture_output=True)
        return checkout

    def _refusal(self, checkout):
        """The refusal's text for a library that runs from `checkout`, on a machine whose home is `self.home`."""
        with mock.patch("sd_db.errors._checkout", return_value=checkout), \
                mock.patch.dict("os.environ", {"HOME": str(self.home)}):
            return str(SchemaTooNew(SCHEMA_VERSION + 3, SCHEMA_VERSION))

    def test_a_newer_database_is_refused_on_open_naming_both(self):
        self._set(SCHEMA_VERSION + 3)
        with mock.patch("sd_db.errors._checkout", return_value=self._checkout(detached=False)), \
                self.assertRaises(SchemaTooNew) as raised:
            connect(self.path)
        message = str(raised.exception)
        self.assertIn(str(SCHEMA_VERSION + 3), message)
        self.assertIn(str(SCHEMA_VERSION), message)
        # sd:1664, sd:3370: the pack runs `lib/` from its checkout, so the remedy is a pull.
        self.assertIn("pull --ff-only", message)

    def test_a_detached_serving_tree_on_the_hub_is_moved_by_repo_sync_refresh(self):
        """sd:3415: a detached tree cannot pull, and `sd-install --pull` refuses one."""
        message = self._refusal(self._checkout(detached=True))
        self.assertNotIn("pull --ff-only", message)
        self.assertIn("`repo-sync.sh refresh` on the hub", message)

    def test_a_detached_tree_on_a_satellite_is_moved_by_repo_sync_follow(self):
        checkout = self._checkout(detached=True)
        (self.home / ".config/sd").mkdir(parents=True)
        (self.home / ".config/sd/hub.json").write_text('{"hub": "hub.example.test"}', encoding="utf-8")
        message = self._refusal(checkout)
        self.assertNotIn("pull --ff-only", message)
        self.assertIn("`repo-sync.sh follow` on this satellite", message)

    def test_a_checkout_whose_head_cannot_be_read_names_both_remedies(self):
        """An unreadable HEAD is unknown, not a branch: the message names the pull and the repo-sync verb."""
        checkout = self.home / "checkout"
        (checkout / ".git").mkdir(parents=True)
        message = self._refusal(checkout)
        self.assertIn("pull --ff-only", message)
        self.assertIn("repo-sync.sh refresh", message)

    def test_a_newer_database_is_refused_for_reading_too(self):
        """A wrong answer read quietly is worse than a process that stops."""
        self._set(SCHEMA_VERSION + 1)
        with self.assertRaises(SchemaTooNew):
            connect(self.path, write=False)

    def test_an_older_database_is_refused_on_write_naming_the_command(self):
        self._set(0)
        with self.assertRaises(SchemaTooOld) as raised:
            connect(self.path)
        # sd:3370: the pack's entrypoint, not the retired local-sd-db.
        self.assertIn("`bin/sd-db migrate` from the command pack", str(raised.exception))
        # sd:2974: the hub's tailnet listener runs against the database too.
        self.assertIn("sd-serve", str(raised.exception))

    def test_an_older_database_still_opens_for_reading(self):
        self._set(0)
        connection = connect(self.path, write=False)
        self.addCleanup(connection.close)
        self.assertEqual(schema_version(connection), 0)


class TheRunnerMergeColumn(SchemaCase):
    """Migration 10. `repo.merge_policy` becomes `repo.runner_merge`.

    The old name said what the column was, not who reads it, and it sat beside
    the pack's assistant grant under a name that read the same. The rebuild is
    007's, so the things to prove are 007's too: the recorded value survives,
    the CHECK came with the column, and the rows that reference `repo` still
    reach it rather than the copy the rebuild dropped.
    """

    def _at_version_nine(self):
        connection = connect(self.path, create=True, write=True)
        try:
            for version, path in schema_module.migrations():
                # 9 literally, never `SCHEMA_VERSION - 1`. The relative form
                # names this migration's predecessor only while 010 is the
                # last one; the next bump would quietly build a v10 database
                # and leave 010 unexercised by the test written for it.
                if version > 9:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.execute(
                "INSERT INTO repo (path, remote, merge_policy, created_at, updated_at) "
                "VALUES ('/repo', 'git@example:o/r.git', 'auto', 't', 't')")
            connection.execute(
                "INSERT INTO item (id, kind, repo, title, status, created_at, updated_at) "
                "VALUES (4, 'work', '/repo', 'Existing work', 'planning', 't', 't')")
            connection.commit()
        finally:
            connection.close()

    def test_the_column_is_renamed_and_the_recorded_policy_survives(self):
        self._at_version_nine()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (9, list(range(10, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        columns = [row["name"] for row in connection.execute("PRAGMA table_info(repo)")]
        self.assertIn("runner_merge", columns)
        self.assertNotIn("merge_policy", columns)
        self.assertEqual(
            connection.execute("SELECT runner_merge FROM repo WHERE path = '/repo'").fetchone()[0],
            "auto")

    def test_the_check_and_the_default_came_with_the_column(self):
        self._at_version_nine()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        connection.execute("INSERT INTO repo (path, created_at, updated_at) VALUES ('/fresh', 't', 't')")
        self.assertEqual(
            connection.execute("SELECT runner_merge FROM repo WHERE path = '/fresh'").fetchone()[0],
            "manual")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE repo SET runner_merge = 'whenever' WHERE path = '/fresh'")

    def test_the_rows_that_reference_the_rebuilt_table_still_reach_it(self):
        self._at_version_nine()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(
            connection.execute("SELECT title FROM item WHERE repo = '/repo'").fetchone()[0],
            "Existing work")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO item (kind, repo, title, status, created_at, updated_at) "
                "VALUES ('work', '/absent', 't', 'planning', 't', 't')")


class TheManagedColumn(SchemaCase):
    """Migration 15. `repo.managed` names the repositories the operator manages.

    Sessions inferred the answer from remotes and prose; the column makes it
    one stored fact. It is added, not rebuilt: an existing row keeps every
    value and reads 0 until something sets it, and the CHECK holds the
    column to the two values a flag has.
    """

    def _at_version_fourteen(self):
        connection = connect(self.path, create=True, write=True)
        try:
            # 014 converts paths with two functions `migrate` registers.
            paths.install(connection)
            for version, path in schema_module.migrations():
                # 14 literally, for the reason `_at_version_nine` gives.
                if version > 14:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.execute(
                "INSERT INTO repo (path, remote, runner_merge, created_at, updated_at) "
                "VALUES ('/repo', 'git@github.com:platypeeps/r.git', 'auto', 't', 't')")
            connection.commit()
        finally:
            connection.close()

    def test_the_column_arrives_at_zero_and_the_row_keeps_its_values(self):
        self._at_version_fourteen()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (14, list(range(15, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        row = connection.execute("SELECT * FROM repo WHERE path = '/repo'").fetchone()
        self.assertEqual((row["managed"], row["runner_merge"], row["remote"]),
                         (0, "auto", "git@github.com:platypeeps/r.git"))

    def test_the_check_holds_the_column_to_zero_and_one(self):
        self._at_version_fourteen()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        connection.execute("UPDATE repo SET managed = 1 WHERE path = '/repo'")
        for value in (2, -1, "yes", None):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE repo SET managed = ? WHERE path = '/repo'", (value,))
        self.assertEqual(
            connection.execute("SELECT managed FROM repo WHERE path = '/repo'").fetchone()[0], 1)


class TheCiColumn(SchemaCase):
    """Migration 16. `repo.ci` says where a repository's checks run (sd:1843).

    `github` is GitHub Actions, which every repository used before the
    column; `local` is `sd-check` run by the pack, posting `sd/local-gate`.
    It is added, not rebuilt: every existing row reads `github` and keeps its
    other values, and the CHECK holds the column to the two modes.
    """

    def _at_version_fifteen(self):
        connection = connect(self.path, create=True, write=True)
        try:
            # 014 converts paths with two functions `migrate` registers.
            paths.install(connection)
            for version, path in schema_module.migrations():
                # 15 literally, for the reason `_at_version_nine` gives.
                if version > 15:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.executemany(
                "INSERT INTO repo (path, remote, runner_merge, managed, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 't', 't')",
                [("/one", "git@github.com:platypeeps/one.git", "auto", 1),
                 ("/two", None, "manual", 0)])
            connection.commit()
        finally:
            connection.close()

    def test_every_existing_row_arrives_at_github_and_keeps_its_values(self):
        self._at_version_fifteen()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (15, list(range(16, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        rows = [tuple(row) for row in connection.execute(
            "SELECT path, ci, runner_merge, managed, remote FROM repo ORDER BY path")]
        self.assertEqual(rows, [
            ("/one", "github", "auto", 1, "git@github.com:platypeeps/one.git"),
            ("/two", "github", "manual", 0, None)])

    def test_the_check_holds_the_column_to_github_and_local(self):
        self._at_version_fifteen()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        connection.execute("UPDATE repo SET ci = 'local' WHERE path = '/one'")
        for value in ("actions", "GITHUB", "", None):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE repo SET ci = ? WHERE path = '/one'", (value,))
        self.assertEqual(
            connection.execute("SELECT ci FROM repo WHERE path = '/one'").fetchone()[0], "local")


class TheNullableRunRepo(SchemaCase):
    """Migration 18. `runner_run.repo` may be NULL: `repo remove` detaches a
    released run of an item that moved to another repo (sd:2581).

    The column is edited in place, as 009 edited `item`, because
    `runner_lease.run` references `runner_run(id)` and the rebuild cannot drop
    the old copy inside `migrate`'s transaction. So the proofs are 009's:
    rows survive, the index is still there, and a shape the file does not
    know is refused untouched. It also adds `detached_from`, the repository a
    detached run had, so a replay is refused untouched too.
    """

    RUN = "a" * 32

    def _at_version_seventeen(self):
        connection = connect(self.path, create=True, write=True)
        try:
            paths.install(connection)
            for version, path in schema_module.migrations():
                if version > 17:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.execute("INSERT INTO repo (path, created_at, updated_at) VALUES ('/repo', 't', 't')")
            connection.execute("INSERT INTO item (id, kind, repo, title, status, created_at, updated_at) "
                               "VALUES (4, 'work', '/repo', 'work', 'done', 't', 't')")
            connection.execute("INSERT INTO assignment (id, item, role, status) VALUES (6, 4, 'author', 'done')")
            connection.execute("INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, "
                               "retained_path, created_at, updated_at, released_at) "
                               "VALUES (?, 6, 1, '/repo', 'sd/x', 'runner', '/w', '/r', 't', 't', 't')", (self.RUN,))
            connection.execute("INSERT INTO runner_lease (run, repo, branch, exclusive, acquired_at, released_at) "
                               "VALUES (?, '/repo', 'sd/x', 1, 't', 't')", (self.RUN,))
            connection.commit()
        finally:
            connection.close()

    def _body(self):
        return dict(schema_module.migrations())[18].read_text(encoding="utf-8")

    def _run_sql(self, connection):
        return connection.execute("SELECT sql FROM sqlite_master WHERE name = 'runner_run'").fetchone()[0]

    def test_the_run_survives_and_its_repo_may_now_be_null(self):
        self._at_version_seventeen()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (17, list(range(18, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(tuple(connection.execute(
            "SELECT id, assignment, repo, branch, detached_from FROM runner_run").fetchone()),
            (self.RUN, 6, "/repo", "sd/x", None))
        connection.execute("UPDATE runner_run SET repo = NULL WHERE id = ?", (self.RUN,))
        self.assertIsNone(connection.execute("SELECT repo FROM runner_run").fetchone()[0])
        # The lease keeps its NOT NULL, and the foreign key still binds a run's repo.
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE runner_lease SET repo = NULL")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE runner_run SET repo = '/elsewhere'")
        self.assertEqual([row[1] for row in connection.execute("PRAGMA index_list(runner_run)")
                          if not row[1].startswith("sqlite_")], ["runner_by_assignment"])
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("PRAGMA writable_schema").fetchone()[0], 0)
        self.assertEqual(sorted(tables(connection)), sorted(TABLES))

    def test_a_replay_onto_the_shape_it_produces_is_refused_untouched(self):
        """Nothing replays 018: the restore path migrates only a snapshot older than 18."""
        self._at_version_seventeen()
        migrate(self.path)
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        before = self._run_sql(raw)
        with self.assertRaisesRegex(sqlite3.OperationalError, "duplicate column name: detached_from"):
            raw.executescript(f"BEGIN;\n{self._body()}\nCOMMIT;")
        try:
            raw.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        self.assertEqual(self._run_sql(raw), before)
        self.assertEqual(raw.execute("PRAGMA writable_schema").fetchone()[0], 0)

    def test_a_shape_this_migration_does_not_recognise_is_refused_untouched(self):
        self._at_version_seventeen()
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        current = self._run_sql(raw)
        mangled = current.replace("    repo TEXT NOT NULL REFERENCES repo(path),",
                                  "    repo text not null references repo(path),")
        self.assertNotEqual(mangled, current, "the mangle matched nothing, so this proves nothing")
        raw.execute("PRAGMA writable_schema = ON")
        raw.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'runner_run'", (mangled,))
        raw.execute("PRAGMA writable_schema = RESET")
        with self.assertRaises(sqlite3.IntegrityError):
            raw.executescript(f"BEGIN;\n{self._body()}\nCOMMIT;")
        try:
            raw.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        self.assertEqual(self._run_sql(raw), mangled)
        self.assertEqual(raw.execute("PRAGMA writable_schema").fetchone()[0], 0)


class TheRequestOutcome(SchemaCase):
    """Migration 19. `request_outcome`, one row per write transaction a
    remote session committed (sd:1335, step 5). A new table, empty, and no
    other table touched; its reverse returns the file to 18."""

    def _at_version_eighteen(self):
        connection = connect(self.path, create=True, write=True)
        try:
            paths.install(connection)
            for version, path in schema_module.migrations():
                if version > 18:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.execute("INSERT INTO item (kind, title, status, created_at, updated_at) "
                               "VALUES ('work', 'kept', 'planning', 't', 't')")
        finally:
            connection.close()

    def _dump(self):
        raw = sqlite3.connect(self.path)
        try:
            return [line for line in raw.iterdump() if "request_outcome" not in line]
        finally:
            raw.close()

    def _migrate_to_nineteen(self):
        # 20 adds a `repo` column, so a migration to the latest version moves
        # the `repo` table this class proves 19 leaves alone.
        through = [m for m in schema_module.migrations() if m[0] <= 19]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            return migrate(self.path)

    def test_the_table_arrives_empty_and_nothing_else_moves(self):
        self._at_version_eighteen()
        before = self._dump()
        result = self._migrate_to_nineteen()
        self.assertEqual((result.before, result.applied), (18, [19]))
        # Raw: `connect` refuses a file below the library's version.
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual([(row[1], row[2], row[3], row[5]) for row in
                          connection.execute("PRAGMA table_info(request_outcome)")],
                         [("id", "TEXT", 1, 1), ("committed_at", "TEXT", 1, 0)])
        self.assertEqual(connection.execute("SELECT count(*) FROM request_outcome").fetchone()[0], 0)
        # 48 adds `vote` (pack sd:8050) and 49 `home_asset` (pack sd:8053)
        # after 19.
        self.assertEqual(sorted(tables(connection)), sorted(set(TABLES) - {"vote", "home_asset"}))
        self.assertEqual(self._dump(), before)
        connection.execute("INSERT INTO request_outcome VALUES ('01J0000000000000000000000A', 't')")
        for row in (("01J0000000000000000000000A", "t"), (None, "t"), ("01J0000000000000000000000B", None)):
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO request_outcome VALUES (?, ?)", row)

    def test_the_reverse_returns_the_file_to_eighteen(self):
        self._at_version_eighteen()
        before = self._dump()
        self._migrate_to_nineteen()
        text = dict(schema_module.migrations())[19].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 18)
        self.assertNotIn("request_outcome", tables(raw))
        self.assertEqual(self._dump(), before)


class TheSatelliteGateColumn(SchemaCase):
    """Migration 20. `repo.satellite_gate`, the operator's per-repository
    grant to merge on a satellite's gate pass (sd:2704, ruling Q1).

    Added, not rebuilt, as 16 was: every existing row reads `off` and keeps
    its other values, the CHECK holds the column to the two values, and the
    reverse returns the file to 19.
    """

    def _at_version_nineteen(self):
        connection = connect(self.path, create=True, write=True)
        try:
            paths.install(connection)
            for version, path in schema_module.migrations():
                # 19 literally, for the reason `_at_version_nine` gives.
                if version > 19:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.executemany(
                "INSERT INTO repo (path, remote, runner_merge, managed, ci, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 't', 't')",
                [("/one", "git@github.com:platypeeps/one.git", "auto", 1, "local"),
                 ("/two", None, "manual", 0, "github")])
            connection.commit()
        finally:
            connection.close()

    def _through_twenty(self):
        # Through 20 only: 25 drops the column again (sd:3217).
        through = [m for m in schema_module.migrations() if m[0] <= 20]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            return migrate(self.path)

    def test_every_existing_row_arrives_at_off_and_keeps_its_values(self):
        self._at_version_nineteen()
        result = self._through_twenty()
        self.assertEqual((result.before, result.applied), (19, [20]))
        # The library refuses a file below its version; read it raw.
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        rows = [tuple(row) for row in connection.execute(
            "SELECT path, satellite_gate, ci, runner_merge, managed, remote FROM repo ORDER BY path")]
        self.assertEqual(rows, [
            ("/one", "off", "local", "auto", 1, "git@github.com:platypeeps/one.git"),
            ("/two", "off", "github", "manual", 0, None)])

    def test_the_check_holds_the_column_to_off_and_accept(self):
        self._at_version_nineteen()
        self._through_twenty()
        # The library refuses a file below its version; read it raw.
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        connection.execute("UPDATE repo SET satellite_gate = 'accept' WHERE path = '/one'")
        for value in ("on", "ACCEPT", "", None):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE repo SET satellite_gate = ? WHERE path = '/one'", (value,))
        self.assertEqual(connection.execute(
            "SELECT satellite_gate FROM repo WHERE path = '/one'").fetchone()[0], "accept")

    def test_the_reverse_returns_the_file_to_nineteen(self):
        self._at_version_nineteen()
        # Through 20 only: 21 adds a `judgment` column 20's reverse leaves.
        self._through_twenty()
        text = dict(schema_module.migrations())[20].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 19)
        self.assertNotIn("satellite_gate", [row[1] for row in raw.execute("PRAGMA table_info(repo)")])


class TheJudgmentCallContextColumns(SchemaCase):
    """Migration 21. Five `judgment` columns for the context of a call:
    `location`, `threshold`, `run_id`, `prompt_hash` and `load_avg` (sd:2950).

    Added, not rebuilt, as 20 was: every existing row reads NULL in all five
    and keeps its other values, and the reverse returns the file to 20.
    """

    CONTEXT = (("location", "TEXT"), ("threshold", "REAL"), ("run_id", "TEXT"),
               ("prompt_hash", "TEXT"), ("load_avg", "REAL"))

    def _at_version_twenty(self):
        connection = connect(self.path, create=True, write=True)
        try:
            paths.install(connection)
            for version, path in schema_module.migrations():
                # 20 literally, for the reason `_at_version_nine` gives.
                if version > 20:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.executemany(
                "INSERT INTO judgment (timestamp, caller, stage, arm, provider, "
                "primitive, outcome, answer) VALUES ('t', ?, ?, ?, 'typesafe', 'noul', 'ok', ?)",
                [("one", "JEV_ONE", "jev", "0.9"), ("two", "JEV_TWO", "baseline", "1")])
            connection.commit()
        finally:
            connection.close()

    def _dump(self):
        raw = sqlite3.connect(self.path)
        try:
            return list(raw.iterdump())
        finally:
            raw.close()

    def test_every_existing_row_arrives_at_null_and_keeps_its_values(self):
        self._at_version_twenty()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (20, list(range(21, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        columns = [(row[1], row[2], row[3], row[4])
                   for row in connection.execute("PRAGMA table_info(judgment)")]
        for name, kind in self.CONTEXT:
            self.assertIn((name, kind, 0, None), columns)
        rows = [tuple(row) for row in connection.execute(
            "SELECT caller, stage, arm, answer, location, threshold, run_id, "
            "prompt_hash, load_avg FROM judgment ORDER BY id")]
        self.assertEqual(rows, [("one", "JEV_ONE", "jev", "0.9") + (None,) * 5,
                                ("two", "JEV_TWO", "baseline", "1") + (None,) * 5])

    def test_the_reverse_returns_the_file_to_twenty(self):
        self._at_version_twenty()
        before = self._dump()
        # Through 21 only: 22 adds a `judgment` column 21's reverse leaves.
        through = [m for m in schema_module.migrations() if m[0] <= 21]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            migrate(self.path)
        text = dict(schema_module.migrations())[21].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 20)
        left = {row[1] for row in raw.execute("PRAGMA table_info(judgment)")}
        self.assertEqual(left & {name for name, _ in self.CONTEXT}, set())
        self.assertEqual(self._dump(), before)


class TheJudgmentBatchChildren(TheJudgmentCallContextColumns):
    """Migration 22. `judgment.parent` names the batch row a child row
    answers one question of (sd:2966). Every existing row is a call of its
    own, so it reads NULL, and the reverse returns the file to 21."""

    def _at_version_twenty_one(self):
        self._at_version_twenty()
        through = [m for m in schema_module.migrations() if m[0] <= 21]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            migrate(self.path)

    def test_every_existing_row_arrives_at_null_and_keeps_its_values(self):
        self._at_version_twenty_one()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (21, list(range(22, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        columns = [(row[1], row[2], row[3], row[4])
                   for row in connection.execute("PRAGMA table_info(judgment)")]
        self.assertIn(("parent", "INTEGER", 0, None), columns)
        self.assertEqual(
            [tuple(row) for row in connection.execute(
                "SELECT caller, answer, parent FROM judgment ORDER BY id")],
            [("one", "0.9", None), ("two", "1", None)])
        self.assertIn("judgment_by_parent", {row[1] for row in connection.execute(
            "PRAGMA index_list(judgment)")})

    def test_the_reverse_returns_the_file_to_twenty_one(self):
        self._at_version_twenty_one()
        before = self._dump()
        # Through 22 only: 23 adds `judgment` columns 22's reverse leaves.
        through = [m for m in schema_module.migrations() if m[0] <= 22]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            migrate(self.path)
        text = dict(schema_module.migrations())[22].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 21)
        self.assertEqual(self._dump(), before)

    # The parent class's own tests are migration 21's; they run there.
    test_the_reverse_returns_the_file_to_twenty = None


class TheJudgmentError(TheJudgmentBatchChildren):
    """Migration 23. `judgment.error_class` and `error_detail` say why a call
    failed (sd:2973). Every existing row reads NULL, and the reverse returns
    the file to 22."""

    def _at_version_twenty_two(self):
        self._at_version_twenty()
        through = [m for m in schema_module.migrations() if m[0] <= 22]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            migrate(self.path)

    def test_every_existing_row_arrives_at_null_and_keeps_its_values(self):
        self._at_version_twenty_two()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (22, list(range(23, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        columns = [(row[1], row[2], row[3], row[4])
                   for row in connection.execute("PRAGMA table_info(judgment)")]
        self.assertIn(("error_class", "TEXT", 0, None), columns)
        self.assertIn(("error_detail", "TEXT", 0, None), columns)
        self.assertEqual(
            [tuple(row) for row in connection.execute(
                "SELECT caller, answer, error_class, error_detail FROM judgment ORDER BY id")],
            [("one", "0.9", None, None), ("two", "1", None, None)])

    def test_the_reverse_returns_the_file_to_twenty_two(self):
        self._at_version_twenty_two()
        before = self._dump()
        # Through 23 only: 24 adds a `repo` column 23's reverse leaves.
        through = [m for m in schema_module.migrations() if m[0] <= 23]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            migrate(self.path)
        text = dict(schema_module.migrations())[23].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 22)
        self.assertEqual(self._dump(), before)

    # Migration 22's own reverse test runs in its class.
    test_the_reverse_returns_the_file_to_twenty_one = None


class TheLaneHostColumn(SchemaCase):
    """Migration 24. `repo.lane_host`, the machine that runs a repository's
    merge lane; NULL is the hub (sd:3075, design sd:3003).

    Added, not rebuilt, as 20 was: every existing row reads NULL and keeps its
    other values, the CHECK holds a value to `[a-z0-9-]+`, and the reverse
    returns the file to 23.
    """

    def _at_version_twenty_three(self):
        connection = connect(self.path, create=True, write=True)
        try:
            paths.install(connection)
            for version, path in schema_module.migrations():
                if version > 23:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.executemany(
                "INSERT INTO repo (path, remote, runner_merge, managed, ci, satellite_gate, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 't', 't')",
                [("/one", "git@github.com:platypeeps/one.git", "auto", 1, "local", "accept"),
                 ("/two", None, "manual", 0, "github", "off")])
            connection.commit()
        finally:
            connection.close()

    def _through_twenty_four(self):
        # Through 24 only: 25 drops `satellite_gate`, which these rows carry (sd:3217).
        through = [m for m in schema_module.migrations() if m[0] <= 24]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            return migrate(self.path)

    def test_every_existing_row_arrives_at_null_and_keeps_its_values(self):
        self._at_version_twenty_three()
        result = self._through_twenty_four()
        self.assertEqual((result.before, result.applied), (23, [24]))
        # The library refuses a file below its version; read it raw.
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        rows = [tuple(row) for row in connection.execute(
            "SELECT path, lane_host, satellite_gate, ci, runner_merge, managed, remote FROM repo ORDER BY path")]
        self.assertEqual(rows, [
            ("/one", None, "accept", "local", "auto", 1, "git@github.com:platypeeps/one.git"),
            ("/two", None, "off", "github", "manual", 0, None)])

    def test_the_check_holds_a_value_to_lower_case_letters_digits_and_dashes(self):
        self._at_version_twenty_three()
        self._through_twenty_four()
        # The library refuses a file below its version; read it raw.
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        connection.execute("UPDATE repo SET lane_host = 'build-2' WHERE path = '/one'")
        for value in ("Build_2", "build 2", "", "build.example.test"):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE repo SET lane_host = ? WHERE path = '/one'", (value,))
        connection.execute("UPDATE repo SET lane_host = NULL WHERE path = '/two'")
        self.assertEqual([row[0] for row in connection.execute(
            "SELECT lane_host FROM repo ORDER BY path")], ["build-2", None])

    def test_the_reverse_returns_the_file_to_twenty_three(self):
        self._at_version_twenty_three()
        self._through_twenty_four()
        text = dict(schema_module.migrations())[24].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 23)
        self.assertNotIn("lane_host", [row[1] for row in raw.execute("PRAGMA table_info(repo)")])


class TheSatelliteGateDrop(SchemaCase):
    """Migration 25. `repo.satellite_gate` goes: the pack retired the
    satellite gate offload, so nothing reads the grant (sd:3217, design sd:3003).

    Dropped in place, as 20's reverse drops it: every row keeps its other
    values. A failed drop leaves the file at 24 with the column and its
    values, and the reverse returns the file to 24 with every row at `off`.
    """

    COLUMNS = "path, remote, mode, runner_merge, managed, ci, lane_host, pieces_source"

    def _at_version_twenty_four(self):
        connection = connect(self.path, create=True, write=True)
        try:
            paths.install(connection)
            for version, path in schema_module.migrations():
                # 24 literally, for the reason `_at_version_nine` gives.
                if version > 24:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.executemany(
                "INSERT INTO repo (path, remote, runner_merge, managed, ci, satellite_gate, lane_host, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 't', 't')",
                [("/one", "git@github.com:platypeeps/one.git", "auto", 1, "local", "accept", "build-2"),
                 ("/two", None, "manual", 0, "github", "off", None)])
            connection.commit()
        finally:
            connection.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(
            f"SELECT {self.COLUMNS}, created_at, updated_at FROM repo ORDER BY path")]

    def _columns(self, connection):
        return [row[1] for row in connection.execute("PRAGMA table_info(repo)")]

    def test_the_column_goes_and_every_row_keeps_its_other_values(self):
        self._at_version_twenty_four()
        raw = sqlite3.connect(self.path)
        before = self._rows(raw)
        raw.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (24, list(range(25, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertNotIn("satellite_gate", self._columns(connection))
        self.assertEqual(self._rows(connection), before)
        self.assertEqual(before[0][:7], ("/one", "git@github.com:platypeeps/one.git", None, "auto", 1,
                                         "local", "build-2"))

    def test_a_failed_drop_leaves_the_file_at_twenty_four_and_a_rerun_finishes_it(self):
        self._at_version_twenty_four()
        # SQLite refuses to drop an indexed column; an index the operator
        # added by hand is the one way this one statement can fail.
        raw = sqlite3.connect(self.path)
        raw.execute("CREATE INDEX by_gate ON repo(satellite_gate)")
        raw.commit()
        before = self._rows(raw)
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            migrate(self.path)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), 24)
        self.assertIn("satellite_gate", self._columns(raw))
        self.assertEqual([row[0] for row in raw.execute("SELECT satellite_gate FROM repo ORDER BY path")],
                         ["accept", "off"])
        self.assertEqual(self._rows(raw), before)
        raw.execute("DROP INDEX by_gate")
        raw.commit()
        raw.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (24, list(range(25, SCHEMA_VERSION + 1))))

    def test_the_reverse_returns_the_file_to_twenty_four_with_every_row_off(self):
        self._at_version_twenty_four()
        migrate(self.path)
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        # 26 came after and is reversed first, newest first.
        for version in (26, 25):
            text = dict(schema_module.migrations())[version].read_text(encoding="utf-8")
            raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 24)
        self.assertEqual([row[0] for row in raw.execute("SELECT satellite_gate FROM repo ORDER BY path")],
                         ["off", "off"])
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("UPDATE repo SET satellite_gate = 'on' WHERE path = '/one'")
        # The column sits where 020 put it: a backup restore compares 24's shape.
        reference = sqlite3.connect(":memory:")
        self.addCleanup(reference.close)
        paths.install(reference)
        for version, path in schema_module.migrations():
            if version <= 24:
                reference.executescript(path.read_text(encoding="utf-8"))
        self.assertEqual(raw.execute("PRAGMA table_info(repo)").fetchall(),
                         reference.execute("PRAGMA table_info(repo)").fetchall())
        self.assertEqual(raw.execute("PRAGMA foreign_key_check").fetchall(), [])


class TheStatusSourceDrop(SchemaCase):
    """Migration 26. `repo.status_source` goes: the pack reads every item's
    status from its row (sd:3015), so nothing reads the owner (sd:3231).

    Dropped in place, as 25 drops `satellite_gate`: every row keeps its other
    values, `pieces_source` among them. A failed drop leaves the file at 25
    with the column and its values, and the reverse returns the file to 25's
    shape with every row at `row`, the owner the rows have been since.
    """

    COLUMNS = "path, remote, mode, runner_merge, pieces_source, managed, ci, lane_host"

    def _at_version_twenty_five(self):
        connection = connect(self.path, create=True, write=True)
        try:
            paths.install(connection)
            for version, path in schema_module.migrations():
                # 25 literally, for the reason `_at_version_nine` gives.
                if version > 25:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.executemany(
                "INSERT INTO repo (path, remote, runner_merge, status_source, pieces_source, managed, ci, "
                "lane_host, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 't1', 't2')",
                [("/one", "git@github.com:platypeeps/one.git", "auto", "row", "row", 1, "local", "build-2"),
                 ("/three", None, "manual", "row", "retiring", 0, "github", None),
                 ("/two", None, "manual", "file", "file", 0, "github", None)])
            connection.commit()
        finally:
            connection.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(
            f"SELECT {self.COLUMNS}, created_at, updated_at FROM repo ORDER BY path")]

    def _columns(self, connection):
        return [row[1] for row in connection.execute("PRAGMA table_info(repo)")]

    def test_the_column_goes_and_every_row_keeps_its_other_values(self):
        self._at_version_twenty_five()
        raw = sqlite3.connect(self.path)
        before = self._rows(raw)
        raw.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (25, list(range(26, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertNotIn("status_source", self._columns(connection))
        self.assertEqual(self._rows(connection), before)
        self.assertEqual(before[0], ("/one", "git@github.com:platypeeps/one.git", None, "auto", "row", 1,
                                     "local", "build-2", "t1", "t2"))
        self.assertEqual([row[4] for row in before], ["row", "retiring", "file"])

    def test_a_failed_drop_leaves_the_file_at_twenty_five_and_a_rerun_finishes_it(self):
        self._at_version_twenty_five()
        # SQLite refuses to drop an indexed column; an index the operator
        # added by hand is the one way this one statement can fail.
        raw = sqlite3.connect(self.path)
        raw.execute("CREATE INDEX by_owner ON repo(status_source)")
        raw.commit()
        before = self._rows(raw)
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            migrate(self.path)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), 25)
        self.assertIn("status_source", self._columns(raw))
        self.assertEqual([row[0] for row in raw.execute("SELECT status_source FROM repo ORDER BY path")],
                         ["row", "row", "file"])
        self.assertEqual(self._rows(raw), before)
        raw.execute("DROP INDEX by_owner")
        raw.commit()
        raw.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (25, list(range(26, SCHEMA_VERSION + 1))))

    def test_a_retiring_repository_refuses_the_drop_and_names_it(self):
        """`retiring` is a restore's hold on rows not yet proven against the
        repository's files; dropping the column would drop the hold (sd:3231
        review round 1). The file stays at 25, untouched, until it clears."""
        self._at_version_twenty_five()
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE repo SET status_source = 'retiring' WHERE path IN ('/three', '/two')")
        raw.commit()
        before = self._rows(raw)
        raw.close()
        with self.assertRaises(sqlite3.IntegrityError) as raised:
            migrate(self.path)
        self.assertIn("repo.status_source is retiring for /three, /two;", str(raised.exception))
        self.assertIn("sd restore reimport", str(raised.exception))
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), 25)
        self.assertEqual([row[0] for row in raw.execute("SELECT status_source FROM repo ORDER BY path")],
                         ["row", "retiring", "retiring"])
        self.assertEqual(self._rows(raw), before)
        raw.execute("UPDATE repo SET status_source = 'row'")
        raw.commit()
        raw.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (25, list(range(26, SCHEMA_VERSION + 1))))
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_temp_master").fetchone()[0], 0)

    def test_the_reverse_returns_the_file_to_twenty_five_with_every_row_on_row(self):
        self._at_version_twenty_five()
        raw = sqlite3.connect(self.path)
        before = self._rows(raw)
        raw.close()
        migrate(self.path)
        text = dict(schema_module.migrations())[26].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 25)
        self.assertEqual([row[0] for row in raw.execute("SELECT status_source FROM repo ORDER BY path")],
                         ["row", "row", "row"])
        self.assertEqual(self._rows(raw), before)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("UPDATE repo SET status_source = 'owned' WHERE path = '/one'")
        # The column sits where 001 put it: a backup restore compares 25's shape.
        reference = sqlite3.connect(":memory:")
        self.addCleanup(reference.close)
        paths.install(reference)
        for version, path in schema_module.migrations():
            if version <= 25:
                reference.executescript(path.read_text(encoding="utf-8"))
        self.assertEqual(raw.execute("PRAGMA table_info(repo)").fetchall(),
                         reference.execute("PRAGMA table_info(repo)").fetchall())
        self.assertEqual(raw.execute("PRAGMA foreign_key_check").fetchall(), [])


class TheSnoozeKind(SchemaCase):
    """Migration 27 (sd:1896). `snooze` joins the `state` kinds: a Today or
    Health row hidden until a time. The table is rebuilt as 7 rebuilt it, so
    every row comes through with its id and all four indexes come back. A
    failed rebuild leaves the file at 26, and the reverse returns it to 26's
    shape without the snooze rows."""

    ROWS = [(7, "heartbeat", "runner", "t1", "{}", None),
            (9, "restore", "2026-09-01", "t2", "{}", "t3"),
            (11, "check", "run-1", "t4", "{}", None),
            (12, "checkpoint", "contribution:a", "t5", "{}", None)]

    def _at_version_twenty_six(self):
        connection = connect(self.path, create=True, write=True)
        try:
            paths.install(connection)
            for version, path in schema_module.migrations():
                # 26 literally, for the reason `_at_version_nine` gives.
                if version > 26:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.executemany(
                "INSERT INTO state (id, kind, key, timestamp, body, resolved_at) VALUES (?, ?, ?, ?, ?, ?)", self.ROWS)
            connection.commit()
        finally:
            connection.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(
            "SELECT id, kind, key, timestamp, body, resolved_at FROM state ORDER BY id")]

    def _shape(self, connection):
        from sd_db.backup import _schema_objects

        return ([tuple(row) for row in connection.execute("PRAGMA table_info(state)")],
                _schema_objects(connection, ["state"]))

    def _reference(self, upto):
        reference = sqlite3.connect(":memory:")
        self.addCleanup(reference.close)
        paths.install(reference)
        for version, path in schema_module.migrations():
            if version <= upto:
                reference.executescript(path.read_text(encoding="utf-8"))
        return reference

    def test_rows_survive_the_rebuild_and_a_snooze_is_accepted(self):
        self._at_version_twenty_six()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (26, list(range(27, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual(self._shape(connection), self._shape(self._reference(26)))
        connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('snooze', 'today:job:x:1', 't6', '{}')")
        connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('snooze', 'today:job:x:1', 't7', '{}')")
        # 031 dropped the kind CHECK; `TheStateKindList` holds the refusal (sd:3506).
        # The heartbeat's and the check's one-row-per-key indexes came back with the table.
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('heartbeat', 'runner', 't9', '{}')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('check', 'run-1', 't9', '{}')")
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'state_before_%'").fetchone()[0], 0)

    def test_a_failed_rebuild_leaves_the_file_at_twenty_six_and_a_rerun_finishes_it(self):
        self._at_version_twenty_six()
        # A row the new CHECK refuses fails the copy, after the rename and the
        # new table: the one way the rebuild can stop half done.
        raw = sqlite3.connect(self.path)
        raw.execute("PRAGMA ignore_check_constraints = ON")
        raw.execute("INSERT INTO state (id, kind, key, timestamp) VALUES (20, 'bored', 'x', 't9')")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.IntegrityError):
            migrate(self.path)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), 26)
        self.assertEqual(self._rows(raw), self.ROWS + [(20, "bored", "x", "t9", None, None)])
        self.assertEqual(self._shape(raw), self._shape(self._reference(26)))
        self.assertEqual(raw.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'state_before_%'").fetchone()[0], 0)
        raw.execute("DELETE FROM state WHERE id = 20")
        raw.commit()
        raw.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (26, list(range(27, SCHEMA_VERSION + 1))))

    def test_the_reverse_returns_the_file_to_twenty_six_without_the_snoozes(self):
        self._at_version_twenty_six()
        # Through 27 only: 29 adds a `repo` column 27's reverse leaves, and the rerun below adds it again.
        with mock.patch("sd_db.migrate.migrations", return_value=[m for m in schema_module.migrations() if m[0] <= 27]):
            migrate(self.path)
        # The library refuses a file below its version; write it raw.
        connection = sqlite3.connect(self.path)
        connection.execute(
            "INSERT INTO state (id, kind, key, timestamp, body) VALUES (30, 'snooze', 'today:job:x:1', 't6', '{}')")
        connection.commit()
        connection.close()
        text = dict(schema_module.migrations())[27].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 26)
        self.assertEqual(self._rows(raw), self.ROWS)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('snooze', 'today:job:x:1', 't7', '{}')")
        # A backup restore compares the table and its indexes to 26's shape.
        self.assertEqual(self._shape(raw), self._shape(self._reference(26)))
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (26, list(range(27, SCHEMA_VERSION + 1))))


class TheViewKind(SchemaCase):
    """Migration 28 (sd:3430). `view` joins the `state` kinds: a Documents
    file pinned, hidden or tagged. The table is rebuilt as 27 rebuilt it, so
    every row comes through with its id and all four indexes come back. A
    failed rebuild leaves the file at 27, and the reverse returns it to 27's
    shape without the view rows."""

    ROWS = [(7, "heartbeat", "runner", "t1", "{}", None),
            (9, "restore", "2026-09-01", "t2", "{}", "t3"),
            (11, "check", "run-1", "t4", "{}", None),
            (12, "snooze", "today:job:x:1", "t5", "{}", None)]

    def _at_version_twenty_seven(self):
        connection = connect(self.path, create=True, write=True)
        try:
            paths.install(connection)
            for version, path in schema_module.migrations():
                # 27 literally, for the reason `_at_version_nine` gives.
                if version > 27:
                    break
                connection.executescript(
                    f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                    f"PRAGMA user_version = {version};\nCOMMIT;")
            connection.executemany(
                "INSERT INTO state (id, kind, key, timestamp, body, resolved_at) VALUES (?, ?, ?, ?, ?, ?)", self.ROWS)
            connection.commit()
        finally:
            connection.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(
            "SELECT id, kind, key, timestamp, body, resolved_at FROM state ORDER BY id")]

    def _shape(self, connection):
        from sd_db.backup import _schema_objects

        return ([tuple(row) for row in connection.execute("PRAGMA table_info(state)")],
                _schema_objects(connection, ["state"]))

    def _reference(self, upto):
        reference = sqlite3.connect(":memory:")
        self.addCleanup(reference.close)
        paths.install(reference)
        for version, path in schema_module.migrations():
            if version <= upto:
                reference.executescript(path.read_text(encoding="utf-8"))
        return reference

    def test_rows_survive_the_rebuild_and_a_view_is_accepted(self):
        self._at_version_twenty_seven()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (27, list(range(28, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual(self._shape(connection), self._shape(self._reference(27)))
        connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('view', 'system/index.html', 't6', '{}')")
        connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('view', 'system/index.html', 't7', '{}')")
        # 031 dropped the kind CHECK; `TheStateKindList` holds the refusal (sd:3506).
        # The heartbeat's and the check's one-row-per-key indexes came back with the table.
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('heartbeat', 'runner', 't9', '{}')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('check', 'run-1', 't9', '{}')")
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'state_before_%'").fetchone()[0], 0)

    def test_a_failed_rebuild_leaves_the_file_at_twenty_seven_and_a_rerun_finishes_it(self):
        self._at_version_twenty_seven()
        # A row the new CHECK refuses fails the copy, after the rename and the
        # new table: the one way the rebuild can stop half done.
        raw = sqlite3.connect(self.path)
        raw.execute("PRAGMA ignore_check_constraints = ON")
        raw.execute("INSERT INTO state (id, kind, key, timestamp) VALUES (20, 'bored', 'x', 't9')")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.IntegrityError):
            migrate(self.path)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), 27)
        self.assertEqual(self._rows(raw), self.ROWS + [(20, "bored", "x", "t9", None, None)])
        self.assertEqual(self._shape(raw), self._shape(self._reference(27)))
        self.assertEqual(raw.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'state_before_%'").fetchone()[0], 0)
        raw.execute("DELETE FROM state WHERE id = 20")
        raw.commit()
        raw.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (27, list(range(28, SCHEMA_VERSION + 1))))

    def test_the_reverse_returns_the_file_to_twenty_seven_without_the_views(self):
        self._at_version_twenty_seven()
        # Through 28 only: 29 adds a `repo` column 28's reverse leaves, and the rerun below adds it again.
        with mock.patch("sd_db.migrate.migrations", return_value=[m for m in schema_module.migrations() if m[0] <= 28]):
            migrate(self.path)
        # The library refuses a file below its version; write it raw.
        connection = sqlite3.connect(self.path)
        connection.execute(
            "INSERT INTO state (id, kind, key, timestamp, body) VALUES (30, 'view', 'system/index.html', 't6', '{}')")
        connection.commit()
        connection.close()
        text = dict(schema_module.migrations())[28].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 27)
        self.assertEqual(self._rows(raw), self.ROWS)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('view', 'system/index.html', 't7', '{}')")
        # A backup restore compares the table and its indexes to 27's shape.
        self.assertEqual(self._shape(raw), self._shape(self._reference(27)))
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (27, list(range(28, SCHEMA_VERSION + 1))))


class TheChannelsColumn(SchemaCase):
    """Migration 29 (sd:3462, design sd:3471). `repo.channels`, a repository's
    publish channels as a JSON object; NULL is no setting, today's behaviour.

    Added, not rebuilt, as 24 was: every existing row reads NULL and keeps its
    other values, the CHECK holds a value to valid JSON, a failure leaves the
    file at 28, and the reverse returns it to 28's shape.
    """

    COLUMNS = "path, remote, mode, runner_merge, pieces_source, created_at, updated_at, managed, ci, lane_host"
    ROWS = [("/one", "git@github.com:platypeeps/one.git", "full", "auto", "file", "t1", "t2", 1, "local", "build-2"),
            ("/two", None, None, "manual", "row", "t3", "t4", 0, "github", None)]

    def _at_version_twenty_eight(self):
        through = [m for m in schema_module.migrations() if m[0] <= 28]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            migrate(self.path)
        raw = sqlite3.connect(self.path)
        raw.executemany(f"INSERT INTO repo ({self.COLUMNS}) VALUES ({', '.join('?' * 10)})", self.ROWS)
        raw.commit()
        raw.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(f"SELECT {self.COLUMNS} FROM repo ORDER BY path")]

    def _migrate_through_twenty_nine(self):
        # Through 29 only: 30 rebuilds `state`, which 29's reverse leaves.
        through = [m for m in schema_module.migrations() if m[0] <= 29]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            return migrate(self.path)

    def _reference(self, upto):
        reference = sqlite3.connect(":memory:")
        self.addCleanup(reference.close)
        paths.install(reference)
        for version, path in schema_module.migrations():
            if version <= upto:
                reference.executescript(path.read_text(encoding="utf-8"))
        return reference

    def test_every_existing_row_arrives_at_null_and_keeps_its_values_and_a_rerun_applies_nothing(self):
        self._at_version_twenty_eight()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (28, list(range(29, SCHEMA_VERSION + 1))))
        self.assertEqual(migrate(self.path).applied, [])
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual([row[0] for row in connection.execute("SELECT channels FROM repo ORDER BY path")],
                         [None, None])

    def test_the_check_holds_a_value_to_json(self):
        self._at_version_twenty_eight()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        connection.execute("UPDATE repo SET channels = ? WHERE path = '/one'", ('{"drive": {}}',))
        for value in ("obsidian", "{", ""):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE repo SET channels = ? WHERE path = '/one'", (value,))
        connection.execute("UPDATE repo SET channels = NULL WHERE path = '/two'")
        self.assertEqual([row[0] for row in connection.execute("SELECT channels FROM repo ORDER BY path")],
                         ['{"drive": {}}', None])

    def test_a_failed_add_leaves_the_file_at_twenty_eight_and_a_rerun_finishes_it(self):
        self._at_version_twenty_eight()
        # A column of that name added by hand is the one way the one statement fails.
        raw = sqlite3.connect(self.path)
        raw.execute("ALTER TABLE repo ADD COLUMN channels TEXT")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            self._migrate_through_twenty_nine()
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), 28)
        self.assertEqual(self._rows(raw), self.ROWS)
        raw.execute("ALTER TABLE repo DROP COLUMN channels")
        raw.commit()
        raw.close()
        self.assertEqual(self._migrate_through_twenty_nine().applied, [29])

    def test_the_reverse_returns_the_file_to_twenty_eight(self):
        self._at_version_twenty_eight()
        self._migrate_through_twenty_nine()
        # The library refuses a write below its version; write it raw.
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE repo SET channels = ? WHERE path = '/one'", ('{"drive": {}}',))
        connection.commit()
        connection.close()
        text = dict(schema_module.migrations())[29].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 28)
        self.assertEqual(self._rows(raw), self.ROWS)
        # A backup restore compares the table to 28's shape.
        self.assertEqual(raw.execute("PRAGMA table_info(repo)").fetchall(),
                         self._reference(28).execute("PRAGMA table_info(repo)").fetchall())
        self.assertEqual(self._migrate_through_twenty_nine().applied, [29])


class TheSettingKind(SchemaCase):
    """Migration 30 (sd:3989, design sd:3983 slice P1). `setting` joins the
    `state` kinds: a value one row serves every machine. The table is rebuilt
    as 28 rebuilt it, so every row comes through with its id and all four
    indexes come back. A failed rebuild leaves the file at 29, and the
    reverse returns it to 29's shape without the setting rows."""

    ROWS = [(7, "heartbeat", "runner", "t1", "{}", None),
            (9, "restore", "2026-09-01", "t2", "{}", "t3"),
            (11, "check", "run-1", "t4", "{}", None),
            (12, "snooze", "today:job:x:1", "t5", "{}", None),
            (13, "view", "system/index.html", "t6", "{}", None)]

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(
            "SELECT id, kind, key, timestamp, body, resolved_at FROM state ORDER BY id")]

    def _shape(self, connection):
        from sd_db.backup import _schema_objects

        return ([tuple(row) for row in connection.execute("PRAGMA table_info(state)")],
                _schema_objects(connection, ["state"]))

    def _reference(self, upto):
        reference = sqlite3.connect(":memory:")
        self.addCleanup(reference.close)
        paths.install(reference)
        for version, path in schema_module.migrations():
            if version <= upto:
                reference.executescript(path.read_text(encoding="utf-8"))
        return reference

    def _at_version_twenty_nine(self):
        through = [m for m in schema_module.migrations() if m[0] <= 29]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            migrate(self.path)
        raw = sqlite3.connect(self.path)
        raw.executemany(
            "INSERT INTO state (id, kind, key, timestamp, body, resolved_at) VALUES (?, ?, ?, ?, ?, ?)", self.ROWS)
        raw.commit()
        raw.close()

    def test_rows_survive_the_rebuild_and_a_setting_is_accepted(self):
        self._at_version_twenty_nine()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (29, list(range(30, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual(self._shape(connection), self._shape(self._reference(29)))
        connection.execute(
            "INSERT INTO state (kind, key, timestamp, body) VALUES ('setting', 'sd.copilot_review', 't7', '{}')")
        # 031 dropped the kind CHECK; `TheStateKindList` holds the refusal (sd:3506).
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('heartbeat', 'runner', 't9', '{}')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('check', 'run-1', 't9', '{}')")
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'state_before_%'").fetchone()[0], 0)

    def test_a_failed_rebuild_leaves_the_file_at_twenty_nine_with_every_row_and_a_rerun_finishes_it(self):
        self._at_version_twenty_nine()
        # A row the new CHECK refuses fails the copy, after the rename and the
        # new table: the one way the rebuild can stop half done.
        raw = sqlite3.connect(self.path)
        raw.execute("PRAGMA ignore_check_constraints = ON")
        raw.execute("INSERT INTO state (id, kind, key, timestamp) VALUES (20, 'bored', 'x', 't9')")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.IntegrityError):
            migrate(self.path)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), 29)
        self.assertEqual(self._rows(raw), self.ROWS + [(20, "bored", "x", "t9", None, None)])
        self.assertEqual(self._shape(raw), self._shape(self._reference(29)))
        self.assertEqual(raw.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'state_before_%'").fetchone()[0], 0)
        raw.execute("DELETE FROM state WHERE id = 20")
        raw.commit()
        raw.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (29, list(range(30, SCHEMA_VERSION + 1))))

    def test_the_reverse_returns_the_file_to_twenty_nine_without_the_settings(self):
        self._at_version_twenty_nine()
        self.migrate_through(30)
        # The library refuses a write below its version; write it raw.
        connection = sqlite3.connect(self.path)
        connection.execute(
            "INSERT INTO state (id, kind, key, timestamp, body) VALUES (30, 'setting', 'sd.copilot_review', 't7', "
            "'{\"value\": \"never\"}')")
        connection.commit()
        connection.close()
        text = dict(schema_module.migrations())[30].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 29)
        self.assertEqual(self._rows(raw), self.ROWS)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('setting', 'sd.x', 't8', '{}')")
        # A backup restore compares the table and its indexes to 29's shape.
        self.assertEqual(self._shape(raw), self._shape(self._reference(29)))
        result = self.migrate_through(30)
        self.assertEqual((result.before, result.applied), (29, [30]))


class TheStateKindList(SchemaCase):
    """Migration 31 (sd:3506). `state.kind` loses its CHECK, edited in place
    as 9 edited `item.kind`'s: no row moves and the table keeps its pages.
    `writes.STATE_KINDS` is the one list, and `record_state` refuses a kind
    outside it. An unknown shape is refused at 30; a replay is a no-op; the
    reverse puts 30's CHECK back."""

    ROWS = TheSettingKind.ROWS + [(14, "setting", "sd.copilot_review", "t7", '{"value": "never"}', None)]

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(
            "SELECT id, kind, key, timestamp, body, resolved_at FROM state ORDER BY id")]

    def _sql(self, connection):
        return connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'state'").fetchone()[0]

    def _reference(self, upto):
        return TheSettingKind._reference(self, upto)  # type: ignore[arg-type]

    def _at_version_thirty(self):
        through = [m for m in schema_module.migrations() if m[0] <= 30]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            migrate(self.path)
        raw = sqlite3.connect(self.path)
        raw.executemany(
            "INSERT INTO state (id, kind, key, timestamp, body, resolved_at) VALUES (?, ?, ?, ?, ?, ?)", self.ROWS)
        raw.commit()
        raw.close()

    def test_the_check_goes_with_no_row_moved_and_the_library_list_refuses(self):
        from sd_db.errors import SdDbError
        from sd_db.writes import record_state

        self._at_version_thirty()
        raw = sqlite3.connect(self.path)
        page = raw.execute("SELECT rootpage FROM sqlite_master WHERE name = 'state'").fetchone()[0]
        raw.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied), (30, list(range(31, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        # The same root page: the table was not rebuilt.
        self.assertEqual(connection.execute("SELECT rootpage FROM sqlite_master WHERE name = 'state'").fetchone()[0], page)
        self.assertNotIn("CHECK (kind", self._sql(connection))
        self.assertEqual(self._sql(connection), self._sql(self._reference(SCHEMA_VERSION)))
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with self.assertRaises(SdDbError):
            record_state(connection, "bored", key="x")
        connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('bored', 'x', 't8', '{}')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES (NULL, 'x', 't8', '{}')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('heartbeat', 'runner', 't9', '{}')")

    def test_every_raw_insert_names_a_listed_kind(self):
        """The CHECK guarded the inserts that skip `record_state`; this guards them now."""
        from sd_db.writes import STATE_KINDS

        sources = [*PACKAGE_ROOT.rglob("*.py"), *(PACKAGE_ROOT.parent / "bin").glob("*.py"),
                   *(path for path in (PACKAGE_ROOT.parent / "bin").iterdir() if path.suffix == "" and path.is_file())]
        found = []
        for path in sources:
            text = path.read_text(encoding="utf-8", errors="replace")
            inserts = re.findall(r"INTO state\s*\(", text)
            kinds = re.findall(r"INTO state\s*\(\s*kind\b[^)]*\)\s*VALUES\s*\(\s*(?:'([a-z]+)'|(\?))", text)
            self.assertEqual(len(kinds), len(inserts), f"{path}: an insert into state this test cannot read")
            for literal, placeholder in kinds:
                if placeholder:
                    self.assertEqual(path.name, "writes.py", f"{path}: a state kind from a variable skips record_state")
                else:
                    self.assertIn(literal, STATE_KINDS, f"{path}: {literal!r} is not in STATE_KINDS")
                found.append(literal or placeholder)
        self.assertGreaterEqual(len(found), 6)

    #: 30's kind column, as 030 wrote it.
    KIND_030 = ("    kind            TEXT NOT NULL\n"
                "                    CHECK (kind IN ('checkpoint', 'verified', 'restore',\n"
                "                                    'watermark', 'heartbeat', 'check',\n"
                "                                    'snooze', 'view', 'setting')),")

    def test_an_unknown_state_shape_is_refused_and_left_at_thirty(self):
        shapes = {
            "another order": ("'view', 'setting'", "'setting', 'view'"),
            # The column's CHECK matches; a second, table-level one would survive the edit.
            "a second CHECK": ("    resolved_at     TEXT\n)", "    resolved_at     TEXT,\n    CHECK (kind <> 'bored')\n)"),
            # No CHECK and a nullable kind: the refusal cannot hang on `state`'s constraints.
            "a nullable kind": (self.KIND_030, "    kind            TEXT,"),
        }
        for shape, (old, new) in shapes.items():
            with self.subTest(shape=shape):
                self.path.unlink(missing_ok=True)
                self._at_version_thirty()
                raw = sqlite3.connect(self.path, isolation_level=None)
                raw.execute("PRAGMA writable_schema = ON")
                raw.execute("UPDATE sqlite_master SET sql = replace(sql, ?, ?) WHERE name = 'state'", (old, new))
                raw.execute("PRAGMA writable_schema = RESET")
                before = self._sql(raw)
                self.assertNotEqual(before, self._sql(self._reference(30)))
                raw.close()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "migration_031_refuses_an_unknown_state_shape"):
                    migrate(self.path)
                raw = sqlite3.connect(self.path)
                self.assertEqual(schema_version(raw), 30)
                self.assertEqual(self._sql(raw), before)
                self.assertEqual(self._rows(raw), self.ROWS)
                self.assertEqual(raw.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                raw.close()

    def test_a_replay_on_the_new_shape_changes_nothing(self):
        """A restore replays migrations onto a snapshot that may carry 31's shape already."""
        initialise(self.path)
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        before = self._sql(raw)
        raw.executescript("BEGIN;\n" + dict(schema_module.migrations())[31].read_text(encoding="utf-8") + "\nCOMMIT;")
        self.assertEqual(self._sql(raw), before)

    def test_the_reverse_puts_thirtys_check_back(self):
        self._at_version_thirty()
        self.migrate_through(31)
        text = dict(schema_module.migrations())[31].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 30)
        self.assertEqual(self._rows(raw), self.ROWS)
        self.assertEqual(self._sql(raw), self._sql(self._reference(30)))
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('bored', 'x', 't8', '{}')")
        result = self.migrate_through(31)
        self.assertEqual((result.before, result.applied), (30, [31]))


class TheDeadItemKinds(SchemaCase):
    """Migration 32 (sd:3510). `proposal` and `dep` leave `item.kind`'s CHECK,
    edited in place as 9 widened it: no producer writes either and the hub
    holds no row of either. A row of either refuses the migration at 31, as
    does an unknown shape; a replay is a no-op; the reverse widens it back."""

    KINDS = ("work", "idea", "task", "report", "skill-review", "personal", "followup", "work-idea", "personal-idea")

    def _sql(self, connection):
        return connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'item'").fetchone()[0]

    def _reference(self, upto):
        return TheSettingKind._reference(self, upto)  # type: ignore[arg-type]

    def _at_version_thirty_one(self, *kinds):
        through = [m for m in schema_module.migrations() if m[0] <= 31]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            migrate(self.path)
        raw = sqlite3.connect(self.path)
        raw.executemany("INSERT INTO item (kind, title, status, created_at, updated_at) VALUES (?, ?, 'planning', 't', 't')",
                        [(kind, kind) for kind in kinds])
        raw.commit()
        raw.close()

    def _kinds(self, connection):
        from sd_db.workflow import schema_kinds

        connection.row_factory = sqlite3.Row
        return schema_kinds(connection)

    def test_the_two_kinds_go_and_every_row_stays(self):
        self._at_version_thirty_one(*self.KINDS)
        raw = sqlite3.connect(self.path)
        page = raw.execute("SELECT rootpage FROM sqlite_master WHERE name = 'item'").fetchone()[0]
        raw.close()
        # Through 45: 46 moves two of these kinds on (`TheRetiredItemKinds`).
        result = self.migrate_through(45)
        self.assertEqual((result.before, result.applied), (31, list(range(32, 46))))
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(self._kinds(connection), self.KINDS)
        self.assertEqual(sorted(row[0] for row in connection.execute("SELECT kind FROM item")), sorted(self.KINDS))
        self.assertEqual(connection.execute("SELECT rootpage FROM sqlite_master WHERE name = 'item'").fetchone()[0], page)
        self.assertEqual(self._sql(connection), self._sql(self._reference(45)))
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        for dead in ("proposal", "dep"):
            with self.subTest(kind=dead), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO item (kind, title, status, created_at, updated_at) "
                                   "VALUES (?, 't', 'planning', 't', 't')", (dead,))

    def test_a_row_of_a_dead_kind_refuses_the_migration_at_thirty_one(self):
        for dead in ("proposal", "dep"):
            with self.subTest(kind=dead):
                self.path.unlink(missing_ok=True)
                self._at_version_thirty_one("task", dead)
                raw = sqlite3.connect(self.path)
                before = self._sql(raw)
                raw.close()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "migration_032_refuses_a_dead_kind_row"):
                    migrate(self.path)
                raw = sqlite3.connect(self.path)
                self.assertEqual(schema_version(raw), 31)
                self.assertEqual(self._sql(raw), before)
                self.assertEqual(sorted(row[0] for row in raw.execute("SELECT kind FROM item")), sorted(["task", dead]))
                self.assertEqual(raw.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                raw.close()

    def test_an_unknown_item_shape_is_refused_and_left_at_thirty_one(self):
        shapes = {
            "another order": lambda sql: sql.replace("'proposal', 'skill-review', 'dep'",
                                                     "'skill-review', 'proposal', 'dep'"),
            # The expected lines are there; a `dep` after them would survive the edit.
            "a dep at the end": lambda sql: sql.replace("'personal-idea'))", "'personal-idea', 'dep'))"),
            # A second CHECK on the kind, spelt another way, would survive the edit.
            "a second kind CHECK": lambda sql: sql[:-1] + ",\n    check (kind in ('work', 'task')))",
            # No kind CHECK: the refusal cannot hang on `item`'s constraints.
            "no kind CHECK": lambda sql: re.sub(r"\n\s*CHECK \(kind IN \([^)]*\)\)", "", sql),
        }
        for shape, change in shapes.items():
            with self.subTest(shape=shape):
                self.path.unlink(missing_ok=True)
                self._at_version_thirty_one("task")
                raw = sqlite3.connect(self.path, isolation_level=None)
                old = self._sql(raw)
                raw.execute("PRAGMA writable_schema = ON")
                raw.execute("UPDATE sqlite_master SET sql = ? WHERE name = 'item'", (change(old),))
                raw.execute("PRAGMA writable_schema = RESET")
                before = self._sql(raw)
                self.assertNotEqual(before, old)
                raw.close()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "migration_032_refuses_a_dead_kind_row"):
                    migrate(self.path)
                raw = sqlite3.connect(self.path)
                self.assertEqual(schema_version(raw), 31)
                self.assertEqual(self._sql(raw), before)
                self.assertEqual([row[0] for row in raw.execute("SELECT kind FROM item")], ["task"])
                raw.close()

    def test_a_replay_on_the_new_shape_changes_nothing(self):
        self.migrate_through(32)
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        before = self._sql(raw)
        raw.executescript("BEGIN;\n" + dict(schema_module.migrations())[32].read_text(encoding="utf-8") + "\nCOMMIT;")
        self.assertEqual(self._sql(raw), before)

    def test_the_reverse_widens_the_list_back(self):
        self._at_version_thirty_one("task")
        self.migrate_through(32)
        text = dict(schema_module.migrations())[32].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), 31)
        self.assertEqual(self._sql(raw), self._sql(self._reference(31)))
        self.assertIn("dep", self._kinds(raw))
        raw.execute("INSERT INTO item (kind, title, status, created_at, updated_at) VALUES ('dep', 't', 'planning', 't', 't')")
        raw.execute("DELETE FROM item WHERE kind = 'dep'")
        result = self.migrate_through(32)
        self.assertEqual((result.before, result.applied), (31, [32]))


class TheRetiredItemKinds(SchemaCase):
    """Migration 46 (sd:7004). `personal` and `skill-review` rows become
    `task` rows and both words leave `item.kind`'s CHECK, edited in place as
    32 did. One transaction: a failure leaves 45 untouched; an unknown shape
    is refused; a replay changes nothing; the reverse widens the list back."""

    VERSION = 46
    BEFORE = ("work", "idea", "task", "report", "skill-review", "personal", "followup", "work-idea", "personal-idea")
    AFTER = ("work", "idea", "task", "report", "followup", "work-idea", "personal-idea")

    def _sql(self, connection):
        return connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'item'").fetchone()[0]

    def _reference(self, upto):
        return TheSettingKind._reference(self, upto)  # type: ignore[arg-type]

    def _body(self):
        return dict(schema_module.migrations())[self.VERSION].read_text(encoding="utf-8")

    def _at_forty_five(self, *kinds):
        self.migrate_through(self.VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.execute("INSERT INTO repo (path, created_at, updated_at) VALUES ('/repo', 't', 't')")
        raw.executemany("INSERT INTO item (kind, repo, title, status, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'done', 't1', 't2')",
                        [(kind, None if kind == "personal" else "/repo", f"a {kind}") for kind in kinds])
        raw.execute("INSERT INTO note (item, timestamp, kind, body) SELECT id, 't', 'comment', title FROM item")
        raw.commit()
        raw.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(
            "SELECT id, kind, repo, title, status, updated_at FROM item ORDER BY id")]

    def _kinds(self, connection):
        from sd_db.workflow import schema_kinds

        connection.row_factory = sqlite3.Row
        return schema_kinds(connection)

    def test_rows_of_the_retired_kinds_become_tasks_and_the_check_narrows(self):
        self._at_forty_five(*self.BEFORE)
        raw = sqlite3.connect(self.path)
        before = self._rows(raw)
        notes = raw.execute("SELECT count(*) FROM note").fetchone()[0]
        raw.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (self.VERSION - 1, list(range(self.VERSION, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._kinds(connection), self.AFTER)
        moved = {"personal": "task", "skill-review": "task"}
        self.assertEqual(self._rows(connection),
                         [(id_, moved.get(kind, kind), *rest) for id_, kind, *rest in before])
        self.assertEqual(connection.execute("SELECT count(*) FROM note").fetchone()[0], notes)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self._sql(connection), self._sql(self._reference(SCHEMA_VERSION)))
        self.assertEqual(connection.execute("PRAGMA writable_schema").fetchone()[0], 0)
        self.assertEqual([row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'migration_046%'")], [])

    def test_a_retired_kind_is_refused_after_the_migration(self):
        initialise(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        for retired in ("personal", "skill-review"):
            with self.subTest(kind=retired), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO item (kind, title, status, created_at, updated_at) "
                                   "VALUES (?, 't', 'planning', 't', 't')", (retired,))

    def test_a_second_migrate_applies_nothing(self):
        self._at_forty_five("personal")
        migrate(self.path)
        self.assertEqual(migrate(self.path).applied, [])

    def test_a_failure_after_the_update_leaves_forty_five_and_a_rerun_finishes(self):
        """Failure table, row "Crash after the `UPDATE`": the abort comes
        from the second guard, after the rows moved, so the transaction is
        what puts them back."""
        self._at_forty_five("personal", "skill-review", "task")
        raw = sqlite3.connect(self.path)
        before, sql = self._rows(raw), self._sql(raw)
        raw.close()
        # The UPDATE runs; the abort comes after it, from inside the file.
        body = self._body().replace("DROP TABLE migration_046_guard;",
                                    "INSERT INTO migration_046_guard VALUES ('stopped');\nDROP TABLE migration_046_guard;")
        self.assertNotEqual(body, self._body())
        path = self.home / "046_stopped.sql"
        path.write_text(body, encoding="utf-8")
        through = [m for m in schema_module.migrations() if m[0] < self.VERSION] + [(self.VERSION, path)]
        with mock.patch("sd_db.migrate.migrations", return_value=through), \
                self.assertRaisesRegex(sqlite3.IntegrityError, "migration_046_refuses"):
            migrate(self.path)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual((self._rows(raw), self._sql(raw)), (before, sql))
        raw.close()
        result = self.migrate_through(self.VERSION)
        self.assertEqual(result.applied, [self.VERSION])
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        self.assertEqual(sorted(row[0] for row in raw.execute("SELECT kind FROM item")), ["task", "task", "task"])

    def test_an_unknown_item_shape_is_refused_and_left_at_forty_five(self):
        shapes = {
            "another order": lambda sql: sql.replace("'personal', 'followup'", "'followup', 'personal'"),
            "a personal at the end": lambda sql: sql.replace(
                "'skill-review',\n                                    'personal', 'followup',",
                "'followup',").replace("'personal-idea'))", "'personal-idea', 'personal'))"),
            "a second kind CHECK": lambda sql: sql[:-1] + ",\n    check (kind in ('personal', 'task')))",
            "no kind CHECK": lambda sql: re.sub(r"\n\s*CHECK \(kind IN \([^)]*\)\)", "", sql),
        }
        for shape, change in shapes.items():
            with self.subTest(shape=shape):
                self.path.unlink(missing_ok=True)
                self._at_forty_five("task")
                raw = sqlite3.connect(self.path, isolation_level=None)
                old = self._sql(raw)
                raw.execute("PRAGMA writable_schema = ON")
                raw.execute("UPDATE sqlite_master SET sql = ? WHERE name = 'item'", (change(old),))
                raw.execute("PRAGMA writable_schema = RESET")
                before = self._sql(raw)
                self.assertNotEqual(before, old)
                raw.close()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "migration_046_refuses"):
                    migrate(self.path)
                raw = sqlite3.connect(self.path)
                self.assertEqual(schema_version(raw), self.VERSION - 1)
                self.assertEqual(self._sql(raw), before)
                raw.close()

    def test_a_replay_on_the_new_shape_changes_nothing(self):
        self._at_forty_five("personal", "task")
        migrate(self.path)
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        before, rows = self._sql(raw), self._rows(raw)
        raw.executescript("BEGIN;\n" + self._body() + "\nCOMMIT;")
        self.assertEqual((self._sql(raw), self._rows(raw)), (before, rows))

    def test_a_restore_of_an_older_snapshot_keeps_every_count(self):
        """A restore replays 46 onto a snapshot at 45 and refuses a changed
        row count (`backup._upgrade_restore_candidate`)."""
        from sd_db.backup import _upgrade_restore_candidate

        self._at_forty_five("personal", "skill-review", "task")
        _upgrade_restore_candidate(self.path)
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        self.assertEqual(schema_version(raw), SCHEMA_VERSION)
        self.assertEqual(sorted(row[0] for row in raw.execute("SELECT kind FROM item")), ["task", "task", "task"])

    def test_the_reverse_widens_the_list_back(self):
        self._at_forty_five("personal")
        self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in self._body().splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._sql(raw), self._sql(self._reference(self.VERSION - 1)))
        self.assertEqual(self._kinds(raw), self.BEFORE)
        # The moved row stays a task: the migration recorded no original kind.
        self.assertEqual([row[0] for row in raw.execute("SELECT kind FROM item")], ["task"])
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])


class TheConnection(SchemaCase):
    def test_wal_and_foreign_keys_are_on(self):
        initialise(self.path)
        connection = connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_a_missing_database_is_not_created_silently(self):
        with self.assertRaises(FileNotFoundError) as raised:
            connect(self.home / ".local/share/sd/absent.db")
        self.assertIn("`bin/sd-db init` from the command pack", str(raised.exception))



class TheClonesAndDependabotColumns(SchemaCase):
    """Migration 33 (sd:4043, design sd:3983 slice P2). `repo.clone_profiles`,
    the machine kinds `repo-sync` clones a repository on, and `repo.dependabot`,
    the sweep's allowed list outside the owner rule.

    Added, not rebuilt, as 29 was: every existing row reads NULL and 0 and
    keeps its other values, each CHECK holds a closed list, a failure in the
    second statement leaves the file at 32 without the first column, and the
    reverse returns it to 32's shape.
    """

    VERSION = 33
    COLUMNS = ("path, remote, mode, runner_merge, pieces_source, created_at, updated_at, managed, ci, lane_host, "
               "channels")
    ROWS = [("/one", "git@github.com:platypeeps/one.git", "full", "auto", "file", "t1", "t2", 1, "local", "build-2",
             '{"drive": {}}'),
            ("/two", None, None, "manual", "row", "t3", "t4", 0, "github", None, None)]

    def _before(self):
        through = [m for m in schema_module.migrations() if m[0] < self.VERSION]
        with mock.patch("sd_db.migrate.migrations", return_value=through):
            migrate(self.path)
        raw = sqlite3.connect(self.path)
        raw.executemany(f"INSERT INTO repo ({self.COLUMNS}) VALUES ({', '.join('?' * 11)})", self.ROWS)
        raw.commit()
        raw.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(f"SELECT {self.COLUMNS} FROM repo ORDER BY path")]

    def _added(self, connection):
        return [tuple(row) for row in connection.execute("SELECT clone_profiles, dependabot FROM repo ORDER BY path")]

    def _reference(self, upto):
        reference = sqlite3.connect(":memory:")
        self.addCleanup(reference.close)
        paths.install(reference)
        for version, path in schema_module.migrations():
            if version <= upto:
                reference.executescript(path.read_text(encoding="utf-8"))
        return reference

    def test_every_existing_row_arrives_at_none_and_no_and_keeps_its_values(self):
        self._before()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (self.VERSION - 1, list(range(self.VERSION, SCHEMA_VERSION + 1))))
        self.assertEqual(migrate(self.path).applied, [])
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual(self._added(connection), [(None, 0), (None, 0)])

    def test_each_check_holds_a_closed_list(self):
        self._before()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        for value in ("personal", "work", "terra", "personal,work", "personal,terra", "work,terra",
                      "personal,work,terra", None):
            connection.execute("UPDATE repo SET clone_profiles = ? WHERE path = '/one'", (value,))
        for value in ("work,personal", "laptop", "", "personal,personal", "personal, work"):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE repo SET clone_profiles = ? WHERE path = '/one'", (value,))
        connection.execute("UPDATE repo SET dependabot = 1 WHERE path = '/two'")
        for flag in (2, -1, None):
            with self.subTest(flag=flag), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE repo SET dependabot = ? WHERE path = '/two'", (flag,))
        self.assertEqual(self._added(connection), [(None, 0), (None, 1)])

    def test_a_failed_second_add_leaves_the_file_at_the_prior_version_without_either_and_a_rerun_finishes_it(self):
        """Failure table, row "migration: stop mid-file"."""
        self._before()
        # A column of that name added by hand fails the second statement, after the first has run.
        raw = sqlite3.connect(self.path)
        raw.execute("ALTER TABLE repo ADD COLUMN dependabot INTEGER")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        self.assertNotIn("clone_profiles", [row[1] for row in raw.execute("PRAGMA table_info(repo)")])
        raw.execute("ALTER TABLE repo DROP COLUMN dependabot")
        raw.commit()
        raw.close()
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])

    def test_the_reverse_returns_the_file_to_the_prior_version(self):
        self._before()
        self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE repo SET clone_profiles = 'personal,work', dependabot = 1 WHERE path = '/one'")
        raw.commit()
        raw.close()
        text = dict(schema_module.migrations())[self.VERSION].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        self.assertEqual(raw.execute("PRAGMA table_info(repo)").fetchall(),
                         self._reference(self.VERSION - 1).execute("PRAGMA table_info(repo)").fetchall())
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])



class TheShadowReasonColumn(SchemaCase):
    """Migration 34 (sd:4636, decision on sd:4628). `shadow.reason` says why a
    synced row is the operator's: `assigned`, `filed`, or `watched` for every
    other match. Added, not rebuilt: every existing row reads NULL, which is
    unknown and not watched, keeps its other values, the CHECK holds a closed
    list, and the reverse returns the table to 33's shape."""

    VERSION = 34
    COLUMNS = "id, tracker, repo, url, number, kind, title, state, author, first_seen, last_seen"
    ROWS = [(1, "github", "example/project", "https://github.com/example/project/issues/1", 1, "issue", "one",
             "open", "octocat", "t1", "t2"),
            (2, "jira", "EXAMPLE", "https://example.test/browse/EXAMPLE-1", None, "issue", "two", "closed",
             "Someone", "t3", "t4")]

    def _before(self):
        self.migrate_through(self.VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.executemany(f"INSERT INTO shadow ({self.COLUMNS}) VALUES ({', '.join('?' * 11)})", self.ROWS)
        raw.commit()
        raw.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(f"SELECT {self.COLUMNS} FROM shadow ORDER BY id")]

    def test_every_existing_row_arrives_unknown_and_keeps_its_values(self):
        self._before()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (self.VERSION - 1, list(range(self.VERSION, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual([row[0] for row in connection.execute("SELECT reason FROM shadow ORDER BY id")],
                         [None, None])

    def test_the_check_holds_a_closed_list(self):
        self._before()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        for value in ("assigned", "filed", "watched", None):
            connection.execute("UPDATE shadow SET reason = ? WHERE id = 1", (value,))
        for value in ("author", "mentioned", "watching", "", "Assigned"):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE shadow SET reason = ? WHERE id = 1", (value,))

    def test_the_reverse_returns_the_file_to_the_prior_version(self):
        self._before()
        self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE shadow SET reason = 'assigned' WHERE id = 1")
        raw.commit()
        raw.close()
        text = dict(schema_module.migrations())[self.VERSION].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        self.assertNotIn("reason", [row[1] for row in raw.execute("PRAGMA table_info(shadow)")])
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])


class TheShadowSettledColumns(SchemaCase):
    """Migration 38 (sd:5303, sd:5315). `shadow.merged_at` and `shadow.closed_at`:
    when the tracker merged or closed a row; `shadow.branch` and
    `shadow.linked_issues`: a pull request's head branch and the urls of the
    issues it closes. Added, not rebuilt: every existing row reads NULL, which is
    unknown until a collect sees it again, keeps its other values, the CHECK
    holds a JSON array or NULL, and the reverse returns the table to 37's shape."""

    VERSION = 38
    COLUMNS = TheShadowReasonColumn.COLUMNS + ", reason"
    ROWS = [(*row, reason) for row, reason in zip(TheShadowReasonColumn.ROWS, ("filed", None), strict=True)]

    def _before(self):
        self.migrate_through(self.VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.executemany(f"INSERT INTO shadow ({self.COLUMNS}) VALUES ({', '.join('?' * 12)})", self.ROWS)
        raw.commit()
        raw.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(f"SELECT {self.COLUMNS} FROM shadow ORDER BY id")]

    def test_every_existing_row_arrives_unknown_and_keeps_its_values(self):
        self._before()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (self.VERSION - 1, list(range(self.VERSION, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual([tuple(row) for row in connection.execute(
            "SELECT merged_at, closed_at, branch, linked_issues FROM shadow ORDER BY id")],
                         [(None, None, None, None), (None, None, None, None)])

    def test_the_check_holds_a_json_array_or_null(self):
        self._before()
        migrate(self.path)
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        for value in ("[]", '["https://github.com/example/project/issues/1"]', None):
            connection.execute("UPDATE shadow SET linked_issues = ? WHERE id = 1", (value,))
        for value in ("", "{}", '"x"', "1", "[", "null"):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE shadow SET linked_issues = ? WHERE id = 1", (value,))

    def test_a_failed_file_leaves_the_store_at_37_and_a_rerun_finishes_it(self):
        """Steps: the file's last statement fails after the three before it; the transaction leaves none."""
        self._before()
        raw = sqlite3.connect(self.path)
        raw.execute("ALTER TABLE shadow ADD COLUMN linked_issues TEXT")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        columns = [row[1] for row in raw.execute("PRAGMA table_info(shadow)")]
        for column in ("merged_at", "closed_at", "branch"):
            self.assertNotIn(column, columns)
        raw.execute("ALTER TABLE shadow DROP COLUMN linked_issues")
        raw.commit()
        raw.close()
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])

    def test_the_reverse_returns_the_file_to_the_prior_version(self):
        self._before()
        self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE shadow SET merged_at = 'x', closed_at = 'y', branch = 'b', linked_issues = '[]' WHERE id = 1")
        raw.commit()
        raw.close()
        text = dict(schema_module.migrations())[self.VERSION].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        columns = [row[1] for row in raw.execute("PRAGMA table_info(shadow)")]
        for column in ("merged_at", "closed_at", "branch", "linked_issues"):
            self.assertNotIn(column, columns)
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])


class TheShadowUpdatedColumn(SchemaCase):
    """Migration 39 (pack sd:5357). `shadow.updated_at`: when the tracker last
    updated a row, so `sd-status` can list the Jira rows touched in the last
    `sd.jira_recent_days`. Added, not rebuilt: every existing row reads NULL,
    unknown until a collect sees it again, and keeps its other values; the
    reverse returns the table to 38's shape."""

    VERSION = 39
    COLUMNS = TheShadowSettledColumns.COLUMNS + ", merged_at, closed_at, branch, linked_issues"
    ROWS = [(*row, "2026-09-01T00:00:00Z", None, "fix/one", "[]") for row in TheShadowSettledColumns.ROWS]

    def _before(self):
        self.migrate_through(self.VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.executemany(f"INSERT INTO shadow ({self.COLUMNS}) VALUES ({', '.join('?' * 16)})", self.ROWS)
        raw.commit()
        raw.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(f"SELECT {self.COLUMNS} FROM shadow ORDER BY id")]

    def test_every_existing_row_arrives_unknown_and_keeps_its_values(self):
        self._before()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (self.VERSION - 1, list(range(self.VERSION, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual([row[0] for row in connection.execute("SELECT updated_at FROM shadow ORDER BY id")],
                         [None, None])

    def test_the_reverse_returns_the_file_to_the_prior_version(self):
        self._before()
        self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE shadow SET updated_at = '2026-10-05T16:12:33Z' WHERE id = 1")
        raw.commit()
        raw.close()
        text = dict(schema_module.migrations())[self.VERSION].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        self.assertNotIn("updated_at", [row[1] for row in raw.execute("PRAGMA table_info(shadow)")])
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])


class TheShadowFiledColumns(SchemaCase):
    """Migration 40 (pack sd:5496). `shadow.created_at`, `shadow.filed`, `shadow.status` and `shadow.assignee`: when
    the tracker created a row, whether the operator filed it (kept apart from `reason`, which names only the strongest
    of assigned and filed), Jira's status name and who it is assigned to. Added, not rebuilt: every existing row reads
    NULL, unknown until a collect sees it again, and keeps its other values; `filed` holds 0, 1 or NULL; the reverse
    returns the table to 39's shape."""

    VERSION = 40
    COLUMNS = TheShadowUpdatedColumn.COLUMNS + ", updated_at"
    ROWS = [(*row, "2026-09-02T00:00:00Z") for row in TheShadowUpdatedColumn.ROWS]
    ADDED = ("created_at", "filed", "status", "assignee")

    def _before(self):
        self.migrate_through(self.VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.executemany(f"INSERT INTO shadow ({self.COLUMNS}) VALUES ({', '.join('?' * 17)})", self.ROWS)
        raw.commit()
        raw.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(f"SELECT {self.COLUMNS} FROM shadow ORDER BY id")]

    def test_every_existing_row_arrives_unknown_and_keeps_its_values(self):
        self._before()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (self.VERSION - 1, list(range(self.VERSION, SCHEMA_VERSION + 1))))
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual([tuple(row) for row in connection.execute(f"SELECT {', '.join(self.ADDED)} FROM shadow ORDER BY id")],
                         [(None,) * 4, (None,) * 4])

    def test_filed_holds_zero_one_or_unknown(self):
        self._before()
        self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        for value in (0, 1, None):
            raw.execute("UPDATE shadow SET filed = ? WHERE id = 1", (value,))
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("UPDATE shadow SET filed = 2 WHERE id = 1")

    def test_the_reverse_returns_the_file_to_the_prior_version(self):
        self._before()
        self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE shadow SET created_at = '2026-01-05T16:12:33Z', filed = 1, status = 'In Review', "
                    "assignee = 'Alex' WHERE id = 1")
        raw.commit()
        raw.close()
        text = dict(schema_module.migrations())[self.VERSION].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        columns = [row[1] for row in raw.execute("PRAGMA table_info(shadow)")]
        self.assertEqual([name for name in self.ADDED if name in columns], [])
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])


class TheGithubIssuesColumn(SchemaCase):
    """Migration 35 (sd:4827, system sd:4810). `repo.github_issues` turns on
    third-party GitHub issue intake for one repository. Added, not rebuilt:
    every existing row reads 0, off, and keeps its other values; the CHECK
    holds 0 and 1; a failed file leaves 34; the reverse returns 34's shape."""

    VERSION = 35
    COLUMNS = ("path, remote, mode, runner_merge, pieces_source, created_at, updated_at, managed, ci, lane_host, "
               "channels, clone_profiles, dependabot")
    ROWS = [("/one", "git@github.com:platypeeps/one.git", "full", "auto", "file", "t1", "t2", 1, "local", "build-2",
             '{"drive": {}}', "personal,work", 1),
            ("/two", None, None, "manual", "row", "t3", "t4", 0, "github", None, None, None, 0)]

    def _before(self):
        self.migrate_through(self.VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.executemany(f"INSERT INTO repo ({self.COLUMNS}) VALUES ({', '.join('?' * 13)})", self.ROWS)
        raw.commit()
        raw.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(f"SELECT {self.COLUMNS} FROM repo ORDER BY path")]

    def _reference(self, upto):
        reference = sqlite3.connect(":memory:")
        self.addCleanup(reference.close)
        paths.install(reference)
        for version, path in schema_module.migrations():
            if version <= upto:
                reference.executescript(path.read_text(encoding="utf-8"))
        return reference

    def test_every_existing_row_arrives_off_and_keeps_its_values(self):
        self._before()
        # Through this file only: 45 moves `github_issues` into the policy.
        result = self.migrate_through(self.VERSION)
        self.assertEqual((result.before, result.applied), (self.VERSION - 1, [self.VERSION]))
        self.assertEqual(self.migrate_through(self.VERSION).applied, [])
        connection = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual([row[0] for row in connection.execute("SELECT github_issues FROM repo ORDER BY path")],
                         [0, 0])

    def test_the_check_holds_0_and_1(self):
        self._before()
        self.migrate_through(self.VERSION)
        connection = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute("UPDATE repo SET github_issues = 1 WHERE path = '/two'")
        for flag in (2, -1, None, "on", ""):
            with self.subTest(flag=flag), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE repo SET github_issues = ? WHERE path = '/two'", (flag,))
        self.assertEqual([row[0] for row in connection.execute("SELECT github_issues FROM repo ORDER BY path")],
                         [0, 1])

    def test_a_failed_file_leaves_the_store_at_34_and_a_rerun_finishes_it(self):
        """Failure table, row "`migrate` runs 035"."""
        self._before()
        raw = sqlite3.connect(self.path)
        raw.execute("ALTER TABLE repo ADD COLUMN github_issues TEXT")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        raw.execute("ALTER TABLE repo DROP COLUMN github_issues")
        raw.commit()
        raw.close()
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])

    def test_the_reverse_returns_the_file_to_the_prior_version(self):
        self._before()
        self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE repo SET github_issues = 1 WHERE path = '/one'")
        raw.commit()
        raw.close()
        text = dict(schema_module.migrations())[self.VERSION].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        self.assertEqual(raw.execute("PRAGMA table_info(repo)").fetchall(),
                         self._reference(self.VERSION - 1).execute("PRAGMA table_info(repo)").fetchall())
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])



class TheJiraProjectColumn(SchemaCase):
    """Migration 36 (sd:5103). `repo.jira_project` names the Jira project a
    repository files issues in; NULL is off. Added, not rebuilt: every existing
    row reads NULL and keeps its other values; the CHECK holds a key's spelling;
    a failed file leaves 35; the reverse returns 35's shape."""

    VERSION = 36
    COLUMNS = ("path, remote, mode, runner_merge, pieces_source, created_at, updated_at, managed, ci, lane_host, "
               "channels, clone_profiles, dependabot, github_issues")
    ROWS = [("/one", "git@github.com:platypeeps/one.git", "full", "auto", "file", "t1", "t2", 1, "local", "build-2",
             '{"jira": {"project": "EXAMPLE"}}', "personal,work", 1, 1),
            ("/two", None, None, "manual", "row", "t3", "t4", 0, "github", None, None, None, 0, 0)]

    def _before(self):
        self.migrate_through(self.VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.executemany(f"INSERT INTO repo ({self.COLUMNS}) VALUES ({', '.join('?' * 14)})", self.ROWS)
        raw.commit()
        raw.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(f"SELECT {self.COLUMNS} FROM repo ORDER BY path")]

    def _reference(self, upto):
        reference = sqlite3.connect(":memory:")
        self.addCleanup(reference.close)
        paths.install(reference)
        for version, path in schema_module.migrations():
            if version <= upto:
                reference.executescript(path.read_text(encoding="utf-8"))
        return reference

    def test_every_existing_row_arrives_off_and_keeps_its_values(self):
        self._before()
        # Through this file only: 45 moves `github_issues` into the policy.
        result = self.migrate_through(self.VERSION)
        self.assertEqual((result.before, result.applied), (self.VERSION - 1, [self.VERSION]))
        self.assertEqual(self.migrate_through(self.VERSION).applied, [])
        connection = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual([row[0] for row in connection.execute("SELECT jira_project FROM repo ORDER BY path")],
                         [None, None])

    def test_the_check_holds_a_key_spelling_or_null(self):
        self._before()
        self.migrate_through(self.VERSION)
        connection = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute("UPDATE repo SET jira_project = 'OPS_2' WHERE path = '/two'")
        for key in ("", "a", "A", "ops", "1AB", "_AB", "AB-1", "AB C", "ÉTÉ", "A" * 256, 7):
            with self.subTest(key=key), self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE repo SET jira_project = ? WHERE path = '/two'", (key,))
        connection.execute("UPDATE repo SET jira_project = NULL WHERE path = '/one'")
        self.assertEqual([row[0] for row in connection.execute("SELECT jira_project FROM repo ORDER BY path")],
                         [None, "OPS_2"])

    def test_a_failed_file_leaves_the_store_at_35_and_a_rerun_finishes_it(self):
        """Failure table, row "`migrate` runs 036"."""
        self._before()
        raw = sqlite3.connect(self.path)
        raw.execute("ALTER TABLE repo ADD COLUMN jira_project TEXT")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        raw.execute("ALTER TABLE repo DROP COLUMN jira_project")
        raw.commit()
        raw.close()
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])

    def test_the_reverse_returns_the_file_to_the_prior_version(self):
        self._before()
        self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE repo SET jira_project = 'EXAMPLE' WHERE path = '/one'")
        raw.commit()
        raw.close()
        text = dict(schema_module.migrations())[self.VERSION].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        self.assertEqual(raw.execute("PRAGMA table_info(repo)").fetchall(),
                         self._reference(self.VERSION - 1).execute("PRAGMA table_info(repo)").fetchall())
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])


class TheNoteHarnessSessionColumn(SchemaCase):
    """Migration 43 (pack sd:6046). `note.harness_session`: the harness session
    that wrote a note whose `session` names the user, as a CLI ruling does.
    Added, not rebuilt: every existing note reads NULL and keeps its other
    values; a failed file leaves 42; the reverse returns 42's shape."""

    VERSION = 43
    COLUMNS = "id, item, timestamp, kind, body, session, resolved_at"
    ROWS = [(1, 1, "t1", "status_change", "opened as planning by operator", "session-a", None),
            (2, 1, "t2", "question", "Which?", "session-a", "t3"),
            (3, 1, "t3", "decision", "Ruling on note #2: This one", "operator", None)]

    def _before(self):
        self.migrate_through(self.VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.execute("INSERT INTO item (id, kind, title, status, created_at, updated_at)"
                    " VALUES (1, 'task', 'A task', 'planning', 't1', 't1')")
        raw.executemany(f"INSERT INTO note ({self.COLUMNS}) VALUES ({', '.join('?' * 7)})", self.ROWS)
        raw.commit()
        raw.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(f"SELECT {self.COLUMNS} FROM note ORDER BY id")]

    def test_every_existing_note_arrives_at_null_and_keeps_its_values_and_a_rerun_applies_nothing(self):
        """Failure table, rows "old database" and "second run"."""
        self._before()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (self.VERSION - 1, list(range(self.VERSION, SCHEMA_VERSION + 1))))
        self.assertEqual(migrate(self.path).applied, [])
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual([row[0] for row in connection.execute("SELECT harness_session FROM note ORDER BY id")],
                         [None, None, None])

    def test_a_failed_file_leaves_the_store_at_42_and_a_rerun_finishes_it(self):
        """Failure table, row "interrupted migration"."""
        self._before()
        raw = sqlite3.connect(self.path)
        raw.execute("ALTER TABLE note ADD COLUMN harness_session TEXT")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        raw.execute("ALTER TABLE note DROP COLUMN harness_session")
        raw.commit()
        raw.close()
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])

    def test_the_reverse_returns_the_file_to_the_prior_version(self):
        """Failure table, row "rollback to the previous commit"."""
        self._before()
        self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE note SET harness_session = 'session-a' WHERE id = 3")
        raw.commit()
        raw.close()
        text = dict(schema_module.migrations())[self.VERSION].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        self.assertNotIn("harness_session", [row[1] for row in raw.execute("PRAGMA table_info(note)")])
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])



class TheShadowMergeStateColumns(SchemaCase):
    """Migration 44 (pack sd:6711). `shadow.review_decision`, `checks` and
    `mergeable`: a pull request's review, check and merge state, for the Ready
    to merge filter. Added, not rebuilt: every existing row reads NULL, not
    read, and keeps its other values; a failed file leaves 43; the reverse
    returns 43's shape."""

    VERSION = 44
    COLUMNS = "id, tracker, url, kind, state, first_seen, last_seen"
    ROWS = [(1, "github", "https://github.com/example/anvil/pull/1", "pull", "open", "t1", "t2"),
            (2, "github", "https://github.com/example/anvil/issues/2", "issue", "closed", "t1", "t3")]
    ADDED = ("review_decision", "checks", "mergeable")

    def _before(self):
        self.migrate_through(self.VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.executemany(f"INSERT INTO shadow ({self.COLUMNS}) VALUES ({', '.join('?' * 7)})", self.ROWS)
        raw.commit()
        raw.close()

    def _rows(self, connection):
        return [tuple(row) for row in connection.execute(f"SELECT {self.COLUMNS} FROM shadow ORDER BY id")]

    def _columns(self, connection):
        return [row[1] for row in connection.execute("PRAGMA table_info(shadow)")]

    def test_every_existing_row_arrives_at_null_and_keeps_its_values_and_a_rerun_applies_nothing(self):
        """Failure table, rows "old database" and "second run"."""
        self._before()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (self.VERSION - 1, list(range(self.VERSION, SCHEMA_VERSION + 1))))
        self.assertEqual(migrate(self.path).applied, [])
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.ROWS)
        self.assertEqual([tuple(row) for row in connection.execute(
            f"SELECT {', '.join(self.ADDED)} FROM shadow ORDER BY id")], [(None, None, None)] * 2)

    def test_a_failed_file_leaves_the_store_at_43_and_a_rerun_finishes_it(self):
        """Failure table, row "interrupted migration"."""
        self._before()
        raw = sqlite3.connect(self.path)
        raw.execute("ALTER TABLE shadow ADD COLUMN mergeable TEXT")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        self.assertNotIn("review_decision", self._columns(raw))
        raw.execute("ALTER TABLE shadow DROP COLUMN mergeable")
        raw.commit()
        raw.close()
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])

    def test_the_reverse_returns_the_file_to_the_prior_version(self):
        """Failure table, row "rollback to the previous commit"."""
        self._before()
        self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE shadow SET review_decision = 'approved', checks = 'success', mergeable = 'mergeable' WHERE id = 1")
        raw.commit()
        raw.close()
        text = dict(schema_module.migrations())[self.VERSION].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual(self._rows(raw), self.ROWS)
        self.assertFalse(set(self.ADDED) & set(self._columns(raw)))
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])


class TheIssuesPolicyMove(SchemaCase):
    """Migration 45 (pack sd:6849). `repo.github_issues` moves into the
    `policy` declaration as `issues` and the column goes, in one transaction:
    a row on arrives as `ask` beside its other policy keys, a row off gains
    nothing; a failed file leaves 44 with the column; the reverse returns
    44's shape and values."""

    VERSION = 45
    COLUMNS = "path, created_at, updated_at, github_issues, declarations"
    ROWS = [("/one", "t1", "t2", 1, None),
            ("/three", "t1", "t2", 1, '{"policy":{"mode":"org"},"review":{"x":1}}'),
            ("/two", "t3", "t4", 0, '{"policy":{"mode":"org"}}')]

    def _before(self):
        self.migrate_through(self.VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.executemany(f"INSERT INTO repo ({self.COLUMNS}) VALUES (?, ?, ?, ?, ?)", self.ROWS)
        raw.commit()
        raw.close()

    def _declared(self, connection):
        import json
        return {path: json.loads(text) if text else None
                for path, text in connection.execute("SELECT path, declarations FROM repo ORDER BY path")}

    def _columns(self, connection):
        return [row[1] for row in connection.execute("PRAGMA table_info(repo)")]

    def test_migration_moves_github_issues_in_one_transaction(self):
        """Failure table, row "Migrate `github_issues` into `policy.issues`"."""
        self._before()
        raw = sqlite3.connect(self.path)
        raw.execute("CREATE VIEW stuck AS SELECT github_issues FROM repo")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertIn("github_issues", self._columns(raw))
        self.assertEqual(self._declared(raw)["/one"], None)
        raw.execute("DROP VIEW stuck")
        raw.commit()
        raw.close()
        result = migrate(self.path)
        self.assertEqual((result.before, result.applied),
                         (self.VERSION - 1, list(range(self.VERSION, SCHEMA_VERSION + 1))))
        self.assertEqual(migrate(self.path).applied, [])
        connection = connect(self.path, write=True)
        self.addCleanup(connection.close)
        self.assertNotIn("github_issues", self._columns(connection))
        self.assertEqual(self._declared(connection), {
            "/one": {"policy": {"issues": "ask"}},
            "/three": {"policy": {"mode": "org", "issues": "ask"}, "review": {"x": 1}},
            "/two": {"policy": {"mode": "org"}}})

    def test_the_reverse_returns_the_file_to_the_prior_version(self):
        self._before()
        self.migrate_through(self.VERSION)
        text = dict(schema_module.migrations())[self.VERSION].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual([tuple(row) for row in raw.execute("SELECT path, github_issues FROM repo ORDER BY path")],
                         [("/one", 1), ("/three", 1), ("/two", 0)])
        self.assertEqual(self._declared(raw), {"/one": None, "/three": {"policy": {"mode": "org"}, "review": {"x": 1}},
                                               "/two": {"policy": {"mode": "org"}}})
        # The column sits where 35 put it, so a backup restore compares `repo` to 44's shape.
        reference = sqlite3.connect(":memory:")
        self.addCleanup(reference.close)
        paths.install(reference)
        for version, path in schema_module.migrations():
            if version < self.VERSION:
                reference.executescript(path.read_text(encoding="utf-8"))
        self.assertEqual(raw.execute("PRAGMA table_info(repo)").fetchall(),
                         reference.execute("PRAGMA table_info(repo)").fetchall())
        self.assertEqual(raw.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])


class TheMergeAndCiPolicyBackfill(SchemaCase):
    """Migration 47 (pack sd:7366 slice 1). `runner_merge` and `ci` gain
    their policy keys, `merge` and `ci.provider`, beside the columns, which
    stay until 48: `auto` and `local` are copied, the defaults add nothing,
    an org or external row takes no `merge`, and the columns are set from
    the policy so every row agrees. One transaction: a failure leaves 46
    with no key written; the reverse takes the keys out again."""

    VERSION = 47
    COLUMNS = "path, created_at, updated_at, runner_merge, ci, declarations"
    ROWS = [("/auto-local", "t1", "t2", "auto", "local", None),
            ("/default", "t1", "t2", "manual", "github", None),
            ("/kept", "t1", "t2", "auto", "github", '{"policy":{"issues":"read"},"review":{"x":1}}'),
            ("/local-only", "t1", "t2", "manual", "local", '{"policy":{"issues":"ask"}}'),
            ("/org", "t1", "t2", "auto", "local", '{"policy":{"mode":"org"}}'),
            ("/jenkins", "t1", "t2", "manual", "local",
             '{"policy":{"ci":{"provider":"jenkins","url":"https://ci.example.test","job":"j/PR-{pr}"}}}'),
            ("/junk", "t1", "t2", "auto", "local", '{"policy":"auto"}')]

    def _before(self):
        self.migrate_through(self.VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.executemany(f"INSERT INTO repo ({self.COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)", self.ROWS)
        raw.commit()
        raw.close()

    def _rows(self, connection):
        import json
        return {path: (merge, ci, json.loads(text) if text else None) for path, merge, ci, text in
                connection.execute("SELECT path, runner_merge, ci, declarations FROM repo ORDER BY path")}

    AFTER = {
        "/auto-local": ("auto", "local", {"policy": {"merge": "auto", "ci": {"provider": "local"}}}),
        "/default": ("manual", "github", None),
        "/kept": ("auto", "github", {"policy": {"issues": "read", "merge": "auto"}, "review": {"x": 1}}),
        "/local-only": ("manual", "local", {"policy": {"issues": "ask", "ci": {"provider": "local"}}}),
        # Failure table, row "S1 backfill, org-mode row": no `merge`, so the row reads, and now says, manual.
        "/org": ("manual", "local", {"policy": {"mode": "org", "ci": {"provider": "local"}}}),
        # A declared provider wins; the column follows it (jenkins checks are not `local`).
        "/jenkins": ("manual", "github", {"policy": {"ci": {"provider": "jenkins", "url": "https://ci.example.test",
                                                              "job": "j/PR-{pr}"}}}),
        # A policy that is not an object takes no key; its readers fail closed, and the columns follow them.
        "/junk": ("manual", "github", {"policy": "auto"}),
    }

    def test_the_backfill_copies_auto_and_local_and_the_columns_agree(self):
        self._before()
        self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        self.assertEqual(schema_version(raw), self.VERSION)
        self.assertEqual(self._rows(raw), self.AFTER)
        from sd_db.writes import CI_SQL, MERGE_SQL
        self.assertEqual(raw.execute(f"SELECT count(*) FROM repo WHERE runner_merge IS NOT {MERGE_SQL.format(t='repo')} "
                                     f"OR ci IS NOT {CI_SQL.format(t='repo')}").fetchone()[0], 0)

    def test_a_failure_at_the_second_update_leaves_46_and_no_key(self):
        """Failure table, row "S1 migration 047 backfill": kill at the second UPDATE."""
        self._before()
        raw = sqlite3.connect(self.path)
        raw.execute("CREATE TRIGGER stuck BEFORE UPDATE ON repo "
                    "WHEN json_extract(NEW.declarations, '$.policy.ci.provider') = 'local' "
                    "BEGIN SELECT RAISE(ABORT, 'stuck'); END")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.migrate_through(self.VERSION)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        self.assertEqual({path: row[2] for path, row in self._rows(raw).items()},
                         {path: (None if text is None else __import__("json").loads(text))
                          for path, _, _, _, _, text in self.ROWS})
        raw.execute("DROP TRIGGER stuck")
        raw.commit()
        raw.close()
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(self._rows(connection), self.AFTER)

    def test_the_reverse_returns_the_file_to_the_prior_version(self):
        self._before()
        self.migrate_through(self.VERSION)
        text = dict(schema_module.migrations())[self.VERSION].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), self.VERSION - 1)
        import json
        self.assertEqual({path: row[2] for path, row in self._rows(raw).items()}, {
            "/auto-local": None, "/default": None, "/kept": {"policy": {"issues": "read"}, "review": {"x": 1}},
            "/local-only": {"policy": {"issues": "ask"}}, "/org": {"policy": {"mode": "org"}},
            "/jenkins": json.loads(str(self.ROWS[5][5])), "/junk": {"policy": "auto"}})
        self.assertEqual({path: row[:2] for path, row in self._rows(raw).items()},
                         {path: row[:2] for path, row in self.AFTER.items()})
        self.assertEqual(self.migrate_through(self.VERSION).applied, [self.VERSION])


if __name__ == "__main__":
    unittest.main()


class TheHomeAssetTable(SchemaCase):
    """Migration 49 (pack sd:8053): `home_asset` arrives empty, nothing else
    moves, and the hand-run reverse returns the file to 48."""

    def test_the_table_arrives_empty_and_the_reverse_drops_it(self):
        self.migrate_through(48)
        result = self.migrate_through(49)
        self.assertEqual((result.before, result.applied), (48, [49]))
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        self.assertEqual([row[1] for row in raw.execute("PRAGMA table_info(home_asset)")],
                         ["id", "kind", "name", "geometry", "fields", "photo", "created_at", "updated_at"])
        self.assertEqual(raw.execute("SELECT count(*) FROM home_asset").fetchone()[0], 0)
        text = dict(schema_module.migrations())[49].read_text(encoding="utf-8")
        reverse = "\n".join(line[len("--   "):] for line in text.splitlines() if line.startswith("--   "))
        raw.executescript(reverse)
        self.assertEqual(schema_version(raw), 48)
        self.assertNotIn("home_asset", tables(raw))
