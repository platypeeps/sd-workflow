"""Open followups for a checkout, read from the row through the library's brief.

Continuity used to come from one file. `bin/sd-handoff` wrote a packet, and
`bin/sd-handoff-restore` injected it -- but only if somebody had run the write
by hand, and nothing runs it automatically. A session that dies mid-task
leaves no packet, so the next one starts from nothing. That is the gap item A's
criterion 29 names: a session killed mid-task and restarted in the same
directory should begin from the followups it had named, with none lost.

A followup is already a durable row. `sd_db.add_note(kind='followup')` writes
one, `resolve_note` closes it, and the vocabulary is the `CHECK` on `note.kind`
rather than anything this pack declares. Nothing here has ever called either,
which is why the row is a continuity source in name only until this module
reads it back.

**This module exists because a suffixless file cannot be imported.**
`bin/sd-handoff-restore` and `bin/sd-note` both need the same two answers --
which item a directory means, and what is still open under it -- and Python
cannot import either from the other. Putting the pair here is one header paid
once instead of the same reader written twice and drifting.

**It reads and never claims.** The packet is claimed on injection, by rename,
so exactly one session gets it. A row is not: three sessions restarting in one
directory all deserve the same open followups, and a followup stops appearing
when the work is done and somebody resolves it -- not when a session happened
to read it. Nothing here writes.

**What is open is `sd_db.note_brief`'s answer, not a query here.** The hook
knows a directory and no item name. The library turns that into the
checked-out branch's item, or every live item of the repository when no
branch matches, and returns their open `followup` and `question` notes newest
first within eight kilobytes. This module carried its own query and renderer
for the same rows until sd:234's PR 10; `brief_for` below says why the second
reader went.

`sd_db` is imported inside the functions, not at module import, for
`bin/sd_restore.py`'s reason -- the library reaches this virtualenv through the
pack's installer, and before that has run every other verb and hook must keep
working. Callers differ in what they may say about its absence: `bin/sd-note`
prints the remedy, and a `SessionStart` hook must print nothing at all, so this
module raises and lets each caller decide.
"""

from __future__ import annotations

import pathlib

#: Why the reader cannot run yet on a machine that has no library at all.
#: Printed by `bin/sd-note`; swallowed by the hooks, which exit 0 silently
#: rather than break a session. Not the only reason the reader cannot run:
#: `library` below picks this sentence for one of the two faults and
#: `sd_lib`'s own for the other, which is a provisioned copy that will not
#: import and wants its error rather than an installer it already ran.
NOT_INSTALLED = (
    "sd_db is not installed in this virtualenv. Followups are rows; run the "
    "pack's bin/ from its checkout, whose lib/ holds sd_db, then run this again."
)

#: The kind `sd-note add` writes by default. `add_note` accepts six more and
#: `transition` owns the seventh; which kinds a resuming session is handed is
#: `sd_db.note_brief`'s decision (`followup` and `question`), not this one's.
FOLLOWUP = "followup"


class RowsRefusal(Exception):
    """Something the caller must settle before followups can be read."""


def library():
    """Import `sd_db`, or refuse with the remedy rather than a traceback."""
    # May or may not be resolvable at type-check time: `sd_db` is built into
    # this virtualenv by the pack's installer, from the `system` checkout, and
    # this repository does not vendor it. `pyproject.toml` carries the
    # override rather than an inline ignore here, which `warn_unused_ignores`
    # turns into a failure on any machine that has run `make setup`. An absent
    # library is a supported state, and the helper's two tries are what makes
    # the ordinary machine -- the pack's `sd_db`, the PATH `python3` -- the
    # present one.
    import sd_lib

    imported = sd_lib.import_sd_db()
    if imported.module is None:
        # Two faults, two remedies, and this is the caller that used to hand
        # both readers the same one. `NOT_INSTALLED` says the library is
        # absent and to run the installer; over a provisioned copy that
        # raised on import that is false in its first clause and useless in
        # its second, and it swallows the only thing that would let anybody
        # fix it -- what the copy actually raised. The helper's own sentence
        # names the copy and quotes the error, so for that fault it *is* the
        # refusal. `bin/sd-note` and `bin/sd` print whichever one arrives
        # verbatim, which is why the choice has to be made here.
        raise RowsRefusal(imported.problem if imported.provisioned else NOT_INSTALLED) from None
    return imported.module


def connect(sd_db, *, write: bool = False, keep: tuple[type[BaseException], ...] = ()):
    """`sd_db.connect`, every error a `RowsRefusal`; one of `keep` keeps its type, for a caller that skips it."""
    try:
        return sd_db.connect(write=write)
    except keep:
        raise
    except Exception as error:
        raise RowsRefusal(str(error)) from None


def item_for(connection, sd_db, root, item_dir):
    """The row one work-item directory names, or None when there is none.

    The resolver is `sd_lib`'s, so a writer and the dashboard key an item the
    same way: `registered_base` resolves the checkout to the registered
    repository through `sd_db.repos.registered_for` -- by path, else by
    origin, so a runner clone reads the row its checkout owns (sd:981) -- and
    `item_for_artifact` reads it back by that base and the relative path.
    The `item_by_external` fallback below is for a library without
    `item_for_artifact`; one that old has no `registered_for` either, and
    stays path-keyed on purpose.

    A folder the path does not reach may name its task or followup row as
    `item: sd:<id>` in its frontmatter (sd:994). That read is `sd_lib`'s
    too, `named_row`, so this reader and `sd_lib.Rows` name one row for one
    folder; a library without `item_by_id` reads the key as nothing.
    """
    import sd_lib

    try:
        from sd_db.progress import item_for_artifact
    except ImportError:
        identity = sd_lib.external_id(root, item_dir)
        row = sd_db.writes.item_by_external(connection, sd_lib.ITEM_ROW_SOURCE, identity)
    else:
        base = sd_lib.registered_base(root, sd_db, connection)
        relative = (item_dir / "prd.md").relative_to(root).as_posix()
        row = item_for_artifact(connection, base, relative)
    if row is None:
        read = None
        try:
            from sd_db.reads import item_by_id
        except ImportError:
            pass  # A library without it reads the key as nothing, as `Rows` does.
        else:
            read = item_by_id
        row, _ = sd_lib.named_row(item_dir, connection, read)
    return None if row is None else dict(row)


def policy_for(root) -> str:
    """The repository's policy line (sd:6715, `sd_repo_policy.summary`); "" when it declares none."""
    import sd_lib

    policy = sd_lib.sibling("sd_repo_policy", "sd_repo_policy.py")
    return policy.summary(sd_lib.repo_declaration(root, "policy"))


def brief_for(root) -> list[str]:
    """The whole read for a hook: open, `sd_db.note_brief`, close. Never writes.

    The brief is the library's and not this module's. Requirement 7 of
    system's one-database item puts the order (newest first), the filter (open
    `followup` and `question` notes of the checked-out branch's item, or of
    every live item in the repository when no branch matches) and the
    eight-kilobyte bound in `sd_db.brief`, because a pack that renders the
    same rows from its own query is a second reader that drifts. This module
    used to be that second reader; what is left is the plumbing a hook needs
    around the call.

    Two paths, on purpose. `item.repo` is the *main* worktree root, since a
    linked worktree was never added to the `repo` table, so the rows are read
    against that. The branch is the session's own checkout, which in a linked
    worktree is not the main one's: `note_brief` left to read the branch
    itself would read it at the main root and brief the wrong item. A
    detached HEAD is passed as `""` rather than None, because None tells
    `note_brief` to go and read the branch at `repo` -- the main root again --
    and `""` is its repository-wide case.
    """
    import sd_lib

    sd_db = library()
    connection = connect(sd_db)
    try:
        here = pathlib.Path(root).resolve()
        base = str(sd_lib.main_worktree_root(here))
        branch = sd_db.brief.checked_out_branch(here) or ""
        text = sd_db.note_brief(connection, base, branch=branch).text.rstrip("\n")
    finally:
        connection.close()
    # `split("\n")` and not `splitlines()`: the brief's lines end in `\n`
    # alone, and `splitlines` also breaks a note body at `\r`, a form feed or
    # U+2028, which the hook's `"\n".join` then hands over changed.
    return text.split("\n") if text else []
