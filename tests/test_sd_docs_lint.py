"""Green and red fixtures for each rule in bin/sd-docs-lint."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
from types import ModuleType
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def in_directory(path: pathlib.Path):
    """Run the body with `path` as the working directory, then put it back."""

    previous = pathlib.Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)

LINT_PATH = REPO_ROOT / "bin" / "sd-docs-lint"


def load_lint() -> ModuleType:
    """Import the executable, which has no .py suffix to import by name."""
    spec = importlib.util.spec_from_loader(
        "sd_docs_lint",
        importlib.machinery.SourceFileLoader("sd_docs_lint", str(LINT_PATH)),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lint = load_lint()

GOOD_PRD = """---
title: A workable item
created: 2026-08-29
---

# PRD

## Acceptance criteria

- [x] the thing works
"""

#: An archived item keeps the `status:` line it died with; an active one has none.
ARCHIVED_PRD = GOOD_PRD.replace("title: A workable item\n", "title: A workable item\nstatus: done\n")

GOOD_DECISION = """---
title: Use JSON for pack-owned config
status: accepted
date: 2026-08-29
---

## Decision

Every pack-owned config file is JSON.
"""


class LintFixture(unittest.TestCase):
    """A throwaway repository laid out the way the rules expect it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = pathlib.Path(self._tmp.name)
        # A git repository, because rule 7 enumerates tracked markdown rather
        # than walking the tree: an untracked scratch file is not a document
        # this repository publishes, and the rule declines to read one.
        self.git("init", "-q")
        self.work = self.repo / "docs" / "work"
        self.spec = self.repo / "docs" / "spec"
        self.write_item("2026-08-29-a-workable-item", GOOD_PRD)
        self.write_spec("backend", ["quality.md"])
        # No test reads the operator's privacy-pattern file: the default is
        # a directory that does not exist, and the privacy tests write their own.
        # Every run opens the one database through `$HOME`. An empty home holds
        # none, so the read answers "no database" and asks git, which has no
        # remote here to fetch from -- the operator's rows are never consulted
        # and nothing leaves the machine.
        home = self.repo / "home"
        home.mkdir()
        env = mock.patch.dict(
            os.environ,
            {"SYSTEM_TOOLS_CONFIG": str(self.repo / "no-config"), "HOME": str(home)},
        )
        env.start()
        self.addCleanup(env.stop)
        # What a row would say, by item directory name. An item not named here
        # reads whatever the database-free path answers.
        self.row_status: dict[str, str] = {}
        # The row's `branch` column, by the same key; absent is no row answer.
        self.row_branch: dict[str, str] = {}
        real = lint.sd_lib.status_report

        def status_report(item, *, statuses=None):
            name = pathlib.Path(item).name
            said = self.row_status.get(name)
            if said is None:
                return real(item, statuses=statuses)
            return lint.sd_lib.StatusReport(said, False, branch=self.row_branch.get(name))

        patched = mock.patch.object(lint.sd_lib, "status_report", status_report)
        patched.start()
        self.addCleanup(patched.stop)

    def write_item(self, name: str, prd: str, *, month: str | None = None) -> pathlib.Path:
        parent = self.work / "archive" / month if month else self.work
        item = parent / name
        item.mkdir(parents=True, exist_ok=True)
        (item / "prd.md").write_text(prd, encoding="utf-8")
        return item

    def write_design(self, name: str, body: str = "# Design\n") -> pathlib.Path:
        """A design-only item (sd:3000): frontmatter naming its row, then `body`."""
        item = self.work / name
        item.mkdir(parents=True, exist_ok=True)
        (item / "design.md").write_text(
            f"---\ntitle: A design\nitem: sd:361\n---\n\n{body}", encoding="utf-8")
        return item

    def write_spec(self, area: str, pages: list[str], *, index: str | None = None) -> None:
        directory = self.spec / area
        directory.mkdir(parents=True, exist_ok=True)
        for page in pages:
            (directory / page).write_text(f"# {page}\n", encoding="utf-8")
        body = index
        if body is None:
            links = "\n".join(f"- [{page}](./{page})" for page in pages)
            body = f"# {area}\n\n{links}\n"
        (directory / "index.md").write_text(body, encoding="utf-8")

    def write_patterns(self, text: str = "secret-host\\.example\\.test\n") -> pathlib.Path:
        """A privacy-pattern file outside the repository, read through `SYSTEM_TOOLS_CONFIG`."""
        config = self.repo.parent / f"{self.repo.name}-config"
        (config / "system").mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, config, True)
        patterns = config / "system" / "privacy-patterns"
        patterns.write_text(text, encoding="utf-8")
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config), "SYSTEM_TOOLS_CONFIG": str(config / "system")})
        env.start()
        self.addCleanup(env.stop)
        return patterns

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True
        )

    def run_lint(self, pr_body: str | None = None, body_only: bool = False) -> lint.Report:
        # Staged, not committed: `ls-files` reads the index, and every test
        # here writes its fixture immediately before asking for a verdict.
        self.git("add", "-A")
        mode = {"body_only": True} if body_only else {}
        return lint.run(self.repo, "docs/work", "docs/spec", "docs/decisions", pr_body, **mode)

    def assert_clean(self) -> None:
        report = self.run_lint()
        self.assertEqual(report.failures, [])

    def notes(self) -> str:
        """Every note one run printed, joined. What a rule says it did."""
        return "\n".join(self.run_lint().notes)

    def assert_fails(self, needle: str, pr_body: str | None = None) -> list[str]:
        report = self.run_lint(pr_body)
        joined = "\n".join(report.failures)
        self.assertIn(needle, joined)
        return report.failures


class Rule1ShapeTests(LintFixture):
    def test_green(self) -> None:
        self.write_item(
            "2026-07-04-an-archived-item",
            ARCHIVED_PRD.replace("created: 2026-08-29", "created: 2026-07-04"),
            month="2026-07",
        )
        self.assert_clean()

    def test_red_directory_name_is_not_dated(self) -> None:
        self.write_item("no-date-here", GOOD_PRD)
        self.assert_fails("named <YYYY-MM-DD>-<slug>")

    def test_green_design_alone(self) -> None:
        """sd:3000. One design.md is a whole work item; prd.md is legacy."""
        item = self.work / "2026-08-30-a-design"
        item.mkdir(parents=True)
        (item / "design.md").write_text(
            "---\ntitle: A design\ncreated: 2026-08-30\nitem: sd:361\n---\n\n# Design\n", encoding="utf-8")
        self.assert_clean()

    def test_red_design_alone_without_frontmatter(self) -> None:
        """sd:3416. sd-status finds a design-only folder's row by its `item:` key; without one it reads no status."""
        item = self.work / "2026-08-30-a-design"
        item.mkdir(parents=True)
        (item / "design.md").write_text("# Design\n\nitem: sd:361\n", encoding="utf-8")
        self.assertTrue(any("a-design/design.md" in failure for failure in
                            self.assert_fails("design.md opens with a --- frontmatter block naming item: sd:<id>")))

    def test_red_design_alone_without_an_item_key(self) -> None:
        item = self.work / "2026-08-30-a-design"
        item.mkdir(parents=True)
        (item / "design.md").write_text("---\ntitle: A design\ncreated: 2026-08-30\n---\n\n# Design\n",
                                        encoding="utf-8")
        self.assert_fails("design.md opens with a --- frontmatter block naming item: sd:<id>")

    def test_red_no_document(self) -> None:
        (self.work / "2026-08-30-empty").mkdir(parents=True)
        self.assert_fails("a work item holds a design.md")

    def test_red_missing_frontmatter(self) -> None:
        self.write_item("2026-08-30-bare", "# PRD\n\n## Acceptance criteria\n")
        self.assert_fails("opens with a --- frontmatter block")

    def test_red_unknown_status(self) -> None:
        self.write_item(
            "2026-08-30-odd",
            ARCHIVED_PRD.replace("status: done", "status: pondering").replace(
                "created: 2026-08-29", "created: 2026-08-30"),
            month="2026-08",
        )
        self.assert_fails("'pondering' is not one of")

    def test_red_created_disagrees_with_the_directory(self) -> None:
        self.write_item("2026-08-30-drifted", GOOD_PRD)
        self.assert_fails("disagrees with the directory date")

    def test_red_stray_file_in_a_work_item(self) -> None:
        (self.work / "2026-08-29-a-workable-item" / "task.json").write_text("{}", encoding="utf-8")
        self.assert_fails("a work item holds design.md, or the legacy prd.md and implement.md, only")

    def test_red_archive_bucket_is_not_a_month(self) -> None:
        (self.work / "archive" / "july").mkdir(parents=True)
        self.assert_fails("archive buckets are named YYYY-MM")

    def test_green_item_key_names_a_row(self) -> None:
        """sd:994. A folder written for a task or followup row names it as
        `item: sd:<id>`; the key is optional and its form is `sd:` then digits."""
        self.write_item(
            "2026-08-29-a-keyed-item",
            GOOD_PRD.replace("created: 2026-08-29\n", "created: 2026-08-29\nitem: sd:361\n"),
        )
        self.assert_clean()

    def test_red_item_key_is_not_sd_then_digits(self) -> None:
        for value in ("361", "sd:x", "sd:", "SD:361"):
            with self.subTest(value=value):
                self.write_item(
                    "2026-08-29-a-keyed-item",
                    GOOD_PRD.replace(
                        "created: 2026-08-29\n", f"created: 2026-08-29\nitem: {value}\n"),
                )
                failures = self.assert_fails("is not sd: followed by digits")
                self.assertTrue(
                    any(f"item {value!r}" in f and "a-keyed-item/prd.md" in f for f in failures),
                    failures,
                )


class Rule1StatusLineTests(LintFixture):
    """Rule 1's status check: the row is the status, so an active `prd.md`
    carries no `status:` line and an archived one keeps the line it had.

    Each case here is one cell of the table -- active or archived, line or no
    line -- and the one red cell for each.
    """

    def test_green_an_active_prd_without_a_status_line(self) -> None:
        self.assert_clean()

    def test_red_a_status_line_in_an_active_prd(self) -> None:
        self.write_item("2026-08-29-a-workable-item", ARCHIVED_PRD)
        failures = self.assert_fails("the row is the status; prd.md carries no status: line")
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("2026-08-29-a-workable-item/prd.md", failures[0])

    def test_green_a_status_line_only_under_the_archive(self) -> None:
        self.write_item(
            "2026-07-04-an-archived-item",
            ARCHIVED_PRD.replace("created: 2026-08-29", "created: 2026-07-04"),
            month="2026-07",
        )
        self.assert_clean()

    def test_a_marker_file_is_not_read(self) -> None:
        """The old `.status-source` word decides nothing now, whatever it says."""
        for word in ("file", "row", "column"):
            with self.subTest(word=word):
                (self.work / ".status-source").write_text(word + "\n", encoding="utf-8")
                self.assert_clean()


class Rule2ReadyTests(LintFixture):
    def test_green_in_progress_with_a_branch(self) -> None:
        self.write_item(
            "2026-08-30-running",
            GOOD_PRD.replace("created: 2026-08-29", "created: 2026-08-30\nbranch: task/08-30-running"),
        )
        self.row_status["2026-08-30-running"] = "in_progress"
        self.assert_clean()

    def test_green_planning_item_needs_no_acceptance_criteria(self) -> None:
        self.write_item(
            "2026-08-30-idea",
            "---\ntitle: An idea\ncreated: 2026-08-30\n---\n\n# PRD\n",
        )
        self.row_status["2026-08-30-idea"] = "planning"
        self.assert_clean()

    def test_red_missing_acceptance_criteria(self) -> None:
        self.write_item(
            "2026-08-30-vague",
            "---\ntitle: Vague\ncreated: 2026-08-30\n---\n\n# PRD\n",
        )
        self.row_status["2026-08-30-vague"] = "ready"
        self.assert_fails("states acceptance criteria")

    def test_red_open_blocking_line(self) -> None:
        self.write_item(
            "2026-08-30-blocked",
            GOOD_PRD.replace("created: 2026-08-29", "created: 2026-08-30")
            + "\nBLOCKING: the API is not designed yet.\n",
        )
        self.row_status["2026-08-30-blocked"] = "ready"
        self.assert_fails("no open BLOCKING line")

    def test_red_open_blocking_line_as_a_list_item(self) -> None:
        self.write_item(
            "2026-08-30-blocked-bullet",
            GOOD_PRD.replace("created: 2026-08-29", "created: 2026-08-30")
            + "\n- BLOCKING: the API is not designed yet.\n",
        )
        self.row_status["2026-08-30-blocked-bullet"] = "ready"
        self.assert_fails("no open BLOCKING line")

    def test_green_prose_that_quotes_the_marker(self) -> None:
        """The word inside a sentence is a record, not an open blocker.

        An item's own log discusses blocking findings; matching the token
        anywhere on a line meant such an item could never be `in_progress`.
        """
        self.write_item(
            "2026-08-30-discusses",
            GOOD_PRD.replace("created: 2026-08-29", "created: 2026-08-30")
            + "\nRule 2 checks that no open `BLOCKING:` line remains.\n",
        )
        self.row_status["2026-08-30-discusses"] = "ready"
        self.assert_clean()

    def test_red_in_progress_without_a_branch(self) -> None:
        self.write_item(
            "2026-08-30-adrift",
            GOOD_PRD.replace("created: 2026-08-29", "created: 2026-08-30"),
        )
        self.row_status["2026-08-30-adrift"] = "in_progress"
        self.assert_fails("records the branch it lives on")

    def test_red_the_note_says_how_many_items_rule_2_actually_checked(self) -> None:
        """Backbone item sd:5. The item total was never rule 2's coverage.

        Rule 2's three checks run on `ready` and `in_progress` items only, and
        the note it shares with rule 1 counts every item on disk. On this
        repository that reads `checked 497 item(s)` for a rule that ran on
        two. `status_source_note` describes the mechanism that narrows it,
        which is not the same as saying how far: one number cannot tell 497 of
        497 from 2 of 497, and both print `clean`.

        Here: one `ready` fixture item, one `planning` item beside it.
        """
        self.write_item(
            "2026-08-30-idea",
            "---\ntitle: An idea\ncreated: 2026-08-30\n---\n\n# PRD\n",
        )
        self.row_status["2026-08-29-a-workable-item"] = "ready"
        self.row_status["2026-08-30-idea"] = "planning"
        report = self.run_lint()
        self.assertEqual(report.failures, [])
        self.assertIn(
            "rules 1-2 work items: checked 2 item(s), 1 of them workable by rule 2",
            "\n".join(report.notes),
        )


class Rule2DesignOnlyTests(LintFixture):
    """sd:3489. Rule 2 reads a design-only item, which it once skipped whole."""

    def test_red_a_ready_design_without_acceptance_criteria(self) -> None:
        self.write_design("2026-08-30-a-design")
        self.row_status["2026-08-30-a-design"] = "ready"
        failures = self.assert_fails("a ready item states acceptance criteria")
        self.assertTrue(any("a-design/design.md" in failure for failure in failures), failures)

    def test_red_a_ready_design_with_an_open_blocking_line(self) -> None:
        self.write_design("2026-08-30-a-design", "## Acceptance criteria\n\nBLOCKING: no API yet.\n")
        self.row_status["2026-08-30-a-design"] = "ready"
        self.assert_fails("a ready item has no open BLOCKING line")

    def test_red_an_in_progress_design_whose_row_names_no_branch(self) -> None:
        self.write_design("2026-08-30-a-design", "## Acceptance criteria\n")
        self.row_status["2026-08-30-a-design"] = "in_progress"
        self.row_branch["2026-08-30-a-design"] = ""
        self.assert_fails("an in_progress item records the branch it lives on")

    def test_green_an_in_progress_design_takes_its_branch_from_the_row(self) -> None:
        """A design.md carries no `branch:` line; the row's column answers."""
        self.write_design("2026-08-30-a-design", "## Acceptance criteria\n")
        self.row_status["2026-08-30-a-design"] = "in_progress"
        self.row_branch["2026-08-30-a-design"] = "feat/a-design"
        report = self.run_lint()
        self.assertEqual(report.failures, [])
        self.assertIn("1 of them workable by rule 2", "\n".join(report.notes))

    def test_the_branch_line_advisory_reads_a_design(self) -> None:
        item = self.write_design("2026-08-30-a-design")
        design = item / "design.md"
        design.write_text(design.read_text(encoding="utf-8").replace(
            "item: sd:361\n", "item: sd:361\nbranch: feat/old\n"), encoding="utf-8")
        rows = mock.Mock(opened=True)
        rows.item.return_value = {"branch": "feat/new"}
        report = lint.Report()
        lint.check_branch_lines(self.work, report, mock.Mock(rows=rows))
        self.assertIn("1 of 1 active item(s) with a branch: line disagree", "\n".join(report.notes))


class SkipsAndRemediesTests(LintFixture):
    """sd:3491. A rule that did not run says so on the last line; a refusal names its fix."""

    def cli(self, *argv: str) -> tuple[int, str, str]:
        self.git("add", "-A")
        with in_directory(self.repo), contextlib.redirect_stderr(io.StringIO()) as said, \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            code = lint.main(list(argv))
        return code, printed.getvalue(), said.getvalue()

    def test_a_skipped_rule_goes_to_stderr_and_the_last_line_counts_it(self) -> None:
        code, printed, said = self.cli("--no-history")
        self.assertEqual(code, 0, said)
        self.assertIn("PR privacy: no --pr-body supplied, not run", said)
        self.assertNotIn("not run", printed)
        skips = len([line for line in said.splitlines() if line])
        self.assertGreater(skips, 1, said)
        self.assertEqual(printed.splitlines()[-1], f"sd-docs-lint: clean ({skips} skip(s) on stderr)")

    def test_red_a_named_work_directory_that_is_missing_names_the_flag(self) -> None:
        shutil.rmtree(self.work)
        report = self.run_lint()
        self.assertEqual(len(report.failures), 1, report.failures)
        self.assertIn("pass --work-dir the repo-relative root", report.failures[0])

    def test_red_rule_7_without_git_names_the_fix(self) -> None:
        report = lint.Report()
        with mock.patch.object(lint.sd_lib, "git_output", return_value=None):
            lint.check_work_references(self.repo, "docs/work", report)
        self.assertIn("run it from inside a git checkout", "\n".join(report.failures))

    def test_green_a_dotfile_in_an_item_is_not_a_document(self) -> None:
        item = self.work / "2026-08-29-a-workable-item"
        for name in (".DS_Store", ".prd.md.swp"):
            (item / name).write_text("x", encoding="utf-8")
        self.assert_clean()

    def test_red_an_index_link_to_a_same_named_page_elsewhere_is_not_a_link(self) -> None:
        self.write_spec("other", ["quality.md"])
        index = "# backend\n\n- [quality](../other/quality.md)\n"
        self.write_spec("backend", ["quality.md"], index=index)
        self.assert_fails("index does not link quality.md")

    def test_green_a_reference_style_link_is_a_link(self) -> None:
        self.write_spec("backend", ["quality.md"], index="# backend\n\n- [quality][q]\n\n[q]: ./quality.md\n")
        self.assert_clean()


class Rule2StatusSourceTests(LintFixture):
    """The run says where rule 2 read its statuses, because a checkout with
    no database checks fewer items and prints the same `clean`."""

    def source_note(self) -> str:
        report = self.run_lint()
        return next(note for note in report.notes if note.startswith("rule 2 status source:"))

    def test_no_database_says_git_and_names_the_gap(self) -> None:
        # The empty HOME of the fixture is a machine with the library and no
        # database: the CI lint job, and any checkout that never ran
        # `sd-db.sh init`.
        note = self.source_note()
        self.assertIn("git, not the row", note)
        self.assertIn("rule 2 does not check it", note)


class Rule3DecisionTests(LintFixture):
    def write_decision(self, name: str, body: str) -> None:
        directory = self.repo / "docs" / "decisions"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(body, encoding="utf-8")

    def test_green(self) -> None:
        self.write_decision("2026-08-29-json-config.md", GOOD_DECISION)
        self.assert_clean()

    def test_green_absent_directory_is_not_a_failure(self) -> None:
        report = self.run_lint()
        self.assertEqual(report.failures, [])
        self.assertTrue(any("rule 3" in warning for warning in report.warnings))

    def test_red_undated_filename(self) -> None:
        self.write_decision("json-config.md", GOOD_DECISION)
        self.assert_fails("named <YYYY-MM-DD>-<slug>.md")

    def test_red_unknown_status(self) -> None:
        self.write_decision(
            "2026-08-29-json-config.md", GOOD_DECISION.replace("accepted", "maybe")
        )
        self.assert_fails("'maybe' is not one of")

    def test_red_date_disagrees_with_filename(self) -> None:
        self.write_decision(
            "2026-08-30-json-config.md", GOOD_DECISION
        )
        self.assert_fails("disagrees with the filename date")

    def test_red_no_decision_section(self) -> None:
        self.write_decision(
            "2026-08-29-json-config.md", GOOD_DECISION.replace("## Decision", "## Notes")
        )
        self.assert_fails("states its decision under a Decision heading")

    def test_red_the_count_is_records_checked_not_files_enumerated(self) -> None:
        """Backbone item sd:5, smallest of the set.

        An `index.md` is a table of contents, and skipping it is right.
        Counting it as a checked record is not: `len(records)` was the
        enumeration, and reporting an enumeration as coverage is how a rule
        claims to have checked what it passed over. One record, one index,
        and the note has to say one.
        """
        self.write_decision("2026-08-29-json-config.md", GOOD_DECISION)
        self.write_decision("index.md", "# decisions\n\n- one\n")
        report = self.run_lint()
        self.assertEqual(report.failures, [])
        self.assertIn(
            "rule 3 decision shape: checked 1 record(s), skipping 1 index.md",
            "\n".join(report.notes),
        )


class Rule4SpecIndexTests(LintFixture):
    def test_green(self) -> None:
        self.write_spec("frontend", ["adapters.md", "layout.md"])
        self.assert_clean()

    def test_red_missing_index(self) -> None:
        directory = self.spec / "tooling"
        directory.mkdir(parents=True)
        (directory / "lanes.md").write_text("# lanes\n", encoding="utf-8")
        self.assert_fails("every spec directory has an index.md")

    def test_red_index_does_not_link_a_sibling(self) -> None:
        self.write_spec("tooling", ["lanes.md", "gates.md"], index="# tooling\n\n- [lanes](./lanes.md)\n")
        self.assert_fails("index does not link gates.md")


class BodyOnlyTests(LintFixture):
    """`--body-only`: the privacy rule with no work root, and nothing else."""

    def setUp(self) -> None:
        super().setUp()
        shutil.rmtree(self.work)
        self.write_patterns()

    def test_green_every_tree_rule_says_it_did_not_run(self) -> None:
        report = self.run_lint("## Summary\n", body_only=True)
        self.assertEqual(report.failures, [])
        notes = "\n".join(report.notes)
        self.assertIn("rules 1-4, 7: --body-only, not run", "\n".join(report.warnings))
        for tree_rule in ("rules 1-2 work items", "rule 3 decision", "rule 4 spec", "rule 7 work"):
            self.assertNotIn(tree_rule, notes)

    def test_without_the_flag_no_work_root_is_still_the_failure_it_was(self) -> None:
        # The mode is opt-in. A mistyped `--work-dir` on a full run stays a
        # failure by name rather than a body-only run nobody asked for.
        report = self.run_lint("## Summary\n")
        self.assertEqual(len(report.failures), 1, report.failures)
        self.assertTrue(report.failures[0].startswith(f"{self.work.resolve()}: the work directory does not exist"))

    def test_the_flag_without_a_body_is_a_failure_in_the_library_and_an_argument_error_at_the_cli(self) -> None:
        report = self.run_lint(None, body_only=True)
        self.assertEqual(report.failures, ["--body-only: needs --pr-body; the privacy rule reads it"])
        with in_directory(self.repo), contextlib.redirect_stderr(io.StringIO()) as said:
            self.assertEqual(lint.main(["--body-only"]), 2)
        self.assertIn("--body-only needs --pr-body", said.getvalue())

    def test_cli_a_body_that_is_not_utf8_is_refused_by_name_and_not_by_traceback(self) -> None:
        # `UnicodeDecodeError` is a `ValueError`, not an `OSError`; a body of
        # bytes that do not decode is a named refusal with exit 2 (#972 review).
        body = self.repo / "body.md"
        body.write_bytes(b"## Summary\n\xff\n")
        with in_directory(self.repo), contextlib.redirect_stderr(io.StringIO()) as said:
            self.assertEqual(lint.main(["--body-only", "--pr-body", str(body)]), 2)
        self.assertIn("error: cannot read --pr-body: 'utf-8' codec can't decode", said.getvalue())


class PullRequestPrivacyTests(LintFixture):
    """sd:2999. `--pr-body` checks one thing: a public body leaks nothing private.

    The patterns are the operator's privacy-pattern file, as `local-leak-guard`
    reads it; the body lines it once carried (`Work:` and the scope lines) are
    neither required nor refused.
    """

    def setUp(self) -> None:
        super().setUp()
        self.patterns = self.write_patterns("# a comment\nsecret-host\\.example\\.test\n")

    def test_red_a_line_matching_a_pattern_is_named_by_number(self) -> None:
        report = self.run_lint("## Summary\n\n- reach secret-host.example.test\n", body_only=True)
        self.assertEqual(
            report.failures,
            [f"pull request body: line 3 matches a privacy pattern in {self.patterns}"])

    def test_red_a_body_line_over_72_characters_is_named(self) -> None:
        """pack sd:10212: the builder's own lint run catches what the lane refused at enqueue."""
        report = self.run_lint("## Summary\n\n" + "z" * 73 + "\n")
        self.assertEqual(report.failures, ["pull request body: line 3 is 73 characters (limit 72)"])

    def test_green_a_body_only_run_measures_no_line(self) -> None:
        """pack sd:10220: `--body-only` lints a foreign repository's body, which no lane line check
        refuses, and `sd-ship` measures its own body first (`sd_ship_body.docs_lint`)."""
        report = self.run_lint("## Summary\n\n" + "z" * 73 + "\n", body_only=True)
        self.assertEqual(report.failures, [])

    def test_green_owned_and_scope_lines_are_neither_required_nor_refused(self) -> None:
        body = "Work: sd:1\nWork: sd:2\nWork:\n\n## Summary\n\n- touches .github/workflows\n"
        self.assertEqual(self.run_lint(body, body_only=True).failures, [])

    def test_red_no_pattern_file_fails_and_names_the_remedy(self) -> None:
        """sd:3490. A machine without the file published bodies unchecked and said clean."""
        self.patterns.unlink()
        report = self.run_lint("## Summary\n", body_only=True)
        self.assertEqual(report.failures, [
            f"{self.patterns}: no privacy-pattern file, so the body is unchecked; "
            "set sd.privacy_patterns, or write one ERE per line to that file"])

    def test_red_a_pattern_file_with_no_pattern_fails(self) -> None:
        self.patterns.write_text("# only a comment\n\n", encoding="utf-8")
        report = self.run_lint("## Summary\n", body_only=True)
        self.assertEqual(len(report.failures), 1, report.failures)
        self.assertIn("holds no privacy pattern, so the body is unchecked", report.failures[0])

    def test_an_alternative_naming_this_repo_does_not_apply_in_it(self) -> None:
        """sd:10720: a site repo's short-name pattern refused its own image names, `shop-web:scan`."""
        self.patterns.write_text("operator|shop\n", encoding="utf-8")
        self.git("remote", "add", "origin", "git@github.com:example/shop-website.git")
        body = "## Summary\n\n- scan the shop-web:scan image\n"
        self.assertEqual(self.run_lint(body, body_only=True).failures, [])
        report = self.run_lint(body + "- ask the operator\n", body_only=True)
        self.assertEqual(report.failures,
                         [f"pull request body: line 4 matches a privacy pattern in {self.patterns}"])

    def test_a_pattern_that_matches_inside_the_repo_name_still_guards_other_text(self) -> None:
        """Review of sd:10720: `operator` matched inside `operators-site`, so it was dropped there and the
        operator's name passed in any body of that repo. Only the repo's own name is exempt."""
        self.patterns.write_text("operator|shop\n", encoding="utf-8")
        self.git("remote", "add", "origin", "git@github.com:example/operators-site.git")
        report = self.run_lint("## Summary\n\n- ask the operator\n", body_only=True)
        self.assertEqual(report.failures,
                         [f"pull request body: line 3 matches a privacy pattern in {self.patterns}"])

    def test_a_valid_pattern_reaches_grep_unchanged(self) -> None:
        """Review of sd:10720: splitting at `|` broke `[[:alpha:]|]x`, a valid ERE, into two invalid ones."""
        self.patterns.write_text("[[:alpha:]|]operator\n", encoding="utf-8")
        self.git("remote", "add", "origin", "git@github.com:example/shop-website.git")
        report = self.run_lint("## Summary\n\n- x|operator\n", body_only=True)
        self.assertEqual(report.failures,
                         [f"pull request body: line 3 matches a privacy pattern in {self.patterns}"])

    def test_only_the_whole_leading_segment_is_left_out(self) -> None:
        """`shops`, `my-shop` and `shop_key` are other text, and the pattern still guards them."""
        self.patterns.write_text("shop\n", encoding="utf-8")
        self.git("remote", "add", "origin", "git@github.com:example/shop-website.git")
        body = "## Summary\n\n- shop shop-web:scan shop-website\n- shops\n- my-shop\n- shop_key\n"
        self.assertEqual(self.run_lint(body, body_only=True).failures,
                         [f"pull request body: line {n} matches a privacy pattern in {self.patterns}"
                          for n in (4, 5, 6)])

    def own_repo(self, patterns: str, body: str) -> list[str]:
        """The failures for `body` in a repo whose origin names it `shop-website`."""
        self.patterns.write_text(patterns, encoding="utf-8")
        self.git("remote", "add", "origin", "git@github.com:example/shop-website.git")
        return self.run_lint("## Summary\n\n" + body, body_only=True).failures

    def line(self, *numbers: int) -> list[str]:
        return [f"pull request body: line {n} matches a privacy pattern in {self.patterns}" for n in numbers]

    def test_a_match_that_runs_past_the_own_name_fails(self) -> None:
        """Round 3 class pass, sd:10720: with the name cut from the scanned text, `shop/keys` was never seen."""
        self.assertEqual(self.own_repo("shop/keys\nshop\n", "- see shop/keys\n- the shop-web image\n"), self.line(3))

    def test_a_match_that_runs_into_the_own_name_fails(self) -> None:
        self.assertEqual(self.own_repo("keys/shop\nshop\n", "- see keys/shop-web\n"), self.line(3))

    def test_a_match_another_match_overlaps_fails(self) -> None:
        """`grep -o` reports `shop` and steps over `op-private`, which starts inside it."""
        self.assertEqual(self.own_repo("shop\nop-private\n", "- the shop-private host\n"), self.line(3))

    def test_a_match_crossing_two_name_matches_fails(self) -> None:
        """`grep -o` reports both `shop`s and steps over `hop/shop`, which crosses them:
        blanking both first bytes at once erased it before the next read."""
        self.assertEqual(self.own_repo("shop\nhop/shop\n", "- read shop/shop/config\n"), self.line(3))

    def test_a_pattern_that_matches_nothing_at_all_still_fails_every_line(self) -> None:
        """An empty match selects each line and reports no text: nothing shows it lies inside the name."""
        self.assertEqual(self.own_repo("shop|x*\n", "- the shop-web image\n"), self.line(1, 2, 3))

    def test_a_name_only_the_folder_carries_exempts_nothing(self) -> None:
        """A PR publishes origin's name; a checkout's folder name is local, so it frees no text."""
        self.patterns.write_text("shop\n", encoding="utf-8")
        with mock.patch.object(lint.sd_lib, "main_worktree_root", return_value=pathlib.Path("/x/shop-website")):
            report = self.run_lint("## Summary\n\n- the shop-web image\n", body_only=True)
        self.assertEqual(report.failures, self.line(3))

    def test_an_alternative_naming_another_repo_still_applies(self) -> None:
        self.patterns.write_text("operator|shop\n", encoding="utf-8")
        self.git("remote", "add", "origin", "https://github.com/example/other-site")
        report = self.run_lint("## Summary\n\n- scan the shop-web:scan image\n", body_only=True)
        self.assertEqual(report.failures,
                         [f"pull request body: line 3 matches a privacy pattern in {self.patterns}"])

    def test_green_no_body_needs_no_pattern_file(self) -> None:
        self.patterns.unlink()
        self.assertEqual(self.run_lint().failures, [])


class NoDefaultWorkRootTests(LintFixture):
    """A repository that tracks its work in sd has no `docs/work` (sd:1570).

    From the root of traces-poc the linter printed `FAIL <repo>/docs/work:
    the work directory does not exist` and exited 1, so a valid repository
    failed. The default root missing is a skip; a root named with
    `--work-dir` and missing is still the failure it was.
    """

    def setUp(self) -> None:
        super().setUp()
        shutil.rmtree(self.work)
        self.git("add", "-A")

    def cli(self, *argv: str) -> tuple[int, str, str]:
        with in_directory(self.repo), contextlib.redirect_stderr(io.StringIO()) as said, \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            code = lint.main(list(argv))
        return code, printed.getvalue(), said.getvalue()

    def test_red_the_default_root_missing_is_a_skip_and_exit_0(self) -> None:
        code, printed, said = self.cli()
        self.assertEqual(code, 0, said)
        self.assertIn("rules 1-2: no docs/work; nothing to lint", said)
        self.assertIn("sd-docs-lint: clean", printed)
        self.assertNotIn("does not exist", said)

    def test_red_rule_7_skips_without_the_default_root(self) -> None:
        """No work root, so a `docs/work` path names another repository's (sd:6673).

        The vault's notes cite other repositories' `docs/work` pages; rule 7
        failed it 71 times on main and its build could not enqueue.
        """
        (self.repo / "README.md").write_text(
            "# readme\n\nSee `docs/work/x/prd.md`.\n", encoding="utf-8"
        )
        self.git("add", "-A")
        code, printed, said = self.cli()
        self.assertEqual(code, 0, said)
        self.assertIn(
            "rule 7 work references: no docs/work in this checkout, nothing to check", said)
        self.assertNotIn("names nothing in the checkout", said)

    def test_the_other_rules_still_run_without_the_default_root(self) -> None:
        body = self.repo / "body.md"
        body.write_text("## Summary\n", encoding="utf-8")
        self.write_patterns()
        code, printed, said = self.cli("--pr-body", str(body))
        self.assertEqual(code, 0, said)
        self.assertIn("PR privacy: 1 pattern(s) from", printed)
        self.assertIn("rule 3 decision shape", said)

    def test_an_explicit_work_dir_that_is_missing_still_fails(self) -> None:
        code, _, said = self.cli("--work-dir", "docs/work")
        self.assertEqual(code, 1)
        self.assertIn("the work directory does not exist", said)


class MinimalModeTests(LintFixture):
    """A `mode: minimal` repository keeps `docs/work` local by policy (sd:3177).

    Its tracked pages name `docs/work/` records a fresh clone never holds, and
    an untracked local folder carries whatever shape it likes. Rules 1, 2 and
    7 read that folder and those names, so they skip with a note; the rules
    that read tracked trees still run.
    """

    def setUp(self) -> None:
        super().setUp()
        self.local_file = self.repo / "CLAUDE.local.md"
        self.write_mode("minimal")
        (self.repo / "README.md").write_text(
            "# readme\n\nSee `docs/work/2026-01-01-local-only/design.md`.\n", encoding="utf-8"
        )
        (self.work / "2026-08-29-a-workable-item" / "notes.txt").write_text("local\n", encoding="utf-8")

    def write_mode(self, value: str) -> None:
        self.local_file.write_text(
            f"{lint.sd_lib.LOCAL_BLOCK_START}\nmode: {value}\n{lint.sd_lib.LOCAL_BLOCK_END}\n",
            encoding="utf-8",
        )

    def test_green_work_rules_skip_with_a_note(self) -> None:
        report = self.run_lint()
        self.assertEqual(report.failures, [])
        self.assertIn("rules 1-2, 7: mode: minimal keeps docs/work local; not run", report.warnings)

    def test_red_the_same_tree_in_full_mode_still_fails_both_rules(self) -> None:
        self.write_mode("full")
        joined = "\n".join(self.run_lint().failures)
        self.assertIn("docs/work/2026-01-01-local-only/design.md names nothing in the checkout", joined)
        self.assertIn("design.md, or the legacy prd.md and implement.md, only", joined)

    def test_red_the_tracked_tree_rules_still_run(self) -> None:
        self.write_spec("tooling", ["lanes.md", "gates.md"], index="# tooling\n\n- [lanes](./lanes.md)\n")
        self.assert_fails("index does not link gates.md")

    def test_red_an_unreadable_mode_line_is_a_failure_not_a_skip(self) -> None:
        self.write_mode("tiny")
        joined = "\n".join(self.run_lint().failures)
        self.assertIn("mode 'tiny' is not one of", joined)
        self.assertIn("names nothing in the checkout", joined)


class RepositoryTests(unittest.TestCase):
    def test_this_repository_is_clean(self) -> None:
        # No history, as `make check` lints it: this runs inside the gate too,
        # and with history it fetches the remote once per item (sd:2606).
        report = lint.run(REPO_ROOT, "docs/work", "docs/spec", "docs/decisions", None, history=False)
        self.assertEqual(report.failures, [])

    def test_missing_work_directory_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            report = lint.run(
                pathlib.Path(name), "docs/work", "docs/spec", "docs/decisions", None
            )
            self.assertEqual(len(report.failures), 1)
            self.assertIn("does not exist", report.failures[0])

    def test_frontmatter_reads_quoted_scalars(self) -> None:
        fields = lint.parse_frontmatter('---\ntitle: "PARKED: do a thing"\nstatus: planning\n---\n')
        assert fields is not None
        self.assertEqual(fields["title"], "PARKED: do a thing")
        self.assertEqual(fields["status"], "planning")

    def test_frontmatter_absent_returns_none(self) -> None:
        self.assertIsNone(lint.parse_frontmatter("# PRD\n"))

    def test_cli_reports_clean_on_this_repository(self) -> None:
        # There is no --repo any more (R10-D6): the linter reads cwd, so the
        # test has to stand in the repository it means to lint.
        with in_directory(REPO_ROOT):
            self.assertEqual(lint.main(["--no-history"]), 0)

    def test_cli_rejects_an_unreadable_pr_body(self) -> None:
        with in_directory(REPO_ROOT):
            self.assertEqual(lint.main(["--pr-body", str(REPO_ROOT / "no-such-file")]), 2)

    def test_cli_refuses_outside_a_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as raw, in_directory(pathlib.Path(raw)):
            self.assertEqual(lint.main([]), 2)


class MetavariableTests(unittest.TestCase):
    """`METAVARIABLE_RE` reads a token, not a substring.

    Two readers share it: rule 7 here and the pull-request template walk in
    `tests/test_pull_request_template_links.py`. Both skip what it matches, so
    a match on a real name is a check that silently did not happen (sd:801).
    """

    def test_a_placeholder_is_a_whole_token(self) -> None:
        for token in ("<YYYY-MM-DD>-<slug>", "YYYY-MM-DD-slug", "archive/YYYY-MM/",
                      "YYYY_MM", "DD.md", "<id>", "MM"):
            with self.subTest(token=token):
                self.assertIsNotNone(lint.METAVARIABLE_RE.search(token))

    def test_a_name_that_merely_contains_the_letters_is_not_one(self) -> None:
        # The last two names separate the halves of the boundary, and until
        # they were added nothing did. Five of the names above them carry a
        # date token, and each of those sits between two alphanumerics -- `MM`
        # inside `COMMANDS` and `SUMMARY`, `DD` inside `ADDING`, `YYYY` inside
        # `HAPPYYYYEAR`, `MM` between the zeroes of `0MM0` -- so either
        # lookaround alone rejects all five, while the remaining two carry no
        # token and ask neither lookaround anything. Deleting either one left
        # all 133 tests green (sd:833). Only the lookbehind rejects
        # `docs/ADD/plan.md`, where a `/` follows the `DD`; only the lookahead
        # rejects `docs/MMap.md`, where a `/` precedes the `MM`.
        for token in ("docs/COMMANDS.md", "ADDING.md", "HAPPYYYYEAR", "2026-08-29-slug",
                      "SUMMARY.md", "0MM0", "docs/work/2026-09-04-an-item/prd.md",
                      "docs/ADD/plan.md", "docs/MMap.md"):
            with self.subTest(token=token):
                self.assertIsNone(lint.METAVARIABLE_RE.search(token))


class Rule7WorkReferenceTests(LintFixture):
    """A `docs/work/` path a document names has to resolve.

    No deletion is needed to break one. This repository's own case is a move:
    `2026-08-29-artifacts-as-product` went into the archive after fourteen
    tracked lines had already linked to it where it used to be, and until this
    rule nothing said the links had stopped resolving.
    """

    def name_it(self, reference: str, page: str = "NOTES.md") -> None:
        (self.repo / page).write_text(
            f"# notes\n\nThe shape is described in `{reference}`.\n", encoding="utf-8"
        )

    def test_red_a_page_naming_an_item_directory_that_is_not_there(self) -> None:
        self.name_it("docs/work/2026-08-29-an-item-that-was-never-created/prd.md")
        joined = "\n".join(self.assert_fails("names nothing in the checkout"))
        self.assertIn("NOTES.md", joined)
        self.assertIn("2026-08-29-an-item-that-was-never-created/prd.md", joined)

    def test_green_a_metavariable_is_a_pattern_and_not_a_path(self) -> None:
        self.name_it("docs/work/<YYYY-MM-DD>-<slug>/prd.md")
        self.assert_clean()

    def test_green_a_bare_date_placeholder_is_a_pattern_without_the_brackets(self) -> None:
        # `YYYY`, `MM` and `DD` as whole tokens carry the skip on their own;
        # the case above carries it on `<` as well, so it cannot say so.
        self.name_it("docs/work/YYYY-MM-DD-slug/prd.md")
        self.assert_clean()
        self.assertIn("and 1 template reference(s)", self.notes())

    def test_red_a_name_carrying_a_date_letter_pair_is_a_path_and_not_a_pattern(self) -> None:
        """`COMMANDS` carries `MM` and `ADDING` carries `DD`.

        Matched as substrings, `METAVARIABLE_RE` made either name a template,
        and a reference to an item that was never created was passed over
        instead of failed. No tracked name carried one when this was found,
        so nothing was hidden yet; the boundary is what keeps it that way
        (sd:801).
        """
        for slug in ("2026-08-29-COMMANDS-that-do-not-exist",
                     "2026-08-29-ADDING-what-is-not-there",
                     "2026-08-29-HAPPYYYYEAR"):
            with self.subTest(slug=slug):
                self.name_it(f"docs/work/{slug}/prd.md")
                joined = "\n".join(self.assert_fails("names nothing in the checkout"))
                self.assertIn(slug, joined)
                self.assertIn("and 0 template reference(s)", self.notes())

    def test_green_a_reference_that_resolves(self) -> None:
        self.name_it("docs/work/2026-08-29-a-workable-item/prd.md")
        self.assert_clean()

    def test_the_archive_is_read_past(self) -> None:
        """Its items are records, and its own links point inside itself."""
        self.write_item("2026-08-29-an-archived-item", ARCHIVED_PRD, month="2026-08")
        archived = self.work / "archive" / "2026-08" / "2026-08-29-an-archived-item"
        (archived / "design.md").write_text(
            "# design\n\nSee `docs/work/2026-01-01-long-gone/prd.md`.\n", encoding="utf-8"
        )
        self.assert_clean()

    def test_the_changelog_is_read_past(self) -> None:
        """It names paths as they were, which is where a stale one is correct."""
        self.name_it("docs/work/2026-01-01-long-gone/prd.md", page="CHANGELOG.md")
        self.assert_clean()

    def test_red_the_note_counts_the_files_and_references_it_skipped(self) -> None:
        """Backbone item sd:5. Both exemptions keep their reason, lose their silence.

        `read 77 reference(s) across 143 file(s)` was rule 7's whole report on
        this repository while 954 tracked documents went unread under
        `archive/` or as `CHANGELOG.md`, and 11 references were passed over as
        templates. Neither skip is wrong and neither is being removed. What
        was wrong is that a reader given one number cannot tell a narrow
        corpus from a whole one, and a skip nobody can count is a skip nobody
        can audit.

        Here: one archived document and one `CHANGELOG.md` unread, one
        metavariable reference passed over, one real reference read.
        """
        self.write_item("2026-08-29-an-archived-item", ARCHIVED_PRD, month="2026-08")
        self.name_it("docs/work/2026-01-01-long-gone/prd.md", page="CHANGELOG.md")
        self.name_it(
            "docs/work/<YYYY-MM-DD>-<slug>/prd.md and "
            "docs/work/2026-08-29-a-workable-item/prd.md"
        )
        report = self.run_lint()
        self.assertEqual(report.failures, [])
        self.assertIn(
            "rule 7 work references: read 1 reference(s) across 4 file(s); "
            "skipped 2 file(s) under docs/work/archive/ or named CHANGELOG.md, "
            "and 1 template reference(s)",
            "\n".join(report.notes),
        )

    def test_an_untracked_page_is_not_a_document_this_repository_publishes(self) -> None:
        self.assert_clean()
        self.name_it("docs/work/2026-01-01-long-gone/prd.md")
        # No `git add` here, so `ls-files` does not see the page and the rule
        # does not read it. Deliberate: a scratch file beside a checkout is not
        # something the repository says.
        report = lint.run(self.repo, "docs/work", "docs/spec", "docs/decisions", None)
        self.assertEqual(report.failures, [])

    def test_a_directory_git_cannot_enumerate_fails_rather_than_passes(self) -> None:
        """The pass-on-nothing failure this whole rule set exists to close.

        `ls-files` outside a repository answers nothing, and a rule that reads
        nothing has checked nothing. Reporting that as clean is the shape of
        every silent fail-open, so the rule says so instead.
        """
        with tempfile.TemporaryDirectory() as scratch:
            outside = pathlib.Path(scratch)
            (outside / "docs" / "work").mkdir(parents=True)
            report = lint.run(outside, "docs/work", "docs/spec", "docs/decisions", None)
        joined = "\n".join(report.failures)
        self.assertIn("rule 7 read nothing", joined)


BAD_REFERENCE = "docs/work/2026-01-01-long-gone/prd.md"
NOTES = f"# notes\n\nThe shape is described in `{BAD_REFERENCE}`.\n"


class Rule7UnmergedIndexTests(unittest.TestCase):
    """What rule 7 reports while a merge is still being resolved.

    `sd-docs-lint` is run *during* the merge workflow, which is the only
    reason this case is worth building by hand: an unmerged path sits in the
    index once per stage, so the enumeration hands the rule the same document
    three times, and it is read, linted and reported on three times.

    The fixture resolves the working tree and deliberately leaves it
    unstaged. That is the state a person is actually in -- the conflict is
    fixed in the editor, the file on disk is ordinary markdown again, nothing
    warns them, and the index still carries three stages.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = pathlib.Path(self._tmp.name)
        self._build()

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            # Identity and signing are set per invocation rather than read
            # from the machine, so the fixture does not fail on a host with no
            # `user.email` or one that signs every commit.
            ["git", "-C", str(self.repo),
             "-c", "user.email=lint@example.invalid",
             "-c", "user.name=docs lint",
             "-c", "commit.gpgsign=false", *args],
            capture_output=True, text=True, check=check,
        )

    def _build(self) -> None:
        """A real merge, not a hand-written index.

        The three stages have to come from git's own conflict machinery, or
        the fixture restates the belief under test instead of evidencing it.
        """
        notes = self.repo / "NOTES.md"
        item = self.repo / "docs" / "work" / "2026-08-29-a-workable-item"
        item.mkdir(parents=True)
        (item / "prd.md").write_text(GOOD_PRD, encoding="utf-8")

        self.git("init", "-q", "-b", "main")
        notes.write_text("# notes\n\nbase\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.git("checkout", "-q", "-b", "other")
        notes.write_text("# notes\n\ntheir side\n", encoding="utf-8")
        self.git("commit", "-qam", "other")
        self.git("checkout", "-q", "main")
        notes.write_text("# notes\n\nmy side\n", encoding="utf-8")
        self.git("commit", "-qam", "mine")
        self.git("merge", "other", check=False)
        # Resolved on disk, never staged.
        notes.write_text(NOTES, encoding="utf-8")

    def assert_the_index_is_unmerged(self) -> None:
        stages = self.git("ls-files", "-u", "--", "NOTES.md").stdout
        self.assertEqual(
            [line.split("\t")[0].split()[-1] for line in stages.splitlines()],
            ["1", "2", "3"],
            "the fixture did not leave an unmerged index, so the cases below "
            "prove nothing -- they would pass against any clean checkout",
        )
        # ls-files-form: plain -- the repetition is what this case asserts
        listed = self.git("ls-files", "-z", "--", "NOTES.md").stdout
        self.assertEqual(
            [name for name in listed.split("\0") if name],
            ["NOTES.md"] * 3,
            "this git no longer repeats an unmerged path; if that is now the "
            "default, say so here rather than deleting the case",
        )

    def test_a_conflicted_document_is_reported_once(self) -> None:
        """One bad reference in one file is one finding, not three.

        This is what the person resolving the merge reads, so it is asserted
        on the report rather than on the path list: a rule that deduplicated
        its enumeration but still counted per stage would look fixed and
        print the same three lines.
        """
        self.assert_the_index_is_unmerged()
        report = lint.run(self.repo, "docs/work", "docs/spec", "docs/decisions", None)
        self.assertEqual(
            [f for f in report.failures if "names nothing in the checkout" in f],
            [f"NOTES.md: {BAD_REFERENCE} names nothing in the checkout"],
            "a conflicted document was linted once per merge stage",
        )

    def test_the_totals_count_the_conflicted_document_once(self) -> None:
        """The counters, which a deduplicated path list alone would not fix.

        Two files are readable here -- `NOTES.md` and the item's `prd.md` --
        and between them they name one `docs/work/` path. Both numbers are
        read rather than computed, so the case cannot agree with the defect.

        Matched as a prefix of the note rather than as the whole of it, so
        that the skip counts the note gained for sd:5 -- and anything else it
        gains later -- cannot make this case pass or fail for a reason that
        has nothing to do with merge stages. The two numbers it is about are
        still pinned exactly.
        """
        self.assert_the_index_is_unmerged()
        report = lint.run(self.repo, "docs/work", "docs/spec", "docs/decisions", None)
        self.assertIn(
            "rule 7 work references: read 1 reference(s) across 2 file(s)",
            "\n".join(report.notes),
            f"inflated totals: {report.notes}",
        )


class WorkDirSpellingTests(LintFixture):
    """A spelling that opens a root is read as that root, or is refused.

    `--work-dir` is a path to rules 1, 2, 5 and 6 and a *literal prefix* to
    rule 7, which matches it against the text of tracked markdown where a
    `docs/work/` reference is written repo-relative and nothing else. Before
    sd:374 every spelling but the plain relative one made those two meanings
    disagree without saying so: `./docs/work`, `docs/work/`,
    `docs/../docs/work` and the tree's own absolute path each left rule 7
    matching nothing, reporting `read 0 reference(s)`, and passing over the
    dangling reference the relative spelling catches. A rule that reads
    nothing has checked nothing, which is the failure the rule's own
    `git could not list tracked markdown` branch already refuses to commit.

    This class first claimed the general form -- one root however it is
    spelled -- and the review of #833 showed that claim false on this
    platform, which is the reason for the hedged sentence above. `resolve()`
    canonicalises `..`, symlinks and separators but not *case*, so on APFS
    `--work-dir DOCS/WORK` opened the real tree for rules 1, 2, 5 and 6 and
    survived as `DOCS/WORK` into rule 7: 77 references across 143 files
    became 0 across 1096, and the command still exited 0. The canonical
    spelling now comes from the directory entry, so the claim holds where the
    filesystem folds case; where it does not fold case, a mis-cased root
    opens nothing and is reported missing. Both are answers. Neither is the
    silent pass.
    """

    def case_folds(self) -> bool:
        """Whether this filesystem opens `DOCS` and `docs` as one directory."""
        return (self.repo / "DOCS" / "WORK").is_dir()

    def setUp(self) -> None:
        super().setUp()
        (self.repo / "NOTES.md").write_text(
            "See `docs/work/2026-01-01-never-created/prd.md`.\n", encoding="utf-8"
        )

    def report_for(self, work_dir: str) -> lint.Report:
        self.git("add", "-A")
        return lint.run(self.repo, work_dir, "docs/spec", "docs/decisions", None)

    def rule_7_note(self, report: lint.Report) -> str:
        notes = [note for note in report.notes if note.startswith("rule 7")]
        self.assertEqual(len(notes), 1, report.notes)
        return notes[0]

    def test_the_relative_spelling_is_the_baseline_and_catches_the_reference(self) -> None:
        report = self.report_for("docs/work")
        self.assertIn("read 1 reference(s)", self.rule_7_note(report))
        self.assertIn("names nothing in the checkout", "\n".join(report.failures))

    def test_an_absolute_root_inside_the_repository_reads_the_same_tree(self) -> None:
        """The same directory, spelled the long way, is the same directory."""
        baseline = self.report_for("docs/work")
        absolute = self.report_for(str(self.work))
        self.assertEqual(self.rule_7_note(absolute), self.rule_7_note(baseline))
        self.assertEqual(absolute.failures, baseline.failures)

    def test_every_accepted_spelling_reads_the_same_references(self) -> None:
        baseline = self.rule_7_note(self.report_for("docs/work"))
        for spelling in ("./docs/work", "docs/work/", "docs/../docs/work", "docs//work"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.rule_7_note(self.report_for(spelling)), baseline)

    def test_a_root_outside_the_repository_refuses_rather_than_half_applies(self) -> None:
        """The recorded case: rules 1-2-6 read there, rule 7 read here.

        Rule 7's enumeration and rule 2's row database are rooted at the
        checkout the caller stands in, so a foreign work root is not a root
        this run can honour. It says so.
        """
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = pathlib.Path(elsewhere) / "docs" / "work"
            outside.mkdir(parents=True)
            report = self.report_for(str(outside))
        self.assertEqual(len(report.failures), 1, report.failures)
        self.assertIn("is outside", report.failures[0])
        self.assertEqual([note for note in report.notes if note.startswith("rule 7")], [])

    def test_the_repository_root_is_not_a_work_directory(self) -> None:
        report = self.report_for(".")
        self.assertEqual(len(report.failures), 1, report.failures)
        self.assertIn("itself, not a directory inside it", report.failures[0])

    def test_cli_refuses_a_work_dir_outside_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere, in_directory(self.repo):
            outside = pathlib.Path(elsewhere) / "docs" / "work"
            outside.mkdir(parents=True)
            self.assertEqual(lint.main(["--work-dir", str(outside)]), 2)

    def test_cli_refuses_a_spec_dir_outside_the_repository(self) -> None:
        """No second meaning, but a verdict over a mixture is still wrong."""
        with tempfile.TemporaryDirectory() as elsewhere, in_directory(self.repo):
            outside = pathlib.Path(elsewhere) / "docs" / "spec"
            outside.mkdir(parents=True)
            self.assertEqual(lint.main(["--spec-dir", str(outside)]), 2)

    def test_a_case_only_misspelling_is_not_a_second_work_root(self) -> None:
        """The #833 finding: `resolve()` does not canonicalise case.

        On a case-folding filesystem `DOCS/WORK` is the same directory, so it
        has to read the same; on one that does not fold, it is no directory at
        all, so it has to be reported missing. The outcome this refuses is the
        third one, which is what the tool did: open the right tree, keep the
        typed string, read nothing with it, and exit 0.
        """
        baseline = self.report_for("docs/work")
        shouted = self.report_for("DOCS/WORK")
        if self.case_folds():
            self.assertEqual(self.rule_7_note(shouted), self.rule_7_note(baseline))
            self.assertEqual(shouted.failures, baseline.failures)
        else:
            self.assertEqual(len(shouted.failures), 1, shouted.failures)
            self.assertIn("does not exist", shouted.failures[0])

    def test_the_canonical_spelling_comes_from_the_directory_entry(self) -> None:
        self.assertEqual(lint.spelled_on_disk(self.repo, "docs"), "docs")
        # Left as typed, so the caller reports the directory that is not there
        # rather than this inventing one that is.
        self.assertEqual(lint.spelled_on_disk(self.repo, "not-a-directory"), "not-a-directory")
        if self.case_folds():
            self.assertEqual(lint.spelled_on_disk(self.repo, "DOCS"), "docs")

    def test_run_guards_every_root_and_not_only_the_work_dir(self) -> None:
        """An absolute right-hand operand wins in pathlib, for all three.

        The foreign tree here is deliberately *valid*, so that a `run` which
        only guards `work_dir` reports no failures at all -- which is what it
        did: rules 3 and 4 lint one repository while the rest lint another,
        and the verdict over that mixture is a clean one.
        """
        for flag, relative in (("--spec-dir", "docs/spec"), ("--decisions-dir", "docs/decisions")):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as elsewhere:
                outside = pathlib.Path(elsewhere) / relative
                outside.mkdir(parents=True)
                (outside / "page.md").write_text("# page\n", encoding="utf-8")
                (outside / "index.md").write_text(
                    "# index\n\n- [page](./page.md)\n", encoding="utf-8"
                )
                self.git("add", "-A")
                report = lint.run(
                    self.repo,
                    "docs/work",
                    str(outside) if flag == "--spec-dir" else "docs/spec",
                    str(outside) if flag == "--decisions-dir" else "docs/decisions",
                    None,
                )
                self.assertEqual(len(report.failures), 1, report.failures)
                self.assertIn(flag, report.failures[0])
                self.assertIn("is outside", report.failures[0])

    def test_cli_accepts_an_absolute_work_dir_inside_the_repository(self) -> None:
        self.git("add", "-A")
        (self.repo / "NOTES.md").unlink()
        self.git("add", "-A")
        with in_directory(self.repo):
            self.assertEqual(lint.main(["--work-dir", str(self.work)]), 0)


class ArchiveIsBelowTheWorkRootTests(unittest.TestCase):
    """sd:1540. Only `archive/` below the work root is history.

    The #1177 review found a checkout under a directory named `archive`
    compared `0 of 0` items; the other checks read the same absolute path.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work_root = pathlib.Path(tmp.name).resolve() / "archive" / "repo" / "docs" / "work"
        self.item = self.work_root / "2026-09-01-thing"
        self.item.mkdir(parents=True)
        (self.item / "prd.md").write_text(GOOD_PRD, encoding="utf-8")

    def test_an_item_is_archived_only_below_the_work_root(self) -> None:
        self.assertFalse(lint.is_archived(self.item, self.work_root))
        self.assertTrue(lint.is_archived(self.work_root / "archive" / "2026-08" / "old", self.work_root))

    def test_no_check_reads_archive_from_the_absolute_path(self) -> None:
        source = LINT_PATH.read_text(encoding="utf-8")
        self.assertEqual(re.findall(r".*in item\.parts.*", source), [])


if __name__ == "__main__":
    unittest.main()
