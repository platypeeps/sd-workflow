"""The tracker collector, in the library, writing `shadow` and its watermark.

When this module was written, the pack's `dashboard` package held the only
collector that refreshed the tracker rows. A one-time import of
`index.sqlite` would therefore have left
`shadow` frozen on the day of the switch: correct at midnight, wrong by
lunch, and nothing in the system would have said so. So the collector moved
here first, and PR 6's retire of `index.sqlite` is gated on a sync *after* the
import having brought in a new issue.

**This module is the collector, not the command.** `sd shadow sync` is a verb
and verbs live in the pack -- that is the settled answer, and an earlier
draft of this item built the verb here, which was the one place this item
decided where a verb lives and decided it against itself. What lands here is
the function the verb calls and the `watermark` rows it resumes from.

**Windowed, with an hour of overlap, and the watermark moves only on
success.** Both rules come from the collector this replaces and both exist
because a windowed collector that gets them wrong loses issues silently.
GitHub accepts second-precision `updated:` bounds over a mutable search
index, so an issue updated at the instant of a collect can be
missing from it and, without overlap, from every later one too. And a
partial collect that advanced its watermark would step the window past
exactly the issues it failed to read.

**Two trackers, one per call.** The 2026-09-06 decision that Jira would
retire with no successor was reversed by the 2026-09-12 decision on sd:361:
Jira tickets are shadowed the way GitHub issues are. The second module is
`shadow_jira`, which collects Jira tickets into the same `Collected` shape
over its own JQL window. `sync`'s `tracker` chooses the collector: `github`
runs the four buckets here, the intake sweep of `policy.issues`
repositories, their contribution details and the protection sweep; `jira`
runs `shadow_jira` with none of that, then `jira_moves`, and any other name is a `ValueError`. `sync` stays one tracker per call; the caller -- the pack's
`sd shadow sync` -- iterates `TRACKERS` and reports each tracker on its own.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import refuse_hub_only, transaction
from .errors import SdDbError
from .writes import (
    close_shadow,
    open_shadow_issues,
    record_state,
    resolve_state,
    restate_reasons,
    see_shadow,
    settle_shadow,
    stale_shadow_rows,
    upsert_shadow,
)

TRACKER = "github"
#: Every tracker `sync` can collect, in the order the caller reports them.
#: Enumerated rather than discovered: a module that exists is not a tracker
#: until it is named here and `sync` dispatches to it.
TRACKERS = (TRACKER, "jira")
GH_TIMEOUT_SECONDS = 60
PAGE_SIZE = 100

#: Ten pages per search range. Larger result sets need narrower ranges.
MAX_PAGES = 10
SEARCH_CAP = 1000
#: One collect's whole budget: the search walk, the contribution details that
#: follow it, and the branch-protection sweep all spend from it. 200/300 was
#: chosen when only the search spent it; once #301 ran details on the same
#: budget, a nightly run had ~90 requests left after the search (45-77 a
#: night) and the protection reserve (then 2 per registered repository), which at
#: 8 per pull cleared ~11 details against 40-90 new rows a day -- the queue
#: reached 213 on 2026-09-12 and could only grow. 1000 clears ~110 a night
#: and is a fifth of GitHub's hourly REST allowance; at the ~2.8 requests/s
#: the nightly measured, spending it needs ~360 s, hence the 600.
MAX_REQUESTS = 1000
MAX_SECONDS = 600

#: An hour, deliberately re-read. Cheaper than the alternative: the upsert
#: makes a repeat sighting free, and a missed issue is never seen again.
OVERLAP = timedelta(hours=1)

#: A first collect has no watermark, and an unbounded query would drag years
#: of closed work in to render a "needs you" list.
FIRST_RUN_WINDOW = timedelta(days=90)

#: `(why, search qualifier)`. `involves:@me` would collapse these into one
#: call and lose *why* an issue reached you, which is the whole value:
#: "three people requested your review" and "you opened three issues" are the
#: same count and completely different mornings. `user-review-requested`
#: lists a request made to the operator by name; `review-requested` also
#: matches one made to a team they belong to (sd:8358).
BUCKETS = (
    ("assigned", "assignee:@me"),
    ("mentioned", "mentions:@me"),
    ("review-requested", "user-review-requested:@me"),
    ("author", "author:@me"),
)

#: The `why` of a row the intake sweep found (sd:4880): an open issue of a
#: repository whose `policy.issues` is read or ask, whoever opened it.
INTAKE = "intake"
#: Repositories per intake sweep: GitHub refuses a long search query.
INTAKE_GROUP = 5
#: `(why, qualifier)`: the unwindowed searches an intake repository whose `issues_scope` is `mine` gets (pack
#: sd:7672). Its intake list decides membership from `reason`, and a windowed bucket never sees an issue last
#: updated before its window, so these list every open issue there assigned to the operator or mentioning them.
MINE_SWEEPS = (("assigned", "assignee:@me"), ("mentioned", "mentions:@me"))
#: How long an open row goes unseen before `sync` reads it directly: an
#: issue outside intake (pack sd:5415) or a pull request (sd:6091). A bucket
#: sees a closure in its window; a week unseen means no bucket will, and an
#: open answer starts the week again.
STALE_AFTER = timedelta(days=7)
#: The stale rows one run reads at most (sd:6091, the operator's ruling of
#: 2026-10-25: about 20 a run). 85 stale rows clear in five runs.
REVISIT_LIMIT = 20

#: `shadow.reason` (migration 34), strongest first, with the `why` words that
#: give each: GitHub's buckets and Jira's `normalize`. Any other word is
#: `watched`: a mention, a review request, a Jira watch, a custom JQL's match.
REASONS = (("assigned", ("assigned",)), ("filed", ("author", "filed")))


def reason_of(why: list[str], unknown=()) -> str | None:
    """The strongest reason `why` gives a row, or None when it cannot be said.

    `unknown` names the buckets whose search did not complete. A row missing
    from one of them may still belong to it, so it gets no reason weaker than
    that bucket's: None, which `upsert_shadow` reads as "keep the stored one".
    """
    for value, words in REASONS:
        if any(word in why for word in words):
            return value
        if any(word in unknown for word in words):
            return None
    return "watched" if why else None


QUERY = """
query($q: String!, $n: Int!, $after: String) {
  search(query: $q, type: ISSUE, first: $n, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      __typename
      ... on Issue {
        number title url state createdAt updatedAt closedAt
        author { login }
        assignees(first: 10) { nodes { login } }
        repository { nameWithOwner }
      }
      ... on PullRequest {
        number title url state createdAt updatedAt closedAt mergedAt headRefName
        reviewDecision mergeable commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
        closingIssuesReferences(first: 10) { nodes { url } }
        author { login }
        assignees(first: 10) { nodes { login } }
        repository { nameWithOwner }
      }
    }
  }
}
"""

NO_GH = "gh is not installed; install it (`brew install gh`) to collect issues"
NO_AUTH = "gh is installed but not authenticated; run `gh auth login`"

#: The `state` kind the cursor lives in. Named in the schema's own CHECK, so
#: a typo here is a refusal from `record_state` and never a silent row.
WATERMARK = "watermark"


def iso(moment: datetime) -> str:
    """UTC, second resolution, the spelling GitHub search accepts."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(text or "")
        return value.astimezone(timezone.utc) if value.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def settled_at(value) -> str | None:
    """A tracker's merge or close time as `iso` writes it, or None when it does not read (sd:5303).

    None is unknown, and `upsert_shadow` keeps the stored time for it.
    """
    moment = parse_iso(value) if isinstance(value, str) else None
    return iso(moment) if moment is not None else None


def closing_issues(node: dict) -> list[str] | None:
    """The urls of the issues a pull request node closes, or None when the field does not read (sd:5315).

    None is unknown, and `upsert_shadow` keeps the stored list for it.
    """
    block = node.get("closingIssuesReferences")
    found = block.get("nodes") if isinstance(block, dict) else None
    if not isinstance(found, list):
        return None
    urls: list[str] = []
    for entry in found:
        url = entry.get("url") if isinstance(entry, dict) else None
        if not isinstance(url, str) or not url.strip():
            return None
        urls.append(url)
    return list(dict.fromkeys(urls))


def assignees(node: dict) -> str | None:
    """A node's assignee logins joined by `, `, `""` for none, or None when the field does not read (pack sd:5496).

    ponytail: the query reads the first ten; an issue with more lists ten.
    """
    block = node.get("assignees")
    found = block.get("nodes") if isinstance(block, dict) else None
    if not isinstance(found, list):
        return None
    logins = [entry.get("login") for entry in found if isinstance(entry, dict)]
    return ", ".join(login for login in logins if isinstance(login, str) and login)


#: A Jira issue key: a project key, a dash and a number, not inside a longer word or number (sd:5315).
JIRA_KEY = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]+-[0-9]+(?![0-9])")


def jira_keys(*texts) -> list[str]:
    """Each Jira key the texts name, in order, once. A text that is not a string names none."""
    return list(dict.fromkeys(key for text in texts if isinstance(text, str) for key in JIRA_KEY.findall(text)))


def jira_rows(connection: sqlite3.Connection, keys) -> dict[str, dict]:
    """The `tracker = 'jira'` shadow rows of `keys`, by key: `{key, url, title, state}`. One statement.

    The key is the last path segment of the row's url (`.../browse/KEY`), as
    `shadow_jira.normalize` writes it; a key no row holds is absent. It reads
    every Jira row and filters here: one OR'd clause per key hit SQLite's
    expression depth (1000) at lifetime history.
    """
    wanted = set(keys)
    if not wanted:
        return {}
    rows = connection.execute(
        "SELECT url, title, state FROM shadow WHERE tracker = 'jira' ORDER BY last_seen, id").fetchall()
    found = {}
    for row in rows:
        key = row["url"].rsplit("/browse/", 1)[-1]
        if key in wanted:
            found[key] = {"key": key, "url": row["url"], "title": row["title"], "state": row["state"]}
    return found


def jira_links(connection: sqlite3.Connection, *texts) -> list[dict]:
    """The Jira issues a pull request names in its title or branch, as shadow rows (sd:5315).

    Keys are found by `JIRA_KEY` and joined to the `tracker = 'jira'` rows;
    no Jira call is made. A key no row holds, such as `UTF-8`, links nothing.
    """
    keys = jira_keys(*texts)
    found = jira_rows(connection, keys)
    return [found[key] for key in keys if key in found]


def window_start(watermark: str | None, now: datetime) -> datetime:
    """Where this collect starts reading.

    A watermark that cannot be parsed is treated as no watermark at all: a
    corrupt value must widen the window, never narrow it. Narrowing on
    garbage is how a windowed collector silently stops collecting.
    """
    previous = parse_iso(watermark)
    if previous is None:
        return now - FIRST_RUN_WINDOW
    return previous - OVERLAP


# --------------------------------------------------------------- the cursor


def read_watermark(connection: sqlite3.Connection, tracker: str = TRACKER) -> str | None:
    """The newest cursor written for this tracker, or None on a first run.

    Only a **resolved** row counts. `resolved_at` on a watermark means the
    cursor was written completely; a row without it is a write that did not
    finish, and resuming from half a cursor is the one way this table could
    make the collector skip a window.
    """
    row = connection.execute(
        "SELECT body FROM state WHERE kind = ? AND key = ? AND resolved_at IS NOT NULL "
        "ORDER BY timestamp DESC, id DESC LIMIT 1",
        (WATERMARK, tracker),
    ).fetchone()
    if row is None:
        return None
    try:
        return (json.loads(row["body"]) or {}).get("collected_at")
    except (TypeError, ValueError):
        return None


def write_watermark(
    connection: sqlite3.Connection,
    tracker: str,
    collected_at: str,
    start: str,
) -> int:
    """Record a *successful* collect, and mark it complete.

    Called only after a collect that returned without error. A failed or
    partial collect deliberately leaves the cursor where it was, so the next
    run re-queries the window that was missed instead of stepping over it.
    """
    row = record_state(
        connection,
        WATERMARK,
        key=tracker,
        body={"collected_at": collected_at, "window_start": start},
    )
    resolve_state(connection, row)
    return row


# ----------------------------------------------------------------- the fetch


def _run(argv: list[str], runner=None, *, timeout=GH_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Fixed-argv subprocess, or the injected runner the tests supply."""
    if runner is not None:
        return runner(argv)
    try:
        done = subprocess.run(  # nosec B603 - fixed argv, shell=False
            argv, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out after {timeout:g}s"
    except OSError as error:
        return 127, "", f"cannot run {argv[0]}: {error}"
    return done.returncode, done.stdout, done.stderr


def available(runner=None, *, timeout=GH_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Whether this run can read GitHub, and if not, why not.

    Reports the *name* of what is missing and never the credential itself;
    `gh auth status` is consulted for its exit code alone.
    """
    if runner is None and shutil.which("gh") is None:
        return False, NO_GH
    code, _, _ = _run(["gh", "auth", "status"], runner, timeout=timeout)
    if code != 0:
        return False, NO_AUTH
    return True, ""


def _page(query: str, cursor: str | None, runner=None, *, timeout=GH_TIMEOUT_SECONDS) -> tuple[dict, str]:
    """One page of one search. Returns `(search block, error sentence)`."""
    argv = ["gh", "api", "graphql", "-f", f"query={QUERY}", "-f", f"q={query}",
            "-F", f"n={PAGE_SIZE}"]
    # Omitted rather than passed empty on the first page: `-F after=` sends an
    # empty string, and GraphQL reads that as a cursor rather than as absent.
    if cursor:
        argv += ["-f", f"after={cursor}"]
    code, out, err = _run(argv, runner, timeout=timeout)
    if code != 0:
        detail = (err or out).strip().splitlines()
        return {}, detail[0] if detail else f"gh api graphql exited {code}"
    try:
        payload = json.loads(out or "null") or {}
    except json.JSONDecodeError as error:
        return {}, f"gh api graphql did not return JSON: {error}"
    if not isinstance(payload, dict):
        return {}, "gh api graphql did not return an object"
    errors = payload.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else {}
        message = first.get("message") if isinstance(first, dict) else ""
        return {}, f"gh api graphql returned errors: {message or errors}"
    data = payload.get("data")
    block = data.get("search") if isinstance(data, dict) else None
    # An absent `search` is malformed; an empty one is a legitimate result
    # with no matches. Collapsing both would turn the first into a silent
    # empty page, which is a silent drop in a different coat.
    if not isinstance(block, dict):
        return {}, "gh api graphql returned no search block"
    return block, ""


@dataclass
class _SearchBudget:
    """One collect's bounded GraphQL walk, shared by every bucket and split."""

    remaining: int
    deadline: float
    requests: int = 0

    def page(self, query, cursor, runner):
        left = self.deadline - time.monotonic()
        if self.remaining <= 0:
            return {}, "collection request limit exhausted"
        if left <= 0:
            return {}, "collection time limit exhausted"
        self.remaining -= 1
        self.requests += 1
        return _page(query, cursor, runner, timeout=min(GH_TIMEOUT_SECONDS, left))


def search(query: str, runner=None, *, budget=None, observed=None) -> tuple[list[dict], bool, str]:
    """One search, followed across pages. Returns `(nodes, truncated, error)`.

    `truncated` means rows exist that this walk did not return. Two things
    cut a walk short and only one is this module's: the page ceiling is, and
    GitHub's 1,000-result search cap is not. The cap arrives disguised as
    `hasNextPage: false`, which is why the finish is checked against
    `issueCount` rather than believed.
    """
    nodes: dict[str, dict] = {}
    cursor: str | None = None
    cursors: set[str] = set()
    count = None
    for _ in range(MAX_PAGES):
        block, error = (budget.page(query, cursor, runner) if budget else
                        _page(query, cursor, runner))
        if error:
            return list(nodes.values()), False, error
        total = block.get("issueCount")
        page = block.get("nodes")
        info = block.get("pageInfo")
        if (type(total) is not int or total < 0 or not isinstance(page, list)
                or not isinstance(info, dict) or type(info.get("hasNextPage")) is not bool):
            return list(nodes.values()), False, "malformed search pagination"
        if count is not None and total != count:
            return list(nodes.values()), False, "search count changed during pagination"
        count = total
        if observed is not None:
            observed["count"] = count
        for node in page:
            if not isinstance(node, dict) or not isinstance(node.get("url"), str) or not node["url"].strip():
                return list(nodes.values()), False, "search page has an unidentifiable result"
            nodes[node["url"]] = node
        if len(nodes) > count:
            return list(nodes.values()), False, "search returned more identities than its count"
        # Do not fetch ten pages that cannot cover the advertised result set.
        if count > SEARCH_CAP:
            return list(nodes.values()), True, ""
        if not info["hasNextPage"]:
            return list(nodes.values()), len(nodes) != count, ""
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            return list(nodes.values()), False, "missing or repeated pagination cursor"
        cursors.add(cursor)
    return list(nodes.values()), True, ""


def _search_window(qualifier, start, end, runner, budget):
    """Split capped inclusive ranges, sharing endpoints to avoid precision gaps."""
    pending = [(start, end)]
    found: dict[str, dict] = {}
    coverage = []
    errors = []
    splits = []
    leaves = []
    while pending:
        lower, upper = pending.pop()
        query = f"{qualifier} updated:{iso(lower)}..{iso(upper)}"
        observed = {}
        nodes, cut, error = search(query, runner, budget=budget, observed=observed)
        for node in nodes:
            updated = parse_iso(node.get("updatedAt"))
            if updated is None or not lower <= updated <= upper:
                error = "search result timestamp is invalid or outside the requested interval"
                cut = False
                continue
            previous = found.get(node["url"])
            if previous is None or updated >= parse_iso(previous["updatedAt"]):
                found[node["url"]] = node
        if cut and upper - lower > timedelta(seconds=1):
            splits.append((lower, upper, observed["count"]))
            midpoint = lower + timedelta(seconds=int((upper - lower).total_seconds()) // 2)
            pending.extend([(midpoint, upper), (lower, midpoint)])
            continue
        if cut:
            error = f"search remains incomplete within second interval {iso(lower)}..{iso(upper)}"
        complete = not cut and not error
        coverage.append({"start": iso(lower), "end": iso(upper),
                         "count": len(nodes), "complete": complete})
        leaves.append((lower, upper, {node["url"] for node in nodes}))
        if error:
            errors.append(error)
    if not errors:
        for lower, upper, expected in splits:
            identities = set().union(*(urls for first, last, urls in leaves
                                      if lower <= first and last <= upper))
            if len(identities) != expected:
                errors.append("search count changed while splitting ranges")
                for window in coverage:
                    if lower <= parse_iso(window["start"]) and parse_iso(window["end"]) <= upper:
                        window["complete"] = False
    return list(found.values()), coverage, "; ".join(dict.fromkeys(errors))


def _utc_second(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    try:
        return value.astimezone(timezone.utc).replace(microsecond=0)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} must fit in the UTC datetime range") from error


def _validate_limits(max_requests, max_seconds):
    if type(max_requests) is not int or max_requests <= 0:
        raise ValueError("max_requests must be a positive integer")
    if (type(max_seconds) not in (int, float) or not math.isfinite(max_seconds)
            or max_seconds <= 0):
        raise ValueError("max_seconds must be a positive finite number")


def merge_state(node: dict) -> dict:
    """A pull request's `review_decision`, `checks` and `mergeable` (pack sd:6711), lower-cased; None is not read.

    GitHub's null review decision or rollup is read: `none`, no review asked or no check on the head.
    """
    def word(value, none=None):
        return none if value is None else value.lower() if isinstance(value, str) and value else None

    commits = node.get("commits")
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    commit = nodes[-1].get("commit") if isinstance(nodes, list) and nodes and isinstance(nodes[-1], dict) else None
    rollup = commit.get("statusCheckRollup") if isinstance(commit, dict) else None
    return {"review_decision": word(node.get("reviewDecision"), "none") if "reviewDecision" in node else None,
            "checks": None if not isinstance(commit, dict) else "none" if rollup is None
            else word(rollup.get("state") if isinstance(rollup, dict) else None),
            "mergeable": word(node.get("mergeable"))}


def normalize(node: dict, why: str) -> dict | None:
    """One search result as a `shadow` row, or None when it is unusable.

    A node with no URL cannot be keyed, unioned or opened, so it is dropped
    rather than stored under an empty string where it would collide with the
    next one.
    """
    url = (node.get("url") or "").strip()
    if not url:
        return None
    return {
        "tracker": TRACKER,
        "url": url,
        "repo": (node.get("repository") or {}).get("nameWithOwner") or "",
        "number": node.get("number"),
        "kind": "pull" if node.get("__typename") == "PullRequest" else "issue",
        "title": node.get("title") or "",
        "state": (node.get("state") or "").lower(),
        "author": (node.get("author") or {}).get("login") or "",
        "merged_at": settled_at(node.get("mergedAt")),
        "closed_at": settled_at(node.get("closedAt")),
        "branch": (node.get("headRefName") or None) if isinstance(node.get("headRefName"), str) else None,
        "linked_issues": closing_issues(node) if node.get("__typename") == "PullRequest" else None,
        "updated_at": node.get("updatedAt") or "",
        "created_at": settled_at(node.get("createdAt")),
        "assignee": assignees(node),
        **(merge_state(node) if node.get("__typename") == "PullRequest" else {}),
        "why": [why],
    }


@dataclass
class Collected:
    """One collect, whether or not it succeeded. `ok` gates the watermark."""

    ok: bool
    reason: str
    issues: list[dict] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    window_start: str = ""
    window_end: str = ""
    requests: int = 0
    coverage: list[dict] = field(default_factory=list)
    #: The intake sweeps that failed or capped, and the listing of each one
    #: that completed: `(owner/name, ...)` to every URL it or the window saw
    #: (sd:4880).
    incomplete: list[str] = field(default_factory=list)
    swept: list[tuple[tuple[str, ...], set[str]]] = field(default_factory=list)
    #: The `mine` repositories whose `MINE_SWEEPS` all completed, and the urls this run found assigned to the
    #: operator and mentioning them, in any search (pack sd:7672): `restate_reasons` decides from these.
    reached: list[tuple[str, ...]] = field(default_factory=list)
    assigned: set[str] = field(default_factory=set)
    mentioned: set[str] = field(default_factory=set)


def collect(watermark: str | None, now: datetime, runner=None, *, since=None,
            max_requests=MAX_REQUESTS, max_seconds=MAX_SECONDS, budget=None, intake=(), mine=()) -> Collected:
    """Every bucket, unioned by URL.

    A bucket that errors makes the whole collect unsuccessful, and the caller
    must not advance the watermark. Returning the rows that did arrive is
    still right -- they are real. What must not happen is treating a partial
    collect as a complete one and stepping the window past the part that
    failed.

    `intake` names the `owner/name` of each repository with `policy.issues`
    read or ask (sd:4880). After the buckets, one unwindowed search lists the open
    issues of up to `INTAKE_GROUP` of them, whoever opened them, so a
    repository switched on today has its old issues read on the next run.
    The sweep reads outside the window the operator's buckets covered, so a
    row only it found names no reason and keeps the stored one. A complete
    sweep lands in `swept`: `sync` reads each stored open issue it missed
    directly (`confirm_absent`), which is how a closure is seen. Every run
    repeats the sweep whole and it reads no window, so its failure or cap
    holds no cursor: it is named in `incomplete`, and closes nothing.

    `mine` names the intake repositories whose `issues_scope` is `mine` (pack
    sd:7672). Before the sweep, `MINE_SWEEPS` list their open issues assigned
    to the operator or mentioning them, unwindowed. A row only these found
    gets `assigned` or no reason, and no `filed`: they cannot say who opened
    it. A group whose searches all completed lands in `reached`, and `sync`
    restates its rows' reasons from them; a failed or capped one is named in
    `incomplete` and restates nothing.
    """
    _validate_limits(max_requests, max_seconds)
    end = _utc_second(now, "now")
    start = _utc_second(since if since is not None else window_start(watermark, end), "since")
    if start > end:
        raise ValueError("since must not be later than now")
    budget = budget or _SearchBudget(max_requests, time.monotonic() + max_seconds)
    left = budget.deadline - time.monotonic()
    if left <= 0:
        return Collected(ok=False, reason="collection time limit exhausted", window_start=iso(start), window_end=iso(end))
    ok, reason = available(runner, timeout=min(GH_TIMEOUT_SECONDS, left))
    if not ok:
        return Collected(ok=False, reason=reason, window_start=iso(start), window_end=iso(end))
    merged: dict[str, dict] = {}
    truncated: list[str] = []
    errors: list[str] = []
    coverage = []
    groups = [" ".join(f"repo:{slug}" for slug in intake[at:at + INTAKE_GROUP])
              for at in range(0, len(intake), INTAKE_GROUP)]
    for why, qualifier in BUCKETS:
        nodes, windows, error = _search_window(qualifier, start, end, runner, budget)
        coverage.extend({"bucket": why, **window} for window in windows)
        if error:
            errors.append(f"{why}: {error}")
        if any(not window["complete"] for window in windows):
            truncated.append(why)
        for node in nodes:
            row = normalize(node, why)
            if row is None:
                continue
            found = merged.get(row["url"])
            if found is None:
                merged[row["url"]] = row
            elif why not in found["why"]:
                found["why"].append(why)
    swept_only: set[str] = set()
    listings = []
    incomplete = []
    windowless: set[str] = set()
    unknown_mine: set[str] = set()
    reached = []
    for at in range(0, len(mine), INTAKE_GROUP):
        slugs = tuple(mine[at:at + INTAKE_GROUP])
        scope = " ".join(f"repo:{slug}" for slug in slugs)
        complete = True
        for why, qualifier in MINE_SWEEPS:
            nodes, cut, error = search(f"is:issue is:open {qualifier} {scope}", runner, budget=budget)
            if error or cut:
                complete = False
                unknown_mine.add(why)
                incomplete.append(f"{qualifier} {scope}: {error or 'more open issues than one search returns'}")
            for node in nodes:
                row = normalize(node, why)
                if row is None:
                    continue
                found = merged.get(row["url"])
                if found is None:
                    merged[row["url"]] = row
                    windowless.add(row["url"])
                elif why not in found["why"]:
                    found["why"].append(why)
        if complete:
            reached.append(slugs)
    for at, group in enumerate(groups):
        nodes, cut, error = search(f"is:issue is:open {group}", runner, budget=budget)
        if error or cut:
            incomplete.append(f"{group}: {error or 'more open issues than one search returns'}")
        for node in nodes:
            row = normalize(node, INTAKE)
            if row is None:
                continue
            found = merged.get(row["url"])
            if found is None:
                merged[row["url"]] = row
                swept_only.add(row["url"])
            elif row["updated_at"] > found["updated_at"]:
                # Reopened after a bucket read it closed: the later answer wins.
                found.update(state=row["state"], title=row["title"], updated_at=row["updated_at"],
                             merged_at=row["merged_at"], closed_at=row["closed_at"])
        if not error and not cut:
            listings.append((intake[at * INTAKE_GROUP:(at + 1) * INTAKE_GROUP], set(merged)))
    rows = sorted(merged.values(), key=lambda row: row["updated_at"], reverse=True)
    for row in rows:
        row["why"].sort()
        # A row only the `mine` searches found is outside the window, so whether the operator opened it is unknown.
        row["reason"] = (None if row["url"] in swept_only
                         else reason_of(row["why"], unknown_mine | {"author"}) if row["url"] in windowless
                         else reason_of(row["why"], truncated))
        # Apart from `reason`, which names an assigned row assigned. A row `author:@me` returned is filed, even from
        # a cut search; absence from a cut one is unknown, as `reason_of` reads it.
        row["filed"] = (None if row["url"] in swept_only | windowless else True if "author" in row["why"]
                        else None if "author" in truncated else False)
    return Collected(
        ok=not errors and not truncated,
        reason="; ".join(errors),
        issues=rows,
        truncated=truncated,
        window_start=iso(start),
        window_end=iso(end),
        requests=budget.requests,
        coverage=coverage,
        incomplete=incomplete,
        swept=listings,
        reached=reached,
        assigned={row["url"] for row in rows if "assigned" in row["why"]},
        mentioned={row["url"] for row in rows if {"mentioned", "review-requested"} & set(row["why"])},
    )


def store(connection: sqlite3.Connection, issues: list[dict]) -> int:
    """Write collected issues as `shadow` rows, keyed by `(tracker, url)`.

    `normalize` returns fourteen keys and this writes thirteen, plus the `reason`
    `collect` derives from `why` (migration 34). `updated_at` (migration 39)
    is written as `iso` spells it, or None when it does not read, which keeps
    the stored one. `created_at`, `filed`, `status` and `assignee` (migration
    40) follow the same rule, as do a pull request's `review_decision`, `checks`
    and `mergeable` (migration 44). The one it does not write, `why`, is not an oversight;
    the `shadow` table has no column for it, and the comment above that table
    says why and where it goes instead. It goes there *before* this runs:
    `sync` passes the same `result.issues` to `contribution_sync.refresh`
    first. Returns the count.
    """
    for issue in issues:
        upsert_shadow(
            connection,
            tracker=issue.get("tracker") or TRACKER,
            url=issue["url"],
            repo=issue.get("repo") or None,
            number=issue.get("number"),
            kind=issue.get("kind") or None,
            title=issue.get("title") or None,
            state=issue.get("state") or None,
            author=issue.get("author") or None,
            reason=issue.get("reason"),
            merged_at=issue.get("merged_at"),
            closed_at=issue.get("closed_at"),
            branch=issue.get("branch"),
            linked_issues=issue.get("linked_issues"),
            updated_at=settled_at(issue.get("updated_at")),
            created_at=settled_at(issue.get("created_at")),
            filed=issue.get("filed"),
            status=issue.get("status") or None,
            assignee=issue.get("assignee"),
            review_decision=issue.get("review_decision"),
            checks=issue.get("checks"),
            mergeable=issue.get("mergeable"),
        )
    return len(issues)


def _observe_protection(connection: sqlite3.Connection, client, observed_at: str) -> dict:
    """Run the protection collector and record its own heartbeat.

    Guarded twice over, because this is a side observation of the tracker's
    sync and not the sync: a failure in the collector becomes `unknown` rows
    (inside `protection.sync`) or, if the collector itself raises, an errors
    list in the heartbeat -- and never an exception out of `sync()`, a failed
    `result.ok`, or a held watermark.
    """
    from . import protection

    try:
        summary = protection.sync(connection, client=client, observed_at=observed_at)
    except Exception as error:  # noqa: BLE001 - a side observation never fails the tracker
        summary = {"attempted": 0, "protected": 0, "unprotected": 0, "unknown": 0,
                   "requests": 0, "errors": [f"{type(error).__name__}: {error}"]}
    try:
        record_state(connection, "heartbeat", key=protection.HEARTBEAT_KEY,
                     timestamp=datetime.fromtimestamp(time.time(), timezone.utc).isoformat(timespec="seconds"),
                     body={"ok": not summary["errors"], "observed_at": observed_at, **summary})
    except Exception:  # noqa: BLE001 - same rule: the heartbeat is reporting, not the work
        pass
    return summary


def confirm_absent(connection: sqlite3.Connection, swept, client, *,
                   tracker: str = TRACKER) -> tuple[dict[str, str | None], list[str], list[str]]:
    """Read each stored open issue a complete sweep missed.

    Returns `(close, unknown, open)`. `close` maps each url to close to the
    `closed_at` its read gave, or None when that read gave none it could
    parse, read another issue (sd:5303), or answered 410 (pack sd:5415).
    `open` names the urls whose read said open.

    Absence from a search is no proof of a closure: GitHub's index can omit
    an open issue for a while (sd:4880 lane review). So each one is read
    through `client`, the contribution REST path on the collect's budget,
    and only that read's own answer closes it: state `closed`, a
    `pull_request` key, or another issue number. Its `html_url` decides
    nothing: REST answers a canonical spelling that a stored url in another
    case, with a trailing slash or under an old repository name does not
    match (round 2). An open answer leaves the row, a transferred issue that
    keeps its number too: it is open somewhere, and Today offers it. A 410
    closes the row: GitHub gives it for a deleted issue and for every issue
    of a repository whose Issues are disabled. Any other failed, refused,
    malformed or over-budget read is unknown, a 404 too, since a token that
    lost access gets one: the row stays open and is named in `unknown`, and
    the next run reads it again.
    """
    from .contribution_github import ISSUE, Gone, Unavailable

    urls = [row["url"] for slugs, seen in swept
            for row in open_shadow_issues(connection, tracker=tracker, repos=slugs) if row["url"] not in seen]
    close: dict[str, str | None] = {}
    unknown: list[str] = []
    opened: list[str] = []
    for url in dict.fromkeys(urls):
        match = ISSUE.fullmatch(url)
        if not match:
            unknown.append(f"{url}: not an issue url")
            continue
        try:
            payload = client.get(f"repos/{match[1]}/{match[2]}/issues/{match[3]}")
        except Gone:
            close[url] = None
            continue
        except Unavailable as error:
            unknown.append(f"{url}: {error}")
            continue
        if not isinstance(payload, dict):
            unknown.append(f"{url}: the issue read is not an object")
        elif payload.get("state") == "closed" or "pull_request" in payload:
            close[url] = settled_at(payload.get("closed_at"))
        elif type(payload.get("number")) is not int:
            unknown.append(f"{url}: the issue read has no number")
        elif payload["number"] != int(match[3]):
            close[url] = None  # another issue's read says nothing of when this one closed
        elif payload.get("state") != "open":
            unknown.append(f"{url}: the issue read says state {payload.get('state')!r}")
        else:
            opened.append(url)
    return close, unknown, opened


def identity(url) -> tuple[str, str, int] | None:
    """`(kind, owner/name, number)` of a GitHub pull request or issue url, lowered, or None."""
    from .contribution_github import ISSUE, PULL

    for kind, pattern in (("pull", PULL), ("issue", ISSUE)):
        match = pattern.fullmatch(url) if isinstance(url, str) else None
        if match:
            return kind, f"{match[1]}/{match[2]}".lower(), int(match[3])
    return None


def _revisit_unsettled(connection: sqlite3.Connection, tracker: str) -> list[str]:
    """The urls revisits read and left as they were, longest deferred first, from the last heartbeat (sd:6091)."""
    row = connection.execute("SELECT body FROM state WHERE kind = 'heartbeat' AND key = ? ORDER BY id DESC LIMIT 1",
                             (f"tracker-sync:{tracker}",)).fetchone()
    try:
        unsettled = json.loads(row["body"]).get("revisit_unsettled") if row else None
    except (TypeError, ValueError, AttributeError, RecursionError):
        return []
    return [url for url in unsettled if isinstance(url, str)] if isinstance(unsettled, list) else []


def revisit_rows(connection: sqlite3.Connection, planned: dict, *, tracker: str, intake, before: datetime,
                 seen, unsettled=()) -> list[dict]:
    """The stale open rows in the order a run reads them directly, oldest first (pack sd:5415, sd:6091).

    A shadow row no search has seen since `before`, an issue outside intake
    or a pull request, and a queued contribution whose last complete
    observation is older than `before`: a windowed bucket stops returning a
    row once it leaves the operator's searches, and a repository that moved
    answers no read under the old name. One url read serves both stores.
    Each row is `{url, shadow, key}`: the url to read, the stored shadow url
    or None, and the contribution key or None. A url this run's search saw
    is current and is left out. One in `unsettled`, which a run read and
    left as it was -- a failed read, or a contribution's unmoved answer --
    goes after the rest, in the order it was deferred, so rows that always
    fail cannot starve the others, nor the oldest of them the rest. Of the
    rest, shadow rows go before contribution-only ones (sd:7265). The
    caller reads the first `REVISIT_LIMIT`.
    """
    floor = datetime.min.replace(tzinfo=timezone.utc)
    current = {identity(url) or url for url in seen}
    rows: dict = {}
    for row in stale_shadow_rows(connection, tracker=tracker, outside=tuple(intake),
                                 before=before.isoformat(timespec="seconds")):
        name = identity(row["url"]) or row["url"]
        if name not in current:
            rows.setdefault(name, {"url": row["url"], "shadow": row["url"], "key": None,
                                   "at": parse_iso(row["last_seen"]) or floor})
    for entry in planned["pending"]:
        snapshot = planned["snapshots"].get(entry["key"]) or {}
        observed = parse_iso((snapshot.get("observation") or {}).get("observed_at"))
        name = identity(entry.get("url"))
        if name is None or observed is None or observed >= before or name in current:
            continue
        rows.setdefault(name, {"url": entry["url"], "shadow": None, "key": None, "at": observed})["key"] = entry["key"]
    # By identity: a shadow row and a contribution can spell one url two ways.
    # Undeferred shadow rows first (sd:7265): no other read closes one, and
    # 1,750 older contributions held every slot. A deferred one goes after
    # the contributions: only a revisit folds one whose repository moved.
    turn = {identity(url) or url: index for index, url in enumerate(unsettled)}
    ordered = sorted(rows.values(), key=lambda row: (turn.get(identity(row["url"]) or row["url"], -1),
                                                     row["shadow"] is None, row["at"], row["url"]))
    return [{name: row[name] for name in ("url", "shadow", "key")} for row in ordered]


def revisit(client, url: str) -> dict:
    """What one direct read says of a stale row (sd:6091).

    `{state, closed_at, merged_at}`, plus `url`, `repo` and `number` when
    GitHub answered under another url of the same kind: a transferred or
    renamed repository redirects the old one, and the row moves there. Or
    `{unknown: reason}`, with `unread` when the budget stopped the read.
    An issue read follows `confirm_absent`'s rules: a 410, a pull request
    or, unmoved, another number closes it. A pull request read settles it
    `merged` or `closed` on its own answer. Any other failed, refused or
    malformed read is unknown, a 404 too.
    """
    from .contribution_github import Exhausted, Gone, Unavailable

    name = identity(url)
    if name is None:
        return {"unknown": f"{url}: not a pull request or issue url"}
    kind, slug, number = name
    try:
        payload = client.get(f"repos/{slug}/{'pulls' if kind == 'pull' else 'issues'}/{number}")
    except Gone:
        return {"state": "closed", "closed_at": None}
    except Exhausted as error:
        return {"unknown": f"{url}: {error}", "unread": True}
    except Unavailable as error:
        return {"unknown": f"{url}: {error}"}
    if not isinstance(payload, dict):
        return {"unknown": f"{url}: the read is not an object"}
    if kind == "issue" and "pull_request" in payload:
        return {"state": "closed", "closed_at": settled_at(payload.get("closed_at"))}
    if type(payload.get("number")) is not int:
        return {"unknown": f"{url}: the read has no number"}
    answer: dict = {}
    moved = identity(payload.get("html_url"))
    if moved is not None and moved[0] == kind and moved != name and moved[2] == payload["number"]:
        answer = {"url": payload["html_url"], "repo": "/".join(payload["html_url"].split("/")[3:5]),
                  "number": moved[2]}
    elif payload["number"] != number:
        return {"state": "closed", "closed_at": None} if kind == "issue" else {
            "unknown": f"{url}: the read answers another number"}
    state = payload.get("state")
    if state not in ("open", "closed") or (kind == "pull" and type(payload.get("merged")) is not bool):
        return {"unknown": f"{url}: the read says state {state!r}"}
    if kind == "pull" and payload["merged"]:
        state = "merged"
    return {**answer, "state": state, "closed_at": None if state == "open" else settled_at(payload.get("closed_at")),
            "merged_at": settled_at(payload.get("merged_at")) if state == "merged" else None}


@dataclass
class Synced:
    """What one sync did, in the shape the pack's verb prints."""

    ok: bool
    reason: str
    written: int
    truncated: list[str]
    window_start: str
    watermark_moved: bool
    window_end: str = ""
    requests: int = 0
    coverage: list[dict] = field(default_factory=list)
    #: Contribution details the budget did not reach, and the ones it reached
    #: but could not complete (`key: reason`). Neither is a failure of the
    #: collect: the first is a backlog for the next run, the second is held
    #: on that contribution's own heartbeat.
    queued: int = 0
    incomplete: list[str] = field(default_factory=list)
    #: False only when the tracker's configuration is absent, so the caller
    #: can print "not collected" rather than a failure without parsing
    #: `reason`. Last, so positional construction is unaffected.
    configured: bool = True
    #: The intake sweeps that failed or capped (sd:4880). Unknown, so named;
    #: not a failure, since the next run repeats each sweep whole.
    intake_incomplete: list[str] = field(default_factory=list)
    #: What `pr_attention` and `issue_attention` filed and cleared after the
    #: sync (sd:6712, sd:8142), and on Jira the ticket moves (`jira_moves`,
    #: sd:8117); never a failure of the sync.
    attention: list[str] = field(default_factory=list)
    #: What the pull-request lane pass could not link or note (sd:7859); a side observation, never a failure.
    lanes: list[str] = field(default_factory=list)

    def report(self) -> list[str]:
        lines = [
            f"shadow sync: {self.written} row(s) from {self.window_start or 'an unwindowed custom query'}, "
            f"watermark {'moved' if self.watermark_moved else 'held'}"
        ]
        if self.truncated:
            # Reported rather than swallowed: a collect that hit its page
            # ceiling collected less than it saw, and a count that omits the
            # remainder reads as "that is all there is".
            lines.append(f"shadow sync: truncated buckets: {', '.join(self.truncated)}")
        if not self.ok:
            lines.append(f"shadow sync: {self.reason}")
        if self.incomplete:
            lines.append(f"shadow sync: contribution observation incomplete: {'; '.join(self.incomplete)}")
        if self.queued:
            lines.append(f"shadow sync: contribution detail backlog: {self.queued} queued")
        if self.intake_incomplete:
            lines.append(f"shadow sync: intake sweep incomplete: {'; '.join(self.intake_incomplete)}")
        lines += [f"shadow sync: {line}" for line in self.attention]
        if self.lanes:
            lines.append(f"shadow sync: pull-request lanes: {'; '.join(self.lanes)}")
        return lines


class SyncBusy(SdDbError):
    """Another `sync` holds this database's shadow-sync lock."""


@contextmanager
def sync_lock(connection: sqlite3.Connection):
    """Hold this database's shadow-sync lock, or raise `SyncBusy` at once.

    Every `sync` runs inside it, so the nightly job, a terminal and the
    dashboard never collect at once (sd:2207 review): `store` writes rows
    before the cursor recheck, and an older run finishing second would leave
    its older observations over a newer run's. The lock is an flock on a file
    beside the database, so it is the hub's alone; a satellite refuses with
    `HubOnly`, as the control gate does. An in-memory database has no file
    another process could reach, and takes no lock.
    """
    from .runner import RunnerRefused
    from .runner_journal import lock

    refuse_hub_only(connection, "shadow sync")
    filename = next((row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not filename:
        yield
        return
    path = Path(filename).resolve().parent / "operation-locks" / "shadow-sync.lock"
    entered = False
    try:
        with lock(path, blocking=False, noun="shadow sync"):
            entered = True
            yield
    except RunnerRefused as error:
        if entered:
            raise
        if not isinstance(error.__cause__, BlockingIOError):
            raise SdDbError(str(error)) from error
        raise SyncBusy("another shadow sync is already running against this database; wait for it to finish") from error


def sync(connection: sqlite3.Connection, **options) -> Synced:
    """`_sync` under `sync_lock`: one collect at a time per database, whoever calls it."""
    with sync_lock(connection):
        return _sync(connection, **options)


def _sync(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    runner=None,
    tracker: str = TRACKER,
    since: datetime | None = None,
    max_requests: int = MAX_REQUESTS,
    max_seconds: float = MAX_SECONDS,
    contribution_client=None,
    notifier=None,
    alerts: bool = False,
    mention_days: int | None = None,
) -> Synced:
    """Collect, write the rows, and advance the cursor only on success.

    `alerts` runs `pr_attention` after a GitHub sync (sd:6712): the pack's
    verb asks for it on a scheduled run, not on a recovery window.

    This is what `sd shadow sync` calls. The order is the whole design: rows
    are written whether or not the collect succeeded, because a partial
    answer is still true; the cursor moves only on the collect's own success.

    Success is the search's: complete coverage of every bucket, and the
    identities it found staged durably. The contribution details that follow
    on the same budget are one observation per contribution, and one that
    cannot be completed -- a dependency with no configured release tag, a
    pull whose API read fails -- is held on that contribution's heartbeat and
    named in `incomplete`, not turned into a failed collect. Until 2026-09-12
    it was, and one item with a missing tag made every night's strict run
    unsuccessful and the tracker's health `degraded`, while the cursor had
    in fact moved. Only the detail collector's own failures (no authenticated
    operator, a queue checkpoint that moved) still fail the run.

    `tracker` chooses the collector, from `TRACKERS`. `jira` is collect,
    store, watermark on success, heartbeat, the linked-ticket moves
    (`jira_moves`, sd:8117), the issue alerts when `alerts` asks for
    them (sd:8142), reading mentions from the last `mention_days` (`sd.jira_recent_days`, sd:8200),
    and nothing else: `runner` is its
    HTTP transport seam rather than a `gh` runner, `max_requests` and
    `max_seconds` are validated and otherwise unused because Jira's ceiling
    is `shadow_jira.MAX_PAGES`, and `since` is refused because the recovery
    window is GitHub's -- a relative-minutes JQL window cannot express it.
    """
    if tracker not in TRACKERS:
        raise ValueError(f"tracker must be one of {TRACKERS}, not {tracker!r}")
    moment = _utc_second(datetime.now(timezone.utc) if now is None else now, "now")
    from . import contribution_sync, protection, repos
    from .contribution_github import Client

    _validate_limits(max_requests, max_seconds)
    if tracker == "jira":
        return _sync_jira(connection, moment, runner, since, alerts=alerts, max_seconds=max_seconds,
                          mention_days=mention_days)
    planned = contribution_sync.plan(connection)
    budget = _SearchBudget(max_requests, time.monotonic() + max_seconds)
    # Existing work must still refresh when a large search consumes its slice.
    reserve = min(80, max_requests // 2) if planned["pending"] else 0
    search_budget = _SearchBudget(max_requests - reserve,
                                 budget.deadline - (min(120, max_seconds / 2) if reserve else 0))
    previous = read_watermark(connection, tracker)
    taking = repos.intake_repos(connection)
    intake = tuple(dict.fromkeys(taking.values()))
    mine = tuple(dict.fromkeys(slug for path, slug in taking.items() if repos.issues_scope(connection, path) == "mine"))
    result = collect(previous, moment, runner, since=since, max_requests=max_requests,
                     max_seconds=max_seconds, budget=search_budget, intake=intake, mine=mine)
    # Stale rows wait for a run whose buckets completed: one that failed may fail these reads too.
    reader = Client(search_budget, runner=runner)
    closing, unconfirmed, opened = confirm_absent(connection, result.swept, reader, tracker=tracker)
    if unconfirmed:
        result.incomplete.append(f"{len(unconfirmed)} absent issue(s) left open unread, first {unconfirmed[0]}")
    unsettled = _revisit_unsettled(connection, tracker)
    stale = revisit_rows(connection, planned, tracker=tracker, intake=intake, before=moment - STALE_AFTER,
                         seen={row["url"] for row in result.issues}, unsettled=unsettled) if result.ok else []
    settled: dict[str, dict] = {}
    unread: list[str] = []
    read: set[str] = set()
    deferred: list[str] = []
    for row in stale[:REVISIT_LIMIT]:
        answer = revisit(reader, row["url"])
        if "unknown" in answer:
            unread.append(answer["unknown"])
            if not answer.get("unread"):
                read.add(row["url"])
                deferred.append(row["url"])
            continue
        read.add(row["url"])
        folded = False
        if row["key"] and answer.get("url"):
            refused = contribution_sync.fold(connection, planned, row["key"], answer["url"])
            if refused:
                result.incomplete.append(f"{row['key']}: {refused}")
            folded = not refused
        if row["shadow"] and (answer.get("url") or answer["state"] != "open"):
            settled[row["shadow"]] = answer
        elif row["shadow"]:
            opened.append(row["shadow"])
        if row["key"] and not folded:
            # The contribution stays stale: its own queue read settles an unmoved one, and an item refused the fold.
            deferred.append(row["url"])
    if stale:
        # A deferral lasts while its row is stale. A row read this run goes to the back of the line or leaves
        # it; one the budget did not reach keeps its place. Urls compare by identity, as `revisit_rows` orders them.
        current = {identity(row["url"]) or row["url"] for row in stale}
        done = {identity(url) or url for url in read}
        unsettled = [url for url in unsettled if (identity(url) or url) in current
                     and (identity(url) or url) not in done] + deferred
    if unread:
        result.incomplete.append(f"{len(unread)} stale row(s) left open unread, first {unread[0]}")
    budget.remaining -= search_budget.requests
    budget.requests = search_budget.requests
    search_complete = result.ok
    client = contribution_client or Client(budget, runner=runner)
    client.budget = budget
    # Details run until the budget runs out, stopping short of what the
    # protection collector below needs to reach every registered repository:
    # its requests and its time. The time is the limit that ran out first
    # (sd:1663): with the requests reserved alone, a detail backlog spent
    # the deadline and the sweep filed every row on zero requests.
    detail = contribution_sync.refresh(connection, planned, result.issues,
                                       client=client, observed_at=iso(moment),
                                       search_complete=search_complete, reserve=protection.reserve(connection),
                                       reserve_seconds=protection.reserve_seconds(connection, max_seconds))
    if detail["errors"]:
        result.ok = False
        result.reason = "; ".join(filter(None, [result.reason, *detail["errors"]]))
    # The fleet's branch protection rides on the same budget, after the work
    # the tracker exists for. It is a side observation: it cannot fail this
    # sync, hold the watermark, or touch `result` beyond the requests it spent.
    _observe_protection(connection, client, iso(moment))
    # The pull-request lane notes are a side observation too (sd:7859): said in `lanes`, never `result.ok`.
    lanes = contribution_sync.note_lanes(connection)
    result.requests = budget.requests
    # Collection holds no write lock. Recheck the cursor under the transaction
    # because another collector can complete while this one waits for GitHub.
    with transaction(connection):
        written = store(connection, result.issues)
        close_shadow(connection, tracker=tracker, closed=closing)
        settle_shadow(connection, tracker=tracker, settled=settled)
        see_shadow(connection, tracker=tracker, urls=opened)
        for slugs in result.reached:
            restate_reasons(connection, tracker=tracker, repos=slugs,
                            assigned=result.assigned, mentioned=result.mentioned)
        previous_time = parse_iso(read_watermark(connection, tracker))
        # A complete explicit recovery can still leave an uncovered prefix.
        moved = search_complete and detail["staged"] and (previous_time is None or (
            moment > previous_time and parse_iso(result.window_start) <= previous_time
        ))
        if moved:
            write_watermark(connection, tracker, iso(moment), result.window_start)
        record_state(
            connection, "heartbeat", key=f"tracker-sync:{tracker}",
            # Historical recovery bounds must not hide a later failed attempt.
            timestamp=datetime.fromtimestamp(time.time(), timezone.utc).isoformat(timespec="seconds"),
            body={"ok": result.ok, "reason": result.reason, "truncated": result.truncated,
                  "window_start": result.window_start, "window_end": result.window_end,
                  "requests": result.requests, "coverage": result.coverage,
                  "queued": detail["queued"], "incomplete": detail["incomplete"],
                  "intake_incomplete": result.incomplete, "revisit_unsettled": unsettled, "lanes": lanes},
        )
    contribution_sync.dispatch_pending(connection, notifier=notifier, keys=detail["completed_keys"],
                                       deadline=budget.deadline)
    attention = _pr_attention(connection, runner, moment, budget) + _issue_attention(connection, runner, moment, budget) \
        if alerts else []
    return Synced(
        ok=result.ok,
        reason=result.reason,
        written=written,
        truncated=result.truncated,
        window_start=result.window_start,
        watermark_moved=moved,
        window_end=result.window_end,
        requests=result.requests,
        coverage=result.coverage,
        queued=detail["queued"],
        incomplete=detail["incomplete"],
        intake_incomplete=result.incomplete,
        attention=attention,
        lanes=lanes,
    )


def _pr_attention(connection: sqlite3.Connection, runner, moment: datetime, budget: _SearchBudget) -> list[str]:
    """`pr_attention` after the sync, as report lines: a side observation, so its failure is named, never raised (sd:6712).

    Its searches spend the sync's budget last; a budget the sync spent reads nothing and clears nothing."""
    from . import pr_attention

    left = budget.deadline - time.monotonic()
    if budget.remaining < len(pr_attention.SEARCHES) or left <= 0:
        return ["pull request alerts not read: the sync spent its request or time limit"]
    budget.remaining -= len(pr_attention.SEARCHES)
    budget.requests += len(pr_attention.SEARCHES)
    try:
        found, complete, reason = pr_attention.read(runner, timeout=min(GH_TIMEOUT_SECONDS, left), now=moment)
        done = pr_attention.alert(connection, found, complete, now=moment)
    except (SdDbError, sqlite3.Error, OSError, ValueError) as error:
        return [f"pull request alerts not filed: {error}"]
    lines = [f"pull request alerts: {len(done['filed'])} filed, {len(done['cleared'])} cleared, "
             f"{len(found)} state(s) open"]
    if not complete:
        lines.append(f"pull request alerts: read incomplete, nothing cleared: {reason}")
    lines += [f"pull request alerts: {problem}" for problem in done["problems"]]
    return lines


def _issue_attention(connection: sqlite3.Connection, runner, moment: datetime, budget: _SearchBudget) -> list[str]:
    """`issue_attention` after the sync, as report lines (sd:8142): a side observation, as `_pr_attention` is."""
    from . import issue_attention

    left = budget.deadline - time.monotonic()
    if budget.remaining < 1 or left <= 0:
        return ["issue alerts not read: the sync spent its request or time limit"]
    budget.remaining -= 1
    budget.requests += 1
    try:
        found, unread, reason = issue_attention.read_github(connection, runner, timeout=min(GH_TIMEOUT_SECONDS, left))
    except (SdDbError, sqlite3.Error, OSError, ValueError) as error:
        return [f"issue alerts not filed: {error}"]
    return _issue_alerts(connection, found, unread, reason, moment)


def _issue_alerts(connection: sqlite3.Connection, found: dict, unread: set, reason: str, moment: datetime) -> list[str]:
    """File what one tracker's read found, as report lines."""
    from . import issue_attention

    try:
        done = issue_attention.alert(connection, found, unread, now=moment)
    except (SdDbError, sqlite3.Error, OSError, ValueError) as error:
        return [f"issue alerts not filed: {error}"]
    lines = [f"issue alerts: {len(done['filed'])} filed, {len(done['cleared'])} cleared, {len(found)} state(s) open"]
    if reason:
        lines.append(f"issue alerts: read incomplete, those states not cleared: {reason}")
    return lines + [f"issue alerts: {problem}" for problem in done["problems"]]


def _sync_jira(connection: sqlite3.Connection, moment: datetime, transport, since, *, alerts: bool = False,
               max_seconds: float = MAX_SECONDS, mention_days: int | None = None) -> Synced:
    """Jira's sync: the GitHub shape with every GitHub-only stage left out.

    An unconfigured Jira is not a failure of the run. It writes the heartbeat
    with the missing variables' names, so a reader on the same machine can say
    why there is no row, and returns `configured=False` before any request.
    """
    from . import shadow_jira

    if since is not None:
        raise ValueError("since is GitHub's recovery window; the jira tracker takes none")
    configured = not shadow_jira.missing(shadow_jira.settings())
    previous = read_watermark(connection, shadow_jira.TRACKER)
    # `collect` checks the configuration first: unconfigured, it answers with
    # no request, no issues and `ok` False, so nothing below stores or moves.
    result = shadow_jira.collect(previous, moment, transport)
    # Collection holds no write lock; recheck the cursor under the transaction.
    with transaction(connection):
        written = store(connection, result.issues)
        previous_time = parse_iso(read_watermark(connection, shadow_jira.TRACKER))
        # A custom `JIRA_JQL` run has no window (empty `window_start`), so it
        # holds the cursor; `windowed` lets `tracker_freshness` count it (pack sd:5376).
        windowed = bool(result.window_start)
        moved = result.ok and windowed and (previous_time is None or moment > previous_time)
        if moved:
            write_watermark(connection, shadow_jira.TRACKER, iso(moment), result.window_start)
        record_state(
            connection, "heartbeat", key=f"tracker-sync:{shadow_jira.TRACKER}",
            timestamp=datetime.fromtimestamp(time.time(), timezone.utc).isoformat(timespec="seconds"),
            body={"ok": result.ok, "reason": result.reason, "truncated": result.truncated,
                  "window_start": result.window_start, "window_end": result.window_end,
                  "windowed": windowed, "requests": result.requests, "coverage": result.coverage,
                  "queued": 0, "incomplete": []},
        )
    # Ticket moves (sd:8117) are a side step, as the lane notes are: said, never a failed sync.
    # A failed collect says Jira did not answer this run, so no move reads a failure as a ticket's own.
    moves: list[str] = []
    if configured and not result.ok:
        moves = ["jira moves: not run, the collect failed; cursor held"]
    elif configured:
        try:
            from . import jira_moves

            moves = jira_moves.run(connection, transport)
        except Exception as error:  # noqa: BLE001 - a side step never fails the sync
            moves = [f"jira moves: not run: {type(error).__name__}: {error}"]
    if alerts:
        from . import issue_attention

        try:
            found, unread, reason = issue_attention.read_jira(
                connection, transport=transport, deadline=time.monotonic() + max_seconds, now=moment,
                days=mention_days or issue_attention.shadow_jira.MENTION_DAYS)
            moves = moves + _issue_alerts(connection, found, unread, reason, moment)
        except (SdDbError, sqlite3.Error, OSError, ValueError) as error:
            moves = moves + [f"issue alerts not filed: {error}"]
    return Synced(
        ok=result.ok,
        reason=result.reason,
        written=written,
        truncated=result.truncated,
        window_start=result.window_start,
        watermark_moved=moved,
        window_end=result.window_end,
        requests=result.requests,
        coverage=result.coverage,
        configured=configured,
        attention=moves,
    )
