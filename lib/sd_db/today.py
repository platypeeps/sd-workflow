"""The sections `sd today`, `sd-status` and the dashboard show, as plain dicts (sd:5098).

Each function reads the shared database at run time and takes an optional
`repos` filter: the `item.repo` keys (`~/repos/name`) to keep, None among
them for the items in no repository, or None for every repository. A lane entry is keyed by its GitHub repository, so it is
matched to a key through the `repo` row's remote. Every row carries `id` (the
item), `repo` (its key, or None) and `title`; the rest is per section, as each
docstring says. `sections` reads them all in the one order the renderers use.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import hub
from .build import HEALTH
from .reads import (
    IDEA_KINDS,
    REVIEW_BATCH,
    WORK_KIND,
    _now,
    _parse,
    _unparked_clause,
    backlog_items,
    open_notes,
    repo_in,
    scheduled_clause,
    today_items,
)
from .reporting import _current_basis
from .runner import OWNING
from .ship import this_host

#: `sd_lane.KEY_PREFIX`: a lane entry is the newest `checkpoint` row of its key.
LANE_KEY_PREFIX = "lane:v1:"
#: Lane states in the order `landing` lists them; the queue holds the first two.
LANE_ORDER = ("running", "pending", "prepared", "handed_back", "failed", "skipped")
#: The section keys of `sections`, in the order every renderer prints them.
ORDER = ("attention", "operations", "decisions", "landing", "queues", "delivered", "planned", "blocked", "unscheduled", "followups", "work")
#: A choice line of a decision, as the dashboard's Decisions reads one (sd:3012).
OPTION_LINE = re.compile(r"^[ \t]*Option:[ \t]*\S", re.MULTILINE)
#: Kinds that are not work to schedule: ideas wait for promotion, reports for acknowledgement.
_UNSCHEDULED_SKIPS = (*IDEA_KINDS, "report")
_DELIVERED_AT = re.compile(r"\bdelivered at ([0-9a-f]{40,64})\b")
_CODE_DELIVERY = re.compile(r"Code delivery (\S+) at ([0-9a-f]{40,64})\b")
_PULL = re.compile(r"/pull/([0-9]+)\b")
_BLOCKED_BECAUSE = re.compile(r"-> blocked by [^:\n]*: *(.*)")
_WAITING = re.compile(r"\s*(?:waiting|waits)\s+(?:on|for)\b", re.IGNORECASE)
#: A weekly grill: the task the KISS audit files under `sd task add --ref grill:<date>` (pack sd:6022).
GRILL = "(item.source IS 'task-ref' AND substr(item.external_id, 1, 6) IS 'grill:')"


def _keep(repos: Iterable[str | None] | None) -> set[str | None] | None:
    return None if repos is None else set(repos)


def _first_line(text: Any) -> str:
    return str(text or "").strip().partition("\n")[0]


def _attention_rows(connection: sqlite3.Connection, keep: set[str | None] | None) -> list[tuple[Any, Any]]:
    """Each open report whose fields say it needs attention, newest first, with that `attention` value."""
    where, values = repo_in("repo", keep)
    rows = connection.execute(
        f"SELECT * FROM item WHERE kind = 'report' AND source IS NOT 'brief' AND status != 'done' AND {where}"
        " ORDER BY created_at DESC, id DESC", values).fetchall()
    found = []
    for row in rows:
        try:
            why = json.loads(row["fields"] or "{}").get("attention")
        except (ValueError, AttributeError):
            why = "fields unreadable"
        if why:
            found.append((row, why))
    return found


#: A satellite's request for the hub's mail, Slack, Notion or Drive work: `sd task add --ref hub:<host>-<slug>`
#: (system sd:7324 H2). The exact source, so a mistyped ref is no request.
HUB_REQUEST = "(item.source IS 'task-ref' AND substr(item.external_id, 1, 4) IS 'hub:')"
#: A hub request's Attention `why`: what the hub session does with it.
HUB_WHY = "hub request: claim it, answer in a note, close it"


def _hub_rows(connection: sqlite3.Connection, keep: set[str | None] | None) -> list[tuple[Any, str]]:
    """Each open hub request, with its `why`, when this machine holds the hub role (pack sd:7358).

    The role is `sd-db fence`'s (`hub.machine_role`), asked only when a
    request is open: `holder` and `off` (one machine, no hub named) list
    them, `elsewhere` (a satellite) lists none, and `unknown` lists them
    with the reason, as a failed read is no satellite.
    """
    where, values = repo_in("item.repo", keep)
    rows = connection.execute(
        f"SELECT * FROM item WHERE item.status != 'done' AND {HUB_REQUEST} AND {where}"
        " ORDER BY created_at DESC, id DESC", values).fetchall()
    if not rows:
        return []
    found = hub.machine_role(connection)
    if found.state == "elsewhere":
        return []
    why = HUB_WHY if found.state in ("holder", "off") else f"{HUB_WHY}; hub role unknown: {found.reason}"
    return [(row, why) for row in rows]


def attention(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None) -> list[dict]:
    """Open reports whose fields say they need attention, and on the hub open hub requests (`_hub_rows`),
    newest first: `status`, `why`, `at`.

    Unreadable fields count, as an unknown is no pass.
    """
    keep = _keep(repos)
    rows = sorted([*_attention_rows(connection, keep), *_hub_rows(connection, keep)],
                  key=lambda pair: (pair[0]["created_at"], pair[0]["id"]), reverse=True)
    return [{"id": row["id"], "repo": row["repo"], "status": row["status"], "title": row["title"],
             "why": why if isinstance(why, str) else "", "at": row["created_at"]}
            for row, why in rows]


#: The finding of a report whose fields do not parse: it may name this machine, so it is a row, not a pass.
FIELDS_UNREAD = "fields unreadable; whether this report names this machine is unknown"


def _host_findings(basis: Any, host: str) -> list[str]:
    """The lines of a report's attention basis that name `host`, each cut to its finding.

    A cron report writes `<job> on <host>: <finding>` per line, and a failed
    run `job exited N on <host>`, with a grade in brackets and the job's
    first line after a colon when it has them (`reporting.ingest_log`). That
    line keeps all but ` on <host>`, so its finding names the exit code
    (pack sd:7399). A line that only heads the next ones (`App Store updates
    pending:` over `App Store updates pending: <app>`) is dropped, as the
    lines under it say it.
    """
    host = re.escape(host)
    exited = re.compile(rf"^(?P<head>job exited -?[0-9]+) on {host}(?P<rest>(?:[ \t]+\(.*|:.*)?)$")
    pattern = re.compile(rf"^(?P<head>.*?) on {host}(?::[ \t]*(?P<finding>.*)|[ \t]*)$")
    found = []
    for line in str(basis or "").splitlines():
        if match := exited.match(line):
            found.append((match["head"] + match["rest"]).strip())
        elif match := pattern.match(line):
            found.append((match["finding"] or match["head"]).strip())
    return [text for text in found
            if not (text.endswith(":") and any(other != text and other.startswith(text) for other in found))]


def machine_gate(gate: Any) -> dict | None:
    """`_gate`'s holders and waiters, with the slot cap, the load and its limit when the snapshot has them."""
    found = _gate(gate)
    if found is not None and isinstance(gate, dict):
        found.update({key: gate.get(key) for key in ("slots", "load", "load_max")})
    return found


def operations(connection: sqlite3.Connection, *, host: str | None = None, gate: Any = None,
               repos: Iterable[str | None] | None = None) -> dict:
    """This machine's findings and gate (pack sd:5432): `host`, `findings` and `gate` (`machine_gate`).

    The reports are `attention`'s, of `repos` (pack sd:6488); the gate is the
    machine's, whatever the filter. Each finding row is one line of a report's current basis
    (`reporting._current_basis`) that names `host`, by default `this_host`,
    `hostname -s` lower-cased: `id`, `repo`, `title`, `job` (the
    `external_id` before `:`) and `finding`. A report with no `fields.report`
    names no host; one whose fields do not parse is a `FIELDS_UNREAD` row.
    """
    host = host or this_host()
    findings = []
    for row, _ in _attention_rows(connection, _keep(repos)):
        try:
            lines = _host_findings(_current_basis({"fields": row["fields"] or "{}"}), host)
        except KeyError:
            lines = []
        except (ValueError, TypeError, AttributeError):
            lines = [FIELDS_UNREAD]
        job = str(row["external_id"] or "").partition(":")[0] or "-"
        findings += [{"id": row["id"], "repo": row["repo"], "title": row["title"], "job": job, "finding": text}
                     for text in lines]
    return {"host": host, "findings": findings, "gate": machine_gate(gate)}


def _note_row(note: sqlite3.Row) -> dict:
    return {"id": note["item"], "note": note["id"], "repo": note["item_repo"], "status": note["item_status"],
            "title": _first_line(note["body"]), "item_title": note["item_title"], "at": note["timestamp"]}


def decisions(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None) -> list[dict]:
    """Open questions with two or more `Option:` lines, oldest first: `note` (what `--answers` takes),
    `title` (the question's first line), `item_title`, `at`.

    A review batch's questions are `review_questions`' count, not rows: the
    dashboard's Review page labels them, and `sd-next` walks these rows (sd:5130).
    The read has no cap, as `decisions.document`'s (pack sd:6338): plain and
    grill questions it leaves out filled one and hid rows (pack sd:7400).
    """
    grilled = {row["id"] for row in grill(connection, repos=repos)}
    notes = open_notes(connection, "question", batches=False, limit=None, repos=_keep(repos))
    return [_note_row(note) for note in notes
            if len(OPTION_LINE.findall(note["body"] or "")) >= 2 and note["item"] not in grilled]


def grill(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None) -> list[dict]:
    """Open grill items with open questions, oldest first: `questions` (how many wait), `at` (when filed).

    Their questions are no `decisions` rows: the operator answers them in one
    fresh session by saying 'grill', and `sd today` says the grill is ready
    (pack sd:6022).
    """
    where, values = repo_in("item.repo", _keep(repos))
    return [dict(row) for row in connection.execute(
        f"""
        SELECT item.id, item.repo, item.title, item.created_at AS at, COUNT(note.id) AS questions
          FROM item JOIN note ON note.item = item.id AND note.kind = 'question' AND note.resolved_at IS NULL
         WHERE item.status != 'done' AND {_unparked_clause(connection)} AND {GRILL} AND {where}
         GROUP BY item.id ORDER BY item.created_at ASC, item.id ASC
        """, values)]


def review_questions(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None) -> int:
    """How many open questions review batches hold, on items neither done nor parked (sd:5130)."""
    where, values = repo_in("item.repo", _keep(repos))
    return connection.execute(
        f"SELECT COUNT(*) FROM note JOIN item ON item.id = note.item"
        f" WHERE note.kind = 'question' AND note.resolved_at IS NULL AND item.status != 'done'"
        f" AND {_unparked_clause(connection)} AND {REVIEW_BATCH} AND {where}", values).fetchone()[0]


def followups(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None) -> list[dict]:
    """Open followups, oldest first, except a report's own, which is the report's attention row:
    `note`, `title` (the followup's first line), `item_title`, `at`."""
    keep = _keep(repos)
    reports = {row["id"] for row, _ in _attention_rows(connection, keep)}
    return [_note_row(note) for note in open_notes(connection, "followup", repos=keep, without=reports)]


def _closed(connection: sqlite3.Connection, repos: Iterable[str | None] | None, now: str | None,
            hours: int) -> list[dict]:
    """Every item closed or merged in the last `hours`, newest first, with its `pr` and `commit` or None."""
    cutoff = (_parse(_now(now)) or datetime.now(timezone.utc)) - timedelta(hours=hours)
    where, values = repo_in("item.repo", _keep(repos))
    rows = connection.execute(
        f"""
        SELECT note.item, note.body, note.timestamp, item.title, item.repo
          FROM note JOIN item ON item.id = note.item
         WHERE substr(note.timestamp, 1, 19) >= ? AND {where}
           AND ((note.kind = 'status_change' AND note.body LIKE '%-> done%')
             OR (note.kind = 'comment' AND note.body LIKE 'Code delivery %'))
         ORDER BY note.id DESC
        """,
        (cutoff.strftime("%Y-%m-%dT%H:%M:%S"), *values),
    ).fetchall()
    found: dict[int, dict] = {}
    for row in rows:
        entry = found.setdefault(row["item"], {"id": row["item"], "repo": row["repo"], "title": row["title"],
                                               "at": row["timestamp"], "pr": None, "commit": None})
        if merge := _CODE_DELIVERY.match(row["body"]):
            pull = _PULL.search(merge.group(1))
            entry["pr"] = entry["pr"] or (int(pull.group(1)) if pull else None)
            entry["commit"] = entry["commit"] or merge.group(2)
        elif done := _DELIVERED_AT.search(row["body"]):
            entry["commit"] = entry["commit"] or done.group(1)
    return list(found.values())


def delivered(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None,
              now: str | None = None, hours: int = 24) -> list[dict]:
    """Items a merged PR or a verified delivery landed in the last `hours`, newest first:
    `pr`, `commit`, `at` (when it landed).

    The commit comes from `sd-ship`'s `delivered at` transition or its
    `Code delivery` note. An item closed by hand has neither and is
    `closed_by_hand`'s, so a bulk close is not a screen of rows.
    """
    return [row for row in _closed(connection, repos, now, hours) if row["commit"]]


def closed_by_hand(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None,
                   now: str | None = None, hours: int = 24) -> list[dict]:
    """Items closed in the last `hours` with no PR or delivered commit, newest first: `delivered`'s fields."""
    return [row for row in _closed(connection, repos, now, hours) if not row["commit"]]


def repo_keys(connection: sqlite3.Connection) -> dict[str, str]:
    """Each registered repository's key by its lower-case GitHub `owner/name`."""
    from .repos import parse_remote

    keys = {}
    for row in connection.execute("SELECT path, remote FROM repo WHERE remote IS NOT NULL"):
        found = parse_remote(row["remote"])
        if found.kind == "github":
            keys[f"{found.owner}/{found.name}".lower()] = row["path"]
    return keys


def _gate(gate: Any) -> dict | None:
    """`sd gate status --json`'s holders and waiters, or the reason they could not be read."""
    if gate is None:
        return None
    if isinstance(gate, str):
        return {"holders": [], "waiters": [], "error": gate}
    return {"holders": [{key: entry.get(key) for key in ("slot", "label", "since")} for entry in gate.get("holders", [])],
            "waiters": [{key: entry.get(key) for key in ("place", "label", "since")} for entry in gate.get("waiters", [])],
            "error": None}


def landing(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None, gate: Any = None) -> dict:
    """The lane: `entries`, each item's newest entry that neither merged nor was cancelled, running first,
    and `gate`, the machine's gate slots.

    An entry carries `entry` (its lane id), `repository` (GitHub `owner/name`),
    `status`, `step`, `place` and `since`. Newest is by the order the hub took
    the entries in, as `sd_lane.newest_entry` reads it; an entry of a done
    item shows only while it is still queued. `place` counts the
    repository's running and pending entries by position. `since` is when
    the entry started running, else when the hub queued it. The lane records
    no ETA, so none is given.

    `gate` is what the caller read from `sd gate status --json` (the slots
    are files on this machine, not rows), or the text of why that read
    failed; it returns as `holders`, `waiters` and `error`, or None.
    """
    rows = connection.execute(
        """
        SELECT state.key, state.body, first, queued.timestamp AS queued_at
          FROM state JOIN (SELECT MAX(id) AS last, MIN(id) AS first FROM state
                            WHERE kind = 'checkpoint' AND substr(key, 1, ?) = ? GROUP BY key)
            ON state.id = last
          JOIN state AS queued ON queued.id = first
        """,
        (len(LANE_KEY_PREFIX), LANE_KEY_PREFIX),
    ).fetchall()
    keys, keep = repo_keys(connection), _keep(repos)
    newest: dict[tuple[str, Any], tuple[int, dict]] = {}
    for row in rows:
        entry = json.loads(row["body"])
        repository = row["key"][len(LANE_KEY_PREFIX):].rpartition(":")[0]
        # An unregistered repository's entry has no key: it is no repository's, not one of no repository.
        if keep is not None and (keys.get(repository.lower()) is None or keys[repository.lower()] not in keep):
            continue
        key = (repository, entry.get("item"))
        if entry.get("status") != "cancelled" and row["first"] > newest.get(key, (0, {}))[0]:
            newest[key] = (row["first"], {**entry, "repository": repository, "queued_at": row["queued_at"]})
    live = [entry for _, entry in newest.values() if entry.get("status") != "merged"]
    items = {row["id"]: row for row in connection.execute(
        f"SELECT id, title, status FROM item WHERE id IN ({', '.join('?' for _ in live)})",
        [entry.get("item") for entry in live]).fetchall()}
    queued = LANE_ORDER[:2]
    live = [entry for entry in live
            if entry.get("status") in queued or items.get(entry.get("item"), {"status": ""})["status"] != "done"]
    live.sort(key=lambda entry: (entry.get("position") or 0, entry["id"]))
    places: dict[str, int] = {}
    found = []
    for entry in live:
        row, repository = items.get(entry.get("item")), entry["repository"]
        place = None
        if entry.get("status") in queued:
            place = places[repository] = places.get(repository, 0) + 1
        found.append({"id": entry.get("item"), "repo": keys.get(repository.lower()), "entry": entry["id"],
                      "repository": repository, "status": entry.get("status"), "step": entry.get("step"),
                      "place": place, "since": entry.get("started_at") or entry["queued_at"],
                      "title": row["title"] if row is not None else entry.get("title", "")})
    rank = {status: index for index, status in enumerate(LANE_ORDER)}
    found.sort(key=lambda entry: rank.get(str(entry["status"]), len(rank)))
    return {"entries": found, "gate": _gate(gate)}


#: The build assignment states the Queues panel lists (pack sd:6946).
BUILD_STATES = ("queued", "running", "blocked")


def queues(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None,
           now: str | None = None, lane: dict | None = None) -> list[dict]:
    """The three queues in one list, as Today's Queues panel and `sd today` show it (pack sd:6946):
    `queue` is `ready`, `build` or `lane`, with `status`, `detail` and `since`.

    Ready is each ready item in the backlog's order (priority first) that no
    build holds. Build is each queued, running or blocked build assignment:
    `detail` is its newest run's step, or the reason the run ended. Lane is
    `landing`'s entries (`lane`, when the caller read it already), with the
    step a stopped entry stopped at and `place` for a queued one.
    """
    keep = _keep(repos)
    built = builds(connection, repos=keep)
    building = {row["item"] for row in built}
    found = [{"queue": "ready", "id": row["id"], "repo": row["repo"], "title": row["title"], "status": "ready",
              "detail": f"P{row['priority']}" if row["priority"] is not None else "-", "since": row["status_since"]}
             for row in backlog_items(connection, status="ready", now=now)
             if (keep is None or row["repo"] in keep) and row["id"] not in building and not row["scheduled"]]
    found += [{"queue": "build", "id": row["item"], "repo": row["repo"], "title": row["title"], "status": row["status"],
               "detail": row["why"] if row["status"] == "blocked" and row["why"] else row["step"] or "-",
               "since": row["since"]} for row in built]
    entries = (lane if lane is not None else landing(connection, repos=keep))["entries"]
    found += [{"queue": "lane", "id": entry["id"], "repo": entry["repo"], "title": entry["title"],
               "status": entry["status"], "detail": entry.get("step") or "-", "since": entry["since"],
               "place": entry["place"]} for entry in entries]
    return found


def builds(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None) -> list[dict]:
    """Each queued, running or blocked build assignment, oldest first: the Queues panel's build rows, and the
    blocked build rows Queue lists (system sd:8651), from this one read.

    Each is `assignment`, `lead` (its bundle's lead, itself when it leads), `item`, `repo`, `title`, `status`,
    `since`, `step` (its own newest run's), and `why`: for a blocked one the first line of the reason its own newest
    run ended with, else of its own result, which a member with no run of its own, or one never built, carries.
    """
    where, values = repo_in("item.repo", _keep(repos))
    rows = connection.execute(
        f"""
        SELECT assignment.id AS assignment, COALESCE(assignment.parent, assignment.id) AS lead,
               assignment.item, item.repo, item.title, assignment.status, assignment.queued_at AS since,
               assignment.result, run.step, run.detail
          FROM assignment JOIN item ON item.id = assignment.item
          LEFT JOIN runner_run AS run ON run.rowid = (SELECT rowid FROM runner_run
                                                      WHERE runner_run.assignment = assignment.id
                                                      ORDER BY run DESC LIMIT 1)
         WHERE assignment.scope = 'build' AND assignment.status IN ({', '.join('?' for _ in BUILD_STATES)})
           AND {where}
         ORDER BY assignment.queued_at, assignment.id
        """, (*BUILD_STATES, *values)).fetchall()
    return [{"assignment": row["assignment"], "lead": row["lead"], "item": row["item"], "repo": row["repo"],
             "title": row["title"], "status": row["status"], "since": row["since"], "step": row["step"],
             "why": _first_line(row["detail"] or row["result"]) if row["status"] == "blocked" else ""}
            for row in rows]


#: The state key prefix of a failure already pushed, one `watermark` row per entry or run (pack sd:6946).
PUSHED = "pushed:"
#: The build run outcomes a push reports.
FAILED_OUTCOMES = ("blocked", "lane_failed", "timed_out", "error_max_turns")
#: The lane states a push reports.
FAILED_LANE = ("failed", "skipped")


def push_due(connection: sqlite3.Connection, *, now: str | None = None) -> list[dict]:
    """Each failure the operator has not had a push for (pack sd:6946): a lane entry that failed or was
    skipped, a build run that ended blocked or failed in the last day, and an item a failed health check
    after an auto-pickup merge filed in the last day. Each is `key`, `title` and `body`.

    Success pushes nothing, and each key is pushed once: the caller records `PUSHED + key` after a send.
    """
    found = [{"key": f"lane:{entry['repository']}:{entry['entry']}", "title": f"Lane {entry['status']}: sd:{entry['id']}",
              "body": f"{entry['title']} (step {entry.get('step') or '-'})"}
             for entry in landing(connection)["entries"] if entry["status"] in FAILED_LANE]
    since = ((_parse(_now(now)) or datetime.now(timezone.utc)) - timedelta(days=1)).isoformat(timespec="seconds")
    runs = connection.execute(
        f"""
        SELECT run.id, run.outcome, run.detail, assignment.item, item.title
          FROM runner_run AS run JOIN assignment ON assignment.id = run.assignment
          JOIN item ON item.id = assignment.item
         WHERE assignment.scope = 'build' AND run.updated_at >= ?
           AND run.outcome IN ({', '.join('?' for _ in FAILED_OUTCOMES)})
         ORDER BY run.updated_at, run.id
        """, (since, *FAILED_OUTCOMES)).fetchall()
    found += [{"key": f"build:{row['id']}", "title": f"Build {row['outcome']}: sd:{row['item']}",
               "body": f"{row['title']}: {_first_line(row['detail'])[:300]}"} for row in runs]
    # The critical push of a failed health check after an auto-pickup merge (pack sd:6097, system sd:5997).
    found += [{"key": f"health:{row['id']}", "title": f"Auto-pickup off: health failed, sd:{row['id']}",
               "body": row["title"]} for row in connection.execute(
        "SELECT id, title FROM item WHERE substr(title, 1, ?) = ? AND created_at >= ? ORDER BY id",
        (len(HEALTH), HEALTH, since))]
    pushed = {row[0] for row in connection.execute(
        "SELECT key FROM state WHERE kind = 'watermark' AND substr(key, 1, ?) = ?", (len(PUSHED), PUSHED))}
    return [row for row in found if PUSHED + row["key"] not in pushed]


def planned(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None,
            now: str | None = None) -> list[dict]:
    """Open unparked work items (`reads.WORK_KIND`) due after today, soonest first: `status`, `due`, `priority`.

    Today's and overdue ones are `work`. A scheduled occurrence of a recurring
    series (`reads.opens_on_clause`) is in neither until its lead brings it
    back (pack sd:5648).
    """
    where, values = repo_in("item.repo", _keep(repos))
    return [{"id": row["id"], "repo": row["repo"], "status": row["status"], "title": row["title"],
             "due": row["due"], "priority": row["priority"], "kind": row["kind"]} for row in connection.execute(
        f"""
        SELECT item.* FROM item
         WHERE item.status != 'done' AND item.due > ? AND {WORK_KIND} AND {_unparked_clause(connection)}
           AND NOT {scheduled_clause(connection, "?")} AND {where}
         ORDER BY item.due ASC, item.priority IS NULL, item.priority ASC, item.id ASC
        """,
        (_now(now)[:10], _now(now)[:10], *values),
    )]


def unscheduled(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None) -> list[dict]:
    """Open work with no due date, lane entry or active assignment, highest priority first:
    `status`, `priority`, `created_at`.

    Blocked items are `blocked`'s, and in-progress ones are `work`'s.
    """
    laned = {entry["id"] for entry in landing(connection, repos=repos)["entries"]}
    where, values = repo_in("item.repo", _keep(repos))
    rows = connection.execute(
        f"""
        SELECT item.* FROM item
         WHERE item.status IN ('planning', 'ready') AND item.due IS NULL
           AND item.kind NOT IN ({', '.join('?' for _ in _UNSCHEDULED_SKIPS)})
           AND {_unparked_clause(connection)} AND {where}
           AND NOT EXISTS (SELECT 1 FROM assignment WHERE assignment.item = item.id
                            AND assignment.status IN ({', '.join('?' for _ in OWNING)}))
         ORDER BY item.priority IS NULL, item.priority ASC, item.id ASC
        """,
        (*_UNSCHEDULED_SKIPS, *values, *OWNING),
    ).fetchall()
    return [{"id": row["id"], "repo": row["repo"], "status": row["status"], "title": row["title"],
             "priority": row["priority"], "created_at": row["created_at"], "kind": row["kind"]}
            for row in rows if row["id"] not in laned]


def suggested(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None, now: str | None = None,
              queued: list[dict] | None = None) -> list[dict]:
    """What sd-next offers next (pack sd:7314), in its order: `queues`' ready items, then `work`'s open items due
    today or before, oldest due first (pack sd:8238), then `planned` soonest first, then `unscheduled` highest
    priority first. Each row is `id`, `repo`, `title`, `status`, `why` (`ready`, `due`, `planned` or
    `unscheduled`), `due`, `priority` and `kind` (the Queue's Build reads it, pack sd:8532).

    An item counts once, under its first reason. One with a lane entry or a build assignment is left out: the
    Queue view shows it as a merging, next, blocked or working row. `queued` is `queues`' rows when the caller
    read them already. Live items and the per-repo cap are the caller's: they depend on what it shows.
    """
    queued = queues(connection, repos=repos, now=now) if queued is None else queued
    taken = {row["id"] for row in queued if row["queue"] in ("build", "lane")}
    ready = [row for row in queued if row["queue"] == "ready"]
    fields = {row["id"]: row for row in connection.execute(
        f"SELECT id, due, priority, kind FROM item WHERE id IN ({', '.join('?' for _ in ready)})",
        [row["id"] for row in ready])} if ready else {}
    # A ready idea or report is a vault reference note, not work (sd:8281).
    ready = [row for row in ready if fields[row["id"]]["kind"] not in _UNSCHEDULED_SKIPS]
    due = [row for row in work(connection, repos=repos, now=now)
           if row["due"] is not None and row["kind"] not in _UNSCHEDULED_SKIPS]
    found = []
    for why, rows in (("ready", [{**row, **dict(fields[row["id"]])} for row in ready]),
                      ("due", due),
                      ("planned", planned(connection, repos=repos, now=now)),
                      ("unscheduled", unscheduled(connection, repos=repos))):
        for row in rows:
            if row["id"] not in taken:
                taken.add(row["id"])
                found.append({"id": row["id"], "repo": row["repo"], "title": row["title"], "status": row["status"],
                              "why": why, "due": row.get("due"), "priority": row.get("priority"), "kind": row["kind"]})
    return found


def blocked(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None) -> list[dict]:
    """Open work items (`reads.WORK_KIND`) that are blocked or whose newest comment says they wait on someone:
    `status`, `why` (the reason on the newest move to blocked, else that comment), `since`."""
    where, values = repo_in("item.repo", _keep(repos))
    rows = connection.execute(
        f"""
        SELECT item.id, item.title, item.status, item.repo,
               (SELECT body FROM note WHERE note.item = item.id AND note.kind = 'status_change'
                  AND note.body LIKE '%-> blocked%' ORDER BY note.id DESC LIMIT 1) AS moved,
               (SELECT timestamp FROM note WHERE note.item = item.id AND note.kind = 'status_change'
                 ORDER BY note.id DESC LIMIT 1) AS moved_at,
               (SELECT body FROM note WHERE note.item = item.id AND note.kind = 'comment'
                 ORDER BY note.id DESC LIMIT 1) AS comment,
               item.updated_at
          FROM item
         WHERE item.status != 'done' AND {WORK_KIND} AND {_unparked_clause(connection)} AND {where}
         ORDER BY item.id ASC
        """,
        values,
    ).fetchall()
    found = []
    for row in rows:
        waiting = row["comment"] if row["comment"] and _WAITING.match(row["comment"]) else ""
        if row["status"] != "blocked" and not waiting:
            continue
        because = _BLOCKED_BECAUSE.search(row["moved"] or "") if row["status"] == "blocked" else None
        why = because.group(1) if because and because.group(1).strip() else waiting
        found.append({"id": row["id"], "repo": row["repo"], "status": row["status"], "title": row["title"],
                      "why": _first_line(why), "since": row["moved_at"] or row["updated_at"]})
    return found


def work(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None,
         now: str | None = None) -> list[dict]:
    """`today_items` as dicts, in its order: every item column, and `status_since`."""
    keep = _keep(repos)
    return [dict(row) for row in today_items(connection, now=now) if keep is None or row["repo"] in keep]


def sections(connection: sqlite3.Connection, *, repos: Iterable[str | None] | None = None,
             now: str | None = None, gate: Any = None) -> dict[str, Any]:
    """Every section in `ORDER`, read once, with `summary` counts first, then `closed_by_hand` and `grill`.

    `summary["review_questions"]` and `summary["grill"]` count what `decisions` leaves out.
    """
    keep = None if repos is None else list(dict.fromkeys(repos))
    found: dict[str, Any] = {
        "attention": attention(connection, repos=keep),
        "operations": operations(connection, gate=gate, repos=keep),
        "decisions": decisions(connection, repos=keep),
        "landing": landing(connection, repos=keep, gate=gate),
        "queues": [],
        "delivered": delivered(connection, repos=keep, now=now),
        "planned": planned(connection, repos=keep, now=now),
        "blocked": blocked(connection, repos=keep),
        "unscheduled": unscheduled(connection, repos=keep),
        "followups": followups(connection, repos=keep),
        "work": work(connection, repos=keep, now=now),
    }
    found["queues"] = queues(connection, repos=keep, now=now, lane=found["landing"])
    by_hand = closed_by_hand(connection, repos=keep, now=now)
    week = ((_parse(_now(now)) or datetime.now(timezone.utc)) + timedelta(days=7)).strftime("%Y-%m-%d")
    summary = {key: len(found[key]) for key in ORDER if key not in ("landing", "operations")}
    summary["landing"] = len(found["landing"]["entries"])
    summary["operations"] = len(found["operations"]["findings"])
    summary["planned_this_week"] = sum(1 for row in found["planned"] if row["due"] <= week)
    summary["closed_by_hand"] = len(by_hand)
    summary["review_questions"] = review_questions(connection, repos=keep)
    grilled = grill(connection, repos=keep)
    summary["grill"] = sum(row["questions"] for row in grilled)
    return {"summary": summary, **{key: found[key] for key in ORDER}, "closed_by_hand": by_hand, "grill": grilled}
