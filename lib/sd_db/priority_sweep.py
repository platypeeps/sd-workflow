"""The nightly priority sweep: every open work item gets a rank or closes (sd:6726).

The operator's ruling of 2026-10-29: a work item is filed with a priority
(`sd task add` refuses one without), and the sweep handles the rest, in
three steps, one run each night (`sd-db review priorities`):

1. **apply**: each ruled question of the `priority` review queue sets its
   item's priority, when the item still has none; a batch with no open
   question closes.
2. **close**: an item still unranked `CLOSE_DAYS` after it was filed is
   closed, with a note that names the sweep and how to reopen it; one a
   queued or running build holds is left open and reported as `held`.
3. **ask**: every other unranked item not already asked gets one question
   in tonight's batch, P1 to P4, with a proposed rank and its reason, for
   the Review page.

Work kinds only (`KINDS`): ideas are out of scope (grill addendum,
2026-10-29), and a review batch's own row is the queue, not work.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

from . import review, workflow
from .errors import SdDbError

QUEUE = "priority"
KINDS = ("task", "followup")
CLOSE_DAYS = 30
#: A due date this close proposes P2.
DUE_SOON_DAYS = 14
OPTIONS = ["P1", "P2", "P3", "P4"]
SUBJECT = re.compile(r"sd:(\d+)")


def unranked(connection: sqlite3.Connection) -> list[dict]:
    """Open work items with no priority, oldest first; a review batch's row is not one."""
    marks = ",".join("?" * len(KINDS))
    rows = connection.execute(
        f"SELECT id, kind, title, repo, due, created_at FROM item WHERE status != 'done' AND priority IS NULL "
        f"AND kind IN ({marks}) AND NOT (source IS ? AND external_id LIKE 'review:%') ORDER BY created_at, id",
        (*KINDS, review.REF_SOURCE)).fetchall()
    return [dict(row) for row in rows]


def propose(item: dict, today: datetime) -> dict:
    """A proposed rank and its reason: a near due date or a followup is P2; anything else is P3."""
    if item.get("due") and item["due"][:10] <= (today + timedelta(days=DUE_SOON_DAYS)).date().isoformat():
        return {"option": "P2", "reason": f"due {item['due'][:10]}, within {DUE_SOON_DAYS} days"}
    if item["kind"] == "followup":
        return {"option": "P2", "reason": "a followup of delivered work"}
    return {"option": "P3", "reason": "no due date or other signal: the default rank"}


def _apply(connection: sqlite3.Connection, who: str) -> tuple[list[int], list[int]]:
    ranked, closed = [], []
    for batch in review._queue(connection, QUEUE):
        if batch["closed"] is not None:
            continue
        for question in batch["questions"]:
            found = SUBJECT.fullmatch(question["subject"])
            if question["label"] not in OPTIONS or not found:
                continue
            item = int(found[1])
            row = connection.execute("SELECT priority, status FROM item WHERE id = ?", (item,)).fetchone()
            if row is not None and row["priority"] is None and row["status"] != "done":
                workflow.edit_item(connection, item, {"priority": OPTIONS.index(question["label"]) + 1}, who=who)
                ranked.append(item)
        if review._close(connection, batch["item"]["id"], who, "priority sweep: every question ruled"):
            closed.append(batch["item"]["id"])
    return ranked, closed


def _asked(connection: sqlite3.Connection) -> set[int]:
    """Items an open question of the queue already asks about."""
    return {int(found[1]) for batch in review._queue(connection, QUEUE) if batch["closed"] is None
            for question in batch["questions"] if question["open"] and (found := SUBJECT.fullmatch(question["subject"]))}


def sweep(connection: sqlite3.Connection, *, who: str, now: datetime) -> dict:
    """Apply the rulings, close the stale, ask about the rest; what each step did."""
    ranked, batches = _apply(connection, who)
    cutoff = (now - timedelta(days=CLOSE_DAYS)).date().isoformat()
    stale: list[dict] = []
    waiting: list[dict] = []
    for item in unranked(connection):
        (stale if (item["created_at"] or "")[:10] < cutoff else waiting).append(item)
    closed: list[int] = []
    held: list[tuple[int, str]] = []
    for item in stale:
        try:
            workflow.change_status(connection, item["id"], "done", who=who,
                                   reason=f"priority sweep (sd:6726): no priority {CLOSE_DAYS} days after filing; reopen "
                                          f"with `sd task status {item['id']} planning` and "
                                          f"`sd task edit {item['id']} --priority N`")
        except SdDbError as refused:  # a queued or running build owns the item; the next night tries again
            held.append((item["id"], str(refused)))
        else:
            closed.append(item["id"])
    asked = _asked(connection)
    questions = [{"subject": f"sd:{item['id']}", "text": f"Priority for sd:{item['id']}: {item['title']}",
                  "evidence": [f"Kind: {item['kind']}", f"Filed: {(item['created_at'] or '?')[:10]}",
                               f"Repo: {item['repo'] or 'none'}"],
                  "options": OPTIONS, "recommendation": propose(item, now)}
                 for item in waiting if item["id"] not in asked]
    filed = None
    if questions:
        day = now.date().isoformat()
        filed = review.file(connection, {
            "schema": review.SCHEMA, "queue": QUEUE, "batch": now.strftime("%Y-%m-%d-%H%M%S"), "title": f"Priority for unranked items, {day}",
            "source": "sd-db review priorities (sd:6726)", "mode": "open", "limit": review.LIMIT,
            "context": {"purpose": f"Rank each open work item filed without a priority; one still unranked "
                                   f"{CLOSE_DAYS} days after filing closes."},
            "questions": questions[:review.LIMIT]}, who=who, now=now.isoformat(), suppress=False)
    return {"ranked": ranked, "batches_closed": batches, "closed": closed, "held": held,
            "asked": filed and filed["filed"] or 0, "batch": filed and filed["item"]}
