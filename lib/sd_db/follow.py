"""A satellite's answer to a build-mismatch refusal: follow the hub's pins once, then run the command again (sd:4748).

The hub refuses a session whose `sd_db` build differs from its own
(`remote.BuildMismatch`). After every pack merge that changes the digest,
each satellite session was refused until `repo-sync.sh follow` ran, by hand
or by its 5-minute job: six episodes on 2026-10-15, about 13 minutes of
session time (sd:4188, the 18:12 MDT decision note).

No retry inside this process can pass (sd:3359): the handshake names the
build this process loaded, and `follow` changes the files on disk, not the
code in memory. So `attempt` runs the follow as a child, bounded, and on
success replaces this process with the same command, which loads the
followed build. Only an entrypoint that may run again from its start
installs it, through `hub.on_mismatch`; a server, a lane runner, a ship
or a build-dispatch tick keeps the refusal. A tick's 75 s budget cannot
hold a follow, and the repo-sync-follow job does it (pack sd:8685).

Guards:

* Recursion: `MARKER` is set in the follow's environment and in the re-run
  command's. A process that finds it set keeps the refusal, so a follow that
  calls `sd`, and a re-run still refused, never follow again.
* Mid-command: a process that already opened a hub session is part way
  through its verb; a re-run would do its first part twice (`sd gate run`
  would run the gate command again). It keeps the refusal.
* Stdin: a re-run reads the same descriptor 0. No `sd` verb reads stdin
  before its first hub session, or at all (bin/sd and the modules it
  dispatches to, read 2026-10-15), so a terminal, `/dev/null` or a file at
  offset 0 replays. A file past offset 0 was read; a pipe or socket cannot
  say whether it was. Both keep the refusal with the hint to run the
  command again after a follow by hand.
* Concurrency: two sessions following at once rely on follow's own refresh
  lock (`refresh_drain.py`). Its wait is capped at `DRAIN_WAIT`, so a lane
  run holding its runner lock on this satellite fails the follow instead of
  holding the session; a refused or waiting follow is a failed one.
* Time: the whole follow runs under `TIMEOUT`; past it its whole process
  group is killed, not just the shell `sh` ran, so the `refresh_drain.py`
  child that holds the refresh and lane locks and the `make setup` it may
  have started do not go on changing checkouts and config after `sd`
  reports failure. The refusal stands, and follow's own intent marker makes
  its next run finish the half-done move.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import remote

#: Set for the follow child and the re-run command: a process that finds it keeps the refusal.
MARKER = "SD_FOLLOWED"
#: Seconds the follow may take: two bounded fetches, the drain wait, the pack's
#: `make setup` and two machine-setup stages fit a typical run in two to four
#: minutes; ten minutes is the bound builders already run tests under, and a
#: session waiting longer than that is better off with the refusal.
TIMEOUT = 600.0
#: Seconds follow's drain waits for its refresh lock and the lane locks
#: (`REPO_SYNC_DRAIN_WAIT`, default 45 minutes). Another session's follow ends
#: inside this; a lane run on this satellite does not, and its entry is put
#: back by the lane's own hub-fault handling.
DRAIN_WAIT = 120
#: The system checkout's follow, under the registered plugin root that holds it.
SCRIPT = Path("local-repo-sync") / "repo-sync.sh"
#: Lines of the follow's output shown when it fails.
TAIL = 6
#: How long a killed follow's output is read when its group kill was refused (sd:7900).
REAP_SECONDS = 5


def script(roots) -> Path | None:
    """The follow script under the first of `roots` that holds one, or `None`."""
    for root in roots:
        candidate = Path(root) / SCRIPT
        if candidate.is_file():
            return candidate
    return None


def stdin_consumed(fd: int = 0) -> str | None:
    """Why a re-run could not read what this command's stdin held, or `None` when it can.

    A closed descriptor is closed for the re-run too, so that replays.
    """
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return None
    if stat.S_ISREG(mode):
        return "stdin is a file already read" if os.lseek(fd, 0, os.SEEK_CUR) > 0 else None
    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return "stdin is a pipe"
    return None


def _exec(argv: list[str], env: dict[str, str]) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(sys.executable, [sys.executable, *argv], env)


def attempt(mismatch: remote.BuildMismatch, roots, *, environ=None, argv=None, out=None,
            rerun=_exec, timeout: float = TIMEOUT, stdin: int = 0) -> None:
    """Follow the hub's pins once and run this command again; return when the refusal stands.

    Prints one line to `out` naming both digests and what happened, so a
    session sees why it waited. Returns only when the caller is to raise the
    refusal it holds: the marker is set, a session already opened, stdin
    cannot be read again, no follow is registered, or the follow failed, was
    refused or timed out.
    """
    environ = os.environ if environ is None else environ
    argv = sys.argv if argv is None else argv
    out = sys.stderr if out is None else out
    digests = f"sd_db build {remote.build_digest()} here, {mismatch.hub_build or mismatch.hub} on the hub"
    if environ.get(MARKER):
        out.write(f"sd: {digests}: still refused after a follow in this process chain; the refusal stands\n")
        return
    if remote.sessions_opened():
        out.write(f"sd: {digests}: the hub changed while this command ran; the refusal stands, run it again\n")
        return
    consumed = stdin_consumed(stdin)
    if consumed:
        out.write(f"sd: {digests}: {consumed}, which a re-run cannot read again; the refusal stands, "
                  f"run the command again after `{SCRIPT.name} follow`\n")
        return
    path = script(roots)
    if path is None:
        out.write(f"sd: {digests}: no registered plugin holds {SCRIPT}, so nothing can follow the hub's pins; "
                  f"register the system checkout with `sd plugin add` or run `repo-sync.sh follow`\n")
        return
    env = {**environ, MARKER: "1", "REPO_SYNC_DRAIN_WAIT": str(DRAIN_WAIT)}
    out.write(f"sd: {digests}: following the hub's pins (`{path} follow`, up to {timeout:g} s)\n")
    out.flush()
    started = time.monotonic()
    try:
        code, output, timed_out = _run_follow(path, env, timeout)
    except OSError as error:
        _failed(out, digests, f"could not start ({error})", "")
        return
    elapsed = time.monotonic() - started
    if timed_out:
        _failed(out, digests, f"timed out after {timeout:g} s", output)
        return
    if code != 0:
        _failed(out, digests, f"exited {code} after {elapsed:.0f} s", output)
        return
    out.write(f"sd: {digests}: follow exited 0 after {elapsed:.0f} s; running the command again\n")
    out.flush()
    rerun(list(argv), env)


def _run_follow(path: Path, env: dict[str, str], timeout: float) -> tuple[int, str, bool]:
    """Run `path follow` in its own process group; on timeout kill the whole group.

    Returns `(returncode, output, timed_out)`. `subprocess.run`'s timeout
    kills only the direct shell, which `sh` has already replaced with
    `refresh_drain.py`'s child and its `make setup`; those keep the locks and
    go on writing. `start_new_session` makes the shell a group leader, so one
    `killpg` ends every descendant that has not changed its own group. The
    group is killed on the timeout and on any other way out of the wait, so
    no follow is ever left running past this function: `KeyboardInterrupt`
    unwinds through `except BaseException`, and `SIGTERM` or `SIGHUP` (a
    closed terminal, a `kill`) would otherwise end `sd` without unwinding,
    so each is trapped to kill the group and then re-delivered so `sd` dies
    as the operator asked. `SIGKILL` cannot be trapped; repo-sync's intent
    marker and stale-lock sweep finish or undo a follow orphaned that way.
    The traps go in before the spawn: a signal while `Popen` is still
    returning, its group already running, waits for the group (sd:5801).
    """
    process = None
    held = []

    def terminate(signum, _frame):
        if process is None:
            held.append(signum)
            return
        _kill_group(process)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    installed = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                installed[sig] = signal.signal(sig, terminate)
            except (OSError, ValueError):  # a platform without the signal
                pass
    try:
        process = subprocess.Popen(["sh", str(path), "follow"], env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=True)
    except BaseException:
        for sig, handler in installed.items():
            signal.signal(sig, handler)
        raise
    if held:
        terminate(held[0], None)
    try:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output, False
    except subprocess.TimeoutExpired:
        output = _kill_group(process)
        return process.returncode, output, True
    except BaseException:
        # Ctrl-C, or anything else that unwinds the wait: the group is
        # detached, so take it down before the exception propagates.
        _kill_group(process)
        raise
    finally:
        for sig, handler in installed.items():
            signal.signal(sig, handler)


def _kill_group(process: subprocess.Popen) -> str:
    """SIGKILL the process's group and reap it, returning whatever output it wrote."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:  # EPERM: macOS for an exiting group, or a sandbox (sd:7900)
        process.kill()
    try:
        output, _ = process.communicate(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired as error:
        # A descendant the group kill missed (refused, or in a group of its own) holds the pipe: stop reading it.
        if process.stdout is not None:
            process.stdout.close()
        process.wait()
        said = error.output or ""
        return said.decode(errors="replace") if isinstance(said, bytes) else said
    return output


def _failed(out, digests: str, what: str, output: str) -> None:
    tail = [line for line in output.splitlines() if line.strip()][-TAIL:]
    out.write(f"sd: {digests}: follow {what}; the refusal stands\n")
    for line in tail:
        out.write(f"  follow: {line}\n")
    out.flush()
