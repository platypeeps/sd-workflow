"""Decisions: the questions that wait on the operator, answered with one button per option (sd:3012) or in words (sd:5064).

A decision is a `question` note whose body names its choices, one line each:

    Which store holds quick notes?
    Option: A pack store kind
    Option: Workflow database rows

`sd task note N "$(printf 'Q\\nOption: A (Recommended)\\nOption: B')" --kind question` writes one. A line that starts
`Option:` is a choice and every other line is the question. A repeated choice counts once. `writes.add_note` refuses a new one
with fewer than two choices or none recommended (pack sd:6708); an older free-text question (sd:5064) lists here too, and the page answers it with a text box in
place of buttons. An old prose "Decision needed" comment stays a comment: it lists nothing here, because only a
`question` note asks.

- `document` is `/api/decisions`: every unresolved decision on an open item Tasks lists (`reads.backlog_items`,
  `reads.brief_notes`), and on a done item (`reads.open_notes`): a delivery answers none of its questions
  (pack sd:6329); a cancel answers them all (`progress.cancel_work`, sd:7992). The item's priority first, then the oldest, each with the revision `workflow.item_state` gives,
  read for every item in one `reads.revisions`.
  `recommended` is the index of the option the note recommends, or null (sd:5306): one option that ends
  "(Recommended)" or starts "(Recommended)" or "Recommended:", else a line "Recommendation: <text>" that names an
  option. A conditional "(Recommended only if …)" and two marked options recommend nothing.
  A question on a review batch (sd:3709, a task under a `review:` ref) is Scoring's: it is counted in `review`, and
  Decide's Decisions view draws one line that opens Scoring in place of its rows (sd:5781).
- `answer` is `/api/decisions/answer`, posted with the item, the note, the option and that revision: the CLI's two
  verbs in one transaction under the revision, as `sd task note N "<ruling>" --kind decision --answers <note>` writes.
  `workflow.add_item_note` writes the ruling, a `decision` note `Ruling on note #<note>: <option>`, and
  `workflow.resolve_item_note` resolves the question. A question with two choices or more takes only one of them;
  a free-text question takes any words. A stale revision writes nothing (`StaleItem`, a 409).
  `sd task show <item>` prints the ruling and the resolved question.

- `drafts` is the document's `drafts` (pack sd:6361), read by `sd_db.draft.drafted`: each item in planning whose
  newest draft assignment is done, with its `Draft spec:` comment and the count of its draft questions still open.
  Decide draws one row each with Mark ready, which posts the item's status under its revision;
  `workflow.change_status` refuses ready while a draft question is open, so the button only saves a round trip.

- `rulings` is the document's `rulings` (pack sd:6015): the `decision` notes of the last `RULINGS_DAYS`, one group per
  note session, newest group first, each ruling with its item, the item's state and the question it answered. A grill
  files many rulings in one session; the group reads them together.
- `reverse` is `/api/decisions/reverse`, posted with the item, the ruling note and the item revision: one transaction
  writes a `decision` note `Reversed ruling #<note>: question #<q> asks again.` and reopens question `<q>`
  (`writes.reopen_note`), so it waits in Decisions again with its options. Only a ruling that answered a question
  (`Ruling on note #<q>: ...`) reverses; a stale revision writes nothing.

Every read and write is an existing library call, so the installed sd_db serves it without a new install.
"""

from __future__ import annotations

import re

from sd_db import draft, reads, workflow
from sd_db.writes import parse, recommended

from .tasks_screen import is_scheduled

__all__ = ["answer", "document", "parse", "recommended", "request", "reverse", "reverse_request", "rulings"]

#: The longest free-text answer the page posts: a ruling is a line or a short paragraph, not a document.
ANSWER_MAX = 4000
#: The days of rulings the page groups by session (pack sd:6015).
RULINGS_DAYS = 7
#: A ruling that answered a question, as `answer` and `sd task note --answers` write it; and its reversal.
RULING = re.compile(r"\ARuling on note #([0-9]+): (.*)\Z", re.S)
REVERSAL = re.compile(r"\AReversed ruling #([0-9]+)\b")


def document(connection, *, now: str | None = None) -> dict:
    open_items = {row["id"]: row for row in reads.backlog_items(connection, include_done_days=0, now=now)
                  if row["status"] != "done" and not is_scheduled(row)}
    for note in reads.open_notes(connection, "question", batches=False, limit=None):
        if note["item_status"] == "done" and note["item"] not in open_items:
            open_items[note["item"]] = reads.item_by_id(connection, note["item"])
    found, batched = [], 0
    batches = reads.review_batches(connection, open_items)  # by its header, not its ref (sd:10981)
    for row in reads.brief_notes(connection, list(open_items)):
        question, options = parse(row["body"])
        if row["kind"] != "question":
            continue
        item = open_items[row["item"]]
        if row["item"] in batches:
            batched += 1
            continue
        found.append({"note": row["id"], "item": row["item"], "title": item["title"], "repo": item["repo"],
                      "priority": item["priority"], "asked": row["timestamp"], "question": question,
                      "options": options, "recommended": recommended(row["body"], options)})
    ruled = rulings(connection, now=now)
    drafted = draft.drafted(connection)
    # One batched revision read for every item asking, drafted or ruled on, not an `item_state` per item (sd:3531).
    rows = [*found, *drafted, *(ruling for group in ruled for ruling in group["rulings"])]
    revisions = reads.revisions(connection, sorted({row["item"] for row in rows}))
    for row in rows:
        row["revision"] = revisions[row["item"]]
    found.sort(key=lambda d: (d["priority"] is None, d["priority"] or 0, d["asked"], d["note"]))
    return {"decisions": found, "drafts": drafted, "review": batched, "rulings": ruled}


def rulings(connection, *, now: str | None = None) -> list[dict]:
    """The last `RULINGS_DAYS` of `decision` notes, one group per session, newest group first (pack sd:6015).

    Each ruling names its item, the item's state, the question it answered (`answers`, else None) and
    whether a later note reversed it. A reversal is not a ruling of its own: it marks the one it reversed.
    `document` adds each ruling's item revision in its one batched read.
    """
    from datetime import datetime, timedelta, timezone

    clock = datetime.fromisoformat(now.replace("Z", "+00:00")) if now else datetime.now(timezone.utc)
    since = (clock - timedelta(days=RULINGS_DAYS)).isoformat(timespec="seconds")
    rows = reads.ruling_notes(connection, since=since)
    reversed_ = {int(found[1]) for found in (REVERSAL.match(row["body"]) for row in rows) if found}
    groups: dict[str, dict] = {}
    for row in rows:
        if REVERSAL.match(row["body"]):
            continue
        ruled = RULING.match(row["body"])
        answers = int(ruled[1]) if ruled and row["question"] is not None else None
        group = groups.setdefault(row["session"] or "", {"session": row["session"] or "", "rulings": []})
        group["at"] = row["timestamp"]
        group["rulings"].append({"note": row["id"], "item": row["item"], "title": row["title"], "repo": row["repo"],
                                 "state": row["status"], "at": row["timestamp"],
                                 "ruling": ruled[2].strip() if ruled else row["body"].strip(), "answers": answers,
                                 "question": parse(row["question"])[0] if answers else None,
                                 "reversed": row["id"] in reversed_})
    return sorted(groups.values(), key=lambda group: group["at"], reverse=True)


def reverse(connection, item: int, note: int, *, expected_revision: str, who: str) -> dict:
    """Reverse ruling `note` of `item`: note the reversal and reopen the question it answered (pack sd:6015)."""
    from sd_db import writes

    with workflow.transaction(connection):
        state = workflow.item_state(connection, item)
        if state["revision"] != expected_revision:
            raise workflow.StaleItem(f"item {item} changed; reload it before applying this change")
        row = next((found for found in state["notes"] if found["id"] == note), None)
        if row is None:
            raise workflow.MissingNote(f"item {item} has no note {note}")
        ruled = RULING.match(row["body"]) if row["kind"] == "decision" else None
        asked = ruled and next((found for found in state["notes"] if found["id"] == int(ruled[1])), None)
        if not asked or asked["kind"] != "question":
            raise workflow.WorkflowError(f"note {note} answered no question on sd:{item}; note the item to reverse it")
        if asked["resolved_at"] is None:
            raise workflow.WorkflowError(f"question #{asked['id']} is open already: answer it there")
        # A done item's reopened question lists in Decisions too (pack sd:6329), so a done item reverses (was sd:6281).
        done = workflow.add_item_note(connection, item, kind="decision", who=who, expected_revision=expected_revision,
                                      body=f"Reversed ruling #{note}: question #{asked['id']} asks again.")
        writes.reopen_note(connection, asked["id"])
        state = workflow.item_state(connection, item)
        state["reversal"] = done["note"]
        return state


def reverse_request(payload: dict):
    """The reverse POST's checks, before the server opens a writer; the write as `action_route` returns one."""
    if (set(payload) != {"item", "note", "revision"}
            or any(type(payload[key]) is not int or not 1 <= payload[key] <= 9223372036854775807 for key in ("item", "note"))
            or not isinstance(payload["revision"], str) or not re.fullmatch(r"[a-f0-9]{64}", payload["revision"])):
        raise ValueError("Send the ruling's item and note and the item revision the page read.")
    return lambda connection: reverse(connection, payload["item"], payload["note"],
                                      expected_revision=payload["revision"], who="dashboard")


def answer(connection, item: int, note: int, option: str, *, expected_revision: str, who: str) -> dict:
    """Record `option` as the ruling on decision `note` of `item` and resolve it; the item's state, with the ruling."""
    with workflow.transaction(connection):
        state = workflow.item_state(connection, item)
        # The revision first: a page that read before another answer landed rereads (409) rather than reading a refusal.
        if state["revision"] != expected_revision:
            raise workflow.StaleItem(f"item {item} changed; reload it before applying this change")
        row = next((found for found in state["notes"] if found["id"] == note), None)
        if row is None:
            raise workflow.MissingNote(f"item {item} has no note {note}")
        options = parse(row["body"])[1]
        if row["kind"] != "question":
            raise workflow.WorkflowError(f"note {note} is not a question: only a question note takes a ruling")
        if row["resolved_at"] is not None:
            raise workflow.WorkflowError(f"note {note} was answered at {row['resolved_at']}")
        # Two choices or more: one of them. Fewer: a free-text question, answered in words (sd:5064).
        if len(options) >= 2 and option not in options:
            raise workflow.WorkflowError(f"note {note} offers no option {option!r}")
        option = option.strip()
        ruled = workflow.add_item_note(connection, item, kind="decision", who=who,
                                       body=f"Ruling on note #{note}: {option}", expected_revision=expected_revision)
        state = workflow.resolve_item_note(connection, note, who=who, expected_revision=ruled["revision"])
        state["ruling"] = ruled["note"]
        return state


def request(payload: dict):
    """The POST's checks, before the server opens a writer; the write as `action_route` returns one."""
    ids = ("item", "note")
    if (set(payload) != {*ids, "revision", "option"}
            or any(type(payload[key]) is not int or not 1 <= payload[key] <= 9223372036854775807 for key in ids)
            or not isinstance(payload["revision"], str) or not re.fullmatch(r"[a-f0-9]{64}", payload["revision"])
            or not isinstance(payload["option"], str) or not payload["option"].strip() or len(payload["option"]) > ANSWER_MAX):
        raise ValueError("Send the decision's item and note, the option the page showed (or an answer of at most "
                         f"{ANSWER_MAX} characters) and the item revision it read.")
    return lambda connection: answer(connection, payload["item"], payload["note"], payload["option"],
                                     expected_revision=payload["revision"], who="dashboard")
