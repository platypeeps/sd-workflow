"""sd:1475. A gate that fails before any reviewer is asked spends no pass.

Under parallel load `make check` outran sd-check's fixed 900-second limit,
sd-review reported `gate_failed` without asking a provider, and sd-ship kept
the reserved pass anyway: sd:1309 lost two of its five automatic passes that
way, and each loss also demanded `--retry-review` for a review nobody ran.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import importlib
import io
import os
import pathlib
import sys
import unittest
import unittest.mock

from tests import test_sd_review as review_tests
from tests import test_sd_ship_provider as provider_tests
from tests.test_sd_review import FakeRunner, sd_review

ship = provider_tests.ship
# After the fixture import, which puts `bin/` on the path.
sd_ship_review = importlib.import_module("sd_ship_review")
HEAD = provider_tests.HEAD

GATE_FAILED = {"status": "gate_failed", "check": {"status": "fail", "exit_code": 1, "detail": "timed out after 900s"},
               "completed_reviews": 0, "reviewed_by": [], "outcomes": [], "findings": []}


class GateFailureSpendsNoPass(unittest.TestCase):
    # Borrowed, not inherited: a subclass would run every selection test twice.
    context = provider_tests.ProviderSelection.context

    def test_a_gate_failure_before_any_reviewer_leaves_no_pass(self):
        review, process = self.context(report_changes=GATE_FAILED)
        with self.assertRaisesRegex(ship.Refusal, "no review pass was spent") as caught:
            review.review(HEAD)
        self.assertIn("timed out after 900s", str(caught.exception))
        self.assertEqual(review.state["passes"], [])
        self.assertIsNone(review.state.get("reviewed_head"))
        self.assertEqual(review.state["review_preflight_error"]["kind"], "gate_failed")
        self.assertEqual(process.call_count, 2)

    def test_the_refusal_names_each_failing_check_and_its_own_tail(self):
        """sd:2021. The refusal kept the last 500 characters of one stream, so a
        long traceback after the assertion dropped the line that said why."""
        rows = [{"name": "check", "status": "pass", "exit_code": 0, "stdout": "ok", "stderr": ""},
                {"name": "test", "status": "fail", "exit_code": 1, "reason": "",
                 "stdout": "", "stderr": "AssertionError: sd2021 marker\n" + "t" * 600 + "\nFAILED (failures=1)"},
                {"name": "lint", "status": "fail", "exit_code": 2, "reason": "", "stdout": "E501 line too long", "stderr": ""}]
        failed = {**GATE_FAILED, "check": {"status": "fail", "exit_code": 1, "detail": "", "checks": rows}}
        review, _process = self.context(report_changes=failed)
        with self.assertRaisesRegex(ship.Refusal, "no review pass was spent") as caught:
            review.review(HEAD)
        message = str(caught.exception)
        self.assertIn("test (exit 1): failing: AssertionError: sd2021 marker\nstderr: AssertionError: sd2021 marker", message)
        self.assertIn("lint (exit 2): stdout: E501 line too long", message)

    def test_the_refusal_names_the_failed_shards_and_the_whole_output(self):
        """sd:2558. A shard that failed early in a long run was in neither
        tail, so prepare refused without naming the test that failed."""
        shard = "shard tests.test_middle: 4s exit=1"
        rows = [{"name": "check", "status": "fail", "exit_code": 2, "reason": "",
                 "stdout": "shard tests.test_last: 1s exit=0\n" + "o" * 2000, "stderr": "make: *** [test] Error 1",
                 "output_path": "/tmp/fixture/.git/sd-check-output/run-check.log", "failed_shards": [shard]}]
        failed = {**GATE_FAILED, "check": {"status": "fail", "exit_code": 1, "detail": "", "checks": rows}}
        review, _process = self.context(report_changes=failed)
        with self.assertRaisesRegex(ship.Refusal, "no review pass was spent") as caught:
            review.review(HEAD)
        message = str(caught.exception)
        self.assertIn(f"check (exit 2): failed {shard}\nwhole output: /tmp/fixture/.git/sd-check-output/run-check.log",
                      message)
        kept = review.state["review_preflight_error"]["checks"][0]
        self.assertEqual((kept["failed_shards"], kept["output_path"]),
                         ([shard], "/tmp/fixture/.git/sd-check-output/run-check.log"))
        self.assertNotIn("check (exit 0)", message)

    def test_the_refusal_names_a_failed_step_outside_any_shard_and_its_failure(self):
        """sd:2608. A gate that failed in a make target, not a shard, was
        refused with `failed_shards: []` and only the tails, which held passing
        shards."""
        rows = [{"name": "check", "status": "fail", "exit_code": 2, "reason": "",
                 "stdout": "shard tests.test_last: 1s exit=0\n" + "o" * 2000, "stderr": "make: *** [docs-lint] Error 1",
                 "output_path": "/tmp/fixture/.git/sd-check-output/run-check.log", "failed_shards": [],
                 "failed_steps": ["make target docs-lint"],
                 "failure": "docs-lint: README.md cites a missing anchor\nmake: *** [docs-lint] Error 1"}]
        failed = {**GATE_FAILED, "check": {"status": "fail", "exit_code": 1, "detail": "", "checks": rows}}
        review, _process = self.context(report_changes=failed)
        with self.assertRaisesRegex(ship.Refusal, "no review pass was spent") as caught:
            review.review(HEAD)
        message = str(caught.exception)
        # The path and the two tails keep their sd:2066 order; the step and the failing lines follow.
        self.assertRegex(message, r"check \(exit 2\): whole output: /tmp/fixture/\.git/sd-check-output/run-check\.log\n"
                                  r"stderr: make: \*\*\* \[docs-lint\] Error 1\nstdout: \.\.\.o+\n"
                                  r"failed step: make target docs-lint\n"
                                  r"failure: docs-lint: README\.md cites a missing anchor")
        kept = review.state["review_preflight_error"]["checks"][0]
        self.assertEqual((kept["failed_steps"], kept["failure"]), (rows[0]["failed_steps"], rows[0]["failure"]))

    def test_the_refusal_leads_with_the_failing_suite_and_keeps_the_slot_wait_as_context(self):
        """sd:2687. sd:2671's refusal opened with the gate-slot wait line, and the
        lane, which keeps the refusal's head, recorded that as the failure."""
        wait = ("waiting for a gate slot: the last gate started 13s ago; starts are 45s apart under /tmp/slots; "
                "slot 1 held by sd-check tree (pid 31023) since 2026-10-05T07:28:35Z\n"
                "gate slot taken after 35s under /tmp/slots")
        summary = "sd-check fail (check fail: suite tools-3; make target check) whole output: /tmp/run-check.log"
        row = {"name": "check", "status": "fail", "exit_code": 2, "stdout": "", "stderr": "c" * 2000,
               "output_path": "/tmp/run-check.log", "failed_shards": [],
               "failed_steps": ["suite tools-3", "make target check"],
               "failure": "FAIL: test_sd2687_marker (tests.test_obsidian_review.DigestTest)\nFAILED (failures=1)"}
        cleared = {**GATE_FAILED, "outcomes": [{"backend": "automatic", "status": "clean"}],
                   "reviewed_by": ["automatic"], "completed_reviews": 1,
                   "check": {"status": "fail", "exit_code": 1, "detail": wait, "summary": summary, "checks": [row]}}
        review, _process = self.context(report_changes=cleared)
        with self.assertRaises(ship.Refusal) as caught:
            review.review(HEAD)
        message = str(caught.exception)
        self.assertIn(f"so the next prepare reviews again: {summary}\ncheck (exit 2): ", message)
        self.assertLess(message.index("FAIL: test_sd2687_marker"), message.index("waiting for a gate slot"))
        self.assertIn("gate output: waiting for a gate slot", message)

    def test_the_lanes_cut_of_the_refusal_names_the_failing_test_when_a_later_suite_passed(self):
        """sd:3476. A system `make check` ends with whichever suite finished last, so 8 of 9 lane
        reasons read `Ran 24 tests in 3.2s OK` and the failing test was only in the log."""
        failed = "FAIL: test_sd3476_marker (tests.test_obsidian_review.DigestTest)\nTraceback (most recent call last):\n" \
                 "AssertionError: 1 != 2\n"
        passing = "." * 3000 + "\nRan 24 tests in 3.2s\n\nOK\n"
        row = {"name": "check", "status": "fail", "exit_code": 2, "stdout": "o" * 3000, "stderr": failed + passing,
               "output_path": "/tmp/run-check.log", "failed_shards": [], "failed_steps": ["suite leg-shared"],
               "failure": failed + "...\n" + passing[-600:]}
        summary = "sd-check fail (check fail: suite leg-shared; make target check) whole output: /tmp/run-check.log"
        review, _process = self.context(report_changes={**CLEARED, "check": {"status": "fail", "exit_code": 1,
                                                                             "summary": summary, "checks": [row]}})
        with self.assertRaises(ship.Refusal) as caught:
            review.review(HEAD)
        kept = str(caught.exception)[:2000]  # sd_lane.REASON_CHARS, the cut a failed entry's reason keeps
        for line in ("FAIL: test_sd3476_marker (tests.test_obsidian_review.DigestTest)", "AssertionError: 1 != 2"):
            self.assertIn(f"failing: {line}", kept)

    def test_the_refusal_leaves_out_failing_lines_the_tails_already_hold(self):
        """sd:2608. A short run's failure lines are its tails; saying them twice only lengthens the refusal."""
        row = {"name": "check", "status": "fail", "exit_code": 2, "stdout": "sd2066-out",
               "stderr": "sd2066-err\nmake[1]: *** [check] Error 1", "failed_shards": [], "output_path": "",
               "failed_steps": ["make target check"], "failure": "sd2066-out\n\nmake[1]: *** [check] Error 1"}
        [named] = sd_ship_review.failing_check_tails([row])
        self.assertTrue(named.endswith("stdout: sd2066-out\nfailed step: make target check"), named)

    def test_the_next_prepare_reviews_without_a_retry_flag(self):
        failed, _process = self.context(report_changes=GATE_FAILED)
        with self.assertRaises(ship.Refusal):
            failed.review(HEAD)
        review, _process = self.context(state=failed.state)
        review.review(HEAD)
        self.assertEqual(len(review.state["passes"]), 1)
        self.assertEqual(review.state["passes"][0]["report"]["status"], "clean")
        self.assertIsNone(review.state["review_preflight_error"])

    def test_a_gate_failure_on_a_fix_verification_keeps_the_earlier_pass_only(self):
        reviewed, _process = self.context()
        reviewed.review(HEAD)
        original = copy.deepcopy(reviewed.state["passes"])
        fixed = "c" * 40
        failed, _process = self.context(state=reviewed.state, head=fixed, report_changes=GATE_FAILED)
        with unittest.mock.patch("sd_ship_review.is_ancestor", return_value=True), self.assertRaises(ship.Refusal):
            failed.review(fixed)
        self.assertEqual(failed.state["passes"], original)

    def test_a_released_re_review_restores_the_binding_it_superseded(self):
        """Local review finding on sd:1475: dispatch saves the new binding before the gate runs.

        Popping the pass alone left the new binding stored, so the next prepare
        no longer saw the policy change and refused with "this head was already
        reviewed" instead of re-reviewing under the new policy.
        """
        reviewed, _process = self.context()
        reviewed.review(HEAD)
        original = copy.deepcopy(reviewed.state)
        failed, _process = self.context(state=reviewed.state, report_changes=GATE_FAILED)
        failed.runtime = dataclasses.replace(failed.runtime, binding=lambda _root: "moved",
                                             manifest=lambda _root: {"schema": "moved"})
        with unittest.mock.patch("sd_ship_review.is_ancestor", return_value=True), \
                self.assertRaisesRegex(ship.Refusal, "no review pass was spent"):
            failed.review(HEAD)
        for key in ("passes", "binding", "binding_manifest", "head", "reviewed_head", "review_clearance"):
            self.assertEqual(failed.state.get(key), original.get(key), key)
        again, process = self.context(state=failed.state)
        again.runtime = dataclasses.replace(again.runtime, binding=lambda _root: "moved")
        with unittest.mock.patch("sd_ship_review.is_ancestor", return_value=True):
            again.review(HEAD)
        self.assertEqual(process.call_count, 2)
        self.assertEqual(len(again.state["passes"]), 2)
        self.assertIn("review_binding_change", again.state["passes"][1])

    def test_a_gate_failure_after_a_clean_review_releases_the_pass(self):
        """sd:2605. The gate runs after a review that cleared; when it fails the
        head cannot ship, and the next prepare reviews the fixed branch again."""
        cleared = {**GATE_FAILED, "outcomes": [{"backend": "automatic", "status": "clean"}],
                   "reviewed_by": ["automatic"], "completed_reviews": 1}
        review, _process = self.context(report_changes=cleared)
        with self.assertRaisesRegex(ship.Refusal, "after the review cleared; the review pass was released") as caught:
            review.review(HEAD)
        self.assertIn("timed out after 900s", str(caught.exception))
        self.assertEqual(review.state["passes"], [])
        self.assertIsNone(review.state.get("reviewed_head"))
        self.assertEqual(review.state["review_preflight_error"]["kind"], "gate_failed")

    def test_a_blocking_finding_beside_a_failed_gate_still_spends_its_pass(self):
        """Not a gate failure: the finding refuses the head on its own."""
        finding = {"path": "a.py", "line": 1, "severity": "high", "summary": "s", "disposition": "blocking"}
        blocked = {**GATE_FAILED, "outcomes": [{"backend": "automatic", "status": "findings"}],
                   "reviewed_by": ["automatic"], "completed_reviews": 1, "findings": [finding]}
        review, _process = self.context(report_changes=blocked)
        with self.assertRaises(ship.Refusal) as caught:
            review.review(HEAD)
        self.assertNotIn("pass was released", str(caught.exception))
        self.assertEqual(len(review.state["passes"]), 1)


#: What sd-review reports when the review cleared and the gate then failed (sd:2721).
CLEARED = {**GATE_FAILED, "cleared_status": "clean", "outcomes": [{"backend": "automatic", "status": "clean"}],
           "reviewed_by": ["automatic"], "completed_reviews": 1}


class AClearedReviewOutlivesItsGate(unittest.TestCase):
    """sd:2721. A gate that fails after a cleared review keeps the review: sd:2671 ran three full reviews for load flakes."""

    context = provider_tests.ProviderSelection.context

    def kept(self) -> dict:
        review, _process = self.context(report_changes=CLEARED)
        with self.assertRaisesRegex(ship.Refusal, "after the review cleared; the review pass is kept") as caught:
            review.review(HEAD)
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "gate_failed")
        self.assertIn("timed out after 900s", str(caught.exception))
        return review.state

    def gate(self, status: str):
        check = {"status": status, "exit_code": 0 if status == "pass" else 1, "detail": "gate-again", "checks": None,
                 "summary": f"sd-check {status}"}
        return unittest.mock.patch.object(sd_ship_review, "adjudicated_check", return_value=check)

    def test_the_failure_keeps_the_pass_and_names_no_reviewed_head(self):
        state = self.kept()
        self.assertEqual([entry["report"]["status"] for entry in state["passes"]], ["gate_failed"])
        self.assertIsNone(state.get("reviewed_head"))
        self.assertEqual(state["review_preflight_error"]["kind"], "gate_failed")

    def test_a_review_short_of_its_depth_is_released_as_before(self):
        review, _process = self.context(report_changes={**CLEARED, "completed_reviews": 0})
        with self.assertRaisesRegex(ship.Refusal, "the review pass was released"):
            review.review(HEAD)
        self.assertEqual(review.state["passes"], [])

    def test_the_next_prepare_at_that_head_runs_only_the_gate(self):
        review, process = self.context(state=self.kept())
        with self.gate("pass") as gate:
            review.review(HEAD)
        self.assertEqual((process.call_count, gate.call_args.args[1]), (0, HEAD))
        [entry] = review.state["passes"]
        self.assertEqual((entry["report"]["status"], entry["report"]["check"]["status"]), ("clean", "pass"))
        self.assertEqual(entry["report"]["failed_check"], GATE_FAILED["check"])
        self.assertEqual(review.state["reviewed_head"], HEAD)
        self.assertIsNone(review.state["review_preflight_error"])

    def test_a_gate_that_fails_again_keeps_the_pass_and_refuses(self):
        review, process = self.context(state=self.kept())
        with self.gate("fail"), self.assertRaisesRegex(ship.Refusal, "failed again at .*; its review pass stays kept"):
            review.review(HEAD)
        self.assertEqual(process.call_count, 0)
        self.assertEqual([entry["report"]["status"] for entry in review.state["passes"]], ["gate_failed"])
        self.assertIsNone(review.state.get("reviewed_head"))

    def caught_up(self, status: str):
        """The kept pass met at a clean catch-up merge of its head, as the lane's `--catch-up` prepare makes (sd:3474)."""
        head = "d" * 40
        review, process = self.context(state={**self.kept(), "base": "main"}, head=head)
        carried = {"from": HEAD, "to": head, "patch_id": "e" * 40, "merges": [head]}
        with self.gate(status) as gate, unittest.mock.patch.object(sd_ship_review, "carry_forward", return_value=carried), \
                unittest.mock.patch("sd_ship_review.is_ancestor", return_value=True):
            if status == "pass":
                review.review(head)
            else:
                with self.assertRaisesRegex(ship.Refusal, "failed again at .*; its review pass stays kept"):
                    review.review(head)
        self.assertEqual(process.call_count, 0, "a reviewer was asked")
        self.assertEqual(gate.call_args.args[1], head)
        return review, head

    def test_a_clean_catch_up_of_that_head_runs_only_the_gate(self):
        """sd:3474. The lane prepares with --catch-up, so a busy base moved the head
        before the retry: 26 of 32 kept passes paid a full review there."""
        review, head = self.caught_up("pass")
        [entry] = review.state["passes"]
        self.assertEqual((entry["head"], entry["report"]["status"], entry["report"]["check"]["status"]),
                         (HEAD, "clean", "pass"))
        self.assertEqual((review.state["reviewed_head"], review.state["review_carry_forward"]["from"]), (head, HEAD))
        self.assertIsNone(review.state["review_preflight_error"])

    def test_a_gate_that_fails_at_the_catch_up_keeps_the_pass_and_records_no_carry(self):
        review, _head = self.caught_up("fail")
        self.assertEqual([entry["report"]["status"] for entry in review.state["passes"]], ["gate_failed"])
        self.assertIsNone(review.state.get("reviewed_head"))
        self.assertNotIn("review_carry_forward", review.state)

    def test_a_catch_up_that_does_not_carry_is_reviewed(self):
        """A base change beside the branch's files, or a merge with content of its own: `carry_forward` says no."""
        head = "d" * 40
        review, process = self.context(state={**self.kept(), "base": "main"}, head=head)
        with self.gate("pass") as gate, unittest.mock.patch.object(sd_ship_review, "carry_forward", return_value=None), \
                unittest.mock.patch("sd_ship_review.is_ancestor", return_value=True):
            review.review(head)
        self.assertEqual((process.call_count, gate.call_count), (2, 0))
        self.assertEqual(len(review.state["passes"]), 2)

    def test_a_fix_after_it_is_verified_as_a_delta(self):
        fixed = "c" * 40
        review, process = self.context(state=self.kept(), head=fixed)
        with unittest.mock.patch("sd_ship_review.is_ancestor", return_value=True):
            review.review(fixed)
        argv = process.call_args_list[-1].args[1]
        self.assertEqual(argv[argv.index("--base") + 1], HEAD)
        self.assertIn("--verify-report", argv)
        # sd:3059: a killed `sd-ship` skips the folder's cleanup; its pid in the name lets the next gate start remove it.
        self.assertRegex(pathlib.Path(argv[argv.index("--verify-report") + 1]).parent.name, rf"sd-ship-verify-{os.getpid()}-")
        self.assertEqual(len(review.state["passes"]), 2)
        self.assertEqual(review.state["reviewed_head"], fixed)


NOT_RUN = {"status": "not_run", "exit_code": None, "reason": "the review is blocking; the gate runs on a head it does not block"}
FINDING = {"path": "a.py", "line": 1, "severity": "high", "summary": "wrong", "family": "correctness",
           "disposition": "blocking", "backend": "automatic"}


class ABlockingReviewRunsNoGate(unittest.TestCase):
    """sd:2605. sd-review runs the gate only after a review that does not block."""

    context = provider_tests.ProviderSelection.context

    def blocked(self) -> dict:
        return {"status": "blocking", "check": dict(NOT_RUN), "findings": [dict(FINDING)],
                "outcomes": [{"backend": "automatic", "status": "findings"}]}

    def test_the_refusal_says_the_gate_did_not_run_and_carries_the_check(self):
        review, _process = self.context(report_changes=self.blocked())
        review.args.item = 42
        with self.assertRaises(ship.Refusal) as caught:
            review.review(HEAD)
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "review_blocking")
        self.assertIn("the gate did not run, so no test has passed at this head", str(caught.exception))
        self.assertEqual(caught.exception.details["check"], NOT_RUN)
        self.assertEqual(review.state["passes"][-1]["report"]["check"], NOT_RUN)

    def test_a_blocked_report_with_no_gate_is_complete(self):
        report = {**self.blocked(), "subject": {"head": HEAD}, "scope": "branch",
                  "requested_reviews": 1, "completed_reviews": 1}
        last = {"head": HEAD, "report": report, "exit_code": 1}
        self.assertIs(sd_ship_review.complete_report(last, HEAD, None), report)

    def test_a_clean_report_with_no_gate_is_not(self):
        report = {"status": "clean", "check": dict(NOT_RUN), "findings": [], "subject": {"head": HEAD},
                  "scope": "branch", "requested_reviews": 1, "completed_reviews": 1}
        last = {"head": HEAD, "report": report, "exit_code": 0}
        with self.assertRaisesRegex(ship.Refusal, "incomplete"):
            sd_ship_review.complete_report(last, HEAD, HEAD)


class TheRealGateFailureIsRecognised(review_tests.ReviewFixture):
    """The predicate read against what sd-review emits, not a hand-built report."""

    # Imported as a module, so the loader does not collect its classes here.
    prepare = review_tests.PipelineTests.prepare
    run_review = review_tests.PipelineTests.run_review

    def test_a_failing_unittest_gate_keeps_its_output_in_the_receipt(self):
        """sd:1484. `check.detail` is sd-check's own stderr, empty when a test
        fails; the failure is in `checks[].stdout/stderr`. The release kept
        only `detail`, so the receipt said the gate failed and not why, and
        learning why meant running the gate again."""
        root = self.make_repo()
        (root / "test_gate.py").write_text(
            "import unittest\n\n\nclass Gate(unittest.TestCase):\n"
            "    def test_sd1484_marker(self):\n        self.assertEqual(1, 2)\n", encoding="utf-8")
        self.local_block(root, f"check: {sys.executable} -m unittest test_gate")
        check = sd_review.run_check(root, sd_review.subprocess_runner, self.environment(), 60)
        self.assertEqual(check["status"], "fail")
        review, _process = GateFailureSpendsNoPass.context(self, report_changes={**GATE_FAILED, "check": check})
        with self.assertRaisesRegex(ship.Refusal, "no review pass was spent") as caught:
            review.review(HEAD)
        receipt = review.state["review_preflight_error"]
        kept = receipt["checks"]
        self.assertEqual([(c["name"], c["status"], c["exit_code"]) for c in kept], [("check", "fail", 1), ("test", "absent", None), ("lint", "absent", None)])
        self.assertIn("FAIL: test_sd1484_marker", kept[0]["stderr"])
        self.assertIn("AssertionError: 1 != 2", str(caught.exception))

    def test_the_kept_output_is_bounded(self):
        noisy = {"name": "test", "status": "fail", "exit_code": 1, "reason": "",
                 "stdout": "o" * 10000 + "END-OUT", "stderr": "e" * 10000 + "END-ERR", "command": ["make", "test"]}
        report = {**GATE_FAILED, "check": {**GATE_FAILED["check"], "checks": [noisy] * 5}}
        review, _process = GateFailureSpendsNoPass.context(self, report_changes=report)
        with self.assertRaises(ship.Refusal):
            review.review(HEAD)
        kept = review.state["review_preflight_error"]["checks"]
        self.assertEqual(len(kept), len(sd_ship_review.sd_lib.CHECK_NAMES))
        for record in kept:
            self.assertEqual(len(record["stdout"]), 4096)
            self.assertTrue(record["stdout"].endswith("END-OUT"))
            self.assertTrue(record["stderr"].endswith("END-ERR"))

    def test_sd_review_s_gate_failure_is_the_shape_sd_ship_releases(self):
        root = self.make_repo()
        self.prepare(root)
        runner = FakeRunner({"sd-check": sd_review.Completed(1, "{}", "timed out after 900s")})
        result = self.run_review(root, runner)
        self.assertEqual(result["status"], "gate_failed")
        self.assertTrue(sd_ship_review.released_gate_failure(result, sd_review.EXIT_GATE))


class PrepareRunsTheLocalGate(unittest.TestCase):
    """sd:2041. Under `repo.ci = local`, prepare's check is the merge gate's, so its receipt serves the merge."""

    def argv(self, ci: str, state: dict) -> list[str]:
        review, process = provider_tests.ProviderSelection.context(self, state=state)
        with unittest.mock.patch.object(sd_ship_review.sd_lib, "repo_ci", lambda connection, root: ci):
            review.review(HEAD)
        return process.call_args_list[-1].args[1]

    def test_a_local_repository_asks_for_the_gate_check_against_its_base(self):
        argv = self.argv("local", {"passes": [], "base": "main"})
        self.assertEqual(argv[argv.index("--gate-check") + 1], "main")

    def test_a_github_repository_runs_the_check_as_before(self):
        self.assertNotIn("--gate-check", self.argv("github", {"passes": [], "base": "main"}))

    def test_a_docs_only_failure_keeps_its_docs_row(self):
        rows = [{"name": name, "status": "skipped"} for name in sd_ship_review.sd_lib.CHECK_NAMES]
        rows.append({"name": "docs", "status": "fail", "exit_code": 1, "stdout": "broken link"})
        kept = sd_ship_review.gate_diagnostics({"checks": rows}, 4096)
        self.assertEqual([record["name"] for record in kept], [*sd_ship_review.sd_lib.CHECK_NAMES, "docs"])
        self.assertEqual(kept[-1]["stdout"], "broken link")


class TimingPlanCarriesTheGatesBound(unittest.TestCase):
    """sd:2041. The watchdog counts the gate's own bound once, beside each reviewer's phase."""

    TIMING = {"setup_seconds": 3600, "phase_seconds": 1800, "check_seconds": 3600, "execution_seconds": 10800,
              "candidates": [{"name": "a", "recipient": "a@fixture"}, {"name": "b", "recipient": "b@fixture"}]}

    def plan(self, timing: dict) -> dict:
        report = {"status": "explained", "requested_reviews": 2, "timing": timing}
        return ship.timing_plan(ship.subprocess.CompletedProcess([], 0, ship.json.dumps(report), ""))

    def test_a_plan_with_a_gate_bound_is_accepted(self):
        self.assertEqual(self.plan(self.TIMING)["execution_seconds"], 10800)

    def test_a_total_that_does_not_count_the_gate_bound_is_refused(self):
        for change in ({"execution_seconds": 9000}, {"check_seconds": 0}, {"check_seconds": True},
                       {"check_seconds": 3600.0}):
            with self.subTest(change=change), self.assertRaises(ship.Refusal):
                self.plan({**self.TIMING, **change})


class TimingPlanCarriesTheSlotBound(TimingPlanCarriesTheGatesBound):
    """sd:2611. The gate-slot wait is its own phase; a plan from before it has none."""

    SLOT = {**TimingPlanCarriesTheGatesBound.TIMING, "slot_seconds": 14400, "execution_seconds": 10800 + 14400}

    def test_a_plan_that_counts_the_slot_bound_is_accepted(self):
        self.assertEqual(self.plan(self.SLOT)["execution_seconds"], 25200)

    def test_a_plan_without_a_slot_bound_is_accepted_as_before(self):
        self.assertNotIn("slot_seconds", self.TIMING)
        self.assertEqual(self.plan(self.TIMING)["execution_seconds"], 10800)

    def test_a_slot_bound_the_total_does_not_count_or_that_is_not_a_count_is_refused(self):
        for change in ({"execution_seconds": 10800}, {"slot_seconds": -1, "execution_seconds": 10799},
                       {"slot_seconds": True, "execution_seconds": 10801}, {"slot_seconds": 14400.0}):
            with self.subTest(change=change), self.assertRaises(ship.Refusal):
                self.plan({**self.SLOT, **change})


class ReviewTimeoutReachesTheGate(unittest.TestCase):
    def test_sd_review_hands_its_phase_budget_to_sd_check(self):
        """The phase budget sd-review plans for the gate is the limit sd-check uses."""
        runner = FakeRunner({})
        sd_review.run_check(provider_tests.fixture.ROOT, runner, {}, 2400)
        argv = runner.calls[0]["argv"]
        self.assertEqual(argv[argv.index("--timeout") + 1], "2400")

    def test_prepare_forwards_review_timeout_to_both_sd_review_stages(self):
        review, process = provider_tests.ProviderSelection.context(self)
        review.args.review_timeout = 3000
        review.review(HEAD)
        for call in process.call_args_list:
            argv = call.args[1]
            self.assertEqual(argv[argv.index("--timeout") + 1], "3000")

    def test_prepare_without_review_timeout_forwards_none(self):
        review, process = provider_tests.ProviderSelection.context(self)
        review.review(HEAD)
        self.assertTrue(all("--timeout" not in call.args[1] for call in process.call_args_list))

    def test_the_parser_takes_a_positive_review_timeout_on_every_dispatch_surface(self):
        for args in (["prepare", "--item", "42"], ["prepare", "--no-item", "--review-id", "record"],
                     ["review", "--no-item", "--review-id", "record"]):
            with self.subTest(args=args):
                self.assertIsNone(ship.parser().parse_args(args).review_timeout)
                self.assertEqual(ship.parser().parse_args([*args, "--review-timeout", "3000"]).review_timeout, 3000)
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    ship.parser().parse_args([*args, "--review-timeout", "0"])


#: A gate whose slot wait ran out at the lane's bound after a cleared review: no check ran (pack sd:7412).
SLOT_RAN_OUT = {**CLEARED, "check": {**CLEARED["check"], "detail": "no gate slot came free within 600s",
                                     "slot_ran_out": True}}
SLOT_CHECK = {"status": "fail", "exit_code": 1, "detail": "no gate slot came free within 600s", "checks": None,
              "summary": "sd-check fail", "slot_ran_out": True}


class ASlotWaitThatRanOut(unittest.TestCase):
    """pack sd:7412. The lane bounds the gate's slot wait by its run budget; a wait that ran out ran no gate, so the
    lane puts the entry back rather than fail it, and the kept review serves its next prepare."""

    context = provider_tests.ProviderSelection.context

    def refused(self, review, head=HEAD):
        with self.assertRaises(ship.Refusal) as caught:
            review.review(head)
        blocker = caught.exception.workflow["blocker"]
        self.assertEqual((blocker["code"], blocker["retryable"]), ("gate_slot_wait", True))
        return str(caught.exception)

    def test_it_keeps_the_cleared_review_and_names_its_own_code(self):
        review, _process = self.context(report_changes=SLOT_RAN_OUT)
        self.assertIn("the review pass is kept", self.refused(review))
        self.assertEqual([entry["report"]["status"] for entry in review.state["passes"]], ["gate_failed"])

    def test_the_kept_reviews_gate_that_waits_again_names_it_too(self):
        review, process = self.context(state=AClearedReviewOutlivesItsGate.kept(self))
        with unittest.mock.patch.object(sd_ship_review, "adjudicated_check", return_value=SLOT_CHECK) as gate:
            self.assertIn("its review pass stays kept", self.refused(review))
        self.assertEqual(process.call_count, 0)
        self.assertEqual(gate.call_args.kwargs["slot_timeout"], ship.sd_lib.GATE_SLOT_SECONDS)

    def test_a_gate_that_failed_its_checks_keeps_the_gate_failed_code(self):
        review, _process = self.context(state=AClearedReviewOutlivesItsGate.kept(self))
        failed = {**SLOT_CHECK, "slot_ran_out": False, "detail": "a test failed"}
        with unittest.mock.patch.object(sd_ship_review, "adjudicated_check", return_value=failed), \
                self.assertRaises(ship.Refusal) as caught:
            review.review(HEAD)
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "gate_failed")

    def test_prepare_forwards_the_slot_bound_to_sd_review_and_its_own_gate(self):
        review, process = self.context(state=AClearedReviewOutlivesItsGate.kept(self))
        review.args.slot_timeout = 600
        with unittest.mock.patch.object(sd_ship_review, "adjudicated_check", return_value=SLOT_CHECK) as gate:
            self.refused(review)
        self.assertEqual(gate.call_args.kwargs["slot_timeout"], 600)
        review, process = self.context()
        review.args.slot_timeout = 600
        review.review(HEAD)
        for call in process.call_args_list:
            argv = call.args[1]
            self.assertEqual(argv[argv.index("--slot-timeout") + 1], "600")

    def test_the_parser_takes_a_slot_bound_up_to_the_gates_own(self):
        cap = ship.sd_lib.GATE_SLOT_SECONDS
        for args in (["prepare", "--item", "42"], ["prepare", "--no-item", "--review-id", "record"],
                     ["review", "--no-item", "--review-id", "record"]):
            with self.subTest(args=args):
                self.assertIsNone(ship.parser().parse_args(args).slot_timeout)
                self.assertEqual(ship.parser().parse_args([*args, "--slot-timeout", str(cap)]).slot_timeout, cap)
                for bad in ("0", "-1", str(cap + 1), "soon"):
                    with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                        ship.parser().parse_args([*args, "--slot-timeout", bad])

    def test_the_adjudicated_gate_queues_on_the_bound_and_says_when_it_ran_out(self):
        gate_run = importlib.import_module("sd_gate_run")
        ran_out = {"status": "failure", "exit_code": 1, "head": HEAD, "stderr": "",
                   "report": {"checks": [], "gate_slot": {"timed_out": True}}}
        with unittest.mock.patch.object(gate_run, "check_in_worktree", return_value=ran_out) as gate:
            check = sd_ship_review.adjudicated_check(pathlib.Path("."), HEAD, None, None, 60, slot_timeout=600)
        self.assertEqual((gate.call_args.kwargs["slot_timeout"], check["status"], check.get("slot_ran_out")),
                         (600, "fail", True))
        with unittest.mock.patch.object(gate_run, "check_in_worktree", return_value={**ran_out, "report": {}}):
            check = sd_ship_review.adjudicated_check(pathlib.Path("."), HEAD, None, None, 60)
        self.assertNotIn("slot_ran_out", check)


#: A gate that failed in one test of one shard, as `sd-check` reports it (sd:9768).
FLAKY_TEST = "tests.test_example.Case.test_a"
SHARD = "shard tests.test_example: 4s exit=1"
#: The notes a flake and a failed rerun write.
FLAKE, RERUN_FAILED = "Gate flake: ", "Gate rerun failed: "
OUTPUT = ("shard tests.test_other: start\nOK\nshard tests.test_other: 2s exit=0\n"
          f"shard tests.test_example: start\nFAIL: test_a ({FLAKY_TEST})\nAssertionError: git failed\n"
          f"Ran 3 tests\nFAILED (failures=1)\n{SHARD}\nmake: *** [test] Error 1\n")


class AFlakyGateTestIsRerunAlone(unittest.TestCase):
    """sd:9768. A gate that failed in tests that pass alone on one rerun records a flake and goes on."""

    context = provider_tests.ProviderSelection.context

    def setUp(self):
        import tempfile

        from sd_db import connect, create_item, initialise
        from sd_db.writes import upsert_repo

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = pathlib.Path(scratch.name)
        initialise(root / "sd.db")
        self.db = connect(root / "sd.db")
        self.addCleanup(self.db.close)
        upsert_repo(self.db, str(root / "repo"))
        self.item = create_item(self.db, kind="task", title="Example entry", repo=str(root / "repo"))
        (root / "check.log").write_text(OUTPUT, encoding="utf-8")
        self.row = {"name": "check", "status": "fail", "exit_code": 2, "stdout": "", "stderr": "",
                    "output_path": str(root / "check.log"), "failed_shards": [SHARD],
                    "failed_steps": [SHARD, "make target test"], "failure": OUTPUT[-200:]}

    def prepared(self, rerun=(0, "OK", ""), state=None, check=None):
        """One prepare whose cleared review's gate failed in FLAKY_TEST, the rerun answering `rerun`."""
        failed = {"status": "fail", "exit_code": 1, "detail": "", "checks": [check or self.row]}
        review, _process = self.context(report_changes={**CLEARED, "check": failed}, state=state)
        review.connection, review.identity = self.db, ship.ItemIdentity(self.item)
        with unittest.mock.patch.object(sd_ship_review, "rerun_tests", return_value=rerun, create=True) as rerun_mock:
            try:
                review.review(HEAD)
            except ship.Refusal as refusal:
                return review, rerun_mock, refusal
        return review, rerun_mock, None

    def notes(self, prefix):
        return [body for (body,) in self.db.execute("SELECT body FROM note WHERE substr(body, 1, ?) = ?",
                                                    (len(prefix), prefix))]

    def test_a_flaky_test_passes_on_rerun_and_the_entry_proceeds_with_a_flake_record(self):
        review, rerun, refused = self.prepared()
        self.assertIsNone(refused)
        self.assertEqual(rerun.call_args.args[1:], (HEAD, [FLAKY_TEST]), "only the failing test, alone, at head")
        [entry] = review.state["passes"]
        self.assertEqual((entry["report"]["status"], entry["report"]["check"]["status"]), ("clean", "pass"))
        self.assertEqual(entry["report"]["failed_check"]["status"], "fail")
        self.assertEqual(review.state["reviewed_head"], HEAD)
        self.assertEqual((review.state["gate_flake"]["head"], review.state["gate_flake"]["tests"]), (HEAD, [FLAKY_TEST]))
        [note] = self.notes(FLAKE)
        self.assertIn(f"{FLAKY_TEST} failed the gate at {HEAD[:12]} and passed alone", note)
        listed = self.db.execute("SELECT id FROM item WHERE external_id = 'flake:tests.test_example'").fetchone()
        self.assertIsNotNone(listed, "the flake list holds the suite")
        self.assertEqual(len(self.notes(f"Gate flake in fixture/repo: {FLAKY_TEST}")), 1)

    def test_a_real_failure_still_fails_and_is_noted(self):
        review, rerun, refused = self.prepared(rerun=(1, f"FAIL: test_a ({FLAKY_TEST})\nFAILED", ""))
        self.assertIsNotNone(refused)
        self.assertIn("after the review cleared; the review pass is kept", str(refused))
        self.assertEqual(rerun.call_count, 1)
        self.assertIsNone(review.state.get("reviewed_head"))
        self.assertEqual(len(self.notes(f"{RERUN_FAILED}{FLAKY_TEST} at ")), 1)
        self.assertEqual(self.notes(FLAKE), [])

    def test_a_second_flake_on_the_same_entry_fails(self):
        _review, rerun, refused = self.prepared(state={"passes": [], "gate_flake": {"head": HEAD, "tests": []}})
        self.assertIsNotNone(refused)
        self.assertEqual(rerun.call_count, 0)

    def test_a_test_that_failed_its_rerun_this_week_is_not_rerun(self):
        from sd_db.writes import add_note

        add_note(self.db, self.item, "comment", f"{RERUN_FAILED}{FLAKY_TEST} at 0123456789ab: x")
        _review, rerun, refused = self.prepared()
        self.assertIsNotNone(refused)
        self.assertEqual(rerun.call_count, 0)

    def test_a_rerun_failure_of_a_longer_named_test_does_not_bar_this_one(self):
        """A name that extends FLAKY_TEST is another test: matched whole, never by prefix."""
        from sd_db.writes import add_note

        add_note(self.db, self.item, "comment", f"{RERUN_FAILED}{FLAKY_TEST}b at 0123456789ab")
        _review, rerun, refused = self.prepared()
        self.assertIsNone(refused)
        self.assertEqual(rerun.call_count, 1)

    def test_the_kept_reviews_gate_alone_takes_the_flake_retry_too(self):
        """The next prepare at the head runs only the gate (`rerun_gate`): a flake there goes on as well."""
        state = AClearedReviewOutlivesItsGate.kept(self)
        review, _process = self.context(state=state)
        review.connection, review.identity = self.db, ship.ItemIdentity(self.item)
        failed = {"status": "fail", "exit_code": 1, "detail": "", "checks": [self.row]}
        with unittest.mock.patch.object(sd_ship_review, "adjudicated_check", return_value=failed), \
                unittest.mock.patch.object(sd_ship_review, "rerun_tests", return_value=(0, "OK", ""), create=True):
            review.review(HEAD)
        self.assertEqual(review.state["reviewed_head"], HEAD)
        self.assertEqual(review.state["passes"][-1]["report"]["check"]["flake"]["tests"], [FLAKY_TEST])

    def test_only_a_failure_of_tests_alone_is_rerun(self):
        lint = {**self.row, "failed_steps": [SHARD, "make target lint"]}
        crashed = {**self.row, "failed_steps": ["shard tests.test_crash: 1s exit=1", "make target test"]}
        hung = {**self.row, "failed_steps": ["shard tests.test_example: unfinished"]}
        many = {**self.row, "output_path": None, "failed_steps": ["make target test"],
                "failure": "".join(f"FAIL: test_{n} (tests.m.C.test_{n})\n" for n in range(6))}
        self.assertEqual(sd_ship_review.failed_tests({"checks": [self.row]}), [FLAKY_TEST])
        for name, row in (("lint", lint), ("crashed", crashed), ("hung", hung), ("many", many)):
            with self.subTest(name):
                self.assertEqual(sd_ship_review.failed_tests({"checks": [row]}), [])
        _review, rerun, refused = self.prepared(check=lint)
        self.assertIsNotNone(refused)
        self.assertEqual(rerun.call_count, 0)

    def test_a_timed_out_gate_is_not_taken_for_a_flake(self):
        """A row that timed out after a failing shard finished never ran its other shards: it is no flake."""
        timed_out = {**self.row, "exit_code": None, "reason": "timed out after 900s"}
        unstarted = {**self.row, "exit_code": None, "reason": "cannot run make: not found"}
        for name, row in (("timed out", timed_out), ("unstarted", unstarted)):
            with self.subTest(name):
                self.assertEqual(sd_ship_review.failed_tests({"checks": [row]}), [])
        _review, rerun, refused = self.prepared(check=timed_out)
        self.assertIsNotNone(refused)
        self.assertEqual(rerun.call_count, 0)

    def test_a_gate_whose_runner_also_failed_is_no_flake(self):
        """`run-tests.sh` failing to publish its results gives the same shard and step lines as a failed test:
        the test passing alone must not clear that second fault."""
        log = pathlib.Path(self.row["output_path"])
        log.write_text(OUTPUT + "error: could not publish this run's results to example; the files there are "
                       "not this run's.\n", encoding="utf-8")
        self.assertEqual(sd_ship_review.failed_tests({"checks": [self.row]}), [])
        log.write_text(OUTPUT + "Tests skipped locally; install required tools or make the skip explicit.\n",
                       encoding="utf-8")
        self.assertEqual(sd_ship_review.failed_tests({"checks": [self.row]}), [])
        _review, rerun, refused = self.prepared()
        self.assertIsNotNone(refused)
        self.assertEqual(rerun.call_count, 0)

    def test_a_rerun_that_skipped_is_no_pass(self):
        """`make test` refuses a skip; a rerun that skipped the test (a missing tool) proves nothing and bars
        nothing."""
        review, _rerun, refused = self.prepared(rerun=(0, "", "Ran 1 test in 0.001s\n\nOK (skipped=1)\n"))
        self.assertIsNotNone(refused)
        self.assertEqual((self.notes(FLAKE), self.notes(RERUN_FAILED)), ([], []))
        self.assertIsNone(review.state.get("gate_flake"))

    def test_a_rerun_that_never_ran_the_test_bars_nothing(self):
        """An import or environment fault in the rerun names no failed test: the gate failure stands, but no
        test goes on the seven-day list for it."""
        for name, rerun in (("import", (1, "", "ImportError: No module named 'example'\nFAILED (errors=1)")),
                            ("timeout", (None, "", "timed out"))):
            with self.subTest(name):
                _review, _rerun, refused = self.prepared(rerun=rerun)
                self.assertIsNotNone(refused)
                self.assertEqual(self.notes(RERUN_FAILED), [])


class TheRerunRunsAtTheReviewedHead(unittest.TestCase):
    """sd:9768 review: the rerun runs in a clean worktree of the gate's head, never the caller's checkout."""

    def test_the_rerun_runs_in_a_clean_worktree_of_head(self):
        import subprocess
        import tempfile

        import sd_gate_run

        from tests.clean_env import clean_environment

        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = pathlib.Path(scratch.name) / "repo"
        root.mkdir()

        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                                  text=True, env=clean_environment()).stdout.strip()

        git("init", "-q")
        # The gate's Makefile builds its environment first (`$(GATE_ENV)`), then names it relative to the tree.
        (root / "Makefile").write_text(f"GATE_ENV := made\nVENV_PYTHON = v/bin/python\nmade:\n"
                                       f"\t@mkdir -p v/bin && ln -sf {sys.executable} v/bin/python\n", encoding="utf-8")
        git("config", "user.email", "operator@example.test")
        git("config", "user.name", "operator")
        (root / "example.py").write_text("BROKEN = True\n", encoding="utf-8")
        git("add", "example.py", "Makefile")
        git("commit", "-q", "-m", "head")
        head = git("rev-parse", "HEAD")
        (root / "example.py").write_text("BROKEN = False\n", encoding="utf-8")  # an uncommitted fix
        seen = {}

        def child(command, env, cwd, timeout):
            seen.update(cwd=pathlib.Path(cwd), command=command,
                        head=subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], capture_output=True,
                                            text=True, env=clean_environment()).stdout.strip(),
                        text=(pathlib.Path(cwd) / "example.py").read_text(encoding="utf-8"))
            return 0, "OK", ""

        with unittest.mock.patch.object(sd_gate_run, "run_child", side_effect=child):
            self.assertEqual(sd_ship_review.rerun_tests(root, head, [FLAKY_TEST]), (0, "OK", ""))
        self.assertNotEqual(seen["cwd"].resolve(), root.resolve())
        self.assertEqual((seen["head"], seen["text"]), (head, "BROKEN = True\n"))
        self.assertEqual((seen["command"][0], seen["command"][-1]), (f"{seen['cwd']}/v/bin/python", FLAKY_TEST),
                         "the gate's interpreter, built and named by the Makefile at head")
        self.assertFalse(seen["cwd"].exists(), "the worktree is removed after the rerun")
        self.assertNotIn(str(seen["cwd"]), git("worktree", "list"))
        git("rm", "-q", "Makefile")
        git("commit", "-q", "-m", "no makefile")
        with unittest.mock.patch.object(sd_gate_run, "run_child", side_effect=child) as ran:
            code, _out, err = sd_ship_review.rerun_tests(root, git("rev-parse", "HEAD"), [FLAKY_TEST])
        self.assertEqual((code, ran.call_count), (None, 0), err)


if __name__ == "__main__":
    unittest.main()
