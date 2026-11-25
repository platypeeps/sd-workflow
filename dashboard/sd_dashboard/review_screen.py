"""Review (sd:3709): the labelling queue's page, one row per question (design docs/work/2026-10-12-review-queue).

A batch is a task filed under `review:<queue>:<batch>` by `sd-db review file`; its questions are question notes and a
label is the Decisions ruling. Every read and write here is `sd_db.review`'s, the code `sd-db review` runs, so the page
and the CLI verb share one path (operator ruling Q3); the page adds no store and no second writer.

- `document` is `/api/review`: each open batch, nightly suggestions first and then by age, with the revision read
  before the batch, so a change between the two makes the page stale (409), never newer than what it shows; each open
  question; and in `reviewed` each ruled or expired one, with `label` and `ruling`. A blind or sealed batch sends the
  question text, options and label only: no subject and no evidence.
  A batch whose notes do not read as one is listed with its reason, never dropped.
  Each batch sends its `context` (purpose, rubric, feeds; None when filed with none), and a question its `recommendation`
  (option and reason) when it has one (sd:5759, pack sd:5763). A blind or sealed batch sends a recommendation only on a
  question its standing label ruled, with `agreed`: an open or expired question never carries it, so its label stays
  independent. A pack without the fields sends neither.
- `rule_request` is `/api/review/rule`: `review.rule`, under the batch revision the page read. Its reply names the
  question's recommendation and whether the label agreed (both None when it has none), now that the label is in.
- `withdraw_request` is `/api/review/withdraw`: `review.withdraw`, the undo.
- `recommendations` is `/api/briefs/<item>/recommendations` (sd:5064): the questions of each batch an adversarial review's
  brief names in its frontmatter (`batches: sd:N, ...`), ruled and open, with each batch's revision, so the Briefs page
  can rule the chosen ones Approve through `/api/review/rule`. A repository is sent by name, never by its local path.
- `sweeps` is `/api/review`'s `sweeps` (sd:9505): the weekly advisory sweep, one row per week, newest first: the
  RESULT line and the cap's `sampled N of M` from the job's log, and its filed items (`advisory-sweep:<date>:<repo>`
  refs) done and not done, each keyed on its week's ending Sunday, the day the job runs. A week with no RESULT keeps None, never zero; a log that does not read keeps the items.
- `export` is `/api/review/<item>/export`: `review.export` without a key, the lines `sd-db review export` writes.
  A blind or sealed batch exports only once no question is open, so the page never shows a subject while labelling.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3

from sd_db import reads, workflow

__all__ = ["BLIND", "NIGHTLY", "SWEEP_JOB", "document", "export", "recommendations", "rule_request", "sweeps",
           "withdraw_request"]

#: The queue the nightly review job files (`sd-db review apply nightly`); its suggestions come first.
NIGHTLY = "nightly"
#: Modes whose questions show text and options only.
BLIND = ("blind", "sealed")
MAX_ID = 9223372036854775807
#: The weekly advisory sweep (code-review-research sd:9494) and the ref prefix of the items it files.
SWEEP_JOB = "advisory-sweep-weekly"
SWEEP_REF = "advisory-sweep:"
# ponytail: the whole log is read up to 1 MB, about 200 weekly runs; read backwards if it ever grows past that.
SWEEP_LOG_BYTES = 1 << 20
SWEEP_START = re.compile(rf"^\[{SWEEP_JOB}\] (\d{{4}}-\d{{2}}-\d{{2}})T\S* starting", re.M)
SWEEP_RESULT = re.compile(r"^RESULT: (\d+) packets, (\d+) open, (\d+) filed\s*$", re.M)
SWEEP_CAP = re.compile(r"sampled (\d+) of (\d+)")


def _rows(connection):
    """The open batch items, oldest first: the Tasks row set (`reads.backlog_items`) under `review`'s ref, as Today counts."""
    from .tasks_screen import is_scheduled

    rows = [row for row in reads.backlog_items(connection, include_done_days=0)
            if row["status"] != "done" and not is_scheduled(row)
            and reads.is_review_batch(row["source"], row["external_id"])]
    return sorted(rows, key=lambda row: (row["created_at"], row["id"]))


def document(connection) -> dict:
    from sd_db import review

    batches, questions, reviewed = [], [], []
    rows = _rows(connection)
    # One batched revision read for the page; `review.read` still reads each batch's notes (sd:3531).
    revisions = reads.revisions(connection, [row["id"] for row in rows])
    for row in rows:
        item = row["id"]
        revision = revisions[item]
        try:
            found = review.read(connection, item)
        except review.ReviewError as problem:
            batches.append({"item": item, "title": row["title"], "created": row["created_at"], "nightly": False,
                            "error": str(problem)})
            continue
        if found["closed"] is not None:
            continue
        header = found["header"]
        blind = header["mode"] in BLIND
        batches.append({"item": item, "title": row["title"], "created": row["created_at"], "ref": found["ref"],
                        "queue": header["queue"], "name": header["batch"], "mode": header["mode"],
                        "source": header["source"], "nightly": header["queue"] == NIGHTLY, "revision": revision,
                        "questions": len(found["questions"]), "open": found["open"], "labels": found["labels"],
                        "context": header.get("context"), "error": None})
        for question in found["questions"]:
            shown = {"item": item, "note": question["note"], "text": question["text"],
                     "options": question["options"], "asked": question["asked_at"]}
            if not blind:
                shown.update(subject=question["subject"], evidence=question["evidence"])
            shown.update(_advice(question, blind))
            if question["open"]:
                questions.append(shown)
            else:
                # Show reviewed (sd:5123): a ruled or expired question, with its label and ruling (None when expired).
                # Its own list, so a page on the previous build, which reads every entry in `questions` as open, never
                # offers it as open.
                reviewed.append({**shown, "open": False, "label": question["label"], "ruling": question["ruling"]})
    order = {batch["item"]: number for number, batch in
             enumerate(sorted(batches, key=lambda batch: not batch["nightly"]))}
    batches.sort(key=lambda batch: order[batch["item"]])
    for found_list in (questions, reviewed):
        found_list.sort(key=lambda question: (order[question["item"]], question["note"]))
    # The rail badge (sd:3446): every open question in a batch that reads. The page keeps its own badge current between
    # reads as its labels land, from the same batches' `open`.
    from .v2.pages import attention

    found = attention(["caution"] * sum(batch.get("open") or 0 for batch in batches), {"caution": "questions open"})
    return {"batches": batches, "questions": questions, "reviewed": reviewed, "attention": found}


def sweeps(connection, cron_root) -> dict:
    """The advisory sweep by week (sd:9505): `state` and `reason` as `management_screen.job_log` gives them, and `weeks`."""
    from .management_screen import job_log

    # One read: a second open of a log rotated in between raised past job_log's error handling (sd:9505 review).
    log = job_log(cron_root, SWEEP_JOB, SWEEP_LOG_BYTES)
    text = log["text"]
    weeks: dict[str, dict] = {}

    def week(date):
        # One sweep week ends on the Sunday the job runs (05:30): a run or a ref dated Monday to Sunday keys on that
        # Sunday, so a rerun by hand mid-week and the Sunday run are one row (sd:9505 review). A date that is no
        # calendar date keys nowhere: its run or item is left out, not the page.
        try:
            day = datetime.date.fromisoformat(date) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None
        except ValueError:
            day = None
        if day is None:
            return None
        date = (day + datetime.timedelta(days=(6 - day.weekday()) % 7)).isoformat()
        return weeks.setdefault(date, {"week": date, "runs": 0, "packets": None, "open": None, "filed": None,
                                       "skipped": None, "fixed": 0, "unfixed": 0})

    starts = list(SWEEP_START.finditer(text))
    for n, start in enumerate(starts):
        block = text[start.end():starts[n + 1].start() if n + 1 < len(starts) else len(text)]
        row = week(start[1])
        if row is None:
            continue
        row["runs"] += 1
        results = SWEEP_RESULT.findall(block)
        if results:
            # A rerun in one week only adds (the script records what it swept), so its counts add too.
            for name, value in zip(("packets", "open", "filed"), results[-1], strict=True):
                row[name] = (row[name] or 0) + int(value)
        caps = SWEEP_CAP.findall(block)
        if caps:
            row["skipped"] = (row["skipped"] or 0) + int(caps[-1][1]) - int(caps[-1][0])
    for found in reads.ref_items(connection, SWEEP_REF):
        row = week(found["external_id"][len(SWEEP_REF):].partition(":")[0])
        if row is not None:
            row["fixed" if found["status"] == "done" else "unfixed"] += 1
    return {"state": log["state"], "reason": log["reason"],
            "weeks": sorted(weeks.values(), key=lambda row: row["week"], reverse=True)}


def _advice(question: dict, blind: bool) -> dict:
    """A question's recommendation as the page may see it (sd:5759): in a blind or sealed batch only once a standing label
    ruled it, with whether that label agreed; in an open batch at once."""
    advice, label = question.get("recommendation"), question["label"]
    if advice is None or (blind and label is None):
        return {}
    return {"recommendation": advice, **({} if label is None else {"agreed": question.get("agreed")})}


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return {}
    return dict(line.split(": ", 1) for line in lines[1:lines.index("---", 1)] if ": " in line)


def recommendations(connection, item: int):
    """An adversarial brief's batches and every question in them, or `(404, error)` for any other item."""
    from sd_db import review

    try:
        row = workflow.item_state(connection, item)["item"]
        found = json.loads(row["body"] or "{}") if row["source"] == "brief" else {}
    except (workflow.MissingItem, ValueError):
        found = {}
    head = _frontmatter(found.get("text") or "" if isinstance(found, dict) else "")
    if head.get("review") != "adversarial":
        return 404, {"error": f"sd:{item} is not an adversarial review's brief"}
    ids = [int(n) for n in re.findall(r"sd:([1-9][0-9]{0,18})", head.get("batches", "")) if int(n) <= MAX_ID]
    # Read before each batch, as `document` does: a change between the two makes a rule stale, never newer.
    revisions = reads.revisions(connection, ids)
    batches, questions = [], []
    for batch in ids:
        try:
            read = review.read(connection, batch)
        except (review.ReviewError, workflow.MissingItem) as problem:
            batches.append({"item": batch, "repo": None, "revision": None, "open": 0, "error": str(problem)})
            continue
        repo = os.path.basename((read["item"]["repo"] or "").rstrip("/")) or None
        batches.append({"item": batch, "repo": repo, "revision": revisions.get(batch), "open": read["open"], "error": None})
        questions += [{"batch": batch, "note": q["note"], "title": q["text"].splitlines()[0], "repo": repo,
                       "label": q["label"], "filed": q["filed"], "open": q["open"], "options": q["options"]}
                      for q in read["questions"]]
    return {"brief": item, "batches": batches, "questions": questions}


def _ids(payload: dict, names: tuple[str, ...]) -> bool:
    return all(type(payload[name]) is int and 1 <= payload[name] <= MAX_ID for name in names)


def _revision(payload: dict) -> bool:
    return isinstance(payload["revision"], str) and re.fullmatch(r"[a-f0-9]{64}", payload["revision"]) is not None


def rule_request(payload: dict):
    """The label POST's checks, before the server opens a writer; the write as `action_route` returns one."""
    if (set(payload) != {"item", "note", "option", "revision"} or not _ids(payload, ("item", "note"))
            or not _revision(payload) or not isinstance(payload["option"], str) or not payload["option"]):
        raise ValueError("Send the batch item, the question note, the option the page showed and the revision it read.")

    def write(connection):
        from sd_db import review

        state = review.rule(connection, payload["item"], payload["note"], payload["option"],
                            expected_revision=payload["revision"], who="dashboard")
        # The label is in, so a blind batch may now show what was recommended (sd:5759). A read that fails here costs only
        # that line: the label landed, and the page's next read shows the recommendation.
        try:
            ruled = [q for q in review.read(connection, payload["item"])["questions"] if q["note"] == payload["note"]]
        except (review.ReviewError, sqlite3.Error):
            ruled = []
        advice = _advice(ruled[0], blind=True) if ruled else {}
        return {"item": payload["item"], "note": payload["note"], "revision": state["revision"],
                "ruling": state["ruling"]["id"], "recommendation": advice.get("recommendation"),
                "agreed": advice.get("agreed")}

    return write


def withdraw_request(payload: dict):
    """The undo POST's checks; the write withdraws the ruling and reopens its question."""
    if set(payload) != {"item", "ruling", "revision"} or not _ids(payload, ("item", "ruling")) or not _revision(payload):
        raise ValueError("Send the batch item, the ruling note to withdraw and the revision the label returned.")

    def write(connection):
        from sd_db import review

        state = review.withdraw(connection, payload["item"], payload["ruling"],
                                expected_revision=payload["revision"], who="dashboard")
        return {"item": payload["item"], "revision": state["revision"], "withdrawal": state["withdrawal"]["id"]}

    return write


def export(connection, item: int):
    """The batch's export lines, or `(status, error)` when it is no batch or a blind one still has open questions."""
    from sd_db import review

    try:
        found = review.read(connection, item)
        if found["header"]["mode"] in BLIND and found["open"]:
            return 409, {"error": f"sd:{item} is {found['header']['mode']} and has {found['open']} open; "
                                  "its export waits until every question is ruled"}
        lines = review.export(connection, item, who="dashboard")
    except workflow.MissingItem as problem:
        return 404, {"error": str(problem)}
    except review.ReviewError as problem:
        return 400, {"error": str(problem)}
    header = lines[0]
    return {"name": f"review-{header['queue']}-{header['name']}.jsonl", "lines": lines}
