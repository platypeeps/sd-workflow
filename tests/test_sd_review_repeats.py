"""pack sd:9270. Reviewers stop repeating settled findings; a repeat of a rebuttal cites evidence.

Evidence, 2026-11-17: on system sd:9230 an out-of-threat-model finding came
back in review round 5 and again in the lane; on a system build branch a
blocker claimed `ls-tree` reads an id as a glob, and git 2.54 matches it
literally. So the prompt carries the repository's threat model and this
branch's rebutted findings. Operator ruling: a first-time blocker blocks,
cited or not; only an uncited repeat of a rebutted finding posts as advice.

Every `CLAUDE.local.md` here is synthetic, and the adjudication rows go into a
fixture database under a scratch `HOME`; the operator's files are never read.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import unittest
import unittest.mock
from datetime import timedelta
from typing import Any

from tests.clean_env import clean_environment
from tests.test_sd_review import FakeRunner, ReviewFixture, namespace, sd_review
from tests.test_sd_review_blocker_checklist import ANCHOR, REPOSITORY

THREAT = "Synthetic model: one user on one laptop. Out of scope: a hostile local user."
BRANCH = "feature"


class RepeatFixture(ReviewFixture):
    def setUp(self) -> None:
        super().setUp()
        self.library = sd_review.sd_lib.import_sd_db().module
        with unittest.mock.patch.dict(os.environ, clean_environment(), clear=True):
            self.root = self.make_repo()
        subprocess.run(["git", "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git"],
                       cwd=str(self.root), check=True, capture_output=True, env=clean_environment())
        # main's tip is the base; `feature` holds two commits, `first` and the reviewed head.
        self.base, _, self.first, self.head = (self.commit(*args) for args in (
            ("base",), ("checkout", "--quiet", "-b", BRANCH), ("first",), ("anchor",)))
        (self.root / "src.py").write_text("x = 1\n", encoding="utf-8")

    def commit(self, *args: str) -> str:
        """A commit dated `ANCHOR` named `args[0]`, or the git command `args`; HEAD after it."""
        stamp = ANCHOR.isoformat()
        command = list(args) if len(args) > 1 else ["commit", "--quiet", "--allow-empty", "-m", args[0]]
        subprocess.run(["git", *command], cwd=str(self.root), check=True, capture_output=True,
                       env=clean_environment(GIT_COMMITTER_DATE=stamp, GIT_AUTHOR_DATE=stamp))
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.root), check=True, capture_output=True,
                              text=True, env=clean_environment()).stdout.strip()

    def database(self) -> pathlib.Path:
        path = self.library.default_path(self.registry_home)
        if not path.exists():
            self.library.initialise(home=self.registry_home)
        return path

    def write_row(self, key: str, body: Any, days: float = 0.5) -> None:
        stamp = (ANCHOR - timedelta(days=days)).isoformat(timespec="seconds")
        with contextlib.closing(self.library.connect(self.database())) as connection:
            self.library.writes.record_state(connection, "checkpoint", key=key, body=body, timestamp=stamp)

    def adjudication(self, *rows: tuple[str, str, str], branch: str = BRANCH, repository: str = REPOSITORY,
                     head: Any = "", days: float = 0.5, prefix: str = "ship-adjudication:", key: str = "k",
                     line: Any = 3, instances: Any = None, ruled: str | None = None) -> None:
        """One `sd-ship adjudicate` record: each (summary, disposition, reason) a blocking finding's answer,
        at the reviewed head unless `head` names another; `ruled`: a build ruling's, judging that evidence."""
        findings = [{"index": index, "finding_digest": "0" * 64, "response_disposition": answer, "reason": reason,
                     "raw_finding": {"path": "bin/tool.py", "line": line, "severity": "high", "instances": instances or [f"bin/tool.py:{line}"], "summary": summary,
                                     "family": "correctness", "disposition": "blocking",
                                     **({} if ruled is None else {"evidence": ruled})},
                     **({} if ruled is None else {"ruled": True})}
                    for index, (summary, answer, reason) in enumerate(rows, 1)]
        bindings = {"repository": repository, "branch": branch, "head": head if head != "" else self.head}
        self.write_row(f"{prefix}{key}", {"decision": "accepted", "proposal": {
            "schema_version": 1, "bindings": bindings, "findings": findings}}, days)

    def prompts(self, **overrides: Any) -> tuple[list[str], dict[str, Any], str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = sd_review.review(self.root, namespace(dry_run=True, **overrides), FakeRunner(),
                                      self.environment(), self.chatgpt_home())
        prompts = [row["stdin"] for row in result["planned_invocations"] if row["would_run"]]
        self.assertTrue(prompts)
        return prompts, result, stderr.getvalue()

    def section(self, prompt: str) -> list[str]:
        return prompt.split(sd_review.REBUTTED_HEADER, 1)[1].split("\n\n", 1)[0].splitlines()[1:]


class ThreatModelTests(RepeatFixture):
    def test_the_threat_model_and_its_rule_reach_every_prompt(self) -> None:
        self.local_block(self.root, f'threat-model: "{THREAT}"')
        prompts, result, _stderr = self.prompts()
        self.assertTrue(result["local_block_prepended"])
        for prompt in prompts:
            self.assertTrue(prompt.startswith("Repository-local conventions"))
            self.assertIn(f"Threat model: {THREAT}\n", prompt)
            self.assertIn("A finding outside this threat model is advisory at most", prompt)
            self.assertNotIn(f"threat-model: {THREAT}", prompt)

    def test_without_a_threat_model_line_the_prompt_says_nothing_of_one(self) -> None:
        for lines in ((), ('threat-model: "   "',)):
            with self.subTest(lines=lines):
                self.local_block(self.root, *lines)
                prompts, _result, _stderr = self.prompts()
                for prompt in prompts:
                    self.assertNotIn("Threat model:", prompt)
                    self.assertNotIn("outside this threat model", prompt)

    def test_a_huge_threat_model_is_cut_to_its_bound(self) -> None:
        self.local_block(self.root, "threat-model: " + "word " * 5000 + "TAIL")
        rendered = sd_review.local_conventions(self.root)
        line = next(row for row in rendered.splitlines() if row.startswith("Threat model: "))
        self.assertLessEqual(len(line), len("Threat model: ") + sd_review.THREAT_MODEL_CHARS)
        self.assertTrue(line.endswith("..."))
        self.assertNotIn("TAIL", rendered)

    def test_odd_local_files(self) -> None:
        local = self.root / "CLAUDE.local.md"
        for data in (b"", b"\n\n", "threat-model: outside the markers\n".encode()):
            with self.subTest(data=data):
                local.write_bytes(data)
                self.assertEqual(sd_review.local_conventions(self.root), "")
        # Not UTF-8: unknown, not empty. The review refuses naming the file rather than drop the model.
        local.write_bytes(b"<!-- SD-AI-COMMAND-PACK:LOCAL:START -->\nthreat-model: \xff\xfe\n"
                          b"<!-- SD-AI-COMMAND-PACK:LOCAL:END -->\n")
        with self.assertRaisesRegex(sd_review.sd_lib.ConfigError, "CLAUDE.local.md"):
            sd_review.local_conventions(self.root)


class RebuttedFindingTests(RepeatFixture):
    def test_this_branchs_rebuttals_from_both_namespaces_and_any_head_reach_every_prompt(self) -> None:
        self.adjudication(("lane claim one", "rebutted", "git 2.54 matches it literally"),
                          ("a parked risk", "parked", "accepted for now"), head=self.first, key="item")
        self.adjudication(("lane claim two", "rebutted", "out of the threat model"), key="item", days=0.4)
        self.adjudication(("local claim", "rebutted", "the test covers it"), prefix="ship-adjudication-no-item:",
                          key="local")
        self.adjudication(("another branch", "rebutted", "x"), branch="topic", key="other-branch")
        self.adjudication(("another repository", "rebutted", "x"), repository="fixture/elsewhere", key="other-repo")
        self.adjudication(("after the head", "rebutted", "x"), days=-1, key="late")
        self.adjudication(("no line", "rebutted", "x"), line=None, key="no-line")
        prompts, _result, stderr = self.prompts()
        for prompt in prompts:
            self.assertIn(sd_review.REBUTTED_HEADER, prompt)
            self.assertIn("- bin/tool.py:3: lane claim one Rebuttal: git 2.54 matches it literally", prompt)
            self.assertIn("- bin/tool.py:3: lane claim two Rebuttal: out of the threat model", prompt)
            self.assertIn("- bin/tool.py:3: local claim Rebuttal: the test covers it", prompt)
            self.assertIn("Raise one again only with new evidence", prompt)
            # Round 9: a receipt recorded after the head's commit, as every same-head retry's is, still rebuts.
            self.assertIn("- bin/tool.py:3: after the head Rebuttal: x", prompt)
            self.assertIn("- bin/tool.py: no line Rebuttal: x", prompt)
            for absent in ("a parked risk", "another branch", "another repository"):
                self.assertNotIn(absent, prompt)
        self.assertEqual(stderr, "")

    def test_no_rebuttal_adds_no_section(self) -> None:
        for setup in ("no database", "empty database", "other rows"):
            with self.subTest(setup=setup):
                if setup == "empty database":
                    self.database()
                if setup == "other rows":
                    self.adjudication(("parked only", "parked", "x"))
                prompts, _result, stderr = self.prompts()
                for prompt in prompts:
                    self.assertNotIn(sd_review.REBUTTED_HEADER, prompt)
                self.assertEqual(stderr, "")

    def test_the_cap_holds_and_the_oldest_go_first(self) -> None:
        total = sd_review.REBUTTED_LINES + 5
        for index in range(total):
            self.adjudication((f"claim {index:02d}", "rebutted", "reason"), key=f"k{index}", days=2 - index / 100)
        self.adjudication(("long " * 300 + "TAIL", "rebutted", "why " * 300 + "END"), key="long", days=0.01)
        self.adjudication((f"claim {total - 1:02d}", "rebutted", "said again"), key="again", days=0.005)
        lines = self.section(self.prompts()[0][0])
        self.assertEqual(len(lines), sd_review.REBUTTED_LINES, lines)
        self.assertTrue(lines[0].startswith(f"- bin/tool.py:3: claim {total - 1:02d} Rebuttal: said again"), lines[0])
        self.assertTrue(lines[1].startswith("- bin/tool.py:3: long long"), lines[1])
        self.assertNotIn("TAIL", "\n".join(lines))
        self.assertNotIn("END", "\n".join(lines))
        kept = {line.split(" Rebuttal:")[0] for line in lines}
        self.assertNotIn("- bin/tool.py:3: claim 00", kept)
        self.assertIn(f"- bin/tool.py:3: claim {total - 2:02d}", kept)
        for line in lines:
            self.assertLessEqual(len(line), 160 + sd_review.REBUTTED_SUMMARY_CHARS + sd_review.REBUTTED_REASON_CHARS)

    def test_malformed_rows_are_skipped_not_fatal(self) -> None:
        self.write_row("ship-adjudication:text", "not json at all")
        self.write_row("ship-adjudication:list", ["a", "b"])
        self.write_row("ship-adjudication:object", {"decision": "accepted", "proposal": {"bindings": {"repository": REPOSITORY, "branch": BRANCH, "head": self.head},
                                                                 "findings": {"index": 1}}})
        self.write_row("ship-adjudication:rows", {"decision": "accepted", "proposal": {
            "bindings": {"repository": REPOSITORY.upper(), "branch": BRANCH, "head": self.head},
            "findings": [7, "x", None, {"response_disposition": "rebutted"},
                         {"response_disposition": "rebutted", "reason": {"a": [1, {"b": 2}]},
                          "raw_finding": {"path": ["x"], "summary": {"nested": {"deeper": [1, 2, 3]}}}},
                         {"response_disposition": "rebutted", "reason": "fine",
                          "raw_finding": {"path": "bin/tool.py", "summary": "a well-formed\n\tclaim \udcff"}}]}})
        self.write_row("ship-adjudication:deep", {"proposal": {
            "bindings": {"repository": REPOSITORY, "branch": BRANCH, "head": self.head}, "findings": json.loads("[" * 200 + "]" * 200)}})
        prompts, _result, stderr = self.prompts()
        lines = self.section(prompts[0])
        # A lone surrogate is replaced, not a failed read; a nested value is its JSON text, on one line.
        self.assertIn("- bin/tool.py: a well-formed claim ? Rebuttal: fine", lines)
        # Round 12: a path that is not text cannot be diffed, so it is never listed (hold on doubt).
        self.assertNotIn('- ["x"]: {"nested": {"deeper": [1, 2, 3]}} Rebuttal: {"a": [1, {"b": 2}]}', lines)
        self.assertLessEqual(len(lines), sd_review.REBUTTED_LINES)
        self.assertEqual(stderr, "")

    def test_a_failed_read_is_unknown_not_empty(self) -> None:
        self.adjudication(("lane claim", "rebutted", "x"))
        stderr = io.StringIO()
        with unittest.mock.patch.object(self.library, "connect", side_effect=sd_review.sqlite3.OperationalError("locked")), \
                contextlib.redirect_stderr(stderr):
            text = sd_review.rebutted_findings(
                self.root, sd_review.resolve_subject(self.root, "worktree"), None, self.environment())
        self.assertEqual(text, [])
        self.assertEqual(stderr.getvalue(), "sd-review: reviewing without the rebutted findings: OperationalError: locked\n")

    def test_planning_and_a_detached_head_read_nothing(self) -> None:
        with unittest.mock.patch.object(self.library, "connect", side_effect=AssertionError("database read")):
            planning = sd_review.Subject("planning", "HEAD", "worktree", ("docs/work/x/design.md",), 1, "plan")
            self.assertEqual(sd_review.rebutted_findings(self.root, planning, None, self.environment()), [])
            subprocess.run(["git", "checkout", "--quiet", "--detach"], cwd=str(self.root), check=True,
                           env=clean_environment())
            subject = sd_review.resolve_subject(self.root, "worktree")
            self.assertEqual(sd_review.rebutted_findings(self.root, subject, None, self.environment()), [])

    def test_the_same_receipts_ask_the_same_request(self) -> None:
        """`--explain`, dispatch and a replay over the same receipts ask the same (sd:1397); a new receipt is
        new input, so it may change the request."""
        self.adjudication(("lane claim", "rebutted", "x"))
        first = self.prompts()[1]
        self.assertEqual(self.prompts()[1]["request_sha256"], first["request_sha256"])
        self.adjudication(("recorded later", "rebutted", "x"), days=-1, key="later")
        self.assertNotEqual(self.prompts()[1]["request_sha256"], first["request_sha256"])

    def test_a_read_capped_at_the_pass_asks_what_the_pass_asked(self) -> None:
        """Advisory sweep, pack sd:9973 (PR #1938): sd-ship's replay read receipts recorded after the pass,
        so its digest differed and a moved binding spent a pass. `--rebutted-through` caps the read."""
        self.adjudication(("lane claim", "rebutted", "x"))
        with contextlib.closing(self.library.connect(self.database())) as connection:
            through = connection.execute("SELECT MAX(id) FROM state").fetchone()[0]
        first = self.prompts(rebutted_through=through)[1]
        self.adjudication(("recorded later", "rebutted", "x"), days=-1, key="later")
        self.assertEqual(self.prompts(rebutted_through=through)[1]["request_sha256"], first["request_sha256"])
        self.assertNotEqual(self.prompts()[1]["request_sha256"], first["request_sha256"])


def row(severity: str = "high", **extra: Any) -> dict[str, Any]:
    return {"path": "src.py", "line": 1, "severity": severity, "summary": "defect", "family": "correctness"} | extra


#: Evidence that does not count: absent, empty, blank, or not a string.
UNCITED = (None, "", "   \n\t", 0, 7, [], ["cmd"], {}, {"command": "x", "output": "y"}, True)
CITED = ("python3 src.py: Traceback", "`f(1)` returns 2, expected 1")
REPRO = "python3 t.py: expected 1, got 2"


def finding(evidence: Any = None, **extra: Any) -> dict[str, Any]:
    return row(**extra) if evidence is None else row(evidence=evidence, **extra)



class ReproRuleTests(unittest.TestCase):
    """Operator ruling on pack sd:9867, amended by note #70162: a finding at the floor blocks only with a
    concrete failing input or command (`evidence`), in touched code or an unchanged caller it breaks; any
    other is advisory, filed, and says why."""

    def test_a_reproduced_finding_in_changed_code_blocks(self) -> None:
        for evidence in CITED:
            with self.subTest(evidence=evidence):
                [record] = sd_review.dispose([finding(evidence)], "high", repro=True)
                self.assertEqual((record["disposition"], record["summary"]), ("blocking", "defect"))

    def test_a_finding_without_a_repro_is_advisory_and_says_why(self) -> None:
        for evidence in UNCITED:
            with self.subTest(evidence=evidence):
                [record] = sd_review.dispose([finding(evidence)], "high", repro=True)
                self.assertEqual((record["disposition"], record["summary"]),
                                 ("advisory", f"defect {sd_review.NO_REPRO_NOTE}"))

    def test_the_gate_reads_whether_evidence_was_given_not_what_it_says(self) -> None:
        """sd:9869, class pass after round 7: a pattern over the evidence's prose downgraded a real repro
        (`Input: ... Actual: ... status 1.`) and passed an unrun one. The gate now checks only that a reader
        gave evidence; whether it reproduces is the lead's call, rebutted with a reason a reader can check."""
        for evidence in ("Input: an empty JSON array. Actual: the process terminates with status 1.",
                         "This fails.", "make test expected to work; never ran it", "python3 src.py: Traceback"):
            with self.subTest(evidence=evidence):
                [record] = sd_review.dispose([finding(evidence)], "high", repro=True)
                self.assertEqual((record["disposition"], record["summary"]), ("blocking", "defect"))
        self.assertFalse(hasattr(sd_review.sd_lib, "reproduces"))

    def test_a_duplicate_that_reproduces_keeps_the_finding_blocking(self) -> None:
        """sd:9869 review: two readers gave one finding, the first with "See summary", the second with a repro;
        deduplication kept the first's evidence, and the gate posted it as advisory. A blank first copy
        must not hide the second's."""
        for first, second in (("", REPRO), (REPRO, "")):
            with self.subTest(first=first, second=second):
                result = {"severity_floor": "high", "requested_reviews": 2}
                answers = [sd_review.Outcome(name, sd_review.FINDINGS, (finding(evidence),), "", (), ())
                           for name, evidence in (("one", first), ("two", second))]
                sd_review.finish_review(result, answers, ["one", "two"], repro=True)
                self.assertEqual(result["status"], "blocking")
                self.assertEqual([row["evidence"] for row in result["findings"]], [REPRO])

    def test_a_duplicate_keeps_every_readers_evidence_for_the_lead(self) -> None:
        """sd:9869 class pass: the lead judges whether evidence reproduces, so a vague first copy must not hide a
        second reader's repro; identical evidence is kept once."""
        answers = [sd_review.Outcome(name, sd_review.FINDINGS, (finding(evidence),), "", (), ())
                   for name, evidence in (("one", "See summary"), ("two", REPRO), ("three", REPRO))]
        result = {"severity_floor": "high", "requested_reviews": 3}
        sd_review.finish_review(result, answers, ["one", "two", "three"], repro=True)
        self.assertEqual([row["evidence"] for row in result["findings"]], [f"See summary\n{REPRO}"])

    def test_a_blocker_a_later_completed_pass_cleared_is_no_prior_blocker(self) -> None:
        """Lane round 2 at 1c528acb0 (pack sd:10008): pass 1 blocked on src.py:10 "Empty input crashes" and pass 2
        completed and cleared it. Only a blocker still open at the last completed pass gets an id and is carried;
        a pass that did not complete clears nothing."""
        first, second = ({"pass": index, "head": head * 40, "completed": True} for index, head in ((1, "a"), (2, "b")))
        blocker = dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")
        for label, history, found, expected in (
                ("cleared by pass 2", [first, second], first, "advisory"),
                ("found at pass 2", [first, second], second, "blocking"),
                ("pass 2 incomplete", [first, dict(second, completed=False)], first, "blocking"),
                ("no completion recorded", [{"pass": 1}, {"pass": 2}], {"pass": 1}, "blocking"),
                ("a single report", None, None, "blocking")):
            with self.subTest(label):
                previous = {"findings": [dict(blocker, **({"prior_review": found} if found else {}))],
                            **({"history": history} if history else {})}
                carried = [row["summary"] for row in sd_review.prior_blockers(previous)]
                self.assertEqual(carried, [] if expected == "advisory" else ["Empty input crashes"])

    def test_a_reproduced_break_in_an_unchanged_caller_blocks(self) -> None:
        [record] = sd_review.dispose([finding("python3 reader.py: KeyError 'state'", path="reader.py")], "high",
                                     repro=True)
        self.assertEqual((record["disposition"], record["summary"]), ("blocking", "defect"))

    def test_a_plan_keeps_the_floor_alone(self) -> None:
        [record] = sd_review.dispose([finding()], "high")
        self.assertEqual((record["disposition"], record["summary"]), ("blocking", "defect"))

    def test_an_uncited_repeat_keeps_its_own_note(self) -> None:
        rebutted = listed(adjudicated(("src.py", "defect", "rebutted")))
        [record] = sd_review.dispose([finding()], "high", rebutted, repro=True)
        self.assertEqual((record["disposition"], record["summary"]), ("advisory", f"defect {sd_review.REPEAT_NOTE}"))

    def test_one_spelling_for_every_accepted_form(self) -> None:
        root = pathlib.Path("/work/repo")
        spelled = sd_review.repo_spelling(root)
        self.assertEqual([spelled(path) for path in ("/work/repo/src.py", "./src.py", " src.py ", "src.py",
                                                     "<review-response>", "/elsewhere/src.py", None)],
                         ["src.py"] * 4 + ["<review-response>", "/elsewhere/src.py", None])

    def test_a_repro_on_input_no_caller_or_data_carries_is_advisory(self) -> None:
        """sd:10288, note #77932: a repro only on synthetic input no caller or recorded data produces
        posts as advisory, filed, and says why."""
        [record] = sd_review.dispose([finding(REPRO, trigger="synthetic")], "high", repro=True)
        self.assertEqual((record["disposition"], record["summary"]),
                         ("advisory", f"defect {sd_review.SYNTHETIC_NOTE}"))

    def test_the_same_repro_on_a_reachable_or_untrusted_input_still_blocks(self) -> None:
        """sd:10288: a real caller, recorded data, input from outside the operator's control (a network
        reply), a data-loss or secret-leak path, or a reader that does not say, still blocks."""
        for trigger in (*sorted(set(sd_review.TRIGGERS) - {"synthetic"}), None, "Synthetic", " synthetic", 3):
            with self.subTest(trigger=trigger):
                raw = finding(REPRO) if trigger is None else finding(REPRO, trigger=trigger)
                parsed = sd_review._finding(raw)
                assert parsed is not None
                [record] = sd_review.dispose([parsed], "high", repro=True)
                self.assertEqual((record["disposition"], record["summary"]), ("blocking", "defect"))

    def test_a_planning_review_keeps_the_floor_alone(self) -> None:
        [record] = sd_review.dispose([finding(REPRO, trigger="synthetic")], "high")
        self.assertEqual(record["disposition"], "blocking")

    def test_a_synthetic_finding_below_the_floor_gets_no_note(self) -> None:
        [record] = sd_review.dispose([finding(REPRO, severity="medium", trigger="synthetic")], "high", repro=True)
        self.assertEqual((record["disposition"], record["summary"]), ("advisory", "defect"))

    def test_an_unknown_trigger_is_a_schema_error(self) -> None:
        self.assertEqual(sd_review._finding_error(finding(REPRO, trigger="maybe", instances=["src.py:1"])),
                         "trigger: invalid enum value")
        for value in ([], {}, None, 3, "Synthetic"):
            with self.subTest(value=value):
                self.assertEqual(sd_review._finding_error(finding(REPRO, trigger=value, instances=["src.py:1"])),
                                 "trigger: invalid enum value")
        self.assertEqual(sd_review._finding_error(finding(REPRO, trigger="untrusted", instances=["src.py:1"])), "")

    def test_a_defect_the_merge_base_already_has_is_advisory(self) -> None:
        """sd:10892: a finding blocks only when the merge base lacks the same defect; one main already
        has posts as advisory, filed for the repo, and says why."""
        parsed = sd_review._finding(finding(REPRO, base="has"))
        assert parsed is not None
        [record] = sd_review.dispose([parsed], "high", repro=True, on_base=lambda _row: True)
        self.assertEqual((record["disposition"], record["summary"]),
                         ("advisory", f"defect {sd_review.ON_BASE_NOTE}"))

    def test_a_has_answer_no_prompt_asked_for_still_blocks(self) -> None:
        """sd:10892 review: with no main merge base named in the prompt, a reader's `has` was judged
        against no stated baseline, so it downgrades nothing."""
        parsed = sd_review._finding(finding(REPRO, base="has"))
        assert parsed is not None
        for on_base in (None, lambda _row: False):
            [record] = sd_review.dispose([parsed], "high", repro=True, on_base=on_base)
            self.assertEqual((record["disposition"], record["summary"]), ("blocking", "defect"))

    def test_the_review_passes_whether_it_asked_to_dispose(self) -> None:
        result = {"severity_floor": "high", "requested_reviews": 1}
        answer = sd_review.Outcome("fake", sd_review.FINDINGS, (finding(REPRO, base="has", instances=["src.py:1"]),),
                                   "", (), ())
        sd_review.finish_review(dict(result), [answer], ["fake"], repro=True)
        asked = dict(result)
        sd_review.finish_review(asked, [answer], ["fake"], repro=True, on_base=lambda _row: True)
        self.assertEqual(asked["status"], "advisory")

    def test_a_has_downgrades_only_when_every_merged_copy_holds(self) -> None:
        """sd:10892 round 4: `distinct_findings` merges one finding two readers report and keeps the
        first reader's `backend`. A patch-only reader's copy, or a `lacks` copy, keeps it blocking."""
        result = {"severity_floor": "high", "requested_reviews": 2}
        def answer(backend: str, base: str) -> Any:
            return sd_review.Outcome(backend, sd_review.FINDINGS,
                                     (finding(REPRO, base=base, instances=["src.py:1"]),), "", (), ())
        reads = lambda row: row.get("backend") == "local"  # noqa: E731
        for label, outcomes, status in (
                ("both read the checkout", [answer("local", "has"), answer("local", "has")], "advisory"),
                ("a patch-only copy", [answer("local", "has"), answer("remote", "has")], "blocking"),
                ("a patch-only copy first", [answer("remote", "has"), answer("local", "has")], "blocking"),
                ("a lacks copy", [answer("local", "has"), answer("local", "lacks")], "blocking")):
            with self.subTest(label):
                done = sd_review.finish_review(dict(result), outcomes, [o.backend for o in outcomes],
                                               repro=True, on_base=reads)
                self.assertEqual(done["status"], status)

    def test_a_defect_the_change_causes_still_blocks(self) -> None:
        """sd:10892: `lacks`, an older answer without `base`, or a value not exactly `has` still blocks."""
        for base in ("lacks", None, "Has", " has", 3, []):
            with self.subTest(base=base):
                parsed = sd_review._finding(finding(REPRO) if base is None else finding(REPRO, base=base))
                assert parsed is not None
                [record] = sd_review.dispose([parsed], "high", repro=True, on_base=lambda _row: True)
                self.assertEqual((record["disposition"], record["summary"]), ("blocking", "defect"))

    def test_the_prompt_asks_whether_the_merge_base_has_it(self) -> None:
        self.assertIn("base", sd_review.FINDING_PROPERTIES)
        self.assertIn('"base": "lacks"', sd_review.URL_OUTPUT_CONTRACT)

    def test_has_means_the_same_failure_at_the_merge_base_not_a_defect_made_reachable(self) -> None:
        """sd:10892 ruling round: a branch caller that starts sending empty input to an unchanged parser
        is this change's failure, so the prompt says that is `lacks`, never `has`."""
        self.assertIn("same failure already happens there, by the same route", sd_review.BASES["has"])
        self.assertIn("makes an old defect reachable", sd_review.BASES["lacks"])

    def test_an_unknown_base_is_a_schema_error(self) -> None:
        for value in ("maybe", [], None, "Has"):
            with self.subTest(value=value):
                self.assertEqual(sd_review._finding_error(finding(REPRO, base=value, instances=["src.py:1"])),
                                 "base: invalid enum value")
        self.assertEqual(sd_review._finding_error(finding(REPRO, base="lacks", instances=["src.py:1"])), "")

    def test_the_lanes_own_blockers_need_no_repro(self) -> None:
        parsed = sd_review.oversized_findings()
        assert parsed is not None
        limit = [record for record in sd_review.dispose(parsed.findings, "high", repro=True)
                 if record["path"] == "<review-response>"]
        self.assertEqual([record["disposition"] for record in limit], ["blocking"])

    def test_the_review_reads_the_changed_files_and_status_follows(self) -> None:
        result = {"severity_floor": "high", "requested_reviews": 1}
        answer = sd_review.Outcome("fake", sd_review.FINDINGS, (finding(),), "", (), ())
        sd_review.finish_review(result, [answer], ["fake"], repro=True)
        self.assertEqual(result["status"], "advisory")
        cited = sd_review.Outcome("fake", sd_review.FINDINGS, (finding(CITED[0]),), "", (), ())
        sd_review.finish_review(result, [cited], ["fake"], repro=True)
        self.assertEqual(result["status"], "blocking")


class RepeatRuleTests(unittest.TestCase):
    """Operator ruling on pack sd:9270: evidence is asked only of a repeat of a rebuttal."""

    def setUp(self) -> None:
        self.REBUTTED = listed(adjudicated(("src.py", "defect", "rebutted")))

    def test_an_uncited_first_time_blocker_stays_blocking(self) -> None:
        for evidence in UNCITED:
            # Another claim one line off: one at the same line is a reworded repeat (sd:10918).
            for rebutted in ([], listed(adjudicated(("src.py", "another claim", "rebutted", 2),
                                                    ("other.py", "defect", "rebutted")))):
                with self.subTest(evidence=evidence, rebutted=rebutted):
                    [record] = sd_review.dispose([finding(evidence)], "high", rebutted)
                    self.assertEqual((record["disposition"], record["summary"]), ("blocking", "defect"))

    def test_an_uncited_repeat_of_a_rebuttal_is_advisory_and_says_why(self) -> None:
        for evidence in UNCITED:
            for summary in ("defect", "  defect\n"):
                with self.subTest(evidence=evidence, summary=summary):
                    [record] = sd_review.dispose([finding(evidence, summary=summary)], "high", self.REBUTTED)
                    self.assertEqual(record["disposition"], "advisory")
                    self.assertEqual(record["summary"], f"{summary.strip()} {sd_review.REPEAT_NOTE}".lstrip())
                    self.assertEqual(record["severity"], "high")

    def test_a_cited_repeat_stays_blocking(self) -> None:
        for evidence in CITED:
            with self.subTest(evidence=evidence):
                [record] = sd_review.dispose([finding(evidence)], "high", self.REBUTTED)
                self.assertEqual((record["disposition"], record["summary"]), ("blocking", "defect"))

    def test_the_floor_still_decides_and_advice_is_untouched(self) -> None:
        disposed = sd_review.dispose([row("medium"), row("low"), row("unspecified")], "high", self.REBUTTED)
        self.assertEqual([(r["disposition"], r["summary"]) for r in disposed], [("advisory", "defect")] * 3)
        [medium] = sd_review.dispose([row("medium")], "medium")
        self.assertEqual(medium["disposition"], "blocking")

    def test_the_lanes_own_blockers_keep_blocking(self) -> None:
        for parsed in (sd_review.oversized_findings(),
                       sd_review.parse_findings(json.dumps({"findings": [row("low")] * (sd_review.MAX_FINDINGS + 1)}))):
            assert parsed is not None
            limit = [record for record in sd_review.dispose(parsed.findings, "high") if record["path"] == "<review-response>"]
            self.assertEqual([record["disposition"] for record in limit], ["blocking"])

    def test_the_parser_carries_evidence_and_reads_an_answer_without_it(self) -> None:
        listed = {"instances": ["src.py:1"]}  # pack sd:9769: a high finding lists its instances
        parsed = sd_review.parse_findings(json.dumps({"findings": [row(evidence="  make test: 1 failed  ", **listed),
                                                                   row(**listed)]}))
        assert parsed is not None
        self.assertEqual(parsed.error, "")
        self.assertEqual(parsed.findings[0]["evidence"], "make test: 1 failed")
        self.assertNotIn("evidence", parsed.findings[1])
        long = sd_review.parse_findings(json.dumps({"findings": [row(evidence="x" * 50_000)]}))
        assert long is not None
        self.assertEqual(len(long.findings[0]["evidence"]), sd_review.EVIDENCE_CHARS)
        # Review of sd:10286 (bin/sd-review:1799): two repros that share the kept prefix stay two, or a
        # ruling on one holds back the other in the build and the lane alike.
        shared = "python3 t.py --input " + "a" * 2000
        cut = [sd_review.parse_findings(json.dumps({"findings": [row(evidence=shared + tail)]})) for tail in "12"]
        self.assertNotEqual(*(answer.findings[0]["evidence"] for answer in cut if answer is not None))
        self.assertEqual({len(answer.findings[0]["evidence"]) for answer in cut if answer is not None},
                         {sd_review.EVIDENCE_CHARS})
        # Class pass 5 (bin/sd-review:1248): a literal citation of a cut evidence's stored form is another
        # text than the one it was cut from, so it reads as another repro.
        assert cut[0] is not None
        stored = cut[0].findings[0]["evidence"]
        literal = sd_review.parse_findings(json.dumps({"findings": [row(evidence=stored)]}))
        assert literal is not None
        self.assertNotEqual(literal.findings[0]["evidence"], stored)
        # Class pass 6 (bin/sd-review:1247): a saved ruling may hold a form an earlier release cut, with
        # a 12-digit digest; a literal citation of it is no copy of the text behind it either.
        for old in ("python3 t.py --input aaaa... sha256:0123456789ab", "x... sha256:" + "f" * 16):
            with self.subTest(old=old):
                cited = sd_review.parse_findings(json.dumps({"findings": [row(evidence=old)]}))
                assert cited is not None
                self.assertNotEqual(cited.findings[0]["evidence"], old)
        nested = sd_review.parse_findings(json.dumps({"findings": [row(evidence={"command": {"argv": ["git"]}})]}))
        assert nested is not None
        self.assertEqual(sd_review.dispose(nested.findings, "high", self.REBUTTED)[0]["disposition"], "advisory")

    def test_null_or_odd_evidence_is_empty_never_an_error(self) -> None:
        """Round 12: a reader that sends `"evidence": null`, or another type, cites nothing and breaks nothing."""
        for evidence in (None, 0, 7, True, [], ["cmd"], {}, {"command": {"argv": ["git"]}}):
            with self.subTest(evidence=evidence):
                parsed = sd_review.parse_findings(json.dumps({"findings": [row(evidence=evidence, instances=["src.py:1"])]}))
                assert parsed is not None
                self.assertEqual(parsed.error, "")
                self.assertNotIn("evidence", parsed.findings[0])

    def test_the_schema_carries_evidence(self) -> None:
        item = sd_review.CODEX_OUTPUT_SCHEMA["properties"]["findings"]["items"]
        self.assertEqual(item["properties"]["evidence"], {"type": "string"})
        self.assertIn("evidence", item["required"])
        self.assertIn("evidence", sd_review.URL_OUTPUT_CONTRACT)

    def test_a_static_reader_may_answer_a_rebuttal_with_the_lines_it_read(self) -> None:
        body = adjudicated(("src.py", "defect", "rebutted"))
        seen = listed(body)
        self.assertEqual(seen, self.REBUTTED)
        section = sd_review.rebuttal_section(seen)
        self.assertIn("a check you ran and its output", section)
        self.assertIn("when you cannot run commands, path:line and a quote of the current code", section)
        self.assertIn("One raised again without it blocks nothing", section)
        self.assertEqual(sd_review.rebuttal_section([]), "")

    def test_one_finding_from_two_readers_is_one_row_whatever_each_cites(self) -> None:
        bare, cited, other = row(), row(evidence="src.py:1: x = 1"), row(evidence="python3 src.py: wrong")
        kept = sd_review.sd_lib.distinct_findings([dict(bare, backend="codex"), dict(cited, backend="second"),
                                                   dict(other, backend="third")])
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0]["backend"], kept[0]["evidence"]), ("codex", "src.py:1: x = 1\npython3 src.py: wrong"))
        two = sd_review.sd_lib.distinct_findings([dict(row(), backend="codex"), dict(row(summary="other"), backend="x")])
        self.assertEqual(len(two), 2)

    def test_one_finding_from_two_readers_keeps_every_instance(self) -> None:
        """Review of sd:9769: the build matches a blocker by its instances, so a dropped list ships a blocker."""
        kept = sd_review.sd_lib.distinct_findings([dict(row(), backend="codex", instances=["src.py:1"]),
                                                   dict(row(), backend="second", instances=["src.py:1", "far.py:9"])])
        self.assertEqual(kept[0]["instances"], ["src.py:1", "far.py:9"])


class RepeatReviewTests(RepeatFixture):
    def test_no_reviewer_is_told_to_cite_every_blocker(self) -> None:
        prompts, _result, _stderr = self.prompts()
        self.assertEqual(len(prompts), 2)
        for text in [*prompts, sd_review.URL_OUTPUT_CONTRACT]:
            self.assertIn(sd_review.EVIDENCE_ASK, text)
            self.assertNotIn("posted as advisory", text)
            self.assertNotIn("run a check before", text)
        claude = sd_review.claude_argv(self.tmp, "claude")
        self.assertIn('"evidence"', claude[claude.index("--json-schema") + 1])

    def test_the_evidence_ask_fits_a_read_only_reader(self) -> None:
        # Readers get Read/Grep/Glob or a read-only sandbox: asking what they ran invites made-up output.
        self.assertNotIn("you ran", sd_review.EVIDENCE_ASK)
        self.assertIn("input", sd_review.EVIDENCE_ASK)

    def test_a_review_holds_only_an_uncited_repeat_of_a_rebuttal_back(self) -> None:
        """A cited repeat blocks, though bin/tool.py is not in this change: note #70162 on sd:9867."""
        self.adjudication(("lane claim", "rebutted", "git 2.54 matches it literally"))
        fresh = row(summary="a first-time claim", evidence="python3 src.py: Traceback")
        for evidence, status in ((None, "advisory"), ("python3 bin/tool.py: glob = True raises TypeError", "blocking")):
            with self.subTest(evidence=evidence):
                repeat = finding(evidence, path="bin/tool.py", line=3, summary="lane claim")
                runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", ""),
                                     "codex": sd_review.Completed(0, json.dumps({"findings": [repeat, fresh]}), "")},
                                    default=sd_review.Completed(0, json.dumps({"findings": []}), ""))
                result = sd_review.review(self.root, namespace(), runner, self.environment(), self.chatgpt_home())
                self.assertEqual(result["status"], "blocking")
                rows = {r["path"]: (r["disposition"], r["summary"]) for r in result["findings"] if r["backend"] == "codex"}
                self.assertEqual(rows["src.py"], ("blocking", "a first-time claim"))
                note = " " + sd_review.REPEAT_NOTE if status == "advisory" else ""
                self.assertEqual(rows["bin/tool.py"], (status, "lane claim" + note))


    def test_a_rebuttal_instance_in_another_spelling_answers_its_repeat(self) -> None:
        """build/sd-1623 review (bin/sd-review:884, :2614): a rebuttal answering `./src.py:1` as an instance.
        The review spelled the repeat's path `src.py` but compared it with the raw `./src.py:1`, so it blocked."""
        (self.root / "bin").mkdir(exist_ok=True)
        (self.root / "bin" / "tool.py").write_text("a\nb\nc\n", encoding="utf-8")
        head = self.commit("add", "src.py", "bin/tool.py") and self.commit("tracked")
        self.adjudication(("lane claim", "rebutted", "x"), head=head, instances=["bin/tool.py:3", "./src.py:1"])
        blocker = dict(finding(REPRO, path="bin/tool.py", line=3, summary="lane claim",
                               instances=["bin/tool.py:3", "./src.py:1"]), disposition="blocking",
                       prior_review={"pass": 1, "head": head})
        report = self.tmp / "aggregate.json"
        report.write_text(json.dumps({"scope": "branch", "subject": {"head": head}, "findings": [blocker]}))
        self.commit("next")
        repeat = finding(path="src.py", line=1, summary="lane claim", instances=["src.py:1"])
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", ""),
                             "codex": sd_review.Completed(0, json.dumps({"findings": [repeat]}), "")},
                            default=sd_review.Completed(0, json.dumps({"findings": []}), ""))
        result = sd_review.review(self.root, namespace(scope="branch", resume_report=str(report)), runner,
                                  self.environment(), self.chatgpt_home())
        rows = [(r["disposition"], r["summary"]) for r in result["findings"] if r["backend"] == "codex"]
        self.assertEqual(rows, [("advisory", f"lane claim {sd_review.REPEAT_NOTE}")])

    def test_a_failed_later_pass_keeps_an_earlier_blocker(self) -> None:
        """build/sd-1423 review (bin/sd-review:1840): pass 1 recorded a blocker and pass 2 stored a report in
        which every provider failed; its digest marked it complete, so the next full-branch pass dropped pass 1
        and an uncited repeat posted as advisory. The aggregate records no pass status, so every pass counts,
        and the blocker is carried with no `fixed` verdict (pack sd:10087)."""
        (self.root / "src.py").write_text("".join(f"line{n}\n" for n in range(1, 41)), encoding="utf-8")
        prior_head = self.commit("add", "src.py") and self.commit("src")
        head = self.commit("next")
        blocker = dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking",
                       prior_review={"pass": 1, "head": prior_head})
        report = self.tmp / "aggregate.json"
        report.write_text(json.dumps({"scope": "branch", "subject": {"head": prior_head}, "findings": [blocker],
                                      "history": [{"pass": 1, "head": prior_head, "report_digest": "a"},
                                                  {"pass": 2, "head": prior_head, "report_digest": "b"}]}))
        repeat = finding(line=11, summary="An empty list raises IndexError")
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", ""),
                             "codex": sd_review.Completed(0, json.dumps({"findings": [repeat]}), "")},
                            default=sd_review.Completed(0, json.dumps({"findings": []}), ""))
        result = sd_review.review(self.root, namespace(scope="branch", resume_report=str(report)), runner, self.environment(),
                                  self.chatgpt_home())
        self.assertEqual(result["subject"]["head"], head)
        self.assertEqual([r["disposition"] for r in result["findings"] if r.get("prior_blocker")], ["blocking"])

    def test_a_prior_report_with_a_nul_in_a_path_is_refused_before_any_provider(self) -> None:
        """build/sd-1423 class pass (bin/sd-review:1865): the prompt's source read resolves each prior blocker's
        path, and a NUL raised `ValueError` there. Validation refuses the report as it refuses a non-string."""
        report = self.tmp / "nul.json"
        report.write_text(json.dumps({"scope": "branch", "subject": {"head": self.first}, "findings": [
            dict(finding(REPRO, path="a\0b.py", line=3), disposition="blocking")]}))
        for kwargs in ({"resume_report": str(report)}, {"verify_report": str(report), "base": self.first}):
            with self.subTest(kwargs=sorted(kwargs)):
                runner = FakeRunner()
                with self.assertRaisesRegex(sd_review.UsageError, "prior review report does not match"):
                    sd_review.review(self.root, namespace(scope="branch", **kwargs), runner, self.environment(),
                                     self.chatgpt_home())
                self.assertEqual(runner.calls, [])

#: The prior blocker every verdict test carries: B1.
PRIOR = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")]


def verdict(verdict: str = "fixed", evidence: Any = REPRO, id: Any = "B1") -> dict[str, Any]:
    return {"id": id, "verdict": verdict, "evidence": evidence}


def answer(name: str, *findings: dict[str, Any], verdicts: Any = (), status: str = "") -> Any:
    return sd_review.Outcome(name, status or (sd_review.FINDINGS if findings else sd_review.CLEAN), findings, "", (), (),
                             verdicts=tuple(verdicts))


def verified(*answers: Any, rebutted: Any = ()) -> dict[str, Any]:
    """`finish_review` over `answers` against `PRIOR`; only completed answers count as reviewed."""
    result = {"severity_floor": "high", "requested_reviews": len(answers)}
    done = [row.backend for row in answers if row.status in (sd_review.CLEAN, sd_review.FINDINGS)]
    return sd_review.finish_review(result, answers, done, rebutted, repro=True, prior=PRIOR)


class PriorVerdictTests(unittest.TestCase):
    """pack sd:10087: a later pass cleared a prior blocker by paraphrase, a nearby caller, a moved line or a
    noprefix diff, since a prose matcher decided whether a finding repeated it. Each prior blocker now has an
    id, and it clears only when every completed reader returns `fixed` for that id with evidence."""

    def test_a_prior_blocker_clears_only_on_a_fixed_verdict_with_evidence(self) -> None:
        for label, findings, verdicts, status in (
                ("silence", (), (), "blocking"),
                ("paraphrase at a moved line", (finding(line=31, summary="An empty list raises IndexError"),), (), "blocking"),
                ("same summary at a nearby caller", (finding(line=14, summary="Empty input crashes"),), (), "blocking"),
                ("open", (), (verdict("open"),), "blocking"),
                ("fixed, no evidence", (), (verdict(evidence=""),), "blocking"),
                ("fixed, blank evidence", (), (verdict(evidence=" \n"),), "blocking"),
                ("fixed for another id", (), (verdict(id="B2"),), "blocking"),
                ("fixed with evidence", (), (verdict(),), "clean")):
            with self.subTest(label):
                result = verified(answer("one", *findings, verdicts=verdicts))
                self.assertEqual(result["status"], status)
                carried = [row for row in result["findings"] if row.get("prior_blocker")]
                self.assertEqual([row["prior_blocker"] for row in carried], ["B1"] if status == "blocking" else [])

    def test_every_completed_reader_must_answer_fixed(self) -> None:
        self.assertEqual(verified(answer("one", verdicts=[verdict()]), answer("two"))["status"], "blocking")
        self.assertEqual(verified(answer("one", verdicts=[verdict()]),
                                  answer("two", verdicts=[verdict("open", "")]))["status"], "blocking")
        self.assertEqual(verified(answer("one", verdicts=[verdict()]), answer("two", verdicts=[verdict()]))["status"], "clean")

    def test_a_failed_readers_fixed_verdict_counts_for_nothing(self) -> None:
        failed = answer("one", verdicts=[verdict()], status=sd_review.UNAVAILABLE)
        self.assertEqual(verified(failed, answer("two"))["status"], "blocking")
        parsed = sd_review.parse_findings(json.dumps({"findings": [], "verdicts": [verdict()]}))
        self.assertEqual(sd_review._answer("one", sd_review.Completed(0, "", ""), parsed).verdicts, (verdict(),))

    def test_an_open_verdict_from_a_failed_reader_still_holds(self) -> None:
        """Review of build/sd-2205 (bin/sd-review:1847): a reader that failed after saying `open` with evidence
        lost its verdict, and a fallback's `fixed` cleared the blocker. Only `fixed` needs a completed answer."""
        failed = answer("one", verdicts=[verdict("open", "python3 t.py: still 2")], status=sd_review.UNAVAILABLE)
        result = verified(failed, answer("two", verdicts=[verdict()]))
        self.assertEqual((result["status"], [row["evidence"] for row in result["findings"]]),
                         ("blocking", [f"{REPRO}\npython3 t.py: still 2"]))
        parsed = sd_review.parse_findings(json.dumps({"findings": [], "verdicts": [verdict("open")]}))
        self.assertEqual(sd_review._answer("one", sd_review.Completed(1, "", ""), parsed).verdicts, (verdict("open"),))
        bad_findings = sd_review.parse_findings(json.dumps({"findings": [{"path": "src.py"}], "verdicts": [verdict("open")]}))
        self.assertEqual(sd_review._answer("one", sd_review.Completed(0, "", ""), bad_findings).verdicts, (verdict("open"),))

    def test_a_malformed_verdict_never_drops_what_holds_a_blocker(self) -> None:
        """Round 3 of build/sd-2205 (bin/sd-review:1381), the class: degraded input must hold a blocker, never
        release it. One bad verdict dropped the valid `open` beside it, and a fallback's `fixed` cleared B1."""
        def failed(body: Any) -> Any:
            return sd_review._answer("one", sd_review.Completed(0, "", ""), sd_review.parse_findings(json.dumps(body)))
        for label, body in (
                ("a bad verdict beside a valid open", {"findings": [], "verdicts": [verdict("maybe"), verdict("open")]}),
                ("an open in an unknown spelling", {"findings": [], "verdicts": [verdict("OPEN")]}),
                ("verdicts that are no array", {"findings": [], "verdicts": {"B1": "open"}}),
                ("an open beside no findings array", {"verdicts": [verdict("open")]})):
            with self.subTest(label):
                outcome = failed(body)
                self.assertNotIn(outcome.status, (sd_review.CLEAN, sd_review.FINDINGS))
                self.assertEqual(verified(outcome, answer("two", verdicts=[verdict()]))["status"], "blocking")
        self.assertIsNone(sd_review.parse_findings(json.dumps({"verdicts": []})))
        self.assertIsNone(sd_review.parse_findings(json.dumps({"summary": "no review"})))

    def test_an_open_verdict_holds_whatever_its_id_spelling(self) -> None:
        """The same class in `carried`: an `open` for `b1` matched no blocker, so the `fixed` beside it cleared
        B1. An id reads without case or space; an `open` naming no listed blocker holds every one."""
        for label, verdicts, status in (
                ("open in lower case", [verdict(), verdict("open", id="b1")], "blocking"),
                ("open for an id never listed", [verdict(), verdict("open", id="B7")], "blocking"),
                ("fixed in lower case", [verdict(id=" b1 ")], "clean")):
            with self.subTest(label):
                self.assertEqual(verified(answer("one", verdicts=verdicts))["status"], status)

    def test_an_interrupted_pass_keeps_every_prior_blocker_and_its_new_evidence(self) -> None:
        # Review of build/sd-2205 (bin/sd-review:1902): a pass with no completed reader carried nothing, so its
        # report, the next resume's input, lost each prior blocker and a failed reader's `open` evidence.
        for label, verdicts, evidence in (("silent", (), REPRO),
                                          ("open", [verdict("open", "python3 t.py: still 2")], f"{REPRO}\npython3 t.py: still 2"),
                                          ("fixed", [verdict()], REPRO)):
            with self.subTest(label):
                result = verified(answer("one", status=sd_review.UNAVAILABLE, verdicts=verdicts))
                [row] = result["findings"]
                self.assertEqual((result["status"], result["completed_reviews"], row["disposition"], row["evidence"]),
                                 (sd_review.UNAVAILABLE, 0, "blocking", evidence))

    def test_new_open_evidence_survives_stored_evidence_at_the_cap(self) -> None:
        # Round 5 of build/sd-2205 (bin/sd-review:1917): stored evidence at the cap filled the row, so the slice
        # dropped each reader's new `open` evidence and a later reader could clear the blocker on the old input.
        full = [dict(PRIOR[0], evidence="x" * sd_review.EVIDENCE_CHARS)]
        for label, answers in (("interrupted", [answer("one", status=sd_review.UNAVAILABLE, verdicts=[verdict("open", "input A fails")])]),
                               ("completed", [answer("one", verdicts=[verdict("open", "input A fails")]),
                                              answer("two", verdicts=[verdict("open", "y" * sd_review.EVIDENCE_CHARS)])])):
            with self.subTest(label):
                done = [row.backend for row in answers if row.status in (sd_review.CLEAN, sd_review.FINDINGS)]
                result = sd_review.finish_review({"severity_floor": "high", "requested_reviews": len(answers)}, answers, done,
                                                 (), repro=True, prior=full)
                [row] = result["findings"]
                self.assertIn("input A fails", row["evidence"])
                self.assertIn("x" * 100, row["evidence"])
                self.assertLessEqual(len(row["evidence"]), sd_review.EVIDENCE_CHARS)

    def test_a_rebutted_prior_blocker_is_not_carried(self) -> None:
        rebutted = listed(adjudicated(("src.py", "Empty input crashes", "rebutted"), line=10))
        self.assertEqual(verified(answer("one"), rebutted=rebutted)["status"], "clean")

    def test_a_rebuttal_holds_no_open_verdict_that_cites_a_fresh_repro(self) -> None:
        """Review of build/sd-2205 (bin/sd-review:1969), the class: a saved judgment erased fresh evidence.
        A reader's `open` with evidence is a citation, as a finding's is in `dispose`: a plain rebuttal holds
        only an uncited `open`, and a build's ruling only one citing the evidence it judged."""
        plain = listed(adjudicated(("src.py", "Empty input crashes", "rebutted"), line=10))
        body = json.loads(adjudicated(("src.py", "Empty input crashes", "rebutted"), line=10))
        body["proposal"]["findings"][0].update(ruled=True)
        body["proposal"]["findings"][0]["raw_finding"]["evidence"] = REPRO
        ruled = listed(json.dumps(body))
        fresh = "python3 t.py --empty: IndexError"
        for label, rebutted, verdicts, status in (
                ("plain, open with a fresh repro", plain, [verdict("open", fresh)], "blocking"),
                ("plain, open with no evidence", plain, [verdict("open", "")], "clean"),
                ("plain, open for no listed id", plain, [verdict("open", fresh, id="B7")], "blocking"),
                ("ruled, open with a fresh repro", ruled, [verdict("open", fresh)], "blocking"),
                ("ruled, open citing the judged repro", ruled, [verdict("open", REPRO)], "clean"),
                ("ruled, silence", ruled, [], "clean")):
            with self.subTest(label):
                self.assertEqual(verified(answer("one", verdicts=verdicts), rebutted=rebutted)["status"], status)
        failed = answer("one", verdicts=[verdict("open", fresh)], status=sd_review.UNAVAILABLE)
        [row] = verified(failed, answer("two", verdicts=[verdict()]), rebutted=plain)["findings"]
        self.assertIn(fresh, row["evidence"])

    def test_an_open_citing_the_ruled_repro_clears_no_blocker_saved_with_another(self) -> None:
        """Last round of build/sd-2205 (bin/sd-review:1968), the class: a hold decided on one piece of
        evidence while another stands. A citation of the ruled repro skipped the saved-evidence test, so it
        cleared a blocker saved with a repro the ruling never judged. Every evidence must be the judged one:
        the stored one, each saved beside it and each `open` citation."""
        body = json.loads(adjudicated(("src.py", "Empty input crashes", "rebutted"), line=10))
        body["proposal"]["findings"][0].update(ruled=True)
        body["proposal"]["findings"][0]["raw_finding"]["evidence"] = REPRO
        ruled, fresh = listed(json.dumps(body)), "python3 t.py --empty: IndexError"
        beside = dict(finding(fresh, line=11, summary="A short list crashes"), disposition="blocking")
        def held(prior: list[dict[str, Any]], *verdicts: dict[str, Any]) -> list[str]:
            result = sd_review.finish_review({"severity_floor": "high", "requested_reviews": 1},
                                             [answer("one", verdicts=verdicts)], ["one"], ruled, repro=True, prior=prior)
            return [row["prior_blocker"] for row in result["findings"]]
        other = [dict(PRIOR[0], evidence=fresh)]
        for label, prior, verdicts, kept in (
                ("saved with another repro, open cites the ruled one", other, [verdict("open", REPRO)], ["B1"]),
                ("saved with another repro, an unlisted open cites the ruled one", other, [verdict("open", REPRO, id="B7")], ["B1"]),
                ("saved with another repro, a blank open beside the citation", other,
                 [verdict("open", ""), verdict("open", REPRO)], ["B1"]),
                ("another blocker saved beside it", [*PRIOR, beside], [verdict("open", REPRO)], ["B1", "B2"]),
                ("saved with the ruled repro, open cites it", PRIOR, [verdict("open", REPRO)], [])):
            with self.subTest(label):
                self.assertEqual(held(prior, *verdicts), kept)

    def test_the_carried_row_keeps_its_place_and_adds_the_open_evidence(self) -> None:
        [row] = verified(answer("one", verdicts=[verdict("open", "python3 t.py: still 2")]))["findings"]
        self.assertEqual({key: row[key] for key in ("path", "line", "summary", "disposition", "evidence")},
                         {"path": "src.py", "line": 10, "summary": "Empty input crashes", "disposition": "blocking",
                          "evidence": f"{REPRO}\npython3 t.py: still 2"})

    def test_the_parser_reads_verdicts_and_refuses_a_malformed_one(self) -> None:
        parsed = sd_review.parse_findings(json.dumps({"findings": [], "verdicts": [verdict()]}))
        self.assertEqual((parsed.error, parsed.verdicts), ("", (verdict(),)))
        self.assertEqual(sd_review.parse_findings(json.dumps({"findings": []})).verdicts, ())
        for bad in ([verdict("maybe")], [verdict(id=3)], [verdict(id="")], [verdict(evidence=None)],
                    [dict(verdict(), extra=1)], ["B1"], {"B1": "fixed"}):
            with self.subTest(bad=bad):
                self.assertIn("verdicts", sd_review.parse_findings(json.dumps({"findings": [], "verdicts": bad})).error)

    def test_the_schema_asks_for_verdicts(self) -> None:
        schema = sd_review.CODEX_OUTPUT_SCHEMA
        self.assertEqual(schema["required"], ["findings", "verdicts"])
        self.assertEqual(schema["properties"]["verdicts"]["items"]["properties"]["verdict"]["enum"], ["fixed", "open"])
        self.assertIn("verdicts", sd_review.URL_OUTPUT_CONTRACT)


class PriorVerdictReviewTests(RepeatFixture):
    def report(self) -> pathlib.Path:
        prior_head = self.commit("add", "src.py") and self.commit("src")
        self.commit("next")
        report = self.tmp / "aggregate.json"
        report.write_text(json.dumps({"scope": "branch", "subject": {"head": prior_head}, "findings": [
            *PRIOR, dict(finding(path="src.py", line=1, summary="advice"), disposition="advisory")]}))
        return report

    def test_a_carried_blocker_follows_its_file_through_a_rename(self) -> None:
        """Review of build/sd-2205 (bin/sd-review:2551): a carried blocker kept `src.py` after `git mv`, so the
        next fix review read no source for it. Its path and instances follow the rename to this head."""
        (self.root / "src.py").write_text("".join(f"line{n}\n" for n in range(1, 21)), encoding="utf-8")
        prior_head = self.commit("add", "src.py") and self.commit("src")
        report = self.tmp / "aggregate.json"
        report.write_text(json.dumps({"scope": "branch", "subject": {"head": prior_head}, "findings": [
            dict(PRIOR[0], instances=["src.py:10"])]}))
        self.commit("mv", "src.py", "moved.py") and self.commit("rename")
        prompts, _result, _stderr = self.prompts(scope="branch", resume_report=str(report))
        self.assertIn("Current source for prior finding moved.py", prompts[0])
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")},
                            default=sd_review.Completed(0, json.dumps({"findings": [], "verdicts": []}), ""))
        result = sd_review.review(self.root, namespace(scope="branch", resume_report=str(report)), runner,
                                  self.environment(), self.chatgpt_home())
        self.assertEqual([(row["path"], row["instances"]) for row in result["findings"] if row.get("prior_blocker")],
                         [("moved.py", ["moved.py:10"])])

    def carried_after(self, row: dict[str, Any], *steps: Any) -> tuple[str, list[Any]]:
        """The first prompt and the carried places for `row`, reported at the 20-line `src.py`, after `steps`."""
        (self.root / "src.py").write_text("".join(f"line{n}\n" for n in range(1, 21)), encoding="utf-8")
        prior_head = self.commit("add", "src.py") and self.commit("src")
        report = self.tmp / "aggregate.json"
        report.write_text(json.dumps({"scope": "branch", "subject": {"head": prior_head}, "findings": [row]}))
        for step in steps:
            step() if callable(step) else self.commit(*step)
        prompts, _result, _stderr = self.prompts(scope="branch", resume_report=str(report))
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")},
                            default=sd_review.Completed(0, json.dumps({"findings": [], "verdicts": []}), ""))
        result = sd_review.review(self.root, namespace(scope="branch", resume_report=str(report)), runner,
                                  self.environment(), self.chatgpt_home())
        return prompts[0], [(found["path"], found["instances"]) for found in result["findings"] if found.get("prior_blocker")]

    def rewrite(self, name: str) -> None:
        (self.root / name).write_text("".join(f"other{n}\n" for n in range(1, 21)), encoding="utf-8")

    def test_a_carried_blocker_follows_a_rename_that_a_rewrite_hides(self) -> None:
        """Round 3 of build/sd-2205 (bin/sd-review:1846): `git mv` in one commit and a rewrite in the next leave
        no rename between the two heads, so the blocker kept the deleted `src.py`. Each commit's renames chain."""
        prompt, places = self.carried_after(
            dict(PRIOR[0], instances=["src.py:10"]), ("mv", "src.py", "moved.py"), ("rename",),
            lambda: self.rewrite("moved.py"), ("add", "moved.py"), ("rewrite",), ("mv", "moved.py", "last.py"), ("again",))
        self.assertEqual(places, [("last.py", ["last.py:10"])])
        self.assertIn("Current source for prior finding last.py:\nother1\n", prompt)

    def test_a_rename_is_read_from_the_report_head_when_the_rows_own_head_is_unusable(self) -> None:
        """The same class: a row whose `prior_review.head` names no commit read no rename at all."""
        for head in ("unknown", "0" * 40):
            with self.subTest(head=head):
                _prompt, places = self.carried_after(dict(PRIOR[0], instances=["src.py:10"], prior_review={"head": head}),
                                                     ("mv", "src.py", "moved.py"), ("rename",))
                self.commit("mv", "moved.py", "src.py") and self.commit("back")
                self.assertEqual(places, [("moved.py", ["moved.py:10"])])

    def test_a_blocker_whose_file_is_gone_asks_where_its_code_went(self) -> None:
        """The same class: one commit deletes `src.py` and adds its rewrite, which git reads as no rename. The
        blocker stays at its stored place and the reader is told a missing file alone fixes nothing."""
        prompt, places = self.carried_after(
            dict(PRIOR[0], instances=["src.py:10"]), ("rm", "--quiet", "src.py"), lambda: self.rewrite("new.py"),
            ("add", "new.py"), ("replace",))
        self.assertEqual(places, [("src.py", ["src.py:10"])])
        self.assertIn("Current source for prior finding src.py:\n[file absent at current HEAD]", prompt)
        self.assertIn("A blocker whose file is absent is fixed only when evidence names where its code went", prompt)

    def test_a_name_reused_after_a_delete_does_not_carry_the_blocker_to_unrelated_code(self) -> None:
        """Round 4 of build/sd-2205 (bin/sd-review:1870): `src.py` moved to `a.py` and was deleted; unrelated
        `x.py` then took the name `a.py` and moved to `b.py`. The chain sent the blocker to `b.py`, and a fix
        review of unrelated code could clear it. A deleted file's blocker stays where it was, marked deleted."""
        def other() -> None:
            self.rewrite("x.py")
        prompt, places = self.carried_after(
            dict(PRIOR[0], instances=["src.py:10"]), ("mv", "src.py", "a.py"), ("rename",), ("rm", "--quiet", "a.py"),
            ("drop",), other, ("add", "x.py"), ("unrelated",), ("mv", "x.py", "a.py"), ("reuse",),
            ("mv", "a.py", "b.py"), ("move",))
        self.assertEqual(places, [("src.py", ["src.py:10"])])
        self.assertNotIn("Current source for prior finding b.py", prompt)
        self.assertIn("Current source for prior finding src.py:\n[file absent at current HEAD]", prompt)

    def test_a_file_deleted_and_added_again_is_not_read_as_the_blockers_source(self) -> None:
        """Round 4 of build/sd-2205 (bin/sd-review:1880, :1881): `src.py` was deleted, then a new `src.py`
        added. The prompt attached the new file as the blocker's source; it is now marked deleted, and the mark
        stays on the carried row so the next pass does not read the new file as the old one either."""
        def other() -> None:
            self.rewrite("src.py")
        prompt, places = self.carried_after(
            dict(PRIOR[0], instances=["src.py:10"]), ("rm", "--quiet", "src.py"), ("drop",), other,
            ("add", "src.py"), ("new",))
        self.assertEqual(places, [("src.py", ["src.py:10"])])
        self.assertNotIn("other1", prompt)
        self.assertIn("Current source for prior finding src.py:\n[file deleted since the prior review", prompt)

    def test_a_deleted_files_blocker_does_not_hide_the_source_of_another_at_its_name(self) -> None:
        """Round 5 of build/sd-2205 (bin/sd-review:2290): B1's `src.py` was deleted and a new `src.py` added,
        where a later pass found B2. One mark per name hid the only source for B2. The mark is per blocker:
        the file is attached while any blocker lives in it, and the prompt says whose it is not."""
        (self.root / "src.py").write_text("".join(f"line{n}\n" for n in range(1, 21)), encoding="utf-8")
        self.commit("add", "src.py") and self.commit("src")
        self.commit("rm", "--quiet", "src.py") and self.commit("drop")
        self.rewrite("src.py")
        prior_head = self.commit("add", "src.py") and self.commit("new")
        report = self.tmp / "aggregate.json"
        report.write_text(json.dumps({"scope": "branch", "subject": {"head": prior_head}, "findings": [
            dict(PRIOR[0], deleted_since_review=["src.py"]), dict(PRIOR[0], line=3, summary="Other defect")]}))
        self.commit("later")
        prompts, _result, _stderr = self.prompts(scope="branch", resume_report=str(report))
        self.assertIn("Current source for prior finding src.py:\nother1\n", prompts[0])
        self.assertIn("deleted_since_review", prompts[0].split("Prior findings:")[0])

    def test_a_resumed_review_asks_each_blocker_by_id_and_holds_it_without_a_fixed_verdict(self) -> None:
        report = self.report()
        prompts, _result, _stderr = self.prompts(scope="branch", resume_report=str(report))
        self.assertIn('"id": "B1"', prompts[0])
        self.assertNotIn('"id": "B2"', prompts[0])
        for verdicts, status in (([], "blocking"), ([verdict()], "clean")):
            with self.subTest(verdicts=verdicts):
                reply = sd_review.Completed(0, json.dumps({"findings": [], "verdicts": verdicts}), "")
                runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")}, default=reply)
                result = sd_review.review(self.root, namespace(scope="branch", resume_report=str(report)), runner,
                                          self.environment(), self.chatgpt_home())
                self.assertEqual(result["status"], status)


#: A receipt head `listed` takes for one on the reviewed branch.
ON_BRANCH = "c" * 40


def adjudicated(*rows: tuple[Any, ...], decision: str = "accepted", line: Any = 1, instances: Any = None) -> str:
    """One adjudication body at `ON_BRANCH`: each (path, summary, disposition[, line]) a blocking finding's answer."""
    return json.dumps({"decision": decision, "proposal": {"bindings": {"head": ON_BRANCH}, "findings": [
        {"response_disposition": answer, "reason": "a reason",
         "raw_finding": {"path": path, "line": at, "severity": "high", "summary": summary, "family": "correctness",
                         "instances": instances or [f"{path}:{at}" if type(at) is int else str(path)]}}  # its own place
        for path, summary, answer, *rest in rows for at in [(*rest, line)[0]]]}})


def ruled_body(summary: str = "defect", evidence: Any = REPRO, ruled: Any = True) -> str:
    """An adjudication body as a build's ruling round writes it: the judged evidence kept, the row marked."""
    body = json.loads(adjudicated(("src.py", summary, "rebutted")))
    body["proposal"]["findings"][0]["ruled"] = ruled
    body["proposal"]["findings"][0]["raw_finding"]["evidence"] = evidence
    return json.dumps(body)


def listed(*bodies: str) -> list[Any]:
    """The rebuttals in `bodies`, newest first, as the prompt lists them."""
    return sd_review.rebuttals([(body,) for body in bodies], {ON_BRANCH}.__contains__, lambda _head, _path: True)


def gate(findings: list[dict[str, Any]], *bodies: str) -> list[str]:
    """The disposition of each finding against the rebuttals in `bodies`, newest first."""
    return [record["disposition"] for record in sd_review.dispose(findings, "high", listed(*bodies))]


class RebuttalIdentityTests(unittest.TestCase):
    """Bundle round 6 of pack sd:9270: which rebuttal a finding repeats, and which one still holds."""

    def test_the_key_is_the_whole_summary_folded_not_its_display_cut(self) -> None:
        """A finding with no line is matched by its whole summary; one with a line by its place (sd:10918)."""
        shared = "x" * (sd_review.REBUTTED_SUMMARY_CHARS - 3)
        body = adjudicated(("src.py", shared + " first defect", "rebutted"), line=None)
        self.assertEqual(gate([row(summary=shared + " second defect", line=None)], body), ["blocking"])
        self.assertEqual(gate([row(summary=shared + " first defect", line=None)], body), ["advisory"])

    def test_a_repeat_in_other_words_at_the_rebutted_line_is_held(self) -> None:
        """sd:10918: readers raise a rebutted finding again in new words each round (build/sd-2204: "can
        change the live repository's shared Git configuration" became "can overwrite shared Git
        settings", both at :274), so the whole-summary key re-blocked it every round."""
        body = adjudicated(("src.py", "A test can change the shared Git configuration", "rebutted"), line=274)
        self.assertEqual(gate([row(summary="A passing test can overwrite shared Git settings", line=274)], body),
                         ["advisory"])
        self.assertEqual(gate([row(summary="A passing test can overwrite shared Git settings", line=275)], body),
                         ["blocking"], "a finding at another line is another finding")
        self.assertEqual(gate([row(summary="other words", line=274, path="other.py")], body), ["blocking"])

    def test_a_tested_rebuttal_holds_a_cited_repeat(self) -> None:
        """sd:10918: every reader cites evidence (sd:9867 asks for it), so a rebuttal that held only an
        uncited repeat held none. One whose test passed (sd:10893) holds a cited repeat; one with no
        test still holds only an uncited one."""
        body = json.loads(adjudicated(("src.py", "defect", "rebutted")))
        body["proposal"]["findings"][0]["test"] = ["python3", "-m", "unittest", "tests.test_x"]
        tested = json.dumps(body)
        self.assertEqual(gate([row(evidence=REPRO)], tested), ["advisory"])
        self.assertEqual(gate([row(evidence=REPRO)], adjudicated(("src.py", "defect", "rebutted"))), ["blocking"])
        for test in ([], "python3 t.py", [7], [""]):
            with self.subTest(test=test):
                body["proposal"]["findings"][0]["test"] = test
                self.assertEqual(gate([row(evidence=REPRO)], json.dumps(body)), ["blocking"])
        body["proposal"]["findings"][0]["test"] = ["python3", "t.py"]
        self.assertIn("Test: python3 t.py", sd_review.rebuttal_section(listed(json.dumps(body))))

    def test_case_spacing_and_the_repeat_note_do_not_change_the_key(self) -> None:
        body = adjudicated(("src.py", "Value  is wrong", "rebutted"), line=40)
        for summary in ("value is wrong", " VALUE is\nwrong ", f"Value is wrong {sd_review.REPEAT_NOTE}"):
            with self.subTest(summary=summary):
                self.assertEqual(gate([row(summary=summary, line=40)], body), ["advisory"])
        self.assertEqual(gate([row(summary="value is wrong", path="other.py")], body), ["blocking"])

    def test_the_newest_disposition_of_a_finding_wins(self) -> None:
        rebutted = adjudicated(("src.py", "defect", "rebutted"))
        parked = adjudicated(("src.py", "defect", "parked"))
        self.assertEqual(gate([row()], parked, rebutted), ["blocking"])
        self.assertEqual(gate([row()], rebutted, parked), ["advisory"])

    def test_a_ruling_rebuttal_holds_back_a_cited_repeat(self) -> None:
        """Review of sd:10286 (build_run.py:2199): a build's ruling round rejected the blocker and its
        evidence, so the cited repeat it clears posts as advice here too. An `sd-ship adjudicate` rebuttal
        still holds back only an uncited repeat.

        Class pass 4 (bin/sd-review:1791): only the evidence the ruling judged, as its rebuttal records it.
        A fresh reproduction at the same place blocks, as does any when the rebuttal records none."""
        plain = adjudicated(("src.py", "defect", "rebutted"))
        cited = row(evidence=REPRO)
        self.assertEqual(gate([cited], plain), ["blocking"])
        self.assertEqual(gate([cited], ruled_body()), ["advisory"])
        # Night triage (bin/sd-review:1801): the build clears only the exact evidence, so a repro that
        # differs in case or spacing is a fresh one here too: `-k ABC` is not `-k abc`.
        for fresh in (" " + REPRO.upper() + "\n", REPRO.upper(), REPRO.replace(" ", "  ", 1)):
            with self.subTest(fresh=fresh):
                self.assertEqual(gate([row(evidence=fresh)], ruled_body()), ["blocking"], "folded")
        self.assertEqual(gate([row()], ruled_body()), ["advisory"], "an uncited repeat holds as ever")
        self.assertEqual(gate([row(evidence="python3 t.py --empty: IndexError")], ruled_body()), ["blocking"],
                         "a fresh reproduction was held back")
        self.assertEqual(gate([row(summary="another defect", evidence=REPRO)], ruled_body()), ["blocking"])
        for evidence in (None, "", 7, [REPRO]):
            with self.subTest(recorded=evidence):
                self.assertEqual(gate([cited], ruled_body(evidence=evidence)), ["blocking"])
        for value in ("true", 1, None):
            with self.subTest(ruled=value):
                self.assertEqual(gate([cited], ruled_body(ruled=value)), ["blocking"], "only a literal true")

    def test_a_ruling_holds_no_uncited_repeat_of_another_saved_repro(self) -> None:
        """Review of sd:10286 (bin/sd-review:1823, :1827, :1842): an uncited repeat carries the repro the
        prior review saved at its place. A ruling judged one repro; another saved there blocks, keeps its
        evidence, and the prior blocker is no rebutted place. The saved ruled repro still holds."""
        fresh = "python3 t.py --empty: IndexError"
        prior = [dict(row(evidence=fresh), disposition="blocking")]
        disposed = sd_review.dispose([row()], "high", listed(ruled_body()), prior=prior)
        self.assertEqual([r["disposition"] for r in disposed], ["blocking"])
        # pack sd:10087: the prior blocker is carried by id, not copied into a repeat: it keeps its evidence.
        kept = sd_review.carried(prior, [], listed(ruled_body()))
        self.assertEqual([(r["disposition"], r["evidence"]) for r in kept], [("blocking", fresh)])
        same = [dict(row(evidence=REPRO), disposition="blocking")]
        disposed = sd_review.dispose([row()], "high", listed(ruled_body()), prior=same)
        self.assertEqual([r["disposition"] for r in disposed], ["advisory"])
        self.assertEqual(sd_review.carried(same, [], listed(ruled_body())), [], "the ruled repro is rebutted")

    def test_a_ruling_holds_no_uncited_repeat_beside_another_saved_blocker(self) -> None:
        """Class pass 7 (bin/sd-review:1820, :1842): the saved repro was found by the prior blocker's words,
        so one in other words at the ruled place let the uncited repeat through. A ruling holds an uncited
        repeat only when every blocker the prior review saved at or beside its place is the claim it judged:
        the ruled repro, or the ruled words with none. Any other saved there blocks, fail closed."""
        fresh = "python3 t.py --empty: IndexError"
        def held(*saved: dict[str, Any]) -> list[str]:
            prior = [dict(old, disposition="blocking") for old in saved]
            return [r["disposition"] for r in sd_review.dispose([row()], "high", listed(ruled_body()), prior=prior)]
        for name, old in (("other words, another repro", row(summary="crash on empty input", evidence=fresh)),
                          ("other words, no repro", row(summary="crash on empty input")),
                          ("other words, a line beside", row(summary="crash on empty input", evidence=fresh,
                                                             line=1 + sd_review.SHIFT_LINES)),
                          ("other words, no line", row(summary="crash on empty input", evidence=fresh, line=None)),
                          ("the ruled words, another repro", row(evidence=fresh))):
            with self.subTest(name):
                self.assertEqual(held(old), ["blocking"])
        for name, old in (("the ruled repro in other words", row(summary="crash on empty input", evidence=REPRO)),
                          ("the ruled words, no repro", row()), ("another file", row(path="other.py", evidence=fresh)),
                          ("a line far off", row(summary="crash", evidence=fresh, line=2 + sd_review.SHIFT_LINES))):
            with self.subTest(name):
                self.assertEqual(held(old), ["advisory"])
        self.assertEqual(held(), ["advisory"])

    def test_every_ruling_rebuttal_holds_past_the_prompts_cap(self) -> None:
        """Class pass 4 (build_run.py:2199): a build clears each exact repeat its ruling round ruled, however
        many; the prompt's cap listed ten rebuttals, so the lane blocked the eleventh. A ruling's hold reads
        the recorded evidence, not what the reviewer was shown, so the cap does not bound it. The cap still
        bounds the prompt, and an `sd-ship adjudicate` rebuttal past it."""
        count = sd_review.REBUTTED_LINES + 3
        bodies = [ruled_body(summary=f"claim {index}") for index in range(count)]
        self.assertEqual(gate([row(summary=f"claim {index}", evidence=REPRO) for index in range(count)], *bodies),
                         ["advisory"] * count)
        self.assertEqual(gate([row(summary=f"claim {index}") for index in range(count)], *bodies),
                         ["advisory"] * count, "an uncited repeat of a ruling past the cap")
        self.assertEqual(sd_review.rebuttal_section(listed(*bodies)).count("\n- "), sd_review.REBUTTED_LINES)
        changed = sd_review.rebuttals([(bodies[0],)], {ON_BRANCH}.__contains__, lambda _head, _path: False)
        self.assertEqual(sd_review.dispose([row(summary="claim 0", evidence=REPRO)], "high", changed)[0]["disposition"],
                         "blocking", "a ruling whose file changed since still held")

    def test_only_an_accepted_adjudication_rebuts(self) -> None:
        for decision in ("rejected", "", "proposed"):
            with self.subTest(decision=decision):
                self.assertEqual(gate([row()], adjudicated(("src.py", "defect", "rebutted"), decision=decision)),
                                 ["blocking"])

    def test_the_gate_matches_only_the_rebuttals_the_prompt_shows(self) -> None:
        """Round 7: one list feeds both; an 11th rebuttal the reviewer never saw does not downgrade."""
        bodies = [adjudicated(("src.py", f"claim {index}", "rebutted")) for index in range(sd_review.REBUTTED_LINES + 3)]
        oldest, shown = f"claim {sd_review.REBUTTED_LINES + 2}", f"claim {sd_review.REBUTTED_LINES - 1}"
        self.assertEqual(gate([row(summary=oldest), row(summary=shown)], *bodies), ["blocking", "advisory"])
        section = sd_review.rebuttal_section(listed(*bodies))
        self.assertEqual(section.count("\n- "), sd_review.REBUTTED_LINES)
        self.assertNotIn(oldest, section)

    def test_a_rebuttal_lapses_when_an_instance_file_changes(self) -> None:
        """Review of sd:9769: a later commit can turn an instance, not the primary file, into the branch's own
        regression; a rebuttal holds only while every file the finding names is unchanged."""
        body = adjudicated(("bin/tool.py", "defect", "rebutted"), instances=["bin/tool.py:1", "src.py:1"])
        seen = sd_review.rebuttals([(body,)], {ON_BRANCH}.__contains__, lambda _head, path: path != "src.py")
        self.assertEqual([entry.listed for entry in seen], [False], "a changed instance file kept the rebuttal")

    def test_a_finding_that_names_a_new_instance_file_is_not_the_rebutted_one(self) -> None:
        """Review of sd:9769: a fix that spreads the defect to `new.py` keeps the primary place and summary;
        the rebuttal named no `new.py`, so it is a new finding and blocks."""
        body = adjudicated(("src.py", "defect", "rebutted"))  # its instance: src.py:1
        self.assertEqual(gate([row(instances=["src.py:1", "new.py:4"])], body), ["blocking"])
        self.assertEqual(gate([row(instances=["src.py:1"])], body), ["advisory"])
        # Review of sd:9769: a new occurrence in a file the rebuttal named is still not adjudicated.
        self.assertEqual(gate([row(instances=["src.py:1", "src.py:60"])], body), ["blocking"])

    def test_a_test_holds_a_cited_repeat_only_in_the_rebuttals_words(self) -> None:
        """Review of build/sd-2233 (bin/sd-review:1945): the place match for a reworded repeat and the test's
        hold of a cited one composed, so a different defect at the tested line, cited, posted as advice.
        A test holds a cited repeat only in the words it was recorded with; a reworded one only uncited."""
        body = json.loads(adjudicated(("src.py", "defect", "rebutted")))
        body["proposal"]["findings"][0]["test"] = ["python3", "t.py"]
        tested = json.dumps(body)
        self.assertEqual(gate([row(summary="another defect", evidence=REPRO)], tested), ["blocking"])
        self.assertEqual(gate([row(summary="another defect")], tested), ["advisory"])
        self.assertEqual(gate([row(evidence=REPRO)], tested), ["advisory"])

    def test_a_test_clears_no_blocker_a_reader_holds_open(self) -> None:
        """Review of build/sd-2233 (bin/sd-review:2031): `carried` decides a prior blocker by verdict
        (pack sd:10087). A reader's `open` with evidence keeps it whatever test its rebuttal names, and a
        prior blocker in other words at a rebutted line is no repeat: the adjudication recorded its words."""
        body = json.loads(adjudicated(("src.py", "defect", "rebutted")))
        body["proposal"]["findings"][0]["test"] = ["python3", "t.py"]
        seen = listed(json.dumps(body))
        prior = [dict(row(evidence=REPRO), disposition="blocking")]
        opened = sd_review.Outcome("codex", sd_review.FINDINGS, (), "", (), (), verdicts=(
            {"id": "B1", "verdict": "open", "evidence": "python3 t.py --other: Traceback"},))
        self.assertEqual([r["disposition"] for r in sd_review.carried(prior, [opened], seen)], ["blocking"])
        self.assertEqual(sd_review.carried(prior, [], seen), [], "an unanswered rebutted blocker still holds")
        other = [dict(row(summary="another defect", evidence=REPRO), disposition="blocking")]
        self.assertEqual([r["disposition"] for r in sd_review.carried(other, [], seen)], ["blocking"])

    def test_a_line_follows_a_zero_context_diff_or_lapses(self) -> None:
        """sd:10918: `followed_line` moves a rebutted line past hunks outside `SHIFT_LINES` of it, and lapses
        it on a hunk inside, no diff, or a diff with no hunk (binary, mode change)."""
        follow, near = sd_review.followed_line, sd_review.SHIFT_LINES
        header = "diff --git a/f b/f\n--- a/f\n+++ b/f\n"
        for name, diff, want in (
                ("no change", "", 50), ("git failed", None, None),
                ("binary", "diff --git a/f b/f\nBinary files a/f and b/f differ", None),
                ("mode only", "diff --git a/f b/f\nold mode 100644\nnew mode 100755", None),
                ("two inserted at the top", header + "@@ -0,0 +1,2 @@\n+a\n+b", 52),
                ("three removed above", header + "@@ -10,3 +9,0 @@\n-a\n-b\n-c", 47),
                ("one replaced by four above", header + "@@ -10 +10,4 @@\n-a\n+a\n+b\n+c\n+d", 53),
                ("an edit below", header + f"@@ -{50 + near + 1} +{50 + near + 1} @@\n-a\n+b", 50),
                ("an edit at the line", header + "@@ -50 +50 @@\n-a\n+b", None),
                ("an edit just inside the reach", header + f"@@ -{50 - near} +{50 - near} @@\n-a\n+b", None),
                ("an insertion just inside the reach", header + f"@@ -{50 + near},0 +{50 + near + 1} @@\n+a", None),
                ("a content line that looks like a hunk", header + "@@ -1 +1 @@\n-@@ -50 +50 @@\n+x", 50),
                ("the file deleted", header + "@@ -1,80 +0,0 @@\n-a", None)):
            with self.subTest(name):
                self.assertEqual(follow(diff, 50), want)

    def test_the_lanes_own_blockers_are_never_rebutted(self) -> None:
        parsed = sd_review.oversized_findings()
        body = adjudicated(*((finding["path"], finding["summary"], "rebutted") for finding in parsed.findings))
        self.assertEqual(set(gate(list(parsed.findings), body)), {"blocking"})
        self.assertEqual(sd_review.rebuttal_section(listed(body)), "")

    def test_a_rebuttal_holds_only_at_its_own_line(self) -> None:
        """Rounds 8 and 11 (operator ruling): a rebuttal at line 40 clears the same summary only at line 40."""
        body = adjudicated(("src.py", "defect", "rebutted"), line=40)
        for line, status in ((40, "advisory"), (41, "blocking"), (39, "blocking"), (50, "blocking"), (400, "blocking"),
                             (None, "blocking"), ("40", "blocking"), (True, "blocking")):
            with self.subTest(line=line):
                self.assertEqual(gate([row(line=line)], body), [status])
        for line, status in ((None, "advisory"), (40, "blocking")):
            with self.subTest(rebuttal_line=None, line=line):
                self.assertEqual(gate([row(line=line)], adjudicated(("src.py", "defect", "rebutted"), line=None)),
                                 [status])

    def test_the_newest_disposition_wins_only_at_its_own_line(self) -> None:
        """Round 8: a parked finding at line 3 leaves the rebuttal of the same summary at line 40 standing."""
        rebutted = adjudicated(("src.py", "defect", "rebutted"), line=40)
        parked = adjudicated(("src.py", "defect", "parked"), line=3)
        self.assertEqual(gate([row(line=40), row(line=3)], parked, rebutted), ["advisory", "blocking"])
        self.assertEqual(len([entry for entry in listed(parked, rebutted) if entry.listed]), 1)

    def test_the_path_is_compared_exactly(self) -> None:
        """Round 10: git prints a path one way; another case or spacing names another file."""
        body = adjudicated(("src/Tool.py", "defect", "rebutted"))
        for path, status in (("src/Tool.py", "advisory"), ("src/tool.py", "blocking"), ("SRC/Tool.py", "blocking"),
                             ("src/Tool.py ", "blocking"), (" src/Tool.py", "blocking")):
            with self.subTest(path=path):
                self.assertEqual(gate([row(path=path)], body), [status])

    def test_a_moved_finding_never_lands_on_a_rebuttal(self) -> None:
        """Round 11, the reviewer's case: `defect` was parked at line 48 and rebutted at line 35; the code moves
        and the parked one now sits at 40. It still blocks; only line 35 itself clears."""
        for rows in ((("src.py", "defect", "parked", 48), ("src.py", "defect", "rebutted", 35)),
                     (("src.py", "defect", "rebutted", 35), ("src.py", "defect", "parked", 48))):
            with self.subTest(first=rows[0][2]):
                body = adjudicated(*rows)
                self.assertEqual(gate([row(line=line) for line in (40, 48, 36, 34, 35)], body),
                                 ["blocking", "blocking", "blocking", "blocking", "advisory"])
        rebutted, parked = (adjudicated(("src.py", "defect", answer, 5)) for answer in ("rebutted", "parked"))
        self.assertEqual(gate([row(line=5)], parked, rebutted), ["blocking"])
        self.assertEqual(gate([row(line=5)], rebutted, parked), ["advisory"])


class RebuttalScopeTests(RepeatFixture):
    """Round 7: the gate holds only the rebuttals the reviewer was shown."""

    def review_answering(self, *findings: dict[str, Any]) -> dict[str, tuple[str, str]]:
        runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", ""),
                             # A located finding keeps the answer: one citing no reviewed path is voided (sd:1406).
                             "codex": sd_review.Completed(0, json.dumps({"findings": [*findings, row()]}), "")},
                            default=sd_review.Completed(0, json.dumps({"findings": []}), ""))
        result = sd_review.review(self.root, namespace(), runner, self.environment(), self.chatgpt_home())
        # "blocking" is what the repeat rule leaves at the floor: these findings cite no repro, so sd:9867 posts
        # them as advisory with NO_REPRO_NOTE; ReproRuleTests pins that rule.
        return {r["summary"].replace(" " + sd_review.REPEAT_NOTE, "").replace(" " + sd_review.NO_REPRO_NOTE, ""):
                ("blocking" if r["disposition"] == "blocking" or r["summary"].endswith(sd_review.NO_REPRO_NOTE)
                 else r["disposition"], r["path"])
                for r in result["findings"] if r["backend"] == "codex"}

    def repeat(self, summary: str) -> dict[str, Any]:
        return row(path="bin/tool.py", line=3, summary=summary)

    def test_a_receipt_a_minute_after_the_heads_commit_rebuts(self) -> None:
        """Round 9: rebut, then the lane retries at the same head; the receipt is newer than the head's commit."""
        self.adjudication(("lane claim", "rebutted", "git 2.54 matches it literally"), days=-1 / 1440)
        self.assertIn("- bin/tool.py:3: lane claim Rebuttal:", self.prompts()[0][0])
        self.assertEqual(self.review_answering(self.repeat("lane claim"))["lane claim"],
                         ("advisory", "bin/tool.py"))
        for scope in ("worktree", "branch"):
            with self.subTest(scope=scope):
                seen = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, scope), None,
                                                   self.environment())
                self.assertEqual([entry.text for entry in seen], ["lane claim"])

    def test_a_rebuttal_older_than_the_bound_does_not_downgrade(self) -> None:
        """A reused branch name does not inherit a merged branch's rebuttals."""
        days = sd_review.BLOCKER_DAYS
        self.adjudication(("an old claim", "rebutted", "x"), days=days + 1, key="old", line=30)
        self.adjudication(("a recent claim", "rebutted", "x"), days=days - 1, key="recent")
        prompt = self.prompts()[0][0]
        self.assertIn("a recent claim", prompt)
        self.assertNotIn("an old claim", prompt)
        rows = self.review_answering(dict(self.repeat("an old claim"), line=30), self.repeat("a recent claim"))
        self.assertEqual((rows["an old claim"][0], rows["a recent claim"][0]), ("blocking", "advisory"))

    def test_only_a_receipt_at_a_head_on_this_branch_rebuts(self) -> None:
        """Round 8: a reused branch name inherits nothing; the receipt's head must be in base..head."""
        self.commit("checkout", "--quiet", "-b", "elsewhere", self.base)
        abandoned = self.commit("an abandoned branch of the same name")
        self.commit("checkout", "--quiet", BRANCH)
        off = {"on another branch": abandoned, "unknown": "a" * 40, "no head": None,
               "short": self.head[:12], "not text": [self.head], "a ref": "HEAD"}
        for index, (claim, head) in enumerate(off.items()):  # each at its own line (sd:10918)
            self.adjudication((claim, "rebutted", "x"), head=head, key=f"off{index}", line=10 + index)
        self.adjudication(("at the base", "rebutted", "x"), head=self.base, key="base")
        self.adjudication(("on this branch", "rebutted", "x"), head=self.first, key="on")
        prompt = self.prompts()[0][0]
        self.assertIn("on this branch", prompt)
        self.assertIn("at the base", prompt)
        for claim in off:
            self.assertNotIn(f": {claim} Rebuttal", prompt)
        rows = self.review_answering(*(dict(self.repeat(claim), line=10 + index) for index, claim in enumerate(off)),
                                     self.repeat("on this branch"), self.repeat("at the base"))
        self.assertEqual({claim: rows[claim][0] for claim in off}, dict.fromkeys(off, "blocking"))
        self.assertEqual((rows["on this branch"][0], rows["at the base"][0]), ("advisory", "advisory"))
        for scope in ("worktree", "branch"):
            with self.subTest(scope=scope):
                seen = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, scope), None,
                                                   self.environment())
                self.assertEqual([entry.text for entry in seen], ["on this branch", "at the base"])

    def test_a_fix_verification_reads_the_receipts_at_its_base(self) -> None:
        """Round 10: sd-ship verifies a fix with `--base <the previous reviewed head>`; that head's receipt
        is the one the reviewer must see."""
        self.adjudication(("lane claim", "rebutted", "x"), head=self.first)
        subject = sd_review.resolve_subject(self.root, "branch", base=self.first)
        seen = sd_review.rebutted_findings(self.root, subject, None, self.environment())
        self.assertEqual([entry.text for entry in seen], ["lane claim"])

    def test_a_review_holds_a_repeat_back_only_near_its_line(self) -> None:
        """Round 8: the receipt's finding sits at bin/tool.py:3; the same summary far off is another defect."""
        self.adjudication(("lane claim", "rebutted", "x"))
        far = dict(self.repeat("lane claim"), line=4)
        self.assertEqual(self.review_answering(far)["lane claim"], ("blocking", "bin/tool.py"))
        self.assertEqual(self.review_answering(self.repeat("lane claim"))["lane claim"], ("advisory", "bin/tool.py"))

    def test_a_rebuttal_holds_only_while_its_file_is_unchanged(self) -> None:
        """Round 12, hold on doubt: a change to the rebutted file since the receipt's head, committed or not,
        or a diff git cannot run, makes the repeat block; a change to another file does not."""
        self.adjudication(("lane claim", "rebutted", "x"), head=self.first)
        (self.root / "other.py").write_text("y = 2\n", encoding="utf-8")
        self.commit("add", "other.py")
        self.commit("another file")
        self.assertEqual(self.review_answering(self.repeat("lane claim"))["lane claim"][0], "advisory")
        (self.root / "bin").mkdir()
        (self.root / "bin/tool.py").write_text("check = False\n", encoding="utf-8")
        self.assertEqual(self.review_answering(self.repeat("lane claim"))["lane claim"][0], "blocking")
        self.assertNotIn("lane claim Rebuttal", self.prompts()[0][0])
        self.commit("add", "bin/tool.py")
        self.commit("the cited check is gone")
        for scope in ("worktree", "branch"):
            with self.subTest(scope=scope):
                seen = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, scope), None,
                                                   self.environment())
                self.assertEqual([entry.text for entry in seen if entry.listed], [])

    def test_a_rebuttal_follows_its_line_past_an_edit_elsewhere_in_its_file(self) -> None:
        """sd:10918: a fix commit to another part of the rebutted file lapsed every rebuttal in it, so the
        next round re-blocked them. The rebuttal now moves with its line; an edit within `SHIFT_LINES`
        of it, committed or not, still lapses it."""
        tool = self.root / "bin" / "tool.py"
        tool.parent.mkdir()
        lines = [f"v{index} = {index}\n" for index in range(1, 41)]
        tool.write_text("".join(lines), encoding="utf-8")
        self.commit("add", "bin/tool.py")
        receipt = self.commit("the rebutted code")
        self.adjudication(("lane claim", "rebutted", "x"), head=receipt, line=20)
        tool.write_text("".join(["# one\n", "# two\n", *lines[:-1], "v40 = 0\n"]), encoding="utf-8")
        self.commit("add", "bin/tool.py")
        self.commit("a fix far from the rebutted line")
        moved = dict(self.repeat("the same claim in other words"), line=22)
        for scope in ("worktree", "branch"):
            with self.subTest(scope=scope):
                seen = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, scope), None,
                                                   self.environment())
                self.assertEqual([(entry.line, entry.listed, entry.instances) for entry in seen],
                                 [(22, True, ("bin/tool.py:22",))])
        self.assertEqual(self.review_answering(moved)["the same claim in other words"][0], "advisory")
        self.assertEqual(self.review_answering(dict(moved, line=20))["the same claim in other words"][0], "blocking")
        text = tool.read_text(encoding="utf-8").replace("v21 = 21", "v21 = 0")  # beside the rebutted line
        tool.write_text(text, encoding="utf-8")
        seen = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, "worktree"), None,
                                           self.environment())
        self.assertEqual([entry.listed for entry in seen], [False], "an edit beside the line kept the rebuttal")

    def test_a_ruling_lapses_on_any_later_change_of_the_branch(self) -> None:
        """Review of sd:10286 (bin/sd-review:919): a ruling judged the code at its head; a later commit to a
        file it does not name (a new caller) can make the crash reachable, yet the ruling held its cited
        repeat. A ruling holds only while the branch has no change of its own since its head."""
        self.adjudication(("lane claim", "rebutted", "x"), ruled="the repro")
        def ruled(scope: str) -> list[Any]:
            seen = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, scope), None,
                                               self.environment())
            return [entry.ruled for entry in seen]
        self.assertEqual(ruled("branch"), ["the repro"])
        (self.root / "caller.py").write_text("import tool\n", encoding="utf-8")
        self.assertEqual(ruled("worktree"), [None], "an uncommitted caller kept the ruling")
        self.commit("add", "caller.py")
        self.commit("a new caller")
        for scope in ("worktree", "branch"):
            with self.subTest(scope=scope):
                self.assertEqual(ruled(scope), [None], "a later commit kept the ruling")

    def test_a_ruling_lapses_on_a_catch_up_merge(self) -> None:
        """Class pass 5 on sd:10286 (bin/sd-review:872): the base's own commits can add the caller too. A
        ruling holds only at the tree it judged, and a lapsed one is no listed rebuttal either."""
        self.adjudication(("lane claim", "rebutted", "x"), ruled="the repro")
        self.commit("checkout", "--quiet", "-")
        (self.root / "caller.py").write_text("import tool\n", encoding="utf-8")
        self.commit("add", "caller.py")
        self.commit("a caller on the base")
        self.commit("checkout", "--quiet", BRANCH)
        self.commit("merge", "--quiet", "--no-edit", "-")
        for scope in ("worktree", "branch"):
            with self.subTest(scope=scope):
                seen = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, scope), None,
                                                   self.environment())
                self.assertEqual([(entry.ruled, entry.listed) for entry in seen], [(None, False)])

    def test_a_diff_git_cannot_run_holds_the_rebuttal_back(self) -> None:
        self.adjudication(("lane claim", "rebutted", "x"))
        real = sd_review.sd_lib.git_output
        with unittest.mock.patch.object(
                sd_review.sd_lib, "git_output",
                side_effect=lambda args, root: None if "diff" in args else real(args, root)):
            seen = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, "worktree"), None,
                                               self.environment())
        self.assertEqual([(entry.text, entry.listed) for entry in seen], [("lane claim", False)])

    def test_a_branch_git_cannot_list_is_unknown_not_empty(self) -> None:
        self.adjudication(("lane claim", "rebutted", "x"))
        real = sd_review.sd_lib.git_output
        stderr = io.StringIO()
        with unittest.mock.patch.object(
                sd_review.sd_lib, "git_output",
                side_effect=lambda args, root: None if args[0] == "rev-list" else real(args, root)), \
                contextlib.redirect_stderr(stderr):
            seen = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, "worktree"), None,
                                               self.environment())
        self.assertEqual(seen, [])
        self.assertIn("sd-review: reviewing without the rebutted findings: ValueError:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
