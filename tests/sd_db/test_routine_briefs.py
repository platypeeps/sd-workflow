"""Routine briefs as `report` rows, and the `sd reports brief` verbs (sd:4040, design sd:3459).

A brief is one `item` row of kind `report`, source `brief`, keyed by its
stem. The row is the source of truth: no file ever replaces a row that
differs, except a routine's own appending draft (`--extend`) and the
operator's `--replace`, named per stem on an import. Every database and
folder is a temp dir; nothing reads the operator's vault or store.
"""

import datetime
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, reads, reporting, repos, routine_briefs, workflow
from sd_db.migrate import initialise
from sd_db.routine_briefs import (
    BriefConflict,
    BriefUnusable,
    add,
    import_folder,
    read_draft,
    rows,
    show,
)

from . import support

PACK = Path(__file__).resolve().parents[2]

DAILY = "2026-10-13 - Daily Intel Brief"
TEXT = "---\ntags:\n  - brief\nrelated: \"[[Agents]]\"\n---\n\n# Daily Intel Brief\n\nThree   things\nhappened today.\n"
FINDINGS = "\n## Adversarial findings\n\nNone.\n"
CAFE = unicodedata.normalize("NFC", "2026-10-13 - Café")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Briefs(support.TempRootCase):
    def setUp(self):
        super().setUp()
        initialise(self.root / "sd.db")
        self.db = connect(self.root / "sd.db")
        self.addCleanup(self.db.close)
        self.checkout = support.repository(self.root / "system")
        self.repo = repos.add(self.db, self.checkout, home=self.root / "home")
        self.folder = self.root / "Briefs"
        self.folder.mkdir()

    def add(self, text=TEXT, stem=DAILY, **options):
        return add(self.db, stem, text, repo=str(self.checkout), who="operator", **options)

    def row(self, stem=DAILY):
        found = self.db.execute("SELECT * FROM item WHERE source = 'brief' AND external_id = ?", (stem,)).fetchone()
        return None if found is None else {**dict(found), "fields": json.loads(found["fields"]),
                                           "body": json.loads(found["body"])}

    def notes(self, stem=DAILY):
        return [(note["kind"], note["body"]) for note in workflow.item_state(self.db, self.row(stem)["id"])["notes"]]

    def count(self):
        return self.db.execute("SELECT count(*) FROM item WHERE source = 'brief'").fetchone()[0]

    def note(self, text=TEXT, stem=DAILY, mtime=None):
        path = self.folder / f"{stem}.md"
        path.write_text(text, encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def imported(self, **options):
        return import_folder(self.db, self.folder, repo=str(self.checkout), who="operator", **options)


class TheDraft(Briefs):
    """Each refusal comes before any write, so the draft stays and no row exists."""

    def test_a_draft_reads_as_its_stem_and_text(self):
        self.assertEqual(read_draft(self.note()), (DAILY, TEXT))

    def test_each_unusable_draft_is_refused_by_rule_and_files_nothing(self):
        cases = {
            "Daily Intel Brief.md": (TEXT.encode(), "file name"),
            "2026-13-01 - Daily Intel Brief.md": (TEXT.encode(), "file name"),
            f"{DAILY}.txt": (TEXT.encode(), "file name"),
            f"{DAILY} no frontmatter.md".replace(" no frontmatter", ""): (b"# no frontmatter\n", "frontmatter"),
            "2026-10-12 - Daily Intel Brief.md": (b"---\ntags: [x]\n---\n\xff\xfe", "UTF-8"),
            "2026-10-11 - Daily Intel Brief.md": (b"---\na: b\n---\n" + b"x" * reporting.MAX_REPORT, "bytes"),
        }
        for name, (data, rule) in cases.items():
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(data)
                with self.assertRaisesRegex(BriefUnusable, rule):
                    read_draft(path)
                self.assertTrue(path.exists())
        self.assertEqual(self.count(), 0)

    def test_add_refuses_an_unusable_text_before_any_write(self):
        with self.assertRaisesRegex(BriefUnusable, "frontmatter"):
            self.add("no frontmatter\n")
        with self.assertRaisesRegex(BriefUnusable, "file name"):
            self.add(stem="not a stem")
        self.assertEqual(self.count(), 0)

    def test_a_stem_whose_series_holds_a_slash_is_refused_since_its_copy_would_leave_the_folder(self):
        for stem in ("2026-10-13 - a/../../outside", "2026-10-13 - a\\b"):
            with self.subTest(stem=stem), self.assertRaisesRegex(BriefUnusable, "no / or"):
                self.add(stem=stem)
        self.assertEqual(self.count(), 0)

    def test_an_unregistered_checkout_is_refused_by_name(self):
        with self.assertRaisesRegex(routine_briefs.BriefRefused, "not a registered repository"):
            add(self.db, DAILY, TEXT, repo=str(self.root / "elsewhere"), who="operator")
        self.assertEqual(self.count(), 0)


class TheRow(Briefs):
    def test_a_new_stem_is_one_done_report_row_with_its_counts(self):
        self.assertEqual(self.add()["result"], "filed")
        row = self.row()
        self.assertEqual((row["kind"], row["source"], row["title"], row["status"], row["repo"]),
                         ("report", "brief", DAILY, "done", self.repo))
        brief = row["fields"]["brief"]
        self.assertEqual(row["fields"]["attention"], False)
        self.assertEqual((brief["series"], brief["day"], brief["words"], brief["sha256"]),
                         ("Daily Intel Brief", "2026-10-13", 8, sha(TEXT)))
        self.assertEqual(brief["lead"], "# Daily Intel Brief Three things happened today.")
        self.assertRegex(brief["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertEqual(row["body"], {"text": TEXT})
        self.assertEqual(self.notes(), [("status_change", "opened as done")])

    def test_signal_topics_are_copied_into_the_rows_fields(self):
        """sd:5798: a page lists briefs per topic from `fields.brief`, without parsing the text."""
        text = TEXT.replace("related:", "signal-topics:\n  - observability\n  - ai-tooling # a note\n"
                                        "  - observability\nquiet-topics:\n  - politics\nrelated:")
        self.add(text)
        self.assertEqual(self.row()["fields"]["brief"]["signal_topics"], ["observability", "ai-tooling"])
        self.assertEqual(rows(self.db)[0]["brief"]["signal_topics"], ["observability", "ai-tooling"])
        self.add(text + FINDINGS, mode="extend")
        self.assertEqual(self.row()["fields"]["brief"]["signal_topics"], ["observability", "ai-tooling"])

    def test_each_spelling_of_signal_topics_is_read_and_none_is_not_recorded(self):
        """A brief that names no `signal-topics` key keeps no field: not recorded, never an empty list."""
        for line, topics in (("signal-topics: [mezmo, 'agntcy', \"politics\"]", ["mezmo", "agntcy", "politics"]),
                             ("signal-topics: []", []), ("signal-topics:", []), ("signal-topics: mezmo", ["mezmo"])):
            with self.subTest(line=line):
                found = routine_briefs.counts(DAILY, f"---\n{line}\ntags: x\n---\nbody\n")
                self.assertEqual(found["signal_topics"], topics)
        block = "signal-topics:\n  # the routine's own\n  - mezmo\n\n  - agntcy\nquiet-topics:\n  - politics"
        self.assertEqual(routine_briefs.counts(DAILY, f"---\n{block}\n---\nbody\n")["signal_topics"],
                         ["mezmo", "agntcy"], "codex review: a comment or blank line in the list dropped topics")
        self.assertNotIn("signal_topics", routine_briefs.counts(DAILY, TEXT))
        self.assertNotIn("signal_topics", routine_briefs.counts(DAILY, "---\na: b\n---\nsignal-topics:\n  - x\n"))

    def test_a_row_filed_before_topics_were_copied_keeps_no_field(self):
        """Older state: no backfill; the same bytes again are `unchanged`, so readers still see not recorded."""
        text = TEXT.replace("related:", "signal-topics:\n  - mezmo\nrelated:")
        self.add(text)
        fields = self.row()["fields"]
        del fields["brief"]["signal_topics"]
        self.db.execute("UPDATE item SET fields = ? WHERE id = ?", (json.dumps(fields), self.row()["id"]))
        self.db.commit()
        self.assertEqual(self.add(text)["result"], "unchanged")
        self.assertNotIn("signal_topics", self.row()["fields"]["brief"])

    def test_the_same_bytes_print_unchanged_and_write_nothing(self):
        self.add()
        before = workflow.item_state(self.db, self.row()["id"])["revision"]
        self.assertEqual(self.add()["result"], "unchanged")
        self.assertEqual(workflow.item_state(self.db, self.row()["id"])["revision"], before)

    def test_other_bytes_with_no_flag_are_a_conflict_and_the_row_stays(self):
        self.add()
        with self.assertRaisesRegex(BriefConflict, DAILY):
            self.add(TEXT + FINDINGS)
        self.assertEqual(self.row()["fields"]["brief"]["sha256"], sha(TEXT))

    def test_extend_takes_a_draft_that_appends_to_the_row(self):
        at = self.add()["at"]
        self.assertEqual(self.add(TEXT + FINDINGS, mode="extend")["result"], "extended")
        row = self.row()
        self.assertEqual((row["body"]["text"], row["fields"]["brief"]["sha256"], row["fields"]["brief"]["at"]),
                         (TEXT + FINDINGS, sha(TEXT + FINDINGS), at))
        self.assertEqual(self.notes()[-1], ("comment", "Brief replaced by operator: 8 -> 12 words"))
        self.assertEqual(self.add(TEXT + FINDINGS, mode="extend")["result"], "unchanged")

    def test_extend_refuses_a_draft_that_edits_rather_than_appends(self):
        self.add()
        with self.assertRaisesRegex(BriefConflict, "not the row's text with more after it"):
            self.add(TEXT.replace("Three", "Four") + FINDINGS, mode="extend")
        self.assertEqual(self.row()["fields"]["brief"]["sha256"], sha(TEXT))

    def test_replace_takes_the_file_whatever_the_row_holds(self):
        self.add()
        self.assertEqual(self.add("---\na: b\n---\nshorter\n", mode="replace")["result"], "replaced")
        self.assertEqual(self.row()["body"]["text"], "---\na: b\n---\nshorter\n")
        self.assertEqual(self.notes()[-1], ("comment", "Brief replaced by operator: 8 -> 1 words"))

    def test_a_failure_between_the_update_and_the_note_leaves_the_old_row(self):
        self.add()
        with patch("sd_db.routine_briefs.add_note", side_effect=RuntimeError("stopped")), \
                self.assertRaisesRegex(RuntimeError, "stopped"):
            self.add(TEXT + FINDINGS, mode="extend")
        self.assertEqual((self.row()["fields"]["brief"]["sha256"], len(self.notes())), (sha(TEXT), 1))

    def test_an_answer_lost_after_the_commit_reruns_as_unchanged_with_one_row(self):
        with patch("sd_db.routine_briefs._answer", side_effect=RuntimeError("answer lost")), \
                self.assertRaisesRegex(RuntimeError, "answer lost"):
            self.add()
        self.assertEqual(self.add()["result"], "unchanged")
        self.assertEqual((self.count(), self.notes()), (1, [("status_change", "opened as done")]))

    def test_a_stale_draft_against_a_row_the_operator_replaced_is_a_conflict(self):
        self.add()
        self.add("---\na: b\n---\nthe operator's text\n", mode="replace")
        with self.assertRaisesRegex(BriefConflict, DAILY):
            self.add(TEXT + FINDINGS, mode="extend")
        self.assertEqual(self.row()["fields"]["brief"]["sha256"], sha("---\na: b\n---\nthe operator's text\n"))

    def test_a_stem_that_names_the_same_file_on_macos_is_refused_naming_both(self):
        self.add()
        self.add(stem=CAFE)
        for other, held in ((DAILY.lower(), DAILY), (DAILY.upper(), DAILY),
                            (unicodedata.normalize("NFD", CAFE), CAFE)):
            for mode in (None, "extend", "replace"):
                with self.subTest(other=other, mode=mode), \
                        self.assertRaisesRegex(routine_briefs.BriefCollision,
                                               f"^{re.escape(other)}: brief {re.escape(held)} \\(item"):
                    self.add(TEXT + FINDINGS, stem=other, mode=mode)
        self.assertEqual((self.count(), self.row()["fields"]["brief"]["sha256"]), (2, sha(TEXT)))


class TheReads(Briefs):
    def test_rows_are_newest_first_without_bodies_and_filter_by_series_and_day(self):
        self.add(stem="2026-10-12 - Daily Intel Brief")
        self.add(stem="2026-10-10 - Weekly Intel Synthesis")
        self.add()
        listed = rows(self.db)
        self.assertEqual([row["title"] for row in listed],
                         [DAILY, "2026-10-12 - Daily Intel Brief", "2026-10-10 - Weekly Intel Synthesis"])
        self.assertNotIn("body", listed[0])
        self.assertEqual(listed[0]["brief"]["words"], 8)
        self.assertEqual([row["title"] for row in rows(self.db, series="Weekly Intel Synthesis")],
                         ["2026-10-10 - Weekly Intel Synthesis"])
        self.assertEqual([row["title"] for row in rows(self.db, since="2026-10-12")],
                         [DAILY, "2026-10-12 - Daily Intel Brief"])

    def test_show_gives_the_stored_markdown_and_names_a_missing_stem(self):
        self.add()
        self.assertEqual(show(self.db, DAILY), TEXT)
        with self.assertRaisesRegex(workflow.MissingItem, "2026-01-01 - Nothing"):
            show(self.db, "2026-01-01 - Nothing")


class TheImport(Briefs):
    def test_it_files_every_brief_once_with_its_note_time_and_verifies(self):
        self.note(mtime=1760000000)
        self.note(stem="2026-06-16 - Intel Lead")
        first = self.imported()
        self.assertEqual((sorted(first["filed"]), first["verified"]), (["2026-06-16 - Intel Lead", DAILY], 2))
        self.assertEqual(self.row()["fields"]["brief"]["at"], "2025-10-09T08:53:20+00:00")
        again = self.imported()
        self.assertEqual((again["filed"], again["unchanged"], again["verified"]), ([], 2, 2))

    def test_a_stopped_import_reruns_to_the_rest_and_verifies(self):
        for day in (11, 12, 13):
            self.note(stem=f"2026-10-{day} - Daily Intel Brief")
        real = routine_briefs.add
        calls = []

        def stop_on_third(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == 3:
                raise KeyboardInterrupt
            return real(*args, **kwargs)

        with patch("sd_db.routine_briefs.add", side_effect=stop_on_third), self.assertRaises(KeyboardInterrupt):
            self.imported()
        self.assertEqual(self.count(), 2)
        rerun = self.imported()
        self.assertEqual((len(rerun["filed"]), rerun["unchanged"], rerun["verified"]), (1, 2, 3))

    def test_a_note_that_is_not_a_brief_is_refused_by_name_and_left(self):
        self.note()
        (self.folder / "Index.md").write_text("# not a brief\n", encoding="utf-8")
        result = self.imported()
        self.assertEqual(result["refused"], [{"name": "Index.md", "reason": result["refused"][0]["reason"]}])
        self.assertIn("file name", result["refused"][0]["reason"])
        self.assertTrue((self.folder / "Index.md").exists())
        self.assertEqual(result["filed"], [DAILY])

    def test_a_changed_note_is_a_conflict_and_replace_takes_that_stem_only(self):
        self.note()
        self.note(stem="2026-10-12 - Daily Intel Brief")
        self.imported()
        self.note(TEXT + FINDINGS)
        self.note(TEXT + FINDINGS, stem="2026-10-12 - Daily Intel Brief")
        listed = self.imported()
        self.assertEqual(sorted(conflict["stem"] for conflict in listed["conflicts"]),
                         ["2026-10-12 - Daily Intel Brief", DAILY])
        self.assertIn("no mirror-hash", listed["conflicts"][0]["copy"])
        self.assertIsNone(listed["verified"])
        taken = self.imported(replace=[DAILY])
        self.assertEqual((taken["replaced"], [conflict["stem"] for conflict in taken["conflicts"]]),
                         ([DAILY], ["2026-10-12 - Daily Intel Brief"]))
        self.assertEqual(self.row()["fields"]["brief"]["sha256"], sha(TEXT + FINDINGS))
        self.assertEqual(self.row("2026-10-12 - Daily Intel Brief")["fields"]["brief"]["sha256"], sha(TEXT))

    def test_a_note_named_as_a_row_but_for_case_or_form_is_refused_by_name_and_left(self):
        self.add()
        path = self.note(TEXT + FINDINGS, stem=DAILY.lower())
        result = self.imported()
        self.assertEqual([entry["name"] for entry in result["refused"]], [path.name])
        self.assertIn(f"brief {DAILY} (item", result["refused"][0]["reason"])
        self.assertEqual((result["filed"], result["conflicts"], self.count()), ([], [], 1))
        self.assertEqual(self.row()["fields"]["brief"]["sha256"], sha(TEXT))
        self.assertTrue(path.exists())

    def test_replace_names_a_note_as_macos_compares_names(self):
        self.note()
        self.imported()
        self.note(TEXT + FINDINGS)
        taken = self.imported(replace=[DAILY.lower()])
        self.assertEqual((taken["replaced"], taken["conflicts"]), ([DAILY], []))
        self.assertEqual(self.row()["fields"]["brief"]["sha256"], sha(TEXT + FINDINGS))

    def test_a_replace_naming_no_note_in_the_folder_is_refused_before_any_write(self):
        self.note()
        with self.assertRaisesRegex(BriefUnusable, "2026-01-01 - Typo"):
            self.imported(replace=["2026-01-01 - Typo"])
        self.assertEqual(self.count(), 0)

    def test_an_import_over_rendered_copies_files_nothing(self):
        self.note()
        self.imported()
        self.note(routine_briefs.hashed(TEXT))
        result = self.imported()
        self.assertEqual((result["filed"], result["unchanged"], result["conflicts"], result["verified"]), ([], 1, [], 1))

    def test_a_snapshot_lacking_a_stem_gets_it_back_from_its_copy(self):
        self.note()
        self.note(stem="2026-10-12 - Daily Intel Brief")
        self.imported()
        self.db.execute("DELETE FROM note WHERE item = ?", (self.row()["id"],))
        self.db.execute("DELETE FROM item WHERE id = ?", (self.row()["id"],))  # the restore took it away
        self.note(routine_briefs.hashed(TEXT))
        result = self.imported()
        self.assertEqual((result["filed"], result["verified"]), ([DAILY], 2))

    def test_a_copy_newer_than_its_restored_row_is_a_conflict_and_the_row_stays(self):
        self.add()
        self.add(TEXT + FINDINGS, mode="extend")
        self.note(routine_briefs.hashed(TEXT + FINDINGS))  # the render wrote the extended row
        self.restore(TEXT)
        result = self.imported()
        self.assertEqual([(c["stem"], c["copy"]) for c in result["conflicts"]],
                         [(DAILY, "its mirror-hash matches: a copy the render wrote")])
        self.assertEqual(self.row()["fields"]["brief"]["sha256"], sha(TEXT))

    def test_a_copy_older_than_its_replaced_row_is_a_conflict_and_the_row_stays(self):
        self.add()
        self.note(routine_briefs.hashed(TEXT))
        self.add("---\na: b\n---\nthe operator's text\n", mode="replace")  # its render failed
        result = self.imported()
        self.assertEqual([c["stem"] for c in result["conflicts"]], [DAILY])
        self.assertEqual(self.row()["fields"]["brief"]["sha256"], sha("---\na: b\n---\nthe operator's text\n"))

    def test_publish_restore_replace_then_import_keeps_the_replaced_row(self):
        self.add()
        self.add(TEXT + FINDINGS, mode="extend")
        self.note(routine_briefs.hashed(TEXT + FINDINGS))  # published
        self.restore(TEXT)  # back to the older row
        self.add("---\na: b\n---\nreplaced after\n", mode="replace")
        result = self.imported()
        self.assertEqual([c["stem"] for c in result["conflicts"]], [DAILY])
        self.assertEqual(self.row()["fields"]["brief"]["sha256"], sha("---\na: b\n---\nreplaced after\n"))

    def test_a_rendered_copy_at_the_size_limit_imports_back_after_its_row_is_gone(self):
        text = TEXT + "x" * (reporting.MAX_REPORT - len(TEXT.encode()) - 1) + "\n"
        self.add(text)
        self.note(routine_briefs.hashed(text))
        self.db.execute("DELETE FROM note WHERE item = ?", (self.row()["id"],))
        self.db.execute("DELETE FROM item WHERE id = ?", (self.row()["id"],))  # the restore took it away
        result = self.imported()
        self.assertEqual((result["filed"], result["refused"], result["verified"]), ([DAILY], [], 1))

    def test_a_hand_edited_copy_is_named_as_one(self):
        self.add()
        self.note(routine_briefs.hashed(TEXT).replace("Three", "Four"))
        self.assertEqual([c["copy"] for c in self.imported()["conflicts"]],
                         ["its mirror-hash does not match: a hand edit"])

    def restore(self, text):
        """What `sd-db restore` leaves: the row as an older backup held it."""
        fields = self.row()["fields"]
        fields["brief"]["sha256"] = sha(text)
        self.db.execute("UPDATE item SET body = ?, fields = ? WHERE id = ?",
                        (json.dumps({"text": text}), json.dumps(fields), self.row()["id"]))


class TheReportVerbs(Briefs):
    """A brief is `done` for good: the Reports list skips it, and no verb moves it."""

    def test_the_reports_list_skips_a_brief(self):
        self.add()
        self.assertEqual(reporting.reports(self.db), [])

    def test_acknowledge_and_reopen_refuse_a_brief(self):
        self.add()
        state = workflow.item_state(self.db, self.row()["id"])
        with self.assertRaisesRegex(workflow.WorkflowError, "a routine brief"):
            reporting.acknowledge(self.db, self.row()["id"], expected_revision=state["revision"], who="operator")
        with self.assertRaisesRegex(workflow.WorkflowError, "a routine brief"):
            reporting.reopen(self.db, self.row()["id"], expected_revision=state["revision"], who="operator")
        self.assertEqual(self.row()["status"], "done")


class TheWorkViews(Briefs):
    def test_a_brief_reaches_neither_the_backlog_nor_today(self):
        task = workflow.create_item(self.db, kind="task", title="a task", repo=self.repo)
        before = ([row["id"] for row in reads.backlog_items(self.db)], [row["id"] for row in reads.today_items(self.db)])
        self.add()
        self.assertEqual(([row["id"] for row in reads.backlog_items(self.db)],
                          [row["id"] for row in reads.today_items(self.db)]), before)
        self.assertIn(task, before[0])


class TheVerbs(Briefs):
    """`sd reports brief` through `bin/sd`, under a temp HOME whose default database is the fixture's."""

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.store = self.home / ".local/share/sd/sd.db"
        self.store.parent.mkdir(parents=True)
        self.db.close()
        (self.root / "sd.db").rename(self.store)
        self.db = connect(self.store)
        self.addCleanup(self.db.close)

    def sd(self, *args, home=None):
        return subprocess.run([sys.executable, str(PACK / "bin/sd"), "reports", "brief", *args],
                              env={**os.environ, "HOME": str(home or self.home), "PYTHONPATH": str(PACK / "lib")},
                              capture_output=True, text=True, check=False, timeout=60)

    def test_add_list_show_and_a_conflict(self):
        draft = self.note()
        filed = self.sd("add", str(draft), "--belongs-to", str(self.checkout))
        self.assertEqual((filed.returncode, filed.stdout), (0, f"filed {DAILY}\n"), filed.stderr)
        self.assertEqual(self.sd("add", str(draft), "--belongs-to", str(self.checkout)).stdout, f"unchanged {DAILY}\n")
        listed = self.sd("list")
        self.assertEqual(listed.stdout, f"2026-10-13  Daily Intel Brief  {DAILY}  8 words\n", listed.stderr)
        self.assertEqual(json.loads(self.sd("list", "--json").stdout)[0]["title"], DAILY)
        self.assertEqual(self.sd("show", DAILY).stdout, TEXT)
        draft.write_text(TEXT + FINDINGS, encoding="utf-8")
        refused = self.sd("add", str(draft), "--belongs-to", str(self.checkout))
        self.assertEqual(refused.returncode, 1)
        self.assertIn(f"{DAILY}: the row holds other text", refused.stderr)
        extended = self.sd("add", str(draft), "--belongs-to", str(self.checkout), "--extend")
        self.assertEqual((extended.returncode, extended.stdout), (0, f"extended {DAILY}\n"), extended.stderr)

    def test_a_list_past_its_limit_says_so_and_show_still_finds_the_rest(self):
        """sd:10638: `list` printed 200 of 300 briefs and said nothing. It still prints the newest 200,
        in the same text and JSON, and says on stderr that more exist and how to reach them."""
        first = datetime.date(2026, 1, 1)
        stems = [f"{first + datetime.timedelta(days=n)} - Daily Intel Brief" for n in range(201)]
        for stem in stems:
            self.add(stem=stem)
        self.db.commit()
        for args in (("list",), ("list", "--json")):
            listed = self.sd(*args)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            shown = json.loads(listed.stdout) if "--json" in args else listed.stdout.splitlines()
            self.assertEqual(len(shown), 200)
            self.assertIn("showing the newest 200 briefs; more exist", listed.stderr)
            self.assertIn("--series or --since", listed.stderr)
        narrowed = self.sd("list", "--since", stems[-3][:10])
        self.assertEqual((len(narrowed.stdout.splitlines()), narrowed.stderr), (3, ""))
        self.assertEqual(self.sd("show", stems[0]).stdout, TEXT)
        missing = self.sd("show", "2025-12-31 - Daily Intel Brief")
        self.assertEqual((missing.returncode, missing.stdout), (1, ""))
        self.assertIn("2025-12-31 - Daily Intel Brief", missing.stderr)
        self.assertIn("at most 200", self.sd("list", "--help").stdout)

    def test_an_unusable_draft_exits_2_and_extend_with_replace_is_refused(self):
        bad = self.root / "notes.md"
        bad.write_text(TEXT, encoding="utf-8")
        refused = self.sd("add", str(bad), "--belongs-to", str(self.checkout))
        self.assertEqual(refused.returncode, 2)
        self.assertIn("file name", refused.stderr)
        both = self.sd("add", str(self.note()), "--belongs-to", str(self.checkout), "--extend", "--replace")
        self.assertEqual(both.returncode, 2)
        self.assertEqual(self.count(), 0)

    def test_import_prints_filed_and_verified_and_a_bare_replace_exits_2(self):
        self.note()
        first = self.sd("import", str(self.folder), "--belongs-to", str(self.checkout))
        self.assertEqual((first.returncode, first.stdout), (0, "1 filed, 0 unchanged, 0 replaced\nverified 1\n"),
                         first.stderr)
        second = self.sd("import", str(self.folder), "--belongs-to", str(self.checkout))
        self.assertEqual(second.stdout, "0 filed, 1 unchanged, 0 replaced\nverified 1\n")
        bare = self.sd("import", str(self.folder), "--belongs-to", str(self.checkout), "--replace")
        self.assertEqual(bare.returncode, 2)
        self.note(TEXT + FINDINGS)
        conflict = self.sd("import", str(self.folder), "--belongs-to", str(self.checkout))
        self.assertEqual(conflict.returncode, 1)
        self.assertIn(f"conflict {DAILY}: no mirror-hash: a hand edit or an unrendered note", conflict.stdout)
        taken = self.sd("import", str(self.folder), "--belongs-to", str(self.checkout), "--replace", DAILY)
        self.assertEqual((taken.returncode, taken.stdout), (0, "0 filed, 0 unchanged, 1 replaced\nverified 1\n"),
                         taken.stderr)

    def test_an_unreachable_hub_exits_1_and_writes_nothing(self):
        satellite = self.root / "satellite"
        (satellite / ".config/sd").mkdir(parents=True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        (satellite / ".config/sd/hub.json").write_text(json.dumps({"hub": "127.0.0.1", "port": port}))
        refused = self.sd("add", str(self.note()), "--belongs-to", str(self.checkout), home=satellite)
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertFalse((satellite / ".local").exists())
        self.assertEqual(self.count(), 0)


if __name__ == "__main__":
    unittest.main()
