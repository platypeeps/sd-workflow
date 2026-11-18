"""Bounded fix review keeps original blockers, authors, and exact endpoints."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
from unittest.mock import patch

from tests.test_sd_review import (
    FakeRunner,
    ReviewFixture,
    codex_sessions,
    namespace,
    sd_review,
)


class FixReviewTests(ReviewFixture):
    def commit(self, root, path, text, message="change\n\nAuthored-with: human"):
        (root / path).write_text(text)
        subprocess.run(["git", "add", "--", path], cwd=root, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=root, capture_output=True, check=True)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()

    def branch(self):
        root = self.make_repo()
        subprocess.run(["git", "checkout", "-b", "topic"], cwd=root, capture_output=True, check=True)
        first = self.commit(root, "src.py", "the_original_defect = True\n")
        return root, first

    def test_exact_ancestor_is_the_whole_fix_subject(self):
        root, first = self.branch()
        second = self.commit(root, "other.py", "only_the_fix = 1\n")
        subject = sd_review.resolve_subject(root, "branch", base=first)
        self.assertEqual((subject.base, subject.head, subject.paths), (first, second, ("other.py",)))
        for scope, base in (("worktree", first), ("pr", first), ("branch", first[:12]), ("branch", "f" * 40)):
            with self.subTest(scope=scope, base=base), self.assertRaises(sd_review.UsageError):
                sd_review.resolve_subject(root, scope, base=base)

    def test_unchanged_blocker_and_source_reach_the_fix_reviewer(self):
        root, first = self.branch()
        blocking = json.dumps({"findings": [{"path": "src.py", "line": 1, "summary": "original unresolved defect",
                                            "family": "correctness", "severity": "high"}]})
        prior = sd_review.review(root, namespace(scope="branch"), FakeRunner({"codex": sd_review.Completed(0, blocking, "")}),
                                 self.environment(), self.chatgpt_home())
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(prior))
        self.commit(root, "other.py", "unrelated_fix = True\n")
        runner = FakeRunner({"codex": sd_review.Completed(0, blocking, "")})
        result = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)), runner,
                                  self.environment(), self.chatgpt_home())
        self.assertEqual(result["status"], "blocking")
        self.assertEqual(result["subject"]["paths"], ["other.py"])
        handed = json.dumps(runner.calls, default=str)
        self.assertIn("original unresolved defect", handed)
        self.assertIn("the_original_defect = True", handed)
        self.assertTrue(result["verification_report_digest"])

    def test_a_reader_that_raises_after_a_prior_report_ends_with_its_own_error(self):
        """sd:7569. The prior finding's source is read in the main thread
        through `subprocess_runner`, which registers that thread as a group
        holder; the cleanup after a failed reader must not join it."""
        root, first = self.branch()
        blocking = json.dumps({"findings": [{"path": "src.py", "line": 1, "summary": "original unresolved defect",
                                            "family": "correctness", "severity": "high"}]})
        prior = sd_review.review(root, namespace(scope="branch"), FakeRunner({"codex": sd_review.Completed(0, blocking, "")}),
                                 self.environment(), self.chatgpt_home())
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(prior))
        self.commit(root, "other.py", "unrelated_fix = True\n")

        class ReaderBroke(Exception):
            pass

        def broken_reader(*_args):
            raise ReaderBroke("the reader failed")
        for mode in ({"verify_report": str(report), "base": first}, {"resume_report": str(report)}):
            sd_review.READER_GROUPS.reopen_groups()
            with self.subTest(mode=sorted(mode)), self.assertRaises(ReaderBroke):
                sd_review.review(root, namespace(scope="branch", **mode), FakeRunner({"codex": broken_reader}),
                                 self.environment(), self.chatgpt_home())

    def test_mismatched_or_malformed_previous_report_cannot_verify(self):
        root, first = self.branch()
        self.commit(root, "fix.py", "fix = True\n")
        report = self.tmp / "prior.json"
        for prior in ({}, {"scope": "branch", "subject": {"head": "0" * 40}, "status": "clean", "findings": []},
                      {"scope": "branch", "subject": {"head": first}, "status": "clean", "findings": ["forged"]}):
            report.write_text(json.dumps(prior))
            runner = FakeRunner()
            with self.assertRaises(sd_review.UsageError):
                sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)), runner,
                                 self.environment(), self.chatgpt_home())
            self.assertEqual(runner.calls, [])

    def test_a_review_that_cleared_before_its_gate_failed_can_be_verified(self):
        """sd:2721. sd-ship keeps that pass, so the fix after it verifies a `gate_failed` report."""
        root, first = self.branch()
        prior = sd_review.review(root, namespace(scope="branch"), FakeRunner({"sd-check": sd_review.Completed(1, "{}", "")}),
                                 self.environment(), self.chatgpt_home())
        self.assertEqual(prior["status"], "gate_failed")
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(prior))
        self.commit(root, "fix.py", "fix = True\n")
        result = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)), FakeRunner(),
                                  self.environment(), self.chatgpt_home())
        self.assertEqual((result["status"], result["subject"]["paths"]), ("clean", ["fix.py"]))
        self.assertTrue(result["verification_report_digest"])

    def test_resume_reviews_full_branch_and_repeats_prior_blockers(self):
        root, first = self.branch()
        blocking = json.dumps({"findings": [{"path": "src.py", "line": 1, "summary": "original unresolved defect",
                                            "family": "correctness", "severity": "high"}]})
        prior = sd_review.review(root, namespace(scope="branch"), FakeRunner({"codex": sd_review.Completed(0, blocking, "")}),
                                 self.environment(), self.chatgpt_home())
        prior["completed_reviews"] = 0
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(prior))
        self.commit(root, "other.py", "unrelated_fix = True\n")
        runner = FakeRunner({"codex": sd_review.Completed(0, blocking, "")})
        result = sd_review.review(root, namespace(scope="branch", resume_report=str(report)), runner,
                                  self.environment(), self.chatgpt_home())
        self.assertEqual(result["status"], "blocking")
        self.assertEqual(set(result["subject"]["paths"]), {"src.py", "other.py"})
        self.assertEqual(result["subject"]["base"], result["authorship_base"])
        self.assertNotEqual(result["subject"]["base"], first)
        handed = json.dumps(runner.calls, default=str)
        self.assertIn("original unresolved defect", handed)
        self.assertIn("the_original_defect = True", handed)
        self.assertTrue(result["resume_report_digest"])
        self.assertIsNone(result["verification_report_digest"])

    def test_explain_refuses_oversized_resume_report_and_current_source(self):
        root, first = self.branch()
        report = self.tmp / "prior.json"
        row = {"path": "src.py", "line": 1, "severity": "high", "family": "correctness",
               "disposition": "blocking", "summary": "x" * (sd_review.MAX_OUTPUT_BYTES + 1)}
        prior = {"scope": "branch", "subject": {"head": first}, "findings": [row]}
        for oversized in ("report", "source"):
            if oversized == "source":
                row["summary"] = "original blocker"
                self.commit(root, "src.py", "x" * (sd_review.MAX_OUTPUT_BYTES + 1))
            report.write_text(json.dumps(prior))
            for explain in (True, False):
                runner = FakeRunner()
                with self.subTest(oversized=oversized, explain=explain):
                    if oversized == "report":
                        with self.assertRaises(sd_review.UsageError):
                            sd_review.review(root, namespace(scope="branch", resume_report=str(report), explain=explain),
                                             runner, self.environment(), self.chatgpt_home())
                    else:
                        result = sd_review.review(root, namespace(scope="branch", resume_report=str(report), explain=explain),
                                                  runner, self.environment(), self.chatgpt_home())
                        self.assertEqual(result["input_manifest"]["status"], "oversized")
                        self.assertEqual(result["readiness"]["status"], "blocked")
                self.assertEqual(runner.calls, [])

    def test_prior_findings_never_open_untracked_or_git_metadata_sources(self):
        root, first = self.branch()
        report = self.tmp / "prior.json"
        for name in (".env", ".git/fixture-marker"):
            marker = "harmless-untracked-fixture-" + name
            (root / name).write_text(marker)
            row = {"path": name, "line": 1, "disposition": "blocking", "summary": "model-selected path"}
            report.write_text(json.dumps({"scope": "branch", "subject": {"head": first},
                                          "findings": [row]}))
            runner = FakeRunner()
            with self.subTest(path=name):
                try:
                    sd_review.review(root, namespace(scope="branch", resume_report=str(report)), runner,
                                     self.environment(), self.chatgpt_home())
                except sd_review.UsageError as error:
                    self.assertIn("tracked regular file", str(error))
                else:
                    self.fail(f"unsafe source entered reviewer prompt: {marker in json.dumps(runner.calls, default=str)}")
                self.assertEqual(runner.calls, [])

    def test_prior_source_uses_exact_committed_regular_blob_not_worktree_replacement(self):
        root, first = self.branch()
        report = self.tmp / "prior.json"
        row = {"path": "src.py", "line": 1, "disposition": "blocking", "summary": "original finding"}
        report.write_text(json.dumps({"scope": "branch", "subject": {"head": first},
                                      "findings": [row]}))
        marker = "harmless-uncommitted-replacement"
        (root / ".env").write_text(marker)
        for replacement in ("dirty", "symlink"):
            (root / "src.py").unlink()
            if replacement == "symlink":
                (root / "src.py").symlink_to(root / ".env")
            else:
                (root / "src.py").write_text(marker)
            runner = FakeRunner()
            with self.subTest(replacement=replacement):
                sd_review.review(root, namespace(scope="branch", resume_report=str(report)), runner,
                                 self.environment(), self.chatgpt_home())
                handed = json.dumps(runner.calls, default=str)
                self.assertIn("the_original_defect = True", handed)
                self.assertNotIn(marker, handed)

    def test_prior_source_refuses_committed_symlinks_and_directories(self):
        root, first = self.branch()
        (root / "linked.py").symlink_to("src.py")
        (root / "folder").mkdir()
        self.commit(root, "folder/child.py", "child = True\n")
        subprocess.run(["git", "add", "linked.py"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "link\n\nAuthored-with: human"], cwd=root, check=True, capture_output=True)
        report = self.tmp / "prior.json"
        for name in ("linked.py", "folder"):
            report.write_text(json.dumps({"scope": "branch", "subject": {"head": first},
                                          "findings": [{"path": name, "disposition": "blocking", "summary": "finding"}]}))
            runner = FakeRunner()
            with self.subTest(path=name), self.assertRaisesRegex(sd_review.UsageError, "tracked regular file"):
                sd_review.review(root, namespace(scope="branch", resume_report=str(report)), runner,
                                 self.environment(), self.chatgpt_home())
            self.assertEqual(runner.calls, [])

    def test_deleted_prior_source_keeps_raw_finding_and_absence_marker(self):
        root, first = self.branch()
        subprocess.run(["git", "rm", "src.py"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "remove\n\nAuthored-with: human"], cwd=root, check=True, capture_output=True)
        findings = [{"path": "src.py", "disposition": "blocking", "summary": "original blocker"}]
        report = self.tmp / "prior.json"
        original = json.dumps({"scope": "branch", "subject": {"head": first}, "findings": findings})
        report.write_text(original)
        runner = FakeRunner()
        sd_review.review(root, namespace(scope="branch", resume_report=str(report)), runner,
                         self.environment(), self.chatgpt_home())
        prompt = next(call["stdin"] for call in codex_sessions(runner))
        self.assertIn("[file absent at current HEAD]", prompt)
        recovered, _ = json.JSONDecoder().raw_decode(prompt.split("Prior findings:\n", 1)[1])
        self.assertEqual(recovered, findings)
        self.assertEqual(report.read_text(), original)

    def test_prior_blob_read_failure_refuses_before_check_or_provider(self):
        root, first = self.branch()
        report = self.tmp / "prior.json"
        report.write_text(json.dumps({"scope": "branch", "subject": {"head": first},
                                      "findings": [{"path": "src.py", "disposition": "blocking", "summary": "finding"}]}))
        runner = FakeRunner()
        with patch.object(sd_review, "subprocess_runner", return_value=sd_review.Completed(1, "", "fixture failure")), \
                self.assertRaisesRegex(sd_review.UsageError, "cannot read prior finding source"):
            sd_review.review(root, namespace(scope="branch", resume_report=str(report)), runner,
                             self.environment(), self.chatgpt_home())
        self.assertEqual(runner.calls, [])

    def test_history_over_64k_preserves_every_raw_finding_and_provenance(self):
        root, first = self.branch()
        self.commit(root, "other.py", "unrelated_change = True\n")
        findings = [{"path": "src.py", "line": 1, "disposition": "blocking", "summary": "x" * 1500,
                     "prior_review": {"pass": index, "head": first, "backend": "original", "status": "blocking"}}
                    for index in range(60)]
        prior = {"scope": "branch", "subject": {"head": first}, "status": "blocking",
                 "findings": findings}
        evidence = json.dumps(findings, sort_keys=True)
        self.assertGreater(len(evidence.encode()), 65536)
        report = self.tmp / "large-prior.json"
        original = json.dumps(prior).encode()
        report.write_bytes(original)
        for mode in ("resume", "verify"):
            kwargs = {"resume_report": str(report)} if mode == "resume" else {"verify_report": str(report), "base": first}
            runner = FakeRunner()
            with self.subTest(mode=mode):
                result = sd_review.review(root, namespace(scope="branch", **kwargs), runner,
                                          self.environment(), self.chatgpt_home())
                self.assertEqual(result["status"], "clean")
                prompts = [call["stdin"] for call in codex_sessions(runner)]
                self.assertEqual(len(prompts), 1)
                self.assertIn("Prior findings:\n" + evidence, prompts[0])
                recovered, _ = json.JSONDecoder().raw_decode(prompts[0].split("Prior findings:\n", 1)[1])
                self.assertEqual(recovered, findings)
                self.assertIn("the_original_defect = True", prompts[0])
                self.assertEqual(report.read_bytes(), original)

    def test_final_prompt_bound_applies_without_blocking_history_before_any_dispatch(self):
        root, first = self.branch()
        report = self.tmp / "prior.json"
        for history in (None, [], [{"path": "src.py", "disposition": "advisory", "summary": "advice"}]):
            kwargs = {}
            if history is not None:
                report.write_text(json.dumps({"scope": "branch", "subject": {"head": first},
                                              "findings": history}))
                kwargs["resume_report"] = str(report)
            for text in ("x" * sd_review.MAX_OUTPUT_BYTES, "é" * (sd_review.MAX_OUTPUT_BYTES // 2 + 1)):
                for explain in (True, False):
                    runner = FakeRunner()
                    with self.subTest(history=history, multibyte=text[0] == "é", explain=explain), \
                            patch.object(sd_review, "local_conventions", return_value=text):
                        result = sd_review.review(root, namespace(scope="branch", explain=explain, **kwargs), runner,
                                                  self.environment(), self.chatgpt_home())
                        self.assertEqual(result["input_manifest"]["status"], "oversized")
                    self.assertEqual(runner.calls, [])

    def test_cumulative_current_source_remains_bounded_before_any_dispatch(self):
        root, first = self.branch()
        report = self.tmp / "prior.json"
        findings = [{"path": name, "disposition": "blocking", "summary": "unchanged blocker"}
                    for name in ("src.py", "other.py")]
        for finding in findings:
            self.commit(root, finding["path"], "x" * (sd_review.MAX_OUTPUT_BYTES // 2))
        report.write_text(json.dumps({"scope": "branch", "subject": {"head": first},
                                      "findings": findings}))
        for explain in (True, False):
            runner = FakeRunner()
            with self.subTest(explain=explain):
                result = sd_review.review(root, namespace(scope="branch", resume_report=str(report), explain=explain), runner,
                                          self.environment(), self.chatgpt_home())
                self.assertEqual(result["input_manifest"]["status"], "oversized")
                self.assertGreater(result["input_manifest"]["context_bytes"], sd_review.MAX_OUTPUT_BYTES)
            self.assertEqual(runner.calls, [])

    def test_oversized_prior_sources_keep_inventory_without_unbounded_reads_or_dry_run_stdin(self):
        for sizes in ((sd_review.MAX_OUTPUT_BYTES + 1,), (1_100_000,) * 3):
            root = self.make_repo("sources-" + str(len(sizes)))
            subprocess.run(["git", "checkout", "-b", "topic"], cwd=root, check=True, capture_output=True)
            findings = []
            for index, size in enumerate(sizes):
                name = f"source-{index}.py"
                first = self.commit(root, name, "x" * size)
                findings.append({"path": name, "disposition": "blocking", "summary": "unchanged original blocker"})
            self.commit(root, "fix.py", "unrelated = True\n")
            report = self.tmp / "prior.json"
            original = json.dumps({"scope": "branch", "subject": {"head": first}, "status": "blocking",
                                   "findings": findings})
            report.write_text(original)
            for mode in ({}, {"explain": True}, {"dry_run": True}):
                runner = FakeRunner()
                with self.subTest(sizes=sizes, mode=mode), \
                        patch.object(sd_review, "subprocess_runner", wraps=sd_review.subprocess_runner) as blobs:
                    result = sd_review.review(root, namespace(scope="branch", provider="codex", base=first,
                        verify_report=str(report), **mode), runner, self.environment(), self.chatgpt_home())
                    self.assertEqual(blobs.call_count, 0 if sizes[0] > sd_review.MAX_OUTPUT_BYTES else 1)
                    self.assertEqual(result["readiness"]["status"], "blocked")
                    manifest = result["input_manifest"]
                    self.assertEqual(manifest["status"], "oversized")
                    paths = {row["path"]: row for row in manifest["paths"]}
                    self.assertTrue({row["path"] for row in findings}.issubset(paths))
                    for index, size in enumerate(sizes):
                        self.assertGreater(paths[f"source-{index}.py"]["bytes"], size)
                    self.assertGreater(manifest["transport_bytes"]["codex"], sd_review.MAX_OUTPUT_BYTES)
                    self.assertEqual(result.get("planned_invocations", []), [])
                    self.assertEqual(runner.calls, [])
                    self.assertEqual(report.read_text(), original)

    def test_dry_run_never_emits_an_oversized_convention_prompt(self):
        root, _first = self.branch()
        runner = FakeRunner()
        with patch.object(sd_review, "local_conventions", return_value="é" * sd_review.MAX_OUTPUT_BYTES):
            result = sd_review.review(root, namespace(scope="branch", provider="codex", dry_run=True), runner,
                                      self.environment(), self.chatgpt_home())
        self.assertEqual(result["readiness"]["status"], "blocked")
        self.assertEqual(result["planned_invocations"], [])
        self.assertEqual(runner.calls, [])

    def test_resume_refuses_fix_only_or_unrelated_prior_head(self):
        root, first = self.branch()
        prior = sd_review.review(root, namespace(scope="branch"), FakeRunner(), self.environment(), self.chatgpt_home())
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(prior))
        runner = FakeRunner()
        for args in (namespace(scope="branch", base=first, resume_report=str(report)),
                     namespace(scope="worktree", resume_report=str(report))):
            with self.assertRaises(sd_review.UsageError):
                sd_review.review(root, args, runner, self.environment(), self.chatgpt_home())
        prior["subject"]["head"] = "0" * 40
        report.write_text(json.dumps(prior))
        with self.assertRaises(sd_review.UsageError):
            sd_review.review(root, namespace(scope="branch", resume_report=str(report)), runner,
                             self.environment(), self.chatgpt_home())
        self.assertEqual(runner.calls, [])

    def test_refreshed_default_code_is_reviewed_without_requiring_item_authorship(self):
        root, first = self.branch()
        subprocess.run(["git", "checkout", "main"], cwd=root, check=True, capture_output=True)
        target = self.commit(root, "upstream.py", "fresh_target = True\n", "upstream change without local attribution")
        subprocess.run(["git", "checkout", "topic"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "merge", "--no-edit", "main"], cwd=root, check=True, capture_output=True)
        result = sd_review.review(root, namespace(scope="branch", base=first), FakeRunner(), self.environment(), self.chatgpt_home())
        self.assertEqual(result["status"], "clean")
        self.assertIn("upstream.py", result["subject"]["paths"])
        self.assertEqual(result["subject"]["base"], first)
        self.assertEqual(result["authorship_base"], target)

    def test_provider_user_is_actual_uid_and_unrelated_keys_stay_absent(self):
        provider = sd_review.sd_registry.Provider(name="fixture", vendor="fixture", bill="first", env=("OWN_KEY",))
        result = sd_review.sd_registry.provider_environment(provider, {"USER": "spoofed", "OWN_KEY": "mine", "OTHER_KEY": "secret"})
        self.assertEqual(result["USER"], pwd.getpwuid(os.getuid()).pw_name)
        self.assertEqual(result["OWN_KEY"], "mine")
        self.assertNotIn("OTHER_KEY", result)

    def test_native_auth_error_is_reported_while_structured_blocker_survives(self):
        findings = {"findings": [{"path": "src.py", "line": 1, "summary": "blocker", "family": "correctness", "severity": "high"}]}
        result, parsed = sd_review.claude_answer(sd_review.Completed(0, json.dumps({"type": "result", "subtype": "error",
                                 "is_error": True, "result": "Not logged in", "structured_output": findings}), ""))
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stderr, "Not logged in")
        self.assertTrue(parsed)
