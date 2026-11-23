"""The frontmatter contract for `skills/sd-*/SKILL.md`, asserted as it is written.

The design's taxonomy (design.md, "Taxonomy (r6 §1)") is a *behavioural* claim:
a COMMAND pre-authorizes side effects, so its invocation must be deliberate and
it sets `disable-model-invocation`; a SKILL is knowledge loaded when relevant and
carries no standing authority, so it must not; an AGENT is a bounded worker and
declares `tools:`. Rendered, that difference is three frontmatter keys, and
nothing in this repository checked them before this file existed.

The contract is grounded rather than invented. Measured across the 60 skills
installed on this machine: `name` and `description` appear in all 60,
`disable-model-invocation` in 16, `model`/`effort` in 54. The marker is a real
mechanism in use, not a design aspiration. The exception has a measured
precedent too: `se-help`, this surface's predecessor and still installed at
`~/.claude/skills/se-help`, carries no marker. It is named here as evidence for
the rule, not as a surface this suite asserts -- everything below is `sd-*`,
where the six commands are the named surfaces.

These run without an installer. That is the point: step 3e builds the renderer
that consumes this tree, so without these assertions the six surfaces would
sit unverified until then, and a missing marker would surface as a command that
silently invokes itself.

Since 5-iii the tree also holds the sixty-four folded skills, so the inventory
claim had to change shape. It used to be "the tree is exactly these surfaces",
which is a list -- and a list of every surface is precisely what stops being
maintainable at seventy-six. What survives is the half that is actually load-
bearing: the *commands* are exactly these six, and every other surface in the
tree is a skill,
which is a property each file carries rather than a roster this file recites. A
sixty-fifth skill needs no edit here; a twelfth command still fails, which is
the invariant: the verb inventory changes by decision record, not by commit.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILLS = REPO_ROOT / "skills"
#: Requirement 10 splits the tree in two without splitting the conventions.
#: `skills/` is what a path names and the installer renders; `contrib/` is
#: everything `sd skill try` can still reach. A skill in either is a skill
#: in this pack, so every rule in this file reads both roots -- a skill that
#: moves to `contrib/` must not lose its frontmatter on the way.
CONTRIB = REPO_ROOT / "contrib"
ROOTS = (SKILLS, CONTRIB)

# The six commands, the surfaces the design names. Pinned deliberately: the verb
# inventory is a CI-tested invariant, so a thirteenth surface fails here until
# the design record grows to justify it. The dependency-triage command left
# the roster when sd:10 requirement 13 cut its `contrib/` skill. The suggest
# skill left it in sd:3040; the `sd suggest` verb stays.
COMMANDS = frozenset({
    "sd-check", "sd-review", "sd-ship", "sd-status", "sd-skill-adopt",
    "sd-handoff",
})
EXPECTED = COMMANDS

MARKER = "disable-model-invocation"
# Matches `sd_install.SHARED_DIR`; spelled here so the test needs no import.
SHARED_DIR = "_shared"


def frontmatter(text: str) -> dict[str, str] | None:
    """The leading `---` block as flat key/value pairs, or None if absent.

    Deliberately the same 20-line flat-scalar subset the rest of the pack parses
    (D-C1): no YAML dependency, no nested structures, and a file that needs more
    than this is a file that has outgrown the contract.
    """

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields: dict[str, str] = {}
    for line in lines[1:21]:
        if line.strip() == "---":
            return fields
        key, separator, value = line.partition(":")
        if separator and not key.startswith((" ", "\t")):
            fields[key.strip()] = value.strip()
    return None  # unterminated block within the bound


def directories() -> list[pathlib.Path]:
    """Every skill directory in the pack, installed or in `contrib/`.

    Directories only. `skills/` holds `paths.json` beside them, and every
    caller here re-checked `is_dir()` for a while, which made this function's
    name a description of its callers rather than of itself -- and would have
    handed the next root file to whichever caller forgot.
    """

    return sorted(
        (
            entry
            for root in ROOTS
            if root.is_dir()
            for entry in root.iterdir()
            if entry.is_dir()
        ),
        key=lambda entry: entry.name,
    )


def surfaces() -> list[pathlib.Path]:
    """Every `SKILL.md` on disk, under either root."""

    return [p / "SKILL.md" for p in directories() if (p / "SKILL.md").is_file()]


def skill_file(name: str) -> pathlib.Path | None:
    """Where `name` keeps its `SKILL.md`, or None if no root holds it."""

    for root in ROOTS:
        candidate = root / name / "SKILL.md"
        if candidate.is_file():
            return candidate
    return None


class InventoryTests(unittest.TestCase):
    def test_a_retired_skill_is_gone_from_the_tree_and_the_paths(self) -> None:
        """sd:6357. `sd-help` and `sd-diagram` went unused for 30 days.

        Neither folder nor a `paths.json` row nor a README row may bring one back.
        """
        paths = (SKILLS / "paths.json").read_text(encoding="utf-8")
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for name in ("sd-help", "sd-diagram"):
            with self.subTest(skill=name):
                self.assertIsNone(skill_file(name), "a retired skill is back in the tree")
                self.assertNotIn(f'"{name}"', paths)
                self.assertNotIn(f"`{name}`", readme)

    def test_the_named_surfaces_are_all_present(self) -> None:
        found = {p.parent.name for p in surfaces()}
        self.assertEqual(set(EXPECTED) - found, set(), "a named surface left the tree")

    def test_directories_returns_directories(self) -> None:
        """The contract, pinned, because `skills/` holds a file beside them.

        `paths.json` lives at the root of the tree. Every caller in this file
        happens to re-check `is_dir()`; that is not the same as this function
        being right, and the next caller is the one that would not.
        """
        found = directories()
        self.assertTrue(found, "no skill directories in either root")
        self.assertEqual([d for d in found if not d.is_dir()], [])
        self.assertNotIn("paths.json", [d.name for d in found])

    def test_every_directory_holds_a_skill_file(self) -> None:
        if not SKILLS.is_dir():
            self.skipTest("skills/ does not exist yet")
        for entry in directories():
            # `_shared` holds reference files the installer fans out by
            # citation; it is deliberately not a surface and has no SKILL.md.
            if entry.is_dir() and entry.name != SHARED_DIR:
                with self.subTest(surface=entry.name):
                    self.assertTrue((entry / "SKILL.md").is_file(), "no SKILL.md")

    def test_the_folded_skills_are_here(self) -> None:
        """Guards every "for each non-command surface" loop from running empty.

        A floor, deliberately, and not the exact count: 5-iii folded sixty-four,
        and pinning that number would put back the roster this file just stopped
        keeping -- retiring one skill would fail here rather than where the
        retirement is decided. sd:9062 retired the never-used ones and sd:10156
        those unused for 60 days; the floor is six -- close enough to the real number that losing a meaningful
        part of what is left fails, loose enough that ordinary movement in
        either direction does not.
        """

        self.assertGreaterEqual(len(surfaces()) - len(EXPECTED), 6)

    def test_no_surface_kept_a_retired_prefix(self) -> None:
        for path in surfaces():
            with self.subTest(surface=path.parent.name):
                self.assertFalse(
                    path.parent.name.startswith("se-"),
                    "a folded surface kept its pre-fold name",
                )

    def test_the_title_is_the_surface_name(self) -> None:
        """The first heading names the surface, and nothing else.

        The directory-name check above is not enough, and the fold proved it:
        all sixty-four folded skills arrived titled `# SE Typed Holes` while
        their directory, frontmatter, and every cross-reference already read
        the kebab-case name. The rename map matched a lowercase prefix, so a
        title-cased one went straight through -- a whole class the map could
        not see, and one nothing in this suite would have reported.

        Checking the title against the directory is what makes the class
        checkable at all: it needs no list of forbidden spellings, so the next
        stale title fails here whatever it says.
        """

        for path in surfaces():
            heading = next(
                (
                    line
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.startswith("# ")
                ),
                None,
            )
            with self.subTest(surface=path.parent.name):
                self.assertEqual(heading, f"# {path.parent.name}")


class FrontmatterTests(unittest.TestCase):
    def test_every_surface_parses(self) -> None:
        for path in surfaces():
            with self.subTest(surface=path.parent.name):
                self.assertIsNotNone(frontmatter(path.read_text(encoding="utf-8")))

    def test_name_matches_the_directory(self) -> None:
        for path in surfaces():
            fields = frontmatter(path.read_text(encoding="utf-8")) or {}
            with self.subTest(surface=path.parent.name):
                self.assertEqual(fields.get("name"), path.parent.name)

    def test_description_is_present_and_one_line(self) -> None:
        for path in surfaces():
            fields = frontmatter(path.read_text(encoding="utf-8")) or {}
            description = fields.get("description", "")
            with self.subTest(surface=path.parent.name):
                self.assertTrue(description.strip(), "empty description")
                self.assertNotIn("\n", description)


class KindMarkerTests(unittest.TestCase):
    """The taxonomy, which is the whole reason this file exists."""

    def test_commands_disable_model_invocation(self) -> None:
        for path in surfaces():
            if path.parent.name not in COMMANDS:
                continue
            fields = frontmatter(path.read_text(encoding="utf-8")) or {}
            with self.subTest(command=path.parent.name):
                self.assertEqual(
                    fields.get(MARKER),
                    "true",
                    f"a command whose invocation is not deliberate: {MARKER} must be true",
                )

    def test_skills_do_not(self) -> None:
        """Every surface that is not one of the six commands.

        Scoped by exclusion rather than by a roster, which is what lets the
        sixty-four folded skills be covered without being named. A folded skill
        that arrived carrying the marker would be a surface the model refuses to
        load on relevance -- silently, since nothing else reads the key.
        """

        for path in surfaces():
            if path.parent.name in COMMANDS:
                continue
            fields = frontmatter(path.read_text(encoding="utf-8")) or {}
            with self.subTest(skill=path.parent.name):
                self.assertNotIn(MARKER, fields, "a skill carrying a command's marker")

    def test_no_surface_here_declares_tools(self) -> None:
        """`tools:` marks an agent, and agents live in `agents/`, not here."""

        for path in surfaces():
            fields = frontmatter(path.read_text(encoding="utf-8")) or {}
            with self.subTest(surface=path.parent.name):
                self.assertNotIn("tools", fields, "an agent in the skills tree")


class DocumentedFlagTests(unittest.TestCase):
    """A skill must not document a flag its tool does not have.

    The frontmatter checks above cannot see this: a skill can be perfectly
    shaped and still describe an option that was removed. That is exactly what
    happened -- `sd-check`'s skill documented `--repo` hours after #605 deleted
    it, because the author branched before the fix landed, and every structural
    test still passed.

    Scoped to the surfaces with a real implementation in `bin/` -- which ones
    those are is `implemented()` below, read off the filesystem, not a count
    kept here. For the rest the design's table is the only source, and
    asserting against it would pin prose to prose.
    """

    def implemented(self) -> list[tuple[str, pathlib.Path, pathlib.Path]]:
        found = []
        for path in surfaces():
            # The six only. A folded skill is a procedure, not a CLI, and a
            # folded name that happened to match a `bin/` file would otherwise
            # be dragged into a flag contract it was never written against.
            if path.parent.name not in EXPECTED:
                continue
            tool = REPO_ROOT / "bin" / path.parent.name
            if tool.is_file():
                found.append((path.parent.name, path, tool))
        return found

    def tool_source(self, tool: pathlib.Path) -> str:
        """The tool's text, plus that of every `bin/` module it imports.

        A surface is not always one file. `sd-review` dispatches `setup-github`
        into `bin/sd_setup_github.py`, and that module owns four of the flags
        the skill documents; reading only the entry point would have reported
        them as undocumented-in-the-tool. The imports are enumerated from the
        source rather than listed here, so a second split needs no edit.
        """

        text = tool.read_text(encoding="utf-8", errors="replace")
        parts = [text]
        for node in ast.walk(ast.parse(text)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                module = REPO_ROOT / "bin" / f"{name}.py"
                if module.is_file():
                    parts.append(module.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts)

    def test_there_are_implemented_surfaces_to_check(self) -> None:
        # Guards the loop below from passing by finding nothing.
        self.assertGreaterEqual(len(self.implemented()), 4)

    # Phrases that mark a flag as documented-but-absent on purpose. A skill is
    # allowed -- encouraged -- to say "the design has the flag, the code does not",
    # and the sentence carrying that disclaimer is often the section heading
    # rather than the line naming the flag. So the window is the flag's line plus
    # the heading it sits under, which is where a skill states this truthfully.
    DISCLAIMERS = (
        "not implemented",
        "does not exist",
        "not exist",
        "no `",
        "will not be",
        "when it exists",
        "yet",
    )

    def disclaimed(self, body: str, option: str) -> bool:
        heading = ""
        for line in body.splitlines():
            if line.startswith("#"):
                heading = line
            if option in line:
                window = f"{heading}\n{line}".lower()
                if any(phrase in window for phrase in self.DISCLAIMERS):
                    return True
        return False

    def test_every_documented_flag_exists_in_the_tool(self) -> None:
        import re

        flag = re.compile(r"`(--[a-z][a-z-]+)")
        for name, skill, tool in self.implemented():
            source = self.tool_source(tool)
            body = skill.read_text(encoding="utf-8")
            for option in sorted(set(flag.findall(body))):
                # A skill may say a flag does NOT exist; that sentence names it.
                present = f'"{option}"' in source or f"'{option}'" in source
                disclaimed = self.disclaimed(body, option)
                with self.subTest(surface=name, flag=option):
                    if present and disclaimed:
                        # The skill says the flag is gone and the tool still has
                        # it. Copilot caught exactly this on #607: the branch
                        # predated the PR that deleted `--repo`, so the skill's
                        # "there is no --repo" was false in its own tree. The
                        # disclaimer path used to `continue` here, which made a
                        # confident denial the one claim this test never read.
                        self.fail(
                            f"{name} says {option} does not exist, but "
                            f"bin/{name} still accepts it"
                        )
                    if not present and not disclaimed:
                        self.fail(
                            f"{name} documents {option}, which bin/{name} "
                            f"does not accept"
                        )


class RunsAsColumn(unittest.TestCase):
    """README's **Runs as** column is derived from `bin/`, not maintained.

    The column exists so a reader can tell a shipped entrypoint from a prose
    sequence without opening eleven files. It was added by hand, against a
    filesystem that changes, in the same commit whose own docstring calls a
    count kept by hand next to a list kept by hand the ordinary way a fact
    rots. Leaving it ungated would have been that commit doing the thing it
    objects to.

    `bin/` is the authority. A row saying `bin/` for a command with no
    entrypoint promises something that cannot be run; a row saying prose for a
    command that has one hides a tool from the person looking for it. Both
    directions fail here.
    """

    ROW = re.compile(r"^\|\s*`(sd-[a-z0-9-]+)`\s*\|\s*(`bin/`|prose)\s*\|", re.M)

    def rows(self) -> list[tuple[str, str]]:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        return [(m.group(1), m.group(2)) for m in self.ROW.finditer(readme)]

    WORDS = {"Two": 2, "Three": 3, "Four": 4, "Five": 5, "Six": 6, "Seven": 7, "Eight": 8, "Nine": 9,
             "Ten": 10, "Eleven": 11, "Twelve": 12, "Thirteen": 13}
    SENTENCE = re.compile(r"^There are ([a-z]+) named surfaces", re.M)

    def stated_total(self) -> int:
        """The number of commands README states in prose."""
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        m = self.SENTENCE.search(readme)
        self.assertIsNotNone(m, "README no longer states how many named surfaces there are")
        word = m.group(1).capitalize()
        self.assertIn(word, self.WORDS, f"README spells a number this test cannot read: {word!r}")
        return self.WORDS[word]

    def test_the_table_is_found_at_all(self) -> None:
        """Guards the derivation, not the data.

        Every other test here passes vacuously on zero rows, so a table that
        grows a column and silently stops matching would read as a clean run.
        """
        self.assertEqual(
            len(self.rows()), self.stated_total(),
            "README's command table did not parse into the number of rows its "
            "own sentence claims -- if a column moved, fix this pattern rather "
            "than letting the checks below pass on nothing",
        )

    # README: "each of the six commands sets `disable-model-invocation`, so
    # invoking it is a deliberate act; every other surface
    # does not." So the table is `COMMANDS`, which is `EXPECTED`, and the
    # table is checked against it.
    #
    # An earlier cut of this derived the set from the marker instead, so that an
    # ninth command would join the table by existing rather than by somebody
    # remembering a line. That is the wrong trade here and the module docstring
    # says so: the roster is pinned *because* the verb inventory is a
    # CI-tested invariant, and a ninth command is supposed to
    # fail until the design record grows to justify it. Deriving silently
    # granted that growth to any skill that set the key.
    #
    # So the marker is used as a cross-check on the roster rather than as a
    # substitute for it, below. Drift between the two still fails; it fails
    # naming the design record, which is where the decision belongs.
    @staticmethod
    def marks_itself(skill: str) -> bool:
        """Whether `skill` sets the marker -- in its frontmatter, not its prose.

        The distinction is not hypothetical. `sd-skill-adopt`
        writes `disable-model-invocation` into its body, because a pack
        whose subject is its own conventions documents them. A substring search
        over the whole file cannot tell a skill that *sets* the key from one
        that *explains* it, so it would enrol any skill that grew a sentence
        about the marker into README's command table and then fail it for
        missing a prose-runner declaration it was never supposed to make.
        `frontmatter()` is the parser the rest of this file already trusts.
        """
        path = skill_file(skill)
        if path is None:
            return False
        fields = frontmatter(path.read_text(encoding="utf-8"))
        return bool(fields) and fields.get(MARKER) == "true"

    def expected(self) -> set[str]:
        return set(EXPECTED)

    def test_the_marker_agrees_with_the_pinned_roster(self) -> None:
        """The filesystem and the design record name the same six commands.

        `COMMANDS` is a roster, and a roster drifts. This is the check that it
        has not: every skill setting the marker is a pinned command, and every
        pinned command sets it. A skill that gates itself without joining the
        design record fails here rather than quietly becoming a command, and a
        command that loses its marker fails here rather than quietly becoming
        model-invocable.
        """
        marked = {
            d.name for d in directories()
            if (d / "SKILL.md").is_file() and self.marks_itself(d.name)
        }
        self.assertEqual(
            marked, set(COMMANDS),
            f"sets {MARKER} but is not a pinned command: "
            f"{sorted(marked - set(COMMANDS))}; pinned command not setting it: "
            f"{sorted(set(COMMANDS) - marked)} -- reconcile the skill with the "
            f"design record, not this list with the tree",
        )

    def test_the_table_lists_exactly_the_commands(self) -> None:
        """Identity of the set, not the shape of it."""
        names = {name for name, _ in self.rows()}
        self.assertEqual(
            names, self.expected(),
            f"missing from README's table: {sorted(self.expected() - names)}; "
            f"unexpected: {sorted(names - self.expected())}",
        )

    def test_each_command_appears_once_and_is_a_real_skill(self) -> None:
        """Row count alone is satisfied by a duplicate covering an omission.

        Counting rows and checking each against `bin/` both pass when one
        command is listed twice and another is missing entirely: the count is
        right and every row present is accurate. Identity is what that misses,
        so it is checked separately, against the skills directory rather than
        against a list kept here.
        """
        names = [name for name, _ in self.rows()]
        self.assertEqual(
            sorted(names), sorted(set(names)),
            f"a command is listed twice in README's table: "
            f"{sorted(n for n in set(names) if names.count(n) > 1)}",
        )
        for name in names:
            with self.subTest(command=name):
                self.assertTrue(
                    skill_file(name) is not None,
                    f"README's table names `{name}`, which is not a skill",
                )

    def test_every_row_matches_the_filesystem(self) -> None:
        for name, label in self.rows():
            shipped = (REPO_ROOT / "bin" / name).is_file()
            with self.subTest(command=name):
                self.assertEqual(
                    label == "`bin/`", shipped,
                    f"README calls `{name}` {label}, but bin/{name} "
                    f"{'exists' if shipped else 'does not exist'}",
                )


class BinaryClaims(unittest.TestCase):
    """A skill that names a `bin/` command must name an existing file."""

    MENTION = re.compile(r"`(bin/sd-[a-z0-9-]+)`")

    def test_every_named_binary_exists(self) -> None:
        missing = []
        for path in surfaces():
            text = path.read_text(encoding="utf-8")
            for named in set(self.MENTION.findall(text)):
                if (REPO_ROOT / named).is_file():
                    continue
                missing.append(f"{path.parent.name} names missing {named}")
        self.assertEqual(sorted(missing), [], "\n".join(sorted(missing)))

    def test_the_patterns_still_match_something(self) -> None:
        """The control prevents a vacuous pass when the mention pattern drifts."""
        joined = "".join(path.read_text(encoding="utf-8") for path in surfaces())
        self.assertNotEqual(self.MENTION.findall(joined), [], "no bin/ mention parsed")


class ValueLadderTests(unittest.TestCase):
    """Two value ladders, `depth` and `sensitivity`, and this is their enforcer.

    Set membership, not order: a skill lists its subset default-first. The
    ladders were once stated in a shared reference that said they were
    "checked" while nothing read it, so a skill could spell `depth=short` and
    stay green; the reference is gone and the ladders live here.
    """

    LADDERS = {"depth": {"brief", "standard", "deep"},
               "sensitivity": {"minimal", "restricted", "standard"}}
    BULLET = re.compile(r"^- `(depth|sensitivity)=([^`]*)`", re.MULTILINE)

    def test_every_declared_subset_is_drawn_from_the_ladder(self) -> None:
        seen = 0
        for path in surfaces():
            for name, values in self.BULLET.findall(path.read_text(encoding="utf-8")):
                seen += 1
                declared = set(values.split("|"))
                self.assertLessEqual(declared, self.LADDERS[name],
                                     f"{path.parent.name}: {name}={values} leaves the ladder")
        self.assertGreater(seen, 0, "no ladder bullets parsed")


if __name__ == "__main__":
    unittest.main()
