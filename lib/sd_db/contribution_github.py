"""Bounded read-only contribution observations; the core owns classification.

GitHub search still discovers work in shadow_sync. These detail reads supplement
that same collector, never infer an empty result from an unavailable endpoint,
and share its request/time budget. No API here posts to GitHub or PyPI.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

from .errors import SdDbError
from .shadow_sync import GH_TIMEOUT_SECONDS, MAX_PAGES, PAGE_SIZE, _run, parse_iso
from .writes import read_settings

MAX_BYTES = 8 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}")
PULL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?")
ISSUE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?")
MAINTAINERS = {"OWNER", "MEMBER", "COLLABORATOR"}
AUTOMATION_KEY = "contributions.automation_logins"
AUTOMATION_PATTERN = r"[A-Za-z0-9](-?[A-Za-z0-9])*(,[A-Za-z0-9](-?[A-Za-z0-9])*)*"
AUTOMATION_SETTINGS = {AUTOMATION_KEY: (
    "GitHub logins, comma-separated, of service accounts: their comments are no maintainer comments.",
    AUTOMATION_PATTERN)}
ACTOR = "actor { login ... on User { databaseId } ... on Bot { databaseId } }"
TIMELINE = """query($owner:String!,$repo:String!,$number:Int!,$after:String) {
  repository(owner:$owner,name:$repo) { pullRequest(number:$number) {
    timelineItems(first:100,after:$after) { pageInfo { hasNextPage endCursor } nodes {
      __typename
      ... on LabeledEvent { id createdAt ACTOR label { name } }
      ... on UnlabeledEvent { id createdAt ACTOR label { name } }
      ... on ConvertToDraftEvent { id createdAt ACTOR }
      ... on ReadyForReviewEvent { id createdAt ACTOR }
      ... on ClosedEvent { id createdAt ACTOR }
      ... on ReopenedEvent { id createdAt ACTOR }
      ... on ReviewRequestedEvent { id createdAt ACTOR requestedReviewer { ... on User { databaseId } } }
      ... on HeadRefForcePushedEvent { id createdAt ACTOR }
    } }
  } }
}""".replace("ACTOR", ACTOR)
# An issue's timeline: the same paging shape rooted at `issue(number:)`, and a
# ClosedEvent carries its reason because GitHub reports it as a field, not a
# separate event.
ISSUE_TIMELINE = """query($owner:String!,$repo:String!,$number:Int!,$after:String) {
  repository(owner:$owner,name:$repo) { issue(number:$number) {
    timelineItems(first:100,after:$after) { pageInfo { hasNextPage endCursor } nodes {
      __typename
      ... on LabeledEvent { id createdAt ACTOR label { name } }
      ... on UnlabeledEvent { id createdAt ACTOR label { name } }
      ... on ClosedEvent { id createdAt ACTOR stateReason }
      ... on ReopenedEvent { id createdAt ACTOR }
    } }
  } }
}""".replace("ACTOR", ACTOR)
TIMELINES = {"pullRequest": TIMELINE, "issue": ISSUE_TIMELINE}
# One page of the pull requests that reference an issue, for the `issue`
# dependency: a merged one resolves the issue even when nobody closed it.
ISSUE_REFERENCES = """query($owner:String!,$repo:String!,$number:Int!) {
  repository(owner:$owner,name:$repo) { issue(number:$number) {
    timelineItems(first:100,itemTypes:[CROSS_REFERENCED_EVENT]) { pageInfo { hasNextPage endCursor } nodes {
      __typename
      ... on CrossReferencedEvent { source { ... on PullRequest { url merged } } }
    } }
  } }
}"""


class Unavailable(ValueError):
    """An observation is incomplete and must not replace the last good state."""


class Exhausted(Unavailable):
    """The collection's request or time budget is spent."""


class Gone(Unavailable):
    """HTTP 410: GitHub says the resource is gone for good, such as a deleted
    issue or any issue of a repository whose Issues are disabled (pack sd:5415)."""


@dataclass
class Response:
    status: int
    headers: dict
    body: str


class Client:
    def __init__(self, budget, *, runner=None, transport=None):
        self.budget = budget
        self.runner = runner
        self.transport = transport
        # Requests held back for the collectors that follow on the same budget;
        # `contribution_sync.refresh` raises it to its reserve while it runs.
        self.floor = 0

    def request(self, path, *, fields=None, missing=False):
        left = self.budget.deadline - time.monotonic()
        if self.budget.remaining <= self.floor or left <= 0:
            raise Exhausted("contribution collection budget exhausted")
        self.budget.remaining -= 1
        self.budget.requests += 1
        timeout = min(GH_TIMEOUT_SECONDS, left)
        if self.transport:
            response = self.transport(path, fields, timeout)
        elif path.startswith("https://pypi.org/pypi/"):
            try:
                with urlopen(path, timeout=timeout) as reply:  # nosec B310 - fixed PyPI origin
                    response = Response(reply.status, dict(reply.headers), reply.read(MAX_BYTES + 1).decode())
            except HTTPError as error:
                response = Response(error.code, {}, "")
            except (OSError, URLError, UnicodeError) as error:
                raise Unavailable(f"PyPI request unavailable: {type(error).__name__}") from error
        else:
            argv = ["gh", "api", "--include", "--method", "POST" if fields else "GET", path]
            for key, value in (fields or {}).items():
                argv += ["-F" if type(value) is int else "-f", f"{key}={value}"]
            code, out, _ = _run(argv, self.runner, timeout=timeout)
            # Do not store gh stderr: authentication diagnostics can contain secrets.
            match = re.match(r"HTTP/\S+ (\d+)[^\n]*\n(.*?)\r?\n\r?\n(.*)", out, re.S)
            if not match:
                raise Unavailable(f"GitHub request unavailable (exit {code})")
            headers = dict(line.split(":", 1) for line in match[2].splitlines() if ":" in line)
            response = Response(int(match[1]), headers, match[3])
        if missing and response.status == 404:
            return None, {}
        if response.status != 200:
            raise (Gone if response.status == 410 else Unavailable)(
                f"API HTTP {response.status}; retry on a later collection")
        if len(response.body.encode()) > MAX_BYTES:
            raise Unavailable("API response exceeds collection size limit")
        try:
            payload = json.loads(response.body)
        except (ValueError, RecursionError) as error:
            raise Unavailable("API response is not bounded valid JSON") from error
        if isinstance(payload, dict) and payload.get("errors"):
            raise Unavailable("GitHub returned partial GraphQL errors")
        if missing and payload is None:
            # A `missing` caller reads None as the 404; a JSON `null` is not one.
            raise Unavailable("API response is JSON null")
        return payload, {key.lower().strip(): value.strip() for key, value in response.headers.items()}

    def get(self, path, *, missing=False):
        return self.request(path, missing=missing)[0]

    def pages(self, path, *, member=None, head=None):
        next_path = f"{path}{'&' if '?' in path else '?'}per_page={PAGE_SIZE}"
        rows, seen, paths = [], set(), set()
        expected = None
        for _ in range(MAX_PAGES):
            if next_path in paths:
                raise Unavailable("repeated API pagination link")
            paths.add(next_path)
            payload, headers = self.request(next_path)
            if head is not None and (not isinstance(payload, dict) or payload.get("sha") != head):
                raise Unavailable("commit status response belongs to a stale head")
            page = payload.get(member) if member and isinstance(payload, dict) else payload
            if not isinstance(page, list):
                raise Unavailable("API list is missing")
            if member:
                count = payload.get("total_count")
                if type(count) is not int or count < 0 or expected not in (None, count):
                    raise Unavailable("API count changed or is missing")
                expected = count
            for row in page:
                if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0 or str(row["id"]) in seen:
                    raise Unavailable("API list has missing or repeated identities")
                seen.add(str(row["id"]))
                rows.append(row)
            link = headers.get("link", "")
            following = re.search(r'<([^>]+)>;\s*rel="next"', link)
            if not following:
                if expected is not None and len(rows) != expected:
                    raise Unavailable("API list is truncated")
                return rows
            url = urlsplit(following[1])
            if url.scheme != "https" or url.netloc != "api.github.com" or url.path != urlsplit(path).path:
                raise Unavailable("API pagination escaped its resource")
            next_path = url.path + "?" + url.query
        raise Unavailable("API pagination limit exhausted")

    def timeline(self, owner, repo, number, *, kind="pullRequest"):
        fields = {"query": TIMELINES[kind], "owner": owner, "repo": repo, "number": number}
        rows, cursors = [], set()
        for _ in range(MAX_PAGES):
            payload, _ = self.request("graphql", fields=fields)
            try:
                block = payload["data"]["repository"][kind]["timelineItems"]
                page, info = block["nodes"], block["pageInfo"]
                if not isinstance(page, list) or type(info["hasNextPage"]) is not bool:
                    raise TypeError
                rows.extend(page)
                if not info["hasNextPage"]:
                    return rows
                cursor = info["endCursor"]
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise TypeError
                cursors.add(cursor)
                fields["after"] = cursor
            except (KeyError, TypeError) as error:
                raise Unavailable("incomplete GitHub timeline") from error
        raise Unavailable("timeline pagination limit exhausted")


def identity(user):
    if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0 or not user.get("login"):
        raise Unavailable("GitHub actor identity is unknown")
    return {"id": str(user["id"]), "login": user["login"]}


def pull_parts(url):
    match = PULL.fullmatch(url)
    if not match:
        raise Unavailable("invalid GitHub pull URL")
    return match[1].lower(), match[2].lower(), int(match[3])


def canonical_url(url):
    owner, repo, number = pull_parts(url)
    return f"https://github.com/{owner}/{repo}/pull/{number}"


def issue_parts(url):
    match = ISSUE.fullmatch(url)
    if not match:
        raise Unavailable("invalid GitHub issue URL")
    return match[1].lower(), match[2].lower(), int(match[3])


def canonical_issue_url(url):
    owner, repo, number = issue_parts(url)
    return f"https://github.com/{owner}/{repo}/issues/{number}"


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise Unavailable("GitHub commit identity is unknown")
    return value


def _at(value):
    if parse_iso(value) is None:
        raise Unavailable("GitHub event timestamp is unknown")
    return value


#: Events whose run is fixed by the head, branch and pull requests alone, so a
#: newer run of the same workflow in the same context replaces the older one (sd:1776).
REPLACED_BY_RERUN = frozenset({"pull_request", "push"})


def _ci(client, prefix, head):
    runs = client.pages(f"{prefix}/commits/{head}/check-runs?filter=latest", member="check_runs")
    statuses = client.pages(f"{prefix}/commits/{head}/status", member="statuses", head=head)
    states, ids = [], []
    for run in runs:
        if run.get("head_sha") != head:
            raise Unavailable("CI response belongs to a stale head")
    # `filter=latest` is per check suite, so a rerun on the same head (close
    # and reopen, a body edit) starts a new suite and leaves the older one
    # listed too (sd:1776). A rerun is a newer suite of the same workflow on
    # the same `REPLACED_BY_RERUN` event, branch and pull requests (one head
    # pushed to two branches, or opened against two bases, runs twice with two
    # refs; a pull_request run listing no pull request, as from a fork, proves
    # no context and always counts), so the newest such suite decides,
    # whole: every job in it counts, two of one name included. Other events
    # carry inputs or payloads (two dispatches can test two environments),
    # and one workflow on two events is two executions: all of those count.
    # A name alone
    # proves no rerun, so the head's workflow runs are read only when a name
    # repeats, and a suite no workflow run names (another CI app) keeps every
    # run it has. The lookup is enrichment: if it fails for any reason but
    # the spent budget, it names no suite, and every run counts.
    # Check-suite ids grow with creation. A suite whose every job was skipped
    # (a job conditioned on the payload, skipped on the edit that reran it)
    # tested nothing, so it never replaces one that ran; when every suite of
    # the workflow was skipped, the newest still decides (sd:1824).
    names = [(run.get("name"), (run.get("app") or {}).get("id")) for run in runs]
    workflow = {}
    if len(set(names)) < len(names):
        try:
            rows = client.pages(f"{prefix}/actions/runs?head_sha={head}", member="workflow_runs")
        except Exhausted:
            raise
        except Unavailable:
            rows = []
        for row in rows:
            event = row.get("event")
            pulls = tuple(sorted((pull.get("number"), (pull.get("base") or {}).get("ref"))
                                 for pull in row.get("pull_requests") or []))
            if event in REPLACED_BY_RERUN and (pulls or event != "pull_request"):
                workflow[row.get("check_suite_id")] = (
                    row.get("workflow_id"), event, row.get("head_branch"), pulls)
    suites = [(run.get("check_suite") or {}).get("id") for run in runs]
    skipped = {}
    for run, suite in zip(runs, suites, strict=True):
        done = run.get("status") == "completed" and run.get("conclusion") == "skipped"
        skipped[suite] = skipped.get(suite, True) and done
    latest = {}
    for suite in suites:
        flow = workflow.get(suite)
        if flow is not None and (flow not in latest
                                 or (not skipped[suite], suite) > (not skipped[latest[flow]], latest[flow])):
            latest[flow] = suite
    newest = {run["id"]: run for run, suite in zip(runs, suites, strict=True)
              if workflow.get(suite) is None or latest[workflow[suite]] == suite}
    for run in newest.values():
        status, conclusion = run.get("status"), run.get("conclusion")
        if status not in {"queued", "in_progress", "completed", "waiting", "requested", "pending"}:
            raise Unavailable("unknown check-run status")
        if status != "completed":
            states.append("pending")
        elif conclusion in {"success", "neutral", "skipped"}:
            states.append("success")
        elif conclusion in {"failure", "timed_out", "action_required", "cancelled", "stale", "startup_failure"}:
            states.append("failure")
        else:
            raise Unavailable("unknown CI conclusion")
        ids.append(f"check-run:{run['id']}")
    for status in statuses:
        value = status.get("state")
        if value not in {"success", "pending", "failure", "error"}:
            raise Unavailable("unknown commit status")
        states.append("failure" if value == "error" else value)
        ids.append(f"status:{status['id']}")
    state = "failure" if "failure" in states else "pending" if not states or "pending" in states else "success"
    return state, sorted(ids)


def automation_logins(connection):
    """The lower-cased logins of `AUTOMATION_KEY`; a row that does not read is unknown, not none (sd:8148)."""
    try:
        value = read_settings(connection, [AUTOMATION_KEY]).get(AUTOMATION_KEY)
    except (SdDbError, sqlite3.Error) as error:
        raise Unavailable(f"{AUTOMATION_KEY}: {error}") from error
    return frozenset(login.strip().lower() for login in (value or "").split(",") if login.strip())


def _comment(row, kind, url, operator, *, at, automation=frozenset()):
    actor = identity(row.get("user"))
    # A bot or app post carries its owner's association, not a human ask (sd:8148).
    automated = row["user"].get("type") == "Bot" or actor["login"].endswith("[bot]") \
        or bool(row.get("performed_via_github_app")) or actor["login"].lower() in automation
    body = row.get("body")
    if not isinstance(body, str):
        raise Unavailable("comment body is unavailable")
    return {"id": f"{kind}:{row['id']}:{at}", "actor_id": actor["id"], "kind": "comment", "at": _at(at),
            "maintainer": not automated and row.get("author_association") in MAINTAINERS,
            "mentions_operator": bool(re.search(r"(?<![\w@])@" + re.escape(operator["login"]) + r"(?![\w-])", body, re.I)),
            "url": row.get("html_url", url)}


def _timeline_events(rows, kinds, url):
    """Typed timeline nodes as events, each beside its row for kind-specific fields."""
    for row in rows:
        if not isinstance(row, dict):
            raise Unavailable("timeline contains unknown data")
        typename = row.get("__typename")
        if not isinstance(typename, str):
            raise Unavailable("timeline event type is missing")
        event_kind = kinds.get(typename)
        if not event_kind:
            continue
        actor = row.get("actor") or {}
        event = {"id": row.get("id"), "actor_id": identity({"id": actor.get("databaseId"), "login": actor.get("login")})["id"],
                 "kind": event_kind, "at": _at(row.get("createdAt")), "url": url}
        if not event["id"]:
            raise Unavailable("timeline event identity is missing")
        if event_kind in {"label_added", "label_removed"}:
            event["label"] = row["label"]["name"]
            if not isinstance(event["label"], str):
                raise Unavailable("timeline label identity is missing")
        yield event, row


def _labels(detail):
    labels = detail.get("labels")
    if not isinstance(labels, list) or any(not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels):
        raise Unavailable("current labels are unavailable")
    return [label["name"] for label in labels]


def pull(client, url, operator, *, observed_at, why=(), blocking_labels=(), automation=frozenset()):
    owner, repo, number = pull_parts(url)
    prefix = f"/repos/{owner}/{repo}"
    path = f"{prefix}/pulls/{number}"
    before = client.get(path)
    author = identity(before.get("user"))
    head, base = _sha(before["head"]["sha"]), _sha(before["base"]["sha"])
    events, reviews = [], []
    for row in client.pages(path + "/reviews"):
        if row.get("state") == "PENDING":
            continue
        state = row.get("state")
        if state not in {"CHANGES_REQUESTED", "APPROVED", "COMMENTED", "DISMISSED"}:
            raise Unavailable("unknown review state")
        reviews.append({"id": f"review:{row['id']}", "actor_id": identity(row.get("user"))["id"],
                        "state": state, "at": _at(row.get("submitted_at"))})
        if row.get("body"):
            events.append(_comment(row, "review-comment", url, operator, at=row.get("submitted_at"), automation=automation))
    for endpoint, kind in ((f"{prefix}/issues/{number}/comments", "comment"), (path + "/comments", "inline")):
        for row in client.pages(endpoint):
            events.append(_comment(row, kind, url, operator, at=row.get("updated_at"), automation=automation))
    kinds = {"LabeledEvent": "label_added", "UnlabeledEvent": "label_removed",
             "ConvertToDraftEvent": "converted_to_draft", "ReadyForReviewEvent": "ready_for_review",
             "ClosedEvent": "closed", "ReopenedEvent": "reopened",
             "ReviewRequestedEvent": "review_requested", "HeadRefForcePushedEvent": "pushed"}
    for event, row in _timeline_events(client.timeline(owner, repo, number), kinds, url):
        if event["kind"] == "review_requested":
            # A team request names no user: no reviewer, so it answers no one's changes request.
            reviewer = (row.get("requestedReviewer") or {}).get("databaseId")
            event["reviewer_id"] = str(reviewer) if reviewer is not None else None
        events.append(event)
    ci, ci_ids = _ci(client, prefix, head)
    after = client.get(path)
    if any(before.get(field) != after.get(field) for field in ("head", "base", "updated_at", "state", "draft", "merged")):
        raise Unavailable("pull changed while collecting details")
    if type(before.get("draft")) is not bool or before.get("state") not in {"open", "closed"} or type(before.get("merged")) is not bool:
        raise Unavailable("pull state is incomplete")
    mergeable = after.get("mergeable")
    if mergeable is not None and type(mergeable) is not bool:
        raise Unavailable("unknown mergeability state")
    labels = _labels(before)
    # sd:8084: users asked by name; a team request is in `requested_teams`, which names no one.
    requested = before.get("requested_reviewers")
    if not isinstance(requested, list) or any(not isinstance(user, dict) or not isinstance(user.get("login"), str) for user in requested):
        raise Unavailable("requested reviewers are unavailable")
    # sd:8360: the search's why outlives the request; keep it only while the operator is asked by name.
    asked = operator["login"].lower() in {user["login"].lower() for user in requested}
    why = [reason for reason in why if reason != "review-requested" or asked]
    return {"complete": True, "observed_at": observed_at, "operator": operator, "author": author,
            "repo": f"{owner}/{repo}", "title": before.get("title", ""), "requested_reviewers": [user["login"] for user in requested],
            "state": "merged" if before["merged"] else before["state"], "draft": before["draft"],
            "head": head, "base": base, "mergeable": "unknown" if mergeable is None else "mergeable" if mergeable is True else "conflicting",
            "ci": ci, "ci_head": head, "ci_ids": ci_ids, "why": list(why),
            "blocking_labels": list(blocking_labels), "labels": labels,
            "reviews": reviews, "events": events}


# GitHub's REST `state_reason` and the timeline's `stateReason` name the same
# outcomes the core tells apart. A reason neither table knows (GitHub added
# `duplicate` in 2025) degrades to the plain form: no reason, plain `closed`.
STATE_REASONS = {"completed": "completed", "not_planned": "not_planned", "duplicate": "not_planned"}
CLOSED_KINDS = {"COMPLETED": "closed_completed", "NOT_PLANNED": "closed_not_planned", "DUPLICATE": "closed_not_planned"}


def _issue_detail(client, path):
    """`/issues/n` answers for pull requests too; an observation must not read one as an issue."""
    detail = client.get(path)
    if "pull_request" in detail:
        raise Unavailable("issue URL names a pull request")
    if detail.get("state") not in {"open", "closed"}:
        raise Unavailable("issue state is incomplete")
    return detail


def issue(client, url, operator, *, observed_at, why=(), blocking_labels=(), automation=frozenset()):
    owner, repo, number = issue_parts(url)
    path = f"/repos/{owner}/{repo}/issues/{number}"
    before = _issue_detail(client, path)
    author = identity(before.get("user"))
    events = []
    for row in client.pages(path + "/comments"):
        events.append(_comment(row, "comment", url, operator, at=row.get("updated_at"), automation=automation))
    kinds = {"LabeledEvent": "label_added", "UnlabeledEvent": "label_removed",
             "ClosedEvent": "closed", "ReopenedEvent": "reopened"}
    for event, row in _timeline_events(client.timeline(owner, repo, number, kind="issue"), kinds, url):
        if row["__typename"] == "ClosedEvent":
            event["kind"] = CLOSED_KINDS.get(row.get("stateReason"), "closed")
        events.append(event)
    after = client.get(path)
    if any(before.get(field) != after.get(field) for field in ("updated_at", "state", "state_reason", "labels")):
        raise Unavailable("issue changed while collecting details")
    labels = _labels(before)
    return {"complete": True, "observed_at": observed_at, "operator": operator, "author": author,
            "repo": f"{owner}/{repo}", "title": before.get("title", ""),
            "state": before["state"], "state_reason": STATE_REASONS.get(before.get("state_reason")),
            "why": list(why), "blocking_labels": list(blocking_labels), "labels": labels, "events": events}


def _issue_dependency(client, configured):
    """Closed as completed, or referenced by a merged pull request, resolves an issue."""
    url = configured.get("url")
    owner, repo, number = issue_parts(url or "")
    detail = _issue_detail(client, f"/repos/{owner}/{repo}/issues/{number}")
    proof = {"url": url, "closed": detail["state"] == "closed", "state_reason": STATE_REASONS.get(detail.get("state_reason"))}
    if proof["closed"] and proof["state_reason"] == "completed":
        return {"state": "satisfied", "proof": proof}
    payload, _ = client.request("graphql", fields={"query": ISSUE_REFERENCES, "owner": owner, "repo": repo, "number": number})
    try:
        block = payload["data"]["repository"]["issue"]["timelineItems"]
        nodes, more = block["nodes"], block["pageInfo"]["hasNextPage"]
        # A node that is not an event, or an event with no source object, is a
        # malformed page and not a reference that was not merged (sd:1219).
        if not isinstance(nodes, list) or type(more) is not bool or not all(
                isinstance(node, dict) and isinstance(node.get("source"), dict) for node in nodes):
            raise TypeError
        merged = [node["source"]["url"] for node in nodes if node["source"].get("merged") is True]
    except (KeyError, TypeError) as error:
        raise Unavailable("incomplete GitHub timeline") from error
    if merged:
        proof["merged_pull"] = canonical_url(merged[0])
        return {"state": "satisfied", "proof": proof}
    if more:
        # A second page is not read: an unread reference may be the merged one.
        raise Unavailable("issue cross-references exceed one page")
    return {"state": "pending", "proof": proof}


def dependency(client, configured):
    """A declared dependency and proof; missing exact release inputs stay unknown."""
    result = {"dependency": configured, "state": "unknown", "proof": {}}
    try:
        kind = configured["kind"]
        if kind == "issue":
            result.update(_issue_dependency(client, configured))
            return result
        pull_url = configured.get("url") if kind == "merge" else configured.get("contains_pull")
        owner, repo, number = pull_parts(pull_url or "")
        detail = client.get(f"/repos/{owner}/{repo}/pulls/{number}")
        if type(detail.get("merged")) is not bool:
            raise Unavailable("dependency merge state unavailable")
        if not detail["merged"]:
            result["state"] = "pending"
            return result
        merged = _sha(detail.get("merge_commit_sha"))
        proof = {"url": pull_url, "merged": True, "merge_commit_sha": merged}
        if kind == "release":
            if configured.get("repo") != f"{owner}/{repo}" or not configured.get("tag"):
                raise Unavailable("exact release repository/tag is required")
            prefix = f"/repos/{owner}/{repo}"
            tag = configured["tag"]
            encoded = quote(tag, safe="")
            release = client.get(f"{prefix}/releases/tags/{encoded}", missing=True)
            if release is None or release.get("draft") is True:
                result["state"] = "pending"
                return result
            if release.get("tag_name") != tag or type(release.get("draft")) is not bool or parse_iso(release.get("published_at")) is None:
                raise Unavailable("release publication identity unavailable")
            ref_path = f"{prefix}/git/ref/tags/{encoded}"
            ref = client.get(ref_path)
            obj = ref["object"]
            for _ in range(8):
                if obj.get("type") == "commit":
                    break
                if obj.get("type") != "tag":
                    raise Unavailable("release tag does not resolve to a commit")
                obj = client.get(f"{prefix}/git/tags/{_sha(obj.get('sha'))}")["object"]
            commit = _sha(obj.get("sha"))
            if obj.get("type") != "commit":
                raise Unavailable("release tag peeling limit exhausted")
            comparison = client.get(f"{prefix}/compare/{merged}...{commit}")
            if comparison.get("status") not in {"ahead", "identical", "behind", "diverged"} or not comparison.get("merge_base_commit", {}).get("sha"):
                raise Unavailable("ancestry comparison is incomplete")
            if comparison.get("status") not in {"ahead", "identical"} or comparison.get("merge_base_commit", {}).get("sha") != merged:
                result["state"] = "pending"
                return result
            proof.update(repo=f"{owner}/{repo}", contains_pull=pull_url, published=True, ancestor=True,
                         tag=tag, tag_commit=commit, release_id=release["id"], comparison_status=comparison["status"])
            package = configured.get("package")
            if "package" in configured:
                name, version = package.get("name"), package.get("version")
                if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
                    raise Unavailable("exact package name/version is required")
                published = client.get(f"https://pypi.org/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json", missing=True)
                if published is None:
                    result["state"] = "pending"
                    return result
                info, files = published.get("info", {}), published.get("urls")
                def normalize(text):
                    return re.sub(r"[-_.]+", "-", text).lower()
                if info.get("version") != version or normalize(info.get("name", "")) != normalize(name) or not isinstance(files, list):
                    raise Unavailable("package publication identity mismatch")
                usable = [{"filename": row["filename"], "sha256": row["digests"]["sha256"], "yanked": False,
                           "upload_time": row["upload_time_iso_8601"]} for row in files
                          if row.get("yanked") is False and re.fullmatch(r"[0-9a-f]{64}", row.get("digests", {}).get("sha256", ""))]
                if not usable:
                    result["state"] = "pending"
                    return result
                proof["package"] = {"name": name, "version": version, "files": usable}
            if client.get(ref_path) != ref:
                raise Unavailable("release tag changed during collection")
        elif kind != "merge":
            raise Unavailable("unsupported remote dependency kind")
        result.update(state="satisfied", proof=proof)
    except (Unavailable, KeyError, TypeError, AttributeError) as error:
        result["reason"] = str(error) if isinstance(error, Unavailable) else "malformed dependency API response"
    return result
