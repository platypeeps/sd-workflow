"""sd:1406. An answer that cites no reviewed path is not advice.

Three answers were measured on the #1153 transcripts, each against a subject
of two paths. Two blind codex answers put a finding at no location and at
`/`; a sighted opencode answer cited `.github/workflows/tests.yml:484`. Before
the fix, the `/` answer was schema-valid, counted as a completed review, and
reached the operator as `advisory` at exit 0. The location-less answer failed
the schema, but its finding was still kept and dispositioned as advice once a
fallback reader answered clean.

The check reads structure, not prose. A completed answer none of whose
findings names a path the run reviewed is voided: it no longer counts toward
depth, and its findings are dropped. A completed answer with one located
finding is kept whole. A failed answer keeps its located findings and every
blocker; only its unlocated advice is dropped.
"""

from __future__ import annotations

import json

from tests.test_sd_review import FakeRunner, sd_review
from tests.test_sd_review_fallbacks import ReviewRunFixture

CHECK = {"sd-check": sd_review.Completed(0, "{}", "")}
CLEAN = sd_review.Completed(0, json.dumps({"findings": []}), "")


def answer(*rows: dict) -> sd_review.Completed:
    return sd_review.Completed(0, json.dumps({"findings": list(rows)}), "")


def row(path: str, severity: str = "unspecified", summary: str = "a finding") -> dict:
    return {"path": path, "line": None, "severity": severity, "summary": summary, "family": "verification"}


BLIND_AT_ROOT = answer(row("/", summary="The diff could not be accessed."))
#: Schema-invalid: no `path` at all, as in blind run 1.
BLIND_NOWHERE = sd_review.Completed(0, json.dumps({"findings": [
    {"line": None, "severity": "unspecified", "summary": "The diff could not be accessed.",
     "family": "verification"}]}), "")


class AnAnswerThatCitesNothingReviewedIsNotAdvice(ReviewRunFixture):
    reviewing_depth = 1  # fallthrough, one reader per tier

    def review_with(self, answers: dict) -> dict:
        result = self.run_review(self.prepare(), FakeRunner({**CHECK, **answers}, default=CLEAN))
        self.assertIn("src.py", result["subject"]["paths"], "the premise: the reader was shown src.py")
        self.assertNotIn("bin/caller.py", result["subject"]["paths"])
        return result

    def exit_code(self, result: dict) -> int:
        return sd_review.STATUS_EXIT.get(str(result["status"]), sd_review.EXIT_OK)

    def test_a_finding_at_the_root_is_not_a_completed_review(self) -> None:
        result = self.review_with({"codex": BLIND_AT_ROOT, "second": BLIND_AT_ROOT})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.exit_code(result), sd_review.EXIT_GATE)
        self.assertEqual((result["completed_reviews"], result["reviewed_by"], result["findings"]), (0, [], []))
        self.assertEqual([(o["status"], o["diagnostic"]["unlocated_findings"]) for o in result["outcomes"]],
                         [("unavailable", 1), ("unavailable", 1)])

    def test_a_blind_answer_falls_through_to_the_next_reader(self) -> None:
        result = self.review_with({"codex": BLIND_AT_ROOT})
        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["reviewed_by"], ["second"])
        self.assertEqual(result["findings"], [])

    def test_a_location_less_finding_is_not_carried_as_advice(self) -> None:
        """Before: the failed answer's finding survived the clean fallback,
        and the run reported `advisory` about a diff its author never saw."""
        result = self.review_with({"codex": BLIND_NOWHERE})
        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["findings"], [])
        self.assertIn("cited no reviewed path", result["outcomes"][0]["detail"])

    def test_a_failed_answer_keeps_its_blockers_and_drops_its_unlocated_advice(self) -> None:
        """A failed answer's evidence survives a clean fallback (the lane's
        rule), but advice that cites nothing reviewed is not evidence."""
        rows = [row("src.py", "low", "located advice"), row("/", "low", "unlocated advice"),
                row("/", "high", "unlocated blocker")]
        failed = sd_review.Completed(0, json.dumps({"findings": rows, "extra": 1}), "")
        result = self.review_with({"codex": failed})
        self.assertEqual(result["outcomes"][0]["status"], "unavailable")
        self.assertEqual(result["outcomes"][0]["diagnostic"]["unlocated_findings"], 1)
        self.assertEqual(result["reviewed_by"], ["second"])
        self.assertEqual(result["status"], "blocking")
        self.assertEqual(sorted(f["summary"] for f in result["findings"]), ["located advice", "unlocated blocker"])

    def test_a_sighted_answer_keeps_every_finding(self) -> None:
        """Guard: one located finding keeps the answer whole, including a
        finding about a caller outside the diff."""
        sighted = answer(row("src.py", "low"), row("bin/caller.py", "low"))
        result = self.review_with({"codex": sighted})
        self.assertEqual(result["status"], "advisory")
        self.assertEqual(result["reviewed_by"], ["codex"])
        self.assertEqual([f["path"] for f in result["findings"]], ["src.py", "bin/caller.py"])

    def test_an_absolute_or_dotted_spelling_of_a_reviewed_path_is_located(self) -> None:
        root = self.prepare()
        for path in (f"{root}/src.py", f"{root.resolve()}/src.py", "./src.py"):
            with self.subTest(path=path):
                runner = FakeRunner({**CHECK, "codex": answer(row(path, "high"))}, default=CLEAN)
                result = self.run_review(root, runner)
                self.assertEqual(result["status"], "blocking")
                self.assertEqual(result["reviewed_by"], ["codex"])

    def test_a_lane_marker_is_never_voided(self) -> None:
        """`evidence_limit` writes `<review-response>`; it must keep blocking."""
        outcome = sd_review.Outcome("codex", sd_review.UNAVAILABLE,
                                    (sd_review.evidence_limit("omitted"),), "oversized", (), ())
        cited = sd_review.reviewed_paths(self.prepare(), sd_review.Subject(
            "worktree", "HEAD", "worktree", ("src.py",), 1, ""), None)
        self.assertIs(sd_review.unlocated(outcome, cited, "medium"), outcome)
