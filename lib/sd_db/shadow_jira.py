"""The Jira tracker: the same five-key contract, a completely different service.

Ported from the system dashboard's `jira_search`/`jira_me`/`collect_jira`, which
have been answering this question for months. Three things are carried over
verbatim because each was learned the hard way, and three are deliberately
changed. Both lists are here so the next reader can tell which is which.

**Carried over.**

* `myself` is the availability check, not the search. Bad credentials do not
  make a JQL search fail -- Jira answers 200 with `{"issues": [], "isLast":
  true}`, because `currentUser()` resolves to anonymous, who is assigned
  nothing. So the search cannot tell "your credentials are wrong" from "you have
  no open issues", and something else has to say which before it is believed.
* The account id, not the email address, is what issues are matched against.
  Jira hides `emailAddress` on any site with the privacy setting on, and an
  empty email compared against an empty email marks every issue as both assigned
  and filed.
* `watches.isWatching` is read rather than derived. Jira has already computed it
  for the authenticated user, so it stays correct even if someone points the JQL
  at a queue that is not theirs.

**Changed, and why.**

* **No default base URL.** The system dashboard falls back to a specific
  Atlassian host. This backbone must carry no employer footprint of any kind, so
  an unset `JIRA_BASE_URL` is "not configured" and never a guess. That is also
  the honest behaviour: a default host silently pointed at the wrong tenant is
  worse than a row saying which variable to set.
* **An https base URL or none.** Every request carries the token in a Basic
  `Authorization` header, so a base URL with any other scheme -- an `http://`
  typo included -- is refused as not configured, before any request, and the
  reason names the scheme and never the value. There is no localhost
  exception in the code; the tests that drive the real transport against
  127.0.0.1 servers replace `settings` to get past the check.
* **No credentials and no stray text in the authority.** A base URL with a
  `user:password@` part, with a port that is not a number from 1 to 65535, or
  that `urllib.parse` cannot split, is refused the same way. Otherwise
  `http.client` raises `InvalidURL` with the text after the last colon -- a
  password pasted into the URL -- in its message, and `urllib.parse` raises
  `ValueError` quoting the authority when it holds look-alike punctuation
  (a fullwidth `：` or `＠`) or a bracketed host that is not an address. The
  check reads the authority as `urllib` does, percent-decoded, so `%40` is an
  `@`, and then refuses any `%` left in it, so the value kept is the value
  checked. And the host must be a DNS name, an IPv4 address or a bracketed IPv6
  address with no zone: `urllib.parse` also admits `[v1.user:password]`, a
  zone such as `[fe80::1%25user:password]`, a second colon and a
  twice-encoded `%2540`, and each hands that text to the resolver, to a proxy
  and to a certificate-mismatch message that the reason keeps. The host
  must also be ASCII once decoded, and the reason says to write an
  international name in its `xn--` form. `http.client` writes the `Host`
  header as Latin-1, so `bücher` went out with a raw `0xFC` byte beside an
  SNI of `xn--bcher-kva`. And the stdlib `idna` codec that would convert it is IDNA
  2003, which names more than a thousand code points differently from IDNA
  2008 (`faẞ` is `fass` to it and `xn--fa-hia` to IDNA 2008), so a converted
  host could carry the token to a name the operator did not mean.
* **No redirect is followed.** `urllib`'s redirect handler copies every header,
  `Authorization` included, into the request it follows, to whatever host and
  scheme `Location` names -- so an https check alone would still hand the token
  on. A 30x is reported as a failed collect that names the redirect.
* **A transport failure is a failed collect, never an exception.** The
  request is built inside the `try`, and every `http.client.HTTPException`
  (`InvalidURL`, `BadStatusLine`, `IncompleteRead`, `RemoteDisconnected` and
  the rest) and every `ValueError` is caught with a reason that names the
  exception type alone, because their messages quote the URL or the bytes
  read. So `sync` still writes the heartbeat and its request count. A
  `URLError` or other `OSError` keeps its message, which says why the
  connection failed, unless the `URLError` wraps one of those exceptions;
  an `HTTPError` keeps Jira's reason phrase.
* **The window replaces the open-only filter.** The system dashboard's JQL asks
  for open issues, because it renders a worklist. An index wants the opposite:
  if closed issues never appear, a ticket that closes simply stops matching and
  its row keeps saying `open` forever. So the default JQL selects by involvement
  and recency, and lets the store record the close.
* **The window is expressed in relative minutes, never as a timestamp.** JQL
  date literals are interpreted in the *Jira account's* configured timezone,
  which this machine has no way to know; a `-90m` offset has no timezone to get
  wrong. This is the one place the port would have been a defect rather than a
  copy.
* **A window past the page ceiling is split, not restarted.** Truncated, the
  collect is not `ok` and the watermark stays put, so a first window with more
  issues than one walk returns would be asked again whole on every run.
  `search_window` halves it until each part fits, within `MAX_REQUESTS`.

Credentials come from the environment and nowhere else. This module never reads
another project's `.env`: standing rule 1 says the framework does not touch repo
files for its own purposes, and a credentials file belonging to a different
repository is the last place to make an exception.

**How it got here.** This is the pack's `dashboard/jira.py`, lifted whole on the
2026-09-12 decision on sd:361 -- the second tracker is built, not closed. Four
things changed and nothing else: `collect` returns `Collected` rather than the
pack's dict, so `sync` reads the same shape from both trackers; the timestamp
helpers are `shadow_sync`'s `iso` and `parse_iso`, because there is no
`dashboard` package here to import; `ok` folds truncation in, matching
`Collected`'s own rule, so the caller's watermark guard on `ok` alone is sound;
and `fetch_issue` stayed behind in the pack, where `sd-trackers ref` calls it.
Two more followed on review of the dispatch that made this module reachable:
the https-only base URL and the refused redirects above, and
`Collected.requests`, which counts every attempt, a failed one included --
even one that never reached a socket -- so the heartbeat reports Jira's traffic.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .shadow_sync import Collected, iso, parse_iso, reason_of

TRACKER = "jira"
TIMEOUT_SECONDS = 30
PAGE_SIZE = 50
# Ten pages, matching the GitHub collector's ceiling for the same reason: the
# steady-state window is minutes wide, and this exists for the first collect.
MAX_PAGES = 10
# Search requests per collect, split windows included. A window past the page
# ceiling is split in two until each half fits, so a first collect with a few
# thousand issues still finishes; this bounds the one that never would.
MAX_REQUESTS = 200
OVERLAP = timedelta(hours=1)
FIRST_RUN_WINDOW = timedelta(days=90)

FIELDS = [
    "summary",
    "status",
    "updated",
    "created",
    "assignee",
    "reporter",
    "project",
    "watches",
]

# Involvement plus recency. Deliberately *not* filtered to open issues -- see
# the module docstring. `{window}` is filled by `window_clause`.
DEFAULT_JQL = (
    "(assignee = currentUser() OR watcher = currentUser() "
    "OR reporter = currentUser()) "
    "AND {window} ORDER BY updated DESC"
)

# The open issues whose comments name the operator, whatever their involvement.
MENTION_JQL = "comment ~ currentUser() AND statusCategory != Done ORDER BY updated DESC"
# One comment read per issue, so a bound on the requests a mention read spends.
MENTION_READS = 50

ENV_BASE = "JIRA_BASE_URL"
ENV_EMAIL = "JIRA_EMAIL"
ENV_TOKEN = "JIRA_API_TOKEN"
ENV_JQL = "JIRA_JQL"


def settings(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment's Jira settings, trimmed. Values are never reported.

    `JIRA_BASE_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN` are required, and
    `missing` names whichever is absent. `JIRA_JQL` is optional: set, it
    replaces `DEFAULT_JQL` whole, window included, so `collect` neither
    windows nor splits it.

    A base URL that is not https, or whose authority carries a user, a port
    that is not a number or a host that is not a plain name or address, is
    dropped, so `missing` names it like an unset one, and `refused` says why
    in words that carry the scheme alone.
    """
    env = os.environ if environ is None else environ
    base = (env.get(ENV_BASE) or "").strip().rstrip("/")
    refused = ""
    if base:
        scheme = base.split("://", 1)[0].lower() if "://" in base else ""
        if scheme != "https":
            # Only a scheme-shaped prefix is echoed: without `://`, a value
            # such as `host:8080` would put part of the host in the reason.
            named = scheme if re.fullmatch(r"[a-z][a-z0-9+.-]{0,15}", scheme) else ""
            refused = (f"{ENV_BASE} must be an https URL, not {named}" if named
                       else f"{ENV_BASE} must be an https URL")
            base = ""
        else:
            refused = _authority_refusal(base)
            if refused:
                base = ""
    return {
        "base": base,
        "email": (env.get(ENV_EMAIL) or "").strip(),
        "token": (env.get(ENV_TOKEN) or "").strip(),
        "jql": (env.get(ENV_JQL) or "").strip(),
        "refused": refused,
    }


def _authority_refusal(base: str) -> str:
    """Why the authority of an https `base` is refused, or "" when it is not.

    Read the way `urllib.request.Request` reads it: everything after `//` up
    to the first `/`, `?` or `#`, then percent-decoded. `urllib.parse.urlsplit`
    must accept both the value and the decoded authority, because `Request`
    splits the one and `http.client` is handed the other, and its `ValueError`
    quotes what it refused. The port rule is `http.client`'s own split -- the
    text after the last `:` that follows any `]` -- held to 1-65535, where
    `http.client` would raise `InvalidURL` with that text in its message.
    What is left before the port is the host the resolver is asked for, so it
    must be ASCII -- an IDN is written in its `xn--` form, and the reason
    says so -- and a DNS name or IPv4 address, or an IPv6 address in
    brackets, and no `%` may survive the decoding: a zone id, or a second
    encoding that `http.client` never decodes. Nor may a `%` stand in the
    value itself: `config["base"]` keeps the encoded text, and the browse
    URLs `normalize` stores are built from it, so the value checked must be
    the value used. The reason never quotes the value.
    """
    raw = re.split(r"[/?#]", base.split("://", 1)[1], maxsplit=1)[0]
    authority = urllib.parse.unquote(raw)
    try:
        urllib.parse.urlsplit(base)
        urllib.parse.urlsplit("https://" + authority)
    except ValueError:
        return f"{ENV_BASE} is not a URL urllib can split"
    if "@" in authority:
        return f"{ENV_BASE} must not carry a user name or password"
    colon, bracket = authority.rfind(":"), authority.rfind("]")
    host, port = (authority[:colon], authority[colon + 1:]) if colon > bracket else (authority, "")
    # Five digits at most before `int`, which refuses 4300 or more.
    if port and not (re.fullmatch(r"[0-9]{1,5}", port) and 1 <= int(port) <= 65535):
        return f"{ENV_BASE} must have a port from 1 to 65535"
    if not host.isascii():
        return f"{ENV_BASE} must have an ASCII host; write an international name in its xn-- form"
    if "%" in raw or not _plain_host(host):
        return f"{ENV_BASE} must have a host that is a DNS name, an IPv4 address or a bracketed IPv6 address"
    return ""


def _plain_host(host: str) -> bool:
    """Whether `host` is a DNS name, an IPv4 address or `[` IPv6 `]`.

    `_authority_refusal` has already refused a host that is not ASCII. A name
    is passed through the `idna` codec `socket` and `http.client` encode
    with, which refuses an empty label and holds a label to 63 characters but
    not the name to DNS's 253, so the 253 is checked here. Brackets hold an
    address `ipaddress` accepts, which is never `urllib.parse`'s `v1.` form.
    """
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return False
        return True
    try:
        name = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return (len(name.rstrip(".")) <= 253
            and re.fullmatch(r"(?:[A-Za-z0-9_-]+\.)*[A-Za-z0-9_-]+\.?", name) is not None)


def missing(config: dict[str, str]) -> list[str]:
    """Which variables are absent, **by name**.

    Names and presence only, never values -- the whole point of reporting this
    at all is to tell an operator what to set without the report itself
    becoming somewhere a credential can be read.
    """
    return [
        name
        for name, key in ((ENV_BASE, "base"), (ENV_EMAIL, "email"), (ENV_TOKEN, "token"))
        if not config[key]
    ]


def window_start(watermark: str | None, now: datetime) -> datetime:
    """Same rule as the GitHub collector: overlap on a mark, wide on nothing.

    The *parser* is shared -- `shadow_sync.parse_iso` reads back exactly what
    `shadow_sync.iso` wrote, and both trackers store their watermark in that one
    spelling, so a second implementation of it could only ever drift. The
    *constants* are deliberately not shared: `OVERLAP` and `FIRST_RUN_WINDOW`
    are declared in each module, so widening one tracker's window is a decision
    about that tracker rather than a change that silently moves the other.
    """
    previous = parse_iso(watermark) if watermark else None
    if previous is None:
        return now - FIRST_RUN_WINDOW
    return previous - OVERLAP


def window_minutes(start: datetime, now: datetime) -> int:
    """The window as whole minutes, rounded up and never below one.

    Rounded up because truncation would shave the oldest edge off the window,
    which is precisely the overlap that exists to stop an issue falling between
    two collects.
    """
    seconds = max((now - start).total_seconds(), 60.0)
    return int(math.ceil(seconds / 60.0))


def _request(url: str, config: dict[str, str], body: dict | None, transport=None):
    """One authenticated call. Returns (payload, error-sentence).

    `transport` is the test seam: a callable taking (url, headers, body) and
    returning a payload dict, so nothing in the suite opens a socket.
    """
    auth = base64.b64encode(
        f"{config['email']}:{config['token']}".encode()
    ).decode()
    headers = {"Authorization": "Basic " + auth, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    return (transport if transport is not None else _http)(url, headers, body)


# The codes `urllib`'s redirect handler follows. Any other 3xx (300, 304, 305)
# reaches the error path without a follow being attempted, and so does one of
# these without a `Location` or `URI` header.
_FOLLOWED = tuple(code for code in range(300, 400)
                  if hasattr(urllib.request.HTTPRedirectHandler, f"http_error_{code}"))


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Every 30x stays an error; nothing is sent to the `Location` it names.

    Refusing rather than filtering: a same-host rule has to parse and compare
    hosts, ports and schemes correctly to be safe, and a base URL that redirects
    is a configuration to correct, which the reason says.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(url: str, headers: dict[str, str], body: dict | None):
    """The real transport: the seam's signature, over `urllib`."""
    try:
        # Inside the `try`: `Request` splits the URL, and `urllib.parse`
        # raises a `ValueError` that quotes the authority.
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        # Built per call, as `urlopen` builds its default, so proxy settings
        # come from the environment at request time rather than at import.
        opener = urllib.request.build_opener(_RefuseRedirects)
        # nosec B310: `collect` builds `url` from `settings()`, which admits an
        # https base only, and `_RefuseRedirects` keeps it on that URL.
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            return json.loads(response.read().decode()), ""
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            return None, (
                f"Jira rejected the credentials ({error.code} {error.reason}); "
                f"check {ENV_EMAIL} and {ENV_TOKEN}"
            )
        if error.code in _FOLLOWED and error.headers is not None and (
            "location" in error.headers or "uri" in error.headers
        ):
            return None, (
                f"Jira redirected the request ({error.code} {error.reason}) and redirects "
                f"are not followed with credentials; set {ENV_BASE} to the URL Jira answers on"
            )
        return None, f"Jira returned {error.code} {error.reason}"
    except (http.client.HTTPException, ValueError) as error:
        # Type only: `InvalidURL` and `urllib.parse`'s `ValueError` quote the
        # host, a secret pasted into it included, and `IncompleteRead` quotes
        # the bytes it did read. Before the `OSError` clause, because
        # `RemoteDisconnected` is both.
        return None, f"the request to Jira failed: {type(error).__name__}"
    except (urllib.error.URLError, OSError) as error:
        # `URLError` quotes its reason, and that can be one of the exceptions
        # above, caught inside `urllib` and wrapped: type only for it too.
        reason = getattr(error, "reason", None)
        if isinstance(reason, (http.client.HTTPException, ValueError)):
            return None, f"the request to Jira failed: {type(reason).__name__}"
        return None, f"could not reach Jira: {type(error).__name__}: {error}"


def account_id(config: dict[str, str], transport=None) -> tuple[str, str]:
    """(accountId, error). The availability check -- see the module docstring."""
    payload, error = _request(config["base"] + "/rest/api/3/myself", config, None, transport)
    if error:
        return "", error
    found = (payload or {}).get("accountId") or ""
    return found, "" if found else "Jira accepted the request but named no account"


def search(jql: str, config: dict[str, str], transport=None) -> tuple[list[dict], bool, str]:
    """Paged JQL. Returns (issues, truncated, error-sentence).

    The endpoint reports neither a total nor a page count -- only `isLast` and a
    `nextPageToken` -- so the ceiling is what stops this, not a number the
    server hands back. Hitting it is reported, never swallowed.
    """
    url = config["base"] + "/rest/api/3/search/jql"
    found: list[dict] = []
    token: str | None = None
    for _ in range(MAX_PAGES):
        body: dict = {"jql": jql, "maxResults": PAGE_SIZE, "fields": FIELDS}
        if token:
            body["nextPageToken"] = token
        payload, error = _request(url, config, body, transport)
        if error:
            return found, False, error
        page = payload or {}
        found.extend(page.get("issues") or [])
        # A boolean, and present: a truthy `"false"` read as the end would drop
        # the remainder while reporting a complete walk.
        if type(page.get("isLast")) is not bool:
            return found, False, "Jira answered with malformed pagination"
        if page["isLast"]:
            return found, False, ""
        token = page.get("nextPageToken")
        # `isLast: false` with no token to reach the next page is a malformed
        # answer, not the end of the results. Reading it as the end would drop
        # the remainder while reporting a complete walk -- the same failure the
        # GitHub collector already guards against, and the reason both say so
        # out loud rather than returning a bare False. An error as well, so
        # `search_window` does not read it as a window to split and ask again.
        if not token:
            return found, True, "Jira answered with malformed pagination"
    return found, True, ""


def window_clause(older: int, newer: int) -> str:
    """`updated` from `older` minutes ago to `newer` minutes ago, both inclusive.

    A `newer` of zero is no newer edge at all: the window runs to Jira's now.
    """
    if newer <= 0:
        return f"updated >= -{older}m"
    return f"updated >= -{older}m AND updated <= -{newer}m"


def search_window(minutes: int, config: dict[str, str], transport, clock=time.monotonic):
    """`DEFAULT_JQL` over the last `minutes`, split until every part fits.

    Returns (issues, truncated, error-sentence), as `search` does. A window
    that hits the page ceiling is halved and each half asked again, so a
    first collect larger than one walk still ends covered, instead of being
    reported truncated and asked again whole on every run. Halving stops at
    one minute and at `MAX_REQUESTS`, and either leaves the collect truncated.

    The edges are relative minutes, which Jira resolves against its own now
    when each request arrives, so a half asked later covers a later span. Its
    older edge is widened by the minutes since the first request, plus one for
    the difference in latency, so no span between two halves goes unasked.
    """
    started = clock()
    spent = 0

    def budgeted(url, headers, body):
        nonlocal spent
        if spent >= MAX_REQUESTS:
            return None, f"the Jira collect reached its request limit of {MAX_REQUESTS}"
        spent += 1
        return transport(url, headers, body)

    found: dict[str, dict] = {}
    truncated = False
    pending = [(0, minutes)]
    while pending:
        newer, older = pending.pop()
        widen = 0 if spent == 0 else math.ceil((clock() - started) / 60) + 1
        jql = DEFAULT_JQL.format(window=window_clause(older + widen, newer))
        raw, cut, error = search(jql, config, budgeted)
        for item in raw:
            found[item.get("key") or f"#{len(found)}"] = item
        if error:
            return list(found.values()), truncated or cut, error
        if cut and older - newer > 1:
            middle = (newer + older) // 2
            pending.extend([(newer, middle), (middle, older)])
        elif cut:
            truncated = True
    return list(found.values()), truncated, ""


def state_of(fields: dict) -> str:
    """open/closed from the status *category*, shared by both readers.

    Extracted rather than repeated: `normalize` (the collector) and
    `fetch_issue` (the reference lookup, which stayed in the pack) must agree
    about what closed means, and two copies of the same rule is exactly how
    they would stop agreeing.
    """
    status = fields.get("status") or {}
    category = ((status.get("statusCategory") or {}).get("name") or "").lower()
    return "closed" if category == "done" else "open"


def normalize(raw: dict, config: dict[str, str], me: str) -> dict | None:
    """One Jira issue as a shadow row, or None when it has no key.

    `state` collapses to open/closed from the status *category*, which Jira
    derives itself, so it catches Done, Closed, Cancelled and whatever else a
    project calls its end state. The status name is `status` (migration 40,
    pack sd:5496), None when it does not read; `filed` is read from this
    issue's own reporter, apart from `reason`, and `assignee` is `""` when no
    one holds it. A person who cannot be read is None for both.
    """
    key = (raw.get("key") or "").strip()
    if not key:
        return None
    fields = raw.get("fields") or {}

    def person(name: str) -> dict:
        value = fields.get(name) or {}
        return {
            "id": value.get("accountId") or "",
            "name": value.get("displayName") or "",
            "email": (value.get("emailAddress") or "").lower(),
        }

    def is_me(who: dict) -> bool:
        # The id when both sides have one; the email only as a fallback, and
        # never when either side is empty -- an empty-equals-empty comparison
        # marks every issue as yours.
        if me and who["id"]:
            return who["id"] == me
        return bool(who["email"]) and who["email"] == config["email"].lower()

    def known(name: str) -> bool:
        # A null field is no one; a field that did not read, or a person with
        # no id or email to match, cannot say whether it is the operator.
        who = person(name)
        return name in fields and (fields[name] is None or bool(me and who["id"]) or bool(who["email"]))

    assignee, reporter = person("assignee"), person("reporter")
    why = []
    if is_me(assignee):
        why.append("assigned")
    if is_me(reporter):
        why.append("filed")
    if (fields.get("watches") or {}).get("isWatching"):
        why.append("watching")
    return {
        "tracker": TRACKER,
        "url": f"{config['base']}/browse/{key}",
        "repo": (fields.get("project") or {}).get("key") or "",
        "number": None,
        "kind": "issue",
        "title": fields.get("summary") or "",
        "state": state_of(fields),
        "author": reporter["name"],
        "updated_at": fields.get("updated") or "",
        "created_at": fields.get("created") or "",
        "status": (fields.get("status") or {}).get("name") or None,
        "assignee": (None if "assignee" not in fields else "" if fields["assignee"] is None
                     else assignee["name"] or None),
        "filed": True if "filed" in why else False if known("reporter") else None,
        # `matched` rather than an empty list: the JQL selected it for some
        # reason, and a row with no reason at all reads as a collector bug
        # rather than as "your JQL is wider than the three named cases".
        "why": why or ["matched"],
        # From this issue's own fields, so no failed search can leave it unknown.
        "reason": reason_of(why or ["matched"]),
    }


def _names(node, account: str) -> bool:
    """Whether a comment's document body holds a mention of `account`."""
    if not isinstance(node, dict):
        return False
    if node.get("type") == "mention" and (node.get("attrs") or {}).get("id") == account:
        return True
    children = node.get("content")
    return isinstance(children, list) and any(_names(child, account) for child in children)


def mentions(*, transport=None, environ=None, deadline: float | None = None) -> tuple[list[dict], bool, str]:
    """`(rows, complete, reason)`: open issues with a comment @mention since the operator's last comment (sd:8142).

    Not configured is `([], False, "")`: nothing was read, and nothing is wrong. Any other short read names
    why, and keeps the rows it reached. A failed search or comment read is never "no mention".
    """
    config = settings(environ)
    if missing(config):
        return [], False, ""
    me, error = account_id(config, transport)
    if error:
        return [], False, error
    raw, cut, error = search(MENTION_JQL, config, transport)
    if error or cut:
        return [], False, error or "the search was cut"
    if len(raw) > MENTION_READS:
        return [], False, f"more than {MENTION_READS} open issues name the operator"
    rows = []
    for item in raw:
        row = normalize(item, config, me)
        if not row:
            continue
        if deadline is not None and time.monotonic() >= deadline:
            return rows, False, "out of time"
        url = f"{config['base']}/rest/api/3/issue/{urllib.parse.quote(item['key'])}/comment?orderBy=-created&maxResults=100"
        payload, error = _request(url, config, None, transport)
        comments = (payload or {}).get("comments")
        if error or not isinstance(comments, list):
            return rows, False, error or "Jira answered with no comment list"
        for comment in comments:
            if not isinstance(comment, dict):
                return rows, False, "Jira answered with a malformed comment"
            if ((comment.get("author") or {}).get("accountId")) == me:
                break
            if _names(comment.get("body"), me):
                rows.append(row)
                break
    return rows, True, ""


def collect(watermark: str | None, now: datetime, seam=None, environ=None, *,
            clock=time.monotonic) -> Collected:
    """Every involved issue touched inside the window.

    A set `JIRA_JQL` has no window, so `window_start` is empty and the caller
    holds the cursor: a run of it covers no span a default run could resume
    from (pack sd:5376).

    `clock` is the test seam for the time `search_window` widens a later
    half by.
    """
    config = settings(environ)
    start = window_start(watermark, now)
    covered = "" if config["jql"] else iso(start)
    absent = missing(config)
    if absent:
        reasons = []
        if config["refused"]:
            reasons.append(config["refused"])
            absent = [name for name in absent if name != ENV_BASE]
        if absent:
            # "A, B and C" rather than "A and B and C": three variables are the
            # common case on a machine that has never configured Jira, and the
            # naive join reads like a bug in the sentence.
            named = (
                absent[0]
                if len(absent) == 1
                else f"{', '.join(absent[:-1])} and {absent[-1]}"
            )
            reasons.append(f"{named} not set")
        return Collected(
            ok=False,
            reason="; ".join(reasons),
            issues=[],
            truncated=[],
            window_start=covered,
            window_end=iso(now),
        )
    spent = 0

    def counted(url, headers, body):
        # Counted before the call, so every attempt counts, one that failed
        # before reaching a socket included.
        nonlocal spent
        spent += 1
        return (seam if seam is not None else _http)(url, headers, body)

    me, error = account_id(config, counted)
    if error:
        return Collected(
            ok=False,
            reason=error,
            issues=[],
            truncated=[],
            window_start=covered,
            window_end=iso(now),
            requests=spent,
        )
    if config["jql"]:
        raw, cut, error = search(config["jql"], config, counted)
    else:
        raw, cut, error = search_window(window_minutes(start, now), config, counted, clock)
    rows = [row for row in (normalize(item, config, me) for item in raw) if row]
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    # `ok` folds truncation in, where the pack set it from `not error` alone and
    # reported `truncated` beside it. The pack's reader looked at `ok` only, and
    # never advanced a cursor over a truncated page because `truncated` was
    # reported rather than guarded. `Collected` is `ok=not errors and not
    # truncated`, so the caller's watermark guard needs only `ok` and a
    # non-empty `window_start` here.
    return Collected(
        ok=not error and not cut,
        reason=error,
        issues=rows,
        truncated=["jql"] if cut else [],
        window_start=covered,
        window_end=iso(now),
        requests=spent,
    )
