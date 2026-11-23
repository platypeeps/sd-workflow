"""The tracked allowlist is derived from this repo's inventories, not typed.

Review found the same defect three times in one pull request, each time a rule
naming something nobody invokes: `python3 scripts/:*` for a directory that does
not exist; `python3 bin/sd-status:*` alone, when the script is executable and
carries a `python3` shebang, so the form the documents actually use is
`bin/sd-status`; and `bin/sd-dashboard:*`, whose `install` verb then wrote a
plist into `~/Library/LaunchAgents` for a command no document told anyone to
run by hand (sd:719 has since deleted the verb). Every one came from writing a
rule out of an assumption about the workflow rather than reading what the
workflow runs, and narrowing the list by
hand found them one at a time while reintroducing the shape twice.

Hand-narrowing was the wrong instrument. The repository already states what it
runs, in three places that are maintained for other reasons:

* the Makefile's public `.PHONY` targets,
* README's `bin/sd_install.py` table, one row per mode, with the mode's effect
  written in the row,
* the `mcp__github__*` tools a shipped skill's `SKILL.md` names.

So the list is derived from those at run time and compared against the file. A
rule with no source fails; a source with no rule fails. Adding a `make` target
and forgetting the allowlist is a test failure rather than a prompt somebody
works around six months later.

**What this can and cannot catch.** The comparison is only as good as the
filters below, and it cannot audit its own judgment: writing the file from this
derivation makes the two agree by construction. Its job is to keep them
agreeing as the inventories move. The two checks that are *not* circular are
the ones after it: every path a rule names must exist, and a target that is
executable with a shebang must carry both invocation forms. Each has to be
written so it can fail on the defect it cites -- the first draft of the second
one could not, walking `bin/...` rules to ask after their `python3` twin when
what shipped was the `python3` rule alone.

Some of the shipped surfaces name a `bin/` command that does not exist yet, and
the skill is expected to declare that rather than read as a command you can
run. This docstring used to carry the list and the count. It said six and
listed six, omitting one; it was then corrected to seven and named
`sd-ship`, which has shipped a binary since -- the ordinary fate of a count
kept by hand next to a list kept by hand, twice over, in the paragraph arguing
against the practice. The list is gone and no number replaces it:
`tests/test_skill_frontmatter.py` derives both facts at run time, in
`UnbuiltSurfaceTests.unbuilt` and `BinaryClaims`. That is also why every
derivation here filters on the filesystem rather than trusting a name it read
in a document.
"""

from __future__ import annotations

import json
import pathlib
import re
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SETTINGS = REPO_ROOT / ".claude" / "settings.json"

# A README row reads `| `bin/sd_install.py --flag` | What it does |`. The mode
# is allowlisted unless the row itself says not to, so both filters below read
# the document rather than a list of flags somebody kept in their head.
INSTALL_ROW = re.compile(r"^\|\s*`(?:python3 )?bin/sd_install\.py ([^`]+)`\s*\|\s*([^|]+?)\s*\|$", re.M)

# A placeholder in the mode means it takes a path, and a path argument is the
# thing that turns a repo-scoped grant into an arbitrary-path write:
# `--repo PATH` writes `PATH/CLAUDE.local.md`, `--home DIR` moves the install
# root. Path-scoping the script is not the test; what the script does with a
# path argument is.
TAKES_A_PATH = re.compile(r"\b[A-Z]{3,}\b")

# The row's own first word. A tracked allowlist that a second contributor
# inherits should not carry a grant whose documented effect is deletion, and
# the README already says which those are.
DESTRUCTIVE = ("Remove", "Delete", "Uninstall", "Drop")

# README's surface table marks exactly one command "Read-only:". A read-only
# surface is the only kind whose whole flag space is safe behind one wildcard,
# so that adjective -- maintained for readers, not for this test -- is what
# decides which `bin/` commands appear at all.
#
# The columns between the name and the purpose are skipped rather than counted.
# Adding a "Runs as" column to that table silently emptied this match and took
# both `sd-status` rules out of the derived set with it -- a table maintained
# for readers is going to grow columns, and a derivation that breaks when it
# does is a derivation that will be quietly wrong instead of loudly.
#
# `[^|\n]`, not `[^|]`. The first draft of the widening used `[^|]`, which
# matches newlines, so it ran from the first row down through four more
# rows to the first `Read-only:` in the table and reported that the first command was
# the read-only surface. A pattern that skips columns must not also skip rows:
# that failure grants a wildcard to the wrong command, which is worse than
# granting none.
READ_ONLY_SURFACE = re.compile(
    r"^\|\s*`(sd-[a-z-]+)`\s*\|(?:[^|\n]*\|)*?\s*Read-only:", re.M)

MCP_TOOL = re.compile(r"mcp__github__[a-z_]+")

BASH_RULE = re.compile(r"^Bash\((.*)\)$")


def settings_allow() -> list[str]:
    """What the tracked file actually grants."""

    return json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"]["allow"]


def public_make_targets() -> list[str]:
    """The Makefile's own public target list.

    The first `.PHONY` line names what a person runs; the second names the two
    path-printing helpers a recipe calls (`lint-ruff-paths`, `lint-mypy-paths`),
    which are not commands anybody types.
    """

    text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    first = re.search(r"^\.PHONY:(.*)$", text, re.M)
    assert first, "Makefile has no .PHONY line"
    return first.group(1).split()


def readme_install_modes() -> list[tuple[str, str]]:
    """Every `sd_install.py` mode README documents, as (mode, what it does)."""

    return INSTALL_ROW.findall((REPO_ROOT / "README.md").read_text(encoding="utf-8"))


def grantable_install_modes() -> list[str]:
    """The documented modes that take no path and destroy nothing."""

    return [mode for mode, effect in readme_install_modes()
            if not TAKES_A_PATH.search(mode) and not effect.startswith(DESTRUCTIVE)]


def read_only_surfaces() -> list[str]:
    """The `bin/` commands README describes as read-only, that exist."""

    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    return [name for name in READ_ONLY_SURFACE.findall(text)
            if (REPO_ROOT / "bin" / name).is_file()]


def skill_mcp_tools(root: pathlib.Path = REPO_ROOT) -> set[str]:
    """The GitHub tools a shipped skill tells a reader to call.

    `SKILL.md` only, never `skills/**/*.md`. A skill directory also holds
    templates and shared reference material, and a tool named in a *reference*
    is being described rather than invoked -- scanning those would grant a
    capability because some prose mentioned it. The entrypoint is the shipped
    instruction; everything beside it is documentation.

    Unlike the `bin/` derivation, this does not require a skill's own binary
    to exist: a skill can invoke an MCP tool directly. No tool mentions is a
    valid result when no shipped skill needs a GitHub grant.
    """

    found: set[str] = set()
    for skill in root.glob("skills/**/SKILL.md"):
        found.update(MCP_TOOL.findall(skill.read_text(encoding="utf-8")))
    return found


def runs_under_python3(name: str) -> bool:
    """A `bin/` file that is executable and says how to run itself."""

    target = REPO_ROOT / "bin" / name
    if not target.is_file():
        return False
    first = target.read_text(encoding="utf-8").splitlines()[:1]
    return bool(first) and first[0].startswith("#!") and "python3" in first[0]


def derived_allow() -> set[str]:
    """The whole tracked allowlist, from the three inventories above."""

    rules = {f"Bash(make {target})" for target in public_make_targets()}
    rules |= {f"Bash(python3 bin/sd_install.py {mode})" for mode in grantable_install_modes()}
    for name in read_only_surfaces():
        rules.add(f"Bash(bin/{name}:*)")
        if runs_under_python3(name):
            # Executable with a shebang, so both spellings are real and a rule
            # matching one does not match the other.
            rules.add(f"Bash(python3 bin/{name}:*)")
    return rules | skill_mcp_tools()


class PermissionAllowlistTests(unittest.TestCase):
    def test_the_tracked_allowlist_is_exactly_what_the_inventories_yield(self) -> None:
        actual = set(settings_allow())
        derived = derived_allow()
        self.assertEqual(
            actual, derived,
            f"\n  granted with no source: {sorted(actual - derived)}"
            f"\n  documented with no rule: {sorted(derived - actual)}")

    def test_every_path_a_rule_names_exists(self) -> None:
        """The defect that arrived three times, checked without a derivation.

        `python3 scripts/:*` named a directory this repository does not have.
        A rule pointing at nothing is not harmless -- it reads as coverage, so
        the prompt it was supposed to stop keeps arriving and the next person
        widens something real to fix it.
        """

        missing = []
        for rule in settings_allow():
            match = BASH_RULE.match(rule)
            if not match:
                continue
            for token in match.group(1).removesuffix(":*").split():
                if "/" not in token:
                    continue
                if not (REPO_ROOT / token).exists():
                    missing.append(f"{rule} names {token}, which does not exist")
        self.assertEqual(missing, [], "\n".join(missing))

    def test_an_executable_target_carries_both_invocation_forms(self) -> None:
        """`bin/sd-status --json` and `python3 bin/sd-status --json` both happen.

        A rule matching one does not match the other, so granting one spelling
        leaves the other prompting. This has to be checked in **both**
        directions, and an earlier draft only checked one: it walked the
        `bin/...` rules asking whether the `python3` form existed, which is
        precisely the direction the shipped defect was not in. What shipped was
        `python3 bin/sd-status:*` *alone* -- the `python3` form present, the
        bare form missing, and no rule to walk from. A check that cannot fail
        on the defect it cites is decoration.
        """

        allow = settings_allow()
        granted = set(allow)
        gaps = set()
        for rule in allow:
            match = BASH_RULE.match(rule)
            if not match:
                continue
            bare = match.group(1).removeprefix("python3 ")
            if not bare.startswith("bin/"):
                continue
            name = bare.removesuffix(":*").split("/")[-1].split()[0]
            if not runs_under_python3(name):
                continue
            for form in (f"Bash({bare})", f"Bash(python3 {bare})"):
                if form not in granted:
                    gaps.add(f"{rule} is granted, but {form} is not")
        self.assertEqual(sorted(gaps), [], "\n".join(sorted(gaps)))

    def test_the_inventories_were_actually_read(self) -> None:
        """The control. A regex that silently stops matching would empty the
        derived set and make the comparison above pass over anything."""

        self.assertNotEqual(public_make_targets(), [], "Makefile .PHONY not parsed")
        self.assertNotEqual(readme_install_modes(), [], "README install table not parsed")
        self.assertNotEqual(read_only_surfaces(), [], "README surface table not parsed")
        self.assertNotEqual(list(REPO_ROOT.glob("skills/**/SKILL.md")), [],
                            "no skill entrypoints were enumerated")

    def test_skill_tool_inventory_reads_entrypoints_and_allows_no_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            skill = root / "skills" / "example"
            skill.mkdir(parents=True)
            entrypoint = skill / "SKILL.md"
            entrypoint.write_text("Local work only.\n", encoding="utf-8")
            (skill / "reference.md").write_text(
                "mcp__github__issue_write\n", encoding="utf-8")
            self.assertEqual(skill_mcp_tools(root), set())

            entrypoint.write_text(
                "Call `mcp__github__list_issues`; then `mcp__github__list_issues` again.\n",
                encoding="utf-8")
            nested = root / "skills" / "group" / "second"
            nested.mkdir(parents=True)
            (nested / "SKILL.md").write_text(
                "Call `mcp__github__pull_request_read`.\n", encoding="utf-8")
            self.assertEqual(skill_mcp_tools(root), {
                "mcp__github__list_issues", "mcp__github__pull_request_read",
            })

    def test_the_read_only_adjective_is_on_the_row_it_is_credited_to(self) -> None:
        """The control above only asks that *something* matched.

        It passes just as happily when the pattern skips rows instead of
        columns, which is what the first draft of the column-tolerant version
        did: `[^|]` matches newlines, so it ran from the first row down to the
        first `Read-only:` five rows below and credited the adjective to a
        command that does not carry it -- handing a whole-flag-space wildcard
        to the wrong entrypoint. Non-circular because it re-reads README to
        ask where the word actually is, rather than comparing the derivation
        to itself.
        """
        lines = (REPO_ROOT / "README.md").read_text(encoding="utf-8").splitlines()
        for name in read_only_surfaces():
            row = [line for line in lines if line.startswith(f"| `{name}` |")]
            self.assertEqual(len(row), 1, f"{name} does not name exactly one row")
            self.assertIn("Read-only:", row[0],
                          f"{name} was credited with an adjective from another row")

    def test_a_destructive_or_path_taking_mode_is_never_granted(self) -> None:
        """Both filters, against the README rows they read.

        Without this, widening `TAKES_A_PATH` or emptying `DESTRUCTIVE` would
        quietly add `--repo [PATH]` and `--uninstall` to the derived set, and
        the comparison would then demand they be granted.
        """

        documented = dict(readme_install_modes())
        self.assertIn("--repo [PATH]", documented, "README no longer documents --repo")
        self.assertIn("--uninstall", documented, "README no longer documents --uninstall")
        granted = grantable_install_modes()
        self.assertNotIn("--repo [PATH]", granted)
        self.assertNotIn("--uninstall", granted)
        self.assertIn("--status", granted)


if __name__ == "__main__":
    unittest.main()
