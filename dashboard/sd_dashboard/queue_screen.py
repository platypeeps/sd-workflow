"""Queue (sd:2585): each repository's `sd-ship lane` queue, one row per item in one of six states, and its reorder.

Reads, per request: each managed repository's lane, what `sd-ship -C <repo> lane list` prints, read in-process
through the pack's own `sd_lane` on the request's connection (`in_process`, sd:TBD; `lane list` itself, `READERS` at a
time, where that read could differ), the builder gate logs (`gate-*.log`) in its lane folder, `sd gate status --json`
for the gate slots, and `os.getloadavg()` (macOS `vm.loadavg`). The queue lives in the hub database (pack sd:3282), so
a lane hosted on another machine has entries but no folder here; every managed repository gets its section (sd:3284).
The pack owns the lane root (`sd_lane.lane_root`); the lane folder is `<root>/<checkout name>/lane`, as its `queue`
path `<lane>/queue/queue.json` says, and a test holds that path to the one `lane list` prints.

The six states, from each entry's `status`:

  merging   `running`: its holder's host, its `step` and claim age (since `started_at`), and its lease's end, each as
            `lane list` prints it; a field an earlier pack did not write reads as not recorded, never guessed. A lease
            that ended lights caution: the holder may be stuck, and only a reclaim or a release ends it. Its `claim`,
            `<holder token>:<step>`, is what Release sends back; an entry without a token or step has none.
  next      `pending`, in queue order; a `held` one keeps its place and the runner skips it.
  building  a builder gate log in the lane folder, changed in the last `BUILDING_HOURS`: empty while its gate runs.
  blocked   the newest entry of an item that `failed`, was `skipped` or stopped `prepared`, in the last `BLOCKED_DAYS`,
            with the first line of its reason. An item that is `done` in the database (cancel closes as `done`) is not
            blocked, whatever its lane entry says: it closed after the lane stopped (sd:5847). A failed one also says
            whether its worktree still holds the head it failed at (`unchanged`), what a failed prepare answered
            (`failure`, from its log) and whether Fix may send it back to the build run that enqueued it (`fix`).
  cancelled the newest entry of an item that was `cancelled`, in the last `BLOCKED_DAYS`, with its head.
  landed    `merged` today, in this machine's zone, with the merge commit and the pull request its subject names.

Beside the lanes, `prs` (pack sd:7859): each item whose linked pull request (its contribution `pull_url`) is open and
waits on review, from its lane notes (`contributions.pr_waits`): who it waits on, `you` or `reviewers`, since when and
for how long. Today's Queue view draws it as blocked by its PR, not working. An item in the lane keeps its lane row;
a `prepared` one, stopped for a person's review in a manual-merge lane, shows the wait on that row.

An item's newest entry is the one with the highest `enqueue_seq`, not the last one listed: `lane move` can list a newer
entry above older ones (sd:3419). `newest` is the one place that picks it.

Writes: `move` runs `sd-ship -C <repo> lane move|hold|release|cancel|retry`, the queue's only writers. It compares the
page's revision (a digest of the pending order, the holds and each blocked or cancelled entry's head) with a fresh `lane list` first
and refuses a stale one. `move`, `hold`, `release` and `cancel` then pass the fresh list's lane revision (the pending
order and holds, the one `lane list` prints) as `--expected-revision`, and the verb checks it again under the queue's
lock, so a write that lands between the two checks is refused too. `cancel` needs it most: it also releases a running
entry (pack sd:3282), so a Cancel on a pending row the runner claimed since must not reach it.
The runner reads the queue again at each item boundary, so an edit takes effect there.
A refusal the verb reports as written-nothing (`refused`) answers 400, or 409 when stale; any other failure answers 502
with `outcome: unknown` (sd:3339). A hub fault, a timeout or an exit without a refusal may come after the write landed,
so the page reads the lane again and never says the queue is unchanged.

Retry and Approve act on a blocked row (sd:3012): `lane retry <item>` queues its last entry again at the same head with
the body copy the lane kept (pack sd:3254); Approve adds `--manual`, the merge grant, so only a repo whose
`runner_merge` is manual takes it. Both carry the head the row showed as `--expected-head`, which the verb checks under
the queue's lock (pack sd:3268), so neither acts on an entry that ended at another head after the page read it. The
page offers both only when `sd-ship lane retry --help` names `--expected-head`: an older pack cannot check the head.

Retry on a failed row whose worktree still holds the head it failed at repeats the failure unless it was transient
(pack sd:8584): `unchanged` (`_unchanged`, one local git read) says so, the page asks first and names the blocker's
code, whether the lane marks it retryable, and its next action; None is a worktree not read here. Fix sends the
finding to the build run that enqueued the branch (`builds.lane_fix_state`), through Send to builder's route, and
hands the failed entry back to that builder (`build.lane_fix`): `lane retry` then refuses it.

Re-queue acts on a cancelled row (sd:3398): `lane retry <item> --expected-head <head>` queues the cancelled entry
again, last, at the head the row showed (pack sd:3389). The page checks first that the item's newest entry is still that
cancelled one, so it re-queues a cancel and enqueues nothing else (operator ruling, 2026-10-11). The verb refuses a
moved branch (`head_moved`), a newer entry (`superseded`) and a merged head (`already_merged`), each writing nothing.
The page offers Re-queue only when `lane retry --help` names a cancelled entry; otherwise the row says why it is off,
and a probe that failed says so, since that is not an answer.

Release acts on a running row (sd:3337, D3 of pack sd:3174): `lane cancel <item> --expected-claim <claim>` releases the
claim the row showed, in the verb's transaction (pack sd:3330). At prepare the entry goes back to pending; at merge it
fails, since a merge may have landed. A token is made per claim, so a step that moved on, a claim taken again at the same
revision and an entry that finished are each refused as `stale_claim`, which the page reads as a 409. The holder token
reaches the page for this; the pending order is not what Release acts on, so it passes no `--expected-revision`.

Read again on the Queue view (sd:8375) first posts `/api/queue/prs/check` `{items}`, the items its pull-request rows
show (`check_prs`). Each open item whose contribution `pull_url` names a pull request is read on GitHub
(`contribution_github.pull`) and recorded as the collector records it (`contributions.observe_pull`). The read that
first records a pull request merged writes `PR <url> merged at <merge commit>` on each open item linking it and
changes no status, unless its commit says `Closes: sd:<item>` (sd:8455). At most `PR_CHECK_ITEMS` items, `PR_CHECK_SECONDS` seconds; a pull
not read, or a merged pull whose item is still open, is named in `unread`, which the page shows under the table.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from sd_db import operations, reads, today
from sd_db.errors import SdDbError
from sd_db.runner import queue_state as runner_state

from . import pack_module

LIST_SECONDS = 20
#: `lane list` calls at once, for the lanes `in_process` leaves to it, beside `sd gate status` and the retry probe.
READERS = 8
WRITE_SECONDS = 30
GIT_SECONDS = 5
BUILDING_HOURS = 6
BLOCKED_DAYS = 3
#: What the page sends as `action`: `move`'s relative places, the two hold verbs, cancel, a blocked row's retry and
#: approve (`retry --manual`), a cancelled row's Re-queue (`requeue`, which runs `lane retry`), and a running row's
#: Release (`reclaim`, which runs `lane cancel --expected-claim`).
ACTIONS = ("up", "down", "top", "hold", "release", "cancel", "retry", "approve", "requeue", "reclaim")
#: The ends short of a merge that the page shows as blocked and `lane retry` takes.
BLOCKED = ("failed", "skipped", "prepared")
#: Who acts on a blocked entry, by its status and failed step.
WHO = {("prepared", None): "operator", ("skipped", None): "builder", ("failed", "prepare"): "builder",
       ("failed", "merge"): "lane", ("failed", None): "lane"}
#: The refusal codes of the verbs the page runs that wrote nothing: a stale check, or a definite answer before the write.
#: Not `hub_unavailable` (a write may have landed) or `publish_unknown` (origin may have moved): those are unknown.
REFUSED = ("stale_revision", "stale_head", "stale_claim", "holder_alive", "head_gone", "branch_diverged", "default_branch",
           "head_moved", "superseded", "already_merged", "handed_back")
#: Read again's pull-request check (sd:8375): how many items, how long, and the requests one pull read may spend
#: (its detail twice, reviews, two comment lists, the timeline and two CI reads, with paging room).
PR_CHECK_ITEMS = 20
PR_CHECK_SECONDS = 30
PR_CHECK_REQUESTS = 15
EDITS = "Edits take effect at the next item boundary: the runner reads the queue again before each item, never mid-merge."


def command(name: str) -> str:
    """A pack command on PATH, else in the installed pack's checkout: the service's PATH has no pack directory."""
    found = shutil.which(name)
    if found:
        return found
    from sd_db import pack

    try:
        checkout = json.loads(pack.receipt_path(Path.home()).read_text(encoding="utf-8")).get("checkout")
    except (OSError, ValueError, AttributeError):
        checkout = None
    return str(Path(checkout) / "bin" / name) if checkout else name


def conventional(title: str, kind: str = "chore") -> str:
    """`title` as a Conventional Commits header, which `lane enqueue` requires (pack sd:6059): kept when it is one,
    else `<kind>: ` before it.

    The rule is the pack's own (`sd_lib.conventional_title`). A pack that does not have it (`ImportError`, or no such
    name) keeps `title`: an enqueue that refuses it says why.
    """
    try:
        check = pack_module("sd_lib").conventional_title
    except (ImportError, AttributeError):
        return title
    return title if check(title) else f"{kind}: {title}"


def _run(argv: list[str], seconds: int) -> dict:
    """A pack command's JSON answer and its exit status, `exit`; a failure to run or parse is `{"ok": False, "error": ...}`
    with no `exit`, since a command stopped past `seconds` may have written first."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=seconds, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": f"{Path(argv[0]).name} gave no answer: {error}"}
    try:
        answer = json.loads(done.stdout)
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        return {"ok": False, "error": (done.stderr or done.stdout).strip()[-400:] or f"exit {done.returncode}"}
    return {**answer, "exit": done.returncode}


def refused(answer: dict) -> bool:
    """Whether a lane verb refused and wrote nothing: `sd-ship lane` exits 3 with its refusal, coded in `REFUSED` or with
    no code, which is a check before the write. Any other failure is unknown, not a refusal (sd:3339)."""
    return answer.get("exit") == 3 and answer.get("ok") is False and (answer.get("code") or None) in (None, *REFUSED)


def lane_list(path: str) -> dict:
    return _run([command("sd-ship"), "-C", path, "lane", "list"], LIST_SECONDS)


#: The `sd_lane` functions `lane list` reads with; a pack without one of them answers through `lane list`.
LANE_READS = ("lane_root", "stored", "enqueue_order", "item_rounds", "shown", "queue_revision")
#: What an in-process lane read can raise: the database's faults, and a stored body `lane list` could not print either.
READ_FAILURES = (sqlite3.Error, SdDbError, OSError, EOFError, ValueError, TypeError, KeyError)


def in_process(connection, path: str, remote: str | None) -> dict | None:
    """`lane list`'s answer for the checkout at `path`, read on `connection` with the pack's own `sd_lane` (sd:TBD).

    The functions `sd-ship lane list` calls -- `stored`, then `enqueue_order`, `item_rounds`, `shown` and
    `queue_revision` -- with no process per repository. Two inputs come without git: the repository is the registered
    `remote`'s GitHub name, which the pack reads from the checkout's origin, and the lane folder is named after the
    registered checkout, the main one. None where either could differ or `lane list` does more than read: a remote
    that names no GitHub repository, a `.git` that is not a folder, an earlier version's queue file, which `lane list`
    imports, or no pack `bin/` beside the library. The caller runs `lane list` for those. A failed read is
    `{"ok": False, "error": ...}`, never an empty lane.
    """
    from sd_db.protection import github_slug

    found = github_slug(remote)
    if found is None or not os.path.isdir(os.path.join(path, ".git")):
        return None
    try:
        lane = pack_module("sd_lane")
    except ImportError:
        return None
    if not all(callable(getattr(lane, name, None)) for name in LANE_READS):
        return None  # an earlier pack: its `lane list` prints what it has
    try:
        queue = lane.lane_root(dict(os.environ)) / Path(path).resolve().name / "lane" / "queue" / "queue.json"
    except lane.sd_lib.ConfigError as error:
        return {"ok": False, "error": f"the lane root was not read: {error}"}
    if queue.exists():
        return None
    repository = "/".join(found).lower()
    try:
        # `stored` first: an entry written between the two reads is then in the order, never missing from it.
        entries = lane.stored(connection, repository)
        order = lane.enqueue_order(connection, repository)
        rounds = lane.item_rounds(entries, order)
        listed = [{**lane.shown(row), "enqueue_seq": order[row["id"]], "round": rounds[row["id"]]} for row in entries]
    except READ_FAILURES as error:
        return {"ok": False, "error": f"the lane was not read: {type(error).__name__}: {error}"}
    return {"ok": True, "queue": str(queue), "revision": lane.queue_revision(entries), "entries": listed}


def retry_help() -> dict:
    """What the installed pack's `lane retry` takes, from its `--help`: `retry` when it names `--expected-head` (sd:3254,
    sd:3268), `requeue` when it also names a cancelled entry (pack sd:3389). A failed probe offers neither; `requeue_off`
    says why Re-queue is off, and names a failed probe as one, not as a pack without it."""
    try:
        done = subprocess.run([command("sd-ship"), "lane", "retry", "--help"], capture_output=True, text=True,
                              timeout=LIST_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {"retry": False, "requeue": False, "requeue_off": f"sd-ship lane retry --help gave no answer ({error})."}
    if done.returncode != 0:
        said = (done.stderr or done.stdout).strip()[-200:]
        return {"retry": False, "requeue": False,
                "requeue_off": f"sd-ship lane retry --help exited {done.returncode}{f': {said}' if said else ''}."}
    retry = "--expected-head" in done.stdout
    requeue = retry and "cancelled" in done.stdout
    return {"retry": retry, "requeue": requeue, "requeue_off": None if requeue else
            "the installed pack's lane retry takes no cancelled entry; it needs pack sd:3389. Enqueue it again from its worktree."}


def lane_revision(entries: list[dict]) -> str:
    """The pending order and holds, which is everything a reorder reads: the digest `lane list` prints."""
    pending = [[row.get("item"), bool(row.get("held"))] for row in entries if row.get("status") == "pending"]
    return hashlib.sha256(json.dumps(pending).encode()).hexdigest()[:16]


def newest(entries: list[dict]) -> dict:
    """Each item's newest entry, by item: the highest `enqueue_seq`, since a `move` can list it above older ones."""
    # sd:3419 fallback: a pack that gives some entry no `enqueue_seq` lists an item's entries oldest first, so the last
    # listed wins. Delete the conditional once the installed pack's `lane list` gives every entry `enqueue_seq`.
    ordered = (sorted(entries, key=lambda row: row["enqueue_seq"])
               if all(type(row.get("enqueue_seq")) is int for row in entries) else entries)
    return {row.get("item"): row for row in ordered}


def revision(entries: list[dict]) -> str:
    """The page's revision: the lane revision and the head of each item's newest entry when it is blocked.

    A run that ends blocked at another head leaves the pending order alone; this still changes (sd:3012 review).
    """
    heads = [[item, row.get("expected_head")] for item, row in newest(entries).items()
             if row.get("status") in (*BLOCKED, "cancelled")]
    return hashlib.sha256(json.dumps([lane_revision(entries), heads]).encode()).hexdigest()[:16]


def _when(stamp: str | None) -> float | None:
    try:
        return datetime.strptime(stamp or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


#: Each `(checkout, commit)`'s pull request, or None, from a git that answered: a commit's subject never changes.
_PULLS: dict[tuple[str, str], str | None] = {}
#: The most `_PULLS` keeps before it starts again: a day lands tens of commits.
PULLS_KEPT = 4096


def _pr(path: str, commit: str) -> str | None:
    """The pull request a squash commit's subject names, `(#161)`, or None; git is asked once per commit it answers."""
    if (path, commit) in _PULLS:
        return _PULLS[(path, commit)]
    try:
        done = subprocess.run(["git", "-C", path, "log", "-1", "--format=%s", commit], capture_output=True, text=True,
                              timeout=GIT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"\(#([0-9]+)\)\s*$", done.stdout.strip())
    if done.returncode == 0:
        if len(_PULLS) >= PULLS_KEPT:
            _PULLS.clear()
        _PULLS[(path, commit)] = found[1] if found else None
    return found[1] if found else None


#: The most of a prepare log the page reads for prepare's answer, which comes before its stderr (pack sd:8584).
PREPARE_LOG_BYTES = 1 << 20


def _failure(entry: dict, lane: Path) -> dict:
    """pack sd:8584: a failed prepare's answer from its log in the lane's `logs/`: the blocker's `code`, whether the
    lane marks it `retryable`, and its `next_action`. `{}` for a failure at another step or a log not read here: a log
    on another host, outside `logs/`, cut short or with no answer."""
    log = Path(str(entry.get("prepare_log") or ""))
    if entry.get("step") != "prepare" or log.parent != lane / "logs":
        return {}
    try:
        with log.open("rb") as handle:
            text = handle.read(PREPARE_LOG_BYTES).decode("utf-8", errors="replace")
        # `sd_lane.ship_process` writes `$ <argv>`, the answer, then `--- stderr ---`; JSON escapes its own newlines.
        answer = json.loads(text.partition("\n")[2].partition("\n--- stderr ---\n")[0])
    except (OSError, ValueError):
        return {}
    workflow = answer.get("workflow") if isinstance(answer, dict) else None
    blocker = workflow.get("blocker") if isinstance(workflow, dict) else None
    if not isinstance(blocker, dict):
        return {}
    return {"code": blocker.get("code"), "retryable": blocker.get("retryable"), "next_action": workflow.get("next_action")}


def _git(tree: Path, *args: str) -> str | None:
    """`git <args>` in `tree`, bounded by `GIT_SECONDS`; its stdout stripped, or None when git did not answer."""
    try:
        done = subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, timeout=GIT_SECONDS,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


#: The most merges prepare's catch-ups stack on a head: `sd_lane.CATCH_UPS`, twice, as `sd_lane.catch_up_of` walks.
CATCH_UP_LINKS = 14


def _unchanged(entry: dict) -> bool | None:
    """pack sd:8584: whether the entry's worktree still holds the head it ended at, or a chain of merges on it, the
    shape of prepare's catch-up. None when not read here: another host's worktree, one gone or on another branch,
    or git that did not answer.

    One local git read under `GIT_SECONDS`: rows() runs on every page read. It never asks `sd_lane.at_head`, which
    reads origin and writes merge objects into the worktree (round 1). So a merge that is not a clean catch-up reads
    as unchanged here; `lane retry` makes the full check and refuses it `HEAD_MOVED`."""
    from sd_db.ship import this_host

    head, branch, tree = entry.get("expected_head"), entry.get("branch"), Path(str(entry.get("worktree") or ""))
    if not (head and branch and entry.get("worktree") and entry.get("enqueued_on") == this_host() and tree.is_dir()):
        return None
    if _git(tree, "branch", "--show-current") != branch:
        return None
    found = _git(tree, "rev-list", "--first-parent", "--parents", "-n", str(CATCH_UP_LINKS + 1), "HEAD")
    if not found:
        return None
    for line in found.splitlines():
        commit, *parents = line.split()
        if commit == head:
            return True
        if len(parents) != 2:  # a commit of its own, not a catch-up merge
            return False
    return False if len(found.splitlines()) <= CATCH_UP_LINKS else None


def _building(lane: Path, now: float) -> list[dict]:
    """Each builder gate log changed in the last `BUILDING_HOURS`, newest first."""
    rows = []
    for log in lane.glob("gate-*.log"):
        try:
            changed, text = log.stat().st_mtime, log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if now - changed > BUILDING_HOURS * 3600:
            continue
        try:
            result = json.loads(text) if text.strip() else None
        except ValueError:
            result = None
        row = {"state": "building", "builder": log.stem.removeprefix("gate-"), "log": str(log), "changed": changed}
        if result is None:
            row.update(gate="running" if not text.strip() else "unreadable", summary=None, head=None)
        else:
            row.update(gate="pass" if result.get("status") == "success" else "fail", summary=result.get("summary"),
                       head=str(result.get("head") or "")[:12] or None)
        rows.append(row)
    return sorted(rows, key=lambda row: -row["changed"])


def closed(connection, entries: list[dict]) -> set:
    """The items whose newest entry is blocked and that are `done` in the database; a cancel closes as `done` too."""
    items = sorted(item for item, row in newest(entries).items() if type(item) is int and row.get("status") in BLOCKED)
    found = {item: reads.item_by_id(connection, item) for item in items}
    return {item for item, row in found.items() if row is not None and row["status"] == "done"}


def shots(entry: dict, lane: Path) -> dict:
    """sd:6728: the PNGs the lane drew of the entry's changed pages, each `{name, url}` at `/lane-shots/`, or why none.

    A file outside the lane's `logs/` gets no link: the route serves only those.
    """
    found = {}
    logs = lane / "logs"
    links = [{"name": Path(file).stem, "url": f"/lane-shots/{Path(file).relative_to(lane.parent.parent).as_posix()}"}
             for file in entry.get("shots") or () if isinstance(file, str) and Path(file).parent.parent == logs]
    if links:
        found["shots"] = links
    if entry.get("shots_warning"):
        found["shots_warning"] = str(entry["shots_warning"])
    return found


def rows(entries: list[dict], *, path: str, lane: Path, now: float, done: frozenset | set = frozenset()) -> list[dict]:
    """The lane's rows in page order: merging, next, building, blocked, cancelled, landed.

    `done` holds the items that are `done` in the database: none of them is a blocked row (sd:5847).
    """
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    merging, following, blocked, cancelled, landed = [], [], [], [], []
    latest = newest(entries)
    for entry in entries:
        status, item = entry.get("status"), entry.get("item")
        # The worktree `lane enqueue` recorded: an adversarial review of the change reads it (sd:5064).
        base = {"item": item, "title": entry.get("title") or "", "worktree": entry.get("worktree") or None,
                **shots(entry, lane)}
        if status == "running":
            started, holder = _when(entry.get("started_at")), entry.get("holder")
            holder = holder if isinstance(holder, dict) else {}
            token, step = holder.get("token"), entry.get("step")
            merging.append({**base, "state": "merging", "step": step or None,
                            # The token only inside the claim Release sends back for `--expected-claim` (sd:3337).
                            "claim": f"{token}:{step}" if isinstance(token, str) and token and step else None,
                            "host": holder.get("host") or None, "claim_age": int(now - started) if started else None,
                            "lease_until": entry.get("lease_until") or None,
                            "lease_passed": (_when(entry.get("lease_until")) or now) < now,
                            "head": str(entry.get("expected_head") or "")[:12]})
        elif status == "pending":
            gate = entry.get("speculation") or {}
            following.append({**base, "state": "next", "position": len(following) + 1, "held": bool(entry.get("held")),
                              "head": str(entry.get("expected_head") or "")[:12],
                              "gate": gate.get("status") or "not gated", "gate_summary": gate.get("summary") or gate.get("reason")})
        elif status in BLOCKED and latest.get(item) is entry and item not in done:
            finished = _when(entry.get("finished_at"))
            if finished and now - finished <= BLOCKED_DAYS * 86400:
                who = WHO.get((status, entry.get("step"))) or WHO.get((status, None), "lane")
                reason = (str(entry.get("reason") or "").strip().splitlines() or [status])[0]
                head = str(entry.get("expected_head") or "")
                # The log of the step that failed: a merge failure's prepare log is of a prepare that passed (sd:8601).
                log = entry.get("merge_log" if entry.get("step") == "merge" else "prepare_log")
                failed = {"branch": entry.get("branch"), "unchanged": _unchanged(entry), "failure": _failure(entry, lane),
                          "log": str(log or "") or None} if status == "failed" else {}
                blocked.append({**base, "state": "blocked", "status": status, "step": entry.get("step"),
                                "reason": reason, "who": who, "finished": entry.get("finished_at"),
                                "head": head[:12], "expected_head": head, **failed})
        elif status == "cancelled" and latest.get(item) is entry:
            finished = _when(entry.get("finished_at"))
            if finished and now - finished <= BLOCKED_DAYS * 86400:
                head = str(entry.get("expected_head") or "")
                # A hand-back to a builder names why, and `lane retry` refuses it (pack sd:8584, sd:8601); its first
                # line, as a blocked row's (pack sd:8615).
                why = ((str(entry.get("reason") or "").strip().splitlines() or ["handed back to its builder"])[0]
                       if entry.get("handed_back") else None)
                cancelled.append({**base, "state": "cancelled", "finished": entry.get("finished_at"),
                                  "head": head[:12], "expected_head": head, "reason": why})
        elif status == "merged":
            finished = _when(entry.get("finished_at"))
            if finished and time.strftime("%Y-%m-%d", time.localtime(finished)) == today:
                commit = str(entry.get("merge_commit") or "")
                landed.append({**base, "state": "landed", "commit": commit[:12] or None,
                               "pr": _pr(path, commit) if commit else None, "finished": entry.get("finished_at")})
    return merging + following + _building(lane, now) + blocked + cancelled + landed[::-1]


def _load() -> dict:
    one, five, fifteen = os.getloadavg()
    trend = "rising" if one > five * 1.1 else "falling" if one < five * 0.9 else "steady"
    return {"load1": round(one, 1), "load5": round(five, 1), "load15": round(fifteen, 1), "trend": trend}


def _gates() -> dict:
    status = _run([command("sd"), "gate", "status", "--json"], LIST_SECONDS)
    if "slots" not in status:
        return {"error": status.get("error") or "sd gate status gave no slot count"}
    return {"running": len(status.get("holders") or []), "cap": status["slots"], "waiting": len(status.get("waiters") or [])}


def runner(connection) -> dict:
    """Overview's runner assignments (pack sd:7115, from Management's Ship lane; pack sd:7217 retired its own view): the
    queued and running assignments. The items at `ready_to_send` are Writing's to show; the idle author runner's finished
    assignments and merge history are not listed (sd:5472).

    `blocked` is each blocked build assignment, the rows `sd today`'s Queues lists (`today.builds`, system sd:8651):
    the Builds panel reads runs, so a member with no run of its own, or one never built, was no row on Queue."""
    keys = ("id", "item", "title", "repo", "role", "provider", "status", "lane", "queued_at", "started", "ended")
    return {"live": [{key: row[key] for key in keys} for row in reads.assignment_ledger(connection, live=True)],
            "blocked": [_blocked(connection, row) for row in today.builds(connection) if row["status"] == "blocked"]}


def _blocked(connection, row: dict) -> dict:
    """One blocked build assignment: its reason, its Cancel (`operations.assignment_state`, the route's own check) and
    its Retry, which is its bundle lead's (`builds._retry_state`, the check `builds.retry` makes); off says why."""
    from .builds import _retry_state

    current = operations.assignment_state(connection, row["assignment"])
    cancel = {"assignment": row["assignment"], "revision": current["revision"], **current["capabilities"]["cancel"]}
    lead = runner_state(connection, row["lead"])
    if lead["run"] is None:
        retry = {"assignment": row["lead"], "revision": lead["revision"], "allowed": False,
                 "reason": "it never ran; Cancel clears it, or build the item again from Tasks", "bundle": lead["status"]}
    else:
        retry = _retry_state(connection, lead["run"])
    return {"id": row["assignment"], "item": row["item"], "title": row["title"], "repo": row["repo"],
            "why": row["why"] or "no reason recorded", "since": row["since"], "lead": row["lead"], "cancel": cancel,
            "retry": retry}


#: The lane states that keep an item's row in the lane: its pull request waits there, not on review (pack sd:7859).
#: A `prepared` blocked entry is the exception: in a repo whose `runner_merge` is manual the lane stops there and the
#: pull request waits on a person's review, so its row shows the wait beside its Approve. A landed row is the lane's
#: last word until the next sync notes the merge (sd:7863): a just-merged item read "blocked: PR". A cancelled entry
#: left its pull request open, so its wait still shows.
LANED = ("merging", "next", "blocked", "landed")
#: What the page reads of each pull-request wait (`contributions.pr_waits`).
PR_FIELDS = ("item", "title", "repo", "url", "lane", "on", "since", "waited", "approximate", "draft", "stale")


def pr_rows(connection, lanes: list[dict], clock: float) -> dict:
    """Pack sd:7859: each item whose linked pull request is open in a waiting lane, `{"rows", "error"}`.

    An item with a lane row in `LANED` keeps its lane state and is left out; so is a `done` one. A
    blocked row that stopped `prepared` waits on review: its item is a row here, and the page draws the two as one.
    A read that failed is its `error` and no rows, never an empty answer; an item it skipped is named in `error`
    beside the other rows (sd:7863).
    """
    from sd_db import contributions

    laned = {row.get("item") for lane in lanes for row in lane["rows"]
             if row.get("state") in LANED and not (row.get("state") == "blocked" and row.get("status") == "prepared")}
    skipped: list[str] = []
    try:
        waits = contributions.pr_waits(connection, now=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(clock)),
                                       errors=skipped)
        # sd:8142: and every open pull no item links that names the operator, either way.
        loose = contributions.unlinked_pr_waits(connection, now=time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                                                             time.gmtime(clock)))
    except READ_FAILURES as error:
        return {"rows": [], "error": f"pull-request waits were not read: {type(error).__name__}: {error}"}
    return {"rows": [{key: wait[key] for key in PR_FIELDS} for wait in waits
                     if (wait["open"] or wait["stale"]) and wait["status"] != "done" and wait["item"] not in laned]
                    + [{key: wait[key] for key in PR_FIELDS} for wait in loose],
            "error": f"pull-request waits skipped {'; '.join(skipped)}" if skipped else ""}


def document(connection, *, now: str = "") -> dict:
    """The page's document: the header readings and one section per repository lane."""
    from sd_db import repos

    from .builds import lane_fix_state
    from .hoa_screen import checkout
    from .places import realm_of, realms

    found = realms(connection)
    rules = found["list"] if found["state"] in ("read", "partial") else None
    hoa = checkout(connection)[0]
    clock = _when(now) or time.time()
    managed = {os.path.expanduser(row["path"]): row for row in repos.registered(connection) if row["managed"]}
    here = [path for path in managed if os.path.isdir(path)]
    # A checkout missing here leaves its lane unread: `sd-ship -C` needs one. Named, never dropped.
    problems = [{"repo": path, "error": "no checkout on this machine"} for path in managed if path not in here]
    with ThreadPoolExecutor(max_workers=READERS) as pool:
        # The two probes run beside the reads; the request's connection stays on this thread.
        gates, probe = pool.submit(_gates), pool.submit(retry_help)
        answers = {path: in_process(connection, path, managed[path]["remote"]) for path in here}
        left = [path for path, answer in answers.items() if answer is None]
        answers.update(zip(left, pool.map(lane_list, left), strict=True))
        gates, probe = gates.result(), probe.result()
    lanes, root = [], None
    for path, answer in answers.items():
        if not answer.get("ok") or not isinstance(answer.get("queue"), str):
            problems.append({"repo": path, "error": answer.get("error") or "lane list named no queue"})
            continue
        lane = Path(answer["queue"]).parent.parent
        root = root or lane.parent.parent
        entries = answer.get("entries") or []
        # pack sd:6016: the realm tab that lists the lane, its checkout's realm (None: the realm list was not read).
        shown = rows(entries, path=path, lane=lane, now=clock, done=closed(connection, entries))
        for row in shown:
            if row["state"] == "blocked" and row["status"] == "failed":
                row["fix"] = lane_fix_state(connection, row["branch"])
        lanes.append({"repo": Path(path).name, "path": path, "realm": realm_of(path, "lane", rules, hoa), "lane": str(lane),
                      "revision": revision(entries),
                      "runner_merge": repos.policy_merge(managed[path]["declarations"]), "host": managed[path]["lane_host"] or "hub",
                      "rows": shown})
    return {"read": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock)), "load": _load(), "gates": gates,
            "lanes": lanes, "problems": problems, "root": str(root) if root else None, "edits": EDITS,
            "managed": len(managed), "prs": pr_rows(connection, lanes, clock),
            **(probe if lanes else {"retry": False, "requeue": False, "requeue_off": None})}


def move(payload: dict):
    """Check the page's request, then return the write: a best-effort revision check, then one lane verb."""
    values = dict(payload)
    again, reclaim = values.get("action") in ("retry", "approve", "requeue"), values.get("action") == "reclaim"
    if (set(values) != {"repo", "item", "action", "revision", *(("head",) if again else ()), *(("claim",) if reclaim else ())}
            or not isinstance(values["repo"], str)
            or type(values["item"]) is not int or not 1 <= values["item"] <= 9223372036854775807
            or values["action"] not in ACTIONS or not isinstance(values["revision"], str)
            or (again and not (isinstance(values["head"], str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", values["head"])))
            or (reclaim and not (isinstance(values["claim"], str) and len(values["claim"]) <= 200
                                 and all(values["claim"].partition(":")[::2])))):
        raise ValueError("Name the repository, the item, one of up, down, top, hold, release, cancel, retry, approve, "
                         "requeue or reclaim, the lane's revision, for retry, approve or requeue the full head the row "
                         "showed, and for reclaim the claim it showed.")

    def write(connection):
        from sd_db import repos, workflow

        row = next((row for row in repos.registered(connection) if os.path.expanduser(row["path"]) == values["repo"]), None)
        if row is None:
            raise ValueError("That repository is not registered.")
        path = os.path.expanduser(row["path"])
        if values["action"] == "approve" and repos.policy_merge(row["declarations"]) != "manual":
            raise ValueError("Approve grants the merge on a repository whose runner_merge is manual; this one needs no grant.")
        listed = lane_list(path)
        if not listed.get("ok"):
            raise ValueError(f"The lane was not read: {listed.get('error')}")
        entries = listed.get("entries") or []
        if revision(entries) != values["revision"]:
            raise workflow.StaleItem("The queue changed since the page read it. Read it again, then retry.")
        item, action = str(values["item"]), values["action"]
        found = newest(entries).get(values["item"])
        if action == "requeue" and not (found and found.get("status") == "cancelled"):
            # The operator's ruling (sd:3398): Re-queue takes back a cancel; it enqueues nothing new.
            raise ValueError(f"sd:{item}'s newest entry in this lane is {found.get('status') if found else 'missing'}; "
                             "Re-queue takes only a cancelled one.")
        verb = (["move", item, action] if action in ("up", "down", "top")
                else ["retry", item, "--expected-head", values["head"]] if again
                else ["cancel", item, "--expected-claim", values["claim"]] if reclaim else [action, item])
        if action == "approve":
            verb.append("--manual")
        if action in ("up", "down", "top", "hold", "release", "cancel"):
            # The fresh list matched the page, so its lane revision is the page's; the verb checks it under its lock.
            verb += ["--expected-revision", lane_revision(entries)]
        answer = _run([command("sd-ship"), "-C", path, "lane", *verb], WRITE_SECONDS)
        if not (answer.get("ok") and answer.get("exit") == 0) and not refused(answer):
            # A hub fault, a timeout or an exit without a refusal may follow a landed write: not known (sd:3339).
            return 502, {"error": answer.get("error") or "sd-ship lane gave no answer.", "outcome": "unknown"}
        if answer.get("code") in ("stale_revision", "stale_head"):
            raise workflow.StaleItem("The queue changed since the page read it. Read it again, then retry.")
        if answer.get("code") == "superseded":
            raise workflow.StaleItem(answer.get("error") or f"sd:{item} has a newer entry than the cancelled one.")
        if answer.get("code") == "stale_claim":
            raise workflow.StaleItem(f"sd:{item} moved on since the page read it: its step changed, another claim holds "
                                     "it, or it ended. Nothing was released. Read it again, then retry.")
        if not answer.get("ok"):
            raise ValueError(answer.get("error") or "sd-ship lane refused the change.")
        return {"ok": True, "action": action, "item": values["item"], "pending": answer.get("pending"), "edits": EDITS}
    return write


def check_prs(payload: dict):
    """`POST /api/queue/prs/check` `{items}` (sd:8375): check the page's request, then return `pr_check`'s write."""
    items = payload.get("items") if set(payload) == {"items"} else None
    if (not isinstance(items, list) or not 1 <= len(items) <= PR_CHECK_ITEMS
            or any(type(item) is not int or not 1 <= item <= 9223372036854775807 for item in items)):
        raise ValueError(f"Name 1 to {PR_CHECK_ITEMS} items.")
    return lambda connection: pr_check(connection, list(dict.fromkeys(items)))


def pr_check(connection, items: list[int], *, client=None) -> dict:
    """Read on GitHub the pull request each open item in `items` links and record it as the collector does.

    `{"checked", "unread"}`: the items read, and one line per item whose pull was not read or merged while the item
    stays open (`observe_pull` notes a merge; it closes only on a `Closes:` trailer). Each record is its own transaction.
    """
    from types import SimpleNamespace

    from sd_db import contribution_github as github
    from sd_db import contributions

    wanted = set(items)
    linked = [(row["item_id"], row["url"]) for row in contributions.projection(connection)
              if row["item_id"] in wanted and row["local_status"] != "done" and github.PULL.fullmatch(row["url"] or "")]
    found: dict = {"checked": [], "unread": []}
    if not linked:
        return found
    if client is None:
        budget = SimpleNamespace(remaining=PR_CHECK_REQUESTS * len(linked) + 1, requests=0,
                                 deadline=time.monotonic() + PR_CHECK_SECONDS)
        client = github.Client(budget)
    observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        operator = github.identity(client.get("/user"))
        automation = github.automation_logins(connection)
    except (ValueError, KeyError, TypeError, SdDbError) as error:
        found["unread"] = [f"sd:{item} left open: {url} not read: {error}" for item, url in linked]
        return found
    for item, url in linked:
        try:
            url = github.canonical_url(url)
            before = contributions.snapshot(connection, "github:" + url)
            previous = before["observation"] or {}
            observation = github.pull(client, url, operator, observed_at=observed_at, why=previous.get("why", []),
                                      blocking_labels=previous.get("blocking_labels", []), automation=automation)
            contributions.observe_pull(connection, url, observation, expected_revision=before["revision"])
            found["checked"].append(item)
            if observation["state"] == "merged":
                found["unread"].append(f"sd:{item} open: {url} merged at {observation['merge_commit']}; close it")
        except (ValueError, KeyError, TypeError, SdDbError) as error:
            found["unread"].append(f"sd:{item} left open: {url}: {error}")
    return found
