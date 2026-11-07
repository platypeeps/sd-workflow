"""HOA: the water system behind the default UI's HOA page (sd:2116).

The page is `v2/hoa.html`; it holds no data. It reads one JSON document,
`/api/hoa`, built here by `document`.

**Where the data is.** The design reads one HOA checkout: the asset map
(`water/map/assets.geojson`, `water/map/base.geojson`), the Mission telemetry
export (`water/metrics/mission/*.csv`), the lot data the checkout's
`tools/fetch-lot-parcels.py` writes to its gitignored `downloads/parcels/lots.json`
(each lot's address and owner of record, sd:4964) and the checkout's open items of
every kind but report. The checkout is the one repo in the workflow database's repo table
whose folder is named `hoa` (sd:3985), so no config file holds its path. A
second repo with that folder name is an error that names both, never a guess.

**Each source is guarded on its own.** A file that is missing or does not
parse is that part's `error`; the other parts still answer, and the page shows
the failed part as unknown with the reason, never as an empty list. The read
reaches no network: the map draws the vectors in the files, and the page loads
no tiles. It runs one command, the checkout's bounded `tools/source-ages.py`,
and reads the refresh jobs' launchd state, and asks the checkout's `tools/sweep.py status --json` for the last sweep
(`sweep`) and each source's last download with the end of its log (`downloads`, sd:5626, sd:5680); it starts nothing. The writes are Acknowledge
(`acknowledge`) and Acknowledge all (`acknowledge_many`), the jobs route Refresh now posts, Run sweep (`sweep.start`) and
Download (`hoa_download.start`).
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from sd_db import SdDbError, operations, repos, workflow, writes

from . import hoa_download, sweep
from .documents import _config_dir, pipe_lines

__all__ = ["ALARM_DAYS", "ALARM_ROOT", "REFRESH_JOBS", "SOURCE_AGES", "SOURCE_SECONDS", "SOON_DAYS", "STALE_DAYS", "THRESHOLDS", "acknowledge", "acknowledge_many", "alarm_key", "document",
           "thresholds"]

#: The folder name that picks the checkout out of the repo table.
FOLDER = "hoa"

#: Days of alarm events the page carries; the trends carry every row, for the All range (sd:6689).
ALARM_DAYS = 30

#: An item due within this many days is caution; one past its due date is a warning.
SOON_DAYS = 7

#: The export is daily and the pump file lags the analog file by one day, so a newest row two days old is on time.
STALE_DAYS = 2

DEFAULTS = {"mission": "water/metrics/mission", "assets": "water/map/assets.geojson", "base": "water/map/base.geojson",
            "lots": "downloads/parcels/lots.json"}

#: The hoa checkout's script that writes the lot data (sd:4964); a page that has none says to run it.
LOT_SCRIPT = "python3 tools/fetch-lot-parcels.py"

#: Each Mission file, the column that dates a row, and the columns the page reads.
FILES = {
    "analog": ("analog-daily.csv", "date", ("samples", "aquifer_ft_min", "aquifer_ft_mean", "aquifer_ft_max", "tank_ft_min",
                                            "tank_ft_mean", "tank_ft_max", "a2_flow_gpm_mean", "booster_psi_mean")),
    "pump": ("pump-daily.csv", "date", ("a2_starts", "a2_runtime_hours", "a2_gallons", "a2_flow_gpm_running", "a2_gallons_metered")),
    "alarms": ("alarm-events.csv", "datetime", ("kind", "event", "notified", "minutes")),
}

#: The trend series' problem levels (sd:4969): the hoa checkout states its levels in prose only (the tank's fire floor),
#: so the `dashboard.hoa_thresholds` setting row holds them (sd:5109). The file it replaced is read only while the
#: database has never held the row (`setting_rows.text`).
THRESHOLDS_KEY = "dashboard.hoa_thresholds"
THRESHOLDS = _config_dir() / "hoa-thresholds.conf"

#: The trend series a level may name: `hoa.js` SERIES keys.
SERIES = ("tank", "runtime", "aquifer")

#: The hoa checkout's one read of each source's age (hoa sd:4965), run from the checkout; `--json` is what the lamps read.
SOURCE_AGES = "tools/source-ages.py"

#: Its ceiling: one Python start and a few small CSV reads.
SOURCE_SECONDS = 10.0

#: The fields of each source row that the page reads; every row the script prints is a lamp.
SOURCE_KEYS = ("source", "label", "latest", "age_days", "allowed_days", "state", "ends", "refresh", "todo", "error")

ASSET_KEYS = ("id", "type", "name", "status", "accuracy", "source", "notes", "updated")
BASE_KEYS = ("kind", "ain", "label", "luc", "acres")


def checkout(connection) -> tuple[str | None, Path | None, list[str]]:
    """The registered repo whose folder is named `hoa`: its recorded path, its place on disk, and what stops the read."""
    found = [row["path"] for row in repos.registered(connection) if Path(row["path"]).name == FOLDER]
    if len(found) > 1:
        return None, None, [f"two repos are named {FOLDER}: {', '.join(found)}"]
    if not found:
        return None, None, []
    return found[0], Path(os.path.expanduser(found[0])), []


def _paths(repo: Path | None) -> dict:
    return {key: repo / place if repo else None for key, place in DEFAULTS.items()}


def _day(text: str) -> date:
    return date.fromisoformat(text[:10])


def _csv(path: Path, stamp: str, columns: tuple[str, ...]) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in (stamp, *columns) if name not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name} has no {', '.join(missing)} column")
        rows = [{name: (row.get(name) or "").strip() for name in (stamp, *columns)} for row in reader]
    for number, row in enumerate(rows, 2):
        try:
            _day(row[stamp])
        except ValueError:
            raise ValueError(f"{path.name} line {number}: {row[stamp]!r} is not a date") from None
    return sorted(rows, key=lambda row: row[stamp])


def _mission(folder: Path | None, today: date) -> dict:
    out = {"dir": str(folder or ""), "error": "", "files": [], "latest": "", "age_days": None, "stale": False,
           "analog": [], "pump": [], "alarms": [], "alarms_today": {"count": None, "reason": "", "acked": 0}}
    if folder is None:
        out["error"] = "no export folder: no repo named hoa is registered"
        return out
    for key, (name, stamp, columns) in FILES.items():
        try:
            rows = _csv(folder / name, stamp, columns)
        except FileNotFoundError:
            out["files"].append({"name": name, "rows": 0, "latest": "", "error": f"{name} is not in the export folder"})
            continue
        except (OSError, UnicodeDecodeError, ValueError, csv.Error) as error:
            out["files"].append({"name": name, "rows": 0, "latest": "", "error": str(error)})
            continue
        latest = rows[-1][stamp] if rows else ""
        out["files"].append({"name": name, "rows": len(rows), "latest": latest, "error": ""})
        if key == "alarms":
            since = (_day(latest) - timedelta(days=ALARM_DAYS)).isoformat() if rows else ""
            out[key] = [row for row in rows if row[stamp] >= since]
        else:
            out[key] = rows
    broken = [f["error"] for f in out["files"] if f["error"]]
    if broken:
        out["error"] = "; ".join(broken)
    days = [f["latest"][:10] for f in out["files"] if f["latest"]]
    if days:
        out["latest"] = max(days)
        out["age_days"] = (today - _day(out["latest"])).days
        out["stale"] = out["age_days"] > STALE_DAYS
    alarms = next((f for f in out["files"] if f["name"] == FILES["alarms"][0]), None)
    if alarms and not alarms["error"]:
        if alarms["latest"][:10] == today.isoformat():
            real = [row for row in out["alarms"] if row["datetime"][:10] == alarms["latest"][:10] and _real(row)]
            out["alarms_today"] = {"count": len(real), "reason": ""}
        else:
            out["alarms_today"]["reason"] = f"the export ends {alarms['latest'][:10] or 'with no rows'}"
    elif alarms:
        out["alarms_today"]["reason"] = alarms["error"]
    return out


def thresholds(connection, path: Path | None = None) -> dict:
    """Each series' problem level, `{"dir": "below"|"above", "value"}`, from `<series>|below|<number>` lines; a line not
    read is named in `problems`. No list is no level, which the page says; a row or file that does not read is `error`."""
    from . import setting_rows

    text, source, problem = setting_rows.text(connection, THRESHOLDS_KEY, THRESHOLDS if path is None else path)
    # The page says "problem level not read: <error>".
    out = {"source": source, "levels": {}, "problems": [], "error": problem.removeprefix("not read: ")}
    if problem or text is None:
        return out
    out["problems"] = parse_thresholds(text, out["levels"])
    return out


def parse_thresholds(text: str, levels: dict | None = None) -> list[str]:
    """One problem per line of `text` that is not a level; each level goes into `levels`."""
    levels, problems = {} if levels is None else levels, []
    for number, _, fields in pipe_lines(text):
        if len(fields) != 3 or fields[0] not in SERIES or fields[1] not in ("below", "above"):
            problems.append(f"line {number}: expected <series>|below|<number> or <series>|above|<number>, "
                            f"series one of {', '.join(SERIES)}")
            continue
        try:
            value = float(fields[2])
        except ValueError:
            value = math.nan
        if not math.isfinite(value):
            problems.append(f"line {number}: {fields[2]!r} is not a number")
        elif fields[0] in levels:
            problems.append(f"line {number}: {fields[0]} has a level already")
        else:
            levels[fields[0]] = {"dir": fields[1], "value": value}
    return problems


def sources(place: Path | None) -> dict:
    """Each source's age and refresh job, from `SOURCE_AGES --json` (sd:4965), within `SOURCE_SECONDS`.

    The script exits 1 when a source did not read and still prints every row, so its rows are kept and the failed one is
    `unknown` with its `error`; any other failure, a timeout or a script the checkout lacks, is this part's `error`.
    """
    out = {"error": "", "rows": [], "command": f"python3 {SOURCE_AGES} --json", "repo": str(place or "")}
    if place is None:
        out["error"] = "no checkout: no repo named hoa is registered"
        return out
    script = place / SOURCE_AGES
    if not script.is_file():
        out["error"] = f"{SOURCE_AGES} is not in the checkout"
        return out
    try:
        done = subprocess.run([sys.executable, str(script), "--json"], cwd=place, capture_output=True, text=True,
                              timeout=SOURCE_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        out["error"] = f"{SOURCE_AGES} ran past its {SOURCE_SECONDS:g} seconds"
        return out
    except OSError as error:
        out["error"] = f"{SOURCE_AGES} did not start: {error}"
        return out
    said = (done.stderr.strip().splitlines() or [""])[0]
    try:
        rows = json.loads(done.stdout)["sources"] if done.returncode in (0, 1) else None
        if rows is not None and not all(isinstance(row, dict) for row in rows):
            raise TypeError("a source is not an object")
    except (ValueError, KeyError, TypeError) as error:
        out["error"] = f"{SOURCE_AGES} printed no source list: {error}"
        return out
    if rows is None:
        out["error"] = f"{SOURCE_AGES} exited {done.returncode}" + (f": {said}" if said else "")
        return out
    out["rows"] = [{key: row.get(key) for key in SOURCE_KEYS} | _job_name(row) for row in rows]
    return out


def _job_name(row: dict) -> dict:
    """`{"job": <name>}` for a row with a refresh job, else `{}`. The row names it (hoa sd:5279), a null meaning none; a
    row from an older checkout carries no `job`, and `REFRESH_JOBS` stands in."""
    name = row["job"] if "job" in row else REFRESH_JOBS.get(row.get("source"))
    return {"job": name} if isinstance(name, str) and name else {}


#: The manual cron job (`JOB_SCHEDULE="manual"`) that refreshes each source, for a checkout whose source-ages rows do
#: not name it yet (sd:5280); Refresh now starts it through the route Management's Retry posts,
#: `/api/jobs/<name>/retry` (sd:4965). The job files are private config, installed with `cron-jobs.sh install <name>`.
REFRESH_JOBS = {"mission": "hoa-mission-export"}

#: The fields of `operations.job_state` the page reads.
JOB_KEYS = ("name", "service", "state", "last_exit", "revision", "capabilities", "last_request")


def _said(log: dict, name: str) -> str:
    """The last line the job's run printed, not the wrapper's own `[<job>] ...` lines: a refused run's stderr reason."""
    lines = [line.strip() for line in log.get("text", "").splitlines()]
    return next((line for line in reversed(lines) if line and not line.startswith(f"[{name}] ")), "")


def _refresh_jobs(connection, rows: list[dict], backend) -> None:
    """Give each row with a refresh job its launchd state, from the jobs inventory Management reads. A job that is not
    installed is the row's `job.error`, with the line that installs it; the read never starts anything."""
    from .management_screen import job_log

    root = getattr(backend, "cron_root", None)
    for row in rows:
        name = row.get("job")
        if not name:
            continue
        try:
            job = operations.job_state(connection, name, backend=backend)
        except workflow.WorkflowError as error:
            row["job"] = {"name": name, "error": f"{name} is not an installed job ({error}); "
                                                 f"local-cron-jobs/cron-jobs.sh install {name} installs it"}
            continue
        row["job"] = {key: job.get(key) for key in JOB_KEYS} | {"said": _said(job_log(root, name), name)}



def _download(source: str, place: Path) -> dict:
    """The source's last download (`hoa_download.status`) and `tail`, the end of its start.log in Management's log shape
    (sd:5680): Show log on the row reads it. Only the path the status names is read, never one from a request."""
    from .management_screen import LOG_BYTES, log_tail, unreadable

    out = hoa_download.status(source, place)
    tail = {"state": "unread", "text": "", "cut": False, "reason": ""}
    try:
        _, raw, start = log_tail(Path(out["log"]), LOG_BYTES)
    except FileNotFoundError:
        tail |= {"state": "none", "reason": f"no log: the {out['label']} download has not run on this machine"}
    except OSError as error:
        tail |= {"reason": unreadable(error)}
    else:
        tail |= {"state": "read", "text": raw.decode("utf-8", "replace"), "cut": bool(start)}
    return out | {"tail": tail}

# ---------------------------------------------------------------------------
# Acknowledge (sd:4967): the workflow database's `view` state, the store Designs' Hide and Documents' Pin write
# (sd:3428, sd:3447). An acknowledged alarm event is a hidden view keyed `hoa-alarm/<datetime> <event>`: the row's own
# fields, so the key holds across reads and across a newer export. Nothing reaches the Mission portal.

#: The key prefix of an alarm event's view state.
ALARM_ROOT = "hoa-alarm"


def alarm_key(row: dict) -> str:
    return f"{ALARM_ROOT}/{row['datetime']} {row['event']}"


def _acks(connection, mission: dict) -> None:
    """Mark each alarm row `key` and `acked`, count today's acknowledged alarms apart, and say whether the store was read.
    A store that does not read acknowledges nothing: every row shows, and `acks.reason` says why Acknowledge is off."""
    from .documents_screen import views

    kept, unread = views(connection)
    for row in mission["alarms"]:
        row["key"] = alarm_key(row)
        row["acked"] = kept.get(row["key"], {}).get("hidden", False)
    mission["acks"] = {"available": not unread, "reason": unread}
    today = mission["alarms_today"]
    if today["count"] is not None:
        day = max(row["datetime"][:10] for row in mission["alarms"])
        real = [row for row in mission["alarms"] if row["datetime"][:10] == day and _real(row)]
        today["acked"] = sum(row["acked"] for row in real)
        today["count"] = len(real) - today["acked"]


def _real(row: dict) -> bool:
    """An alarm, not a generator test or a return to normal."""
    return row["kind"] != "generator" and "Normal" not in row["event"]


def _listed(connection) -> set[str]:
    """The keys of the alarm events the export lists now: a write reads the file again, so a key it no longer holds is stale."""
    _, place, problems = checkout(connection)
    if problems or place is None:
        raise ValueError(f"Nothing was acknowledged: {problems[0] if problems else 'no repo named hoa is registered'}.")
    name, stamp, columns = FILES["alarms"]
    try:
        rows = _csv(_paths(place)["mission"] / name, stamp, columns)
    except (OSError, UnicodeDecodeError, ValueError, csv.Error) as error:
        raise ValueError(f"Nothing was acknowledged: {name} did not read: {error}") from None
    return {alarm_key(row) for row in rows}


def _need_views() -> None:
    if not hasattr(writes, "set_view"):
        raise ValueError("The command pack has no view store yet (sd:3430): pull it and restart the dashboard.")


def acknowledge(payload: dict):
    """`POST /api/hoa/alarm`: `{"key", "acked"}`; the write as `action_route` returns one. The write reads the export
    again, so a key the export no longer holds is refused as stale and nothing is written."""
    key, acked = payload.get("key"), payload.get("acked")
    if set(payload) != {"key", "acked"} or not isinstance(key, str) or not isinstance(acked, bool) \
            or not key.startswith(f"{ALARM_ROOT}/"):
        raise ValueError("Send the alarm event's key and acked, true or false.")
    _need_views()

    def write(connection):
        if key not in _listed(connection):
            raise workflow.StaleItem(f"{FILES['alarms'][0]} holds no alarm event {key[len(ALARM_ROOT) + 1:]}. "
                                     "Nothing was acknowledged. Read HOA again.")
        return {"key": key, **writes.set_view(connection, key, hidden=acked)}

    return write


def acknowledge_many(payload: dict):
    """`POST /api/hoa/alarms`: `{"keys", "acked"}`, Acknowledge all and its Undo: the single write for each key, in one
    transaction, with the export read once. A key the export no longer holds, or the store refuses, is `failed` with its
    reason and the others still land (`acked`); when none lands the answer is stale and nothing is written."""
    keys, acked = payload.get("keys"), payload.get("acked")
    if set(payload) != {"keys", "acked"} or not isinstance(acked, bool) or not isinstance(keys, list) or not keys \
            or not all(isinstance(key, str) and key.startswith(f"{ALARM_ROOT}/") for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("Send the alarm events' keys, each once, and acked, true or false.")
    _need_views()

    def write(connection):
        listed, landed, failed = _listed(connection), [], []
        for key in keys:
            if key not in listed:
                failed.append({"key": key, "error": f"{FILES['alarms'][0]} holds no alarm event {key[len(ALARM_ROOT) + 1:]}"})
                continue
            try:
                landed.append({"key": key, **writes.set_view(connection, key, hidden=acked)})
            except SdDbError as error:
                failed.append({"key": key, "error": str(error)})
        if not landed:
            raise workflow.StaleItem(f"{FILES['alarms'][0]} holds none of the {len(keys)} alarm events. "
                                     "Nothing was acknowledged. Read HOA again.")
        return {"acked": landed, "failed": failed}

    return write


def _geojson(path: Path | None, keys: tuple[str, ...], kind: str) -> dict:
    if path is None:
        return {"error": f"no {kind} file: no repo named hoa is registered", "features": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        features, lots = [], 0
        for feature in data["features"]:
            props, geometry = feature["properties"], feature["geometry"]
            row = {key: props.get(key, "") for key in keys}
            # A lot's index among the file's lots, counted before any is skipped: the lot data's key (sd:4964).
            if props.get("kind") == "lot":
                row["index"], lots = lots, lots + 1
            if geometry["type"] == "Point":
                row["lng"], row["lat"] = (round(float(v), 7) for v in geometry["coordinates"][:2])
            elif geometry["type"] == "Polygon":
                row["rings"] = [[[round(float(x), 7), round(float(y), 7)] for x, y, *_ in ring] for ring in geometry["coordinates"]]
            elif geometry["type"] == "LineString" and kind == "assets":
                # sd:7516: an irrigation line. Its middle vertex is its lng, lat: where the page centres it and Details places
                # it. The base's outlines stay polygons only: the page draws each from its rings.
                row["path"] = [[round(float(x), 7), round(float(y), 7)] for x, y, *_ in geometry["coordinates"]]
                row["lng"], row["lat"] = row["path"][len(row["path"]) // 2]
            else:
                continue
            features.append(row)
    except FileNotFoundError:
        return {"error": f"{path.name} is not there", "features": []}
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, IndexError) as error:
        return {"error": f"{path.name} did not parse: {error.__class__.__name__}: {error}", "features": []}
    return {"error": "", "features": features}


def _lots(path: Path | None, base: Path | None, features: list[dict]) -> dict:
    """The checkout's lot data (downloads/parcels/lots.json, the hoa checkout's `LOT_SCRIPT`), written onto each lot of
    `features` as address, owner and parcel. It names homeowners, so it is read from the gitignored downloads/ at request
    time and never copied. It joins only against the base file it was made from (its `base_sha256`); anything else is
    the part's error, and no lot gets a value."""
    if path is None:
        return {"error": "no lot data: no repo named hoa is registered", "source": "", "fetched_at": ""}
    rerun = f"run {LOT_SCRIPT} in the hoa checkout"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        by_index = {}
        for row in data["lots"]:
            if not isinstance(row["index"], int) or not all(isinstance(row.get(k), str) for k in ("ain", "address", "owner")):
                raise ValueError(f"lot row {row!r:.40} is not index, ain, address and owner")
            by_index[row["index"]] = row
        made_from, fetched = data["base_sha256"], str(data.get("fetched_at") or "")
        current = hashlib.sha256(base.read_bytes()).hexdigest()
    except FileNotFoundError as error:
        if Path(error.filename or "") == base:
            return {"error": f"{base.name} is not there", "source": str(path), "fetched_at": ""}
        return {"error": f"lot data not fetched: {rerun}", "source": str(path), "fetched_at": ""}
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as error:
        return {"error": f"{path.name} did not parse: {error.__class__.__name__}: {error}", "source": str(path), "fetched_at": ""}
    if made_from != current:
        return {"error": f"lot data is for another base.geojson: {rerun}", "source": str(path), "fetched_at": fetched}
    for feature in features:
        row = by_index.get(feature.get("index")) if feature["kind"] == "lot" else None
        if row:
            feature.update(address=row["address"], owner=row["owner"], parcel=row["ain"])
    return {"error": "", "source": str(path), "fetched_at": fetched}


def _items(connection, repo: str | None, now: str, today: date) -> dict:
    """The checkout's open items of every kind, and any other whose own area is hoa (pack sd:8010), less any whose
    own area is home; ranked: overdue, then due within a week, then the rest; priority breaks
    ties. A report is its family's (`places.area_of`), so it is not here (sd:4959). Each is the Tasks page's row
    (`tasks_screen._rows`, read in one snapshot as `places.tasks` reads it), so its Details on HOA are the Tasks page's
    (sd:5671), with its revision, moves, edit capability and issue targets; HOA adds `due_days` and `state`. `done` holds
    the week's done ones (`places.task_rows`), for the Tasks view's All (sd:5752); each is `ok`, and none is counted."""
    from .operations_screen import _one_snapshot
    from .places import area_of, task_rows

    if repo is None:
        return {"error": "no checkout: no repo named hoa is registered", "rows": [], "done": []}
    with _one_snapshot(connection):
        # pack sd:8010: an item of another checkout whose own area is hoa is HOA's too, so every repo's rows are read.
        rows, done = task_rows(connection, lambda path, kind, own=None: area_of(path, kind, repo, own) == "hoa", now=now)
    for row in rows + done:
        try:
            due = (_day(row["due"]) - today).days if row["due"] else None
        except ValueError:
            due = None
        row["due_days"], row["state"] = due, "ok" if row["status"] == "done" else "queued" if due is None or due > SOON_DAYS else "caution" if due >= 0 else "warning"
    order = {"warning": 0, "caution": 1, "queued": 2}
    rows.sort(key=lambda row: (order[row["state"]], row["priority"] is None, row["priority"] or 0,
                               row["due_days"] is None, row["due_days"] or 0, row["id"]))
    return {"error": "", "rows": rows, "done": done}


def document(connection, *, now: str, jobs=None) -> dict:
    """The HOA page's document: the map, the Mission export, the checkout's open items and its sources' ages.
    `jobs` is the operations backend the refresh jobs are read from, `operations.LaunchdBackend` by default."""
    recorded, place, problems = checkout(connection)
    state = "error" if problems else "read" if recorded else "missing"
    today = datetime.fromisoformat(now.replace("Z", "+00:00")).date()
    paths = _paths(place)
    mission, items = _mission(paths["mission"], today), _items(connection, recorded, now, today)
    if problems:
        mission["error"], items["error"] = problems[0], problems[0]
    _acks(connection, mission)
    base = _geojson(paths["base"], BASE_KEYS, "base") if not problems else {"error": problems[0], "features": []}
    lots = (_lots(paths["lots"], paths["base"], base["features"]) if not base["error"]
            else {"error": f"no lot data: {base['error']}", "source": "", "fetched_at": ""})
    ages = sources(place) if not problems else {"error": problems[0], "rows": [], "command": "", "repo": ""}
    if ages["rows"]:
        _refresh_jobs(connection, ages["rows"], jobs or operations.LaunchdBackend())
    return {
        "read": now,
        "config": {"state": state, "source": "the repo table", "problems": problems},
        "mission": mission,
        "assets": _geojson(paths["assets"], ASSET_KEYS, "assets") if not problems else {"error": problems[0], "features": []},
        "base": base,
        "lots": lots,
        "items": items,
        "thresholds": thresholds(connection),
        "sources": ages,
        "sweep": sweep.status(place) if not problems else {**sweep.status(None), "error": problems[0]},
        "downloads": {source: _download(source, place) for source in hoa_download.SOURCES} if place else {},
        "attention": _attention(mission, items),
    }


def _attention(mission: dict, items: dict) -> dict:
    """The rail badge (sd:3446): the annunciator cells that can light, as hoa.js draws them: alarms today, overdue or
    soon items, and a stale export. Water, tank, pumps and aquifer are never lit."""
    from .v2.pages import attention

    states = [r["state"] for r in items["rows"]] if not items["error"] else []
    cells = ["warning" if mission["alarms_today"]["count"] else "",
             "warning" if "warning" in states else "caution" if "caution" in states else "",
             "caution" if mission["latest"] and mission["stale"] else ""]
    return attention(cells, {"caution": "HOA annunciator cells", "warning": "HOA annunciator cells", "ok": "HOA water system"})
