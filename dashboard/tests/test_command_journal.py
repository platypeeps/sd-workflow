"""The pages' commands in the execution journal (sd:5541).

Both writers the Commands lamp read retired, so a button that runs something left no record. `command_journal` writes
one `exec` note for each command a POST runs, in the palette's record shape, once it answered: ended with its answer,
or, for a run a 202 starts, open until that run's own reader says it ended. A refusal ran nothing and writes nothing. A stand-in kit plays Build deck and
an in-memory launchd plays Run now; no real job, deck or outward call runs.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from sd_dashboard import activity_screen, command_journal, research_deck
from sd_db import operations, runner_exec, writes

from .support import ScreenCase
from .test_operations_actions import FakeBackend
from .test_research_deck import FAKE_KIT, brief
from .test_workflow_actions import BrowserSession


def records(connection):
    return runner_exec.execution_journal(connection)["executions"]


def ended(connection, note, seconds=20):
    """The note's row once it has ended; fails the test past `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        row = connection.execute("SELECT * FROM note WHERE id=?", (note,)).fetchone()
        if row["ended"]:
            return row
        time.sleep(0.05)
    raise AssertionError(f"note {note} did not end within {seconds} s")


class ThePost(BrowserSession):
    """One record for each command a page posts, and none for a write that only changes the database."""

    def setUp(self):
        self.backend = FakeBackend()
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        bin_dir = base / "pack" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "sd-research-kit").write_text(FAKE_KIT, encoding="utf-8")
        self.enterContext(mock.patch("sd_dashboard.pack_bin", return_value=bin_dir))
        self.root = base / "repos"
        brief(self.root, "research/alpha")
        self.enterContext(mock.patch.dict(os.environ, {"REPO_ROOT": str(self.root), "FAKE_MODE": "write"}))
        self.enterContext(mock.patch.object(command_journal, "POLL_SECONDS", 0.05))
        super().setUp()

    def test_run_now_writes_one_record_ended_with_its_answer(self):
        state = operations.job_state(self.connection, "failed-fixture", backend=self.backend)
        status, _, _ = self.post("/api/jobs/failed-fixture/retry", {"revision": state["revision"]})
        self.assertEqual(status, 200)
        [record] = records(self.connection)
        self.assertEqual((record["command"], record["scope"], record["exit_code"], record["session"]),
                         ("Run now · failed-fixture", "dashboard", 0, "local"))
        self.assertIsNotNone(record["ended"])
        self.assertEqual(record["title"], command_journal.HOLDER_TITLE)
        output = runner_exec.read_execution(self.connection, record["id"])
        self.assertEqual(output["state"], "finished")
        self.assertRegex(output["output"], r"^Run now · failed-fixture\nPOST /api/jobs/failed-fixture/retry by local at "
                                           r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00\nanswered 200: ")

    def test_a_refused_command_ran_nothing_and_writes_nothing(self):
        """A 4xx wrote nothing (the pages' contract): a stale revision raised, and a busy deck answered 409."""
        before = self.snapshot()
        self.assertEqual(self.post("/api/jobs/failed-fixture/retry", {"revision": "0" * 64})[0], 409)
        with mock.patch("sd_dashboard.research_deck.start", return_value=(409, {"error": "A deck is already building"})):
            self.assertEqual(self.post("/api/research/deck", {"repo": "research/alpha"})[0], 409)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.backend.calls, [])

    def test_a_command_that_failed_ends_failed_and_its_record_is_no_operator_action(self):
        def actions():
            return self.connection.execute("SELECT count(*) FROM state WHERE key='operator-action'").fetchone()[0]

        with mock.patch("sd_dashboard.research_slides.start", side_effect=OSError("Slides did not answer")):
            self.assertEqual(self.post("/api/research/slides", {"repo": "research/alpha"})[0], 503)
        [failed] = records(self.connection)
        self.assertEqual((failed["command"], failed["exit_code"]), ("Import to Slides · research/alpha", 1))
        self.assertIn("raised OSError: Slides did not answer", runner_exec.read_execution(self.connection, failed["id"])["output"])
        with mock.patch("sd_dashboard.research_slides.start", return_value={"url": "https://example.test/deck"}):
            self.assertEqual(self.post("/api/research/slides", {"repo": "research/alpha"})[0], 200)
        self.assertEqual([r["exit_code"] for r in records(self.connection)], [0, 1])
        self.assertEqual(actions(), 0, "an action that wrote nothing is no operator action; the journal's writes are not its")

    def test_a_write_that_only_changes_the_database_is_no_command(self):
        self.assertEqual(self.post("/api/items", {"title": "Book the review"})[0], 201)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM note WHERE kind='exec'").fetchone()[0], 0)

    def test_build_deck_is_recorded_when_it_starts_and_ended_when_its_run_ends(self):
        status, _, run = self.post("/api/research/deck", {"repo": "research/alpha"})
        self.assertEqual(status, 202, run)
        [note] = [row[0] for row in self.connection.execute("SELECT id FROM note WHERE kind='exec'")]
        row = ended(self.connection, note)
        self.assertEqual(row["exit_code"], 0)
        body = json.loads(row["body"])
        self.assertEqual((body["command"], body["run"]), ("Build deck · research/alpha", {"kind": "deck", "id": run["id"]}))
        output = runner_exec.read_execution(self.connection, note)["output"]
        self.assertIn(f"run {run['id']} done\n", output)
        self.assertIn("wrote docs/dashboard/deck.pptx: 3 slides", output, "the run's own log is copied in")

    def test_a_deck_that_fails_ends_failed(self):
        with mock.patch.dict(os.environ, {"FAKE_MODE": "fail"}):
            status, _, run = self.post("/api/research/deck", {"repo": "research/alpha"})
            self.assertEqual(status, 202, run)
            [note] = [row[0] for row in self.connection.execute("SELECT id FROM note WHERE kind='exec'")]
            self.assertEqual(ended(self.connection, note)["exit_code"], 1)
        self.assertIn("at most 6 cards", runner_exec.read_execution(self.connection, note)["output"])

    def test_an_item_route_names_its_item_and_leaves_the_items_revision_alone(self):
        """A note on the item would move the revision the answer handed the page: the record is the holder's."""
        from sd_db import workflow

        item = self.item("Fix me")
        revision = workflow.item_state(self.connection, item)["revision"]
        with mock.patch("sd_dashboard.fix_session.start", return_value={"started": True}) as start:
            self.assertEqual(self.post(f"/api/items/{item}/fix", {"revision": revision})[0], 200)
        self.assertEqual(start.call_count, 1)
        [record] = records(self.connection)
        self.assertEqual((record["command"], record["exit_code"]), (f"Fix session · sd:{item}", 0))
        self.assertEqual(workflow.item_state(self.connection, item)["revision"], revision)
        holder = self.connection.execute("SELECT id FROM item WHERE source=? AND external_id=?",
                                         command_journal.HOLDER).fetchone()[0]
        self.assertEqual(record["item"], holder)

    def test_a_journal_that_cannot_be_written_does_not_stop_the_command(self):
        (self.path.parent / "executions").write_text("not a folder", encoding="utf-8")
        state = operations.job_state(self.connection, "failed-fixture", backend=self.backend)
        said = io.StringIO()
        with redirect_stderr(said):
            self.assertEqual(self.post("/api/jobs/failed-fixture/retry", {"revision": state["revision"]})[0], 200)
        self.assertEqual(self.backend.calls, [("retry", "fixture/failed-fixture")])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM note WHERE kind='exec'").fetchone()[0], 0)
        self.assertIn("command journal: /api/jobs/failed-fixture/retry ran unrecorded", said.getvalue())

    def test_activity_shows_the_record_with_its_label_and_source(self):
        state = operations.job_state(self.connection, "failed-fixture", backend=self.backend)
        self.post("/api/jobs/failed-fixture/retry", {"revision": state["revision"]})
        [record] = records(self.connection)
        doc = activity_screen.document(self.connection, now=writes.now(), jobs_backend=self.backend)
        [event] = [e for e in doc["events"] if e["k"] == "command"]
        self.assertEqual((event["what"], event["source"], event["scope"], event["note"], event["s"]),
                         ("Run now · failed-fixture · exit 0", "dashboard", "dashboard", record["id"], "ok"))
        status, _, body = self.request(f"/api/executions/{record['id']}", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        self.assertIn("answered 200", json.loads(body)["output"])


class TheSteps(ScreenCase):
    """A detached run's record between its steps: the next look ends it, or leaves it open on a failed read."""

    def setUp(self):
        super().setUp()
        self.state = research_deck.state_dir() / "runs"
        self.state.mkdir(parents=True)
        self.watching = mock.patch.object(command_journal, "watch")  # each look is the test's own
        self.watch = self.watching.start()
        self.addCleanup(self.watching.stop)

    def opened(self, path="/api/research/deck", payload=None, answer=(202, {"id": "deck-20261020T120000Z"})):
        note = command_journal.record(self.path, self.connection, path, payload or {"repo": "research/alpha"}, "local",
                                      writes.now(), answer)
        self.assertIsNotNone(note)
        return note

    def deck_run(self, run_id, state, **fields):
        document = {"id": run_id, "state": state, "reason": None, "repo": "research/alpha", "started": writes.now(),
                    "finished": None, "pid": os.getpid(), **fields}
        (self.state / f"{run_id}.json").write_text(json.dumps(document), encoding="utf-8")
        (self.state / f"{run_id}.log").write_text("kit said its piece\n", encoding="utf-8")

    def test_a_started_run_is_open_and_watched(self):
        note = self.opened()
        row = self.connection.execute("SELECT * FROM note WHERE id=?", (note,)).fetchone()
        self.assertEqual((row["ended"], row["exit_code"]), (None, None))
        self.watch.assert_called_once_with(self.path, note)

    def test_a_run_left_open_is_watched_again_at_the_next_start(self):
        note = self.opened()
        self.deck_run("deck-20261020T120000Z", "done")
        self.watching.stop()
        with mock.patch.object(command_journal, "POLL_SECONDS", 0.05):
            command_journal.resume(self.path)
            row = ended(self.connection, note)
        self.assertEqual(row["exit_code"], 0)
        self.assertIn("kit said its piece", runner_exec.read_execution(self.connection, note)["output"])

    def test_a_run_whose_state_does_not_read_stays_open(self):
        note = self.opened()
        (self.state / "deck-20261020T120000Z.json").write_text("{not json", encoding="utf-8")
        with redirect_stderr(io.StringIO()) as said:
            self.assertFalse(command_journal.settle(self.connection, note))
        self.assertIsNone(self.connection.execute("SELECT ended FROM note WHERE id=?", (note,)).fetchone()[0])
        self.assertIn("did not read, so it stays open", said.getvalue())

    def test_another_runs_end_is_not_this_records(self):
        note = self.opened()
        self.deck_run("deck-20261020T120000Z", "running")
        self.deck_run("deck-20261020T110000Z", "done")
        self.assertFalse(command_journal.settle(self.connection, note))
        self.assertIsNone(self.connection.execute("SELECT ended FROM note WHERE id=?", (note,)).fetchone()[0])

    def test_a_run_whose_state_file_is_gone_ends_unknown(self):
        note = self.opened()
        self.assertTrue(command_journal.settle(self.connection, note))
        self.assertEqual(self.connection.execute("SELECT exit_code FROM note WHERE id=?", (note,)).fetchone()[0],
                         command_journal.UNKNOWN)

    def test_a_run_past_its_bound_ends_unknown(self):
        note = self.opened()
        self.deck_run("deck-20261020T120000Z", "running")
        self.watching.stop()
        command_journal.watch(self.path, note, wait=lambda seconds: None, seconds=0)
        row = self.connection.execute("SELECT * FROM note WHERE id=?", (note,)).fetchone()
        self.assertEqual(row["exit_code"], command_journal.UNKNOWN)
        self.assertIn("did not end within 0 s", runner_exec.read_execution(self.connection, note)["output"])

    def test_a_watch_stops_when_its_database_is_gone(self):
        self.watching.stop()
        waited = []
        command_journal.watch(Path(self.tmp.name) / "gone.db", 1, wait=waited.append)
        self.assertEqual(waited, [])

    def test_a_202_that_names_no_run_ends_unknown(self):
        note = self.opened(answer=(202, {"started": True}))
        self.assertEqual(self.connection.execute("SELECT exit_code FROM note WHERE id=?", (note,)).fetchone()[0],
                         command_journal.UNKNOWN)
        self.watch.assert_not_called()

    def test_an_answer_that_says_its_outcome_is_unknown_ends_unknown(self):
        note = self.opened("/api/skills/intake", {"source": "https://example.test/x"},
                           (502, {"error": "no answer", "outcome": "unknown"}))
        self.assertEqual(self.connection.execute("SELECT exit_code FROM note WHERE id=?", (note,)).fetchone()[0],
                         command_journal.UNKNOWN)

    def test_the_holder_item_is_made_once(self):
        first, second = self.opened(), self.opened("/api/shared-upload/regenerate", {}, {"url": "https://example.test"})
        items = {row[0] for row in self.connection.execute("SELECT item FROM note WHERE id IN (?, ?)", (first, second))}
        self.assertEqual(len(items), 1)
        [holder] = items
        row = self.connection.execute("SELECT * FROM item WHERE id=?", (holder,)).fetchone()
        self.assertEqual((row["source"], row["external_id"], row["status"]), (*command_journal.HOLDER, "done"))

    def test_a_sweep_record_older_than_this_run_is_not_its_end(self):
        started = "2026-10-20T12:00:00+00:00"
        note = self.opened("/api/tasks/sweep", {"realm": "work"}, (202, {"started": started, "repo": "/x", "command": "c"}))
        log = Path(self.tmp.name) / "start.log"
        log.write_text("started\n", encoding="utf-8")
        older = {"running": False, "error": "", "start_log": str(log),
                 "last": {"state": "done", "started": "2026-10-20T11:00:00+00:00", "exit": 0}}
        ours = {**older, "last": {"state": "failed", "started": "2026-10-20T12:00:01+00:00", "exit": 3, "reason": "no mail"}}
        with mock.patch("sd_dashboard.sweep.status", return_value=older) as status:
            self.assertTrue(command_journal.settle(self.connection, note))
        status.assert_called_once_with(Path("/x"), "work")
        self.assertEqual(self.connection.execute("SELECT exit_code FROM note WHERE id=?", (note,)).fetchone()[0],
                         command_journal.UNKNOWN)
        again = self.opened("/api/tasks/sweep", {"realm": "work"}, (202, {"started": started, "repo": "/x", "command": "c"}))
        with mock.patch("sd_dashboard.sweep.status", return_value=ours):
            self.assertTrue(command_journal.settle(self.connection, again))
        self.assertEqual(self.connection.execute("SELECT exit_code FROM note WHERE id=?", (again,)).fetchone()[0], 3)
        self.assertIn("work sweep failed: no mail", runner_exec.read_execution(self.connection, again)["output"])

    def test_a_shadow_sync_the_server_did_not_run_last_is_not_this_records(self):
        from sd_dashboard.shadow_run import RUN

        note = self.opened("/api/shadow/sync", {}, (202, {"started": "2026-10-20T12:00:00Z", "running": True}))
        with mock.patch.object(RUN, "status", return_value={"running": False, "started": "2026-10-20T13:00:00Z",
                                                            "trackers": [], "error": None}):
            self.assertTrue(command_journal.settle(self.connection, note))
        self.assertEqual(self.connection.execute("SELECT exit_code FROM note WHERE id=?", (note,)).fetchone()[0],
                         command_journal.UNKNOWN)

    def test_the_palettes_execute_and_reconcile_refuse_a_pages_record(self):
        """The record keeps the palette's shape, not its authority: neither palette route runs or closes it."""
        from sd_db import workflow

        note = self.opened()
        for operation in (runner_exec.execute_immediate, runner_exec.reconcile):
            with self.subTest(operation=operation.__name__), self.assertRaises(workflow.WorkflowError):
                operation(self.connection, note)
        self.assertIsNone(self.connection.execute("SELECT ended FROM note WHERE id=?", (note,)).fetchone()[0])

    def test_attach_writes_the_database_only_and_is_no_command(self):
        self.assertIsNone(command_journal.record(self.path, self.connection, "/api/research/experiments/act",
                                                 {"verb": "attach", "project": "p", "page": "0" * 32}, "local",
                                                 writes.now(), {"linked": True}))
        self.assertIsNotNone(command_journal.command("/api/research/experiments/act", {"verb": "list"}))

    def test_the_lane_build_github_and_drive_routes_are_commands_with_their_targets(self):
        """sd:7274: each starts a process or calls out (sd-ship lane, launchctl kickstart, GitHub, Drive)."""
        cases = (("/api/queue/move", {"repo": "/r/system", "item": 12, "action": "hold", "revision": "r"}, "Lane · hold sd:12"),
                 ("/api/queue/builds/retry", {"assignment": 7, "revision": "r"}, "Retry build · assignment 7"),
                 ("/api/queue/builds/retry", {"assignment": 7, "revision": "r", "finding": "Fix the test"},
                  "Retry build · assignment 7 with a finding"),
                 ("/api/tasks/build", {"items": [12, 13], "revisions": {}}, "Build · sd:12, sd:13"),
                 ("/api/today/github/copilot-review", {"repo": "example-org/system", "number": 41},
                  "Request Copilot · example-org/system 41"),
                 ("/api/hoa/attach", {"asset": "pump-2", "link": "https://drive.google.com/x", "name": "Manual"},
                  "Attach · pump-2 Manual"))
        for path, payload, label in cases:
            with self.subTest(path=path, label=label):
                note = command_journal.record(self.path, self.connection, path, payload, "local", writes.now(), {"ok": True})
                self.assertIsNotNone(note, path)
                self.assertEqual(json.loads(self.connection.execute("SELECT body FROM note WHERE id=?", (note,)).fetchone()[0])["command"], label)

    def test_activity_shows_how_a_pages_command_ended_not_the_holders_title(self):
        """sd:7274: a failed run's detail is its reason, a failed answer's its error; a success at once has none, and a
        log that does not read says so."""
        reason = "sd-research-kit render exited 1: dashboard: no command by hand has executed this research.conf.py"
        failed = self.opened("/api/research/render", answer=(202, {"id": "render-20261020T120000Z"}))
        self.deck_run("render-20261020T120000Z", "failed", reason=reason)
        self.assertTrue(command_journal.settle(self.connection, failed))
        raised = command_journal.record(self.path, self.connection, "/api/research/slides", {"repo": "research/alpha"},
                                        "local", writes.now(), failure=OSError("Slides did not answer"))
        fine = command_journal.record(self.path, self.connection, "/api/research/slides", {"repo": "research/alpha"},
                                      "local", writes.now(), {"url": "https://example.test/deck"})
        gone = command_journal.record(self.path, self.connection, "/api/research/slides", {"repo": "research/beta"},
                                      "local", writes.now(), failure=OSError("Drive did not answer"))
        Path(runner_exec.read_execution(self.connection, gone)["output_path"]).unlink()
        doc = activity_screen.document(self.connection, now=writes.now(), jobs_backend=FakeBackend())
        detail = {event["note"]: event["detail"] for event in doc["events"] if event["k"] == "command"}
        self.assertEqual(detail[failed], f"run render-20261020T120000Z failed: {reason}")
        self.assertEqual(detail[raised], "raised OSError: Slides did not answer")
        self.assertEqual(detail[fine], "")
        self.assertEqual(detail[gone], "its log is empty or gone, so how it ended is not read")
        self.assertNotIn(command_journal.HOLDER_TITLE, detail.values())

    def test_a_route_that_journals_itself_or_runs_nothing_is_no_command(self):
        """Home writes its own record on its own item (`home_screen._journal`); a chat question runs nothing."""
        for path in ("/api/home/service", "/api/chat", "/api/palette/prepare"):
            self.assertIsNone(command_journal.command(path, {}), path)


if __name__ == "__main__":
    import unittest

    unittest.main()
