"""Move a linked Jira ticket when its sd item changes status (sd:8117).

The operator ruled on 2026-11-08 (sd:8117 #49086): a hub job, not a hook in
every status writer. This is that job's step: `shadow_sync._sync_jira` runs
it after the Jira collect, in the hourly `sd shadow sync`, with the same
`JIRA_*` environment and transport. Nothing else calls it.

It reads the `status_change` notes newer than its cursor, less each item's
opening note (`opened as ...`, which linking writes), keeps the items
whose ref is `jira:<KEY-N>` (`source = 'task-ref'`), and brings each ticket to
the status category of the item's current status: planning and ready to
"To Do", in_progress to "In Progress", done to "Done"; blocked and any other
status move nothing. A ticket already in that category is left alone. The
transition is the first the ticket offers into that category, one named
like the category first.

`done` is also how sd records work nobody did: `progress.cancel_work` and
the priority sweep's closure. Neither is finished work, so each leaves the
ticket as it is, and the sync says so. A done item whose stored fields
cannot be read is unknown, so it fails rather than moves.

A permanent failure, one an answer from Jira pins on that ticket (a
JSON answer of the wrong shape, no matching transition, a 4xx other than
401, 403 or 429), is one comment note on the item, and the cursor still moves past it, so it is
said once, not every hour. Every field read from a Jira answer is checked,
so a malformed answer is that item's failure too and the later moves
continue. Every other failure is transient (`shadow_jira.Transient`: any
failure of the transport, a refused redirect, a 401, 403, 429 or 5xx, a
body that is not JSON) and stops the batch with no note: the cursor stays
at the last entry fully handled, the run's lines say so, and the next run
resumes at the failed entry. Entries run in the order of the note that
decides them and the cursor is written after each, so neither a stop nor a
crash repeats an earlier move or note. A ticket stuck on a transient error
holds the later ones until it clears, and says so every hour. A Jira that
is not configured moves nothing and holds the cursor.

The first run has no cursor. It checks the token by listing the transitions
of the newest linked ticket, read-only, and starts the cursor at the newest
note: history is not replayed. A failed check, a malformed answer included,
holds the cursor, so the next run checks again; no move waits on it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse

from . import shadow_jira
from .writes import record_state, resolve_state


class _Retry(Exception):
    """A transient failure of one request: not a `ValueError`, so no per-item handler takes it."""


#: The `checkpoint` key the cursor lives under: `{"note": <last note id read>}`.
CURSOR = "jira-moves"
WHO = "jira-moves"
KEY = re.compile(r"jira:([A-Z][A-Z0-9_]*-[1-9][0-9]*)")
#: sd status -> Jira status category key, and the name preferred inside it.
TARGET = {"planning": ("new", "To Do"), "ready": ("new", "To Do"),
          "in_progress": ("indeterminate", "In Progress"), "done": ("done", "Done")}


def read_cursor(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT body FROM state WHERE kind = 'checkpoint' AND key = ? AND resolved_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1", (CURSOR,)).fetchone()
    try:
        return int(json.loads(row["body"])["note"]) if row else None
    except (TypeError, ValueError, KeyError):
        return None


def _write_cursor(connection: sqlite3.Connection, note: int) -> None:
    resolve_state(connection, record_state(connection, "checkpoint", key=CURSOR, body={"note": note}))


def _linked(connection: sqlite3.Connection, after: int) -> tuple[list[dict], int]:
    """The linked items with a status change after note `after`, oldest deciding note first, and the newest note id read."""
    rows = connection.execute(
        "SELECT note.id AS note, note.body AS body, item.id AS item, item.status AS status,"
        " item.external_id AS ref, item.fields AS fields"
        " FROM note JOIN item ON item.id = note.item"
        # An item's opening note is not a change: linking a ticket must not move it (`reads.py`'s test).
        " WHERE note.kind = 'status_change' AND substr(note.body, 1, 10) IS NOT 'opened as '"
        " AND note.id > ? ORDER BY note.id", (after,)).fetchall()
    newest = max([after] + [row["note"] for row in rows])
    items: dict[int, dict] = {}
    for row in rows:
        found = KEY.fullmatch(row["ref"] or "")
        if found:
            # The newest note in the window wins: it is the one that set the item's status.
            items[row["item"]] = {"item": row["item"], "status": row["status"], "key": found[1],
                                  "body": row["body"], "fields": row["fields"], "note": row["note"]}
    return sorted(items.values(), key=lambda entry: entry["note"]), newest


def _stop(error: str) -> None:
    """Raise for a failed request: `_Retry` for a transient one, `ValueError` for the rest; nothing for no error."""
    if error:
        raise _Retry(error) if isinstance(error, shadow_jira.Transient) else ValueError(error)


def _issue(config: dict, key: str) -> str:
    return f"{config['base']}/rest/api/3/issue/{urllib.parse.quote(key)}"


def _object(value: object, what: str) -> dict:
    """`value` when it is a JSON object; otherwise that item's failure, never an error that stops the batch."""
    if not isinstance(value, dict):
        raise ValueError(f"Jira's answer has no {what} object")
    return value


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Jira's answer has no {what}")
    return value


def _state(value: object) -> tuple[str, str]:
    """The name and the category key of a Jira status object."""
    state = _object(value, "status")
    category = _object(state.get("statusCategory"), "status category")
    return _text(state.get("name"), "status name"), _text(category.get("key"), "status category key")


def _transitions(payload: object) -> list[tuple[str, str, str]]:
    """Each transition offered, as (id, target name, target category key); a malformed one raises `ValueError`."""
    listed = _object(payload, "transition list").get("transitions")
    if not isinstance(listed, list):
        raise ValueError("Jira's answer has no list of transitions")
    offers = []
    for entry in listed:
        offer = _object(entry, "transition")
        name, category = _state(offer.get("to"))
        offers.append((_text(offer.get("id"), "transition id"), name, category))
    return offers


def _nobody_did(entry: dict) -> str:
    """Why this item's `done` is not finished work, or "": said on the sync's line, and the ticket stays."""
    from .priority_sweep import CLOSE_REASON

    if entry["status"] != "done":
        return ""
    if f": {CLOSE_REASON}" in entry["body"]:
        return "closed by the priority sweep"
    try:
        fields = json.loads(entry["fields"] or "{}")
    except (TypeError, ValueError, RecursionError):
        fields = None
    if not isinstance(fields, dict):
        raise ValueError("its stored fields cannot be read")
    completion = fields.get("completion")
    return "cancelled" if isinstance(completion, dict) and completion.get("outcome") == "cancelled" else ""


def move(config: dict, key: str, status: str, transport=None) -> str:
    """Bring ticket `key` to the category of sd `status`: what was done, as a sentence.

    Raises `ValueError` for a permanent failure and `_Retry` for a transient one. A retry after a transition that
    timed out but landed reads the ticket as already there and posts nothing.
    """
    if status not in TARGET:
        return f"{key}: {status} moves nothing"
    category, name = TARGET[status]
    payload, error = shadow_jira._request(_issue(config, key) + "?fields=status", config, None, transport)
    _stop(error)
    current, current_category = _state(_object(_object(payload, "issue").get("fields"), "fields").get("status"))
    if current_category == category:
        return f"{key}: already {current}"
    payload, error = shadow_jira._request(_issue(config, key) + "/transitions", config, None, transport)
    _stop(error)
    offered = [offer for offer in _transitions(payload) if offer[2] == category]
    if not offered:
        raise ValueError(f"no transition into {name} is offered from {current}")
    chosen = next((offer for offer in offered if offer[1].lower() == name.lower()), offered[0])
    _, error = shadow_jira._request(_issue(config, key) + "/transitions", config,
                                    {"transition": {"id": chosen[0]}}, transport)
    _stop(error)
    return f"{key}: moved to {chosen[1]}"


def run(connection: sqlite3.Connection, transport=None, environ=None) -> list[str]:
    """One pass: the lines `sd shadow sync` prints. Never raises for Jira's answers."""
    from . import workflow

    config = shadow_jira.settings(environ)
    if shadow_jira.missing(config):
        return []
    cursor = read_cursor(connection)
    if cursor is None:
        newest = connection.execute("SELECT COALESCE(MAX(id), 0) FROM note").fetchone()[0]
        last = connection.execute(
            "SELECT external_id FROM item WHERE source = 'task-ref' AND external_id LIKE 'jira:%'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        found = KEY.fullmatch(last[0]) if last is not None else None
        if found:
            key = found[1]
            payload, error = shadow_jira._request(_issue(config, key) + "/transitions", config, None, transport)
            if not error:
                try:
                    _transitions(payload)
                except ValueError as malformed:
                    error = str(malformed)
            if error:
                return [f"jira moves: token check on {key} failed, cursor held: {error}"]
            _write_cursor(connection, newest)
            return [f"jira moves: token check on {key} passed; cursor starts at note {newest}"]
        _write_cursor(connection, newest)
        return [f"jira moves: no linked ticket; cursor starts at note {newest}"]
    items, newest = _linked(connection, cursor)
    lines: list[str] = []
    at = cursor
    for entry in items:
        try:
            why = _nobody_did(entry)
            said = f"{entry['key']}: {why}; ticket left as it is" if why else move(
                config, entry["key"], entry["status"], transport)
            lines.append("jira moves: " + said)
        except _Retry as error:
            lines.append(f"jira moves: {entry['key']}: not moved, cursor held for the next run: {error}")
            return lines
        except ValueError as error:
            text = f"Jira ticket {entry['key']} was not moved to match {entry['status']}: {error}"
            workflow.add_item_note(connection, entry["item"], kind="comment", body=text, who=WHO)
            lines.append(f"jira moves: item {entry['item']}: {text}")
        # This entry is done for good: a stop or a crash at a later one repeats neither its move nor its note.
        at = entry["note"]
        _write_cursor(connection, at)
    if newest != at:
        _write_cursor(connection, newest)
    return lines
