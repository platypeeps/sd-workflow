"""A database with rows on it, for the three screens to render.

The harness proper is `sd_db.testing` and this is not a second one: it seeds
*rows*, which is what these tests are about. Anything that doubles the outside
world -- GitHub, launchd, a home directory -- comes from `sd_db.testing`.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from sd_db import (
    add_note,
    connect,
    create_assignment,
    create_item,
    reads,
    record_cost,
    set_item_fields,
    transition,
    update_assignment,
    upsert_repo,
    upsert_shadow,
)
from sd_db.migrate import initialise
from sd_db.testing import patience
from sd_db.writes import now

NOW = "2026-09-06T12:00:00Z"


class ScreenCase(unittest.TestCase):
    """A fixture database, and the clock frozen at `NOW`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.connection = connect(self.path)
        self.addCleanup(self.connection.close)
        self.now = NOW
        # The Tasks read carries the sweeps' records (sd:5096): a run on this machine must not reach a fixture's page.
        # The build caps read <XDG_CONFIG_HOME>/sd-ai-command-pack (sd:10311): a module run outside the package
        # (`python -m unittest test_build_panel`) read the hub's `repo_cap` 2 and `machine_cap` 8.
        state = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(self.tmp.name) / "state"),
                                             "XDG_CONFIG_HOME": str(Path(self.tmp.name) / "xdg-config"),
                                             "SYSTEM_TOOLS_CONFIG": str(Path(self.tmp.name) / "config")})
        state.start()
        self.addCleanup(state.stop)
        # Today reads Queue's document (sd:5921), which asks this machine's gate and pack: a fixture's Today reads no lane
        # unless its test passes one (now_screen.document's `lanes`). test_today_needs reads the real path.
        from sd_dashboard import now_screen

        lanes = mock.patch.object(now_screen, "_lanes", lambda connection, now: {"lanes": [], "gates": {}, "problems": []})
        lanes.start()
        self.addCleanup(lanes.stop)
        # sd:6324: a 202 start's journal watch (`command_journal.watch`) looked every 5 s, and a look while the
        # folder was removed wrote into it: 'Directory not empty'. It looks often here, and `end_journal_watches`
        # ends it before the folder goes.
        from sd_dashboard import command_journal

        poll = mock.patch.object(command_journal, "POLL_SECONDS", min(command_journal.POLL_SECONDS, 0.1))
        poll.start()
        self.addCleanup(poll.stop)
        self.addCleanup(self.end_journal_watches, set(threading.enumerate()))

    def end_journal_watches(self, before) -> None:
        """Move the database aside, so each journal watch the case started returns at its next look; wait for it."""
        watches = [thread for thread in threading.enumerate()
                   if thread not in before and thread.name.startswith("sd-journal ")]
        if watches and self.path.exists():
            os.replace(self.path, self.path.with_name(self.path.name + ".ended"))
        deadline = time.monotonic() + patience(30)
        for thread in watches:
            thread.join(max(0.0, deadline - time.monotonic()))
        self.assertEqual([thread.name for thread in watches if thread.is_alive()], [], "a journal watch outlived its case")

    # -- seeds -------------------------------------------------------------

    def repo(self, path: str = "/repos/system") -> str:
        upsert_repo(self.connection, path, remote="git@example.invalid:x.git")
        return path

    def item(self, title: str = "an item", **columns) -> int:
        columns.setdefault("kind", "work")
        return create_item(self.connection, title=title, **columns)

    def note(self, item: int, body: str, kind: str = "followup", **columns) -> int:
        """A note; a question is seeded as written, so a page may read one older than pack sd:6708's shape."""
        if kind != "question":
            return add_note(self.connection, item, kind=kind, body=body, **columns)
        names = ["item", "timestamp", "kind", "body", *columns]
        cursor = self.connection.execute(
            f"INSERT INTO note ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
            (item, now(), kind, body, *columns.values()))
        self.connection.commit()
        return int(cursor.lastrowid)

    def age(self, item: int, days: int) -> None:
        """Backdate the item's status history so the row reads as `days` old.

        `status_since` is the newest `status_change` note, and `create_item`
        writes one at the real clock -- so every freshly seeded row is zero
        days old and every one of them lands in the first age bucket. A test
        about buckets needs rows in more than one of them.

        The library has no setter for a note's timestamp, correctly: nothing in
        production backdates history. So the fixture writes the column, which
        is seeding and not a second writer -- it touches `note.timestamp`, and
        never `item.status`.
        """
        when = (
            datetime.strptime(NOW, "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.connection.execute(
            "UPDATE note SET timestamp = ? WHERE item = ? AND kind = 'status_change'",
            (when, item),
        )
        self.connection.commit()

    def schedule(self, item: int, due: str) -> int:
        """Make `item` a weekly occurrence due `due`: a day after NOW's is a scheduled occurrence (pack sd:5648)."""
        set_item_fields(self.connection, item, due=due, recurrence="FREQ=WEEKLY", recurrence_anchor="schedule")
        self.connection.commit()
        return item

    def provider(self, name: str = "opus", **columns) -> str:
        """A `provider` row. `assignment.provider` is a foreign key into it."""
        self.connection.execute(
            "INSERT OR IGNORE INTO provider (name, enabled, author_rank, reviewer_rank) "
            "VALUES (?, ?, ?, ?)",
            (name, int(columns.get("enabled", 1)), columns.get("author_rank"),
             columns.get("reviewer_rank")),
        )
        self.connection.commit()
        return name

    def assignment(self, item: int, **columns) -> int:
        columns.setdefault("role", "author")
        columns.setdefault("status", "queued")
        if columns.get("provider"):
            self.provider(columns["provider"])
        return create_assignment(self.connection, item=item, **columns)

    def render(self, path: str, parameters=None) -> str:
        from sd_dashboard.server import route

        return route(self.connection, path, parameters or {}, now=self.now)


class _Subprocess:
    """A module's `subprocess` with one `Popen` swapped in; every other name is the real module's."""

    def __init__(self, popen):
        self.Popen = popen

    def __getattr__(self, name):
        return getattr(subprocess, name)


def bound_from(module, ready: Path, *, wait: float = 10):
    """Count `module`'s bounded `communicate` from the moment `ready` exists (sd:6299).

    A test of what a timeout kills needs that child to exist first. Under load a double took longer than a 1 s bound
    to start its child and write `ready`, so the kill came first and the test found no pid file. The run's stdin is
    sent and closed before the wait, so a double that reads it first still gets there. The wait ends after
    `patience(wait)` seconds or when the run exits; the test's own read of `ready` then says what went wrong.
    """

    class Popen(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            if timeout is not None and not self._communication_started:
                if self.stdin:
                    with contextlib.suppress(BrokenPipeError):
                        if input:
                            self.stdin.write(input)
                    with contextlib.suppress(BrokenPipeError):
                        self.stdin.close()
                    input = None
                deadline = time.monotonic() + patience(wait)
                while not ready.exists() and self.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
            return super().communicate(input, timeout)

    return mock.patch.object(module, "subprocess", _Subprocess(Popen))


def before_5648():
    """`reads.backlog_items` as a pack before sd:5648 reads it: rows with no `opens_on` and no `scheduled`."""
    real = reads.backlog_items

    def read(connection, **arguments):
        return [{key: row[key] for key in row.keys() if key not in ("opens_on", "scheduled")}
                for row in real(connection, **arguments)]

    return mock.patch.object(reads, "backlog_items", read)


__all__ = [
    "NOW",
    "ScreenCase",
    "before_5648",
    "bound_from",
    "record_cost",
    "transition",
    "update_assignment",
    "upsert_shadow",
]
