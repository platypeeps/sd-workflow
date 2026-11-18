"""Every `os.killpg` in the pack survives EPERM (sd:7900).

macOS answers EPERM, not only ESRCH, for a group whose members are exiting
(sd:7704, sd:2402), and the gate's sandbox can deny the call outright. An
uncaught PermissionError leaves the child running and fails the caller.

The fallback kills the run's own process only, so a grandchild can live on
and hold its pipes: the read that follows a killpg needs a bound (sd:7910).
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: The trees the gate runs; their own tests may call killpg bare on purpose.
TREES = ("bin", "dashboard", "lib")
CAUGHT = {"PermissionError", "OSError"}
#: Functions that read a killed run's pipe with a bound of their own, by `path:function`.
BOUNDED_READERS = {"dashboard/sd_dashboard/hoa_download.py:_lines"}


def sources():
    """Every Python file the gate runs in `TREES`: `*.py` and the extensionless scripts with a python shebang."""
    for tree in TREES:
        for path in sorted((ROOT / tree).rglob("*")):
            parts = path.relative_to(ROOT).parts
            if not path.is_file() or "tests" in parts or ".venv" in parts or "node_modules" in parts:
                continue
            if path.suffix == ".py":
                yield path
            elif not path.suffix:
                with open(path, "rb") as handle:
                    first = handle.readline(200)
                if first.startswith(b"#!") and b"python" in first:
                    yield path


def _parsed(source: str):
    module = ast.parse(source)
    return module, {child: node for node in ast.walk(module) for child in ast.iter_child_nodes(node)}


def _killpgs(module: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(module) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == "killpg"]


def _enclosing(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[str]:
    """Names of the functions around `node`, innermost first."""
    names = []
    while (node := _function(parents.get(node), parents)) is not None:
        names.append(node.name)
    return names


def _function(node: ast.AST, parents: dict[ast.AST, ast.AST]):
    while node is not None and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        node = parents.get(node)
    return node


def _names(node: ast.AST | None) -> set[str]:
    return {n.id if isinstance(n, ast.Name) else n.attr for n in ast.walk(node)
            if isinstance(n, (ast.Name, ast.Attribute))} if node is not None else set()


def _guarded(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    child, node = call, parents.get(call)
    while node is not None:
        if isinstance(node, ast.Try) and child in node.body and any(
                CAUGHT & _names(handler.type) for handler in node.handlers):
            return True
        if isinstance(node, ast.With) and any(
                isinstance(item.context_expr, ast.Call) and "suppress" in _names(item.context_expr.func)
                and CAUGHT & set().union(*map(_names, item.context_expr.args)) for item in node.items):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return False
        child, node = node, parents.get(node)
    return False


def unguarded() -> list[str]:
    """`path:line` of each `os.killpg` call outside a handler that catches PermissionError."""
    found = []
    for path in sources():
        module, parents = _parsed(path.read_text(encoding="utf-8"))
        found += [f"{path.relative_to(ROOT)}:{node.lineno}" for node in _killpgs(module) if not _guarded(node, parents)]
    return found


def unbounded_reads(name: str, source: str) -> list[str]:
    """`name:line` of each read in `source` that can wait on a grandchild after a killpg.

    Two shapes: a `communicate()` with no `timeout` after a killpg in the same function, and a loop over a
    `.stdout` in a module that calls killpg (it reads to EOF), outside `BOUNDED_READERS`.
    """
    module, parents = _parsed(source)
    killers = _killpgs(module)
    if not killers:
        return []
    found = set()
    for killer in killers:
        function = _function(killer, parents)
        for node in ast.walk(function if function is not None else module):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "communicate"
                    and node.lineno > killer.lineno and not any(k.arg == "timeout" for k in node.keywords)):
                found.add(f"{name}:{node.lineno}")
    for node in ast.walk(module):
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Attribute) and node.iter.attr == "stdout":
            if not any(f"{name}:{function}" in BOUNDED_READERS for function in _enclosing(node, parents)):
                found.add(f"{name}:{node.lineno}")
    return sorted(found)


class KillpgSurvivesEperm(unittest.TestCase):
    def test_every_killpg_catches_permission_error(self):
        self.assertEqual(unguarded(), [])

    def test_every_read_after_a_killpg_is_bounded(self):
        self.assertEqual([line for path in sources() for line in
                          unbounded_reads(str(path.relative_to(ROOT)), path.read_text(encoding="utf-8"))], [])

    def test_the_read_guard_refuses_an_unbounded_reap_and_a_loop_to_eof(self):
        kill = "def f(p):\n    try:\n        os.killpg(p.pid, 9)\n    except PermissionError:\n        p.kill()\n"
        cases = (
            (kill + "    p.communicate()\n", ["x.py:6"]),
            (kill + "    p.communicate(timeout=5)\n", []),
            (kill + "    for line in p.stdout:\n        pass\n", ["x.py:6"]),
            ("def g(p):\n    for line in p.stdout:\n        pass\n", []),
        )
        for source, want in cases:
            with self.subTest(source=source):
                self.assertEqual(unbounded_reads("x.py", source), want)
        reader = "def _lines(p):\n    def pump():\n        for line in p.stdout:\n            pass\n" + kill
        self.assertEqual(unbounded_reads("dashboard/sd_dashboard/hoa_download.py", reader), [])

    def test_the_scan_reads_the_extensionless_python_scripts_in_bin(self):
        names = {path.name for path in sources()}
        self.assertTrue({"sd-ship", "sd-check"} <= names, sorted(name for name in names if "." not in name))

    def test_the_guard_refuses_a_bare_call_and_one_that_catches_only_esrch(self):
        bare = ast.parse("def f(p):\n    os.killpg(p.pid, 9)\n")
        esrch = ast.parse("def f(p):\n    try:\n        os.killpg(p.pid, 9)\n    except ProcessLookupError:\n"
                          "        pass\n")
        caught = ast.parse("def f(p):\n    with contextlib.suppress(ProcessLookupError, PermissionError):\n"
                           "        os.killpg(p.pid, 9)\n")
        for module, want in ((bare, False), (esrch, False), (caught, True)):
            parents = {child: node for node in ast.walk(module) for child in ast.iter_child_nodes(node)}
            call = next(node for node in ast.walk(module) if isinstance(node, ast.Call))
            with self.subTest(source=ast.unparse(module)):
                self.assertIs(_guarded(call, parents), want)


class SignalGroupFallsBackToTheLeader(unittest.TestCase):
    def test_a_refused_group_signal_reaches_the_leader(self):
        """sd:9332 lane round: `signal_group` swallowed EPERM, so the lane's TERM and KILL went nowhere."""
        import os
        import signal
        import sys
        from unittest import mock
        sys.path.insert(0, str(ROOT / "bin"))
        import sd_lib
        with mock.patch.object(os, "killpg", side_effect=PermissionError), \
                mock.patch.object(os, "kill") as kill:
            sd_lib.signal_group(4242, signal.SIGKILL)
        kill.assert_called_once_with(4242, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
