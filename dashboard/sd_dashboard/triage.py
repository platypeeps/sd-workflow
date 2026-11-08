"""The decision queues (sd:8013, sd:8014): store rows that wait on a person's stage move.

`QUEUES` names each queue: its store kind, the stage a person decides, its name and the page that decides it. Today's
Needs you counts them (`now_screen.triage_rows`). Blog Ideas, Tips and Market Watch also carry the bulk Decline:
`pool` is each waiting row with its score, age and revision, so the page shows which rows a Decline at a score and an
age would move and asks first; `decline` moves those rows to declined, each at the revision the page showed and only
while it still matches. The note the move writes names the control (`records.edit_row`'s "Stage x -> declined by
<who>"), and `declined` lists each declined row with that note's time and words. Nothing is removed, and nothing
declines on a schedule: only the operator's click posts `/api/triage/decline`.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from pathlib import Path

QUEUES = (("blog-idea", "inbox", "Blog Ideas", "/writing#ideas"), ("tip", "inbox", "Tips", "/writing#tips"),
          ("market", "candidate", "Market Watch", "/watches?stage=candidate"),
          ("topic", "candidate", "Topics", "/briefs#view=topics"))
#: The queues the bulk Decline serves, and those whose rows carry a score: a market row has none, so its Decline is by
#: age alone.
DECLINES = ("blog-idea", "tip", "market")
SCORED = ("blog-idea", "tip")
#: The control's defaults (operator 2026-11-07): below score 6, older than 30 days.
SCORE, DAYS = 6, 30
DECLINED = re.compile(r"^Stage \S+ -> declined by (.+)$")


def score(value) -> float | None:
    """A finite number, or None: a JSON field may hold any value (writing_screen's rule)."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def age(row, today: date) -> int | None:
    """Days since the row's `dateCreated`, else its item's creation; None when neither is a date."""
    for value in ((row.get("fields") or {}).get("dateCreated"), row.get("created_at")):
        try:
            return (today - date.fromisoformat(str(value)[:10])).days
        except ValueError:
            continue
    return None


def _today(now: str) -> date:
    return datetime.fromisoformat(now.replace("Z", "+00:00")).date()


def matches(kind: str, row: dict, *, below: float, days: int) -> bool:
    """Whether a waiting row is one a Decline at `below` and `days` moves: older than `days`, and, where the kind keeps
    a score, unscored or scored below `below`."""
    if row["age"] is None or row["age"] <= days:
        return False
    return kind not in SCORED or row["score"] is None or row["score"] < below


def pool(connection, kind: str, *, now: str) -> list[dict]:
    """The waiting rows of a queue the bulk Decline serves, oldest first, each with what the page shows and posts."""
    from sd_db import reads, records

    stage = dict((k, s) for k, s, _, _ in QUEUES)[kind]
    rows = records.list_rows(connection, kind, stage=stage)
    revisions = reads.revisions(connection, [row["id"] for row in rows]) if rows else {}
    today = _today(now)
    out = [{"item": row["id"], "title": row["title"], "score": score(row["fields"].get("score")),
            "age": age(row, today), "revision": revisions.get(row["id"], "")} for row in rows]
    return sorted(out, key=lambda row: (-(row["age"] or 0), row["title"].casefold()))


def declined(connection, kind: str) -> list[dict]:
    """The queue's declined rows, newest decline first, each with when and by what its decline note says."""
    from sd_db import reads, records

    out = []
    for row in records.list_rows(connection, kind, stage="declined"):
        notes = reads.item_notes(connection, row["id"])
        found = next(((note["timestamp"], hit.group(1)) for note in reversed(notes)
                      if note["kind"] == "comment" and (hit := DECLINED.match(note["body"] or ""))), (None, None))
        out.append({"item": row["id"], "title": row["title"], "at": found[0], "by": found[1]})
    return sorted(out, key=lambda row: row["at"] or "", reverse=True)


def decline(payload, principal):
    """`POST /api/triage/decline` {kind, score, days, items: [{item, revision}]}: a write that moves each named row to
    declined while it still waits and matches, at the revision the page showed; a row that moved or no longer matches
    is refused by name, and the rest still move. `principal` is unused: the note names the control, the route the user."""
    values = dict(payload)
    if set(values) != {"kind", "score", "days", "items"} or values["kind"] not in DECLINES:
        raise ValueError(f"Provide kind ({', '.join(DECLINES)}), score, days and items.")
    below, days = score(values["score"]), values["days"]
    if below is None or type(days) is not int or days < 0:
        raise ValueError("score is a number and days a whole number of days, 0 or more.")
    items = values["items"]
    if not isinstance(items, list) or not items or not all(
            isinstance(one, dict) and set(one) == {"item", "revision"} and type(one["item"]) is int
            and isinstance(one["revision"], str) for one in items):
        raise ValueError("items is a list of {item, revision}, one for each row the page showed.")
    kind = values["kind"]
    rule = f"score below {values['score']}, " if kind in SCORED else ""
    who = f"bulk Decline ({rule}older than {days} days)"

    def write(connection):
        from sd_db import records
        from sd_db.errors import SdDbError

        rules = records.rules_for(kind, home=Path.home())
        stage = dict((k, s) for k, s, _, _ in QUEUES)[kind]
        today = _today(datetime.now().astimezone().isoformat())
        moved, refused = [], []
        for one in items:
            try:
                row = records.get_row(connection, one["item"])
                if (row["fields"].get("kind"), row["stage"]) != (kind, stage):
                    raise ValueError(f"it is {row['stage']}, not a waiting {kind} row")
                if not matches(kind, {"age": age(row, today), "score": score(row["fields"].get("score"))},
                               below=below, days=days):
                    raise ValueError("it no longer matches the Decline's score and age")
                records.edit_row(connection, one["item"], rules, revision=one["revision"], who=who, stage="declined")
                moved.append(one["item"])
            except (SdDbError, ValueError) as error:
                refused.append({"item": one["item"], "reason": str(error) or type(error).__name__})
        return {"declined": moved, "refused": refused, "by": who}
    return write
