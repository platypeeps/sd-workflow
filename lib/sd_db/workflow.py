"""User task controls shared by the dashboard and CLI.

These functions write only the database. Repository files and external
trackers are optional context, never prerequisites for an ordinary task.
Validation and the writes it authorizes share a transaction, so stale forms
cannot overwrite a newer edit or leave history behind after a refusal.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date
from typing import Any

from . import repos
from .database import transaction
from .errors import SdDbError
from .reads import item_by_id, item_notes
from .recurrence import (
    ANCHORS,
    MAX_LEAD_DAYS,
    RecurrenceError,
    first_barren_start,
    next_after,
)
from .recurrence import parse as parse_rule
from .writes import (
    STATUSES,
    TransitionRefused,
    add_note,
    create_item,
    read_settings,
    refuse_base_status,
    resolve_note,
    set_item_fields,
    stamp,
    transition,
)

USER_FIELDS = frozenset({"title", "body", "priority", "due", "repo", "kind"})

#: The recurrence columns migrations 012 (sd:1099) and 041 (pack sd:5648)
#: added. `edit_item` accepts them beside `USER_FIELDS` but they stay out of
#: that set: the dashboard's details form offers exactly `USER_FIELDS`, its
#: `ItemRepository` test holds the two equal, and the form has no recurrence
#: control yet.
RECURRENCE_FIELDS = frozenset({"recurrence", "recurrence_anchor", "recurrence_lead"})

#: `branch`, which `edit_item` clears (sd:2818) or sets (sd:7659): a stale
#: name needs a way out, and an in_progress row that lost its branch needs a
#: way back, because the build (`build.py`) is the only other writer. Outside
#: `USER_FIELDS` for the same reason as `RECURRENCE_FIELDS`.
BRANCH_FIELDS = frozenset({"branch"})

#: `parked_at`: the nightly prune parks an untouched P4 item and `edit_item`
#: clears it (sd:3007); `edit_item` also parks by hand, given an aware
#: timestamp (sd:5905). A writing piece parks and revives through
#: `sd writing park`, which also moves its file.
PARK_FIELDS = frozenset({"parked_at"})

#: `area`, an item's own place (pack sd:8010): `home`, `hoa`, `personal` or a
#: realm id of the `dashboard.realms` setting row (pack sd:8062), kept as
#: `item.fields.area` rather than a column. The dashboard's `places.area_of`
#: reads it ahead of the repository rule, so a vault task can sit in Home
#: alone. `edit_item` sets and clears it against `areas`; `item_area` reads it.
AREA_FIELDS = frozenset({"area"})
AREAS = ("home", "hoa", "personal")
#: A realm id, as the dashboard's `places.parse_realms` takes one.
AREA_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
REALMS_KEY = "dashboard.realms"


def areas(connection: sqlite3.Connection) -> tuple[str, ...]:
    """`AREAS` and then each realm id the `dashboard.realms` row names, in
    line order (pack sd:8062): its `realm|<id>|<label>|<glob>` lines, less
    ids that are `AREAS` or `all`. A row never held, cleared or unreadable
    names no realm. The pre-row `realms.conf` file is the dashboard's alone."""
    try:
        text = read_settings(connection, [REALMS_KEY]).get(REALMS_KEY) or ""
    except SdDbError:
        text = ""
    found = dict.fromkeys(AREAS)
    for line in text.splitlines():
        fields = [field.strip() for field in line.split("|")]
        if (len(fields) == 4 and fields[0] == "realm" and all(fields[1:]) and AREA_ID.fullmatch(fields[1])
                and fields[1] != "all"):
            found.setdefault(fields[1])
    return tuple(found)


def item_area(item: Any) -> str | None:
    """An item row's own area, `fields.area`, or None: no fields, unreadable
    fields, or a value that is not an area id (pack sd:8010, sd:8062). A
    realm the setting no longer names still reads; the page places it."""
    try:
        fields = json.loads(item["fields"] or "{}")
    except (TypeError, ValueError, RecursionError):
        return None
    area = fields.get("area") if isinstance(fields, dict) else None
    return area if isinstance(area, str) and AREA_ID.fullmatch(area) else None

#: The item kinds a person may set by hand: the four the pack's `sd task add`
#: files (`ADD_KINDS`, `bin/sd_work.py`), for the reason that list gives. Each
#: other kind in `item.kind`'s CHECK has a producer -- `report` the job that
#: ran -- or is `idea`, which `writing.list_pieces` reads as a draft article
#: only when it carries a `piece`, or is `work`, which is retiring and has
#: none (pack sd:7064). A hand edit that made one would be a second way to
#: make that row,
#: and a hand edit that unmade one would take a row away from its producer.
HAND_KINDS = ("task", "followup", "work-idea", "personal-idea")

#: The hand kinds filed with no repository, which the pack's `sd task add`
#: reads to settle the repository question and `reads.backlog_items(repo=NO_REPO)`
#: reads as a group: the two ideas. `personal` left with its kind (sd:7004),
#: so a to-do is a task in a repository. `followup` left the
#: set on 2026-09-14 (sd:809): a followup from a code review is about one
#: repository, and with no repo a checkout's brief (`reads.brief_items`) and
#: `reads.backlog_items(repo=path)` never list it. A repository does not make
#: a followup runnable: `runner._item` refuses every kind outside
#: `runner.RUNNABLE_KINDS`, whatever repository and branch the row carries.
REPO_LESS_KINDS = frozenset({"work-idea", "personal-idea"})

#: The kinds whose user fields `edit_item` edits: title, priority, due,
#: repository and body, and `kind` beside them. A `work` row is further held to
#: its file owner until cutover, and only a `task` or `followup` body is edited
#: here. `followup` joined on 2026-09-14 (sd:809), so a followup can be moved
#: to the repository it is about. Every other kind may have its `kind` edited
#: alone, and its other fields keep their own editing workflow.
DETAIL_KINDS = ("task", "followup", "work")

#: The `item.fields` keys a producer writes onto a hand kind, each read back
#: by a reader that also keys on the row's kind. A row carrying one is a
#: produced row whatever its kind says, so its kind stays put:
#: `contribution` is written by `contributions.configure` and read by
#: `contributions._registered`, which takes only `contributions.KINDS` rows
#: -- a move away and back would skip its one-per-pull-request and
#: dependency-cycle checks; `skill_review` is written by `skills_catalog` onto the `task` rows
#: that promote, demote or apply proposals to a skill, which the runner and
#: `delivery_candidates` read by kind.
PRODUCED_FIELDS = ("contribution", "skill_review")

#: The assignment statuses that still hold an item. `ending` is one: the
#: runner has not released the checkout yet (`runner.begin_ending`). No runner
#: constant names the set; `runner.py` spells it inline.
ACTIVE_ASSIGNMENTS = ("queued", "running", "ending")

NOTE_KINDS = ("comment", "followup", "question", "decision", "proposal")
TASK_STATUSES = ("planning", "ready", "in_progress", "blocked", "done")

#: The kinds whose status a person moves with the task controls, through the
#: same five `TASK_STATUSES`. `followup` joined `task` on 2026-09-13 (sd:768):
#: it is a hand-filed item worked in its own right, and before that the task
#: controls offered it no status, so it could not be closed. `personal`
#: joined then too and left with its kind (sd:7004). `work` has its own
#: delivery rules; `idea`, `work-idea`,
#: `personal-idea` and `report` keep their own workflows and get no status
#: choices here.
TASK_STATUS_KINDS = ("task", "followup")


class WorkflowError(SdDbError):
    """A user request could not be applied."""


class MissingItem(WorkflowError):
    """The stable item ID does not exist."""


class MissingNote(WorkflowError):
    """The note ID does not exist."""


class StaleItem(WorkflowError):
    """The request was based on a state that has since changed."""


def _identifier(value: int, name: str) -> None:
    if type(value) is not int or value < 1:
        raise WorkflowError(f"{name} must be a positive integer")


def _text(value: Any, name: str, *, blank: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise WorkflowError(f"{name} must be text without NUL characters")
    if not blank and not value.strip():
        raise WorkflowError(f"{name} must not be blank")
    return value if blank else value.strip()


def json_safe(value: object) -> dict:
    """Encode for `json.dumps` what it otherwise refuses.

    sd:874. `item.fields` and `item.body` are declared `TEXT`. TEXT affinity
    converts every numeric storage class to text before storing, so an INTEGER,
    a REAL, a CAST and a bound Python number all come back from these columns
    as `str`. BLOB is the one storage class affinity leaves alone: it comes
    back as `bytes`, and `json.JSONEncoder` has no rule for `bytes`. So a BLOB
    holding perfectly valid JSON raised `TypeError: Object of type bytes is not
    JSON serializable` out of `item_state` -- before any caller reached its own
    parse, and past every `except` the dashboard's `do_GET` names, so its
    reader got no response at all rather than an error page.

    A `CHECK` on the column would settle the same premise, but it would reject
    rows the store already holds and it is a migration this does not need. All
    the hash owes the bytes is that they are covered and told apart: an object
    is never what a `str` column value dumps to, so a BLOB and the same bytes
    stored as text do not share a revision. `default` runs only for a value
    `json.dumps` would otherwise refuse, so every row that hashes today keeps
    the revision it has and no open edit goes stale.

    Public, because the revision is not the only dump of this readback. Every
    mutation returns `item_state`, and the dashboard serialises that state
    straight back to the browser -- so a hash that survives a BLOB while the
    response does not just moves the `TypeError` from the GET to the POST.
    One encoding for both, and the API answers rather than dropping the
    connection. Its readers take `id`, `title`, `kind`, `status`, `repo` and
    `parked_at` off the item and never `fields` or `body`, so the shape here
    only has to be unambiguous, which an object among strings is.
    """
    if isinstance(value, bytes):
        return {"sd_db_bytes_hex": value.hex()}
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def item_state(connection: sqlite3.Connection, item: int) -> dict:
    """Readback plus a revision covering content and note resolutions.

    Timestamps have one-second precision. Hashing the actual rows avoids
    accepting two different edits made during the same second as one state.
    """
    _identifier(item, "item")
    # A deferred read snapshot does not reserve the writer lock. Without it a
    # concurrent writer can place new notes beside an item row from before its
    # edit, producing a revision which never described a real database state.
    connection.execute("SAVEPOINT sd_workflow_state")
    try:
        row = item_by_id(connection, item)
        if row is None:
            raise MissingItem(f"no item {item}")
        result = {"item": dict(row), "notes": [dict(note) for note in item_notes(connection, item)]}
    finally:
        connection.execute("RELEASE sd_workflow_state")
    result["revision"] = _revision(result)
    return result


def _revision(state: dict) -> str:
    """The hash of an `{"item", "notes"}` readback; `reads.revisions` hashes with it too."""
    content = json.dumps(state, sort_keys=True, separators=(",", ":"),
                         default=json_safe).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _checked_state(connection: sqlite3.Connection, item: int, expected: str | None) -> dict:
    state = item_state(connection, item)
    if expected is not None:
        if not isinstance(expected, str):
            raise WorkflowError("expected_revision must be text")
        if expected != state["revision"]:
            raise StaleItem(f"item {item} changed; reload it before applying this change")
    return state


def schema_kinds(connection: sqlite3.Connection) -> tuple[str, ...]:
    """The kinds this store's `item.kind` CHECK allows, read from its schema.

    Read rather than listed, because the CHECK is the enforcement and the
    store may predate the migration that widened it: a kind the constant
    names and the schema lacks is refused here by name, not by an
    `IntegrityError` from inside the write.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'item'"
    ).fetchone()
    match = re.search(r"CHECK\s*\(\s*kind\s+IN\s*\(([^)]*)\)", row["sql"] if row else "")
    if match is None:
        raise WorkflowError("item.kind carries no CHECK this library can read")
    return tuple(re.findall(r"'([^']*)'", match.group(1)))


def _fields(connection: sqlite3.Connection, changes: dict) -> dict:
    if not isinstance(changes, dict) or any(not isinstance(key, str) for key in changes):
        raise WorkflowError("changes must be an object with named fields")
    unknown = set(changes) - USER_FIELDS - RECURRENCE_FIELDS - BRANCH_FIELDS - PARK_FIELDS - AREA_FIELDS
    if unknown:
        raise WorkflowError(f"fields cannot be edited here: {', '.join(sorted(unknown))}")
    result = dict(changes)
    if result.get("branch") is not None:
        result["branch"] = _text(result["branch"], "branch")
        if re.search(r"\s", result["branch"]):
            raise WorkflowError("branch must be one name without spaces")
    if result.get("parked_at") is not None:
        try:
            result["parked_at"] = stamp(result["parked_at"])
        except SdDbError as error:
            raise WorkflowError(f"parked_at must be an aware timestamp or null: {error}") from None
    if "area" in result and result["area"] is not None:
        known = areas(connection)
        if not isinstance(result["area"], str) or result["area"] not in known:
            raise WorkflowError(f"area must be one of {', '.join(known)}, or null")
    if "title" in result:
        result["title"] = _text(result["title"], "title")
    if "body" in result:
        result["body"] = {"text": _text(result["body"], "body", blank=True)}
    if "priority" in result:
        priority = result["priority"]
        if priority is not None and (type(priority) is not int or priority not in range(1, 5)):
            raise WorkflowError("priority must be 1, 2, 3, 4, or null")
    if "due" in result and result["due"] is not None:
        due = result["due"]
        try:
            parsed = date.fromisoformat(due)
        except (ValueError, TypeError):
            raise WorkflowError("due must be a valid YYYY-MM-DD date or null") from None
        if parsed.isoformat() != due:
            raise WorkflowError("due must be a valid YYYY-MM-DD date or null")
    if "repo" in result and result["repo"] is not None:
        repo = _text(result["repo"], "repo")
        found = repos.row_for(connection, repo)
        if found is None:
            raise WorkflowError(f"repository {repo!r} is not registered; register it or omit repo")
        result["repo"] = found["path"]
    if "kind" in result:
        kind = result["kind"]
        allowed = schema_kinds(connection)
        if not isinstance(kind, str) or kind not in allowed:
            raise WorkflowError(f"no item kind {kind!r}; the kinds are {', '.join(allowed)}")
    if result.get("recurrence") is not None:
        try:
            result["recurrence"] = parse_rule(result["recurrence"]).text()
        except RecurrenceError as error:
            raise WorkflowError(str(error)) from None
    anchor = result.get("recurrence_anchor")
    if anchor is not None and anchor not in ANCHORS:
        raise WorkflowError(f"recurrence_anchor must be {' or '.join(ANCHORS)}, or null")
    lead = result.get("recurrence_lead")
    # `type is int`: a bool is an int to `isinstance`, and True is not a day count.
    if lead is not None and (type(lead) is not int or not 0 <= lead <= MAX_LEAD_DAYS):
        raise WorkflowError(f"recurrence_lead must be a whole number of days from 0 to {MAX_LEAD_DAYS}, or null")
    return result


def _recurring(kind: str, current: dict, values: dict) -> dict:
    """Settle the rule, its anchor and `due` together, before anything is written.

    `current` is the row as it stands (empty for a new one) and `values` the
    validated changes; the result is `values` with the anchor filled in or
    cleared. A rule that could never fire is refused here rather than left
    inert: one on a row with no `due`, one on a kind `change_status` cannot
    complete, and one with no occurrence after its `due`.
    """
    values = dict(values)
    rule = values.get("recurrence", current.get("recurrence"))
    anchor = values.get("recurrence_anchor", current.get("recurrence_anchor"))
    if rule is None:
        for field in ("recurrence_anchor", "recurrence_lead"):
            if values.get(field) is not None:
                raise WorkflowError(
                    f"{field} needs a recurrence rule; set recurrence in the same write")
            if "recurrence" in values and current.get(field) is not None:
                values[field] = None
        return values
    if anchor is None:
        values["recurrence_anchor"] = anchor = "schedule"
    if kind not in TASK_STATUS_KINDS:
        raise WorkflowError(
            f"{kind} items cannot recur; only {', '.join(TASK_STATUS_KINDS)} items are "
            f"completed by the task controls, so clear recurrence first")
    due = values.get("due", current.get("due"))
    if due is None:
        raise WorkflowError(
            "a recurring item needs a due date; set due, or clear recurrence in the same write")
    try:
        parsed = parse_rule(rule)
    except RecurrenceError as error:
        raise WorkflowError(str(error)) from None
    # Before the occurrence check: this refusal names its remedy, and the
    # occurrence check would otherwise answer first for some due dates.
    if anchor == "completion":
        # The completion anchor restarts the series at the completion date,
        # which supplies every part the rule omits: the day, the month, and
        # the phase INTERVAL counts from. Checking against `due` proves
        # nothing about that date, so every start in a leap cycle is tried.
        # FREQ=YEARLY;BYMONTH=2 completed on a 30th asks for February 30th.
        barren = first_barren_start(parsed)
        if barren is not None:
            raise WorkflowError(
                f"recurrence {parsed.text()} cannot be completion-anchored: completed on a "
                f"date like {barren.isoformat()} it has no next occurrence, because that date "
                f"supplies the parts the rule leaves out; use recurrence_anchor schedule, or "
                f"add the missing BYMONTH or BYMONTHDAY, with INTERVAL=1 when a BY part "
                f"is set")
    try:
        next_after(parsed, date.fromisoformat(due))
    except RecurrenceError as error:
        raise WorkflowError(str(error)) from None
    return values


def _refuse_active_assignment(connection: sqlite3.Connection, item: int) -> None:
    active = connection.execute(
        f"SELECT id, status FROM assignment WHERE item = ? AND status IN "
        f"({', '.join('?' for _ in ACTIVE_ASSIGNMENTS)}) LIMIT 1",
        (item, *ACTIVE_ASSIGNMENTS),
    ).fetchone()
    if active:
        raise WorkflowError(f"item {item} has {active['status']} assignment {active['id']}")


def _branch_change(connection: sqlite3.Connection, row: Any, new: str | None) -> None:
    """Refuse a branch change while the runner owns the row (sd:2818), and a clear on in_progress (sd:7659).

    The runner reads the branch to claim a queued assignment and to restore a
    run, so the guards are: no active assignment and no unreleased runner
    run. `sd-docs-lint` rule 2 wants an in_progress item to record its branch,
    so a clear would leave a row the gate fails and no verb could repair; a new
    name replaces the old one in a single step instead.
    """
    item = row["id"]
    if new is None and row["status"] == "in_progress":
        raise WorkflowError(
            f"an in_progress item records its branch; item {item} would fail docs-lint: "
            f"set the new name with --branch NAME, or move the item out of in_progress first")
    _refuse_active_assignment(connection, item)
    leased = connection.execute(
        "SELECT 1 FROM runner_run JOIN assignment ON assignment.id = runner_run.assignment "
        "WHERE assignment.item = ? AND runner_run.released_at IS NULL LIMIT 1", (item,),
    ).fetchone()
    if leased:
        raise WorkflowError(f"item {item} has a retained runner lease; reconcile it before clearing its branch")


def _kind_change(connection: sqlite3.Connection, row: dict, new: str, repo: str | None) -> None:
    """Refuse a kind change that would take a row from its producer or reader.

    `_fields` settled that `new` is a kind the schema allows. What is left is
    whether a hand edit may make it, which kinds a row may leave, and what the
    row carries that a reader keys on. `repo` is the row's repository after
    this edit. The rule mirrors the create side, the pack's `ADD_KINDS`.
    """
    # Imported here rather than at the top: `sources` imports this module,
    # so the name is read when the rule runs and not while it is defined.
    from .sources.vault import SOURCE as VAULT

    old = row["kind"]
    if new not in HAND_KINDS:
        raise WorkflowError(
            f"{new} items are made by their own producer, not by an edit; "
            f"the kinds an edit can set are {', '.join(HAND_KINDS)}")
    if old == "idea" and row["piece"] is not None:
        raise WorkflowError(
            f"item {row['id']} is writing piece {row['piece']!r}; `writing.list_pieces` "
            f"selects kind = 'idea' AND piece IS NOT NULL, so its kind stays idea")
    if old == "idea" and row["source"] == VAULT:
        # An idea with no `piece` is a free idea *unless* the vault put it
        # there. A base row is an `idea` whose `fields.kind` names the plugin
        # kind, and `sd_db.records` lists it by that item kind, so a changed
        # kind would drop it from its plugin's list. The row has a producer;
        # that it carries no `piece` says only which producer.
        raise WorkflowError(
            f"item {row['id']} is a base row from the vault; its kind belongs to its plugin, "
            f"so change its stage or fields rather than its kind")
    if old not in HAND_KINDS and old != "idea":
        raise WorkflowError(f"{old} items are owned by their producer; their kind cannot be changed here")
    try:
        fields = json.loads(row["fields"] or "{}")
    except (TypeError, ValueError, RecursionError):
        fields = None
    if not isinstance(fields, dict):
        raise WorkflowError(f"item {row['id']} has unreadable fields; its kind cannot be changed here")
    owned = [key for key in PRODUCED_FIELDS if fields.get(key) is not None]
    if owned:
        raise WorkflowError(
            f"item {row['id']} carries {', '.join(owned)} metadata its producer reads by kind; "
            f"its kind cannot be changed here")
    _refuse_active_assignment(connection, row["id"])
    if new in REPO_LESS_KINDS and repo is not None:
        raise WorkflowError(
            f"a {new} item carries no repository; clear repo in the same edit to move this row to {new}")


def capture_task(
    connection: sqlite3.Connection, *, title: str, body: str = "",
    priority: int | None = None, due: str | None = None, repo: str | None = None,
    recurrence: str | None = None, recurrence_anchor: str | None = None,
    recurrence_lead: int | None = None, who: str, session: str | None = None,
) -> dict:
    """Capture one local task, including its initial status history.

    `recurrence` is an RRULE in `sd_db.recurrence`'s subset and needs `due`;
    `recurrence_anchor` defaults to `schedule` when a rule is given, and
    `recurrence_lead` is the days before `due` each later occurrence shows.
    `session` is the opening note's session, `who` when not given (sd:6023).
    """
    who = _text(who, "who")
    with transaction(connection):
        values = _fields(connection, {
            "title": title, "body": body, "priority": priority, "due": due, "repo": repo,
            "recurrence": recurrence, "recurrence_anchor": recurrence_anchor,
            "recurrence_lead": recurrence_lead,
        })
        values = _recurring("task", {}, values)
        item = create_item(connection, kind="task", status="planning", session=session or who, **values)
        return item_state(connection, item)


def edit_item(
    connection: sqlite3.Connection, item: int, changes: dict, *,
    who: str, expected_revision: str | None = None,
) -> dict:
    """Edit user fields without exposing status or internal source metadata.

    `kind` is the one field a row outside `DETAIL_KINDS` may have edited,
    alone or with `repo` cleared, since a repository-less kind needs that in
    the same edit: a kind change is how such a row is reclassified, and its
    other fields keep their own editing workflow. A kind change is recorded
    as a note naming both kinds and who made it, because it rewrites what the
    row means. An `idea` may have `repo` edited alone (sd:5142): the row keeps
    its kind, so a base row stays in its base and page, which `records` lists
    by kind and never by repository. A writing piece keeps its repository,
    which holds its file.
    """
    who = _text(who, "who")
    with transaction(connection):
        state = _checked_state(connection, item, expected_revision)
        row = state["item"]
        reclassify = (isinstance(changes, dict) and "kind" in changes
                      and set(changes) <= {"kind", "repo"} and changes.get("repo") is None)
        # A park or an unpark alone touches no detail, so any kind takes it.
        unpark = isinstance(changes, dict) and set(changes) == PARK_FIELDS
        move = isinstance(changes, dict) and set(changes) == {"repo"} and row["kind"] == "idea"
        if row["kind"] not in DETAIL_KINDS and not reclassify and not unpark and not move:
            raise WorkflowError(f"{row['kind']} items use their own editing workflow")
        if move and row["piece"] is not None:
            raise WorkflowError(f"item {item} is writing piece {row['piece']!r}; its repository holds its file, "
                                f"so the repository stays")
        values = _fields(connection, changes)
        if "parked_at" in values and row["piece"]:
            raise WorkflowError("a writing piece parks and revives through its own park control, "
                                "which moves its file")
        values = _park(item, row, values)
        values = _area(item, row, values)
        if "body" in values and row["kind"] not in ("task", "followup"):
            raise WorkflowError("body text can only be edited here for task and followup items")
        if "repo" in values and row["kind"] == "work" and values["repo"] != row["repo"]:
            raise WorkflowError("a work item's repository is part of its source identity and cannot be reassigned here")
        # Equal submissions should not manufacture activity or history.
        changed = {
            key: value for key, value in values.items()
            if row[key] != (json.dumps(value, sort_keys=True) if key == "body" else value)
        }
        if row["kind"] not in DETAIL_KINDS and changed and "kind" not in changed and not unpark and not move:
            # A repository clear rides only on a real reclassification.
            raise WorkflowError(f"{row['kind']} items use their own editing workflow")
        if "kind" in changed:
            _kind_change(connection, row, changed["kind"], values.get("repo", row["repo"]))
        if "branch" in changed:
            _branch_change(connection, row, changed["branch"])
        changed = _recurring(changed.get("kind", row["kind"]), row, changed)
        if changed:
            set_item_fields(connection, item, **changed)
            lines = []
            if "kind" in changed:
                lines.append(f"Changed kind {row['kind']} -> {changed['kind']} by {who}")
            if "parked_at" in changed:
                lines.append(f"{'Unparked' if changed['parked_at'] is None else 'Parked'} by {who}")
            # `fields` carries `area` alone here (`_area`), so the note names the area.
            fields = sorted({"area" if key == "fields" else key for key in changed} - {"kind", "parked_at"})
            if fields:
                lines.append(f"Updated {', '.join(fields)} by {who}")
            add_note(connection, item, "comment", "\n".join(lines), session=who)
        return item_state(connection, item)


def _area(item: int, row: Any, values: dict) -> dict:
    """`values` with `area` turned into the row's `fields`, that key set or
    dropped and every other key kept (pack sd:8010); refused on fields that do
    not read as an object, which a write would lose."""
    if "area" not in values:
        return values
    values = dict(values)
    area = values.pop("area")
    try:
        fields = json.loads(row["fields"] or "{}")
    except (TypeError, ValueError, RecursionError):
        fields = None
    if not isinstance(fields, dict):
        raise WorkflowError(f"item {item} has unreadable fields; its area cannot be set here")
    if fields.get("area") == area or (area is None and "area" not in fields):
        return values  # no change: no write, no note, the revision holds
    if area is None:
        del fields["area"]
    else:
        fields["area"] = area
    values["fields"] = json.dumps(fields, sort_keys=True) if fields else None
    return values


def _park(item: int, row: Any, values: dict) -> dict:
    """`values` with a park refused on a done item and dropped on a parked one (sd:5905).

    A parked item keeps its first stamp: a second park is a no-op, so it
    writes no note and the revision holds.
    """
    if values.get("parked_at") is None:
        return values
    if row["status"] == "done":
        raise WorkflowError(f"item {item} is done; only an open item parks")
    if row["parked_at"]:
        return {key: value for key, value in values.items() if key != "parked_at"}
    return values


#: One line of the audit comment `edit_item` writes. The name is one token: a
#: person's comment that starts "Updated priority by hand, ..." is not one.
AUDIT_LINE = re.compile(r"(?:Updated [a-z_]+(?:, [a-z_]+)*|Changed kind \S+ -> \S+|Unparked|Parked) by \S+")


def is_audit_note(note: Any) -> bool:
    """Whether `note` is a field-edit comment `edit_item` wrote, which readers fold (sd:3511)."""
    lines = str(note["body"] or "").splitlines()
    return note["kind"] == "comment" and bool(lines) and all(AUDIT_LINE.fullmatch(line) for line in lines)


def allowed_statuses(connection: sqlite3.Connection, item: int, *, state: dict | None = None) -> list[str]:
    """The status choices a write through `change_status` accepts.

    `state` is the item's `item_state` when the caller holds it already, read
    in the same snapshot, so a caller listing many items reads each history
    once (sd:2380); it must be this item's.

    A `TASK_STATUS_KINDS` item gets `TASK_STATUSES`; work that is not done
    gets every status but `done`; every other kind, and any item with a
    queued or running assignment, gets none. A vault base row is an `idea`,
    so it gets none: it moves by stage only (sd:3972).

    The dashboard item screen offers exactly these choices, but only on the
    panels it renders: `task` and `TASK_STATUS_KINDS` items always, a `work`
    item only while `progress.work_controls` gives no reason, and no other
    kind. `edit_item` accepts a followup's details since sd:809, and since sd:816 the screen renders the matching
    form instead of a hint naming the CLI's `sd task edit`.
    """
    if state is not None and state["item"]["id"] != item:
        raise WorkflowError(f"the state given is item {state['item']['id']}'s, not item {item}'s")
    row = (state if state is not None else item_state(connection, item))["item"]
    active = connection.execute(
        "SELECT 1 FROM assignment WHERE item = ? AND status IN ('queued', 'running') LIMIT 1",
        (item,),
    ).fetchone()
    if active:
        return []
    if row["kind"] in TASK_STATUS_KINDS:
        return list(TASK_STATUSES)
    if row["kind"] == "work":
        if row["status"] == "done":
            return []
        return [status for status in STATUSES if status != "done"]
    return []


#: The names a default branch goes by; a row on one has no branch of its own
#: to land, so a close needs no merge of it (the pack's `DEFAULT_BRANCHES`).
DEFAULT_BRANCHES = ("main", "master")
#: The comment `ship.note_merge` writes for each merge of an item.
CODE_DELIVERY = re.compile(r"Code delivery (\S+) at ([0-9a-f]{40,64})\b")
#: The sentence every delivering transition writes.
DELIVERED_AT = re.compile(r"delivered at ([0-9a-f]{40,64})\b")


def _refuse_unlanded_branch(state: dict, item: int) -> None:
    """Refuse a reasonless close of a row worked on its own branch (sd:1990, sd:2570).

    The build records the branch an item is worked on (`build.py`).
    Three such items were closed while their branches had no pull request,
    and the pack's `sd task status` now refuses that; the rule lives here so
    the dashboard's status write keeps it too. A recorded merge (the `Code
    delivery` comment or a delivering transition) closes the row as before,
    and so does any reason, which the transition records: the pack's
    `--delivered-by` arrives as its delivery sentence.
    """
    row = state["item"]
    branch = row.get("branch") or ""
    if not branch or branch.removeprefix("origin/") in DEFAULT_BRANCHES:
        return
    for note in state["notes"]:
        body = note.get("body") or ""
        if CODE_DELIVERY.match(body) or (note.get("kind") == "status_change" and DELIVERED_AT.search(body)):
            return
    raise TransitionRefused(
        f"item {item} was worked on branch {branch}, and no merge of it is recorded; "
        f"close it with a reason saying why no pull request is needed, or name the merge "
        f"commit that landed it as its delivery")


def change_status(
    connection: sqlite3.Connection, item: int, target: str, *, who: str,
    reason: str | None = None, expected_revision: str | None = None, session: str | None = None,
) -> dict:
    """Move a task, followup or active work item without bypassing delivery authority.

    `session` is the status note's session, `who` when not given (pack sd:6046).
    """
    who = _text(who, "who")
    if reason is not None:
        reason = _text(reason, "reason")
    if not isinstance(target, str) or target not in STATUSES:
        # sd:2741: 'dropped' or 'cancelled' is a close; name the verbs that record one.
        raise TransitionRefused(
            f"no status {target!r}; the statuses are {', '.join(STATUSES)}; "
            f"to close item {item} that nobody will do, cancel it with a reason")
    with transaction(connection):
        state = _checked_state(connection, item, expected_revision)
        row = state["item"]
        # Before the same-status no-op below, on purpose: criterion 7's clause
        # 7.19 refuses *every* status write while the runner owns the row,
        # and a write of the current status is a write. The only callers are
        # the dashboard's status route and the pack's `sd task status`; the
        # runner moves its item through `writes.transition` and never here.
        active = connection.execute(
            "SELECT id, status, scope FROM assignment WHERE item = ? AND status IN ('queued', 'running') LIMIT 1",
            (item,),
        ).fetchone()
        if active:
            # The refusal names the row and the way out, as an action each
            # surface renders as its own control (sd:3503). A queued row has no
            # process, so its cancel is the library's own. A running build's
            # cancel asks its run to stop (pack sd:6093). Any other running row
            # has no cancel: the runner's stop went with the runner (sd:3041),
            # and `operations.assignment_state` refuses it.
            if active["status"] == "queued":
                way_out = f"cancel queued assignment {active['id']} first"
            elif active["scope"] == "build":
                way_out = f"cancel running build assignment {active['id']} first"
            elif active["scope"] == "draft":
                way_out = "its draft is running; it ends within its 15-minute bound"
            else:
                way_out = "it is running, and no verb cancels a running assignment now that the runner is gone"
            raise TransitionRefused(f"item {item} has queued or running assignment {active['id']}; {way_out}")
        # Before the same-status no-op too: a base row's status is its plugin
        # map's answer to its stage, so no status write is its own (sd:3972).
        refuse_base_status(row)
        if row["status"] == target:
            return state
        if target == "ready":
            # A ready item is always buildable: an open question a draft run
            # asked is answered first (pack sd:6361, ruling #31097). Questions
            # from other sources keep the old rule.
            asked = [note for note in state["notes"] if note["kind"] == "question" and not note["resolved_at"]
                     and (note["session"] or "").startswith("draft:")]
            if asked:
                named = "; ".join(f"#{note['id']}: {(note['body'].strip().splitlines() or [''])[0]}" for note in asked)
                raise TransitionRefused(f"item {item} has {len(asked)} open draft "
                                        f"question{'s' if len(asked) > 1 else ''}; answer each first: {named}")
        if row["kind"] == "work":
            if row["status"] == "done":
                raise TransitionRefused("completed work is terminal; its delivery or cancellation history cannot be reopened here")
        if row["kind"] == "work" and target == "done":
            raise TransitionRefused("work completion requires verified delivery or cancellation evidence")
        if target not in allowed_statuses(connection, item):
            # sd:5065: a report closes by acknowledgement; name that way out.
            way_out = (f"; acknowledge report {item} instead, which can resolve its open followups too"
                       if row["kind"] == "report" and target == "done" else "")
            raise TransitionRefused(f"{row['kind']} item {item} cannot move to {target} through task controls{way_out}")
        if target == "done" and reason is None:
            _refuse_unlanded_branch(state, item)
        if row["status"] == "done":
            # A cancel receipt (sd:1005) belongs to the `done` it was written
            # with; a reopened task drops it, so a later plain `done` is not
            # read as cancelled. Unreadable fields are left for their repair.
            try:
                held = json.loads(row["fields"]) if row["fields"] else {}
            except ValueError:
                held = None
            if isinstance(held, dict) and "completion" in held:
                del held["completion"]
                set_item_fields(connection, item, fields=held)
        if target == "done":
            # sd:3505: what a done item still owed is no longer owed; the brief
            # and Today read only open notes, so close them with the move. A
            # question is not owed work: it waits on the operator's answer,
            # which a close does not give (pack sd:6329).
            owed = [note["id"] for note in state["notes"]
                    if note["kind"] == "followup" and not note["resolved_at"]]
            for note in owed:
                resolve_note(connection, note)
            if owed:
                closed = (f"resolved open note{'s' if len(owed) > 1 else ''} "
                          + ", ".join(str(note) for note in owed))
                reason = f"{reason}; {closed}" if reason else closed
        transition(connection, item, target, who=who, reason=reason, session=session)
        if target != "done" or row.get("recurrence") is None:
            return item_state(connection, item)
        spawned, ended = _next_occurrence(connection, row, who=who)
        return {**item_state(connection, item), "next_occurrence": spawned,
                "next_occurrence_reason": ended}


def _today() -> date:
    """The completion date: the machine's calendar day, as `due` is written."""
    return date.today()


def _series_start(row: dict, rule, anchor: str, today: date) -> date:
    """The date the next occurrence is searched from.

    A schedule anchor starts at the item's `due`. A completion anchor starts at the completion day,
    or at `due` when that is later: `next_after` is strictly after its start, so an early completion
    never finds the due date it closes again (pack sd:6230).
    """
    if anchor == "completion":
        return max(today, date.fromisoformat(row["due"])) if row["due"] is not None else today
    if row["due"] is None:
        raise RecurrenceError(f"recurrence {rule.text()} is schedule-anchored and the item has no due date")
    return date.fromisoformat(row["due"])


def next_occurrence_due(row: dict, *, today: date | None = None) -> str | None:
    """The due date that completing `row` today gives its next occurrence, or None.

    It makes the computation `_next_occurrence` makes when the completion
    lands, and writes nothing, so a page can name the date before it asks.
    None means no rule, or no next occurrence: the completion would end the
    series.
    """
    anchor = row["recurrence_anchor"] or "schedule"
    if row["recurrence"] is None or anchor not in ANCHORS:
        return None
    try:
        rule = parse_rule(row["recurrence"])
        return next_after(rule, _series_start(row, rule, anchor, today or _today())).isoformat()
    except (RecurrenceError, ValueError):
        return None


#: What a completion writes on the row it closes: every recurrence field, cleared.
CLEARED = dict.fromkeys(sorted(RECURRENCE_FIELDS))


def _next_occurrence(connection: sqlite3.Connection, row: dict, *, who: str) -> tuple[int | None, str | None]:
    """Create the row a completed recurring item owes, and move the rule to it.

    Called inside `change_status`'s transaction, after the completion. The
    new row copies what the obligation is -- kind, title, body, priority,
    repository -- and not what one instance of it carried: `branch`, `path`
    and the producer-owned `fields`, all but the area (pack sd:8010). The
    rule, its anchor and its lead move together (pack sd:5648): the lead is copied as stored, since a read treats
    a value `_fields` would refuse as open rather than hiding the row.
    Clearing the rule on the completed row is what makes a reopen and a
    second completion spawn nothing.

    `schedule` walks the series that starts at the completed row's `due` and
    takes the first occurrence after it. `completion` restarts the series at
    the completion date and takes the first occurrence after that, so it
    drifts by design: "every three years after the last time we did it". An
    early completion restarts at the closed row's `due` instead, so the next
    row never lands on the date of the one it closed (pack sd:6230).

    **The recurrence never blocks the completion.** When there is no next
    occurrence -- none within `HORIZON_YEARS`, a schedule-anchored row with
    no `due`, or a rule or anchor written around `_recurring` -- the
    completion still lands, no row is created, the rule is cleared as usual,
    and a note on the item says the recurrence ended and why. The write-time
    sweep in `_recurring` catches the common cases early; this is the
    guarantee, because a sweep over one leap cycle cannot see 2100, which is
    not a leap year. Returns the new row's id, or None and the reason.
    """
    anchor = row["recurrence_anchor"] or "schedule"
    start = None
    try:
        if anchor not in ANCHORS:
            raise RecurrenceError(f"recurrence_anchor {anchor!r} is not one of {', '.join(ANCHORS)}")
        rule = parse_rule(row["recurrence"])
        start = _series_start(row, rule, anchor, _today())
        due = next_after(rule, start).isoformat()
    except (RecurrenceError, ValueError) as error:
        searched = f", searched from {start.isoformat()}" if start is not None else ""
        reason = f"{error} ({anchor} anchor{searched})"
        set_item_fields(connection, row["id"], **CLEARED)
        add_note(connection, row["id"], "comment",
                 f"Recurrence ended: {reason}. No next occurrence was created; completed by {who}",
                 session=who)
        return None, reason
    spawned = create_item(
        connection, kind=row["kind"], title=row["title"], status="planning",
        repo=row["repo"], priority=row["priority"], due=due,
        recurrence=rule.text(), recurrence_anchor=anchor,
        recurrence_lead=row.get("recurrence_lead"), session=who,
    )
    if row["body"] is not None:
        set_item_fields(connection, spawned, body=row["body"])
    if (area := item_area(row)) is not None:
        # The area is the operator's, not the producer's: it recurs (pack sd:8010).
        set_item_fields(connection, spawned, fields={"area": area})
    set_item_fields(connection, row["id"], **CLEARED)
    add_note(connection, row["id"], "comment",
             f"Next occurrence: item {spawned}, due {due} ({rule.text()}, {anchor}-anchored) by {who}",
             session=who)
    add_note(connection, spawned, "comment",
             f"Recurs from item {row['id']}, completed by {who}", session=who)
    return spawned, None


def add_item_note(
    connection: sqlite3.Connection, item: int, *, body: str, kind: str = "comment",
    who: str, expected_revision: str | None = None, session: str | None = None,
    harness_session: str | None = None,
) -> dict:
    """Add a human note; status and execution history have their own writers.

    `session` is the note's session, `who` when not given (sd:6023).
    `harness_session` is the harness session beside a `session` that names
    the user, as a CLI ruling keeps (pack sd:6046).
    """
    who = _text(who, "who")
    body = _text(body, "body")
    if kind not in NOTE_KINDS:
        raise WorkflowError(f"note kind must be one of {', '.join(NOTE_KINDS)}")
    with transaction(connection):
        _checked_state(connection, item, expected_revision)
        note = add_note(connection, item, kind, body, session=session or who, harness_session=harness_session)
        state = item_state(connection, item)
        state["note"] = next(row for row in state["notes"] if row["id"] == note)
        return state


def resolve_item_note(
    connection: sqlite3.Connection, note: int, *, who: str,
    expected_revision: str | None = None,
) -> dict:
    """Resolve a user note, retaining its text and ID for later readback."""
    _identifier(note, "note")
    _text(who, "who")
    with transaction(connection):
        row = connection.execute("SELECT * FROM note WHERE id = ?", (note,)).fetchone()
        if row is None:
            raise MissingNote(f"no note {note}")
        _checked_state(connection, row["item"], expected_revision)
        if row["kind"] not in NOTE_KINDS:
            raise WorkflowError("status and execution history cannot be resolved as user notes")
        resolve_note(connection, note)
        state = item_state(connection, row["item"])
        state["note"] = next(found for found in state["notes"] if found["id"] == note)
        return state
