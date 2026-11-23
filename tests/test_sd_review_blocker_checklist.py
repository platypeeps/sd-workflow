"""The blocker classes past reviews of a repository raised, in its review prompt (pack sd:6028).

The checklist is read from the ship receipts `sd-ship prepare` already
writes (`state` rows keyed `ship:`), never from a store of its own. These
tests write those rows into a fixture database under a scratch `HOME`; the
operator's database is never opened.

It is anchored at the reviewed base's commit time, so `--explain`, the
dispatch and a later replay of the same subject ask the reviewers the same
thing: a receipt written after the base changes no prompt.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import subprocess
import unittest.mock
from datetime import datetime, timedelta, timezone
from typing import Any

from tests.clean_env import clean_environment
from tests.test_sd_review import FakeRunner, ReviewFixture, namespace, sd_review

#: The reviewed base's commit time; every receipt below is placed around it.
ANCHOR = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
REPOSITORY = "fixture/repo"


def finding(summary: str, *, path: str = "bin/tool.py", family: str = "correctness",
            disposition: str = "blocking") -> dict[str, Any]:
    return {"path": path, "line": 3, "severity": "high", "instances": ["src.py:1"], "summary": summary, "family": family,
            "disposition": disposition}


class BlockerChecklistTests(ReviewFixture):
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

    def receipt(self, key: str, *findings: dict[str, Any], days: float = 1.0,
                repository: str = REPOSITORY) -> None:
        """One ship receipt row `days` before the anchor (negative: after it), one pass holding `findings`."""
        stamp = (ANCHOR - timedelta(days=days)).isoformat(timespec="seconds")
        body = {"repository": repository, "branch": key,
                "passes": [{"head": "0" * 40, "report": {"status": "blocking", "findings": list(findings)}}]}
        with contextlib.closing(self.library.connect(self.database())) as connection:
            self.library.writes.record_state(connection, "checkpoint", key=f"ship:{key}", body=body,
                                             timestamp=stamp)

    def prompts(self) -> tuple[list[str], dict[str, Any], str]:
        """Every planned reviewer's prompt for a dry-run worktree review, the result, and stderr."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = sd_review.review(self.root, namespace(dry_run=True), FakeRunner(), self.environment(),
                                      self.chatgpt_home())
        prompts = [row["stdin"] for row in result["planned_invocations"] if row["would_run"]]
        self.assertTrue(prompts)
        return prompts, result, stderr.getvalue()

    def test_past_blockers_reach_every_planned_prompt(self) -> None:
        self.receipt("one", finding("older reap race"), days=3)
        self.receipt("two", finding("reap races the state write"), finding("advisory only", disposition="advisory"))
        self.receipt("three", finding("a token leaks to the log", path="lib/log.py", family="security"))
        self.receipt("other", finding("another repository's blocker"), repository="fixture/elsewhere")
        prompts, _result, stderr = self.prompts()
        for prompt in prompts:
            self.assertIn(sd_review.BLOCKER_HEADER, prompt)
            self.assertIn("- correctness in bin/tool.py (2 blockers): reap races the state write", prompt)
            self.assertIn("- security in lib/log.py (1 blocker): a token leaks to the log", prompt)
            self.assertNotIn("advisory only", prompt)
            self.assertNotIn("another repository", prompt)
            self.assertNotIn("older reap race", prompt)
        self.assertEqual(stderr, "")

    def test_no_past_blockers_add_no_section(self) -> None:
        self.database()
        self.receipt("clean")
        self.receipt("other", finding("another repository's blocker"), repository="fixture/elsewhere")
        prompts, _result, stderr = self.prompts()
        for prompt in prompts:
            self.assertNotIn(sd_review.BLOCKER_HEADER, prompt)
        self.assertEqual(stderr, "")

    def test_no_database_is_no_history_and_says_nothing(self) -> None:
        # No database at all under this HOME: nothing to read, so nothing to say.
        prompts, result, stderr = self.prompts()
        self.assertEqual(result["status"], "dry_run")
        for prompt in prompts:
            self.assertNotIn(sd_review.BLOCKER_HEADER, prompt)
        self.assertEqual(stderr, "")

    def test_an_unreadable_database_still_renders_the_prompt(self) -> None:
        # A database that exists but does not answer: the read fails, the review goes on.
        self.receipt("one", finding("reap races the state write"))
        with unittest.mock.patch.object(sd_review, "BLOCKER_QUERY", "SELECT * FROM no_such_table"):
            prompts, result, stderr = self.prompts()
        self.assertEqual(result["status"], "dry_run")
        for prompt in prompts:
            self.assertNotIn(sd_review.BLOCKER_HEADER, prompt)
        self.assertEqual(len(stderr.splitlines()), 1, stderr)
        self.assertIn("without the blocker checklist", stderr)

    def test_a_failed_query_is_unknown_not_empty(self) -> None:
        self.database()
        stderr = io.StringIO()
        with unittest.mock.patch.object(self.library, "connect", side_effect=sd_review.sqlite3.OperationalError("locked")), \
                contextlib.redirect_stderr(stderr):
            text = sd_review.blocker_checklist(
                self.root, sd_review.resolve_subject(self.root, "worktree"), None, self.environment())
        self.assertEqual(text, "")
        self.assertEqual(stderr.getvalue(), "sd-review: reviewing without the blocker checklist: OperationalError: locked\n")

    def test_the_cap_holds(self) -> None:
        for index in range(sd_review.BLOCKER_LINES + 4):
            self.receipt(f"k{index}", *[finding(f"class {index}", path=f"bin/f{index}.py")] * (index + 1))
        self.receipt("long", *[finding("word " * 200 + "\n\ntail", path="bin/long.py")] * 20)
        prompts, _result, _stderr = self.prompts()
        section = prompts[0].split(sd_review.BLOCKER_HEADER, 1)[1].split("\n\n", 1)[0]
        lines = section.splitlines()[1:]
        self.assertEqual(len(lines), sd_review.BLOCKER_LINES, section)
        # Most frequent first, and a long summary is cut to one bounded line.
        self.assertTrue(lines[0].startswith("- correctness in bin/long.py (20 blockers): word word"), lines[0])
        self.assertTrue(lines[1].startswith(f"- correctness in bin/f{sd_review.BLOCKER_LINES + 3}.py"), lines[1])
        self.assertNotIn("tail", section)
        for line in lines:
            self.assertLessEqual(len(line), 260, line)

    def test_a_receipt_after_the_base_changes_no_request(self) -> None:
        """`--explain`, dispatch and a replay must ask the same (sd:1397): the base anchors the read."""
        self.receipt("before", finding("reap races the state write"))
        _prompts, first, _stderr = self.prompts()
        self.receipt("after", finding("a blocker written after the base"), days=-1)
        self.receipt("stale", finding("a blocker from long before"), days=sd_review.BLOCKER_DAYS + 1)
        prompts, again, _stderr = self.prompts()
        self.assertEqual(again["request_sha256"], first["request_sha256"])
        self.assertNotIn("written after the base", prompts[0])
        self.assertNotIn("long before", prompts[0])

    def adjudication(self, key: str, *rows: tuple[dict[str, Any], str], days: float = 0.5,
                     prefix: str = "ship-adjudication:") -> None:
        """The row `sd-ship adjudicate` records for receipt `key`: each finding with its disposition."""
        stamp = (ANCHOR - timedelta(days=days)).isoformat(timespec="seconds")
        findings = [{"index": index, "finding_digest": "0" * 64, "raw_finding": raw, "response_disposition": answer,
                     "reason": "checked"} for index, (raw, answer) in enumerate(rows, 1)]
        body = {"decision": "accepted", "proposal": {"schema_version": 1, "bindings": {}, "findings": findings},
                "protocol": 1}
        with contextlib.closing(self.library.connect(self.database())) as connection:
            self.library.writes.record_state(connection, "checkpoint", key=f"{prefix}{key}", body=body, timestamp=stamp)

    def test_a_rebutted_blocker_is_left_out(self) -> None:
        """pack sd:6105: a blocker the lead rebutted was no defect, so it is no class to check.
        A parked one stays: the risk is real. A rebuttal after the base changes nothing (sd:1397)."""
        rebutted, parked = finding("a rebutted race"), finding("a parked leak", path="lib/log.py")
        self.receipt("one", rebutted, parked)
        self.adjudication("one", (rebutted, "rebutted"), (parked, "parked"))
        no_item = finding("a rebutted no-item claim", path="bin/other.py")
        self.receipt("no-item", no_item)
        with contextlib.closing(self.library.connect(self.database())) as connection:
            connection.execute("UPDATE state SET key = 'ship-review-no-item:x' WHERE key = 'ship:no-item'")
            connection.commit()
        self.adjudication("x", (no_item, "rebutted"), prefix="ship-adjudication-no-item:")
        late = finding("rebutted only after the base", path="bin/late.py")
        self.receipt("late", late)
        self.adjudication("late", (late, "rebutted"), days=-1)
        prompts, _result, stderr = self.prompts()
        for prompt in prompts:
            self.assertIn("a parked leak", prompt)
            self.assertIn("rebutted only after the base", prompt)
            self.assertNotIn("a rebutted race", prompt)
            self.assertNotIn("a rebutted no-item claim", prompt)
        self.assertEqual(stderr, "")

    def test_planning_reviews_read_no_history(self) -> None:
        with unittest.mock.patch.object(self.library, "connect", side_effect=AssertionError("database read")):
            subject = sd_review.Subject("planning", "HEAD", "worktree", ("docs/work/x/design.md",), 1, "plan")
            self.assertEqual(sd_review.blocker_checklist(self.root, subject, None, self.environment()), "")


if __name__ == "__main__":
    unittest.main()
