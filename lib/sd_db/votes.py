"""Board votes: the `vote` table's one writer and its reads (pack sd:8050).

One row per vote or decision, keyed by `(repo, key)`, such as the hoa
checkout's `CI-1`. The columns follow that checkout's `votes.csv`, so
`import_rows` takes its rows as `csv.DictReader` gives them; its `id`
column is the key. Design: `docs/work/2026-11-07-vote-table/design.md`.

- `votes` keeps the cell as written. `tally` is what `parse_votes` reads
  from it: each director's `yes`, `no`, `abstain`, `absent` or `none`. A
  cell it cannot read whole gives no tally, never part of one.
- `state` is `open` or `closed`. A write that names no state takes it from
  the outcome: one that starts with `pending` is open (`state_of`), as the
  hoa workbook reads it.
- `revision` is 1 on add and rises by one with each change. An edit that
  names another revision is refused (`StaleVote`), and the `UPDATE` checks
  it again in its `WHERE` clause.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from .database import transaction
from .errors import SdDbError
from .repos import row_for
from .writes import now

#: The text columns, in `votes.csv`'s order after its `id`.
FIELDS = ("date", "topic", "motion", "type", "moved", "seconded", "votes", "outcome", "follow_up", "source", "notes")
#: The columns an import reads: the key and the text columns.
CSV_COLUMNS = ("id", *FIELDS)
STATES = ("open", "closed")
#: The most characters one cell holds. A guard against a pasted file, not a
#: measured limit: the longest cell of the register this was built for is 353.
MAX_TEXT = 10_000
#: A key: ASCII letters, digits, `.`, `_` and `-`, starting with a letter or digit.
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

#: The words a votes cell ends a segment with, and the answer each records.
VERDICTS = {"yes": "yes", "no": "no", "abstain": "abstain", "abstained": "abstain", "abstains": "abstain",
            "absent": "absent", "none": "none", "agreed": "yes", "approved": "yes"}
#: `5-0:` or `unanimous:` before the names.
HEADER = re.compile(r"(?:(\d+)\s*-\s*(\d+)|unanimous)\s*:\s*(.*)", re.IGNORECASE | re.DOTALL)
#: One director: one to three words, each starting with a letter.
NAME = re.compile(r"[^\W\d_][\w'.-]*(?: [^\W\d_][\w'.-]*){0,2}")
ASIDE = re.compile(r"\s*\([^()]*\)")
#: A votes cell longer than this is prose, not a tally.
TALLY_TEXT = 2_000


class VoteRefused(SdDbError):
    """A vote write was refused; nothing was written."""


class StaleVote(VoteRefused):
    """The vote changed since the revision the caller read."""


def state_of(outcome: str | None) -> str:
    """`open` for an outcome that starts with `pending`, any case, else `closed`."""
    return "open" if str(outcome or "").strip().lower().startswith("pending") else "closed"


def parse_votes(text: str | None) -> dict[str, str] | None:
    """Each director's answer in a votes cell, or None when the cell names none or is not read whole.

    A cell is `;`-separated segments, each a list of names (`,` or `and`, any case)
    then a verdict word: `Ada, Ben Yes; Cy No`. A `N-M:` or `unanimous:`
    head may come first; then the first segment may list names alone, which
    read yes when the head is unanimous or `N-0`, and a `N-M` head must
    match the yes and no counts. An aside in parentheses is dropped.
    """
    body = ASIDE.sub("", str(text or "")).strip()
    if not body or len(body) > TALLY_TEXT:
        return None
    head = HEADER.fullmatch(body)
    counts = None
    if head:
        if head.group(1) is not None:
            counts = (int(head.group(1)), int(head.group(2)))
        body = head.group(3)
    all_yes = bool(head) and (counts is None or counts[1] == 0)
    tally: dict[str, str] = {}
    for index, segment in enumerate(part.strip() for part in body.split(";")):
        if not segment:
            return None
        words = segment.rsplit(None, 1)
        verdict = VERDICTS.get(words[-1].lower()) if len(words) == 2 else None
        if verdict is not None:
            names_text = words[0]
        elif index == 0 and all_yes:
            names_text, verdict = segment, "yes"
        else:
            return None
        names = [name.strip() for name in re.split(r",|\band\b", names_text, flags=re.IGNORECASE)]
        # A name holding a verdict word is no director's: `Ada Yes, Ben No` used `,` for `;` (sd:8063).
        if any(not NAME.fullmatch(name) or VERDICTS.keys() & set(name.lower().split()) for name in names):
            return None
        for name in names:
            if name in tally:
                return None
            tally[name] = verdict
    if counts is not None:
        answers = list(tally.values())
        if (answers.count("yes"), answers.count("no")) != counts:
            return None
    return tally or None


def _clean(fields: Mapping[str, Any], where: str) -> dict[str, Any]:
    """The text columns `fields` names, stripped and checked; a refusal names `where` and the column."""
    cleaned: dict[str, Any] = {}
    for column, value in fields.items():
        if column not in FIELDS:
            raise VoteRefused(f"{where}: {column!r} is not a vote column; expected one of {', '.join(FIELDS)}")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise VoteRefused(f"{where}: {column} is not text")
        value = value.strip()
        if len(value) > MAX_TEXT:
            raise VoteRefused(f"{where}: {column} holds {len(value)} characters; the most is {MAX_TEXT}")
        if column == "date":
            value = _day(value, where)
        cleaned[column] = value
    return cleaned


def _day(value: str, where: str) -> str | None:
    if not value:
        return None
    try:
        if DAY.fullmatch(value) and date.fromisoformat(value):
            return value
    except ValueError:
        pass
    raise VoteRefused(f"{where}: date {value!r} is not a YYYY-MM-DD day")


def _key(key: Any, where: str) -> str:
    text = key.strip() if isinstance(key, str) else ""
    if not KEY.fullmatch(text):
        raise VoteRefused(f"{where}: key {str(key)[:80]!r} is not a vote key: ASCII letters, digits, '.', '_' or '-', "
                          f"64 at most, starting with a letter or digit")
    return text


def _state(state: Any, where: str) -> str:
    if state not in STATES:
        raise VoteRefused(f"{where}: state {state!r} is not open or closed")
    return str(state)


def _stored(connection: sqlite3.Connection, repo: str) -> str | None:
    """The repo row's own path for a key or a disk path (`repos.row_for`), or None: every lookup binds that."""
    found = row_for(connection, repo)
    return None if found is None else str(found["path"])


def _repo(connection: sqlite3.Connection, repo: str) -> str:
    stored = _stored(connection, repo)
    if stored is None:
        raise VoteRefused(f"{repo} is not a registered repo")
    return stored


def _item(connection: sqlite3.Connection, item: Any, where: str) -> int | None:
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item < 2**63:
        raise VoteRefused(f"{where}: item {item!r} is not an item id")
    if connection.execute("SELECT 1 FROM item WHERE id = ?", (item,)).fetchone() is None:
        raise VoteRefused(f"{where}: no item sd:{item}")
    return item


def _shape(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    found = dict(row)
    found["tally"] = json.loads(found["tally"]) if found["tally"] is not None else None
    return found


def _tally_text(votes_text: str) -> str | None:
    tally = parse_votes(votes_text)
    return None if tally is None else json.dumps(tally, ensure_ascii=False, separators=(",", ":"))


def get(connection: sqlite3.Connection, repo: str, key: str) -> dict[str, Any] | None:
    """One vote by its repo, a key or a disk path, and its key; or None."""
    stored = _stored(connection, repo)
    return None if stored is None else _get(connection, stored, key)


def _get(connection: sqlite3.Connection, stored: str, key: str) -> dict[str, Any] | None:
    return _shape(connection.execute("SELECT * FROM vote WHERE repo = ? AND key = ?", (stored, key)).fetchone())


def rows(connection: sqlite3.Connection, *, repo: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
    """Votes, open first, then newest first, a vote with no date last; one repo's when `repo` names it."""
    where, arguments = [], []
    if repo is not None:
        stored = _stored(connection, repo)
        if stored is None:
            return []
        where.append("repo = ?")
        arguments.append(stored)
    if state is not None:
        where.append("state = ?")
        arguments.append(_state(state, "rows"))
    sql = ("SELECT * FROM vote" + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY (state = 'open') DESC, (date IS NULL), date DESC, key")
    return [found for found in map(_shape, connection.execute(sql, arguments)) if found is not None]


def _write(connection: sqlite3.Connection, repo: str, key: str, values: dict[str, Any],
           existing: dict[str, Any] | None, stamp: str) -> None:
    """Insert one vote, or update `existing` at its revision; the caller holds the transaction."""
    if existing is None:
        columns = ("repo", "key", *values, "created_at", "updated_at")
        connection.execute(f"INSERT INTO vote ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                           (repo, key, *values.values(), stamp, stamp))
        return
    assignments = ", ".join(f"{column} = ?" for column in values)
    changed = connection.execute(
        f"UPDATE vote SET {assignments}, revision = revision + 1, updated_at = ? WHERE id = ? AND revision = ?",
        (*values.values(), stamp, existing["id"], existing["revision"])).rowcount
    if changed != 1:
        raise StaleVote(f"vote {key} changed while it was written; read it again")


def _values(fields: dict[str, Any], state: str, item: int | None, *, keep_item: bool = False) -> dict[str, Any]:
    """Every stored column but the key, revision and stamps, from cleaned text columns."""
    values: dict[str, Any] = {column: fields.get(column, None if column == "date" else "") for column in FIELDS}
    values["tally"] = _tally_text(str(values["votes"]))
    values["state"] = state
    if not keep_item:
        values["item"] = item
    return values


def _same(existing: dict[str, Any], values: dict[str, Any]) -> bool:
    """Whether `values` would store what `existing` holds; `tally` follows `votes`, so it is not compared."""
    return all(existing[column] == value for column, value in values.items() if column != "tally")


def add(connection: sqlite3.Connection, repo: str, key: str, fields: Mapping[str, Any], *,
        state: str | None = None, item: int | None = None) -> dict[str, Any]:
    """Add one vote at revision 1; a key the repo already holds is refused."""
    key = _key(key, "add")
    cleaned = _clean(fields, f"vote {key}")
    chosen = _state(state, f"vote {key}") if state is not None else state_of(cleaned.get("outcome"))
    with transaction(connection):
        repo = _repo(connection, repo)
        linked = _item(connection, item, f"vote {key}")
        if _get(connection, repo, key) is not None:
            raise VoteRefused(f"vote {key} is already in {repo}; edit it instead")
        _write(connection, repo, key, _values(cleaned, chosen, linked), None, now())
    found = _get(connection, repo, key)
    assert found is not None
    return found


def edit(connection: sqlite3.Connection, repo: str, key: str, changes: Mapping[str, Any], *,
         expected_revision: int | None = None) -> dict[str, Any]:
    """Change the columns `changes` names: text columns, `state` and `item` (None clears the link).

    A new outcome with no state given sets the state from the outcome. A
    change that leaves every column as it was writes nothing and keeps the
    revision.
    """
    where = f"vote {key}"
    text = _clean({column: value for column, value in changes.items() if column not in ("state", "item")}, where)
    with transaction(connection):
        repo = _repo(connection, repo)
        existing = _get(connection, repo, key)
        if existing is None:
            raise VoteRefused(f"no vote {key} in {repo}")
        if expected_revision is not None and expected_revision != existing["revision"]:
            raise StaleVote(f"vote {key} is at revision {existing['revision']}, not {expected_revision}; "
                            f"read it again")
        fields = {column: existing[column] for column in FIELDS} | text
        if "state" in changes:
            state = _state(changes["state"], where)
        elif "outcome" in text:
            state = state_of(text["outcome"])
        else:
            state = existing["state"]
        item = _item(connection, changes["item"], where) if "item" in changes else existing["item"]
        values = _values(fields, state, item)
        if _same(existing, values):
            return existing
        _write(connection, repo, key, values, existing, now())
    found = _get(connection, repo, key)
    assert found is not None
    return found


def import_rows(connection: sqlite3.Connection, repo: str, file: Iterable[Mapping[Any, Any]]) -> dict[str, int]:
    """Load rows shaped like `votes.csv` into `repo`, in one transaction; return the counts.

    Every row is read and checked before the first write, and the writes
    commit together, so a refused or stopped import writes nothing. A key the
    repo holds is updated in place with its item link kept, and a row equal to
    the stored one is not written: a second import of one file changes nothing.
    The file wins over an edit made since its first import. The state comes
    from each row's outcome.
    """
    planned: dict[str, tuple[int, dict[str, Any]]] = {}
    for number, raw in enumerate(file, start=2):  # line 1 is the header
        where = f"row {number}"
        if None in raw:
            raise VoteRefused(f"{where}: the row has more cells than the header")
        unknown = [str(column) for column in raw if column not in CSV_COLUMNS]
        if unknown:
            raise VoteRefused(f"{where}: {', '.join(unknown)} is not a votes.csv column; expected "
                              f"{', '.join(CSV_COLUMNS)}")
        # Every column, or an update would blank the ones the file left out (lead review of sd:8050).
        missing = [column for column in CSV_COLUMNS if column not in raw]
        if missing:
            raise VoteRefused(f"{where}: the header has no {', '.join(missing)} column; a votes.csv names every one "
                              f"of {', '.join(CSV_COLUMNS)}")
        key = _key(raw["id"], where)
        where = f"{where} ({key})"
        # csv.DictReader gives a short row's missing cells as None; read as '' they would erase stored text.
        if any(value is None for value in raw.values()):
            raise VoteRefused(f"{where}: the row has fewer cells than the header")
        if key in planned:
            raise VoteRefused(f"{where}: key {key} is also row {planned[key][0]}; one row per key")
        cleaned = _clean({column: value for column, value in raw.items() if column != "id"}, where)
        planned[key] = (number, cleaned)
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    stamp = now()
    with transaction(connection):
        repo = _repo(connection, repo)
        for key, (_, cleaned) in planned.items():
            existing = _get(connection, repo, key)
            values = _values(cleaned, state_of(cleaned.get("outcome")), None, keep_item=True)
            if existing is not None and _same(existing, values):
                counts["unchanged"] += 1
                continue
            _write(connection, repo, key, values, existing, stamp)
            counts["added" if existing is None else "updated"] += 1
    return counts
