"""Briefs and problem reports on Today: what the reports view used to push by ntfy or mail (operator ruling 2026-10-17).

Two lists, read from the workflow database and from nothing else:

- `problems`: every open report that asks for a person. A report row (kind `report`, any source but `brief`) that is not
  `done` and whose `fields.attention` is not false: a non-zero exit, an `SD_REPORT_ATTENTION:` line, or fields nobody can
  read, which is for a person to look at as well (`reporting.UNREADABLE_FIELDS`). A quiet run report is left out, as the
  backlog leaves it out. Acknowledge posts the Reports page's route, `/api/reports/<id>/acknowledge`, with the revision.
  A report whose output shows an expired login carries `login` (`job_failures.login`), which Fix opens (sd:6964).
  Fix on any other report is the Reports page's debug session (pack sd:7683). A report another session already took
  carries `claim`, who has it: `in_progress`, by its status change's `who`, or a note Fix wrote (`fix_session.NOTE`)
  in the last `CLAIM_HOURS`, by that note's session; Today then names it in place of Fix.
- `briefs`: the routine briefs (`briefs_screen`) filed in the last `WINDOW_DAYS` days, newest first, that the operator has
  not marked seen. Seen is a Today snooze (`snooze`), key `today:brief:<stem>`, held until the brief changes
  (`writes.SNOOZE_FOREVER`, sd:5752): the place pages' Briefs (`places.briefs`) read the same mark (`seen_row`), so a brief
  seen in either place stays seen in both. A brief whose text grows (an `extend`) reads as new again, since its word
  count is its problem. No new store: the snooze is the existing one.

A read that fails is its reason (`problems_error`, `briefs_error`), never an empty list. Nothing here writes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from sd_db import reads, reporting, writes
from sd_db.errors import SdDbError

from . import active_sessions, briefs_screen, job_failures, snooze

__all__ = ["READ", "SHOWN", "WINDOW_DAYS", "document", "seen_row"]

#: Briefs older than this many days stay on the Briefs page only.
WINDOW_DAYS = 7
#: Rows shown per list; the rest are counted and stay on Reports or Briefs.
SHOWN = 50
#: The last characters of a report's body Details shows.
BODY_CHARS = 2000

#: Open report rows read per refresh, newest first: `reporting.reports` reads at most 200. An open report older than
#: the newest `READ` open ones is on Reports only, and the count then says `+`.
READ = 200


#: A status change's body, as `writes.transition` writes it: `<old> -> <new> by <who>`, then `: <reason>` if given.
MOVED = re.compile(r"-> in_progress by (?P<who>.+?)(?:: |$)")
#: A Fix note claims its report for this long (pack sd:7733); after it, Today offers Fix again.
# ponytail: an age limit, not a herdr pane probe; join `active_sessions` launches if a long Fix run gets a second one.
CLAIM_HOURS = 4


def _recent(stamp, now: datetime) -> bool:
    """Within `CLAIM_HOURS` of `now`; a stamp that does not parse still claims, since its age is unknown."""
    try:
        at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return True
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return now - at < timedelta(hours=CLAIM_HOURS)


def claim(connection, row, now: datetime) -> str | None:
    """Who has the report (pack sd:7683): the session that moved it to `in_progress`, else the newest Fix note's from
    the last `CLAIM_HOURS`; None while nobody took it."""
    notes = reads.item_notes(connection, row["id"])
    if row["status"] == "in_progress":
        moved = [MOVED.search(note["body"]) for note in notes if note["kind"] == "status_change"]
        who = next((found["who"] for found in reversed(moved) if found), None)
        return f"{who or 'someone'} has it (in_progress)"
    for note in reversed(notes):
        found = note["kind"] == "comment" and active_sessions.LAUNCH.match(note["body"])
        if found and not _recent(note["timestamp"], now):
            return None
        if found:
            return f"{note['session'] or 'a session'} has it (Fix: {found['session']} session, herdr pane {found['pane']})"
    return None


def _problem(connection, row, revision: str, now: datetime) -> dict:
    try:
        fields = reporting.fields_document(connection, row["fields"])
        readable = isinstance(fields, dict)
    except reporting.UnreadableFields:
        fields, readable = {}, False
    fields = fields if readable else {}
    report = fields.get("report") if isinstance(fields.get("report"), dict) else {}
    repeats = fields.get("repeats") if isinstance(fields.get("repeats"), dict) else {}
    basis = repeats.get("basis") if repeats.get("basis") is not None else report.get("attention_basis")
    basis = basis if isinstance(basis, str) else ""
    try:
        body = json.loads(row["body"] or "{}")
    except ValueError:
        body = {}
    text = body.get("text") if isinstance(body, dict) and isinstance(body.get("text"), str) else ""
    return {"id": f"report:{row['id']}", "item": row["id"], "title": row["title"], "at": row["created_at"],
            "job": report.get("job") or None, "exit": report.get("exit_code"), "host": report.get("host") or None,
            "why": basis.splitlines()[0] if basis else ("its fields cannot be read" if not readable else ""),
            "basis": basis, "repeats": repeats.get("count"), "body": text[-BODY_CHARS:], "cut": len(text) > BODY_CHARS,
            "revision": revision, "fields_read": readable, "login": job_failures.login(text), "claim": claim(connection, row, now)}


def problems(connection: sqlite3.Connection, now: datetime) -> tuple[list[dict], int, bool]:
    """The open reports that ask for a person, newest first and at most `SHOWN`; how many more; whether the read was full."""
    # `reporting.OPEN_REPORT` filters before the limit (sd:8795): newer quiet reports no longer hide an open one.
    found = reporting.reports(connection, limit=READ, open_only=True)
    rows = found[:SHOWN]
    revisions = reads.revisions(connection, [row["id"] for row in rows])
    return [_problem(connection, row, revisions[row["id"]], now) for row in rows], len(found) - len(rows), len(found) >= READ


def seen_row(row: dict) -> dict:
    """A brief row's Seen mark as `snooze` reads it: its Today row id and its problem, the stem and its word count."""
    return {"id": f"brief:{row['id']}", "problem": [row["id"], row["words"]]}


def briefs(connection: sqlite3.Connection, *, now: str, snoozes: dict) -> tuple[list[dict], int, int]:
    """The briefs of the last `WINDOW_DAYS` days not marked seen, newest first; how many more; how many rows were bad."""
    today = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    got = briefs_screen._library().rows(connection, since=since, limit=briefs_screen.ROWS + 1)
    if not isinstance(got, list):
        raise ValueError("the brief reader returned no list")  # noqa: TRY004 - external document validation
    found, bad = briefs_screen.rows(got, None, briefs_screen.copies(None), None)
    rows = [{**row, **seen_row(row), "stem": row["id"], "until": writes.SNOOZE_FOREVER} for row in found if row["day"] >= since]
    shown, _seen = snooze.split("today", rows, snoozes)
    return shown[:SHOWN], max(0, len(shown) - SHOWN), bad


def document(connection: sqlite3.Connection, *, now: str, snoozes: dict) -> dict:
    """`problems`, `problems_more`, `problems_capped`, `problems_error`; `briefs`, `briefs_more`, `briefs_error`,
    `briefs_skipped`."""
    out = {"problems": [], "problems_more": 0, "problems_capped": False, "problems_error": "",
           "briefs": [], "briefs_more": 0, "briefs_error": "", "briefs_skipped": 0}
    try:
        out["problems"], out["problems_more"], out["problems_capped"] = problems(connection, datetime.fromisoformat(now.replace("Z", "+00:00")))
    except (SdDbError, sqlite3.Error, ValueError, KeyError) as failure:
        out["problems_error"] = str(failure) or "the reports could not be read"
    try:
        out["briefs"], out["briefs_more"], out["briefs_skipped"] = briefs(connection, now=now, snoozes=snoozes)
    except (SdDbError, sqlite3.Error, ValueError, KeyError, TypeError) as failure:
        out["briefs_error"] = str(failure) or "the briefs could not be read"
    return out
