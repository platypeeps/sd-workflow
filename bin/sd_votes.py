"""`sd votes add|edit|list|show|import`: board votes in the `vote` table (pack sd:8050).

One noun, because no existing verb fits: `sd store` reads a plugin's note
kinds, and `sd task` writes items, which the operator's ruling of 2026-11-07
rejected for votes. The rules are `sd_db.votes`'s; this module reads the
arguments, names the checkout and prints.

The checkout is cwd's, as `sd task add` reads it, or `--belongs-to PATH`
(`sd_work._belongs_to`'s spelling: `tests/test_verb_inventory.py` refuses
`--repo`). It must be registered. Reads take `--json`; `edit` takes
`--if-revision N` and refuses a vote at another revision.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
from typing import Any

import sd_handoff_rows
import sd_lib
from sd_notes import ITEM_HELP, item_id
from sd_work import WorkRefusal

#: The text options, by the column each sets.
TEXT = {"date": "YYYY-MM-DD", "topic": "the subject, a few words", "motion": "the motion or decision as worded",
        "type": "how it was taken, such as meeting or email vote", "moved": "who moved it",
        "seconded": "who seconded it", "votes": "each director's vote, such as 'Ada, Ben Yes; Cy No'",
        "outcome": "such as adopted, failed, or pending: <why>", "follow_up": "what is owed next",
        "source": "where the record is", "notes": "anything else"}


def _library():
    sd_db = sd_handoff_rows.library()
    try:
        from sd_db import votes  # noqa: PLC0415 - after the helper
    except ImportError as error:
        raise WorkRefusal("the installed sd_db has no vote table; update this pack checkout, "
                          "whose lib/ is the sd_db bin/ runs") from error
    return sd_db, votes


def _checkout(args: argparse.Namespace, connection: Any) -> str:
    """The registered repo's own path for `--belongs-to` or cwd's checkout."""
    if args.belongs_to is not None:
        path = pathlib.Path(args.belongs_to).expanduser()
        root = sd_lib.repo_root(path)
        wanted = str(sd_lib.main_worktree_root(root) if root is not None else path.resolve())
    else:
        root = sd_lib.repo_root()
        if root is None:
            raise WorkRefusal("not in a Git checkout; run from the votes' checkout or pass --belongs-to PATH")
        wanted = str(sd_lib.main_worktree_root(root))
    found = sd_lib.repo_row(connection, wanted)
    if found is None:
        raise WorkRefusal(f"{wanted} is not a registered repository; `sd-db repo add` it, or pass --belongs-to PATH")
    return str(found["path"])


def _open(write: bool):
    sd_db, votes = _library()
    return sd_handoff_rows.connect(sd_db, write=write), votes


def _fields(args: argparse.Namespace) -> dict[str, str]:
    return {column: getattr(args, column) for column in TEXT if getattr(args, column) is not None}


def _tally(vote: dict) -> str:
    tally = vote.get("tally")
    return ", ".join(f"{name} {answer}" for name, answer in tally.items()) if tally else "not read"


def _line(vote: dict) -> str:
    what = " · ".join(part for part in (vote["topic"], vote["outcome"]) if part) or "(no topic)"
    return f"{vote['key']}  {vote['date'] or '—':10}  {vote['state']:6}  {what}"


def _print_vote(vote: dict, out) -> None:
    print(f"{vote['key']} in {vote['repo']}: {vote['state']}, revision {vote['revision']}", file=out)
    for column in TEXT:
        if column == "votes":
            print(f"  votes: {vote['votes'] or '—'} (tally: {_tally(vote)})", file=out)
        elif vote[column]:
            print(f"  {column.replace('_', '-')}: {vote[column]}", file=out)
    if vote["item"] is not None:
        print(f"  item: sd:{vote['item']}", file=out)
    print(f"  updated {vote['updated_at']}", file=out)


def run_votes(args: argparse.Namespace, out=None) -> int:
    """Open the store, name the checkout, run the verb's `votes_body`; a library refusal is exit 1 with its message."""
    out = sys.stdout if out is None else out
    if args.votes_verb == "edit":
        args.changes = _changes(args)
    if args.votes_verb == "import":
        args.rows = _read_csv(args.file)
    connection, votes = _open(write=args.votes_verb in ("add", "edit", "import"))
    try:
        repo = _checkout(args, connection)
        try:
            return args.votes_body(votes, connection, repo, args, out)
        except votes.VoteRefused as refusal:
            raise WorkRefusal(str(refusal)) from None
    finally:
        connection.close()


def votes_add(votes, connection, repo: str, args: argparse.Namespace, out) -> int:
    vote = votes.add(connection, repo, args.key, _fields(args), state=args.state, item=args.item)
    print(json.dumps(vote, ensure_ascii=False) if args.json else f"vote {vote['key']} added (revision 1)", file=out)
    return 0


def _changes(args: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = _fields(args)
    if args.state is not None:
        changes["state"] = args.state
    if args.item is not None:
        changes["item"] = args.item
    if args.clear_item:
        changes["item"] = None
    if not changes:
        raise WorkRefusal("edit: name at least one change")
    return changes


def votes_edit(votes, connection, repo: str, args: argparse.Namespace, out) -> int:
    before = votes.get(connection, repo, args.key)
    vote = votes.edit(connection, repo, args.key, args.changes, expected_revision=args.if_revision)
    if args.json:
        print(json.dumps(vote, ensure_ascii=False), file=out)
    else:
        moved = before is None or vote["revision"] != before["revision"]
        print(f"vote {vote['key']} {'edited' if moved else 'unchanged'} (revision {vote['revision']})", file=out)
    return 0


def votes_list(votes, connection, repo: str, args: argparse.Namespace, out) -> int:
    found = votes.rows(connection, repo=repo, state="open" if args.open else None)
    if args.json:
        print(json.dumps({"repo": repo, "votes": found}, ensure_ascii=False), file=out)
    elif not found:
        print(f"no {'open ' if args.open else ''}votes in {repo}", file=out)
    else:
        for vote in found:
            print(_line(vote), file=out)
    return 0


def votes_show(votes, connection, repo: str, args: argparse.Namespace, out) -> int:
    vote = votes.get(connection, repo, args.key)
    if vote is None:
        raise WorkRefusal(f"no vote {args.key} in {repo}")
    if args.json:
        print(json.dumps(vote, ensure_ascii=False), file=out)
    else:
        _print_vote(vote, out)
    return 0


def _read_csv(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as error:
        raise WorkRefusal(f"import: cannot read {path}: {error}") from None
    # DictReader keeps the last of two same-named cells and drops the other, silently.
    twice = sorted({name for name in header if header.count(name) > 1})
    if twice:
        raise WorkRefusal(f"import: {path}: the header names {', '.join(twice)} more than once")
    return rows


def votes_import(votes, connection, repo: str, args: argparse.Namespace, out) -> int:
    counts = votes.import_rows(connection, repo, args.rows)
    if args.json:
        print(json.dumps({"repo": repo, **counts}), file=out)
    else:
        print(f"imported {args.file} into {repo}: {counts['added']} added, {counts['updated']} updated, "
              f"{counts['unchanged']} unchanged", file=out)
    return 0


def _common(parser: argparse.ArgumentParser, *, json_help: str | None = None) -> None:
    parser.add_argument("--belongs-to", metavar="PATH",
                        help="the registered checkout the votes belong to, not the one cwd is in")
    parser.add_argument("--json", action="store_true", help=json_help or "print the vote as JSON")


def _text(parser: argparse.ArgumentParser) -> None:
    for column, words in TEXT.items():
        parser.add_argument(f"--{column.replace('_', '-')}", dest=column, metavar="TEXT", help=words)
    parser.add_argument("--state", choices=("open", "closed"),
                        help="open or closed; left out, an outcome starting 'pending' is open")
    parser.add_argument("--item", type=item_id, help=f"link an item: {ITEM_HELP}")


def register_votes(groups: Any) -> None:
    group = groups.add_parser("votes", help="board votes: add, edit, list, show, or import a votes.csv")
    verbs = group.add_subparsers(dest="votes_verb", required=True)
    add = verbs.add_parser("add", help="add one vote by its key, such as CI-1")
    add.add_argument("key")
    _text(add)
    _common(add)
    add.set_defaults(handler=run_votes, votes_body=votes_add)
    edit = verbs.add_parser("edit", help="change a vote's columns; a new outcome resets its state unless --state")
    edit.add_argument("key")
    _text(edit)
    edit.add_argument("--clear-item", action="store_true", help="drop the item link")
    edit.add_argument("--if-revision", type=int, metavar="N", help="refuse if the vote is not at revision N")
    _common(edit)
    edit.set_defaults(handler=run_votes, votes_body=votes_edit)
    listing = verbs.add_parser("list", help="every vote, open ones first, then newest first")
    listing.add_argument("--open", action="store_true", help="only open votes")
    _common(listing, json_help="print the votes as JSON")
    listing.set_defaults(handler=run_votes, votes_body=votes_list)
    show = verbs.add_parser("show", help="one vote's every column, its tally and its revision")
    show.add_argument("key")
    _common(show)
    show.set_defaults(handler=run_votes, votes_body=votes_show)
    load = verbs.add_parser("import", help="load a votes.csv in one transaction; a key already held is updated")
    load.add_argument("file", help="a CSV with an id column and votes.csv's other columns")
    _common(load, json_help="print the counts as JSON")
    load.set_defaults(handler=run_votes, votes_body=votes_import)
