"""Download from Mission (sd:5626): one click on the HOA page opens the source's portal in a
Claude Chrome session, waits there for the operator to log in, then runs the hoa checkout's own download.

    python3 -m sd_dashboard.hoa_download run mission <hoa checkout>

    environment: XDG_STATE_HOME, CLAUDE_BIN

The download steps are the checkout's, not this module's (`SOURCES`): `tools/mission/mission.sh export`,
one `claude -p --chrome` that stops with exit 3 on a login page. The portal's address is the first `https://` link in
section 2 of that run's prompt, so no address is kept here.

`start` answers `POST /api/hoa/download {"source"}` through `sweep.spawn`: the source's lock, `<state>/lock`, then this
module's `run` detached, its output in `<state>/start.log`, and 202. `status` is `/api/hoa`'s `downloads` part.
`run`, in steps, and what a stop between them leaves:

1. `<state>/last.json` as `running`, phase `opening`, with the runner's pid. From here a stopped runner reads failed;
   a session it started still holds the lock, so no second start opens another tab beside it, until that session's
   own alarm (`alarmed`) ends it.
2. The login wait: one `claude -p --chrome` with an exact allowlist (`allowed_tools`), no shell and no form tool, that
   opens the page in its own tab. On a login page it prints `STATUS: waiting for login`, and the phase becomes
   `waiting`; it reads the page again every 30 seconds, up to 15 minutes, and never types. `RESULT: signed in`, as the
   last RESULT line of a successful result event, goes on; anything else ends the run there, before any download, as
   `unavailable` or `failed`. No RESULT line from a session whose init named no Chrome tool or server is
   `unavailable`: the Chrome tools did not load (pack sd:7723).
3. The download: the checkout's command, phase `downloading`. Exit 0 is `done`, 3 `unavailable`, any other `failed`,
   each with the command's last output line. The command keeps its own lock and its own landing.
4. `last.json` as `done`, `unavailable` or `failed`, with the exit code and the reason. Each record carries
   `last_done`, the end of the last run that was done, so a failure still names the last good download.

Exit codes: 0 done, 3 not possible now (signed out, no browser, another run), 1 failed.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .realm_sweep import REAP_SECONDS, WITH_TOKEN, alive, claude_binary, write_record
from .sweep import DASHBOARD, _held, _since, spawn

#: The login run's shell: env.sh's PATH and the rest, but no CLAUDE_CODE_OAUTH_TOKEN. The setup token's
#: user:inference scope disables Claude in Chrome, so claude signs in from the keychain (pack sd:7750).
NO_TOKEN = WITH_TOKEN.replace('exec "$@"', 'unset CLAUDE_CODE_OAUTH_TOKEN; exec "$@"')

__all__ = ["LOGIN_SECONDS", "SOURCES", "allowed_tools", "portal_url", "request", "run", "start", "state_dir",
           "status"]

EXIT_OK, EXIT_NONE, EXIT_ERROR = 0, 3, 1

#: Each source's download: its label, the prompt whose section 2 names the portal, and the checkout's command.
SOURCES = {
    "mission": {"label": "Mission", "prompt": "tools/mission/export-prompt.md",
                "argv": ("sh", "tools/mission/mission.sh", "export")},
}
#: The login wait reads the page this many times, WAIT_SECONDS apart: 15 minutes.
CHECKS, WAIT_SECONDS = 30, 30
#: The login session's bound, past its own 15 minutes, and the download's.
LOGIN_SECONDS = 20 * 60
DOWNLOAD_SECONDS = 150 * 60
#: Each child's own alarm, past those bounds: it ends a child whose runner was killed (`alarmed`).
GRACE = 60
WAITING = "STATUS: waiting for login"
RESULT_SIGNED_IN = re.compile(r"^RESULT: signed in\s*$")
RESULT_UNAVAILABLE = re.compile(r"^RESULT: unavailable, (.+?)\s*$")
#: pack sd:7723: a run whose init event named no Chrome tool or server and that said no RESULT line.
CHROME_UNLOADED = ("the Chrome tools did not load in the headless claude run; reconnect Chrome with /chrome in an "
                   "interactive claude, then start the download again")
SECTION = re.compile(r"^## 2\..*?(?=^## |\Z)", re.M | re.S)
URL = re.compile(r"https://[^\s`)>\]\"']+")

CHROME_TOOLS = ("tabs_context_mcp", "tabs_create_mcp", "tabs_close_mcp", "navigate", "computer", "get_page_text",
                "read_page", "find")
DENIED = ("Bash", "Read", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Skill",
          *(f"mcp__claude-in-chrome__{n}" for n in ("form_input", "javascript_tool", "file_upload", "upload_image",
                                                    "shortcuts_execute")))
TOLD = ("This run has no person to answer it, no shell and no file tool. Never type into a page and never fill a form: "
        "the operator logs in by hand. Use computer only for its wait and screenshot actions.")
PROMPT = """You open {label} in the operator's Chrome and wait there for them to log in. Nobody answers questions.

Rules:
- Never type into the page, never press a key in it, never click a sign-in, submit or consent button. The operator
  logs in by hand, in your tab.
- Work only in your own tab. Never touch another tab.

1. Load the Chrome tools with one ToolSearch call: `select:{tools}`. If they do not load or no browser is connected,
   end with `RESULT: unavailable, <reason>`.
2. Call tabs_context_mcp, then tabs_create_mcp, and navigate your tab to {url}. If it does not load in 90 seconds,
   end with `RESULT: unavailable, {host} did not load`.
3. Read the page with get_page_text or find. If it shows a login or sign-in form, or asks for a password or a code,
   print a line that is exactly `{waiting}`. Then wait {wait} seconds with computer's wait action and read the page
   again, at most {checks} times, until it no longer shows a login form.
4. Signed in (the page shows the portal, not a login form): close your tab with tabs_close_mcp and end with
   `RESULT: signed in`. Still a login form after the last read: leave the tab open and end with
   `RESULT: unavailable, no login within 15 minutes; log in to {host} in Chrome and start the download again`.

The last line of your reply is exactly one RESULT line.
"""


def state_dir(source: str) -> Path:
    """`<XDG_STATE_HOME>/system/hoa-download/<source>`: the lock, start.log and last.json of one source's downloads."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "system" / "hoa-download" / source


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _command(source: str) -> str:
    return " ".join(SOURCES[source]["argv"])


def _gap(place: Path, source: str) -> str:
    """Why the checkout cannot run this source's download, or ''. A missing file is the procedure's gap, by name."""
    spec = SOURCES[source]
    for name in (spec["prompt"], *(a for a in spec["argv"] if a.startswith("tools/"))):
        if not (place / name).is_file():
            return f"{name} is not in the hoa checkout {place}, so the {spec['label']} download has no procedure there"
    return ""


def portal_url(place: Path, source: str) -> str:
    """The first https link in section 2 of the source's prompt; a prompt that names none raises ValueError."""
    name = SOURCES[source]["prompt"]
    try:
        text = (place / name).read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"{name} did not read: {error}") from None
    section = SECTION.search(text)
    found = URL.search(section.group(0)) if section else None
    if not found:
        raise ValueError(f"{name} names no https address in its section 2, so there is no page to open")
    return found.group(0).rstrip(".,;:")


def allowed_tools() -> list[str]:
    return [f"mcp__claude-in-chrome__{name}" for name in CHROME_TOOLS]


def claude_argv(claude: str, source: str, url: str) -> list[str]:
    """The login wait: Chrome reads and waits only; an exact allowlist, never skipped permissions."""
    host = url.split("/")[2]
    prompt = PROMPT.format(label=SOURCES[source]["label"], tools=",".join(allowed_tools()), url=url, host=host,
                           waiting=WAITING, wait=WAIT_SECONDS, checks=CHECKS)
    return [
        claude, "-p", prompt,
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--setting-sources", "project",
        "--strict-mcp-config",
        "--mcp-config", json.dumps({"mcpServers": {}}),
        "--tools", "ToolSearch",
        "--allowedTools", ",".join(allowed_tools()),
        "--disallowedTools", ",".join(DENIED),
        "--append-system-prompt", TOLD,
        "--chrome",
        "--output-format", "stream-json", "--verbose",
    ]


# ---------------------------------------------------------------------------
# The dashboard's side: start and status.

def start(connection, source: str):
    """`(202, {"started", "repo", "command"})`, `(409, {"error"})` while this source's run holds its lock, or a
    ValueError (400) for a checkout without the procedure."""
    from .hoa_screen import checkout

    _, place, problems = checkout(connection)
    if problems:
        raise ValueError(problems[0])
    if place is None:
        raise ValueError("No repo named hoa is registered, so there is nothing to download into (sd-db repo add).")
    gap = _gap(place, source)
    if gap:
        raise ValueError(f"{gap}; pull its main first.")
    portal_url(place, source)
    label = SOURCES[source]["label"]
    argv = [sys.executable, "-m", "sd_dashboard.hoa_download", "run", source, str(place)]
    code, body = spawn(state_dir(source), DASHBOARD, argv, _command(source), f"A {label} download")
    return code, body | ({"repo": str(place)} if code == 202 else {})


def request(values: dict):
    """`POST /api/hoa/download`'s checks: one field, `source`, mission."""
    source = values.get("source")
    if set(values) != {"source"} or source not in SOURCES:
        raise ValueError(f"Download takes one field, source: {' or '.join(SOURCES)}.")
    return lambda connection: start(connection, source)


def _last_done(record) -> str:
    """sd:10616: when the source's last good download finished, from its record: its `last_done`, or its own end when
    a record from before that field says done. No record, or one that is not an object, names none."""
    if not isinstance(record, dict):
        return ""
    done = record.get("last_done") or (record.get("finished") if record.get("state") == "done" else "")
    return done if isinstance(done, str) else ""


def _read_record(path: Path):
    """The record at `path`, or None when it does not read."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status(source: str, place: Path | None = None) -> dict:
    """The source's last download for the page: `running`, `state` (never, running, done, unavailable, failed),
    `phase` while it runs (opening, waiting, downloading), `reason`, times, `log`, `last_done`, when the last good
    download finished (sd:10616), and `error`, why Download is off."""
    state = state_dir(source)
    out = {"source": source, "label": SOURCES[source]["label"], "command": _command(source), "running": False,
           "state": "never", "phase": "", "reason": "", "started": "", "finished": "", "exit": None,
           "log": str(state / "start.log"), "error": _gap(place, source) if place else "",
           "last_done": ""}
    try:
        record = json.loads((state / "last.json").read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("the record is not an object")
    except FileNotFoundError:
        record = None
    except (OSError, ValueError) as error:
        record, out["error"] = None, f"{state / 'last.json'} did not read: {error}"
    held = _held(state)
    if record:
        out.update({key: record.get(key, out[key]) for key in ("state", "phase", "reason", "started", "finished", "exit")})
        out["last_done"] = _last_done(record)
    if out["state"] == "running" and not held:
        # Every live runner holds the lock, so a free one ends the run even if its pid now names another process.
        out.update(state="failed", phase="", reason=f"the download stopped without recording an outcome; see {out['log']}")
    elif out["state"] == "running" and not alive((record or {}).get("pid")):
        out["reason"] = (f"its runner stopped, and its session still holds the lock; the session ends by itself within "
                         f"{(DOWNLOAD_SECONDS + GRACE) // 60} minutes, and nothing starts until then")
    elif out["state"] != "running" and held:
        # Spawned, and the runner has not written its record yet.
        out.update(state="running", phase="opening", started=held, reason="")
    try:
        began = (state / "lock").read_text(encoding="utf-8").strip()
    except OSError:
        began = ""
    ours, theirs = _since(began), _since(out["started"])
    if out["state"] != "running" and ours and (theirs is None or ours > theirs) and not out["error"]:
        out.update(state="failed", phase="", started=began, exit=None,
                   reason=f"the download started at {began} ended without a record of its own; see {out['log']}")
    out["running"] = out["state"] == "running"
    return out


# ---------------------------------------------------------------------------
# The runner's side.

def alarmed(seconds: int) -> list[str]:
    """An argv prefix that execs the rest with SIGALRM `seconds` away. The alarm outlives exec and the runner, so a child
    left by a runner killed with SIGKILL, whose timer dies with it, still ends, and frees the lock it holds."""
    return [sys.executable, "-c", "import os, signal, sys; signal.alarm(int(sys.argv[1])); os.execvp(sys.argv[2], sys.argv[2:])",
            str(seconds)]


def _bounded(proc: subprocess.Popen, seconds: float) -> threading.Timer:
    """Kill `proc`'s process group after `seconds`; the timer's `fired` says it did."""
    def kill():
        timer.fired = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:  # EPERM: macOS for an exiting group, or a sandbox (sd:7900)
            proc.kill()

    timer = threading.Timer(seconds, kill)
    timer.fired = False
    timer.daemon = True
    timer.start()
    return timer


class Unreadable(Exception):
    """A run's output could not be read; only `_lines` raises it, so a caller's own error is not taken for it."""


def _lines(proc: subprocess.Popen, timer: threading.Timer):
    """`proc`'s output lines until EOF, or until REAP_SECONDS after `timer` fired (sd:7910).

    A grandchild that a refused group kill left alive (EPERM, sd:7900) holds stdout open, so a read to EOF could wait
    on it for as long as it lives. A daemon thread reads the pipe and closes it at EOF; past the bound it is left to
    the grandchild, since closing a pipe another thread reads blocks on that read.

    The bound is checked before each line, so a survivor that keeps printing cannot hold the read past it. A read
    that fails raises here, never leaves the caller waiting (sd:7920). Output that is not UTF-8 is no failure: both
    pipes read it with `errors="replace"` (sd:7939).
    """
    feed: queue.Queue = queue.Queue()

    def pump():
        try:
            for line in proc.stdout:
                feed.put(line)
        except Exception as error:  # forwarded: the caller says why the read stopped
            feed.put(error)
        finally:
            feed.put(None)
            with contextlib.suppress(OSError, ValueError):
                proc.stdout.close()

    threading.Thread(target=pump, daemon=True).start()
    deadline = None
    while True:
        if deadline is None and timer.fired:
            deadline = time.monotonic() + REAP_SECONDS
        if deadline is not None and time.monotonic() >= deadline:
            return
        try:
            line = feed.get(timeout=0.2 if deadline is None else max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            if deadline is not None and time.monotonic() >= deadline:
                return
            continue
        if line is None:
            return
        if isinstance(line, Exception):
            raise Unreadable(f"{type(line).__name__}: {line}") from line
        yield line


def _unread(proc: subprocess.Popen, timer: threading.Timer, what: str, error: Unreadable) -> tuple[int, str]:
    """Stop `proc`, whose output could not be read, and say why (sd:7920)."""
    timer.cancel()
    timer.function()
    proc.wait()
    return EXIT_ERROR, f"cannot read {what}'s output: {error}"


def _chrome_loaded(init: dict) -> bool | None:
    """Whether the init event names a Chrome tool or a claude-in-chrome server; None when it lists neither field.

    ToolSearch's deferred tools need not appear in `tools`, so a server counts too, and only a reply with no RESULT
    line is judged by this (pack sd:7723).
    """
    tools, servers = init.get("tools"), init.get("mcp_servers")
    if not isinstance(tools, list) and not isinstance(servers, list):
        return None
    return (any(str(name).startswith("mcp__claude-in-chrome__") for name in tools or [])
            or any(isinstance(server, dict) and "claude-in-chrome" in str(server.get("name", "")) for server in servers or []))


def login(source: str, url: str, cwd: Path, keep: tuple[int, ...], out, on_waiting) -> tuple[int, str]:
    """Step 2: the login wait, judged by its reply's last RESULT line; `on_waiting()` once it prints WAITING."""
    argv = claude_argv(claude_binary(), source, url)
    try:
        proc = subprocess.Popen([*alarmed(LOGIN_SECONDS + GRACE), "sh", "-c", NO_TOKEN, "claude-run", *argv], cwd=cwd,
                                env={**os.environ, "SD_HANDOFF_RESTORE": "0"}, text=True, errors="replace", stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=keep)
    except OSError as error:
        return EXIT_ERROR, f"cannot start {argv[0]}: {error}"
    timer, texts, waited, final, ended, chrome = _bounded(proc, LOGIN_SECONDS), [], False, None, "", None
    try:
        for line in _lines(proc, timer):
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if not isinstance(event, dict):
                print(line.rstrip(), file=out, flush=True)
                texts.append(line)
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                chrome = _chrome_loaded(event)
                continue
            if event.get("type") == "assistant":
                said = "\n".join(block.get("text", "") for block in (event.get("message") or {}).get("content") or []
                                 if isinstance(block, dict) and block.get("type") == "text")
            elif event.get("type") == "result":
                said = str(event.get("result") or "")
                ended = str(event.get("subtype") or "an unnamed result")
                final = said if event.get("subtype") == "success" and event.get("is_error") is False else None
            else:
                continue
            if said.strip():
                print(said.rstrip(), file=out, flush=True)
                texts.append(said)
            if not waited and any(row.strip() == WAITING for row in said.splitlines()):
                waited = True
                on_waiting()
    except Unreadable as error:
        return _unread(proc, timer, argv[0], error)
    proc.wait()
    timer.cancel()
    if timer.fired:
        return EXIT_NONE, f"no login within {LOGIN_SECONDS // 60} minutes; the session was stopped"
    lines = [row.strip() for text in texts for row in text.splitlines()]
    if proc.returncode != 0:
        return EXIT_ERROR, f"claude exited {proc.returncode}: {next((r for r in reversed(lines) if r), '')[:200]}"
    # A RESULT line said along the way is not the end: only the successful result event's last one counts.
    results = [row.strip() for row in (final or "").splitlines() if row.strip().startswith("RESULT: ")]
    last = results[-1] if results else ""
    if not last and chrome is False:
        return EXIT_NONE, CHROME_UNLOADED
    if final is None:
        return EXIT_ERROR, f"claude ended with {ended}" if ended else "claude ended without a result"
    if RESULT_SIGNED_IN.match(last):
        return EXIT_OK, last
    unavailable = RESULT_UNAVAILABLE.match(last)
    if unavailable:
        return EXIT_NONE, unavailable.group(1)
    return EXIT_ERROR, last or "no RESULT: line in the reply"


def download(source: str, place: Path, keep: tuple[int, ...], out) -> tuple[int, str]:
    """Step 3: the checkout's command; its exit code, and its last output line as the reason."""
    argv = [sys.executable if part == "python3" else part for part in SOURCES[source]["argv"]]
    try:
        proc = subprocess.Popen([*alarmed(DOWNLOAD_SECONDS + GRACE), *argv], cwd=place, text=True, errors="replace",
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=keep)
    except OSError as error:
        return EXIT_ERROR, f"cannot start {_command(source)}: {error}"
    timer, last = _bounded(proc, DOWNLOAD_SECONDS), ""
    try:
        for line in _lines(proc, timer):
            print(line.rstrip(), file=out, flush=True)
            last = line.strip() or last
    except Unreadable as error:
        return _unread(proc, timer, _command(source), error)
    proc.wait()
    timer.cancel()
    if timer.fired:
        return EXIT_ERROR, f"{_command(source)} passed {DOWNLOAD_SECONDS // 60} minutes and was stopped"
    if proc.returncode in (EXIT_OK, EXIT_NONE):
        return proc.returncode, last
    return EXIT_ERROR, f"{_command(source)} exited {proc.returncode}" + (f": {last}" if last else "")


def run(source: str, place: Path, out=sys.stdout, err=sys.stderr) -> int:
    """Steps 1 to 4, under the lock `spawn` handed down (`SD_RUN_LOCK_FD`), or one taken here for a run by hand."""
    state = state_dir(source)
    state.mkdir(parents=True, exist_ok=True)
    inherited = os.environ.get("SD_RUN_LOCK_FD", "")
    handle = None
    if inherited.isdigit():
        keep = (int(inherited),)
    else:
        handle = open(state / "lock", "a+", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"hoa_download: a {source} download is already running", file=err)
            handle.close()
            return EXIT_NONE
        handle.seek(0)
        handle.truncate()
        handle.write(_now())
        handle.flush()
        keep = (handle.fileno(),)
    try:
        started = (state / "lock").read_text(encoding="utf-8").strip() or _now()
        record = {"source": source, "state": "running", "phase": "opening", "started": started, "finished": "",
                  "exit": None, "reason": "", "pid": os.getpid(), "log": str(state / "start.log"),
                  "last_done": _last_done(_read_record(state / "last.json"))}

        def phase(name):
            record["phase"] = name
            write_record(state / "last.json", record)

        def finish(code, reason):
            record.update(state={EXIT_OK: "done", EXIT_NONE: "unavailable"}.get(code, "failed"), phase="", exit=code,
                          reason=reason, finished=_now())
            if code == EXIT_OK:
                record["last_done"] = record["finished"]
            write_record(state / "last.json", record)
            if code != EXIT_OK:
                print(f"hoa_download: {source}: {reason}", file=err)
            return code

        phase("opening")
        try:
            url = portal_url(place, source)
        except ValueError as error:
            return finish(EXIT_ERROR, str(error))
        code, reason = login(source, url, state, keep, out, lambda: phase("waiting"))
        if code != EXIT_OK:
            return finish(code, reason)
        phase("downloading")
        return finish(*download(source, place, keep, out))
    finally:
        if handle is not None:
            handle.close()


def main(argv: list[str]) -> int:
    if argv[:1] in (["-h"], ["--help"], ["help"]):
        print(__doc__.split("\n\n")[1])
        return 0
    if len(argv) != 3 or argv[0] != "run" or argv[1] not in SOURCES:
        print(f"usage: python3 -m sd_dashboard.hoa_download run {'|'.join(SOURCES)} <hoa checkout>", file=sys.stderr)
        return EXIT_ERROR
    return run(argv[1], Path(argv[2]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
