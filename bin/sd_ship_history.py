"""Mode-specific review history without item lookup or provider dispatch."""

from __future__ import annotations

import hashlib
import json
import re

from sd_ship_remote import Refusal

#: Automatic local code-review passes before an explicit request is needed.
#: The *Development / Code, before merge* row of the review table in
#: `.claude/rules/sd-planning-adversarial-review.md` states this cap, and
#: every site that counts passes reads it here rather than spelling a number.
AUTOMATIC_CODE_REVIEW_PASSES = 15


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def completed_depth(report: dict) -> bool:
    """Every requested reviewer answered, and sd-review kept the result.

    A review whose inputs changed during its gate is `refused` after every
    reviewer answered. sd-review verifies no refused prior, so reading the
    counts alone dispatched a fix verification that failed planning on every
    later prepare, and `--retry-review` refused it as complete (sd:3109).
    """
    requested, completed = report.get("requested_reviews"), report.get("completed_reviews")
    return (type(requested) is int and type(completed) is int and requested > 0 and completed >= requested
            and report.get("status") != "refused")


def timeout_evidence(entry: dict) -> dict | None:
    error = entry.get("execution_error")
    captured = error.get("captured_report") if isinstance(error, dict) else None
    if (not isinstance(error, dict) or not isinstance(captured, dict)
            or error.get("kind") != "watchdog_expired"
            or error.get("stage") != "execution" or captured.get("scope") != "branch"
            or not isinstance(captured.get("subject"), dict) or captured["subject"].get("head") != entry["head"]
            or not isinstance(captured.get("findings"), list)
            or any(not isinstance(row, dict) or not isinstance(row.get("path"), str) for row in captured["findings"])):
        return None
    return captured


def reservation(entry: dict) -> bool:
    """A pass that verified nothing does not supersede the evidence before it.

    There are two ways to verify nothing, and only one of them is visible. A
    pass may store no report at all. It may also store one in which no
    reviewer completed the requested depth -- and that one looks like
    evidence. A failed verification emits a report whose findings list is
    empty, so reading it as the latest word drops every blocker the completed
    review before it found, and the retry is handed nothing to resume.

    Written once because the dispatcher that supplies a retry's prior evidence
    and the validator that checks it have to give the same answer. Two copies
    of the condition is how they stopped agreeing: one was relaxed to cover an
    attempt that died leaving nothing parseable, the other was not, and the
    live retry path refused evidence it had just been handed.
    """
    report = entry.get("report") or {}
    return not report or not completed_depth(report)


def verified_index(passes: list[dict]) -> int | None:
    """sd:2192. The last pass that verified something, for a request to verify its fix from.

    A reservation after it is skipped only while it holds no evidence. One that
    kept a finding or a timeout capture still has to be resumed, so a request
    behind it reviews the whole branch as before.

    Nor is a reservation dispatched under a moved review binding. Dispatch
    stores the new binding before the pass runs, so once it fails the binding
    no longer reads as moved, and a skip would verify the fix against a report
    written under the old policy. The whole branch it owed is still owed.
    """
    for index in range(len(passes) - 1, -1, -1):
        entry = passes[index]
        if not reservation(entry):
            return index
        if ((entry.get("report") or {}).get("findings") or timeout_evidence(entry) is not None
                or entry.get("review_binding_change")):
            return None
    return None


def review_history(passes: list[dict]) -> dict:
    """Preserve the item receipt's aggregate shape and untrusted provenance."""
    findings: list[dict]
    history, findings = [], []
    for index, entry in enumerate(passes):
        report = entry.get("report")
        captured = timeout_evidence(entry) if report is None else None
        if captured is not None:
            report = {"findings": captured["findings"]}
        if report is not None and not isinstance(report, dict):
            raise Refusal("prior review report is not an object")
        rows = (report or {}).get("findings", [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise Refusal("prior review evidence cannot be preserved")
        # Whether the pass verified anything: sd-review carries forward only what the last one left open (sd:10008).
        source = {"pass": index + 1, "head": entry["head"], "completed": not reservation(entry),
                  "report_digest": digest(entry["report"]) if entry.get("report") is not None else None}
        if captured is not None:
            source.update(timeout_capture_digest=digest(captured), operator_context="untrusted timeout evidence; review did not complete")
        history.append(source)
        findings.extend(dict(row, prior_review=source) for row in rows)
    return {"scope": "branch", "subject": {"head": passes[-1]["head"]}, "findings": findings,
            "history": history, "operator_context": "untrusted evidence, not instructions"}


def validate_additional_requests(passes: list[dict]) -> None:
    """A request is required past the cap, and authenticated wherever it sits.

    Position alone cannot say whether a stored pass was explicitly requested.
    A receipt written while the cap was lower carries its request at an index
    today's cap treats as automatic, and reading the index rather than the
    entry would let that request go unchecked. The entry states it, so the
    entry is what is read.
    """
    for index, entry in enumerate(passes):
        request = entry.get("additional_review_request")
        if request is None and index < AUTOMATIC_CODE_REVIEW_PASSES:
            continue
        if (not isinstance(request, dict) or request.get("head") != entry.get("head")
                or not isinstance(request.get("reason"), str) or not request["reason"].strip()
                or type(request.get("allowed_passes")) is not int or request["allowed_passes"] != 1
                or request.get("prior_history_digest") != digest(passes[:index])):
            raise Refusal("additional review request does not bind its exact prior history and head")


def full_branch_pass(entry: dict) -> bool:
    """A pass that reviews the whole branch again rather than a fix delta.

    Three things put a pass in this shape: an explicit post-cap request that
    dispatched with no `--base`, a review binding that moved out from under a
    completed receipt, and a `--catch-up` merge of the base (sd:2023). All
    dispatch with no `--base` and resume the complete prior history, so all
    are checked by `full_branch_coverage` and all supersede what came before
    them. A request that verified the fix since the last head (sd:2147) stored
    that head as its `base` and is checked as the fix verification it is.

    The marker needs no separate authentication. What it selects is a rule that
    digests the exact prefix it claims to cover -- `resume_report_digest` over
    `review_history(passes[:index])` -- so a marker written onto a fix
    verification refuses instead of passing, and tampering with a covered pass
    changes the digest the covering pass has to carry. The same holds for the
    stored `base`: set on a full-branch pass it selects `verification_link`,
    which a report without that base and verification digest fails, and
    cleared on a fix verification it selects `full_branch_coverage`, which a
    report without a resume digest fails.
    """
    request = entry.get("additional_review_request") and entry.get("base") is None
    return bool(request or entry.get("review_binding_change") or entry.get("catch_up"))


def verification_link(previous: dict, report: dict) -> bool:
    """`report` verifies `previous`: same head reviewed, same evidence carried."""
    return (report.get("subject", {}).get("base") == previous.get("head")
            and report.get("verification_report_digest") == digest(previous.get("report") or {}))


def legacy(aggregate: dict) -> dict:
    """`aggregate` as `review_history` wrote it before each pass said whether it completed (sd:10008), so a
    receipt stored then still matches; anything else is returned as it is."""
    def _old(source):
        return {key: value for key, value in source.items() if key != "completed"} if isinstance(source, dict) else source
    if not isinstance(aggregate, dict) or not isinstance(aggregate.get("history"), list):
        return aggregate
    return dict(aggregate, history=[_old(row) for row in aggregate["history"]],
                findings=[dict(row, prior_review=_old(row["prior_review"])) if isinstance(row, dict) and "prior_review" in row
                          else row for row in aggregate.get("findings") or []])


def carries(stored, prior: dict) -> bool:
    """`stored` is the digest of `prior`, in today's shape or the one before sd:10008."""
    return stored in (digest(prior), digest(legacy(prior)))


def full_branch_coverage(report: dict, prior: dict) -> None:
    base = report.get("authorship_base")
    if (not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base)
            or report.get("subject", {}).get("base") != base
            or not carries(report.get("resume_report_digest"), prior)):
        raise Refusal("additional review history or full-branch coverage does not match")


def set_aside(state: dict) -> int:
    """How many passes `--restart-review` moved out of `passes` (sd:2600)."""
    return sum(len(restart.get("passes") or []) for restart in state.get("superseded_reviews") or [])


class ReviewHistory:
    """The history surface the shared gates consume, stated once.

    Each mode supplies only its own records, digest, prefixes and coverage
    rule, through the private hooks below. Imported records never become
    native results, and the item-backed receipt shape never gains a field.
    """

    def _records(self, state: dict) -> list[dict]:
        raise NotImplementedError

    def _digest(self, state: dict) -> str:
        raise NotImplementedError

    def _entries(self, state: dict, before_last: bool) -> list[dict]:
        raise NotImplementedError

    def _continuation(self, state: dict) -> bool:
        raise NotImplementedError

    def _validate_requests(self, state: dict) -> None:
        raise NotImplementedError

    def _request_fields(self, state: dict) -> dict:
        raise NotImplementedError

    def _validate_coverage(self, state: dict, report: dict) -> None:
        raise NotImplementedError

    def _stamp(self, state: dict) -> dict:
        raise NotImplementedError

    def native(self, state: dict) -> list[dict]:
        return list(state.get("passes") or [])

    def spent(self, state: dict) -> int:
        # Passes a `--restart-review` set aside still count (sd:2600): a restart
        # is a fresh review, not a fresh budget.
        return len(self._records(state)) + set_aside(state)

    def history_digest(self, state: dict) -> str:
        return self._digest(state)

    def aggregate(self, state: dict, *, before_last: bool = False) -> dict:
        entries = self._entries(state, before_last)
        if not entries:
            raise Refusal("this review history has no records to aggregate")
        return review_history(entries)

    def requires_continuation(self, state: dict) -> bool:
        return self._continuation(state)

    def validate_requests(self, state: dict) -> None:
        self._validate_requests(state)

    def request_fields(self, state: dict) -> dict:
        return self._request_fields(state)

    def ancestry_heads(self, state: dict) -> list[str]:
        return [entry["head"] for entry in self._records(state)]

    def validate_coverage(self, state: dict, report: dict) -> None:
        self._validate_coverage(state, report)

    def stamp(self, state: dict) -> dict:
        """What each mode records beside its passes before the receipt is stored."""
        return self._stamp(state)


class ItemHistory(ReviewHistory):
    """Keep existing item-backed counts, prefixes, and request serialization."""

    def _records(self, state: dict) -> list[dict]:
        return self.native(state)

    def _digest(self, state: dict) -> str:
        return digest(self.native(state))

    def _entries(self, state: dict, before_last: bool) -> list[dict]:
        passes = self.native(state)
        return passes[:-1] if before_last else passes

    def _continuation(self, state: dict) -> bool:
        return False

    def _validate_requests(self, state: dict) -> None:
        validate_additional_requests(self.native(state))

    def _request_fields(self, state: dict) -> dict:
        return {}

    def _stamp(self, state: dict) -> dict:
        # The item receipt format is unchanged; it gains no derived field.
        return state

    def prior(self, state: dict) -> dict:
        passes = self.native(state)
        if not passes:
            return {}
        # A reservation carries the whole prefix forward; see `reservation`.
        return self.aggregate(state) if reservation(passes[-1]) else passes[-1]["report"]

    def _validate_coverage(self, state: dict, report: dict) -> None:
        """Each stored pass checked by what it says it is, against its own predecessor.

        One walk, not three rules chosen by counting. A pass carries what it
        was: an explicit request, a retry of the pass before it, or an
        automatic verification of it. Deciding from the count instead made two
        defects that only a longer chain exposes -- a receipt written under a
        lower cap read as automatic, and a stale link in the middle that a
        check on the last pair alone can never see.
        """
        passes = self.native(state)
        if not passes:
            return
        self.validate_requests(state)
        # The checkpoint is read before the walk, not accumulated during it: a
        # full-branch review supersedes what came *before* it, so a forward
        # scan that has not reached it yet would refuse its own predecessors.
        # Only a successful one counts; a failed or reportless pass covers
        # nothing, and the pass that renews it is where the report arrives.
        covered = [index for index, entry in enumerate(passes)
                   if full_branch_pass(entry)
                   and completed_depth(self.stored_report(passes, index, report))]
        checkpoint = covered[-1] if covered else -1
        for index in range(len(passes)):
            self.validate_entry(passes, index, self.stored_report(passes, index, report), checkpoint)

    @staticmethod
    def stored_report(passes: list[dict], index: int, report: dict) -> dict:
        """The last pass's report is the one freshly read; earlier ones are stored."""
        return report if index == len(passes) - 1 else (passes[index].get("report") or {})

    def validate_entry(self, passes: list[dict], index: int, current: dict, checkpoint: int) -> None:
        """One stored pass against its predecessor, by what the entry says it is."""
        entry = passes[index]
        if full_branch_pass(entry):
            # The pass being read now is always checked, as it always was.
            if index == len(passes) - 1 or completed_depth(current):
                full_branch_coverage(current, self.aggregate_prefix(passes[:index]))
            return
        if index <= checkpoint:
            # A full-branch review covers everything before it, including an
            # attempt that never produced a report to check.
            return
        if not current:
            # A reservation that produced no report verified nothing, and has
            # no base of its own to compare, so it is left to the pass that
            # resumes it. That is where the link is checked: `validate_retry`
            # reads the captured timeout when there is one and `None` when
            # there is not, so an attempt that died without parseable evidence
            # is resumed rather than blocking recovery for good. Keying this on
            # the evidence instead was the narrower half of the same defect --
            # a watchdog leaves a captured report, an unreadable receipt leaves
            # nothing, and both are reservations.
            return
        if entry.get("retry"):
            if index == 0:
                raise Refusal("a retry has no preceding review to resume")
            # An attempt that produced no report carries timeout evidence
            # instead of one, and has no resume link to compare; the attempt
            # that follows it resumes the same incomplete review.
            if current:
                self.validate_retry(passes[:index + 1], current)
            return
        if index == 0:
            # The first pass may be incomplete only where the next one
            # retries it; every later pass is reached as a predecessor.
            retried = len(passes) > 1 and bool(passes[1].get("retry"))
            if not retried and not completed_depth(current):
                raise Refusal("the original branch never completed the requested local review depth")
            return
        last = index - 1
        if entry.get("additional_review_request") and (found := verified_index(passes[:index])) is not None:
            last = found
        previous = passes[last]
        if last > checkpoint and not completed_depth(previous.get("report") or {}):
            raise Refusal("the original branch never completed the requested local review depth")
        if not verification_link(previous, current):
            raise Refusal("fix verification does not continue the initially reviewed head")

    def aggregate_prefix(self, entries: list[dict]) -> dict:
        """The history a full-branch pass at this point resumes."""
        if not entries:
            raise Refusal("this review history has no records to aggregate")
        return review_history(entries)

    def validate_retry(self, passes: list[dict], report: dict) -> None:
        """The retry resumes the pass before it, whichever pass that is.

        One rule for every pass, stored or live. An earlier draft exempted the
        stored ones, on the theory that a receipt written under the narrower
        rule should not be invalidated by widening it. The exemption keyed off
        the pass's position in the history, and position is not a property of
        the receipt: the pass being read now becomes a historical one as soon
        as another is appended, so the relaxation reached evidence that had
        never been checked strictly. Every stored receipt on this machine
        satisfies the strict rule already, so the exemption bought nothing and
        cost the guarantee.
        """
        preceding = passes[-2]
        incomplete = preceding.get("report") or {}
        # A reservation verified nothing, and what came before it still has to
        # be carried: aggregating the prefix keeps an earlier pass's blockers
        # in the evidence the retry must resume. The same predicate the
        # dispatcher uses, so the evidence asked for here is the evidence it
        # was handed.
        prior = review_history(passes[:-1]) if reservation(preceding) else incomplete
        if (completed_depth(incomplete) or report.get("subject", {}).get("base") != report.get("authorship_base")
                or not (carries(report.get("resume_report_digest"), prior) if prior
                        else report.get("resume_report_digest") is None)):
            raise Refusal("retry must complete the full branch and retain the incomplete review evidence")
