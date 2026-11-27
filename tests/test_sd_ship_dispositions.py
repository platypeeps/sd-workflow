"""Recorded rebuttals clear blockers at once and keep raw external reviews intact."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_sd_ship as fixture

ship = fixture.ship
# `fixture` puts bin/ on sys.path, so this import has to follow it.
sd_ship_dispositions = importlib.import_module("sd_ship_dispositions")


class DispositionTests(unittest.TestCase):
    setUp = fixture.ShipCase.setUp
    args = fixture.ShipCase.args
    operation = fixture.ShipCase.operation
    prepare = fixture.ShipCase.prepare
    merge = fixture.ShipCase.merge
    cli = fixture.ShipCase.cli

    def blocked(self):
        provider = self.programs / "review-fixture"
        payload = {
            "type": "result",
            "subtype": "success",
            "structured_output": {
                "findings": [
                    {
                        "path": "src.py",
                        "line": 1,
                        "severity": "high", "instances": ["src.py:1"],
                        "family": "correctness",
                        "summary": "fixture disputed finding",
                        "evidence": "python3 src.py: Traceback",
                    }
                ]
            },
        }
        provider.write_text(
            "#!/usr/bin/env python3\nprint(" + repr(json.dumps(payload)) + ")\n"
        )
        with self.assertRaises(ship.Refusal):
            self.prepare()
        self.head = fixture._git(self.root, "rev-parse", "HEAD")
        self.raw = json.loads(json.dumps(self.operation().state["passes"]))
        self.proposal_file = self.directory / "dispositions.json"

    def command(self, *extra):
        return self.cli("adjudicate", "--expected-head", self.head, *extra)

    def filled(self):
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        proposal = json.loads(result.stdout)["proposal"]
        for row in proposal["findings"]:
            row.update(
                response_disposition="rebutted",
                reason="src.py:1 reads the value the finding says is missing",
            )
        self.proposal_file.write_text(json.dumps(proposal))
        return proposal

    def accepted(self, proposal=None):
        """One command records the filled template; there is no digest step."""
        proposal = self.filled() if proposal is None else proposal
        self.proposal_file.write_text(json.dumps(proposal))
        result = self.command("--dispositions-file", str(self.proposal_file))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["phase"], "disposition_recorded")
        return proposal

    def rejected_proposal(self, proposal):
        self.proposal_file.write_text(json.dumps(proposal))
        before = list(self.connection.execute("SELECT * FROM state"))
        result = self.command("--dispositions-file", str(self.proposal_file))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(list(self.connection.execute("SELECT * FROM state")), before)

    def test_a_blocking_refusal_names_the_head_its_review_saw(self):
        """sd:9966 review: prepare's catch-up moves the head past the queued one, and adjudicate takes only the
        reviewed head, so the answer names it for Reject and enqueue."""
        self.blocked()
        with self.assertRaises(ship.Refusal) as caught:
            self.prepare()
        self.assertEqual(caught.exception.details["reviewed_head"], self.head)

    def test_a_blocking_refusal_names_the_digest_of_each_finding_adjudicate_binds(self):
        """sd:9966 review round 2: Reject binds the review the page showed; adjudicate's rows carry the same digests."""
        self.blocked()
        with self.assertRaises(ship.Refusal) as caught:
            self.prepare()
        template = json.loads(self.command().stdout)["proposal"]["findings"]
        self.assertEqual(caught.exception.details["finding_digests"], [row["finding_digest"] for row in template])
        self.assertTrue(template)

    def test_the_template_names_the_dispositions_this_review_already_holds(self):
        """sd:9966 class pass: a filled template replaces every recorded disposition, so the template says
        whether this review holds some; Reject and enqueue refuses rather than replace them."""
        self.blocked()
        self.assertIsNone(json.loads(self.command().stdout)["recorded_revision"])
        self.accepted()
        self.assertIsNotNone(json.loads(self.command().stdout)["recorded_revision"])

    def test_the_command_a_blocking_refusal_names_prints_each_finding_whole(self):
        """sd:2102. `next_action` is only useful if the command it names runs and shows the findings."""
        self.blocked()
        with self.assertRaises(ship.Refusal) as caught:
            self.prepare()
        named = re.search(r"`sd-ship (adjudicate [^`]+)`", caught.exception.workflow["next_action"]).group(1)
        result = subprocess.run([sys.executable, str(fixture.ROOT / "bin/sd-ship"), *shlex.split(named)],
                                cwd=self.root, env=self.environment, text=True, capture_output=True,
                                timeout=fixture.CLI_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = json.loads(result.stdout)["proposal"]["findings"]
        self.assertEqual([row["raw_finding"]["summary"] for row in rows], ["fixture disputed finding"])

    def test_the_findings_print_after_a_landing_that_keeps_the_request(self):
        """pack sd:7576. A verdict file moved but `--explain` asks the same, so the
        receipt is rebound. The template opens the database read-only: the rebind
        stays in memory there, and the findings print instead of "attempt to write
        a readonly database". The filled template then records as before."""
        self.blocked()
        operation = self.operation()
        operation.state["binding"] = "a review tool file landed on the default branch"
        operation.state["binding_manifest"]["verdict"]["sd_route.py"] = "ast:before"
        operation.save()
        before = list(self.connection.execute("SELECT * FROM state"))
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = json.loads(result.stdout)["proposal"]["findings"]
        self.assertEqual([row["raw_finding"]["summary"] for row in rows], ["fixture disputed finding"])
        self.assertEqual(list(self.connection.execute("SELECT * FROM state")), before, "a read wrote the receipt")
        self.accepted()
        state = self.operation().state
        self.assertEqual(state["binding"], ship.binding(self.root))
        self.assertEqual(state["review_binding_kept"][0]["changed"], [["sd_route.py", "verdict"]])

    def test_a_blocking_refusal_bounds_its_message_and_names_a_no_item_record(self):
        """sd:2102. Five findings are named, a line-less one by path, and `findings` carries every one."""
        rows = [{"disposition": "blocking", "severity": "high", "instances": ["src.py:1"], "path": f"f{n}.py", "line": n, "summary": "x " * 200}
                for n in range(7)] + [{"disposition": "advisory", "severity": "low", "path": "a.py", "line": 1,
                                       "summary": "advice"}]
        rows[0]["line"] = None
        review = SimpleNamespace(args=SimpleNamespace(no_item=True, review_id="r-1", item=None))
        refusal = sd_ship_dispositions.blocking_refusal(review, "abc123", {"findings": rows}, "local review blocking")
        message = str(refusal)
        self.assertIn("high f0.py x x", message)
        self.assertIn("f4.py:4", message)
        self.assertNotIn("f5.py", message)
        self.assertIn("; and 2 more", message)
        self.assertIn("sd-ship adjudicate --no-item --review-id r-1 --expected-head abc123 --json",
                      refusal.workflow["next_action"])
        findings = ship.failure("prepare", refusal)["findings"]
        self.assertEqual([row["path"] for row in findings], [f"f{n}.py" for n in range(7)])
        self.assertTrue(all(len(row["summary"]) == sd_ship_dispositions.SUMMARY_CHARS for row in findings))

    def test_a_blocking_refusal_keeps_the_full_finding_text_in_its_record(self):
        """pack sd:7133. The refusal's `summary` is cut; `text` keeps the whole finding for the prepare log."""
        long = "Dating the answer from arrival assumes fresh observations. " * 8
        row = {"disposition": "blocking", "severity": "high", "instances": ["src.py:1"], "path": "a.py", "line": 3, "summary": long}
        review = SimpleNamespace(args=SimpleNamespace(no_item=False, review_id=None, item=7))
        refusal = sd_ship_dispositions.blocking_refusal(review, "abc123", {"findings": [row]}, "local review blocking")
        [finding] = ship.failure("prepare", refusal)["findings"]
        self.assertEqual(finding["text"], long.strip())
        self.assertTrue(finding["summary"].endswith("..."))
        self.assertLess(len(finding["summary"]), len(long))

    def test_blank_template_and_retired_flags_refuse_without_writing(self):
        self.blocked()
        blank = json.loads(self.command().stdout)["proposal"]
        self.rejected_proposal(blank)
        self.filled()
        before = list(self.connection.execute("SELECT * FROM state"))
        for args in (
            ("--accept-dispositions", "0" * 64),
            ("--dispositions-file", str(self.proposal_file), "--prepare-evidence"),
            ("--additional-review-for", self.head),
        ):
            with self.subTest(args=args):
                result = self.command(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    list(self.connection.execute("SELECT * FROM state")), before
                )
        with (
            self.assertRaises(ship.Refusal),
            patch.object(
                ship,
                "review_process",
                side_effect=AssertionError("unaccepted dispatch"),
            ),
        ):
            self.prepare()

    def test_only_a_rebuttal_or_parked_risk_with_a_reason_is_recorded(self):
        self.blocked()
        proposal = self.filled()
        for value in ("addressed", "unresolved", "", "accepted"):
            trial = json.loads(json.dumps(proposal))
            trial["findings"][0]["response_disposition"] = value
            self.rejected_proposal(trial)
        for row in proposal["findings"]:
            row.update(response_disposition="parked", reason="revisit when the parser changes")
        trial = json.loads(json.dumps(proposal))
        trial["findings"][0]["reason"] = " "
        self.rejected_proposal(trial)
        # The retired fields are refused, not ignored, so an old proposal says so.
        for field in ("owner", "trigger", "evidence"):
            trial = json.loads(json.dumps(proposal))
            trial["findings"][0][field] = "x"
            self.rejected_proposal(trial)
        trial = json.loads(json.dumps(proposal))
        trial.update(operator="fixture", authority_context="fixture")
        self.rejected_proposal(trial)
        self.accepted(proposal)
        with patch.object(
            ship,
            "review_process",
            side_effect=AssertionError("risk acceptance dispatched"),
        ):
            self.prepare()

    def with_test(self, argv):
        proposal = self.filled()
        for row in proposal["findings"]:
            row["test"] = argv
        return proposal

    def test_a_rebuttal_whose_test_passes_clears_and_one_whose_test_fails_keeps_the_blocker(self):
        """sd:10893. A high blocks only while a failing test reproduces it: the builder's attempted test
        passes, so the rebuttal records and prepare clears; a failing one refuses and nothing is written."""
        self.blocked()
        for argv in ([sys.executable, "-c", "raise SystemExit(1)"], ["tests/example_missing_test"]):
            with self.subTest(argv=argv):
                self.rejected_proposal(self.with_test(argv))
        self.accepted(self.with_test([sys.executable, "-c", "pass"]))
        with patch.object(ship, "review_process", side_effect=AssertionError("dispatched")):
            self.prepare()

    def test_prepare_reruns_the_test_and_a_failing_one_reopens_the_blocker(self):
        """sd:10893. The lane counts a blocker open while its test fails, read at prepare, not only at record."""
        self.blocked()
        flag = self.directory / "reproduced"
        script = f"import pathlib, sys; sys.exit(pathlib.Path({str(flag)!r}).exists())"
        self.accepted(self.with_test([sys.executable, "-c", script]))
        flag.write_text("x")
        with self.assertRaisesRegex(ship.Refusal, "reproduces the finding"):
            self.prepare()

    def test_a_test_that_fails_on_a_rerun_revokes_the_recorded_clearance(self):
        """sd:10893 class pass: a rerun that runs the test and fails, at prepare or at a new recording, drops
        the clearance a passing prepare saved, so a later read (merge, `verify-review`) cannot reuse it."""
        self.blocked()
        flag = self.directory / "reproduced"
        script = f"import pathlib, sys; sys.exit(pathlib.Path({str(flag)!r}).exists())"
        for rerun in ("prepare", "recording"):
            with self.subTest(rerun=rerun):
                flag.unlink(missing_ok=True)
                self.accepted(self.with_test([sys.executable, "-c", script]))
                self.prepare()
                self.operation().check_review(self.head)
                flag.write_text("x")
                if rerun == "prepare":
                    with self.assertRaisesRegex(ship.Refusal, "reproduces the finding"):
                        self.prepare()
                else:
                    self.proposal_file.write_text(json.dumps(self.with_test([sys.executable, "-c", script])))
                    result = self.command("--dispositions-file", str(self.proposal_file))
                    self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIsNone(self.operation().state.get("review_clearance"))
                with self.assertRaisesRegex(ship.Refusal, "prepare again"):
                    self.operation().check_review(self.head)

    def test_a_test_reaches_no_code_in_the_live_checkout(self):
        """sd:10893 class pass: the test runs the head's code alone. An environment entry or an argv path
        naming the live checkout would load its uncommitted files and clear the finding against them."""
        root = self.root.resolve()
        with (root / ".git/info/exclude").open("a") as stream:
            stream.write("example_probe.py\n")
        (root / "example_probe.py").write_text("")
        head = fixture._git(root, "rev-parse", "HEAD")
        environments = {"PYTHONPATH": str(root), "EXAMPLE_SOURCE": str(root)}
        script = "import os, sys; sys.path[:0] = [os.environ['EXAMPLE_SOURCE']]; import example_probe"
        for argv in ([sys.executable, "-c", "import example_probe"], [sys.executable, "-c", script],
                     [sys.executable, str(root / "example_probe.py")]):
            with self.subTest(argv=argv), patch.dict("os.environ", environments):
                with self.assertRaises(ship.Refusal):
                    sd_ship_dispositions.reproduces(root, head, 1, argv)

    def test_any_end_of_a_test_run_but_a_pass_leaves_no_clearance(self):
        """sd:10893 class pass: the clearance is voided before the first test runs, so no way a run ends
        without a pass keeps it: output that does not decode, an error that is no refusal, an interruption."""
        self.blocked()
        flag = self.directory / "reproduced"
        script = (f"import pathlib, sys\nif pathlib.Path({str(flag)!r}).exists():\n"
                  "    sys.stdout.buffer.write(b'\\xff\\xfe'); sys.exit(1)")
        endings = {"undecodable": None, "error": RuntimeError("example"), "interrupted": KeyboardInterrupt()}
        for name, raised in endings.items():
            with self.subTest(ending=name):
                flag.unlink(missing_ok=True)
                self.accepted(self.with_test([sys.executable, "-c", script]))
                self.prepare()
                self.assertIsNotNone(self.operation().state.get("review_clearance"))
                flag.write_text("x")
                if raised is None:
                    with self.assertRaisesRegex(ship.Refusal, "reproduces the finding"):
                        self.prepare()
                else:
                    with patch.object(sd_ship_dispositions, "reproduces", side_effect=raised), \
                            self.assertRaises(type(raised)):
                        self.prepare()
                self.assertIsNone(self.operation().state.get("review_clearance"))

    def test_a_path_into_the_live_checkout_is_named_in_any_spelling(self):
        """sd:10893 class pass: the matcher refuses every spelling of the checkout, not one boundary shape:
        quoted in an inline script, under `~` or `$HOME`, in another letter case, as given or resolved."""
        home = pathlib.Path.home()
        root = home / "example-area" / "Clone"
        for value in (f"sys.path.insert(0, '{root}')", f'"{root}"', "~/example-area/Clone/x.py",
                      "$HOME/example-area/Clone", "${HOME}/example-area/Clone", str(root).upper(),
                      f"--source={root}", f"a{chr(10)}{root}{chr(10)}b"):
            with self.subTest(value=value):
                self.assertTrue(sd_ship_dispositions.names_root(root, value))
        for value in ("tests/test_example.py", str(home / "example-area"), "-c", "import example"):
            with self.subTest(value=value):
                self.assertFalse(sd_ship_dispositions.names_root(root, value))
        live = self.root.resolve()
        head = fixture._git(live, "rev-parse", "HEAD")
        inline = f"import sys; sys.path.insert(0, {str(live)!r})"
        with self.assertRaisesRegex(ship.Refusal, "names a path in the live checkout"):
            sd_ship_dispositions.reproduces(live, head, 1, [sys.executable, "-c", inline])
        sibling = f"{live}-tools/bin"
        with patch.dict("os.environ", {"PATH": os.pathsep.join([sibling, os.environ["PATH"]]),
                                       "EXAMPLE_QUOTED": f"'{live}'"}):
            environment = sd_ship_dispositions.rebuttal_environment(live)
        self.assertNotIn("EXAMPLE_QUOTED", environment)
        self.assertNotIn(sibling, environment["PATH"].split(os.pathsep))
        self.assertTrue(environment["PATH"])

    def test_only_recording_and_prepare_run_the_test_never_a_read(self):
        """sd:10893 review: merge, publication and `verify-review` read the record and run nothing; a test
        is code, so a read-only command executing it in the live checkout is not read-only."""
        self.blocked()
        runs = self.directory / "runs"
        script = f"open({str(runs)!r}, 'a').write('x')"
        self.accepted(self.with_test([sys.executable, "-c", script]))
        self.assertEqual(runs.read_text(), "x")
        self.prepare()
        self.assertEqual(runs.read_text(), "xx")
        self.operation().check_review(self.head)
        self.operation().check_review(self.head, refresh_adjudication=True)
        self.assertEqual(runs.read_text(), "xx")

    def test_a_rebuttal_test_runs_in_a_disposable_worktree_at_the_head(self):
        """sd:10893 class fix: the test runs in a throwaway worktree of the exact head, as the gate's rerun
        does, so no test writes the live checkout, tracked or ignored, and git cannot restore an ignored file."""
        self.blocked()
        with (self.root / ".git/info/exclude").open("a") as stream:
            stream.write("example-ignored.txt\n")
        ignored = self.root / "example-ignored.txt"
        ignored.write_text("operator data")
        tracked = fixture._git(self.root, "ls-files").split()[0]
        before = (self.root / tracked).read_bytes()
        script = ("import os, subprocess; open('example-ignored.txt', 'w').write('clobbered'); "
                  f"open({tracked!r}, 'w').write('clobbered'); subprocess.run(['git', 'checkout', '--', '.']); "
                  f"assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == {self.head!r}")
        self.accepted(self.with_test([sys.executable, "-c", script]))
        self.assertEqual(ignored.read_text(), "operator data")
        self.assertEqual((self.root / tracked).read_bytes(), before)
        self.assertEqual(fixture._git(self.root, "status", "--porcelain"), "")
        listed = fixture._git(self.root, "worktree", "list", "--porcelain")
        self.assertEqual(listed.count("worktree "), 1, listed)

    def test_a_test_that_dirties_the_checkout_or_is_malformed_refuses(self):
        """sd:10893. The record binds the clean exact head; `test` is an argv list, not a shell string."""
        self.blocked()
        dirty = [sys.executable, "-c", "open('example-left-behind.txt', 'w').close()"]
        self.rejected_proposal(self.with_test(dirty))
        self.assertFalse((self.root / "example-left-behind.txt").exists())
        for value in ("python3 -c pass", [""], [1], {"argv": []}, ["a\0b"]):
            with self.subTest(value=value):
                self.rejected_proposal(self.with_test(value))

    def test_the_template_offers_an_empty_test(self):
        """sd:10893. The template names the field; [] and an absent `test` both record as before."""
        self.blocked()
        template = json.loads(self.command().stdout)["proposal"]
        self.assertEqual([row["test"] for row in template["findings"]], [[]])
        self.accepted(self.with_test([]))

    def test_the_template_and_the_refusal_name_the_allowed_values(self):
        """sd:9881: a blank template and `rebut` left the values unnamed."""
        self.blocked()
        template = json.loads(self.command().stdout)["proposal"]
        row = template["findings"][0]
        self.assertEqual(row["response_disposition"], "rebutted|parked")
        row["reason"] = "the operator ruled on this finding"
        for value in ("rebut", "rebutted|parked"):
            with self.subTest(value=value):
                row["response_disposition"] = value
                self.proposal_file.write_text(json.dumps(template))
                result = self.command("--dispositions-file", str(self.proposal_file))
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("'rebutted' or 'parked'", result.stdout + result.stderr)
                self.assertIn(repr(value), result.stdout + result.stderr)

    def test_duplicate_occurrences_need_distinct_indices_and_unchanged_raw_findings(
        self,
    ):
        self.blocked()
        op = self.operation()
        first = op.state["passes"][-1]["report"]["findings"][0]
        op.state["passes"][-1]["report"]["findings"] = [first, dict(first)]
        op.save()
        proposal = self.filled()
        self.assertEqual([row["index"] for row in proposal["findings"]], [1, 2])
        self.assertEqual(
            proposal["findings"][0]["finding_digest"],
            proposal["findings"][1]["finding_digest"],
        )
        for kind in ("missing", "duplicate", "boolean", "raw", "digest"):
            trial = json.loads(json.dumps(proposal))
            if kind == "missing":
                trial["findings"].pop()
            elif kind == "duplicate":
                trial["findings"][1] = trial["findings"][0]
            elif kind == "boolean":
                trial["findings"][0]["index"] = True
            elif kind == "raw":
                trial["findings"][0]["raw_finding"]["summary"] = "rewritten"
            else:
                trial["findings"][0]["finding_digest"] = "0" * 64
            with self.subTest(kind=kind):
                self.rejected_proposal(trial)

    def test_incomplete_transport_counts_status_and_check_cannot_be_waived(self):
        self.blocked()
        original = self.operation().state
        for kind in (
            "depth",
            "false_count",
            "duplicate_backend",
            "reviewed_by",
            "check",
            "check_exit",
            "transport",
            "timeout",
            "status",
            "scope",
        ):
            state = json.loads(json.dumps(original))
            last = state["passes"][-1]
            report = last["report"]
            if kind == "depth":
                report["completed_reviews"] = 0
            elif kind == "false_count":
                report["outcomes"].pop()
            elif kind == "duplicate_backend":
                report["outcomes"].append(dict(report["outcomes"][0]))
                report["reviewed_by"].append(report["reviewed_by"][0])
                report["completed_reviews"] = len(report["outcomes"])
            elif kind == "reviewed_by":
                report["reviewed_by"] = ["invented"]
            elif kind == "check":
                report["check"]["status"] = "fail"
            elif kind == "check_exit":
                report["check"]["exit_code"] = 1
            elif kind == "transport":
                last["exit_code"] = 3
            elif kind == "timeout":
                last["execution_error"] = {"kind": "watchdog_expired"}
            elif kind == "status":
                report["status"] = "unavailable"
            else:
                report["scope"] = "worktree"
            self.operation().save(**state)
            with self.subTest(kind=kind):
                self.assertEqual(self.command().returncode, 3)
        self.operation().save(**original)

    def test_failed_primary_with_real_completed_fallback_still_qualifies(self):
        self.blocked()
        op = self.operation()
        op.state["passes"][-1]["report"]["outcomes"].insert(
            0, {"backend": "primary", "status": "rate_limited"}
        )
        op.save()
        self.accepted()
        with patch.object(
            ship, "review_process", side_effect=AssertionError("fallback dispatched")
        ):
            self.prepare()

    def test_changed_head_and_dirty_tracked_source_refuse(self):
        self.blocked()
        self.accepted()
        (self.root / "src.py").write_text("changed after acceptance")
        with self.assertRaises(ship.Refusal):
            self.operation().check_review(self.head)
        fixture._git(self.root, "restore", "src.py")
        fixture._git(
            self.root,
            "commit",
            "--allow-empty",
            "-m",
            "changed head\n\nAuthored-with: human",
        )
        self.assertEqual(self.command().returncode, 3)

    def test_later_reportless_pass_and_changed_report_invalidate_acceptance(self):
        self.blocked()
        self.accepted()
        self.prepare()
        original = self.operation().state
        for kind in ("report", "history", "reportless"):
            changed = json.loads(json.dumps(original))
            if kind == "report":
                changed["passes"][-1]["report"]["findings"][0]["summary"] = (
                    "new finding"
                )
            elif kind == "history":
                changed["passes"][0]["started_at"] = "changed history"
            else:
                changed["passes"].append(
                    {"head": self.head, "started_at": "later", "base": None}
                )
            self.operation().save(**changed)
            with self.subTest(kind=kind), self.assertRaises(ship.Refusal):
                self.operation().check_review(self.head)
        self.operation().save(**original)

    def test_acceptance_is_separate_append_only_and_does_not_confuse_for_item(self):
        self.blocked()
        original = list(
            self.connection.execute(
                "SELECT id,body FROM state WHERE kind='checkpoint' ORDER BY id"
            )
        )
        self.accepted()
        current = list(
            self.connection.execute(
                "SELECT id,body FROM state WHERE kind='checkpoint' ORDER BY id"
            )
        )
        self.assertEqual(current[: len(original)], original)
        self.assertEqual(len(current), len(original) + 1)
        resolved = fixture.receipts.for_item(
            self.connection, self.remote.slug, self.item, "topic"
        )
        self.assertEqual(resolved[2]["passes"], self.raw)
        self.assertEqual(self.operation().state["passes"], self.raw)

    def test_a_forged_no_item_receipt_under_the_item_key_cannot_clear(self):
        """A no-item acceptance copied to the item key cannot authorize publication.

        The reverse of the no-item contract of the same name. Both readers own
        this, because either one accepting the other's receipt would let one
        mode's approval clear work it never reviewed.
        """

        self.blocked()
        self.accepted()
        key = ship.sd_ship_dispositions.key(self.operation())
        revision, receipt = fixture.receipts.read(self.connection, key)
        forged = json.loads(json.dumps(receipt))
        bindings = forged["proposal"]["bindings"]
        bindings.pop("item")
        bindings.update(identity_mode="no-item", review_id="0" * 32,
                        identity_revision=1, schema_version=1)
        forged["proposal_digest"] = ship.sd_ship_dispositions.digest(forged["proposal"])
        fixture.receipts.save(self.connection, key, revision, forged)
        with self.assertRaisesRegex(ship.Refusal, "bind|identity|item"):
            self.prepare()

    def test_reacceptance_requires_prepare_refresh_before_merge(self):
        self.blocked()
        proposal = self.accepted()
        self.prepare()
        previous = self.operation().state["review_clearance"]
        proposal["findings"][0]["reason"] = "a new fixture rebuttal"
        self.accepted(proposal)
        with self.assertRaisesRegex(ship.Refusal, "prepare again"):
            self.merge()
        with patch.object(
            ship, "review_process", side_effect=AssertionError("refresh dispatched")
        ):
            self.prepare()
        self.assertNotEqual(self.operation().state["review_clearance"], previous)
        self.assertEqual(self.merge()["phase"], "merged")

    def test_preview_uses_readonly_connection_without_lock_or_write_transaction(self):
        from sd_db import database

        self.blocked()
        args = [
            "adjudicate",
            "--item",
            str(self.item),
            "--expected-head",
            self.head,
            "--database",
            str(self.database),
            "--json",
        ]
        with (
            patch.object(database, "connect", wraps=database.connect) as connect,
            patch.object(ship.sd_lib, "repo_root", return_value=self.root),
            patch.object(
                fixture.receipts,
                "repository_lock",
                side_effect=AssertionError("readonly lock"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(ship.main(args), 0)
        self.assertIs(connect.call_args.kwargs["write"], False)

    def test_recording_checks_source_cas_inside_the_write_transaction(self):
        self.blocked()
        self.filled()
        op = self.operation(
            "adjudicate",
            "--expected-head",
            self.head,
            "--dispositions-file",
            str(self.proposal_file),
        )
        original_read = fixture.receipts.read
        observations = []

        def watched(connection, key):
            if key == op.key:
                observations.append(connection.in_transaction)
            return original_read(connection, key)

        with patch.object(fixture.receipts, "read", side_effect=watched):
            op.adjudicate()
        self.assertEqual(observations, [True])
        stale = self.operation(
            "adjudicate",
            "--expected-head",
            self.head,
            "--dispositions-file",
            str(self.proposal_file),
        )
        self.operation().save(warnings=["concurrent phase update"])
        before = list(self.connection.execute("SELECT * FROM state"))
        with self.assertRaisesRegex(ship.Refusal, "changed before"):
            stale.adjudicate()
        self.assertEqual(list(self.connection.execute("SELECT * FROM state")), before)

    def test_review_tool_adjudicator_tool_and_policy_changes_refuse(self):
        self.blocked()
        self.accepted()
        self.prepare()
        with patch.object(ship, "binding", return_value="different review tools"):
            with self.assertRaises(ship.Refusal):
                self.operation().check_review(self.head)
        original_read = pathlib.Path.read_bytes
        # sd:1834: tool files bind by class. Verdict code and the written
        # acceptance rules refuse; gate code runs live and does not.
        for name in (
            "sd-review",
            "sd_lib.py",
            "sd_registry.py",
            "SKILL.md",
            "sd-planning-adversarial-review.md",
        ):

            def changed(path, name=name):
                value = original_read(path)
                return value + b"changed" if path.name == name else value

            with (
                self.subTest(name=name),
                patch.object(pathlib.Path, "read_bytes", changed),
                self.assertRaises(ship.Refusal),
            ):
                self.operation().check_review(self.head)
        for name in ("sd-ship", "sd_ship_dispositions.py", "sd_ship_remote.py", "sd-check"):

            def changed(path, name=name):
                value = original_read(path)
                return value + b"changed" if path.name == name else value

            with self.subTest(gate=name), patch.object(pathlib.Path, "read_bytes", changed):
                self.operation().check_review(self.head)

    def test_typed_json_identity_changes_refuse(self):
        self.blocked()
        proposal = self.filled()
        for field, value in (
            ("line", True),
            ("line", 1.0),
            ("item", float(self.item)),
            ("item", True),
        ):
            trial = json.loads(json.dumps(proposal))
            if field == "line":
                trial["findings"][0]["raw_finding"][field] = value
            else:
                trial["bindings"][field] = value
            with self.subTest(field=field, value=value):
                self.rejected_proposal(trial)

    def test_every_spent_pass_can_reuse_exact_complete_report_without_another_review(
        self,
    ):
        self.blocked()
        for index in range(fixture.CAP - 1):
            fixture._git(
                self.root,
                "commit",
                "--allow-empty",
                "-m",
                f"fixture fix attempt {index + 1}\n\nAuthored-with: human",
            )
            self.head = fixture._git(self.root, "rev-parse", "HEAD")
            with self.assertRaises(ship.Refusal):
                self.prepare()
        for renewal in (False, True):
            arguments = [
                "--additional-review-for",
                self.head,
                "--request-reason",
                "fixture explicit review",
            ]
            if renewal:
                arguments += [
                    "--review-history-digest",
                    ship.digest(self.operation().state["passes"]),
                ]
            with self.assertRaises(ship.Refusal):
                self.prepare(*arguments)
        previous = json.loads(json.dumps(self.operation().state["passes"]))
        self.assertEqual(len(previous), fixture.CAP + 2)
        self.assertIsNone(self.operation().state["reviewed_head"])
        self.accepted()
        with patch.object(
            ship,
            "review_process",
            side_effect=AssertionError("a further review dispatched"),
        ):
            self.prepare()
        self.assertEqual(self.operation().state["passes"], previous)

    def test_unreadable_and_oversized_file_reads_refuse(self):
        path = self.directory / "read-dispositions.json"
        path.write_text("{}")
        read = ship.sd_ship_dispositions.read_file
        with self.assertRaisesRegex(ship.Refusal, "bounded input"):
            read(path, 1)
        with self.assertRaisesRegex(ship.Refusal, "cannot be read"):
            read(self.directory / "missing.json", 1024)

    def test_duplicate_json_keys_and_oversized_proposals_refuse(self):
        self.blocked()
        proposal = self.filled()
        duplicate = json.dumps(proposal).replace(
            '"schema_version": 1', '"schema_version": 2, "schema_version": 1'
        )
        for data in (
            duplicate,
            "x" * (ship.sd_ship_dispositions.MAX_PROPOSAL_BYTES + 1),
        ):
            self.proposal_file.write_text(data)
            before = list(self.connection.execute("SELECT * FROM state"))
            result = self.command("--dispositions-file", str(self.proposal_file))
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertEqual(
                list(self.connection.execute("SELECT * FROM state")), before
            )

    def test_the_template_writes_no_receipt(self):
        self.blocked()
        before = list(self.connection.execute("SELECT * FROM state"))
        self.filled()
        self.assertEqual(list(self.connection.execute("SELECT * FROM state")), before)

    def test_a_recorded_rebuttal_reuses_blocking_review_without_provider_dispatch(self):
        self.blocked()
        self.filled()
        result = self.command("--dispositions-file", str(self.proposal_file))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(json.loads(result.stdout)["proposal_digest"], r"^[0-9a-f]{64}$")
        with patch.object(
            ship,
            "review_process",
            side_effect=AssertionError("provider dispatch after adjudication"),
        ):
            self.assertEqual(self.prepare()["phase"], "ready_to_send")
        self.assertEqual(self.operation().state["passes"], self.raw)
        self.assertEqual(
            self.operation().state["review_clearance"]["kind"], "adjudicated"
        )
        self.assertEqual(self.merge()["phase"], "merged")


sd_ship_review = importlib.import_module("sd_ship_review")


class TheAdjudicatedGate(unittest.TestCase):
    """sd:2605. A blocking review runs no gate, so the prepare that reads its
    accepted dispositions runs the gate itself before clearance."""

    setUp = fixture.ShipCase.setUp
    args = fixture.ShipCase.args
    operation = fixture.ShipCase.operation
    prepare = fixture.ShipCase.prepare
    merge = fixture.ShipCase.merge
    cli = fixture.ShipCase.cli
    blocked = DispositionTests.blocked
    command = DispositionTests.command
    filled = DispositionTests.filled
    accepted = DispositionTests.accepted

    def gate_runs(self):
        """A spy on the one gate run this path makes, passing each call through."""
        spy = patch.object(sd_ship_review, "adjudicated_check", wraps=sd_ship_review.adjudicated_check)
        self.addCleanup(spy.stop)
        return spy.start()

    def test_the_blocking_report_carries_a_gate_that_did_not_run(self):
        self.blocked()
        self.assertEqual(self.raw[-1]["report"]["check"]["status"], "not_run")

    def test_prepare_runs_the_gate_once_before_clearance_and_merge_reads_it(self):
        self.blocked()
        self.accepted()
        runs = self.gate_runs()
        with patch.object(ship, "review_process", side_effect=AssertionError("provider dispatch after adjudication")):
            self.assertEqual(self.prepare()["phase"], "ready_to_send")
        state = self.operation().state
        self.assertEqual((state["adjudicated_gate"]["head"], state["adjudicated_gate"]["status"]), (self.head, "pass"))
        self.assertEqual(state["review_clearance"]["kind"], "adjudicated")
        self.assertEqual(state["passes"], self.raw)
        self.assertEqual(self.merge()["phase"], "merged")
        self.assertEqual([call.args[1] for call in runs.call_args_list], [self.head])

    def test_publication_and_merge_refuse_a_head_whose_gate_never_passed(self):
        self.blocked()
        self.accepted()
        self.prepare()
        for recorded in (None, {"head": "0" * 40, "status": "pass"}, {"head": self.head, "status": "fail"}):
            with self.subTest(recorded=recorded):
                operation = self.operation("merge", "--manual", "--expected-head", self.head)
                operation.state["adjudicated_gate"] = recorded
                runs = self.gate_runs()
                with self.assertRaises(ship.Refusal) as caught:
                    operation.check_review(self.head)
                self.assertEqual(caught.exception.workflow["blocker"]["code"], "gate_not_run")
                self.assertEqual(runs.call_count, 0)

    def test_a_gate_that_fails_after_adjudication_refuses_and_keeps_the_pass(self):
        (self.root / "Makefile").write_text("check:\n\t@echo adjudicated-gate-broke; exit 2\n")
        fixture._git(self.root, "commit", "-qam", "break the gate\n\nAuthored-with: human")
        self.blocked()
        self.accepted()
        with self.assertRaises(ship.Refusal) as caught:
            self.prepare()
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "gate_failed")
        self.assertIn("after its blocking findings were adjudicated; the review pass stays spent", str(caught.exception))
        self.assertIn("adjudicated-gate-broke", str(caught.exception))
        state = self.operation().state
        self.assertEqual(state["passes"], self.raw)
        self.assertEqual(state["adjudicated_gate"]["status"], "fail")
        self.assertIsNone(state.get("review_clearance"))


class GateWaivable(unittest.TestCase):
    """sd:2605. Adjudication waives a gate that passed or never ran, nothing else."""

    def test_a_pass_or_a_gate_that_never_ran(self):
        for check in ({"status": "pass", "exit_code": 0}, {"status": "not_run", "exit_code": None}):
            with self.subTest(check=check):
                self.assertTrue(sd_ship_dispositions.gate_waivable(check))

    def test_never_a_failed_partial_or_malformed_gate(self):
        for check in ({"status": "fail", "exit_code": 1}, {"status": "not_run", "exit_code": 2},
                      {"status": "pass", "exit_code": None}, {"status": "pass", "exit_code": True},
                      {"status": "not_run", "exit_code": "0"}, {}, None, "pass"):
            with self.subTest(check=check):
                self.assertFalse(sd_ship_dispositions.gate_waivable(check))


if __name__ == "__main__":
    unittest.main()
