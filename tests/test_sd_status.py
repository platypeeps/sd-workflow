"""Fixtures for bin/sd-status: the whole report, and the gaps it must name.

Two levels, deliberately. The subprocess tests run the executable the way an
operator does, so an exit code under test is an exit code a caller sees. The
in-process tests reach for `workflow_checks` and `_protection_gaps` directly,
because "which enforcement legs are missing" is a pure function of two objects
and deserves to be tested as one rather than through six fake HTTP responses.

The fake `gh`, the fixture repository and the read-only digest come from
`tests.test_sd_pr_state`; there is one copy of them and this module imports it.
"""

from __future__ import annotations

import ast
import datetime
import hashlib
import importlib.machinery
import importlib.util
import io
import itertools
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.row_seed import seed_declaration
from tests.test_sd_pr_state import (
    BIN,
    REPO_SETTINGS,
    SD_STATUS,
    ToolFixture,
    tree_digest,
)

#: The other half of the pin in `SkillSurfaceTests`. Imported as the function
#: alone so this module collects its own tests and not that file's.
from tests.test_skill_frontmatter import surfaces as frontmatter_surfaces

SD_HANDOFF = BIN / "sd-handoff"

#: The skill page whose claims about this tool's output are measured against a
#: real run below (sd:804). Nothing else gates its prose.
SKILL_MD = BIN.parent / "skills" / "sd-status" / "SKILL.md"

#: The number words the skill page spells its limits in.
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _skill_prose() -> str:
    """SKILL.md as one line, so a line break cannot split a claim from its number."""
    return " ".join(SKILL_MD.read_text(encoding="utf-8").split())


def _skill_says(*sentences: str) -> str:
    """The page's prose, once each of `sentences` is in it verbatim, once, at a sentence start.

    A rule stated in prose has no slot to extract. "every row of the class,
    not just the rows past the cap" and "only the rows past the cap, never
    the rest of the class" share every word the earlier regexes read, and
    #946's review swapped one for the other with every gate green (sd:821
    N-1). So a sentence that states a rule is pinned whole, and the test that
    pins it measures the thing the sentence says against a run: the pin
    proves the page says it, the run proves it is so. Rewording the page is
    not free, then. A change that keeps the meaning updates the copy here
    after re-reading the assertions under it; a change that does not keeps
    failing, which is the point.
    """
    prose, starts = _skill_prose(), _skill_starts()
    for sentence in sentences:
        pinned = " ".join(sentence.split())
        found = [match.start() for match in re.finditer(re.escape(pinned), prose)]
        assert len(found) == 1, \
            f"{SKILL_MD.name} says this {len(found)} times, not once: {sentence!r}"
        # Bound at a sentence or list-item start, so a "never" put in front of
        # a pinned rule does not count as the rule (sd:1002, 11b348a64abb).
        assert found[0] in starts, \
            f"{SKILL_MD.name} says this, but not at a sentence start: {sentence!r}"
    return prose


#: A list item's marker: "- ", "* " or "1. " at the start of a line.
_ITEM = re.compile(r"(?:[-*]|\d+\.)$")
#: What may close a sentence after its stop: emphasis, code, quotes, brackets.
_SENTENCE_END = re.compile(r"[.!?][*_`\"')\]]*$")


def _skill_starts() -> set[int]:
    """Where in `_skill_prose()` a sentence or a list item begins.

    Read from the page's lines, since the one-line prose has none: the first
    word of a paragraph, a heading, a table row or a list item (its marker and
    the word after it), and every word after a sentence's stop.
    """
    starts, offset, previous, block = set(), 0, None, True
    for line in SKILL_MD.read_text(encoding="utf-8").splitlines():
        words = line.split()
        if not words:
            block = True
            continue
        if words[0].startswith(("#", "|")) or _ITEM.match(words[0]):
            block = True
        for index, word in enumerate(words):
            item_text = index == 1 and _ITEM.match(words[0])
            after_stop = previous is not None and _SENTENCE_END.search(previous)
            if (index == 0 and block) or item_text or after_stop:
                starts.add(offset)
            offset += len(word) + 1
            previous = word
        block = words[0].startswith("#")
    return starts


def _number(word: str) -> int | None:
    return int(word) if word.isdigit() else _NUMBER_WORDS.get(word.lower())


def _word(number: int) -> str:
    """The number word the page spells `number` with, so a pin reads the constant.

    A number `_NUMBER_WORDS` does not spell fails here by name (sd:836 N-3).
    It used to raise `KeyError` out of whichever test was mid-sentence, so
    raising `PENDING_LIMIT` past ten made those tests error rather than say
    what to do, and the reading was that the page had not been re-read. The
    table above is the thing to extend, and the message says so.
    """
    spelled = {value: word for word, value in _NUMBER_WORDS.items()}
    if number not in spelled:
        raise AssertionError(
            f"_NUMBER_WORDS spells one to {max(spelled)}, not {number}: add the word "
            f"there before a test can look for how the page spells {number}")
    return spelled[number]


def _load(name: str, module_name: str) -> Any:
    path = BIN / name
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_file_location(module_name, str(path), loader=loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


status = _load("sd-status", "sd_status_under_test")

WORKFLOW = """\
name: Tests
on:
  pull_request:
  push:
    branches: [main]
jobs:
  unittest:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        include:
          - os: ubuntu-latest
            python-version: "3.10"
          - os: macos-latest
            python-version: "3.13"
    steps:
      - run: make test
  shell-coverage:
    name: Shell coverage
    runs-on: ubuntu-latest
    steps:
      - run: make shell
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: make lint
"""

PRD = """\
---
title: {title}
status: {status}
created: 2026-08-01
{extra}---

# {title}
"""


class SkillSaysTests(unittest.TestCase):
    """sd:1002, 11b348a64abb: a pin counts only where a sentence or list item starts."""

    def says(self, page: str, sentence: str) -> str:
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "SKILL.md"
            path.write_text(page, encoding="utf-8")
            with mock.patch.object(sys.modules[__name__], "SKILL_MD", path):
                return _skill_says(sentence)

    def test_a_negation_in_front_of_a_pinned_rule_fails_the_pin(self) -> None:
        rule = "It is capped at ten and says so."
        for page in ("# Rules\n\nNobody believes that\nIt is capped at ten and says so.\n",
                     "- **Never read it whole.** Never say It is capped at ten and says so.\n"):
            with self.subTest(page=page), \
                    self.assertRaisesRegex(AssertionError, "not at a sentence start"):
                self.says(page, rule)

    def test_a_rule_at_a_sentence_or_item_start_passes(self) -> None:
        rule = "It is capped at ten and says so."
        for page in ("# Rules\n\nIt is capped at ten and says so.\n",
                     "# Rules\nIt is capped at ten\nand says so.\n",
                     "- **Never read it whole.** It is capped at ten and says so.\n",
                     "Read the list. It is capped at ten and says so.\n",
                     "- It is capped at ten and says so.\n"):
            with self.subTest(page=page):
                self.assertIn(rule, self.says(page, rule))
        self.assertIn("- **Rank 3**, below the rest.",
                      self.says("- **Rank 3**, below the rest.\n", "- **Rank 3**, below the rest."))


class WorkflowCheckNameTests(unittest.TestCase):
    """The names GitHub will report, derived from the file rather than guessed."""

    def setUp(self) -> None:
        self._fixture = ToolFixture("run")
        self._fixture.setUp()
        self.addCleanup(self._fixture.doCleanups)
        self.repo = self._fixture.repo
        self.workflows = self.repo / ".github" / "workflows"
        self.workflows.mkdir(parents=True)

    def write(self, name: str, body: str) -> None:
        (self.workflows / name).write_text(body, encoding="utf-8")

    def test_matrix_include_names_match_githubs_spelling(self) -> None:
        self.write("tests.yml", WORKFLOW)
        produced, notes = status.workflow_checks(self.repo)
        self.assertEqual(
            produced,
            {
                "unittest (ubuntu-latest, 3.10)",
                "unittest (macos-latest, 3.13)",
                "Shell coverage",
                "lint",
            },
        )
        self.assertEqual(notes, [])

    def test_plain_axes_cross_multiply_in_declaration_order(self) -> None:
        self.write(
            "axes.yml",
            "on: [pull_request]\n"
            "jobs:\n"
            "  build:\n"
            "    strategy:\n"
            "      matrix:\n"
            "        os: [linux, mac]\n"
            "        node: [20, 22]\n"
            "    steps:\n"
            "      - run: true\n",
        )
        produced, _ = status.workflow_checks(self.repo)
        self.assertEqual(
            produced,
            {
                "build (linux, 20)",
                "build (linux, 22)",
                "build (mac, 20)",
                "build (mac, 22)",
            },
        )

    def test_a_workflow_without_a_pull_request_trigger_produces_nothing(self) -> None:
        self.write(
            "nightly.yml",
            "on:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n  sweep:\n"
            "    steps:\n      - run: true\n",
        )
        produced, _ = status.workflow_checks(self.repo)
        self.assertEqual(produced, set())

    def test_a_reusable_workflow_call_is_a_note_not_a_guess(self) -> None:
        self.write(
            "call.yml",
            "on: [pull_request]\njobs:\n  shared:\n    uses: acme/ci/.github/workflows/x.yml@v1\n",
        )
        produced, notes = status.workflow_checks(self.repo)
        self.assertEqual(produced, set())
        self.assertTrue(any("reusable workflow" in note for note in notes))

    def test_an_unresolvable_expression_is_a_note_not_a_guess(self) -> None:
        self.write(
            "dyn.yml",
            "on: [pull_request]\njobs:\n  build:\n"
            "    name: build-${{ github.event_name }}\n    steps:\n      - run: true\n",
        )
        produced, notes = status.workflow_checks(self.repo)
        self.assertEqual(produced, set())
        self.assertTrue(any("expression" in note for note in notes))

    def test_a_name_reading_only_the_matrix_resolves_per_combination(self) -> None:
        """sd:1420: platypeeps/system's `system-native.yml`, as it stands.

        GitHub reports `system-native (shared)` and three more for this job,
        and appends no suffix of its own to a name that interpolates the
        matrix. Before the fix all four required contexts read
        `required_not_produced` beside an expression note.
        """
        self.write(
            "system-native.yml",
            "name: System native tests\n"
            "on:\n  pull_request:\n  push:\n    branches: [main]\n"
            "jobs:\n  system-native:\n"
            "    name: system-native (${{ matrix.leg }})\n"
            "    runs-on: macos-15\n"
            "    strategy:\n      fail-fast: false\n      matrix:\n"
            "        leg: [shared, dashboard, runner, tools]\n"
            "    steps:\n      - run: true\n",
        )
        produced, notes = status.workflow_checks(self.repo)
        self.assertEqual(
            produced,
            {"system-native (shared)", "system-native (dashboard)",
             "system-native (runner)", "system-native (tools)"},
        )
        self.assertEqual(notes, [])

    def test_include_and_exclude_follow_githubs_documented_merge(self) -> None:
        """Exclude first, then include: an entry joins every original
        combination whose axis values it does not overwrite -- a value an
        earlier include added may be -- else it becomes its own."""
        self.write(
            "merge.yml",
            "on: [pull_request]\njobs:\n  build:\n"
            "    name: ${{ matrix.os }}/${{ matrix.py }}-${{ matrix.tag }}\n"
            "    strategy:\n      matrix:\n"
            "        os:\n          - linux\n          - mac\n"
            "        py: ['3.10', 3.13]\n"
            "        exclude:\n          - os: mac\n            py: '3.10'\n"
            "        include:\n          - tag: plain\n"
            "          - os: mac\n            tag: arm\n"
            "          - os: linux\n            py: '3.11'\n            tag: extra\n"
            "          - os: windows\n            py: '3.12'\n            tag: win\n"
            "    steps:\n      - run: true\n",
        )
        produced, notes = status.workflow_checks(self.repo)
        self.assertEqual(
            produced,
            {"linux/3.10-plain", "linux/3.13-plain", "mac/3.13-arm",
             "linux/3.11-extra", "windows/3.12-win"},
        )
        self.assertEqual(notes, [])

    def test_a_matrix_name_this_reader_cannot_resolve_keeps_its_note(self) -> None:
        """Anything not a static literal matrix stays the runner's to name."""
        cases = {
            "another context": ("x (${{ matrix.os }}, ${{ github.ref }})", "        os: [a, b]\n"),
            "a computed matrix": ("x (${{ matrix.os }})", "        os: ${{ fromJSON(inputs.os) }}\n"),
            "a retyped value": ("x (${{ matrix.py }})", "        py: [3.10, 3.12]\n"),
            "a boolean": ("x (${{ matrix.ok }})", "        ok: [true, false]\n"),
            "a key one combination lacks": (
                "x (${{ matrix.tag }})",
                "        os: [a]\n        include:\n          - other: b\n",
            ),
        }
        for index, (label, (name, axes)) in enumerate(cases.items()):
            with self.subTest(label):
                self.write(
                    f"unresolved{index}.yml",
                    "on: [pull_request]\njobs:\n  build:\n"
                    f"    name: {name}\n    strategy:\n      matrix:\n{axes}"
                    "    steps:\n      - run: true\n",
                )
                produced, notes = status.workflow_checks(self.repo)
                self.assertEqual(produced, set())
                self.assertTrue(any("expression" in note for note in notes))
                (self.workflows / f"unresolved{index}.yml").unlink()

    def test_a_conditional_job_is_reported_because_a_skip_pends_forever(self) -> None:
        self.write(
            "cond.yml",
            "on: [pull_request]\njobs:\n  build:\n"
            "    if: github.actor != 'bot'\n    steps:\n      - run: true\n",
        )
        produced, notes = status.workflow_checks(self.repo)
        self.assertEqual(produced, {"build"})
        self.assertTrue(any("conditional" in note for note in notes))

    def test_an_aggregate_gate_written_to_always_report_is_not_flagged(self) -> None:
        """The idiom the note used to accuse of the opposite of what it does.

        `if: ${{ !cancelled() }}` is how a required aggregate context is meant
        to be written: it runs the job when the jobs it `needs` were skipped or
        failed, which is precisely the case the note warned would leave the
        context pending forever. Flagging it argued for deleting a working
        branch protection, so the expression is read now rather than merely
        noticed.
        """
        self.write(
            "ci.yml",
            "on: [pull_request]\njobs:\n  result:\n    name: CI Result\n"
            "    needs: [lint]\n    if: ${{ !cancelled() }}\n    steps:\n      - run: true\n",
        )
        produced, notes = status.workflow_checks(self.repo)
        self.assertEqual(produced, {"CI Result"})
        self.assertEqual([note for note in notes if "conditional" in note], [])

    def test_always_reports_too_and_is_recognised_however_it_is_spelled(self) -> None:
        """`always()` is the same guarantee, and spelling must not decide it.

        GitHub accepts a bare expression as well as a `${{ }}` one, treats the
        function name case-insensitively, and lets the author quote the value
        or space the `!` out from `cancelled()`. A reader that recognised one
        spelling and flagged the next would be the same defect with a smaller
        blast radius, so each form a real workflow uses is covered here.
        """
        for index, condition in enumerate(
            (
                "always()",
                "${{ always() }}",
                "${{ ALWAYS() }}",
                "'${{ !cancelled() }}'",
                "${{ ! cancelled() }}",
                "${{ !cancelled() }} # the aggregate gate",
            )
        ):
            with self.subTest(condition=condition):
                self.write(
                    f"gate{index}.yml",
                    "on: [pull_request]\njobs:\n  result:\n"
                    f"    if: {condition}\n    steps:\n      - run: true\n",
                )
                _, notes = status.workflow_checks(self.repo)
                self.assertEqual([note for note in notes if "conditional" in note], [])
                (self.workflows / f"gate{index}.yml").unlink()

    def test_a_condition_that_can_still_skip_the_job_is_still_flagged(self) -> None:
        """The other half: reading the expression must not amount to trusting it.

        Every one of these can leave the job skipped, and two of them mention
        the reporting functions -- `always() && ...` is skippable on the right
        operand, and a job that runs only when nothing was cancelled *on main*
        reports nothing on a pull request. A note that cleared them because the
        word appeared would be worse than the note it replaced.
        """
        for index, condition in enumerate(
            (
                "github.actor != 'bot'",
                "${{ always() && github.ref == 'refs/heads/main' }}",
                "${{ !cancelled() && github.event_name == 'push' }}",
                "${{ cancelled() }}",
                "${{ success() }}",
            )
        ):
            with self.subTest(condition=condition):
                self.write(
                    f"skip{index}.yml",
                    "on: [pull_request]\njobs:\n  build:\n"
                    f"    if: {condition}\n    steps:\n      - run: true\n",
                )
                _, notes = status.workflow_checks(self.repo)
                self.assertTrue(any("conditional" in note for note in notes))
                (self.workflows / f"skip{index}.yml").unlink()

    def test_no_workflows_directory_is_stated_rather_than_silent(self) -> None:
        empty = self.repo / "empty"
        empty.mkdir()
        produced, notes = status.workflow_checks(empty)
        self.assertEqual(produced, set())
        self.assertTrue(any("no .github/workflows" in note for note in notes))


class ProtectionGapTests(unittest.TestCase):
    """Enforcement state, leg by leg. Presence is not enforcement."""

    def gaps(self, protection: dict[str, Any], produced: set[str]) -> list[str]:
        found, _ = status._protection_gaps(protection, "main", produced, [])
        return [gap["id"] for gap in found]

    def enforcing(self, **overrides: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "enforce_admins": {"enabled": True},
            "required_status_checks": {"strict": True, "contexts": ["lint"]},
            "required_pull_request_reviews": {"required_approving_review_count": 1},
        }
        record.update(overrides)
        return record

    def test_a_fully_enforcing_branch_has_no_gaps(self) -> None:
        self.assertEqual(self.gaps(self.enforcing(), {"lint"}), [])

    def test_admin_exemption_is_a_gap(self) -> None:
        found = self.gaps(self.enforcing(enforce_admins={"enabled": False}), {"lint"})
        self.assertEqual(found, ["enforce_admins"])

    def test_a_bypass_that_does_not_reach_administrators_is_its_own_gap(self) -> None:
        """Codex on #1140: every bypass actor was folded into `enforce_admins`,
        so a ruleset one app could walk past printed "exempts the admins who
        do the merging" on a repository whose administrators are subject to
        every rule. It is the `bypass` gap, naming the ruleset and the actor,
        and `enforce_admins` stays what `synthesize` said."""
        rulesets = [{"id": 42, "name": "release", "enforcement": "active",
                     "bypass_actors": [{"actor_id": 77, "actor_type": "Integration", "bypass_mode": "pull_request"}, {"actor_id": 9, "actor_type": "Team", "bypass_mode": "always"}]}]
        found, detail = status._protection_gaps(
            self.enforcing(source="ruleset", rulesets=rulesets), "main", {"lint"}, [])
        self.assertEqual([gap["id"] for gap in found], ["bypass"])
        self.assertIn("release (#42): Integration 77 (pull_request); release (#42): Team 9 (always)", found[0]["gap"])
        self.assertIn("administrators stay subject to release (#42)", found[0]["gap"])
        self.assertEqual(detail["admin_bypass"], [])
        self.assertNotIn("exempts the admins", found[0]["gap"])
        self.assertTrue(detail["enforce_admins"])
        self.assertEqual(detail["bypass"],
                         ["release (#42): Integration 77 (pull_request)", "release (#42): Team 9 (always)"])
        # Classic protection has no ruleset to bypass: the fact is empty, not absent.
        self.assertEqual(status._protection_gaps(self.enforcing(), "main", {"lint"}, [])[1]["bypass"], [])

    def test_non_strict_checks_are_a_gap(self) -> None:
        protection = self.enforcing(
            required_status_checks={"strict": False, "contexts": ["lint"]}
        )
        self.assertEqual(self.gaps(protection, {"lint"}), ["strict"])

    def test_no_required_checks_at_all_is_a_gap(self) -> None:
        protection = self.enforcing(required_status_checks={})
        self.assertIn("required_checks", self.gaps(protection, set()))

    def test_a_required_context_no_workflow_produces_is_a_gap(self) -> None:
        protection = self.enforcing(
            required_status_checks={"strict": True, "contexts": ["lint", "ghost"]}
        )
        found, detail = status._protection_gaps(protection, "main", {"lint"}, [])
        self.assertEqual([gap["id"] for gap in found], ["required_not_produced"])
        self.assertEqual(detail["required_not_produced"], ["ghost"])

    def test_a_check_the_repo_runs_but_does_not_require_is_a_gap(self) -> None:
        found, detail = status._protection_gaps(
            self.enforcing(), "main", {"lint", "security"}, []
        )
        self.assertEqual([gap["id"] for gap in found], ["produced_not_required"])
        self.assertEqual(detail["produced_not_required"], ["security"])

    def test_a_missing_review_requirement_is_a_gap(self) -> None:
        protection = self.enforcing()
        del protection["required_pull_request_reviews"]
        self.assertEqual(self.gaps(protection, {"lint"}), ["reviews"])

    def test_zero_required_approvals_is_a_gap(self) -> None:
        protection = self.enforcing(
            required_pull_request_reviews={"required_approving_review_count": 0}
        )
        found, _ = status._protection_gaps(protection, "main", {"lint"}, [])
        self.assertEqual([gap["id"] for gap in found], ["reviews"])
        # Verbatim, not a substring. `implement.md` quotes this string as a
        # transcript of current behaviour, and a substring assertion let the
        # message be reworded with the quote left stale.
        self.assertEqual(
            found[0]["gap"],
            "a pull request is required but no approving review is: 0 approvals, so one with green CI self-merges",
        )
        # The distinction the old wording lost. Both branches report `reviews`,
        # so an assertion on the id alone passes whichever text is emitted --
        # and the two describe opposite states of the branch.
        self.assertNotIn("no pull-request review is required", found[0]["gap"])

    def test_a_missing_review_object_is_the_worse_gap_not_the_same_one(self) -> None:
        """Deleting the requirement does not leave behaviour identical.

        The `required_pull_request_reviews` object is what requires a pull
        request at all. Removing it to silence the 0-approvals row trips this
        branch instead, on a branch anybody can now push to directly. Written
        because the plan said the two were equivalent and acted on it.
        """
        protection = self.enforcing()
        # Absent, not empty: this is the shape the API returns for a branch
        # whose protection carries no review requirement at all.
        del protection["required_pull_request_reviews"]
        found, _ = status._protection_gaps(protection, "main", {"lint"}, [])
        self.assertEqual([gap["id"] for gap in found], ["reviews"])
        self.assertIn("no pull-request review is required on main", found[0]["gap"])


class SoleOperatorReviewTests(unittest.TestCase):
    """sd:3497: one maintainer cannot approve their own pull request; the runner merges."""

    def test_under_the_local_gate_the_reviews_gap_says_gate(self) -> None:
        protection = ProtectionGapTests.enforcing(self, required_pull_request_reviews={"required_approving_review_count": 0})
        found, _ = status._protection_gaps(protection, "main", {"lint"}, [], local=True)
        self.assertEqual(found[0]["gap"], "a pull request is required but no approving review is: "
                                          "0 approvals, so one with a green gate self-merges")

    def test_the_merge_settings_carry_no_lane_b_vocabulary(self) -> None:
        text = json.dumps(status._merge_settings({}))
        self.assertNotIn("carrier branch", text)
        self.assertIn("a builder's `wip:` subjects", text)


class GapVocabularyTests(unittest.TestCase):
    """The pack owns the gap vocabulary, and the system's collector must agree (sd:1372).

    `sd_db.protection` writes the fleet's gap ids and the dashboard reads them;
    `sd-status` emits them here and `ACKNOWLEDGEABLE_GAPS` accepts them. Both
    sides were checked alone, and nothing held them together: an id added on
    one side only failed nothing. This reads the library `make setup`
    installs from `lib/`, and names each id on one side only. `unprotected` is the system's status column, not a gap cell.
    """

    def test_the_systems_gap_ids_are_the_packs(self) -> None:
        import sd_db.protection as system  # noqa: PLC0415 - provisioned by `make setup`

        pack = set(status.ACKNOWLEDGEABLE_GAPS) - {"unprotected"}
        self.assertEqual(
            (sorted(pack - set(system.GAP_IDS)), sorted(set(system.GAP_IDS) - pack)), ([], []),
            "(pack only, system only) gap ids",
        )

    def test_the_dashboard_draws_a_column_for_each_gap_id(self) -> None:
        """sd:6072. `GAPS` in the dashboard's protection screen, read with `ast` so nothing is imported."""
        screen = BIN.parent / "dashboard" / "sd_dashboard" / "protection_screen.py"
        columns = next(
            ast.literal_eval(node.value)
            for node in ast.parse(screen.read_text(encoding="utf-8"), filename=str(screen)).body
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "GAPS" for t in node.targets)
        )
        pack = set(status.ACKNOWLEDGEABLE_GAPS) - {"unprotected"}
        ours = {gap for gap, _ in columns}
        self.assertEqual(
            (sorted(pack - ours), sorted(ours - pack)), ([], []),
            "(pack only, dashboard only) gap ids: ACKNOWLEDGEABLE_GAPS owns them",
        )

    def test_the_systems_merge_flags_are_the_ones_sd_status_reports(self) -> None:
        import sd_db.protection as system  # noqa: PLC0415 - provisioned by `make setup`

        pack = {flag["id"] for flag in status._merge_settings({})}
        self.assertEqual(
            (sorted(pack - set(system.MERGE_FLAG_IDS)), sorted(set(system.MERGE_FLAG_IDS) - pack)), ([], []),
            "(pack only, system only) merge flag ids",
        )

    def test_the_default_baseline_owners_are_the_systems(self) -> None:
        """sd:1807. With no `sd.fleet_owners`, sd-status flags the repositories the dashboard flags."""
        import sd_db.protection as system  # noqa: PLC0415 - provisioned by `make setup`
        import sd_fleet  # noqa: PLC0415

        with mock.patch.dict(os.environ):
            os.environ.pop("SD_BASELINE_OWNERS", None)
            default = importlib.reload(system).BASELINE_OWNERS
        importlib.reload(system)
        self.assertEqual(frozenset(sd_fleet.DEFAULT_OWNERS), default)
        # The row is in the workflow database under `HOME` (sd:3989): an empty home has none.
        with (tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}),
              mock.patch.object(status.sd_lib, "machine_config", lambda path: {})):
            self.assertEqual(status._baseline_owners(), (sd_fleet.DEFAULT_OWNERS, ""))

    def test_the_baseline_flags_are_the_systems_ids_and_sentences(self) -> None:
        """sd:1807. Same ids, values and sentences as `sd_db.protection.baseline_flags`."""
        import sd_db.protection as system  # noqa: PLC0415 - provisioned by `make setup`

        self.assertEqual(status.BASELINE_FLAG_IDS, system.BASELINE_FLAG_IDS)
        ruleset = {"source": "ruleset", "required_status_checks": {"contexts": ["ci", "body-lint"]}}
        classic = {"required_status_checks": {"contexts": ["CI Result"]}}
        for protection, classic_present, ci in [(None, False, None), (classic, True, None), (ruleset, False, None),
                                                (ruleset, True, None), (ruleset, False, "local"),
                                                ({"source": "combined"}, True, "local")]:
            with self.subTest(protection=protection, classic_present=classic_present, ci=ci):
                self.assertEqual(status._baseline_flags(protection, True, classic_present, ci),
                                 system.baseline_flags(protection, "platypeeps", classic_present, ci))


class AcknowledgementTests(unittest.TestCase):
    """The repo row's `status` declaration (sd:5109): what it accepts, and when it stops accepting.

    The one property worth more than the rest: an acknowledgement is keyed on
    the *observed protection state*, never on the gap id. `reviews` is emitted
    for two opposite states of the branch, so an entry keyed on the id would
    accept "nothing requires a pull request" while meaning "a pull request is
    required and asks for no approvals". Half of this class exists to hold
    that line.
    """

    ZERO_APPROVALS = {
        "id": "reviews",
        "state": {
            "required_pull_request_reviews": True,
            "required_approving_review_count": 0,
            "enforce_admins": True,
        },
        "because": "one maintainer, enforce_admins on",
        "since": "2026-09-01",
        "until": "a second account with merge rights exists",
    }

    def enforcing(self, **overrides: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "enforce_admins": {"enabled": True},
            "required_status_checks": {"strict": True, "contexts": ["lint"]},
            "required_pull_request_reviews": {"required_approving_review_count": 0},
        }
        record.update(overrides)
        return record

    def split(
        self, protection: dict[str, Any], entries: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        gaps, _ = status._protection_gaps(protection, "main", {"lint"}, [])
        return status._apply_acknowledgements(
            gaps, status._observed_state(protection), entries
        )

    def written(self, body: str) -> tuple[list[dict[str, Any]], list[str]]:
        """`body` as a declaration's value, through the parser both readers share."""
        return status.sd_lib.parse_acknowledgements(json.loads(body))

    def test_an_enforce_admins_off_acknowledgement_still_accepts_a_known_off(self) -> None:
        """sd:2755's other half: keeping unknown apart from `False` must not
        stop an entry for `enforce_admins` off accepting the known state."""
        entry = {"id": "enforce_admins", "state": {"enforce_admins": False},
                 "because": "admins ship the release bump", "since": "2026-10-05", "until": "never"}
        still_open, accepted = self.split(self.enforcing(enforce_admins={"enabled": False}), [entry])
        self.assertEqual([gap["id"] for gap in accepted], ["enforce_admins"])
        self.assertEqual([gap["id"] for gap in still_open], ["reviews"])

    def test_an_object_without_enforce_admins_keeps_its_unknown_gap(self) -> None:
        """`_admin_gaps` reports an object without `enforce_admins` as
        unknown, so `observed_state` reads it unknown too, and an entry for
        off does not accept it (sd:2755)."""
        entry = {"id": "enforce_admins", "state": {"enforce_admins": False},
                 "because": "admins ship the release bump", "since": "2026-10-05", "until": "never"}
        protection = self.enforcing()
        del protection["enforce_admins"]
        still_open, accepted = self.split(protection, [entry])
        self.assertEqual(accepted, [])
        self.assertEqual([gap["id"] for gap in still_open], ["enforce_admins", "reviews"])

    def test_a_pinned_fact_cannot_be_null(self) -> None:
        """`enforce_admins` observes `None` when it is unknown; a `null` pin
        would accept exactly that, on any gap's entry (sd:2755)."""
        entry = dict(self.ZERO_APPROVALS, state={"required_pull_request_reviews": True, "enforce_admins": None})
        entries, problems = self.written(json.dumps({"accepted_gaps": [entry]}))
        self.assertEqual(entries, [])
        self.assertIn("accepted_gaps[0].state.enforce_admins is null", problems[0])

    def test_absent_protection_is_a_distinct_observed_state_from_empty_protection(self) -> None:
        """The fact that separates "no object" from "an object enforcing nothing".

        Both reduce every other fact to the same falsy value, so without this
        one the two branch states are indistinguishable to an acknowledgement
        -- and they are not the same branch: one can be pushed to freely, the
        other has a protection object someone can tighten.
        """
        self.assertFalse(status._observed_state(None)["branch_protection"])
        self.assertTrue(status._observed_state({})["branch_protection"])
        absent = status._observed_state(None)
        empty = status._observed_state({})
        self.assertNotEqual(absent, empty)
        # ... and in `enforce_admins`: no object is off, while an object
        # without the answer is unknown, the gap `_admin_gaps` reports for it
        # (sd:2755). The other facts genuinely are constants on both.
        differing = [key for key in absent if absent[key] != empty[key]]
        self.assertEqual(differing, ["branch_protection", "enforce_admins"])
        self.assertIs(absent["enforce_admins"], False)
        self.assertIsNone(empty["enforce_admins"])

    def test_a_matching_acknowledgement_moves_the_finding_out_of_the_gaps(self) -> None:
        still_open, accepted = self.split(self.enforcing(), [self.ZERO_APPROVALS])
        self.assertEqual(still_open, [])
        self.assertEqual([entry["id"] for entry in accepted], ["reviews"])
        # The finding text travels with the acceptance rather than being
        # dropped: what was accepted has to stay readable.
        self.assertIn("0 approvals", accepted[0]["gap"])

    def test_an_acknowledgement_stops_applying_when_the_state_drifts(self) -> None:
        """The whole point. Accepting a state is not accepting an id forever.

        `enforce_admins` going off is half of what makes 0 approvals
        survivable here, so an entry that pinned it must not go on accepting
        the branch once it is gone.
        """
        protection = self.enforcing(enforce_admins={"enabled": False})
        still_open, accepted = self.split(protection, [self.ZERO_APPROVALS])
        self.assertEqual(accepted, [])
        ids = [gap["id"] for gap in still_open]
        self.assertIn("reviews", ids)
        stale = [gap for gap in still_open if gap["id"] == "reviews"][0]
        self.assertIn("no longer matches", stale["acknowledgement_stale"])
        self.assertIn("enforce_admins acknowledged True, observed False", stale["acknowledgement_stale"])

    def test_the_zero_approval_entry_never_accepts_the_missing_review_object(self) -> None:
        """The security case: both states are `reviews`, and only one is accepted.

        Deleting `required_pull_request_reviews` leaves a branch anybody can
        push to directly -- strictly worse than the state that was accepted.
        An acknowledgement matched on the id alone would silence exactly that.
        """
        protection = self.enforcing()
        del protection["required_pull_request_reviews"]
        still_open, accepted = self.split(protection, [self.ZERO_APPROVALS])
        self.assertEqual(accepted, [])
        self.assertEqual([gap["id"] for gap in still_open], ["reviews"])
        self.assertIn("no pull-request review is required on main", still_open[0]["gap"])
        self.assertIn("no longer matches", still_open[0]["acknowledgement_stale"])

    def test_a_bypass_acknowledgement_pins_the_actors_and_stops_when_one_is_added(self) -> None:
        """The `bypass` fact is the list the gap prints, not a boolean: an
        entry that accepted the release app goes on accepting nothing once a
        team is added beside it."""
        app, team = {"actor_id": 77, "actor_type": "Integration", "bypass_mode": "pull_request"}, {"actor_id": 9, "actor_type": "Team", "bypass_mode": "always"}
        def protection(*actors: dict[str, Any]) -> dict[str, Any]:
            return self.enforcing(source="ruleset", rulesets=[
                {"id": 42, "name": "release", "enforcement": "active", "bypass_actors": list(actors)}])
        entry = {"id": "bypass", "state": {"bypass": ["release (#42): Integration 77 (pull_request)"]},
                 "because": "the release app opens the version bump", "since": "2026-09-22",
                 "until": "the release app is retired"}
        still_open, accepted = self.split(protection(app), [self.ZERO_APPROVALS, entry])
        self.assertEqual(sorted(gap["id"] for gap in accepted), ["bypass", "reviews"])
        self.assertEqual(still_open, [])
        still_open, accepted = self.split(protection(app, team), [self.ZERO_APPROVALS, entry])
        self.assertEqual([gap["id"] for gap in accepted], ["reviews"])
        stale = [gap for gap in still_open if gap["id"] == "bypass"][0]
        self.assertIn("no longer matches", stale["acknowledgement_stale"])
        self.assertIn("Team 9 (always)", stale["acknowledgement_stale"])
        _, problems = self.written(json.dumps({"accepted_gaps": [entry]}))
        self.assertEqual(problems, [])

    def test_an_enforce_admins_acknowledgement_pins_the_exempting_rulesets(self) -> None:
        """`enforce_admins: false` alone accepts every admin bypass at once:
        an entry written for the release ruleset went on accepting a second
        exemption added on the checks ruleset. `admin_bypass` is the list
        per ruleset with the rules each reaches, and the entry that pins it
        stops applying when the list grows."""
        admin = {"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}
        def protection(*exempt: int) -> dict[str, Any]:
            return self.enforcing(source="ruleset", enforce_admins={"enabled": False}, rulesets=[
                {"id": 42, "name": "release", "rules": ["pull_request"], "bypass_actors": [admin] if 42 in exempt else []},
                {"id": 43, "name": "checks", "rules": ["required_status_checks"],
                 "bypass_actors": [admin] if 43 in exempt else []}])
        entry = {"id": "enforce_admins",
                 "state": {"enforce_admins": False, "admin_bypass": ["release (#42) [pull_request]: OrganizationAdmin 1 (always)"]},
                 "because": "admins ship the release bump; CI still gates", "since": "2026-09-22",
                 "until": "the release ruleset loses its bypass"}
        still_open, accepted = self.split(protection(42), [entry])
        self.assertEqual([gap["id"] for gap in accepted], ["enforce_admins"])
        self.assertEqual([gap["id"] for gap in still_open], ["reviews"])  # 0 approvals, unacknowledged here
        still_open, accepted = self.split(protection(42, 43), [entry])
        self.assertEqual(accepted, [])
        stale = [gap for gap in still_open if gap["id"] == "enforce_admins"][0]
        self.assertIn("no longer matches", stale["acknowledgement_stale"])
        self.assertIn("checks (#43) [required_status_checks]", stale["acknowledgement_stale"])
        _, problems = self.written(json.dumps({"accepted_gaps": [entry]}))
        self.assertEqual(problems, [])

    def test_an_unrelated_gap_is_untouched_by_an_acknowledgement(self) -> None:
        protection = self.enforcing(
            required_status_checks={"strict": False, "contexts": ["lint"]}
        )
        still_open, accepted = self.split(protection, [self.ZERO_APPROVALS])
        self.assertEqual([entry["id"] for entry in accepted], ["reviews"])
        self.assertEqual([gap["id"] for gap in still_open], ["strict"])

    def test_an_absent_file_accepts_nothing_and_is_not_a_fault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entries, problems = status.load_acknowledgements(pathlib.Path(directory))
        self.assertEqual((entries, problems), ([], []))

    def test_an_empty_state_is_rejected_rather_than_accepting_the_id(self) -> None:
        # An entry with no facts would accept `reviews` whatever the branch
        # looked like, which is the shape this file must not be able to take.
        entry = dict(self.ZERO_APPROVALS, state={})
        entries, problems = self.written(json.dumps({"accepted_gaps": [entry]}))
        self.assertEqual(entries, [])
        self.assertTrue(any("must be a non-empty object" in problem for problem in problems))

    def test_an_id_no_gap_can_emit_is_rejected_rather_than_accepting_nothing(self) -> None:
        """A typo in `id` used to load clean and accept nothing, silently.

        The loader checked that the id was a non-empty string and stopped
        there, so `unprotectd` passed, matched no finding, printed nothing, and
        left the gap it was written to accept printing on every run with no
        sign that an acknowledgement had been attempted. That is the failure
        mode this file exists to prevent, reintroduced one keystroke down, and
        it is rejected the same way an unobservable fact name already was.
        """
        for wrong in ("unprotectd", "enforce-admins", "squash_message", ""):
            with self.subTest(id=wrong):
                entry = dict(self.ZERO_APPROVALS, id=wrong)
                entries, problems = self.written(json.dumps({"accepted_gaps": [entry]}))
                self.assertEqual(entries, [])
                self.assertTrue(problems)
        # `squash_message` is in that list on purpose: it is a real id printed
        # by the report, on a merge-settings flag that never passes through
        # `_apply_acknowledgements`. An id that exists somewhere in the output
        # is still an id no acknowledgement can ever match, so the vocabulary
        # is the set of *acknowledgeable* findings, not every id in the file.
        entries, problems = self.written(
            json.dumps({"accepted_gaps": [dict(self.ZERO_APPROVALS, id="squash_message")]})
        )
        self.assertTrue(any("is not an acknowledgeable gap" in problem for problem in problems))

    def test_every_id_the_protection_section_emits_is_in_the_vocabulary(self) -> None:
        """The two halves are read from the code, not from a list written here.

        The ids were scattered string literals, which is how the loader came to
        validate a vocabulary nobody had written down. This drives the
        producers until each one fires and compares what came out against
        `ACKNOWLEDGEABLE_GAPS`, both directions: an id the producers can emit
        and the tuple omits would be unacceptable by acknowledgement, and a
        tuple member no producer emits would be a spelling an author could put
        in the file and never see applied.
        """
        emitted: set[str] = set()
        unprotected, _ = status._apply_acknowledgements(
            [{"id": "unprotected", "gap": "no protection at all"}],
            status._observed_state(None),
            [],
        )
        emitted.update(gap["id"] for gap in unprotected)
        for protection, produced in (
            ({}, set()),
            (self.enforcing(), {"lint", "a-check-nothing-requires"}),
            (self.enforcing(required_status_checks={"contexts": ["nobody-reports-this"]}), set()),
            (self.enforcing(required_pull_request_reviews=None), {"lint"}),
            (self.enforcing(source="ruleset", rulesets=[{"id": 42, "name": "release", "enforcement": "active",
                                                          "bypass_actors": [{"actor_id": 77, "actor_type": "Integration", "bypass_mode": "pull_request"}]}]), {"lint"}),
        ):
            gaps, _ = status._protection_gaps(protection, "main", produced, [])
            emitted.update(gap["id"] for gap in gaps)
        self.assertEqual(emitted, set(status.ACKNOWLEDGEABLE_GAPS))

    def test_a_fact_name_this_reader_cannot_observe_is_rejected(self) -> None:
        entry = dict(self.ZERO_APPROVALS, state={"requred_approving_review_count": 0})
        entries, problems = self.written(json.dumps({"accepted_gaps": [entry]}))
        self.assertEqual(entries, [])
        self.assertTrue(any("not an observable protection fact" in p for p in problems))

    def test_a_reason_and_an_end_condition_are_both_required(self) -> None:
        entry = {"id": "reviews", "state": {"enforce_admins": True}, "since": "2026-09-01"}
        entries, problems = self.written(json.dumps({"accepted_gaps": [entry]}))
        self.assertEqual(entries, [])
        self.assertTrue(any("missing because, until" in problem for problem in problems))

    def test_a_malformed_file_accepts_nothing_at_all(self) -> None:
        """Fail closed, and fail loudly. Never a partial application.

        One bad entry disables the file rather than the entry, because the
        author who wrote it meant all of it, and the failure direction that is
        safe is the one where every gap goes on printing.
        """
        entries, problems = self.written("[]")
        self.assertEqual(entries, [])
        self.assertTrue(any("the top level must be a JSON object" in problem for problem in problems))

    def test_an_unread_declaration_accepts_nothing_and_says_so(self) -> None:
        """sd:5109: a database that does not answer is a problem, never "nothing declared"."""
        unread = status.sd_lib.DatabaseUnreadable("the workflow database did not open")
        with mock.patch.object(status.sd_lib, "repo_declaration", side_effect=unread):
            entries, problems = status.load_acknowledgements(pathlib.Path("/nonexistent"))
        self.assertEqual(entries, [])
        self.assertEqual(problems, [f"{status.ACKNOWLEDGEMENT_SOURCE}: cannot be read (the workflow database did not open)"])

    def test_a_declaration_on_the_row_is_read_and_a_file_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home, root, other = (pathlib.Path(directory) / name for name in ("home", "repo", "other"))
            for checkout in (root, other):
                (checkout / ".github").mkdir(parents=True)
            (other / ".github" / "sd-status.json").write_text(json.dumps({"accepted_gaps": [self.ZERO_APPROVALS]}))
            seed_declaration(home, root, "status", {"accepted_gaps": [self.ZERO_APPROVALS]})
            with mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}):
                self.assertEqual(status.load_acknowledgements(root), ([self.ZERO_APPROVALS], []))
                self.assertEqual(status.load_acknowledgements(other), ([], []))

    def test_an_unknown_top_level_key_is_named(self) -> None:
        entries, problems = self.written(json.dumps({"accepted_gap": []}))
        self.assertEqual(entries, [])
        self.assertTrue(any("unknown key(s) accepted_gap" in problem for problem in problems))
        # `sd-db repo declare` drops `$schema` before writing (sd:5109), so a row never carries it.
        self.assertTrue(any("known keys are accepted_gaps" in p for p in problems))


class MergeSettingsTests(unittest.TestCase):
    """The two r7 flags: what the merge button does when a human presses it."""

    def test_clean_settings_are_not_flagged(self) -> None:
        flags = status._merge_settings(
            {
                "squash_merge_commit_title": "PR_TITLE",
                "squash_merge_commit_message": "PR_BODY",
                "allow_rebase_merge": False,
            }
        )
        self.assertEqual([flag["flagged"] for flag in flags], [False, False])

    def test_commit_messages_and_rebase_are_both_flagged(self) -> None:
        flags = status._merge_settings(
            {
                "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
                "squash_merge_commit_message": "COMMIT_MESSAGES",
                "allow_rebase_merge": True,
            }
        )
        self.assertEqual([flag["id"] for flag in flags], ["squash_message", "rebase_merge"])
        self.assertEqual([flag["flagged"] for flag in flags], [True, True])


class StatusFixture(ToolFixture):
    """The full report, run as a subprocess against a fixture repository.

    Every status is the item's row. `HOME` is the fixture's own for the whole
    test, so the operator's database is never opened in process, and the
    subprocess runs read the same database in-process readers do.
    """

    def setUp(self) -> None:
        super().setUp()
        patched = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)
        self._ready = False

    def row(
        self, directory: pathlib.Path, status: str, *, branch: str | None = None,
        stamp: str = "2026-08-01T00:00:00+00:00",
    ) -> None:
        """The row that owns `directory`'s status, in the fixture's database.

        Stamped on the day the fixture items date themselves, not now: ages
        here are measured from a pinned day, and a row stamped by the wall
        clock would always be newer than it.
        """
        import sd_db

        if not self._ready:
            sd_db.initialise(home=self.home)
            self._ready = True
        connection = sd_db.connect(home=self.home)
        try:
            sd_db.upsert_repo(connection, str(self.repo), managed=1)
            sd_db.upsert_item(
                connection, source="docs/work",
                external_id=f"{self.repo}::docs/work/{directory.name}/prd.md",
                kind="work", title=directory.name, status=status, who="test",
                repo=str(self.repo), branch=branch,
            )
            with connection:
                found = connection.execute(
                    "SELECT id FROM item WHERE external_id = ?",
                    (f"{self.repo}::docs/work/{directory.name}/prd.md",),
                ).fetchone()
                connection.execute(
                    "UPDATE item SET created_at = ?, updated_at = ? WHERE id = ?",
                    (stamp, stamp, found["id"]))
                connection.execute(
                    "UPDATE note SET timestamp = ? WHERE item = ?", (stamp, found["id"]))
        finally:
            connection.close()

    def item(self, name: str, *, status: str = "planning", extra: str = "") -> pathlib.Path:
        """A work folder as the retire leaves it -- no `status:` line -- and its row."""
        directory = self.repo / "docs" / "work" / name
        directory.mkdir(parents=True)
        (directory / "prd.md").write_text(
            PRD.format(title=name, status=status, extra=extra).replace(f"status: {status}\n", ""),
            encoding="utf-8",
        )
        branch = re.search(r"^branch: (.+)$", extra, re.MULTILINE)
        self.row(directory, status, branch=branch.group(1) if branch else None)
        return directory

    def report(self, *args: str) -> dict[str, Any]:
        return self.run_json(SD_STATUS, *args)

    def write_packet(self) -> pathlib.Path:
        """One handoff packet for the fixture repo, written by the real tool.

        On the fixture and not on `HandoffTests`, because the skill-page
        census below needs a packet too and called the other class's method
        unbound, which held only while both classes kept the same state
        (sd:821 N-6).
        """
        completed = subprocess.run(
            [sys.executable, str(SD_HANDOFF), "--summary", "mid-refactor"],
            cwd=str(self.repo),
            env=self.env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        found = sorted(
            (self.state / "sd-ai-command-pack" / "handoff").glob("*.json")
        )
        self.assertEqual(len(found), 1, completed.stdout)
        return found[0]

    @staticmethod
    def headings(text: str) -> list[str]:
        """Section headings, which are the only lines starting at column 0, without their counts.

        Derived from the output rather than declared, so a section added to
        `render()` is picked up by whoever compares two of these instead of
        being missed by both. The count is the data, so two runs over
        different data compare by name.
        """
        return [re.sub(r" \(([0-9]+|unknown)\)$", "", line) for line in text.splitlines()
                if line and not line[0].isspace() and not line.startswith("sd-status:")]

    @staticmethod
    def cells(*cells: str) -> str:
        """A pattern for a table row whose leading cells are `cells`, aligned as `sd_views.table` aligns them."""
        return r"\n  " + r" {2,}".join(re.escape(cell) for cell in cells)


class ReportShapeTests(StatusFixture):
    def test_every_section_is_present(self) -> None:
        self.with_github(pulls=[])
        result = self.report()
        for key in (
            "pack",
            "work",
            "pull_requests",
            "setup",
            "protection",
            "handoff",
            "backends",
            "residue",
        ):
            self.assertIn(key, result)

    def test_the_section_skeleton_is_the_same_with_content_and_without(
        self,
    ) -> None:
        """Step 7(d): the shape of the report is not a function of its data.

        A reader learns where to look once. If a section vanished when it had
        nothing to say, "no open pull requests" and "this tool stopped
        reporting pull requests" would render identically -- as absence -- and
        only one of them is good news. Every section therefore prints its
        heading and says so in words underneath.

        Headings are read out of the rendered text rather than listed here.
        A hardcoded list would pass while the report grew a thirteenth section
        that neither run printed, which is the failure this compares two runs
        to avoid.

        The first run is empty of *items*, not of findings: the fixture's
        protection requires a `build` context no workflow here produces, so it
        carries one gap either way. `work items` is therefore the section that
        genuinely differs between the two runs, and it is what falsifies this
        -- teaching `_render_work` to skip its heading when the repository has
        no items fails the comparison.
        """
        self.with_github(pulls=[])
        empty = self.headings(self.run_tool(SD_STATUS).stdout)
        self.assertIn("Work items", empty)

        self.item("2026-08-01-alpha", status="in_progress")
        self.item("2026-08-02-beta", status="planning")
        filled = self.headings(self.run_tool(SD_STATUS).stdout)
        self.assertEqual(empty, filled)

    def test_the_pack_banner_names_the_checkout_the_tools_came_from(self) -> None:
        self.with_github(pulls=[])
        result = self.report()
        self.assertEqual(result["pack"]["root"], str(BIN.parent))
        completed = self.run_tool(SD_STATUS)
        self.assertIn(f"pack: {BIN.parent}", completed.stdout)

    def test_a_detached_pack_checkout_prints_as_pinned(self) -> None:
        """sd:5675: the hub pins the pack on purpose (system sd:3097); `HEAD @` read as a fault."""
        stream = io.StringIO()
        pack = {"root": "/pack", "branch": "HEAD", "head": "acd4be0", "dirty": False}
        status._render_pack({"repo": "/repo", "pack": pack}, stream.write)
        self.assertIn("  pack: /pack (pinned @ acd4be0)\n", stream.getvalue())
        stream = io.StringIO()
        status._render_pack({"repo": "/repo", "pack": dict(pack, branch="main")}, stream.write)
        self.assertIn("  pack: /pack (main @ acd4be0)\n", stream.getvalue())

    def test_gaps_are_reported_not_raised(self) -> None:
        self.with_github(pulls=[], protection=None)
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("has no branch protection at all", completed.stdout)

    def test_pull_requests_come_from_the_same_code_path_as_sd_pr_state(self) -> None:
        self.with_github(
            pulls=[
                {
                    "number": 12,
                    "title": "One",
                    "headRefName": "task/one",
                    "baseRefName": "main",
                    "isDraft": True,
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "BLOCKED",
                    "reviewDecision": "",
                    "headRepositoryOwner": {"login": "acme"},
                    "statusCheckRollup": [{"name": "lint", "conclusion": "SUCCESS"}],
                }
            ]
        )
        from tests.test_sd_pr_state import SD_PR_STATE

        combined = self.report()["pull_requests"]["pull_requests"]
        alone = self.run_json(SD_PR_STATE)["pull_requests"]
        self.assertEqual(combined, alone)
        self.assertIn("draft", self.run_tool(SD_STATUS).stdout)


class ProtectionSectionTests(StatusFixture):
    def test_this_repos_workflows_are_diffed_against_its_required_contexts(self) -> None:
        workflows = self.repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "tests.yml").write_text(WORKFLOW, encoding="utf-8")
        self.with_github(
            pulls=[],
            protection={
                "enforce_admins": {"enabled": True},
                "required_status_checks": {
                    "strict": True,
                    "contexts": [
                        "unittest (ubuntu-latest, 3.10)",
                        "unittest (macos-latest, 3.13)",
                        "Shell coverage",
                        "lint",
                    ],
                },
                "required_pull_request_reviews": {"required_approving_review_count": 1},
            },
        )
        section = self.report()["protection"]
        self.assertEqual(section["gaps"], [])
        self.assertEqual(section["detail"]["required_not_produced"], [])
        self.assertEqual(section["detail"]["produced_not_required"], [])
        self.assertIn("fully enforcing", self.run_tool(SD_STATUS).stdout)

    def test_a_classic_404_to_a_token_without_admin_prints_as_unknown(self) -> None:
        """The same 404, from a token without `admin` on the repository: the
        classic side is unseen, so the report says unknown and why, raises no
        `unprotected` finding, and does not claim the all-clear either."""
        self.with_github(pulls=[], protection=None, repo=dict(REPO_SETTINGS, permissions={"admin": False}))
        section = self.report()["protection"]
        self.assertIsNone(section["protected"])
        self.assertEqual(section["gaps"], [])
        self.assertIn("admin", section["reason"])
        text = self.run_tool(SD_STATUS).stdout
        self.assertIn("protection unknown -- classic protection on main is not visible to this token", text)
        self.assertNotIn("fully enforcing", text)
        self.assertNotIn("no branch protection at all", text)

    def test_an_unprotected_default_branch_is_one_named_gap(self) -> None:
        self.with_github(pulls=[], protection=None)
        section = self.report()["protection"]
        self.assertEqual([gap["id"] for gap in section["gaps"]], ["unprotected"])
        self.assertFalse(section["protected"])

    def test_admin_exemption_reaches_the_human_output(self) -> None:
        self.with_github(
            pulls=[],
            protection={
                "enforce_admins": {"enabled": False},
                "required_status_checks": {"strict": True, "contexts": []},
                "required_pull_request_reviews": {"required_approving_review_count": 1},
            },
        )
        completed = self.run_tool(SD_STATUS)
        self.assertRegex(completed.stdout, self.cells("enforce_admins", "gap"))
        self.assertIn("prose, not authority", completed.stdout)

    def acknowledge(self, *entries: Any, body: Any = None) -> None:
        """The fixture repository's `status` declaration on its row (sd:5109)."""
        seed_declaration(self.home, self.repo, "status", {"accepted_gaps": list(entries)} if body is None else body)

    ZERO_APPROVALS = AcknowledgementTests.ZERO_APPROVALS

    def test_an_accepted_finding_prints_as_accepted_and_leaves_the_gaps(self) -> None:
        self.acknowledge(self.ZERO_APPROVALS)
        self.with_github(
            pulls=[],
            protection={
                "enforce_admins": {"enabled": True},
                "required_status_checks": {"strict": True, "contexts": []},
                "required_pull_request_reviews": {"required_approving_review_count": 0},
            },
        )
        section = self.report()["protection"]
        # A consumer counting `gaps` sees none; one reading `accepted` sees the
        # decision. Neither has to know about the other to stay correct.
        self.assertEqual(section["gaps"], [])
        self.assertEqual([entry["id"] for entry in section["accepted"]], ["reviews"])
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertRegex(completed.stdout, self.cells("reviews", "accepted", "2026-09-01: one maintainer"))
        self.assertIn("reviews until: a second account with merge rights exists", completed.stdout)
        # An accepted finding is not an absent one: the all-clear must not
        # print over the top of a state the repository wrote down.
        self.assertNotIn("fully enforcing", completed.stdout)

    def test_a_drifted_acknowledgement_prints_the_gap_and_says_it_no_longer_matches(self) -> None:
        self.acknowledge(self.ZERO_APPROVALS)
        self.with_github(
            pulls=[],
            protection={
                # The review object is gone: nothing requires a pull request
                # any more, which is the worse of the two `reviews` states.
                "enforce_admins": {"enabled": True},
                "required_status_checks": {"strict": True, "contexts": []},
            },
        )
        section = self.report()["protection"]
        self.assertEqual(section["accepted"], [])
        self.assertEqual([gap["id"] for gap in section["gaps"]], ["reviews"])
        completed = self.run_tool(SD_STATUS)
        self.assertRegex(completed.stdout, self.cells("reviews", "gap", "no pull-request review is required"))
        self.assertIn("no longer matches the live protection state", completed.stdout)

    UNPROTECTED = {
        "id": "unprotected",
        "state": {"branch_protection": False},
        "because": "sole operator, and no CI checks exist for protection to require",
        "since": "2026-09-11",
        "until": "a second account with push rights exists",
    }

    def test_an_accepted_unprotected_branch_prints_as_accepted(self) -> None:
        """The 404 branch returned before it ever applied the file.

        `unprotected` is the only gap a repository with no protection can be
        told about, so it is the one a repository that has decided against
        protection most needs to accept -- and it was the single finding the
        acknowledgement path could not reach. The file was read and its path
        was reported in `detail`, which is what made the omission look like a
        working feature: nothing was ever matched against it.
        """
        self.acknowledge(self.UNPROTECTED)
        self.with_github(pulls=[], protection=None)
        section = self.report()["protection"]
        self.assertEqual(section["gaps"], [])
        self.assertEqual([entry["id"] for entry in section["accepted"]], ["unprotected"])
        # Accepted is not protected. A consumer reading `protected` still gets
        # the fact about the branch, whatever the repository decided about it.
        self.assertFalse(section["protected"])
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertRegex(completed.stdout, self.cells("unprotected", "accepted", "2026-09-11: sole operator"))
        self.assertIn("unprotected until: a second account with push rights exists", completed.stdout)

    def test_an_accepted_unprotected_branch_whose_rules_failed_prints_as_unknown(self) -> None:
        """sd:1000. The same acknowledgement, but the rules endpoint did not
        answer a list: the report says unknown and why, and prints no
        acceptance over a state it could not read."""
        self.acknowledge(self.UNPROTECTED)
        self.with_github(pulls=[], protection=None, rules={"message": "Server Error"})
        section = self.report()["protection"]
        self.assertIsNone(section["protected"])
        self.assertEqual(section["accepted"], [])
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("main: protection unknown -- branch rulesets on main could not be read (Server Error",
                      completed.stdout)
        self.assertNotIn("accepted 2026-09-11", completed.stdout)

    def test_an_unprotected_acknowledgement_naming_protection_does_not_apply(self) -> None:
        """`branch_protection` is a real pin here, and the other four cannot be.

        Every other fact in the vocabulary reduces a protection object. On this
        branch there is no object, so all four are constants whatever the
        branch looks like -- an entry pinning only those accepts the id
        unconditionally, which is the shape the non-empty `state` rule exists
        to forbid. `branch_protection` is the one fact that can be wrong here,
        so it is the one that can go stale.
        """
        self.acknowledge(dict(self.UNPROTECTED, state={"branch_protection": True}))
        self.with_github(pulls=[], protection=None)
        section = self.report()["protection"]
        self.assertEqual(section["accepted"], [])
        self.assertEqual([gap["id"] for gap in section["gaps"]], ["unprotected"])
        completed = self.run_tool(SD_STATUS)
        self.assertRegex(completed.stdout, self.cells("unprotected", "gap"))
        self.assertIn("no longer matches the live protection state", completed.stdout)

    def test_a_broken_acknowledgement_file_still_fails_closed_when_unprotected(self) -> None:
        """Failing closed has to hold on this branch too, not just the other one.

        A malformed file accepts nothing anywhere; the risk is that the branch
        which never applied acknowledgements also never reported why, leaving
        `unprotected` printing with no hint that the file meant to accept it.
        """
        self.acknowledge(body={"accepted_gaps": "nope"})
        self.with_github(pulls=[], protection=None)
        section = self.report()["protection"]
        self.assertEqual(
            [gap["id"] for gap in section["gaps"]], ["acknowledgements", "unprotected"]
        )
        self.assertEqual(section["accepted"], [])

    def test_an_unreadable_acknowledgement_file_is_itself_a_gap(self) -> None:
        self.acknowledge(body={"accepted_gaps": "nope"})
        self.with_github(
            pulls=[],
            protection={
                "enforce_admins": {"enabled": True},
                "required_status_checks": {"strict": True, "contexts": []},
                "required_pull_request_reviews": {"required_approving_review_count": 0},
            },
        )
        section = self.report()["protection"]
        self.assertEqual(
            [gap["id"] for gap in section["gaps"]], ["acknowledgements", "reviews"]
        )
        self.assertEqual(section["accepted"], [])

    def test_protection_degrades_with_no_github_remote(self) -> None:
        self.install_gh()
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("no GitHub remote", completed.stdout)

    def test_a_broken_acknowledgement_file_reaches_the_terminal_without_github(self) -> None:
        """The one finding this branch can still make must not be `--json`-only.

        A malformed file is a fact about the checkout, not about GitHub. If it
        printed only when the protection object could be fetched, the file
        would silently accept nothing in exactly the situation -- no `gh`, no
        network -- where nobody is reading the JSON.
        """
        self.acknowledge(body={"accepted_gaps": "nope"})
        self.install_gh()
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("unavailable:", completed.stdout)
        self.assertIn("\nProtection (unknown)\n", completed.stdout)
        self.assertRegex(completed.stdout, self.cells("acknowledgements", "gap"))
        self.assertIn("accepted_gaps must be a list", completed.stdout)


class WorkItemTests(StatusFixture):
    def test_status_is_derived_and_counted(self) -> None:
        self.item("2026-08-01-alpha", status="planning")
        self.item("2026-08-02-beta", status="in_progress", extra="branch: task/beta\n")
        result = self.report()
        self.assertEqual(result["work"]["counts"], {"planning": 1, "in_progress": 1})
        self.assertEqual(result["work"]["active"], 2)

    def test_a_parked_frontmatter_line_is_read_by_nothing(self) -> None:
        """31(a) cut the field. The line may still sit in an item's
        frontmatter -- 88 archived items across the operator's repositories
        carry one -- and it must now be inert rather than an unknown key or
        a crash. The item counts and is listed like any other."""
        self.item("2026-08-01-alpha")
        self.item("2026-08-02-stale", extra="parked: 2026-08-20 age-sweep\n")
        result = self.report()
        self.assertEqual(result["work"]["active"], 2)
        self.assertNotIn("parked", result["work"])
        entry = [row for row in result["work"]["items"] if row["slug"] == "stale"][0]
        self.assertNotIn("parked", entry)

    def test_the_parked_flag_is_gone(self) -> None:
        completed = self.run_tool(SD_STATUS, "--parked")
        self.assertEqual(completed.returncode, 2, completed.stdout)
        self.assertIn("unrecognized arguments: --parked", completed.stderr)

    def test_an_item_with_no_row_is_an_inconsistency_not_a_crash(self) -> None:
        self.item("2026-08-02-beta")
        orphan = self.repo / "docs" / "work" / "2026-08-01-alpha"
        orphan.mkdir(parents=True)
        (orphan / "prd.md").write_text(
            PRD.format(title="alpha", status="", extra="").replace("status: \n", ""),
            encoding="utf-8",
        )
        result = self.report()
        entry = next(row for row in result["work"]["items"] if row["slug"] == "alpha")
        self.assertEqual(entry["status"], "unknown")
        self.assertTrue(entry["inconsistencies"])


class HandoffTests(StatusFixture):
    """The packet is read. It is never consumed -- that is `--show`'s job."""

    def test_the_packet_is_reported_as_pending(self) -> None:
        self.write_packet()
        section = self.report()["handoff"]
        self.assertTrue(section["packet"]["pending"])
        self.assertEqual(section["packet"]["summary"], "mid-refactor")
        self.assertIsNone(section["packet"]["consumed"])

    def test_reading_leaves_the_file_byte_identical_and_unconsumed(self) -> None:
        path = self.write_packet()
        before = path.read_bytes()
        for _ in range(2):
            self.assertEqual(self.run_tool(SD_STATUS).returncode, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNone(json.loads(before)["consumed"])

    def test_an_already_consumed_packet_is_not_pending(self) -> None:
        path = self.write_packet()
        packet = json.loads(path.read_text(encoding="utf-8"))
        packet["consumed"] = "2026-08-29T00:00:00+00:00"
        path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
        section = self.report()["handoff"]
        self.assertFalse(section["packet"]["pending"])
        self.assertIn("consumed at", section["packet"]["detail"])

    def test_no_packet_is_a_sentence_not_an_absence(self) -> None:
        completed = self.run_tool(SD_STATUS)
        self.assertIn("no packet written for this directory", completed.stdout)

    def test_the_packet_is_the_reported_repository_s_and_not_pwd_s(self) -> None:
        """31(c): the section answers about the repo the report is about.

        `handoff.resolve_root` falls back to `environ["PWD"]`, and this
        section used to pass it nothing, so the packet it read was whatever
        directory the environment named. That is the same directory almost
        always, which is why the bug survived: it separates only when `PWD`
        is stale or a caller runs the tool with a cwd it did not export, and
        then the report describes one repository while its handoff line
        describes another.

        The fixture makes them differ on purpose. `cwd` is the repository,
        `PWD` is a second one beside it, and the packet must follow the
        first. Nothing here passes a repository to the tool -- there is no
        argument that could, and R10-D6 says there will not be. The fix is
        that the root already resolved from cwd is the one handed on.
        """
        other = self.base / "elsewhere"
        other.mkdir()
        self.init_repo(other)
        path = self.write_packet()

        environment = self.env()
        environment["PWD"] = str(other)
        completed = subprocess.run(
            [sys.executable, str(SD_STATUS), "--json"],
            cwd=str(self.repo), env=environment, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        packet = json.loads(completed.stdout)["handoff"]["packet"]

        self.assertEqual(packet["directory"], str(self.repo))
        self.assertEqual(packet["path"], str(path))
        self.assertTrue(packet["pending"], completed.stdout)


class ResidueTests(StatusFixture):
    def test_each_finding_carries_the_command_that_removes_it(self) -> None:
        (self.repo / ".trellis").mkdir()
        (self.repo / ".trellis" / "state.json").write_text("{}", encoding="utf-8")
        result = self.report()
        found = {entry["id"]: entry for entry in result["residue"]}
        self.assertIn("trellis", found)
        self.assertIn("rm -rf .trellis", found["trellis"]["remove"])
        self.assertIn(f"\n  trellis remove: {found['trellis']['remove']}\n", self.run_tool(SD_STATUS).stdout)

    def test_a_clean_repository_reports_none(self) -> None:
        completed = self.run_tool(SD_STATUS)
        self.assertTrue(completed.stdout.endswith("\nLegacy residue (0)\n"), completed.stdout)

    def test_a_configured_hooks_path_is_residue(self) -> None:
        self.git("config", "core.hooksPath", ".githooks")
        found = {entry["id"]: entry for entry in self.report()["residue"]}
        self.assertIn("hooks-path", found)
        self.assertEqual(found["hooks-path"]["remove"], "git config --unset core.hooksPath")


#: A registry holding one entry of each shape, under names this repository
#: writes nowhere else. A hand-written backend list cannot produce them, which
#: is what makes the tests below falsify a recited one.
FIXTURE_REGISTRY = """\
bills:
  acme:  { cost: prepaid }
  seat:  { cost: subscription }
  local: { cost: local }

providers:
  acme-cli:  { start: "git status", vendor: acme, bill: seat,
               roles: [reviewer], reader: codex-json, env: [] }
  acme-url:  { url: "https://api.acme.example/v1", model: acme-1, vendor: acme,
               bill: acme, roles: [reviewer], max_tokens: 1024,
               price: { in: 1, out: 1 }, env: [ACME_API_KEY] }
  homelab:   { url: "http://localhost:9099/v1", model: homelab-1, vendor: local,
               bill: local, roles: [reviewer], max_tokens: 1024,
               price: { in: 0, out: 0 }, env: [] }

roles:
  author:   []
  reviewer: [acme-cli]
"""


class BackendTests(StatusFixture):
    def seed_registry(self) -> pathlib.Path:
        """Give the temp HOME a registry, where the installer puts one."""
        path = self.home / ".local" / "share" / "sd" / "providers.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(FIXTURE_REGISTRY, encoding="utf-8")
        return path

    def backends(self) -> list[dict[str, str]]:
        return self.report()["backends"]

    def test_backends_are_reported_by_name_and_state_only(self) -> None:
        self.seed_registry()
        rows = self.backends()
        for entry in rows:
            self.assertEqual(set(entry), {"backend", "command", "state"})
            self.assertIn(entry["state"], ("present", "absent", "unauthenticated"))
        self.assertIn("copilot", [entry["backend"] for entry in rows])

    def test_the_registry_names_the_lanes(self) -> None:
        """The section enumerates entries no list in this repository holds."""
        self.seed_registry()
        states = {entry["backend"]: entry["state"] for entry in self.backends()}
        # The fixture PATH holds git alone, so this start entry resolves.
        self.assertEqual(states["acme-cli"], "present")
        # A url entry declaring a key needs its value, which nothing exports.
        self.assertEqual(states["acme-url"], "unauthenticated")
        # A keyless entry reaching this machine authenticates through the
        # socket, which is the rule `sd_registry.loopback` carries.
        self.assertEqual(states["homelab"], "present")

    def test_a_url_entry_reports_no_command(self) -> None:
        """It runs no program, so the section invents none for it."""
        self.seed_registry()
        commands = {entry["backend"]: entry["command"] for entry in self.backends()}
        self.assertEqual(commands["acme-url"], "")
        self.assertEqual(commands["acme-cli"], "git")

    def test_no_registry_leaves_the_lane_no_entry_names(self) -> None:
        """A missing registry degrades the section; it does not fail the run.

        `copilot` survives because GitHub's reviewer is reached through `gh`
        and no provider entry can describe it. The fixture PATH holds git
        alone, so `gh` is not found either.
        """
        rows = self.backends()
        self.assertEqual([entry["backend"] for entry in rows], ["registry", "copilot"])
        self.assertTrue(rows[0]["state"].startswith("unreadable (no provider registry at "))
        self.assertEqual(rows[1]["state"], "absent")

    def test_an_unreadable_registry_says_why(self) -> None:
        """sd:3494: a typo in providers.yaml listed no backend and said nothing."""
        self.seed_registry().write_text("providers: [\n", encoding="utf-8")
        rows = {entry["backend"]: entry["state"] for entry in self.backends()}
        self.assertTrue(rows["registry"].startswith("unreadable ("), rows)


class CodexAuthTests(unittest.TestCase):
    """sd:3497: codex keeps auth.json under CODEX_HOME, as sd-review reads it."""

    def test_codex_auth_is_read_where_codex_home_points(self) -> None:
        codex = SimpleNamespace(name="codex", enabled=True, kind="start", start="git", env=())
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as moved:
            (pathlib.Path(moved) / "auth.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": home, "CODEX_HOME": moved}):
                self.assertEqual(status._provider_row(codex)["state"], "present")
            with mock.patch.dict(os.environ, {"HOME": home, "CODEX_HOME": ""}):
                self.assertEqual(status._provider_row(codex)["state"], "unauthenticated")


class RepoResolutionTests(StatusFixture):
    """R10-D6, asserted against the parser rather than only against --help."""

    def test_no_action_accepts_a_repository_path(self) -> None:
        for module, tool in (
            (status, "sd-status"),
            (_load("sd-pr-state", "sd_pr_state_under_test"), "sd-pr-state"),
        ):
            parser = module.build_parser()
            banned = {
                "repo",
                "repo-path",
                "path",
                "root",
                "dir",
                "directory",
                "cwd",
                "checkout",
                "worktree",
                "fleet",
                "all",
                "all-repos",
                "C",
            }
            if tool == "sd-pr-state":
                # R10-D6's one exception (sd:1910): a lane command takes `-C`,
                # a change of working directory before anything resolves.
                # `tests/test_verb_inventory.py` allow-lists the five by name.
                banned.discard("C")
            with self.subTest(tool=tool):
                for action in parser._actions:
                    self.assertTrue(
                        action.option_strings,
                        f"{tool} takes a positional argument: {action.dest}",
                    )
                    for option in action.option_strings:
                        self.assertNotIn(option.lstrip("-"), banned)

    def test_there_is_no_fleet_walk(self) -> None:
        text = self.run_tool(SD_STATUS, "--help").stdout
        for word in ("fleet", "every installed", "all repos", "installed checkout"):
            self.assertNotIn(word, text.lower())


class IssueSectionTests(StatusFixture):
    """The `issues:` lines, which read the shared database and never collect.

    sd:719 step 4 retired the legacy index and the `store` fallback that read
    it, so every test here seeds `sd_db.shadow` under the fixture HOME the
    way `JiraSectionTests` does -- the one store the section has left. The
    distinction the section is built around survives: no database is a
    different answer from no rows, and reporting the second where the first
    is true is the kind of wrong that looks right. The `needs_you` split went
    with the index; the database path reports every open row under `other`.
    """

    def seed_shadow(self, rows: list[dict]) -> None:
        """Rows through the library, into the database the subprocess resolves under HOME."""
        import sd_db

        sd_db.initialise(home=self.home)
        connection = sd_db.connect(home=self.home)
        try:
            for row in rows:
                sd_db.writes.upsert_shadow(connection, **row)
        finally:
            connection.close()

    @staticmethod
    def jira_row(key: str, *, repo: str = "") -> dict:
        """A row shaped the way the Jira collector writes them: a browse URL, no number."""
        return {
            "tracker": "jira",
            "url": f"https://example.atlassian.net/browse/{key}",
            "repo": repo,
            "number": None,
            "kind": "issue",
            "title": key,
            "state": "open",
            "reason": "assigned",
        }

    @staticmethod
    def row(repo: str, number: int, *, tracker: str = "github") -> dict:
        return {
            "tracker": tracker,
            "url": f"https://github.com/{repo}/pull/{number}",
            "repo": repo,
            "number": number,
            "kind": "pull",
            "title": f"work on {number}",
            "state": "open",
        }

    def test_no_shared_database_is_a_reported_gap_that_names_no_index(self) -> None:
        """sd:719 step 4. The shared database is the only source, and its absence says so.

        The legacy index and the `sd-dashboard index` verb that filled it are
        gone, so the no-database answer can no longer send the reader to run
        it. Same shape as the section's other gaps -- both empty lists -- and
        the reason names the library the rows now live in.
        """
        self.with_github(pulls=[])
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("sd-dashboard index", completed.stdout)
        self.assertNotIn("no index yet", completed.stdout)
        section = self.report()["issues"]
        self.assertEqual(sorted(section), ["available", "needs_you", "other", "reason"])
        self.assertFalse(section["available"])
        self.assertIn("sd_db", section["reason"])
        self.assertEqual((section["needs_you"], section["other"]), ([], []))

    def test_rows_for_this_repo_are_reported_from_the_shared_database(self) -> None:
        self.with_github(pulls=[])
        self.seed_shadow([self.row("acme/widget", 1), self.row("acme/widget", 2)])
        completed = self.run_tool(SD_STATUS)
        self.assertIn("other open: 2", completed.stdout)
        result = self.report()["issues"]
        self.assertEqual(result["source"], "database")
        self.assertEqual(sorted(row["number"] for row in result["other"]), [1, 2])

    def test_another_repository_s_rows_are_not_shown(self) -> None:
        """The filter is the point; presence alone would pass without it."""
        self.with_github(pulls=[])
        self.seed_shadow([self.row("acme/widget", 1), self.row("other/thing", 99)])
        result = self.report()["issues"]
        self.assertEqual([row["number"] for row in result["other"]], [1],
                         "another repository's issue leaked in")

    def test_a_jira_row_is_not_attributed_to_a_checkout(self) -> None:
        """Named gap: no committed fact ties a Jira project to a repository.

        A real Jira row carries no repo slug at all. It reaches the report
        through the `jira` section, never this one.
        """
        self.with_github(pulls=[])
        self.seed_shadow([self.jira_row("RS-9")])
        result = self.report()
        self.assertEqual(result["issues"]["other"], [])
        self.assertEqual([row["url"].rpartition("/")[2] for row in result["jira"]["rows"]], ["RS-9"])

    def test_the_filter_is_on_the_tracker_and_not_only_on_the_slug(self) -> None:
        """Belt to the previous test's braces.

        A real Jira row carries no repo, so the test above would still pass if
        the filter were `repo == slug` alone. This one carries a matching slug
        and must still be excluded, which is only true while `tracker` is part
        of the filter.
        """
        self.with_github(pulls=[])
        self.seed_shadow([self.jira_row("RS-10", repo="acme/widget")])
        result = self.report()["issues"]
        self.assertTrue(result["available"])
        self.assertEqual(result["other"], [], "a Jira row with a matching slug leaked in")

    def test_a_copy_of_bin_alone_runs_the_section_without_the_dashboard_package(self) -> None:
        """`bin/` copied alone has no `dashboard` to import, and no longer needs one.

        The read-only suite runs exactly that copy, and it is how the old
        coupling to the index reader was found. With the fallback gone the
        section answers from `sd_lib` and the shared database, which travel
        with `bin/`, and `lib/` beside it, the one `sd_db` (sd:3280); the copy
        reports the same gap the full checkout does.
        """
        tools = self.base / "tools"
        shutil.copytree(BIN, tools, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(BIN.parent / "lib", self.base / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        self.with_github(pulls=[])
        completed = self.run_tool(tools / "sd-status")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("not importable from this checkout", completed.stdout)
        self.assertIn(f"no shared sd_db database yet; {status.NO_DATABASE_REMEDY}", completed.stdout)

    def test_the_section_is_in_the_json_report(self) -> None:
        self.with_github(pulls=[])
        self.seed_shadow([self.row("acme/widget", 1)])
        result = self.report()
        self.assertIn("issues", result)
        self.assertTrue(result["issues"]["available"])
        self.assertEqual(len(result["issues"]["other"]), 1)

    def test_the_shared_database_does_not_stall_work(self) -> None:
        """A tracker row is external context: it raises no `issue-*` action."""
        import sd_db

        self.with_github(pulls=[])
        sd_db.initialise(home=self.home)
        connection = sd_db.connect(home=self.home)
        try:
            sd_db.writes.upsert_shadow(
                connection, tracker="github", repo="acme/widget", number=7,
                url="https://github.com/acme/widget/issues/7", title="External context",
                kind="issue", state="open")
        finally:
            connection.close()
        result = self.report()
        self.assertEqual(result["issues"]["source"], "database")
        self.assertEqual(result["issues"]["freshness"]["state"], "never")
        self.assertEqual([row["number"] for row in result["issues"]["other"]], [7])
        self.assertEqual(result["issues"]["needs_you"], [])
        self.assertFalse(any(row["check"].startswith("issue-") for row in result["actions"]))

    #: `import sd_db` raises `ImportError` while a `None` sits under the name.
    #: Nothing is written anywhere for this: a fixture site-packages directory
    #: would be a second installed copy of the library to keep true, and the
    #: branch under test is reached by the import failing, not by where it
    #: failed from.
    NO_SHARED_LIBRARY = {"sd_db": None, "sd_db.progress": None}

    def test_a_missing_shared_library_is_a_reported_gap(self) -> None:
        """sd:746, kept after sd:719 step 4 took the index away.

        The library being unimportable says nothing about the shared
        database, which can be on disk and current while this checkout simply
        has no reader for it, so the answer is a reported gap. Before step 4
        the wrong branch here sent the caller to the legacy index; there is no
        index and no `store` name left to send it to, and the second
        assertion pins that.

        This runs in process because the branch is reached by an import
        failing, and the fixture's child process runs on this interpreter,
        where `sd_db` is installed.
        """
        self.with_github(pulls=[])
        # Nothing provisioned, pinned: a checkout with a `.venv` would otherwise take the other branch.
        library_unimportable(self, self.NO_SHARED_LIBRARY, provisioned="")
        section = status.issues_section(self.repo)

        self.assertFalse(hasattr(status, "store"), "the index reader is back under `store`")
        self.assertFalse(section["available"])
        self.assertIn("not installed", section["reason"])
        self.assertEqual(section["needs_you"], [])
        self.assertEqual(section["other"], [])

    def test_the_missing_library_answer_is_shaped_like_the_sections_other_gaps(
        self,
    ) -> None:
        """`needs_you` and `other` are indexed by readers that never check first.

        `_render_issues` returns on `available` alone, but the `--json` object
        is one shape per section and every other unavailable answer here --
        `no GitHub remote`, `no shared sd_db database yet`, `shared database
        unreadable` -- carries both empty lists. A gap that dropped them would
        be the only one, and a consumer indexing `needs_you` would raise on
        exactly the machine that is already missing a library.
        """
        library_unimportable(self, self.NO_SHARED_LIBRARY, provisioned="")
        answer = status._database_issues("acme/widget")

        self.assertEqual(
            sorted(answer), ["available", "needs_you", "other", "reason"]
        )
        self.assertEqual(answer["needs_you"], [])
        self.assertEqual(answer["other"], [])


class HubUnreachableTests(StatusFixture):
    """sd:3496: on a satellite `default_path().exists()` asks the hub, and a hub that is down raised a traceback."""

    def test_every_database_section_names_the_unreachable_hub_and_the_remedy(self) -> None:
        import sd_db
        from sd_db.remote import HubUnreachable

        class DownHub(type(pathlib.Path())):
            def exists(self, *, follow_symlinks: bool = True) -> bool:
                raise HubUnreachable("hub.example.test", 7000, "connection refused")

        self.enterContext(mock.patch.object(sd_db, "default_path", return_value=DownHub(self.home / "sd.db")))
        for answer in (status._database_issues("acme/widget"), status.jira_section(),
                       status.contributions_section(self.repo)):
            self.assertFalse(answer["available"])
            self.assertEqual(answer["reason"], "the sd hub at hub.example.test:7000 is unreachable: connection refused; "
                                               "check ~/.config/sd/hub.json and that the hub is up")


class JiraSectionTests(StatusFixture):
    """The `jira` section: the operator's Jira involvement, across every repository.

    sd:361 step 7. The issues section is "this repo": `issues_section` returns
    `no GitHub remote` before any database is opened, and its renderer formats
    `number`, which a Jira row does not have. So the Jira rows get their own
    producer, which never looks at the checkout's slug, and their own
    renderer, which formats the URL tail and never `number`. The fixture
    database is the library's own, initialised under the temp HOME the
    subprocess resolves, so no test here reads or writes the live store.
    """

    HEADING = "Jira, shared database, all repositories"

    def seed(self, rows: list[dict[str, Any]], *, heartbeat: dict[str, Any] | None = None,
             collected_at: str | None = None) -> None:
        """Rows, an optional `tracker-sync:jira` heartbeat and an optional watermark, through the library.

        `last_seen` is the collector's clock and `upsert_shadow` stamps it
        `now`; a row that must look older is aged afterwards with one UPDATE,
        because no library write takes a stamp and the producer's cutoff is a
        property of that column alone.
        """
        import sd_db

        sd_db.initialise(home=self.home)
        connection = sd_db.connect(home=self.home)
        try:
            for row in rows:
                age = row.pop("age_days", None)
                sd_db.writes.upsert_shadow(connection, tracker="jira", **row)
                if age is not None:
                    seen = (datetime.datetime.now(datetime.timezone.utc)
                            - datetime.timedelta(days=age)).isoformat(timespec="seconds")
                    with connection:
                        connection.execute("UPDATE shadow SET last_seen = ? WHERE tracker = 'jira' AND url = ?",
                                           (seen, row["url"]))
            if heartbeat is not None:
                sd_db.writes.record_state(connection, "heartbeat", key="tracker-sync:jira", body=heartbeat)
            if collected_at is not None:
                from sd_db.shadow_sync import write_watermark
                write_watermark(connection, "jira", collected_at, collected_at)
        finally:
            connection.close()

    @staticmethod
    def updated(days: float) -> str:
        """An `updated_at` `days` before now, as `shadow_sync.iso` writes it."""
        return (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    @classmethod
    def ticket(cls, key: str, title: str, *, state: str = "open", **extra: Any) -> dict[str, Any]:
        """A row as `sd_db.shadow_jira` writes it: no number, a browse URL, a project key for repo."""
        return {"url": f"https://example.atlassian.net/browse/{key}", "repo": key.partition("-")[0],
                "number": None, "kind": "issue", "title": title, "state": state, "author": "someone",
                "reason": "assigned", "updated_at": cls.updated(1), **extra}

    def setUp(self) -> None:
        super().setUp()
        # The machine config `sd.jira_recent_days` is read from, under the temp HOME, never the operator's.
        patched = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / ".config")})
        patched.start()
        self.addCleanup(patched.stop)

    def configure(self, days: str) -> None:
        path = self.home / ".config" / "sd-ai-command-pack" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"config": {"sd": {"jira_recent_days": days}}}), encoding="utf-8")

    def section(self, text: str, count: str | None = None) -> str:
        """The lines under the `jira` heading, up to the blank line before the next one; `count` pins the heading's."""
        found = re.search(rf"\n{re.escape(self.HEADING)} \(([0-9]+|unknown)\)\n", text)
        self.assertIsNotNone(found, text)
        if count is not None:
            self.assertEqual(count, found[1])
        return text[found.end():].split("\n\n", 1)[0] + "\n"

    def test_a_row_with_no_number_prints_its_key_in_a_checkout_with_no_remote(self) -> None:
        """(a) The regression for `#{row['number']:<6}` and for the slug gate.

        No `with_github`: the fixture repo has no origin, so the issues section
        answers `no GitHub remote` and, before this section existed, nothing
        read a Jira row anywhere. `number` is NULL, so routing the row through
        `_render_issues` is a `TypeError`.
        """
        self.seed([self.ticket("LOG-23818", "Benchmark harness")])
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertRegex(self.section(completed.stdout), r'\n  LOG-23818 +open +assigned +"Benchmark harness"\n')

    def test_an_empty_tracker_says_never_collected_then_none(self) -> None:
        """(b) No rows and no heartbeat: two lines, both about absence."""
        self.seed([])
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual("  never collected\n", self.section(completed.stdout, "0"))

    def test_a_partial_collect_shows_its_reason_and_the_rows_it_stored(self) -> None:
        """(c) A truncated first run stores what it saw, and the reader says so.

        `tracker_freshness` calls this `degraded`, not `never`, because a
        failed heartbeat exists; the line is keyed on `last_success_at`, which
        is still absent, so it reads `never collected (<reason>)`. The rows are
        printed anyway: hiding them would contradict the verb that reported
        writing them.
        """
        self.seed([self.ticket("LOG-1", "First"), self.ticket("LOG-2", "Second")],
                  heartbeat={"ok": False, "reason": "collection time limit exhausted", "truncated": True})
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        block = self.section(completed.stdout, "2")
        self.assertTrue(block.endswith("\n  never collected (collection time limit exhausted)\n"), block)
        self.assertRegex(block, r'\n  LOG-1 +open +assigned +"First"\n')
        self.assertRegex(block, r'\n  LOG-2 +open +assigned +"Second"\n')

    def test_a_completed_collect_prints_the_external_context_pair(self) -> None:
        """The other side of the `last_success_at` key: a watermark exists, so the pair prints.

        A failed heartbeat lands after the watermark, so `tracker_freshness`
        says `degraded` -- the same word it says with no success at all. Keyed
        on the state word the line would read `never collected (boom)` and
        deny a sync that happened; keyed on `last_success_at` it names it.
        """
        collected = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(minutes=1)).isoformat(timespec="seconds")
        self.seed([self.ticket("LOG-10", "Ten")], collected_at=collected,
                  heartbeat={"ok": False, "reason": "boom", "truncated": False})
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        block = self.section(completed.stdout)
        self.assertEqual('  Key     State  Reason    Title\n'
                         '  ------  -----  --------  ----------------\n'
                         '  LOG-10  open   assigned  "Ten"\n'
                         f"  external context: degraded; last successful sync {collected}\n", block)
        self.assertNotIn("never collected", block)
        freshness = self.report()["jira"]["freshness"]
        self.assertEqual((freshness["state"], freshness["last_success_at"]), ("degraded", collected))

    def test_json_keeps_every_row_with_when_jira_updated_it(self) -> None:
        """(d) pack sd:5357: the window is the text's; `--json` drops no row, however long unseen."""
        self.seed([self.ticket("LOG-8", "Eight days unseen", state="closed", age_days=8,
                               updated_at="2026-07-01T00:00:00Z"),
                   self.ticket("LOG-6", "Never dated", state="closed", updated_at=None)])
        rows = {row["url"].rpartition("/")[2]: row for row in self.report()["jira"]["rows"]}
        self.assertEqual(sorted(rows), ["LOG-6", "LOG-8"])
        self.assertEqual(rows["LOG-8"]["updated_at"], "2026-07-01T00:00:00Z")
        self.assertIsNone(rows["LOG-6"]["updated_at"])

    def test_only_an_assigned_or_filed_row_is_listed_and_json_keeps_every_row(self) -> None:
        """Ruling on sd:5357: a watched row, or one with no reason yet, is not the operator's worklist."""
        self.seed([self.ticket("LOG-11", "Mine"), self.ticket("LOG-12", "Filed", reason="filed"),
                   self.ticket("LOG-13", "Watched", reason="watched"), self.ticket("LOG-14", "Unknown", reason=None)])
        result = self.report()
        self.assertEqual(sorted((row["url"].rpartition("/")[2], row["reason"]) for row in result["jira"]["rows"]),
                         [("LOG-11", "assigned"), ("LOG-12", "filed"), ("LOG-13", "watched"), ("LOG-14", None)])
        block = self.section(self.run_tool(SD_STATUS).stdout, "2")
        self.assertRegex(block, r'\n  LOG-11 +open +assigned +"Mine"\n')
        self.assertRegex(block, r'\n  LOG-12 +open +filed +"Filed"\n')
        self.assertNotIn("LOG-13", block)
        self.assertNotIn("LOG-14", block)

    def test_past_ten_rows_the_rest_is_a_count(self) -> None:
        """Ruling on sd:5357: ten rows, then `… N more`; the heading keeps the whole count."""
        self.seed([self.ticket(f"LOG-{number}", f"Ticket {number}") for number in range(100, 112)])
        block = self.section(self.run_tool(SD_STATUS).stdout, "12")
        self.assertEqual(10, len(re.findall(r"\n  LOG-1[0-9]{2} ", block)), block)
        self.assertIn("\n  … 2 more\n", block)

    def test_the_text_lists_rows_jira_updated_in_the_last_thirty_days_open_first(self) -> None:
        """pack sd:5357: open or closed, a row Jira updated inside the window is listed, open rows first;
        one updated before it, or never dated, is hidden and counted on one line naming the key."""
        self.seed([self.ticket("LOG-3", "Done lately", state="closed", updated_at=self.updated(2)),
                   self.ticket("LOG-4", "Live"),
                   self.ticket("LOG-5", "Bulk touched", updated_at=self.updated(52)),
                   self.ticket("LOG-6", "Never dated", updated_at=None)])
        block = self.section(self.run_tool(SD_STATUS).stdout, "2")
        self.assertRegex(block, r'\n  LOG-4 +open +assigned +"Live"\n  LOG-3 +closed +assigned +"Done lately"\n')
        self.assertNotIn("LOG-5", block)
        self.assertNotIn("LOG-6", block)
        self.assertIn("\n  2 hidden: not updated in the last 30 days, or never dated; `sd.jira_recent_days` sets 30\n",
                      block)

    def test_sd_jira_recent_days_sets_the_window(self) -> None:
        self.configure("60")
        self.seed([self.ticket("LOG-5", "Bulk touched", updated_at=self.updated(52)),
                   self.ticket("LOG-7", "Older", updated_at=self.updated(61))])
        block = self.section(self.run_tool(SD_STATUS).stdout, "1")
        self.assertIn("LOG-5", block)
        self.assertIn("\n  1 hidden: not updated in the last 60 days, or never dated; `sd.jira_recent_days` sets 60\n",
                      block)

    def test_a_bad_sd_jira_recent_days_is_refused_by_name(self) -> None:
        for days in ("0", "-3", "thirty"):
            with self.subTest(days=days):
                self.configure(days)
                self.seed([self.ticket("LOG-4", "Live")])
                completed = self.run_tool(SD_STATUS)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                block = self.section(completed.stdout, "unknown")
                self.assertIn("sd.jira_recent_days", block)
                self.assertIn("a whole number of days, 1 or more", block)
                self.assertNotIn("LOG-4", block)

    def test_a_title_with_terminal_controls_is_one_quoted_line(self) -> None:
        """(e) The same rule `_render_contributions` is tested against.

        A Jira summary is external text. Unquoted, the newline would print a
        fifteenth heading and the escape would clear the terminal.
        """
        self.seed([self.ticket("LOG-5", "a\x1b[2Jb\nprotection")])
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertRegex(self.section(completed.stdout), r'\n  LOG-5 +open +assigned +"a\\u001b\[2Jb\\nprotection"\n')
        self.assertNotIn("\x1b", completed.stdout)
        headings = self.headings(completed.stdout)
        self.assertEqual(headings, ReportSectionTests.ORDER)
        self.assertEqual(headings.count("Protection"), 1)

    def test_the_json_report_carries_the_section_and_its_rows(self) -> None:
        """(f) Asserted on the parsed object, not on the text."""
        self.seed([self.ticket("LOG-7", "Seven")])
        result = self.report()
        section = result["jira"]
        self.assertTrue(section["available"])
        self.assertEqual(section["reason"], "")
        self.assertEqual(section["freshness"]["tracker"], "jira")
        self.assertIsNone(section["freshness"]["last_success_at"])
        [row] = section["rows"]
        for key in ("url", "state", "title", "last_seen"):
            self.assertIn(key, row)
        self.assertEqual((row["url"].rpartition("/")[2], row["state"], row["title"]), ("LOG-7", "open", "Seven"))

    def test_the_issues_section_still_says_no_github_remote(self) -> None:
        """(g) The control: the fourteenth section changed nothing about the ninth."""
        self.seed([self.ticket("LOG-9", "Nine")])
        result = self.report()
        self.assertEqual(result["issues"], {"available": False, "reason": "no GitHub remote",
                                            "needs_you": [], "other": []})
        completed = self.run_tool(SD_STATUS)
        self.assertIn("\nIssues, this repo, shared database (unknown)\n  ! no GitHub remote\n", completed.stdout)
        self.assertIn("LOG-9", self.section(completed.stdout))

    def test_no_shared_database_is_a_reported_gap(self) -> None:
        """The same gate `_database_issues` uses, in the same shape the other sections report."""
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        reason = f"no shared sd_db database yet; {status.NO_DATABASE_REMEDY}"
        self.assertEqual(f"  ! {reason}\n", self.section(completed.stdout, "unknown"))
        section = self.report()["jira"]
        self.assertEqual(section, {"available": False, "reason": reason, "freshness": None, "rows": []})


class ReadOnlyTests(StatusFixture):
    def test_nothing_under_the_temp_root_changes(self) -> None:
        self.with_github(pulls=[])
        self.item("2026-08-01-alpha")
        (self.repo / ".github" / "workflows").mkdir(parents=True)
        (self.repo / ".github" / "workflows" / "tests.yml").write_text(
            WORKFLOW, encoding="utf-8"
        )
        # SQLite mode=ro may maintain these two lock files for a WAL database.
        # Main database bytes and every repository file still must be unchanged.
        lock_files = {"home/.local/share/sd/sd.db-wal", "home/.local/share/sd/sd.db-shm"}

        def snapshot():
            return {path: value for path, value in tree_digest(self.base).items()
                    if path not in lock_files}

        before = snapshot()
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(snapshot(), before)

    def test_the_state_directory_is_never_created(self) -> None:
        self.with_github(pulls=[])
        self.run_tool(SD_STATUS)
        self.run_tool(SD_STATUS, "--json")
        self.assertFalse(self.state.exists())

    def test_the_working_tree_stays_clean(self) -> None:
        self.with_github(pulls=[])
        self.run_tool(SD_STATUS)
        self.assertEqual(self.git("status", "--porcelain").strip(), "")

    def test_the_tools_own_directory_is_not_written_to_either(self) -> None:
        """Importing a sibling module must not leave a `__pycache__` in bin/.

        The other read-only tests watch the *repository*, which is exactly
        where a tool that writes its own directory would go unnoticed. So this
        one runs a copy of bin/ and watches the copy.
        """
        tools = self.base / "tools"
        shutil.copytree(BIN, tools, ignore=shutil.ignore_patterns("__pycache__"))
        self.with_github(pulls=[])
        before = tree_digest(tools)
        for tool in ("sd-status", "sd-pr-state"):
            completed = self.run_tool(tools / tool)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(tree_digest(tools), before)
        self.assertFalse((tools / "__pycache__").exists())


class InventoryFixture(StatusFixture):
    """The inventory producer, exercised in process against a fixture repo.

    In process rather than through the executable, because nothing renders the
    inventory yet: step 1 of this item lands the producer and the report is
    unchanged, so a subprocess assertion would have nothing to look at. The
    subprocess tests above still bracket the report, which is what says the
    producer stayed invisible.
    """

    #: Every age in this class is measured from one fixed day, never from the
    #: wall clock. `actionable_inventory` defaults `today` to `date.today()`,
    #: and an unpinned fixture drifts twice over: two calls that straddle
    #: midnight can order their rows differently, and a fixture item dated
    #: `2026-08-01` silently crosses `IDLE_DAYS` as real time passes and starts
    #: producing an `idle-planning` row no assertion here expects.
    TODAY = datetime.date(2026, 9, 7)

    #: A `protection` section that says GitHub could not be read at all, which
    #: is what puts tier 2 out of reach and the merge class into `unchecked`.
    BLIND = {
        "default_branch": "main",
        "gaps": [],
        "detail": {},
        "available": False,
        "reason": "gh is not installed",
    }

    def inventory(self, **overrides: Any) -> Any:
        return status.actionable_inventory(
            self.repo, self.sections(**overrides), self.TODAY
        )

    def branch(self, name: str, *, land: bool) -> None:
        """A real branch off `main`, squash-merged back or left standing.

        Committed before any item file exists, so `git add -A` cannot sweep a
        `docs/work` fixture into the branch and change what the diff sees.
        """
        self.git("checkout", "-q", "-b", name)
        (self.repo / f"{name}.txt").write_text("work\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"work on {name}")
        self.git("checkout", "-q", "main")
        if land:
            self.git("merge", "-q", "--squash", name)
            self.git("commit", "-q", "-m", f"squash {name}")

    def pull(self, **overrides: Any) -> dict[str, Any]:
        """A row shaped exactly as `sd-pr-state`'s `describe` returns one."""
        row = {
            "number": 7,
            "title": "One",
            "url": "https://github.com/acme/widget/pull/7",
            "head": "main",
            "base": "main",
            "draft": False,
            "mergeable": "MERGEABLE",
            "merge_state": "BLOCKED",
            "review_decision": "NONE",
            "checks": {"failure": 1},
            "checks_total": 1,
            "failing": ["lint"],
            "behind_by": 0,
        }
        row.update(overrides)
        return row

    def sections(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "work": status.work_section(self.repo),
            "pull_requests": {"repo": "acme/widget", "pull_requests": []},
            "protection": {"default_branch": "main", "gaps": [], "detail": {}},
            "issues": {"available": False, "needs_you": [], "other": []},
            "lane": {"available": True, "reason": "", "entries": []},
        }
        base.update(overrides)
        return base

    def rows(self, **overrides: Any) -> list[dict[str, Any]]:
        return status.actionable_inventory(
            self.repo, self.sections(**overrides), self.TODAY
        ).rows

    def by_check(self, rows: list[dict[str, Any]], check: str) -> list[dict[str, Any]]:
        return [row for row in rows if row["check"] == check]


class ClassTableTests(unittest.TestCase):
    """`CLASSES` is the enumeration, so the enumeration is what gets asserted."""

    def test_every_check_name_appears_exactly_once(self) -> None:
        names = [kind.check for kind in status.CLASSES]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), len(status.BY_CHECK))

    def test_the_table_carries_the_checks_the_design_enumerates(self) -> None:
        # Pinned as a set, not a count: a count passes when a check is renamed
        # into a duplicate of another, which is the drift this table exists to
        # make impossible.
        self.assertEqual(
            {kind.check for kind in status.CLASSES},
            {
                "branch-already-merged", "in-progress-without-branch",
                "branch-unresolvable", "status-unreadable", "unresolved-concern",
                "pr-check-failing", "pr-check-missing", "dirty-tree-with-open-pr",
                "protection-gap", "accepted-gap-standing",
                "pr-needs-action", "unmerged-branch",
                "parked-concern", "idle-planning", "undated-planning",
                "source-marker", "unreadable-concern-row",
                "undisclosed-tool", "mirror-sync-pending", "lane-entry-failed",
            },
        )

    def test_a_class_letter_is_one_lowercase_character(self) -> None:
        for kind in status.CLASSES:
            self.assertRegex(kind.letter, r"^[a-z]$")
            self.assertGreater(kind.rank, 0)
            self.assertTrue(kind.source and kind.what)

    def test_the_exclusions_are_stated_in_words_and_not_only_in_the_design(self) -> None:
        """`design.md:310` requires the report to print what it skipped."""
        self.assertTrue(status.EXCLUDED)
        for sentence in status.EXCLUDED:
            self.assertIsInstance(sentence, str)
            self.assertGreater(len(sentence.split()), 5)
        joined = " ".join(status.EXCLUDED)
        # `parked` left this list with the field, cut by 31(a) on 2026-09-19.
        # The archive sentence is what excludes every item that carried one.
        for skipped in ("archive", "Jira", "review-learnings.md"):
            self.assertIn(skipped, joined)
        self.assertNotIn("parked", joined)


class ActionIdTests(unittest.TestCase):
    def test_two_checks_on_one_object_get_two_ids(self) -> None:
        """C-11's regression, at the level of the formula.

        A pull request can be `pr-check-failing` and `dirty-tree-with-open-pr`
        at once. Both are class `p`, so the rejected letter-keyed formula gives
        them one hash -- at four digits and at eight, which is why widening was
        no answer to it. Keyed on the check name they differ.
        """
        key = "acme/widget!7"
        pair = ("pr-check-failing", "dirty-tree-with-open-pr")
        rejected = {
            hashlib.sha1(f"p\0{key}".encode(), usedforsecurity=False).hexdigest()[:4]
            for _ in pair
        }
        self.assertEqual(len(rejected), 1, "the rejected formula collides, as C-11 says")
        self.assertEqual(len({status.action_id(check, key) for check in pair}), 2)

    def test_the_letter_is_a_display_prefix_and_the_hash_is_hex(self) -> None:
        for check, kind in status.BY_CHECK.items():
            found = status.action_id(check, "some/key")
            self.assertRegex(found, r"^[a-z][0-9a-f]{4}$")
            self.assertEqual(found[0], kind.letter)

    def test_the_same_data_gives_the_same_id_every_time(self) -> None:
        first = status.action_id("parked-concern", "alpha/prd.md#Steps#do it#0")
        second = status.action_id("parked-concern", "alpha/prd.md#Steps#do it#0")
        self.assertEqual(first, second)
        self.assertNotEqual(
            first, status.action_id("parked-concern", "alpha/prd.md#Steps#do it#1")
        )

    def test_a_check_the_table_does_not_carry_is_an_error_not_an_id(self) -> None:
        with self.assertRaises(KeyError):
            status.action_id("invented-check", "k")

    def test_colliding_rows_both_widen_to_eight_digits_and_say_so(self) -> None:
        rows = [
            status._row("parked-concern", "a", "a", "", ""),
            status._row("parked-concern", "b", "b", "", ""),
        ]
        rows[1]["id"] = rows[0]["id"]
        status._widen_collisions(rows)
        self.assertEqual(len({row["id"] for row in rows}), 2)
        for row in rows:
            self.assertRegex(row["id"], r"^[a-z][0-9a-f]{8}$")
            self.assertTrue(row["widened"])


class InventoryShapeTests(InventoryFixture):
    def test_every_row_carries_the_fields_the_three_sections_read(self) -> None:
        self.item("2026-08-01-alpha", status="in_progress")
        for row in self.rows():
            self.assertEqual(set(row), {
                "id", "check", "letter", "key", "title", "detail", "suggest",
                "rank", "abnormal", "source", "age_days", "widened",
            })
            kind = status.BY_CHECK[row["check"]]
            self.assertEqual((row["rank"], row["abnormal"], row["source"]),
                             (kind.rank, kind.abnormal, kind.source))

    def test_the_ids_are_a_pure_function_of_the_data(self) -> None:
        """`implement.md`'s verification 5, run twice against one tree."""
        self.item("2026-08-01-alpha", status="in_progress")
        self.item("2026-08-02-beta")
        sections = self.sections()
        first = status.actionable_inventory(self.repo, sections, self.TODAY).rows
        second = status.actionable_inventory(self.repo, sections, self.TODAY).rows
        self.assertTrue(first)
        self.assertEqual([row["id"] for row in first], [row["id"] for row in second])
        self.assertEqual(len({row["id"] for row in first}), len(first))

    def test_rows_come_out_ranked_by_class_then_age_then_id(self) -> None:
        self.item("2026-08-01-alpha", status="in_progress")
        rows = self.rows()
        ordered = sorted(rows, key=lambda r: (r["rank"], -r["age_days"], r["id"]))
        self.assertEqual(rows, ordered)

    def test_the_report_now_carries_what_steps_one_to_three_produced(self) -> None:
        """Retires `test_nothing_renders_it_yet`, which step 4 falsified.

        Steps 1 to 3b built producers deliberately wired to nothing, and that
        test asserted the report was unchanged -- true then, and the check
        that said each step was landable on its own. Step 4 is the step that
        makes them visible, so the assertion inverts here rather than being
        loosened. `ReportSectionTests` pins the order and the wording; this
        one pins only that the executable an operator runs shows them at all,
        which the in-process tests cannot say.

        The `actions` key was asserted absent here until step 5 added it,
        which is the same inversion one step later and for the same reason.
        `ActionsCliTests` owns that surface now.
        """
        self.with_github(pulls=[])
        self.item("2026-08-01-alpha", status="in_progress")
        completed = self.run_tool(SD_STATUS)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for present in ("Abnormalities", "Pending", "Next", "Open threads"):
            self.assertIn(present, self.headings(completed.stdout))
        self.assertIn("\nNext (1)\n", completed.stdout)


class WorkItemInventoryTests(InventoryFixture):
    def test_in_progress_with_no_branch_is_a_finding(self) -> None:
        self.item("2026-08-01-alpha", status="in_progress")
        found = self.by_check(self.rows(), "in-progress-without-branch")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["abnormal"])
        self.assertIn("2026-08-01-alpha", found[0]["detail"])
        # The branch is a column on the row; no frontmatter line holds it.
        self.assertNotIn("prd.md", found[0]["suggest"])
        self.assertIn("sd task status", found[0]["suggest"])

    def test_a_branch_no_ref_carries_is_a_finding_and_a_live_one_is_not(self) -> None:
        self.item("2026-08-01-alpha", status="in_progress", extra="branch: gone/away\n")
        self.item("2026-08-02-beta", status="in_progress", extra="branch: here/now\n")
        self.git("branch", "here/now")
        found = self.by_check(self.rows(), "branch-unresolvable")
        self.assertEqual([row["title"] for row in found], ["alpha"])
        self.assertEqual(found[0]["key"], "2026-08-01-alpha")
        self.assertNotIn("prd.md", found[0]["suggest"])
        self.assertIn("sd task edit", found[0]["suggest"])
        self.assertIn("--branch NAME", found[0]["suggest"])
        self.assertNotIn("--clear-branch", found[0]["suggest"])

    def test_a_done_items_deleted_branch_is_no_finding(self) -> None:
        """A merge deletes the squashed branch, so a done item's branch is history (sd:2729)."""
        self.item("2026-08-01-alpha", status="done", extra="branch: gone/away\n")
        self.assertEqual([], self.by_check(self.rows(), "branch-unresolvable"))

    def ancient(self, name: str = "2026-01-01-ancient") -> pathlib.Path:
        """A planning item whose own date is 249 days before `TODAY`.

        Uncommitted and unrecorded, so the only thing dating it is the date it
        wrote on itself. Every idle test below starts here and then adds one
        piece of evidence that something has happened to it since.
        """
        directory = self.repo / "docs" / "work" / name
        directory.mkdir(parents=True)
        (directory / "prd.md").write_text(
            f"---\ntitle: {name}\ncreated: 2026-01-01\n---\n",
            encoding="utf-8",
        )
        self.row(directory, "planning", stamp="2026-01-01T00:00:00+00:00")
        return directory

    def committed(self, when: str) -> None:
        """Commit the item tree with a committer date this test chooses.

        `sd_lib.touched` reads `%cs`, the *committer* date -- when the work
        entered this history rather than when it was first written, which is
        the question "has anything happened to this item" actually asks. Git
        takes it from the environment and from nowhere else, so a fixture that
        did not set it would be measuring the wall clock.
        """
        with mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": when}):
            self.git("add", "-A")
            self.git("commit", "-q", "-m", "record the items")

    def test_the_threshold_is_read_from_the_library_and_still_says_45(self) -> None:
        """R10-D1's number lives in `sd_lib` now that the sweep is cut.

        `IDLE_DAYS` used to read the sweep module's constant, so the report and the
        sweep could not disagree about what idle meant. The sweep is gone
        (sd:10, criterion 21) and the constant moved with the aging basis into
        `sd_lib`, where this is the one reader left. Both halves are asserted:
        that the name is the library's, not a restated literal here, and that
        the move did not change the number. The name is checked in the
        source, because `assertIs` on two small integers is true whether or
        not one was read from the other.
        """
        self.assertEqual(status.IDLE_DAYS, 45)
        self.assertEqual(status.IDLE_DAYS, status.sd_lib.DEFAULT_DAYS)
        assignments = [
            ast.unparse(node.value)
            for node in ast.walk(ast.parse(SD_STATUS.read_text(encoding="utf-8")))
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "IDLE_DAYS" for t in node.targets)
        ]
        self.assertEqual(assignments, ["sd_lib.DEFAULT_DAYS"])

    def test_a_planning_item_past_the_threshold_ages_into_a_finding(self) -> None:
        """`created:` is what ages an item, so the fixture writes its own.

        The control for the two tests under it: nothing has been committed and
        nothing recorded, so birth date and last activity are the same day and
        this fires either way. It passed before `idle-planning` changed its
        basis and passes after, which is what makes the two failures below
        evidence about the basis rather than about the fixture.
        """
        self.ancient()
        self.item("2026-08-01-alpha")
        rows = status.actionable_inventory(self.repo, self.sections(), self.TODAY).rows
        idle = self.by_check(rows, "idle-planning")
        self.assertEqual([row["key"] for row in idle], ["2026-01-01-ancient"])
        self.assertGreater(idle[0]["age_days"], status.IDLE_DAYS)

    def test_an_ancient_item_committed_to_this_week_is_not_idle(self) -> None:
        """sd:455: an item worked on two days ago is not one nobody has touched.

        The defect, in one item. `idle-planning` aged an item by the date it
        dated itself, so the number it printed could only ever go up: 21 of
        example-benchmark's 24 planning items read past the threshold on birth
        dates while every one of them had been triaged live the day before.
        Its finding says the item has been planning for N days and its remedy
        is start it, park it or archive it -- all three of which assume
        neglect, and acting on them would have buried confirmed-live work.

        Fails against the old basis, which cannot see the commit at all.
        """
        self.ancient()
        self.committed("2026-09-05T12:00:00 +0000")
        rows = status.actionable_inventory(self.repo, self.sections(), self.TODAY).rows
        self.assertEqual([], self.by_check(rows, "idle-planning"))

    def test_a_stamp_only_the_database_carries_resets_the_clock(self) -> None:
        """The other half of sd:455, and the half git cannot answer.

        A `row` checkout records a triage decision as a note against the item
        and touches no file, so the item tree is byte-identical before and
        after -- which is why this fixture commits nothing. The four decision
        notes on example-benchmark's items were exactly this shape.

        The stamp arrives on the work section, where `work_section` puts it
        from `WorkItem.activity`; `RowActivity` in `tests/test_sd_lib.py` is
        what checks the database actually fills it.
        """
        self.ancient()
        work = status.work_section(self.repo)
        for entry in work["items"]:
            entry["activity"] = "2026-09-06T09:14:00+00:00"
        self.assertEqual([], self.by_check(self.rows(work=work), "idle-planning"))

    def test_the_row_says_what_the_age_measures_rather_than_how_old_it_is(
        self,
    ) -> None:
        """The wording is the finding: "planning for N days" was the false half.

        An age measured from activity and described as an age since creation
        would be a second way of saying the wrong thing, so the detail line is
        pinned here rather than left to read like the old one.
        """
        self.ancient()
        rows = status.actionable_inventory(self.repo, self.sections(), self.TODAY).rows
        detail = self.by_check(rows, "idle-planning")[0]["detail"]
        self.assertIn("has had nothing recorded against it for 249 days", detail)

    def test_a_planning_item_with_no_date_anywhere_is_its_own_finding(self) -> None:
        directory = self.repo / "docs" / "work" / "undated-thing"
        directory.mkdir(parents=True)
        (directory / "prd.md").write_text(
            "---\ntitle: undated\nstatus: planning\n---\n\n# undated\n",
            encoding="utf-8",
        )
        rows = status.actionable_inventory(self.repo, self.sections(), self.TODAY).rows
        (row,) = self.by_check(rows, "undated-planning")
        self.assertEqual(row["title"], "undated-thing")
        # The age sweep was cut; the repair names the reader that ages it.
        self.assertIn("so idle-planning can age it", row["suggest"])
        self.assertNotIn("prd.md", row["suggest"])

    def test_archived_items_contribute_no_rows(self) -> None:
        archived = self.repo / "docs" / "work" / "archive" / "2026-08"
        archived.mkdir(parents=True)
        (archived / "2026-01-01-old").mkdir()
        (archived / "2026-01-01-old" / "prd.md").write_text(
            PRD.format(title="old", status="planning", extra=""), encoding="utf-8"
        )
        rows = status.actionable_inventory(self.repo, self.sections(), self.TODAY).rows
        self.assertEqual([], [row for row in rows if "old" in row["key"]])

    def test_an_archived_item_that_is_branched_and_carries_a_parked_line(
        self,
    ) -> None:
        """The real case: `archived` suppresses, and the cut field does not.

        `archive/2026-09/2026-08-21-port-integration-only-profile` carries
        `status: in_progress`, a `parked:` line, a `branch:` field and an
        `archive/` path all at once. Until 31(a) that was an intersection of
        two suppressors, and this test held it to prove the reader handled
        both rather than double-counting. `parked` is cut, so the archive
        path is now the only suppressor, and the `parked:` line is inert
        text that must change nothing.

        **The frontmatter's `in_progress` is not what the reader sees.** With
        no row for an archived item the reader answers from where it lives,
        and since sd:2729 no check fires on a done item. A database holding
        the archived item's row may say it is open; the direct `_work_rows`
        call at the end is that case, and it fails with the guard deleted.

        The two live items are the contrast that makes the rest able to fail.
        One of them carries the cut `parked:` line and must fire exactly like
        the one that does not: a reader still consulting the field would drop
        it, and that is the regression this asserts.
        """
        self.item("2026-08-01-live", status="in_progress",
                  extra="branch: feat/nope\n")
        self.item("2026-08-02-parked", status="in_progress",
                  extra="parked: 2026-09-01 superseded\nbranch: feat/nope\n")
        archive = self.repo / "docs" / "work" / "archive" / "2026-09"
        for name, extra in (
            ("2026-08-03-filed", "branch: feat/nope\n"),
            ("2026-08-04-port", "parked: 2026-09-01 superseded\nbranch: feat/nope\n"),
        ):
            directory = archive / name
            directory.mkdir(parents=True)
            (directory / "prd.md").write_text(
                PRD.format(title=name, status="in_progress", extra=extra),
                encoding="utf-8",
            )
        rows = status.actionable_inventory(self.repo, self.sections(), self.TODAY).rows
        self.assertEqual(
            ["2026-08-01-live", "2026-08-02-parked"],
            sorted({row["key"] for row in rows if "2026-08-0" in row["key"]}),
            "both live items fire, including the one whose `parked:` line "
            "nothing reads any more",
        )
        work = {"items": [
            {"path": "docs/work/archive/2026-09/2026-08-03-filed", "slug": "filed",
             "status": "in_progress", "branch": "feat/nope", "archived": True}]}
        with mock.patch.object(status.sd_lib, "upstream", return_value=(None, "main")):
            archived = status._work_rows(self.repo, work, self.TODAY)
        self.assertEqual([], archived, "an archived row is suppressed whatever its status")
        self.assertEqual(
            len(rows), len({(row["check"], row["key"]) for row in rows}),
            "no object may produce the same check twice",
        )


class ItemDateParityTests(unittest.TestCase):
    """`_item_date` here and `sd_lib.item_date` read one date the same way (sd:1000).

    Each held its own pattern: the library accepted `created: 20260701` and
    a padded value, and this file did not, so one item could be undated in
    one report and dated in the other.
    """

    CREATED = ("2026-07-01", " 2026-07-01", "2026-07-01T10:00", "20260701", "soon", "", "2026-13-45")
    NAMES = ("2026-01-01-x", "2026-01-01", "untitled", "2026-13-45-x", "20260101-x")

    def test_both_readers_give_the_same_date(self) -> None:
        for created, name in itertools.product(self.CREATED, self.NAMES):
            with self.subTest(created=created, name=name):
                item = SimpleNamespace(created=created, path=pathlib.Path("docs/work") / name)
                entry = {"created": created, "path": f"docs/work/{name}"}
                self.assertEqual(status._item_date(entry), status.sd_lib.item_date(item))

    def test_one_pattern_is_shared(self) -> None:
        self.assertIs(status._ITEM_DATE_RE, status.sd_lib.ITEM_DATE_RE)


class PullRequestInventoryTests(InventoryFixture):
    def test_two_checks_firing_on_one_pull_request_are_two_rows(self) -> None:
        """C-11's regression at the level of the inventory, not the formula."""
        (self.repo / "scratch.txt").write_text("dirty\n", encoding="utf-8")
        pulls = {"repo": "acme/widget", "pull_requests": [self.pull()]}
        rows = self.by_check(self.rows(pull_requests=pulls), "pr-check-failing")
        rows += self.by_check(
            self.rows(pull_requests=pulls), "dirty-tree-with-open-pr"
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["id"] for row in rows}), 2)
        self.assertEqual({row["key"] for row in rows}, {"acme/widget!7"})

    def test_a_pull_request_with_no_check_against_a_branch_that_requires_one(self) -> None:
        pulls = {
            "repo": "acme/widget",
            "pull_requests": [self.pull(failing=[], checks_total=0, checks={})],
        }
        protection = {
            "default_branch": "main", "gaps": [],
            "detail": {"required_contexts": ["Tests"]},
        }
        rows = self.rows(pull_requests=pulls, protection=protection)
        self.assertEqual(len(self.by_check(rows, "pr-check-missing")), 1)
        self.assertEqual(
            [], self.by_check(self.rows(pull_requests=pulls), "pr-check-missing")
        )

    def test_a_draft_is_not_waiting_on_anybody(self) -> None:
        pulls = {
            "repo": "acme/widget",
            "pull_requests": [self.pull(draft=True, failing=[])],
        }
        self.assertEqual([], self.by_check(self.rows(pull_requests=pulls),
                                           "pr-needs-action"))


class AdapterInventoryTests(InventoryFixture):
    def test_a_protection_gap_becomes_an_addressable_row(self) -> None:
        protection = {
            "default_branch": "main",
            "gaps": [{"id": "enforce_admins", "gap": "enforce_admins is off"}],
            "detail": {},
        }
        found = self.by_check(self.rows(protection=protection), "protection-gap")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["key"], "main#enforce_admins")
        self.assertIn(status.ACKNOWLEDGEMENT_SOURCE, found[0]["suggest"])

    def test_an_issue_in_the_section_is_context_not_a_row(self) -> None:
        """sd:3497: the two issue classes could never fire from the shared database; they are gone."""
        issues = {
            "available": True,
            "needs_you": [{
                "repo": "acme/widget", "number": 4, "title": "Fix it",
                "state": "open", "updated_at": "2026-09-01", "why": ["assigned"],
                "url": "https://github.com/acme/widget/issues/4",
            }],
            "other": [],
        }
        self.assertEqual([], [row for row in self.rows(issues=issues) if row["check"].startswith("issue-")])
        self.assertNotIn("issue-open", status.BY_CHECK)

    def test_a_branch_on_origin_with_no_pull_request_is_an_open_thread(self) -> None:
        remote = self.base / "origin.git"
        self.git("init", "-q", "--bare", str(remote))
        self.set_origin(str(remote))
        self.git("branch", "task/one")
        self.git("push", "-q", "origin", "main", "task/one")
        self.git("fetch", "-q", "origin")
        found = self.by_check(self.rows(), "unmerged-branch")
        self.assertEqual([row["key"] for row in found], ["origin/task/one"])
        carried = {
            "repo": "acme/widget",
            "pull_requests": [self.pull(head="task/one")],
        }
        self.assertEqual(
            [], self.by_check(self.rows(pull_requests=carried), "unmerged-branch")
        )

    def test_a_branch_origin_no_longer_carries_is_not_a_row(self) -> None:
        """sd:496: the ghost, and why `fetch --prune` is not the cure here.

        A remote-tracking ref outlives its branch the moment a pull request is
        squash-merged with `--delete-branch` by anything but this checkout.
        Observed on `origin/worktree-agent-a429ba188d801962d` after #839
        landed: `git ls-remote --heads origin <name>` printed nothing, and both
        actions the row named -- open a pull request for it, or delete it --
        had nothing to act on.

        `sd-status` cannot prune its way out, because pruning writes refs and
        this tool writes nothing, so it asks origin instead. The branch is
        deleted **in the bare repository directly** rather than with
        `git push --delete`, which removes the remote-tracking ref here as a
        side effect and would leave this fixture with no ghost to find.

        Fails before the cross-check, which reports `origin/task/gone`.
        """
        remote = self.base / "ghost.git"
        self.git("init", "-q", "--bare", str(remote))
        self.set_origin(str(remote))
        self.git("branch", "task/gone")
        self.git("push", "-q", "origin", "main", "task/gone")
        self.git("fetch", "-q", "origin")
        self.git("update-ref", "-d", "refs/heads/task/gone", cwd=remote)

        self.assertIn(
            "origin/task/gone",
            self.git("for-each-ref", "--format=%(refname:short)", "refs/remotes"),
            "the fixture is meant to leave a stale remote-tracking ref standing",
        )
        self.assertEqual([], self.by_check(self.rows(), "unmerged-branch"))

    def test_a_remote_that_cannot_be_asked_says_so_in_the_row(self) -> None:
        """Option (b), kept as the fallback rather than dropped for option (a).

        Offline, the refs are the only answer there is, and reporting nothing
        would trade a ghost for a blind spot. The row is still emitted and says
        what it was built from, and its suggestion names the prune ahead of the
        two actions that are unrunnable against a branch already gone.

        `unchecked` is not the vehicle: `unmerged-branch` is not abnormal and
        `banner` reports `unchecked` for abnormal classes only, so a reason
        recorded there is a reason nothing prints.
        """
        remote = self.base / "away.git"
        self.git("init", "-q", "--bare", str(remote))
        self.set_origin(str(remote))
        self.git("branch", "task/one")
        self.git("push", "-q", "origin", "main", "task/one")
        self.git("fetch", "-q", "origin")
        shutil.rmtree(remote)

        found = self.by_check(self.rows(), "unmerged-branch")
        self.assertEqual([row["key"] for row in found], ["origin/task/one"])
        self.assertIn(status.STALE_REFS, found[0]["detail"])
        self.assertIn("git fetch --prune", found[0]["suggest"])


class LaneTests(InventoryFixture):
    """sd:3495: the merge lane is the one merge path, and the report knew nothing of it."""

    PULL = {"failing": [], "checks_total": 0, "checks": {}, "head": "task/queued"}
    REQUIRED = {"default_branch": "main", "gaps": [], "detail": {"required_contexts": ["sd/local-gate"]}}

    def lane(self, *entries: dict[str, Any]) -> dict[str, Any]:
        return {"available": True, "reason": "", "repository": "acme/widget", "entries": list(entries)}

    def test_a_failed_lane_entry_is_an_abnormal_row(self) -> None:
        lane = self.lane({"item": 12, "branch": "task/twelve", "status": "failed", "step": "prepare",
                          "reason": "the gate failed"})
        found = self.by_check(self.rows(lane=lane), "lane-entry-failed")
        self.assertEqual([row["key"] for row in found], ["sd:12"])
        self.assertTrue(found[0]["abnormal"])
        self.assertIn("failed at prepare: the gate failed", found[0]["detail"])
        self.assertIn("sd-ship lane retry 12", found[0]["suggest"])
        pending = self.lane({"item": 13, "branch": "task/thirteen", "status": "pending"})
        self.assertEqual([], self.by_check(self.rows(lane=pending), "lane-entry-failed"))

    def test_an_unreadable_lane_is_unchecked_not_clear(self) -> None:
        inventory = self.inventory(lane={"available": False, "reason": "the hub is down", "entries": []})
        self.assertEqual(inventory.unchecked["lane-entry-failed"], "the hub is down")

    def test_a_queued_pull_request_waits_on_the_lane_not_on_anyone(self) -> None:
        pulls = {"repo": "acme/widget", "pull_requests": [self.pull(**self.PULL)]}
        lane = self.lane({"item": 14, "branch": "task/queued", "status": "pending"})
        rows = self.rows(pull_requests=pulls, protection=self.REQUIRED, lane=lane)
        self.assertEqual([], self.by_check(rows, "pr-check-missing"))
        self.assertEqual([], self.by_check(rows, "pr-needs-action"))
        rows = self.rows(pull_requests=pulls, protection=self.REQUIRED)
        self.assertEqual(len(self.by_check(rows, "pr-check-missing")), 1)

    def test_local_ci_suggestions_name_the_gate_not_workflows(self) -> None:
        pulls = {"repo": "acme/widget", "pull_requests": [
            self.pull(**self.PULL), self.pull(number=8, head="task/failing")]}
        self.enterContext(mock.patch.object(status.sd_lib, "ci_mode", return_value="local"))
        rows = self.rows(pull_requests=pulls, protection=self.REQUIRED)
        missing = self.by_check(rows, "pr-check-missing")[0]["suggest"]
        failing = self.by_check(rows, "pr-check-failing")[0]["suggest"]
        self.assertIn("sd/local-gate at prepare", missing)
        self.assertNotIn("workflows", missing)
        self.assertIn("sd-ship lane retry", failing)
        self.assertNotIn("re-run the failing checks", failing)

    def test_a_branch_the_lane_holds_is_not_an_unmerged_branch(self) -> None:
        remote = self.base / "origin.git"
        self.git("init", "-q", "--bare", str(remote))
        self.set_origin(str(remote))
        self.git("branch", "task/queued")
        self.git("push", "-q", "origin", "main", "task/queued")
        self.git("fetch", "-q", "origin")
        self.assertEqual(len(self.by_check(self.rows(), "unmerged-branch")), 1)
        lane = self.lane({"item": 15, "branch": "task/queued", "status": "pending"})
        self.assertEqual([], self.by_check(self.rows(lane=lane), "unmerged-branch"))

    def test_origin_is_asked_without_a_prompt(self) -> None:
        real = subprocess.run
        seen: list[dict[str, str]] = []

        def recording(argv: Any, *args: Any, **kwargs: Any) -> Any:
            if "ls-remote" in argv:
                seen.append(kwargs.get("env") or {})
            return real(argv, *args, **kwargs)

        self.enterContext(mock.patch.object(status.sd_lib.subprocess, "run", side_effect=recording))
        status._origin_heads(self.repo)
        self.assertEqual([env.get("GIT_TERMINAL_PROMPT") for env in seen], ["0"])

    def test_the_section_shows_each_items_newest_unmerged_entry(self) -> None:
        import sd_db
        import sd_lane

        self.set_origin("https://github.com/acme/widget.git")
        sd_db.initialise(home=self.home)
        connection = sd_db.connect(home=self.home)
        try:
            with connection:
                for entry in (
                    {"id": "a1", "item": 20, "branch": "task/twenty", "status": "failed", "reason": "old"},
                    {"id": "a2", "item": 20, "branch": "task/twenty", "status": "pending"},
                    {"id": "b1", "item": 21, "branch": "task/twenty-one", "status": "merged"},
                    {"id": "c1", "item": 22, "branch": "task/twenty-two", "status": "skipped",
                     "code": "head_moved", "body": "long text"},
                ):
                    sd_lane.store(connection, "acme/widget", entry)
        finally:
            connection.close()
        section = status.lane_section(self.repo, {"HOME": str(self.home)})
        self.assertTrue(section["available"], section)
        self.assertEqual([(entry["item"], entry["status"]) for entry in section["entries"]],
                         [(20, "pending"), (22, "skipped")])
        self.assertNotIn("body", section["entries"][1])
        stream = io.StringIO()
        status._render_lane(section, stream.write)
        self.assertRegex(stream.getvalue(), r"\n  #22 +widget +skipped +head_moved +- +task/twenty-two\n")

    def test_a_failed_entry_of_a_finished_item_is_in_neither_the_section_nor_the_rows(self) -> None:
        """sd:5062, sd:5275: no lane verb clears a done item's failed entry, so it is history.

        The rows skipped it while the section's table still listed it; both
        now read the one list `lane_section` returns, so they cannot disagree.
        An item with no row is unknown, never finished, and stays in both.
        """
        import sd_db
        import sd_lane

        self.set_origin("https://github.com/acme/widget.git")
        sd_db.initialise(home=self.home)
        connection = sd_db.connect(home=self.home)
        try:
            with connection:
                made = {}
                for name, state in (("done", "done"), ("open", "in_progress"), ("queued", "done")):
                    made[name] = sd_db.create_item(connection, kind="task", title=name, repo=None)
                    connection.execute("UPDATE item SET status = ? WHERE id = ?", (state, made[name]))
                for entry_id, name, lane_status in (("d1", "done", "skipped"), ("o1", "open", "failed"),
                                                    ("q1", "queued", "pending")):
                    sd_lane.store(connection, "acme/widget", {"id": entry_id, "item": made[name],
                                                              "branch": f"task/{name}", "status": lane_status})
                sd_lane.store(connection, "acme/widget",
                              {"id": "g1", "item": 9999, "branch": "task/ghost", "status": "failed"})
        finally:
            connection.close()
        section = status.lane_section(self.repo, {"HOME": str(self.home)})
        self.assertEqual({entry["item"]: entry["item_status"] for entry in section["entries"]},
                         {made["open"]: "in_progress", made["queued"]: "done", 9999: None})
        stream = io.StringIO()
        status._render_lane(section, stream.write)
        self.assertNotIn("task/done", stream.getvalue())
        self.assertIn("task/open", stream.getvalue())
        found = self.by_check(self.rows(lane=section), "lane-entry-failed")
        self.assertEqual(sorted(row["key"] for row in found), sorted([f"sd:{made['open']}", "sd:9999"]))

    def test_a_repository_with_no_github_origin_has_no_lane(self) -> None:
        section = status.lane_section(self.repo, {"HOME": str(self.home)})
        self.assertFalse(section["available"])
        self.assertIn("has no lane", section["reason"])


class ViewsTests(InventoryFixture):
    """sd:5098: `sd today`'s sections, for the repository `sd-status` stands in and no other."""

    def fill(self) -> tuple[str, dict[str, int]]:
        import sd_db

        sd_db.initialise(home=self.home)
        connection = sd_db.connect(home=self.home)
        try:
            key = status.sd_lib.registered_base(self.repo, sd_db, connection)
            for repo in (key, "~/repos/example.test/lib"):
                sd_db.upsert_repo(connection, repo)
            made = {repo: sd_db.create_item(connection, kind="task", title=f"Free in {repo}", repo=repo)
                    for repo in (key, "~/repos/example.test/lib", None)}
            connection.commit()
        finally:
            connection.close()
        return key, made

    def test_the_views_keep_this_repositorys_rows(self) -> None:
        key, made = self.fill()
        section = status.views_section(self.repo, {"HOME": str(self.home)})
        self.assertTrue(section["available"], section)
        self.assertEqual(section["repos"], [key])
        self.assertEqual([row["id"] for row in section["sections"]["unscheduled"]], [made[key]])
        stream = io.StringIO()
        status._render_views(section, stream.write, width=100)
        text = stream.getvalue()
        self.assertIn("\n  Unscheduled (1)\n", text)
        self.assertIn("\n  Landing now (0)\n", text)
        self.assertNotIn("Free in ~/repos/example.test/lib", text)

    def test_an_undated_item_in_progress_shows_in_the_work_table(self) -> None:
        """pack sd:5933: no other table lists an item in progress with no due date."""
        import sd_db

        key, made = self.fill()
        connection = sd_db.connect(home=self.home)
        try:
            with connection:
                for repo in (key, "~/repos/example.test/lib"):
                    connection.execute("UPDATE item SET status = 'in_progress' WHERE id = ?", (made[repo],))
        finally:
            connection.close()
        section = status.views_section(self.repo, {"HOME": str(self.home)})
        stream = io.StringIO()
        status._render_views(section, stream.write, width=100)
        text = stream.getvalue()
        self.assertIn("\n  Work (1)\n", text)
        self.assertRegex(text, rf"\n  #{made[key]} +\S+ +in_progress +- +- +Free in ")
        self.assertNotIn(f"#{made['~/repos/example.test/lib']} ", text)

    def test_a_companion_the_setting_row_lists_joins_the_views_labelled(self) -> None:
        """The system repository lists the pack too: one `sd.status_companions` row, no path in the code."""
        import sd_db

        key, made = self.fill()
        connection = sd_db.connect(home=self.home)
        try:
            sd_db.writes.set_setting(connection, "sd.status_companions",
                                     f"~/repos/example.test/other=~/repos/example.test/app;{key}=~/repos/example.test/lib")
        finally:
            connection.close()
        section = status.views_section(self.repo, {"HOME": str(self.home)})
        self.assertEqual(section["repos"], [key, "~/repos/example.test/lib"])
        self.assertEqual({row["id"] for row in section["sections"]["unscheduled"]},
                         {made[key], made["~/repos/example.test/lib"]})
        stream = io.StringIO()
        status._render_views(section, stream.write, width=100)
        self.assertRegex(stream.getvalue(), rf"\n  #{made['~/repos/example.test/lib']} +lib +planning ")
        self.assertEqual(sd_db.repos.companions(key, None), [])

    def test_a_companion_setting_that_does_not_read_is_named_not_dropped(self) -> None:
        import sd_db

        key, _ = self.fill()
        connection = sd_db.connect(home=self.home)
        try:
            sd_db.writes.set_setting(connection, "sd.status_companions", "not a repository list")
        finally:
            connection.close()
        section = status.views_section(self.repo, {"HOME": str(self.home)})
        self.assertEqual(section["repos"], [key])
        self.assertTrue(section["companions_error"].startswith("companions unknown: "), section)
        stream = io.StringIO()
        status._render_views(section, stream.write, width=100)
        self.assertIn("\n  ! companions unknown: ", stream.getvalue())

    def test_an_unreadable_database_is_a_reason_not_empty_views(self) -> None:
        section = status.views_section(self.repo, {"HOME": str(self.home)})
        self.assertFalse(section["available"])
        stream = io.StringIO()
        status._render_views(section, stream.write)
        self.assertIn("  ! ", stream.getvalue())


class OperationsTests(InventoryFixture):
    """sd:5432: the open report findings that name this machine, and the gate line, in every repository."""

    def environ(self) -> dict[str, str]:
        gate = self.base / "gate-slots"
        gate.mkdir(exist_ok=True)
        return {"HOME": str(self.home), "SD_GATE_SLOTS": "2", "SD_GATE_SLOTS_DIR": str(gate),
                "SD_GATE_LOAD_MAX": "40"}

    def report(self, connection: Any, job: str, basis: str, *, status: str = "planning",
               attention: bool = True, **extra: Any) -> int:
        import sd_db

        made = sd_db.create_item(connection, kind="report", title=f"{job} findings", source="cron-report",
                                 external_id=f"{job}:20261020T101505Z-1",
                                 fields={"attention": attention, "report": {"attention_basis": basis}, **extra})
        connection.execute("UPDATE item SET status = ? WHERE id = ?", (status, made))
        return made

    def fill(self) -> dict[str, int]:
        import sd_db

        sd_db.initialise(home=self.home)
        connection = sd_db.connect(home=self.home)
        try:
            with connection:
                made = {
                    "health": self.report(connection, "health-check-nightly", "\n".join((
                        "health check on alpha: launchd: com.example.test.job reports a failed run",
                        "health check on beta: disk: under 10% free",
                        "health check on alphabet: not this machine"))),
                    "maintenance": self.report(connection, "maintenance-nightly", "\n".join((
                        "maintenance on alpha: App Store updates pending:",
                        "maintenance on alpha: App Store updates pending: 1 Example (1.0 -> 1.1)"))),
                    "exited": self.report(connection, "secret-scan-weekly", "job exited 2 on alpha"),
                    "other": self.report(connection, "repo-sync-nightly", "repo-sync on beta: hygiene report"),
                    "folded": self.report(connection, "brew-doctor-nightly", "brew doctor on alpha: first set",
                                          repeats={"basis": "brew doctor on alpha: current set"}),
                    "done": self.report(connection, "done-nightly", "done on alpha: closed", status="done"),
                    "quiet": self.report(connection, "quiet-nightly", "quiet on alpha: x", attention=False),
                    "unread": self.report(connection, "broken-nightly", "broken on beta: x"),
                }
                connection.execute("UPDATE item SET fields = 'not json' WHERE id = ?", (made["unread"],))
        finally:
            connection.close()
        return made

    def section(self) -> dict[str, Any]:
        with mock.patch("socket.gethostname", return_value="Alpha.example.test"):
            return status.operations_section(self.environ())

    def test_only_the_lines_that_name_this_machine_are_rows(self) -> None:
        import sd_db.today

        made = self.fill()
        section = self.section()
        self.assertTrue(section["available"], section)
        self.assertEqual(section["host"], "alpha")
        self.assertEqual(sorted((row["id"], row["job"], row["finding"]) for row in section["findings"]), sorted([
            (made["health"], "health-check-nightly", "launchd: com.example.test.job reports a failed run"),
            (made["maintenance"], "maintenance-nightly", "App Store updates pending: 1 Example (1.0 -> 1.1)"),
            (made["exited"], "secret-scan-weekly", "job exited 2"),
            (made["folded"], "brew-doctor-nightly", "current set"),
            (made["unread"], "broken-nightly", sd_db.today.FIELDS_UNREAD),
        ]))
        self.assertEqual(section["gate"], {"holders": [], "waiters": [], "error": None, "slots": 2,
                                           "load": section["gate"]["load"], "load_max": 40.0})
        stream = io.StringIO()
        status._render_operations(section, stream.write, width=120)
        text = stream.getvalue()
        self.assertIn("\nOperations, this machine (5)\n  ID ", text)
        self.assertRegex(text, rf"\n  #{made['health']} +health-check-nightly +launchd: com\.example\.test\.job ")
        self.assertNotIn("disk: under 10% free", text)
        self.assertNotIn("not this machine", text)
        self.assertRegex(text, r"\n  Gate slots: 0 holding · 0 waiting · load \S+, limit 40\n$")

    def test_a_database_read_that_fails_counts_unknown_not_zero(self) -> None:
        self.fill()
        import sd_db.today

        with mock.patch.object(sd_db.today, "operations", side_effect=RuntimeError("disk I/O error")):
            section = self.section()
        self.assertFalse(section["available"])
        self.assertEqual(section["reason"], "shared database unreadable: disk I/O error")
        stream = io.StringIO()
        status._render_operations(section, stream.write)
        self.assertTrue(stream.getvalue().startswith(
            "\nOperations, this machine (unknown)\n  ! shared database unreadable: disk I/O error\n"
            "  Gate slots: 0 holding · 0 waiting · load "))

    def test_no_database_counts_unknown_and_a_gate_that_does_not_read_is_named(self) -> None:
        import sd_gate_slots

        with mock.patch.object(sd_gate_slots, "snapshot", side_effect=PermissionError("slot directory denied")):
            section = self.section()
        self.assertFalse(section["available"])
        self.assertEqual(section["gate"], {"holders": [], "waiters": [], "error": "slot directory denied"})
        stream = io.StringIO()
        status._render_operations(section, stream.write)
        self.assertIn("\nOperations, this machine (unknown)\n  ! no shared sd_db database yet", stream.getvalue())
        self.assertIn("\n  Gate slots: unknown (slot directory denied)\n", stream.getvalue())


class LowYieldProducerTests(InventoryFixture):
    def test_a_marker_in_tracked_source_is_found_and_one_in_docs_is_not(self) -> None:
        # Spelled in halves so this repository's own scan stays at zero, which
        # is the number `design.md` records for it.
        marker = "TO" + "DO"
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "thing.py").write_text(
            f"# {marker}: finish this\n", encoding="utf-8"
        )
        (self.repo / "docs").mkdir(exist_ok=True)
        (self.repo / "docs" / "note.md").write_text(
            f"the {marker} convention\n", encoding="utf-8"
        )
        self.git("add", "bin/thing.py", "docs/note.md")
        self.git("commit", "-q", "-m", "markers")
        found = self.by_check(self.rows(), "source-marker")
        self.assertEqual(len(found), 1)
        self.assertIn("bin/thing.py", found[0]["detail"])

    def test_an_untracked_marker_is_not_a_finding(self) -> None:
        marker = "FIX" + "ME"
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "loose.py").write_text(
            f"# {marker}\n", encoding="utf-8"
        )
        self.assertEqual([], self.by_check(self.rows(), "source-marker"))

    def test_a_skill_naming_a_tool_that_is_not_built_is_a_row(self) -> None:
        skill = self.repo / "skills" / "sd-thing"
        skill.mkdir(parents=True)
        skill.parent.joinpath("sd-thing", "SKILL.md").write_text(
            "Run `bin/sd-thing`, which reads `bin/sd-built`.\n", encoding="utf-8"
        )
        (self.repo / "bin").mkdir(exist_ok=True)
        (self.repo / "bin" / "sd-built").write_text("#!/bin/sh\n", encoding="utf-8")
        found = self.by_check(self.rows(), "undisclosed-tool")
        self.assertEqual([row["title"] for row in found], ["bin/sd-thing"])
        self.assertFalse(found[0]["abnormal"])
        self.assertEqual(found[0]["key"], "skills/sd-thing/SKILL.md#bin/sd-thing")

    def test_an_unreadable_skill_root_marks_the_class_unchecked(self) -> None:
        """An unreadable root is reported, not raised out of the whole report (sd:1000)."""
        skills = self.repo / "skills"
        (skills / "sd-thing").mkdir(parents=True)
        (skills / "sd-thing" / "SKILL.md").write_text("Run `bin/sd-thing`.\n", encoding="utf-8")
        skills.chmod(0)
        self.addCleanup(skills.chmod, 0o755)
        inventory = status.actionable_inventory(self.repo, self.sections(), self.TODAY)
        self.assertEqual([], self.by_check(inventory.rows, "undisclosed-tool"))
        self.assertIn("skills", inventory.unchecked["undisclosed-tool"])

    def test_a_contrib_skill_discloses_on_the_same_terms_as_a_shipped_one(
        self,
    ) -> None:
        """Both roots, or the class reports health over a tree it never read.

        `_tool_rows` used to glob `skills/` alone. `contrib/` is where every
        skill `sd skill try` can still reach lives, and two of them disclose a
        `bin/` command that is not built, so the class printed a count that
        described one half of the surface and read as the whole of it.

        Asserted as one fixture holding one skill in each root, because the
        defect was never "contrib is missed" in isolation -- it was the two
        roots being answered differently. A revert drops the `contrib/` row
        and leaves the `skills/` one, so this fails on exactly the change it
        guards.
        """
        for root, name in (("skills", "sd-shipped"), ("contrib", "sd-tried")):
            skill = self.repo / root / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                f"There is no `bin/{name}` yet.\n", encoding="utf-8"
            )
        (self.repo / "bin").mkdir(exist_ok=True)
        found = self.by_check(self.rows(), "undisclosed-tool")
        self.assertEqual(
            sorted(row["key"] for row in found),
            [
                "contrib/sd-tried/SKILL.md#bin/sd-tried",
                "skills/sd-shipped/SKILL.md#bin/sd-shipped",
            ],
        )

    def disclose(self, said: str) -> None:
        """One skill saying one thing, so a row is about the resolver alone."""
        skill = self.repo / "skills" / "sd-thing"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(said + "\n", encoding="utf-8")
        (self.repo / "bin").mkdir(exist_ok=True)

    def test_a_disclosure_quoting_a_real_module_path_is_not_a_row(self) -> None:
        """`_DISCLOSED_RE` stops at the `.`, so the suffix has to be restored.

        This is two of the six findings the literal test produced in this
        repository: `skills/sd-handoff` writes `bin/sd_handoff_rows.py` and
        `skills/sd-review` writes `bin/sd_setup_github.py`, both of which are
        the true paths of files that are on disk.
        """
        self.disclose("Rows come from `bin/sd_rows.py`.")
        (self.repo / "bin" / "sd_rows.py").write_text("x = 1\n", encoding="utf-8")
        self.assertEqual([], self.by_check(self.rows(), "undisclosed-tool"))

    def test_a_bin_inside_another_path_is_not_a_disclosure(self) -> None:
        """`.venv/bin/python` names a virtualenv's interpreter, not `bin/python`.

        A skill page writes `.venv/bin/python`, and the unanchored pattern
        reported it as an unbuilt tool (k5245). The pack's `bin/` starts a path,
        so a `bin/` after a path segment is someone else's.
        """
        self.disclose("Run `.venv/bin/python` or `~/.local/bin/sd-gone`.")
        self.assertEqual([], self.by_check(self.rows(), "undisclosed-tool"))

    def test_a_module_sharing_a_stem_does_not_silence_a_missing_command(self) -> None:
        """A hyphenated tool is not built by an underscored module beside it.

        The regression guard. An earlier revision of `_tool_candidates` also
        tried `-` respelled `_`, on the theory that `bin/sd_suggest.py` builds
        `bin/sd-suggest`. It does not: such a module is an implementation
        detail `bin/sd` imports, and the command is a `sd <verb>` subcommand.
        The transform therefore gave two identically-worded disclosures
        opposite verdicts -- one silenced by a module that shares its stem, the
        other reported -- and the silenced one was true.

        Which surfaces disclose an absent binary is not written down here, for
        the reason `tests/test_permission_allowlist.py` gives about hand-kept
        lists: `tests/test_skill_frontmatter.py`'s `BinaryClaims` derives it at
        run time. This test's own fixture is synthetic, so it keeps working
        whichever surfaces those are.

        Fails if the `underscored` candidates come back, which is the point:
        the defect it guards was a silenced true finding, and a silenced
        finding leaves nothing in the report to notice.
        """
        self.disclose("There is no `bin/sd-rows` yet.")
        (self.repo / "bin" / "sd_rows.py").write_text("x = 1\n", encoding="utf-8")
        found = self.by_check(self.rows(), "undisclosed-tool")
        self.assertEqual([row["title"] for row in found], ["bin/sd-rows"])

    def test_a_tool_absent_in_every_spelling_survives_the_widening(self) -> None:
        """The failure mode of widening a resolver: silence, not noise.

        Three neighbours that a glob or a prefix test would accept, and none
        of them is a spelling of the disclosed name.
        """
        self.disclose("Planning will live in `bin/sd-gone`.")
        for name in ("sd-goneish", "sd_gone_helper.py", "sd-other"):
            (self.repo / "bin" / name).write_text("x = 1\n", encoding="utf-8")
        found = self.by_check(self.rows(), "undisclosed-tool")
        self.assertEqual([row["title"] for row in found], ["bin/sd-gone"])

    def test_a_directory_under_bin_is_not_a_built_tool(self) -> None:
        """`is_file`, not `exists`: a directory answers no disclosure."""
        self.disclose("Planning will live in `bin/sd-gone`.")
        (self.repo / "bin" / "sd-gone").mkdir()
        found = self.by_check(self.rows(), "undisclosed-tool")
        self.assertEqual([row["title"] for row in found], ["bin/sd-gone"])

    def test_the_candidates_are_the_name_and_the_name_with_its_suffix(self) -> None:
        """Pinned as a list: the shortness is the argument.

        Two candidates, and the second exists only because `_DISCLOSED_RE`
        truncates at the `.`. No respelling of `-` as `_` in either direction
        -- see `_tool_candidates` for why the transform cannot come back.
        """
        self.assertEqual(
            list(status._tool_candidates("sd-example")), ["sd-example", "sd-example.py"]
        )
        self.assertEqual(
            list(status._tool_candidates("sd_plan")), ["sd_plan", "sd_plan.py"]
        )
        # Stated as a property too, so a third candidate cannot be added
        # later in a spelling this pin happens not to name.
        self.assertTrue(
            all("_" not in name for name in status._tool_candidates("sd-example"))
        )


class SkillSurfaceTests(unittest.TestCase):
    """One answer to "what is a skill surface", pinned across the two readers.

    The pack had two, and they disagreed. `bin/sd-status` globbed `skills/`;
    `tests/test_skill_frontmatter.surfaces()` walked `skills/` and `contrib/`
    both. The producer's half was the wrong one, and the cost was not a
    cosmetic count -- `undisclosed-tool` asserted health over `contrib/`
    without opening it, which is the failure the banner refuses by never
    printing `clear` over an unchecked class.

    The single answer lives in `bin/`, not here. A check may not depend on the
    test suite: `bin/` ships and `tests/` does not, so the direction that
    survives installation is the suite reading the producer. What this file
    owes in return is the pin -- if either reader widens or narrows alone, the
    sets stop matching and this fails. Agreeing today without pinning the
    agreement is the same defect deferred to whoever adds the third root.
    """

    def test_the_producer_and_the_suite_enumerate_the_same_surfaces(self) -> None:
        root = BIN.parent
        produced = {p.relative_to(root) for p in status.skill_surfaces(root)}
        asserted = {p.relative_to(root) for p in frontmatter_surfaces()}
        self.assertEqual(produced, asserted)
        # Not vacuous: an enumeration that returned nothing would also match.
        self.assertTrue(any(p.parts[0] == "contrib" for p in produced))
        self.assertTrue(any(p.parts[0] == "skills" for p in produced))

    def test_a_directory_without_a_skill_file_is_not_a_surface(self) -> None:
        """`skills/_shared` is a real directory holding references, not a skill.

        The suite checks `is_file()` on the entrypoint; so must the producer,
        or the two sets differ by a directory that was never a surface.
        """
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            (root / "skills" / "_shared").mkdir(parents=True)
            (root / "contrib" / "sd-real").mkdir(parents=True)
            (root / "contrib" / "sd-real" / "SKILL.md").write_text(
                "x\n", encoding="utf-8"
            )
            self.assertEqual(
                [p.relative_to(root) for p in status.skill_surfaces(root)],
                [pathlib.Path("contrib/sd-real/SKILL.md")],
            )

    def test_a_missing_root_is_not_an_error(self) -> None:
        """A checkout without `contrib/` still reports on `skills/`."""
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            (root / "skills" / "sd-one").mkdir(parents=True)
            (root / "skills" / "sd-one" / "SKILL.md").write_text(
                "x\n", encoding="utf-8"
            )
            self.assertEqual(
                [p.relative_to(root) for p in status.skill_surfaces(root)],
                [pathlib.Path("skills/sd-one/SKILL.md")],
            )


class RowWorkItemInventoryTests(InventoryFixture):
    """Retired status files cannot be the repair for database-owned work."""

    ITEM = "2026-09-05-alpha"

    def setUp(self) -> None:
        super().setUp()
        import sd_db

        self.db = sd_db
        self.db.initialise(home=self.home)
        self._ready = True

    def row_item(self, *, branch: str = "feature", missing: bool = False) -> pathlib.Path:
        if not missing:
            return self.item(self.ITEM, status="in_progress", extra=f"branch: {branch}\n")
        directory = self.repo / "docs" / "work" / self.ITEM
        directory.mkdir(parents=True)
        (directory / "prd.md").write_text(
            PRD.format(title=self.ITEM, status="", extra=f"branch: {branch}\n").replace("status: \n", ""),
            encoding="utf-8",
        )
        return directory

    def test_unmerged_feature_closing_trailer_is_not_already_merged(self) -> None:
        self.branch("feature", land=False)
        self.row_item()
        self.git("checkout", "-q", "feature")
        self.git("commit", "-q", "--allow-empty", "-m", f"Proposed delivery\n\nDelivers: {self.ITEM}")
        result = self.inventory(protection=self.BLIND)
        self.assertEqual([], self.by_check(result.rows, "branch-already-merged"))

    def test_row_closing_history_is_read_once_per_default_ref(self) -> None:
        self.git("commit", "-q", "--allow-empty", "-m", "Delivered item\n\nCloses: item-0")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        work = {"items": [
            {"path": f"docs/work/item-{index}", "slug": f"item-{index}",
             "status": "in_progress", "branch": "main", "archived": False}
            for index in range(40)]}
        with mock.patch.object(status.sd_lib, "upstream", return_value=("origin", "main")), \
                mock.patch.object(status.sd_lib, "git_output", wraps=status.sd_lib.git_output) as git_calls:
            rows = status._work_rows(self.repo, work, self.TODAY)
        walks = [call.args[0] for call in git_calls.call_args_list
                 if call.args[0][0] == "log" and "--grep" in call.args[0]]
        self.assertEqual([argv[-1] for argv in walks], ["main", "origin/main"])
        self.assertEqual([row["key"] for row in self.by_check(rows, "branch-already-merged")], ["item-0"])

    def test_quoted_closing_line_is_not_a_default_branch_trailer(self) -> None:
        self.branch("feature", land=True)
        self.row_item()
        self.git("commit", "-q", "--allow-empty", "-m", f"Quoted example\n\nCloses: {self.ITEM}\n\nThis is an example, not a delivery.")
        self.assertEqual([], self.by_check(self.inventory(protection=self.BLIND).rows, "branch-already-merged"))

    def test_an_ordinary_slice_merge_does_not_close_a_row_owned_item(self) -> None:
        self.branch("feature", land=True)
        self.row_item()
        result = self.inventory(protection=self.BLIND)
        self.assertEqual([], self.by_check(result.rows, "branch-already-merged"))
        self.assertNotIn("branch-already-merged", result.unchecked)

    def test_a_closing_trailer_reconciles_the_row_without_rewriting_frontmatter(self) -> None:
        self.branch("feature", land=True)
        self.row_item()
        self.git("commit", "-q", "--allow-empty", "-m", f"Deliver\n\nCloses: {self.ITEM}")
        # SQLite mode=ro may maintain these two lock files for a WAL database.
        # Main database bytes and every repository file still must be unchanged.
        lock_files = {"home/.local/share/sd/sd.db-wal", "home/.local/share/sd/sd.db-shm"}

        def snapshot():
            return {path: value for path, value in tree_digest(self.base).items()
                    if path not in lock_files}

        before = snapshot()
        result = self.inventory(protection=self.BLIND)
        found = self.by_check(result.rows, "branch-already-merged")
        self.assertEqual(1, len(found))
        self.assertIn(f"Closes: {self.ITEM}", found[0]["detail"])
        self.assertIn("database row", found[0]["suggest"])
        self.assertNotIn("set status:", found[0]["suggest"])
        self.assertEqual(before, snapshot())

    def test_a_missing_database_row_keeps_its_identity_and_source_in_the_repair(self) -> None:
        self.branch("feature", land=False)
        self.row_item(missing=True)
        found = self.by_check(self.inventory(protection=self.BLIND).rows, "status-unreadable")
        self.assertEqual(1, len(found))
        self.assertIn("database holds no docs/work row", found[0]["detail"])
        self.assertIn(f"{self.repo}::docs/work/{self.ITEM}/prd.md", found[0]["detail"])
        self.assertIn("database holds no docs/work row", found[0]["suggest"])
        self.assertNotIn("frontmatter", found[0]["suggest"])

    def test_an_unknown_git_fallback_keeps_the_failed_probe_and_its_repair(self) -> None:
        self.branch("feature", land=False)
        self.row_item()
        with mock.patch.object(self.db, "connect", side_effect=FileNotFoundError("no database")), \
             mock.patch.object(status.sd_lib, "delivered", return_value=status.sd_lib.Answer(
                 status.sd_lib.UNKNOWN, "git fetch --unshallow"
             )):
            work = status.work_section(self.repo)
            result = self.inventory(work=work, protection=self.BLIND)
        found = self.by_check(result.rows, "status-unreadable")
        self.assertEqual(1, len(found))
        self.assertIn("cannot see whether", found[0]["detail"])
        self.assertIn("git fetch --unshallow", found[0]["suggest"])
        self.assertNotIn("database row", found[0]["suggest"])
        self.assertNotIn("frontmatter", found[0]["suggest"])
        self.assertEqual("unknown", work["items"][0]["status"])
        checked = next(row for row in status.banner(result)["classes"]
                       if row["check"] == "status-unreadable")
        self.assertEqual(status.FINDINGS, checked["state"])


class BannerTests(InventoryFixture):
    """The three per-class states, and the one substring `prd.md:134` checks.

    Against real git: the state under test is produced by asking git a
    question, and a mock would assert the arrangement rather than the answer.
    """

    def state_of(self, result: dict[str, Any], check: str) -> dict[str, Any]:
        return next(row for row in result["classes"] if row["check"] == check)

    # -- the shape ----------------------------------------------------------

    def test_the_banner_lists_every_abnormal_class_and_only_those(self) -> None:
        """`CLASSES` is the enumeration; the banner does not keep a second one."""
        result = status.banner(self.inventory())
        self.assertEqual(
            [kind.check for kind in status.CLASSES if kind.abnormal],
            [row["check"] for row in result["classes"]],
        )

    def test_nothing_wrong_and_nothing_unread_is_the_only_way_to_say_clear(self) -> None:
        result = status.banner(self.inventory())
        self.assertEqual(0, result["unchecked_classes"])
        self.assertEqual([], result["findings"])
        self.assertIn("clear", result["summary"])
        self.assertEqual(
            {status.CLEAR}, {row["state"] for row in result["classes"]}
        )

    def test_a_class_that_fired_carries_its_own_count(self) -> None:
        self.item("2026-08-01-alpha", status="in_progress")
        one = status.banner(self.inventory())
        self.assertEqual(
            "1 finding", self.state_of(one, "in-progress-without-branch")["label"]
        )
        self.assertIn("1 finding across 1 check;", one["summary"])
        self.item("2026-08-02-beta", status="in_progress")
        two = status.banner(self.inventory())
        self.assertEqual(
            "2 findings", self.state_of(two, "in-progress-without-branch")["label"]
        )
        self.assertIn("2 findings across 1 check;", two["summary"])

    # -- the third state ----------------------------------------------------

    def test_a_check_that_could_not_run_is_unchecked_and_the_word_clear_is_gone(
        self,
    ) -> None:
        """`prd.md:134`, in process: unchecked > 0 and `clear` not in summary.

        The acceptance criterion runs this through `--json` with `gh` taken
        off `PATH`; nothing renders the banner until step 4, so the same two
        assertions are made against the structure the renderer will read.
        """
        self.branch("feature", land=False)
        self.item("2026-08-01-alpha", status="in_progress", extra="branch: feature\n")
        result = status.banner(self.inventory(protection=self.BLIND))
        row = self.state_of(result, "protection-gap")
        self.assertEqual(status.UNCHECKED, row["state"])
        self.assertEqual("unchecked: gh is not installed", row["label"])
        self.assertNotIn("clear", result["summary"])
        # A blind `protection` puts every class that declares it out of reach,
        # which is sd:600 -- so the assertion names the set rather than a
        # number that would grow silently. The merge class reads rows and
        # local history, so it is not in it.
        self.assertEqual(
            {"protection-gap", "pr-check-missing"},
            {row["check"] for row in result["classes"]
             if row["state"] == status.UNCHECKED},
        )

    def test_an_unresolvable_branch_leaves_the_merge_check_clear(self) -> None:
        """A stale `branch:` field is a finding of its own class.

        It says nothing about whether GitHub could be read, so it must not
        mark the merge class unchecked.
        """
        self.item(
            "2026-08-01-alpha", status="in_progress", extra="branch: gone-away\n"
        )
        inventory = self.inventory(protection=self.BLIND)
        self.assertEqual(
            ["branch-unresolvable"],
            [row["check"] for row in inventory.rows if row["check"].startswith("branch-")],
        )
        # Narrowed to this test's subject. A blind `protection` does mark the
        # classes that read it, so an empty map would assert something this
        # test is not about and sd:600 says is wrong.
        self.assertNotIn("branch-already-merged", inventory.unchecked)
        result = status.banner(inventory)
        self.assertEqual(
            status.CLEAR, self.state_of(result, "branch-already-merged")["state"]
        )

    # -- sd:600: a class whose section could not be read ----------------------

    #: Which collected section each check reads, as the design states it and
    #: not as `CLASSES` happens to say today. A test that derives this from
    #: the table cannot catch a deleted or mistyped `needs` entry, because the
    #: expectation moves with the defect. Adding a dependency means adding it
    #: here and in `CLASSES`, and the two are asserted equal below.
    DECLARED_SECTIONS = {
        "pull_requests": {
            "pr-check-failing", "pr-check-missing", "dirty-tree-with-open-pr",
        },
        "protection": {"pr-check-missing", "protection-gap"},
        "lane": {"lane-entry-failed"},
    }

    def test_the_table_declares_exactly_the_dependencies_the_design_states(
        self,
    ) -> None:
        """`CLASSES` against the design, so a deleted `needs` entry is loud."""
        actual: dict[str, set[str]] = {}
        for kind in status.CLASSES:
            for name in kind.needs:
                actual.setdefault(name, set()).add(kind.check)
        self.assertEqual(self.DECLARED_SECTIONS, actual)

    def test_a_blind_section_marks_every_class_that_declares_it(self) -> None:
        """Every section any class declares, driven blind in turn.

        Derived from `CLASSES.needs`, never from a list of check names: the
        defect this replaces counted eleven of twelve classes clear on a
        machine that could not reach GitHub at all, because `unchecked` had
        exactly one producer.

        Driving only `protection` was not enough, and a mutation proved it:
        deleting all three `pull_requests` dependencies left this suite green
        at 211 passing, so the merge checks would have gone on reporting clear
        against a `pull_requests` section nothing could read.

        Deriving the expectation from `CLASSES.needs` does not fix that, and
        the same mutation proved that too. Blank an entry and the sections
        looped over and the checks expected shrink together, so the assertion
        agrees with whatever the table says. The expectation therefore comes
        from `DECLARED_SECTIONS`, which states the design.
        """
        for name, expected in sorted(self.DECLARED_SECTIONS.items()):
            with self.subTest(section=name):
                # Blind the section in place, so it keeps the shape its
                # producers expect and only the readability flag changes.
                blinded = dict(self.sections()[name], **{
                    "available": False, "reason": "gh is not installed"})
                inventory = self.inventory(**{name: blinded})
                self.assertLessEqual(expected, set(inventory.unchecked))
                for check in expected:
                    self.assertEqual(
                        "gh is not installed", inventory.unchecked[check])

    def test_a_class_that_needs_no_section_is_not_marked_unchecked(
        self,
    ) -> None:
        """The control. Without it the test above passes on a blanket mark.

        `in-progress-without-branch` reads work items and git and nothing
        else, so a blind `protection` must leave it exactly as it was.
        """
        self.item("2026-08-01-alpha", status="in_progress")
        blind = status.banner(self.inventory(protection=self.BLIND))
        self.assertEqual(
            status.FINDINGS,
            self.state_of(blind, "in-progress-without-branch")["state"],
        )
        self.assertNotIn("in-progress-without-branch", 
                         {row["check"] for row in blind["classes"]
                          if row["state"] == status.UNCHECKED})

    def test_a_readable_section_marks_nothing(self) -> None:
        """The second control: the success case still reports clear.

        A guard that marked on every run would satisfy both assertions above
        and report a healthy repository as blind forever.
        """
        result = status.banner(self.inventory())
        self.assertEqual(0, result["unchecked_classes"])
        self.assertIn("clear", result["summary"])

    def test_the_summary_cannot_say_clear_while_any_class_is_blind(self) -> None:
        """sd:4's criterion 14, as an executable check.

        `skills/sd-status/SKILL.md` states this three times in its Never list
        and the code did not do it: the banner said `all 12 checks clear` over
        a `protection` section carrying `available: False` and a reason.
        """
        summary = status.banner(self.inventory(protection=self.BLIND))["summary"]
        self.assertNotIn("clear", summary)
        self.assertIn("could not run", summary)

    def test_every_section_a_class_declares_is_a_section_that_exists(self) -> None:
        """A typo in `needs` would make a class silently never go blind.

        Compared against the section names the report actually assembles, so
        renaming a section breaks here rather than turning a check back into a
        silent pass.
        """
        declared = {name for kind in status.CLASSES for name in kind.needs}
        self.assertLessEqual(declared, set(self.sections()))

    # -- the producer -------------------------------------------------------

    def test_a_done_item_whose_branch_landed_is_not_a_finding(self) -> None:
        self.branch("feature", land=True)
        self.item("2026-08-01-alpha", status="done", extra="branch: feature\n")
        self.assertEqual(
            [], self.by_check(self.inventory().rows, "branch-already-merged")
        )


class ConcernLedgerTests(InventoryFixture):
    """The ledger scanner, against real `git grep` over a real index.

    Real git because the scan *is* a `git grep`, and its two known traps --
    POSIX ERE rejecting `\\b`, and the space after a table pipe -- are
    properties of that program rather than of the design.
    """

    def ledger(self, name: str, body: str) -> str:
        """A `docs/work` page carrying a ledger, tracked so `git grep` sees it."""
        path = self.repo / "docs" / "work" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        self.git("add", "-A")
        return f"docs/work/{name}"

    def checks(self, rows: list[dict[str, Any]]) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for row in rows:
            found.setdefault(row["check"], []).append(row["key"])
        return found

    def scan(self, **overrides: Any) -> list[dict[str, Any]]:
        return [
            row for row in self.inventory(**overrides).rows
            if row["check"].endswith("-concern") or row["check"].endswith("-concern-row")
        ]

    # -- `accepted`, the word that is also prose ----------------------------

    def test_accepted_beside_a_closing_word_does_not_open_the_row(self) -> None:
        """`accepted` read before `addressed` turns a closed row into a finding.

        Eight rows in this repo's own ledgers say `accepted` about something
        other than their disposition -- "validation accepted a codex
        provider" -- while closing themselves a few words later. Read as an
        opening word, the prose wins and the row reports as a defect.
        """
        self.ledger("2026-08-03-prose/prd.md", (
            "# prose\n\n"
            "- **C-9 -- the ninth** `addressed`: validation accepted a "
            "provider it should have refused.\n"
        ))
        self.assertEqual([], self.scan())

    def test_accepted_alone_parks_the_row_rather_than_opening_it(self) -> None:
        """An accepted concern is a decision that stands, not open work.

        `accepted-gap-standing` already says so for the file side, and is not
        abnormal. A ledger row saying only `accepted` describes the same
        state and must not land in the banner as a defect.
        """
        self.ledger("2026-08-04-standing/prd.md", (
            "# standing\n\n"
            "- **C-11 -- the eleventh** (high, ACCEPTED): the guard reads "
            "only the paths this change touches.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            {"parked-concern": ["docs/work/2026-08-04-standing/prd.md#C-11"]},
            found,
        )

    # -- both shapes --------------------------------------------------------

    def test_a_table_ledger_and_a_bullet_ledger_are_both_read(self) -> None:
        """One ledger is a markdown table and one is a bold bullet list.

        The space after the table pipe is C-18: `| C-7 | ... |` without ` *`
        in the anchor matches nothing, which is how an entire ledger table
        disappears in silence.
        """
        self.ledger("2026-08-01-table/prd.md", (
            "# table\n\n"
            "| id | disposition |\n| --- | --- |\n"
            "| C-1 | addressed |\n| C-2 | deferred |\n"
        ))
        self.ledger("2026-08-02-bullets/prd.md", (
            "# bullets\n\n"
            "- **C-1 -- the first** `addressed`.\n"
            "- **C-3 -- the third** left `unresolved`.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-01-table/prd.md#C-2",
             "docs/work/2026-08-02-bullets/prd.md#C-3"],
            sorted(found.get("unresolved-concern", [])),
        )

    # -- the precedence rules ----------------------------------------------

    def test_an_open_token_beats_a_closing_one_on_the_same_row(self) -> None:
        """`ACCEPTED and parked` is the common form, and such a row is parked.

        Under-reporting an open concern is the worst failure this can have, so
        the order is parked, then open, then closed, then standing.
        """
        self.ledger("2026-08-01-mixed/prd.md", (
            "# mixed\n\n"
            "- C-1 (high, ACCEPTED and parked): a security acceptance.\n"
            "- C-2: addressed, but the fix is deferred to a later item.\n"
            "- C-3: rebutted with evidence.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-01-mixed/prd.md#C-1"], found.get("parked-concern")
        )
        self.assertEqual(
            ["docs/work/2026-08-01-mixed/prd.md#C-2"], found.get("unresolved-concern")
        )
        self.assertNotIn("C-3", str(found))

    def test_a_row_that_declares_addressed_is_read_by_its_declaration(self) -> None:
        """sd:1000 (note #2752): C-6 and C-33 in the system ledgers said
        `Addressed:` and then named the defect's own words, `open means
        unresolved` and `an unresolved merge`, so they read as unresolved.
        `_declared_verdict` reads the declaration; a lower-case mention is
        not one, so the control row stays open.
        """
        self.ledger("2026-09-05-declared/prd.md", (
            "# declared\n\n"
            "- C-6, requirement 7: notes had no resolution state. Addressed: notes carry "
            "`resolved_at`, and open means unresolved.\n"
            "- C-33: Addressed: the fixture restores an unresolved merge before the run.\n"
            "- C-34: the reader still treats an unresolved merge as clean; addressed: later.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            {"unresolved-concern": ["docs/work/2026-09-05-declared/prd.md#C-34"]}, found
        )

    def test_a_table_row_outranks_a_prose_mention_of_the_same_concern(self) -> None:
        """Shape precedence, and what it stops.

        The ledger of record is the most structured row present. Without this
        rule a `## Log` sentence that merely begins with a `C-` id decides the
        concern's disposition -- measured on this item's own C-4, which is
        `rebutted` in its table and read as open from a Log line that mentions
        a *different* concern's parking.

        The Log comes first in the fixture on purpose. Rows of equal shape keep
        the one seen first, so a scanner with no precedence rule would keep the
        table row anyway if the table came first, and the test would pass
        against the broken code.
        """
        self.ledger("2026-08-01-both/prd.md", (
            "# both\n\n"
            "## Log\n\n"
            "C-4, C-8 and C-10 were rebutted; C-6 parked.\n\n"
            "## Review\n\n"
            "| id | disposition |\n| --- | --- |\n"
            "| C-4 | rebutted |\n"
        ))
        self.assertEqual([], self.scan())

    def test_a_bullet_row_outranks_a_prose_sentence_about_the_same_concern(
        self,
    ) -> None:
        """Bullet is its own tier, above prose.

        A bulleted row is a ledger entry; a sentence that happens to open with
        a `C-` id is not. Sharing a tier, which one decides the concern is
        settled by whichever line `git grep` returns first, so the prose is
        written above the row here -- exactly as the shape-precedence fixture
        does, and for the same reason.

        Found by review on #792. On the live corpus the three-tier and
        four-tier rules agree on every one of 532 concerns, re-measured after
        `_ROW_START_RE` fixed absorption, so this is a latent defect removed
        rather than a live misclassification corrected.
        """
        self.ledger("2026-08-01-bullet/prd.md", (
            "# bullet\n\n"
            "## Log\n\n"
            "C-9 was raised while C-2 was still parked.\n\n"
            "## Review\n\n"
            "- C-9, minor: the count was off by one. Corrected.\n"
        ))
        self.assertEqual([], self.scan())

    def test_a_table_row_is_read_by_its_verdict_column_and_not_by_its_narrative(self) -> None:
        """C-11's shape: the verdict says addressed, the narrative says deferred.

        Every cell before the last is *about the defect*, so it carries the
        subject's vocabulary. Under tier order alone the narrative's `deferred`
        outranks the verdict's `addressed` and the row reads open -- which is
        the misread `_disposition` already documents for prose rows and fixes
        by reading the header first. A table row has no header to read, so it
        fell through unchanged until the last cell was read the same way.
        """

        self.ledger("2026-08-02-table/design.md", (
            "# table\n\n"
            "| id | sev | finding | disposition |\n"
            "|---|---|---|---|\n"
            "| C-11 | low | validation was deferred to a phase-2 refresh on the"
            " assumption the fix was in shipped text | **addressed** - D2d"
            " validates locally |\n"
        ))
        # Every row, not two named classes. Copilot's verification pass was
        # right that excluding `unresolved-concern` and `unreadable-concern-row`
        # leaves a misread free to land as `parked-concern` and still pass.
        # `addressed` is closed, and closed means this ledger yields nothing.
        self.assertEqual([], self.scan())

    def test_a_table_row_whose_verdict_column_is_open_stays_open(self) -> None:
        """The control, and the half that stops the fix from silencing rows.

        Without it the last-cell read could return anything and the test above
        would still pass. C-1's real shape: a deferral written *as* the
        verdict, which is open and has to stay open.
        """

        self.ledger("2026-08-03-deferred/design.md", (
            "# deferred\n\n"
            "| id | sev | finding | disposition |\n"
            "|---|---|---|---|\n"
            "| C-1 | critical | the installer flips a real registry entry,"
            " mutating the source checkout | **deferred** to"
            " 08-11-thin-candidate-loop-shape |\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-03-deferred/design.md#C-1"],
            found.get("unresolved-concern"),
        )

    def test_a_disposition_on_the_next_line_is_still_read(self) -> None:
        """Continuation absorption: dispositions wrap, and the row is one row."""
        self.ledger("2026-08-01-wrapped/prd.md", (
            "# wrapped\n\n"
            "- C-5, minor: the count in the third paragraph is off by one\n"
            "  under the criterion's own literal strings.\n"
            "  Corrected.\n\n"
            "- C-6, minor: a second row whose disposition never arrives\n"
            "  because the paragraph simply stops.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-01-wrapped/prd.md#C-6"],
            found.get("unreadable-concern-row"),
        )

    def test_a_continuation_opening_with_a_cross_reference_is_not_a_new_row(self) -> None:
        """A row's argument is usually about another row, so its prose opens
        with one. Absorption must not read that as the next entry.

        Measured on C-120 of the solo-first item before this: the row says
        `Addressed` on its fifth line, its third line opens `C-100's rebuild`,
        and the row was reported as disposed in words nothing could read. The
        bullet below it is a real row and must still end absorption, or one
        entry's disposition would close the entry above it.
        """

        self.ledger("2026-08-01-crossref/prd.md", (
            "# crossref\n\n"
            "- C-8, blocking: the criterion's documentation clause was skipped.\n"
            "    C-100's rebuild derived the Touches from the runtime clauses\n"
            "    and dropped it. Addressed, and PR 1 is ordered before PR 6.\n"
            "- C-9, blocking: a second row whose disposition never arrives.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-01-crossref/prd.md#C-9"],
            found.get("unreadable-concern-row"),
            "C-8 closes on its own fifth line; C-9 carries no word at all",
        )

    def test_an_unindented_row_needs_no_marker_to_end_absorption(self) -> None:
        """The older archived ledgers write rows as bare prose at column zero.

        Requiring a bullet would make every one of those rows a continuation of
        the row above it, which is the opposite failure and a larger one.

        The disposition sits on the *second* row and the bare one comes first,
        so over-absorption changes the answer rather than preserving it. Put
        the other way round -- the closing word above, the bare row below --
        the second row is still found on its own and still reads as nothing,
        and the fixture passes whether the unindented branch is there or not.
        Found by review on #821: the first version of this test was written
        that way and guarded nothing.
        """

        self.ledger("2026-08-01-bare/prd.md", (
            "# bare\n\n"
            "C-1 identified the wrong anchor and nothing here says what became"
            " of it.\n"
            "C-2 is an argument-construction defect and only argv proves it."
            " Corrected.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-01-bare/prd.md#C-1"],
            found.get("unreadable-concern-row"),
        )

    def test_a_wrapped_sentence_is_not_a_ledger_row(self) -> None:
        """A concern named only in passing has no row, and reporting one
        invents a finding nobody can go and read.

        `CONCERN_ANCHOR` is anchored at the start of a line, so the only
        prose it can match is prose that happens to *wrap* onto a `C-` id.
        Real: `2026-09-04-the-plan-interview-is-one-sentence/design.md` has
        no C-9 row anywhere, and the only place the id appears in the file is
        the tail of the sentence below. 7 of the 627 hits collapse to 6 such
        rows of record, four reported as unreadable and two reported as
        parked from a word inside somebody else's paragraph.

        The rule is the wrap and not the prose shape, so the last two rows
        are the half that must not move: `C-3` opens a block and `C-4`
        follows a finished sentence, and both are bare prose rows of the kind
        `_ROW_START_RE` exists for. `C-1` is not a hit at all -- it sits
        mid-line, where the anchor cannot reach it -- which is the shape that
        makes this defect invisible until a sentence wraps.
        """
        self.ledger("2026-08-04-wrap/prd.md", (
            "# wrap\n\n"
            "The clause survived because it was the same fixed-string "
            "quoting that C-1 and\n"
            "C-2 earned. A criterion whose path names nothing checks "
            "nothing.\n\n"
            "C-3 identified the wrong anchor and nothing here says what "
            "became of it.\n"
            "C-4 is an argument-construction defect and only argv proves it."
            " Corrected.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-04-wrap/prd.md#C-3"],
            found.get("unreadable-concern-row"),
            "C-2 is the tail of a sentence, not a row; C-3 and C-4 are rows",
        )

    def test_a_bullet_that_is_not_a_row_still_ends_absorption(self) -> None:
        """A row's verdict comes from the row, and the bullet below it is a
        different bullet.

        Real, and the one row the boundary moves: C-89 of the solo-first
        item's `prd.md` writes no disposition word of its own, and was read
        as closed from `Recorded, not corrected here` in the bullet under it
        -- a bullet about criterion 4 naming a directory that is not there.
        Both halves are here: the row states its severity and its decision
        and never says what became of it, and the bullet below carries two
        closing words. Absorbing the second is how the first reads clean.
        """
        self.ledger("2026-08-01-foreign/prd.md", (
            "# foreign\n\n"
            "- C-3, blocking: the two issues stay open, and criterion 28's\n"
            "  clause is rewritten to assert the rows instead.\n"
            "- **The governed tree names a directory that is not there.**\n"
            "  The criterion lists `templates/`; there is no such directory.\n"
            "  Recorded, not corrected here.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-01-foreign/prd.md#C-3"],
            found.get("unreadable-concern-row"),
            "C-3 says nothing this reader carries; the bullet below is not "
            "its row and must not close it",
        )

    def test_a_wrap_onto_a_number_is_not_a_new_item(self) -> None:
        """The bullet boundary stops at bullets and deliberately not at
        numbered items.

        Real: C-107 of the solo-first item wraps `... was cited at line` onto
        `21. Line 21 is about rendered copies ...`, so the row's own
        continuation opens exactly like an ordered-list item. Widening the
        boundary to catch it costs the row its own `Addressed.` and makes a
        correctly-read row unreadable -- one more finding, no rescue, against
        a row that was already right. This fixture is that shape, and C-5
        below it is the row that must stay the only finding.
        """
        self.ledger("2026-08-01-numbered/prd.md", (
            "# numbered\n\n"
            "- C-4, material: the writes-nothing claim was cited at line\n"
            "  21. Line 21 is about rendered copies; the claim is line 34.\n"
            "  Addressed.\n"
            "- C-5, material: a row whose disposition never arrives.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-01-numbered/prd.md#C-5"],
            found.get("unreadable-concern-row"),
            "C-4 closes itself on its third line, across a wrap that opens "
            "with a number",
        )

    # -- never dropped ------------------------------------------------------

    def test_a_row_with_no_word_this_reader_carries_is_a_finding(self) -> None:
        """An unrecognised format inflates the count rather than emptying it.

        That is the failure the design was protecting against: parsing one
        format, finding nothing in the other three, and printing a clean
        banner. A row nobody can classify is abnormal on purpose.
        """
        path = self.ledger("2026-08-01-strange/prd.md", (
            "# strange\n\n- C-7: **Disputed, pending a second opinion.**\n"
        ))
        rows = self.scan()
        self.assertEqual(["unreadable-concern-row"], [row["check"] for row in rows])
        self.assertTrue(rows[0]["abnormal"])
        self.assertIn(f"{path}:3", rows[0]["detail"])

    def test_the_sixth_vocabulary_closes_rows_and_moves_nothing_else(self) -> None:
        """`Corrected`/`Recorded`/`Noted`/`superseded`/`Confirmed` were read.

        Closing words are checked last, so adding one can only ever reclassify
        a row nothing could read -- never an open one. This asserts both
        halves: the five close, and a row that also carries an open token
        stays open.
        """
        self.ledger("2026-08-01-sixth/prd.md", (
            "# sixth\n\n"
            "- C-1, minor: a line number was wrong. Corrected.\n"
            "- C-2, minor: the count is off. Recorded rather than recounted.\n"
            "- C-3, minor: the omission stands. Noted in the registry block.\n"
            "- C-4, minor: C-3 superseded here.\n"
            "- C-5: **Confirmed, design changed.**\n"
            "- C-6: Corrected in place, but the wider gap is deferred.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-01-sixth/prd.md#C-6"], found.get("unresolved-concern")
        )
        self.assertEqual([], found.get("unreadable-concern-row", []))

    def test_restated_closes_a_row_and_an_open_token_still_beats_it(self) -> None:
        """`Restated` is the seventh closing word, and the tier order holds.

        Measured whole-corpus: it rescues 3 of the 33 rows nothing could read
        and reclassifies none, taking the floor to 30 of 532. Two of the three
        are the real thing -- C-163 in the solo-first item ends `Restated as
        the work.` and C-172 ends `Restated as a bare-token shape`, and in
        both the sentence carrying the word is the disposition.

        The second row here is the half that is easy to lose: closing words
        are read after the open ones, so a row that restates *and* defers is
        still open. Under-reporting an open concern is the worst failure this
        feature can have, and a new closing word is exactly how it would
        happen.
        """
        self.ledger("2026-08-01-seventh/prd.md", (
            "# seventh\n\n"
            "- C-1, minor: the requirement read as a description of the file "
            "rather than as work. Restated as the work.\n"
            "- C-2, blocking: the criterion could not pass as written. "
            "Restated as a bare-token shape, but the vendor clause is "
            "deferred to a later pull request.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-01-seventh/prd.md#C-2"],
            found.get("unresolved-concern"),
        )
        self.assertEqual([], found.get("unreadable-concern-row", []))

    def test_supersedes_closes_the_row_superseded_missed_by_one(self) -> None:
        """`supersedes` is the inflection `superseded` does not cover.

        One row in the corpus, C-175 in the solo-first item, opens
        `supersedes C-173's split`. 1 rescue, 0 reclassifications. Carried as
        a literal rather than by stemming, which would fold `noted` into
        `note`/`notes`/`noting` to win it.
        """
        self.ledger("2026-08-01-inflected/prd.md", (
            "# inflected\n\n"
            "- C-1, blocking, supersedes C-2's split: the clause is PR 6's "
            "whole, not two files of eight.\n"
        ))
        self.assertEqual([], self.scan())

    def test_a_bold_header_disposes_the_row_its_own_prose_cannot(self) -> None:
        """A row's bold header is the row saying what it is, and it is read
        before the tiers see the prose after it.

        The prose after the header is *about the defect*, so it carries the
        subject's vocabulary -- a rule that defers, an unresolved path -- and
        under tier order alone any of those outranks an `addressed` written
        four words in. Measured: reading the header first moves 5 rows, every
        one `open` to `closed`, rescues none and loses none. Four are
        `**C-N -- <finding>. `addressed`.**` in the write-the-test-first item
        and the fifth is `**C-18 addressed**` in the consolidate-git item.
        Five of the fourteen rows this feature called open were this misread.

        The second row is the half that must not move: with no bold header
        there is nothing to read first, so the tiers decide as before and a
        row whose prose defers is still open. That is what keeps this rule
        off C-182, which says `accepted` before it says `Closed by
        normalizing at the write` and which earliest-match-wins would break.
        """
        self.ledger("2026-08-02-header/prd.md", (
            "# header\n\n"
            "- **C-1 -- the bound rule contradicted the classifier. "
            "`addressed`.** The safety rule said a bound closes `partial`, "
            "in the section admitting the question is unresolved.\n"
            "- C-2, blocking: the same finding without a header of its own, "
            "and the wider gap is deferred.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-02-header/prd.md#C-2"],
            found.get("unresolved-concern"),
            "C-1 is disposed in its header; C-2 has none and defers",
        )
        self.assertEqual([], found.get("unreadable-concern-row", []))

    def test_a_header_that_defers_opens_a_row_whose_prose_says_parked(self) -> None:
        """The header decides whatever it says -- this is not a closing rule.

        Tier order is preserved *inside* the header, so a header carrying
        both a closing and an open token reads open exactly as a whole row
        does. Were the header only allowed to close a row, it would be a way
        to dispose of a concern by writing the right word first, which is the
        failure the tier order exists to prevent moved one level in.

        The row below is the case that separates the two rules: its header
        defers and its body mentions something parked. `parked` is the first
        tier, so the whole-row read calls it parked and a close-only header
        rule would leave it there. The row's own header says the remainder is
        deferred, and open is both the honest answer and the louder one.
        """
        self.ledger("2026-08-03-deferring/prd.md", (
            "# deferring\n\n"
            "- **C-1 -- addressed in part, the remainder deferred.** The "
            "corrected clause landed; the rest waits behind the parked "
            "refresh C-2 describes.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-03-deferring/prd.md#C-1"],
            found.get("unresolved-concern"),
        )
        self.assertEqual([], found.get("parked-concern", []))

    def test_the_verbs_that_were_measured_and_refused_stay_unreadable(self) -> None:
        """`added` and `verified` are the two the counts argue for and the
        reading argues against, so the refusal is written down as a test.

        Whole-corpus, `added` rescues 7 rows and `verified` 2, both without
        reclassifying anything today -- which is why a count alone cannot
        decide this. Read instead: `added` appears in 41 row extents, 32 of
        them already closed and 2 already open, and `verified` in 36. Both
        sentences below are real, from C-20 in the write-the-test-first item
        and C-183 in the solo-first one, and in both the word describes the
        defect rather than disposing of it. They must keep reporting as rows
        this reader cannot classify.

        This one guards against an addition rather than a reversion: it fails
        the day either word joins `CLOSED_WORDS` on the strength of its
        rescue count.
        """
        self.ledger("2026-08-01-refused/prd.md", (
            "# refused\n\n"
            "- C-1 found a no-evidence exit written into the gate that was "
            "added to close a no-evidence hole.\n"
            "- C-2, material: the commit that claimed `make check exit 0` was "
            "verified by the weaker of the two runs.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            [
                "docs/work/2026-08-01-refused/prd.md#C-1",
                "docs/work/2026-08-01-refused/prd.md#C-2",
            ],
            sorted(found.get("unreadable-concern-row", [])),
        )

    # -- `parked`, the word that is also a field name -----------------------

    def test_a_backticked_field_name_does_not_park_a_row(self) -> None:
        """`parked` is this corpus's one verdict that is also its own field
        name, and the two are told apart by where the code span sits.

        A verdict written as code ends its clause; a name written as code is
        a noun with the rest of its phrase still to come. Both rows below are
        real. The first is C-162 of the solo-first item, which named
        `bin/sd-docs-lint` among the readers of the `archived` and `parked`
        fields and was reported as waiting on a trigger nobody re-reads --
        the most expensive misread this section has, because a disposed row
        reads as live work. The second is C-17 of the write-the-test-first
        item, whose disposition *is* the backticked word, and it must stay
        parked: 3 of the 7 backticked `parked`s in the corpus are that shape,
        and ignoring matches inside code spans wholesale loses all three.

        Whole-corpus: 4 rows move, every one `parked` to `closed`, none is
        made unreadable and `open` moves by zero.
        """
        self.ledger("2026-08-04-naming/prd.md", (
            "# naming\n\n"
            "- C-1, minor: `bin/sd-docs-lint:87-91` was listed among the "
            "readers of the `archived` and `parked` fields. It reads "
            "neither, and the line range is unrelated. Corrected.\n"
            "- **C-2** (the handoff had no operative pre-fix step) -- "
            "`parked`. The seam it describes no longer exists.\n"
        ))
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-04-naming/prd.md#C-2"],
            found.get("parked-concern"),
            "C-1 names the field; C-2's whole disposition is the code span",
        )
        self.assertEqual([], found.get("unreadable-concern-row", []))

    def test_removed_is_a_disposition_this_reader_carries(self) -> None:
        """`removed` joins `CLOSED_WORDS`, and it comes in with the naming
        rule rather than on its own.

        C-162 of the solo-first item disposes itself `Removed from PR 2's
        reader enumeration`, and until the parked tier stopped reading the
        field name in its prose the row never reached this tier at all.
        Measured whole-corpus, its exposure is 16 row extents of 532 -- 13
        already closed, 1 parked, 2 nothing could read -- against `added`'s
        41 and `verified`'s 36, the figures that rejected those two. 1
        rescue, 0 reclassifications, 0 rows made unreadable.
        """
        self.ledger("2026-08-04-removed/prd.md", (
            "# removed\n\n"
            "- C-1, minor: the reader enumeration named a file that reads "
            "neither field. Removed from PR 2's enumeration.\n"
        ))
        self.assertEqual([], self.scan())

    # -- the keying ---------------------------------------------------------

    def test_one_c_id_in_two_items_is_two_concerns(self) -> None:
        """Keyed on the full path.

        A prototype keying on `split("/")[2]` collapsed 487 archived items
        into one bucket and lost `C-19` outright.
        """
        self.ledger("2026-08-01-alpha/prd.md", "# a\n\n- C-19: deferred.\n")
        self.ledger(
            "archive/2026-08/2026-08-02-beta/prd.md", "# b\n\n- C-19: deferred.\n"
        )
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/2026-08-01-alpha/prd.md#C-19",
             "docs/work/archive/2026-08/2026-08-02-beta/prd.md#C-19"],
            sorted(found.get("unresolved-concern", [])),
        )

    def test_the_same_concern_written_three_times_is_one_row(self) -> None:
        """Dedupe by `(file, C-id)`: three rows about one concern, one id."""
        self.ledger("2026-08-01-repeat/prd.md", (
            "# repeat\n\n"
            "| id | disposition |\n| --- | --- |\n| C-8 | deferred |\n\n"
            "- C-8 re-raised in a later pass, no new disposition.\n\n"
            "- **C-8** deferred again.\n"
        ))
        self.assertEqual(1, len(self.scan()))

    def test_an_archived_ledger_is_read_though_its_boxes_are_not(self) -> None:
        """The one deliberate exception to the archive exclusion.

        A parked security acceptance outlives the item it was written in, so
        excluding it because its directory moved is the silent under-report
        this section exists to prevent.
        """
        self.ledger(
            "archive/2026-08/2026-08-26-adapter/prd.md",
            "# adapter\n\n- C-19 (high, ACCEPTED and parked): reads anywhere.\n",
        )
        found = self.checks(self.scan())
        self.assertEqual(
            ["docs/work/archive/2026-08/2026-08-26-adapter/prd.md#C-19"],
            found.get("parked-concern"),
        )


class AcceptedGapTests(InventoryFixture):
    """Step 3b: a written acceptance is listed, and is not an abnormality."""

    ENTRY = {
        "id": "required-checks",
        "state": "missing",
        "because": "the check is reported by an app nobody has installed",
        "since": "2026-07-01",
        "until": "the app is installed or the check is dropped",
    }

    def rows_for(self, *entries: dict[str, str]) -> list[dict[str, Any]]:
        sections = self.sections(protection={
            "default_branch": "main", "gaps": [], "detail": {},
            "accepted": list(entries),
        })
        return [
            row for row in
            status.actionable_inventory(self.repo, sections, self.TODAY).rows
            if row["check"] == "accepted-gap-standing"
        ]

    def test_an_accepted_gap_is_listed_and_is_not_abnormal(self) -> None:
        """A decision is not a defect; re-flagging one is how a banner becomes
        noise. It is listed because `until` is prose nothing re-evaluates."""
        rows = self.rows_for(self.ENTRY)
        self.assertEqual(["required-checks"], [row["key"] for row in rows])
        self.assertFalse(rows[0]["abnormal"])
        self.assertEqual(45, rows[0]["rank"])
        self.assertIn("2026-07-01", rows[0]["detail"])
        self.assertIn("the app is installed", rows[0]["suggest"])

    def test_an_acceptance_until_never_lists_no_row(self) -> None:
        """`until: never` has no condition to re-read, so a reminder is noise.

        The entry still prints in Protection as accepted; only the
        re-read row goes. Case and spaces around the word are ignored.
        """
        rows = self.rows_for(
            {**self.ENTRY, "id": "unprotected", "until": " Never "},
            self.ENTRY,
        )
        self.assertEqual(["required-checks"], [row["key"] for row in rows])

    def test_an_accepted_gap_never_reaches_the_banner(self) -> None:
        inventory = status.actionable_inventory(
            self.repo,
            self.sections(protection={
                "default_branch": "main", "gaps": [], "detail": {},
                "accepted": [self.ENTRY],
            }),
            self.TODAY,
        )
        result = status.banner(inventory)
        self.assertEqual([], result["findings"])
        self.assertNotIn(
            "accepted-gap-standing", [row["check"] for row in result["classes"]]
        )


class ReportSectionTests(InventoryFixture):
    """The twelve headings, and the three the inventory feeds.

    Rendered rather than inspected. Step 1 through 3b built producers nothing
    printed, so every assertion up to here was about a structure; these are
    about the text an operator reads, which is where two defects that passed
    every structural test became visible at once.
    """

    def report(self, shown: dict[str, Any] | None = None, **overrides: Any) -> str:
        """The rendered report; `shown` replaces sections of the result the inventory does not read."""
        inventory = self.inventory(**overrides)
        result = {
            "repo": str(self.repo),
            "pack": {"root": str(self.repo), "branch": "main",
                     "head": "0000000", "dirty": False},
            "inventory": {"rows": inventory.rows, "unchecked": inventory.unchecked},
            "abnormalities": status.banner(inventory),
            "work": status.work_section(self.repo),
            "pull_requests": {"repo": "acme/widget", "pull_requests": [],
                              "available": False, "reason": "no gh"},
            "setup": {"mode": "full", "mode_error": "", "source": "",
                      "detected": 0, "entrypoints": {}},
            "protection": {"available": False, "reason": "no remote",
                           "gaps": [], "accepted": [], "detail": {}},
            "handoff": {"packet": {"pending": False, "detail": status.NO_PACKET}},
            "backends": [],
            "residue": [],
            "issues": {"available": False, "reason": "no index",
                       "needs_you": [], "other": []},
            "contributions": {"available": False, "reason": "no shared database", "rows": []},
            "jira": {"available": False, "reason": "no shared database yet",
                     "freshness": None, "rows": []},
        }
        result.update(shown or {})
        stream = io.StringIO()
        status.render(result, stream)
        return stream.getvalue()

    def filled(self) -> dict[str, Any]:
        """A row in every section the inventory does not feed, for `report(shown=...)`."""
        found = {"summary": {"landing": 0, "delivered": 0, "planned_this_week": 0, "blocked": 0, "unscheduled": 0,
                             "work": 0},
                 "landing": {"entries": [], "gate": None}, "delivered": [], "planned": [], "blocked": [],
                 "unscheduled": [], "work": []}
        url = "https://github.com/example/widget/pull/2"
        return {
            "views": {"available": True, "reason": "", "repos": [str(self.repo)], "companions_error": "",
                      "now": "2026-09-01T12:00:00Z", "sections": found},
            "operations": {"available": True, "reason": "", "host": "alpha", "findings": [
                {"id": 31, "repo": None, "title": "health-check-nightly: needs attention", "job": "health-check-nightly",
                 "finding": "disk: under 10% free"}],
                           "gate": {"holders": [], "waiters": [], "error": None, "slots": 2,
                                    "load": [1.0, 1.0, 1.0], "load_max": 40.0}},
            "contributions": {"available": True, "reason": "", "rows": [
                {"key": f"github:{url}", "url": url, "lane": "awaiting_you", "item_id": 14,
                 "local_status": "planning", "external_state": "open", "title": "Fold the widget",
                 "reasons": ["review requested"]}]},
            "pull_requests": {"repo": "example/widget", "available": True, "reason": "", "pull_requests": [{
                "number": 7, "title": "Teach the widget to fold", "head": "task/fold", "base": "main",
                "draft": False, "mergeable": "MERGEABLE", "merge_state": "CLEAN", "review_decision": "APPROVED",
                "behind_by": 0, "checks": {"success": 1}, "failing": []}]},
            "lane": {"available": True, "reason": "", "repository": "example/widget",
                     "entries": [{"item": 14, "status": "queued", "branch": "task/fold"}]},
            "issues": {"available": True, "reason": "", "freshness": None, "needs_you": [],
                       "other": [{"number": 3, "title": "Widget folds twice", "why": []}]},
            "jira": {"available": True, "reason": "", "rows": [
                {"url": "https://example.atlassian.net/browse/LOG-1", "state": "Open", "title": "Fold",
                 "reason": "assigned", "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}],
                     "freshness": {"state": "fresh", "last_success_at": "2026-09-01T11:00:00Z"}},
            "protection": {"available": True, "reason": "", "default_branch": "main", "protected": True,
                           "gaps": [{"id": "strict", "gap": "strict is off"}],
                           "accepted": [{"id": "reviews", "since": "2026-09-01", "because": "one maintainer",
                                         "until": "a second account exists"}],
                           "merge_settings": [{"id": "rebase_merge", "flagged": False, "value": "disallowed",
                                               "gap": ""}],
                           "detail": {}},
            "handoff": {"packet": {"pending": True, "age": "2 h", "item": "sd:14", "summary": "mid-fold",
                                   "directory": "/example.test/widget", "detail": "pending; load it"}},
            "backends": [{"backend": "codex", "command": "codex", "state": "present"}],
            "residue": [{"id": "hooks-path", "paths": [".githooks"], "why": "core.hooksPath names a hooks folder",
                         "remove": "git config --unset core.hooksPath"}],
        }

    @staticmethod
    def blocks(text: str) -> list[tuple[str, list[str]]]:
        """Each section's heading and the lines under it, the banner left out."""
        found: list[tuple[str, list[str]]] = []
        for line in text.splitlines():
            if line and not line[0].isspace() and not line.startswith("sd-status:"):
                found.append((line, []))
            elif found:
                found[-1][1].append(line)
        return found

    #: `<Section> (<count>)`; `unknown` is a count no read could establish.
    HEADING = re.compile(r"^[A-Z][^\n]* \((?P<count>[0-9]+|unknown)\)$")

    def test_every_section_is_a_table_under_a_counted_heading(self) -> None:
        """sd:5357: every section takes `sd today`'s layout, a counted heading, a header and a dash rule.

        Walked from the text, so a section added later is checked without an edit here.
        """
        self.item("2026-08-01-alpha", status="in_progress")
        found = self.blocks(self.report(shown=self.filled()))
        for heading, body in found:
            with self.subTest(heading=heading):
                match = self.HEADING.match(heading)
                self.assertIsNotNone(match, heading)
                self.assertGreater(int(match["count"]), 0)
                self.assertRegex(body[0], r"^  [A-Z]\S*( {2,}[A-Z]\S*)*$")
                self.assertRegex(body[1], r"^  -+( {2}-+)*$")
        self.assertEqual(self.ORDER, [re.sub(r" \([^)]*\)$", "", heading) for heading, _ in found])

    def test_a_section_nobody_could_read_counts_unknown_not_zero(self) -> None:
        """A failed read is an unknown, never an empty section."""
        found = dict(self.blocks(self.report()))
        for heading in ("Contributions, this repo, shared database order (unknown)", "Open pull requests (unknown)",
                        "Issues, this repo, shared database (unknown)",
                        "Jira, shared database, all repositories (unknown)", "Protection (unknown)",
                        "Work across the shared database (unknown)", "Operations, this machine (unknown)",
                        "Merge lane, this repo, shared database (unknown)"):
            self.assertIn(heading, found)
        for heading in found:
            self.assertRegex(heading, self.HEADING)
        self.assertIn("Resumable handoffs (0)", found)
        unread = {"handoff": {"packet": {"pending": False, "detail": "packet is unreadable: boom"}}}
        self.assertIn("\nResumable handoffs (unknown)\n  packet: packet is unreadable: boom\n", self.report(shown=unread))

    def test_open_pull_requests_and_backends_print_as_tables(self) -> None:
        """sd:5116: the two sections whose rows share one shape take `sd today`'s layout.

        A state too long for its cell, as an unreadable registry's reason is,
        prints whole under the table rather than cut.
        """
        pulls = {"repo": "acme/widget", "available": True, "reason": "", "pull_requests": [{
            "number": 7, "title": "Teach the widget to fold", "head": "task/fold", "base": "main",
            "draft": False, "mergeable": "MERGEABLE", "merge_state": "CLEAN", "review_decision": "APPROVED",
            "behind_by": 0, "checks": {"success": 1}, "failing": []}]}
        reason = "unreadable (no provider registry at /example.test/providers.yaml)"
        backends = [{"backend": "codex", "command": "codex", "state": "present"},
                    {"backend": "registry", "command": "", "state": reason}]
        text = self.report(shown={"pull_requests": pulls, "backends": backends})
        self.assertRegex(text, r"\nOpen pull requests \(1\)\n  PR +State +Review +Checks +Head +Base +Title\n  -+")
        self.assertRegex(text, r"\n  #7 +CLEAN +APPROVED +success:1 +task/fold +main +Teach the widget to fold\n")
        self.assertRegex(text, r"\nBackends \(2\)\n  Backend +State\n  -+  -+\n  codex +present\n")
        self.assertIn(f"\n  registry state: {reason}\n", text)

    #: `design.md`'s skeleton, verbatim. The three new sections lead because
    #: the report is read top-down and the judgement is what the reader came
    #: for; the eight below keep the order they already had.
    ORDER = [
        "Abnormalities", "Pending", "Next", "Work across the shared database", "Operations, this machine",
        "Open threads", "Work items",
        "Contributions, this repo, shared database order",
        "Open pull requests", "Merge lane, this repo, shared database", "Detected setup",
        "Issues, this repo, shared database",
        "Jira, shared database, all repositories", "Protection",
        "Resumable handoffs", "Backends", "Legacy residue",
    ]

    # -- the skeleton -------------------------------------------------------

    def test_every_heading_prints_in_order_when_there_is_nothing_to_report(
        self,
    ) -> None:
        """The skeleton is fixed, so a missing section is a missing section.

        A report whose sections appear only when non-empty cannot be read for
        absence: the reader cannot tell "nothing found" from "not looked at",
        which is the same distinction the banner's third state exists to make.
        """
        self.assertEqual(self.ORDER, self.headings(self.report()))

    def test_the_same_headings_print_in_the_same_order_with_findings(self) -> None:
        self.item("2026-08-01-alpha", status="in_progress")
        self.assertEqual(self.ORDER, self.headings(self.report()))

    # -- the summary sentence -----------------------------------------------

    def test_the_summary_does_not_call_checks_clear_beside_its_own_findings(
        self,
    ) -> None:
        """`all N checks clear` printed beside `1 finding` contradicts itself.

        The head and the tail describe the same thirteen classes. Saying every
        one is clear while naming a class that fired hands a skimmer the
        opposite of the answer, and the tail is the half people skim.
        """
        self.item("2026-08-01-alpha", status="in_progress")
        summary = status.banner(self.inventory())["summary"]
        self.assertIn("1 finding across 1 check;", summary)
        self.assertIn("the other 12 checks clear", summary)
        self.assertNotIn("all 13 checks clear", summary)

    def test_nothing_found_still_says_all_of_them_are_clear(self) -> None:
        summary = status.banner(self.inventory())["summary"]
        self.assertEqual("no findings; all 13 checks clear", summary)

    def test_a_blind_class_keeps_the_word_out_even_when_others_fired(self) -> None:
        """The never-say-clear rule outranks the new middle tail.

        A run with findings *and* a class nobody could ask must not print the
        word at all: `the other N checks clear` would count the blind class
        among the clear ones, which is the under-report the third state
        exists to prevent.
        """
        self.branch("feature", land=False)
        self.item("2026-08-01-alpha", status="in_progress", extra="branch: feature\n")
        self.item("2026-08-02-beta", status="in_progress")
        summary = status.banner(self.inventory(protection=self.BLIND))["summary"]
        self.assertNotIn("clear", summary)
        self.assertIn("could not run", summary)

    # -- the banner is the judgement, not the list --------------------------

    def body(self, text: str, name: str) -> list[str]:
        """The lines under the section headed `name`, whatever its count."""
        return next(lines for heading, lines in self.blocks(text) if re.sub(r" \([^)]*\)$", "", heading) == name)

    def test_a_class_over_the_cap_elides_and_says_how_many(self) -> None:
        """Four findings in one class print three rows and a count.

        The class row already carries the true total, so the elision is not
        a loss of information -- it is the difference between a banner and a
        listing, and `pending` is the listing.
        """
        for name in ("alpha", "beta", "gamma", "delta"):
            self.item(f"2026-08-01-{name}", status="in_progress")
        banner = self.body(self.report(), "Abnormalities")
        self.assertRegex("\n" + "\n".join(banner), self.cells("in-progress-without-branch", "findings", "4"))
        listed = [line for line in banner if re.match(r"^  w[0-9a-f]{4,} +in-progress-without-branch ", line)]
        self.assertEqual(status.BANNER_LIMIT, len(listed))
        self.assertIn("  in-progress-without-branch: ... 1 more, ranked in `pending`", banner)

    def test_a_class_at_the_cap_elides_nothing(self) -> None:
        for name in ("alpha", "beta", "gamma"):
            self.item(f"2026-08-01-{name}", status="in_progress")
        self.assertNotIn("more, ranked in", "\n".join(self.body(self.report(), "Abnormalities")))

    def test_an_unchecked_class_counts_no_findings_and_names_its_reason(self) -> None:
        """A blind class is `unchecked` with `-`, never `clear` or 0, and its reason prints whole."""
        banner = self.body(self.report(**{"protection": self.BLIND}), "Abnormalities")
        self.assertRegex("\n" + "\n".join(banner), self.cells("protection-gap", "unchecked", "-"))
        self.assertTrue(any(line.startswith("  protection-gap: unchecked: ") for line in banner), banner)

    # -- one row, three renderings ------------------------------------------

    def test_next_names_the_first_row_of_pending_and_not_a_fourth_judgement(
        self,
    ) -> None:
        """C-3, rendered. The id in `next` is an id in `pending`.

        Three independent producers would give one fact three ids; one
        producer with three views gives it one, and this is the assertion
        that says the views did not drift apart.
        """
        # Three rows, not one. With a single row every candidate for "the
        # row `next` names" is the same row, so a renderer reaching for the
        # second would still pass -- which is exactly what this test did
        # until the breakage that was supposed to fail it did not.
        self.item("2026-08-01-alpha", status="in_progress")
        self.item("2026-08-02-beta", status="in_progress")
        self.item("2026-08-03-gamma", status="in_progress")
        rows = self.inventory().rows
        self.assertGreater(len(rows), 1)
        text = self.report()
        pending = self.body(text, "Pending")
        following = "\n".join(self.body(text, "Next"))
        self.assertEqual(rows[0]["id"], pending[2].split()[0])
        self.assertEqual(rows[0]["id"], following.splitlines()[2].split()[0])
        self.assertIn(" ".join(rows[0]["suggest"].split()), following)
        for other in rows[1:]:
            self.assertNotIn(other["id"], following)

    def test_pending_states_the_denominator_it_capped_against(self) -> None:
        """A list that elides in silence is worse than no list."""
        for index in range(status.PENDING_LIMIT + 3):
            self.item(f"2026-08-{index + 1:02d}-item", status="in_progress")
        text = self.report()
        self.assertIn("\nPending (13)\n", text)
        pending = self.body(text, "Pending")
        self.assertIn(f"  {status.PENDING_LIMIT} of 13, by rank", pending)
        self.assertEqual(status.PENDING_LIMIT, len([line for line in pending if re.match(r"^  w[0-9a-f]{4,} ", line)]))

    def test_open_threads_counts_every_row_the_inventory_carries(self) -> None:
        """The per-source counts are a partition, not a sample.

        If they summed to less than the inventory, `pending`'s cap would have
        a denominator that understated what was elided.
        """
        self.item("2026-08-01-alpha", status="in_progress")
        self.item("2026-08-02-beta", status="planning")
        rows = self.inventory().rows
        threads = self.body(self.report(), "Open threads")
        counted = sum(
            int(line.rsplit(" ", 1)[1])
            for line in threads
            if line.startswith("  ") and line.rsplit(" ", 1)[-1].isdigit()
        )
        self.assertEqual(len(rows), counted)

    # -- widths are measured, never typed -----------------------------------

    def test_the_check_column_is_wide_enough_for_the_longest_check(self) -> None:
        """A hardcoded width lets one long name break every other line.

        `in-progress-without-branch` is 26 characters and the first draft of
        this renderer padded to 24, so that one row overflowed and every
        aligned line below it read as ragged.
        """
        self.assertEqual(
            max(len(kind.check) for kind in status.CLASSES), status.CHECK_WIDTH
        )
        banner = self.body(self.report(), "Abnormalities")
        labels = [line.index("clear") for line in banner
                  if line.startswith("  ") and line.rstrip().endswith("clear  0")]
        abnormal = sum(1 for kind in status.CLASSES if kind.abnormal)
        self.assertEqual(abnormal, len(labels))
        self.assertEqual(1, len(set(labels)))

class ActionsFlagTests(InventoryFixture):
    """`--actions`, and the two `--json` keys the report now carries.

    The flag exists because `pending` caps at ten and a caller that wants row
    eleven should not have to re-derive the list. So the thing under test is
    mostly that this surface is *not* the capped one.
    """

    def result(self, **overrides: Any) -> dict[str, Any]:
        inventory = self.inventory(**overrides)
        return {
            "actions": inventory.rows,
            "next": status.next_action(inventory.rows),
        }

    def actions_text(self, rows: list[dict[str, Any]]) -> str:
        stream = io.StringIO()
        status.render_actions(rows, stream)
        return stream.getvalue()

    def pending_text(self, rows: list[dict[str, Any]]) -> str:
        stream = io.StringIO()
        status._render_pending(rows, stream.write)
        return stream.getvalue()

    def test_the_count_on_the_first_line_equals_the_rows_below_it(self) -> None:
        """The acceptance criterion, in process rather than through a shell."""
        for index in range(6):
            self.item(f"2026-08-{index + 1:02d}-item", status="in_progress")
        rows = self.inventory().rows
        text = self.actions_text(rows)
        head = text.splitlines()[0]
        self.assertEqual(f"{len(rows)} actionable", head)
        self.assertEqual(
            len(rows),
            sum(1 for line in text.splitlines()
                if re.match(r"^[a-z][0-9a-f]{4,} ", line)),
        )

    def test_the_id_pattern_admits_a_widened_id(self) -> None:
        """`{4,}` and not `{4}`, which is what the criterion said.

        A collided id widens to eight hex digits by this design's own rule.
        The fixed-width form counted 120 of 122 in this repository and could
        never have passed here, so the criterion was wrong rather than the
        ids.
        """
        # `sd08e3f70` verbatim, one of the two this checkout actually widened,
        # and not a shortened stand-in: an id carrying seven hex digits passes
        # both assertions below while contradicting the docstring above them,
        # so the fixture has to carry the real width to be pinning anything.
        rows = [{"id": "sd08e3f70", "check": "parked-concern", "title": "t",
                 "suggest": "do the thing"}]
        line = self.actions_text(rows).splitlines()[1]
        self.assertTrue(re.match(r"^[a-z][0-9a-f]{4,} ", line))
        self.assertFalse(re.match(r"^[a-z][0-9a-f]{4} ", line))

    def test_a_widened_id_is_named_rather_than_changed_in_silence(self) -> None:
        """The mitigation `design.md:540` accepts the collision risk on.

        `_widen_collisions` has set `widened` since step 2 and no renderer read
        it, so a colliding id changed between runs with nothing in the text
        report saying why -- only `--json` carried the flag. The risk the
        design accepts is that "the survivor reverts to four, so its id
        changed"; the note is the whole reason that was acceptable.

        Two rows are forced to collide by giving them the same `(check, key)`
        under different data, which is what `_widen_collisions` keys on. One
        row would prove nothing: widening only happens to a pair.
        """
        rows = [
            {"id": "s0000", "check": "parked-concern", "title": "a",
             "suggest": "do a", "key": "same", "widened": True},
            {"id": "s0000", "check": "parked-concern", "title": "b",
             "suggest": "do b", "key": "same", "widened": True},
        ]
        for text in (self.actions_text(rows), self.pending_text(rows)):
            self.assertIn("widened to eight digits", text)
            self.assertIn("s0000", text)

    def test_no_note_is_printed_when_nothing_widened(self) -> None:
        """A line that always prints is not a signal."""
        rows = [{"id": "s0001", "check": "parked-concern", "title": "a",
                 "suggest": "do a", "key": "k", "widened": False}]
        self.assertNotIn("widened", self.actions_text(rows))

    def test_actions_is_not_capped_the_way_pending_is(self) -> None:
        """The whole point of the flag: `pending` elides, this does not."""
        for index in range(status.PENDING_LIMIT + 4):
            self.item(f"2026-08-{index + 1:02d}-item", status="in_progress")
        rows = self.inventory().rows
        self.assertGreater(len(rows), status.PENDING_LIMIT)
        listed = [line for line in self.actions_text(rows).splitlines()
                  if re.match(r"^[a-z][0-9a-f]{4,} ", line)]
        self.assertEqual(len(rows), len(listed))

    # -- the two keys -------------------------------------------------------

    def test_next_carries_the_id_it_belongs_to(self) -> None:
        """An object, not a string.

        A caller that acts on the suggestion needs the row it came from in
        the same breath; a bare sentence would make it look the id up again
        and could pick a different row than the one the report named.
        """
        self.item("2026-08-01-alpha", status="in_progress")
        self.item("2026-08-02-beta", status="in_progress")
        result = self.result()
        top = result["actions"][0]
        self.assertEqual(
            {"id": top["id"], "check": top["check"], "suggest": top["suggest"]},
            result["next"],
        )

    def test_next_is_none_rather_than_an_empty_object_when_nothing_is_open(
        self,
    ) -> None:
        """`null` says "no row"; `{}` would say "a row with no fields"."""
        self.assertEqual([], self.result()["actions"])
        self.assertIsNone(self.result()["next"])

    def test_actions_is_the_whole_inventory_and_not_a_second_producer(
        self,
    ) -> None:
        """C-3 again, at the `--json` boundary."""
        self.item("2026-08-01-alpha", status="in_progress")
        self.assertEqual(self.inventory().rows, self.result()["actions"])


class ActionsCliTests(StatusFixture):
    """The flag as a caller invokes it, including what wins against `--json`."""

    def test_json_wins_when_both_flags_are_given(self) -> None:
        """One output per run, and the machine-readable one is the wider.

        `--json` carries `actions` in full, so a caller passing both loses
        nothing by getting the object; the reverse would drop every other key.
        """
        self.item("2026-08-01-alpha", status="in_progress")
        completed = self.run_tool(SD_STATUS, "--json", "--actions")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(3, payload["schema"])
        self.assertTrue(payload["actions"])

    def test_the_actions_key_is_the_whole_inventory_not_the_pending_slice(
        self,
    ) -> None:
        """Through `collect()`, and with more rows than `pending` will show.

        `ActionsFlagTests` builds this key by hand to exercise the renderer,
        so nothing there can see a slice applied inside `collect()`: a
        deliberate `rows[:PENDING_LIMIT]` in the producer passed that whole
        class. It is the fixture defect the C-3 test had on step 4 wearing a
        different hat -- a fixture holding one row cannot tell an uncapped
        list from a capped one -- and the fix is the same, which is to hand
        the assertion more rows than the cap.
        """
        for index in range(status.PENDING_LIMIT + 4):
            self.item(f"2026-08-{index + 1:02d}-item", status="in_progress")
        payload = self.report()
        rows = payload["inventory"]["rows"]
        self.assertGreater(len(rows), status.PENDING_LIMIT)
        self.assertEqual(rows, payload["actions"])

    def test_the_flag_prints_the_list_and_nothing_else(self) -> None:
        self.item("2026-08-01-alpha", status="in_progress")
        completed = self.run_tool(SD_STATUS, "--actions")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.startswith("1 actionable\n"))
        for absent in ("abnormalities", "pending", "work items"):
            self.assertNotIn(f"\n{absent}\n", completed.stdout)


def library_unimportable(
    test: unittest.TestCase, modules: dict[str, None], *, provisioned: str
) -> None:
    """For the rest of `test`, `sd_db` will not import; `provisioned` names a `lib/` copy that raised.

    `sd-status` asks `sd_lib.import_sd_db`, whose answer is patched here, so
    the test does not depend on the checkout it runs in. The import prepends
    to `sys.path`, so a copy is patched in and nothing leaks into later tests.
    """
    answer = status.sd_lib.Imported(
        None, f"sd_db is provisioned at {provisioned} but will not import: blocked" if provisioned
        else "sd_db is not installed here: blocked", provisioned)
    test.enterContext(mock.patch.dict(sys.modules, modules))
    test.enterContext(mock.patch.object(status.sd_lib, "import_sd_db", return_value=answer))
    test.enterContext(mock.patch.object(sys, "path", list(sys.path)))


class ProvisionedButBrokenLibraryTests(StatusFixture):
    """sd:746 review: a copy `make setup` left that raises is a fault, not a gap.

    Both readers of the shared library used to answer "not installed" for it,
    which sends the reader to reinstall a library that is installed. They now
    make the same two-way call as `sd_codex` and `sd_restore`.
    """

    PROGRESS = {"sd_db": None, "sd_db.progress": None}
    CONTRIBUTIONS = {"sd_db": None, "sd_db.contributions": None}

    def assert_names_the_provisioned_copy(self, reason: str) -> None:
        self.assertIn("provisioned at /pack/site", reason)
        self.assertIn("will not import", reason)
        self.assertNotIn("not installed", reason, "the remedy for absent, sent to a broken copy")

    def test_the_issues_answer_names_the_broken_copy(self) -> None:
        library_unimportable(self, self.PROGRESS, provisioned="/pack/site")
        answer = status._database_issues("acme/widget")

        self.assertFalse(answer["available"])
        self.assert_names_the_provisioned_copy(answer["reason"])
        self.assertEqual((answer["needs_you"], answer["other"]), ([], []))

    def test_the_contributions_answer_names_the_broken_copy(self) -> None:
        library_unimportable(self, self.CONTRIBUTIONS, provisioned="/pack/site")
        answer = status.contributions_section(self.repo)

        self.assertFalse(answer["available"])
        self.assert_names_the_provisioned_copy(answer["reason"])
        self.assertEqual(answer["rows"], [])

    def test_nothing_provisioned_still_calls_contributions_not_installed(self) -> None:
        library_unimportable(self, self.CONTRIBUTIONS, provisioned="")
        answer = status.contributions_section(self.repo)

        self.assertEqual(answer["reason"], "shared contribution library is not installed; run `make setup` in the pack checkout")
        self.assertEqual(answer["rows"], [])


class SkillPageClaimTests(StatusFixture):
    """What `skills/sd-status/SKILL.md` says about the output, held to a run (sd:804).

    #940's review wrote nine mutations of that page -- a top-level `pending`
    key, three nested ones, a boolean called an integer, two symbols that do
    not exist -- and every test and `bin/sd-docs-lint` stayed green. The page
    is prose, so these read the claim out of it and measure the thing it is
    about: the keys from a real `--json` payload, the buckets from
    `bin/sd-pr-state`, the symbols from the two tools' sources.
    """

    @staticmethod
    def pending_keys(node: Any, path: str = "") -> list[tuple[str, Any]]:
        """Every `pending` key in `node`, as a dotted path with `[]` for a list."""
        found: list[tuple[str, Any]] = []
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else key
                if key == "pending":
                    found.append((here, value))
                found.extend(SkillPageClaimTests.pending_keys(value, here))
        elif isinstance(node, list):
            for value in node:
                found.extend(SkillPageClaimTests.pending_keys(value, f"{path}[]"))
        return found

    #: The `--json` paragraph's sentences on `pending`, pinned whole (sd:821
    #: N-1): #946's review made the packet boolean "say whether a check still
    #: runs", made the key absent "with a running check", and made both nested
    #: keys "this list", and each regex that read a slot out of these stayed
    #: green. The two tests below measure what each sentence says.
    KEYS = ("It has no top-level `pending` key, and the two nested ones are something "
            "else again: `handoff.packet.pending` is a boolean about the handoff packet, "
            "and `pull_requests.pull_requests[].checks.pending` an integer count of that "
            "pull request's checks that have not finished.")
    ABSENT = ("`rollup_buckets` writes only the buckets it saw, so with no such check "
              "the key is absent, not 0.")
    NOT_A_LIST = "Neither is this list."

    def running_and_done(self) -> None:
        """Two pull requests: one with a check still running, one with none."""
        pull = {"baseRefName": "main", "isDraft": False, "mergeable": "MERGEABLE",
                "mergeStateStatus": "BLOCKED", "reviewDecision": "",
                "headRepositoryOwner": {"login": "acme"}}
        self.with_github(pulls=[
            dict(pull, number=12, title="Running", headRefName="task/running",
                 statusCheckRollup=[{"name": "lint", "conclusion": "SUCCESS"},
                                    {"name": "build", "status": "IN_PROGRESS"}]),
            dict(pull, number=13, title="Done", headRefName="task/done",
                 statusCheckRollup=[{"name": "lint", "conclusion": "SUCCESS"}]),
        ])

    def test_the_pending_keys_the_page_names_are_the_ones_json_carries(self) -> None:
        """The census walks a payload with a handoff packet and a check still running.

        A second pull request has no such check, which is the shape the page
        says leaves the key out rather than writing 0. The report runs twice,
        without the packet and with it: the packet boolean is what moves, and
        the check count is what does not, which is what "about the handoff
        packet" means and what a boolean "saying whether a check still runs"
        would fail.
        """
        self.running_and_done()
        without = self.report()
        self.write_packet()
        payload = self.report()
        found = self.pending_keys(payload)
        nested = {path for path, _ in found if "." in path}
        self.assertEqual({"handoff.packet.pending",
                          "pull_requests.pull_requests[].checks.pending"}, nested,
                         "the census itself: a new pending key needs the page to name it")
        self.assertEqual(nested, {path for path, _ in self.pending_keys(without)})

        prose = _skill_says(self.KEYS, self.ABSENT, self.NOT_A_LIST)
        self.assertNotIn("pending", payload)
        self.assertEqual(nested, set(re.findall(r"`((?:[\w\[\]]+\.)+pending)`", self.KEYS)))
        count = re.search(r"the (\w+) nested ones", self.KEYS)
        assert count is not None
        self.assertEqual(len(nested), _number(count.group(1)))
        self.assertLess(prose.index("The `--json` schema is version"), prose.index(self.KEYS),
                        "the sentence is in the paragraph on the schema")

        kinds = {"boolean": bool, "integer": int}
        for path, value in found:
            said = re.search(rf"`{re.escape(path)}`(?: \w+)?? an? (\w+)", self.KEYS)
            assert said is not None, f"the page gives no type for {path}"
            self.assertIs(kinds.get(said.group(1)), type(value), path)
            self.assertNotIsInstance(value, list, path)

        def checks(report: dict[str, Any]) -> dict[int, dict[str, int]]:
            return {pr["number"]: pr["checks"] for pr in report["pull_requests"]["pull_requests"]}

        self.assertFalse(without["handoff"]["packet"]["pending"])
        self.assertTrue(payload["handoff"]["packet"]["pending"])
        self.assertEqual(checks(without), checks(payload))
        self.assertEqual({"success": 1, "pending": 1}, checks(payload)[12])
        self.assertNotIn("pending", checks(payload)[13])
        self.assertEqual({"success": 1}, checks(payload)[13])

    def test_the_states_the_page_says_count_as_pending_are_the_buckets(self) -> None:
        """`_BUCKETS` folds six GitHub states into `pending`, and the page says which is what.

        The page used to gloss the last three as "a check GitHub expects and
        has not heard from", which fits `EXPECTED` alone: a `WAITING` run
        exists and waits (sd:821 N-5). What is true is what `_outcome` reads.
        A check run's `conclusion` comes first, so its `status` counts only
        while there is none; a status context has only a `state`. Which enum
        each name belongs to is GitHub's schema, not this code: on 2026-09-14
        `CheckRun.status: CheckStatusState!` holds `QUEUED`, `IN_PROGRESS`,
        `WAITING`, `REQUESTED`, `PENDING` and `COMPLETED`, `CheckRun.conclusion`
        is nullable, and `StatusContext.state: StatusState!` holds `EXPECTED`
        and `PENDING` among the finished ones. That part is read here from the
        page's sentence and from the code, not from GitHub.
        """
        six = ("`_BUCKETS` in `bin/sd-pr-state` folds six states into it: `PENDING`, "
               "`QUEUED`, `IN_PROGRESS`, `WAITING`, `REQUESTED` and `EXPECTED`.")
        which = ("`QUEUED`, `IN_PROGRESS`, `WAITING` and `REQUESTED` are a check run's "
                 "`status`, which `_outcome` reads only while the run has no `conclusion`; "
                 "`EXPECTED` is a commit status GitHub expects and has not received; "
                 "`PENDING` is either.")
        prose = _skill_says(six, which)
        self.assertEqual(prose.index(six) + len(six) + 1, prose.index(which),
                         "the gloss follows the list it glosses")
        named = re.findall(r"`([A-Z_]+)`", six.partition(": ")[2])
        buckets = status.pr_state._BUCKETS
        self.assertEqual({state for state, bucket in buckets.items() if bucket == "pending"},
                         set(named))
        folds = re.search(r"folds (\w+) states", six)
        assert folds is not None
        self.assertEqual(len(named), _number(folds.group(1)))

        outcome = status.pr_state._outcome
        runs = re.findall(r"`([A-Z_]+)`", which.partition(" are a check run's")[0])
        self.assertEqual(4, len(runs))
        for state in runs:
            with self.subTest(state):
                self.assertEqual("pending", outcome({"name": "build", "status": state}))
                self.assertEqual("failure", outcome({"name": "build", "status": state,
                                                     "conclusion": "FAILURE"}),
                                 "a run with a conclusion is read by it, whatever its status")
        self.assertEqual("pending", outcome({"context": "ci/required", "state": "EXPECTED"}))
        self.assertEqual("pending", outcome({"context": "ci/required", "state": "PENDING"}))
        self.assertEqual("pending", outcome({"name": "build", "status": "PENDING"}))
        self.assertEqual(set(named), {*runs, "EXPECTED", "PENDING"})
        rollup = [{"name": state, "status": state} for state in named]
        self.assertEqual({"pending": len(named)}, status.pr_state.rollup_buckets(rollup))

    #: Every place the page gives `pending` its length, as the phrase that
    #: holds it: `{n}` for the digit, `{w}` for the word, `{W}` for the word
    #: starting a sentence. #946 chose five phrases and missed three sites,
    #: and sd:821's count of all of them identified none, so one site
    #: reworded to "twelve" and another given a second `ten` cancelled out
    #: with the suite green and the page telling a reader the list holds
    #: twelve (sd:836 N-1). Naming each site is what closes that: a phrase
    #: reworded is one that is no longer found, wherever the arithmetic
    #: lands. The limit is a field in every one, so raising the constant
    #: rewrites the whole enumeration and the page has to be re-read.
    TENS = (
        "at most {w} actionable rows by rank",
        "stating the denominator — `{n} of 123, by rank`",
        "{W} pending rows do not fit",
        "Rows 5 to {n} are addressable by typing the id",
        "`--json`: `pending` is the first {w} of it",
        "`pending` caps at {w} because a report is read whole",
        "of which `pending` is the first {w}",
        "It is capped at {w} and says so",
    )

    def test_every_ten_the_page_gives_pending_is_pending_limit(self) -> None:
        """Every ten on the page is the list's length, and each one is named.

        The sites are enumerated, not chosen, and now identified rather than
        counted: `TENS` names all eight, each phrase must be on the page
        exactly once, each must hold exactly one ten, and the positions they
        cover must be every ten the page carries. A total alone let one site
        say "twelve" while another grew a second `ten`, because the two
        cancelled (sd:836 N-1); a phrase that no longer reads as written
        fails wherever the count lands, and a fourteenth ten fails as one no
        phrase covers.

        The number-only table cells go first, because the class table ranks
        four checks at 10. Both regexes are below rather than in a shell
        approximation of them: sd:821's docstring gave a `sed` that consumed
        the closing pipe, so on a row `| 10 | 10 | x |` it left a ten standing
        that the lookahead here drops (sd:836 N-2).
        """
        limit = status.PENDING_LIMIT
        fields = {"n": limit, "w": _word(limit), "W": _word(limit).capitalize()}
        lines = [re.sub(r"\|\s*\d+\s*(?=\|)", "|", line)
                 for line in SKILL_MD.read_text(encoding="utf-8").splitlines()]
        prose = " ".join(" ".join(lines).split())
        ten = rf"(?<![\w#-])(?:{_word(limit)}|{limit})(?![\w-])"

        covered: list[int] = []
        for phrase in self.TENS:
            said = phrase.format(**fields)
            # Bounded, not a raw substring: "by rank" is inside "by ranking",
            # so a site reworded by attaching a word to its last one would
            # still be counted and would still hold its ten in place
            # (#965's review). The phrase has to end where the page ends it.
            bounded = rf"(?<![\w-]){re.escape(said)}(?![\w-])"
            whole = list(re.finditer(bounded, prose))
            with self.subTest(said):
                self.assertEqual(1, len(whole),
                                 f"{SKILL_MD.name} no longer says this once: {said!r}")
                at = whole[0].start()
                here = [at + m.start() for m in re.finditer(ten, said, re.IGNORECASE)]
                self.assertEqual(1, len(here), "the phrase names one ten, not several")
                covered.extend(here)
        self.assertEqual([m.start() for m in re.finditer(ten, prose, re.IGNORECASE)],
                         sorted(covered),
                         "every ten on the page is one of the sites TENS names")

    def test_the_rule_against_reading_pending_as_the_whole_list_is_a_run(self) -> None:
        """The page's rule on the cap, pinned whole and measured (sd:836 N-5).

        The sentence sat on the page with nothing reading it, and "and says
        so" inverted to "but never says so" was green and false. It claims
        three things at once -- the list stops at `PENDING_LIMIT`, its heading
        and the line under it carry the total, and the flags carry what the list drops
        -- so a pin alone would prove only that the page says them. Each is
        read here off a report with more rows than the list holds.
        """
        limit = status.PENDING_LIMIT
        rule = (f"It is capped at {_word(limit)} and says so; the total is in its heading "
                "and on the line under the rows, and `--actions` and `--json` carry the rest.")
        _skill_says(rule)

        for index in range(limit + 3):
            self.item(f"2026-08-{index + 1:02d}-item", status="in_progress")
        payload = self.report()
        rows = payload["inventory"]["rows"]
        self.assertGreater(len(rows), limit, "the list has to be short of the inventory")

        # "capped at ten": the list is the limit's length, not the inventory's.
        shown = rows[:limit]
        self.assertEqual(limit, len(shown))
        dropped = [row for row in rows if row not in shown]
        self.assertTrue(dropped, "the list has to drop rows for the rest to be carried")

        # "and says so; the total is in its heading and on the line under the rows".
        text = self.run_tool(SD_STATUS).stdout
        self.assertIn(f"\nPending ({len(rows)})\n", text)
        self.assertIn(f"\n  {limit} of {len(rows)}, by rank\n", text)

        # "`--actions` and `--json` carry the rest": both hold the held rows.
        actions = self.run_tool(SD_STATUS, "--actions").stdout
        for row in rows:
            self.assertIn(f"{row['id']} {row['check']}", actions)
        self.assertEqual(rows, payload["actions"])

    def test_every_symbol_the_page_cites_is_in_the_tools_it_describes(self) -> None:
        """A renamed function leaves the page citing nothing, and nothing said so.

        A backticked name with an underscore or in capitals must appear in
        `bin/sd-status`, `bin/sd-pr-state` or a tool the page names as
        `bin/...`; #946's version read two sources and failed a true citation
        of a name in the third tool (sd:821 N-2). A private name, or one the
        page places "in `bin/...`", must be defined there, not just mentioned.
        """
        prose = _skill_prose()
        tools = {"sd-status", "sd-pr-state", *re.findall(r"`bin/([\w-]+)`", prose)}
        # Reading the set the page names, rather than two fixed ones, makes a
        # tool it names but `bin/` does not hold a `FileNotFoundError` from
        # inside the comprehension -- an error naming a path, where what went
        # wrong is a page citing a tool that is gone (sd:836 N-7).
        self.assertEqual([], sorted(name for name in tools if not (BIN / name).is_file()),
                         "the page names a bin/ tool that is not in bin/")
        sources = {name: (BIN / name).read_text(encoding="utf-8") for name in sorted(tools)}

        def defines(source: str, name: str) -> bool:
            return bool(re.search(rf"^\s*(?:(?:def|class)\s+{name}\b|{name}\s*[:=])",
                                  source, re.MULTILINE))

        cited = {name for name in re.findall(r"`([A-Za-z_]\w*)`", prose)
                 if "_" in name or name.isupper()}
        self.assertTrue(any(name.startswith("_") for name in cited), "not vacuous")
        for name in sorted(cited):
            with self.subTest(name):
                self.assertTrue(any(re.search(rf"\b{name}\b", source)
                                    for source in sources.values()))
                if name.startswith("_"):
                    self.assertTrue(any(defines(source, name) for source in sources.values()))
        placed = re.findall(r"`([A-Za-z_]\w*)` in `bin/([\w-]+)`", prose)
        self.assertTrue(placed)
        for name, tool in placed:
            with self.subTest(f"{name} in bin/{tool}"):
                self.assertTrue(defines(sources[tool], name))


class MirrorSyncPendingTests(InventoryFixture):
    """`mirror-sync-pending`: the queue that is durable and otherwise invisible.

    A render enqueues an outward mirror because it cannot write one itself, and
    an agent session drains the queue later. Between those two moments the
    request is a file nobody looks at. These fixtures are what makes the
    report look at it.
    """

    def queue(self) -> pathlib.Path:
        """A queue directory of this test's own, and the environment naming it."""
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        return pathlib.Path(held.name)

    def request(self, queue: pathlib.Path, name: str, **fields: Any) -> pathlib.Path:
        path = queue / name
        body: dict[str, Any] = {
            "repo": str(self.repo),
            "document": "the delivery",
            "destination": "notion",
            "target": "the Research Notion space",
        }
        body.update(fields)
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def queued(
        self, queue: pathlib.Path, legacy: pathlib.Path | None = None,
    ) -> list[dict[str, Any]]:
        """Rows for these two queues, and for no directory outside the test.

        Both variables are always set. Leaving the legacy one unset would let
        the default reach `~/.claude/`, so a developer holding a real stranded
        queue would see this test report their own pending mirrors.
        """
        return self.by_check(status.actionable_inventory(
            self.repo, self.sections(), self.TODAY,
            environ={
                status.MIRROR_QUEUE: str(queue),
                status.LEGACY_MIRROR_QUEUE: str(
                    legacy if legacy is not None else queue / "absent"),
            },
        ).rows, "mirror-sync-pending")

    def test_a_queued_mirror_for_this_repository_is_a_row(self) -> None:
        queue = self.queue()
        self.request(queue, "widget.delivery.notion.json")
        rows = self.queued(queue)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "widget.delivery.notion.json")
        self.assertEqual(rows[0]["title"], "the delivery")
        self.assertIn("the Research Notion space", rows[0]["detail"])

    def test_a_drive_request_is_the_same_class_worded_for_drive(self) -> None:
        """One class for every destination. The request words its own, so this
        producer never carries a second copy of the destination table."""
        queue = self.queue()
        self.request(
            queue, "widget.delivery.drive.json",
            destination="drive", target="the Deliverables Drive folder",
        )
        row, = self.queued(queue)
        self.assertEqual(row["check"], "mirror-sync-pending")
        self.assertIn("the Deliverables Drive folder", row["detail"])

    def test_a_request_under_the_queue_s_former_name_is_still_a_row(
            self) -> None:
        """The rename must not hide work the upgrade left behind.

        A machine that rendered before the queue was renamed holds requests
        under the old name. Reading only the new one makes them durable and
        invisible, which is the condition this whole class exists to end."""
        queue = self.queue()
        legacy = self.queue()
        self.request(legacy, "widget.delivery.notion.json")
        rows = self.queued(queue, legacy)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "widget.delivery.notion.json")

    def test_both_queues_report_together(self) -> None:
        """A migration may be half done, or not started. Either way every
        request owed is a row, wherever it is sitting."""
        queue = self.queue()
        legacy = self.queue()
        self.request(queue, "widget.delivery.drive.json",
                     destination="drive", target="the Deliverables Drive folder")
        self.request(legacy, "widget.delivery.notion.json")
        rows = self.queued(queue, legacy)
        self.assertEqual(
            sorted(row["key"] for row in rows),
            ["widget.delivery.drive.json", "widget.delivery.notion.json"])

    def test_one_directory_under_both_names_is_read_once(self) -> None:
        """A machine that pointed the old variable at the new directory has
        one queue, not two, and one request there is one piece of work."""
        queue = self.queue()
        self.request(queue, "widget.delivery.notion.json")
        self.assertEqual(len(self.queued(queue, queue)), 1)

    def test_looking_creates_neither_directory(self) -> None:
        """A report that provisions a queue would invent the work it reports."""
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        root = pathlib.Path(held.name)
        self.assertEqual(self.queued(root / "current", root / "legacy"), [])
        self.assertEqual(sorted(p.name for p in root.iterdir()), [])

    def test_one_document_designated_twice_is_two_rows(self) -> None:
        """Either mirror can drain while the other waits, so each is its own
        piece of outstanding work and its own id."""
        queue = self.queue()
        self.request(queue, "widget.delivery.notion.json")
        self.request(
            queue, "widget.delivery.drive.json",
            destination="drive", target="the Deliverables Drive folder",
        )
        rows = self.queued(queue)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["id"] for row in rows}), 2)

    def test_a_request_with_no_destination_still_names_the_document(self) -> None:
        """The fallback is for a request written before `target` existed; a row
        with a vague destination beats no row at all."""
        queue = self.queue()
        path = queue / "widget.old.json"
        path.write_text(json.dumps(
            {"repo": str(self.repo), "document": "the delivery"}), encoding="utf-8")
        row, = self.queued(queue)
        self.assertIn("an unnamed destination", row["detail"])

    def test_a_request_naming_another_checkout_is_that_checkouts_row(self) -> None:
        """The queue is machine-wide; this tool reports on one repository."""
        queue = self.queue()
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        self.request(queue, "other.delivery.notion.json", repo=elsewhere.name)
        self.assertEqual(self.queued(queue), [])

    def test_an_unreadable_request_is_skipped_rather_than_diagnosed(self) -> None:
        """A row about it would fire every run and name no action this tool may take."""
        queue = self.queue()
        (queue / "truncated.json").write_text("{not json", encoding="utf-8")
        (queue / "a-list.json").write_text("[]", encoding="utf-8")
        self.request(queue, "widget.delivery.notion.json")
        self.assertEqual([row["key"] for row in self.queued(queue)],
                         ["widget.delivery.notion.json"])

    def test_an_absent_queue_is_quiet(self) -> None:
        """The default is a machine-wide path most repositories never write to."""
        queue = self.queue() / "never-created"
        self.assertEqual(self.queued(queue), [])

    def test_an_empty_queue_is_quiet(self) -> None:
        self.assertEqual(self.queued(self.queue()), [])

    def test_a_queued_mirror_is_work_outstanding_and_not_a_defect(self) -> None:
        """Not abnormal, so it never reaches the banner; rank puts it below the defects."""
        queue = self.queue()
        self.request(queue, "widget.delivery.notion.json")
        row = self.queued(queue)[0]
        self.assertFalse(row["abnormal"])
        self.assertEqual(row["rank"], 75)
        self.assertEqual(row["source"], "~/.claude/pending-mirror-syncs/")

    def test_the_request_that_has_waited_longest_leads(self) -> None:
        """Age carries the urgency, because the class itself is never abnormal."""
        queue = self.queue()
        fresh = self.request(queue, "a-fresh.json", document="fresh")
        stale = self.request(queue, "z-stale.json", document="stale")
        # Local noon, not UTC midnight: the producer reads the mtime back with
        # `date.fromtimestamp`, which is local, so a midnight fixture lands on
        # the day before west of Greenwich and the age is off by one there.
        when = datetime.datetime(2026, 8, 24, 12, 0).timestamp()
        os.utime(stale, (when, when))
        os.utime(fresh, None)
        rows = self.queued(queue)
        self.assertEqual([row["title"] for row in rows], ["stale", "fresh"])
        self.assertEqual(rows[0]["age_days"], 14)

    def test_the_skill_table_carries_this_class(self) -> None:
        """`skills/sd-status/SKILL.md` mirrors `CLASSES` word for word."""
        kind = status.BY_CHECK["mirror-sync-pending"]
        skill = BIN.parent / "skills" / "sd-status" / "SKILL.md"
        table = skill.read_text(encoding="utf-8")
        self.assertIn(
            f"| {kind.rank} | `{kind.check}` | `{kind.letter}` | no | "
            f"`{kind.source}` | {kind.what} |",
            table,
        )


class RulesetProtectionCase(unittest.TestCase):
    """`protection_section` when classic protection is a 404 and a ruleset applies.

    Measured on example-website 2026-09-22 (sd:1323): the section read
    `branches/main/protection` alone and reported a branch enforcing four
    ruleset rules as `protected: false`. It now reads `rules/branches/main`
    after the 404 and, when an active ruleset carries a merge-gating rule,
    runs the same gap analysis over the object `sd_protection.synthesize`
    shapes from it. A ruleset that gates no merge (`deletion` alone) keeps
    the `unprotected` finding, with the rules named in `detail`.
    """

    SLUG = "acme/widget"
    GH = {"available": True, "slug": SLUG, "reason": ""}
    RULESET = {"id": 42, "name": "main", "enforcement": "active", "bypass_actors": []}
    #: An operator's own repository: `admin`, so a 404 on classic protection
    #: is GitHub saying there is none, not that this token may not look.
    REPO = {"default_branch": "main", "allow_rebase_merge": False, "permissions": {"admin": True},
            "squash_merge_commit_title": "PR_TITLE", "squash_merge_commit_message": "PR_BODY"}

    @staticmethod
    def gating_rules() -> list[dict[str, Any]]:
        return [
            {"type": "deletion", "ruleset_id": 42},
            {"type": "pull_request", "ruleset_id": 42,
             "parameters": {"required_approving_review_count": 1}},
            {"type": "required_status_checks", "ruleset_id": 42,
             "parameters": {"strict_required_status_checks_policy": True,
                            "required_status_checks": [{"context": "lint", "integration_id": 7}]}},
        ]

    def section(self, rules: Any, ruleset: Any = RULESET, *, repo: dict[str, Any] | None = None,
                extra: dict[int, Any] | None = None,
                classic: str = "gh: Branch not protected (HTTP 404)",
                accepted: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
        seen: list[str] = []

        def answer(args: list[str], root: pathlib.Path) -> tuple[Any, str]:
            seen.append(args[1])
            url = urlsplit(args[1])
            path, query = url.path, parse_qs(url.query)
            if path == f"repos/{self.SLUG}":
                return dict(self.REPO if repo is None else repo), ""
            if path.endswith("/branches/main/protection"):
                return None, classic
            if path.endswith("/rules/branches/main"):
                if rules is None:
                    return None, "gh: Not Found (HTTP 404)"
                # Paged the way the endpoint pages: `per_page` and `page`,
                # thirty a page when neither is asked.
                size, page = int(query.get("per_page", ["30"])[0]), int(query.get("page", ["1"])[0])
                return rules[(page - 1) * size:page * size], ""
            if path.endswith("/rulesets/42"):
                return (ruleset, "") if ruleset is not None else (None, "gh: Not Found (HTTP 404)")
            for ruleset_id, entry in (extra or {}).items():
                if path.endswith(f"/rulesets/{ruleset_id}"):
                    return entry, ""
            raise AssertionError(f"unexpected read {path}")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                status.pr_state, "gh_json", answer):
            home = pathlib.Path(directory) / "home"
            if accepted:
                seed_declaration(home, pathlib.Path(directory), "status", {"accepted_gaps": list(accepted)})
            with mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}):
                result = status.protection_section(pathlib.Path(directory), self.GH)
        result["_seen"] = seen
        return result

    def test_a_runner_merged_repository_reports_zero_approvals_as_an_ok_flag(self) -> None:
        """sd:3497: one maintainer cannot approve their own pull request.

        Under `repo.runner_merge = auto` the runner merges, so zero approvals is
        the setting, not a gap. A runner-merge read that cannot tell keeps the gap.
        """
        import sd_lane  # noqa: PLC0415

        rules = self.gating_rules()
        rules[1]["parameters"]["required_approving_review_count"] = 0
        with mock.patch.object(sd_lane, "default_runner_merge", return_value="auto"):
            result = self.section(rules)
        self.assertNotIn("reviews", [gap["id"] for gap in result["gaps"]])
        self.assertIn({"id": "reviews", "value": "0 approvals; repo.runner_merge is auto, the runner merges",
                       "flagged": False, "gap": ""}, result["merge_settings"])
        for answer in ({"return_value": "manual"}, {"side_effect": sd_lane.LaneError("the hub is down")}):
            with mock.patch.object(sd_lane, "default_runner_merge", **answer):
                result = self.section(rules)
            self.assertIn("reviews", [gap["id"] for gap in result["gaps"]], answer)

    def test_ci_local_expects_the_local_gate_in_place_of_workflow_contexts(self) -> None:
        """sd:1843: a `repo.ci = local` repository produces `sd/local-gate` and
        nothing else, so requiring `lint` is `required_not_produced` and the
        gate is the context it should require."""
        with mock.patch.object(status.sd_lib, "ci_mode", lambda root: "local"):
            result = self.section(self.gating_rules())
        self.assertEqual(result["detail"]["produced_contexts"], ["sd/local-gate"])
        self.assertEqual(result["detail"]["required_not_produced"], ["lint"])
        self.assertEqual(result["detail"]["produced_not_required"], ["sd/local-gate"])
        self.assertIn("repo.ci is local", " ".join(result["detail"]["workflow_notes"]))

    def test_ci_github_leaves_the_produced_contexts_to_the_workflows(self) -> None:
        with mock.patch.object(status.sd_lib, "ci_mode", lambda root: "github"):
            result = self.section(self.gating_rules())
        self.assertEqual(result["detail"]["produced_contexts"], [])
        self.assertNotIn("repo.ci", " ".join(result["detail"]["workflow_notes"]))

    def test_a_ruleset_that_gates_the_merge_is_reported_as_protection(self) -> None:
        result = self.section(self.gating_rules())
        self.assertTrue(result["protected"])
        ids = [gap["id"] for gap in result["gaps"]]
        self.assertNotIn("unprotected", ids)
        self.assertNotIn("reviews", ids)
        self.assertNotIn("strict", ids)
        self.assertNotIn("enforce_admins", ids)
        self.assertEqual(result["detail"]["required_contexts"], ["lint"])
        self.assertEqual(result["detail"]["source"], "ruleset")
        self.assertEqual(result["detail"]["rulesets"],
                         [{"id": 42, "name": "main", "enforcement": "active", "bypass_actors": [],
                           "rules": ["pull_request", "required_status_checks"]}])
        self.assertEqual(result["_seen"][-2:], [status.sd_protection.rules_path(f"repos/{self.SLUG}", "main", 1),
                                                f"repos/{self.SLUG}/rulesets/42"])

    def test_every_page_of_rules_reaches_the_gap_analysis(self) -> None:
        """Thirty rules fill the endpoint's first page. The gating rules on
        the second must reach the analysis, or the branch reads as
        `unprotected` -- the weaker picture, the wrong direction."""
        size = status.sd_protection.PAGE_SIZE
        filler = [{"type": "tag_name_pattern", "ruleset_id": 42, "parameters": {"pattern": f"v{n}"}}
                  for n in range(size)]
        result = self.section(filler + self.gating_rules())
        self.assertTrue(result["protected"])
        self.assertNotIn("unprotected", [gap["id"] for gap in result["gaps"]])
        self.assertEqual(result["detail"]["required_contexts"], ["lint"])
        rules_reads = [path for path in result["_seen"] if "/rules/branches/" in path]
        self.assertEqual(rules_reads, [status.sd_protection.rules_path(f"repos/{self.SLUG}", "main", 1),
                                       status.sd_protection.rules_path(f"repos/{self.SLUG}", "main", 2)])

    def test_a_bypass_that_reaches_administrators_is_enforce_admins_off(self) -> None:
        """`OrganizationAdmin` is the actor an administrator merges as, so its
        bypass is the classic finding, in the classic words."""
        bypass = dict(self.RULESET, bypass_actors=[{"actor_id": 1, "actor_type": "OrganizationAdmin",
                                                    "bypass_mode": "always"}])
        result = self.section(self.gating_rules(), bypass)
        self.assertTrue(result["protected"])
        gaps = {gap["id"]: gap["gap"] for gap in result["gaps"]}
        self.assertIn("main (#42) [pull_request, required_status_checks] by OrganizationAdmin 1 (always)",
                      gaps["enforce_admins"])
        self.assertIn("Every rule below stops collaborators and exempts the admins who do the merging",
                      gaps["enforce_admins"])
        self.assertNotIn("bypass", gaps)
        self.assertFalse(result["detail"]["enforce_admins"])
        self.assertEqual(result["detail"]["bypass"], [])
        self.assertEqual(result["detail"]["admin_bypass"],
                         ["main (#42) [pull_request, required_status_checks]: OrganizationAdmin 1 (always)"])
        self.assertEqual(result["detail"]["rulesets"][0]["bypass_actors"], bypass["bypass_actors"])

    def test_a_bypass_on_one_ruleset_leaves_the_other_rulesets_rules_binding(self) -> None:
        """GitHub layers rulesets: a bypass on the review ruleset exempts
        its holder from the review rule and from nothing the checks ruleset
        requires. Folded into one boolean, the sentence said "every rule
        below ... exempts the admins" of a branch whose CI requirement still
        bound them (Codex on #521, the system half of this change). Now it
        names each exempting ruleset with its actor and rules, then the
        rulesets still binding administrators, then the ones not known
        either way; "every rule below" only when neither is left."""
        admin = {"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}
        rules = [{"type": "deletion", "ruleset_id": 42},
                 {"type": "pull_request", "ruleset_id": 42, "parameters": {"required_approving_review_count": 1}},
                 {"type": "required_status_checks", "ruleset_id": 43,
                  "parameters": {"strict_required_status_checks_policy": True,
                                 "required_status_checks": [{"context": "lint", "integration_id": 7}]}}]
        review = dict(self.RULESET, bypass_actors=[admin])
        checks = {"id": 43, "name": "checks", "enforcement": "active"}
        for actors, expect, absent in (
                ([], "Still binding them: checks (#43) [required_status_checks].", "very rule below"),
                ([admin], "checks (#43) [required_status_checks] by OrganizationAdmin 1 (always). Every rule below",
                 "Still binding"),
                (None, "Not known either way: checks (#43) [required_status_checks]", "very rule below")):
            with self.subTest(actors=actors):
                shown = dict(checks, bypass_actors=actors) if actors is not None else checks
                result = self.section(rules, review, extra={43: shown})
                self.assertTrue(result["protected"])
                gaps = {gap["id"]: gap["gap"] for gap in result["gaps"]}
                self.assertNotIn("bypass", gaps)
                self.assertIn("enforce_admins is off on main: main (#42) [pull_request] by OrganizationAdmin 1 (always)",
                              gaps["enforce_admins"])
                self.assertIn(expect, gaps["enforce_admins"])
                self.assertNotIn(absent, gaps["enforce_admins"])
                self.assertFalse(result["detail"]["enforce_admins"])
                self.assertIn("main (#42) [pull_request]: OrganizationAdmin 1 (always)", result["detail"]["admin_bypass"])
                self.assertEqual([entry["rules"] for entry in result["detail"]["rulesets"]],
                                 [["pull_request"], ["required_status_checks"]])

    def test_a_role_bypass_is_unknown_until_the_role_is_confirmed(self) -> None:
        """A `RepositoryRole` actor carries a numeric id, and which role it
        names is confirmed nowhere here -- no ruleset a registered token can
        read carries one. Guessing the admin id would pick which of two
        sentences an operator reads, and the wrong guess prints
        "administrators stay subject" on a branch they can walk past. So it
        is `enforce_admins` unknown, naming the role, in neither sentence."""
        for role_id in (5, 4):
            with self.subTest(role_id=role_id):
                actor = {"actor_id": role_id, "actor_type": "RepositoryRole", "bypass_mode": "always"}
                result = self.section(self.gating_rules(), dict(self.RULESET, bypass_actors=[actor]))
                self.assertTrue(result["protected"])
                gaps = {gap["id"]: gap["gap"] for gap in result["gaps"]}
                self.assertNotIn("bypass", gaps)
                self.assertIn(f"main (#42) [pull_request, required_status_checks] lets RepositoryRole {role_id} "
                              "(always) bypass it", gaps["enforce_admins"])
                self.assertIn("not confirmed here", gaps["enforce_admins"])
                self.assertNotIn("exempts the admins", gaps["enforce_admins"])
                self.assertNotIn("stay subject", gaps["enforce_admins"])
                self.assertFalse(result["detail"]["enforce_admins"])
                self.assertEqual(result["detail"]["bypass"], [])
                self.assertEqual(status.sd_protection.synthesize(self.gating_rules(), {42: dict(
                    self.RULESET, bypass_actors=[actor])})["enforce_admins"], {"enabled": None})

    def test_a_bypass_for_one_app_is_its_own_gap_and_administrators_stay_subject(self) -> None:
        """Codex on #1140: an app's bypass printed as `enforce_admins` off,
        "exempts the admins who do the merging", on a repository whose
        administrators are subject to every rule. The section reports it as
        the `bypass` gap naming the actor, with `enforce_admins` on."""
        bypass = dict(self.RULESET, bypass_actors=[{"actor_id": 77, "actor_type": "Integration", "bypass_mode": "pull_request"}, {"actor_id": 9, "actor_type": "Team", "bypass_mode": "always"}])
        result = self.section(self.gating_rules(), bypass)
        self.assertTrue(result["protected"])
        gaps = {gap["id"]: gap["gap"] for gap in result["gaps"]}
        self.assertNotIn("enforce_admins", gaps)
        self.assertIn("main (#42) [pull_request, required_status_checks]: Integration 77 (pull_request); "
                      "main (#42) [pull_request, required_status_checks]: Team 9 (always)", gaps["bypass"])
        self.assertIn("administrators stay subject to main (#42) [pull_request, required_status_checks]", gaps["bypass"])
        self.assertTrue(result["detail"]["enforce_admins"])
        self.assertEqual(result["detail"]["bypass"],
                         ["main (#42) [pull_request, required_status_checks]: Integration 77 (pull_request)",
                          "main (#42) [pull_request, required_status_checks]: Team 9 (always)"])
        self.assertEqual(result["detail"]["admin_bypass"], [])
        # A withheld list beside a shown one stays unknown: the app's bypass
        # is reported, and so is the list nobody was shown.
        hidden = {"id": 43, "name": "ops", "enforcement": "active"}
        rules = self.gating_rules() + [{"type": "pull_request", "ruleset_id": 43,
                                        "parameters": {"required_approving_review_count": 1}}]
        result = self.section(rules, bypass, extra={43: hidden})
        gaps = {gap["id"]: gap["gap"] for gap in result["gaps"]}
        self.assertIn("did not show bypass_actors for ops (#43) [pull_request]", gaps["enforce_admins"])
        self.assertIn("Integration 77 (pull_request)", gaps["bypass"])
        # The withheld ruleset is not one administrators are known to stay subject to.
        self.assertIn("administrators stay subject to main (#42) [pull_request, required_status_checks].", gaps["bypass"])

    def test_a_bypass_list_not_shown_is_unknown_not_enforcement(self) -> None:
        """Absent `bypass_actors` is what GitHub answers a caller who cannot
        edit the ruleset. The section reports it as the `enforce_admins` gap
        naming the ruleset that withheld its list, never as enforced; `[]`
        beside it stays enforced, so the two states do not collapse."""
        result = self.section(self.gating_rules(), {"id": 42, "name": "main", "enforcement": "active"})
        self.assertTrue(result["protected"])
        gaps = {gap["id"]: gap["gap"] for gap in result["gaps"]}
        self.assertIn("enforce_admins", gaps)
        self.assertIn("did not show bypass_actors for main (#42)", gaps["enforce_admins"])
        self.assertNotIn("exempts the admins", gaps["enforce_admins"])
        self.assertFalse(result["detail"]["enforce_admins"])
        self.assertIsNone(result["detail"]["rulesets"][0]["bypass_actors"])
        shown = self.section(self.gating_rules())
        self.assertNotIn("enforce_admins", [gap["id"] for gap in shown["gaps"]])
        self.assertTrue(shown["detail"]["enforce_admins"])

    def test_no_acknowledgement_accepts_an_unknown_enforce_admins(self) -> None:
        """sd:2755: `observed_state` read unknown as `False`, so an entry
        written for `enforce_admins` off also silenced the unknown gap. No
        entry for the gap applies while its fact is unknown, by any pin: off,
        the admin exemptions alone, or a fact unrelated to admins."""
        withheld = {"id": 42, "name": "main", "enforcement": "active"}
        role = dict(self.RULESET, bypass_actors=[
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}])
        pins = {"off": {"enforce_admins": False, "admin_bypass": []},
                "exemptions only": {"admin_bypass": []}, "unrelated": {"strict": True}}
        for unknown, ruleset in (("withheld", withheld), ("role", role)):
            for pinned, state in pins.items():
                with self.subTest(unknown=unknown, pinned=pinned):
                    entry = {"id": "enforce_admins", "state": state, "because": "admins ship the release bump",
                             "since": "2026-10-05", "until": "the bypass is gone"}
                    result = self.section(self.gating_rules(), ruleset, accepted=(entry,))
                    self.assertEqual(result["accepted"], [])
                    stale = [gap for gap in result["gaps"] if gap["id"] == "enforce_admins"][0]
                    self.assertIn("unknown", stale["acknowledgement_stale"])

    def test_a_ruleset_that_gates_no_merge_keeps_the_unprotected_finding(self) -> None:
        rules = [{"type": "deletion", "ruleset_id": 42}, {"type": "non_fast_forward", "ruleset_id": 42}]
        result = self.section(rules)
        self.assertFalse(result["protected"])
        self.assertIn("unprotected", [gap["id"] for gap in result["gaps"]])
        self.assertEqual(result["detail"]["ruleset_rules"], ["deletion", "non_fast_forward"])
        self.assertEqual(result["detail"]["read_error"], "gh: Branch not protected (HTTP 404)")

    def test_a_ruleset_that_is_not_active_is_no_protection(self) -> None:
        result = self.section(self.gating_rules(), dict(self.RULESET, enforcement="evaluate"))
        self.assertFalse(result["protected"])
        self.assertIn("unprotected", [gap["id"] for gap in result["gaps"]])
        self.assertEqual(result["detail"]["ruleset_rules"], [])

    def test_a_404_from_a_token_without_admin_is_unknown_not_unprotected(self) -> None:
        """GitHub answers 404 `Not Found` on classic protection to a caller
        without `admin` on the repository, whether or not the branch is
        protected -- measured 2026-09-22 on home-assistant/core and
        gohugoio/hugo, both protected. So a 404 is "no protection" only when
        the token administers the repository; otherwise the classic side is
        unknown, the section says why, and no `unprotected` finding is raised
        on it. `permissions` missing altogether is the same unknown."""
        for permissions in ({"admin": False}, {"push": True}, None):
            with self.subTest(permissions=permissions):
                repo = dict(self.REPO)
                repo.pop("permissions")
                if permissions is not None:
                    repo["permissions"] = permissions
                result = self.section([], repo=repo)
                self.assertTrue(result["available"])
                self.assertIsNone(result["protected"])
                self.assertNotIn("unprotected", [gap["id"] for gap in result["gaps"]])
                self.assertIn("admin", result["reason"])
                self.assertIn(self.SLUG, result["reason"])
                self.assertEqual(result["detail"]["classic_visibility"], "hidden")
                self.assertEqual(result["detail"]["ruleset_rules"], [])
                # The rulesets were still read: they are visible without admin.
                self.assertTrue(any("/rules/branches/" in path for path in result["_seen"]))

    def test_a_classic_read_that_failed_is_unknown_not_unprotected(self) -> None:
        """Only a 404 says "no protection". A 403, a 5xx or a transport fault
        is a read that failed, and before sd:1000 it took the same branch as
        the 404: `unprotected` raised on no evidence, then moved to
        `accepted` by the repository's standing acknowledgement."""
        for error in ("gh: Resource not accessible by integration (HTTP 403)",
                      "gh: Server Error (HTTP 502)", "error connecting to api.github.com"):
            with self.subTest(error=error):
                result = self.section([], classic=error)
                self.assertIsNone(result["protected"])
                self.assertNotIn("unprotected", [gap["id"] for gap in result["gaps"]])
                self.assertEqual(result["accepted"], [])
                self.assertIn(error, result["reason"])
                self.assertEqual(result["detail"]["classic_visibility"], "unread")

    def test_a_gating_ruleset_is_protection_even_when_classic_is_hidden(self) -> None:
        """The rules endpoint answers a non-admin (log-distiller, read with
        `maintain`), so a ruleset that gates the merge is the protection
        object whatever the classic endpoint would not show."""
        result = self.section(self.gating_rules(), repo=dict(self.REPO, permissions={"admin": False}))
        self.assertTrue(result["protected"])
        self.assertEqual(result["detail"]["source"], "ruleset")
        self.assertEqual(result["detail"]["classic_visibility"], "hidden")

    def test_no_rules_is_unprotected_and_says_the_rules_were_read(self) -> None:
        result = self.section([])
        self.assertFalse(result["protected"])
        self.assertIn("unprotected", [gap["id"] for gap in result["gaps"]])
        self.assertEqual(result["detail"]["ruleset_rules"], [])
        self.assertNotIn("rules_read_error", result["detail"])

    def test_an_acknowledged_unprotected_branch_whose_rules_were_read_is_accepted(self) -> None:
        """The control for the test below: rules read as `[]` and an admin's
        classic 404 are evidence, so the acknowledgement applies."""
        result = self.section([], accepted=(ProtectionSectionTests.UNPROTECTED,))
        self.assertFalse(result["protected"])
        self.assertEqual([entry["id"] for entry in result["accepted"]], ["unprotected"])

    def test_rules_that_cannot_be_read_are_named_not_assumed_absent(self) -> None:
        """sd:1000. An admin's classic 404 says only that classic protection
        is absent; a ruleset may still gate the merge. A rules read that
        failed was reported as `unprotected`, which a standing acknowledgement
        then moved to `accepted`: a read failure shown as an accepted state."""
        result = self.section(None, accepted=(ProtectionSectionTests.UNPROTECTED,))
        self.assertIsNone(result["protected"])
        self.assertNotIn("unprotected", [gap["id"] for gap in result["gaps"]])
        self.assertEqual(result["accepted"], [])
        self.assertIn("gh: Not Found (HTTP 404)", result["reason"])
        self.assertEqual(result["detail"]["rules_read_error"], "gh: Not Found (HTTP 404)")



class BaselineFlagCase(unittest.TestCase):
    """The fleet baseline's two flags, as `sd_db.protection` writes them (sd:1807).

    The dashboard showed `protection_source` (classic protection alone, no
    repository ruleset: sd:5406) and `required_check` (`ci`, or `sd/local-gate`
    under `repo.ci = local`) for every owned repository; `sd-status` printed
    neither. They ride in `merge_settings` beside the merge flags, so no
    acknowledgement reaches them, and a repository outside the configured
    owners carries neither.
    """

    SLUG = "platypeeps/widget"
    GH = {"available": True, "slug": SLUG, "reason": ""}
    REPO = RulesetProtectionCase.REPO
    CLASSIC = {"enforce_admins": {"enabled": True},
               "required_status_checks": {"strict": True, "contexts": ["CI Result"]},
               "required_pull_request_reviews": {"required_approving_review_count": 1}}

    @staticmethod
    def rules(context: str) -> list[dict[str, Any]]:
        return [{"type": "pull_request", "ruleset_id": 42, "parameters": {"required_approving_review_count": 1}},
                {"type": "required_status_checks", "ruleset_id": 42,
                 "parameters": {"strict_required_status_checks_policy": True,
                                "required_status_checks": [{"context": context}]}}]

    def flags(self, classic: dict[str, Any] | str | None, rules: list[dict[str, Any]] | str, *, ci: str = "github",
              owners: tuple[str, ...] = ("platypeeps",), admin: bool = True) -> dict[str, dict[str, Any]]:
        repo = dict(self.REPO, permissions={"admin": admin})

        def answer(args: list[str], root: pathlib.Path) -> tuple[Any, str]:
            path = urlsplit(args[1]).path
            if path == f"repos/{self.SLUG}":
                return dict(repo), ""
            if path.endswith("/branches/main/protection"):
                if isinstance(classic, str):
                    return None, classic
                return (classic, "") if classic is not None else (None, "gh: Branch not protected (HTTP 404)")
            if path.endswith("/rules/branches/main"):
                return (None, rules) if isinstance(rules, str) else (rules, "")
            if path.endswith("/rulesets/42"):
                return RulesetProtectionCase.RULESET, ""
            raise AssertionError(f"unexpected read {path}")

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(status.pr_state, "gh_json", answer), \
                mock.patch.object(status.sd_lib, "ci_mode", lambda root: ci), \
                mock.patch.object(status, "_baseline_owners", lambda: (owners, "")):
            section = status.protection_section(pathlib.Path(directory), self.GH)
        return {flag["id"]: flag for flag in section["merge_settings"]}

    def test_classic_alone_requiring_ci_raises_neither_flag(self) -> None:
        """sd:5406: the fleet baseline is classic protection and no repository ruleset."""
        flags = self.flags(dict(self.CLASSIC, required_status_checks={"strict": True, "contexts": ["ci"]}), [])
        self.assertEqual({key: (flags[key]["value"], flags[key]["flagged"]) for key in status.BASELINE_FLAG_IDS},
                         {"protection_source": ("classic", False), "required_check": ("ci", False)})

    def test_a_ruleset_alone_raises_the_source(self) -> None:
        flags = self.flags(None, self.rules("ci"))
        self.assertEqual((flags["protection_source"]["value"], flags["protection_source"]["flagged"]), ("ruleset", True))
        self.assertIn("classic protection and no repository ruleset", flags["protection_source"]["gap"])
        self.assertEqual((flags["required_check"]["value"], flags["required_check"]["flagged"]), ("ci", False))

    def test_classic_protection_without_ci_raises_the_check(self) -> None:
        flags = self.flags(self.CLASSIC, [])
        self.assertEqual((flags["protection_source"]["value"], flags["protection_source"]["flagged"]), ("classic", False))
        self.assertEqual((flags["required_check"]["value"], flags["required_check"]["flagged"]), ("CI Result", True))
        self.assertIn("`ci` is not a required check", flags["required_check"]["gap"])

    def test_combined_protection_raises_the_source(self) -> None:
        flag = self.flags(self.CLASSIC, self.rules("ci"))["protection_source"]
        self.assertEqual((flag["value"], flag["flagged"]), ("combined", True))

    def test_a_classic_object_that_gates_nothing_beside_a_ruleset_still_raises_the_source(self) -> None:
        """The layered result reads `ruleset`, and a ruleset is not the baseline."""
        flag = self.flags({"enforce_admins": {"enabled": True}}, self.rules("ci"))["protection_source"]
        self.assertEqual((flag["value"], flag["flagged"]), ("ruleset, beside a classic object", True))

    def test_classic_beside_a_ruleset_that_gates_nothing_raises_the_source(self) -> None:
        """`combine` keeps the classic object when no ruleset rule gates a merge,
        so `source` reads classic; the active ruleset still stands beside it."""
        rules = [{"type": "deletion", "ruleset_id": 42}, {"type": "non_fast_forward", "ruleset_id": 42}]
        flag = self.flags(self.CLASSIC, rules)["protection_source"]
        self.assertEqual((flag["value"], flag["flagged"]), ("classic, beside a ruleset", True))

    def test_classic_with_a_failed_rules_read_raises_the_source(self) -> None:
        """A rules read that failed is no evidence that no ruleset stands: not a pass."""
        flag = self.flags(self.CLASSIC, "gh: Server Error (HTTP 502)")["protection_source"]
        self.assertEqual((flag["value"], flag["flagged"]), ("classic, rulesets unread", True))
        self.assertIn("unknown", flag["gap"])

    def test_a_local_ci_repository_is_judged_on_the_local_gate(self) -> None:
        self.assertTrue(self.flags(None, self.rules("ci"), ci="local")["required_check"]["flagged"])
        flag = self.flags(None, self.rules("sd/local-gate"), ci="local")["required_check"]
        self.assertFalse(flag["flagged"])
        self.assertIn("`sd/local-gate`", flag["gap"])

    def test_an_unprotected_branch_raises_both_with_none(self) -> None:
        flags = self.flags(None, [])
        self.assertEqual({key: (flags[key]["value"], flags[key]["flagged"]) for key in status.BASELINE_FLAG_IDS},
                         {"protection_source": ("none", True), "required_check": ("none", True)})

    def test_another_owners_repository_carries_neither(self) -> None:
        flags = self.flags(self.CLASSIC, [], owners=("example-corp",))
        self.assertEqual(sorted(flags), ["rebase_merge", "squash_message"])

    def test_unknown_protection_carries_neither(self) -> None:
        """A classic 404 to a token without admin is unknown: no verdict on the baseline either."""
        self.assertEqual(sorted(self.flags(None, [], admin=False)), ["rebase_merge", "squash_message"])

    def test_a_failed_classic_read_beside_a_ruleset_carries_neither(self) -> None:
        """The system files a classic read that failed as unknown, so the dashboard
        shows no baseline flag; `ruleset` here would be a verdict on no evidence."""
        flags = self.flags("gh: Server Error (HTTP 502)", self.rules("ci"))
        self.assertEqual(sorted(flags), ["rebase_merge", "squash_message"])

    def test_neither_flag_is_acknowledgeable(self) -> None:
        self.assertEqual(sorted(set(status.BASELINE_FLAG_IDS) & set(status.ACKNOWLEDGEABLE_GAPS)), [])


class ClassicAndRulesetProtectionCase(unittest.TestCase):
    """`protection_section` when classic answers 200 *and* a ruleset applies.

    Before sd:1419 the rulesets were read only after a classic 404, so a
    ruleset beside classic protection was invisible. They are read beside it
    now and layered, strictest per rule. One source keeps today's output;
    more than one reports `source: combined` and a `sources` map (Q4).
    """

    SLUG = RulesetProtectionCase.SLUG
    GH = RulesetProtectionCase.GH
    RULESET = RulesetProtectionCase.RULESET
    REPO = RulesetProtectionCase.REPO
    INTEGRATION = {"actor_id": 77, "actor_type": "Integration", "bypass_mode": "pull_request"}

    @staticmethod
    def classic(**overrides: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "enforce_admins": {"enabled": True},
            "required_status_checks": {"strict": True, "contexts": ["lint"]},
            "required_pull_request_reviews": {"required_approving_review_count": 1},
        }
        record.update(overrides)
        return record

    def section(self, classic: dict[str, Any], rules: list[dict[str, Any]],
                ruleset: dict[str, Any] = RULESET) -> dict[str, Any]:
        def answer(args: list[str], root: pathlib.Path) -> tuple[Any, str]:
            path = urlsplit(args[1]).path
            if path == f"repos/{self.SLUG}":
                return dict(self.REPO), ""
            if path.endswith("/branches/main/protection"):
                return classic, ""
            if path.endswith("/rules/branches/main"):
                return rules, ""
            if path.endswith("/rulesets/42"):
                return ruleset, ""
            raise AssertionError(f"unexpected read {path}")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
                status.pr_state, "gh_json", answer):
            return status.protection_section(pathlib.Path(directory), self.GH)

    @staticmethod
    def ids(result: dict[str, Any]) -> list[str]:
        """The gap ids, less the one every fixture carries: the temporary
        root has no workflows, so `lint` is required and never produced."""
        return [gap["id"] for gap in result["gaps"] if gap["id"] != "required_not_produced"]

    def test_classic_alone_reports_exactly_what_it_did(self) -> None:
        result = self.section(self.classic(), [])
        self.assertTrue(result["protected"])
        self.assertEqual(self.ids(result), [])
        for key in ("source", "sources", "rulesets", "bypass_info", "advisory", "classic_visibility"):
            self.assertNotIn(key, result["detail"])

    def test_both_with_the_same_rules_is_combined_and_names_each_rules_sources(self) -> None:
        result = self.section(self.classic(), RulesetProtectionCase.gating_rules())
        self.assertTrue(result["protected"])
        self.assertEqual(self.ids(result), [])
        self.assertEqual(result["detail"]["source"], "combined")
        self.assertEqual(result["detail"]["sources"], {"pull_request": ["classic", "ruleset:42"],
                                                       "required_status_checks": ["classic", "ruleset:42"]})
        self.assertTrue(result["detail"]["enforce_admins"])

    def test_a_bypass_beside_firm_classic_is_information_not_the_bypass_gap(self) -> None:
        """The acknowledgement fact `bypass` stays `[]`: an entry pinning it
        keeps matching, because nothing that fact names has changed."""
        result = self.section(self.classic(), RulesetProtectionCase.gating_rules(),
                              dict(self.RULESET, bypass_actors=[self.INTEGRATION]))
        self.assertNotIn("bypass", [gap["id"] for gap in result["gaps"]])
        self.assertEqual(result["detail"]["bypass"], [])
        self.assertEqual(result["detail"]["bypass_info"],
                         ["main (#42) [pull_request, required_status_checks]: Integration 77 (pull_request)"])

    def test_a_decisive_bypass_is_the_bypass_gap(self) -> None:
        classic = self.classic()
        del classic["required_status_checks"]
        result = self.section(classic, RulesetProtectionCase.gating_rules(),
                              dict(self.RULESET, bypass_actors=[self.INTEGRATION]))
        self.assertEqual(result["detail"]["bypass"],
                         ["main (#42) [pull_request, required_status_checks]: Integration 77 (pull_request)"])
        self.assertIn("bypass", [gap["id"] for gap in result["gaps"]])

    def test_a_stricter_bypassable_ruleset_is_advisory_and_leaves_the_gap_open(self) -> None:
        rules = RulesetProtectionCase.gating_rules()
        result = self.section(self.classic(required_status_checks={"strict": False, "contexts": ["lint"]}),
                              rules, dict(self.RULESET, bypass_actors=[self.INTEGRATION]))
        self.assertIn("strict", [gap["id"] for gap in result["gaps"]])
        self.assertFalse(result["detail"]["strict"])
        self.assertEqual(len(result["detail"]["advisory"]), 1)
        self.assertIn("[required_status_checks] asks for more than the firm sources",
                      result["detail"]["advisory"][0])

    def test_admins_exempt_on_classic_are_enforced_when_a_firm_ruleset_carries_every_rule(self) -> None:
        exempt = self.classic(enforce_admins={"enabled": False})
        result = self.section(exempt, RulesetProtectionCase.gating_rules())
        self.assertTrue(result["detail"]["enforce_admins"])
        self.assertNotIn("enforce_admins", [gap["id"] for gap in result["gaps"]])
        partial = [rule for rule in RulesetProtectionCase.gating_rules() if rule["type"] != "required_status_checks"]
        result = self.section(exempt, partial)
        self.assertFalse(result["detail"]["enforce_admins"])
        gaps = {gap["id"]: gap["gap"] for gap in result["gaps"]}
        self.assertIn("enforce_admins is off on main for required_status_checks", gaps["enforce_admins"])
        self.assertIn("classic protection (enforce_admins off)", gaps["enforce_admins"])
        self.assertIn("Still binding them: pull_request.", gaps["enforce_admins"])


if __name__ == "__main__":
    unittest.main()
