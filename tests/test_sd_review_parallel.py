"""sd:7519. Every reviewing tier asks two readers, at once, and Claude's gets the ranked prompt.

The registry orders the readers; a tier's depth says how many must answer. The
first `depth` start together, a slot whose reader failed goes to the next one,
and the receipt keeps the registry's order whatever order the answers came in.
"""

from __future__ import annotations

import _thread
import datetime
import io
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import unittest
import urllib.request
from typing import Any

from tests.test_sd_review import FakeRunner, ReviewFixture, namespace, sd_review

#: How long a fake reader waits for its partner before it calls the run serial.
PARTNER_SECONDS = 5

THREE_READERS = """
bills:
  first:  { cost: subscription }
  second: { cost: subscription }
  third:  { cost: subscription }
  fourth: { cost: subscription }

providers:
  codex:  { start: "codex exec", vendor: openai, bill: first, roles: [reviewer], reader: codex-json, env: [] }
  second: { start: "second exec", vendor: secondvendor, bill: second, roles: [reviewer], reader: codex-json, env: [] }
  third:  { start: "third exec", vendor: thirdvendor, bill: third, roles: [reviewer], reader: codex-json, env: [] }
  fourth: { start: "fourth exec", vendor: fourthvendor, bill: fourth, roles: [reviewer], reader: codex-json, env: [] }

roles:
  author: []
  reviewer: [codex, second, third, fourth]
"""

CLAUDE_AND_CODEX = """
bills:
  first:  { cost: subscription }
  second: { cost: subscription }

providers:
  codex:  { start: "codex exec", vendor: openai, bill: first, roles: [reviewer], reader: codex-json, env: [] }
  claude: { start: "claude exec", vendor: anthropic, bill: second, roles: [reviewer], reader: claude-json, env: [] }

roles:
  author: []
  reviewer: [codex, claude]
"""

CLEAN = sd_review.Completed(0, '{"findings": []}', "")
FAILED = sd_review.Completed(1, "", "runtime failed")
CLAUDE_CLEAN = sd_review.Completed(0, json.dumps({"type": "result", "subtype": "success",
                                                  "structured_output": {"findings": []}}), "")


def review_session(argv: Any) -> bool:
    """A review session, not the codex skill probe or its version read."""
    return list(argv[1:3]) != ["debug", "prompt-input"] and list(argv[1:]) != ["--version"]


class ParallelFixture(ReviewFixture):
    def prepare(self, registry: str | None = None, consent: str | None = None) -> pathlib.Path:
        if registry is not None:
            (self.registry_home / ".local/share/sd/providers.yaml").write_text(registry, encoding="utf-8")
        root = self.make_repo()
        if consent is not None:
            (root / "CLAUDE.local.md").write_text(
                "<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\n"
                f"{sd_review.sd_lib.CONSENT_KEY}: {consent}\n"
                "<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n", encoding="utf-8")
        (root / "src.py").write_text("the_review_subject = 123\n", encoding="utf-8")
        self.stub_policy({"default_tier": "standard"})
        return root

    def review(self, root: pathlib.Path, runner: Any, **options: Any) -> dict[str, Any]:
        return sd_review.review(root, namespace(**options), runner, self.environment(), self.chatgpt_home())


class TwoReadersAtOnce(ParallelFixture):
    def test_every_reviewing_tier_asks_two_readers(self) -> None:
        for tier in ("cheap", "standard", "deep"):
            with self.subTest(tier=tier):
                self.assertEqual(sd_review.sd_route.TIER_DEPTH[tier], 2)
        self.assertEqual(sd_review.sd_route.TIER_DEPTH["skip"], 0)

    def test_a_lensed_brief_that_routes_to_skip_still_asks_two_readers(self) -> None:
        root = self.prepare()
        (root / "src.py").unlink()
        (root / "brief.md").write_text("# A brief\n", encoding="utf-8")
        for options in ({"lens": "research-brief"}, {"challenge": True}):
            with self.subTest(options=options):
                planned = self.review(root, FakeRunner(), explain=True, **options)
                self.assertEqual(planned["route"]["tier"], "skip")
                self.assertEqual(planned["requested_reviews"], 2)
                self.assertEqual(planned["providers"], ["codex", "second"])

    def test_the_two_readers_run_at_the_same_time(self) -> None:
        root = self.prepare()
        barrier = threading.Barrier(2, timeout=PARTNER_SECONDS)

        def meets_its_partner(argv: Any, env: Any, cwd: Any, timeout: int) -> Any:
            if not review_session(argv):
                return sd_review.Completed(0, "[]", "")
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                return sd_review.Completed(1, "", "the other reader never ran beside this one")
            return CLEAN

        runner = FakeRunner({"codex": meets_its_partner, "second": meets_its_partner})
        result = self.review(root, runner)
        self.assertEqual((result["requested_reviews"], result["completed_reviews"]), (2, 2), result["outcomes"])
        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["reviewed_by"], ["codex", "second"])

    def test_a_failed_slot_goes_to_the_next_reader_while_the_other_still_runs(self) -> None:
        root = self.prepare(THREE_READERS, "codex@codex, second@second, third@third, fourth@fourth")
        third_started = threading.Event()

        def waits_for_third(argv: Any, env: Any, cwd: Any, timeout: int) -> Any:
            return CLEAN if third_started.wait(PARTNER_SECONDS) else sd_review.Completed(1, "", "third never ran beside second")

        def third(argv: Any, env: Any, cwd: Any, timeout: int) -> Any:
            third_started.set()
            return CLEAN

        def codex(argv: Any, env: Any, cwd: Any, timeout: int) -> Any:
            return FAILED if review_session(argv) else sd_review.Completed(0, "[]", "")

        runner = FakeRunner({"codex": codex, "second": waits_for_third, "third": third})
        result = self.review(root, runner)
        self.assertEqual(result["status"], "clean", result["outcomes"])
        self.assertEqual(result["reviewed_by"], ["second", "third"])
        self.assertEqual([row["backend"] for row in result["outcomes"]], ["codex", "second", "third"])
        self.assertEqual(result["remaining"], [])
        self.assertNotIn("fourth", [pathlib.Path(call["argv"][0]).name for call in runner.calls])

    def test_outcomes_keep_the_registry_order_whatever_finishes_first(self) -> None:
        root = self.prepare()
        second_answered = threading.Event()

        def codex(argv: Any, env: Any, cwd: Any, timeout: int) -> Any:
            if review_session(argv):
                second_answered.wait(PARTNER_SECONDS)
                time.sleep(0.05)
            return CLEAN if review_session(argv) else sd_review.Completed(0, "[]", "")

        def second(argv: Any, env: Any, cwd: Any, timeout: int) -> Any:
            second_answered.set()
            return CLEAN

        result = self.review(root, FakeRunner({"codex": codex, "second": second}))
        self.assertEqual([row["backend"] for row in result["outcomes"]], ["codex", "second"])
        self.assertEqual(result["reviewed_by"], ["codex", "second"])

    def test_a_finding_both_readers_report_is_listed_once(self) -> None:
        """Two readers, one finding: adjudication takes a row per finding, so a copy is a second row to dispute."""
        root = self.prepare()

        def answer(*summaries: str) -> sd_review.Completed:
            return sd_review.Completed(0, json.dumps({"findings": [
                {"path": "src.py", "line": 1, "severity": "high", "family": "correctness", "summary": text}
                for text in summaries]}), "")

        def codex(argv: Any, env: Any, cwd: Any, timeout: int) -> Any:
            return answer("the value is wrong") if review_session(argv) else sd_review.Completed(0, "[]", "")

        def second(argv: Any, env: Any, cwd: Any, timeout: int) -> Any:
            return answer("the value is wrong", "the name is wrong")

        result = self.review(root, FakeRunner({"codex": codex, "second": second}))
        self.assertEqual(result["reviewed_by"], ["codex", "second"])
        self.assertEqual([(row["summary"], row["backend"]) for row in result["findings"]],
                         [("the value is wrong", "codex"), ("the name is wrong", "second")])

    def test_two_failures_leave_the_review_incomplete_and_name_both(self) -> None:
        root = self.prepare()

        def fails(argv: Any, env: Any, cwd: Any, timeout: int) -> Any:
            return FAILED if review_session(argv) else sd_review.Completed(0, "[]", "")

        result = self.review(root, FakeRunner({"codex": fails, "second": fails}))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual((result["requested_reviews"], result["completed_reviews"]), (2, 0))
        self.assertEqual(result["remaining"], ["codex", "second"])


class AShortChainSaysSo(ParallelFixture):
    """#43332: depth is min(2, eligible readers), and the result names the shortfall."""

    def test_one_eligible_reader_still_reviews_and_names_the_shortfall(self) -> None:
        root = self.prepare(THREE_READERS, "codex@codex")
        result = self.review(root, FakeRunner())
        self.assertEqual((result["requested_reviews"], result["completed_reviews"]), (1, 1), result.get("outcomes"))
        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["review_shortfall"], {"requested": 2, "eligible": 1})
        stream = io.StringIO()
        sd_review.render(result, stream)
        self.assertIn("requested 2, eligible 1", stream.getvalue())

    def test_two_eligible_readers_name_no_shortfall(self) -> None:
        result = self.review(self.prepare(), FakeRunner())
        self.assertIsNone(result["review_shortfall"])
        stream = io.StringIO()
        sd_review.render(result, stream)
        self.assertNotIn("eligible", stream.getvalue())


class ClaudeGetsTheRankedPrompt(ParallelFixture):
    def test_only_the_claude_reader_is_asked_to_trace_and_rank(self) -> None:
        root = self.prepare(CLAUDE_AND_CODEX, "codex@codex, claude@claude")
        read: list[str] = []

        def claude(argv: Any, env: Any, cwd: Any, timeout: int) -> Any:
            subject = pathlib.Path(argv[-1].split(" ", 2)[1])
            read.append(subject.read_text(encoding="utf-8"))
            return CLAUDE_CLEAN

        runner = FakeRunner({"claude": claude})
        for name in ("claude", "codex"):
            result = self.review(root, runner, provider=name)
            self.assertEqual(result["reviewed_by"], [name], result["outcomes"])
        sessions = [call for call in runner.calls if call["argv"][0] == "codex" and review_session(call["argv"])]
        self.assertEqual(len(read), 1)
        self.assertEqual(len(sessions), 1)
        for phrase in ("trace each changed function to its callers", "Rank findings by likely production impact"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, read[0])
                self.assertNotIn(phrase, sessions[0]["stdin"])



class EachReaderReportsItsTimeAndCost(ParallelFixture):
    """pack sd:8503: a rule such as "drop the Claude reader past 5 min or $3"
    needs each reader's wall time and cost in its own outcome."""

    def test_each_outcome_carries_its_wall_time_and_the_claude_cost(self) -> None:
        root = self.prepare(CLAUDE_AND_CODEX, "codex@codex, claude@claude")
        envelope = {"type": "result", "subtype": "success", "structured_output": {"findings": []},
                    "total_cost_usd": 1.25, "usage": {"input_tokens": 10, "cache_creation_input_tokens": 200,
                                                     "cache_read_input_tokens": 3000, "output_tokens": 40}}

        def claude(argv: Any, env: Any, cwd: Any, timeout: int) -> Any:
            time.sleep(0.2)
            return sd_review.Completed(0, json.dumps(envelope), "")

        result = self.review(root, FakeRunner({"claude": claude}))
        self.assertEqual(result["reviewed_by"], ["codex", "claude"], result["outcomes"])
        rows = {row["backend"]: row for row in result["outcomes"]}
        for name, row in rows.items():
            with self.subTest(reader=name):
                started = datetime.datetime.fromisoformat(row["started"])
                finished = datetime.datetime.fromisoformat(row["finished"])
                self.assertIsNotNone(started.tzinfo)
                self.assertLessEqual(started, finished)
                self.assertGreaterEqual(row["wall_seconds"], 0)
        self.assertGreaterEqual(rows["claude"]["wall_seconds"], 0.2)
        self.assertEqual(rows["claude"]["cost"], {"usd": 1.25, "input_tokens": 3210, "output_tokens": 40})
        self.assertIsNone(rows["codex"]["cost"])

    def test_a_claude_envelope_without_cost_reports_none(self) -> None:
        odd = ('{"total_cost_usd": NaN}', '{"total_cost_usd": Infinity}', '{"total_cost_usd": -1}',
               '{"total_cost_usd": true}', '{"total_cost_usd": 1e400}', '{"total_cost_usd": null}')
        for stdout in ("", "not json", json.dumps({"type": "result", "total_cost_usd": "1"}), json.dumps([1]), *odd):
            with self.subTest(stdout=stdout):
                self.assertIsNone(sd_review.reader_cost("claude-json", stdout))
        self.assertIsNone(sd_review.reader_cost("codex-json", json.dumps({"total_cost_usd": 1.0})))
        self.assertEqual(sd_review.reader_cost("claude-json", json.dumps({"total_cost_usd": 0, "usage": {
            "input_tokens": 10**13, "output_tokens": -1}})), {"usd": 0, "input_tokens": None, "output_tokens": None})


DRIVER = textwrap.dedent("""
    import pathlib, sys
    sys.path.insert(0, sys.argv[1])
    from tests.test_sd_review import sd_review
    out = pathlib.Path(sys.argv[2])
    child = "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(120)"
    def run_one(name):
        return sd_review.subprocess_runner([sys.executable, "-c", child, str(out / name)], {}, out, 300)
    sd_review.sd_lib.fill_slots(["a", "b"], 2, run_one, lambda answer: True, sd_review.READER_GROUPS)
""")

#: A url reader on a server that accepts and never answers, beside a process reader.
STALLED_DRIVER = textwrap.dedent("""
    import pathlib, socket, sys, urllib.request
    sys.path.insert(0, sys.argv[1])
    from tests.test_sd_review import sd_review
    out = pathlib.Path(sys.argv[2])
    stalled = socket.socket()
    stalled.bind(("127.0.0.1", 0))
    stalled.listen(4)
    child = "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(120)"
    def run_one(name):
        if name == "url":
            (out / "url").write_text("asked")
            return urllib.request.urlopen(f"http://127.0.0.1:{stalled.getsockname()[1]}/", timeout=120)
        return sd_review.subprocess_runner([sys.executable, "-c", child, str(out / name)], {}, out, 300)
    sd_review.sd_lib.fill_slots(["url", "a"], 2, run_one, lambda answer: True, sd_review.READER_GROUPS)
""")

#: Well under the phase timeout the stalled reads above wait for (120 s).
PROMPT_EXIT_SECONDS = 15


class ATermEndsBothReaders(ParallelFixture):
    def test_a_sigterm_to_the_review_ends_every_reader_group(self) -> None:
        """Only the main thread hears SIGTERM (`sd_lib.term_ends_group`); the readers run in threads."""
        out = self.tmp / "groups"
        out.mkdir()
        driver = subprocess.Popen([sys.executable, "-c", DRIVER, str(pathlib.Path(__file__).resolve().parents[1]), str(out)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(driver.kill)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not all((out / name).is_file() and (out / name).read_text() for name in "ab"):
            time.sleep(0.05)
        pids = [int((out / name).read_text()) for name in "ab"]
        for pid in pids:
            self.addCleanup(lambda pid=pid: _quietly_kill(pid))
        driver.send_signal(signal.SIGTERM)
        _out, errors = driver.communicate(timeout=30)
        self.assertEqual(driver.returncode, -signal.SIGTERM, errors)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and any(_alive(pid) for pid in pids):
            time.sleep(0.05)
        self.assertEqual([pid for pid in pids if _alive(pid)], [])

    def test_a_sigterm_beside_a_stalled_url_read_ends_promptly(self) -> None:
        """A url reader leads no group: the review must not wait out its read."""
        out = self.tmp / "stalled"
        out.mkdir()
        driver = subprocess.Popen([sys.executable, "-c", STALLED_DRIVER, str(pathlib.Path(__file__).resolve().parents[1]), str(out)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(driver.kill)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not all((out / name).is_file() and (out / name).read_text() for name in ("url", "a")):
            time.sleep(0.05)
        pid = int((out / "a").read_text())
        self.addCleanup(_quietly_kill, pid)
        sent = time.monotonic()
        driver.send_signal(signal.SIGTERM)
        try:
            _out, errors = driver.communicate(timeout=PROMPT_EXIT_SECONDS)
        except subprocess.TimeoutExpired:
            self.fail(f"sd-review still running {PROMPT_EXIT_SECONDS}s after SIGTERM: it waits on the stalled url read")
        self.assertEqual(driver.returncode, -signal.SIGTERM, errors)
        self.assertLess(time.monotonic() - sent, PROMPT_EXIT_SECONDS)
        self.assertFalse(_alive(pid), "the process reader's group outlived the review")


class AnyExitEndsTheReaders(unittest.TestCase):
    """Round 1 class: a reader group outlives the review on a non-normal exit of `fill_slots`.

    Each test starts a real `sleep` group in a reader thread, ends the dispatch
    another way than SIGTERM, and asks that the group and its thread are gone
    when `fill_slots` returns: `review()`'s `finally` gives the gate slot back then.
    """

    def setUp(self) -> None:
        self.groups = sd_review.sd_lib.ThreadGroups()
        self.pids: list[int] = []
        self.started = threading.Event()

    def sleeper(self, seconds: float = 60, timeout: float = 60) -> Any:
        hook = self.groups.group_hook()

        def heard(pid: int | None) -> None:
            if pid is not None:
                self.pids.append(pid)
                self.addCleanup(_quietly_kill, pid)
                self.started.set()
            hook(pid)
        return sd_review.sd_lib.run_group(["sleep", str(seconds)], cwd=pathlib.Path.cwd(), env=dict(os.environ),
                                          timeout=timeout, started=heard)

    def assert_nothing_left(self) -> None:
        self.assertTrue(self.pids, "no reader group started")
        self.assertEqual([pid for pid in self.pids if _alive(pid)], [], "a reader group outlived fill_slots")
        self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith("sd-slot")], [])

    def test_a_ctrl_c_ends_every_reader_group_before_fill_slots_returns(self) -> None:
        def interrupt_once_started() -> None:
            if self.started.wait(10):
                _thread.interrupt_main()
        threading.Thread(target=interrupt_once_started, daemon=True).start()
        with self.assertRaises(KeyboardInterrupt):
            sd_review.sd_lib.fill_slots(["a", "b"], 2, lambda _item: self.sleeper(), lambda _answer: True, self.groups)
        self.assert_nothing_left()

    def test_a_reader_that_raises_ends_the_other_readers_group(self) -> None:
        def run_one(item: str) -> Any:
            if item == "b":
                return self.sleeper()
            self.started.wait(10)
            raise RuntimeError("reader a failed outside its group")
        with self.assertRaisesRegex(RuntimeError, "reader a failed"):
            sd_review.sd_lib.fill_slots(["a", "b"], 2, run_one, lambda _answer: True, self.groups)
        self.assert_nothing_left()

    def test_a_counting_rule_that_raises_ends_the_other_readers_group(self) -> None:
        def run_one(item: str) -> Any:
            return self.sleeper() if item == "b" else self.started.wait(10)
        def counted(answer: Any) -> bool:
            raise ValueError("unreadable answer")
        with self.assertRaisesRegex(ValueError, "unreadable answer"):
            sd_review.sd_lib.fill_slots(["a", "b"], 2, run_one, counted, self.groups)
        self.assert_nothing_left()

    def test_after_an_interrupted_dispatch_the_next_one_runs_its_readers(self) -> None:
        self.test_a_reader_that_raises_ends_the_other_readers_group()
        answers = sd_review.sd_lib.fill_slots(
            ["a"], 1, lambda _item: self.sleeper(seconds=0.2).returncode, lambda _answer: True, self.groups)
        self.assertEqual(answers, [0], "a later dispatch's group was ended as it started")

    def test_a_ctrl_c_beside_a_stalled_url_read_returns_promptly(self) -> None:
        stalled = socket.socket()
        self.addCleanup(stalled.close)
        stalled.bind(("127.0.0.1", 0))
        stalled.listen(4)
        url_asked = threading.Event()

        def run_one(item: str) -> Any:
            if item == "url":
                url_asked.set()
                return urllib.request.urlopen(f"http://127.0.0.1:{stalled.getsockname()[1]}/", timeout=120)
            return self.sleeper()

        def interrupt_once_both_run() -> None:
            if self.started.wait(10) and url_asked.wait(10):
                _thread.interrupt_main()
        threading.Thread(target=interrupt_once_both_run, daemon=True).start()
        began = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            sd_review.sd_lib.fill_slots(["url", "a"], 2, run_one, lambda _answer: True, self.groups)
        self.assertLess(time.monotonic() - began, PROMPT_EXIT_SECONDS, "Ctrl-C waited on the stalled url read")
        self.assertEqual([pid for pid in self.pids if _alive(pid)], [], "a reader group outlived fill_slots")
        left = [t for t in threading.enumerate() if t.name.startswith("sd-slot")]
        self.assertTrue(all(t.daemon for t in left), "an abandoned url reader would hold the interpreter's exit")

    def test_a_reader_past_its_phase_timeout_ends_its_own_group(self) -> None:
        def run_one(_item: str) -> str:
            try:
                self.sleeper(timeout=0.5)
            except sd_review.sd_lib.GroupTimeout:
                return "timed out"
            return "finished"
        self.assertEqual(sd_review.sd_lib.fill_slots(["a"], 1, run_one, lambda _answer: False, self.groups), ["timed out"])
        self.assert_nothing_left()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _quietly_kill(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
