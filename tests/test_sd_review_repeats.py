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
                     line: Any = 3) -> None:
        """One `sd-ship adjudicate` record: each (summary, disposition, reason) a blocking finding's answer,
        at the reviewed head unless `head` names another."""
        findings = [{"index": index, "finding_digest": "0" * 64, "response_disposition": answer, "reason": reason,
                     "raw_finding": {"path": "bin/tool.py", "line": line, "severity": "high", "instances": ["src.py:1"], "summary": summary,
                                     "family": "correctness", "disposition": "blocking"}}
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


def row(severity: str = "high", **extra: Any) -> dict[str, Any]:
    return {"path": "src.py", "line": 1, "severity": severity, "summary": "defect", "family": "correctness"} | extra


#: Evidence that does not count: absent, empty, blank, or not a string.
UNCITED = (None, "", "   \n\t", 0, 7, [], ["cmd"], {}, {"command": "x", "output": "y"}, True)
CITED = ("python3 src.py: Traceback", "src.py:1: x = 1")


def finding(evidence: Any = None, **extra: Any) -> dict[str, Any]:
    return row(**extra) if evidence is None else row(evidence=evidence, **extra)


class RepeatRuleTests(unittest.TestCase):
    """Operator ruling on pack sd:9270: evidence is asked only of a repeat of a rebuttal."""

    def setUp(self) -> None:
        self.REBUTTED = listed(adjudicated(("src.py", "defect", "rebutted")))

    def test_an_uncited_first_time_blocker_stays_blocking(self) -> None:
        for evidence in UNCITED:
            for rebutted in ([], listed(adjudicated(("src.py", "another claim", "rebutted"),
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
        self.assertIn("One raised again without it blocks nothing.", section)
        self.assertEqual(sd_review.rebuttal_section([]), "")

    def test_one_finding_from_two_readers_is_one_row_whatever_each_cites(self) -> None:
        bare, cited, other = row(), row(evidence="src.py:1: x = 1"), row(evidence="python3 src.py: wrong")
        kept = sd_review.sd_lib.distinct_findings([dict(bare, backend="codex"), dict(cited, backend="second"),
                                                   dict(other, backend="third")])
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0]["backend"], kept[0]["evidence"]), ("codex", "src.py:1: x = 1"))
        two = sd_review.sd_lib.distinct_findings([dict(row(), backend="codex"), dict(row(summary="other"), backend="x")])
        self.assertEqual(len(two), 2)


class RepeatReviewTests(RepeatFixture):
    def test_no_reviewer_is_told_to_cite_every_blocker(self) -> None:
        prompts, _result, _stderr = self.prompts()
        self.assertEqual(len(prompts), 2)
        for text in [*prompts, sd_review.URL_OUTPUT_CONTRACT]:
            self.assertIn("empty unless you raise again a finding listed as rebutted", text)
            self.assertNotIn("posted as advisory", text)
            self.assertNotIn("run a check before", text)
        claude = sd_review.claude_argv(self.tmp, "claude")
        self.assertIn('"evidence"', claude[claude.index("--json-schema") + 1])

    def test_a_review_holds_only_an_uncited_repeat_of_a_rebuttal_back(self) -> None:
        self.adjudication(("lane claim", "rebutted", "git 2.54 matches it literally"))
        fresh = row(summary="a first-time claim")
        for evidence, status in ((None, "advisory"), ("bin/tool.py:3: glob = True", "blocking")):
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


#: A receipt head `listed` takes for one on the reviewed branch.
ON_BRANCH = "c" * 40


def adjudicated(*rows: tuple[Any, ...], decision: str = "accepted", line: Any = 1) -> str:
    """One adjudication body at `ON_BRANCH`: each (path, summary, disposition[, line]) a blocking finding's answer."""
    return json.dumps({"decision": decision, "proposal": {"bindings": {"head": ON_BRANCH}, "findings": [
        {"response_disposition": answer, "reason": "a reason",
         "raw_finding": {"path": path, "line": (*rest, line)[0], "severity": "high", "instances": ["src.py:1"], "summary": summary,
                         "family": "correctness"}}
        for path, summary, answer, *rest in rows]}})


def listed(*bodies: str) -> list[Any]:
    """The rebuttals in `bodies`, newest first, as the prompt lists them."""
    return sd_review.rebuttals([(body,) for body in bodies], {ON_BRANCH}.__contains__, lambda _head, _path: True)


def gate(findings: list[dict[str, Any]], *bodies: str) -> list[str]:
    """The disposition of each finding against the rebuttals in `bodies`, newest first."""
    return [record["disposition"] for record in sd_review.dispose(findings, "high", listed(*bodies))]


class RebuttalIdentityTests(unittest.TestCase):
    """Bundle round 6 of pack sd:9270: which rebuttal a finding repeats, and which one still holds."""

    def test_the_key_is_the_whole_summary_folded_not_its_display_cut(self) -> None:
        shared = "x" * (sd_review.REBUTTED_SUMMARY_CHARS - 3)
        body = adjudicated(("src.py", shared + " first defect", "rebutted"))
        self.assertEqual(gate([row(summary=shared + " second defect")], body), ["blocking"])
        self.assertEqual(gate([row(summary=shared + " first defect")], body), ["advisory"])

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
        return {r["summary"].replace(" " + sd_review.REPEAT_NOTE, ""): (r["disposition"], r["path"])
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
        self.adjudication(("an old claim", "rebutted", "x"), days=days + 1, key="old")
        self.adjudication(("a recent claim", "rebutted", "x"), days=days - 1, key="recent")
        prompt = self.prompts()[0][0]
        self.assertIn("a recent claim", prompt)
        self.assertNotIn("an old claim", prompt)
        rows = self.review_answering(self.repeat("an old claim"), self.repeat("a recent claim"))
        self.assertEqual((rows["an old claim"][0], rows["a recent claim"][0]), ("blocking", "advisory"))

    def test_only_a_receipt_at_a_head_on_this_branch_rebuts(self) -> None:
        """Round 8: a reused branch name inherits nothing; the receipt's head must be in base..head."""
        self.commit("checkout", "--quiet", "-b", "elsewhere", self.base)
        abandoned = self.commit("an abandoned branch of the same name")
        self.commit("checkout", "--quiet", BRANCH)
        off = {"on another branch": abandoned, "unknown": "a" * 40, "no head": None,
               "short": self.head[:12], "not text": [self.head], "a ref": "HEAD"}
        for index, (claim, head) in enumerate(off.items()):
            self.adjudication((claim, "rebutted", "x"), head=head, key=f"off{index}")
        self.adjudication(("at the base", "rebutted", "x"), head=self.base, key="base")
        self.adjudication(("on this branch", "rebutted", "x"), head=self.first, key="on")
        prompt = self.prompts()[0][0]
        self.assertIn("on this branch", prompt)
        self.assertIn("at the base", prompt)
        for claim in off:
            self.assertNotIn(f": {claim} Rebuttal", prompt)
        rows = self.review_answering(*(self.repeat(claim) for claim in [*off, "on this branch", "at the base"]))
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
