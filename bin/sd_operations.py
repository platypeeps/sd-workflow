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
import time
from pathlib import Path
from typing import Any

import sd_handoff_rows
from sd_notes import ITEM_HELP, item_id
from sd_work import WorkRefusal


def record_machine(connection: Any, interval: int, inventory: dict) -> None:
    """Store this machine's Disk and Services readings for Health's machine chooser (sd:9757).

    A satellite's scheduled job runs `sd services list --record SECONDS`; the hub's Health reads the row back, keyed
    by this machine's host, and marks it stale past SECONDS. Machines meet through the database, never a session.
    """
    sys.path.insert(0, str(BIN.parent / "dashboard"))
    try:
        # By name, as `sd_work` loads its modules: the dashboard is not on bin/'s lint path.
        collectors = importlib.import_module("sd_dashboard.health_collectors")
        collectors.record_reading(connection, interval=interval, services=inventory)
    finally:
        sys.path.remove(str(BIN.parent / "dashboard"))


def service_result(connection: Any, args: argparse.Namespace) -> Any:
    try:
        import sd_db.services as services
    except ImportError:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs for service controls") from None
    if args.operation == "list":
        inventory = services.inventory(connection)
        if getattr(args, "record", None) is not None:
            record_machine(connection, args.record, inventory)
        return inventory["services"]
    state = services.service_state(connection, args.identity)
    if args.operation == "get":
        return state
    operation = {"start": services.start_service, "stop": services.stop_service,
                 "restart": services.restart_service}[args.operation]
    return operation(connection, args.identity,
                     expected_revision=args.if_revision or state["revision"],
                     who=getpass.getuser())


#: The assignment statuses `get --wait` waits out: a build not yet ended (pack sd:8793).
WAITING = ("queued", "claimed", "running", "ending")
POLL_SECONDS = 30


def read_assignments(sd_db: Any, operations: Any, identities: list[str], *, merged: bool) -> list[dict]:
    """One read of each assignment's state; with `merged`, a done build's `lane` entry too (pack sd:8798)."""
    connection = sd_handoff_rows.connect(sd_db, write=False)
    try:
        states = [operations.assignment_state(connection, int(ident)) for ident in identities]
        for state in states if merged else ():
            if state["status"] == "done" and state["item"]:
                state["lane"] = operations.lane_state(connection, state["item"])
        return states
    except (sd_db.SdDbError, ValueError) as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()


def get_assignments(args: argparse.Namespace) -> int:
    """`sd assignments get N...`: each assignment as `get` prints it; `--wait` polls until every one has left
    `WAITING`, exit 1 at `--timeout` minutes (default 240), in place of session-written poll scripts (pack sd:8793).
    `--until merged` also waits out a done build's lane entry, named in its `lane` key, and exits 0 only when each
    one merged (pack sd:8798)."""
    for flag, given in (("--timeout", args.timeout is not None), ("--until", args.until != "ended")):
        if given and not args.wait:
            raise WorkRefusal(f"{flag} goes with --wait")
    sd_db = sd_handoff_rows.library()
    try:
        import sd_db.operations as operations
    except ImportError:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs for job controls") from None
    merged = args.until == "merged"
    deadline = time.monotonic() + 60 * (240 if args.timeout is None else args.timeout)
    while True:
        states = read_assignments(sd_db, operations, args.identity, merged=merged)
        lanes = [(state.get("lane") or {}).get("status") for state in states]
        ended = all(state["status"] not in WAITING for state in states) and not {"pending", "running"} & set(lanes)
        left = deadline - time.monotonic()
        if not args.wait or ended or left <= 0:
            break
        time.sleep(min(POLL_SECONDS, left))
    for state in states:
        print(json.dumps(state, ensure_ascii=False) if args.json else json.dumps(state, ensure_ascii=False, indent=2))
    return 0 if not args.wait or (ended and (not merged or set(lanes) == {"merged"})) else 1


def run(args: argparse.Namespace) -> int:
    sd_db = sd_handoff_rows.library()
    try:
        import sd_db.operations as operations
    except ImportError:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs for job controls") from None
    write = args.operation in {"retry", "cancel", "start", "stop", "restart"} or getattr(args, "record", None) is not None
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
    """`sd assignments build`: the Build press, one queued build assignment per item (pack sd:6093); an item whose
    latest bundle ended blocked or cancelled is Retry of that bundle instead (pack sd:8785), and one whose lane entry
    failed is the panel's Fix (pack sd:8791)."""
    sd_db = sd_handoff_rows.library()
    try:
        import sd_db.build as build
    except ImportError:
        raise WorkRefusal("update this pack checkout, whose lib/ is the sd_db bin/ runs for builds") from None
    if args.drop and (len(args.items) != 1 or args.finding):
        raise WorkRefusal("--drop takes one item and no --finding")
    sys.path.insert(0, str(BIN))
    try:  # by name from bin/, as `holders` loads the dashboard: a caller may not have bin/ on its path
        sd_bundle_drop = importlib.import_module("sd_bundle_drop")
    finally:
        sys.path.remove(str(BIN))
    connection = sd_handoff_rows.connect(sd_db, write=True)
    try:
        if args.drop:
            queued = sd_bundle_drop.drop_item(connection, build, args.items[0], who=getpass.getuser())
        else:
            queued = build.queue_again(connection, args.items, who=getpass.getuser(), finding=args.finding)
    except (sd_db.SdDbError, ValueError) as error:
        raise WorkRefusal(str(error)) from error
    except sd_bundle_drop.DropRefused as error:
        raise WorkRefusal(f"Drop refused: {error}") from error
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
    """The five-minute load from `sd gate status --json` and the builder cap, `build.load_cap`; (None, None)
    when unread. The gate's own `load_max` is not the builders' (sd:9094)."""
    sd_handoff_rows.library()
    from sd_db import follow  # noqa: PLC0415 - after the helper
    try:
        # The marker keeps a mismatch refused in the child too: no follow under GATE_SECONDS (pack sd:8685).
        done = subprocess.run([sys.executable, str(BIN / "sd"), "gate", "status", "--json"], capture_output=True,
                              text=True, timeout=GATE_SECONDS, check=False, stdin=subprocess.DEVNULL,
                              env={**os.environ, follow.MARKER: "1"})
        answer = json.loads(done.stdout) if done.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None
    from sd_db import build  # noqa: PLC0415 - after the helper
    load, cap = answer.get("load") if isinstance(answer, dict) else None, build.load_cap()
    if not (isinstance(load, list) and len(load) >= 2 and isinstance(load[1], (int, float)) and math.isfinite(load[1])
            and math.isfinite(cap)):
        return None, None
    return build.counted(answer, float(load[1])), float(cap)


def interactive_builders(connection: Any, now: str, key: Any) -> tuple[list[dict] | None, str]:
    """One `{repo, session_id}` per slot the dashboard's `claude_sessions.builders` holds, and "";
    (None, why) when not counted.

    `repo` is the registered repository the holder's cwd lies in, else None
    (it counts against the machine only). A cwd in a linked worktree, such as
    one outside `~/repos`, lies in its main checkout, read from git's common
    dir by `sd_lib.main_worktree_root` (pack sd:6112). The holders are the one
    rule: an unread session is one slot, not an uncounted machine (pack sd:8606).
    """
    import sd_lib
    sys.path.insert(0, str(BIN.parent / "dashboard"))
    try:
        # By name, as `sd_work` loads its modules: the dashboard is not on bin/'s lint path.
        claude_sessions = importlib.import_module("sd_dashboard.claude_sessions")
        counted = claude_sessions.document(connection, now=now)["builders"]
        if counted["running"] is None:
            return None, counted["why"] or "no reason was given"
        repos = [row["path"] for row in connection.execute("SELECT path FROM repo")]
        found = []
        for holder in counted["holders"]:
            cwd = key(str(sd_lib.main_worktree_root(Path(holder["cwd"]).resolve()))) if holder.get("cwd") else ""
            repo = next((path for path in repos if cwd and (cwd == path or cwd.startswith(path.rstrip("/") + "/"))),
                        None)
            found.append({"repo": repo, "session_id": holder["session_id"]})
        return found, ""
    except Exception as error:  # noqa: BLE001 -- any failure is "not counted", which claims nothing
        return None, f"{type(error).__name__}: {error}"[:300]
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
        commits = sd_lane.unreverted(worktree, head, sd_lane.branch_commits(worktree, branch, head))
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
        try:
            done = subprocess.run(["sh", str(guard), "check", "--range", f"refs/remotes/origin/{branch}..{head}"],
                                  cwd=worktree, capture_output=True, text=True, timeout=60, env=environ,
                                  check=False)
        except (OSError, subprocess.TimeoutExpired):  # pack sd:9483: no answer adds nothing
            done = None
        if done is not None and done.returncode == 1:
            found.append("the pre-push leak guard refuses these commits; replace each private value with a "
                         "synthetic one (operator, example.test):\n" + done.stderr.strip()[-1500:])
    return found


def own_checks(worktree: Path) -> list[list[str]]:
    """The checks a build runs where `sd-review` ran no gate, as `sd-check` picks them (pack sd:7868).

    The Makefile `precheck` first, as the gate runs it (sd:10286), then the
    aggregate `check` when the repository has one, else each of `test` and
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
    first = [list(found.precheck)] if found.precheck else []
    return first + [list(found.commands[name]) for name in sd_lib._runs(found.commands)]


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


def hub_outages() -> tuple[type[BaseException], ...]:
    """The hub's brief outages a tick skips with exit 0, as a locked database (pack sd:9541): the hub timed out, a
    hub refresh is ahead of this satellite's follow (`outage` refuses any other mismatch), or Tailscale names no
    hub node (`outage` refuses any other `Fenced`, such as a demotion, which lasts: pack sd:9574)."""
    sd_handoff_rows.library()
    from sd_db import hub, remote  # noqa: PLC0415 - after the helper
    return remote.HubUnreachable, remote.BuildMismatch, hub.Fenced


#: Skipped ticks in a row that exit 0, a mismatch aside; the next fails, so a lasting outage, a hub that hangs or
#: a held lock alerts (pack sd:9541).
SKIPS = 2


def skip_stamp() -> Path:
    """`$XDG_STATE_HOME/sd/dispatch-skipped`: the first skip's time and the count of skipped ticks in a row, beside
    the tick's other local state. Only a tick that runs to its end removes it."""
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.environ.get("HOME") or os.path.expanduser("~"),
                                                             ".local/state")
    return Path(state) / "sd" / "dispatch-skipped"


def utc_now() -> str:
    """Now in UTC, as the stamp records a streak's first skip."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def counted(reason: str, failure: str) -> str:
    """Count one more skipped tick: `reason` with the streak while it is short, else `failure`, the latest skip's
    own text with its remedy, as a refusal.

    A stamp that does not read or parse starts a new streak at one; it never reads as healthy. A count that cannot
    be written is unknown, so the tick refuses. Only the count ends the skips: a clock jump moves no tick.
    """
    stamp = skip_stamp()
    try:
        held = json.loads(stamp.read_text(encoding="utf-8"))
        count, first = held["count"], held["first"]
        if not (isinstance(count, int) and count > 0 and isinstance(first, str)):
            raise ValueError(stamp)
    except (OSError, ValueError, KeyError, TypeError):  # absent, unreadable or corrupt: a new streak
        count, first = 0, utc_now()
    count += 1
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        written = stamp.with_name(stamp.name + ".tmp")
        written.write_text(json.dumps({"first": first, "count": count}), encoding="utf-8")
        os.replace(written, stamp)
    except OSError as failed:
        raise WorkRefusal(f"{failure}; the skip count was not recorded in {stamp}: {failed}") from None
    if count > SKIPS:
        raise WorkRefusal(failure)
    return f"{reason} (skipped tick {count} in a row since {first}; tick {SKIPS + 1} fails)"


def end_skips() -> None:
    """The tick ran to its end: the skip streak ends. A stamp that stays only makes the next skip alert sooner."""
    try:
        skip_stamp().unlink(missing_ok=True)
    except OSError:
        pass


def outage(error: BaseException) -> tuple[str, str | None]:
    """The `skipped` reason for one of `hub_outages`, and the text the streak's failing tick refuses with.

    A `Fenced` counts only when Tailscale named no node (`cause` `tailscale`); any other refuses at once.
    A mismatch skips uncounted, None: the follow job follows (pack sd:8685), and a satellite stays mismatched for
    minutes after every pack merge, so a count would alert on each; a stuck follow alerts in its own report. Every
    hub refresh differs only in its build digest, `upgrade` `both`. One whose `upgrade` names the hub needs
    `repo-sync.sh refresh` there, which no follow job runs, so it refuses at once (pack sd:9541).
    """
    sd_handoff_rows.library()
    from sd_db import hub, remote  # noqa: PLC0415 - after the helper
    if isinstance(error, hub.Fenced) and getattr(error, "cause", None) != "tailscale":
        raise WorkRefusal(str(error)) from error  # a demotion, or a hub too old to say: lasting (pack sd:9543)
    if not isinstance(error, remote.BuildMismatch):
        return str(error), str(error)
    if getattr(error, "upgrade", None) == "hub":
        raise WorkRefusal(str(error)) from error
    return "sd_db build mismatch; the follow job follows", None


def say_skipped(args: argparse.Namespace, skip: tuple[str, str | None], partial: bool) -> None:
    """A skipped tick's one line, once `counted` unless its failure text is None: JSON under `--json` (pack
    sd:9541), `skipped (partial)` once the tick had begun."""
    reason = skip[0] if skip[1] is None else counted(skip[0], skip[1])
    if args.json:
        print(json.dumps({"skipped": reason, "partial": partial}, ensure_ascii=False))
    else:
        print(f"skipped{' (partial)' if partial else ''}: {reason}")


class Overdue(BaseException):
    """The tick ran past `TICK_SECONDS`: a BaseException, so no broad `except Exception` in the tick holds it."""


def dispatch_builds(args: argparse.Namespace) -> int:
    """`sd assignments dispatch`: one build-dispatch tick (pack sd:6094)."""
    import sd_lib
    # A follow takes 2-4 min, past the tick's budget, and holds the refresh lock the repo-sync-follow job
    # waits on: the tick keeps the refusal, as a lane runner does, and that job follows (pack sd:8685).
    sd_lib.on_mismatch = None
    sd_db, build, build_run = build_modules()
    from sd_db import hub  # noqa: PLC0415 - the copy build_modules imported
    hub.on_mismatch = None
    why = skip = ""
    try:
        root = log_root()
    except sd_lib.ConfigError as error:  # a malformed setting is unread: no claim, but the reap and watch run
        root, why = None, f"{error}, so a build run has no log folder"
    if root is None and not why:
        why = "sd.bulk_storage_root is unset, so a build run has no log folder"
    elif root is not None and (ready := writable(root)) is not True:
        # A live run logs locally (pack sd:8246): a bulk root that takes no write holds the archive, not the
        # claim; the ended runs' files wait locally for a tick whose bulk root answers (pack sd:8845).
        skip = (f"the bulk root {root} is not writable" if ready is False
                else f"the bulk root {root} did not answer in {PROBE_SECONDS:g} s")

    def overdue(*_: Any) -> None:
        raise Overdue
    # One bound for the whole tick, the waits on a lock and the lane list too; a skip exits 0 (pack sd:6494).
    signal.signal(signal.SIGALRM, overdue)
    armed = time.monotonic()
    signal.setitimer(signal.ITIMER_REAL, TICK_SECONDS)
    unstarted: dict[str, str] = {}
    began: list[bool] = []  # the tick's flag: it connected, so work it began may have committed (pack sd:9541)
    skipped: tuple[str, str | None] | None = None  # the reason, and the text the streak's third skip refuses with
    try:
        result = tick(sd_db, build, build_run, root, why, unstarted, archive=not skip, began=began)
    except Overdue:
        skipped = (f"the tick ran past {TICK_SECONDS:g} s",) * 2
    except hub_outages() as error:
        skipped = outage(error)
    except sqlite3.OperationalError as error:
        if "locked" not in str(error) and "busy" not in str(error):
            raise WorkRefusal(str(error)) from error
        skipped = (str(error),) * 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        # The copier dies with this process: it keeps the rest of the tick's budget, as before sd:8275 (sd:8339).
        build_run.archive_wait(TICK_SECONDS - (time.monotonic() - armed))
    if skipped is not None:
        say_skipped(args, skipped, bool(began))
        return 0
    if skip:
        result["archive_skipped"] = skip
    if unstarted:  # an unwritable run log skips that run's supervisor, not the tick
        result["spawned"] = [ident for ident in result["spawned"] if ident not in unstarted]
        result["why"] = "; ".join(filter(None, [result["why"], *unstarted.values()]))
    print(json.dumps(result, ensure_ascii=False) if args.json else
          f"reaped {len(result['reaped'])}, moved {len(result['moved'])}, swept {len(result['swept'])}, claimed {len(result['claimed'])}, "
          f"drafted {len(result['drafted'])}, spawned {len(result['spawned'])}" + (f"; no claim: {result['why']}" if result["why"] else "")
          + (f"; no archive: {skip}" if skip else ""))
    return 0


def tick(sd_db: Any, build: Any, build_run: Any, root: Path | None, why: str, unstarted: dict[str, str], *,
         archive: bool = True, began: list[bool] | None = None) -> dict:
    """`dispatch_builds`' tick in the database; each run whose supervisor could not start goes in `unstarted`.

    `archive` False: the bulk root did not take a write, so no ended run's files move to it this tick. `began` gets
    True once the tick connected, so a later skip says it was partial (pack sd:9541)."""
    outages = hub_outages()  # they keep their types, so `dispatch_builds` skips them
    load, load_cap = gate_load()
    # A lock while the database opens is the locked-database skip, so it keeps its type too (pack sd:9541).
    connection = sd_handoff_rows.connect(sd_db, write=True, keep=(*outages, sqlite3.OperationalError))
    if began is not None:
        began.append(True)
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

        interactive, uncounted = interactive_builders(connection, build_run.now(), build_run.paths.key)
        # The beat names the reason, so the hub can say why (pack sd:8606). The claim's own name: `why` also says
        # whether the bulk root answered, and an uncounted machine still archives its logs (pack sd:8615).
        refusal = why or (f"{build.UNREAD[1]}: {uncounted}" if interactive is None else "")
        result = build_run.dispatch(connection, owner="build-dispatch", load=load, load_cap=load_cap,
                                    interactive=interactive, spawn=spawn, lane=lane, why=refusal)
        result["pushed"] = push_failures(connection)
        if root is not None and not why and archive:  # the bulk root answered the probe
            result["logs_moved"] = build_run.archive(connection, build_run.local_root(), root)
    except outages:
        raise
    except (sd_db.SdDbError, ValueError, OSError) as error:
        raise WorkRefusal(str(error)) from error
    finally:
        connection.close()
    end_skips()  # the whole tick ran: a connect alone is no pass, as a hub may drop each request after it
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
            if (name, action) == ("assignments", "get"):
                parser.set_defaults(handler=get_assignments)
                parser.add_argument("identity", nargs="+", help="assignment ID")
                parser.add_argument("--wait", action="store_true",
                                    help="poll until every assignment has left queued, claimed and running")
                parser.add_argument("--timeout", type=float, metavar="MIN",
                                    help="with --wait: give up after MIN minutes, exit 1 (default 240)")
                parser.add_argument("--until", choices=("ended", "merged"), default="ended",
                                    help="with --wait: merged also waits out a done build's lane entry, "
                                         "exit 0 only when each merged")
            elif (name, action) == ("services", "list"):
                parser.add_argument("--record", type=int, metavar="SECONDS",
                                    help="also store this machine's disk and services for Health; the job runs "
                                         "every SECONDS, and a reading older than that is stale (sd:9757)")
            elif action != "list":
                parser.add_argument("identity", help={"jobs": "registered job name",
                    "assignments": "assignment ID", "services": "launchd label (get also accepts system:LABEL)"}[name])
            if action in {"retry", "cancel", "start", "stop", "restart"}:
                parser.add_argument("--if-revision", help="require the exact revision you inspected")
        if name == "assignments":
            parser = verbs.add_parser("build", help="queue a build of each item, a planning one set ready "
                                                    "first; retry its blocked or "
                                                    "cancelled bundle, or Fix its failed lane entry; the whole "
                                                    "selection or none")
            parser.set_defaults(handler=queue_builds)
            parser.add_argument("items", nargs="+", type=item_id, help=ITEM_HELP)
            parser.add_argument("--finding", default="", metavar="TEXT",
                                help="Send to builder: the note the retried or fixed bundle's builder reads")
            parser.add_argument("--drop", action="store_true",
                                help="take the one item out of its ended bundle: the kept items' commits go on a "
                                     "fresh branch under a kept lead, and the item builds alone (pack sd:9709)")
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
                                "state-move, in-lane or p1, why the repository gets a second open branch "
                                "(pack sd:7737, sd:9700)")
            parser.add_argument("--json", action="store_true")
