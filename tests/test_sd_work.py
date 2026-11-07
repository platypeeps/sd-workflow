"""The real CLI operates on a scratch database, without repository ceremony."""

import argparse
import contextlib
import getpass
import importlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import sd_db  # noqa: E402
import sd_db.repos  # noqa: E402
import sd_db.today  # noqa: E402
import sd_db.workflow as workflow  # noqa: E402
import sd_lib  # noqa: E402
import sd_work  # noqa: E402
from sd_db.workflow import NOTE_KINDS  # noqa: E402
from sd_views import HEADINGS  # noqa: E402

from tests.clean_env import clean_environment  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def scratch_environment(home: Path) -> dict[str, str]:
    """This process's environment under a scratch `HOME`, without the caller's `CLAUDE_PID`.

    `sd today` nudges a session begun before today (sd:6955), so a child that inherited the pid of a
    Claude session started yesterday printed one more line after local midnight (sd:7154).
    """
    return {**{key: value for key, value in os.environ.items() if key != "CLAUDE_PID"}, "HOME": str(home)}


@contextlib.contextmanager
def database(*args, **kwargs):
    """`sd_db.connect` that commits and then closes: a bare `with` commits only, and the unclosed
    connection printed a ResourceWarning into whichever later test's captured stderr ran the collector
    (sd:7154)."""
    with contextlib.closing(sd_db.connect(*args, **kwargs)) as connection, connection:
        yield connection


class TaskCLI(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name)
        sd_db.initialise(home=self.home)
        self.environment = scratch_environment(self.home)
        # The library keys a checkout under `$HOME` as `~/...` (sd:1439), so the
        # rows this process writes and the ones the CLI writes agree only when
        # both read the scratch home.
        home = unittest.mock.patch.dict(os.environ, {"HOME": str(self.home)})
        home.start()
        self.addCleanup(home.stop)

    def call(self, *arguments, code=0, cwd=None, env=None):
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "sd"), *map(str, arguments)],
            cwd=str(cwd or self.home), env=env or self.environment,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def test_an_add_without_a_priority_is_refused_but_an_idea_files(self):
        """sd:6726: a work item is filed ranked; an idea is not ranked."""
        refused = self.call("task", "add", "Unranked", code=1).stderr
        self.assertIn("sd task add needs --priority 1-4 for a task", refused)
        self.assertIn("needs --priority 1-4 for a followup", self.call("task", "add", "F", "--kind", "followup", code=1).stderr)
        self.call("task", "add", "An idea", "--kind", "work-idea")
        self.call("task", "add", "Ranked", "--priority", 3)

    def test_experiment_designates_and_refuses_a_bad_parent(self):
        """sd:3982: `--experiment` writes the designation; a task of itself or of an undesignated item refuses."""
        first = json.loads(self.call("task", "add", "--priority", "3", "An experiment", "--json").stdout)["item"]["id"]
        second = json.loads(self.call("task", "add", "--priority", "3", "A task", "--json").stdout)["item"]["id"]
        self.assertIn("task of itself",
                      self.call("task", "edit", second, "--experiment", f"of:{second}", code=1).stderr)
        self.assertIn(f"sd:{first} is not designated experiment",
                      self.call("task", "edit", second, "--experiment", f"of:{first}", code=1).stderr)
        self.assertIn("without other changes",
                      self.call("task", "edit", first, "--experiment", "yes", "--priority", 1, code=1).stderr)
        self.call("task", "edit", first, "--experiment", "yes")
        state = json.loads(self.call("task", "edit", second, "--experiment", f"of:{first}", "--json").stdout)
        designation = json.loads(state["item"]["fields"])["experiments"]
        self.assertEqual((designation["as"], designation["of"]), ("task", first))

    def test_task_lifecycle_outside_checkout_and_stale_edit(self):
        state = json.loads(self.call("task", "add", "--priority", "3", "Check the workflow", "--json").stdout)
        item = state["item"]["id"]
        edited = json.loads(self.call(
            "task", "edit", item, "--priority", 1, "--due", "2026-09-08",
            "--if-revision", state["revision"], "--json").stdout)
        refused = self.call("task", "status", item, "done", "--if-revision",
                            state["revision"], code=1)
        self.assertIn("changed", refused.stderr)
        readback = json.loads(self.call("store", "item", item, "--json").stdout)
        self.assertEqual(readback, edited)
        noted = json.loads(self.call("task", "note", item, "Verify on iPad",
                                     "--json").stdout)
        self.call("note", "resolve", noted["note"]["id"])
        resolved = json.loads(self.call("task", "show", item, "--json").stdout)
        self.assertTrue(next(note for note in resolved["notes"]
                             if note["id"] == noted["note"]["id"])["resolved_at"])
        completed = json.loads(self.call("task", "status", item, "done", "--json").stdout)
        self.assertEqual(completed["item"]["status"], "done")
        self.assertEqual(json.loads(self.call("store", "items", "--open", "--json").stdout), [])
        self.assertFalse((self.home / "docs").exists())
        self.assertFalse((self.home / ".git").exists())

    def test_task_status_refuses_a_vault_row_and_names_the_stage_move(self):
        """sd:3972: a base row moves by stage only; the refusal says so and writes nothing."""
        with database(home=self.home) as connection:
            item = sd_db.create_item(connection, kind="idea", title="Example idea", source="vault",
                                     external_id="topic:System/Databases/Topics/Example idea.md",
                                     stage="active", fields={"kind": "topic"})
        before = json.loads(self.call("task", "show", item, "--json").stdout)
        refused = self.call("task", "status", item, "done", code=1)
        self.assertIn("topic row from the vault", refused.stderr)
        self.assertIn("by stage only", refused.stderr)
        self.assertEqual(json.loads(self.call("task", "show", item, "--json").stdout), before)

    def test_task_status_done_on_a_report_names_the_acknowledge_command(self):
        """sd:5065: a report closes by acknowledgement, so the refusal names that command."""
        with database(home=self.home) as connection:
            item = sd_db.create_item(connection, kind="report", title="Example report")
        refused = self.call("task", "status", item, "done", code=1)
        self.assertIn(f"`sd reports acknowledge {item} --resolve-followups`", refused.stderr)

    def test_an_edit_with_no_field_names_the_edit_command(self):
        """sd:5065: the refusal names the command that fixes it."""
        item = json.loads(self.call("task", "add", "--priority", "3", "A task", "--json").stdout)["item"]["id"]
        refused = self.call("task", "edit", item, code=1)
        self.assertIn(f"`sd task edit {item} --due YYYY-MM-DD`", refused.stderr)

    def test_work_register_is_gone(self):
        """pack sd:7064: nothing files a `work` row; the verb is not offered."""
        refused = self.call("work", "register", "docs/work/x/prd.md", code=2)
        self.assertIn("invalid choice: 'register'", refused.stderr)

    def test_store_items_since_lists_only_rows_created_or_updated_since(self):
        """sd:5065: `--since` takes a date (00:00 UTC) or a duration; a bad value names both forms."""
        old = json.loads(self.call("task", "add", "--priority", "3", "Old", "--json").stdout)["item"]["id"]
        new = json.loads(self.call("task", "add", "--priority", "3", "New", "--json").stdout)["item"]["id"]
        touched = json.loads(self.call("task", "add", "--priority", "3", "Touched", "--json").stdout)["item"]["id"]
        with database(sd_db.default_path(self.home), write=True) as connection:
            connection.execute("UPDATE item SET created_at = '2026-01-01T00:00:00+00:00', "
                               "updated_at = '2026-01-02T00:00:00+00:00' WHERE id = ?", (old,))
            connection.execute("UPDATE item SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (touched,))
            connection.commit()
        for value in ("2d", "36h", "90m", "1w", "2026-01-03"):
            with self.subTest(value=value):
                listed = json.loads(self.call("store", "items", "--since", value, "--json").stdout)
                self.assertEqual(sorted(row["id"] for row in listed), sorted([new, touched]))
        listed = json.loads(self.call("store", "items", "--since", "2026-01-02", "--json").stdout)
        self.assertEqual(sorted(row["id"] for row in listed), sorted([old, new, touched]))
        for value in ("yesterday", "2026-02-30", "5", "-2d", "0d", "99999999999w"):
            with self.subTest(value=value):
                refused = self.call("store", "items", "--since", value, code=2)
                self.assertIn("give YYYY-MM-DD (00:00 UTC) or a duration such as 90m, 6h, 2d or 1w",
                              " ".join(refused.stderr.split()))

    def test_task_show_reads_what_store_item_reads(self):
        """sd:1112. `sd task show` is a read alias; `sd store item` stays canonical.

        Same row, same notes, same revision, in both output forms, and
        reading it changes nothing: the revision after is the one before.
        """

        state = json.loads(self.call("task", "add", "--priority", "3", "Read me", "--json").stdout)
        item = state["item"]["id"]
        self.call("task", "note", item, "--body", "A note")
        canonical = json.loads(self.call("store", "item", item, "--json").stdout)
        alias = json.loads(self.call("task", "show", item, "--json").stdout)
        self.assertEqual(alias, canonical)
        self.assertIn("A note", [note["body"] for note in alias["notes"]])
        self.assertEqual(self.call("task", "show", item).stdout,
                         self.call("store", "item", item).stdout)
        after = json.loads(self.call("store", "item", item, "--json").stdout)
        self.assertEqual(after["revision"], canonical["revision"])
        missing = self.call("task", "show", 999999, code=1)
        self.assertNotIn("Traceback", missing.stderr)

    def shimmed(self, source):
        """An environment whose child runs `source` before `sd` imports anything.

        The pack runs `sd_db` at the pinned system SHA, so a test that needs
        the library with or without an attribute sets it in the child rather
        than depending on which build the pin carries.
        """
        shim = Path(tempfile.mkdtemp(dir=self.home))
        (shim / "sitecustomize.py").write_text(source)
        return {**self.environment,
                "PYTHONPATH": f"{shim}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"}

    # Stands in for the library's `task_guard` and records what `cancel_work`
    # was handed; the real pair lives in the system repository (sd:1005).
    RECORDING_CANCEL = (
        "import json, os\n"
        "import sd_db.progress as progress\n"
        "from sd_db.workflow import item_state\n"
        "def task_guard(connection, row):\n"
        "    return ''\n"
        "def cancel_work(connection, item, **keywords):\n"
        "    guard = keywords.pop('guard', None)\n"
        "    keywords['guard'] = None if guard is None else guard is task_guard\n"
        "    with open(os.environ['SD_TEST_CANCEL'], 'w') as handle:\n"
        "        json.dump({'item': item, **keywords}, handle)\n"
        "    return item_state(connection, item)\n"
        "progress.task_guard = task_guard\n"
        "progress.cancel_work = cancel_work\n")

    def test_task_cancel_hands_cancel_work_the_task_guard(self):
        """sd:1005. A task or followup is cancelled through `cancel_work`.

        The library's own guard admits work rows only, so the verb has to name
        `task_guard`; `sd work cancel` keeps the default and names none.
        """

        environment = self.shimmed(self.RECORDING_CANCEL)
        record = self.home / "cancel.json"
        environment["SD_TEST_CANCEL"] = str(record)
        state = json.loads(self.call("task", "add", "--priority", "3", "Superseded", "--kind", "followup",
                                     "--no-repo", "--json").stdout)
        item = state["item"]["id"]
        for verb, guard in (("task", True), ("work", None)):
            result = subprocess.run(
                [sys.executable, str(ROOT / "bin" / "sd"), verb, "cancel", str(item),
                 "--reason", "superseded by sd:2", "--if-revision", state["revision"]],
                cwd=str(self.home), env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(record.read_text()), {
                "item": item, "reason": "superseded by sd:2", "who": getpass.getuser(),
                "expected_revision": state["revision"], "guard": guard}, verb)

    def test_task_cancel_on_a_library_without_task_guard_refuses_by_name(self):
        """An install older than the guard refuses; it does not cancel or crash.

        Its `cancel_work` takes no `guard`, so calling it anyway would be a
        TypeError from inside an open write, and calling it without one would
        be the work guard refusing a task for the wrong reason.
        """

        environment = self.shimmed(
            "import sd_db.progress as progress\n"
            "progress.__dict__.pop('task_guard', None)\n")
        state = json.loads(self.call("task", "add", "--priority", "3", "Not cancellable here", "--json").stdout)
        item = state["item"]["id"]
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "sd"), "task", "cancel", str(item),
             "--reason", "dropped"],
            cwd=str(self.home), env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("sd_db.progress.task_guard", result.stderr)
        self.assertIn("Update this pack checkout", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        after = json.loads(self.call("store", "item", item, "--json").stdout)
        self.assertEqual((after["item"]["status"], after["revision"]),
                         ("planning", state["revision"]))

    def test_task_cancel_needs_a_reason(self):
        item = json.loads(self.call("task", "add", "--priority", "3", "No reason given", "--json").stdout)["item"]["id"]
        refused = self.call("task", "cancel", item, code=2)
        self.assertIn("--reason", refused.stderr)
        self.assertIn("--reason", self.call("task", "cancel", "--help").stdout)

    def test_cli_and_today_query_agree(self):
        for title in ("One", "Two"):
            state = json.loads(self.call("task", "add", "--priority", "3", title, "--json").stdout)
            self.call("task", "status", state["item"]["id"], "in_progress")
        with database(sd_db.default_path(self.home), write=False) as connection:
            expected = [dict(row) for row in sd_db.reads.today_items(connection)]
        self.assertEqual(json.loads(self.call("today", "--json").stdout), expected)

    def test_note_defaults_to_comment_without_creating_a_followup(self):
        state = json.loads(self.call("task", "add", "--priority", "3", "Parent task", "--json").stdout)
        item = state["item"]["id"]
        result = json.loads(self.call("task", "note", item, "--body", "A useful observation",
                                      "--json").stdout)
        self.assertEqual(result["note"]["kind"], "comment")
        self.assertEqual((result["item"]["kind"], result["item"]["status"]), ("task", "planning"))
        readback = json.loads(self.call("store", "item", item, "--json").stdout)
        self.assertEqual(readback["notes"], result["notes"])
        with database(sd_db.default_path(self.home), write=False) as connection:
            self.assertEqual(sd_db.reads.open_followups(connection), [])
        self.assertNotIn("advisory", self.call("task", "note", item, "--body", "Again").stderr)

    def harness(self, session: str | None = None, agent: str | None = None,
                mark: str | None = None) -> dict[str, str]:
        """This environment with the harness's session id, agent marker and teammate mark set as given,
        else unset."""
        bare = {key: value for key, value in self.environment.items()
                if key not in ("CLAUDE_CODE_SESSION_ID", "AI_AGENT", "SD_AGENT_SESSION")}
        return {**bare, **({"CLAUDE_CODE_SESSION_ID": session} if session else {}),
                **({"AI_AGENT": agent} if agent else {}), **({"SD_AGENT_SESSION": mark} if mark else {})}

    def test_add_and_note_record_the_harness_session_id(self):
        """pack sd:6023: add, note and an answer record `CLAUDE_CODE_SESSION_ID`; unset, the user as before."""
        session = self.harness("session-under-test")
        item = json.loads(self.call("task", "add", "--priority", "3", "Filed by a session", "--json", env=session).stdout)["item"]["id"]
        asked = json.loads(self.call("task", "note", item, "Which?\nOption: Yes (Recommended)\nOption: No", "--kind", "question", "--json",
                                     env=session).stdout)["note"]["id"]
        self.call("task", "note", item, "This one", "--kind", "decision", "--answers", asked, env=session)
        state = json.loads(self.call("task", "note", item, "Plain", "--json", env=self.harness()).stdout)
        self.assertEqual([(note["kind"], note["session"]) for note in state["notes"]],
                         [("status_change", "session-under-test"), ("question", "session-under-test"),
                          ("decision", "session-under-test"), ("comment", getpass.getuser())])

    def test_a_status_move_records_the_harness_session_id(self):
        """pack sd:6046: `sd task status` recorded the user where add and note record the session."""
        item = json.loads(self.call("task", "add", "--priority", "3", "Moved by a session", "--json", env=self.harness()).stdout)
        state = json.loads(self.call("task", "status", item["item"]["id"], "in_progress", "--json",
                                     env=self.harness("session-under-test")).stdout)
        self.assertEqual([(note["body"].split(" by ")[0], note["session"]) for note in state["notes"]],
                         [("opened as planning", getpass.getuser()), ("planning -> in_progress", "session-under-test")])

    def test_a_note_under_ai_agent_records_the_plain_session_id(self):
        """pack sd:6046: Claude Code sets `AI_AGENT` in the lead's Bash as in a teammate's, so it tells
        neither apart; a mark from it hid the lead's own notes from its trail."""
        lead = self.harness("session-under-test", "claude-code_2-1-303_agent")
        item = json.loads(self.call("task", "add", "--priority", "3", "Filed by the lead", "--json", env=lead).stdout)["item"]["id"]
        state = json.loads(self.call("task", "note", item, "Lead ruling", "--kind", "decision", "--json",
                                     env=lead).stdout)
        self.assertEqual([note["session"] for note in state["notes"]], ["session-under-test", "session-under-test"])

    def test_a_teammate_records_its_mark_and_a_mark_from_another_session_marks_nothing(self):
        """pack sd:6046: a teammate shares its lead's session id; `bin/sd-session-beat` exports
        `SD_AGENT_SESSION=<session>:<agent>` into its Bash call, and add, note and status record it."""
        mate = self.harness("session-under-test", mark="session-under-test:agent-1")
        item = json.loads(self.call("task", "add", "--priority", "3", "Filed by a teammate", "--json", env=mate).stdout)["item"]["id"]
        self.call("task", "note", item, "Teammate ruling", "--kind", "decision", env=mate)
        self.call("task", "status", item, "in_progress", env=mate)
        stray = self.harness("session-under-test", mark="session-other:agent-1")
        state = json.loads(self.call("task", "note", item, "Lead note", "--json", env=stray).stdout)
        self.assertEqual([note["session"] for note in state["notes"]],
                         ["session-under-test:agent-1"] * 3 + ["session-under-test"])

    def test_the_beat_hooks_rewrite_runs_sd_under_the_teammates_mark(self):
        """pack sd:6046: the command `bin/sd-session-beat` hands back, run by a shell as Claude Code
        runs it, records the teammate's mark; the lead's call comes back unchanged."""
        item = json.loads(self.call("task", "add", "--priority", "3", "Shared", "--json", env=self.harness()).stdout)["item"]["id"]
        command = f"{shlex.quote(sys.executable)} {ROOT / 'bin' / 'sd'} task note {item} Marked --json"

        def rewritten(payload: dict) -> str:
            done = subprocess.run([sys.executable, str(ROOT / "bin" / "sd-session-beat")], check=False,
                                  input=json.dumps({"hook_event_name": "PreToolUse", "session_id": "session-under-test",
                                                    "tool_name": "Bash", "tool_input": {"command": command},
                                                    **payload}),
                                  capture_output=True, text=True, env=clean_environment(HOME=str(self.home)))
            self.assertEqual((done.returncode, done.stderr), (0, ""))
            return json.loads(done.stdout)["hookSpecificOutput"]["updatedInput"]["command"] if done.stdout else ""

        self.assertEqual(rewritten({}), "")
        ran = subprocess.run(["/bin/sh", "-c", rewritten({"agent_id": "agent-1"})], capture_output=True, text=True,
                             env=clean_environment(HOME=str(self.home), CLAUDE_CODE_SESSION_ID="session-under-test"),
                             check=True)
        self.assertEqual(json.loads(ran.stdout)["note"]["session"], "session-under-test:agent-1")

    def done_item(self, *merge_note):
        """A task closed by hand, optionally carrying a merge comment first."""
        item = json.loads(self.call("task", "add", "--priority", "3", "Delivered twice", "--json").stdout)["item"]["id"]
        if merge_note:
            self.call("task", "note", item, "--body", merge_note[0])
        self.call("task", "status", item, "done")
        return item

    def test_a_note_on_a_done_item_names_its_delivery_and_still_lands(self):
        """sd:1317: two lanes worked one item, and the second lane's note on the
        already-done row went in without remark. The note still lands."""
        sha = "a" * 40
        item = self.done_item(
            f"Code delivery https://github.com/example/repo/pull/336 at {sha}\n{{}}")
        since = json.loads(self.call("store", "item", item, "--json").stdout)["item"]["status_since"]
        result = self.call("task", "note", item, "--kind", "decision",
                           "--body", "Late decision", "--json")
        self.assertRegex(result.stderr, rf"sd: advisory: \S+ sd:{item} is done since {since[:10]}, delivered by #336")
        self.assertEqual("Late decision", json.loads(result.stdout)["note"]["body"])

    def test_a_done_item_without_a_merge_comment_says_none_is_recorded(self):
        item = self.done_item()
        result = self.call("task", "note", item, "--body", "Late note")
        self.assertRegex(result.stderr, rf"sd: advisory: \S+ sd:{item} is done since")
        self.assertIn("no delivery is recorded", result.stderr)
        notes = json.loads(self.call("store", "item", item, "--json").stdout)["notes"]
        self.assertEqual("Late note", notes[-1]["body"])

    def test_a_delivery_with_no_pull_request_names_its_commit(self):
        """The transition sentence `--delivered-by` and `sd work deliver` write."""
        sha = "b" * 40
        item = json.loads(self.call("task", "add", "--priority", "3", "Delivered", "--json").stdout)["item"]["id"]
        with database(sd_db.default_path(self.home), write=True) as connection:
            workflow.change_status(connection, item, "done", who="fixture",
                                   reason=f"delivered at {sha} on origin/main")
        result = self.call("task", "note", item, "--body", "Late note")
        self.assertIn(f"delivered by commit {sha[:12]}", result.stderr)

    def question(self, item, body="Which store?\nOption: Pack store (Recommended)\nOption: Database rows"):
        return json.loads(self.call("task", "note", item, body, "--kind", "question", "--json").stdout)["note"]["id"]

    def test_answers_records_the_decision_and_resolves_the_question_in_one_write(self):
        """sd:5014: a decision note left its question in Decisions waiting until a second verb resolved it."""
        item = json.loads(self.call("task", "add", "--priority", "3", "Asks", "--json").stdout)["item"]["id"]
        asked = self.question(item)
        state = json.loads(self.call("task", "note", item, "Database rows", "--kind", "decision",
                                     "--answers", asked, "--json").stdout)
        self.assertEqual((state["note"]["kind"], state["note"]["body"]),
                         ("decision", f"Ruling on note #{asked}: Database rows"))
        self.assertTrue(next(note for note in state["notes"] if note["id"] == asked)["resolved_at"])
        with database(sd_db.default_path(self.home), write=False) as connection:
            self.assertEqual([row["id"] for row in sd_db.reads.open_notes(connection, "question")], [])
        self.assertNotIn("Decisions waiting", self.call("today").stdout)

    def test_answers_refuses_and_writes_nothing(self):
        item = json.loads(self.call("task", "add", "--priority", "3", "Asks", "--json").stdout)["item"]["id"]
        other = json.loads(self.call("task", "add", "--priority", "3", "Asks too", "--json").stdout)["item"]["id"]
        asked, theirs = self.question(item), self.question(other)
        comment = json.loads(self.call("task", "note", item, "Plain", "--json").stdout)["note"]["id"]
        settled = self.question(item, "Settled?\nOption: Yes (Recommended)\nOption: No")
        self.call("note", "resolve", settled)
        for name, flags, said in (
                ("no decision kind", ("--answers", asked), "--kind decision"),
                ("not a question", ("--kind", "decision", "--answers", comment), "is a comment, not a question"),
                ("another item", ("--kind", "decision", "--answers", theirs), f"belongs to sd:{other}"),
                ("resolved", ("--kind", "decision", "--answers", settled), "already resolved"),
                ("missing", ("--kind", "decision", "--answers", 999999), "no note 999999")):
            with self.subTest(name):
                before = [json.loads(self.call("task", "show", row, "--json").stdout) for row in (item, other)]
                refused = self.call("task", "note", item, "Answer", *flags, code=1)
                self.assertIn(said, refused.stderr)
                self.assertEqual([json.loads(self.call("task", "show", row, "--json").stdout)
                                  for row in (item, other)], before)

    def test_answers_writes_both_or_neither(self):
        """A resolve that fails after the ruling is written leaves no ruling behind."""
        item = json.loads(self.call("task", "add", "--priority", "3", "Asks", "--json").stdout)["item"]["id"]
        asked = self.question(item)
        before = json.loads(self.call("task", "show", item, "--json").stdout)
        args = argparse.Namespace(item=item, kind="decision", answers=asked)
        with database(sd_db.default_path(self.home), write=True) as connection, \
                unittest.mock.patch.object(workflow, "resolve_item_note", side_effect=workflow.WorkflowError("cut")):
            with self.assertRaises(workflow.WorkflowError):
                sd_work._answer(workflow, connection, args, "Pack store", "tester", None)
        self.assertEqual(json.loads(self.call("task", "show", item, "--json").stdout), before)

    def batch_question(self):
        """A filed review batch with one Approve-or-Reject question: the batch and the question's note."""
        import sd_db.review as review

        with database(home=self.home) as connection:
            item = review.file(connection, {
                "schema": 1, "queue": "nightly", "batch": "2026-10-18", "title": "Nightly review 2026-10-18",
                "source": "system@6c4317b", "mode": "open",
                "questions": [{"subject": "cap-retries", "text": "Suggestion: cap retries.",
                               "options": ["Approve", "Reject"]}]}, who="fixture")["item"]
            connection.commit()
            return item, review.read(connection, item)["questions"][0]["note"]

    def test_option_rules_a_review_batch_question_and_approve_files_a_task(self):
        """sd:5065: `--option` rules a batch question from the CLI, as `review.rule` does."""
        item, asked = self.batch_question()
        state = json.loads(self.call("task", "note", item, "Worth a try", "--kind", "decision",
                                     "--answers", asked, "--option", "approve", "--json").stdout)
        self.assertRegex(state["note"]["body"], rf"^Ruling on note #{asked}: Approve -> sd:\d+$")
        self.assertTrue(next(note for note in state["notes"] if note["id"] == asked)["resolved_at"])
        self.assertIn(f"On note #{asked}: Worth a try", [note["body"] for note in state["notes"]])
        filed = int(state["note"]["body"].rsplit("sd:", 1)[1])
        self.assertEqual(json.loads(self.call("task", "show", filed, "--json").stdout)["item"]["kind"], "task")

    def test_an_approval_task_records_the_session_and_the_ruling_the_user(self):
        """pack sd:6046: the task an approval files is the session's, so its trail lists it; the ruling
        and its comment keep the user, because the Review page shows their session as "ruled by"."""
        item, asked = self.batch_question()
        state = json.loads(self.call("task", "note", item, "Worth a try", "--kind", "decision", "--answers", asked,
                                     "--option", "approve", "--json", env=self.harness("session-under-test")).stdout)
        filed = json.loads(self.call("task", "show", int(state["note"]["body"].rsplit("sd:", 1)[1]), "--json").stdout)
        self.assertEqual([note["session"] for note in filed["notes"]], ["session-under-test"])
        self.assertEqual({note["session"] for note in state["notes"] if note["id"] > asked}, {getpass.getuser()})

    def test_a_cli_ruling_records_its_harness_session_and_shows_in_that_sessions_trail(self):
        """pack sd:6046: a CLI ruling keeps the user as "ruled by"; its session went nowhere, so the session's
        compact trail missed its own ruling. `harness_session` keeps it, a teammate's mark included."""
        import sd_db.review as review

        with database(home=self.home) as connection:
            item = review.file(connection, {
                "schema": 1, "queue": "nightly", "batch": "2026-10-18", "title": "Nightly review 2026-10-18",
                "source": "system@6c4317b", "mode": "open",
                "questions": [{"subject": f"cap-{number}", "text": f"Suggestion: cap {number}.",
                               "options": ["Approve", "Reject"]} for number in range(3)]}, who="fixture")["item"]
            connection.commit()
            questions = [question["note"] for question in review.read(connection, item)["questions"]]
        ruled = {}
        for asked, (env, session) in zip(questions, (
                (self.harness("session-under-test"), "session-under-test"),
                (self.harness("session-under-test", mark="session-under-test:agent-1"), "session-under-test:agent-1"),
                (self.harness(), None)), strict=True):
            state = json.loads(self.call("task", "note", item, "Worth a try", "--kind", "decision", "--answers",
                                         asked, "--option", "reject", "--json", env=env).stdout)
            mine = [note for note in state["notes"] if note["id"] > max(*questions, *ruled.values())]
            self.assertEqual([(note["kind"], note["session"], note["harness_session"]) for note in mine],
                             [("comment", getpass.getuser(), session), ("decision", getpass.getuser(), session)])
            ruled[session] = state["note"]["id"]
        with database(sd_db.default_path(self.home), write=False) as connection:
            for session in ("session-under-test", "session-under-test:agent-1"):
                self.assertEqual([row["id"] for row in sd_db.reads.session_trail(connection, session, limit=10)],
                                 [ruled[session]])

    def test_option_refuses_an_unknown_label_or_a_plain_question_and_writes_nothing(self):
        """sd:5065: an unknown label names the valid ones; `--option` needs `--answers` on a batch question."""
        item, asked = self.batch_question()
        plain = json.loads(self.call("task", "add", "--priority", "3", "Asks", "--json").stdout)["item"]["id"]
        question = self.question(plain)
        for name, row, flags, said in (
                ("unknown label", item, ("--answers", asked, "--option", "Maybe"), "its options are Approve, Reject"),
                ("no --answers", item, ("--option", "Approve"), "--option needs --answers"),
                ("not a batch", plain, ("--answers", question, "--option", "Approve"), "not a review batch")):
            with self.subTest(name):
                before = json.loads(self.call("task", "show", row, "--json").stdout)
                refused = self.call("task", "note", row, "Answer", "--kind", "decision", *flags, code=1)
                self.assertIn(said, refused.stderr)
                self.assertEqual(json.loads(self.call("task", "show", row, "--json").stdout), before)

    def test_option_writes_the_comment_and_the_ruling_or_neither(self):
        """sd:5065: a ruling refused after the comment is written leaves no comment behind."""
        import sd_db.review as review

        item, asked = self.batch_question()
        before = json.loads(self.call("task", "show", item, "--json").stdout)
        args = argparse.Namespace(item=item, kind="decision", answers=asked, option="Reject")
        with database(sd_db.default_path(self.home), write=True) as connection, \
                unittest.mock.patch.object(review, "rule", side_effect=review.ReviewError("cut")):
            with self.assertRaises(review.ReviewError):
                sd_work._answer(workflow, connection, args, "Not now", "tester", None)
        self.assertEqual(json.loads(self.call("task", "show", item, "--json").stdout), before)

    def test_answers_on_a_batch_question_without_option_names_the_option_to_give(self):
        """sd:5065: the refusal on sd:5038 was a dead end; it now names `--option` and the labels."""
        item, asked = self.batch_question()
        refused = self.call("task", "note", item, "Approve", "--kind", "decision", "--answers", asked, code=1)
        self.assertIn("--option LABEL, one of Approve, Reject", refused.stderr)

    def test_answers_refuses_a_review_batch_question(self):
        """A batch question is Review's: its ruling may file a task, which `review.rule` writes and a note does not."""
        with database(home=self.home) as connection:
            item = sd_db.create_item(connection, kind="task", title="Batch", source="task-ref",
                                     external_id="review:example:1")
        asked = self.question(item)
        before = json.loads(self.call("task", "show", item, "--json").stdout)
        refused = self.call("task", "note", item, "Pack store", "--kind", "decision", "--answers", asked, code=1)
        self.assertIn("review batch", refused.stderr)
        self.assertEqual(json.loads(self.call("task", "show", item, "--json").stdout), before)

    def test_all_public_note_kinds_parse_and_persist_without_internal_kinds(self):
        self.assertEqual(set(NOTE_KINDS), {"comment", "followup", "question", "decision", "proposal"})
        state = json.loads(self.call("task", "add", "--priority", "3", "Parent task", "--json").stdout)
        item = state["item"]["id"]
        initial_notes = state["notes"]
        initial_ids = {note["id"] for note in initial_notes}
        # pack sd:6708: a question carries its options and a pick, or the CLI refuses it and writes nothing.
        refused = self.call("task", "note", item, "--kind", "question", "--body", "Recorded question", code=1)
        self.assertIn("two or more `Option:` lines and one recommended", refused.stderr)
        for kind in NOTE_KINDS:
            with self.subTest(kind=kind):
                body = f"Recorded {kind}" + ("\nOption: Yes (Recommended)\nOption: No" if kind == "question" else "")
                state = json.loads(self.call("task", "note", item, "--kind", kind,
                    "--body", body, "--if-revision", state["revision"], "--json").stdout)
                self.assertEqual(state["note"]["kind"], kind)
                self.assertEqual(state["note"]["body"], body)
        before = json.loads(self.call("store", "item", item, "--json").stdout)
        self.assertEqual([note["kind"] for note in before["notes"] if note["id"] not in initial_ids], list(NOTE_KINDS))
        self.assertEqual([note for note in before["notes"] if note["id"] in initial_ids], initial_notes)
        self.call("task", "note", item, "--body", "Forged history", "--kind", "status_change", code=2)
        self.assertEqual(json.loads(self.call("store", "item", item, "--json").stdout), before)
        help_text = self.call("task", "note", "--help").stdout
        self.assertIn("{" + ",".join(NOTE_KINDS) + "}", help_text)
        self.assertIn("default: comment", help_text)

    def test_task_add_files_every_hand_filed_kind_and_no_produced_one(self):
        """sd:730. Four item kinds shipped in migration 009 with no producer.

        `--kind` is the producer, and it is deliberately narrower than the
        CHECK: the six kinds left out each have a maker of their own, and
        `idea` is the sharp one -- it is a draft article, selected as
        `kind = 'idea' AND piece IS NOT NULL`, so filing one here would write
        a row the publishing queue cannot see.
        """
        self.assertEqual(workflow.HAND_KINDS,
                         ("task", "followup", "work-idea", "personal-idea"))
        default = json.loads(self.call("task", "add", "--priority", "3", "Standalone work", "--json").stdout)
        self.assertEqual(default["item"]["kind"], "task")

        filed = {}
        for kind in workflow.HAND_KINDS:
            with self.subTest(kind=kind):
                state = json.loads(
                    self.call("task", "add", "--priority", "3", f"A {kind}", "--kind", kind, "--json").stdout)
                self.assertEqual(state["item"]["kind"], kind)
                readback = json.loads(
                    self.call("store", "item", state["item"]["id"], "--json").stdout)
                self.assertEqual(readback["item"]["kind"], kind)
                self.assertEqual(readback["revision"], state["revision"])
                filed[kind] = state["item"]["id"]

        # `personal` and `skill-review` left item.kind (sd:7004).
        for kind in ("work", "report", "dep", "skill-review", "personal", "proposal", "idea", "nonsense"):
            with self.subTest(refused=kind):
                refused = self.call("task", "add", "--priority", "3", f"Not a {kind}", "--kind", kind, code=2)
                self.assertIn("invalid choice", refused.stderr)

        rows = json.loads(self.call("store", "items", "--json").stdout)
        self.assertEqual(sorted(row["id"] for row in rows),
                         sorted([default["item"]["id"], *filed.values()]))

    def test_a_repo_less_kind_is_filed_off_the_checkout_it_was_typed_in(self):
        """`work-idea` and `personal-idea` carry no repository.

        The group is read by asking for exactly that -- `backlog_items(repo=
        NO_REPO)` -- so an idea row that took the checkout cwd happened
        to be in would be absent from the one query written to list it.
        `--no-repo` is implied and not required; `--here` asks for the
        repository the kind cannot have and is refused rather than ignored.
        """
        root = self._checkout("project")
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, root, home=self.home)

        ordinary = json.loads(self.call("task", "add", "--priority", "3", "Ordinary", "--json", cwd=root).stdout)
        self.assertEqual(ordinary["item"]["repo"], sd_lib.stored_repo(root))

        for kind in sorted(workflow.REPO_LESS_KINDS):
            with self.subTest(kind=kind):
                filed = json.loads(
                    self.call("task", "add", "--priority", "3", f"A {kind}", "--kind", kind, "--json",
                              cwd=root).stdout)
                self.assertIsNone(filed["item"]["repo"])
                refused = self.call("task", "add", "--priority", "3", "Insisting", "--kind", kind, "--here",
                                    cwd=root, code=1)
                self.assertIn(f"a {kind} item carries no repository", refused.stderr)

        redundant = json.loads(
            self.call("task", "add", "--priority", "3", "Said twice", "--kind", "work-idea", "--no-repo",
                      "--json", cwd=root).stdout)
        self.assertIsNone(redundant["item"]["repo"])

        with database(sd_db.default_path(self.home), write=False) as connection:
            grouped = sorted(row["kind"] for row in
                             sd_db.reads.backlog_items(connection, repo=sd_db.reads.NO_REPO))
        # sd:809: `followup` left the set, so the loop above files none; sd:7004 took `personal`.
        self.assertEqual(sorted(workflow.REPO_LESS_KINDS),
                         ["personal-idea", "work-idea"])
        self.assertEqual(grouped, ["personal-idea", "work-idea", "work-idea"])
        self.assertEqual(
            [row["kind"] for row in
             json.loads(self.call("store", "items", "--kind", "task", "--json").stdout)],
            ["task"])

    def test_a_followup_takes_the_repository_it_was_filed_from(self):
        """sd:809. A followup is filed by the rule a task is filed by.

        A followup from a code review is about the checkout it was typed in,
        and with no repository a checkout's brief never lists it. So the
        registered checkout enclosing cwd is the default, a linked worktree
        resolves to its main checkout, an unregistered checkout falls back to
        none, `--no-repo` opts out and `--here` refuses only where a task's
        would.
        """
        root = self._checkout("reviewed")
        unregistered = json.loads(self.call(
            "task", "add", "--priority", "3", "Before registering", "--kind", "followup", "--json",
            cwd=root).stdout)
        self.assertIsNone(unregistered["item"]["repo"])
        self.assertIn("not a registered repository", self.call(
            "task", "add", "--priority", "3", "Insisting", "--kind", "followup", "--here", cwd=root,
            code=1).stderr)

        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, root, home=self.home)

        filed = json.loads(self.call(
            "task", "add", "--priority", "3", "Review finding", "--kind", "followup", "--json", cwd=root).stdout)
        self.assertEqual((filed["item"]["kind"], filed["item"]["repo"]),
                         ("followup", sd_lib.stored_repo(root)))
        here = json.loads(self.call(
            "task", "add", "--priority", "3", "Said here", "--kind", "followup", "--here", "--json",
            cwd=root).stdout)
        self.assertEqual(here["item"]["repo"], sd_lib.stored_repo(root))

        linked = self.home / "reviewed-linked"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "side", str(linked)],
                       cwd=str(root), check=True, capture_output=True, text=True)
        from_worktree = json.loads(self.call(
            "task", "add", "--priority", "3", "From a worktree", "--kind", "followup", "--json",
            cwd=linked).stdout)
        self.assertEqual(from_worktree["item"]["repo"], sd_lib.stored_repo(root))

        opted_out = json.loads(self.call(
            "task", "add", "--priority", "3", "Nowhere", "--kind", "followup", "--no-repo", "--json",
            cwd=root).stdout)
        self.assertIsNone(opted_out["item"]["repo"])
        outside = json.loads(self.call(
            "task", "add", "--priority", "3", "Outside", "--kind", "followup", "--json").stdout)
        self.assertIsNone(outside["item"]["repo"])

        with database(sd_db.default_path(self.home), write=False) as connection:
            in_repo = sorted(row["id"] for row in
                             sd_db.reads.backlog_items(connection, repo=str(root.resolve())))
            unscoped = sorted(row["id"] for row in
                              sd_db.reads.backlog_items(connection, repo=sd_db.reads.NO_REPO))
        self.assertEqual(in_repo, sorted([filed["item"]["id"], here["item"]["id"],
                                          from_worktree["item"]["id"]]))
        self.assertEqual(unscoped, sorted([unregistered["item"]["id"],
                                           opted_out["item"]["id"], outside["item"]["id"]]))

    def test_edit_clears_a_stale_branch(self):
        """sd:2729. `--clear-branch` is the one way a stale branch name leaves a row."""
        state = json.loads(self.call("task", "add", "--priority", "3", "Worked on a branch", "--json").stdout)
        item = state["item"]["id"]
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.writes.set_item_fields(connection, item, branch="feat/gone")
        before = json.loads(self.call("store", "item", item, "--json").stdout)
        refused = self.call("task", "edit", item, "--clear-branch",
                            "--if-revision", state["revision"], code=1)
        self.assertIn("changed", refused.stderr)
        cleared = json.loads(self.call(
            "task", "edit", item, "--clear-branch",
            "--if-revision", before["revision"], "--json").stdout)
        self.assertIsNone(cleared["item"]["branch"])
        self.assertEqual(cleared["notes"][-1]["body"], f"Updated branch by {getpass.getuser()}")

    def test_edit_branch_sets_a_branch_and_clear_branch_refuses_in_progress(self):
        """sd:7659. `--branch` re-sets one; a clear never strands an in_progress row."""
        state = json.loads(self.call("task", "add", "--priority", "3", "Branch to set", "--json").stdout)
        item = state["item"]["id"]
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.writes.set_item_fields(connection, item, branch="feat/old")
            sd_db.writes.transition(connection, item, "in_progress", who="test", reason="work")
        refused = self.call("task", "edit", item, "--clear-branch", code=1)
        self.assertIn("in_progress item records its branch", refused.stderr)
        set_to = json.loads(self.call("task", "edit", item, "--branch", "feat/new", "--json").stdout)
        self.assertEqual(set_to["item"]["branch"], "feat/new")
        self.call("task", "edit", item, "--branch", "x", "--clear-branch", code=2)

    def test_edit_area_sets_and_clears_the_items_own_area_and_refuses_another(self):
        """pack sd:8010. `--area home|hoa` keeps the area in `fields`; `--clear-area` drops it."""
        state = json.loads(self.call("task", "add", "--priority", "3", "Clean the gutters", "--json").stdout)
        item = state["item"]["id"]
        refused = self.call("task", "edit", item, "--area", "work", code=2)
        self.assertIn("invalid choice: 'work'", refused.stderr)
        self.call("task", "edit", item, "--area", "home", "--clear-area", code=2)
        stale = self.call("task", "edit", item, "--title", "Gutters", "--json")
        self.assertNotEqual(json.loads(stale.stdout)["revision"], state["revision"])
        refused = self.call("task", "edit", item, "--area", "home", "--if-revision", state["revision"], code=1)
        self.assertIn("changed", refused.stderr)
        tagged = json.loads(self.call("task", "edit", item, "--area", "home", "--json").stdout)
        self.assertEqual(json.loads(tagged["item"]["fields"]), {"area": "home"})
        self.assertEqual(tagged["notes"][-1]["body"], f"Updated area by {getpass.getuser()}")
        self.assertIn("  area: home\n", self.call("task", "show", item, "--audit").stdout)
        cleared = json.loads(self.call("task", "edit", item, "--clear-area", "--json").stdout)
        self.assertIsNone(cleared["item"]["fields"])
        self.assertNotIn("area:", self.call("task", "show", item).stdout)

    # Stands in for the library's `edit_item` and records what it was handed:
    # the pin may predate the unpark (sd:3007), and the library's own tests
    # cover what it does with `parked_at`.
    RECORDING_EDIT = (
        "import json, os\n"
        "import sd_db.workflow as workflow\n"
        "def edit_item(connection, item, changes, **keywords):\n"
        "    with open(os.environ['SD_TEST_EDIT'], 'w') as handle:\n"
        "        json.dump({'item': item, 'changes': changes, **keywords}, handle)\n"
        "    return workflow.item_state(connection, item)\n"
        "workflow.edit_item = edit_item\n")

    def test_edit_unpark_hands_edit_item_a_parked_at_clear_and_the_revision(self):
        """sd:3007. `--unpark` clears `parked_at`, checked against `--if-revision`."""
        environment = self.shimmed(self.RECORDING_EDIT)
        record = self.home / "edit.json"
        environment["SD_TEST_EDIT"] = str(record)
        state = json.loads(self.call("task", "add", "--priority", "3", "Parked at P4", "--json").stdout)
        item = state["item"]["id"]
        self.call("task", "edit", item, "--unpark", "--if-revision", state["revision"], env=environment)
        self.assertEqual(json.loads(record.read_text()), {
            "item": item, "changes": {"parked_at": None}, "who": getpass.getuser(),
            "expected_revision": state["revision"]})
        self.assertIn("--unpark", self.call("task", "edit", "--help").stdout)

    def test_edit_park_parks_once_and_refuses_unpark_and_a_done_item(self):
        """sd:5905. `--park` stamps `parked_at` now, the reverse of `--unpark`."""
        item = json.loads(self.call("task", "add", "--priority", "3", "Not now", "--json").stdout)["item"]["id"]
        parked = json.loads(self.call("task", "edit", item, "--park", "--json").stdout)
        self.assertRegex(parked["item"]["parked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertEqual(parked["notes"][-1]["body"], f"Parked by {getpass.getuser()}")
        again = self.call("task", "edit", item, "--park").stdout
        self.assertIn(f"already parked since {parked['item']['parked_at']}; not parked again", again)
        self.assertEqual(json.loads(self.call("task", "show", item, "--json").stdout)["revision"],
                         parked["revision"])
        self.assertIn("not allowed with argument",
                      self.call("task", "edit", item, "--park", "--unpark", code=2).stderr)
        done = json.loads(self.call("task", "add", "--priority", "3", "Shipped", "--json").stdout)["item"]["id"]
        self.call("task", "status", done, "done")
        self.assertIn("is done", self.call("task", "edit", done, "--park", code=1).stderr)
        self.assertIn("--park", self.call("task", "edit", "--help").stdout)

    def test_edit_moves_a_followup_and_changes_its_details(self):
        """sd:809. `edit_item` edits a followup's title, body, priority, due
        date and repository, as it does a task's. Before sd:809 it refused
        every field but `kind`."""
        first, second = self._checkout("first"), self._checkout("second")
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, first, home=self.home)
            sd_db.repos.add(connection, second, home=self.home)
        state = json.loads(self.call(
            "task", "add", "--priority", "3", "Filed off its checkout", "--kind", "followup", "--json").stdout)
        item = state["item"]["id"]
        self.assertIsNone(state["item"]["repo"])

        moved = json.loads(self.call(
            "task", "edit", item, "--belongs-to", first, "--title", "Fix the finding",
            "--priority", 2, "--due", "2026-09-20", "--if-revision", state["revision"],
            "--json").stdout)
        row = moved["item"]
        self.assertEqual((row["kind"], row["repo"], row["title"], row["priority"], row["due"]),
                         ("followup", sd_lib.stored_repo(first), "Fix the finding", 2, "2026-09-20"))
        self.assertEqual(moved["notes"][-1]["body"],
                         f"Updated due, priority, repo, title by {getpass.getuser()}")

        again = json.loads(self.call(
            "task", "edit", item, "--belongs-to", ".", "--json", cwd=second).stdout)
        self.assertEqual(again["item"]["repo"], sd_lib.stored_repo(second))

        bodied = json.loads(self.call(
            "task", "edit", item, "--body", "The finding is at line 12", "--json").stdout)
        self.assertEqual(json.loads(bodied["item"]["body"]), {"text": "The finding is at line 12"})
        self.assertEqual(bodied["notes"][-1]["body"], f"Updated body by {getpass.getuser()}")

        # Leaving for a repository-less kind still needs the repository cleared.
        refused = self.call("task", "edit", item, "--kind", "work-idea", code=1)
        self.assertIn("a work-idea item carries no repository", refused.stderr)
        self.assertEqual(json.loads(self.call("store", "item", item, "--json").stdout), bodied)

        cleared = json.loads(self.call("task", "edit", item, "--no-repo", "--json").stdout)
        self.assertIsNone(cleared["item"]["repo"])
        self.assertEqual(cleared["item"]["kind"], "followup")

    def test_help_names_the_repository_less_kinds_the_library_holds(self):
        """sd:809. The help said "the four others"; it names the three.

        The names are spelled in the help because the parser may not import
        the library to read them, so this reads them from the library and
        fails the day the two disagree.
        """
        for verb in ("add", "edit"):
            with self.subTest(verb=verb):
                text = " ".join(self.call("task", verb, "--help").stdout.split())
                text = text.replace("- ", "-")
                for kind in sorted(workflow.REPO_LESS_KINDS):
                    self.assertIn(kind, text)
                self.assertNotIn("four", text)
        add = " ".join(self.call("task", "add", "--priority", "3", "--help").stdout.split()).replace("- ", "-")
        self.assertIn("a followup takes a checkout as a task does", add)
        self.assertIn("and work-idea and personal-idea carry no repository", add)

    def _checkout(self, name):
        """A real checkout, because the repository is resolved by asking git."""
        root = self.home / name
        root.mkdir()
        def run(*args):
            return subprocess.run(["git", *args], cwd=str(root), check=True,
                                  capture_output=True, text=True)

        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "T")
        (root / "README.md").write_text("x\n")
        run("add", "README.md")
        run("commit", "-qm", "first")
        return root

    def test_a_new_task_takes_the_repository_it_was_filed_from(self):
        """The default is cwd's registered checkout, and never a refusal.

        Two cases carry the design. A linked worktree resolves to its main
        checkout, because `--show-toplevel` names the temporary worktree,
        which is deleted long before anyone reads the row back. An
        unregistered checkout falls back to no repository: `capture_task`
        rejects an unregistered path, so a default that passed cwd through
        would make the verb fail everywhere it used to work.
        """
        root = self._checkout("project")

        unregistered = json.loads(
            self.call("task", "add", "--priority", "3", "Before registering", "--json", cwd=root).stdout)
        self.assertIsNone(unregistered["item"]["repo"])
        self.assertIn("not a registered repository",
                      self.call("task", "add", "--priority", "3", "Insisting", "--here", cwd=root, code=1).stderr)

        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, root, home=self.home)

        filed = json.loads(self.call("task", "add", "--priority", "3", "Inside", "--json", cwd=root).stdout)
        self.assertEqual(filed["item"]["repo"], sd_lib.stored_repo(root))

        linked = self.home / "linked"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "side", str(linked)],
                       cwd=str(root), check=True, capture_output=True, text=True)
        from_worktree = json.loads(
            self.call("task", "add", "--priority", "3", "From a worktree", "--json", cwd=linked).stdout)
        self.assertEqual(from_worktree["item"]["repo"], sd_lib.stored_repo(root))

        opted_out = json.loads(
            self.call("task", "add", "--priority", "3", "Neither", "--no-repo", "--json", cwd=root).stdout)
        self.assertIsNone(opted_out["item"]["repo"])

        outside = json.loads(self.call("task", "add", "--priority", "3", "Outside", "--json").stdout)
        self.assertIsNone(outside["item"]["repo"])

        self.call("task", "add", "--priority", "3", "Both", "--here", "--no-repo", cwd=root, code=2)

    def test_store_items_here_lists_only_this_checkouts_items(self):
        """sd:3197. `--here` resolves the checkout as `task add --here` does, and refuses as it does."""
        root, other = self._checkout("listed"), self._checkout("elsewhere")
        self.assertIn("not a registered repository",
                      self.call("store", "items", "--here", cwd=root, code=1).stderr)
        self.assertIn("Git checkout", self.call("store", "items", "--here", code=1).stderr)
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, root, home=self.home)
            sd_db.repos.add(connection, other, home=self.home)
        self.call("task", "add", "--priority", "3", "Mine", cwd=root)
        self.call("task", "add", "--priority", "3", "Theirs", cwd=other)
        self.call("task", "add", "--priority", "3", "Nobody's", "--no-repo", cwd=root)
        done = json.loads(self.call("task", "add", "--priority", "3", "Mine, done", "--json", cwd=root).stdout)["item"]["id"]
        self.call("task", "status", done, "done")
        linked = self.home / "listed-linked"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "side", str(linked)],
                       cwd=str(root), check=True, capture_output=True, text=True, env=clean_environment())
        for cwd in (root, linked):
            with self.subTest(cwd=cwd.name):
                listed = json.loads(self.call("store", "items", "--open", "--here", "--json", cwd=cwd).stdout)
                self.assertEqual([row["title"] for row in listed], ["Mine"])

    def test_a_filed_task_can_be_moved_between_repositories_and_off_them(self):
        """The move a hand-written `UPDATE item.repo` used to be (sd:507, sd:452).

        `repo` was the one capture-time field `edit` could not change, so a row
        filed from the wrong directory stayed mis-attributed. The rule it has
        to obey is the same one `add` obeys -- the `repo` table decides what a
        repository is -- and the note the library already writes for every
        edited field is what records the move, so nothing here re-implements
        either half.
        """
        first, second = self._checkout("first"), self._checkout("second")
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, first, home=self.home)
            sd_db.repos.add(connection, second, home=self.home)

        state = json.loads(self.call("task", "add", "--priority", "3", "Filed nowhere", "--json").stdout)
        item = state["item"]["id"]
        self.assertIsNone(state["item"]["repo"])

        moved = json.loads(self.call("task", "edit", item, "--belongs-to", first, "--json").stdout)
        self.assertEqual(moved["item"]["repo"], sd_lib.stored_repo(first))

        # The path is read the way `add` reads cwd, so `.` inside a checkout
        # names that checkout rather than a directory the `repo` table has
        # never heard of. The flag is `--belongs-to` and not `--repo` because
        # R10-D6 refuses that option name anywhere under `bin/`, and `add`
        # answered the same question without it one verb earlier.
        again = json.loads(
            self.call("task", "edit", item, "--belongs-to", ".", "--json", cwd=second).stdout)
        self.assertEqual(again["item"]["repo"], sd_lib.stored_repo(second))

        cleared = json.loads(self.call("task", "edit", item, "--no-repo", "--json").stdout)
        self.assertIsNone(cleared["item"]["repo"])

    def test_an_idea_row_moves_to_a_repository_and_back_and_keeps_its_kind(self):
        """sd:5142: `--belongs-to` and `--no-repo` move an idea; every other field edit on it stays refused."""
        root = self._checkout("ideas")
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, root, home=self.home)
            item = sd_db.writes.create_item(connection, kind="idea", title="Blog idea", source="vault",
                                            external_id="blog-idea:Blog Ideas/Blog idea.md")
        moved = json.loads(self.call("task", "edit", item, "--belongs-to", root, "--json").stdout)
        self.assertEqual((moved["item"]["kind"], moved["item"]["repo"]), ("idea", sd_lib.stored_repo(root)))
        cleared = json.loads(self.call("task", "edit", item, "--no-repo", "--json").stdout)
        self.assertEqual((cleared["item"]["kind"], cleared["item"]["repo"]), ("idea", None))
        refused = self.call("task", "edit", item, "--title", "Renamed", code=1)
        self.assertIn("idea items use their own editing workflow", refused.stderr)

    def test_the_move_is_recorded_on_the_item_rather_than_happening_silently(self):
        root = self._checkout("recorded")
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, root, home=self.home)
        state = json.loads(self.call("task", "add", "--priority", "3", "Mis-filed", "--json").stdout)
        item = state["item"]["id"]
        before = {note["id"] for note in state["notes"]}
        moved = json.loads(self.call("task", "edit", item, "--belongs-to", root, "--json").stdout)
        added = [note for note in moved["notes"] if note["id"] not in before]
        self.assertEqual([note["kind"] for note in added], ["comment"])
        self.assertIn("repo", added[0]["body"])

    def test_the_move_is_visible_without_asking_for_json(self):
        """The other half of sd:452: `repo` was the field no output showed.

        `--belongs-to` wrote the database and printed the same five words back,
        so the only way to see the move was `--json`. It prints now -- but only
        where it says something, because a path on every row of every listing
        is a field nobody reads.
        """
        root = self._checkout("visible")
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, root, home=self.home)
        item = json.loads(self.call("task", "add", "--priority", "3", "Mis-filed", "--json").stdout)["item"]["id"]

        # The move, made from inside the destination, where "which repository"
        # is otherwise answered by where the caller is standing.
        moved = self.call("task", "edit", item, "--belongs-to", ".", cwd=root)
        self.assertIn(f"repo: {sd_lib.stored_repo(root)}", moved.stdout)

        # Read back from outside every checkout: nothing else answers it.
        self.assertIn(f"repo: {sd_lib.stored_repo(root)}",
                      self.call("store", "item", item).stdout)

        # The control. Standing in the row's own checkout, an ordinary listing
        # does not grow a repo line.
        listed = self.call("store", "items", cwd=root)
        self.assertIn("Mis-filed", listed.stdout)
        self.assertNotIn("repo:", listed.stdout)

        # And a linked worktree of it is still standing in it: the row carries
        # the main checkout, which is the spelling `add` resolved it to.
        linked = self.home / "visible-linked"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "side", str(linked)],
                       cwd=str(root), check=True, capture_output=True, text=True)
        self.assertNotIn("repo:", self.call("store", "items", cwd=linked).stdout)

        # Clearing it is a move too, and says so rather than printing a blank.
        cleared = self.call("task", "edit", item, "--no-repo", cwd=root)
        self.assertIn(f"repo: {sd_work.NO_CHECKOUT}", cleared.stdout)
        # ...and afterwards there is no path to name, so nothing is named.
        self.assertNotIn("repo:", self.call("store", "item", item).stdout)

    def test_an_unregistered_repository_is_refused_without_moving_the_row(self):
        """The `repo` table is the authority, and a refusal changes nothing."""
        stranger = self._checkout("stranger")
        state = json.loads(self.call("task", "add", "--priority", "3", "Stays put", "--json").stdout)
        item = state["item"]["id"]
        refused = self.call("task", "edit", item, "--belongs-to", stranger, code=1)
        self.assertIn("not registered", refused.stderr)
        readback = json.loads(self.call("store", "item", item, "--json").stdout)
        self.assertIsNone(readback["item"]["repo"])
        self.assertEqual(readback["revision"], state["revision"])

    # Stands in for a library whose `edit_item` reports success and writes nothing.
    DROPPING_EDIT = (
        "import sd_db.workflow as workflow\n"
        "def edit_item(connection, item, changes, **keywords):\n"
        "    return workflow.item_state(connection, item)\n"
        "workflow.edit_item = edit_item\n")

    def _detached(self, name):
        root = self._checkout(name)
        subprocess.run(["git", "checkout", "-q", "--detach"], cwd=str(root), check=True,
                       capture_output=True, text=True, env=clean_environment())
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, root, home=self.home)
        return root

    def test_belongs_to_names_a_registered_checkout_on_a_detached_head(self):
        """sd:4740. `~/...`, `.` and an absolute path each set the repo from a detached HEAD."""
        root = self._detached("detached")
        for spelling, cwd in ((f"~/{root.name}", self.home), (".", root), (str(root), self.home)):
            item = json.loads(self.call("task", "add", "--priority", "3", "Mis-filed", "--json").stdout)["item"]["id"]
            self.call("task", "edit", item, "--belongs-to", spelling, cwd=cwd)
            row = json.loads(self.call("store", "item", item, "--json").stdout)["item"]
            self.assertEqual(row["repo"], sd_lib.stored_repo(root), spelling)

    def test_a_belongs_to_that_changed_nothing_is_a_refusal_naming_the_path(self):
        """sd:4740. A library that returns the row unchanged exited 0 and printed it."""
        root = self._detached("dropped")
        state = json.loads(self.call("task", "add", "--priority", "3", "Mis-filed", "--json").stdout)
        item = state["item"]["id"]
        environment = self.shimmed(self.DROPPING_EDIT)
        refused = self.call("task", "edit", item, "--belongs-to", f"~/{root.name}", code=1, env=environment)
        self.assertIn(f"--belongs-to ~/{root.name}", refused.stderr)
        self.assertIn(f"expected item {item} to belong to {sd_lib.stored_repo(root)}", refused.stderr)
        self.assertIn("belongs to no checkout", refused.stderr)
        readback = json.loads(self.call("store", "item", item, "--json").stdout)
        self.assertEqual(readback["revision"], state["revision"])

    def test_a_no_repo_that_changed_nothing_is_a_refusal(self):
        """sd:4740. The same read-back guards the clearing spelling."""
        root = self._detached("kept")
        item = json.loads(self.call("task", "add", "--priority", "3", "Filed", "--belongs-to", str(root), "--json").stdout)["item"]["id"]
        refused = self.call("task", "edit", item, "--no-repo", code=1, env=self.shimmed(self.DROPPING_EDIT))
        self.assertIn(f"expected item {item} to belong to no checkout", refused.stderr)

    def test_a_home_that_cannot_be_expanded_is_a_refusal_not_a_traceback(self):
        """sd:1000 (1c0e15d3a773): `~nobody/x` raised RuntimeError out of `expanduser`."""
        state = json.loads(self.call("task", "add", "--priority", "3", "Stays put", "--json").stdout)
        refused = self.call("task", "edit", state["item"]["id"], "--belongs-to",
                            "~sd-no-such-user-1000/x", code=1)
        self.assertIn("--belongs-to: cannot expand ~sd-no-such-user-1000/x", refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)

    def test_json_help_names_where_the_item_id_is(self):
        """sd:5742: a caller read `.get('id')` off `task add --json`, printed `None` and filed the item twice."""
        filed = json.loads(self.call("task", "add", "--priority", "3", "Shape", "--json").stdout)
        self.assertNotIn("id", filed)
        self.assertIsInstance(filed["item"]["id"], int)
        for verb in (("task", "add"), ("task", "edit"), ("task", "show"), ("task", "status")):
            said = " ".join(self.call(*verb, "--help").stdout.split())
            self.assertIn('{"item": {"id": N, ...}, "notes": [...], "revision": ...}', said, verb)

    def test_here_help_names_the_registration_the_refusal_needs(self):
        """sd:1000 (6b9855085e9c): the help said any checkout; the refusal needs a registered one."""
        said = " ".join(self.call("task", "add", "--priority", "3", "--help").stdout.split())
        self.assertIn("refuse unless this is a checkout registered in the sd database", said)

    def test_add_belongs_to_files_the_item_into_another_checkout_in_one_call(self):
        """sd:3567. Filing into another repository took an add and then an edit."""
        first, second, stranger = self._checkout("first"), self._checkout("second"), self._checkout("stranger")
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, first, home=self.home)
            sd_db.repos.add(connection, second, home=self.home)
        linked = self.home / "first-linked"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "side", str(linked)],
                       cwd=str(first), check=True, capture_output=True, text=True, env=clean_environment())
        filed = json.loads(self.call("task", "add", "--priority", "3", "For the other repo", "--belongs-to", linked,
                                     "--json", cwd=second).stdout)
        self.assertEqual(filed["item"]["repo"], sd_lib.stored_repo(first))
        here = json.loads(self.call("task", "add", "--priority", "3", "Here", "--belongs-to", ".", "--json", cwd=second).stdout)
        self.assertEqual(here["item"]["repo"], sd_lib.stored_repo(second))
        before = json.loads(self.call("store", "items", "--json").stdout)
        refused = self.call("task", "add", "--priority", "3", "Nowhere", "--belongs-to", stranger, code=1)
        self.assertIn("not registered", refused.stderr)
        idea = self.call("task", "add", "--priority", "3", "Mine", "--kind", "work-idea", "--belongs-to", first, code=1)
        self.assertIn("--belongs-to: a work-idea item carries no repository", idea.stderr)
        self.assertEqual(json.loads(self.call("store", "items", "--json").stdout), before)
        for other in ("--here", "--no-repo"):
            self.call("task", "add", "--priority", "3", "Both", "--belongs-to", first, other, code=2)

    def test_belongs_to_and_no_repo_are_the_same_field_and_cannot_both_be_given(self):
        state = json.loads(self.call("task", "add", "--priority", "3", "One or the other", "--json").stdout)
        self.call("task", "edit", state["item"]["id"], "--belongs-to", self.home,
                  "--no-repo", code=2)
        self.assertIn("requires a field",
                      self.call("task", "edit", state["item"]["id"], code=1).stderr)

    def test_help_says_how_to_defer_close_a_report_and_what_each_note_kind_is(self):
        """sd:5065: how to defer, a report is closed elsewhere, and a note kind needs a meaning."""
        def said(*verb):
            return " ".join(self.call(*verb, "--help").stdout.split()).replace("- ", "-")

        self.assertIn("to defer the item, set a later date, or park it with --park", said("task", "edit"))
        self.assertIn("`report`, a scheduled job's run, closes with `sd reports acknowledge N`",
                      said("store", "items"))
        # sd:7004: a skill review is a task now, so the kind names no review.
        self.assertNotIn("skill-review", said("store", "items"))
        self.assertIn("a report closes with `sd reports acknowledge N`", said("task", "status"))
        note = said("task", "note")
        for kind in NOTE_KINDS:
            with self.subTest(kind=kind):
                self.assertRegex(note, rf"\b{kind} \(")

    def test_kind_help_says_the_kinds_come_from_the_installed_library(self):
        """sd:1000 (0f93e3b2bf82): a followup takes a checkout only on an sd_db from sd:809 on."""
        said = " ".join(self.call("task", "add", "--priority", "3", "--help").stdout.split())
        self.assertIn("from the kinds the installed sd_db offers", said)
        self.assertIn("(an sd_db older than sd:809 files it with none)", said)

    def test_the_kind_lists_are_the_library_s_and_the_pack_keeps_no_copy(self):
        """sd:743. `add` and `edit` offer exactly `workflow.HAND_KINDS`.

        The pack's `ADD_KINDS` was the list the library's `HAND_KINDS` was
        copied from, and two copies of one list drift. So the pack holds
        neither `ADD_KINDS` nor `REPO_LESS_KINDS`, and both verbs' `--kind`
        read the library's tuple, in the library's order.
        """
        self.assertFalse(hasattr(sd_work, "ADD_KINDS"))
        self.assertFalse(hasattr(sd_work, "REPO_LESS_KINDS"))
        offered = "{" + ",".join(workflow.HAND_KINDS) + "}"
        for verb in ("add", "edit"):
            with self.subTest(verb=verb):
                self.assertIn(offered, self.call("task", verb, "--help").stdout)

    def test_building_the_parser_imports_no_sd_db(self):
        """The choices are read when argparse asks, not when the parser is built.

        `sd --help` answers in guest mode, and every entrypoint reaches `sd_db`
        through `sd_lib.import_sd_db`; a parser that imported the library to
        spell its choices would do both wrong on every invocation.
        """
        probe = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import importlib.machinery as m, importlib.util as u; "
            "loader = m.SourceFileLoader('sd', sys.argv[2]); "
            "spec = u.spec_from_loader('sd', loader); "
            "sd = u.module_from_spec(spec); loader.exec_module(sd); "
            "sd.build_parser(); "
            "print(sorted(name for name in sys.modules if name.split('.')[0] == 'sd_db'))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe, str(ROOT / "bin"), str(ROOT / "bin" / "sd")],
            env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")

    def test_edit_moves_a_task_with_a_repository_to_a_repo_less_kind_in_one_edit(self):
        """sd:743. `--kind work-idea --no-repo` is one edit, attributed and noted."""
        root = self._checkout("reclassified")
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.repos.add(connection, root, home=self.home)
        state = json.loads(self.call("task", "add", "--priority", "3", "Not work at all", "--json", cwd=root).stdout)
        item = state["item"]["id"]
        self.assertEqual((state["item"]["kind"], state["item"]["repo"]),
                         ("task", sd_lib.stored_repo(root)))
        before = {note["id"] for note in state["notes"]}

        moved = json.loads(self.call(
            "task", "edit", item, "--kind", "work-idea", "--no-repo",
            "--if-revision", state["revision"], "--json").stdout)
        self.assertEqual((moved["item"]["kind"], moved["item"]["repo"]), ("work-idea", None))
        added = [note for note in moved["notes"] if note["id"] not in before]
        self.assertEqual([note["kind"] for note in added], ["comment"])
        self.assertEqual(added[0]["body"].splitlines()[0],
                         f"Changed kind task -> work-idea by {getpass.getuser()}")

        with database(sd_db.default_path(self.home), write=False) as connection:
            row = connection.execute("SELECT kind, repo FROM item WHERE id = ?", (item,)).fetchone()
        self.assertEqual((row["kind"], row["repo"]), ("work-idea", None))
        self.assertEqual(json.loads(self.call("store", "item", item, "--json").stdout), moved)

    def test_edit_refuses_a_produced_kind_at_the_parser_without_touching_the_row(self):
        state = json.loads(self.call("task", "add", "--priority", "3", "Stays a task", "--json").stdout)
        item = state["item"]["id"]
        for kind in ("report", "work", "dep", "skill-review", "personal", "proposal", "idea", "nonsense"):
            with self.subTest(kind=kind):
                refused = self.call("task", "edit", item, "--kind", kind, code=2)
                self.assertIn("invalid choice", refused.stderr)
        readback = json.loads(self.call("store", "item", item, "--json").stdout)
        self.assertEqual(readback, state)

    def test_the_library_s_kind_refusal_reaches_the_caller_without_a_traceback(self):
        """A contribution task keeps its kind, and `edit_item` is what says so."""
        state = json.loads(self.call("task", "add", "--priority", "3", "Upstream pull request", "--json").stdout)
        item = state["item"]["id"]
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.writes.set_item_fields(
                connection, item, fields={"contribution": {"url": "https://example.invalid/pr/1"}})
            connection.commit()
        before = json.loads(self.call("store", "item", item, "--json").stdout)

        refused = self.call("task", "edit", item, "--kind", "work-idea", code=1)
        self.assertIn(f"item {item} carries contribution metadata", refused.stderr)
        self.assertTrue(refused.stderr.startswith("sd: "), refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        self.assertEqual(json.loads(self.call("store", "item", item, "--json").stdout), before)

    def test_refusals_and_usage_have_distinct_exit_codes(self):
        self.assertIn("no item", self.call("store", "item", 9999, code=1).stderr)
        self.call("task", "add", "Task", "--priority", 9, code=2)
        self.assertIn("Git checkout", self.call("task", "add", "--priority", "3", "Task", "--here", code=1).stderr)
        self.call("task", "add", "--priority", "3", "Task", "--due", "tomorrow", code=1)


class TaskRef(unittest.TestCase):
    """sd:1902. `sd task add --ref` files one row per occurrence and prints it back."""

    # Borrowed, not inherited: a subclass would run every `TaskCLI` test twice.
    setUp = TaskCLI.setUp
    call = TaskCLI.call

    def add(self, *arguments):
        return json.loads(self.call("task", "add", "--priority", "3", *arguments, "--json").stdout)

    def rows(self):
        return json.loads(self.call("store", "items", "--json").stdout)

    def test_help_lists_ref(self):
        self.assertIn("--ref", self.call("task", "add", "--priority", "3", "--help").stdout)

    def test_a_second_add_with_the_same_ref_refuses_and_leaves_the_row(self):
        """sd:3703. Two items filed with `--ref sd:3399` overwrote sd:3407 twice; an add files, an edit changes."""
        first = self.add("repo-sync failed", "--kind", "followup", "--ref", "job:repo-sync:42",
                         "--body", "log at line 9")
        refused = self.call("task", "add", "--priority", "3", "repo-sync failed: core", "--kind", "followup",
                            "--ref", "job:repo-sync:42", "--body", "", code=1)
        item = first["item"]["id"]
        self.assertIn(f"sd:{item} (planning) already carries --ref job:repo-sync:42", refused.stderr)
        self.assertIn(f"sd task edit {item}", refused.stderr)
        self.assertEqual(len(self.rows()), 1)
        shown = json.loads(self.call("store", "item", item, "--json").stdout)
        self.assertEqual((shown["item"]["title"], shown["item"]["body"], shown["revision"]),
                         (first["item"]["title"], first["item"]["body"], first["revision"]))
        self.assertIn("  ref: job:repo-sync:42", self.call("task", "show", item).stdout)

    def test_a_done_or_cancelled_row_is_never_overwritten(self):
        for status, ref in (("done", "job:repo-sync:42"), ("cancelled", "job:repo-sync:43")):
            with self.subTest(status=status):
                first = self.add("repo-sync failed", "--kind", "followup", "--ref", ref)
                item = first["item"]["id"]
                if status == "done":
                    self.call("task", "status", item, "done")
                else:
                    self.call("task", "cancel", item, "--reason", "not needed")
                before = json.loads(self.call("store", "item", item, "--json").stdout)
                refused = self.call("task", "add", "--priority", "3", "other work", "--kind", "followup", "--ref", ref, code=1)
                self.assertIn(f"sd:{item} ({before['item']['status']}) already carries", refused.stderr)
                self.assertEqual(json.loads(self.call("store", "item", item, "--json").stdout), before)
        later = self.add("repo-sync failed", "--kind", "followup", "--ref", "job:repo-sync:44")
        self.assertNotIn(later["item"]["id"], [row["id"] for row in self.rows()[:2]])
        self.assertEqual(len(self.rows()), 3)

    def test_add_prints_the_new_id_first(self):
        """sd:3703. sd:3161 was a second add made to read the first one's id back."""
        shown = self.call("task", "add", "--priority", "3", "one row").stdout
        self.assertRegex(shown.splitlines()[0], r"^\S+ sd:\d+  planning  one row · P3$")

    def test_today_carries_the_ref(self):
        state = self.add("HOA alarm follow-up", "--ref", "hoa:a2-vsd")
        self.call("task", "status", state["item"]["id"], "in_progress")
        (row,) = json.loads(self.call("today", "--json").stdout)
        self.assertEqual((row["id"], row["ref"]), (state["item"]["id"], "hoa:a2-vsd"))

    def test_a_hub_ref_files_and_lists_under_attention(self):
        """pack sd:7358: a satellite files `--ref hub:<host>-<slug>`; the hub's `sd today` lists it.

        The scratch database names no hub, so the fence is off and this machine is the hub.
        """
        item = self.add("Read the release thread", "--kind", "followup", "--ref", "hub:sat-mail-release")["item"]["id"]
        shown = self.call("today").stdout
        attention = shown.partition("Attention")[2].partition("Operations")[0]
        self.assertIn(f"#{item}", attention)
        self.assertIn("hub request", attention)

    def test_a_ref_the_upsert_cannot_keep_is_refused(self):
        for arguments, reason in (
            (("--ref", "job"), "<source>:<id>"),
            (("--ref", ":42"), "<source>:<id>"),
            (("--ref", "job:x", "--kind", "work-idea"), "task or a followup"),
            (("--ref", "job:x", "--due", "2026-01-01", "--recur", "FREQ=WEEKLY"), "--recur"),
        ):
            with self.subTest(arguments=arguments):
                refused = self.call("task", "add", "--priority", "3", "x", *arguments, code=1)
                self.assertIn(reason, refused.stderr)
        self.assertEqual(self.rows(), [])


class Recurrence(unittest.TestCase):
    """sd:1428. `--recur` and `--recur-anchor` reach the rule sd:1099 stores.

    The library owns every rule about a rule: its grammar, the default
    anchor, the kinds that may recur and the due date a series needs. These
    tests read its refusals back through the CLI, so the day the pack starts
    deciding one of them itself, the message stops being the library's.
    """

    # Borrowed, not inherited: a subclass would run every `TaskCLI` test twice.
    setUp = TaskCLI.setUp
    call = TaskCLI.call

    def add(self, *arguments):
        return json.loads(self.call("task", "add", "--priority", "3", *arguments, "--json").stdout)

    def test_add_stores_the_rule_with_the_library_s_default_anchor(self):
        state = self.add("File the weekly report", "--due", "2026-01-01", "--recur", "freq=weekly")
        self.assertEqual(state["item"]["recurrence"], "FREQ=WEEKLY")
        self.assertEqual(state["item"]["recurrence_anchor"], "schedule")
        anchored = self.add("Service the boiler", "--due", "2026-01-01",
                            "--recur", "FREQ=YEARLY", "--recur-anchor", "completion")
        self.assertEqual(anchored["item"]["recurrence_anchor"], "completion")

    def test_add_prints_the_rule_without_json(self):
        shown = self.call("task", "add", "--priority", "3", "Weekly", "--due", "2026-01-01", "--recur", "FREQ=WEEKLY").stdout
        self.assertIn("recurs FREQ=WEEKLY (schedule)", shown)

    def test_a_recurring_followup_takes_its_rule(self):
        state = self.add("Chase the vendor", "--kind", "followup", "--due", "2026-01-01",
                         "--recur", "FREQ=MONTHLY")
        self.assertEqual((state["item"]["kind"], state["item"]["recurrence"]),
                         ("followup", "FREQ=MONTHLY"))

    def test_add_refusals_are_the_library_s(self):
        before = json.loads(self.call("store", "items", "--json").stdout)
        cases = (
            (("--recur", "FREQ=WEEKLY"), "a recurring item needs a due date"),
            (("--due", "2026-01-01", "--recur-anchor", "completion"),
             "recurrence_anchor needs a recurrence rule"),
            (("--due", "2026-01-01", "--recur", "FREQ=WEEKLY", "--recur-anchor", "sometimes"),
             "recurrence_anchor must be schedule or completion"),
            (("--due", "2026-01-01", "--recur", "FREQ=HOURLY"), "FREQ"),
            (("--kind", "work-idea", "--due", "2026-01-01", "--recur", "FREQ=WEEKLY"),
             "work-idea items cannot recur"),
        )
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                refused = self.call("task", "add", "--priority", "3", "Refused", *arguments, code=1)
                self.assertIn(message, refused.stderr)
                self.assertNotIn("Traceback", refused.stderr)
        self.assertEqual(json.loads(self.call("store", "items", "--json").stdout), before)

    def test_edit_sets_changes_and_clears_the_rule(self):
        item = self.add("Water the plants", "--due", "2026-01-01")["item"]["id"]
        edited = json.loads(self.call("task", "edit", item, "--recur", "FREQ=WEEKLY", "--json").stdout)
        self.assertEqual((edited["item"]["recurrence"], edited["item"]["recurrence_anchor"]),
                         ("FREQ=WEEKLY", "schedule"))
        anchored = json.loads(self.call("task", "edit", item, "--recur-anchor", "completion",
                                        "--json").stdout)
        self.assertEqual(anchored["item"]["recurrence_anchor"], "completion")
        cleared = json.loads(self.call("task", "edit", item, "--clear-recur", "--json").stdout)
        self.assertEqual((cleared["item"]["recurrence"], cleared["item"]["recurrence_anchor"]),
                         (None, None))
        self.call("task", "edit", item, "--recur", "FREQ=WEEKLY", "--clear-recur", code=2)
        refused = self.call("task", "edit", item, "--recur-anchor", "completion", code=1)
        self.assertIn("recurrence_anchor needs a recurrence rule", refused.stderr)

    def test_recur_lead_on_add_and_edit_and_cleared_with_the_rule(self):
        # pack sd:5648.
        state = self.add("Pay the dues", "--due", "2026-01-10", "--recur", "FREQ=MONTHLY", "--recur-lead", "3")
        item = state["item"]["id"]
        self.assertEqual(state["item"]["recurrence_lead"], 3)
        edited = json.loads(self.call("task", "edit", item, "--recur-lead", "7", "--json").stdout)
        self.assertEqual(edited["item"]["recurrence_lead"], 7)
        shown = self.call("task", "edit", item, "--recur-lead", "5").stdout
        self.assertIn("recurs FREQ=MONTHLY (schedule, shows 5d early)", shown)
        cleared = json.loads(self.call("task", "edit", item, "--clear-recur", "--json").stdout)
        self.assertEqual((cleared["item"]["recurrence"], cleared["item"]["recurrence_lead"]), (None, None))

    def test_store_items_names_the_day_a_scheduled_occurrence_opens(self):
        self.add("Renew the licence", "--due", "2099-01-10", "--recur", "FREQ=YEARLY", "--recur-lead", "3")
        self.assertIn("recurs FREQ=YEARLY (schedule, shows 3d early) · opens 2099-01-07",
                      self.call("store", "items").stdout)
        rows = json.loads(self.call("store", "items", "--json").stdout)
        self.assertEqual([(row["opens_on"], row["scheduled"]) for row in rows], [("2099-01-07", 1)])

    def test_recur_lead_refusals(self):
        before = json.loads(self.call("store", "items", "--json").stdout)
        for arguments, code, message in (
                (("--due", "2026-01-10", "--recur", "FREQ=WEEKLY", "--recur-lead", "-1"), 1,
                 "recurrence_lead must be a whole number of days"),
                (("--due", "2026-01-10", "--recur", "FREQ=WEEKLY", "--recur-lead", "1.5"), 2, "invalid int"),
                (("--due", "2026-01-10", "--recur", "FREQ=WEEKLY", "--recur-lead", "soon"), 2, "invalid int"),
                (("--due", "2026-01-10", "--recur-lead", "3"), 1, "recurrence_lead needs a recurrence rule")):
            with self.subTest(arguments=arguments):
                refused = self.call("task", "add", "--priority", "3", "Refused", *arguments, code=code)
                self.assertIn(message, refused.stderr)
                self.assertNotIn("Traceback", refused.stderr)
        self.assertEqual(json.loads(self.call("store", "items", "--json").stdout), before)
        plain = self.add("Plain", "--due", "2026-01-10")["item"]["id"]
        refused = self.call("task", "edit", plain, "--recur-lead", "3", code=1)
        self.assertIn("recurrence_lead needs a recurrence rule", refused.stderr)

    def test_completion_names_the_next_occurrence(self):
        item = self.add("Weekly filing", "--due", "2026-01-01", "--recur", "FREQ=WEEKLY")["item"]["id"]
        completed = json.loads(self.call("task", "status", item, "done", "--json").stdout)
        spawned = completed["next_occurrence"]
        self.assertIsInstance(spawned, int)
        self.assertIsNone(completed["next_occurrence_reason"])
        following = json.loads(self.call("store", "item", spawned, "--json").stdout)["item"]
        self.assertEqual((following["due"], following["recurrence"]), ("2026-01-08", "FREQ=WEEKLY"))
        shown = self.call("task", "status", spawned, "done").stdout
        self.assertRegex(shown, rf"next occurrence: \S+ sd:{spawned + 1} · due 2026-01-15")

    def test_a_completion_that_ends_the_series_says_so(self):
        """The runtime case the write-time checks cannot see: no `due` left."""
        item = self.add("Ends", "--due", "2026-01-01", "--recur", "FREQ=WEEKLY")["item"]["id"]
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.writes.set_item_fields(connection, item, due=None)
            connection.commit()
        shown = self.call("task", "status", item, "done").stdout
        self.assertIn("recurrence ended: ", shown)
        self.assertIn("no due date", shown)
        self.assertNotIn("next occurrence: ", shown)

    def test_a_completion_that_ends_the_series_carries_its_reason_in_json(self):
        item = self.add("Ends", "--due", "2026-01-01", "--recur", "FREQ=WEEKLY")["item"]["id"]
        with database(sd_db.default_path(self.home), write=True) as connection:
            sd_db.writes.set_item_fields(connection, item, due=None)
            connection.commit()
        completed = json.loads(self.call("task", "status", item, "done", "--json").stdout)
        self.assertIsNone(completed["next_occurrence"])
        self.assertIn("no due date", completed["next_occurrence_reason"])

    def test_a_task_that_does_not_recur_prints_no_occurrence_line(self):
        item = self.add("Once")["item"]["id"]
        shown = self.call("task", "status", item, "done").stdout
        self.assertNotIn("occurrence", shown)
        self.assertNotIn("recur", shown)

    def test_help_names_both_flags_and_both_anchors(self):
        for verb in ("add", "edit"):
            with self.subTest(verb=verb):
                text = self.call("task", verb, "--help").stdout
                for word in ("--recur", "--recur-anchor", "schedule", "completion"):
                    self.assertIn(word, text)
        self.assertIn("--clear-recur", self.call("task", "edit", "--help").stdout)


def load_cli():
    """`bin/sd` as a module, so `main` runs in-process."""
    loader = importlib.machinery.SourceFileLoader("sd_cli_for_replay", str(ROOT / "bin" / "sd"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class NoSelfInstall(unittest.TestCase):
    """sd:3280: `sd_db.self_install` is gone; a read verb runs without it."""

    def test_a_read_verb_runs_with_no_self_install_module(self):
        with unittest.mock.patch.object(sd_work, "run", unittest.mock.Mock(return_value=0)) as run:
            self.assertEqual(load_cli().main(["task", "show", "5"]), 0)
        run.assert_called_once()
        self.assertNotIn("sd_db.self_install", sys.modules)


class OneItemIdSpelling(unittest.TestCase):
    """sd:3500. Every positional named `item` reads 234, sd:234 and item:234.

    `sd task show sd:3432` was an invalid int while `sd note list sd:3432`
    worked, and `sd task contribution show 1429` refused a bare id.
    """

    setUp = TaskCLI.setUp
    call = TaskCLI.call

    def item_positionals(self):
        sd = sd_lib.sibling("sd_under_item_ids", "sd")
        found = []

        def walk(parser, path):
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for name, child in action.choices.items():
                        walk(child, (*path, name))
                elif not action.option_strings and action.dest == "item":
                    found.append((" ".join(path), action.type))

        walk(sd.build_parser(), ())
        # `sd-ship lane` builds its own parser (sd:3715).
        walk(sd_lib.sibling("sd_ship_under_item_ids", "sd-ship").parser(), ("sd-ship",))
        return found

    def test_every_item_positional_takes_item_id(self):
        import sd_notes

        found = self.item_positionals()
        paths = [path for path, _ in found]
        self.assertIn("task show", paths, "the walk missed a verb that exists")
        self.assertIn("sd-ship lane retry", paths, "the walk missed sd-ship's lane verbs")
        wrong = [path for path, kind in found if kind is not sd_notes.item_id]
        self.assertEqual(wrong, [])

    def test_item_id_reads_three_spellings_and_refuses_others(self):
        import sd_notes

        self.assertEqual({sd_notes.item_id(text) for text in ("234", "sd:234", "item:234")}, {234})
        for text in ("x234", "sd:", "item:-1", "issue:234"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=text):
                sd_notes.item_id(text)

    def test_task_show_takes_sd_and_item_spellings(self):
        item = json.loads(self.call("task", "add", "--priority", "3", "Spell me", "--json").stdout)["item"]["id"]
        for spelling in (f"sd:{item}", f"item:{item}"):
            shown = json.loads(self.call("task", "show", spelling, "--json").stdout)
            self.assertEqual(shown["item"]["id"], item)

    def test_note_body_is_positional_and_body_flag_still_works(self):
        item = json.loads(self.call("task", "add", "--priority", "3", "Note me", "--json").stdout)["item"]["id"]
        positional = json.loads(self.call("task", "note", item, "Positional text", "--json").stdout)
        self.assertEqual(positional["note"]["body"], "Positional text")
        flagged = json.loads(self.call("task", "note", item, "--body", "Flag text", "--json").stdout)
        self.assertEqual(flagged["note"]["body"], "Flag text")
        both = self.call("task", "note", item, "One", "--body", "Two", code=1)
        self.assertIn("not both", both.stderr)
        neither = self.call("task", "note", item, code=1)
        self.assertIn("the note text", neither.stderr)

    def test_task_resolve_is_gone(self):
        refused = self.call("task", "resolve", "1", code=2)
        self.assertIn("invalid choice: 'resolve'", refused.stderr)

    def test_contribution_key_takes_a_bare_or_sd_id(self):
        import sd_db.contributions as contributions

        for spelling in ("1429", "sd:1429", "item:1429"):
            self.assertEqual(contributions._key(spelling), "item:1429")


class FieldEditsFold(unittest.TestCase):
    """sd:3511. 'Updated X by Y' notes were 29 percent of the notes on open items.

    `sd task show` folds them into one count line unless `--audit`; the store
    and `--json` keep every note.
    """

    setUp = TaskCLI.setUp
    call = TaskCLI.call

    def test_show_folds_field_edits_unless_audit(self):
        item = json.loads(self.call("task", "add", "--priority", "3", "Fold me", "--json").stdout)["item"]["id"]
        self.call("task", "edit", item, "--priority", 2)
        self.call("task", "edit", item, "--due", "2026-12-01")
        self.call("task", "note", item, "--body", "Updated priority by hand, a person wrote this")
        plain = self.call("task", "show", item).stdout
        self.assertNotIn("Updated priority by", plain.replace("Updated priority by hand", ""))
        self.assertNotIn("Updated due by", plain)
        self.assertIn(f"2 field edits hidden; sd task show {item} --audit shows them", plain)
        self.assertIn("Updated priority by hand, a person wrote this", plain)
        audit = self.call("task", "show", item, "--audit").stdout
        self.assertIn("Updated due by", audit)
        self.assertNotIn("field edits hidden", audit)
        bodies = [note["body"] for note in json.loads(self.call("task", "show", item, "--json").stdout)["notes"]]
        self.assertEqual(sum(body.startswith("Updated ") and " by " in body for body in bodies), 3)

    def test_the_predicate_reads_what_edit_item_writes(self):
        audit = workflow.is_audit_note
        for body in ("Updated priority by operator", "Updated due, title by dashboard",
                     "Changed kind task -> followup by operator\nUpdated repo by operator", "Unparked by operator",
                     "Parked by operator"):
            self.assertTrue(audit({"kind": "comment", "body": body}), body)
        for note in ({"kind": "comment", "body": "Updated priority by hand, a person wrote this"},
                     {"kind": "decision", "body": "Updated priority by operator"},
                     {"kind": "comment", "body": "Updated priority by operator\nand a second thought"}):
            self.assertFalse(audit(note), note)


class TodayIsTheDashboardsToday(unittest.TestCase):
    """sd:3501. `sd today` printed work rows only: no reports, no followups, no
    decisions, no overdue mark, and due rows in oldest-in-status order."""

    setUp = TaskCLI.setUp
    call = TaskCLI.call

    def task(self, title, *flags):
        return json.loads(self.call("task", "add", "--priority", "3", title, *flags, "--json").stdout)["item"]["id"]

    def test_today_prints_each_section_and_marks_overdue(self):
        late = self.task("Late one", "--due", "2020-01-02")
        later = self.task("Later one", "--due", "2020-01-01")
        self.call("task", "status", late, "in_progress")
        open_item = self.task("Has a followup")
        self.call("task", "note", open_item, "--body", "Chase the vendor", "--kind", "followup")
        closed = self.task("Done with a followup")
        self.call("task", "note", closed, "--body", "Owed on a done item", "--kind", "followup")
        self.call("task", "status", closed, "done", "--reason", "test")
        asked = json.loads(self.call("task", "note", open_item, "--kind", "question", "--json",
                                     "--body", "Which store?\nOption: Pack store (Recommended)\nOption: Database rows").stdout)
        log = self.home / "run.log"
        log.write_text("Observed drift.\nSD_REPORT_ATTENTION: two stale rows\n")
        report = json.loads(self.call(
            "reports", "ingest", "fixture", "--run-id", "r1", "--started", "2026-09-08T00:00:00Z",
            "--ended", "2026-09-08T00:01:00Z", "--exit-code", "0", "--log", log, "--json").stdout)["item"]["id"]

        lines = self.call("today", env={**self.environment, "COLUMNS": "120"}).stdout.splitlines()
        # sd:5098: every section prints, in one order, as a table; a hand close is one line, not a row.
        heads = [line for line in lines if line.endswith(")") and line.split(" (")[0] in HEADINGS.values()]
        self.assertEqual(heads, ["Attention (1)", "Operations, this machine (0)", "Decisions (1)", "Landing now (0)", "Queues (0)", "Delivered (0)", "Planned (0)",
                                 "Blocked (0)", "Unscheduled (1)", "Followups (1)", "Work (2)"])
        self.assertEqual(lines[lines.index("Delivered (0)") + 1], "+1 closed by hand")
        text = "\n".join(lines)
        self.assertRegex(text, rf"#{report} +fleet +planning ")
        # sd:5014: the note id is what `sd task note --answers` takes.
        self.assertRegex(text, rf"#{open_item} +fleet +planning +#{asked['note']['id']} +Which store\?")
        self.assertRegex(text, rf"#{open_item} +fleet +planning +#[0-9]+ +Chase the vendor")
        self.assertNotIn("Owed on a done item", text)
        work = lines[lines.index("Work (2)") + 3:]
        self.assertEqual([line.split()[0] for line in work], [f"#{later}", f"#{late}"])
        self.assertTrue(all(" late " in line for line in work), work)

    def test_today_shows_gate_holders_and_an_unknown_gate(self):
        """sd:5098: the holders `sd gate status --json` reads; a failed read is unknown, not an idle gate."""
        import fcntl
        slots = self.home / "slots"
        slots.mkdir()
        lock = slots / "slot.1.lock"
        lock.write_text(f"{os.getpid()}\n")
        (slots / "slot.1.info").write_text(json.dumps({"pid": os.getpid(), "label": "fixture tests", "since": 0}))
        held = os.open(lock, os.O_RDWR)
        self.addCleanup(os.close, held)
        fcntl.flock(held, fcntl.LOCK_EX)
        lines = self.call("today", env={**self.environment, "SD_GATE_SLOTS_DIR": str(slots)}).stdout.splitlines()
        self.assertTrue(any(re.fullmatch(r"Gate slots: 1 holding \(fixture tests since .*\) · 0 waiting · load \S+, limit off", line)
                            for line in lines), lines)
        # A folder the queue lock cannot be made in: the read fails.
        unreadable = self.home / "unreadable"
        unreadable.mkdir(mode=0o500)
        self.addCleanup(unreadable.chmod, 0o700)
        lines = self.call("today", env={**self.environment, "SD_GATE_SLOTS_DIR": str(unreadable)}).stdout.splitlines()
        self.assertTrue(any(line.startswith("Gate slots: unknown (") for line in lines), lines)

    def test_today_nudges_a_session_begun_before_today(self):
        """sd:6955: a lead session that outlives its day gets one line saying to end it."""
        import datetime

        stub = self.home / "stub-bin"
        stub.mkdir()
        ps = stub / "ps"
        yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
        ps.write_text(f"#!/bin/sh\necho '{yesterday:%a %b %d} 09:15:00 {yesterday:%Y}'\n")
        ps.chmod(0o755)
        env = {**self.environment, "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}", "CLAUDE_PID": "4242"}
        self.assertIn(f"This session started {yesterday:%Y-%m-%d} 09:15 ", self.call("today", env=env).stdout)
        env.pop("CLAUDE_PID")
        self.assertNotIn("This session started", self.call("today", env=env).stdout)

    def test_session_age_line_is_quiet_for_today_and_unread_starts(self):
        """sd:6955: today's start, no `$CLAUDE_PID` and no `ps` answer each print nothing."""
        import datetime

        day = datetime.date(2026, 10, 31)
        line = sd_work._session_age_line({"CLAUDE_PID": "7"}, started=lambda pid: "Fri Oct 30 22:05:09 2026",
                                         today=day)
        self.assertRegex(line, r"^This session started 2026-10-30 22:05 \S+; end it after today's merges "
                               r"and start a fresh one from sd today\.$")
        self.assertIsNone(sd_work._session_age_line({"CLAUDE_PID": "7"}, today=day,
                                                    started=lambda pid: "Sat Oct 31 00:01:00 2026"))
        self.assertIsNone(sd_work._session_age_line({}, started=lambda pid: "Fri Oct 30 22:05:09 2026", today=day))
        self.assertIsNone(sd_work._session_age_line({"CLAUDE_PID": "7"}, started=lambda pid: None, today=day))

    def test_today_reads_the_lanes_own_key_prefix(self):
        import sd_lane
        self.assertEqual(sd_db.today.LANE_KEY_PREFIX, sd_lane.KEY_PREFIX)

    def test_today_json_stays_the_work_rows(self):
        self.call("task", "status", self.task("Busy"), "in_progress")
        rows = json.loads(self.call("today", "--json").stdout)
        self.assertEqual([row["title"] for row in rows], ["Busy"])

    def test_today_repo_keeps_one_repositorys_rows(self):
        """pack sd:6488: `--only NAME` filters every section and the counts; `fleet` is no repository."""
        app, lib = "~/repos/example.test/app", "~/repos/example.test/lib"
        with database(sd_db.default_path(self.home), write=True) as connection:
            for repo in (app, lib):
                sd_db.upsert_repo(connection, repo)
            made = {}
            for repo in (app, lib, None):
                made[repo] = item = sd_db.create_item(connection, kind="task", title=f"Open {repo}", repo=repo)
                sd_db.create_item(connection, kind="task", title=f"Busy {repo}", repo=repo, status="in_progress")
                sd_db.writes.add_note(connection, item, "followup", f"Chase {repo}")
            connection.commit()
        env = {**self.environment, "COLUMNS": "120"}

        def heads(*flags):
            lines = self.call("today", *flags, env=env).stdout.splitlines()
            return lines[0], [line for line in lines if line.endswith(")") and line.split(" (")[0] in HEADINGS.values()]

        summary, found = heads("--only", "app")
        self.assertIn("1 unscheduled · 1 followups · 1 work", summary)
        self.assertIn("Followups (1)", found)
        text = self.call("today", "--only", "app", env=env).stdout
        self.assertRegex(text, rf"#{made[app]} +app ")
        self.assertNotRegex(text, rf"(?m)^#({made[lib]}|{made[None]}) ")
        summary, found = heads("--only", "app", "--only", "fleet")
        self.assertIn("2 unscheduled · 2 followups · 2 work", summary)
        self.assertRegex(self.call("today", "--only", "fleet", env=env).stdout, rf"#{made[None]} +fleet ")
        rows = json.loads(self.call("today", "--only", "lib", "--json").stdout)
        self.assertEqual([(row["title"], row["repo"]) for row in rows], [(f"Busy {lib}", lib)])
        refused = self.call("today", "--only", "nope", code=2)
        self.assertIn("unknown repository 'nope'; known: app, lib, fleet", refused.stderr)
        self.assertEqual(refused.stdout, "")

    def test_today_all_lists_every_row_of_a_capped_section(self):
        """pack sd:6959: the text view caps a section at five rows; `--all` prints them all and no `more` line."""
        for number in range(8):
            self.task(f"Unscheduled {number}")
        env = {**self.environment, "COLUMNS": "120"}
        capped = self.call("today", env=env).stdout
        self.assertIn("Unscheduled (8)", capped)
        self.assertIn("… 3 more", capped)
        text = self.call("today", "--all", env=env).stdout
        self.assertEqual(len(re.findall(r"Unscheduled \d", text)), 8)
        self.assertNotIn(" more", text)
        scoped = self.call("today", "--all", "--only", "fleet", env=env).stdout
        self.assertEqual(len(re.findall(r"Unscheduled \d", scoped)), 8)
        self.assertEqual(json.loads(self.call("today", "--all", "--json").stdout),
                         json.loads(self.call("today", "--json").stdout))


if __name__ == "__main__":
    unittest.main()
