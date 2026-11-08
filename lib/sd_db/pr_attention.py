"""Open pull requests that need the operator, as one report alert per pull and state (sd:6712).

After a GitHub shadow sync, three unwindowed searches read every open pull the
operator authored, every one with a review requested of them by name
(`user-review-requested`, so a team request is not theirs), and every other
one that @mentions them (sd:8142). Each pull's attention states:

| state             | whose pull | read from                                  |
| ----------------- | ---------- | ------------------------------------------ |
| review requested  | another's  | the `user-review-requested:@me` search     |
| mentioned         | another's  | `mentions:@me`, unless a review is asked   |
| waiting on review | yours      | not draft, no review 24 h after it opened  |
| changes requested | yours      | `reviewDecision` CHANGES_REQUESTED         |
| checks failing    | yours      | red rollup unless every red is optional    |
| merge conflict    | yours      | `mergeable` CONFLICTING                    |
| ready to merge    | yours      | approved, mergeable, checks green or none  |

Only a required check blocks the merge, so a red optional one (a Snyk scan)
is no alert (sd:7862). The search cannot say which checks are required:
`isRequired` takes the pull's number, and `mergeStateStatus` reads BLOCKED
for a missing review too. So a pull whose whole rollup is red costs one
batched `CHECKS` query, all such pulls in one call, which reads its head's
required checks; a pull with no red rollup costs none.

A red rollup is never ready to merge: `isRequired` covers only the checks
that reported, so green ones cannot prove a required check is not missing.
When the answer cannot say for one pull (more than 100 checks, a malformed
or missing entry, a failed call), that pull keeps the whole rollup's rule,
as before sd:7862, and the read stays complete: one pull's unread checks
must not stop every alert from clearing.

A state new since the last read files one `reporting.ingest` report, job
`JOB`, basis `<state> <url>`: the reports path the operator already triages,
no new store. The states read are kept under `SEEN_KEY`, so a state that
persists files once, even after the operator acknowledged its alert. A state
that cleared acknowledges its open alert. A read that failed or was cut files
what it read and clears nothing: absence from it is unknown.

| step            | state moved            | failure            | recovery                         |
| --------------- | ---------------------- | ------------------ | -------------------------------- |
| budget check    | none                   | the sync spent it  | named; read and clear nothing    |
| search          | none                   | gh fails or cut    | file what was read, clear none   |
| search shape    | none                   | malformed answer   | cut read: file valid, clear none |
| required checks | none                   | gh fails, partial  | that pull: whole rollup; complete|
| file one alert  | one report row         | ingest refuses     | left out of seen: next run files |
| clear one alert | one report to done     | acknowledge fails  | named; next run clears again     |
| record seen     | the `SEEN_KEY` row     | write fails        | next run files again; ingest folds |

The whole step is a side observation of the sync: `shadow_sync` catches its
failure and names it; the watermark never waits on it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import reporting, workflow
from .database import transaction
from .writes import record_state, resolve_note

JOB = "pr-attention"
SEEN_KEY = "pr-attention:seen"
#: One page per search: more open pulls than this is a cut read.
PAGE = 100
QUERY = """query($q: String!, $n: Int!) { search(query: $q, type: ISSUE, first: $n) { issueCount nodes {
  ... on PullRequest { url number title repository { nameWithOwner } reviewDecision mergeable isDraft createdAt
    reviews(first: 1) { totalCount }
    commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } } } } }"""
#: One red rollup's head contexts and which are required, per pull: aliased `p<i>` in one query.
CHECKS = """p{i}: repository(owner: {owner}, name: {name}) {{ pullRequest(number: {n}) {{ commits(last: 1) {{ nodes {{
  commit {{ statusCheckRollup {{ contexts(first: 100) {{ pageInfo {{ hasNextPage }} nodes {{
    ... on CheckRun {{ conclusion isRequired(pullRequestNumber: {n}) }}
    ... on StatusContext {{ state isRequired(pullRequestNumber: {n}) }} }} }} }} }} }} }} }} }}"""
#: A check run's conclusion or a status's state that fails the check.
RED = frozenset({"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"})
#: `requested` reads before `mentioned`, so a pull that asks the operator's review alerts once, as that (sd:8142).
SEARCHES = (("author", "is:pr is:open archived:false author:@me"),
            ("requested", "is:pr is:open archived:false user-review-requested:@me"),
            ("mentioned", "is:pr is:open archived:false mentions:@me -author:@me"))
LABELS = {"review_requested": "review requested of you", "changes_requested": "changes requested",
          "checks_failing": "checks failing", "conflict": "merge conflict", "ready": "approved, ready to merge",
          "mentioned": "you are mentioned", "waiting_on_reviewers": "waiting on reviewers, no review in 24 h"}
#: How long the operator's open pull may go without a review before it is an alert (sd:8142).
REVIEW_WAIT = timedelta(hours=24)


def _rollup(pull: dict) -> str | None:
    nodes = ((pull.get("commits") or {}).get("nodes") or [{}])
    return (((nodes[-1] or {}).get("commit") or {}).get("statusCheckRollup") or {}).get("state")


def states(pull: dict, why: str, optional_red: bool = False, now: datetime | None = None) -> list[str]:
    """The attention states of one search node, from `LABELS`.

    `optional_red`, from `_optional_red`: every red check on the head is optional, so the red rollup blocks nothing.
    `now` dates the review wait; without it no pull waits on reviewers.
    """
    if why == "requested":
        return ["review_requested"]
    if why == "mentioned":
        return ["mentioned"]
    rollup = _rollup(pull)
    decision, mergeable = pull.get("reviewDecision"), pull.get("mergeable")
    found = []
    if decision == "CHANGES_REQUESTED":
        found.append("changes_requested")
    if rollup in ("FAILURE", "ERROR") and not optional_red:
        found.append("checks_failing")
    if mergeable == "CONFLICTING":
        found.append("conflict")
    if decision == "APPROVED" and mergeable == "MERGEABLE" and rollup in ("SUCCESS", None):
        found.append("ready")
    if now is not None and pull.get("isDraft") is False and (pull.get("reviews") or {}).get("totalCount") == 0 \
            and now - datetime.fromisoformat(pull["createdAt"].replace("Z", "+00:00")) >= REVIEW_WAIT:
        found.append("waiting_on_reviewers")
    return found


def _has(block, key, kind, *, null: bool = False) -> bool:
    """Whether `block` holds `key` as a `kind`; absent is never null, and null passes only where GitHub allows it."""
    return isinstance(block, dict) and key in block and (
        isinstance(block[key], kind) and type(block[key]) is not bool or null and block[key] is None)


def _well_formed(node) -> bool:
    """Whether a search node holds every field `QUERY` asks for, null only where GitHub's schema allows.

    A node short of a field is no evidence its state cleared: `read` names it and the read stays incomplete.
    The head commit must be there; its `statusCheckRollup` may be null, meaning no checks.
    """
    if not (_has(node, "url", str) and node["url"] and _has(node, "number", int) and _has(node, "title", str)
            and _has(node, "reviewDecision", str, null=True) and _has(node, "mergeable", str)
            and _has(node, "repository", dict) and _has(node["repository"], "nameWithOwner", str)
            and isinstance(node.get("isDraft"), bool) and _has(node, "createdAt", str) and _moment(node["createdAt"])
            and _has(node, "reviews", dict) and _has(node["reviews"], "totalCount", int)
            and _has(node, "commits", dict) and _has(node["commits"], "nodes", list) and node["commits"]["nodes"]):
        return False
    tip = node["commits"]["nodes"][-1]
    if not (_has(tip, "commit", dict) and _has(tip["commit"], "statusCheckRollup", dict, null=True)):
        return False
    rollup = tip["commit"]["statusCheckRollup"]
    return rollup is None or _has(rollup, "state", str)


def _moment(value: str) -> bool:
    """Whether `value` is an ISO instant with a zone, as GitHub's `createdAt` is."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _verdict(answer) -> bool | None:
    """Whether a required check on one pull's head is red; None when the answer does not say."""
    try:
        block = answer["pullRequest"]["commits"]["nodes"][-1]["commit"]["statusCheckRollup"]["contexts"]
        if block["pageInfo"]["hasNextPage"] is not False or not isinstance(block["nodes"], list):
            return None
        red = False
        for node in block["nodes"]:
            key = "conclusion" if "conclusion" in node else "state"
            if type(node.get("isRequired")) is not bool or key not in node or not (
                    node[key] is None or isinstance(node[key], str)):
                return None
            red = red or node["isRequired"] and node[key] in RED
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return red


def _optional_red(pulls: list[dict], runner, timeout: float) -> set[str]:
    """The URLs of pulls, all with a red rollup, whose head has no red required check: one call for all.

    A pull the answer does not settle is left out, so it keeps the whole rollup's rule. gh exits non-zero
    when one alias errors and still prints the others' data, so that data is read whatever the exit code.
    """
    from .shadow_sync import _run

    if not pulls:
        return set()
    parts = []
    for i, pull in enumerate(pulls):
        owner, _, name = pull["repository"]["nameWithOwner"].partition("/")
        parts.append(CHECKS.format(i=i, owner=json.dumps(owner), name=json.dumps(name), n=int(pull["number"])))
    _code, out, _err = _run(["gh", "api", "graphql", "-f", "query=query { " + " ".join(parts) + " }"],
                            runner, timeout=timeout)
    try:
        data = json.loads(out)["data"]
    except (ValueError, KeyError, TypeError):
        return set()
    if not isinstance(data, dict):
        return set()
    return {pull["url"] for i, pull in enumerate(pulls) if _verdict(data.get(f"p{i}")) is False}


def read(runner=None, *, timeout: float = 60, now: datetime | None = None) -> tuple[dict[str, dict], bool, str]:
    """`({basis: pull}, complete, reason)`: every open pull's states, keyed `<state> <url>`. `now` dates review waits."""
    from .shadow_sync import _run

    now = now or datetime.now(timezone.utc)
    found: dict[str, dict] = {}
    problems = []
    for why, query in SEARCHES:
        code, out, err = _run(["gh", "api", "graphql", "-f", f"query={QUERY}", "-f", f"q={query}", "-F", f"n={PAGE}"],
                              runner, timeout=timeout)
        try:
            block = json.loads(out)["data"]["search"] if code == 0 else None
        except (ValueError, KeyError, TypeError):
            block = None
        if not isinstance(block, dict):
            problems.append(f"{why}: {(err or out).strip().splitlines()[0] if (err or out).strip() else f'gh exited {code}'}")
            continue
        count, listed = block.get("issueCount"), block.get("nodes")
        if type(count) is not int or not isinstance(listed, list):
            problems.append(f"{why}: malformed search, no issueCount or nodes list")
            continue
        nodes = [node for node in listed if _well_formed(node)]
        if len(nodes) < len(listed):
            problems.append(f"{why}: {len(listed) - len(nodes)} malformed pull(s)")
        elif count > len(nodes):
            problems.append(f"{why}: more than {PAGE} open pulls")
        optional = _optional_red([node for node in nodes if _rollup(node) in ("FAILURE", "ERROR")],
                                 runner, timeout) if why == "author" else set()
        for node in nodes:
            if why == "mentioned" and f"review_requested {node['url']}" in found:
                continue  # one alert per pull: the review request says it
            for state in states(node, why, node["url"] in optional, now):
                found[f"{state} {node['url']}"] = {"state": state, "url": node["url"], "number": node.get("number"),
                                                    "title": node.get("title") or "",
                                                    "repo": (node.get("repository") or {}).get("nameWithOwner") or ""}
    return found, not problems, "; ".join(problems)


def _open_alerts(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute("SELECT id, fields FROM item WHERE kind = 'report' AND source = 'cron-report' "
                              "AND status != 'done' AND external_id > ? AND external_id < ?", (JOB + ":", JOB + ";"))
    return {json.loads(row["fields"])["report"]["attention_basis"]: row["id"] for row in rows}


def _seen(connection: sqlite3.Connection) -> set[str]:
    row = connection.execute("SELECT body FROM state WHERE kind = 'heartbeat' AND key = ? ORDER BY id DESC LIMIT 1",
                             (SEEN_KEY,)).fetchone()
    return set(json.loads(row["body"]).get("seen") or []) if row else set()


def _clear(connection: sqlite3.Connection, item: int) -> bool:
    """Close the alert's own followup; acknowledge the alert only when no one else's followup stays open.

    A followup a person or a scheduled agent added is work they still owe: a pull whose state cleared does
    not finish it. The alert's own is the one `reporting` rules ingest's: session `INGEST_SESSION` and the text
    derived from the report's fields, since a scheduled agent writes under that session too.
    """
    with transaction(connection):
        state = workflow.item_state(connection, item)
        ingests = reporting._ingest_followup_for(state["item"]["fields"])
        followups = [note for note in state["notes"] if note["kind"] == "followup" and not note["resolved_at"]]
        own = [note for note in followups if note["session"] == reporting.INGEST_SESSION and note["body"] == ingests]
        for note in own:
            resolve_note(connection, note["id"])
        if len(own) < len(followups):
            return False
        revision = workflow.item_state(connection, item)["revision"]
        reporting.acknowledge(connection, item, expected_revision=revision, who="shadow sync",
                              resolve_followups=False)
    return True


def alert(connection: sqlite3.Connection, found: dict[str, dict], complete: bool, *, now: datetime) -> dict:
    """File a report per state new since the last read; acknowledge each open one that cleared (only on a complete read)."""
    when = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    seen, filed, cleared, problems = _seen(connection), [], [], []
    for basis, pull in sorted(found.items()):
        if basis in seen:
            continue
        label = LABELS[pull["state"]]
        try:
            reporting.ingest(connection, job=JOB, run_id=f"{when}:{basis}"[:160], started=when, ended=when, exit_code=0,
                             text=f"{pull['repo']}#{pull['number']} {pull['title']}: {label}\n{pull['url']}\n",
                             source_path="sd shadow sync", attention=True, attention_basis=basis,
                             grade=f"{pull['repo']}#{pull['number']}: {label}")
            filed.append(basis)
        except (workflow.WorkflowError, sqlite3.Error) as error:
            problems.append(f"{basis}: {error}")
    if complete:
        for basis, item in _open_alerts(connection).items():
            if basis in found:
                continue
            try:
                if _clear(connection, item):
                    cleared.append(basis)
            except (workflow.WorkflowError, sqlite3.Error) as error:
                problems.append(f"{basis}: {error}")
    # A cut read keeps what it did not see: a pull it missed is not a pull whose state cleared.
    kept = (seen if not complete else set()) | {basis for basis in found if basis in seen or basis in filed}
    record_state(connection, "heartbeat", key=SEEN_KEY, timestamp=when, body={"seen": sorted(kept)})
    return {"filed": filed, "cleared": cleared, "problems": problems}
