"""Itemless publication shares the existing gates and retains its review history."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import unittest
from unittest.mock import patch

from sd_db import connect, create_assignment, create_item, initialise, upsert_repo
from sd_db import ship as receipts
from sd_db.testing.remote import _git

from tests import test_sd_ship as fixture
from tests import test_sd_ship_no_item as review_fixture

ship = fixture.ship
no_item = review_fixture.no_item


class NoItemPublication(unittest.TestCase):
    #: A commit message to add on the branch before the record is made.
    extra_commit: str | None = None

    def setUp(self):
        fixture.ShipCase.setUp(self)
        review_fixture.freeze_library(self, self.directory)
        if getattr(self, "extra_commit", None):
            _git(self.root, "commit", "--allow-empty", "-qm", self.extra_commit)
        self.database = self.database.with_name("no-items.db")
        initialise(self.database)
        self.connection = connect(self.database)
        self.addCleanup(self.connection.close)
        self.head = _git(self.root, "rev-parse", "HEAD")
        self.base = _git(self.root, "rev-parse", "origin/main")
        result = self.invoke("review", "--create-record", "--assert-new-work")
        self.review_id = result["review_id"]
        self.key = no_item.review_key("fixture/repo", self.review_id)
        revision, state = receipts.read(self.connection, self.key)
        report = review_fixture.NoItemContracts.report(self)
        state.update(passes=[{"head": self.head, "report": report, "exit_code": 0}],
                     head=self.head, reviewed_head=self.head, binding=ship.binding(self.root), phase="reviewed")
        receipts.save(self.connection, self.key, revision, no_item.with_digest(state))
        self.initial_history = no_item.combined_digest(state)

    def invoke(self, command, *extra, code=0, root=None):
        output = io.StringIO()
        previous = pathlib.Path.cwd()
        args = [command, "--no-item", "--json", "--database", str(self.database), *extra]
        if hasattr(self, "review_id"):
            args += ["--review-id", self.review_id]
        with (contextlib.redirect_stdout(output),
              patch.object(ship, "review_process", side_effect=AssertionError("reused review dispatched a provider")),
              patch.object(receipts, "identity", side_effect=AssertionError("item lookup")),
              patch.object(receipts, "note_merge", side_effect=AssertionError("item completion"))):
            try:
                os.chdir(root or self.root)
                actual = ship.main(args)
            finally:
                os.chdir(previous)
        result = json.loads(output.getvalue())
        self.assertEqual(actual, code, result)
        self.assertEqual(result["ok"], code == 0, result)
        return result

    def prepare(self):
        return self.invoke("prepare")

    def merge(self, **kwargs):
        return self.invoke("merge", "--manual", "--expected-head", self.head, **kwargs)

    def test_native_review_is_reused_through_publication_and_merge_without_item_rows(self):
        prepared = self.prepare()
        self.assertEqual(prepared["phase"], "ready_to_send")
        self.prepare()
        merged = self.merge()
        self.assertEqual(merged["phase"], "merged")
        self.assertEqual(merged["workflow"]["state"], "success")
        _revision, state = receipts.read(self.connection, self.key)
        self.assertEqual(no_item.combined_digest(state), self.initial_history)
        self.assertEqual(len(state["passes"]), 1)
        self.assertNotIn("item", state)
        self.assertNotIn("deliver", state)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM item").fetchone()[0], 0)
        body = state["merge_message"]
        self.assertNotRegex(body, r"(?m)^(?:Item|Delivers|Work):")
        self.assertNotIn("Authored-with:", body)
        self.assertEqual(len([call for call in self.remote.calls if call.method == "PUT"]), 1)

    def test_observe_is_read_only_after_checkout_moves_and_becomes_dirty(self):
        self.prepare()
        before = list(self.connection.iterdump())
        _git(self.root, "checkout", "--detach", "origin/main")
        (self.root / "unrelated.txt").write_text("unfinished work")
        result = self.invoke("observe")
        self.assertTrue(result["observed_only"])
        self.assertEqual(result["branch"], "topic")
        self.assertEqual(list(self.connection.iterdump()), before)

    def test_reconcile_accepts_no_remaining_diff_and_preserves_history(self):
        self.prepare()
        merged = self.merge()
        _git(self.root, "reset", "--hard", "origin/main")
        result = self.invoke("reconcile")
        self.assertEqual(result["merge_commit"], merged["merge_commit"])
        self.assertEqual(self.prepare()["merge_commit"], merged["merge_commit"])
        self.assertEqual(no_item.combined_digest(receipts.read(self.connection, self.key)[1]), self.initial_history)

    def test_merge_requires_manual_authority_and_all_existing_remote_guards(self):
        self.prepare()
        result = self.invoke("merge", "--expected-head", self.head, code=3)
        self.assertEqual(result["workflow"]["blocker"]["code"], "merge_authority_required")
        self.remote.protection = None
        self.assertIn("not protected", self.merge(code=3)["error"].lower())
        self.assertFalse([call for call in self.remote.calls if call.method == "PUT"])

    def test_stale_head_and_missing_ci_never_dispatch_merge(self):
        self.prepare()
        self.invoke("merge", "--manual", "--expected-head", "0" * 40, code=3)
        self.remote.pull(1).checks = []
        result = self.merge(code=3)
        self.assertEqual(result["workflow"]["blocker"]["code"], "ci_missing")
        self.assertFalse([call for call in self.remote.calls if call.method == "PUT"])

    def test_lost_create_response_reuses_pr_without_spending_another_review(self):
        self.double.lose_create = True
        self.invoke("prepare", code=3)
        self.double.lose_create = False
        self.prepare()
        self.assertEqual(len(self.remote.pull_requests), 1)
        self.assertEqual(no_item.combined_digest(receipts.read(self.connection, self.key)[1]), self.initial_history)

    def test_lost_merge_response_reconciles_without_second_merge(self):
        self.prepare()
        self.double.lose_merge = True
        self.merge()
        self.invoke("reconcile")
        self.assertEqual(len([call for call in self.remote.calls if call.method == "PUT"]), 1)

    def test_a_merged_record_reconciles_and_closes_from_the_default_branch(self):
        """sd:1932: once the merge deletes its branch, the record finishes from main.

        Closing used to need the branch recreated at its old head. Reconcile
        reads GitHub and the default branch, never this checkout's branch, so
        the branch guard is lifted for a merged record only.
        """
        self.prepare()
        _git(self.root, "checkout", "-q", "main")
        self.assertIn("bound to branch topic, not main", self.invoke("reconcile", code=3)["error"])
        _git(self.root, "checkout", "-q", "topic")
        merged = self.merge()
        _git(self.root, "checkout", "-q", "main")
        _git(self.root, "fetch", "-q", "origin")
        _git(self.root, "reset", "-q", "--hard", "origin/main")
        _git(self.root, "branch", "-q", "-D", "topic")
        self.assertEqual(self.invoke("reconcile")["merge_commit"], merged["merge_commit"])
        self.assertEqual(self.invoke("review", "--close-record", "fixture work merged")["lifecycle"], "closed")
        self.assertRegex(self.invoke("reconcile", code=3)["error"], "is closed; reopen it")
        self.assertEqual(no_item.combined_digest(receipts.read(self.connection, self.key)[1]), self.initial_history)

    def test_a_branch_switch_after_validation_is_refused_at_the_lock(self):
        """The lock compares the checkout with the branch the record validated.

        Read again after validation, a switch in between becomes the expected
        branch, and the lock would publish another branch's HEAD under this
        record. The switch lands at the same commit, so only the branch check
        can refuse it.
        """
        self.prepare()
        _git(self.root, "branch", "-q", "other")
        validated = no_item.publication_review

        def then_switch(*args, **kwargs):
            review = validated(*args, **kwargs)
            _git(self.root, "checkout", "-q", "other")
            return review

        with patch.object(no_item, "publication_review", side_effect=then_switch):
            result = self.merge(code=3)
        self.assertIn("the checkout left topic", result["error"])
        self.assertFalse([call for call in self.remote.calls if call.method == "PUT"])

    def test_item_runner_and_commit_flags_refuse_before_any_database_write(self):
        before = list(self.connection.iterdump())
        for command, flags in (("prepare", ["--deliver"]), ("prepare", ["--path", "src.py"]),
                               ("prepare", ["--acceptance-file", "missing"])):
            with self.subTest(flags=flags):
                result = self.invoke(command, *flags, code=3)
                self.assertEqual(result["workflow"]["blocker"]["code"], "no_item_flags_refused")
        self.assertEqual(list(self.connection.iterdump()), before)

    def test_item_trailers_are_stripped_from_a_no_item_publication(self):
        """sd:2999: a stray link is dropped, so the published body links no item."""
        body = self.directory / "body.md"
        for trailer in ("Work", "Item", "Delivers"):
            with self.subTest(trailer=trailer):
                body.write_text(f"Proposed change\n\n{trailer}: sd:9\n")
                self.assertEqual(self.invoke("prepare", "--body-file", str(body))["normalized"], [f"{trailer}: sd:9"])
                self.assertNotIn("sd:9", self.remote.pull_requests[1].body)

    def test_a_reprepare_that_does_not_publish_its_body_merges_nothing(self):
        """Lead review of sd:9268 at 50b91b935: a prepare after the pull request exists saves its body as
        pending, and a failure before `ready_to_send` left it there; merge squashed the reviewed head with the
        previous prepare's body. While a prepare's pending set is unpublished, merge refuses; once a prepare
        finishes, it merges with the new body."""
        self.prepare()
        body = self.directory / "body.md"
        body.write_text("The newer description.\n")
        with patch.object(ship.Ship, "prepare_copilot_review", side_effect=ship.Refusal("copilot step failed")):
            self.invoke("prepare", "--body-file", str(body), code=3)
        refused = self.merge(code=3)
        self.assertEqual(refused["workflow"]["blocker"]["code"], "prepare_required", refused)
        self.assertFalse([call for call in self.remote.calls if call.method == "PUT"])
        self.invoke("prepare", "--body-file", str(body))
        self.assertEqual(self.merge()["phase"], "merged")
        self.assertIn("The newer description.", receipts.read(self.connection, self.key)[1]["merge_message"])

    def accepted_blocker(self):
        revision, state = receipts.read(self.connection, self.key)
        state["passes"][0].update(report=review_fixture.NoItemContracts.report(self, blocking=True), exit_code=1)
        state["reviewed_head"] = None
        receipts.save(self.connection, self.key, revision, no_item.with_digest(state))
        proposal = self.invoke("adjudicate", "--expected-head", self.head)["proposal"]
        for row in proposal["findings"]:
            row.update(response_disposition="rebutted", reason="src.py:1 already handles the case the finding names")
        path = self.directory / "proposal.json"
        path.write_text(json.dumps(proposal))
        self.invoke("adjudicate", "--expected-head", self.head, "--dispositions-file", str(path))
        return proposal, path

    def test_recorded_dispositions_survive_prepare_and_are_rechecked_at_merge(self):
        proposal, path = self.accepted_blocker()
        history = no_item.combined_digest(receipts.read(self.connection, self.key)[1])
        prepared = self.prepare()
        self.assertEqual(prepared["review_clearance"]["kind"], "adjudicated")
        proposal["findings"][0]["reason"] = "a later rebuttal recorded after prepare"
        path.write_text(json.dumps(proposal))
        self.invoke("adjudicate", "--expected-head", self.head, "--dispositions-file", str(path))
        refused = self.merge(code=3)
        self.assertRegex(refused["error"], "prepare again")
        self.assertEqual(no_item.combined_digest(receipts.read(self.connection, self.key)[1]), history)
        self.assertFalse([call for call in self.remote.calls if call.method == "PUT"])

    def test_manual_guard_reruns_immediately_before_dispatch(self):
        self.prepare()
        original = receipts.manual_merge_guard
        calls = []

        def moved(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise ship.Refusal("concurrent assignment appeared")
            return original(*args, **kwargs)

        with patch.object(receipts, "manual_merge_guard", side_effect=moved):
            self.assertIn("concurrent assignment", self.merge(code=3)["error"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(row == {"repository": "fixture/repo"} for row in calls))
        self.assertFalse([call for call in self.remote.calls if call.method == "PUT"])

    def test_old_library_refuses_canonical_manual_guard_without_dispatch(self):
        self.prepare()
        with patch.object(receipts, "manual_merge_guard", side_effect=TypeError("unexpected keyword")):
            result = self.merge(code=3)
        self.assertEqual(result["workflow"]["blocker"]["code"], "library_incompatible")
        self.assertFalse([call for call in self.remote.calls if call.method == "PUT"])

    def test_assignment_in_another_registered_clone_blocks_itemless_merge(self):
        self.prepare()
        upsert_repo(self.connection, str(self.operator), remote=self.remote_url, managed=1)
        item = create_item(self.connection, kind="work", title="concurrent work", status="in_progress",
                           repo=str(self.operator), branch="other")
        create_assignment(self.connection, item=item, role="author", status="ending")
        result = self.merge(code=3)
        self.assertIn("manual merge authority", result["error"])
        self.assertFalse([call for call in self.remote.calls if call.method == "PUT"])

    # The strict reading (sd:10929): by default a tool change keeps the clearance (sd:10928).
    @patch.object(fixture.ship.sd_lib, "TOOL_CHANGES_KEEP_REVIEWS", False)
    def test_cross_skill_receipt_policy_change_invalidates_accepted_clearance(self):
        reference = fixture.ROOT / "skills/sd-check/references/check-receipts.md"
        content = b"fixture receipt policy before acceptance"
        original_read = pathlib.Path.read_bytes

        def policy_bytes(path):
            return content if path == reference else original_read(path)

        with patch.object(pathlib.Path, "read_bytes", policy_bytes):
            self.accepted_blocker()
            self.prepare()
            content = b"changed receipt reuse and acceptance policy"
            result = self.merge(code=3)
        self.assertIn("does not bind", result["error"])
        self.assertFalse([call for call in self.remote.calls if call.method == "PUT"])



class NoItemDeliversWarning(unittest.TestCase):
    """sd:2171. A no-item squash drops a branch commit's `Delivers:`, so
    `sd task status N done --delivered-by` found no trailer; prepare says so."""

    extra_commit = "record the delivery\n\nDelivers: sd:2005\nAuthored-with: human"
    setUp, invoke = NoItemPublication.setUp, NoItemPublication.invoke

    def test_a_branch_commit_that_delivers_an_item_warns_to_ship_with_it(self):
        prepared = self.invoke("prepare", "--title", "Record the delivery")
        self.assertEqual(prepared["phase"], "ready_to_send")
        [warning] = [line for line in prepared["warnings"] if "Delivers:" in line]
        self.assertEqual(warning, f"commit {self.head[:12]} carries `Delivers: sd:2005`, and a no-item squash "
                                  "does not carry it; ship with --item 2005 to record the delivery")


if __name__ == "__main__":
    unittest.main()
