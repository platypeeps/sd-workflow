"""pack sd:9270. Reviewers stop repeating settled findings, and a blocker cites a check it ran.

Evidence, 2026-11-17: on system sd:9230 an out-of-threat-model finding came
back in review round 5 and again in the lane; on a system build branch a
blocker claimed `ls-tree` reads an id as a glob, and git 2.54 matches it
literally. So the prompt carries the repository's threat model and this
branch's rebutted findings, and a blocker without evidence posts as advice.

Every `CLAUDE.local.md` here is synthetic, and the adjudication rows go into a
fixture database under a scratch `HOME`; the operator's files are never read.
"""

from __future__ import annotations

import contextlib
import io
import json
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


class RepeatFixture(ReviewFixture):
    def setUp(self) -> None:
        super().setUp()
        self.library = sd_review.sd_lib.import_sd_db().module
        self.root = self.make_repo()
        subprocess.run(["git", "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git"],
                       cwd=str(self.root), check=True, capture_output=True, env=clean_environment())
        stamp = ANCHOR.isoformat()
        subprocess.run(["git", "commit", "--quiet", "--allow-empty", "-m", "anchor"], cwd=str(self.root),
                       check=True, capture_output=True,
                       env=clean_environment(GIT_COMMITTER_DATE=stamp, GIT_AUTHOR_DATE=stamp))
        (self.root / "src.py").write_text("x = 1\n", encoding="utf-8")

    def database(self) -> pathlib.Path:
        path = self.library.default_path(self.registry_home)
        if not path.exists():
            self.library.initialise(home=self.registry_home)
        return path

    def write_row(self, key: str, body: Any, days: float = 0.5) -> None:
        stamp = (ANCHOR - timedelta(days=days)).isoformat(timespec="seconds")
        with contextlib.closing(self.library.connect(self.database())) as connection:
            self.library.writes.record_state(connection, "checkpoint", key=key, body=body, timestamp=stamp)

    def adjudication(self, *rows: tuple[str, str, str], branch: str = "main", repository: str = REPOSITORY,
                     head: str = "a" * 40, days: float = 0.5, prefix: str = "ship-adjudication:", key: str = "k") -> None:
        """One `sd-ship adjudicate` record: each (summary, disposition, reason) a blocking finding's answer."""
        findings = [{"index": index, "finding_digest": "0" * 64, "response_disposition": answer, "reason": reason,
                     "raw_finding": {"path": "bin/tool.py", "line": 3, "severity": "high", "summary": summary,
                                     "family": "correctness", "disposition": "blocking"}}
                    for index, (summary, answer, reason) in enumerate(rows, 1)]
        bindings = {"repository": repository, "branch": branch, "head": head}
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
                          ("a parked risk", "parked", "accepted for now"), head="a" * 40, key="item")
        self.adjudication(("lane claim two", "rebutted", "out of the threat model"), head="b" * 40, key="item", days=0.4)
        self.adjudication(("local claim", "rebutted", "the test covers it"), prefix="ship-adjudication-no-item:",
                          key="local")
        self.adjudication(("another branch", "rebutted", "x"), branch="topic", key="other-branch")
        self.adjudication(("another repository", "rebutted", "x"), repository="fixture/elsewhere", key="other-repo")
        self.adjudication(("after the head", "rebutted", "x"), days=-1, key="late")
        prompts, _result, stderr = self.prompts()
        for prompt in prompts:
            self.assertIn(sd_review.REBUTTED_HEADER, prompt)
            self.assertIn("- bin/tool.py: lane claim one Rebuttal: git 2.54 matches it literally", prompt)
            self.assertIn("- bin/tool.py: lane claim two Rebuttal: out of the threat model", prompt)
            self.assertIn("- bin/tool.py: local claim Rebuttal: the test covers it", prompt)
            self.assertIn("Raise one again only with new evidence", prompt)
            for absent in ("a parked risk", "another branch", "another repository", "after the head"):
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
        self.assertTrue(lines[0].startswith(f"- bin/tool.py: claim {total - 1:02d} Rebuttal: said again"), lines[0])
        self.assertTrue(lines[1].startswith("- bin/tool.py: long long"), lines[1])
        self.assertNotIn("TAIL", "\n".join(lines))
        self.assertNotIn("END", "\n".join(lines))
        kept = {line.split(" Rebuttal:")[0] for line in lines}
        self.assertNotIn("- bin/tool.py: claim 00", kept)
        self.assertIn(f"- bin/tool.py: claim {total - 2:02d}", kept)
        for line in lines:
            self.assertLessEqual(len(line), 160 + sd_review.REBUTTED_SUMMARY_CHARS + sd_review.REBUTTED_REASON_CHARS)

    def test_malformed_rows_are_skipped_not_fatal(self) -> None:
        self.write_row("ship-adjudication:text", "not json at all")
        self.write_row("ship-adjudication:list", ["a", "b"])
        self.write_row("ship-adjudication:object", {"proposal": {"bindings": {"repository": REPOSITORY, "branch": "main"},
                                                                 "findings": {"index": 1}}})
        self.write_row("ship-adjudication:rows", {"proposal": {
            "bindings": {"repository": REPOSITORY.upper(), "branch": "main"},
            "findings": [7, "x", None, {"response_disposition": "rebutted"},
                         {"response_disposition": "rebutted", "reason": {"a": [1, {"b": 2}]},
                          "raw_finding": {"path": ["x"], "summary": {"nested": {"deeper": [1, 2, 3]}}}},
                         {"response_disposition": "rebutted", "reason": "fine",
                          "raw_finding": {"path": "bin/tool.py", "summary": "a well-formed\n\tclaim \udcff"}}]}})
        self.write_row("ship-adjudication:deep", {"proposal": {
            "bindings": {"repository": REPOSITORY, "branch": "main"}, "findings": json.loads("[" * 200 + "]" * 200)}})
        prompts, _result, stderr = self.prompts()
        lines = self.section(prompts[0])
        # A lone surrogate is replaced, not a failed read; a nested value is its JSON text, on one line.
        self.assertIn("- bin/tool.py: a well-formed claim ? Rebuttal: fine", lines)
        self.assertIn('- ["x"]: {"nested": {"deeper": [1, 2, 3]}} Rebuttal: {"a": [1, {"b": 2}]}', lines)
        self.assertLessEqual(len(lines), sd_review.REBUTTED_LINES)
        self.assertEqual(stderr, "")

    def test_a_failed_read_is_unknown_not_empty(self) -> None:
        self.adjudication(("lane claim", "rebutted", "x"))
        stderr = io.StringIO()
        with unittest.mock.patch.object(self.library, "connect", side_effect=sd_review.sqlite3.OperationalError("locked")), \
                contextlib.redirect_stderr(stderr):
            text = sd_review.rebutted_findings(
                self.root, sd_review.resolve_subject(self.root, "worktree"), None, self.environment())
        self.assertEqual(text, "")
        self.assertEqual(stderr.getvalue(), "sd-review: reviewing without the rebutted findings: OperationalError: locked\n")

    def test_planning_and_a_detached_head_read_nothing(self) -> None:
        with unittest.mock.patch.object(self.library, "connect", side_effect=AssertionError("database read")):
            planning = sd_review.Subject("planning", "HEAD", "worktree", ("docs/work/x/design.md",), 1, "plan")
            self.assertEqual(sd_review.rebutted_findings(self.root, planning, None, self.environment()), "")
            subprocess.run(["git", "checkout", "--quiet", "--detach"], cwd=str(self.root), check=True,
                           env=clean_environment())
            subject = sd_review.resolve_subject(self.root, "worktree")
            self.assertEqual(sd_review.rebutted_findings(self.root, subject, None, self.environment()), "")

    def test_a_rebuttal_after_the_head_changes_no_request(self) -> None:
        """`--explain`, dispatch and a replay ask the same (sd:1397): the head's commit time anchors the read."""
        self.adjudication(("lane claim", "rebutted", "x"))
        first = self.prompts()[1]
        self.adjudication(("recorded later", "rebutted", "x"), days=-1, key="later")
        again = self.prompts()[1]
        self.assertEqual(again["request_sha256"], first["request_sha256"])


def row(severity: str = "high", **extra: Any) -> dict[str, Any]:
    return {"path": "src.py", "line": 1, "severity": severity, "summary": "defect", "family": "correctness"} | extra


class EvidenceTests(unittest.TestCase):
    def test_a_blocker_without_evidence_posts_as_advisory_and_says_why(self) -> None:
        for evidence in (None, "", "   \n\t", 0, 7, [], ["cmd"], {}, {"command": "x", "output": "y"}, True):
            with self.subTest(evidence=evidence):
                finding = row() if evidence is None else row(evidence=evidence)
                [record] = sd_review.dispose([finding], "high")
                self.assertEqual(record["disposition"], "advisory")
                self.assertEqual(record["summary"], "defect " + sd_review.NO_CHECK_NOTE)
                self.assertEqual(record["severity"], "high")

    def test_a_blocker_with_evidence_stays_blocking(self) -> None:
        [record] = sd_review.dispose([row(evidence="git ls-tree HEAD -- '[id]': printed nothing")], "high")
        self.assertEqual((record["disposition"], record["summary"]), ("blocking", "defect"))

    def test_the_floor_still_decides_and_advice_is_untouched(self) -> None:
        disposed = sd_review.dispose([row("medium"), row("low"), row("unspecified")], "high")
        self.assertEqual([(r["disposition"], r["summary"]) for r in disposed], [("advisory", "defect")] * 3)
        [medium] = sd_review.dispose([row("medium")], "medium")
        self.assertEqual((medium["disposition"], medium["summary"]), ("advisory", "defect " + sd_review.NO_CHECK_NOTE))

    def test_the_note_is_added_once(self) -> None:
        [once] = sd_review.dispose([row()], "high")
        [twice] = sd_review.dispose([once], "high")
        self.assertEqual(twice["summary"], once["summary"])

    def test_the_lanes_own_blockers_keep_blocking(self) -> None:
        for parsed in (sd_review.oversized_findings(),
                       sd_review.parse_findings(json.dumps({"findings": [row("low")] * (sd_review.MAX_FINDINGS + 1)}))):
            assert parsed is not None
            limit = [record for record in sd_review.dispose(parsed.findings, "high") if record["path"] == "<review-response>"]
            self.assertEqual([record["disposition"] for record in limit], ["blocking"])

    def test_the_parser_carries_evidence_and_reads_an_answer_without_it(self) -> None:
        parsed = sd_review.parse_findings(json.dumps({"findings": [row(evidence="  make test: 1 failed  "), row()]}))
        assert parsed is not None
        self.assertEqual(parsed.error, "")
        self.assertEqual(parsed.findings[0]["evidence"], "make test: 1 failed")
        self.assertNotIn("evidence", parsed.findings[1])
        long = sd_review.parse_findings(json.dumps({"findings": [row(evidence="x" * 50_000)]}))
        assert long is not None
        self.assertEqual(len(long.findings[0]["evidence"]), sd_review.EVIDENCE_CHARS)
        nested = sd_review.parse_findings(json.dumps({"findings": [row(evidence={"command": {"argv": ["git"]}})]}))
        assert nested is not None
        self.assertEqual(nested.error, "response violates the findings schema: findings[0].evidence: expected string")
        self.assertEqual(sd_review.dispose(nested.findings, "high")[0]["disposition"], "advisory")

    def test_the_schema_asks_for_evidence(self) -> None:
        item = sd_review.CODEX_OUTPUT_SCHEMA["properties"]["findings"]["items"]
        self.assertEqual(item["properties"]["evidence"], {"type": "string"})
        self.assertIn("evidence", item["required"])
        self.assertIn("evidence", sd_review.URL_OUTPUT_CONTRACT)


class EvidencePromptTests(RepeatFixture):
    def test_both_readers_are_asked_for_evidence(self) -> None:
        prompts, result, _stderr = self.prompts()
        self.assertEqual(len(prompts), 2)
        for prompt in prompts:
            self.assertIn("a check you ran, as the command and the output lines", prompt)
            # Bundle review round 3: five of six readers cannot run a command.
            self.assertIn("when you cannot run commands, the exact lines you read, as path:line", prompt)
            self.assertIn("A high finding with empty evidence is posted as advisory", prompt)
        self.assertIn("path:line and a quote of the lines you read", sd_review.URL_OUTPUT_CONTRACT)
        claude = sd_review.claude_argv(self.tmp, "claude")
        self.assertIn('"evidence"', claude[claude.index("--json-schema") + 1])

    def test_a_review_posts_an_unchecked_blocker_as_advice(self) -> None:
        for evidence, status in ((None, "advisory"), ("python3 src.py: Traceback", "blocking")):
            with self.subTest(evidence=evidence):
                answer = json.dumps({"findings": [row() if evidence is None else row(evidence=evidence)]})
                runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", ""),
                                     "codex": sd_review.Completed(0, answer, "")},
                                    default=sd_review.Completed(0, json.dumps({"findings": []}), ""))
                result = sd_review.review(self.root, namespace(), runner, self.environment(), self.chatgpt_home())
                self.assertEqual(result["status"], status)
                self.assertEqual(result["findings"][0]["disposition"], status)


class RoundThreeTests(ReviewFixture):
    """Bundle review round 3 of pack sd:9270: read-only readers, verification and dedupe."""

    def commit(self, root: pathlib.Path, path: str, text: str) -> str:
        (root / path).write_text(text)
        for argv in (["add", "--", path], ["commit", "-qm", "change\n\nAuthored-with: human"]):
            subprocess.run(["git", *argv], cwd=root, capture_output=True, check=True, env=clean_environment())
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, env=clean_environment()).strip()

    def branch(self) -> tuple[pathlib.Path, str]:
        root = self.make_repo()
        subprocess.run(["git", "checkout", "-qb", "topic"], cwd=root, capture_output=True, check=True,
                       env=clean_environment())
        return root, self.commit(root, "src.py", "the_original_defect = True\n")

    def answer(self, *rows: dict[str, Any]) -> sd_review.Completed:
        return sd_review.Completed(0, json.dumps({"findings": list(rows)}), "")

    def test_a_claude_finding_quoting_the_lines_it_read_stays_blocking(self) -> None:
        quoted = row(evidence="src.py:1: the_original_defect = True")
        [record] = sd_review.dispose([dict(quoted, backend="claude")], "high")
        self.assertEqual((record["disposition"], record["summary"]), ("blocking", "defect"))

    def test_a_verification_cannot_clear_a_confirmed_blocker_by_repeating_it_bare(self) -> None:
        root, first = self.branch()
        cited = row(path="src.py", evidence="python3 src.py: wrong")
        prior = sd_review.review(root, namespace(scope="branch"), FakeRunner({"codex": self.answer(cited)}),
                                 self.environment(), self.chatgpt_home())
        self.assertEqual(prior["status"], "blocking")
        report = self.tmp / "prior.json"
        report.write_text(json.dumps(prior))
        self.commit(root, "other.py", "unrelated_fix = True\n")
        # Round 4: the repeat matches on path and folded summary; a new unchecked claim in the same
        # file is not the confirmed blocker.
        bare, fresh = row(path="src.py", summary="  DEFECT "), row(path="other.py", summary="new and unchecked")
        same_file = row(path="src.py", summary="a new unchecked claim")
        for option in ("verify_report", "resume_report"):
            with self.subTest(option=option):
                runner = FakeRunner({"codex": self.answer(bare, fresh, same_file)})
                args = namespace(scope="branch", **({"base": first} if option == "verify_report" else {}),
                                 **{option: str(report)})
                result = sd_review.review(root, args, runner, self.environment(), self.chatgpt_home())
                self.assertEqual(result["status"], "blocking")
                self.assertTrue(any("with its path and summary unchanged" in str(call["stdin"]) + " ".join(call["argv"])
                                    for call in runner.calls))
                dispositions = {(row["path"], row["summary"]): row["disposition"] for row in result["findings"]
                                if row["backend"] == "codex"}
                self.assertEqual(dispositions, {
                    ("src.py", "DEFECT"): "blocking",
                    ("other.py", f"new and unchecked {sd_review.NO_CHECK_NOTE}"): "advisory",
                    ("src.py", f"a new unchecked claim {sd_review.NO_CHECK_NOTE}"): "advisory"})
        # Fixed: a verifier that does not repeat it clears it.
        runner = FakeRunner({"codex": self.answer()})
        result = sd_review.review(root, namespace(scope="branch", base=first, verify_report=str(report)), runner,
                                  self.environment(), self.chatgpt_home())
        self.assertNotEqual(result["status"], "blocking")

    def test_a_static_reader_may_answer_a_rebuttal_with_the_lines_it_read(self) -> None:
        # Round 4: the rebuttal section asked for a check only; five of six readers cannot run one.
        body = json.dumps({"proposal": {"findings": [{"raw_finding": row(), "response_disposition": "rebutted",
                                                       "reason": "not reachable"}]}})
        section = sd_review.rebuttal_section([(body,)])
        self.assertIn("a check you ran and its output", section)
        self.assertIn("when you cannot run commands, path:line and a quote of the current code", section)

    def test_one_finding_from_two_readers_is_one_row_whatever_each_cites(self) -> None:
        bare, cited, other = row(), row(evidence="src.py:1: x = 1"), row(evidence="python3 src.py: wrong")
        kept = sd_review.sd_lib.distinct_findings([dict(bare, backend="codex"), dict(cited, backend="second"),
                                                   dict(other, backend="third")])
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0]["backend"], kept[0]["evidence"]), ("codex", "src.py:1: x = 1"))
        self.assertEqual(sd_review.dispose(kept, "high")[0]["disposition"], "blocking")
        two = sd_review.sd_lib.distinct_findings([dict(row(), backend="codex"), dict(row(summary="other"), backend="x")])
        self.assertEqual(len(two), 2)


if __name__ == "__main__":
    unittest.main()
