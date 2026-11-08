"""Issues that need the operator, filed as report alerts after a shadow sync (sd:8142).

`pr_attention`'s alert flow over issues: one report per issue and state through `reporting.ingest`, a state that
persists files once, a state that clears acknowledges its alert. `read` finds the states.

    state            means                                          read from
    assigned         an open issue assigned to the operator         the `shadow` rows, GitHub and Jira, every repo
    mentioned        a GitHub @mention since the operator's reply   a `mentions:@me` search, issue body and comments
    jira_mentioned   a Jira comment @mention since their reply      `shadow_jira.mentions`

A shadow row's `reason` holds assigned, filed or watched and no mention, so mentions read live. A team mention and a
watcher-only row match neither read. An assignment already open at the first read is not new: it is recorded and
files nothing. A wait is a wait however old, so a mention files on the first read too.

    failure                      result
    a mention read fails or cut  that state is in `unread`: what it read files, none of that state clears or is forgotten
    Jira is not configured       `jira_mentioned` is unread, with no complaint: a machine without Jira is not broken
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from . import pr_attention, shadow_jira
from .shadow_sync import _run

JOB = "issue-attention"
SEEN_KEY = "issue-attention:seen"
PAGE = 100
#: The newest comments an issue is read for: a mention further back than this is not a live wait.
COMMENTS = 100
QUERY = """query($q: String!, $n: Int!) { viewer { login } search(query: $q, type: ISSUE, first: $n) { issueCount nodes {
  ... on Issue { url number title body author { login } repository { nameWithOwner }
    comments(last: %d) { nodes { body author { login } } } } } } }""" % COMMENTS
SEARCH = "is:issue is:open archived:false mentions:@me"
LABELS = {"assigned": "assigned to you", "mentioned": "you are mentioned", "jira_mentioned": "you are mentioned"}


def _add(found: dict[str, dict], state: str, row: dict) -> None:
    number = row.get("number")
    found[f"{state} {row['url']}"] = {
        "state": state, "url": row["url"], "number": number, "title": row.get("title") or "", "repo": row.get("repo") or "",
        "name": f"{row.get('repo')}#{number}" if number else row["url"].rsplit("/", 1)[-1]}


def _well_formed(node) -> bool:
    """Whether a search node holds what `_waits` reads; a node short of a field is no evidence its mention cleared."""
    if not isinstance(node, dict) or not all(isinstance(node.get(key), str) for key in ("url", "title", "body")):
        return False
    comments = (node.get("comments") or {}).get("nodes")
    return bool(node["url"]) and type(node.get("number")) is int and isinstance(comments, list) \
        and isinstance((node.get("repository") or {}).get("nameWithOwner"), str) \
        and all(isinstance(entry, dict) and isinstance(entry.get("body"), str) for entry in comments)


def _waits(node: dict, login: str) -> bool:
    """Whether the newest thing the operator is named in comes after the last thing they wrote; the body is oldest."""
    named = re.compile(rf"(?<![\w/-])@{re.escape(login)}(?![\w/-])", re.IGNORECASE)
    for entry in [*reversed(node["comments"]["nodes"]), node]:
        if ((entry.get("author") or {}).get("login") or "").lower() == login.lower():
            return False
        if named.search(entry["body"]):
            return True
    return False


def _github(found: dict[str, dict], runner, timeout: float) -> str:
    """Add the GitHub mention states; the reason the read is short, or `""`."""
    code, out, err = _run(["gh", "api", "graphql", "-f", f"query={QUERY}", "-f", f"q={SEARCH}", "-F", f"n={PAGE}"],
                          runner, timeout=timeout)
    try:
        data = json.loads(out)["data"] if code == 0 else None
        block, login = data["search"], data["viewer"]["login"]
        count, listed = block["issueCount"], block["nodes"]
    except (ValueError, KeyError, TypeError):
        if code == 0:
            return "malformed search"
        return err.strip().splitlines()[0] if err.strip() else f"gh exited {code}"
    if type(count) is not int or not isinstance(listed, list) or not isinstance(login, str) or not login:
        return "malformed search"
    nodes = [node for node in listed if _well_formed(node)]
    for node in nodes:
        if _waits(node, login):
            _add(found, "mentioned", {"url": node["url"], "number": node["number"], "title": node["title"],
                                      "repo": node["repository"]["nameWithOwner"]})
    if len(nodes) < len(listed):
        return f"{len(listed) - len(nodes)} malformed issue(s)"
    return f"more than {PAGE} open issues" if count > len(nodes) else ""


def _assigned(connection: sqlite3.Connection) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for row in connection.execute("SELECT repo, number, url, title FROM shadow WHERE kind = 'issue' AND state = 'open' "
                                  "AND reason = 'assigned' ORDER BY url"):
        _add(found, "assigned", dict(row))
    return found


def read_github(connection: sqlite3.Connection, runner=None, *, timeout: float = 60) -> tuple[dict[str, dict], set[str], str]:
    """`({basis: issue}, unread states, reason)` for a GitHub sync: the assignments and the GitHub mentions."""
    found = _assigned(connection)
    short = _github(found, runner, timeout)
    return found, {"jira_mentioned", "mentioned"} if short else {"jira_mentioned"}, f"GitHub mentions: {short}" if short else ""


def read_jira(connection: sqlite3.Connection, *, deadline: float | None = None, transport=None,
              environ=None) -> tuple[dict[str, dict], set[str], str]:
    """`({basis: issue}, unread states, reason)` for a Jira sync, whose `transport` seam keeps tests off the network."""
    found = _assigned(connection)
    rows, complete, short = shadow_jira.mentions(transport=transport, environ=environ, deadline=deadline)
    for row in rows:
        _add(found, "jira_mentioned", row)
    return found, {"mentioned"} if complete else {"mentioned", "jira_mentioned"}, f"Jira mentions: {short}" if short else ""


def alert(connection: sqlite3.Connection, found: dict[str, dict], unread: set[str], *, now: datetime) -> dict:
    """File each new state and acknowledge each cleared one, leaving `unread` states as they stand."""
    first = connection.execute("SELECT 1 FROM state WHERE kind = 'heartbeat' AND key = ? LIMIT 1", (SEEN_KEY,)).fetchone() is None
    quiet = {basis for basis, issue in found.items() if issue["state"] == "assigned"} if first else set()
    return pr_attention.alert(connection, found, True, now=now, job=JOB, seen_key=SEEN_KEY, labels=LABELS,
                              unread=unread, quiet=quiet)
