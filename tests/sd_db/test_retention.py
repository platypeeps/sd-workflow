"""The nightly row prune: criterion 22's retention clauses.

Seeded rows past every retention age and a backup that passed; the counts
removed match the seeded excess and a `report` row carries them; with the
backup failed the prune refuses and removes nothing; an `exec` note past
ninety days survives marked `output expired` with its file gone and the
palette's read says so; a fourteen-month `cost` row on a `blocked`
assignment with a spent budget survives with the ledger intact; a clean run
report that sat in `planning` for a week is settled `done` by retention, and
an attention report or one with an open followup is not, however old.

Ages are seeded by backdating the rows and handing the prune its `now`;
nothing sleeps.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from sd_db import (
    reads,
    reporting,
    retention,
    runner,
    runner_exec,
    runner_journal,
    ship,
    workflow,
)
from sd_db.backup import Snapshot, passed, run
from sd_db.database import connect, transaction
from sd_db.migrate import initialise
from sd_db.retention import (
    CLEAN_REPORT_AGE,
    EXEC_OUTPUT_AGE,
    JOB,
    OUTPUT_EXPIRED,
    RETENTION,
    RetentionRefused,
    prune,
    settle_clean_reports,
)
from sd_db.testing import Stubs
from sd_db.testing.wire import hub_only
from sd_db.writes import (
    add_note,
    create_assignment,
    create_item,
    record_cost,
    record_state,
    resolve_note,
    upsert_repo,
)

from . import support

PACKAGE_ROOT = support.LIB
ENTRYPOINT = support.SD_DB

NOW = datetime(2026, 9, 12, 2, 10, tzinfo=UTC)


def ago(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat(timespec="seconds")


class PruneCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.state = self.home / ".local/share/sd"
        self.state.mkdir(parents=True)
        initialise(home=self.home)
        self.db = connect(home=self.home)
        self.addCleanup(self.db.close)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        upsert_repo(self.db, str(self.repo), remote="https://example.invalid/repo.git")
        self.item = create_item(self.db, kind="task", title="Fixture", repo=str(self.repo), branch="item/one", status="ready")

    # -- seeds ---------------------------------------------------------------

    def heartbeats(self, key, count):
        """`count` ticks under one key, oldest first; the newest carries `count - 1`."""
        for tick in range(count):
            record_state(self.db, "heartbeat", key=key, body={"tick": tick}, timestamp=ago(hours=count - tick))

    def exec_note(self, *, days):
        """A palette command that really ran, then aged `days`: note, log and receipt."""
        program = self.root / f"command-{days}"
        program.write_text("#!/bin/sh\necho fixture output\nexit 3\n")
        program.chmod(0o755)
        catalog = self.root / f"commands-{days}.yaml"
        entry = {"argv": [str(program), "{item}"], "screens": ["item"], "mutates": False,
                 "scope": "worktree", "placeholders": {"item": "item"}}
        catalog.write_text("version: 1\ncommands:\n  inspect: " + json.dumps(entry) + "\n")
        prepared = runner_exec.prepare(
            self.db, self.item, "inspect", {"item": self.item},
            expected_revision=workflow.item_state(self.db, self.item)["revision"],
            expected_catalog=runner_exec.catalog(path=catalog)["sha256"], path=catalog, who="operator")["execution"]
        result = runner_exec.execute_immediate(self.db, prepared["note"], home=self.home)
        self.assertEqual(result["exit_code"], 3)
        self.db.execute("UPDATE note SET timestamp=?, started=?, ended=? WHERE id=?",
                        (ago(days=days), ago(days=days), ago(days=days), prepared["note"]))
        self.db.commit()
        return prepared["note"], Path(prepared["output_path"])

    def old_cost(self):
        """A `blocked` assignment whose one-dollar budget one fourteen-month-old call spent."""
        assignment = create_assignment(self.db, role="author", status="blocked", item=self.item, budget_usd=1.0)
        row = record_cost(self.db, source="run", assignment=assignment, usd=1.0, provider=None, call_id="call:old")
        self.db.execute("UPDATE cost SET timestamp=? WHERE id=?", (ago(days=14 * 30), row))
        self.db.commit()
        return assignment, row

    def kept_worktree(self):
        """A released run whose clone was kept, fifteen days old."""
        other = create_item(self.db, kind="task", title="Kept", repo=str(self.repo), branch="item/kept", status="ready")
        ident = runner.enqueue(self.db, [other], who="operator")[0]["id"]
        claimed = runner.claim(self.db, ident, owner="fixture", work_root=self.root / "work", retention_root=self.root / "retained")["run"]
        retained = Path(claimed["retained_path"])
        retained.mkdir(parents=True, exist_ok=True)
        (retained / "WORK").write_text("uncommitted\n")
        self.db.execute("UPDATE runner_run SET created_at=?, updated_at=?, released_at=? WHERE id=?",
                        (ago(days=15), ago(days=15), ago(days=15), claimed["id"]))
        self.db.commit()
        # The backup refuses runner rows without their durable journal.
        record = dict(self.db.execute("SELECT * FROM runner_run WHERE id=?", (claimed["id"],)).fetchone())
        runner_journal.persist(self.state / "sd.db", record)
        return claimed["id"], retained

    def report(self, *, days, attention=False, job="fixture-job"):
        """A cron report `ingest` opened as `planning`, then aged `days`."""
        ended = ago(days=days)
        state = reporting.ingest(self.db, job=job, run_id=f"{job}-{days}-{attention}", started=ended, ended=ended,
                                 exit_code=1 if attention else 0, text="fixture run\n",
                                 source_path=str(self.root / "log"), attention=attention)
        self.db.execute("UPDATE item SET created_at=? WHERE id=?", (ended, state["item"]["id"]))
        self.db.commit()
        return state["item"]["id"]

    def status_of(self, item):
        return self.db.execute("SELECT status FROM item WHERE id=?", (item,)).fetchone()[0]

    def backup(self):
        return run(home=self.home, when=NOW)

    def heartbeat_rows(self):
        return self.db.execute("SELECT key, body FROM state WHERE kind='heartbeat' ORDER BY key, id").fetchall()


@hub_only
class TheSeededExcess(PruneCase):
    def test_the_counts_removed_match_the_excess_and_the_report_row_carries_them(self):
        self.heartbeats("tracker-sync:github", 6)
        self.heartbeats("contribution-sync:github", 2)
        runner.heartbeat(self.db, {"ok": True})
        old_note, old_log = self.exec_note(days=91)
        young_note, young_log = self.exec_note(days=10)
        assignment, cost = self.old_cost()
        run_id, retained = self.kept_worktree()
        snapshot = self.backup()

        pruned = prune(self.db, snapshot, now=NOW)

        self.assertEqual(pruned.counts, {"exec_outputs": 1, "heartbeats": 6, "checkpoints": 0, "clean_reports": 0, "parked": 0})
        self.assertFalse(old_log.exists())
        self.assertFalse(old_log.with_suffix(".receipt.json").exists())
        self.assertTrue(young_log.exists())
        rows = self.heartbeat_rows()
        self.assertEqual([(row["key"], json.loads(row["body"])) for row in rows],
                         [("contribution-sync:github", {"tick": 1}), ("runner", {"ok": True}),
                          ("tracker-sync:github", {"tick": 5})])
        # Never pruned: the cost row and both exec notes.
        self.assertEqual(self.db.execute("SELECT count(*) FROM cost").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM note WHERE kind='exec'").fetchone()[0], 2)
        # The kept worktree is the runner's business, and it is still there.
        self.assertTrue((retained / "WORK").is_file())
        self.assertEqual(self.db.execute("SELECT retained_path FROM runner_run WHERE id=?", (run_id,)).fetchone()[0], str(retained))

        report = self.db.execute("SELECT * FROM item WHERE id=?", (pruned.report,)).fetchone()
        self.assertEqual(report["kind"], "report")
        self.assertEqual(report["external_id"], f"{JOB}:{snapshot.run_id}")
        provenance = json.loads(report["fields"])["report"]
        self.assertEqual(provenance["removed"], {"exec_outputs": 1, "heartbeats": 6, "checkpoints": 0, "clean_reports": 0, "parked": 0})
        self.assertEqual(provenance["source_path"], str(snapshot.directory))
        self.assertIn(report["id"], [row["id"] for row in reporting.reports(self.db)])
        self.assertIn("1 exec output(s) expired, 6 stale heartbeat row(s) removed, 0 superseded checkpoint row(s) removed, 0 clean report(s) settled",
                      json.loads(report["body"])["text"])

    def test_a_second_night_finds_nothing_more_to_take_and_the_next_backup_still_passes(self):
        self.heartbeats("tracker-sync:github", 3)
        self.exec_note(days=100)
        prune(self.db, self.backup(), now=NOW)
        # The expired note has no file now; the next backup's evidence check must know why.
        later = run(home=self.home, when=NOW + timedelta(days=1))
        self.assertEqual(prune(self.db, later, now=NOW + timedelta(days=1)).counts,
                         {"exec_outputs": 0, "heartbeats": 0, "checkpoints": 0, "clean_reports": 0, "parked": 0})


@hub_only
class TheRefusal(PruneCase):
    def seed(self):
        self.heartbeats("tracker-sync:github", 4)
        note, log = self.exec_note(days=120)
        return note, log

    def untouched(self, log):
        self.assertTrue(log.is_file())
        self.assertEqual(len(self.heartbeat_rows()), 4)
        self.assertEqual(self.db.execute("SELECT count(*) FROM item WHERE kind='report'").fetchone()[0], 0)

    def test_no_backup_means_no_prune(self):
        _, log = self.seed()
        for backup in (None, "tonight", Snapshot(directory=self.root / "absent", run_id="0" * 32)):
            with self.subTest(backup=backup), self.assertRaisesRegex(RetentionRefused, "did not pass"):
                prune(self.db, backup, now=NOW)
        self.untouched(log)

    def test_a_backup_that_changed_after_it_passed_is_not_a_passed_backup(self):
        _, log = self.seed()
        snapshot = self.backup()
        self.assertTrue(passed(self.db, snapshot))
        (snapshot.directory / "sd.db").write_bytes(b"truncated")
        self.assertFalse(passed(self.db, snapshot))
        with self.assertRaisesRegex(RetentionRefused, "did not pass"):
            prune(self.db, snapshot, now=NOW)
        self.untouched(log)

    def test_another_database_s_backup_does_not_admit_this_one(self):
        _, log = self.seed()
        elsewhere = self.root / "elsewhere"
        (elsewhere / ".local/share/sd").mkdir(parents=True)
        initialise(home=elsewhere)
        foreign = run(home=elsewhere, when=NOW)
        self.assertFalse(passed(self.db, foreign))
        with self.assertRaisesRegex(RetentionRefused, "did not pass"):
            prune(self.db, foreign, now=NOW)
        self.untouched(log)

    def test_a_second_prune_against_one_backup_is_refused_before_anything_is_removed(self):
        """The report is the prune's last write, and `ingest` refuses a
        second one for the same run id with different evidence. So a second
        prune against the same snapshot expired outputs, compacted heartbeats
        and settled reports in three committed transactions, and only then
        failed -- every one of them irreversible, none of them reported."""
        snapshot = self.backup()
        prune(self.db, snapshot, now=NOW)
        # New stale rows since, so the second run's counts would differ.
        self.heartbeats("tracker-sync:github", 4)
        _, log = self.exec_note(days=120)
        with self.assertRaisesRegex(RetentionRefused, "already has its prune report"):
            prune(self.db, snapshot, now=NOW)
        self.assertTrue(log.is_file())
        self.assertEqual(len([row for row in self.heartbeat_rows() if row["key"] == "tracker-sync:github"]), 4)
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM item WHERE source='cron-report' AND external_id=?",
            (f"{JOB}:{snapshot.run_id}",)).fetchone()[0], 1)

    def test_an_executions_directory_replaced_by_a_symlink_is_refused(self):
        """`_output_files` compared the note's parent with `<db>/executions`
        as text, and `_unlink` rejects a symlinked file but never looked at
        the directory above it. A replaced `executions` directory therefore
        passed, and the prune deleted through the link into wherever it led.
        """
        _, log = self.seed()
        # The backup first: its own evidence capture refuses a linked
        # directory, and what is under test is the prune's check, not that one.
        snapshot = self.backup()
        executions = log.parent
        self.assertEqual(executions, self.state / "executions")
        elsewhere = self.root / "somewhere-else"
        executions.rename(elsewhere)
        executions.symlink_to(elsewhere, target_is_directory=True)
        target = elsewhere / log.name
        self.assertTrue(target.is_file())
        with self.assertRaisesRegex(RetentionRefused, "outside the executions directory"):
            prune(self.db, snapshot, now=NOW)
        self.assertTrue(target.is_file())

    def test_the_job_with_a_failed_backup_exits_one_and_prunes_nothing(self):
        _, log = self.seed()
        stubs = Stubs(self.root / "stubs", names=("local-notify",))
        shutil.copy2(stubs.bin / "local-notify", stubs.bin / "notify")
        backups = self.home / "Documents/sd-backups"
        backups.mkdir(parents=True)
        backups.chmod(0o500)
        self.addCleanup(backups.chmod, 0o700)
        environment = stubs.environment(base=dict(os.environ))
        environment.update({"HOME": str(self.home), "PYTHON": sys.executable})
        environment.pop("SD_NOTIFY", None)
        completed = subprocess.run([sys.executable, str(ENTRYPOINT), "backup"], capture_output=True, text=True,
                                   input="", env=environment, check=False)
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertNotIn("pruned:", completed.stdout + completed.stderr)
        self.untouched(log)


class TheJob(PruneCase):
    def test_the_backup_verb_prunes_after_it_passed_and_prints_the_counts(self):
        self.heartbeats("tracker-sync:github", 3)
        stubs = Stubs(self.root / "stubs", names=("local-notify",))
        shutil.copy2(stubs.bin / "local-notify", stubs.bin / "notify")
        environment = stubs.environment(base=dict(os.environ))
        environment.update({"HOME": str(self.home), "PYTHON": sys.executable})
        completed = subprocess.run([sys.executable, str(ENTRYPOINT), "backup"], capture_output=True, text=True,
                                   input="", env=environment, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("pruned: 0 exec output(s) expired, 2 stale heartbeat row(s) removed, "
                      "0 superseded checkpoint row(s) removed, 0 clean report(s) settled, 0 untouched item(s) parked (report #", completed.stdout)
        self.assertEqual(stubs.calls("notify"), [])
        self.assertEqual(len(self.heartbeat_rows()), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM item WHERE kind='report' AND source='cron-report'").fetchone()[0], 1)


class TheExecNote(PruneCase):
    @hub_only
    def test_it_survives_marked_output_expired_and_the_palette_says_so(self):
        note, log = self.exec_note(days=91)
        before = dict(self.db.execute("SELECT * FROM note WHERE id=?", (note,)).fetchone())
        descriptor = json.loads(before["body"])
        self.assertTrue(log.is_file())

        pruned = prune(self.db, self.backup(), now=NOW)

        self.assertEqual(pruned.exec_outputs, 1)
        after = dict(self.db.execute("SELECT * FROM note WHERE id=?", (note,)).fetchone())
        self.assertFalse(log.exists())
        for column in ("id", "item", "kind", "started", "ended", "exit_code", "output_path", "session"):
            self.assertEqual(after[column], before[column], column)
        self.assertEqual(after["exit_code"], 3)
        marked = json.loads(after["body"])
        self.assertEqual(marked.pop(OUTPUT_EXPIRED), NOW.isoformat(timespec="seconds"))
        self.assertEqual(marked, descriptor, "entry, arguments and everything else are as recorded")
        self.assertEqual(marked["command"], "inspect")
        self.assertEqual(marked["argv"][1], str(self.item))

        # The read the dashboard's /api/executions route and the palette's history share.
        read = runner_exec.read_execution(self.db, note)
        self.assertEqual(read["state"], "output expired")
        self.assertEqual(read["output_expired"], NOW.isoformat(timespec="seconds"))
        self.assertEqual(read["output"], "")
        self.assertEqual(read["exit_code"], 3)
        # The command log on the Operations screen.
        listed = [row for row in runner_exec.executions(self.db) if row["id"] == note]
        self.assertEqual(listed[0]["output_expired"], NOW.isoformat(timespec="seconds"))
        self.assertEqual(listed[0]["command"], "inspect")
        # Never run again: the marked descriptor is not a valid one to execute.
        with self.assertRaises(workflow.WorkflowError):
            runner_exec.verify_descriptor(marked | {OUTPUT_EXPIRED: read["output_expired"]})
        with self.assertRaisesRegex(workflow.WorkflowError, "already ended"):
            runner_exec.execute_immediate(self.db, note, home=self.home)

    @hub_only
    def test_a_note_one_day_inside_ninety_keeps_its_output(self):
        note, log = self.exec_note(days=89)
        self.assertEqual(prune(self.db, self.backup(), now=NOW).exec_outputs, 0)
        self.assertTrue(log.is_file())
        self.assertEqual(runner_exec.read_execution(self.db, note)["output"], "fixture output\n")
        self.assertIsNone(runner_exec.read_execution(self.db, note)["output_expired"])

    @hub_only
    def test_an_unfinished_note_is_never_aged(self):
        """`reconcile` needs the file to decide what happened; retention is not the judge."""
        note, log = self.exec_note(days=200)
        self.db.execute("UPDATE note SET ended=NULL, exit_code=NULL WHERE id=?", (note,))
        self.db.commit()
        self.assertEqual(prune(self.db, self.backup(), now=NOW).exec_outputs, 0)
        self.assertTrue(log.is_file())

    def test_the_age_is_the_table_s(self):
        self.assertEqual(EXEC_OUTPUT_AGE, timedelta(days=90))


class TheCleanReport(PruneCase):
    """`ingest` opens every report as `planning`; nothing but a person moved a clean one."""

    def test_a_clean_report_older_than_seven_days_is_settled_done_by_retention(self):
        item = self.report(days=8)
        self.assertEqual(self.status_of(item), "planning")
        self.assertEqual(settle_clean_reports(self.db, now=NOW), 1)
        self.assertEqual(self.status_of(item), "done")
        notes = [note["body"] for note in workflow.item_state(self.db, item)["notes"] if note["kind"] == "status_change"]
        self.assertEqual(notes[-1], f"planning -> done by {RETENTION}")
        # Settled, not removed: the row and its evidence are still there.
        self.assertIsNotNone(self.db.execute("SELECT id FROM item WHERE id=? AND kind='report'", (item,)).fetchone())
        # A second pass finds nothing to settle.
        self.assertEqual(settle_clean_reports(self.db, now=NOW), 0)

    def test_a_clean_report_six_days_old_waits(self):
        item = self.report(days=6)
        self.assertEqual(settle_clean_reports(self.db, now=NOW), 0)
        self.assertEqual(self.status_of(item), "planning")

    def test_an_attention_report_is_never_settled_however_old(self):
        item = self.report(days=40, attention=True)
        self.assertEqual(settle_clean_reports(self.db, now=NOW), 0)
        self.assertEqual(self.status_of(item), "planning")

    def test_a_clean_report_with_an_open_followup_waits_for_the_person(self):
        item = self.report(days=30)
        followup = add_note(self.db, item, "followup", "look at this", session="user")
        self.assertEqual(settle_clean_reports(self.db, now=NOW), 0)
        self.assertEqual(self.status_of(item), "planning")
        # Resolved, the followup no longer holds it.
        resolve_note(self.db, followup)
        self.assertEqual(settle_clean_reports(self.db, now=NOW), 1)
        self.assertEqual(self.status_of(item), "done")

    def test_a_report_that_never_said_whether_it_needed_attention_is_left_alone(self):
        """Absent is not false: a row with no `attention` key never said it was clean."""
        unknown = self.report(days=30)
        self.db.execute("UPDATE item SET fields=json_remove(fields, '$.attention') WHERE id=?", (unknown,))
        self.db.commit()
        self.assertIsNone(self.db.execute("SELECT json_extract(fields, '$.attention') FROM item WHERE id=?",
                                          (unknown,)).fetchone()[0])
        # `IS NOT 1` would read this NULL as clean and settle it; `= 0` does not.
        self.assertEqual(settle_clean_reports(self.db, now=NOW), 0)
        self.assertEqual(self.status_of(unknown), "planning")
        # The same row with the key back, false, is what the sweep settles.
        self.db.execute("UPDATE item SET fields=json_set(fields, '$.attention', json('false')) WHERE id=?", (unknown,))
        self.db.commit()
        self.assertEqual(settle_clean_reports(self.db, now=NOW), 1)
        self.assertEqual(self.status_of(unknown), "done")

    def test_a_followup_landing_as_the_sweep_takes_its_lock_is_seen(self):
        """The candidate read runs under the write lock, not before it.

        Another connection files a followup on the report the instant the
        sweep opens its transaction. A read taken before the transaction
        would already hold the row and settle over the followup; the read
        inside it sees the followup and leaves the row alone.
        """
        item = self.report(days=30)
        other = connect(home=self.home)
        self.addCleanup(other.close)
        real = retention.transaction

        @contextmanager
        def followup_lands_first(connection):
            if not connection.in_transaction:
                add_note(other, item, "followup", "one more thing", session="user")
            with real(connection) as inner:
                yield inner

        with mock.patch.object(retention, "transaction", followup_lands_first):
            self.assertEqual(settle_clean_reports(self.db, now=NOW), 0)
        self.assertEqual(self.status_of(item), "planning")

    @hub_only
    def test_the_prune_settles_them_and_its_report_row_counts_them(self):
        old = self.report(days=9, job="job-a")
        young = self.report(days=2, job="job-b")
        flagged = self.report(days=20, attention=True, job="job-c")
        snapshot = self.backup()

        pruned = prune(self.db, snapshot, now=NOW)

        self.assertEqual(pruned.clean_reports, 1)
        self.assertEqual(pruned.counts, {"exec_outputs": 0, "heartbeats": 0, "checkpoints": 0, "clean_reports": 1, "parked": 0})
        self.assertEqual([self.status_of(item) for item in (old, young, flagged)], ["done", "planning", "planning"])
        report = self.db.execute("SELECT * FROM item WHERE id=?", (pruned.report,)).fetchone()
        self.assertEqual(json.loads(report["fields"])["report"]["removed"]["clean_reports"], 1)
        self.assertIn("1 clean report(s) settled", json.loads(report["body"])["text"])
        # The prune's own report is clean and opened tonight, so it waits its week too.
        self.assertEqual(report["status"], "planning")
        self.assertEqual(str(pruned), "0 exec output(s) expired, 0 stale heartbeat row(s) removed, 0 superseded checkpoint row(s) removed, 1 clean report(s) settled, "
                                      "0 untouched item(s) parked")

    def test_the_age_is_the_table_s(self):
        self.assertEqual(CLEAN_REPORT_AGE, timedelta(days=7))


class AnUnreadableReport(PruneCase):
    """A `planning` report whose `fields` is not JSON (sd:755).

    Before the guard, `json_extract` on it failed the settle's whole read, so
    one bad row stopped the nightly prune. Now it is not settled, and the
    prune report names it with attention, in a bounded list.
    """

    def unreadable(self, count=1, *, first_id=None):
        """`count` reports whose `fields` is the text `{not json`, eight days old.

        `first_id` raises the id floor, so the ids have as many digits as a
        live store's and the uncapped id list is as long as it would be there.
        """
        if first_id is not None:
            self.db.execute("INSERT INTO item (id, kind, title, status, created_at, updated_at)"
                            " VALUES (?, 'task', 'id floor', 'done', ?, ?)", (first_id - 1, ago(days=8), ago(days=8)))
        with transaction(self.db):
            items = [create_item(self.db, kind="report", title="broken: run report", fields={"attention": False},
                                 created_at=ago(days=8)) for _ in range(count)]
            self.db.execute(f"UPDATE item SET fields='{{not json' WHERE id IN ({','.join('?' * len(items))})", items)
        return items

    def prune_report(self):
        pruned = prune(self.db, self.backup(), now=NOW)
        row = self.db.execute("SELECT * FROM item WHERE id=?", (pruned.report,)).fetchone()
        return pruned, json.loads(row["fields"]), json.loads(row["body"])["text"], row

    def unreadable_line(self, text):
        return next(line for line in text.splitlines() if "unreadable fields" in line)

    def test_the_settle_skips_it_and_still_settles_the_clean_report(self):
        broken, = self.unreadable()
        clean = self.report(days=8)
        self.assertEqual(settle_clean_reports(self.db, now=NOW), 1)
        self.assertEqual((self.status_of(broken), self.status_of(clean)), ("planning", "done"))

    @hub_only
    def test_the_prune_names_it_and_asks_for_a_person(self):
        broken, = self.unreadable()
        clean = self.report(days=8)
        pruned, fields, text, row = self.prune_report()
        self.assertEqual(pruned.clean_reports, 1)
        self.assertEqual((self.status_of(broken), self.status_of(clean)), ("planning", "done"))
        self.assertIs(fields["attention"], True)
        followups = [note for note in workflow.item_state(self.db, row["id"])["notes"]
                     if note["kind"] == "followup" and not note["resolved_at"]]
        self.assertEqual(len(followups), 1)
        line = f"1 report(s) in planning have unreadable fields: fleet sd:{broken}"
        self.assertIn(line + "\n", text)
        self.assertEqual(fields["report"]["attention_basis"], line)

    @hub_only
    def test_absent_empty_and_malformed_fields_are_one_case_named_for_a_person(self):
        """sd:873. One meaning for a `fields` that cannot say the report is
        clean, whatever shape the column holds: NULL, the empty string, or
        text that is not JSON. `json_valid(NULL)` is NULL, not false, so `NOT
        json_valid` alone would miss the first; `json_valid('')` is 0. The
        same three inputs are pinned at the bulk clean
        (`test_report_bulk_acknowledge`), the backlog (`test_reads`) and the
        page (`local-project-dashboard`), so the predicate cannot drift at
        one site and stay green at the rest.
        """
        for value in (None, "", "{not json"):
            with self.subTest(fields=value):
                self.setUp()
                broken, = self.unreadable()
                self.db.execute("UPDATE item SET fields=? WHERE id=?", (value, broken))
                self.db.commit()
                self.assertEqual(settle_clean_reports(self.db, now=NOW), 0)
                _, fields, text, _ = self.prune_report()
                self.assertEqual(self.status_of(broken), "planning")
                self.assertIs(fields["attention"], True)
                line = f"1 report(s) in planning have unreadable fields: fleet sd:{broken}"
                self.assertIn(line + "\n", text)

    @hub_only
    def test_with_none_the_prune_report_is_the_one_it_always_was(self):
        """The values the prune writes for this fixture, spelled out; sd:3007 added the park count."""
        clean = self.report(days=8)
        snapshot = self.backup()
        pruned = prune(self.db, snapshot, now=NOW)
        row = self.db.execute("SELECT * FROM item WHERE id=?", (pruned.report,)).fetchone()
        text = (f"sd-db prune after backup {snapshot.directory.name} ({snapshot.run_id}): 0 exec output(s) expired, "
                f"0 stale heartbeat row(s) removed, 0 superseded checkpoint row(s) removed, 1 clean report(s) settled, 0 untouched item(s) parked; "
                f"cost rows and exec notes are never pruned.\n")
        stamp = NOW.isoformat(timespec="seconds")
        self.assertEqual(json.loads(row["body"])["text"], text)
        self.assertEqual(json.loads(row["fields"]), {"attention": False, "report": {
            "job": JOB, "run_id": snapshot.run_id, "started": stamp, "ended": stamp, "exit_code": 0,
            "source_path": str(snapshot.directory), "truncated": False,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "attention_basis": "explicit report",
            "removed": {"exec_outputs": 0, "heartbeats": 0, "checkpoints": 0, "clean_reports": 1, "parked": 0}}})
        self.assertEqual(row["title"], f"{JOB}: run report")
        self.assertEqual(self.status_of(clean), "done")

    @hub_only
    def test_three_hundred_are_all_named_in_the_text_and_twenty_in_the_basis(self):
        broken = self.unreadable(300, first_id=10000)
        _, fields, text, _ = self.prune_report()
        self.assertIs(fields["attention"], True)
        basis = fields["report"]["attention_basis"]
        self.assertEqual([int(item) for item in re.findall(r"fleet sd:(\d+)", basis)], broken[:20])
        self.assertTrue(basis.endswith(" and 280 more"), basis)
        self.assertLessEqual(len(basis), 2000)
        line = self.unreadable_line(text)
        self.assertEqual([int(item) for item in re.findall(r"fleet sd:(\d+)", line)], broken)
        self.assertNotIn("more", line)

    @hub_only
    def test_the_basis_says_more_only_past_twenty(self):
        self.unreadable(20)
        _, fields, _, _ = self.prune_report()
        basis = fields["report"]["attention_basis"]
        self.assertEqual(len(re.findall(r"fleet sd:\d+", basis)), 20)
        self.assertNotIn("more", basis)
        self.unreadable(1)
        later = NOW + timedelta(days=1)
        pruned = prune(self.db, run(home=self.home, when=later), now=later)
        basis = json.loads(self.db.execute("SELECT fields FROM item WHERE id=?",
                                           (pruned.report,)).fetchone()[0])["report"]["attention_basis"]
        self.assertEqual(len(re.findall(r"fleet sd:\d+", basis)), 20)
        self.assertTrue(basis.endswith(" and 1 more"), basis)

    @hub_only
    def test_the_text_names_a_thousand_and_says_how_many_more(self):
        broken = self.unreadable(1001, first_id=10000)
        _, _, text, _ = self.prune_report()
        line = self.unreadable_line(text)
        self.assertTrue(line.startswith("1001 report(s) in planning have unreadable fields: "), line)
        self.assertEqual([int(item) for item in re.findall(r"fleet sd:(\d+)", line)], broken[:1000])
        self.assertTrue(line.endswith(" and 1 more"), line)


class TheCheckpointRows(PruneCase):
    """One row per newest-only checkpoint key once the key sat a week (sd:3472)."""

    def rows(self, key, *, days, count=3):
        """`count` receipts under `key`, the newest written `days` ago; returns their ids, oldest first."""
        return [record_state(self.db, "checkpoint", key=key, body={"protocol": 1, "step": step},
                             timestamp=ago(days=days, hours=count - 1 - step)) for step in range(count)]

    def ids(self, key):
        return [row[0] for row in self.db.execute("SELECT id FROM state WHERE key=? ORDER BY id", (key,))]

    def test_an_idle_newest_only_key_keeps_its_newest_row_and_ship_read_still_answers(self):
        idle, live = self.rows("ship:idle", days=8), self.rows("ship:live", days=6)
        receipt = self.rows("sd-gate-receipt:v1:idle", days=30, count=2)
        removed = retention.compact_checkpoints(self.db, now=NOW)
        self.assertEqual(removed, 3)
        self.assertEqual(self.ids("ship:idle"), idle[-1:])
        self.assertEqual(self.ids("ship:live"), live)
        self.assertEqual(self.ids("sd-gate-receipt:v1:idle"), receipt[-1:])
        self.assertEqual(ship.read(self.db, "ship:idle"), (idle[-1], {"protocol": 1, "step": 2}))
        # The revision a waiting `ship.save` holds is the row that stayed.
        self.assertGreater(ship.save(self.db, "ship:idle", idle[-1], {"step": 3}), idle[-1])
        self.assertEqual(retention.compact_checkpoints(self.db, now=NOW), 0)

    def test_a_kept_key_and_a_lookalike_keep_every_row(self):
        lane = self.rows("lane:v1:example/repo:entry", days=30)
        operator = self.rows("operator-action", days=30)
        lookalike = self.rows("Ship:upper", days=30) + self.rows("shipX:wildcard", days=30)
        self.assertEqual(retention.compact_checkpoints(self.db, now=NOW), 0)
        self.assertEqual(self.ids("lane:v1:example/repo:entry"), lane)
        self.assertEqual(self.ids("operator-action"), operator)
        self.assertEqual(self.ids("Ship:upper") + self.ids("shipX:wildcard"), lookalike)

    @hub_only
    def test_the_prune_compacts_them_and_its_report_counts_them(self):
        kept = self.rows("ship:idle", days=10)[-1]
        pruned = prune(self.db, self.backup(), now=NOW)
        self.assertEqual(pruned.counts["checkpoints"], 2)
        self.assertEqual(self.ids("ship:idle"), [kept])
        report = self.db.execute("SELECT * FROM item WHERE id=?", (pruned.report,)).fetchone()
        self.assertEqual(json.loads(report["fields"])["report"]["removed"]["checkpoints"], 2)
        self.assertIn("2 superseded checkpoint row(s) removed", json.loads(report["body"])["text"])

    @hub_only
    def test_a_prune_stopped_after_the_compaction_is_finished_by_the_next_run(self):
        """The compaction commits alone; a stop before the report leaves one row per key, and the rerun reports."""
        kept = self.rows("ship:idle", days=10)[-1]
        snapshot = self.backup()
        with mock.patch.object(retention, "settle_clean_reports", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                prune(self.db, snapshot, now=NOW)
        self.assertEqual(self.ids("ship:idle"), [kept])
        self.assertEqual(self.db.execute("SELECT count(*) FROM item WHERE kind='report'").fetchone()[0], 0)
        pruned = prune(self.db, snapshot, now=NOW)
        self.assertEqual((pruned.counts["checkpoints"], self.ids("ship:idle")), (0, [kept]))
        self.assertTrue(pruned.report)


class EveryCheckpointWriter(unittest.TestCase):
    """Every file that writes a checkpoint names its key prefixes, and retention classifies each one.

    A new writer fails here until its prefixes go in `NEWEST_ONLY_CHECKPOINTS`,
    which compacts them, or `KEPT_CHECKPOINTS`, which says why not (sd:3472).
    """

    ROOT = Path(__file__).resolve().parents[2]
    WRITE = re.compile(r"record_state\(\s*\w+,\s*[\"']checkpoint[\"']|VALUES\s*\(\s*'checkpoint'|\b(?:ship|store)\.save\(")
    WRITERS = {
        "bin/sd-review": ("review:",),
        "bin/sd-ship": ("ship:",),
        "bin/sd_check_receipts.py": ("sd-check-receipt:",),
        "bin/sd_gate_receipts.py": ("sd-gate-receipt:",),
        "bin/sd_lane.py": ("lane:",),
        "bin/sd_ship_dispositions.py": ("ship-adjudication:", "ship-adjudication-no-item:"),
        "bin/sd_ship_no_item.py": ("ship:", "ship-review-no-item:", "ship-review-no-item-index:",
                                   "ship-adjudication-no-item:"),
        "bin/sd_ship_review.py": ("ship:", "ship-review-no-item:"),
        "lib/sd_db/backup.py": ("<backup run id>",),
        "lib/sd_db/contribution_sync.py": ("contribution-refresh-queue",),
        "lib/sd_db/contributions.py": ("contribution:",),
        "lib/sd_db/operations.py": ("operations:",),
        "lib/sd_db/provider_controls.py": ("provider-orders:",),
        "lib/sd_db/reporting.py": ("operator-action",),
        "lib/sd_db/services.py": ("operations:",),
        "lib/sd_db/ship.py": ("ship:",),
    }

    def sources(self):
        for path in sorted([*(self.ROOT / "lib").rglob("*.py"), *(self.ROOT / "bin").iterdir()]):
            if path.is_file() and not path.is_symlink():
                text = path.read_text(encoding="utf-8", errors="replace")
                if path.suffix == ".py" or text.startswith("#!/usr/bin/env python"):
                    yield path.relative_to(self.ROOT).as_posix(), text

    def test_every_writer_is_listed(self):
        found = sorted(name for name, text in self.sources() if self.WRITE.search(text))
        self.assertEqual(found, sorted(self.WRITERS))

    def test_every_prefix_is_classified_once_and_every_class_entry_has_a_writer(self):
        written = {prefix for prefixes in self.WRITERS.values() for prefix in prefixes}
        compacted, kept = set(retention.NEWEST_ONLY_CHECKPOINTS), set(retention.KEPT_CHECKPOINTS)
        self.assertEqual(compacted & kept, set())
        self.assertEqual(written, compacted | kept)

    def test_every_listed_prefix_is_spelled_in_the_tree(self):
        text = "\n".join(text for _, text in self.sources())
        for prefix in {prefix for prefixes in self.WRITERS.values() for prefix in prefixes} - {"<backup run id>"}:
            with self.subTest(prefix=prefix):
                self.assertIn(prefix, text)


@hub_only
class TheCostRow(PruneCase):
    def test_a_fourteen_month_row_on_a_spent_blocked_assignment_survives(self):
        assignment, cost = self.old_cost()
        ledger_before = [dict(row) for row in reads.item_assignments(self.db, self.item)]
        total_before = sum(row["usd"] for row in ledger_before)
        self.assertEqual(total_before, 1.0)

        prune(self.db, self.backup(), now=NOW)

        row = self.db.execute("SELECT * FROM cost WHERE id=?", (cost,)).fetchone()
        self.assertIsNotNone(row, "the ledger row is gone")
        self.assertEqual(row["usd"], 1.0)
        self.assertEqual(row["timestamp"], ago(days=14 * 30))
        held = runner.queue_state(self.db, assignment)
        self.assertEqual(held["status"], "blocked")
        # What a `budget spent` refusal reads: the assignment's rows summed
        # against its budget. The refusal itself is requirement 6's
        # reservation and is not in this library yet; the sum it will read is.
        ledger = [dict(row) for row in reads.item_assignments(self.db, self.item)]
        self.assertEqual(ledger, ledger_before)
        spent = next(row for row in ledger if row["id"] == assignment)
        self.assertGreaterEqual(spent["usd"], spent["budget_usd"], "the budget reads as spent")
        self.assertEqual(sum(row["usd"] for row in ledger), total_before, "the item screen's total")


class ParkCase(PruneCase):
    """Items seeded with a backdated last touch, for the park rules."""

    def seed(self, *, days, priority=4, status="planning", note_days=None, piece=None, due=None, kind="task",
             seconds=0, ran=False, recurrence=None):
        item = create_item(self.db, kind=kind, title=f"P{priority} {status} {days}d", repo=str(self.repo),
                           status=status, priority=priority, due=due)
        if ran:
            create_assignment(self.db, role="author", status="done", item=item)
        touched = ago(days=days, seconds=seconds)
        self.db.execute("UPDATE item SET created_at=?, updated_at=?, piece=?, recurrence=? WHERE id=?",
                        (touched, touched, piece, recurrence, item))
        self.db.execute("UPDATE note SET timestamp=? WHERE item=?", (touched, item))
        self.db.commit()
        if note_days is not None:
            note = add_note(self.db, item, "comment", "still wanted", session="operator")
            self.db.execute("UPDATE note SET timestamp=? WHERE id=?", (ago(days=note_days), note))
            self.db.commit()
        return item

    def parked_at(self, item):
        return self.db.execute("SELECT parked_at FROM item WHERE id=?", (item,)).fetchone()[0]


class TheUntouchedP4(ParkCase):
    """sd:3007: a P4 item untouched for thirty days is parked, and stays readable."""

    def test_only_an_open_p4_untouched_for_thirty_days_is_parked(self):
        stale = self.seed(days=31)
        blocked = self.seed(days=40, status="blocked")
        young = self.seed(days=29)
        noted = self.seed(days=31, note_days=5)
        p3 = self.seed(days=31, priority=3)
        done = self.seed(days=31, status="done")
        piece = self.seed(days=31, piece="essay-one")
        dated = self.seed(days=31, due="2027-01-01")

        self.assertEqual(retention.park_untouched(self.db, now=NOW), [stale, blocked])

        stamp = NOW.isoformat(timespec="seconds")
        self.assertEqual([self.parked_at(item) for item in (stale, blocked)], [stamp, stamp])
        for item in (young, noted, p3, done, piece, dated):
            self.assertIsNone(self.parked_at(item), item)
        # The status stays; only the park moves, with a note saying who and why.
        self.assertEqual(self.status_of(blocked), "blocked")
        note = self.db.execute("SELECT kind, session, body FROM note WHERE item=? ORDER BY id DESC LIMIT 1",
                               (stale,)).fetchone()
        self.assertEqual((note["kind"], note["session"]), ("comment", RETENTION))
        self.assertIn("untouched for 30 days", note["body"])
        # A second night finds nothing more.
        self.assertEqual(retention.park_untouched(self.db, now=NOW + timedelta(days=1)), [])

    def test_a_parked_item_leaves_the_backlog_and_stays_searchable(self):
        stale = self.seed(days=31)
        retention.park_untouched(self.db, now=NOW)
        self.assertNotIn(stale, [row["id"] for row in reads.backlog_items(self.db)])
        self.assertIn(stale, [row["id"] for row in reads.capture_items(self.db)])
        self.assertEqual(workflow.item_state(self.db, stale)["item"]["id"], stale)

    @hub_only
    def test_the_prune_parks_and_its_report_names_the_items(self):
        stale = self.seed(days=31)
        pruned = prune(self.db, self.backup(), now=NOW)
        self.assertEqual(pruned.counts["parked"], 1)
        report = self.db.execute("SELECT * FROM item WHERE id=?", (pruned.report,)).fetchone()
        self.assertEqual(json.loads(report["fields"])["report"]["removed"]["parked"], 1)
        text = json.loads(report["body"])["text"]
        self.assertIn("1 untouched item(s) parked", text)
        self.assertIn(f"Parked: {self.repo.name} sd:{stale}\n", text)


class TheIdlePlanningItem(ParkCase):
    """sd:6008: a planning item that never ran and sat ninety days is parked."""

    def last_note(self, item):
        return self.db.execute("SELECT kind, session, body FROM note WHERE item=? ORDER BY id DESC LIMIT 1",
                               (item,)).fetchone()

    def test_only_a_never_run_planning_item_idle_past_ninety_days_is_parked(self):
        idle = self.seed(days=91, priority=2)
        unprioritised = self.seed(days=91, priority=None, kind="idea")
        past = self.seed(days=90, seconds=1, priority=1)
        exact = self.seed(days=90, priority=2)
        ran = self.seed(days=200, priority=2, ran=True)
        ready = self.seed(days=200, priority=2, status="ready")
        blocked = self.seed(days=200, priority=3, status="blocked")
        recurring = self.seed(days=200, priority=2, recurrence="FREQ=WEEKLY")
        dated = self.seed(days=200, priority=2, due="2026-01-01")
        report = self.seed(days=200, priority=None, kind="report")
        followup = self.seed(days=200, priority=None, kind="followup")
        piece = self.seed(days=200, priority=2, piece="essay-two")
        noted = self.seed(days=200, priority=2, note_days=89)

        self.assertEqual(retention.park_untouched(self.db, now=NOW), [idle, unprioritised, past])

        for item in (exact, ran, ready, blocked, recurring, dated, report, followup, piece, noted):
            self.assertIsNone(self.parked_at(item), item)
        note = self.last_note(idle)
        self.assertEqual((note["kind"], note["session"]), ("comment", RETENTION))
        self.assertEqual(note["body"], "Parked: planning, never run, untouched for 90 days")
        self.assertEqual(self.status_of(idle), "planning")
        # A second run finds nothing more; the next day the exact one is past.
        self.assertEqual(retention.park_untouched(self.db, now=NOW), [])
        self.assertEqual(retention.park_untouched(self.db, now=NOW + timedelta(days=1)), [exact])

    def test_a_parked_item_keeps_its_stamp_and_gets_no_second_note(self):
        item = self.seed(days=200, priority=2)
        earlier = ago(days=150)
        self.db.execute("UPDATE item SET parked_at=? WHERE id=?", (earlier, item))
        self.db.commit()
        notes = self.db.execute("SELECT count(*) FROM note WHERE item=?", (item,)).fetchone()[0]

        self.assertEqual(retention.park_untouched(self.db, now=NOW), [])

        self.assertEqual(self.parked_at(item), earlier)
        self.assertEqual(self.db.execute("SELECT count(*) FROM note WHERE item=?", (item,)).fetchone()[0], notes)

    def test_a_stale_p4_planning_item_is_parked_once_by_the_p4_rule(self):
        item = self.seed(days=200)

        self.assertEqual(retention.park_untouched(self.db, now=NOW), [item])

        bodies = [row[0] for row in self.db.execute(
            "SELECT body FROM note WHERE item=? AND session=?", (item, RETENTION))]
        self.assertEqual(bodies, ["Parked: P4 untouched for 30 days"])


if __name__ == "__main__":
    unittest.main()
