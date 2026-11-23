"""Criterion 26, the Codex half: rows read back out of `~/.codex/sessions`.

There is no recorded-session fixture anywhere in this repository, so the
transcripts here are built line by line. They are shaped from the real thing:
a `session_meta` record carrying `cwd`, `event_msg`/`user_message` turns, and
`response_item`/`custom_tool_call` records whose `input` is the shell command
Codex uses to open a skill -- which is the only place a skill name appears on
that surface.

The three things worth breaking are all here: a session counts once however
often it names a skill, the cursor moves only on a clean run, and a damaged
line costs one measurement rather than the nightly.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    """Import a `bin/` module by path -- `bin/` is not a package."""
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "bin" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sd_codex = load("sd_codex")

import sd_db  # noqa: E402 - after the path juggling above


def meta(cwd: str) -> str:
    return json.dumps({
        "timestamp": "2026-09-01T09:00:00.000Z",
        "type": "session_meta",
        "payload": {"cwd": cwd, "originator": "Codex CLI"},
    })


def opened(skill: str, stamp: str) -> str:
    """A tool call that reads a skill, the way Codex actually reads one."""
    return json.dumps({
        "timestamp": stamp,
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": "exec",
            "input": "await tools.exec_command({command: "
                     f'"cat /Users/x/.codex/skills/{skill}/SKILL.md"' + "})",
        },
    })


def typed(text: str, stamp: str) -> str:
    return json.dumps({
        "timestamp": stamp,
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text},
    })


def spoken(text: str, stamp: str) -> str:
    """The other user-turn shape: a `message` record with a content list."""
    return json.dumps({
        "timestamp": stamp,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    })


class CodexCase(unittest.TestCase):
    """A scratch `CODEX_HOME` beside a real database."""

    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self._scratch.cleanup)
        self.home = Path(self._scratch.name).resolve()
        sd_db.initialise(home=self.home)
        self.connection = sd_db.connect(sd_db.default_path(self.home))
        self.addCleanup(self.connection.close)
        self.root = self.home / ".codex" / "sessions"

    def transcript(self, day: str, name: str, *lines: str) -> Path:
        folder = self.root / day.replace("-", "/")
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / f"rollout-{day}T09-00-00-{name}.jsonl"
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return source

    def rows(self) -> list[tuple]:
        found = self.connection.execute(
            "SELECT skill, surface, mode, cwd, timestamp FROM skill_use "
            "ORDER BY timestamp, skill, mode"
        ).fetchall()
        return [tuple(row) for row in found]

    def scan(self, since=None) -> dict:
        return sd_codex.collect(sd_db, self.connection, self.root, since)


class WhatCountsAsAUse(CodexCase):
    def test_a_skill_opened_by_path_is_one_row_on_the_codex_surface(self):
        self.transcript(
            "2026-09-01", "aaa",
            meta("/Users/x/repos/thing"),
            opened("sd-review", "2026-09-01T09:01:00.000Z"),
        )
        report = self.scan()
        self.assertTrue(report["ok"], report["reason"])
        # The stored stamp is the library's normal form, not the transcript's
        # text: `sd_db.writes.stamp` re-renders what it is handed, so a row
        # written from a Codex `...Z` and one written by a hook sort together.
        self.assertEqual(
            self.rows(),
            [("sd-review", "codex", "path", "/Users/x/repos/thing",
              "2026-09-01T09:01:00+00:00")],
        )

    def test_a_session_that_names_a_skill_ten_times_writes_one_row(self):
        """The row is "this session used this skill", not "this line said so".

        Real transcripts echo a single read through the tool call, its output
        and every later turn that quotes either. On the machine this was
        written against one skill reached four figures of textual hits across
        a few dozen sessions; counted per mention the table would measure how
        chatty a session was.
        """
        self.transcript(
            "2026-09-01", "aaa",
            meta("/Users/x/repos/thing"),
            *[opened("sd-review", f"2026-09-01T09:0{n}:00.000Z") for n in range(1, 10)],
        )
        self.scan()
        self.assertEqual(len(self.rows()), 1)

    def test_the_row_carries_the_first_stamp_and_not_the_last(self):
        self.transcript(
            "2026-09-01", "aaa",
            meta("/Users/x/repos/thing"),
            opened("sd-review", "2026-09-01T09:05:00.000Z"),
            opened("sd-review", "2026-09-01T09:09:00.000Z"),
        )
        self.scan()
        self.assertEqual(self.rows()[0][4], "2026-09-01T09:05:00+00:00")

    def test_two_sessions_naming_one_skill_are_two_rows(self):
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        opened("sd-review", "2026-09-01T09:01:00.000Z"))
        self.transcript("2026-09-02", "bbb", meta("/b"),
                        opened("sd-review", "2026-09-02T09:01:00.000Z"))
        self.scan()
        self.assertEqual([row[3] for row in self.rows()], ["/a", "/b"])

    def test_a_typed_slash_command_is_a_direct_use(self):
        """Codex has no such surface today; the branch is here for when it does.

        `bin/sd_install.py:203` renders the Codex home as a directory of
        `SKILL.md` files and there is no `~/.codex/prompts`, so nothing on
        this surface produces a `/sd-*` turn. The criterion names one, and it
        costs a branch rather than a module to be ready for it.
        """
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        typed("/sd-example the thing", "2026-09-01T09:01:00.000Z"))
        self.scan()
        self.assertEqual(self.rows(), [
            ("sd-example", "codex", "direct", "/a", "2026-09-01T09:01:00+00:00")])

    def test_the_other_user_turn_shape_is_read_too(self):
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        spoken("/sd-example", "2026-09-01T09:01:00.000Z"))
        self.scan()
        self.assertEqual([row[2] for row in self.rows()], ["direct"])

    def test_one_session_can_produce_both_modes(self):
        self.transcript(
            "2026-09-01", "aaa", meta("/a"),
            typed("/sd-example", "2026-09-01T09:01:00.000Z"),
            opened("sd-review", "2026-09-01T09:02:00.000Z"),
        )
        self.scan()
        self.assertEqual(
            [(row[0], row[2]) for row in self.rows()],
            [("sd-example", "direct"), ("sd-review", "path")],
        )


class WhatDoesNot(CodexCase):
    def test_an_empty_surface_writes_nothing_and_is_not_an_error(self):
        report = self.scan()
        self.assertTrue(report["ok"])
        self.assertEqual((report["files"], report["rows"]), (0, 0))
        self.assertEqual(self.rows(), [])

    def test_a_damaged_line_costs_one_measurement_and_not_the_run(self):
        self.transcript(
            "2026-09-01", "aaa",
            meta("/a"),
            "{ this is not json",
            opened("sd-review", "2026-09-01T09:02:00.000Z"),
        )
        report = self.scan()
        self.assertTrue(report["ok"], report["reason"])
        self.assertEqual([row[0] for row in self.rows()], ["sd-review"])

    def test_prose_that_merely_mentions_a_slash_word_is_not_a_use(self):
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        typed("look at the /etc dir", "2026-09-01T09:01:00.000Z"))
        self.scan()
        self.assertEqual(self.rows(), [])

    def test_a_bare_slash_names_no_skill(self):
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        typed("/", "2026-09-01T09:01:00.000Z"))
        self.scan()
        self.assertEqual(self.rows(), [])

    def test_a_skill_path_that_is_not_under_codex_is_not_a_codex_use(self):
        """`~/.claude/skills/<name>/SKILL.md` in a transcript is Claude's row.

        `bin/sd-skill-use` wrote it as it happened. Counting it again here
        would double every skill the operator uses in both places, on a
        surface that did not see the use.
        """
        self.transcript("2026-09-01", "aaa", meta("/a"), json.dumps({
            "timestamp": "2026-09-01T09:01:00.000Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec",
                        "input": "cat /Users/x/.claude/skills/sd-review/SKILL.md"},
        }))
        self.scan()
        self.assertEqual(self.rows(), [])

    def test_a_session_with_no_cwd_writes_a_row_without_one(self):
        self.transcript("2026-09-01", "aaa",
                        opened("sd-review", "2026-09-01T09:01:00.000Z"))
        self.scan()
        self.assertEqual(self.rows()[0][3], None)


class TheCursor(CodexCase):
    def test_a_second_scan_over_the_same_transcripts_writes_nothing(self):
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        opened("sd-review", "2026-09-01T09:01:00.000Z"))
        first = self.scan()
        sd_codex.write_cursor(sd_db, self.connection, first["through"], first["files"])
        again = self.scan(sd_codex.read_cursor(sd_db, self.connection))
        self.assertEqual(again["rows"], 0)
        self.assertEqual(len(self.rows()), 1)

    def test_a_session_after_the_cursor_is_read_and_one_before_it_is_not(self):
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        opened("sd-old", "2026-09-01T09:01:00.000Z"))
        self.transcript("2026-09-03", "bbb", meta("/b"),
                        opened("sd-new", "2026-09-03T09:01:00.000Z"))
        self.scan("2026-09-02T00:00:00.000Z")
        self.assertEqual([row[0] for row in self.rows()], ["sd-new"])

    def test_a_session_that_crossed_midnight_is_still_reached(self):
        """The day directory is a coarse filter and the overlap is why.

        A session that opened on the 1st writes records dated the 2nd into the
        1st's directory. Filtering directories on the cursor's own day exactly
        would step over them, once, silently, and only for sessions that ran
        late.
        """
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        opened("sd-late", "2026-09-02T01:30:00.000Z"))
        self.scan("2026-09-02T00:00:00.000Z")
        self.assertEqual([row[0] for row in self.rows()], ["sd-late"])

    def test_the_cursor_is_the_newest_stamp_actually_read(self):
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        opened("sd-a", "2026-09-01T09:01:00.000Z"),
                        opened("sd-b", "2026-09-01T11:00:00.000Z"))
        self.assertEqual(self.scan()["through"], "2026-09-01T11:00:00.000Z")

    def test_an_unwritten_cursor_does_not_count(self):
        """A `watermark` row without `resolved_at` is a write that did not end.

        `read_watermark` takes only resolved rows, and resuming from half a
        cursor is the one way this table could make the scan skip a window.
        """
        sd_db.record_state(
            self.connection, "watermark", key=sd_codex.CURSOR_KEY,
            body={"collected_at": "2099-01-01T00:00:00Z"},
        )
        self.assertIsNone(sd_codex.read_cursor(sd_db, self.connection))

    def test_a_run_that_stops_leaves_the_cursor_where_it_was(self):
        """`collect` reports rather than raises, and `ok` gates the cursor.

        A partial read that moved the cursor would lose the window it failed
        on -- permanently, because nothing ever looks at it again.
        """
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        opened("sd-review", "2026-09-01T09:01:00.000Z"))

        class Broken:
            @staticmethod
            def record_skill_use(*args, **kwargs):
                raise RuntimeError("disk went away")

        report = sd_codex.collect(Broken, self.connection, self.root, None)
        self.assertFalse(report["ok"])
        self.assertIn("disk went away", report["reason"])
        self.assertEqual(self.rows(), [])
        self.assertIn("cursor did not move", "\n".join(sd_codex.render(report)))


class WhatItRefusesToBreak(CodexCase):
    def test_an_absent_sessions_directory_is_a_state_and_not_a_fault(self):
        self.assertEqual(sd_codex.rollouts(self.home / "nowhere", None), [])

    def test_codex_home_moves_the_surface(self):
        moved = {"CODEX_HOME": "/srv/codex"}
        self.assertEqual(sd_codex.sessions_root(moved), Path("/srv/codex/sessions"))
        self.assertEqual(
            sd_codex.sessions_root({}), Path.home() / ".codex" / "sessions")

    def test_a_directory_that_is_not_a_date_is_read_rather_than_guessed_at(self):
        """Skipping what does not look like a date is how a surface goes dark.

        The layout is Codex's to change. A file that holds nothing new costs
        one parse; a filter that silently drops a whole tree costs every row
        in it, with no way to notice.
        """
        odd = self.root / "archive" / "old" / "batch"
        odd.mkdir(parents=True)
        (odd / "rollout-x.jsonl").write_text(
            opened("sd-odd", "2026-09-09T09:00:00.000Z") + "\n", encoding="utf-8")
        self.scan("2026-09-08T00:00:00.000Z")
        self.assertEqual([row[0] for row in self.rows()], ["sd-odd"])

    def test_an_unreadable_cursor_means_a_full_rescan_and_not_a_crash(self):
        class Broken:
            @staticmethod
            def read_watermark(*args, **kwargs):
                raise RuntimeError("no such column")

        self.assertIsNone(sd_codex.read_cursor(Broken, self.connection))

    def test_the_render_says_what_was_written_and_what_it_read_through(self):
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        opened("sd-review", "2026-09-01T09:01:00.000Z"))
        lines = sd_codex.render(self.scan())
        self.assertIn("1 transcript(s), 1 row(s) written", lines[0])
        self.assertIn("sd-review", lines[1])
        self.assertIn("read through 2026-09-01T09:01:00.000Z", lines[-1])


class TheVerbEndToEnd(CodexCase):
    """`sd skill scan` as the nightly runs it, against the scratch home.

    A subprocess and not a call: `skill_scan` finds both the transcripts and
    the database through `$HOME`, which is the seam the scheduler uses and the
    only one worth asserting.
    """

    def scan_cli(self, *flags: str) -> tuple[int, str]:
        done = subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "sd"), "skill", "scan", *flags],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(self.home)},
            cwd=tempfile.gettempdir(),
        )
        return done.returncode, done.stdout + done.stderr

    def test_the_json_form_is_the_report_itself(self):
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        opened("sd-review", "2026-09-01T09:01:00.000Z"))
        code, output = self.scan_cli("--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["rows"], 1)

    def test_a_second_run_writes_nothing_because_the_cursor_moved(self):
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        opened("sd-review", "2026-09-01T09:01:00.000Z"))
        self.assertEqual(self.scan_cli("--json")[0], 0)
        code, output = self.scan_cli("--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["rows"], 0)

    def test_dry_run_writes_the_rows_and_leaves_the_cursor(self):
        self.transcript("2026-09-01", "aaa", meta("/a"),
                        opened("sd-review", "2026-09-01T09:01:00.000Z"))
        self.assertEqual(self.scan_cli("--dry-run", "--json")[0], 0)
        self.assertIsNone(sd_codex.read_cursor(sd_db, self.connection))

    def test_a_quiet_night_exits_zero_rather_than_looking_broken(self):
        """A nightly that found nothing is not a failure.

        A non-zero exit here would page the operator every quiet day, which is
        how a scheduled job gets switched off.
        """
        code, output = self.scan_cli()
        self.assertEqual(code, 0, output)
        self.assertIn("0 row(s) written", output)


class TheRefusalNamesTheFault(unittest.TestCase):
    """sd:746. `_library` asks `sd_lib.import_sd_db`, and picks a sentence per fault.

    The helper is replaced with its two failure answers rather than driven
    against a real `.venv`: what is under test here is only which sentence
    `sd_codex` chooses, and the helper's own tries are tested where it lives.
    """

    def refusal(self, problem: str, provisioned: str) -> str:
        answer = SimpleNamespace(module=None, problem=problem, provisioned=provisioned)
        helper = SimpleNamespace(import_sd_db=lambda: answer)
        with mock.patch.dict(sys.modules, {"sd_lib": helper}):
            with self.assertRaises(sd_codex.CodexRefusal) as raised:
                sd_codex._library()
        return str(raised.exception)

    def test_nothing_to_import_gets_the_installer(self):
        said = self.refusal("sd_db is not installed here: No module named 'sd_db'", "")
        self.assertEqual(said, sd_codex.NOT_INSTALLED, "said once, not NOT_INSTALLED plus the helper's echo")

    def test_a_provisioned_copy_that_will_not_import_is_not_called_absent(self):
        said = self.refusal("sd_db is provisioned at /pack/site but will not import: broken", "/pack/site")
        self.assertIn("will not import: broken", said)
        self.assertNotIn("sd-install", said)


if __name__ == "__main__":
    unittest.main()
