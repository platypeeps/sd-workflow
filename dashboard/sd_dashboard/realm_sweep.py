"""The Work and Personal sweeps (sd:5096, sd:5097): one headless `claude -p` per realm, started from the Tasks page.

    python3 -m sd_dashboard.realm_sweep run work|personal             the sweep, now; one run per realm at a time
    python3 -m sd_dashboard.realm_sweep status work|personal [--json] the last run: running, its end, its log

    environment: XDG_STATE_HOME, XDG_CONFIG_HOME, CLAUDE_BIN, WORKSPACE_MCP_URL,
    REALM_SWEEP_SD (default sd), REALM_SWEEP_TIMEOUT (seconds, default 3600)

A run leaves three things and commits to no repository: a dated brief on the Briefs page (`<day> - Work sweep` or
`<day> - Personal sweep`) with the status and the changes since the last run; sd followups, one per action, deduped by
`--ref` and against the open items; and Gmail drafts, never sent. The HOA sweep is the hoa checkout's own
(`tools/sweep.py`), with its own prompt; `sweep.py` starts all three.

Sources:

- personal: the personal Google account's mail and its My Drive changes. `mail_query` leaves out the HOA aliases and
  `drive_exclude` the HOA shared Drives: the HOA sweep owns them.
- work: the work Google account's mail and Drive changes, Notion page changes, Jira issues assigned to, mentioning or
  watched by the operator, and GitHub pull requests and reviews requesting the operator, and mentions.

The filters are fleet settings (sd:5109), `sweep.<realm>.<name>` rows in the workflow database (`SETTINGS`), which the
Settings page edits under Sweeps. The account is the `google.accounts.<realm>.email` setting row, or without one that key
in the pack's machine config (`<XDG_CONFIG_HOME>/sd-ai-command-pack/config.json`): the row first, as `sd config` reads a
fleet key. A failed database read fails the run: it is unknown, not unset.

The run has an exact allowlist, `--permission-mode dontAsk`, and never skips permission checks. Its MCP tools read
(`MCP_READS`); its one write is `draft_gmail_message`. No tool that sends, forwards or labels mail, writes Drive, Notion
or Jira, or comments on GitHub is allowed, and `DENIED` names them as well. Bash runs only `sd store items`,
`sd task show|note` and the realm's `sd task add`; Write reaches only the run folder. Each run loads its servers alone
(`--strict-mcp-config`): the Personal run workspace-mcp, the Work run also notion, jira and github (`WORK_MCP`), whose
credentials come from env.sh by name, never in argv or a file.

Steps, and what a stop between them leaves:

1. The lock, `<state>/sweep.lock`: a second run of the realm exits 3 and writes nothing. The model run inherits it
   (sd:5341), so a runner killed in step 3 leaves its model holding the lock, `status` reads it running, and no run
   starts beside it.
2. `<state>/last.json` as `running`, naming the run folder, the log, the brief and the pid. `status` reads a `running`
   record whose pid is gone as failed, so a stop from here on reads failed.
3. The model run, in `<state>/runs/<stamp>/`. Followups and drafts land as it goes; a stop leaves them without a brief.
   The next run finds them: a followup's `--ref` names one message or comment (or a file's version) and refuses a
   second add, and a reply a draft already gives gets no other.
4. The brief filed with `sd reports brief add --replace`. A stop before it leaves the brief in the run folder. Before
   its model, each run reads today's filed brief, if any, and keeps its changes, so a second run in a day replaces it
   with more; a read that failed for another reason than "no brief" stops the run there.
5. `last.json` as `done` or `failed`, with the exit code, the reason, and the sources read (`read`) and not (`gaps`).
   Each source keeps its own checkpoint (`checkpoints`): only a done run moves one on, and only for a source its
   reply names on its `READ:` line. A failed run and an unread source keep the older time. A source's first attempt
   writes its start, seven days before that run, to the record before the model, and it stays until a run reads it.

Exit codes: 0 done, 3 another run of the realm holds the lock, 1 failed.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .claude_env import SHELL_ENV
from .sweep import DASHBOARD, _held, state_dir

REALMS = ("work", "personal")
SERIES = {"work": "Work sweep", "personal": "Personal sweep"}
EXIT_OK, EXIT_NONE, EXIT_ERROR = 0, 3, 1
TIMEOUT = 3600
SD_SECONDS = 60
#: How long a stopped run's pipes are read once it is killed: a grandchild a refused group kill left can hold
#: them open (sd:7910).
REAP_SECONDS = 5
#: The checkout the briefs belong to: `sd reports brief add` files a brief only under a registered repository.
REPO = DASHBOARD.parent

#: The sweep settings (sd:5109) and what each holds, as Settings lists them. `REQUIRED` must hold a value; a source left
#: unset is skipped and named under the brief's Gaps.
SETTINGS = {
    "sweep.personal.mail_query": "The Personal sweep's Gmail search. Leave out the HOA aliases: the HOA sweep reads them.",
    "sweep.personal.drive_exclude": "Shared Drives the Personal sweep skips, comma-separated: the HOA Drives.",
    "sweep.work.mail_query": "The Work sweep's Gmail search.",
    "sweep.work.drive_exclude": "Shared Drives the Work sweep skips, comma-separated.",
    "sweep.work.notion_scope": "The Notion teamspaces or pages the Work sweep searches. Unset skips Notion.",
    "sweep.work.jira_scope": "The Jira projects the Work sweep searches, as JQL such as project in (ABC). Unset skips Jira.",
    "sweep.work.github_scope": "The GitHub owners the Work sweep searches, as qualifiers such as org:example. Unset skips GitHub.",
    "sweep.work.followups_repo": "The registered repo the Work sweep files its follow-up tasks under.",
}
REQUIRED = ("sweep.personal.mail_query", "sweep.work.mail_query", "sweep.work.followups_repo")
#: Each realm's sources and the setting that switches one on; `None` is always read. Each keeps its own checkpoint.
SOURCES = {"personal": {"mail": None, "drive": None},
           "work": {"mail": None, "drive": None, "notion": "notion_scope", "jira": "jira_scope", "github": "github_scope"}}
ACCOUNT = "google.accounts.{realm}.email"
#: How far back a source's first attempt reads; its record then keeps that time until a run reads the source.
FIRST_WINDOW = datetime.timedelta(days=7)

BUILT_IN_TOOLS = "Bash,Read,Write,ToolSearch"
DRAFT = "draft_gmail_message"
#: The reads, by MCP server. The Work run has all four; the Personal run workspace-mcp alone.
MCP_READS = {
    "workspace-mcp": ("search_gmail_messages", "get_gmail_message_content", "get_gmail_messages_content_batch",
                      "get_gmail_thread_content", "search_drive_files", "get_drive_file_content", "get_doc_content",
                      "get_doc_as_markdown"),
    "notion": ("notion-search", "notion-fetch"),
    "jira": ("jira_search_issues", "jira_get_my_issues", "jira_get_issue", "jira_get_comments"),
    "github": ("get_me", "search_pull_requests", "search_issues", "pull_request_read", "issue_read",
               "list_notifications"),
}
#: Denied by name as well: dontAsk refuses what is not allowed, and a deny wins over an allow a settings file adds.
DENIED = (
    "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Skill",
    *(f"mcp__workspace-mcp__{n}" for n in (
        "send_gmail_message", "modify_gmail_message_labels", "batch_modify_gmail_message_labels", "manage_gmail_filter",
        "manage_gmail_label", "create_drive_file", "update_drive_file", "copy_drive_file", "create_drive_folder",
        "manage_drive_access", "set_drive_file_permissions", "create_doc", "modify_doc_text", "batch_update_doc")),
    *(f"mcp__notion__notion-{n}" for n in (
        "create-pages", "update-page", "move-pages", "duplicate-page", "create-database", "update-database",
        "create-comment")),
    *(f"mcp__jira__jira_{n}" for n in (
        "create_issue", "update_issue", "transition_issue", "add_comment", "add_attachment", "delete_attachment",
        "create_subtask", "create_issue_link")),
    *(f"mcp__github__{n}" for n in (
        "add_issue_comment", "add_comment_to_pending_review", "pull_request_review_write", "create_pull_request",
        "update_pull_request", "merge_pull_request", "issue_write", "request_copilot_review", "create_or_update_file",
        "push_files")),
)
#: The Work run's other servers, as the user's Claude config defines them: `--setting-sources project` skips that
#: config (sd:5764). No credential is written here or reaches argv. notion finds the login it stored under its name;
#: claude expands github's `${NAME}` when it starts the server, and jira's process inherits its JIRA_* variables, both
#: from env.sh, which the run's shell sources (`WITH_TOKEN`).
WORK_MCP = {
    "notion": {"type": "http", "url": "https://mcp.notion.com/mcp"},
    "jira": {"type": "stdio", "command": "npx", "args": ["-y", "mcp-jira-stdio@latest"]},
    "github": {"type": "http", "url": "https://api.githubcopilot.com/mcp",
               "headers": {"Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"}},
}
TOLD = (
    "This unattended run has no person to answer. Bash runs only the sd commands the prompt names, each on its own: "
    "no cd, no pipes, no $(...). Write reaches only the run folder. Gmail allows reading and draft_gmail_message, "
    "nothing that sends; Drive, Notion, Jira and GitHub allow reading only. An MCP server may still be connecting when "
    "the run starts: if ToolSearch reports it still connecting, wait and search again, up to five times, before "
    "treating that source as unreachable."
)
RUN_ENV_DEFAULTS = {"MCP_CONNECT_TIMEOUT_MS": "60000", "MCP_TOOL_TIMEOUT": "600000"}
# The dashboard server under launchd has no token. env.sh holds it as CUSTOM_CLAUDE_CODE_OAUTH_TOKEN, so SHELL_ENV
# (claude_env) sources env.sh and maps it (sd:5791). A machine without the file runs on.
WITH_TOKEN = SHELL_ENV + 'exec "$@"'
RESULT_OK = re.compile(r"^RESULT: ok\s*$")
#: The reply's line naming the sources it read in full, as `READ: mail, drive`.
READ_LINE = re.compile(r"^READ:(.*)$")

PROMPT = """You run the operator's {label} for them, unattended. Nobody answers questions. Do the steps in order.

Facts:
- Google account: {account}. Every Gmail and Drive call names it as user_google_email.
- Run folder: {folder}. Write files only there.
- Brief file: {brief}
- Today's brief so far: {earlier}
- Followup reference: {realm}-sweep:<source>-<id>

Since, by source. Read what changed in each source after its own time; a source an earlier run could not read keeps
the time that run had. A source marked "not configured" is skipped and named under Gaps.
{since}

The sweep's settings:
{filters}

{steps}
Followups. For each action the operator owns, keep one sd item:
- Read the open items once: `sd store items --open --json`.
- An open item that covers the action gets a note: `sd task note <id> "<what changed>" --kind followup`.
- Otherwise file one: `{add} --priority <1-4> --ref {realm}-sweep:<source>-<id> "<title>"`, 3 unless it is due within
  two weeks (2) or urgent (1): sd refuses an item without a priority (sd:6726). <source> is gmail, drive, notion, jira or
  github. <id> names the one message or comment that asks, never its whole thread, file, page, issue or pull request,
  which can ask again later: a Gmail message id; a Jira issue key and comment id (`ABC-12/10023`); a GitHub
  owner/repo#number and comment or review id (`owner/repo#7/123`); a Notion comment id. Where nothing finer than the
  whole asks (a Drive file, a Notion page, a Jira assignment, a review request), add `@` and its modified time
  (`<file id>@2026-10-18T09:00:00Z`). A second action in that same message or comment adds `.2`, `.3`. A refusal
  that names an existing item means this one occurrence is filed already: leave it.

Drafts. For each reply the operator owes, save one Gmail draft in {account} with draft_gmail_message, in the
thread it answers. Search `in:drafts` first and skip a reply a draft already gives; a thread with a draft for
another message still gets this one. Never send.

Brief. Write {brief} with the Write tool. It opens with these three lines:
---
series: {series}
---
Then `## Status` (what needs the operator now, most urgent first), `## Changes` (one short part per source, since its
time above), `## Followups` (each sd item filed or noted, with its id), `## Drafts` (each draft saved, by subject) and
`## Gaps` (each source not read, and why). If today's brief so far is a file, read it and keep its changes. Plain
sentences, no tables, no message bodies quoted at length.

Reply with a short report. Its second-last line is `READ: ` and the sources you read in full, comma-separated (for
example `READ: mail, drive`); leave out every source you skipped or read only in part. Its last line is `RESULT: ok`,
or `RESULT: failed, <step>: <reason>` when a step could not finish. A source that could not be read is a gap in the
brief, not a failure.
"""
STEPS = {
    "personal": """Mail. Search Gmail with mail_query and `after:` its Since date. Read the threads that ask the
operator for something, change a commitment, or carry a deadline.

Drive. Search Drive for files in the account's My Drive modified after its Since time. Skip the shared Drives that
drive_exclude names; the HOA sweep owns them.
""",
    "work": """Mail. Search Gmail with mail_query and `after:` its Since date. Read the threads that ask the
operator for something, change a commitment, or carry a deadline.

Drive. Search Drive for files modified after its Since time, in My Drive and the shared Drives, except those
drive_exclude names.

Notion. Search Notion within notion_scope for pages edited after its Since time, and read those that concern the
operator. If Notion does not answer, or answers from a workspace that is not the work one, say so under Gaps.

Jira. Search issues within jira_scope, updated after its Since time, that are assigned to the operator, mention them, or
that they watch: (assignee = currentUser() OR watcher = currentUser() OR text ~ currentUser()).

GitHub. Within github_scope, find open pull requests that request the operator's review by name
(user-review-requested:@me), reviews on their own open pull requests (author:@me), and mentions (mentions:@me),
updated after its Since time.
""",
}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def accounts_path() -> Path:
    """The pack's machine config, by its rule (`sd_lib.machine_config_path`)."""
    home = os.environ.get("XDG_CONFIG_HOME") or Path(os.environ.get("HOME") or Path.home()) / ".config"
    return Path(home) / "sd-ai-command-pack" / "config.json"


def held(realm: str) -> tuple[dict, str]:
    """The realm's setting rows and its account row, read once: `({key: value}, "")`, or `({}, why not)`.

    A key with no row is absent, a cleared one `None` (`sd_db.writes.read_settings`). A failed open or read is named:
    it is unknown, never "unset".
    """
    keys = [key for key in SETTINGS if key.startswith(f"sweep.{realm}.")] + [ACCOUNT.format(realm=realm)]
    try:
        from sd_db import connect, writes

        with contextlib.closing(connect(write=False)) as connection:
            return writes.read_settings(connection, keys), ""
    except Exception as error:  # noqa: BLE001 - any open or read failure is named, as sd_lib.fleet_connection does
        return {}, f"the {realm} sweep settings did not read from the workflow database: {type(error).__name__}: {error}"


def account(realm: str, rows: dict) -> tuple[str, str]:
    """`(address, "")`, or `("", why not)`: the `google.accounts.<realm>.email` row, else that key in config.json."""
    key = ACCOUNT.format(realm=realm)
    if key in rows:
        found, where = rows[key], f"the workflow database's {key} setting"
        if found is None:
            return "", f"{where} is unset"
    else:
        path = accounts_path()
        where = str(path)
        try:
            found = json.loads(path.read_text(encoding="utf-8"))["google"]["accounts"][realm]["email"]
        except FileNotFoundError:
            return "", f"no {key} setting, and {path} is missing"
        except (OSError, ValueError, KeyError, TypeError) as error:
            return "", f"no {key} setting, and {path} holds none ({error.__class__.__name__})"
    if not isinstance(found, str) or "@" not in found:
        return "", f"{where}: {key} is not an address"
    return found, ""


def filters(realm: str, rows: dict) -> tuple[dict, str]:
    """`({name: value}, "")`, each setting by its name after `sweep.<realm>.`, or `({}, why not)` when a required one is unset."""
    prefix = f"sweep.{realm}."
    missing = [key for key in REQUIRED if key.startswith(prefix) and not rows.get(key)]
    if missing:
        return {}, f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set; set it on Settings, under Sweeps"
    found = {key[len(prefix):]: rows.get(key) for key in SETTINGS if key.startswith(prefix)}
    if found.get("followups_repo"):
        found["followups_repo"] = os.path.expanduser(found["followups_repo"])
    return found, ""


def adder(realm: str, followups: str) -> str:
    """The realm's `sd task add`. sd takes `--ref` on a task or followup only, so Personal files a followup with no
    repository, which is what puts a row in Personal; a `--kind personal` add refused every ref (sd:5096 review)."""
    return "sd task add --kind followup --no-repo" if realm == "personal" else f"sd task add --belongs-to {followups}"


def allowed_tools(realm: str, folder: Path, followups: str = "") -> list[str]:
    """The run's exact allowlist: reads, the run folder, the realm's sd verbs and the one Gmail draft tool. Write's
    grant is an `Edit(path)` rule: Claude Code checks every file write against Edit rules and ignores `Write(path)`."""
    add = adder(realm, followups)
    servers = ("workspace-mcp",) if realm == "personal" else tuple(MCP_READS)
    return [
        f"Read(/{folder}/**)", f"Edit(/{folder}/**)",
        "Bash(sd store items:*)", "Bash(sd task show:*)", "Bash(sd task note:*)", f"Bash({add}:*)",
        *(f"mcp__{server}__{name}" for server in servers for name in MCP_READS[server]),
        f"mcp__workspace-mcp__{DRAFT}",
    ]


def claude_argv(claude: str, realm: str, prompt: str, mcp_url: str, allowed: list[str]) -> list[str]:
    """The run: an exact allowlist, never skipped permissions, and the realm's servers alone (`MCP_READS`)."""
    loaded = {"workspace-mcp": {"type": "http", "url": mcp_url}}
    if realm == "work":
        loaded.update(WORK_MCP)
    servers = ["--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": loaded})]
    return [
        claude, "-p", prompt,
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--setting-sources", "project",
        *servers,
        "--tools", BUILT_IN_TOOLS,
        "--allowedTools", ",".join(allowed),
        "--disallowedTools", ",".join(DENIED),
        "--append-system-prompt", TOLD,
    ]


def claude_binary() -> str:
    """CLAUDE_BIN, then `claude` on PATH, then ~/.local/bin/claude, as local-cron-jobs does."""
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def ask(argv: list[str], cwd: Path, timeout: int, out, keep: tuple[int, ...] = ()) -> tuple[int, str, list[str]]:
    """Run claude headless, its reply to `out`, and judge the reply's last `RESULT:` line: ok is 0, anything else 1.

    The third value is what the reply's last `READ:` line names, and empty without one: an unnamed source was not read.

    `claude -p` exits 0 whatever the prompt concluded. The run gets its own process group, so a timeout stops its MCP
    children too. SD_HANDOFF_RESTORE=0: an unattended run must not consume a handoff written for a person's session.
    `keep` are descriptors the run inherits, such as the lock its caller holds.
    """
    env = {**RUN_ENV_DEFAULTS, **os.environ, "SD_HANDOFF_RESTORE": "0"}
    try:
        proc = subprocess.Popen(["sh", "-c", WITH_TOKEN, "claude-run", *argv], cwd=cwd, env=env, text=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, pass_fds=keep)
    except OSError as error:
        return EXIT_ERROR, f"cannot start {argv[0]}: {error}", []
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:  # EPERM: macOS for an exiting group, or a sandbox (sd:7900)
            proc.kill()
        try:
            proc.communicate(timeout=REAP_SECONDS)
        except subprocess.TimeoutExpired:
            # A grandchild the group kill missed holds the pipes; the run is dead, so stop reading them.
            proc.stdout.close()
            proc.stderr.close()
            proc.wait()
        return EXIT_ERROR, f"the run passed {timeout}s and was stopped", []
    print(stdout.rstrip(), file=out)
    if proc.returncode != 0:
        tail = (stderr or stdout).strip().splitlines()[-1:] or [""]
        return EXIT_ERROR, f"claude exited {proc.returncode}: {tail[0][:200]}", []
    results = [line.strip() for line in stdout.splitlines() if line.startswith("RESULT: ")]
    last = results[-1] if results else ""
    reads = [m[1] for m in map(READ_LINE.match, stdout.splitlines()) if m]
    # The names the since block shows, read loosely: case, stray punctuation, and gmail for mail, as followup refs say.
    names = [name.strip(" .`").lower() for name in reads[-1].split(",")] if reads else []
    read = ["mail" if name == "gmail" else name for name in names if name]
    return (EXIT_OK, last, read) if RESULT_OK.match(last) else (EXIT_ERROR, last or "no RESULT: line in the reply", read)


def sd(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([os.environ.get("REALM_SWEEP_SD", "sd"), *args], capture_output=True, text=True,
                          timeout=SD_SECONDS, check=False, stdin=subprocess.DEVNULL)


def write_record(path: Path, record: dict) -> None:
    """Replace the record whole, so a reader never sees half of one."""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)


def read_record(realm: str) -> dict | None:
    try:
        return json.loads((state_dir(realm) / "last.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def alive(pid) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def status(realm: str) -> dict:
    """The last run as the dashboard reads it: `running`, `state`, `exit`, `reason`, `log`, `brief`, times.

    A record that does not read raises OSError or ValueError; the caller names it.
    """
    record = read_record(realm)
    if record is None:
        return {"running": False, "state": "never", "reason": f"no {realm} sweep has run here"}
    if record.get("state") == "running" and not alive(record.get("pid")):
        if _held(state_dir(realm), "sweep.lock"):
            record["reason"] = ("its runner stopped, and its model run still holds the lock; nothing starts until "
                                "that ends")
        else:
            record.update(state="failed", reason=f"the run (pid {record.get('pid')}) stopped without recording an "
                                                 f"outcome; see {record.get('log')}")
    record["running"] = record.get("state") == "running"
    return record


def run(realm: str, out, err) -> int:
    """Step 1: the realm's lock, without waiting; the sweep under it."""
    state = state_dir(realm)
    (state / "runs").mkdir(parents=True, exist_ok=True)
    with open(state / "sweep.lock", "w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"sweep: another {realm} sweep is already running", file=err)
            return EXIT_NONE
        return sweep(realm, state, out, err, handle.fileno())


def checkpoints(realm: str, previous: dict) -> dict:
    """Each source's checkpoint, the time its next read starts from: the previous run's start for a source that run
    read in full and recorded done, else the checkpoint that run started from. `None` is a source never attempted
    here; `sweep` writes its first window before the model runs.

    So a failed run, a done run's unread source, and a source configured since all keep the older time (sd:5096
    review). A record from before per-source checkpoints names one `since` and no `read`: every source keeps it.
    """
    before = previous.get("since")
    before = before if isinstance(before, dict) else {source: before for source in SOURCES[realm]}
    read = previous.get("read") if previous.get("state") == "done" and isinstance(previous.get("read"), list) else []
    return {source: previous.get("started") if source in read else before.get(source) for source in SOURCES[realm]}


def sweep(realm: str, state: Path, out, err, lock: int) -> int:
    """Steps 2 to 5 above, under `lock`, the descriptor of step 1's lock, which the model run inherits."""
    try:
        previous = read_record(realm) or {}
    except (OSError, ValueError):
        previous = {}
    since = checkpoints(realm, previous)
    started = _now()
    stamp = started.strftime("%Y%m%d-%H%M%S-%f")
    folder = state / "runs" / stamp
    folder.mkdir(parents=True)
    log = folder / "run.log"
    day = started.astimezone().date().isoformat()
    stem = f"{day} - {SERIES[realm]}"
    brief = folder / f"{stem}.md"
    record = {"state": "running", "started": started.isoformat(timespec="seconds"), "finished": None, "exit": None,
              "reason": None, "since": since, "read": [], "gaps": [], "log": str(log), "brief": str(brief),
              "pid": os.getpid()}
    write_record(state / "last.json", record)

    def end(code: int, reason: str) -> int:
        record.update(state="done" if code == EXIT_OK else "failed", exit=code, reason=reason,
                      finished=_now().isoformat(timespec="seconds"))
        write_record(state / "last.json", record)
        print(f"sweep: exit {code}: {reason}", file=out if code == EXIT_OK else err)
        return code

    rows, problem = held(realm)
    if problem:
        return end(EXIT_ERROR, problem)
    address, problem = account(realm, rows)
    if not address:
        return end(EXIT_ERROR, f"no Google account for the {realm} sweep: {problem}")
    found, problem = filters(realm, rows)
    if problem:
        return end(EXIT_ERROR, problem)
    earlier = folder / "earlier.md"
    try:
        shown = sd("reports", "brief", "show", stem)
    except (OSError, subprocess.TimeoutExpired) as error:
        return end(EXIT_ERROR, f"today's brief was not read: {error}")
    # Only "no brief has the stem" means none yet: any other failure is unknown, and a replace after it could drop the
    # changes an earlier run filed today.
    if shown.returncode == 0 and shown.stdout.strip():
        earlier.write_text(shown.stdout, encoding="utf-8")
    elif shown.returncode != 0 and "no brief has the stem" not in shown.stderr:
        return end(EXIT_ERROR, f"today's brief was not read: {(shown.stderr or shown.stdout).strip()[:200]}")
    configured = [source for source, switch in SOURCES[realm].items() if switch is None or found.get(switch)]
    # A source's first attempt fixes its window's start, seven days back, in the record before the model runs: a
    # relative "seven days ago" would slide on every retry past the changes the first attempt was owed (sd:5096 review).
    for source in configured:
        since[source] = since[source] or (started - FIRST_WINDOW).isoformat(timespec="seconds")
    write_record(state / "last.json", record)
    prompt = PROMPT.format(
        label=SERIES[realm].lower(), account=address, folder=folder, brief=brief, realm=realm, series=SERIES[realm],
        since="\n".join(f"- {source}: {since[source] if source in configured else 'not configured'}"
                        for source in SOURCES[realm]),
        earlier=earlier if earlier.exists() else "none yet",
        # An unset drive_exclude excludes nothing: "not configured" would read as "skip Drive".
        filters="\n".join(f"- {name}: {value or ('none' if name == 'drive_exclude' else 'not configured')}"
                          for name, value in found.items() if name != "followups_repo"),
        steps=STEPS[realm],
        add=adder(realm, found.get("followups_repo") or ""))
    argv = claude_argv(claude_binary(), realm, prompt, os.environ.get("WORKSPACE_MCP_URL", "http://127.0.0.1:8083/mcp"),
                       allowed_tools(realm, folder, found.get("followups_repo") or ""))
    with log.open("a", encoding="utf-8") as logged:
        code, reason, read = ask(argv, folder, int(os.environ.get("REALM_SWEEP_TIMEOUT", TIMEOUT)), logged,
                                 keep=(lock,))
    # Only a configured source the reply names counts as read; the rest keep their checkpoints and show as gaps.
    record.update(read=[source for source in configured if source in read],
                  gaps=[source for source in configured if source not in read])
    if code != EXIT_OK:
        return end(code, reason)
    if not brief.is_file():
        return end(EXIT_ERROR, f"the run ended {reason!r} but wrote no brief at {brief}")
    try:
        filed = sd("reports", "brief", "add", "--file", str(brief), "--belongs-to", str(REPO), "--replace")
    except (OSError, subprocess.TimeoutExpired) as error:
        return end(EXIT_ERROR, f"the brief was not filed: {error}")
    if filed.returncode != 0:
        return end(EXIT_ERROR, f"the brief was not filed: {(filed.stderr or filed.stdout).strip()[:200]}")
    return end(EXIT_OK, f"{filed.stdout.strip()}; {reason}")


def main(argv: list[str], out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    verb = argv[0] if argv else ""
    if verb in ("-h", "--help", "help"):
        print("\n\n".join(__doc__.strip().split("\n\n")[1:3]), file=out)
        return 0
    if verb == "run" and argv[1:2] and argv[1] in REALMS and len(argv) == 2:
        return run(argv[1], out, err)
    if verb == "status" and argv[1:2] and argv[1] in REALMS and argv[2:] in ([], ["--json"]):
        try:
            found = status(argv[1])
        except (OSError, ValueError) as error:
            print(f"sweep: {state_dir(argv[1]) / 'last.json'} did not read: {error}", file=err)
            return EXIT_ERROR
        if argv[2:]:
            print(json.dumps(found, sort_keys=True), file=out)
        else:
            print(f"{found['state']}" + (f", exit {found['exit']}" if found.get("exit") is not None else "")
                  + (f": {found['reason']}" if found.get("reason") else "")
                  + (f"\nlog: {found['log']}" if found.get("log") else ""), file=out)
        return 0
    print("usage: python3 -m sd_dashboard.realm_sweep run|status work|personal [--json]", file=err)
    return EXIT_ERROR


if __name__ == "__main__":
    # The pack's lib/ first, as bootstrap.py puts it for the server: the dashboard starts this as its own process.
    from . import pack_library

    sys.path.insert(0, str(pack_library()))
    sys.exit(main(sys.argv[1:]))
