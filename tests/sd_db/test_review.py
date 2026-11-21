"""`sd_db.review`: one review queue made of notes, and `sd-db review file|export|apply` (sd:3635).

Each case names the failure-table row of the design it covers (system
`docs/work/2026-10-12-review-queue/design.md`). Every case runs on a temporary
store; none reads or writes the live database.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from sd_db import connect, review, workflow, writes
from sd_db.migrate import initialise

from . import support

KEY_A = b"subject,tier\nexample.test/a@1a2b3c4:src/one.py,cheap\nexample.test/a@1a2b3c4:src/two.py,deep\n"
KEY_B = b"subject,tier\nexample.test/b@5d6e7f8:src/one.py,skip\n"


def batch(**changes) -> dict:
    """A small valid batch file, open mode, two questions."""
    found = {
        "schema": 1, "queue": "jev-entry-2", "batch": "C1", "title": "Label: C1 review tiers",
        "source": "system@6c4317b", "mode": "open", "context": CONTEXT,
        "questions": [
            {"subject": "example.test/a@1a2b3c4:src/one.py", "text": "Which review tier fits this change?",
             "evidence": ["diff: git show 1a2b3c4"], "options": ["skip", "cheap", "standard", "deep"]},
            {"subject": "example.test/a@1a2b3c4:src/two.py", "text": "Which review tier fits this change?",
             "options": ["skip", "cheap", "standard", "deep"]},
        ],
    }
    found.update(changes)
    return found


def sealed(key: bytes = KEY_A, **changes) -> dict:
    return batch(mode="sealed", seal={"sha256": hashlib.sha256(key).hexdigest(), "name": "key-C1.csv"}, **changes)


def nightly(name: str, subjects: list[str], **changes) -> dict:
    changes.setdefault("queue", "nightly")
    return batch(batch=name, title=f"Nightly review {name}", questions=[
        {"subject": subject, "text": f"Suggestion: {subject}. Expected gain: fewer stalls.",
         "evidence": ["log: example.test/logs/run.log"], "options": ["Approve", "Reject"]}
        for subject in subjects], **changes)


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)

    def counts(self) -> tuple[int, int]:
        return (self.db.execute("SELECT count(*) FROM item").fetchone()[0],
                self.db.execute("SELECT count(*) FROM note").fetchone()[0])

    def file(self, found: dict, **extra) -> int:
        result = review.file(self.db, found, who="tester", **extra)
        return result["item"]

    def rule(self, item: int, index: int, option: str) -> dict:
        batch_read = review.read(self.db, item)
        note = batch_read["questions"][index]["note"]
        revision = workflow.item_state(self.db, item)["revision"]
        return review.rule(self.db, item, note, option, expected_revision=revision, who="tester")

    def withdraw(self, item: int, ruling: int) -> dict:
        revision = workflow.item_state(self.db, item)["revision"]
        return review.withdraw(self.db, item, ruling, expected_revision=revision, who="tester")

    def status(self, item: int) -> str:
        return self.db.execute("SELECT status FROM item WHERE id = ?", (item,)).fetchone()[0]

    def ruled_by_hand(self, item: int, index: int, option: str) -> None:
        """The Decisions panel's two library calls, which file no item."""
        note = review.read(self.db, item)["questions"][index]["note"]
        ruled = workflow.add_item_note(self.db, item, kind="decision", who="dashboard",
                                       body=f"Ruling on note #{note}: {option}")
        workflow.resolve_item_note(self.db, note, who="dashboard", expected_revision=ruled["revision"])


class FileABatch(ReviewCase):
    def test_the_item_carries_the_ref_the_header_and_one_question_note_each(self):
        item = self.file(batch())
        row = self.db.execute("SELECT * FROM item WHERE id = ?", (item,)).fetchone()
        self.assertEqual((row["source"], row["external_id"], row["title"]),
                         ("task-ref", "review:jev-entry-2:C1", "Label: C1 review tiers"))
        found = review.read(self.db, item)
        self.assertEqual(found["header"]["queue"], "jev-entry-2")
        self.assertEqual(found["header"]["source"], "system@6c4317b")
        self.assertEqual([q["subject"] for q in found["questions"]],
                         ["example.test/a@1a2b3c4:src/one.py", "example.test/a@1a2b3c4:src/two.py"])
        first = found["questions"][0]
        self.assertEqual(first["options"], ["skip", "cheap", "standard", "deep"])
        self.assertEqual(first["evidence"], ["diff: git show 1a2b3c4"])
        body = self.db.execute("SELECT kind, body FROM note WHERE id = ?", (first["note"],)).fetchone()
        # The Decisions panel's format: the question, then one `Option:` line per choice.
        self.assertEqual(body["kind"], "question")
        self.assertIn("\nOption: skip\nOption: cheap\n", body["body"])
        self.assertEqual(first["sha256"], hashlib.sha256(body["body"].encode()).hexdigest())

    def test_an_error_after_the_second_question_leaves_no_item_and_no_note(self):
        before = self.counts()
        real = writes.add_note
        calls = []

        def failing(*args, **kwargs):
            calls.append(args)
            if len(calls) == 4:  # the header, two questions, then a third write
                raise RuntimeError("stopped after the second question")
            return real(*args, **kwargs)

        three = batch()
        three["questions"].append({"subject": "example.test/a@1a2b3c4:src/three.py",
                                   "text": "Which tier?", "options": ["a", "b"]})
        with mock.patch.object(review, "add_note", side_effect=failing):
            with self.assertRaises(RuntimeError):
                review.file(self.db, three, who="tester")
        self.assertEqual(self.counts(), before)
        # The rerun files the whole batch.
        self.assertEqual(len(review.read(self.db, self.file(three))["questions"]), 3)

    def test_filing_twice_returns_one_item(self):
        first = review.file(self.db, batch(), who="tester")
        before = self.counts()
        second = review.file(self.db, batch(), who="tester")
        self.assertEqual((first["created"], second["created"]), (True, False))
        self.assertEqual(first["item"], second["item"])
        self.assertEqual(self.counts(), before)

    def test_a_changed_option_is_refused_naming_it_and_the_batch_is_unchanged(self):
        item = self.file(batch())
        before = (self.counts(), workflow.item_state(self.db, item)["revision"])
        changed = batch()
        changed["questions"][1]["options"] = ["skip", "cheap", "standard", "deeper"]
        with self.assertRaisesRegex(review.ReviewError, r"src/two\.py.*options"):
            review.file(self.db, changed, who="tester")
        self.assertEqual((self.counts(), workflow.item_state(self.db, item)["revision"]), before)

    def test_a_sealed_batch_without_a_good_seal_files_nothing(self):
        before = self.counts()
        broken = [batch(mode="sealed"),
                  batch(mode="sealed", seal={"sha256": "abc", "name": "key.csv"}),
                  batch(mode="sealed", seal={"sha256": "A" * 64, "name": "key.csv"}),
                  batch(mode="sealed", seal={"sha256": "a" * 64}),
                  batch(seal={"sha256": "a" * 64, "name": "key.csv"})]
        for found in broken:
            with self.subTest(found=found.get("seal")), self.assertRaises(review.ReviewError):
                review.file(self.db, found, who="tester")
        self.assertEqual(self.counts(), before)

    def test_the_sealed_header_carries_the_seal(self):
        item = self.file(sealed())
        header = review.read(self.db, item)["header"]
        self.assertEqual((header["mode"], header["seal"]), ("sealed", hashlib.sha256(KEY_A).hexdigest()))

    def test_the_schema_limits_are_refused_before_any_write(self):
        def one(**question):
            return batch(questions=[{"subject": "s", "text": "t", "options": ["a", "b"], **question}])

        def many(count):
            return batch(questions=[{"subject": f"s{n}", "text": "t", "options": ["a", "b"]} for n in range(count)])

        refused = {
            "one option": one(options=["a"]),
            "ten options": one(options=[str(n) for n in range(10)]),
            "a repeated option": one(options=["a", "a"]),
            "a long option": one(options=["a", "b" * 81]),
            "a padded option": one(options=["a", " b"]),
            "long text": one(text="x" * 4097),
            "six evidence lines": one(evidence=["e"] * 6),
            "a long subject": one(subject="s" * 201),
            "an Option line in the text": one(text="Which?\nOption: smuggled"),
            "a Subject line in the text": one(text="Which?\n  Subject: other"),
            "a newline in an option": one(options=["a", "b\nc"]),
            "a repeated subject": batch(questions=[{"subject": "s", "text": "t", "options": ["a", "b"]}] * 2),
            "an unknown key": batch(owner="someone"),
            "a queue with a colon": batch(queue="a:b"),
            "an unknown mode": batch(mode="secret"),
            "schema 2": batch(schema=2),
            "501 questions": many(501),
            "a limit of 501": batch(limit=501),
            "no questions": batch(questions=[]),
        }
        before = self.counts()
        for name, found in refused.items():
            with self.subTest(name), self.assertRaises(review.ReviewError):
                review.file(self.db, found, who="tester")
        self.assertEqual(self.counts(), before)
        # 501 questions file when a limit says how many to keep.
        result = review.file(self.db, batch(batch="big", questions=many(501)["questions"], limit=500), who="tester")
        self.assertEqual(len(result["dropped"]), 1)
        self.assertEqual(len(review.read(self.db, result["item"])["questions"]), 500)


    def test_300_questions_file_in_one_transaction(self):
        many = batch(questions=[{"subject": f"example.test/a@1a2b3c4:src/{n}.py", "text": "Which tier?",
                                 "options": ["skip", "cheap", "standard", "deep"]} for n in range(300)])
        statements = []
        self.db.set_trace_callback(statements.append)
        item = self.file(many)
        self.db.set_trace_callback(None)
        self.assertEqual([s for s in statements if s in ("BEGIN IMMEDIATE", "COMMIT")], ["BEGIN IMMEDIATE", "COMMIT"])
        self.assertEqual(len(review.read(self.db, item)["questions"]), 300)


class NightlySuppression(ReviewCase):
    def test_a_subject_rejected_in_90_days_is_dropped_and_named(self):
        first = self.file(nightly("2026-10-11", ["cap-retries", "split-gate", "old-idea"]))
        self.rule(first, 0, "Reject")
        self.rule(first, 1, "Approve")
        result = review.file(self.db, nightly("2026-10-12", ["cap-retries", "split-gate", "new-idea"]), who="tester")
        self.assertEqual([q["subject"] for q in review.read(self.db, result["item"])["questions"]],
                         ["split-gate", "new-idea"])
        self.assertEqual([d["subject"] for d in result["dropped"]], ["cap-retries"])
        self.assertIn("rejected", result["dropped"][0]["reason"])

    def test_a_rejection_older_than_90_days_does_not_suppress(self):
        first = self.file(nightly("2026-06-01", ["cap-retries"]))
        self.rule(first, 0, "Reject")
        later = (datetime.now(UTC) + timedelta(days=91)).isoformat(timespec="seconds")
        result = review.file(self.db, nightly("2026-09-01", ["cap-retries"]), who="tester", now=later)
        self.assertEqual(result["dropped"], [])

    def test_a_withdrawn_rejection_does_not_suppress(self):
        first = self.file(nightly("2026-10-11", ["cap-retries"]))
        ruled = self.rule(first, 0, "Reject")
        self.withdraw(first, ruled["ruling"]["id"])
        result = review.file(self.db, nightly("2026-10-12", ["cap-retries"]), who="tester")
        self.assertEqual(result["dropped"], [])

    def test_the_limit_keeps_the_first_in_rank_and_names_the_rest(self):
        result = review.file(self.db, nightly("2026-10-12", [f"idea-{n}" for n in range(7)], limit=5), who="tester")
        self.assertEqual([q["subject"] for q in review.read(self.db, result["item"])["questions"]],
                         [f"idea-{n}" for n in range(5)])
        self.assertEqual([d["subject"] for d in result["dropped"]], ["idea-5", "idea-6"])

    def test_a_rerun_after_a_new_rejection_still_finds_its_batch(self):
        """The same file twice is one batch, even when a rejection of one of its subjects landed between."""
        yesterday = self.file(nightly("2026-10-11", ["cap-retries"]))
        today = review.file(self.db, nightly("2026-10-12", ["cap-retries", "split-gate"]), who="tester")
        self.rule(yesterday, 0, "Reject")
        before = self.counts()
        again = review.file(self.db, nightly("2026-10-12", ["cap-retries", "split-gate"]), who="tester")
        self.assertEqual((again["item"], again["created"], again["dropped"]), (today["item"], False, []))
        self.assertEqual([q["subject"] for q in review.read(self.db, today["item"])["questions"]],
                         ["cap-retries", "split-gate"])
        self.assertEqual(self.counts(), before)


class LabelAndWithdraw(ReviewCase):
    def test_a_ruling_writes_the_decision_note_and_resolves_the_question(self):
        item = self.file(batch())
        state = self.rule(item, 0, "cheap")
        self.assertEqual(state["ruling"]["body"], f"Ruling on note #{review.read(self.db, item)['questions'][0]['note']}: cheap")
        found = review.read(self.db, item)
        self.assertEqual((found["questions"][0]["label"], found["open"], found["labels"]), ("cheap", 1, 1))

    def test_the_decisions_panels_two_calls_land_a_label_too(self):
        """Older state: the dashboard before the Review page writes a ruling with the same two library calls."""
        item = self.file(batch())
        note = review.read(self.db, item)["questions"][0]["note"]
        ruled = workflow.add_item_note(self.db, item, kind="decision", who="dashboard",
                                       body=f"Ruling on note #{note}: standard")
        workflow.resolve_item_note(self.db, note, who="dashboard", expected_revision=ruled["revision"])
        found = review.read(self.db, item)["questions"][0]
        self.assertEqual((found["label"], found["ruled_by"]), ("standard", "dashboard"))

    def test_a_ruling_naming_an_option_not_offered_is_not_a_label(self):
        item = self.file(batch())
        note = review.read(self.db, item)["questions"][0]["note"]
        workflow.add_item_note(self.db, item, kind="decision", who="tester", body=f"Ruling on note #{note}: medium")
        workflow.resolve_item_note(self.db, note, who="tester")
        found = review.read(self.db, item)["questions"][0]
        self.assertEqual((found["label"], found["rulings"]), (None, []))

    def test_a_stale_revision_is_refused_and_writes_nothing(self):
        item = self.file(batch())
        note = review.read(self.db, item)["questions"][0]["note"]
        stale = workflow.item_state(self.db, item)["revision"]
        self.rule(item, 1, "skip")
        before = self.counts()
        with self.assertRaises(workflow.StaleItem):
            review.rule(self.db, item, note, "cheap", expected_revision=stale, who="tester")
        self.assertEqual(self.counts(), before)

    def test_an_option_not_offered_and_a_question_already_ruled_are_refused(self):
        item = self.file(batch())
        with self.assertRaisesRegex(review.ReviewError, "no option"):
            self.rule(item, 0, "medium")
        self.rule(item, 0, "cheap")
        with self.assertRaisesRegex(review.ReviewError, "ruled"):
            self.rule(item, 0, "deep")

    def test_a_note_outside_the_batch_is_refused(self):
        item = self.file(batch())
        extra = workflow.add_item_note(self.db, item, kind="question", who="tester",
                                       body="Hand filed?\nOption: yes (Recommended)\nOption: no")["note"]["id"]
        revision = workflow.item_state(self.db, item)["revision"]
        with self.assertRaisesRegex(review.ReviewError, "not a question of"):
            review.rule(self.db, item, extra, "yes", expected_revision=revision, who="tester")

    def test_an_item_that_is_not_a_batch_is_refused(self):
        plain = workflow.capture_task(self.db, title="a plain task", who="tester")["item"]["id"]
        with self.assertRaisesRegex(review.ReviewError, "not a review batch"):
            review.read(self.db, plain)

    def test_a_withdrawal_reopens_the_question_and_keeps_both_notes(self):
        item = self.file(batch())
        ruled = self.rule(item, 0, "cheap")
        note = review.read(self.db, item)["questions"][0]["note"]
        state = self.withdraw(item, ruled["ruling"]["id"])
        self.assertEqual(state["withdrawal"]["body"], f"Withdrawn: ruling #{ruled['ruling']['id']} on note #{note}")
        found = review.read(self.db, item)["questions"][0]
        self.assertEqual((found["label"], found["open"]), (None, True))
        resolved = self.db.execute("SELECT resolved_at FROM note WHERE id = ?", (note,)).fetchone()[0]
        self.assertIsNone(resolved)
        self.assertEqual([r["ruling"] for r in found["rulings"] if r["withdrawal"]], [ruled["ruling"]["id"]])
        # It can be ruled again.
        self.assertEqual(self.rule(item, 0, "deep")["ruling"]["body"], f"Ruling on note #{note}: deep")
        self.assertEqual(review.read(self.db, item)["questions"][0]["label"], "deep")

    def test_a_second_withdrawal_is_refused_and_nothing_moves(self):
        item = self.file(batch())
        ruled = self.rule(item, 0, "cheap")
        self.withdraw(item, ruled["ruling"]["id"])
        before = (self.counts(), workflow.item_state(self.db, item)["revision"])
        with self.assertRaisesRegex(review.ReviewError, "withdrawn"):
            self.withdraw(item, ruled["ruling"]["id"])
        self.assertEqual((self.counts(), workflow.item_state(self.db, item)["revision"]), before)

    def test_a_superseded_ruling_cannot_be_withdrawn(self):
        item = self.file(batch())
        first = self.rule(item, 0, "cheap")
        self.withdraw(item, first["ruling"]["id"])
        self.rule(item, 0, "deep")
        with self.assertRaises(review.ReviewError):
            self.withdraw(item, first["ruling"]["id"])

    def test_a_withdrawal_naming_another_question_moves_nothing(self):
        item = self.file(batch())
        ruled = self.rule(item, 0, "cheap")
        other = review.read(self.db, item)["questions"][1]["note"]
        workflow.add_item_note(self.db, item, kind="decision", who="tester",
                               body=f"Withdrawn: ruling #{ruled['ruling']['id']} on note #{other}")
        found = review.read(self.db, item)["questions"][0]
        self.assertEqual((found["label"], found["ruling"]), ("cheap", ruled["ruling"]["id"]))
        self.withdraw(item, ruled["ruling"]["id"])
        self.assertIsNone(review.read(self.db, item)["questions"][0]["label"])

    def test_a_reopened_batch_still_refuses_and_a_late_hand_ruling_moves_no_label(self):
        item = self.file(batch())
        self.rule(item, 0, "cheap")
        workflow.change_status(self.db, item, "done", who="tester", reason="closed for the test")
        workflow.change_status(self.db, item, "planning", who="tester")
        with self.assertRaisesRegex(review.ReviewError, "closed"):
            self.rule(item, 1, "skip")
        note = review.read(self.db, item)["questions"][1]["note"]
        ruled = workflow.add_item_note(self.db, item, kind="decision", who="tester",
                                       body=f"Ruling on note #{note}: deep")
        workflow.resolve_item_note(self.db, note, who="tester", expected_revision=ruled["revision"])
        found = review.read(self.db, item)["questions"][1]
        self.assertEqual((found["label"], [late["note"] for late in found["late"]]), (None, [ruled["note"]["id"]]))

    def test_a_done_batch_refuses_a_ruling_and_a_withdrawal(self):
        item = self.file(batch())
        ruled = self.rule(item, 0, "cheap")
        workflow.change_status(self.db, item, "done", who="tester", reason="closed for the test")
        before = self.counts()
        with self.assertRaisesRegex(review.ReviewError, "closed"):
            self.rule(item, 1, "skip")
        with self.assertRaisesRegex(review.ReviewError, "closed"):
            self.withdraw(item, ruled["ruling"]["id"])
        self.assertEqual(self.counts(), before)


#: The question-note tags a pack before system sd:5759 reads; every other line is question text to it.
OLD_TAGS = re.compile(r"^[ \t]*(Option|Subject|Evidence):[ \t]*(.*?)[ \t]*$")
CONTEXT = {"purpose": "Calibrate the review-tier router.", "rubric": ["skip: docs only", "deep: touches state"],
           "feeds": "jev-entry-2 training labels"}


def advised(mode: str = "open", **changes) -> dict:
    """A batch with a context block and a recommendation on its first question (system sd:5759)."""
    changes.setdefault("context", CONTEXT)
    found = batch(mode=mode, **changes)
    found["questions"][0]["recommendation"] = {"option": "cheap", "reason": "a two-line diff"}
    if mode == "sealed":
        found["seal"] = {"sha256": hashlib.sha256(KEY_A).hexdigest(), "name": "key-C1.csv"}
    return found


class ContextAndRecommendation(ReviewCase):
    """A batch's context block and a question's recommendation, and the agreement read from the notes (system sd:5759)."""

    def test_the_context_files_in_the_header_and_reads_back(self):
        item = self.file(advised())
        self.assertEqual(review.read(self.db, item)["header"]["context"], CONTEXT)
        header = self.db.execute("SELECT body FROM note WHERE item = ? AND kind = 'comment' ORDER BY id LIMIT 1",
                                 (item,)).fetchone()[0]
        self.assertIn("\nPurpose: Calibrate the review-tier router.\nRubric: skip: docs only\n"
                      "Rubric: deep: touches state\nFeeds: jev-entry-2 training labels\nRecommendations: 1\n", header)
        # A batch filed with none, before pack sd:9479 required one, reads none.
        with mock.patch.object(review, "_require_context", lambda found: None):
            self.assertIsNone(review.read(self.db, self.file(batch(batch="C2", context=None)))["header"]["context"])

    def test_a_recommendation_reads_apart_from_the_evidence(self):
        item = self.file(advised())
        first, second = review.read(self.db, item)["questions"]
        self.assertEqual(first["recommendation"], {"option": "cheap", "reason": "a two-line diff"})
        self.assertEqual(first["evidence"], ["diff: git show 1a2b3c4"])
        self.assertIsNone(second["recommendation"])

    def test_an_older_reader_files_the_recommendation_under_evidence_never_the_text(self):
        # A blind batch shows the text and options only, so an older reader must not read the recommendation as text.
        item = self.file(advised(mode="blind"))
        body = self.db.execute("SELECT body FROM note WHERE id = ?",
                               (review.read(self.db, item)["questions"][0]["note"],)).fetchone()[0]
        text = [line for line in body.splitlines() if not OLD_TAGS.match(line)]
        self.assertEqual(text, ["Which review tier fits this change?"])
        self.assertIn("Evidence: Recommended: cheap", body.splitlines())
        self.assertIn("Evidence: Recommended because: a two-line diff", body.splitlines())

    def test_a_batch_filed_before_keeps_its_recommended_evidence_and_files_again(self):
        # A batch that recommends nothing writes the header and notes the version before sd:5759 wrote, which took any
        # evidence line; its header has no Recommendations line, so those lines stay evidence (codex review).
        legacy = batch()
        legacy["questions"][0]["evidence"] = ["Recommended: cheap", "Recommended because: small", "diff: git show 1a2b3c4"]
        first = review.file(self.db, legacy, who="tester")
        found = review.read(self.db, first["item"])
        self.assertIsNone(found["questions"][0]["recommendation"])
        self.assertEqual(found["questions"][0]["evidence"], legacy["questions"][0]["evidence"])
        self.assertNotIn("Recommendations:", self.db.execute(
            "SELECT body FROM note WHERE item = ? AND kind = 'comment'", (first["item"],)).fetchone()[0])
        again = review.file(self.db, legacy, who="tester")
        self.assertEqual((again["item"], again["created"]), (first["item"], False))

    def test_a_bad_context_or_recommendation_is_refused_before_any_write(self):
        def recommending(value):
            found = batch()
            found["questions"][0]["recommendation"] = value
            return found

        def evidence(line):
            found = batch()
            found["questions"][0]["evidence"] = [line]
            found["questions"][1]["recommendation"] = {"option": "skip", "reason": "docs only"}
            return found

        refused = {
            "an empty context": batch(context={}),
            "an unknown context key": batch(context={"purpose": "p", "owner": "o"}),
            "a two-line purpose": batch(context={"purpose": "p\nq"}),
            "a long purpose": batch(context={"purpose": "p" * (review.CONTEXT_CHARS + 1)}),
            "a rubric that is text": batch(context={"rubric": "one"}),
            "an empty rubric": batch(context={"rubric": []}),
            "eleven rubric lines": batch(context={"rubric": ["r"] * (review.RUBRIC_LINES + 1)}),
            "a context list": batch(context=["p"]),
            "an option not offered": recommending({"option": "never", "reason": "r"}),
            "no reason": recommending({"option": "cheap"}),
            "a two-line reason": recommending({"option": "cheap", "reason": "r\ns"}),
            "a long reason": recommending({"option": "cheap", "reason": "r" * (review.REASON_CHARS + 1)}),
            "an unknown recommendation key": recommending({"option": "cheap", "reason": "r", "score": 1}),
            "a recommendation as text": recommending("cheap"),
            "evidence that poses as one": evidence("Recommended: cheap"),
            "evidence that poses as its reason": evidence("Recommended because: small"),
            # Extreme and nested input: each refuses and none truncates.
            "a nested rubric line": batch(context={"rubric": [["r"]]}),
            "a null rubric line": batch(context={"rubric": [None]}),
            "a null among rubric lines": batch(context={"rubric": ["r", None]}),
            "a null purpose": batch(context={"purpose": None}),
            "an infinite purpose": batch(context={"purpose": float("inf")}),
            "a zero feeds": batch(context={"feeds": 0}),
            "a NUL in the purpose": batch(context={"purpose": "p\x00q"}),
            "a lone surrogate in the reason": recommending({"option": "cheap", "reason": "\ud800"}),
            "a null reason": recommending({"option": "cheap", "reason": None}),
            "a nested option": recommending({"option": ["cheap"], "reason": "r"}),
            "an empty reason": recommending({"option": "cheap", "reason": ""}),
        }
        before = self.counts()
        for name, found in refused.items():
            with self.subTest(name), self.assertRaises(review.ReviewError):
                review.file(self.db, found, who="tester")
        self.assertEqual(self.counts(), before)
        # Each limit itself files, whole.
        edge = batch(batch="edge", context={"purpose": "p" * review.CONTEXT_CHARS, "feeds": "f" * review.CONTEXT_CHARS,
                                            "rubric": ["r" * review.CONTEXT_CHARS] * review.RUBRIC_LINES})
        edge["questions"][0]["recommendation"] = {"option": "deep", "reason": "w" * review.REASON_CHARS}
        found = review.read(self.db, self.file(edge))
        self.assertEqual(found["header"]["context"]["rubric"], ["r" * review.CONTEXT_CHARS] * review.RUBRIC_LINES)
        self.assertEqual(found["questions"][0]["recommendation"]["reason"], "w" * review.REASON_CHARS)

    def test_a_changed_recommendation_or_context_is_refused_naming_it(self):
        self.file(advised())
        changed = advised()
        changed["questions"][0]["recommendation"]["option"] = "deep"
        with self.assertRaisesRegex(review.ReviewError, r"src/one\.py: the recommendation differ"):
            review.file(self.db, changed, who="tester")
        with self.assertRaisesRegex(review.ReviewError, r"context is"):
            review.file(self.db, advised(context={"purpose": "Another purpose."}), who="tester")

    def test_a_batch_missing_part_of_its_context_is_refused_naming_the_tag(self):
        """pack sd:9479: two batches reached the Review page with no purpose, rubric or expectations. A batch files
        with its Purpose, at least one Rubric line and its Feeds, or not at all, and the refusal names the format."""
        cases = {"Purpose": {k: v for k, v in CONTEXT.items() if k != "purpose"},
                 "Rubric": {k: v for k, v in CONTEXT.items() if k != "rubric"},
                 "Feeds": {k: v for k, v in CONTEXT.items() if k != "feeds"},
                 "Purpose, Rubric or Feeds": None}
        before = self.counts()
        for tags, context in cases.items():
            with self.subTest(tags), self.assertRaisesRegex(
                    review.ReviewError, rf"no {tags} line; .*\"context\": {{\"purpose\": TEXT, \"rubric\": \[LINE"):
                review.file(self.db, batch(context=context), who="tester")
        self.assertEqual(self.counts(), before)

    def test_a_batch_filed_before_without_context_files_again(self):
        """pack sd:9479: the same file again, or that file once its filer adds the context, finds the batch filed
        before context was required and writes nothing."""
        with mock.patch.object(review, "_require_context", lambda found: None):
            first = review.file(self.db, batch(context=None), who="tester")
        before = self.counts()
        for found in (batch(context=None), batch()):
            again = review.file(self.db, found, who="tester")
            self.assertEqual((again["item"], again["created"]), (first["item"], False))
        self.assertEqual(self.counts(), before)
        self.assertIsNone(review.read(self.db, first["item"])["header"]["context"])

    def test_a_legacy_batch_refiled_with_context_and_a_change_names_the_change(self):
        """sd:9659: a batch filed without context, refiled with its context and another change, names that change,
        not the context the header never had."""
        with mock.patch.object(review, "_require_context", lambda found: None):
            review.file(self.db, batch(context=None), who="tester")
        with self.assertRaises(review.ReviewError) as caught:
            changed = batch()
            changed["questions"][1]["text"] = "Which tier now?"
            review.file(self.db, changed, who="tester")
        self.assertIn("question example.test/a@1a2b3c4:src/two.py: the text differ", str(caught.exception))

    def test_a_kev_file_carries_its_context_and_files(self):
        """pack sd:9479: a Kev file holds no context, so the import says what its lines are, how to label them and
        what the labels feed."""
        found = review.from_kev(kev_text([kev_line("span:a")]), batch="b", mode="blind", source="kev:t.jsonl")
        context = found["context"]
        self.assertIn("kev:t.jsonl", context["purpose"])
        self.assertTrue(context["rubric"])
        self.assertIn("reviews", context["feeds"])
        self.assertIsNotNone(self.file(found))

    def test_a_kev_file_digests_as_before(self):
        # A Kev batch filed before sd:5759 must find itself again: the import adds no key to its questions. Its
        # context, added for pack sd:9479, still finds a batch filed without one.
        found = review.from_kev(kev_text([kev_line("span:a")]), batch="b", mode="blind", source="kev:t.jsonl")
        self.assertEqual(set(found["questions"][0]), {"subject", "text", "evidence", "options"})
        with mock.patch.object(review, "_require_context", lambda found: None):
            first = review.file(self.db, {key: value for key, value in found.items() if key != "context"}, who="t")
        again = review.file(self.db, found, who="tester")
        self.assertEqual((again["item"], again["created"]), (first["item"], False))

    def test_an_open_ruling_agrees_but_is_never_blind(self):
        item = self.file(advised())
        self.rule(item, 0, "cheap")
        self.rule(item, 1, "deep")
        first, second = review.read(self.db, item)["questions"]
        self.assertEqual((first["agreed"], first["ruled_blind"]), (True, False))
        # No recommendation: nothing to agree with.
        self.assertEqual((second["agreed"], second["ruled_blind"]), (None, None))

    def test_a_blind_first_ruling_is_blind_and_a_ruling_after_an_undo_is_not(self):
        for mode in ("blind", "sealed"):
            with self.subTest(mode):
                item = self.file(advised(mode=mode, batch=f"B-{mode}"))
                found = review.read(self.db, item)["questions"][0]
                self.assertEqual((found["agreed"], found["ruled_blind"]), (None, None))
                ruled = self.rule(item, 0, "deep")
                found = review.read(self.db, item)["questions"][0]
                self.assertEqual((found["agreed"], found["ruled_blind"]), (False, True))
                # The page shows the recommendation once a ruling lands, so a ruling after its undo saw it.
                self.withdraw(item, ruled["ruling"]["id"])
                self.rule(item, 0, "cheap")
                found = review.read(self.db, item)["questions"][0]
                self.assertEqual((found["agreed"], found["ruled_blind"]), (True, False))

    def test_the_export_carries_the_context_each_recommendation_and_the_agreement(self):
        found = advised(mode="blind")
        found["questions"][1]["recommendation"] = {"option": "skip", "reason": "docs only"}
        item = self.file(found)
        self.rule(item, 0, "cheap")
        self.rule(item, 1, "deep")
        manifest, first, second = review.export(self.db, item, who="tester")
        self.assertEqual(manifest["context"], CONTEXT)
        self.assertEqual(manifest["agreement"], {"recommended": 2, "agreed": 1, "ruled_blind": 2, "agreed_blind": 1})
        self.assertEqual((first["recommendation"], first["agreed"], first["ruled_blind"]),
                         ({"option": "cheap", "reason": "a two-line diff"}, True, True))
        self.assertEqual((second["agreed"], second["ruled_blind"]), (False, True))
        self.assertEqual(first["evidence"], ["diff: git show 1a2b3c4"])

    def test_a_batch_with_no_recommendation_exports_a_zero_agreement(self):
        item = self.file(batch())
        self.rule(item, 0, "cheap")
        manifest, first, _ = review.export(self.db, item, who="tester")
        self.assertEqual((manifest["context"], manifest["agreement"]),
                         (CONTEXT, {"recommended": 0, "agreed": 0, "ruled_blind": 0, "agreed_blind": 0}))
        self.assertEqual((first["recommendation"], first["agreed"], first["ruled_blind"]), (None, None, None))


class Export(ReviewCase):
    def test_the_manifest_and_each_line_carry_the_provenance(self):
        item = self.file(batch())
        ruled = self.rule(item, 0, "cheap")
        self.withdraw(item, ruled["ruling"]["id"])
        self.rule(item, 0, "deep")
        note = review.read(self.db, item)["questions"][0]["note"]
        workflow.add_item_note(self.db, item, who="tester", body=f"On note #{note}: borderline")
        lines = review.export(self.db, item, who="tester")
        manifest, first, second = lines
        self.assertEqual({k: manifest[k] for k in ("kind", "batch", "ref", "queue", "source", "mode", "seal",
                                                     "questions", "labels", "open")},
                         {"kind": "manifest", "batch": item, "ref": "review:jev-entry-2:C1", "queue": "jev-entry-2",
                          "source": "system@6c4317b", "mode": "open", "seal": None,
                          "questions": 2, "labels": 1, "open": 1})
        self.assertRegex(manifest["exported_by"], r"^sd-ai-command-pack@([0-9a-f]{40}|unknown)$")
        self.assertEqual((first["label"], first["position"], first["ruled_by"]), ("deep", 4, "tester"))
        # A producer's per-question provenance, such as a grader version, rides in the evidence lines.
        self.assertEqual(first["evidence"], ["diff: git show 1a2b3c4"])
        self.assertEqual([w["ruling"] for w in first["withdrawn"]], [ruled["ruling"]["id"]])
        self.assertEqual([c["text"] for c in first["comments"]], ["borderline"])
        body = self.db.execute("SELECT body FROM note WHERE id = ?", (note,)).fetchone()[0]
        self.assertEqual(first["question_sha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual((second["label"], second["ruling"]), (None, None))
        self.assertNotIn("key", first)

    def test_the_file_lands_through_a_part_file_and_a_rename(self):
        item = self.file(batch())
        out = self.root / "out" / "labels.jsonl"
        out.parent.mkdir()
        review.write(review.export(self.db, item, who="tester"), out)
        self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ["labels.jsonl"])
        self.assertEqual(json.loads(out.read_text().splitlines()[0])["kind"], "manifest")

    def test_a_key_on_a_batch_that_is_not_sealed_is_refused(self):
        item = self.file(batch())
        with self.assertRaisesRegex(review.ReviewError, "not sealed"):
            review.export(self.db, item, key=KEY_A, who="tester")


class SealedExport(ReviewCase):
    def ruled_sealed(self) -> int:
        item = self.file(sealed())
        self.rule(item, 0, "cheap")
        self.rule(item, 1, "standard")
        return item

    def test_the_key_joins_in_by_subject_and_the_batch_closes(self):
        item = self.ruled_sealed()
        manifest, first, second = review.export(self.db, item, key=KEY_A, who="tester")
        self.assertEqual(self.status(item), "done")
        self.assertEqual(manifest["key"], {"sha256": hashlib.sha256(KEY_A).hexdigest(), "rows": 2, "unmatched": 0})
        self.assertEqual((first["key"]["tier"], second["key"]["tier"]), ("cheap", "deep"))

    def test_a_stop_after_the_close_refuses_a_ruling_and_the_rerun_writes_the_file(self):
        item = self.ruled_sealed()
        # Reopen one question first, so a ruling could land if the close did not hold.
        ruling = review.read(self.db, item)["questions"][1]["ruling"]
        self.withdraw(item, ruling)
        self.rule(item, 1, "deep")
        out = self.root / "labels.jsonl"
        with mock.patch.object(review, "_lines", side_effect=RuntimeError("stopped after the close")):
            with self.assertRaises(RuntimeError):
                review.export(self.db, item, key=KEY_A, who="tester")
        self.assertEqual(self.status(item), "done")
        latest = review.read(self.db, item)["questions"][1]["ruling"]
        with self.assertRaisesRegex(review.ReviewError, "closed"):
            self.withdraw(item, latest)
        review.write(review.export(self.db, item, key=KEY_A, who="tester"), out)
        self.assertEqual(len(out.read_text().splitlines()), 3)

    def test_another_batchs_key_is_refused_and_changes_nothing(self):
        item = self.ruled_sealed()
        before = (self.counts(), workflow.item_state(self.db, item)["revision"])
        with self.assertRaisesRegex(review.ReviewError, "key"):
            review.export(self.db, item, key=KEY_B, who="tester")
        self.assertEqual((self.counts(), workflow.item_state(self.db, item)["revision"]), before)
        self.assertEqual(self.status(item), "planning")

    def test_one_open_question_refuses_the_export_naming_the_count(self):
        item = self.file(sealed())
        self.rule(item, 0, "cheap")
        before = self.counts()
        with self.assertRaisesRegex(review.ReviewError, "1 question"):
            review.export(self.db, item, key=KEY_A, who="tester")
        self.assertEqual((self.counts(), self.status(item)), (before, "planning"))

    def test_without_a_key_a_sealed_batch_exports_labels_and_stays_open(self):
        item = self.file(sealed())
        self.rule(item, 0, "cheap")
        lines = review.export(self.db, item, who="tester")
        self.assertEqual((lines[0]["open"], self.status(item)), (1, "planning"))
        self.assertNotIn("key", lines[1])

    def test_a_key_is_parsed_only_after_the_close_commits(self):
        item = self.ruled_sealed()
        with mock.patch.object(review, "_key_rows", side_effect=RuntimeError("stopped while reading the key")):
            with self.assertRaises(RuntimeError):
                review.export(self.db, item, key=KEY_A, who="tester")
        self.assertEqual(self.status(item), "done")

    def test_a_late_hand_ruling_after_the_export_moves_no_label(self):
        item = self.ruled_sealed()
        review.export(self.db, item, key=KEY_A, who="tester")
        note = review.read(self.db, item)["questions"][0]["note"]
        workflow.add_item_note(self.db, item, kind="decision", who="tester", body=f"Ruling on note #{note}: deep")
        line = review.export(self.db, item, key=KEY_A, who="tester")[1]
        self.assertEqual((line["label"], len(line["late"])), ("cheap", 1))

    def test_a_key_with_a_repeated_subject_or_column_is_refused(self):
        for key, reason in ((KEY_A + b"example.test/a@1a2b3c4:src/one.py,skip\n", "more than once"),
                            (b"subject,subject,tier\nexample.test/a@1a2b3c4:src/one.py,"
                             b"example.test/a@1a2b3c4:src/two.py,deep\n", "unique, named columns")):
            with self.subTest(reason=reason):
                item = self.file(sealed(key=key, batch=f"K{len(key)}"))
                self.rule(item, 0, "cheap")
                self.rule(item, 1, "deep")
                with self.assertRaisesRegex(review.ReviewError, reason):
                    review.export(self.db, item, key=key, who="tester")


class Apply(ReviewCase):
    def approved(self) -> int:
        """Rulings that filed nothing: the Decisions panel's two calls, or a build before sd:4924."""
        item = self.file(nightly("2026-10-12", ["cap-retries", "split-gate", "drop-idea"]))
        self.ruled_by_hand(item, 0, "Approve")
        self.ruled_by_hand(item, 1, "Approve")
        self.ruled_by_hand(item, 2, "Reject")
        return item

    def task(self, note: int):
        return self.db.execute("SELECT * FROM item WHERE source = 'task-ref' AND external_id = ?",
                               (f"review-approved:{note}",)).fetchone()

    def test_each_approval_becomes_one_task_and_the_ruled_batch_closes(self):
        item = self.approved()
        result = review.apply(self.db, "nightly", who="tester")
        notes = [q["note"] for q in review.read(self.db, item)["questions"]]
        self.assertEqual([t["note"] for t in result["tasks"]], notes[:2])
        self.assertIsNone(self.task(notes[2]))
        self.assertIn("cap-retries", self.task(notes[0])["title"])
        self.assertEqual((result["closed"], self.status(item)), ([item], "done"))

    def test_a_stop_after_the_first_task_reruns_to_one_task_each_and_never_edits_the_first(self):
        item = self.approved()
        notes = [q["note"] for q in review.read(self.db, item)["questions"]]
        real = workflow.capture_task
        calls = []

        def stopping(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("stopped after task one")
            return real(*args, **kwargs)

        with mock.patch.object(review.workflow, "capture_task", side_effect=stopping):
            with self.assertRaises(RuntimeError):
                review.apply(self.db, "nightly", who="tester")
        first = dict(self.task(notes[0]))
        self.assertIsNone(self.task(notes[1]))
        self.assertEqual(self.status(item), "planning")
        result = review.apply(self.db, "nightly", who="tester")
        self.assertEqual(([t["note"] for t in result["tasks"]], result["existing"]), ([notes[1]], [notes[0]]))
        self.assertEqual(dict(self.task(notes[0])), first)
        self.assertEqual(self.status(item), "done")
        self.assertEqual(review.apply(self.db, "nightly", who="tester")["tasks"], [])

    def test_a_batch_older_than_7_days_expires_its_open_questions_and_closes(self):
        item = self.file(nightly("2026-10-01", ["cap-retries", "split-gate"]))
        self.rule(item, 0, "Reject")
        self.assertEqual(review.apply(self.db, "nightly", who="tester")["closed"], [])
        later = (datetime.now(UTC) + timedelta(days=8)).isoformat(timespec="seconds")
        result = review.apply(self.db, "nightly", who="tester", now=later)
        note = review.read(self.db, item)["questions"][1]["note"]
        self.assertEqual((result["expired"], result["closed"]), ([note], [item]))
        found = review.read(self.db, item)["questions"][1]
        self.assertEqual((found["label"], [c["text"] for c in found["comments"]]), (None, ["Expired, not ruled"]))
        self.assertEqual((found["open"], found["expired"]), (False, True))
        resolved = self.db.execute("SELECT resolved_at FROM note WHERE id = ?", (note,)).fetchone()[0]
        self.assertIsNotNone(resolved)

    def test_a_stop_after_one_expiry_reruns_to_a_closed_batch(self):
        item = self.file(nightly("2026-10-01", ["cap-retries", "split-gate"]))
        later = (datetime.now(UTC) + timedelta(days=8)).isoformat(timespec="seconds")
        real = writes.resolve_note
        calls = []

        def stopping(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("stopped after one expiry")
            return real(*args, **kwargs)

        with mock.patch.object(review, "resolve_note", side_effect=stopping):
            with self.assertRaises(RuntimeError):
                review.apply(self.db, "nightly", who="tester", now=later)
        found = review.read(self.db, item)
        self.assertEqual((found["open"], self.status(item)), (1, "planning"))
        result = review.apply(self.db, "nightly", who="tester", now=later)
        self.assertEqual((len(result["expired"]), result["closed"]), (1, [item]))
        comments = [c for q in review.read(self.db, item)["questions"] for c in q["comments"]]
        self.assertEqual(len(comments), 2)

    def test_a_queue_with_other_options_is_refused_before_any_write(self):
        self.approved()
        labels = self.file(batch(queue="nightly", batch="odd"))
        before = self.counts()
        with self.assertRaisesRegex(review.ReviewError, "Approve and Reject"):
            review.apply(self.db, "nightly", who="tester")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.status(labels), "planning")

    def test_another_queue_is_untouched(self):
        other = self.file(nightly("2026-10-12", ["cap-retries"], queue="weekly"))
        self.rule(other, 0, "Approve")
        self.assertEqual(review.apply(self.db, "nightly", who="tester"),
                         {"tasks": [], "existing": [], "expired": [], "closed": []})
        self.assertEqual(self.status(other), "planning")


class ApprovalFiles(ReviewCase):
    """sd:4924: an Approve ruling files the task `apply` would, at once, and names it on the ruling."""

    SUBJECT = "repo-sync:routine-prune"

    def setUp(self):
        super().setUp()
        self.repo = self.root / "repos" / "system"
        writes.upsert_repo(self.db, str(self.repo))

    def note(self, item: int, index: int = 0) -> int:
        return review.read(self.db, item)["questions"][index]["note"]

    def filed(self, note: int):
        return writes.item_by_external(self.db, "task-ref", f"review-approved:{note}")

    def test_an_approval_files_one_task_and_the_ruling_names_it(self):
        item = self.file(nightly("2026-10-17", [self.SUBJECT]))
        note = self.note(item)
        before = self.counts()[0]

        state = self.rule(item, 0, "Approve")

        filed = self.filed(note)
        ruling = state["ruling"]["id"]
        self.assertEqual(self.counts()[0], before + 1)
        self.assertEqual(filed["title"], f"Suggestion: {self.SUBJECT}. Expected gain: fewer stalls.")
        self.assertEqual(filed["body"], f"Suggestion: {self.SUBJECT}. Expected gain: fewer stalls.\n"
                                        "Evidence: log: example.test/logs/run.log\n"
                                        f"Approved by ruling #{ruling} on note #{note} of sd:{item} "
                                        "(review:nightly:2026-10-17).")
        self.assertEqual(filed["repo"], writes.upsert_repo(self.db, str(self.repo)))
        self.assertEqual(state["ruling"]["body"], f"Ruling on note #{note}: Approve -> sd:{filed['id']}")
        self.assertEqual(state["filed"], {"item": filed["id"], "created": True})
        self.assertEqual(state["revision"], workflow.item_state(self.db, item)["revision"])
        question = review.read(self.db, item)["questions"][0]
        self.assertEqual((question["label"], question["filed"], question["open"]), ("Approve", filed["id"], False))

    def test_rule_then_apply_files_exactly_one_item(self):
        item = self.file(nightly("2026-10-17", [self.SUBJECT]))
        before = self.counts()[0]

        self.rule(item, 0, "Approve")
        result = review.apply(self.db, "nightly", who="tester")

        self.assertEqual((result["tasks"], result["existing"], result["closed"]), ([], [self.note(item)], [item]))
        self.assertEqual(self.counts()[0], before + 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM item WHERE external_id LIKE 'review-approved:%'")
                         .fetchone()[0], 1)

    def test_a_rejection_files_nothing(self):
        item = self.file(nightly("2026-10-17", [self.SUBJECT]))
        before = self.counts()[0]

        state = self.rule(item, 0, "Reject")

        self.assertEqual(self.counts()[0], before)
        self.assertIsNone(self.filed(self.note(item)))
        self.assertEqual(state["ruling"]["body"], f"Ruling on note #{self.note(item)}: Reject")
        self.assertIsNone(state["filed"])

    def test_a_source_no_registered_repo_answers_files_with_no_repo(self):
        writes.upsert_repo(self.db, str(self.root / "other" / "system"))
        for name, source in (("2026-10-15", "elsewhere@83035dae4965"), ("2026-10-16", "system@83035dae4965")):
            item = self.file(nightly(name, [self.SUBJECT], source=source))
            self.rule(item, 0, "Approve")
            # The second: two registered checkouts are named system, so neither is the one.
            self.assertIsNone(self.filed(self.note(item))["repo"], source)

    def test_a_question_with_other_options_files_nothing_on_approve(self):
        item = self.file(batch(queue="labels", questions=[
            {"subject": self.SUBJECT, "text": "Keep this label?", "options": ["Approve", "Hold"]}]))
        before = self.counts()[0]

        state = self.rule(item, 0, "Approve")

        self.assertEqual(self.counts()[0], before)
        self.assertIsNone(state["filed"])

    def test_a_withdrawal_leaves_the_task_and_a_second_approval_names_it_again(self):
        item = self.file(nightly("2026-10-17", [self.SUBJECT]))
        note = self.note(item)
        ruling = self.rule(item, 0, "Approve")["ruling"]["id"]
        filed = self.filed(note)["id"]

        state = self.withdraw(item, ruling)

        self.assertEqual(state["withdrawal"]["body"], f"Withdrawn: ruling #{ruling} on note #{note}; sd:{filed} stays filed")
        self.assertEqual(self.status(filed), "planning")
        self.assertTrue(review.read(self.db, item)["questions"][0]["open"])
        before = self.counts()[0]
        again = self.rule(item, 0, "Approve")
        self.assertEqual(again["filed"], {"item": filed, "created": False})
        self.assertEqual(again["ruling"]["body"], f"Ruling on note #{note}: Approve -> sd:{filed}")
        self.assertEqual(self.counts()[0], before)

    def test_a_stop_before_the_ruling_lands_leaves_no_task(self):
        item = self.file(nightly("2026-10-17", [self.SUBJECT]))
        before = self.counts()

        with (mock.patch.object(review.workflow, "resolve_item_note", side_effect=RuntimeError("stopped")),
              self.assertRaises(RuntimeError)):
            self.rule(item, 0, "Approve")

        self.assertEqual(self.counts(), before)
        self.assertIsNone(self.filed(self.note(item)))


class FrozenAtClose(ReviewCase):
    """Class pass (lane review on 2ab91c11d): labels come from the note history up to the first
    close, never from a question's live `resolved_at`, so nothing after the close moves them."""

    def hand_ruling(self, item: int, index: int, option: str) -> int:
        """A ruling written as a decision note alone, as `sd task note` writes one: no resolution."""
        note = review.read(self.db, item)["questions"][index]["note"]
        return workflow.add_item_note(self.db, item, kind="decision", who="tester",
                                      body=f"Ruling on note #{note}: {option}")["note"]["id"]

    def close(self, item: int) -> None:
        workflow.change_status(self.db, item, "done", who="tester", reason="closed for the test")

    @staticmethod
    def frozen(lines: list[dict]) -> list[dict]:
        return [{k: v for k, v in line.items() if k != "exported_at"} for line in lines]

    def test_resolving_a_question_after_the_close_moves_no_label(self):
        item = self.file(batch())
        self.rule(item, 0, "cheap")
        self.hand_ruling(item, 1, "deep")
        self.close(item)
        before = self.frozen(review.export(self.db, item, who="tester"))
        note = review.read(self.db, item)["questions"][1]["note"]
        workflow.resolve_item_note(self.db, note, who="tester")
        self.assertEqual(self.frozen(review.export(self.db, item, who="tester")), before)
        workflow.change_status(self.db, item, "planning", who="tester")
        self.assertEqual(self.frozen(review.export(self.db, item, who="tester")), before)
        self.assertEqual([line["label"] for line in before[1:]], ["cheap", "deep"])

    def test_reopening_a_question_after_the_close_moves_no_label(self):
        item = self.file(batch())
        self.rule(item, 0, "cheap")
        self.rule(item, 1, "skip")
        self.close(item)
        before = self.frozen(review.export(self.db, item, who="tester"))
        writes.reopen_note(self.db, review.read(self.db, item)["questions"][0]["note"])
        self.assertEqual(self.frozen(review.export(self.db, item, who="tester")), before)
        self.assertEqual((before[0]["labels"], before[0]["open"]), (2, 0))

    def test_a_sealed_export_is_the_same_after_a_question_is_reopened(self):
        item = self.file(sealed())
        self.rule(item, 0, "cheap")
        self.rule(item, 1, "deep")
        before = self.frozen(review.export(self.db, item, key=KEY_A, who="tester"))
        writes.reopen_note(self.db, review.read(self.db, item)["questions"][1]["note"])
        self.assertEqual(self.frozen(review.export(self.db, item, key=KEY_A, who="tester")), before)

    def test_a_rejection_still_suppresses_after_its_question_is_reopened(self):
        first = self.file(nightly("2026-10-11", ["cap-retries"]))
        self.rule(first, 0, "Reject")
        self.close(first)
        writes.reopen_note(self.db, review.read(self.db, first)["questions"][0]["note"])
        result = review.file(self.db, nightly("2026-10-12", ["cap-retries", "split-gate"]), who="tester")
        self.assertEqual([d["subject"] for d in result["dropped"]], ["cap-retries"])

    def test_apply_does_not_expire_a_question_a_ruling_settled(self):
        item = self.file(nightly("2026-10-01", ["cap-retries", "split-gate"]))
        self.rule(item, 0, "Reject")
        self.hand_ruling(item, 1, "Approve")
        later = (datetime.now(UTC) + timedelta(days=8)).isoformat(timespec="seconds")
        result = review.apply(self.db, "nightly", who="tester", now=later)
        self.assertEqual((result["expired"], len(result["tasks"]), result["closed"]), ([], 1, [item]))
        self.assertEqual(review.read(self.db, item)["questions"][1]["label"], "Approve")

    def test_apply_leaves_a_batch_once_closed_and_reopened_alone(self):
        item = self.file(nightly("2026-10-01", ["cap-retries", "split-gate"]))
        self.rule(item, 0, "Approve")
        self.close(item)
        workflow.change_status(self.db, item, "planning", who="tester")
        before = self.counts()
        later = (datetime.now(UTC) + timedelta(days=8)).isoformat(timespec="seconds")
        self.assertEqual(review.apply(self.db, "nightly", who="tester", now=later),
                         {"tasks": [], "existing": [], "expired": [], "closed": []})
        self.assertEqual(self.counts(), before)

    def test_an_expiry_after_the_close_settles_nothing(self):
        item = self.file(batch())
        self.rule(item, 0, "cheap")
        self.close(item)
        note = review.read(self.db, item)["questions"][1]["note"]
        workflow.add_item_note(self.db, item, who="tester", body=f"On note #{note}: Expired, not ruled")
        found = review.read(self.db, item)
        self.assertEqual((found["open"], found["questions"][1]["expired"]), (1, False))


class ABenchmarkPlugIn(ReviewCase):
    """The contract a benchmark's labelling flow needs (the work satellite's survey, note #16318 on sd:3635).

    Cells keyed by their row id; one vocabulary; provenance on every label;
    labels that outlive a regrade; an export a kappa reads; LLM adjudication
    conflicts filed as questions. No code here is the benchmark's.
    """

    VERDICTS = ["keyword_correct", "rubric_correct", "both_wrong"]

    def conflicts(self, name: str, source: str, key: bytes) -> dict:
        return batch(queue="benchmark-conflicts", batch=name, title=f"Conflicts {name}", source=source,
                     mode="sealed", seal={"sha256": hashlib.sha256(key).hexdigest(), "name": f"key-{name}.csv"},
                     questions=[{"subject": row, "text": "Which grader read this cell right?",
                                 "evidence": ["grader: rubric v3, prompt v7", "adjudication: needs_human_review, low"],
                                 "options": self.VERDICTS} for row in ("row-0001", "row-0002", "row-0003")])

    def test_labels_keyed_by_row_id_export_with_provenance_and_feed_an_agreement_count(self):
        key = b"row_id,judge\nrow-0001,rubric_correct\nrow-0002,both_wrong\nrow-0003,rubric_correct\n"
        item = self.file(self.conflicts("matrix-0001", "bench@1a2b3c4", key))
        for index, verdict in enumerate(["rubric_correct", "keyword_correct", "rubric_correct"]):
            self.rule(item, index, verdict)
        manifest, *lines = review.export(self.db, item, key=key, who="tester")
        self.assertEqual(manifest["source"], "bench@1a2b3c4")
        self.assertEqual([line["subject"] for line in lines], ["row-0001", "row-0002", "row-0003"])
        for line in lines:
            self.assertEqual((line["ruled_by"], line["evidence"][0]), ("tester", "grader: rubric v3, prompt v7"))
            self.assertIsNotNone(line["ruled_at"])
        # Human against judge, joined by row id in one line each: what a kappa counts.
        pairs = [(line["label"], line["key"]["judge"]) for line in lines]
        self.assertEqual(sum(human == judge for human, judge in pairs), 2)

    def test_a_regrade_files_a_new_batch_and_the_old_labels_still_export(self):
        first_key = b"row_id,judge\nrow-0001,rubric_correct\nrow-0002,both_wrong\nrow-0003,rubric_correct\n"
        first = self.file(self.conflicts("matrix-0001", "bench@1a2b3c4", first_key))
        for index in range(3):
            self.rule(first, index, "rubric_correct")
        review.export(self.db, first, key=first_key, who="tester")
        second = self.file(self.conflicts("matrix-0002", "bench@5d6e7f8", b"row_id,judge\nrow-0001,keyword_correct\n"))
        self.assertNotEqual(first, second)
        # The regrade deletes nothing: the first batch's labels export unchanged.
        self.assertEqual([line["label"] for line in review.export(self.db, first, who="tester")[1:]],
                         ["rubric_correct"] * 3)
        self.assertEqual(review.read(self.db, second)["open"], 3)



def kev_line(ident: str, **changes) -> dict:
    """One line of a Kev label file, as Kev's tools/review reads it."""
    found: dict = {"id": ident, "document": f"attribute: {ident}\nscope: span", "source": "aura-field-audit",
             "label_origin": "blind",
             "question": {"type": "binary", "instructions": "Could this attribute carry free text?",
                          "options": {"payload": "can carry free text", "skeleton": "structural metadata"}},
             "proposed_label": "", "judges": [], "adjudication": None}
    found.update(changes)
    return found


def kev_text(lines) -> str:
    return "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines)


class TheKevImport(ReviewCase):
    """Kev's label JSONL as a Review batch (sd:4789): one question per line, `(drop)` as the last option."""

    def kev(self, lines, *, mode="blind", name="aura"):
        return review.from_kev(kev_text(lines), batch=name, mode=mode, source="kev:aura-name-labels.jsonl")

    def test_each_line_is_a_question_with_the_option_keys_then_drop(self):
        found = self.kev([kev_line("span:error.type"), "", kev_line("span:asgi.event.type")])
        self.assertEqual((found["queue"], found["batch"], found["title"], found["mode"]),
                         ("kev", "aura", "Label: aura", "blind"))
        first = found["questions"][0]
        self.assertEqual(first["subject"], "span:error.type")
        self.assertEqual(first["options"], ["payload", "skeleton", review.DROP])
        self.assertIn("payload: can carry free text", first["text"])
        self.assertTrue(first["text"].endswith("attribute: span:error.type\nscope: span"))
        self.assertEqual(first["evidence"], [])
        item = self.file(found)
        self.assertEqual(len(review.read(self.db, item)["questions"]), 2)

    def test_the_proposal_and_judges_are_evidence_lines(self):
        line = kev_line("span:a", proposed_label="payload", judges=[
            {"model": "example/kev-4b", "label": "skeleton", "rationale": "an\nidentifier"}])
        evidence = self.kev([line], mode="open")["questions"][0]["evidence"]
        self.assertEqual(evidence, ["Proposed label: payload", "Judge example/kev-4b: skeleton, an identifier"])

    def test_a_line_separator_inside_a_string_stays_in_its_record(self):
        # JSON allows a literal U+2028 inside a string; only a newline ends a JSONL record.
        raw = json.dumps(kev_line("a", document="a b"), ensure_ascii=False) + "\n"
        found = review.from_kev(raw, batch="aura", mode="blind", source="kev:x.jsonl")
        self.assertEqual(len(found["questions"]), 1)
        self.assertTrue(found["questions"][0]["text"].endswith("a b"))
        item = self.file(found)
        # The note reader splits lines as the filing check does, so it shows U+2028 as a line break.
        self.assertTrue(review.read(self.db, item)["questions"][0]["text"].endswith("a\nb"))

    def test_text_that_is_not_unicode_refuses_naming_the_line_and_files_nothing(self):
        # A JSON escape can carry a lone surrogate, which no UTF-8 write can store.
        before = self.counts()
        for name, line in {"id": kev_line("\ud800"), "document": kev_line("a", document="x\udfff"),
                           "option": kev_line("a", question={"instructions": "x", "options": {"\ud800": None}}),
                           "judge": kev_line("a", judges=[{"model": "m", "label": "\ud800"}])}.items():
            with self.subTest(name), self.assertRaises(review.ReviewError) as caught:
                self.file(self.kev([line]))
            self.assertIn("line 1", str(caught.exception))
        plain = batch()
        plain["questions"][0]["subject"] = "example.test/\ud800"
        with self.assertRaises(review.ReviewError):
            self.file(plain)
        self.assertEqual(self.counts(), before)

    def test_a_bad_line_refuses_the_file_naming_it_and_files_nothing(self):
        before = self.counts()
        cases: dict[str, list] = {
            "not JSON": ["{"],
            "duplicate": [kev_line("a"), kev_line("a")],
            "not an option": [kev_line("a", proposed_label="maybe")],
            "no options": [kev_line("a", question={"instructions": "x", "options": {}})],
            "too many options": [kev_line("a", question={"instructions": "x",
                                                         "options": {f"o{n}": None for n in range(9)}})],
            "a drop option": [kev_line("a", question={"instructions": "x", "options": {review.DROP: None}})],
            "options a list": [kev_line("a", question={"instructions": "x", "options": ["yes", "no"]})],
            "no document": [{"id": "a", "proposed_label": "", "question": {"options": {"y": None}}}],
            "long document": [kev_line("a", document="x" * 5000)],
            "too many judges": [kev_line("a", judges=[{"model": f"m{n}", "label": "payload"} for n in range(6)])],
            "empty": [],
        }
        for name, lines in cases.items():
            with self.subTest(name), self.assertRaises(review.ReviewError) as caught:
                self.file(self.kev([kev_line("ok"), *lines]) if lines else self.kev(lines))
            if lines:
                self.assertIn("line ", str(caught.exception))
        self.assertEqual(self.counts(), before)

    def test_extreme_input_files_or_refuses_and_never_truncates(self):
        # Unicode ids and documents, the widest option set, and a document at the size limit file whole.
        widest = {f"opción-{n}": "é" * 70 for n in range(8)}
        lines = [kev_line("span:日本語.名前", document="ünïcødé 🚀\n" * 3),
                 kev_line("span:wide", question={"instructions": "pick", "options": widest})]
        item = self.file(self.kev(lines))
        questions = review.read(self.db, item)["questions"]
        self.assertEqual(questions[0]["subject"], "span:日本語.名前")
        self.assertIn("ünïcødé 🚀", questions[0]["text"])
        self.assertEqual(questions[1]["options"], [*widest, review.DROP])
        # One byte over the 4 KiB question limit refuses rather than cuts.
        for size in (2000, 3800, 4096, 10 ** 6):
            with self.subTest(size=size):
                line = kev_line("span:long", document="d" * size)
                try:
                    found = self.kev([line], name=f"long{size}")
                except review.ReviewError as refused:
                    self.assertIn("line 1", str(refused))
                    continue
                self.assertIn("d" * size, found["questions"][0]["text"])

    def test_a_rejected_subject_in_the_queue_does_not_drop_a_kev_question(self):
        lines = [kev_line("span:a", question={"instructions": "x", "options": {"Reject": None, "Approve": None}})]
        first = self.file(self.kev(lines, name="one"))
        self.rule(first, 0, "Reject")
        second = review.file(self.db, self.kev(lines, name="two"), who="tester", suppress=False)
        self.assertEqual((second["filed"], second["dropped"]), (1, []))

    def test_filing_the_same_file_twice_returns_one_item(self):
        lines = [kev_line("span:a"), kev_line("span:b")]
        self.assertEqual(self.file(self.kev(lines)), self.file(self.kev(lines)))


class TheReviewsExport(ReviewCase):
    """`reviews(lines)`: an export in Kev's reviews.jsonl shape (sd:4789)."""

    def labelled(self, mode):
        lines = [kev_line("span:a", proposed_label="payload"), kev_line("span:b", proposed_label="payload"),
                 kev_line("span:c"), kev_line("span:d")]
        item = self.file(review.from_kev(kev_text(lines), batch="b", mode=mode, source="kev:t.jsonl"))
        self.rule(item, 0, "payload")
        self.rule(item, 1, "skeleton")
        self.rule(item, 2, review.DROP)
        note = review.read(self.db, item)["questions"][1]["note"]
        workflow.add_item_note(self.db, item, who="tester", body=f"On note #{note}: unsure")
        return review.reviews(review.export(self.db, item, who="tester"))

    def test_an_open_batch_reads_accept_relabel_and_drop(self):
        out = self.labelled("open")
        self.assertEqual([sorted(line) for line in out],
                         [["id", "label", "note", "proposed_label", "reviewed_at", "verdict"]] * 3)
        self.assertEqual([(line["id"], line["verdict"], line["label"], line["proposed_label"], line["note"])
                          for line in out],
                         [("span:a", "accept", "payload", "payload", ""),
                          ("span:b", "relabel", "skeleton", "payload", "unsure"),
                          ("span:c", "drop", None, "", "")])
        self.assertTrue(all(datetime.fromisoformat(line["reviewed_at"]).tzinfo for line in out))

    def test_a_blind_batch_never_reads_accept(self):
        self.assertEqual([line["verdict"] for line in self.labelled("blind")], ["relabel", "relabel", "drop"])

    def test_a_plain_batch_reads_with_no_proposal(self):
        item = self.file(batch())
        self.rule(item, 0, "cheap")
        self.assertEqual([(line["id"], line["verdict"], line["label"], line["proposed_label"])
                          for line in review.reviews(review.export(self.db, item, who="tester"))],
                         [("example.test/a@1a2b3c4:src/one.py", "relabel", "cheap", "")])

    def test_a_stop_before_the_rename_leaves_no_file_and_the_rerun_writes_it(self):
        lines = self.labelled("open")
        out = self.root / "reviews.jsonl"
        with mock.patch.object(review.os, "replace", side_effect=KeyboardInterrupt("stopped")), \
                self.assertRaises(KeyboardInterrupt):
            review.write(lines, out)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith("reviews")], [])
        review.write(lines, out)
        self.assertEqual([json.loads(line)["id"] for line in out.read_text().splitlines()],
                         ["span:a", "span:b", "span:c"])


class TheVerb(unittest.TestCase):
    """`bin/sd-db review` through the entrypoint, on a store under a temporary home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".local/share/sd").mkdir(parents=True)
        self.sd_db("init")

    def sd_db(self, *args, expect=0):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("PYTHONPATH", "XDG_STATE_HOME")}
        done = subprocess.run(support.sd_db_argv(*args), capture_output=True, text=True, input="",
                              env={**environment, "HOME": str(self.home), "PYTHONPATH": str(support.LIB)})
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        return done

    def store(self):
        connection = sqlite3.connect(self.home / ".local/share/sd/sd.db")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        return connection

    def write(self, name: str, found) -> Path:
        path = self.home / name
        path.write_text(found if isinstance(found, str) else json.dumps(found), encoding="utf-8")
        return path

    def test_file_then_export_then_apply(self):
        filed = self.sd_db("review", "file", str(self.write("batch.json", sealed())))
        self.assertRegex(filed.stdout, r"sd-db: filed fleet sd:\d+ ")
        again = self.sd_db("review", "file", str(self.write("batch.json", sealed())))
        self.assertIn("unchanged", again.stdout)
        item = self.store().execute("SELECT id FROM item WHERE external_id = 'review:jev-entry-2:C1'").fetchone()[0]
        out = self.home / "labels.jsonl"
        self.sd_db("review", "export", str(item), "--out", str(out))
        self.assertEqual(json.loads(out.read_text().splitlines()[0])["open"], 2)
        refused = self.sd_db("review", "export", str(item), "--key", str(self.write("key.csv", KEY_A.decode())),
                             "--out", str(out), expect=1)
        self.assertIn("2 questions", refused.stderr)
        self.assertIn("nothing to apply", self.sd_db("review", "apply", "nightly").stdout)

    def test_an_export_that_cannot_write_exits_1_and_the_rerun_writes(self):
        self.sd_db("review", "file", str(self.write("batch.json", sealed())))
        item = self.store().execute("SELECT id FROM item WHERE external_id = 'review:jev-entry-2:C1'").fetchone()[0]
        connection = connect(self.home / ".local/share/sd/sd.db")
        self.addCleanup(connection.close)
        for index, option in ((0, "cheap"), (1, "deep")):
            note = review.read(connection, item)["questions"][index]["note"]
            revision = workflow.item_state(connection, item)["revision"]
            review.rule(connection, item, note, option, expected_revision=revision, who="tester")
        key = str(self.write("key.csv", KEY_A.decode()))
        missing = self.home / "absent" / "labels.jsonl"
        refused = self.sd_db("review", "export", str(item), "--key", key, "--out", str(missing), expect=1)
        self.assertIn("is closed, and the same command writes it again", refused.stderr)
        out = self.home / "labels.jsonl"
        self.sd_db("review", "export", str(item), "--key", key, "--out", str(out))
        self.assertEqual(json.loads(out.read_text().splitlines()[1])["key"]["tier"], "cheap")

    def test_a_kev_file_files_blind_and_exports_as_reviews(self):
        path = self.write("aura.jsonl", kev_text([kev_line("span:a"), kev_line("span:b")]))
        filed = self.sd_db("review", "file", "--kev", str(path), "--batch", "aura", "--blind")
        self.assertRegex(filed.stdout, r"sd-db: filed fleet sd:\d+ ")
        row = self.store().execute("SELECT id FROM item WHERE external_id = 'review:kev:aura'").fetchone()
        self.assertIsNotNone(row)
        self.assertIn("unchanged", self.sd_db("review", "file", "--kev", str(path), "--batch", "aura",
                                               "--blind").stdout)
        connection = connect(self.home / ".local/share/sd/sd.db")
        self.addCleanup(connection.close)
        self.assertEqual(review.read(connection, row[0])["header"]["mode"], "blind")
        note = review.read(connection, row[0])["questions"][0]["note"]
        revision = workflow.item_state(connection, row[0])["revision"]
        review.rule(connection, row[0], note, "payload", expected_revision=revision, who="tester")
        out = self.home / "reviews.jsonl"
        self.sd_db("review", "export", str(row[0]), "--out", str(out), "--format", "reviews")
        self.assertEqual([json.loads(line)["id"] for line in out.read_text().splitlines()], ["span:a"])
        bad = self.write("bad.jsonl", kev_text([kev_line("span:a"), "{"]))
        refused = self.sd_db("review", "file", "--kev", str(bad), "--batch", "bad", expect=1)
        self.assertIn("line 2", refused.stderr)

    def test_invalid_json_refuses_and_files_nothing(self):
        refused = self.sd_db("review", "file", str(self.write("batch.json", '{"schema": 1, "queue": ')), expect=1)
        self.assertIn("not JSON", refused.stderr)
        self.assertEqual(self.store().execute("SELECT count(*) FROM item").fetchone()[0], 0)

    def test_a_bad_call_prints_the_usage_and_exits_1(self):
        for argv in (["review"], ["review", "file"], ["review", "export", "x", "--out", "y"],
                     ["review", "file", "--kev", "f.jsonl"], ["review", "export", "1", "--out", "y", "--format", "csv"],
                     ["review", "export", "1"], ["review", "apply"], ["review", "undo", "1"]):
            with self.subTest(argv=argv):
                self.assertIn("sd-db review", self.sd_db(*argv, expect=1).stderr)


if __name__ == "__main__":
    unittest.main()
