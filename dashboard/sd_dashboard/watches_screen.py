"""Watches (sd:6264): the company and product watches, the store's market rows, on their own page.

The page is `v2/watches.html`; it holds no rows. It reads one JSON document, `/api/watches`, built here:

- `rows`: every `sdw.market` row (`records.list_rows(connection, "market")`). Each has its title, its status (the
  row's stage: candidate, active, paused or declined), its kind (`content-type`), area and description, the first
  http(s) link in its `## Sources` section (`url_text` instead when it does not parse), and its last refresh: the newest `Last full refresh: YYYY-MM-DD` the
  market-watch routine writes into the body (`refreshed`, "" for none) and its age in days.
- `fresh` says what that date is worth: `fresh` (active, refreshed within `STALE_DAYS`), `stale` (active, older),
  `never` (active, no refresh date: no profile yet) or `idle` (not active: the routine refreshes active watches only).
  A date is never shown as fresh past `STALE_DAYS`, whatever the job's last run said.
- `job`: the market-watch job (`JOB`) as HOA reads its refresh jobs (`hoa_screen.JOB_KEYS`): its launchd state,
  revision and Run now capability, its last run's stamp (`management_screen.last_run`) and the last line its run
  printed (`said`), which is why a failed run failed. A job that is not installed is `{"name", "error"}`.

Each row carries its `revision`: Activate, Pause and Unpause post it to `/api/items/<n>/stage` (sd:6694).

Nothing here writes. Refresh all posts `/api/jobs/market-watch/retry`; Refresh and Add watch post `/api/actions/run`
(`watch.refresh`, `watch.add` in `run_actions`). A store that does not read raises, so the route answers 500 with the
cause and the page says the watches were not read.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .learning_screen import _url

__all__ = ["JOB", "STALE_DAYS", "document"]

#: The job that refreshes the watches, three a run, daily at 03:00 (its `.job` file is private config).
JOB = "market-watch"
#: Days after which an active watch's refresh is stale. Three a run, daily: about 35 active watches take 12 days a cycle.
# ponytail: a fixed bound; derive it from the active count if the roster outgrows it.
STALE_DAYS = 14
REFRESHED = re.compile(r"Last full refresh:\s*(\d{4}-\d{2}-\d{2})")
LINK = re.compile(r"https?://[^\s<>()\]`*]+")


def _text(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def _refreshed(body: str) -> str:
    """The newest refresh date in the body: an imported profile keeps an older lead-in above `## What it is`."""
    found = []
    for text in REFRESHED.findall(body):
        try:
            found.append(date.fromisoformat(text))
        except ValueError:
            continue
    return max(found).isoformat() if found else ""


def _source(body: str) -> str:
    """The first http(s) link's text in the `## Sources` section, or ""; `_url` says whether it parses."""
    section = re.search(r"^## Sources\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    found = LINK.search(section.group(1)) if section else None
    if not found:
        return ""
    return found.group(0).rstrip(".,;:")


def _row(row, today: date, revision: str) -> dict:
    fields, body = row["fields"], row.get("body") or ""
    stage = row.get("stage") or ""
    refreshed = _refreshed(body)
    age = (today - date.fromisoformat(refreshed)).days if refreshed else None
    fresh = ("idle" if stage != "active" else "never" if age is None else "stale" if age > STALE_DAYS else "fresh")
    text = _source(body)
    url, host = _url(text)
    return {"id": row["id"], "title": row["title"], "stage": stage, "kind": _text(fields.get("content-type")),
            "area": _text(fields.get("area")), "slug": _text(fields.get("slug")), "description": _text(fields.get("description")),
            "added": _text(fields.get("dateCreated"))[:10], "url": url, "host": host, "url_text": "" if url else text,
            "refreshed": refreshed, "age": age, "fresh": fresh, "revision": revision}


def _job(connection, backend) -> dict:
    from sd_db import operations, workflow

    from .hoa_screen import JOB_KEYS, _said
    from .management_screen import job_log, last_run

    backend = backend or operations.LaunchdBackend()
    try:
        job = operations.job_state(connection, JOB, backend=backend)
    except workflow.WorkflowError as error:
        return {"name": JOB, "error": f"{JOB} is not an installed job ({error}); local-cron-jobs/cron-jobs.sh install {JOB} installs it"}
    root = getattr(backend, "cron_root", None)
    return {key: job.get(key) for key in JOB_KEYS} | {"last_run": last_run(root, JOB), "said": _said(job_log(root, JOB), JOB)}


def document(connection, *, now: str, jobs=None) -> dict:
    """`/api/watches`: the market rows in the store's order, the page sorts them, and the market-watch job. `pool` and
    `declined` are the candidates' bulk Decline and each declined row's when and by (sd:8014)."""
    from sd_db import reads, records

    from . import triage

    today = datetime.fromisoformat(now.replace("Z", "+00:00")).date()
    rows = records.list_rows(connection, "market")
    revisions = reads.revisions(connection, [row["id"] for row in rows]) if rows else {}
    return {"read": now, "today": today.isoformat(), "stale_days": STALE_DAYS,
            "rows": [_row(row, today, revisions[row["id"]]) for row in rows], "job": _job(connection, jobs),
            "pool": triage.pool(connection, "market", now=now), "declined": triage.declined(connection, "market")}
