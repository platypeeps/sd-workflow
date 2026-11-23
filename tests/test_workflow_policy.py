"""`WORKFLOW.md` is the policy, and the payload agrees with it.

Six things drift silently and each has a test here.

**The override keys.** The `CLAUDE.local.md` block the installer writes and the
Overrides section of `WORKFLOW.md` describe the same set of keys. Neither is
checked against a list written down beside it: the expectation is enumerated
from `sd_lib.MODES`, `sd_lib.CHECK_NAMES`, `sd_lib.CONSENT_KEY` and
`sd_lib.GUEST_ALLOW_KEY` in the source, so adding a seventh key to the library
and to only one of the two pages fails here.

**The review table.** It appears in exactly two files and the two copies are
byte-identical. A cap edited in one place and not the other is the failure this
catches; a third copy is the failure the count catches.

**The planning review rule.** Exactly one file states it. Every other file that
needs it links to that one.

**The deleted second-model lane.** It is named nowhere in the governed tree.
`CHANGELOG.md` and `docs/work/` are history and are excluded by name, since the
archive is kept unchanged and holds every name the cuts removed.

**The bare vendor token.** No file under `skills/` names `codex`, `claude`,
`openai` or `anthropic` as a bare token: a skill names a role or a registry
entry, and the registry maps it to a vendor. The grep was once zero and nothing
pinned it, so three tokens came back in `skills/sd-review/SKILL.md` unnoticed.

**The skills that run a review.** Each names its point in the review table and
reads the cap from the one rule file, and none carries a cap of its own. The
skills are enumerated from what their pages invoke, not from a list kept here.
"""

# This module reads the whole checkout, so no changed-files fast path may
# narrow it away. `.github/scripts/select-tests.py` greps for the line below.
# select-tests: always-run

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for entry in (REPO_ROOT, REPO_ROOT / "bin"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import sd_install  # noqa: E402
import sd_lib  # noqa: E402

#: What runs or governs, read off the index by `tests/governed.py`. This was a
#: third hand-typed copy of the same ten names.
from tests.governed import GOVERNED  # noqa: E402

WORKFLOW = REPO_ROOT / "WORKFLOW.md"
RULE = REPO_ROOT / ".claude/rules/sd-planning-adversarial-review.md"

TABLE_HEADER = "| Flow | Point | What it checks | Cap |"

SELF = "tests/test_workflow_policy.py"

#: A bare vendor token: one of the four names with none of `/`, `.`, `_`, `~`
#: or `-` against either side. That shape drops paths, filenames, environment
#: variables, MCP tool identifiers and URLs, and keeps a vendor named as a
#: choice of who runs a pass. Only those five characters exempt a neighbour, as
#: the criterion states it: a letter beside the name does not, so `Codexes`
#: counts too.
BARE_VENDOR = re.compile(
    r"(?<![/._~-])(codex|claude|openai|anthropic)(?![/._~-])", re.IGNORECASE
)

#: The one product name the rule exempts. It is blanked before matching, not
#: used to skip a line, so a bare token sharing a line with it still counts.
#: Every separator it accepts is named here, because each one is a hole in the
#: gate and an unnamed hole is an unpinned one. They are: a run of spaces or
#: tabs, or exactly one line break -- LF or CRLF -- with spaces or tabs
#: allowed either side of it, since the pages hard-wrap and a reflow must not
#: turn the gate red. Nothing else. A blank line is two breaks, not a wrap,
#: and stays refused; so does a form feed. `\s` is not the shape, and the
#: second reason is the one that bites: `str.splitlines()` below treats a form
#: feed as a line break, while the blank reinstates only `\n`, so a name
#: spanning one would report every row after it a line early. The blank keeps
#: the break it spans, so line numbers do not move. `\b` stops it short of a
#: longer word: `Claude Codex` is not the product, and blanking its first
#: eleven characters would hide the `codex` in it.
PRODUCT_NAME = re.compile(r"Claude(?:[ \t]+|[ \t]*\r?\n[ \t]*)Code\b")

#: An indented `key: value` line inside a fenced-free block.
KEY_LINE = re.compile(r"^ {4}([a-z][a-z_]*):")


def governed_grep(pattern: str) -> list[str]:
    """`git grep` over the governed tree, returning `path:line:text` rows."""
    result = subprocess.run(
        # This file is excluded: it quotes every pattern it searches for, and
        # a test that matches itself measures nothing.
        ["git", "grep", "-nIE", pattern, "--", *GOVERNED, f":(exclude){SELF}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(result.stderr.strip())
    return [line for line in result.stdout.splitlines() if line]


def bare_vendor_lines(text: str) -> list[int]:
    """The 1-based numbers of the lines carrying a bare vendor token."""
    blanked = PRODUCT_NAME.sub(lambda match: re.sub(r"[^\n]", " ", match[0]), text)
    return [
        number
        for number, line in enumerate(blanked.splitlines(), start=1)
        if BARE_VENDOR.search(line)
    ]


def tracked_files(top: str, root: pathlib.Path = REPO_ROOT) -> list[str]:
    """Every tracked file under `top`, whatever its suffix. Enumerated from the
    index, so a new skill is covered on arrival.

    `--deduplicate` because the index holds an unmerged path once per merge
    stage, and plain `ls-files` prints it once per stage. No verdict moves:
    the sweep filters this list and compares lists, so three copies of an
    offender is still an offender and three copies of a clean file is still
    nothing -- which is why it went unnoticed. What moves is the report, which
    named one file three times to somebody mid-merge who was already hunting
    for what they had just broken. `tests/test_no_shipped_shell.py` reads the
    index this way; this call did not, and one right form beside one wrong one
    is how the wrong one survives.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "--deduplicate", "--", top],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return list(filter(None, result.stdout.split("\0")))


def bare_vendor_tokens(names: list[str], root: pathlib.Path = REPO_ROOT) -> list[str]:
    """`path:line:text` for every bare vendor token in the files `names`,
    relative to `root`."""
    rows = []
    for name in names:
        raw = (root / name).read_bytes()
        # A binary file carries no token a reader sees. A tracked text file
        # that does not decode fails loudly rather than being skipped.
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8")
        lines = text.splitlines()
        rows.extend(f"{name}:{n}:{lines[n - 1].strip()}" for n in bare_vendor_lines(text))
    return rows


def expected_keys() -> set[str]:
    """The key set, enumerated from the library rather than from a list."""
    keys = {"mode"} if sd_lib.MODES else set()
    return keys | set(sd_lib.CHECK_NAMES) | {sd_lib.CONSENT_KEY, sd_lib.GUEST_ALLOW_KEY}


def section(text: str, heading: str) -> str:
    """One `## ` section of a markdown page, heading excluded."""
    lines = text.splitlines()
    start = lines.index(heading) + 1
    end = start
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    return "\n".join(lines[start:end])


def documented_keys(text: str) -> set[str]:
    """Every `    key:` line in an indented block."""
    return {m.group(1) for m in (KEY_LINE.match(line) for line in text.splitlines()) if m}


def overrides_keys(page: pathlib.Path) -> set[str]:
    return documented_keys(section(page.read_text(encoding="utf-8"), "## Overrides"))


def states_the_rule(path: pathlib.Path) -> bool:
    """A file states the rule when it carries both halves: the trigger and
    the table the caps come from. A file with one half is a pointer or a
    procedure, not a second statement."""
    text = path.read_text(encoding="utf-8")
    return TABLE_HEADER in text and "convergence" in text


def review_table(path: pathlib.Path) -> list[str]:
    """The contiguous pipe-block that starts at the review table's header."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = lines.index(TABLE_HEADER)
    end = start
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    return lines[start:end]


class WorkflowPage(unittest.TestCase):
    def test_page_exists_at_the_repository_root(self):
        self.assertTrue(WORKFLOW.is_file(), "WORKFLOW.md is missing from the root")

    def test_page_states_the_named_sections(self):
        headings = {
            line.strip()
            for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")
        }
        for wanted in (
            "## Two flows, one spine",
            "## Defaults",
            "## Opt-in",
            "## Reviews",
            "## Advisory",
            "## Never in a shared repository",
            "## The path for a change",
            "## Modes",
            "## Overrides",
        ):
            self.assertIn(wanted, headings)

    def test_every_mode_is_documented(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        for mode in sd_lib.MODES:
            self.assertIn(f"`{mode}`", text, f"WORKFLOW.md never names the {mode!r} mode")

    def test_the_installer_block_names_the_page(self):
        self.assertIn("WORKFLOW.md", sd_install.DEFAULT_BLOCK_BODY)


class MutationChecks(unittest.TestCase):
    """sd:1790: a same-size edit restored within a second reuses a stale `.pyc`."""

    def test_the_section_states_the_bytecode_rule(self):
        text = section(WORKFLOW.read_text(encoding="utf-8"), "## Mutation checks")
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", text)
        self.assertIn("__pycache__", text)

    def test_the_trap_is_real_and_the_rule_avoids_it(self):
        """The rule's premise, made deterministic by pinning the source mtime."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            module = root / "mutated.py"
            env = {k: v for k, v in os.environ.items()
                   if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}

            def write(text: str) -> None:
                module.write_text(text, encoding="utf-8")
                os.utime(module, (1_700_000_000, 1_700_000_000))

            def value(**extra: str) -> str:
                return subprocess.run(
                    [sys.executable, "-c", "import mutated; print(mutated.X)"], cwd=root,
                    env={**env, **extra}, capture_output=True, text=True, check=True, timeout=60,
                ).stdout.strip()

            write("X = 1\n")
            self.assertEqual(value(), "1")
            write("X = 2\n")
            self.assertEqual(value(), "1", "same size and second: the stale .pyc answers")
            for cached in (root / "__pycache__").iterdir():
                cached.unlink()
            write("X = 1\n")
            self.assertEqual(value(PYTHONDONTWRITEBYTECODE="1"), "1")
            write("X = 2\n")
            self.assertEqual(value(PYTHONDONTWRITEBYTECODE="1"), "2")


class OverrideKeys(unittest.TestCase):
    """The two pages and the library agree, and none of the three is a list."""

    def test_the_page_documents_the_keys_the_library_reads(self):
        self.assertEqual(overrides_keys(WORKFLOW), expected_keys())

    def test_the_installer_block_writes_the_keys_the_library_reads(self):
        self.assertEqual(documented_keys(sd_install.DEFAULT_BLOCK_BODY), expected_keys())

    def test_a_sixth_key_on_one_side_alone_fails(self):
        """The guard against the guard: a key added to the library and to
        neither page must not pass, or the two tests above prove nothing."""
        with_a_sixth = expected_keys() | {"sixth"}
        self.assertNotEqual(overrides_keys(WORKFLOW), with_a_sixth)
        self.assertNotEqual(documented_keys(sd_install.DEFAULT_BLOCK_BODY), with_a_sixth)


class ReviewTable(unittest.TestCase):
    def test_the_table_appears_in_exactly_one_file(self):
        """The rule file holds the one copy; `WORKFLOW.md` links to it."""
        rows = governed_grep(re.escape(TABLE_HEADER))
        files = {row.split(":", 1)[0] for row in rows}
        self.assertEqual(files, {".claude/rules/sd-planning-adversarial-review.md"})
        self.assertIn("sd-planning-adversarial-review.md", WORKFLOW.read_text(encoding="utf-8"))

    def test_the_table_has_a_cap_on_every_row(self):
        body = review_table(RULE)[2:]
        self.assertEqual(len(body), 4)
        for row in body:
            self.assertTrue(row.rsplit("|", 2)[1].strip(), f"no cap on row: {row}")


class OneStatementOfTheRule(unittest.TestCase):
    def test_exactly_one_file_states_the_planning_review_rule(self):
        rows = governed_grep(r"convergence boundary")
        mentions = sorted({row.split(":", 1)[0] for row in rows})
        statements = [f for f in mentions if states_the_rule(REPO_ROOT / f)]
        self.assertEqual(statements, [".claude/rules/sd-planning-adversarial-review.md"])

    def test_every_other_mention_links_to_that_one_file(self):
        rows = governed_grep(r"convergence boundary")
        mentions = sorted({row.split(":", 1)[0] for row in rows})
        for name in mentions:
            if name == ".claude/rules/sd-planning-adversarial-review.md":
                continue
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            self.assertTrue(
                "sd-planning-adversarial-review.md" in text
                or name == ".claude/sd-ai-command-pack/planning-adversarial-review.md",
                f"{name} mentions the boundary and does not point at the rule",
            )

    def test_the_contract_is_reached_by_link_and_not_restated(self):
        agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(".claude/rules/sd-planning-adversarial-review.md", agents)
        self.assertNotIn("concern ledger", agents)


class ConditionalObligations(unittest.TestCase):
    """Criterion 8: the ledger and the sweep are gated on a `sensitive` path."""

    SITES = (
        ".claude/sd-ai-command-pack/planning-adversarial-review.md",
    )

    def test_every_site_naming_the_ledger_names_the_gate(self):
        rows = governed_grep(r"concern ledger|cross-artifact")
        files = {row.split(":", 1)[0] for row in rows}
        self.assertEqual(files, set(self.SITES))
        for site in self.SITES:
            text = (REPO_ROOT / site).read_text(encoding="utf-8")
            self.assertIn("`sensitive`", text, f"{site} states the obligation unconditionally")


class DeletedLane(unittest.TestCase):
    def test_the_lane_page_is_gone(self):
        self.assertFalse((REPO_ROOT / "docs/planning-adversarial-review-codex.md").exists())

    def test_no_governed_file_names_the_lane(self):
        rows = governed_grep(r"second-model lane|codex (review )?lane")
        self.assertEqual(rows, [])

    def test_no_governed_file_references_the_deleted_page(self):
        rows = governed_grep(r"planning-adversarial-review-codex")
        self.assertEqual(rows, [])


class BareVendorTokens(unittest.TestCase):
    """A grep of `skills/` for a bare vendor token returns nothing."""

    def test_no_skill_names_a_vendor_as_a_bare_token(self):
        names = tracked_files("skills")
        # An empty walk finds no token and would pass on any tree.
        self.assertTrue(names, "the walk enumerated no file under skills/")
        rows = bare_vendor_tokens(names)
        self.assertEqual(rows, [], "bare vendor tokens under skills/:\n" + "\n".join(rows))

    def test_the_walk_reads_every_tracked_file_whatever_its_suffix(self):
        """The criterion names the tree, not a suffix: a token in a script or a
        JSON file under `skills/` counts as much as one in a page."""
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            subprocess.run(  # nosec B603 B607 - fixed argv, a scratch repository
                ["git", "init", "-q"], cwd=root, check=True)
            files = {
                "skills/a/SKILL.md": "Run the review.\n",
                "skills/a/run.sh": "#!/bin/sh\nexec codex review\n",
                "skills/a/paths.json": '{"reviewer": "anthropic"}\n',
            }
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text)
            subprocess.run(  # nosec B603 B607 - fixed argv, a scratch repository
                ["git", "add", "--", "skills"], cwd=root, check=True)
            names = tracked_files("skills", root)
            self.assertEqual(sorted(names), sorted(files))
            self.assertEqual(
                sorted(bare_vendor_tokens(names, root)),
                [
                    'skills/a/paths.json:1:{"reviewer": "anthropic"}',
                    "skills/a/run.sh:2:exec codex review",
                ],
            )

    def test_a_file_being_merged_is_enumerated_once(self):
        """An unmerged path must arrive once, not once per merge stage.

        Cosmetic, and stated as such: the sweep filters this list and compares
        lists, so no verdict moves. What moves is the report. Nothing about a
        clean checkout separates the two behaviours, so the conflict is built
        for real and the premises are asserted before the conclusion -- the
        index is genuinely unmerged, and plain `ls-files` does repeat the path
        there. Drop either and what follows would pass against any ordinary
        repository, which is how this survived.
        """

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)

            def git(*argv, check=True):
                return subprocess.run(  # nosec B603 B607 - fixed argv, scratch repo
                    ["git", "-c", "user.email=policy@example.invalid",
                     "-c", "user.name=workflow policy",
                     "-c", "commit.gpgsign=false", *argv],
                    cwd=root, capture_output=True, text=True, check=check)

            page = root / "skills" / "f.md"
            page.parent.mkdir(parents=True)
            git("init", "-q", "-b", "main", ".")
            page.write_text("base\n")
            git("add", "--", "skills")
            git("commit", "-qm", "base")
            git("checkout", "-q", "-b", "other")
            page.write_text("other\n")
            git("commit", "-qam", "other")
            git("checkout", "-q", "main")
            page.write_text("mine\n")
            git("commit", "-qam", "mine")
            git("merge", "other", check=False)

            stages = git("ls-files", "-u", "--", "skills").stdout
            self.assertEqual(
                [line.split("\t")[0].split()[-1] for line in stages.splitlines()],
                ["1", "2", "3"],
                "the fixture did not leave an unmerged index, so the case "
                "below proves nothing")

            # ls-files-form: plain -- the repetition is what this case asserts
            repeated = git("ls-files", "-z", "--", "skills").stdout
            self.assertEqual(
                [name for name in repeated.split("\0") if name],
                ["skills/f.md", "skills/f.md", "skills/f.md"],
                "this git no longer repeats an unmerged path; if that is now "
                "the default, say so here rather than deleting the case")

            self.assertEqual(
                tracked_files("skills", root), ["skills/f.md"],
                "a file being merged entered the walk once per merge stage")

    def test_the_shape_catches_a_vendor_named_as_who_runs_a_pass(self):
        """The guard against the guard: the three lines that came back, and
        the cases around them, match. A pattern that missed them would leave
        the test above passing on the regression."""
        for line in (
            "an authorized exact-head `--scope branch --provider claude` review",
            "variables declared in its registry `env` list. Codex also receives",
            "The `claude-json` reader runs Claude in safe and restricted modes",
            # One name to a line: a line naming two would still match with
            # either dropped from the pattern.
            "OpenAI bills separately",
            "Anthropic bills separately",
            "Claude Code hands the pass to codex",
            "two Codexes and a claudeish reviewer",
            "the Claude Codex entry",
        ):
            with self.subTest(line=line):
                self.assertEqual(bare_vendor_lines(line), [1])

    def test_a_hard_wrap_inside_the_product_name_is_still_the_product(self):
        """A reflow that breaks `Claude Code` across two lines adds no vendor.
        A token split the same way still counts, and on its own line."""
        self.assertEqual(bare_vendor_lines("the Claude\nCode headless guide"), [])
        self.assertEqual(bare_vendor_lines("- the Claude\n  Code headless guide"), [])
        self.assertEqual(bare_vendor_lines("the Claude\nCodex entry"), [1, 2])
        self.assertEqual(
            bare_vendor_lines("see the Claude\nCode guide, which hands the\npass to codex"),
            [3],
        )

    def test_every_separator_the_product_name_accepts_is_pinned(self):
        """One case per separator the comment above names, and one per
        separator it refuses. Each of the three widenings that reads as
        harmless -- `\\s+` for the break, `[ \\t]` for the run, dropping the
        `\\r?` -- is green against the cases before this one, so each is given
        the case that fails it.

        A run of spaces or tabs is the product, so `[ \\t]` alone is not the
        shape. CRLF is the product, so the `\\r?` is load-bearing: a page
        written on Windows, or checked out with git's `core.autocrlf` on,
        hard-wraps the same way and must not turn the gate red.
        """
        for text in ("the Claude  Code guide", "the Claude\tCode guide",
                     "the Claude \t Code guide", "the Claude\r\nCode entry",
                     "the Claude  \r\n  Code entry"):
            with self.subTest(exempt=text):
                self.assertEqual(bare_vendor_lines(text), [])

        # A blank line is two breaks. A reflow does not produce one inside a
        # name, so it stays a bare token rather than widening the exemption.
        self.assertEqual(bare_vendor_lines("Claude\n\nCode"), [1])

        # A form feed is the expensive one. `str.splitlines()` counts it as a
        # break; the blank reinstates only `\n`. A pattern that spanned it
        # would merge two rows into one and report `codex` on line 2 of a file
        # where a reader finds it on line 3.
        self.assertEqual(bare_vendor_lines("Claude\x0cCode\ncodex"), [1, 3])

    def test_the_shape_drops_identifiers_paths_and_the_product_name(self):
        for line in (
            "The `claude-json` reader and the `codex-json` entry",
            "`codex_preflight` scrubs `CODEX_HOME` and `OPENAI_API_KEY`",
            "settings live in ~/.claude/settings.json and .codex/",
            "see https://code.claude.com/docs/en/headless",
            "the `mcp__claude-in-chrome__navigate` tool",
            "[Claude Code's headless guide](https://code.claude.com/docs)",
        ):
            with self.subTest(line=line):
                self.assertEqual(bare_vendor_lines(line), [])


#: A page runs a review when it invokes a reviewer, in any of the shapes an
#: invocation takes on these pages: a backticked reviewer with a flag; a
#: bare backticked reviewer that a verb runs, "Run `sd-review`"; or a command
#: line of a code block that begins with the reviewer. The reviewers are
#: `sd-review` and `sd-research-kit review`, the front of
#: `bin/sd_research_review.py`, and the same three shapes apply to both: a
#: bare reviewer with no verb before it names the tool, its lane or its
#: report and is not an invocation. The reviewer's name ends at a backtick,
#: a space or the line: `sd-review-other` stands for another tool, and `\b` reads its
#: hyphen as the end of a word.
REVIEWER = r"(?:sd-review|sd-research-kit review)"
REVIEW_INVOCATION = re.compile(
    rf"`{REVIEWER} --[a-z]"
    rf"|\b(?:run|runs|running|invoke|invokes)\s+`{REVIEWER}`"
    rf"|^\s*(?:\$ )?{REVIEWER}(?:[ \t]|$)",
    re.IGNORECASE | re.MULTILINE,
)

#: A cap stated on a skill's own page, as a count of passes or a "cap of N".
#: Searched over the page with its whitespace collapsed, because the pages
#: hard-wrap and a literal split across a line break is still a literal.
CAP_LITERAL = re.compile(r"\b[0-9]+ passes?\b|cap of [0-9]")

#: The sentence of a collapsed page that links the rule file must say that
#: the cap is the one on that row of the table: a link beside a point that
#: only mentions a cap is a pointer, not the reads-from relationship the
#: criterion asks for.
CAP_WORD = re.compile(r"\bcaps?\b.*\bon (?:that|those) rows?\b", re.IGNORECASE)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def collapsed(path: pathlib.Path) -> str:
    """A page with its whitespace collapsed, so a hard wrap is one space."""
    return " ".join(path.read_text(encoding="utf-8").split())


def skills_that_run_a_review(root: pathlib.Path = REPO_ROOT) -> list[str]:
    """The `skills/*/SKILL.md` pages that invoke a reviewer, from the index.

    The rule is the invocation and not a mention: six pages name `sd-review`
    as the lane that holds the verdict, or as what runs `sd-check`, and run
    nothing. `REVIEW_INVOCATION` states the shapes. The reviewer's own page is
    in the set by name, so it stays there even if its last example is edited
    away.
    """
    names = [n for n in tracked_files("skills", root) if n.count("/") == 2 and n.endswith("/SKILL.md")]
    found = {n for n in names if REVIEW_INVOCATION.search((root / n).read_text(encoding="utf-8"))}
    return sorted(found | ({"skills/sd-review/SKILL.md"} & set(names)))


def table_points(path: pathlib.Path = RULE) -> list[str]:
    """The Point cell of every row of the review table, lower-cased."""
    return [row.split("|")[2].strip().lower() for row in review_table(path)[2:]]


class SkillsThatRunAReview(unittest.TestCase):
    """sd:10 criterion 5, the table-reads clause: every skill that runs a
    review names its point in the table and reads the cap from it.

    The set is enumerated by `skills_that_run_a_review`, whose docstring
    states the rule. A skill in the set links the one rule file that carries
    the table, names one of that table's Point cells, says in the sentence
    that links the rule that the cap comes from there, and states no cap of
    its own, so a cap changes in exactly one place. A page is compared with
    its whitespace collapsed, because the pages hard-wrap and a point name or
    a cap literal may span a line break.
    """

    def test_the_enumeration_finds_the_reviewer_and_the_skills_that_call_it(self):
        """A floor, not the set: the three pages that run a review today must
        stay in, so an invocation edited into a mention shape is a failure
        here and not a page that quietly stops being checked."""
        skills = skills_that_run_a_review()
        self.assertLessEqual({"skills/sd-research-repo/SKILL.md",
                              "skills/sd-review/SKILL.md", "skills/sd-ship/SKILL.md"},
                             set(skills), "a page that runs a review left the enumeration")

    def test_the_enumeration_takes_every_invocation_shape_and_no_mention(self):
        """Each shape `REVIEW_INVOCATION` names is one page here, and the
        mention shapes the six real pages use are two more that stay out."""
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            subprocess.run(  # nosec B603 B607 - fixed argv, a scratch repository
                ["git", "init", "-q"], cwd=root, check=True)
            files = {
                "skills/flag/SKILL.md": "Then `sd-review --scope branch` on the commits.\n",
                "skills/verb/SKILL.md": "Run `sd-review` and read its report.\n",
                "skills/block/SKILL.md": "```\nsd-review\n```\n",
                "skills/kit/SKILL.md": "Run the mechanical half:\n\n   sd-research-kit review\n",
                "skills/lane/SKILL.md": "The verdict belongs to the sd-review lane.\n",
                "skills/tool/SKILL.md": "`sd-review` runs it; `sd-review-other` marks the row.\n",
                "skills/ack/SKILL.md": "Run `sd-review-other` on the row.\n\n```\nsd-review-other --ack 3\n```\n",
                "skills/kitmention/SKILL.md":
                    "The `sd-research-kit review` report lists where the template moved.\n",
                "skills/sd-review/SKILL.md": "# sd-review\n",
            }
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text)
            subprocess.run(  # nosec B603 B607 - fixed argv, a scratch repository
                ["git", "add", "--", "skills"], cwd=root, check=True)
            self.assertEqual(skills_that_run_a_review(root), [
                "skills/block/SKILL.md", "skills/flag/SKILL.md", "skills/kit/SKILL.md",
                "skills/sd-review/SKILL.md", "skills/verb/SKILL.md"])

    def test_every_skill_that_runs_a_review_links_the_rule_and_names_its_point(self):
        points = table_points()
        self.assertTrue(points)
        for name in skills_that_run_a_review():
            with self.subTest(skill=name):
                text = collapsed(REPO_ROOT / name)
                self.assertIn(".claude/rules/sd-planning-adversarial-review.md", text,
                              f"{name} runs a review and does not link the rule file")
                self.assertTrue(any(point in text.lower() for point in points),
                                f"{name} names none of the table's points: {points}")
                linking = [s for s in SENTENCE_END.split(text)
                           if ".claude/rules/sd-planning-adversarial-review.md" in s]
                self.assertTrue(any(CAP_WORD.search(s) for s in linking),
                                f"{name} links the rule and no linking sentence says the cap "
                                f"comes from it: {linking}")

    def test_the_linking_sentence_must_say_the_cap_is_the_one_on_the_row(self):
        """The reads-from relationship, not the word. `CAP_WORD` used to
        accept any sentence carrying `cap` beside the link, so a pointer --
        "see the cap in the rule file" -- passed as if the page read its cap
        from the table. The four pages say the cap is the one on that row."""
        for sentence in (
            "The review table in `.claude/rules/sd-planning-adversarial-review.md` "
            "gives the cap on that row.",
            "Their caps are the ones on those rows in the checkout's "
            "`.claude/rules/sd-planning-adversarial-review.md`, and this tool states neither.",
        ):
            self.assertIsNotNone(CAP_WORD.search(sentence), sentence)
        for sentence in (
            "See the cap in `.claude/rules/sd-planning-adversarial-review.md`.",
            "The cap is capped by `.claude/rules/sd-planning-adversarial-review.md`.",
            "Read `.claude/rules/sd-planning-adversarial-review.md` before the pass.",
        ):
            self.assertIsNone(CAP_WORD.search(sentence), sentence)

    def test_no_skill_that_runs_a_review_carries_a_cap_of_its_own(self):
        for name in skills_that_run_a_review():
            with self.subTest(skill=name):
                text = collapsed(REPO_ROOT / name)
                rows = [f"{name}: …{text[max(0, m.start() - 30):m.end() + 30]}…"
                        for m in CAP_LITERAL.finditer(text)]
                self.assertEqual(rows, [], "a cap stated outside the table:\n" + "\n".join(rows))


class StandingAuthorizationInventory(unittest.TestCase):
    def test_core_settings_are_documented_without_shipping_a_personal_grant(self):
        for relative in ("WORKFLOW.md", "README.md"):
            text = (REPO_ROOT / relative).read_text()
            for key in sd_lib.CORE_CONFIG:
                with self.subTest(path=relative, key=key):
                    self.assertIn(f"sd.{key}", text)
        # AGENTS.md points at the one inventory rather than copying it.
        self.assertIn("WORKFLOW.md` § Standing authorization", (REPO_ROOT / "AGENTS.md").read_text())
        ship = (REPO_ROOT / "skills/sd-ship/SKILL.md").read_text()
        review = (REPO_ROOT / "skills/sd-review/SKILL.md").read_text()
        self.assertIn("explicitly says wait", ship)
        self.assertNotIn("sd config get sd.assistant_merge", ship)  # sd:3014: the key is retired
        self.assertIn("sd.external_reviews", review)
        self.assertNotIn("- No merge. The loop stops", WORKFLOW.read_text())
        self.assertNotIn("external_reviews", sd_install.DEFAULT_BLOCK_BODY)
        self.assertNotIn("assistant_merge", sd_install.DEFAULT_BLOCK_BODY)


if __name__ == "__main__":
    unittest.main()
