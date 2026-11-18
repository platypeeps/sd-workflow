"""`/api/usage`, and Health's Service status rows (pack sd:9260).

`document` is `/api/usage`: the month as `sd usage --json` prints it, as the same bytes. Requirement 6 says the four
numbers, the item screen and `sd usage` cannot disagree, so it serves `usage.json_text` of `usage.read`, the read `sd
usage` prints, and nothing it computes itself. The one difference is the sweep: the dashboard's GET connection is
read-only (`sd_db.connect` with `write=False`: `mode=ro`, `query_only`), so the API shows the read as it stands, a dead
owner's `reserved` or `sending` row still held until the next reservation or `sd usage` sweeps it, after which the two
are the same bytes. The Usage area that drew this month moved to Metrics' Spend view in pack sd:7524.

`services` reads what system's `agent-meter.py services` writes (system sd:9259): one `heartbeat` state row per line of
the 'status services' list, key `service-status:<name>`, the newest row per key being the answer. A row `{"status":
"removed"}` means the line left its list, so it is not shown. A reading carries its own `read_at`; one older than
`STATUS_STALE_MINUTES` is unknown, never its last answer. A row that does not parse, or names a status the job does not
write, is unknown too. Nothing here writes.

`parse_services` is the list's grammar, as system's `service_status.parse_services` reads it, so Settings refuses a line
the job would refuse.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone

from sd_db import usage

__all__ = ["document", "parse_services", "services"]

#: A provider or service name in a Settings list (system usage_sources.NAME).
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
#: The service kinds the status job reads (system service_status.KINDS).
KINDS = ("statuspage", "slack", "google-incidents", "http")
#: A service status older than this reads unknown (pack sd:9260).
STATUS_STALE_MINUTES = 15
#: The state grammar of a service status.
SERVICE_STATE = {"ok": "ok", "degraded": "caution", "outage": "warning", "unknown": "unknown"}
#: What Health links to for add and remove (Settings' list row, `f-<key>`).
SERVICES_LIST = "/settings#f-dashboard.status_services"
STATUS_PREFIX = "service-status:"


def document(connection: sqlite3.Connection, *, now: str, month: str | None) -> str:
    """`/api/usage`: the read as `sd usage --json` prints it, without the verb's sweep (the module docstring)."""
    return usage.json_text(usage.read(connection, month=month, now=now))


def _web_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port  # a bad port raises here, as the job's read would
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def parse_services(text: str | None) -> tuple[list[tuple[str, str, str, list[str]]], list[str]]:
    """`[(name, kind, url, providers)]` in order, and one problem per refused line."""
    found, problems, seen = [], [], set()
    for number, line in enumerate((text or "").splitlines(), 1):
        fields = line.split("#", 1)[0].split()
        if not fields:
            continue
        if len(fields) not in (3, 4):
            problems.append(f"line {number}: want <name> <kind> <url> [provider]")
            continue
        name, kind, url = fields[:3]
        providers = fields[3].split(",") if len(fields) == 4 else []
        if not NAME.fullmatch(name) or name in seen:
            problems.append(f"line {number}: name is not a new lower-case name")
        elif kind not in KINDS:
            problems.append(f"line {number}: kind is not one of {', '.join(KINDS)}")
        elif not _web_url(url):
            problems.append(f"line {number}: url is not http(s)")
        elif not all(NAME.fullmatch(provider) for provider in providers):
            problems.append(f"line {number}: a provider is not a lower-case name")
        else:
            seen.add(name)
            found.append((name, kind, url, providers))
    return found, problems


def _when(stamp) -> datetime | None:
    """An aware time from an ISO-8601 string, or None."""
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo else None


def _newest(connection: sqlite3.Connection, prefix: str) -> dict[str, tuple[str, object]]:
    """The newest heartbeat row of each key under `prefix`, by name: `(timestamp, body)`. A removed entry is left out;
    a body that does not parse is None, which reads unknown."""
    end = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    found = {}
    for key, stamp, body in connection.execute(
            "SELECT key, timestamp, body FROM state WHERE id IN (SELECT MAX(id) FROM state"
            " WHERE kind = 'heartbeat' AND key >= ? AND key < ? GROUP BY key) ORDER BY key", (prefix, end)):
        try:
            body = json.loads(body)
        except (TypeError, ValueError, RecursionError):
            body = None
        if isinstance(body, dict) and body.get("status") == "removed":
            continue
        found[key[len(prefix):]] = (stamp, body)
    return found


def _age(read_at: datetime | None, now: datetime) -> float | None:
    """Minutes since `read_at`; a time in the future reads as now."""
    return None if read_at is None else max(0.0, (now - read_at).total_seconds() / 60)


def _span(minutes: float) -> str:
    """The shell's relative grammar: `Nm`, `Nh` under 48 h, then `Nd`."""
    whole = round(minutes)
    return f"{whole}m" if whole < 60 else f"{round(whole / 60)}h" if whole < 48 * 60 else f"{round(whole / 1440)}d"


def _stale(age: float | None, limit: int) -> str:
    return f"read {_span(age)} ago; a reading over {limit} min old is unknown" if age is not None and age > limit else ""


def services(connection: sqlite3.Connection, *, now: str) -> list[dict]:
    """Each watched service's newest status, by name: `status` is ok, degraded, outage or unknown, with `why` when it is
    unknown. Raises `sqlite3.Error` when the store does not answer: the caller's area or part says so."""
    clock = _when(now)
    out = []
    for name, (stamp, body) in _newest(connection, STATUS_PREFIX).items():
        body = body if isinstance(body, dict) else {}
        read_at = body.get("read_at") if _when(body.get("read_at")) else None
        status = body.get("status") if body.get("status") in SERVICE_STATE else "unknown"
        why = (_stale(_age(_when(read_at), clock), STATUS_STALE_MINUTES) if read_at else "the reading has no read time")
        if not body:
            why = "the row does not parse"
        elif status == "unknown" and not why:
            why = str(body.get("error") or "the status job could not read it")[:200]
        incidents = [one for one in body.get("incidents") or [] if isinstance(one, dict)] if isinstance(
            body.get("incidents"), list) else []
        components = [one for one in body.get("components") or [] if isinstance(one, dict)] if isinstance(
            body.get("components"), list) else []
        providers = [p for p in body.get("providers") or [] if isinstance(p, str)] if isinstance(
            body.get("providers"), list) else []
        out.append({"name": name, "status": "unknown" if why else status, "why": why, "read_at": read_at,
                    "row_at": stamp, "kind": body.get("kind") if isinstance(body.get("kind"), str) else "",
                    "url": body.get("url") if isinstance(body.get("url"), str) and _web_url(body["url"]) else "",
                    "providers": providers, "indicator": body.get("indicator") if isinstance(body.get("indicator"), str) else "",
                    "incidents": [{"title": str(one.get("title") or "")[:200],
                                   "url": one["url"] if isinstance(one.get("url"), str) and _web_url(one["url"]) else "",
                                   "impact": str(one.get("impact") or ""),
                                   "started_at": one.get("started_at") if _when(one.get("started_at")) else None}
                                  for one in incidents],
                    "components": [{"name": str(one.get("name") or ""), "status": str(one.get("status") or "")}
                                   for one in components]})
    return out


def lasted(incidents: list[dict], now: str) -> str:
    """How long the oldest open incident has lasted, in the shell's grammar, or "" when none says when it began."""
    begun = [moment for moment in (_when(one.get("started_at")) for one in incidents) if moment]
    return _span(_age(min(begun), _when(now))) if begun else ""


