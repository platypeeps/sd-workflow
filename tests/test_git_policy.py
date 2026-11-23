"""One subprocess policy for `git`, enumerated from `bin/` rather than listed.

Criterion 31 of sd:10 asks for one git wrapper, asserted by a grep that finds
no second definition. Measured on 2026-09-19 the tree had nine, and the owner
decision of that date ruled on what "one" means here: not one name, but one
*subprocess policy*. A wrapper that differs only in argument order is a name;
a wrapper with its own timeout, or none, is a policy, and a second policy is
a bound nobody keeps in step.

So this does not count names. It walks `bin/` from the filesystem, finds every
place that hands `git` to `subprocess.run`, and holds each one to the rule
that applies to it. Enumerating is the point: a list written here would go
stale the first time somebody added a file, which is how the nine accumulated.

Three kinds of site exist, and the difference is not stylistic:

* **The policy.** `bin/sd_lib.py` runs `git` for everything that can import
  it, with the timeout, no shell, and failure-is-None. Anything importable
  reaches `git` through `git_output` and appears nowhere below.
* **The standalone hooks.** `bin/sd-handoff`, `bin/sd-handoff-restore` and
  `bin/sd-skill-use` are loaded by path with no `bin/` on `sys.path`, and two
  of them are pinned by their own suites to have no dependency there at all.
  They cannot borrow the policy, so they copy the one number it turns on and
  this file keeps the copies equal.
* **The deliberate variants.** Three call sites need something `git_output`
  destroys or refuses: raising instead of returning `None`, bytes instead of
  text, unstripped stdout. Each is named here with its reason. They still
  carry a timeout.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"

#: The one policy. Not a site to be justified -- it is what the others defer to.
POLICY = "bin/sd_lib.py"

#: Hooks that cannot import the policy, and why. Each copies the timeout, and
#: `test_every_copied_timeout_equals_the_policy_s` keeps the copies honest.
STANDALONE = {
    "bin/sd-handoff": "a writer run by path; its suite pins it to import nothing from bin/",
    "bin/sd-handoff-restore": "a SessionStart hook; same pin, same reason",
    "bin/sd-skill-use": "a PreToolUse/UserPromptSubmit hook, loaded the same way",
}

#: Call sites whose contract `git_output` cannot provide. The value is the
#: thing it cannot provide, so a reader can check the claim rather than take it.
#:
#: `bin/sd_ship_remote.py` is a fourth variant -- it raises `Refusal` where
#: `git_output` returns `None` -- and is absent here because it does not match
#: the shape below: it builds its argv inside a generic `run()` that already
#: carries a timeout, so nothing in it hands a `["git", ...]` literal to
#: `subprocess.run`. Naming it would fail `test_every_recorded_exception_is_
#: still_a_git_runner`, which is the check that keeps this list from
#: accumulating names nothing corresponds to.
VARIANTS = {
    "bin/sd-size-report": "returns bytes, because it reads blobs",
    "bin/sd_review_material.py": "needs NUL-delimited unstripped stdout; git_output strips",
    "bin/sd_install.py": "runs before any sibling is borrowed, and one call fetches",
    "bin/sd_research_render.py": "reads a --follow log for dates; wants the lines, not the strip",
    "bin/sd_fleet.py": "reads file blobs for a byte-exact diff; git_output strips the last newline",
    "bin/sd_gate_run.py": "raises git's stderr as the gate's reason, and `worktree add` of a large tree outlasts 15 s",
    "bin/sd_ship_review.py": "pipes a byte-exact `git diff --binary` into `git patch-id` on stdin (sd:1485)",
    "bin/sd_lane.py": "publish_git tells git's exit 1 (no such commit, not an ancestor) from a failure, which "
                      "git_output folds into one None, and a fetch or push outlasts 15 s (sd:3282)",
    "bin/sd_skill_intake.py": "reads untrusted blobs as bytes, raises git's stderr as the refusal, and a "
                              "fetch from GitHub outlasts 15 s (sd:3560)",
    "bin/sd_bundle_drop.py": "tells a lookup's absent exit (1 or 2) from a failure, reads NUL-delimited "
                             "unstripped paths, and a fetch or cherry-pick outlasts 15 s (pack sd:9709)",
}


def bin_sources() -> list[pathlib.Path]:
    """Every Python file under `bin/`, suffixed or not, from the filesystem."""
    found = []
    for path in sorted(BIN.iterdir()):
        if not path.is_file():
            continue
        if path.suffix == ".py":
            found.append(path)
            continue
        try:
            first = path.read_text(encoding="utf-8").splitlines()[:1]
        except (OSError, UnicodeDecodeError):  # pragma: no cover - not in tree
            continue
        if first and first[0].startswith("#!") and "python" in first[0]:
            found.append(path)
    return found


def git_subprocess_sites() -> dict[str, list[tuple[int, bool]]]:
    """`{repo-relative path: [(line, has_timeout), ...]}` for every git run.

    A site is a `subprocess.run` whose first argument is a list literal whose
    first element is the string `"git"`. That is the shape every one of them
    uses, and matching the shape rather than the text means a call spread over
    four lines is found the same as a call on one.
    """
    sites: dict[str, list[tuple[int, bool]]] = {}
    for path in bin_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "run" or not isinstance(node.func.value, ast.Name):
                continue
            if node.func.value.id != "subprocess" or not node.args:
                continue
            argv = node.args[0]
            if not isinstance(argv, ast.List) or not argv.elts:
                continue
            first = argv.elts[0]
            if not (isinstance(first, ast.Constant) and first.value == "git"):
                continue
            timed = any(keyword.arg == "timeout" for keyword in node.keywords)
            sites.setdefault(str(path.relative_to(REPO_ROOT)), []).append((node.lineno, timed))
    return sites


def assigned_timeout(path: pathlib.Path, name: str = "GIT_TIMEOUT_SECONDS") -> int | None:
    """The timeout constant a file assigns, read from its AST.

    Read rather than imported, both ends. Importing `bin/sd_lib.py` under a
    second name gives its frozen dataclasses a module they cannot look
    themselves up in -- the trap `tests/test_sd_handoff_rows.py` documents --
    and importing it under its own name would hand a second module object to
    every test that already imported it. Two source constants compared as
    source is also the more honest check: what this guards is a number copied
    into a file, not a value some import happened to produce.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(node.value, ast.Constant):
                        return node.value.value
    return None


class OneGitPolicy(unittest.TestCase):
    def test_the_enumeration_finds_something(self) -> None:
        """The bound. Every assertion below passes trivially on an empty walk."""
        self.assertGreaterEqual(len(bin_sources()), 20)
        self.assertIn(POLICY, git_subprocess_sites())

    def test_no_file_runs_git_itself_without_a_recorded_reason(self) -> None:
        allowed = {POLICY, *STANDALONE, *VARIANTS}
        found = set(git_subprocess_sites())
        unexplained = sorted(found - allowed)
        self.assertEqual(
            unexplained, [],
            "these run `git` through their own subprocess call and are not "
            "recorded as a standalone hook or a deliberate variant; route them "
            f"through sd_lib.git_output or say here why they cannot: {unexplained}",
        )

    def test_every_recorded_exception_is_still_a_git_runner(self) -> None:
        """A name left here after its call site went is a stale exemption."""
        found = set(git_subprocess_sites())
        stale = sorted({*STANDALONE, *VARIANTS} - found)
        self.assertEqual(stale, [], f"recorded but no longer runs git: {stale}")

    def test_every_git_call_carries_a_timeout(self) -> None:
        untimed = [
            f"{path}:{line}"
            for path, calls in sorted(git_subprocess_sites().items())
            for line, timed in calls if not timed
        ]
        self.assertEqual(
            untimed, [],
            "a git read with no timeout hangs the caller for as long as git "
            f"does; three of these were the defect criterion 31 found: {untimed}",
        )

    def test_every_copied_timeout_equals_the_policy_s(self) -> None:
        """The promise each standalone file's comment makes, checked."""
        expected = assigned_timeout(REPO_ROOT / POLICY)
        self.assertIsInstance(expected, int)
        copies = {name: assigned_timeout(REPO_ROOT / name) for name in sorted(STANDALONE)}
        self.assertEqual(copies, {name: expected for name in sorted(STANDALONE)})

    def test_the_policy_is_reached_by_name_and_not_re_wrapped(self) -> None:
        """`bin/sd-review` carried a wrapper that only flipped the arguments.

        A second name for the same call is how a second policy starts: the
        next edit that needs one more thing edits the nearer definition.
        """
        source = (BIN / "sd-review").read_text(encoding="utf-8")
        self.assertNotIn("def _git(", source)
        self.assertIn("sd_lib.git_output(", source)


if __name__ == "__main__":
    unittest.main()
