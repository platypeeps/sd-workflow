"""The checkpoint rules (sd:8655): old rows are deleted outright (sd:9064), and the file shrinks.

Every rule's kept rows, the delete stopped or raced, and the VACUUM the
backup job runs after the prune. Ages are seeded by backdating the rows and
handing the prune its `now`; nothing sleeps.
"""

import contextlib
import io
import os
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
import sd_lane  # type: ignore[import-not-found]  # noqa: E402

QUEUE = contribution_sync.QUEUE


class CheckpointCase(PruneCase):
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

    def compact(self):
        return retention.compact_checkpoints(self.db, now=NOW)


class TheQueueRule(CheckpointCase):
    """`contribution-refresh-queue` keeps its newest row and its newest resolved row, at any age."""

    def test_it_keeps_the_newest_and_the_newest_resolved_row_and_its_readers_answer_the_same(self):
        ids = self.rows(QUEUE, days=0, count=5, resolved=(0, 1, 2))
        before = contribution_sync._queue(self.db)["revision"]
        self.assertEqual(before, ids[2])
        self.assertEqual(self.compact(), 3)
        self.assertEqual(self.ids(QUEUE), [ids[2], ids[4]])
        self.assertEqual(contribution_sync._queue(self.db)["revision"], before)
        self.assertEqual(removal._malformed(self.db)[1], before)

    def test_a_queue_of_only_resolved_rows_keeps_its_newest(self):
        ids = self.rows(QUEUE, days=0, count=4, resolved=(0, 1, 2, 3))
        self.assertEqual(self.compact(), 3)
        self.assertEqual(self.ids(QUEUE), ids[-1:])
        self.assertEqual(contribution_sync._queue(self.db)["revision"], ids[-1])

    def test_a_queue_of_one_row_or_none_deletes_nothing(self):
        self.assertEqual(self.compact(), 0)
        ids = self.rows(QUEUE, days=0, count=1)
        self.assertEqual(self.compact(), 0)
        self.assertEqual(self.ids(QUEUE), ids)

    def test_a_lookalike_key_is_not_the_queue(self):
        lookalike = self.rows("contribution-refresh-queue-other", days=0) + self.rows("Contribution-refresh-queue",
                                                                                     days=0)
        self.assertEqual(self.compact(), 0)
        self.assertEqual(self.ids("contribution-refresh-queue-other") + self.ids("Contribution-refresh-queue"),
                         lookalike)


class TheLaneRule(CheckpointCase):
    """A `lane:` key keeps its first row and its newest once the newest is thirty days old."""

    KEY = "lane:v1:example/repo:entry"

    def test_an_idle_key_keeps_its_first_and_newest_row_and_the_lane_reads_the_same(self):
        ids = self.rows(self.KEY, days=31, count=5)
        order, stored = sd_lane.enqueue_order(self.db, "example/repo"), sd_lane.stored(self.db, "example/repo")
        self.assertEqual(self.compact(), 3)
        self.assertEqual(self.ids(self.KEY), [ids[0], ids[-1]])
        self.assertEqual(sd_lane.enqueue_order(self.db, "example/repo"), order)
        self.assertEqual(sd_lane.stored(self.db, "example/repo"), stored)

    def test_a_key_written_inside_thirty_days_keeps_every_row(self):
        ids = self.rows(self.KEY, days=29, count=5)
        self.assertEqual(self.compact(), 0)
        self.assertEqual(self.ids(self.KEY), ids)

    def test_an_idle_key_of_two_rows_keeps_both(self):
        ids = self.rows(self.KEY, days=60, count=2)
        self.assertEqual(self.compact(), 0)
        self.assertEqual(self.ids(self.KEY), ids)

    def test_the_age_is_the_table_s(self):
        self.assertEqual(retention.LANE_IDLE_AGE, timedelta(days=30))
        self.assertGreater(retention.LANE_IDLE_AGE.days, sd_lane.TIMES_DAYS)


class TheDelete(CheckpointCase):
    """The victims are read outside the lock and re-checked under it; any fault deletes nothing."""

    def race(self, then):
        """Run `then` between the victim read and the delete."""
        real = retention._victims

        def victims(*args, **kwargs):
            found = real(*args, **kwargs)
            then()
            return found
        return mock.patch.object(retention, "_victims", side_effect=victims)

    @hub_only
    def test_a_failed_victim_read_removes_nothing(self):
        ids = self.rows("ship:idle", days=10)
        with mock.patch.object(retention, "_victims", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
                retention.prune(self.db, self.backup(), now=NOW)
        self.assertEqual(self.ids("ship:idle"), ids)

    def test_a_stop_mid_delete_rolls_back_and_the_next_run_finishes(self):
        ids = self.rows("ship:idle", days=10, count=4)
        self.db.execute(f"CREATE TEMP TRIGGER stop BEFORE DELETE ON main.state WHEN old.id = {ids[1]}"
                        " BEGIN SELECT RAISE(ABORT, 'stopped mid-delete'); END")
        with mock.patch.object(retention, "CHUNK", 1), self.assertRaisesRegex(sqlite3.Error, "stopped mid-delete"):
            self.compact()
        self.assertEqual(self.ids("ship:idle"), ids)
        self.db.execute("DROP TRIGGER temp.stop")
        self.assertEqual(self.compact(), 3)
        self.assertEqual(self.ids("ship:idle"), ids[-1:])

    def test_a_key_whose_rows_span_chunks_drains_in_one_run(self):
        """pack sd:8665: the bounds were re-read per chunk after the earlier chunks' deletes, so a key's first row
        was gone and every later chunk kept its rows."""
        ids = self.rows("ship:idle", days=10, count=4)
        with mock.patch.object(retention, "CHUNK", 1):
            self.assertEqual(self.compact(), 3)
        self.assertEqual(self.ids("ship:idle"), ids[-1:])

    def test_a_key_revived_between_read_and_delete_keeps_its_rows(self):
        ids = self.rows("ship:idle", days=10)
        revived = []
        with self.race(lambda: revived.append(record_state(self.db, "checkpoint", key="ship:idle",
                                                           body={"step": "live"}))):
            self.assertEqual(self.compact(), 0)
        self.assertEqual(self.ids("ship:idle"), ids + revived)

    def test_a_row_resolved_after_the_read_is_kept(self):
        ids = self.rows("ship:idle", days=10, count=3)
        with self.race(lambda: resolve_state(self.db, ids[0])):
            self.assertEqual(self.compact(), 1)
        self.assertEqual(self.ids("ship:idle"), [ids[0], ids[2]])

    def test_under_the_write_lock_only_the_victim_rows_are_read_from_the_table(self):
        """`resolved_at` sits after `body`, so the victim read walks every body: it runs before the lock (sd:8657)."""
        self.rows("ship:live", days=0)
        ids = self.rows("ship:idle", days=10)
        inside = []
        self.db.set_trace_callback(lambda sql: inside.append(sql) if self.db.in_transaction else None)
        try:
            deleted = self.compact()
        finally:
            self.db.set_trace_callback(None)
        self.assertEqual((deleted, self.ids("ship:idle")), (2, ids[-1:]))
        reads = [sql for sql in inside if sql.lstrip().upper().startswith(("SELECT", "DELETE"))]
        self.assertTrue(reads)
        path = next(row[2] for row in self.db.execute("PRAGMA database_list") if row[1] == "main")
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as plain:
            plans = [row[3] for sql in reads for row in plain.execute("EXPLAIN QUERY PLAN " + sql)]
        wide = [plan for plan in plans if re.match(r"(SCAN|SEARCH) (?!\()", plan)
                and not re.search(r"USING (INTEGER PRIMARY KEY|COVERING INDEX)", plan)]
        self.assertEqual(wide, [], plans)

    def test_an_empty_or_null_key_is_no_rule_s(self):
        for key in ("", None):
            self.db.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, '{}')",
                            (key, ago(days=400)))
            self.db.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, '{}')",
                            (key, ago(days=399)))
        self.assertEqual(retention.checkpoint_victims(self.db, now=NOW), [])


@hub_only
class TheVacuum(CheckpointCase):
    """VACUUM once 50 MiB of pages are free; a held lock skips it and the next night runs it."""

    def free(self, megabytes=3):
        """Write then delete `megabytes` of rows, leaving their pages free."""
        for _ in range(megabytes):
            record_state(self.db, "checkpoint", key="ship:scratch", body="y" * (1024 * 1024))
        self.db.execute("DELETE FROM state WHERE key='ship:scratch'")
        return retention._size(self.db)[1]

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

    def test_a_held_lock_skips_it_and_the_next_night_runs_it(self):
        free = self.free(3)
        other = connect(home=self.home)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', 'ship:other', ?, '{}')",
                      (ago(days=0),))
        with self.assertRaisesRegex(sqlite3.OperationalError, "locked|busy"):
            retention.vacuum(self.db, threshold=free, busy_ms=50)
        other.execute("COMMIT")
        self.assertEqual(retention._size(self.db)[1], free)
        self.assertTrue(retention.vacuum(self.db, threshold=free).ran)
        self.assertEqual(self.ids("ship:other"), [self.ids("ship:other")[0]])

    def test_a_reader_leaves_the_checkpoint_to_the_next_night(self):
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


class TheJob(CheckpointCase):
    """The backup job prunes, then vacuums; it reads no bulk root and writes no archive (sd:9064)."""

    def env(self):
        return {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.root / "config")}

    @hub_only
    def test_a_vacuum_that_did_not_run_fails_the_job_and_names_why(self):
        self.rows("review:idle", days=10)  # the job runs at the clock's now: no `STALE_CHECKPOINTS` key
        error = io.StringIO()
        with mock.patch.dict(os.environ, self.env()), \
                mock.patch.object(backup_job, "vacuum", side_effect=sqlite3.OperationalError("database is locked")), \
                contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            code = backup_job.main([])
        self.assertEqual(code, 1)
        self.assertIn("and the prune ran: ", error.getvalue())
        self.assertIn("2 superseded checkpoint row(s) removed", error.getvalue())
        self.assertIn("the VACUUM did not run: database is locked", error.getvalue())
        self.assertEqual(len(self.ids("review:idle")), 1)

    def test_a_failure_after_the_vacuum_ran_names_its_stage(self):
        """The WAL checkpoint or the size read failing is not the VACUUM failing (sd:8657)."""
        self.rows("ship:idle", days=10)
        real = retention._size
        calls = []

        def size(connection):
            calls.append(1)
            if len(calls) > 1:
                raise sqlite3.OperationalError("disk I/O error")
            return real(connection)
        error = io.StringIO()
        with mock.patch.dict(os.environ, self.env()), \
                mock.patch.object(backup_job, "vacuum", side_effect=lambda c: retention.vacuum(c, threshold=0)), \
                mock.patch.object(retention, "_size", side_effect=size), \
                contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            code = backup_job.main([])
        self.assertEqual(code, 1)
        self.assertNotIn("did not run", error.getvalue())
        self.assertIn("the VACUUM ran, then the size read failed: OperationalError: disk I/O error", error.getvalue())

    def test_the_job_deletes_the_rows_and_vacuums(self):
        """Older state: whole lane and queue keys, as every store before this change holds them."""
        lane = self.rows("lane:v1:example/repo:old", days=45, count=3)
        queue = self.rows(QUEUE, days=0, count=4, resolved=(0, 1, 2, 3))
        for ident in queue[:3]:
            self.db.execute("UPDATE state SET body = ? WHERE id = ?", ("z" * (18 * 1024 * 1024), ident))
        self.db.commit()
        stubs = Stubs(self.root / "stubs", names=("local-notify",))
        shutil.copy2(stubs.bin / "local-notify", stubs.bin / "notify")
        environment = stubs.environment(base=dict(os.environ))
        environment.update({"PYTHON": sys.executable, **self.env()})
        completed = subprocess.run([sys.executable, str(support.SD_DB), "backup"], capture_output=True, text=True,
                                   input="", env=environment, check=False)
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("4 superseded checkpoint row(s) removed", output)
        self.assertNotIn("archive", output)
        vacuumed = re.search(r"vacuumed (\d+) bytes to (\d+) bytes", output)
        assert vacuumed is not None, output
        before, after = map(int, vacuumed.groups())
        self.assertLess(after, before - 50 * 1024 * 1024)
        self.assertEqual(self.ids("lane:v1:example/repo:old"), [lane[0], lane[-1]])
        self.assertEqual(self.ids(QUEUE), queue[-1:])
        self.assertEqual(stubs.calls("notify"), [])
