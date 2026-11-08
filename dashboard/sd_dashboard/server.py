"""The server: explicit listeners and an authentication policy on every response.

Task controls use an expiring browser session bound to its origin and verified
operator. Remote requests require the explicitly configured private Tailscale
Serve boundary to be revalidated on every request; headers cannot select it.

* **Every** response carries
  `Content-Security-Policy: default-src 'self'; script-src 'self';
  object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors
  'none'` and `X-Frame-Options: DENY` -- error pages, the 404, the stylesheet
  and the JavaScript file included. It is set in one place, `end_headers`, so
  a route added later cannot forget it.
* **No page may be framed**, which is the last directive above and the older
  header beside it. That is not a content rule: it is what stops a page on
  another origin of the same tailnet site from putting an overlay over a real
  control (`prd.md:706-716`).
* The primary socket binds loopback for Tailscale Serve. Optional direct IP
  access binds one validated Tailscale address and authenticates its socket peer.

Connections come from `sd_db.connect`: reads open `write=False`, actions open
a writer only after the request and finite action vocabulary are validated.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import sd_db
from sd_db import workflow
from sd_db.database import schema_version
from sd_db.errors import SdDbError
from sd_db.schema import SCHEMA_VERSION

from . import auth, caching, runtime, screens
from .pages import error_page

__all__ = ["CSP", "Dashboard", "SECURITY_HEADERS", "build", "capture_followup", "main", "route"]

CSP = (
    "default-src 'self'; script-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

#: On every response. The tuple is what the test enumerates.
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)

STATIC = caching.STATIC

#: The one CSS file and the one JavaScript file, and no third. Criterion 12
#: reads this tuple and the directory listing and asserts they agree.
STATIC_FILES = caching.STATIC_FILES

DEFAULT_PORT = 8767
SESSION_SECONDS = 3600
#: Why a read without a valid page session is refused: a session lasts SESSION_SECONDS and a restart ends it, so the
#: page's own fix is a reload (sd:5664); reselecting a row reads with the same dead session.
SESSION_ENDED = "its session ended, as it does after an hour or when the dashboard restarts."
MAX_BODY = 65536
#: Requests that may hold a store connection for a read at once (sd:7842). Each open connection holds file handles
#: (the store, its -wal and -shm); with no bound, polls that piled up under load ran the process out of handles, and
#: the front door then refused every tailnet request. A read past the bound waits READ_WAIT seconds, then answers 503.
READ_SLOTS = 8
READ_WAIT = 20.0


class NotFound(Exception):
    """A path with no screen behind it."""


BUSY = f"The dashboard is busy: {READ_SLOTS} reads held the store for {READ_WAIT:g} seconds. Try again shortly."


def _unchecked(error):
    """The failure under a front-door refusal that means the check never ran: an `OSError` or a timeout, as the
    error itself or along its `from` chain. None for a real refusal (sd:7842)."""
    while error is not None:
        if isinstance(error, (OSError, subprocess.TimeoutExpired)):
            return error
        error = error.__cause__
    return None


def _metrics_kept(database):
    """Metrics' slow parts kept in a snapshot (sd:4955); each read opens its own read connection, off the request's."""
    from . import metrics_screen
    from .snapshot import Kept

    def read(name):
        connection = sd_db.connect(database, write=False)
        try:
            return metrics_screen.slow(connection, name, now=_now())
        finally:
            connection.close()

    return Kept({name: (lambda name=name: read(name)) for name in metrics_screen.SLOW})


def _usage_kept(database):
    """`/api/usage`'s current month, the 1.7 s `usage.read`, kept in a snapshot (sd:5064); each read opens its own connection."""
    from .snapshot import Snapshot
    from .usage_screen import document

    def read():
        connection = sd_db.connect(database, write=False)
        try:
            return document(connection, now=_now(), month=None)
        finally:
            connection.close()

    return Snapshot(read)


def _warm(database, kept) -> None:
    """Start the slow reads in the background so the first page after a start finds them (sd:5064): each kept read, and
    Health's walkers and doctor, as `health_screen.summary` starts them."""
    from . import health_screen

    for one in kept:
        one.warm()

    def health():
        try:
            connection = sd_db.connect(database, write=False)
            try:
                health_screen.summary(connection, now=_now(), fleet=kept[0])
            finally:
                connection.close()
        except Exception as failure:  # noqa: BLE001 - a warm-up that fails leaves the first page to read as before
            print(f"sd-dashboard: the Health warm-up did not start: {failure!r}", file=sys.stderr, flush=True)
    threading.Thread(target=health, name="sd-dashboard-warm", daemon=True).start()


def _piece_sweep(database) -> None:
    """One sweep of every writing piece's Drive mirror (pack sd:7243, ruling on note #43370), so a hand edit with no
    verb after it reaches the queue. `enqueue_pieces` queues only a file whose text changed, so the log names only a
    change; a sweep that cannot run says why and the next one tries again."""
    from . import publishing

    try:
        connection = sd_db.connect(database, write=False)
        try:
            lines = publishing.queue_piece(connection, None)
        finally:
            connection.close()
    except Exception as failure:  # noqa: BLE001 - unknown, never "nothing changed"; the next sweep retries
        print(f"sd-dashboard: the piece sweep did not run: {failure or type(failure).__name__}", file=sys.stderr, flush=True)
        return
    for line in lines:
        if "unchanged since" not in line:
            print(f"sd-dashboard: piece sweep: {line}", file=sys.stderr, flush=True)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def route(connection: sqlite3.Connection, path: str, parameters, *, now: str) -> str:
    """One request, one page. The router is a function so a test can call it.

    A test that has to start a socket to assert a page is a test that runs
    once and then flakes. Everything above `Dashboard` is pure: rows in,
    markup out.
    """
    from . import v2

    # The new design is the default (sd:2163): each registered page serves its routes, and an old screen whose path a
    # page took is served where its page module says (sd:2418). The old screens that are left keep their paths; the
    # ones whose controls moved were retired (sd:3402) and answer 404.
    found = v2.page(path)
    if found is not None:
        return found
    taken = v2.old(path)
    if taken is not None:
        return taken.render(v2.registry.Read(connection=connection, now=now, path=path, parameters=parameters))
    if path == "/protection":
        from .protection_screen import render

        return render(connection, parameters=parameters)
    if path == "/operations":
        # pack sd:7517: every area moved; the handler answers a moved one with a 301 before this, and the bare address
        # lists their homes. Any other area, such as one sd:3402 retired, is a 404.
        if parameters.get("area"):
            raise NotFound("/operations?area=" + parameters["area"][0])
        from .operations_screen import moved_page

        return moved_page()
    if path.startswith("/item/"):
        tail = path[len("/item/") :].strip("/")
        if not tail.isdigit():
            raise NotFound(f"no item {tail!r}")
        rendered = screens.item(connection, int(tail), now=now, parameters=parameters)
        if rendered is None:
            raise NotFound(f"no item {tail}")
        return rendered
    raise NotFound(path)


#: The one ref the dashboard files under (sd:4840): Today's issue intake names a GitHub issue, `github:<owner>/<repo>#N`.
GITHUB_REF = re.compile(r"github:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[1-9][0-9]{0,9}")
#: Health's Fix on a Dependabot row (sd:6685): one open item per repository, reused while it is open.
DEPENDABOT_REF = re.compile(r"dependabot:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
#: A Health doctor row's ref (pack sd:6684): `doc:<hash>` as `doctor:<hash>`, filed in the system repo, reused alike.
DOCTOR_REF = re.compile(r"doctor:[0-9a-f]{12}")
#: Today's Fix on a flaky head (pack sd:8113): `flaky:<owner>/<repo>:<head>`, reused while its item is open.
FLAKY_REF = re.compile(r"flaky:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+:[0-9a-f]{7,40}")
#: Today's Fix on a broken Needs you row with no fix of its own (pack sd:8113): `broken:<row id>`, reused alike.
BROKEN_REF = re.compile(r"broken:[\w.:/@#-]{1,200}")


def _reused(ref) -> bool:
    """A ref whose open item a second Fix answers instead of filing anew; a done one gives the ref up."""
    return isinstance(ref, str) and any(pattern.fullmatch(ref) for pattern in (DEPENDABOT_REF, DOCTOR_REF, FLAKY_REF,
                                                                                BROKEN_REF))


def capture_ref(connection, *, ref, who, **values):
    """A task filed under `ref`, as `sd task add --ref` files one (sd:4840, Today's issue intake).

    The pack's verb (`_capture` in its `bin/sd_work.py`) refuses a ref a row already carries, then runs
    `workflow.capture_task` and `writes.set_item_fields(source="task-ref", external_id=ref)` in one transaction; the
    same calls here. The intake read already leaves out an issue an item carries in any letter case, so this refusal
    is for a second click or an agent that filed the same ref first.
    """
    from sd_db import writes

    with workflow.transaction(connection):
        filed = writes.item_by_external(connection, "task-ref", ref)
        if filed is not None and _reused(ref):
            # A Dependabot ref names a repository, not one alert, and a doctor ref one finding (pack sd:6684): its open
            # item is reused, and a done one gives the ref up (`<ref>@<id>`), so the next Fix files anew under the
            # unique (source, external_id) index.
            if filed["status"] != "done":
                return {**workflow.item_state(connection, filed["id"]), "existing": True}
            writes.set_item_fields(connection, filed["id"], external_id=f"{ref}@{filed['id']}")
            filed = None
        if filed is not None:
            raise workflow.WorkflowError(f"sd:{filed['id']} ({filed['status']}) already carries --ref {ref}")
        state = workflow.capture_task(connection, who=who, **values)
        writes.set_item_fields(connection, state["item"]["id"], source="task-ref", external_id=ref)
        return workflow.item_state(connection, state["item"]["id"])


def capture_drafted(connection, capture, *, who):
    """A task the capture box files, and its draft, in one transaction (pack sd:6361, system sd:6013).

    `capture` files the item (`workflow.capture_task` or `capture_ref`);
    `draft.queue_if_eligible` then queues its draft when its repository is
    managed, has a remote and lands on the hub. A failure in either rolls
    both back, so no captured item misses its draft. A followup, or a task
    another path files, queues none (ruling #31096).
    """
    from sd_db import draft

    with workflow.transaction(connection):
        state = capture()
        item = state["item"]["id"]
        if draft.queue_if_eligible(connection, item, who=who) is None:
            return state
        return workflow.item_state(connection, item)


def capture_followup(connection, *, followup_of=None, who, **values):
    """A followup item, filed as `sd task add --kind followup` files one (sd:719 step 6a).

    The pack's verb (the `_capture` function of its `bin/sd_work.py` at a7572f14) is
    `workflow.capture_task`, which hard-codes `kind='task'`, and then the
    library's own `writes.set_item_fields(kind=...)`, both inside one
    `transaction` so nothing observes the row while it still reads `task`.
    The same two calls here, for the same reason: `capture_task` is the
    capture entry point and this is not the place to widen it.

    `followup_of` is the item the form had selected, and it is optional: a
    followup item stands on its own (that is what makes it an item and not a
    note). Given, the link is written twice, once
    each way: the child carries `fields.followup_of`, and the parent gets a
    `comment` note naming the child -- a `comment`, never a `followup` note,
    which would put the item on Today's open followups as a note as well. The
    child takes the parent's repository unless the form named one, as the
    verb takes the enclosing checkout. Named means a repository: a `repo`
    of `null` is a form that named none, the way a serialised optional
    field arrives, and it inherits like a missing key does (PR #428
    review). The form itself never sends `null`; the JSON route is open to
    any caller.
    """
    from sd_db import writes

    with workflow.transaction(connection):
        parent = workflow.item_state(connection, followup_of)["item"] if followup_of else None
        if parent is not None and values.get("repo") is None:
            values["repo"] = parent["repo"]
        state = workflow.capture_task(connection, who=who, **values)
        item = state["item"]["id"]
        fields = {"fields": {"followup_of": followup_of}} if followup_of else {}
        writes.set_item_fields(connection, item, kind="followup", **fields)
        if parent is not None:
            workflow.add_item_note(connection, followup_of, kind="comment", who=who,
                                   body=f"Followup item #{item} filed: {state['item']['title']}")
        return workflow.item_state(connection, item)


def _acknowledge_report(connection, item, *, expected_revision):
    """The Reports page's Acknowledge: the ingest's own followup goes with the report, a person's refuses (sd:3533, sd:4755).

    The pack's `resolve_followups=True` resolves every open followup since sd:4722, and its `reopen` opens none
    again, so a one-click call would close a person's work for good. The ingest's followup is the one whose
    `session` is `INGEST_SESSION` and whose text `_ingest_followup_for` derives from the report's own fields,
    the rule the pack's `resolve_ingest_followups` used before sd:4722. One transaction holds the check and the
    acknowledge, so a followup written between them cannot slip through.
    """
    from sd_db import reporting

    with workflow.transaction(connection):
        state = workflow._checked_state(connection, item, expected_revision)
        ingests = reporting._ingest_followup_for(state["item"]["fields"])
        blocking = [note["id"] for note in state["notes"] if note["kind"] == "followup" and not note["resolved_at"]
                    and (note["session"] != reporting.INGEST_SESSION or note["body"] != ingests)]
        if blocking:
            raise workflow.WorkflowError("resolve the report's followups before acknowledging it: "
                                         + "; ".join(f"resolve followup {note}" for note in blocking))
        return reporting.acknowledge(connection, item, expected_revision=expected_revision, who="dashboard",
                                     resolve_followups=True)


#: The reason `reporting.acknowledge` writes on its `<status> -> done by <who>` note: `resolved followup(s) <ids>`
#: since sd:4722, `resolved the ingest's followup(s) <ids>` before it, so a report an older pack acknowledged undoes too.
_ACKNOWLEDGED_FOLLOWUPS = re.compile(r"\w+ -> done by .*?: resolved (?:the ingest's )?followups? ([0-9]+(?:, [0-9]+)*)")


def _reopen_report(connection, item, *, expected_revision):
    """The Reports page's Undo: the report's status, then the followups its acknowledgement resolved (sd:2395, sd:4755).

    The pack's `reopen` restores the status and leaves those followups resolved. The newest `<status> -> done`
    note, the one `reopen` reads its target from, names them; only this report's resolved followups reopen.
    """
    from sd_db import reporting, writes

    with workflow.transaction(connection):
        was_done = workflow.item_state(connection, item)["item"]["status"] == "done"
        state = reporting.reopen(connection, item, expected_revision=expected_revision, who="dashboard")
        if not was_done:
            return state
        acknowledged = next((note["body"] for note in reversed(state["notes"])
                             if note["kind"] == "status_change" and re.match(r"\w+ -> done by ", note["body"])), "")
        named = _ACKNOWLEDGED_FOLLOWUPS.fullmatch(acknowledged)
        resolved = {int(note) for note in named[1].split(", ")} if named else set()
        for note in state["notes"]:
            if note["id"] in resolved and note["kind"] == "followup" and note["resolved_at"]:
                writes.reopen_note(connection, note["id"])
        return workflow.item_state(connection, item)


def _queued_piece(connection, number: int, answer):
    """`answer`, after the pack queues the piece's Drive copies (pack sd:7243), as an `sd writing` verb queues them.

    Best effort: the write is done, so `publishing.queue_piece` never raises and its lines go to the log only.
    """
    from . import publishing

    for line in publishing.queue_piece(connection, number):
        print(f"sd-dashboard: sd:{number}: {line}", file=sys.stderr, flush=True)
    return answer


def _base_kind(connection, item):
    """The base kind of a vault base row (sd:3977), or None: the item routes send such a row to `records.edit_row`."""
    from sd_db.sources.vault import SOURCE

    row = workflow.item_state(connection, item)["item"]
    return json.loads(row["fields"] or "{}").get("kind") if row["source"] == SOURCE else None


def _edit_base_row(connection, item, revision, values):
    """A base row's stage move or field and body edit, under its kind's rules, answered as the item's state.

    `mirror` is the heartbeat the write's render left: a write never fails for its render, so the page says when it did.
    """
    from . import health_screen

    try:
        from sd_db import records
    except ImportError as missing:  # a pack older than the records module refuses, as its vault driver would
        raise ValueError("the installed sd_db has no records module; edit the note in the vault") from missing
    if set(values) - {"stage", "fields", "body"}:
        raise ValueError("A base row takes a stage, its declared fields or its body.")
    records.edit_row(connection, item, records.rules_for(_base_kind(connection, item), home=Path.home()),
                     revision=revision, who="dashboard", **values)
    try:
        beat = health_screen.read_mirror(connection)
    except ValueError:  # a library without the read: the page says nothing rather than ok
        beat = None
    return {**workflow.item_state(connection, item), "mirror": beat[1] if beat else None}


def action_route(path, payload, *, principal, operations_backend=None, services_backend=None, fleet_backend=None):
    """Parse one finite action vocabulary before opening a writable connection.

    `principal` is what the listener authenticated for this request
    (`auth.Context.principal`): `local` on loopback, the operator login
    behind Tailscale. Required, with no default, for the same reason the
    library's `who` has none: an action that records it must be handed it,
    not fall back to a name that fits every caller (sd:755).
    """
    from . import v2

    registered = v2.action(path)
    if registered is not None:
        return registered.write(payload, principal)
    values = dict(payload)
    if path == "/api/adversarial":
        from . import adversarial

        # sd:5064: a codex review of a task, a brief, a repository or a change, started in the background.
        return adversarial.request(values)
    if path == "/api/chat":
        from . import chat

        # sd:5294: the right pane's question, answered by one bounded, read-only claude -p; it writes nothing.
        return chat.request(values)
    if path == "/api/research/intake":
        from . import source_intake

        # sd:5112: pasted URLs and staged files become ledger rows on a fresh branch, landed through the lane.
        return source_intake.request(values, principal)
    issue_run = re.fullmatch(r"/api/issue-runs/([A-Za-z0-9-]{1,80})/(confirm|check|discard)", path)
    if issue_run:
        from . import issue_filing

        # sd:5103: Confirm files the previewed issue once and links it back; Check looks again for the issue of a run
        # whose outcome is not known, and never files; Discard ends a run.
        if issue_run[2] == "confirm":
            return issue_filing.confirm_request(issue_run[1], values)
        if issue_run[2] == "check":
            return issue_filing.check_request(issue_run[1], values)
        return issue_filing.discard_request(issue_run[1], values)
    if path == "/api/now/fix":
        from . import fix_session

        # sd:5652: Fix on a Today Now row no other command addresses, read again through the server's fleet seam.
        return fix_session.row_request(values, fleet=fleet_backend, jobs=operations_backend)
    if path == "/api/actions/run":
        from . import run_actions

        # sd:5886: a button that gave a line to copy runs it: the page names an action and ids, the registry the argv.
        return run_actions.request(values, jobs=operations_backend)
    if path == "/api/snooze":
        from . import snooze

        # Today and Health both post it (sd:1896), so it is not one page's `Api` write.
        return snooze.request(values)
    if path == "/api/contributions/acknowledge":
        from sd_db import contributions

        if (set(values) != {"key", "revision", "event_ids"}
                or not isinstance(values["key"], str) or not isinstance(values["revision"], str)
                or not isinstance(values["event_ids"], list) or not values["event_ids"]
                or any(not isinstance(event, str) for event in values["event_ids"])):
            raise ValueError("Provide the contribution key, current revision and exact event IDs.")
        return lambda connection: contributions.acknowledge(connection, values["key"], values["event_ids"],
            expected_revision=values["revision"], who="dashboard")
    if path == "/api/palette/prepare":
        from sd_db import runner_exec

        if set(values) - {"item", "command", "values", "revision", "catalog", "screen", "target"} or not {"item", "command", "values", "revision", "catalog", "screen"} <= set(values):
            raise ValueError("Choose a registered command, item and current revisions.")
        if type(values["item"]) is not int or not 1 <= values["item"] <= 9223372036854775807 or not isinstance(values["command"], str) or not isinstance(values["screen"], str):
            raise ValueError("Choose a valid item and registered command.")
        return lambda connection: runner_exec.prepare(connection, values["item"], values["command"], values["values"],
            expected_revision=values["revision"], expected_catalog=values["catalog"], screen=values["screen"],
            target=values.get("target"), who="dashboard")
    match = re.fullmatch(r"/api/palette/([1-9][0-9]{0,18})/(execute|reconcile)", path)
    if match:
        from sd_db import runner_exec

        if values or int(match[1]) > 9223372036854775807:
            raise ValueError("Execute the recorded command without additional arguments.")
        operation = runner_exec.execute_immediate if match[2] == "execute" else runner_exec.reconcile
        return lambda connection: operation(connection, int(match[1]))
    if path == "/api/providers/configure":
        from sd_db import provider_controls

        if set(values) != {"revision", "enabled", "orders"}:
            raise ValueError("Provide the complete provider configuration and current revision.")
        return lambda connection: provider_controls.configure(connection, enabled=values["enabled"],
            orders=values["orders"], expected_revision=values["revision"], who="dashboard")
    from .metrics_screen import BILL_NAME

    match = re.fullmatch(f"/api/bills/({BILL_NAME})/cap", path)
    if match:
        from sd_db import provider_controls

        # Slice 12b (sd:234). The number is `set_cap`'s to judge: it refuses a
        # stale revision (`StaleItem`, 409 below), an unknown bill and a cap on a
        # bill a `start` entry is billed to (`WorkflowError`, `RegistryError`)
        # and a number the ledger would not hold (`LedgerRefused`), the last
        # three `SdDbError`s the `do_POST` mapping already answers with 400
        # and the library's own sentence. `null` clears the cap.
        if set(values) != {"revision", "cap_usd_month"}:
            raise ValueError("Provide the new monthly cap, or null to clear it, and the current revision.")
        return lambda connection: provider_controls.set_cap(connection, match[1], values["cap_usd_month"],
            expected_revision=values["revision"], who="dashboard")
    match = re.fullmatch(r"/api/skills/(sd-[a-z0-9-]+)/(try|review|promote|demote)", path)
    if match:
        from sd_db import skills_catalog

        if set(values) - {"revision", "path_name"} or "revision" not in values:
            raise ValueError("Provide the current skill revision and optional workflow path.")
        if match[2] == "try":
            return lambda connection: skills_catalog.trial(connection, match[1], expected_revision=values["revision"])
        return lambda connection: skills_catalog.request(connection, match[1], match[2],
            expected_revision=values["revision"], path_name=values.get("path_name"), who="dashboard")
    match = re.fullmatch(r"/api/skill-reviews/([1-9][0-9]*)/apply", path)
    if match:
        from sd_db import skills_catalog

        if set(values) != {"revision", "notes"}:
            raise ValueError("Select proposals and provide the current review revision.")
        return lambda connection: skills_catalog.apply_proposals(connection, int(match[1]), values["notes"], expected_revision=values["revision"], who="dashboard")
    match = re.fullmatch(r"/api/reports/([1-9][0-9]*)/acknowledge", path)
    if match:
        if set(values) != {"revision"}:
            raise ValueError("Provide the current report revision.")
        return lambda connection: _acknowledge_report(connection, int(match[1]), expected_revision=values["revision"])
    # The Reports page's Undo (sd:2395): it posts the revision its acknowledge answered, so a report changed since is stale.
    match = re.fullmatch(r"/api/reports/([1-9][0-9]*)/reopen", path)
    if match:
        if set(values) != {"revision"}:
            raise ValueError("Provide the revision the acknowledgement answered.")
        return lambda connection: _reopen_report(connection, int(match[1]), expected_revision=values["revision"])
    service_action = re.fullmatch(r"/api/services/([A-Za-z0-9][A-Za-z0-9._-]{0,199})/(start|stop|restart)", path)
    if service_action:
        from sd_db import services

        label, action = service_action.groups()
        if (set(values) != {"revision"} or not isinstance(values["revision"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", values["revision"])):
            raise ValueError("The current service revision is required.")
        mutation = {"start": services.start_service, "stop": services.stop_service,
                    "restart": services.restart_service}[action]
        return lambda connection: mutation(connection, label, backend=services_backend,
            expected_revision=values["revision"], who="dashboard")
    if path == "/api/repos/refresh":
        # sd:5614: Repos' Refresh writes nothing; any answered post invalidates the fleet snapshot (sd:4955), so the
        # page's next read waits for a git reading begun after it.
        if values:
            raise ValueError("Refresh repos takes no values.")
        return lambda connection: {"refreshed": True}
    if path == "/api/satellites/refresh":
        from .management_screen import refresh_satellites

        # sd:5108: one request row every satellite answers on its next follow run; the post carries nothing.
        if values:
            raise ValueError("Refresh satellites takes no values.")
        return lambda connection: refresh_satellites(connection)
    if path == "/api/shared-upload/regenerate":
        from . import upload_link

        # sd:5405: sign a new upload link with local-shared-upload's signer and keep it as one setting row.
        if values:
            raise ValueError("Regenerate takes no values.")
        return lambda connection: upload_link.regenerate(connection)
    match = re.fullmatch(r"/api/repos/(runner-merge|managed|lane-host|channels|dependabot|github-issues|issues-scope|jira-project)", path)
    if match:
        from .management_screen import set_repo

        if match[1] == "channels":
            # sd:3465: the value is an object of channels or null, no setting; `channels.validate` judges the object,
            # and `before` is the canonical text the page showed, `none` for no setting.
            if (set(values) != {"path", "value", "before"} or not isinstance(values["path"], str)
                    or not isinstance(values["value"], (dict, type(None)))
                    or not isinstance(values["before"], str) or not values["before"]):
                raise ValueError("Provide the repository path, the new channels object or null, and the value the page showed.")
            return lambda connection: set_repo(connection, "channels", values["path"], values["value"], values["before"])

        # The sd-db repo verbs Management runs (sd:2118); `before` is what the page showed, refused when stale.
        # A lane host is `hub` or a typed host name (sd:3075): `repos.set_lane_host` refuses a bad one by its rule.
        def word(value) -> bool:
            if match[1] in ("lane-host", "jira-project"):
                # A Jira key or `off` (sd:5103): `repos.set_jira_project` refuses a bad key by its rule.
                return isinstance(value, str) and value != ""
            words = {"runner-merge": ("manual", "auto"), "github-issues": ("off", "read", "ask"),
                     "issues-scope": ("all", "mine")}
            return value in words.get(match[1], ("yes", "no"))

        if (set(values) != {"path", "value", "before"} or not isinstance(values["path"], str)
                or not word(values["value"]) or not word(values["before"]) or values["value"] == values["before"]):
            raise ValueError(f"Provide the repository path, the new {match[1]} value and the value the page showed.")
        return lambda connection: set_repo(connection, match[1], values["path"], values["value"], values["before"])
    operation = re.fullmatch(r"/api/(jobs|assignments)/([a-z0-9][a-z0-9_-]{0,99})/(retry|cancel)", path)
    if operation:
        from sd_db import operations

        entity, identifier, action = operation.groups()
        if set(values) != {"revision"} or not isinstance(values["revision"], str):
            raise ValueError("The current operation revision is required.")
        options = {"expected_revision": values["revision"], "who": "dashboard"}
        if entity == "assignments":
            if action != "cancel" or not re.fullmatch(r"[1-9][0-9]*", identifier):
                raise NotFound(path)
            return lambda connection: operations.cancel_assignment(connection, int(identifier), **options)
        mutation = operations.retry_job if action == "retry" else operations.cancel_job
        return lambda connection: mutation(connection, identifier, backend=operations_backend, **options)
    if path == "/api/items" and "fix" in values:
        from sd_db.writes import now as clock

        from . import builds

        # pack sd:8113: Today's Fix and Fix all on flaky heads; the server writes each brief from its own lane read.
        rows = values["fix"]
        if (set(values) != {"fix"} or not isinstance(rows, list) or not rows or len(rows) > 50
                or not all(isinstance(row, str) and FLAKY_REF.fullmatch(row) for row in rows) or len(set(rows)) != len(rows)):
            raise ValueError("Fix takes up to 50 distinct flaky row ids, each flaky:<repo>:<head>.")
        return lambda connection: builds.fix_flaky(connection, rows, now=clock(), capture=lambda c, **kw: capture_ref(
            c, who="dashboard", **kw))
    if path == "/api/items":
        if set(values) - {"title", "body", "priority", "due", "repo", "kind", "followup_of", "ref", "build"}:
            raise ValueError("Unknown task field.")
        if values.pop("build", False) is not False:
            from . import builds

            # pack sd:8113: a broken row's Fix files (or answers the open item) under its ref, then queues the build.
            ref = values.pop("ref", None)
            if set(values) - {"title", "body", "repo"} or not isinstance(ref, str) or not BROKEN_REF.fullmatch(ref):
                raise ValueError("Build on capture takes a title, body, repo and a broken:<row id> ref.")
            return lambda connection: builds.file_and_build(
                connection, lambda: capture_ref(connection, ref=ref, who="dashboard", **values))
        if "title" not in values:
            raise ValueError("A title is required.")
        kind = values.pop("kind", "task")
        parent = values.pop("followup_of", None)
        ref = values.pop("ref", None)
        reused = _reused(ref)
        if ref is not None and (kind != "task" or not isinstance(ref, str)
                                or not (GITHUB_REF.fullmatch(ref) or reused)):
            raise ValueError("A ref files a task, and names a GitHub issue as github:<owner>/<repo>#<number>,"
                             " a repository's alerts as dependabot:<owner>/<repo>, or a doctor row as doctor:<hash>.")
        if kind == "task":
            if parent is not None:
                raise ValueError("Only a followup item names the item it follows up.")
            if reused:
                # Fix starts the session on it at once, so no draft is queued. A doctor finding is the system repo's.
                if DOCTOR_REF.fullmatch(ref):
                    from sd_db.config import system_checkout

                    values["repo"] = str(system_checkout())
                return lambda connection: capture_ref(connection, ref=ref, who="dashboard", **values)
            if ref is not None:
                return lambda connection: capture_drafted(
                    connection, lambda: capture_ref(connection, ref=ref, who="dashboard", **values), who="dashboard")
            return lambda connection: capture_drafted(
                connection, lambda: workflow.capture_task(connection, who="dashboard", **values), who="dashboard")
        if kind != "followup":
            raise ValueError("Capture files a task or a followup item.")
        if parent is not None and (type(parent) is not int or not 1 <= parent <= 9223372036854775807):
            raise ValueError("Choose a valid item for the followup to follow up.")
        return lambda connection: capture_followup(connection, followup_of=parent, who="dashboard", **values)
    match = re.fullmatch(r"/api/(items|notes)/([1-9][0-9]*)(/status|/notes|/resolve|/relink|/cancel|/cancel-task|/stage|/park|/revive|/fix|/draft-email|/file-issue|/experiment)?", path)
    if not match:
        raise NotFound(path)
    entity, number, suffix = match.groups()
    revision = values.pop("revision", None)
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise ValueError("The current item revision is required. Reload the item.")
    options = {"who": "dashboard", "expected_revision": revision}
    number = int(number)
    if entity == "items" and suffix is None:
        return lambda connection: (_edit_base_row(connection, number, revision, values)
                                   if _base_kind(connection, number) else
                                   workflow.edit_item(connection, number, values, **options))
    if entity == "items" and suffix == "/status":
        if set(values) - {"status", "reason"} or "status" not in values:
            raise ValueError("A status and optional reason are required.")
        return lambda connection: workflow.change_status(connection, number,
            values["status"], reason=values.get("reason") or None, **options)
    if entity == "items" and suffix == "/notes":
        if set(values) - {"body", "kind"} or "body" not in values:
            raise ValueError("A note body and optional kind are required.")
        return lambda connection: workflow.add_item_note(connection, number, **values, **options)
    if entity == "notes" and suffix == "/resolve" and not values:
        return lambda connection: workflow.resolve_item_note(connection, number, **options)
    if entity == "items" and suffix in ("/park", "/revive"):
        from sd_db import writing

        if values:
            raise ValueError("Parking accepts only the current item revision.")
        def park(connection):
            return _queued_piece(connection, number, writing.park_piece(connection, number,
                parked=suffix == "/park", actor="human", require_row=True, **options))
        return park
    if entity == "items" and suffix == "/stage":
        from sd_db import writing

        if set(values) - {"stage", "reason", "correct"} or "stage" not in values:
            raise ValueError("Provide a stage, optional correction and reason.")
        if "correct" in values and type(values["correct"]) is not bool:
            raise ValueError("Correction must be true or false.")

        def stage(connection):
            # A vault base row moves through its kind's graph (sd:3977); `published` there is a stage, not a publication.
            if _base_kind(connection, number):
                if set(values) != {"stage"}:
                    raise ValueError("A base row's stage move takes the stage only.")
                return _edit_base_row(connection, number, revision, values)
            # A Studio project's item (sd:6921) moves along its kind's strip; a writing piece never does, whatever names it.
            row = workflow.item_state(connection, number)["item"]
            if not (row["kind"] == "idea" and row["piece"]):
                from . import studio

                project = studio.naming(connection, number)
                if project is not None:
                    if set(values) != {"stage"} or not isinstance(values["stage"], str):
                        raise ValueError("A Studio stage move takes the stage only.")
                    return studio.move_stage(connection, number, project, values["stage"], who="dashboard",
                                             expected_revision=revision)
            if values["stage"] == "published":
                raise ValueError("Publication uses the reviewed publishing workflow.")
            return _queued_piece(connection, number, writing.change_stage(connection, number, values["stage"],
                actor="human", require_row=True, reason=values.get("reason") or None,
                correct=values.get("correct", False), **options))
        return stage
    if entity == "items" and suffix in ("/relink", "/cancel"):
        from sd_db import progress

        if suffix == "/relink" and set(values) == {"path"}:
            return lambda connection: progress.relink_artifact(connection, number, values["path"], **options)
        if suffix == "/cancel" and set(values) == {"reason"}:
            return lambda connection: progress.cancel_work(connection, number, reason=values["reason"], **options)
        raise ValueError("Provide the artifact path or cancellation reason.")
    if entity == "items" and suffix == "/fix":
        from . import fix_session

        # Fix (sd:3456): Tasks, Reports and Today post it, so it is no one page's `Api` write.
        return fix_session.request(number, values, revision)
    if entity == "items" and suffix == "/draft-email":
        from . import email_draft

        # Draft email (sd:3463): claude writes it, workspace-mcp saves it as a Gmail draft; nothing sends it.
        return email_draft.request(number, values, revision)
    if entity == "items" and suffix == "/file-issue":
        from . import issue_filing

        # File issue (sd:5103): a tool-less claude drafts a sanitized issue in the background; nothing files until Confirm.
        return issue_filing.request(number, values, revision)
    if entity == "items" and suffix == "/experiment":
        # sd:4639: the values `sd task edit <n> --experiment` takes, to the same `projection.designate` (pack sd:3982).
        specs = values.get("experiment")
        if set(values) != {"experiment"} or not isinstance(specs, list) or not 1 <= len(specs) <= 2 \
                or not all(isinstance(spec, str) for spec in specs):
            raise ValueError("Provide the experiment values: yes, of:M or off, and row:ID or new.")
        try:
            import sd_db.projection as projection
        except ImportError:
            raise ValueError("This sd_db has no projection; update the command pack.") from None
        return lambda connection: projection.designate(connection, number, specs, **options)
    if entity == "items" and suffix == "/cancel-task":
        from sd_db import progress

        # `sd task cancel` (sd:3012, the Tasks page's Close): task_guard admits a task or followup, never work or a recurrence.
        if set(values) != {"reason"}:
            raise ValueError("Provide the reason nobody will do the task.")
        return lambda connection: progress.cancel_work(connection, number, reason=values["reason"], guard=progress.task_guard, **options)
    raise NotFound(path)


class Dashboard(BaseHTTPRequestHandler):
    """Same-origin task controls over the shared workflow library."""

    server_version = "sd-dashboard"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    #: Set by `build`.
    database: Path | None = None
    clock = staticmethod(_now)
    session_secret: bytes
    operations_backend = None
    services_backend = None
    ports_backend = None
    #: `now_screen.document`'s fleet seam, `area -> document`; None runs the child.
    fleet_backend = None
    #: `metrics_screen.document`'s kept slow parts, `name -> document` (sd:4955); None reads them per request.
    metrics_backend = None
    #: `/api/usage`'s current month kept (`_usage_kept`, sd:5064); None reads it per request.
    usage_backend = None
    #: `now_screen.document`'s Jev command for the failed-job shadow; None asks nothing.
    jev_command = None
    frontdoor: auth.FrontDoor | None = None
    frontdoor_check = None
    direct_listener = False
    peer_lookup = None
    #: The checkout commit this process started on (sd:3528); None when git could not say.
    served_commit = None
    #: Each page's rail badge (sd:3446); `build` gives each server its own.
    badges = None
    #: The READ_SLOTS read connections (sd:7842); `build` gives each server its own, shared by its direct listener.
    reads = None
    #: Set once `_send` begins an answer: a failure after it cannot answer again (sd:3529).
    _started = False

    # `_policy` lets one response replace the shared Content-Security-Policy,
    # and only that header. A served document carries its stylesheet in its own
    # head, which `default-src 'self'` refuses; the policy it carries instead
    # permits that and forbids script outright, which the shared one does not.
    # The substitution happens here rather than in the route, so the loop below
    # stays the single place any response gets its headers.
    #
    # `_overrides` does the same for any other header in the tuple, by name. A
    # design asset uses it to relax Cross-Origin-Resource-Policy, because the
    # sandboxed mockup that loads it has an opaque origin (see designs.py). An
    # override the tuple does not name is added after it: a design font sends
    # Access-Control-Allow-Origin that way.
    def end_headers(self) -> None:
        policy = getattr(self, "_policy", None)
        overrides = getattr(self, "_overrides", None) or {}
        for name, value in SECURITY_HEADERS:
            if policy and name == "Content-Security-Policy":
                value = policy
            value = overrides.get(name, value)
            self.send_header(name, value)
        named = {name for name, _ in SECURITY_HEADERS}
        for name, value in overrides.items():
            if name not in named:
                self.send_header(name, value)
        super().end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - base class
        return

    def handle(self) -> None:
        """A client that left before the response is one line, not a traceback.

        A browser tab navigating away, a curl with `--max-time`, a health
        probe: the write into the closed socket is ordinary, and the stdlib's
        traceback for it was 109 blocks of the err log on one day (sd:970).
        The handler is shared by both listeners, so the direct IP socket is
        covered too. A route's own failure never reaches here: `_answer`
        answers it (sd:3529).
        """
        try:
            super().handle()
        except ConnectionError:
            self._left()

    def _left(self) -> None:
        host, port = self.client_address[:2]
        print(f"client {host}:{port} left before the response", file=sys.stderr)

    def _answer(self, method) -> None:
        """The last-resort handler (sd:3529): a failure no route's catch list names still answers.

        An `OSError` is 503 and anything else 500, each naming the method and route: JSON on `/api/`, the error
        page elsewhere, so the browser never gets a closed socket with no body. Only the 500 keeps its traceback
        in the service log. Once an answer has started a second one would corrupt it: a socket error there is
        the client leaving, one line as in `handle`, and anything else closes the connection after its traceback.
        """
        self._started = False
        try:
            method()
        except Exception as error:
            path = urlsplit(self.path).path
            self.close_connection = True
            if self._started and isinstance(error, OSError):
                return self._left()
            problem = f"{self.command} {path} failed: {type(error).__name__}: {error}"
            print(f"sd-dashboard: {problem}", file=sys.stderr, flush=True)
            if not isinstance(error, OSError):
                traceback.print_exc()
            if not self._started:
                self._error(503 if isinstance(error, OSError) else 500, problem, path)

    def _error(self, status: int, message: str, path: str) -> None:
        """An error answer in the shape its caller reads: JSON for a script on `/api/`, the error page for a browser."""
        if path.startswith("/api/"):
            return self._json(status, {"error": message})
        self._send(status, error_page(status, message).encode("utf-8"), "text/html; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str, *, cookie=None,
              policy: str | None = None, overrides: dict | None = None,
              cache: str = "no-store", compress: bool = False) -> None:
        # Assigned unconditionally: the handler instance is reused for every
        # request on a kept-alive connection, and a policy left behind would
        # apply to the next page served down the same socket.
        self._started = True
        self._policy = policy
        self._overrides = overrides
        # `compress` is a static file's: `caching` says why nothing else is (sd:2141).
        encoded = compress and caching.compressible(content_type) and \
            caching.accepts_gzip(self.headers.get("Accept-Encoding"))
        if encoded:
            body = caching.gzipped(body)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        if compress:
            self.send_header("Vary", "Accept-Encoding")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802 - the base class names it
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 - the base class names it
        self._answer(self._get)

    def _get(self) -> None:
        context = self._context()
        if context is None:
            return self._json(403, {"error": "Untrusted dashboard host."})
        split = urlsplit(self.path)
        path = split.path
        # A read carries the dashboard's own origin or none. `Origin: null` is a sandboxed page (sd:1502): its cookie
        # is withheld by SameSite, and this refusal holds if a browser ever sends one anyway.
        if path.startswith("/api/") and self.headers.get_all("Origin", [context.origin]) != [context.origin]:
            return self._json(403, {"error": "A dashboard read must come from the dashboard's own origin."})
        if path == "/health":
            return self._health()
        if path == "/favicon.ico":
            return self._static("favicon.ico")
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :], split.query)
        if path == "/ui/sections.js":
            return self._sections(context)
        if path.startswith("/ui/"):
            return self._v2_asset(path[len("/ui/") :], split.query)
        if path.startswith("/v2/"):
            return self._moved(path, split.query)
        if path.startswith("/documents/"):
            return self._document(path[len("/documents/") :])
        if path.startswith("/designs/"):
            return self._design(path[len("/designs/") :])
        if path.startswith("/lane-shots/"):
            return self._lane_shot(path[len("/lane-shots/") :])
        if path.startswith("/research/deck/"):
            return self._research_deck(context, path[len("/research/deck/") :])
        if path == "/home/assistant":
            return self._home_assistant(context)
        if path.startswith("/api/adversarial/") and not split.query:
            from . import adversarial

            # sd:5064: one review run's state, from its state file; no database read.
            if not self._session(context):
                return self._json(403, {"error": f"Reload the page to read a review run: {SESSION_ENDED}"})
            return self._json(*adversarial.status(path[len("/api/adversarial/"):]))
        if path.startswith("/api/issue-runs/") and not split.query:
            from . import issue_filing

            # sd:5103: one issue run's draft and state, from its state file; a read that never files.
            if not self._session(context):
                return self._json(403, {"error": f"Reload the page to read an issue run: {SESSION_ENDED}"})
            return self._json(*issue_filing.status(path[len("/api/issue-runs/"):]))
        parameters = parse_qs(split.query, keep_blank_values=True)
        if not self.reads.acquire(timeout=READ_WAIT):
            if self._session(context):
                self.badges.remember(path, None, self.clock())  # a counted read that could not run is unknown (sd:3446)
            return self._error(503, BUSY, path)
        try:
            connection = sd_db.connect(self.database, write=False)
        except (OSError, SdDbError, sqlite3.Error) as problem:
            self.reads.release()
            if self._session(context):
                self.badges.remember(path, None, self.clock())  # a counted read that could not run is unknown (sd:3446)
            return self._send(
                503, error_page(503, str(problem)).encode("utf-8"),
                "text/html; charset=utf-8",
            )
        except BaseException:
            self.reads.release()
            raise
        held = [connection]

        def release():
            """Close the read connection and give its slot back, once; a read that waits on no store calls it early."""
            if held:
                held.pop().close()
                self.reads.release()
        try:
            if path.startswith("/studio/files/"):
                return self._studio_file(context, connection, path[len("/studio/files/"):])
            if path.startswith("/api/"):
                from . import v2

                registered = v2.api(path)
                if registered is not None:
                    return self._api(context, connection, split, parameters, *registered, release)
                if path == "/api/contributions":
                    from sd_db import contributions

                    if not self._session(context):
                        return self._json(403, {"error": f"Reload the page to read contribution details: {SESSION_ENDED}"})
                    if split.query:
                        return self._json(400, {"error": "Contribution projection does not accept query parameters."})
                    return self._json(200, {"contributions": contributions.projection(connection)})
                if path == "/api/palette" or re.fullmatch(r"/api/executions/[1-9][0-9]{0,18}", path):
                    from sd_db import runner_exec

                    if not self._session(context):
                        return self._json(403, {"error": f"Reload the page to read command details: {SESSION_ENDED}"})
                    try:
                        if path == "/api/palette":
                            if set(parameters) - {"screen", "item"} or any(len(values) != 1 for values in parameters.values()):
                                raise ValueError("Choose one screen and item.")
                            item = parameters.get("item", [None])[0]
                            if item is not None and (not re.fullmatch(r"[1-9][0-9]{0,18}", item) or int(item) > 9223372036854775807):
                                raise ValueError("Invalid item.")
                            return self._json(200, runner_exec.inventory(connection, screen=parameters.get("screen", ["item"])[0], item=int(item) if item else None))
                        if set(parameters) - {"offset"} or any(len(values) != 1 for values in parameters.values()):
                            raise ValueError("Provide one output offset.")
                        offset = parameters.get("offset", ["0"])[0]
                        if not re.fullmatch(r"[0-9]{1,10}", offset):
                            raise ValueError("Invalid output offset.")
                        return self._json(200, runner_exec.read_execution(connection, int(path.rsplit("/", 1)[1]), offset=int(offset)))
                    except (ValueError, SdDbError) as error:
                        return self._json(400, {"error": str(error)})
                if path == "/api/now":
                    from . import now_screen

                    # The fleet child, the shadow read and the launchd jobs, for
                    # a page that was opened first: the document describes this machine.
                    if not self._session(context):
                        return self._json(403, {"error": f"Reload the page to read Now: {SESSION_ENDED}"})
                    if split.query:
                        return self._json(400, {"error": "Now does not accept query parameters."})
                    # Every page's rail shows what this read carried (sd:3446); a read that raised leaves its badge unknown.
                    now, ticket = self.clock(), self.badges.ticket()
                    try:
                        document = now_screen.document(connection, now=now, fleet=self.fleet_backend,
                                                       jobs=self.operations_backend, jev=self.jev_command)
                    except BaseException:
                        self.badges.remember(path, None, now, ticket)
                        raise
                    self.badges.remember(path, document, now, ticket)
                    return self._json(200, document)
                if path == "/api/usage":
                    from .usage_screen import document

                    # The read `sd usage --json` prints, as the same bytes.
                    if not self._session(context):
                        return self._json(403, {"error": f"Reload the page to read Usage: {SESSION_ENDED}"})
                    if set(parameters) - {"month"} or any(len(values) != 1 for values in parameters.values()):
                        return self._json(400, {"error": "Usage takes one month, YYYY-MM."})
                    try:
                        if self.usage_backend is not None and "month" not in parameters:
                            body, observed, stale = self.usage_backend.read()
                            if stale:
                                return self._json(503, {"error": f"Usage was not read again: {stale}; the last read began {observed}."})
                        else:
                            body = document(connection, now=self.clock(), month=parameters.get("month", [None])[0])
                    except SdDbError as error:
                        return self._json(400, {"error": str(error)})
                    return self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
                if path == "/api/capture-repos" and not split.query:
                    from sd_db import repos

                    # The capture box's repo picker (sd:3521): every registered repository's key and folder name.
                    return self._json(200, {"repos": [{"path": row["path"], "name": Path(row["path"]).name}
                                                      for row in repos.registered(connection)]})
                match = re.fullmatch(r"/api/items/([1-9][0-9]{0,18})/capture-context", path)
                if not match or split.query or int(match[1]) > 9223372036854775807:
                    return self._json(404, {"error": "No capture context at this address."})
                state = workflow.item_state(connection, int(match[1]))
                # Only the selected identity and its revision belong in the
                # picker response. Full note history stays on the item page.
                return self._json(200, {
                    "item": {key: state["item"][key]
                             for key in ("id", "title", "kind", "status", "repo", "parked_at")},
                    "revision": state["revision"],
                })
            if path == "/operations":
                from .operations_screen import moved_to

                # pack sd:7517: a retired area's bookmark opens the view that holds it now.
                moved = moved_to(parameters)
                if moved:
                    return self._send(301, b"", "text/plain; charset=utf-8", overrides={"Location": moved})
            if path == "/queue":
                # pack sd:7234: Queue became Today's Queue view; its Show All is that view's Ended builds filter.
                shown = "?show=ended" if (parameters.get("show") or [""])[0] == "all" else ""
                return self._send(301, b"", "text/plain; charset=utf-8",
                                  overrides={"Location": f"/today{shown}#view=queue"})
            if path == "/backlog":
                # sd:2356: Tasks took over /backlog; an old address opens Tasks with the same query. v1's screen
                # was deleted in sd:2622.
                from .tasks_screen import from_backlog

                location = from_backlog(connection, parameters, now=self.clock())
                return self._send(301, b"", "text/plain; charset=utf-8", overrides={"Location": location})
            body = route(connection, path, parameters, now=self.clock())
        except workflow.MissingItem:
            return self._error(404, "The related item no longer exists. Choose another item.", path)
        except NotFound as missing:
            return self._send(
                404, error_page(404, f"Nothing at {missing}.").encode("utf-8"),
                "text/html; charset=utf-8",
            )
        except (SdDbError, sqlite3.Error) as problem:
            return self._send(503, error_page(503, str(problem)).encode("utf-8"),
                              "text/html; charset=utf-8")
        finally:
            release()
        session = self._session(context) or self._new_session(context)
        token = self._csrf(session)
        body = body.replace("</head>", f'<meta name="sd-csrf" content="{token}"></head>', 1)
        cookie = f"sd_session={session}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}"
        if context.secure:
            cookie += "; Secure"
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8", cookie=cookie)

    def _context(self):
        try:
            if self.direct_listener:
                context = auth.direct_context(self.headers, self.client_address,
                                              self.frontdoor, self.peer_lookup)
            else:
                context = auth.access_context(self.headers, self.client_address[0],
                                              self.server.server_address[1], self.frontdoor)
            if context and context.remote:
                if self.frontdoor_check() is not True:
                    return None
        except (runtime.RuntimeRefused, OSError, ValueError) as refused:
            # A check that could not run is unknown, not a refusal (sd:7842): out of file handles, the front door's
            # config read failed and every tailnet request read "Untrusted dashboard host". `_answer` makes it a 503
            # that names the cause; a host or login that does not match still returns None, the 403.
            cause = _unchecked(refused)
            if cause is not None:
                raise OSError(f"the dashboard could not check its front door: {cause}") from refused
            return None
        return context

    def _health(self):
        """Read-only local health used by the installed runtime's check."""
        try:
            connection = sd_db.connect(self.database, write=False)
            try:
                version = schema_version(connection)
            finally:
                connection.close()
            current_build = runtime.build_digests()
        except (FileNotFoundError, SdDbError, sqlite3.Error, runtime.RuntimeRefused) as problem:
            return self._json(503, {"service": "sd-dashboard", "ok": False, "error": str(problem)})
        changed = current_build != self.loaded_build
        owner = getattr(self.server, "runtime_owner", self.server)
        direct_ok = owner.direct_listener_healthy()
        healthy = version == SCHEMA_VERSION and not changed and direct_ok
        result = {
            "service": "sd-dashboard", "ok": healthy, "code_changed": changed,
            "pid": os.getpid(), "ppid": os.getppid(), "commit": self.served_commit,
            "schema": version, "expected_schema": SCHEMA_VERSION,
            "library": str(Path(sd_db.__file__).resolve()),
            **self.loaded_build,
        }
        if self.frontdoor is not None and self.frontdoor.direct is not None:
            result["direct_listener_ok"] = direct_ok
            if direct_ok:
                result["ip_origin"] = self.frontdoor.direct.origin
        self._json(200 if healthy else 503, result)

    def _mac(self, value):
        return hmac.new(self.session_secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _new_session(self, context):
        value = f"{int(time.time())}.{secrets.token_hex(24)}"
        signature = self._mac(f"{value}\n{context.scope}")
        return f"{value}.{signature}"

    def _session(self, context):
        if context is None or len(self.headers.get_all("Cookie", [])) != 1:
            return None
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            session = cookie["sd_session"].value
            stamp, random, signature = session.split(".")
            if not re.fullmatch(r"[0-9]{1,12}", stamp):
                return None
            age = time.time() - int(stamp)
            if (not (0 <= age <= SESSION_SECONDS)
                    or not re.fullmatch(r"[a-f0-9]{48}", random)
                    or not re.fullmatch(r"[a-f0-9]{64}", signature)):
                return None
            if not hmac.compare_digest(signature, self._mac(f"{stamp}.{random}\n{context.scope}")):
                return None
            return session
        except (CookieError, KeyError, ValueError):
            return None

    def _csrf(self, session):
        return self._mac("csrf." + session)

    def _api(self, context, connection, split, parameters, page, entry, release=None):
        """A registered page's GET (sd:2418): one session rule and one query rule for every page, then its read.

        The read answers a document (200) or `(status, document)`. A route refuses a query unless it says it takes one.
        """
        if not self._session(context):
            return self._json(403, {"error": f"Reload the page to read {page.section}: {SESSION_ENDED}"})
        if split.query and not entry.query:
            return self._json(400, {"error": f"{page.section} does not accept query parameters."})
        read, ticket = self._read(connection, split.path, parameters, release=release), self.badges.ticket()
        try:
            answer = entry.read(read)
        except BaseException:
            self.badges.remember(split.path, None, read.now, ticket)
            raise
        if isinstance(answer, tuple) and len(answer) == 3:
            # A picture's bytes (sd:6690, HOA's thumbnails): an image type only, never a page or a script.
            status, body, kind = answer
            self.badges.remember(split.path, None, read.now, ticket)
            if not kind.startswith("image/") or kind == "image/svg+xml":
                return self._json(502, {"error": f"{kind} is not a picture this page draws."})
            return self._send(status, body, kind)
        status, document = answer if isinstance(answer, tuple) else (200, answer)
        self.badges.remember(split.path, document if status == 200 else None, read.now, ticket)
        return self._json(status, document)

    def _read(self, connection, path, parameters, *, release=None):
        from . import v2

        return v2.registry.Read(connection=connection, now=self.clock(), path=path, parameters=parameters,
                                fleet=self.fleet_backend, jobs=self.operations_backend, services=self.services_backend,
                                ports=self.ports_backend, metrics=self.metrics_backend, served_commit=self.served_commit,
                                release=release)

    def _sections(self, context):
        """`/ui/sections.js`: the rail's map on every request, and each page's badge for a signed-in page (sd:3446).

        Unversioned and never cached, since the badges change. A request without a session gets the map alone: the
        badges are workflow state, and a page on another site that loads this script sends no cookie (SameSite).
        """
        from . import places, v2

        badges, realms = {}, []
        if self._session(context):
            # A busy store reads as one that could not open (sd:7842): no live page, so each says unknown.
            slot, connection = self.reads.acquire(timeout=READ_WAIT), None
            try:
                try:
                    connection = sd_db.connect(self.database, write=False) if slot else None
                except (FileNotFoundError, SdDbError, sqlite3.Error):
                    # No live page can be read, so each says unknown; the kept ones still show.
                    connection = None
                # The realm labels are the operator's own (dashboard.realms), so a request without a session gets none.
                realms = places.realm_places(connection)
                badges = self.badges.read(self._read(connection, "", {}), connected=connection is not None)
            finally:
                if connection is not None:
                    connection.close()
                if slot:
                    self.reads.release()
        self._send(200, v2.sections(badges, realms), "text/javascript; charset=utf-8")

    def _json(self, status, value):
        # sd:874. `value` is usually a `workflow.item_state` readback, which
        # carries a BLOB column back as `bytes`. The same encoding the revision
        # hash uses, so a row that renders on GET also answers on POST.
        self._send(status, json.dumps(value, default=workflow.json_safe).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - the base class names it
        self._answer(self._post)

    def _post(self) -> None:
        # Failed preflight must not leave unread bytes as another keep-alive request.
        self.close_connection = True
        context = self._context()
        origin = context.origin if context else None
        session = self._session(context)
        csrf = self.headers.get("X-SD-CSRF", "")
        if (context is None or self.headers.get_all("Origin", []) != [origin]
                or self.headers.get_all("Sec-Fetch-Site", ["same-origin"]) != ["same-origin"]
                or len(self.headers.get_all("X-SD-CSRF", [])) != 1
                or not session or not re.fullmatch(r"[a-f0-9]{64}", csrf)
                or not hmac.compare_digest(csrf, self._csrf(session))):
            return self._json(403, {"error": "Open the dashboard again before saving. The browser session, identity or origin is invalid."})
        if urlsplit(self.path).path in ("/api/research/intake/file", "/api/hoa/attach/file", "/api/home/attach/file") and not urlsplit(self.path).query:
            from . import hoa_attach, source_intake

            # sd:6690, sd:8112: HOA's and Home's Upload stage the same way, with their own suffixes and cap.
            bounds = ({"suffixes": hoa_attach.SUFFIXES, "limit": hoa_attach.UPLOAD_BYTES}
                      if urlsplit(self.path).path != "/api/research/intake/file" else {})

            # sd:5112: a dropped file's raw bytes, past the JSON bound; the intake request names it by its hash.
            if self.headers.get_content_type() != "application/octet-stream" or self.headers.get("Transfer-Encoding"):
                return self._json(415, {"error": "Expected the file's bytes as application/octet-stream."})
            try:
                self.connection.settimeout(source_intake.STAGE_READ_SECONDS)  # one wait; stage bounds the whole upload
                return self._json(*source_intake.stage(self.headers.get("X-SD-File-Name", ""),
                                                        self.headers.get("Content-Length", ""), self.rfile, **bounds))
            except OSError as problem:
                return self._json(503, {"error": f"The file was not staged: {problem}"})
        if self.headers.get_content_type() != "application/json":
            return self._json(415, {"error": "Expected application/json."})
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not lengths[0].isdigit()
                or not 0 < int(lengths[0]) <= MAX_BODY or self.headers.get("Transfer-Encoding")):
            return self._json(400, {"error": "A bounded request body is required."})
        try:
            payload = json.loads(self.rfile.read(int(lengths[0])))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object.")
            path = urlsplit(self.path)
            if path.query or path.fragment:
                raise ValueError("Action URLs do not accept query parameters.")
            action = action_route(path.path, payload, principal=context.principal,
                                 operations_backend=self.operations_backend,
                                 services_backend=self.services_backend, fleet_backend=self.fleet_backend)
        except (ValueError, UnicodeDecodeError) as problem:
            return self._json(400, {"error": str(problem)})
        except NotFound:
            return self._json(404, {"error": "No such action."})
        try:
            connection = sd_db.connect(self.database)
            try:
                from sd_db import reporting, writes

                from . import command_journal

                # A command that runs something gets its execution journal record once it answered (sd:5541). Its
                # writes are the journal's, not the action's, so they count as no operator action.
                journal = (self.database, connection, path.path, payload, context.principal, writes.now())
                before = connection.total_changes
                try:
                    result = action(connection)
                except BaseException as failure:
                    command_journal.record(*journal, failure=failure)
                    raise
                changed = connection.total_changes > before
                command_journal.record(*journal, result)
                if changed:
                    reporting.observed_action(connection, path.path)
            finally:
                connection.close()
                # Any write may have changed what a snapshot shows: the next read waits for a new one (sd:4955).
                for kept in (self.fleet_backend, self.metrics_backend, self.usage_backend):
                    invalidate = getattr(kept, "invalidate", None)
                    if invalidate is not None:
                        invalidate()
        except (workflow.MissingItem, workflow.MissingNote) as problem:
            return self._json(404, {"error": str(problem)})
        except workflow.StaleItem as problem:
            return self._json(409, {"error": str(problem), "reload": True})
        except (workflow.WorkflowError, SdDbError, ValueError) as problem:
            return self._json(400, {"error": str(problem)})
        except (OSError, sqlite3.Error) as problem:
            return self._json(503, {"error": str(problem)})
        # A write that answers its own status (sd:2207: 202 started, 409 refused) returns `(status, document)`, as a read can.
        status, result = result if isinstance(result, tuple) else (201 if path.path == "/api/items" else 200, result)
        self._json(status, result)

    def _document(self, tail: str) -> None:
        """One generated report, handed over whole.

        Read from disk and written to the socket unchanged. Embedding it in a
        dashboard page would put it through the markup filter, which would
        take its stylesheet and its chart with it, so the filter is avoided
        rather than fought: the document gets its own address and its own,
        tighter policy.
        """
        from .documents import located, policy

        key, _, name = tail.partition("/")
        found = located(key, name)
        if found is None:
            # One answer for every way of missing. Telling a prober which of
            # "no such root" and "outside the root" they reached is telling
            # them how the check works.
            return self._send(
                404, error_page(404, "No such document.").encode("utf-8"),
                "text/html; charset=utf-8",
            )
        root, target = found
        try:
            body = target.read_bytes()
        except OSError as problem:
            return self._send(
                503, error_page(503, str(problem)).encode("utf-8"),
                "text/html; charset=utf-8",
            )
        # A `*.app.html` page gets a sandbox and its own script; every other page gets none (sd:1502).
        self._send(200, body, "text/html; charset=utf-8", policy=policy(root, name, body))

    def _home_assistant(self, context) -> None:
        """Home's web UI tile (sd:6163): a 302 to Home Assistant from local-ha-mcp's settings, or a 503 naming why not."""
        from .home_screen import web

        if not self._session(context):
            return self._send(403, error_page(403, f"Reload the page to open Home Assistant: {SESSION_ENDED}").encode("utf-8"),
                              "text/html; charset=utf-8")
        location, why = web()
        if location is None:
            return self._send(503, error_page(503, f"Home Assistant cannot open: {why}.").encode("utf-8"), "text/html; charset=utf-8")
        return self._send(302, b"", "text/plain; charset=utf-8", overrides={"Location": location})

    def _research_deck(self, context, key: str) -> None:
        """One experiment's built status deck, `docs/dashboard/deck.pptx` in its checkout, as a download (pack sd:5525)."""
        from .research_deck import built

        if not self._session(context):
            return self._send(403, error_page(403, f"Reload the page to download a deck: {SESSION_ENDED}").encode("utf-8"),
                              "text/html; charset=utf-8")
        found = built(key)
        try:
            body = found.read_bytes() if found is not None else None
        except OSError as problem:
            return self._send(503, error_page(503, str(problem)).encode("utf-8"), "text/html; charset=utf-8")
        if body is None:
            return self._send(404, error_page(404, "No built deck at this address.").encode("utf-8"), "text/html; charset=utf-8")
        name = key.replace("/", "-") + "-deck.pptx"
        self._send(200, body, "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                   overrides={"Content-Disposition": f'attachment; filename="{name}"'})

    def _design(self, tail: str) -> None:
        """One file from the ui-design checkout, at its path in the tree.

        A page gets the sandboxing policy; an asset keeps the dashboard's policy
        and relaxes only its resource policy, so the sandboxed page may load it.
        """
        from .designs import ASSET_HEADERS, FONT_HEADERS, POLICY, read, resolve

        found = resolve(tail)
        if found is None:
            return self._send(
                404, error_page(404, "No such design file.").encode("utf-8"),
                "text/html; charset=utf-8",
            )
        target, kind = found
        try:
            body = read(target)
        except OSError as problem:
            return self._send(
                503, error_page(503, str(problem)).encode("utf-8"),
                "text/html; charset=utf-8",
            )
        if kind.startswith("text/html"):
            return self._send(200, body, kind, policy=POLICY)
        self._send(200, body, kind, overrides=FONT_HEADERS if kind.startswith("font/") else ASSET_HEADERS)

    def _studio_file(self, context, connection, tail: str) -> None:
        """sd:6920: one gallery file from a Studio project's root, an image under a sandbox (`studio.policy`, sd:7513); a
        seekable type answers a Range."""
        from . import studio

        if not self._session(context):
            return self._send(403, error_page(403, f"Reload the page to open a Studio file: {SESSION_ENDED}").encode("utf-8"),
                              "text/html; charset=utf-8")
        found = studio.located(connection, tail)
        if found is None:
            return self._send(404, error_page(404, "No such Studio file.").encode("utf-8"), "text/html; charset=utf-8")
        target, kind = found
        try:
            handle = open(target, "rb")  # noqa: SIM115 - closed below, after the answer streams
        except OSError as problem:
            return self._send(503, error_page(503, str(problem)).encode("utf-8"), "text/html; charset=utf-8")
        with handle:
            size = os.fstat(handle.fileno()).st_size
            ranged = target.suffix.lower() in studio.RANGED
            status, start, length = studio.byte_range(self.headers.get("Range") if ranged else None, size)
            overrides = {"Accept-Ranges": "bytes"} if ranged else {}
            if status == 416:
                overrides["Content-Range"] = f"bytes */{size}"
                return self._send(416, b"", kind, policy=studio.policy(target), overrides=overrides)
            if status == 206:
                overrides["Content-Range"] = f"bytes {start}-{start + length - 1}/{size}"
            self._started = True
            self._policy, self._overrides = studio.policy(target), overrides
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command == "HEAD":
                return None
            handle.seek(start)
            left = length
            while left > 0:
                chunk = handle.read(min(left, 1 << 16))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)
        return None

    def _lane_shot(self, tail: str) -> None:
        """sd:6728: one PNG the lane drew of a changed page, `<repo>/lane/logs/shots-<item>-<stamp>/<page>.png`."""
        from . import pack_module
        from .designs import ASSET_HEADERS, read, resolve

        try:
            root = pack_module("sd_lane").lane_root(dict(os.environ))
        except ImportError:
            root = None
        shape = re.fullmatch(r"[^/]+/lane/logs/shots-[^/]+/[^/]+\.png", tail)
        found = resolve(tail, root) if root is not None and shape else None
        if found is None:
            return self._send(404, error_page(404, "No such screenshot.").encode("utf-8"), "text/html; charset=utf-8")
        try:
            body = read(found[0])
        except OSError as problem:
            return self._send(503, error_page(503, str(problem)).encode("utf-8"), "text/html; charset=utf-8")
        self._send(200, body, found[1], overrides=ASSET_HEADERS)

    def _v2_asset(self, name: str, query: str = "") -> None:
        from .v2 import asset

        found = asset(name)
        if found is None:
            return self._send(404, error_page(404, "No such file.").encode("utf-8"), "text/html; charset=utf-8")
        body, kind = found
        self._send(200, body, kind, cache=caching.cache_control(body, parse_qs(query)), compress=True)

    def _moved(self, path: str, query: str) -> None:
        """A `/v2/` bookmark: 301 to the page's or asset's address since sd:2163, or the 404 of a path that names neither."""
        from .v2 import moved

        target = moved(path)
        if target is None:
            return self._send(404, error_page(404, "No such page.").encode("utf-8"), "text/html; charset=utf-8")
        location = f"{target}?{query}" if query else target
        self._send(301, b"", "text/plain; charset=utf-8", overrides={"Location": location})

    def _static(self, name: str, query: str = "") -> None:
        if name not in STATIC_FILES:
            return self._send(
                404, error_page(404, "No such file.").encode("utf-8"),
                "text/html; charset=utf-8",
            )
        body = (STATIC / name).read_bytes()
        guessed = mimetypes.guess_type(name)[0] or "application/octet-stream"
        kind = f"{guessed}; charset=utf-8" if guessed.startswith("text/") or guessed.endswith("+xml") else guessed
        self._send(200, body, kind,
                   cache=caching.cache_control(body, parse_qs(query)), compress=True)


class Listener(ThreadingHTTPServer):
    """A listening socket whose accept queue holds a page's burst of connections (sd:2131).

    socketserver's default backlog is 5. Tailscale serve takes a page's
    assets over one HTTP/2 connection and dials a backend connection for each
    at once; macOS resets a connect beyond the queue, and the proxy answers 502.
    """

    request_queue_size = socket.SOMAXCONN


class CodeChanged(Exception):
    """The build on disk is no longer the one this process loaded (sd:3018)."""


class DashboardServer(Listener):
    """Own both listeners so partial startup and shutdown leave no orphan socket.

    It also stops once its build changes on disk and the new build's start
    check passes, so the LaunchAgent's KeepAlive starts the new code; a stale
    process answered /health 503 until someone kickstarted it (sd:3018). A
    failed check keeps the old build serving: a replacement that cannot start,
    such as one ahead of the installed sd_db, would restart in a loop.
    """

    direct_server = None
    direct_thread = None
    #: Starts the slow reads in the background as serving begins (`_warm`, sd:5064); `main` sets it. None warms nothing.
    warm_up = None
    # The new build's start check as a command; `main` sets it for a --config start. None never restarts.
    restart_probe = None
    refused_build = None
    # The probe in flight: a thread that leaves `(build, reason)` in `probed` when it ends (sd:3530).
    probing = None
    probed = None
    # A check hashes the library and the package, about 30 ms.
    build_check_seconds = 30
    # The probe's own bound; it runs beside the accept loop, so no request waits on it.
    probe_seconds = 60

    #: The piece mirror's sweep (pack sd:7243, ruling on note #43370): `main` sets it; None sweeps nothing. A hand
    #: edit reaches the queue within `piece_sweep_seconds`. It runs in a thread, so a slow read never holds the tick.
    piece_sweep = None
    piece_sweep_seconds = 300
    piece_swept = float("-inf")
    piece_sweeping = None
    piece_sweep_late = False

    def _sweep_pieces(self):
        if self.piece_sweep is None or time.monotonic() - self.piece_swept < self.piece_sweep_seconds:
            return
        if self.piece_sweeping is not None and self.piece_sweeping.is_alive():
            if not self.piece_sweep_late:  # said once: a sweep that never ends stops every later one
                self.piece_sweep_late = True
                print(f"sd-dashboard: the piece sweep is still running after {self.piece_sweep_seconds} s; "
                      "the next one waits for it", file=sys.stderr, flush=True)
            return
        self.piece_swept, self.piece_sweep_late = time.monotonic(), False
        self.piece_sweeping = threading.Thread(target=self.piece_sweep, daemon=True, name="sd-dashboard-piece-sweep")
        self.piece_sweeping.start()

    def service_actions(self):
        super().service_actions()
        self._sweep_pieces()
        if self.restart_probe is None:
            return
        if self.probing is not None:
            if self.probing.is_alive():
                return
            (build, reason), self.probing, self.probed = self.probed, None, None
            if not reason:
                raise CodeChanged("the dashboard build changed on disk")
            self.refused_build = build
            print(f"sd-dashboard: the build changed on disk but cannot start; serving the old build: {reason}",
                  file=sys.stderr, flush=True)
            return
        if time.monotonic() - self.build_checked < self.build_check_seconds:
            return
        self.build_checked = time.monotonic()
        try:
            current = runtime.build_digests()
        except runtime.RuntimeRefused:
            return  # A checkout mid-update can lack a file; the next check sees it settled.
        if current in (self.RequestHandlerClass.loaded_build, self.refused_build):
            return
        # The probe takes a second or, with a slow git or Tailscale, up to its bound: on the accept loop every
        # request waited that long (sd:3530). A thread runs it; the next tick after it ends acts on its answer.
        self.probing = threading.Thread(target=self._probe, args=(current,), daemon=True,
                                        name="sd-dashboard-restart-probe")
        self.probing.start()

    def _probe(self, build):
        try:
            probe = subprocess.run(self.restart_probe, stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=self.probe_seconds, check=False)
            reason = "" if probe.returncode == 0 else (
                probe.stderr.strip().splitlines() or [f"exit {probe.returncode}"])[-1]
        except (OSError, subprocess.SubprocessError) as error:
            reason = str(error) or type(error).__name__
        except BaseException as error:  # noqa: BLE001 - an answer is owed, or the probe never ends
            reason = f"the probe failed: {type(error).__name__}: {error}"
        self.probed = (build, reason)

    def direct_listener_healthy(self):
        return self.direct_server is None or (
            self.direct_thread is not None and self.direct_thread.is_alive())

    def _close_direct(self):
        if self.direct_server is not None:
            if self.direct_thread is not None and self.direct_thread.is_alive():
                self.direct_server.shutdown()
                self.direct_thread.join()
            self.direct_server.server_close()

    def serve_forever(self, poll_interval=0.5):
        if self.warm_up is not None:
            self.warm_up()
        try:
            if self.direct_server is not None:
                self.direct_thread = threading.Thread(
                    target=self.direct_server.serve_forever, daemon=True,
                    name="sd-dashboard-ip")
                self.direct_thread.start()
                if not self.direct_thread.is_alive():
                    raise RuntimeError("The required dashboard IP listener did not start.")
            self.build_checked = time.monotonic()
            super().serve_forever(poll_interval=poll_interval)
        except BaseException:
            self.server_close()
            raise
        finally:
            self._close_direct()

    def server_close(self):
        try:
            self._close_direct()
        finally:
            super().server_close()


def _served_commit():
    """The checkout commit this process starts on, which /health reports (sd:3528); None when git cannot say."""
    try:
        return runtime.checkout_commit()
    except runtime.RuntimeRefused:
        return None


def build(database: Path | str | None = None, *, port: int = DEFAULT_PORT,
          host: str = "127.0.0.1", operations_backend=None,
          frontdoor: auth.FrontDoor | None = None, frontdoor_check=None,
          services_backend=None, ports_backend=None,
          peer_lookup=None, fleet_backend=None, metrics_backend=None, usage_backend=None, jev=None,
          bind=True) -> ThreadingHTTPServer:
    """Build the loopback server and optional, separately authenticated IP socket.

    `bind=False` builds both without binding: the restart probe's check, while the old server holds the ports.
    """
    if host != "127.0.0.1":
        raise ValueError("The dashboard binds 127.0.0.1 only.")
    if frontdoor is not None and not callable(frontdoor_check):
        raise ValueError("Configured remote access requires an ongoing private Serve check.")
    if frontdoor is not None and frontdoor.direct is not None:
        if peer_lookup is None:
            peer_lookup = runtime.peer_login
        if not callable(peer_lookup):
            raise ValueError("Direct access requires a current Tailscale peer lookup.")
    from . import v2

    handler = type("BoundDashboard", (Dashboard,), {
        "database": database, "session_secret": secrets.token_bytes(32),
        "operations_backend": operations_backend,
        "services_backend": services_backend,
        "ports_backend": ports_backend,
        "fleet_backend": staticmethod(fleet_backend) if fleet_backend else None,
        "metrics_backend": staticmethod(metrics_backend) if metrics_backend else None,
        "usage_backend": usage_backend,
        "jev_command": list(jev) if jev else None,
        "frontdoor": frontdoor,
        "frontdoor_check": staticmethod(frontdoor_check) if frontdoor_check else None,
        "peer_lookup": staticmethod(peer_lookup) if peer_lookup else None,
        "loaded_build": runtime.build_digests(),
        "served_commit": _served_commit(),
        "badges": v2.Badges(),
        "reads": threading.BoundedSemaphore(READ_SLOTS),
    })
    primary = DashboardServer((host, port), handler, bind_and_activate=bind)
    try:
        if frontdoor is not None and frontdoor.direct is not None:
            direct = frontdoor.direct
            direct_handler = type("DirectDashboard", (handler,), {"direct_listener": True})
            primary.direct_server = Listener((direct.address, direct.port), direct_handler, bind_and_activate=bind)
            primary.direct_server.runtime_owner = primary
        return primary
    except BaseException:
        primary.server_close()
        raise


def main(argv: list[str] | None = None, *, serve: bool = True) -> int:
    """Start the dashboard. `serve=False` runs every start step short of the bind, then returns 0 (sd:3018)."""
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description="The local workflow dashboard.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database", default=None)
    parser.add_argument("--config", type=Path, default=None)
    arguments = parser.parse_args(argv)
    frontdoor = None
    try:
        if arguments.config:
            config = runtime.read_config(arguments.config)
            if config["port"] != arguments.port:
                raise runtime.RuntimeRefused("Configured backend port differs from --port.")
            if arguments.database is None:
                arguments.database = config.get("database")
        runtime.installed_library(arguments.database)
        if arguments.config:
            frontdoor = runtime.load_frontdoor(arguments.config, arguments.port)
    except (runtime.RuntimeRefused, OSError, ValueError) as problem:
        parser.error(str(problem))
    options = {}
    if frontdoor is not None:
        def private_frontdoor_unchanged():
            return runtime.load_frontdoor(arguments.config, arguments.port) == frontdoor

        options = {"frontdoor": frontdoor, "frontdoor_check": private_frontdoor_unchanged}
    # Only the served dashboard asks Jev; every test build passes no command.
    options["jev"] = runtime.jev_command()
    # Only the served dashboard keeps the fleet's slow reads in a snapshot (sd:TBD); a test build reads its own seam.
    from .snapshot import Fleet

    options["fleet_backend"] = Fleet()
    options["metrics_backend"] = _metrics_kept(arguments.database)
    options["usage_backend"] = _usage_kept(arguments.database)
    server = build(arguments.database, port=arguments.port, bind=serve, **options)
    if not serve:
        server.server_close()
        return 0
    kept = (options["fleet_backend"], options["metrics_backend"], options["usage_backend"])
    server.warm_up = lambda: _warm(arguments.database, kept)
    server.piece_sweep = lambda: _piece_sweep(arguments.database)
    # A run a command started before the last stop is watched again, so its journal record ends (sd:5541).
    from . import command_journal

    command_journal.resume(arguments.database)
    if arguments.config:
        # The restart probe, two steps from this checkout. First its dashboard.sh preflight, which runs
        # its bootstrap.py and runtime. Then a fresh interpreter imports the serving code, as bootstrap.py
        # does, and runs this function with the same arguments short of the bind. Only the LaunchAgent's
        # --config start has a launchd to start the replacement. The pack's lib/ goes first, as there (sd:3279).
        probe = ("import sys; sys.path[:0] = [sys.argv.pop(1), sys.argv.pop(1)]; from sd_dashboard import server; "
                 "raise SystemExit(server.main(sys.argv[1:], serve=False))")
        server.restart_probe = ["sh", "-c", '"$1" preflight --config "$2" >/dev/null && shift 2 && exec "$@"', "sh",
                                str(runtime.HERE / "dashboard.sh"), str(arguments.config),
                                sys.executable, "-I", "-c", probe, str(runtime.pack_library()), str(runtime.HERE), *argv]
    host, port = server.server_address[:2]
    print(f"sd-dashboard on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except CodeChanged as changed:
        # Non-zero, and launchd's KeepAlive starts the new build.
        print(f"sd-dashboard: {changed}; exiting to restart", file=sys.stderr, flush=True)
        return 1
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - the entrypoint
    raise SystemExit(main())
