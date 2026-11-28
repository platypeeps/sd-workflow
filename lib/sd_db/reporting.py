"""Observed cron reports and explicit interaction counts, never inferred effort."""

import hashlib
import json
import os
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from . import reads, runner, ship, workflow
from .database import transaction
from .writes import (
    STATUSES,
    add_note,
    create_item,
    now,
    record_state,
    resolve_note,
    set_item_fields,
    stamp,
    transition,
)

MAX_REPORT = 200_000
#: The line prefix a job declares a finding with (`ingest_log`).
MARK = b"SD_REPORT_ATTENTION: "
#: The finding a failed job declares once it proved its data safe, then the
#: step that failed (system sd:6030). `ingest_log` titles the report with
#: it; every other failure is titled `AT_RISK`.
SAFE = "data safe, one step failed: "
AT_RISK = "data at risk"
#: The line `cron-jobs.sh` opens each run's log slice with: `[<job>] <local
#: time> starting (cwd: ...)`. A carried lost run (sd:3537) opens with its
#: own, so only the lines after this run's own line grade it (`_grade`).
RUN_START = re.compile(rb"\[([^\]\n]*)\] (\S+) starting(?: |\r?\n|\Z)")

#: The `state` key a job's health is recorded under, one row per job by the
#: `heartbeat` retention rule. Prefixed, because `key` is a flat namespace
#: shared with every other heartbeat writer and a bare job name would collide
#: with whatever else chose the same word.
HEARTBEAT_KEY = "cron-report:"

#: The `state` key, `heartbeat` kind, that holds the newest run of a job on
#: one host: `cron-chain:<job>@<host>`, body `{"run_id", "ended", "item"}`,
#: one row per key, updated in place (`_set_chain`), so a quiet tick adds no
#: row for `retention` to compact. `item` is the report a clean-exit run with
#: findings went to, else `None`. It is what lets an acknowledged report take
#: an unbroken repeat and nothing after a break (sd:4629): the heartbeat
#: above is one row per job, and every host overwrites it.
CHAIN_KEY = "cron-chain:"

#: The reason `_supersede` gives its transition, after `-> done by cron: `.
#: The status-change note is how a superseded report is told from an
#: acknowledged one (sd:4641): it never counts as acknowledged.
SUPERSEDED = "superseded by sd:"
#: pack sd:7729: the reason a clean run of the job on the report's host closed it, then `<job>:<run id>`.
RECOVERED = "recovered: clean run "

#: How far past the clock a recorded `ended` may be and still count as the
#: newest tick. `cron-jobs.sh` stamps `ended` on this machine moments before it
#: ingests, so a real run is seconds ahead at most; five minutes covers a
#: slewing clock and a hand recovery typed to the minute. A row beyond it is a
#: bad stamp, and letting it win would drop every later heartbeat until the
#: clock caught up. A bad row inside it holds health for five minutes at most.
FUTURE_ALLOWANCE = timedelta(minutes=5)

#: The job-name shape `ingest` accepts, and the shape of a record marker.
JOB_NAME = r"[a-z0-9][a-z0-9_-]{0,99}"

#: The `session` `ingest` writes on the report and on the followup it opens
#: for a run that needs attention. Half of how `acknowledge` tells that
#: followup from one a person wrote; the other half is the note's text,
#: which `_ingest_followup` derives from the report's own provenance.
#: `session` alone is not proof: `workflow.add_item_note` writes whatever
#: `who` it is handed into the same column.
INGEST_SESSION = "cron"


def _ingest_followup(job, ended, attention_basis):
    """The one followup `ingest` writes, so `acknowledge` can recognise it."""
    return f"Review {job} findings from {ended}; {attention_basis}."


def _ingest_followup_for(fields):
    """The followup text `ingest` wrote for a report, read back from its `fields`.

    `None` when the fields cannot say: not JSON, no `report`, a provenance
    missing a key. A note can then match nothing, so a report whose fields
    are unreadable is refused with the note's id rather than resolved on a
    guess, and the default path -- which never reads `fields` at all --
    still finishes such a report the way the dashboard's notice promises.
    """
    try:
        report = json.loads(fields)["report"]
        return _ingest_followup(report["job"], report["ended"], report["attention_basis"])
    except (TypeError, ValueError, KeyError):
        return None

#: The most reports one bulk acknowledge moves. A guard against a mistaken
#: cutoff, not a measured limit: a 281-row apply held the write lock for about
#: 25 ms on a copy of the live store.
MAX_BATCH = 1000

#: The job a bulk acknowledge files its batch report under, and the record
#: marker that report carries so a later bulk run declines it.
BULK_JOB = "reports-acknowledge"

#: How many folded run ids an open report remembers under `fields.repeats`,
#: newest last. Enough to answer the replay of any run the operator could
#: plausibly recover by hand; bounded, because remembering every one is the
#: unbounded growth the fold exists to stop.
REMEMBERED_RUNS = 20


def _folded_into(connection, job, run_id, host=None):
    """The `cron-report` row of `job` on `host` that remembers `run_id` under `repeats.run_ids`, or `None`.

    A run folds only into its own host's report, and two hosts can mint one
    run id (sd:10440), so another host's row is not this run's replay. A row
    filed before the host was stored matches any host, as it always did.

    Done or not: a folded run's identity lives only here, so this is how a
    replay of one is told from a new failure after the report it folded into
    was acknowledged (sd:955). The newest such row, because a run older than
    the `REMEMBERED_RUNS` remembered is counted again into a later report, and
    the later one is then the report that answers for it. Read behind
    `json_valid` the way `_fold_target` reads; `json_each` over NULL yields
    no row, so an unreadable `fields` simply does not match.

    Every report-worthy ingest asks this, so it reads one job's rows and not
    the whole table (the #401 review). `ingest` writes `external_id` as
    `job:run_id`, and `JOB_NAME` has no `:`, so the job's reports are exactly
    the `external_id` values between `job:` and `job;` (`;` sorts right after
    `:`). That range seeks `item_by_external`, whose `external_id IS NOT NULL`
    condition the comparison implies. The JSON is then read for this job's
    reports only: on the live store on 2026-09-23, 469 `cron-report` rows and 156 for the
    busiest job, one lookup went from 0.67 ms to 0.15 ms. It still grows with
    the job's own report history; a lookup that does not would need a
    run-id column or table, which is a migration.
    """
    return connection.execute(
        "SELECT id FROM item WHERE source='cron-report' AND external_id > ? AND external_id < ?"
        " AND kind='report'"
        " AND CASE WHEN json_valid(fields) THEN json_extract(fields, '$.report.job') END = ?"
        " AND EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(fields) THEN fields END, '$.repeats.run_ids')"
        "             WHERE value = ?)"
        " AND COALESCE(CASE WHEN json_valid(fields) THEN json_extract(fields, '$.report.host') END = ?, 1)"
        " ORDER BY id DESC LIMIT 1", (job + ":", job + ";", job, run_id, host)).fetchone()


def _filed(connection, job, run_id, host):
    """This run's own report row and its `external_id`: `(external, row or None)`.

    The identity is `job:run_id`. A run id is a UTC second and a pid, so two
    hosts can mint one (sd:10440): when that row is another host's, this
    host's run is `job:run_id@host`. A row with no stored host is anyone's,
    as it was before hosts were stored.
    """
    external = job + ":" + run_id
    query = "SELECT id, fields FROM item WHERE source='cron-report' AND external_id=?"
    row = connection.execute(query, (external,)).fetchone()
    if row and host is not None and _stored_host(row) not in (None, host):
        external += "@" + host
        row = connection.execute(query, (external,)).fetchone()
    return external, row


def _stored_host(row):
    """The host a report row stored, or `None` when it stored none or its fields do not parse."""
    try:
        host = (json.loads(row["fields"]).get("report") or {}).get("host")
    except (TypeError, ValueError, AttributeError):
        return None
    return host if isinstance(host, str) else None


#: A report's current set of findings: `repeats.basis` once a shrunken set
#: replaced its body (sd:4658), else its first run's basis.
CURRENT_BASIS = "COALESCE(json_extract(fields, '$.repeats.basis'), json_extract(fields, '$.report.attention_basis'))"


def _fold_target(connection, job, attention_basis, *, host, chain=None):
    """The attention report a repeat of `job` on `host` folds into, or `None`.

    The newest `cron-report` row for this job that is not `done`, has
    `fields.attention` exactly true, the same current basis (`CURRENT_BASIS`), no
    `record` marker, and the same `fields.report.host`, absent when `host` is
    `None`. A fold keeps the first run's provenance and takes the new run's
    body, so another host's run folded in showed its body under this host's
    name and log path (pack sd:7481): a findings basis names no host. A row
    filed before the host was stored takes only a run with no host (no
    backfill). A `done` row qualifies only as `chain`, after every open one
    (sd:4629): the report the previous run on this host went to, so the
    operator's acknowledgement holds while the findings repeat
    without a break and no longer (`ingest_log`). A row whose newest status
    change is `_supersede`'s never qualifies: nobody acknowledged it
    (sd:4641). The `external_id` range is `_folded_into`'s: done rows are
    never deleted, so the read seeks this job's rows only. Every JSON function sits inside
    `CASE WHEN json_valid(fields)`, the way `clean_candidates` and
    `clean_reports` read: a `CASE` evaluates its branch only when the test
    is true, whatever order the query plan picks for `AND` terms, so a row
    whose `fields` are unreadable (`UNREADABLE_FIELDS`) is skipped rather
    than raising. The `NOT (UNREADABLE_FIELDS)` term names that intent; it
    is not what makes the read safe. Such a row is for a person to look at,
    and it can neither carry a count nor say what it was.
    """
    return connection.execute(
        "SELECT id, fields FROM item WHERE kind='report' AND source='cron-report'"
        " AND external_id > ? AND external_id < ?"
        " AND (status != 'done' OR id IS ? AND NOT EXISTS (SELECT 1 FROM note WHERE note.item = item.id"
        "      AND note.id = (SELECT MAX(id) FROM note AS last WHERE last.item = item.id AND last.kind = 'status_change')"
        f"      AND note.body LIKE '% -> done by {INGEST_SESSION}: {SUPERSEDED}%'))"
        f" AND NOT ({UNREADABLE_FIELDS})"
        " AND CASE WHEN json_valid(fields) THEN json_extract(fields, '$.report.job') END = ?"
        " AND CASE WHEN json_valid(fields) THEN json_type(fields, '$.attention') END = 'true'"
        f" AND CASE WHEN json_valid(fields) THEN {CURRENT_BASIS} END = ?"
        " AND CASE WHEN json_valid(fields) THEN json_type(fields, '$.record') END IS NULL"
        " AND CASE WHEN json_valid(fields) THEN json_extract(fields, '$.report.host') END IS ?"
        " ORDER BY status = 'done', id DESC LIMIT 1",
        (job + ":", job + ";", chain, job, attention_basis, host)).fetchone()


def _check_identity(job, run_id):
    """The job name and run id every run is checked against before anything is written for it."""
    if not isinstance(job, str) or not re.fullmatch(JOB_NAME, job or "") or not isinstance(run_id, str) or not 1 <= len(run_id) <= 160:
        raise workflow.WorkflowError("report needs a known job name and stable run identity")


def _check_times(started, ended):
    """Both stamps in the one shape, and a run that does not end before it starts."""
    started, ended = stamp(started), stamp(ended)
    if ended < started:
        raise workflow.WorkflowError("report cannot end before it starts")
    return started, ended


def ingest(connection, *, job, run_id, started, ended, exit_code, text, source_path,
           attention=False, attention_basis="explicit report", truncated=False, removed=None,
           actor=None, record=None, chain=None, now=None, host=None, grade=None):
    """File one run's report, or answer a replay of it with the report it filed.

    `grade` titles a new report in place of `needs attention`: `ingest_log`
    passes the failed run's grade (`SAFE`, `AT_RISK`). A fold keeps the
    target's title, and a replay never reads it.

    `host` is the machine the run ran on, which `ingest_log` passes, stored as
    `fields.report.host`: the key `_supersede` reads (sd:4641). A row filed
    before it existed replays against its fields without it.

    `chain` is the one `done` report this run may fold into: the report the
    previous run of the job on this host went to, which only `ingest_log`
    knows (`CHAIN_KEY`). Without it no done report is a target. `now` is the
    clock a fold's `last_ended` is checked against, as `_beat` checks.

    `actor` and `record` are for a report that records an operator act rather
    than a cron run. `actor` says who and what acted, stored beside `removed`
    as `fields.report.actor`. `record` marks the row as such a record, stored
    as top-level `fields.record`, which a bulk acknowledge declines by the
    key's existence. A call that gives neither writes the `fields` it always
    did.

    A run that needs attention while an open attention report for the same
    job, `host` and `attention_basis` exists folds into that report instead
    of opening another (sd:920: sd:880's watchdog opened 51 rows in thirteen
    hours, one every fifteen minutes, each with its own followup). The open
    report's `fields.repeats` becomes `{"count", "first_ended",
    "last_run_id", "last_ended", "run_ids"}`: the count starts at 2 on the
    first fold, `first_ended` is the open report's own `ended`, and `run_ids`
    holds the last `REMEMBERED_RUNS` folded run ids, in arrival order. The body
    becomes the folded run's `text` when it ended no earlier than the run
    `last_run_id` names, and that run becomes the last (sd:4629: the
    sd:4042 ruling keeps one report "updated while its findings stay the
    same", and a drifting measure is in the text, not the basis). It replaces
    the old body, so the row holds one body however often it folds. Nothing
    else on the row changes -- not the title, not the provenance under
    `fields.report`, which stays the first run's, and no second followup is
    written. `text` differing between runs is expected (a timestamp in
    stderr) and does not block the fold; a different `attention_basis` is a
    different failure and opens its own report. The returned state is the
    target's, so a caller tells a fold from a new row by `external_id`.

    Never folded: a clean report (`attention` false), a `record` report, and
    a report whose `fields` are unreadable is never a target
    (`_fold_target`). An acknowledged report stays `done` while the same
    findings repeat without a break: a run with a clean exit folds into it
    when it is `chain` and no open report matches (sd:4629). A clean run, a
    failure or other findings on that host in between is a break, so
    findings that recover and return open a fresh report. A carried lost
    run's other finding is in the basis (`ingest_log`), so it never closes
    unseen. A non-zero exit never
    folds into a `done` report: its basis says how the run ended, not what
    was found, so acknowledging one failure does not hide the next, and the
    run after an acknowledged failure opens a fresh report as before.

    Replay: a `run_id` in `repeats.run_ids` of any of the job's reports, or
    a report's own, is answered with that report's state and counts nothing
    -- whether the report is still open or already `done`. The run id is
    looked up before a fold target is chosen (`_folded_into`), so a hand
    recovery of a folded run after the acknowledge neither opens a fresh
    report nor writes a followup (sd:955). A `record` report is not asked:
    it was never folded, so one that reuses a folded run's `job` and
    `run_id` is filed as its own row, with its own `record` and `actor`
    fields, and replays only against that row. A folded run older than the
    `REMEMBERED_RUNS` remembered has left `run_ids`, so its replay is
    counted again: the count can overstate by hand recoveries of runs that
    old, and it never understates.
    """
    _check_identity(job, run_id)
    if type(exit_code) is not int or type(attention) is not bool or type(truncated) is not bool:
        raise workflow.WorkflowError("report exit code and attention must be explicit typed values")
    if not isinstance(text, str) or len(text.encode()) > MAX_REPORT or not isinstance(source_path, str):
        raise workflow.WorkflowError("report text exceeds the bounded report size")
    if not isinstance(attention_basis, str) or len(attention_basis) > 2000 or len(source_path) > 4000:
        raise workflow.WorkflowError("report provenance exceeds its size limit")
    if removed is not None and (not isinstance(removed, dict) or len(removed) > 20
                                or any(not isinstance(key, str) or type(count) is not int or count < 0
                                       for key, count in removed.items())):
        raise workflow.WorkflowError("report removal counts must be small nonnegative integers by name")
    if actor is not None and (not isinstance(actor, dict) or len(actor) > 10
                              or any(not isinstance(key, str)
                                     or not (value is None or type(value) is int
                                             or isinstance(value, str) and len(value) <= 200)
                                     for key, value in actor.items())):
        raise workflow.WorkflowError("report actor must be at most ten short text, integer or empty values by name")
    if record is not None and (not isinstance(record, str) or not re.fullmatch(JOB_NAME, record)):
        raise workflow.WorkflowError("report record marker must be a job name")
    if chain is not None and type(chain) is not int:
        raise workflow.WorkflowError("report chain must be an item id")
    if host is not None and (not isinstance(host, str) or not 1 <= len(host) <= 255):
        raise workflow.WorkflowError("report host must be a short name")
    if grade is not None and (not isinstance(grade, str) or not 1 <= len(grade) <= 200 or "\x00" in grade):
        raise workflow.WorkflowError("report grade must be short text")
    started, ended = _check_times(started, ended)
    provenance = {"job": job, "run_id": run_id, "started": started, "ended": ended,
                  "exit_code": exit_code, "source_path": source_path, "truncated": truncated,
                  "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "attention_basis": attention_basis}
    if removed is not None:
        # What a prune took, by name, so the counts are read back as numbers
        # and not parsed out of the report's prose.
        provenance["removed"] = removed
    if actor is not None:
        provenance["actor"] = actor
    if host is not None:
        provenance["host"] = host
    fields = {"attention": attention, "report": provenance}
    if record is not None:
        fields["record"] = record
    with transaction(connection):
        external, previous = _filed(connection, job, run_id, host)
        if previous:
            # `repeats` is written by later runs, not by this one, so the
            # open report's own run replays against its fields without it.
            stored = json.loads(previous["fields"])
            stored.pop("repeats", None)
            expected = fields
            if "host" not in (stored.get("report") or {}):
                expected = {**fields, "report": {k: v for k, v in provenance.items() if k != "host"}}
            if stored != expected:
                raise workflow.WorkflowError("this report identity already has different evidence")
            return workflow.item_state(connection, previous["id"])
        # A record replays only through the exact identity above: it was
        # never folded, so a folded run id it happens to reuse is not its
        # replay, and answering with that report would drop its own fields.
        folded = _folded_into(connection, job, run_id, host) if record is None else None
        if folded:
            return workflow.item_state(connection, folded["id"])
        # A failure never folds into a done report: its basis says how the
        # run ended, not what it found.
        target = (_fold_target(connection, job, attention_basis, host=host, chain=chain if exit_code == 0 else None)
                  if attention and record is None else None)
        if not target and attention and record is None and exit_code == 0 and host is not None:
            # A set that only shrank is the same report with fewer findings
            # (sd:4658): it folds into this host's open report that holds
            # every one of them, and its body replaces the larger set's.
            lines = set(attention_basis.split("\n"))
            target = next((row for row in _host_reports(connection, job, host)
                           if isinstance(basis := _current_basis(row), str) and lines <= set(basis.split("\n"))), None)
        if target:
            stored = json.loads(target["fields"])
            repeats = stored.get("repeats") or {"count": 1, "first_ended": stored["report"]["ended"], "run_ids": []}
            latest = repeats.get("last_ended", stored["report"]["ended"])
            newest = not ended < latest <= stamp(((now or datetime.now(UTC)) + FUTURE_ALLOWANCE).isoformat())
            repeats.update(count=repeats["count"] + 1, run_ids=(repeats["run_ids"] + [run_id])[-REMEMBERED_RUNS:])
            if newest:
                # A late run (a hand recovery) counts, but the body and
                # `last_*` stay the newest run's; a stamp past the allowance
                # is not the newest.
                repeats.update(last_run_id=run_id, last_ended=ended)
            # A cut body never replaces a whole one (sd:4641): the row's
            # `report.truncated` is the first run's, and the dashboard reads
            # it, so `body_truncated` says what the kept body is. The basis
            # matched, so the findings are the same.
            kept_cut = _kept_cut(target)
            whole = not truncated or kept_cut
            if newest and whole and truncated != kept_cut:
                repeats["body_truncated"] = truncated
            # The kept body's set is the report's set from now on (sd:4658):
            # a finding missing from it is new when it comes back.
            if newest and whole and attention_basis != _current_basis(target):
                repeats["basis"] = attention_basis
            set_item_fields(connection, target["id"], fields={**stored, "repeats": repeats},
                            **({"body": {"text": text}} if newest and whole else {}))
            return workflow.item_state(connection, target["id"])
        item = create_item(connection, kind="report", title=f"{job}: {grade or ('needs attention' if attention else 'run report')}",
                           status="planning", source="cron-report", external_id=external,
                           fields=fields, body={"text": text}, session=INGEST_SESSION)
        if attention:
            add_note(connection, item, "followup", _ingest_followup(job, ended, attention_basis), session=INGEST_SESSION)
        return workflow.item_state(connection, item)


def _current_basis(item):
    """Report `item`'s current set of findings, as `CURRENT_BASIS` reads it."""
    fields = json.loads(item["fields"])
    return (fields.get("repeats") or {}).get("basis", fields["report"].get("attention_basis"))


def _kept_cut(item):
    """Whether report `item`'s kept body is cut: `repeats.body_truncated`, else its first run's flag."""
    fields = json.loads(item["fields"])
    return (fields.get("repeats") or {}).get("body_truncated", fields["report"].get("truncated")) is True


def _host_reports(connection, job, host, *, but=None, failed=False):
    """The open clean-exit findings reports of `job` that `host` filed, newest first, other than `but`.

    `failed` takes the host's failure reports too, any exit code (pack sd:7729).

    Only rows that store this `host` (sd:4641, no backfill), with `attention`
    and no `record`, and none another host's newest run folded into before
    `_fold_target` read the host (pack sd:7481): that host's chain row still
    names it, and its findings are not this host's to close or to shrink.
    """
    return connection.execute(
        "SELECT id, fields FROM item WHERE kind='report' AND source='cron-report' AND status != 'done'"
        " AND external_id > ? AND external_id < ? AND id IS NOT ?"
        " AND CASE WHEN json_valid(fields) THEN json_extract(fields, '$.report.job') = ?"
        "      AND json_extract(fields, '$.report.host') = ?"
        "      AND json_type(fields, '$.attention') = 'true'"
        "      AND (? OR json_type(fields, '$.report.exit_code') = 'integer' AND json_extract(fields, '$.report.exit_code') = 0)"
        "      AND json_type(fields, '$.record') IS NULL END"
        " AND NOT EXISTS (SELECT 1 FROM state WHERE kind='heartbeat' AND substr(key, 1, ?) = ? AND key != ?"
        "      AND CASE WHEN json_valid(body) THEN json_extract(body, '$.item') END = item.id)"
        " ORDER BY id DESC",
        (job + ":", job + ";", but, job, host, failed, len(f"{CHAIN_KEY}{job}@"), f"{CHAIN_KEY}{job}@",
         f"{CHAIN_KEY}{job}@{host}")).fetchall()


def _supersede(connection, *, job, host, by, recovered=None):
    """Close the other open findings reports of `job` on `host` as superseded by report `by` (sd:4641).

    A clean-exit findings run with a whole body, filed or folded into `by`,
    is the host's whole current set, so another open one -- a larger set
    that changed -- would only sit beside it. A set that only shrank folds
    instead (sd:4658). Only `_host_reports` rows take part, and never one
    with a followup the ingest did not write: somebody owes work on it. The
    ingest's own followup is resolved, as `acknowledge` resolves it, and the status note names `by`; `_fold_target` reads that note, so a
    superseded report never counts as acknowledged. `ingest_log` calls this
    only for the host's newest run, so a late run closes nothing. pack sd:7729: `recovered`, a clean run's
    `<job>:<run id>`, closes the host's failure reports too, under the same rules, with `RECOVERED` as the reason.
    """
    for row in _host_reports(connection, job, host, but=by, failed=recovered is not None):
        ingests = _ingest_followup_for(row["fields"])
        followups = connection.execute(
            "SELECT id, session, body FROM note WHERE item=? AND kind='followup' AND resolved_at IS NULL",
            (row["id"],)).fetchall()
        if any(note["session"] != INGEST_SESSION or note["body"] != ingests for note in followups):
            continue
        for note in followups:
            resolve_note(connection, note["id"])
        transition(connection, row["id"], "done", who=INGEST_SESSION, reason=(
            f"{RECOVERED}{recovered}" if recovered else f"{SUPERSEDED}{by}") + (
            "; resolved the ingest's followup " + ", ".join(str(note["id"]) for note in followups) if followups else ""))


def _set_chain(connection, key, row, body):
    """Write a host's chain row (`CHAIN_KEY`) in place, or its first one."""
    if row is None:
        record_state(connection, "heartbeat", key=key, body=body)
    else:
        connection.execute("UPDATE state SET body=?, timestamp=? WHERE id=?",
                           (json.dumps(body, sort_keys=True), now(), row["id"]))


def health(connection, job, *, now=None):
    """What this job's last recorded tick said, or `None` if it has never run.

    Read from the heartbeat rather than from the report items, because the
    reports are the exceptions now and a job with no report is the normal case
    rather than a job that has never run. The last tick is `_current_tick`'s.
    """
    body = _current_tick(connection, HEARTBEAT_KEY + job, now)
    return body["healthy"] if body else None


def _current_tick(connection, key, now=None):
    """The newest real tick under `key`, its body, or `None` when it has none.

    The latest `ended` within `now` plus `FUTURE_ALLOWANCE`, the newest row on
    a tie: not the newest row by id, which can be one stamped past the
    allowance (sd:10410), and it held health over the real newest tick. With
    no such row, the newest row by id: a bad stamp is then all there is.
    """
    limit = stamp(((now or datetime.now(UTC)) + FUTURE_ALLOWANCE).isoformat())
    row = connection.execute(
        "SELECT body FROM state WHERE kind='heartbeat' AND key=? AND json_valid(body)"
        " AND json_type(body, '$.ended') = 'text' AND json_extract(body, '$.ended') <= ?"
        " ORDER BY json_extract(body, '$.ended') DESC, id DESC LIMIT 1", (key, limit)).fetchone()
    row = row or connection.execute(
        "SELECT body FROM state WHERE kind='heartbeat' AND key=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    return json.loads(row["body"]) if row else None


def _beat(connection, *, job, run_id, healthy, ended, exit_code, now, host=None):
    """Record this tick's health, once per run, and only if it is the newest.

    Guarded on `run_id` rather than written unconditionally, for the same
    reason `ingest` is guarded on `external_id`: one run is one tick, and the
    ingest for a given run can be entered twice. Nothing retries it on its own
    -- `cron-jobs.sh` calls it once and only warns on stderr if it fails, and a
    re-run of the job mints a fresh run id rather than resending the old one --
    so the way one run id arrives twice is an operator recovering a report by
    hand. An appended second row would make that one run look like two to
    anything counting them, and `ingest`'s own replay is a no-op, so this one
    has to be too, or the two halves of `ingest_log` disagree about what a
    replay means. Returns whether a row was written, so a replay can say it
    recorded nothing.

    The guard is any row carrying this run, not only the latest: a hand
    recovery is exactly the case where newer ticks have landed since (sd:757).
    And it is also on `ended`, because `retention` keeps one heartbeat per key,
    so an older run's own row may already be gone. A run that ended before the
    newest recorded tick is never written, replayed or not: `health` reads the
    newest row by id, and appending an older tick after a newer one would make
    the older one the job's current health.

    Except against a row stamped past `now` plus `FUTURE_ALLOWANCE`: that row
    is a bad stamp rather than a newer tick, and it does not outrank anything.
    The alternative was refusing such an `ended` at ingest. That loses the
    run's report -- `cron-jobs.sh` only warns when the ingest fails -- and does
    nothing about a row already written.

    A tie on `ended` is the case the `ended` guard cannot decide: `stamp` keeps
    whole seconds, so two runs of one job can share one (sd:757, the #326
    review). Between two runs that each arrive for the first time, arrival
    order is the order: `cron-jobs.sh` holds a lock per job and ingests a run
    as it ends, so the second arrival ended second, and a job that failed and
    recovered inside one second still reads as recovered. What arrival order
    cannot rank is a *replay* of the older run, which arrives last. Only its
    run id tells it apart, and compaction can delete the row carrying that id.
    So a row written at a tie also carries `same_second`: the run ids of the
    newest row and of that row's own `same_second`, newest last, bounded at
    `REMEMBERED_RUNS` as a folded report's run ids are. Compaction keeps that
    newest row, so the run-id guard still finds every run of that second, and
    such a replay returns `"recorded"` instead of overwriting current health.
    What stays open is a run that never reached the database at all, recovered
    by hand, that ended in the very second of a newer tick: nothing recorded
    it, so nothing can rank it, and it is written as the newest. Closing that
    needs an order finer than a whole second, outside this function.

    The run is `run_id` on `host`, stored as `host` and, in `same_second`,
    as `run_id@host`: two hosts can mint one run id (sd:10440), and another
    host's clean run read as this host's replay and hid its failure. A row or
    entry with no host is anyone's, as it was before hosts were stored.

    Returns `"written"`, `"tied"` when written at a tie with a different run,
    `"recorded"` when a row already carries this run, or `"older"` when a
    newer tick is recorded, so a result can say which.
    """
    key = HEARTBEAT_KEY + job
    if connection.execute(
            "SELECT 1 FROM state WHERE kind='heartbeat' AND key=?"
            " AND (CASE WHEN json_valid(body) THEN json_extract(body,'$.run_id') END = ? AND " + _SAME_HOST +
            "      OR EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(body) THEN body END, '$.same_second')"
            "                 WHERE value IN (?, ?)))",
            (key, run_id, host, run_id, _tagged(run_id, host))).fetchone():
        return "recorded"
    # The newest real tick (sd:10410), as `health` reads it; a row stamped
    # past the allowance outranks nothing, even when it is the only one.
    newest_body = _current_tick(connection, key, now) or {}
    newest = newest_body.get("ended") or ""
    if ended < newest <= stamp((now + FUTURE_ALLOWANCE).isoformat()):
        return "older"
    body = {"healthy": healthy, "run_id": run_id, "ended": ended, "exit_code": exit_code}
    if host is not None:
        body["host"] = host
    tied = ended == newest
    if tied:
        earlier = newest_body.get("same_second")
        earlier = [value for value in earlier if isinstance(value, str)] if isinstance(earlier, list) else []
        newest_host = newest_body.get("host") if isinstance(newest_body.get("host"), str) else None
        previous = [_tagged(newest_body["run_id"], newest_host)] if isinstance(newest_body.get("run_id"), str) else []
        body["same_second"] = [*earlier, *previous][-REMEMBERED_RUNS:]
    record_state(connection, "heartbeat", key=key, body=body)
    return "tied" if tied else "written"


#: A heartbeat row is this host's, the second `?`, or stored no host (sd:10440).
_SAME_HOST = "COALESCE(CASE WHEN json_valid(body) THEN json_extract(body,'$.host') END = ?, 1)"


def _tagged(run_id, host):
    """A run as a `same_second` entry: `run_id@host`, or `run_id` alone with no host."""
    return run_id if host is None else f"{run_id}@{host}"


def _quiet(beat, previous):
    """Why a clean run filed no report, true of this run (the #315 review).

    `previous` is the job's health before this run: `None` when it had
    never ticked, which is a baseline and not a tick that followed a clean
    one. A replay and a tie say so too, rather than borrowing the ordinary
    sentence.
    """
    if beat == "older":
        return "an older run than the job's newest recorded tick, so no heartbeat was written"
    if beat == "recorded":
        return "a replay of a run whose heartbeat is already recorded, so nothing was written"
    if previous is None:
        return "the job's first recorded tick, a baseline and not an event"
    if beat == "tied":
        return ("a clean tick that ended in the same second as the job's newest recorded tick, which was"
                " also clean; recorded after it, in the order the two arrived")
    return "a clean tick of a job whose last tick was also clean"


def _grade(marks, starts, *, started, ended):
    """A failed run's grade: its first line that proved the data safe, else `AT_RISK`.

    `marks` are the slice's `MARK` lines; `starts` its `RUN_START` lines of
    this job, each as the count of marks before it and its time. Only the
    marks between this run's own start line and the next one count: a lost
    run carried in ahead (sd:3537), or a later run a hand recovery reads
    past (sd:955), proves nothing about this run's data. This run's line is
    the one stamped from `started` to `ended`. With none, two, or a start
    time that does not parse, the run is not told apart, so it is ungraded,
    and ungraded is at risk (system sd:6030 note #29319).
    """
    try:
        times = [datetime.strptime(at.decode("ascii"), "%Y-%m-%dT%H:%M:%S%z") for _, at in starts]
    except (UnicodeDecodeError, ValueError):
        return AT_RISK
    started, ended = datetime.fromisoformat(stamp(started)), datetime.fromisoformat(stamp(ended))
    ours = [number for number, at in enumerate(times) if started <= at <= ended]
    if len(ours) != 1:
        return AT_RISK
    first = starts[ours[0]][0]
    last = starts[ours[0] + 1][0] if ours[0] + 1 < len(starts) else len(marks)
    safe = next((mark.strip() for mark in marks[first:last] if mark.startswith(SAFE) and mark[len(SAFE):].strip()), None)
    return safe[:200].replace("\x00", "") if safe else AT_RISK


def ingest_log(connection, *, job, run_id, started, ended, exit_code, log_path, offset=0, now=None):
    """One cron run, recorded as an item only when something happened.

    A job that runs every fifteen minutes and succeeds every time used to file
    a report item every fifteen minutes. `claude-mem-pro-watchdog` had 102 of
    them open on the live database, every one `exit_code` 0 with `attention`
    false, and `retention` settles a clean report after seven days without
    removing the row -- so the count converges on the tick rate times seven
    days, not on anything a person needs to read.

    So a quiet tick writes a heartbeat and nothing else, which is the shape
    `retention`'s table already prescribes for heartbeats: one row per key, the
    newest being the only one any reader asks for. An item is created only
    when the job failed, or declared findings with `SD_REPORT_ATTENTION`.

    A recovery -- this tick is clean and the last one was not -- writes the
    heartbeat and says so in `why`, and files nothing (sd:3509): recovery
    reports were most of the 298 clean reports in 14 days, 85 of them from one
    flapping job. `health` reads the recovery; the failure's own report waits
    for a person. A replay of a recovery report filed before that still
    answers with the report.

    A failure that repeats an open one -- same job, host and basis -- is one
    event still counted, not a new one: it folds into the open report
    (`ingest`, sd:920) and the result says `folded` with the new `count`.

    The alternative was a per-job knob saying which jobs are noisy. That puts
    the decision in configuration a reader has to find, and it would still file
    a row for the tick where a quiet job broke.

    `now` is the clock a recorded `ended` is checked against (see `_beat`),
    an aware datetime; it defaults to the real one and exists for tests.

    The run is checked as `ingest` checks it -- the job name, the run id, an
    integer exit code, and an end not before the start -- before the log is
    read or a heartbeat written (the #315 review): the quiet path returns
    without reaching `ingest`, so its checks alone let a bad run write a
    heartbeat.
    """
    _check_identity(job, run_id)
    if type(exit_code) is not int:
        raise workflow.WorkflowError("report exit code and attention must be explicit typed values")
    _, ended = _check_times(started, ended)
    path = Path(log_path)
    if type(offset) is not int or offset < 0 or not path.is_file() or path.is_symlink():
        raise workflow.WorkflowError("report log must be a regular file with a valid starting offset")
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(MAX_REPORT + 1)
        # The body stops at `MAX_REPORT`, the findings do not (sd:4641): a
        # line past the cut was read as no finding, and a clean run with one
        # filed nothing, folded into an acknowledged report, or superseded
        # the report that still held it. Every decision below reads them all.
        stream.seek(offset)
        marks, starts = [], []
        for line in stream:
            if line.startswith(MARK):
                marks.append(line[len(MARK):].decode("utf-8", errors="replace").removesuffix("\n"))
            elif (opened := RUN_START.match(line)) and opened[1] == job.encode():
                starts.append((len(marks), opened[2]))
    truncated = len(data) > MAX_REPORT
    text = data[:MAX_REPORT].decode("utf-8", errors="replace")
    # A successful exit does not prove there were no findings. Jobs can emit
    # this explicit marker; absent it, a report filed for a clean run (a
    # recovery) says attention was not supplied. Every distinct line is the
    # basis, in order (sd:4629): a lost run's slice is carried in ahead of
    # this run's (sd:3537), and its first line alone made the basis, so the
    # whole slice folded into the lost run's report and this run's finding
    # went unseen. A failure names the host and the lines, as one job name
    # files from every host, and `job exited N` alone folded one host's or
    # one cause's failure into another's report. A basis past `ingest`'s
    # limit keeps its head and a digest of the whole, so it files. A line
    # holds no newline, so joining on one keeps two lines apart from one.
    declared = "\n".join(dict.fromkeys(mark for mark in marks if mark))
    # A failure is titled with its grade (system sd:6030); a clean exit is
    # no failure, so its findings keep `needs attention`.
    grade = _grade(marks, starts, started=started, ended=ended) if exit_code else None
    attention = exit_code != 0 or bool(declared)
    host = ship.this_host() or None
    # A carried safe line gives an ungraded failure the lines of a safe one,
    # so a failure with a safe line names its grade too, and a fold never
    # moves it under another grade's title. Without one the basis is as it
    # was, so an open report filed before the grades still takes its folds.
    # The ungraded basis is the one a run filed before the grades holds: its
    # replay is answered against it (pack sd:6151).
    graded = f" ({grade})" if exit_code and any(mark.startswith(SAFE) for mark in marks) else ""
    basis, ungraded = ((f"job exited {exit_code} on {host or ''}{suffix}" + (f": {declared}" if declared else "")
                        if exit_code else declared or "not supplied by the job") for suffix in (graded, ""))
    basis, ungraded = (value if len(value) <= 2000 else f"{value[:1900]} [sha256 {hashlib.sha256(value.encode()).hexdigest()[:16]}]"
                       for value in (basis, ungraded))
    # Each invalid byte decodes to a three-byte replacement character, so
    # this second cut can drop text the first kept: that body is cut too.
    truncated = truncated or len(text.encode()) > MAX_REPORT
    text = text.encode()[:MAX_REPORT].decode("utf-8", errors="ignore")
    with transaction(connection):
        # A run that already filed a report is answered with that report, the
        # way `ingest` answers an attention replay: a recovery's own heartbeat
        # makes the job read healthy, so its replay cannot be told by health.
        _, filed = _filed(connection, job, run_id, host)
        if filed and basis != ungraded and (json.loads(filed["fields"]).get("report") or {}).get("attention_basis") == ungraded:
            basis = ungraded
        # The newest run of this job on this host, before this one (sd:4629).
        chain_key = f"{CHAIN_KEY}{job}@{host or ''}"
        newest_row = connection.execute(
            "SELECT id, body FROM state WHERE kind='heartbeat' AND key=? ORDER BY id DESC LIMIT 1", (chain_key,)).fetchone()
        last = json.loads(newest_row["body"]) if newest_row else {}
        # Read the previous health before this tick overwrites it, or a
        # recovery reads as a run that was always fine. And only a tick that
        # became the current health can recover from anything: an older run
        # was never the tick after the failure. It is this host's (sd:10440):
        # its chain row's, else the job's when no other host wrote that, so
        # another host's clean tick is no recovery from this host's failure.
        previous = last.get("healthy")
        if type(previous) is not bool:
            latest = _current_tick(connection, HEARTBEAT_KEY + job, now) or {}
            previous = latest.get("healthy") if latest.get("host") in (None, host) else None
        clock = now or datetime.now(UTC)
        beat = _beat(connection, job=job, run_id=run_id, healthy=not attention,
                     ended=ended, exit_code=exit_code, now=clock, host=host)
        wrote = beat in ("written", "tied")
        recovered = not attention and wrote and previous is False
        # This run's own heartbeat says it was clean, and it filed no report,
        # so its first ingest took the quiet path. The run id is the key: a
        # replay that now reads a finding -- the log has grown a later run's
        # `SD_REPORT_ATTENTION` line, or the exit code was typed differently
        # -- is not a new event (the #315 review). The read runs only on such
        # a replay, never on a new run. A run named only in a `same_second`
        # list has no health of its own to read, and is left to `ingest`.
        if beat == "recorded" and attention and not filed and connection.execute(
                "SELECT 1 FROM state WHERE kind='heartbeat' AND key=?"
                " AND CASE WHEN json_valid(body) THEN json_extract(body, '$.run_id') END = ? AND " + _SAME_HOST +
                " AND CASE WHEN json_valid(body) THEN json_type(body, '$.healthy') END = 'true'",
                (HEARTBEAT_KEY + job, run_id, host)).fetchone():
            return {"recorded": "nothing", "job": job, "run_id": run_id, "healthy": False,
                    "exit_code": exit_code, "ended": ended,
                    "why": "a replay of a run whose heartbeat recorded it clean;"
                           " the run id is the key, so its changed evidence was not written"}
        # A run that ended no earlier is the newest now, ties in arrival
        # order as `_beat` ranks them; an older one, a hand recovery, takes
        # no done target and moves nothing. A replay moved it the first time,
        # and is told by its run id, not by `beat`: once compaction took its
        # heartbeat, a replay reads as a new run that ties the newest end. A
        # row stamped past `FUTURE_ALLOWANCE` outranks nothing, as in `_beat`.
        replay = beat == "recorded" or bool(filed) or bool(attention and _folded_into(connection, job, run_id, host))
        moves = not replay and not ended < (last.get("ended") or "") <= stamp((clock + FUTURE_ALLOWANCE).isoformat())
        if not (attention or filed):
            if moves:
                _set_chain(connection, chain_key, newest_row, {"run_id": run_id, "ended": ended, "item": None, "healthy": True})
                if host:
                    _supersede(connection, job=job, host=host, by=None, recovered=f"{job}:{run_id}")
            return {"recorded": "heartbeat" if wrote else "nothing", "job": job, "run_id": run_id,
                    "healthy": True, "exit_code": exit_code, "ended": ended,
                    "why": "the job recovered" if recovered else _quiet(beat, previous)}
        state = ingest(connection, job=job, run_id=run_id, started=started, ended=ended,
                       exit_code=exit_code, text=text, source_path=str(path.resolve()),
                       attention=attention, attention_basis=basis, truncated=truncated,
                       chain=last.get("item") if moves and type(last.get("item")) is int else None, now=clock,
                       host=host, grade=grade)
        # A truncated body is not the whole set to show in place of the older
        # report's, even with every finding named in the basis: neither this
        # run's nor the one the report it folded into kept. A whole run
        # supersedes whether it filed or folded: a cut run left the older
        # report open, and its whole repeats fold.
        if moves and host and attention and not exit_code and not truncated and not _kept_cut(state["item"]):
            _supersede(connection, job=job, host=host, by=state["item"]["id"])
        if moves:
            _set_chain(connection, chain_key, newest_row, {
                "run_id": run_id, "ended": ended, "item": state["item"]["id"] if attention and not exit_code else None,
                "healthy": not attention})
        if state["item"]["external_id"] != _filed(connection, job, run_id, host)[0]:
            # `ingest` answered with another run's report: this run folded
            # into it (sd:920), or is the replay of one that did.
            return {"recorded": "folded", "why": basis,
                    "count": json.loads(state["item"]["fields"])["repeats"]["count"], **state}
        return {"recorded": "report", "why": basis if attention else "the job recovered", **state}


#: An open report: not done, and `fields.attention` is not false; fields that do not parse ask for a person too.
OPEN_REPORT = ("status != 'done' AND COALESCE(CASE WHEN json_valid(fields)"
               " THEN json_type(fields, '$.attention') END, '') != 'false'")


def reports(connection, *, limit=200, open_only=False):
    """The newest reports, at most 200; `open_only` filters before the limit, so an old open one is not cut (sd:8795)."""
    where = " AND " + OPEN_REPORT if open_only else ""
    return [dict(row) for row in connection.execute(
        "SELECT * FROM item WHERE kind='report' AND source IS NOT 'brief'" + where
        + " ORDER BY created_at DESC,id DESC LIMIT ?", (min(200, max(1, limit)),))]


def _refuse_brief(row):
    """A routine brief is a `report` row filed `done` for good (sd:3459): no verb moves it."""
    if row["source"] == "brief":
        raise workflow.WorkflowError("a routine brief stays done; only filing or replacing the brief changes it")


def acknowledge(connection, item, *, expected_revision, who, resolve_followups=False):
    """Move one report to `done` on the operator's say-so, naming the operator.

    `who` has no default. The `transition` this makes is the one thing the
    store keeps about who ended the report, and a default writes a name that
    is true of every caller and identifies none of them: 281 reports went
    `planning -> done by user` in a single second, and the row cannot say what
    ran them. Both shipped callers already name themselves -- the dashboard
    `"dashboard"`, the pack CLI the login account -- so the only caller a
    missing argument ever served was an unattributed script, which is the one
    this refuses.

    An unresolved followup refuses the move, and the refusal names each note
    as `resolve followup <id>`, the action that closes one (sd:3503). `ingest` opens such
    a followup on every report for a run that needs attention, under
    `INGEST_SESSION`, so every failure report is born refusing this verb
    (sd:921: 51 watchdog reports on the live store, none acknowledgeable).
    `resolve_followups=True` resolves every open followup the report owns --
    the ingest's and a person's -- in the transaction that makes the
    transition, so a refusal or a failed write leaves them open, and the
    status note says which notes it resolved under the same `who`.
    `resolve_note` itself records only `resolved_at`; the status note is
    where the name lives. Without the keyword a person's followup is work
    somebody still owes and blocks: acknowledging the report is the
    operator's say-so, so the keyword is the operator's statement that the
    work is done or dropped (sd:4722). The keyword is exactly `True` or
    `False`, as `ingest` takes `attention`: it changes what is persisted, so
    a truthy string does not get to mean yes.
    """
    if type(resolve_followups) is not bool:
        raise workflow.WorkflowError("resolve_followups must be exactly True or False")
    with transaction(connection):
        state = workflow._checked_state(connection, item, expected_revision)
        if state["item"]["kind"] != "report":
            raise workflow.WorkflowError("only report items can be acknowledged here")
        _refuse_brief(state["item"])
        open_followups = [note for note in state["notes"] if note["kind"] == "followup" and not note["resolved_at"]]
        resolving = [note["id"] for note in open_followups] if resolve_followups else []
        blocking = [note["id"] for note in open_followups if note["id"] not in resolving]
        if blocking:
            raise workflow.WorkflowError("resolve the report's followups before acknowledging it: "
                                         + "; ".join(f"resolve followup {note}" for note in blocking))
        for note in resolving:
            resolve_note(connection, note)
        reason = ("resolved followup" + ("s " if len(resolving) > 1 else " ")
                  + ", ".join(str(note) for note in resolving)) if resolving else None
        transition(connection, item, "done", who=who, reason=reason)
        return workflow.item_state(connection, item)



def reopen(connection, item, *, expected_revision, who, reason=None):
    """Undo `acknowledge`: move one `done` report back, naming the operator (sd:2395).

    The report returns to the status its newest `<status> -> done` history
    note names, which is the status it was acknowledged from; with no such
    note, or one naming no known status, it returns to `planning`, the
    status every report is filed in. A report that is not `done` is
    returned as it stands: an Undo that races a reload changes nothing.

    `who` has no default, as on `acknowledge`. Followups that the
    acknowledgement resolved stay resolved: `resolve_followups`
    records them in the status note, and this verb does not guess which
    ones to open again. The dashboard's Acknowledge never resolves any.
    """
    who = workflow._text(who, "who")
    if reason is not None:
        reason = workflow._text(reason, "reason")
    with transaction(connection):
        state = workflow._checked_state(connection, item, expected_revision)
        if state["item"]["kind"] != "report":
            raise workflow.WorkflowError("only report items can be reopened here")
        _refuse_brief(state["item"])
        if state["item"]["status"] != "done":
            return state
        target = "planning"
        for note in reversed(state["notes"]):
            match = note["kind"] == "status_change" and re.match(r"(\w+) -> done by ", note["body"])
            if match:
                if match[1] in STATUSES and match[1] != "done":
                    target = match[1]
                break
        transition(connection, item, target, who=who, reason="reopened" + (f": {reason}" if reason else ""))
        return workflow.item_state(connection, item)

def clean_candidates(connection, *, before):
    """The reports retention's rule calls clean at `before`, in id order.

    `kind='report'`, `status='planning'`, `fields.attention` exactly 0 (JSON
    false, or the number), `created_at` earlier than `before`, no
    unresolved followup, and no assignment in `runner.OWNING`: a runner or
    a teammate run still owns the report (pack sd:7284). Retention settles
    these at seven days; the bulk acknowledge starts from the same rows and
    refuses more, so the two cannot drift apart.

    `json_extract` sits inside `CASE WHEN json_valid(...)`, as in `_beat`: a
    `CASE` evaluates its branch only when the test is true, whatever order the
    query plan picks for `AND` terms, so a row whose `fields` is not JSON is
    not a candidate instead of failing the whole read.

    No transaction and no savepoint: the caller owns the read. Retention reads
    under its write lock, the dry run under one snapshot.
    """
    owning = ", ".join("?" for _ in runner.OWNING)
    return [row["id"] for row in connection.execute(
        "SELECT id FROM item WHERE kind='report' AND status='planning'"
        " AND CASE WHEN json_valid(fields) THEN json_extract(fields, '$.attention') END = 0"
        " AND created_at < ?"
        " AND NOT EXISTS (SELECT 1 FROM note WHERE note.item = item.id"
        "                 AND note.kind='followup' AND note.resolved_at IS NULL)"
        " AND NOT EXISTS (SELECT 1 FROM assignment WHERE assignment.item = item.id"
        f"                 AND assignment.status IN ({owning}))"
        " ORDER BY id", (before, *runner.OWNING))]


def cutoff(value):
    """The one `before` both surfaces pass on: a date, meaning 00:00 UTC.

    `YYYY-MM-DD` becomes `YYYY-MM-DDT00:00:00+00:00`, and that exact stamp is
    returned as it is, so the stamped value can travel through a hidden field
    or a pasted command and come back unchanged. Everything else is refused:
    another time or offset, `Z`, a date that does not exist. `clean_reports`
    takes only the stamp this returns, and refuses even another spelling of
    the same instant, so a caller that skipped this fails there rather than
    getting a different cutoff, or a different plan token, by accident
    (sd:1168).
    """
    match = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(T00:00:00\+00:00)?", value) if isinstance(value, str) else None
    if match:
        try:
            date.fromisoformat(match[1])
        except ValueError:
            pass
        else:
            return match[1] + "T00:00:00+00:00"
    raise workflow.WorkflowError("give the cutoff as a date, YYYY-MM-DD, meaning 00:00 UTC")


#: A `fields` that cannot say the report is clean, in SQL over the `item`
#: table. One meaning for the three shapes the nullable, unchecked column can
#: hold besides a document: NULL, the empty string and text that is not JSON
#: (sd:873). Such a report is for a person to look at. Retention never settles
#: it and names it in the prune report; the bulk clean declines it; the
#: backlog does not list it on its own word, because it has none; the page
#: renders it, says why it is here and withholds the one-click acknowledge,
#: so the command line, whose acknowledge takes a named `who`, finishes it.
#: `json_valid(NULL)` is NULL and `json_valid('')` is 0, so `NOT json_valid`
#: alone would miss the first: the `coalesce` is what makes the three one.
#: `retention` reads this name, not a copy, so the two cannot drift; the
#: tests at each site pin the same three inputs.
UNREADABLE_FIELDS = "coalesce(json_valid(fields), 0) = 0"


class UnreadableFields(workflow.WorkflowError):
    """A stored `fields` that `UNREADABLE_FIELDS` says no reader can read."""


def _not_json(constant):
    # `json.loads` accepts `NaN`, `Infinity` and `-Infinity`; `json_valid`
    # does not. SQLite has already refused them by the time this runs, so
    # this is the belt to that brace.
    raise UnreadableFields(f"{constant} is not JSON")


def _json_int(token):
    # `int` refuses a decimal string of 4300 digits or more (Python's
    # `sys.int_max_str_digits`), and `json_valid` accepts one. `Decimal`
    # holds the same exact value without that limit and prints it back
    # digit for digit, so a reader shows it rather than refusing the row.
    try:
        return int(token)
    except ValueError:
        return Decimal(token)


def fields_document(connection, value):
    """The stored `fields` parsed, or `UnreadableFields` when SQLite says so.

    For a reader that shows a report -- the dashboard's item page -- and must
    give the verdict retention and the bulk clean give. It asks SQLite first,
    with `UNREADABLE_FIELDS` over the value, so the verdict is theirs by
    construction and not Python's: `json.loads` accepts `NaN` and `Infinity`,
    which `json_valid` refuses, and it reads nesting past `json_valid`'s
    depth limit (1000 in SQLite 3.53) up to its own recursion limit, where it
    raises `RecursionError` instead (the review of #395, both rounds). An
    integer of 4300 or more digits, which `json_valid` accepts and `int`
    refuses, parses as a `Decimal` of the same value (`_json_int`), so the
    page agrees with retention there too (the third round). The
    query runs on `connection` because the library is the one thing that
    issues SQL; it reads no table, so a read-only connection serves.
    """
    unreadable = connection.execute(
        f"SELECT {UNREADABLE_FIELDS} FROM (SELECT ? AS fields)", (value,)).fetchone()[0]
    if unreadable:
        raise UnreadableFields("the stored fields are missing or not valid JSON")
    try:
        return json.loads(value, parse_constant=_not_json, parse_int=_json_int)
    except (TypeError, ValueError, RecursionError) as error:
        raise UnreadableFields(f"the stored fields could not be parsed: {error}") from error


#: Why a `planning` report created before the cutoff is not selected, in the
#: order the reasons are tested. The first that applies is the one given.
DECLINED = (
    ("unreadable", "its fields are not valid JSON, so it cannot say it is clean"),
    ("record", "an operator record, not a run report"),
    ("attention", "it needs attention, which waits for a person"),
    ("unclean", "it never said it was clean"),
    ("followup", "it has an unresolved followup"),
    ("assignment", "work on it is queued, running or ending"),
    ("late", "its run ended after the job's newest recorded tick"),
)


def clean_reports(connection, *, before, now=None):
    """The dry run of a bulk acknowledge: what it would move, what it declines.

    Writes nothing and opens no transaction. Every read runs between one
    `SAVEPOINT` and its `RELEASE`, the way `workflow.item_state` reads, so all
    rows come from one snapshot and the call works on a `write=False`
    connection, where `BEGIN IMMEDIATE` does not.

    `before` is the stamp `cutoff` returns, exactly, and may not be later
    than `now`, an aware `datetime` that defaults to the real clock. `selected` is
    `clean_candidates` less the refusals retention does not make: a record
    marker, a run newer than its job's recorded health. Work in flight both
    refuse; this read names it as the reason.
    `declined` is every other `planning` report created before `before`, with
    the first reason that applies; a later or already-moved report is in
    neither list.

    `plan` is a sha256 over `before` and each selected id with its revision,
    so it changes when a report joins or leaves the selection or a listed one
    changes. It is `None` when nothing is selected or more than `MAX_BATCH`
    is, because no apply of that selection can succeed, and the revisions are
    then not read.
    """
    if cutoff(before) != before:
        raise workflow.WorkflowError(f"pass the cutoff as `cutoff` stamps it, {cutoff(before)}, not {before!r}")
    current = stamp((now or datetime.now(UTC)).isoformat())
    if before > current:
        raise workflow.WorkflowError(f"the cutoff {before} is later than now ({current}); give a date that has begun")
    connection.execute("SAVEPOINT sd_clean_reports")
    try:
        candidates = set(clean_candidates(connection, before=before))
        active = ", ".join("?" for _ in runner.OWNING)
        # The first five reasons in one read. Every JSON function sits inside
        # `CASE WHEN json_valid(fields)`, so no row can fail the query.
        rows = connection.execute(
            "SELECT id, title, created_at,"
            " CASE WHEN json_valid(fields) THEN json_extract(fields, '$.report.job') END AS job,"
            " CASE WHEN json_valid(fields) THEN json_extract(fields, '$.report.ended') END AS ended,"
            f" {UNREADABLE_FIELDS} AS unreadable,"
            " CASE WHEN json_valid(fields) THEN json_type(fields, '$.record') END IS NOT NULL AS record,"
            " CASE WHEN json_valid(fields) THEN json_extract(fields, '$.attention') END = 1 AS attention,"
            " CASE WHEN json_valid(fields) THEN json_extract(fields, '$.attention') END IS NOT 0 AS unclean,"
            " EXISTS (SELECT 1 FROM note WHERE note.item = item.id"
            "         AND note.kind='followup' AND note.resolved_at IS NULL) AS followup,"
            " EXISTS (SELECT 1 FROM assignment WHERE assignment.item = item.id"
            f"         AND assignment.status IN ({active})) AS assignment"
            " FROM item WHERE kind='report' AND status='planning' AND created_at < ? ORDER BY id",
            (*runner.OWNING, before)).fetchall()
        beats = {}
        selected, declined = [], []
        for row in rows:
            reasons = {key for key, _ in DECLINED[:6] if row[key]}
            job, ended = row["job"], row["ended"]
            if isinstance(job, str) and isinstance(ended, str):
                if job not in beats:
                    # The newest row by id, the one `health` and `_beat` read.
                    beat = connection.execute(
                        "SELECT CASE WHEN json_valid(body) THEN json_extract(body, '$.ended') END"
                        " FROM state WHERE kind='heartbeat' AND key=? ORDER BY id DESC LIMIT 1",
                        (HEARTBEAT_KEY + job,)).fetchone()
                    beats[job] = beat[0] if beat else None
                if isinstance(beats[job], str) and ended > beats[job]:
                    reasons.add("late")
            if row["id"] in candidates and not reasons & {"record", "assignment", "late"}:
                selected.append({"id": row["id"], "revision": None, "job": job,
                                 "created_at": row["created_at"], "title": row["title"]})
            else:
                declined.append({"id": row["id"], "why": next((why for key, why in DECLINED if key in reasons),
                                                              DECLINED[3][1])})
        plan = None
        if 1 <= len(selected) <= MAX_BATCH:
            for entry in selected:
                entry["revision"] = workflow.item_state(connection, entry["id"])["revision"]
            token = json.dumps([before, sorted([entry["id"], entry["revision"]] for entry in selected)],
                               separators=(",", ":"))
            plan = hashlib.sha256(token.encode()).hexdigest()
    finally:
        connection.execute("RELEASE sd_clean_reports")
    return {"before": before, "selected": selected, "declined": declined, "count": len(selected),
            "declined_count": len(declined), "plan": plan, "max_batch": MAX_BATCH}


def acknowledge_clean(connection, *, before, expected_plan, who, principal, program, session=None, now=None):
    """Move the previewed clean reports to `done`, all or none, naming who did.

    `expected_plan` is the `plan` of `clean_reports` at the same `before`.
    Inside one `BEGIN IMMEDIATE` the selection and its plan are computed
    again; any difference, including an empty or oversized selection, whose
    preview issues no plan, is `StaleItem` and writes nothing.

    That sentence is about the selection computed here, not about the token
    passed in. `expected_plan` must be a plan, 64 lowercase hex digits. A
    preview's `None` is its answer "no apply can succeed", not a token: the
    dashboard renders no apply form for it and refuses any other plan
    shape, and the CLI prints no apply command. So `None`, like any other
    malformed token, is a bad request refused before the transaction
    (`WorkflowError`, not `StaleItem`; the #347 review asked otherwise).

    Then one batch report is filed as job `reports-acknowledge`, with the plan
    as its run id, the moved ids as its text, `who`, the `principal` the
    caller's channel authenticated, the program and the process as its
    `actor`, and the `record` marker. Each report moves with a note naming
    that batch report, and the batch report itself moves to `done`, so it is
    never a `planning` report for retention or a later bulk run to take.

    `who` and `principal` have no default, for the reason `acknowledge` gives.
    `session` is not a name the caller chooses, so an over-long one is cut to
    200 characters rather than refused.
    """
    if not isinstance(expected_plan, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_plan):
        raise workflow.WorkflowError("preview the clean reports and pass its plan")
    who, principal, program = (workflow._text(value, name) for value, name in
                               ((who, "who"), (principal, "principal"), (program, "program")))
    for value, name in ((who, "who"), (principal, "principal"), (program, "program")):
        if len(value) > 200:
            raise workflow.WorkflowError(f"{name} must be at most 200 characters")
    if session is not None and not isinstance(session, str):
        raise workflow.WorkflowError("session must be text")
    session = session[:200] if session and session.strip() else None
    now = now or datetime.now(UTC)
    stamped_now = now.astimezone(UTC).isoformat(timespec="seconds")
    with transaction(connection):
        preview = clean_reports(connection, before=before, now=now)
        if preview["plan"] != expected_plan:
            raise workflow.StaleItem("the clean-report selection changed since the preview; preview it again")
        ids = [entry["id"] for entry in preview["selected"]]
        if not 1 <= len(ids) <= MAX_BATCH:
            raise workflow.WorkflowError(f"a bulk acknowledge moves between 1 and {MAX_BATCH} reports")
        actor = {"who": who, "principal": principal, "program": program,
                 "pid": os.getpid(), "ppid": os.getppid(), "session": session}
        batch = ingest(connection, job=BULK_JOB, run_id=expected_plan, started=stamped_now, ended=stamped_now,
                       exit_code=0, text="".join(f"{item}\n" for item in ids), source_path=program,
                       attention=False, actor=actor, record=BULK_JOB)["item"]["id"]
        for item in ids:
            transition(connection, item, "done", who=who, reason=f"bulk acknowledge, report #{batch}")
        transition(connection, batch, "done", who=who, reason="bulk acknowledge record")
        state = workflow.item_state(connection, batch)
        return {"item": state["item"], "revision": state["revision"], "acknowledged": ids, "actor": actor}


def observed_action(connection, action, *, item=None, channel="dashboard"):
    """Count accepted requests explicitly; a browser request is not human labor."""
    record_state(connection, "checkpoint", key="operator-action",
                 body={"action": action, "item": item, "channel": channel})


def metrics(connection, *, now):
    since, until = reads._week(now)
    upper = until + "T23:59:59Z"
    actions = connection.execute("SELECT count(*) FROM state WHERE kind='checkpoint' AND key='operator-action' AND timestamp BETWEEN ? AND ?", (since, upper)).fetchone()[0]
    reviews = connection.execute("SELECT count(*) FROM assignment WHERE role='reviewer' AND ended BETWEEN ? AND ?", (since, upper)).fetchone()[0]
    delivered = connection.execute("SELECT count(*) FROM item WHERE shipped_at BETWEEN ? AND ?", (since, upper)).fetchone()[0]
    return {"since": since, "until": until, "recorded_requests": actions,
            "finished_review_attempts": reviews, "recorded_deliveries": delivered,
            "interpretation": "Observed database events. Requests may come from a person or an agent; they do not measure human interventions, time saved or adoption."}
