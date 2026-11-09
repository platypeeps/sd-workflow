"""Every skill that writes a file in this tree names `sd-rules --for`.

sd:431 step 8, part (ii). The authoring tier is a verb, `sd-rules --for
<path>`, and a verb nobody calls enforces nothing: `design.md`'s tier table
says the skill "consults the registry and names the rule ids in scope", and
the owner's note of 2026-09-16 says where -- "skills call it in their setup
step". This module holds that link. Part (i), the verb, landed at #997 and is
tested in `tests/test_sd_rules_for.py`; part (iii), one teaching section per
live-row id, lands as the rows do and is held by leg a in
`tests/test_rule_registry.py`.

**Which skills.** `WRITER_SKILLS` is not typed from memory: it is the answer
`WRITES_A_FILE` gives over every tracked `skills/*/SKILL.md`, less the names
`NOT_WRITERS` holds with a reason each. The predicate is a write verb within
one clause of a file noun, which is how the audit of `8548d512` read the
skills -- sd-plan "Write from the templates", sd-spec "Rewrites the spec
pages", sd-debug "Edits in the tree", sd-handoff "writing any file the
packet's `files[]` lists", sd-humanizer "rewrite the file in place",
sd-technical-editor "Edit mode authorizes only the supplied draft". It is
looser than the audit's reading, on purpose: a predicate tight enough to match
only the six would be the list again, spelled as a regex. So it also matches
a skill that declares itself read-only in the negative ("It does not
create or edit slide files") and one that writes another repository's tree, and those are held in
`NOT_WRITERS` where a reviewer can read why. A seventh skill that writes a
file lands in neither set and `test_the_list_is_what_the_predicate_answers`
names it; a skill that leaves `NOT_WRITERS` for the tree without moving to
`WRITER_SKILLS` is named the same way.

**sd-review is not here.** Its `setup-github` writes `.github/`, which
`sd-rules` classes as code and answers with the `bin/`-shaped rows; the
audit left it out until that residue is settled, and the predicate does not
match it, so nothing here has to say so twice.

**Where the sentence is read from.** The whole page, fenced blocks removed:
every skill that carries the pointer carries it inline, as sd-check's two
teaching sections and sd-handoff's restore step do, and a pointer inside a
fence would be an example rather than an instruction.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The literal a skill's setup step carries. `<path>` or a concrete path
#: follows it; the test reads the verb and the flag, not the argument.
POINTER = "sd-rules --for"

#: The skills whose setup step names the verb, as the predicate below answers
#: on `8548d512` less `NOT_WRITERS`. Sorted, so a diff against the predicate's
#: answer reads in one order.
WRITER_SKILLS = (
    "sd-handoff",
    "sd-humanizer",
)

#: The skills `WRITES_A_FILE` also matches, with the sentence it matched and
#: why that sentence does not make the skill a writer of this tree. An entry
#: here is held to the predicate: the day its page stops matching, the entry
#: is stale and the derivation test says so.
NOT_WRITERS = {
    "sd-research-repo": "matches `Write or edit through the file tools`; the "
                        "files it writes are another repository's, its own "
                        "`CLAUDE.md` among them, not this tree's",
}

#: A write verb, then within one clause a noun for the thing written. One
#: clause: no full stop and no line break between them, so a verb in one
#: sentence and a noun in the next are not read as one act.
WRITES_A_FILE = re.compile(
    r"\b(write|writes|writing|rewrite|rewrites|rewriting|edit|edits|editing"
    r"|fix|fixes)\b[^.\n]{0,60}"
    r"\b(file|files|page|pages|draft|in place|in the tree|work root"
    r"|docs/\w+|templates)\b",
    re.IGNORECASE,
)

FENCE = re.compile(r"^\s*(?:```|~~~)")


def skill_pages(root: pathlib.Path = REPO_ROOT) -> dict[str, pathlib.Path]:
    """Every tracked `skills/<name>/SKILL.md`, keyed by skill name.

    The index rather than a directory walk, as every registry checker reads
    the tree, so a scratch copy of a skill cannot join the census.
    """

    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--deduplicate", "--",
         "skills/*/SKILL.md"],
        capture_output=True, text=True, check=True).stdout.split("\0")
    return {pathlib.Path(name).parent.name: root / name for name in listed if name}


def outside_fences(text: str) -> str:
    """The page with every fenced block cut out, line by line."""

    kept, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            kept.append(line)
    return "\n".join(kept)


#: Where a sentence ends: a terminator before whitespace and a capital, or at
#: the end of the text. A dot inside `<page>.md` is followed by a letter.
SENTENCE_END = re.compile(r"[.!?](?=\s+[A-Z]|\s*$)")


def names_the_verb_before_writing(page: str) -> bool:
    """Whether a sentence outside a fence runs the verb *before* a write.

    The contract is a setup-step action (sd:999, review-1018): the owner's
    note says "skills call it in their setup step". Page-wide presence let the
    pointer move into a report paragraph while no step ran it. Each live
    skill writes the order into the sentence that holds the pointer --
    "Before the first edit to a file, run", "before rewriting", "the setup
    step, before it resumes writing" -- so that sentence must say `before`.
    """

    flat = outside_fences(page).replace("\n", " ")
    sentences = SENTENCE_END.split(flat)
    return any(POINTER in sentence and re.search(r"\bbefore\b", sentence, re.IGNORECASE)
               for sentence in sentences)


def writer_shaped(root: pathlib.Path = REPO_ROOT) -> dict[str, str]:
    """Every skill `WRITES_A_FILE` matches, with the first sentence it matched."""

    found = {}
    for name, path in sorted(skill_pages(root).items()):
        text = path.read_text(encoding="utf-8", errors="replace")
        match = WRITES_A_FILE.search(text)
        if match:
            found[name] = match.group(0)
    return found


class TheSetupStep(unittest.TestCase):
    """Each writer skill's page carries the pointer, inline."""

    def test_every_writer_skill_names_the_verb_outside_a_fence(self) -> None:
        pages = skill_pages()
        missing = []
        for name in WRITER_SKILLS:
            text = pages[name].read_text(encoding="utf-8", errors="replace")
            if not names_the_verb_before_writing(text):
                missing.append(name)
        self.assertEqual(missing, [], f"""
These writer skills do not name `{POINTER}` in a sentence that runs it
before the write (a fenced example does not count): {missing}.

Before the skill writes a file, its setup step runs the verb for that file and
cites the ids it prints -- the shape `skills/sd-handoff/SKILL.md` uses in its
restore step. If a skill here has stopped writing files, take it out of
`WRITER_SKILLS`; the derivation test will then say whether the predicate agrees.
""")


class TheSetupStepIsAnchored(unittest.TestCase):
    """sd:999 (review-1018). The pointer has to run before the write, not just appear."""

    def test_a_pointer_outside_a_before_sentence_does_not_count(self) -> None:
        for page in (f"Report what `{POINTER} <path>` printed.\n",
                     f"## Final report\n\nList the `{POINTER}` ids you cited.\n",
                     f"Edit the file. Then run `{POINTER} <path>`.\n"):
            with self.subTest(page=page):
                self.assertFalse(names_the_verb_before_writing(page))

    def test_each_live_shape_counts(self) -> None:
        for page in (f"Before the first edit to a file, run\n`{POINTER} <path>` first.\n",
                     f"run `{POINTER} <path>` from the root before rewriting.\n",
                     f"The setup step, before it resumes writing a file, is\n`{POINTER} <path>`.\n"):
            with self.subTest(page=page):
                self.assertTrue(names_the_verb_before_writing(page))


class TheWriterList(unittest.TestCase):
    """`WRITER_SKILLS` is derived, and the derivation is checked both ways."""

    def test_the_list_is_what_the_predicate_answers(self) -> None:
        matched = writer_shaped()
        expected = set(WRITER_SKILLS) | set(NOT_WRITERS)
        self.assertEqual(set(matched), expected, f"""
`WRITES_A_FILE` and the two lists disagree over the tracked skills.

Matched by the predicate but in neither list: {sorted(set(matched) - expected)}
-- with the sentence each matched:
{ {name: matched[name] for name in sorted(set(matched) - expected)} }.
Listed but not matched: {sorted(expected - set(matched))}.

A skill the predicate matches goes into `WRITER_SKILLS`, and gains the setup
sentence, or into `NOT_WRITERS` with the reason its match is not a write into
this tree. A listed skill the predicate has stopped matching leaves the list it
is in.
""")

    def test_no_skill_is_in_both_lists(self) -> None:
        both = sorted(set(WRITER_SKILLS) & set(NOT_WRITERS))
        self.assertEqual(both, [])

    def test_the_list_is_sorted_and_unique(self) -> None:
        self.assertEqual(list(WRITER_SKILLS), sorted(set(WRITER_SKILLS)))

    def test_every_not_writer_carries_a_reason(self) -> None:
        empty = sorted(name for name, why in NOT_WRITERS.items() if not why.strip())
        self.assertEqual(empty, [])


class ThePredicate(unittest.TestCase):
    """The regex reads a clause, not a page."""

    def test_a_write_verb_and_a_file_noun_in_one_clause_match(self) -> None:
        self.assertTrue(WRITES_A_FILE.search("then rewrite the file in place"))
        self.assertTrue(WRITES_A_FILE.search("Rewrites the spec pages"))
        self.assertTrue(WRITES_A_FILE.search("Edits in the tree"))

    def test_a_full_stop_or_a_line_break_ends_the_clause(self) -> None:
        self.assertIsNone(WRITES_A_FILE.search("Write it down. The file is read."))
        self.assertIsNone(WRITES_A_FILE.search("Write it down\nthe file is read."))

    def test_a_verb_with_no_noun_in_reach_is_not_a_write(self) -> None:
        self.assertIsNone(WRITES_A_FILE.search("edit the wording of the prompt"))

    def test_a_fenced_pointer_is_not_read(self) -> None:
        page = f"# skill\n\n```\n{POINTER} <path>\n```\n\nprose\n"
        self.assertNotIn(POINTER, outside_fences(page))
        self.assertIn(POINTER, outside_fences(f"run `{POINTER} <path>` first\n"))


if __name__ == "__main__":
    unittest.main()
