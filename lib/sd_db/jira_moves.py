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

A failure is one comment note on the item, and the cursor still moves past
it, so a failure is said once, not every hour. A Jira that is not configured
moves nothing and holds the cursor.

The first run has no cursor. It checks the token by listing the transitions
of the newest linked ticket, read-only, and starts the cursor at the newest
note: history is not replayed. A failed check holds the cursor, so the next
run checks again.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse

from . import shadow_jira
from .writes import record_state, resolve_state

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
    """The linked items with a status change after note `after`, one row each, and the newest note id read."""
    rows = connection.execute(
        "SELECT note.id AS note, item.id AS item, item.status AS status, item.external_id AS ref"
        " FROM note JOIN item ON item.id = note.item"
        # An item's opening note is not a change: linking a ticket must not move it (`reads.py`'s test).
        " WHERE note.kind = 'status_change' AND substr(note.body, 1, 10) IS NOT 'opened as '"
        " AND note.id > ? ORDER BY note.id", (after,)).fetchall()
    newest = max([after] + [row["note"] for row in rows])
    items: dict[int, dict] = {}
    for row in rows:
        found = KEY.fullmatch(row["ref"] or "")
        if found:
            items[row["item"]] = {"item": row["item"], "status": row["status"], "key": found[1]}
    return list(items.values()), newest


def _issue(config: dict, key: str) -> str:
    return f"{config['base']}/rest/api/3/issue/{urllib.parse.quote(key)}"


def move(config: dict, key: str, status: str, transport=None) -> str:
    """Bring ticket `key` to the category of sd `status`: what was done, as a sentence; raises `ValueError`."""
    if status not in TARGET:
        return f"{key}: {status} moves nothing"
    category, name = TARGET[status]
    payload, error = shadow_jira._request(_issue(config, key) + "?fields=status", config, None, transport)
    if error:
        raise ValueError(error)
    current = (((payload or {}).get("fields") or {}).get("status") or {})
    if (current.get("statusCategory") or {}).get("key") == category:
        return f"{key}: already {current.get('name')}"
    payload, error = shadow_jira._request(_issue(config, key) + "/transitions", config, None, transport)
    if error:
        raise ValueError(error)
    offered = [t for t in (payload or {}).get("transitions") or []
               if isinstance(t, dict) and ((t.get("to") or {}).get("statusCategory") or {}).get("key") == category]
    if not offered:
        raise ValueError(f"no transition into {name} is offered from {current.get('name') or 'its status'}")
    chosen = next((t for t in offered if (t.get("to") or {}).get("name", "").lower() == name.lower()), offered[0])
    _, error = shadow_jira._request(_issue(config, key) + "/transitions", config,
                                    {"transition": {"id": str(chosen.get("id"))}}, transport)
    if error:
        raise ValueError(error)
    return f"{key}: moved to {(chosen.get('to') or {}).get('name') or name}"


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
            _, error = shadow_jira._request(_issue(config, key) + "/transitions", config, None, transport)
            if error:
                return [f"jira moves: token check on {key} failed, cursor held: {error}"]
            _write_cursor(connection, newest)
            return [f"jira moves: token check on {key} passed; cursor starts at note {newest}"]
        _write_cursor(connection, newest)
        return [f"jira moves: no linked ticket; cursor starts at note {newest}"]
    items, newest = _linked(connection, cursor)
    lines = []
    for entry in items:
        try:
            lines.append("jira moves: " + move(config, entry["key"], entry["status"], transport))
        except ValueError as error:
            text = f"Jira ticket {entry['key']} was not moved to match {entry['status']}: {error}"
            workflow.add_item_note(connection, entry["item"], kind="comment", body=text, who=WHO)
            lines.append(f"jira moves: item {entry['item']}: {text}")
    if newest != cursor:
        _write_cursor(connection, newest)
    return lines
