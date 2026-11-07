"""Task controls and workspace reads, backed by the dashboard's operations.

These verbs do not need a checkout. A new task takes the repository enclosing
the current directory by default; ``--no-repo`` files one that belongs to no
checkout, and ``--here`` refuses rather than filing a repo-less task.
``--kind`` files one of the other hand kinds: a ``followup`` takes a checkout
as a task does, and the two that carry no repository at all -- ``work-idea``
and ``personal-idea`` -- have the repository question already answered.
``edit --kind`` moves a row between the same four -- see
``LibraryKinds``.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import pathlib
import re
import stat
import sys
from typing import Any

import sd_handoff_rows
import sd_jev_shadow
import sd_lib
from sd_notes import ITEM_HELP, item_id, note_id


class WorkRefusal(Exception):
    """A workflow operation was refused without changing its state."""


class LibraryKinds:
    """`sd_db.workflow.HAND_KINDS`, read when argparse asks and not before.

    The item kinds a person files or sets by hand, which is five of the eleven
    `item.kind`'s CHECK carries. The library owns the list and says why each
    of the other six is left out -- each has a producer of its own, or is
    reserved, or is `idea`, which `writing.list_pieces` reads as a draft
    article. This pack kept its own copy as `ADD_KINDS` until sd:743 put the
    same tuple into `workflow` for `edit_item`, and two copies of one list are
    two answers the first time either moves. So `add --kind` and `edit --kind`
    both read the library's.

    Not read when the parser is built. `sd --help` answers in guest mode, and
    every entrypoint reaches `sd_db` through `sd_lib.import_sd_db`, whose
    second try is the only reason the library imports at all under the PATH
    `python3` (sd:745, sd:746) -- so `choices=workflow.HAND_KINDS` at
    construction would import the library on every invocation of every verb,
    or fail before the helper had run. argparse touches `choices` only to
    check a value (`in`) and to spell usage and errors (iteration), so this
    object answers those two and imports then.

    Absent, or a build that predates `HAND_KINDS`, the object offers nothing
    and refuses nothing: the verb's own `_library` and `_kinds` then refuse,
    naming the install to fix, which "invalid choice" about a valid kind
    would not. `register` assigns it after `add_argument`, because
    `add_argument` formats the metavar once -- iterating the choices -- to
    validate it.
    """

    def _read(self) -> tuple[str, ...]:
        imported = sd_lib.import_sd_db()
        if imported.module is None:
            return ()
        try:
            import sd_db.workflow as workflow  # noqa: PLC0415 - after the helper
        except ImportError:
            return ()
        return tuple(getattr(workflow, "HAND_KINDS", ()))

    def __contains__(self, value: object) -> bool:
        kinds = self._read()
        return not kinds or value in kinds

    def __iter__(self):
        return iter(self._read())


#: What `add --kind` and `edit --kind` read from `sd_db.workflow`, as
#: attributes. `REPO_LESS_KINDS` is the two of the hand kinds that carry no
#: repository -- `work-idea` and `personal-idea` -- read as a group by
#: `reads.backlog_items(repo=reads.NO_REPO)`. `followup` was one until sd:809,
#: and `personal` until its kind retired (sd:7004).
KIND_NEEDS = ("HAND_KINDS", "REPO_LESS_KINDS")


def _kinds(workflow: Any) -> None:
    """Refuse, naming the install, when the library predates the kind lists.

    `sd_db.workflow` imports on builds older than sd:743, so the import
    `_library` checks cannot stand in for this; the alternative is an
    `AttributeError` traceback naming a Python attribute.
    """
    missing = [name for name in KIND_NEEDS if not hasattr(workflow, name)]
    if missing:
        raise WorkRefusal(
            "the installed sd_db cannot file or reclassify item kinds; it is missing "
            + ", ".join(f"sd_db.workflow.{name}" for name in missing)
            + ". Update this pack checkout, whose lib/ is the sd_db bin/ runs, "
            "then run this again."
        )


def _task_repo(args: argparse.Namespace, connection: Any, workflow: Any) -> str | None:
    """Which checkout a new task owns, defaulting to the one enclosing cwd.

    Filing from inside a checkout and getting no repository is the mistake
    ``--here`` existed to prevent, and a flag nobody remembers prevents
    nothing: 41 of the rows it guards were filed without it. So the enclosing
    repository is the default and the flags are the exceptions --
    ``--no-repo`` for work that belongs to no checkout, ``--here`` to refuse
    rather than file a repo-less task by accident, ``--belongs-to`` to name
    another checkout (sd:3567).

    Two things make the default narrower than "whatever cwd is in".

    A linked worktree resolves to its main checkout. The worktree is a
    temporary path and the row outlives it, so storing the worktree would
    name a directory that is gone by the time anyone reads the row.

    An unregistered checkout falls back to no repository rather than
    refusing. `capture_task` rejects a repo the `repo` table does not carry,
    so a default that passed cwd through unconditionally would turn
    `sd task add` from a verb that works anywhere into one that fails in
    every checkout nobody has registered. A default may not break the
    command; only an explicit `--here` gets to refuse, and it says which of
    the two reasons applied.

    A kind in `workflow.REPO_LESS_KINDS` settles the question before any of that. Those
    two -- `work-idea` and `personal-idea` -- carry no repository by
    definition, and the group is read by asking for exactly that
    (`reads.backlog_items(repo=reads.NO_REPO)`), so an idea row that took
    the checkout cwd happened to be in would be missing from the one query
    written to list it. A `followup` is not one of them since sd:809: a
    followup from a code review is about the checkout it was filed in, so it
    takes the rule a task takes, `--here` and `--no-repo` included. The set
    is the installed library's, so a build older than sd:809 still files a
    followup with no repository. `--no-repo` is therefore
    implied rather than required: this function's own history is that a flag
    nobody remembers prevents nothing. `--here` is refused instead of ignored,
    because it asks for a repository the kind cannot have, and a flag that
    silently succeeds at the opposite of what it says is worse than absent.
    """
    belongs_to = getattr(args, "belongs_to", None)
    if args.kind in workflow.REPO_LESS_KINDS:
        if args.here:
            raise WorkRefusal(f"--here: a {args.kind} item carries no repository")
        if belongs_to is not None:
            raise WorkRefusal(f"--belongs-to: a {args.kind} item carries no repository")
        return None
    if args.no_repo:
        return None
    # `edit`'s option (sd:3567): the path is read by `_belongs_to`, and
    # `capture_task` refuses an unregistered checkout as `edit_item` does.
    if belongs_to is not None:
        return _belongs_to(belongs_to)
    root = sd_lib.repo_root()
    if root is None:
        if args.here:
            raise WorkRefusal("--here requires a Git checkout")
        return None
    repo = str(sd_lib.main_worktree_root(root))
    # Whichever form the row holds (sd:1439): `~/...` on a migrated store, the
    # absolute path on an older one. The row's own spelling is what goes back.
    found = sd_lib.repo_row(connection, repo)
    if found:
        return str(found["path"])
    if args.here:
        raise WorkRefusal(f"--here: {repo} is not a registered repository")
    return None


def _belongs_to(value: str) -> str:
    """Which checkout `--belongs-to` names, read the way `add` reads cwd.

    Not spelled `--repo`, and the reason is R10-D6 rather than taste:
    `tests/test_verb_inventory.py` refuses that option name anywhere under
    `bin/`, because a command that can be *pointed at* another checkout is one
    that can act on it. This flag only sets a field on a row -- the command
    still runs where the caller stands -- but `sd task add` met the same
    question one verb earlier and answered it without `--repo` too, in the
    `--here` / `--no-repo` pair. One family, one spelling; a second answer
    here would make the rule read as negotiable.

    A row's `repo` is a repository key -- `~/...` under `$HOME` (sd:1439) --
    and a foreign key into the `repo` table, so the argument has to become one
    before the library sees it.
    `--belongs-to .` from inside a checkout is the spelling a caller standing
    in the misfiled row's real repository will reach for, and a relative path
    is what a shell hands over; both resolve here rather than arriving as a
    string the `repo` table has never heard of.

    A linked worktree resolves to its main checkout for the reason `add` does:
    the worktree is a temporary path and the row outlives it. A path that is
    no checkout at all is passed through resolved, because a registered
    repository is whatever the `repo` table carries and this is not the place
    that decides -- `edit_item` refuses an unregistered path by name, and one
    rule with one owner is the point of the move going through the library.
    """
    try:
        path = pathlib.Path(value).expanduser()
    except RuntimeError as error:  # `~nobody/x`: a refusal, not a traceback (sd:1000)
        raise WorkRefusal(f"--belongs-to: cannot expand {value}: {error}") from None
    root = sd_lib.repo_root(path)
    if root is None:
        return sd_lib.stored_repo(path.resolve())
    return sd_lib.stored_repo(sd_lib.main_worktree_root(root))


def _repo_landed(args: argparse.Namespace, wanted: str | None, result: dict[str, Any]) -> None:
    """Refuse a `--belongs-to` or `--no-repo` whose row does not hold the checkout afterwards (sd:4740).

    A library that took the edit and wrote nothing returned the row as it was,
    and the verb printed it with exit 0: a failed write read as a pass. The
    row `edit_item` returns is the read-back, so the check costs no query.
    """
    held = result["item"]["repo"] or None
    if (held is None) if wanted is None else sd_lib.same_repo(held, wanted):
        return
    flag = "--no-repo" if wanted is None else f"--belongs-to {args.belongs_to}"
    raise WorkRefusal(
        f"{flag}: expected item {args.item} to belong to {wanted or 'no checkout'}, but it belongs to "
        f"{held or 'no checkout'}; the library reported the edit and did not make it")


def _edit_changes(args: argparse.Namespace) -> dict[str, Any]:
    """The fields `edit` was asked to set, or a refusal naming the omission.

    Lifted out of `run` rather than left inline: `edit` is the one verb whose
    arguments need work before the library sees them -- a cleared field is a
    `None` no `getattr` loop can distinguish from an absent one, and a
    checkout is a path that has to be resolved -- and `run`'s job is to pick
    the operation, not to do this.
    """
    changes: dict[str, Any] = {
        field: getattr(args, field) for field in ("title", "body", "priority", "due")
        if getattr(args, field) is not None
    }
    if args.clear_priority:
        changes["priority"] = None
    if args.clear_due:
        changes["due"] = None
    # `edit_item` refuses a clear on an in_progress row, which lint rule 2 fails (sd:2729, sd:7659).
    if args.branch is not None:
        changes["branch"] = args.branch
    if args.clear_branch:
        changes["branch"] = None
    # pack sd:8010: the item's own area, which the dashboard's Home or HOA tab
    # holds alone; `edit_item` keeps it in `fields` and refuses another value.
    if args.area is not None:
        changes["area"] = args.area
    if args.clear_area:
        changes["area"] = None
    # The field `add` sets and nothing could change afterwards. It is not in
    # the loop above because the flag carries a path and the row carries a
    # checkout; `_belongs_to` is the one step between them, and `edit_item` --
    # not this function -- decides whether the checkout is registered and
    # writes the note that records the move.
    if args.belongs_to is not None:
        changes["repo"] = _belongs_to(args.belongs_to)
    if args.no_repo:
        changes["repo"] = None
    # A row's kind, which `edit_item` guards and notes: the kinds it may set
    # and leave, the rows a producer still reads, and a repository-less kind
    # arriving with its repository cleared -- `--no-repo` in the same command,
    # because the library refuses a move that leaves one behind rather than
    # dropping it silently. None of those rules is repeated here.
    if args.kind is not None:
        changes["kind"] = args.kind
    # The rule and its anchor go through as typed. The grammar, the default
    # anchor, the kinds that may recur and the due date a rule needs are all
    # `edit_item`'s to refuse; a cleared rule clears its anchor there too.
    if args.recur is not None:
        changes["recurrence"] = args.recur
    if args.clear_recur:
        changes["recurrence"] = None
    if args.recur_anchor is not None:
        changes["recurrence_anchor"] = args.recur_anchor
    if args.recur_lead is not None:
        changes["recurrence_lead"] = args.recur_lead
    # `edit_item` refuses a writing piece and a done item, and leaves a parked
    # item's first stamp alone (sd:3007, sd:5905).
    if args.unpark:
        changes["parked_at"] = None
    if args.park:
        changes["parked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if not changes and not getattr(args, "experiment", None):
        raise WorkRefusal(f"edit requires a field to change; name one, such as "
                          f"`sd task edit {args.item} --due YYYY-MM-DD`; `sd task edit --help` lists them")
    return changes


def _edit(workflow: Any, args: argparse.Namespace, connection: Any, who: str,
          revision: str | None) -> tuple[dict, bool]:
    """`edit` through `edit_item`: the result, and whether the row's checkout moved."""
    changes = _edit_changes(args)
    if "kind" in changes:
        _kinds(workflow)
    moved = "repo" in changes
    before = workflow.item_state(connection, args.item)["item"] if args.park else None
    result = workflow.edit_item(
        connection, args.item, changes, who=who, expected_revision=revision)
    if before and before["parked_at"] and not args.json:
        # `edit_item` keeps a parked item's first stamp (sd:5905).
        print(f"already parked since {before['parked_at']}; not parked again")
    if moved:
        _repo_landed(args, changes["repo"], result)
    return result, moved


def _designate(sd_db: Any, args: argparse.Namespace, connection: Any, who: str,
               revision: str | None) -> dict:
    """`edit --experiment` (sd:3982): the item's experiments designation, alone.

    Alone, because the designation is written by `projection.designate` for
    any kind, in its own transaction, while the other fields go through
    `edit_item`; two writes in one command could land one and refuse the other.
    """
    if _edit_changes(args):
        raise WorkRefusal("give --experiment without other changes; edit the rest in a second command")
    import sd_db.projection as projection

    return projection.designate(connection, args.item, args.experiment, who=who, expected_revision=revision)


def _library():
    sd_db = sd_handoff_rows.library()
    try:
        import sd_db.workflow as workflow
    except ImportError as error:
        raise WorkRefusal(
            "the installed sd_db lacks workflow controls; update this pack checkou"
            "t, whose lib/ is the sd_db bin/ runs"
        ) from error
    return sd_db, workflow


#: The row belongs to no checkout, said as a sentence rather than as a blank.
#: Only `--no-repo` prints it: on an ordinary listing the absence is the answer
#: already, and a row saying "none" on every line is the noise this avoids.
NO_CHECKOUT = "belongs to no checkout"


def _standing_in(rows: list[Any]) -> str | None:
    """The checkout this command ran in, spelled the way a row carries it.

    Resolved through the same two steps `_task_repo` uses, so the comparison
    below is between one spelling and itself: a linked worktree answers as its
    main checkout, which is what `add` wrote into the row in the first place.

    Read once per command and only when some row names a repository, because
    it costs a `git` call and most of what this prints has nothing to compare
    against.
    """
    if not any(row.get("repo") for row in rows):
        return None
    root = sd_lib.repo_root()
    return None if root is None else sd_lib.stored_repo(sd_lib.main_worktree_root(root))


def _repo_line(row: Any, *, here: str | None, moved: bool) -> str | None:
    """The row's checkout, printed only where it tells the reader something.

    `repo` is the field `sd task edit --belongs-to` exists to change, and
    until this line it was the one field no output ever showed: the move
    landed in the database and the caller saw the same five words back
    (sd:452). Unconditionally, though, a path on every row of every listing is
    noise, and a field that is always there is a field nobody reads. So two
    cases, and no third.

    **A move prints it, always.** The change is what is being reported, and
    `--belongs-to .` inside the destination checkout is exactly the case the
    rule below would hide. Clearing it prints `NO_CHECKOUT` rather than
    nothing, for the same reason: the caller asked, so the answer is a
    sentence.

    **Otherwise, only a row that is somewhere else.** Standing in the row's
    own checkout, "which repository" is answered by where you are; standing
    outside every checkout, nothing answers it, so every row that names one
    says so. A row that belongs to no checkout prints nothing here -- there is
    no path to name, and `--no-repo` already said it where it mattered.

    No `sd_lib.display_fields` here, and deliberately: that function exists so
    a hand-written *membership* tuple stops dropping keys a producer adds
    (sd:602). This prints one named field under a fixed header sentence and
    keeps no tuple, so there is nothing for a new key to fall out of.
    """
    repo = row.get("repo")
    if moved:
        return f"  repo: {repo}" if repo else f"  repo: {NO_CHECKOUT}"
    return f"  repo: {repo}" if repo and not sd_lib.same_repo(repo, here) else None


def _name_refs(value: Any) -> None:
    """Copy the reference `sd task add --ref` filed onto each row as `ref` (sd:1902).

    Under its own key, so a reader of `sd today --json` need not know
    `REF_SOURCE`, and only on a row that has one.
    """
    for row in value if isinstance(value, list) else [value.get("item") or {}]:
        if row.get("source") == REF_SOURCE:
            row["ref"] = row["external_id"]


def _ref(item: Any, repo: Any) -> str:
    """`system sd:5103`, as a person reads an item (sd:5124); the rows came through `sd_db`, so it imports."""
    sd_lib.import_sd_db()  # the gateway first, as for every sd_db import here
    from sd_db.repos import item_ref

    return item_ref(item, repo)


def _area(row: dict[str, Any]) -> str | None:
    """The row's own area, `edit --area`'s (pack sd:8010), or None; a row read without `fields` has none."""
    if not row.get("fields"):
        return None
    sd_lib.import_sd_db()
    from sd_db.workflow import item_area

    return item_area(row)


def _print_rows(rows: list[dict[str, Any]], *, moved: bool = False, today: str | None = None) -> None:
    """One line per row, and its checkout, reference and rule; `today` marks a past due date `overdue` (sd:3501)."""
    here = _standing_in(rows)
    for row in rows:
        priority = f" · P{row['priority']}" if row.get("priority") else ""
        due = f" · due {row['due']}" if row.get("due") else ""
        overdue = " · overdue" if today and row.get("due") and row["due"] < today else ""
        print(f"{_ref(row['id'], row.get('repo'))}  {row['status']}  {row['title']}{priority}{due}{overdue}")
        line = _repo_line(row, here=here, moved=moved)
        if line:
            print(line)
        if row.get("ref"):
            print(f"  ref: {row['ref']}")
        area = _area(row)
        if area:
            print(f"  area: {area}")
        if row.get("recurrence"):
            lead = f", shows {row['recurrence_lead']}d early" if row.get("recurrence_lead") else ""
            opens = f" · opens {row['opens_on']}" if row.get("scheduled") else ""
            print(f"  recurs {row['recurrence']} ({row.get('recurrence_anchor')}{lead}){opens}")


def _emit(value: Any, *, machine: bool, moved: bool = False, following: Any = None,
          audit: bool = False, audited: Any = None) -> None:
    _name_refs(value)
    if machine:
        print(json.dumps(value, ensure_ascii=False))
        return
    rows = value if isinstance(value, list) else [value["item"]]
    if not rows:
        print("No items.")
    _print_rows(rows, moved=moved)
    if isinstance(value, dict) and "next_occurrence" in value:
        _occurrence_line(value, following)
    if isinstance(value, dict):
        _print_notes(value, None if audit else audited)
        print(f"revision: {value['revision']}")


def _print_notes(value: dict[str, Any], audited: Any) -> None:
    """The item's notes, with the field-edit comments `audited` names folded into one count (sd:3511).

    `audited` is None when the caller asked for every note; `--json` always keeps them all.
    """
    notes = value.get("notes", [])
    folded = [] if audited is None else [note for note in notes if audited(note)]
    for note in notes:
        if note in folded:
            continue
        resolved = " · resolved" if note.get("resolved_at") else ""
        print(f"  note #{note['id']} · {note['kind']}{resolved}: {note['body']}")
    if folded:
        print(f"  {len(folded)} field {'edit' if len(folded) == 1 else 'edits'} hidden; "
              f"sd task show {value['item']['id']} --audit shows them")


def _today_gate() -> Any:
    """`sd gate status --json`'s snapshot, or why it could not be read: a failed read is unknown, not idle."""
    import sd_gate_slots

    return sd_gate_slots.machine_snapshot(dict(os.environ))


def _print_today(connection: Any, repos: list[str | None] | None, *, show_all: bool = False) -> None:
    """`sd today` as text: `sd_db.today.sections` in the one layout `sd_views` gives it and `sd-status` (sd:5098)."""
    import shutil

    import sd_views

    sd_lib.import_sd_db()  # the gateway first, as for every sd_db import here
    from sd_db import today

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    found = today.sections(connection, repos=repos, now=now.strftime("%Y-%m-%dT%H:%M:%SZ"), gate=_today_gate())
    width = shutil.get_terminal_size((sd_views.DEFAULT_WIDTH, 24)).columns
    print(sd_views.render_sections(found, now=now, width=width, show_all=show_all), end="")
    line = _session_age_line(os.environ)
    if line:
        print(line)


def _session_age_line(env: Any, *, started: Any = None, today: datetime.date | None = None) -> str | None:
    """The nudge to end a Claude Code session begun before today (sd:6955); None when it is today's or unread.

    The start is `ps -o lstart=` of `$CLAUDE_PID`, local time: the rule "a lead session ends after its day's merges".
    """
    pid = env.get("CLAUDE_PID", "")
    if not pid.isdigit():
        return None
    if started is None:
        sd_lib.import_sd_db()  # the gateway first, as for every sd_db import here
        from sd_db.build import Probe

        started = Probe.started
    stamp = started(int(pid))
    try:
        begun = datetime.datetime.strptime(stamp or "", "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    if begun.date() >= (today or datetime.date.today()):
        return None
    zone = begun.astimezone().strftime("%Z")
    return (f"This session started {begun:%Y-%m-%d %H:%M} {zone}; "
            "end it after today's merges and start a fresh one from sd today.")


def _occurrence_line(value: dict[str, Any], following: Any) -> None:
    """What completing a recurring row did to its series (sd:1428).

    `change_status` adds `next_occurrence` and `next_occurrence_reason` only
    when the completed row recurred, so a caller that finished an ordinary
    task sees nothing new. A completion that ended the series says so with
    the library's reason: the obligation stops here, and a caller who reads
    only the status line would otherwise assume the next one exists.

    `following` is the new row, read by `run` from the store rather than
    parsed out of the note that names it, so the due date printed is the
    one the row carries.
    """
    if following is None:
        print(f"  recurrence ended: {value.get('next_occurrence_reason')}")
        return
    print(f"  next occurrence: {_ref(following['id'], following.get('repo'))} · due {following['due']}")


#: A delivery commit as `sd_db.progress` requires it to be spelled: the full
#: object name, lowercase, SHA-1 or SHA-256. Abbreviations are refused there
#: and are refused here, so `--delivered-by` and `sd work deliver` accept
#: exactly the same argument and a caller who learns one has learned both.
COMMIT_LENGTHS = (40, 64)
COMMIT_DIGITS = frozenset("0123456789abcdef")


def _is_commit(value: str) -> bool:
    """Whether `value` is a full lowercase object name, and nothing shorter."""
    return len(value) in COMMIT_LENGTHS and COMMIT_DIGITS.issuperset(value)


#: What `sd work deliver` writes on the transition it makes, and therefore
#: what `--delivered-by` writes on the transition a task makes. One sentence,
#: one format, so "which commit delivered this item" is one query over
#: `note.kind = 'status'` rather than one per kind.
DELIVERY_REASON = "delivered at {commit} on {ref}"


def _verified_tip(root: pathlib.Path) -> tuple[str, str]:
    """The default branch's current tip, fetched, and the ref it was read from.

    The same thing `sd_db.progress._delivery_evidence` establishes before it
    will record a delivery, asked here through `sd_lib.upstream` because a
    checkout with no remote still has a default branch and a feature branch's
    unpushed trailer must not stand in for its tip.
    """
    remote, default = sd_lib.upstream(root)
    if not remote:
        head = sd_lib.git_output(["symbolic-ref", "--quiet", "HEAD"], root)
        if head not in ("refs/heads/main", "refs/heads/master"):
            raise WorkRefusal("a local-only delivery must be verified on main or master; land the commit there, "
                              f"run `git -C {root} switch main`, then run this command again")
        tip = sd_lib.git_output(["rev-parse", "--verify", "HEAD^{commit}"], root)
        if not tip:
            raise WorkRefusal(f"{root} has no commit on {head} to verify against")
        return tip, head
    if sd_lib.git_output(["fetch", "--no-tags", remote, default], root) is None:
        raise WorkRefusal(f"could not fetch {remote} {default}; delivery cannot be verified")
    tip = sd_lib.git_output(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], root)
    if not tip:
        raise WorkRefusal(f"{remote}/{default} yielded no tip to verify against")
    return tip, f"{remote}/{default}"


def _delivery_reason(row: Any, commit: str, checkout: str | None = None,
                     trailer: str = "Delivers") -> str:
    """The delivery sentence for `commit`, or a refusal naming what failed.

    `sd work deliver` is the only writer of delivery evidence and it refuses a
    `kind=task` row outright, so an ordinary task closed with `task status
    done` recorded who and when and nothing about what shipped it. The
    verification is not the part that belonged to work items -- reachability
    and a `Delivers:` trailer are facts about a commit, not about a kind -- so
    it is asked here and the answer goes on the transition.

    A trailer git will not read back is named as that, and never as a missing
    one. `demoted_trailers` is the whole of sd:590 in this path: the blank line
    that costs an item its evidence looks exactly like an author who forgot.

    `checkout` is where the commit lives when that is not the row's own
    repository (sd:1569): an item filed in one checkout and fixed in another.
    The row keeps the checkout it was filed in, and the sentence names the
    other one, since `origin/main` alone would read as the row's branch.

    `trailer` is `Item` only for `sd work deliver --associated` on a task or
    followup (sd:1913): a merge prepared associate-only names the item and
    delivers nothing, and the operator's reason says it was the whole item.
    """
    if not _is_commit(commit):
        raise WorkRefusal(f"--delivered-by takes the full lowercase commit ID; "
                          f"`git rev-parse {commit}` in the checkout prints it")
    if not row["repo"] and not checkout:
        raise WorkRefusal(
            f"item {row['id']} belongs to no checkout, so no commit can be verified "
            "for it; `--delivered-in` names the checkout the commit landed in")
    elsewhere = checkout if checkout and not sd_lib.same_repo(checkout, row["repo"]) else None
    root = sd_lib.repo_disk(checkout or row["repo"])
    if not root.is_dir():
        raise WorkRefusal(f"{root} is unavailable; delivery cannot be verified")
    if sd_lib.git_output(["rev-parse", "--verify", f"{commit}^{{commit}}"], root) != commit:
        raise WorkRefusal(
            f"{root} has no commit {commit}" + ("" if checkout else
            "; a commit in another checkout is named with `--delivered-in <path>`"))
    tip, ref = _verified_tip(root)
    if sd_lib.git_output(["merge-base", "--is-ancestor", commit, tip], root) is None:
        raise WorkRefusal(f"{commit} is not reachable from {ref}")
    message = sd_lib.git_output(["show", "-s", "--format=%B", commit], root) or ""
    wanted = f"sd:{row['id']}"
    demoted = [line for line in sd_lib.demoted_trailers(message)
               if line.partition(":")[0] == trailer and line.partition(":")[2].strip() == wanted]
    if demoted:
        raise WorkRefusal(
            f"{commit} states {demoted[0]!r} outside the trailer block git reads, so "
            "nothing can see it; re-record it contiguously with the other trailers")
    block = sd_lib.trailer_block(message).splitlines()
    if not any(line.partition(":")[0] == trailer and line.partition(":")[2].strip() == wanted
               for line in block):
        raise WorkRefusal(f"{commit} carries no `{trailer}: {wanted}` trailer"
                          f"{_repair(row['id'], commit, block, trailer)}")
    reason = DELIVERY_REASON.format(commit=commit, ref=ref)
    return f"{reason} in {elsewhere}" if elsewhere else reason


def _repair(item: int, commit: str, block: list[str], trailer: str) -> str:
    """The way out for `Item:` alone: a merge prepared associate-only (sd:1913).

    A merge naming neither trailer, such as a no-item squash (sd:2171), can
    never verify for the item, so the refusal names the three ways out
    (sd:2565).
    """
    if trailer != "Delivers":
        return ""
    if f"Item: sd:{item}" not in block:
        return (f"; it names neither `Delivers: sd:{item}` nor `Item: sd:{item}`, so ship "
                f"the work with `sd-ship prepare --item {item} --deliver`, name `Closes: sd:{item}` "
                f"in a later item's `--deliver` merge body, or close it by hand with `--reason TEXT` and no "
                f"`--delivered-by`")
    return (f"; it carries `Item: sd:{item}`, so `sd work deliver {item} {commit} "
            "--associated --reason TEXT` delivers it")


def _delivered_in(connection: Any, value: str) -> str:
    """The registered checkout `--delivered-in` names, as the `repo` table spells it.

    Read the way `--belongs-to` reads a path, and held to a registered
    repository here rather than by the library, because nothing is written
    to the row's `repo`: the checkout only says where to verify the commit.
    """
    key = _belongs_to(value)
    found = sd_lib.repo_row(connection, key)
    if not found:
        raise WorkRefusal(f"--delivered-in: {sd_lib.repo_disk(key)} is not a registered repository")
    return str(found["path"])


def _status_reason(workflow: Any, connection: Any, args: argparse.Namespace) -> str | None:
    """What the transition records, verifying `--delivered-by` before it moves.

    Read and refused before the write, because a status that landed and then
    failed to record what shipped it is the hole the flag exists to close.
    """
    delivered_in = getattr(args, "delivered_in", None)
    if not args.delivered_by:
        if delivered_in:
            raise WorkRefusal("--delivered-in names where --delivered-by is verified; "
                              "it takes no effect alone")
        _refuse_unlanded_branch(workflow, connection, args)
        return args.reason
    if args.status != "done":
        raise WorkRefusal("--delivered-by belongs on the move to done")
    row = workflow.item_state(connection, args.item)["item"]
    kind = row["kind"]
    if kind == "work":
        raise WorkRefusal(
            f"item {args.item} is a work item; `sd work deliver {args.item} "
            f"{args.delivered_by}` records its delivery")
    if kind != "task" and not row["repo"]:
        # sd:772. `followup` takes task statuses since sd:768,
        # and every kind but `task` that carries no repository is one no
        # checkout can verify a commit for. Pointing at `sd work deliver`
        # sent the caller to a verb that refuses the same row. The close hint
        # is given only where the installed library says the kind can close:
        # a build older than `TASK_STATUS_KINDS` refuses `done` for these.
        closes = kind in getattr(workflow, "TASK_STATUS_KINDS", ("task",))
        raise WorkRefusal(
            f"{kind} item {args.item} belongs to no checkout, so --delivered-by has "
            "nothing to verify" + ("; close it without --delivered-by" if closes else ""))
    if kind != "task":
        raise WorkRefusal(
            f"{kind} item {args.item} takes no --delivered-by; only a task's "
            "move to done records one")
    checkout = _delivered_in(connection, delivered_in) if delivered_in else None
    return _delivery_reason(row, args.delivered_by, checkout)


#: The names a default branch goes by; a row on one has no branch of its own
#: to land, so a close needs no merge of it.
DEFAULT_BRANCHES = ("main", "master")


def _refuse_unlanded_branch(workflow: Any, connection: Any, args: argparse.Namespace) -> None:
    """Refuse a plain close of a row worked on its own branch (sd:1990).

    The build records the branch an item is worked on (`build.py`).
    Three such items were closed with a plain `sd task status done` while
    their branches had no pull request, and one never reached main. A close
    now needs a recorded merge (the `Code delivery` comment `sd-ship` writes,
    or a delivering transition), `--delivered-by` naming the merge, or
    `--reason` saying why no pull request is needed, which goes on the
    transition. A work item is left to the library, which sends it to
    `sd work deliver`.
    """
    if args.status != "done" or (args.reason or "").strip():
        return
    state = workflow.item_state(connection, args.item)
    row = state["item"]
    branch = row.get("branch") or ""
    if (row["kind"] == "work" or row["status"] == "done" or not branch
            or branch.removeprefix("origin/") in DEFAULT_BRANCHES or _delivered_by(state)):
        return
    raise WorkRefusal(
        f"item {args.item} was worked on branch {branch}, and no merge of it is recorded; "
        f"close it with --delivered-by <commit> naming the merge that landed it, "
        f"or with --reason saying why no pull request is needed")


def _change_status(workflow: Any, connection: Any, args: argparse.Namespace,
                   who: str) -> tuple[dict[str, Any], Any]:
    """The transition, and the row a recurring completion created (sd:1428).

    The second value is for the human line only; `--json` already carries
    its id as `next_occurrence`. None when nothing was created.

    A report's `done` is refused here with the command that closes one
    (sd:5065), after `--delivered-by` is refused with its own reason; the
    library's refusal names the action, never a verb (sd:3503).
    """
    reason = _status_reason(workflow, connection, args)
    if args.status == "done" and workflow.item_state(connection, args.item)["item"]["kind"] == "report":
        raise WorkRefusal(
            f"item {args.item} is a report, and a report closes by acknowledgement: "
            f"`sd reports acknowledge {args.item} --resolve-followups` also resolves its open followups")
    result = workflow.change_status(
        connection, args.item, args.status, who=who, session=_session(who),
        expected_revision=getattr(args, "if_revision", None), reason=reason)
    spawned = result.get("next_occurrence")
    following = None if spawned is None else workflow.item_state(connection, spawned)["item"]
    return result, following


def _refuse_task_delivery(workflow: Any, connection: Any, args: argparse.Namespace) -> None:
    """Refuse `deliver` on an ordinary task, saying where the evidence goes.

    The library refuses it one call later and says only that the operation is
    for work items, which leaves a caller holding a real merged SHA with
    nowhere to put it -- four items closed on 2026-09-12 lost theirs that way,
    and the workaround was a note written by hand. A dead end that knows the
    way out and does not say it is the defect here, not the kind check.
    """
    if workflow.item_state(connection, args.item)["item"]["kind"] == "task":
        raise WorkRefusal(
            f"item {args.item} is an ordinary task, and delivery evidence for one "
            f"is recorded by `sd task status {args.item} done --delivered-by "
            f"{args.commit}`, which verifies the same commit")


#: The comment `sd_db.ship.note_merge` writes for each merge of an item.
CODE_DELIVERY = re.compile(r"Code delivery (\S+) at ([0-9a-f]{40,64})\b")


def _delivered_by(state: dict[str, Any]) -> str | None:
    """What delivered a done row, as a reader would name it, or None.

    The latest merge comment names the pull request; a delivery without one
    still names its commit on the status transition.
    """
    notes = state.get("notes") or []
    for note in reversed(notes):
        found = CODE_DELIVERY.match(note.get("body") or "")
        if found:
            url = found.group(1)
            number = url.rstrip("/").rpartition("/pull/")[2]
            return f"#{number}" if number.isdigit() else url
    for note in reversed(notes):
        found = sd_lib.DELIVERED_AT.search(note.get("body") or "")
        if found and note.get("kind") == "status_change":
            return f"commit {found.group(1)[:12]}"
    return None


def _done_advisory(workflow: Any, connection: Any, item: int) -> None:
    """Warn on stderr when a note lands on a row that is already done (sd:1317).

    Two lanes once worked one item: the second posted its note after the
    first had delivered, and nothing remarked on it. The note is still
    written; this is advice at the cheapest point to catch a duplicate.
    """
    state = workflow.item_state(connection, item)
    row = state["item"]
    if row.get("status") != "done":
        return
    since = (row.get("status_since") or "")[:10]
    head = f"{_ref(item, row.get('repo'))} is done" + (f" since {since}" if since else "")
    delivered = _delivered_by(state)
    tail = f", delivered by {delivered}" if delivered else "; no delivery is recorded"
    print(f"sd: advisory: {head}{tail}; the note is recorded anyway", file=sys.stderr)


#: The kinds `sd work deliver --associated` closes by status rather than by a
#: work receipt: the two `sd-ship prepare --deliver` closes the same way.
ASSOCIATED_STATUS_KINDS = ("task", "followup")


def _deliver(progress: Any, workflow: Any, connection: Any, args: argparse.Namespace,
             who: str) -> Any:
    """`sd work deliver`, ordinary or after the fact (sd:1590).

    A whole-item merge prepared without `--deliver` lands `Item: sd:<id>` and
    no `Delivers:`, and nothing could close the row afterwards: `deliver_work`
    refuses the commit, `task status done` refuses a work item, and `prepare`
    on a merged item only reconciles. `deliver_associated_work` is the
    library's named way out. It is reached only through `--associated` with
    a reason, so an ordinary delivery never falls through to it, and the
    receipt records the trailer and the reason.

    A task or followup has no receipt for the library to write (sd:1913). Its
    `Item:` merge is verified as `--delivered-by` verifies a `Delivers:` one,
    and the move to done records the delivery sentence with the reason.
    """
    if not args.associated:
        if args.reason is not None:
            raise WorkRefusal("--reason belongs to --associated; an ordinary delivery records the commit only")
        _refuse_task_delivery(workflow, connection, args)
        return progress.deliver_work(
            connection, args.item, args.commit, who=who, expected_revision=args.if_revision)
    if not (args.reason or "").strip():
        raise WorkRefusal("--associated needs --reason: say why the merge carried Item: and not Delivers:, as "
                          f"`sd work deliver {args.item} {args.commit} --associated --reason TEXT`")
    state = workflow.item_state(connection, args.item)
    row = state["item"]
    if row["kind"] in ASSOCIATED_STATUS_KINDS:
        if row["status"] == "done":
            return _delivered_again(state, args.commit)
        sentence = _delivery_reason(row, args.commit, trailer="Item")
        return workflow.change_status(
            connection, args.item, "done", who=who, expected_revision=args.if_revision,
            reason=f"{sentence} (after the fact: {args.reason.strip()})")
    deliver = getattr(progress, "deliver_associated_work", None)
    if deliver is None:
        raise WorkRefusal(
            "the installed sd_db lacks deliver_associated_work; update this pack checkou"
            "t, whose lib/ is the sd_db bin/ runs")
    return deliver(connection, args.item, args.commit, who=who, reason=args.reason,
                   expected_revision=args.if_revision)


def _delivered_again(state: dict[str, Any], commit: str) -> dict[str, Any]:
    """A done task or followup: unchanged when its receipt names `commit`, else refused (sd:3346).

    Its move to `done` is the receipt, and a row already done does not move:
    `change_status` would write nothing and report success, and `sd-status`
    would go on calling the item unmarked.
    """
    if sd_lib.delivery_receipt(state.get("notes") or []) == commit:
        return state
    item = state["item"]["id"]
    raise WorkRefusal(
        f"item {item} is already done, so no move to done can record this delivery; "
        f"reopen it with `sd task status {item} planning`, then run this again")


def _cancel_guard(progress: Any, action: str) -> dict[str, Any]:
    """The guard `sd task cancel` hands `cancel_work` (sd:1005); none for work.

    `cancel_work` is the one cancellation the library has, and its default
    guard admits work rows only. A task or followup passes `task_guard`
    instead, so the row reads `done` with the same `cancelled` receipt a
    cancelled work item carries. A library from before the guard has neither
    the guard nor the keyword, so it is refused by name rather than reaching a TypeError.
    """
    if action == "cancel":
        return {}
    guard = getattr(progress, "task_guard", None)
    if guard is None:
        raise WorkRefusal(
            "the installed sd_db cannot cancel a task or followup; it is missing "
            "sd_db.progress.task_guard. Update this pack checkout, whose lib/ is the "
            "sd_db bin/ runs, then run this again.")
    return {"guard": guard}


def _capture(sd_db: Any, workflow: Any, args: argparse.Namespace,
             connection: Any, who: str) -> Any:
    """The row `add` writes, and the one step `capture_task` will not take.

    `capture_task` is the library's capture entry point and stays the one that
    makes the row: it validates the user fields, refuses a repository the
    `repo` table does not carry, and writes the opening status note. It also
    hard-codes `kind='task'`, and it is not this pack's function to widen --
    migration 009 added four item kinds and gave none of them a producer,
    which is the whole defect. So the kind is set immediately afterwards, by
    the library's own `set_item_fields`, whose allow-list names `kind`.

    Both writes are one unit. `transaction` takes a savepoint when the
    connection is already inside one, so nothing outside this function
    observes the row while it still reads `task`, and the returned state is
    re-read after the update rather than patched -- `revision` is computed
    from the row, and handing back the pre-update one would arm every
    `--if-revision` caller with a checkpoint that was already stale.

    A recurrence rule (sd:1428) is where that shortcut stops being enough.
    `capture_task` checks the rule against `task`, and only some kinds recur,
    so `set_item_fields` would leave a `work-idea` carrying a rule nothing
    could ever fire. With a rule and another kind, the kind goes through
    `edit_item` instead, which checks the rule against the kind the row ends
    up with and refuses by name. The row then carries the note that edit
    writes, which says what happened: captured as a task, then reclassified.

    A `--ref` (sd:1902) is read first, in the same transaction: a row that
    already carries the reference refuses the add and is left as it was
    (sd:3703). Updating it overwrote other work filed under one ref, done
    and cancelled rows too; `sd task edit` is the verb that changes a row.
    """
    _ref_checked(args)
    _priority_checked(args)
    with workflow.transaction(connection):
        if args.ref is not None:
            filed = sd_db.writes.item_by_external(connection, REF_SOURCE, args.ref)
            if filed is not None:
                raise WorkRefusal(f"sd:{filed['id']} ({filed['status']}) already carries --ref {args.ref}; "
                                  f"change it with `sd task edit {filed['id']}`, or file this occurrence "
                                  "under its own ref")
        state = workflow.capture_task(
            connection, title=args.title, body=args.body or "", priority=args.priority,
            due=args.due, repo=_task_repo(args, connection, workflow),
            recurrence=args.recur, recurrence_anchor=args.recur_anchor,
            recurrence_lead=args.recur_lead, who=who, session=_session(who),
        )
        item = state["item"]["id"]
        if args.ref is not None:
            sd_db.writes.set_item_fields(connection, item, source=REF_SOURCE, external_id=args.ref)
        if args.kind == "task":
            return state if args.ref is None else workflow.item_state(connection, item)
        if args.recur is not None:
            return workflow.edit_item(connection, item, {"kind": args.kind}, who=who)
        sd_db.writes.set_item_fields(connection, item, kind=args.kind)
        return workflow.item_state(connection, item)


#: The `item.source` of a row `sd task add --ref` filed (sd:1902). The whole
#: reference is the row's `external_id`, so the unique index on `(source,
#: external_id)` holds one row per reference, and a reference whose own source
#: part is `docs/work` or `cron-report` can never reach a row those sources own.
REF_SOURCE = "task-ref"


#: The kinds `sd task add` files without a priority: ideas are not ranked (sd:6726).
UNRANKED_KINDS = ("work-idea", "personal-idea")


def _priority_checked(args: argparse.Namespace) -> None:
    """Refuse a work item with no `--priority` (sd:6726): an unranked item closes after 30 days."""
    if args.priority is None and args.kind not in UNRANKED_KINDS:
        raise WorkRefusal(f"sd task add needs --priority 1-4 for a {args.kind} (1 most urgent, 3 the usual); "
                          "the nightly sweep closes an unranked item 30 days after filing")


def _ref_checked(args: argparse.Namespace) -> None:
    """Refuse a `--ref` the upsert cannot keep to one row per reference.

    Only `task` and `followup` rows take one. A recurring row hands its rule
    to a new row on completion, and a reference names one occurrence.
    """
    if args.ref is None:
        return
    source, _, ident = args.ref.partition(":")
    if not source.strip() or not ident.strip():
        raise WorkRefusal(f"--ref must be <source>:<id>, such as job:repo-sync:42; got {args.ref!r}")
    if args.kind not in ("task", "followup"):
        raise WorkRefusal(f"--ref files a task or a followup, not a {args.kind}")
    if args.recur is not None:
        raise WorkRefusal("--ref names one occurrence, so it does not take --recur")


#: `--since` durations: a count and a unit (sd:5065).
SINCE_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def since(value: str) -> datetime.datetime:
    """`store items --since`: `YYYY-MM-DD` as 00:00 UTC, or a positive duration back from now."""
    duration = re.fullmatch(r"([1-9][0-9]*)([mhdw])", value)
    if duration:
        try:
            return (datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(**{SINCE_UNITS[duration[2]]: int(duration[1])}))
        except OverflowError:
            pass
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return datetime.datetime.fromisoformat(value).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(
        f"{value!r}: give YYYY-MM-DD (00:00 UTC) or a duration such as 90m, 6h, 2d or 1w")


def _touched_since(row: dict[str, Any], cutoff: datetime.datetime) -> bool:
    """Whether the row was created or updated at or after `cutoff`; a stamp without a zone reads as UTC.

    An unreadable stamp keeps the row: unknown is not "older".
    """
    for stamp in (row.get("created_at"), row.get("updated_at")):
        if stamp:
            try:
                moment = datetime.datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                return True
            if (moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)) >= cutoff:
                return True
    return False


def _items(sd_db: Any, workflow: Any, args: argparse.Namespace, connection: Any) -> list[dict[str, Any]]:
    """`sd store items`, filtered as its flags ask.

    sd:3197. `--here` names the checkout as `task add --here` does, refusals included.
    """
    here = (_task_repo(argparse.Namespace(kind=None, no_repo=False, here=True), connection, workflow)
            if args.here else None)
    return [row for row in (dict(found) for found in sd_db.reads.backlog_items(connection, repo=here))
            if (not args.open or row["status"] != "done")
            and (not args.kind or row["kind"] == args.kind)
            and (not args.status or row["status"] == args.status)
            and (getattr(args, "since", None) is None or _touched_since(row, args.since))]


def _note_text(args: argparse.Namespace) -> str:
    """The note text, given as TEXT or as the older `--body`, and not both (sd:3500)."""
    if (args.text is None) == (args.body is None):
        raise WorkRefusal("give the note text as TEXT or --body TEXT, not both" if args.text is not None
                          else "give the note text: sd task note ITEM TEXT")
    return args.text if args.text is not None else args.body


#: Claude Code sets it to the session's id in every Bash call and hook (sd:6023).
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"


#: `<session>:<agent>`, which `bin/sd-session-beat` exports into a teammate's Bash call (pack sd:6046).
AGENT_ENV = "SD_AGENT_SESSION"


def _harness() -> str | None:
    """The harness session id, None outside a harness (sd:6023).

    A teammate shares its lead's id, so it is the `AGENT_ENV` mark instead,
    when the mark names this session: one inherited from another session
    marks nothing (pack sd:6046).
    """
    session = os.environ.get(SESSION_ENV)
    if not session:
        return None
    mark = os.environ.get(AGENT_ENV, "")
    return mark if mark.startswith(f"{session}:") and len(mark) > len(session) + 1 else session


def _session(who: str) -> str:
    """The session a note records: the harness session when set, else `who` (sd:6023).

    `bin/sd-session-trail` reads a session's own notes back by it after a compact.
    """
    return _harness() or who


def _note(workflow: Any, connection: Any, args: argparse.Namespace, who: str, revision: str | None) -> dict:
    body = _note_text(args)
    if getattr(args, "option", None) is not None and args.answers is None:
        raise WorkRefusal("--option needs --answers NOTE: it rules that review batch question")
    _done_advisory(workflow, connection, args.item)
    if args.answers is not None:
        return _answer(workflow, connection, args, body, who, revision)
    return workflow.add_item_note(connection, args.item, body=body, kind=args.kind, who=who,
                                  expected_revision=revision, session=_session(who))


def _answer(workflow: Any, connection: Any, args: argparse.Namespace, body: str, who: str,
            revision: str | None) -> dict:
    """`note --answers Q` (sd:5014): the ruling and its question resolved in one transaction.

    The same two writes, and the same `Ruling on note #Q: ` body, as the
    dashboard's Decisions answer; every refusal comes before the first write.
    A review batch question is ruled with `--option` instead (sd:5065).
    """
    if args.kind != "decision":
        raise WorkRefusal("--answers records a decision: add --kind decision")
    sd_lib.import_sd_db()  # the gateway first, as for every sd_db import here
    import sd_db.review as review  # noqa: PLC0415 - after the helper
    from sd_db.review import REF_SOURCE  # noqa: PLC0415 - after the helper

    with workflow.transaction(connection):
        asked = connection.execute("SELECT item, kind, resolved_at FROM note WHERE id = ?",
                                   (args.answers,)).fetchone()
        if asked is None:
            raise WorkRefusal(f"no note {args.answers}")
        if asked["item"] != args.item:
            raise WorkRefusal(f"note {args.answers} belongs to sd:{asked['item']}, not sd:{args.item}")
        if asked["kind"] != "question":
            raise WorkRefusal(f"note {args.answers} is a {asked['kind']}, not a question")
        if asked["resolved_at"] is not None:
            raise WorkRefusal(f"note {args.answers} is already resolved, at {asked['resolved_at']}")
        item = workflow.item_state(connection, args.item)["item"]
        if item["source"] == REF_SOURCE and (item["external_id"] or "").startswith("review:"):
            return _rule_batch(review, workflow, connection, args, body, who, revision)
        if getattr(args, "option", None) is not None:
            raise WorkRefusal(f"sd:{args.item} is not a review batch, so --option rules nothing: drop it")
        ruled = workflow.add_item_note(connection, args.item, kind="decision", who=who,
                                       body=f"Ruling on note #{args.answers}: {body}",
                                       expected_revision=revision, session=_session(who))
        state = workflow.resolve_item_note(connection, args.answers, who=who,
                                           expected_revision=ruled["revision"])
        state["note"] = ruled["note"]
        return state


def _rule_batch(review: Any, workflow: Any, connection: Any, args: argparse.Namespace, body: str, who: str,
                revision: str | None) -> dict:
    """`note --answers Q --option LABEL` on a review batch: `review.rule`, which may file a task (sd:5065).

    LABEL matches one of the question's options, case aside. TEXT other than
    the label is kept as the question's comment, written first in the same
    transaction, so a refused ruling leaves neither.
    """
    try:
        question = next((held for held in review.read(connection, args.item)["questions"]
                         if held["note"] == args.answers), None)
    except review.ReviewError:
        question = None
    options = [] if question is None else question["options"]
    named = f", one of {', '.join(options)}" if options else ""
    if args.option is None:
        raise WorkRefusal(f"sd:{args.item} is a review batch: name the ruling with --option LABEL{named}")
    option = next((held for held in options if held.casefold() == args.option.casefold()), None)
    if option is None:
        raise WorkRefusal(f"--option {args.option!r} is not an option of note {args.answers}"
                          + (f"; its options are {', '.join(options)}" if options else ""))
    if body.strip().casefold() != option.casefold():
        revision = workflow.add_item_note(connection, args.item, kind="comment", who=who,
                                          body=f"On note #{args.answers}: {body}",
                                          expected_revision=revision, harness_session=_harness())["revision"]
    if revision is None:
        revision = workflow.item_state(connection, args.item)["revision"]
    state = review.rule(connection, args.item, args.answers, option, expected_revision=revision, who=who,
                        session=_harness())
    state["note"] = state["ruling"]
    return state


def _today_repos(connection: Any, names: list[str] | None) -> tuple[list[str | None] | None, str]:
    """The `item.repo` keys `--only` names by short name, None for `fleet`, or the names it does not know (pack sd:6488)."""
    if not names:
        return None, ""
    sd_lib.import_sd_db()  # the gateway first, as for every sd_db import here
    from sd_db import repos

    known: dict[str, list[str | None]] = {}
    for row in repos.registered(connection):
        known.setdefault(repos.short(row["path"]), []).append(row["path"])
    known[repos.NO_REPO] = [None]
    unknown = [name for name in names if name not in known]
    if unknown:
        return None, (f"unknown repository {', '.join(map(repr, unknown))}; "
                      f"known: {', '.join(sorted(set(known) - {repos.NO_REPO}) + [repos.NO_REPO])}")
    return [key for name in names for key in known[name]], ""


def run_today(args: argparse.Namespace) -> int:
    """`sd today`: `today_items` as rows for `--json`, and the dashboard's Today sections for a person (sd:3501)."""
    sd_db, _ = _library()
    connection = sd_handoff_rows.connect(sd_db, write=False)
    try:
        keep, unknown = _today_repos(connection, args.only)
        if unknown:
            print(f"sd: {unknown}", file=sys.stderr)
            return 2
        if args.json:
            from sd_db import today

            _emit(today.work(connection, repos=keep), machine=True)
        else:
            _print_today(connection, keep, show_all=args.all)
        return 0
    except sd_db.SdDbError as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()


def run(args: argparse.Namespace) -> int:
    sd_db, workflow = _library()
    write = args.work_action not in {"items", "item"}
    connection = sd_handoff_rows.connect(sd_db, write=write)
    try:
        action = args.work_action
        who = getpass.getuser()
        revision = getattr(args, "if_revision", None)
        # Whether this command changed the row's checkout, which is the one
        # thing `_emit` cannot read off the result: a row that already sat in
        # the destination looks the same afterwards as one that just moved.
        # `following` is the row a recurring completion created, if any.
        moved, following, dedupe = False, None, None
        result: Any
        if action == "items":
            result = _items(sd_db, workflow, args, connection)
        elif action == "item":
            result = workflow.item_state(connection, args.item)
        elif action == "add":
            result, dedupe = _add(sd_db, workflow, args, connection, who)
        elif action == "edit" and getattr(args, "experiment", None):
            result = _designate(sd_db, args, connection, who, revision)
        elif action == "edit":
            result, moved = _edit(workflow, args, connection, who, revision)
        elif action == "status":
            result, following = _change_status(workflow, connection, args, who)
        elif action == "note":
            result = _note(workflow, connection, args, who, revision)
        elif action in {"relink", "cancel", "cancel-task", "deliver"}:
            import sd_db.progress as progress

            if action == "relink":
                result = progress.relink_artifact(
                    connection, args.item, args.path, who=who, expected_revision=revision)
            elif action in {"cancel", "cancel-task"}:
                result = progress.cancel_work(
                    connection, args.item, reason=args.reason, who=who,
                    expected_revision=revision, **_cancel_guard(progress, action))
            else:
                result = _deliver(progress, workflow, connection, args, who)
        else:
            raise WorkRefusal(f"unknown workflow operation: {action}")
        _emit(result, machine=args.json, moved=moved, following=following,
              audit=getattr(args, "audit", False), audited=workflow.is_audit_note)
        _dedupe(dedupe)
        return 0
    except sd_db.SdDbError as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()


def _add(sd_db: Any, workflow: Any, args: argparse.Namespace, connection: Any,
         who: str) -> tuple[dict, tuple[Any, Any, dict]]:
    """The filed row, and what the shadow duplicate hint (sd:2093) reads it from.

    `run` asks the hint after the row is filed and printed; Jev's pick goes to
    its ledger and is never read back. Nothing here reads for the hint: every
    read is `_dedupe_ask`'s, inside `shadow_safely` and after the switch.
    """
    _kinds(workflow)
    state = _capture(sd_db, workflow, args, connection, who)
    return state, (sd_db, connection, state)


#: The duplicate hint's stage, its name in the judgment ledger, and its shadow
#: answer: `add` looks for no duplicate today, which is `none`.
DEDUPE_STAGE = "JEV_SD_TASK_DEDUPE"
DEDUPE_CALLER = "sd-task-add"
NO_DUPLICATE = "none"
#: How many open items one add offers as candidates.
MAX_CANDIDATES = 40


def _dedupe(inputs: tuple[Any, Any, dict] | None, env: Any = os.environ) -> None:
    """Record which open item Jev reads as a duplicate of the one `_add` filed (sd:2093).

    Shadow only: `sd_jev_shadow.shadow_ready` says when. Sent: the new title,
    and the ids and titles of up to `MAX_CANDIDATES` open items of the same
    repository. Any failure is a note.
    """
    if inputs is not None:
        sd_jev_shadow.shadow_safely(DEDUPE_CALLER, DEDUPE_STAGE, sys.stderr, _dedupe_ask, *inputs, env)


def _dedupe_ask(sd_db: Any, connection: Any, state: dict, env: Any) -> None:
    """The hint itself. An item with no checkout has no repository whose
    visibility could make its titles sendable."""
    item = state["item"]
    if not item.get("repo"):
        return
    root = pathlib.Path(item["repo"]).expanduser()
    ready = sd_jev_shadow.shadow_ready(DEDUPE_STAGE, DEDUPE_CALLER, root, env, sys.stderr)
    if ready is None:
        return
    rows = sd_db.reads.backlog_items(connection, repo=item["repo"])
    shown = [(row["id"], _bare(row["title"])) for row in rows
             if row["status"] != "done" and row["id"] != item["id"]][:MAX_CANDIDATES]
    if not shown:
        return
    criteria = ",".join([f"{NO_DUPLICATE}=no listed item tracks the same work"]
                        + [f"sd-{number}={text}" for number, text in shown])
    sd_jev_shadow.shadow_ask(ready[0], "Which open item already tracks the same work as the new item?",
                      criteria, json.dumps({"new_item_title": _bare(item["title"])}), env, sys.stderr,
                      caller=DEDUPE_CALLER, stage=DEDUPE_STAGE, answer=NO_DUPLICATE, scope=ready[2],
                      subject=f"sd-task-dedupe:sd-{item['id']}")


def _bare(text: object) -> str:
    """One line of `text`, bounded, with no `,` to split a criteria list on."""
    return " ".join(str(text).replace(",", ";").split())[:sd_jev_shadow.MAX_TEXT]


def _recurrence_flags(parser: argparse.ArgumentParser, *, clear: bool = False) -> None:
    """`--recur`, `--recur-anchor` and `--recur-lead`, the same on `add` and `edit` (sd:1428).

    No `choices` and no grammar check: `sd_db.workflow` refuses a bad rule, a
    bad anchor, a rule with no due date and a kind that cannot recur, each by
    name, and a second copy here would be the one that drifts. The two anchor
    words are recited in the help because the parser may not import the
    library (`test_building_the_parser_imports_no_sd_db`).
    """
    rule = parser.add_mutually_exclusive_group() if clear else parser
    rule.add_argument("--recur", metavar="RRULE",
                      help="repeat on completion, e.g. FREQ=WEEKLY or FREQ=MONTHLY;INTERVAL=3 "
                           "(FREQ, INTERVAL, BYMONTH, BYMONTHDAY, BYDAY on WEEKLY); needs a due date")
    if clear:
        rule.add_argument("--clear-recur", action="store_true",
                          help="stop the series; clears the anchor too")
    parser.add_argument("--recur-anchor", metavar="ANCHOR",
                        help="schedule (default: the next due date follows the last one) or "
                             "completion (it follows the day the task was done)")
    # `type=int` only parses: the range and the rule it needs are the library's (pack sd:5648).
    parser.add_argument("--recur-lead", metavar="DAYS", type=int,
                        help="show each next occurrence this many days before its due date "
                             "(default 0: on the due date); needs a rule, and --clear-recur clears it")


def _audit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--audit", action="store_true",
                        help="print the field-edit notes ('Updated priority by ...') a plain read folds into a count")


#: What `--json` prints for one item: the id is `item.id`. A caller that read a top-level `id` printed
#: `None`, took the add for a failure and filed the item again (sd:5742).
ITEM_JSON_HELP = 'machine-readable: {"item": {"id": N, ...}, "notes": [...], "revision": ...}'


def _output(parser: argparse.ArgumentParser, action: str, *, revision: bool = False) -> None:
    single = action not in {"items", "today"}
    parser.add_argument("--json", action="store_true", help=ITEM_JSON_HELP if single else "machine-readable")
    if revision:
        parser.add_argument("--if-revision", help="refuse if the item changed since this revision")
    parser.set_defaults(handler=run, work_action=action)


def _contribution_library():
    sd_db = sd_handoff_rows.library()
    try:
        import sd_db.contributions as contributions
    except ImportError as error:
        raise WorkRefusal(
            "the installed sd_db lacks contribution controls; update this pack checkou"
            "t, whose lib/ is the sd_db bin/ runs"
        ) from error
    return sd_db, contributions


def _contribution_changes(path: str) -> dict[str, Any]:
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"nonfinite JSON number: {value}")

    try:
        # Nonblocking open also makes FIFO input refuse instead of waiting for a writer.
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("input must be a regular file")
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError("input exceeds 65536 bytes")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(value, dict):
            raise ValueError("input must be a JSON object")
        return value
    except (OSError, ValueError, RecursionError) as error:
        raise WorkRefusal(f"contribution input refused: {error}") from error


#: Display order only; membership comes from the row through
#: `sd_lib.display_fields`. The issue identity sits beside `url` and the three
#: draft keys beside the unfiled-branch keys they are the issue analogue of,
#: so an unfiled issue draft reads next to the unfiled branch work it resembles.
CONTRIBUTION_ORDER = (
    "url", "issue_url", "local_clone", "local_branch", "tested_commit",
    "target_repo", "draft_title", "draft_path", "draft_verified",
    "blocked_on", "depends_on", "evidence", "reasons", "event_ids", "attention_sources",
)

#: Printed by the trailer below for a `show` result, so the loop must not
#: print them again. `revision` reached the loop for the first time when
#: membership stopped being the tuple, and a snapshot then carried it twice.
SNAPSHOT_SHOWN = ("key", "attention", "notifications", "revision")

# Printed by the header lines above the loop, so the loop must not repeat them.
CONTRIBUTION_SHOWN = (
    "key", "lane", "title", "local_status", "external_state", "freshness", "evidence_verified",
)


def _emit_contributions(value: Any, *, machine: bool) -> None:
    if machine:
        print(json.dumps(value, ensure_ascii=False))
        return
    if isinstance(value, dict) and "item" in value:
        _emit(value, machine=False)
        print(f"contribution: item:{value['item']['id']}")
        return
    rows = value if isinstance(value, list) else [value.get("contribution")]
    trailer = SNAPSHOT_SHOWN if isinstance(value, dict) else ()
    for row in rows:
        if row is None:
            continue
        print(f"{row['key']}  {row['lane']}  {row['title']}")
        print(f"  local: {row.get('local_status')} · external: {row.get('external_state')}"
              f" · freshness: {row.get('freshness')}")
        print(f"  evidence_verified: {json.dumps(row.get('evidence_verified', False))}")
        # Order here, membership from the row: the seven keys this tuple did
        # not name were dropped in silence on every live contribution.
        for field in sd_lib.display_fields(
                row, CONTRIBUTION_ORDER, CONTRIBUTION_SHOWN + trailer):
            if sd_lib.display_value(row.get(field)):
                print(f"  {field}: {json.dumps(row[field], ensure_ascii=False)}")
    if not rows:
        print("No contributions.")
    if isinstance(value, dict):
        print(f"key: {value['key']}")
        for field in ("attention", "notifications"):
            print(f"{field}: {json.dumps(value.get(field), ensure_ascii=False)}")
        print(f"revision: {value['revision']}")


def run_contribution(args: argparse.Namespace) -> int:
    changes = _contribution_changes(args.file) if args.verb in {"add", "edit"} else None
    sd_db, contributions = _contribution_library()
    connection = None
    try:
        connection = sd_handoff_rows.connect(sd_db, write=args.verb not in {"list", "show"})
        who = getpass.getuser()
        result: Any
        if args.verb == "add":
            result = contributions.capture(connection, title=args.title, changes=changes, who=who)
        elif args.verb == "edit":
            result = contributions.configure(
                connection, args.item, changes, who=who, expected_revision=args.if_revision)
        elif args.verb == "list":
            result = contributions.projection(connection)
        elif args.verb in {"show", "ack", "dismiss"}:
            if args.verb == "show":
                result = contributions.snapshot(connection, args.key)
            elif args.verb == "ack":
                result = contributions.acknowledge(
                    connection, args.key, args.event, who=who, expected_revision=args.if_revision)
            else:
                result = contributions.dismiss(connection, args.key, who=who, expected_revision=args.if_revision)
            result = {**result, "contribution": next(
                (row for row in contributions.projection(connection)
                 if row["key"] == result["key"] or f"item:{row.get('item_id')}" == result["key"]
                 or any(source["key"] == result["key"] for source in row.get("attention_sources", []))),
                None,
            )}
        else:
            raise WorkRefusal(f"unknown contribution operation: {args.verb}")
        _emit_contributions(result, machine=args.json)
        return 0
    except sd_db.SdDbError as error:
        raise WorkRefusal(str(error)) from error
    finally:
        if connection is not None:
            connection.close()


#: The three key forms, recited because the parser is built before the library
#: is known to be installed -- `sd --help` answers in guest mode, so this string
#: cannot be derived from `sd_db.contributions._key`, which is its authority and
#: which refuses anything else with the same three names. Recited and not
#: derived is a cost, named here rather than hidden: if the library grows a
#: fourth form, this string is the place that will not know.
CONTRIBUTION_KEY_HELP = (
    "an item id (234, sd:234 or item:234), github:https://github.com/OWNER/REPO/pull/NUMBER, "
    "or issue:https://github.com/OWNER/REPO/issues/NUMBER"
)


def _register_contributions(verbs: Any) -> None:
    group = verbs.add_parser("contribution", help="track local work and upstream contribution attention")
    actions = group.add_subparsers(dest="verb", required=True)
    for action in ("add", "edit", "list", "show", "ack", "dismiss"):
        parser = actions.add_parser(action)
        parser.add_argument("--json", action="store_true", help="machine-readable")
        parser.set_defaults(handler=run_contribution)
        if action in {"add", "edit"}:
            parser.add_argument("--file", required=True, help="contribution changes as JSON (maximum 64 KiB)")
            if action == "add":
                parser.add_argument("title")
            else:
                parser.add_argument("item", type=item_id, help=ITEM_HELP)
                parser.add_argument("--if-revision", help="item revision from sd store item")
        if action in {"show", "ack", "dismiss"}:
            parser.add_argument("key", help=CONTRIBUTION_KEY_HELP)
        if action == "ack":
            parser.add_argument("--event", action="append", required=True, help="observed event ID; repeat as needed")
        if action in {"ack", "dismiss"}:
            parser.add_argument("--if-revision", required=True, help="checkpoint revision from contribution show")


def register(groups: Any, store: Any) -> None:
    """Extend the existing parser, preserving the plugin note-store verbs."""
    today = groups.add_parser(
        "today", help="reports needing attention, decisions, open followups, then due and active work")
    _output(today, "today")
    today.add_argument("--only", action="append", metavar="NAME",
                       help="keep only this repository's rows, by short name (system), or fleet for items in "
                            "none; repeat for more; every section and count follows")
    today.add_argument("--all", action="store_true",
                       help="print every row of every section, not five and `… N more` (text view only)")
    today.set_defaults(handler=run_today)

    items = store.add_parser("items", help="workspace items from the shared database")
    items.add_argument("--open", action="store_true", help="exclude done items")
    items.add_argument("--here", action="store_true",
                       help="only this checkout's items; refuse unless it is registered in the sd database")
    items.add_argument("--kind", help="filter by item kind, such as task or followup; `report`, a scheduled "
                                      "job's run, closes with `sd reports acknowledge N`")
    items.add_argument("--status", help="filter by workflow status")
    items.add_argument("--since", type=since, metavar="DATE|DURATION",
                       help="only items created or updated since then: YYYY-MM-DD (00:00 UTC) "
                            "or a duration back from now, such as 90m, 6h, 2d or 1w")
    _output(items, "items")
    item = store.add_parser("item", help="one workspace item, notes, and revision")
    item.add_argument("item", type=item_id, help=ITEM_HELP)
    _audit(item)
    _output(item, "item")

    task = groups.add_parser("task", help="capture and manage work without files or GitHub")
    verbs = task.add_subparsers(dest="verb", required=True)
    _register_contributions(verbs)
    add = verbs.add_parser("add", help="capture a standalone item, a task unless --kind says")
    add.add_argument("title")
    add.add_argument("--body")
    add.add_argument("--priority", type=int, choices=range(1, 5))
    add.add_argument("--due", help="YYYY-MM-DD")
    kind = add.add_argument(
        "--kind", default="task",
        help="what the item is (default: task), from the kinds the installed sd_db offers; a "
             "followup takes a checkout as a task does (an sd_db older than sd:809 files it "
             "with none), and work-idea and personal-idea carry no repository")
    kind.choices = LibraryKinds()  # after add_argument: see `LibraryKinds`
    _recurrence_flags(add)
    add.add_argument("--ref", metavar="SOURCE:ID",
                     help="the occurrence that raised the item, such as job:repo-sync:42; a second "
                          "add with the same reference refuses and names the item")
    where = add.add_mutually_exclusive_group()
    where.add_argument("--here", action="store_true",
                       help="refuse unless this is a checkout registered in the sd database "
                            "(a registered one is used by default)")
    where.add_argument("--no-repo", action="store_true",
                       help="file the item, whatever --kind names, belonging to no checkout")
    where.add_argument("--belongs-to", metavar="PATH",
                       help="file the item in this registered checkout, not the one cwd is in, as edit "
                            "--belongs-to moves it (`.` is this one)")
    _output(add, "add")

    edit = verbs.add_parser("edit", help="change a task's or followup's details, or any hand-filed item's kind")
    edit.add_argument("item", type=item_id, help=ITEM_HELP)
    edit.add_argument("--title")
    edit.add_argument("--body")
    priority = edit.add_mutually_exclusive_group()
    priority.add_argument("--priority", type=int, choices=range(1, 5))
    priority.add_argument("--clear-priority", action="store_true")
    due = edit.add_mutually_exclusive_group()
    due.add_argument("--due", help="YYYY-MM-DD; to defer the item, set a later date, or park it with --park")
    due.add_argument("--clear-due", action="store_true")
    branch_group = edit.add_mutually_exclusive_group()
    branch_group.add_argument("--branch", metavar="NAME",
                              help="record the branch the item lives on; the build records one too")
    branch_group.add_argument("--clear-branch", action="store_true",
                              help="clear a stale branch name; an in_progress item takes --branch instead")
    area = edit.add_mutually_exclusive_group()
    area.add_argument("--area", choices=("home", "hoa"),
                      help="show the item only in the dashboard's Home or HOA tab, whatever its "
                           "checkout; not in Personal or a realm tab")
    area.add_argument("--clear-area", action="store_true",
                      help="drop the item's own area, so its checkout places it again")
    # `--belongs-to` and not `--repo`: see `_belongs_to`. `--no-repo` is the
    # word `add` already uses for the same idea, so the pair reads the same on
    # both verbs.
    belongs = edit.add_mutually_exclusive_group()
    belongs.add_argument("--belongs-to", metavar="PATH",
                         help="move the item to a registered checkout (`.` is this one); an idea "
                              "moves alone and keeps its kind")
    belongs.add_argument("--no-repo", action="store_true",
                         help="leave the item belonging to no checkout")
    kind = edit.add_argument(
        "--kind", help="reclassify the item; a move to work-idea or personal-idea "
                       "carries no repository and needs --no-repo in the same command")
    kind.choices = LibraryKinds()  # after add_argument: see `LibraryKinds`
    _recurrence_flags(edit, clear=True)
    park = edit.add_mutually_exclusive_group()
    park.add_argument("--park", action="store_true",
                      help="park an open item now, as the nightly prune parks an untouched P4 "
                           "one: it leaves Today and the backlog; a writing piece parks with "
                           "`sd writing park`")
    park.add_argument("--unpark", action="store_true",
                      help="bring back a parked item; a writing piece "
                           "revives with `sd writing park --revive`")
    edit.add_argument("--experiment", action="append", metavar="yes|of:M|row:ID|new|off",
                      help="project the item into the team experiments databases: yes as an experiment, "
                           "of:M as a task of experiment M, row:ID to pin an existing row, new to drop "
                           "the pinned row, off to stop and keep the row; repeat to combine "
                           "(yes with row:ID); given alone, without other changes")
    _output(edit, "edit", revision=True)

    status = verbs.add_parser("status", help="change status with an atomic history entry")
    status.add_argument("item", type=item_id, help=ITEM_HELP)
    status.add_argument("status", help="the status to move to; a report closes "
                                       "with `sd reports acknowledge N` instead")
    reason = status.add_mutually_exclusive_group()
    reason.add_argument("--reason", help="recorded on the transition; on a row worked on "
                                           "its own branch, why no pull request is needed")
    # `--delivered-by` and not `--commit`: the row records what delivered the
    # task, and the word says so where `--commit` would only say which one.
    # Exclusive with `--reason` because both write the same field and a caller
    # supplying both would have one of them silently dropped.
    reason.add_argument("--delivered-by", metavar="SHA",
                        help="full SHA carrying `Delivers: sd:<id>`, verified against "
                             "the default branch and recorded on the transition")
    # Not in the group: it qualifies `--delivered-by` rather than replacing it.
    # Not `--repo`, for the reason `_belongs_to` gives.
    status.add_argument("--delivered-in", metavar="PATH",
                        help="registered checkout the --delivered-by commit landed in, "
                             "when it is not the item's own; the item keeps its checkout")
    _output(status, "status", revision=True)

    cancel = verbs.add_parser(
        "cancel", help="close a task or followup nobody will do: done, with a cancelled receipt")
    cancel.add_argument("item", type=item_id, help=ITEM_HELP)
    cancel.add_argument("--reason", required=True)
    _output(cancel, "cancel-task", revision=True)

    note = verbs.add_parser("note", help="add an item note or follow-up")
    note.add_argument("item", type=item_id, help=ITEM_HELP)
    # sd:3500: positional id, then positional value. `--body` is the old
    # spelling, kept for one release; `_note_text` takes exactly one of the two.
    note.add_argument("text", nargs="?", help="the note text")
    note.add_argument("--body", help="the note text, the old spelling of TEXT")
    note.add_argument("--kind", default="comment",
                      choices=("comment", "followup", "question", "decision", "proposal"),
                      help="note kind (default: comment): comment (a remark), followup (work still owed; "
                           "`sd note resolve` closes it), question (an operator decision the dashboard "
                           "shows), decision (a ruling; --answers resolves its question), proposal (a "
                           "suggested change)")
    note.add_argument("--answers", type=note_id, metavar="NOTE",
                      help="with --kind decision: the question note this rules on, resolved in the same write")
    note.add_argument("--option", metavar="LABEL",
                      help="with --answers on a review batch question: the option it rules, such as Approve or "
                           "Reject; Approve on an Approve-or-Reject question files a task, and TEXT other than "
                           "the label is kept as the question's comment")
    _output(note, "note", revision=True)
    # A read alias (sd:1112): the same operation as `sd store item`, which
    # stays the canonical read for every kind, so both print the same thing.
    show = verbs.add_parser("show", help="one item, notes, and revision (as `sd store item`)")
    show.add_argument("item", type=item_id, help=ITEM_HELP)
    _audit(show)
    _output(show, "item")

    work = groups.add_parser("work", help="maintain artifact links and verified completion")
    working = work.add_subparsers(dest="verb", required=True)
    relink = working.add_parser("relink", help="link a moved artifact without changing item identity")
    relink.add_argument("item", type=item_id, help=ITEM_HELP)
    relink.add_argument("path", help="existing artifact path relative to the item's repository")
    _output(relink, "relink", revision=True)
    cancel = working.add_parser("cancel", help="record a deliberate cancellation, with a reason")
    cancel.add_argument("item", type=item_id, help=ITEM_HELP)
    cancel.add_argument("--reason", required=True)
    _output(cancel, "cancel", revision=True)
    deliver = working.add_parser("deliver", help="verify a delivery commit and complete its item")
    deliver.add_argument("item", type=item_id, help=ITEM_HELP)
    deliver.add_argument("commit", help="full commit SHA carrying the item's Delivers trailer")
    deliver.add_argument("--associated", action="store_true",
                         help="accept a merge whose trailer block carries `Item:` for the item "
                              "where `Delivers:` was meant; needs --reason")
    deliver.add_argument("--reason", help="why the merge carried Item: and not Delivers:; "
                                          "recorded on the receipt")
    _output(deliver, "deliver", revision=True)
