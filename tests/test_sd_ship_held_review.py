"""sd:3340. A completed review waits on disk until the hub records it.

`prepare` ran a review of 15 to 40 minutes and held its result only in
memory until the hub save. A hub that was down at that save, or that came
back on a newer build, lost the review, and the retry repeated a full review
and gate under `--retry-review`.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import importlib
import io
import json
import pathlib
import tempfile
import unittest
import unittest.mock

from tests import test_sd_ship_gate_failure as gate_tests
from tests import test_sd_ship_provider as provider_tests

ship = provider_tests.ship
# After the fixture import, which puts `bin/` on the path.
sd_ship_review = importlib.import_module("sd_ship_review")
HEAD = provider_tests.HEAD
CAP = provider_tests.CAP


class HubDown(ConnectionError):
    pass


class Hub:
    """The ship store: keeps the last state it saved, and refuses saves while down."""

    def __init__(self, state=None):
        self.state, self.revision, self.down, self.drop_reply = copy.deepcopy(state or {"passes": []}), 1, False, False

    def save(self, _connection, _key, previous, value):
        if self.down:
            raise HubDown("hub unavailable")
        self.state, self.revision = copy.deepcopy(value), previous + 1
        if self.drop_reply:
            raise HubDown("reply lost after the hub stored the save")
        return self.revision


class HeldReview(unittest.TestCase):
    context = provider_tests.ProviderSelection.context

    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="sd3340-")
        self.addCleanup(scratch.cleanup)
        self.folder = pathlib.Path(scratch.name) / "review"
        self.hub = Hub()

    def prepare(self, *, key="key", head=HEAD, binding="binding", report_changes=None, args=None, manifest=None):
        """One prepare's review against `self.hub`, with the held folder under the test's own folder."""
        review, process = self.context(state=self.hub.state, head=head, report_changes=report_changes)
        review.runtime = dataclasses.replace(review.runtime, held=lambda _root: self.folder,
                                             binding=lambda _root: binding)
        if manifest is not None:
            review.runtime = dataclasses.replace(review.runtime, manifest=manifest)
        review.store, review.key, review.revision = self.hub, key, self.hub.revision
        for name, value in (args or {}).items():
            setattr(review.args, name, value)
        return review, process

    def run_review(self, review):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            review.review(review.runtime.current_head(review.root))
        return stderr.getvalue()

    def held_files(self):
        return sorted(path.name for path in self.folder.iterdir()) if self.folder.is_dir() else []

    def review_done_then_hub_down(self, **kwargs):
        """Review at HEAD with the hub going down after the reservation: the recording save fails."""
        review, process = self.prepare(**kwargs)
        reserve = self.hub.save

        def save(*args):
            revision = reserve(*args)
            if args[3].get("phase") == "reviewing":
                self.hub.down = True
            return revision
        review.store = unittest.mock.Mock(save=unittest.mock.Mock(side_effect=save))
        with self.assertRaises(HubDown), contextlib.redirect_stderr(io.StringIO()):
            review.review(HEAD)
        self.hub.down = False
        self.assertEqual(process.call_count, 2)
        return review

    def test_review_done_then_hub_down_records_the_held_review_with_no_provider(self):
        self.review_done_then_hub_down()
        self.assertEqual(len(self.held_files()), 1)
        self.assertNotIn("report", self.hub.state["passes"][-1])
        review, process = self.prepare()
        said = self.run_review(review)
        self.assertEqual(process.call_count, 0, said)
        self.assertIn("no provider called", said)
        self.assertEqual(len(self.hub.state["passes"]), 1)
        self.assertEqual(self.hub.state["passes"][-1]["report"]["status"], "clean")
        self.assertEqual(self.hub.state["reviewed_head"], HEAD)
        self.assertEqual(self.held_files(), [])

    def test_a_held_review_of_a_moved_head_resumes_under_the_head_it_covers(self):
        """sd:3379: the resumed review records the head sd-review reviewed, not the one prepare had read."""
        moved = "e" * 40
        changes = {"subject": {"head": moved, "base": provider_tests.BASE}}
        self.review_done_then_hub_down(report_changes=changes)
        review, process = self.prepare(report_changes=changes)
        with self.assertRaises(ship.Refusal) as caught:
            self.run_review(review)
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "review_head_moved")
        self.assertEqual(process.call_count, 0)
        stored = self.hub.state["passes"][-1]
        self.assertEqual((stored["head"], stored["reserved_head"], self.hub.state["reviewed_head"]), (moved, HEAD, moved))
        self.assertEqual(self.held_files(), [])

    def test_the_held_review_resumes_under_retry_review_too(self):
        """The lane's one automatic retry passes --retry-review; the held review still spends no pass."""
        self.review_done_then_hub_down()
        review, process = self.prepare(args={"retry_review": True})
        self.run_review(review)
        self.assertEqual(process.call_count, 0)
        self.assertEqual(len(self.hub.state["passes"]), 1)

    def test_a_hub_save_removes_the_held_review(self):
        review, process = self.prepare()
        self.run_review(review)
        self.assertEqual(process.call_count, 2)
        self.assertEqual(self.hub.state["reviewed_head"], HEAD)
        self.assertEqual(self.held_files(), [])

    def test_a_save_the_hub_stored_but_never_answered_is_set_aside(self):
        review, _process = self.prepare()
        reserve = self.hub.save

        def save(*args):
            revision = reserve(*args)
            if args[3].get("phase") == "reviewing":
                self.hub.drop_reply = True
            return revision
        review.store = unittest.mock.Mock(save=unittest.mock.Mock(side_effect=save))
        with self.assertRaises(HubDown), contextlib.redirect_stderr(io.StringIO()):
            review.review(HEAD)
        self.hub.drop_reply = False
        self.assertEqual(len(self.held_files()), 1)
        self.assertEqual(self.hub.state["passes"][-1]["report"]["status"], "clean")
        review, process = self.prepare()
        said = self.run_review(review)
        self.assertIn("bound to another reservation", said)
        self.assertEqual(process.call_count, 0, "the hub's own receipt is reused at its head")
        self.assertEqual(self.held_files(), [])

    def test_killed_between_the_file_write_and_the_save_resumes(self):
        review, _process = self.prepare()
        with unittest.mock.patch.object(sd_ship_review.SharedReview, "record_review", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt), contextlib.redirect_stderr(io.StringIO()):
            review.review(HEAD)
        self.assertEqual(len(self.held_files()), 1)
        review, process = self.prepare()
        self.run_review(review)
        self.assertEqual(process.call_count, 0)
        self.assertEqual(self.hub.state["reviewed_head"], HEAD)

    def test_killed_during_the_file_write_leaves_a_part_file_nothing_reads(self):
        self.review_done_then_hub_down()
        [name] = self.held_files()
        held = self.folder / name
        held.rename(held.with_name(name + ".part"))
        review, _process = self.prepare()
        with self.assertRaisesRegex(ship.Refusal, "--retry-review"), contextlib.redirect_stderr(io.StringIO()):
            review.review(HEAD)
        self.assertEqual(self.held_files(), [])

    def test_a_held_review_for_an_old_head_is_set_aside(self):
        self.review_done_then_hub_down()
        review, process = self.prepare(head="c" * 40)
        with self.assertRaises(ship.Refusal), contextlib.redirect_stderr(io.StringIO()) as said:
            review.review("c" * 40)
        self.assertIn("bound to another head", said.getvalue())
        self.assertEqual(process.call_count, 0)
        self.assertEqual(self.held_files(), [])

    def test_a_corrupted_held_review_is_set_aside(self):
        for damage in (lambda text: text[: len(text) // 2], lambda text: text.replace('\\"clean\\"', '\\"blocking\\"')):
            with self.subTest(damage=damage):
                self.hub = Hub()
                self.review_done_then_hub_down()
                [name] = self.held_files()
                held = self.folder / name
                changed = damage(held.read_text(encoding="utf-8"))
                self.assertNotEqual(changed, held.read_text(encoding="utf-8"))
                held.write_text(changed, encoding="utf-8")
                review, _process = self.prepare()
                with self.assertRaisesRegex(ship.Refusal, "--retry-review"), \
                        contextlib.redirect_stderr(io.StringIO()) as said:
                    review.review(HEAD)
                self.assertIn("set aside the held review", said.getvalue())
                self.assertEqual(self.held_files(), [])

    def test_moved_review_tools_set_the_held_review_aside(self):
        self.review_done_then_hub_down()
        review, _process = self.prepare(binding="moved")
        review.runtime = dataclasses.replace(review.runtime, process=unittest.mock.Mock(side_effect=AssertionError))
        # A moved binding re-reviews in full; the provider call is where this test stops it.
        with unittest.mock.patch.object(sd_ship_review, "is_ancestor", return_value=True), \
                self.assertRaises(AssertionError), contextlib.redirect_stderr(io.StringIO()) as said:
            review.review(HEAD)
        self.assertIn("review tools or policy changed", said.getvalue())
        self.assertEqual(self.held_files(), [])

    def test_two_items_hold_two_files_and_neither_reads_the_other(self):
        self.review_done_then_hub_down(key="ship:item-1")
        first = self.held_files()
        self.hub = Hub()
        self.review_done_then_hub_down(key="ship:item-2")
        self.assertEqual(len(self.held_files()), 2)
        review, process = self.prepare(key="ship:item-2")
        self.run_review(review)
        self.assertEqual(process.call_count, 0)
        self.assertEqual(self.held_files(), first)

    def test_an_unwritable_folder_holds_nothing_and_the_save_goes_ahead(self):
        self.folder.write_text("not a folder", encoding="utf-8")
        review, _process = self.prepare()
        review.runtime = dataclasses.replace(review.runtime, held=lambda _root: self.folder / "review")
        said = self.run_review(review)
        self.assertIn("was not held on disk", said)
        self.assertEqual(self.hub.state["reviewed_head"], HEAD)

    def test_a_held_gate_failure_releases_its_pass_and_restores_the_dispatch_fields(self):
        before = {"passes": [], "phase": "new", "binding": "old-binding", "binding_manifest": {"tools": "old"},
                  "review_clearance": {"cleared": "old"}}
        self.hub = Hub(before)
        review, _process = self.prepare(report_changes=gate_tests.GATE_FAILED)
        review.runtime = dataclasses.replace(review.runtime, manifest=lambda _root: {"tools": "new"})
        reserve = self.hub.save

        def save(*args):
            revision = reserve(*args)
            self.hub.down = args[3].get("phase") == "reviewing"
            return revision
        review.store = unittest.mock.Mock(save=unittest.mock.Mock(side_effect=save))
        with self.assertRaises(HubDown), contextlib.redirect_stderr(io.StringIO()):
            review.review(HEAD)
        self.hub.down = False
        self.assertEqual({key: self.hub.state.get(key) for key in sd_ship_review.DISPATCH_FIELDS},
                         {"phase": "reviewing", "head": HEAD, "binding": "binding",
                          "binding_manifest": {"tools": "new"}, "review_clearance": None})
        review, process = self.prepare(report_changes=gate_tests.GATE_FAILED)
        review.runtime = dataclasses.replace(review.runtime, manifest=lambda _root: {"tools": "new"})
        with self.assertRaisesRegex(ship.Refusal, "no review pass was spent"), contextlib.redirect_stderr(io.StringIO()):
            review.review(HEAD)
        self.assertEqual(process.call_count, 0)
        self.assertEqual(self.hub.state["passes"], [])
        self.assertEqual({key: self.hub.state.get(key, "absent") for key in sd_ship_review.DISPATCH_FIELDS},
                         {"phase": "new", "head": "absent", "binding": "old-binding",
                          "binding_manifest": {"tools": "old"}, "review_clearance": {"cleared": "old"}})
        self.assertEqual(self.held_files(), [])

    def test_a_tool_move_resumes_the_held_review_and_a_stop_after_one_save_loses_nothing(self):
        """Lead review of sd:10928: the resume rebinds no receipt before it records the held result."""
        def manifest(verdict):
            return lambda _root: {"schema": 2, "normalizer": "n", "verdict": {"sd-review": verdict},
                                  "gate": {}, "check": {}, "policy": {"local": "p"}}

        self.review_done_then_hub_down(binding="binding", manifest=manifest("before"))
        review, _process = self.prepare(binding="moved", manifest=manifest("after"))
        saves = []
        reserve = self.hub.save

        def save(*args):
            saves.append(args[3])
            if len(saves) > 1:
                raise HubDown("stopped after one save")
            return reserve(*args)
        review.store = unittest.mock.Mock(save=unittest.mock.Mock(side_effect=save))
        with contextlib.suppress(HubDown), contextlib.redirect_stderr(io.StringIO()):
            review.review(HEAD)
        review, process = self.prepare(binding="moved", manifest=manifest("after"))
        self.run_review(review)
        self.assertEqual(process.call_count, 0, "the completed review was set aside and run again")
        self.assertEqual(self.hub.state["passes"][-1]["report"]["status"], "clean")

    def test_a_named_provider_is_checked_against_the_resumed_review(self):
        """Codex review of sd:3340: a resume returned before `--provider` was compared with the receipt."""
        self.review_done_then_hub_down()
        review, process = self.prepare(args={"provider": "minimax"})
        with self.assertRaises(ship.Refusal) as caught, contextlib.redirect_stderr(io.StringIO()):
            review.review(HEAD)
        self.assertEqual(caught.exception.workflow["blocker"]["code"], "review_provider_mismatch")
        process.assert_not_called()
        self.assertEqual(self.hub.state["passes"][-1]["report"]["reviewed_by"], ["automatic"])
        self.assertEqual(self.held_files(), [])

    def test_an_additional_request_is_dispatched_after_the_resumed_review(self):
        """Codex review of sd:3340: a resume returned before an additional request was dispatched."""
        incomplete = {"status": "unavailable", "completed_reviews": 0, "reviewed_by": []}
        with unittest.mock.patch.object(sd_ship_review, "is_ancestor", return_value=True):
            for index in range(CAP - 1):
                spent, _process = self.prepare(report_changes=incomplete, args={"retry_review": index > 0})
                with self.assertRaises(ship.Refusal), contextlib.redirect_stderr(io.StringIO()):
                    spent.review(HEAD)
            self.review_done_then_hub_down(args={"retry_review": True})
            self.assertEqual(len(self.hub.state["passes"]), CAP)
            review, process = self.prepare(args={"additional_review_for": HEAD,
                                                 "request_reason": "Synthetic explicit additional request"})
            self.run_review(review)
        self.assertEqual(process.call_count, 2)
        passes = self.hub.state["passes"]
        self.assertEqual(len(passes), CAP + 1)
        self.assertEqual(passes[CAP - 1]["report"]["status"], "clean")
        self.assertEqual(passes[-1]["additional_review_request"]["head"], HEAD)
        self.assertEqual(self.held_files(), [])

    def test_the_held_file_is_owner_only_and_bound_to_its_record(self):
        self.review_done_then_hub_down()
        [name] = self.held_files()
        held = self.folder / name
        self.assertEqual(held.stat().st_mode & 0o777, 0o600)
        record = json.loads(held.read_text(encoding="utf-8"))
        self.assertEqual((record["binding"]["head"], record["binding"]["key"]), (HEAD, "key"))


class HeldFolder(unittest.TestCase):
    def test_sd_ship_holds_reviews_in_the_repository_lane_folder(self):
        """The existing per-repository lane folder, shared by every worktree; no new root."""
        sd_lane = importlib.import_module("sd_lane")
        with tempfile.TemporaryDirectory() as root, unittest.mock.patch.dict("os.environ", {"SD_LANE_ROOT": root}):
            self.assertEqual(ship.review_runtime().held(provider_tests.fixture.ROOT),
                             sd_lane.lane_dir(provider_tests.fixture.ROOT, {"SD_LANE_ROOT": root}) / "review")


if __name__ == "__main__":
    unittest.main()
