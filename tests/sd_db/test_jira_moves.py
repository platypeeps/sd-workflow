"""Move a linked Jira ticket when its sd item changes status (sd:8117)."""

from __future__ import annotations

import copy
import os
import sqlite3
import tempfile
import unittest
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sd_db import connect, jira_moves, priority_sweep, progress, workflow
from sd_db.migrate import initialise
from sd_db.shadow_jira import _http, settings
from sd_db.shadow_sync import sync
from sd_db.writes import create_item, transition

from .test_shadow_jira import JIRA_ENV, RawServer, jira_transport

STATUS = {"To Do": "new", "In Progress": "indeterminate", "Done": "done"}


class FakeJira:
    """A Jira of tickets by key: answers the status read, the transition list and the transition."""

    def __init__(self, tickets: dict[str, str], *, fail=None, malformed: dict[tuple[str, str], Any] | None = None):
        self.tickets = dict(tickets)
        self.fail = fail
        #: (key, "status" or "transitions") -> the answer that replaces Jira's own.
        self.malformed = malformed or {}
        self.posted: list[tuple[str, str]] = []

    def __call__(self, url, headers, body):
        key = url.split("/issue/")[1].split("/")[0].split("?")[0]
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
        jira.fail = "Jira returned 500 Server Error"
        transition(self.db, self.item, "done", who="operator")
        jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual(self.notes(self.item),
                         ["Jira ticket ABC-7 was not moved to match done: Jira returned 500 Server Error"])
        jira_moves.run(self.db, jira, JIRA_ENV)
        self.assertEqual(len(self.notes(self.item)), 1)

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
