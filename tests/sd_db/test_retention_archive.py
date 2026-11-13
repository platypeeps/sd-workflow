"""The checkpoint archive (sd:8655): each dropped row goes to the bulk root first, and the file shrinks.

Every rule's kept rows, every step of archive-then-delete stopped or failed,
the rows at the extremes the archive must carry byte for byte, and the
VACUUM the backup job runs after the prune. Ages are seeded by backdating
the rows and handing the prune its `now`; nothing sleeps.
"""

import base64
import contextlib
import gzip
import io
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest import mock

from sd_db import contribution_sync, removal, retention
from sd_db.database import connect
from sd_db.jobs import backup as backup_job
from sd_db.testing import Stubs
from sd_db.testing.wire import hub_only
from sd_db.writes import record_state, resolve_state

from . import support
from .test_retention import NOW, PruneCase, ago

sys.path.insert(0, str(support.PACK / "bin"))
import sd_lane  # noqa: E402

QUEUE = contribution_sync.QUEUE


class ArchiveCase(PruneCase):
    def setUp(self):
        super().setUp()
        self.bulk = self.root / "bulk"
        self.bulk.mkdir()
        self.folder = self.bulk / "sd-workflow" / "db-archive"  # the path sd:8655 names

    def rows(self, key, *, days, count=3, resolved=()):
        """`count` rows under `key`, the newest written `days` ago; the steps in `resolved` resolved; ids, oldest first."""
        body = {"id": key.rpartition(":")[2], "pending": []}
        ids = [record_state(self.db, "checkpoint", key=key, body={**body, "step": step},
                            timestamp=ago(days=days, hours=count - 1 - step)) for step in range(count)]
        for step in resolved:
            resolve_state(self.db, ids[step])
        return ids

    def ids(self, key):
        return [row[0] for row in self.db.execute("SELECT id FROM state WHERE key=? ORDER BY id", (key,))]

    def compact(self, name=None, **kwargs):
        return retention.compact_checkpoints(self.db, now=NOW, root=kwargs.pop("root", self.bulk), name=name,
                                             **kwargs)

    def lines(self, path):
        with gzip.open(path, "rb") as packed:
            return [json.loads(line) for line in packed]

    def raw(self, ident):
        row = self.db.execute("SELECT CAST(key AS BLOB), typeof(body), CAST(body AS BLOB) FROM state WHERE id=?",
                              (ident,)).fetchone()
        return (None if row[0] is None else bytes(row[0]), row[1], None if row[2] is None else bytes(row[2]))

    def decoded(self, record):
        def one(name):
            if f"{name}_b64" in record:
                return base64.b64decode(record[f"{name}_b64"])
            return None if record[name] is None else record[name].encode("utf-8")
        return one("key"), record["type"], one("body")

    def files(self):
        return sorted(path.name for path in self.folder.iterdir()) if self.folder.exists() else []


class TheQueueRule(ArchiveCase):
    """`contribution-refresh-queue` keeps its newest row and its newest resolved row, at any age."""

    def test_it_keeps_the_newest_and_the_newest_resolved_row_and_its_readers_answer_the_same(self):
        ids = self.rows(QUEUE, days=0, count=5, resolved=(0, 1, 2))
        before = contribution_sync._queue(self.db)["revision"]
        self.assertEqual(before, ids[2])
        compacted = self.compact()
        self.assertEqual((compacted.deleted, compacted.archived), (3, 3))
        self.assertEqual(self.ids(QUEUE), [ids[2], ids[4]])
        self.assertEqual(contribution_sync._queue(self.db)["revision"], before)
        self.assertEqual(removal._malformed(self.db)[1], before)

    def test_a_queue_of_only_resolved_rows_keeps_its_newest(self):
        ids = self.rows(QUEUE, days=0, count=4, resolved=(0, 1, 2, 3))
        self.assertEqual(self.compact().deleted, 3)
        self.assertEqual(self.ids(QUEUE), ids[-1:])
        self.assertEqual(contribution_sync._queue(self.db)["revision"], ids[-1])

    def test_a_queue_of_one_row_or_none_writes_no_archive(self):
        self.assertEqual(self.compact(), retention.Compacted())
        self.rows(QUEUE, days=0, count=1)
        self.assertEqual(self.compact(), retention.Compacted())
        self.assertEqual(self.files(), [])

    def test_a_lookalike_key_is_not_the_queue(self):
        lookalike = self.rows("contribution-refresh-queue-other", days=0) + self.rows("Contribution-refresh-queue",
                                                                                     days=0)
        self.assertEqual(self.compact().deleted, 0)
        self.assertEqual(self.ids("contribution-refresh-queue-other") + self.ids("Contribution-refresh-queue"),
                         lookalike)


class TheLaneRule(ArchiveCase):
    """A `lane:` key keeps its first row and its newest once the newest is thirty days old."""

    KEY = "lane:v1:example/repo:entry"

    def test_an_idle_key_keeps_its_first_and_newest_row_and_the_lane_reads_the_same(self):
        ids = self.rows(self.KEY, days=31, count=5)
        order, stored = sd_lane.enqueue_order(self.db, "example/repo"), sd_lane.stored(self.db, "example/repo")
        self.assertEqual(self.compact().deleted, 3)
        self.assertEqual(self.ids(self.KEY), [ids[0], ids[-1]])
        self.assertEqual(sd_lane.enqueue_order(self.db, "example/repo"), order)
        self.assertEqual(sd_lane.stored(self.db, "example/repo"), stored)

    def test_a_key_written_inside_thirty_days_keeps_every_row(self):
        ids = self.rows(self.KEY, days=29, count=5)
        self.assertEqual(self.compact().deleted, 0)
        self.assertEqual(self.ids(self.KEY), ids)

    def test_an_idle_key_of_two_rows_keeps_both(self):
        ids = self.rows(self.KEY, days=60, count=2)
        self.assertEqual(self.compact().deleted, 0)
        self.assertEqual(self.ids(self.KEY), ids)

    def test_the_age_is_the_table_s(self):
        self.assertEqual(retention.LANE_IDLE_AGE, timedelta(days=30))
        self.assertGreater(retention.LANE_IDLE_AGE.days, sd_lane.TIMES_DAYS)


class TheArchive(ArchiveCase):
    """Archive, read back, then delete; any fault before the delete deletes nothing."""

    def test_each_deleted_row_reads_back_from_the_archive_byte_for_byte(self):
        ids = self.rows("ship:idle", days=10, count=5)
        bodies = {ids[0]: "CAST(x'fffe00c3' AS TEXT)", ids[1]: "x'00ff10'", ids[2]: "NULL",
                  ids[3]: "CAST(zeroblob(3145728) AS TEXT)"}
        for ident, body in bodies.items():
            self.db.execute(f"UPDATE state SET body = {body} WHERE id = ?", (ident,))
        before = {ident: self.raw(ident) for ident in ids[:-1]}
        compacted = self.compact(name="tonight.jsonl.gz")
        self.assertEqual((compacted.deleted, compacted.archived), (4, 4))
        self.assertEqual(compacted.path, self.folder / "tonight.jsonl.gz")
        self.assertEqual(compacted.freed, sum(len(raw[2] or b"") for raw in before.values()))
        records = self.lines(compacted.path)
        self.assertEqual([record["id"] for record in records], ids[:-1])
        self.assertEqual({record["id"]: self.decoded(record) for record in records}, before)
        self.assertEqual(self.ids("ship:idle"), ids[-1:])

    def test_a_failed_archive_write_deletes_nothing_and_the_report_says_why(self):
        ids = self.rows("ship:idle", days=10)
        with mock.patch.object(retention.gzip.GzipFile, "write", side_effect=OSError(28, "No space left on device")):
            pruned = retention.prune(self.db, self.backup(), now=NOW, bulk_root=self.bulk)
        self.assertEqual(self.ids("ship:idle"), ids)
        self.assertEqual(self.files(), [])
        self.assertEqual(pruned.checkpoints, 0)
        report = self.db.execute("SELECT * FROM item WHERE id=?", (pruned.report,)).fetchone()
        fields = json.loads(report["fields"])
        self.assertTrue(fields["attention"])
        self.assertIn("2 checkpoint row(s) kept: the archive", fields["report"]["attention_basis"])
        self.assertIn("No space left on device", json.loads(report["body"])["text"])

    @hub_only
    def test_a_failed_victim_read_removes_nothing(self):
        ids = self.rows("ship:idle", days=10)
        with mock.patch.object(retention, "checkpoint_victims", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
                retention.prune(self.db, self.backup(), now=NOW, bulk_root=self.bulk)
        self.assertEqual((self.ids("ship:idle"), self.files()), (ids, []))

    def test_an_archive_that_reads_back_short_deletes_nothing(self):
        ids = self.rows("ship:idle", days=10)
        real = retention.read_archive
        with mock.patch.object(retention, "read_archive", side_effect=lambda path: real(path)[:-1]):
            compacted = self.compact(name="short.jsonl.gz")
        self.assertEqual(self.ids("ship:idle"), ids)
        self.assertEqual((compacted.deleted, compacted.attention), (0, True))
        self.assertIn("read back 1 row(s) of 2 written", compacted.why)
        self.assertEqual(self.files(), [])

    def test_no_bulk_root_deletes_no_checkpoint_row_and_the_report_says_so(self):
        ids = self.rows("ship:idle", days=10)
        pruned = retention.prune(self.db, self.backup(), now=NOW)
        self.assertEqual((self.ids("ship:idle"), pruned.checkpoints), (ids, 0))
        report = self.db.execute("SELECT * FROM item WHERE id=?", (pruned.report,)).fetchone()
        self.assertFalse(json.loads(report["fields"]).get("attention"))
        self.assertIn("2 checkpoint row(s) kept: sd.bulk_storage_root is unset", json.loads(report["body"])["text"])

    def test_a_bulk_root_that_is_not_a_folder_deletes_nothing_and_asks_for_a_person(self):
        ids = self.rows("ship:idle", days=10)
        for root, unresolved, says in ((self.root / "unmounted", "", "is not a folder"),
                                       (None, "sd.bulk_storage_root was not read: ConfigError", "was not read")):
            with self.subTest(says=says):
                compacted = self.compact(root=root, unresolved=unresolved)
                self.assertEqual((compacted.deleted, compacted.attention), (0, True))
                self.assertIn(says, compacted.why)
                self.assertEqual(self.ids("ship:idle"), ids)
        self.assertFalse((self.root / "unmounted").exists())

    def test_a_stop_after_the_archive_before_the_delete_is_finished_by_the_next_run(self):
        ids = self.rows("ship:idle", days=10)
        real = retention.write_archive

        def stopped(*args):
            real(*args)
            raise KeyboardInterrupt
        with mock.patch.object(retention, "write_archive", side_effect=stopped), self.assertRaises(KeyboardInterrupt):
            self.compact(name="first.jsonl.gz")
        self.assertEqual(self.ids("ship:idle"), ids)
        self.assertEqual(self.compact(name="second.jsonl.gz").deleted, 2)
        self.assertEqual(self.ids("ship:idle"), ids[-1:])
        self.assertEqual(self.files(), ["first.jsonl.gz", "second.jsonl.gz"])
        self.assertEqual([line["id"] for line in self.lines(self.folder / "second.jsonl.gz")], ids[:-1])

    def test_a_stop_inside_the_delete_rolls_back_and_the_next_run_finishes(self):
        ids = self.rows("ship:idle", days=10, count=4)
        self.db.execute(f"CREATE TEMP TRIGGER stop BEFORE DELETE ON main.state WHEN old.id = {ids[1]}"
                        " BEGIN SELECT RAISE(ABORT, 'stopped mid-delete'); END")
        with mock.patch.object(retention, "CHUNK", 1), self.assertRaisesRegex(sqlite3.Error, "stopped mid-delete"):
            self.compact(name="first.jsonl.gz")
        self.assertEqual(self.ids("ship:idle"), ids)
        self.db.execute("DROP TRIGGER temp.stop")
        self.assertEqual(self.compact(name="second.jsonl.gz").deleted, 3)
        self.assertEqual(self.ids("ship:idle"), ids[-1:])

    def test_a_key_revived_between_archive_and_delete_keeps_its_rows(self):
        ids = self.rows("ship:idle", days=10)
        real = retention.write_archive
        revived = []

        def then_write(*args):
            written = real(*args)
            revived.append(record_state(self.db, "checkpoint", key="ship:idle", body={"step": "live"}))
            return written
        with mock.patch.object(retention, "write_archive", side_effect=then_write):
            compacted = self.compact()
        self.assertEqual((compacted.archived, compacted.deleted), (2, 0))
        self.assertEqual(self.ids("ship:idle"), ids + revived)

    def test_a_row_resolved_after_its_archive_is_kept(self):
        ids = self.rows("ship:idle", days=10, count=3)
        real = retention.write_archive

        def then_resolve(*args):
            written = real(*args)
            resolve_state(self.db, ids[0])
            return written
        with mock.patch.object(retention, "write_archive", side_effect=then_resolve):
            compacted = self.compact()
        self.assertEqual((compacted.archived, compacted.deleted), (2, 1))
        self.assertEqual(self.ids("ship:idle"), [ids[0], ids[2]])

    def test_a_stale_part_file_is_removed_by_the_next_run(self):
        self.folder.mkdir(parents=True)
        stale = self.folder / ".2026-09-11-abc.jsonl.gz.99999.part"
        stale.write_bytes(b"torn")
        self.rows("ship:idle", days=10)
        self.assertEqual(self.compact(name="tonight.jsonl.gz").deleted, 2)
        self.assertEqual(self.files(), ["tonight.jsonl.gz"])

    def test_an_archive_name_already_taken_deletes_nothing(self):
        ids = self.rows("ship:idle", days=10)
        self.folder.mkdir(parents=True)
        (self.folder / "taken.jsonl.gz").write_bytes(b"an earlier night's")
        compacted = self.compact(name="taken.jsonl.gz")
        self.assertEqual((compacted.deleted, compacted.attention), (0, True))
        self.assertEqual(self.ids("ship:idle"), ids)
        self.assertEqual((self.folder / "taken.jsonl.gz").read_bytes(), b"an earlier night's")

    @hub_only
    def test_the_prune_names_its_archive_by_the_backup_run(self):
        self.rows("ship:idle", days=10)
        snapshot = self.backup()
        pruned = retention.prune(self.db, snapshot, now=NOW, bulk_root=self.bulk)
        name = f"{NOW.date().isoformat()}-{snapshot.run_id}.jsonl.gz"
        self.assertEqual((pruned.checkpoints, pruned.compacted.path), (2, self.folder / name))
        text = json.loads(self.db.execute("SELECT body FROM item WHERE id=?", (pruned.report,)).fetchone()[0])["text"]
        self.assertIn(f"Archived 2 checkpoint row(s) to {self.folder / name}, read back, before the delete", text)


class TheArchiveAtTheExtremes(ArchiveCase):
    """Property: whatever bytes a row holds, its archive line decodes to them, and a delete follows only that."""

    BODIES = (None, b"", b"\x00", b"\xff\xfe\xfd", b"\xed\xa0\x80", "é\U0001f600".encode(), b"{}",
              b"x" * (4 * 1024 * 1024), bytes(range(256)))
    KEYS = (None, b"", b"ship:", b"\xff", "ship:é".encode())

    def test_every_extreme_row_round_trips(self):
        generator = random.Random(8655)
        ids = []
        for _ in range(60):
            key, body = generator.choice(self.KEYS), generator.choice(self.BODIES)
            storage = generator.choice(("TEXT", "BLOB"))
            cursor = self.db.execute(
                "INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', CAST(? AS TEXT), ?, "
                f"CAST(? AS {storage}))", (key, ago(days=generator.randint(0, 400)), body))
            ids.append(cursor.lastrowid)
        before = {ident: self.raw(ident) for ident in ids}
        written = retention.write_archive(self.db, ids, self.folder, "extremes.jsonl.gz")
        self.assertEqual([row[0] for row in written], ids)
        records = self.lines(self.folder / "extremes.jsonl.gz")
        self.assertEqual({record["id"]: self.decoded(record) for record in records}, before)
        self.assertEqual(retention.read_archive(self.folder / "extremes.jsonl.gz"), [row[:4] for row in written])

    def test_zero_rows_write_an_empty_archive_that_reads_back_empty(self):
        self.assertEqual(retention.write_archive(self.db, [], self.folder, "none.jsonl.gz"), [])
        self.assertEqual(retention.read_archive(self.folder / "none.jsonl.gz"), [])

    def test_an_empty_or_null_key_is_no_rule_s(self):
        for key in ("", None):
            self.db.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, '{}')",
                            (key, ago(days=400)))
            self.db.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, '{}')",
                            (key, ago(days=399)))
        self.assertEqual(retention.checkpoint_victims(self.db, now=NOW), [])


@hub_only
class TheVacuum(ArchiveCase):
    """VACUUM once 50 MiB of pages are free; a held lock skips it and the next night runs it."""

    def free(self, megabytes=3):
        """Write then delete `megabytes` of rows, leaving their pages free."""
        for _ in range(megabytes):
            record_state(self.db, "checkpoint", key="ship:scratch", body="y" * (1024 * 1024))
        self.db.execute("DELETE FROM state WHERE key='ship:scratch'")
        return self.db.execute("PRAGMA freelist_count").fetchone()[0] * self.db.execute(
            "PRAGMA page_size").fetchone()[0]

    def test_under_the_threshold_it_does_not_run(self):
        free = self.free(1)
        vacuumed = retention.vacuum(self.db)
        self.assertFalse(vacuumed.ran)
        self.assertEqual(vacuumed.before, vacuumed.after)
        self.assertIn(f"{free} free bytes, under the {retention.VACUUM_FREE_BYTES}", str(vacuumed))
        self.assertEqual(retention.VACUUM_FREE_BYTES, 50 * 1024 * 1024)

    def test_over_it_the_database_shrinks_and_both_sizes_are_reported(self):
        free = self.free(3)
        vacuumed = retention.vacuum(self.db, threshold=free)
        self.assertTrue(vacuumed.ran and vacuumed.checkpointed)
        self.assertLessEqual(vacuumed.after, vacuumed.before - free)
        self.assertEqual(self.db.execute("PRAGMA freelist_count").fetchone()[0], 0)
        self.assertEqual(Path(self.state / "sd.db").stat().st_size, vacuumed.after)
        self.assertEqual(str(vacuumed), f"vacuumed {vacuumed.before} bytes to {vacuumed.after} bytes")
        self.assertEqual(self.db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_a_writer_holding_the_lock_skips_it_and_the_next_night_runs_it(self):
        free = self.free(3)
        other = connect(home=self.home)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', 'ship:other', ?, '{}')",
                      (ago(days=0),))
        with self.assertRaisesRegex(sqlite3.OperationalError, "locked|busy"):
            retention.vacuum(self.db, threshold=free, busy_ms=50)
        other.execute("COMMIT")
        self.assertEqual(self.db.execute("PRAGMA freelist_count").fetchone()[0] * self.db.execute(
            "PRAGMA page_size").fetchone()[0], free)
        self.assertTrue(retention.vacuum(self.db, threshold=free).ran)
        self.assertEqual(self.ids("ship:other"), [self.ids("ship:other")[0]])

    def test_a_reader_holding_a_snapshot_leaves_the_checkpoint_to_the_next_night(self):
        free = self.free(3)
        reader = connect(home=self.home, write=False)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM state").fetchone()
        self.db.execute("PRAGMA busy_timeout = 100")  # the checkpoint waits the connection's own timeout
        vacuumed = retention.vacuum(self.db, threshold=free)
        self.assertTrue(vacuumed.ran)
        self.assertFalse(vacuumed.checkpointed)
        self.assertGreaterEqual(vacuumed.wal, free)
        self.assertIn(f"a reader held the WAL at {vacuumed.wal} bytes", str(vacuumed))
        reader.execute("ROLLBACK")
        night = retention.vacuum(self.db, threshold=free)
        self.assertFalse(night.ran)
        self.assertTrue(night.checkpointed)
        self.assertEqual((night.wal, Path(f"{self.state / 'sd.db'}-wal").stat().st_size), (0, 0))
        self.assertIn("so it was checkpointed", str(night))

    def test_inside_a_transaction_it_refuses(self):
        self.db.execute("BEGIN IMMEDIATE")
        self.addCleanup(lambda: self.db.in_transaction and self.db.execute("ROLLBACK"))
        with self.assertRaisesRegex(retention.RetentionRefused, "inside a transaction"):
            retention.vacuum(self.db, threshold=0)


class TheBulkRoot(ArchiveCase):
    """The job reads `sd.bulk_storage_root` through `sd_lib`, as the build log archive does."""

    def config(self, value):
        path = self.root / "config" / "sd-ai-command-pack" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return {"XDG_CONFIG_HOME": str(self.root / "config"), "HOME": str(self.home)}

    def test_unset_set_and_unreadable(self):
        cases = (({}, (None, "")),
                 ({"config": {"sd": {"bulk_storage_root": str(self.bulk)}}}, (self.bulk, "")),
                 ({"config": {"sd": {"bulk_storage_root": "relative/path"}}}, None),
                 ("{not json", None))
        for value, expected in cases:
            with self.subTest(value=value), mock.patch.dict(os.environ, self.config(value)):
                found = backup_job.bulk_root()
                if expected is None:
                    self.assertIsNone(found[0])
                    self.assertIn("sd.bulk_storage_root was not read: ConfigError", found[1])
                else:
                    self.assertEqual(found, expected)

    @hub_only
    def test_a_vacuum_that_did_not_run_fails_the_job_and_names_why(self):
        self.rows("ship:idle", days=10)
        error = io.StringIO()
        with mock.patch.dict(os.environ, self.config({"config": {"sd": {"bulk_storage_root": str(self.bulk)}}})), \
                mock.patch.object(backup_job, "vacuum", side_effect=sqlite3.OperationalError("database is locked")), \
                contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            code = backup_job.main([])
        self.assertEqual(code, 1)
        self.assertIn("and the prune ran: ", error.getvalue())
        self.assertIn("2 superseded checkpoint row(s) removed", error.getvalue())
        self.assertIn("the VACUUM did not run: database is locked", error.getvalue())
        self.assertEqual(len(self.ids("ship:idle")), 1)

    @hub_only
    def test_the_job_archives_to_the_bulk_root_and_vacuums(self):
        """Older state: whole lane and queue keys, as every store before this change holds them."""
        lane = self.rows("lane:v1:example/repo:old", days=45, count=3)
        queue = self.rows(QUEUE, days=0, count=4, resolved=(0, 1, 2, 3))
        for ident in queue[:3]:
            self.db.execute("UPDATE state SET body = ? WHERE id = ?", ("z" * (18 * 1024 * 1024), ident))
        self.db.commit()
        stubs = Stubs(self.root / "stubs", names=("local-notify",))
        shutil.copy2(stubs.bin / "local-notify", stubs.bin / "notify")
        environment = stubs.environment(base=dict(os.environ))
        environment.update({"PYTHON": sys.executable, **self.config(
            {"config": {"sd": {"bulk_storage_root": str(self.bulk)}}})})
        completed = subprocess.run([sys.executable, str(support.SD_DB), "backup"], capture_output=True, text=True,
                                   input="", env=environment, check=False)
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("4 superseded checkpoint row(s) removed", output)
        self.assertIn(f"4 checkpoint row(s) archived to {self.folder}", output)
        vacuumed = re.search(r"vacuumed (\d+) bytes to (\d+) bytes", output)
        self.assertIsNotNone(vacuumed, output)
        before, after = map(int, vacuumed.groups())
        self.assertLess(after, before - 50 * 1024 * 1024)
        self.assertEqual(self.ids("lane:v1:example/repo:old"), [lane[0], lane[-1]])
        self.assertEqual(self.ids(QUEUE), queue[-1:])
        (archive,) = self.folder.iterdir()
        self.assertEqual(sorted(line["id"] for line in self.lines(archive)), sorted([lane[1], *queue[:3]]))
        self.assertEqual(stubs.calls("notify"), [])
