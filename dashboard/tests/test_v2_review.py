"""Scoring (sd:3709): the labelling queue, the Review page until sd:5781 and now Decide's Scoring tab.

What this slice promises: Decide (`/decide`, and `/review`, its old address) is a registered page in the rail's Now group
(`g l`); `/api/review` lists each open
batch, nightly suggestions first, and its open questions, and a blind or sealed batch sends no subject or evidence; a
batch whose notes do not read is listed with its reason. A label and its undo go through `sd_db.review.rule` and
`withdraw`, the code `sd-db review` runs, under the batch revision the page read (409 when stale). Today's Decisions panel
lists no batch question and counts them for one line that opens Scoring. `review.js` runs under JavaScriptCore
(osascript) against the stand-in page `test_v2_tasks` uses: the digits label and advance, u undoes, a failed read is
unknown and never an empty queue. The browser half (the widths, the themes) is recorded on the pull request.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import decisions, review_screen, v2
from sd_db import review, workflow
from support import ScreenCase, before_5648
from test_v2_registry import GROUP_BLOCK, Registers
from test_v2_tasks import MARKUP_JS, SHELL, STAND_IN
from test_v2_today import OSASCRIPT
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
STATIC = V2 / "static"
PAGE_JS = (STATIC / "review.js").read_text(encoding="utf-8") if (STATIC / "review.js").exists() else ""
PAGE_CSS = (STATIC / "review.css").read_text(encoding="utf-8") if (STATIC / "review.css").exists() else ""


def batch(name, *, queue="labels", mode="open", count=3, options=("yes", "no"), **extra):
    found = {"schema": 1, "queue": queue, "batch": name, "title": f"Label: {name}", "source": "system@1a2b3c4",
             "mode": mode, "questions": [{"subject": f"example.test/repo@1a2b3c4:file{n}.py",
                                          "text": f"Is file {n} right?\nRead it whole.",
                                          "evidence": [f"diff: git show 1a2b3c4 -- file{n}.py"],
                                          "options": list(options)} for n in range(1, count + 1)], **extra}
    if mode == "sealed":
        found["seal"] = {"sha256": "a" * 64, "name": "key.csv"}
    return found


def seed(case):
    """An open batch, a blind one, a nightly one filed last, a closed one and an item that only looks like a batch."""
    ids = {"open": review.file(case.connection, batch("C1"), who="test")["item"],
           "blind": review.file(case.connection, batch("B1", mode="blind", count=2), who="test")["item"],
           "nightly": review.file(case.connection, batch("2026-10-12", queue="nightly", count=2,
                                                         options=("Approve", "Reject")), who="test")["item"]}
    closed = review.file(case.connection, batch("D1", count=1), who="test")["item"]
    state = workflow.item_state(case.connection, closed)
    note = next(n["id"] for n in state["notes"] if n["kind"] == "question")
    state = review.rule(case.connection, closed, note, "yes", expected_revision=state["revision"], who="test")
    workflow.change_status(case.connection, closed, "done", who="test", reason="closed")
    ids["closed"] = closed
    broken = case.item("Not a batch")
    case.connection.execute("UPDATE item SET source = 'task-ref', external_id = 'review:labels:X9' WHERE id = ?", (broken,))
    case.connection.commit()
    case.note(broken, "Just a comment", kind="comment")
    ids["broken"] = broken
    return ids


def questions_of(connection, item):
    return [n["id"] for n in workflow.item_state(connection, item)["notes"] if n["kind"] == "question"]


class TheRegistration(Registers, unittest.TestCase):
    page, section, route = "decide", "Decide", "/decide"
    api = ("/api/decisions", "/api/decisions/answer", "/api/decisions/reverse", "/api/review", "/api/review/7/export",
           "/api/review/rule", "/api/review/withdraw")

    def test_decide_follows_today_in_now_with_reviews_chord_and_icon(self):
        now = re.search(r"\['Now',\s*\[(.*?)\]\],", GROUP_BLOCK).group(1)
        self.assertIn("['Today', 'sunrise', 't'], ['Decide', 'list-checks', 'l']", now)
        self.assertIn('<symbol id="i-list-checks"', (STATIC / "icons.js").read_text(encoding="utf-8"))


class TheDocument(ScreenCase):
    def test_open_batches_are_listed_nightly_first_with_their_open_questions(self):
        ids = seed(self)
        doc = review_screen.document(self.connection)
        self.assertEqual([b["item"] for b in doc["batches"]], [ids["nightly"], ids["open"], ids["blind"], ids["broken"]])
        opened = doc["batches"][1]
        self.assertEqual((opened["queue"], opened["name"], opened["mode"], opened["questions"], opened["open"], opened["labels"]),
                         ("labels", "C1", "open", 3, 3, 0))
        self.assertEqual(opened["revision"], workflow.item_state(self.connection, ids["open"])["revision"])
        self.assertTrue(doc["batches"][0]["nightly"])
        self.assertEqual([q["item"] for q in doc["questions"]],
                         [ids["nightly"]] * 2 + [ids["open"]] * 3 + [ids["blind"]] * 2)
        first = doc["questions"][2]
        self.assertEqual((first["text"], first["options"], first["subject"], first["evidence"]),
                         ("Is file 1 right?\nRead it whole.", ["yes", "no"], "example.test/repo@1a2b3c4:file1.py",
                          ["diff: git show 1a2b3c4 -- file1.py"]))

    def test_a_scheduled_batch_is_not_listed_until_it_opens(self):
        """sd:5670: the row set is Tasks', so a scheduled occurrence (pack sd:5648) is out until it opens; this read takes
        the real clock, so the due day is far off. A pack before sd:5648 lists it as before."""
        ids = seed(self)
        self.schedule(ids["open"], "2999-01-01")
        self.assertNotIn(ids["open"], [b["item"] for b in review_screen.document(self.connection)["batches"]])
        with before_5648():
            self.assertIn(ids["open"], [b["item"] for b in review_screen.document(self.connection)["batches"]])

    def test_the_rail_badge_counts_every_open_question_in_a_batch_that_reads(self):
        # sd:3446: the rule every page's rail shows; the broken batch counts nothing, a blind one counts its questions.
        ids = seed(self)
        self.assertEqual(review_screen.document(self.connection)["attention"], {"state": "caution", "n": 7, "what": "questions open"})
        note = questions_of(self.connection, ids["open"])[0]
        revision = workflow.item_state(self.connection, ids["open"])["revision"]
        review_screen.rule_request({"item": ids["open"], "note": note, "option": "no", "revision": revision})(self.connection)
        self.assertEqual(review_screen.document(self.connection)["attention"]["n"], 6)

    def test_a_blind_batch_sends_no_subject_and_no_evidence(self):
        ids = seed(self)
        doc = review_screen.document(self.connection)
        blind = [q for q in doc["questions"] if q["item"] == ids["blind"]]
        self.assertEqual(len(blind), 2)
        for q in blind:
            self.assertEqual(set(q), {"item", "note", "text", "options", "asked"})
        self.assertNotIn("example.test", json.dumps(blind))

    def test_a_batch_that_does_not_read_is_listed_with_its_reason(self):
        ids = seed(self)
        broken = [b for b in review_screen.document(self.connection)["batches"] if b["item"] == ids["broken"]]
        self.assertEqual(len(broken), 1)
        self.assertIn("is not a review batch", broken[0]["error"])

    def test_a_labelled_question_leaves_the_list_and_its_withdrawal_brings_it_back(self):
        ids = seed(self)
        note = questions_of(self.connection, ids["open"])[0]
        revision = workflow.item_state(self.connection, ids["open"])["revision"]
        out = review_screen.rule_request({"item": ids["open"], "note": note, "option": "no", "revision": revision})(self.connection)
        doc = review_screen.document(self.connection)
        self.assertNotIn(note, [q["note"] for q in doc["questions"]])
        self.assertEqual(next(b for b in doc["batches"] if b["item"] == ids["open"])["labels"], 1)
        self.assertEqual(review.read(self.connection, ids["open"])["questions"][0]["label"], "no")
        review_screen.withdraw_request({"item": ids["open"], "ruling": out["ruling"], "revision": out["revision"]})(self.connection)
        self.assertIn(note, [q["note"] for q in review_screen.document(self.connection)["questions"]])
        bodies = [n["body"] for n in workflow.item_state(self.connection, ids["open"])["notes"] if n["kind"] == "decision"]
        self.assertEqual(bodies, [f"Ruling on note #{note}: no", f"Withdrawn: ruling #{out['ruling']} on note #{note}"])

    def test_a_ruled_question_is_listed_as_reviewed_with_its_label(self):
        # sd:5123: Show reviewed lists the ruled questions of an open batch, each with the choice made and its ruling, in
        # their own list: `questions` stays open-only, as a page on the previous build reads it.
        ids = seed(self)
        first, second = questions_of(self.connection, ids["open"])[:2]
        revision = workflow.item_state(self.connection, ids["open"])["revision"]
        out = review_screen.rule_request({"item": ids["open"], "note": first, "option": "no", "revision": revision})(self.connection)
        doc = review_screen.document(self.connection)
        self.assertEqual([(q["note"], q["open"], q["label"], q["ruling"]) for q in doc["reviewed"]], [(first, False, "no", out["ruling"])])
        self.assertEqual(doc["reviewed"][0]["subject"], "example.test/repo@1a2b3c4:file1.py")
        self.assertIn(second, [q["note"] for q in doc["questions"]])
        self.assertNotIn("label", doc["questions"][0])

    def test_nothing_open_is_an_empty_document(self):
        self.assertEqual(review_screen.document(self.connection), {"batches": [], "questions": [], "reviewed": [],
                                                                   "attention": {"state": "ok", "n": 0, "what": "questions open"}})


#: What a pack with pack sd:5763 reads from a batch filed with a context block and a recommendation (system sd:5759).
ADVICE = {"option": "no", "reason": "the diff only renames"}
CONTEXT = {"purpose": "Calibrate the file check.", "rubric": ["yes: the file reads right", "no: it does not"],
           "feeds": "the file-check eval"}


def advised():
    """`review.read` as a pack with pack sd:5763 answers it: a context block and one recommendation on every question.

    The gate's pinned pack may predate those fields, so the page's filter is tested on the read it serves; the pack's own
    suite tests the note format and the agreement."""
    real = review.read

    def read(connection, item):
        found = real(connection, item)
        found["header"]["context"] = CONTEXT
        for question in found["questions"]:
            question["recommendation"] = dict(ADVICE)
            question["agreed"] = None if question["label"] is None else question["label"] == ADVICE["option"]
        return found

    return mock.patch.object(review, "read", read)


def blind_batches(case):
    """A blind batch and a sealed one, two questions each."""
    return {"blind": review.file(case.connection, batch("B1", mode="blind", count=2), who="test")["item"],
            "sealed": review.file(case.connection, batch("S1", mode="sealed", count=2), who="test")["item"]}


class TheRecommendation(ScreenCase):
    """sd:5759: each batch's context block, and each question's recommendation, which a blind batch holds back until it is
    ruled."""

    def test_every_batch_sends_its_context_whatever_its_mode(self):
        ids = seed(self)
        with advised():
            batches = {b["item"]: b for b in review_screen.document(self.connection)["batches"]}
        for name in ("open", "blind", "nightly"):
            self.assertEqual(batches[ids[name]]["context"], CONTEXT, name)

    def test_an_open_batch_sends_each_recommendation_before_the_ruling(self):
        ids = seed(self)
        with advised():
            doc = review_screen.document(self.connection)
        opened = [q for q in doc["questions"] if q["item"] == ids["open"]]
        self.assertEqual([q["recommendation"] for q in opened], [ADVICE] * 3)
        self.assertNotIn("agreed", opened[0])

    def test_a_blind_or_sealed_question_sends_its_recommendation_only_once_ruled(self):
        ids = blind_batches(self)
        for mode in ("blind", "sealed"):
            with self.subTest(mode):
                item = ids[mode]
                first, second = questions_of(self.connection, item)
                with advised():
                    doc = review_screen.document(self.connection)
                asked = [q for q in doc["questions"] if q["item"] == item]
                self.assertEqual([set(q) for q in asked], [{"item", "note", "text", "options", "asked"}] * 2)
                self.assertNotIn(ADVICE["reason"], json.dumps(doc))
                revision = workflow.item_state(self.connection, item)["revision"]
                out = review_screen.rule_request({"item": item, "note": first, "option": "yes", "revision": revision})(self.connection)
                with advised():
                    doc = review_screen.document(self.connection)
                ruled = next(q for q in doc["reviewed"] if q["note"] == first)
                self.assertEqual((ruled["recommendation"], ruled["agreed"]), (ADVICE, False))
                self.assertEqual([q["note"] for q in doc["questions"] if q["item"] == item and "recommendation" in q], [])
                # An undo reopens the question, which holds its recommendation back again.
                review_screen.withdraw_request({"item": item, "ruling": out["ruling"], "revision": out["revision"]})(self.connection)
                # An expired question was never ruled, so it shows none either.
                self.assertTrue(review._expire(self.connection, item, second, "test"))
                with advised():
                    doc = review_screen.document(self.connection)
                self.assertNotIn(ADVICE["reason"], json.dumps(doc))

    def test_the_label_reply_names_the_recommendation_and_whether_it_agreed(self):
        ids = blind_batches(self)
        note = questions_of(self.connection, ids["blind"])[0]
        revision = workflow.item_state(self.connection, ids["blind"])["revision"]
        with advised():
            out = review_screen.rule_request({"item": ids["blind"], "note": note, "option": "no", "revision": revision})(self.connection)
        self.assertEqual((out["recommendation"], out["agreed"]), (ADVICE, True))

    def test_a_read_that_fails_after_the_label_still_answers_the_label(self):
        # The label committed in `review.rule`; a failed read of its recommendation must not turn it into "not labelled".
        ids = blind_batches(self)
        note = questions_of(self.connection, ids["blind"])[0]
        revision = workflow.item_state(self.connection, ids["blind"])["revision"]
        with mock.patch.object(review, "read", side_effect=sqlite3.OperationalError("disk I/O error")):
            out = review_screen.rule_request({"item": ids["blind"], "note": note, "option": "no", "revision": revision})(self.connection)
        self.assertEqual((out["recommendation"], out["agreed"]), (None, None))
        self.assertEqual(review.read(self.connection, ids["blind"])["questions"][0]["label"], "no")

    def test_a_question_with_no_recommendation_sends_none(self):
        # The pack as pinned, or a batch filed with neither field: no key, and the batch's context is None.
        ids = seed(self)
        doc = review_screen.document(self.connection)
        self.assertEqual({b.get("context") for b in doc["batches"] if b["item"] != ids["broken"]}, {None})
        self.assertFalse(any("recommendation" in q for q in doc["questions"]))
        note = questions_of(self.connection, ids["open"])[0]
        revision = workflow.item_state(self.connection, ids["open"])["revision"]
        out = review_screen.rule_request({"item": ids["open"], "note": note, "option": "no", "revision": revision})(self.connection)
        self.assertEqual((out["recommendation"], out["agreed"]), (None, None))


class TheExport(ScreenCase):
    def test_an_open_batch_exports_the_lines_sd_db_review_export_writes(self):
        ids = seed(self)
        out = review_screen.export(self.connection, ids["open"])
        self.assertEqual(out["name"], "review-labels-C1.jsonl")
        self.assertEqual(out["lines"][0]["kind"], "manifest")
        self.assertEqual(len(out["lines"]), 4)
        expected = review.export(self.connection, ids["open"], who="test")
        for line in (out["lines"][0], expected[0]):
            line.pop("exported_at")
        self.assertEqual(out["lines"], expected)

    def test_a_blind_batch_with_an_open_question_and_an_item_that_is_no_batch_are_refused(self):
        ids = seed(self)
        status, body = review_screen.export(self.connection, ids["blind"])
        self.assertEqual(status, 409)
        self.assertIn("has 2 open", body["error"])
        self.assertEqual(review_screen.export(self.connection, ids["broken"])[0], 400)
        self.assertEqual(review_screen.export(self.connection, 999999)[0], 404)


class TheRoutes(BrowserSession):
    def test_a_label_lands_once_a_second_on_the_old_revision_is_stale_and_undo_reopens(self):
        ids = seed(self)
        status, _, body = self.request("/api/review", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        doc = json.loads(body)
        q = next(q for q in doc["questions"] if q["item"] == ids["open"])
        revision = next(b["revision"] for b in doc["batches"] if b["item"] == ids["open"])
        payload = {"item": ids["open"], "note": q["note"], "option": "yes", "revision": revision}
        status, _, out = self.post("/api/review/rule", payload)
        self.assertEqual(status, 200, out)
        self.assertEqual(set(out), {"item", "note", "revision", "ruling", "recommendation", "agreed"})
        status, _, refused = self.post("/api/review/rule", payload)
        self.assertEqual((status, refused.get("reload")), (409, True))
        status, _, pulled = self.post("/api/review/withdraw", {"item": ids["open"], "ruling": out["ruling"], "revision": out["revision"]})
        self.assertEqual(status, 200, pulled)
        self.assertTrue(review.read(self.connection, ids["open"])["questions"][0]["open"])
        status = self.request("/api/review/%d/export" % ids["open"], headers={"Cookie": self.cookie})[0]
        self.assertEqual(status, 200)

    def test_a_blind_batch_never_serves_a_recommendation_before_its_ruling(self):
        # sd:5759: the server's reply, not the page, holds it back: the bytes /api/review sends name no recommendation.
        ids = blind_batches(self)
        with advised():
            status, _, body = self.request("/api/review", headers={"Cookie": self.cookie})
            self.assertEqual(status, 200)
            self.assertNotIn(ADVICE["reason"], body)
            self.assertNotIn("recommendation", body)
            doc = json.loads(body)
            q = next(q for q in doc["questions"] if q["item"] == ids["sealed"])
            revision = next(b["revision"] for b in doc["batches"] if b["item"] == ids["sealed"])
            status, _, out = self.post("/api/review/rule", {"item": ids["sealed"], "note": q["note"], "option": "no", "revision": revision})
            self.assertEqual(status, 200, out)
            self.assertEqual((out["recommendation"], out["agreed"]), (ADVICE, True))
            body = self.request("/api/review", headers={"Cookie": self.cookie})[2]
        self.assertEqual(body.count(ADVICE["reason"]), 1)
        self.assertEqual([r["note"] for r in json.loads(body)["reviewed"]], [q["note"]])

    def test_a_closed_batch_refuses_a_label(self):
        ids = seed(self)
        note = questions_of(self.connection, ids["closed"])[0]
        revision = workflow.item_state(self.connection, ids["closed"])["revision"]
        before = self.snapshot()
        status, _, out = self.post("/api/review/rule", {"item": ids["closed"], "note": note, "option": "no", "revision": revision})
        self.assertEqual(status, 400, out)
        self.assertIn("closed", out["error"])
        self.assertEqual(self.snapshot(), before)

    def test_a_body_out_of_shape_is_refused_before_the_database_opens(self):
        good = {"item": 3, "note": 4, "option": "yes", "revision": "a" * 64}
        for payload in ({k: v for k, v in good.items() if k != "revision"}, {**good, "revision": "x"}, {**good, "option": ""},
                        {**good, "note": "4"}, {**good, "item": 0}, {**good, "note": 2 ** 63}, {**good, "who": "me"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post("/api/review/rule", payload)[0], 400)
        for payload in ({"item": 3, "ruling": 4}, {"item": 3, "ruling": True, "revision": "a" * 64}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post("/api/review/withdraw", payload)[0], 400)


class TodaysDecisions(ScreenCase):
    def test_batch_questions_are_not_decision_rows_and_are_counted_for_review(self):
        ids = seed(self)
        plain = self.item("Plain work")
        asked = self.note(plain, "Which?\nOption: A\nOption: B", kind="question")
        doc = decisions.document(self.connection)
        self.assertEqual([d["note"] for d in doc["decisions"]], [asked])
        self.assertEqual(doc["review"], 7)
        self.assertEqual(ids["open"] in [d["item"] for d in doc["decisions"]], False)

    def test_the_batch_ref_today_names_is_the_one_review_files_under(self):
        self.assertEqual(decisions.BATCH_SOURCE, review.REF_SOURCE)
        self.assertTrue("review:labels:C1".startswith(decisions.BATCH_PREFIX))


class ThePage(unittest.TestCase):
    def test_scoring_is_its_own_view_and_decide_owns_the_decisions(self):
        # sd:5651: Review's Questions view drew Today's Decisions rows again. sd:5781: Decisions and Scoring are Decide's
        # two views; review.js draws Scoring alone and declares no views of its own.
        page = (V2 / "decide.html").read_text(encoding="utf-8")
        self.assertNotIn("PAGE_VIEWS", PAGE_JS)
        self.assertNotIn('id="view-questions"', page)
        scoring = page[page.index('<div id="view-scoring"'):]
        self.assertLess(scoring.index('id="annunciator"'), scoring.index('<tbody id="rows">'))
        self.assertIn('<link rel="stylesheet" href="/ui/decisions.css">', page)
        self.assertNotIn("decisions.css", (V2 / "today.html").read_text(encoding="utf-8"))
        # One home for the decision rows' look: Today's sheet no longer carries them.
        self.assertNotIn(".decisions .choices", (STATIC / "today.css").read_text(encoding="utf-8"))
        self.assertIn(".decisions .choices", (STATIC / "decisions.css").read_text(encoding="utf-8"))

    def test_the_page_has_no_details_pane_and_the_shell_pane_is_chat(self):
        # sd:5123: the row carries the question. sd:5592: with no Details element the shell docks Chat alone.
        page = (V2 / "decide.html").read_text(encoding="utf-8")
        self.assertNotIn('id="details"', page)
        self.assertNotIn("#tab-details", PAGE_CSS)
        self.assertNotIn(".pane", PAGE_CSS)
        self.assertNotIn("openPane", PAGE_JS)


class TheStylesheet(unittest.TestCase):
    def test_review_css_names_tokens_and_no_colour_literal(self):
        self.assertTrue(PAGE_CSS)
        self.assertNotRegex(PAGE_CSS, r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla|oklch|oklab|lab|lch|color-mix)\(")
        tokens = set(re.findall(r"^\s*(--[a-z0-9-]+):", (STATIC / "tokens.css").read_text(encoding="utf-8"), re.M))
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", PAGE_CSS))
        self.assertEqual(used - tokens, set())


def question(note, item=11, **changes):
    return {"item": item, "note": note, "text": f"Question {note}?\nmore", "options": ["Approve", "Reject", "Later <b>"],
            "asked": "2026-10-12T15:00:00Z", **changes}


def doc_with(n=3, **batch_changes):
    return {"batches": [{"item": 11, "title": "Nightly review", "created": "2026-10-12T15:00:00Z", "ref": "review:nightly:x",
                         "queue": "nightly", "name": "x", "mode": "open", "source": "s", "nightly": True,
                         "revision": "a" * 64, "questions": n, "open": n, "labels": 0, "error": None, **batch_changes}],
            "questions": [question(10 + i) for i in range(n)]}


class TheScript(unittest.TestCase):
    """review.js against a stand-in page: the lamps, the rows, the digits, Undo and the failed read."""

    def run_page(self, body, answer, search="", doc=None, prefix=""):
        script = (STAND_IN + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL
                  + "\nlocation.search = " + json.dumps(search) + ";\nvar DOC = " + json.dumps(doc) + ";\nANSWER = " + answer + ";\n"
                  + "window.shell.commands.select = id => { OUT.selected = id; };\n" + prefix
                  + "var BareEl = El; El = id => { const e = BareEl(id); e.click = () => { (OUT.saved = OUT.saved || []).push(e.download); }; return e; };\n"
                  + PAGE_JS
                  + "\nvar R = {};\nconst key = k => (DOC_LISTENERS.keydown || []).forEach(f => f({ key: k, target: { matches: () => false }, preventDefault() {} }));\n"
                  + "(async () => { try {\n(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    RULE = """(path, body) => path === '/api/review' ? [200, DOC]
      : path === '/api/review/rule' ? [200, { item: body.item, note: body.note, revision: String(body.note).padStart(64, '0'), ruling: 100 + body.note }]
      : [200, { item: 11, revision: 'c'.repeat(64), withdrawal: 500 }]"""

    def test_the_lamps_and_rows_draw_and_values_stay_text(self):
        out = self.run_page("R.lamps = ELS.annunciator.html; R.rows = ELS.rows.html; R.sub = ELS.sub.textContent;",
                            "(path, body) => [200, DOC]", doc=doc_with())
        self.assertEqual(out["gets"], ["/api/review"])
        self.assertIn("Nightly review", out["R"]["lamps"])
        self.assertIn("<b>3</b> of 3 open", out["R"]["lamps"])
        self.assertEqual(out["R"]["rows"].count('<tr data-id="q'), 3)
        self.assertNotIn("<b>", out["R"]["rows"])
        self.assertRegex(out["R"]["sub"], r"^0 labelled · 3 open · 0 skipped · read ")

    def test_a_question_is_the_shells_selection_only_while_scoring_shows(self):
        # sd:5781 review: Scoring reads beside Decisions; a selection it made while hidden took Enter and a note there.
        out = self.run_page("R.hidden = OUT.selected; VIEW = 'scoring'; view(); R.shown = OUT.selected;"
                            " VIEW = 'decisions'; view(); R.left = OUT.selected;",
                            "(path, body) => [200, DOC]", doc=doc_with(),
                            prefix="var VIEW = 'decisions'; window.shell.view.current = () => VIEW;"
                                   " const view = () => document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: VIEW } }));\n")
        self.assertEqual((out["R"].get("hidden"), out["R"].get("shown"), out["R"].get("left")), (None, "q10", None))

    def test_a_batch_that_did_not_read_links_its_lamp_to_the_item(self):
        # sd:5298: an unknown lamp links to its evidence; for a batch that is its sd item, where the batch is filed.
        out = self.run_page("R.lamps = ELS.annunciator.html;", "(path, body) => [200, DOC]",
                            doc=doc_with(error="item 11 is not a review batch"))
        self.assertRegex(out["R"]["lamps"], r'<a class="cell[^"]*" href="/tasks\?row=11"[^>]*data-state="unknown"')

    def test_each_row_carries_its_question_context_and_one_button_per_option(self):
        # sd:5123: no Details pane; the row holds the question, its subject and evidence, and the options as buttons.
        doc = doc_with(2)
        doc["questions"][0].update(subject="span:error.type", evidence=["Judge: skeleton, an enum"])
        out = self.run_page("R.rows = ELS.rows.html; R.details = 'details' in ELS;", "(path, body) => [200, DOC]", doc=doc)
        rows = out["R"]["rows"]
        self.assertFalse(out["R"]["details"])  # the page never reads a Details element
        self.assertIn('<p class="ask">Question 10?</p>', rows)
        self.assertIn('<span class="subject">span:error.type</span>', rows)
        self.assertIn("Judge: skeleton, an enum", rows)
        self.assertIn('data-note="10" data-i="2" aria-keyshortcuts="3"><kbd>3</kbd><span>Later &lt;b&gt;</span></button>', rows)
        self.assertEqual(rows.count('class="btn quiet sm opt"'), 6)
        # The whole question opens in place, in the row.
        self.assertIn('<details class="more"><summary>Whole question</summary><p class="full">Question 10?\nmore</p>', rows)

    def test_the_row_marks_the_proposed_option_in_an_open_batch_only(self):
        # A Kev file's proposal is an evidence line (sd:4789); its digit is the accept. sd:5497: the row says it plainly too.
        for mode, marked in (("open", True), ("blind", False)):
            doc = doc_with(1, mode=mode)
            doc["questions"][0]["evidence"] = ["Proposed label: Reject"]
            out = self.run_page("R.rows = ELS.rows.html;", "(path, body) => [200, DOC]", doc=doc)
            with self.subTest(mode):
                self.assertEqual('<span>Reject</span><span class="tag">recommended</span>' in out["R"]["rows"], marked)
                self.assertEqual(out["R"]["rows"].count('class="tag">recommended'), int(marked))
                self.assertEqual('<p class="rec">Recommended: <b>Reject</b></p>' in out["R"]["rows"], marked)

    def test_a_band_carries_its_batch_context_and_values_stay_text(self):
        # sd:5759: purpose, rubric and what the labels feed, above the batch's questions; a batch with none draws none.
        doc = doc_with(2, context={"purpose": "Calibrate <b>", "rubric": ["skip: docs only", "deep: touches state"],
                                   "feeds": "the router"})
        doc["batches"].append({**doc["batches"][0], "item": 12, "title": "Label: C1", "context": None})
        doc["questions"].append(question(30, item=12))
        rows = self.run_page("R.rows = ELS.rows.html;", "(path, body) => [200, DOC]", doc=doc)["R"]["rows"]
        self.assertIn('<dl class="brief" data-tkeep=""><dt>Purpose</dt><dd>Calibrate &lt;b&gt;</dd><dt>Rubric</dt>'
                      '<dd><ul><li>skip: docs only</li><li>deep: touches state</li></ul></dd><dt>Feeds</dt><dd>the router</dd></dl>', rows)
        self.assertEqual(rows.count('class="brief"'), 1)
        self.assertLess(rows.index('class="brief"'), rows.index('data-id="q10"'))

    def test_an_open_rows_recommendation_shows_its_reason_marks_its_option_and_bulk_accepts_it(self):
        doc = doc_with(2)
        doc["questions"][0]["recommendation"] = {"option": "Reject", "reason": "a repeat of an earlier suggestion"}
        out = self.run_page(TheBulkView.PICK + "R.rows = ELS.rows.html; bar('#select-all'); R.bar = ELS.bulk.html; bar('#accept-rec'); await settle();",
                            TheBulkView.BULK_RULE.replace("body.note === REFUSE", "false").replace("body.note === STALE", "false")
                            .replace("body.note === LOST", "false"), doc=doc)
        rows = out["R"]["rows"]
        self.assertIn('<p class="rec">Recommended: <b>Reject</b> · a repeat of an earlier suggestion</p>', rows)
        self.assertIn('<span>Reject</span><span class="tag">recommended</span>', rows)
        self.assertIn('id="accept-rec">Accept recommendation <span class="n">1</span></button>', out["R"]["bar"])
        self.assertEqual([(p[1]["note"], p[1]["option"]) for p in out["posts"] if p[0] == "/api/review/rule"], [(10, "Reject")])

    def test_a_ruled_rows_recommendation_says_whether_the_label_matched(self):
        # A blind batch sends it only once ruled (review_screen); the row then says it and how the label compares.
        doc = doc_with(3, mode="blind", open=1, labels=2)
        doc["reviewed"] = [{**doc["questions"].pop(), "open": False, "label": "Reject", "ruling": 90,
                            "recommendation": {"option": "Approve", "reason": "small change"}, "agreed": False},
                           {**doc["questions"].pop(), "open": False, "label": "Approve", "ruling": 91,
                            "recommendation": {"option": "Approve", "reason": "small change"}, "agreed": True}]
        rows = self.run_page("R.rows = ELS.rows.html;", "(path, body) => [200, DOC]", search="?reviewed=1", doc=doc)["R"]["rows"]
        self.assertIn('<p class="rec">Recommended: <b>Approve</b> · small change · <span class="agree">differs from your label</span></p>', rows)
        self.assertIn('<p class="rec">Recommended: <b>Approve</b> · small change · <span class="agree">matches your label</span></p>', rows)
        # The open blind row says nothing of a recommendation.
        self.assertEqual(rows.split('data-id="q10"')[1].split("</tr>")[0].count('class="rec'), 0)

    def test_a_blind_label_reply_shows_its_recommendation_in_the_toast_and_the_row(self):
        out = self.run_page("key('1'); await flush(); R.rows = ELS.rows.html;",
                            """(path, body) => path === '/api/review' ? [200, DOC]
                              : [200, { item: 11, note: body.note, revision: 'b'.repeat(64), ruling: 110,
                                        recommendation: { option: 'Reject', reason: 'seen before' }, agreed: false }]""",
                            search="?reviewed=1", doc=doc_with(2, mode="blind"))
        self.assertEqual(out["toasts"][-1], ["Labelled note #10: Approve · recommended Reject", True])
        self.assertIn('<p class="rec">Recommended: <b>Reject</b> · seen before · <span class="agree">differs from your label</span></p>',
                      out["R"]["rows"])

    def test_a_row_with_no_proposal_says_it_has_no_recommendation(self):
        out = self.run_page("R.rows = ELS.rows.html;", "(path, body) => [200, DOC]", doc=doc_with(1))
        self.assertIn('<p class="rec none">No recommendation</p>', out["R"]["rows"])

    # A click on a row's option: the event's target is the button, whose data-note and data-i name the question and option.
    CLICK = ("const click = (sel, data) => ELS.rows.listeners.click.forEach(f => f({ target: { closest: s => s === sel"
             " ? { dataset: data, disabled: false } : null } }));\n")

    def test_one_click_on_a_rows_option_labels_that_question_hides_it_and_moves_on(self):
        out = self.run_page(self.CLICK + "ELS.rows.querySelector = s => ({ focus() { (OUT.focused = OUT.focused || []).push(s); }, scrollIntoView() {} });"
                            " click('button[data-i]', { note: '11', i: '1' }); await flush(); R.rows = ELS.rows.html;",
                            self.RULE, doc=doc_with(3))
        self.assertEqual([p[1]["note"] for p in out["posts"]], [11])
        self.assertEqual(out["posts"][0][1]["option"], "Reject")
        self.assertNotIn('data-id="q11"', out["R"]["rows"])
        self.assertEqual(out["R"]["rows"].count('<tr data-id="q'), 2)
        self.assertEqual(out["selected"], "q12")
        # Focus follows to the next question's first option, so the next click or Tab starts there.
        self.assertIn('tr[data-id="q12"] .opt', out["focused"])
        self.assertEqual(out["toasts"][-1], ["Labelled note #11: Reject", True])

    def test_undo_after_a_click_brings_focus_back_to_the_reopened_question(self):
        # Focus in the ledger follows the selection, so the focused button and a digit name the same question.
        out = self.run_page(self.CLICK + "const focused = [];"
                            " ELS.rows.contains = () => true; ELS.rows.querySelector = s => ({ contains: () => false, scrollIntoView() {},"
                            " focus() { focused.push(s); }, querySelector: t => ({ focus() { focused.push(s + ' ' + t); } }) });"
                            " click('button[data-i]', { note: '10', i: '0' }); await flush(); OUT.toasts[OUT.toasts.length - 1].undo(); await flush();"
                            " R.focused = focused;",
                            """(path, body) => path === '/api/review/rule' ? [200, { item: 11, note: body.note, revision: 'b'.repeat(64), ruling: 110 }]
                              : path === '/api/review/withdraw' ? [200, { item: 11, revision: 'c'.repeat(64), withdrawal: 111 }] : [200, JSON.parse(JSON.stringify(DOC))]""",
                            doc=doc_with(3))
        self.assertEqual(out["selected"], "q10")
        self.assertEqual(out["R"]["focused"][-1], 'tr[data-id="q10"] .opt, [data-withdraw], summary')

    def reviewed_doc(self):
        doc = doc_with(3, open=2, labels=1)
        doc["reviewed"] = [{**doc["questions"].pop(), "open": False, "label": "Reject", "ruling": 90}]
        return doc

    def test_a_reviewed_row_is_hidden_until_show_reviewed_brings_it_back_marked(self):
        out = self.run_page("R.rows = ELS.rows.html; R.tally = ELS.tally.html; window.shell.url = p => { OUT.url = p.toString(); };"
                            " ELS.tally.listeners.click[0]({ target: { closest: s => s === '#show-reviewed' ? {} : null } });"
                            " R.after = ELS.rows.html; R.pressed = ELS.tally.html;",
                            "(path, body) => [200, DOC]", doc=self.reviewed_doc())
        self.assertEqual(out["R"]["rows"].count('<tr data-id="q'), 2)
        self.assertNotIn('data-id="q12"', out["R"]["rows"])
        self.assertIn('id="show-reviewed" aria-pressed="false">Show reviewed <span class="n">1</span></button>', out["R"]["tally"])
        self.assertEqual(out["R"]["after"].count('<tr data-id="q'), 3)
        self.assertIn('<tr data-id="q12" data-reviewed=""', out["R"]["after"])
        self.assertIn('<p class="chose">Labelled <b>Reject</b>', out["R"]["after"])
        self.assertIn('aria-pressed="true"', out["R"]["pressed"])
        self.assertEqual(out["url"], "reviewed=1")

    def test_show_reviewed_from_the_address_and_a_label_keeps_its_row_marked(self):
        out = self.run_page("key('2'); await flush(); R.rows = ELS.rows.html;", self.RULE, search="?reviewed=1", doc=self.reviewed_doc())
        self.assertEqual([p[1]["note"] for p in out["posts"]], [10])
        self.assertIn('<tr data-id="q10" data-reviewed=""', out["R"]["rows"])
        self.assertIn('<p class="chose">Labelled <b>Reject</b>', out["R"]["rows"].split('data-id="q11"')[0])
        self.assertEqual(out["selected"], "q11")

    def test_withdraw_on_a_reviewed_row_posts_its_ruling_and_reopens_it(self):
        out = self.run_page(self.CLICK + "click('button[data-withdraw]', { withdraw: '12' }); await flush();",
                            self.RULE, search="?reviewed=1", doc=self.reviewed_doc())
        self.assertEqual(out["posts"], [["/api/review/withdraw", {"item": 11, "ruling": 90, "revision": "a" * 64}, 64]])
        self.assertEqual(out["selected"], "q12")

    def test_enter_opens_the_selected_rows_whole_question_in_place(self):
        out = self.run_page("document.dispatchEvent(new CustomEvent('shell:open', { detail: 'q10' })); R.rows = ELS.rows.html;",
                            "(path, body) => [200, DOC]", doc=doc_with(2))
        self.assertIn('data-id="q10"', out["R"]["rows"])
        self.assertEqual(out["R"]["rows"].count('<details class="more" open>'), 1)
        self.assertLess(out["R"]["rows"].index('<details class="more" open>'), out["R"]["rows"].index('data-id="q11"'))

    def test_ten_digits_write_ten_labels_in_turn_and_u_withdraws_the_last(self):
        doc = doc_with(10)
        out = self.run_page("for (const k of '1212121212') { key(k); } await flush(); R.rows = ELS.rows.html;"
                            " R.sub = ELS.sub.textContent; OUT.undoes = OUT.toasts.filter(t => t.undo).length;"
                            " OUT.toasts[OUT.toasts.length - 1].undo(); await flush();",
                            self.RULE, doc=doc)
        rules = [p for p in out["posts"] if p[0] == "/api/review/rule"]
        self.assertEqual([p[1]["note"] for p in rules], list(range(10, 20)))
        self.assertEqual([p[1]["option"] for p in rules], ["Approve", "Reject"] * 5)
        # Each write carries the revision the one before it returned: one at a time, never a stale pair.
        self.assertEqual(rules[0][1]["revision"], "a" * 64)
        self.assertEqual([p[1]["revision"] for p in rules[1:]], [str(n).zfill(64) for n in range(10, 19)])
        self.assertIn("No question is open", out["R"]["rows"])
        self.assertRegex(out["R"]["sub"], r"^10 labelled · 0 open")
        self.assertEqual(out["undoes"], 10)
        withdraw = [p for p in out["posts"] if p[0] == "/api/review/withdraw"]
        self.assertEqual(withdraw, [["/api/review/withdraw", {"item": 11, "ruling": 119, "revision": "19".zfill(64)}, 64]])
        self.assertEqual(out["gets"], ["/api/review", "/api/review"])

    def test_undo_selects_the_question_again_and_counts_no_skip(self):
        # The label moved the selection on by itself; undo moving it back is not a skip (browser run, 375 px).
        doc = doc_with(3)
        out = self.run_page("key('1'); await flush(); OUT.toasts[OUT.toasts.length - 1].undo(); await flush();"
                            " R.sub = ELS.sub.textContent;",
                            """(path, body) => path === '/api/review/rule' ? [200, { item: 11, note: body.note, revision: 'b'.repeat(64), ruling: 110 }]
                              : path === '/api/review/withdraw' ? [200, { item: 11, revision: 'c'.repeat(64), withdrawal: 111 }] : [200, JSON.parse(JSON.stringify(DOC))]""",
                            doc=doc)
        self.assertEqual(out["selected"], "q10")
        self.assertRegex(out["R"]["sub"], r"^0 labelled · 3 open · 0 skipped · ")

    # Batch-level actions sit on the batch header, which a lamp opens; none waits on a selected question (lane review 235).
    DOWNLOAD = ("globalThis.URL = { createObjectURL: () => 'blob:x', revokeObjectURL() {} }; globalThis.Blob = function (parts) { this.parts = parts; };"
                " ELS.tally.listeners.click[0]({ target: { closest: s => s === '#export' ? { disabled: false } : null } }); await flush();")
    EXPORT = """(path, body) => path === '/api/review' ? [200, DOC]
      : [200, { name: 'review-nightly-x.jsonl', lines: [{ kind: 'manifest' }, { note: 10 }, { note: 11 }, { note: 12 }] }]"""

    def test_a_finished_blind_batch_downloads_from_its_header_with_no_question_selected(self):
        doc = doc_with(3, mode="blind", open=0, labels=3)
        doc["questions"] = []
        out = self.run_page("R.tally = ELS.tally.html; " + self.DOWNLOAD, self.EXPORT,
                            search="?batch=11", doc=doc)
        self.assertIsNone(out["selected"])
        self.assertIn('id="export">Download labels</button>', out["R"]["tally"])
        self.assertNotIn("disabled", out["R"]["tally"])
        self.assertEqual(out["gets"], ["/api/review", "/api/review/11/export"])
        self.assertIn("Downloaded review-nightly-x.jsonl · 3 questions", [t[0] for t in out["toasts"]])
        self.assertEqual(out["saved"], ["review-nightly-x.jsonl"])

    def test_a_blind_batch_with_open_questions_keeps_download_off_and_says_why(self):
        out = self.run_page("R.tally = ELS.tally.html; R.rows = ELS.rows.html;", self.EXPORT,
                            search="?batch=11", doc=doc_with(2, mode="blind"))
        self.assertIn('id="export" disabled>Download labels</button>', out["R"]["tally"])
        self.assertIn("A blind batch downloads once every question is ruled.", out["R"]["tally"])
        self.assertNotIn('id="export"', out["R"]["rows"])

    def test_the_batch_header_links_the_batch_task_with_no_question_selected(self):
        # A comment on the whole batch is a note on its task; the header links it, so it waits on no question.
        doc = doc_with(3, open=0, labels=3)
        doc["questions"] = []
        out = self.run_page("R.tally = ELS.tally.html;", self.EXPORT, search="?batch=11", doc=doc)
        self.assertIn('<a href="/tasks?row=11">sd:11</a>', out["R"]["tally"])

    def test_a_digit_past_the_options_a_held_key_and_a_chord_label_nothing(self):
        out = self.run_page("key('7'); (DOC_LISTENERS.keydown || []).forEach(f => f({ key: '1', repeat: true, target: { matches: () => false }, preventDefault() {} }));"
                            " window.shell.chording = () => true; key('1'); await flush();",
                            self.RULE, doc=doc_with())
        self.assertEqual(out["posts"], [])

    def test_a_stale_label_rereads_and_puts_the_question_back(self):
        out = self.run_page("key('1'); await flush(); R.rows = ELS.rows.html;",
                            "((path, body) => path === '/api/review' ? [200, DOC]"
                            " : [409, { error: 'item 11 changed; reload it before applying this change', reload: true }])", doc=doc_with())
        self.assertEqual(out["gets"], ["/api/review", "/api/review"])
        self.assertEqual(out["R"]["rows"].count('<tr data-id="q'), 3)
        self.assertIn("The batch changed; reread", out["toasts"][-1][0])

    def test_a_refused_label_stays_open_and_says_why(self):
        out = self.run_page("key('2'); await flush(); R.rows = ELS.rows.html;",
                            "((path, body) => path === '/api/review' ? [200, DOC]"
                            " : [400, { error: 'sd:11 is closed' }])", doc=doc_with())
        self.assertEqual(out["R"]["rows"].count('<tr data-id="q'), 3)
        self.assertEqual(out["toasts"][-1], ["Not labelled · note #10: sd:11 is closed", False])

    def test_a_failed_read_is_unknown_and_never_an_empty_queue(self):
        out = self.run_page("R.rows = ELS.rows.html; R.sub = ELS.sub.textContent;",
                            "(path, body) => [500, { error: 'database is locked' }]")
        self.assertIn("▨", out["R"]["rows"])
        self.assertIn("Not read: database is locked", out["R"]["rows"])
        self.assertNotIn("No question is open", out["R"]["rows"])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertEqual(out["R"]["sub"], "The review queue was not read")

    def test_a_batch_filter_from_the_address_and_esc_clears_it(self):
        doc = doc_with()
        doc["batches"].append({**doc["batches"][0], "item": 12, "title": "Label: C1", "nightly": False, "queue": "labels"})
        doc["questions"].append(question(30, item=12))
        out = self.run_page("R.rows = ELS.rows.html; R.cleared = window.PAGE_LIST.clear(); R.after = ELS.rows.html; R.again = window.PAGE_LIST.clear();",
                            "(path, body) => [200, DOC]", search="?batch=12", doc=doc)
        self.assertEqual(out["R"]["rows"].count('<tr data-id="q'), 1)
        self.assertTrue(out["R"]["cleared"])
        self.assertEqual(out["R"]["after"].count('<tr data-id="q'), 4)
        self.assertFalse(out["R"]["again"])

    def test_n_files_a_comment_on_the_batch_naming_the_question(self):
        out = self.run_page("R.toast = await window.SHELL_CAPTURE({ kind: 'note', title: 'looks off', item: 11, about: { id: 'q10' } });",
                            "((path, body) => path === '/api/review' ? [200, DOC]"
                            " : [200, { revision: 'd'.repeat(64), note: { id: 77 } }])", doc=doc_with())
        self.assertEqual(out["posts"], [["/api/items/11/notes", {"body": "On note #10: looks off", "kind": "comment", "revision": "a" * 64}, 64]])
        self.assertEqual(out["R"]["toast"], "Comment on note #10 added to sd:11")

    def test_the_page_binds_digits_alone_and_names_its_keys(self):
        self.assertIn("/^[1-9]$/", PAGE_JS)
        self.assertNotRegex(PAGE_JS, r"e\.key === '[jknu]'")
        out = self.run_page("R.keys = window.PAGE_KEYS.map(k => k[0]);", "(path, body) => [200, { batches: [], questions: [] }]")
        self.assertEqual(out["R"]["keys"], ["1 – 9", "j / k", "↵", "u", "n", "Esc"])
        self.assertEqual(out["states"][-1]["kind"], "empty")


def two_batches():
    """Batch 11 (nightly, Approve / Reject / Later) and batch 12 (Approve / Reject / Skip), with a proposal on two rows."""
    doc = doc_with(3)
    doc["batches"].append({**doc["batches"][0], "item": 12, "title": "Label: C1", "nightly": False, "queue": "labels",
                           "revision": "e" * 64, "questions": 2, "open": 2})
    doc["questions"] += [question(30, item=12, options=["Approve", "Reject", "Skip"]), question(31, item=12, options=["Approve", "Reject", "Skip"])]
    doc["questions"][0]["evidence"] = ["Proposed label: Reject"]
    doc["questions"][3]["evidence"] = ["Proposed label: Skip"]
    return doc


class TheBulkView(unittest.TestCase):
    """sd:5497: the queue bands by batch; a checkbox per open row, select and clear all, and labels for every selected row."""

    run_page = TheScript.run_page

    PICK = ("const change = data => ELS.rows.listeners.change.forEach(f => f({ target: { closest: s => s === 'input[data-pick]' && data.pick"
            " ? { dataset: data, checked: data.checked } : s === 'input[data-band-pick]' && data.bandPick ? { dataset: data, checked: data.checked } : null } }));\n"
            "const settle = async () => { for (let i = 0; i < 10; i++) await flush(); };\n"
            "const bar = (sel, data) => ELS.bulk.listeners.click.forEach(f => f({ target: { closest: s => s === sel ? { dataset: data || {}, disabled: false } : null } }));\n")
    BULK_RULE = """(path, body) => path === '/api/review' ? [200, JSON.parse(JSON.stringify(DOC))]
      : path === '/api/review/rule' ? (body.note === REFUSE ? [400, { error: 'sd:' + body.item + ' is closed' }]
          : body.note === STALE ? [409, { error: 'item changed; reload it', reload: true }]
          : body.note === LOST ? (() => { throw new Error('connection reset'); })()
          : [200, { item: body.item, note: body.note, revision: String(body.note).padStart(64, '0'), ruling: 100 + body.note }])
      : [200, { item: body.item, revision: String(body.ruling).padStart(64, '9'), withdrawal: 500 + body.ruling }]"""

    def bulk_page(self, body, refuse=0, stale=0, lost=0, doc=None):
        return self.run_page(self.PICK + f"globalThis.REFUSE = {refuse}; globalThis.STALE = {stale}; globalThis.LOST = {lost};\n" + body,
                             self.BULK_RULE, doc=doc or two_batches())

    def test_rows_group_under_one_band_per_batch_that_the_shell_folds(self):
        out = self.bulk_page("R.rows = ELS.rows.html;")
        rows = out["R"]["rows"]
        self.assertEqual(rows.count('<tr class="band"'), 2)
        self.assertLess(rows.index('data-band="b11"'), rows.index('data-id="q10"'))
        self.assertLess(rows.index('data-id="q12"'), rows.index('data-band="b12"'))
        self.assertLess(rows.index('data-band="b12"'), rows.index('data-id="q30"'))
        # The band is one cell across the table, the shell's fold grammar; its checkbox stays out of the fold button.
        self.assertIn('<tr class="band" data-band="b11"><td colspan="4">Nightly review', rows)
        self.assertIn('<label class="bandpick" data-tkeep="">', rows)
        self.assertIn('data-band-pick="11"', rows)
        self.assertEqual(rows.count('data-pick="'), 5)

    def test_select_all_and_clear_all_toggle_every_checkbox_and_the_bar_count(self):
        out = self.bulk_page("R.before = ELS.bulk.html; bar('#select-all'); R.rows = ELS.rows.html; R.bar = ELS.bulk.html;"
                             " bar('#clear-sel'); R.cleared = ELS.rows.html; R.after = ELS.bulk.html;")
        self.assertIn("<b>0</b> selected", out["R"]["before"])
        self.assertEqual(out["R"]["rows"].count(" checked"), 5 + 2)  # five rows and both bands
        self.assertIn("<b>5</b> selected", out["R"]["bar"])
        self.assertEqual(out["R"]["cleared"].count(" checked"), 0)
        self.assertIn("<b>0</b> selected", out["R"]["after"])

    def test_a_band_checkbox_selects_its_batch_alone_and_a_row_box_one_row(self):
        out = self.bulk_page("change({ bandPick: '12', checked: true }); R.bar = ELS.bulk.html; R.first = ELS.rows.html;"
                             " change({ pick: '10', checked: true }); R.more = ELS.bulk.html; R.rows = ELS.rows.html;"
                             " change({ bandPick: '12', checked: false }); R.less = ELS.bulk.html;")
        self.assertIn("<b>2</b> selected", out["R"]["bar"])
        self.assertIn('data-pick="30" aria-label="Select note 30" checked', out["R"]["first"])
        self.assertNotIn('data-pick="10" aria-label="Select note 10" checked', out["R"]["first"])
        self.assertIn("<b>3</b> selected", out["R"]["more"])
        self.assertIn("<b>1</b> selected", out["R"]["less"])
        # One row of batch 11 picked: that band reads as partly selected; batch 12 whole, as selected.
        self.assertIn('data-band-pick="11" aria-label="Select every open question in Nightly review" data-mixed=""', out["R"]["rows"])
        self.assertIn('data-band-pick="12" aria-label="Select every open question in Label: C1" checked', out["R"]["rows"])

    def test_the_bar_offers_the_options_every_selected_question_shares(self):
        out = self.bulk_page("bar('#select-all'); R.all = ELS.bulk.html; bar('#clear-sel'); change({ bandPick: '12', checked: true }); R.one = ELS.bulk.html;")
        self.assertIn('data-bulk-option="Approve"', out["R"]["all"])
        self.assertIn('data-bulk-option="Reject"', out["R"]["all"])
        self.assertNotIn('data-bulk-option="Skip"', out["R"]["all"])
        self.assertNotIn("Later", out["R"]["all"])
        self.assertIn('data-bulk-option="Skip"', out["R"]["one"])
        # Two of the five carry a recommendation.
        self.assertIn('id="accept-rec">Accept recommendation <span class="n">2</span></button>', out["R"]["all"])

    def test_label_selected_posts_one_rule_per_question_in_turn_and_undo_withdraws_each(self):
        out = self.bulk_page("bar('#select-all'); bar('button[data-bulk-option]', { bulkOption: 'Reject' }); await settle();"
                             " R.rows = ELS.rows.html; R.bar = ELS.bulk.html; OUT.toasts[OUT.toasts.length - 1].undo(); await settle();")
        rules = [p[1] for p in out["posts"] if p[0] == "/api/review/rule"]
        self.assertEqual([(r["item"], r["note"], r["option"]) for r in rules],
                         [(11, 10, "Reject"), (11, 11, "Reject"), (11, 12, "Reject"), (12, 30, "Reject"), (12, 31, "Reject")])
        # The same write the row buttons make, each with the revision the one before it in its batch returned.
        self.assertEqual([r["revision"] for r in rules], ["a" * 64, "10".zfill(64), "11".zfill(64), "e" * 64, "30".zfill(64)])
        self.assertIn("No question is open", out["R"]["rows"])
        self.assertIn("<b>0</b> selected", out["R"]["bar"])
        self.assertIn(["Labelled 5 of 5 as Reject", True], out["toasts"])
        withdraws = [p[1] for p in out["posts"] if p[0] == "/api/review/withdraw"]
        self.assertEqual(sorted(w["ruling"] for w in withdraws), [110, 111, 112, 130, 131])
        self.assertEqual(out["toasts"][-1], ["Withdrew 5 of 5 labels", False])

    def test_accept_recommendation_labels_each_with_its_own_proposal_and_keeps_the_rest_selected(self):
        out = self.bulk_page("bar('#select-all'); bar('#accept-rec'); await settle(); R.bar = ELS.bulk.html; R.rows = ELS.rows.html;")
        rules = [(p[1]["note"], p[1]["option"]) for p in out["posts"] if p[0] == "/api/review/rule"]
        self.assertEqual(rules, [(10, "Reject"), (30, "Skip")])
        self.assertIn("<b>3</b> selected", out["R"]["bar"])
        self.assertIn(["Labelled 2 of 2 as recommended · 3 with no recommendation stay selected", True], out["toasts"])

    def test_a_partial_failure_is_reported_by_row_and_that_row_stays_selected(self):
        out = self.bulk_page("bar('#select-all'); bar('button[data-bulk-option]', { bulkOption: 'Approve' }); await settle();"
                             " R.rows = ELS.rows.html; R.bar = ELS.bulk.html;", refuse=11)
        rules = [p[1]["note"] for p in out["posts"] if p[0] == "/api/review/rule"]
        self.assertEqual(rules, [10, 11, 12, 30, 31])
        self.assertIn(["Labelled 4 of 5 as Approve · not labelled: note #11 (sd:11 is closed)", True], out["toasts"])
        rows = out["R"]["rows"]
        self.assertIn('<p class="refused" role="alert">Not labelled: sd:11 is closed</p>', rows)
        self.assertIn('data-pick="11" aria-label="Select note 11" checked', rows)
        self.assertIn("<b>1</b> selected", out["R"]["bar"])

    def test_a_stale_reply_sends_nothing_more_to_that_batch_and_rereads(self):
        out = self.bulk_page("bar('#select-all'); bar('button[data-bulk-option]', { bulkOption: 'Approve' }); await settle();", stale=11)
        rules = [p[1]["note"] for p in out["posts"] if p[0] == "/api/review/rule"]
        self.assertEqual(rules, [10, 11, 30, 31])
        self.assertEqual(out["gets"], ["/api/review", "/api/review"])
        self.assertIn(["Labelled 3 of 5 as Approve · not labelled: note #11 (the batch changed; read again), note #12 (not sent: the batch changed)", True],
                      out["toasts"])

    def test_a_label_with_no_answer_is_unknown_sends_nothing_more_to_that_batch_and_rereads(self):
        out = self.bulk_page("bar('#select-all'); bar('button[data-bulk-option]', { bulkOption: 'Approve' }); await settle(); R.rows = ELS.rows.html;", lost=10)
        self.assertEqual([p[1]["note"] for p in out["posts"] if p[0] == "/api/review/rule"], [10, 30, 31])
        self.assertEqual(out["gets"], ["/api/review", "/api/review"])
        self.assertIn("note #10 (no answer, so not known: connection reset)", out["toasts"][-1][0])
        self.assertIn("note #11 (not sent: an earlier label got no answer)", out["toasts"][-1][0])


if __name__ == "__main__":
    unittest.main()


SWEEP_LOG = """[advisory-sweep-weekly] 2026-11-18T15:40:37-0700 starting (cwd: /home/operator)
The script sampled 100 of 192 advisories, so 92 were not triaged this week.

RESULT: 100 packets, 14 open, 10 filed
[advisory-sweep-weekly] 2026-11-18T15:44:05-0700 done
[advisory-sweep-weekly] 2026-11-22T05:30:00-0700 starting (cwd: /home/operator)
none
RESULT: 0 packets, 0 open, 0 filed
[advisory-sweep-weekly] 2026-11-22T05:31:00-0700 done
[advisory-sweep-weekly] 2026-11-29T05:30:00-0700 starting (cwd: /home/operator)
RESULT: failed, the packet file is missing
[advisory-sweep-weekly] 2026-11-29T05:31:00-0700 FAILED
"""


class TheAdvisorySweep(ScreenCase):
    """sd:9505: the weekly advisory sweep's RESULT, its cap shortfall and its filed items, one row per week."""

    def setUp(self):
        super().setUp()
        self.cron = Path(self.tmp.name) / "cron"
        (self.cron / "logs").mkdir(parents=True)

    def file(self, ref, status="planning"):
        found = self.item("Open review advisories", source="task-ref", external_id=ref)
        # The status alone is what the panel reads; how an item reached it is the workflow's business.
        self.connection.execute("UPDATE item SET status = ? WHERE id = ?", (status, found))

    def test_each_week_carries_its_result_cap_shortfall_and_filed_items(self):
        (self.cron / "logs" / f"{review_screen.SWEEP_JOB}.log").write_text(SWEEP_LOG, encoding="utf-8")
        self.file("advisory-sweep:2026-11-18:alpha", "done")
        self.file("advisory-sweep:2026-11-18:beta")
        self.file("advisory-sweep:2026-11-18:gamma", "in_progress")
        self.file("advisory-sweep:2026-12-06:alpha")
        self.item("Not the sweep's", source="task-ref", external_id="review:nightly:x")
        found = review_screen.sweeps(self.connection, self.cron)
        self.assertEqual(found["state"], "read")
        self.assertEqual(found["weeks"], [
            {"week": "2026-12-06", "runs": 0, "packets": None, "open": None, "filed": None, "skipped": None,
             "fixed": 0, "unfixed": 1},
            {"week": "2026-11-29", "runs": 1, "packets": None, "open": None, "filed": None, "skipped": None,
             "fixed": 0, "unfixed": 0},
            {"week": "2026-11-22", "runs": 2, "packets": 100, "open": 14, "filed": 10, "skipped": 92,
             "fixed": 1, "unfixed": 2}])

    def test_runs_and_items_of_one_sweep_week_are_one_row_keyed_on_its_sunday(self):
        # sd:9505 review: a Wednesday run by hand, a Sunday run and a Tuesday item are one sweep week, the one ending
        # Sunday 2026-11-22, the day the job runs; the Monday after starts the next week.
        log = SWEEP_LOG.split("[advisory-sweep-weekly] 2026-11-29")[0] + (
            "[advisory-sweep-weekly] 2026-11-23T09:00:00-0700 starting (cwd: /home/operator)\n"
            "RESULT: 5 packets, 1 open, 1 filed\n")
        (self.cron / "logs" / f"{review_screen.SWEEP_JOB}.log").write_text(log, encoding="utf-8")
        self.file("advisory-sweep:2026-11-17:alpha", "done")
        self.file("advisory-sweep:2026-11-22:beta")
        self.file("advisory-sweep:2026-11-23:gamma")
        # Not calendar dates: each is left out, and the page still reads.
        self.file("advisory-sweep:2026-13-45:delta")
        self.file("advisory-sweep:20261124:epsilon")
        found = review_screen.sweeps(self.connection, self.cron)["weeks"]
        self.assertEqual([(w["week"], w["runs"], w["packets"], w["filed"], w["skipped"], w["fixed"], w["unfixed"])
                          for w in found],
                         [("2026-11-29", 1, 5, 1, None, 0, 1), ("2026-11-22", 2, 100, 10, 92, 1, 1)])

    def test_no_log_still_lists_the_filed_items_and_says_why(self):
        self.file("advisory-sweep:2026-11-18:alpha", "done")
        found = review_screen.sweeps(self.connection, self.cron)
        self.assertEqual(found["state"], "none")
        self.assertIn("no log", found["reason"])
        self.assertEqual([(w["week"], w["fixed"], w["packets"]) for w in found["weeks"]], [("2026-11-22", 1, None)])

    def test_the_review_api_sends_the_sweep_beside_the_queue(self):
        from sd_dashboard.v2.pages import decide

        read = mock.Mock(connection=self.connection, jobs=mock.Mock(cron_root=self.cron))
        found = decide._read(read)
        self.assertIn("batches", found)
        self.assertEqual(found["sweeps"]["state"], "none")


class TheSweepPanel(unittest.TestCase):
    """sd:9505: Scoring's Advisory sweep panel draws one row per week from `/api/review`'s `sweeps`."""

    run_page = TheScript.run_page

    def test_each_week_is_a_row_and_an_unknown_count_is_a_dash(self):
        doc = doc_with(1)
        doc["sweeps"] = {"state": "read", "reason": "", "weeks": [
            {"week": "2026-11-29", "runs": 1, "packets": None, "open": None, "filed": None, "skipped": None,
             "fixed": 0, "unfixed": 0},
            {"week": "2026-11-18", "runs": 1, "packets": 100, "open": 14, "filed": 10, "skipped": 92,
             "fixed": 3, "unfixed": 7}]}
        out = self.run_page("R.rows = ELS.sweeps.html; R.tally = ELS['sweep-tally'].textContent;",
                            "(path, body) => [200, DOC]", doc=doc)
        rows = out["R"]["rows"]
        self.assertEqual(rows.count("<tr>"), 2)
        self.assertIn("<td>2026-11-18</td><td>100</td><td>14</td><td>10</td><td>92</td><td>3 of 10</td>", rows)
        self.assertIn("<td>2026-11-29</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td>", rows)
        self.assertEqual(out["R"]["tally"], "2 weeks")

    def test_an_unread_log_says_why_and_no_week_is_an_empty_row(self):
        doc = doc_with(1)
        doc["sweeps"] = {"state": "none", "reason": "no log: the job has not run <b>", "weeks": []}
        out = self.run_page("R.rows = ELS.sweeps.html; R.tally = ELS['sweep-tally'].textContent;",
                            "(path, body) => [200, DOC]", doc=doc)
        self.assertIn('<tr class="empty"><td colspan="6">No sweep has run yet.</td></tr>', out["R"]["rows"])
        self.assertEqual(out["R"]["tally"], "0 weeks · log: no log: the job has not run <b>")
