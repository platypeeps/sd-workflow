"""Machine-wide review slots (sd:2523): a review past the cap waits, and a dead holder frees its slot.

The holders are real processes holding real kernel locks, because the claim is
about what the kernel does when a holder dies; a double could only assert that
a release was called.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"
sys.path.insert(0, str(BIN))

import sd_lib  # noqa: E402
import sd_review_slots as slots  # noqa: E402

from tests.test_sd_review import (  # noqa: E402
    FakeRunner,
    ReviewFixture,
    namespace,
    sd_review,
)

#: A holder: take a slot, say which, then hold it until killed.
HOLDER = (
    "import os, sys, time; sys.path.insert(0, sys.argv[1]); import sd_review_slots as s; "
    "slot = s.take_review_slot(os.environ, lambda: None, stream=sys.stderr, label=sys.argv[2]); "
    "print(slot.report['slot'], flush=True); time.sleep(600)"
)


class Pool(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        self.environ = {"SD_REVIEW_SLOTS_DIR": str(self.tmp / "slots"), "HOME": str(self.tmp)}

    def hold(self, label: str, count: int) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, "-c", HOLDER, str(BIN), label], stdout=subprocess.PIPE, text=True,
                                 env={**os.environ, **self.environ, "SD_REVIEW_SLOTS": str(count)})
        self.addCleanup(child.wait)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertTrue(child.stdout.readline().strip().isdigit(), f"{label} took no slot")
        return child

    def take(self, count: int, seconds: float) -> tuple[slots.Slot | None, str]:
        said = io.StringIO()
        slot = slots.take_review_slot({**self.environ, "SD_REVIEW_SLOTS": str(count)}, lambda: None, stream=said,
                             label="the test", deadline=slots.clock() + seconds, poll=0.05)
        if slot is not None:
            self.addCleanup(slot.give_back)
        return slot, said.getvalue()

    def test_the_review_past_the_cap_waits_and_names_each_holder(self) -> None:
        first, _second = self.hold("lane-a review", 2), self.hold("lane-b review", 2)
        slot, said = self.take(2, 0.5)
        self.assertIsNone(slot, "a third review took a slot while two held both")
        self.assertIn("waiting for a review slot: 2 of 2 in use", said)
        self.assertIn("lane-a review pid", said)
        self.assertIn("lane-b review pid", said)
        self.assertEqual(said.count("\n"), 1, "one line, not one per poll")
        first.terminate()
        first.wait()
        slot, _ = self.take(2, 5)
        self.assertIsNotNone(slot, "a slot that came free was not taken")

    def test_a_killed_holder_frees_its_slot(self) -> None:
        holder = self.hold("doomed review", 1)
        self.assertIsNone(self.take(1, 0.2)[0])
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait()
        slot, said = self.take(1, 5)
        self.assertIsNotNone(slot, said)
        self.assertEqual(slot.report["slot"], 1)

    def test_zero_is_no_cap(self) -> None:
        self.hold("one", 1)
        slot, said = self.take(0, 0.2)
        self.assertEqual((slot.report["slots"], said), (0, ""))


class TheCap(unittest.TestCase):
    def test_the_config_key_overrides_the_default_and_the_variable_overrides_both(self) -> None:
        self.assertEqual(slots.review_slot_cap({}, None), (2, "default"))
        self.assertEqual(slots.review_slot_cap({}, "1"), (1, "sd.review_slots"))
        self.assertEqual(slots.review_slot_cap({"SD_REVIEW_SLOTS": "3"}, "1"), (3, "SD_REVIEW_SLOTS"))

    def test_the_key_is_read_from_the_machine_config(self) -> None:
        home = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(home)], check=False))
        environ = {"HOME": str(home)}
        path = sd_lib.machine_config_path(environ)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"config": {"sd": {"review_slots": "1"}}}), encoding="utf-8")
        slot = slots.take_review_slot({**environ, "SD_REVIEW_SLOTS_DIR": str(home / "slots")},
                             lambda: sd_lib.core_setting("review_slots", environ), stream=io.StringIO(), label="t")
        self.addCleanup(slot.give_back)
        self.assertEqual((slot.report["slots"], slot.report["source"]), (1, "sd.review_slots"))

    def test_a_bad_setting_warns_and_reviews_under_the_default(self) -> None:
        home = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(home)], check=False))
        said = io.StringIO()

        def unreadable() -> str:
            raise sd_lib.ConfigError("invalid sd.review_slots policy")

        slot = slots.take_review_slot({"SD_REVIEW_SLOTS_DIR": str(home)}, unreadable, stream=said, label="t")
        self.addCleanup(slot.give_back)
        self.assertEqual(slot.report["slots"], slots.DEFAULT_SLOTS)
        self.assertIn("warning: invalid sd.review_slots policy", said.getvalue())


class TheReviewHoldsASlot(ReviewFixture):
    """`sd-review` takes a slot before its first reviewer and gives it back before the gate."""

    def change(self) -> pathlib.Path:
        root = self.make_repo()
        (root / "src.py").write_text("x = 1\n", encoding="utf-8")
        return root

    def test_a_review_records_the_slot_it_held(self) -> None:
        report = sd_review.review(self.change(), namespace(), FakeRunner(), self.environment(), self.chatgpt_home())
        self.assertEqual(report["review_slot"]["slots"], slots.DEFAULT_SLOTS, json.dumps(report)[:2000])
        self.assertEqual(report["reviewed_by"], ["codex"])  # every tier asks one reader (sd:10193)

    def test_the_slot_is_free_once_the_review_returns(self) -> None:
        """`sd-review` holds the slot in a local of `review`, so returning frees it, as a `finally` did."""

        where = self.tmp / "review-slots"
        cap = {"SD_REVIEW_SLOTS": "1", "SD_REVIEW_SLOTS_DIR": str(where)}
        report = sd_review.review(self.change(), namespace(), FakeRunner(), self.environment(**cap),
                                  self.chatgpt_home())
        self.assertEqual(report["review_slot"]["slot"], 1, json.dumps(report)[:2000])
        after = slots.take_review_slot(cap, lambda: None, stream=io.StringIO(), label="after",
                                       deadline=slots.clock(), poll=0.01)
        self.assertIsNotNone(after, "the review kept its slot after it returned")
        after.give_back()

    def test_the_slot_is_free_while_the_gate_runs(self) -> None:
        """The reviewers run before the gate (sd:2605); the gate queues in its own pool, not in a review slot."""

        where = self.tmp / "review-slots"
        cap = {"SD_REVIEW_SLOTS": "1", "SD_REVIEW_SLOTS_DIR": str(where)}
        free: list[bool] = []

        def gate(*_args: object) -> object:
            during = slots.take_review_slot(cap, lambda: None, stream=io.StringIO(), label="during the gate",
                                            deadline=slots.clock(), poll=0.01)
            free.append(during is not None)
            if during is not None:
                during.give_back()
            return sd_review.Completed(0, "{}", "")

        report = sd_review.review(self.change(), namespace(), FakeRunner({"sd-check": gate}),
                                  self.environment(**cap), self.chatgpt_home())
        self.assertEqual(report["review_slot"]["slot"], 1, json.dumps(report)[:2000])
        self.assertEqual(free, [True], "the review held its slot while the gate ran")

    def test_the_slot_is_free_after_a_reviewer_raises_while_the_error_lives(self) -> None:
        """The `finally` gives the slot back on the raising path, not the frame's end.

        The error is kept past its `except` clause, so its traceback keeps the
        `review` frame and its locals alive: a slot freed only by `__del__`
        would still be held here.
        """

        where = self.tmp / "review-slots"
        cap = {"SD_REVIEW_SLOTS": "1", "SD_REVIEW_SLOTS_DIR": str(where)}
        kept: list[BaseException] = []

        def crash(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("the reviewer crashed")

        with mock.patch.object(sd_review, "run_provider", crash):
            try:
                sd_review.review(self.change(), namespace(), FakeRunner(), self.environment(**cap), self.chatgpt_home())
            except RuntimeError as error:
                kept.append(error)
        self.assertEqual([str(error) for error in kept], ["the reviewer crashed"])
        after = slots.take_review_slot(cap, lambda: None, stream=io.StringIO(), label="after",
                                       deadline=slots.clock(), poll=0.01)
        self.assertIsNotNone(after, "the slot stayed held after a reviewer raised")
        after.give_back()

    def test_the_slot_is_free_once_main_s_handler_catches_what_the_review_raised(self) -> None:
        """The raising path: `main` catches a Refusal `as error`, and the clause's end drops the frame and the slot.

        Each call raises a new exception, as real code does: one instance kept
        and re-raised would keep its traceback, the `review` frame and the slot.
        """

        where = self.tmp / "review-slots"
        cap = {"SD_REVIEW_SLOTS": "1", "SD_REVIEW_SLOTS_DIR": str(where)}

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise sd_review.Refusal("the review failed after it took its slot")

        with mock.patch.object(sd_review, "finish_review", refuse):
            try:
                sd_review.review(self.change(), namespace(), FakeRunner(), self.environment(**cap), self.chatgpt_home())
            except sd_review.Refusal as error:
                said = str(error)
        self.assertEqual(said, "the review failed after it took its slot")
        after = slots.take_review_slot(cap, lambda: None, stream=io.StringIO(), label="after",
                                       deadline=slots.clock(), poll=0.01)
        self.assertIsNotNone(after, "the slot stayed held after the handler that caught the review's error")
        after.give_back()

    def test_with_every_slot_held_the_review_waits_out_the_bound_and_refuses_before_any_reviewer(self) -> None:
        where = self.tmp / "review-slots"
        child = subprocess.Popen([sys.executable, "-c", HOLDER, str(BIN), "another lane"], stdout=subprocess.PIPE,
                                 text=True, env={**os.environ, "SD_REVIEW_SLOTS": "1", "SD_REVIEW_SLOTS_DIR": str(where)})
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertTrue(child.stdout.readline().strip())
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")})
        # The wait spends the setup bound from the process start; a start that
        # far past leaves one second of it, which the busy slot outlasts.
        said, started = io.StringIO(), time.monotonic()
        with (mock.patch.object(sd_lib, "STARTED", slots.clock() - sd_review.SETUP_TIMEOUT_SECONDS + 1),
              contextlib.redirect_stderr(said)):
            report = sd_review.review(self.change(), namespace(), runner,
                                      self.environment(SD_REVIEW_SLOTS="1", SD_REVIEW_SLOTS_DIR=str(where)),
                                      self.chatgpt_home())
        waited = time.monotonic() - started
        self.assertEqual(report["status"], "refused")
        self.assertIn("waiting for a review slot: 1 of 1 in use", said.getvalue())
        self.assertIn("slot 1: another lane", said.getvalue())
        busy = [row for row in report["readiness"]["blockers"] if row["code"] == "review_slot_busy"]
        self.assertEqual(len(busy), 1, json.dumps(report["readiness"])[:2000])
        self.assertIn("within the setup bound", json.dumps(busy[0]))
        self.assertGreaterEqual(waited, 0.9, "the review refused without waiting for its bound")
        self.assertLess(waited, 15, "the wait outlasted its bound")
        self.assertEqual(runner.calls, [], "a reviewer or the gate ran without a slot")


if __name__ == "__main__":
    unittest.main()
