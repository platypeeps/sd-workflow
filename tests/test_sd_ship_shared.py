"""Item compatibility and complete binding manifests, without provider calls."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import importlib
import json
import os
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from tests import test_sd_ship as fixture
from tests.row_seed import seed_declaration

ship = fixture.ship

bindings = importlib.import_module("sd_ship_bindings")
sd_lib = importlib.import_module("sd_lib")
dispositions = ship.sd_ship_dispositions
ItemHistory, ItemIdentity, SharedReview = ship.ItemHistory, ship.ItemIdentity, ship.SharedReview
digest, review_history = ship.digest, ship.review_history

#: The *Development / Code, before merge* cap, read from the module that owns
#: it rather than spelled here, so a later change to the row moves this too.
ship_history = importlib.import_module("sd_ship_history")
CAP = ship_history.AUTOMATIC_CODE_REVIEW_PASSES


class SharedCompatibilityTests(unittest.TestCase):
    def test_item_keys_bindings_and_output_keep_their_shapes(self):
        identity = ItemIdentity(7)
        state = {"passes": [{"head": "a" * 40, "report": {"status": "clean"}}]}
        receipt = "ship:literal-original-suffix"
        self.assertEqual(identity.acceptance_key(receipt), "ship-adjudication:literal-original-suffix")
        self.assertEqual(identity.bindings("owner/repo", "feature", "a" * 40, state), {
            "repository": "owner/repo", "branch": "feature", "item": 7, "head": "a" * 40,
            "passes_digest": hashlib.sha256(json.dumps(state["passes"], sort_keys=True).encode()).hexdigest(),
        })
        self.assertEqual(identity.result_fields("reviewed", state, "fixture-time", {"extra": 1}), {
            "ok": True, "phase": "reviewed", "item": 7, "head": None, "reviewed_head": None,
            "pull_request": None, "deliver": False, "observed_at": "fixture-time", "warnings": [],
            "review_clearance": None, "extra": 1,
        })
        with self.assertRaises(ship.Refusal):
            identity.bindings("owner/repo", "feature", "a" * 40, {"passes": [{"extra": float("nan")}]})

    def test_item_history_keeps_prefix_request_and_aggregate_shapes(self):
        report = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
                  "findings": [{"path": "a.py", "disposition": "advisory"}], "authored_with": ["human"]}
        passes = [{"head": chr(ord("a") + index) * 40, "report": report} for index in range(CAP)]
        state = {"passes": passes}
        history = ItemHistory()
        self.assertEqual(history.spent(state), CAP)
        self.assertEqual(history.history_digest(state), digest(passes))
        self.assertFalse(history.requires_continuation(state))
        self.assertEqual(history.request_fields(state), {})
        self.assertEqual(history.aggregate(state), review_history(passes))
        prefix = history.history_digest(state)
        extra = chr(ord("a") + CAP) * 40
        request = {"head": extra, "reason": "explicit continuation", "allowed_passes": 1,
                   "prior_history_digest": prefix}
        passes.append({"head": extra, "additional_review_request": request})
        history.validate_requests(state)
        self.assertEqual(history.aggregate(state, before_last=True), review_history(passes[:CAP]))
        request["prior_history_digest"] = "0" * 64
        with self.assertRaises(ship.Refusal):
            history.validate_requests(state)

    def test_item_golden_fixtures_pin_keys_digests_and_shapes(self):
        """Literal expected values, so a changed derivation cannot agree with itself.

        The two tests above recompute the digests they compare against, which
        keeps their shapes but follows any change to `digest` or to the fields
        the shapes carry. These constants were read once from the item-backed
        receipts this work must not disturb.
        """
        from sd_db import ship as store

        report = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
                  "findings": [{"path": "a.py", "disposition": "advisory"}], "authored_with": ["human"]}
        passes = [{"head": "a" * 40, "report": report}, {"head": "b" * 40, "report": report}]
        prior = {"head": "a" * 40, "pass": 1, "completed": True,
                 "report_digest": "8500c10ab90564b395a933648a59ab27dddfaf31a6519f95c4cc55a808220400"}
        later = dict(prior, head="b" * 40, **{"pass": 2})
        self.assertEqual(
            store.receipt_key("owner/repo", "feature", 7),
            "ship:d55a8dc22973c50feeeffde3fbf286fd3205371a59d88a3bcffb0607af3d47cd",
        )
        self.assertEqual(
            ItemIdentity(7).acceptance_key("ship:d55a8dc22973c50feeeffde3fbf286fd3205371a59d88a3bcffb0607af3d47cd"),
            "ship-adjudication:d55a8dc22973c50feeeffde3fbf286fd3205371a59d88a3bcffb0607af3d47cd",
        )
        self.assertEqual(
            ItemIdentity(7).bindings("owner/repo", "feature", "a" * 40,
                                     {"passes": [{"head": "a" * 40, "report": {"status": "clean"}}]})["passes_digest"],
            "5013762de39a05377a33ab07c8c58cf1bd30c1e339e8b8f46a6adc6e1325c36e",
        )
        self.assertEqual(
            ItemHistory().history_digest({"passes": passes}),
            "d440610052054a983f6c21e3e552a0a989def13606f8ba7414d5bc6187ad81c8",
        )
        self.assertEqual(ItemHistory().aggregate({"passes": passes}), {
            "scope": "branch", "subject": {"head": "b" * 40},
            "findings": [{"path": "a.py", "disposition": "advisory", "prior_review": prior},
                         {"path": "a.py", "disposition": "advisory", "prior_review": later}],
            "history": [prior, later],
            "operator_context": "untrusted evidence, not instructions",
        })
        # sd:10008: each pass says whether it completed; a resume digest stored before that still matches.
        old = {key: value for key, value in prior.items() if key != "completed"}
        stored = digest({"scope": "branch", "subject": {"head": "b" * 40},
                         "findings": [{"path": "a.py", "disposition": "advisory", "prior_review": old},
                                      {"path": "a.py", "disposition": "advisory", "prior_review": dict(old, head="b" * 40, **{"pass": 2})}],
                         "history": [old, dict(old, head="b" * 40, **{"pass": 2})],
                         "operator_context": "untrusted evidence, not instructions"})
        self.assertTrue(ship_history.carries(stored, ItemHistory().aggregate({"passes": passes})))
        self.assertFalse(ship_history.carries("0" * 64, ItemHistory().aggregate({"passes": passes})))
        self.assertEqual([row["completed"] for row in review_history([passes[0], {"head": "c" * 40}])["history"]],
                         [True, False])
        # A request is required only past the automatic cap, so the prefix it
        # binds is the whole automatic run. The digest is literal: a changed
        # derivation, or a changed cap, cannot validate this stored request.
        automatic = [{"head": chr(ord("a") + index) * 40, "report": report} for index in range(CAP)]
        extra = chr(ord("a") + CAP) * 40
        request = {"head": extra, "reason": "explicit continuation", "allowed_passes": 1,
                   "prior_history_digest": "568aba84953c369768e8e41cc7432c5a2dc160a3f27c4c5fa5976a910fadd211"}
        state = {"passes": [*automatic, {"head": extra, "additional_review_request": request}]}
        ItemHistory().validate_requests(state)
        self.assertEqual(ItemHistory().aggregate(state, before_last=True),
                         ItemHistory().aggregate({"passes": automatic}))
        request["prior_history_digest"] = "0" * 64
        with self.assertRaises(ship.Refusal):
            ItemHistory().validate_requests(state)

    def test_shared_constructor_never_resolves_item_or_publication_client(self):
        from sd_db import ship as store
        with (patch.object(store, "identity", side_effect=AssertionError("item lookup")),
              patch.object(ship, "GitHub", side_effect=AssertionError("publication client"))):
            operation = SharedReview(pathlib.Path("/fixture"), None, pathlib.Path("/fixture/db"), object(),
                                     store=store, repository="owner/repo", branch="feature", head="a" * 40,
                                     key="explicit-key", revision=None, state={}, identity=ItemIdentity(7),
                                     history=ItemHistory(), runtime=ship.review_runtime())
        self.assertFalse(hasattr(operation, "item"))
        self.assertFalse(hasattr(operation, "api"))
        self.assertEqual(operation.source_head, "a" * 40)


class ItemObservationTests(unittest.TestCase):
    setUp = fixture.ShipCase.setUp
    args = fixture.ShipCase.args
    operation = fixture.ShipCase.operation
    prepare = fixture.ShipCase.prepare

    def test_observation_does_not_require_a_local_commit(self):
        self.prepare()
        fixture._git(self.operator, "checkout", "--orphan", "unborn-observer")
        before = (self.operator / "operator.txt").read_bytes()
        operation = ship.Ship(self.operator, self.connection, self.database, self.args("observe"))
        self.assertEqual(operation.observe()["phase"], "ready_to_send")
        self.assertEqual((self.operator / "operator.txt").read_bytes(), before)


class SharedBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sd-shared-bindings-")
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name).resolve()
        # The review declaration is a row (sd:5109): a scratch database, never the operator's.
        self.home = self.root / "home"
        home = patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config")})
        home.start()
        self.addCleanup(home.stop)
        # These pin the strict reading; `ToolChangesKeepReviewTests` pins the default (operator 2026-11-27).
        strict = patch.object(sd_lib, "TOOL_CHANGES_KEEP_REVIEWS", False)
        strict.start()
        self.addCleanup(strict.stop)
        self.head = "a" * 40
        self.report = {"status": "blocking", "scope": "branch", "subject": {"head": self.head},
                       "requested_reviews": 1, "completed_reviews": 1, "reviewed_by": ["fixture"],
                       "outcomes": [{"backend": "fixture", "status": "findings"}],
                       "check": {"status": "pass", "exit_code": 0},
                       "findings": [{"path": "a.py", "disposition": "blocking"}]}
        from sd_db import ship as store
        self.operation = SharedReview(self.root, None, self.root / "db", object(), store=store,
                                      repository="owner/repo", branch="feature", head=self.head,
                                      key="ship:fixture", revision=None, identity=ItemIdentity(7),
                                      history=ItemHistory(), runtime=ship.review_runtime(), state={
                                          "binding": ship.binding(self.root), "binding_manifest": ship.binding_manifest(self.root),
                                          "passes": [{"head": self.head, "report": self.report, "exit_code": 1}]})
        self.git = patch.object(dispositions, "git", side_effect=lambda root, *args: "" if args[0] == "status" else self.head)
        self.git.start()
        self.addCleanup(self.git.stop)

    @staticmethod
    def edited(target, suffix=b"changed"):
        original = pathlib.Path.read_bytes
        return patch.object(pathlib.Path, "read_bytes", lambda path: original(path) + (suffix if path == target else b""))

    def test_each_verdict_member_mutation_refuses_stale_review(self):
        self.assertEqual(self.operation.review_inputs(self.head), self.report)
        for name in bindings.VERDICT_FILES:
            with self.subTest(name=name), self.edited(bindings.BIN / name):
                with self.assertRaisesRegex(ship.Refusal, f"tools or repository policy changed after review: {name} \\(verdict\\)"):
                    self.operation.review_inputs(self.head)

    def test_a_comment_on_a_verdict_member_keeps_the_review(self):
        """sd:1834. The reviewer was asked the same thing; a comment cannot change a verdict."""
        for name in bindings.VERDICT_FILES:
            with self.subTest(name=name), self.edited(bindings.BIN / name, b"\n# a comment moves no verdict\n"):
                self.assertEqual(self.operation.review_inputs(self.head), self.report)

    def test_each_gate_and_check_member_mutation_keeps_the_review(self):
        """sd:1834. Gate code runs live on every prepare and merge, and the merge
        gate runs the check again; binding them spent a pass for no evidence."""
        for name in bindings.GATE_FILES + bindings.CHECK_FILES:
            with self.subTest(name=name), self.edited(bindings.BIN / name):
                self.assertEqual(self.operation.review_inputs(self.head), self.report)

    def test_a_moved_binding_names_what_moved_and_marks_what_does_not_bind(self):
        """sd:1246. The refusal names the file; nobody substitutes blobs to find it."""
        original = pathlib.Path.read_bytes
        targets = {bindings.BIN / "sd-review", bindings.BIN / "sd-ship"}
        with patch.object(pathlib.Path, "read_bytes", lambda path: original(path) + (b"changed" if path in targets else b"")):
            with self.assertRaises(ship.Refusal) as caught:
                self.operation.review_inputs(self.head)
        self.assertEqual(str(caught.exception), "review tools or repository policy changed after review: "
                                                "sd-review (verdict); also changed, not binding: sd-ship (gate)")
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "review_binding_moved")
        self.assertIn("sd-ship prepare", caught.exception.workflow["next_action"])

    def test_a_policy_change_refuses_and_is_named(self):
        seed_declaration(self.home, self.root, "review", {})
        with self.assertRaisesRegex(ship.Refusal, "changed after review: review_declaration \\(policy\\)$"):
            self.operation.review_inputs(self.head)

    def test_a_policy_file_in_the_tree_binds_nothing(self):
        """sd:5109: `.github/sd-review.json` is retired; the row's declaration is the policy."""
        (self.root / ".github").mkdir()
        (self.root / ".github" / "sd-review.json").write_text("{}")
        self.assertEqual(self.operation.review_inputs(self.head), self.report)

    def test_an_unread_declaration_refuses_rather_than_binding_absent(self):
        seed_declaration(self.home, self.root, "review", {})
        sd_lib.import_sd_db().module.default_path(self.home).write_bytes(b"not a database")
        with self.assertRaisesRegex(ship.Refusal, "review binding input cannot be read"):
            self.operation.review_inputs(self.head)

    def test_a_receipt_without_a_manifest_refuses_once_and_says_why(self):
        """Stored before sd:1834. Its binding is never backfilled from current files."""
        self.operation.state.pop("binding_manifest")
        self.operation.state["binding"] = "schema-1 digest"
        with self.assertRaisesRegex(ship.Refusal, "changed after review: receipt predates the per-file manifest \\(legacy\\)$"):
            self.operation.review_inputs(self.head)

    def test_each_adjudicator_manifest_member_mutation_refuses_stale_proposal(self):
        context, rows = dispositions.context(self.operation, self.head)
        proposal = {"schema_version": 1, "bindings": context,
                    "findings": [dict(rows[0], response_disposition="parked", reason="revisit when the scope changes")]}
        self.assertEqual(dispositions.validate(self.operation, self.head, proposal), dispositions.digest(proposal))
        # Isolate the adjudicator manifest from the earlier review-binding check.
        self.operation.review_inputs = types.MethodType(lambda operation, head: self.report, self.operation)
        original = pathlib.Path.read_bytes
        targets = [bindings.BIN / name for name in bindings.VERDICT_FILES]
        targets += [bindings.BIN.parent / name for name in bindings.ADJUDICATOR_POLICY_FILES]
        targets.append(pathlib.Path(self.operation.store.__file__))
        for target in targets:
            with self.subTest(path=target), patch.object(pathlib.Path, "read_bytes", lambda path, target=target: original(path) + (b"changed" if path == target else b"")):
                with self.assertRaisesRegex(ship.Refusal, "does not bind"):
                    dispositions.validate(self.operation, self.head, proposal)
        # sd:1834. Tool files bind by class; the library and policy above stay byte-exact.
        for name in bindings.GATE_FILES + bindings.CHECK_FILES:
            with self.subTest(gate=name), self.edited(bindings.BIN / name):
                self.assertEqual(dispositions.validate(self.operation, self.head, proposal), dispositions.digest(proposal))
        for name in bindings.VERDICT_FILES:
            with self.subTest(comment=name), self.edited(bindings.BIN / name, b"\n# a comment moves no verdict\n"):
                self.assertEqual(dispositions.validate(self.operation, self.head, proposal), dispositions.digest(proposal))

    def replaying(self, answer: str, exit_code: int = 0) -> list[list[str]]:
        """sd:1397. The `--explain` replay answers `answer`; each argv is kept, with its prior report's digest."""
        calls: list[list[str]] = []

        def process(root, argv, timeout):
            calls.append(list(argv))
            for flag in ("--verify-report", "--resume-report"):
                if flag in argv:
                    calls[-1].append(hashlib.sha256(pathlib.Path(argv[argv.index(flag) + 1]).read_bytes()).hexdigest())
            return subprocess.CompletedProcess(argv, exit_code, json.dumps({"request_sha256": answer}), "")

        self.operation.runtime = dataclasses.replace(self.operation.runtime, process=process)
        self.operation.save = lambda **updates: self.operation.state.update(updates)
        return calls

    def test_a_moved_verdict_binding_keeps_a_receipt_whose_request_replays_unchanged(self):
        """sd:1397, option E. A pack landing that leaves the reviewers' question unchanged spends no pass."""
        self.operation.state["passes"][-1]["review_request"] = {"sha256": "asked", "verify": None, "resume": None}
        calls = self.replaying("asked")
        with self.edited(bindings.BIN / "sd_route.py"):
            self.assertEqual(self.operation.review_inputs(self.head), self.report)
            self.assertEqual(self.operation.state["binding"], ship.binding(self.root))
            self.assertEqual(self.operation.review_inputs(self.head), self.report)
        self.assertEqual(len(calls), 1, "a kept receipt is rebound, so the replay runs once")
        self.assertIn("--explain", calls[0])
        self.assertNotIn("--base", calls[0])
        kept = self.operation.state["review_binding_kept"]
        self.assertEqual([row["changed"] for row in kept], [[["sd_route.py", "verdict"]]])
        self.assertEqual(kept[0]["request_sha256"], "asked")

    def test_a_replay_reads_the_rebuttal_receipts_the_pass_read(self):
        """Advisory sweep, pack sd:9973: the replay read receipts recorded since the pass, so its digest
        differed and a pass was spent. It caps the read at the newest receipt the pass read."""
        stored = {key: self.operation.state[key] for key in ("binding", "binding_manifest")}
        for through, expected in ((41, ["--rebutted-through", "41"]), (None, [])):
            self.operation.state.update(stored)
            self.operation.state["passes"][-1]["review_request"] = {"sha256": "asked", "verify": None, "resume": None,
                                                                    "rebutted_through": through}
            calls = self.replaying("asked")
            with self.subTest(through=through), self.edited(bindings.BIN / "sd_route.py"):
                self.operation.review_inputs(self.head)
            argv = calls[0]
            found = argv[argv.index("--rebutted-through"):argv.index("--rebutted-through") + 2] \
                if "--rebutted-through" in argv else []
            self.assertEqual(found, expected)

    def test_a_moved_binding_whose_request_replays_differently_still_refuses(self):
        self.operation.state["passes"][-1]["review_request"] = {"sha256": "asked", "verify": None, "resume": None}
        for answer, exit_code in (("asked differently", 0), ("asked", 1)):
            calls = self.replaying(answer, exit_code)
            with self.subTest(answer=answer, exit_code=exit_code), self.edited(bindings.BIN / "sd_route.py"):
                with self.assertRaisesRegex(ship.Refusal, "changed after review: sd_route.py \\(verdict\\)$"):
                    self.operation.review_inputs(self.head)
                self.assertEqual(len(calls), 1)
        self.assertNotIn("review_binding_kept", self.operation.state)

    def test_a_moved_finding_parser_re_reviews_even_with_an_unchanged_request(self):
        """sd:1397, operator ruling (option A). An equal request does not make an equal verdict
        when the code that parses reviewer output or disposes findings moved; no replay is asked."""
        self.operation.state["passes"][-1]["review_request"] = {"sha256": "asked", "verify": None, "resume": None}
        stored = {key: self.operation.state[key] for key in ("binding", "binding_manifest")}
        for name in ("sd-review", "sd_opencode.py", "sd_registry.py"):
            self.operation.state.update(stored)
            calls = self.replaying("asked")
            with self.subTest(name=name), self.edited(bindings.BIN / name):
                with self.assertRaisesRegex(ship.Refusal, f"changed after review: {name} \\(verdict\\)$"):
                    self.operation.review_inputs(self.head)
            self.assertEqual(calls, [], name)
        self.assertNotIn("review_binding_kept", self.operation.state)
        self.assertEqual(set(getattr(bindings, "FINDING_FILES", ())), {"sd-review", "sd_opencode.py", "sd_registry.py"})
        self.assertLessEqual(set(bindings.FINDING_FILES), set(bindings.VERDICT_FILES))

    def test_a_policy_change_is_never_kept_by_a_replay(self):
        """Policy stays byte-exact (sd:1834 operator decision); no replay is even asked."""
        self.operation.state["passes"][-1]["review_request"] = {"sha256": "asked", "verify": None, "resume": None}
        calls = self.replaying("asked")
        seed_declaration(self.home, self.root, "review", {})
        with self.assertRaisesRegex(ship.Refusal, "review_declaration \\(policy\\)$"):
            self.operation.review_inputs(self.head)
        self.assertEqual(calls, [])

    def test_a_fix_verification_replays_with_the_prior_report_it_verified(self):
        first = dict(self.report, subject={"head": "b" * 40})
        verified = {"head": self.head, "base": "b" * 40, "report": self.report, "exit_code": 1, "requested_provider": "fixture",
                    "review_request": {"sha256": "asked", "verify": digest(first), "resume": None}}
        self.operation.state["passes"] = [{"head": "b" * 40, "report": first, "exit_code": 1}, verified]
        stored = {name: self.operation.state[name] for name in ("binding", "binding_manifest")}
        calls = self.replaying("asked")
        with self.edited(bindings.BIN / "sd_route.py"):
            self.assertFalse(self.operation.binding_moved())
        argv = calls[0]
        self.assertEqual(argv[argv.index("--base") + 1], "b" * 40)
        self.assertEqual(argv[argv.index("--provider") + 1], "fixture")
        self.assertEqual(argv[-1], digest(first), "the replay hands over the very report the pass verified")
        # A prior that no longer reproduces from the stored history is not guessed at.
        verified["review_request"]["verify"] = "a report nobody stored"
        self.operation.state.update(stored)
        del self.operation._binding_moved
        calls.clear()
        with self.edited(bindings.BIN / "sd_route.py"):
            self.assertTrue(self.operation.binding_moved())
        self.assertEqual(calls, [])

    def retired(self, kind: str):
        """sd:3272. The item was reviewed while `sd_retired.py` was a `kind` member; the file is now gone."""
        name, target = "sd_retired.py", bindings.BIN / "sd_retired.py"
        original, present = pathlib.Path.read_bytes, [True]

        def read(path):
            if path != target:
                return original(path)
            if present[0]:
                return b"RETIRED = True\n"
            raise FileNotFoundError(2, "No such file or directory")

        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(bindings, kind, getattr(bindings, kind) + (name,)))
        stack.enter_context(patch.object(pathlib.Path, "read_bytes", read))
        self.operation.state.update(binding=ship.binding(self.root), binding_manifest=ship.binding_manifest(self.root))
        present[0] = False
        return stack

    def test_a_check_member_retired_after_review_keeps_the_review(self):
        """sd:3272: sd:3216 retired `sd_gate_tools.py`, and prepare refused a receipt
        bound with it as a prerequisite failure. Check code binds no receipt."""
        with self.retired("CHECK_FILES"):
            self.assertEqual(bindings.binding_manifest(self.root)["check"]["sd_retired.py"], bindings.ABSENT)
            self.assertEqual(self.operation.review_inputs(self.head), self.report)
        # The version that dropped it from the tuple reads the same receipt.
        self.assertEqual(self.operation.review_inputs(self.head), self.report)

    def test_a_verdict_member_retired_after_review_reads_as_moved(self):
        with self.retired("VERDICT_FILES"):
            with self.assertRaises(ship.Refusal) as caught:
                self.operation.review_inputs(self.head)
        self.assertEqual(str(caught.exception), "review tools or repository policy changed after review: sd_retired.py (verdict)")
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "review_binding_moved")

    def test_missing_manifest_members_read_absent_never_a_partial_binding(self):
        """A missing member is recorded `absent`, never dropped, so it can match no stored hash (sd:3272)."""
        original = pathlib.Path.read_bytes
        full = ship.binding(self.root)
        adjudicator = bindings.adjudicator_binding(self.operation.store.__file__)
        for name in bindings.REVIEW_TOOL_FILES:
            target = bindings.BIN / name

            def missing(path, target=target):
                if path == target:
                    raise FileNotFoundError(2, "fixture missing member")
                return original(path)

            with self.subTest(name=name), patch.object(pathlib.Path, "read_bytes", missing):
                manifest = ship.binding_manifest(self.root)
                kind = next(kind for kind in ("verdict", "gate", "check") if name in manifest[kind])
                self.assertEqual(manifest[kind][name], bindings.ABSENT)
                if kind == "verdict":
                    self.assertNotEqual(ship.binding(self.root), full)
                    self.assertNotEqual(bindings.adjudicator_binding(self.operation.store.__file__), adjudicator)

    def test_an_unreadable_manifest_member_still_refuses(self):
        """A failed read is no answer: only a missing file reads `absent`."""
        original = pathlib.Path.read_bytes
        for name in bindings.REVIEW_TOOL_FILES:
            target = bindings.BIN / name

            def unreadable(path, target=target):
                if path == target:
                    raise PermissionError(13, "Permission denied")
                return original(path)

            with self.subTest(name=name), patch.object(pathlib.Path, "read_bytes", unreadable):
                with self.assertRaisesRegex(ship.Refusal, "required review binding file cannot be read: .*Permission denied"):
                    ship.binding_manifest(self.root)


class HistoryChainTests(unittest.TestCase):
    """The chain a raised cap makes longer, checked at every link.

    Both cases below were reported against the cap raise and are the reason
    the coverage rule reads each stored entry rather than its position.
    """

    @staticmethod
    def _complete(base: str, head: str, previous: dict | None = None) -> dict:
        report = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
                  "authorship_base": base, "subject": {"base": base, "head": head}}
        if previous is not None:
            report["verification_report_digest"] = digest(previous.get("report") or {})
        return report

    def _chain(self, length: int) -> tuple[dict, dict]:
        """`length` stored passes; the last one's report is the one read now."""
        base = "0" * 40
        passes: list[dict] = []
        for index in range(length):
            head = f"{index + 1:040x}"
            previous = passes[-1] if passes else None
            passes.append({"head": head, "report": self._complete(
                base if previous is None else previous["head"], head, previous)})
        return {"passes": passes}, passes[-1]["report"]

    def test_a_stale_digest_in_the_middle_of_the_chain_is_refused(self):
        state, report = self._chain(4)
        ItemHistory()._validate_coverage(state, report)
        # The mutation is two links back, where a check on the last link
        # alone cannot reach it.
        state["passes"][0]["report"]["status"] = "mutated after the fact"
        with self.assertRaisesRegex(ship.Refusal, "does not continue"):
            ItemHistory()._validate_coverage(state, report)

    def test_an_explicit_request_below_the_cap_still_takes_the_full_branch_rule(self):
        base = "0" * 40
        incomplete = {"status": "clean", "requested_reviews": 2, "completed_reviews": 1,
                      "authorship_base": base, "subject": {"base": base, "head": "a" * 40}}
        passes = [{"head": "a" * 40, "report": incomplete}, {"head": "b" * 40, "report": incomplete}]
        requested = {"head": "c" * 40, "reason": "operator asked", "allowed_passes": 1,
                     "prior_history_digest": digest(passes)}
        full = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
                "authorship_base": base, "subject": {"base": base, "head": "c" * 40},
                "resume_report_digest": digest(review_history(passes))}
        state = {"passes": [*passes, {"head": "c" * 40, "report": full, "additional_review_request": requested}]}
        # Two incomplete passes then an explicitly requested full-branch
        # review: valid under the cap that was in force when it was written,
        # and the entry says so whatever the cap is now.
        self.assertIsNone(ItemHistory()._validate_coverage(state, full))
        # Which rule it took, asserted rather than assumed: only the
        # full-branch rule reads `resume_report_digest`, so breaking that one
        # field must refuse. The chain rule would have refused on the two
        # incomplete predecessors instead, with a different sentence.
        broken = dict(full, resume_report_digest=digest({"not": "the prior history"}))
        state["passes"][-1]["report"] = broken
        with self.assertRaisesRegex(ship.Refusal, "full-branch coverage does not match"):
            ItemHistory()._validate_coverage(state, broken)

    def test_an_automatic_verification_after_a_full_review_needs_no_earlier_completion(self):
        base = "0" * 40
        incomplete = {"status": "clean", "requested_reviews": 2, "completed_reviews": 1,
                      "authorship_base": base, "subject": {"base": base, "head": "a" * 40}}
        passes = [{"head": "a" * 40, "report": incomplete}, {"head": "b" * 40, "report": incomplete}]
        full = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
                "authorship_base": base, "subject": {"base": base, "head": "c" * 40},
                "resume_report_digest": digest(review_history(passes))}
        checkpoint = {"head": "c" * 40, "report": full,
                      "additional_review_request": {"head": "c" * 40, "reason": "operator asked",
                                                    "allowed_passes": 1, "prior_history_digest": digest(passes)}}
        verification = self._complete("c" * 40, "d" * 40, checkpoint)
        state = {"passes": [*passes, checkpoint, {"head": "d" * 40, "report": verification}]}
        # The full review covered the whole branch, so the two incomplete
        # passes it superseded cannot hold the verification after it.
        self.assertIsNone(ItemHistory()._validate_coverage(state, verification))
        broken = dict(verification, verification_report_digest=digest({"not": full}))
        state["passes"][-1]["report"] = broken
        with self.assertRaisesRegex(ship.Refusal, "does not continue"):
            ItemHistory()._validate_coverage(state, broken)

    def test_a_stale_resume_link_on_an_intermediate_retry_is_refused(self):
        base = "0" * 40
        incomplete = {"status": "clean", "requested_reviews": 2, "completed_reviews": 1,
                      "authorship_base": base, "subject": {"base": base, "head": "a" * 40}}
        first = {"head": "a" * 40, "report": incomplete}
        # `first` completed 1 of the 2 reviews it requested, so it verified
        # nothing and the retry resumes the aggregate before it, not that
        # pass's own report.
        resumed = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
                   "authorship_base": base, "subject": {"base": base, "head": "b" * 40},
                   "resume_report_digest": digest(review_history([first]))}
        retry = {"head": "b" * 40, "report": resumed, "retry": True}
        verification = self._complete("b" * 40, "c" * 40, retry)
        state = {"passes": [first, retry, {"head": "c" * 40, "report": verification}]}
        self.assertIsNone(ItemHistory()._validate_coverage(state, verification))
        # Mutating the report the retry resumed leaves the retry's own
        # resume link stale, two entries back from the report read now.
        state["passes"][0]["report"] = dict(incomplete, status="mutated after the fact")
        with self.assertRaisesRegex(ship.Refusal, "retain the incomplete review evidence"):
            ItemHistory()._validate_coverage(state, verification)

    def test_a_retry_that_produced_no_report_is_resumed_by_the_next_one(self):
        base = "0" * 40
        incomplete = {"status": "clean", "requested_reviews": 2, "completed_reviews": 1,
                      "authorship_base": base, "subject": {"base": base, "head": "a" * 40}}
        first = {"head": "a" * 40, "report": incomplete}
        # The attempt between them timed out: it reserved a pass and stored no
        # report at all, which is not a stale link, only an absent one.
        timed_out = {"head": "b" * 40, "retry": True,
                     "execution_error": {"kind": "watchdog_expired", "stage": "execution",
                                         "captured_report": {"scope": "branch", "findings": [],
                                                             "authored_with": [],
                                                             "subject": {"head": "b" * 40}}}}
        resumed = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
                   "authorship_base": base, "subject": {"base": base, "head": "c" * 40},
                   "resume_report_digest": digest(review_history([first, timed_out]))}
        state = {"passes": [first, timed_out, {"head": "c" * 40, "report": resumed, "retry": True}]}
        self.assertIsNone(ItemHistory()._validate_coverage(state, resumed))
        broken = dict(resumed, resume_report_digest=digest({"not": "the prior history"}))
        state["passes"][-1]["report"] = broken
        with self.assertRaisesRegex(ship.Refusal, "retain the incomplete review evidence"):
            ItemHistory()._validate_coverage(state, broken)

    def _request_past(self, reservation: dict) -> tuple[dict, dict]:
        """A completed pass at a, `reservation` at b, then a request at c verifying a (sd:2192)."""
        first = {"head": "a" * 40, "report": self._complete("0" * 40, "a" * 40)}
        request = {"head": "c" * 40, "reason": "operator asked", "allowed_passes": 1,
                   "prior_history_digest": digest([first, reservation])}
        verification = self._complete("a" * 40, "c" * 40, first)
        return {"passes": [first, reservation, {"head": "c" * 40, "base": "a" * 40, "report": verification,
                                                "additional_review_request": request}]}, verification

    def test_a_request_links_past_a_reservation_that_holds_no_evidence(self):
        """sd:2192. The chain check reads the request's link to the last completed pass."""
        unreadable = {"head": "b" * 40, "base": "a" * 40,
                      "execution_error": {"kind": "unreadable_receipt", "stage": "execution"}}
        self.assertEqual(importlib.import_module("sd_ship_history").verified_index([{"head": "a" * 40, "report": self._complete(
            "0" * 40, "a" * 40)}, unreadable]), 0)
        state, verification = self._request_past(unreadable)
        self.assertIsNone(ItemHistory()._validate_coverage(state, verification))

    def test_a_request_does_not_link_past_a_reservation_that_kept_a_finding(self):
        """sd:2192. Only a full-branch resume keeps that finding, so the skip-link refuses."""
        incomplete = dict(self._complete("a" * 40, "b" * 40, {"report": self._complete("0" * 40, "a" * 40)}),
                          status="blocking", requested_reviews=2,
                          findings=[{"path": "src.py", "summary": "found by the reviewer that completed"}])
        state, verification = self._request_past({"head": "b" * 40, "base": "a" * 40, "report": incomplete})
        with self.assertRaisesRegex(ship.Refusal, "never completed"):
            ItemHistory()._validate_coverage(state, verification)

    def test_a_request_does_not_link_past_a_failed_full_branch_review(self):
        """sd:2192. A binding re-review that failed still owes the whole branch.

        Dispatch stored the new binding before the pass ran, so after it failed
        the binding no longer reads as moved. Skipping the reservation would
        verify the fix against a report written under the old policy.
        """
        verified_index = importlib.import_module("sd_ship_history").verified_index
        first = {"head": "a" * 40, "report": self._complete("0" * 40, "a" * 40)}
        moved = {"head": "a" * 40, "base": None,
                 "review_binding_change": {"head": "a" * 40, "superseded_binding": "old policy", "changed": []},
                 "execution_error": {"kind": "unreadable_receipt", "stage": "execution"}}
        self.assertIsNone(verified_index([first, moved]))
        # A retry of it that also failed does not clear what it owed.
        retried = {"head": "a" * 40, "base": None, "retry": True,
                   "execution_error": {"kind": "unreadable_receipt", "stage": "execution"}}
        self.assertIsNone(verified_index([first, moved, retried]))
        state, verification = self._request_past(moved)
        with self.assertRaisesRegex(ship.Refusal, "never completed"):
            ItemHistory()._validate_coverage(state, verification)

    def test_an_automatic_verification_that_produced_no_report_is_resumed_not_refused(self):
        """A reservation is not a verification, whatever the next pass calls itself.

        Reported against the cap raise: the reportless exemption reached only
        entries marked `retry`, so a verification that timed out was read as a
        stale link and refused the recovery that follows it. The chain is only
        long enough to hold one once the cap allows five.
        """
        base = "0" * 40
        complete = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
                    "authorship_base": base, "subject": {"base": base, "head": "a" * 40}}
        first = {"head": "a" * 40, "report": complete}
        # No `retry` key: this reserved an automatic verification and timed out.
        timed_out = {"head": "b" * 40,
                     "execution_error": {"kind": "watchdog_expired", "stage": "execution",
                                         "captured_report": {"scope": "branch", "findings": [],
                                                             "authored_with": [],
                                                             "subject": {"head": "b" * 40}}}}
        resumed = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
                   "authorship_base": base, "subject": {"base": base, "head": "c" * 40},
                   "resume_report_digest": digest(review_history([first, timed_out]))}
        state = {"passes": [first, timed_out, {"head": "c" * 40, "report": resumed, "retry": True}]}
        self.assertIsNone(ItemHistory()._validate_coverage(state, resumed))
        # An attempt that died without parseable evidence is the same
        # reservation: a watchdog leaves a captured report, an unreadable
        # receipt leaves nothing, and neither verified anything.
        state["passes"][1] = {"head": "b" * 40,
                              "execution_error": {"kind": "unreadable_receipt", "stage": "execution"}}
        bare = dict(resumed, resume_report_digest=digest(
            review_history([first, state["passes"][1]])))
        state["passes"][-1]["report"] = bare
        self.assertIsNone(ItemHistory()._validate_coverage(state, bare))
        # The exemption reaches the reservation, not what follows it: a plain
        # verification cannot continue from a pass that produced no report.
        state["passes"][-1] = {"head": "c" * 40, "report": dict(
            bare, subject={"base": "b" * 40, "head": "c" * 40})}
        with self.assertRaisesRegex(ship.Refusal, "never completed the requested local review depth"):
            ItemHistory()._validate_coverage(state, state["passes"][-1]["report"])

    def test_a_retry_after_a_reportless_one_still_carries_the_earlier_blockers(self):
        """An absent report is not a licence to forget what came before it.

        Reported at high severity against the reportless exemption: the
        aggregate a retry must resume was taken only when the attempt left
        parseable timeout evidence, so an attempt that died leaving nothing
        let an earlier pass's blocking findings out of the evidence the next
        retry carries -- the case that most needs them kept.
        """
        base = "0" * 40
        blocking = {"status": "blocking", "requested_reviews": 1, "completed_reviews": 1,
                    "authorship_base": base, "subject": {"base": base, "head": "a" * 40},
                    "findings": [{"path": "bin/x.py", "summary": "a real blocker"}],
                    "authored_with": ["codex"]}
        first = {"head": "a" * 40, "report": blocking}
        reportless = {"head": "b" * 40, "retry": True,
                      "execution_error": {"kind": "unreadable_receipt", "stage": "execution"}}
        carried = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
                   "authorship_base": base, "subject": {"base": base, "head": "c" * 40},
                   "resume_report_digest": digest(review_history([first, reportless]))}
        state = {"passes": [first, reportless, {"head": "c" * 40, "report": carried, "retry": True}]}
        self.assertIsNone(ItemHistory()._validate_coverage(state, carried))
        # Dropping the earlier evidence is what the rule refuses, and the
        # blocker is what would have been dropped.
        dropped = dict(carried, resume_report_digest=None)
        state["passes"][-1]["report"] = dropped
        with self.assertRaisesRegex(ship.Refusal, "retain the incomplete review evidence"):
            ItemHistory()._validate_coverage(state, dropped)

    def test_a_failed_verification_that_wrote_a_report_forgets_nothing(self):
        """A report is not evidence that anybody reviewed anything.

        Reported at high severity against the reportless exemption above: a
        verification that completed no review still emits a report, and its
        findings list is empty. Read as the latest word it supersedes the
        completed blocking review before it, and the retry that follows is
        handed zero blockers -- the same continuity hole as an absent report,
        wearing the shape of evidence.
        """
        base = "0" * 40
        blocking = {"status": "blocking", "requested_reviews": 1, "completed_reviews": 1,
                    "authorship_base": base, "subject": {"base": base, "head": "a" * 40},
                    "findings": [{"path": "bin/x.py", "summary": "a real blocker"}],
                    "authored_with": ["codex"]}
        first = {"head": "a" * 40, "report": blocking}
        # Nobody completed: 1 requested, 0 done, and so nothing found.
        failed = {"status": "clean", "requested_reviews": 1, "completed_reviews": 0,
                  "authorship_base": "a" * 40, "subject": {"base": "a" * 40, "head": "b" * 40},
                  "findings": [], "authored_with": [],
                  "verification_report_digest": digest(blocking)}
        second = {"head": "b" * 40, "report": failed}
        carried = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
                   "authorship_base": base, "subject": {"base": base, "head": "c" * 40},
                   "resume_report_digest": digest(review_history([first, second]))}
        state = {"passes": [first, second, {"head": "c" * 40, "report": carried, "retry": True}]}
        self.assertIsNone(ItemHistory()._validate_coverage(state, carried))
        # The aggregate the dispatcher hands out is the aggregate the
        # validator asks for, and the blocker is in it.
        handed = ItemHistory().prior({"passes": [first, second]})
        self.assertEqual([row["summary"] for row in handed["findings"]], ["a real blocker"])
        # Carrying the failed verification's own report instead is what the
        # rule refuses, and it is exactly what drops the blocker.
        dropped = dict(carried, resume_report_digest=digest(failed))
        state["passes"][-1]["report"] = dropped
        with self.assertRaisesRegex(ship.Refusal, "retain the incomplete review evidence"):
            ItemHistory()._validate_coverage(state, dropped)

    def test_a_stored_retry_carrying_only_the_preceding_report_is_refused(self):
        """The rule reads a stored pass exactly as it reads a live one.

        This history is the one the removed exemption let through: a retry two
        entries back whose `resume_report_digest` is the preceding report's
        own digest rather than the aggregate before it. Position decided the
        verdict, so the same receipt passed here and failed as the last pass.
        It is refused in both places now, and this test is what keeps it so.
        """
        base = "0" * 40
        incomplete = {"status": "blocking", "requested_reviews": 2, "completed_reviews": 1,
                      "authorship_base": base, "subject": {"base": base, "head": "a" * 40},
                      "findings": [{"path": "bin/x.py", "summary": "a real blocker"}],
                      "authored_with": ["codex"]}
        first = {"head": "a" * 40, "report": incomplete}
        legacy = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
                  "authorship_base": base, "subject": {"base": base, "head": "b" * 40},
                  "resume_report_digest": digest(incomplete)}
        second = {"head": "b" * 40, "report": legacy, "retry": True}
        now = {"status": "clean", "requested_reviews": 1, "completed_reviews": 1,
               "authorship_base": "b" * 40, "subject": {"base": "b" * 40, "head": "c" * 40},
               "verification_report_digest": digest(legacy)}
        state = {"passes": [first, second, {"head": "c" * 40, "report": now}]}
        with self.assertRaisesRegex(ship.Refusal, "retain the incomplete review evidence"):
            ItemHistory()._validate_coverage(state, now)
        # Non-vacuous: the aggregate digest is the one value that clears it,
        # so the refusal above names the digest and not some other defect in
        # the history this test builds.
        second["report"] = dict(legacy, resume_report_digest=digest(review_history([first])))
        state["passes"][-1]["report"] = dict(now, verification_report_digest=digest(second["report"]))
        self.assertIsNone(ItemHistory()._validate_coverage(state, state["passes"][-1]["report"]))


class ToolChangesKeepReviewTests(unittest.TestCase):
    """Operator 2026-11-27: while the tools change daily, a tool change alone keeps a review; policy re-reviews."""

    edited = staticmethod(SharedBindingTests.edited)

    def setUp(self):
        SharedBindingTests.setUp(self)
        lenient = patch.object(sd_lib, "TOOL_CHANGES_KEEP_REVIEWS", True)
        lenient.start()
        self.addCleanup(lenient.stop)
        self.operation.save = lambda **updates: self.operation.state.update(updates)

    def test_a_moved_review_tool_keeps_the_receipt_without_a_replay(self):
        self.operation.runtime = dataclasses.replace(
            self.operation.runtime, process=lambda *a, **k: self.fail("no replay is asked"))
        for name in ("sd-review", "sd_route.py", "sd_registry.py"):
            with self.subTest(name=name), self.edited(bindings.BIN / name):
                self.assertEqual(self.operation.review_inputs(self.head), self.report)
                self.assertEqual(self.operation.state["binding"], ship.binding(self.root))
        kept = self.operation.state["review_binding_kept"]
        self.assertEqual(len(kept), 3, "each move is rebound once and recorded")
        self.assertEqual(kept[0]["changed"], [["sd-review", "verdict"]])
        self.assertIn(["sd_registry.py", "verdict"], kept[-1]["changed"])
        self.assertTrue(all(row["tools_only"] for row in kept))

    def test_a_policy_change_still_re_reviews(self):
        seed_declaration(self.home, self.root, "review", {})
        with self.assertRaisesRegex(ship.Refusal, "review_declaration \\(policy\\)$"):
            self.operation.review_inputs(self.head)

    def test_a_normalizer_move_does_not_hide_a_policy_change(self):
        stored = self.operation.state["binding_manifest"]
        self.operation.state["binding_manifest"] = dict(stored, normalizer="py-old")
        seed_declaration(self.home, self.root, "review", {})
        with self.assertRaises(ship.Refusal):
            self.operation.review_inputs(self.head)
        self.assertNotIn("review_binding_kept", self.operation.state)

    def test_a_lead_review_binding_ignores_only_the_tools_entry(self):
        stored = {"tools": "old", "local": "L", "review_declaration": {"x": 1}}
        self.assertTrue(sd_lib.same_review_binding(stored, dict(stored, tools="new")))
        self.assertFalse(sd_lib.same_review_binding(stored, dict(stored, local="L2")))
        with patch.object(sd_lib, "TOOL_CHANGES_KEEP_REVIEWS", False):
            self.assertFalse(sd_lib.same_review_binding(stored, dict(stored, tools="new")))

