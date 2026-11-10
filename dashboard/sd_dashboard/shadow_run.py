"""`sd shadow sync` from the dashboard (sd:2207): one server thread, one run at a time.

The operator ruled option (a) on 2026-10-03 (sd:2207 #8765): a thread calls
`sd_db.sync_shadow` once per tracker in `sd_db.TRACKERS`, as the pack's
`sd shadow sync` does, with one 120-second deadline for the whole run instead of
the library's 600 per tracker. GitHub gets what is left of it as `max_seconds`.
Jira ignores `max_seconds`, so its transport refuses a request once the deadline
has passed; a request already in flight can still take Jira's 30-second timeout.
A request while a run is live is refused, not queued.

Every `sync_shadow` holds the library's shadow-sync lock (`sd_db.shadow_sync.sync_lock`),
so this run and `sd shadow sync` (the nightly job, a terminal) never collect at
once; the second gets `SyncBusy`. A start tries the lock first and answers 409
while another holder has it. The lock is the hub's: a satellite refuses (HubOnly),
before the run would reopen the hub's file path from `PRAGMA database_list`.

The run opens its own writer on the database the request's writer opened: the
request's connection closes when the POST answers, long before the run ends.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone

import sd_db

__all__ = ["MAX_SECONDS", "RUN", "Run"]

MAX_SECONDS = 120


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Run:
    """The one sync this server runs: its state, and the lock that keeps it to one."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.state = {"running": False, "started": None, "finished": None, "trackers": [], "error": None}

    def status(self) -> dict:
        with self.lock:
            return self._snapshot()

    def _snapshot(self) -> dict:
        return dict(self.state, trackers=list(self.state["trackers"]), max_seconds=MAX_SECONDS)

    def start(self, connection: sqlite3.Connection):
        """Start a run on the database `connection` reads: `(202, status)`, or `(409, error)` while one is live."""
        from sd_db.shadow_sync import SyncBusy, sync_lock

        with self.lock:
            if self.state["running"]:
                return 409, {"error": f"A shadow sync started at {self.state['started']} is still running. Wait for it to finish."}
            # The run takes the lock itself; this try only answers a busy lock now rather than from the thread.
            # It raises HubOnly (a 400) on a satellite.
            try:
                with sync_lock(connection):
                    pass
            except SyncBusy as busy:
                return 409, {"error": str(busy)}
            database = connection.execute("PRAGMA database_list").fetchone()[2]
            self.state = {"running": True, "started": _now(), "finished": None, "trackers": [], "error": None}
            # The answer is the state as it started: a run that ends at once must not answer a start with "finished".
            answer = self._snapshot()
            self.thread = threading.Thread(target=self._run, args=(database,), daemon=True, name="sd-shadow-sync")
        # Started outside the lock, which the run takes to finish; `running` already refuses a second start.
        self.thread.start()
        return 202, answer

    def _run(self, database: str) -> None:
        trackers, error = [], None
        deadline = time.monotonic() + MAX_SECONDS
        try:
            connection = sd_db.connect(database)
            try:
                for name in sd_db.TRACKERS:
                    # The library refuses a budget that is not positive; Jira, asked last, is held by its transport.
                    options = {"max_seconds": max(deadline - time.monotonic(), 1)}
                    if name == "jira":
                        options["runner"] = _until(deadline)
                    result = sd_db.sync_shadow(connection, tracker=name, **options)
                    # An unconfigured tracker (Jira without its settings) is not a failure, as the pack's verb reads it.
                    trackers.append({"tracker": name, "ok": result.ok, "configured": getattr(result, "configured", True),
                                     "reason": result.reason, "lines": result.report()})
            finally:
                connection.close()
        except Exception as problem:  # noqa: BLE001 - a thread's failure is reported, not raised into nothing
            error = f"{type(problem).__name__}: {problem}"
        with self.lock:
            self.state.update(running=False, finished=_now(), trackers=trackers, error=error)


def _until(deadline: float):
    """Jira's real transport, refusing each request once `deadline` has passed; the refusal is a failed collect."""
    from sd_db import shadow_jira

    def transport(url, headers, body, method=None):
        if time.monotonic() >= deadline:
            # Transient: a spent budget says nothing about one ticket, so `jira_moves` holds its cursor (sd:8117).
            return None, shadow_jira.Transient(f"the dashboard's {MAX_SECONDS}-second sync budget ran out")
        # The library's own transport; it has no public name. `method` is the assignment's PUT (sd:8293).
        return shadow_jira._http(url, headers, body, method=method)

    return transport


#: The server's one run.
RUN = Run()
