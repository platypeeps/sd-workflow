"""Move a linked Jira ticket when its sd item changes status (sd:8117)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, jira_moves
from sd_db.migrate import initialise
from sd_db.shadow_jira import _http
from sd_db.shadow_sync import sync
from sd_db.writes import create_item, transition

from .test_shadow_jira import JIRA_ENV, RawServer, jira_transport

STATUS = {"To Do": "new", "In Progress": "indeterminate", "Done": "done"}


class FakeJira:
    """A Jira of tickets by key: answers the status read, the transition list and the transition."""

    def __init__(self, tickets: dict[str, str], *, fail=None):
        self.tickets = dict(tickets)
        self.fail = fail
        self.posted: list[tuple[str, str]] = []

    def __call__(self, url, headers, body):
        key = url.split("/issue/")[1].split("/")[0].split("?")[0]
        if self.fail:
            return None, self.fail
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


class TheEmptyAnswer(unittest.TestCase):
    def test_a_204_with_no_body_is_an_empty_payload(self):
        server = RawServer(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        self.addCleanup(server.close)
        self.assertEqual(_http(server.url + "/rest/api/3/issue/ABC-7/transitions", {}, {"transition": {}}), ({}, ""))


if __name__ == "__main__":
    unittest.main()
