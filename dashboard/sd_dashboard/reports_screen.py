"""Database reports, and the bounded, read-only machine and vault views other pages show.

The views (`VIEWS`) are `sd_tile.py` tabs, each run as a child under its budget by `collect` and served by `view`:
Toolbox as Management's Machine view, Research as its Repos filter and Vault on Notes (pack sd:7527); Queues left
for Today's Needs you (sd:8013).
They were Operations > Resources until then.

Since sd:2121 it also builds the documents behind the default UI's Reports
page (`v2/reports.html`, at `/reports`); the page holds no rows:

- `/api/reports` is `document`: the newest 200 reports `reporting.reports`
  gives v1, each with its provenance, its body's last `BODY_CHARS`
  characters, the revision the acknowledge route checks and its open
  followups; the scheduled jobs `operations.inventory` gives Management; each
  job's run cadence over the last seven local days, read from its
  `cron-jobs.sh` log, with its own last scheduled run before today and
  whether the log holds it, whatever the job's cadence (`cadence`); and the job families the
  `dashboard.report_families` setting row names (sd:5109). Each source is guarded on its own: a failed
  one is `null` with its reason in `sources`, never an empty list.
"""

import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from sd_db import operations, reads, reporting
from sd_db.errors import SdDbError

from . import job_failures
from .controls import form
from .documents import _config_dir, pipe_lines
from .management_screen import log_tail, unreadable
from .markup import join, tag

VIEWS = (("toolbox", "Toolbox"), ("vault", "Vault"), ("research", "Research"))
# The child each view runs: one plugin tile, owned by this dashboard checkout
# and never supplied by a page.
TILE = Path(__file__).resolve().parents[1] / "sd_tile.py"
# Each view's time ceiling, applied by `collectors.Budget` together with the
# shared 64 KB while the tile's output is read (sd:758). Five seconds was the
# retired plugin contract's figure, and these tabs were built for it
# (`sd_tile.py`, `TILE_SECONDS`).
# Measured on 2026-09-13 through this interpreter at load average ~14, ten runs
# each: toolbox, briefs and vault 0.08s worst, research 0.53s worst. Read those
# as the quiet case: on 2026-09-14, with this machine at load average 11 to 45,
# Toolbox, Vault and Research were each seen refused at this ceiling, and Briefs
# and Vault answered in 0.19-0.50s a few minutes later at load 42. The scan did
# not grow; the machine was busy. A view that outgrows its ceiling is
# refused with the reason, not waited on; file it as sd:756 was filed for Ports
# rather than raise the number here.
VIEW_SECONDS = {"toolbox": 5.0, "vault": 5.0, "research": 5.0}


def _collectors():
    # `Budget` is defined once, in the collectors this dashboard owns, and read
    # by path as `ports_screen.py` reads it.
    path = Path(__file__).resolve().parents[1] / "collectors.py"
    spec = importlib.util.spec_from_file_location("sd_dashboard_resource_collectors", path)
    if spec is None or spec.loader is None:
        raise ValueError("resource budget is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect(area):
    if area not in dict(VIEWS):
        raise ValueError("unknown resource view")
    module = _collectors()
    budget = module.Budget(VIEW_SECONDS[area])
    try:
        # Both ceilings apply while reading: past either one the tile's process
        # group is killed then, not after it exits (sd:758). A cut of the
        # tile's stderr is named for the tab, not the interpreter (sd:834).
        # The view's start on the shared clock: the tile's deadline counts from it, not from the tile's main (sd:2501).
        since = time.clock_gettime(time.CLOCK_MONOTONIC)
        process = budget.run([sys.executable, "-I", str(TILE), area, f"{since:.6f}"], label=area)
    except module.OverBudget as error:
        raise ValueError(f"resource inspection was stopped at its budget: {error}") from None
    if process.returncode:
        raise ValueError(process.stderr.strip() or f"resource collector exited {process.returncode} without a reason")
    result = json.loads(process.stdout)
    if not isinstance(result, dict) or not _sections(result.get("sections")):
        raise ValueError("resource collector returned incomplete output")  # noqa: TRY004 - external document validation
    rows = result.get("rows", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict)
            or not isinstance(row.get("what"), str) or not isinstance(row.get("detail"), str) for row in rows):
        raise ValueError("resource collector returned incomplete observations")  # noqa: TRY004 - external document validation
    paths = result.get("paths", [])
    if not isinstance(paths, list) or not all(map(_text, paths)):
        raise ValueError("resource collector returned incomplete output")  # noqa: TRY004 - external document validation
    return {"title": str(result.get("title") or dict(VIEWS)[area]), "sections": result["sections"],
            "rows": [{"what": row["what"], "detail": row["detail"]} for row in rows]} | ({"paths": paths} if "paths" in result else {})


def _text(value):
    return isinstance(value, str)


def _cell(value):
    return (isinstance(value, dict) and _text(value.get("text")) and set(value) <= {"text", "title", "href", "num"}
            and all(_text(value[key]) for key in ("title", "href") if key in value)
            and str(value.get("href", "https://")).startswith(("https://", "http://")))


def _section(section):
    """One of sd_tile.py's sections (its docstring): figures, a heading, a table or a listing, all text."""
    if not isinstance(section, dict) or len(section) != 1:
        return False
    (kind, value), = section.items()
    if kind == "heading":
        return _text(value)
    if kind == "listing":
        return isinstance(value, list) and all(map(_text, value))
    if kind == "figures":
        return isinstance(value, list) and all(isinstance(pair, list) and len(pair) == 2 and all(map(_text, pair)) for pair in value)
    if kind == "table":
        return (isinstance(value, dict) and set(value) == {"columns", "rows", "search"} and _text(value["search"])
                and isinstance(value["columns"], list) and isinstance(value["rows"], list)
                and all(isinstance(column, list) and len(column) == 2 and all(map(_text, column)) for column in value["columns"])
                and all(isinstance(row, list) and len(row) == len(value["columns"]) and all(map(_cell, row)) for row in value["rows"]))
    return False


def _sections(sections):
    return isinstance(sections, list) and all(map(_section, sections))


def view(area):
    """A moved view's API document (pack sd:7527): the tile's `{title, sections, rows}`, or 503 with the reason it
    could not be observed, never an empty view."""
    try:
        return collect(area)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return 503, {"error": f"{dict(VIEWS)[area]} could not be observed: {error}"}


def report_controls(connection, row, revision):
    # A routine brief is a report row filed done for good (sd:3459): no source to show, nothing to acknowledge.
    if row["kind"] != "report" or row["source"] == "brief":
        return ""
    try:
        # The library's parse, so the verdict is the one retention and the
        # bulk clean give (`reporting.UNREADABLE_FIELDS`): NULL, '', text that
        # is not JSON, `NaN`, and nesting past SQLite's depth are one case.
        # Before sd:873 the parse was `json.loads(row["fields"] or "{}")`,
        # which turned NULL and '' into an empty document: every provenance
        # field read "Not recorded" and the acknowledge form was offered.
        fields = reporting.fields_document(connection, row["fields"])
    except reporting.UnreadableFields:
        # sd:775. `item.fields` has no `json_valid` check on it, so a row the
        # page cannot parse is a row the store permits; unguarded, the parse
        # raised through `screens.item` and `do_GET` -- which catches
        # `MissingItem`, `NotFound` and the database errors and not this --
        # and the reader got no response at all, not even an error page.
        # Fail closed the way the same column is treated elsewhere: one
        # meaning for NULL, '' and text that is not JSON, the
        # `reporting.UNREADABLE_FIELDS` predicate retention and the bulk
        # clean read (sd:754 I11, sd:755, sd:873). Such a report is for a
        # person to look at, and this page is that person's surface: it
        # renders, the reason is plain, and the acknowledge form is withheld
        # rather than offered beside a source nothing could read, because
        # one click here would close unexamined the row those two declined.
        # `reporting.acknowledge` never parses `fields`, so the CLI, whose
        # acknowledge takes a named `who`, still finishes such a report and
        # the notice names it.
        return tag("section", tag("h2", "Report source"),
            tag("p", "This report's stored fields are missing or not valid JSON, so its source cannot be read. "
                     "This page withholds Acknowledge: one click would close a report nobody has read. "
                     f"Read its log, then acknowledge report {row['id']} in a terminal: ",
                tag("code", f"sd reports acknowledge {row['id']}"), ".", class_="notice"),
            class_="control-panel")
    provenance = fields.get("report", {}) if isinstance(fields, dict) else {}
    if not isinstance(provenance, dict):
        provenance = {}
    # sd:920: a job failing the same way on a schedule folds into one report,
    # and `fields.repeats` carries the count and the last run. One row, only
    # when the key exists; a single failure's page is unchanged.
    repeats = fields.get("repeats") if isinstance(fields, dict) else None
    repeated = (tag("div", tag("dt", "Repeats"),
                    tag("dd", f"{repeats.get('count', '?')} runs, last {repeats.get('last_ended', 'Not recorded')}"))
                if isinstance(repeats, dict) else "")
    return tag("section", tag("h2", "Report source"),
        tag("dl", join(tag("div", tag("dt", label), tag("dd", provenance.get(key, "Not recorded")))
            for key, label in (("job", "Job"), ("started", "Started"), ("ended", "Ended"), ("exit_code", "Exit code"),
                               ("source_path", "Source log"))),
            tag("div", tag("dt", "Attention evidence"), tag("dd", _basis(provenance, repeats) or "Not recorded")), repeated),
        tag("p", "Output was truncated. The complete source remains in the log.", class_="notice") if _cut(provenance, repeats) else "",
        form(f"/api/reports/{row['id']}/acknowledge", label="Acknowledge report", command=f"sd reports acknowledge {row['id']}", revision=revision)
        if row["status"] != "done" else tag("p", "Acknowledged"), class_="control-panel")


# ---------- The default UI's Reports page (sd:2121) ----------

#: The family list's setting row (sd:5109): job names are the operator's, and this repository is public. The file it
#: replaced is read only while the database has never held the row (`setting_rows.text`).
FAMILIES_KEY = "dashboard.report_families"
FAMILIES_CONFIG = _config_dir() / "report-families.conf"
#: How much of a report's body the page shows: the end, where a failing run says why.
BODY_CHARS = 1400
#: The local days the run cadence covers, ending today.
DAYS = 7
#: How much of a job log is read from its end. A log longer than this is read from the first whole line in it, and the
#: days up to and including that line's day are `not read`, never zero runs: the line can fall partway through its day.
TAIL_BYTES = 1 << 20
#: How far back `last_scheduled` looks for a job's last scheduled run: a year and a little, so a yearly job has one.
LOOKBACK = 400
#: The outcome lines `cron-jobs.sh` writes for a run: `[<job>] <stamp> done` and `[<job>] <stamp> FAILED rc=<n>`.
#: The stamp carries the machine's offset, so its date is the local day the run belongs to. The marker ends the line
#: but need not start it: a job whose output has no trailing newline gets `error[demo] <stamp> FAILED rc=7`.
OUTCOME = re.compile(r"\[(?P<job>[^\]\s]+)\] (?P<day>\d{4}-\d{2}-\d{2})T[0-9:]+[+-]\d{4} (?P<what>done|FAILED rc=\d+)\s*$")
STAMP = re.compile(r"^\[[^\]\s]+\] (\d{4}-\d{2}-\d{2})T")
#: A family line: `family|<key>|<label>|<icon>|<job>,<job>,...`.
KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
JOB = re.compile(reporting.JOB_NAME)
FAILURES = (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error)


def parse_families(text: str) -> tuple[list[dict], list[str]]:
    """The families in file order, and one problem per line that was not read."""
    families, problems, owner = [], [], {}
    for number, _, fields in pipe_lines(text):
        if len(fields) != 5 or fields[0] != "family":
            problems.append(f"line {number}: expected family|<key>|<label>|<icon>|<job>,<job>,...")
            continue
        _, key, label, icon, names = fields
        jobs = [name.strip() for name in names.split(",") if name.strip()]
        if not KEY.fullmatch(key) or not KEY.fullmatch(icon):
            problems.append(f"line {number}: the key and the icon are lower-case words joined by hyphens")
            continue
        if not label or not jobs:
            problems.append(f"line {number}: a family needs a label and at least one job")
            continue
        bad = [name for name in jobs if not JOB.fullmatch(name)]
        if bad:
            problems.append(f"line {number}: {bad[0]!r} is not a job name")
            continue
        if any(f["key"] == key for f in families):
            problems.append(f"line {number}: the family {key} is listed twice")
            continue
        repeated = next((name for at, name in enumerate(jobs) if name in jobs[:at]), None)
        if repeated:
            problems.append(f"line {number}: {repeated} is listed twice in the family {key}")
            continue
        twice = [name for name in jobs if name in owner]
        if twice:
            problems.append(f"line {number}: {twice[0]} is already in the family {owner[twice[0]]}")
            continue
        owner.update({name: key for name in jobs})
        families.append({"key": key, "label": label, "icon": icon, "jobs": jobs})
    return families, problems


def families(connection, config: Path | None = None) -> dict:
    """The families, row first; `state` error when the row was not read, so they are unknown."""
    from . import setting_rows

    text, source, problem = setting_rows.text(connection, FAMILIES_KEY, FAMILIES_CONFIG if config is None else config)
    if problem:
        return {"state": "error", "source": source, "problems": [problem], "list": []}
    if text is None:
        return {"state": "missing", "source": source, "problems": [], "list": []}
    found, problems = parse_families(text)
    return {"state": "partial" if problems else "read", "source": source, "problems": problems, "list": found}


def local_days(now: str, *, tz=None) -> list[str]:
    """The last `DAYS` local dates, oldest first, ending on the local date of `now`."""
    today = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(tz).date()
    return [(today - timedelta(days=back)).isoformat() for back in range(DAYS - 1, -1, -1)]


def scheduled(schedule: list, day: str) -> bool:
    """Whether a launchd calendar can fire on the day. An empty one is a manual job (`JOB_SCHEDULE="manual"`, sd:4965),
    which never does: only a kickstart starts it. cron-jobs.sh writes no interval job."""
    if not schedule:
        return False
    when = date.fromisoformat(day)
    weekday = (when.weekday() + 1) % 7  # launchd: 0 and 7 are Sunday
    for entry in schedule:
        if "Weekday" in entry and entry["Weekday"] % 7 != weekday:
            continue
        if "Day" in entry and entry["Day"] != when.day:
            continue
        if "Month" in entry and entry["Month"] != when.month:
            continue
        return True
    return False


def last_scheduled(schedule: list, before: str) -> str | None:
    """The last day before `before` the calendar can fire on, looking back `LOOKBACK` days; None when none is."""
    day = date.fromisoformat(before)
    for back in range(1, LOOKBACK + 1):
        when = (day - timedelta(days=back)).isoformat()
        if scheduled(schedule, when):
            return when
    return None


def job_log(path: Path, days: list[str]) -> dict:
    """One job's runs per local day from its log: `[done, failed]` for each day, where the log begins, and where the
    read begins when the log is longer than `TAIL_BYTES`. A job with no log is `log: false`. `cut` says the read began
    partway through the log; a cut read with no whole stamped line (`read_from` None) read no day at all."""
    try:
        first, tail, start = log_tail(path, TAIL_BYTES)
    except FileNotFoundError:
        return {"log": False, "from": None, "read_from": None, "cut": False, "runs": {}}
    except OSError as error:
        # Unreadable, or not a regular file: no log, with the reason Management gives for the same file (sd:3532).
        return {"log": False, "from": None, "read_from": None, "cut": False, "runs": {}, "reason": unreadable(error)}
    lines = tail.decode("utf-8", "replace").splitlines()
    begins = STAMP.match(first.decode("utf-8", "replace"))
    read_from = None
    if start:
        read_from = next((m[1] for m in map(STAMP.match, lines) if m), None)
    runs = {day: [0, 0] for day in days}
    for line in lines:
        m = OUTCOME.search(line)
        # Job output is copied verbatim, so a marker naming another job is that job's run, not this one's.
        if m and m["job"] == path.stem and m["day"] in runs:
            runs[m["day"]][0 if m["what"] == "done" else 1] += 1
    return {"log": True, "from": begins[1] if begins else None, "read_from": read_from, "cut": bool(start), "runs": runs}


def cadence(jobs: list[dict] | None, logs: Path | None, days: list[str]) -> dict:
    """Each job's runs per local day, and the days its schedule leaves out."""
    if jobs is None:
        raise ValueError("the job list was not read, so no job log was")
    if logs is None:
        raise ValueError("this jobs backend names no cron-jobs root, so no job log was read")
    out = {}
    for job in jobs:
        name, schedule = job["name"], job.get("schedule") or []
        # Health follows the job's own last scheduled run, whatever its cadence: a monthly job's falls outside `days`.
        # Today is not over, so the last run is the one before it. It is read when the log holds that day whole.
        last = last_scheduled(schedule, days[-1])
        log = job_log(logs / f"{name}.log", days + ([last] if last and last not in days else []))
        runs = (log["runs"].pop(last, [0, 0]) if last not in days else log["runs"].get(last, [0, 0])) if last else [0, 0]
        read = bool(log["log"] and last and log["from"] and last >= log["from"]
                    and not (log["cut"] and (not log["read_from"] or last <= log["read_from"])))
        out[name] = log | {"manual": not schedule, "scheduled": [scheduled(schedule, day) for day in days],
                           "last": {"day": last, "read": read, "runs": runs if read else [0, 0]}}
    return out


def _basis(provenance, repeats):
    """The open report's current finding set (sd:4664): a smaller run folds in and keeps its set in `repeats.basis`
    (pack sd:4658); `report.attention_basis` is the first run's, read when that is absent or null, as the pack's
    CURRENT_BASIS reads it."""
    current = repeats.get("basis") if isinstance(repeats, dict) else None
    return current if current is not None else provenance.get("attention_basis")


def _cut(provenance, repeats) -> bool:
    """Whether the report's kept body is cut (sd:4652): a later whole run can replace a cut first one (pack sd:4641),
    and `repeats.body_truncated` then says so; `report.truncated` is the first run's, read only when that key is absent,
    as the pack's `reporting._kept_cut` reads it."""
    repeats = repeats if isinstance(repeats, dict) else {}
    return repeats.get("body_truncated", provenance.get("truncated")) is True


def _report(connection, row, revision, followups) -> dict:
    try:
        fields = reporting.fields_document(connection, row["fields"])
        readable = isinstance(fields, dict)
    except reporting.UnreadableFields:
        fields, readable = {}, False
    fields = fields if readable else {}
    provenance = fields.get("report") if isinstance(fields.get("report"), dict) else {}
    repeats = fields.get("repeats") if isinstance(fields.get("repeats"), dict) else None
    try:
        body = json.loads(row["body"] or "{}")
    except ValueError:
        body = {}
    text = body.get("text") if isinstance(body, dict) and isinstance(body.get("text"), str) else ""
    return {
        "id": row["id"], "title": row["title"], "status": row["status"], "at": row["created_at"],
        "source": row["source"], "revision": revision, "fields_read": readable,
        "attention": fields.get("attention") is True, "record": "record" in fields,
        "job": provenance.get("job") or None, "run": provenance.get("run_id"),
        "started": provenance.get("started"), "ended": provenance.get("ended"), "exit": provenance.get("exit_code"),
        "src": provenance.get("source_path"), "basis": _basis(provenance, repeats),
        "truncated": _cut(provenance, repeats),
        "repeats": {"count": repeats.get("count"), "last": repeats.get("last_ended")} if repeats else None,
        "body": text[-BODY_CHARS:], "cut": len(text) > BODY_CHARS,
        "followups": followups,
        # sd:6964: an expired login in the job's output; Fix then opens its Credentials row.
        "login": job_failures.login(text),
    }


def _reports(connection) -> list[dict]:
    from .operations_screen import _one_snapshot

    # One revision read and one note read for the page, not an `item_state` per report (sd:3531).
    with _one_snapshot(connection):
        rows = reporting.reports(connection)
        ids = [row["id"] for row in rows]
        revisions = reads.revisions(connection, ids)
        followups = {item: [] for item in ids}
        # `brief_notes` is newest first; reversed it is the history order `item_state` lists.
        for note in reversed(reads.brief_notes(connection, ids)):
            if note["kind"] == "followup":
                followups[note["item"]].append(note["id"])
        return [_report(connection, row, revisions[row["id"]], followups[row["id"]]) for row in rows]


def _jobs(connection, backend) -> list[dict]:
    keep = ("name", "service", "schedule", "state", "last_exit", "revision", "capabilities")
    return [{key: job.get(key) for key in keep} for job in operations.inventory(connection, backend=backend)["jobs"]]


def _html_folders() -> int:
    from . import documents

    return len(documents.enumerated())


def document(connection, *, now: str, jobs=None, tz=None, config: Path | None = None, only=None) -> dict:
    """Every source the page reads, and the reason for each one that could not be read.

    `jobs` is the operations backend, the launchd one by default; its `cron_root` places the job logs. `tz` is the
    zone the local days are in, the machine's by default. `only` names the jobs a place's Reports view reads (sd:5636):
    the reports, jobs and cadence are those jobs' alone, and the HTML folders are not counted.
    """
    backend = jobs or operations.LaunchdBackend()
    days = local_days(now, tz=tz)
    out: dict = {"read": now, "limit": 200, "days": days, "sources": {}, "families": families(connection, config)}
    root = getattr(backend, "cron_root", None)
    logs = None if root is None else Path(root) / "logs"
    keep = (lambda rows, key: rows) if only is None else (lambda rows, key: [row for row in rows if row[key] in only])
    for source, collect in (("reports", lambda: keep(_reports(connection), "job")),
                            ("jobs", lambda: keep(_jobs(connection, backend), "name")),
                            ("cadence", lambda: cadence(out["jobs"], logs, days)),
                            *((("html", _html_folders),) if only is None else ())):
        try:
            out[source] = collect()
        except FAILURES as failure:
            out[source] = None
            out["sources"][source] = str(failure) or f"{source} could not be read"
            continue
        out["sources"][source] = ""
    out["attention"] = _attention(out["reports"])
    return out


def _attention(reports) -> dict:
    """The rail badge (sd:3446): open reports that ask for attention. A report read that failed is unknown."""
    from .v2.pages import attention

    if reports is None:
        return {"state": "unknown", "n": 0, "what": "reports not read"}
    return attention(("warning" if report["status"] != "done" and report["attention"] else "" for report in reports),
                     {"warning": "reports want you", "caution": "reports want you"})
