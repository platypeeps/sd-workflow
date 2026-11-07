"""The backup, the restore that proves it, and the paths that fail.

The failure tests run `sd-db.sh backup` -- what the scheduled `sd-db-backup`
job runs -- and not the library, because what they assert is the job's
contract: one email through the cron mail path, and nothing written to the
database. `notify` is the fixture stub, on PATH, so the mail is
recorded rather than sent.
"""

import contextlib
import dataclasses
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from unittest import mock

from sd_db import add_note, connect, create_assignment, create_item, upsert_repo
from sd_db.backup import (
    BACKUP_MANIFEST,
    KEEP,
    _dated_directory,
    backup_root,
    check_restorable,
    passed,
    prune,
    prune_older,
    restore,
    run,
    verify,
)
from sd_db.errors import BackupError, SdDbError
from sd_db.jobs import backup as job_backup
from sd_db.migrate import initialise
from sd_db.schema import SCHEMA_DIR, SCHEMA_VERSION, migrations
from sd_db.testing import Stubs, add_unknown_table, break_foreign_keys, make_store
from sd_db.testing.wire import hub_only
from sd_db.writes import record_state, unresolved_state

from . import support

PACKAGE_ROOT = support.LIB
ENTRYPOINT = support.SD_DB

#: The module, not `sd_db.backup` the re-exported function: two tests patch
#: names inside it.
backup_module = import_module("sd_db.backup")


class BackupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.state = self.home / ".local/share/sd"
        self.state.mkdir(parents=True)
        initialise(home=self.home)
        self.database = self.state / "sd.db"

    def killed(self, script, *args):
        """Run a restore `script` that kills itself mid-run (sd:3059). A killed restore skips the `finally` that
        removes its `sd-restore-` stage, so the child's `TMPDIR` is this test's folder, which cleanup removes."""
        return subprocess.run([sys.executable, "-c", script, *map(str, args)], capture_output=True, text=True,
                              check=False, env={**os.environ, "TMPDIR": self.tmp.name})

    def open(self, **kwargs):
        connection = connect(home=self.home, **kwargs)
        self.addCleanup(connection.close)
        return connection

    def configuration(self):
        (self.state / "providers.yaml").write_text("providers: {}\n", encoding="utf-8")
        (self.state / "commands.yaml").write_text("commands: {}\n", encoding="utf-8")


@hub_only
class TheSnapshot(BackupCase):
    def test_it_writes_a_dated_directory_with_the_database_and_the_configuration(self):
        self.configuration()
        snapshot = run(home=self.home)
        self.assertTrue(snapshot.directory.is_dir())
        self.assertEqual(snapshot.directory.parent, backup_root(self.home))
        self.assertRegex(snapshot.directory.name, r"^\d{4}-\d{2}-\d{2}")
        for name in ("sd.db", "providers.yaml", "commands.yaml"):
            self.assertTrue((snapshot.directory / name).is_file(), name)

    def test_the_checkpoint_row_is_written_before_the_snapshot_so_the_copy_holds_it(self):
        snapshot = run(home=self.home)
        copy = sqlite3.connect(snapshot.directory / "sd.db")
        self.addCleanup(copy.close)
        found = copy.execute(
            "SELECT count(*) FROM state WHERE kind = 'checkpoint' AND key = ?",
            (snapshot.run_id,),
        ).fetchone()[0]
        self.assertEqual(found, 1)

    def test_the_checkpoint_row_body_is_a_json_object_naming_when(self):
        """The proof row was a bare timestamp, the one checkpoint body not JSON (sd:3578)."""
        when = datetime(2026, 9, 6, 3, tzinfo=UTC)
        snapshot = run(home=self.home, when=when)
        connection = self.open()
        body = connection.execute(
            "SELECT body FROM state WHERE kind = 'checkpoint' AND key = ?", (snapshot.run_id,)).fetchone()[0]
        self.assertEqual(json.loads(body), {"at": when.isoformat()})
        self.assertTrue(passed(connection, snapshot))

    def test_two_runs_in_a_day_do_not_overwrite_each_other(self):
        when = datetime(2026, 9, 6, tzinfo=UTC)
        first = run(home=self.home, when=when)
        second = run(home=self.home, when=when)
        self.assertNotEqual(first.directory, second.directory)
        self.assertTrue(first.directory.exists())

    def test_it_keeps_thirty(self):
        root = backup_root(self.home)
        first = datetime(2026, 1, 1, tzinfo=UTC)
        snapshots = [run(home=self.home, when=first + timedelta(days=day)) for day in range(40)]
        removed = prune(root, KEEP)
        self.assertEqual(len(removed), 10)
        self.assertEqual(len(list(root.iterdir())), 30)
        self.assertFalse(snapshots[0].directory.exists())
        self.assertTrue(snapshots[-1].directory.exists())

    def test_keep_zero_is_refused_before_it_deletes_the_backup_it_takes(self):
        # The run's own snapshot is one of the N it keeps, so 0 would remove
        # it and then fail the row prune that re-verifies it.
        earlier = run(home=self.home)
        with self.assertRaises(BackupError) as raised:
            run(home=self.home, keep=0)
        self.assertIn("at least 1", str(raised.exception))
        self.assertEqual(sorted(backup_root(self.home).iterdir()), [earlier.directory])


@hub_only
class TheConcurrentWriter(BackupCase):
    """The source counts and the copy are one moment, whoever writes (sd:3121).

    On 2026-10-09 the nightly run failed `state: source 34476, snapshot
    34478`: two rows landed between the count and the copy.
    """

    def writer_after_the_count(self):
        """Patch `_counts` so another connection commits right after the source count."""
        original = backup_module._counts
        landed = []

        def counted(connection):
            found = original(connection)
            if not landed:
                writer = connect(home=self.home)
                try:
                    landed.append(record_state(writer, "watermark", key="concurrent"))
                finally:
                    writer.close()
            return found

        self.enterContext(mock.patch.object(backup_module, "_counts", counted))
        return landed

    def test_a_write_between_the_count_and_the_copy_does_not_fail_the_comparison(self):
        landed = self.writer_after_the_count()
        snapshot = run(home=self.home)
        self.assertTrue(landed)
        copy = sqlite3.connect(f"file:{snapshot.directory / 'sd.db'}?mode=ro", uri=True)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("SELECT count(*) FROM state").fetchone()[0], snapshot.counts["state"])
        self.assertEqual(copy.execute("SELECT count(*) FROM state WHERE id = ?", landed).fetchone()[0], 0)
        source = self.open()
        self.assertEqual(source.execute("SELECT count(*) FROM state").fetchone()[0], snapshot.counts["state"] + 1)

    def test_the_copy_is_a_closed_single_file(self):
        snapshot = run(home=self.home)
        copy = snapshot.directory / "sd.db"
        # Header bytes 18 and 19 are 1 for a rollback journal and 2 for WAL.
        self.assertEqual(copy.read_bytes()[18:20], b"\x01\x01")
        self.assertEqual(sorted(path.name for path in snapshot.directory.glob("sd.db*")), ["sd.db"])

    def test_a_run_killed_before_the_journal_switch_leaves_an_unowned_directory_the_next_run_keeps(self):
        script = (
            "import os, sqlite3, sys\n"
            "from sd_db.backup import run\n"
            "real = sqlite3.connect\n"
            "class Killed(sqlite3.Connection):\n"
            "    def execute(self, sql, *args):\n"
            "        if sql == 'PRAGMA journal_mode = DELETE':\n"
            "            os._exit(9)\n"
            "        return super().execute(sql, *args)\n"
            # In place of any factory: the library's own write open names one (system sd:5986).
            "sqlite3.connect = lambda *a, **k: real(*a, **{**k, 'factory': Killed})\n"
            "run(home=sys.argv[1])\n"
        )
        self.assertEqual(self.killed(script, self.home).returncode, 9)
        [left] = backup_root(self.home).iterdir()
        self.assertEqual((left / "sd.db").read_bytes()[18:20], b"\x02\x02")
        self.assertFalse((left / BACKUP_MANIFEST).exists())

        snapshot = run(home=self.home, keep=1)
        self.assertEqual(snapshot.removed, [])
        self.assertEqual(sorted(backup_root(self.home).iterdir()), sorted([left, snapshot.directory]))


class TheDestinationProbe(BackupCase):
    """A destination that does not answer fails the run in seconds (sd:2660).

    On some nights macOS stops answering permission checks for launchd jobs.
    Each open then fails with EINTR, Python retries it, and the backup ran
    until the job's two-hour limit killed it. A child that sleeps stands in
    for the blocked open: no test can make macOS stall on purpose.
    """

    def hang(self):
        # `create=True`: the attributes are this change's, and without it a
        # run against the code before it errors out instead of failing.
        return mock.patch.multiple(backup_module, create=True,
                                   _PROBE="import time; time.sleep(60)", PROBE_SECONDS=0.5)

    def checkpoints(self):
        connection = self.open(write=False)
        return connection.execute(
            "SELECT count(*) FROM state WHERE kind = 'checkpoint'").fetchone()[0]

    @hub_only
    def test_a_destination_that_does_not_answer_fails_within_the_bound(self):
        root = backup_root(self.home)
        root.mkdir(parents=True)
        started = time.monotonic()

        with self.hang(), self.assertRaises(BackupError) as raised:
            run(home=self.home)

        self.assertEqual(str(raised.exception),
                         f"backup destination {root} did not answer within 0.5 s")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(self.checkpoints(), 0)

    @hub_only
    def test_the_job_exits_1_naming_the_destination(self):
        stderr = io.StringIO()
        with self.hang(), mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                contextlib.redirect_stderr(stderr):
            status = job_backup.main([])

        self.assertEqual(status, 1)
        self.assertEqual(stderr.getvalue(), f"sd-db backup: backup destination "
                         f"{backup_root(self.home)} did not answer within 0.5 s\n")

    def test_the_bound_is_thirty_seconds(self):
        self.assertEqual(backup_module.PROBE_SECONDS, 30)

    @hub_only
    def test_the_probe_leaves_no_file_behind(self):
        snapshot = run(home=self.home)
        self.assertEqual(list(backup_root(self.home).iterdir()), [snapshot.directory])


@hub_only
class TheRestoreAndCompare(BackupCase):
    def test_an_empty_database_backs_up_and_restores(self):
        snapshot = run(home=self.home)
        self.assertEqual(snapshot.counts["item"], 0)

    def test_one_runs_directory_with_another_runs_id_is_not_a_passed_backup(self):
        """`passed` asked two questions about two different runs: is this
        directory *some* owned backup, and does *some* run's checkpoint row
        exist here. With two valid runs on one machine, the first directory
        under the second run's id answered yes to both -- and the nightly
        prune ran against a snapshot that is not the one it names."""
        first = run(home=self.home, when=datetime(2026, 1, 1, tzinfo=UTC))
        second = run(home=self.home, when=datetime(2026, 1, 2, tzinfo=UTC))
        connection = self.open(write=False)
        self.assertTrue(passed(connection, first))
        self.assertTrue(passed(connection, second))
        crossed = dataclasses.replace(first, run_id=second.run_id)
        self.assertFalse(passed(connection, crossed))
        self.assertFalse(passed(connection, dataclasses.replace(second, run_id=first.run_id)))


    def test_a_database_whose_newest_row_is_a_month_old_backs_up_and_restores(self):
        connection = self.open()
        item = create_item(connection, kind="work", title="Old")
        old = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
        connection.execute("UPDATE item SET created_at = ?, updated_at = ? WHERE id = ?",
                           (old, old, item))
        connection.execute("UPDATE note SET timestamp = ?", (old,))
        snapshot = run(home=self.home)
        self.assertEqual(snapshot.counts["item"], 1)

    def test_a_database_with_a_fresh_row_backs_up_and_restores(self):
        connection = self.open()
        create_item(connection, kind="work", title="New")
        snapshot = run(home=self.home)
        self.assertEqual(snapshot.counts["item"], 1)
        self.assertEqual(snapshot.counts["note"], 1)

    def test_a_truncated_copy_fails_the_comparison(self):
        connection = self.open()
        for index in range(5):
            create_item(connection, kind="work", title=f"Item {index}")
        snapshot = run(home=self.home)
        copy = snapshot.directory / "sd.db"

        # Cut the copy short: the file is still a file, and is no longer the
        # database that was checked.
        data = copy.read_bytes()
        copy.write_bytes(data[: len(data) // 2])

        with self.assertRaises(BackupError) as raised:
            verify(copy, run_id=snapshot.run_id, expected=snapshot.counts)
        message = str(raised.exception)
        self.assertTrue(
            "integrity_check" in message or "different counts" in message, message
        )

    def test_a_copy_from_another_run_is_refused_by_its_checkpoint(self):
        first = run(home=self.home)
        second = run(home=self.home)
        with self.assertRaises(BackupError) as raised:
            verify(first.directory / "sd.db", run_id=second.run_id, expected=second.counts)
        self.assertIn("some other moment", str(raised.exception))


@hub_only
class PaletteEvidence(BackupCase):
    def execution(self):
        from sd_db import runner_exec, workflow
        connection = self.open()
        repo = self.home / "repo"
        repo.mkdir()
        upsert_repo(connection, str(repo))
        item = create_item(connection, kind="task", title="Palette backup", repo=str(repo))
        program = self.home / "fixture-command"
        program.write_text("#!/usr/bin/python3\nprint('preserved execution evidence')\n")
        program.chmod(0o700)
        catalog = self.state / "commands.yaml"
        catalog.write_text("version: 1\ncommands:\n  inspect: " + json.dumps({"argv": [str(program)],
            "screens": ["item"], "scope": "worktree", "mutates": False}) + "\n")
        prepared = runner_exec.prepare(connection, item, "inspect", {}, expected_revision=workflow.item_state(connection, item)["revision"],
            expected_catalog=runner_exec.catalog(home=self.home)["sha256"], home=self.home, who="operator")["execution"]
        runner_exec.execute_immediate(connection, prepared["note"], home=self.home)
        return prepared

    def test_catalog_and_completed_log_restore_readably_to_a_fresh_home(self):
        from sd_db import runner_exec
        value = self.execution()
        snapshot = run(home=self.home)
        self.assertIn("execution-evidence.json", snapshot.configuration)
        other = Path(self.tmp.name) / "other"
        restored = restore(snapshot.directory, home=other)
        connection = connect(restored)
        self.addCleanup(connection.close)
        output = runner_exec.read_execution(connection, value["note"])
        self.assertEqual(output["exit_code"], 0)
        self.assertEqual(output["output"], "preserved execution evidence\n")
        self.assertEqual(Path(output["output_path"]).parent, restored.resolve().parent / "executions")
        self.assertEqual((restored.parent / "commands.yaml").read_bytes(), (self.state / "commands.yaml").read_bytes())
        self.assertEqual(json.loads((snapshot.directory / "execution-evidence.json").read_text())["notes"][str(value["note"])]["output_path"], value["output_path"])

    def test_missing_completed_log_is_not_a_complete_backup(self):
        value = self.execution()
        Path(value["output_path"]).rename(self.home / "saved-log")
        with self.assertRaisesRegex(BackupError, "missing its output"):
            run(home=self.home)

    def test_catalog_or_log_corruption_refuses_before_live_database_changes(self):
        value = self.execution()
        snapshot = run(home=self.home)
        connection = self.open()
        before = tuple(connection.iterdump())
        for path in (snapshot.directory / "commands.yaml", snapshot.directory / "executions" / Path(value["output_path"]).name):
            original = path.read_bytes()
            path.write_bytes(original + b"corruption")
            with self.assertRaisesRegex(BackupError, "differs from its snapshot"):
                restore(snapshot.directory, home=self.home)
            self.assertEqual(tuple(connection.iterdump()), before)
            path.write_bytes(original)

    def test_restore_preserves_newer_logs_and_refuses_conflicting_same_name(self):
        value = self.execution()
        snapshot = run(home=self.home)
        newer = self.state / "executions" / ("b" * 32 + ".log")
        newer.write_bytes(b"newer evidence")
        newer.chmod(0o600)
        restore(snapshot.directory, home=self.home)
        self.assertEqual(newer.read_bytes(), b"newer evidence")
        live = Path(value["output_path"])
        live.write_bytes(b"different live evidence")
        with self.assertRaisesRegex(BackupError, "overwrite different"):
            restore(snapshot.directory, home=self.home)
        self.assertEqual(live.read_bytes(), b"different live evidence")

    def test_symlink_log_refuses_without_following_it(self):
        value = self.execution()
        original = Path(value["output_path"])
        saved = self.home / "saved-log"
        original.rename(saved)
        original.symlink_to(saved)
        with self.assertRaisesRegex(BackupError, "unsafe"):
            run(home=self.home)

    def test_interrupted_log_install_retries_without_partial_or_replaced_evidence(self):
        value = self.execution()
        snapshot = run(home=self.home)
        other = Path(self.tmp.name) / "interrupted-home"
        initialise(home=other)
        script = """
import os,sys
from sd_db import runner_exec_backup
from sd_db.backup import restore
original = runner_exec_backup.os.link
def interrupted(source, destination):
    original(source, destination)
    if str(destination).endswith('.log'):
        os._exit(77)
runner_exec_backup.os.link = interrupted
restore(sys.argv[1], home=sys.argv[2])
"""
        result = self.killed(script, snapshot.directory, other)
        self.assertEqual(result.returncode, 77, result.stderr)
        restored_log = other / ".local/share/sd/executions" / Path(value["output_path"]).name
        self.assertEqual(restored_log.read_bytes(), Path(value["output_path"]).read_bytes())
        restored = restore(snapshot.directory, home=other)
        from sd_db import runner_exec
        connection = connect(restored)
        self.addCleanup(connection.close)
        self.assertEqual(runner_exec.read_execution(connection, value["note"])["exit_code"], 0)



@hub_only
class TheRestore(BackupCase):
    def test_a_restored_database_lands_as_an_unresolved_record(self):
        connection = self.open()
        create_item(connection, kind="work", title="Before")
        snapshot = run(home=self.home)

        other = Path(self.tmp.name) / "other-home"
        (other / ".local/share/sd").mkdir(parents=True)
        target = restore(snapshot.directory, home=other)

        self.assertTrue(target.exists())
        restored = connect(target, home=other)
        self.addCleanup(restored.close)
        open_rows = unresolved_state(restored, "restore")
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["key"], snapshot.directory.name)
        self.assertEqual(
            restored.execute("SELECT count(*) FROM item").fetchone()[0], 1
        )

    def test_the_configuration_comes_back_with_it(self):
        self.configuration()
        snapshot = run(home=self.home)
        other = Path(self.tmp.name) / "third-home"
        (other / ".local/share/sd").mkdir(parents=True)
        restore(snapshot.directory, home=other)
        self.assertTrue((other / ".local/share/sd/providers.yaml").is_file())

    def test_a_directory_with_no_database_is_refused(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(BackupError):
            restore(empty, home=self.home)


@hub_only
class PublicationEvidence(BackupCase):
    def claim(self, name="a" * 32):
        from sd_db import publication_journal as journal
        connection = self.open()
        item = create_item(connection, kind="idea", title="Publication fixture")
        payload = {"repo": "/fixture", "piece": name, "html": "<p>Fixture</p>"}
        state = {"phase": "prepared"}
        journal.create(connection, name, item, payload, state)
        connection.execute("INSERT INTO publication_claim VALUES (?,?,?,?,?,?,?)",
                           (name, item, item, json.dumps(payload), json.dumps(state), "fixture", "fixture"))
        return connection, name

    def files(self, root=None):
        root = root or self.state / "publications"
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def test_complete_journal_is_backed_up_and_restored_to_a_fresh_home(self):
        self.claim()
        before = self.files()
        snapshot = run(home=self.home)
        self.assertIn("publications", snapshot.configuration)
        self.assertEqual(self.files(snapshot.directory / "publications"), before)
        other = Path(self.tmp.name) / "fresh-home"
        restore(snapshot.directory, home=other)
        self.assertEqual(self.files(other / ".local/share/sd/publications"), before)

    def test_newer_live_events_and_claims_survive_an_older_database_restore(self):
        from sd_db import publication_journal as journal
        connection, claim = self.claim()
        snapshot = run(home=self.home)
        journal.append(connection, claim, {"phase": "importing", "pending": "already-sent"})
        self.claim("b" * 32)
        newer = self.files()
        restore(snapshot.directory, home=self.home)
        self.assertEqual(self.files(), newer)
        self.assertEqual(len(journal.validate_path(self.state / "publications")), 2)

    def test_newer_backup_adds_missing_events_to_an_older_compatible_live_journal(self):
        from sd_db import publication_journal as journal
        connection, claim = self.claim()
        old = run(home=self.home)
        other = Path(self.tmp.name) / "older-live"
        restore(old.directory, home=other)
        journal.append(connection, claim, {"phase": "importing", "pending": "already-sent"})
        self.claim("b" * 32)
        new = run(home=self.home)
        restore(new.directory, home=other)
        self.assertEqual(self.files(other / ".local/share/sd/publications"), self.files())

    def test_corrupt_backup_journal_refuses_before_database_or_live_evidence_changes(self):
        connection, claim = self.claim()
        snapshot = run(home=self.home)
        before, evidence = "\n".join(connection.iterdump()), self.files()
        (snapshot.directory / "publications" / claim / "00000001.json").write_text('{"previous_sha256":"wrong","state":{}}')
        with self.assertRaisesRegex(BackupError, "publication journal"):
            restore(snapshot.directory, home=self.home)
        self.assertEqual("\n".join(connection.iterdump()), before)
        self.assertEqual(self.files(), evidence)

    def test_corrupt_live_journal_is_preserved_and_never_replaced_with_an_older_copy(self):
        connection, claim = self.claim()
        snapshot = run(home=self.home)
        (self.state / "publications" / claim / "00000002.json").write_text("corrupt fixture")
        before, evidence = "\n".join(connection.iterdump()), self.files()
        with self.assertRaisesRegex(BackupError, "publication journal"):
            restore(snapshot.directory, home=self.home)
        self.assertEqual("\n".join(connection.iterdump()), before)
        self.assertEqual(self.files(), evidence)

    def test_divergent_valid_event_chains_refuse_without_overwriting_either(self):
        from sd_db import publication_journal as journal
        connection, claim = self.claim()
        old = run(home=self.home)
        other = Path(self.tmp.name) / "divergent-home"
        restore(old.directory, home=other)
        other_db = connect(home=other)
        self.addCleanup(other_db.close)
        journal.append(other_db, claim, {"phase": "importing", "pending": "first-result"})
        journal.append(connection, claim, {"phase": "importing", "pending": "different-result"})
        newer = run(home=self.home)
        before = "\n".join(other_db.iterdump())
        evidence = self.files(other / ".local/share/sd/publications")
        with self.assertRaisesRegex(BackupError, "conflicts with live evidence"):
            restore(newer.directory, home=other)
        self.assertEqual("\n".join(other_db.iterdump()), before)
        self.assertEqual(self.files(other / ".local/share/sd/publications"), evidence)

    def test_missing_journal_with_database_claims_is_not_a_complete_backup(self):
        self.claim()
        (self.state / "publications").rename(self.state / "preserved-publications")
        with self.assertRaisesRegex(BackupError, "journal is missing"):
            run(home=self.home)

    def test_a_valid_but_unrelated_journal_does_not_prove_database_claims(self):
        from sd_db import publication_journal as journal
        self.claim()
        snapshot = run(home=self.home)
        (snapshot.directory / "publications").rename(snapshot.directory / "preserved-publications")
        separate = Path(self.tmp.name) / "unrelated"
        initialise(home=separate)
        unrelated = connect(home=separate)
        self.addCleanup(unrelated.close)
        journal.initialise(unrelated)
        shutil.copytree(separate / ".local/share/sd/publications", snapshot.directory / "publications")
        before = self.database.read_bytes()
        with self.assertRaisesRegex(BackupError, "differs from the backup journal"):
            restore(snapshot.directory, home=self.home)
        self.assertEqual(self.database.read_bytes(), before)

    def test_a_legacy_snapshot_without_journal_keeps_future_publication_held(self):
        from sd_db import publication_journal as journal
        from sd_db.workflow import WorkflowError
        snapshot = run(home=self.home)
        other = Path(self.tmp.name) / "legacy-home"
        target = restore(snapshot.directory, home=other)
        connection = connect(target)
        self.addCleanup(connection.close)
        with self.assertRaisesRegex(WorkflowError, "journal is missing after restore"):
            journal.for_piece(connection, "/fixture", "piece")
        self.assertFalse((target.parent / "publications").exists())

    def test_killed_dispatched_journal_restore_stays_held_and_resumes_from_verified_material(self):
        from sd_db import publication_journal as journal
        from sd_db.workflow import WorkflowError
        connection, claim = self.claim()
        journal.append(connection, claim, {"phase": "importing", "pending": "already-sent"})
        snapshot = run(home=self.home)
        other = Path(self.tmp.name) / "interrupted-home"
        initialise(home=other)
        script = """import importlib,os,sys
from pathlib import Path
backup = importlib.import_module('sd_db.backup')
original = backup._write_recovery_record
live = Path(sys.argv[2]) / '.local/share/sd/publications'
def interrupt(path, data):
    original(path, data)
    if path.is_relative_to(live) and path.name == '00000001.json':
        os._exit(79)
backup._write_recovery_record = interrupt
backup.restore(sys.argv[1], home=sys.argv[2])
"""
        result = self.killed(script, snapshot.directory, other)
        self.assertEqual(result.returncode, 79, result.stderr)
        state = other / ".local/share/sd"
        self.assertTrue((state / "publication-restore-intent.json").is_file())
        restored = connect(home=other)
        self.addCleanup(restored.close)
        self.assertEqual(restored.execute("SELECT COUNT(*) FROM publication_claim").fetchone()[0], 0)
        with self.assertRaisesRegex(WorkflowError, "restore"):
            journal.for_piece(restored, "/fixture", claim)
        with self.assertRaisesRegex(WorkflowError, "restore"):
            journal.repair_incomplete(restored)
        restore(snapshot.directory, home=other)
        self.assertFalse((state / "publication-restore-intent.json").exists())
        self.assertEqual(self.files(state / "publications"), self.files())
        self.assertEqual(journal.validate_path(state / "publications")[claim]["state"]["pending"], "already-sent")


@hub_only
class RunnerEvidence(BackupCase):
    def run_record(self, name="c" * 32):
        from sd_db import runner_journal as journal
        connection = self.open()
        upsert_repo(connection, "/fixture")
        assignment = create_assignment(connection, role="worker", status="queued")
        connection.execute("INSERT INTO runner_run (id,assignment,run,repo,branch,owner,journal_version,work_path,retained_path,created_at,updated_at) "
                           "VALUES (?,?,1,'/fixture','fixture','fixture',1,'/tmp/work','/tmp/retained','fixture','fixture')", (name, assignment))
        record = dict(connection.execute("SELECT * FROM runner_run WHERE id=?", (name,)).fetchone())
        journal.persist(self.database, record)
        return connection, record

    def test_complete_run_journal_is_backed_up_and_restored(self):
        from sd_db import runner_journal as journal
        _, record = self.run_record()
        snapshot = run(home=self.home)
        self.assertIn("runner-journal", snapshot.configuration)
        self.assertEqual(journal.validate_path(snapshot.directory / "runner-journal"), [record])
        other = Path(self.tmp.name) / "runner-home"
        target = restore(snapshot.directory, home=other)
        self.assertEqual(journal.records(target), [record])
        self.assertFalse((target.parent / "runner-restore-intent.json").exists())

    def test_the_snapshot_leaves_the_run_locks_out(self):
        # sd:6071: a lock is an empty flock file restore makes again; copied, rsync's open
        # of one failed the NAS mirror, and listed, offsite-verify failed on the one it missed.
        _, record = self.run_record()
        self.assertTrue((self.state / "runner-journal" / f"{record['id']}.lock").exists())
        snapshot = run(home=self.home)
        self.assertEqual(sorted(path.name for path in (snapshot.directory / "runner-journal").iterdir()),
                         [f"{record['id']}.json"])
        entries = json.loads((snapshot.directory / BACKUP_MANIFEST).read_text())["entries"]
        self.assertEqual([name for name in entries if name.endswith(".lock")], [])

    def test_newer_live_run_record_is_never_rolled_back(self):
        from sd_db import runner_journal as journal
        _, record = self.run_record()
        snapshot = run(home=self.home)
        newer = {**record, "journal_version": 2, "start_step": "launched"}
        journal.persist(self.database, newer)
        self.run_record("d" * 32)
        before = journal.records(self.database)
        restore(snapshot.directory, home=self.home)
        self.assertEqual(journal.records(self.database), before)

    def test_newer_snapshot_does_not_replace_existing_live_process_ownership(self):
        from sd_db import runner_journal as journal
        connection, record = self.run_record()
        old = run(home=self.home)
        other = Path(self.tmp.name) / "older-runner-home"
        target = restore(old.directory, home=other)
        connection.execute("UPDATE runner_run SET journal_version=2,start_step='launched' WHERE id=?", (record["id"],))
        journal.persist(self.database, dict(connection.execute("SELECT * FROM runner_run").fetchone()))
        newer = run(home=self.home)
        restore(newer.directory, home=other)
        self.assertEqual(journal.records(target), [record])

    def test_same_version_conflict_refuses_without_overwriting_database(self):
        import hashlib

        from sd_db import runner_journal as journal
        connection, record = self.run_record()
        snapshot = run(home=self.home)
        conflict = {**record, "detail": "conflicting ownership evidence"}
        envelope = {"record": conflict, "sha256": hashlib.sha256(journal._bytes(conflict)).hexdigest()}
        (snapshot.directory / "runner-journal" / f"{record['id']}.json").write_bytes(journal._bytes(envelope))
        before = "\n".join(connection.iterdump())
        with self.assertRaisesRegex(BackupError, "conflicts with live evidence"):
            restore(snapshot.directory, home=self.home)
        self.assertEqual("\n".join(connection.iterdump()), before)
        self.assertEqual(journal.records(self.database), [record])

    def test_corrupt_run_journal_refuses_before_restoring_database(self):
        connection, record = self.run_record()
        snapshot = run(home=self.home)
        (snapshot.directory / "runner-journal" / f"{record['id']}.json").write_text("corrupt fixture")
        before = "\n".join(connection.iterdump())
        with self.assertRaisesRegex(BackupError, "runner journal"):
            restore(snapshot.directory, home=self.home)
        self.assertEqual("\n".join(connection.iterdump()), before)

    def links(self, state):
        live = {path.name: path.stat().st_nlink for path in (state / "runner-journal").glob("*.json")}
        installs = sorted(path.name for path in (state / "runner-recovery-evidence").glob("restore-*/install-*"))
        return live, installs

    def interrupted_restore(self, snapshot, other, when):
        initialise(home=other)
        script = """import importlib,os,sys
from pathlib import Path
backup = importlib.import_module('sd_db.backup')
original = os.link
live = Path(sys.argv[2]) / '.local/share/sd/runner-journal'
def interrupt(source, target):
    if Path(target).parent == live and sys.argv[3] == 'before':
        os._exit(77)
    original(source, target)
    if Path(target).parent == live:
        os._exit(78)
backup.os.link = interrupt
backup.restore(sys.argv[1], home=sys.argv[2])
"""
        result = self.killed(script, snapshot.directory, other, when)
        self.assertEqual(result.returncode, 77 if when == "before" else 78, result.stderr)
        return other / ".local/share/sd"

    def test_restored_journal_files_keep_one_link_and_evidence_keeps_its_copy(self):
        # sd:779: a name removed from the live journal came back as a second link to restore material.
        from sd_db import runner_journal as journal
        self.run_record()
        self.run_record("d" * 32)
        snapshot = run(home=self.home)
        (self.state / "runner-journal" / ("c" * 32 + ".json")).unlink()
        restore(snapshot.directory, home=self.home)
        self.assertEqual(self.links(self.state), ({"c" * 32 + ".json": 1, "d" * 32 + ".json": 1}, []))
        material = next((self.state / "runner-recovery-evidence").glob("restore-*/runner-journal"))
        self.assertEqual(journal.validate_path(material), journal.records(self.database))

    def test_a_killed_restore_leaves_nothing_in_the_outer_temp_dir(self):
        """sd:3059: a restore killed mid-run never reaches the `finally` that removes its `sd-restore-` stage."""
        self.run_record()
        snapshot = run(home=self.home)
        outer = Path(self.tmp.name) / "outer-tmp"
        outer.mkdir()
        with mock.patch.dict(os.environ, {"TMPDIR": str(outer)}):
            self.interrupted_restore(snapshot, Path(self.tmp.name) / "killed-home", "before")
        self.assertEqual(sorted(path.name for path in outer.iterdir()), [])

    def test_crash_before_the_link_resumes_with_one_link_and_no_install_copy(self):
        from sd_db import runner_journal as journal
        self.run_record()
        self.run_record("d" * 32)
        snapshot = run(home=self.home)
        state = self.interrupted_restore(snapshot, Path(self.tmp.name) / "before-link-home", "before")
        self.assertTrue((state / "runner-restore-intent.json").is_file())
        live, installs = self.links(state)
        self.assertEqual((live, len(installs)), ({}, 1))
        restore(snapshot.directory, home=state.parent.parent.parent)
        self.assertFalse((state / "runner-restore-intent.json").exists())
        self.assertEqual(self.links(state), ({"c" * 32 + ".json": 1, "d" * 32 + ".json": 1}, []))
        self.assertEqual(journal.records(state / "sd.db"), journal.records(self.database))

    def test_interrupted_run_journal_restore_retains_intent_and_resumes_missing_ids(self):
        from sd_db import runner_journal as journal
        self.run_record()
        self.run_record("d" * 32)
        snapshot = run(home=self.home)
        other = Path(self.tmp.name) / "interrupted-runner-home"
        state = self.interrupted_restore(snapshot, other, "between")
        target = state / "sd.db"
        self.assertTrue((target.parent / "runner-restore-intent.json").is_file())
        self.assertEqual(len(journal.records(target)), 1)
        # Between the link and the unlink the install copy is the live file's second name.
        live, installs = self.links(state)
        self.assertEqual((list(live.values()), installs), ([2], ["install-" + name for name in live]))
        with self.assertRaisesRegex(BackupError, "restore is incomplete"):
            run(home=other)
        restore(snapshot.directory, home=other)
        self.assertFalse((target.parent / "runner-restore-intent.json").exists())
        self.assertEqual(journal.records(target), journal.records(self.database))
        self.assertEqual(self.links(state), ({"c" * 32 + ".json": 1, "d" * 32 + ".json": 1}, []))

    def test_completed_recovery_evidence_is_included_with_a_hash_inventory(self):
        self.run_record()
        snapshot = run(home=self.home)
        restore(snapshot.directory, home=self.home)
        copied = run(home=self.home)
        inventory = json.loads((copied.directory / "recovery-evidence-sha256.json").read_bytes())
        self.assertTrue(inventory["runner-recovery-evidence"])
        self.assertIn("runner-recovery-evidence", copied.configuration)

    def test_a_restores_install_copy_is_not_evidence_and_does_not_fail_the_backup(self):
        """`install-<record>` lives under the archive between `os.link` and
        `install.unlink()`. A restore that starts after `_copy_runner_journal`
        has already passed its intent check leaves one inside this walk: the
        quiet half copies a live record into the archive, and the loud half
        fails the whole night with "recovery archive changed during backup"
        when it goes away between the hash pass and the copy.
        """
        self.run_record()
        snapshot = run(home=self.home)
        restore(snapshot.directory, home=self.home)
        material = next((self.state / "runner-recovery-evidence").glob("restore-*"))
        transient = material / ("install-" + "c" * 32 + ".json")
        transient.write_bytes(b"a restore in flight")

        copied = run(home=self.home)
        archive = copied.directory / "runner-recovery-evidence"
        self.assertEqual(list(archive.glob("restore-*/install-*")), [])
        inventory = json.loads((copied.directory / "recovery-evidence-sha256.json").read_bytes())
        self.assertNotIn(f"{material.name}/{transient.name}", inventory["runner-recovery-evidence"])
        self.assertTrue(inventory["runner-recovery-evidence"])

        # The same file removed mid-walk, which is what the other restore does.
        original = backup_module.shutil.copytree

        def vanishing(*args, **kwargs):
            # `copytree` recurses through this same name; only the outer call
            # stands for the walk the other restore is racing.
            if transient.exists():
                transient.unlink()
            return original(*args, **kwargs)

        transient.write_bytes(b"a restore in flight")
        with mock.patch.object(backup_module.shutil, "copytree", vanishing):
            again = run(home=self.home)
        self.assertEqual(list((again.directory / "runner-recovery-evidence").glob("restore-*/install-*")), [])

    def test_an_install_named_file_that_is_not_the_generated_one_stays_evidence(self):
        """PR #497 review. Both predicates matched every `install-*` under a
        `restore-*` directory, but `_restore_runner_journal` makes exactly one
        name: `install-<run id>.json`, and a run id is a `uuid4().hex`. A
        corrupted or hand-written `install-notes.txt` beside a restore is the
        artifact the evidence check exists to keep, and it was dropped from
        both the hash and the archive without a word.
        """
        self.run_record()
        snapshot = run(home=self.home)
        restore(snapshot.directory, home=self.home)
        material = next((self.state / "runner-recovery-evidence").glob("restore-*"))
        kept = material / "install-notes.txt"
        kept.write_bytes(b"what the operator found")
        dropped = material / ("install-" + "c" * 32 + ".json")
        dropped.write_bytes(b"a restore in flight")

        copied = run(home=self.home)
        archive = copied.directory / "runner-recovery-evidence"
        self.assertTrue((archive / material.name / kept.name).exists())
        self.assertFalse((archive / material.name / dropped.name).exists())
        inventory = json.loads((copied.directory / "recovery-evidence-sha256.json").read_bytes())
        self.assertIn(f"{material.name}/{kept.name}", inventory["runner-recovery-evidence"])
        self.assertNotIn(f"{material.name}/{dropped.name}", inventory["runner-recovery-evidence"])

    def test_the_retry_into_an_existing_link_fsyncs_the_journal_directory(self):
        """A restore killed between `os.link` and its `_sync_directory` leaves
        a link that is visible and not durable. The retry finds the target
        present and used to `continue` straight past, with
        `_drop_install_copy` having fsynced only the material directory -- so
        the intent was replaced and the restore read as finished over a link
        a power loss removes.

        One record, so the existing-target path is the only path taken: with
        a second record the normal path fsyncs the journal for it and the
        gap is invisible.
        """
        self.run_record()
        snapshot = run(home=self.home)
        other = Path(self.tmp.name) / "resumed-runner-home"
        state = self.interrupted_restore(snapshot, other, "between")
        journal = state / "runner-journal"
        self.assertTrue((journal / ("c" * 32 + ".json")).is_file())

        synced = []
        original = backup_module._sync_directory

        def recording(path):
            synced.append(Path(path))
            return original(path)

        with mock.patch.object(backup_module, "_sync_directory", recording):
            restore(snapshot.directory, home=other)
        self.assertFalse((state / "runner-restore-intent.json").exists())
        self.assertIn(journal, synced)


def one_migration_back(database):
    """Put a database one migration back, the way a machine mid-upgrade is.

    This reverses the *last* migration, so it moves with `SCHEMA_VERSION`.
    Reversing an earlier one instead would label a current-shape database
    with the version before it and leave the upgrade nothing real to do.

    A module function and not a method: two classes need it, and one of them
    is a `JobRun` that cannot inherit from the other without inheriting its
    tests.
    """
    raw = sqlite3.connect(database, isolation_level=None)
    try:
        # The reverse of 048_home_asset.sql, which its header carries as `--   ` lines: the
        # `home_asset` table goes, and the version goes to 47. The helper is rewritten with every
        # migration: a migration that adds a table or a column would be refused if left in place.
        text = (SCHEMA_DIR / "048_home_asset.sql").read_text()
        script = "\n".join(line[4:] for line in text.splitlines() if line.startswith("--   "))
        assert "PRAGMA user_version = 47" in script, \
            "048's reverse is not where this helper expects it"
        raw.executescript(script)
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION - 1
    finally:
        raw.close()


class TheDatabaseWaitingForMigrate(BackupCase):
    """The backup `migrate` asks for first must not need the migration first.

    On 2026-09-13 a schema bump was deployed and `sd-db.sh backup` refused
    with the message that told the operator to take a backup: the writable
    open is the version gate, and the checkpoint row is a write. The copy is
    taken read-only instead, proved by counts, and kept out of retention.
    """

    def older(self):
        one_migration_back(self.database)


    def version(self, path):
        raw = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return raw.execute("PRAGMA user_version").fetchone()[0]
        finally:
            raw.close()

    @hub_only
    def test_the_copy_is_taken_read_only_and_proved_by_counts(self):
        connection = self.open()
        create_item(connection, kind="work", title="Before the migration")
        connection.close()
        self.older()
        before = self.database.read_bytes()

        snapshot = run(home=self.home)

        self.assertFalse(snapshot.checkpointed)
        copy = snapshot.directory / "sd.db"
        self.assertTrue(copy.is_file())
        self.assertEqual(self.version(copy), SCHEMA_VERSION - 1)
        self.assertEqual(snapshot.counts["item"], 1)
        # No write reached the source: no checkpoint row, no version change, no byte moved.
        self.assertEqual(self.version(self.database), SCHEMA_VERSION - 1)
        self.assertEqual(self.database.read_bytes(), before)
        raw = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("SELECT count(*) FROM state WHERE kind = 'checkpoint'").fetchone()[0], 0)

    @hub_only
    def test_the_snapshot_is_never_owned_by_retention_and_never_passes_a_prune(self):
        self.older()
        snapshot = run(home=self.home)
        manifest = json.loads((snapshot.directory / BACKUP_MANIFEST).read_text())
        self.assertIs(manifest["checkpoint"], False)
        # `--keep 0` deletes every owned backup; this one stays.
        self.assertEqual(prune(backup_root(self.home), 0), [])
        self.assertTrue(snapshot.directory.is_dir())
        # `passed` re-verifies through the checkpoint row, so the nightly prune refuses it.
        connection = self.open(write=False)
        self.assertFalse(passed(connection, snapshot))

    @hub_only
    def test_a_current_database_still_writes_its_checkpoint_row(self):
        snapshot = run(home=self.home)
        self.assertTrue(snapshot.checkpointed)
        manifest = json.loads((snapshot.directory / BACKUP_MANIFEST).read_text())
        self.assertIs(manifest["checkpoint"], True)
        self.assertEqual(prune(backup_root(self.home), 0), [snapshot.directory])

    @hub_only
    def test_the_snapshot_restores_after_the_library_moved_on(self):
        connection = self.open()
        create_item(connection, kind="work", title="Survives")
        connection.close()
        self.older()
        snapshot = run(home=self.home)
        other = Path(self.tmp.name) / "migrated-home"
        (other / ".local/share/sd").mkdir(parents=True)
        target = restore(snapshot.directory, home=other)
        restored = connect(target, home=other)
        self.addCleanup(restored.close)
        self.assertEqual(restored.execute("SELECT count(*) FROM item").fetchone()[0], 1)

    @hub_only
    def test_a_database_older_than_the_auxiliary_tables_still_backs_up(self):
        """The read-only fallback is taken for *every* older schema, and the
        evidence checks after it queried `publication_claim` (migration 004)
        and `runner_run` (005) unconditionally. On a valid version 3 database
        -- the shape the fallback exists to serve -- `run` died with a bare
        `no such table` from sqlite3 rather than this module's `BackupError`.
        """
        self.database.unlink()
        raw = sqlite3.connect(self.database, isolation_level=None)
        try:
            for version, path in migrations():
                if version <= 3:
                    raw.executescript(path.read_text(encoding="utf-8"))
            raw.execute("PRAGMA user_version = 3")
            raw.execute("INSERT INTO item (kind,title,status,created_at,updated_at) "
                        "VALUES ('work','From before the claims','planning',?,?)",
                        ("2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"))
        finally:
            raw.close()
        self.assertEqual(self.version(self.database), 3)

        snapshot = run(home=self.home)

        self.assertFalse(snapshot.checkpointed)
        self.assertEqual(snapshot.counts["item"], 1)
        self.assertNotIn("publication_claim", snapshot.counts)
        self.assertNotIn("runner_run", snapshot.counts)
        self.assertEqual(self.version(snapshot.directory / "sd.db"), 3)

    @hub_only
    def test_a_current_database_missing_an_evidence_table_is_refused_not_passed(self):
        """Codex on sd:1207: the guard above must read the version, not the
        table list. A version 11 database without `publication_claim` is
        malformed -- `_check_restore` refuses it -- and a snapshot of it must
        not take an ownership manifest that retention would then honour.
        """
        for table, fragment in (("publication_claim", "no publication_claim table"),
                                ("runner_run", "no runner_run table")):
            with self.subTest(table=table):
                raw = sqlite3.connect(self.database, isolation_level=None)
                try:
                    raw.execute("PRAGMA foreign_keys = OFF")
                    raw.execute(f"DROP TABLE {table}")
                finally:
                    raw.close()
                self.assertEqual(self.version(self.database), SCHEMA_VERSION)
                with self.assertRaises(BackupError) as raised:
                    run(home=self.home)
                self.assertIn(fragment, str(raised.exception))
                self.assertEqual(list(backup_root(self.home).glob(f"*/{BACKUP_MANIFEST}")), [])
                self.database.unlink()
                initialise(home=self.home)

    def test_the_job_reports_the_read_only_copy_and_exits_zero_without_a_prune(self):
        self.older()
        environment = dict(os.environ, HOME=str(self.home), PYTHON=sys.executable)
        completed = subprocess.run([sys.executable, str(ENTRYPOINT), "backup"], capture_output=True, text=True,
                                   input="", env=environment, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("taken read-only before `migrate`", completed.stdout)
        self.assertIn("prune skipped", completed.stdout)
        self.assertNotIn("did not open", completed.stderr)

    @hub_only
    def test_the_job_reports_the_snapshots_its_count_removed(self):
        # The read-only copy skips the row prune, but `--keep` still applies
        # file retention to the owned backups, and the summary must say so.
        first = datetime(2026, 9, 1, tzinfo=UTC)
        older = [run(home=self.home, when=first + timedelta(days=day)) for day in range(3)]
        self.older()
        environment = dict(os.environ, HOME=str(self.home), PYTHON=sys.executable)
        completed = subprocess.run([sys.executable, str(ENTRYPOINT), "backup", "--keep", "1"], capture_output=True,
                                   text=True, input="", env=environment, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual([snapshot.directory.exists() for snapshot in older], [False, False, True])
        self.assertIn("2 old snapshot(s) removed; row prune skipped", completed.stdout)


class MigrateNeedsABackupOfThisStore(BackupCase):
    """sd:3507: `sd-db migrate` refuses unless the newest snapshot under the
    backup root verifies and is this store: its schema version and its row
    counts. A database waiting for `migrate` refuses every library write, so
    equal counts mean the snapshot holds every row the migration rewrites."""

    def migrate(self, expect):
        environment = dict(os.environ, HOME=str(self.home), PYTHON=sys.executable)
        completed = subprocess.run([sys.executable, str(ENTRYPOINT), "migrate"], capture_output=True, text=True,
                                   input="", env=environment, check=False)
        self.assertEqual(completed.returncode, expect, completed.stdout + completed.stderr)
        return completed

    def version(self):
        raw = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        try:
            return raw.execute("PRAGMA user_version").fetchone()[0]
        finally:
            raw.close()

    def test_no_backup_is_refused_and_names_both_ways_out(self):
        one_migration_back(self.database)
        completed = self.migrate(1)
        self.assertIn(f"no backup under {backup_root(self.home)}", completed.stderr)
        self.assertIn("bin/sd-db backup", completed.stderr)
        self.assertIn("SD_DB_BACKUP_ROOT", completed.stderr)
        self.assertEqual(self.version(), SCHEMA_VERSION - 1)

    @hub_only
    def test_a_backup_of_this_store_lets_it_migrate(self):
        one_migration_back(self.database)
        snapshot = run(home=self.home)
        completed = self.migrate(0)
        self.assertIn(f"backup {snapshot.directory}", completed.stdout)
        self.assertEqual(self.version(), SCHEMA_VERSION)

    @hub_only
    def test_a_row_written_after_the_backup_is_refused(self):
        one_migration_back(self.database)
        run(home=self.home)
        raw = sqlite3.connect(self.database, isolation_level=None)
        raw.execute("INSERT INTO item (kind,title,status,created_at,updated_at) "
                    "VALUES ('task','After the backup','planning','2026-10-13T00:00:00+00:00','2026-10-13T00:00:00+00:00')")
        raw.close()
        completed = self.migrate(1)
        self.assertIn("item: store 1, backup 0", completed.stderr)
        self.assertEqual(self.version(), SCHEMA_VERSION - 1)

    @hub_only
    def test_a_backup_at_another_schema_version_is_refused(self):
        # The snapshot of the store before it moved back: its rows, another version.
        run(home=self.home)
        one_migration_back(self.database)
        completed = self.migrate(1)
        self.assertIn(f"is at schema version {SCHEMA_VERSION}, the store at {SCHEMA_VERSION - 1}", completed.stderr)
        self.assertEqual(self.version(), SCHEMA_VERSION - 1)

    @hub_only
    def test_only_the_newest_backup_counts(self):
        one_migration_back(self.database)
        good = run(home=self.home, when=datetime(2026, 10, 1, tzinfo=UTC))
        newer = run(home=self.home, when=datetime(2026, 10, 2, tzinfo=UTC))
        (newer.directory / "sd.db").write_bytes(b"not a database")
        completed = self.migrate(1)
        self.assertIn(str(newer.directory), completed.stderr)
        self.assertNotIn(str(good.directory), completed.stderr)
        self.assertEqual(self.version(), SCHEMA_VERSION - 1)

    @hub_only
    def test_a_copy_that_links_to_the_store_is_refused(self):
        one_migration_back(self.database)
        snapshot = run(home=self.home)
        (snapshot.directory / "sd.db").unlink()
        (snapshot.directory / "sd.db").symlink_to(self.database)
        completed = self.migrate(1)
        self.assertIn("refuses linked or mounted entries", completed.stderr)
        self.assertEqual(self.version(), SCHEMA_VERSION - 1)

    @hub_only
    def test_an_update_after_the_backup_is_refused(self):
        # An UPDATE leaves every count as it was; only the store's own file says it moved.
        connection = self.open()
        item = create_item(connection, kind="task", title="Before the backup")
        connection.close()
        one_migration_back(self.database)
        run(home=self.home)
        raw = sqlite3.connect(self.database, isolation_level=None)
        raw.execute("UPDATE item SET title = 'After the backup' WHERE id = ?", (item,))
        raw.close()
        completed = self.migrate(1)
        self.assertIn("the store was written after the newest backup", completed.stderr)
        self.assertEqual(self.version(), SCHEMA_VERSION - 1)

    @hub_only
    def test_a_deleted_or_changed_file_in_the_backup_is_refused(self):
        self.configuration()
        one_migration_back(self.database)
        for change in ("delete", "corrupt"):
            with self.subTest(change=change):
                snapshot = run(home=self.home)
                kept = snapshot.directory / "providers.yaml"
                if change == "delete":
                    kept.unlink()
                else:
                    kept.write_text("providers: {changed: true}\n", encoding="utf-8")
                completed = self.migrate(1)
                self.assertIn("its files differ from its manifest", completed.stderr)
                self.assertEqual(self.version(), SCHEMA_VERSION - 1)

    @hub_only
    def test_a_checkpointed_backup_is_refused(self):
        """A checkpointed run writes its row and copies the log back on close,
        so no fingerprint it could record still describes the store: the
        previous library's backup does not let the next one migrate."""
        snapshot = run(home=self.home)
        self.assertTrue(snapshot.checkpointed)
        self.assertNotIn("store", json.loads((snapshot.directory / BACKUP_MANIFEST).read_text()))
        with mock.patch.object(backup_module, "SCHEMA_VERSION", SCHEMA_VERSION + 1):
            with self.assertRaisesRegex(BackupError, "records no fingerprint of the store"):
                backup_module.migration_backup(self.home)

    def one_back_with_an_item(self):
        """The store one migration back, holding one item `raw_write` changes."""
        connection = self.open()
        create_item(connection, kind="task", title="Before the backup")
        connection.close()
        one_migration_back(self.database)

    def raw_write(self):
        # An UPDATE: every count stays as it was, so only the fingerprint can tell.
        raw = sqlite3.connect(self.database, isolation_level=None)
        try:
            raw.execute("UPDATE item SET title = 'Written in the gap'")
        finally:
            raw.close()

    @hub_only
    def test_a_write_after_the_snapshot_and_before_the_manifest_is_refused(self):
        # The gap round 2 found: the copy's read ends, then the run copies the
        # files beside it, verifies and writes the manifest last.
        self.one_back_with_an_item()
        real = backup_module._record_backup

        def late(*args, **kwargs):
            self.raw_write()
            return real(*args, **kwargs)

        with mock.patch.object(backup_module, "_record_backup", late):
            snapshot = run(home=self.home)
        self.assertFalse(snapshot.checkpointed)
        completed = self.migrate(1)
        self.assertIn("the store was written after the newest backup", completed.stderr)
        self.assertEqual(self.version(), SCHEMA_VERSION - 1)

    @hub_only
    def test_a_write_once_the_snapshot_has_read_is_refused(self):
        self.one_back_with_an_item()
        real = backup_module._counts
        calls: list[threading.Thread] = []

        def counted(connection):
            found = real(connection)
            if not calls:
                # The view is fixed; this write waits for the lock and lands after it.
                calls.append(threading.Thread(target=self.raw_write))
                calls[0].start()
            return found

        with mock.patch.object(backup_module, "_counts", counted):
            try:
                run(home=self.home)
            finally:
                for thread in calls:
                    thread.join()
        completed = self.migrate(1)
        self.assertIn("the store was written after the newest backup", completed.stderr)
        self.assertEqual(self.version(), SCHEMA_VERSION - 1)

    @hub_only
    def test_a_writer_mid_commit_lands_in_the_copy(self):
        # Round 3: a writer puts its frames in the log before it publishes the
        # commit. The read-only run takes the write lock around the
        # fingerprint and the read that fixes its view, so it waits for the
        # commit and copies it. The wait is a minute here: under gate load
        # the writer's 1 s hold outran the 5 s busy timeout (build 312 gate).
        self.one_back_with_an_item()
        holding = threading.Event()

        def writer():
            raw = sqlite3.connect(self.database, isolation_level=None)
            try:
                raw.execute("BEGIN IMMEDIATE")
                raw.execute("UPDATE item SET title = 'Written in the gap'")
                holding.set()
                time.sleep(1)
                raw.execute("COMMIT")
            finally:
                raw.close()

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(holding.wait(10))
        try:
            with mock.patch.object(backup_module, "BUSY_TIMEOUT", 60_000):
                snapshot = run(home=self.home)
        finally:
            thread.join()
        copy = sqlite3.connect(f"file:{snapshot.directory / 'sd.db'}?mode=ro", uri=True)
        try:
            self.assertEqual([("Written in the gap",)], copy.execute("SELECT title FROM item").fetchall())
        finally:
            copy.close()
        self.migrate(0)
        self.assertEqual(self.version(), SCHEMA_VERSION)

    @hub_only
    def test_a_write_between_the_check_and_the_migration_is_refused(self):
        from sd_db.jobs import cli
        self.one_back_with_an_item()
        run(home=self.home)
        real = cli.migration_backup

        def then_write(home):
            found = real(home)
            self.raw_write()
            return found

        with mock.patch.object(cli, "migration_backup", then_write), \
                mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            with self.assertRaisesRegex(BackupError, "the store was written after the newest backup"):
                cli.command_migrate([])
        self.assertEqual(self.version(), SCHEMA_VERSION - 1)

    @hub_only
    def test_a_manifest_without_a_fingerprint_is_refused(self):
        # What a library before this one wrote for a read-only snapshot.
        one_migration_back(self.database)
        snapshot = run(home=self.home)
        manifest = snapshot.directory / BACKUP_MANIFEST
        value = json.loads(manifest.read_text())
        del value["store"]
        os.chmod(manifest, 0o600)
        manifest.write_text(json.dumps(value, sort_keys=True) + "\n")
        completed = self.migrate(1)
        self.assertIn("records no fingerprint of the store", completed.stderr)
        self.assertEqual(self.version(), SCHEMA_VERSION - 1)

    @hub_only
    def test_a_fingerprint_that_cannot_be_read_is_refused(self):
        one_migration_back(self.database)
        snapshot = run(home=self.home)
        self.database.rename(self.database.with_name("moved.db"))
        with self.assertRaisesRegex(BackupError, "fingerprint could not be read"):
            backup_module.unchanged_since(self.home, snapshot.directory)

    @hub_only
    def test_a_library_read_after_the_backup_still_migrates(self):
        # Readers write only the shared-memory file, which the fingerprint leaves out.
        one_migration_back(self.database)
        snapshot = run(home=self.home)
        reader = connect(self.database, write=False)
        try:
            reader.execute("SELECT count(*) FROM item").fetchone()
        finally:
            reader.close()
        completed = self.migrate(0)
        self.assertIn(f"backup {snapshot.directory}", completed.stdout)
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_a_store_with_nothing_pending_needs_no_backup(self):
        self.assertIn("nothing applied", self.migrate(0).stdout)


class JobRun(BackupCase):
    """Runs `sd-db.sh backup` -- the entrypoint the scheduled job runs -- and
    holds no tests of its own, so the classes below do not re-run each other's
    tests.
    """

    def setUp(self):
        super().setUp()
        self.stubs = Stubs(Path(self.tmp.name) / "stubs", names=("local-notify",))
        # Match bin-links' installed command without changing the shared fixture API.
        shutil.copy2(self.stubs.bin / "local-notify", self.stubs.bin / "notify")

    def job(self, expect, *, notify=None, argv=()):
        environment = self.stubs.environment(base=dict(os.environ))
        environment["HOME"] = str(self.home)
        environment["PYTHON"] = sys.executable
        environment.pop("SD_NOTIFY", None)
        if notify is not None:
            environment["SD_NOTIFY"] = str(notify)
        completed = subprocess.run(
            [sys.executable, str(ENTRYPOINT), "backup", *argv], capture_output=True, text=True,
            input="", env=environment, check=False
        )
        self.assertEqual(completed.returncode, expect, completed.stdout + completed.stderr)
        return completed

    def mails(self):
        return self.stubs.calls("notify")

    def rows(self):
        """Every row in the database, read without the library's guards.

        Read raw on purpose: after the failure the database may be one the
        library refuses to open, and the assertion is about what is in the
        file.
        """
        raw = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        try:
            return sum(
                raw.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in ("state", "note", "item")
            )
        finally:
            raw.close()


class TheJobsFailures(JobRun):
    """One email through the cron mail path, and nothing written."""

    def test_a_successful_run_sends_no_mail(self):
        self.job(expect=0)
        self.assertEqual(self.mails(), [])

    def test_default_notification_uses_the_canonical_self_only_mail_flags(self):
        self.database.write_bytes(b"not a database")

        completed = self.job(expect=1)

        mails = self.mails()
        self.assertEqual(len(mails), 1)
        self.assertEqual(mails[0].argv, [
            "-t", "sd-db backup FAILED", "-k", "status", "-F", "-c", "ntfy,email",
            "-b", completed.stderr.rstrip("\n"),
        ])
        self.assertEqual(self.stubs.calls("local-notify"), [])
        self.assertEqual(self.database.read_bytes(), b"not a database")

    def test_an_explicit_notifier_override_takes_precedence_over_notify(self):
        self.database.write_bytes(b"not a database")

        completed = self.job(expect=1, notify=self.stubs.bin / "local-notify")

        self.assertEqual(self.mails(), [])
        mails = self.stubs.calls("local-notify")
        self.assertEqual(len(mails), 1)
        self.assertEqual(mails[0].argv, [
            "-t", "sd-db backup FAILED", "-k", "status", "-F", "-c", "ntfy,email",
            "-b", completed.stderr.rstrip("\n"),
        ])

    def test_an_unopenable_database_sends_one_email_and_writes_nothing(self):
        self.database.write_bytes(b"this is not a database")
        for suffix in ("-wal", "-shm"):
            stale = self.database.with_name(self.database.name + suffix)
            if stale.exists():
                stale.unlink()
        size_before = self.database.stat().st_size

        completed = self.job(expect=1)

        mails = self.mails()
        self.assertEqual(len(mails), 1)
        self.assertIn("-c", mails[0].argv)
        self.assertIn("ntfy,email", mails[0].argv)
        self.assertIn("did not open", " ".join(mails[0].argv))
        self.assertIn("did not open", completed.stderr)
        self.assertEqual(self.database.stat().st_size, size_before)

    def test_a_backups_directory_that_cannot_be_written_sends_one_email(self):
        """Criterion 1's filled-volume path, reached by permissions.

        A literal full volume needs a mount this suite would have to create
        and detach, and a mount that outlives a failed run is worse than the
        gap. What the code distinguishes is "the snapshot was not written",
        which both produce, so the destination is made unwritable instead.
        """
        root = backup_root(self.home)
        root.mkdir(parents=True)
        before = self.rows()
        root.chmod(0o500)
        self.addCleanup(root.chmod, 0o700)

        self.job(expect=1)

        mails = self.mails()
        self.assertEqual(len(mails), 1)
        self.assertIn("ntfy,email", mails[0].argv)
        self.assertIn("backup directory was not created", " ".join(mails[0].argv))
        # The checkpoint row is written before the snapshot and stays; nothing
        # else is. That is the row the next successful run's copy is checked
        # against, so it is not a leak.
        self.assertEqual(self.rows(), before + 1)

    def test_a_failure_to_mail_does_not_hide_the_failure(self):
        self.database.write_bytes(b"not a database")
        shutil.copy2(self.stubs.bin / "notify", self.stubs.bin / "notify.bak")
        (self.stubs.bin / "notify").write_text(
            "#!/bin/sh\nexit 3\n", encoding="utf-8"
        )
        (self.stubs.bin / "notify").chmod(0o755)
        completed = self.job(expect=1)
        self.assertIn("the failure mail did not leave", completed.stderr)


@hub_only
class TheCheckpointOrder(BackupCase):
    def test_a_checkpoint_written_after_the_snapshot_would_prove_nothing(self):
        """The reason step 1 comes before step 2, stated as a test.

        A row written after the snapshot is in the source and not in the
        copy, so `verify` cannot find it -- which is exactly the failure a
        stale copy produces, and the two would be indistinguishable.
        """
        snapshot = run(home=self.home)
        connection = self.open()
        late = "written-after"
        record_state(connection, "checkpoint", key=late)
        with self.assertRaises(BackupError) as raised:
            verify(snapshot.directory / "sd.db", run_id=late, expected=snapshot.counts)
        self.assertIn("some other moment", str(raised.exception))

@hub_only
class TheReferentialCheckOnTheSnapshot(BackupCase):
    """sd:744. `integrity_check` and `foreign_key_check` are not the same
    question, and the backup was only ever asking the first one.

    `verify` runs `integrity_check` on the copy: it asks whether the b-tree
    pages are well formed. A database of nothing but orphans answers `ok`.
    The snapshot copies orphans faithfully and the counts match, because the
    rows are all present -- they just point at nothing. So six orphan rows
    survived three nightly backups, each of which reported success.
    """

    def orphan(self, *, how_many=1):
        """Make orphans the way the real ones were made: raw, pragma off.

        Not through `connect`, which sets `PRAGMA foreign_keys = ON` and would
        refuse the insert. The `sqlite3` CLI leaves it off by default, and a
        hand-run one-liner through it is what produced sd:744.
        """
        raw = sqlite3.connect(self.database)
        try:
            self.assertEqual(raw.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            for index in range(how_many):
                raw.execute(
                    "INSERT INTO note (item, timestamp, kind, body) VALUES (?, 't', 'comment', ?)",
                    (9000 + index, f"a note on an item that is not there {index}"),
                )
            raw.commit()
        finally:
            raw.close()

    def test_a_clean_source_reports_no_violations(self):
        snapshot = run(home=self.home)
        self.assertEqual(snapshot.violations, [])

    def test_an_orphan_in_the_source_reaches_the_snapshot(self):
        self.orphan(how_many=6)
        snapshot = run(home=self.home)
        self.assertEqual(len(snapshot.violations), 6)
        self.assertEqual({row[0] for row in snapshot.violations}, {"note"})

    def test_the_backup_is_still_taken_and_still_verified(self):
        """The deliberate half: report, do not raise.

        A store that has just gone referentially bad is the one whose bytes are
        most worth keeping -- refusing to copy it destroys the only record of
        what it looked like before somebody repaired it. What that copy is NOT
        is a restore point; the test below pins that half.
        """
        self.orphan()
        snapshot = run(home=self.home)
        self.assertTrue((snapshot.directory / "sd.db").is_file())
        self.assertTrue(snapshot.checkpointed)
        self.assertEqual(snapshot.counts["note"], 1)

    def test_a_snapshot_of_a_broken_source_is_not_restorable(self):
        """The contract, tested rather than asserted in a comment.

        `run` keeps the violations and copies the store anyway. `restore` then
        refuses that copy at `_check_restore`'s `foreign_key_check` arm. Both
        are right, and together they mean the snapshot is forensic: the
        repair happens in the live database and the NEXT night's backup is the
        restorable one. Said out loud here because the first version of this
        change called the forensic copy "the recovery path" in a comment, and
        nothing tested the word.
        """
        self.orphan(how_many=6)
        snapshot = run(home=self.home)
        self.assertEqual(len(snapshot.violations), 6)

        other = Path(self.tmp.name) / "restore-home"
        (other / ".local/share/sd").mkdir(parents=True)
        with self.assertRaises(BackupError) as refused:
            restore(snapshot.directory, home=other)
        message = str(refused.exception)
        self.assertIn("6 foreign key violation(s)", message)
        self.assertIn("in note", message)
        self.assertIn("so it is not a restore point", message)
        # Candidate-only wording: this check cannot see what `run` found, so it
        # names the forensic snapshot as the likely cause and not as a fact.
        self.assertIn("a forensic snapshot is the usual cause", message)
        self.assertIn("cannot tell that from later damage", message)
        # Refused before touching the target, not part way through it.
        self.assertFalse((other / ".local/share/sd/sd.db").exists())

    def test_a_clean_snapshot_still_restores(self):
        """The other side of the same contract, so the refusal above is not
        simply `restore` being broken."""
        connection = self.open()
        create_item(connection, kind="work", title="Before")
        snapshot = run(home=self.home)
        other = Path(self.tmp.name) / "clean-restore-home"
        (other / ".local/share/sd").mkdir(parents=True)
        self.assertTrue(restore(snapshot.directory, home=other).exists())

    def test_a_schema_level_referential_fault_raises_instead_of_reporting(self):
        """A foreign-key MISMATCH is a different animal from an orphan row.

        `PRAGMA foreign_key_check` RAISES on a constraint that names a column
        which is not a key, rather than returning a row for it. That is a
        schema fault, it cannot be carried on a summary line, and unconverted
        it would leave a traceback where the module promises a `BackupError`.
        """
        raw = sqlite3.connect(self.database)
        try:
            raw.executescript(
                "PRAGMA writable_schema = ON;"
                " CREATE TABLE mismatched (id INTEGER PRIMARY KEY,"
                " item INTEGER REFERENCES item(no_such_column));"
            )
            raw.commit()
        finally:
            raw.close()
        with self.assertRaises(BackupError) as raised:
            run(home=self.home)
        self.assertIn("the referential check did not run", str(raised.exception))

    def test_the_check_reads_the_copy_so_a_late_writer_cannot_slip_past(self):
        """The pragma runs after the snapshot, on the copy, and that is load-bearing.

        The source's read transaction ends with the copy (`_snapshot`), so a
        check on the source answers for a later image than the one written to
        disk. This test makes the row an orphan before the copy is taken, by
        re-pointing an existing row rather than adding one, so every table
        count stays equal and `verify` cannot notice either. The orphan
        reaches the copy, and the check on the copy reports it.
        """
        connection = self.open()
        item = create_item(connection, kind="work", title="Has a note")
        add_note(connection, item, kind="comment", body="Valid, for now")
        connection.close()
        def orphan_between(root, when):
            directory = _dated_directory(root, when)
            self.repoint()
            return directory

        with mock.patch("sd_db.backup._dated_directory", orphan_between):
            snapshot = run(home=self.home)

        self.assertEqual(len(snapshot.violations), 1)
        self.assertEqual(snapshot.violations[0][0], "note")
        copy = sqlite3.connect(snapshot.directory / "sd.db")
        self.addCleanup(copy.close)
        self.assertEqual(len(copy.execute("PRAGMA foreign_key_check").fetchall()), 1)

    def repoint(self):
        """Orphan a row that is already there, leaving every table count equal.

        One note and not all of them: `create_item` writes its own, and that
        one stays valid, so the count the assertion below is about is real.
        """
        raw = sqlite3.connect(self.database)
        try:
            self.assertEqual(raw.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            raw.execute("UPDATE note SET item = 9000 WHERE body = ?", ("Valid, for now",))
            self.assertEqual(raw.total_changes, 1)
            raw.commit()
        finally:
            raw.close()

    def test_integrity_check_alone_would_have_said_nothing(self):
        """Why the new pragma is not redundant with the one already there."""
        self.orphan(how_many=6)
        snapshot = run(home=self.home)
        copy = sqlite3.connect(snapshot.directory / "sd.db")
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(len(copy.execute("PRAGMA foreign_key_check").fetchall()), 6)


class TheJobsReportOfABrokenSource(JobRun):
    """The finding has to reach a person. Exit 0 was the actual defect: three
    nightly runs said "restored and compared" and nobody had a reason to read
    the log for three days.

    Exit 3 and not 1, because 1 is the documented failed backup -- nothing
    written, no prune -- and `sd-db.sh` mails that as "sd-db backup FAILED".
    Here the snapshot is on disk and the prune ran.

    Three and not 2, because `argparse` exits 2 on a usage error and the shell
    reads a status rather than a traceback.
    """

    def orphan(self):
        raw = sqlite3.connect(self.database)
        try:
            raw.execute("INSERT INTO note (item, timestamp, kind, body) "
                        "VALUES (9000, 't', 'comment', 'an orphan')")
            raw.commit()
        finally:
            raw.close()

    def test_the_job_exits_three_and_names_the_table(self):
        self.orphan()
        completed = self.job(expect=3)
        output = completed.stdout + completed.stderr
        self.assertIn("BROKEN: 1 foreign key violation(s) in the source, in note", output)

    def test_the_normal_summary_survives_beside_the_finding(self):
        """The backup passed. The line still has to say so, or the operator
        reads a failure and starts looking for a snapshot that is on disk."""
        self.orphan()
        completed = self.job(expect=3)
        output = completed.stdout + completed.stderr
        self.assertIn("restored and compared", output)
        self.assertRegex(output, r"row\(s\) across \d+ table\(s\)")

    def test_the_mail_does_not_call_a_passing_backup_a_failure(self):
        """The subject line is the whole point of the separate exit code.

        "sd-db backup FAILED" over a run that wrote its checkpoint, its
        snapshot and its prune report sends the operator looking for a backup
        that is already on disk.
        """
        self.orphan()
        self.job(expect=3)
        sent = self.mails()
        self.assertEqual(len(sent), 1, sent)
        subject = " ".join(sent[0].argv)
        self.assertIn("the source is referentially broken", subject)
        self.assertNotIn("FAILED", subject)

    def test_a_usage_error_is_not_reported_as_a_broken_source(self):
        """`argparse` owns exit 2, and that is the whole reason this one is 3.

        `--keep nope` exits from argument parsing, so `main` never runs and no
        database is opened. `sd-db.sh` sees a status and not a traceback, so
        on 2 the two outcomes are one, and a typo in the options mails a
        referential finding about a store nobody read.
        """
        completed = self.job(expect=1, argv=["--keep", "nope"])
        output = completed.stdout + completed.stderr
        self.assertIn("keep must be 'all' or a positive integer", output)
        sent = self.mails()
        self.assertEqual(len(sent), 1, sent)
        subject = " ".join(sent[0].argv)
        self.assertNotIn("referentially broken", subject)
        self.assertIn("FAILED", subject)

    def test_a_real_failure_still_mails_as_a_failure(self):
        """Exit 3 must not have swallowed the exit-1 branch beside it."""
        self.database.write_text("not a database", encoding="utf-8")
        self.job(expect=1)
        sent = self.mails()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("FAILED", " ".join(sent[0].argv))

    def test_a_clean_source_still_exits_zero_and_says_nothing_about_it(self):
        completed = self.job(expect=0)
        self.assertNotIn("BROKEN", completed.stdout + completed.stderr)
        self.assertEqual(self.mails(), [])



class TheJobOnABrokenSourceWaitingForMigrate(JobRun):
    """Both of the job's returns can carry `BROKEN_SOURCE`, and one pruned nothing.

    The read-only-before-`migrate` path writes no checkpoint row and prints
    "prune skipped" on the very line the finding rides on. So the exit code
    has to mean the finding and nothing more: any claim it makes about the
    checkpoint or the prune is false on half its call sites.
    """

    def test_a_broken_pre_migration_source_exits_three_and_still_skipped_the_prune(self):
        one_migration_back(self.database)
        raw = sqlite3.connect(self.database)
        try:
            raw.execute("INSERT INTO note (item, timestamp, kind, body) "
                        "VALUES (9000, 't', 'comment', 'an orphan')")
            raw.commit()
        finally:
            raw.close()
        completed = self.job(expect=3)
        output = completed.stdout + completed.stderr
        self.assertIn("taken read-only before `migrate`", output)
        self.assertIn("prune skipped", output)
        self.assertIn("BROKEN: 1 foreign key violation(s) in the source, in note", output)
        sent = self.mails()
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("the source is referentially broken", " ".join(sent[0].argv))


@hub_only
class TheJobsPruneFailureBesideABrokenSource(BackupCase):
    """Two things wrong on one night, and the mail has to say both.

    The prune-failure branch returns before the success line. Without the
    finding on that branch too, a night that broke referentially AND failed to
    prune reports only the prune -- losing the one fact this change exists to
    surface. Driven in process rather than through `sd-db.sh`, because the
    failure being provoked is inside `main`'s branching and the shell would
    only be in the way.
    """

    def test_the_prune_failure_message_carries_the_finding(self):
        raw = sqlite3.connect(self.database)
        try:
            raw.execute("INSERT INTO note (item, timestamp, kind, body) "
                        "VALUES (9000, 't', 'comment', 'an orphan')")
            raw.commit()
        finally:
            raw.close()

        def refuse(connection, snapshot):
            raise SdDbError("the retention table said no")

        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                mock.patch.object(job_backup, "prune", refuse):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = job_backup.main([])
        self.assertEqual(code, 1)
        message = errors.getvalue()
        self.assertIn("the prune did not run", message)
        self.assertIn("the retention table said no", message)
        self.assertIn("BROKEN: 1 foreign key violation(s) in the source, in note", message)


class TheHourlyRetention(BackupCase):
    """`keep_days`: a rolling window by age, for the hourly job.

    Directories are named for the run's day with an unpadded `.N`, so a day
    of hourly runs reaches `.23` and lexical order is wrong from `.10` on.
    """

    NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

    def root(self):
        return backup_root(self.home)

    @hub_only
    def test_a_day_wholly_past_the_window_goes_and_the_seventh_day_stays(self):
        # 7 days 12.5 hours old, but its day (09-16) ends 7.5 days ago: gone.
        gone = run(home=self.home, when=datetime(2026, 9, 16, 23, 30, tzinfo=UTC))
        # 7 days 11.5 hours old, dated exactly 7 days back (09-17): kept,
        # because part of its day is still inside the window.
        kept = run(home=self.home, when=datetime(2026, 9, 17, 0, 30, tzinfo=UTC))
        young = run(home=self.home, when=datetime(2026, 9, 18, 12, 0, tzinfo=UTC))

        latest = run(home=self.home, keep_days=7, when=self.NOW)

        self.assertEqual(latest.removed, [gone.directory])
        self.assertFalse(gone.directory.exists())
        for survivor in (kept, young, latest):
            self.assertTrue(survivor.directory.exists(), survivor.directory)

    @hub_only
    def test_nothing_younger_than_the_window_is_removed(self):
        run(home=self.home, when=self.NOW - timedelta(days=7))
        self.assertEqual(prune_older(self.root(), 7, now=self.NOW), [])

    @hub_only
    def test_a_whole_old_day_goes_in_run_order_across_nine_and_ten(self):
        old = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
        taken = [run(home=self.home, when=old) for _ in range(11)]
        names = [snapshot.directory.name for snapshot in taken]
        self.assertEqual(names[9:], ["2026-09-10.9", "2026-09-10.10"])
        young = run(home=self.home, when=self.NOW)

        removed = prune_older(self.root(), 7, now=self.NOW)

        self.assertEqual([path.name for path in removed], names)
        self.assertTrue(young.directory.exists())

    @hub_only
    def test_only_owned_backups_are_deleted(self):
        root = self.root()
        run(home=self.home, when=self.NOW)
        legacy = root / "2026-01-01"
        legacy.mkdir()
        (legacy / "sd.db").write_bytes(b"a hand-made copy with no manifest")
        unrelated = root / "notes"
        unrelated.mkdir()
        sibling = root.parent / "replaced"
        sibling.mkdir()
        (sibling / "2026-01-02").mkdir()

        self.assertEqual(prune_older(root, 7, now=self.NOW), [])
        for path in (legacy, unrelated, sibling / "2026-01-02"):
            self.assertTrue(path.exists(), path)

    def test_a_count_and_an_age_together_are_refused(self):
        with self.assertRaisesRegex(BackupError, "not both"):
            run(home=self.home, keep=3, keep_days=7)
        with self.assertRaisesRegex(BackupError, "positive number of days"):
            run(home=self.home, keep_days=0)
        self.assertFalse(self.root().exists())


@hub_only
class TheCountAcrossNineAndTen(BackupCase):
    def test_keep_counts_runs_by_number_not_by_name(self):
        when = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
        names = [run(home=self.home, when=when).directory.name for _ in range(11)]
        # Lexical order would keep .7, .8 and .9 and delete .10, the newest.
        self.assertEqual(sorted(names)[-3:], ["2026-09-06.7", "2026-09-06.8", "2026-09-06.9"])

        prune(backup_root(self.home), 3)

        left = sorted(path.name for path in backup_root(self.home).iterdir())
        self.assertEqual(left, ["2026-09-06.10", "2026-09-06.8", "2026-09-06.9"])


@hub_only
class TheMountRequirement(BackupCase):
    """`mount`: a detached drive's mount point is a plain local directory."""

    def setUp(self):
        super().setUp()
        self.volume = Path(self.tmp.name).resolve() / "volume"
        self.volume.mkdir()
        self.destination = self.volume / "Backup Local" / "sd-backups"

    def state_rows(self):
        connection = self.open(write=False)
        return connection.execute("SELECT count(*) FROM state").fetchone()[0]

    def mounted(self):
        real = os.path.ismount
        volume = self.volume
        return mock.patch.object(backup_module.os.path, "ismount",
                                 side_effect=lambda path: Path(path).resolve() == volume or real(path))

    def test_an_unmounted_volume_is_refused_before_anything_is_written(self):
        before = self.state_rows()

        with self.assertRaisesRegex(BackupError, "is not a mounted volume"):
            run(home=self.home, destination=self.destination, mount=self.volume, keep_days=7)

        self.assertFalse((self.volume / "Backup Local").exists())
        self.assertEqual(self.state_rows(), before)

    def test_a_mounted_volume_takes_the_snapshot(self):
        with self.mounted():
            snapshot = run(home=self.home, destination=self.destination, mount=self.volume, keep_days=7)
        self.assertEqual(snapshot.directory.parent, self.destination)
        self.assertTrue((snapshot.directory / "sd.db").is_file())

    def test_a_destination_linked_off_the_volume_is_refused(self):
        elsewhere = Path(self.tmp.name).resolve() / "elsewhere"
        elsewhere.mkdir()
        (self.volume / "Backup Local").symlink_to(elsewhere, target_is_directory=True)

        with self.mounted(), self.assertRaisesRegex(BackupError, "not by the mounted volume"):
            run(home=self.home, destination=self.destination, mount=self.volume)
        self.assertEqual(list(elsewhere.iterdir()), [])


class TheDefaultRoot(BackupCase):
    """No destination named: the USB disk's `Backup` folder, and only while it is mounted.

    The package sets `SD_DB_BACKUP_ROOT` for every other test (`tests/__init__.py`),
    so each injected home keeps its own root. These tests remove it to reach the
    default. Past the first test, the default is moved into the test's own
    directory, so a run whose mount check was lost writes there and not to a
    real `/Volumes/local`; the mount question is answered with a double.
    """

    def setUp(self):
        super().setUp()
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("SD_DB_BACKUP_ROOT", None)
        self.volume = Path(self.tmp.name).resolve() / "volume"
        self.volume.mkdir()
        self.root = self.volume / "Backup" / "sd-backups"

    def default_moved(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(backup_module, "DEFAULT_ROOT", self.root, create=True))
        stack.enter_context(mock.patch.object(backup_module, "DEFAULT_MOUNT", self.volume, create=True))
        stack.enter_context(mock.patch.object(backup_module.os.path, "ismount", return_value=False))
        return stack

    def checkpoints(self):
        connection = self.open(write=False)
        return connection.execute("SELECT count(*) FROM state WHERE kind = 'checkpoint'").fetchone()[0]

    def test_the_default_root_is_the_usb_backup_folder(self):
        self.assertEqual(backup_root(self.home), Path("/Volumes/local/Backup/sd-backups"))
        self.assertEqual(backup_module.DEFAULT_MOUNT, Path("/Volumes/local"))

    @hub_only
    def test_a_detached_disk_refuses_the_default_and_writes_nothing(self):
        with self.default_moved(), self.assertRaises(BackupError) as caught:
            run(home=self.home)
        message = str(caught.exception)
        for part in (f"{self.volume} is not a mounted volume", str(self.root), "--destination"):
            self.assertIn(part, message)
        self.assertEqual(self.checkpoints(), 0)
        self.assertFalse((self.volume / "Backup").exists())
        self.assertFalse((self.home / "Documents").exists())

    @hub_only
    def test_the_job_reports_the_detached_disk_and_exits_1(self):
        stderr = io.StringIO()
        with self.default_moved(), mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(job_backup.main([]), 1)
        self.assertIn("mount the disk or pass --destination", stderr.getvalue())
        self.assertFalse((self.volume / "Backup").exists())

    @hub_only
    def test_an_explicit_destination_is_not_asked_about_the_disk(self):
        destination = Path(self.tmp.name) / "elsewhere"
        with self.default_moved():
            snapshot = run(home=self.home, destination=destination)
        self.assertEqual(snapshot.directory.parent, destination)

    @hub_only
    def test_the_root_variable_names_the_root_and_is_not_asked_about_the_disk(self):
        named = Path(self.tmp.name) / "named"
        os.environ["SD_DB_BACKUP_ROOT"] = str(named)
        with self.default_moved():
            snapshot = run(home=self.home)
        self.assertEqual(snapshot.directory.parent, named)
        os.environ["SD_DB_BACKUP_ROOT"] = "Documents/sd-backups"
        self.assertEqual(backup_root(self.home), self.home / "Documents/sd-backups")


class TheHourlyJobsFlags(JobRun):
    def reports(self):
        raw = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        try:
            return raw.execute("SELECT count(*) FROM item WHERE kind = 'report'").fetchone()[0]
        finally:
            raw.close()

    def test_an_unmounted_volume_fails_loudly_with_one_mail(self):
        volume = Path(self.tmp.name) / "volume"
        volume.mkdir()
        completed = self.job(expect=1, argv=[
            "--destination", str(volume / "Backup Local/sd-backups"),
            "--require-mount", str(volume), "--keep-days", "7", "--no-row-prune"])
        self.assertIn("is not a mounted volume", completed.stderr)
        mails = self.mails()
        self.assertEqual(len(mails), 1)
        self.assertIn("is not a mounted volume", " ".join(mails[0].argv))
        self.assertFalse((volume / "Backup Local").exists())

    def test_no_row_prune_files_no_report(self):
        before = self.reports()
        completed = self.job(expect=0, argv=["--keep-days", "7", "--no-row-prune"])
        self.assertIn("row prune skipped (--no-row-prune)", completed.stdout)
        self.assertEqual(self.reports(), before)
        self.assertEqual(self.mails(), [])

    def test_a_count_and_an_age_are_a_usage_error(self):
        completed = self.job(expect=1, argv=["--keep", "3", "--keep-days", "7"])
        self.assertIn("not allowed with argument", completed.stderr)
        completed = self.job(expect=1, argv=["--keep-days", "0"])
        self.assertIn("keep-days must be a positive integer", completed.stderr)
        completed = self.job(expect=1, argv=["--keep", "0"])
        self.assertIn("keep must be 'all' or a positive integer", completed.stderr)
        self.assertFalse(backup_root(self.home).exists())


class TheRestoreCheckOnABareFile(unittest.TestCase):
    """`check_restorable`: restore's own question, asked of one file.

    The off-machine verifier holds a database pulled back off the share and no
    snapshot directory around it. It asks here, because nothing outside this
    library opens the database and a verifier keeping a weaker contract of its
    own is what an unverified backup is made of.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "sd.db"

    def test_a_good_store_is_accepted_with_its_counts(self):
        counts = make_store(self.database)

        check = check_restorable(self.database)

        self.assertTrue(check.accepted, check.refusal)
        self.assertIsNone(check.refusal)
        self.assertEqual(check.version, SCHEMA_VERSION)
        for table, rows in counts.items():
            self.assertEqual(check.counts[table], rows)

    def test_a_store_from_an_older_schema_is_still_a_restore_point(self):
        """Most of what a share holds was written before the last migration."""
        make_store(self.database, version=SCHEMA_VERSION - 1)

        check = check_restorable(self.database)

        self.assertTrue(check.accepted, check.refusal)
        self.assertEqual(check.version, SCHEMA_VERSION - 1)

    def test_an_orphan_row_is_refused_in_the_validator_s_words(self):
        make_store(self.database)
        break_foreign_keys(self.database)

        check = check_restorable(self.database)

        self.assertFalse(check.accepted)
        self.assertIn("foreign key violation", check.refusal)
        self.assertEqual(check.counts, {})

    def test_a_table_the_schema_does_not_have_is_refused(self):
        """Counts, hashes and both pragmas pass on this store. Only the
        validator sees it, which is why the verifier has to come here."""
        make_store(self.database)
        add_unknown_table(self.database)

        check = check_restorable(self.database)

        self.assertFalse(check.accepted)
        self.assertIn("incompatible table set", check.refusal)

    def test_a_store_missing_an_index_or_a_trigger_is_refused(self):
        # `record_check` upserts through `runner_check`, and the publication
        # trigger is the payload's immutability; tables and columns alone
        # pass a store without either.
        for statement, name in (("DROP INDEX runner_check", "runner_check"),
                                ("DROP TRIGGER publication_payload_immutable",
                                 "publication_payload_immutable")):
            with self.subTest(name=name):
                database = Path(self.tmp.name) / f"{name}.db"
                make_store(database)
                raw = sqlite3.connect(database)
                try:
                    raw.execute(statement)
                    raw.commit()
                finally:
                    raw.close()

                check = check_restorable(database)

                self.assertFalse(check.accepted)
                self.assertIn(name, check.refusal or "")

    def test_a_file_that_is_not_a_database_is_a_refusal_not_a_traceback(self):
        self.database.write_bytes(b"not a database at all")

        check = check_restorable(self.database)

        self.assertFalse(check.accepted)
        self.assertIsNone(check.version)

    def test_a_missing_file_is_a_refusal_too(self):
        check = check_restorable(self.database)

        self.assertFalse(check.accepted)
        self.assertIn("does not open", check.refusal)


if __name__ == "__main__":
    unittest.main()
