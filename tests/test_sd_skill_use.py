"""The `skill_use` hook, which is the pack finding out what is actually used.

`bin/sd_install.py:1323` already asks `sd_db.writes.skill_use_since` whether a
skill has been used before it keeps one an operator did not choose. Until this
hook existed nothing answered: the table, its writer and its reader were all
built, and the only rows anywhere came from `tests/test_sd_skill.py`.

Every test here writes to a real `sd_db` under a `HOME` nothing else shares,
because the claim being asserted is that a row landed with the right values in
it. A double would assert that the right call was made, which is the weaker
claim, and `mode` in particular is a value the SQL constrains and the caller
can still get backwards.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess  # nosec B404 - fixed argv, running the hook as a program
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "bin" / "sd-skill-use"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_db  # noqa: E402 - installed into this virtualenv by `make setup`


def payload(**fields: Any) -> str:
    return json.dumps(fields)


class Fixture(unittest.TestCase):
    """One checkout, one HOME, one database, and the hook run in process."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.repo = self.tmp / "checkout"
        (self.repo / "src").mkdir(parents=True)
        subprocess.run(  # nosec B603 B607 - fixed argv, a scratch repository
            ["git", "init", "-q"], cwd=self.repo, check=True)

        patched = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)

        self.module = self.load()

    def load(self) -> Any:
        """The hook, imported from a file whose name has no `.py` suffix."""
        import importlib.util

        spec = importlib.util.spec_from_loader(
            "sd_skill_use_under_test",
            importlib.machinery.SourceFileLoader(
                "sd_skill_use_under_test", str(HOOK)),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def database(self) -> Any:
        sd_db.initialise(home=self.home)
        connection = sd_db.connect(home=self.home)
        self.addCleanup(connection.close)
        return connection

    def fire(self, text: str, **environ: str) -> int:
        env = {"HOME": str(self.home), "PWD": str(self.repo / "src")}
        env.update(environ)
        return self.module.run(text, env)

    def rows(self, connection: Any) -> list[dict]:
        cursor = connection.execute(
            "SELECT skill, surface, mode, cwd FROM skill_use ORDER BY id")
        return [dict(zip(("skill", "surface", "mode", "cwd"), r, strict=True))
                for r in cursor.fetchall()]


class WhatCountsAsAUse(Fixture):
    def test_the_skill_tool_firing_is_a_direct_use(self) -> None:
        connection = self.database()
        self.assertEqual(self.fire(payload(
            hook_event_name="PreToolUse", tool_name="Skill",
            tool_input={"skill": "sd-ship"})), 0)
        self.assertEqual(
            self.rows(connection),
            [{"skill": "sd-ship", "surface": "claude", "mode": "direct",
              "cwd": str(self.repo)}],
        )

    def test_reading_a_skill_file_is_a_path_use(self) -> None:
        """The distinction the whole table exists for.

        A skill read because a path lists it is a skill nobody chose, and
        `sd skill try`'s verdict turns on which of the two a use was.
        """

        connection = self.database()
        where = str(self.home / ".claude" / "skills" / "sd-example" / "SKILL.md")
        self.fire(payload(
            hook_event_name="PreToolUse", tool_name="Read",
            tool_input={"file_path": where}))
        self.assertEqual(
            [(r["skill"], r["mode"]) for r in self.rows(connection)],
            [("sd-example", "path")],
        )

    def test_a_slash_prompt_is_a_direct_use(self) -> None:
        connection = self.database()
        self.fire(payload(
            hook_event_name="UserPromptSubmit", prompt="/sd-example the next item"))
        self.assertEqual(
            [(r["skill"], r["mode"]) for r in self.rows(connection)],
            [("sd-example", "direct")],
        )

    def test_the_cwd_recorded_is_the_checkout_and_not_the_subdirectory(self) -> None:
        """`cwd` is what a reader joins against the `repo` table, so two uses
        from two subdirectories of one checkout are two rows about one repo."""

        connection = self.database()
        self.fire(payload(
            hook_event_name="PreToolUse", tool_name="Skill",
            tool_input={"skill": "sd-ship"}, cwd=str(self.repo / "src")))
        self.assertEqual(self.rows(connection)[0]["cwd"], str(self.repo))


class WhatDoesNot(Fixture):
    def test_an_ordinary_tool_call_writes_nothing(self) -> None:
        connection = self.database()
        self.fire(payload(
            hook_event_name="PreToolUse", tool_name="Bash",
            tool_input={"command": "ls"}))
        self.assertEqual(self.rows(connection), [])

    def test_reading_an_ordinary_file_writes_nothing(self) -> None:
        connection = self.database()
        self.fire(payload(
            hook_event_name="PreToolUse", tool_name="Read",
            tool_input={"file_path": str(self.repo / "src" / "SKILL.md")}))
        self.assertEqual(self.rows(connection), [])

    def test_an_ordinary_prompt_writes_nothing(self) -> None:
        connection = self.database()
        self.fire(payload(
            hook_event_name="UserPromptSubmit", prompt="fix the failing test"))
        self.assertEqual(self.rows(connection), [])

    def test_another_event_entirely_writes_nothing(self) -> None:
        """Registered on two events; a third arriving is a misconfiguration
        and the row it would write would be a wrong one."""

        connection = self.database()
        self.fire(payload(
            hook_event_name="SessionStart", tool_name="Skill",
            tool_input={"skill": "sd-ship"}))
        self.assertEqual(self.rows(connection), [])

    def test_the_opt_out_writes_nothing(self) -> None:
        connection = self.database()
        self.fire(payload(
            hook_event_name="PreToolUse", tool_name="Skill",
            tool_input={"skill": "sd-ship"}), SD_SKILL_USE="0")
        self.assertEqual(self.rows(connection), [])


class WhatItRefusesToBreak(Fixture):
    """A hook that breaks a tool call is worse than a lost measurement."""

    def test_with_no_database_at_all_it_still_returns_zero(self) -> None:
        self.assertEqual(self.fire(payload(
            hook_event_name="PreToolUse", tool_name="Skill",
            tool_input={"skill": "sd-ship"})), 0)

    def test_garbage_on_stdin_returns_zero(self) -> None:
        for text in ("", "   ", "not json", "[]", "null"):
            self.assertEqual(self.fire(text), 0, text)

    def test_a_payload_missing_every_field_returns_zero(self) -> None:
        self.assertEqual(self.fire(payload(hook_event_name="PreToolUse")), 0)

    def test_it_never_writes_to_stdout(self) -> None:
        """A `PreToolUse` hook that prints is injecting text into the session
        before a tool runs. This one has nothing to say to the model."""

        self.database()
        done = subprocess.run(  # nosec B603 - fixed argv, the hook as installed
            [sys.executable, str(HOOK)],
            input=payload(hook_event_name="PreToolUse", tool_name="Skill",
                          tool_input={"skill": "sd-ship"}),
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(self.home), "PWD": str(self.repo)},
            check=False,
        )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "")


if __name__ == "__main__":
    unittest.main()
