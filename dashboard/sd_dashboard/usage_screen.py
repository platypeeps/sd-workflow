"""`/api/usage`, the Usage dropdown's strip (pack sd:9257, sd:9569) and Health's Service status rows (pack sd:9260).

`document` is `/api/usage`: the month as `sd usage --json` prints it, as the same bytes. Requirement 6 says the four
numbers, the item screen and `sd usage` cannot disagree, so it serves `usage.json_text` of `usage.read`, the read `sd
usage` prints, and nothing it computes itself. The one difference is the sweep: the dashboard's GET connection is
read-only (`sd_db.connect` with `write=False`: `mode=ro`, `query_only`), so the API shows the read as it stands, a dead
owner's `reserved` or `sending` row still held until the next reservation or `sd usage` sweeps it, after which the two
are the same bytes. The Usage area that drew this month moved to Metrics' Spend view in pack sd:7524.

`strip` and `services` read what system's `agent-meter.py usage` and `services` write (system sd:9256, sd:9259): one
`heartbeat` state row per entry of the 'usage providers' and 'status services' lists, keys `usage:<provider>` and
`service-status:<name>`, the newest row per key being the answer. A row `{"status": "removed"}` means the entry left its
list, so it is not shown. A reading carries its own `read_at`; one older than `USAGE_STALE_MINUTES` (Usage) or
`STATUS_STALE_MINUTES` (Health) is unknown, never its last numbers and never 0. A row that does not parse, or names a
status the job does not write, is unknown too. A Settings line with no row yet reads unknown, `NO_READING`, so a
typo or a job that never ran shows (pack sd:9333). Nothing here writes.

`parse_providers` and `parse_services` are the two lists' grammars, as system's `usage_sources.parse_providers` and
`service_status.parse_services` read them, so Settings refuses a line the job would refuse.
"""

from __future__ import annotations

import math
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone

from sd_db import usage

__all__ = ["document", "parse_providers", "parse_services", "services", "strip"]

#: A provider or service name in a Settings list (system usage_sources.NAME).
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
#: The service kinds the status job reads (system service_status.KINDS).
KINDS = ("statuspage", "slack", "google-incidents", "http")
#: A Usage reading older than this reads unknown (pack sd:9257).
USAGE_STALE_MINUTES = 30
#: A service status older than this reads unknown (pack sd:9260).
STATUS_STALE_MINUTES = 15
#: A window this full is caution, and at its limit a warning.
CAUTION_PERCENT = 80
WARNING_PERCENT = 100
#: The state grammar of a service status, and its rank: the loudest of a provider's services flags its Usage row.
SERVICE_STATE = {"ok": "ok", "degraded": "caution", "outage": "warning", "unknown": "unknown"}
RANK = {"warning": 0, "caution": 1, "unknown": 2, "ok": 3}
#: What the Usage strip and Health link to for add and remove (Settings' list rows, `f-<key>`).
PROVIDERS_LIST = "/settings#f-dashboard.usage_providers"
SERVICES_LIST = "/settings#f-dashboard.status_services"
USAGE_PREFIX, STATUS_PREFIX = "usage:", "service-status:"
#: Why a listed entry with no row reads unknown (pack sd:9333): a typo or a job that never ran shows, not vanishes.
NO_READING = "no reading yet: the job has written no row for it"


def document(connection: sqlite3.Connection, *, now: str, month: str | None) -> str:
    """`/api/usage`: the read as `sd usage --json` prints it, without the verb's sweep (the module docstring)."""
    return usage.json_text(usage.read(connection, month=month, now=now))


def parse_providers(text: str | None) -> tuple[list[str], list[str]]:
    """The list's names in order, and one problem per line that is not one; `#` starts a comment."""
    names, problems = [], []
    for number, line in enumerate((text or "").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not NAME.fullmatch(line):
            problems.append(f"line {number}: not a provider name")
        elif line not in names:
            names.append(line)
    return names, problems


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
        return moment.astimezone(timezone.utc) if moment.tzinfo else None
    except (ValueError, OverflowError):  # a time past the calendar's end, once in UTC
        return None


def _word(value, known) -> str:
    """`value` when it is one of the `known` strings, else unknown: a list or object would not hash."""
    return value if isinstance(value, str) and value in known else "unknown"


def _text(value) -> str:
    """A reading's text field as shown: a string, at most 200 characters."""
    return str(value or "")[:200]


def _minutes(value) -> int | None:
    """A window's length in minutes, or None: a bool, a non-positive value or one past 10 years is none, so no float
    conversion overflows and no label runs to hundreds of digits."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 10 * 525_600 else None


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


# The heartbeat reads live in the library: screens issue no SQL (criterion 12).
ever_read = usage.ever_read
_newest = usage.newest_heartbeats


def _listed(connection: sqlite3.Connection, key: str) -> str:
    """A Settings list's text as Settings reads it, or "" when it holds none or was not read: then nothing is added."""
    from . import places, setting_rows

    found, _, problem = setting_rows.text(connection, key, places.REALMS_CONFIG.parent / setting_rows.LISTS[key][0])
    return "" if problem else found or ""


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
        status = _word(body.get("status"), SERVICE_STATE)
        # The order `strip` checks: a failed read says its error before a missing or old read time (pack sd:9380).
        if not body:
            why = "the row does not parse"
        elif status == "unknown":
            why = _text(body.get("error") or "the status job could not read it")
        elif not read_at:
            why = "the reading has no read time"
        else:
            why = _stale(_age(_when(read_at), clock), STATUS_STALE_MINUTES)
        # An unknown reading shows none of its incidents or components: a stale row's last outage may have ended
        # (pack sd:9498).
        incidents = [one for one in body.get("incidents") or [] if isinstance(one, dict)] if isinstance(
            body.get("incidents"), list) and not why else []
        components = [one for one in body.get("components") or [] if isinstance(one, dict)] if isinstance(
            body.get("components"), list) and not why else []
        providers = [p for p in body.get("providers") or [] if isinstance(p, str) and NAME.fullmatch(p)] if isinstance(
            body.get("providers"), list) else []
        out.append({"name": name, "status": "unknown" if why else status, "why": why, "read_at": read_at,
                    "row_at": stamp, "kind": _text(body.get("kind")) if isinstance(body.get("kind"), str) else "",
                    "url": body.get("url") if isinstance(body.get("url"), str) and _web_url(body["url"]) else "",
                    "providers": providers,
                    "indicator": _text(body.get("indicator")) if isinstance(body.get("indicator"), str) else "",
                    "incidents": [{"title": _text(one.get("title")),
                                   "url": one["url"] if isinstance(one.get("url"), str) and _web_url(one["url"]) else "",
                                   "impact": _text(one.get("impact")),
                                   "started_at": one.get("started_at") if _when(one.get("started_at")) else None}
                                  for one in incidents],
                    "components": [{"name": _text(one.get("name")), "status": _text(one.get("status"))}
                                   for one in components]})
    read = {one["name"] for one in out}
    for name, kind, url, providers in parse_services(_listed(connection, "dashboard.status_services"))[0]:
        if name not in read:
            out.append({"name": name, "status": "unknown", "why": NO_READING, "read_at": None, "row_at": None,
                        "kind": kind, "url": url, "providers": providers, "indicator": "", "incidents": [],
                        "components": []})
    return out


def lasted(incidents: list[dict], now: str) -> str:
    """How long the oldest open incident has lasted, in the shell's grammar, or "" when none says when it began."""
    begun = [moment for moment in (_when(one.get("started_at")) for one in incidents) if moment]
    return _span(_age(min(begun), _when(now))) if begun else ""


def _flag(found: list[dict]) -> dict | None:
    """A provider's service flag: the loudest of the services that name it, with its first incident."""
    if not found:
        return None
    worst = min(found, key=lambda one: RANK[SERVICE_STATE[one["status"]]])
    incident = worst["incidents"][0] if worst["incidents"] else None
    return {"name": worst["name"], "status": worst["status"], "state": SERVICE_STATE[worst["status"]],
            "why": worst["why"], "title": incident["title"] if incident else "",
            "url": (incident["url"] if incident and incident["url"] else worst["url"])}


def _label(window: dict) -> str:
    minutes = _minutes(window.get("window_minutes"))
    if minutes:
        return f"{minutes // 1440}d" if minutes % 1440 == 0 else f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"
    return _text(window.get("name") or "window")


def _pace(used: float, minutes, resets_at, clock: datetime, expected=None) -> float | None:
    """Percentage points used over (+) or under (-) an even pace through the window; None when the window does not say
    where in it the read falls."""
    if _number(expected) is not None:
        over = used - float(expected)
        return round(over) if math.isfinite(over) else None
    reset, minutes = _when(resets_at), _minutes(minutes)
    if reset is None or minutes is None:
        return None
    left = (reset - clock).total_seconds() / 60
    if not 0 <= left <= minutes:
        return None
    return round(used - (minutes - left) / minutes * 100)


def _window(window: dict, clock: datetime, expected=None) -> dict | None:
    used = _number(window.get("used_percent"))
    if used is None:
        return None
    resets = window.get("resets_at") if _when(window.get("resets_at")) else None
    return {"label": _label(window), "used": round(used, 1), "resets_at": resets,
            "pace": _pace(used, window.get("window_minutes"), resets, clock, expected)}


def _money(value) -> dict | None:
    if not isinstance(value, dict) or _number(value.get("amount")) is None:
        return None
    return {"amount": _number(value["amount"]), "unit": _text(value.get("unit")), "period": _text(value.get("period"))}


def _usage_row(ident: str, label: str, *, status: str, why: str, read_at, windows: list[dict], balance, spend,
               flag, active=None) -> dict:
    if status != "ok" or why:
        state, windows, balance, spend = "unknown", [], None, None
    else:
        top = max((window["used"] for window in windows), default=0.0)
        state = "warning" if top >= WARNING_PERCENT else "caution" if top >= CAUTION_PERCENT else "ok"
    return {"id": ident, "label": label, "state": state, "status": status, "why": why, "read_at": read_at,
            "windows": windows, "balance": balance, "spend": spend, "service": flag, "active": active}


def strip(connection: sqlite3.Connection, *, now: str) -> dict:
    """The Usage dropdown's strip (pack sd:9569): one row per provider, and per cswap account for claude (`claude#N`).

    A row shows the windows its reading has (claude has its seven days only: cswap has no five hours), each with its
    use, reset and pace, or its balance or spend; its read time; and the flag of the services that name its provider
    (`service-status:<name>` rows' `providers`). A reading not `ok`, or older than `USAGE_STALE_MINUTES`, is unknown and
    carries no number. A `no-source` provider is listed in `nosource`, not as a row. A store that does not answer is
    `error`, so the strip says Usage was not read, never an empty strip.
    """
    clock = _when(now)
    try:
        readings = _newest(connection, USAGE_PREFIX)
        try:
            flags, flags_error = services(connection, now=now), ""
        except sqlite3.Error as failure:
            flags, flags_error = [], str(failure) or "the service status rows could not be read"
    except sqlite3.Error as failure:
        return {"error": str(failure) or "the usage rows could not be read", "rows": [], "nosource": [],
                "providers": PROVIDERS_LIST, "services": SERVICES_LIST}
    by_provider: dict[str, list[dict]] = {}
    for found in flags:
        for provider in found["providers"]:
            by_provider.setdefault(provider, []).append(found)
    rows, nosource = [], []
    for name, (_, body) in readings.items():
        body = body if isinstance(body, dict) else {}
        status = _word(body.get("status"), ("ok", "unknown", "no-source"))
        if status == "no-source":
            nosource.append(name)
            continue
        flag = _flag(by_provider.get(name, []))
        accounts = body.get("accounts")
        # A failed read keeps its status and error: it falls through to one provider row below, as does a list that
        # renders no account.
        if name == "claude" and status == "ok" and isinstance(accounts, list) and accounts:
            before = len(rows)
            for account in accounts:
                number = account.get("number") if isinstance(account, dict) else None
                if not isinstance(number, int) or isinstance(number, bool):
                    continue
                read_at = account.get("fetchedAt") if _when(account.get("fetchedAt")) else None
                # Pace is where the window stood at the read, not at render (pack sd:9380).
                week = _window({"used_percent": account.get("pct"), "window_minutes": 10080,
                                "resets_at": account.get("resetsAt")}, _when(read_at) or clock,
                               account.get("expectedPct"))
                why = (_stale(_age(_when(read_at), clock), USAGE_STALE_MINUTES) if read_at else "cswap has no fetch time")
                if not why and week is None:
                    why = "cswap has no seven-day share"
                rows.append(_usage_row(f"usage:claude#{number}", f"claude#{number}", status="ok", why=why, read_at=read_at,
                                       windows=[week] if week else [], balance=None, spend=None, flag=flag,
                                       active=bool(account.get("active"))))
            if len(rows) > before:
                continue
        read_at = body.get("read_at") if _when(body.get("read_at")) else None
        windows = [one for one in (_window(window, _when(read_at) or clock) for window in body.get("windows") or []
                                   if isinstance(window, dict)) if one] if isinstance(body.get("windows"), list) else []
        balance, spend = _money(body.get("balance")), _money(body.get("spend"))
        why = ""
        if not body:
            why = "the row does not parse"
        elif status != "ok":
            why = _text(body.get("error") or "the usage job could not read it")
        elif name == "claude" and isinstance(accounts, list) and accounts:
            why = "cswap lists no numbered account"
        elif not read_at:
            why = "the reading has no read time"
        else:
            why = _stale(_age(_when(read_at), clock), USAGE_STALE_MINUTES)
            if not why and not (windows or balance or spend):
                why = "the reading carries no window, balance or spend"
        rows.append(_usage_row(f"usage:{name}", name, status=status, why=why, read_at=read_at, windows=windows,
                               balance=balance, spend=spend, flag=flag))
    for name in parse_providers(_listed(connection, "dashboard.usage_providers"))[0]:
        if name not in readings:
            rows.append(_usage_row(f"usage:{name}", name, status="unknown", why=NO_READING, read_at=None, windows=[],
                                   balance=None, spend=None, flag=_flag(by_provider.get(name, []))))
    return {"error": "", "rows": rows, "nosource": nosource, "flags_error": flags_error,
            "providers": PROVIDERS_LIST, "services": SERVICES_LIST}
