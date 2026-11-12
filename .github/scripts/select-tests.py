#!/usr/bin/env python3
"""Pick the test modules a set of changed files needs, or say the suite does.

`make check CHANGED="<paths>"` hands its paths to `run-tests.sh`, which asks
this script what to run. It prints either the one line `full`, meaning run
every module, or the module names to run, one per line. Without `CHANGED` the
script is never asked and the full suite runs, which is sd:10 criterion 16:
the fast path is an argument, and the full suite stays the default.

Every doubt resolves to running more, never less:

* A path the full suite must answer for selects `full`: the `Makefile`,
  anything under `.github/`, the Python and dependency configuration, the
  installer (its coverage gate needs the whole suite), and anything under
  `tests/` or `dashboard/tests/` that is not a test module, such as the
  coverage `sitecustomize`.
* A path no test module names selects `full`. So does a path outside the
  checkout, a directory, and a path that names more than half the modules
  outside the always-run set: that file is shared, and a fast path that runs
  most of the suite without its coverage gate is a slower full run that
  proves less.
* An empty list of paths selects `full`. `CHANGED=` and `CHANGED="   "` are
  what an operator produces by accident, from a diff that came back empty or
  failed, and a narrowed run that skips the coverage gates is the wrong answer
  to "I changed nothing I can name".
* The always-run set runs on every fast path. It is the modules that walk the
  whole tree rather than naming a file: shape, line caps, code health,
  citations, shell placement, the retired framework's name, verbs, workflow
  policy, the governed pathspec, the cut symbols, and the form every call to
  git's index lister declares. The set is not written here. A module that
  walks the tree declares it, with `ALWAYS_RUN_MARKER` on a line of its own,
  and this script greps for that -- so a whole-tree module joins the set by
  existing rather than by being remembered. It was a hand-typed tuple of nine
  names until sd:1389, and `tests/test_cut_symbols.py` was not one of them:
  the same kind of grep over the same tree as `test_no_trellis_residue`,
  which was. If no module carries the marker the set has drifted and the
  answer is `full`.

A test module is selected when its source names the changed path, its file
name, or its module stem, as a whole token. A change under `bin/` or
`dashboard/` also selects the tests that name any file there whose text
carries an `import` statement for it, followed to a fixed point. The closure
reads import statements and matches the changed file's stem, so it is empty
for a stem that is not a Python identifier: `import sd-pr-state` is not
something Python can say, so no hyphenated `bin/` tool is reachable through
the closure. No import scan could reach one -- such a tool is loaded by a
`SourceFileLoader` from a name held in a string -- so a hyphenated tool is
selected by the tests that write its name and by nothing else. A test that
reaches a file without naming it -- a walk of a whole directory, a script
reached through another script -- is not selected unless it is in the
always-run set. That is the price of a fast path, and it is why the full
suite still runs before a push, and why CI never runs this.
"""

from __future__ import annotations

import pathlib
import re
import sys

FULL = "full"

#: What a module writes, on a line of its own, to say it reads the whole tree
#: and so cannot be narrowed away. Anchored to the start of a line, so a test
#: that quotes the marker inside a string does not thereby join the set.
ALWAYS_RUN_MARKER = "# select-tests: always-run"
ALWAYS_RUN_PATTERN = re.compile(
    rf"^{re.escape(ALWAYS_RUN_MARKER)}[^\S\n]*$", re.MULTILINE)

# Paths whose change the full suite answers for, as exact names or prefixes.
FULL_RUN_NAMES = frozenset({
    "Makefile",
    ".coveragerc",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements-security.txt",
    "bin/sd_install.py",
})
FULL_RUN_PREFIXES = (".github/",)

# Source trees whose Python modules import one another.
IMPORTING_TREES = ("bin", "dashboard")


def token_pattern(name: str) -> re.Pattern[str]:
    """`name` as a whole token: no word character or hyphen on either side."""

    return re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")


def relative(root: pathlib.Path, raw: str) -> str | None:
    """`raw` as a forward-slash path inside `root`, or None when it is not one."""

    path = pathlib.Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    text = rel.as_posix()
    return None if text in ("", ".") else text


def names_for(rel: str) -> set[str]:
    """What a test would write to mean this path: the path, its name, its stem."""

    path = pathlib.PurePosixPath(rel)
    names = {rel, path.name}
    if path.suffix == ".py":
        names.add(path.stem)
    return names


def always_run(modules: dict[str, str]) -> frozenset[str]:
    """The modules declaring themselves whole-tree, read off their source."""

    return frozenset(name for name, text in modules.items()
                     if ALWAYS_RUN_PATTERN.search(text))


#: Folders whose `test_*.py` files are modules this script selects from:
#: the pack's own, the library's and the dashboard's, the last two moved
#: here from system (sd:5987). The runner shards all three (sd:8553).
TEST_DIRS = ("tests", "tests/sd_db", "dashboard/tests")


def module_name(rel: str) -> str:
    """The unittest name of the module at forward-slash path `rel`."""

    return rel.removesuffix(".py").replace("/", ".")


def test_modules(root: pathlib.Path) -> dict[str, str]:
    """Every `test_*.py` module name in `TEST_DIRS`, with its source."""

    return {
        module_name(path.relative_to(root).as_posix()):
            path.read_text(encoding="utf-8", errors="replace")
        for folder in TEST_DIRS
        for path in sorted((root / folder).glob("test_*.py"))
    }


def imports_pattern(stem: str) -> re.Pattern[str]:
    """Matches the import forms this repository uses, absolute and relative.

    `import x`, `from x import y`, `from .x import y`, and the package form
    `from . import a, x, z`, which is how `dashboard/` imports its own
    modules and which an absolute-only pattern misses.
    """

    name = re.escape(stem)
    return re.compile(
        rf"^\s*(?:import\s+{name}(?![\w-])"
        rf"|from\s+\.*{name}\s+import"
        rf"|from\s+\.+\s+import\s+[^\n]*(?<![\w-]){name}(?![\w-]))",
        re.MULTILINE)


def importers(root: pathlib.Path, rel: str) -> set[str]:
    """Paths of Python modules in `IMPORTING_TREES` importing `rel`, transitively."""

    sources = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8", errors="replace")
        for tree in IMPORTING_TREES if (root / tree).is_dir()
        for path in sorted((root / tree).rglob("*")) if path.is_file()
    }
    found: set[str] = set()
    pending = [rel]
    while pending:
        stem = pathlib.PurePosixPath(pending.pop()).stem
        pattern = imports_pattern(stem)
        for path, text in sources.items():
            if path not in found and path != rel and pattern.search(text):
                found.add(path)
                pending.append(path)
    return found


def full_reason(root: pathlib.Path, rel: str | None, raw: str) -> str | None:
    """Why `rel` needs the full suite before any module is looked at, if it does."""

    if rel is None:
        return f"{raw} is not a path inside this checkout"
    if rel in FULL_RUN_NAMES or rel.startswith(FULL_RUN_PREFIXES):
        return f"{rel} is a path the full suite answers for"
    if (root / rel).is_dir():
        return f"{rel} is a directory"
    if any(re.fullmatch(rf"{re.escape(folder)}/test_[^/]*\.py", rel)
           for folder in TEST_DIRS):
        return None
    for folder in TEST_DIRS:
        if rel.startswith(f"{folder}/"):
            return f"{rel} is under {folder}/ and is not a test module"
    return None


def modules_for(root: pathlib.Path, rel: str, modules: dict[str, str]) -> set[str]:
    """The test modules naming `rel` or a module importing it; a test module is its own."""

    own = {module_name(rel)} & set(modules)
    names = names_for(rel)
    if rel.startswith(tuple(f"{tree}/" for tree in IMPORTING_TREES)):
        for importer in importers(root, rel):
            names |= names_for(importer)
    patterns = [token_pattern(name) for name in names]
    return own | {module for module, text in modules.items()
                  if any(pattern.search(text) for pattern in patterns)}


def select(root: pathlib.Path, changed: list[str]) -> tuple[list[str] | None, str]:
    """The modules to run and why, or None and why the full suite must run."""

    if not changed:
        return None, "no path was given, so there is nothing to narrow to"
    modules = test_modules(root)
    always = always_run(modules)
    if not always:
        return None, ("no test module declares itself whole-tree, so the "
                      "always-run set has drifted")
    chosen: set[str] = set()
    for raw in changed:
        rel = relative(root, raw)
        reason = full_reason(root, rel, raw)
        if reason:
            return None, reason
        assert rel is not None
        found = modules_for(root, rel, modules)
        if not found:
            return None, f"no test module names {rel}"
        chosen |= found - always
    optional = len(modules) - len(always)
    if 2 * len(chosen) > optional:
        return None, f"the change selects {len(chosen)} of {optional} modules, so it is shared"
    return sorted(chosen | always), f"{len(changed)} changed path(s)"


def main(argv: list[str]) -> int:
    args = argv[1:]
    root = pathlib.Path(__file__).resolve().parents[2]
    if args[:1] == ["--root"] and len(args) >= 2:
        root, args = pathlib.Path(args[1]), args[2:]
    if args[:1] == ["--"]:
        args = args[1:]
    selection, reason = select(root, args)
    print(f"select-tests: {reason}", file=sys.stderr)
    print("\n".join(selection) if selection is not None else FULL)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
