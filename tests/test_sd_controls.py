"""Provider/report CLI round trips through the same shared domain as HTTP."""

import argparse
import contextlib
import getpass
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import initialise

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import sd_controls  # noqa: E402
import sd_work  # noqa: E402

PROVIDERS = """bills:
  a: {cost: subscription}
  b: {cost: subscription}
providers:
  claude: {start: "claude -p", vendor: anthropic, bill: a, roles: [author, reviewer], reader: claude-json}
  codex: {start: "codex exec", vendor: openai, bill: b, roles: [author, reviewer], reader: codex-json}
roles:
  author: [claude, codex]
  reviewer: [codex, claude]
"""


class Controls(unittest.TestCase):
    def setUp(self):
        # sd:6138: a note records the harness session id in place of the user
        # (sd:6023), so a run from a Claude Code session must not leak it in.
        self.enterContext(patch.dict(os.environ))
        os.environ.pop(sd_work.SESSION_ENV, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        initialise(home=self.home)
        (self.home / ".local/share/sd/providers.yaml").write_text(PROVIDERS)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "bin/sd"), *args],
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def test_providers_has_no_precision_verb(self):
        """sd:3034. Plain rebuttals need no reviewer scoring, so `sd providers precision` is gone."""
        result = self.cli("providers", "precision", "--json")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("invalid choice: 'precision'", result.stderr)

    def test_provider_complete_proposal_and_invalid_disable(self):
        result = self.cli("providers", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        proposal = self.home / "proposal.json"
        payload = {
            "revision": state["revision"],
            "enabled": {"claude": True, "codex": False},
            "orders": state["orders"],
        }
        proposal.write_text(json.dumps(payload))
        refused = self.cli("providers", "configure", "--file", str(proposal), "--json")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("first in both", refused.stderr)
        self.assertEqual(
            json.loads(self.cli("providers", "list", "--json").stdout), state
        )
        proposal.write_text("[]")
        refused = self.cli("providers", "configure", "--file", str(proposal))
        self.assertEqual(refused.returncode, 1)
        self.assertNotIn("Traceback", refused.stderr)

    def ingest(self, run_id, exit_code, text):
        log = self.home / f"{run_id}.log"
        log.write_text(text)
        return (
            "reports",
            "ingest",
            "fixture",
            "--run-id",
            run_id,
            "--started",
            "2026-09-08T00:00:00Z",
            "--ended",
            "2026-09-08T00:01:00Z",
            "--exit-code",
            str(exit_code),
            "--log",
            str(log),
            "--json",
        )

    def reports(self):
        listed = self.cli("reports", "list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        return json.loads(listed.stdout)

    def test_reports_list_open_is_a_table_of_open_reports(self):
        """sd:8795: `sd reports list --open` prints the open reports as a table."""
        filed = json.loads(self.cli(*self.ingest(
            "findings", 0, "Observed drift.\nSD_REPORT_ATTENTION: two stale rows\n")).stdout)
        item = filed["item"]["id"]
        listed = self.cli("reports", "list", "--open")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        head, rule, row = listed.stdout.splitlines()
        self.assertEqual(head.split(), ["ID", "Status", "Filed", "Job", "Title"])
        self.assertTrue(rule.startswith("--"))
        self.assertTrue(row.startswith(f"#{item} "), row)
        self.assertIn("fixture", row)
        done = self.cli("reports", "acknowledge", str(item), "--resolve-followups", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.cli("reports", "list", "--open").stdout, "no open reports\n")
        self.assertEqual(json.loads(self.cli("reports", "list", "--open", "--json").stdout), [])

    def test_reports_sweep_previews_then_closes_and_names_why(self):
        """sd:9759: `sd reports sweep` runs the Needs you sweep; --dry-run lists what would close and writes nothing."""
        from sd_db import connect
        from sd_db.writes import add_note, create_item
        db = connect(home=self.home)
        try:
            item = create_item(db, kind="task", title="a task", status="planning")
            note = add_note(db, item, "question", "Which way?\nOption: a (Recommended)\nOption: b", session="operator")
            ruled = add_note(db, item, "decision", f"Ruling on note #{note}: a", session="operator")
            db.commit()
        finally:
            db.close()
        preview = self.cli("reports", "sweep", "--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(preview.stdout, f"would close 1\n  sd:{item} question #{note}: answered by decision #{ruled}\n")
        ran = json.loads(self.cli("reports", "sweep", "--json").stdout)
        self.assertEqual((ran["applied"], [row["note"] for row in ran["closed"]]), (True, [note]))
        db = connect(home=self.home)
        try:
            self.assertIsNotNone(db.execute("SELECT resolved_at FROM note WHERE id = ?", (note,)).fetchone()[0])
        finally:
            db.close()
        self.assertEqual(self.cli("reports", "sweep").stdout, "closed 0\n")

    def test_report_ingestion_is_replayable_and_acknowledged(self):
        # A clean tick is not an event: the library records a heartbeat and
        # files no report (system sd:739), and replaying the same run records
        # nothing more.
        quiet = self.ingest("quiet", 0, "A completed observation.\n")
        clean = self.cli(*quiet)
        self.assertEqual(clean.returncode, 0, clean.stderr)
        beat = json.loads(clean.stdout)
        self.assertEqual(beat["recorded"], "heartbeat")
        self.assertNotIn("item", beat)
        replay = self.cli(*quiet)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replayed = json.loads(replay.stdout)
        self.assertEqual(replayed["recorded"], "nothing")
        self.assertNotIn("item", replayed)
        self.assertEqual(self.reports(), [])

        # A tick with findings is the one that files a report, so it is the one
        # the acknowledge verb has to be proved against.
        findings = self.ingest(
            "findings", 0, "Observed drift.\nSD_REPORT_ATTENTION: two stale rows\n"
        )
        first = self.cli(*findings)
        self.assertEqual(first.returncode, 0, first.stderr)
        state = json.loads(first.stdout)
        self.assertEqual(state["recorded"], "report")
        self.assertEqual(state["item"]["kind"], "report")
        self.assertIs(json.loads(state["item"]["fields"])["attention"], True)
        replay = self.cli(*findings)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        again = json.loads(replay.stdout)
        self.assertEqual(state["item"]["id"], again["item"]["id"])
        report = str(state["item"]["id"])

        # Its followup is open, so acknowledging it now is refused and changes
        # nothing.
        refused = self.cli(
            "reports", "acknowledge", report, "--if-revision", state["revision"], "--json"
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("resolve the report's followups", refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        [followup] = [note for note in state["notes"] if note["kind"] == "followup"]
        resolved = self.cli("note", "resolve", str(followup["id"]))
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        resolved = self.cli("task", "show", report, "--json")

        result = self.cli(
            "reports",
            "acknowledge",
            report,
            "--if-revision",
            json.loads(resolved.stdout)["revision"],
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["item"]["status"], "done")
        self.assertEqual([row["id"] for row in self.reports()], [state["item"]["id"]])

    #: The cutoff every bulk fixture below is judged at, and the day before it,
    #: when the two clean reports were filed.
    CUTOFF = "2026-09-10T00:00:00+00:00"
    FILED = "2026-09-09T00:00:00+00:00"

    def clean_reports(self):
        """Two clean planning reports filed before the cutoff, by id.

        Shaped the way `ingest` shapes a clean report's `fields`, filed at a
        chosen instant through the library rather than the CLI, because
        `reports ingest` stamps `created_at` with the clock and a clean tick
        files no report at all (system sd:739).
        """
        import sd_db
        from sd_db.writes import create_item

        with contextlib.closing(sd_db.connect(home=self.home)) as connection:
            return [create_item(
                connection, kind="report", title=f"{job}: run report", status="planning",
                source="cron-report", external_id=f"{job}:run", created_at=self.FILED,
                fields={"attention": False, "report": {"job": job, "ended": self.FILED}},
            ) for job in ("alpha", "beta")]

    def dump(self):
        import sd_db

        with contextlib.closing(sd_db.connect(home=self.home, write=False)) as connection:
            return tuple(connection.iterdump())

    def status(self, report):
        shown = self.cli("store", "item", str(report), "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        return json.loads(shown.stdout)["item"]["status"]

    def test_the_bulk_acknowledge_previews_refuses_and_applies_by_name(self):
        # Two clean reports before the cutoff. The dry run takes a bare date,
        # stamps it to 00:00 UTC, selects both, issues a plan and writes
        # nothing. A CLI that passed the bare date on would fail at
        # `writes.stamp` ("carries no timezone") and exit nonzero here.
        first, second = self.clean_reports()
        before = self.dump()
        dry = self.cli("reports", "acknowledge", "--all-clean", "--before", "2026-09-10", "--json")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        preview = json.loads(dry.stdout)
        self.assertEqual(preview["before"], self.CUTOFF)
        self.assertEqual([row["id"] for row in preview["selected"]], [first, second])
        self.assertEqual(preview["count"], 2)
        self.assertEqual(self.dump(), before)
        plan = preview["plan"]
        self.assertRegex(plan, r"^[0-9a-f]{64}$")

        # The stamped form is the same cutoff, so it issues the same plan.
        stamped = self.cli("reports", "acknowledge", "--all-clean", "--before", self.CUTOFF, "--json")
        self.assertEqual(stamped.returncode, 0, stamped.stderr)
        self.assertEqual(json.loads(stamped.stdout)["plan"], plan)

        # Without --json the last line is the apply command, with the stamped
        # cutoff and the plan filled in and `--who NAME` left for the caller.
        human = self.cli("reports", "acknowledge", "--all-clean", "--before", "2026-09-10")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertTrue(human.stdout.rstrip("\n").splitlines()[-1].startswith(
            f"sd reports acknowledge --all-clean --before {self.CUTOFF} --apply --if-plan "), human.stdout)
        self.assertIn(plan, human.stdout.rstrip("\n").splitlines()[-1])

        # An apply needs both the plan and a stated name (design D4). Each
        # half alone is refused before the store is opened.
        for form in (("--apply", "--if-plan", plan), ("--apply", "--who", "tester")):
            with self.subTest(form=form):
                refused = self.cli("reports", "acknowledge", "--all-clean", "--before", "2026-09-10", *form)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertNotIn("Traceback", refused.stderr)
                self.assertEqual(self.dump(), before)

        # The bulk flags with an item id, and the apply-only flags without
        # --apply: each is refused before the store is opened. A refusal that
        # fired only under --apply would let `<id> --who tester` through to
        # the single-item branch, which ignores the flag and acknowledges the
        # report under the login name -- the defect sd:755 removes.
        item = str(first)
        for form in ((item, "--who", "tester"), (item, "--before", "2026-09-10"), (item, "--apply"),
                     (item, "--if-plan", plan), (item, "--apply", "--if-plan", plan, "--who", "tester"),
                     ("--all-clean", "--before", "2026-09-10", "--who", "tester"),
                     ("--all-clean", "--before", "2026-09-10", "--if-plan", plan)):
            with self.subTest(form=form):
                refused = self.cli("reports", "acknowledge", *form)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertNotIn("Traceback", refused.stderr)
                self.assertEqual(self.dump(), before)
                self.assertEqual(self.status(first), "planning")

        # A cutoff that is not a date, or not the exact stamp, is refused with
        # the library helper's sentence: `Z` is the same instant spelled so
        # that it sorts after every `+00:00` row.
        zulu = self.cli("reports", "acknowledge", "--all-clean", "--before", "2026-09-10T00:00:00Z")
        self.assertEqual(zulu.returncode, 1, zulu.stdout)
        self.assertIn("give the cutoff as a date, YYYY-MM-DD, meaning 00:00 UTC", zulu.stderr)
        self.assertNotIn("Traceback", zulu.stderr)
        self.assertEqual(self.dump(), before)

        # The apply moves both, and the record names the stated name, the
        # login the channel authenticated, the program and the session.
        applied = subprocess.run(
            [sys.executable, str(ROOT / "bin/sd"), "reports", "acknowledge", "--all-clean", "--before",
             "2026-09-10", "--apply", "--if-plan", plan, "--who", "tester", "--json"],
            env={**os.environ, "HOME": str(self.home), "SD_SESSION": "test-session"},
            capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        record = json.loads(applied.stdout)
        self.assertEqual(record["acknowledged"], [first, second])
        self.assertEqual(
            {key: record["actor"][key] for key in ("who", "principal", "program", "session")},
            {"who": "tester", "principal": getpass.getuser(), "program": "sd reports acknowledge",
             "session": "test-session"},
        )
        self.assertEqual((self.status(first), self.status(second)), ("done", "done"))

    def test_the_single_item_flags_are_refused_with_all_clean(self):
        # `--if-revision` and `--resolve-followups` belong to the
        # single-item verb. The bulk verb has no revision to check and never
        # touches a followup, so with `--all-clean` each is refused before the
        # store is opened rather than read and ignored (review of #1002).
        self.clean_reports()
        before = self.dump()
        for form in (("--if-revision", "0" * 64), ("--resolve-followups",)):
            with self.subTest(form=form):
                refused = self.cli("reports", "acknowledge", "--all-clean", "--before", "2026-09-10", *form)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn(form[0], refused.stderr)
                self.assertNotIn("Traceback", refused.stderr)
                self.assertEqual(self.dump(), before)

    def test_the_human_dry_run_names_the_declined_rows_and_a_selection_with_no_plan(self):
        # One clean report and one with a followup a person wrote: the human
        # form lists the declined row with the library's reason. A cutoff
        # before both was filed selects nothing, so the library issues no
        # plan and the last line says no apply can succeed instead of
        # printing a command with `None` in it.
        import sd_db
        from sd_db.writes import add_note

        first, second = self.clean_reports()
        with contextlib.closing(sd_db.connect(home=self.home)) as connection:
            add_note(connection, second, "followup", "look at this", session="example")
        human = self.cli("reports", "acknowledge", "--all-clean", "--before", "2026-09-10")
        self.assertEqual(human.returncode, 0, human.stderr)
        lines = human.stdout.rstrip("\n").splitlines()
        self.assertEqual(lines[0], "clean reports before 2026-09-10T00:00:00+00:00: 1 selected, 1 declined")
        self.assertIn(f"  selected fleet sd:{first}  {self.FILED}  alpha: run report", lines)
        self.assertIn(f"  declined fleet sd:{second}: it has an unresolved followup", lines)
        self.assertTrue(lines[-1].startswith(
            f"sd reports acknowledge --all-clean --before {self.CUTOFF} --apply --if-plan "), lines[-1])

        empty = self.cli("reports", "acknowledge", "--all-clean", "--before", "2026-09-01")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        lines = empty.stdout.rstrip("\n").splitlines()
        self.assertEqual(lines[0], "clean reports before 2026-09-01T00:00:00+00:00: 0 selected, 0 declined")
        self.assertEqual(lines[-1], "no apply: a bulk acknowledge moves between 1 and 1000 reports")
        self.assertNotIn("None", empty.stdout)

    def test_the_apply_with_a_wrong_plan_is_refused_by_the_library_and_writes_nothing(self):
        # A plan token that is well-formed but not the preview's: the store
        # opens for writing, the library's savepoint refuses (`StaleItem`),
        # and `run` turns that into the one-line refusal, exit 1, so the
        # caller sees the library's sentence and no traceback, and the dump
        # is the one from before the attempt.
        first, second = self.clean_reports()
        before = self.dump()
        wrong = "f" * 64
        refused = self.cli("reports", "acknowledge", "--all-clean", "--before", "2026-09-10",
                           "--apply", "--if-plan", wrong, "--who", "tester")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertEqual(refused.stderr.rstrip("\n"),
                         "sd: the clean-report selection changed since the preview; preview it again")
        self.assertEqual(refused.stdout, "")
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.status(first), "planning")
        self.assertEqual(self.status(second), "planning")

    def test_the_bulk_dry_run_opens_the_store_read_only_and_the_apply_writes(self):
        # `run` makes one connection call. The dry run must open the store
        # the way `list` does, `write=False`, so the read runs under SQLite's
        # `mode=ro` and `query_only`; the shipped rule
        # `write=args.control_action != "list"` gives `True` for both forms
        # and fails here.
        import sd_handoff_rows

        self.clean_reports()
        opened = []
        real = sd_handoff_rows.connect

        def wrapped(sd_db, *, write=False):
            opened.append(write)
            return real(sd_db, write=write)

        def bulk(**flags):
            fields = dict(
                control_group="reports", control_action="acknowledge", item=None, if_revision=None,
                resolve_followups=False, json=True, all_clean=True, before="2026-09-10",
                apply=False, if_plan=None, who=None,
            )
            return argparse.Namespace(**{**fields, **flags})

        out = io.StringIO()
        with patch.dict(os.environ, {"HOME": str(self.home), "SD_SESSION": "test-session"}), \
                patch.object(sd_handoff_rows, "connect", wrapped), contextlib.redirect_stdout(out):
            self.assertEqual(sd_controls.run(bulk()), 0)
            plan = json.loads(out.getvalue())["plan"]
            self.assertEqual(sd_controls.run(bulk(apply=True, if_plan=plan, who="tester")), 0)
        self.assertEqual(opened, [False, True])

    def failed_run(self):
        """One failed run as the ingest files it: a report born with the ingest's own followup."""
        failed = self.cli(*self.ingest("failed", 1, "boom\n"))
        self.assertEqual(failed.returncode, 0, failed.stderr)
        state = json.loads(failed.stdout)
        [followup] = [note for note in state["notes"] if note["kind"] == "followup"]
        self.assertEqual((followup["session"], followup["resolved_at"]), ("cron", None))
        return str(state["item"]["id"]), state["revision"], followup["id"]

    def followups(self, report):
        shown = self.cli("store", "item", report, "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        state = json.loads(shown.stdout)
        return state["item"]["status"], [(note["id"], note["session"], note["resolved_at"] is not None)
                                         for note in state["notes"] if note["kind"] == "followup"]

    def test_reopen_undoes_an_acknowledge(self):
        """sd:3504. The dashboard's Undo had no CLI: `sd reports reopen` is it."""
        report, revision, _ = self.failed_run()
        done = self.cli("reports", "acknowledge", report, "--if-revision", revision,
                        "--resolve-followups", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        stale = self.cli("reports", "reopen", f"sd:{report}", "--if-revision", revision, "--json")
        self.assertEqual(stale.returncode, 1, stale.stdout)
        self.assertIn("changed", stale.stderr)
        reopened = self.cli("reports", "reopen", f"sd:{report}", "--reason", "too early", "--json")
        self.assertEqual(reopened.returncode, 0, reopened.stderr)
        state = json.loads(reopened.stdout)
        self.assertEqual(state["item"]["status"], "planning")
        self.assertIn("reopened: too early", state["notes"][-1]["body"])

    def test_the_flag_resolves_the_followup_and_is_off_by_default(self):
        # Every failure report is born with the followup the ingest wrote, so
        # without the flag the verb refuses it and names the note (sd:941).
        report, revision, note = self.failed_run()
        refused = self.cli("reports", "acknowledge", report, "--if-revision", revision, "--json")
        self.assertEqual(refused.returncode, 1)
        self.assertIn(
            f"resolve the report's followups before acknowledging it: resolve followup {note}",
            refused.stderr,
        )
        self.assertNotIn("Traceback", refused.stderr)
        self.assertEqual(self.followups(report), ("planning", [(note, "cron", False)]))

        # With the flag, one call resolves that note and finishes the report,
        # and the status note says which note it resolved.
        result = self.cli(
            "reports", "acknowledge", report, "--if-revision", revision, "--resolve-followups", "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(state["item"]["status"], "done")
        self.assertEqual(self.followups(report), ("done", [(note, "cron", True)]))
        self.assertIn(
            f"planning -> done by {getpass.getuser()}: resolved followup {note}",
            [entry["body"] for entry in state["notes"] if entry["kind"] == "status_change"],
        )

    def test_a_followup_a_person_wrote_blocks_without_the_flag_and_resolves_with_it(self):
        # sd:4722: the flag covers every open followup the report owns.
        report, revision, cron = self.failed_run()
        noted = self.cli(
            "task", "note", report, "--kind", "followup", "--body", "check the disk too",
            "--if-revision", revision, "--json",
        )
        self.assertEqual(noted.returncode, 0, noted.stderr)
        owed = json.loads(noted.stdout)["note"]["id"]
        refused = self.cli("reports", "acknowledge", report, "--json")
        self.assertEqual(refused.returncode, 1)
        self.assertIn(f"resolve followup {owed}", refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        self.assertEqual(self.followups(report)[0], "planning")
        done = self.cli("reports", "acknowledge", report, "--resolve-followups", "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        status, notes = self.followups(report)
        self.assertEqual(status, "done")
        self.assertEqual(sorted(notes), sorted([(cron, "cron", True), (owed, getpass.getuser(), True)]))

    def test_the_flag_refuses_a_library_without_the_keyword_naming_the_provisioner(self):
        # A library that predates system #394 takes no
        # `resolve_followups`; the flag must not reach it as a
        # TypeError. The verb runs in-process here so the library function can
        # be replaced by one with the old signature.
        report, revision, note = self.failed_run()

        def old_acknowledge(connection, item, *, expected_revision, who):
            raise AssertionError("the old library must not be called with the flag on")

        args = argparse.Namespace(
            control_group="reports", control_action="acknowledge", item=int(report),
            if_revision=revision, resolve_followups=True, json=True,
            all_clean=False, before=None, apply=False, if_plan=None, who=None,
        )
        from sd_db import reporting

        with patch.dict(os.environ, {"HOME": str(self.home)}), \
                patch.object(reporting, "acknowledge", old_acknowledge), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(sd_work.WorkRefusal) as raised:
                sd_controls.run(args)
        self.assertEqual(
            str(raised.exception),
            "this sd_db takes no --resolve-followups; "
            "run bin/ and lib/ from one pack checkout",
        )
        self.assertEqual(self.followups(report), ("planning", [(note, "cron", False)]))


if __name__ == "__main__":
    unittest.main()
