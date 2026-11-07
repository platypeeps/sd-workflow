"""Places: the HOA, Home and realm place pages' Tasks, Briefs and Reports views, and the area rule Tasks' Realm
filter uses for HOA and Home.

Each view reads its own document: `/api/<area>/tasks`, `/api/<area>/briefs` and `/api/<area>/reports`, built here
from readers that already exist, with no store of its own. HOA's Tasks is the exception: its rows are
`/api/hoa`'s items (`hoa_screen`, ranked by urgency), which the page reads anyway for its annunciator and badge; they
are the same `tasks_screen._rows` (sd:5671).

**Which rows are an area's.**

- An item is HOA when its repo is the hoa checkout (`hoa_screen.checkout`), and Home when it has no repo: the
  operator files personal and home items without one. An item's own area, `fields.area` (`sd task edit N --area
  home|hoa`, pack sd:8010), wins: it puts the item in that area alone, out of every realm. A report is in neither: its family's Reports view holds it.
  `area_of` is that rule, and Tasks' rows carry its answer as `area`, so Tasks' HOA and Home realms hold the place
  pages' Tasks rows.
- A brief is the area's when its repo is the area's checkout, or its series, lower-cased with spaces as hyphens, is a
  job of the area's report family (`series_job`): briefs always name a registered repo, so Home has only the second.
- A report is the area's when its job is in the area's report family. The family is the `dashboard.report_families` line
  whose key is the area's (`reports_screen.families`).

**A realm's place page (sd:5663).** A realm whose id is also a `dashboard.report_families` key is a place, at
`/places?realm=<id>`, its views at `/api/place/<id>/<view>`. Its Tasks are the open items `realm_of` puts in it, as
Tasks' realm tab; its Briefs are those whose repo is in it or whose series is a job of its family; its Reports are its
family's. `realm_places` says which realms are places, for the rail: a realm or family line that did not read makes none.

**Which realm a row is in (sd:5064).** Tasks' realm tabs, Personal, Work and the others: the `dashboard.realms` setting row
(sd:5109; Settings edits it) names each realm and the checkout path globs that are its (`realms`); `realm_of` gives a row the first realm whose glob matches its repo path. The
hoa checkout's rows are HOA's before any glob (pack sd:7687). A row with no repo, or a repo no line claims, is Personal; a report's realm is
`report_realm`'s: its pull repo's for pr-attention, else its family's key when that is a realm (pack sd:7774). Without a readable row a repo row's
realm is unknown (None), so Personal holds only the rows with no repo and the page says why.

**What is not read.** A missing hoa checkout, family or list makes the view `unknown` with the reason and the line to
add, never an empty list; a family list that did not read makes it `error`. Nothing here writes.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from sd_db import reads

from .documents import _config_dir, pipe_lines

__all__ = ["AREAS", "area_of", "briefs", "parse_realms", "pull_url", "realm_issues", "realm_of", "realm_places", "realms", "report_realm",
           "reports", "series_job", "task_rows", "tasks"]

#: The places, by family key, and their labels.
AREAS = {"hoa": "HOA", "home": "Home"}

#: The ids a realm line may not take: Personal is every row no line claims, and the others are Tasks' other Realm choices.
RESERVED = ("personal", "hoa", "home", "all")
REALM_ID = re.compile(r"[a-z0-9][a-z0-9-]*")


#: The realms' setting row (sd:5109), and the file it replaced, read only while the database has never held the row.
REALMS_KEY = "dashboard.realms"
REALMS_CONFIG = _config_dir() / "realms.conf"


def realms(connection, config: Path | None = None) -> dict:
    """The `dashboard.realms` row's `realm|<id>|<label>|<glob>` lines, in order; lines with one id add globs to it.

    `state` is read, partial (a line was skipped; `problems` names it), missing or error (the row was not read, so the
    realms are unknown). Row wins (`setting_rows.text`). A glob's `~` is the home folder.
    """
    from . import setting_rows

    text, source, problem = setting_rows.text(connection, REALMS_KEY, REALMS_CONFIG if config is None else config)
    if problem:
        return {"state": "error", "source": source, "problems": [problem], "list": []}
    if text is None:
        return {"state": "missing", "source": source, "problems": [], "list": []}
    found, problems = parse_realms(text)
    return {"state": "partial" if problems else "read", "source": source, "problems": problems, "list": found}


def realm_places(connection, config: Path | None = None) -> list[dict]:
    """The realms (`realms`), each with `place`: the icon of the report family whose key is its id, which makes it a
    place page (sd:5663), else None. A family list that did not read gives no realm a place."""
    from .reports_screen import families

    icons = {family["key"]: family["icon"] for family in families(connection, config)["list"]}
    return [{**realm, "place": icons.get(realm["id"])} for realm in realms(connection)["list"]]


def parse_realms(text: str) -> tuple[list[dict], list[str]]:
    """The realms in line order, and one problem per line that was not read."""
    found: dict[str, dict] = {}
    problems = []
    for number, line, fields in pipe_lines(text):
        if len(fields) != 4 or fields[0] != "realm" or not all(fields[1:]):
            problems.append(f"line {number}: not realm|<id>|<label>|<glob>: {line}")
        elif not REALM_ID.fullmatch(fields[1]) or fields[1] in RESERVED:
            problems.append(f"line {number}: {fields[1]!r} is not a realm id (lower case and hyphens; not {', '.join(RESERVED)})")
        else:
            realm = found.setdefault(fields[1], {"id": fields[1], "label": fields[2], "globs": []})
            realm["globs"].append(os.path.expanduser(fields[3]))
    return list(found.values()), problems


def realm_of(repo: str | None, kind: str, rules: list | None, hoa: str | None = None) -> str | None:
    """The realm of an item: `hoa` for the hoa checkout's (`hoa`, as `area_of`), else the first `rules` realm a glob of
    which matches its repo path, else `personal`.

    No repo is Personal. A report's realm is `report_realm`'s, here None. pack sd:7687: the hoa checkout is
    HOA before any glob and without a realm list, so its rows leave Personal for the HOA tab. `rules` None (no realm
    list, or one not read) leaves any other repo row's realm unknown, None.
    """
    if kind == "report":
        return None
    if not repo:
        return "personal"
    path = os.path.expanduser(repo)  # the database keeps ~/repos/...; the globs are expanded too
    if hoa is not None and path == os.path.expanduser(hoa):
        return "hoa"
    if rules is None:
        return None
    return next((r["id"] for r in rules if any(fnmatch.fnmatchcase(path, glob) for glob in r["globs"])), "personal")


#: A GitHub pull's address in a pr-attention report's basis, `<state> <url>` (`sd_db.pr_attention.alert`).
PULL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/[1-9][0-9]*")


def pull_url(job: str | None, basis: str | None) -> str | None:
    """The GitHub pull a pr-attention report is about, from its attention basis; None for any other report or basis
    (pack sd:7774, sd:7776). Tasks reads its repo's realm from it, and Today's Needs you links it."""
    from sd_db.pr_attention import JOB

    found = PULL.search(basis) if job == JOB and isinstance(basis, str) else None
    return found[0] if found else None


def report_realm(job: str | None, basis: str | None, rules: list | None, hoa: str | None, *, checkouts: dict,
                 families: list | None) -> str | None:
    """A report's realm (pack sd:7774): a pr-attention report's is its pull repo's registered checkout's (`checkouts`,
    lower-case `owner/name` to path, `sd_db.today.repo_keys`), by `realm_of`: the hoa checkout is HOA without a realm
    list, and a pull in no checkout has no repo, so it is Personal (pack sd:7792). Any other's is its family's key when
    that key is a realm id (`families`, `reports_screen.families`' list); no match is Personal. Without a realm list,
    or with `families` None (the family list did not read), it is unknown, None, as a repo row's."""
    found = PULL.fullmatch(pull_url(job, basis) or "")
    if found:
        return realm_of(checkouts.get(f"{found[1]}/{found[2]}".lower()), "task", rules, hoa)
    if rules is None or families is None:
        return None
    ids = {realm["id"] for realm in rules}
    return next((family["key"] for family in families if family["key"] in ids and job in family["jobs"]), "personal")


def area_of(repo: str | None, kind: str, hoa: str | None, own: str | None = None) -> str | None:
    """The area an item belongs to: its own area `own` (`fields.area`, `workflow.item_area`, pack sd:8010) when it has
    one, else `hoa` for the hoa checkout's, `home` for one with no repo, else None.

    A report is in no area's Tasks: the area's Reports view lists it by its job. Both paths have `~` expanded, as
    `realm_of` expands it, so a row stored as `~/...` against an absolute registered path is still HOA's (sd:7707).
    """
    if kind == "report":
        return None
    if own:
        return own
    if not repo:
        return "home"
    return "hoa" if hoa is not None and os.path.expanduser(repo) == os.path.expanduser(hoa) else None


def series_job(series: str) -> str:
    """The job name a brief series spells: lower case, runs of spaces as one hyphen."""
    return re.sub(r"\s+", "-", series.strip().lower())


def _hoa(connection) -> tuple[str | None, str]:
    """The hoa checkout's recorded path, and why there is none."""
    from .hoa_screen import checkout

    recorded, _, problems = checkout(connection)
    if problems:
        return None, problems[0]
    return recorded, "" if recorded else "no repo named hoa is registered"


def _place(connection, area: str):
    """`owns(repo, kind, own=None)`, whether a row is the place's, or None; why it is None; and whether that is a read
    that failed. `own` is an item's own area (pack sd:8010): HOA and Home are `area_of`'s, so it puts the item there
    alone; any other area is a realm of `realms`, and owns what `realm_of` gives a row with no area of its own."""
    if area == "home":
        return lambda repo, kind, own=None: area_of(repo, kind, None, own) == "home", "", False
    if area == "hoa":
        hoa, why = _hoa(connection)
        return None if hoa is None else (lambda repo, kind, own=None: area_of(repo, kind, hoa, own) == "hoa"), why, False
    found = realms(connection)
    if found["state"] == "error":
        return None, f"realms {found['problems'][0]}", True
    realm = next((r for r in found["list"] if r["id"] == area), None)
    if realm is None:
        line = f"add realm|{area}|<label>|<glob> to {REALMS_KEY} on Settings"
        named = "no realms are set" if found["state"] == "missing" else f"{found['source']} names no realm {area}"
        return None, f"not configured: {named}; {line}", False
    hoa = _hoa(connection)[0]
    return lambda repo, kind, own=None: not own and realm_of(repo, kind, found["list"], hoa) == area, "", False


def _family(connection, area: str, config: Path | None) -> tuple[list[str] | None, str, bool]:
    """The jobs of the area's report family, why there are none, and whether the family list did not read (unknown)."""
    from .reports_screen import FAMILIES_KEY, families

    found = families(connection, config)
    label = AREAS.get(area) or next((r["label"] for r in realms(connection)["list"] if r["id"] == area), "<label>")
    line = f"add family|{area}|{label}|<icon>|<job>,<job>,... to {FAMILIES_KEY} on Settings"
    if found["state"] == "missing":
        return None, f"not configured: no report families are set; {line}", False
    if found["state"] == "error":
        return None, f"report families {found['problems'][0]}", True
    family = next((f for f in found["list"] if f["key"] == area), None)
    if family is None:
        return None, f"not configured: {found['source']} names no family {area}; {line}", False
    return family["jobs"], "", False


def _unknown(now: str, area: str, reason: str, **more) -> dict:
    return {"read": now, "area": area, "state": "unknown", "reason": reason, "rows": [], **more}


def task_rows(connection, owns, *, now: str) -> tuple[list[dict], list[dict]]:
    """The open rows and the week's done rows `owns(repo, kind, own)` places, as the Tasks page's rows
    (`tasks_screen._rows`); `own` is the row's own area (`workflow.item_area`, pack sd:8010).

    Both are `reads.backlog_items`' unparked rows, every repo's, since an item's own area places it whatever its repo;
    so done is bounded as on Tasks: done within
    the week. A scheduled occurrence waits until it opens (sd:5670). The caller holds the snapshot. sd:5752: the done
    rows are a place's All view, and a row Done moved there stays a command object, so its Undo finds it.
    """
    from sd_db.workflow import item_area

    from .tasks_screen import _rows, is_scheduled

    found = [row for row in reads.backlog_items(connection, now=now)
             if not is_scheduled(row) and owns(row["repo"], row["kind"], item_area(row))]
    rows = _rows(connection, found, realms(connection), now=now)
    return [row for row in rows if row["status"] != "done"], [row for row in rows if row["status"] == "done"]


def tasks(connection, area: str, *, now: str) -> dict:
    """The area's open items in `rows`, and the week's done ones in `done` (`task_rows`): `area_of` (or a realm's
    `realm_of`) places each in it.

    Each is the Tasks page's row (`tasks_screen._rows`), read in one snapshot as `/api/tasks` is: a row's Details on the
    place page are the Tasks page's own (sd:5635), with its revision, moves, edit capability and issue targets, and `lead`
    says whether the store takes a lead, as `/api/tasks` does.
    """
    owns, why, unread = _place(connection, area)
    if owns is None:
        return {**_unknown(now, area, why, done=[]), **({"state": "error"} if unread else {})}
    from .operations_screen import _one_snapshot
    from .tasks_screen import _has_lead

    with _one_snapshot(connection):
        rows, done = task_rows(connection, owns, now=now)
        lead = _has_lead(connection)
        more = {} if area in AREAS else realm_issues(connection, owns, now=now)
    return {"read": now, "area": area, "state": "read", "reason": "", "rows": rows, "done": done, "lead": lead, **more}


def realm_issues(connection, owns, *, now: str) -> dict:
    """sd:6164: a realm place's issues: the open Your issues rows `/api/github/issues` reads (`issues.with_github`), each
    with its `tracker`, whose repo the realm owns. A GitHub row outside the issue sources has no `repo_path`, so its
    registered checkout stands in; an upstream one has neither and stays off. `hidden` is issue Hide's (sd:6175);
    `issues_reason` names a source that did not read, and `issues_store` whether Hide's store did."""
    from . import issues, today_github
    from .documents_screen import views

    github = today_github.document(connection, now=now)
    found = issues.with_github(issues.document(connection), github)
    checkout = {f"github:{e['ref']}".lower(): e.get("checkout") for e in (*github["issues"], *github["settled"])}
    rows = [row for row in found["issues"] if str(row.get("state") or "open").lower() == "open"
            and owns(row["repo_path"] or checkout.get(str(row["key"]).lower()), "issue")]
    kept, store = views(connection)
    for row in rows:
        row["hidden"] = bool(kept.get(f"issue:{str(row['key']).lower()}", {}).get("hidden"))
    reason = "; ".join(why for why in (found["issues_error"], found.get("github_error", "")) if why)
    return {"issues": rows, "issues_reason": reason, "issues_store": {"available": not store, "reason": store}}


#: How many briefs are read before the area filter: every one the store holds, in practice; a page shows `ROWS`.
BRIEF_SCAN = 100_000


def briefs(connection, area: str, *, now: str, config: Path | None = None) -> dict:
    """The area's briefs, in the Briefs page's row shape (`briefs_screen.document`), with its watchdog for their Details.

    Each row carries Today's Seen mark (sd:5752): `mark` the Today row id it is snoozed under, `seen` its fingerprint, and
    `marked` whether that snooze holds. A snooze read that fails marks nothing and says so in `reason`.
    """
    from . import briefs_screen, snooze, today_reports

    jobs, unconfigured, unread = _family(connection, area, config)
    if unread:
        # The checkout's briefs could still be listed, but which series are the area's is unknown, not "none".
        return {**_unknown(now, area, unconfigured), "state": "error"}
    owns, why, unread = _place(connection, area)
    if unread:
        # The series briefs could still be listed, but which repos are the realm's is unknown, not "none".
        return {**_unknown(now, area, why), "state": "error"}
    if area == "home":
        owns = None  # a brief always names a repo, and Home's rows have none: Home holds only its series' briefs
    if jobs is None and owns is None:
        return _unknown(now, area, "; ".join(reason for reason in (why, unconfigured) if reason))
    wanted = set(jobs or ())

    def reader(conn):
        got = briefs_screen._library().rows(conn, limit=BRIEF_SCAN)
        return [raw for raw in got if (owns is not None and raw.get("repo") and owns(raw["repo"], "brief"))
                or series_job(str((raw.get("brief") or {}).get("series") or "")) in wanted][:briefs_screen.ROWS + 1]

    doc = briefs_screen.document(connection, now=now, reader=reader)
    state = "error" if doc["reader"]["state"] == "error" else "read"
    held, unheld = snooze.held(connection, now=now)
    for row in doc["briefs"]:
        mark = today_reports.seen_row(row)
        row["mark"], row["seen"] = mark["id"], snooze.fingerprint(mark)
        row["marked"] = held.get(snooze.key("today", mark["id"]), {}).get("seen") == row["seen"]
    # A family not configured still leaves the checkout's briefs; the reason says what else would be read.
    reason = [doc["reader"]["reason"] or unconfigured or why, unheld and f"Seen marks were not read: {unheld}. Every brief shows as open."]
    return {"read": now, "area": area, "state": state, "reason": " ".join(part for part in reason if part),
            "rows": doc["briefs"], "reader": doc["reader"], "watchdog": doc["watchdog"]}


def reports(connection, area: str, *, now: str, jobs, config: Path | None = None) -> dict:
    """The reports of the area's family jobs, from the newest `reporting.reports` the Reports page reads.

    `details` is the Reports page's document (`reports_screen.document`) for those jobs alone, read through `jobs`, the
    operations backend: a row's Details on the place page are the Reports page's own (sd:5636), with the diff, the job's
    cadence and its commands.
    """
    from . import reports_screen

    names, unconfigured, unread = _family(connection, area, config)
    if names is None:
        return {**_unknown(now, area, unconfigured, jobs=[]), **({"state": "error"} if unread else {})}
    doc = reports_screen.document(connection, now=now, jobs=jobs, config=config, only=frozenset(names))
    if doc["reports"] is None:
        return {**_unknown(now, area, f"reports not read: {doc['sources']['reports']}", jobs=names), "state": "error"}
    keep = ("id", "title", "status", "at", "job", "attention", "exit", "ended", "followups")
    rows = [{key: row[key] for key in keep} for row in doc["reports"]]
    # A jobs or cadence read that failed leaves the rows; the view names it, so Details without a job's bar is not "no job".
    failed = [f"{source} ({why})" for source, why in doc["sources"].items() if why]
    return {"read": now, "area": area, "state": "read", "reason": f"Not read: {', '.join(failed)}." if failed else "", "rows": rows,
            "jobs": names, "limit": 200, "details": doc}
