"""Metrics: the readings behind the default UI's Metrics page (sd:2119).

The page is `v2/metrics.html`; it holds no data. It reads one JSON document,
`/api/metrics`, built here by `document` from the reads the retired Usage and
Progress areas made:

- `usage`: the month as `sd usage --json` prints it (`usage.read`), so the
  page and the CLI cannot disagree about a dollar (requirement 6). Dollars are
  the month to date, not a week: a week of spend would be a second computation
  of the same ledger. `?month=` names another month (pack sd:7524).
- `trend`: the last 7-day-window meter reading of each day for the last week
  (`reads.meter_days`).
- `numbers`: the week's four numbers (`reads.weekly_numbers`).
- `scorecard`: the providers this month (`reads.scorecard`).
- `age`: open items by days in their current status (`reads.age_histogram`).
- `observed`: observed workflow activity (`reporting.metrics`).
- `ci`: the lane's gate runs over the page's range (`CI_DAYS`), per repository (`_ci`, sd:5473). The flaky heads
  moved to Today's Needs you (`flaky`, pack sd:7663).
- `lanes`: each managed repository's lane over the last 7 days (`times`, pack sd:6006, moved from Queue in pack
  sd:7233): the median wait from enqueue to claim, the median gate time (the speculative gate plus the prepare step)
  and the slowest entry, enqueue to end, read from each entry's history rows by the pack's `sd_lane.lane_times`. A
  median wait past the 30 min target (pack sd:7412) is `over`: the lane runner files the parallel-lanes question
  then. The range leaves it at 7 days, as it leaves Age.
- `prs`: pull-request waits from each item's PR lane notes (`_prs`, pack sd:7859): the time on you and on reviewers,
  the median first PR to merge, and the same per repository with its pull requests. Only the operator's count:
  authored or asked to review by name (sd:8084). All time, whatever the range.
- `controls`: the provider registry as `provider_controls.snapshot` reads it, with its `revision`, and per bill the
  one cap write the route takes (`_writes`, sd:3387).
- `range`: the page's range, `7d` or `6mo` (`RANGES`, pack sd:7208). With `6mo`, `ranged` holds the parts the range
  changes, read over the last six calendar months, this one to date (`_ranged`): `spend`, dollars by bill per month,
  one `usage.read` a month; `scorecard`, the providers' six months; `ci`, the gate runs since the first month began.
  The parts above stay as `7d` reads them, so the lamps and the rail badge do not move with the range.

`usage` and `numbers` take seconds (`SLOW`): the served dashboard keeps them in a snapshot, and each then carries
`observed`, when it was read, and `stale`, why its newest read failed (sd:4955). A stale month lights no lamp.

Each part is guarded on its own: one that raises is that part's `error`, and
the others still answer. `unread` names the design's panels no reader covers,
with the reason, so the page shows them as unknown and never as empty.
Nothing here writes or reaches the network. The page's Cap and Configure post `controls.revision` to
`/api/bills/<bill>/cap` and `/api/providers/configure` (`server.action_route`), whose library refuses it once the
registry, a bill's cap or the order policy changed since this read.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import statistics
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sd_db import provider_controls, reads, registry, reporting, usage
from sd_db.errors import SdDbError

from . import pack_module
from .queue_screen import READ_FAILURES
from .tasks_screen import is_scheduled

__all__ = ["BILL_NAME", "CI_DAYS", "GATE_CODES", "GATE_FAILED", "HEALTH_DAYS", "MONTHS", "RANGES", "SLOT_WAIT", "SLOW", "TREND_DAYS", "UNREAD", "WINDOW",
           "document", "flaky", "health", "slow", "times"]

#: A bill name the cap route (`/api/bills/<name>/cap`, `server.action_route`) accepts in its path. The registry puts
#: no rule on a bill name (`registry.parse` takes every key of the `bills` mapping), so the bound is this route's own
#: and the services route's, character for character, 200 at most (#435's verification round). `_writes` offers a
#: write only for a name it matches, so no control posts to a path that answers 404. Moved here from the retired
#: Usage area (pack sd:7524).
BILL_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}"

#: The vendor window the trend draws: seven days, in minutes.
WINDOW = 10080

#: Days of window readings the trend carries, ending today.
TREND_DAYS = 7

#: The design's panels no reader covers, and why; the page draws each as unknown with its reason.
UNREAD = (
    {"id": "model", "name": "By model", "reason": "the cost ledger records bill and provider, not model; judgment.model covers Jev calls only"},
)

#: Days of lane runs CI health and the flaky heads read, ending now, at the page's default range, 7d.
CI_DAYS = TREND_DAYS

#: The page's live ranges (pack sd:7208): 7d, the default, and the last `MONTHS` calendar months, this one to date.
RANGES = ("7d", "6mo")
MONTHS = 6

#: A failed lane entry whose `reason` says sd-ship's gate refusal: prepare's "the repository gate failed …"
#: (`sd_ship_review`) or merge's "repo.ci is local and sd/local-gate is failure on …" (`sd_local_gate`). The lane keeps
#: the refusal's text; its `code` is mostly empty, so the codes count only when present.
GATE_FAILED = re.compile(r"\bthe repository gate failed\b|\bsd/local-gate is failure on\b")
GATE_CODES = frozenset({"gate_failed", "ci_not_passing"})
#: sd-ship's refusal when the gate's slot wait ran out (pack sd:7412). Its text says the gate failed, but no check ran,
#: so the entry is no gate run; the lane puts such an entry back, and only a lane before it failed one.
SLOT_WAIT = "gate_slot_wait"

#: An entry the gate passed: the lane merged it (merge refuses a failed `sd/local-gate`) or prepare cleared it.
PASSED = ("merged", "prepared")

FAILURES = (SdDbError, sqlite3.Error, OSError, ValueError)

#: The parts that take seconds, which the served dashboard keeps in a `snapshot.Kept` (sd:4955).
SLOW = ("usage", "numbers")


def _stamp(now: str) -> datetime:
    return datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc)


def _guard(read):
    try:
        return read()
    except FAILURES as error:
        return {"error": f"{error.__class__.__name__}: {error}"}


def _attention(spend: dict, trend: dict) -> dict:
    """The rail badge (sd:3446): the lamps metrics.js can light, each week window at 75 % or more and the estimated
    spend rows. A flaky head lights Today's Lane lamp, not this badge (pack sd:7663). An unread usage is unknown: no lamp
    could be read."""
    from .v2.pages import attention

    if spend["error"] or spend.get("stale"):
        return {"state": "unknown", "n": 0, "what": "readings not read"}

    def level(used):
        number = isinstance(used, (int, float)) and not isinstance(used, bool)
        return "warning" if number and used >= 100 else "caution" if number and used >= 75 else ""

    window = trend.get("window")
    cells = [level(meter["used_percent"]) for meter in spend["meter"] if meter["window_minutes"] == window]
    return attention([*cells, "caution" if spend["bound"] else ""], {"caution": "readings past a threshold"})


def _trend(connection, today):
    since = today - timedelta(days=TREND_DAYS - 1)
    days = [(since + timedelta(days=n)).isoformat() for n in range(TREND_DAYS)]
    series = {}
    for row in reads.meter_days(connection, window_minutes=WINDOW, since=since.isoformat()):
        series.setdefault(row["provider"], {"provider": row["provider"], "bill": row["bill"], "points": []})["points"].append(
            [row["day"], row["used_percent"], row["timestamp"]])
    return {"error": "", "window": WINDOW, "days": days, "series": list(series.values())}


def _numbers(connection, now):
    return {"error": "", "rows": [{"key": n.key, "label": n.label, "value": n.value, "unit": n.unit, "inputs": [list(i) for i in n.inputs]}
                                  for n in reads.weekly_numbers(connection, now=now)]}


def _scorecard_rows(rows):
    return {"error": "", "rows": [{"provider": r.provider, "enabled": r.enabled, "reason": r.reason or "", "author_rank": r.author_rank,
                                   "reviewer_rank": r.reviewer_rank, "passes": r.passes, "blocking": r.blocking, "usd": r.usd,
                                   "usd_per_pass": r.usd_per_pass, "fallthrough": r.fallthrough} for r in rows]}


def _scorecard(connection, now):
    return _scorecard_rows(reads.scorecard(connection, now=now))


def _age(connection, now):
    # pack sd:6743: as Tasks, which the bars open, it counts no idea kind.
    rows = [row for row in reads.backlog_items(connection, now=now)
            if row["status"] != "done" and not is_scheduled(row) and row["kind"] not in reads.IDEA_KINDS]
    return {"error": "", "buckets": [{"key": b.key, "label": b.label, "lower": b.lower, "upper": b.upper, "ready_to_send": b.counts["ready_to_send"],
                                      "other": b.counts["other"]} for b in reads.age_histogram(rows, now=now)]}


def _failure(entry: dict) -> str:
    """What stopped a failed lane entry: `gate`, `review` (it stopped before the gate ran), `other`, or `unknown`."""
    reason = entry.get("reason")
    if entry.get("code") == SLOT_WAIT:
        return "other"
    if entry.get("code") in GATE_CODES or (isinstance(reason, str) and GATE_FAILED.search(reason)):
        return "gate"
    if not isinstance(reason, str) or not reason.strip():
        return "unknown"
    return "review" if re.search(r"\breview", reason) else "other"


def _ended(row) -> dict | None:
    """The body of a lane entry that passed or failed, None for one that has not ended or ran no gate (cancelled,
    skipped, handed back); ValueError for a body that is not a lane entry or names no end time."""
    entry = json.loads(row["body"]) if isinstance(row["body"], str) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("repository"), str):
        raise ValueError(row["key"])
    if entry.get("status") not in (*PASSED, "failed"):
        return None
    if not isinstance(entry.get("finished_at"), str):
        raise ValueError(row["key"])
    _stamp(entry["finished_at"])
    return entry


def _written(row, since: str) -> bool:
    """The entry's row was written at or after `since`; a stamp that does not parse counts, since its age is unknown."""
    try:
        return _stamp(str(row["timestamp"])) >= _stamp(since)
    except ValueError:
        return True


def _entries(connection, since: str) -> tuple[list[tuple[str, dict]], int]:
    """The lane entries that ended, oldest first, each with its end as `%Y-%m-%dT%H:%M:%SZ`, and the count of bodies that
    are not lane entries (`unreadable`) written since `since` (pack sd:7734): an older bad body is out of the range. One
    entry runs once: the lane queues a retry as a new entry."""
    ended, unreadable = [], 0
    for row in reads.lane_entries(connection):
        try:
            entry = _ended(row)
        except ValueError:
            unreadable += _written(row, since)
            continue
        if entry:
            ended.append((_stamp(entry["finished_at"]).strftime("%Y-%m-%dT%H:%M:%SZ"), entry))
    ended.sort(key=lambda pair: pair[0])
    return ended, unreadable


def _ci(connection, now, since=None):
    """CI health from the lane's entries (sd:5473): the latest checkpoint of each, as `reads.lane_entries` reads it.

    A run is an entry that ended in the range: passed (`PASSED`) or failed, the failure split by `_failure`. The rate is
    passed over gate runs, passed plus `gate`; a review stop or a conflict ran no gate. A body in the range that is not
    a lane entry is `unreadable`. The range is `CI_DAYS` ending now, or from `since`; `days` counts it in whole days. The flaky heads
    moved to Today's Needs you (`flaky`, pack sd:7663).
    """
    days = CI_DAYS if since is None else math.ceil((_stamp(now) - _stamp(since)).total_seconds() / 86400)
    since = since or (_stamp(now) - timedelta(days=CI_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ended, unreadable = _entries(connection, since)
    repos: dict[str, dict] = {}
    for at, entry in ended:
        if at < since:
            continue
        row = repos.setdefault(entry["repository"], {"repository": entry["repository"], "passed": 0, "gate": 0, "review": 0,
                                                     "other": 0, "unknown": 0, "newest": None, "newest_gate": None})
        if entry["status"] in PASSED:
            row["passed"] += 1
            continue
        kind = _failure(entry)
        row[kind] += 1
        said = {"item": entry.get("item"), "title": entry.get("title") or "", "at": at, "kind": kind,
                "step": entry.get("step") or "", "reason": str(entry.get("reason") or "")[:240]}
        row["newest"] = said
        if kind == "gate":
            row["newest_gate"] = said
    for row in repos.values():
        runs = row["passed"] + row["gate"]
        row["rate"] = row["passed"] / runs if runs else None
    return {"error": "", "days": days, "since": since, "unreadable": unreadable,
            "repos": sorted(repos.values(), key=lambda r: (-r["gate"], r["repository"]))}


def _run(at: str, entry: dict) -> dict:
    """One lane run as the flaky Fix brief names it: its entry id, end, status and prepare log with the log's host."""
    return {"entry": entry.get("id"), "at": at, "status": entry["status"], "log": entry.get("prepare_log") or "",
            "host": entry.get("log_host") or ""}


def flaky(connection, now: str) -> tuple[list[dict], int]:
    """The flaky heads of the last `CI_DAYS` (sd:5473), newest pass first, and the count of bodies that are not lane
    entries (`_entries`' `unreadable`); Today lists them (pack sd:7663) and says the count (pack sd:7706).

    A flaky head is one a gate failed at, and a later entry of the same repository passed at that same head: `head`, or
    `expected_head` where the failed entry names none, so a catch-up merge or a new commit is a new head. The pass falls
    in the range; the failures may be older. A read that fails raises: the caller says it, never an empty week.
    """
    since = (_stamp(now) - timedelta(days=CI_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    failed: dict[tuple, list] = {}
    found = {}
    ended, unreadable = _entries(connection, since)
    for at, entry in ended:
        head = entry.get("head") or entry.get("expected_head")
        if not isinstance(head, str):
            continue
        key = (entry["repository"], head)
        if entry["status"] == "failed" and _failure(entry) == "gate":
            failed.setdefault(key, []).append((at, entry))
        elif entry["status"] in PASSED and failed.get(key) and key not in found and at >= since:
            # pack sd:8113: each run's entry id and prepare log, for the Fix brief that names them.
            found[key] = {"repository": entry["repository"], "item": entry.get("item"), "title": entry.get("title") or "",
                          "head": head, "failed": [when for when, _ in failed[key]], "passed": at, "status": entry["status"],
                          "runs": [_run(when, run) for when, run in failed[key]], "pass_run": _run(at, entry)}
    return sorted(found.values(), key=lambda f: f["passed"], reverse=True), unreadable


#: The weekly KISS audit's window, with the window that ended a week ago beside it (pack sd:6744).
HEALTH_DAYS = 14

#: What names a failed suite in a gate refusal: sd-check's `shard <module>: 46s exit=1`, or a `make target <name>`.
SUITE = re.compile(r"\bshard ([\w./-]+): \S+ exit=[1-9]|\bmake target ([\w./-]+)")

#: A build run's reap detail opens with its id and step, then says why: `_reason` keeps the step and the why.
RUN_PREFIX = re.compile(r"^build run \w+ ended at step ")

#: What sets one reap reason apart from another of the same kind: a head or a count.
VARYING = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b|\d+")


def _reason(detail: str | None, outcome: str) -> str:
    """A build run's reap reason, grouped: its step and the why's first clause, with heads and counts as N."""
    if not detail:
        return outcome
    step, _, why = RUN_PREFIX.sub("", detail).partition(": ") if RUN_PREFIX.match(detail) else ("", "", detail)
    why = re.split(r"[;:\n]", why, maxsplit=1)[0]
    return VARYING.sub("N", f"{step}: {why}" if step else why).strip()[:120]


def _health_window(entries: list[tuple[str, dict]], runs: list, since: str, until: str) -> dict:
    repos: dict[str, dict] = {}
    suites: Counter = Counter()
    lead: list[float] = []
    for at, entry in entries:
        if not since <= at <= until:
            continue
        row = repos.setdefault(entry["repository"], {"repository": entry["repository"], "entries": 0, "review": 0,
                                                     "gate": 0, "other": 0})
        row["entries"] += 1
        if entry["status"] in PASSED:
            if entry["status"] == "merged" and "enqueued" in entry:
                lead.append((_stamp(at) - entry["enqueued"]).total_seconds() / 60)
            continue
        kind = _failure(entry) if entry["status"] == "failed" else "other"
        row["review" if kind == "review" else "gate" if kind == "gate" else "other"] += 1
        if kind == "gate":
            named = {shard or target for shard, target in SUITE.findall(str(entry.get("reason") or ""))}
            suites.update((entry["repository"], name) for name in named or {"unnamed"})
    for row in repos.values():
        row["failed_percent"] = round(100 * (row["review"] + row["gate"] + row["other"]) / row["entries"])
    ended = [run for run in runs if run["outcome"] and since <= _stamp(run["updated_at"]).strftime("%Y-%m-%dT%H:%M:%SZ") <= until]
    lead.sort()
    return {
        "since": since, "until": until,
        "lane": sorted(repos.values(), key=lambda r: (-r["entries"], r["repository"])),
        "gate_suites": [{"repository": repo, "suite": name, "failures": count}
                        for (repo, name), count in suites.most_common(6)],
        "builds": {"done": sum(run["outcome"] == "done" for run in ended), "total": len(ended),
                   "reasons": [{"reason": reason, "runs": count} for reason, count in Counter(
                       _reason(run["detail"], run["outcome"]) for run in ended if run["outcome"] != "done").most_common()]},
        "lead_minutes": {"merged": len(lead), "median": round(statistics.median(lead)) if lead else None,
                         "p90": round(lead[math.ceil(0.9 * len(lead)) - 1]) if lead else None},
    }


def health(connection, *, now: str) -> dict:
    """The weekly KISS audit's four numbers (pack sd:6744), over `HEALTH_DAYS` ending now and ending a week ago.

    - `lane`: each repository's lane entries that ended, and how many failed, by class: `review`, `gate`, and `other`
      (a conflict, an uncommitted tree, a cancel, a skip or a hand-back), with the failed percent.
    - `gate_suites`: the six suites, each in its repository, that gate failures named most (`SUITE`); a refusal naming none is `unnamed`.
    - `builds`: build runs that ended, done over all, and the reap reasons of the rest (`_reason`).
    - `lead_minutes`: enqueue to merge, the median and p90 over merged entries.

    An entry that ran no lane step (pending, running) is not counted; a body that is not a lane entry, or whose
    `finished_at` or `enqueued_at` is no time, is `unreadable` (pack sd:7005). A build run counts in the window it
    ended in: its `updated_at`.
    """
    entries, unreadable = [], 0
    for row in reads.lane_entries(connection):
        try:
            entry = json.loads(row["body"]) if isinstance(row["body"], str) else None
            if not isinstance(entry, dict) or not isinstance(entry.get("repository"), str):
                raise ValueError(row["key"])
            if entry.get("status") in ("pending", "running", None):
                continue
            finished = _stamp(entry["finished_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")
            if entry["status"] == "merged" and isinstance(entry.get("enqueued_at"), str):
                entry = {**entry, "enqueued": _stamp(entry["enqueued_at"])}
            entries.append((finished, entry))
        except (ValueError, KeyError, TypeError, AttributeError):
            unreadable += 1
    runs = reads.build_runs(connection)
    stamp, day = _stamp(now), timedelta(days=1)

    def window(end):
        return _health_window(entries, runs, (end - HEALTH_DAYS * day).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return {"days": HEALTH_DAYS, "unreadable": unreadable, "now": window(stamp), "last_week": window(stamp - 7 * day)}


def _writes(name, spawned, held):
    """The cap write a bill takes, and why not when it takes none: `set`, `clear` or `""`.

    A bill a `start` entry is billed to takes no cap (`set_cap` refuses it with the registry's sentence), except the
    clear of a legacy row cap the merged view hides, the repair `set_cap(..., None)` makes. A name outside `BILL_NAME`
    has no route. Operations Usage's cap forms had the same rule; they retired in pack sd:7524.
    """
    if not re.fullmatch(BILL_NAME, name):
        return "", "its name is outside what the cap route accepts"
    if name in spawned and held.get(name) is not None:
        return "clear", f"the row holds a cap of ${held[name]:,.2f} that nothing enforces; clearing it is the one write this bill takes"
    if name in spawned:
        return "", "a 'start' entry is billed to it, so no cap can be set"
    return "set", ""


def _controls(connection):
    current = provider_controls.snapshot(connection)
    spawned = {entry.bill for entry in registry.read(connection=connection).providers.values() if entry.kind == "start"}
    held = {row["name"]: row["cap_usd_month"] for row in reads.cost_by_bill(connection)}
    bills = []
    for bill in current["bills"]:
        write, why = _writes(bill["name"], spawned, held)
        bills.append({**bill, "write": write, "why": why})
    return {"error": "", "revision": current["revision"], "orders": current["orders"], "warnings": current["warnings"], "bills": bills,
            "providers": [{key: entry[key] for key in ("name", "vendor", "enabled", "reason", "roles")} for entry in current["providers"]]}


def _months(today) -> list[str]:
    """The last `MONTHS` calendar months as `YYYY-MM`, oldest first, ending with today's."""
    at = today.year * 12 + today.month - 1
    return [f"{n // 12:04d}-{n % 12 + 1:02d}" for n in range(at - MONTHS + 1, at + 1)]


def _spend_months(connection, now, months):
    """Dollars by bill for each month, one `usage.read` a month, as `sd-db usage --month` reads it.

    `unbilled` and `spent` are the read's own: the dollars on rows naming no listed bill, and every bill's with them
    (pack sd:7219). `rows` is whether the month has a run or bound row: a bill's burn point or `unbilled`, both the
    month's own. A month without either is no rows, not $0. The roles are not asked: they carry every open hold
    whatever its month (pack sd:7229).
    """
    found, bills = [], {}
    for month in months:
        read = usage.read(connection, month=month, now=now).document()
        for bill in read["bills"]:
            bills.setdefault(bill["name"], bill["cost_basis"])
        found.append({"month": month, "rows": read["unbilled"] is not None or any(bill["burn"] for bill in read["bills"]),
                      "spent": read["spent"], "unbilled": read["unbilled"],
                      "bills": {bill["name"]: bill["spent"] for bill in read["bills"]}})
    return {"error": "", "bills": [{"name": name, "cost_basis": basis} for name, basis in bills.items()], "months": found}


def _scorecard_months(connection, months):
    """The provider scorecard summed over the months, one `reads.scorecard` a month; state and ranks are the provider's now."""
    summed: dict = {}
    for month in months:
        for row in reads.scorecard(connection, now=f"{month}-01T00:00:00Z"):
            held = summed.get(row.provider)
            summed[row.provider] = row if held is None else replace(
                held, passes=held.passes + row.passes, blocking=held.blocking + row.blocking, usd=held.usd + row.usd,
                fallthrough=held.fallthrough + row.fallthrough)
    return _scorecard_rows(summed.values())


def times(connection, remote: str | None, now: float) -> dict:
    """One lane's last `sd_lane.TIMES_DAYS`: its median wait and gate time and its slowest entry (pack sd:6006).

    Read through the pack's own `sd_lane.lane_times` on the request's connection, from each entry's history rows. A
    read that failed, or a remote that names no GitHub repository, is `{"error": ...}`, never a quiet lane.
    """
    from sd_db.protection import github_slug

    found = github_slug(remote)
    if found is None:
        return {"error": "its remote names no GitHub repository, so it has no lane history"}
    try:
        read = pack_module("sd_lane").lane_times
    except (ImportError, AttributeError) as error:
        return {"error": f"the pack's sd_lane reads no lane times: {error}"}
    try:
        return read(connection, "/".join(found).lower(), now=now)
    except READ_FAILURES as error:
        return {"error": f"the lane times were not read: {type(error).__name__}: {error}"}


def _prs(connection, now) -> dict:
    """Pull-request waits (pack sd:7859): the totals on you and on reviewers and first PR to merge, then the same per
    repository with its pull requests, slowest first; repositories with the most wait first (sd:8084, note 48655).

    Read from each item's PR lane notes (`contributions.pr_waits`), all time: an open one counts to `now`. `to_merge`
    is the median of the merged ones, or None when none merged. Only the operator's pull requests count: authored,
    or asked to review by name (`contributions.operator_pull`, sd:8084). One not known either way, or an item whose
    metadata is malformed, is named in `unknown` and not counted.
    """
    from sd_db import contributions

    unknown: list[str] = []
    waits = contributions.pr_waits(connection, now=now, errors=unknown)
    unknown += [f"sd:{wait['item']}: no reading of its author and requested reviewers" for wait in waits if wait["mine"] is None]
    keys = ("item", "title", "url", "open", "on", "on_you", "on_reviewers", "to_merge")
    groups: dict[str, list[dict]] = {}
    for wait in (wait for wait in waits if wait["mine"]):
        groups.setdefault("/".join(wait["url"].split("/")[3:5]).lower(), []).append(wait)
    repos = sorted(({"repo": repo, **_wait_totals(rows), "prs": [{key: wait[key] for key in keys} for wait in sorted(
        rows, key=lambda wait: (-(wait["on_you"] + wait["on_reviewers"]), wait["item"]))]} for repo, rows in groups.items()),
        key=lambda group: (-(group["on_you"] + group["on_reviewers"]), group["repo"]))
    return {"error": "", **_wait_totals([wait for rows in groups.values() for wait in rows]), "unknown": unknown,
            "repos": repos}


def _wait_totals(waits: list[dict]) -> dict:
    merged = [wait["to_merge"] for wait in waits if wait["to_merge"] is not None]
    return {"items": len(waits), "open": sum(1 for wait in waits if wait["open"]),
            "on_you": sum(wait["on_you"] for wait in waits), "on_reviewers": sum(wait["on_reviewers"] for wait in waits),
            "merged": len(merged), "to_merge": statistics.median(merged) if merged else None}


def _lanes(connection, now) -> dict:
    """Each managed repository's lane times (pack sd:7233), by folder name: the history is in the database, so a lane
    needs no checkout here."""
    from sd_db import repos

    clock = _stamp(now).timestamp()
    managed = sorted((row for row in repos.registered(connection) if row["managed"]), key=lambda row: row["path"])
    return {"error": "", "lanes": [{"repo": os.path.basename(os.path.expanduser(row["path"]).rstrip("/")),
                                    "path": os.path.expanduser(row["path"]),
                                    "times": times(connection, row["remote"], clock)} for row in managed]}


def _ranged(connection, now, today):
    """The parts the 6mo range changes, each guarded on its own (pack sd:7208)."""
    months = _months(today)
    since = f"{months[0]}-01T00:00:00Z"
    return {"months": months, "since": since,
            "spend": _guard(lambda: _spend_months(connection, now, months)),
            "scorecard": _guard(lambda: _scorecard_months(connection, months)),
            "ci": _guard(lambda: _ci(connection, now, since=since))}


def slow(connection, name: str, *, now: str) -> dict:
    """One part of `SLOW`, read now; it raises what its read raised."""
    if name == "usage":
        return {"error": "", **usage.read(connection, month=None, now=now).document()}
    if name == "numbers":
        return _numbers(connection, now)
    raise ValueError(f"{name!r} is not a slow Metrics part")


def _past(connection, month, now, current) -> dict:
    """`month`'s read for the history panels, with this month's `meter`: the week lamps and Budget used, now stay live.
    An unread live meter is `meter_error`, not an empty list that reads as no reading. A stale kept meter keeps its
    `observed` and `stale`, so metrics.js lights no lamp from it (pack sd:7556)."""
    past = _guard(lambda: {"error": "", **usage.read(connection, month=month, now=now).document()})
    if past["error"]:
        return past
    flags = {key: current[key] for key in ("observed", "stale") if key in current} if current.get("stale") else {}
    return {**past, "meter": current.get("meter", []), **({"meter_error": current["error"]} if current["error"] else {}), **flags}


def document(connection, *, now: str, kept=None, span: str = "7d", month: str | None = None) -> dict:
    """The Metrics page's document: every reading, each guarded on its own, and the panels no reader covers.

    `kept(name)` answers a `SLOW` part from the server's snapshot, with `observed` and `stale`; without it they are
    read now. `span` is one of `RANGES`; `6mo` adds `ranged`, read now. `month`, `YYYY-MM`, makes `usage` that
    month's read, now, the picker Operations Usage had (pack sd:7524), but its `meter` stays this month's (`_past`);
    `attention` stays on this month's too, so the rail badge does not move with the month the page shows.
    """
    if span not in RANGES:
        raise ValueError(f"{span!r} is not a Metrics range: {', '.join(RANGES)}")
    today = _stamp(now).date()
    read_slow = kept or (lambda name: slow(connection, name, now=now))
    current, trend = _guard(lambda: read_slow("usage")), _guard(lambda: _trend(connection, today))
    spend = current if month is None else _past(connection, month, now, current)
    ranged = {"ranged": _ranged(connection, now, today)} if span == "6mo" else {}
    return {
        "read": now,
        "range": span,
        "attention": _attention(current, trend),
        "usage": spend,
        "trend": trend,
        "numbers": _guard(lambda: read_slow("numbers")),
        "scorecard": _guard(lambda: _scorecard(connection, now)),
        "age": _guard(lambda: _age(connection, now)),
        "observed": _guard(lambda: {"error": "", **reporting.metrics(connection, now=now)}),
        "controls": _guard(lambda: _controls(connection)),
        "ci": _guard(lambda: _ci(connection, now)),
        "lanes": _guard(lambda: _lanes(connection, now)),
        "prs": _guard(lambda: _prs(connection, now)),
        "unread": list(UNREAD),
        **ranged,
    }
