"""Build runs read from the store into the monthly lane report (pack sd:6947).

`run_spans` turns each ended build run into its steps: a root for the run,
then its claim, build, each review round, enqueue, lane and merge, as far as
the run's row, its notes and its cost rows record them. Nothing leaves the
store: the genai-traces collector is the one trace sink, and sd-db sends no
traces of its own (pack sd:7365).

`month_report` reads those steps into a monthly Markdown draft for the
writing pipeline: item ids, PR numbers, counts, cost and run ids, never a
title, note body or detail.

The lane's prepare and gate steps live in the lane's own files, not here:
the `lane` step runs from the enqueue to the merge or the run's end.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import date, datetime, timedelta

from . import build

#: A report names the first run of each failure class, then this many costliest merged runs.
COSTLIEST = 3

#: The failure classes, first match wins, read from a run's outcome and detail.
CLASSES = (
    ("lost_run", re.compile(r"supervisor lost|lost at step|lost .* a second time")),
    ("timed_out", re.compile(r"ran past \d+ s")),
    ("review_blockers", re.compile(r"review rounds left blockers|local review blocking")),
    ("review_no_answer", re.compile(r"review gave no answer|review stayed rate_limited")),
    ("db_locked", re.compile(r"database is locked|did not answer", re.I)),
    ("conflict", re.compile(r"conflict")),
    ("line_too_long", re.compile(r"lines? too long|\(limit 72\)")),
    ("leak_guard", re.compile(r"leak-guard")),
    ("no_closes_trailer", re.compile(r"Closes:")),
    ("denied_tool", re.compile(r"permission denied")),
    ("flake", re.compile(r"flak", re.I)),
    ("gate_failed", re.compile(r"gate failed|make check")),
    ("no_commit", re.compile(r"left no commit")),
    ("lint_refused", re.compile(r"sd-docs-lint refused")),
)


def failure_class(outcome: str | None, detail: str | None) -> str | None:
    """None for a run that merged; else the first class whose pattern the detail matches, or `other`."""
    if outcome in (None, "done"):
        return None
    if outcome == "cancelled":
        return "cancelled"
    text = detail or ""
    return next((name for name, pattern in CLASSES if pattern.search(text)), "other")


def _nanos(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp).timestamp()) * 1_000_000_000


def _span_id(run: str, name: str) -> str:
    return hashlib.sha256(f"{run}/{name}".encode()).hexdigest()[:16]


def _value(value) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _attributes(values: dict) -> list[dict]:
    return [{"key": key, "value": _value(value)} for key, value in values.items() if value is not None]


def _span(run: str, name: str, start: str, end: str, attributes: dict, *, root: bool = False,
          error: str | None = None) -> dict:
    span = {"traceId": run, "spanId": _span_id(run, name), "name": name.split("#")[0], "kind": 1,
            "startTimeUnixNano": str(_nanos(start)), "endTimeUnixNano": str(_nanos(max(start, end))),
            "attributes": _attributes(attributes),
            "status": {"code": 2, "message": error} if error else {"code": 1}}
    if not root:
        span["parentSpanId"] = _span_id(run, "build_run")
    return span


def run_spans(connection, row: dict) -> list[dict]:
    """One ended run's spans, the root first."""
    ident, start, end = row["id"], row["created_at"], row["released_at"]
    members = build.run(connection, ident)["members"]
    items = [member["item"] for member in members]
    marks = ", ".join("?" for _ in items)
    titles = [title for (title,) in connection.execute(
        f"SELECT title FROM item WHERE id IN ({marks}) ORDER BY id", items)]
    notes = [dict(note) for note in connection.execute(
        f"SELECT item, timestamp, body FROM note WHERE item IN ({marks}) AND timestamp >= ? "
        f"AND timestamp <= ? ORDER BY timestamp, id", (*items, start, end))]
    cost = connection.execute("SELECT SUM(usd), SUM(tokens_in), SUM(tokens_out) FROM cost WHERE pass = ?",
                              (ident,)).fetchone()
    failure = failure_class(row["outcome"], row["detail"])
    common = {"sd.build_run": ident, "sd.repo": row["repo"], "sd.items": ",".join(map(str, items))}
    reports = [note["timestamp"] for note in notes if note["body"].startswith(f"Build run {ident} report:")]
    enqueued = next((note for note in notes if note["body"].startswith(f"Build run {ident} enqueued ")), None)
    merged = enqueued and next((note for note in notes if note["timestamp"] >= enqueued["timestamp"]
                                and note["body"].startswith("Code delivery ")), None)
    spans = [_span(ident, "build_run", start, end, {
        **common, "sd.item_titles": " | ".join(titles),
        "sd.branch": row["branch"], "sd.model": row["model"],
        "sd.cost_usd": float(cost[0]) if cost[0] is not None else None,
        "sd.tokens_in": cost[1], "sd.tokens_out": cost[2], "sd.review_round": row["review_round"],
        "sd.step": row["step"], "sd.outcome": row["outcome"], "sd.failure_class": failure}, root=True, error=failure),
        _span(ident, "claim", start, start, common)]
    building = reports[0] if reports else (enqueued["timestamp"] if enqueued else end)
    spans.append(_span(ident, "build", start, building, common))
    for number, (since, until) in enumerate(zip(reports, reports[1:], strict=False), start=1):
        spans.append(_span(ident, f"review_round#{number}", since, until, {**common, "sd.review_round": number}))
    if enqueued:
        entry = re.search(r"as entry (\S+)", enqueued["body"])
        lane = {**common, "sd.lane_entry": entry.group(1) if entry else None}
        spans.append(_span(ident, "enqueue", enqueued["timestamp"], enqueued["timestamp"], lane))
        spans.append(_span(ident, "lane", enqueued["timestamp"], merged["timestamp"] if merged else end, lane,
                           error=None if merged else failure))
    if merged:
        pull = re.search(r"/pull/(\d+)", merged["body"])
        commit = re.search(r"\bat ([0-9a-f]{40})\b", merged["body"])
        spans.append(_span(ident, "merge", merged["timestamp"], merged["timestamp"], {
            **common, "sd.pr": int(pull.group(1)) if pull else None, "sd.commit": commit.group(1) if commit else None}))
    return spans


def ended_runs(connection) -> list[dict]:
    """Every ended build run, oldest first."""
    return [dict(row) for row in connection.execute(
        "SELECT runner_run.* FROM runner_run JOIN assignment ON assignment.id = runner_run.assignment "
        "WHERE assignment.scope = ? AND runner_run.released_at IS NOT NULL AND runner_run.outcome IS NOT NULL "
        "ORDER BY runner_run.created_at, runner_run.id", (build.SCOPE,))]


def last_month(today: date | None = None) -> str:
    """The month before `today`'s, `YYYY-MM`: a job on the 1st reports the month just ended."""
    return ((today or date.today()).replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def _attributes_of(span: dict) -> dict:
    return {row["key"]: next(iter(row["value"].values())) for row in span["attributes"]}


def _runs(count: int) -> str:
    return f"{count} run" if count == 1 else f"{count} runs"


def month_report(connection, month: str) -> str:
    """The Markdown draft for the build runs that ended in `month` (`YYYY-MM`, UTC), against the month before.

    Merged PRs with their cost and lead time (the bundle's first run to the
    merge), failure classes with their trend, review rounds and the
    repeat-blocker rate (reviewed runs whose blockers outlived every round),
    denied tool calls, database-lock refusals, and the run id of the first
    run of each failure class and of the costliest merges. Metadata only.
    """
    first = date.fromisoformat(month + "-01")
    before = last_month(first)
    runs = ended_runs(connection)
    this = [row for row in runs if row["released_at"].startswith(month)]
    classes = Counter(failure_class(row["outcome"], row["detail"]) for row in this)
    prior = Counter(failure_class(row["outcome"], row["detail"]) for row in runs
                    if row["released_at"].startswith(before))
    lines = [f"# Lane report, {month}", "",
             "Draft from `sd-db telemetry report`: metadata only, times in UTC. "
             "Not for publication as is.", "",
             f"Build runs ended: {len(this)} ({sum(prior.values())} in {before}).", "",
             "## Merged pull requests", "", "| Repo | PR | Items | Cost (USD) | Lead time (h) | Review rounds |",
             "| --- | --- | --- | --- | --- | --- |"]
    notable: list[tuple[str, str]] = []
    merged: list[tuple[float, str, str]] = []
    for row in this:
        if row["outcome"] != "done":
            continue
        spans = run_spans(connection, row)
        # The merge may be noted on an earlier run of the bundle, one interrupted after it enqueued (pack sd:7008).
        merge = next((span for run in runs if run["assignment"] == row["assignment"]
                      for span in (spans if run["id"] == row["id"] else run_spans(connection, run))
                      if span["name"] == "merge"), None)
        if merge is None:
            continue
        pr = _attributes_of(merge).get("sd.pr")
        items = _attributes_of(spans[0])["sd.items"].split(",")
        bundle = [ident for (ident,) in connection.execute(
            "SELECT id FROM runner_run WHERE assignment = ?", (row["assignment"],))]
        marks = ", ".join("?" for _ in bundle)
        cost = connection.execute(f"SELECT COALESCE(SUM(usd), 0) FROM cost WHERE pass IN ({marks})",
                                  bundle).fetchone()[0]
        start = connection.execute("SELECT MIN(created_at) FROM runner_run WHERE assignment = ?",
                                   (row["assignment"],)).fetchone()[0]
        hours = (int(merge["startTimeUnixNano"]) - _nanos(start)) / 3.6e12
        repo, label = row["repo"].rstrip("/").rsplit("/", 1)[-1], f"#{pr}" if pr else "unrecorded"
        lines.append(f"| {repo} | {label} | {', '.join(f'sd:{item}' for item in items)} | {cost:.2f} "
                     f"| {hours:.1f} | {row['review_round'] or '-'} |")
        merged.append((cost, f"{repo} PR {label}, {cost:.2f} USD", row["id"]))
    lines += ["", f"## Failure classes, against {before}", "", f"| Class | {month} | {before} | Change |",
              "| --- | --- | --- | --- |"]
    names = sorted((name for name in set(classes) | set(prior) if name), key=lambda name: (-classes[name], name))
    for name in names:
        change = classes[name] - prior[name]
        lines.append(f"| {name} | {classes[name]} | {prior[name]} | {f'{change:+d}' if change else '0'} |")
        failed = next((row for row in this if failure_class(row["outcome"], row["detail"]) == name), None)
        if failed:
            notable.append((name, failed["id"]))
    reviewed = [row for row in this if row["review_round"] is not None
                or failure_class(row["outcome"], row["detail"]) == "review_blockers"]
    rounds = [row["review_round"] for row in reviewed if row["review_round"] is not None]
    rate = f" ({100 * classes['review_blockers'] // len(reviewed)}%)" if reviewed else ""
    lines += ["", "## Reviews and tools", "",
              f"- Review rounds: {sum(rounds) / len(rounds):.1f} on average over {_runs(len(rounds))}."
              if rounds else "- Review rounds: none recorded.",
              f"- Repeat-blocker rate: {classes['review_blockers']} of {len(reviewed)} reviewed runs{rate} "
              "ended with blockers left after every round.",
              f"- Denied tool calls: {_runs(classes['denied_tool'])} ended on one.",
              f"- Database-lock refusals: {_runs(classes['db_locked'])} ended on one.",
              "", "## Notable runs", ""]
    notable += [(what, ident) for _, what, ident in sorted(merged, key=lambda run: -run[0])[:COSTLIEST]]
    lines += [f"- {what}: build run {ident}" for what, ident in notable] or ["- None."]
    return "\n".join(lines) + "\n"
