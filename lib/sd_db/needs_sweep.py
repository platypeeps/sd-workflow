"""The Needs you sweep (sd:9759): close what asks for the operator but no longer needs them, each with its reason.

`sd reports sweep` runs it, Today's Clean up runs that line, and the system cron framework runs it nightly. Three
rules close; anything a rule cannot prove stays open:

| asks for the operator            | closes when                                         | stays open when                |
| -------------------------------- | --------------------------------------------------- | ------------------------------ |
| a cron job's attention report    | the host's newest run of the job, after the        | no host stored, no newer run,  |
|                                  | report's, filed nothing (`cron-chain:` row)        | a person's followup is open,   |
|                                  |                                                     | another host's run holds it    |
| a pr- or issue-attention report  | a complete read since it was filed, its state      | never read since, a cut read,  |
|                                  | read, did not see it (`<job>:seen`)                | its state unread, or a         |
|                                  |                                                     | person's followup is open      |
| a question                       | a ruling on it (`Ruling on note #N:`) stands       | no ruling, or a later note     |
|                                  |                                                     | reversed or withdrew it        |

A followup is left alone: `done` resolves what was owed (sd:3505), and a closed item proves nothing about a
followup's own work, such as sending a summary.

A report closes as the report flows close one (`reporting._supersede`, `pr_attention._clear`): the ingest's own
followup is resolved, and a followup anyone else wrote is work still owed, so the report stays. Each close writes
its reason on the item: the status note of a report, a comment beside a resolved note. `apply=False` lists what
would close and writes nothing. Each rule is read again inside the write that closes its row, so an ingest, a run
or a reversal that landed since the first read keeps the row open.

| step                  | state moved                 | failure                 | recovery                          |
| --------------------- | --------------------------- | ----------------------- | --------------------------------- |
| read the open rows    | none                        | the read raises         | nothing closed; the run fails     |
| close one report      | its followup, its status    | the write raises        | its transaction undoes it; named  |
| resolve one note      | the note, one comment       | the write raises        | its transaction undoes it; named  |

Each close is its own transaction, so a failure leaves the rest closed and the next run tries that row again.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import TypeGuard

from . import issue_attention, pr_attention, reporting, workflow
from .database import transaction
from .writes import add_note, resolve_note, transition

#: The attention jobs whose alerts clear by a later read (`pr_attention.alert`): job and its seen key.
SEEN = {pr_attention.JOB: pr_attention.SEEN_KEY, issue_attention.JOB: issue_attention.SEEN_KEY}
#: The prefix of every reason this sweep writes.
SWEEP = "needs-you sweep"


def _fields(row) -> dict:
    try:
        fields = json.loads(row["fields"] or "{}")
    except ValueError:
        return {}
    return fields if isinstance(fields, dict) else {}


#: Why a row stays open when the data a clear rests on has the wrong shape (review round 3): proof needs its fields.
BAD_FIELDS = "its report fields are not in the shape a clear needs"
BAD_READ = "the newest read is not in the shape a clear needs"


def _text(value) -> TypeGuard[str]:
    return isinstance(value, str) and value != ""


def _texts(value) -> bool:
    return isinstance(value, list) and all(isinstance(entry, str) for entry in value)


def _time(value) -> datetime | None:
    """An ISO time with its zone, or None: a time a clear compares must be one, never text that merely sorts."""
    if not _text(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _body(text) -> dict | None:
    """A heartbeat body as a dict, or None when it does not parse to one: then it proves nothing."""
    try:
        body = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    return body if isinstance(body, dict) else None


def _report_cleared(connection: sqlite3.Connection, row, fields: dict) -> tuple[str | None, str]:
    """Why `row`'s condition cleared, or None and why that is not known.

    Every stored value a clear rests on is checked for its shape first: a field or a heartbeat of another shape
    keeps the row open, never closes it and never stops the sweep.
    """
    report = found if isinstance(found := fields.get("report"), dict) else {}
    job, host, basis = report.get("job"), report.get("host"), report.get("attention_basis")
    if job is not None and not _text(job):
        return None, BAD_FIELDS
    if job in SEEN:
        if not _text(basis):
            return None, BAD_FIELDS
        seen = connection.execute("SELECT body, timestamp FROM state WHERE kind = 'heartbeat' AND key = ?"
                                  " ORDER BY id DESC LIMIT 1", (SEEN[job],)).fetchone()
        # By the second: the read that filed the alert records it seen in that second, so only a later read counts.
        if seen is None or (seen["timestamp"] or "")[:19] <= (row["created_at"] or "")[:19]:
            return None, "no read since it was filed"
        read = _body(seen["body"])
        # `pr_attention.alert` writes all three: a list of seen states, `complete`, and a list of unread kinds.
        if read is None or not _texts(read.get("seen")) or not _texts(read.get("unread")) \
                or not isinstance(read.get("complete"), bool):
            return None, BAD_READ
        # Absence proves a clear only from a complete read that read this state (`pr_attention.alert`).
        if read["complete"] is not True or basis.split(" ", 1)[0] in read["unread"]:
            return None, "the newest read did not read its state"
        if basis in read["seen"]:
            return None, "its state still holds"
        return f"the read at {seen['timestamp']} no longer sees {basis}", ""
    if not job or not host:
        return None, "no job or host stored" if host is None or _text(host) else BAD_FIELDS
    if not _text(host):
        return None, BAD_FIELDS
    repeats = found if isinstance(found := fields.get("repeats"), dict) else {}
    # The report's end, which a clean run must come after: two real times, compared as times (review round 4).
    ended = _time(repeats.get("last_ended") or report.get("ended"))
    if ended is None:
        return None, BAD_FIELDS
    # As `reporting._host_reports`: a report another host's newest run folded into is that host's finding.
    prefix = f"{reporting.CHAIN_KEY}{job}@"
    if connection.execute("SELECT 1 FROM state WHERE kind = 'heartbeat' AND substr(key, 1, ?) = ? AND key != ?"
                          " AND CASE WHEN json_valid(body) THEN json_extract(body, '$.item') END = ?",
                          (len(prefix), prefix, prefix + host, row["id"])).fetchone():
        return None, f"another host's newest run of {job} holds it"
    chain = connection.execute("SELECT body FROM state WHERE kind = 'heartbeat' AND key = ? ORDER BY id DESC LIMIT 1",
                               (f"{reporting.CHAIN_KEY}{job}@{host}",)).fetchone()
    # `reporting`'s chain row is `{"run_id", "ended", "item"}`; a clean run is one whose `item` is stored as null.
    last = (_body(chain["body"]) if chain else None) or {}
    run = last.get("run_id")
    run_ended = _time(last.get("ended"))
    if not _text(run) or run_ended is None or "item" not in last or last["item"] is not None or not run_ended > ended:
        return None, f"no clean run of {job} on {host} since"
    if connection.execute("SELECT 1 FROM item WHERE source = 'cron-report' AND external_id = ?",
                          (f"{job}:{run}",)).fetchone() or reporting._folded_into(connection, job, run):
        return None, f"{job}'s newest run on {host} filed a report"
    return f"{reporting.RECOVERED}{job}:{run} on {host} ended {last['ended']}", ""


def _report_check(connection: sqlite3.Connection, item: int) -> tuple[str | None, str]:
    """Why report `item` may close, or None and why not; the ingest's own followup alone may be open."""
    row = connection.execute(f"SELECT * FROM item WHERE id = ? AND {reporting.OPEN_REPORT}", (item,)).fetchone()
    if row is None:
        return None, "no longer open"
    why, unsure = _report_cleared(connection, row, _fields(row))
    if why is not None and _foreign_followups(connection, row):
        return None, "a followup someone else wrote is open"
    return why, unsure


def _foreign_followups(connection: sqlite3.Connection, row) -> bool:
    ingests = reporting._ingest_followup_for(row["fields"])
    return any(note["session"] != reporting.INGEST_SESSION or note["body"] != ingests
               for note in connection.execute("SELECT session, body FROM note WHERE item = ? AND kind = 'followup'"
                                              " AND resolved_at IS NULL", (row["id"],)))


def _close_report(connection: sqlite3.Connection, item: int, why: str, who: str) -> None:
    """Close one report as the report flows do: resolve the ingest's own followup, then move it to done."""
    own = [note["id"] for note in connection.execute("SELECT id FROM note WHERE item = ? AND kind = 'followup'"
                                                     " AND resolved_at IS NULL", (item,))]
    for note in own:
        resolve_note(connection, note)
    transition(connection, item, "done", who=who, reason=f"{why}; {SWEEP}"
               + (f"; resolved the ingest's followup {', '.join(map(str, own))}" if own else ""))


def _question_check(connection: sqlite3.Connection, note: int) -> tuple[str | None, str]:
    row = connection.execute("SELECT * FROM note WHERE id = ? AND resolved_at IS NULL", (note,)).fetchone()
    ruling = _standing_ruling(connection, row) if row else None
    return (f"answered by decision #{ruling}", "") if ruling else (None, "no standing ruling")


def _close_question(connection: sqlite3.Connection, note, why: str, who: str) -> None:
    resolve_note(connection, note["id"])
    add_note(connection, note["item"], "comment", f"{SWEEP}: resolved {note['kind']} #{note['id']}: {why}",
             session=who)


def sweep(connection: sqlite3.Connection, *, who: str, apply: bool = True) -> dict:
    """Close each attention row no longer relevant; `closed` and `kept` rows name the item, the row and why.

    `problems` names a close that raised; the rest still ran.
    """
    closed, kept, problems = [], [], []

    def settle(entry: dict, check, close) -> None:
        why, unsure = check()
        if why is None:
            kept.append({**entry, "why": unsure})
            return
        try:
            if apply:
                with transaction(connection):
                    why, unsure = check()  # again inside the write: an ingest, a run or a reversal may have landed
                    if why is None:
                        kept.append({**entry, "why": unsure})
                        return
                    close(why)
            closed.append({**entry, "why": why})
        except (workflow.WorkflowError, sqlite3.Error) as error:
            problems.append({**entry, "why": f"{why}: {error}"})

    for row in connection.execute("SELECT * FROM item WHERE kind = 'report' AND source = 'cron-report'"
                                  f" AND {reporting.OPEN_REPORT} ORDER BY id").fetchall():
        fields = _fields(row)
        if fields.get("attention") is not True or "record" in fields:
            continue
        settle({"item": row["id"], "kind": "report", "title": row["title"]},
               lambda item=row["id"]: _report_check(connection, item),
               lambda why, item=row["id"]: _close_report(connection, item, why, who))

    for note in connection.execute("SELECT * FROM note WHERE kind = 'question' AND resolved_at IS NULL"
                                   " ORDER BY id").fetchall():
        if _standing_ruling(connection, note) is None:
            continue  # an unanswered question is the Decisions list's, not this sweep's to doubt
        settle({"item": note["item"], "kind": "question", "note": note["id"], "title": _first(note["body"])},
               lambda note=note: _question_check(connection, note["id"]),
               lambda why, note=note: _close_question(connection, note, why, who))
    return {"applied": apply, "closed": closed, "kept": kept, "problems": problems}


#: A ruling, as Decisions writes it (`decisions.RULING`, `review.RULING`), and the two notes that take one back:
#: Decisions' reversal and a review batch's withdrawal. Each reopens the question it ruled.
RULING = re.compile(r"\ARuling on note #([0-9]+): ")
UNDONE = re.compile(r"\A(?:Reversed ruling #([0-9]+)\b|Withdrawn: ruling #([0-9]+) on note #)")


def _standing_ruling(connection: sqlite3.Connection, question) -> int | None:
    """The newest ruling on `question` that no later reversal or withdrawal took back, or None."""
    standing = None
    for decision in connection.execute("SELECT id, body FROM note WHERE item = ? AND kind = 'decision' AND id > ?"
                                       " ORDER BY id", (question["item"], question["id"])).fetchall():
        body = decision["body"] or ""
        if (ruled := RULING.match(body)) and int(ruled[1]) == question["id"]:
            standing = decision["id"]
        elif (undone := UNDONE.match(body)) and standing is not None and int(undone[1] or undone[2]) == standing:
            standing = None
    return standing


def _first(text) -> str:
    return str(text or "").strip().partition("\n")[0][:120]

