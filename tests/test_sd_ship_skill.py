"""Canonical manual ship-sequence invariants supplement the executable tests.

`tests/test_sd_ship.py` exercises the actual CLI with real Git and a local
GitHub protocol server. These checks retain the prose guards for the manual
nine-step sequence, which also supports changes without a database item.

**Absence carries the weight.** A test that greps for a sentence passes
forever and catches nothing -- it breaks on a rewrite that preserves the
meaning and survives a rewrite that destroys it. So the checks aim at the
forbidden thing: a `--delete-branch` on a merge, a `sleep` or a loop in the
settle step, a `docs/spec/` update inside the sequence, a placeholder `Work:` line, a
review cap restated in the skill. Where presence must be asserted it anchors
on a flag or a command token (`--watch`, `--match-head-commit`,
`delete_branch_on_merge`) rather than on the sentence around it, or on a
value derived from `WORKFLOW.md` rather than typed here.

**Commands are parsed, not matched.** The backticked spans are extracted and
the ones that begin with `git` or `gh` are read as command lines. A deletion
flag added to the merge fails here; the paragraph that names
`--delete-branch` in order to forbid it does not, because it is not a
command.

Criterion 32's merge test goes one step further than reading. It takes the
merge command the file prescribes, fills in the head the lane reviewed, and
runs it against a model of GitHub's documented merge semantics -- `sha=`
given and the head moved since it was given is a refusal and no merge. The
control beside it strips `--match-head-commit` from the same command and
watches the same model merge the moved head, which is what makes the first
test mean anything: the flag is what refuses, not the model. Nothing here
reaches the network or invokes `gh`.

The reviewer-order comparison at the end overlaps `tests/test_sd_registry.py`
on purpose. That file keeps the two readers honest for the registry's own
reasons; criterion 32 names the same comparison as one of its four tests, and
both must pass.

The later classes read the same file for the row the merge writes: criterion
13's `sd-ship` half, and item B's clauses 7.18, 7.21 and 15.25. These remain claims about the manual sequence text. Three have a
mechanical form that survives a rewrite, and those carry the weight. A row written by hand rather than through `sd_db` is a pattern, not a
sentence. A reconciliation that calls a merge is a `gh pr merge` span in a
section that must hold none, checked with the same command parser that finds
the real merge in step 7. A cancel that opens a pull request is a `gh pr
create` where there must be none. The wordier assertions beside them -- that
`shipped_at` does not move on a second merge, that a slice merge writes no
status -- are anchored on the field and the status name rather than on the
prose around them, because those two tokens are what a rewrite has to keep.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import shlex
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_registry  # noqa: E402

SKILL = REPO_ROOT / "skills/sd-ship/SKILL.md"
WORKFLOW = REPO_ROOT / "WORKFLOW.md"
RULE = REPO_ROOT / ".claude/rules/sd-planning-adversarial-review.md"

SKILL_TEXT = SKILL.read_text(encoding="utf-8")

#: A `Work:` line standing in for an item that does not exist. The criterion
#: forbids the form, so the test names the form rather than a sentence about
#: it.
PLACEHOLDER_WORK = re.compile(
    r"Work:\s*\(?(?:none|n/?a|tbd|todo|pending|unknown|null|-|—)"
    r"(?=[\s`).,]|$)",
    re.IGNORECASE,
)

#: Anything that removes a branch, on either side. `--delete\b` covers
#: `--delete-branch` as well; both spellings are listed because a merge and a
#: push carry different ones and a reader should see both named.
DELETION = re.compile(
    r"--delete-branch|--delete\b|\bbranch\s+-[dD]\b|\bpush\s+\S+\s+:"
    r"|:refs/heads|-X\s*DELETE|--method\s+DELETE"
)

#: The only deletion commands the post-merge closeout may name: one exact
#: target each, never a pattern, never forced (sd:2452).
SANCTIONED_CLEANUP = frozenset({
    "git branch -d", "git branch -D",
    "git push --force-with-lease=<branch>:<recorded id> origin --delete <branch>",
    "git stash drop <selector>", "git update-ref -d <ref> <recorded id>",
    "git worktree remove <path>",
})

#: A settle step that re-asks. `--watch` is one wait; these are a loop.
POLLING = re.compile(r"\bsleep\b|\bwhile\b|\buntil\b|\bdone\b|\bwatch\s+-n\b")

#: A row reached past the library. `sd_db` owns every write to it, so a skill
#: that opens the file or spells the statement is describing a second writer,
#: which is the whole defect the single-writer rule exists to prevent.
RAW_WRITE = re.compile(
    r"\bsqlite3?\b|\bUPDATE\s+\w*item\b|\bINSERT\s+INTO\b|\.execute\(",
    re.IGNORECASE,
)

#: Words that would turn criterion 3's warning into a gate. The criterion is
#: explicit that `sd-ship` warns *and ships*, and a warning that holds the
#: commit is a different command from the specified one.
REFUSAL = ("refus", "block", "abort", "halt", "reject", "does not commit")


def section(text: str, heading: str) -> str:
    """One `## ` section of a markdown page, its heading excluded."""

    lines = text.splitlines()
    start = lines.index(heading) + 1
    end = start
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    return "\n".join(lines[start:end])


def sequence_section() -> str:
    return section(SKILL_TEXT, "## The sequence")


def executable_section() -> str:
    return section(SKILL_TEXT, "## Executable interface")


def reconcile_section() -> str:
    text = section(SKILL_TEXT, "## What a rerun reconciles")
    return text + "\n" + referenced_procedure(text, "recovery.md")


def referenced_procedure(text: str, filename: str) -> str:
    """Follow the procedure the entrypoint actually names, not an assumed copy."""
    paths = re.findall(r"`(skills/sd-ship/references/[^`]+)`", text)
    matches = [path for path in paths if pathlib.Path(path).name == filename]
    if len(matches) != 1:
        raise AssertionError(f"expected one {filename} reference, found {matches}")
    return (REPO_ROOT / matches[0]).read_text(encoding="utf-8")


def steps() -> dict[int, str]:
    """The numbered steps of the sequence, each with its continuation lines.

    A step owns the indented lines under it and stops at the next number or
    at the first unindented paragraph. That boundary is the point: the
    paragraph excluding a `docs/spec/` update from the sequence sits after
    step 9 and must not be read as part of it, or criterion 2's check would
    pass on a file that updated `docs/spec/` in step 9.
    """

    found: dict[int, str] = {}
    current: int | None = None
    for line in sequence_section().splitlines():
        started = re.match(r"^(\d+)\.\s", line)
        if started:
            current = int(started.group(1))
            found[current] = line
            continue
        if current is None:
            continue
        if not line.strip() or line.startswith((" ", "\t")):
            found[current] += "\n" + line
            continue
        current = None
    return found


def spans(text: str) -> list[str]:
    """Every backticked span, newlines and indentation collapsed."""

    return [
        " ".join(match.group(1).split())
        for match in re.finditer(r"`([^`]+)`", text, re.DOTALL)
    ]


def commands(text: str) -> list[str]:
    """The backticked spans that are command lines rather than names."""

    return [span for span in spans(text) if span.startswith(("git ", "gh "))]


def sentences(text: str) -> list[str]:
    """The text's sentences, wrapping collapsed.

    Coarse on purpose. What it buys is the difference between a file that
    states a property somewhere and a file that states it about the thing at
    hand: three tokens scattered over a step say much less than the same
    three in one sentence.
    """

    return re.split(r"(?<=\.)\s+", " ".join(text.split()))


def bullet_containing(text: str, needle: str) -> str:
    """The one `- ` bullet holding `needle`, with its wrapped lines.

    Narrower than the enclosing block on purpose. `WORKFLOW.md`'s Defaults
    list is one unbroken block, and reading the whole of it for the words
    before "repository" would collect "a repository you own" from three
    bullets away.
    """

    lines = text.splitlines()
    hit = next(i for i, line in enumerate(lines) if needle in line)
    start = hit
    while start >= 0 and not lines[start].startswith("- "):
        start -= 1
    if start < 0:
        raise AssertionError(f"{needle!r} is in no bullet")
    end = hit + 1
    while end < len(lines) and lines[end].startswith(" ") and lines[end].strip():
        end += 1
    return "\n".join(lines[start:end])


def sentences_with(text: str, *needles: str) -> list[str]:
    """The sentences holding all of `needles`.

    One sentence rather than one step, for the reason the push test already
    gives: three tokens scattered over a paragraph can each belong to some
    other claim, and a check that collects them anyway passes on a file that
    makes none of the claim at hand.
    """

    return [
        line
        for line in sentences(text)
        if all(needle in line for needle in needles)
    ]


def repositories_named(block: str) -> set[str]:
    """The repositories a `Needed-by:` rule applies to, read off the rule."""

    words = {
        match.group(1).lower()
        for match in re.finditer(r"\b([A-Za-z]+)\s+repository\b", block)
    }
    words -= {"the", "a", "an", "that", "this", "shared", "upstream"}
    if "pack" in block:
        words.add("pack")
    return words


def needed_by_values(block: str) -> set[str]:
    """The accepted trailer values, derived from whichever page is read.

    Every `<...>` collapses to one token so the item form compares equal
    across two pages that word the placeholder differently.
    """

    values: set[str] = set()
    for span in spans(block):
        match = re.match(r"Needed-by:\s*(.+)", span)
        if not match:
            continue
        for part in match.group(1).split("|"):
            values.add(re.sub(r"<[^>]+>", "<item>", part.strip()))
    return values


def code_row_cap() -> str:
    """The *code, before merge* cap, from the table that owns it."""

    for line in RULE.read_text(encoding="utf-8").splitlines():
        if line.startswith("| Development |") and "Code, before merge" in line:
            return line.rsplit("|", 2)[1].strip()
    raise AssertionError("the review table has no code-before-merge row")


def merge_argv(command: str, head: str) -> list[str]:
    """The prescribed merge, as argv, with `head` as the sha it names.

    The sha is substituted by position after `--match-head-commit` rather
    than by matching the placeholder's wording, so rewording
    `<the reviewed sha>` does not quietly stop the substitution and leave the
    test asserting against a literal nobody passes.
    """

    filled = re.sub(r"<[^>]+>", "X", command).replace("#N", "#1")
    argv = shlex.split(filled)
    if "--match-head-commit" in argv:
        argv[argv.index("--match-head-commit") + 1] = head
    return argv


def without_the_flag(argv: list[str]) -> list[str]:
    index = argv.index("--match-head-commit")
    return argv[:index] + argv[index + 2:]


class FakeGitHub:
    """GitHub's merge semantics, and no rule of its own.

    `PUT /repos/{owner}/{repo}/pulls/{n}/merge` takes an optional `sha=`;
    where it is given and the pull request's head is no longer that sha the
    answer is 405 and nothing merges. `gh pr merge --match-head-commit` is
    that parameter. The model knows nothing about reviews -- it cannot tell a
    reviewed head from any other -- which is what leaves the refusal
    attributable to the flag the skill prescribes.
    """

    def __init__(self, head: str) -> None:
        self.head = head
        self.merged: str | None = None

    def run(self, argv: list[str]) -> tuple[int, str]:
        named = None
        if "--match-head-commit" in argv:
            named = argv[argv.index("--match-head-commit") + 1]
        if named is not None and named != self.head:
            return 1, (
                f"failed to merge: the head is {self.head}, "
                f"not the {named} the call named"
            )
        self.merged = self.head
        return 0, "merged"


REVIEWED = "a" * 40
MOVED = "b" * 40


class TheSequenceParses(unittest.TestCase):
    """The controls. Every check below reads one of these three things, and
    a parser returning nothing would make all of them pass over any file."""

    def test_the_prose_count_matches_the_steps_that_exist(self) -> None:
        """The recital that went stale, made mechanical so it cannot again.

        A reviewer found "Eight steps." still standing in the sequence's own
        introduction after a ninth was added, and a case-sensitive grep for
        `eight step` had missed it. A recited number drifts; a derived one
        cannot, so the page is read for whichever number word it uses and that
        word has to be the count `steps()` finds.
        """

        words = ("zero", "one", "two", "three", "four", "five", "six",
                 "seven", "eight", "nine", "ten", "eleven", "twelve")
        expected = words[len(steps())]
        recited = [w for w in words if re.search(rf"\b{w}[- ]steps?\b",
                                                 SKILL_TEXT, re.IGNORECASE)]
        self.assertNotEqual(recited, [], "the page recites no step count at all")
        self.assertEqual(
            sorted(set(recited)), [expected],
            f"the page says {sorted(set(recited))} but there are {len(steps())} steps")

    def test_the_sequence_has_nine_numbered_steps(self) -> None:
        self.assertEqual(sorted(steps()), list(range(1, 10)))

    def test_every_step_has_a_body(self) -> None:
        for number, body in steps().items():
            self.assertGreater(len(body), 60, f"step {number} is a stub")

    def test_the_command_scan_reaches_the_real_commands(self) -> None:
        found = commands(sequence_section())
        self.assertIn("git fetch -p", found)
        self.assertTrue(
            any(span.startswith("gh pr merge") for span in found),
            "the merge command was not found in the sequence",
        )


class AChangeWithNoWorkItem(unittest.TestCase):
    """Criterion 2, all four clauses, against the file's own text."""

    def test_no_step_updates_the_spec(self) -> None:
        offenders = [n for n, body in steps().items() if "docs/spec/" in body]
        self.assertEqual(offenders, [], f"a docs/spec/ update is inside steps {offenders}")

    def test_the_file_still_says_where_the_spec_update_runs(self) -> None:
        """The other half: the token vanishing from the page entirely would
        satisfy the check above while telling the reader nothing."""

        outside = sequence_section()
        for body in steps().values():
            outside = outside.replace(body, "")
        self.assertIn("docs/spec/", outside)

    def test_no_placeholder_work_line_anywhere_in_the_file(self) -> None:
        self.assertEqual(PLACEHOLDER_WORK.findall(SKILL_TEXT), [])

    def test_the_placeholder_pattern_recognises_one(self) -> None:
        """A dead pattern would clear the check above on any file."""

        for form in ("Work: none", "Work: n/a", "Work: TBD", "Work: -"):
            self.assertTrue(PLACEHOLDER_WORK.search(form), form)
        self.assertIsNone(PLACEHOLDER_WORK.search("a `Work:` line resolving"))

    def test_the_work_line_is_stated_as_conditional_on_the_item(self) -> None:
        holding = [body for body in steps().values() if "Work:" in body]
        self.assertTrue(holding, "no step mentions the Work: line")
        for body in holding:
            self.assertTrue(
                any(mark in body for mark in ("absent", "omitted", "only when")),
                "the Work: line is described unconditionally",
            )

    def test_no_step_carries_a_branch_deletion_command(self) -> None:
        for number, body in steps().items():
            for command in commands(body):
                self.assertIsNone(
                    DELETION.search(command),
                    f"step {number} deletes a branch: {command}",
                )

    def test_the_deletion_pattern_recognises_the_forms(self) -> None:
        for form in (
            "gh pr merge --squash --delete-branch",
            "git push origin --delete topic",
            "git push origin :topic",
            "git branch -D topic",
        ):
            self.assertTrue(DELETION.search(form), form)
        for kept in ("git fetch -p", "git worktree list", "gh pr checks 1 --watch"):
            self.assertIsNone(DELETION.search(kept), kept)

    def test_the_last_step_leaves_the_remote_branch_to_the_repository(self) -> None:
        """Anchored on the setting's name, which is what does the removing."""

        self.assertIn("delete_branch_on_merge", steps()[9])

    def test_the_settle_step_issues_no_polling_loop(self) -> None:
        """Every backticked span, not only the command-shaped ones.

        A polling loop starts with `while`, so the command filter used
        elsewhere in this file walks straight past the shape this check
        exists to catch. It did, until a mutation put a loop in step 5 and
        only the wait-once test noticed.
        """

        for span in spans(steps()[5]):
            self.assertIsNone(POLLING.search(span), f"a loop in step 5: {span}")

    def test_the_settle_step_waits_once_with_watch(self) -> None:
        waits = [c for c in commands(steps()[5]) if c.startswith("gh pr checks")]
        self.assertEqual(len(waits), 1, f"step 5 issues {len(waits)} waits")
        self.assertIn("--watch", waits[0])

    def test_the_polling_pattern_recognises_a_loop(self) -> None:
        for form in (
            "while true; do gh pr checks 1; sleep 10; done",
            "sleep 30",
            "watch -n 5 gh pr checks 1",
        ):
            self.assertTrue(POLLING.search(form), form)
        self.assertIsNone(POLLING.search("gh pr checks 1 --watch"))


class PostMergeCloseout(unittest.TestCase):
    """Every merge gets an inventory and removes only the merged PR's safe targets.

    The operator authorized automatic cleanup on 2026-10-02 (sd:2452): branches,
    stashes, refs and stale worktrees go without asking once every safety
    condition holds, each after a recovery record.
    """

    def setUp(self) -> None:
        self.closeout = referenced_procedure(steps()[9], "post-merge-closeout.md")

    def test_every_merge_loads_closeout_without_requiring_a_cleanup_request(self) -> None:
        self.assertIn("After every confirmed in-scope merge", steps()[9])
        self.assertIn("Run this after every confirmed in-scope merge", self.closeout)
        self.assertIn("automatic cleanup of safe targets", self.closeout)
        for resource in ("refs", "branches", "stashes", "worktrees"):
            with self.subTest(resource=resource):
                self.assertIn(resource, steps()[9])

    def test_cleanup_is_automatic_only_inside_its_safety_and_recovery_boundaries(self) -> None:
        for token in ("authorized this cleanup on 2026-10-02", "full object ID", "sd task note",
                      "re-read it immediately before removal", "squash merge",
                      "dirty, active, locked, unrelated, primary, and serving",
                      "unique or unverified", "shared virtualenv/cache",
                      "index and untracked parents", "concurrent stash activity",
                      "another open PR", "`git status --porcelain` is empty",
                      "only build output or caches", "not under `~/repos`",
                      "Keep the target and report it", "Retention is not a failure",
                      "its index parent and its untracked parent", "The lease makes the delete refuse"):
            with self.subTest(boundary=token):
                self.assertIn(token, self.closeout)
        for command in commands(self.closeout):
            if DELETION.search(command) or "stash drop" in command or "worktree remove" in command:
                with self.subTest(command=command):
                    self.assertIn(command, SANCTIONED_CLEANUP)
        for line in self.closeout.splitlines():
            if "stash clear" in line or re.search(r"--force(?!-with-lease=<branch>:<recorded id>)", line):
                with self.subTest(line=line):
                    self.assertTrue(line.startswith("Never"), line)

    def test_threads_need_supported_dispositions_and_readback(self) -> None:
        for token in ("inline thread", "review-body", "all result pages", "late Copilot",
                      "durable successor",
                      "eligible inline thread", "read back", "leave the thread open",
                      "Authorized shipping closeout owns remote replies",
                      "That step remains local-only"):
            with self.subTest(boundary=token):
                self.assertIn(token, self.closeout)


class TheNeededByTrailer(unittest.TestCase):
    """Criterion 3: the warning path, the pass path, and one definition.

    The accepted forms and the three repositories are read out of
    `WORKFLOW.md` rather than typed here. A second, drifting definition in
    the skill is the defect this repository keeps finding, and a test holding
    its own copy of the answer cannot see it.
    """

    def setUp(self) -> None:
        self.policy = bullet_containing(
            WORKFLOW.read_text(encoding="utf-8"), "Needed-by:"
        )
        self.passage = next(
            body for body in steps().values() if "Needed-by:" in body
        )

    def test_the_policy_bullet_yields_something_to_compare(self) -> None:
        self.assertGreaterEqual(len(needed_by_values(self.policy)), 3)
        self.assertEqual(len(repositories_named(self.policy)), 3)

    def test_the_skill_accepts_the_forms_the_policy_defines(self) -> None:
        self.assertEqual(
            needed_by_values(self.passage), needed_by_values(self.policy)
        )

    def test_the_skill_names_the_repositories_the_policy_names(self) -> None:
        for repository in repositories_named(self.policy):
            self.assertIn(repository, self.passage)

    def test_a_missing_trailer_warns(self) -> None:
        self.assertTrue(re.search(r"\bwarn", self.passage), "nothing warns")

    def test_the_warning_does_not_hold_the_ship(self) -> None:
        lowered = self.passage.lower()
        for word in REFUSAL:
            self.assertNotIn(word, lowered, f"the warning became a gate: {word}")
        self.assertTrue(
            any(mark in lowered for mark in ("continues", "ships", "with the commit")),
            "the passage never says the run goes on",
        )

    def test_the_carried_trailer_passes_quietly(self) -> None:
        lowered = self.passage.lower()
        self.assertTrue(
            any(
                mark in lowered
                for mark in ("nothing said", "no warning", "says nothing", "silent")
            ),
            "the pass path is not stated",
        )


class ABranchAnotherPullRequestIsBasedOn(unittest.TestCase):
    """Step 6: the lane asks whether this branch is some open PR's base.

    Anchored on the query and on two absences, not on the paragraph. The
    query is `gh pr list` carrying `--base`, which is the whole of the
    question; a step that asked for open pull requests without scoping them
    to this branch would be asking a different one. The absences are that
    the step prescribes no merge -- it precedes the merge and must not
    duplicate it -- and that it refuses rather than warns, because a warning
    here is indistinguishable from no check at all.
    """

    def setUp(self) -> None:
        self.step = steps()[6]

    def test_the_step_asks_github_for_pull_requests_based_on_this_branch(self) -> None:
        asks = [
            c for c in commands(self.step)
            if c.startswith("gh pr list") and {"--base", "--state"} <= set(shlex.split(c))
        ]
        self.assertEqual(
            len(asks), 1, f"step 6 issues {len(asks)} base queries, not one"
        )
        words = shlex.split(asks[0])
        self.assertIn(
            "--base <this branch> ", asks[0] + " ",
            "the query asks about another branch's children, not this one's (sd:1002)")
        self.assertEqual(
            words[words.index("--state") + 1], "open",
            "the query is not scoped to open pull requests, so a merged or "
            "closed child would stop the run")

    def test_the_repair_is_a_rebase_and_not_only_a_retarget(self) -> None:
        """The defect a reviewer found in the first draft of this step.

        `gh pr edit --base` moves the pull request's metadata and nothing
        else. A child whose head descends from this branch keeps this branch's
        commits, so after the retarget its diff against the new base still
        re-proposes them and still conflicts with the squash -- the exact
        outcome the step was written to prevent, reached by following the
        step. The repair has to move the child's *commits*, so the step has to
        prescribe a rebase; asserting on the metadata command alone would pass
        the wrong instruction.
        """

        rebases = [c for c in commands(self.step) if c.startswith("git rebase")]
        self.assertEqual(
            len(rebases), 1,
            f"step 6 prescribes {len(rebases)} rebases; a retarget alone "
            "leaves the child carrying this branch's commits")
        self.assertIn("--onto", shlex.split(rebases[0]),
                      "the rebase does not drop this branch's commit range")

    def test_the_step_prescribes_no_merge(self) -> None:
        merges = [c for c in commands(self.step) if c.startswith("gh pr merge")]
        self.assertEqual(merges, [], "step 6 merges; the merge is step 7's")

    def test_a_non_empty_answer_stops_the_run(self) -> None:
        """Tied to the *result*, not to the word "refuse" in the heading.

        The first version of this asserted a refusal word anywhere in the
        step, which the heading supplies on its own -- so it would have passed
        a step that asked the question and then merged anyway. The refusal has
        to be predicated on the answer being non-empty, so both halves are
        required in one sentence.
        """

        stopping = ("ends the run", "stops the run", "refuse", "does not merge")
        tied = [
            line for line in sentences(self.step)
            if "non-empty" in line.lower()
            and any(word in line.lower() for word in stopping)
        ]
        self.assertTrue(
            tied, "step 6 never ties stopping the run to a non-empty answer")

    def test_a_failed_query_stops_the_run(self) -> None:
        stopping = ("stop", "ends the run", "refuse", "does not merge")
        tied = [
            line for line in sentences(self.step)
            if "fail" in line.lower()
            and "query" in line.lower()
            and any(word in line.lower() for word in stopping)
        ]
        self.assertTrue(tied, "step 6 treats an unanswered query as an empty answer")


class TheReviewedHead(unittest.TestCase):
    """Criterion 32: only a cleared reviewed head, or its verified fix, ships."""

    def merge_command(self) -> str:
        found = [c for c in commands(steps()[7]) if c.startswith("gh pr merge")]
        self.assertEqual(len(found), 1, f"step 7 prescribes {len(found)} merges")
        return found[0]

    def test_a_fix_gets_one_further_pass_over_its_own_diff(self) -> None:
        body = steps()[2]
        self.assertIn("diff", body)
        self.assertIn("since", body)
        self.assertIn("reviewed head", body)

    def test_the_skill_reads_that_cap_from_the_table_and_states_none(self) -> None:
        """The rule file owns the caps; a skill restating one is how the two
        drift. So the cap's own text, taken from the table, must not appear
        here, and step 2 must point at the file it comes from."""

        cap = code_row_cap()
        self.assertTrue(cap, "the code-before-merge row has no cap")
        self.assertNotIn(cap, SKILL_TEXT)
        self.assertIn(".claude/rules/sd-planning-adversarial-review.md", steps()[2])

    def test_each_code_row_states_the_limit_its_runtime_enforces(self) -> None:
        """sd:10215: one row stated 15 rounds, while a build run parks after
        3 reviews; a reader took one limit for the other. Each code row's
        cap is the number the code that enforces it holds."""

        import sd_ship_history

        runner = (REPO_ROOT / "lib/sd_db/build_run.py").read_text(encoding="utf-8")
        rounds = int(re.search(r"^REVIEW_ROUNDS = (\d+)$", runner, re.MULTILINE).group(1))
        caps = {line.split("|")[2].strip(): line.rsplit("|", 2)[1].strip()
                for line in RULE.read_text(encoding="utf-8").splitlines() if line.startswith("| Development | Code")}
        self.assertEqual(caps, {
            "Code, in a build run": f"{rounds + 1} reviews, then it parks",
            "Code, before merge": f"{sd_ship_history.AUTOMATIC_CODE_REVIEW_PASSES} `sd-ship` passes",
        })

    def test_the_push_refuses_a_head_the_local_review_has_not_cleared(self) -> None:
        """One sentence carries all three, rather than the step between them.

        Step 3 also says that `--match-head-commit` refuses the same head at
        GitHub, and that sentence holds a refusal and a sha of its own. Read
        token by token across the whole step it stands in for the local
        refusal and lets it be deleted unnoticed, which is what the first
        version of this test did.
        """

        carried = [
            line
            for line in sentences(steps()[3])
            if "refus" in line and "sha" in line and "cleared" in line
        ]
        self.assertTrue(
            carried,
            "no sentence in step 3 refuses a head the local review has not cleared "
            "and checks its sha",
        )

    def test_the_never_list_forbids_pushing_an_unseen_head(self) -> None:
        never = section(SKILL_TEXT, "## Never").lower()
        self.assertIn("never push a head the local lane has not seen", never)

    def test_a_moved_head_is_refused_by_the_merge_the_skill_prescribes(self) -> None:
        """The reviewed head is named in the call; the head moved after the
        review; the model refuses and merges nothing."""

        argv = merge_argv(self.merge_command(), REVIEWED)
        self.assertIn(REVIEWED, argv, "the merge call never names the reviewed head")
        github = FakeGitHub(head=MOVED)
        code, message = github.run(argv)
        self.assertNotEqual(code, 0, "the moved head merged")
        self.assertIsNone(github.merged)
        self.assertIn(REVIEWED, message)

    def test_the_same_merge_is_accepted_when_the_head_did_not_move(self) -> None:
        argv = merge_argv(self.merge_command(), REVIEWED)
        github = FakeGitHub(head=REVIEWED)
        code, _ = github.run(argv)
        self.assertEqual(code, 0)
        self.assertEqual(github.merged, REVIEWED)

    def test_the_flag_is_what_refuses_and_not_the_model(self) -> None:
        """The control. Strip `--match-head-commit` from the same command and
        the same model merges the moved head, so the refusal above belongs to
        the flag rather than to a fixture written to refuse."""

        argv = without_the_flag(merge_argv(self.merge_command(), REVIEWED))
        github = FakeGitHub(head=MOVED)
        code, _ = github.run(argv)
        self.assertEqual(code, 0)
        self.assertEqual(github.merged, MOVED)


class TheAuthorStopsAtPullRequestReady(unittest.TestCase):
    """The executable author phase cannot borrow the separate merge authority."""

    def test_prepare_stops_before_merge(self) -> None:
        lane = executable_section()
        prepare = lane.split("- `sd-ship prepare", 1)[1].split("\n- ", 1)[0]
        self.assertIn("ready_to_send", prepare)
        self.assertIn("never merges", prepare)
        for command in commands(prepare):
            self.assertNotIn("pr merge", command, command)

    def test_merge_needs_explicit_manual_authority(self) -> None:
        """sd:3085. `--manual` is the one merge authority; no runner lease is named."""
        lane = executable_section()
        self.assertIn("--manual` is the one merge authority", lane)
        self.assertNotIn("--run RUN-ID", lane)
        self.assertIn("runner_merge: auto", lane)
        self.assertIn("An author assignment cannot use this authority", lane)
        self.assertIn("enforcing protection", lane)


class TheTwoRegistryReadersAgree(unittest.TestCase):
    """Criterion 32's other clause: one reviewer order, read two ways.

    `sd_db` is installed in this tree, so the branch that applies is the
    comparison. Where it is not installed the first test says so and fails
    rather than skipping -- a skip would leave the pair pinned on one reader.
    """

    def test_the_library_is_installed_so_there_are_two_readers(self) -> None:
        self.assertIsNotNone(
            sd_registry.library(),
            "sd_db is not in this virtualenv; run `make setup`",
        )

    def test_both_readers_give_the_same_reviewer_order(self) -> None:
        shipped = sd_registry.shipped_path(REPO_ROOT)
        by_file = sd_registry.read(shipped, prefer_library=False)
        by_library = sd_registry.read(shipped, prefer_library=True)
        order = [provider.name for provider in by_file.order("reviewer")]
        self.assertNotEqual(order, [], "the registry named no reviewer")
        self.assertEqual(
            order, [provider.name for provider in by_library.order("reviewer")]
        )


class TheRowTheMergeWrites(unittest.TestCase):
    """Item B's clauses 7.21 and 15.25, as `sd-ship` states them.

    The row is written after the remote has confirmed the merge, through the
    library and through nothing else, once. Everything here reads step 8,
    which is where the closure already lived.
    """

    def setUp(self) -> None:
        self.step = steps()[8] + "\n" + referenced_procedure(steps()[8], "delivery.md")

    def test_the_step_still_owns_the_closure(self) -> None:
        """The control. A step 8 that stopped naming the trailers would make
        every check below vacuous rather than failing."""

        self.assertIn("Delivers:", self.step)
        self.assertIn("Item:", self.step)

    def test_the_write_goes_through_the_verified_library_operation(
        self,
    ) -> None:
        for token in ("sd work deliver", "sd_db.progress.deliver_work", "current remote"):
            self.assertIn(token, self.step, f"step 8 never names {token}")

    def test_the_row_waits_for_the_remote_to_confirm_the_merge(self) -> None:
        """A row written before the remote answered is a row that can outlive
        a merge that never happened, which is the failure reconciliation
        exists to clean up after."""

        self.assertTrue(
            sentences_with(self.step, "row", "confirmed", "not before"),
            "step 8 does not condition the row on a confirmed merge",
        )

    def test_the_status_write_is_one_transition_and_one_note(self) -> None:
        carried = sentences_with(self.step, "status_change", "same transaction")
        self.assertTrue(carried, "the single status_change note is not stated")
        self.assertTrue(
            any("transition" in line and "single" in line for line in carried),
            "the note is not tied to `transition` as the only status writer",
        )

    def test_shipped_at_does_not_move_on_a_second_merge(self) -> None:
        """Clause 15.25. Anchored on the field name and on `done`, which a
        rewrite has to keep, rather than on the sentence around them."""

        carried = sentences_with(self.step, "second", "`done`", "no further note")
        self.assertTrue(
            carried, "step 8 does not say what a second delivering merge does"
        )
        self.assertTrue(
            any("where it was" in line for line in carried),
            "the second merge is not said to leave the field alone",
        )

    def test_shipped_at_is_defined_as_when_the_item_shipped(self) -> None:
        self.assertTrue(
            sentences_with(self.step, "shipped_at", "not the moment"),
            "`shipped_at` is not distinguished from the last merge's time",
        )

    def test_a_slice_merge_writes_no_status_and_leaves_the_item_open(
        self,
    ) -> None:
        carried = sentences_with(self.step, "slice merge", "no status")
        self.assertTrue(carried, "a non-delivering merge's row write is unstated")
        for line in carried:
            self.assertIn("stays open", line)

    def test_a_checkout_with_no_database_still_merges(self) -> None:
        """The other half of clause 7.21: the row is a record of the merge,
        never a precondition for it."""

        self.assertTrue(
            sentences_with(self.step, "no database", "no-op"),
            "step 8 does not say what happens without a database",
        )
        self.assertTrue(
            sentences_with(self.step, "refuses no merge"),
            "a missing row is not stated to be harmless",
        )

    def test_the_skill_never_reaches_past_the_library(self) -> None:
        found = RAW_WRITE.findall(SKILL_TEXT)
        self.assertEqual(found, [], f"the skill writes the row itself: {found}")

    def test_the_raw_write_pattern_recognises_the_forms(self) -> None:
        """A dead pattern would clear the check above on any file."""

        for form in (
            "sqlite3.connect(path)",
            "UPDATE item SET status = 'done'",
            "INSERT INTO note (item, kind) VALUES (?, ?)",
            "connection.execute(sql)",
        ):
            self.assertTrue(RAW_WRITE.search(form), form)
        for kept in ("sd_db.transition", "set_item_fields", "the item's row"):
            self.assertIsNone(RAW_WRITE.search(kept), kept)


class TheCancelPath(unittest.TestCase):
    """Cancellation completes in the database without requiring another merge."""

    def setUp(self) -> None:
        self.step = steps()[8] + "\n" + referenced_procedure(steps()[8], "delivery.md")

    def test_the_cancel_writes_done_with_a_cancelled_note(self) -> None:
        self.assertTrue(
            sentences_with(self.step, "cancel", "`done`", "`cancelled`"),
            "the cancel's row write is not stated",
        )

    def test_the_cancel_touches_no_file_and_opens_no_pull_request(self) -> None:
        carried = sentences_with(self.step, "cancel", "no pull request")
        self.assertTrue(carried, "the cancel is not said to open no pull request")
        self.assertTrue(sentences_with(self.step, "cancel", "touches no file"))
        self.assertIn("does not wait for another merge", self.step)

    def test_an_associated_later_merge_can_record_closes(self) -> None:
        self.assertTrue(
            sentences_with(self.step, "cancel", "`Closes: <item>`"),
            "no merge is said to carry `Closes:` for a cancelled item",
        )

    def test_no_sentence_gives_a_cancel_a_delivers_trailer(self) -> None:
        """The mechanical half. Every sentence that mentions both a cancel
        and `Delivers:` must negate the pairing; one that does not is a file
        that hands a cancelled item a delivery."""

        for line in sentences_with(self.step, "cancel", "Delivers:"):
            self.assertTrue(
                re.search(r"\bno\b|\bnever\b|\bnot\b", line),
                f"a cancel is given `Delivers:` unqualified: {line}",
            )

    def test_the_two_trailers_are_distinguished(self) -> None:
        """`Closes:` on its own would otherwise read as a quieter spelling of
        `Delivers:`, and a cancelled item would look shipped."""

        carried = sentences_with(self.step, "`Delivers:`", "`Closes:`", "never")
        self.assertTrue(
            carried, "the file does not say why a cancel gets only one trailer"
        )


class AKilledRunIsReconciledByTheNext(unittest.TestCase):
    """Criterion 13's kill clauses.

    The merge and the row's write are two acts, and a run can die between
    them. The next run repairs the row *from the pull request*, and the check
    that carries the most weight is an absence: no merge command anywhere in
    the section, read with the same parser that finds the real merge in step
    6.
    """

    def setUp(self) -> None:
        self.reconcile = reconcile_section()

    def test_the_section_exists_and_has_a_body(self) -> None:
        """The control. `section()` returning nothing would pass every
        absence check below over any file at all."""

        self.assertGreater(len(self.reconcile), 400, "the section is a stub")

    def test_the_command_parser_would_see_a_merge_if_one_were_there(
        self,
    ) -> None:
        """The other control, and the one that matters: the absence below is
        worth nothing unless this parser finds a merge where there is one."""

        self.assertTrue(
            any(c.startswith("gh pr merge") for c in commands(steps()[7])),
            "the parser finds no merge in step 7, so finding none elsewhere "
            "says nothing",
        )

    def test_reconciliation_issues_no_merge(self) -> None:
        for command in commands(self.reconcile):
            self.assertNotIn("pr merge", command, command)

    def test_it_says_so_in_words_as_well(self) -> None:
        """The absence above survives a rewrite; it does not survive a reader
        who does not know the rule, which is who this file is for."""

        self.assertTrue(
            sentences_with(self.reconcile, "no merge is called"),
            "the no-merge rule is nowhere in the prose",
        )

    def test_the_kill_window_leaves_the_row_in_progress(self) -> None:
        self.assertTrue(
            sentences_with(self.reconcile, "killed", "`in_progress`"),
            "the section never says what a killed run leaves behind",
        )

    def test_the_repair_is_the_next_run_and_not_the_dying_one(self) -> None:
        carried = sentences_with(self.reconcile, "next `sd-ship` run")
        self.assertTrue(carried, "nothing names the run that repairs the row")
        for line in carried:
            self.assertIn("runs no handler", line)

    def test_the_repair_reads_the_pull_request_the_row_names(self) -> None:
        self.assertTrue(
            sentences_with(self.reconcile, "pull request the row names"),
            "the source of the reconciled answer is unstated",
        )

    def test_a_delivering_kill_reconciles_to_done_with_shipped_at(self) -> None:
        self.assertTrue(
            sentences_with(self.reconcile, "`Delivers:`", "sd work deliver", "verify"),
            "the delivering case does not use the verified completion operation",
        )

    def test_a_slice_kill_notes_the_squash_and_leaves_the_row_open(self) -> None:
        carried = sentences_with(self.reconcile, "`Item:`", "squash commit")
        self.assertTrue(carried, "the slice case is unstated")
        for line in carried:
            self.assertIn("stays open", line)

    def test_an_open_pull_request_is_not_a_kill(self) -> None:
        self.assertTrue(
            sentences_with(self.reconcile, "not merged yet"),
            "an unmerged pull request is not distinguished from a kill",
        )

    def test_a_run_after_the_reconciling_one_changes_nothing(self) -> None:
        carried = sentences_with(self.reconcile, "writes nothing")
        self.assertTrue(carried, "the second rerun's no-op is unstated")
        for line in carried:
            for token in ("status_change", "shipped_at", "no merge call"):
                self.assertIn(token, line, f"the no-op omits {token}")

    def test_a_hand_merge_leaves_the_row_open_and_the_item_picked(self) -> None:
        """The reconciliation's boundary: a trailerless merge is not a
        delivery, however merged the branch is."""

        carried = sentences_with(self.reconcile, "no trailer", "`in_progress`")
        self.assertTrue(carried, "the hand merge's row is unstated")
        for line in carried:
            self.assertIn("picking the item", line)

    def test_the_two_ways_the_merge_never_happens_are_named_together(self) -> None:
        """Criterion 13's first two kill clauses, which are one window.

        A run killed after the push and a merge the remote refused differ in
        why they stopped and in nothing that follows, so the section states
        them once. Naming only one would leave the other reading as a case the
        page forgot, which is how a reader talks themselves into reconciling
        it.
        """

        self.assertTrue(
            sentences_with(self.reconcile, "killed after the push", "refused"),
            "the section names neither the kill before the merge nor the "
            "refused merge, or names them in separate sentences",
        )

    def test_neither_of_them_closes_anything(self) -> None:
        """The three things criterion 13 asserts, in one sentence.

        Scattered they say much less: "untouched" belongs to half the page.
        Together they are the claim that a branch reaching the remote is not
        a delivery, which is the only reason this paragraph exists.
        """

        carried = sentences_with(self.reconcile, "not `done`")
        self.assertTrue(carried, "no sentence says the row stays open")
        for line in carried:
            for token in ("untouched", "picking the item"):
                self.assertIn(token, line, f"the consequence omits {token}")

    def test_the_guess_is_named_as_the_thing_not_to_do(self) -> None:
        self.assertTrue(
            sentences_with(self.reconcile, "closed", "by a slice"),
            "the failure mode the boundary prevents is not named",
        )


class AStandingRejection(unittest.TestCase):
    """sd:1929: a rejection that survives one review pass is adjudicated before the next fix is pushed."""

    RULE = "A rejection that still stands after one review pass is recorded with `sd-ship adjudicate` before the next fix is pushed."

    def test_the_skill_states_the_rule_once_and_the_reference_points_back(self) -> None:
        self.assertEqual(SKILL_TEXT.count(self.RULE), 1)
        reference = (REPO_ROOT / "skills/sd-ship/references/adjudication.md").read_text(encoding="utf-8")
        self.assertNotIn(self.RULE, reference, "one statement of the rule; the reference cross-references it")
        self.assertIn("before the next fix is pushed (`skills/sd-ship/SKILL.md`", reference)


class TheMergeNotRebasePin(unittest.TestCase):
    """sd:2034: a reviewed branch takes the newer base by a merge.

    The pin lives here, not in `tests/test_sd_ship.py`: the fast path picks
    every module that names a changed file, so a pin there ran all of that
    module's shards for a policy edit (sd:5836).
    """

    def test_the_policy_says_merge_never_rebase(self) -> None:
        self.assertIn("take a newer default branch with `git merge origin/<base>`, never a rebase",
                      " ".join(WORKFLOW.read_text(encoding="utf-8").split()))

    def test_a_policy_edit_selects_no_sd_ship_shard(self) -> None:
        path = REPO_ROOT / ".github/scripts/select-tests.py"
        spec = importlib.util.spec_from_file_location("select_tests", path)
        assert spec is not None and spec.loader is not None
        selector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(selector)
        selection, reason = selector.select(REPO_ROOT, [WORKFLOW.name])
        self.assertIsNotNone(selection, reason)
        self.assertIn("tests.test_sd_ship_skill", selection)
        self.assertNotIn("tests.test_sd_ship", selection)


if __name__ == "__main__":
    unittest.main()
