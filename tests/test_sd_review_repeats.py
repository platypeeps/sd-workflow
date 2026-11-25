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
                     line: Any = 3, instances: Any = None) -> None:
        """One `sd-ship adjudicate` record: each (summary, disposition, reason) a blocking finding's answer,
        at the reviewed head unless `head` names another."""
        findings = [{"index": index, "finding_digest": "0" * 64, "response_disposition": answer, "reason": reason,
                     "raw_finding": {"path": "bin/tool.py", "line": line, "severity": "high", "instances": instances or [f"bin/tool.py:{line}"], "summary": summary,
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
CITED = ("python3 src.py: Traceback", "`f(1)` returns 2, expected 1")
REPRO = "python3 t.py: expected 1, got 2"


def finding(evidence: Any = None, **extra: Any) -> dict[str, Any]:
    return row(**extra) if evidence is None else row(evidence=evidence, **extra)


class PriorLineTests(RepeatFixture):
    """build/sd-1423 review: each prior blocker's places map through the diff from the head it was found at to
    this head, so the reader and the lead see where it is now. Whether it still blocks is its verdict's
    (sd:10087); the moved-text matcher and its fallbacks are gone with the prose matching."""

    def mapped(self, old: str, new: str, *prior: dict[str, Any]) -> list[tuple[Any, Any]]:
        return [(row["path"], row["line"]) for row in sd_review.prior_at(self.root, old, new, list(prior))]

    def blocker(self, **extra: Any) -> dict[str, Any]:
        return dict(finding(REPRO, **({"line": 10} | extra)), disposition="blocking")

    def test_a_blocker_moved_by_an_insertion_above_it_maps_down(self) -> None:
        source = "".join(f"line{number}\n" for number in range(1, 41))
        (self.root / "src.py").write_text(source, encoding="utf-8")
        old = self.commit("add", "src.py") and self.commit("old")
        (self.root / "src.py").write_text("".join(f"new{n}\n" for n in range(21)) + source, encoding="utf-8")
        new = self.commit("add", "src.py") and self.commit("new")
        self.assertEqual(self.mapped(old, new, self.blocker(), self.blocker(path="gone.py", line=7)),
                         [("src.py", 31), ("gone.py", 7)])

    def test_a_noprefix_diff_config_maps_the_same(self) -> None:
        """sd:10087: the old matcher read paths from diff headers, so `diff.noprefix` lost every moved place."""
        source = "".join(f"line{number}\n" for number in range(1, 41))
        (self.root / "src.py").write_text(source, encoding="utf-8")
        old = self.commit("add", "src.py") and self.commit("old")
        (self.root / "src.py").write_text("x\ny\n" + source, encoding="utf-8")
        new = self.commit("add", "src.py") and self.commit("new")
        for key in ("diff.noprefix", "diff.mnemonicPrefix"):
            self.commit("config", key, "true")
        self.assertEqual(self.mapped(old, new, self.blocker(instances=["src.py:20"]))[0], ("src.py", 12))
        [row] = sd_review.prior_at(self.root, old, new, [self.blocker(instances=["src.py:20"])])
        self.assertEqual(row["instances"], ["src.py:22"])

    def test_each_blocker_maps_from_the_head_it_was_found_at(self) -> None:
        """Lead ruling 2026-11-22: a blocker maps from its own `prior_review.head`, not the aggregate's latest."""
        source = "".join(f"line{number}\n" for number in range(1, 41))
        (self.root / "src.py").write_text(source, encoding="utf-8")
        first = self.commit("add", "src.py") and self.commit("first")
        (self.root / "src.py").write_text("".join(f"new{n}\n" for n in range(30)) + source, encoding="utf-8")
        latest = self.commit("add", "src.py") and self.commit("latest")
        (self.root / "other.py").write_text("x = 1\n", encoding="utf-8")
        new = self.commit("add", "other.py") and self.commit("new")
        self.assertEqual(self.mapped(latest, new, self.blocker(prior_review={"pass": 1, "head": first}),
                                     self.blocker(line=40, prior_review={"pass": 2, "head": latest}),
                                     self.blocker(line=5, prior_review={"head": "not-a-head"})),
                         [("src.py", 40), ("src.py", 40), ("src.py", 5)])

    def test_each_instance_maps_on_its_own_in_one_row(self) -> None:
        (self.root / "src.py").write_text("".join(f"line{n}\n" for n in range(1, 21)), encoding="utf-8")
        (self.root / "b.py").write_text("".join(f"b{n}\n" for n in range(1, 61)), encoding="utf-8")
        old = self.commit("add", "src.py", "b.py") and self.commit("old")
        (self.root / "b.py").write_text("top\ntop\n" + "".join(f"b{n}\n" for n in range(1, 61)), encoding="utf-8")
        new = self.commit("add", "b.py") and self.commit("new")
        odd = ["src.py:10", "b.py:40", " c.py ", "", None, 7, "d.py:" + "9" * 4301]
        [row] = sd_review.prior_at(self.root, old, new, [self.blocker(instances=odd)])
        self.assertEqual((row["path"], row["line"]), ("src.py", 10))
        self.assertEqual(row["instances"], ["src.py:10", "b.py:42", "c.py", "", None, 7, "d.py"])

    def test_a_blocker_follows_its_file_through_a_rename(self) -> None:
        source = "".join(f"line{number}\n" for number in range(1, 41))
        (self.root / "src.py").write_text(source, encoding="utf-8")
        old = self.commit("add", "src.py") and self.commit("old")
        self.commit("mv", "src.py", "renamed.py")
        (self.root / "renamed.py").write_text("top\n" + source, encoding="utf-8")
        new = self.commit("add", "renamed.py") and self.commit("renamed")
        self.assertEqual(self.mapped(old, new, self.blocker()), [("renamed.py", 11)])

    def test_an_unplaced_blocker_follows_its_file_through_a_rename(self) -> None:
        (self.root / "old.py").write_text("x = 1\n" * 20, encoding="utf-8")
        old = self.commit("add", "old.py") and self.commit("old")
        self.commit("mv", "old.py", "new.py")
        new = self.commit("renamed")
        self.assertEqual(self.mapped(old, new, self.blocker(path="old.py", line=None)), [("new.py", None)])

    def test_a_patch_that_is_not_utf8_leaves_the_line_as_stored(self) -> None:
        (self.root / "src.py").write_bytes(b"caf\xe9 = 1\n" * 20)
        old = self.commit("add", "src.py") and self.commit("old")
        (self.root / "src.py").write_bytes(b"x\n" + b"caf\xe9 = 2\n" * 20)
        new = self.commit("add", "src.py") and self.commit("new")
        self.assertEqual(self.mapped(old, new, self.blocker()), [("src.py", 10)])

    def test_a_path_with_a_nul_leaves_the_blocker_as_stored(self) -> None:
        old = self.commit("old")
        new = self.commit("new")
        self.assertEqual(self.mapped(old, new, self.blocker(path="a\0b.py", line=3),
                                     self.blocker(path="c\0d.py", line=None)),
                         [("a\0b.py", 3), ("c\0d.py", None)])

    def test_a_path_is_read_literally_not_as_a_pattern(self) -> None:
        source = "".join(f"line{number}\n" for number in range(1, 41))
        for name in ("page1.py", "page[1].py"):
            (self.root / name).write_text(source, encoding="utf-8")
        old = self.commit("add", ".") and self.commit("old")
        (self.root / "page[1].py").write_text("".join(f"new{n}\n" for n in range(25)) + source, encoding="utf-8")
        (self.root / "page1.py").write_text(source.replace("line3\n", ""), encoding="utf-8")
        new = self.commit("add", ".") and self.commit("new")
        self.assertEqual(self.mapped(old, new, self.blocker(path="page[1].py")), [("page[1].py", 35)])

    def test_the_line_map_reads_each_hunk(self) -> None:
        diff = "@@ -0,0 +1,21 @@\n+x\n@@ -5,2 +26,3 @@\n@@ -20 +42,0 @@\n"
        self.assertEqual([sd_review.moved_line(diff, line) for line in (1, 4, 5, 6, 7, 19, 20, 30)],
                         [22, 25, 26, 27, 29, 41, 42, 51])

    def test_a_blocker_inside_a_replaced_hunk_keeps_its_place(self) -> None:
        (self.root / "src.py").write_text("".join(f"old{n}\n" for n in range(1, 201)), encoding="utf-8")
        old = self.commit("add", "src.py") and self.commit("old")
        lines = [f"old{n}\n" if not 100 <= n < 160 else f"new{n}\n" for n in range(1, 201)]
        (self.root / "src.py").write_text("".join(lines), encoding="utf-8")
        new = self.commit("add", "src.py") and self.commit("new")
        self.assertEqual(self.mapped(old, new, self.blocker(line=145)), [("src.py", 145)])

    def test_only_open_blockers_get_an_id_and_a_cleared_one_none(self) -> None:
        """pack sd:10008: a blocker a later completed pass cleared is no prior blocker; a pass that did not
        complete clears nothing. Ids count the open blockers in report order, and an old id is dropped."""
        first, second = ({"pass": index, "head": head * 40, "completed": True} for index, head in ((1, "a"), (2, "b")))
        for label, history, found, ids in (
                ("cleared by pass 2", [first, second], first, [None, None]),
                ("found at pass 2", [first, second], second, ["B1", None]),
                ("pass 2 incomplete", [first, dict(second, completed=False)], first, ["B1", None]),
                ("no completion recorded", [{"pass": 1}, {"pass": 2}], {"pass": 1}, ["B1", None]),
                ("a single report", None, None, ["B1", None])):
            with self.subTest(label):
                rows = [dict(self.blocker(id="B9"), **({"prior_review": found} if found else {})),
                        dict(finding(), disposition="advisory", id="B7")]
                previous = {"findings": rows, **({"history": history} if history else {})}
                self.assertEqual([row.get("id") for row in sd_review.prior_blockers(previous)], ids)

    def test_an_incomplete_pass_adds_evidence_to_its_blocker_not_a_new_id(self) -> None:
        """build/sd-1675 lane review (bin/sd-review:1935): each incomplete retry's carried copy of a blocker
        took a new id, so every retry added one. A carried row merges into the open blocker of an earlier
        pass its id names, its evidence added; a new blocker still gets the next id."""
        passes = [{"pass": 1, "head": "a" * 40, "completed": True},
                  {"pass": 2, "head": "b" * 40, "completed": False},
                  {"pass": 3, "head": "c" * 40, "completed": False}]
        original = dict(self.blocker(), prior_review=passes[0])
        again = dict(self.blocker(line=12), evidence="one: python3 t.py []: IndexError",
                     carried={"id": "B1", "verdict": "open"}, prior_review=passes[1])
        fresh = dict(self.blocker(summary="Another crash", line=30), prior_review=passes[1])
        third = dict(self.blocker(), evidence="two: still fails", carried={"id": "B1", "verdict": "open"},
                     prior_review=passes[2])
        rows = sd_review.prior_blockers({"findings": [original, again, fresh, third], "history": passes})
        named = [row for row in rows if "id" in row]
        self.assertEqual([(row["id"], row["line"]) for row in named], [("B1", 10), ("B2", 30)])
        self.assertEqual(named[0]["evidence"].splitlines(),
                         [REPRO, "one: python3 t.py []: IndexError", "two: still fails"])
        # once a later pass completes, its own carried copy is the blocker and earlier passes hold none
        done = [passes[0], dict(passes[1], completed=True)]
        rows = sd_review.prior_blockers({"findings": [original, again], "history": done})
        self.assertEqual([(row.get("id"), row["line"]) for row in rows], [(None, 10), ("B1", 12)])


def verdict(answer: str, evidence: str = "src.py:10 now returns early on []", ident: str = "B1") -> dict[str, str]:
    return {"id": ident, "verdict": answer, "evidence": evidence}


class PriorVerdictTests(unittest.TestCase):
    """sd:10087: a later pass cleared a prior blocker by paraphrase, a nearby caller, a moved line or a noprefix
    diff, since only prose matching tied a finding to it. Each prior blocker now has an id, and every reader
    that answers must return a verdict for it: `fixed` with evidence clears it; anything else carries it."""

    def settle(self, *answers: Any, prior: Any = None, reviewed: Any = None) -> dict[str, Any]:
        prior = prior if prior is not None else [
            dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking", id="B1")]
        result = {"severity_floor": "high", "requested_reviews": len(answers) or 1}
        names = [answer.backend for answer in answers] if reviewed is None else reviewed
        return sd_review.finish_review(result, answers, names, repro=True, prior=prior)

    def answer(self, *findings: Any, verdicts: Any = (), name: str = "one") -> Any:
        status = sd_review.FINDINGS if findings else sd_review.CLEAN
        return sd_review.Outcome(name, status, tuple(findings), "", (), (), verdicts=tuple(verdicts))

    def carried(self, result: dict[str, Any]) -> list[tuple[Any, ...]]:
        return [(row["disposition"], row.get("evidence"), row["carried"]) for row in result["findings"]
                if "carried" in row]

    def test_a_prior_blocker_no_reader_answers_still_blocks(self) -> None:
        result = self.settle(self.answer())
        self.assertEqual(result["status"], "blocking")
        self.assertEqual(self.carried(result), [("blocking", REPRO, {"id": "B1", "verdict": "none"})])

    def test_a_paraphrase_far_from_the_blocker_does_not_clear_it(self) -> None:
        result = self.settle(self.answer(finding(line=40, summary="An empty list raises IndexError")))
        self.assertEqual(result["status"], "blocking")
        self.assertEqual([row["disposition"] for row in result["findings"]], ["advisory", "blocking"])

    def test_a_fixed_verdict_with_evidence_clears_it(self) -> None:
        result = self.settle(self.answer(verdicts=[verdict("fixed")]))
        self.assertEqual((result["status"], result["findings"]), ("clean", []))

    def test_a_fixed_verdict_without_evidence_does_not_clear_it(self) -> None:
        for evidence in ("", "  \n"):
            with self.subTest(evidence=evidence):
                result = self.settle(self.answer(verdicts=[verdict("fixed", evidence)]))
                self.assertEqual(self.carried(result), [("blocking", REPRO, {"id": "B1", "verdict": "none"})])

    def test_an_open_verdict_blocks_with_its_own_evidence(self) -> None:
        result = self.settle(self.answer(verdicts=[verdict("open", "python3 t.py []: IndexError")]))
        self.assertEqual(self.carried(result),
                         [("blocking", "one: python3 t.py []: IndexError", {"id": "B1", "verdict": "open"})])

    def test_every_reader_that_answered_must_say_fixed(self) -> None:
        result = self.settle(self.answer(verdicts=[verdict("fixed")]), self.answer(name="two"))
        self.assertEqual(result["status"], "blocking")
        conflicting = self.answer(verdicts=[verdict("fixed"), verdict("open", "still")])
        self.assertEqual(self.settle(conflicting)["status"], "blocking")

    def test_a_verdict_for_another_id_answers_nothing(self) -> None:
        result = self.settle(self.answer(verdicts=[verdict("fixed", ident="B2")]))
        self.assertEqual(result["status"], "blocking")

    def test_a_rebutted_prior_blocker_is_not_carried(self) -> None:
        rebutted = listed(adjudicated(("src.py", "Empty input crashes", "rebutted"), line=10))
        result = sd_review.finish_review({"severity_floor": "high", "requested_reviews": 1}, [self.answer()],
                                         ["one"], rebutted, True,
                                         [dict(finding(REPRO, line=10, summary="Empty input crashes"),
                                               disposition="blocking", id="B1")])
        self.assertEqual(result["status"], "clean")

    def test_a_rebuttal_must_cover_every_place_the_blocker_names(self) -> None:
        """build/sd-1623 review: a rebuttal answers a blocker only when the newest record at every place it
        names is a listed rebuttal; one that misses an instance, or that a newer record overrides, does not."""
        both = ["src.py:10", "b.py:40"]
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes", instances=both),
                      disposition="blocking", id="B1")]
        covering = adjudicated(("src.py", "Empty input crashes", "rebutted"), line=10, instances=both)
        missing = adjudicated(("src.py", "Empty input crashes", "rebutted"), line=10)
        newer = adjudicated(("b.py", "Empty input crashes", "accepted"), line=40)
        for label, rebutted, count in (("covers both", listed(covering), 0), ("misses b.py", listed(missing), 1),
                                       ("newer record", listed(newer, covering), 1)):
            with self.subTest(label):
                self.assertEqual(len(sd_review.carried(prior, [self.answer()], rebutted)), count)

    def test_a_failed_readers_open_verdict_outlives_a_fallbacks_fixed(self) -> None:
        """build/sd-1675 review: a reviewer that failed after a valid `open` verdict, and a fallback that
        said `fixed`, cleared the blocker, since only the readers that completed were read."""
        raw = json.dumps({"findings": [], "verdicts": [verdict("open", "python3 t.py []: IndexError")]})
        failed = sd_review._answer("one", sd_review.Completed(1, raw, "crash"), sd_review.parse_findings(raw))
        self.assertEqual(failed.verdicts, (verdict("open", "python3 t.py []: IndexError"),))
        result = self.settle(failed, self.answer(verdicts=[verdict("fixed")], name="two"), reviewed=["two"])
        self.assertEqual(result["status"], "blocking")
        self.assertEqual(self.carried(result),
                         [("blocking", "one: python3 t.py []: IndexError", {"id": "B1", "verdict": "open"})])

    def test_only_a_completed_answers_own_fixed_verdict_clears(self) -> None:
        """build/sd-1675 class pass: verdicts were pooled by reader name over every outcome, so a `fixed` from
        an attempt that did not complete answered for the same reader's retry that gave none. Each producer of
        such an outcome is here: a failed process, a voided blind answer, a partial read."""
        fixed = self.answer(verdicts=[verdict("fixed")])
        raw = json.dumps({"findings": [], "verdicts": [verdict("fixed")]})
        blind = self.answer(finding(path="elsewhere.py", line=1), verdicts=[verdict("fixed")])
        attempts = {"failed": sd_review._answer("one", sd_review.Completed(1, raw, "crash"), sd_review.parse_findings(raw)),
                    "voided": sd_review.unlocated(blind, frozenset({"src.py"}), "high"),
                    "partial": sd_review.partial(fixed, ["big.bin"])}
        for label, attempt in attempts.items():
            with self.subTest(label):
                self.assertNotIn(attempt.status, (sd_review.CLEAN, sd_review.FINDINGS))
                self.assertEqual(attempt.verdicts, (verdict("fixed"),))
                result = self.settle(attempt, self.answer(), reviewed=["one"])  # the retry completes, with no verdict
                self.assertEqual(result["status"], "blocking")
                self.assertEqual([row[2] for row in self.carried(result)], [{"id": "B1", "verdict": "none"}])
                # it does not answer for another reader either, and a completed `fixed` needs nothing from it
                self.assertEqual(self.settle(attempt, self.answer(name="two"), reviewed=["two"])["status"], "blocking")
                cleared = self.settle(attempt, self.answer(verdicts=[verdict("fixed")], name="two"), reviewed=["two"])
                self.assertEqual([row for row in cleared["findings"] if "carried" in row], [])

    def test_two_completed_answers_under_one_name_each_need_a_fixed_verdict(self) -> None:
        result = self.settle(self.answer(verdicts=[verdict("fixed")]), self.answer())
        self.assertEqual(result["status"], "blocking")
        self.assertEqual(self.settle(self.answer(verdicts=[verdict("fixed")]),
                                     self.answer(verdicts=[verdict("fixed")]))["status"], "clean")

    def test_a_cited_open_verdict_outlives_a_rebuttal_as_a_cited_repeat_does(self) -> None:
        """build/sd-1675 class pass: a rebuttal cleared a prior blocker over a reader's own `open` verdict with
        evidence, while the same reader's cited repeat of a rebutted finding blocks. An uncited one is answered."""
        rebutted = listed(adjudicated(("src.py", "Empty input crashes", "rebutted"), line=10))
        prior = [dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking", id="B1")]
        for label, evidence, count in (("cited", "python3 t.py []: IndexError", 1), ("uncited", " ", 0)):
            with self.subTest(label):
                rows = sd_review.carried(prior, [self.answer(verdicts=[verdict("open", evidence)])], rebutted)
                self.assertEqual(len(rows), count)

    def test_no_completed_reader_carries_nothing(self) -> None:
        failed = sd_review.Outcome("one", sd_review.UNAVAILABLE, (), "", (), ())
        result = self.settle(failed, reviewed=[])
        self.assertEqual((result["status"], result["findings"]), ("unavailable", []))

    def test_a_cited_open_verdict_carries_when_no_reader_completes(self) -> None:
        """build/sd-1675 lane review (bin/sd-review:1849): with every reader failed, a failed reader's cited
        `open` verdict was dropped, so the retry lost it. It carries; an uncited one does not."""
        for label, evidence, carried in (
                ("cited", "python3 t.py []: IndexError",
                 [("blocking", "one: python3 t.py []: IndexError", {"id": "B1", "verdict": "open"})]),
                ("uncited", " ", [])):
            with self.subTest(label):
                raw = json.dumps({"findings": [], "verdicts": [verdict("open", evidence)]})
                failed = sd_review._answer("one", sd_review.Completed(1, raw, "crash"), sd_review.parse_findings(raw))
                result = self.settle(failed, reviewed=[])
                self.assertEqual(self.carried(result), carried)
                self.assertEqual(result["completed_reviews"], 0)

    def test_the_parser_reads_verdicts_and_refuses_a_bad_one(self) -> None:
        parsed = sd_review.parse_findings(json.dumps({"findings": [], "verdicts": [verdict("fixed")]}))
        self.assertEqual((parsed.error, parsed.verdicts), ("", (verdict("fixed"),)))
        self.assertEqual(sd_review.parse_findings('{"findings": []}').verdicts, ())
        for bad in ({"id": "B1", "verdict": "maybe", "evidence": ""}, {"id": "", "verdict": "open", "evidence": ""},
                    {"id": "B1", "verdict": "open"}, {"id": "B1", "verdict": "open", "evidence": 3}, "B1",
                    dict(verdict("open"), extra=1)):
            with self.subTest(bad=bad):
                parsed = sd_review.parse_findings(json.dumps({"findings": [], "verdicts": [bad]}))
                self.assertIn("verdicts[0]", parsed.error)
        self.assertIn("verdicts", sd_review.parse_findings('{"findings": [], "verdicts": {}}').error)
        self.assertIn("unexpected", sd_review.parse_findings('{"findings": [], "other": 1}').error)

    def test_the_schema_requires_verdicts(self) -> None:
        schema = sd_review.CODEX_OUTPUT_SCHEMA
        self.assertEqual(schema["required"], ["findings", "verdicts"])
        self.assertEqual(schema["properties"]["verdicts"]["items"]["required"], ["id", "verdict", "evidence"])
        self.assertIn("verdicts", sd_review.URL_OUTPUT_CONTRACT)
        self.assertLess(len(sd_review.URL_OUTPUT_CONTRACT.encode()), 1600)


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
        self.assertEqual(result["status"], "blocking")
        self.assertEqual([(r["disposition"], r.get("carried")) for r in result["findings"] if "carried" in r],
                         [("blocking", {"id": "B1", "verdict": "none"})])

    def test_a_verify_pass_clears_a_blocker_only_on_a_fixed_verdict(self) -> None:
        """sd:10087, end to end: the verify prompt names each prior blocker's id, and the pass blocks until every
        reader that answered returns `fixed` with evidence for it."""
        (self.root / "src.py").write_text("".join(f"line{n}\n" for n in range(1, 41)), encoding="utf-8")
        base = self.commit("add", "src.py") and self.commit("src")
        self.commit("fix")
        blocker = dict(finding(REPRO, line=10, summary="Empty input crashes"), disposition="blocking")
        report = self.tmp / "prior.json"
        report.write_text(json.dumps({"scope": "branch", "subject": {"head": base}, "status": "blocking",
                                      "findings": [blocker]}))
        paraphrase = {"findings": [finding(line=30, summary="An empty list raises IndexError", instances=["src.py:30"])]}
        fixed = {"findings": [], "verdicts": [{"id": "B1", "verdict": "fixed", "evidence": "src.py:10 checks []"}]}
        for label, answer, status in (("paraphrase", paraphrase, "blocking"), ("fixed", fixed, "clean")):
            with self.subTest(label):
                runner = FakeRunner({"sd-check": sd_review.Completed(0, "{}", "")},
                                    default=sd_review.Completed(0, json.dumps(answer), ""))
                result = sd_review.review(self.root, namespace(scope="branch", base=base, verify_report=str(report)),
                                          runner, self.environment(), self.chatgpt_home())
                self.assertEqual(result["status"], status)
        prompts, _result, _stderr = self.prompts(scope="branch", base=base, verify_report=str(report))
        self.assertIn('"id": "B1"', prompts[0])
        self.assertIn("verdicts", prompts[0])

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
