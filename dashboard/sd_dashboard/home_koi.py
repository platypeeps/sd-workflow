"""Home's Koi view (pack sd:8054): the fish, the pond's care and its water changes, at `/api/home/koi`.

**Pond and fish.** Both are `sd_db.assets` rows, the property map's table (pack sd:8053): the pond is the `pond` asset
(placed on the Map; `fields.volume_gal` sizes a change in gallons), each fish a `koi` asset with no place: variety,
acquired, health notes and a `log` of `{date, size_in, weight_lb}`. Water tests (ammonia, nitrite, nitrate, pH, KH,
temperature) are kept on the pond as `fields.tests`.

**Care.** Water tests, feeding, filter cleaning and treatments are recurring sd items naming the pond in
`fields.asset`, filed through the map's `POST /api/home/care`, so they recur and an overdue one shows.

**Water changes.** Home Assistant runs a change as a script; the setting row `dashboard.koi_water` names each, one
`<entity_id>|<percent>` line, such as `script.pond_water_25_change|25`; entity ids describe a house, so the pack
holds none. Each time the
script's state turns `on` in Home Assistant's history is one change of that percent. History keeps ten days, so
`POST /api/home/koi/water` writes the changes it finds into the pond's `fields.water_log`, once each; the page posts
it when Home Assistant holds a change the log does not. Days since the last change and the percent replaced in the
last 7 days and on average over the last 28 come from the log and the history together.

**Gaps.** A read proves the last `KEEP_DAYS`, the recorder's keep, whatever rows it holds: a script that did not run
has none. Each sync stores `fields.water_synced` `{since, to}`: `since` is the first sync's proven start, `to` the
latest sync. When a sync's proven start falls after the last `to`, the days between were purged unread; the gap goes to
`fields.water_gaps`. A total reads "incomplete since" the first unproven day its window reaches: before `since`, in a
stored gap, or in the days since the last sync the recorder already purged. The view syncs when the log lacks a change
or the last sync is older than `SYNC_HOURS`. A daily job runs the same sync with nobody on the page (sd:8077):
`dashboard.sh koi-sync`, `main` here.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

KEY = "dashboard.koi_water"
#: How far back one history read looks; Home Assistant's recorder keeps ten days by default.
HISTORY_DAYS = 30
#: The days a read proves: Home Assistant's default `purge_keep_days`.
# ponytail: a constant; read the recorder's setting if a machine keeps fewer.
KEEP_DAYS = 10
TIMEOUT = 10.0
#: A stored window older than this makes the view sync on open, so a quiet pond still records its coverage.
SYNC_HOURS = 24
#: Gaps older than the 28-day window are dropped at the next sync.
GAP_DAYS = 28
TESTS = ("ammonia", "nitrite", "nitrate", "ph", "kh", "temp")
#: The care tasks the Koi view offers to file on the pond (operator 2026-11-07); each recurs.
CARE = (("Water test: ammonia, nitrite, nitrate, pH, KH, temp", "FREQ=WEEKLY"), ("Feed the koi", "FREQ=DAILY"),
        ("Clean the filter", "FREQ=WEEKLY;INTERVAL=2"), ("Pond treatment", "FREQ=MONTHLY"))


def parse(text: str) -> tuple[list[tuple[str, float]], list[str]]:
    """`[(entity_id, percent)]` and the problems, one `<entity_id>|<percent>` line each; `#` starts a comment."""
    from .home_screen import ENTITY

    found, problems = [], []
    for number, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entity, _, percent = line.partition("|")
        try:
            value = float(percent)
        except ValueError:
            value = -1.0
        if not ENTITY.fullmatch(entity.strip()) or not 0 < value <= 100:
            problems.append(f"line {number}: expected <entity_id>|<percent 1-100>")
            continue
        found.append((entity.strip(), value))
    return found, problems


def _history(entities: list[str], *, now: str, environ=None, timeout: float = TIMEOUT) -> list:
    """Home Assistant's history of `entities` over `HISTORY_DAYS`: one list of state rows per entity."""
    from . import home_screen

    base, token = home_screen._settings(os.environ if environ is None else environ)
    start = (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(days=HISTORY_DAYS)).isoformat()
    query = urllib.parse.urlencode({"filter_entity_id": ",".join(entities), "minimal_response": "", "no_attributes": ""})
    request = urllib.request.Request(f"{base}/api/history/period/{urllib.parse.quote(start)}?{query}",
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with home_screen._OPENER.open(request, timeout=timeout) as response:
            body = response.read(home_screen.LIMIT + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise home_screen._Failed(f"HA error {error.code}", f"Home Assistant answered HTTP {error.code}") from None
    except (OSError, ValueError) as error:
        raise home_screen._Failed("HA did not answer", f"Home Assistant did not answer: {error.__class__.__name__}") from None
    try:
        rows = json.loads(body)
    except ValueError:
        rows = None
    if not isinstance(rows, list) or not all(isinstance(series, list) for series in rows):
        raise home_screen._Failed("HA answer not read", "the history answer is not a list of state lists")
    return rows


def changes(history: list, entities: list[tuple[str, float]]) -> list[dict]:
    """Each time a script turned `on`: `{at, entity, percent}`, oldest first. Minimal rows name the entity once."""
    percent = dict(entities)
    out = []
    for series in history:
        entity, before = None, None
        for row in series:
            if not isinstance(row, dict):
                continue
            entity = row.get("entity_id") or entity
            state, at = row.get("state"), row.get("last_changed")
            if entity in percent and state == "on" and before != "on" and isinstance(at, str):
                out.append({"at": at, "entity": entity, "percent": percent[entity]})
            before = state
    return sorted(out, key=lambda change: change["at"])


def _day(at: str) -> date:
    return datetime.fromisoformat(at.replace("Z", "+00:00")).date()


def _when(at: str) -> datetime:
    return datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(timezone.utc)


def _uncovered(synced, *, now: str) -> dict | None:
    """The purged days after the last sync `to` that a read at `now` cannot prove, or None."""
    proven = _when(now) - timedelta(days=KEEP_DAYS)
    try:
        last = _when(synced["to"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return {"from": last.isoformat(), "to": proven.isoformat()} if proven > last else None


def _gap_age(gap, today: date) -> int | None:
    """Days from a stored gap's end to `today`; None for a row that is not a readable gap."""
    try:
        return (today - _day(gap["to"])).days if isinstance(gap["from"], str) else None
    except (KeyError, TypeError, ValueError):
        return None


def incomplete_since(gaps: list, since, *, today: date) -> dict:
    """The first unproven day each total's window reaches, or None: `{week, month}`. No `since` proves nothing."""
    try:
        tracked = _day(since)
    except (TypeError, ValueError, AttributeError):
        tracked = None

    def first(days: int):
        start = today - timedelta(days=days - 1)
        hit = [gap["from"][:10] for gap in gaps if (age := _gap_age(gap, today)) is not None and age < days]
        if tracked is None or tracked > start:
            hit.append(start.isoformat())
        return min(hit, default=None)

    return {"week": first(7), "month": first(28)}


def summary(log: list[dict], *, today: date, volume) -> dict:
    """Days since the last change, the percent replaced in the last 7 days and the weekly average over 28."""
    days = [(_day(row["at"]), float(row["percent"])) for row in log]
    last = max((day for day, _ in days), default=None)
    week = sum(p for day, p in days if (today - day).days < 7)
    month = sum(p for day, p in days if (today - day).days < 28)
    gallons = float(volume) if isinstance(volume, (int, float)) and not isinstance(volume, bool) and volume > 0 else None
    return {"days_since": (today - last).days if last else None, "week_percent": week, "average_week_percent": month / 4,
            "week_gallons": gallons * week / 100 if gallons else None}


def _entities(connection) -> tuple[list[tuple[str, float]], str]:
    from sd_db import writes

    try:
        held = writes.read_settings(connection, [KEY]).get(KEY)
    except Exception as error:  # an unread row is unknown, named
        return [], f"{KEY} not read: {error.__class__.__name__}"
    found, problems = parse(held or "")
    if problems:
        return [], f"{KEY} on Settings: {'; '.join(problems)}"
    return found, "" if found else f"No water-change scripts: set {KEY} on Settings, one <entity_id>|<percent> line each."


def _pond(connection) -> dict | None:
    from sd_db import assets

    ponds = assets.list_assets(connection, ("pond",))
    return ponds[0] if ponds else None


def document(connection, *, now: str, environ=None, timeout: float = TIMEOUT) -> dict:
    """The Koi view's document: the pond with its care, tests and water log, the fish, and Home Assistant's changes."""
    from sd_db import assets

    from . import home_screen

    pond, care = _pond(connection), assets.care(connection, today=now[:10])
    fish = [{**row, "care": care.get(row["id"], {"items": [], "overdue": False})}
            for row in assets.list_assets(connection, ("koi",))]
    entities, reason = _entities(connection)
    live: list[dict] = []
    if entities:
        try:
            live = changes(_history([e for e, _ in entities], now=now, environ=environ, timeout=timeout), entities)
        except home_screen._Failed as failed:
            reason = f"Water changes not read from Home Assistant: {failed.reason}"
    logged = list((pond or {}).get("fields", {}).get("water_log") or [])
    seen = {row.get("at") for row in logged}
    unlogged = [row for row in live if row["at"] not in seen]
    log = sorted(logged + unlogged, key=lambda row: row["at"], reverse=True)
    held = (pond or {}).get("fields", {})
    volume = held.get("volume_gal")
    synced = held.get("water_synced") if isinstance(held.get("water_synced"), dict) else {}
    try:
        due = not synced or _when(now) - _when(synced["to"]) > timedelta(hours=SYNC_HOURS)
    except (KeyError, TypeError, ValueError):
        due = True
    today = date.fromisoformat(now[:10])
    return {"state": "read" if not reason else "partial", "reason": reason,
            "pond": {**pond, "care": care.get(pond["id"], {"items": [], "overdue": False})} if pond else None,
            "pond_reason": "" if pond else "No pond yet: add a pond on the Map, then its fish here.",
            "fish": fish, "water": {"log": log, "unlogged": len(unlogged), "sync_due": bool(pond) and due,
                                    "incomplete_since": incomplete_since(
                                        list(held.get("water_gaps") or []) + [g for g in [_uncovered(synced, now=now)] if g],
                                        synced.get("since"), today=today),
                                    **summary(log, today=today, volume=volume)},
            "tests": list(TESTS), "care": [{"title": t, "recurrence": r} for t, r in CARE]}


def log_water(payload: dict, *, now: str, environ=None, timeout: float = TIMEOUT):
    """`POST /api/home/koi/water {}`: write Home Assistant's changes the pond's log lacks; `{added}`."""
    from . import home_screen

    if payload:
        raise ValueError("Send {}: the changes come from Home Assistant.")

    def write(connection):
        from sd_db.database import transaction

        if _pond(connection) is None:
            return 409, {"error": "No pond yet: add a pond on the Map first."}
        entities, reason = _entities(connection)
        if not entities:
            return 409, {"error": reason}
        try:
            history = _history([e for e, _ in entities], now=now, environ=environ, timeout=timeout)
        except home_screen._Failed as failed:
            return 503, {"error": failed.reason}
        live = changes(history, entities)
        # sd:8076: the pond is read again after Home Assistant's wait, and read and saved in one transaction, so a test
        # or an edit written while the history was read is kept.
        with transaction(connection):
            return _merge(connection, live, now=now)

    return write


def _merge(connection, live: list[dict], *, now: str):
    """Write `live`'s changes the pond's log lacks and the sync window, on the pond as it is now; `{added}`."""
    from sd_db import assets

    pond = _pond(connection)
    if pond is None:
        return 409, {"error": "No pond yet: add a pond on the Map first."}
    fields = dict(pond["fields"])
    log = list(fields.get("water_log") or [])
    seen = {row.get("at") for row in log}
    added = [row for row in live if row["at"] not in seen]
    if added:
        fields["water_log"] = log + added
    last = fields.get("water_synced") if isinstance(fields.get("water_synced"), dict) else {}
    gaps = [gap for gap in fields.get("water_gaps") or []
            if (age := _gap_age(gap, _when(now).date())) is not None and age < GAP_DAYS]
    if gap := _uncovered(last, now=now):
        gaps.append(gap)
    since = last.get("since") if isinstance(last.get("since"), str) else None
    proven = (_when(now) - timedelta(days=KEEP_DAYS)).isoformat()
    fields["water_synced"] = {"since": since or proven, "to": _when(now).isoformat()}
    if gaps:
        fields["water_gaps"] = gaps
    else:
        fields.pop("water_gaps", None)
    if fields != pond["fields"]:
        assets.save_asset(connection, asset=pond["id"], kind="pond", name=pond["name"], geometry=pond["geometry"],
                          photo=pond["photo"], fields=fields, kinds=assets.ALL_KINDS)
    return {"added": len(added)}


def log_test(payload: dict, *, now: str):
    """`POST /api/home/koi/test {<test>: number, ...}`: append one reading to the pond's current `fields.tests`."""
    import math

    from sd_db import assets

    if not payload or not set(payload) <= set(TESTS) or not all(
            type(value) in (int, float) and math.isfinite(value) for value in payload.values()):
        raise ValueError(f"Send one or more of {', '.join(TESTS)}, each a number.")
    # sd:8076: the day the test was taken where the pond is, the machine's zone, not UTC's.
    day = _when(now).astimezone().date().isoformat()
    reading = {"date": day, **{test: payload[test] for test in TESTS if test in payload}}

    def write(connection):
        from sd_db.database import transaction

        with transaction(connection):
            pond = _pond(connection)
            if pond is None:
                return 409, {"error": "No pond yet: add a pond on the Map first."}
            tests = list(pond["fields"].get("tests") or []) + [reading]
            assets.save_asset(connection, asset=pond["id"], kind="pond", name=pond["name"], geometry=pond["geometry"],
                              photo=pond["photo"], fields={**pond["fields"], "tests": tests}, kinds=assets.ALL_KINDS)
            return {"tests": len(tests)}

    return write


def main(argv=None) -> int:
    """`dashboard.sh koi-sync` (sd:8077): one `log_water` on the default store, for a daily job, so Home Assistant's
    changes are kept even when nobody opens the Koi view within the recorder's `KEEP_DAYS`. 0 with `{added}` on
    stdout; 1 with the refusal on stderr (no pond, no scripts, Home Assistant down); 2 for an argument."""
    import sys

    import sd_db
    from sd_db.writes import now

    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print("usage: dashboard.sh koi-sync (no arguments)", file=sys.stderr)
        return 2
    connection = sd_db.connect()
    try:
        result = log_water({}, now=now())(connection)
    finally:
        connection.close()
    if isinstance(result, tuple):
        print(f"koi-sync: {result[1]['error']}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
