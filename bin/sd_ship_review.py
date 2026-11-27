"""Shared review gates after explicit identity resolution.

This context performs no item lookup and constructs no publication client.
Adapters supply identity and history; the existing provider executable owns routing.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import posixpath
import re
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable

import sd_lib
import sd_review_request
import sd_ship_bindings
import sd_ship_dispositions
from sd_ship_history import (
    AUTOMATIC_CODE_REVIEW_PASSES,
    completed_depth,
    digest,
    legacy,
    reservation,
    verified_index,
)
from sd_ship_remote import Refusal, completed_process
from sd_ship_workflow import GATE_FAILED, GATE_SLOT_WAIT, success


class ReviewTimeout(Exception):
    def __init__(self, diagnostic: dict):
        self.diagnostic = diagnostic
        super().__init__("local review watchdog expired")


def is_ancestor(root: pathlib.Path, previous: str, head: str) -> bool:
    """Whether `previous` is reachable from `head`.

    `--is-ancestor` answers by exit status and prints nothing, so the raising
    `git` helper rendered "not an ancestor" as a bare, retryable "git failed"
    (sd:1348). Only exit 1 means "no"; any other failure, a timeout or a
    missing git still raises that retryable runtime refusal, because a git
    that cannot answer is not evidence of a rewritten history. The caller
    owns the refusal that names the heads.
    """
    argv = ["git", "merge-base", "--is-ancestor", previous, head]
    return completed_process(root, argv, answers=frozenset({0, 1})).returncode == 0


def commit_absent(root: pathlib.Path, commit: str) -> bool:
    """sd:3167. Whether the object store holds no commit `commit`, as after gc prunes an amended-away head.

    `rev-parse --verify --quiet` answers exit 1 for a name that names no
    commit; any other failure raises, as `is_ancestor` does. A shallow clone
    answers False: a commit past its boundary is absent and may still be an
    ancestor.
    """
    argv = ["git", "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"]
    if completed_process(root, argv, answers=frozenset({0, 1})).returncode == 0:
        return False
    return completed_process(root, ["git", "rev-parse", "--is-shallow-repository"]).stdout.strip() != "true"


def empty_branch_base(root: pathlib.Path, head: str) -> str | None:
    """sd:1405. The merge base when `head` changes no file against it, else None.

    A branch of empty commits only changes no file. `sd-review` refuses
    such a subject as `subject_empty`, so the ship lane waives review for it.
    """
    for ref in ("origin/HEAD", "main", "master"):
        base = sd_lib.git_output(["merge-base", head, ref], root)
        if base:
            return base if sd_lib.git_output(["diff", "--name-only", "--no-renames", base, head], root) == "" else None
    return None


def own_patch_id(root: pathlib.Path, head: str, base_ref: str) -> str | None:
    """`git patch-id --stable` of the branch's own change, fork..head, or None.

    Bytes, not text: a diff of a non-UTF-8 file must still hash. `--binary`
    puts binary content in the hash; without it only an `index` line names
    it, and patch-id ignores that line. Any git failure is None, and None
    carries nothing forward.
    """
    fork = sd_lib.git_output(["merge-base", head, base_ref], root)
    if not fork:
        return None
    try:
        diff = subprocess.run(["git", "diff", "--binary", "--full-index", "--no-renames", "--no-color",
                               "--no-ext-diff", "--no-textconv", fork, head],
                              cwd=root, capture_output=True, timeout=120, check=False)
        if diff.returncode or not diff.stdout:
            return None
        hashed = subprocess.run(["git", "patch-id", "--stable"], cwd=root, input=diff.stdout,
                                capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    words = hashed.stdout.decode("ascii", "replace").split()
    return words[0] if hashed.returncode == 0 and words else None


def clean_merges(root: pathlib.Path, reviewed: str, head: str, base_ref: str) -> list[str] | None:
    """The commits `head` adds to `reviewed`, when each is a clean merge-in of the base.

    Every commit the base does not already hold must have exactly two
    parents, the second held by the base, and a tree that is git's own
    conflict-free merge of them. A resolved conflict, an edit folded into
    the merge, or any ordinary commit returns None.
    """
    listed = sd_lib.git_output(["rev-list", "--parents", head, f"^{reviewed}", f"^{base_ref}"], root)
    if not listed:
        return None
    merges = []
    for line in listed.splitlines():
        commit, *parents = line.split()
        if len(parents) != 2 or not is_ancestor(root, parents[1], base_ref):
            return None
        merged = sd_lib.git_output(["merge-tree", "--write-tree", "--no-messages", *parents], root)
        if not merged or merged != sd_lib.git_output(["rev-parse", f"{commit}^{{tree}}"], root):
            return None
        merges.append(commit)
    return merges


#: How many merges deep `built_from` reads a reviewed head: one per lane round that reviewed a merge.
MERGE_BACK_DEPTH = 8


def dropped_catch_up(root: pathlib.Path, reviewed: str, head: str, base_ref: str) -> bool:
    """sd:4925. Whether `reviewed` is a reviewed catch-up merge that `head` lacks, safe to merge back.

    The lane prepares with `--catch-up` in a worktree of its own and drops
    it after a failure, so its reviewed merge survives only in the receipt.
    The retry merged the base again into the fixed branch, the reviewed
    merge was no ancestor of that, and `validate_dispatch` refused a branch
    that had only added commits. Such a merge holds nothing that `head`
    and the base do not (`catch_up_merge`). Anything else, as after an
    amend or a rebase, or a commit the store no longer holds, stays
    orphaned and is refused as before.
    """
    if commit_absent(root, reviewed) or is_ancestor(root, reviewed, head):
        return False
    return catch_up_merge(root, reviewed, head, base_ref, MERGE_BACK_DEPTH)


def catch_up_merge(root: pathlib.Path, commit: str, head: str, base_ref: str, depth: int) -> bool:
    """Whether `commit` is a clean merge of `head`'s history and the base, or of such merges.

    Two parents: the first in `head`'s history or such a merge, the second
    on the base or such a merge (an earlier merge back), never a side
    commit. Its tree is git's own merge of them, so it adds no content. No
    other commit `head` reaches has the same parents: that one is an amend
    of it, and merging the old merge back would publish what the amend
    removed (sd:2600). A failed read is no "yes": `is_ancestor` raises, and
    an unreadable commit is not a catch-up, so it stays orphaned.
    """
    parents = (sd_lib.git_output(["rev-list", "--parents", "-n", "1", commit], root) or "").split()[1:]
    if depth <= 0 or len(parents) != 2:
        return False
    merged = sd_lib.git_output(["merge-tree", "--write-tree", "--no-messages", *parents], root)
    if not merged or merged != sd_lib.git_output(["rev-parse", f"{commit}^{{tree}}"], root):
        return False
    listed = sd_lib.git_output(["rev-list", "--parents", head, f"^{parents[0]}"], root)
    if listed is None or any(line.split()[1:] == parents and line.split()[0] != commit for line in listed.splitlines()):
        return False
    first = is_ancestor(root, parents[0], head) or catch_up_merge(root, parents[0], head, base_ref, depth - 1)
    return first and (is_ancestor(root, parents[1], base_ref) or catch_up_merge(root, parents[1], head, base_ref, depth - 1))


def named_commits(root: pathlib.Path, have: str, lack: str, *, skip: tuple[str, ...] = (), shown: int = 3) -> str:
    """`N commits (a, b, c, +M more)`: what `have` holds and `lack` does not, oldest first, less `skip` (sd:2339)."""
    listed = [commit for commit in (sd_lib.git_output(["rev-list", "--reverse", have, f"^{lack}"], root) or "").split()
              if commit not in skip]
    more = f", +{len(listed) - shown} more" if len(listed) > shown else ""
    return f"{len(listed)} commit{'' if len(listed) == 1 else 's'} ({', '.join(c[:12] for c in listed[:shown])}{more})"


def base_merge_note(root: pathlib.Path, reviewed: str | None, head: str, base: str | None) -> str:
    """Why `head` misses the receipt for `reviewed`, when it adds only clean merges of `origin/<base>` (sd:2339)."""
    if not reviewed or not base or reviewed == head or not is_ancestor(root, reviewed, head):
        return ""
    merges = clean_merges(root, reviewed, head, f"refs/remotes/origin/{base}")
    if merges is None:
        return ""
    return (f"; {head[:12]} adds only a merge of origin/{base}, "
            f"{named_commits(root, head, reviewed, skip=tuple(merges))}, to the reviewed {reviewed[:12]}: "
            "run sd-ship prepare, which carries the review forward or reviews only the branch's own diff")


def paths_between(root: pathlib.Path, old: str, new: str) -> set[str] | None:
    """Every path `old..new` changes, both sides of a rename, or None when git cannot answer."""
    listed = sd_lib.git_output(["diff", "--name-only", "-z", "--no-renames", old, new], root)
    return None if listed is None else {path for path in listed.split("\0") if path}


def base_near_branch(root: pathlib.Path, reviewed: str, head: str, base_ref: str) -> bool:
    """Whether the base's new commits touch a file, or a directory, the branch touches (sd:1485).

    An unchanged patch-id does not show the branch still works on the new
    base: main can change a callee's contract while the branch adds a
    caller in another file. The operator's ruling narrows the carry to base
    commits that stay out of every file the branch changes and every
    directory holding one. A directory is a path's own parent, so a
    top-level file shares the root with every other top-level file.
    Unanswerable reads as near.
    """
    old = sd_lib.git_output(["merge-base", reviewed, base_ref], root)
    new = sd_lib.git_output(["merge-base", head, base_ref], root)
    brought = paths_between(root, old, new) if old and new else None
    own = paths_between(root, new, head) if new else None
    if brought is None or own is None:
        return True
    directories = {posixpath.dirname(path) for path in own}
    return any(path in own or posixpath.dirname(path) in directories for path in brought)


def carry_forward(root: pathlib.Path, reviewed: str, head: str, base_ref: str) -> dict | None:
    """sd:1485. Why a review of `reviewed` still covers `head`, or None.

    A catch-up merge moves the head, and a review of the old head used to
    count as stale. It still covers the branch when `reviewed` is an
    ancestor, every commit since is a clean merge-in of the base
    (`clean_merges`), the base's new commits stay out of the branch's files
    and directories (`base_near_branch`), and the branch's own patch-id
    against the base is unchanged. A rebase is not carried: no merge commit
    shows that nothing else changed.
    """
    if reviewed == head or not is_ancestor(root, reviewed, head):
        return None
    merges = clean_merges(root, reviewed, head, base_ref)
    if merges is None or base_near_branch(root, reviewed, head, base_ref):
        return None
    before = own_patch_id(root, reviewed, base_ref)
    if before is None or before != own_patch_id(root, head, base_ref):
        return None
    return {"from": reviewed, "to": head, "patch_id": before, "merges": merges}


#: The `sd-review` statuses a lead record may carry: no blocking finding (sd:9767).
PASSING_LEAD_REVIEWS = ("clean", "advisory")


def lead_review(connection: Any, root: pathlib.Path, repository: str, branch: str, head: str, base: str,
                binding: dict | None) -> tuple[dict | None, str]:
    """sd:5988. The passing `sd-review --scope branch` pass recorded for `branch` that covers `head`, and why.

    A pass is clean or advisory only (sd:9767): a build's review that cleared
    stands for the lane's at the same head; a blocking one is never recorded.

    It covers `head` when it reviewed `head`, or when `carry_forward` carries
    it across clean merges of `origin/<base>` alone, and only when it diffed
    from the reviewed head's fork from `origin/<base>`, as prepare does. A
    record of another subject, of other review tools, or one that selected an automatic Copilot
    review (which only a lane pass requests) covers nothing. A read or a git
    that cannot answer is unknown, never a cover: the lane reviews in full.
    """
    try:
        row = connection.execute("SELECT body FROM state WHERE id = (SELECT MAX(id) FROM state "
                                 "WHERE kind = 'checkpoint' AND key = ?)",
                                 (sd_lib.lead_review_key(repository, branch),)).fetchone()
        record = json.loads(row["body"]) if row else None
    except Exception as error:  # unknown, so the full review runs
        return None, f"the record did not read ({error})"
    if record is None:
        return None, f"none recorded for {branch}"
    reviewed = record.get("head") if isinstance(record, dict) else None
    if (not isinstance(reviewed, str) or record.get("status") not in PASSING_LEAD_REVIEWS
            or (record.get("repository"), record.get("branch")) != (repository, branch)):
        return None, "it names another subject"
    if binding is None or record.get("binding") != binding:
        return None, "the review tools or policy changed since, or do not read"
    if record.get("copilot_automatic") is not False:
        return None, "it selects an automatic Copilot review"
    # Prepare diffs from the fork from origin/<base>; a pass from a local-only
    # base (sd-review falls back to local main) left the commits between unread.
    fork = sd_lib.git_output(["merge-base", reviewed, f"refs/remotes/origin/{base}"], root)
    if fork is None:
        return None, f"git could not compare {reviewed[:12]} with origin/{base}"
    if record.get("base") != fork:
        return None, f"it reviewed from {str(record.get('base'))[:12]}, not the fork from origin/{base}"
    if reviewed == head:
        return record, f"the {record['status']} pass of {head[:12]} covers it"
    if record.get("head_only"):  # a build's settled pass: its rulings judged that tree alone (sd:10919)
        return None, f"the pass of {reviewed[:12]} settled on rulings, which cover that head alone"
    try:
        carried = carry_forward(root, reviewed, head, f"refs/remotes/origin/{base}")
    except Refusal:
        return None, f"git could not compare {reviewed[:12]} with {head[:12]}"
    if carried is None:
        return None, f"it changed more than a clean merge of origin/{base} since {reviewed[:12]}"
    return ({**record, "carry_forward": carried},
            f"the {record['status']} pass of {reviewed[:12]} covers it; {head[:12]} only merges origin/{base} since")


#: How much younger than its reserved pass a process may read and still be its reviewer: `ps` rounds to seconds.
REVIEWER_AGE_SLACK_SECONDS = 5
#: How long past its execution watchdog a reserved pass still reads live: the watchdog's cleanup and the result's save.
RESERVATION_MARGIN_SECONDS = 600


def reviewer_process() -> dict:
    """The process a reserved pass names, so a second prepare can tell a live review from a dead one (sd:1938)."""
    return {"pid": os.getpid(), "host": socket.gethostname()}


def process_age(pid: int) -> float | None:
    """Seconds since `pid` started, None when no such process runs; infinite when `ps` cannot say."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    try:
        listed = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True,
                                timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return float("inf")
    if not listed:
        return None
    # POSIX `etime`: [[dd-]hh:]mm:ss.
    days, _, clock = listed.rpartition("-")
    try:
        seconds = sum(int(part) * 60 ** index for index, part in enumerate(reversed(clock.split(":"))))
        return seconds + int(days or 0) * 86400
    except ValueError:
        return float("inf")


def reservation_deadline(execution_seconds: int) -> str:
    """When a pass reserved now stops reading live: its execution watchdog, from now, plus the margin (sd:1938).

    From the reservation's save, not the pass's `started_at`: planning ran
    between them, for up to the setup bound, and nothing was reserved yet.
    """
    return (datetime.now(timezone.utc) + timedelta(seconds=execution_seconds + RESERVATION_MARGIN_SECONDS)).isoformat()


def live_reviewer(state: dict, passes: list[dict]) -> dict | None:
    """The process still running the reserved pass, or None when no review is in flight (sd:1938).

    Prepare takes no ship lock, so a second prepare can start while the first
    reviews. It would read the reserved pass as incomplete and spend another on
    `--retry-review`, and the first would lose its receipt to the revision check.
    A pass expires at its `deadline`, written with the reservation (`reservation_deadline`):
    the watchdog has stopped its reviewer by then, so a crash on any machine
    blocks the item for that long at most. Before it, another host's process
    cannot be read here, so it counts as live. A pid that now names a process
    younger than the pass was reused, so it is dead.
    """
    last = passes[-1] if passes else {}
    process = last.get("process")
    if state.get("phase") != "reviewing" or "report" in last or last.get("execution_error") or not isinstance(process, dict):
        return None
    now = datetime.now(timezone.utc)
    try:
        expired = now > datetime.fromisoformat(process["deadline"])
    except (KeyError, TypeError, ValueError):
        expired = True
    if expired:
        return None
    if process.get("host") != socket.gethostname():
        return process
    since = (now - datetime.fromisoformat(last["started_at"])).total_seconds()
    pid = process.get("pid")
    age = process_age(pid) if type(pid) is int and pid > 0 else None
    return None if age is None or age + REVIEWER_AGE_SLACK_SECONDS < since else process


@dataclass(frozen=True)
class ReviewRuntime:
    binding: Callable[[pathlib.Path], str]
    process: Callable[..., subprocess.CompletedProcess]
    current_head: Callable[[pathlib.Path], str]
    clock: Callable[[], str]
    timing_plan: Callable[[subprocess.CompletedProcess], dict]
    bin_dir: pathlib.Path
    setup_seconds: int
    diagnostic_bytes: int
    # The per-file manifest stored beside `binding`, so a moved binding names
    # what moved (sd:1834). None where a test seam supplies a bare digest.
    manifest: Callable[[pathlib.Path], dict] | None = None
    # The folder where a completed review waits until the hub records it
    # (sd:3340, `hold_review`). None where a test seam holds nothing.
    held: Callable[[pathlib.Path], pathlib.Path] | None = None


GATE_NOT_RUN = sd_ship_dispositions.GATE_NOT_RUN


#: The report fields `timing_plan` judges, kept whole in a refused plan's diagnostic (sd:2646): the stdout tail cut them off.
PLAN_FIELDS = ("timing", "requested_reviews", "selection_refusal")


def plan_fields(output: str) -> dict | None:
    """`PLAN_FIELDS` from a review run's output; None when the output is not a JSON object."""
    try:
        report = json.loads(output)
    except (ValueError, RecursionError):
        return None
    return {key: report.get(key) for key in PLAN_FIELDS} if isinstance(report, dict) else None


def complete_report(last: dict, head: str, reviewed_head: str | None) -> dict:
    """The stored report for `head`, refused unless it is whole.

    A blocking report may carry a gate that never ran: sd-review runs the gate
    only after a review that does not block (sd:2605). Any other report needs
    the gate's pass.
    """
    report = last.get("report") or {}
    missing = missing_part(last, report, head, reviewed_head)
    if missing:
        # sd:3379. The refusal names the part that fails, so a lane log says why.
        raise Refusal(f"local review receipt is incomplete or does not name this exact head: {missing}")
    return report


def missing_part(last: dict, report: dict, head: str, reviewed_head: str | None) -> str | None:
    """The first part of a stored pass that keeps it from clearing `head`, or None when it is whole."""
    blocked = report.get("status") == "blocking"
    subject = report.get("subject")
    subject = subject if isinstance(subject, dict) else {}
    gate = (report.get("check") or {}).get("status")
    if last.get("head") != head:
        return f"the last pass reserved {short(last.get('head'))}, not {head[:12]}"
    if subject.get("head") != head:
        return f"its review covers {short(subject.get('head'))}, not {head[:12]}"
    if reviewed_head != head and not blocked:
        return f"no review is recorded as clearing {head[:12]}"
    if report.get("status") not in ("clean", "advisory", "blocking"):
        return f"its review status is {report.get('status') or 'missing'}"
    if gate not in (("pass", GATE_NOT_RUN) if blocked else ("pass",)):
        return f"its gate status is {gate or 'missing'}"
    if not completed_depth(report):
        return f"{report.get('completed_reviews')}/{report.get('requested_reviews')} reviewers completed"
    if blocked and (last.get("exit_code") != 1 or last.get("execution_error")
                    or report.get("scope") != "branch" or report.get("dry_run") or report.get("explain_only")):
        return "its blocking report is not a whole-branch review that exited 1"
    return None


def short(commit: Any) -> str:
    """The first 12 characters of a commit id, or `no head`."""
    return commit[:12] if isinstance(commit, str) and commit else "no head"


def reviewed_commit(report: dict) -> str | None:
    """The commit a report's subject names, when it is a whole commit id (sd:3379)."""
    subject = report.get("subject")
    head = subject.get("head") if isinstance(subject, dict) else None
    return head if isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", head) else None


def validate_findings(report: dict) -> None:
    findings = report.get("findings")
    if not isinstance(findings, list) or any(not isinstance(row, dict) or row.get("disposition") not in ("blocking", "advisory") for row in findings):
        raise Refusal("local review findings are malformed")
    if report.get("status") != "blocking" and any(row["disposition"] == "blocking" for row in findings):
        raise Refusal("local review status contradicts its blocking findings")


def validate_provider_selection(report: dict, requested: str | None, *, completed: bool = False) -> None:
    """An explicit pick authorizes no alternate candidate or completed reviewer."""
    if requested is None:
        return
    timing = report.get("timing")
    candidates = timing.get("candidates") if isinstance(timing, dict) else None
    valid = (isinstance(requested, str) and bool(requested) and report.get("providers") == [requested]
             and report.get("fallback_candidates") == [] and report.get("requested_reviews") == 1
             and isinstance(candidates, list) and len(candidates) == 1 and isinstance(candidates[0], dict)
             and candidates[0].get("name") == requested
             and (not completed or report.get("reviewed_by") == [requested]))
    if not valid:
        raise Refusal("local review does not match the requested reviewer; no fallback is permitted",
                      code="review_provider_mismatch", boundary="provider", state="operator_decision",
                      next_action="Inspect the complete reviewer plan and repeat the intended --provider selection.")


class SharedReview:
    #: The branch this checkout had open when the record was validated, which
    #: the ship lock compares with the live checkout (sd:2008); never re-read.
    checkout_branch: str | None = None

    def __init__(self, root: pathlib.Path, connection, database: pathlib.Path, args, *, store,
                 repository: str, branch: str, head: str, key: str, revision, state: dict,
                 identity: Any, history: Any, runtime: ReviewRuntime):
        self.store, self.root, self.connection, self.args = store, root, connection, args
        self.database, self.repository, self.branch = database, repository, branch
        self.source_head, self.key, self.revision, self.state = head, key, revision, state
        self.identity, self.history, self.runtime = identity, history, runtime

    def save(self, **updates) -> None:
        self.state.update(updates)
        self.revision = self.store.save(self.connection, self.key, self.revision, self.history.stamp(self.state))

    def read_only(self) -> bool:
        """The connection refuses writes: `sd-ship` opens it so for a read verb (`connect(write=False)`)."""
        return self.connection is not None and bool(self.connection.execute("PRAGMA query_only").fetchone()[0])

    def refuse_live_review(self) -> None:
        """Refuse before anything is saved while another process reviews this record (sd:1938).

        `--restart-review REASON` is the operator's explicit word, so it sets aside a pass that still reads live.
        """
        if getattr(self.args, "restart_review", None) is not None:
            return
        process = live_reviewer(self.state, self.history.native(self.state))
        if process is not None:
            raise Refusal(f"pid {process.get('pid')} on {process.get('host')} is still reviewing this record; "
                          "a second prepare would spend another pass and discard that review's receipt",
                          code="review_running", boundary="runtime", state="retryable_failure",
                          next_action="Wait for that prepare to finish. If its process died, prepare again: "
                                      "the reserved pass then reads as incomplete, and --retry-review continues it.")

    def result(self, phase: str, **extra) -> dict:
        result = self.identity.result_fields(phase, self.state, self.runtime.clock(), extra)
        result["workflow"] = success(phase, observed_only=bool(extra.get("observed_only")))
        passes = self.history.native(self.state)
        if passes:
            report = passes[-1].get("report") or {}
            result["review_selection"] = {"requested_provider": passes[-1].get("requested_provider"),
                                          "reviewed_by": report.get("reviewed_by", [])}
            carried = self.state.get("review_carry_forward") or {}
            if carried.get("to") and carried.get("to") == self.state.get("reviewed_head"):
                result["review_carry_forward"] = carried
        skipped = self.state.get("review_skipped") or {}
        if skipped.get("head") and skipped.get("head") == self.state.get("reviewed_head"):
            result["review_skipped"] = skipped["tier"]  # sd:2998
        return result

    def review_inputs(self, head: str) -> dict:
        passes = self.history.native(self.state)
        waived = self.state.get("empty_diff") or {}
        if not passes and waived.get("head") == head and empty_branch_base(self.root, head) == waived.get("base"):
            return {"status": "clean", "findings": [], "subject": {"base": waived["base"], "head": head, "paths": []}}
        skipped = self.state.get("review_skipped") or {}
        if not passes and skipped.get("head") == head and (skipped.get("check") or {}).get("status") == "pass":
            # sd:2998. The gate alone cleared a docs-only or pin-bump head.
            return {"status": "clean", "findings": [], "review_skipped": skipped["tier"],
                    "subject": {"base": skipped["base"], "head": head, "paths": []}}
        if not passes:
            raise Refusal("no completed local review receipt for this head")
        if not self.binding_holds():
            raise self.binding_refusal()
        subject = self.carried_from(head, passes[-1])
        try:
            report = complete_report(passes[-1], subject, subject if subject != head else self.state.get("reviewed_head"))
        except Refusal as refusal:
            note = base_merge_note(self.root, passes[-1].get("head"), head, self.state.get("base"))
            refusal.args = (f"{refusal.args[0]}{note}",)
            raise
        validate_provider_selection(report, passes[-1].get("requested_provider"), completed=True)
        self.history.validate_coverage(self.state, report)
        validate_findings(report)
        return report

    def carried_from(self, head: str, last: dict) -> str:
        """The head the last pass reviewed, when a recorded carry covers `head` (sd:1485), else `head`."""
        carried = self.state.get("review_carry_forward") or {}
        if (carried.get("to") == head and carried.get("from") == last.get("head")
                and self.state.get("reviewed_head") == head
                and (last.get("report") or {}).get("status") in ("clean", "advisory")):
            return carried["from"]
        return head

    def carried_review(self, head: str) -> bool:
        """sd:1485. Carry the last cleared review to a clean merge-in of the base.

        Only a clean or advisory receipt: a blocking one is cleared by
        dispositions bound to its own head. `carry_forward` decides; the
        receipt records what it found, and the output names it.

        A review that cleared before its gate failed (`kept_gate_failure`)
        carries too, and the gate runs alone at the new head (`rerun_gate`,
        sd:3474). The lane prepares with `--catch-up`, so a busy base moved
        the head before the retry, and 26 of 32 kept passes paid a full
        review all the same. Such a pass names no reviewed head.
        """
        passes, base = self.history.native(self.state), self.state.get("base")
        report = (passes[-1].get("report") or {}) if passes else {}
        kept = kept_gate_failure(report)
        if not passes or not base or not (self.state.get("reviewed_head") or kept):
            return False
        if (report.get("status") not in ("clean", "advisory") and not kept) or not completed_depth(report) \
                or self.binding_moved():
            return False
        # A named reviewer is a selection a receipt from another cannot replace.
        requested = getattr(self.args, "provider", None)
        if requested is not None and report.get("reviewed_by") != [requested]:
            return False
        carried = carry_forward(self.root, passes[-1]["head"], head, f"refs/remotes/origin/{base}")
        if carried is None:
            return False
        carried = {**carried, "recorded_at": self.runtime.clock()}
        print(f"sd-ship: {head[:12]} adds only clean merges of origin/{base} to reviewed {carried['from'][:12]}, "
              "and the branch's own patch-id is unchanged; its review carries forward, no provider called"
              + ("; the gate that failed after it runs again here" if kept else ""), file=sys.stderr)
        if kept:
            self.rerun_gate(head, carried)
            return True
        self.save(reviewed_head=head, review_carry_forward=carried)
        self.check_review(head)
        return True

    def check_review(self, head: str, *, refresh_adjudication: bool = False, run_gate: bool = False) -> dict | None:
        report = self.review_inputs(head)
        if report["status"] == "blocking":
            clearance = sd_ship_dispositions.accepted(self, head, run_tests=run_gate)  # a read runs no test (sd:10893)
            if (report.get("check") or {}).get("status") == GATE_NOT_RUN:
                self.adjudicated_gate(head, run=run_gate)
            if not refresh_adjudication and self.state.get("review_clearance") != clearance:
                raise Refusal("accepted dispositions changed; prepare again before publishing or merging")
            return clearance
        return None

    def adjudicated_gate(self, head: str, *, run: bool) -> None:
        """sd:2605. The gate a blocking review never ran, run once its dispositions are accepted.

        Adjudication accepts a blocking report whose `check` is `not_run`, so
        the head has passed no test when its findings clear. The prepare that
        reuses that report runs the gate here, before clearance, and records it
        as `adjudicated_gate`; publication, merge and `verify-review` read the
        record and never run one. A failure keeps the pass: its review found
        blocking findings, which is a spent pass as it always was.
        """
        gate = self.state.get("adjudicated_gate") or {}
        if gate.get("head") == head and gate.get("status") == "pass":
            return
        if not run:
            raise Refusal(f"the review of {head[:12]} blocked before the gate ran, and no gate has passed there "
                          "since its dispositions were accepted",
                          code="gate_not_run", boundary="review", state="retryable_failure",
                          next_action="Run the review or prepare again with the same identity; it reuses the "
                                      "adjudicated review and runs the gate at this head before clearance.")
        base = self.gate_check_base()
        check = adjudicated_check(self.root, head, base, self.database if base else None,
                                  getattr(self.args, "review_timeout", None) or sd_lib.GATE_CHECK_SECONDS,
                                  slot_timeout=getattr(self.args, "slot_timeout", None) or sd_lib.GATE_SLOT_SECONDS)
        self.save(adjudicated_gate={**check, "head": head, "recorded_at": self.runtime.clock()})
        if check["status"] != "pass":
            said = "\n".join(failing_check_tails(check.get("checks"))) or check.get("detail") or "no check output"
            raise Refusal(f"the repository gate failed at {head[:12]} after its blocking findings were adjudicated; "
                          f"the review pass stays spent: {said}",
                          code=gate_code(check), boundary="runtime", state="retryable_failure",
                          next_action="Fix the gate and prepare again; the fix is reviewed as a fix verification.")

    def rerun_gate(self, head: str, carried: dict | None = None) -> None:
        """sd:2721. The gate alone, at the head whose review cleared before its gate failed.

        `release_gate_failure` kept that pass, so a gate that fails under load
        costs a gate run rather than a review. A pass puts back the status the
        review cleared with and this gate's check, keeping the failed one as
        `failed_check`; the report then reads as any cleared review does. A
        failure keeps the pass as it was.

        `carried` is the `carry_forward` record when `head` is a clean
        catch-up of the pass's head (sd:3474); it is saved with the pass in
        one write, so a gate that fails or a killed run leaves no carry.
        """
        base = self.gate_check_base()
        check = adjudicated_check(self.root, head, base, self.database if base else None,
                                  getattr(self.args, "review_timeout", None) or sd_lib.GATE_CHECK_SECONDS,
                                  slot_timeout=getattr(self.args, "slot_timeout", None) or sd_lib.GATE_SLOT_SECONDS)
        passes = self.history.native(self.state)
        flake = self.gate_flake(check, head) if check["status"] != "pass" else None
        if flake is not None:
            check = flake
        if check["status"] != "pass":
            self.save(review_preflight_error={"kind": "gate_failed", "stage": "check", "head": head,
                                              "exit_code": check.get("exit_code"),
                                              "checks": gate_diagnostics(check, self.runtime.diagnostic_bytes)})
            said = "\n".join(failing_check_tails(check.get("checks"))) or check.get("detail") or "no check output"
            raise Refusal(f"the repository gate failed again at {head[:12]}; its review pass stays kept: {said}",
                          code=gate_code(check), boundary="runtime", state="retryable_failure",
                          next_action="Fix the gate and prepare again; the fix is reviewed as a fix verification.")
        report = passes[-1]["report"]
        passes[-1].update(exit_code=0, report={**report, "status": report["cleared_status"], "check": check,
                                               "failed_check": report["check"]})
        self.save(passes=passes, reviewed_head=head, review_preflight_error=None,
                  **({"review_carry_forward": carried} if carried else {}), **flake_record(check))
        self.check_review(head)

    def flaked(self, report: dict, head: str, passes: list[dict], exit_code: int) -> dict:
        """The cleared report a flake stands for (sd:9768); without one, the gate failure refuses as before."""
        flake = self.gate_flake(report.get("check") or {}, head) if kept_gate_failure(report) else None
        if flake is None:
            self.release_gate_failure(report, head, passes, exit_code)
        return {**report, "status": report["cleared_status"], "check": flake, "failed_check": report["check"]}

    def gate_flake(self, check: dict, head: str) -> dict | None:
        """Rerun a failed gate's failing tests once, alone; the passing check that stands for the gate, or None
        (sd:9768).

        None, and the gate fails as before, unless every failure is a unittest test (`failed_tests`), this
        receipt took no flake at `head` already (once per entry), and no test failed a rerun in the last
        `FLAKE_DAYS` days. A rerun that passes is noted on the item and as a comment on the suite's flake
        item, the list the lane already files (sd:6739). It leaves no gate receipt: the merge runs its own gate.
        """
        item = getattr(self.identity, "item", None)
        tests = failed_tests(check)
        if self.connection is None or item is None or not tests or (self.state.get("gate_flake") or {}).get("head") == head:
            return None
        since = (datetime.now(timezone.utc) - timedelta(days=FLAKE_DAYS)).isoformat(timespec="seconds")
        for test in tests:
            failed = f"{RERUN_FAILED_NOTE}{test} at "
            if self.connection.execute("SELECT 1 FROM note WHERE timestamp >= ? AND substr(body, 1, ?) = ?",
                                       (since, len(failed), failed)).fetchone():
                print(f"sd-ship: {test} failed a rerun in the last {FLAKE_DAYS} days; no flake retry", file=sys.stderr)
                return None
        code, out, err = rerun_tests(self.root, head, tests)
        if code == 0 and FAILING_OUTCOME.search(f"{out}\n{err}"):
            code = None  # a skipped or unexpectedly passing rerun is no pass, and bars nothing
        sd_lib.import_sd_db()
        from sd_db.database import transaction  # noqa: PLC0415
        from sd_db.writes import add_note  # noqa: PLC0415
        if code != 0:
            # Only a test the rerun ran and saw fail is barred: one that did not load or run is not (sd:9768).
            # A header the parser cannot read bars every test it reran: the omitted one may be any of them.
            parsed = named_failures(f"{out}\n{err}") if code is not None else []
            named = set(tests) if parsed is None else {paren if paren.endswith(f".{name}") else f"{paren}.{name}"
                                                       for name, paren in parsed}
            with transaction(self.connection):
                for test in tests:
                    if test in named:
                        add_note(self.connection, item, "comment",
                                 f"{RERUN_FAILED_NOTE}{test} at {head[:12]}: it failed alone too (sd:9768)")
            print(f"sd-ship: the failing tests failed again alone ({rerun_reason(code, out, err)}); "
                  "the gate failure stands",
                  file=sys.stderr)
            return None
        flake = {"head": head, "tests": tests, "recorded_at": self.runtime.clock()}
        import sd_lane  # noqa: PLC0415 -- the flake list is the lane's
        with transaction(self.connection):
            repo = self.connection.execute("SELECT repo FROM item WHERE id = ?", (item,)).fetchone()
            for test in tests:
                said = f"{test} failed the gate at {head[:12]} and passed alone on a rerun"
                add_note(self.connection, item, "comment", f"{FLAKE_NOTE}{said}; prepare went on (sd:9768)")
                if repo is not None and repo[0]:
                    flaky = sd_lane.flake_item(self.connection, test_suite(test), repo[0], self.repository)
                    add_note(self.connection, flaky, "comment", f"Gate flake in {self.repository}: {said} "
                             f"(sd:{item}). Fix the suite at its root with a test that fails first.")
        # No save here: each caller saves `gate_flake` with the cleared review in one write, so a stop between
        # them leaves the retry unspent rather than spent with nothing to reuse it.
        print(f"sd-ship: {', '.join(tests)} passed alone on a rerun; recorded as a flake, prepare goes on",
              file=sys.stderr)
        return {"status": "pass", "exit_code": 0, "detail": f"flake: {', '.join(tests)} passed alone", "flake": flake}

    def adjudicate(self) -> dict:
        return sd_ship_dispositions.adjudicate(self)

    def additional_request(self, head: str, passes: list[dict]) -> dict | None:
        if passes != self.history.native(self.state):
            raise Refusal("additional review must use the complete stored native history")
        args = self.args
        if all(value is None for value in (args.additional_review_for, args.request_reason, args.review_history_digest)):
            return None
        reason = (args.request_reason or "").strip()
        # One refusal per fault, each naming the flag that fixes it (sd:2026).
        if args.additional_review_for is None:
            raise Refusal("--request-reason and --review-history-digest are valid only with --additional-review-for SHA; "
                          "drop them, or name the exact committed head for one more review")
        if args.additional_review_for != head:
            raise Refusal(f"--additional-review-for {args.additional_review_for} is not the clean current HEAD {head}")
        if not reason:
            raise Refusal("--additional-review-for needs a nonempty --request-reason")
        if args.retry_review:
            raise Refusal("--retry-review and --additional-review-for do not combine; a retry spends an automatic pass")
        if args.path or args.message_file:
            raise Refusal("additional review cannot commit; name an already committed exact head")
        self.request_history(args.review_history_digest)
        if self.runtime.current_head(self.root) != head:
            raise Refusal("additional review must name the clean current HEAD")
        return {"head": head, "reason": reason, "recorded_at": self.runtime.clock(), "allowed_passes": 1,
                "prior_history_digest": self.history.history_digest(self.state),
                "operator_context": "untrusted assertion, not proof of user approval", **self.history.request_fields(self.state)}

    def request_history(self, supplied_digest: str | None) -> None:
        cap = AUTOMATIC_CODE_REVIEW_PASSES
        spent = self.history.spent(self.state)
        continuation = self.history.requires_continuation(self.state)
        history_digest = self.history.history_digest(self.state)
        if spent < cap and not continuation:
            raise Refusal(f"additional review requires {cap} spent passes")
        if spent == cap and not continuation and supplied_digest is not None:
            raise Refusal(f"--review-history-digest renews only after at least {cap + 1} spent passes")
        if (spent > cap or continuation) and supplied_digest != history_digest:
            raise Refusal(f"renewal requires --review-history-digest {history_digest} for the complete current history; each digest is spent once")
        self.history.validate_requests(self.state)
        self.history.aggregate(self.state)

    def binding_changes(self) -> list[tuple[str, str]]:
        """What moved since the stored manifest, by entry and class (sd:1246).

        Called only once the digests differ: the digest decides, and this
        names. Gate and check entries ride along, marked, never decisive.
        """
        manifest = getattr(self.runtime, "manifest", None)
        if manifest is None:
            return []
        return sd_ship_bindings.binding_change(self.state.get("binding_manifest"), manifest(self.root))

    def binding_refusal(self) -> Refusal:
        detail = sd_ship_bindings.describe_change(self.binding_changes())
        return Refusal("review tools or repository policy changed after review" + (f": {detail}" if detail else ""),
                       code="review_binding_moved", boundary="review", state="operator_decision",
                       next_action="Run sd-ship prepare again; it re-reviews this head in full (sd:1390).")

    def binding_moved(self) -> bool:
        """The stored receipt was produced by review tools or policy now gone.

        Reuse was decided on head equality alone and the receipt was then
        rejected on this manifest inside `review_inputs`, with nothing between
        them saying "the binding moved, therefore review again" -- so a branch
        whose review completed and whose head then stopped moving was bricked
        by the next unrelated landing on the default branch that touched a
        review tool file, with no verb able to clear it (sd:1390, live on
        #1140).

        Read lazily and memoized, never eagerly. `review_binding` reaches
        `sd_lib.main_worktree_root`, which shells out to `git rev-parse`, and
        the cap refusal below has to come first: a pass past the cap is
        refused before anything is dispatched or even asked, which is what
        `test_the_pass_after_the_cap_refuses_and_an_explicit_request_still_admits_it`
        pins by making the binding read itself raise, not every subprocess.
        """
        if not hasattr(self, "_binding_moved"):
            self._binding_moved = bool(self.history.native(self.state)) and not self.binding_holds()
        return self._binding_moved

    def binding_holds(self) -> bool:
        return self.state.get("binding") == self.runtime.binding(self.root) or self.request_unchanged()

    def request_unchanged(self) -> bool:
        """sd:1397, option E: only review code moved, and the reviewers would be asked the same.

        `sd-review --explain` is replayed for the stored pass, with the prior
        report it was handed, and its `request_sha256` compared with the one
        the pass recorded. Equal, the receipt is rebound and the record says
        so; no pass is spent. Policy stays byte-exact, a legacy receipt has no
        request to replay, a moved `FINDING_FILES` member could judge the same
        request differently, and any doubt re-reviews.
        """
        passes = self.history.native(self.state)
        request = (passes[-1].get("review_request") if passes else None) or {}
        changed, manifest = self.binding_changes(), getattr(self.runtime, "manifest", None)
        if (manifest is None or not request.get("sha256") or not changed
                or any(kind not in ("verdict", "gate", "check") or name in sd_ship_bindings.FINDING_FILES
                       for name, kind in changed)
                or self.replayed_request(passes, request) != request["sha256"]):
            return False
        kept = {"head": passes[-1].get("head"), "recorded_at": self.runtime.clock(), "superseded_binding": self.state.get("binding"),
                "changed": [list(row) for row in changed], "request_sha256": request["sha256"]}
        rebound = {"binding": self.runtime.binding(self.root), "binding_manifest": manifest(self.root),
                   "review_binding_kept": [*(self.state.get("review_binding_kept") or []), kept]}
        if self.read_only():
            # pack sd:7576. A read verb, such as the template `sd-ship adjudicate`
            # prints, rebinds in memory only: saving failed it with "attempt to
            # write a readonly database". The next writing verb replays and records it.
            self.state.update(rebound)
        else:
            self.save(**rebound)
        return True

    def replayed_request(self, passes: list[dict], request: dict) -> str | None:
        """The request digest `--explain` gives now for the last pass; None when it cannot be rebuilt."""
        last = passes[-1]
        argv = sd_review_request.review_argv(self.runtime.bin_dir, self.database,
                                             SimpleNamespace(provider=last.get("requested_provider")), last.get("base"))
        wanted = request.get("verify") or request.get("resume")
        with tempfile.TemporaryDirectory(prefix="sd-ship-replay-") as directory:
            if wanted:
                # The prior is rebuilt the two ways `review` builds it, and used only if it is the one handed over.
                earlier = dict(self.state, passes=passes[:-1])
                candidates = [self.history.prior(earlier)]
                try:
                    aggregate = self.history.aggregate(earlier)
                    candidates += [aggregate, legacy(aggregate)]  # a request stored before sd:10008 binds the old shape
                except Refusal:
                    pass
                prior = next((row for row in candidates if digest(row) == wanted), None)
                if prior is None:
                    return None
                path = pathlib.Path(directory) / "prior-review.json"
                path.write_text(json.dumps(prior, sort_keys=True), encoding="utf-8")
                argv += ["--verify-report" if request.get("verify") else "--resume-report", str(path)]
            try:
                planned = self.runtime.process(self.root, argv + ["--explain"], timeout=self.runtime.setup_seconds)
                return json.loads(planned.stdout).get("request_sha256") if planned.returncode == 0 else None
            except (ReviewTimeout, ValueError, AttributeError):
                return None

    def reusable_review(self, head: str, prior: dict, retry: bool, additional: bool) -> bool:
        if additional:
            return False
        requested = getattr(self.args, "provider", None)
        # A carried head is covered by the receipt of the head it came from (sd:1485).
        passes = self.history.native(self.state)
        covered = self.carried_from(head, passes[-1]) if passes else head
        if (requested is not None and prior.get("subject", {}).get("head") == covered
                and completed_depth(prior) and prior.get("reviewed_by") != [requested]):
            raise Refusal("completed receipt does not match the requested reviewer; selection cannot replace review evidence",
                          code="review_provider_mismatch", boundary="provider", state="operator_decision",
                          next_action="Reuse the recorded reviewer, or obtain a permitted additional-review request.")
        # A receipt bound to tools or policy that have since changed is not
        # reusable. The manifest is read here, inside each path that would
        # otherwise reuse, rather than once above: each of these ends in
        # `check_review`, which refuses on exactly this manifest, so reuse was
        # being selected on head equality and then rejected on the binding.
        if not retry and prior.get("status") == "blocking" and prior.get("subject", {}).get("head") == head:
            if self.binding_moved():
                return False
            clearance = self.check_review(head, refresh_adjudication=True, run_gate=True)
            self.save(reviewed_head=head, review_clearance=clearance)
            return True
        if not retry and kept_gate_failure(prior) and prior.get("subject", {}).get("head") == head:
            if self.binding_moved():
                return False
            self.rerun_gate(head)
            return True
        if self.state.get("reviewed_head") == head and (not retry or completed_depth(prior)):
            if self.binding_moved():
                return False
            self.check_review(head)
            return True
        return not retry and self.carried_review(head)

    def validate_dispatch(self, head: str, prior: dict, retry: bool, additional: bool) -> None:
        passes = self.history.native(self.state)
        if not additional and (self.history.spent(self.state) >= AUTOMATIC_CODE_REVIEW_PASSES
                               or self.history.requires_continuation(self.state)):
            raise Refusal(f"all {AUTOMATIC_CODE_REVIEW_PASSES} automatic code review passes are spent; "
                          "further review requires an explicit new request")
        if retry and (not passes or completed_depth(prior)):
            raise Refusal("--retry-review can continue only an incomplete review")
        # A moved binding re-reviews the branch at the head it already covered,
        # so neither refusal below applies: the fix-verification shape is not
        # what is being dispatched, and the already-reviewed head is the whole
        # point. Leaving them in place only moves the deadlock one line down.
        # The cap above still holds and is read first -- a re-review spends a
        # pass, and a pass past the cap is refused before the binding is even
        # asked for, which is why `binding_moved` is last in this condition.
        if passes and not retry and not additional and not self.binding_moved():
            if not completed_depth(prior):
                # The code is what the lane reads to spend its one automatic retry (sd:3037).
                raise Refusal("the preceding review did not complete its requested depth; use --retry-review for a full-branch retry",
                              code="review_incomplete", boundary="review", state="operator_decision",
                              next_action="Prepare again with --retry-review; it spends one automatic pass.")
            if passes[-1].get("head") == head:
                raise Refusal("this head was already reviewed; address its findings before the fix verification")
        for previous in self.history.ancestry_heads(self.state):
            if not is_ancestor(self.root, previous, head):
                item = getattr(self.identity, "item", None)
                # The reset publishes the reviewed commit, so it cannot be the
                # only way out of a rewrite made to keep a line unpublished (sd:2600).
                restart = (f"If the old history must not be pushed, as after a privacy amend, run "
                           f"`sd-ship prepare --item {item} --restart-review REASON`: it sets the orphaned review "
                           "aside and reviews the whole branch again, one pass against the cap. "
                           "If the amend only edited a commit message, r" if item is not None else "R")
                raise Refusal(
                    f"reviewed head {previous} is not an ancestor of the offered head {head} on {self.branch}; "
                    "an amend or a rebase after a review orphans the reviewed head, and a review of a "
                    "commit the branch cannot reach is not evidence about the branch",
                    code="reviewed_head_orphaned", boundary="review", state="operator_decision",
                    next_action=f"{restart}estore a history that contains {previous}: after an amend, run "
                                f"`git reset --soft {previous}`, commit the change on top, then prepare again. "
                                "An unchanged retry refuses the same way.")

    def restart_review(self, head: str) -> None:
        """sd:2600. Set an orphaned review aside, with its reason, for one fresh full-branch review.

        An amend or a rebase after review leaves reviewed heads the branch
        cannot reach, and `validate_dispatch` refuses them. `git reset --soft`
        onto the reviewed head restores the ancestry, but it also publishes the
        reviewed commit, which is closed when the amend removed something that
        may not reach the remote. sd-review refuses to resume a report whose
        head is not an ancestor, so the orphaned findings cannot be carried
        into the new pass. They stay in the receipt under `superseded_reviews`,
        and their passes still count against the cap (`set_aside`).
        """
        reason = restart_reason(self.args)
        orphaned = [previous for previous in self.history.ancestry_heads(self.state)
                    if self.unreachable(previous, head)]
        if not orphaned:
            raise Refusal("--restart-review applies only when a reviewed head is not an ancestor of HEAD; "
                          "this branch still contains every reviewed head, so their findings stand",
                          code="restart_review_unneeded", boundary="input", state="operator_decision",
                          next_action="Prepare again without --restart-review.")
        if self.history.spent(self.state) >= AUTOMATIC_CODE_REVIEW_PASSES:
            raise Refusal(f"all {AUTOMATIC_CODE_REVIEW_PASSES} automatic code review passes are spent, "
                          "set-aside passes included; a restart spends one more")
        restart = {"reason": reason, "recorded_at": self.runtime.clock(), "head": head, "orphaned": orphaned,
                   "history_digest": self.history.history_digest(self.state),
                   "passes": self.history.native(self.state)}
        self.state.pop("review_carry_forward", None)
        self.save(passes=[], reviewed_head=None, review_clearance=None,
                  superseded_reviews=[*(self.state.get("superseded_reviews") or []), restart])
        print(f"sd-ship: set aside {len(restart['passes'])} pass(es) whose heads this branch cannot reach "
              f"({', '.join(previous[:12] for previous in orphaned)}); reviewing {head[:12]} in full",
              file=sys.stderr)

    def unreachable(self, previous: str, head: str) -> bool:
        """Whether `head` cannot reach the reviewed head `previous`, for a restart (sd:3167).

        A head gc pruned after an amend is the orphan a restart exists for, so
        when `is_ancestor` cannot answer, a commit the store no longer holds
        counts as unreachable; any other failure still raises. `validate_dispatch`
        keeps `is_ancestor` strict (sd:1348).
        """
        try:
            return not is_ancestor(self.root, previous, head)
        except Refusal:
            if commit_absent(self.root, previous):
                return True
            raise

    def authorship_start(self) -> str:
        """Where this branch's commits begin, for trailer and vendor reads."""
        passes = self.state.get("passes") or []
        if not passes:
            return (self.state.get("empty_diff") or self.state["review_skipped"])["base"]
        return passes[-1]["report"].get("authorship_base") or passes[0]["report"]["subject"]["base"]

    def gate_check_base(self) -> str | None:
        """The base sd-review runs the gate check against, or None (sd:2041).

        Under `repo.ci = local` the merge gate runs this same check again; run
        it as the gate does, so its receipt answers there.
        """
        if self.state.get("base") and sd_lib.repo_ci(self.connection, self.root) == "local":
            return self.state["base"]
        return None

    def request_verifies_fix(self, passes: list[dict], head: str) -> bool:
        """sd:2147. Whether a post-cap request reviews the diff since the last reviewed head.

        That is the subject an automatic pass would take at this head. A
        request used to review the whole branch unconditionally, so a branch
        whose fix deltas each fit the review input cap outgrew it on the one
        pass the operator asked for (ui-design #19: 2.1 MB over 85 files).
        The whole branch is still reviewed where a fix verification has no
        sound base: no completed pass to verify, the same head again (an
        empty range), a moved review binding, or an imported history whose
        every native pass is a full-branch continuation. A catch-up merge is
        read after this, by `caught_up_pass`, as it is for an automatic pass.
        The completed pass is the last one that verified something: a spent
        request that reviewed nothing and kept no evidence is skipped (sd:2192).
        """
        if not passes or self.history.requires_continuation(self.state):
            return False
        index = verified_index(passes)
        if index is None or passes[index]["head"] == head:
            return False
        return not self.binding_moved()

    @staticmethod
    def verified_pass(passes: list[dict], additional: bool) -> dict:
        """The pass a fix verification continues: for a request, the one `request_verifies_fix` read."""
        index = verified_index(passes) if additional else None
        return passes[-1 if index is None else index]

    def dispatch_prior(self, passes: list[dict], prior: dict, full: bool, additional: bool) -> dict:
        """The evidence a pass resumes, or the report a fix verification verifies (sd:2192)."""
        if full:
            return self.history.aggregate(self.state)
        if additional and reservation(passes[-1]):
            return self.verified_pass(passes, additional)["report"]
        return prior

    def caught_up_pass(self, passes: list[dict], head: str, whole: bool, additional: bool = False) -> dict | None:
        """Where the base was merged in since the last pass, when it was (sd:2023).

        From the previous head, the range then holds every commit the base
        brought in, and a fix verification would review code this branch never
        touched, so the pass is full-branch. Read from git ancestry, not from
        the `--catch-up` flag: a prepare that fails after the merge, or a merge
        done by hand, must be scoped the same way on the next run.
        """
        base = self.state.get("base")
        if not passes or whole or not base:
            return None
        previous = self.verified_pass(passes, additional)["head"]
        fork = sd_lib.git_output(["merge-base", head, f"refs/remotes/origin/{base}"], self.root)
        if not fork or is_ancestor(self.root, fork, previous):
            return None
        return {"from": previous, "base": fork}

    def covered_head(self, last: dict, report: dict) -> str:
        """sd:3379. The head a finished pass reviewed, with the pass moved under it when that is not the one reserved.

        sd-review reads HEAD itself, after prepare read it. An amend between
        the two reads left a review of the amended commit stored under the
        old one, with that old head as `reviewed_head`. `complete_report`
        then refused it on every prepare, and neither --retry-review nor
        --restart-review could see why: the pass named a head the branch
        still held. Under the head it covers, the review is reused there, and
        a branch that cannot reach it gets the orphaned-head refusal and its
        --restart-review remedy. `reserved_head` keeps the head prepare read.
        A pass an operator request bought stays where it is: the request
        binds its head (`validate_additional_requests`), and `record_review`
        releases such a pass instead (`release_moved_request`).
        """
        covered, reserved = reviewed_commit(report), last["head"]
        if covered is None or covered == reserved or last.get("additional_review_request"):
            return reserved
        last.update(head=covered, reserved_head=reserved)
        print(f"sd-ship: HEAD moved from {reserved[:12]} to {covered[:12]} while the review ran; "
              f"the review covers {covered[:12]}, so its pass is kept under that head", file=sys.stderr)
        return covered

    def rekey_moved_pass(self) -> None:
        """sd:3379. Move a stored review of another head under the head it covers, as `record_review` now does."""
        passes = self.history.native(self.state)
        if not passes or not isinstance(passes[-1].get("report"), dict):
            return
        reserved = passes[-1]["head"]
        covered = self.covered_head(passes[-1], passes[-1]["report"])
        if covered != reserved:
            cleared = self.state.get("reviewed_head")
            self.save(passes=passes, reviewed_head=covered if cleared == reserved else cleared)

    def review(self, head: str) -> None:
        # Before a restart reads which reviewed heads the branch cannot reach.
        self.rekey_moved_pass()
        if getattr(self.args, "restart_review", None) is not None:
            self.restart_review(head)
        # A resumed review is on the hub now; what follows treats it as any recorded pass (sd:3340).
        self.resume_held(head)
        passes = self.history.native(self.state)
        if not passes and (base := empty_branch_base(self.root, head)):
            print(f"sd-ship: {head[:12]} changes no file against {base[:12]}; local review skipped, no provider called",
                  file=sys.stderr)
            self.save(empty_diff={"head": head, "base": base}, reviewed_head=head)
            return
        prior = self.history.prior(self.state)
        retry = bool(self.args.retry_review)
        request = self.additional_request(head, passes)
        additional = request is not None
        # sd:2147. A request verifies the fix like the automatic pass it
        # follows, where one would; only a request with no such pass reviews
        # the whole branch again.
        whole = additional and not self.request_verifies_fix(passes, head)
        if self.reusable_review(head, prior, retry, additional):
            return
        self.validate_dispatch(head, prior, retry, additional)
        # Only past the cap check, so a spent item refuses before the manifest
        # is read. A re-review forced by a moved binding is a full-branch pass,
        # not a fix verification: `--base <previous head>` would be this same
        # head and would review an empty range, which is a rubber stamp rather
        # than a review. It resumes the complete prior history so no earlier
        # blocker is dropped, exactly as a whole-branch post-cap request does.
        # sd:2192. A request under a moved binding is marked too, so a later
        # request cannot verify past it if it fails (`verified_index`).
        moved = self.binding_moved()
        caught_up = self.caught_up_pass(passes, head, whole, additional)
        full = whole or moved or caught_up is not None
        prior = self.dispatch_prior(passes, prior, full, additional)
        base = self.verified_pass(passes, additional)["head"] if passes and not (retry or full) else None
        argv = sd_review_request.review_argv(self.runtime.bin_dir, self.database, self.args, base, gate_check=self.gate_check_base())
        requested = getattr(self.args, "provider", None)
        passes.append({"head": head, "started_at": self.runtime.clock(), "base": base, "retry": retry,
                       "requested_provider": requested, "process": reviewer_process()})
        if request:
            passes[-1]["additional_review_request"] = request
        if caught_up is not None:
            passes[-1]["catch_up"] = caught_up
        if moved:
            passes[-1]["review_binding_change"] = {"head": head, "recorded_at": self.runtime.clock(),
                                                   "superseded_binding": self.state.get("binding"),
                                                   "changed": [list(row) for row in self.binding_changes()]}
        # The pid lets the next gate start remove the folder a killed run left (sd:3059, `sd_gate_cache.OWNED`).
        with tempfile.TemporaryDirectory(prefix=f"sd-ship-verify-{os.getpid()}-") as directory:
            if prior:
                prior_path = pathlib.Path(directory) / "prior-review.json"
                prior_path.write_text(json.dumps(prior, sort_keys=True), encoding="utf-8")
                argv += ["--resume-report" if retry or full else "--verify-report", str(prior_path)]
            result = self.execute_review(argv, head, passes)
        self.record_held(result, head, passes, self.hold_review(result, head, passes))

    def held_path(self) -> pathlib.Path | None:
        """sd:3340. Where this record's completed review waits for the hub; None without a lane folder."""
        if self.runtime.held is None:
            return None
        try:
            folder = self.runtime.held(self.root)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"sd-ship: no lane folder to hold the review in ({error})", file=sys.stderr)
            return None
        return folder / (hashlib.sha256(self.key.encode()).hexdigest()[:32] + ".json")

    def held_binding(self, head: str, passes: list[dict]) -> dict:
        """What a held review answers for: this record, the head, the reserved pass and the tools that ran it."""
        return {"version": HELD_VERSION, "key": self.key, "repository": self.repository, "branch": self.branch,
                "head": head, "passes": len(passes), "reservation": digest(passes[-1]) if passes else None,
                "binding": self.state.get("binding"), "binding_manifest": digest(self.state.get("binding_manifest"))}

    def hold_review(self, result: subprocess.CompletedProcess, head: str, passes: list[dict]) -> pathlib.Path | None:
        """sd:3340. Write the completed review beside the lane before the hub save, so a hub fault keeps it.

        A part file renamed into place, so a reader never sees half a record.
        A failed write holds nothing and the save goes ahead, as before.
        """
        path = self.held_path()
        if path is None:
            return None
        argv = result.args if isinstance(result.args, list) else [result.args]
        record: dict[str, Any] = {"binding": self.held_binding(head, passes),
                  "superseded": {key: value for key, value in self.superseded.items() if value is not ABSENT},
                  "result": {"args": [str(part) for part in argv], "returncode": result.returncode,
                             "stdout": result.stdout, "stderr": result.stderr}}
        part = path.with_name(path.name + ".part")
        try:
            record["digest"] = digest(record)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with os.fdopen(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(part, path)
        except (OSError, TypeError, ValueError) as error:
            print(f"sd-ship: the completed review was not held on disk ({error}); a hub fault before its save loses it",
                  file=sys.stderr)
            discard_held(path)
            return None
        return path

    def record_held(self, result: subprocess.CompletedProcess, head: str, passes: list[dict],
                    path: pathlib.Path | None) -> None:
        """Record the review with the hub; drop the held copy once a hub save has gone through.

        Every path through `record_review` saves the outcome before it can
        refuse, so a new revision means the hub holds it. No new revision, as
        when the hub is down or its reply was lost, keeps the file.
        """
        revision = self.revision
        try:
            self.record_review(result, head, passes)
        finally:
            if path is not None and self.revision != revision:
                discard_held(path)

    def resume_held(self, head: str) -> None:
        """sd:3340. Record the review a hub fault left on disk, as its own prepare would have.

        Only a whole record bound to this record, this head, the pass the hub
        still holds reserved and unchanged review tools resumes. Anything else
        is set aside and removed. Either way `review` then goes on as with any
        recorded pass: a reviewed head is reused, a named provider is checked
        against it and an additional request is dispatched.
        """
        path = self.held_path()
        if path is None:
            return
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            discard_held(path)  # a part file a killed write left
            return
        except (OSError, UnicodeDecodeError) as error:
            return refuse_held(path, f"unreadable: {error}")
        try:
            record = json.loads(text)
        except ValueError:
            return refuse_held(path, "not a whole record")
        if not isinstance(record, dict) or record.pop("digest", None) != digest(record):
            return refuse_held(path, "its digest does not match its content")
        passes = self.history.native(self.state)
        expected = self.held_binding(head, passes)
        bound = record.get("binding")
        held: dict[str, Any] = bound if isinstance(bound, dict) else {}
        moved = sorted(name for name in expected if held.get(name) != expected[name])
        if moved:
            return refuse_held(path, f"bound to another {', '.join(moved)}")
        if self.state.get("binding") != self.runtime.binding(self.root):
            return refuse_held(path, "the review tools or policy changed since it ran")
        try:
            outcome = record["result"]
            result = subprocess.CompletedProcess(list(outcome["args"]), int(outcome["returncode"]),
                                                 str(outcome["stdout"]), str(outcome["stderr"]))
            kept = dict(record["superseded"])
        except (KeyError, TypeError, ValueError):
            return refuse_held(path, "not a whole record")
        self.superseded = {key: kept.get(key, ABSENT) for key in DISPATCH_FIELDS}
        print(f"sd-ship: recording the review of {head[:12]} held at {path}; no provider called", file=sys.stderr)
        self.record_held(result, head, passes, path)

    def preflight_diagnostic(self, planned: subprocess.CompletedProcess,
                             kind: str = "invalid_timing_plan", stage: str = "planning") -> dict:
        argv = planned.args if isinstance(planned.args, list) else [planned.args]
        diagnostic: dict = {"kind": kind, "stage": stage, "exit_code": planned.returncode,
                            "argv": [str(part) for part in argv], "plan": plan_fields(planned.stdout)}
        limit = self.runtime.diagnostic_bytes
        for name in ("stdout", "stderr"):
            data = getattr(planned, name).encode("utf-8", "replace")
            diagnostic[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                "tail": data[-limit:].decode("utf-8", "replace"), "truncated": len(data) > limit}
        return diagnostic

    def execute_review(self, argv: list[str], head: str, passes: list[dict]) -> subprocess.CompletedProcess:
        stage = "planning"
        try:
            planned = self.runtime.process(self.root, argv + ["--explain"], timeout=self.runtime.setup_seconds)
            try:
                plan = self.runtime.timing_plan(planned)
                validate_review_readiness(planned)
                validate_provider_selection(json.loads(planned.stdout), passes[-1].get("requested_provider"))
            except Refusal:
                self.save(review_preflight_error=self.preflight_diagnostic(planned))
                raise
            # sd:1397. What the reviewers are asked, so a moved binding can replay it.
            explained = json.loads(planned.stdout)
            passes[-1]["review_request"] = {"sha256": explained.get("request_sha256"),
                                            "verify": explained.get("verification_report_digest"),
                                            "resume": explained.get("resume_report_digest")}
            # What dispatch overwrites, so a released gate failure can put it back.
            self.superseded = {key: self.state.get(key, ABSENT) for key in DISPATCH_FIELDS}
            bound: dict[str, Any] = {"binding": self.runtime.binding(self.root)}
            if self.runtime.manifest is not None:
                bound["binding_manifest"] = self.runtime.manifest(self.root)
            # The reservation's bound, so one whose process cannot be read still expires (sd:1938).
            passes[-1]["process"].update(execution_seconds=plan["execution_seconds"],
                                         deadline=reservation_deadline(plan["execution_seconds"]))
            self.save(passes=passes, phase="reviewing", head=head, **bound, review_preflight_error=None, review_clearance=None)
            stage = "execution"
            return self.runtime.process(self.root, argv + ["--expected-timing", digest(plan)], timeout=plan["execution_seconds"])
        except ReviewTimeout as error:
            diagnostic = withhold_raw(dict(error.diagnostic, stage=stage), head)
            if stage == "planning":
                self.save(review_preflight_error=diagnostic)
            else:
                passes[-1].update(execution_error=diagnostic, exit_code=124)
                self.save(passes=passes, reviewed_head=None, phase="reviewed")
            # sd:3478. A planning stage that timed out reserved no pass, so a rerun is retryable.
            said = (f"local review {stage} watchdog expired after {error.diagnostic['allowed_seconds']}s; "
                    + ("no provider pass reserved; " if stage == "planning" else f"reserved pass retained{consumed(passes)}; ")
                    + "bounded diagnostics remain in the item ship receipt")
            if stage == "planning":
                raise Refusal(said, code="review_planning_timeout", boundary="runtime", state="retryable_failure",
                              next_action="Run prepare again when the machine is less loaded.") from None
            raise Refusal(said) from None

    def release_gate_failure(self, report: dict, head: str, passes: list[dict], exit_code: int) -> None:
        """Drop the pass a gate failure reserved, and refuse with the gate's words.

        sd:2721 reverses sd:2605 for a review that cleared before its gate
        failed (`kept_gate_failure`): that pass reviewed the branch, so it is
        kept with its report and no reviewed head. The next prepare at the
        head runs the gate alone (`rerun_gate`), and a fix is verified as the
        delta from it. The operator ruled so on 2026-10-05: sd:2671 spent three
        full reviews on load-flaky gates.

        The reservation is removed rather than kept as an incomplete pass: it
        reviewed nothing, so it has no findings to carry forward, and keeping
        it charged the cap and made the next prepare demand `--retry-review`
        for a review that never started. The gate's output stays in the
        receipt under `review_preflight_error`, the field a failure before
        dispatch already uses.

        Removing the pass is not enough to undo the dispatch: `execute_review`
        stored the current tool and policy binding before the gate ran. Left
        in place, a re-review forced by a moved binding would, after its gate
        failed, read as bound to the new policy, and the next prepare would
        stop re-reviewing (found by the local review of sd:1475). Every field dispatch overwrote
        goes back to what it was.
        """
        check = report.get("check") or {}
        detail = str(check.get("detail") or "")[-self.runtime.diagnostic_bytes:]
        checks = gate_diagnostics(check, self.runtime.diagnostic_bytes)
        diagnostic = {"kind": "gate_failed", "stage": "check", "head": head,
                      "exit_code": check.get("exit_code"), "detail": detail, "checks": checks}
        kept = kept_gate_failure(report)
        if kept:
            passes[-1].update(report=report, exit_code=exit_code)
            self.save(passes=passes, reviewed_head=None, phase="reviewed", review_preflight_error=diagnostic)
        else:
            self.release_pass(passes, diagnostic)
        # Each failing check by name with its own tail; the last 500 characters
        # of one stream dropped the failing test's assertion (sd:2021).
        # The gate's summary and the failing checks lead; the gate's own
        # stderr, such as its slot wait, follows as context: a reader that
        # cuts the refusal's head read the wait line as the failure (sd:2687).
        said = detail.strip()[-FAILING_TAIL_CHARS:]
        summary = str(check.get("summary") or "").strip()
        failed = [summary] * bool(summary) + failing_check_tails(checks)
        evidence = "\n".join(failed + [f"gate output: {said}" if failed else said] * bool(said))
        # sd:2605. A pass that asked reviewers but kept no cleared status is
        # released too, and the next prepare reviews the fixed branch again.
        when = ("after the review cleared; the review pass is kept, so the next prepare at this head, or at a "
                "clean catch-up merge of the base, runs only the gate" if kept else
                "after the review cleared; the review pass was released, so the next prepare reviews again"
                if report.get("outcomes") else "before any reviewer was asked; no review pass was spent")
        raise Refusal(f"the repository gate failed {when}: "
                      f"{evidence or 'no check output; sd-ship observe prints the ship receipt'}",
                      code=gate_code(check), boundary="runtime", state="retryable_failure",
                      next_action=("Rerun prepare when the machine is less loaded: it runs only the gate at this head "
                                   "or a clean catch-up of it (--review-timeout raises the limit). Or fix the gate "
                                   "and prepare again: the fix "
                                   "is reviewed as a fix verification." if kept else
                                   "Fix the gate, or rerun prepare when the machine is less loaded "
                                   "(--review-timeout raises the limit); the next prepare reviews normally."))

    def release_pass(self, passes: list[dict], diagnostic: dict) -> None:
        """Drop the pass just reserved and put back every field its dispatch overwrote."""
        passes.pop()
        for key, value in self.superseded.items():
            if value is ABSENT:
                self.state.pop(key, None)
        self.save(passes=passes, **{key: value for key, value in self.superseded.items() if value is not ABSENT},
                  review_preflight_error=diagnostic)

    def release_unreviewed_request(self, report: dict, head: str, passes: list[dict], exit_code: int) -> None:
        """sd:2147. A post-cap pass that reviewed nothing does not spend the operator's request.

        The request buys one review. A pass in which no reviewer completed
        and none left a finding -- every reviewer refused the input, failed,
        or answered without citing the subject -- bought none, and keeping it
        made the operator renew with a history digest for a review that never
        happened. It is released the way a gate failure is, and what the
        reviewers said stays under `review_preflight_error`. A pass that kept
        a finding, or a watchdog or unreadable receipt whose evidence cannot
        say it reviewed nothing, stays spent.
        """
        self.release_pass(passes, {"kind": "additional_review_unreviewed", "stage": "execution", "head": head,
                                   "exit_code": exit_code, "status": report.get("status"),
                                   "detail": failed_outcomes(report)[-self.runtime.diagnostic_bytes:]})
        raise Refusal(f"local review {report.get('status')}: 0/{report.get('requested_reviews', 0)} completed"
                      f"{failed_outcomes(report)}; nothing was reviewed, so the operator request was not consumed",
                      code="review_unreviewed", boundary="review", state="operator_decision",
                      next_action="Resolve the reviewers' refusals, then repeat the same --additional-review-for request.")

    def release_moved_request(self, report: dict, head: str, passes: list[dict], exit_code: int) -> None:
        """sd:3379. A requested pass that reviewed another head does not spend the operator's request.

        The request buys a review of the head it names, and the pass cannot
        move to the head sd-review read without breaking that binding. It is
        released as `release_unreviewed_request` releases one that reviewed
        nothing, with the head it did review under `review_preflight_error`.
        """
        covered = str(reviewed_commit(report))
        self.release_pass(passes, {"kind": "review_head_moved", "stage": "execution", "head": head,
                                   "reviewed_head": covered, "exit_code": exit_code, "status": report.get("status")})
        raise Refusal(f"HEAD moved from {head[:12]} to {covered[:12]} while the requested review ran; it reviewed "
                      f"{covered[:12]}, not the head the request names, so the operator request was not consumed",
                      code="review_head_moved", boundary="review", state="operator_decision",
                      next_action="Keep one writer in this checkout while prepare runs, then repeat "
                                  "--additional-review-for for the head to review.")

    def record_review(self, result: subprocess.CompletedProcess, head: str, passes: list[dict]) -> None:
        try:
            report = json.loads(result.stdout)
        except ValueError:
            report = None
        if not isinstance(report, dict):
            # Keep what the reviewer process said: without its exit code and
            # output tails, a crash in sd-review and a provider failure read
            # the same (#590's first MiniMax pass).
            error = self.preflight_diagnostic(result, "invalid_receipt", "execution")
            written = write_raw(f"{head[:12]}-invalid-receipt", {"head": head, "exit_code": result.returncode,
                                                                 "stdout": result.stdout, "stderr": result.stderr})
            if written:
                error["raw_capture"] = written
            withhold_raw(error, head)
            passes[-1].update(execution_error=error, exit_code=result.returncode)
            self.save(passes=passes, reviewed_head=None, phase="reviewed")
            raise Refusal(f"local review emitted no valid receipt (sd-review exit {result.returncode}); "
                          f"its reserved pass remains recorded{consumed(passes)}; "
                          "bounded diagnostics remain in the item ship receipt")
        stash_raw_responses(report, head)
        request = passes[-1].get("additional_review_request")
        if request and reviewed_commit(report) not in (None, head):
            self.release_moved_request(report, head, passes, result.returncode)
        reserved = head
        head = self.covered_head(passes[-1], report)
        code, flake = result.returncode, {}
        if released_gate_failure(report, code):
            report, code = self.flaked(report, head, passes, code), 0
            flake = flake_record(report["check"])
        if request and unreviewed(report, code):
            self.release_unreviewed_request(report, head, passes, code)
        passes[-1]["report"] = report
        passes[-1]["exit_code"] = code
        self.save(passes=passes, reviewed_head=head if code == 0 else None, phase="reviewed", **flake)
        try:
            validate_provider_selection(report, passes[-1].get("requested_provider"), completed=completed_depth(report))
        except Refusal:
            self.save(reviewed_head=None)
            raise
        if head != reserved:
            raise Refusal(f"HEAD moved from {reserved[:12]} to {head[:12]} while the review ran; sd-review reviewed "
                          f"and gated {head[:12]}, so the receipt keeps the pass under {head[:12]}",
                          code="review_head_moved", boundary="review", state="retryable_failure",
                          next_action=f"Keep one writer in this checkout while prepare runs, then prepare again: at "
                                      f"{head[:12]} it reuses this review, and a head that cannot reach "
                                      f"{head[:12]}, as after an amend, is refused with the --restart-review remedy.")
        if code:
            # sd:2605. A review that refuses the head stops before the gate; say so,
            # so nobody reads a refusal with no gate failure in it as tests that passed.
            unrun = (report.get("check") or {}).get("status") == GATE_NOT_RUN
            message = (f"local review {report.get('status')}: {report.get('completed_reviews', 0)}/{report.get('requested_reviews', 0)} completed"
                       f"{failed_outcomes(report)}{REQUEST_CONSUMED if request else ''}"
                       f"{'; the gate did not run, so no test has passed at this head' if unrun else ''}")
            if report.get("status") == "blocking":
                raise sd_ship_dispositions.blocking_refusal(self, head, report, message)
            raise Refusal(f"{message}; see item ship receipt")
        self.check_review(head)
        if self.runtime.current_head(self.root) != head:
            raise Refusal("HEAD or checkout changed during local checks and review")


#: What a refusal adds when the pass it kept was an explicit post-cap request (sd:2147).
REQUEST_CONSUMED = "; the operator request was consumed"


def restart_reason(args) -> str:
    """sd:2600. The stated reason for `--restart-review`, once the flag stands alone."""
    reason = (getattr(args, "restart_review", None) or "").strip()
    if not reason:
        raise Refusal("--restart-review needs a nonempty REASON; the receipt records why the review restarted",
                      code="restart_review_reason", boundary="input", state="operator_decision",
                      next_action="Repeat the prepare with --restart-review and a reason.")
    if args.retry_review or args.additional_review_for is not None or getattr(args, "catch_up", False):
        raise Refusal("--restart-review does not combine with --retry-review, --additional-review-for or --catch-up; "
                      "it spends one automatic pass on a fresh full-branch review of the current head",
                      code="restart_review_combined", boundary="input", state="operator_decision",
                      next_action="Run --restart-review alone, then prepare again for anything else.")
    return reason


def consumed(passes: list[dict]) -> str:
    return REQUEST_CONSUMED if passes and passes[-1].get("additional_review_request") else ""


def unreviewed(report: dict, exit_code: int) -> bool:
    """sd:2147. A failed pass in which no reviewer completed and no finding survived.

    Read from a whole receipt only: one that names the reviewers it asked,
    each with a failed outcome. A report missing those fields cannot say it
    reviewed nothing, so its pass stays spent.
    """
    outcomes = report.get("outcomes")
    requested = report.get("requested_reviews")
    return (exit_code != 0 and report.get("status") in ("refused", "rate_limited", "unavailable")
            and type(requested) is int and requested > 0 and report.get("completed_reviews") == 0
            and report.get("reviewed_by") == [] and report.get("findings") == []
            and isinstance(outcomes, list) and bool(outcomes)
            and all(isinstance(row, dict) and row.get("status") not in ANSWERED for row in outcomes))


#: The receipt fields `execute_review` overwrites when it dispatches a pass.
DISPATCH_FIELDS = ("phase", "head", "binding", "binding_manifest", "review_clearance")
ABSENT = object()

#: The held review's record shape (sd:3340); a record of another version is set aside.
HELD_VERSION = 1


def discard_held(path: pathlib.Path) -> None:
    """Remove a held review and any part file a killed write left; a file already gone is fine."""
    for name in (path, path.with_name(path.name + ".part")):
        try:
            name.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            print(f"sd-ship: could not remove {name} ({error})", file=sys.stderr)


def refuse_held(path: pathlib.Path, reason: str) -> None:
    """Refuse a held review, say why, and remove it: the review runs as it would have (sd:3340)."""
    print(f"sd-ship: set aside the held review at {path}: {reason}", file=sys.stderr)
    discard_held(path)


def gate_diagnostics(check: dict, limit: int) -> list[dict]:
    """sd:1484. What each gate check said, bounded, for a receipt that outlives the pass.

    `check.detail` is sd-check's own stderr, which is empty when a test or a
    lint fails: that output is in each check's `stdout` and `stderr`. Keeping
    only `detail` left a receipt that said the gate failed and not why. At
    most one record per name sd-check runs, each stream cut to its last
    `limit` characters, where a failure summary ends.
    """
    records = check.get("checks")
    if not isinstance(records, list):
        return []
    # A docs-only run (sd:2072) adds one `docs` row after the three names; it is the one that ran.
    docs = [record for record in records[len(sd_lib.CHECK_NAMES):] if isinstance(record, dict)
            and record.get("name") == "docs"][:1]
    return [{"name": record.get("name"), "status": record.get("status"), "exit_code": record.get("exit_code"),
             "reason": str(record.get("reason") or "")[-limit:],
             "stdout": str(record.get("stdout") or "")[-limit:], "stderr": str(record.get("stderr") or "")[-limit:],
             # Where sd-check kept the whole output, and the failed shards it read there (sd:2558).
             "output_path": str(record["output_path"])[:limit] if record.get("output_path") else None,
             "failed_shards": [str(line)[:limit] for line in shard_lines(record.get("failed_shards"))][:FAILED_SHARDS_NAMED],
             # Every step that failed, shard or not, and the failing part of the output (sd:2608).
             "failed_steps": [str(line)[:limit] for line in shard_lines(record.get("failed_steps"))][:FAILED_SHARDS_NAMED],
             "failure": str(record.get("failure") or "")[-limit:]}
            for record in [*records[:len(sd_lib.CHECK_NAMES)], *docs] if isinstance(record, dict)]


def shard_lines(value: object) -> list:
    """`value` when it is a list, else an empty one: a receipt row is read, not trusted."""
    return value if isinstance(value, list) else []


#: How much of each stream of one failing check a refusal repeats; the receipt keeps `sd-check`'s whole tail.
FAILING_TAIL_CHARS = 1200
#: How many failed-shard lines a receipt row and a refusal carry.
FAILED_SHARDS_NAMED = 20
#: A line that says which test failed and why: unittest's `FAIL:`/`ERROR:`, a traceback, an exception (sd:3476).
FAILING_LINE = re.compile(r"^(?:FAIL|ERROR): |^Traceback \(|^\w*(?:Error|Exception)\b")
#: How many of those lines lead a failing check's entry, and how much of each.
FAILING_LINES_NAMED, FAILING_LINE_CHARS = 8, 300


def leading_lines(row: dict) -> list[str]:
    """What leads one failing check's entry: its failed shards, the whole output's path, then its failing lines.

    The failing lines are the last `FAILING_LINE`s of `sd-check`'s `failure`
    excerpt and the streams (sd:3476). A run of many suites ends with
    whichever finished last, so the tail of a failed `make check` read
    `Ran 24 tests ... OK`, and the lane's cut of the refusal held nothing
    else: 8 of 9 system gate failures needed the log opened.
    """
    found = [f"failed {line}" for line in shard_lines(row.get("failed_shards"))[:FAILED_SHARDS_NAMED]]
    found += [f"whole output: {row['output_path']}"] * bool(row.get("output_path"))
    text = "\n".join(str(row.get(key) or "") for key in ("failure", "stderr", "stdout"))
    failing = [line.rstrip()[:FAILING_LINE_CHARS] for line in text.splitlines() if FAILING_LINE.search(line)]
    return found + [f"failing: {line}" for line in list(dict.fromkeys(failing))[-FAILING_LINES_NAMED:]]


def failing_check_tails(rows: Any, limit: int = FAILING_TAIL_CHARS) -> list[str]:
    """Each failing `sd-check` row as a refusal names it: name, exit code, and the tail of its own output.

    Both streams, each cut to its last `limit` characters: a test runner
    reports on stderr and a linter on stdout, and `make` adds its own
    `Error 1` line to stderr whichever one failed. A row with no output names
    `sd-check`'s reason instead, such as a timeout (sd:2066, sd:2021).

    The failed shards and the file holding the whole output come first: a
    shard that failed early in a long run is in neither tail, and the tails
    are what a lane log cuts again (sd:2558). The lines that name the failing
    test follow (`leading_lines`, sd:3476). Each other failed step, such as
    a make target, follows the tails, and then the failing lines the tails
    do not already hold (sd:2608).
    """
    named = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("status") != "fail":
            continue
        found = leading_lines(row)
        streams = [(stream, str(row.get(stream) or "").strip()) for stream in ("stderr", "stdout")]
        said = [f"{stream}: {'...' if len(text) > limit else ''}{text[-limit:]}" for stream, text in streams if text]
        reason = str(row.get("reason") or "").strip()
        shards = set(shard_lines(row.get("failed_shards")))
        steps = [f"failed step: {step}" for step in shard_lines(row.get("failed_steps"))[:FAILED_SHARDS_NAMED]
                 if step not in shards]
        failure = str(row.get("failure") or "").strip()
        adds = any(line.strip() and line not in "\n".join(said) for line in failure.splitlines())
        steps += [f"failure: {'...' if len(failure) > limit else ''}{failure[-limit:]}"] * adds
        named.append(f"{row.get('name')} (exit {row.get('exit_code')}): "
                     + "\n".join(found + (said if said else [reason or "no output"]) + steps))
    return named


#: Statuses of a reviewer that answered usably; any other outcome names its cause.
ANSWERED = ("clean", "findings")


#: How much of a failed reviewer's share its name may take in a refusal (sd:1819).
NAME_CHARS = 40


def failed_outcomes(report: dict, limit: int = 600) -> str:
    """The failed reviewers' own details, so a refusal names why (sd:1805).

    "0/1 completed" alone sent the operator to the receipt to learn that kimi
    spent its whole max_tokens reasoning. sd-review writes each detail from
    counts and its own sentences, never model text but a voided finding's
    path and summary prefix (sd:9536); bounded all the same.
    Each failed reviewer gets an equal share of `limit` under its name, so a
    long first detail cannot push the second out of the refusal (sd:1819).
    The name is bounded apart from the cause, so a long name cannot erase it.
    """
    rows = [row for row in report.get("outcomes") or []
            if isinstance(row, dict) and row.get("status") not in ANSWERED and row.get("detail")]
    if not rows:
        return ""
    share, details = limit // len(rows), []
    for row in rows:
        backend, text = str(row.get("backend") or ""), str(row["detail"])
        # sd-review's own sentences start with the name; any other detail gets it as a label.
        cause = text[len(backend):] if text.startswith(backend) else f": {text}" if backend else text
        name = backend if len(backend) <= NAME_CHARS else f"{backend[:NAME_CHARS - 3]}..."
        room = max(share - len(name), 0)
        details.append(f"{name}{cause[:room]}{'...' if len(cause) > room else ''}")
    return f" ({'; '.join(details)})"


def adjudicated_check(root: pathlib.Path, head: str, base: str | None, database: pathlib.Path | None,
                      timeout: int, slot_timeout: int = sd_lib.GATE_SLOT_SECONDS) -> dict:
    """`sd-check` at `head` in a clean worktree, as sd-review's gate step runs it (sd:2605).

    With a `base` it runs as the local gate does and reads or leaves a receipt
    the merge gate reuses; without one exit 0 passes and no receipt is kept.
    `slot_ran_out` marks a gate whose slot wait ran out at `slot_timeout` (pack sd:7412).
    """
    import sd_gate_run  # noqa: PLC0415 -- only a prepare that clears adjudicated findings runs a gate here

    try:
        gate = sd_gate_run.check_in_worktree(root, head, timeout=timeout, base=sd_gate_run.base_ref(base),
                                             database=database, slot_timeout=slot_timeout)
    except sd_gate_run.GateError as error:
        gate = {"status": "failure", "exit_code": None, "stderr": str(error), "head": head}
    passed = gate["status"] == "success" if base else gate["exit_code"] == 0
    check = {"status": "pass" if passed and gate.get("head") == head else "fail", "exit_code": gate["exit_code"],
             "detail": str(gate.get("stderr") or "").strip()[-2000:], "checks": (gate.get("report") or {}).get("checks"),
             "summary": gate.get("summary"), **({"slot_ran_out": True} if sd_lib.slot_ran_out(gate) else {})}
    return {**check, "reused": gate["reused"]} if gate.get("reused") else check


def gate_code(check: dict) -> str:
    """The refusal code of a failed gate: `GATE_SLOT_WAIT` when its slot wait ran out and no check ran, which the
    lane puts back (pack sd:7412); else `GATE_FAILED`, which it does not."""
    return GATE_SLOT_WAIT if check.get("slot_ran_out") is True else GATE_FAILED


#: A unittest failure header, `FAIL: test_x (tests.mod.Class.test_x)`, or before 3.11 `(tests.mod.Class)` (sd:9768).
FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): (\w+) \(([\w.]+)\)", re.M)
#: Every unittest failure header, parsed or not: a name `FAILED_TEST` cannot read must not drop out unseen.
FAILURE_HEADER = re.compile(r"^(?:FAIL|ERROR): ", re.M)
#: A unittest run's failure summary: `FAILED (failures=N, errors=M, ...)`.
FAILED_SUMMARY = re.compile(r"^FAILED \(([^)]*)\)$", re.M)
#: The most failing tests a gate may name and be rerun as a flake: more is a real break.
FLAKE_TESTS = 5
#: A unittest outcome other than FAIL and ERROR that fails a gate or a rerun by itself: a skip, which `make test`
#: refuses, and an unexpected success, which fails the run.
FAILING_OUTCOME = re.compile(r"skipped=[1-9]|unexpected successes=[1-9]|^UNEXPECTED SUCCESS:", re.M)
#: A fault of the gate's runner, not a test, outside every shard block: `run-tests.sh`'s `error:` lines
#: (a log it could not assemble, results it could not publish) and `make test`'s skip refusal.
RUNNER_FAULT = re.compile(r"^(?:error: |fatal: |Tests skipped locally)", re.M)
#: A constant guard `run-tests.sh` exports to its shards: Jev off, git maintenance off.
RUNNER_GUARD = re.compile(r"^export ((?:JEV|GIT_CONFIG)_\w+)=([\w.-]*)$", re.M)
#: A shard's start and complete end lines in `run-tests.sh` output; unittest exits 1 when a test failed.
SHARD_START = re.compile(r"^shard (\S+): start$", re.M)
#: The shard count `run-tests.sh` planned; a shard `xargs` never launched leaves no log, so no start line.
SHARDS_PLANNED = re.compile(r"^test runner: workers=\d+ shards=(\d+)$", re.M)
SHARD_END = re.compile(r"^shard (\S+): \d+s exit=(\d+)$", re.M)
UNITTEST_FAILED = "1"
#: The paths, under the tree, a shard of these tests puts on PYTHONPATH (`run-tests.sh`); `()` for none. Every
#: other test runs with none here: the runner adds only its coverage hook, which the rerun does not use.
SHARD_PATHS = {"dashboard.tests.": ("dashboard", "lib", "dashboard/tests"), "tests.sd_db.": ()}
#: One shard's block of `run-tests.sh` output, from its start line to its end line.
SHARD_BLOCK = re.compile(r"^shard (\S+): start$.*?^shard \1: \d+s exit=\d+$", re.M | re.S)
#: How long a test that failed its rerun stays out of the flake retry (sd:9768).
FLAKE_DAYS = 7
#: The note a flake writes on the entry's item, and the one a failed rerun writes: one test each, then " at ".
FLAKE_NOTE, RERUN_FAILED_NOTE = "Gate flake: ", "Gate rerun failed: "


def failed_tests(check: dict) -> list[str]:
    """The unittest ids a failed gate names when failing tests are all that failed; [] otherwise (sd:9768).

    Every failing check's steps must be test shards or `make target test`, none unfinished, and every failed
    shard's block of the whole output must hold a FAIL or ERROR header: a shard that crashed with none, a
    lint, or a timeout is no flake. More than `FLAKE_TESTS` ids, or a module that did not load, is none either.
    """
    rows = [row for row in shard_lines(check.get("checks")) if isinstance(row, dict) and row.get("status") == "fail"]
    if not rows or check.get("slot_ran_out"):
        return []
    ids: list[str] = []
    for row in rows:
        found = row_tests(row)
        if not found:
            return []
        ids += found
    ids = list(dict.fromkeys(ids))
    return ids if len(ids) <= FLAKE_TESTS and not any(test.startswith("unittest.") for test in ids) else []


def exited_failing(row: dict[str, Any]) -> bool:
    """Whether `row`'s check exited on its own with a failure: a timeout or a check that could not start
    (`exit_code` None, a `reason`) never finished its work, so no test in it is known to be the only fault."""
    code = row.get("exit_code")
    return isinstance(code, int) and not isinstance(code, bool) and code != 0 and not row.get("reason")


def row_tests(row: dict) -> list[str]:
    """The unittest ids one failing check row names when failing tests are all that failed in it; [] otherwise."""
    if not exited_failing(row):
        return []
    steps = [str(step) for step in shard_lines(row.get("failed_steps"))]
    text = whole_output(row)
    failing = unittest_failed_shards(text)
    # A skip or an unexpected success anywhere fails the gate by itself; a failing shard only ends
    # `make test` before it says so.
    if RUNNER_FAULT.search(SHARD_BLOCK.sub("", text)) or FAILING_OUTCOME.search(text):
        return []
    if not steps or not failing or not all(test_step(step, failing) for step in steps):
        return []
    # Only a failing shard's headers name its failures: a passing one may print one as output (sd:9866). The
    # blocks are the ones validation counted, so an early end line a test printed hides no failure.
    blocks = "\n".join(failing.values())
    return [paren if paren.endswith(f".{name}") else f"{paren}.{name}" for name, paren in named_failures(blocks) or []]


def unittest_failed_shards(text: str) -> dict[str, str]:
    """The shards in `run-tests.sh` output `text` that finished with unittest's own failure exit and printed a FAIL
    or ERROR header, each with its block, from its start line to its end line; empty when any shard did not write
    its complete end line (killed or hung), ended with any other non-zero exit (137 for a kill), or failed with no
    header: then more than a test failed.

    Empty too unless the markers read as `run-tests.sh` writes them: one
    planned line, then each planned shard's start line and end line once,
    each pair whole before the next starts. A test's output can quote any
    of them (a multiline assertion, a captured runner log), in its own
    block or another's, and a quoted one could move a block or its exit:
    then no block is trusted, and no flake (class pass of the lane review
    of sd:9862).
    """
    planned = SHARDS_PLANNED.findall(text)
    marks = sorted([*SHARD_START.finditer(text), *SHARD_END.finditer(text)], key=lambda match: match.start())
    starts, ends = marks[0::2], marks[1::2]
    names = [match.group(1) for match in starts]
    # Every planned shard finished: one that never launched left no log to count (review of sd:9768).
    if (planned != [str(len(starts))] or len(starts) != len(ends) or len(set(names)) < len(names)
            or any(start.re is not SHARD_START or end.re is not SHARD_END or end.group(1) != start.group(1)
                   for start, end in zip(starts, ends, strict=True))):
        return {}
    failing: dict[str, str] = {}
    for start, end in zip(starts, ends, strict=True):
        if end.group(2) == "0":
            continue
        block = text[start.start():end.start()]
        if end.group(2) != UNITTEST_FAILED or not read_failures(block):
            return {}
        failing[start.group(1)] = block
    return failing


def named_failures(text: str) -> list[tuple[str, str]] | None:
    """The `(name, parent)` of each unittest failure header in `text`, or None when any header does not parse:
    a valid name with a combining mark escapes `FAILED_TEST` (review of sd:9768)."""
    named = FAILED_TEST.findall(text)
    return named if len(named) == len(FAILURE_HEADER.findall(text)) else None


def read_failures(block: str) -> bool:
    """Whether one shard's output names every failure it counts: each header parses, and they add up to its
    `FAILED (failures=N, errors=M)` summary; one it cannot read is a failure no rerun would clear."""
    named = named_failures(block)
    summaries = FAILED_SUMMARY.findall(block)
    if not named or len(summaries) != 1:
        return False
    return sum(int(count) for count in re.findall(r"(?:^|, )(?:failures|errors)=(\d+)", summaries[0])) == len(named)


def test_step(step: str, failing: dict[str, str]) -> bool:
    """`make target test`, or one complete `shard X: Ns exit=1` line of a shard in `failing`: a listed step that
    only starts like one (`exit=1` of `exit=137`) or names an unfinished shard is not a test failure."""
    if step == "make target test":
        return True
    match = SHARD_END.fullmatch(step)
    return match is not None and match.group(2) == UNITTEST_FAILED and match.group(1) in failing


def whole_output(row: dict) -> str:
    """A failing check's whole output where `sd-check` kept it (sd:2558), else "": an excerpt or a tail can leave
    out the runner error or skip summary that would show more than tests failed."""
    path = row.get("output_path")
    if isinstance(path, str) and path:
        try:
            return pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return ""


def flake_record(check: dict) -> dict:
    """The `gate_flake` state update a flake check carries, saved with the cleared review it stands for."""
    flake = check.get("flake") if isinstance(check, dict) else None
    return {"gate_flake": flake} if isinstance(flake, dict) else {}


def test_suite(test: str) -> str:
    """The module of a unittest id, as the gate's shards name a suite: the parts before the first class name."""
    parts = test.split(".")
    at = next((n for n, part in enumerate(parts) if part[:1].isupper()), len(parts) - 1)
    return ".".join(parts[:at]) or test


def runner_guards(tree: pathlib.Path) -> dict[str, str]:
    """The constant `JEV_*` and `GIT_CONFIG_*` exports of `tree`'s `run-tests.sh`, as its shards get them, read at
    the rerun's head so they never drift; `JEV_ENABLED=0` even without the file: no rerun sends a live, metered
    Jev call (sd:2136)."""
    try:
        text = (tree / ".github/scripts/run-tests.sh").read_text(encoding="utf-8")
    except (OSError, ValueError):
        text = ""
    return {"JEV_ENABLED": "0", **dict(RUNNER_GUARD.findall(text))}


def shard_environment(environment: dict[str, str], tree: pathlib.Path, test: str) -> dict[str, str]:
    """`environment` with the PYTHONPATH `run-tests.sh` gives the shard `test` runs in (`SHARD_PATHS`)."""
    env = {key: value for key, value in environment.items() if key != "PYTHONPATH"}
    paths = next((paths for prefix, paths in SHARD_PATHS.items() if test.startswith(prefix)), ())
    if paths:
        env["PYTHONPATH"] = os.pathsep.join(str(tree / path) for path in paths)
    return env


def gate_python(tree: pathlib.Path, environment: dict[str, str]) -> str | None:
    """The interpreter `make test` runs at `tree` under `environment`, or None: the Makefile's `VENV_PYTHON`,
    after its `$(GATE_ENV)` prerequisite, which in gate mode builds the worktree's own `.venv` as the gate does."""
    probe = "sd-rerun-python: $(GATE_ENV)\n\t@echo $(abspath $(VENV_PYTHON))\n"
    try:  # as a group: at the deadline no provisioning child outlives `make` into the removed worktree
        done = sd_lib.run_group(["make", "-s", "--no-print-directory", "-f", "Makefile", "-f", "-", "sd-rerun-python"],
                                cwd=tree, env=environment, input_text=probe, timeout=sd_lib.GATE_CHECK_SECONDS)
    except (OSError, subprocess.SubprocessError, sd_lib.GroupTimeout):
        return None
    lines = done.stdout.strip().splitlines()
    python = lines[-1] if done.returncode == 0 and lines else ""
    return python if python and os.access(python, os.X_OK) else None


def rerun_reason(code: int | None, out: str, err: str) -> str:
    """A failed rerun's exit code; with none, why it stopped: in words for a skip or an unexpected success, never
    unittest's own summary, which `make test`'s skip check would read in the gate's log as a skip of its own."""
    if code is not None:
        return f"exit {code}"
    if FAILING_OUTCOME.search(f"{out}\n{err}"):
        return "the rerun skipped a test or passed one unexpectedly"
    return (err.strip().splitlines() or ["no clean pass"])[-1]


def rerun_tests(root: pathlib.Path, head: str, tests: list[str]) -> tuple[int | None, str, str]:
    """The gate's interpreter `-m unittest` on `tests` alone in a clean detached worktree of `head`, as the gate ran them
    (sd:9768): no uncommitted or untracked file in `root` reaches the rerun. None for the exit code when the
    worktree could not be made or the rerun did not finish.

    ponytail: no gate slot, as a handful of tests is not a gate; take one if reruns ever grow.
    """
    import sd_gate_cache  # noqa: PLC0415
    import sd_gate_run  # noqa: PLC0415

    with tempfile.TemporaryDirectory(**sd_gate_cache.worktree_place(root, head)) as parent:
        # Resolved, as make's `abspath` names the interpreter: `/var` links to `/private/var` (sd:10530).
        tree = pathlib.Path(parent).resolve() / "tree"
        try:
            sd_gate_run.gate_git(root, "worktree", "add", "--detach", str(tree), head)
        except sd_gate_run.GateError as error:
            return None, "", f"no worktree of {head[:12]} for the rerun: {error}"
        try:
            if sd_gate_run.gate_git(tree, "rev-parse", "HEAD") != head:
                return None, "", f"the rerun's worktree is not at {head[:12]}"
            # The runner's git entries replace the gate's; fsmonitor stays off on top of them. The gate's own
            # environment: no PATH entry in the checkout `root`, as `check_in_worktree` cuts it (sd:9866).
            environment = sd_lib.without_fsmonitor({**sd_gate_run.gate_environment(root), **runner_guards(tree)})
            python = gate_python(tree, environment)
            if python is None:
                return None, "", f"no gate interpreter (the Makefile's VENV_PYTHON) at {head[:12]}"
            # The Makefile reads SD_LOCAL_GATE and unexports it from its recipes: the tests see a plain checkout.
            environment.pop(sd_gate_run.GATE_VARIABLE, None)
            # As `make test` and `run-tests.sh` give every shard: no real review or gate slot to wait on.
            environment.update(SD_REVIEW_SLOTS="0", SD_GATE_SLOTS="0", SD_AI_COMMAND_PACK_PYTHON=python)
            outputs: list[tuple[str, str]] = []
            code: int | None = 0
            for test in tests:  # one child per test, each with its own shard's path
                ran, out, err = sd_gate_run.run_child([python, "-m", "unittest", test],
                                                      shard_environment(environment, tree, test), tree,
                                                      sd_lib.GATE_CHECK_SECONDS)
                if ran is None:
                    # `out` says why it did not finish: it ends the errors. An earlier failure keeps its code.
                    outputs.append((err, out))
                    code = code or None
                    break
                outputs.append((out, err))
                code = code or ran
            return code, "\n".join(out for out, _ in outputs), "\n".join(err for _, err in outputs)
        finally:
            sd_lib.git_output(["worktree", "remove", "--force", str(tree)], root)


#: The statuses a review clears a head with; `cleared_status` is one of them when the gate failed after it.
CLEARED_STATUSES = ("clean", "advisory")


def kept_gate_failure(report: dict) -> bool:
    """sd:2721. A `gate_failed` report whose review completed and cleared, so its pass is kept."""
    return (report.get("status") == "gate_failed" and report.get("cleared_status") in CLEARED_STATUSES
            and completed_depth(report))


def released_gate_failure(report: dict, exit_code: int) -> bool:
    """sd:1475, sd:2605. The repository gate failed, so the pass is released.

    sd-review runs the gate only after a review that does not block, so a
    `gate_failed` report either asked nobody (an old binding) or holds a
    review that cleared. The first is released: the head cannot ship, and the
    next prepare reviews the fixed branch again. The second keeps its pass
    (`kept_gate_failure`, sd:2721). A report holding a blocking finding is
    not a gate failure and keeps its pass as it always has.
    """
    return (exit_code != 0 and report.get("status") == "gate_failed"
            and (report.get("check") or {}).get("status") == "fail"
            and not any(isinstance(row, dict) and row.get("disposition") == "blocking"
                        for row in report.get("findings") or []))


#: Opt-in debugging (system #590): the directory that receives raw reviewer
#: output. sd-review puts a url reviewer's raw response in its diagnostic
#: only when this is set; sd-ship moves it here so the receipt keeps a path.
RAW_CAPTURE_ENV = "SD_REVIEW_RAW_DIR"


def write_raw(name: str, record: dict) -> str:
    """Write one owner-only JSON record under `SD_REVIEW_RAW_DIR`; return its path or the error."""
    directory = os.environ.get(RAW_CAPTURE_ENV, "")
    if not directory:
        return ""
    text = json.dumps(record, indent=1)
    path = pathlib.Path(directory).expanduser() / f"{name}-{hashlib.sha256(text.encode()).hexdigest()[:12]}.json"
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            handle.write(text)
    except FileExistsError:
        pass
    except OSError as error:
        return f"not written: {error}"
    return str(path)


def stash_raw_responses(report: dict, head: str) -> None:
    """Replace each outcome's `raw_response` with the file that now holds it.

    Without the directory the field is dropped: model output never reaches
    the item's ship state."""
    for outcome in report.get("outcomes") or []:
        diagnostic = outcome.get("diagnostic") if isinstance(outcome, dict) else None
        if isinstance(diagnostic, dict) and "raw_response" in diagnostic:
            raw = diagnostic.pop("raw_response")
            written = write_raw(f"{head[:12]}-{outcome.get('backend', 'provider')}", {"head": head, **raw})
            if written:
                diagnostic["raw_capture"] = written


def withhold_raw(diagnostic: dict, head: str) -> dict:
    """Keep a reviewer's raw model text out of a diagnostic the ship state stores.

    Only with `SD_REVIEW_RAW_DIR` set does sd-review put `raw_response` in its
    output, and a truncated receipt or a watchdog kill can leave it in the
    stdout tail or the captured report. The tail is withheld (the private file
    holds the whole output) and a captured report's responses move to files.
    A watchdog kill hands over its whole output as `raw_output`, which is always
    removed here and written to its own file (sd:1588).
    """
    raw = diagnostic.pop("raw_output", None)
    if os.environ.get(RAW_CAPTURE_ENV):
        if isinstance(raw, dict):
            written = write_raw(f"{head[:12]}-watchdog", {"head": head, **raw})
            if written:
                diagnostic["raw_capture"] = written
        stdout = diagnostic.get("stdout")
        if isinstance(stdout, dict):
            diagnostic["stdout"] = {"withheld": "raw capture is on; stdout may carry model output",
                                    "bytes": stdout.get("bytes")}
        if isinstance(diagnostic.get("captured_report"), dict):
            stash_raw_responses(diagnostic["captured_report"], head)
    return diagnostic


def validate_review_readiness(planned: subprocess.CompletedProcess) -> None:
    """Only live explain output needs readiness; stored historical reports retain their schema."""
    try:
        readiness = json.loads(planned.stdout)["readiness"]
        status, blockers = readiness["status"], readiness["blockers"]
        valid = (status in ("ready", "blocked") and isinstance(blockers, list)
                 and isinstance(readiness.get("warnings"), list)
                 and readiness.get("runtime_approval") == "not_observable"
                 and all(isinstance(row, dict) and all(isinstance(row.get(key), str) and row[key]
                         for key in ("code", "boundary", "next_action")) for row in blockers)
                 and (status == "ready") == (not blockers))
    except (ValueError, KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise Refusal("local review emitted no valid readiness plan; no provider pass was reserved",
                      code="readiness_invalid", boundary="runtime", state="operator_decision",
                      next_action="Install matching review tools, then retry prepare.")
    if blockers:
        first = blockers[0]
        raise Refusal(f"local review readiness blocked: {first['code']}; no provider pass was reserved",
                      code=first["code"], boundary=first["boundary"], next_action=first["next_action"],
                      state="operator_decision", approval_required=first["code"] == "consent_missing")
