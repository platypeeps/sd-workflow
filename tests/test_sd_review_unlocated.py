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

sd:9536. A file the repository holds at the reviewed head is located too:
a docs-only change drew a finding on the file it contradicts, and both
lane passes were voided. The note names each voided path and summary.
"""

from __future__ import annotations

import json
import subprocess
from unittest import mock

from tests.clean_env import clean_environment
from tests.test_sd_review import (
    FIXTURE_REGISTRY,
    FakeClient,
    FakeRunner,
    chat_answer,
    namespace,
    sd_review,
)
from tests.test_sd_review_fallbacks import ReviewRunFixture

CHECK = {"sd-check": sd_review.Completed(0, "{}", "")}
CLEAN = sd_review.Completed(0, json.dumps({"findings": []}), "")


def answer(*rows: dict) -> sd_review.Completed:
    return sd_review.Completed(0, json.dumps({"findings": list(rows)}), "")


def row(path: str, severity: str = "unspecified", summary: str = "a finding", evidence: str | None = None) -> dict:
    found = {"path": path, "line": None, "severity": severity, "summary": summary, "family": "verification",
             **({"instances": [path]} if severity == "high" else {})}
    return found if evidence is None else {**found, "evidence": evidence}


#: A high finding blocks only with cited evidence (sd:9867), so a blocking fixture carries some.
REPRO = "python3 src.py: expected 0, got 1"


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
                row("/", "high", "unlocated blocker", REPRO)]
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
                runner = FakeRunner({**CHECK, "codex": answer(row(path, "high", evidence=REPRO))}, default=CLEAN)
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


class AFileTheRepositoryHoldsIsLocated(ReviewRunFixture):
    """sd:9536. A finding on a tracked file outside the diff is advice."""

    reviewing_depth = 1

    def review(self, codex: sd_review.Completed | None, client: FakeClient | None = None, **options) -> dict:
        root = self.make_repo(f"repo-{len(list(self.tmp.iterdir()))}")
        (root / "src.py").write_text("the_review_subject = 123\n")
        self.stub_policy({"default_tier": "cheap"})
        if options.get("scope") == "branch":
            for argv in (["checkout", "-b", "change"], ["add", "src.py"], ["commit", "-m", "change"]):
                subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True, env=clean_environment())
        (root / "loose.md").write_text("in the working tree only\n")
        if client is not None:
            self.local_block(root)
            (root / "CLAUDE.local.md").write_text((root / "CLAUDE.local.md").read_text().replace(
                "codex@codex", "codex@codex.example.test"))
            result = sd_review.review(root, namespace(**options), FakeRunner(CHECK, default=CLEAN),
                                      self.environment(REMOTE_KEY="fixture"), self.chatgpt_home(), client=client)
        else:
            result = self.run_review(root, FakeRunner({**CHECK, "codex": codex}, default=CLEAN), **options)
        self.assertNotIn("README.md", result["subject"]["paths"], "the premise: README.md is outside the diff")
        return result

    def test_a_tracked_file_outside_the_diff_is_located(self) -> None:
        for scope in ("worktree", "branch"):
            for path in ("README.md", "./README.md", "{root}/README.md"):
                with self.subTest(scope=scope, path=path):
                    spelled = path.format(root=self.tmp / f"repo-{len(list(self.tmp.iterdir()))}")
                    result = self.review(answer(row(spelled, "low", "README contradicts src.py")), scope=scope)
                    self.assertEqual(result["status"], "advisory")
                    self.assertEqual(result["reviewed_by"], ["codex"])
                    self.assertEqual([f["summary"] for f in result["findings"]], ["README contradicts src.py"])

    def test_a_path_the_head_does_not_hold_is_still_voided(self) -> None:
        """Blind answers stay voided (sd:1406): nothing, the root, prose, a
        missing file, a directory, a path above the repository, or a file
        only the working tree holds on a committed subject."""
        for path in ("", "/", "the diff", "missing.md", ".git", "../README.md", "/etc/hosts"):
            with self.subTest(path=path):
                result = self.review(answer(row(path)))
                self.assertEqual(result["reviewed_by"], ["second"])
                self.assertEqual(result["outcomes"][0]["status"], "unavailable")
        result = self.review(answer(row("loose.md")), scope="branch")
        self.assertEqual(result["outcomes"][0]["status"], "unavailable")

    def test_a_failed_git_read_is_not_located(self) -> None:
        with mock.patch.object(sd_review.sd_lib, "git_output", return_value=None):
            self.assertFalse(sd_review.head_holds(self.make_repo(), "HEAD")("README.md"))

    def test_the_note_names_each_voided_path_and_summary(self) -> None:
        long = "The diff could not be accessed. " + "x" * 200
        result = self.review(answer(row("/", summary=long), row("missing.md", summary="line one\nline two")))
        outcome = result["outcomes"][0]
        self.assertEqual(outcome["status"], "unavailable")
        self.assertIn("'/': The diff could not be accessed. x", outcome["detail"])
        self.assertIn("'missing.md': line one line two", outcome["detail"])
        self.assertNotIn("x" * 120, outcome["detail"])
        self.assertEqual(outcome["diagnostic"]["unlocated_findings"], 2)
        self.assertEqual(outcome["diagnostic"]["unlocated"],
                         [{"path": "/", "summary": " ".join(long.split())[:120]},
                          {"path": "missing.md", "summary": "line one line two"}])

    def test_a_path_git_cannot_take_is_not_located_and_never_raises(self) -> None:
        """Lead review of 4032cdd34: a NUL or a lone surrogate raised in the
        git check before any fallback ran; an overlong path is refused too."""
        held = sd_review.head_holds(self.make_repo(), "HEAD")
        for path in ("README.md\x00", "README\ud800.md", "a/" * 600 + "README.md"):
            with self.subTest(path=path[:20]):
                self.assertFalse(held(path))
        result = self.review(answer(row("README.md\x00"), row("\ud800")))
        self.assertEqual((result["outcomes"][0]["status"], result["reviewed_by"]), ("unavailable", ["second"]))

    def test_the_note_escapes_control_characters_and_lone_surrogates(self) -> None:
        result = self.review(answer(row("/", summary="red \x1b[31mtext\x07 and \ud800 end"), row("\x1b]0;x\ud800")))
        detail, shown = result["outcomes"][0]["detail"], result["outcomes"][0]["diagnostic"]["unlocated"]
        self.assertIn("red \\x1b[31mtext\\x07 and \\ud800 end", detail)
        self.assertIn("'\\x1b]0;x\\ud800'", detail)
        self.assertEqual([row["path"] for row in shown], ["/", "\\x1b]0;x\\ud800"])
        for text in (detail, json.dumps(shown, ensure_ascii=False)):
            self.assertFalse(any(not ch.isprintable() for ch in text), text)

    def test_the_note_bounds_each_path_and_the_list(self) -> None:
        long = "p" * 300
        result = self.review(answer(*(row(f"{n}{long}", summary=f"finding {n}") for n in range(7))))
        detail = result["outcomes"][0]["detail"]
        self.assertIn(f"'0{'p' * 119}': finding 0", detail)
        self.assertNotIn("p" * 120, detail)
        self.assertIn("finding 4; +2 more", detail)
        self.assertNotIn("finding 5", detail)
        self.assertEqual(result["outcomes"][0]["diagnostic"]["unlocated_findings"], 7)

    def test_a_claude_reader_in_the_checkout_may_cite_a_file_it_can_read(self) -> None:
        """Re-review of 6bb31d81d: claude-json gets the material and also runs
        in the checkout with Read, Grep and Glob, so it can see README.md."""
        registry = self.registry_home / ".local/share/sd/providers.yaml"
        registry.write_text(FIXTURE_REGISTRY.replace("roles: [reviewer], reader: codex-json",
                                                     "roles: [reviewer], reader: claude-json", 1))
        claude = sd_review.Completed(0, json.dumps({"type": "result", "subtype": "success", "structured_output": {
            "findings": [row("README.md", "low", "README contradicts src.py")]}}), "")
        result = self.review(claude)
        self.assertEqual((result["status"], result["reviewed_by"]), ("advisory", ["codex"]))
        self.assertEqual([f["path"] for f in result["findings"]], ["README.md"])

    def test_a_url_reader_sees_only_the_material(self) -> None:
        """A URL entry gets the supplied material alone: README.md is not its to cite."""
        registry = self.registry_home / ".local/share/sd/providers.yaml"
        registry.write_text(FIXTURE_REGISTRY.replace('codex:  { start: "codex exec", vendor: openai, bill: first,',
                                                     'codex:  { url: "https://codex.example.test/v1", model: fixture, '
                                                     'vendor: openai, bill: first, env: [REMOTE_KEY],', 1))
        client = FakeClient({"codex": chat_answer(json.dumps({"findings": [row("README.md", "low")]}))})
        result = self.review(None, client=client)
        self.assertEqual(len(client.sent), 1, "the premise: the URL reader answered")
        self.assertEqual(result["outcomes"][0]["status"], "unavailable")
        self.assertIn("'README.md': a finding", result["outcomes"][0]["detail"])
        self.assertEqual(result["reviewed_by"], ["second"])

    def test_a_url_reader_s_branch_pass_covers_nothing(self) -> None:
        """Review of sd:9769: a URL reader gets patches, not whole files, so its pass seeds no chain."""
        registry = self.registry_home / ".local/share/sd/providers.yaml"
        registry.write_text(FIXTURE_REGISTRY.replace('codex:  { start: "codex exec", vendor: openai, bill: first,',
                                                     'codex:  { url: "https://codex.example.test/v1", model: fixture, '
                                                     'vendor: openai, bill: first, env: [REMOTE_KEY],', 1))
        client = FakeClient({"codex": chat_answer(json.dumps({"findings": []}))})
        result = self.review(None, client=client, scope="branch")
        self.assertIn("codex", result["reviewed_by"], "the premise: the URL reader completed")
        self.assertIsNone(result["covers_from"], "a patch-only pass covers the branch")

    def test_only_a_reader_launched_in_the_checkout_reads_it(self) -> None:
        def entry(**fields) -> sd_review.sd_registry.Provider:
            return sd_review.sd_registry.Provider(name="p", vendor="v", bill="b", **fields)
        self.assertEqual([sd_review.reads_checkout(entry(start="x", reader=reader)) for reader in sd_review.READERS],
                         [reader != "opencode-json" for reader in sd_review.READERS])
        self.assertFalse(sd_review.reads_checkout(entry(url="https://remote.example.test/v1", model="m")))

    def test_a_planning_review_cites_only_its_own_plan(self) -> None:
        """Another item's plan is a file the head holds; on a planning review
        it is not this subject, so the old rule stands."""
        subject = sd_review.Subject("planning", "HEAD", "worktree", ("src.py",), 1, "the plan")
        with mock.patch.object(sd_review, "resolve_subject", return_value=subject):
            result = self.review(answer(row("README.md", "low")), scope="planning")
        self.assertEqual(result["outcomes"][0]["status"], "unavailable")
        self.assertEqual(result["reviewed_by"], ["second"])
