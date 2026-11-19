"""One review queue, made of rows the store already has (sd:3635).

The design is system's `docs/work/2026-10-12-review-queue/design.md`. A
repository or an experiment files a batch of small questions; the operator
answers each with one option; the producer exports the labels with their
provenance. No table is new:

- A **batch** is one task filed under the ref `review:<queue>:<batch>`, the
  key `sd task add --ref` files under, so the unique index on `(source,
  external_id)` makes a second filing find the first.
- Its first comment is the **header**: queue, batch, schema, source, mode,
  the seal in sealed mode, the question count and the digest of the batch
  file. A note is append-only, so the header is what binds; the item's title
  and body may change and bind nothing.
- A **question** is a `question` note in the Decisions format (one `Option:`
  line per choice), plus one `Subject:` line and up to five `Evidence:` lines.
  The batch's questions are the header's count of question notes after it;
  a question added by hand later is not one of them.
- The batch's **context** (purpose, rubric, what the labels feed) rides the
  header as `Purpose:`, `Rubric:` and `Feeds:` lines. A new batch files with
  all three or not at all (pack sd:9479); one filed before reads None. A question's
  optional **recommendation** rides its note as two more evidence lines,
  `Evidence: Recommended: <option>` and `Evidence: Recommended because:
  <reason>`, so a reader older than system sd:5759 files it under evidence,
  which a blind batch already hides, and never reads it as question text.
  The header's `Recommendations: <n>` line marks a batch filed that way; only
  there are those lines read as one, so an older batch's evidence stays evidence.
  Whether the standing label agreed with it, and whether it was ruled before
  the recommendation could be seen (blind or sealed, and the question's first
  ruling), is read from the notes, never written.
- A **label** is the Decisions ruling, `Ruling on note #N: <option>`, written
  with the question's resolution in one transaction; **undo** is a
  `Withdrawn: ruling #M on note #N` decision with the question reopened; a
  **comment** is `On note #N: <text>`.
- An **approval** of an Approve-or-Reject question files its task under the
  ref `review-approved:<note>` in the ruling's transaction, as `apply` would,
  or finds the task that ref already names, and the ruling ends
  ` -> sd:<task>` (sd:4924). A withdrawal of it ends `; sd:<task> stays filed`.

Modes: `open` shows everything; `blind` asks the reader to show the text and
options only (the dashboard's job, not this module's); `sealed` is blind plus
a key the producer keeps outside the store, whose SHA-256 the header holds.
A sealed export refuses another key and an open question, and commits the
batch's close before it parses the key. A batch once closed refuses every
ruling and withdrawal through `rule` and `withdraw`. Every label, and whether
a question is open, is read from the note history up to the first close and
never from a question's `resolved_at`: a note written after the close by
another path (`sd task note`, a reopen) is read as `late` and moves nothing.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import repos, workflow, writes
from .database import transaction
from .writes import add_note, reopen_note, resolve_note

__all__ = ["DROP", "ReviewError", "apply", "export", "file", "from_kev", "read", "reviews", "rule", "withdraw", "write"]

#: The batch file's one schema.
SCHEMA = 1
#: The `item.source` `sd task add --ref` files under (`bin/sd_work.py`
#: `REF_SOURCE`), so a batch is found by the same ref either way.
REF_SOURCE = "task-ref"
MODES = ("open", "blind", "sealed")
#: A batch's ceiling; a larger set splits into batches.
LIMIT = 500
OPTION_COUNT = range(2, 10)
OPTION_CHARS = 80
TEXT_BYTES = 4096
EVIDENCE_LINES = 5
SUBJECT_CHARS = 200
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA256 = re.compile(r"[0-9a-f]{64}")
KEYS = frozenset({"schema", "queue", "batch", "title", "repo", "source", "mode", "limit", "seal", "context",
                  "questions"})
QUESTION_KEYS = frozenset({"subject", "text", "evidence", "options", "recommendation"})
#: A batch's context block (system sd:5759): each key's header tag, the line limit and the rubric's line count.
CONTEXT_TAGS = {"purpose": "Purpose", "rubric": "Rubric", "feeds": "Feeds"}
CONTEXT_CHARS = 400
RUBRIC_LINES = 10
#: A recommendation's two evidence-line prefixes and its reason's limit.
RECOMMENDED = "Recommended: "
BECAUSE = "Recommended because: "
REASON_CHARS = 200
#: The modes whose labeller sees no recommendation before ruling.
BLIND = ("blind", "sealed")
#: A question note's tagged lines; every other line is the question text.
TAGGED = re.compile(r"^[ \t]*(Option|Subject|Evidence):[ \t]*(.*?)[ \t]*$")
HEADER = "Review batch"
RULING = re.compile(r"Ruling on note #(\d+): (.+)")
#: The tail an approval's ruling carries: the item it filed or found (sd:4924).
FILED = re.compile(r"(.+) -> sd:(\d+)")
WITHDRAWN = re.compile(r"Withdrawn: ruling #(\d+) on note #(\d+)(?:; sd:\d+ stays filed)?")
COMMENT = re.compile(r"On note #(\d+): (.+)", re.DOTALL)
#: The `status_change` note `writes.transition` writes when an item closes.
CLOSE = re.compile(r"\S+ -> done by ")
#: The nightly queue's options, the ref prefix of the task an approval
#: files, and how long an unruled suggestion waits (operator, 2026-10-12).
APPROVE, REJECT = "Approve", "Reject"
APPROVED = "review-approved"
EXPIRE_DAYS = 7
EXPIRED = "Expired, not ruled"
#: How long a rejected subject keeps a suggestion out of its queue.
SUPPRESS_DAYS = 90
UNKNOWN = "unknown"
#: A Kev file's questions end with this option: a drop is a label like any other, so Undo and the export
#: treat it the same way, and `reviews` reads it as Kev's `drop` verdict (sd:4789).
DROP = "(drop)"
#: The queue a Kev label file files under, and the evidence line that carries its proposed label.
KEV_QUEUE = "kev"
PROPOSED = "Proposed label: "


class ReviewError(workflow.WorkflowError):
    """A batch, a ruling or an export the queue refuses; nothing was written."""


# ---------------------------------------------------------------- the batch file


def _unicode(value: str, name: str) -> None:
    """Refuse text UTF-8 cannot store: a JSON escape can carry a lone surrogate."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ReviewError(f"{name} is not valid Unicode") from None


def _line(value: Any, name: str, *, limit: int | None = None) -> str:
    """One line of text, as filed: no padding, no line break, no NUL."""
    if (not isinstance(value, str) or not value or value != value.strip() or "\x00" in value
            or len(value.splitlines()) != 1):
        raise ReviewError(f"{name} must be one line of text with no padding")
    _unicode(value, name)
    if limit is not None and len(value) > limit:
        raise ReviewError(f"{name} is {len(value)} characters; the limit is {limit}")
    return value


def _name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ReviewError(f"{name} must be letters, digits, dots, dashes or underscores, at most 64")
    return value


def _question(found: Any, where: str) -> dict:
    if not isinstance(found, dict) or set(found) - QUESTION_KEYS or not {"subject", "text", "options"} <= set(found):
        raise ReviewError(f"{where} must hold subject, text and options, and evidence at most")
    subject = _line(found["subject"], f"{where} subject", limit=SUBJECT_CHARS)
    text = found["text"]
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise ReviewError(f"{where} text must be text")
    text = text.strip()
    _unicode(text, f"{where} text")
    if len(text.encode("utf-8")) > TEXT_BYTES:
        raise ReviewError(f"{where} text is over {TEXT_BYTES} bytes")
    if any(TAGGED.match(line) for line in text.splitlines()):
        raise ReviewError(f"{where} text has a line that starts Option:, Subject: or Evidence:")
    evidence = found.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > EVIDENCE_LINES:
        raise ReviewError(f"{where} evidence must be a list of at most {EVIDENCE_LINES} lines")
    options = found["options"]
    if not isinstance(options, list) or len(options) not in OPTION_COUNT:
        raise ReviewError(f"{where} needs {OPTION_COUNT.start} to {OPTION_COUNT.stop - 1} options")
    options = [_line(option, f"{where} option", limit=OPTION_CHARS) for option in options]
    if len(set(options)) != len(options):
        raise ReviewError(f"{where} repeats an option")
    evidence = [_line(line, f"{where} evidence") for line in evidence]
    checked: dict[str, Any] = {"subject": subject, "text": text, "options": options, "evidence": evidence}
    # Added only when given: a Kev import's digest is over these dicts, so a batch it filed before finds itself.
    if "recommendation" in found:
        checked["recommendation"] = _recommendation(found["recommendation"], options, where)
    return checked


def _recommendation(found: Any, options: list[str], where: str) -> dict:
    if not isinstance(found, dict) or set(found) != {"option", "reason"}:
        raise ReviewError(f"{where} recommendation must hold an option and a reason")
    if found["option"] not in options:
        raise ReviewError(f"{where} recommends {found['option']!r}, which is not one of its options")
    return {"option": found["option"], "reason": _line(found["reason"], f"{where} reason", limit=REASON_CHARS)}


def _context(found: Any) -> dict:
    if not isinstance(found, dict) or not found or set(found) - set(CONTEXT_TAGS):
        raise ReviewError(f"context must hold one or more of {', '.join(CONTEXT_TAGS)}")
    rubric = found.get("rubric", [])
    if not isinstance(rubric, list) or ("rubric" in found and not 1 <= len(rubric) <= RUBRIC_LINES):
        raise ReviewError(f"context rubric must be a list of 1 to {RUBRIC_LINES} lines")

    def line(name: str, value: Any) -> str:
        return _line(value, f"context {name}", limit=CONTEXT_CHARS)

    return {"purpose": line("purpose", found["purpose"]) if "purpose" in found else None,
            "rubric": [line("rubric", entry) for entry in rubric],
            "feeds": line("feeds", found["feeds"]) if "feeds" in found else None}


#: What a batch's context holds, for the refusal of one that lacks part of it (pack sd:9479).
CONTEXT_FORMAT = ('"context": {"purpose": TEXT, "rubric": [LINE, ...], "feeds": TEXT}: why the batch exists, '
                  f'how to label it in 1 to {RUBRIC_LINES} lines, and what the labels decide and when')


def _require_context(found: dict) -> None:
    """Refuse a batch without its Purpose, a Rubric line and its Feeds (pack sd:9479): a page of bare questions."""
    context = found["context"] or {}
    tags = [tag for key, tag in CONTEXT_TAGS.items() if not context.get(key)]
    if tags:
        named = tags[0] if len(tags) == 1 else f"{', '.join(tags[:-1])} or {tags[-1]}"
        raise ReviewError(f"a batch needs its context, and this one has no {named} line; give {CONTEXT_FORMAT}")


def _checked(batch: Any) -> dict:
    """The batch file's fields, or a refusal naming the first that is wrong."""
    if not isinstance(batch, dict):
        raise ReviewError("a batch is a JSON object")
    unknown = set(batch) - KEYS
    if unknown:
        raise ReviewError(f"a batch takes no {', '.join(sorted(unknown))}")
    missing = {"schema", "queue", "batch", "title", "source", "mode", "questions"} - set(batch)
    if missing:
        raise ReviewError(f"a batch needs {', '.join(sorted(missing))}")
    if type(batch["schema"]) is not int or batch["schema"] != SCHEMA:
        raise ReviewError(f"schema must be {SCHEMA}")
    if batch["mode"] not in MODES:
        raise ReviewError(f"mode must be one of {', '.join(MODES)}")
    limit = batch.get("limit")
    if limit is not None and (type(limit) is not int or not 1 <= limit <= LIMIT):
        raise ReviewError(f"limit must be a whole number from 1 to {LIMIT}")
    seal = batch.get("seal")
    if (batch["mode"] == "sealed") != (seal is not None):
        raise ReviewError("a sealed batch needs a seal, and only a sealed batch takes one")
    if seal is not None and (not isinstance(seal, dict) or set(seal) != {"sha256", "name"}
                             or not isinstance(seal["sha256"], str) or not SHA256.fullmatch(seal["sha256"])):
        raise ReviewError("seal must be {\"sha256\": <64 lowercase hex>, \"name\": <the key file's name>}")
    repo = batch.get("repo")
    if repo is not None and not isinstance(repo, str):
        raise ReviewError("repo must be a registered checkout's path")
    questions = batch["questions"]
    if not isinstance(questions, list) or not questions:
        raise ReviewError("questions must be a list of at least one")
    if limit is None and len(questions) > LIMIT:
        raise ReviewError(f"{len(questions)} questions; a batch holds at most {LIMIT}, so split it or set a limit")
    checked = [_question(found, f"question {number}") for number, found in enumerate(questions, 1)]
    # In a batch that recommends, those evidence lines are the recommendation's alone; a batch that does not keeps any
    # evidence it had, so a batch filed before system sd:5759 files again unchanged.
    if any("recommendation" in found for found in checked):
        for number, found in enumerate(checked, 1):
            if any(line.startswith((RECOMMENDED, BECAUSE)) for line in found["evidence"]):
                raise ReviewError(f"question {number} evidence has a line that starts {RECOMMENDED.strip()} or "
                                  f"{BECAUSE.strip()}; in a batch that recommends, a recommendation is its own key")
    subjects = [found["subject"] for found in checked]
    if len(set(subjects)) != len(subjects):
        raise ReviewError("a subject appears twice; each is unique in its batch")
    context = None if batch.get("context") is None else _context(batch["context"])
    return {"queue": _name(batch["queue"], "queue"), "batch": _name(batch["batch"], "batch"),
            "title": _line(batch["title"], "title"), "source": _line(batch["source"], "source"),
            "mode": batch["mode"], "limit": limit or LIMIT, "repo": repo, "context": context,
            "seal": None if seal is None else {"sha256": seal["sha256"], "name": _line(seal["name"], "seal name")},
            "questions": checked}


def _digest(batch: dict) -> str:
    return hashlib.sha256(json.dumps(batch, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _header_body(found: dict, digest: str, kept: list[dict]) -> str:
    lines = [HEADER, f"Queue: {found['queue']}", f"Batch: {found['batch']}", f"Schema: {SCHEMA}",
             f"Source: {found['source']}", f"Mode: {found['mode']}"]
    if found["seal"] is not None:
        lines.append(f"Seal: {found['seal']['sha256']} {found['seal']['name']}")
    context = found.get("context") or {}
    for key, tag in CONTEXT_TAGS.items():
        values = context.get(key)
        lines += [f"{tag}: {value}" for value in (values if isinstance(values, list) else [values]) if value]
    advised = sum("recommendation" in question for question in kept)
    if advised:
        lines.append(f"Recommendations: {advised}")
    return "\n".join([*lines, f"Questions: {len(kept)}", f"Digest: {digest}"])


def _question_body(found: dict) -> str:
    advice = found.get("recommendation")
    advised = [] if advice is None else [RECOMMENDED + advice["option"], BECAUSE + advice["reason"]]
    return "\n".join([found["text"], f"Subject: {found['subject']}",
                      *(f"Evidence: {line}" for line in [*found["evidence"], *advised]),
                      *(f"Option: {option}" for option in found["options"])])


# ---------------------------------------------------------------- reading a batch


def _parse_header(body: str) -> dict | None:
    lines = body.splitlines()
    if not lines or lines[0] != HEADER:
        return None
    fields = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
    if not {"Queue", "Batch", "Schema", "Source", "Mode", "Questions", "Digest"} <= set(fields):
        return None
    seal, _, name = fields.get("Seal", "").partition(" ")
    tagged = [line.split(": ", 1) for line in lines[1:] if ": " in line]
    context = {key: [value for found, value in tagged if found == tag] for key, tag in CONTEXT_TAGS.items()}
    return {"queue": fields["Queue"], "batch": fields["Batch"], "schema": fields["Schema"],
            "source": fields["Source"], "mode": fields["Mode"], "seal": seal or None, "seal_name": name or None,
            "questions": int(fields["Questions"]) if fields["Questions"].isdigit() else -1,
            "digest": fields["Digest"], "recommendations": "Recommendations" in fields,
            "context": {"purpose": next(iter(context["purpose"]), None), "rubric": context["rubric"],
                        "feeds": next(iter(context["feeds"]), None)} if any(context.values()) else None}


def _parse_question(note: dict, advised: bool = False) -> dict:
    text, subject, evidence, options = [], None, [], []
    for line in note["body"].splitlines():
        tagged = TAGGED.match(line)
        if tagged is None:
            text.append(line)
        elif tagged[1] == "Option":
            options.append(tagged[2])
        elif tagged[1] == "Evidence":
            evidence.append(tagged[2])
        elif subject is None:
            subject = tagged[2]
    # The recommendation's evidence lines, only in a batch the header marks, and when the first names an offered option.
    advice = next((line for line in evidence if line.startswith(RECOMMENDED)), None) if advised else None
    recommendation = None
    if advice is not None and advice[len(RECOMMENDED):] in options:
        because = next((line for line in evidence if line.startswith(BECAUSE)), None)
        recommendation = {"option": advice[len(RECOMMENDED):], "reason": because and because[len(BECAUSE):]}
        evidence = [line for line in evidence if line not in (advice, because)]
    return {"note": note["id"], "subject": subject, "text": "\n".join(text).strip(), "evidence": evidence,
            "options": options, "recommendation": recommendation,
            "sha256": hashlib.sha256(note["body"].encode("utf-8")).hexdigest(), "asked_at": note["timestamp"]}


def _batch(row: dict, notes: list[dict]) -> dict:
    """The batch an item's row and notes hold, or a refusal when they hold none."""
    notes = sorted(notes, key=lambda note: note["id"])
    first = next((note for note in notes if note["kind"] == "comment"), None)
    header = None if first is None else _parse_header(first["body"])
    if (first is None or header is None or row["source"] != REF_SOURCE
            or row["external_id"] != f"review:{header['queue']}:{header['batch']}"):
        raise ReviewError(f"sd:{row['id']} is not a review batch")
    asked = [note for note in notes if note["kind"] == "question" and note["id"] > first["id"]]
    if len(asked) < header["questions"] or header["questions"] < 0:
        raise ReviewError(f"sd:{row['id']}'s header counts {header['questions']} questions; "
                          f"{len(asked)} follow it")
    questions = {note["id"]: {**_parse_question(note, header["recommendations"]), "rulings": [], "comments": [], "late": [],
                              "expired": False}
                 for note in asked[:header["questions"]]}
    # Every label is read from this note history up to the first close, never
    # from a question's `resolved_at`, which a writer may change after it: a
    # ruling, withdrawal or comment written after the close is kept as `late`
    # and moves nothing (lane review on 2ab91c11d).
    closed = next((note["id"] for note in notes if note["kind"] == "status_change"
                   and CLOSE.match(note["body"] or "")), None)
    rulings: dict[int, dict] = {}
    for note in notes:
        body = note["body"] or ""
        late = closed is not None and note["id"] > closed
        if note["kind"] == "decision" and (ruled := RULING.fullmatch(body)):
            question = questions.get(int(ruled[1]))
            option, filed = ruled[2], None
            if question is not None and option not in question["options"] and (tail := FILED.fullmatch(option)):
                option, filed = tail[1], int(tail[2])
            if question is None or option not in question["options"]:
                continue
            if late:
                question["late"].append({"note": note["id"], "body": body, "at": note["timestamp"]})
                continue
            rulings[note["id"]] = {"ruling": note["id"], "question": question["note"], "option": option,
                                   "filed": filed, "by": note["session"], "at": note["timestamp"],
                                   "withdrawal": None}
            question["rulings"].append(rulings[note["id"]])
        elif note["kind"] == "decision" and (pulled := WITHDRAWN.fullmatch(body)):
            ruling = rulings.get(int(pulled[1]))
            if ruling is None or ruling["question"] != int(pulled[2]) or ruling["withdrawal"] is not None:
                continue
            if late:
                questions[ruling["question"]]["late"].append({"note": note["id"], "body": body,
                                                              "at": note["timestamp"]})
                continue
            ruling.update(withdrawal=note["id"], withdrawn_at=note["timestamp"])
        elif note["kind"] == "comment" and note["id"] > first["id"] and (said := COMMENT.fullmatch(body)):
            question = questions.get(int(said[1]))
            if question is not None and late:
                question["late"].append({"note": note["id"], "body": body, "at": note["timestamp"]})
            elif question is not None:
                question["expired"] = question["expired"] or said[2] == EXPIRED
                question["comments"].append({"note": note["id"], "text": said[2], "by": note["session"],
                                             "at": note["timestamp"]})
    for question in questions.values():
        standing = [ruling for ruling in question["rulings"] if ruling["withdrawal"] is None]
        live = standing[-1] if standing else None
        question.update(label=live and live["option"], ruling=live and live["ruling"],
                        ruled_by=live and live["by"], ruled_at=live and live["at"], filed=live and live["filed"],
                        position=live and question["options"].index(live["option"]) + 1,
                        open=live is None and not question["expired"])
        # Agreement (system sd:5759): None with no label or no recommendation. A label is blind only in a blind or
        # sealed batch and only as the question's first ruling: once one lands the page shows the recommendation.
        advice = question["recommendation"]
        question.update(agreed=None, ruled_blind=None)
        if advice is not None and live is not None:
            question.update(agreed=live["option"] == advice["option"],
                            ruled_blind=header["mode"] in BLIND and question["rulings"][0] is live)
    found = list(questions.values())
    return {"item": row, "ref": row["external_id"], "header": header, "questions": found, "closed": closed,
            "open": sum(question["open"] for question in found),
            "labels": sum(question["label"] is not None for question in found)}


def read(connection: sqlite3.Connection, item: int) -> dict:
    """The batch `item` holds: its header, and each question with its standing label."""
    state = workflow.item_state(connection, item)
    return _batch(state["item"], state["notes"])


def _queue(connection: sqlite3.Connection, queue: str) -> list[dict]:
    prefix = f"review:{queue}:"
    rows = connection.execute(
        "SELECT id FROM item WHERE source = ? AND substr(external_id, 1, ?) = ? ORDER BY id",
        (REF_SOURCE, len(prefix), prefix)).fetchall()
    return [read(connection, row["id"]) for row in rows]


def _moment(now: str | None) -> datetime:
    moment = datetime.now(UTC) if now is None else datetime.fromisoformat(now)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


# ---------------------------------------------------------------- filing


def _rejected(connection: sqlite3.Connection, queue: str, since: str) -> dict[str, str]:
    """Each subject the queue's standing rulings rejected at or after `since`, and where."""
    found = {}
    for batch in _queue(connection, queue):
        for question in batch["questions"]:
            if question["label"] == REJECT and question["ruled_at"] >= since:
                found[question["subject"]] = (f"rejected by ruling #{question['ruling']} in "
                                              f"sd:{batch['item']['id']} at {question['ruled_at']}")
    return found


def _difference(filed: dict, found: dict) -> str:
    """The first field in which the filed batch and the file differ, as far as the store can tell."""
    header = filed["header"]
    given = {"schema": str(SCHEMA), "source": found["source"], "mode": found["mode"],
             "seal": found["seal"] and found["seal"]["sha256"], "context": found["context"]}
    for name, value in given.items():
        if header[name] != value:
            return f"{name} is {header[name]!r} in the header and {value!r} in the file"
    questions = {question["subject"]: question for question in found["questions"]}
    for question in filed["questions"]:
        other = questions.get(question["subject"])
        if other is None:
            return f"question {question['subject']} is not in the file"
        for name in ("text", "evidence", "options", "recommendation"):
            if question[name] != other.get(name):
                return f"question {question['subject']}: the {name} differ ({question[name]!r} filed)"
    return ("the title, repo, limit or a question that was not filed differs "
            f"(digest {header['digest']} filed)")


def file(connection: sqlite3.Connection, batch: Any, *, who: str, now: str | None = None,
         suppress: bool = True) -> dict:
    """File `batch` (the parsed batch file): the item, the header and each question, in one transaction.

    The same file again finds its item and writes nothing; another file under
    the same queue and batch is refused, naming the first difference. A
    subject whose question the queue rejected in the last 90 days is dropped,
    then questions past `limit` are; each drop is returned with its reason.
    `suppress=False` skips the 90-day check, for a label file whose every
    question must file or none (`from_kev`).
    """
    found = _checked(batch)
    digest = _digest(batch)
    ref = f"review:{found['queue']}:{found['batch']}"
    since = (_moment(now) - timedelta(days=SUPPRESS_DAYS)).isoformat(timespec="seconds")
    with transaction(connection):
        held = writes.item_by_external(connection, REF_SOURCE, ref)
        if held is not None:
            filed = read(connection, held["id"])
            # A batch filed before its context was required finds itself again once its filer adds one (pack sd:9479).
            bare = {key: value for key, value in batch.items() if key != "context"}
            before = {_digest(bare), _digest({**bare, "context": None})} if filed["header"]["context"] is None else set()
            if filed["header"]["digest"] != digest and filed["header"]["digest"] not in before:
                raise ReviewError(f"{ref} is filed as sd:{held['id']} with other content: "
                                  f"{_difference(filed, found)}")
            return {"item": held["id"], "ref": ref, "created": False, "filed": len(filed["questions"]),
                    "dropped": []}
        _require_context(found)
        rejected = _rejected(connection, found["queue"], since) if suppress else {}
        kept = [question for question in found["questions"] if question["subject"] not in rejected]
        dropped = [{"subject": question["subject"], "reason": rejected[question["subject"]]}
                   for question in found["questions"] if question["subject"] in rejected]
        dropped += [{"subject": question["subject"], "reason": f"past the limit of {found['limit']}"}
                    for question in kept[found["limit"]:]]
        kept = kept[:found["limit"]]
        if not kept:
            return {"item": None, "ref": ref, "created": False, "filed": 0, "dropped": dropped}
        item = workflow.capture_task(connection, title=found["title"], repo=found["repo"], who=who,
                                     body=f"Review batch {ref}; its questions are the notes.")["item"]["id"]
        writes.set_item_fields(connection, item, source=REF_SOURCE, external_id=ref)
        add_note(connection, item, "comment", _header_body(found, digest, kept), session=who)
        for question in kept:
            add_note(connection, item, "question", _question_body(question), session=who,
                     labelling=True)
    return {"item": item, "ref": ref, "created": True, "filed": len(kept), "dropped": dropped}


# ---------------------------------------------------------------- ruling and undo


def _open_batch(connection: sqlite3.Connection, item: int, expected_revision: str) -> tuple[dict, dict]:
    state = workflow.item_state(connection, item)
    if state["revision"] != expected_revision:
        raise workflow.StaleItem(f"item {item} changed; reload it before applying this change")
    found = _batch(state["item"], state["notes"])
    if state["item"]["status"] == "done" or found["closed"] is not None:
        raise ReviewError(f"sd:{item} is closed; a batch once closed takes no ruling or withdrawal")
    return state, found


def rule(connection: sqlite3.Connection, item: int, note: int, option: str, *,
         expected_revision: str, who: str, session: str | None = None) -> dict:
    """Label question `note` of batch `item` with `option`: the Decisions ruling, under the item revision.

    Approve on an Approve-or-Reject question files the task `apply` would,
    under the same ref, in the same transaction, and the ruling names it; a
    task that ref already names (a withdrawn approval's) is named instead. The
    state's `filed` is `{"item", "created"}`, or None when nothing was filed.
    So `apply` finds the ref and files nothing more (sd:4924).

    `session` is the harness session that rules, None outside one (pack sd:6046).
    The filed task records it as its session, `who` when None; the ruling keeps
    `who`, which the Review page shows as "ruled by", and records `session` as
    its `harness_session`, so the ruling shows in that session's trail.
    """
    with transaction(connection):
        _, found = _open_batch(connection, item, expected_revision)
        question = next((question for question in found["questions"] if question["note"] == note), None)
        if question is None:
            raise ReviewError(f"note {note} is not a question of sd:{item}")
        if not question["open"]:
            raise ReviewError(f"note {note} is settled: ruled by note #{question['ruling']}"
                              if question["ruling"] else f"note {note} is settled: it expired")
        if option not in question["options"]:
            raise ReviewError(f"note {note} offers no option {option!r}")
        filed = None
        if option == APPROVE and question["options"] == [APPROVE, REJECT]:
            held = writes.item_by_external(connection, REF_SOURCE, f"{APPROVED}:{note}")
            filed = ({"item": held["id"], "created": False} if held is not None
                     else {"item": _approval_task(connection, found, question, who, session=session), "created": True})
        tail = "" if filed is None else f" -> sd:{filed['item']}"
        ruled = workflow.add_item_note(connection, item, kind="decision", who=who,
                                       body=f"Ruling on note #{note}: {option}{tail}",
                                       expected_revision=expected_revision, harness_session=session)
        if filed is not None and filed["created"]:
            # The ruling's id exists only now, so the body is written last.
            writes.set_item_fields(connection, filed["item"],
                                   body=_approval_body(found, question, ruled["note"]["id"]))
        state = workflow.resolve_item_note(connection, note, who=who, expected_revision=ruled["revision"])
        state["ruling"] = ruled["note"]
        state["filed"] = filed
        return state


def withdraw(connection: sqlite3.Connection, item: int, ruling: int, *,
             expected_revision: str, who: str) -> dict:
    """Undo `ruling`: a withdrawal note and its question reopened, in one transaction."""
    with transaction(connection):
        _, found = _open_batch(connection, item, expected_revision)
        question = next((question for question in found["questions"]
                         if any(held["ruling"] == ruling for held in question["rulings"])), None)
        if question is None:
            raise ReviewError(f"note {ruling} is not a ruling on a question of sd:{item}")
        held = next(held for held in question["rulings"] if held["ruling"] == ruling)
        if held["withdrawal"] is not None:
            raise ReviewError(f"ruling #{ruling} was withdrawn by note #{held['withdrawal']}")
        if question["ruling"] != ruling:
            raise ReviewError(f"ruling #{ruling} is not the standing label on note #{question['note']}")
        # The item an approval filed stays: it may be worked or linked already.
        kept = "" if held["filed"] is None else f"; sd:{held['filed']} stays filed"
        pulled = workflow.add_item_note(connection, item, kind="decision", who=who,
                                        body=f"Withdrawn: ruling #{ruling} on note #{question['note']}{kept}",
                                        expected_revision=expected_revision)
        reopen_note(connection, question["note"])
        state = workflow.item_state(connection, item)
        state["withdrawal"] = pulled["note"]
        return state


# ---------------------------------------------------------------- export


def _key_rows(key: bytes) -> dict[str, dict]:
    """The key's rows by subject: CSV, its first column naming the subject."""
    try:
        reader = csv.DictReader(io.StringIO(key.decode("utf-8-sig"), newline=""))
        names = reader.fieldnames
        if not names or not all(names) or len(set(names)) != len(names):
            raise ReviewError("the key needs a header row of unique, named columns; the first names the subject")
        column, rows = names[0], {}
        for row in reader:
            if row[column] in rows:
                raise ReviewError(f"the key names subject {row[column]!r} more than once")
            rows[row[column]] = row
    except (UnicodeDecodeError, csv.Error) as failure:
        raise ReviewError(f"the key is not CSV text: {failure}") from None
    return rows


def _pack_commit() -> str:
    """The commit of the pack checkout this library runs from, or `unknown`."""
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return UNKNOWN
    try:
        done = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
                              capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    commit = done.stdout.strip()
    return commit if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", commit) else UNKNOWN


def _lines(found: dict, key: bytes | None, rows: dict[str, dict] | None, now: str | None) -> list[dict]:
    header = found["header"]
    questions = found["questions"]
    manifest = {
        "kind": "manifest", "schema": SCHEMA, "batch": found["item"]["id"], "ref": found["ref"],
        "queue": header["queue"], "name": header["batch"], "title": found["item"]["title"],
        "closed": found["closed"], "source": header["source"], "mode": header["mode"],
        "seal": header["seal"], "digest": header["digest"],
        "exported_by": f"sd-ai-command-pack@{_pack_commit()}",
        "exported_at": _moment(now).astimezone(UTC).isoformat(timespec="seconds"),
        "questions": len(questions), "labels": found["labels"], "open": found["open"],
        "context": header["context"],
        # The labels a recommendation stood beside, how many agreed, and how many of each were ruled blind.
        "agreement": {"recommended": sum(question["agreed"] is not None for question in questions),
                      "agreed": sum(question["agreed"] is True for question in questions),
                      "ruled_blind": sum(question["ruled_blind"] is True for question in questions),
                      "agreed_blind": sum(question["agreed"] is True and question["ruled_blind"] is True
                                          for question in questions)},
    }
    if rows is not None and key is not None:
        subjects = {question["subject"] for question in questions}
        manifest["key"] = {"sha256": hashlib.sha256(key).hexdigest(), "rows": len(rows),
                           "unmatched": sum(subject not in subjects for subject in rows)}
    lines = [manifest]
    for question in questions:
        line = {"kind": "question", "note": question["note"], "subject": question["subject"],
                "question_sha256": question["sha256"], "evidence": question["evidence"], "options": question["options"],
                "label": question["label"], "position": question["position"], "ruling": question["ruling"],
                "ruled_by": question["ruled_by"], "ruled_at": question["ruled_at"],
                "expired": question["expired"], "recommendation": question["recommendation"],
                "agreed": question["agreed"], "ruled_blind": question["ruled_blind"],
                "withdrawn": [{"ruling": held["ruling"], "option": held["option"], "withdrawal": held["withdrawal"],
                               "at": held["withdrawn_at"]}
                              for held in question["rulings"] if held["withdrawal"] is not None],
                "comments": question["comments"], "late": question["late"]}
        if rows is not None:
            line["key"] = rows.get(question["subject"])
        lines.append(line)
    return lines


def export(connection: sqlite3.Connection, item: int, *, who: str, key: bytes | None = None,
           now: str | None = None) -> list[dict]:
    """The batch's labels as JSONL lines: a manifest, then one line per question.

    With `key` (a sealed batch's key file, as bytes) the export refuses a key
    whose SHA-256 is not the header's seal and refuses while a question is
    open; then it closes the batch, in its own committed transaction, and only
    then joins the key's row for each subject into the lines. A rerun on the
    closed batch with the same key exports again.
    """
    rows = None
    if key is not None:
        with transaction(connection):
            found = read(connection, item)
            header = found["header"]
            if header["mode"] != "sealed":
                raise ReviewError(f"sd:{item} is not sealed; export it without a key")
            digest = hashlib.sha256(key).hexdigest()
            if digest != header["seal"]:
                raise ReviewError(f"the key's SHA-256 {digest} is not the seal {header['seal']} in sd:{item}'s "
                                  f"header: it is another batch's key, or this key changed")
            if found["open"]:
                count = found["open"]
                raise ReviewError(f"sd:{item} has {count} question{'s' if count != 1 else ''} open; "
                                  f"a sealed export waits until every question is ruled")
            if found["closed"] is None:
                workflow.change_status(connection, item, "done", who=who,
                                       reason="sealed export: closed before the key is read")
        rows = _key_rows(key)
    return _lines(read(connection, item), key, rows, now)


def write(lines: list[dict], out: Path | str) -> None:
    """Write the export to `out` through `<out>.part` and a rename, owner-only."""
    out = Path(out)
    part = out.with_name(out.name + ".part")
    text = "".join(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n" for line in lines)
    descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, out)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- Kev label files (sd:4789)


def _kev_text(value: Any) -> str:
    """A Kev field as Kev's review app shows it: a string, or an object as `key: value` lines."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(f"{key}: {item if isinstance(item, str) else json.dumps(item)}"
                         for key, item in value.items())
    return "" if value is None else str(value)


def _kev_question(line: Any, where: str) -> dict:
    if not isinstance(line, dict):
        raise ReviewError(f"{where} is not a JSON object")
    question = line.get("question")
    if (not isinstance(line.get("id"), str) or not isinstance(line.get("document"), str)
            or not isinstance(line.get("proposed_label"), str) or not isinstance(question, dict)):
        raise ReviewError(f"{where} needs a string id, document and proposed_label, and a question object")
    options = question.get("options")
    if not isinstance(options, dict) or not 1 <= len(options) <= OPTION_COUNT.stop - 2:
        raise ReviewError(f"{where} needs question.options as an object of 1 to {OPTION_COUNT.stop - 2} "
                          f"keys; {DROP} is added as the last option")
    if DROP in options:
        raise ReviewError(f"{where} names an option {DROP}, which the import adds itself")
    proposed = line["proposed_label"]
    if proposed and proposed not in options:
        raise ReviewError(f"{where} proposes {proposed!r}, which is not one of its options")
    described = [f"{key}: {_kev_text(text)}" if text is not None else key for key, text in options.items()]
    text = "\n\n".join(part for part in (_kev_text(question.get("instructions")).strip(),
                                        "\n".join(described), line["document"].strip()) if part)
    evidence = [f"{PROPOSED}{proposed}"] if proposed else []
    judges = line.get("judges") or []
    if not isinstance(judges, list) or not all(isinstance(judge, dict) for judge in judges):
        raise ReviewError(f"{where} judges must be a list of objects")
    for judge in judges:
        said = " ".join(_kev_text(judge.get("rationale")).split())
        evidence.append(f"Judge {' '.join(_kev_text(judge.get('model')).split()) or 'unnamed'}: "
                        f"{' '.join(_kev_text(judge.get('label')).split()) or '-'}" + (f", {said}" if said else ""))
    return _question({"subject": line["id"], "text": text, "evidence": evidence,
                      "options": [*options, DROP]}, where)


def from_kev(text: str, *, batch: str, mode: str, source: str) -> dict:
    """A Kev label file (the JSONL Kev's tools/review reads) as a batch file for `file`.

    One question per non-blank line, in file order: the subject is the line's
    `id`; the text is the instructions, each option as `key: description`,
    then the document; the options are the option keys, then `DROP`. A
    proposed label and each judge become evidence lines, which a blind batch
    never shows. Every refusal names the line; nothing is cut or skipped.
    """
    if mode not in ("open", "blind"):
        raise ReviewError("a Kev file files open or blind")
    questions, seen = [], set()
    # Only a newline ends a JSONL record: splitlines would also cut at U+2028 inside a string.
    for number, raw in enumerate(text.split("\n"), 1):
        if not raw.strip():
            continue
        where = f"line {number}"
        try:
            line = json.loads(raw)
        except ValueError as failure:
            raise ReviewError(f"{where} is not JSON: {failure}") from None
        question = _kev_question(line, where)
        if question["subject"] in seen:
            raise ReviewError(f"{where} repeats id {question['subject']!r}")
        seen.add(question["subject"])
        questions.append(question)
    if not questions:
        raise ReviewError("the Kev file holds no line")
    if len(questions) > LIMIT:
        raise ReviewError(f"{len(questions)} lines; a batch holds at most {LIMIT}, so split the file")
    # A Kev file holds no context; this one says what Kev's tools read back (pack sd:9479).
    context = {"purpose": f"Label each line of {source[:200]} as its own instructions ask.",
               "rubric": ["Pick the option whose description fits the document.",
                          f"{DROP}: the line cannot be labelled as asked."],
               "feeds": "Kev's reviews.jsonl: `sd-db review export N --format reviews` writes each labelled line "
                        "as accept, relabel or drop."}
    found = {"schema": SCHEMA, "queue": KEV_QUEUE, "batch": _name(batch, "batch"), "title": f"Label: {batch}",
             "source": source, "mode": mode, "context": context, "questions": questions}
    _checked(found)
    return found


def reviews(lines: list[dict]) -> list[dict]:
    """`export` lines in Kev's reviews.jsonl shape: one line per labelled question, in batch order.

    `verdict` is `drop` for `DROP` (label null); `accept` when an open
    batch's label equals its non-empty proposed label; else `relabel`. A
    blind or sealed batch never reads `accept`: its labeller saw no proposal.
    """
    blind = lines[0]["mode"] != "open"
    out = []
    for line in lines[1:]:
        if line["label"] is None:
            continue
        proposed = next((entry[len(PROPOSED):] for entry in line["evidence"] if entry.startswith(PROPOSED)), "")
        verdict = ("drop" if line["label"] == DROP
                   else "accept" if proposed and line["label"] == proposed and not blind else "relabel")
        out.append({"id": line["subject"], "verdict": verdict, "label": None if verdict == "drop" else line["label"],
                    "proposed_label": proposed, "note": "\n".join(comment["text"] for comment in line["comments"]),
                    "reviewed_at": line["ruled_at"]})
    return out


# ---------------------------------------------------------------- apply


def _source_repo(connection: sqlite3.Connection, source: str) -> str | None:
    """The registered checkout a batch's `Source: <name>@<commit>` names, or None.

    The name is matched against each registered path's last part; none, or
    more than one, is None, and the task is filed with no repo.
    """
    name = source.partition("@")[0]
    found = [row["path"] for row in repos.registered(connection) if Path(row["path"]).name == name]
    return found[0] if len(found) == 1 else None


def _approval_body(batch: dict, question: dict, ruling: int) -> str:
    return "\n".join([question["text"], *(f"Evidence: {line}" for line in question["evidence"]),
                      f"Approved by ruling #{ruling} on note #{question['note']} "
                      f"of sd:{batch['item']['id']} ({batch['ref']})."])


def _approval_task(connection: sqlite3.Connection, batch: dict, question: dict, who: str, body: str = "", *,
                   session: str | None = None) -> int:
    """File an approval's task under `review-approved:<note>`; the caller checked the ref is free.

    Its repo is the batch's, else the one the header's Source names (`_source_repo`).
    """
    repo = batch["item"]["repo"] or _source_repo(connection, batch["header"]["source"])
    # P3, the usual rank: an automated filer names one (sd:6726); the operator moves it from there.
    task = workflow.capture_task(connection, title=question["text"].splitlines()[0], body=body, priority=3,
                                 repo=repo, who=who, session=session)["item"]["id"]
    writes.set_item_fields(connection, task, source=REF_SOURCE, external_id=f"{APPROVED}:{question['note']}")
    return task


def _approved_task(connection: sqlite3.Connection, batch: dict, question: dict, who: str) -> int | None:
    """File the task an approval asks for, once: None when its ref is filed already."""
    ref = f"{APPROVED}:{question['note']}"
    with transaction(connection):
        if writes.item_by_external(connection, REF_SOURCE, ref) is not None:
            return None
        return _approval_task(connection, batch, question, who, _approval_body(batch, question, question["ruling"]))


def _expire(connection: sqlite3.Connection, item: int, note: int, who: str) -> bool:
    """Settle one unruled question as expired; False when a ruling or a close landed first."""
    with transaction(connection):
        found = read(connection, item)
        if found["closed"] is not None or not any(q["note"] == note and q["open"] for q in found["questions"]):
            return False
        add_note(connection, item, "comment", f"On note #{note}: {EXPIRED}", session=who)
        resolve_note(connection, note)
        return True


def _close(connection: sqlite3.Connection, item: int, who: str, reason: str) -> bool:
    """Close the batch when nothing is open and every approval has its task; False otherwise."""
    with transaction(connection):
        found = read(connection, item)
        waiting = any(question["label"] == APPROVE
                      and writes.item_by_external(connection, REF_SOURCE, f"{APPROVED}:{question['note']}") is None
                      for question in found["questions"])
        if found["open"] or waiting or found["closed"] is not None:
            return False
        workflow.change_status(connection, item, "done", who=who, reason=reason)
        return True


def apply(connection: sqlite3.Connection, queue: str, *, who: str, now: str | None = None) -> dict:
    """The nightly queue's deterministic step: approvals to tasks, then close or expire each batch.

    Each approval files one task under `review-approved:<question note>`, in
    its own transaction; a ref already filed is skipped and never updated. A
    ruling through `rule` files its task itself, so only an approval written
    another way, such as the Decisions panel's two calls, files here. A
    batch with no open question closes. A batch older than 7 days resolves
    each open question as expired, one transaction each, then closes. Every
    step finds what an earlier stopped run left and finishes it.
    """
    _name(queue, "queue")
    moment = _moment(now)
    batches = [found for found in _queue(connection, queue) if found["closed"] is None]
    for found in batches:
        for question in found["questions"]:
            if question["options"] != [APPROVE, REJECT]:
                raise ReviewError(f"sd:{found['item']['id']} in queue {queue} asks note #{question['note']} with "
                                  f"other options; apply acts only on questions that offer Approve and Reject")
    result: dict[str, list] = {"tasks": [], "existing": [], "expired": [], "closed": []}
    for found in batches:
        item = found["item"]["id"]
        for question in found["questions"]:
            if question["label"] != APPROVE:
                continue
            task = _approved_task(connection, found, question, who)
            if task is None:
                result["existing"].append(question["note"])
            else:
                result["tasks"].append({"note": question["note"], "item": task, "subject": question["subject"]})
        opened = _moment(found["item"]["created_at"])
        if found["open"] and opened > moment - timedelta(days=EXPIRE_DAYS):
            continue
        for question in found["questions"]:
            if question["open"] and _expire(connection, item, question["note"], who):
                result["expired"].append(question["note"])
        reason = "every question ruled" if not found["open"] else f"unruled after {EXPIRE_DAYS} days"
        if _close(connection, item, who, f"review apply: {reason}"):
            result["closed"].append(item)
    return result
