"""The collectors the system dashboard was built around, and nothing else.

**That dashboard is gone.** Its HTTP server, its page, its assets and its
write endpoints were deleted at 6b-8 of the artifacts-as-product rollout. What
is left is the half that reads this machine -- the vault, the launchd job
table, the research checkouts, the local-* toolbox, the service map -- because
that half is system-owned and always was. It reached the pack dashboard
through the plugin contract, one tab per invocation, until sd:719 retired that
contract. The workflow dashboard in `sd_dashboard/` reads it now, in its
Operations Resources views and its Ports area.

**Two callers, and the Ports budget belongs to neither of them.** `sd_tile.py`
imports this module for its tabs. `sd_dashboard/ports_screen.py` loads it by
path and calls `collect_ports` inside the serving process, with no tile in
between. (`sd_dashboard/reports_screen.py` runs `sd_tile.py` as a child for
four views, so it reaches the collectors through the first caller. It
loads this module by path only for `Budget`, which reads that child within the
64KB and a ceiling each view declares, sd:758.)

The plugin contract was also a budget: the pack read each tile under five
seconds and 64 KB, while reading (`dashboard/plugins.py`, `TILE_SECONDS` and
`TILE_BYTES`, all three gone from the pack since sd:719 step 3). The
Resources views keep both figures (`VIEW_SECONDS` in `reports_screen.py`, and
`Budget` below). The in-process call had neither ceiling -- it passed
`timeout=12` and returned whatever the scan printed -- so until sd:722 the
same collector was bounded for one caller and unbounded for the other.

So `collect_ports` now carries the budget itself. `Budget` below is the one
definition. The size ceiling, `BUDGET_BYTES`, is shared by every collector.
The time ceiling is declared by each collector with `budgeted`, and a caller
cannot raise it. Every command a collection runs reads through one `Budget`,
which stops the command at either ceiling while reading and raises
`OverBudget`.

Ports declares 5 seconds, the retired plugin contract's figure. It declared 15
for a day while `machine-setup.sh candidates service` ran one lsof per port and took
10.7s to 34s (sd:722); since sd:756 that scan runs one lsof and one netstat
for the whole table.

Five seconds on a whole tile process was the retired plugin contract's outer
limit. The pack's loader enforced it for every tab; a Resources view's `Budget`
enforces it now for the three tabs it runs (`reports_screen.VIEWS`,
`VIEW_SECONDS`), which hold Toolbox, Vault and Research, and
kills the tile's process group there. Ports is not among them: no Resources
view runs the ports tile, and the Ports page calls `collect_ports` in-process.
`sd_tile.py` holds every tab a second earlier than five, so the collector
refuses, and stops its scan, before that kill arrives: a failed tab with the
reason on stderr. It does that for the tab as a whole through `set_deadline`;
`within`, which can only tighten a declaration, it passes for Ports alone, and
with the loader gone the only caller that reaches `within` is
`dashboard.sh tile ports` from a shell, which sets no outer limit of its own.
The Ports page applies the declaration as it stands and refuses visibly past it
instead of waiting. The other collectors here have no budget of their own,
because a tile is still their only way in; a second in-process caller for one
of them owes it the same treatment. On that way in, every command they run
stops at the tile's deadline (`set_deadline`, sd:760), so the tile refuses with
its reason before its caller's kill, whatever the command's own timeout.

Which dashboard that is has changed twice, so name it rather than say "the new
one": the pack's dashboard held :8767 for a week of 2026-09, and the workflow
server in `sd_dashboard/` -- this same directory -- took the port back at #221
and serves it now. A reader who arrives at this file looking for the program
that used to be here will find one next door, and it is not the one this
docstring was written against.

So this is a library now, not a program. No `main`, no port, no `Handler`, no
refresher thread, and no `if __name__ == "__main__"`: running it does nothing,
which is the honest shape for a file whose entry point is somebody else's.

**The writes went with the server, deliberately.** `update_note`, `set_ack` and
`note_path` are deleted rather than kept for later. The pack's actions were
commands and not forms, so nothing a page sent was interpolated into an argv
(R11-D25), and setting a note's status stays in Obsidian, where it already
happened and where the single-writer rules are specified. A dashboard that
could still write these notes from here would be the second writer that record
declines to become.

**What was dropped with them, and is not coming back through this file:** the
repo, work, PR, issue, Jira and rtk collectors, whose replacements were the
pack dashboard's own; the ack store; and the markdown renderer, which existed to
draw pages. Every knob still arrives as an environment variable, and with the
server went the shell that used to resolve them: the vault (`OBSIDIAN_VAULT`,
kept once in `<config>/shared.env`) and `REPO_ROOT` are defaulted here, in the
module that reads them, and `dashboard.sh` resolves only the interpreter now.

**No shebang, and no `main()`.** This file is imported by `sd_tile.py`,
`sd_dashboard/ports_screen.py` and `sd_dashboard/reports_screen.py`, and run by
nothing. It had both while it was a program.
"""

import ast
import contextlib
import datetime
import hashlib
import importlib.util
import ipaddress
import json
import os
import pathlib
import re
import select
import signal
import subprocess
import sys
import time
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent


def _config():
    """The pack's `lib/sd_db/config.py`, loaded by path (sd:5987): the shared config rule, without the cost of
    importing all of `sd_db` into a tile that runs under a deadline."""
    spec = importlib.util.spec_from_file_location("sd_tile_config", HERE.parent / "lib" / "sd_db" / "config.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {HERE.parent / 'lib/sd_db/config.py'}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


system_tools_config = _config()
#: The system checkout, which holds the tools these collectors read (`SYSTEM_TOOLS_ROOT`, sd:5987).
SYSTEM = system_tools_config.system_checkout()


def cron_job_files():
    """Every job file `cron-jobs.sh` would run, one per name, in name order.

    The same lookup (`system_tools_config.cron_job_dirs`): each
    `CRON_JOBS_EXTRA_DIRS` entry first, then this host's
    `<config>/cron-jobs/jobs/<host>`, then the shared `<config>/cron-jobs/jobs`;
    a job in an earlier directory overrides a same-named one in a later one.
    """
    found = {}
    for directory in system_tools_config.cron_job_dirs():
        for path in sorted(directory.glob("*.job")):
            found.setdefault(path.stem, path)
    return [found[name] for name in sorted(found)]

VAULT = system_tools_config.obsidian_vault()
REPO_ROOT = pathlib.Path(os.environ.get("REPO_ROOT", os.path.expanduser("~/repos")))
#: launchd label prefix shared by every system job and service.
LABEL_PREFIX = system_tools_config.label_prefix()


# The vault's area folders are its top-level `* Home` directories, found on
# disk rather than listed here, so an area added to the vault needs no edit.
AREA_SUFFIX = " Home"


def vault_areas(vault=None):
    """Top-level `<Area> Home` folders of the vault, sorted by name."""
    vault = VAULT if vault is None else vault
    try:
        return sorted(p.name for p in vault.iterdir()
                      if p.is_dir() and p.name.endswith(AREA_SUFFIX))
    except OSError:
        return []

# ------------------------------------------------------------------ helpers


def run(cmd, cwd=None, timeout=25):
    """Capture a command's stdout. A dead collector returns ''.

    The child gets its own process group so the timeout can kill the whole
    tree. subprocess.run() kills the direct child only, and a grandchild that
    inherited the stdout pipe keeps the read open — so the call hangs long past
    its timeout. machine-setup.sh's nested python3, stopped on a TCC prompt, is
    exactly that shape.

    Inside a tile the timeout is also capped at the tile's deadline (sd:760).
    A command cut there, or not started because the deadline has passed,
    raises `PastDeadline` instead of returning '': the tile is out of time,
    and a blank read would pass for a command with nothing to say. A command
    that times out on its own timeout first still returns '', and the wait
    for what it wrote after the kill stops at the deadline too (sd:763).

    One known limit (sd:763): a grandchild that calls setsid() leaves the
    group, so the kill misses it and it lives on, holding the pipe. The call
    still returns: a command cut at the deadline is not waited on at all, and
    any other waits no longer than that bounded wait after the kill."""
    left = time_left()
    # Three ways the deadline owns the wait, where `left < timeout` alone
    # covered one. `timeout is None` is a command with no ceiling of its own
    # and raised TypeError on that comparison; `left <= 0` is a deadline
    # already spent, which `0 < 0` let start anyway. `Budget.run` below tests
    # its own stop with `<=` for the same reason.
    capped = left is not None and (timeout is None or left <= 0 or left < timeout)
    if capped:
        if left <= 0:
            raise PastDeadline(f"{pathlib.Path(str(cmd[0])).name} was not started: "
                               f"this tile's budget of {seconds_terms(DEADLINE_SECONDS)} was spent",
                               started=False)
    # The deadline is absolute, so it is fixed before the child is created and
    # the wait is measured from it afterwards. Passing `timeout` straight to
    # `communicate` spent the process-creation time twice: once making the
    # child and again waiting for it, which put the wait past the deadline by
    # however long `Popen` took. `read_output` in `fleet.py` already bounds
    # itself this way.
    #
    # A capped wait stops at `DEADLINE` itself and not at `now + left`. `left`
    # was sampled above, so rebuilding the stop from a later `monotonic()`
    # hands back everything spent in between and puts the wait past the
    # deadline again, by exactly that much -- the same error the paragraph
    # above describes, moved one line up. `Budget.__init__` assigns
    # `self.stop = DEADLINE` for this reason; this is the same assignment.
    if capped:
        stop = DEADLINE
    else:
        stop = None if timeout is None else time.monotonic() + timeout
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True,
                             start_new_session=True)
    except OSError:
        return ""
    try:
        out, _ = p.communicate(timeout=None if stop is None else max(0.0, stop - time.monotonic()))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:  # gone, or EPERM (sd:7900): the leader at least goes
            p.kill()
        if capped:
            # The group is gone with its leader; what else holds the pipe is
            # not waited on, because the tile has no time left to wait with.
            p.kill()
            p.wait()
            p.stdout.close()
            raise PastDeadline(f"{pathlib.Path(str(cmd[0])).name} ran past "
                               f"this tile's budget of {seconds_terms(DEADLINE_SECONDS)}",
                               started=True) from None
        left = time_left()
        try:
            out, _ = p.communicate(timeout=5 if left is None else max(0, min(5, left)))
        except subprocess.SubprocessError:
            return ""
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out or "").strip()


# ------------------------------------------------------------------- budget

# What one collection may spend, whoever asked for it. The budget lives on the
# collector and not on its callers because a budget each caller applies is a
# budget the next caller forgets: `ports_screen.py` read `collect_ports` in
# process with `timeout=12` and no size ceiling at all (sd:722).
#
# The size ceiling is one number for every collector: 64KB of command output,
# the plugin contract's per-tile figure (`TILE_BYTES` in the pack's
# `dashboard/plugins.py`, which sd:719 step 3 deleted), copied rather than
# imported because this repository does not import the pack. The time ceiling is each collector's own, declared with
# `budgeted` above its `def`, because how long a scan takes is a fact about that
# scan. No caller can raise it: `Budget`'s `within` only lowers it.
BUDGET_BYTES = 64 * 1024
# What a bounded read keeps of a command's stderr: its tail, because an
# unbounded keep would put the command in charge of how much the collector
# holds, and the end is where a failing command says why. Two readers ask two
# questions of it. The ports scan asks only whether there was any. A Resources
# view (`reports_screen.collect`) shows it as the view's reason, and there the
# tail is one line, `sd_tile.main`'s `<tab>: <error>`, whose tab name is at the
# FRONT. A cap the line outgrows therefore drops the name first: at 512 bytes,
# a checkout under a 136-character HOME lost exactly `vault` from the Full Disk
# Access refusal and turned a green suite red with a message that named
# nothing (sd:830). That line embeds two paths, the vault's and the
# interpreter's, each at most PATH_MAX (1024 bytes on macOS), inside about 200
# bytes of prose, so 4 KB holds the longest one the tile was known to write.
# Known to, not could: an error of any other length was still the tile's to
# write, and one past the cap lost the name the same way (sd:834). So the
# tile bounds the line itself, to this many bytes (`sd_tile.ERROR_BYTES`,
# pinned to this value by `tests/test_stderr_cap.py`), and what a chatty
# child printed before it is all a cut here can drop. When a cut does
# happen, `Budget.run` says so at the front of what it kept, naming the tab
# the reader asked for (`label`) rather than the interpreter that ran it, so
# a headless tail cannot pass for the whole and a reader can tell whose it
# was.
BUDGET_STDERR = 4096


class OverBudget(RuntimeError):
    """A collection that ran past its budget and was stopped, not waited on."""


class PastDeadline(OverBudget):
    """A command stopped at, or refused past, the tile's deadline (sd:760).

    `started` says whether the command ran at all: a probe cut at the deadline
    has an answer to read from its silence, and one never started has none.
    """

    def __init__(self, message, *, started):
        super().__init__(message)
        self.started = started


# The tile's deadline, on `time.monotonic`'s clock, and the seconds it was set
# for. `sd_tile.main` sets both through `set_deadline`, `TILE_SECONDS` less
# `TILE_MARGIN` after it starts, for as long as it builds its tab. Nothing else does: the page, and the dashboard
# process that runs a tile as a child, load their own copy of this module and
# read with no deadline, exactly as before.
#
# Why the tile needs one of its own (sd:760): every command here starts in a
# session of its own so its timeout can kill its whole tree. A caller that
# kills the tile at five seconds kills the tile's group, which those sessions
# are not in, so a nested command outlived the tile with nobody left to enforce
# its timeout -- and a vault probe held by TCC lost its Full Disk Access reason
# to a bare "ran past its budget". With every nested read capped here, the tile
# stops its own commands and says why before the caller's kill arrives.
DEADLINE = None
DEADLINE_SECONDS = None


@contextlib.contextmanager
def set_deadline(seconds, *, started):
    """Cap every command this module runs at `seconds` after `started`.

    Only inside the `with`: a module the tile hands back to a caller in the
    same process (a test, for one) reads with no deadline again. A `with`
    nested inside another hands the outer deadline back when it exits (sd:763).
    """
    global DEADLINE, DEADLINE_SECONDS
    outer = DEADLINE, DEADLINE_SECONDS
    DEADLINE, DEADLINE_SECONDS = started + seconds, seconds
    try:
        yield
    finally:
        DEADLINE, DEADLINE_SECONDS = outer


def time_left():
    """Seconds left before the tile's deadline, or None outside a tile."""
    return None if DEADLINE is None else DEADLINE - time.monotonic()


def seconds_terms(seconds):
    return f"{seconds:g} second" + ("" if seconds == 1 else "s")


def budgeted(*, seconds):
    """Declare a collector's time ceiling, where the collector is defined.

    The declaration is the collector's attribute, `budget_seconds`, so the
    number sits on the line above the `def` it bounds and a caller reads it
    rather than supplies it.
    """
    def declare(collect):
        collect.budget_seconds = seconds
        return collect
    return declare


class Budget:
    """One collection's allowance, spent by every command the collection reads.

    The deadline and the byte count are shared across those commands, so two
    reads that each fit cannot add up to a collection that does not. Both are
    applied to the stream as it arrives. Reading everything and measuring
    afterwards would make the ceiling advisory -- the command would already
    have decided how long the caller waited and how much it holds -- which is
    the reason the pack's loader read a tile the same way, and the reason
    `reports_screen.collect` reads one through this class now.

    `seconds` is the collector's declaration. `within` is a caller's own outer
    limit and can only tighten it: `sd_tile.py` passes four seconds
    (`TILE_SECONDS - TILE_MARGIN`), so on that path the budget is the smaller
    of the two, and no caller can make it larger.

    Inside a tile the stop is also no later than the tile's deadline, which
    then names the budget in a refusal (sd:760).
    """

    def __init__(self, seconds, *, within=None):
        self.seconds = seconds if within is None else min(seconds, within)
        self.limit = BUDGET_BYTES
        self.stop = time.monotonic() + self.seconds
        self.cut = DEADLINE is not None and DEADLINE < self.stop
        if self.cut:
            self.stop = DEADLINE
        self.spent = 0

    def terms(self, which):
        if which == "seconds":
            return seconds_terms(DEADLINE_SECONDS if self.cut else self.seconds)
        if self.limit % 1024 == 0:
            return f"{self.limit // 1024} KB"
        return f"{self.limit} bytes"

    def run(self, argv, *, timeout=None, keep=None, label=None, **_):
        """`subprocess.run`'s answer for `argv`, read inside this budget.

        A `CompletedProcess` with text, so it stands in for `subprocess.run`
        wherever a collector takes a runner. `timeout` is the command's own and
        stays a `TimeoutExpired` when it ends the read first: lsof's three
        seconds degrade one observation rather than refuse the collection. The
        budget's deadline or byte ceiling raises `OverBudget` instead, after
        the command's whole process group is killed -- a grandchild holding
        stdout would otherwise keep the read open, the same shape `run`
        describes. The refusal names the command and the budget and nothing
        the command wrote, because what it wrote is not known to be safe to
        show.

        `keep`, when given, tests each stdout line as it arrives. Only the
        lines it keeps are held and count against the byte ceiling, so rows a
        collector would discard cannot spend the collection's 64KB (sd:756
        review B1: netstat's connection rows). A line still arriving counts
        until it ends, so the ceiling also bounds what is held.

        `label`, when given, is what a cut of the command's stderr is said to
        be a cut of. Without it the marker names the command: `sh`, or for a
        tile the interpreter, which tells a reader of a Resources view
        nothing about which tab was cut. `reports_screen.collect` passes the
        tab (sd:834). The budget refusals still name the command, as they
        did.
        """
        name = pathlib.Path(str(argv[0])).name
        if self.cut and self.stop <= time.monotonic():
            raise PastDeadline(f"{name} was not started: its budget of {self.terms('seconds')} was spent",
                               started=False)
        own = None if timeout is None else time.monotonic() + timeout
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        out, err, pending = bytearray(), b"", b""
        dropped = 0
        stdout, stderr = proc.stdout.fileno(), proc.stderr.fileno()

        def expired():
            if own is not None and own <= self.stop:
                return subprocess.TimeoutExpired(argv, timeout)
            if self.cut:
                return PastDeadline(f"{name} ran past its budget of {self.terms('seconds')}", started=True)
            return OverBudget(f"{name} ran past its budget of {self.terms('seconds')}")

        try:
            watch = [stdout, stderr]
            while watch:
                end = self.stop if own is None else min(self.stop, own)
                left = end - time.monotonic()
                if left <= 0:
                    raise expired()
                for fd in select.select(watch, [], [], left)[0]:
                    # One byte past the ceiling is enough to know it was
                    # crossed, and asking for more would allocate it first.
                    size = min(65536, self.limit - self.spent - len(pending) + 1) if fd == stdout else 65536
                    chunk = os.read(fd, size)
                    if not chunk:
                        watch.remove(fd)
                        if fd == stdout and pending and keep(pending):
                            self.spent += len(pending)
                            out += pending
                    elif fd == stderr:
                        err += chunk
                        if len(err) > BUDGET_STDERR:
                            dropped += len(err) - BUDGET_STDERR
                            err = err[-BUDGET_STDERR:]
                    else:
                        if keep is not None:
                            *lines, pending = (pending + chunk).split(b"\n")
                            chunk = b"".join(line + b"\n" for line in lines if keep(line))
                        self.spent += len(chunk)
                        if self.spent + len(pending) > self.limit:
                            raise OverBudget(f"{name} wrote more than its budget of {self.terms('bytes')}")
                        out += chunk
            # Both pipes closed; the exit is still inside the budget.
            while proc.poll() is None:
                end = self.stop if own is None else min(self.stop, own)
                left = end - time.monotonic()
                if left <= 0:
                    raise expired()
                time.sleep(min(left, 0.01))
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:  # gone, or EPERM (sd:7900): the leader at least goes, so the wait ends
                proc.kill()
            proc.wait()
            raise
        finally:
            proc.stdout.close()
            proc.stderr.close()
        text = err.decode("utf-8", "replace")
        if dropped:
            # Said at the front, where the bytes went missing: a reader that
            # expects `<tab>: <error>` there finds the cut instead of a line
            # that begins mid-word (sd:830), and finds it named for the tab
            # when the caller said which (sd:834).
            text = (f"[{label or name}: the first {dropped} bytes of its stderr were dropped; "
                    f"the last {BUDGET_STDERR} bytes follow] " + text)
        return subprocess.CompletedProcess(argv, proc.returncode, out.decode("utf-8", "replace"), text)


# macOS gates ~/Documents behind TCC, and a launchd job has nobody to click the
# prompt: on 2026-08-28 an unanswered one held collect_areas for 1605 seconds
# with the page stuck on "collecting…". Probing in a child bounds that to
# VAULT_PROBE_SECONDS and turns a silent hang into a visible error.
VAULT_PROBE_SECONDS = 15

# Run in a child by `vault_blocked`. Prints which of the three answers applies:
# the directory listed, it is not there, or something -- TCC, in practice --
# refused. A timeout prints nothing at all, which reads as refused, and that is
# right: an ungranted read under launchd does not fail, it waits forever.
VAULT_PROBE = (
    "import os, sys\n"
    "try:\n"
    "    os.listdir(sys.argv[1])\n"
    "except FileNotFoundError:\n"
    "    print('missing')\n"
    "except OSError:\n"
    "    print('denied')\n"
    "else:\n"
    "    print('ok')\n"
)

_VAULT_STATE = None


def probe_vault(executable, *, timeout=VAULT_PROBE_SECONDS):
    """What `executable` answers when it lists the vault.

    `ok`, `missing` or `denied` from `VAULT_PROBE`, or '' for a probe that
    said nothing: a TCC prompt nobody answered, or a path that did not run.
    The child is the binary whose access is in question, which is the point:
    macOS grants Full Disk Access per binary, and the tile has two
    interpreters (`sd_tile.py`'s module docstring), so `vault_blocked` asks
    this of `sys.executable` and `sd_tile.py --grants` asks it of each named
    path (sd:831).
    """
    # -I, as `dashboard.sh grants` uses for the interpreter that calls this.
    # The answer is meant to be the candidate binary's own, and without it a
    # caller's PYTHONPATH or user site-packages reaches the probe: a
    # `sitecustomize` runs before the -c body and can print or raise, so the
    # grant reported would not be the one the binary holds. The probe imports
    # `os` and `sys` only, so isolation costs it no import.
    return run([str(executable), "-I", "-c", VAULT_PROBE, str(VAULT)],
               timeout=timeout)


# The control `sd_tile.py --grants` reads: a binary that holds no Full Disk
# Access grant of its own, so what it gets is what the calling process's own
# access gets. Unbundled and platform-owned, so TCC attributes its read to the
# process responsible for it and not to an identity of its own.
VAULT_CONTROL = "/bin/ls"


def control_listing(*, timeout=VAULT_PROBE_SECONDS):
    """What `/bin/ls` gets for the vault: 'listed', 'refused', 'waited' or ''.

    The control has to run the operation the probe runs -- a listing. `ls -d`
    stats, and TCC gates the listing and not the stat: `/bin/ls -d` prints the
    name of a directory the same `/bin/ls` cannot list, so a stat as the
    control passes on every existing path and measures only that it exists
    (review-376 B1, measured on ~/Library/Safari).

    Four answers, not two, because `run` above returns '' for a command that
    was refused and for one that never ran alike, and a control that cannot
    tell those apart turns its own failure into a conclusive pass. So this
    keeps the exit status and the reason:

    - `listed`, exit zero: the caller's own access reached this binary, which
      holds no grant of its own.
    - `refused`: the failure named itself on stderr, the way an access failure
      does and a missing directory or a broken `ls` does not.
    - `waited`: nothing at all for the whole timeout. That is what an
      ungranted read looks like where nobody can answer the prompt -- the
      reason `VAULT_PROBE` reads its own silence as `denied` -- so it is a
      refusal that says how it refused rather than a failure to measure.
    - '': anything else. The control measured nothing, which is a state its
      caller has to carry rather than round off to either answer.

    The child is in a session of its own and the whole group is killed at the
    timeout, for the reason `run` above gives: a grandchild holding the pipe
    would otherwise keep the read open past it.

    It runs under `LC_ALL=C`, because `refused` is read off the words on
    stderr and those words are a locale's (sd:845). Darwin does not localise
    them today -- its `LC_MESSAGES` catalogues hold the yes/no expressions and
    no `strerror` at all, so `/bin/ls` says `Permission denied` under
    `fr_FR.UTF-8` as it does under `C`, measured 2026-09-14 -- so this pins a
    platform property rather than fixing a live bug. Worth pinning anyway: it
    costs one word, it is the only assumption in the classifier that the code
    could not state for itself, and the answer it would otherwise fall to is
    '', which is inconclusive and never a false pass.
    """
    try:
        proc = subprocess.Popen([VAULT_CONTROL, "--", str(VAULT)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, start_new_session=True,
                                env={**os.environ, "LC_ALL": "C"})
    except OSError:
        return ""
    try:
        _, why = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.kill()
        proc.wait()
        proc.stderr.close()
        return "waited"
    except subprocess.SubprocessError:
        return ""
    if proc.returncode == 0:
        return "listed"
    if re.search(r"Operation not permitted|Permission denied", why or ""):
        return "refused"
    return ""


def vault_refusal(executable, answer):
    """'' when `answer` is the probe's `ok`, otherwise why `executable` cannot read the vault.

    Four answers, not three. `denied` is the probe saying it was refused, and
    Full Disk Access is the whole of that story. '' is the probe saying
    nothing, which `probe_vault` documents as two stories -- a TCC prompt
    nobody answered, or a binary that did not run -- and one message asserted
    the first. A reader whose interpreter was simply broken granted access and
    saw the line unchanged, with nothing to suggest the grant was not the
    problem. The '' line keeps the grant as its first step, because that is
    the usual cause under launchd, and names the second so the reader knows
    what to check when the line does not move.
    """
    if answer == "ok":
        return ""
    if answer == "missing":
        return f"vault path does not exist: {VAULT}"
    if answer == "denied":
        return (
            f"cannot read {VAULT} — macOS is asking for Documents access and "
            f"nothing under launchd can answer the prompt. Grant Full Disk "
            f"Access to {executable} in System Settings > Privacy & "
            f"Security, then restart the agent.")
    return (
        f"cannot read {VAULT} — {executable} gave no answer within the probe's "
        f"{seconds_terms(VAULT_PROBE_SECONDS)}. Either macOS is asking for "
        f"Documents access and nothing under launchd can answer the prompt, or "
        f"this binary did not run at all. Grant Full Disk Access to "
        f"{executable} in System Settings > Privacy & Security, then restart "
        f"the agent; if the line does not change, run the probe by hand to see "
        f"whether the binary runs.")


def vault_blocked():
    """'' if the vault lists, otherwise why it does not.

    Probed once per process and answered the same way after. That is once per
    tile invocation, which is the right grain: the answer cannot change
    mid-run -- a TCC grant arriving between two reads of the same tile is not
    a case worth paying for twice.

    The child answers which failure it hit rather than the parent guessing.
    `VAULT.parent.exists()` was the only check, and it cannot tell a vault that
    was moved or renamed from one macOS is refusing to open -- both leave the
    parent standing, and the caller was told to grant Full Disk Access for a
    path that is not there. A `FileNotFoundError` from inside the probe is
    authoritative in a way no stat from out here is, because the child is the
    binary whose access is in question.
    """
    global _VAULT_STATE
    if _VAULT_STATE is not None:
        return _VAULT_STATE
    try:
        out = probe_vault(sys.executable)
    except PastDeadline as stopped:
        # A probe the tile's deadline cut short stayed silent as a timed-out
        # one does, and reads the same way: refused (sd:760). A probe the
        # deadline never let start has said nothing, so that refusal stands.
        if not stopped.started:
            raise
        out = ""
    _VAULT_STATE = vault_refusal(sys.executable, out)
    return _VAULT_STATE


def obsidian_url(rel):
    return "obsidian://open?vault=%s&file=%s" % (
        urllib.parse.quote(VAULT.name), urllib.parse.quote(rel))


# ------------------------------------------------------------- db read/write


# ---------------------------------------------------------------- git facts


def git_facts(path):
    """Branch, dirt, divergence and last commit for one checkout."""
    p = str(path)
    if not (path / ".git").exists():
        return None
    branch = run(["git", "-C", p, "rev-parse", "--abbrev-ref", "HEAD"]) or "?"
    dirty = len([line for line in run(["git", "-C", p, "status", "--porcelain"]).split("\n") if line.strip()])
    last = run(["git", "-C", p, "log", "-1", "--format=%cI%x1f%s%x1f%an"])
    when, subject, author = (last.split("\x1f") + ["", "", ""])[:3]
    ahead = behind = None
    counts = run(["git", "-C", p, "rev-list", "--left-right", "--count", "@{upstream}...HEAD"])
    if counts and "\t" in counts:
        b, a = counts.split("\t")[:2]
        behind, ahead = int(b), int(a)
    remote = run(["git", "-C", p, "remote", "get-url", "origin"])
    web = ""
    m = re.match(r"(?:git@github\.com:|https://github\.com/)(.+?)(?:\.git)?$", remote)
    if m:
        web = "https://github.com/" + m.group(1)
    return {
        "branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind,
        "last": when[:10], "last_iso": when, "subject": subject, "author": author,
        "web": web,
    }


# --------------------------------------------------------------- collectors

# Where a research document's page is built, newest layout first: sd-research-kit renders into docs/dashboard/, and
# build/ is where it rendered before (sd:2122). The first that holds the page is the one compared with its source.
RESEARCH_OUTPUT = ("docs/dashboard", "build")


def read_research_config(path):
    """Read declared data without executing repository Python on a page GET."""
    if path.stat().st_size > 2 * 1024 * 1024 or path.is_symlink():
        raise ValueError("research configuration is linked or oversized")
    tree = ast.parse(path.read_text())
    expressions = {target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                   for target in node.targets if isinstance(target, ast.Name)}
    visiting = set()

    def value(node, depth=0):
        if depth > 30:
            raise ValueError("research data is too deeply nested")
        def child(part):
            return value(part, depth + 1)
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [child(part) for part in node.elts]
        if isinstance(node, ast.Dict):
            result = {}
            for key, part in zip(node.keys, node.values, strict=True):
                if key is None:
                    result.update(child(part))
                else:
                    result[child(key)] = child(part)
            return result
        if isinstance(node, ast.Name) and node.id in expressions and node.id not in visiting:
            visiting.add(node.id)
            try:
                return child(expressions[node.id])
            finally:
                visiting.remove(node.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict" and len(node.args) <= 1:
            result = dict(child(node.args[0])) if node.args else {}
            for keyword in node.keywords:
                if keyword.arg is None:
                    result.update(child(keyword.value))
                else:
                    result[keyword.arg] = child(keyword.value)
            return result
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = child(node.left), child(node.right)
            if type(left) is type(right) and isinstance(left, (str, list)) and len(left) + len(right) <= 100000:
                return left + right
        raise ValueError("research configuration uses computed data that cannot be read safely")

    return research_inventory({key: value(expressions[key]) for key in ("PROJECT", "DOCS") if key in expressions})


def research_inventory(result):
    """`result` when its DOCS is a list of at most 100 dicts, each with a non-empty string src and out; else ValueError."""
    if not isinstance(result.get("DOCS", []), list) or len(result.get("DOCS", [])) > 100:
        raise ValueError("research document inventory is invalid or oversized")
    for index, document in enumerate(result.get("DOCS", []), 1):
        if not isinstance(document, dict):
            raise ValueError(f"research document {index} must be a declared dictionary; computed configuration is not executed")
        for field in ("src", "out"):
            if not isinstance(document.get(field), str) or not document[field].strip():
                raise ValueError(f"research document {index} requires a declared non-empty string {field!r}; computed configuration is not executed")
    return result


# What `sd-research-kit render` writes beside its pages (pack sd:5658): the PROJECT and DOCS it executed, and the sha256 of
# the research.conf.py bytes it ran. A config this reader refuses is read from here; nothing here runs repository code.
RESEARCH_MANIFEST = "docs/dashboard/research.json"
RESEARCH_MANIFEST_BYTES = 2 * 1024 * 1024
# The destinations each manifest document flags (pack sd:5936), so a designation a config sets in code still shows (sd:5898).
RESEARCH_MANIFEST_FLAGS = ("notion", "drive")
COMPUTED_FIX = "research.conf.py computes its data; run sd-research-kit render once to write docs/dashboard/research.json"


def read_research_manifest(repo):
    """The manifest's PROJECT and DOCS and whether an older research.conf.py wrote it; None when there is no manifest."""
    path = repo / RESEARCH_MANIFEST
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(repo.resolve()):
        raise ValueError(f"{RESEARCH_MANIFEST} is linked, not a regular file, or outside its repository")
    with path.open("rb") as handle:
        data = handle.read(RESEARCH_MANIFEST_BYTES + 1)
    if len(data) > RESEARCH_MANIFEST_BYTES:
        raise ValueError(f"{RESEARCH_MANIFEST} exceeds the bounded read size")
    try:
        manifest = json.loads(data)
    except ValueError:
        raise ValueError(f"{RESEARCH_MANIFEST} is not JSON; run sd-research-kit render to rewrite it") from None
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError(f"{RESEARCH_MANIFEST} is not a version 1 manifest")
    result = {"DOCS": manifest.get("docs")}
    if isinstance(manifest.get("project"), str):
        result["PROJECT"] = manifest["project"]
    research_inventory(result)

    def text(document):
        # The render writes strings, and one true or false per destination it designates (pack sd:5936); anything else is
        # dropped, so the board's cells and links stay text.
        links = document.get("links") if isinstance(document.get("links"), list) else []
        return {key: value for key, value in document.items() if isinstance(value, str)} | {
            key: document[key] for key in RESEARCH_MANIFEST_FLAGS if type(document.get(key)) is bool} | {
            "links": [link for link in links if isinstance(link, list) and len(link) == 2 and all(isinstance(part, str) for part in link)]}

    result["DOCS"] = [text(document) for document in result["DOCS"]]
    with (repo / "research.conf.py").open("rb") as handle:
        current = hashlib.file_digest(handle, "sha256").hexdigest()
    return result, manifest.get("conf_sha256") != current


def read_research_inventory(conf):
    """PROJECT and DOCS, and where they came from: "" declared in research.conf.py, "current" or "older" a manifest.

    The manifest is read only when the declared reader refuses the config: a config that declares its data is read
    as written, whatever an earlier render left.
    """
    try:
        return read_research_config(conf), ""
    except ValueError as refused:
        found = read_research_manifest(conf.parent)
        if found is None:
            raise ValueError(COMPUTED_FIX if "computed" in str(refused) else str(refused)) from None
        return found[0], "older" if found[1] else "current"


def collect_research():
    """Every checkout carrying a research.conf.py — the repos rendered by
    sd-research-kit, which is where this page's visual identity comes from."""
    items = []
    # A linked group, checkout or config is never read (no config parse, no git): with none followed, the walk stays in REPO_ROOT.
    roots = [REPO_ROOT] + [d for d in sorted(REPO_ROOT.glob("*")) if d.is_dir() and not d.is_symlink()]
    seen = set()
    for root in roots:
        for conf in sorted(root.glob("*/research.conf.py")):
            repo = conf.parent
            if repo in seen or repo.is_symlink() or conf.is_symlink():
                continue
            seen.add(repo)
            try:
                ns, manifest = read_research_inventory(conf)
            except Exception as e:  # a broken conf is a finding, not a crash
                items.append({"name": repo.name, "path": str(repo), "error": str(e),
                              "docs": [], "git": git_facts(repo), "links": []})
                continue
            docs, links = [], []
            for cfg in ns.get("DOCS", []):
                src = repo / cfg["src"]
                outputs = [repo / folder / (cfg["out"] + ".html") for folder in RESEARCH_OUTPUT]
                built = next((path for path in outputs if path.exists()), outputs[0])
                if not src.resolve().is_relative_to(repo.resolve()) or not built.resolve().is_relative_to(repo.resolve()):
                    raise ValueError("research source or output is outside its repository")
                if src.exists() and src.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("research source exceeds the bounded read size")
                state = "not built"
                if built.exists():
                    state = "stale" if (src.exists() and src.stat().st_mtime > built.stat().st_mtime) \
                        else "fresh"
                words = len(src.read_text(errors="replace").split()) if src.exists() else 0
                docs.append({
                    "title": cfg.get("title", cfg["out"]), "out": cfg["out"], "src": cfg["src"],
                    "eyebrow": cfg.get("eyebrow", ""), "stand": cfg.get("stand", ""),
                    "state": state, "words": words,
                    "updated": datetime.date.fromtimestamp(src.stat().st_mtime).isoformat()
                    if src.exists() else "",
                })
                links.extend(cfg.get("links", []))
            readme = repo / "README.md"
            notion = []
            if readme.exists():
                text = readme.read_text(errors="replace")
                for label, href in re.findall(r"\[([^\]]+)\]\((https://(?:www\.)?notion\.[^)]+)\)", text):
                    if (label, href) not in notion:
                        notion.append((label, href))
            items.append({
                "name": repo.name, "path": str(repo),
                "project": ns.get("PROJECT", repo.name), "docs": docs,
                "links": [list(line) for line in dict.fromkeys(tuple(line) for line in links)],
                "notion": [list(n) for n in notion[:6]],
                "git": git_facts(repo), "error": "", "manifest": manifest,
            })
    items.sort(key=lambda i: (i["git"] or {}).get("last_iso", ""), reverse=True)
    return items


def collect_areas():
    """Vault areas, with the inbox pressure that says whether an area is moving."""
    blocked = vault_blocked()
    if blocked:
        return {"error": blocked, "areas": [], "inbox": 0, "stale_inbox": [],
                "followups": {}, "vault": str(VAULT)}

    areas = []
    for name in vault_areas():
        d = VAULT / name
        if not d.is_dir():
            continue
        notes = [p for p in d.rglob("*.md")]
        latest = max(notes, key=lambda p: p.stat().st_mtime, default=None)
        key = name.replace(" Home", "")
        areas.append({
            "name": name, "key": key, "notes": len(notes),
            "subfolders": sorted(p.name for p in d.glob("*") if p.is_dir() and p.name != "space"),
            "latest": latest.stem if latest else "",
            "latest_when": datetime.date.fromtimestamp(latest.stat().st_mtime).isoformat()
            if latest else "",
            "obsidian": obsidian_url(f"{name}/{name}"),
        })

    inbox = list((VAULT / "Inbox").glob("*.md")) if (VAULT / "Inbox").is_dir() else []
    stale_inbox = [{"name": p.stem, "obsidian": obsidian_url(f"Inbox/{p.stem}")}
                   for p in inbox
                   if (time.time() - p.stat().st_mtime) > 7 * 86400]
    followups = {}
    fdir = VAULT / "Followups"
    if fdir.is_dir():
        for b in sorted(fdir.glob("*.base")):
            label = b.stem.replace("Followup - ", "")
            followups[label] = len(list((fdir / label).glob("*.md"))) if (fdir / label).is_dir() else 0
    return {
        "areas": areas,
        "inbox": len(inbox), "stale_inbox": sorted(stale_inbox, key=lambda n: n["name"]),
        "followups": followups, "vault": str(VAULT),
    }


# Two nightly runs may be missed before a drift number stops meaning anything.
DRIFT_STALE_H = 48


def last_run(log, stamp):
    """The final run block, found by reading backwards from the end.

    Nothing rotates the logs in `local-cron-jobs` -- repo-sync's is already
    830KB -- so this file grows by a run a night forever. Reading all of it to
    use the last few hundred lines would give back, slowly, the cost this
    whole change removes. Doubling windows from the end touches one 64KB block
    in the ordinary case and still finds a run block of any size.
    """
    size = log.stat().st_size
    read, step = 0, 65536
    with log.open("rb") as handle:
        while read < size:
            read = min(size, read + step)
            handle.seek(size - read)
            lines = handle.read(read).decode("utf-8", "replace").split("\n")
            if read < size:
                # The window opened mid-line; that fragment is not a record.
                # It is dropped unconditionally, which does discard a whole
                # line when the seek lands exactly on a boundary -- and that
                # is still safe, because the two things it would take to
                # return the wrong run cannot both hold. Losing the newest
                # `starting` needs the window to open on it, and a window
                # opening there contains nothing older, so `begins` comes back
                # empty and the next doubling reads the line intact. The cost
                # of the alignment is one extra read, not a stale answer.
                lines = lines[1:]
            begins = [i for i, line in enumerate(lines)
                      if (got := stamp.match(line)) and got.group(2) == "starting"]
            if begins:
                return lines[begins[-1]:]
            step *= 2
    return None


def read_drift(log):
    """The last `machine-setup-update-nightly` run: its drift numbers, and how old they are.

    Drift used to come from a fresh `machine-setup.sh status` here. That call
    costs ~3.6s of the tile's 5s budget on this machine -- most of the budget,
    for a number the nightly job already computed and wrote to this log
    verbatim: its last step is `status --fail-on-drift`. Reading it costs a
    millisecond.

    What the swap gives up is freshness, so freshness is reported rather than
    implied: `drift_at` is the run that produced the numbers and `drift_age_h`
    is its age. `drift_unknown` says why the current drift is not known --
    which covers both having no number and having one that cannot be trusted,
    because a number measured three days ago describes a machine that no
    longer exists as much as a missing one does. A missing log and a clean
    machine both rendered as an em-dash before, and those are not the same
    fact either. `drift_logged` is false only when no log exists at all,
    which is the one case that means never rather than merely unknown.

    The job appends, so only the final run block counts, and within it only
    the status part, from its last `profile :` line: the `update --apply`
    before it prints MISSING for what it then fixes. A block with no
    `drift :` line is unknown rather than zero, and which kind of unknown
    depends on its last stamp: a run still going has not reported yet, while
    one that already logged `done` or `FAILED` never will.
    """
    blank = {"drift": None, "drift_at": None, "drift_age_h": None, "drift_items": [],
             "drift_logged": True}
    if not log.exists():
        return {**blank, "drift_logged": False,
                "drift_unknown": f"{log.name} does not exist: the nightly update job has never run here"}
    stamp = re.compile(rf"^\[{re.escape(log.stem)}\] (\S+) (starting|done|FAILED)")
    block = last_run(log, stamp)
    if block is None:
        return {**blank, "drift_unknown": f"{log.name} holds no complete run"}
    when = phase = None
    for line in block:
        got = stamp.match(line)
        if got:
            when, phase = got.group(1), got.group(2)
    try:
        at = datetime.datetime.strptime(when, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        at = None
    hours = (datetime.datetime.now(datetime.timezone.utc) - at).total_seconds() / 3600 \
        if at else None
    age = round(hours, 1) if hours is not None else None
    starts = [i for i, line in enumerate(block) if re.match(r"^profile\s*:", line)]
    block = block[starts[-1]:] if starts else block
    count = re.search(r"^drift\s*:\s*(\d+)", "\n".join(block), re.M)
    if not count:
        return {**blank, "drift_at": when, "drift_age_h": age, "drift_logged": True,
                "drift_unknown": (f"the run that began {when} has not reported drift yet"
                                  if phase == "starting" else
                                  f"the {when or 'last'} run ended before it reported drift")}
    items = [line.strip() for line in block
             if re.match(r"^\s+(missing|extra|differs|DIFFERS|MISSING|EXTRA|STALE|BUSY|CLASH)", line)]
    return {
        "drift": int(count.group(1)),
        "drift_at": when,
        "drift_age_h": age,
        "drift_items": items[:25],
        "drift_logged": True,
        # Compared unrounded: `age` exists to be printed, and a run 48.04h old
        # rounds to 48.0 and would sit just inside a threshold it is past.
        # Of these two, a count whose own timestamp will not parse is the
        # worse, so it is stated first -- it would otherwise show a number
        # with "drift checked: never" beside it and raise nothing.
        "drift_unknown": (
            f"the {when} run reported {count.group(1)}, but its timestamp does not parse"
            if hours is None else
            f"measured {round(hours)}h ago; the nightly job has not run since"
            if hours > DRIFT_STALE_H else ""
        ),
    }


def collect_toolbox():
    """The local-* fleet: cron health from launchd's own last-exit record,
    docker state, and machine-setup's drift count."""
    logs_dir = SYSTEM / "local-cron-jobs" / "logs"

    loaded = {}
    for line in run(["launchctl", "list"]).split("\n"):
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith(LABEL_PREFIX + "."):
            loaded[parts[2]] = (parts[0], parts[1])

    jobs = []
    for jf in cron_job_files():
        text = jf.read_text(errors="replace")
        sched = (re.search(r'JOB_SCHEDULE="([^"]*)"', text) or [None, ""])[1]
        desc = " ".join(line.lstrip("# ").strip() for line in text.split("\n")
                        if line.startswith("#"))[:220]
        name = jf.stem
        label = f"{LABEL_PREFIX}.cron.{name}"
        pid, rc = loaded.get(label, (None, None))
        log = logs_dir / f"{name}.log"
        jobs.append({
            "name": name, "schedule": sched, "desc": desc,
            "installed": label in loaded,
            "last_exit": int(rc) if rc not in (None, "") else None,
            "running": pid not in (None, "-", ""),
            "last_run": datetime.datetime.fromtimestamp(log.stat().st_mtime)
            .strftime("%Y-%m-%d %H:%M") if log.exists() else "",
            "log_age_h": round((time.time() - log.stat().st_mtime) / 3600, 1)
            if log.exists() else None,
        })
        nxt = cron_next(sched)
        jobs[-1]["next_run"] = nxt.strftime("%Y-%m-%d %H:%M") if nxt else ""
        jobs[-1]["next_in_h"] = round((nxt - datetime.datetime.now()).total_seconds() / 3600, 1) \
            if nxt else None

    fails = []
    flog = logs_dir / "failures.log"
    if flog.exists():
        fails = [line for line in flog.read_text(errors="replace").strip().split("\n") if line][-10:]

    agents = []
    for label, (pid, rc) in sorted(loaded.items()):
        if label.startswith(f"{LABEL_PREFIX}.cron."):
            continue
        agents.append({"label": label, "pid": pid, "rc": int(rc) if rc != "" else None,
                       "running": pid not in ("-", "")})

    containers = []
    for line in run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
                    timeout=10).split("\n"):
        if "\t" in line:
            n, s, p = (line.split("\t") + ["", "", ""])[:3]
            containers.append({"name": n, "status": s, "ports": p})

    tools = []
    for d in sorted(SYSTEM.glob("*/")):
        if d.name.startswith(".") or not (d / "README.md").exists():
            continue
        entry = d / (re.sub(r"^local-", "", d.name) + ".sh")
        tools.append({"name": d.name, "entry": entry.name if entry.exists() else "",
                      "readme_lines": len(( d / "README.md").read_text(errors="replace").split("\n"))})

    return {
        "jobs": jobs, "failures": fails, "agents": agents, "containers": containers,
        "tools": tools,
        **read_drift(logs_dir / "machine-setup-update-nightly.log"),
    }


# ----------------------------------------------------------------- cron time


def _cron_field(expr, lo, hi):
    """One crontab field to the set of values it matches."""
    out = set()
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, st = part.split("/", 1)
            step = int(st)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part.lstrip("-"):
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        out |= set(range(a, b + 1, step))
    return {v for v in out if lo <= v <= hi}


def cron_next(expr, now=None):
    """Next fire time for a 5-field crontab expression, or None.

    Walks day by day rather than minute by minute: every job here fires at a
    handful of times a day, so scanning 527k minutes to find the next one
    would cost more than the whole collection pass.
    """
    now = now or datetime.datetime.now()
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    try:
        mins = _cron_field(parts[0], 0, 59)
        hours = _cron_field(parts[1], 0, 23)
        doms = _cron_field(parts[2], 1, 31)
        mons = _cron_field(parts[3], 1, 12)
        dows = {d % 7 for d in _cron_field(parts[4], 0, 7)}
    except (ValueError, TypeError):
        return None
    dom_any = parts[2].strip() == "*"
    dow_any = parts[4].strip() == "*"
    day = now.date()
    for offset in range(0, 400):
        d = day + datetime.timedelta(days=offset)
        if d.month not in mons:
            continue
        # cron's one oddity: with both day-of-month and day-of-week restricted,
        # a match on either one fires, not both.
        hit_dom, hit_dow = d.day in doms, (d.weekday() + 1) % 7 in dows
        if dom_any and dow_any:
            pass
        elif dom_any:
            if not hit_dow:
                continue
        elif dow_any:
            if not hit_dom:
                continue
        elif not (hit_dom or hit_dow):
            continue
        for h in sorted(hours):
            for m in sorted(mins):
                t = datetime.datetime.combine(d, datetime.time(h, m))
                if t > now:
                    return t
    return None


MACHINE_SETUP = SYSTEM / "local-machine-setup" / "machine-setup.sh"


# 5s, the retired contract's figure. Measured on 2026-09-13 after sd:756, three runs
# each at load average 9-12.5: `candidates service` alone 2.58-2.94s, this
# whole collection in process 1.70-1.85s, `dashboard.sh tile ports` 1.72-1.96s.
# The page reads within these 5s. The tile reads within 4s (`TILE_MARGIN` in
# `sd_tile.py`), about one second above the slowest of those runs. At load
# 13-18 review-330 measured 3.2-3.8s for both, under a second from the tile's
# ceiling; past it the tile refuses with the reason, it does not guess.
@budgeted(seconds=5.0)
def collect_ports(*, within=None):
    """The port map, read out of the scripts by machine-setup rather than
    recited from prose — a prose port list goes stale, so this asks
    the machine.

    **The budget is this function's, not its caller's.** It declares 5 seconds
    (see the comment above) and inherits the shared 64KB; every command it
    reads spends one `Budget`, and running past it raises `OverBudget` rather
    than returning late or returning a partial map as if it were whole. There is no `timeout` argument
    any more, on purpose: a caller that could pass one could loosen the budget,
    which is how the in-process Ports area came to wait twelve seconds with no
    size ceiling at all (sd:722). `within` only tightens it.

    **`CLASH` and `BUSY` arrive on their own lines, not on the service rows.**
    `machine-setup.sh` prints the table, then a `---` summary, then one line
    per conflict naming the port and the services that want it. The service-row
    regex requires a `local-` name in the second field, so those lines
    never matched it and the flags were read off the row that could not carry
    them: `clash` and `busy` were structurally always 0, hiding exactly the
    thing the Ports tab exists to show. Found in review at 6b-9, and not
    reproducible on this machine at the time -- there were no conflicts to
    find, which is the condition under which a broken conflict detector looks
    correct.
    """
    budget = Budget(collect_ports.budget_seconds, within=within)
    try:
        txt = budget.run([str(MACHINE_SETUP), "candidates", "service"]).stdout.strip()
    except OSError:
        # `run` read a missing script as no output, and so does this: an empty
        # inventory is already reported as incomplete, never as no ports.
        txt = ""
    snapshot = parse_ports(txt)
    snapshot["observed"] = collect_tcp_listeners(runner=budget.run)
    return snapshot


def parse_tcp_listeners(txt):
    """Read lsof's selected fields, preserving every visible PID and address."""
    ports, pid, command, complete = {}, None, None, True
    for line in txt.splitlines():
        if not line:
            continue
        kind, value = line[0], line[1:]
        if kind == "p":
            pid, command = (int(value), None) if value.isdecimal() and int(value) > 0 else (None, None)
            complete = complete and pid is not None
        elif kind == "c":
            command = value[:160] or None
        elif kind == "f" and re.fullmatch(r"[0-9]+[A-Za-z]*", value):
            continue
        elif kind == "n":
            address, separator, port = value.rpartition(":")
            if not pid or not separator or not address or not port.isdecimal() or not 1 <= int(port) <= 65535:
                complete = False
                continue
            try:
                if address != "*":
                    ipaddress.ip_address(address.strip("[]"))
            except ValueError:
                complete = False
                continue
            owner = {"pid": pid, "command": command, "address": value[:240]}
            group = ports.setdefault(str(int(port)), [])
            if owner not in group:
                group.append(owner)
        else:
            complete = False
    for group in ports.values():
        group.sort(key=lambda owner: (owner["pid"], owner["address"], owner["command"] or ""))
    return {"ports": ports, "complete": complete}


def collect_tcp_listeners(*, runner=None):
    """Two local socket-table reads, `lsof` then `netstat`, not a network scan.

    Neither command line takes an argument from a caller.

    Part of the Ports collection, so a call without a runner reads inside a
    budget of Ports' declaration; `collect_ports` hands it the one it is
    spending.

    The same two rules the machine-setup port probe follows (sd:756).
    `-w` silences lsof's warnings, such as this machine's "can't stat() apfs
    file system" line for a Carbon Copy Cloner snapshot mount, which concern
    naming files and not the socket table; whatever still reaches stderr is an
    error. And lsof run as this user leaves out root's listeners without saying
    so, so netstat's LISTEN ports are read too: one that lsof did not name, or a
    netstat table that was not read, means the inventory is not complete. The
    ports lsof did name stay visible either way. The comparison is by port,
    not by socket: a root listener on another address of a port lsof names
    leaves the inventory complete with that port's holders partial (review-330
    N3). The shell function is not called from here because it keeps one
    holder per port, and this keeps them all.
    """
    run = runner or Budget(collect_ports.budget_seconds).run
    try:
        result = run(["lsof", "-w", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"],
            capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return {"ports": {}, "complete": False}
    snapshot = parse_tcp_listeners(result.stdout)
    try:
        table = run(["netstat", "-an", "-p", "tcp"], capture_output=True, text=True, timeout=3, check=False,
                    keep=netstat_kept)
        listening = parse_netstat_listeners(table.stdout) if table.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        listening = None
    # lsof uses exit 1 with no output for an empty match, and exit 0 only with
    # a listener to show (as `tcp_listeners` requires). Other failures and
    # errors leave positive observations useful but never prove completeness.
    snapshot["complete"] = snapshot["complete"] and not result.stderr and (
        (result.returncode == 0 and bool(snapshot["ports"])) or (result.returncode == 1 and not result.stdout)) and (
        listening is not None and listening.issubset(snapshot["ports"]))
    return snapshot


def netstat_kept(line):
    """The netstat lines `parse_netstat_listeners` reads: the header and LISTEN rows.

    A busy machine's TIME_WAIT and ESTABLISHED rows are most of the table, and
    held they would spend the Ports byte ceiling on data nothing reads.
    """
    fields = line.split()
    return bool(fields) and fields[-1] in (b"LISTEN", b"(state)")


def parse_netstat_listeners(txt):
    """The ports macOS `netstat -an -p tcp` lists as LISTEN, or None if unread.

    A row ends in its state and names the local address as <address>.<port>:
    "*.445", "::1.8021", "100.64.0.10.8443". No "(state)" header -- Linux's
    net-tools reads -p as "show PIDs" and prints another table -- or a LISTEN
    row this cannot read gives None, never an empty set.
    """
    ports, header = set(), False
    for line in txt.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "Proto" and fields[-1] == "(state)":
            header = True
        elif fields[-1] == "LISTEN":
            port = fields[3].rpartition(".")[2] if len(fields) > 3 else ""
            if not header or not port.isdecimal():
                return None
            ports.add(str(int(port)))
    return ports if header else None


def parse_ports(txt):
    """The existing candidates table plus its explicit per-port observations.

    Conflict names changed from local-foo to foo in the upstream output;
    associate by port, which is the actual identity shared by both formats.
    A legacy report without listener evidence remains unknown, never free.
    """
    rows, section, flagged, listeners = [], "", {}, {}
    complete, expected_rows = False, None
    # sd:5840: the running containers, from the same `docker ps` the service states come from.
    containers, docker = [], None
    for line in txt.split("\n"):
        if line.startswith("  CONTAINER\t"):
            fields = line.split("\t")[1:]
            if len(fields) == 6:
                name, image, status, project, workdir = (field[:240] for field in fields[:5])
                # sd:5964: split the mounts before the cap, so a mount past char 240 is still read.
                mounts = [mount[:240] for mount in fields[5].split(",") if mount][:40]
                containers.append({"name": name, "image": image, "status": status, "project": project,
                                   "workdir": workdir, "mounts": mounts})
            continue
        said = re.fullmatch(r"  DOCKER  (read: (\d+) running|unknown: (.{1,200}))", line)
        if said:
            docker = said
            continue
        if line.startswith("== "):
            section = line[3:].strip()
            continue
        if line.startswith("  add by hand:"):
            complete = True
        summary = re.match(r"^\s+---\s+(\d+) startable,", line)
        if summary:
            expected_rows = int(summary[1])
        observation = re.fullmatch(r"\s+(LISTEN|CLEAR|UNKNOWN)\s+port\s+(\d+)\s+(.*)", line)
        if observation:
            kind, port, detail = observation.groups()
            value = {"state": "unknown", "source": "lsof", "holder": None, "pid": None}
            if kind == "LISTEN":
                owner = re.fullmatch(r"held by (\S+) \(pid ([1-9][0-9]*)\)", detail)
                if owner:
                    value.update(state="listening", holder=owner[1], pid=int(owner[2]))
            elif kind == "CLEAR" and detail == "no TCP listener observed":
                value["state"] = "not_listening"
            if port in listeners and listeners[port] != value:
                value = {"state": "unknown", "source": "unavailable", "holder": None, "pid": None}
            listeners[port] = value
            continue
        # `  CLASH   port 8002 wanted by local-a local-b — list at most one`
        # `  BUSY    port 5434 held by postgres — local-postgres cannot bind it`
        conflict = re.match(r"^\s+(CLASH|BUSY)\s+port\s+(\d+)\s+(.*)$", line)
        if conflict:
            kind, port, rest = conflict.groups()
            flagged.setdefault(port, set()).add(kind)
            if kind == "BUSY" and port not in listeners:
                owner = re.match(r"held by (\S+) \(pid ([1-9][0-9]*)\)(?: —|$)", rest)
                if owner:
                    listeners[port] = {"state": "listening", "source": "lsof-legacy",
                                       "holder": owner[1], "pid": int(owner[2])}
            continue
        m = re.match(r"^\s+(\S)\s+(local-\S+)\s+(.*?)\s\s+(\S.*)$", line)
        if not m:
            continue
        mark, name, ports, state = m.groups()
        rows.append({
            "name": name, "mark": mark, "section": section,
            "ports": [p for p in ports.split() if p],
            "state": state.strip() if state.strip() in ("running", "stopped", "unknown") else "unknown",
            "flags": [], "conflicts": [],
        })
    for row in rows:
        for port in row["ports"]:
            for flag in sorted(flagged.get(port, ())):
                if flag not in row["flags"]:
                    row["flags"].append(flag)
                row["conflicts"].append({"flag": flag, "port": port})
            listeners.setdefault(port, {"state": "unknown", "source": "unavailable", "holder": None, "pid": None})
    if docker is None:
        reason = "machine-setup.sh candidates service printed no DOCKER line; one from before sd:5840 does not"
    elif docker[3]:
        reason = docker[3]
    elif int(docker[2]) != len(containers):
        reason = (f"docker ps listed {docker[2]} running containers and {len(containers)} "
                  f"{'line was' if len(containers) == 1 else 'lines were'} read")
    else:
        reason = ""
    return {"services": rows,
            "listeners": listeners, "complete": complete and expected_rows == len(rows),
            "containers": {"read": not reason, "reason": reason, "rows": [] if reason else containers},
            "source": "local-machine-setup/machine-setup.sh candidates service",
            # Counted over the services, not over the lines: one BUSY port that
            # three services want is three services that cannot start.
            "clash": sum(1 for r in rows if "CLASH" in r["flags"]),
            "busy": sum(1 for r in rows if "BUSY" in r["flags"])}
