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
import itertools
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
                     line: Any = 3, instances: Any = None, evidence: Any = None) -> None:
        """One `sd-ship adjudicate` record: each (summary, disposition, reason) a blocking finding's answer,
        at the reviewed head unless `head` names another; `evidence` is what the answered finding cited."""
        findings = [{"index": index, "finding_digest": "0" * 64, "response_disposition": answer, "reason": reason,
                     "raw_finding": {"path": "bin/tool.py", "line": line, "severity": "high", "instances": instances or [f"bin/tool.py:{line}"], "summary": summary,
                                     "family": "correctness", "disposition": "blocking",
                                     **({} if evidence is None else {"evidence": evidence})}}
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
CITED = ("python3 src.py: Traceback", "`f(1)` returns 2, expected 1")
REPRO = "python3 t.py: expected 1, got 2"


def finding(evidence: Any = None, **extra: Any) -> dict[str, Any]:
    return row(**extra) if evidence is None else row(evidence=evidence, **extra)


class PriorLineTests(RepeatFixture):
    """build/sd-1423 review (bin/sd-review:1810): 21 lines inserted above a blocker at line 10 moved it past the
    fixed window, so an uncited, paraphrased repeat at line 31 cleared the gate. Verification and resume now map
    each prior blocker's line through the diff from the prior head to this head before any is matched."""

    def test_a_blocker_moved_by_an_insertion_above_it_still_blocks(self) -> None:
        source = "".join(f"line{number}\n" for number in range(1, 41))
        (self.root / "src.py").write_text(source, encoding="utf-8")
        old = self.commit("add", "src.py") and self.commit("old")
        (self.root / "src.py").write_text("".join(f"new{n}\n" for n in range(21)) + source, encoding="utf-8")
        new = self.commit("add", "src.py") and self.commit("new")
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking"),
                 dict(finding(REPRO, path="gone.py", line=7), disposition="blocking")]
        moved = sd_review.prior_at(self.root, old, new, prior)
        self.assertEqual([row["line"] for row in moved], [31, 7])
        [record] = sd_review.dispose([finding(line=31, summary="An empty list raises IndexError")], "high",
                                     repro=True, prior=moved)
        self.assertEqual(record["disposition"], "blocking")

    def test_a_textconv_filter_cannot_hide_an_insertion_above_a_blocker(self) -> None:
        """Class pass of bin/sd-review:871: `prior_at` maps a blocker's line through the raw diff; a `diff=`
        textconv that drops the inserted lines would leave it at its old line."""
        source = "".join(f"line{number}\n" for number in range(1, 41))
        (self.root / "src.py").write_text(source, encoding="utf-8")
        (self.root / ".gitattributes").write_text("*.py diff=hide\n", encoding="utf-8")
        self.commit("config", "diff.hide.textconv", "sed /new/d")
        old = self.commit("add", "src.py", ".gitattributes") and self.commit("old")
        (self.root / "src.py").write_text("".join(f"new{n}\n" for n in range(21)) + source, encoding="utf-8")
        new = self.commit("add", "src.py") and self.commit("new")
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")]
        self.assertEqual([row["line"] for row in sd_review.prior_at(self.root, old, new, prior)], [31])

    def test_evidence_found_after_a_rebuttal_still_blocks_its_uncited_repeat(self) -> None:
        """Review of sd:10191 (bin/sd-review:886): a rebuttal at line 20 rests on line 2; a distant edit of
        line 2 keeps the ruling's hunk, so it stays listed. A pass after the ruling found the crash with
        evidence; the next pass repeats it uncited, and the rebuttal held it back as advisory. Evidence found
        after the ruling's head is new evidence: the repeat takes it and blocks. Evidence of the pass the
        ruling answered is not: that repeat stays advisory."""
        (self.root / "src.py").write_text("".join(f"line{n}\n" for n in range(1, 41)), encoding="utf-8")
        ruled = self.commit("add", "src.py") and self.commit("ruled")
        (self.root / "src.py").write_text((self.root / "src.py").read_text(encoding="utf-8").replace(
            "line2\n", "divisor = 0\n"), encoding="utf-8")
        found = self.commit("add", "src.py") and self.commit("a distant edit")
        (self.root / "other.py").write_text("x = 1\n", encoding="utf-8")
        new = self.commit("add", "other.py") and self.commit("new")
        rebutted = [sd_review.Rebuttal(ruled, "src.py", sd_review.summary_key("Division by zero"), 20, True,
                                       "src.py", "Division by zero", "line 2 sets the divisor to 1", ("src.py:20",), REPRO)]
        repeat = finding(line=20, summary="Division by zero")
        for head, expected in ((found, "blocking"), (ruled, "advisory")):
            with self.subTest(found_at=head):
                prior = [dict(finding(REPRO, line=20, summary="Division by zero"), disposition="blocking",
                              prior_review={"pass": 1, "head": head})]
                moved = sd_review.prior_at(self.root, head, new, prior)
                [record] = sd_review.dispose([repeat], "high", rebutted, repro=True, prior=moved,
                                             later=sd_review.found_after(self.root))
                self.assertEqual(record["disposition"], expected)
                self.assertEqual(record.get("evidence"), REPRO if expected == "blocking" else None)

    def test_a_rebuttal_at_an_adjacent_line_does_not_clear_an_open_blocker(self) -> None:
        """Review of sd:10191 (bin/sd-review:1870, :1875): a rebuttal sits at line 20. A prior pass found
        the crash at line 21 with evidence, and no ruling covers 21. The next reviewer cites line 20
        uncited. The rebuttal must not clear the open blocker at 21: the repeat takes its evidence and
        blocks. When a finding at 21 repeats that blocker itself, the repeat at 20 stays advisory."""
        rebutted = [sd_review.Rebuttal("a" * 40, "src.py", sd_review.summary_key("Division by zero"), 20, True,
                                       "src.py", "Division by zero", "a reason", ("src.py:20",), "")]
        prior = [dict(finding(REPRO, line=21, summary="Division by zero"), disposition="blocking")]
        repeat = finding(line=20, summary="Division by zero")
        [record] = sd_review.dispose([repeat], "high", rebutted, repro=True, prior=prior)
        self.assertEqual((record["disposition"], record.get("evidence")), ("blocking", REPRO))
        own = finding(REPRO, line=21, summary="Division by zero")
        disposed = sd_review.dispose([repeat, own], "high", rebutted, repro=True, prior=prior)
        self.assertEqual([row["disposition"] for row in disposed], ["advisory", "blocking"])
        [record] = sd_review.dispose([repeat], "high", rebutted, repro=True, prior=[])
        self.assertEqual(record["disposition"], "advisory")

    def test_a_rebutted_repeat_reads_the_same_hold_as_any_uncited_finding(self) -> None:
        """Class pass of sd:10191 (bin/sd-review:1878). Class: a listed rebuttal made its uncited repeat
        advisory before the hold of an open prior blocker was read, while every other uncited finding
        fails closed beside one (`prior_evidence`). A rebuttal sits at line 20; an unruled prior blocker
        sits nearby under another summary, as a second reviewer words it. The repeat at 20 blocks while
        that blocker is within `SHIFT_LINES` and no other blocking finding holds it; farther off, or
        held by its own repeat, the rebuttal still answers line 20."""
        rebutted = [sd_review.Rebuttal("a" * 40, "src.py", sd_review.summary_key("Division by zero"), 20, True,
                                       "src.py", "Division by zero", "a reason", ("src.py:20",), "")]
        repeat = finding(line=20, summary="Division by zero")
        for line, expected in ((21, "blocking"), (20, "blocking"), (17, "blocking"), (23, "blocking"),
                               (24, "advisory"), (None, "advisory")):
            with self.subTest(open_blocker_at=line):
                prior = [dict(finding(REPRO, line=line, summary="A zero divisor crashes the report"),
                              disposition="blocking")]
                [record] = sd_review.dispose([repeat], "high", rebutted, repro=True, prior=prior)
                self.assertEqual(record["disposition"], expected)
        prior = [dict(finding(REPRO, line=21, summary="A zero divisor crashes the report"), disposition="blocking")]
        own = finding(line=21, summary="A zero divisor crashes the report")
        disposed = sd_review.dispose([repeat, own], "high", rebutted, repro=True, prior=prior)
        self.assertEqual([(row["disposition"], row.get("evidence")) for row in disposed],
                         [("advisory", None), ("blocking", REPRO)])
        other = [dict(finding(REPRO, path="other.py", line=20, summary="A zero divisor crashes the report"),
                      disposition="blocking")]
        [record] = sd_review.dispose([repeat], "high", rebutted, repro=True, prior=other)
        self.assertEqual(record["disposition"], "advisory")

    def test_a_rebutted_place_does_not_shadow_an_open_blocker_from_a_finding_beside_it(self) -> None:
        """Class pass of sd:10191 (bin/sd-review:1878), in `prior_evidence`: the ruled blocker sits rebutted
        at line 20 and an open blocker of the same summary sits a few lines on. An uncited finding beside
        line 20, not on it, echoed the rebutted place as the nearer one, so it took no hold and posted as
        advisory: the rebuttal cleared the open blocker. Without the rebuttal the same finding blocks, so
        with it the finding blocks too. A finding that repeats the open blocker at its own line still
        releases it."""
        rebutted = [sd_review.Rebuttal("a" * 40, "src.py", sd_review.summary_key("Division by zero"), 20, True,
                                       "src.py", "Division by zero", "a reason", ("src.py:20",), "")]
        for at, blocker in ((19, 24), (19, 27), (21, 25), (21, 27), (22, 27), (18, 28)):
            with self.subTest(finding_at=at, open_blocker_at=blocker):
                prior = [dict(finding("old evidence", line=20, summary="Division by zero"), disposition="blocking"),
                         dict(finding(REPRO, line=blocker, summary="Division by zero"), disposition="blocking")]
                beside = finding(line=at, summary="Division by zero")
                [record] = sd_review.dispose([beside], "high", rebutted, repro=True, prior=prior)
                self.assertEqual(record["disposition"], "blocking")
                own = finding(line=blocker, summary="Division by zero")
                disposed = sd_review.dispose([beside, own], "high", rebutted, repro=True, prior=prior)
                self.assertEqual([(row["disposition"], row.get("evidence")) for row in disposed],
                                 [("advisory", None), ("blocking", REPRO)])

    def test_only_the_evidence_a_ruling_answered_is_old_to_it(self) -> None:
        """Class pass of sd:10191 (bin/sd-review:2011, :2024): what a ruling answered is the evidence in its
        own adjudication, not a commit and not a normal form of it. At the ruling's head only that text,
        whole and unchanged, is old. Every other text is new, as each form the comparison once read as the
        same shows: another case, other spacing, another line break, a blank line, another order, a repeat,
        a part. A head before the ruling is old; a head on another line of history, or one git cannot
        place, counts as after: hold on doubt."""
        (self.root / "src.py").write_text("".join(f"line{n}\n" for n in range(1, 41)), encoding="utf-8")
        before = self.commit("add", "src.py") and self.commit("before")
        ruled = self.commit("ruled")
        self.commit("checkout", "--quiet", "-b", "side", before)
        aside = self.commit("aside")
        self.commit("checkout", "--quiet", "-")
        new = self.commit("new")
        answered, fresh = f"{REPRO}\nsecond line", "python3 src.py: ZeroDivisionError"
        rebutted = [sd_review.Rebuttal(ruled, "src.py", sd_review.summary_key("Division by zero"), 20, True,
                                       "src.py", "Division by zero", "a reason", ("src.py:20",), answered)]
        other = {"case": answered.swapcase(), "inner spacing": answered.replace(" ", "  ", 1),
                 "a line's end": f"{REPRO} \nsecond line", "a line's start": f"{REPRO}\n second line",
                 "a form feed for the break": f"{REPRO}\fsecond line", "a blank line": f"{REPRO}\n\nsecond line",
                 "order": f"second line\n{REPRO}", "a repeat": f"{REPRO}\n{answered}", "a part": REPRO,
                 "one more line": f"{answered}\n{fresh}"}
        for head, evidence, expected in ((ruled, answered, "advisory"), (ruled, fresh, "blocking"),
                                         *((ruled, text, "blocking") for text in other.values()),
                                         (before, fresh, "advisory"), (aside, answered, "blocking"),
                                         ("f" * 40, answered, "blocking")):
            with self.subTest(found_at=head, evidence=evidence):
                prior = [dict(finding(evidence, line=20, summary="Division by zero"), disposition="blocking",
                              prior_review={"pass": 1, "head": head})]
                moved = sd_review.prior_at(self.root, ruled, new, prior)
                [record] = sd_review.dispose([finding(line=20, summary="Division by zero")], "high", rebutted,
                                             repro=True, prior=moved, later=sd_review.found_after(self.root))
                self.assertEqual(record["disposition"], expected)
                self.assertEqual(record.get("evidence"), evidence if expected == "blocking" else None)

    def test_a_ruling_keeps_the_evidence_its_adjudication_answered(self) -> None:
        """The evidence in the adjudicated finding is what the ruling answered; any other shape is none."""
        def answered(evidence: Any) -> str:
            body = json.dumps({"decision": "accepted", "proposal": {"bindings": {"head": ON_BRANCH}, "findings": [
                {"response_disposition": "rebutted", "reason": "a reason",
                 "raw_finding": {"path": "src.py", "line": 1, "severity": "high", "summary": "defect",
                                 "instances": ["src.py:1"], "evidence": evidence}}]}})
            return listed(body)[0].answered
        self.assertEqual(answered(REPRO), REPRO)
        for shape in UNCITED:
            self.assertEqual(answered(shape).strip(), "", repr(shape))

    def test_a_receipt_that_kept_no_evidence_does_not_say_what_its_ruling_answered(self) -> None:
        """Class pass of sd:10191 (bin/sd-review:2030): a receipt without evidence is unknown, not empty. A
        build's follow-up receipt once kept none, whatever the finding cited; only such a receipt, by its
        key, leaves the answer unknown (None). Any other receipt without evidence answered an uncited finding."""
        def answered(key: str, raw: dict[str, Any]) -> Any:
            body = json.dumps({"decision": "accepted", "proposal": {"bindings": {"head": ON_BRANCH}, "findings": [
                {"response_disposition": "rebutted", "reason": "a reason",
                 "raw_finding": {"path": "src.py", "line": 1, "summary": "defect", "instances": ["src.py:1"], **raw}}]}})
            [ruling] = sd_review.rebuttals([(body, key)], {ON_BRANCH}.__contains__, lambda _head, _path, line: (line,))
            return ruling.answered
        filed, shipped = "ship-adjudication:build-run:0a1b:7", "ship-adjudication:7:k"
        self.assertIsNone(answered(filed, {}), "a build receipt from before it kept evidence")
        for key, raw, expected in ((filed, {"evidence": REPRO}, REPRO), (filed, {"evidence": ""}, ""),
                                   (filed, {"evidence": None}, ""), (shipped, {}, ""), (shipped, {"evidence": REPRO}, REPRO),
                                   ("ship-adjudication:7:build-run:", {}, ""), ("ship-adjudication-no-item:build-run:k", {}, "")):
            with self.subTest(key=key, raw=raw):
                self.assertEqual(answered(key, raw), expected)

    def test_a_ruling_that_cannot_say_what_it_answered_holds_at_its_own_head(self) -> None:
        """Class pass of sd:10191 (bin/sd-review:2030): a build filed a blocker before its receipt kept
        evidence. A retry at that head read the filed finding's own repro as new and blocked on it. An
        unknown answer holds at the ruling's head, as before the ruling; evidence at a later head still
        blocks, and a ruling that answered an uncited finding still reads any repro as new."""
        (self.root / "src.py").write_text("".join(f"line{n}\n" for n in range(1, 41)), encoding="utf-8")
        ruled = self.commit("add", "src.py") and self.commit("ruled")
        new = self.commit("new")
        for answered, head, expected in ((None, ruled, "advisory"), (None, new, "blocking"), ("", ruled, "blocking")):
            with self.subTest(answered=answered, found_at=head):
                rebutted = [sd_review.Rebuttal(ruled, "src.py", sd_review.summary_key("Division by zero"), 20, True,
                                               "src.py", "Division by zero", "filed", ("src.py:20",), answered)]
                prior = [dict(finding(REPRO, line=20, summary="Division by zero"), disposition="blocking",
                              prior_review={"pass": 1, "head": head})]
                moved = sd_review.prior_at(self.root, head, new, prior)
                [record] = sd_review.dispose([finding(line=20, summary="Division by zero")], "high", rebutted,
                                             repro=True, prior=moved, later=sd_review.found_after(self.root))
                self.assertEqual(record["disposition"], expected)

    def test_fresh_evidence_at_one_rebuttal_leaves_a_nearby_rebuttal_holding(self) -> None:
        """Review of sd:10191 (bin/sd-review:1857, :1865): two separate rebuttals of one summary sit at lines
        20 and 25. Evidence found after the ruling at line 20 revives its blocker; it lent that evidence, as
        an echo, to the uncited repeat at line 25 that its own rebuttal answers. Only a revived blocker of
        the same ruling lends: the repeat at 25 stays advisory, the one at 20 blocks."""
        key = sd_review.summary_key("Division by zero")
        rebutted = [sd_review.Rebuttal(ON_BRANCH, "src.py", key, line, True, "src.py", "Division by zero",
                                       "a reason", (f"src.py:{line}",), "old evidence") for line in (20, 25)]
        prior = [dict(finding(REPRO, line=20, summary="Division by zero"), disposition="blocking")]
        for line, expected in ((25, "advisory"), (20, "blocking")):
            with self.subTest(line=line):
                [record] = sd_review.dispose([finding(line=line, summary="Division by zero")], "high", rebutted,
                                             repro=True, prior=prior, later=lambda *_: True)
                self.assertEqual(record["disposition"], expected)

    def test_fresh_evidence_at_one_place_of_a_ruling_blocks_its_repeat_at_another(self) -> None:
        """Review of sd:10191 (bin/sd-review:1868, :1877): one rebuttal names src.py:20 and src.py:40. The pass
        it answered is rebutted at 20; a later pass found the crash at 40 with fresh evidence. The uncited
        repeat at 20 is that ruling's too: the old rebutted record there held it back, and the fresh evidence
        at 40 was lent to nothing. It blocks, carrying the fresh evidence."""
        key = sd_review.summary_key("Division by zero")
        rebutted = [sd_review.Rebuttal(ON_BRANCH, "src.py", key, 20, True, "src.py", "Division by zero",
                                       "a reason", ("src.py:20", "src.py:40"), "old evidence")]
        prior = [dict(finding("old evidence", line=20, summary="Division by zero"), disposition="blocking"),
                 dict(finding(REPRO, line=40, summary="Division by zero"), disposition="blocking")]
        [record] = sd_review.dispose([finding(line=20, summary="Division by zero")], "high", rebutted,
                                     repro=True, prior=prior, later=lambda _, old: old.get("evidence") == REPRO)
        self.assertEqual((record["disposition"], record.get("evidence")), ("blocking", REPRO))

    def test_each_blocker_maps_from_the_head_it_was_found_at(self) -> None:
        """Lead ruling 2026-11-22 (bin/sd-review:2496): a full-branch retry mapped every prior finding from the
        aggregate's latest head, so a blocker found at line 10 on an older head, with 30 lines inserted above it
        since, stayed at 10 and its uncited repeat at 40 posted as advisory. Each maps from `prior_review.head`."""
        source = "".join(f"line{number}\n" for number in range(1, 41))
        (self.root / "src.py").write_text(source, encoding="utf-8")
        first = self.commit("add", "src.py") and self.commit("first")
        (self.root / "src.py").write_text("".join(f"new{n}\n" for n in range(30)) + source, encoding="utf-8")
        latest = self.commit("add", "src.py") and self.commit("latest")
        (self.root / "other.py").write_text("x = 1\n", encoding="utf-8")
        new = self.commit("add", "other.py") and self.commit("new")
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking",
                      prior_review={"pass": 1, "head": first}),
                 dict(finding(REPRO, line=40, summary="Empty input crashes"), disposition="blocking",
                      prior_review={"pass": 2, "head": latest}),
                 dict(finding(REPRO, line=5), disposition="blocking", prior_review={"head": "not-a-head"})]
        moved = sd_review.prior_at(self.root, latest, new, prior)
        self.assertEqual([row["line"] for row in moved], [40, 40, 5])
        [record] = sd_review.dispose([finding(line=40, summary="An empty list raises IndexError")], "high",
                                     repro=True, prior=moved)
        self.assertEqual(record["disposition"], "blocking")

    def test_a_blocker_follows_its_code_moved_within_its_file(self) -> None:
        """build/sd-1423 review (bin/sd-review:1852), class pass: moving `broken_call()` unfixed from line 10 to
        line 100 left the blocker at line 9, so an uncited repeat at 100 posted as advisory. A removed line whose
        exact text is added once elsewhere follows it; an edited or ambiguous line keeps its hunk offset."""
        body = ["".join(f"line{n}\n" for n in range(1, 10)), "    broken_call()\n",
                "".join(f"line{n}\n" for n in range(11, 101))]
        (self.root / "src.py").write_text("".join(body), encoding="utf-8")
        old = self.commit("add", "src.py") and self.commit("old")
        (self.root / "src.py").write_text(body[0] + body[2] + "broken_call()\n", encoding="utf-8")
        new = self.commit("add", "src.py") and self.commit("moved")
        prior = [dict(finding(REPRO, line=10, summary="broken_call crashes"), disposition="blocking")]
        moved = sd_review.prior_at(self.root, old, new, prior)
        self.assertEqual([(row["path"], row["line"]) for row in moved], [("src.py", 9), ("src.py", 100)])
        [record] = sd_review.dispose([finding(line=100, summary="broken_call crashes")], "high",
                                     repro=True, prior=moved)
        self.assertEqual((record["disposition"], record.get("evidence")), ("blocking", REPRO))
        # Fixed while moved: the text differs, so the blocker keeps its offset and its repeat needs evidence.
        (self.root / "src.py").write_text(body[0] + body[2] + "fixed_call()\n", encoding="utf-8")
        fixed = self.commit("add", "src.py") and self.commit("fixed")
        self.assertEqual([row["line"] for row in sd_review.prior_at(self.root, old, fixed, prior)], [9])

    def test_a_blocker_follows_its_code_moved_to_another_file(self) -> None:
        """Class pass on bin/sd-review:1852: the same move into another file, not a rename, left the blocker
        on src.py, so the repeat at helpers.py posted as advisory."""
        body = "".join(f"line{n}\n" for n in range(1, 10)) + "    broken_call()\n" + "tail\n"
        (self.root / "src.py").write_text(body, encoding="utf-8")
        (self.root / "helpers.py").write_text("".join(f"h{n}\n" for n in range(1, 30)), encoding="utf-8")
        old = self.commit("add", "src.py", "helpers.py") and self.commit("old")
        (self.root / "src.py").write_text(body.replace("    broken_call()\n", ""), encoding="utf-8")
        (self.root / "helpers.py").write_text("".join(f"h{n}\n" for n in range(1, 30)) + "    broken_call()\n",
                                              encoding="utf-8")
        new = self.commit("add", "src.py", "helpers.py") and self.commit("moved")
        prior = [dict(finding(REPRO, line=10, summary="broken_call crashes"), disposition="blocking")]
        moved = sd_review.prior_at(self.root, old, new, prior)
        self.assertEqual([(row["path"], row["line"]) for row in moved], [("src.py", 9), ("helpers.py", 30)])

    def test_a_repeat_at_a_moved_blocker_leaves_its_old_place_holding(self) -> None:
        """build/sd-1623 review (bin/sd-review:1838, :1980): code moved from src.py:10 to helpers.py:30 and
        repeated there. The move was inferred from line text, and the repeat at the new place cleared the hold
        at the old hunk, where the defect may have stayed. A move adds a place and answers none: the old place
        holds a finding beside it until a completed pass clears the blocker (`unresolved`, lead #72671)."""
        body = "".join(f"line{n}\n" for n in range(1, 10)) + "    broken_call()\n" + "tail\n"
        (self.root / "src.py").write_text(body, encoding="utf-8")
        (self.root / "helpers.py").write_text("".join(f"h{n}\n" for n in range(1, 30)), encoding="utf-8")
        old = self.commit("add", "src.py", "helpers.py") and self.commit("old")
        (self.root / "src.py").write_text(body.replace("    broken_call()\n", ""), encoding="utf-8")
        (self.root / "helpers.py").write_text("".join(f"h{n}\n" for n in range(1, 30)) + "    broken_call()\n",
                                              encoding="utf-8")
        new = self.commit("add", "src.py", "helpers.py") and self.commit("moved")
        prior = [dict(finding(REPRO, line=10, summary="broken_call crashes"), disposition="blocking")]
        moved = sd_review.prior_at(self.root, old, new, prior)
        repeat, other = sd_review.dispose([finding(path="helpers.py", line=30, summary="broken_call crashes"),
                                           finding(line=9, summary="the cache is never cleared")],
                                          "high", repro=True, prior=moved)
        self.assertEqual([(row["disposition"], row.get("evidence")) for row in (repeat, other)],
                         [("blocking", REPRO), ("blocking", None)])

    def test_a_repeat_midway_between_a_hunk_and_its_moved_text_releases_neither(self) -> None:
        """build/sd-1623 review (bin/sd-review:1982): `broken_call()` moved from line 10 to line 19 of one file.
        The blocker holds line 9, its hunk, and line 19; a repeat at line 14 is as near one as the other and
        answered both, so a paraphrase beside either posted as advisory."""
        lines = [f"line{n}\n" for n in range(1, 31)]
        (self.root / "src.py").write_text("".join(lines[:9]) + "    broken_call()\n" + "".join(lines[9:]), encoding="utf-8")
        old = self.commit("add", "src.py") and self.commit("old")
        (self.root / "src.py").write_text("".join(lines[:18]) + "    broken_call()\n" + "".join(lines[18:]), encoding="utf-8")
        new = self.commit("add", "src.py") and self.commit("moved")
        moved = sd_review.prior_at(self.root, old, new, [dict(finding(REPRO, line=10, summary="broken_call crashes"),
                                                              disposition="blocking")])
        self.assertEqual([row["line"] for row in moved], [9, 19])
        disposed = sd_review.dispose([finding(line=14, summary="broken_call crashes"),
                                      finding(line=8, summary="the cache is never cleared"),
                                      finding(line=20, summary="the cache is never cleared")], "high", repro=True, prior=moved)
        self.assertEqual([(row["disposition"], row.get("evidence")) for row in disposed],
                         [("blocking", REPRO), ("blocking", None), ("blocking", None)])

    def test_each_instance_maps_to_this_head_on_its_own(self) -> None:
        """Class pass on bin/sd-review:1838: `prior_at` takes a blocker as stored and maps each place it names
        through its own file's diff; no row keeps the list of places as the prior head spelled them."""
        (self.root / "src.py").write_text("".join(f"line{n}\n" for n in range(1, 21)), encoding="utf-8")
        (self.root / "b.py").write_text("".join(f"b{n}\n" for n in range(1, 61)), encoding="utf-8")
        old = self.commit("add", "src.py", "b.py") and self.commit("old")
        (self.root / "b.py").write_text("top\ntop\n" + "".join(f"b{n}\n" for n in range(1, 61)), encoding="utf-8")
        new = self.commit("add", "b.py") and self.commit("new")
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes", instances=["src.py:10", "b.py:40"]),
                      disposition="blocking")]
        moved = sd_review.prior_at(self.root, old, new, prior)
        self.assertEqual([(row["path"], row["line"], "instances" in row) for row in moved],
                         [("src.py", 10, False), ("b.py", 42, False)])
        # A rebuttal recorded at this head's place answers that place.
        rebutted = listed(adjudicated(("b.py", "Empty input crashes", "rebutted"), line=42))
        [record] = sd_review.dispose([finding(path="b.py", line=43, summary="the cache is never cleared")], "high",
                                     rebutted, repro=True, prior=moved)
        self.assertEqual(record["disposition"], "advisory")

    def test_a_blocker_whose_line_was_edited_keeps_its_place_when_its_text_appears_elsewhere(self) -> None:
        """build/sd-1623 review (bin/sd-review:2612, :1921): `return None` at src.py:10 replaced by another
        expression that keeps the defect, and `return None` added once in an unrelated helpers.py. The mapper
        moved the blocker to helpers.py, so an uncited repeat at the still-broken src.py:10 posted as advisory.
        A moved blocker now also holds the place its hunk maps to (fail closed)."""
        body = "".join(f"line{n}\n" for n in range(1, 10)) + "    return None\n" + "tail\n"
        (self.root / "src.py").write_text(body, encoding="utf-8")
        (self.root / "helpers.py").write_text("h1\n", encoding="utf-8")
        old = self.commit("add", "src.py", "helpers.py") and self.commit("old")
        (self.root / "src.py").write_text(body.replace("return None", "return None if x else None"), encoding="utf-8")
        (self.root / "helpers.py").write_text("h1\n    return None\n", encoding="utf-8")
        new = self.commit("add", "src.py", "helpers.py") and self.commit("edited")
        prior = [dict(finding(REPRO, line=10, summary="returns None on empty input"), disposition="blocking")]
        moved = sd_review.prior_at(self.root, old, new, prior)
        self.assertIn(("src.py", 10), [(row["path"], row["line"]) for row in moved])
        [record] = sd_review.dispose([finding(line=10, summary="returns None on empty input")], "high",
                                     repro=True, prior=moved)
        self.assertEqual((record["disposition"], record.get("evidence")), ("blocking", REPRO))
        # build/sd-1623 review (bin/sd-review:1980): the same summary at the place the text went answers that
        # place alone, so a paraphrase at the edited line is still held.
        disposed = sd_review.dispose([finding(path="helpers.py", line=2, summary="returns None on empty input"),
                                      finding(line=10, summary="x is never checked")], "high", repro=True, prior=moved)
        self.assertEqual([row["disposition"] for row in disposed], ["blocking", "blocking"])

    def test_a_blocker_follows_its_file_through_a_rename(self) -> None:
        """build/sd-1423 review (bin/sd-review:1839): renaming src.py to renamed.py left the prior blocker on
        src.py, so an uncited repeat at renamed.py posted as advisory though the defect survived the rename."""
        source = "".join(f"line{number}\n" for number in range(1, 41))
        (self.root / "src.py").write_text(source, encoding="utf-8")
        old = self.commit("add", "src.py") and self.commit("old")
        self.commit("mv", "src.py", "renamed.py")
        (self.root / "renamed.py").write_text("top\n" + source, encoding="utf-8")
        new = self.commit("add", "renamed.py") and self.commit("renamed")
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")]
        moved = sd_review.prior_at(self.root, old, new, prior)
        self.assertEqual([(row["path"], row["line"]) for row in moved], [("renamed.py", 11)])
        [record] = sd_review.dispose([finding(path="renamed.py", line=11, summary="An empty list raises IndexError")],
                                     "high", repro=True, prior=moved)
        self.assertEqual(record["disposition"], "blocking")

    def test_an_unplaced_blocker_follows_its_file_through_a_rename(self) -> None:
        """build/sd-1423 review (bin/sd-review:1846): a prior blocker with `line: null` skipped the rename, so
        its uncited repeat at the new path found no prior blocker and posted as advisory."""
        (self.root / "old.py").write_text("x = 1\n" * 20, encoding="utf-8")
        old = self.commit("add", "old.py") and self.commit("old")
        self.commit("mv", "old.py", "new.py")
        new = self.commit("renamed")
        prior = [dict(finding(REPRO, path="old.py", line=None), disposition="blocking")]
        moved = sd_review.prior_at(self.root, old, new, prior)
        self.assertEqual([(row["path"], row["line"]) for row in moved], [("new.py", None)])
        [record] = sd_review.dispose([finding(path="new.py", line=None)], "high", repro=True, prior=moved)
        self.assertEqual((record["disposition"], record.get("evidence")), ("blocking", REPRO))

    def test_a_patch_that_is_not_utf8_leaves_the_line_as_stored(self) -> None:
        """build/sd-1423 review (bin/sd-review:1852): an edited Windows-1252 line (byte 0xE9) in the blocker's
        file raised UnicodeDecodeError out of the strict decode, after the providers had finished. git output
        that does not decode is an answer git could not give: the line stays as stored."""
        (self.root / "src.py").write_bytes(b"caf\xe9 = 1\n" * 20)
        old = self.commit("add", "src.py") and self.commit("old")
        (self.root / "src.py").write_bytes(b"x\n" + b"caf\xe9 = 2\n" * 20)
        new = self.commit("add", "src.py") and self.commit("new")
        prior = [dict(finding(REPRO, line=10), disposition="blocking")]
        self.assertEqual([row["line"] for row in sd_review.prior_at(self.root, old, new, prior)], [10])
        self.assertIsNone(sd_review.sd_lib.git_output(["show", f"{new}:src.py"], self.root))

    def test_a_path_with_a_nul_leaves_the_blocker_as_stored(self) -> None:
        """build/sd-1423 review (bin/sd-review:1865): a stored blocker's JSON path with a NUL reached git as a
        pathspec, and subprocess raised `ValueError: embedded null byte` after the providers had finished. An
        argument git cannot take is an answer it could not give, so the blocker stays as stored."""
        old = self.commit("old")
        new = self.commit("new")
        prior = [dict(finding(REPRO, path="a\0b.py", line=3), disposition="blocking"),
                 dict(finding(REPRO, path="c\0d.py", line=None), disposition="blocking")]
        self.assertEqual([(row["path"], row["line"]) for row in sd_review.prior_at(self.root, old, new, prior)],
                         [("a\0b.py", 3), ("c\0d.py", None)])
        self.assertIsNone(sd_review.sd_lib.git_output(["log", "-1", "a\0b"], self.root))

    def test_a_path_is_read_literally_not_as_a_pattern(self) -> None:
        """build/sd-1423 class pass (bin/sd-review:1853): the remap passed `page[1].py` to git as a glob, so it
        read page1.py's patch too, and an insertion of 25 lines above the defect mapped it wrong."""
        source = "".join(f"line{number}\n" for number in range(1, 41))
        for name in ("page1.py", "page[1].py"):
            (self.root / name).write_text(source, encoding="utf-8")
        old = self.commit("add", ".") and self.commit("old")
        (self.root / "page[1].py").write_text("".join(f"new{n}\n" for n in range(25)) + source, encoding="utf-8")
        (self.root / "page1.py").write_text(source.replace("line3\n", ""), encoding="utf-8")
        new = self.commit("add", ".") and self.commit("new")
        prior = [dict(finding(REPRO, path="page[1].py", line=10), disposition="blocking")]
        self.assertEqual([row["line"] for row in sd_review.prior_at(self.root, old, new, prior)], [35])

    def test_the_line_map_reads_each_hunk(self) -> None:
        diff = "@@ -0,0 +1,21 @@\n+x\n@@ -5,2 +26,3 @@\n@@ -20 +42,0 @@\n"
        self.assertEqual([sd_review.moved_line(diff, line) for line in (1, 4, 5, 6, 7, 19, 20, 30)],
                         [22, 25, 26, 27, 29, 41, 42, 51])

    def test_a_blocker_inside_a_replaced_hunk_keeps_its_place(self) -> None:
        """build/sd-1423 review (bin/sd-review:1829): `@@ -100,60 +100,60 @@` mapped a blocker at line 145 to the
        hunk start, 100, so its exact repeat at 145 missed the match. A replaced line keeps its offset in the hunk.
        A finding past `SHIFT_LINES` of it is advisory."""
        (self.root / "src.py").write_text("".join(f"old{n}\n" for n in range(1, 201)), encoding="utf-8")
        old = self.commit("add", "src.py") and self.commit("old")
        lines = [f"old{n}\n" if not 100 <= n < 160 else f"new{n}\n" for n in range(1, 201)]
        (self.root / "src.py").write_text("".join(lines), encoding="utf-8")
        new = self.commit("add", "src.py") and self.commit("new")
        prior = [dict(finding(REPRO, line=145, summary="Empty input crashes"), disposition="blocking")]
        [moved] = sd_review.prior_at(self.root, old, new, prior)
        self.assertEqual(moved["line"], 145)
        disposed = sd_review.dispose([finding(line=145, summary="Empty input crashes"),
                                      finding(line=146 + sd_review.SHIFT_LINES, summary="An empty list raises IndexError")],
                                     "high", repro=True, prior=[moved])
        self.assertEqual([(row["disposition"], row.get("evidence")) for row in disposed],
                         [("blocking", REPRO), ("advisory", None)])

    def test_an_edited_blocker_line_still_holds_its_paraphrase(self) -> None:
        """build/sd-1423 review (bin/sd-review:1814): `return total / count` became `return round(total / count)`,
        so the division by zero stayed, but the changed line held nothing and an uncited paraphrase at the same
        line posted as advisory. A changed blocker line still holds an uncited finding on it."""
        source = [f"line{number}\n" for number in range(1, 41)]
        source[9] = "    return total / count\n"
        (self.root / "src.py").write_text("".join(source), encoding="utf-8")
        old = self.commit("add", "src.py") and self.commit("old")
        source[9] = "    return round(total / count)\n"
        (self.root / "src.py").write_text("".join(source), encoding="utf-8")
        new = self.commit("add", "src.py") and self.commit("new")
        prior = [dict(finding(REPRO, line=10, summary="Empty input divides by zero"), disposition="blocking")]
        [moved] = sd_review.prior_at(self.root, old, new, prior)
        self.assertEqual(moved["line"], 10)
        disposed = sd_review.dispose([finding(line=10, summary="count of zero raises ZeroDivisionError"),
                                      finding(line=11 + sd_review.SHIFT_LINES, summary="the cache is never cleared")], "high",
                                     repro=True, prior=[moved])
        self.assertEqual([row["disposition"] for row in disposed], ["blocking", "advisory"])

    def test_a_partly_fixed_line_holds_a_repeat_one_line_off(self) -> None:
        """build/sd-1423 review (bin/sd-review:1814): a partial fix edited line 20, and a reader cited the same
        unresolved defect at line 21 uncited; the edited line held only itself, so it posted as advisory. An
        edited blocker line holds the same `SHIFT_LINES` window as an unchanged one (fail closed)."""
        source = [f"line{number}\n" for number in range(1, 41)]
        source[19] = "    return total / count\n"
        (self.root / "src.py").write_text("".join(source), encoding="utf-8")
        old = self.commit("add", "src.py") and self.commit("old")
        source[19] = "    return round(total / count)\n"
        (self.root / "src.py").write_text("".join(source), encoding="utf-8")
        new = self.commit("add", "src.py") and self.commit("new")
        prior = [dict(finding(REPRO, line=20, summary="Empty input divides by zero"), disposition="blocking")]
        [record] = sd_review.dispose([finding(line=21, summary="count of zero raises ZeroDivisionError")], "high",
                                     repro=True, prior=sd_review.prior_at(self.root, old, new, prior))
        self.assertEqual(record["disposition"], "blocking")

    def test_a_fixed_blocker_holds_no_finding_past_the_window(self) -> None:
        """build/sd-1423 class pass (bin/sd-review:1816): after the blocker at src.py:10 was fixed, an unrelated
        uncited finding blocked on closeness alone. A fixed line cannot be told from a partial fix
        (bin/sd-review:1814), so it holds only within `SHIFT_LINES` (fail closed); past that, advisory."""
        source = [f"line{number}\n" for number in range(1, 41)]
        (self.root / "src.py").write_text("".join(source), encoding="utf-8")
        old = self.commit("add", "src.py") and self.commit("old")
        source[9] = "fixed10\n"
        (self.root / "src.py").write_text("".join(source), encoding="utf-8")
        new = self.commit("add", "src.py") and self.commit("new")
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")]
        moved = sd_review.prior_at(self.root, old, new, prior)
        [record] = sd_review.dispose([finding(line=11 + sd_review.SHIFT_LINES, summary="the cache is never cleared")],
                                     "high", repro=True, prior=moved)
        self.assertEqual(record["disposition"], "advisory")


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

    def test_a_repeated_prior_blocker_keeps_its_stored_evidence(self) -> None:
        """build/sd-1423 review: fix verification asks a reader to repeat each unresolved prior blocker, and one
        repeated without `evidence` posted as advisory at the head, so the unresolved blocker cleared. The prior
        report's evidence for that path still stands; a finding elsewhere gains none."""
        prior = [dict(finding(REPRO), disposition="blocking")]
        result = {"severity_floor": "high", "requested_reviews": 1}
        answers = [sd_review.Outcome("one", sd_review.FINDINGS, (finding(), finding(path="other.py")), "", (), ())]
        sd_review.finish_review(result, answers, ["one"], repro=True, prior=prior)
        self.assertEqual(result["status"], "blocking")
        self.assertEqual([(row["disposition"], row.get("evidence")) for row in result["findings"]],
                         [("blocking", REPRO), ("advisory", None)])

    def test_prior_evidence_goes_only_to_the_same_finding_in_any_spelling(self) -> None:
        """build/sd-1423 review, then the lead after round 8: evidence moves only to the same file, line and
        summary, the file in any spelling the review accepts. Once one finding repeats the prior blocker, an
        unrelated uncited finding on that file is advisory, as on any file."""
        prior = [dict(finding(REPRO, line=10, summary="the cache is never cleared"), disposition="blocking")]
        rows = [finding(path=path, line=line, summary=summary) for path, line, summary in (
            ("./src.py", 10, "The cache is  never cleared"), ("src.py", 12, "the cache is never cleared"),
            ("src.py", 10, "an unrelated defect"), ("other/src.py", 10, "the cache is never cleared"))]
        disposed = sd_review.dispose(rows, "high", repro=True, prior=prior)
        self.assertEqual([(row["disposition"], row.get("evidence")) for row in disposed],
                         [("blocking", REPRO), ("advisory", None), ("advisory", None), ("advisory", None)])

    def test_a_shifted_paraphrased_repeat_of_a_prior_blocker_still_blocks(self) -> None:
        """Lead after round 8 (bin/sd-review:1797): a patch moved the blocker from line 10 to 12 and the reader
        paraphrased it uncited; the matcher found nothing and it posted as advisory. A prior blocker no
        finding matches keeps every uncited finding on its file blocking (fail closed), with no evidence."""
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")]
        rows = [finding(line=12, summary="An empty list raises IndexError"), finding(path="b.py")]
        disposed = sd_review.dispose(rows, "high", repro=True, prior=prior)
        self.assertEqual([(row["disposition"], row.get("evidence")) for row in disposed],
                         [("blocking", None), ("advisory", None)])

    def test_a_fixed_prior_blocker_does_not_hold_an_unrelated_finding_far_away(self) -> None:
        """build/sd-1423 review (bin/sd-review:1806): the blocker at src.py:10 was fixed, and an unrelated uncited
        finding at src.py:90 blocked by the same-file fallback alone. Only a finding near the prior line can be
        its shifted repeat; one with no line cannot be placed, so it stays blocking."""
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")]
        rows = [finding(line=90, summary="an unrelated defect"), finding(line=10 + sd_review.SHIFT_LINES)]
        disposed = sd_review.dispose(rows, "high", repro=True, prior=prior)
        self.assertEqual([row["disposition"] for row in disposed], ["advisory", "blocking"])

    def test_an_unchanged_blocker_does_not_hold_a_finding_nineteen_lines_away(self) -> None:
        """build/sd-1423 review (bin/sd-review:1816): an unchanged blocker at src.py:20 held an unrelated uncited
        finding at line 39. `prior_at` maps the line through the diff, so the window covers only a reader's
        imprecision about the same code, not a shift."""
        prior = [dict(finding(REPRO, line=20, summary="Empty input crashes"), disposition="blocking")]
        disposed = sd_review.dispose([finding(line=39, summary="the cache is never cleared"),
                                      finding(line=22, summary="An empty list raises IndexError")], "high",
                                     repro=True, prior=prior)
        self.assertEqual([row["disposition"] for row in disposed], ["advisory", "blocking"])

    def test_the_same_summary_on_a_nearby_caller_repeats_the_blocker(self) -> None:
        """build/sd-1423 review (bin/sd-review:1816): a reader cited the unresolved blocker's own summary at a
        caller, line 14, though it mapped to line 10, and the repeat posted as advisory. The same file and
        summary at another line is the repeat when no finding repeats it at its own line; it keeps the repro."""
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")]
        disposed = sd_review.dispose([finding(line=14, summary="empty input  crashes"),
                                      finding(path="b.py", line=10, summary="Empty input crashes")], "high",
                                     repro=True, prior=prior)
        self.assertEqual([(row["disposition"], row.get("evidence")) for row in disposed],
                         [("blocking", REPRO), ("advisory", None)])

    def test_the_same_summary_far_from_the_blocker_gets_no_repro(self) -> None:
        """build/sd-1423 review (bin/sd-review:1809): a blocker at line 20 lent its repro to a separate,
        uncited finding with the same summary at line 200, which then blocked. A summary repeats the blocker
        only within `ECHO_LINES` of its mapped line; farther, it is a finding of its own."""
        prior = [dict(finding(REPRO, line=20, summary="Missing null check"), disposition="blocking")]
        for line, expected in ((200, ("advisory", None)), (20 + sd_review.ECHO_LINES, ("blocking", REPRO))):
            with self.subTest(line=line):
                [record] = sd_review.dispose([finding(line=line, summary="missing null check")], "high",
                                             repro=True, prior=prior)
                self.assertEqual((record["disposition"], record.get("evidence")), expected)

    def test_a_nearby_same_summary_repeat_answers_the_blocker(self) -> None:
        """build/sd-1423 review (bin/sd-review:1822): a blocker at line 10 was repeated by its own summary at
        line 15, which took the repro, yet an unrelated uncited claim at line 11 still blocked, since the
        fallback counted only an exact-line repeat as the answer. Any repeat that takes the repro answers it."""
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")]
        repeat, other = sd_review.dispose([finding(line=15, summary="empty input crashes"),
                                           finding(line=11, summary="the cache is never cleared")],
                                          "high", repro=True, prior=prior)
        self.assertEqual([(row["disposition"], row.get("evidence")) for row in (repeat, other)],
                         [("blocking", REPRO), ("advisory", None)])

    def test_a_line_that_cannot_be_placed_holds_no_nearby_finding(self) -> None:
        """build/sd-1423 review (bin/sd-review:1813): a prior blocker with `line: null` made an unrelated,
        uncited finding at line 200 of its file block after the defect was fixed. A proximity hold needs two
        placed lines that overlap (lead after round 8), so an unplaced line on either side holds nothing; the
        blocker's own summary still repeats it and keeps the repro."""
        for prior_line, line in ((None, 200), (None, 12), (10, None), (None, None)):
            with self.subTest(prior=prior_line, line=line):
                prior = [dict(finding(REPRO, line=prior_line, summary="Empty input crashes"),
                              disposition="blocking")]
                unrelated, repeat = sd_review.dispose([finding(line=line, summary="the cache is never cleared"),
                                                       finding(line=line, summary="empty input crashes")],
                                                      "high", repro=True, prior=prior)
                self.assertEqual([(row["disposition"], row.get("evidence")) for row in (unrelated, repeat)],
                                 [("advisory", None), ("blocking", REPRO)])

    def test_a_repeat_below_the_floor_does_not_clear_the_fallback(self) -> None:
        """build/sd-1423 review (bin/sd-review:1811): one reader's exact repeat at medium, under a high floor,
        counted as the prior blocker's match, so another reader's nearby high paraphrase posted as advisory and
        the unresolved blocker cleared. Only a repeat that blocks at the floor answers a prior blocker."""
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")]
        rows = [finding(severity="medium", line=10, summary="Empty input crashes"),
                finding(line=12, summary="An empty list raises IndexError")]
        disposed = sd_review.dispose(rows, "high", repro=True, prior=prior)
        self.assertEqual([row["disposition"] for row in disposed], ["advisory", "blocking"])

    def test_a_rebutted_prior_blocker_holds_no_other_finding(self) -> None:
        """build/sd-1423 review (bin/sd-review:1814): with a listed rebuttal at src.py:10, an unrelated uncited
        high finding at line 12 blocked: the rebuttal check read the new finding, the proximity check the
        rebutted prior row. A rebutted prior blocker is answered and holds nothing."""
        rebutted = listed(adjudicated(("src.py", "Empty input crashes", "rebutted"), line=10))
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")]
        [record] = sd_review.dispose([finding(line=12, summary="the cache is never cleared")], "high", rebutted,
                                     repro=True, prior=prior)
        self.assertEqual(record["disposition"], "advisory")

    def test_a_row_is_one_row_per_place_it_names(self) -> None:
        """build/sd-1623 review (bin/sd-review:1833): odd entries name no place and never raise. Class pass on
        bin/sd-review:1838: a place row carries no `instances`, so no decision at it reads another place, and an
        instance's file is the one `sd_lib.instance_files` gives the reader."""
        entries = ["src.py:10", "b.py:40", "b.py:040", " c.py ", "d.py:\u0663", "e.py:4\n", "", "  ", None, 7, ["x"],
                   "f.py:" + "9" * 4301, "g\u00e9.py:2", "h.py:0", ":7"]
        blocker = dict(finding(REPRO, line=10, summary="s"), disposition="blocking", instances=entries)
        rows = sd_review.instance_rows(blocker)
        self.assertEqual([(row["path"], row["line"]) for row in rows],
                         [("src.py", 10), ("b.py", 40), ("c.py", None), ("d.py:\u0663", None), ("e.py", 4),
                          ("f.py", None), ("g\u00e9.py", 2), ("h.py", 0)])
        self.assertEqual({row["path"] for row in rows}, set(sd_review.sd_lib.instance_files(entries)) - {""})
        self.assertFalse(any("instances" in row for row in rows))
        self.assertEqual(sd_review.instance_rows(rows[1]), [rows[1]])
        self.assertEqual(sd_review.instance_rows(dict(finding(line=3), instances="src.py:1")), [finding(line=3)])
        # build/sd-1623 review (bin/sd-review:1844): a stored place that is not JSON-scalar is no key error.
        odd = dict(blocker, path=["src.py"], line=[], instances=["b.py:40"])
        self.assertEqual([row["path"] for row in sd_review.instance_rows(odd)], [["src.py"], "b.py"])
        for line in (10, None, [], 10 ** 400, True, float("nan"), "10"):  # no stored or reported place raises
            [record] = sd_review.dispose([dict(finding(line=10, summary="s"), instances=entries)], "high", repro=True,
                                         prior=[dict(odd, line=line, instances=entries)])
            self.assertEqual(record["disposition"], "blocking", line)

    def test_a_rebuttal_covering_an_instance_answers_its_repeat_there(self) -> None:
        """build/sd-1623 review (bin/sd-review:1779, :1791): a rebuttal at src.py:10 that answered both
        src.py:10 and b.py:40. An uncited repeat at b.py:40 blocked with the old repro: the expanded prior row
        and the finding sit at b.py:40, and the rebuttal was matched at its reported place alone."""
        body = json.dumps({"decision": "accepted", "proposal": {"bindings": {"head": ON_BRANCH}, "findings": [
            {"response_disposition": "rebutted", "reason": "a reason", "raw_finding": {
                "path": "src.py", "line": 10, "severity": "high", "instances": ["src.py:10", "b.py:40"],
                "summary": "Empty input crashes", "family": "correctness"}}]}})
        both = {"instances": ["src.py:10", "b.py:40"]}
        blocker = dict(finding(REPRO, line=10, summary="Empty input crashes", **both), disposition="blocking")
        [record] = sd_review.dispose([finding(path="b.py", line=40, summary="Empty input crashes",
                                              instances=["b.py:40"])], "high", listed(body), repro=True,
                                     prior=sd_review.instance_rows(blocker))
        self.assertEqual((record["disposition"], record.get("evidence")), ("advisory", None))

    def test_a_newer_record_at_one_instance_overrides_an_older_rebuttal(self) -> None:
        """build/sd-1623 review (bin/sd-review:885): a rebuttal covering src.py:10 and b.py:40, then a newer
        record keeping the defect at b.py:40 blocking. A repeat listing both skipped the newer record, as it
        covers only b.py:40, and the older rebuttal cleared the prior blocker. The newest overlapping record
        decides, and it holds a finding back only when it covers every place the finding names."""
        both = ["src.py:10", "b.py:40"]
        older = adjudicated(("src.py", "Empty input crashes", "rebutted"), line=10, instances=both)
        newer = adjudicated(("b.py", "Empty input crashes", "accepted"), line=40)
        blocker = dict(finding(REPRO, line=10, summary="Empty input crashes", instances=both), disposition="blocking")
        [record] = sd_review.dispose([finding(line=10, summary="Empty input crashes", instances=both)], "high",
                                     listed(newer, older), repro=True, prior=sd_review.instance_rows(blocker))
        self.assertEqual((record["disposition"], record.get("evidence")), ("blocking", REPRO))

    def test_a_rebuttal_that_misses_an_instance_leaves_the_prior_blocker(self) -> None:
        """build/sd-1623 review (bin/sd-review:1779): a rebuttal at src.py:10, then a cited blocker with one more
        instance at b.py:40. Its uncited repeat listing both posted as advisory: the prior filter matched the
        old rebuttal without the instances, as the finding's own rebuttal check already reads them."""
        rebutted = listed(adjudicated(("src.py", "Empty input crashes", "rebutted"), line=10))
        both = {"instances": ["src.py:10", "b.py:40"]}
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes", **both), disposition="blocking")]
        [record] = sd_review.dispose([finding(line=10, summary="Empty input crashes", **both)], "high", rebutted,
                                     repro=True, prior=prior)
        self.assertEqual((record["disposition"], record.get("evidence")), ("blocking", REPRO))

    def test_a_repeat_at_one_place_leaves_every_other_place_holding(self) -> None:
        """build/sd-1623 review (bin/sd-review:1838), class pass: "Empty input crashes" at a.py:10 and b.py:40,
        repeated at a.py:10 and paraphrased at b.py:41. The repeat answered the whole blocker, so the paraphrase
        posted as advisory. Each place holds alone, and one finding place repeats the nearest prior place only."""
        def held(places: list[str], *rows: dict[str, Any]) -> list[str]:
            path, _, line = places[0].partition(":")
            blocker = dict(finding(REPRO, path=path, line=int(line), summary="Empty input crashes",
                                   instances=places), disposition="blocking")
            return [row["disposition"] for row in sd_review.dispose(list(rows), "high", repro=True, prior=[blocker])]
        paraphrase = "An empty list raises IndexError"
        for places, repeat, other in ((["a.py:10", "b.py:40"], ("a.py", 10), ("b.py", 41)),
                                      (["src.py:10", "src.py:14"], ("src.py", 10), ("src.py", 15)),
                                      (["src.py:10", "src.py:14"], ("src.py", 11), ("src.py", 15))):
            with self.subTest(places=places, repeat=repeat):
                self.assertEqual(held(places, finding(path=repeat[0], line=repeat[1], summary="Empty input crashes"),
                                      finding(path=other[0], line=other[1], summary=paraphrase)),
                                 ["blocking", "blocking"])
        # Repeated at each place, the blocker is answered: an unrelated finding beside one is no longer held.
        self.assertEqual(held(["src.py:10", "src.py:14"], finding(line=10, summary="Empty input crashes"),
                              finding(line=14, summary="Empty input crashes"), finding(line=15, summary=paraphrase)),
                         ["blocking", "blocking", "advisory"])

    def test_every_place_is_held_until_a_repeat_at_that_place(self) -> None:
        """Property for the class of bin/sd-review:1838, over every subset of repeated places, across files, far
        apart in one file and inside one echo window: a paraphrase beside a place is held exactly when no
        finding repeats that place, whether each repeat is a finding of its own or one finding lists them all."""
        places = [("a.py", 10), ("a.py", 20), ("a.py", 90), ("b.py", 40)]
        blocker = dict(finding(REPRO, path="a.py", line=10, summary="Empty input crashes",
                               instances=[f"{path}:{line}" for path, line in places]), disposition="blocking")
        beside = [finding(path=path, line=line + 1, summary="An empty list raises IndexError") for path, line in places]
        for repeated in (chosen for count in range(len(places) + 1) for chosen in itertools.combinations(places, count)):
            each = [finding(path=path, line=line, summary="Empty input crashes") for path, line in repeated]
            listing = [dict(each[0], instances=[f"{path}:{line}" for path, line in repeated])] if each else []
            for repeats in (each, listing):
                with self.subTest(repeated=repeated, findings=len(repeats)):
                    disposed = sd_review.dispose(repeats + beside, "high", repro=True, prior=[blocker])
                    self.assertEqual([row["disposition"] for row in disposed], ["blocking"] * len(repeats) + [
                        "advisory" if place in repeated else "blocking" for place in places])

    def test_one_finding_place_releases_at_most_one_hold(self) -> None:
        """build/sd-1623 review (bin/sd-review:1841), class pass: with a.py:10 and a.py:14 recorded, a repeat at
        line 12 is as near one as the other, and it answered both, so a paraphrase at line 15 posted as
        advisory. Property over every repeat line: it releases the hold of the one place strictly nearest it,
        within `ECHO_LINES`, and of neither on a tie; it blocks wherever it echoes a place."""
        blocker = dict(finding(REPRO, path="a.py", line=10, summary="Empty input crashes",
                               instances=["a.py:10", "a.py:14"]), disposition="blocking")
        for line in [None, *range(-2, 30)]:
            with self.subTest(line=line):
                repeat, low, high = sd_review.dispose([
                    finding(path="a.py", line=line, summary="Empty input crashes"),
                    finding(path="a.py", line=9, summary="An empty list raises IndexError"),
                    finding(path="a.py", line=15, summary="An empty list raises IndexError")],
                    "high", repro=True, prior=[blocker])
                gaps = [abs(line - place) if line is not None else 99 for place in (10, 14)]
                echoes = line is None or min(gaps) <= sd_review.ECHO_LINES
                self.assertEqual((repeat["disposition"], repeat.get("evidence")),
                                 ("blocking", REPRO) if echoes else ("advisory", None))
                self.assertEqual([low["disposition"], high["disposition"]], [
                    "advisory" if echoes and mine < other else "blocking" for mine, other in (gaps, gaps[::-1])])

    def test_only_a_finding_that_blocks_releases_a_hold(self) -> None:
        """build/sd-1623 review (bin/sd-review:1783), class pass: a.py:10 and a.py:14 recorded, line 10 alone
        rebutted. The repeat at line 10 posted as advisory, yet with its own place dropped it echoed line 14
        and answered it, so a paraphrase at line 15 posted as advisory and the review passed. A rebutted repeat
        answers nothing, and a rebutted place is still the place a repeat on it repeats."""
        rebutted = listed(adjudicated(("a.py", "Empty input crashes", "rebutted"), line=10))
        prior = [dict(finding(REPRO, path="a.py", line=10, summary="Empty input crashes",
                              instances=["a.py:10", "a.py:14"]), disposition="blocking")]
        paraphrase = finding(path="a.py", line=15, summary="An empty list raises IndexError")
        for name, repeat, expected in (
                ("uncited", finding(path="a.py", line=10, summary="Empty input crashes"), "advisory"),
                ("cited", finding("python3 t.py: fails", path="a.py", line=10, summary="Empty input crashes"), "blocking"),
                ("shifted", finding(path="a.py", line=11, summary="Empty input crashes"), "blocking")):
            with self.subTest(repeat=name):
                disposed = sd_review.dispose([repeat, paraphrase], "high", rebutted, repro=True, prior=prior)
                self.assertEqual([row["disposition"] for row in disposed], [expected, "blocking"])
        # A repeat at the open place releases it; the rebutted place's repro is never lent.
        repeat, other = sd_review.dispose([finding(path="a.py", line=14, summary="Empty input crashes"), paraphrase],
                                          "high", rebutted, repro=True, prior=prior)
        self.assertEqual([repeat["disposition"], repeat.get("evidence"), other["disposition"]], ["blocking", REPRO, "advisory"])

    def test_every_released_hold_has_a_blocking_repeat_of_its_own(self) -> None:
        """Property for the class of bin/sd-review:1841 and :1783, over random places, repeats, citations and
        rebuttals in one file: a hold is released when an uncited probe on its line posts as advisory. The
        released lines of a summary never outnumber the blocking finding places of that summary, and each has
        one within `ECHO_LINES`; so no repeat releases two holds, and none that does not block releases one."""
        random = __import__("random").Random(9867)
        for case in range(400):
            summaries = ["Empty input crashes", "Missing null check"]
            prior = [dict(finding(REPRO, path="a.py", line=random.randint(1, 30), summary=random.choice(summaries)),
                          disposition="blocking") for _ in range(random.randint(1, 4))]
            rebutted = listed(*(adjudicated(("a.py", row["summary"], "rebutted"), line=row["line"])
                                for row in prior if random.random() < 0.3))
            rows = [finding(*(["python3 t.py: fails"] if random.random() < 0.3 else []), path="a.py",
                            line=random.choice([None, *range(1, 31)]), summary=random.choice(summaries))
                    for _ in range(random.randint(0, 4))]
            blocking = [row for row in sd_review.dispose(rows, "high", rebutted, repro=True, prior=prior)
                        if row["disposition"] == "blocking"]
            for summary in summaries:
                repeats = [row["line"] for row in blocking if sd_review.summary_key(row["summary"]) == sd_review.summary_key(summary)]
                held = {row["line"] for row in prior if row["summary"] == summary and not any(
                    record.listed and (record.path, record.line, record.summary) == ("a.py", row["line"], sd_review.summary_key(summary))
                    for record in rebutted)}
                released = {line for line in held if sd_review.dispose(
                    [*rows, finding(path="a.py", line=line, summary="a probe of this line")], "high", rebutted,
                    repro=True, prior=prior)[-1]["disposition"] == "advisory"}
                self.assertLessEqual(len(released), len(repeats), (case, prior, rows))
                for line in released:
                    self.assertTrue(any(at is None or abs(at - line) <= sd_review.ECHO_LINES for at in repeats), (case, line))

    def test_a_finding_repeats_a_prior_blocker_at_any_place_it_names(self) -> None:
        """Class pass on bin/sd-review:1838: the matcher read a finding's reported place alone, so an uncited
        repeat reported at a new place, c.py:5, that lists the prior blocker's b.py:40 as an instance posted as
        advisory. A finding is matched at each place it names, as a prior blocker is."""
        prior = [dict(finding(REPRO, path="b.py", line=40, summary="Empty input crashes"), disposition="blocking")]
        for summary, instance, expected in (("Empty input crashes", "b.py:40", ("blocking", REPRO)),
                                            ("An empty list raises IndexError", "b.py:41", ("blocking", None)),
                                            ("An empty list raises IndexError", "b.py:90", ("advisory", None))):
            with self.subTest(instance=instance):
                [record] = sd_review.dispose([finding(path="c.py", line=5, summary=summary,
                                                      instances=["c.py:5", instance])], "high", repro=True, prior=prior)
                self.assertEqual((record["disposition"], record.get("evidence")), expected)
        # A repeat that names the place, at the floor, answers it for a finding beside it.
        repeat, other = sd_review.dispose([
            finding(path="c.py", line=5, summary="Empty input crashes", instances=["c.py:5", "b.py:40"]),
            finding(path="b.py", line=41, summary="the cache is never cleared")], "high", repro=True, prior=prior)
        self.assertEqual([repeat["disposition"], other["disposition"]], ["blocking", "advisory"])

    def test_a_rebuttal_answers_only_the_places_it_covers(self) -> None:
        """Class pass on bin/sd-review:1838: a rebuttal at a.py:10 and a blocker at a.py:10 and b.py:40. The
        rebutted place holds no finding beside it; the place the rebuttal does not cover still does, and still
        lends its repro to a repeat that names it."""
        rebutted = listed(adjudicated(("a.py", "Empty input crashes", "rebutted"), line=10))
        both = ["a.py:10", "b.py:40"]
        prior = [dict(finding(REPRO, path="a.py", line=10, summary="Empty input crashes", instances=both),
                      disposition="blocking")]
        disposed = sd_review.dispose([finding(path="a.py", line=12, summary="the cache is never cleared"),
                                      finding(path="b.py", line=41, summary="the cache is never cleared"),
                                      finding(path="b.py", line=40, summary="Empty input crashes")],
                                     "high", rebutted, repro=True, prior=prior)
        self.assertEqual([(row["disposition"], row.get("evidence")) for row in disposed],
                         [("advisory", None), ("advisory", None), ("blocking", REPRO)])
        [alone] = sd_review.dispose([finding(path="b.py", line=41, summary="the cache is never cleared")], "high",
                                    rebutted, repro=True, prior=prior)
        self.assertEqual(alone["disposition"], "blocking")

    def test_the_same_line_alone_gives_no_prior_evidence(self) -> None:
        """Lead after round 8: the matcher gave the old repro to an unrelated cache finding at the same line."""
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")]
        [record] = sd_review.dispose([finding(line=10, summary="the cache is never cleared")], "high",
                                     repro=True, prior=prior)
        self.assertEqual((record["disposition"], record.get("evidence")), ("blocking", None))
        repeat, other = sd_review.dispose([finding(line=10, summary="Empty input crashes"),
                                           finding(line=10, summary="the cache is never cleared")],
                                          "high", repro=True, prior=prior)
        self.assertEqual([(row["disposition"], row.get("evidence")) for row in (repeat, other)],
                         [("blocking", REPRO), ("advisory", None)])

    def test_a_blocker_a_later_completed_pass_cleared_holds_no_finding(self) -> None:
        """Lane round 2 at 1c528acb0 (pack sd:10008): pass 1 blocked on src.py:10 "Empty input crashes", pass 2
        completed and cleared it, yet an uncited "the cache is never cleared" at line 10 still blocked. Only a
        blocker still open at the last completed pass holds a nearby finding; one still open keeps the 3-line
        fail-closed rule (ruling #72671), and a pass that did not complete clears nothing."""
        first, second = ({"pass": index, "head": head * 40, "completed": True} for index, head in ((1, "a"), (2, "b")))
        blocker = dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")
        new = finding(line=10, summary="the cache is never cleared")
        for label, history, found, expected in (
                ("cleared by pass 2", [first, second], first, "advisory"),
                ("found at pass 2", [first, second], second, "blocking"),
                ("pass 2 incomplete", [first, dict(second, completed=False)], first, "blocking"),
                ("no completion recorded", [{"pass": 1}, {"pass": 2}], {"pass": 1}, "blocking"),
                ("a single report", None, None, "blocking")):
            with self.subTest(label):
                previous = {"findings": [dict(blocker, **({"prior_review": found} if found else {}))],
                            **({"history": history} if history else {})}
                [record] = sd_review.dispose([new], "high", repro=True, prior=sd_review.unresolved(previous))
                self.assertEqual(record["disposition"], expected)

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

    def test_a_path_matches_only_in_its_one_spelling(self) -> None:
        """build/sd-1423 class pass: the matcher also took any absolute path ending in the prior file's name, so a
        finding at `/elsewhere/src.py`, outside the repository, gained the repro of `src.py`. `repo_spelling`
        gives every path inside the repository one spelling before any is matched, so only equal paths match."""
        prior = [dict(finding(REPRO), disposition="blocking")]
        for path in ("/elsewhere/src.py", "/work/repo/src.py"):
            with self.subTest(path=path):
                [record] = sd_review.dispose([finding(path=path)], "high", repro=True, prior=prior)
                self.assertEqual((record["disposition"], record.get("evidence")), ("advisory", None))
        [record] = sd_review.dispose([finding()], "high", repro=True, prior=prior)
        self.assertEqual((record["disposition"], record.get("evidence")), ("blocking", REPRO))

    def test_one_spelling_for_every_accepted_form(self) -> None:
        root = pathlib.Path("/work/repo")
        spelled = sd_review.repo_spelling(root)
        self.assertEqual([spelled(path) for path in ("/work/repo/src.py", "./src.py", " src.py ", "src.py",
                                                     "<review-response>", "/elsewhere/src.py", None)],
                         ["src.py"] * 4 + ["<review-response>", "/elsewhere/src.py", None])

    def test_a_repeat_of_a_prior_blocker_without_stored_evidence_still_blocks(self) -> None:
        """build/sd-1423 review: a report from before sd:9867 stored no evidence, so the verifier's uncited
        repeat found none to keep and cleared the unresolved blocker; a resumed review did the same."""
        prior = [dict(finding(), disposition="blocking"), dict(finding(path="b.py"), disposition="advisory")]
        disposed = sd_review.dispose([finding(), finding(path="b.py")], "high", repro=True, prior=prior)
        self.assertEqual([row["disposition"] for row in disposed], ["blocking", "advisory"])

    def test_an_uncited_repeat_of_a_rebutted_prior_blocker_stays_rebutted(self) -> None:
        """build/sd-1423 review: the prior report's repro was copied on before the rebuttal check, so a repeat
        of a blocker the lead rebutted blocked again on every verification pass."""
        rebutted = listed(adjudicated(("src.py", "defect", "rebutted")))
        prior = [dict(finding(REPRO), disposition="blocking")]
        [record] = sd_review.dispose([finding()], "high", rebutted, repro=True, prior=prior)
        self.assertEqual((record["disposition"], record["summary"], record.get("evidence")),
                         ("advisory", f"defect {sd_review.REPEAT_NOTE}", None))

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

    def test_a_synthetic_repro_releases_no_prior_hold(self) -> None:
        """sd:10288: a synthetic finding beside a prior blocker neither answers its hold nor posts as advisory."""
        prior = [dict(finding(REPRO), disposition="blocking")]
        [record] = sd_review.dispose([finding(REPRO, trigger="synthetic", line=2, summary="other")],
                                     "high", repro=True, prior=prior)
        self.assertEqual(record["disposition"], "blocking")

    def test_a_synthetic_repeat_of_a_held_prior_blocker_still_blocks(self) -> None:
        """sd:10288 review (bin/sd-review:1798): a reader cleared an unresolved blocker by repeating it with
        `trigger: synthetic`; the downgrade skipped the prior evidence check."""
        prior = [dict(finding(REPRO), disposition="blocking")]
        for evidence in (None, "python3 t.py: still 2"):
            with self.subTest(evidence=evidence):
                [record] = sd_review.dispose([finding(evidence, trigger="synthetic")], "high", repro=True, prior=prior)
                self.assertEqual((record["disposition"], record["summary"], record["evidence"]),
                                 ("blocking", "defect", evidence or REPRO))

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
        self.adjudication(("lane claim", "rebutted", "x"), head=head, instances=["bin/tool.py:3", "./src.py:1"],
                          evidence=REPRO)  # the ruling answered this blocker's repro; another repro is new to it
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
        and an uncited repeat posted as advisory. The aggregate records no pass status, so every pass counts."""
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
        self.assertEqual([r["disposition"] for r in result["findings"] if r["backend"] == "codex"], ["blocking"])

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

#: A receipt head `listed` takes for one on the reviewed branch.
ON_BRANCH = "c" * 40


def adjudicated(*rows: tuple[Any, ...], decision: str = "accepted", line: Any = 1, instances: Any = None) -> str:
    """One adjudication body at `ON_BRANCH`: each (path, summary, disposition[, line]) a blocking finding's answer."""
    return json.dumps({"decision": decision, "proposal": {"bindings": {"head": ON_BRANCH}, "findings": [
        {"response_disposition": answer, "reason": "a reason",
         "raw_finding": {"path": path, "line": at, "severity": "high", "summary": summary, "family": "correctness",
                         "instances": instances or [f"{path}:{at}" if type(at) is int else str(path)]}}  # its own place
        for path, summary, answer, *rest in rows for at in [(*rest, line)[0]]]}})


def listed(*bodies: str) -> list[Any]:
    """The rebuttals in `bodies`, newest first, as the prompt lists them."""
    return sd_review.rebuttals([(body,) for body in bodies], {ON_BRANCH}.__contains__, lambda _head, _path, line: (line,))


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

    def test_a_rebuttal_lapses_when_an_instance_file_changes(self) -> None:
        """Review of sd:9769: a later commit can turn an instance, not the primary file, into the branch's own
        regression; a rebuttal holds only while every file the finding names is unchanged."""
        body = adjudicated(("bin/tool.py", "defect", "rebutted"), instances=["bin/tool.py:1", "src.py:1"])
        seen = sd_review.rebuttals([(body,)], {ON_BRANCH}.__contains__, lambda _head, path, line: None if path == "src.py" else (line,))
        self.assertEqual([entry.listed for entry in seen], [False], "a changed instance file kept the rebuttal")

    def test_a_newer_ruling_that_cannot_be_placed_decides_for_its_file(self) -> None:
        """Review of sd:10191: a parked ruling whose hunk changed since its receipt has no line to compare, so an
        older rebuttal of the same summary in a file both name is not listed; one in another file still is."""
        newer = adjudicated(("src.py", "defect", "parked", 30), ("lib.py", "other defect", "parked", 30))
        older = adjudicated(("src.py", "defect", "rebutted", 20), ("src.py", "other defect", "rebutted", 20))
        seen = sd_review.rebuttals([(newer,), (older,)], {ON_BRANCH}.__contains__,
                                   lambda _head, _path, line: None if line == 30 else (line,))
        self.assertEqual([(entry.summary, entry.line, entry.listed) for entry in seen],
                         [("defect", 30, False), ("other defect", 30, False), ("defect", 20, False),
                          ("other defect", 20, True)])
        self.assertEqual([entry.listed for entry in sd_review.rebuttals(
            [(adjudicated(([], "defect", "rebutted", 20)),)], {ON_BRANCH}.__contains__,
            lambda _head, _path, line: (line,))], [True], "a path that is no text broke the comparison")

    def test_a_finding_that_names_a_new_instance_file_is_not_the_rebutted_one(self) -> None:
        """Review of sd:9769: a fix that spreads the defect to `new.py` keeps the primary place and summary;
        the rebuttal named no `new.py`, so it is a new finding and blocks."""
        body = adjudicated(("src.py", "defect", "rebutted"))  # its instance: src.py:1
        self.assertEqual(gate([row(instances=["src.py:1", "new.py:4"])], body), ["blocking"])
        self.assertEqual(gate([row(instances=["src.py:1"])], body), ["advisory"])
        # Review of sd:9769: a new occurrence in a file the rebuttal named is still not adjudicated.
        self.assertEqual(gate([row(instances=["src.py:1", "src.py:60"])], body), ["blocking"])

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

    def test_a_ruling_holds_while_a_later_change_leaves_its_hunk_alone(self) -> None:
        """sd:10191: a catch-up merge of main that edits the ruled file elsewhere keeps the ruling, at the line
        its code moved to; an edit within `SHIFT_LINES` of that line lapses it, as does one at an instance."""
        tool, other = self.root / "bin/tool.py", self.root / "lib.py"
        tool.parent.mkdir()
        tool.write_text("".join(f"line_{n} = {n}\n" for n in range(1, 31)), encoding="utf-8")
        other.write_text("".join(f"line_{n} = {n}\n" for n in range(1, 31)), encoding="utf-8")
        self.commit("add", "bin/tool.py", "lib.py")
        ruled = self.commit("the ruled code")
        self.adjudication(("lane claim", "rebutted", "x"), head=ruled, line=20, instances=["bin/tool.py:20", "lib.py:10"])

        def seen() -> list[tuple[str, int | None, tuple[str, ...]]]:
            found = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, "branch"), None,
                                                self.environment())
            return [(entry.text, entry.line, entry.instances) for entry in found if entry.listed]

        text = tool.read_text(encoding="utf-8").replace("line_2 = 2\n", "line_2 = 22\nadded = 1\nadded = 2\n")
        tool.write_text(text, encoding="utf-8")
        self.commit("add", "bin/tool.py")
        self.commit("a catch-up merge edits line 2")
        self.assertEqual(seen(), [("lane claim", 22, ("bin/tool.py:22", "lib.py:10"))])
        other.write_text(other.read_text(encoding="utf-8").replace("line_12 = 12", "line_12 = 0"), encoding="utf-8")
        self.commit("add", "lib.py")
        self.commit("an edit two lines from the instance")
        self.assertEqual(seen(), [], "an edit at an instance's hunk kept the ruling")

    def test_a_finding_at_a_new_place_is_not_covered_by_an_old_rebuttal(self) -> None:
        """Lane ruling on sd:10191 (bin/sd-review:885): a distant edit keeps a rebuttal at its own line, but the
        distant edit is its own hunk; the same summary found there is a new place no rebuttal covers, so it
        blocks, while the repeat at the ruled line stays held back."""
        tool = self.root / "bin/tool.py"
        tool.parent.mkdir()
        tool.write_text("".join(f"line_{n} = {n}\n" for n in range(1, 61)), encoding="utf-8")
        self.commit("add", "bin/tool.py")
        ruled = self.commit("the ruled code")
        self.adjudication(("lane claim", "rebutted", "x"), head=ruled, line=20)
        tool.write_text(tool.read_text(encoding="utf-8").replace("line_50 = 50", "line_50 = crash()"),
                        encoding="utf-8")
        self.commit("add", "bin/tool.py")
        self.commit("a distant edit")
        found = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, "branch"), None,
                                            self.environment())
        self.assertEqual([(entry.listed, entry.line) for entry in found], [(True, 20)])
        answers = self.review_answering(row(path="bin/tool.py", line=50, summary="lane claim"))
        self.assertEqual(answers["lane claim"][0], "blocking")

    def test_a_distant_edit_that_makes_a_rebutted_crash_real_blocks_once_cited(self) -> None:
        """Class pass of sd:10191 (bin/sd-review:888): a distant edit keeps the rebuttal at its own line, and may
        make the rebutted crash real. The ruling holds back only a repeat that cites nothing: one that cites the
        check it ran blocks at the ruled line, and the uncited one beside it stays advisory."""
        tool = self.root / "bin/tool.py"
        tool.parent.mkdir()
        tool.write_text("".join(f"line_{n} = {n}\n" for n in range(1, 61)), encoding="utf-8")
        self.commit("add", "bin/tool.py")
        ruled = self.commit("the ruled code")
        self.adjudication(("lane claim", "rebutted", "x"), head=ruled, line=20)
        self.adjudication(("other claim", "rebutted", "x"), head=ruled, line=30)
        tool.write_text(tool.read_text(encoding="utf-8").replace("line_50 = 50", "line_50 = crash()"),
                        encoding="utf-8")
        self.commit("add", "bin/tool.py")
        self.commit("a distant edit")
        answers = self.review_answering(
            row(path="bin/tool.py", line=20, summary="lane claim", evidence=REPRO),
            row(path="bin/tool.py", line=30, summary="other claim"))
        self.assertEqual((answers["lane claim"][0], answers["other claim"][0]), ("blocking", "advisory"))

    def test_a_ruling_lapses_when_its_own_hunk_changes(self) -> None:
        tool = self.root / "bin/tool.py"
        tool.parent.mkdir()
        tool.write_text("".join(f"line_{n} = {n}\n" for n in range(1, 31)), encoding="utf-8")
        self.commit("add", "bin/tool.py")
        ruled = self.commit("the ruled code")
        self.adjudication(("lane claim", "rebutted", "x"), head=ruled, line=20)
        tool.write_text(tool.read_text(encoding="utf-8").replace("line_23 = 23", "line_23 = 0"), encoding="utf-8")
        self.commit("add", "bin/tool.py")
        self.commit("an edit three lines below the ruling")
        found = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, "branch"), None,
                                            self.environment())
        self.assertEqual([entry.listed for entry in found], [False])

    def test_a_textconv_filter_cannot_hide_an_edit_at_the_ruled_line(self) -> None:
        """Review of sd:10191 (bin/sd-review:871): a `diff=` attribute's textconv may drop the ruled line from
        both sides, so the diff shows no hunk there; the ruling must read the raw source and lapse."""
        tool = self.root / "bin/tool.py"
        tool.parent.mkdir()
        tool.write_text("".join(f"line_{n} = {n}\n" for n in range(1, 31)), encoding="utf-8")
        (self.root / ".gitattributes").write_text("*.py diff=hide\n", encoding="utf-8")
        self.commit("config", "diff.hide.textconv", "sed /line_20/d")
        self.commit("add", "bin/tool.py", ".gitattributes")
        ruled = self.commit("the ruled code")
        self.adjudication(("lane claim", "rebutted", "x"), head=ruled, line=20)
        tool.write_text(tool.read_text(encoding="utf-8").replace("line_20 = 20", "line_20 = crash()"),
                        encoding="utf-8")
        self.commit("add", "bin/tool.py")
        self.commit("an edit at the ruled line")
        for scope in ("worktree", "branch"):
            with self.subTest(scope=scope):
                found = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, scope), None,
                                                    self.environment())
                self.assertEqual([entry.listed for entry in found], [False])

    def test_hunk_kept_holds_on_doubt(self) -> None:
        """A ruling without a line, a diff with no hunk (binary, mode) or with two files' hunks (a path that names
        a directory) lapses; a hunk past the window moves it."""
        def diff(*hunks: str, files: int = 1) -> str:
            return "\n".join(["diff --git a/x b/x"] * files + list(hunks))
        self.assertIsNone(sd_review.hunk_kept(diff("@@ -1 +1 @@\n-a\n+b"), None))
        self.assertIsNone(sd_review.hunk_kept(diff("Binary files a/x and b/x differ"), 20))
        self.assertIsNone(sd_review.hunk_kept(diff("@@ -17,0 +18,2 @@\n+a\n+b"), 20))
        self.assertEqual(sd_review.hunk_kept(diff("@@ -16,0 +17,2 @@\n+a\n+b"), 20), (22,))
        self.assertEqual(sd_review.hunk_kept(diff("@@ -24,2 +23,0 @@\n-a\n-b"), 20), (20,))
        self.assertIsNone(sd_review.hunk_kept(diff("@@ -23,2 +22,0 @@\n-a\n-b"), 20))
        self.assertIsNone(sd_review.hunk_kept(diff("@@ -1 +1 @@\n-a\n+b", files=2), 20), "two files' hunks moved a line")
        self.assertIsNone(sd_review.hunk_kept("@@ -1 +1 @@\n-a\n+b", 20), "hunks of no named file moved a line")
        # Lead review of sd:10191 (bin/sd-review:883): a mode change with a distant content edit kept the ruling.
        for header in ("old mode 100644\nnew mode 100755", "deleted file mode 100644", "new file mode 100644"):
            self.assertIsNone(sd_review.hunk_kept(diff(header, "@@ -1 +1 @@\n-a\n+b"), 20), header)

    def test_a_newer_ruling_decides_where_its_place_is_now(self) -> None:
        """Review of sd:10191: `defect` was rebutted at line 20; two lines came in above and it was parked at 22;
        three more came in. Both rulings sit at 25 now, and the newer one, parked, blocks. A newer ruling whose
        hunk changed since cannot be placed, so it decides for every place in its file."""
        tool = self.root / "bin/tool.py"
        tool.parent.mkdir()
        tool.write_text("".join(f"line_{n} = {n}\n" for n in range(1, 31)), encoding="utf-8")
        self.commit("add", "bin/tool.py")
        ruled = self.commit("the ruled code")

        def above(count: int, message: str) -> str:
            tool.write_text("".join(f"{message.split()[0]}_{n} = 0\n" for n in range(count))
                            + tool.read_text(encoding="utf-8"), encoding="utf-8")
            self.commit("add", "bin/tool.py")
            return self.commit(message)

        def seen() -> list[tuple[str, bool, int | None]]:
            found = sd_review.rebutted_findings(self.root, sd_review.resolve_subject(self.root, "branch"), None,
                                                self.environment())
            return [(entry.reason, entry.listed, entry.line) for entry in found]

        self.adjudication(("lane claim", "rebutted", "older"), head=ruled, line=20, key="older", days=0.5)
        parked = above(2, "first catch-up merge")
        self.assertEqual(seen(), [("older", True, 22)])
        self.adjudication(("lane claim", "parked", "newer"), head=parked, line=22, key="newer", days=0.25)
        above(3, "second catch-up merge")
        self.assertEqual(seen(), [("newer", False, 25), ("older", False, 25)])
        self.assertEqual(self.review_answering(row(path="bin/tool.py", line=25, summary="lane claim"))["lane claim"][0],
                         "blocking")
        tool.write_text(tool.read_text(encoding="utf-8").replace("line_20 = 20", "line_20 = 0"), encoding="utf-8")
        self.commit("add", "bin/tool.py")
        self.commit("the ruled line changes")
        self.assertEqual([entry[1] for entry in seen()], [False, False])

    def test_a_newer_ruling_at_an_instance_decides_for_the_primary_place(self) -> None:
        """Lead review of sd:10191 (bin/sd-review:968): a rebuttal at src.py:10 with an instance at b.py:40, then
        a newer accepted ruling at b.py:40 only. Placement read the newest record at src.py:10 alone and listed
        the older rebuttal; the gate reads every place it names and found the newer ruling. Both read all."""
        def ruling(path: str, line: int, listed: bool, instances: tuple[str, ...]) -> Any:
            return sd_review.Rebuttal("head", path, "empty input crashes", line, listed, path, "x", "y", instances)
        newer = ruling("b.py", 40, False, ("b.py:40",))
        older = ruling("src.py", 10, True, ("src.py:10", "b.py:40"))
        placed = sd_review.rulings_placed([newer, older], lambda _head, _path, line: (line,))
        self.assertEqual([entry.listed for entry in placed], [False, False])

    def test_a_newer_ruling_in_another_spelling_decides_for_an_older_rebuttal(self) -> None:
        """Review of sd:10191 (bin/sd-review:967): a newer parked ruling at `./src.py:20` and an older
        rebuttal at `src.py:20` name one place, but placement compared the raw spellings and listed the
        older rebuttal. Each ruling is read in `repo_spelling`'s one spelling first."""
        def ruling(path: str, listed: bool) -> Any:
            return sd_review.Rebuttal("head", path, "s", 20, listed, path, "x", "y", (f"{path}:20",))
        spelled = sd_review.repo_spelling(pathlib.Path("/example.test/repo"))
        for newer in ("./src.py", "/example.test/repo/src.py"):
            with self.subTest(newer=newer):
                placed = sd_review.rulings_placed([ruling(newer, False), ruling("src.py", True)],
                                                  lambda _head, _path, line: (line,), spelled)
                self.assertEqual([entry.listed for entry in placed], [False, False])

    def test_a_newline_in_an_instance_path_still_places_its_line(self) -> None:
        """Review of sd:10191 (bin/sd-review:942): an instance `a\nb.py:10` did not match the `path:line` pattern, as `.`
        stops at a newline, so it was placed as a path with no line and kept after its line changed."""
        older = sd_review.Rebuttal("head", "src.py", "s", 10, True, "src.py", "x", "y", ("src.py:10", "a\nb.py:10"))

        def kept(_head: str, path: Any, line: int | None) -> tuple[int | None] | None:
            return None if (path, line) == ("a\nb.py", 10) else (line,)
        self.assertEqual([entry.listed for entry in sd_review.rulings_placed([older], kept)], [False])

    def test_an_instance_line_too_long_to_read_lapses_its_ruling(self) -> None:
        """Lead review of sd:10191 (bin/sd-review:943): a saved instance whose line number has 5000 digits
        raised ValueError from int() and aborted every later review of the branch. It lapses its ruling."""
        older = sd_review.Rebuttal("head", "src.py", "s", 10, True, "src.py", "x", "y", ("b.py:" + "9" * 5000,))
        placed = sd_review.rulings_placed([older], lambda _head, _path, line: (line,))
        self.assertEqual([entry.listed for entry in placed], [False])

    def test_a_non_ascii_digit_is_part_of_a_file_name_not_a_line(self) -> None:
        """Review of sd:10191 (bin/sd-review:949, :941, :964): `\\d` matches any Unicode digit, so an
        instance naming the file `b.py:\u0665` was read as b.py line 5, and listed there, where it held back
        a blocker in another file. Only ASCII digits make a line number."""
        name = "b.py:\u0665"
        calls: list[tuple[Any, int | None]] = []

        def kept(_head: str, path: Any, line: int | None) -> tuple[int | None] | None:
            calls.append((path, line))
            return None if path == "b.py" else (line,)
        older = sd_review.Rebuttal("head", "src.py", "s", 10, True, "src.py", "x", "y", ("src.py:10", name))
        placed = sd_review.rulings_placed([older], kept)
        self.assertEqual((placed[0].listed, placed[0].instances), (True, ("src.py:10", name)))
        self.assertIn((name, None), calls)
        newer = sd_review.Rebuttal("head", "b.py", "s", 7, False, "b.py", "x", "y", ("b.py:7",))
        placed = sd_review.rulings_placed([newer, older], kept)
        self.assertEqual([entry.listed for entry in placed], [False, True])

    def test_an_untracked_ruled_file_at_worktree_scope_lapses_the_ruling(self) -> None:
        """Failure table of sd:10191: `git diff HEAD` cannot see an untracked file, so a ruled path that is
        untracked in the worktree counts as changed and the ruling lapses; once gone, it holds again."""
        self.adjudication(("lane claim", "rebutted", "x"))

        def seen() -> list[bool]:
            return [entry.listed for entry in sd_review.rebutted_findings(
                self.root, sd_review.resolve_subject(self.root, "worktree"), None, self.environment())]
        self.assertEqual(seen(), [True])
        (self.root / "bin").mkdir()
        (self.root / "bin/tool.py").write_text("line_3 = 3\n", encoding="utf-8")
        self.assertEqual(seen(), [False])

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
