"""Run (sd:5886, sd:5887): a button that gave a line to copy runs that line here, and Details shows the output.

Every page posts `/api/actions/run` with `{"action": <name>, ...fields}`. `ACTIONS` is the one registry: each name maps to a
check that reads its fields against what the server holds now and answers one `Step`. The page never sends a
command: a step's argv is fixed text and values the check found -- an installed job, a configured storage folder, a
mounted volume, a port number, a Home Assistant tile, a registered repo row, a contribution row. No step runs a shell.

| action             | checked against                              | step                                          |
| ------------------ | -------------------------------------------- | --------------------------------------------- |
| job.log            | an installed job (`operations.job_state`)    | `cron-jobs.sh logs <job> 200`                 |
| job.print          | the same; its launchd service                | `launchctl print <service>`                   |
| assignment.get     | an assignment row's number                   | `sd assignments get <n>`                      |
| storage.du         | a configured storage folder or one inside it | `du -k -d 1 <folder>`, largest first          |
| volume.df          | a mount point or a configured storage folder | `df -h <path>`                                |
| port.lsof          | an integer port                              | `lsof -nP -iTCP:<port> -sTCP:LISTEN`          |
| credentials.probe  | nothing                                      | `credentials.check`, as `sd-db credentials`   |
| credentials.signin | a probe `credentials.signin` finds on disk   | its consent URL, or its login in herdr        |
| check.run          | a Health check row id this module knows      | the row's read, see `CHECKS`                  |
| sensor.read        | a tile in Home's tile list                   | `GET /api/states/<entity>`                    |
| skill.scan         | nothing                                      | `sd skill scan`                               |
| idea.capture       | `notes_screen.quick_text`'s rules            | `sd store add sdw.blog-idea <text> ...`       |
| declaration.write  | a registered repo row, the page's `before`   | `repos.set_declaration`, as `repo declare`    |
| skill.run          | a skill on disk, a registered repo row       | claude `/<skill>` in herdr (`fix_session`)    |
| skill.adopt        | an owner/repo[/path] source                  | claude `/sd-skill-adopt <source>` in herdr    |
| contribution.nudge | a contribution row and its revision          | `gh pr|issue comment <url> --body-file -`     |
| repo.pull          | `pull_refusal`, read again under the lock    | `git pull --ff-only`, under the lane's lock   |
| watch.refresh      | an active market row, the vault's routine    | claude, one market-watch entity, in herdr     |
| watch.add          | an http(s) URL, an optional name             | `sd store add sdw.market <name> ...`          |
| watch.remove       | a market row not declined                    | `records.edit_row`, stage declined            |
| watch.restore      | a declined market row                        | `records.edit_row`, stage candidate           |
| watch.delete       | a market row                                 | `sd-db item remove <id>`, preview then apply  |
| idea.restore       | a declined blog-idea row                     | `records.edit_row`, stage inbox               |
| tip.restore        | a declined tip row                           | `records.edit_row`, stage inbox               |
| topic.add          | a topic title                                | `sd store add sdw.topic <title> ...`          |
| topic.remove       | a topic row not retired                      | `records.edit_row`, stage retired             |
| topic.restore      | a retired topic row                          | `records.edit_row`, stage candidate           |
| topic.delete       | a topic row                                  | `sd-db item remove <id>`, preview then apply  |
| learning.add       | books or videos, an http(s) URL, a title     | `sd store add sdw.learning <title> ...`       |
| studio.check       | a Studio project the server finds again      | `make check` in its checkout                  |
| studio.episode     | an episode project, a verb in its list       | `tools/episode.py <verb> <folder>`            |
| studio.song        | a song project, a verb in its list           | `tools/song_pipeline.py <verb> <song>`        |
| studio.session     | a Studio project                             | claude in herdr, a note on its item           |

A step is bounded (`Step.seconds`), and its output is kept to its last `OUTPUT_CHARS` characters. The answer is
`{action, line, exit, output, cut}` for a step that ran, whatever its exit. A refusal before the step ran is a
`ValueError` or `WorkflowError` (400), a `StaleItem` (409), or `(status, {"error"})`, and ran nothing. A step past its
bound is 504 with `outcome: unknown`: a write may have landed. The command journal records each answered run.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sd_db.config import system_checkout

__all__ = ["ACTIONS", "CHECKOUT", "OUTPUT_CHARS", "SECONDS", "Step", "request", "run"]

#: The system checkout (`SYSTEM_TOOLS_ROOT`, sd:5987): `local-cron-jobs` and `local-machine-setup` run from here.
CHECKOUT = system_checkout()
#: The bound on a step with none of its own, in seconds.
SECONDS = 30
#: The most of a step's output an answer carries: the tail, where a failure says why.
OUTPUT_CHARS = 64 * 1024
#: The lines of a job's log Show log prints.
LOG_LINES = 200
#: The entries Show biggest lists.
BIGGEST = 15
#: The longest nudge, in characters: a comment, not a page.
NUDGE_MAX = 4000
#: The market-watch routine, under the vault: Refresh runs its per-entity step for one watch (sd:6264).
WATCH_ROUTINE = "System/Scheduled Tasks/market-watch/SKILL.md"
#: The longest URL Add watch takes.
URL_MAX = 2000
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")
SKILL = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
#: The bound on a login printing its device URL and code, in seconds (sd:6957).
LOGIN_SECONDS = 20
#: Google's consent URL in start_google_auth's answer, which words it as Markdown.
CONSENT = re.compile(r"https://accounts\.google\.com/[^\s)\]>\"'`]+")
DEVICE_URL = re.compile(r"https://[^\s)\]>\"'`]+")
DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")
SOURCE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")


@dataclass(frozen=True)
class Step:
    """What one action runs: `argv` in `cwd` with `stdin`, or `call()` in this process, which answers `(exit, output)`.

    `line` is the command a reader would type for the same thing; Details names it beside the output."""

    line: str
    argv: tuple[str, ...] = ()
    cwd: str | None = None
    stdin: str | None = None
    #: Answers `(exit, output)`, or `(exit, output, extra)` whose fields join the answer (a login's `url`).
    call: Callable[[], tuple] | None = None
    seconds: int = SECONDS
    env: dict | None = None
    #: How the output reads in Details: a step may sort or word it (`du`'s sizes, the largest first).
    shape: Callable[[int, str], str] | None = None


def _fields(values: dict, names: set[str]) -> dict:
    """The action's fields, exactly `names`, each a string unless the check says otherwise."""
    given = set(values) - {"action"}
    if given != names:
        raise ValueError(f"{values['action']} takes {', '.join(sorted(names)) or 'no fields'}; nothing ran.")
    return {name: values[name] for name in names}


def _text(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Name the {what}; nothing ran.")
    return value.strip()


def _row(connection, key):
    """A registered repo row, by its key or its path."""
    from sd_db import repos

    row = repos.row_for(connection, _text(key, "repository"))
    if row is None:
        raise ValueError(f"{key} is not a registered repository; nothing ran.")
    return row


def _repo(connection, key) -> Path:
    """A registered repo row's checkout on this machine, never another path."""
    from sd_db import paths

    path = paths.disk(_row(connection, key)["path"])
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory on this machine; nothing ran.")
    return path


def _command(name: str) -> str:
    from .queue_screen import command

    return command(name)


# ---------- Reads (sd:5886) ----------

def _job(connection, values, jobs):
    """The installed job a row names: `operations.job_state` refuses a name launchd does not run as a trusted job."""
    from sd_db import operations

    name = _text(values["job"], "job")
    if not NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not a job name; nothing ran.")
    return operations.job_state(connection, name, backend=jobs or operations.LaunchdBackend())


def job_log(connection, values, jobs=None) -> Step:
    state = _job(connection, _fields(values, {"job"}), jobs)
    script = CHECKOUT / "local-cron-jobs" / "cron-jobs.sh"
    return Step(line=f"local-cron-jobs/cron-jobs.sh logs {state['name']}",
                argv=("/bin/bash", str(script), "logs", state["name"], str(LOG_LINES)))


def job_print(connection, values, jobs=None) -> Step:
    state = _job(connection, _fields(values, {"job"}), jobs)
    return Step(line=f"launchctl print {state['service']}", argv=("launchctl", "print", state["service"]))


def assignment_get(connection, values, jobs=None) -> Step:
    """Show assignment (sd:6682): the record `sd assignments get` prints, for an assignment row the store holds."""
    from sd_db import reads

    n = _fields(values, {"assignment"})["assignment"]
    if type(n) is not int or n < 1:
        raise ValueError("Name the assignment by its number; nothing ran.")
    if not reads.assignment_exists(connection, n):
        raise ValueError(f"#{n} is not an assignment; nothing ran.")
    return Step(line=f"sd assignments get {n}", argv=(_command("sd"), "assignments", "get", str(n)))


def _storage_roots(connection) -> list[Path]:
    from . import health_collectors

    text, _, problem = health_collectors.storage_list(connection)
    if text is None:
        raise ValueError(f"The storage folders were not read ({problem or 'none configured'}); nothing ran.")
    roots, _ = health_collectors.parse_storage(text)
    return [Path(os.path.realpath(root)) for root in roots]


def _sizes(code: int, out: str) -> str:
    """`du -k -d 1`'s lines, largest first, in human sizes; the folder's own total line leads.

    Any other line is du's own diagnostic, such as a folder it could not read; it follows the rows unchanged (sd:5958),
    since a total that skipped a folder is not the folder's size.
    """
    rows, said = [], []
    for line in out.splitlines():
        size, _, path = line.partition("\t")
        if size.strip().isdigit() and path:
            rows.append((int(size), path))
        elif line.strip():
            said.append(line)
    if not rows:
        return out
    rows.sort(key=lambda row: -row[0])
    def human(kb):
        return next(f"{kb / 1024 ** n:.1f} {unit}" for n, unit in ((3, "TiB"), (2, "GiB"), (1, "MiB"), (0, "KiB"))
                            if kb >= 1024 ** n or n == 0)
    shown = "\n".join(f"{human(kb):>10}  {path}" for kb, path in rows[:BIGGEST + 1])
    shown += f"\n... {len(rows) - BIGGEST - 1} more" if len(rows) > BIGGEST + 1 else ""
    return "\n".join([shown, *said])


def storage_du(connection, values, jobs=None) -> Step:
    """A configured storage folder, or a folder directly in one: what Health's storage rows name, never another path."""
    path = _text(_fields(values, {"path"})["path"], "folder")
    real = Path(os.path.realpath(path))
    roots = _storage_roots(connection)
    if not (real in roots or real.parent in roots) or not real.is_dir():
        raise ValueError(f"{path} is not a storage folder Health reads; nothing ran.")
    return Step(line=f"du -sh {path}/* | sort -h | tail -5", argv=("du", "-k", "-d", "1", str(real)), seconds=60,
                shape=_sizes)


def volume_df(connection, values, jobs=None) -> Step:
    path = _text(_fields(values, {"path"})["path"], "volume")
    real = os.path.realpath(path)
    if not os.path.ismount(real) and Path(real) not in _storage_roots(connection):
        raise ValueError(f"{path} is not a mounted volume or a storage folder; nothing ran.")
    return Step(line=f"df -h {path}", argv=("df", "-h", real))


def port_lsof(connection, values, jobs=None) -> Step:
    port = _fields(values, {"port"})["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("A port is a whole number from 1 to 65535; nothing ran.")
    line = f"lsof -nP -iTCP:{port} -sTCP:LISTEN"
    # lsof exits 1 when nothing matches: that is an answer, not a failure.
    return Step(line=line, argv=tuple(line.split()),
                shape=lambda code, out: out if out.strip() else f"Nothing listens on TCP port {port}.")


def _credentials(connection, only: str | None = None) -> Callable[[], tuple[int, str]]:
    def call():
        from sd_db import credentials

        body = credentials.check(connection, env=os.environ, **({"only": only} if only else {}))
        family = only.split(":")[0] if only else None
        return 0, "\n".join(credentials.describe(probe) for probe in body["probes"]
                            if family is None or str(probe.get("id", "")).split(":")[0] == family)

    return call


def credentials_probe(connection, values, jobs=None) -> Step:
    _fields(values, set())
    return Step(line="sd-db credentials", call=_credentials(connection), seconds=60)


def _google_consent(account: str) -> Callable[[], tuple]:
    """workspace-mcp's start_google_auth for one account: its consent URL rides the answer, never the output or a log."""
    def call():
        from . import notes_screen

        url, _, _ = notes_screen.workspace()
        client = notes_screen._mail_intake().Mcp(url, timeout=30)
        try:
            client.connect()
            said = client.call("start_google_auth", {"service_name": "Google Workspace", "user_google_email": account})
        except Exception as error:  # noqa: BLE001 - whatever broke, there is no URL to show
            return 1, f"workspace-mcp at {url} gave no consent URL: {error}"
        finally:
            notes_screen.close_session(url, client)
        found = CONSENT.search(said)
        if found is None:
            return 1, f"workspace-mcp answered with no consent URL: {said[:300]}"
        return 0, f"workspace-mcp waits for {account}'s consent; nothing opened a browser.", \
            {"url": found.group(0), "account": account}

    return call


def _device_login(argv: tuple[str, ...], account: str) -> Callable[[], tuple]:
    """A login that prints a device URL and code, then waits for the code: answered with both, left running to finish."""
    def call():
        try:
            child = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, start_new_session=True)
        except OSError as error:
            return 1, f"{argv[0]} did not start: {error}; nothing ran."
        lines: list[str] = []

        def drain():
            for line in child.stdout or ():
                lines.append(line)

        threading.Thread(target=drain, daemon=True).start()
        deadline = time.monotonic() + LOGIN_SECONDS
        while time.monotonic() < deadline:
            said = "".join(lines)
            url, code = DEVICE_URL.search(said), DEVICE_CODE.search(said)
            if url and code:
                return 0, f"{argv[0]} waits for the code for {account}; it stops on its own when the code expires.", \
                    {"url": url.group(0), "code": code.group(0), "account": account}
            if child.poll() is not None:
                break
            time.sleep(0.1)
        if child.poll() is None:
            child.kill()
        return 1, "".join(lines).strip() or f"{argv[0]} printed no URL and code in {LOGIN_SECONDS} s."

    return call


def credentials_signin(connection, values, jobs=None) -> Step:
    """Sign in (sd:6722, sd:6957): a Google account's consent URL or an SSO profile's device URL and code to copy into
    the right browser profile, never opened here; any other login in a new herdr workspace, read from disk now."""
    from sd_db import credentials

    probe = _text(_fields(values, {"probe"})["probe"], "probe")
    text = credentials.signin(probe, os.environ)
    if text is None:
        raise ValueError(f"{probe} is not a credential this machine can sign in; nothing ran.")
    family, _, name = probe.partition(":")
    if family == "google":
        return Step(line=f"workspace-mcp start_google_auth user_google_email={name}", call=_google_consent(name),
                    seconds=60)
    if text.startswith("! aws sso login "):
        argv = (*shlex.split(text[2:]), "--no-browser", "--use-device-code")
        return Step(line=shlex.join(argv), call=_device_login(argv, f"AWS profile {name}"), seconds=LOGIN_SECONDS + 5)
    home = Path.home()
    label = "signin-" + re.sub(r"[^a-z0-9]+", "-", probe.lower()).strip("-")
    return Step(line=f"cd {home} && claude {shlex.quote(text)}", call=_herdr(home, label, text), seconds=100)


def _credential_check(connection, row: str) -> Step:
    """A credential row's Re-check runs its own probe family again (sd:6722); a heartbeat row runs every probe."""
    from sd_db import credentials

    probe = row.removeprefix("cred:")
    only = probe if probe.split(":")[0] in credentials.FAMILIES else None
    return Step(line="sd-db credentials", call=_credentials(connection, only), seconds=60)


#: Health check rows whose read this module runs, by the id's prefix; every other check's Re-check reads Health again.
CHECKS = {
    "doc:": lambda connection, row: Step(line="local-machine-setup/machine-setup.sh doctor", seconds=120,
                                         argv=("/bin/sh", str(CHECKOUT / "local-machine-setup" / "machine-setup.sh"), "doctor")),
    "cred:": _credential_check,
}


def check_run(connection, values, jobs=None) -> Step:
    row = _text(_fields(values, {"row"})["row"], "check")
    found = next((make for prefix, make in CHECKS.items() if row.startswith(prefix)), None)
    if found is None:
        raise ValueError(f"{row} has no read the dashboard runs; Re-check reads Health again.")
    return found(connection, row)


def sensor_read(connection, values, jobs=None):
    """One tile's state, as Home reads it after a service call: the tile must be in the tile list."""
    from . import home_screen

    entity = _text(_fields(values, {"entity_id"})["entity_id"], "tile")
    if not home_screen.ENTITY.fullmatch(entity):
        raise ValueError(f"{entity!r} is not an entity id (domain.object_id); nothing ran.")
    try:
        _, text = home_screen._current(connection, home_screen.CONFIG)
    except home_screen._Unread as unread:
        return 503, {"error": f"The tile list was not read: {unread}; nothing ran."}
    headline, groups, _ = home_screen.parse(text or "")
    if entity not in {tile["id"] for tile in ([headline] if headline else []) + [t for g in groups for t in g["tiles"]]}:
        raise ValueError(f"{entity} is not a tile in the tile list; nothing ran.")

    def call():
        try:
            base, token = home_screen._settings(os.environ)
        except home_screen._Failed as failed:
            return 1, failed.reason
        state, unread = home_screen._reread(base, token, entity, home_screen.TIMEOUT)
        return (1, unread) if state is None else (0, json.dumps({"entity_id": entity, **state}, indent=2))

    return Step(line=f'curl -H "Authorization: Bearer $HA_TOKEN" "$HA_URL/api/states/{entity}"', call=call)


def skill_scan(connection, values, jobs=None) -> Step:
    _fields(values, set())
    return Step(line="sd skill scan", argv=(_command("sd"), "skill", "scan"), seconds=120)


# ---------- Local writes (sd:5887) ----------

def idea_capture(connection, values, jobs=None) -> Step:
    """A blog idea in the vault's inbox, through `sd store add`, as Notes keeps a quick note."""
    import shlex

    from .notes_screen import quick_text

    text = quick_text(_fields(values, {"text"})["text"])
    if text.startswith("-"):
        # The idea is the note's title, a positional argument: a leading dash would read as an option.
        raise ValueError("An idea starts with a letter or digit, not a dash; nothing ran.")
    argv = (_command("sd"), "store", "add", "sdw.blog-idea", text, "--field", "status=inbox")
    return Step(line=f"sd store add sdw.blog-idea {shlex.quote(text)} --field status=inbox", argv=argv)


def declaration_write(connection, values, jobs=None):
    """`sd-db repo declare <path> review <json>`, on the database this server serves.

    `before` is the declaration text the page showed (`""` for none); a row that changed since refuses (409). The
    pack reader's own validator (`sd_lib.declaration_problems`) judges the value, as `bin/sd-db` asks before it writes.
    `value` null removes the kind, as `repo declare ... off` does: that is the Undo of a first declaration."""
    import shlex

    from sd_db import paths, repos, workflow

    from . import pack_module

    given = _fields(values, {"path", "kind", "value", "before"})
    if given["kind"] != "review":
        raise ValueError("Management writes the review declaration only; nothing ran.")
    value, before = given["value"], given["before"]
    if value is not None and not isinstance(value, dict) or not isinstance(before, str):
        raise ValueError("Send the declaration as a JSON object, or null to remove it, and the text the page showed.")
    path = _row(connection, given["path"])["path"]
    shown = repos.declarations(connection, path).get("review")
    if (json.loads(before) if before.strip() else None) != shown:
        raise workflow.StaleItem("The review declaration changed since the page read it; nothing ran. Read it again.")
    if value is not None:
        try:
            sd_lib = pack_module("sd_lib")
        except ImportError as error:
            return 503, {"error": f"The pack's declaration check was not found ({error}); nothing ran. Run the line by hand."}
        problems = sd_lib.declaration_problems("review", {k: v for k, v in value.items() if k != "$schema"})
        if problems:
            raise ValueError("; ".join(problems))
    word = "off" if value is None else json.dumps(value, separators=(",", ":"))

    def call():
        # set_declaration reads the row again in its own transaction; the check above is the page's reading.
        target, was = repos.set_declaration(connection, paths.disk(path), "review", value)
        return 0, f"{target}: review {'none' if was is None else 'declared'} -> {'none' if value is None else 'declared'}"

    return Step(line=f"sd-db repo declare {shlex.quote(given['path'])} review {shlex.quote(word)}", call=call)


def _skill_on_disk(name: str) -> bool:
    from . import pack_library

    pack = pack_library().parent
    return any((folder / name / "SKILL.md").is_file()
               for folder in (pack / "skills", pack / "contrib", Path.home() / ".claude" / "skills"))


def _herdr(directory: Path, label: str, text: str) -> Callable[[], tuple[int, str]]:
    def call():
        from . import fix_session

        pane = fix_session._launch(directory, label, text)
        if isinstance(pane, tuple):
            status, said = pane
            # _launch words its refusal for Fix; the action's own label leads in Details.
            return (None if status == 504 else 1), said["error"].removeprefix("Fix failed: ")
        return 0, f"Started claude in herdr workspace {label}, pane {pane}, in {directory}."

    return call


def skill_run(connection, values, jobs=None) -> Step:
    """Run: claude in a new herdr workspace in the chosen registered checkout, given `/<skill>` as typed text."""
    given = _fields(values, {"skill", "repo"})
    name = _text(given["skill"], "skill")
    if not SKILL.fullmatch(name) or not _skill_on_disk(name):
        raise ValueError(f"{name!r} is not an installed skill; nothing ran.")
    directory = _repo(connection, given["repo"])
    return Step(line=f"cd {directory} && claude '/{name}'", call=_herdr(directory, f"skill-{name}", f"/{name}"),
                seconds=100)


def skill_adopt(connection, values, jobs=None) -> Step:
    """Adopt: claude runs `/sd-skill-adopt <owner/repo[/path]>` in a new herdr workspace in the pack checkout."""
    from . import pack_library

    source = _text(_fields(values, {"source"})["source"], "source")
    source = re.sub(r"^https://github\.com/", "", source)
    source = re.sub(r"/(tree|blob)/[^/]+", "", source).rstrip("/")
    if not SOURCE.fullmatch(source):
        raise ValueError("Adopt takes owner/repo or owner/repo/path, or its github.com URL; nothing ran.")
    pack = pack_library().parent
    return Step(line=f"cd {pack} && claude '/sd-skill-adopt {source}'", call=_herdr(pack, "skill-adopt", f"/sd-skill-adopt {source}"),
                seconds=100)


def contribution_nudge(connection, values, jobs=None) -> Step:
    """Post nudge: the operator's comment on the contribution's own pull request or issue, at the revision the page read.

    The URL comes from the row's key, never from the page; the body goes on stdin, never into the argv."""
    from sd_db import contributions

    from .issue_filing import GH

    given = _fields(values, {"key", "revision", "body"})
    key, body = _text(given["key"], "contribution"), given["body"]
    if not isinstance(body, str) or not body.strip():
        raise ValueError("The nudge is empty; nothing was posted.")
    if len(body) > NUDGE_MAX:
        raise ValueError(f"A nudge holds at most {NUDGE_MAX} characters; this one has {len(body)}. Nothing was posted.")
    canonical = contributions._key(key)
    url = contributions._observed_url(canonical)
    if url is None:
        raise ValueError("Only a contribution filed on GitHub takes a nudge; nothing was posted.")
    state = contributions._checked(connection, canonical, given["revision"])  # StaleItem (409) when it changed
    if state["observation"] is None:
        raise ValueError(f"{url} has not been observed; nothing was posted.")
    noun = "issue" if canonical.startswith("issue:") else "pr"
    return Step(line=f"gh {noun} comment {url} --body-file -", argv=("sh", "-c", GH, "sh", noun, "comment", url, "--body-file", "-"),
                stdin=body.strip() + "\n", seconds=60)


# ---------- Pull (sd:5888) ----------

def _git(path: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(path), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _pinned() -> list[Path]:
    """The hub's pinned checkouts: this repository's and the pack's, which only the lane or repo-sync move (sd:3097)."""
    from . import pack_library

    found = [CHECKOUT, pack_library().parent]
    for checkout in list(found):
        common = _git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common is not None and common.returncode == 0:
            found.append(Path(common.stdout.strip()).parent)
    return [Path(os.path.realpath(path)) for path in found]


def pull_refusal(path: Path) -> str:
    """Why Pull does not run in `path`, or "": a check that gave no answer refuses, since its answer is not known."""
    if Path(os.path.realpath(path)) in _pinned():
        return f"{path} is a pinned checkout: only the lane or local-repo-sync/repo-sync.sh refresh moves it"
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    own = _git(path, "rev-parse", "--path-format=absolute", "--git-dir")
    if common is None or own is None or common.returncode or own.returncode:
        return "git did not say whether this is the main checkout, so it is not known"
    if common.stdout.strip() != own.stdout.strip():
        return f"{path} is a linked worktree: a builder or a session holds it"
    branch = _git(path, "symbolic-ref", "-q", "--short", "HEAD")
    if branch is None:
        return "git did not say which branch is checked out"
    if branch.returncode:
        return "HEAD is detached: Pull moves only the default branch"
    default = _git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if default is None or default.returncode or not default.stdout.strip().startswith("origin/"):
        return "origin/HEAD is not set, so the default branch is not known"
    name = default.stdout.strip().removeprefix("origin/")
    if branch.stdout.strip() != name:
        return f"it is on {branch.stdout.strip()}, not the default branch {name}"
    status = _git(path, "status", "--porcelain")
    if status is None or status.returncode:
        return "git status gave no answer, so whether the tree is clean is not known"
    if status.stdout.strip():
        count = len(status.stdout.splitlines())
        return f"{count} uncommitted or untracked {'file' if count == 1 else 'files'}"
    return ""


def _lane_lock(path: Path):
    """The lane's runner lock for this checkout, held now; None when this machine has no lane folder for it.

    `BlockingIOError` when a lane holds it. The lock is the one `sd_lane.runner_lock` takes, never waited for."""
    import fcntl

    from . import pack_module

    lane = pack_module("sd_lane")
    lock = lane.lane_dir(path, dict(os.environ)) / "queue" / "runner.lock"
    if not lock.exists():
        return None
    handle = open(lock, "a", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


def repo_pull(connection, values, jobs=None) -> Step:
    """`git pull --ff-only` in a registered checkout on its default branch, clean, and held by no lane or worktree.

    Every check runs here, just before the pull, whatever the page read; a refusal is a ValueError (400), and nothing
    ran. The lane's runner lock is held across the checks and the pull, so a lane that starts meanwhile finds it held
    and tries again at its next tick."""
    path = _repo(connection, _fields(values, {"path"})["path"])

    def call():
        try:
            held = _lane_lock(path)
        except BlockingIOError:
            raise ValueError(f"No pull: the lane runs in {path} now, and it fast-forwards the checkout after it lands.") from None
        except (ImportError, OSError) as error:
            raise ValueError(f"No pull: whether a lane holds {path} is not known ({error}).") from None
        try:
            why = pull_refusal(path)
            if why:
                raise ValueError(f"No pull: {why}.")
            done = subprocess.run(["git", "-C", str(path), "pull", "--ff-only"], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=110, check=False,
                                  env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            return done.returncode, (done.stdout + done.stderr).strip()
        except subprocess.TimeoutExpired:
            return None, f"git pull gave no answer in 110 s, so whether {path} moved is not known. Read Repos again."
        finally:
            if held is not None:
                held.close()

    return Step(line=f"git -C {path} pull --ff-only", call=call, seconds=120)


# ---------- Watches (sd:6264) ----------

def watch_refresh(connection, values, jobs=None) -> Step:
    """Refresh: claude in a new herdr workspace in the vault, told to run the market-watch routine's per-entity step for
    one active market row only. The routine refreshes three a run from its own queue; this is the one-entity run it
    lacks. The row's title is read from the store here, never taken from the page."""
    from sd_db import records

    from .notes_screen import _vault

    item = _fields(values, {"item"})["item"]
    if type(item) is not int:
        raise ValueError("Name the watch by its item number; nothing ran.")
    row = next((r for r in records.list_rows(connection, "market") if r["id"] == item), None)
    if row is None:
        raise ValueError(f"#{item} is not a market row; nothing ran.")
    if row.get("stage") != "active":
        raise ValueError(f"{row['title']} is {row.get('stage') or 'not active'}: the routine refreshes active watches only; nothing ran.")
    vault = _vault(os.environ)
    if not (vault / WATCH_ROUTINE).is_file():
        raise ValueError(f"The market-watch routine is not at {vault / WATCH_ROUTINE}; nothing ran.")
    title = row["title"]
    text = (f"You are a one-entity run of the 'market-watch' routine, started from the dashboard's Watches page. Read "
            f"'{WATCH_ROUTINE}' and carry out its per-entity refresh, with its lock, for the market row "
            f"{json.dumps(title)} only: skip the newsletter sweep, the queue and the Sunday suggestions.")
    return Step(line=f"cd {vault} && claude {json.dumps(text)}", call=_herdr(vault, f"watch-{item}", text), seconds=100)


def _watch_title(url: str, name: str) -> str:
    """The row's title: the name typed, else a repository's name or the site's host; the characters a vault file name
    cannot hold go, as the routine strips them."""
    parts = urllib.parse.urlsplit(url)
    path = [p for p in parts.path.split("/") if p]
    title = name or (path[1] if parts.hostname in ("github.com", "gitlab.com") and len(path) >= 2
                     else (parts.hostname or "").removeprefix("www."))
    return re.sub(r"\s+", " ", re.sub(r"[/\\:()~#\[\]|\"]", " ", title)).strip()


def watch_add(connection, values, jobs=None) -> Step:
    """Add watch: a candidate market row for a URL, through `sd store add`, as Capture idea files a blog idea. The URL is
    its first source; the operator activates it, after which the routine writes its profile."""
    import shlex
    from datetime import date

    given = _fields(values, {"url", "name"})
    url = _text(given["url"], "URL")
    name = given["name"].strip() if isinstance(given["name"], str) else ""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > URL_MAX or re.search(r"\s", url):
        raise ValueError(f"A watch is an http or https URL of at most {URL_MAX} characters, with no spaces; nothing ran.")
    title = _watch_title(url, name)
    if not title or title.startswith("-") or len(title) > 120:
        raise ValueError("Name the watch: a name starts with a letter or digit and has at most 120 characters; nothing ran.")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        raise ValueError("Name the watch with at least one letter or digit; nothing ran.")
    kind = "repository" if parts.hostname in ("github.com", "gitlab.com") else "company"
    today = date.today().isoformat()
    args = ["sdw.market", title, "--field", f"content-type={kind}", "--field", f"slug={slug}",
            "--field", f"dateCreated={today}", "--field", "tags+=market-watch",
            "--section", f"Sources=- {url}", "--section", f"Provenance=Added on the dashboard's Watches page, {today}."]
    return Step(line="sd store add " + " ".join(shlex.quote(a) for a in args), argv=(_command("sd"), "store", "add", *args))


def topic_add(connection, values, jobs=None) -> Step:
    """Add topic: a candidate topic row through `sd store add`, as Add watch files a market row (sd:6703). The brief
    routines read active topics only, so it waits for the operator to activate it in Tasks."""
    from datetime import date

    title = _text(_fields(values, {"title"})["title"], "topic")
    title = re.sub(r"\s+", " ", re.sub(r"[/\\:()~#\[\]|\"]", " ", title)).strip()
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug or title.startswith("-") or len(title) > 120:
        raise ValueError("Name the topic: a title starts with a letter or digit and has at most 120 characters; nothing ran.")
    today = date.today().isoformat()
    args = ["sdw.topic", title, "--field", f"slug={slug}", "--field", f"dateCreated={today}",
            "--section", f"Provenance=Added on the dashboard's Briefs page, {today}."]
    return Step(line="sd store add " + " ".join(shlex.quote(a) for a in args), argv=(_command("sd"), "store", "add", *args))


#: Add book and Add video (pack sd:7081): each view's content type for a new row, and the hosts a video is youtube on.
LEARNING_KINDS = {"books": "book", "videos": "video"}
YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")


def learning_add(connection, values, jobs=None) -> Step:
    """Add book or Add video: a to-read or to-watch learning row for a URL, through `sd store add`, as Add watch files a
    market row. The title is the one typed, else the page's `<title>` through the source intake's bounded fetch; a URL
    a learning row already holds is refused by that row's number."""
    import shlex
    import time
    from datetime import date

    from sd_db import records

    from . import source_intake

    given = _fields(values, {"view", "url", "title"})
    if given["view"] not in LEARNING_KINDS:
        raise ValueError("Add to books or videos; nothing ran.")
    url = _text(given["url"], "URL")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > URL_MAX or re.search(r"\s", url):
        raise ValueError(f"Add an http or https URL of at most {URL_MAX} characters, with no spaces; nothing ran.")
    held = next((row["id"] for row in records.list_rows(connection, "learning") if row["fields"].get("url") == url), None)
    if held is not None:
        raise ValueError(f"That URL is already in Learning as #{held}; nothing ran.")
    typed = given["title"].strip() if isinstance(given["title"], str) else ""
    if typed and re.search(r"[/\\]", typed):
        raise ValueError("Name the row without a slash: its title is a vault file name; nothing ran.")
    title = typed or source_intake._title(url, time.monotonic() + source_intake.TITLE_SECONDS).removesuffix(" - YouTube")
    title = re.sub(r"\s+", " ", re.sub(r"[/\\:#\[\]|\"]", " ", title)).strip()
    if not title or title.startswith((".", "-")) or len(title) > 120:
        raise ValueError("Name the row: a title starts with a letter or digit and has at most 120 characters; nothing ran.")
    kind = "youtube" if given["view"] == "videos" and parts.hostname in YOUTUBE_HOSTS else LEARNING_KINDS[given["view"]]
    args = ["sdw.learning", title, "--field", f"content-type={kind}", "--field", f"url={url}",
            "--field", f"dateCreated={date.today().isoformat()}"]
    return Step(line="sd store add " + " ".join(shlex.quote(a) for a in args), argv=(_command("sd"), "store", "add", *args))


# ---------- Remove and Delete (sd:6703) ----------

#: The stage Remove moves a row to: the one its routine skips. market-watch refreshes active watches and the brief
#: routines read active topics, so a removed row keeps its body, Memory and Sources and gets no new run.
RETIRED = {"market": "declined", "topic": "retired", "blog-idea": "declined", "tip": "declined"}


def _store_row(connection, values, kind: str) -> dict:
    """The `sdw.<kind>` row the page names by its item number, read from the store here, never taken from the page."""
    from sd_db import records

    item = _fields(values, {"item"})["item"]
    if type(item) is not int:
        raise ValueError(f"Name the {kind} row by its item number; nothing ran.")
    row = next((r for r in records.list_rows(connection, kind) if r["id"] == item), None)
    if row is None:
        raise ValueError(f"#{item} is not a {kind} row; nothing ran.")
    return row


def _move(kind: str, restore: bool = False):
    """Remove: the row's stage moves to `RETIRED[kind]`; Restore moves it from there back to the stage its queue waits
    in (`triage.QUEUES`): candidate (sd-writing sd:6828 added that move), inbox for a declined idea or tip (sd:8014). Both go through `records.edit_row`, as Tasks moves a base row. The dashboard is the
    operator, so the plugin's `human-only` stage is its to set; `sd store set` refuses it to agents."""

    def check(connection, values, jobs=None) -> Step:
        from sd_db import records, workflow

        from . import triage

        row = _store_row(connection, values, kind)
        current = row.get("stage") or ""
        stage = dict((k, s) for k, s, _, _ in triage.QUEUES)[kind] if restore else RETIRED[kind]
        if restore and current != RETIRED[kind]:
            raise ValueError(f"{row['title']} is {current}, not {RETIRED[kind]}; nothing ran.")
        if current == stage:
            raise ValueError(f"{row['title']} is {stage} already; nothing ran.")
        rules = records.rules_for(kind, home=Path.home())
        if rules.transitions is not None and stage not in rules.transitions.get(current, []):
            raise ValueError(f"{row['title']} is {current}: its plugin allows no move to {stage}; nothing ran.")

        def call():
            try:
                revision = workflow.item_state(connection, row["id"])["revision"]
                records.edit_row(connection, row["id"], rules, revision=revision, who="dashboard", stage=stage)
            except workflow.WorkflowError as error:
                return 1, str(error)
            return 0, f"{row['title']}: stage {current} -> {stage}. Its body stays; " + (
                f"it waits in its {stage} again." if restore else "Restore brings it back, Delete removes the row.")

        return Step(line=f"stage {current} -> {stage} on #{row['id']} (records.edit_row, as Tasks moves it)", call=call)

    return check


def _delete(kind: str):
    """Delete: the row, its notes and its assignments go for good, through `sd-db item remove`. Its preview's
    fingerprint is handed to its apply at once, and only when the verb's default store is the database the row was read
    from. The verb files its removal record and backs the store up first, and
    refuses a row another one still needs (an open question, a running assignment)."""

    def check(connection, values, jobs=None) -> Step:
        from sd_db import database

        row = _store_row(connection, values, kind)
        # `sd-db item remove` opens the default store, and the dashboard may serve another (`--database`): the same id
        # there is another row, so Delete runs only when both are one file (review of build/sd-206).
        served = next((r[2] for r in connection.execute("PRAGMA database_list") if r[1] == "main"), "")
        target = Path(str(database.default_path(Path.home())))
        if not served or Path(served).resolve() != target.resolve():
            raise ValueError(f"sd-db item remove opens {target}, another database than the one this dashboard serves "
                             f"({served or 'no file'}); nothing ran. Delete #{row['id']} with sd-db on its own store.")
        verb = [_command("sd-db"), "item", "remove", str(row["id"]), "--who", "dashboard",
                "--reason", f"Deleted on the dashboard: sdw.{kind} {row['title']}"]

        def call():
            said = []
            try:
                preview = subprocess.run(verb, capture_output=True, text=True, timeout=30, check=False)
                said.append((preview.stdout + preview.stderr).strip())
                found = re.search(r"^sd-db: fingerprint: ([0-9a-f]+)$", preview.stdout, re.M)
                if preview.returncode != 0 or not found:
                    return preview.returncode or 1, "\n".join(said)
                done = subprocess.run([*verb, "--apply", "--if-fingerprint", found.group(1)], capture_output=True,
                                      text=True, timeout=80, check=False)
                said.append((done.stdout + done.stderr).strip())
                return done.returncode, "\n".join(said)
            except subprocess.TimeoutExpired:
                return None, "\n".join([*said, f"sd-db item remove gave no answer, so whether #{row['id']} is gone is not known."])
            except OSError as error:
                return 1, f"{verb[0]} did not start: {error}; nothing ran."

        return Step(line=" ".join(shlex.quote(a) for a in ["sd-db", *verb[1:]]), call=call, seconds=120)

    return check


#: Every action a page may post, by name: its label for the journal and Details, and its check.
ACTIONS = {
    "job.log": ("Show log", job_log),
    "job.print": ("Print", job_print),
    "assignment.get": ("Show assignment", assignment_get),
    "storage.du": ("Show biggest", storage_du),
    "volume.df": ("Show use", volume_df),
    "port.lsof": ("Inspect listener", port_lsof),
    "credentials.probe": ("Probe again", credentials_probe),
    "credentials.signin": ("Sign in", credentials_signin),
    "check.run": ("Re-check", check_run),
    "sensor.read": ("Read state", sensor_read),
    "skill.scan": ("Scan", skill_scan),
    "idea.capture": ("Capture idea", idea_capture),
    "declaration.write": ("Write declaration", declaration_write),
    "skill.run": ("Run skill", skill_run),
    "skill.adopt": ("Adopt skill", skill_adopt),
    "contribution.nudge": ("Post nudge", contribution_nudge),
    "repo.pull": ("Pull", repo_pull),
    "watch.refresh": ("Refresh watch", watch_refresh),
    "watch.add": ("Add watch", watch_add),
    "watch.remove": ("Remove watch", _move("market")),
    "watch.restore": ("Restore watch", _move("market", restore=True)),
    "watch.delete": ("Delete watch", _delete("market")),
    "idea.restore": ("Restore idea", _move("blog-idea", restore=True)),
    "tip.restore": ("Restore tip", _move("tip", restore=True)),
    "topic.add": ("Add topic", topic_add),
    "topic.remove": ("Remove topic", _move("topic")),
    "topic.restore": ("Restore topic", _move("topic", restore=True)),
    "topic.delete": ("Delete topic", _delete("topic")),
    "learning.add": ("Add", learning_add),
}


def _studio():
    from . import studio

    ACTIONS.update(studio.ACTIONS)


_studio()


def run(name: str, step: Step) -> tuple[int, dict]:
    """The step, bounded: 200 with its exit and output tail, or 504 when it ran past its bound."""
    answer = {"action": name, "label": ACTIONS[name][0], "line": step.line}
    extra: dict = {}
    if step.call is not None:
        code, output, *more = step.call()
        extra = more[0] if more else {}
    else:
        try:
            done = subprocess.run(list(step.argv), cwd=step.cwd, input=step.stdin if step.stdin is not None else "",
                                  capture_output=True, text=True, timeout=step.seconds, check=False, env=step.env)
            code, output = done.returncode, (done.stdout + (("\n" if done.stdout and done.stderr else "") + done.stderr))
        except subprocess.TimeoutExpired:
            code, output = None, f"It gave no answer in {step.seconds} s; its process was stopped."
        except OSError as error:
            # Nothing started: the program is missing or cannot run, so nothing ran.
            return 503, {**answer, "error": f"{step.argv[0]} did not start: {error}; nothing ran."}
    if code is None:
        return 504, {**answer, "error": output, "outcome": "unknown"}
    if step.shape is not None:
        output = step.shape(code, output)
    output = output.rstrip()
    cut = len(output) > OUTPUT_CHARS
    return 200, {**answer, **extra, "exit": code, "output": output[-OUTPUT_CHARS:], "cut": cut}


def request(values: dict, *, jobs=None):
    """`POST /api/actions/run`'s checks before the server opens a writer; the write, as `action_route` takes it."""
    values = dict(values)
    name = values.get("action")
    if not isinstance(name, str) or name not in ACTIONS:
        raise ValueError(f"Choose one of {', '.join(ACTIONS)}; nothing ran.")
    check = ACTIONS[name][1]

    def write(connection):
        step = check(connection, values, jobs)
        return step if isinstance(step, tuple) else run(name, step)

    return write
