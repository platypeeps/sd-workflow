"""Observe jobs, services and assignments, and request supported operations."""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import math
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import sd_handoff_rows
from sd_notes import ITEM_HELP, item_id
from sd_work import WorkRefusal


def service_result(connection: Any, args: argparse.Namespace) -> Any:
    try:
        import sd_db.services as services
    except ImportError:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs for service controls") from None
    if args.operation == "list":
        return services.inventory(connection)["services"]
    state = services.service_state(connection, args.identity)
    if args.operation == "get":
        return state
    operation = {"start": services.start_service, "stop": services.stop_service,
                 "restart": services.restart_service}[args.operation]
    return operation(connection, args.identity,
                     expected_revision=args.if_revision or state["revision"],
                     who=getpass.getuser())


def run(args: argparse.Namespace) -> int:
    sd_db = sd_handoff_rows.library()
    try:
        import sd_db.operations as operations
    except ImportError:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs for job controls") from None
    write = args.operation in {"retry", "cancel", "start", "stop", "restart"}
    connection = sd_handoff_rows.connect(sd_db, write=write)
    try:
        result: Any
        jobs = args.operation_group == "jobs"
        if args.operation_group == "services":
            result = service_result(connection, args)
        elif args.operation == "list":
            result = operations.inventory(connection)[args.operation_group]
        else:
            state = (operations.job_state(connection, args.identity) if jobs
                     else operations.assignment_state(connection, int(args.identity)))
            if args.operation == "get":
                result = state
            else:
                revision = args.if_revision or state["revision"]
                if jobs:
                    operation = operations.retry_job if args.operation == "retry" else operations.cancel_job
                    result = operation(connection, args.identity, expected_revision=revision,
                                       who=getpass.getuser())
                else:
                    result = operations.cancel_assignment(connection, int(args.identity),
                                                          expected_revision=revision, who=getpass.getuser())
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif isinstance(result, list):
            if not result:
                print(f"No {args.operation_group}.")
            for entry in result:
                if args.operation_group == "services":
                    print(f"{entry['id']}  {entry['state']}")
                elif jobs:
                    print(f"{entry['name']}  {entry['state']}")
                else:
                    print(f"#{entry['id']}  {entry['status']}  {entry['role']}  {entry['title']}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("request", {}).get("status") in {"failed", "unknown"} else 0
    except (sd_db.SdDbError, ValueError) as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()


def queue_builds(args: argparse.Namespace) -> int:
    """`sd assignments build`: the Build press, one queued build assignment per item (pack sd:6093)."""
    sd_db = sd_handoff_rows.library()
    try:
        import sd_db.build as build
    except ImportError:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs for builds") from None
    connection = sd_handoff_rows.connect(sd_db, write=True)
    try:
        queued = build.queue(connection, args.items, who=getpass.getuser())
    except (sd_db.SdDbError, ValueError) as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()
    if args.json:
        print(json.dumps(queued, ensure_ascii=False))
    for entry in [] if args.json else queued:
        print(f"#{entry['id']}  {entry['status']}  build  sd:{entry['item']}")
    return 0


def _beat_flags(args: argparse.Namespace) -> None:
    """Refuse a `beat` flag set that does not go together, before anything is read."""
    if not args.start and (args.worktree or args.branch or args.budget is not None or args.into):
        raise WorkRefusal("--worktree, --branch, --budget and --into go with --start")
    if args.start and (not args.worktree or not args.branch or args.budget is None):
        raise WorkRefusal("--start needs --worktree, --branch and --budget")
    if args.reason is not None and not (args.start or args.end):
        raise WorkRefusal("--reason goes with --start or --end")
    if not args.start and len(args.items) != 1:
        raise WorkRefusal("a beat or an end names one item of the run")


def beat_teammate(args: argparse.Namespace) -> int:
    """`sd assignments beat ITEM`: a teammate run's beat; `--start` registers one, `--end` ends it (pack sd:7221)."""
    sd_db = sd_handoff_rows.library()
    try:
        import sd_db.teammate as teammate
    except ImportError:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs for teammate runs") from None
    _beat_flags(args)
    connection = sd_handoff_rows.connect(sd_db, write=True)
    try:
        if args.start:
            found = teammate.start(connection, args.items, worktree=str(Path(args.worktree).expanduser().resolve()),
                                   branch=args.branch, budget_minutes=args.budget, owner=getpass.getuser(),
                                   into=args.into, reason=args.reason)
            result = {"run": found["id"], "due": found["due"], "items": [m["item"] for m in found["members"]]}
            line = f"teammate run {found['id']} started; due {found['due']}"
        elif args.end:
            run = teammate.end(connection, args.items[0], reason=args.reason or "")
            result, line = {"run": run, "ended": True}, f"teammate run {run} ended"
        else:
            run = teammate.beat(connection, args.items[0])
            result, line = {"run": run, "beat": True}, f"teammate run {run} beat"
    except (sd_db.SdDbError, ValueError) as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()
    print(json.dumps(result, ensure_ascii=False) if args.json else line)
    return 0


def list_sessions(args: argparse.Namespace) -> int:
    """`sd assignments dispatch --sessions`: the live Claude sessions and the worktrees each holds (pack sd:6024).

    For a cleanup outside the pack, such as a `target/` prune: a worktree a
    listed session on this machine names is held. A failed read exits 1, so
    the caller keeps everything.
    """
    sd_db = sd_handoff_rows.library()
    try:
        import sd_db.sessions as sessions
    except ImportError:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs for sessions") from None
    connection = sd_handoff_rows.connect(sd_db, write=False)
    try:
        found = sessions.live(connection)
    except (sd_db.SdDbError, ValueError) as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()
    if args.json:
        print(json.dumps(found, ensure_ascii=False))
    elif not found:
        print("No live sessions.")
    for entry in [] if args.json else found:
        trees = ", ".join(str(tree.get("path")) for tree in entry.get("worktrees") or []) or "no worktree"
        print(f"{entry.get('machine')}  {entry['session'][:8]}  {entry['beat']}  {trees}")
    return 0


BIN = Path(__file__).resolve().parent
GATE_SECONDS = 15
#: The tick's own bound, under the build-dispatch job's 90 s JOB_TIMEOUT: past it the tick skips (pack sd:6494).
TICK_SECONDS = 75
#: The log-folder probe's bound: a stat on a hung bulk volume never returns, even to the tick's alarm (pack sd:6749).
PROBE_SECONDS = 5


def build_modules() -> tuple[Any, Any, Any]:
    sd_db = sd_handoff_rows.library()
    try:
        import sd_db.build as build
        import sd_db.build_run as build_run
    except ImportError:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs for builds") from None
    return sd_db, build, build_run


def log_root() -> Path | None:
    """`sd.bulk_storage_root`, where an ended run's logs go as `<root>/<repository>/build/`; None when unset.

    A live run writes them under `build_run.local_root()` (pack sd:8246)."""
    import sd_lib
    value = sd_lib.core_setting("bulk_storage_root")
    return Path(os.path.expanduser(value)) if value else None


def writable(root: Path) -> bool | None:
    """Whether `root` is a writable folder; None when the volume does not answer in `PROBE_SECONDS`.

    The probe runs in a daemon thread: a hung stat holds only that thread, and the process still exits.
    """
    answer: list[bool] = []
    probe = threading.Thread(target=lambda: answer.append(root.is_dir() and os.access(root, os.W_OK)), daemon=True)
    probe.start()
    probe.join(PROBE_SECONDS)
    return answer[0] if answer else None


def gate_load() -> tuple[float | None, float | None]:
    """The five-minute load and its cap from `sd gate status --json`; (None, None) when unread."""
    try:
        done = subprocess.run([sys.executable, str(BIN / "sd"), "gate", "status", "--json"], capture_output=True,
                              text=True, timeout=GATE_SECONDS, check=False, stdin=subprocess.DEVNULL)
        answer = json.loads(done.stdout) if done.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None
    load, cap = (answer.get("load"), answer.get("load_max")) if isinstance(answer, dict) else (None, None)
    if not (isinstance(load, list) and len(load) >= 2 and isinstance(load[1], (int, float)) and math.isfinite(load[1])
            and isinstance(cap, (int, float)) and math.isfinite(cap)):
        return None, None
    return float(load[1]), float(cap)


def interactive_builders(connection: Any, now: str, key: Any) -> list[dict] | None:
    """One `{repo, session_id}` per interactive builder, as the dashboard counts them; None when not counted.

    `repo` is the registered repository the session's cwd lies in, else None
    (it counts against the machine only). A cwd in a linked worktree, such as
    one outside `~/repos`, lies in its main checkout, read from git's common
    dir by `sd_lib.main_worktree_root` (pack sd:6112).
    """
    import sd_lib
    sys.path.insert(0, str(BIN.parent / "dashboard"))
    try:
        # By name, as `sd_work` loads its modules: the dashboard is not on bin/'s lint path.
        claude_sessions = importlib.import_module("sd_dashboard.claude_sessions")
        document = claude_sessions.document(connection, now=now)
        if document["builders"]["running"] is None:
            return None
        repos = [row["path"] for row in connection.execute("SELECT path FROM repo")]
        found = []
        for session in document["sessions"] or []:
            count = sum(1 for agent in session["agents"] if agent["builder"] and agent["state"] not in ("idle", "stalled"))
            count += 1 if session["builder"] and session["status"] != "idle" else 0
            cwd = key(str(sd_lib.main_worktree_root(Path(session["cwd"]).resolve()))) if session.get("cwd") else ""
            repo = next((path for path in repos if cwd and (cwd == path or cwd.startswith(path.rstrip("/") + "/"))),
                        None)
            found += [{"repo": repo, "session_id": session["id"]}] * count
        return found
    except Exception:  # noqa: BLE001 -- any failure is "not counted", which claims nothing
        return None
    finally:
        sys.path.remove(str(BIN.parent / "dashboard"))


def cited(worktree: Path, base: str, head: str, body: str) -> set[int] | None:
    """The items the lane reads the branch as delivering, as `lane enqueue --deliver` reads them; None when unread.

    `sd_ship_body.cited_ids` over the commits `head` adds to `base` and the
    body: each `Closes: sd:N` trailer, less a reverted commit's.
    """
    import sd_lane
    import sd_ship_body
    branch = base.removeprefix("origin/")
    try:
        commits = sd_lane.branch_commits(worktree, branch, head)
        parents = sd_lane.branch_parents(worktree, branch, head)
    except sd_lane.LaneError:
        return None
    return set(sd_ship_body.cited_ids(body, commits, parents))


def enqueue_refusals(worktree: Path, base: str, head: str, title: str, body: str, *,
            environ: dict[str, str] | None = None) -> list[str]:
    """What `lane enqueue` and its push would refuse, as the builder can fix it (pack sd:6926).

    The lane's own line check over the title, the body and each commit
    `head` adds to `base`, then the leak guard over the same range when
    the repository's pre-push hook runs it. A guard that cannot answer
    adds nothing: the push still checks.
    """
    import sd_lane
    import sd_lib
    sd_lib.import_sd_db()  # the gateway first, as for every sd_db import here
    from sd_db.config import system_checkout
    branch = base.removeprefix("origin/")
    found = []
    try:
        sd_lane.check_lines(worktree, title, body, branch, head)
    except sd_lane.LaneError as error:
        found.append(str(error))
    hook = sd_lib.git_output(["rev-parse", "--git-path", "hooks/pre-push"], Path(worktree)) or ""
    try:
        guarded = "local-leak-guard" in (Path(worktree) / hook).read_text(encoding="utf-8")
    except (OSError, ValueError):
        guarded = False
    guard = system_checkout(environ) / "local-leak-guard" / "leak-guard.sh"
    if guarded and guard.is_file():
        done = subprocess.run(["sh", str(guard), "check", "--range", f"refs/remotes/origin/{branch}..{head}"],
                              cwd=worktree, capture_output=True, text=True, timeout=60, env=environ, check=False)
        if done.returncode == 1:
            found.append("the pre-push leak guard refuses these commits; replace each private value with a "
                         "synthetic one (operator, example.test):\n" + done.stderr.strip()[-1500:])
    return found


def own_checks(worktree: Path) -> list[list[str]]:
    """The checks an unmanaged repository's build runs, as `sd-check` detects and picks them (pack sd:7868).

    The aggregate `check` when the repository has one, else each of `test` and
    `lint` it has; none is an empty list. A declaration that does not read is
    ValueError, a configuration fault that ends the run blocked; a `git` that
    gave no answer is `Unread`, which the reap retries. Neither is a fix
    round: the builder cannot fix either (review of b34af431e).
    """
    import sd_lib
    try:
        found = sd_lib.detect_entrypoints(worktree)
    except sd_lib.GitUnavailable as error:
        raise build_modules()[2].Unread(f"git gave no answer reading the check entrypoints: {error}") from error
    except sd_lib.ConfigError as error:
        raise ValueError(str(error)) from error
    return [list(found.commands[name]) for name in sd_lib._runs(found.commands)]


def push_failures(connection: Any, environ: dict[str, str] | None = None) -> list[str]:
    """Push each lane entry or build run that failed or blocked, once, on ntfy (pack sd:6946); the keys sent.

    The send goes through `SD_NOTIFY` or `notify` on PATH, as `sd-db
    backup` reports a failure. A key is recorded only once its send
    landed, so a failed send is tried again next tick. Success pushes
    nothing. Only the hub pushes, every host's failures: a satellite's tick
    would send each one a second time (pack sd:7933).
    """
    import sd_lib
    sd_lib.import_sd_db()  # the gateway first, as for every sd_db import here
    from sd_db import build, today
    from sd_db.writes import record_state
    if None not in build.lane_hosts(connection):
        return []
    env = os.environ if environ is None else environ
    notify = env.get("SD_NOTIFY") or shutil.which("notify", path=env.get("PATH"))
    if not notify:
        return []
    sent = []
    for row in today.push_due(connection):
        try:
            done = subprocess.run([notify, "-t", row["title"], "-c", "ntfy", row["body"]], capture_output=True,
                                  timeout=30, env=env, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            record_state(connection, "watermark", key=today.PUSHED + row["key"], body=row["title"])
            sent.append(row["key"])
    return sent


def tools(build_run: Any) -> Any:
    return build_run.Tools(claude=["claude"], review=[str(BIN / "sd-review")], ship=[str(BIN / "sd-ship")],
                           lint=[str(BIN / "sd-docs-lint")])


class Overdue(BaseException):
    """The tick ran past `TICK_SECONDS`: a BaseException, so no broad `except Exception` in the tick holds it."""


def dispatch_builds(args: argparse.Namespace) -> int:
    """`sd assignments dispatch`: one build-dispatch tick (pack sd:6094)."""
    import sd_lib
    sd_db, build, build_run = build_modules()
    why = ""
    try:
        root = log_root()
    except sd_lib.ConfigError as error:  # a malformed setting is unread: no claim, but the reap and watch run
        root, why = None, f"{error}, so a build run has no log folder"
    if root is None and not why:
        why = "sd.bulk_storage_root is unset, so a build run has no log folder"
    elif root is not None and (ready := writable(root)) is not True:
        why = (f"the log folder {root} is not writable" if ready is False
               else f"the log folder {root} did not answer in {PROBE_SECONDS:g} s")

    def overdue(*_: Any) -> None:
        raise Overdue
    # One bound for the whole tick, the waits on a lock and the lane list too; a skip exits 0 (pack sd:6494).
    signal.signal(signal.SIGALRM, overdue)
    signal.setitimer(signal.ITIMER_REAL, TICK_SECONDS)
    unstarted: dict[str, str] = {}
    try:
        result = tick(sd_db, build, build_run, root, why, unstarted)
    except Overdue:
        result = None
        print(f"skipped: the tick ran past {TICK_SECONDS:g} s")
    except sqlite3.OperationalError as error:
        if "locked" not in str(error) and "busy" not in str(error):
            raise WorkRefusal(str(error)) from error
        result = None
        print(f"skipped: {error}")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    if result is None:
        return 0
    if unstarted:  # an unwritable run log skips that run's supervisor, not the tick
        result["spawned"] = [ident for ident in result["spawned"] if ident not in unstarted]
        result["why"] = "; ".join(filter(None, [result["why"], *unstarted.values()]))
    print(json.dumps(result, ensure_ascii=False) if args.json else
          f"reaped {len(result['reaped'])}, moved {len(result['moved'])}, swept {len(result['swept'])}, claimed {len(result['claimed'])}, "
          f"drafted {len(result['drafted'])}, spawned {len(result['spawned'])}" + (f"; no claim: {result['why']}" if result["why"] else ""))
    return 0


def tick(sd_db: Any, build: Any, build_run: Any, root: Path | None, why: str, unstarted: dict[str, str]) -> dict:
    """`dispatch_builds`' tick in the database; each run whose supervisor could not start goes in `unstarted`."""
    load, load_cap = gate_load()
    connection = sd_handoff_rows.connect(sd_db, write=True)
    try:
        def spawn(ident: str) -> None:
            if root is None:
                return
            repo = build.run(connection, ident)["repo"]
            try:
                build_run.spawn([sys.executable, str(BIN / "sd"), "assignments", "dispatch", "--supervise", ident],
                                log=build_run.log_dir(build_run.local_root(), repo) / f"{ident}.supervisor.log")
            except OSError as error:
                unstarted[ident] = f"cannot start {ident}'s supervisor: {error}"

        def lane(row: dict, items: list[int]) -> dict | None:
            return build_run.lane_entry(tools(build_run).ship, build_run.paths.expand(row["repo"]), items)

        result = build_run.dispatch(connection, owner="build-dispatch", load=load, load_cap=load_cap,
                                    interactive=interactive_builders(connection, build_run.now(), build_run.paths.key),
                                    spawn=spawn, lane=lane, why=why)
        result["pushed"] = push_failures(connection)
        if root is not None and not why:  # the bulk root answered the probe
            result["archived"] = build_run.archive(connection, build_run.local_root(), root)
    except (sd_db.SdDbError, ValueError, OSError) as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()
    return result


def supervise_build(args: argparse.Namespace) -> int:
    """`sd assignments dispatch --supervise RUN`: walk one build run to the lane, or one draft run to its spec
    (pack sd:6361). The tick starts it in its own session; the run's assignment scope picks the supervisor.
    """
    import sd_lib
    sd_db, _, build_run = build_modules()
    try:
        root = log_root()
    except sd_lib.ConfigError as error:
        raise WorkRefusal(str(error)) from error
    if root is None:
        raise WorkRefusal("sd.bulk_storage_root is unset, so a build run has no log folder")
    local = build_run.resume_root(args.run, root)  # a resumed image keeps its child's files

    def start() -> tuple[Any, Any]:
        # `sd_db.connect`, not the wrapper that turns every error into one refusal: a mismatch must keep its type.
        connection = sd_db.connect(write=True)
        try:
            scope = connection.execute("SELECT assignment.scope FROM runner_run JOIN assignment "
                                       "ON assignment.id = runner_run.assignment WHERE runner_run.id = ?",
                                       (args.run,)).fetchone()
            if scope is not None and scope[0] == build_run.draft.SCOPE:
                drafter = build_run.DraftSupervisor(connection, args.run, tools=tools(build_run), log_root=local)
                return connection, drafter
            return connection, build_run.Supervisor(connection, args.run, tools=tools(build_run), log_root=local,
                                                    cited=cited, checked=enqueue_refusals, checks=own_checks)
        except BaseException:
            connection.close()
            raise

    try:
        # A supervisor exec'd across a hub refresh holds a live child: every read before `run()` waits (pack sd:8182).
        connection, supervisor = build_run.start_again(args.run, start)
        try:
            result = supervisor.run()
        finally:
            connection.close()
    except (sd_db.SdDbError, ValueError) as error:
        raise WorkRefusal(str(error)) from error
    print(json.dumps(result, ensure_ascii=False))
    return 0


def build_dispatcher(args: argparse.Namespace) -> int:
    """`sd assignments dispatch`: the tick; `--supervise RUN` walks one run; `--sessions` lists the live sessions."""
    if args.run is not None:
        return supervise_build(args)
    if args.sessions:
        return list_sessions(args)
    return dispatch_builds(args)


def register(groups: Any) -> None:
    for name in ("jobs", "assignments", "services"):
        group = groups.add_parser(name, help=f"inspect {name} and request supported controls")
        verbs = group.add_subparsers(dest="verb", required=True)
        actions = {"jobs": ("list", "get", "retry", "cancel"),
                   "assignments": ("list", "get", "cancel"),
                   "services": ("list", "get", "start", "stop", "restart")}[name]
        for action in actions:
            parser = verbs.add_parser(action)
            parser.set_defaults(handler=run, operation_group=name, operation=action)
            parser.add_argument("--json", action="store_true")
            if action != "list":
                parser.add_argument("identity", help={"jobs": "registered job name",
                    "assignments": "assignment ID", "services": "launchd label (get also accepts system:LABEL)"}[name])
            if action in {"retry", "cancel", "start", "stop", "restart"}:
                parser.add_argument("--if-revision", help="require the exact revision you inspected")
        if name == "assignments":
            parser = verbs.add_parser("build", help="queue a build of each ready item; the whole selection or none")
            parser.set_defaults(handler=queue_builds)
            parser.add_argument("items", nargs="+", type=item_id, help=ITEM_HELP)
            parser.add_argument("--json", action="store_true")
            # One verb for the dispatcher (ruling #31545, pack sd:6356): the tick, and with a flag its supervisor or
            # the sessions it counts.
            parser = verbs.add_parser("dispatch", help="one build-dispatch tick: reap, watch the lane, claim, spawn")
            parser.set_defaults(handler=build_dispatcher)
            mode = parser.add_mutually_exclusive_group()
            mode.add_argument("--supervise", metavar="RUN", dest="run",
                              help="supervise one build or draft run; the tick starts it")
            mode.add_argument("--sessions", action="store_true",
                              help="list the live Claude sessions and the worktrees each holds")
            parser.add_argument("--json", action="store_true")
            # One verb for an in-session builder (pack sd:7221, ruling note #41072): the beat, and with a flag the
            # lead's start or end of the run.
            parser = verbs.add_parser("beat", help="beat the teammate run that holds an item; --start or --end it")
            parser.set_defaults(handler=beat_teammate)
            parser.add_argument("items", nargs="+", type=item_id, help=ITEM_HELP)
            mode = parser.add_mutually_exclusive_group()
            mode.add_argument("--start", action="store_true",
                              help="the lead registers a run for the items, the first its lead")
            mode.add_argument("--end", action="store_true", help="the lead ends the run once the builder reported")
            parser.add_argument("--worktree", help="with --start: the builder's worktree")
            parser.add_argument("--branch", help="with --start: the builder's branch")
            parser.add_argument("--budget", type=int, metavar="MINUTES", help="with --start: the builder's budget")
            parser.add_argument("--into", metavar="BRANCH",
                                help="with --start: the open bundle branch this side branch merges into (pack sd:7737)")
            parser.add_argument("--reason", help="with --end: why it ended (default: ended by its lead); with --start: "
                                "state-move or in-lane, why the repository gets a second open branch (pack sd:7737)")
            parser.add_argument("--json", action="store_true")
