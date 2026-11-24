"""Draft runs: the headless lead drafts a spec for each captured item (pack sd:6361).

Design: system `docs/work/2026-10-25-lead-drafts-specs/design.md` (system sd:6013).

A task the dashboard captures in a managed repository with a remote, whose
lane runs on the hub (`repo_refusal`), queues one `assignment` of
scope `draft` in the capture's own transaction (`queue_if_eligible`). No
other path queues one: no `sd task add`, no backlog pass (ruling #31096).

The build-dispatch tick claims one draft at a time (`claim`): one
`runner_run` at step `claimed` on branch `draft/sd-<item>`, which names no
git branch, with no lease, so it counts against the machine cap only
(`build.room`). The item stays `planning`; its running assignment refuses
status writes until the run ends (`workflow.change_status`).

`build_run.DraftSupervisor` runs one read-only `claude -p` in the
repository's checkout, judges its last stream line, and hands the reply to
`parse`. `write` lands the spec and its questions in one compare-and-set on
the run, so they appear all at once or not at all, and a late supervisor
whose run was reaped writes nothing. Any other end goes through `end`:
a loss retries once as a fresh run, then blocks the assignment with a
`Draft blocked:` followup; an unparsed reply blocks it with no note.
"""

from __future__ import annotations

import json
import re
import uuid

from . import build, paths
from .build import LOSSES, REAP_READS, RETRIES, Probe, kill_group
from .database import transaction
from .runner import RUNNABLE_KINDS, restoration_pending
from .writes import add_note, create_assignment, recommended
from .writes import now as clock

SCOPE = build.DRAFT
BUDGET_MINUTES = 15
#: The steps a draft run walks; `write` or `end` ends it.
STEPS = build.DRAFT_STEPS
MAX_TURNS = 60
#: The tools a draft holds: read only, so its notes pass through `parse` and nothing else.
TOOLS = ("Read", "Grep", "Glob")
QUESTIONS_MAX = 3
#: The comment a draft's spec starts with, and the session prefix of its notes: the status guard reads it.
SPEC = "Draft spec:"
#: The comment a design run's design starts with (sd:10096): the brief of the code bundle it rides reads it.
DESIGN = "Design:"
SESSION = "draft:"
BLOCKED = "Draft blocked:"

_SAME = " AND ".join(f"{name} IS ?" for name in REAP_READS)
_HEADING = re.compile(r"^##[ \t]+(Spec|Questions)[ \t]*$", re.IGNORECASE)
_OPTION = re.compile(r"^[ \t]*Option:[ \t]*(.*?)[ \t]*$")
_NONE = re.compile(r"^[ \t]*none\.?[ \t]*$", re.IGNORECASE)


def branch(item: int) -> str:
    """The run row's branch: a name only, as `runner_run.branch` is NOT NULL; no git branch is made."""
    return f"draft/sd-{item}"


def session(ident: str) -> str:
    return f"{SESSION}{ident}"


def queue_if_eligible(connection, item: int, *, who: str) -> int | None:
    """Queue one draft of a captured `item`, or None when it takes none.

    The item takes one when it is a runnable kind in `planning` and its
    repository passes `repo_refusal` (managed, a remote, a hub lane).
    The caller holds the capture's transaction, so the item and its draft
    land together or not at all.
    """
    with transaction(connection):
        row = connection.execute(build._REPO, (item,)).fetchone()
        if (row is None or row["kind"] not in RUNNABLE_KINDS or row["status"] != "planning"
                or repo_refusal(row, connection) is not None or restoration_pending(connection)):
            return None
        return _queue(connection, item, who=who, what="Draft")


def queue_design(connection, item: int, *, who: str) -> int:
    """Queue the design run of a design item (`build.DESIGN_KIND`, sd:10096); `build.queue` checked it."""
    return _queue(connection, item, who=who, what="Design")


def _queue(connection, item: int, *, who: str, what: str) -> int:
    ident = create_assignment(connection, item=item, role="author", status="queued", lane="parallel",
                              budget_minutes=BUDGET_MINUTES)
    connection.execute("UPDATE assignment SET scope = ?, queued_at = ?, started = NULL WHERE id = ?",
                       (SCOPE, clock(), ident))
    add_note(connection, item, "comment", f"{what} assignment {ident} queued by {who}")
    return ident


def is_design(connection, item: int) -> bool:
    return build.work_kind(connection, item) == build.DESIGN_KIND


def design(connection, item: int) -> str | None:
    """The design the newest design run of `item` wrote, or None: no run, or a newer one not done."""
    ran = connection.execute("SELECT status FROM assignment WHERE item = ? AND scope = ? ORDER BY id DESC LIMIT 1",
                             (item, SCOPE)).fetchone()
    if ran is None or ran["status"] != "done":
        return None
    note = connection.execute(
        "SELECT body FROM note WHERE item = ? AND kind = 'comment' AND session LIKE ? AND body LIKE ? "
        "ORDER BY timestamp DESC, id DESC LIMIT 1", (item, f"{SESSION}%", f"{DESIGN}%")).fetchone()
    return note["body"].removeprefix(DESIGN).strip() if note else None


def open_runs(connection, hosts=None) -> list[dict]:
    """Every unreleased draft run, oldest first; with `hosts`, only those hosts' runs (`build.mine`)."""
    return [dict(row) for row in connection.execute(
        "SELECT runner_run.* FROM runner_run JOIN assignment ON assignment.id = runner_run.assignment "
        "WHERE runner_run.released_at IS NULL AND assignment.scope = ? ORDER BY runner_run.created_at, runner_run.id",
        (SCOPE,)) if hosts is None or build.mine(row, hosts)]


def claim(connection, *, owner: str, available: dict) -> dict | None:
    """Claim the oldest queued draft under one `BEGIN IMMEDIATE`; None when none may start now.

    `available` is `build.room`'s answer. None while any draft run is
    unreleased (one at a time) or the machine has no room. A queued draft
    whose item left `planning`, or whose repository turned ineligible, ends
    `cancelled` with a note, and the claim goes on to the next.
    """
    with transaction(connection):
        if restoration_pending(connection) or available.get("why") or available.get("machine", 0) <= 0:
            return None
        if open_runs(connection):
            return None
        while True:
            row = connection.execute(
                "SELECT assignment.id, assignment.item, item.status FROM assignment JOIN item ON item.id = assignment.item "
                "WHERE assignment.status = 'queued' AND assignment.scope = ? "
                "ORDER BY assignment.queued_at, assignment.id LIMIT 1", (SCOPE,)).fetchone()
            if row is None:
                return None
            refused = _refusal(connection, row)
            if refused is None:
                break
            connection.execute("UPDATE assignment SET status = 'cancelled', ended = ?, result = ? WHERE id = ?",
                               (clock(), refused, row["id"]))
            add_note(connection, row["item"], "comment", f"Draft assignment {row['id']} cancelled: {refused}")
        stamp = clock()
        connection.execute("UPDATE assignment SET status = 'running', started = ?, ended = NULL WHERE id = ?",
                           (stamp, row["id"]))
        ident = _open(connection, row["id"], owner=owner, stamp=stamp)
        return build.run(connection, ident)


def repo_refusal(row, connection) -> str | None:
    """Why the item's repository takes no draft, or None: `build._repo_refusal`, and managed only.

    A build may take an unmanaged repository on GitHub (pack sd:7868), but a
    draft is a paid run the operator did not ask for there: it stays on the
    managed repositories it served before.
    """
    refused = build._repo_refusal(row, connection, hub_only=True)
    if refused is None and not row["managed"]:
        return f"item {row['id']}'s repository {row['repo']} is not managed"
    return refused


def _refusal(connection, row) -> str | None:
    if row["status"] not in (("planning", "ready") if is_design(connection, row["item"]) else ("planning",)):
        return f"item {row['item']} moved to {row['status']} before its draft ran"
    refused = repo_refusal(connection.execute(build._REPO, (row["item"],)).fetchone(), connection)
    return f"{refused} before its draft ran" if refused else None


def _open(connection, assignment: int, *, owner: str, stamp: str) -> str:
    """A fresh run of `assignment` at `claimed`, numbered after its last; no lease, no worktree: the checkout."""
    row = connection.execute("SELECT assignment.item, assignment.run_count, item.repo FROM assignment "
                             "JOIN item ON item.id = assignment.item WHERE assignment.id = ?", (assignment,)).fetchone()
    number = connection.execute("SELECT MAX(COALESCE((SELECT MAX(run) FROM runner_run WHERE assignment = ?), 0), ?) + 1",
                                (assignment, row["run_count"])).fetchone()[0]
    connection.execute("UPDATE assignment SET run_count = ? WHERE id = ?", (number, assignment))
    ident = uuid.uuid4().hex
    work = str(paths.expand(row["repo"]))
    connection.execute(
        "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, start_step, "
        "step, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'claimed', 'claimed', ?, ?, ?)",
        (ident, assignment, number, row["repo"], branch(row["item"]), owner, work, work, build.OPUS, stamp, stamp))
    return ident


def parse(result: object) -> tuple[str, list[str]] | None:
    """The spec and the question notes of a draft's reply, or None for anything else (it writes no note).

    The reply starts with `## Spec`, which holds text; an optional
    `## Questions` follows with at most `QUESTIONS_MAX` questions, each its
    text and then two or more `Option:` lines, one recommended (pack
    sd:6708). A Questions section that says
    only "None" holds none. Each question returns as the note body Decide
    reads: its text, then one `Option:` line per choice.
    """
    if not isinstance(result, str):
        return None
    sections: dict[str, list[str]] = {}
    current = None
    for line in result.strip().splitlines():
        heading = _HEADING.match(line)
        if heading:
            current = heading[1].casefold()
            if current in sections:
                return None
            sections[current] = []
        elif current is None:
            return None
        else:
            sections[current].append(line.rstrip())
    if list(sections)[:1] != ["spec"]:
        return None
    spec = "\n".join(sections["spec"]).strip()
    if not spec:
        return None
    blocks: list[list[str]] = []
    for line in sections.get("questions", []):
        if not line.strip():
            continue
        option = _OPTION.match(line)
        if not blocks or (option is None and any(_OPTION.match(seen) for seen in blocks[-1])):
            blocks.append([])
        blocks[-1].append(line.strip())
    if len(blocks) == 1 and len(blocks[0]) == 1 and _NONE.match(blocks[0][0]):
        return spec, []
    if len(blocks) > QUESTIONS_MAX:
        return None
    questions = []
    for block in blocks:
        text = [line for line in block if not _OPTION.match(line)]
        options = [found[1] for found in map(_OPTION.match, block) if found is not None]
        if not text or len(options) < 2 or not all(options) or len(set(options)) != len(options):
            return None
        body = "\n".join([*text, *(f"Option: {option}" for option in options)])
        # `add_note` refuses a question with no pick (pack sd:6708): such a reply writes nothing.
        if recommended(body, options) is None:
            return None
        questions.append("\n".join([*text, *(f"Option: {option}" for option in options)]))
    return spec, questions


class _Moved(Exception):
    """The run's assignment is no longer running; the write rolls back."""


class Moved(Exception):
    """The item moved to another repository during its run, whose reply read the old one; the write rolls back."""


def write(connection, ident: str, spec: str, questions: list[str]) -> bool:
    """Land a parsed draft: the `Draft spec:` comment, one question note each, the assignment `done`, the run released.

    One transaction under one compare-and-set on the run, open and with no
    outcome; False, with nothing written, for a run that ended or was
    released (a late supervisor after a reap), or whose assignment no longer
    runs. Every note's session is `draft:<run>`, which the status guard reads.
    """
    try:
        with transaction(connection):
            stamp = clock()
            if connection.execute(
                    "UPDATE runner_run SET outcome = 'done', detail = ?, released_at = ?, updated_at = ? "
                    "WHERE id = ? AND outcome IS NULL AND released_at IS NULL",
                    (f"drafted with {len(questions)} question{'s' if len(questions) != 1 else ''}", stamp, stamp,
                     ident)).rowcount != 1:
                return False
            row = connection.execute("SELECT assignment.id, assignment.item, runner_run.repo AS ran, item.repo "
                                     "FROM runner_run JOIN assignment ON assignment.id = runner_run.assignment "
                                     "JOIN item ON item.id = assignment.item WHERE runner_run.id = ?",
                                     (ident,)).fetchone()
            # Its draft fits the repository it read, not the one it now belongs to (review of sd:10096).
            if not paths.same(row["repo"], row["ran"]):
                raise Moved(f"sd:{row['item']} moved to {row['repo'] or 'no repository'} during its run, which "
                            f"read {row['ran']}; nothing was written; Build it again")
            if connection.execute("UPDATE assignment SET status = 'done', ended = ?, result = ? "
                                  "WHERE id = ? AND status = 'running'",
                                  (stamp, f"draft run {ident} wrote its spec", row["id"])).rowcount != 1:
                raise _Moved
            designed = is_design(connection, row["item"])
            add_note(connection, row["item"], "comment", f"{DESIGN if designed else SPEC}\n\n{spec}",
                     session=session(ident))
            for question in questions:
                add_note(connection, row["item"], "question", question, session=session(ident))
            if designed and not questions:
                # It rides the repository's next code bundle, which commits it (sd:10096); with a question, the
                # operator's ruling, then Build, queues it. So does Build when the press refuses it now (an item
                # moved off ready, a dependency reopened): the refusal precedes every write of the row.
                try:
                    build.queue_row(connection, row["item"], who=f"design run {ident}")
                except build.BuildRefused as refusal:
                    add_note(connection, row["item"], "comment", f"Design written; no build queued: {refusal}")
            return True
    except _Moved:
        return False


def end(connection, ident: str, outcome: str, detail: str) -> dict | None:
    """The supervisor ends its own run short of a draft; `_settle`'s answer, or None once the run moved.

    A `LOSSES` outcome retries once; `blocked` (an unparsed or failed reply)
    blocks the assignment, with no note, and holds the run until the reap
    reads its supervisor gone; `build.RATE_LIMITED` queues it again and
    holds its host, as a build run's does (sd:9938).
    """
    if outcome not in ("blocked", build.RATE_LIMITED, *LOSSES):
        raise build.BuildRefused(f"no draft outcome {outcome!r}")
    with transaction(connection):
        if connection.execute("UPDATE runner_run SET outcome = ?, detail = ?, updated_at = ? WHERE id = ? "
                              "AND outcome IS NULL AND released_at IS NULL",
                              (outcome, detail, clock(), ident)).rowcount != 1:
            return None
        row = dict(connection.execute("SELECT * FROM runner_run WHERE id = ?", (ident,)).fetchone())
        return _settle(connection, row, block=detail if outcome == "blocked" else None, quiet=True)


def reap(connection, *, now: str | None = None, probe=Probe, kill=kill_group, hosts=None) -> list[dict]:
    """`build.reap` over the draft runs: a lost one is killed, then retried once or blocked (`_settle`)."""
    return build._reap(connection, open_runs, _settle, now=now, probe=probe, kill=kill, hosts=hosts)


def _settle(connection, read: dict, *, block: str | None = None, quiet: bool = False) -> dict | None:
    """Release an ended draft run, then retry it once or block it, in one transaction; the item stays `planning`.

    `read` is the row decided on; nothing happens unless every `REAP_READS`
    column still holds its value. `block` names why the run blocks: it stays
    unreleased, so it holds the one draft slot, until `build._release` finds
    its groups gone. A block or a second loss ends the assignment `blocked`,
    with a `Draft blocked:` followup unless `quiet` (the supervisor's own
    unparsed reply writes no note).
    """
    ident = read["id"]
    with transaction(connection):
        row = connection.execute(f"SELECT * FROM runner_run WHERE id = ? AND outcome IS NOT NULL AND {_SAME}",
                                 (ident, *(read[name] for name in REAP_READS))).fetchone()
        if row is None:
            return None
        stamp = clock()
        assignment = connection.execute("SELECT id, item, status FROM assignment WHERE id = ?",
                                        (row["assignment"],)).fetchone()
        running = assignment["status"] == "running"
        log = f"log {row['log_path'] or 'none'}"
        if block is not None:
            reason = f"draft run {ident} ended at step {row['step']}: {block}" + ("" if log in block else f"; {log}")
            connection.execute("UPDATE runner_run SET outcome = 'blocked', detail = ?, updated_at = ? WHERE id = ?",
                               (reason, stamp, ident))
            if running:
                _block(connection, assignment, reason, stamp, note=not quiet)
            return {"run": ident, "outcome": "blocked", "retry": None, "blocked": block}
        connection.execute("UPDATE runner_run SET released_at = ?, updated_at = ? WHERE id = ?", (stamp, stamp, ident))
        retry = None
        if row["outcome"] == build.RATE_LIMITED and running:
            # No loss and no block (sd:9938): the account stopped it. The draft queues again at its place,
            # and its host drafts and builds nothing until the limit resets (`build.rate_hold`).
            host = build.run_host(row["owner"]) or build.this_host()
            until = build.hold_until(row["final"], stamp)
            build.set_setting(connection, build.hold_key(host), until)
            connection.execute("UPDATE assignment SET status = 'queued', started = NULL, ended = NULL, "
                               "result = NULL WHERE id = ?", (assignment["id"],))
            add_note(connection, assignment["item"], "comment",
                     f"Draft run {ident} hit the builder account's usage limit; assignment {assignment['id']} "
                     f"queued again; {host} drafts from {until}")
        elif row["outcome"] in LOSSES and running:
            marks = ", ".join("?" for _ in LOSSES)
            losses = connection.execute(f"SELECT COUNT(*) FROM runner_run WHERE assignment = ? AND outcome IN ({marks})",
                                        (assignment["id"], *LOSSES)).fetchone()[0]
            if losses <= RETRIES:
                retry = _open(connection, assignment["id"], owner=row["owner"], stamp=stamp)
                add_note(connection, assignment["item"], "comment",
                         f"Draft run {ident} lost at step {row['step']}; retried as run {retry}")
            else:
                _block(connection, assignment, f"draft run {ident} lost at step {row['step']} a second time; {log}",
                       stamp, note=True)
        return {"run": ident, "outcome": row["outcome"], "retry": retry, "blocked": None}


def _block(connection, assignment, reason: str, stamp: str, *, note: bool) -> None:
    connection.execute("UPDATE assignment SET status = 'blocked', ended = ?, result = ? WHERE id = ?",
                       (stamp, reason, assignment["id"]))
    if note:
        add_note(connection, assignment["item"], "followup",
                 f"{BLOCKED} {reason}. Read the log, then write the spec by hand or capture the item again.")


def drafted(connection) -> list[dict]:
    """Planning items whose newest draft assignment is done, oldest draft first, each with its spec and open questions.

    Decide's Drafts group reads it (`sd_dashboard.decisions.document`); the spec drops its `Draft spec:` line.
    """
    rows = connection.execute(
        "SELECT item.id, item.title, item.repo, assignment.ended FROM item JOIN assignment ON assignment.id = "
        "(SELECT MAX(id) FROM assignment WHERE assignment.item = item.id AND assignment.scope = ?) "
        "WHERE item.status = 'planning' AND assignment.status = 'done' ORDER BY assignment.ended, item.id",
        (SCOPE,)).fetchall()
    found = []
    for row in rows:
        spec = connection.execute(
            "SELECT body FROM note WHERE item = ? AND kind = 'comment' AND session LIKE ? AND body LIKE ? "
            "ORDER BY timestamp DESC, id DESC LIMIT 1",
            (row["id"], f"{SESSION}%", f"{SPEC}%")).fetchone()
        open_questions = connection.execute(
            "SELECT COUNT(*) FROM note WHERE item = ? AND kind = 'question' AND resolved_at IS NULL "
            "AND session LIKE ?", (row["id"], f"{SESSION}%")).fetchone()[0]
        found.append({"item": row["id"], "title": row["title"], "repo": row["repo"], "drafted": row["ended"],
                      "spec": spec["body"].removeprefix(SPEC).strip() if spec else "",
                      "questions": open_questions})
    return found


def item_text(connection, item: int) -> tuple[str, str]:
    """The item's title and body text, as the draft prompt gives them: a JSON body's `text`, else the raw body."""
    row = connection.execute("SELECT title, body FROM item WHERE id = ?", (item,)).fetchone()
    body = row["body"] or ""
    try:
        decoded = json.loads(body)
    except ValueError:
        decoded = None
    if isinstance(decoded, dict) and isinstance(decoded.get("text"), str):
        body = decoded["text"]
    return row["title"], body
