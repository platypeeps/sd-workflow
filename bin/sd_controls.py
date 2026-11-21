"""Thin provider and report commands; state and validation stay in sd_db."""

from __future__ import annotations

import argparse
import datetime
import getpass
import inspect
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import sd_handoff_rows
import sd_lib
from sd_notes import ITEM_HELP, item_id
from sd_work import WorkRefusal, _belongs_to

#: What the bulk apply records as the acting program, beside the login name
#: `getpass.getuser()` gives as the principal and the name the caller states.
BULK_PROGRAM = "sd reports acknowledge"

#: The flags that belong to `--all-clean`. With an item id each is refused,
#: because the single-item branch would otherwise ignore it: `--who tester`
#: would acknowledge under the login name, which is the defect sd:755 removes.
BULK_FLAGS = ("before", "apply", "if_plan", "who")

#: The flags that belong to an item id, refused with `--all-clean` for the
#: same reason in the other direction: the bulk verb checks a plan, not a
#: revision, and never resolves a followup, so a flag it read and ignored
#: would let the caller believe it did.
ITEM_FLAGS = ("if_revision", "resolve_followups")


def bulk_cutoff(args: argparse.Namespace, reporting: Any) -> str | None:
    """The stamped cutoff of a bulk acknowledge, or None for the single-item verb.

    Every refusal here fires before the store is opened, so a refused call
    can write nothing. The caller gives exactly one of an item id and
    `--all-clean`; the item flags do not go with `--all-clean` and the bulk
    flags do not go with an item id; the bulk form needs `--before`; an apply
    needs both the plan and a stated name (design D4), and neither belongs
    without `--apply`.
    The date goes through `reporting.cutoff`, so the value passed on is the
    one stamp both surfaces write, and a bad one is the helper's refusal.
    """
    if args.control_action != "acknowledge":
        return None
    given = [f"--{flag.replace('_', '-')}" for flag in BULK_FLAGS if getattr(args, flag) not in (None, False)]
    if (args.item is None) == (not args.all_clean):
        raise WorkRefusal("give exactly one of an item id and --all-clean")
    if args.item is not None:
        if given:
            raise WorkRefusal(f"{', '.join(given)}: only with --all-clean, not with an item id")
        return None
    stray = [f"--{flag.replace('_', '-')}" for flag in ITEM_FLAGS if getattr(args, flag) not in (None, False)]
    if stray:
        raise WorkRefusal(f"{', '.join(stray)}: only with an item id, not with --all-clean")
    if args.before is None:
        raise WorkRefusal("--all-clean needs --before DATE")
    if args.apply and (args.if_plan is None or args.who is None):
        raise WorkRefusal("--apply needs --if-plan TOKEN from the dry run and --who NAME")
    if not args.apply and (args.if_plan is not None or args.who is not None):
        raise WorkRefusal("--if-plan and --who go with --apply; leave both off for the dry run")
    return reporting.cutoff(args.before)


def bulk_preview(result: dict[str, Any], connection: Any) -> str:
    """The dry run for a person: the rows, then the apply command to paste.

    The last line is the exact apply, with the stamped `before` and the plan
    filled in and `--who NAME` left for the caller. With no plan there is no
    apply that could succeed, and the last line says so instead. Each row is
    named with its repository, read over `connection` (sd:5124).
    """
    sd_lib.import_sd_db()  # the gateway first, as for every sd_db import here
    from sd_db.repos import item_refs

    refs = item_refs(connection, [row["id"] for row in result["selected"] + result["declined"]])
    lines = [f"clean reports before {result['before']}: {result['count']} selected, "
             f"{result['declined_count']} declined"]
    lines += [f"  selected {refs[row['id']]}  {row['created_at']}  {row['title']}" for row in result["selected"]]
    lines += [f"  declined {refs[row['id']]}: {row['why']}" for row in result["declined"]]
    if result["plan"] is None:
        lines.append(f"no apply: a bulk acknowledge moves between 1 and {result['max_batch']} reports")
    else:
        lines.append(f"sd reports acknowledge --all-clean --before {result['before']} --apply "
                     f"--if-plan {result['plan']} --who NAME")
    return "\n".join(lines)


def acknowledge_reports(args: argparse.Namespace, connection: Any, reporting: Any, workflow: Any) -> dict[str, Any]:
    if args.all_clean:
        if args.apply:
            return reporting.acknowledge_clean(connection, before=args.before, expected_plan=args.if_plan,
                                               who=args.who, principal=getpass.getuser(), program=BULK_PROGRAM,
                                               session=os.environ.get("SD_SESSION"))
        return reporting.clean_reports(connection, before=args.before)
    revision = args.if_revision or workflow.item_state(connection, args.item)["revision"]
    # The keyword is passed only when the flag is on, so a library
    # from before system #394 still serves the plain verb. With the
    # flag on, that library would raise TypeError from inside the
    # call; the signature is read first so the refusal names the
    # remedy instead.
    keywords: dict[str, bool] = {}
    if args.resolve_followups:
        if "resolve_followups" not in inspect.signature(reporting.acknowledge).parameters:
            raise WorkRefusal("this sd_db takes no --resolve-followups; "
                              "run bin/ and lib/ from one pack checkout")
        keywords["resolve_followups"] = True
    return reporting.acknowledge(connection, args.item, expected_revision=revision, who=getpass.getuser(),
                                 **keywords)


def open_table(rows: list[dict[str, Any]]) -> str:
    """`sd reports list --open` as text (sd:8795): one row per open report, newest first."""
    import shutil

    import sd_views
    if not rows:
        return "no open reports"

    def job(row: dict[str, Any]) -> str:
        try:
            report = json.loads(row["fields"] or "{}").get("report") or {}
        except (ValueError, AttributeError):
            return "-"
        return str(report.get("job") or "-") if isinstance(report, dict) else "-"

    width = shutil.get_terminal_size((sd_views.DEFAULT_WIDTH, 24)).columns
    return "\n".join(sd_views.table(["ID", "Status", "Filed", "Job", "Title"],
                                    [[f"#{row['id']}", row["status"], row["created_at"], job(row), row["title"]]
                                     for row in rows], width))


def sweep_lines(result: dict[str, Any]) -> str:
    """`sd reports sweep`'s text: how many closed, or would, then one line each, then what failed."""
    def named(row: dict[str, Any]) -> str:
        return f"sd:{row['item']} {row['kind']}" + (f" #{row['note']}" if row.get("note") else "")
    lines = [f"{'closed' if result['applied'] else 'would close'} {len(result['closed'])}"]
    lines += [f"  {named(row)}: {row['why']}" for row in result["closed"]]
    lines += [f"  not closed, {named(row)}: {row['why']}" for row in result["problems"]]
    return "\n".join(lines)


def sweep_reports(args: argparse.Namespace) -> int:
    """`sd reports sweep` (sd:9759): the Needs you sweep, as text unless `--json`; `--dry-run` opens the store read-only."""
    sd_db = sd_handoff_rows.library()
    from sd_db import needs_sweep  # noqa: PLC0415 -- after the library's gateway
    connection = sd_handoff_rows.connect(sd_db, write=not args.dry_run)
    try:
        result = needs_sweep.sweep(connection, who=getpass.getuser(), apply=not args.dry_run)
    except (sd_db.SdDbError, sqlite3.Error) as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()
    print(json.dumps(result, ensure_ascii=False) if args.json else sweep_lines(result))
    return 0


def list_reports(args: argparse.Namespace, connection: Any, reporting: Any) -> int:
    """`sd reports list`: JSON rows; with `--open` only the open ones, a table unless `--json`."""
    rows = reporting.reports(connection, open_only=args.open)
    print(open_table(rows) if args.open and not args.json
          else json.dumps(rows, ensure_ascii=False, indent=None if args.json else 2))
    return 0


def run(args: argparse.Namespace) -> int:
    sd_db = sd_handoff_rows.library()
    try:
        from sd_db import provider_controls, reporting, workflow
    except ImportError:
        raise WorkRefusal("install the current shared library for provider and report controls") from None
    try:
        before = bulk_cutoff(args, reporting)
    except sd_db.SdDbError as error:
        raise WorkRefusal(str(error)) from error
    if before is not None:
        args.before = before
    # The bulk dry run reads the way `list` does: `write=False` opens SQLite
    # with `mode=ro` and `query_only`, so nothing it does can write.
    connection = sd_handoff_rows.connect(sd_db, write=args.control_action != "list"
                                         and not (before is not None and not args.apply))
    try:
        if args.control_group == "providers":
            if args.control_action == "list":
                result = provider_controls.snapshot(connection)
            else:
                source = Path(args.file)
                if source.stat().st_size > 65536:
                    raise ValueError("provider proposal exceeds 64 KiB")
                proposal = json.loads(source.read_text(encoding="utf-8"))
                if not isinstance(proposal, dict) or set(proposal) != {"revision", "enabled", "orders"}:
                    raise ValueError("provider proposal needs revision, enabled flags and both role orders")
                result = provider_controls.configure(connection, enabled=proposal["enabled"], orders=proposal["orders"],
                    expected_revision=proposal["revision"], who=getpass.getuser())
        elif args.control_action == "list":
            return list_reports(args, connection, reporting)
        elif args.control_action == "acknowledge":
            result = acknowledge_reports(args, connection, reporting, workflow)
            if before is not None and not args.apply and not args.json:
                print(bulk_preview(result, connection))
                return 0
        elif args.control_action == "reopen":
            # The dashboard's Undo (sd:3504): `done` back to the status it was acknowledged from.
            revision = args.if_revision or workflow.item_state(connection, args.item)["revision"]
            result = reporting.reopen(connection, args.item, expected_revision=revision, who=getpass.getuser(),
                                      reason=args.reason)
        else:
            result = reporting.ingest_log(connection, job=args.job, run_id=args.run_id, started=args.started,
                ended=args.ended, exit_code=args.exit_code, log_path=args.log, offset=args.offset)
        print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
        return 0
    except (sd_db.SdDbError, ValueError, KeyError, OSError) as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()


def run_brief(args: argparse.Namespace) -> int:
    """`sd reports brief add|list|show|import` (sd:3459): a file the rules refuse exits 2, before any write."""
    sd_db = sd_handoff_rows.library()
    try:
        from sd_db import remote, routine_briefs
    except ImportError:
        raise WorkRefusal("install the current shared library for routine briefs") from None
    verb = args.brief_verb
    try:
        draft = routine_briefs.read_draft(Path(args.file)) if verb == "add" else None
    except routine_briefs.BriefUnusable as error:
        print(f"sd: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        raise WorkRefusal(f"{args.file}: {error.strerror or error}") from error
    repo = _belongs_to(args.belongs_to) if verb in ("add", "import") else None
    connection = sd_handoff_rows.connect(sd_db, write=verb in ("add", "import", "render"))
    try:
        if verb == "add":
            mode = "extend" if args.extend else "replace" if args.replace else None
            stem, text = draft or ("", "")
            answer = routine_briefs.add(connection, stem, text, repo=repo, who=getpass.getuser(), mode=mode)
            print(f"{answer['result']} {answer['stem']}")
            _copies_after(routine_briefs, remote.HubOnly, connection)
        elif verb == "render":
            status = _rendered(routine_briefs.render(connection, os.environ.get("OBSIDIAN_VAULT")))
            _mirror_briefs(connection, sys.stdout)
            return status
        elif verb == "list":
            _list_briefs(routine_briefs.rows(connection, series=args.series, since=args.since), args.json)
        elif verb == "show":
            sys.stdout.write(routine_briefs.show(connection, args.stem))
        else:
            return _imported(routine_briefs.import_folder(connection, Path(args.folder), repo=repo,
                                                          who=getpass.getuser(), replace=args.replace or []))
        return 0
    except routine_briefs.BriefUnusable as error:
        print(f"sd: {error}", file=sys.stderr)
        return 2
    except (sd_db.SdDbError, ValueError, OSError) as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()


def _copies_after(routine_briefs: Any, hub_only: type[Exception], connection: Any) -> None:
    """The render after a hub `add`'s commit. It never fails the add: the row is the deliverable.

    On a satellite it is skipped: the hub's next `add` or `render` writes the copy.
    """
    try:
        copies = routine_briefs.render(connection, os.environ.get("OBSIDIAN_VAULT"))
    except hub_only:
        return
    except Exception as error:  # noqa: BLE001 - a failed render never fails the add
        copies = {"ok": False, "reason": str(error) or type(error).__name__, "refused": []}
    if not copies["ok"]:
        why = copies["reason"] or f"{len(copies['refused'])} copy(ies) refused, first {copies['refused'][0][0]}: " \
                                  f"{copies['refused'][0][1]}"
        print(f"vault copy not written: {why}", file=sys.stderr)
    _mirror_briefs(connection, sys.stderr)


def _mirror_briefs(connection: Any, stream: Any) -> None:
    """Queue the briefs their items designate (sd:5102), after the vault copies, as a document's render queues it.

    It never fails the verb: a request is a mirror, and the next render queues it again.
    """
    try:
        import sd_research_publish  # noqa: PLC0415 - only a hub render reaches here

        lines = sd_research_publish.enqueue_briefs(connection)
    except Exception as error:  # noqa: BLE001 - reported; the copies and the row stand
        lines = [f"mirror: briefs not queued: {error or type(error).__name__}"]
    for line in lines:
        print(line, file=stream)


def _rendered(copies: dict[str, Any]) -> int:
    """Print a render; exit 1 on any refused file or a render that stopped."""
    for stem, reason in copies["refused"]:
        print(f"refused {stem}: {reason}")
    for path in copies["left"]:
        print(f"left in the previous folder: {path}; delete it once the new folder is in use")
    if copies["reason"]:
        print(f"sd: vault copies not written: {copies['reason']}", file=sys.stderr)
    print(f"{copies['state']}: written {len(copies['written'])}, refused {len(copies['refused'])}")
    return 0 if copies["ok"] else 1


def _list_briefs(listed: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps(listed, ensure_ascii=False))
        return
    for row in listed:
        brief = row["brief"]
        print(f"{brief.get('day')}  {brief.get('series')}  {row['title']}  {brief.get('words')} words")


def _imported(result: dict[str, Any]) -> int:
    """Print an import; exit 1 while any conflict or refused note is left, or nothing verified."""
    for conflict in result["conflicts"]:
        print(f"conflict {conflict['stem']}: {conflict['copy']}; the row stays "
              f"(`--replace {conflict['stem']}` takes the note)")
    for refused in result["refused"]:
        print(f"refused {refused['name']}: {refused['reason']}; left")
    print(f"{len(result['filed'])} filed, {result['unchanged']} unchanged, {len(result['replaced'])} replaced")
    if result["verified"] is not None:
        print(f"verified {result['verified']}")
    return 1 if result["conflicts"] or result["refused"] or result["verified"] is None else 0


def _day(value: str) -> str:
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a day, YYYY-MM-DD") from None


def register_briefs(verbs: Any) -> None:
    """`sd reports brief`: one verb group under `sd reports`, so no new top-level command."""
    group = verbs.add_parser("brief", help="routine briefs in the workflow database: file, list, show, import")
    briefs = group.add_subparsers(dest="brief_verb", required=True)
    add = briefs.add_parser("add", help="file one brief, `<YYYY-MM-DD> - <Series>.md`; a differing row is a "
                                        "conflict unless a flag allows the file")
    add.add_argument("file")
    add.add_argument("--belongs-to", metavar="PATH", required=True, help="the registered checkout that owns it")
    replacing = add.add_mutually_exclusive_group()
    replacing.add_argument("--extend", action="store_true",
                           help="take the file only when the row's text is a strict prefix of it")
    replacing.add_argument("--replace", action="store_true",
                           help="the operator's flag: take the file whatever the row holds")
    listing = briefs.add_parser("list", help="day, series, stem and words, newest first")
    listing.add_argument("--series")
    listing.add_argument("--since", metavar="DAY", type=_day)
    listing.add_argument("--json", action="store_true")
    show = briefs.add_parser("show", help="print one brief's stored Markdown")
    show.add_argument("stem")
    folder = briefs.add_parser("import", help="file each brief note in a folder; a differing row is listed, "
                                              "never replaced, unless its stem is named")
    folder.add_argument("folder")
    folder.add_argument("--belongs-to", metavar="PATH", required=True, help="the registered checkout that owns them")
    folder.add_argument("--replace", metavar="STEM", action="append",
                        help="take the note for this stem over its row; repeat per stem")
    render = briefs.add_parser("render", help="write the vault copies of every brief, on the hub; a hand-edited "
                                              "copy is kept and refused")
    for parser in (add, listing, show, folder, render):
        parser.set_defaults(handler=run_brief)


def run_health(args: argparse.Namespace) -> int:
    """`sd reports health`: the weekly KISS audit's lane and build numbers, as the Metrics page reads the lane."""
    import importlib
    sd_db = sd_handoff_rows.library()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
    # By name, as `sd_operations` loads the dashboard: it is not on bin/'s lint path.
    metrics = importlib.import_module("sd_dashboard.metrics_screen")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    connection = sd_handoff_rows.connect(sd_db, write=False)
    try:
        result = metrics.health(connection, now=now)
    except (sd_db.SdDbError, ValueError) as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    for name in ("now", "last_week"):
        window, lead = result[name], result[name]["lead_minutes"]
        print(f"{name}: {window['since']} to {window['until']}")
        for row in window["lane"]:
            print(f"  lane {row['repository']}: {row['entries']} entries, {row['failed_percent']}% failed "
                  f"(review {row['review']}, gate {row['gate']}, other {row['other']})")
        for row in window["gate_suites"]:
            print(f"  gate {row['repository']} {row['suite']}: {row['failures']}")
        print(f"  builds {window['builds']['done']}/{window['builds']['total']} done")
        for row in window["builds"]["reasons"]:
            print(f"    {row['runs']} {row['reason']}")
        print(f"  lead time over {lead['merged']} merged: median {lead['median']} min, p90 {lead['p90']} min")
    return 0


def register(groups: Any) -> None:
    for name in ("providers", "reports"):
        group = groups.add_parser(name, help=f"inspect and control {name} through the workflow database")
        verbs = group.add_subparsers(dest="verb", required=True)
        for action in (("list", "configure") if name == "providers"
                       else ("list", "ingest", "acknowledge", "reopen", "sweep")):
            command = verbs.add_parser(action)
            command.set_defaults(handler=sweep_reports if action == "sweep" else run, control_group=name,
                                 control_action=action)
            command.add_argument("--json", action="store_true")
            if name == "reports" and action == "list":
                command.add_argument("--open", action="store_true",
                                     help="only open reports, newest 200, as a table unless --json")
            if action == "configure":
                command.add_argument("--file", required=True, help="JSON containing revision, enabled flags and both role orders")
            if action == "acknowledge":
                command.add_argument("item", type=item_id, nargs="?", help=ITEM_HELP)
                command.add_argument("--if-revision")
                command.add_argument("--all-clean", action="store_true",
                                     help="every clean planning report filed before --before, as retention "
                                          "would settle it: a dry run that writes nothing and prints the plan "
                                          "token; --apply moves exactly that plan or nothing")
                command.add_argument("--before", metavar="DATE",
                                     help="the cutoff, YYYY-MM-DD meaning 00:00 UTC, or that exact stamp")
                command.add_argument("--apply", action="store_true",
                                     help="move the previewed selection; needs --if-plan and --who")
                command.add_argument("--if-plan", metavar="TOKEN", help="the plan the dry run printed")
                command.add_argument("--who", metavar="NAME",
                                     help="the name recorded as acting, beside the login and this program")
                command.add_argument("--resolve-followups", action="store_true",
                                     help="also resolve every open followup this report owns, in the same "
                                          "transaction; without it the refusal names each followup, which "
                                          "`sd note resolve <id>` closes")
            if action == "sweep":
                command.help = ("close what Needs you lists but no longer needs you, each with a note naming why "
                                "(sd:9759); a row it cannot prove stays open")
                command.add_argument("--dry-run", action="store_true", help="list what would close; write nothing")
            if action == "reopen":
                command.add_argument("item", type=item_id, help=ITEM_HELP)
                command.add_argument("--if-revision")
                command.add_argument("--reason", help="recorded on the transition after 'reopened: '")
            if action == "ingest":
                command.add_argument("job")
                for option in ("run-id", "started", "ended", "log"):
                    command.add_argument("--" + option, required=True)
                command.add_argument("--exit-code", type=int, required=True)
                command.add_argument("--offset", type=int, default=0)
        if name == "reports":
            register_briefs(verbs)
            health = verbs.add_parser("health", help="the weekly KISS audit's numbers: lane failures by class, "
                                                     "gate failures by suite, build runs done, lead time; 14 days, "
                                                     "and the 14 days that ended a week ago")
            health.add_argument("--json", action="store_true")
            health.set_defaults(handler=run_health)
