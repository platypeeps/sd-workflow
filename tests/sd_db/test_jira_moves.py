"""Move a linked Jira ticket when its sd item changes status (sd:8117)."""

from __future__ import annotations

import copy
import http.client
import os
import socket
import sqlite3
import tempfile
import unittest
import urllib.error
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from sd_db import connect, jira_moves, priority_sweep, progress, shadow_jira, workflow
from sd_db.migrate import initialise
from sd_db.shadow_jira import _http, settings
from sd_db.shadow_sync import sync
from sd_db.writes import create_item, transition

from .test_shadow_jira import JIRA_ENV, RawServer, jira_transport

STATUS = {"To Do": "new", "In Progress": "indeterminate", "Done": "done"}


def refused(code: int, reason: str = "Refused") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://jira.example.test/x", code, reason, None, None)  # type: ignore[arg-type]


def sentence(exception: BaseException) -> str:
    """The error the real transport gives for `exception`: its classification is what these tests rest on."""
    opener = MagicMock()
    opener.open.side_effect = exception
    with patch("sd_db.shadow_jira.urllib.request.build_opener", return_value=opener):
        payload, error = _http("https://jira.example.test/rest/api/3/issue/ABC-7", {}, None)
    assert payload is None and error
    return error


class FakeJira:
    """A Jira of tickets by key: answers the status read, the transition list and the transition."""

    def __init__(self, tickets: dict[str, str], *, fail=None, malformed: dict[tuple[str, str], Any] | None = None):
        self.tickets = dict(tickets)
        self.fail = fail
        #: (key, "status" or "transitions") -> the answer that replaces Jira's own.
        self.malformed = malformed or {}
        self.posted: list[tuple[str, str]] = []
        #: key -> the error every request about that ticket gets.
        self.down: dict[str, str] = {}

    def __call__(self, url, headers, body):
        key = url.split("/issue/")[1].split("/")[0].split("?")[0]
        if key in self.down:
            return None, self.down[key]
        if self.fail:
            return None, self.fail
        which = "status" if url.endswith("?fields=status") else "transitions" if body is None else ""
        if (key, which) in self.malformed:
            return self.malformed[key, which], ""
        if url.endswith("?fields=status"):
            name = self.tickets[key]
            return {"fields": {"status": {"name": name, "statusCategory": {"key": STATUS[name]}}}}, ""
        if body is None:
            return {"transitions": [
                {"id": "11", "to": {"name": "To Do", "statusCategory": {"key": "new"}}},
                {"id": "21", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
                {"id": "41", "to": {"name": "Won't Do", "statusCategory": {"key": "done"}}},
                {"id": "31", "to": {"name": "Done", "statusCategory": {"key": "done"}}}]}, ""
        name = {"11": "To Do", "21": "In Progress", "31": "Done", "41": "Won't Do"}[body["transition"]["id"]]
        self.posted.append((key, name))
        self.tickets[key] = name
        return {}, ""


class TheMoves(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        database = Path(tmp.name) / "sd.db"
        initialise(database)
        self.db = connect(database)
        self.addCleanup(self.db.close)
        self.item = create_item(self.db, kind="task", title="linked", source="task-ref", external_id="jira:ABC-7")
        self.other = create_item(self.db, kind="task", title="not linked")

    def notes(self, item):
        return [row["body"] for row in self.db.execute(
            "SELECT body FROM note WHERE item = ? AND kind = 'comment'", (item,))]

    def test_a_status_change_moves_the_linked_ticket_once(self):
        jira = FakeJira({"ABC-7": "To Do"})
        first = jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertIn("token check on ABC-7 passed", first[0])
        self.assertEqual(jira.posted, [])
        transition(self.db, self.item, "in_progress", who="operator")
        transition(self.db, self.other, "in_progress", who="operator")
        transition(self.db, self.item, "done", who="operator")
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), ["jira moves: ABC-7: moved to Done"])
        # The done-category transition named Done, not the first one offered.
        self.assertEqual(jira.posted, [("ABC-7", "Done")])
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), [])

    def test_linking_a_ticket_to_a_new_item_moves_nothing(self):
        jira = FakeJira({"ABC-7": "To Do", "ABC-9": "Done"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        create_item(self.db, kind="task", title="filed later", source="task-ref", external_id="jira:ABC-9")
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), [])
        self.assertEqual(jira.posted, [])

    def test_a_ticket_already_there_and_a_blocked_item_move_nothing(self):
        jira = FakeJira({"ABC-7": "In Progress"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        transition(self.db, self.item, "in_progress", who="operator")
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), ["jira moves: ABC-7: already In Progress"])
        transition(self.db, self.item, "blocked", who="operator")
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), ["jira moves: ABC-7: blocked moves nothing"])
        self.assertEqual(jira.posted, [])

    def test_a_failure_is_one_note_on_the_item_and_the_cursor_moves_on(self):
        jira = FakeJira({"ABC-7": "To Do"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        jira.fail = "Jira returned 400 Bad Request"
        transition(self.db, self.item, "done", who="operator")
        jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual(self.notes(self.item),
                         ["Jira ticket ABC-7 was not moved to match done: Jira returned 400 Bad Request"])
        jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual(len(self.notes(self.item)), 1)

    def linked(self, *keys):
        return [create_item(self.db, kind="task", title=key, source="task-ref", external_id=f"jira:{key}") for key in keys]

    def held_then_moved_once(self, exception):
        jira = FakeJira({"ABC-7": "To Do"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        held = jira_moves.read_cursor(self.db)
        transition(self.db, self.item, "done", who="operator")
        jira.fail = sentence(exception)
        lines = jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual(jira_moves.read_cursor(self.db), held, "a transient failure holds the cursor")
        self.assertEqual(self.notes(self.item), [], "a transient failure writes no note on the item")
        self.assertEqual(lines, [f"jira moves: ABC-7: not moved, cursor held for the next run: {jira.fail}"])
        jira.fail = None
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), ["jira moves: ABC-7: moved to Done"])
        self.assertEqual(jira.posted, [("ABC-7", "Done")])
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), [])

    def test_a_503_holds_the_cursor_and_a_rerun_moves_the_ticket_once(self):
        self.held_then_moved_once(refused(503, "Service Unavailable"))

    def test_a_timeout_holds_the_cursor_and_a_rerun_moves_the_ticket_once(self):
        self.held_then_moved_once(TimeoutError("timed out"))

    def test_a_refused_connection_holds_the_cursor_and_a_rerun_moves_the_ticket_once(self):
        self.held_then_moved_once(urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")))

    def test_a_dropped_connection_holds_the_cursor_and_a_rerun_moves_the_ticket_once(self):
        self.held_then_moved_once(http.client.RemoteDisconnected("closed"))

    def test_an_expired_token_holds_the_cursor_and_a_rerun_moves_the_ticket_once(self):
        self.held_then_moved_once(refused(401, "Unauthorized"))

    def test_a_forbidden_token_holds_the_cursor_and_a_rerun_moves_the_ticket_once(self):
        self.held_then_moved_once(refused(403, "Forbidden"))

    def test_a_429_midway_keeps_the_earlier_move_and_resumes_at_the_failed_entry(self):
        jira = FakeJira({"ABC-7": "To Do", "ABC-8": "To Do", "ABC-9": "To Do"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        eight, nine = self.linked("ABC-8", "ABC-9")
        for item in (self.item, eight, nine):
            transition(self.db, item, "done", who="operator")
        limit = sentence(refused(429, "Too Many Requests"))
        jira.down["ABC-8"] = limit
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV),
                         ["jira moves: ABC-7: moved to Done",
                          f"jira moves: ABC-8: not moved, cursor held for the next run: {limit}"])
        self.assertEqual(jira.posted, [("ABC-7", "Done")], "the batch stops at the failure")
        jira.down.clear()
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV),
                         ["jira moves: ABC-8: moved to Done", "jira moves: ABC-9: moved to Done"])
        self.assertEqual(jira.posted, [("ABC-7", "Done"), ("ABC-8", "Done"), ("ABC-9", "Done")])

    def test_an_item_changed_on_both_sides_of_another_item_is_handled_once(self):
        jira = FakeJira({"ABC-7": "To Do", "ABC-8": "To Do"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        [eight] = self.linked("ABC-8")
        transition(self.db, self.item, "in_progress", who="operator")
        transition(self.db, eight, "done", who="operator")
        transition(self.db, self.item, "done", who="operator")
        jira.down["ABC-7"] = sentence(refused(503))
        jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual(jira.posted, [("ABC-8", "Done")], "entries run in the order of the note that decides them")
        jira.down.clear()
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), ["jira moves: ABC-7: moved to Done"])
        self.assertEqual(jira.posted, [("ABC-8", "Done"), ("ABC-7", "Done")])

    def test_a_timed_out_transition_that_landed_is_not_posted_again(self):
        jira = FakeJira({"ABC-7": "To Do"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        transition(self.db, self.item, "done", who="operator")
        timeout = sentence(TimeoutError("timed out"))

        def landed(url, headers, body):
            answer = jira(url, headers, body)
            return (None, timeout) if body is not None else answer

        jira_moves.run(self.db, landed, JIRA_ENV)
        self.assertEqual(jira.posted, [("ABC-7", "Done")])
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), ["jira moves: ABC-7: already Done"])
        self.assertEqual(jira.posted, [("ABC-7", "Done")])

    def test_a_permanent_failure_still_passes_and_notes_once(self):
        for what, error in (("404", sentence(refused(404, "Not Found"))),
                            ("409", sentence(refused(409, "Conflict"))),
                            ("a body that does not parse", sentence(ValueError("no json")))):
            with self.subTest(what):
                [item] = self.linked("ABC-8")
                self.addCleanup(self.db.execute, "DELETE FROM note WHERE item = ?", (item,))
                jira = FakeJira({"ABC-7": "To Do", "ABC-8": "To Do"})
                jira_moves.run(self.db, jira, JIRA_ENV)
                jira.down["ABC-8"] = error
                transition(self.db, item, "done", who="operator")
                jira_moves.run(self.db, jira, JIRA_ENV)
                self.assertEqual(self.notes(item), [f"Jira ticket ABC-8 was not moved to match done: {error}"])
                self.assertEqual(jira_moves.read_cursor(self.db),
                                 self.db.execute("SELECT MAX(id) FROM note").fetchone()[0] - 1,
                                 "the cursor passes the failure, up to the status note before this comment")
                jira_moves.run(self.db, jira, JIRA_ENV)
                self.assertEqual(len(self.notes(item)), 1)
                self.db.execute("UPDATE item SET external_id = 'jira:ABC-0' || id WHERE id = ?", (item,))

    def test_no_matching_transition_still_passes_and_notes(self):
        jira = FakeJira({"ABC-7": "To Do"}, malformed={("ABC-7", "transitions"): {"transitions": []}})
        jira_moves.run(self.db, FakeJira({"ABC-7": "To Do"}), JIRA_ENV)
        transition(self.db, self.item, "done", who="operator")
        jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual(self.notes(self.item),
                         ["Jira ticket ABC-7 was not moved to match done: no transition into Done is offered from To Do"])
        jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual(len(self.notes(self.item)), 1)

    def test_a_transient_failure_repeats_no_note_written_before_it(self):
        jira = FakeJira({"ABC-7": "To Do", "ABC-8": "To Do"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        [eight] = self.linked("ABC-8")
        transition(self.db, self.item, "done", who="operator")
        transition(self.db, eight, "done", who="operator")
        jira.down.update({"ABC-7": sentence(refused(404, "Not Found")), "ABC-8": sentence(refused(503))})
        jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual(len(self.notes(self.item)), 1)
        del jira.down["ABC-8"]
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), ["jira moves: ABC-8: moved to Done"])
        self.assertEqual(len(self.notes(self.item)), 1, "the note before the held cursor is not written again")

    def test_a_crash_midway_repeats_no_note_written_before_it(self):
        jira = FakeJira({"ABC-7": "To Do", "ABC-8": "To Do"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        [eight] = self.linked("ABC-8")
        transition(self.db, self.item, "done", who="operator")
        transition(self.db, eight, "done", who="operator")
        jira.fail = "Jira returned 400 Bad Request"
        real = workflow.add_item_note

        def fails_on_the_second(connection, item, **options):
            if item == eight:
                raise sqlite3.OperationalError("database is locked")
            return real(connection, item, **options)

        with patch.object(workflow, "add_item_note", side_effect=fails_on_the_second):
            with self.assertRaises(sqlite3.OperationalError):
                jira_moves.run(self.db, jira, JIRA_ENV)
        jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual((len(self.notes(self.item)), len(self.notes(eight))), (1, 1))

    def test_a_failed_token_check_holds_the_cursor(self):
        jira = FakeJira({"ABC-7": "To Do"}, fail="Jira rejected the credentials (401 Unauthorized)")
        lines = jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertIn("token check on ABC-7 failed, cursor held", lines[0])
        self.assertIsNone(jira_moves.read_cursor(self.db))

    def test_an_unconfigured_jira_moves_nothing(self):
        self.assertEqual(jira_moves.run(self.db, FakeJira({}), {}), [])
        self.assertIsNone(jira_moves.read_cursor(self.db))

    def test_the_hourly_jira_sync_runs_the_moves(self):
        collect = jira_transport([{"issues": [], "isLast": True}])
        jira = FakeJira({"ABC-7": "To Do"})

        def transport(url, headers, body):
            return jira(url, headers, body) if "/issue/" in url else collect(url, headers, body)

        now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
        with patch.dict(os.environ, {**os.environ, **JIRA_ENV}, clear=True):
            sync(self.db, tracker="jira", now=now, runner=transport)
            transition(self.db, self.item, "done", who="operator")
            result = sync(self.db, tracker="jira", now=now, runner=transport)
        self.assertIn("shadow sync: jira moves: ABC-7: moved to Done", result.report())


    def test_a_cancelled_item_leaves_its_ticket_alone(self):
        jira = FakeJira({"ABC-7": "To Do"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        progress.cancel_work(self.db, self.item, reason="nobody needs it", who="operator", guard=progress.task_guard)
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), ["jira moves: ABC-7: cancelled; ticket left as it is"])
        self.assertEqual(jira.posted, [])

    def test_a_priority_sweep_closure_leaves_its_ticket_alone(self):
        jira = FakeJira({"ABC-7": "To Do"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        now = datetime(2026, 11, 8, tzinfo=timezone.utc)
        self.db.execute("UPDATE item SET created_at = ? WHERE id = ?",
                        ((now - timedelta(days=priority_sweep.CLOSE_DAYS + 1)).isoformat(), self.item))
        self.db.commit()
        priority_sweep.sweep(self.db, who="sweep", now=now)
        self.assertEqual(self.db.execute("SELECT status FROM item WHERE id = ?", (self.item,)).fetchone()[0], "done")
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV),
                         ["jira moves: ABC-7: closed by the priority sweep; ticket left as it is"])
        self.assertEqual(jira.posted, [])

    def test_a_reopened_cancel_moves_again_when_finished(self):
        jira = FakeJira({"ABC-7": "To Do"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        progress.cancel_work(self.db, self.item, reason="nobody needs it", who="operator", guard=progress.task_guard)
        jira_moves.run(self.db, jira, JIRA_ENV)
        workflow.change_status(self.db, self.item, "in_progress", who="operator")
        workflow.change_status(self.db, self.item, "done", who="operator")
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), ["jira moves: ABC-7: moved to Done"])

    def test_a_done_item_whose_fields_cannot_be_read_moves_nothing(self):
        for raw in ("{not json", "[1]", "[" * 200000):
            with self.subTest(raw=raw[:9]):
                jira = FakeJira({"ABC-7": "To Do"})
                self.db.execute("DELETE FROM state WHERE key = ?", (jira_moves.CURSOR,))
                self.db.execute("DELETE FROM note WHERE item = ? AND kind = 'comment'", (self.item,))
                self.db.execute("UPDATE item SET status = 'planning', fields = NULL WHERE id = ?", (self.item,))
                self.db.commit()
                jira_moves.run(self.db, jira, JIRA_ENV)
                transition(self.db, self.item, "done", who="operator")
                self.db.execute("UPDATE item SET fields = ? WHERE id = ?", (raw, self.item))
                self.db.commit()
                self.assertIn("its stored fields cannot be read", jira_moves.run(self.db, jira, JIRA_ENV)[0])
                self.assertEqual(jira.posted, [])
                self.assertEqual(len(self.notes(self.item)), 1)

    def test_a_malformed_answer_fails_that_item_and_the_next_still_moves(self):
        ticket = {"name": "To Do", "statusCategory": {"key": "new"}}
        moved = {"id": "31", "to": {"name": "Done", "statusCategory": {"key": "done"}}}
        cases: list[tuple[str, Any]] = [
            ("status", [1]), ("status", None), ("status", {}), ("status", {"fields": [1]}), ("status", {"fields": "x"}),
            ("status", {"fields": {}}), ("status", {"fields": {"status": [1]}}),
            ("status", {"fields": {"status": {**ticket, "statusCategory": [1]}}}),
            ("status", {"fields": {"status": {**ticket, "statusCategory": {"key": 3}}}}),
            ("status", {"fields": {"status": {**ticket, "statusCategory": {"key": ""}}}}),
            ("status", {"fields": {"status": {"statusCategory": {"key": "new"}}}}),
            ("transitions", [1]), ("transitions", None), ("transitions", {}), ("transitions", {"transitions": 5}),
            ("transitions", {"transitions": "abc"}), ("transitions", {"transitions": [1]}),
            ("transitions", {"transitions": [{"id": "31"}]}), ("transitions", {"transitions": [{"id": "31", "to": 1}]}),
            ("transitions", {"transitions": [{"id": "31", "to": {"name": "Done", "statusCategory": [1]}}]}),
            ("transitions", {"transitions": [{"id": "31", "to": {"name": None, "statusCategory": {"key": "done"}}}]}),
            ("transitions", {"transitions": [{"to": moved["to"]}]}),
            ("transitions", {"transitions": [{"id": None, "to": moved["to"]}]}),
            ("transitions", {"transitions": [moved, 1]}),
        ]
        healthy = create_item(self.db, kind="task", title="linked too", source="task-ref", external_id="jira:ABC-8")
        for which, answer in cases:
            with self.subTest(which=which, answer=answer):
                jira = FakeJira({"ABC-7": "To Do", "ABC-8": "To Do"}, malformed={("ABC-7", which): answer})
                self.db.execute("DELETE FROM state WHERE key = ?", (jira_moves.CURSOR,))
                self.db.execute("DELETE FROM note WHERE item = ? AND kind = 'comment'", (self.item,))
                self.db.execute("UPDATE item SET status = 'planning' WHERE id IN (?, ?)", (self.item, healthy))
                self.db.commit()
                jira_moves.run(self.db, jira, JIRA_ENV)
                held = jira_moves.read_cursor(self.db)
                transition(self.db, self.item, "done", who="operator")
                transition(self.db, healthy, "done", who="operator")
                lines = jira_moves.run(self.db, jira, JIRA_ENV)
                self.assertEqual(jira.posted, [("ABC-8", "Done")])
                self.assertEqual(lines[-1], "jira moves: ABC-8: moved to Done")
                self.assertEqual(len(self.notes(self.item)), 1)
                self.assertGreater(jira_moves.read_cursor(self.db), held)

    def test_a_malformed_token_check_holds_the_cursor(self):
        for answer in ([1], None, {}, {"transitions": 5}, {"transitions": [1]}):
            with self.subTest(answer=answer):
                jira = FakeJira({"ABC-7": "To Do"}, malformed={("ABC-7", "transitions"): answer})
                lines = jira_moves.run(self.db, jira, JIRA_ENV)
                self.assertIn("token check on ABC-7 failed, cursor held", lines[0])
                self.assertIsNone(jira_moves.read_cursor(self.db))

    def test_a_crash_before_the_cursor_write_moves_no_ticket_twice(self):
        jira = FakeJira({"ABC-7": "To Do"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        transition(self.db, self.item, "done", who="operator")
        with patch.object(jira_moves, "_write_cursor", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual(jira.posted, [("ABC-7", "Done")])
        self.assertEqual(jira_moves.run(self.db, jira, JIRA_ENV), ["jira moves: ABC-7: already Done"])
        self.assertEqual(jira.posted, [("ABC-7", "Done")])

    def test_a_failed_note_write_holds_the_cursor_and_the_next_run_says_it(self):
        jira = FakeJira({"ABC-7": "To Do"})
        jira_moves.run(self.db, jira, JIRA_ENV)
        held = jira_moves.read_cursor(self.db)
        jira.fail = "Jira returned 400 Bad Request"
        transition(self.db, self.item, "done", who="operator")
        with patch.object(workflow, "add_item_note", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual(jira_moves.read_cursor(self.db), held)
        jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual(len(self.notes(self.item)), 1)


#: Every shape a JSON value can take, at its extremes, and a Python nest deeper than any answer.
JUNK: list[Any] = [None, True, False, 0, -1, 2**70, 1e308, float("inf"), float("nan"), 5e-324, "", "x", [], [1], [[]],
                   {}, {"a": 1}, {"statusCategory": 1}, {"key": []}]
DEEP: list[Any] = []
for _ in range(200):
    DEEP = [DEEP]
JUNK.append(DEEP)
STATUS_ANSWER: dict[str, Any] = {"fields": {"status": {"name": "To Do", "statusCategory": {"key": "new"}}}}
TRANSITIONS_ANSWER: dict[str, Any] = {"transitions": [
    {"id": "11", "to": {"name": "To Do", "statusCategory": {"key": "new"}}},
    {"id": "31", "to": {"name": "Done", "statusCategory": {"key": "done"}}}]}


def paths(node: Any, at: tuple = ()) -> Iterator[tuple]:
    """Every position in a JSON tree, the root included."""
    yield at
    if isinstance(node, dict):
        for key, value in node.items():
            yield from paths(value, (*at, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from paths(value, (*at, index))


def replaced(tree: Any, at: tuple, value: Any) -> Any:
    if not at:
        return value
    tree = copy.deepcopy(tree)
    node = tree
    for step in at[:-1]:
        node = node[step]
    node[at[-1]] = value
    return tree


def scripted(answers: dict[str, Any], posted: list[dict]):
    """A transport that gives `answers` and records each transition body it is sent."""

    def transport(url, headers, body):
        if body is not None:
            posted.append(body)
            return {}, ""
        return answers["status" if url.endswith("?fields=status") else "transitions"], ""

    return transport


class ThePropertyOfTheAnswers(unittest.TestCase):
    def test_junk_at_any_position_is_a_result_or_a_value_error(self):
        config = settings(JIRA_ENV)
        for status in ("done", "planning", "in_progress"):
            for which, tree in (("status", STATUS_ANSWER), ("transitions", TRANSITIONS_ANSWER)):
                for at in paths(tree):
                    for junk in JUNK:
                        answers = {"status": STATUS_ANSWER, "transitions": TRANSITIONS_ANSWER, which: replaced(tree, at, junk)}
                        posted: list[dict] = []
                        transport = scripted(answers, posted)
                        with self.subTest(status=status, which=which, at=at, junk=repr(junk)[:20]):
                            try:
                                said = jira_moves.move(config, "ABC-7", status, transport)
                            except ValueError:
                                self.assertEqual(posted, [])
                            else:
                                self.assertIsInstance(said, str)
                                for body in posted:
                                    self.assertRegex(body["transition"]["id"], r"\S")


class TheFailureClasses(unittest.TestCase):
    def test_the_transport_marks_what_a_later_run_can_retry(self):
        transient = [refused(401), refused(403), refused(429), refused(500), refused(502), refused(503), refused(504), TimeoutError("timed out"),
                     socket.gaierror(8, "no such host"), ConnectionResetError(54, "reset"),
                     urllib.error.URLError(ConnectionRefusedError(61, "refused")), urllib.error.URLError("timed out"),
                     http.client.RemoteDisconnected("closed"), http.client.IncompleteRead(b""),
                     http.client.BadStatusLine("x"), urllib.error.URLError(http.client.RemoteDisconnected("closed"))]
        permanent = [refused(400), refused(404), refused(409), refused(304),
                     ValueError("no json"), RecursionError(), http.client.InvalidURL("bad"),
                     urllib.error.URLError(http.client.InvalidURL("bad")), urllib.error.URLError(ValueError("bad"))]
        for exception, wanted in [(e, True) for e in transient] + [(e, False) for e in permanent]:
            with self.subTest(repr(exception)):
                self.assertIs(isinstance(sentence(exception), shadow_jira.Transient), wanted)


class TheEmptyAnswer(unittest.TestCase):
    def test_a_204_with_no_body_is_an_empty_payload(self):
        server = RawServer(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        self.addCleanup(server.close)
        self.assertEqual(_http(server.url + "/rest/api/3/issue/ABC-7/transitions", {}, {"transition": {}}), ({}, ""))

    def test_a_nest_too_deep_to_parse_is_a_failed_request(self):
        deep = b"[" * 200000
        server = RawServer(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(deep) + deep)
        self.addCleanup(server.close)
        self.assertEqual(_http(server.url + "/rest/api/3/issue/ABC-7", {}, None),
                         (None, "the request to Jira failed: RecursionError"))


if __name__ == "__main__":
    unittest.main()
