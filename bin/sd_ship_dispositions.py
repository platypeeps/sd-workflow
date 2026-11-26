"""Recorded rebuttals and parked risks, separate from immutable external review reports.

A row needs a disposition and a reason a reader can check; it needs no operator
acceptance. Hashes bind the dispositions to the exact review, history, tools and
head they answer; they do not establish whether a rebuttal is substantively true.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
from datetime import datetime, timezone
from typing import Any

from sd_ship_bindings import adjudicator_binding
from sd_ship_remote import Refusal, git

MAX_PROPOSAL_BYTES = 2_000_000
#: The only values that clear a blocker; the template names them (sd:9881).
DISPOSITIONS = ("rebutted", "parked")
#: sd:10893. How long one row's `test` may run, and the most argv bytes it may carry.
TEST_TIMEOUT_SECONDS = 300  # ponytail: one focused test; a suite belongs to the gate
TEST_ARGV_BYTES = 4096


def digest(value: Any) -> str:
    try:
        data = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError):
        raise Refusal("disposition value is not valid finite JSON") from None
    return hashlib.sha256(data).hexdigest()


def read_file(path: pathlib.Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as error:
        raise Refusal(f"dispositions file cannot be read: {path}: {error.strerror}") from None
    if len(data) > limit:
        raise Refusal("dispositions file exceeds its bounded input size")
    return data


def unique_object(pairs: list[tuple[str, Any]]) -> dict:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON object key")
    return value


def key(operation: Any) -> str:
    return operation.identity.acceptance_key(operation.key)


#: `check.status` of a review that blocked before the gate ran (sd:2605).
GATE_NOT_RUN = "not_run"


def gate_waivable(check: Any) -> bool:
    """A gate that passed, or one a blocking review never ran (sd:2605).

    sd-review runs the gate only after a review that does not block, so a
    blocking report carries `not_run`. Adjudication accepts it only because the
    prepare that reads the accepted dispositions runs the gate itself before
    clearance (`SharedReview.adjudicated_gate`); a failed or partial check is
    never waived.
    """
    if not isinstance(check, dict):
        return False
    code = check.get("exit_code")
    return (type(code) is int and code == 0) or (check.get("status") == GATE_NOT_RUN and code is None)


def context(operation: Any, head: str) -> tuple[dict, list[dict]]:
    if git(operation.root, "status", "--porcelain", "--untracked-files=all") or git(operation.root, "rev-parse", "HEAD") != head:
        raise Refusal("adjudication requires the clean exact reviewed head")
    report = operation.review_inputs(head)
    if report["status"] != "blocking":
        raise Refusal("adjudication requires a complete blocking report")
    outcomes, reviewed = report.get("outcomes"), report.get("reviewed_by")
    if not isinstance(outcomes, list) or any(not isinstance(row, dict) for row in outcomes):
        raise Refusal("adjudication requires complete outcome evidence")
    completed = [row.get("backend") for row in outcomes if row.get("status") in ("clean", "findings")]
    if (any(not isinstance(name, str) or not name for name in completed) or len(set(completed)) != len(completed)
            or reviewed != completed or len(completed) != report["completed_reviews"]
            or not gate_waivable(report.get("check"))):
        raise Refusal("adjudication cannot waive incomplete transport or deterministic checks")
    rows = [{"index": index, "finding_digest": digest(row), "raw_finding": row}
            for index, row in enumerate(report["findings"], 1) if row["disposition"] == "blocking"]
    if not rows:
        raise Refusal("blocking report has no blocking findings to adjudicate")
    identity = operation.identity.bindings(operation.repository, operation.branch, head, operation.state)
    return {**identity, "report_digest": digest(report), "review_binding": operation.state["binding"],
            "adjudicator_binding": adjudicator_binding(operation.store.__file__)}, rows


#: How much of one finding's summary a refusal repeats; `text` and `sd-ship adjudicate` carry it whole (sd:2102, sd:7133).
SUMMARY_CHARS = 160
#: How many blocking findings a refusal's message names; its `findings` field carries every one.
NAMED_FINDINGS = 5


def blocking_findings(report: dict) -> list[dict]:
    """sd:2102. Each blocking finding as a refusal names it: severity, place and a bounded summary.

    `text` is the whole finding, whitespace folded, so the lane's prepare log
    gives a builder the full finding, not the cut `summary` (sd:7133).
    """
    rows = []
    for row in report.get("findings") or []:
        if isinstance(row, dict) and row.get("disposition") == "blocking":
            full = summary = " ".join(str(row.get("summary") or "").split())
            if len(summary) > SUMMARY_CHARS:
                summary = summary[:SUMMARY_CHARS - 3].rstrip() + "..."
            rows.append({"severity": row.get("severity"), "path": row.get("path"), "line": row.get("line"),
                         "summary": summary, "text": full})
    return rows


def blocking_refusal(operation: Any, head: str, report: dict, lead: str) -> Refusal:
    """sd:2102. A blocking review refuses naming its findings and the command that prints them.

    The refusal used to say only `see item ship receipt`, and nothing in the
    CLI printed that receipt: the operator read the findings out of the
    `state` table by hand. The adjudication template lists each blocking
    finding whole, so `next_action` names it rather than a new reader.
    """
    findings = blocking_findings(report)
    named = "; ".join(f"{row['severity']} {place(row)} {row['summary']}" for row in findings[:NAMED_FINDINGS])
    more = f"; and {len(findings) - NAMED_FINDINGS} more" if len(findings) > NAMED_FINDINGS else ""
    args = operation.args
    selector = f"--no-item --review-id {args.review_id}" if getattr(args, "no_item", False) else f"--item {args.item}"
    command = f"sd-ship adjudicate {selector} --expected-head {head} --json"
    return Refusal(f"{lead}: {named}{more}", code="review_blocking", boundary="review", state="operator_decision",
                   next_action=f"Run `{command}` to print each blocking finding in full; fix them and prepare "
                               "again, or rebut or park each one through that adjudication.",
                   # The head the review saw, which a catch-up may have moved past the queued one, and the digest
                   # `context` binds of each blocking finding: Reject rebuts only the review a page showed (sd:9966).
                   details={"findings": findings, "check": report.get("check"), "reviewed_head": head,
                            "finding_digests": [digest(row) for row in report.get("findings") or []
                                                if isinstance(row, dict) and row.get("disposition") == "blocking"]})


def place(row: dict) -> str:
    """`path:line`, or the path alone for a finding that names no line."""
    return str(row["path"]) if row["line"] is None else f"{row['path']}:{row['line']}"


def text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 4096:
        raise Refusal(f"disposition {label} needs nonempty bounded text")


def validate(operation: Any, head: str, proposal: Any, *, run_tests: bool = False) -> str:
    """`run_tests`: recording and prepare run each row's `test`; a read (merge, publication, `verify-review`) does not (sd:10893)."""
    bindings, expected = context(operation, head)
    if (not isinstance(proposal, dict) or set(proposal) != {"schema_version", "bindings", "findings"}
            or type(proposal["schema_version"]) is not int or proposal["schema_version"] != 1
            or digest(proposal["bindings"]) != digest(bindings)):
        raise Refusal("disposition proposal does not bind the current review, history, tools and head")
    rows = proposal["findings"]
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise Refusal("every blocking finding needs one separate disposition")
    for row, identity in zip(rows, expected, strict=True):
        if (not isinstance(row, dict) or set(row) - {"test"} != set(identity) | {"response_disposition", "reason"}
                or type(row.get("index")) is not int or digest({name: row.get(name) for name in identity}) != digest(identity)):
            raise Refusal("disposition finding indices, raw findings or digests changed")
        if row["response_disposition"] not in DISPOSITIONS:
            raise Refusal("only a rebuttal or a parked risk can clear a blocker: response_disposition "
                          f"is 'rebutted' or 'parked', not {row['response_disposition']!r}")
        text(row["reason"], "reason")
    tests = [test_argv(row.get("test")) for row in rows]
    for index, argv in enumerate(tests, 1):
        if argv and run_tests:
            reproduces(operation.root, head, index, argv)
    return digest(proposal)


def test_argv(value: Any) -> list[str]:
    """sd:10893. A row's `test`: the argv of the test the builder wrote for the finding; absent or [] is none."""
    if value is None or value == []:
        return []
    if (not isinstance(value, list) or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in value)
            or len(json.dumps(value).encode()) > TEST_ARGV_BYTES):
        raise Refusal("disposition test is a list of nonempty strings, the argv of one test, or []")
    return value


def reproduces(root: pathlib.Path, head: str, index: int, argv: list[str]) -> None:
    """sd:10893. A blocker stays open while its test exists and fails: run it at the clean `head`, no shell.

    Exit 0 means the test does not reproduce the finding, so the rebuttal stands. Any other exit, a test
    that is missing, cannot start or overruns, refuses: fail closed. So does a test that moved the head
    or left the tree dirty, since the record binds the clean exact head.
    """
    try:
        done = subprocess.run(argv, cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              errors="replace", timeout=TEST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise Refusal(f"finding {index}: its test did not finish in {TEST_TIMEOUT_SECONDS} s; "
                      "a blocker stays open until its test passes") from None
    except OSError as error:
        raise Refusal(f"finding {index}: its test could not start: {error.strerror or error}") from None
    if git(root, "status", "--porcelain", "--untracked-files=all") or git(root, "rev-parse", "HEAD") != head:
        raise Refusal(f"finding {index}: its test changed the checkout; the record binds the clean exact head")
    if done.returncode != 0:
        tail = " ".join((done.stdout + done.stderr).split())[-SUMMARY_CHARS:]
        raise Refusal(f"finding {index}: its test fails (exit {done.returncode}), so it reproduces the finding: "
                      f"fix it, or name a test that passes; {tail}")


def accepted(operation: Any, head: str, *, run_tests: bool = False) -> dict:
    revision, value = operation.store.read(operation.connection, key(operation))
    if not revision or value.get("decision") != "accepted":
        raise blocking_refusal(operation, head, operation.review_inputs(head),
                               "local review contains blocking findings without recorded dispositions")
    proposal_digest = validate(operation, head, value.get("proposal"), run_tests=run_tests)
    if value.get("proposal_digest") != proposal_digest:
        raise Refusal("recorded disposition receipt digest does not match")
    return {"kind": "adjudicated", "key": key(operation), "revision": revision, "digest": proposal_digest}


def adjudicate(operation: Any) -> dict:
    """Print the template, or record a filled one at once; a reason is the whole rebuttal."""
    args = operation.args
    bindings, rows = context(operation, args.expected_head)
    if args.dispositions_file is None:
        proposal = {"schema_version": 1, "bindings": bindings,
                    "findings": [dict(row, response_disposition="|".join(DISPOSITIONS), reason="", test=[]) for row in rows]}
        # sd:9966: the dispositions this review already holds, which a filled template replaces whole.
        revision, value = operation.store.read(operation.connection, key(operation))
        held = (revision and value.get("decision") == "accepted"
                and digest((value.get("proposal") or {}).get("bindings")) == digest(bindings))
        return operation.result("disposition_template", proposal=proposal, recorded_revision=revision if held else None)
    try:
        proposal = json.loads(read_file(args.dispositions_file, MAX_PROPOSAL_BYTES), object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        raise Refusal("disposition proposal is not bounded valid JSON") from None
    proposal_digest = validate(operation, args.expected_head, proposal, run_tests=True)
    from sd_db.database import transaction
    with transaction(operation.connection):
        source_revision, source_state = operation.store.read(operation.connection, operation.key)
        if source_revision != operation.revision or source_state != operation.state:
            raise Refusal("ship receipt changed before the dispositions were recorded")
        previous, prior = operation.store.read(operation.connection, key(operation))
        revision = operation.store.save(operation.connection, key(operation), previous, {
            "decision": "accepted", "proposal": proposal, "proposal_digest": proposal_digest,
            "source_checkpoint": source_revision, "previous_adjudication_digest": digest(prior),
            "accepted_at": datetime.now(timezone.utc).isoformat()})
    return operation.result("disposition_recorded", proposal_digest=proposal_digest, adjudication_revision=revision)
