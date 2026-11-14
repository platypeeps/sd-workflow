"""No module under `bin/` reaches `sd_db` except through `sd_lib.import_sd_db`.

sd:745 and sd:746. `sd_db` is provisioned into the pack's virtualenv and every
entrypoint starts `#!/usr/bin/env python3`, so no interpreter a person actually
invokes has the library on `sys.path`. `sd_lib.import_sd_db` makes the second
try, against the provisioned copy; an import that runs before it has been
called fails on every machine and then says "install the library" about a
library that is installed. `bin/sd-ship` shipped exactly that, and fourteen
other files were found carrying the same one-try shape by a grep. A list of
instances closes nothing; this closes the class.

The rule, per `sd_db` import (`import sd_db...`, `from sd_db... import`, or
`importlib.import_module` / `__import__` on a literal or f-string naming
`sd_db`), judged in the function it sits in:

  1. A call that reaches the helper comes first, on an earlier line of the
     same function. "Reaches" is computed, not listed: `import_sd_db` itself,
     or any function in `bin/` whose own body calls something that reaches it
     (`sd_handoff_rows.library`, `sd_work._library`, ...). A wrapper that
     stops calling the helper stops counting, with nothing to update here.
  2. Or the function is handed the library: it has a parameter named
     `sd_db`, which only a caller holding the imported module can pass.
  3. Or it is on `REACHED_WITH_THE_LIBRARY` below, which names the caller that
     already ran the helper. An entry whose function no longer imports
     `sd_db` fails the test, so the list cannot outlive its reasons.

The helper's own body is the one place a first try is allowed.

Parsed with `ast`, not grepped: the imports that matter are inside functions,
behind `try`, and spelled four ways, and a textual search for the helper's
name is satisfied by a comment. "Earlier line" is textual order, not control
flow -- a helper call in a branch that did not run still counts -- which is
the price of a check that does not execute anything.

Known limits, found by adversarial review on PR #907 and left open because
each needs data flow or execution this check does not do. The rule passes,
wrongly, on:

  - an import inside a `lambda`, which is not a scope the walk visits;
  - a helper call in a conditional branch, or inside a conditional wrapper
    (one that calls the helper on only some paths);
  - a helper call on the same line as the import, or after it on that line;
  - resolution by name only: `self.import_sd_db()`, `anything.import_sd_db()`
    or a local `def import_sd_db` all count as the helper, whatever the owner;
  - a module name that is not a literal (`import_module(name)`), including an
    f-string whose leading part is a variable;
  - a function whose `sd_db` parameter has a default, so a caller can omit it;
  - a generator whose helper call precedes the import but never runs because
    the generator is not consumed.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from collections.abc import Iterator

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / "bin"
HELPER = ("sd_lib", "import_sd_db")

#: Functions that import `sd_db` with no helper call of their own, because
#: nothing reaches them before one has run. Keyed `file::qualified name`.
#: Each reason names the caller that ran it. Kept short on purpose: the
#: ordinary fix for a new entry is a helper call, not a line here.
REACHED_WITH_THE_LIBRARY = {
    # `rows` is `_rows()`, that is `sd_handoff_rows`, so `rows.library()` ran first (sd:8721).
    "sd_shadow.py::shadow_sync": "shadow_sync() calls rows.library() (sd_handoff_rows) before the import",
    # Only called from `run`, after `sd_handoff_rows.library()`.
    "sd_operations.py::service_result": "run() calls sd_handoff_rows.library() first",
    # Only called from `cmd_user`, once `open_library` returned a connection.
    "sd_install.py::expire_trials": "cmd_user() calls open_library() first",
    # `Ship` is built only by `main`, after `sd_lib.import_sd_db()` answered.
    "sd-ship::Ship.__init__": "main() calls sd_lib.import_sd_db() before building Ship",
    "sd-ship::Ship.close_task": "a Ship exists only after main() called the helper",
    "sd-ship::Ship.delivered_at": "a Ship exists only after main() called the helper",
    "sd-ship::Ship.merge_branch": "a Ship exists only after main() called the helper",
    "sd-ship::Ship.note_demotion": "a Ship exists only after main() called the helper",
    "sd-ship::Ship.reconcile": "a Ship exists only after main() called the helper",
    "sd-ship::Ship.record_delivery": "a Ship exists only after main() called the helper",
    "sd-ship::Ship.row_merges": "a Ship exists only after main() called the helper",
    # Handed that same `Ship` by `Ship.adjudicate`.
    "sd_ship_dispositions.py::adjudicate": "called with a Ship, built after the helper",
}

SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def python_sources() -> list[pathlib.Path]:
    """Every Python file directly under `bin/`, from the filesystem.

    The directory and not a hand list, and not git's index either: a new
    entrypoint is governed from the moment it exists.
    """

    found = []
    for path in sorted(BIN.iterdir()):
        if not path.is_file():
            continue
        if path.suffix == ".py":
            found.append(path)
            continue
        with path.open("rb") as handle:
            first = handle.readline(4096)
        if first.startswith(b"#!") and b"python" in first:
            found.append(path)
    return found


def own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """The nodes that run as part of `scope`, not of a scope nested in it."""

    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, SCOPES):
            pending.extend(ast.iter_child_nodes(node))


def scopes(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """`(qualified name, node)` for the module and every def and class in it."""

    stack: list[tuple[str, ast.AST]] = [("", tree)]
    while stack:
        prefix, node = stack.pop()
        yield prefix or "<module>", node
        for child in own_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stack.append((f"{prefix}.{child.name}" if prefix else child.name, child))


def names_sd_db(module: str) -> bool:
    return module == "sd_db" or module.startswith("sd_db.")


def sd_db_imports(scope: ast.AST) -> list[int]:
    """Lines in `scope` itself that import `sd_db` or one of its submodules."""

    lines = []
    for node in own_nodes(scope):
        if isinstance(node, ast.Import):
            if any(names_sd_db(alias.name) for alias in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and names_sd_db(node.module):
                lines.append(node.lineno)
        elif isinstance(node, ast.Call) and callee(node)[1] in {"import_module", "__import__"}:
            if node.args and names_sd_db(literal_prefix(node.args[0])):
                lines.append(node.lineno)
    return sorted(lines)


def literal_prefix(node: ast.AST) -> str:
    """A string literal, or the leading literal part of an f-string."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        return literal_prefix(node.values[0])
    return ""


def callee(call: ast.Call) -> tuple[str, str]:
    """`(owner, name)`: `("", "f")` for `f()`, `("mod", "f")` for `mod.f()`.

    Any other receiver -- `sibling("sd_lib").f()`, `self.f()` -- keeps the
    name with an unknown owner, `"?"`.
    """

    function = call.func
    if isinstance(function, ast.Name):
        return "", function.id
    if isinstance(function, ast.Attribute):
        owner = function.value.id if isinstance(function.value, ast.Name) else "?"
        return owner, function.attr
    return "?", ""


def calls(scope: ast.AST) -> list[tuple[int, str, str]]:
    return [(node.lineno, *callee(node)) for node in own_nodes(scope) if isinstance(node, ast.Call)]


class Corpus:
    """Every `bin/` Python file parsed once, and what reaches the helper."""

    def __init__(self, sources: list[pathlib.Path]) -> None:
        self.trees = {path.name: ast.parse(path.read_text(encoding="utf-8"), str(path)) for path in sources}
        self.scopes = {
            (name, qualified): node
            for name, tree in self.trees.items()
            for qualified, node in scopes(tree)
        }
        self.gateways = self._gateways()

    @staticmethod
    def module(name: str) -> str:
        return name.removesuffix(".py")

    def resolves_to(self, name: str, owner: str, called: str) -> tuple[str, str]:
        """The `(module, top-level function)` a call names, as far as a parse can say."""

        if called == HELPER[1]:
            return HELPER
        return (self.module(name), called) if owner == "" else (owner, called)

    def _gateways(self) -> set[tuple[str, str]]:
        """Functions whose own body calls something that reaches the helper."""

        reaching = {HELPER}
        changed = True
        while changed:
            changed = False
            for (name, qualified), node in self.scopes.items():
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or "." in qualified:
                    continue
                key = (self.module(name), qualified)
                if key in reaching:
                    continue
                if any(self.resolves_to(name, owner, called) in reaching for _, owner, called in calls(node)):
                    reaching.add(key)
                    changed = True
        return reaching

    def violations(self) -> list[str]:
        found = []
        for (name, qualified), node in sorted(self.scopes.items(), key=lambda item: item[0]):
            if (self.module(name), qualified) == HELPER:
                continue  # the one place a first try lives
            lines = sd_db_imports(node)
            if not lines or f"{name}::{qualified}" in REACHED_WITH_THE_LIBRARY:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                argument.arg == "sd_db"
                for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            ):
                continue
            reached = [
                line for line, owner, called in calls(node)
                if self.resolves_to(name, owner, called) in self.gateways
            ]
            first = min(reached, default=None)
            for line in lines:
                if first is None or first > line:
                    found.append(f"bin/{name}:{line} ({qualified}) imports sd_db before sd_lib.import_sd_db()")
        return found


class SdDbImportGateway(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = Corpus(python_sources())

    def test_every_sd_db_import_comes_after_the_helper(self) -> None:
        self.assertEqual(self.corpus.violations(), [])

    def test_the_census_found_what_it_is_about(self) -> None:
        """A walk that parsed nothing, or lost the helper, would pass the rule vacuously."""

        self.assertIn(HELPER, self.corpus.gateways)
        self.assertIn(("sd_handoff_rows", "library"), self.corpus.gateways)
        importing = {name for (name, _), node in self.corpus.scopes.items() if sd_db_imports(node)}
        self.assertIn("sd_lib.py", importing)
        self.assertIn("sd-status", importing)
        self.assertGreater(len(importing), 10)

    def test_every_allow_listed_function_still_needs_its_entry(self) -> None:
        stale = sorted(
            key for key in REACHED_WITH_THE_LIBRARY
            if not sd_db_imports(self.corpus.scopes.get(tuple(key.split("::", 1)), ast.Module(body=[], type_ignores=[])))
        )
        self.assertEqual(stale, [], "these no longer import sd_db; remove them from REACHED_WITH_THE_LIBRARY")

    def test_the_rule_catches_a_bare_first_try(self) -> None:
        """The shape sd:745 shipped, in a scratch file, must be a violation."""

        tree = ast.parse(
            "def main():\n"
            "    try:\n"
            "        from sd_db import ship\n"
            "    except ImportError:\n"
            "        return 3\n"
            "    import sd_lib\n"
            "    sd_lib.import_sd_db()\n"
            "\n"
            "def dynamic(name):\n"
            "    return importlib.import_module(f'sd_db.{name}')\n"
        )
        corpus = Corpus([])
        corpus.trees = {"sd-scratch": tree}
        corpus.scopes = {("sd-scratch", qualified): node for qualified, node in scopes(tree)}
        corpus.gateways = corpus._gateways()
        self.assertEqual(corpus.violations(), [
            "bin/sd-scratch:10 (dynamic) imports sd_db before sd_lib.import_sd_db()",
            "bin/sd-scratch:3 (main) imports sd_db before sd_lib.import_sd_db()",
        ])


if __name__ == "__main__":
    unittest.main()
