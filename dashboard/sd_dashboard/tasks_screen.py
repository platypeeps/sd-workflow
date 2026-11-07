"""Tasks: the rows and the Details reading behind the default UI's Tasks page (sd:2124).

The page is `v2/tasks.html`; it holds no rows. It reads two JSON documents,
both built here from the reads v1 already makes, so the port adds no second
query of the store:

- `/api/tasks` is `document`: every row `reads.backlog_items` returns for v1
  /backlog (unparked open items, and `done` for the week), then the open items
  the nightly prune parked (`parked`, its stamp; None on every other row), each with the
  revision a write sends back and the statuses `workflow.allowed_statuses`
  accepts for it, so the board refuses a move for the reason the library
  would, before it posts. Each row carries its `reads.age_bucket` key and the
  document names the buckets (`ages`), so the page's age filter is the one
  Operations' histogram counts with (sd:2589). Each row also carries whether `workflow.edit_item`
  takes its fields (`_edit_capability`), so Edit, P2, recurrence and a
  matrix drop are off, with the library's reason, where the edit would fail.
  The document's `attention` is the page's rail badge (sd:3446, `_attention`).
  A vault base row (sd:3977) also carries its `base` (`fields.kind`), `stage` and `score`; every other row has None.
  A repeating row carries its `recurrence_lead` and, from `backlog_items` (pack sd:5648), `opens_on` and `scheduled`:
  the page lists a scheduled occurrence only under its Scheduled scope (sd:5649). The document's `lead` says whether
  the store takes a lead; a store before schema 41 does not, and the page then shows no Lead field or column.
- `/api/tasks/<id>` is `details`: what `sd task show <id> --json` prints
  (`item`, `notes`, `revision`, from `workflow.item_state`), split into the
  status history and the other notes as v1's item page splits them, plus the
  item's assignments (`reads.item_assignments`, with the cancel capability
  and revision `sd assignments cancel` checks), a work
  item's relink and cancel availability (`progress.work_controls`) and its
  external context (`reads.item_shadow` and `progress.tracker_freshness`,
  v1's "External context" block), or `null` when the item has none. A vault base row's
  `base` is its stage moves, declared fields, body and mirror section (`_base`), or `null` for any other row.
  `row` is the item as `/api/tasks` would list it, and `left_out` why that list does not, or `null` (sd:5462).
- `from_backlog` is where a v1 `/backlog` address goes since sd:2356 retired
  it: the same `/tasks` view with the query v1 read, so a bookmark or an old
  link keeps its filters.

Every write the page makes goes through a route `server.action_route`
already answered for v1: status, edit (priority, due, recurrence), note
resolve, work relink and cancel, task cancel (`/cancel-task`, the bulk
Close of sd:3012), Unpark (the edit route with `parked_at: null`, sd:3012), and Park (the
same route with `parked_at` as now, sd:5906, which needs pack sd:5905).
Fix (`/fix`, sd:3456) is new, and Reports and Today post it too: `fix_session`
starts a herdr session for the item and adds a note. Draft email (`/draft-email`, sd:3463) is Tasks' alone:
`email_draft` has claude write an email about a task or followup and saves it as a Gmail draft, never sent, with a note
naming it. File GitHub issue and File Jira issue (`/file-issue`, sd:5103) are `issue_filing`'s: each row carries
`issue_targets`, where each would file or why it is off, and `issue_link`, the issue its ref names. Nothing here writes.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from sd_db import reads, repos, workflow
from sd_db.protection import github_slug
from sd_db.records import DOCUMENT_VIEWS
from sd_db.sources.vault import SOURCE as VAULT
from sd_db.workflow import WorkflowError

from . import issue_filing, sweep
from .markup import href

__all__ = ["details", "document"]

#: The board's columns, in order: `workflow.TASK_STATUSES`. Keys 1-5 follow it.
STATUSES = workflow.TASK_STATUSES


def _assignment_states(connection, *, now: str) -> dict[int, str]:
    """Item id -> `running` or `queued`, for an item with a live assignment; running wins.

    The lanes `reads.runner_board` gives v1's runner board, so no query here.
    """
    board = reads.runner_board(connection, now=now)
    live = {row["item"]: "queued" for row in board["queued"] if row["item"] is not None}
    live.update({row["item"]: "running" for row in board["running"] if row["item"] is not None})
    return live


def _edit_capability(row) -> dict:
    """Whether `workflow.edit_item` takes this row's fields, with the reason it gives when it refuses.

    The refusal `edit_item` makes for a field edit before it reads the changes: a kind outside
    `workflow.DETAIL_KINDS`. Without this, such a row showed Edit, P2 and the matrix drop, and every save failed
    (review, PR #46). `test_v2_tasks` checks it against `edit_item` itself.
    """
    reason = None
    if row["kind"] not in workflow.DETAIL_KINDS:
        reason = f"{row['kind']} items use their own editing workflow"
    return {"allowed": reason is None, "reason": reason}


def document(connection, *, now: str) -> dict:
    """The Tasks rows: one object per `reads.backlog_items` row, in its order.

    One read snapshot holds every read below (`operations_screen._one_snapshot`). Each was its own before, so an edit
    committed between the row read and its `item_state` paired the old fields with the new revision, and a write chosen
    from the old fields passed the revision check (review, PR #46).
    """
    from .operations_screen import _one_snapshot

    with _one_snapshot(connection):
        return _document(connection, now=now)


def _document(connection, *, now: str) -> dict:
    from .places import realms

    found = realms(connection)
    # The parked rows follow the listed ones (sd:3012): the page shows them only under its Parked chip, for Unpark.
    rows = reads.backlog_items(connection, now=now) + reads.backlog_items(connection, now=now, parked=True)
    out = _rows(connection, rows, found, now=now)
    # The histogram's buckets with nothing counted: their keys and the labels Operations draws on its bars.
    ages = [{"key": bucket.key, "label": bucket.label} for bucket in reads.age_histogram([], now=now)]
    return {"read": now, "statuses": list(STATUSES), "ages": ages, "rows": out, "attention": _attention(out, now=now),
            "lead": _has_lead(connection),
            # pack sd:6743: the idea kinds, off every row and count until Kind picks one (the Vault view).
            "ideas": list(reads.IDEA_KINDS),
            "realms": {key: found[key] for key in ("state", "source", "problems")},
            # The Personal and Work views' Run sweep (sd:5096, sd:5097): each realm's last run, and whether one runs now.
            "sweeps": {realm: sweep.status(None, realm) for realm in sweep.REALMS[1:]}}


def _rows(connection, rows, found, *, now: str) -> list[dict]:
    """One Tasks row per item row, in order: the shape `/api/tasks` lists and `details` gives an item it leaves out.

    `found` is `places.realms`, the realm rules each row's `realm` follows.
    """
    from .hoa_screen import checkout
    from .places import area_of, realm_of, report_realm
    from .screens import _repo_labels

    label = _repo_labels(row["repo"] for row in rows)
    live = _assignment_states(connection, now=now)
    hoa = checkout(connection)[0]
    rules = found["list"] if found["state"] in ("read", "partial") else None
    out = []
    filing = {}
    sources = None
    for row in rows:
        state = workflow.item_state(connection, row["id"])
        if row["kind"] == "report" and sources is None:
            sources = _report_sources(connection)
        if row["repo"] not in filing:
            filing[row["repo"]] = issue_filing.targets(connection, row["repo"])
        out.append({
            "id": row["id"], "title": row["title"], "kind": row["kind"], "status": row["status"],
            "priority": row["priority"], "due": row["due"],
            "repo": label(row["repo"]) if row["repo"] else None, "repo_path": row["repo"],
            "recurrence": row["recurrence"], "recurrence_anchor": state["item"]["recurrence_anchor"],
            # pack sd:5648: the lead in days (None is 0), and the day a scheduled occurrence opens. A store before 041 or
            # a pack before sd:5648 has neither: None and 0, and `_document`'s `lead` turns the Lead field off.
            "recurrence_lead": state["item"].get("recurrence_lead"),
            "opens_on": _column(row, "opens_on"), "scheduled": is_scheduled(row),
            "assignment": live.get(row["id"]),
            # The due date a completion today gives the next occurrence, for the confirm to name before it writes.
            "next_due": workflow.next_occurrence_due(state["item"]) if row["recurrence"] else None,
            "status_since": row["status_since"], "age": reads.age_bucket(row, now=now), "revision": state["revision"],
            # The matrix's urgency is `reads.is_urgent`, decided here. Without the due-date rule it is
            # `urgent_otherwise`, which the page keeps for a due edit made before the rows are read again.
            "urgent": reads.is_urgent(row, now=now),
            "urgent_otherwise": reads.is_urgent({**dict(row), "due": None}, now=now),
            # The row's own `item_state`, so each row reads its history once (sd:2380).
            "allowed": workflow.allowed_statuses(connection, row["id"], state=state),
            "edit": _edit_capability(row),
            "parked": row["parked_at"],
            # The place Tasks' Realm HOA and Home filter on: the HOA and Home pages' Tasks rule (`places.area_of`).
            # pack sd:8010: an item's own area (`fields.area`) wins, and `area_set` says so: such a row sits in its
            # area's tab alone, not in Personal or a realm tab.
            "area": area_of(row["repo"], row["kind"], hoa, own := workflow.item_area(state["item"])),
            "area_set": bool(own) and row["kind"] != "report",
            # The realm Tasks' Realm filter narrows to: Personal, Work and the others (sd:5064, `places.realm_of`).
            "realm": (report_realm(*_job_basis(state["item"]), rules, hoa, **sources) if row["kind"] == "report"
                      else realm_of(row["repo"], row["kind"], rules, hoa)),
            # File GitHub issue and File Jira issue (sd:5103): where each would file, or why it is off; and the
            # issue the item's ref names, for the row's link icon.
            "issue_targets": filing[row["repo"]], "issue_link": issue_filing.link_of(state["item"]),
            **_base_fields(state["item"]),
        })
    return out


def _report_sources(connection) -> dict:
    """What `places.report_realm` reads (pack sd:7774): each registered GitHub checkout by lower-case `owner/name`,
    and the report families; a family list that did not read is None, so no report gets a family's realm or Personal
    by it: its realm is unknown (pack sd:7792)."""
    from sd_db.today import repo_keys

    from .reports_screen import families

    read = families(connection)
    return {"checkouts": repo_keys(connection), "families": None if read["state"] == "error" else read["list"]}


def _job_basis(item) -> tuple[str | None, str | None]:
    """A report item's job and current attention basis, or Nones when its fields do not read. The basis is
    `repeats.basis` once a later run stored one, else the first run's, as `sd_db.reporting.CURRENT_BASIS` reads it; any
    failure to read the fields, such as nesting too deep or a value that is not text, is Nones (pack sd:7792)."""
    try:
        fields = json.loads(item["fields"] or "{}")
    except (ValueError, TypeError, RecursionError):
        return None, None
    report = fields.get("report") if isinstance(fields, dict) else None
    if not isinstance(report, dict):
        return None, None
    repeats = fields.get("repeats")
    basis = repeats.get("basis") if isinstance(repeats, dict) else None
    return report.get("job"), report.get("attention_basis") if basis is None else basis


def _column(row, name: str):
    """`row[name]`, or None when the read has no such column: `reads.backlog_items` before pack sd:5648, or `item_by_id`."""
    return row[name] if name in row.keys() else None


def is_scheduled(row) -> bool:
    """Whether a `reads.backlog_items` row is a scheduled occurrence (pack sd:5648), which a list shows only once it opens.

    Home and HOA Tasks, the HOA page, Metrics' age, Start next, Scoring and Decisions hide it, as Tasks does (sd:5670).
    A pack before sd:5648 gives no `scheduled`: nothing is scheduled, and every row shows as before.
    """
    return bool(_column(row, "scheduled"))


def _scheduled(connection, row, *, now: str):
    """`row` with the `opens_on` `reads.item_opens_on` gives (pack sd:5648) and `scheduled`, so Details' row is the list's.

    A pack before sd:5648 has no `item_opens_on`: the row as it is, which `_column` reads as not scheduled.
    """
    opens = getattr(reads, "item_opens_on", None)
    opens_on = opens(connection, row["id"]) if opens else None
    return {**dict(row), "opens_on": opens_on, "scheduled": int(opens_on > now[:10])} if opens_on else row


def _has_lead(connection) -> bool:
    """Whether an edit can write `recurrence_lead` (pack sd:5648): the store has the column and `edit_item` takes it."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(item)")}
    return "recurrence_lead" in columns and "recurrence_lead" in getattr(workflow, "RECURRENCE_FIELDS", ())


def _left_out(row, *, now: str) -> str | None:
    """Why `/api/tasks` leaves the item out, for Details to say when a link opens it there (sd:5462); None if it lists it.

    These are `reads.backlog_items`' rules: no report without `attention` (`_NOT_A_QUIET_REPORT`), nothing done before
    the week (`include_done_days`), no parked writing piece, and no row in a Documents view (`reads.NOT_A_DOCUMENT`,
    pack sd:6262). `test_v2_tasks` checks them against the rows it lists.
    """
    try:
        fields = json.loads(row["fields"] or "{}")
    except (TypeError, ValueError):
        fields = None
    if row["kind"] == "report" and not (isinstance(fields, dict) and fields.get("attention") is True):
        return "a report that needs no attention"
    view = reads.document_view(fields)
    if view:
        return f"a document: it shows under Documents > {DOCUMENT_VIEWS.get(view, view)}"
    cutoff = (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(days=7)).strftime("%Y-%m-%d")
    if row["status"] == "done" and not (row["status_since"] and row["status_since"] >= cutoff):
        return "done before the week"
    if row["parked_at"] and row["piece"]:
        return "a parked writing piece, which revives through its own workflow"
    return None


def _base_fields(item) -> dict:
    """A vault base row's kind, stage and score (sd:3977), None for every other row."""
    if item["source"] != VAULT:
        return {"base": None, "stage": None, "score": None}
    fields = json.loads(item["fields"] or "{}")
    return {"base": fields.get("kind"), "stage": item["stage"], "score": fields.get("score")}


def _attention(rows, *, now: str) -> dict:
    """The rail badge (sd:3446): open, unparked rows past their due day are warning, and those due within 2 days caution.

    A scheduled occurrence (pack sd:5648) counts once it opens, as Tasks lists it only then.

    Days are calendar days in this machine's zone, as the page's row glyphs count them in the browser on the same machine.
    """
    today = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone().date()

    def state(row) -> str:
        if row["status"] == "done" or row["parked"] or row.get("scheduled") or not row["due"]:
            return ""
        days = (date.fromisoformat(row["due"][:10]) - today).days
        return "warning" if days < 0 else "caution" if days <= 2 else ""

    from .v2.pages import attention

    return attention(map(state, rows), {"warning": "overdue tasks", "caution": "tasks due within 2 days", "ok": "nothing due"})


def _note(note) -> dict:
    return {"id": note["id"], "kind": note["kind"], "at": note["timestamp"], "body": note["body"],
            "session": note["session"], "resolved": note["resolved_at"]}


def _external(connection, row, *, now: str) -> dict | None:
    """v1's External context (`screens.external_context`) as data: None when the item has no tracker reference."""
    from sd_db import progress

    shadow = reads.item_shadow(connection, row["id"])
    if shadow is None and row["source"] not in ("github", "jira"):
        return None
    url = shadow["url"] if shadow else row["external_id"]
    if not url:
        return None
    tracker = shadow["tracker"] if shadow else row["source"]
    freshness = progress.tracker_freshness(connection, tracker=tracker,
                                          now=datetime.fromisoformat(now.replace("Z", "+00:00")))
    return {"tracker": tracker, "url": url, "state": shadow["state"] if shadow else None,
            "last_seen": shadow["last_seen"] if shadow else None,
            "freshness": {"state": freshness["state"], "last_success_at": freshness["last_success_at"],
                          "reason": freshness["reason"]}}


def _pull(connection, state) -> dict | None:
    """The item's linked pull request (its contribution `pull_url`), with the lane the contribution projection reads.

    A build in an unmanaged repository links its draft here (pack sd:7868); `needs_you` and `reasons` are the
    projection's, so an open draft the operator wrote reads "mark it ready for review". None when the item links none.
    """
    from sd_db import contributions

    try:
        url = (json.loads(state["item"]["fields"] or "{}").get("contribution") or {}).get("pull_url")
    except (ValueError, AttributeError):
        return None
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        return None
    try:
        found = next((row for row in contributions.projection(connection) if row.get("item_id") == state["item"]["id"]
                      and row.get("url") == url), None)
    except (contributions.WorkflowError, ValueError) as error:
        return {"url": url, "lane": None, "state": None, "needs_you": False, "reasons": [],
                "error": f"the contribution lane was not read: {error}"}
    found = found or {}
    return {"url": url, "lane": found.get("lane"), "state": found.get("external_state"),
            "needs_you": bool(found.get("needs_you")), "reasons": list(found.get("reasons") or []), "error": ""}


#: A file path a body or note names: absolute, `~/`, or repo-relative with at least one folder.
_NAMED_PATH = re.compile(r"(?<![\w./~-])((?:~?/)?[\w.-]+(?:/[\w.-]+)+\.(?:md|html))")


def _documents(connection, state) -> list[dict]:
    """The item's design and related documents: its path, its work folder, `docs/work` files naming `sd:<id>`,
    and files its body or notes name. A file in the checkout links to GitHub at the default branch; one in
    the gitignored `docs/dashboard` or outside the checkout shows its path only."""
    item = state["item"]
    root = Path(item["repo"]).expanduser() if item["repo"] else None
    registered = repos.row_for(connection, item["repo"]) if root else None
    slug = github_slug(registered["remote"]) if registered else None
    web = f"https://github.com/{slug[0]}/{slug[1]}" if slug else ""
    found: dict[Path, tuple[Path, str]] = {}  # resolved path -> (path as named, why)

    def add(path: Path, why: str) -> None:
        if path.is_file():
            found.setdefault(path.resolve(), (path, why))

    if root:
        if item["path"]:
            own = root / item["path"]
            add(own, "the item's path")
            if item["path"].startswith("docs/work/"):
                for sibling in sorted(own.parent.glob("*.md")):
                    add(sibling, "its work folder")
        names = re.compile(rf"\bsd:{item['id']}\b")
        for doc in sorted((root / "docs" / "work").rglob("*.md")):
            try:
                if names.search(doc.read_text(encoding="utf-8", errors="replace")[:200_000]):
                    add(doc, f"names sd:{item['id']}")
            except OSError:
                continue
    text = "\n".join([item["body"] or ""] + [note["body"] or "" for note in state["notes"]])
    for raw in _NAMED_PATH.findall(text):
        path = Path(raw).expanduser()
        if not path.is_absolute():
            if root is None:
                continue
            path = root / raw
        add(path, "named in a note")
    base = root.resolve() if root else None
    rows = []
    for resolved, (named, why) in found.items():
        rel = resolved.relative_to(base).as_posix() if base and resolved.is_relative_to(base) else None
        url = f"{web}/blob/HEAD/{rel}" if web and rel and not rel.startswith("docs/dashboard/") else ""
        rows.append({"path": rel or str(named), "why": why, "url": url})
    return rows


def details(connection, item: int, *, now: str) -> dict:
    """One item's Details sections. Raises `workflow.MissingItem` for an id with no item.

    `row` is the item as `/api/tasks` would list it, and `left_out` why that list leaves it out (`_left_out`): a link to
    an item the list leaves out opens it in Details from these (sd:5462). One read snapshot, as `document` has.
    """
    from .operations_screen import _one_snapshot

    with _one_snapshot(connection):
        return _details(connection, item, now=now)


def _details(connection, item: int, *, now: str) -> dict:
    from sd_db import operations, progress

    from .places import realms

    state = workflow.item_state(connection, item)
    row = reads.item_by_id(connection, item)
    assignments = []
    for assignment in reads.item_assignments(connection, item):
        held = operations.assignment_state(connection, assignment["id"])
        assignments.append({
            "id": assignment["id"], "role": assignment["role"], "provider": assignment["provider"],
            "status": assignment["status"], "started": assignment["started"], "ended": assignment["ended"],
            "usd": assignment["usd"], "estimated": bool(assignment["estimated"]),
            "revision": held["revision"], "cancel": held["capabilities"]["cancel"],
        })
    return {
        "read": now, "item": state["item"], "revision": state["revision"],
        "history": [_note(note) for note in state["notes"] if note["kind"] == "status_change"],
        "notes": [_note(note) for note in state["notes"] if note["kind"] != "status_change"],
        "assignments": assignments, "allowed": workflow.allowed_statuses(connection, item, state=state),
        "external": _external(connection, row, now=now),
        "pull": _pull(connection, state),
        "documents": _documents(connection, state),
        "published": _published(connection, state),
        # `sd work relink` and `sd work cancel` availability, as the mutation checks it under its lock
        # (`progress.work_controls`), for a work item; None for any other kind (sd:2200).
        "work": progress.work_controls(connection, item) if state["item"]["kind"] == "work" else None,
        "base": _base(state),
        "experiment": _experiment(connection, state),
        "row": _rows(connection, [_scheduled(connection, row, now=now)], realms(connection), now=now)[0],
        "left_out": _left_out(row, now=now),
    }


def _published(connection, state) -> list[dict]:
    """Where the item's work was published (sd:3465), in the design's order: each document in `fields.published`
    with its channel links, then `fields.writing.published_urls`, then each publication claim's url.

    The item row holds each URL (`channels.record_url`); `href` is the URL when it is a link the page may open, else None.
    """
    from sd_db.channels import CHANNELS

    fields = json.loads(state["item"]["fields"] or "{}")
    link = lambda channel, url: {"channel": channel, "url": url, "href": href(url, ("https", "http", "obsidian"))}  # noqa: E731
    out = []
    published = fields.get("published")
    for key, entry in (published.items() if isinstance(published, dict) else ()):
        if not isinstance(entry, dict):
            continue
        repo, _, document = key.rpartition("::")
        order = [c for c in CHANNELS if c in entry] + sorted(c for c in entry if c not in CHANNELS and c != "title")
        out.append({"title": str(entry.get("title") or document), "where": f"{Path(repo).name} · {document}",
                    "links": [link(c, str(entry[c])) for c in order]})
    writing = fields.get("writing")
    urls = writing.get("published_urls") if isinstance(writing, dict) else None
    if isinstance(urls, dict) and urls:
        out.append({"title": "Writing piece", "where": "writing targets",
                    "links": [link(str(target), str(url)) for target, url in urls.items()]})
    # sd:4639: the row a drain recorded in the team experiments database (pack sd:3982); sd owns its Status there
    # until `off`, which keeps the row and drops its database.
    experiments = fields.get("experiments")
    if isinstance(experiments, dict) and experiments.get("url"):
        where = {"experiment": "experiments database · sd keeps Status", "task": "tasks database · sd keeps Status"}.get(
            experiments.get("as"), "projection off · sd no longer writes it")
        out.append({"title": "Experiments row", "where": where,
                    "links": [link("notion-row", str(experiments["url"]))]})
    # A publication claim belongs to a writing piece; `writing.piece_state` reads them. A read that fails says so.
    item = state["item"]
    if item["kind"] == "idea" and item["piece"]:
        from sd_db import writing as pieces

        try:
            claims = pieces.piece_state(connection, item["id"])["writing"].get("publications") or []
        except (WorkflowError, ValueError, KeyError) as error:
            return out + [{"title": "Publication claims", "where": f"not read: {error}", "links": []}]
        out += [{"title": f"Publication claim {claim['id'][:8]}", "where": str(claim["phase"] or ""),
                 "links": [link("claim", str(claim["url"]))]} for claim in claims if claim["url"]]
    return out


def _experiment(connection, state) -> dict:
    """The item's experiments designation (sd:4639, pack sd:3982), as `sd task edit --experiment` spells it.

    `value` is `yes`, `of:<m>`, `off` or None; `choices` are the `of:<m>` values for each other designated experiment,
    none for an experiment with tasks (one level). A pack without the projection, or a designation that cannot be read,
    is `reason`, never read as no designation.
    """
    item = state["item"]
    try:
        import sd_db.projection as projection
    except ImportError:
        return {"value": None, "row": None, "choices": [], "reason": "this sd_db has no projection; update the command pack"}
    try:
        held = projection.designation(json.loads(item["fields"] or "{}"), item["id"])
        designated = projection.designated_rows(connection)
    except (projection.ProjectionRefusal, ValueError) as error:
        return {"value": None, "row": None, "choices": [], "reason": f"the designation cannot be read: {error}"}
    value = None if held is None else {"experiment": "yes", "off": "off"}.get(held["as"], f"of:{held.get('of')}")
    parent = any(other.get("of") == item["id"] and other["as"] == "task" for _, other in designated)
    choices = [] if parent else [{"value": f"of:{row['id']}", "label": f"task of #{row['id']} {row['title']}"}
                                 for row, other in designated if other["as"] == "experiment" and row["id"] != item["id"]]
    return {"value": value, "row": None if held is None else held.get("row"), "choices": choices, "reason": None}


def _base(state) -> dict | None:
    """A vault base row's panel (sd:3977): its stage moves, declared fields, body and mirror section.

    The moves and fields are the kind's rules (`records.rules_for`); a refusal there is `reason`, and the page offers no move.
    `mirror` is the row's section of `<vault>/docs/dashboard/base-<kind>.html`, with a Documents address when a root serves it.
    """
    item = state["item"]
    if item["source"] != VAULT:
        return None
    fields = json.loads(item["fields"] or "{}")
    kind = fields.pop("kind", None)
    out = {"kind": kind, "stage": item["stage"], "fields": fields,
           "body": json.loads(item["body"] or "{}").get("markdown", ""),
           "stages": [], "declared": [], "reason": None, "mirror": None}
    try:
        from sd_db import records

        rules = records.rules_for(kind, home=Path.home())
    except (ImportError, workflow.WorkflowError) as refused:
        out["reason"] = str(refused)
        return out
    out["declared"] = [name for name in rules.fields if name != "status"]
    if rules.driver != "db":
        out["reason"] = f"plugin {rules.prefix} still keeps {kind} rows in the vault: edit the note"
    else:
        graph = rules.transitions
        out["stages"] = list(graph.get(item["stage"], []) if graph is not None else
                             [stage for stage in rules.stages if stage != item["stage"]])
    if rules.vault is not None:
        from .documents import roots

        name = f"base-{kind}.html"
        folder = rules.vault / "docs" / "dashboard"
        served = next((root.key for root in roots() if root.path.expanduser().resolve() == folder.resolve()), None)
        out["mirror"] = {"path": f"{folder / name}#item-{item['id']}",
                         "url": f"/documents/{served}/{name}#item-{item['id']}" if served else ""}
    return out


#: The views v1 /backlog had; Tasks has the same three. v1 opened on the list, Tasks on the board.
BACKLOG_VIEWS = ("list", "board", "matrix")


def from_backlog(connection, parameters, *, now: str) -> str:
    """The `/tasks` address for a v1 `/backlog` query (sd:2356).

    Tasks reads the names v1 read (sd:2589, sd:2590): view, kind, status, age, active, q, page and skill. Its repo filter
    is the row's label, not the path v1 took, so a path becomes the label `_document` gives it. v1's run picks
    (`sel`) do not carry over: a run from a link is not a run the operator chose.
    """
    from urllib.parse import quote, urlencode

    from .screens import _repo_labels

    def one(key: str) -> str:
        return (parameters.get(key) or [""])[0]

    view = one("view")
    query = {"view": view if view in BACKLOG_VIEWS else "list"}
    for key in ("kind", "status", "age"):
        if one(key):
            query[key] = one(key)
    repo = one("repo")
    if repo == reads.NO_REPO_TOKEN:
        query["repo"] = "no repo"
    elif repo:
        label = _repo_labels(row["repo"] for row in reads.backlog_items(connection, now=now))(repo)
        query["repo"] = repo if label == "—" else label
    if one("active") == "1":
        query["active"] = "1"
    if one("q"):
        query["q"] = one("q")[:200]
    if one("page").isdigit() and int(one("page")) > 1:
        query["page"] = str(int(one("page")))
    if one("skill"):
        query["skill"] = one("skill")
    # %20, not +, for a space: every URL decoder reads %20 as a space; decodeURIComponent keeps a + as a plus.
    return "/tasks?" + urlencode(query, quote_via=quote)
