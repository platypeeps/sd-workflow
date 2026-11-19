"""Fixtures for bin/sd_lib.py: real git repositories, real temporary trees.

Nothing here mocks git. Worktree behaviour is the whole point of two of these
tests, and a mocked `git rev-parse` would have agreed with every wrong answer.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_db  # noqa: E402 - `make setup` provisions it; `RowActivity` needs one
import sd_lib  # noqa: E402

#: What a parent make exports to its children (sd:1641). Under `make check
#: VENV=.venv` the command-line VENV reaches every child make through
#: MAKEFLAGS and overrides the fixture Makefile's own choice of environment.
PARENT_MAKE_VARIABLES = ("MAKEFLAGS", "MFLAGS", "MAKELEVEL")


def child_make_env(**extra: str) -> dict[str, str]:
    """The environment for a fixture's `make`, free of the make running this suite."""

    env = {key: value for key, value in os.environ.items() if key not in PARENT_MAKE_VARIABLES}
    env.update(extra)
    return env

PRD = """---
title: {title}
status: {status}
created: 2026-08-29
{extra}---

# PRD
"""


def prd(title: str = "An item", status: str = "planning", **extra: str) -> str:
    tail = "".join(f"{key}: {value}\n" for key, value in extra.items())
    return PRD.format(title=title, status=status, extra=tail)


class Fixture(unittest.TestCase):
    """A throwaway directory, and a git repository when one is asked for."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name).resolve()

    def git(self, cwd: pathlib.Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )

    def make_repo(self, name: str = "repo") -> pathlib.Path:
        root = self.tmp / name
        root.mkdir(parents=True)
        self.git(root, "init", "-b", "main")
        self.git(root, "config", "user.email", "test@example.com")
        self.git(root, "config", "user.name", "Test User")
        (root / "README.md").write_text("seed\n", encoding="utf-8")
        self.git(root, "add", "README.md")
        self.git(root, "commit", "-m", "seed")
        return root

    def add_worktree(self, root: pathlib.Path, name: str = "wt") -> pathlib.Path:
        path = self.tmp / name
        self.git(root, "worktree", "add", "-b", name, str(path))
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=str(root),
                check=False,
                capture_output=True,
            )
        )
        return path.resolve()

    def write_local_block(self, root: pathlib.Path, body: str) -> pathlib.Path:
        path = root / sd_lib.LOCAL_FILE_NAME
        path.write_text(
            f"# Local notes\n\n{sd_lib.LOCAL_BLOCK_START}\n{body}{sd_lib.LOCAL_BLOCK_END}\n",
            encoding="utf-8",
        )
        return path

    def write_item(
        self, root: pathlib.Path, relative: str, text: str | None = None
    ) -> pathlib.Path:
        item = root / relative
        item.mkdir(parents=True)
        if text is not None:
            (item / "prd.md").write_text(text, encoding="utf-8")
        return item


class RepoRootTests(Fixture):
    def test_outside_a_repository_is_none(self) -> None:
        outside = self.tmp / "loose"
        outside.mkdir()
        self.assertIsNone(sd_lib.repo_root(outside))

    def test_main_checkout_resolves_to_itself(self) -> None:
        root = self.make_repo()
        nested = root / "a" / "b"
        nested.mkdir(parents=True)
        self.assertEqual(sd_lib.repo_root(nested), root.resolve())
        self.assertEqual(sd_lib.main_worktree_root(root), root.resolve())

    def test_linked_worktree_resolves_to_the_worktree_not_the_checkout(self) -> None:
        root = self.make_repo()
        worktree = self.add_worktree(root)
        self.assertEqual(sd_lib.repo_root(worktree), worktree)
        self.assertNotEqual(sd_lib.repo_root(worktree), root.resolve())
        self.assertEqual(sd_lib.main_worktree_root(worktree), root.resolve())

    def test_a_missing_directory_is_not_a_repository(self) -> None:
        self.assertIsNone(sd_lib.repo_root(self.tmp / "nowhere" / "deeper"))

    def test_a_git_that_gave_no_answer_is_not_called_outside_a_repository(self) -> None:
        """sd:2986: a lane run under load read a git timeout as 'cwd is not inside a Git repository'."""
        root = self.make_repo()
        stalled = subprocess.TimeoutExpired(["git"], sd_lib.GIT_TIMEOUT_SECONDS)
        with unittest.mock.patch.object(sd_lib.subprocess, "run", side_effect=stalled):
            with self.assertRaisesRegex(sd_lib.ConfigError, "did not finish within"):
                sd_lib.repo_root(root)

    def test_every_git_call_overrides_fsmonitor_after_the_callers_own_entries_once(self) -> None:
        """sd:2993: the override outranks the repository's config file and keeps the caller's `GIT_CONFIG_*` entries."""
        caller = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.autocrlf", "GIT_CONFIG_VALUE_0": "false"}
        env = sd_lib.without_fsmonitor(caller)
        self.assertEqual([env[name] for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1")],
                         ["2", "core.autocrlf", "core.fsmonitor", "false"])
        self.assertEqual(sd_lib.without_fsmonitor(env), env)
        root = self.make_repo()
        self.git(root, "config", "core.fsmonitor", "true")
        read = subprocess.run(["git", "config", "--bool", "core.fsmonitor"], cwd=root, capture_output=True, text=True,
                              env=sd_lib.without_fsmonitor(os.environ), check=True)
        self.assertEqual(read.stdout.strip(), "false")


class LocalBlockTests(Fixture):
    def test_missing_file_is_empty(self) -> None:
        root = self.make_repo()
        self.assertEqual(sd_lib.local_block(root), {})

    def test_file_without_the_block_is_empty(self) -> None:
        root = self.make_repo()
        (root / sd_lib.LOCAL_FILE_NAME).write_text("just notes\n", encoding="utf-8")
        self.assertEqual(sd_lib.local_block(root), {})

    def test_existing_nonfile_local_paths_refuse_instead_of_looking_absent(self):
        root = self.make_repo()
        local = root / sd_lib.LOCAL_FILE_NAME
        local.mkdir()
        with self.assertRaises(sd_lib.ConfigError):
            sd_lib.local_block(root)
        local.rmdir()
        local.symlink_to(root / "missing-target")
        with self.assertRaises(sd_lib.ConfigError):
            sd_lib.local_block(root)
        self.assertTrue(local.is_symlink())
        non_directory = root / "file"
        non_directory.write_text("file")
        with self.assertRaises(sd_lib.ConfigError):
            sd_lib.local_block(non_directory)

    def test_scalars_comments_and_quotes(self) -> None:
        root = self.make_repo()
        self.write_local_block(
            root,
            "# a comment line\n"
            "mode: minimal\n"
            "check: make ci   # trailing comment\n"
            'title: "a # inside quotes"\n'
            "\n",
        )
        self.assertEqual(
            sd_lib.local_block(root),
            {"mode": "minimal", "check": "make ci", "title": "a # inside quotes"},
        )

    def test_a_linked_worktree_reads_the_main_checkout_copy(self) -> None:
        root = self.make_repo()
        self.write_local_block(root, "mode: guest\n")
        worktree = self.add_worktree(root)
        self.assertFalse((worktree / sd_lib.LOCAL_FILE_NAME).exists())
        self.assertEqual(sd_lib.local_block(worktree), {"mode": "guest"})
        self.assertEqual(sd_lib.local_block_path(worktree), root.resolve() / "CLAUDE.local.md")

    def test_malformed_blocks_are_controlled_errors(self) -> None:
        cases = [
            ("no end marker", f"{sd_lib.LOCAL_BLOCK_START}\nmode: full\n"),
            ("end without start", f"{sd_lib.LOCAL_BLOCK_END}\n"),
            (
                "duplicate start",
                f"{sd_lib.LOCAL_BLOCK_START}\n{sd_lib.LOCAL_BLOCK_START}\n"
                f"{sd_lib.LOCAL_BLOCK_END}\n",
            ),
            (
                "duplicate end",
                f"{sd_lib.LOCAL_BLOCK_START}\n{sd_lib.LOCAL_BLOCK_END}\n"
                f"{sd_lib.LOCAL_BLOCK_END}\n",
            ),
            (
                "line that is not key: value",
                f"{sd_lib.LOCAL_BLOCK_START}\nnot a pair\n{sd_lib.LOCAL_BLOCK_END}\n",
            ),
        ]
        for label, text in cases:
            with self.subTest(label):
                with self.assertRaises(sd_lib.ConfigError):
                    sd_lib.parse_local_block(text)

    def test_the_marker_check_stands_on_its_own(self) -> None:
        """`local_block_body` is the marker half of `parse_local_block`.

        The installer refreshes a block whose body the grammar refuses, so it
        needs the markers checked without the body parsed: a marker fault
        still raises, a body that is not `key: value` comes back as text, and
        no block at all is None rather than an empty dict.
        """
        with self.assertRaises(sd_lib.ConfigError):
            sd_lib.local_block_body(f"{sd_lib.LOCAL_BLOCK_START}\nmode: full\n")
        self.assertEqual(
            sd_lib.local_block_body(
                f"{sd_lib.LOCAL_BLOCK_START}\nnot a pair\n{sd_lib.LOCAL_BLOCK_END}\n"
            ),
            "\nnot a pair\n",
        )
        self.assertIsNone(sd_lib.local_block_body("no block here\n"))


class ModeTests(Fixture):
    def test_declared_modes_and_the_no_line_case_this_fixture_actually_is(self) -> None:
        """No `mode:` line is not a default of `full`; it is a question asked.

        This test used to read `mode(make_repo()) == "full"` and call that the
        default, which is what `bin/sd_lib.py` did and what criterion 11 says is
        wrong: `full` is the most permissive mode, and returning it for every
        repository with no line inverted detection in exactly the cases it
        exists for -- a fork, or a remote you cannot administer. `full` is still
        the right answer *here*, but for a stated reason rather than by default:
        `make_repo` adds no remote, so this is criterion 11's sixth case, where
        there is nobody to expose anything to. `tests/test_mode_detection.py`
        holds the other five and the composition rule; a repository with a
        remote is asked, and every answer but three yeses resolves to `guest`.
        """

        root = self.make_repo()
        self.assertIsNone(sd_lib.git_output(["remote", "get-url", "origin"], root))
        self.assertEqual(sd_lib.mode(root), "full")
        for value in sd_lib.MODES:
            with self.subTest(value):
                self.write_local_block(root, f"mode: {value}\n")
                self.assertEqual(sd_lib.mode(root), value)

    def test_unknown_mode_is_a_controlled_error(self) -> None:
        root = self.make_repo()
        self.write_local_block(root, "mode: readonly\n")
        with self.assertRaises(sd_lib.ConfigError):
            sd_lib.mode(root)


class MachineConfigTests(Fixture):
    def config_at(self, home: pathlib.Path) -> pathlib.Path:
        path = home / sd_lib.CONFIG_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(sd_lib.machine_config(self.tmp / "absent.json"), {})

    def test_reads_json(self) -> None:
        path = self.config_at(self.tmp / "cfg")
        path.write_text(json.dumps({"repos": {"a": 1}}), encoding="utf-8")
        self.assertEqual(sd_lib.machine_config(path), {"repos": {"a": 1}})

    def test_malformed_json_is_a_controlled_error(self) -> None:
        path = self.config_at(self.tmp / "cfg")
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(sd_lib.ConfigError):
            sd_lib.machine_config(path)

    def test_non_object_json_is_a_controlled_error(self) -> None:
        path = self.config_at(self.tmp / "cfg")
        path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(sd_lib.ConfigError):
            sd_lib.machine_config(path)

    def test_default_path_follows_xdg_config_home(self) -> None:
        home = self.tmp / "xdg"
        path = self.config_at(home)
        path.write_text(json.dumps({"seen": True}), encoding="utf-8")
        previous = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(home)
        try:
            self.assertEqual(sd_lib.machine_config_path(), path)
            self.assertEqual(sd_lib.machine_config(), {"seen": True})
        finally:
            if previous is None:
                del os.environ["XDG_CONFIG_HOME"]
            else:
                os.environ["XDG_CONFIG_HOME"] = previous


class RowFixture(Fixture):
    """A checkout, and an empty home holding the one database when asked for.

    Every status is the row's, so a test that reads one writes the row. `HOME`
    points at the scratch home for the whole test, so the operator's database
    is never opened.
    """

    database = True

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        patched = unittest.mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)
        self.root = self.make_repo("tree")
        self.connection = None
        if self.database:
            sd_db.initialise(home=self.home)
            self.connection = sd_db.connect(sd_db.default_path(self.home), write=True)
            self.addCleanup(self.connection.close)
            sd_db.writes.upsert_repo(self.connection, str(self.root))

    def seed(self, name: str, status: str, text: str | None = None, *, month: str | None = None,
             branch: str | None = None) -> pathlib.Path:
        """A folder with its `prd.md` and the row that owns its status."""
        where = f"docs/work/archive/{month}/{name}" if month else f"docs/work/{name}"
        item = self.write_item(self.root, where, prd(title=name, status=status)
                               if text is None else text)
        sd_db.writes.create_item(
            self.connection, kind="work", title=name, status=status, branch=branch,
            repo=str(self.root), source=sd_lib.ITEM_ROW_SOURCE,
            external_id=sd_lib.external_id(self.root, item),
        )
        return item


def line_free(title: str = "An item", **extra: str) -> str:
    """A `prd.md` as the retire left it: no `status:` line."""
    return prd(title=title, **extra).replace("status: planning\n", "")


class DeriveStatusTests(RowFixture):
    def test_each_row_status(self) -> None:
        for status in ("planning", "ready", "in_progress", "blocked"):
            with self.subTest(status):
                item = self.seed(f"2026-08-29-{status}", status, line_free())
                report = sd_lib.status_report(item)
                self.assertEqual(report.status, status)
                self.assertEqual(report.inconsistencies, ())
                self.assertFalse(report.archived)

    def test_a_done_row_nothing_delivered_is_reported_unmarked(self) -> None:
        item = self.seed("2026-08-29-done", "done", line_free())
        report = sd_lib.status_report(item)
        self.assertEqual(report.status, "done")
        self.assertIn("unmarked", " ".join(report.inconsistencies))

    def test_a_status_line_that_disagrees_with_the_row_is_stale(self) -> None:
        item = self.seed("2026-08-29-stale", "ready", prd(status="planning"))
        report = sd_lib.status_report(item)
        self.assertEqual(report.status, "ready")
        self.assertIn("the line is stale", " ".join(report.inconsistencies))

    def test_the_row_decides_not_the_line(self) -> None:
        item = self.seed("2026-08-29-line", "planning", prd(status="done"))
        self.assertEqual(sd_lib.status_report(item).status, "planning")

    def test_an_item_with_no_row_is_unknown(self) -> None:
        item = self.write_item(self.root, "docs/work/2026-08-29-orphan", line_free())
        report = sd_lib.status_report(item)
        self.assertEqual(report.status, "unknown")
        self.assertIn("holds no docs/work row", " ".join(report.inconsistencies))

    def test_a_prd_that_cannot_be_read_is_reported_beside_the_row(self) -> None:
        cases = [("no frontmatter", "# PRD\n"), ("no prd at all", None)]
        for index, (label, text) in enumerate(cases):
            with self.subTest(label):
                item = self.seed(f"2026-08-29-bad{index}", "planning", text)
                if text is None:
                    (item / "prd.md").unlink(missing_ok=True)
                report = sd_lib.status_report(item)
                self.assertEqual(report.status, "planning")
                self.assertTrue(report.inconsistencies)


class DatabaseFreeStatusTests(RowFixture):
    """A checkout or runner with no database asks git, and the archive is `done`."""

    database = False

    def test_archive_location_is_done(self) -> None:
        item = self.write_item(
            self.root, "docs/work/archive/2026-06/2026-06-01-old", prd(status="planning"))
        report = sd_lib.status_report(item)
        self.assertEqual(report.status, "done")
        self.assertTrue(report.archived)

    def test_an_open_item_with_no_delivery_evidence_reads_as_planning(self) -> None:
        item = self.write_item(self.root, "docs/work/2026-08-29-open", line_free())
        self.assertEqual(sd_lib.status_report(item).status, "planning")

    def test_an_open_item_recording_a_branch_reads_as_in_progress(self) -> None:
        item = self.write_item(
            self.root, "docs/work/2026-08-29-open", line_free(branch="task/open"))
        self.assertEqual(sd_lib.status_report(item).status, "in_progress")

    def test_a_status_line_does_not_decide(self) -> None:
        item = self.write_item(self.root, "docs/work/2026-08-29-line", prd(status="done"))
        self.assertEqual(sd_lib.status_report(item).status, "planning")


class WorkItemsTests(RowFixture):
    def test_enumerates_active_and_archived_from_the_filesystem(self) -> None:
        self.seed("2026-08-29-alpha", "in_progress", line_free(title="Alpha", branch="task/alpha"),
                  branch="task/alpha")
        self.seed("2026-08-28-beta", "ready", line_free(title="Beta"))
        self.seed("2026-06-01-gamma", "done", prd(title="Gamma", status="ready"), month="2026-06")
        (self.root / "docs/work/README.md").write_text("index\n", encoding="utf-8")

        items = sd_lib.work_items(self.root)
        self.assertEqual([item.slug for item in items], ["beta", "alpha", "gamma"])
        by_slug = {item.slug: item for item in items}
        self.assertEqual(by_slug["alpha"].status, "in_progress")
        self.assertEqual(by_slug["alpha"].branch, "task/alpha")
        self.assertEqual(by_slug["alpha"].title, "Alpha")
        self.assertEqual(by_slug["alpha"].created, "2026-08-29")
        self.assertFalse(by_slug["alpha"].archived)
        self.assertEqual(by_slug["beta"].status, "ready")
        self.assertEqual(by_slug["gamma"].status, "done")
        self.assertTrue(by_slug["gamma"].archived)

    def test_no_work_directory_is_an_empty_list(self) -> None:
        self.assertEqual(sd_lib.work_items(self.tmp / "empty"), [])


class EntrypointTests(Fixture):
    _made = 0

    def repo_with(self, files: dict[str, str]) -> pathlib.Path:
        EntrypointTests._made += 1
        root = self.tmp / f"tree{EntrypointTests._made}"
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def test_autodetect_branches(self) -> None:
        makefile = ".PHONY: check test lint\n\ncheck: test lint\n\ntest:\n\techo t\n\nlint:\n\techo l\n"
        taskfile = "version: '3'\n\ntasks:\n  test:\n    cmds:\n      - echo t\n  lint:\n    cmds:\n      - echo l\n"
        package = json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}})
        cases = [
            (
                "makefile",
                {"Makefile": makefile},
                "makefile",
                {"check": ["make", "check"], "test": ["make", "test"], "lint": ["make", "lint"]},
            ),
            (
                "taskfile",
                {"Taskfile.yml": taskfile},
                "taskfile",
                {"test": ["task", "test"], "lint": ["task", "lint"]},
            ),
            (
                "package.json",
                {"package.json": package},
                "package.json",
                {"test": ["npm", "run", "test"], "lint": ["npm", "run", "lint"]},
            ),
            (
                "cargo",
                {"Cargo.toml": "[package]\nname = 'x'\n"},
                "cargo",
                {"check": ["cargo", "check"], "test": ["cargo", "test"]},
            ),
            (
                "pyproject",
                {"pyproject.toml": "[project]\nname = 'x'\n"},
                "pyproject",
                {"test": ["python3", "-m", "pytest"]},
            ),
            ("nothing", {"README.md": "hi\n"}, None, {}),
        ]
        for label, files, source, commands in cases:
            with self.subTest(label):
                root = self.repo_with(files)
                detection = sd_lib.detect_entrypoints(root)
                self.assertEqual(detection.source, source)
                self.assertEqual(detection.commands, commands)
                self.assertEqual(sd_lib.entrypoints(root), commands)
                self.assertTrue(detection.reason)

    def test_pyproject_prefers_the_repo_venv_interpreter(self) -> None:
        # sd:1309: python3 on PATH is the one interpreter guaranteed not to
        # hold the repo's dependencies when they live in .venv.
        for layout in (("bin", "python"), ("Scripts", "python.exe")):
            with self.subTest(layout[0]):
                root = self.repo_with({"pyproject.toml": "[project]\nname = 'x'\n"})
                interpreter = root / ".venv" / layout[0] / layout[1]
                interpreter.parent.mkdir(parents=True)
                interpreter.write_text("", encoding="utf-8")
                interpreter.chmod(0o755)
                with unittest.mock.patch.dict(os.environ):
                    for name in ("VIRTUAL_ENV", "CONDA_PREFIX"):
                        os.environ.pop(name, None)
                    detection = sd_lib.detect_entrypoints(root)
                expected = str(pathlib.PurePosixPath(".venv", *layout))
                self.assertEqual(detection.commands, {"test": [expected, "-m", "pytest"]})
                self.assertIn(expected, detection.reason)

    def test_pyproject_keeps_an_activated_environment(self) -> None:
        # An activated environment (tox, another venv) is the caller's choice;
        # PATH resolves python3 to it, and the repo's .venv must not override.
        root = self.repo_with({"pyproject.toml": "[project]\nname = 'x'\n"})
        interpreter = root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("", encoding="utf-8")
        interpreter.chmod(0o755)
        activations = {
            "virtualenv": {"VIRTUAL_ENV": str(root / ".tox" / "py312")},
            "conda": {"CONDA_PREFIX": "/opt/conda/envs/py312", "CONDA_DEFAULT_ENV": "py312"},
            "conda base": {"CONDA_PREFIX": "/opt/conda", "CONDA_DEFAULT_ENV": "base"},
        }
        for label, activated in activations.items():
            with self.subTest(label), unittest.mock.patch.dict(os.environ, activated):
                detection = sd_lib.detect_entrypoints(root)
                self.assertEqual(detection.commands, {"test": ["python3", "-m", "pytest"]})

    def test_pyproject_ignores_a_venv_without_a_runnable_interpreter(self) -> None:
        root = self.repo_with({"pyproject.toml": "[project]\nname = 'x'\n", ".venv/pyvenv.cfg": ""})
        (root / ".venv" / "bin").mkdir()
        (root / ".venv" / "bin" / "python").symlink_to(root / "missing")
        with unittest.mock.patch.dict(os.environ):
            os.environ.pop("VIRTUAL_ENV", None)
            detection = sd_lib.detect_entrypoints(root)
        self.assertEqual(detection.commands, {"test": ["python3", "-m", "pytest"]})

    def test_probe_order_stops_at_the_first_hit(self) -> None:
        root = self.repo_with(
            {
                "Makefile": "check:\n\techo c\n",
                "Taskfile.yml": "tasks:\n  test:\n    cmds:\n      - echo t\n",
                "package.json": json.dumps({"scripts": {"test": "jest"}}),
            }
        )
        self.assertEqual(sd_lib.detect_entrypoints(root).source, "makefile")

    def test_a_makefile_without_those_targets_does_not_stop_the_search(self) -> None:
        root = self.repo_with(
            {
                "Makefile": "build:\n\techo b\n\ninstall:\n\techo i\n",
                "package.json": json.dumps({"scripts": {"test": "jest"}}),
            }
        )
        detection = sd_lib.detect_entrypoints(root)
        self.assertEqual(detection.source, "package.json")
        self.assertEqual(detection.commands, {"test": ["npm", "run", "test"]})

    def test_the_local_block_wins(self) -> None:
        root = self.make_repo()
        (root / "Makefile").write_text("check:\n\techo c\n", encoding="utf-8")
        self.write_local_block(root, "check: ./ci.sh --fast\nlint: ruff check .\n")
        detection = sd_lib.detect_entrypoints(root)
        self.assertEqual(detection.source, "local-block")
        self.assertEqual(
            detection.commands,
            {"check": ["./ci.sh", "--fast"], "lint": ["ruff", "check", "."]},
        )

    def test_an_unparseable_local_command_is_a_controlled_error(self) -> None:
        root = self.make_repo()
        self.write_local_block(root, "check: ./ci.sh 'unbalanced\n")
        with self.assertRaises(sd_lib.ConfigError):
            sd_lib.detect_entrypoints(root)

    def test_a_malformed_package_json_is_a_controlled_error(self) -> None:
        root = self.repo_with({"package.json": "{oops"})
        with self.assertRaises(sd_lib.ConfigError):
            sd_lib.detect_entrypoints(root)


class SharedParserTests(unittest.TestCase):
    """bin/sd-docs-lint reads frontmatter through this module, not a twin."""

    def test_docs_lint_imports_the_shared_parser(self) -> None:
        import importlib.machinery
        import importlib.util

        path = REPO_ROOT / "bin" / "sd-docs-lint"
        loader = importlib.machinery.SourceFileLoader("sd_docs_lint_shared", str(path))
        spec = importlib.util.spec_from_loader("sd_docs_lint_shared", loader)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIs(module.parse_frontmatter, sd_lib.parse_frontmatter)

    def test_frontmatter_conventions_are_unchanged(self) -> None:
        cases: list[tuple[str, str, dict[str, str] | None]] = [
            ("plain", "---\ntitle: A\nstatus: ready\n---\n", {"title": "A", "status": "ready"}),
            ("quoted", '---\ntitle: "PARKED: a thing"\n---\n', {"title": "PARKED: a thing"}),
            ("no block", "# PRD\n", None),
            ("unterminated", "---\ntitle: A\n", None),
            ("hash is literal", "---\ntitle: a # b\n---\n", {"title": "a # b"}),
        ]
        for label, text, expected in cases:
            with self.subTest(label):
                self.assertEqual(sd_lib.parse_frontmatter(text), expected)


class CorePolicyReadTests(Fixture):
    def test_supplied_home_and_xdg_select_policy_without_inheriting_the_process(self):
        one, two = self.tmp / "one", self.tmp / "two"
        one_path = one / ".config" / sd_lib.CONFIG_RELATIVE_PATH
        two_path = two / sd_lib.CONFIG_RELATIVE_PATH
        for path, value in ((one_path, "deny"), (two_path, "configured")):
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"config": {"sd": {"external_reviews": value}}}))
        self.assertEqual(sd_lib.core_setting("external_reviews", {"HOME": str(one)}), "deny")
        self.assertEqual(sd_lib.core_setting("external_reviews", {"HOME": str(one), "XDG_CONFIG_HOME": str(two)}), "configured")
        self.assertIsNone(sd_lib.core_setting("external_reviews", {"HOME": str(self.tmp / "fresh")}))

    def test_malformed_policy_never_becomes_a_grant(self):
        env = {"HOME": str(self.tmp)}
        path = self.tmp / ".config" / sd_lib.CONFIG_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        for value in ({"config": []}, {"config": {"sd": []}},
                      {"config": {"sd": {"external_reviews": None}}},
                      {"config": {"sd": {"external_reviews": True}}},
                      {"config": {"sd": {"external_reviews": "allow"}}}):
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                with self.assertRaises(sd_lib.ConfigError):
                    sd_lib.core_setting("external_reviews", env)

    def test_copilot_review_reads_its_three_words_and_nothing_else(self):
        env = {"HOME": str(self.tmp)}
        path = self.tmp / ".config" / sd_lib.CONFIG_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        self.assertIsNone(sd_lib.core_setting("copilot_review", env))
        for value in ("deep", "never", "always"):
            path.write_text(json.dumps({"config": {"sd": {"copilot_review": value}}}))
            self.assertEqual(sd_lib.core_setting("copilot_review", env), value)
        for value in ("Deep", "true", "", None, True):
            with self.subTest(value=value):
                path.write_text(json.dumps({"config": {"sd": {"copilot_review": value}}}))
                with self.assertRaises(sd_lib.ConfigError):
                    sd_lib.core_setting("copilot_review", env)

    def test_bulk_storage_root_reads_a_folder_and_is_empty_by_default(self):
        """sd:1792: the per-machine root for large uncommitted data; unset is no bulk root."""
        env = {"HOME": str(self.tmp)}
        path = self.tmp / ".config" / sd_lib.CONFIG_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        self.assertIsNone(sd_lib.core_setting("bulk_storage_root", env))
        for value in ("/Volumes/bulk/repo-storage", "~/bulk"):
            path.write_text(json.dumps({"config": {"sd": {"bulk_storage_root": value}}}))
            self.assertEqual(sd_lib.core_setting("bulk_storage_root", env), value)
        for value in ("relative/dir", "", None):
            with self.subTest(value=value):
                path.write_text(json.dumps({"config": {"sd": {"bulk_storage_root": value}}}))
                with self.assertRaises(sd_lib.ConfigError):
                    sd_lib.core_setting("bulk_storage_root", env)


class CopilotPolicyResolution(unittest.TestCase):
    """The two pure halves both lanes share (sd:1328): who decides, and
    whether the decided word selects a review of this tier."""

    def test_machine_never_then_repository_then_machine_then_default(self):
        self.assertEqual(sd_lib.copilot_policy(True, "never"), ("never", "machine config"))
        self.assertEqual(sd_lib.copilot_policy(False, "never"), ("never", "machine config"))
        self.assertEqual(sd_lib.copilot_policy(True, "always"), ("deep", "repository"))
        self.assertEqual(sd_lib.copilot_policy(False, "always"), ("never", "repository"))
        self.assertEqual(sd_lib.copilot_policy(False, "deep"), ("never", "repository"))
        self.assertEqual(sd_lib.copilot_policy(None, "always"), ("always", "machine config"))
        self.assertEqual(sd_lib.copilot_policy(None, "never"), ("never", "machine config"))
        self.assertEqual(sd_lib.copilot_policy(None, None), (sd_lib.COPILOT_REVIEW_DEFAULT, "machine default"))

    def test_the_word_selects_by_tier_and_always_respects_skip(self):
        self.assertTrue(sd_lib.copilot_automatic("deep", "deep", 1))
        self.assertFalse(sd_lib.copilot_automatic("deep", "standard", 1))
        self.assertFalse(sd_lib.copilot_automatic("never", "deep", 1))
        self.assertTrue(sd_lib.copilot_automatic("always", "cheap", 1))
        self.assertTrue(sd_lib.copilot_automatic("always", "deep", 1))
        self.assertFalse(sd_lib.copilot_automatic("always", "skip", 0))


class RowActivity(unittest.TestCase):
    """`Rows.activity`: what the database last recorded against an item.

    The half of sd:455 that git cannot answer. A `row` checkout records a
    triage decision as a note against the item and touches no file, so the item
    tree is byte-identical before and after and the only evidence that anything
    happened is in the database.

    **And `updated_at` is not that evidence on its own.** `sd_db.writes.add_note`
    inserts the note and leaves the item row alone -- measured on item 492,
    whose `comment` note stands four and a half hours after an `updated_at`
    that still equals its `created_at`. A reader that stopped at the row would
    have called a freshly-triaged item as idle as an abandoned one, which is
    the defect being fixed rather than a detail of it.
    """

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = pathlib.Path(os.path.realpath(scratch.name))
        sd_db.initialise(home=self.home)
        self.connection = sd_db.connect(sd_db.default_path(self.home), write=True)
        self.addCleanup(self.connection.close)

        self.root = self.home / "checkout"
        self.item_dir = self.root / sd_lib.WORK_DIR / "2026-01-01-x"
        self.item_dir.mkdir(parents=True)
        (self.item_dir / "prd.md").write_text("# x\n", encoding="utf-8")
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "T"], ["add", "-A"],
                     ["commit", "-qm", "first"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True,
                           capture_output=True)
        self.root = self.root.resolve()
        sd_db.writes.upsert_repo(self.connection, str(self.root))
        self.item = sd_db.writes.create_item(
            self.connection, kind="work", title="x", status="planning",
            repo=str(self.root), source=sd_lib.ITEM_ROW_SOURCE,
            external_id=sd_lib.external_id(self.root, self.item_dir),
        )

    def read(self) -> str:
        """`Rows` opens the default database, so `HOME` is what points it here."""
        with unittest.mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            rows = sd_lib.Rows(self.root)
            try:
                return rows.activity(self.item_dir)
            finally:
                rows.close()

    def test_a_row_with_no_notes_answers_with_its_own_stamp(self) -> None:
        stamp = self.connection.execute(
            "SELECT updated_at FROM item WHERE id = ?", (self.item,)
        ).fetchone()["updated_at"]
        self.assertEqual(self.read(), stamp)

    def test_a_note_is_newer_than_the_row_it_was_written_against(self) -> None:
        """The whole point: the note moves the answer and `updated_at` does not.

        The row and its `opened as` note are backdated first, so the decision
        note is strictly newer on every run. Read in the same second, all three
        stamps were equal and the test proved nothing; read across a second
        boundary, `fetchone()` returned the `opened as` note and the test
        failed on a correct answer (sd:1402).
        """
        before = "2026-01-01T00:00:00+00:00"
        with self.connection:
            self.connection.execute("UPDATE item SET updated_at = ? WHERE id = ?", (before, self.item))
            self.connection.execute("UPDATE note SET timestamp = ? WHERE item = ?", (before, self.item))
        sd_db.add_note(self.connection, self.item, "decision", "triaged live")
        note = self.connection.execute(
            "SELECT timestamp FROM note WHERE item = ? AND kind = 'decision'", (self.item,)
        ).fetchone()["timestamp"]
        after = self.connection.execute(
            "SELECT updated_at FROM item WHERE id = ?", (self.item,)
        ).fetchone()["updated_at"]

        self.assertEqual(after, before, "add_note is not supposed to move the row")
        self.assertGreater(note, before)
        self.assertEqual(self.read(), note)

    def test_an_item_the_database_does_not_hold_answers_empty(self) -> None:
        """Empty is every absence, and it never lowers an age."""
        absent = self.root / sd_lib.WORK_DIR / "2026-01-01-nothing"
        absent.mkdir(parents=True)
        with unittest.mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            rows = sd_lib.Rows(self.root)
            self.addCleanup(rows.close)
            self.assertEqual(rows.activity(absent), "")

    def test_a_checkout_with_no_database_answers_empty(self) -> None:
        """`Statuses` over an empty home holds `Rows` that opened nothing."""
        with tempfile.TemporaryDirectory() as empty, unittest.mock.patch.dict(
            os.environ, {"HOME": empty}
        ):
            statuses = sd_lib.Statuses.of(self.root)
            self.addCleanup(statuses.close)
            self.assertFalse(statuses.rows.opened)
            self.assertEqual(sd_lib._recorded(statuses, self.item_dir), "")


class DisplayFieldsTests(unittest.TestCase):
    """The membership comes from the row; only the order comes from the caller."""

    def test_a_key_the_caller_never_named_is_still_returned(self) -> None:
        """sd:602. The two contribution renderers dropped these in silence."""
        row = {"url": 1, "blocking_labels": ["blocked"], "revision": "abc"}
        self.assertEqual(
            ["url", "blocking_labels", "revision"],
            sd_lib.display_fields(row, ("url",)),
        )

    def test_named_keys_lead_in_the_caller_s_order_and_the_rest_are_sorted(self) -> None:
        # The named pair is deliberately out of alphabetical order. With
        # ("first", "second") a sort of the named keys is invisible, and the
        # test passes on an implementation that ignores the caller entirely.
        row = {"zeta": 1, "alpha": 2, "second": 3, "first": 4}
        self.assertEqual(
            ["second", "first", "alpha", "zeta"],
            sd_lib.display_fields(row, ("second", "first")),
        )

    def test_a_key_the_header_already_printed_is_not_repeated(self) -> None:
        row = {"title": 1, "lane": 2, "url": 3, "extra": 4}
        self.assertEqual(
            ["url", "extra"],
            sd_lib.display_fields(row, ("url",), ("title", "lane")),
        )

    def test_a_named_key_the_row_lacks_is_still_offered_to_the_caller(self) -> None:
        """The caller filters empties; this function does not read values.

        Returning only present keys would make the order depend on the data,
        so two rows of the same kind would print their fields differently.
        """
        self.assertEqual(["a", "b"], sd_lib.display_fields({}, ("a", "b")))

    def test_a_key_named_twice_in_the_order_is_offered_once(self) -> None:
        """A hand-written tuple acquires a repeat, and it printed twice."""
        row = {"url": 1, "extra": 2}
        self.assertEqual(
            ["url", "local_status", "extra"],
            sd_lib.display_fields(row, ("url", "local_status", "url")),
        )

    def test_the_result_never_repeats_a_key(self) -> None:
        row = {"a": 1, "b": 2}
        fields = sd_lib.display_fields(row, ("a", "a", "b"), ())
        self.assertEqual(sorted(set(fields)), sorted(set(row)))


# -- the aging basis ---------------------------------------------------------
#
# `item_date`, `last_active` and `touched` came out of the 45-day age sweep
# when sd:10's criterion 21 cut it; the cases below came with them. `today`
# is a literal in every one: the rule under test is arithmetic, and a test
# that read the clock would pass or fail depending on the day it ran.


def dated_item(repo: pathlib.Path, name: str, **fields) -> sd_lib.WorkItem:
    """One work item on disk, read back through `work_item`."""
    item = repo / "docs" / "work" / name
    item.mkdir(parents=True)
    lines = ["---", f"title: {name}"]
    lines += [f"{key}: {value}" for key, value in fields.items() if value is not None]
    lines += ["---", "", "# body"]
    (item / "prd.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sd_lib.work_item(item)


class ItemDate(unittest.TestCase):
    """When an item dated itself, and when nothing did."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = pathlib.Path(tmp.name) / "repo"
        (self.repo / "docs" / "work").mkdir(parents=True)

    def test_created_is_the_date(self) -> None:
        item = dated_item(self.repo, "2026-07-01-old", status="planning", created="2026-07-01")
        self.assertEqual(sd_lib.item_date(item), datetime.date(2026, 7, 1))

    def test_the_directory_prefix_dates_an_item_whose_frontmatter_does_not(self) -> None:
        """Every templated item carries one, and the bulk-park sorted on it."""
        item = dated_item(self.repo, "2026-01-01-no-created-line", status="planning")
        self.assertEqual(sd_lib.item_date(item), datetime.date(2026, 1, 1))

    def test_created_outranks_the_directory_prefix(self) -> None:
        """The item's own statement about itself wins.

        A directory renamed or copied from another item carries a date that is
        not this item's, so the frontmatter is the more trustworthy of the two
        whenever both exist.
        """
        item = dated_item(self.repo, "2020-01-01-stale-prefix", status="planning",
                          created="2026-08-30")
        self.assertEqual(sd_lib.item_date(item), datetime.date(2026, 8, 30))

    def test_an_unparseable_created_falls_back_rather_than_crashing(self) -> None:
        """`created: soon` is a real thing a person types."""
        item = dated_item(self.repo, "2026-01-01-vague", status="planning", created="soon")
        self.assertEqual(sd_lib.item_date(item), datetime.date(2026, 1, 1))

    def test_a_garbage_date_everywhere_is_undated_not_an_error(self) -> None:
        item = dated_item(self.repo, "2026-13-45-impossible", status="planning", created="nope")
        self.assertIsNone(sd_lib.item_date(item))

    def test_no_date_anywhere_is_undated(self) -> None:
        item = dated_item(self.repo, "untitled-thing", status="planning")
        self.assertIsNone(sd_lib.item_date(item))


class LastActive(unittest.TestCase):
    """The aging basis: what counts as something happening to an item.

    `item_date` answers when an item began. `last_active` answers when anything
    last happened to it, and it is what the threshold is measured from -- the
    two are different questions, and `idle-planning` read the first one while
    saying the second. This is the one definition `sd-status` reads, so it is
    where the difference is pinned.
    """

    ITEM = datetime.date(2026, 1, 1)

    def last(self, name: str = "2026-01-01-x", activity: str = "",
             marks: dict | None = None) -> datetime.date:
        return sd_lib.last_active(self.ITEM, name, activity, marks)

    def test_an_item_with_no_evidence_ages_from_its_own_date(self) -> None:
        """The floor, and the whole of the old behaviour."""
        self.assertEqual(self.last(), self.ITEM)

    def test_a_commit_on_its_directory_is_activity(self) -> None:
        commit = datetime.date(2026, 9, 5)
        self.assertEqual(self.last(marks={"2026-01-01-x": commit}), commit)

    def test_a_database_stamp_is_activity(self) -> None:
        """A note carries a full timestamp; only its day is read."""
        self.assertEqual(
            self.last(activity="2026-09-06T09:14:00+00:00"),
            datetime.date(2026, 9, 6),
        )

    def test_the_latest_of_the_three_wins_rather_than_the_first_found(self) -> None:
        """A union, not a precedence chain: each source is blind where the
        others see, so the newest evidence is the answer whichever gave it."""
        marks = {"2026-01-01-x": datetime.date(2026, 8, 1)}
        self.assertEqual(
            self.last(activity="2026-09-06T09:14:00+00:00", marks=marks),
            datetime.date(2026, 9, 6),
        )
        self.assertEqual(
            self.last(activity="2026-07-01T09:14:00+00:00", marks=marks),
            datetime.date(2026, 8, 1),
        )

    def test_an_unparseable_stamp_lowers_nothing(self) -> None:
        """Absent evidence degrades to the floor, never below it."""
        self.assertEqual(self.last(activity="not a date"), self.ITEM)
        self.assertEqual(self.last(activity=""), self.ITEM)

    def test_a_commit_on_another_item_is_not_this_item_s_activity(self) -> None:
        self.assertEqual(
            self.last(marks={"2026-01-01-other": datetime.date(2026, 9, 5)}),
            self.ITEM,
        )


class ItemDirectory(unittest.TestCase):
    """Which item a tracked path belongs to, for `touched`'s one `git log`."""

    def resolve(self, path: str) -> str:
        return sd_lib._item_directory(path, "docs/work")

    def test_a_file_in_an_item_names_that_item(self) -> None:
        self.assertEqual(self.resolve("docs/work/2026-01-01-x/prd.md"), "2026-01-01-x")

    def test_an_archived_item_is_named_through_its_month(self) -> None:
        self.assertEqual(
            self.resolve("docs/work/archive/2026-08/2026-01-01-x/prd.md"),
            "2026-01-01-x",
        )

    def test_a_file_directly_in_the_work_directory_names_no_item(self) -> None:
        """`.status-source` lives there and is not an item."""
        self.assertEqual(self.resolve("docs/work/.status-source"), "")

    def test_a_path_outside_the_work_directory_names_no_item(self) -> None:
        self.assertEqual(self.resolve("bin/sd-status"), "")


class Touched(unittest.TestCase):
    """One `git log` per root, and what it maps."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = pathlib.Path(tmp.name) / "repo"
        (self.repo / "docs" / "work").mkdir(parents=True)
        self.at("init", "--quiet", "--initial-branch=main")
        self.at("config", "user.email", "t@example.com")
        self.at("config", "user.name", "t")
        self.at("config", "commit.gpgsign", "false")

    def at(self, *args: str, when: str | None = None) -> None:
        """One git call in the fixture repository.

        Not named `run`: that is `TestCase.run`, and overriding it stops the
        case from running at all -- which it did, loudly, on the first draft.
        """
        env = {**os.environ}
        if when:
            env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = when
        subprocess.run(["git", *args], cwd=self.repo, check=True,
                       capture_output=True, env=env)

    def commit(self, name: str, when: str) -> None:
        """One item, written as a real item and committed on a chosen day."""
        item = self.repo / "docs" / "work" / name
        if item.exists():
            (item / "touched").write_text(when, encoding="utf-8")
        else:
            dated_item(self.repo, name, status="planning")
        self.at("add", "-A")
        self.at("commit", "--quiet", "-m", f"touch {name}", when=when)

    def test_each_item_maps_to_the_day_it_was_last_committed_to(self) -> None:
        self.commit("2026-01-01-a", "2026-08-01T12:00:00 +0000")
        self.commit("2026-01-01-b", "2026-09-05T12:00:00 +0000")
        self.assertEqual(
            sd_lib.touched(self.repo),
            {"2026-01-01-a": datetime.date(2026, 8, 1),
             "2026-01-01-b": datetime.date(2026, 9, 5)},
        )

    def test_the_latest_commit_wins_and_not_the_first_one(self) -> None:
        """`git log` is newest first, so the first sighting is the latest."""
        self.commit("2026-01-01-a", "2026-08-01T12:00:00 +0000")
        self.commit("2026-01-01-a", "2026-09-05T12:00:00 +0000")
        self.assertEqual(
            sd_lib.touched(self.repo), {"2026-01-01-a": datetime.date(2026, 9, 5)}
        )

    def test_a_directory_that_is_not_a_checkout_is_empty_and_not_an_error(self) -> None:
        """Git refusing and git finding nothing leave the age on its other two
        sources, so there is no third state for a caller to handle."""
        loose = self.repo.parent / "not-a-checkout"
        loose.mkdir()
        self.assertEqual(sd_lib.touched(loose), {})


class SetupStaysLocalTests(unittest.TestCase):
    """`make setup` provisions the checkout it stands in, never a borrowed one.

    The virtualenv fallback that lets a worktree *use* the main checkout's
    environment must not let it *rewrite* one: `setup` would install this
    branch's pinned requirements over the environment another session is
    running on. Borrowing is for commands that consume an environment.
    """

    MAKEFILE = pathlib.Path(__file__).resolve().parents[1] / "Makefile"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name).resolve()
        self.main = self.tmp / "main"
        self.main.mkdir()
        self.git("init", "-q", "-b", "main", cwd=self.main)
        (self.main / "Makefile").write_text(
            self.MAKEFILE.read_text(encoding="utf-8"), encoding="utf-8")
        # A real executable, because the fallback tests for one.
        venv_python = self.main / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
        venv_python.chmod(0o755)
        self.git("add", "Makefile", cwd=self.main)
        self.git("-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "-m", "makefile", cwd=self.main)
        self.linked = self.tmp / "linked"
        self.git("worktree", "add", "-q", str(self.linked), cwd=self.main)

    def git(self, *args: str, cwd: pathlib.Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)

    def make(self, target: str, *extra: str, cwd: pathlib.Path | None = None) -> str:
        env = child_make_env()
        env.pop("SERVE", None)
        done = subprocess.run(["make", "-s", "-n", target, *extra],
                              cwd=cwd or self.linked, capture_output=True, text=True,
                              env=env)
        return done.stdout

    def test_setup_in_a_worktree_does_not_touch_the_main_virtualenv(self) -> None:
        recipe = self.make("setup")
        self.assertIn('-m venv ".venv"', recipe)
        self.assertNotIn(str(self.main / ".venv"), recipe)

    def test_an_explicit_venv_is_still_honoured(self) -> None:
        """`origin` distinguishes a deliberate VENV from this Makefile's default."""

        recipe = self.make("setup", f"VENV={self.tmp}/chosen")
        self.assertIn(f'-m venv "{self.tmp}/chosen"', recipe)

    def test_setup_takes_sd_db_out_of_the_environment_it_builds(self) -> None:
        """sd:3280. No copy goes in; one left by an older setup or self-install comes out.

        A caller that still reads the venv copy then fails with `ImportError`
        instead of running stale code. `LIVE_VENV` went with the A/B slots (sd:3009).
        """

        recipe = self.make("setup", f"VENV={self.tmp}/chosen", f"LIVE_VENV={self.tmp}/live")
        self.assertIn(f'"{self.tmp}/chosen/bin/python" -m pip uninstall --yes --quiet sd-db\n', recipe)
        self.assertNotIn("--provision-library", recipe)
        self.assertNotIn(f"{self.tmp}/live", recipe)

    def test_setup_ends_by_serving_from_the_dedicated_checkout(self) -> None:
        """sd:1118: `make setup` is what refreshes the serving checkout; `SERVE=no` skips it."""

        recipe = self.make("setup", cwd=self.main)
        self.assertTrue(recipe.rstrip().endswith('".venv/bin/python" bin/sd_install.py --serve'), recipe)
        self.assertNotIn("--serve", self.make("setup", "SERVE=no", cwd=self.main))

    def test_setup_in_a_linked_worktree_serves_only_when_asked(self) -> None:
        """sd:5551: the tree serves the checkout's HEAD, so a worktree's setup served unmerged code.

        A linked worktree skips the step and says so; `SERVE=yes` is the deliberate way back.
        """

        recipe = self.make("setup")
        self.assertNotIn("--serve", recipe)
        self.assertIn("not serving the machine", recipe)
        self.assertIn("SERVE=yes", recipe)
        self.assertTrue(self.make("setup", "SERVE=yes").rstrip().endswith("bin/sd_install.py --serve"))
        self.assertNotIn("not serving", self.make("setup", "SERVE=no"))

    def test_setup_where_git_cannot_say_serves_only_when_asked(self) -> None:
        """A failed probe is no answer: unknown is not taken for the main checkout."""

        loose = self.tmp / "loose"
        loose.mkdir()
        (loose / "Makefile").write_text(self.MAKEFILE.read_text(encoding="utf-8"), encoding="utf-8")
        env = child_make_env(GIT_CEILING_DIRECTORIES=str(self.tmp))
        env.pop("SERVE", None)
        done = subprocess.run(["make", "-s", "-n", "setup"], cwd=loose, capture_output=True, text=True, env=env)
        self.assertNotIn("--serve", done.stdout)
        self.assertIn("cannot tell whether", done.stdout)


class SetupUninstallsAPlantedCopyTests(unittest.TestCase):
    """sd:3280 failure table, step 3 older state: a venv copy from step 1 or a self-install.

    Runs the uninstall line `make -n setup` prints against a real venv
    holding a real `sd_db` built from `lib/`; afterwards `python -I` cannot
    import it, and a second run (nothing installed) still exits 0.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def test_the_uninstall_line_removes_a_planted_copy_and_reruns_clean(self) -> None:
        from tests.clean_env import clean_environment
        with tempfile.TemporaryDirectory() as tmp:
            venv = pathlib.Path(tmp) / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
            python = venv / "bin" / "python"
            subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--no-index", str(self.ROOT / "lib")],
                           check=True, capture_output=True, text=True, env=clean_environment())
            probe = [str(python), "-I", "-c", "import sd_db"]
            self.assertEqual(subprocess.run(probe, capture_output=True, check=False).returncode, 0)
            recipe = subprocess.run(["make", "-s", "-n", "setup", f"VENV={venv}", "SERVE=no"],
                                    cwd=self.ROOT, capture_output=True, text=True, check=True,
                                    env=child_make_env()).stdout
            line = next(line for line in recipe.splitlines() if "pip uninstall" in line)
            for _ in range(2):
                done = subprocess.run(["sh", "-c", line], capture_output=True, text=True, check=False,
                                      env=clean_environment())
                self.assertEqual(done.returncode, 0, done.stderr)
            gone = subprocess.run(probe, capture_output=True, text=True, check=False)
        self.assertNotEqual(gone.returncode, 0)
        self.assertIn("No module named 'sd_db'", gone.stderr)


class BorrowedEnvironmentTests(unittest.TestCase):
    """A worktree borrows the main checkout's virtualenv only when it fits.

    The fallback that lets a linked worktree use the environment the main
    checkout provisioned assumed the two were interchangeable. They are not.
    A branch that moves a pin in requirements-dev.txt or
    requirements-security.txt would lint, test and audit against another
    branch's versions, and `make audit` would go on promising the
    requirements-security.txt scanner while running whatever the other branch
    installed -- a misleading pass or a misleading failure, with nothing said
    either way.

    So `make setup` leaves a copy of the two files inside the environment it
    provisions, and the borrow is allowed only where those copies are this
    tree's. Everything here reads `make -n`: the decision is a variable, so it
    is settled before a recipe would run.

    A `.venv` the checkout carries as a real directory is its own environment
    and is not checked. A `.venv` that is a symlink into another checkout is
    checked, because that is the same borrow under a local name, and it is
    how sd:1020 says a worktree here gets an environment -- but on a proven
    mismatch only, so an environment that predates the record goes on working.
    """

    MAKEFILE = pathlib.Path(__file__).resolve().parents[1] / "Makefile"
    REQUIREMENTS = ("requirements-dev.txt", "requirements-security.txt")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name).resolve()
        self.main = self.tmp / "main"
        self.main.mkdir()
        self.git("init", "-q", "-b", "main", cwd=self.main)
        (self.main / "Makefile").write_text(
            self.MAKEFILE.read_text(encoding="utf-8"), encoding="utf-8")
        for name in self.REQUIREMENTS:
            (self.main / name).write_text(f"# {name}\n", encoding="utf-8")
        self.git("add", "-A", cwd=self.main)
        self.git("-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "-m", "base", cwd=self.main)
        # A real executable, because the fallback tests for one, and beside it
        # the record a `make setup` of this same tree would have left.
        venv_python = self.main / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
        venv_python.chmod(0o755)
        self.record = self.main / ".venv" / "sd-requirements"
        self.record.mkdir()
        for name in self.REQUIREMENTS:
            (self.record / name).write_text(f"# {name}\n", encoding="utf-8")
        self.linked = self.tmp / "linked"
        self.git("worktree", "add", "-q", str(self.linked), cwd=self.main)

    def git(self, *args: str, cwd: pathlib.Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)

    def make(self, *argv: str, cwd: pathlib.Path | None = None
             ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["make", "-s", "-n", *argv],
                              cwd=cwd or self.linked,
                              capture_output=True, text=True,
                              env=child_make_env())

    def move_a_pin(self, name: str = "requirements-dev.txt") -> None:
        (self.linked / name).write_text(f"# {name}\nmoved==2\n", encoding="utf-8")

    def mark_mid_provision(self) -> pathlib.Path:
        """The file `make setup` leaves in an environment while it builds it."""

        marker = self.main / ".venv" / "sd-provisioning"
        marker.write_text("building\n", encoding="utf-8")
        return marker

    def test_a_mid_provision_environment_is_refused_through_the_link(self) -> None:
        """The reviewer's state: record gone, marker present, sd:1020 link.

        `setup` removes the record before it mutates anything, so from the
        outside a half-built environment looks exactly like one provisioned
        before the record existed -- and the symlink path treats that as
        legacy and borrows it with a note. The marker is what tells the two
        apart, and on this path it has to refuse and not merely say so: the
        packages are being replaced while the borrower reads them.
        """

        self.symlink_the_environment()
        self.record.rename(self.main / ".venv" / "not-the-record")
        self.mark_mid_provision()
        done = self.make("docs-lint")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("refusing-to-borrow", done.stderr)
        self.assertIn("mid-provision", done.stderr)
        self.assertNotIn("records no provisioning", done.stderr)
        self.assertNotIn(str(self.main / ".venv" / "bin" / "python"), done.stdout)

    def test_a_mid_provision_environment_is_refused_on_the_strict_path(self) -> None:
        """The same fact on the other path, with the record still in place.

        The record matching proves the requirements agree; it says nothing
        about whether the packages behind them are the ones installed. So the
        marker is checked before the record is compared, not after it fails.
        """

        self.mark_mid_provision()
        done = self.make("docs-lint")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("mid-provision", done.stderr)

    def test_the_refusal_names_the_checkout_to_finish_it_in(self) -> None:
        """`make setup VENV=.venv` is the wrong remedy for a marked environment.

        Forking a second environment out of a run that is still going is not
        what the reader wants, and a marker left by a killed `make` has to be
        recoverable rather than permanent: the way out is to finish the
        provision where it started.
        """

        self.mark_mid_provision()
        done = self.make("docs-lint")
        self.assertIn(f"run 'make setup' in {self.main}", done.stderr)
        self.assertNotIn("VENV=.venv", done.stderr)

    def test_setup_still_runs_in_a_worktree_refused_for_the_marker(self) -> None:
        """The refusal fires at expansion, and `setup` reaches SETUP_VENV."""

        self.symlink_the_environment()
        self.mark_mid_provision()
        done = self.make("setup", "VENV=.venv")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_an_unmarked_environment_with_no_record_is_still_legacy(self) -> None:
        """The pin: absence of the record without the marker keeps its meaning.

        This is every machine on the day the record landed, and the marker
        must not turn it into a refusal. The note is the whole of the
        difference, and it is the one this same state got before the marker
        existed.
        """

        self.symlink_the_environment()
        self.record.rename(self.main / ".venv" / "not-the-record")
        self.assertFalse((self.main / ".venv" / "sd-provisioning").exists())
        done = self.make("docs-lint")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("records no provisioning", done.stderr)
        self.assertNotIn("refusing-to-borrow", done.stderr)

    def test_every_implementation_spells_the_marker_the_same(self) -> None:
        """Two users of one name, and no import between them.

        The Makefile writes the file and `hooks/pre-commit` reads it. The
        Makefile cannot import the hook, so the name is shared by spelling --
        which is only safe while something fails when one is renamed alone.
        """

        root = pathlib.Path(__file__).resolve().parents[1]
        for path in (root / "Makefile", root / "hooks" / "pre-commit"):
            self.assertIn("sd-provisioning", path.read_text(encoding="utf-8"),
                          f"{path.name} does not name the marker")

    def test_an_environment_that_matches_is_borrowed(self) -> None:
        done = self.make("docs-lint")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(str(self.main / ".venv" / "bin" / "python"), done.stdout)

    def test_a_moved_pin_refuses_and_names_the_file(self) -> None:
        self.move_a_pin("requirements-security.txt")
        done = self.make("docs-lint")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("requirements-security.txt", done.stderr)
        self.assertIn("make setup VENV=.venv", done.stderr)
        self.assertNotIn(str(self.main / ".venv"), done.stdout)

    def test_an_environment_with_no_record_refuses(self) -> None:
        """The case every machine is in the day this lands.

        An environment provisioned before the record existed cannot say what
        it holds, so it is refused rather than borrowed on the assumption
        that it fits.
        """

        self.record.rename(self.main / ".venv" / "not-the-record")
        done = self.make("docs-lint")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("does not record what it was provisioned from", done.stderr)

    def test_an_explicit_venv_is_honoured_even_when_the_borrow_is_refused(self) -> None:
        """`VENV=` is a deliberate choice; the check is on the automatic borrow."""

        self.move_a_pin()
        done = self.make("docs-lint", f"VENV={self.tmp}/chosen")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"{self.tmp}/chosen/bin/python", done.stdout)

    def test_setup_is_still_runnable_in_a_worktree_that_was_refused(self) -> None:
        """The refusal names `make setup`, so `make setup` must not refuse."""

        self.move_a_pin()
        done = self.make("setup")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('-m venv ".venv"', done.stdout)

    # That `setup` records what it provisioned is asserted in
    # `ProvisioningRecordTests`, against a `make setup` that actually runs.
    # It was a `make -n` text match here until the recipe learned to publish
    # the record by renaming a directory built beside it -- at which point the
    # match broke while the behaviour it stood for was unchanged, which is
    # what a test of a recipe's spelling is worth.

    def symlink_the_environment(self) -> None:
        """What sd:1020 does by hand: a `.venv` link into the main checkout.

        It passes `[ -x .venv/bin/python ]`, so it is a borrow wearing a local
        name, and it is the usual way a worktree here gets an environment.
        """

        (self.linked / ".venv").symlink_to(self.main / ".venv")

    def test_a_symlinked_environment_that_matches_is_used(self) -> None:
        self.symlink_the_environment()
        done = self.make("docs-lint")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('".venv/bin/python"', done.stdout)

    def test_a_symlinked_environment_is_compared_too(self) -> None:
        """The hole a local-first test would leave: a link is not an own one."""

        self.symlink_the_environment()
        self.move_a_pin()
        done = self.make("docs-lint")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("requirements-dev.txt", done.stderr)
        # Named by what it points at, which is the environment at issue.
        self.assertIn(str(self.main / ".venv"), done.stderr)

    def test_a_symlinked_environment_with_no_record_runs_and_says_so(self) -> None:
        """Proven mismatch only, where the automatic borrow is strict -- and
        a note, because a lenient pass has to be legible as one.

        The link is not this Makefile's doing, and refusing an environment
        that predates the record would strand every checkout carrying one.
        The day it is provisioned again the case is covered like any other,
        which `test_a_symlinked_environment_is_compared_too` is.

        Until then the rule covers nothing in the case that actually occurs
        on this machine, so it says so on stderr: an exit code that cannot be
        told from the check having run is the shape this repository refuses
        elsewhere. sd:1349 is the row; the note is how a reader finds it.
        """

        self.symlink_the_environment()
        self.record.rename(self.main / ".venv" / "not-the-record")
        self.move_a_pin()
        done = self.make("docs-lint")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('".venv/bin/python"', done.stdout)
        self.assertIn("records no provisioning", done.stderr)
        self.assertIn(str(self.main / ".venv"), done.stderr)
        self.assertIn(f"Run 'make setup' in {self.main}", done.stderr)

    def test_a_matching_symlinked_environment_stays_quiet(self) -> None:
        """The note is the absent-record case, not every symlink."""

        self.symlink_the_environment()
        done = self.make("docs-lint")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("records no provisioning", done.stderr)

    def test_a_checkout_with_its_own_environment_is_never_checked(self) -> None:
        """Nothing was borrowed, so there is nothing to be compatible with."""

        (self.main / "requirements-dev.txt").write_text("moved==2\n",
                                                        encoding="utf-8")
        done = self.make("docs-lint", cwd=self.main)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('".venv/bin/python"', done.stdout)


class ProvisioningRecordTests(unittest.TestCase):
    """`make setup` and the record it leaves, in the two orders that matter.

    The record is what lets a worktree borrow an environment, so a record
    that is present while the environment is in motion is worse than none:
    it is a claim of compatibility made about packages that are being
    replaced. And the recipe has to be runnable in the state its own refusal
    recommends it from, which is a worktree whose `.venv` is an sd:1020 link.

    These run `make setup` for real, against a stand-in interpreter, because
    what is under test is the order of the steps and the state each failure
    leaves behind -- neither of which `make -n` can show.
    """

    MAKEFILE = pathlib.Path(__file__).resolve().parents[1] / "Makefile"
    REQUIREMENTS = ("requirements-dev.txt", "requirements-security.txt")

    #: Stands in for the interpreter the recipe calls. `-m venv <dir>` copies
    #: it to <dir>/bin/python, so the steps after it run it too, and each
    #: step's exit code is an environment variable the test sets. `-c` prints
    #: the version baked in on the first line, so a copy answers with its
    #: creator's. `pyvenv.cfg` is written on every run, as venv writes it. It
    #: mirrors the two real behaviours the recipe works around
    #: -- CPython's venv refuses a path that is a symlink, and keeps a
    #: `bin/python` that is already there, even a dangling link -- and
    #: `test_the_real_interpreter_refuses_a_symlinked_target` and
    #: `test_the_real_interpreter_keeps_an_existing_python` are what keep that
    #: mirror honest rather than convenient.
    STUB = """#!/bin/sh
version=3.15
last=""
for a in "$@"; do last="$a"; done
case " $* " in
  *" -m venv "*)
    if [ -L "$last" ]; then
      echo "Error: Unable to create directory '$last'" >&2
      exit 1
    fi
    mkdir -p "$last/bin" || exit 1
    echo "version = $version" > "$last/pyvenv.cfg" || exit 1
    if [ ! -e "$last/bin/python" ] && [ ! -L "$last/bin/python" ]; then
      cp "$0" "$last/bin/python" || exit 1
      chmod +x "$last/bin/python"
    fi
    exit 0
    ;;
  *" -c "*) echo "$version"; exit 0 ;;
  *" -m pip uninstall "*) exit "${STUB_UNINSTALL_EXIT:-0}" ;;
  *" -m pip "*) exit "${STUB_PIP_EXIT:-0}" ;;
esac
exit 0
"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name).resolve()
        self.stub = self.tmp / "stub-python"
        self.stub.write_text(self.STUB, encoding="utf-8")
        self.stub.chmod(0o755)

        self.main = self.tmp / "main"
        self.main.mkdir()
        self.git("init", "-q", "-b", "main", cwd=self.main)
        (self.main / "Makefile").write_text(
            self.MAKEFILE.read_text(encoding="utf-8"), encoding="utf-8")
        for name in self.REQUIREMENTS:
            (self.main / name).write_text(f"# {name}\n", encoding="utf-8")
        self.git("add", "-A", cwd=self.main)
        self.git("-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "-m", "base", cwd=self.main)
        # An environment in the main checkout, with the record `make setup`
        # would have left, so a worktree linking to it is the live shape.
        self.main_venv = self.main / ".venv"
        (self.main_venv / "bin").mkdir(parents=True)
        (self.main_venv / "bin" / "python").write_text("#!/bin/sh\n",
                                                       encoding="utf-8")
        (self.main_venv / "bin" / "python").chmod(0o755)
        (self.main_venv / "sd-requirements").mkdir()
        for name in self.REQUIREMENTS:
            (self.main_venv / "sd-requirements" / name).write_text(
                f"# {name}\n", encoding="utf-8")

        self.linked = self.tmp / "linked"
        self.git("worktree", "add", "-q", str(self.linked), cwd=self.main)
        self.record = self.linked / ".venv" / "sd-requirements"

    def git(self, *args: str, cwd: pathlib.Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)

    def setup(self, venv: str = ".venv", python: pathlib.Path | None = None,
              **env: str) -> subprocess.CompletedProcess[str]:
        """The command the borrow refusal recommends, verbatim."""

        return subprocess.run(
            ["make", "-s", "setup", f"PYTHON={python or self.stub}", f"VENV={venv}"],
            cwd=self.linked, capture_output=True, text=True,
            env=child_make_env(**env))

    def interpreter(self, version: str) -> pathlib.Path:
        """A stand-in for another installed Python, such as the one brew replaced."""

        path = self.tmp / f"stub-python{version}"
        path.write_text(self.STUB.replace("version=3.15", f"version={version}", 1),
                        encoding="utf-8")
        path.chmod(0o755)
        return path

    def version_in(self, venv: pathlib.Path) -> str:
        from tests.clean_env import clean_environment

        done = subprocess.run([str(venv / "bin" / "python"), "-c", "import sys"],
                              capture_output=True, text=True, env=clean_environment())
        return done.stdout.strip()

    def dry_run(self, target: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["make", "-s", "-n", target], cwd=self.linked,
                              capture_output=True, text=True, env=child_make_env())

    def move_a_pin(self) -> None:
        (self.linked / "requirements-dev.txt").write_text(
            "# requirements-dev.txt\nmoved==2\n", encoding="utf-8")

    def test_a_successful_provision_publishes_the_record(self) -> None:
        done = self.setup()
        self.assertEqual(done.returncode, 0, done.stderr)
        for name in self.REQUIREMENTS:
            self.assertEqual((self.record / name).read_text(encoding="utf-8"),
                             (self.linked / name).read_text(encoding="utf-8"))
        # Nothing is left of the directory the record was published from.
        self.assertFalse((self.linked / ".venv" / ".sd-requirements.new").exists())

    def test_a_failed_provision_leaves_no_record(self) -> None:
        """A re-provision that dies mid-flight must not leave a stale claim.

        The record used to be written last and nothing removed it first, so
        the previous run's copy sat beside packages this run had already
        changed. A worktree comparing against it matched, passed the borrow
        check, and ran the versions the failed run had half-installed.
        """

        self.assertEqual(self.setup().returncode, 0)
        self.assertTrue(self.record.is_dir(), "the first run should record")
        self.move_a_pin()
        done = self.setup(STUB_UNINSTALL_EXIT="1")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertFalse(self.record.exists(),
                         "a record survived a provision that failed")

    def test_the_recovery_the_refusal_recommends_detaches_the_link(self) -> None:
        """The combined case: a refused symlinked worktree, then the remedy.

        `python -m venv` refuses a symlinked path, so the recommendation in
        the refusal -- `make setup VENV=.venv` -- used to fail in exactly the
        configuration that produced the refusal.
        """

        (self.linked / ".venv").symlink_to(self.main_venv)
        self.move_a_pin()
        refused = self.dry_run("docs-lint")
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn("refusing-to-borrow", refused.stderr)

        done = self.setup()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("detaching .venv", done.stderr)
        self.assertIn(str(self.main_venv), done.stderr)
        self.assertFalse((self.linked / ".venv").is_symlink(),
                         "the link should be gone")
        self.assertTrue((self.linked / ".venv" / "bin" / "python").exists())
        # The environment behind the link, and its record, are untouched --
        # the detach precedes the record removal for this reason.
        self.assertTrue((self.main_venv / "bin" / "python").exists())
        for name in self.REQUIREMENTS:
            self.assertTrue((self.main_venv / "sd-requirements" / name).is_file())

        after = self.dry_run("docs-lint")
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertIn('".venv/bin/python"', after.stdout)
        self.assertNotIn("records no provisioning", after.stderr)

    def test_a_killed_provision_is_refused_and_then_recoverable(self) -> None:
        """The marker's full life: written, honoured, and cleared by a retry.

        A marker that outlived its run would brick the worktree, since the
        thing it refuses is the thing that would clear it. It does not: the
        refusal sends the reader back to `make setup`, which rewrites the
        marker over the dead one and removes it when the run completes.
        """

        failed = self.setup(STUB_UNINSTALL_EXIT="1")
        self.assertNotEqual(failed.returncode, 0, failed.stdout)
        marker = self.linked / ".venv" / "sd-provisioning"
        self.assertTrue(marker.is_file(), "the marker should outlive the run")

        refused = self.dry_run("docs-lint")
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn("mid-provision", refused.stderr)

        done = self.setup()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(marker.exists(), "a completed run should clear it")
        self.assertTrue(self.record.is_dir())
        after = self.dry_run("docs-lint")
        self.assertEqual(after.returncode, 0, after.stderr)

    def test_no_mutation_happens_unless_the_marker_exists(self) -> None:
        """The block's contract, asked at the two commands that can be stepped over.

        A recipe line is one shell with `;` between commands and no `set -e`,
        so a command that fails is stepped over and the line's status is the
        last command's. That is not a hypothetical here: both halves below
        used to pass, quietly, with the mutation already done.

        Asked as the contract rather than as two exit codes, because what
        matters is not which command reported what -- it is that the record is
        removed only after a marker exists to say why it is missing. Codex's
        note on the first pass was that setup and the symlink refusal were
        each covered and their combination was not; this is the combination.
        """

        # The marker cannot be written: a directory sits where the file goes,
        # so the redirection fails. Without `|| exit 1` the `rm -rf` runs
        # anyway on a line that exits 0, and the record is gone with no marker
        # to say why -- the absence the borrow paths read as legacy.
        #
        # The next step is failed deliberately, and that is what makes this
        # discriminating rather than merely red. Left to run on, the old
        # recipe republished the record at the end and failed only on its own
        # `rm -f` of the directory in the marker's place: a nonzero exit, a
        # record present, and nothing said about the window in between. What
        # is under test is the state after the mutation, so the run has to
        # stop there.
        self.assertEqual(self.setup().returncode, 0)
        self.assertTrue(self.record.is_dir())
        (self.linked / ".venv" / "sd-provisioning").mkdir()
        blocked = self.setup(STUB_PIP_EXIT="1")
        self.assertNotEqual(blocked.returncode, 0,
                            "a failed marker write reported success")
        self.assertTrue(self.record.is_dir(),
                        "the record was removed without a marker to explain it")

        # The link cannot be removed: its parent is read-only. `mkdir -p` on a
        # symlink to an existing directory succeeds, so an unchecked `rm`
        # leaves the marker written *through* the link and the record removed
        # from the environment every other worktree borrows from. This half is
        # about the other checkout, not this one.
        held = self.linked / "held"
        held.mkdir()
        (held / "env").symlink_to(self.main_venv)
        held.chmod(0o555)
        self.addCleanup(held.chmod, 0o755)
        through = self.setup(venv="held/env")
        self.assertNotEqual(through.returncode, 0,
                            "a failed detach reported success")
        self.assertTrue((held / "env").is_symlink(),
                        "the link is still the thing that could not be removed")
        self.assertFalse((self.main_venv / "sd-provisioning").exists(),
                         "the marker was written through the link")
        for name in self.REQUIREMENTS:
            self.assertTrue((self.main_venv / "sd-requirements" / name).is_file(),
                            "the borrowed-from environment lost its record")

    def test_the_marker_precedes_the_record_removal(self) -> None:
        """Ordering, read off the state a failure leaves rather than the text.

        The window the marker closes opens at the first mutation. Failing the
        very next step is the narrowest way to ask whether the marker was
        already there when that step ran.
        """

        self.assertEqual(self.setup().returncode, 0)
        self.assertTrue(self.record.is_dir())
        failed = self.setup(STUB_PIP_EXIT="1")
        self.assertNotEqual(failed.returncode, 0, failed.stdout)
        self.assertFalse(self.record.exists(), "the record should be gone")
        self.assertTrue((self.linked / ".venv" / "sd-provisioning").is_file(),
                        "the marker should have been written before the removal")

    def test_setup_rebuilds_an_environment_another_python_made(self) -> None:
        """sd:5254: a newer PYTHON over an older environment installed into the older one.

        venv rewrites `pyvenv.cfg` and adds `python3.15`, but keeps the
        `bin/python` the old version linked, so pip ran under 3.14 and put
        every package in `lib/python3.14` of an environment that said 3.15.
        """

        venv = self.linked / ".venv"
        self.assertEqual(self.setup(python=self.interpreter("3.14")).returncode, 0)
        self.assertEqual(self.version_in(venv), "3.14")
        (venv / "lib" / "python3.14").mkdir(parents=True)

        done = self.setup()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.version_in(venv), "3.15",
                         "setup kept the old interpreter's bin/python")
        self.assertIn("3.14", done.stderr)
        self.assertFalse((venv / "lib" / "python3.14").exists(),
                         "the old version's packages were left behind")
        self.assertFalse((venv / "sd-provisioning").exists())
        self.assertTrue(self.record.is_dir())

    def test_setup_keeps_an_environment_of_the_same_python(self) -> None:
        venv = self.linked / ".venv"
        self.assertEqual(self.setup().returncode, 0)
        kept = venv / "lib" / "kept"
        kept.mkdir(parents=True)
        done = self.setup()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("recreating", done.stderr)
        self.assertTrue(kept.is_dir(), "a matching environment was rebuilt")

    def test_an_environment_whose_python_does_not_run_is_rebuilt(self) -> None:
        """A failed probe is an unknown version, never a match: brew removed the old one."""

        venv = self.linked / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("version = 3.14.0\n", encoding="utf-8")
        (venv / "bin" / "python").symlink_to(self.tmp / "gone" / "python3.14")
        done = self.setup()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.version_in(venv), "3.15")
        self.assertIn("recreating", done.stderr)

    def test_a_python_that_does_not_run_changes_nothing(self) -> None:
        """The version probe comes before the first mutation, so its failure leaves the record."""

        self.assertEqual(self.setup().returncode, 0)
        done = self.setup(python=self.tmp / "no-such-python")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertTrue(self.record.is_dir(), "a failed probe removed the record")
        self.assertFalse((self.linked / ".venv" / "sd-provisioning").exists())
        self.assertEqual(self.version_in(self.linked / ".venv"), "3.15")

    def test_a_rebuild_stopped_midway_is_refused_and_then_finished(self) -> None:
        """Stop the wipe between its two steps; the next run must find it and finish it.

        A directory the wipe cannot empty stops the first step. `bin/` goes
        last, so the old interpreter is still there to say a rebuild is owed.
        """

        venv = self.linked / ".venv"
        self.assertEqual(self.setup(python=self.interpreter("3.14")).returncode, 0)
        locked = venv / "lib" / "locked"
        locked.mkdir(parents=True)
        (locked / "package.py").write_text("", encoding="utf-8")
        locked.chmod(0o555)
        self.addCleanup(lambda: locked.exists() and locked.chmod(0o755))

        stopped = self.setup()
        self.assertNotEqual(stopped.returncode, 0, stopped.stdout)
        self.assertEqual(self.version_in(venv), "3.14")
        self.assertTrue((venv / "sd-provisioning").is_file())
        refused = self.dry_run("docs-lint")
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn("mid-provision", refused.stderr)

        locked.chmod(0o755)
        done = self.setup()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.version_in(venv), "3.15")
        self.assertFalse(locked.exists())
        self.assertFalse((venv / "sd-provisioning").exists())
        self.assertEqual(self.dry_run("docs-lint").returncode, 0)

    def other_venv(self) -> pathlib.Path:
        """Another checkout's older environment, holding data that must survive."""

        other = self.tmp / "other"
        self.assertEqual(self.setup(venv=str(other), python=self.interpreter("3.14")).returncode, 0)
        (other / "lib").mkdir(exist_ok=True)
        (other / "lib" / "data").write_text("theirs\n", encoding="utf-8")
        return other

    def assert_untouched(self, other: pathlib.Path) -> None:
        self.assertTrue((other / "lib" / "data").is_file(), "the wipe reached through the link")
        self.assertEqual(self.version_in(other), "3.14")
        self.assertTrue((other / "sd-requirements").is_dir(), "the other environment lost its record")
        self.assertFalse((other / "sd-provisioning").exists(), "a marker was written into the target")

    def test_a_trailing_slash_still_names_the_link(self) -> None:
        """`env/` is no link to `[ -L ]`, so the wipe used to run through it into the target."""

        other = self.other_venv()
        for typed in ("env/", "env//"):
            with self.subTest(venv=typed):
                shutil.rmtree(self.linked / "env", ignore_errors=True)
                (self.linked / "env").symlink_to(other)
                done = self.setup(venv=typed)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("detaching env", done.stderr)
                self.assert_untouched(other)
                self.assertFalse((self.linked / "env").is_symlink())
                self.assertEqual(self.version_in(self.linked / "env"), "3.15")

    def test_a_dot_leaf_is_refused(self) -> None:
        """sd:5254 round 2: `env/.` is no link to `[ -L ]` either, and `env/..` names the target's parent.

        Stripping slashes left both; the leaf must be a name, so neither is
        read as a path at all.
        """

        other = self.other_venv()
        (self.linked / "env").symlink_to(other)
        for typed in ("env/.", "env/./", "env/..", "."):
            with self.subTest(venv=typed):
                done = self.setup(venv=typed)
                self.assertNotEqual(done.returncode, 0, done.stdout)
                self.assertIn("name the environment's own directory", done.stderr)
                self.assert_untouched(other)
                self.assertFalse((self.tmp / "sd-provisioning").exists(), "a marker was written into the parent")
                self.assertTrue((self.linked / "env").is_symlink())

    def test_a_symlinked_parent_still_sees_the_leaf_link(self) -> None:
        """`lnk/env`: the parent resolves, and the leaf is still asked whether it is a link."""

        other = self.other_venv()
        holder = self.tmp / "holder"
        holder.mkdir()
        (holder / "env").symlink_to(other)
        (self.linked / "lnk").symlink_to(holder)
        done = self.setup(venv="lnk/env")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("detaching lnk/env", done.stderr)
        self.assert_untouched(other)
        self.assertFalse((holder / "env").is_symlink())
        self.assertEqual(self.version_in(holder / "env"), "3.15")

    def test_an_interior_dotdot_resolves_where_the_kernel_does(self) -> None:
        """`sub/../.venv` with `sub` a link: the guards and the later lines reach one directory."""

        deep = self.tmp / "deep"
        (deep / "dir").mkdir(parents=True)
        (self.linked / "sub").symlink_to(deep / "dir")
        self.assertEqual(self.setup(venv="sub/../.venv", python=self.interpreter("3.14")).returncode, 0)
        self.assertEqual(self.version_in(deep / ".venv"), "3.14")
        done = self.setup(venv="sub/../.venv")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("recreating sub/../.venv", done.stderr)
        self.assertEqual(self.version_in(deep / ".venv"), "3.15")
        self.assertTrue((deep / ".venv" / "sd-requirements").is_dir())
        self.assertFalse((self.linked / ".venv").exists(), "the lexical path was built instead")

    def test_a_python_behind_the_link_is_refused_before_the_detach(self) -> None:
        """The detach would cut `.venv/bin/...` off from the PYTHON it names; refuse first."""

        other = self.other_venv()
        shutil.copy(self.stub, other / "bin" / "sd-test-python3.15")
        (self.linked / ".venv").symlink_to(other)
        done = self.setup(python=pathlib.Path(".venv/bin/sd-test-python3.15"))
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("from outside it", done.stderr)
        self.assertTrue((self.linked / ".venv").is_symlink(), "the link was detached before the refusal")
        self.assert_untouched(other)

    def test_a_parent_that_does_not_exist_is_built(self) -> None:
        """Nothing there can be deleted, so the path is used as typed and `mkdir -p` makes it."""

        done = self.setup(venv="new/deep/.venv")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.version_in(self.linked / "new" / "deep" / ".venv"), "3.15")
        self.assertTrue((self.linked / "new" / "deep" / ".venv" / "sd-requirements").is_dir())

    def test_an_unresolvable_parent_is_refused(self) -> None:
        """A parent that exists and will not resolve is unknown, never a pass."""

        shut = self.linked / "shut"
        shut.mkdir()
        shut.chmod(0o600)
        self.addCleanup(shut.chmod, 0o755)
        done = self.setup(venv="shut/.venv")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("cannot resolve the directory that holds shut/.venv", done.stderr)

    def test_a_directory_that_is_not_a_virtualenv_is_refused(self) -> None:
        """A prefix such as /usr/local has a bin/python too; with no pyvenv.cfg setup touches nothing."""

        prefix = self.linked / "prefix"
        (prefix / "bin").mkdir(parents=True)
        shutil.copy(self.interpreter("3.14"), prefix / "bin" / "python")
        (prefix / "lib").mkdir()
        (prefix / "lib" / "data").write_text("theirs\n", encoding="utf-8")

        done = self.setup(venv="prefix")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("not a virtualenv", done.stderr)
        self.assertTrue((prefix / "lib" / "data").is_file(), "a non-virtualenv was emptied")
        self.assertEqual(self.version_in(prefix), "3.14")
        self.assertFalse((prefix / "sd-provisioning").exists(), "the refusal came after a mutation")

    def test_a_python_inside_the_environment_is_refused(self) -> None:
        """A mixed environment holds a 3.15 beside its 3.14; the wipe would delete the PYTHON it runs."""

        venv = self.linked / ".venv"
        inside = venv / "bin" / "sd-test-python3.15"
        for python, env in ((inside, {}), (pathlib.Path(inside.name),
                                           {"PATH": f"{venv / 'bin'}{os.pathsep}{os.environ['PATH']}"})):
            with self.subTest(python=str(python)):
                shutil.rmtree(venv, ignore_errors=True)
                self.assertEqual(self.setup(python=self.interpreter("3.14")).returncode, 0)
                shutil.copy(self.stub, inside)
                done = self.setup(python=python, **env)
                self.assertNotEqual(done.returncode, 0, done.stdout)
                self.assertIn("from outside it", done.stderr)
                self.assertTrue(inside.is_file(), "setup deleted the PYTHON it was running")
                self.assertTrue(self.record.is_dir(), "the refusal came after a mutation")
                self.assertFalse((venv / "sd-provisioning").exists())

    def test_the_real_interpreter_keeps_an_existing_python(self) -> None:
        """The premise of sd:5254, asked of the real interpreter.

        Two installed versions are not portable, so this asks the narrower
        fact the fault rests on: venv leaves a `bin/python` it finds, a file
        or a dangling link, in place.
        """

        for kind in ("file", "dangling link"):
            with self.subTest(kind=kind):
                env = self.tmp / f"real-{kind.replace(' ', '-')}"
                python = env / "bin" / "python"
                python.parent.mkdir(parents=True)
                if kind == "file":
                    python.write_text("#!/bin/sh\necho 3.14\n", encoding="utf-8")
                else:
                    python.symlink_to(self.tmp / "gone" / "python3.14")
                done = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(env)],
                                      capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertTrue((env / "pyvenv.cfg").is_file(), "the stand-in writes it on this premise")
                if kind == "file":
                    self.assertFalse(python.is_symlink())
                    self.assertEqual(python.read_text(encoding="utf-8"), "#!/bin/sh\necho 3.14\n")
                else:
                    self.assertEqual(os.readlink(python), str(self.tmp / "gone" / "python3.14"))

    def test_the_real_interpreter_refuses_a_symlinked_target(self) -> None:
        """The premise the stand-in mirrors, asked of the real interpreter.

        Nothing is built: venv refuses before it creates anything, which is
        why this costs a process and not an environment.
        """

        target = self.tmp / "link-to-a-directory"
        (self.tmp / "a-directory").mkdir()
        target.symlink_to(self.tmp / "a-directory")
        done = subprocess.run([sys.executable, "-m", "venv", str(target)],
                              capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("Unable to create directory", done.stderr + done.stdout)


class TheCheckoutLibraryAnswersFirst(unittest.TestCase):
    """sd:3278. `import_sd_db` reads `<checkout>/lib` before any installed copy.

    Each `sd_db` below names where it came from. The checkout's `bin/` and
    `lib/` then come from one commit, whatever the interpreter has installed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name).resolve() / "pack"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "bin" / "sd_lib.py").write_text(
            (REPO_ROOT / "bin" / "sd_lib.py").read_text(encoding="utf-8"), encoding="utf-8")
        self.copy(self.root / "lib", "lib")
        self.copy(self.root / ".venv/lib/python3.13/site-packages", "venv")
        self.copy(self.root.parent / "elsewhere", "pythonpath")

    @staticmethod
    def copy(where: pathlib.Path, name: str, body: str = "") -> None:
        (where / "sd_db").mkdir(parents=True, exist_ok=True)
        (where / "sd_db" / "__init__.py").write_text(f"{body}WHERE = {name!r}\n", encoding="utf-8")

    def answer(self, **extra: str) -> str:
        from tests.clean_env import clean_environment
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import sd_lib; "
                "got = sd_lib.import_sd_db(); print(got.module.WHERE if got.module else got.problem)")
        done = subprocess.run([sys.executable, "-S", "-c", code, str(self.root / "bin")],
                              capture_output=True, text=True, check=False,
                              env=clean_environment(**extra))
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_the_checkout_lib_wins_over_the_venv_copy(self) -> None:
        self.assertEqual(self.answer(), "lib")

    def test_the_checkout_lib_wins_over_an_importable_pythonpath_copy(self) -> None:
        self.assertEqual(self.answer(PYTHONPATH=str(self.root.parent / "elsewhere")), "lib")

    def test_a_lib_that_will_not_import_is_reported_and_no_venv_copy_answers(self) -> None:
        """sd:3280. No copy is provisioned any more, so a stale one left in `.venv` is never run."""
        self.copy(self.root / "lib", "lib", body="raise ImportError('broken on purpose')\n")
        answer = self.answer()
        self.assertIn("broken on purpose", answer)
        self.assertNotEqual(answer, "venv")


#: A fake `sd_db` that reads its database's version from a file: `FAKE_DB` holds the number,
#: or `hub:<digest>` for a hub that refuses any build but that one.
FAKE_LIBRARY = """\
import os, pathlib
SCHEMA_VERSION = {built}
WHERE = {where!r}
def default_path():
    return pathlib.Path(os.environ["FAKE_DB"])
class Connection:
    def __init__(self, found):
        self.found = found
    def close(self):
        pass
class remote:
    @staticmethod
    def tree_digest(package):
        return (pathlib.Path(package) / "BUILD").read_text()
def connect(path, write=True):
    text = pathlib.Path(path).read_text()
    if text.startswith("hub:"):
        error = Exception("the hub runs another build")
        error.hub_build = text[4:]
        raise error
    found = int(text)
    if found > SCHEMA_VERSION:
        error = Exception(f"the database is at schema version {{found}} and this library is built for {built}")
        error.found = found
        raise error
    return Connection(found)
def schema_version(connection):
    return connection.found
"""


class TheCheckoutLibraryIsTheOnlyCopy(unittest.TestCase):
    """sd:3280. `lib/` answers whatever schema or build the database or hub wants.

    Step 1 let a provisioned `.venv` copy answer when it matched the database
    and `lib/` did not (sd:3278 review round 1). Step 3 provisions no copy, so a
    copy still in `.venv` is stale by definition and never runs; `lib/`'s own
    open names both versions, and the hub's refusal names `repo-sync.sh follow`.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name).resolve() / "pack"
        (self.root / "bin").mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "bin" / "sd_lib.py", self.root / "bin" / "sd_lib.py")
        self.database = self.root.parent / "sd.db"

    def library(self, where: str, built: int) -> None:
        folder = self.root / ".venv/lib/python3.13/site-packages" if where == "venv" else self.root / where
        (folder / "sd_db").mkdir(parents=True, exist_ok=True)
        (folder / "sd_db" / "__init__.py").write_text(FAKE_LIBRARY.format(built=built, where=where),
                                                    encoding="utf-8")
        (folder / "sd_db" / "schema.py").write_text(f"SCHEMA_VERSION = {built}\n", encoding="utf-8")
        (folder / "sd_db" / "BUILD").write_text(f"build-{where}", encoding="utf-8")

    def answer(self, lib: int, venv: int, database: str) -> str:
        from tests.clean_env import clean_environment
        self.library("lib", lib)
        self.library("venv", venv)
        self.database.write_text(database, encoding="utf-8")
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import sd_lib; "
                "got = sd_lib.import_sd_db().module; print(got.WHERE, "
                "any('site-packages' in entry for entry in sys.path))")
        done = subprocess.run([sys.executable, "-S", "-c", code, str(self.root / "bin")],
                              capture_output=True, text=True, check=False,
                              env=clean_environment(FAKE_DB=str(self.database)))
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_a_checkout_ahead_of_the_database_runs_lib_not_a_matching_venv_copy(self) -> None:
        self.assertEqual(self.answer(lib=28, venv=27, database="27"), "lib False")

    def test_a_worktree_behind_a_migrated_database_runs_lib_not_a_matching_venv_copy(self) -> None:
        self.assertEqual(self.answer(lib=27, venv=28, database="28"), "lib False")

    def test_a_satellite_whose_hub_wants_the_venv_copy_s_build_still_runs_lib(self) -> None:
        self.assertEqual(self.answer(lib=27, venv=27, database="hub:build-venv"), "lib False")


class TheCopyHashesLikeItsSource(unittest.TestCase):
    """sd:3278 failure table, step 1 follow: the build digest names bytes, not folders.

    A satellite's self-install exports system's `local-sd-db` while the hub
    runs the pack's `lib/`. `tree_digest` hashes relative paths and bytes, so
    the same package under either folder digests the same and no build refuses.
    """

    def test_lib_sd_db_digests_the_same_under_the_system_folder_name(self) -> None:
        from tests.clean_env import clean_environment
        with tempfile.TemporaryDirectory() as tmp:
            copy = pathlib.Path(tmp) / "local-sd-db" / "sd_db"
            shutil.copytree(REPO_ROOT / "lib" / "sd_db", copy,
                            ignore=shutil.ignore_patterns("__pycache__"))
            code = ("import sys; sys.path.insert(0, sys.argv[1]); from sd_db.remote import tree_digest; "
                    "print(tree_digest(sys.argv[2]), tree_digest(sys.argv[3]))")
            done = subprocess.run([sys.executable, "-S", "-c", code, str(REPO_ROOT / "lib"),
                                   str(REPO_ROOT / "lib" / "sd_db"), str(copy)],
                                  capture_output=True, text=True, check=False, env=clean_environment())
        self.assertEqual(done.returncode, 0, done.stderr)
        ours, theirs = done.stdout.split()
        self.assertEqual(ours, theirs)


class RunGroupTimeoutTests(unittest.TestCase):
    """sd:3414: a group past its deadline is ended, and what it wrote first rides on `GroupTimeout`."""

    def test_the_timeout_carries_what_the_group_wrote(self) -> None:
        with self.assertRaises(sd_lib.GroupTimeout) as raised:
            sd_lib.run_group(["sh", "-c", "echo 'shard x: start'; echo 'to stderr' >&2; sleep 30"],
                             cwd=pathlib.Path("."), env={"PATH": "/usr/bin:/bin"}, timeout=2)
        self.assertEqual(str(raised.exception), "sh: timed out after 2s")
        self.assertEqual(raised.exception.stdout, "shard x: start\n")
        self.assertEqual(raised.exception.stderr, "to stderr\n")

    def test_a_process_that_left_the_group_cannot_hold_the_timeout_open(self) -> None:
        """A child in a session of its own keeps the pipe open past the group's end; the drain is bounded."""
        started = time.monotonic()
        with self.assertRaises(sd_lib.GroupTimeout) as raised:
            sd_lib.run_group([sys.executable, "-c", (
                "import subprocess, sys, time\n"
                "subprocess.Popen(['sleep', '20'], start_new_session=True)\n"
                "print('before the hang', flush=True)\ntime.sleep(30)\n")],
                cwd=pathlib.Path("."), env={"PATH": "/usr/bin:/bin"}, timeout=2)
        self.assertLess(time.monotonic() - started, 2 + 3 * sd_lib.GROUP_CLEANUP_SECONDS)
        self.assertEqual(raised.exception.stdout, "before the hang\n")


#: A leader that ignores SIGTERM and starts `sleep 60` in a session of its own; it writes that pid to argv[1].
SESSION_LEADER = (
    "import pathlib, signal, subprocess, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "below = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
    "pathlib.Path(sys.argv[1]).write_text(str(below.pid))\n"
    "time.sleep(60)\n")


#: A leader that starts `sleep 60` in a session of its own, writes its pid to argv[1], and dies on SIGTERM at once.
TERM_QUITTER = (
    "import pathlib, subprocess, sys, time\n"
    "below = subprocess.Popen(['sleep', '60'], start_new_session=True, stdout=subprocess.DEVNULL,\n"
    "                         stderr=subprocess.DEVNULL)\n"
    "pathlib.Path(sys.argv[1]).write_text(str(below.pid))\n"
    "time.sleep(60)\n")


#: A leader that starts a worker in its own group, writes nothing, and dies on SIGTERM at once (round 4).
WORKER_LEADER = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
    "time.sleep(60)\n")
#: A worker that writes its pid to argv[1], and on SIGTERM takes 1 s, writes `<argv[1]>.done` and exits.
SLOW_WORKER = (
    "import os, pathlib, signal, sys, time\n"
    "def done(*_):\n"
    "    time.sleep(1)\n"
    "    pathlib.Path(sys.argv[1] + '.done').write_text('done')\n"
    "    sys.exit(0)\n"
    "signal.signal(signal.SIGTERM, done)\n"
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
    "time.sleep(60)\n")
#: A worker that on SIGTERM starts `sleep 60` in a session of its own, writes its pid to `<argv[1]>.below`
#: and runs on.
SPAWNING_WORKER = (
    "import os, pathlib, signal, subprocess, sys, time\n"
    "def spawn(*_):\n"
    "    below = subprocess.Popen(['sleep', '60'], start_new_session=True, stdout=subprocess.DEVNULL,\n"
    "                             stderr=subprocess.DEVNULL)\n"
    "    pathlib.Path(sys.argv[1] + '.below').write_text(str(below.pid))\n"
    "signal.signal(signal.SIGTERM, spawn)\n"
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
    "time.sleep(60)\n")


def wait_for_pid(path: pathlib.Path) -> int:
    for _ in range(400):  # bounded: a leader that never writes fails on the read
        if path.exists() and path.read_text():
            break
        time.sleep(0.05)
    return int(path.read_text())


def gone(pid: int, seconds: float = 3) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def end_session(pid: int) -> None:
    with contextlib.suppress(OSError):
        os.killpg(pid, signal.SIGKILL)


class KillReachTests(unittest.TestCase):
    """pack sd:9392: a SIGKILL of a group reaches each session below it, which a SIGTERM's unwind would have ended."""

    def start(self, scratch: str) -> tuple[subprocess.Popen, int]:
        mark = pathlib.Path(scratch) / "below.pid"
        leader = sd_lib.spawn_group([sys.executable, "-c", SESSION_LEADER, str(mark)])
        sd_lib.release_term()
        below = wait_for_pid(mark)
        self.addCleanup(end_session, below)
        self.addCleanup(end_session, leader.pid)
        return leader, below

    def test_end_group_kills_a_session_its_leader_left(self) -> None:
        """Failure table row 1: the KILL reached the leader's group only; the session below ran on with ppid 1."""
        with tempfile.TemporaryDirectory() as scratch:
            leader, below = self.start(scratch)
            sd_lib.end_group(leader)
            self.assertTrue(gone(below), "the session below the leader outlived end_group's SIGKILL")

    def test_thread_groups_kill_ends_the_sessions_below(self) -> None:
        """Failure table row 3: `ThreadGroups.end_groups` SIGKILLed the registered group only."""
        with tempfile.TemporaryDirectory() as scratch:
            leader, below = self.start(scratch)
            groups = sd_lib.ThreadGroups()
            groups.group_hook()(leader.pid)
            with unittest.mock.patch.object(sd_lib, "GROUP_CLEANUP_SECONDS", 0.5):
                groups.end_groups()
            leader.wait(5)
            self.assertTrue(gone(below), "the session below the thread's group outlived end_groups's SIGKILL")

    def test_a_reused_group_number_is_not_signalled(self) -> None:
        """Advisory 4474: a group from the first read whose leader now has another start is another process."""
        with tempfile.TemporaryDirectory() as scratch:
            leader, below = self.start(scratch)
            earlier = {below: (1, below, "Thu Jan  1 00:00:00 1970")}  # the same number, another start
            with unittest.mock.patch.object(os, "killpg") as killpg:
                sd_lib.kill_below(leader.pid, earlier, sd_lib.process_table())
            self.assertNotIn(unittest.mock.call(below, signal.SIGKILL), killpg.call_args_list)

    def test_an_unread_table_still_kills_the_leaders_group(self) -> None:
        """Reads row: a `ps` that fails is unknown; the leader's own group is still killed, and stderr says so."""
        with tempfile.TemporaryDirectory() as scratch:
            leader, _ = self.start(scratch)
            with unittest.mock.patch.object(sd_lib, "process_table", return_value=None), \
                    unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as said:
                sd_lib.end_group(leader)
            self.assertIsNotNone(leader.returncode)
            self.assertIn("cannot read the process table", said.getvalue())

    def test_a_diagnostic_that_cannot_be_written_stops_no_kill(self) -> None:
        """Class pass, rule 2: under SIGHUP the terminal is gone, so the stderr line raised EIO before the
        leader's SIGKILL, and end_group raised with the leader still running."""
        class Revoked(io.StringIO):
            def write(self, text: str) -> int:
                raise OSError(5, "Input/output error")
        with tempfile.TemporaryDirectory() as scratch:
            leader, _ = self.start(scratch)
            with unittest.mock.patch.object(sd_lib, "process_table", return_value=None), \
                    unittest.mock.patch("sys.stderr", new=Revoked()):
                sd_lib.end_group(leader)
            self.assertIsNotNone(leader.returncode)

    def test_end_groups_kills_what_a_term_quitter_left(self) -> None:
        """Class pass, rule 1: the leader ended on TERM, so its group left the set; `end_groups` killed
        only groups still registered, and the session it left ran on with ppid 1."""
        with tempfile.TemporaryDirectory() as scratch:
            mark = pathlib.Path(scratch) / "below.pid"
            groups = sd_lib.ThreadGroups()
            thread = threading.Thread(target=lambda: sd_lib.run_group(
                [sys.executable, "-c", TERM_QUITTER, str(mark)], cwd=pathlib.Path("."), env=dict(os.environ),
                timeout=60, started=groups.group_hook()), daemon=True)
            thread.start()
            below = wait_for_pid(mark)
            self.addCleanup(end_session, below)
            time.sleep(0.3)  # the leader has installed its handler
            groups.end_groups()
            thread.join(30)
            self.assertTrue(gone(below), "the session below a leader that ended on TERM outlived end_groups")

    def test_end_groups_reads_the_table_once_for_every_group(self) -> None:
        """Class pass, rule 3: `end_groups` read the table once per group, up to 3 s each, before any TERM.

        One shared read now comes first, then every TERM: a read after the TERM misses a leader that dies on
        it at once (the test above), so rule 1 needs the read first; the TERMs wait on one bounded read only."""
        events: list[str] = []
        groups = sd_lib.ThreadGroups()
        for pid in (999991, 999992, 999993):
            groups.group_hook()(pid)
        with unittest.mock.patch.object(sd_lib, "process_table", side_effect=lambda: events.append("read") or {}), \
                unittest.mock.patch.object(sd_lib, "signal_group",
                                           side_effect=lambda pid, number: events.append(f"signal {number}")), \
                unittest.mock.patch.object(sd_lib, "GROUP_CLEANUP_SECONDS", 0.1):
            groups.end_groups()
        self.assertEqual(events[:4], ["read"] + [f"signal {int(signal.SIGTERM)}"] * 3)
        self.assertEqual(events.count("read"), 2, events)  # one before the TERMs, one before the KILLs


    def test_a_group_registered_after_the_end_began_has_its_session_killed(self) -> None:
        """Round 3, rule B: a group that registered after `end_groups` was TERMed with no read before it;
        its leader quit on the TERM, and the session it left ran on with ppid 1."""
        with tempfile.TemporaryDirectory() as scratch:
            mark = pathlib.Path(scratch) / "below.pid"
            groups = sd_lib.ThreadGroups()
            groups.end_groups()  # the end began; no group was live
            release = sd_lib.release_term

            def late() -> None:  # the group registers once its leader has started the session below it
                release()
                self.addCleanup(end_session, wait_for_pid(mark))
                time.sleep(0.3)
            with unittest.mock.patch.object(sd_lib, "release_term", late):
                sd_lib.run_group([sys.executable, "-c", TERM_QUITTER, str(mark)], cwd=pathlib.Path("."),
                                 env=dict(os.environ), timeout=60, started=groups.group_hook())
            self.assertTrue(gone(int(mark.read_text())), "the session below a late group outlived its end")


    def worker_group(self, scratch: str, worker: str) -> tuple[list[str], pathlib.Path]:
        """argv for `WORKER_LEADER` with `worker` in its group, and the worker's pid file."""
        script = pathlib.Path(scratch) / "worker.py"
        script.write_text(worker, encoding="utf-8")
        mark = pathlib.Path(scratch) / "worker.pid"
        return [sys.executable, "-c", WORKER_LEADER, str(script), str(mark)], mark

    def test_end_group_waits_for_the_workers_not_the_leader(self) -> None:
        """Round 4: the TERM grace ended when the leader alone exited, so a worker still in its TERM
        handler was KILLed at once."""
        with tempfile.TemporaryDirectory() as scratch:
            argv, mark = self.worker_group(scratch, SLOW_WORKER)
            leader = sd_lib.spawn_group(argv)
            sd_lib.release_term()
            self.addCleanup(end_session, leader.pid)
            wait_for_pid(mark)
            time.sleep(0.3)  # the worker has installed its handler
            sd_lib.end_group(leader)
            self.assertTrue(pathlib.Path(f"{mark}.done").exists(), "the worker was KILLed inside its TERM grace")

    def test_a_late_group_waits_for_its_workers_not_its_leader(self) -> None:
        """Round 4, blocker 1: `end_late_group` SIGKILLed as soon as the leader exited."""
        with tempfile.TemporaryDirectory() as scratch:
            argv, mark = self.worker_group(scratch, SLOW_WORKER)
            groups = sd_lib.ThreadGroups()
            groups.end_groups()  # the end began; no group was live
            release = sd_lib.release_term

            def late() -> None:
                release()
                wait_for_pid(mark)
                time.sleep(0.3)
            with unittest.mock.patch.object(sd_lib, "release_term", late):
                sd_lib.run_group(argv, cwd=pathlib.Path("."), env=dict(os.environ), timeout=60,
                                 started=groups.group_hook())
            self.assertTrue(pathlib.Path(f"{mark}.done").exists(), "the worker was KILLed inside its TERM grace")

    def test_a_leaderless_groups_worker_session_is_killed(self) -> None:
        """Round 4, blocker 2: the leader ended on TERM, so the KILL's walk skipped its group; a worker
        still in it started a session that ran on with ppid 1."""
        with tempfile.TemporaryDirectory() as scratch:
            argv, mark = self.worker_group(scratch, SPAWNING_WORKER)
            leader = sd_lib.spawn_group(argv)
            sd_lib.release_term()
            self.addCleanup(end_session, leader.pid)
            self.addCleanup(end_session, wait_for_pid(mark))
            time.sleep(0.3)  # the worker has installed its handler
            with unittest.mock.patch.object(sd_lib, "GROUP_CLEANUP_SECONDS", 1):
                sd_lib.end_group(leader)
            below = wait_for_pid(pathlib.Path(f"{mark}.below"))
            self.addCleanup(end_session, below)
            self.assertTrue(gone(below), "the session a leaderless group's worker started outlived the KILL")

    def deaf_leader(self) -> subprocess.Popen:
        """A group leader that ignores SIGTERM, so only a SIGKILL ends it."""
        leader = sd_lib.spawn_group([sys.executable, "-c", "import signal, time\n"
                                     "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"])
        sd_lib.release_term()
        self.addCleanup(end_session, leader.pid)
        time.sleep(0.5)  # the leader has installed its handler
        return leader

    @staticmethod
    def reads(fail_at: int) -> Any:
        """`process_table`, whose read number `fail_at` (from 1) raises KeyboardInterrupt."""
        real, count = sd_lib.process_table, [0]

        def read() -> Any:
            count[0] += 1
            if count[0] == fail_at:
                raise KeyboardInterrupt
            return real()
        return read

    def assert_killed(self, leader: subprocess.Popen) -> None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            leader.wait(5)
        self.assertEqual(leader.returncode, -signal.SIGKILL, "the TERM went out and no SIGKILL followed")

    def test_an_interrupted_read_in_end_group_still_kills(self) -> None:
        """Round 5, R2: a KeyboardInterrupt in either read left a TERMed group with no SIGKILL."""
        for fail_at in (1, 2):
            with self.subTest(read=fail_at):
                leader = self.deaf_leader()
                with unittest.mock.patch.object(sd_lib, "process_table", side_effect=self.reads(fail_at)), \
                        unittest.mock.patch.object(sd_lib, "GROUP_CLEANUP_SECONDS", 0.5), \
                        self.assertRaises(KeyboardInterrupt):
                    sd_lib.end_group(leader)
                self.assert_killed(leader)

    def test_an_interrupted_grace_in_end_group_still_kills(self) -> None:
        """Round 5, R2: a KeyboardInterrupt in the grace left the workers to finish with no SIGKILL."""
        with tempfile.TemporaryDirectory() as scratch:
            argv, mark = self.worker_group(scratch, SLOW_WORKER)
            leader = sd_lib.spawn_group(argv)
            sd_lib.release_term()
            self.addCleanup(end_session, leader.pid)
            worker = wait_for_pid(mark)
            time.sleep(0.3)  # the worker has installed its handler
            with unittest.mock.patch.object(sd_lib, "wait_empty", side_effect=KeyboardInterrupt), \
                    self.assertRaises(KeyboardInterrupt):
                sd_lib.end_group(leader)
            self.assertTrue(gone(worker), "the worker outlived an interrupted grace")
            time.sleep(1.2)
            self.assertFalse(pathlib.Path(f"{mark}.done").exists(), "the worker finished: no SIGKILL reached it")

    def test_an_interrupted_read_in_end_groups_still_kills(self) -> None:
        """Round 5, R2, `ThreadGroups.end_groups`: an interrupted read left the TERMed groups unKILLed."""
        for fail_at in (1, 2):
            with self.subTest(read=fail_at):
                leader = self.deaf_leader()
                groups = sd_lib.ThreadGroups()
                groups.group_hook()(leader.pid)
                with unittest.mock.patch.object(sd_lib, "process_table", side_effect=self.reads(fail_at)), \
                        unittest.mock.patch.object(sd_lib, "GROUP_CLEANUP_SECONDS", 0.5), \
                        self.assertRaises(KeyboardInterrupt):
                    groups.end_groups()
                self.assert_killed(leader)

    def test_an_interrupted_read_in_end_late_group_still_kills(self) -> None:
        """Round 5, R2, `end_late_group`: an interrupted read left the late group unKILLed."""
        for fail_at in (1, 2):
            with self.subTest(read=fail_at):
                leader = self.deaf_leader()
                with unittest.mock.patch.object(sd_lib, "process_table", side_effect=self.reads(fail_at)), \
                        unittest.mock.patch.object(sd_lib, "GROUP_CLEANUP_SECONDS", 0.5), \
                        self.assertRaises(KeyboardInterrupt):
                    sd_lib.end_late_group(leader.pid)
                self.assert_killed(leader)

    def test_a_reused_number_without_a_witness_is_not_signalled(self) -> None:
        """Round 5, R1: the KILL walked from a group number whose every member read before the TERM was
        gone, so a stranger now holding the number had the groups below it signalled."""
        groups = sd_lib.ThreadGroups()
        hook = groups.group_hook()
        hook(4242)
        reads = iter([{4242: (1, 4242, "Thu Jan  1 00:00:00 1970"), 4300: (4242, 4300, "Thu Jan  1 00:00:01 1970")},
                      {5000: (1, 4242, "Fri Jan  2 00:00:00 1970"), 5001: (5000, 5100, "Fri Jan  2 00:00:01 1970"),
                       4300: (1, 4300, "Fri Jan  2 00:00:02 1970")}])
        sent: list[tuple[int, int]] = []

        def record(pid: int, number: int) -> None:
            sent.append((pid, number))
            if number == signal.SIGTERM:
                hook(None)  # its run_group reaped it and left the set
        with unittest.mock.patch.object(sd_lib, "process_table", side_effect=lambda: next(reads)), \
                unittest.mock.patch.object(sd_lib, "signal_group", side_effect=record), \
                unittest.mock.patch.object(sd_lib, "GROUP_CLEANUP_SECONDS", 0.1):
            groups.end_groups()
        self.assertEqual([pid for pid, number in sent if number == signal.SIGKILL], [], sent)

    def test_a_reused_child_pid_roots_no_walk(self) -> None:
        """Round 5, R1, `end_child`: a reaped child's pid, now another process, rooted the walk, and an
        earlier group below it whose members were all gone was signalled."""
        earlier = {100: (1, 100, "Thu Jan  1 00:00:00 1970"), 200: (100, 200, "Thu Jan  1 00:00:01 1970")}
        now = {100: (1, 100, "Fri Jan  2 00:00:00 1970"), 300: (100, 300, "Fri Jan  2 00:00:01 1970"),
               201: (1, 200, "Fri Jan  2 00:00:02 1970")}
        with unittest.mock.patch.object(os, "killpg") as killpg:
            held = sd_lib.kill_below(100, earlier, now, process=True)
        self.assertFalse(held)
        self.assertEqual(killpg.call_args_list, [])

    def test_a_reaped_leaders_reused_number_gets_no_sigkill(self) -> None:
        """Round 5, R1, `end_group`: the SIGKILL went to the reaped leader's bare number, which another
        group held by then."""
        process = unittest.mock.Mock(pid=4242, returncode=0, stdin=None, stdout=None, stderr=None)
        reads = iter([{4242: (1, 4242, "Thu Jan  1 00:00:00 1970")}, {5000: (1, 4242, "Fri Jan  2 00:00:00 1970")}])
        with unittest.mock.patch.object(sd_lib, "process_table", side_effect=lambda: next(reads)), \
                unittest.mock.patch.object(sd_lib, "wait_empty"), \
                unittest.mock.patch.object(os, "killpg") as killpg:
            sd_lib.end_group(process)
        self.assertNotIn(unittest.mock.call(4242, signal.SIGKILL), killpg.call_args_list)

    def test_a_term_deaf_replacement_in_a_timed_out_group_is_killed(self) -> None:
        """pack sd:9600, sd_lib.py:4212: the grace reaped the leader, the read members all left, and a
        TERM-deaf worker one of them forked on the TERM held the group with no witness, so no SIGKILL."""
        replacing = (
            "import os, pathlib, signal, sys, time\n"
            "mark = pathlib.Path(sys.argv[1])\n"
            "if os.fork() == 0:\n"
            "    def replace(*_):\n"
            "        if os.fork() == 0:\n"
            "            signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "            pathlib.Path(f'{mark}.deaf').write_text(str(os.getpid()))\n"
            "            time.sleep(30)\n"
            "        os._exit(0)\n"
            "    signal.signal(signal.SIGTERM, replace)\n"
            "    mark.write_text(str(os.getpid()))\n"
            "    while True:\n"
            "        time.sleep(1)\n"
            "time.sleep(60)\n")
        with tempfile.TemporaryDirectory() as scratch:
            mark = pathlib.Path(scratch) / "worker.pid"
            leader = sd_lib.spawn_group([sys.executable, "-c", replacing, str(mark)])
            sd_lib.release_term()
            self.addCleanup(end_session, leader.pid)
            wait_for_pid(mark)
            with unittest.mock.patch.object(sd_lib, "GROUP_CLEANUP_SECONDS", 0.5):
                sd_lib.end_group(leader)
            deaf = wait_for_pid(pathlib.Path(f"{mark}.deaf"))
            self.addCleanup(end_session, deaf)
            self.assertTrue(gone(deaf), "the TERM-deaf replacement outlived end_group")

    def test_an_unread_table_before_the_term_kills_no_reaped_leaders_number(self) -> None:
        """pack sd:9600 review: with no read before the TERM and the leader reaped, the fallback SIGKILL
        went to the bare number, which another group may hold by then."""
        process = unittest.mock.Mock(pid=4242, returncode=0, stdin=None, stdout=None, stderr=None)
        with unittest.mock.patch.object(sd_lib, "process_table", return_value=None), \
                unittest.mock.patch.object(sd_lib, "group_alive", return_value=True), \
                unittest.mock.patch.object(sd_lib, "wait_empty"), \
                unittest.mock.patch("sys.stderr", new_callable=io.StringIO), \
                unittest.mock.patch.object(os, "killpg") as killpg:
            sd_lib.end_group(process)
        self.assertNotIn(unittest.mock.call(4242, signal.SIGKILL), killpg.call_args_list)

    def test_an_unread_table_still_kills_a_timed_out_leaders_term_deaf_worker(self) -> None:
        """pack sd:9600, sd_lib.py:3821: a `ps` that failed before the TERM, with the leader reaped in the
        grace, skipped the group SIGKILL; the leader now stays unreaped, so its number is its own."""
        with tempfile.TemporaryDirectory() as scratch:
            mark = pathlib.Path(scratch) / "worker.pid"
            worker = ("import os, pathlib, signal, sys, time\n"
                      "if os.fork() == 0:\n"
                      "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                      "    pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                      "    time.sleep(30)\n"
                      "    os._exit(0)\n"
                      "time.sleep(60)\n")
            leader = sd_lib.spawn_group([sys.executable, "-c", worker, str(mark)])
            sd_lib.release_term()
            self.addCleanup(end_session, leader.pid)
            deaf = wait_for_pid(mark)
            with unittest.mock.patch.object(sd_lib, "process_table", return_value=None), \
                    unittest.mock.patch.object(sd_lib, "GROUP_CLEANUP_SECONDS", 0.5), \
                    unittest.mock.patch("sys.stderr", new_callable=io.StringIO):
                sd_lib.end_group(leader)
            self.assertTrue(gone(deaf), "the TERM-deaf worker outlived end_group with no table read")

    def test_a_session_a_surviving_detached_gate_started_is_killed(self) -> None:
        """pack sd:9600, sd_lib.py:4214: the second walk ran from the root alone; a detached gate whose
        parent had exited started a session after the first read, and only the gate got the SIGKILL."""
        earlier = {100: (1, 100, "Thu Jan  1 00:00:00 1970"), 200: (100, 200, "Thu Jan  1 00:00:01 1970")}
        now = {200: (1, 200, "Thu Jan  1 00:00:01 1970"), 300: (200, 300, "Thu Jan  1 00:00:02 1970")}
        with unittest.mock.patch.object(os, "killpg") as killpg:
            sd_lib.kill_below(100, earlier, now)
        self.assertEqual(sorted(call.args[0] for call in killpg.call_args_list), [200, 300])

    def test_a_sigterm_handler_reentering_the_groups_lock_does_not_deadlock(self) -> None:
        """pack sd:9600 class pass: `term_ends_group` runs `end_groups` in the handler, on the main
        thread; it took `_lock` again where the interrupted code held it, and waited forever. Every
        `with self._lock` in `ThreadGroups` runs on that thread: the hook, `reopen_groups`,
        `holding_threads` and `end_groups`' snapshot, grace and KILL."""
        groups = sd_lib.ThreadGroups()

        def reenter() -> None:
            with groups._lock:  # where any of those blocks holds it
                groups.end_groups()
        thread = threading.Thread(target=reenter, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "end_groups waited on the lock its own thread held")

    def test_a_sigterm_in_end_groups_kill_loop_finishes_the_kills(self) -> None:
        """pack sd:9600 class pass, sd_lib.py:4016: a SIGTERM while the KILL loop held `_lock` ran
        `end_groups` again on the same thread, which waited on that lock."""
        groups = sd_lib.ThreadGroups()
        hook = groups.group_hook()
        hook(4242)
        sent: list[tuple[int, int]] = []

        def record(pid: int, number: int) -> None:
            sent.append((pid, number))
            if number == signal.SIGKILL and len([n for _, n in sent if n == signal.SIGKILL]) == 1:
                groups.end_groups()  # the handler's `first`, on this thread
        table = {4242: (1, 4242, "Thu Jan  1 00:00:00 1970")}

        def run() -> None:
            with unittest.mock.patch.object(sd_lib, "process_table", return_value=table), \
                    unittest.mock.patch.object(sd_lib, "signal_group", side_effect=record), \
                    unittest.mock.patch.object(sd_lib, "GROUP_CLEANUP_SECONDS", 0.1):
                groups.end_groups()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive(), "the handler's end_groups deadlocked the KILL loop")

    def test_a_reentrant_stderr_write_stops_no_kill(self) -> None:
        """pack sd:9600 class pass: the handler's diagnostic line re-entered the stderr writer the main
        thread was in, which raises RuntimeError, and the SIGKILLs below never went out."""
        class Busy(io.StringIO):
            def write(self, text: str) -> int:
                raise RuntimeError("reentrant call inside <_io.BufferedWriter name='<stderr>'>")
        earlier = {100: (1, 100, "Thu Jan  1 00:00:00 1970"), 200: (100, 200, "Thu Jan  1 00:00:01 1970")}
        with unittest.mock.patch.object(sd_lib, "_group_of", side_effect=lambda pid: pid), \
                unittest.mock.patch("sys.stderr", new=Busy()), \
                unittest.mock.patch.object(os, "killpg") as killpg:
            sd_lib.kill_below(100, earlier, None)
        self.assertIn(unittest.mock.call(200, signal.SIGKILL), killpg.call_args_list)

    def test_a_group_that_left_the_set_during_the_kill_read_gets_no_sigkill(self) -> None:
        """pack sd:9600, sd_lib.py:4014: the set was read before the up to 3 s KILL read; a group whose
        run_group ended it and reaped its leader in that read still had its bare number signalled."""
        groups = sd_lib.ThreadGroups()
        hook = groups.group_hook()
        hook(4242)
        table = {4242: (1, 4242, "Thu Jan  1 00:00:00 1970")}
        count = [0]

        def read() -> Any:
            count[0] += 1
            if count[0] == 2:
                hook(None)  # its run_group began ending it during the KILL read
            return table
        sent: list[tuple[int, int]] = []
        with unittest.mock.patch.object(sd_lib, "process_table", side_effect=read), \
                unittest.mock.patch.object(sd_lib, "signal_group", side_effect=lambda *args: sent.append(args)), \
                unittest.mock.patch.object(sd_lib, "GROUP_CLEANUP_SECONDS", 0.1):
            groups.end_groups()
        self.assertNotIn((4242, signal.SIGKILL), sent)


class TermEndsGroupTests(unittest.TestCase):
    """sd:5776: SIGTERM inside nested blocks unwinds to the block that holds it, which then ends the process."""

    def test_the_outer_block_runs_first_and_its_cleanup_then_the_process_ends_on_sigterm(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            said = pathlib.Path(scratch) / "said"
            script = (
                "import pathlib, sys, time\n"
                f"sys.path.insert(0, {str(REPO_ROOT / 'bin')!r})\n"
                "import sd_lib\n"
                f"said = pathlib.Path({str(said)!r})\n"
                "def say(text):\n"
                "    with said.open('a') as handle:\n"
                "        handle.write(text + '\\n')\n"
                "with sd_lib.term_ends_group(first=lambda: say('first')):\n"
                "    try:\n"
                "        with sd_lib.term_ends_group():\n"
                "            try:\n"
                "                say('ready')\n"
                "                time.sleep(30)\n"
                "            finally:\n"
                "                say('inner cleanup')\n"
                "    finally:\n"
                "        say('outer cleanup')\n"
                "say('after the block')\n")
            child = subprocess.Popen([sys.executable, "-c", script], stderr=subprocess.PIPE, text=True)
            for _ in range(400):  # bounded: a child that never gets ready fails below
                if (said.exists() and said.read_text() == "ready\n") or child.poll() is not None:
                    break
                time.sleep(0.05)
            child.send_signal(signal.SIGTERM)
            _, errors = child.communicate(timeout=30)
            self.assertEqual(child.returncode, -signal.SIGTERM, errors)
            self.assertEqual(said.read_text().splitlines(), ["ready", "first", "inner cleanup", "outer cleanup"])

    def test_a_sighup_ends_the_group_as_a_sigterm_does(self) -> None:
        """pack sd:9568, failure table row 7 of sd:9392: a closed terminal sends SIGHUP, whose default
        action ended `sd-ship lane run` with no unwind, so the group `run_group` led ran on with ppid 1."""
        with tempfile.TemporaryDirectory() as scratch:
            mark = pathlib.Path(scratch) / "group.pid"
            script = (
                "import sys\n"
                f"sys.path.insert(0, {str(REPO_ROOT / 'bin')!r})\n"
                "import pathlib, sd_lib\n"
                "with sd_lib.term_ends_group():\n"
                f"    sd_lib.run_group(['sleep', '60'], cwd=pathlib.Path('.'), env={{'PATH': '/usr/bin:/bin'}},\n"
                f"                     timeout=60, started=lambda pid: pid and pathlib.Path({str(mark)!r}).write_text(str(pid)))\n")
            child = subprocess.Popen([sys.executable, "-c", script], stderr=subprocess.PIPE, text=True)
            group = wait_for_pid(mark)
            self.addCleanup(end_session, group)
            child.send_signal(signal.SIGHUP)
            _, errors = child.communicate(timeout=30)
            self.assertEqual(child.returncode, -signal.SIGHUP, errors)
            self.assertTrue(gone(group), "the group run_group led outlived the SIGHUP")

    def test_an_ignored_sighup_stays_ignored(self) -> None:
        """pack sd:9568: under `nohup` the caller ignores SIGHUP; the block keeps that, as it keeps a TERM handler."""
        script = (
            "import signal, sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT / 'bin')!r})\n"
            "import sd_lib\n"
            "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
            "with sd_lib.term_ends_group():\n"
            "    print(signal.getsignal(signal.SIGHUP) is signal.SIG_IGN)\n"
            "print(signal.getsignal(signal.SIGHUP) is signal.SIG_IGN)\n")
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(done.stdout.split(), ["True", "True"], done.stderr)


class SiblingTests(unittest.TestCase):
    """`sibling` loads a suffixless `bin/` tool that a caller shares."""

    def test_a_sibling_that_defines_a_dataclass_loads(self) -> None:
        # The dataclass decorator looks the module up by name while it is
        # built; an unregistered module failed with AttributeError (sd:3560).
        adopt = sd_lib.sibling("sd_skill_adopt", "sd-skill-adopt")
        self.assertEqual(adopt.Finding("warn", "rule", 1).line, 1)


class RepoDeclarationTests(Fixture):
    """`sd_lib.repo_declaration` (sd:5109): the row's object, and what a read that fails means."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config")}

    def register(self, root: pathlib.Path, remote: str | None = None) -> None:
        if not sd_db.default_path(self.home).exists():
            sd_db.initialise(home=self.home)
        connection = sd_db.connect(home=self.home, write=True)
        try:
            sd_db.upsert_repo(connection, str(root), remote=remote, managed=1)
        finally:
            connection.close()

    def declare(self, root: pathlib.Path, kind: str, value: dict) -> None:
        self.register(root)
        connection = sd_db.connect(home=self.home, write=True)
        try:
            sd_db.repos.set_declaration(connection, str(root), kind, value)
        finally:
            connection.close()

    def test_a_linked_worktree_reads_its_repositorys_row(self) -> None:
        root = self.make_repo()
        self.declare(root, "review", {"default_tier": "cheap"})
        worktree = self.add_worktree(root)
        self.assertEqual(sd_lib.repo_declaration(worktree, "review", self.env), {"default_tier": "cheap"})
        self.assertIsNone(sd_lib.repo_declaration(worktree, "status", self.env))

    def test_no_database_on_the_machine_declares_nothing(self) -> None:
        self.assertIsNone(sd_lib.repo_declaration(self.make_repo(), "review", self.env))

    def test_a_database_that_does_not_open_is_unknown_never_none(self) -> None:
        root = self.make_repo()
        self.declare(root, "review", {})
        sd_db.default_path(self.home).write_bytes(b"not a database")
        with self.assertRaises(sd_lib.DatabaseUnreadable):
            sd_lib.repo_declaration(root, "review", self.env)

    def test_a_clone_whose_origin_two_rows_share_is_refused(self) -> None:
        """Older state: two rows for one remote. A guess would apply another repository's policy."""
        remote = "https://github.com/example/shared.git"
        for name in ("one", "two"):
            self.register(self.make_repo(name), remote)
        clone = self.make_repo("clone")
        self.git(clone, "remote", "add", "origin", remote)
        with self.assertRaises(sd_lib.DatabaseUnreadable) as caught:
            sd_lib.repo_declaration(clone, "review", self.env)
        self.assertIn("origin is the remote of 2 rows", str(caught.exception))

    def test_an_unknown_kind_is_a_programming_error(self) -> None:
        with self.assertRaises(ValueError):
            sd_lib.repo_declaration(self.tmp, "reviews", self.env)

    def test_the_kinds_are_the_librarys_kinds(self) -> None:
        """bin/ keeps its own copy, as it imports the library only on demand; the two must not drift."""
        self.assertEqual(sd_lib.DECLARATION_KINDS, sd_db.repos.DECLARATION_KINDS)

    def test_an_experiment_declaration_reads_back_from_the_row(self) -> None:
        """sd:5302. A research repository names its Notion experiment page and that page's data source."""
        root = self.make_repo()
        value = {"page": "https://www.notion.so/example/Experiment-0123456789abcdef0123456789abcdef",
                 "data_source": "collection://01234567-89ab-cdef-0123-456789abcdef", "title": "An experiment"}
        self.assertEqual(sd_lib.declaration_problems("experiment", value), [])
        self.declare(root, "experiment", value)
        self.assertEqual(sd_lib.repo_declaration(self.add_worktree(root), "experiment", self.env), value)


class ExperimentDeclarationTests(unittest.TestCase):
    """sd:5302. What `sd-db repo declare PATH experiment` accepts: a Notion page and its data source, a title."""

    PAGE = "0123456789abcdef0123456789abcdef"
    SOURCE = "01234567-89ab-cdef-0123-456789abcdef"

    def test_the_library_spells_the_ids_as_the_settings_do(self) -> None:
        """The check lives in the library, which the dashboard calls; its patterns are bin's Notion settings'."""
        self.assertEqual(sd_db.repos.EXPERIMENT_FIELDS,
                         {"page": sd_lib.NOTION_PAGE, "data_source": sd_lib.NOTION_DATA_SOURCE})

    def test_each_accepted_shape_reads(self) -> None:
        for value in ({"page": self.PAGE, "data_source": self.SOURCE},
                      {"page": "01234567-89AB-CDEF-0123-456789ABCDEF", "data_source": "collection://" + self.PAGE},
                      {"page": f"https://www.notion.so/Title-{self.PAGE}?pvs=4", "data_source": self.SOURCE,
                       "title": "t", "url": f"https://www.notion.so/Title-{self.PAGE}"}):
            with self.subTest(value=value):
                self.assertEqual(sd_lib.declaration_problems("experiment", value), [])

    def test_every_value_outside_the_shape_is_refused(self) -> None:
        """Property over extreme inputs: wrong types, empty and huge strings, ids one digit short, unknown keys."""
        import random

        good = {"page": self.PAGE, "data_source": self.SOURCE}
        cases: list = [{}, {"page": self.PAGE}, {"data_source": self.SOURCE}, {**good, "url": "x"},
                       {**good, "url": "https://"}, {**good, "url": "https://example.test/" + "a" * 2001},
                       {**good, "decks": []},
                       {**good, "title": ""}, {**good, "title": " "}, {**good, "title": 7},
                       {**good, "title": "t" * 2001}, {**good, "page": self.PAGE[:-1]},
                       {**good, "page": "https://example.test/" + "a/" * 10000 + self.PAGE},
                       {**good, "data_source": "https://www.notion.so/" + self.PAGE},
                       {**good, "data_source": self.SOURCE + "0"}, {**good, "page": "z" * 32}]
        for wrong in (None, 0, 1.5, float("nan"), float("inf"), True, [], {}, [self.PAGE], "", " " + self.PAGE):
            cases += [{**good, "page": wrong}, {**good, "data_source": wrong}]
        draw = random.Random(5302)
        for _ in range(200):
            # A `g` in every draw keeps it from spelling a valid id by chance.
            text = "".join(draw.choice("0123456789abcdef-/:") for _ in range(draw.randrange(0, 40)))
            cases.append({**good, draw.choice(["page", "data_source"]): text + "g"})
        for value in cases:
            with self.subTest(value=str(value)[:80]):
                self.assertTrue(sd_lib.declaration_problems("experiment", value))


if __name__ == "__main__":
    unittest.main()
