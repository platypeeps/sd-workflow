"""Bounded fix review keeps original blockers, authors, and exact endpoints."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
from unittest.mock import patch

from tests.clean_env import clean_environment
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
                                            "family": "correctness", "severity": "high", "instances": ["src.py:1"],
                                            "evidence": "python3 src.py: Traceback"}]})
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

    def held_blocker_report(self):
        """A full pass with a blocker, saved, and a fix commit on top."""
        root, first = self.branch()
        blocking = json.dumps({"findings": [{"path": "src.py", "line": 1, "summary": "held defect",
                                            "family": "correctness", "severity": "high", "instances": ["src.py:1"]}]})
        prior = sd_review.review(root, namespace(scope="branch"), FakeRunner({"codex": sd_review.Completed(0, blocking, "")}),
                                 self.environment(), self.chatgpt_home())
        self.assertEqual(prior["status"], "blocking")
        self.assertEqual(prior["providers"], ["codex", "second"], "a full branch pass keeps both readers")
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(prior))
        self.commit(root, "src.py", "the_original_defect = False\n")
        return root, first, report

    @staticmethod
    def programs(runner):
        return {os.path.basename(call["argv"][0]) for call in runner.calls}

    def test_a_verify_round_with_a_held_blocker_asks_codex_alone(self):
        """sd:10127: rounds 2-4 verify with one reader, the chain's first."""
        root, first, report = self.held_blocker_report()
        runner = FakeRunner()
        result = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)),
                                  runner, self.environment(), self.chatgpt_home())
        self.assertEqual(result["providers"], ["codex"])
        self.assertEqual(result["reviewed_by"], ["codex"])
        self.assertEqual((result["status"], result["requested_reviews"], result["review_shortfall"]), ("clean", 1, None))
        self.assertEqual(result["fallback_candidates"], ["second"])
        self.assertTrue(result["covers_from"], "a one-reader verify pass is a full one: it extends the chain")
        self.assertNotIn("second", self.programs(runner), "the second reader ran beside the first")
        self.assertIn("codex", self.programs(runner))

    def test_a_full_pass_and_a_challenged_verify_round_keep_both_readers(self):
        """sd:10127: only a plain verify round drops to one; a resumed full pass holds its blocker at two."""
        root, first, report = self.held_blocker_report()  # asserts round 1's two readers
        resumed = sd_review.review(root, namespace(scope="branch", resume_report=str(report)), FakeRunner(),
                                   self.environment(), self.chatgpt_home())
        self.assertEqual(resumed["providers"], ["codex", "second"])
        challenged = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report), challenge=True),
                                      FakeRunner(), self.environment(), self.chatgpt_home())
        self.assertEqual(challenged["providers"], ["codex", "second"])

    def test_a_verify_round_falls_back_when_codex_is_unready(self):
        """sd:10127: an unready codex hands the one verify read to the next reader."""
        root, first, report = self.held_blocker_report()
        path = self.registry_home / ".local/share/sd/providers.yaml"  # second needs no codex auth
        path.write_text(path.read_text().replace(
            "roles: [author, reviewer], reader: codex-json", "roles: [author, reviewer], reader: claude-json"))
        runner = FakeRunner({"second": sd_review.Completed(0, json.dumps({"type": "result", "subtype": "success",
                                                                         "structured_output": {"findings": []}}), "")})
        result = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)),
                                  runner, self.environment(), self.tmp / "missing-auth")
        self.assertEqual(result["reviewed_by"], ["second"])
        self.assertEqual((result["status"], result["requested_reviews"]), ("clean", 1))
        self.assertEqual(self.programs(runner) & {"codex", "second"}, {"second"}, "an unready codex started")

    def test_a_verify_round_passes_over_a_capped_or_metered_codex(self):
        """sd:10127: a capped or metered bill leaves the chain, so the one read goes to the next reader, in full."""
        root, first, report = self.held_blocker_report()
        runner = FakeRunner()
        with patch.object(sd_review, "capped_bills", return_value={"first": "its window reads zero"}):
            result = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)),
                                      runner, self.environment(), self.chatgpt_home())
        self.assertEqual((result["providers"], result["reviewed_by"]), (["second"], ["second"]))
        self.assertIsNone(result["review_shortfall"], "one asked, one eligible: the round is not short")
        self.assertTrue(result["covers_from"])

    def test_a_grouped_blocker_hands_the_reviewer_every_instance_file(self):
        """Lead ruling on sd:9769 (15:20 MST): a fix to the primary file alone must not clear a blocker whose
        second instance sits in another file; the verifier gets each listed file's current source and is told
        the blocker clears only when every instance is fixed."""
        root, _ = self.branch()
        first = self.commit(root, "second.py", "the_second_instance = True\n")
        blocking = json.dumps({"findings": [{"path": "src.py", "line": 1, "summary": "grouped unresolved defect",
                                            "family": "correctness", "severity": "high",
                                            "instances": ["src.py:1", "second.py:1"],
                                            "evidence": "python3 src.py: Traceback"}]})
        prior = sd_review.review(root, namespace(scope="branch"), FakeRunner({"codex": sd_review.Completed(0, blocking, "")}),
                                 self.environment(), self.chatgpt_home())
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(prior))
        self.commit(root, "src.py", "the_original_defect = False\n")
        runner = FakeRunner({"codex": sd_review.Completed(0, blocking, "")})
        result = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)), runner,
                                  self.environment(), self.chatgpt_home())
        handed = json.dumps(runner.calls, default=str)
        self.assertIn("Current source for prior finding second.py", handed, "an instance file's source was not attached")
        self.assertIn("the_second_instance = True", handed)
        self.assertIn("every listed instance", handed, "the verifier was not told each instance must be fixed")
        self.assertEqual(result["status"], "blocking")

    def test_an_uncited_repeat_at_a_remaining_instance_still_blocks(self):
        """build/sd-1623 review (bin/sd-review:1833): a blocker at src.py:1 that also lists second.py:1. With
        src.py fixed, its uncited repeat at second.py posted as advisory: the prior match read the reported
        place alone. Each listed instance now holds the blocker, mapped through the diff as its place is."""
        self.remaining_instance_repeat("")

    def test_a_remaining_instance_moved_by_an_insertion_still_blocks(self):
        """build/sd-1623 review (bin/sd-review:1928): the instance's line moves through the diff too."""
        self.remaining_instance_repeat("# a comment\n" * (sd_review.ECHO_LINES + 2))  # past an echo

    def remaining_instance_repeat(self, above):
        sd_review.READER_GROUPS.reopen_groups()  # a failed reader in an earlier test closes them
        root, _ = self.branch()
        first = self.commit(root, "second.py", "the_second_instance = True\n")
        prior = {"scope": "branch", "subject": {"head": first}, "status": "blocking", "findings": [
            {"path": "src.py", "line": 1, "summary": "grouped unresolved defect", "family": "correctness",
             "severity": "high", "instances": ["src.py:1", "second.py:1"],
             "evidence": "python3 src.py: Traceback", "disposition": "blocking"}]}
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(prior))
        self.commit(root, "src.py", "the_original_defect = False\n")
        if above:
            self.commit(root, "second.py", above + "the_second_instance = True\n")
        line = 1 + above.count("\n")
        repeat = json.dumps({"findings": [{"path": "second.py", "line": line,
                                           "summary": "grouped unresolved defect", "family": "correctness",
                                           "severity": "high", "instances": [f"second.py:{line}"]}]})
        result = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)),
                                  FakeRunner({"codex": sd_review.Completed(0, repeat, "")}),
                                  self.environment(), self.chatgpt_home())
        self.assertEqual(result["status"], "blocking")
        self.assertEqual([row.get("evidence") for row in result["findings"]], ["python3 src.py: Traceback"])

    def test_a_respelled_repeat_of_a_prior_blocker_still_blocks(self):
        """build/sd-1423 class pass: a finding is matched across reports and readers in any spelling the review
        accepts. A prior blocker at an absolute path, repeated uncited as `src.py`, posted as advisory; so did
        the reverse spellings. Each is now matched on one repository-relative spelling."""
        sd_review.READER_GROUPS.reopen_groups()  # the failed reader above closes them
        root, first = self.branch()
        spellings = (f"{root}/src.py", f"{root.resolve()}/src.py", "./src.py", "src.py")
        # An absolute prior path is refused before review ("outside this repository"); filed apart.
        for before, after in [(a, b) for a in spellings[2:] for b in spellings if a != b]:
            with self.subTest(before=before, after=after):
                prior = {"scope": "branch", "subject": {"head": first}, "status": "blocking", "findings": [
                    {"path": before, "line": 1, "summary": "original unresolved defect", "family": "correctness",
                     "severity": "high", "evidence": "python3 src.py: Traceback", "disposition": "blocking"}]}
                report = self.tmp / "prior.json"
                report.write_text(json.dumps(prior))
                if not (root / "other.py").exists():
                    self.commit(root, "other.py", "unrelated_fix = True\n")
                repeat = json.dumps({"findings": [{"path": after, "line": 1, "summary": "original unresolved defect",
                                                   "family": "correctness", "severity": "high",
                                                   "instances": [f"{after}:1"]}]})
                result = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)),
                                          FakeRunner({"codex": sd_review.Completed(0, repeat, "")}),
                                          self.environment(), self.chatgpt_home())
                self.assertEqual(result["status"], "blocking")
                self.assertEqual([(row["path"], row["evidence"]) for row in result["findings"]],
                                 [("src.py", "python3 src.py: Traceback")])

    def test_a_reader_that_raises_after_a_prior_report_ends_with_its_own_error(self):
        """sd:7569. The prior finding's source is read in the main thread
        through `subprocess_runner`, which registers that thread as a group
        holder; the cleanup after a failed reader must not join it."""
        root, first = self.branch()
        blocking = json.dumps({"findings": [{"path": "src.py", "line": 1, "summary": "original unresolved defect",
                                            "family": "correctness", "severity": "high", "instances": ["src.py:1"],
                                            "evidence": "python3 src.py: Traceback"}]})
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

    def test_a_docs_only_fix_still_verifies_the_prior_code_blockers(self):
        """Review of sd:9769: a delta the router would skip must not clear blockers nobody checked."""
        root, first = self.branch()
        blocking = json.dumps({"findings": [{"path": "src.py", "line": 1, "summary": "original unresolved defect",
                                            "family": "correctness", "severity": "high", "instances": ["src.py:1"],
                                            "evidence": "python3 src.py: Traceback"}]})
        prior = sd_review.review(root, namespace(scope="branch"), FakeRunner({"codex": sd_review.Completed(0, blocking, "")}),
                                 self.environment(), self.chatgpt_home())
        self.assertEqual(prior["status"], "blocking")
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(prior))
        self.commit(root, "notes.md", "a note\n")
        runner = FakeRunner({"codex": sd_review.Completed(0, blocking, "")})
        result = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)), runner,
                                  self.environment(), self.chatgpt_home())
        self.assertEqual(result["status"], "blocking", "a docs-only delta skipped the prior blockers")

    def test_a_verify_pass_carries_the_base_its_chain_covers(self):
        """Review of sd:9769: a verify pass covers from where its first full pass started, and no further."""
        root, first = self.branch()
        full = sd_review.review(root, namespace(scope="branch"), FakeRunner(), self.environment(), self.chatgpt_home())
        self.assertEqual(full["covers_from"], full["subject"]["base"])
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(full))
        second = self.commit(root, "fix.py", "fix = True\n")
        chained = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)),
                                   FakeRunner(), self.environment(), self.chatgpt_home())
        self.assertEqual(chained["covers_from"], full["subject"]["base"])
        narrow = {**full, "subject": {**full["subject"], "base": first, "head": second}}  # delta-only, no covers_from
        narrow.pop("covers_from")
        report.write_text(json.dumps(narrow))
        self.commit(root, "more.py", "more = True\n")
        unchained = sd_review.review(root, namespace(scope="branch", base=second, verify_report=str(report)),
                                     FakeRunner(), self.environment(), self.chatgpt_home())
        self.assertIsNone(unchained["covers_from"], "a narrow prior report was read as a full pass")

    def test_a_verify_pass_under_another_binding_covers_nothing(self):
        """Review of sd:9769: a policy changed between rounds never reviewed the branch the chain stands for."""
        root, first = self.branch()
        full = sd_review.review(root, namespace(scope="branch"), FakeRunner(), self.environment(), self.chatgpt_home())
        report = self.tmp / "prior.json"
        report.write_text(json.dumps({**full, "binding": {"review_declaration": {"severity_floor": "high"}}}))
        self.commit(root, "fix.py", "fix = True\n")
        chained = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)),
                                   FakeRunner(), self.environment(), self.chatgpt_home())
        self.assertIsNone(chained["covers_from"], "a pass under another policy extended the chain")

    def test_an_unreadable_binding_seeds_no_chain_and_extends_none(self):
        """Review of sd:9767: two unreadable bindings (None) are not one binding; a pass under no binding
        covers nothing, full or delta."""
        root, first = self.branch()
        with patch.object(sd_review.sd_lib, "lead_review_binding", return_value=None):
            full = sd_review.review(root, namespace(scope="branch"), FakeRunner(), self.environment(),
                                    self.chatgpt_home())
            self.assertIsNone(full["covers_from"], "a full pass under no binding covers the branch")
            report = self.tmp / "prior.json"
            report.write_text(json.dumps({**full, "covers_from": full["subject"]["base"]}))
            self.commit(root, "fix.py", "fix = True\n")
            chained = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)),
                                       FakeRunner(), self.environment(), self.chatgpt_home())
        self.assertIsNone(chained["covers_from"], "a delta under no binding inherited coverage")

    def test_a_verify_report_from_a_sibling_branch_is_refused(self):
        """Review of sd:9767: a report whose head is not an ancestor of HEAD never chains."""
        root, first = self.branch()
        full = sd_review.review(root, namespace(scope="branch"), FakeRunner(), self.environment(), self.chatgpt_home())
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(full))
        subprocess.run(["git", "checkout", "-b", "sibling", full["subject"]["base"]], cwd=root,
                       capture_output=True, check=True, env=clean_environment())
        self.commit(root, "src.py", "the_original_defect = True\n", message="same change\n\nAuthored-with: other")
        with self.assertRaisesRegex(sd_review.UsageError, "ancestor of HEAD"):
            sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)),
                             FakeRunner(), self.environment(), self.chatgpt_home())

    def test_a_pass_the_lane_would_not_record_seeds_no_chain(self):
        """Review of sd:9769: a --draft or --provider pass must not hand its coverage to a verify pass."""
        root, first = self.branch()
        for option in ({"draft": True}, {"provider": "codex"}):
            with self.subTest(option=option):
                full = sd_review.review(root, namespace(scope="branch", **option), FakeRunner(),
                                        self.environment(), self.chatgpt_home())
                self.assertIsNone(full["covers_from"], "an unrecordable pass covers the branch")

    def test_a_blocking_pass_short_of_its_depth_covers_nothing(self):
        """Review of sd:9769: a pass one reader short must not seed a chain the lane trusts."""
        blocker = {"path": "src.py", "line": 1, "summary": "defect", "family": "correctness",
                   "severity": "high", "instances": ["src.py:1"]}
        answered = sd_review.Outcome("codex", sd_review.FINDINGS, (blocker,), "", (), ())
        missing = sd_review.Outcome("claude", sd_review.UNAVAILABLE, (), "no answer", (), ())
        result = sd_review.finish_review({"severity_floor": "medium", "requested_reviews": 2, "covers_from": "a" * 40},
                                         [answered, missing], ["codex"])
        self.assertEqual(result["status"], "blocking")
        self.assertIsNone(result["covers_from"], "a pass one reader short still covers the branch")

    def test_a_pass_short_of_its_policy_depth_covers_nothing(self):
        """Review of sd:9769: `requested_reviews` is capped to the readers left; the shortfall is not a full pass."""
        answered = sd_review.Outcome("codex", sd_review.CLEAN, (), "", (), ())
        result = sd_review.finish_review({"severity_floor": "medium", "requested_reviews": 1, "covers_from": "a" * 40,
                                          "review_shortfall": {"requested": 2, "eligible": 1}}, [answered], ["codex"])
        self.assertIsNone(result["covers_from"], "a pass one provider short of its policy covers the branch")

    def test_a_verify_pass_keeps_the_branch_s_copilot_requirement(self):
        """Review of sd:9769: a small fix's tier must not drop the Copilot review the branch's tier required."""
        root, first = self.branch()
        full = sd_review.review(root, namespace(scope="branch"), FakeRunner(), self.environment(), self.chatgpt_home())
        full["remote_reviews"]["copilot"]["automatic"] = True  # the deep branch's own requirement
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(full))
        self.commit(root, "fix.py", "fix = True\n")
        chained = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)),
                                   FakeRunner(), self.environment(), self.chatgpt_home())
        self.assertIs(chained["remote_reviews"]["copilot"]["automatic"], True, "the fix tier dropped Copilot")

    def test_a_verify_pass_routes_the_whole_branch_for_copilot(self):
        """Review of sd:9769: two standard deltas that cross the deep line together still select Copilot."""
        root, first = self.branch()
        real = sd_review.sd_route.route

        def route(paths, lines, draft, policy):
            plan = real(paths, lines, draft, policy)
            return plan._replace(tier="deep") if {"src.py", "fix.py"} <= set(paths) else plan._replace(tier="standard")

        with patch.object(sd_review.sd_route, "route", route), patch.object(sd_review.sd_lib, "reported_copilot_policy", lambda *a: ("deep", "test")):
            full = sd_review.review(root, namespace(scope="branch"), FakeRunner(), self.environment(), self.chatgpt_home())
            self.assertIs(full["remote_reviews"]["copilot"]["automatic"], False)
            report = self.tmp / "prior.json"
            report.write_text(json.dumps(full))
            self.commit(root, "fix.py", "fix = True\n")
            chained = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)),
                                       FakeRunner(), self.environment(), self.chatgpt_home())
        self.assertIs(chained["remote_reviews"]["copilot"]["automatic"], True,
                      "two standard deltas crossed the deep line and Copilot was not selected")

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
                                            "family": "correctness", "severity": "high", "instances": ["src.py:1"],
                                            "evidence": "python3 src.py: Traceback"}]})
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
        row = {"path": "src.py", "line": 1, "severity": "high", "instances": ["src.py:1"], "family": "correctness",
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
        findings = {"findings": [{"path": "src.py", "line": 1, "summary": "blocker", "family": "correctness", "severity": "high", "instances": ["src.py:1"]}]}
        result, parsed = sd_review.claude_answer(sd_review.Completed(0, json.dumps({"type": "result", "subtype": "error",
                                 "is_error": True, "result": "Not logged in", "structured_output": findings}), ""))
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stderr, "Not logged in")
        self.assertTrue(parsed)
