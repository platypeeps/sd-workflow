"""Adversarial review (sd:5064): one click runs `local-adversarial-gate` on a task, a brief, a repository or a change.

The operator asked on 2026-10-18 (a note on sd:5064) for a hostile second read by another model than the one that
wrote the work: codex, at the model and effort the `adversarial.model` and `adversarial.effort` setting rows hold
(sd:5109; Settings edits them), else `MODEL` at `EFFORT`. A page posts `/api/adversarial` with `{"kind", "target"}`:

  task    an item id. The item's repository is the checkout; an item without one is refused, as Fix refuses it.
  brief   the id of a routine brief (kind `report`, source `brief`). Its repository, else this checkout.
  repo    a registered repository, by its key or its path.
  change  a checkout directory, a worktree or a clone of a registered repository. Optional: `base`, the ref it is
          compared with (default `origin/HEAD`, then `origin/main`, then `main`); `item`, the item it belongs to; and
          `body`, the file holding its pull-request body. With `pr`, a number (sd:5100), the target is a registered
          repository and the change is that open pull request: it must be open in that repository's GitHub slug as
          `sd shadow sync` last saw it, and its head is `refs/pull/<pr>/head` as `git ls-remote origin` names it. The
          head commit must already be in the checkout: the dashboard fetches nothing, and a refusal names the fetch.
  deep    a registered repository, reviewed by headless claude rather than codex (sd:5100): `lenses/deep.md` has the
          lead fan out read-only sub-agents, one per area, then verify and rank what they find. Its model, effort,
          bound and sub-agent cap are the `DEEP` setting rows in the workflow database (sd:5109), else their defaults;
          the Settings page writes them (sd:5784). Its lock is the repository's own, so one deep review runs per
          repository.
  source  a research checkout's key, as the Research page lists it, with `source`, one ledger row's key: its URL or
          `id:<ID>` (sd:5101). The checkout must be registered. The input holds the row and the claims citing it, as
          the page's own reader finds them; the runner appends the row's link as it fetched it, one bounded GET
          (`_fetch`), so codex's sandbox needs no network. The brief names the row's key in its front matter, which is
          how `source_reviews` finds a row's reviews: a re-run files a brief of its own, and a ledger edit that keeps
          the key keeps them.
  piece   a writing piece's item id (pack sd:6699). The runner takes `sd writing adversarial`'s own steps on the
          database the server reads: the `writing-draft` lens, codex read-only in the piece's repository, and the report
          written to the piece's `adversarial.md` with its draft stamp. It files no brief and no batch: the piece's
          Apply review (`fix_session`) acts on the report, and the item's note names it.

`start` runs in the POST, in four steps, and answers 202 at once:

1. The target, resolved to a checkout, its registered repository and the lens's inputs; then the settings.
2. The lock, taken without waiting: `<state>/lock` for a codex review, `<state>/deep-<checkout hash>.lock` for a deep
   one. A second run while one holds it is a 409 naming the run.
3. The run's state file, `<state>/runs/<id>.json`, as `queued`, with the item's or the change's text beside it as
   `<id>.input.md`. Codex writes its result beside them as `<id>.md`.
4. This file as a script, in a session of its own, holding the lock's descriptor: the lock is released only when
   the runner and every process under it have exited, so nothing runs codex beside it, and no second deep review
   reads the same repository.

The runner (`run`) marks the run `running`, runs `adversarial-gate.sh run` (`deep` for a deep review), bounded by
the gate at the run's timeout (exit 124) and here at that timeout plus `GRACE`, and marks it `done` or `failed` with the reason. It then files the outcome in the
database the server reads, in one transaction, as the operator asked in note #22943 on sd:5064:

- Each titled hypothesis is a recommendation: one Approve-or-Reject question in a review batch (`sd_db.review`,
  queue `QUEUE`). An approval files a task in the batch's repository, titled with the question's first line
  (sd:4924), so the runner files one batch per repository the recommendations name.
- A routine brief (`sd_db.routine_briefs`) holds the target, the model and effort, the verdict and the numbered
  recommendations, each naming its batch. Each batch header's `Source:` names the brief's stem.
- When the target is an item (a task, a brief, or the change's `item`), a `comment` note names the brief and the
  batches, or the failure. A failed run files no brief and no batch.

Nothing here approves, merges, publishes or posts anywhere else; the operator picks recommendations on Decide's Scoring tab.

`status` answers `GET /api/adversarial/<id>` from the state file. A run whose runner is gone without an outcome reads
`failed`, with the reason, and a run queued for longer than `STARTUP` that never started reads `failed` too. A run
whose runner is gone after its outcome but before it recorded the filing reads `filed` as not known.

The runner is this file run by path. It imports `sd_db` from the folder the server loaded it from and opens the
database file the server's connection reads; the state file names both.
"""

from __future__ import annotations

import codecs
import datetime
import fcntl
import hashlib
import html.parser
import http.client
import importlib.util
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from sd_db.config import system_checkout

__all__ = ["DEEP", "EFFORT", "GATE", "KINDS", "MODEL", "NOTE", "QUEUE", "SETTINGS", "TIMEOUT", "deep_settings",
           "recommendations", "request", "run", "run_settings", "settings", "start", "status"]

#: The system checkout (`SYSTEM_TOOLS_ROOT`, sd:5987): a brief that names no repository is reviewed here.
CHECKOUT = system_checkout()
GATE = CHECKOUT / "local-adversarial-gate" / "adversarial-gate.sh"
RANK = CHECKOUT / "local-adversarial-gate" / "rank.py"
KINDS = ("task", "brief", "repo", "change", "deep", "source", "piece")
#: The model and effort when no `adversarial.model` or `adversarial.effort` row names one.
MODEL = "gpt-6-astra"
EFFORT = "high"
#: The gate's bound on codex, in seconds; the runner's own bound on the gate is `GRACE` longer.
TIMEOUT = 1800
#: A deep review's settings (sd:5109): fleet-wide `setting` rows in the workflow database, not the `.env`. Each key's
#: default, its pattern and what it wants: the claude CLI's alias for the latest Opus, the highest effort `claude
#: --effort` takes, the bound in whole minutes, and the most sub-agents the lead starts.
DEEP = {
    "adversarial.deep.model": ("opus", r"[A-Za-z0-9._:/@][A-Za-z0-9._:/@-]{0,99}", "a claude model id or alias, such as opus"),
    "adversarial.deep.effort": ("max", r"low|medium|high|xhigh|max", "one of low, medium, high, xhigh or max"),
    "adversarial.deep.bound_minutes": ("90", r"[1-9][0-9]{0,3}", "whole minutes, such as 90"),
    "adversarial.deep.agents": ("8", r"[1-9]|1[0-6]", "a number of sub-agents from 1 to 16, such as 8"),
}
GRACE = 60
#: How long a queued run may wait for its runner to say it started before it reads as failed.
STARTUP = 60
GIT_SECONDS = 10
#: The bound on `git ls-remote origin`, which asks the remote for a pull request's head.
REMOTE_SECONDS = 20
#: A source review's fetch (sd:5101): its whole bound in seconds, the most bytes read, and the most characters of
#: text the input keeps. A source past them is cut and says so.
FETCH_SECONDS = 20
FETCH_BYTES = 2_000_000
#: How long past its own bound `_fetch` waits for its worker to stop at that bound and answer.
FETCH_GRACE = 2
FETCH_CHARS = 150_000
#: How many of a checkout's source-review briefs `source_reviews` reads, newest first.
SOURCE_BRIEFS = 500
#: How many of the newest briefs, from every repository, `source_reviews` looks through for them.
BRIEF_SCAN = 5000
#: Hypotheses whose titles the note names.
TOP = 3
#: The longest pull-request body read into the input file, in bytes.
BODY_MAX = 200_000
#: The review queue the recommendations are asked in, and who the runner writes as.
QUEUE = "adversarial"
WHO = "dashboard-adversarial"
#: Per review: the most recommendations filed. Per recommendation: the longest title in characters, reasoning in
#: bytes and evidence line in characters, and the most evidence lines (`sd_db.review` takes five). 30 recommendations
#: at these sizes stay well under a brief's 200,000 bytes, so no overflow file is needed.
RECOMMENDATIONS = 30
TITLE_CHARS = 120
WHY_BYTES = 3000
EVIDENCE_CHARS = 300
EVIDENCE_LINES = 5
RUN_ID = re.compile(r"(task|brief|repo|change|deep|source|piece)-[A-Za-z0-9._-]{1,60}-[0-9]{8}T[0-9]{6}Z")
REF = re.compile(r"[A-Za-z0-9._/-]{1,200}")
#: A line the lenses ask for under each hypothesis, bulleted or bold or neither.
TAG = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*)?(Recommendation|Evidence|Repo)(?:\*\*)?\s*:(?:\*\*)?\s*(.*?)\s*$", re.IGNORECASE)
#: A line `sd_db.review` refuses in a question's text, as it would read it as the question's own field.
RESERVED = re.compile(r"^[ \t]*(Option|Subject|Evidence):")

NOTE = ("Adversarial review ({model}, {effort} effort): brief {stem} (sd:{brief}); recommendations: {count}, to pick in "
        "review batch {batches}. Top hypotheses: {titles}. These are hypotheses for a person to check, not findings.")
WRITTEN = "Adversarial review ({model}): adversarial.md for {piece} written; Apply review acts on its findings."
FAILED = "Adversarial review failed ({model}, {effort} effort): {reason}. Input: {input}."


# -- where things live -------------------------------------------------------------------------------------------


def state_dir() -> Path:
    """`st_state_dir adversarial-gate`: what runs write and nobody edits."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "system" / "adversarial-gate"


#: The setting rows (sd:5109) and what Settings says each holds; a key with no row, or a cleared one, is the default.
SETTINGS = {
    "adversarial.model": f"The Codex model the background review uses. Unset is {MODEL}.",
    "adversarial.effort": f"How hard the background review thinks: one lower-case word such as low, medium or high. Unset is {EFFORT}.",
}
#: What a value may hold: it reaches codex's argv as one word.
PATTERNS = {"adversarial.model": r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", "adversarial.effort": r"[a-z]{1,20}"}


def settings(connection) -> tuple[str, str]:
    """The model and effort: the setting rows, else `MODEL` and `EFFORT`. A failed read raises: it is unknown, not unset."""
    from sd_db import writes

    held = writes.read_settings(connection, list(SETTINGS))
    return held.get("adversarial.model") or MODEL, held.get("adversarial.effort") or EFFORT


def run_settings(connection, kind: str) -> tuple[str, str, int, dict]:
    """The model, effort, timeout in seconds and the lens's extra `--set` values for a run of `kind`.

    A codex review reads the `SETTINGS` rows (`settings`) and runs for `TIMEOUT`. A deep review reads its `DEEP`
    setting rows (`deep_settings`).
    """
    if kind == "deep":
        held = {key: one["value"] for key, one in deep_settings(connection).items()}
        return (held["adversarial.deep.model"], held["adversarial.deep.effort"],
                int(held["adversarial.deep.bound_minutes"]) * 60, {"AGENTS": held["adversarial.deep.agents"]})
    model, effort = settings(connection)
    return model, effort, TIMEOUT, {}


def _deep_value(key: str, value) -> str:
    """`value` for the `DEEP` setting `key`, or `ValueError` naming what it wants."""
    _, pattern, wants = DEEP[key]
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"{key} wants {wants}, not {value!r}")
    return value


def deep_settings(connection) -> dict:
    """Each `DEEP` key's `{value, from}`: its latest setting row (`database`), else its default (`default`).

    A cleared row reads as the default. A row that fails the key's pattern, or a read that fails, is refused with its
    reason rather than run at the default: a setting nobody can read is unknown, not unset.
    """
    from sd_db import writes

    try:
        held = writes.read_settings(connection, list(DEEP))
    except Exception as error:  # noqa: BLE001 - any failed read is unknown, named
        raise ValueError(f"the deep review's settings were not read: {type(error).__name__}: {error}") from None
    out = {}
    for key, (default, _, _) in DEEP.items():
        value = held.get(key)
        if value is None:
            out[key] = {"value": default, "from": "default"}
            continue
        try:
            out[key] = {"value": _deep_value(key, value), "from": "database"}
        except ValueError as problem:
            raise ValueError(f"the setting {problem}; set it again in the repo's Deep review settings") from None
    return out


def _stamp(now: datetime.datetime) -> str:
    return now.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(now: datetime.datetime) -> str:
    return now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, document: dict) -> None:
    """The state file, replaced whole, so a reader never sees half of one."""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)


def _git(directory: Path, *args: str, seconds: int = GIT_SECONDS) -> str | None:
    """One bounded git read; None when it fails, so a caller names what it could not learn."""
    try:
        done = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=seconds, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


# -- step 2: the target ------------------------------------------------------------------------------------------


def _text(body) -> str:
    """An item body's text: a task keeps `{"text": ...}`, an older row plain text."""
    try:
        value = json.loads(body or "")
    except ValueError:
        return body or ""
    return value.get("text") or "" if isinstance(value, dict) else body or ""


def _item_text(state: dict, *, heading: str, extra: list[str]) -> str:
    """The item as the reviewer reads it: its fields, its body and every note but the status changes."""
    row = state["item"]
    notes = [note for note in state["notes"] if note["kind"] != "status_change"]
    lines = [f"# {heading} sd:{row['id']}: {row['title']}", "",
             f"- kind: {row['kind']}; status: {row['status']}; priority: {row.get('priority') or 'none'}; "
             f"due: {row.get('due') or 'none'}",
             f"- repository: {row.get('repo') or 'none'}", *extra, "", "## Body", "",
             _text(row.get("body")).strip() or "(no body)", "", "## Notes", ""]
    for note in notes:
        resolved = " · resolved" if note.get("resolved_at") else ""
        lines += [f"### #{note['id']} · {note['kind']} · {note['timestamp']}{resolved}", "", note["body"].strip(), ""]
    if not notes:
        lines.append("(no notes)")
    return "\n".join(lines).rstrip() + "\n"


def _checkout_of(row: dict, *, fallback: Path | None) -> Path:
    from sd_db import paths, workflow

    if not row.get("repo"):
        if fallback is None:
            raise workflow.WorkflowError(f"item {row['id']} names no repository, so the review has no checkout to read; "
                                         f"set its repo first")
        return fallback
    directory = paths.expand(row["repo"])
    if not directory.is_dir():
        raise workflow.WorkflowError(f"{row['repo']} is not a directory on this machine, so the review has no checkout")
    return directory


def _resolve_task(connection, target) -> dict:
    from sd_db import workflow

    state = workflow.item_state(connection, _item_id(target, "target"))
    row = state["item"]
    checkout = _checkout_of(row, fallback=None)
    design, extra = "none", ["- design document: none"]
    if row.get("path"):
        linked = Path(row["path"]) if Path(row["path"]).is_absolute() else checkout / row["path"]
        if linked.is_file():
            design, extra = str(linked), [f"- design document: {linked}"]
        else:
            extra = [f"- design document: {row['path']}, not found in the checkout"]
    return {"checkout": checkout, "repo": checkout, "label": str(row["id"]), "item": row["id"],
            "input": _item_text(state, heading="Item", extra=extra), "sets": {"DESIGN_PATH": design}}


def _resolve_brief(connection, target) -> dict:
    from sd_db import workflow

    state = workflow.item_state(connection, _item_id(target, "target"))
    row = state["item"]
    if row["kind"] != "report" or row.get("source") != "brief":
        raise workflow.WorkflowError(f"item {row['id']} is not a routine brief; review it as a task")
    checkout = _checkout_of(row, fallback=CHECKOUT)
    text = (f"# Brief sd:{row['id']}: {row['title']}\n\n- filed: {row['created_at']}\n"
            f"- filed as: {row.get('external_id') or 'unknown'}\n\n## Brief\n\n{_text(row.get('body')).strip() or '(empty)'}\n")
    return {"checkout": checkout, "repo": checkout, "label": str(row["id"]), "item": row["id"], "input": text,
            "sets": {}}


def _resolve_piece(connection, target) -> dict:
    from sd_db import writing

    item = _item_id(target, "target")
    brief = writing.adversarial_brief(connection, item)
    root = Path(brief["root"])
    return {"checkout": root, "repo": root, "label": str(item), "item": item, "input": None, "sets": {},
            "result": (root / brief["draft"]).with_name("adversarial.md"), "extra": {"piece": brief["piece"]}}


def _registered(connection, path) -> Path | None:
    from sd_db import paths, repos

    row = repos.row_for(connection, path)
    return paths.expand(row["path"]) if row is not None else None


def _resolve_repo(connection, target) -> dict:
    from sd_db import workflow

    if not isinstance(target, str) or not target:
        raise ValueError("A repo review names a registered repository by its key or path.")
    checkout = _registered(connection, target)
    if checkout is None:
        raise workflow.WorkflowError(f"{target} is not a registered repository (see sd-db repo list)")
    if not checkout.is_dir():
        raise workflow.WorkflowError(f"{target} is not a directory on this machine")
    return {"checkout": checkout, "repo": checkout, "label": checkout.name, "item": None, "input": None,
            "sets": {"REPO_NAME": checkout.name}}


def _resolve_change(connection, target, base, item, body) -> dict:
    from sd_db import paths, workflow

    if not isinstance(target, str) or not target:
        raise ValueError("A change review names a checkout directory: a worktree or a clone.")
    directory = paths.disk(target)
    if not directory.is_absolute() or not directory.is_dir():
        raise workflow.WorkflowError(f"{target} is not a directory on this machine")
    checkout = _git(directory, "rev-parse", "--show-toplevel")
    common = _git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not checkout or not common:
        raise workflow.WorkflowError(f"{target} is not a git checkout, or git could not read it")
    main = Path(common).parent if Path(common).name == ".git" else None
    repo = _registered(connection, main) if main else None
    if repo is None:
        raise workflow.WorkflowError(f"{target} is not a checkout of a registered repository (see sd-db repo list)")
    if base is not None and (not isinstance(base, str) or not REF.fullmatch(base) or base.startswith("-")):
        raise ValueError("A base is a git ref: letters, digits and . _ / -.")
    head = _git(Path(checkout), "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    named = None
    for ref in ([base] if base else ["origin/HEAD", "origin/main", "main"]):
        if _git(Path(checkout), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            named = ref
            break
    if not head or named is None:
        raise workflow.WorkflowError(f"{target} has no {'base ' + base if base else 'origin/HEAD, origin/main or main'} "
                                     f"to compare with, or no commit")
    merge_base = _git(Path(checkout), "merge-base", named, head)
    if not merge_base:
        raise workflow.WorkflowError(f"git found no merge base of {named} and HEAD in {target}")
    if merge_base == head:
        raise workflow.WorkflowError(f"{target} has no commits on top of {named}; there is no change to review")
    owner = None
    if item is not None:
        owner = workflow.item_state(connection, _item_id(item, "item"))["item"]["id"]
    if body is None:
        text = "No pull-request body was given; review the diff and the commits alone."
    else:
        found = paths.disk(body) if isinstance(body, str) and body else None
        if found is None or not found.is_absolute() or not found.is_file():
            raise workflow.WorkflowError(f"the pull-request body {body!r} is not a file on this machine")
        text = found.read_bytes()[:BODY_MAX].decode("utf-8", "replace").strip() or "(empty)"
    branch = _git(Path(checkout), "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    lines = [f"# Change: {branch} in {checkout}", "", f"- checkout: {checkout}", f"- repository: {paths.key(str(repo))}",
             f"- branch: {branch}", f"- head: {head}", f"- base: {named}, merge base {merge_base}",
             f"- item: {'sd:' + str(owner) if owner else 'none'}", "", "## Pull-request body", "", text]
    return {"checkout": Path(checkout), "repo": repo, "label": Path(checkout).name, "item": owner,
            "input": "\n".join(lines) + "\n", "sets": {"BASE": merge_base, "HEAD": head}}


def _pull_number(row) -> int | None:
    """A shadow row's pull-request number: its column, else its URL's last part."""
    if type(row["number"]) is int:
        return row["number"]
    tail = (row["url"] or "").rpartition("/")[2]
    return int(tail) if tail.isdigit() else None


def _resolve_pull(connection, target, number, base, item) -> dict:
    """An open pull request of a registered repository, as a change: its head from origin, its base as a change's."""
    from sd_db import paths, progress, repos, workflow

    if type(number) is not int or not 1 <= number <= 10_000_000:
        raise ValueError("A pull request is its number.")
    plan = _resolve_repo(connection, target)
    checkout = plan["checkout"]
    found = repos.parse_remote(repos.row_for(connection, target)["remote"])
    if found.kind != "github":
        raise workflow.WorkflowError(f"{target}'s origin is not on GitHub, so it has no pull requests to review")
    slug = f"{found.owner}/{found.name}"
    # The pull request must be one `sd shadow sync` saw open in this repository: a number alone could name another's.
    pulls = [row for row in progress.tracker_items(connection, tracker="github")
             if (row["kind"] or "") == "pull" and (row["repo"] or "").lower() == slug.lower()
             and _pull_number(row) == number]
    if not pulls:
        raise workflow.WorkflowError(f"pull request #{number} is not open in {slug} as sd shadow sync last saw it")
    pull = pulls[0]
    said = _git(checkout, "ls-remote", "origin", f"refs/pull/{number}/head", seconds=REMOTE_SECONDS)
    head = said.split()[0] if said else None
    if not head or not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise workflow.WorkflowError(f"git ls-remote origin did not name pull request #{number}'s head in {checkout}, "
                                     f"or git could not reach origin")
    if _git(checkout, "cat-file", "-e", f"{head}^{{commit}}") is None:
        raise workflow.WorkflowError(f"pull request #{number}'s head {head[:12]} is not in {checkout}; the dashboard "
                                     f"fetches nothing. Run git -C {checkout} fetch origin pull/{number}/head, then "
                                     f"start the review again")
    if base is not None and (not isinstance(base, str) or not REF.fullmatch(base) or base.startswith("-")):
        raise ValueError("A base is a git ref: letters, digits and . _ / -.")
    named = next((ref for ref in ([base] if base else ["origin/HEAD", "origin/main", "main"])
                  if _git(checkout, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")), None)
    if named is None:
        raise workflow.WorkflowError(f"{target} has no {'base ' + base if base else 'origin/HEAD, origin/main or main'} "
                                     f"to compare with")
    merge_base = _git(checkout, "merge-base", named, head)
    if not merge_base:
        raise workflow.WorkflowError(f"git found no merge base of {named} and pull request #{number} in {target}")
    if merge_base == head:
        raise workflow.WorkflowError(f"pull request #{number} has no commits on top of {named}; there is nothing to "
                                     f"review")
    owner = workflow.item_state(connection, _item_id(item, "item"))["item"]["id"] if item is not None else None
    lines = [f"# Change: pull request #{number} in {slug}", "", f"- checkout: {checkout}",
             f"- repository: {paths.key(str(checkout))}", f"- pull request: {pull['url']}",
             f"- title: {pull['title'] or 'not recorded'}", f"- author: {pull['author'] or 'not recorded'}",
             f"- head: {head}", f"- base: {named}, merge base {merge_base}",
             f"- item: {'sd:' + str(owner) if owner else 'none'}", "", "## Pull-request body", "",
             "Not read: the dashboard holds the pull request's title and not its body, and calls no GitHub API. "
             "Review the diff and the commits alone; the head may not be checked out, so read files at the head with "
             f"`git show {head}:<path>`."]
    return {"checkout": checkout, "repo": checkout, "label": f"{checkout.name}-pr-{number}", "item": owner,
            "input": "\n".join(lines) + "\n", "sets": {"BASE": merge_base, "HEAD": head}}


def _resolve_source(connection, target, source) -> dict:
    """One ledger row of a registered research checkout and the claims that cite it (sd:5101), read through the
    Research page's own bounded reader. The runner fetches the row's link before codex runs; nothing is fetched here."""
    from sd_db import paths, workflow

    from . import research_screen

    if not isinstance(target, str) or not research_screen.KEY.fullmatch(target):
        raise ValueError("A source review names a research checkout by its key, as the Research page lists it.")
    if not isinstance(source, str) or not 0 < len(source) <= 400 or not source.isprintable():
        raise ValueError("A source review names its ledger row by the row's key: its URL, or id:<ID>.")
    found = research_screen.sources(target, now="")
    if found is None:
        raise workflow.WorkflowError(f"{target} is not a research checkout")
    if found["error"] or not found.get("checkout"):
        raise workflow.WorkflowError(f"the ledger of {target} was not read: {found['error'] or 'no checkout named'}")
    row = next((row for row in found["rows"] if row.get("key") == source), None)
    if row is None:
        raise workflow.WorkflowError(f"{source} is not a row of {target}'s ledger as the Research page reads it")
    checkout = Path(found["checkout"])
    repo = _registered(connection, checkout)
    if repo is None:
        raise workflow.WorkflowError(f"{target} is not a registered repository (see sd-db repo list), and a brief is "
                                     f"filed in one")
    mapped = found.get("claims")
    citing = [claim for claim in (mapped or {}).get("rows", []) if source in claim.get("keys", [])]
    heads = row.get("h") or ["", "", "", ""]
    lines = [f"# Source {row['id']}: {row['title']}", "", f"- checkout: {checkout}",
             f"- repository: {paths.key(str(repo))}", f"- source key: {source}",
             f"- ledger: {row['f']}, section {row.get('sec') or 'none'}",
             f"- grade: {row.get('grade') or (row['id'] if row['id'] in research_screen.GRADES else 'none')}",
             f"- link: {row.get('url') or row.get('file') or 'none'}",
             f"- {heads[2] or 'third column'}: {row.get('c3') or 'empty'}",
             *([f"- {heads[3] or 'last column'}: {row['c4']}"] if row.get("c4") else []), "",
             "## Claims that cite it", ""]
    if mapped is None:
        lines.append("Not known: the claim map was not read.")
    else:
        cited = row.get("cited", len(citing))
        lines.append(f"{cited} claims cite it in {', '.join(mapped.get('dirs', []))}; {len(citing)} are listed"
                     + ("." if len(citing) >= cited else ", the rest are past the reader's print bound."))
        lines += ["", *(f"- {claim['doc']}:{claim['line']} ({claim['s']}): {claim['text']}" for claim in citing)]
    lines += ["", "## The source as fetched", ""]
    label = f"{checkout.name}-{row['id'] if research_screen.is_id(row['id']) else source.split('://')[-1]}"
    return {"checkout": checkout, "repo": repo, "label": label, "item": None, "input": "\n".join(lines) + "\n",
            "sets": {}, "extra": {"source": source, "fetch": row.get("url") or None,
                                                 "file": None if row.get("url") else row.get("file") or None}}


def _item_id(value, name: str) -> int:
    if type(value) is str and re.fullmatch(r"[1-9][0-9]{0,18}", value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise ValueError(f"The {name} is an item id.")
    return value


# -- the POST ----------------------------------------------------------------------------------------------------


def _held_by(handle) -> str:
    handle.seek(0)
    return handle.read().strip() or "unknown"


def _database(connection) -> str:
    """The file the server's connection reads: the runner opens it to file the outcome."""
    for row in connection.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return row[2]
    raise ValueError("The dashboard's database has no file the review's runner could open.")


def _resolve(connection, kind: str, target, base, item, body, pr, source=None) -> dict:
    if kind == "source":
        return _resolve_source(connection, target, source)
    if kind == "task":
        return _resolve_task(connection, target)
    if kind == "brief":
        return _resolve_brief(connection, target)
    if kind == "piece":
        return _resolve_piece(connection, target)
    if kind in ("repo", "deep"):
        return _resolve_repo(connection, target)
    if pr is not None:
        return _resolve_pull(connection, target, pr, base, item)
    return _resolve_change(connection, target, base, item, body)


def _lock(state: Path, kind: str, checkout: Path) -> Path:
    """The lock a run holds: one for every codex review, and one per repository for a deep review."""
    if kind != "deep":
        return state / "lock"
    return state / f"deep-{hashlib.sha256(str(checkout).encode()).hexdigest()[:16]}.lock"


def start(connection, kind: str, target, *, base=None, item=None, body=None, pr=None, source=None,
          now: datetime.datetime | None = None):
    """Steps 1 to 4 above: `(202, run)`, or `(409, {"error"})` when a run holds the lock."""
    import sd_db

    now = now or datetime.datetime.now(datetime.timezone.utc)
    state = state_dir()
    runs = state / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    plan = _resolve(connection, kind, target, base, item, body, pr, source)
    handle = open(_lock(state, kind, plan["checkout"]), "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if kind == "deep":
                return 409, {"error": f"A deep review of {plan['checkout'].name} is already running (run "
                                      f"{_held_by(handle)}); one runs at a time per repository. Wait for it to "
                                      f"finish, then start this one."}
            return 409, {"error": f"An adversarial review is already running (run {_held_by(handle)}); one runs at a "
                                  f"time. Wait for it to finish, then start this one."}
        # Before any state is written: a failed read leaves no run behind, and `finally` releases the lock.
        try:
            model, effort, timeout, extra = run_settings(connection, kind)
        except (sd_db.SdDbError, sqlite3.Error) as unread:
            return 503, {"error": f"The review's model and effort did not read from the workflow database: {unread}"}
        plan["sets"].update(extra)
        label = re.sub(r"[^A-Za-z0-9._-]", "-", plan["label"])[:60].strip("-.") or "x"
        run_id = f"{kind}-{label}-{_stamp(now)}"
        record = runs / f"{run_id}.json"
        if record.exists():
            return 409, {"error": f"Run {run_id} started this second; start it again in a moment."}
        result = plan.get("result") or runs / f"{run_id}.md"
        written = runs / f"{run_id}.input.md" if plan["input"] is not None else None
        sets = {**plan["sets"], **({"INPUT_PATH": str(written)} if written else {})}
        # `render` reads its --set values one per line, so a newline would end one early.
        for key, value in sets.items():
            if "\n" in value:
                raise ValueError(f"The review's {key} holds a newline, which the gate cannot pass: {value!r}")
        database = _database(connection)
        if written:
            written.write_text(plan["input"], encoding="utf-8")
        document = {"id": run_id, "kind": kind, "target": str(target), "label": label, "state": "queued",
                    "reason": None, "repo": str(plan["repo"]), "checkout": str(plan["checkout"]),
                    "result": str(result), "input": str(written) if written else None, "item": plan["item"],
                    "sets": sets, "model": model, "effort": effort, "timeout": timeout, "started": _iso(now),
                    "finished": None, "filed": None, "brief": None, "brief_item": None, "batches": None, "pid": None,
                    "database": database, "library": str(Path(sd_db.__file__).resolve().parent.parent),
                    **plan.get("extra", {})}
        _write(record, document)
        handle.seek(0)
        handle.truncate()
        handle.write(run_id)
        handle.flush()
        from . import runner_environment
        environment = runner_environment()
        # Jev ordering sends each finding's text to a third party; a review of private items keeps it off unless
        # the server was started with the switch set (local-adversarial-gate/README.md, "What leaves the machine").
        environment.setdefault("JEV_ADVERSARIAL_GATE", "0")
        try:
            runner = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", str(record),
                                       str(handle.fileno())],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      pass_fds=(handle.fileno(),), start_new_session=True, env=environment,
                                      cwd=str(state))
        except OSError as problem:
            _write(record, {**document, "state": "failed", "reason": f"the runner did not start: {problem}",
                            "finished": _iso(datetime.datetime.now(datetime.timezone.utc))})
            raise
        # Reaped here when it ends, so a long-lived server keeps no zombie; a restart hands it to launchd instead.
        threading.Thread(target=runner.wait, name=f"adversarial {run_id}", daemon=True).start()
        return 202, public(document)
    finally:
        handle.close()


def request(values: dict):
    """The POST's checks, before the server opens a connection; the start as `action_route` returns one."""
    if set(values) - {"kind", "target", "base", "item", "body", "pr", "source"} or not {"kind", "target"} <= set(values):
        raise ValueError("An adversarial review takes a kind and a target, and for a change an optional base, item, "
                         "body and pr; a source review takes its ledger row's source key.")
    kind = values["kind"]
    if kind not in KINDS:
        raise ValueError(f"An adversarial review's kind is one of {', '.join(KINDS)}.")
    if kind == "source":
        if set(values) != {"kind", "target", "source"}:
            raise ValueError("A source review takes a research checkout's key as its target and a ledger row's key "
                             "as its source, and nothing else.")
    elif kind != "change" and set(values) - {"kind", "target"}:
        raise ValueError("Only a change review takes a base, an item, a body or a pr.")
    elif "source" in values:
        raise ValueError("Only a source review takes a source.")
    if "pr" in values and "body" in values:
        raise ValueError("A pull request's review reads no body file; the dashboard holds no pull-request body.")
    return lambda connection: start(connection, kind, values["target"], base=values.get("base"),
                                    item=values.get("item"), body=values.get("body"), pr=values.get("pr"),
                                    source=values.get("source"))


# -- the GET -----------------------------------------------------------------------------------------------------


def public(document: dict) -> dict:
    """What a page reads: the state file less the runner's own fields."""
    return {key: value for key, value in document.items()
            if key not in ("pid", "sets", "database", "library")}


def _alive(pid) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _files(document: dict) -> bool:
    """Whether the run writes to the database: a done run files its brief; a failed one notes its item, if any."""
    return document["state"] == "done" or (document["state"] == "failed" and document.get("item") is not None)


def status(run_id: str, *, now: datetime.datetime | None = None):
    """`(status, document)` for one run; a run whose runner is gone without an outcome reads failed."""
    if not RUN_ID.fullmatch(run_id):
        return 404, {"error": "No such review run."}
    try:
        document = json.loads((state_dir() / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 404, {"error": "No such review run."}
    except (OSError, ValueError) as problem:
        return 503, {"error": f"The state of run {run_id} could not be read: {problem}"}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if document["state"] == "running" and not _alive(document.get("pid")):
        document.update(state="failed", reason=f"the runner (pid {document.get('pid')}) stopped without recording an "
                                               f"outcome; look for {document['result']}")
    elif document["state"] == "queued":
        started = datetime.datetime.fromisoformat(document["started"].replace("Z", "+00:00"))
        if (now - started).total_seconds() > STARTUP:
            document.update(state="failed", reason=f"the runner never started within {STARTUP} s")
    elif _files(document) and document.get("filed") is None and not _alive(document.get("pid")):
        # The outcome was recorded and the runner stopped before it said whether its one transaction committed.
        document["filed"] = (f"not known: the runner stopped before it recorded the filing; look for brief "
                             f"{_stem(document)} on the Briefs page")
    return 200, public(document)


# -- the runner: the outcome in the database ---------------------------------------------------------------------


def _stem(document: dict) -> str:
    """The brief's stem: `<day> - Adversarial review · <kind> <label> · <time>Z`; the Briefs page cuts it at the ` · `."""
    started = datetime.datetime.fromisoformat(document["started"].replace("Z", "+00:00"))
    return f"{started:%Y-%m-%d} - Adversarial review · {document['kind']} {document['label']} · {started:%H%M%S}Z"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _plain(text: str) -> str:
    return " ".join(text.replace("**", "").replace("`", "").split())


def recommendations(result: str, top: int = RECOMMENDATIONS) -> tuple[list[dict], str]:
    """The result's titled hypotheses as recommendations, found as `rank.py` finds them, and its verdict line.

    Each is `{heading, title, why, evidence, repo}`: the title is the `Recommendation:` line, else the heading; `why`
    is the rest of the hypothesis, with a line `sd_db.review` would read as a field quoted; `repo` is the `Repo:`
    line as written, or None. The verdict is the last line, which the last hypothesis holds when nothing follows it.
    """
    spec = importlib.util.spec_from_file_location("adversarial_gate_rank", RANK)
    rank = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rank)
    lines = result.splitlines()
    filled = [number for number, line in enumerate(lines) if line.strip()]
    last = filled[-1] if filled else None
    out = []
    for first, end in rank.finding_blocks(lines)[:top]:
        heading = _cut(_plain(re.sub(r"^#+\s*(?:[0-9]+[.)]\s+)?", "", lines[first])), TITLE_CHARS)
        found = {"heading": heading, "recommendation": None, "evidence": [], "repo": None}
        why = []
        for number in range(first + 1, end):
            if number == last:
                continue
            tagged = TAG.match(lines[number])
            if tagged is None:
                why.append(("> " if RESERVED.match(lines[number]) else "") + lines[number])
                continue
            tag, value = tagged[1].lower(), " ".join(tagged[2].replace("**", "").split())
            if not value:
                continue
            if tag == "evidence" and len(found["evidence"]) < EVIDENCE_LINES:
                found["evidence"].append(_cut(value, EVIDENCE_CHARS))
            elif tag in ("repo", "recommendation") and found[tag] is None:
                found[tag] = value
        text = "\n".join(why).strip().encode("utf-8")
        found["why"] = (text[:WHY_BYTES].decode("utf-8", "ignore").rstrip() + " …") if len(text) > WHY_BYTES \
            else text.decode("utf-8")
        found["title"] = _cut(_plain(found.pop("recommendation") or "") or heading, TITLE_CHARS)
        if RESERVED.match(found["title"]):
            found["title"] = "> " + found["title"]
        out.append(found)
    return out, lines[last].strip() if last is not None else ""


def _repo_of(connection, named: str | None, home: str) -> str:
    """The registered repository a `Repo:` line names, by key, path or last path part; else the review's own."""
    from sd_db import repos

    if not named:
        return home
    value = named.strip("`'\" ")
    row = repos.row_for(connection, value)
    if row is not None:
        return row["path"]
    found = [row["path"] for row in repos.registered(connection) if Path(row["path"]).name == value]
    return found[0] if len(found) == 1 else home


def _question(found: dict, subject: str, stem: str) -> dict:
    """One recommendation as a question: its first line is the title an approval files its task under."""
    text = "\n\n".join(part for part in (found["title"], found["why"], f"From the adversarial review in brief {stem}.")
                       if part)
    return {"subject": subject, "text": text, "evidence": found["evidence"], "options": ["Approve", "Reject"]}


def _brief_text(document: dict, verdict: str, found: list[dict], batches: list[tuple[int, str]]) -> str:
    target = f"{document['kind']} {document['target']}"
    named = ", ".join(f"sd:{item} ({repo})" for item, repo in batches) or "none"
    lines = ["---", "review: adversarial", f"run: {document['id']}", f"target: {target}",
             *([f"source: {document['source']}"] if document.get("source") else []),
             f"model: {document['model']}", f"effort: {document['effort']}",
             f"batches: {', '.join(f'sd:{item}' for item, _ in batches) or 'none'}", "---", "",
             f"# Adversarial review of {target}", "",
             f"- Target: {target}, read in {document['checkout']}",
             f"- Model: {document['model']} at {document['effort']} effort",
             f"- Review batches: {named}; approve a question there to file its task, reject it to drop it",
             f"- Result as {_engine(document)} wrote it: {document['result']}", "", "## Summary", "",
             verdict or "The result ends with no verdict line.", "",
             f"Recommendations: {len(found)}. These are hypotheses for a person to check, not findings.", "",
             "## Recommendations", ""]
    for number, one in enumerate(found, 1):
        lines += [f"### {number}. {one['title']}", ""]
        if one["title"] != one["heading"]:
            lines.append(f"- Hypothesis: {one['heading']}")
        lines.append(f"- Repo: {one['repo']}")
        lines.append(f"- Review batch: sd:{one['batch']}" if one.get("batch") else f"- Not asked: {one['dropped']}")
        lines += [*(f"- Evidence: {line}" for line in one["evidence"]), "", one["why"] or "(no reasoning given)", ""]
    return "\n".join(lines).rstrip() + "\n"


def source_reviews(connection, checkout) -> dict:
    """Each ledger row's source reviews in a research checkout (sd:5101): row key -> its briefs, newest first.

    The association is the brief itself: a source review's brief is filed in the checkout's registered repository with
    a `source: <row key>` line in its front matter, so a ledger edit that keeps the row's key keeps its reviews, and a
    re-run files a brief of its own beside the last. Each is `{item, stem, filed, verdict}`. An unregistered checkout
    raises `LookupError`: its briefs have nowhere to be filed.
    """
    from sd_db import reads, repos, routine_briefs

    owner = repos.row_for(connection, checkout) if checkout else None
    if owner is None:
        raise LookupError(f"{checkout or 'the checkout'} is not a registered repository, so it holds no review briefs")
    out: dict = {}
    # The library's brief list, newest first, filtered here: the dashboard issues no SQL of its own (criterion 12).
    # ponytail: reads the newest BRIEF_SCAN briefs of every repository; give routine_briefs.rows a repo filter if a
    # source review's brief starts to fall past it.
    mine = [row for row in routine_briefs.rows(connection, limit=BRIEF_SCAN)
            if row["repo"] == owner["path"] and " - Adversarial review · source " in (row["title"] or "")]
    for row in mine[:SOURCE_BRIEFS]:
        found = reads.item_by_id(connection, row["id"])
        text = _text(found["body"]) if found is not None else ""
        front = text.split("\n---\n", 1)[0] if text.startswith("---\n") else ""
        key = next((line[len("source: "):] for line in front.splitlines() if line.startswith("source: ")), None)
        if key is None:
            continue
        summary = text.split("\n## Summary\n\n", 1)
        verdict = summary[1].split("\n", 1)[0].strip() if len(summary) == 2 else ""
        out.setdefault(key, []).append({"item": row["id"], "stem": row["title"], "filed": row["created_at"],
                                        "verdict": _cut(verdict, 200)})
    return out


def _file_done(connection, document: dict) -> tuple[dict, str]:
    """A done run's batches, then its brief, which names them: the fields to record and the note's body."""
    from sd_db import repos, review, routine_briefs

    found, verdict = recommendations(Path(document["result"]).read_text(encoding="utf-8", errors="replace"))
    stem, kind, label = _stem(document), document["kind"], document["label"]
    home = repos.row_for(connection, document["repo"])
    if home is None:
        raise ValueError(f"{document['repo']} is no longer a registered repository")
    groups, subjects = {home["path"]: []}, set()
    for one in found:
        one["repo"] = _repo_of(connection, one["repo"], home["path"])
        said, copy = f"{kind} {label}: {one['heading']}", 1
        subject = _cut(said, 190)
        while subject in subjects:  # Two hypotheses with one heading: a batch takes each subject once, within its bound.
            copy += 1
            subject = _cut(said, 190 - len(f" ({copy})")) + f" ({copy})"
        subjects.add(subject)
        one["subject"] = subject
        groups.setdefault(one["repo"], []).append(one)
    started = datetime.datetime.fromisoformat(document["started"].replace("Z", "+00:00"))
    name = f"{kind}-{label[:30]}-{_stamp(started)}"
    batches = []
    for number, (repo, asked) in enumerate([group for group in groups.items() if group[1]], 1):
        filed = review.file(connection, {
            "schema": review.SCHEMA, "queue": QUEUE, "batch": name if number == 1 else f"{name}-{number}",
            "title": f"Adversarial review of {kind} {label}: pick the recommendations to file as tasks",
            "repo": repo, "source": f"local-adversarial-gate; brief {stem}", "mode": "open",
            "context": {"purpose": f"Pick which recommendations of the adversarial review of {kind} {label[:200]} "
                                   f"become tasks.",
                        "rubric": ["Approve: the finding holds and is worth a task.",
                                   "Reject: it does not hold, or it is not worth the work."],
                        "feeds": f"An approval files the finding as a task in its repo; brief {stem[:200]} lists "
                                 f"every finding."},
            "questions": [_question(one, one["subject"], stem) for one in asked]}, who=WHO)
        # A subject the queue rejected in the last 90 days is dropped, not asked again.
        dropped = {entry["subject"]: entry["reason"] for entry in filed["dropped"]}
        for one in asked:
            one["batch"] = None if one["subject"] in dropped else filed["item"]
            one["dropped"] = dropped.get(one["subject"])
        if filed["item"] is not None:
            batches.append((filed["item"], repo))
    brief = routine_briefs.add(connection, stem, _brief_text(document, verdict, found, batches),
                               repo=home["path"], who=WHO)
    titles = "; ".join(f"{number}. {one['heading']}" for number, one in enumerate(found[:TOP], 1))
    body = NOTE.format(model=document["model"], effort=document["effort"], stem=stem, brief=brief["item"],
                       count=len(found), batches=", ".join(f"sd:{item}" for item, _ in batches) or "none",
                       titles=titles or "none titled in the result; read the brief")
    return {"brief": stem, "brief_item": brief["item"], "batches": [item for item, _ in batches]}, body


def _file(document: dict) -> str | None:
    """The outcome in the database, in one transaction, so all of it lands or none; None when it landed.

    Any failure is recorded as the reason nothing was written: an `sd_db` refusal, a locked database, a result that
    cannot be read. The runner has no one else to tell.
    """
    if document["library"] not in sys.path:
        sys.path.insert(0, document["library"])
    try:
        import sd_db
        from sd_db import workflow
        from sd_db.database import transaction

        connection = sd_db.connect(document["database"])
    except Exception as problem:  # noqa: BLE001 - see the docstring
        return f"the database did not open: {type(problem).__name__}: {problem}"
    try:
        with transaction(connection):
            fields, body = {}, None
            if document["state"] == "done" and document["kind"] == "piece":
                body = WRITTEN.format(model=document["model"], piece=document["piece"])
            elif document["state"] == "done":
                fields, body = _file_done(connection, document)
            else:
                body = FAILED.format(model=document["model"], effort=document["effort"], reason=document["reason"],
                                     input=document["input"] or document["checkout"])
            if document["item"] is not None:
                workflow.add_item_note(connection, document["item"], kind="comment", body=body, who=WHO)
    except Exception as problem:  # noqa: BLE001 - see the docstring
        return f"{type(problem).__name__}: {problem}"
    finally:
        connection.close()
    document.update(fields)
    return None


# -- the runner --------------------------------------------------------------------------------------------------


class _Text(html.parser.HTMLParser):
    """A page's readable text: its title and the text outside script, style and the like, one block per line."""

    SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self.skipping, self.in_title = [], "", 0, False

    def handle_starttag(self, tag, attrs):
        self.skipping += tag in self.SKIP
        self.in_title = self.in_title or tag == "title"
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        self.skipping -= tag in self.SKIP and self.skipping > 0
        self.in_title = self.in_title and tag != "title"

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skipping:
            self.parts.append(data)


def _fetch(url: str, *, seconds: float = FETCH_SECONDS) -> str:
    """The source at `url` as the input's last section: what was fetched, or why not (sd:5101).

    One GET, read-only, bounded as a whole by `seconds` and by `FETCH_BYTES`. A failure is written as not fetched,
    with its reason: whether the page is still there is then not known, and the lens reads it so. An HTTP error and
    a redirect are written as the answer they are, since a page that moved or is gone says so that way.

    A socket timeout bounds one wait, not the fetch: a server that drips a byte at a time never trips it. So the read
    runs in a thread the caller waits on for `seconds` plus `FETCH_GRACE` at most, and the read loop stops itself at
    `seconds`. A worker still waiting then has its connection shut down, and the fetch is written as not fetched.
    """
    import socket

    if not re.match(r"https?://[^\s/]", url):
        return f"Not fetched: {url} is not an http or https address, so it is not a public page the dashboard reads.\n"
    done, opened = [], []

    def work():
        try:
            done.append(_read(url, seconds, opened))
        except BaseException as error:  # the caller writes any failure as not fetched, never as a dead thread
            done.append(f"Not fetched: {type(error).__name__}: {error}. Whether the page is still there is not known.\n")

    worker = threading.Thread(target=work, name="source fetch", daemon=True)
    worker.start()
    worker.join(seconds + FETCH_GRACE)
    if done:
        return done[0]
    for connection in list(opened):  # wakes a worker blocked on the connection or the headers
        try:
            if connection.sock is not None:
                connection.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    return (f"Not fetched: {url} gave no whole answer within {seconds:g} s. Whether the page is still there is not "
            f"known.\n")


def _opener(opened: list):
    """A urllib opener that keeps each connection it makes in `opened`, so `_fetch` can shut a stalled one down."""
    import urllib.request

    def kept(cls):
        def make(host, **options):
            connection = cls(host, **options)
            opened.append(connection)
            return connection
        return make

    class Http(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(kept(http.client.HTTPConnection), req)

    class Https(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(kept(http.client.HTTPSConnection), req, context=self._context)

    return urllib.request.build_opener(Http, Https)


def _read(url: str, seconds: float, opened: list) -> str:
    """`_fetch`'s worker: the GET, its read loop stopped at `seconds` and `FETCH_BYTES`, and the text it yields."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + seconds
    request = urllib.request.Request(url, headers={"User-Agent": "sd-dashboard-adversarial-review/1 (read-only)",
                                                   "Accept": "text/html, text/plain;q=0.9, */*;q=0.5"})
    try:
        with _opener(opened).open(request, timeout=seconds) as response:
            status, final = response.status, response.geturl()
            kind, charset = response.headers.get_content_type(), response.headers.get_content_charset() or "utf-8"
            chunks, size, cut = [], 0, ""
            while True:
                if time.monotonic() > deadline:
                    cut = f"the read stopped at its {seconds:g} s bound"
                    break
                chunk = response.read1(65536)  # whatever has arrived, so the deadline is checked between drips
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > FETCH_BYTES:
                    cut = f"the read stopped at {FETCH_BYTES} bytes"
                    break
    except urllib.error.HTTPError as error:
        error.close()
        return (f"Fetched {url}: HTTP {error.code} {error.reason}. The server answered with an error, so the text the "
                f"ledger names was not read from it.\n")
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as error:
        return f"Not fetched: {type(error).__name__}: {error}. Whether the page is still there is not known.\n"
    head = [f"Fetched {url}: HTTP {status}, {kind}, {size} bytes" + (f"; {cut}" if cut else "") + "."]
    if final != url:
        head.append(f"It redirected to {final}.")
    body = b"".join(chunks)
    try:
        codecs.lookup(charset)
    except LookupError:  # a charset Python does not know: read it as UTF-8 rather than fail the run
        head.append(f"Its charset {charset} is not known here, so it is read as UTF-8.")
        charset = "utf-8"
    if kind in ("text/html", "application/xhtml+xml"):
        parser = _Text()
        parser.feed(body.decode(charset, "replace"))
        parser.close()
        text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if parser.title.strip():
            head.append(f"Its title: {' '.join(parser.title.split())}.")
    elif kind.startswith("text/") or kind in ("application/json", "application/xml"):
        text = body.decode(charset, "replace")
    else:
        return "\n".join(head) + f"\nIts {kind} text is not extracted here, so the claims are unchecked against it.\n"
    if len(text) > FETCH_CHARS:
        text = text[:FETCH_CHARS]
        head.append(f"Its text is cut at {FETCH_CHARS} characters.")
    return "\n".join(head) + "\n\n" + (text.strip() or "(no text)") + "\n"


def _engine(document: dict) -> str:
    return "claude" if document["kind"] == "deep" else "codex"


def _reason(log: str, code: int, document: dict) -> str:
    if code == 124:
        return f"{_engine(document)} did not answer within {document['timeout']} s; its process group was killed"
    said = [line.strip() for line in log.splitlines() if line.strip() and not line.startswith("# adversarial gate:")]
    return f"{said[-1]} (exit {code})" if said else f"the gate exited {code} with no message"


def _piece(document: dict) -> str | None:
    """A piece's review, as `sd writing adversarial` takes it, on the server's database; None when the report landed."""
    for folder in (str(Path(document["library"]).parent / "bin"), document["library"]):
        if folder not in sys.path:
            sys.path.insert(0, folder)
    try:
        import argparse

        import sd_db
        import sd_writing
        from sd_db import writing

        connection = sd_db.connect(document["database"])
    except Exception as problem:  # noqa: BLE001 - the runner has no one else to tell
        return f"the review did not start: {type(problem).__name__}: {problem}"
    try:
        sd_writing.adversarial(writing, connection, document["item"],
                               argparse.Namespace(model=document["model"], effort=document["effort"],
                                                  timeout=document["timeout"]))
    except Exception as problem:  # noqa: BLE001 - a refusal is the run's reason
        return str(problem) or type(problem).__name__
    finally:
        connection.close()
    return None


def run(record: Path, lock: int | None = None) -> int:
    """The runner: the gate, its outcome and the filing; the exit code is 0 for a done run and 1 for a failed one.

    `lock` is the lock's descriptor, handed on to the gate, so the lock outlives a runner that dies before codex.
    """
    document = json.loads(record.read_text(encoding="utf-8"))
    document.update(state="running", pid=os.getpid())
    _write(record, document)
    reason = _piece(document) if document["kind"] == "piece" else _gate(document, record, lock)
    finished = _iso(datetime.datetime.now(datetime.timezone.utc))
    document.update(state="failed" if reason else "done", reason=reason, finished=finished)
    if _files(document):
        _write(record, document)
        problem = _file(document)
        document["filed"] = "written" if problem is None else f"not written: {problem}"
    # The gate has exited, so this is the lock's last descriptor: it goes before the write a page reads as settled,
    # so a review started at once finds the lock free.
    if lock is not None:
        os.close(lock)
    _write(record, document)
    return 1 if reason else 0


def _gate(document: dict, record: Path, lock: int | None) -> str | None:
    """`adversarial-gate.sh` on the run, bounded; the reason it failed, or None when it wrote its result."""
    # A deep review is the gate's `deep`, which runs claude on `lenses/deep.md`; every other kind is codex's `run`.
    if document["kind"] == "source":
        # The source as the input's last section, fetched here so codex's sandbox needs no network (sd:5101).
        with open(document["input"], "a", encoding="utf-8") as sink:
            # A file the ledger links (sd:5112) is read where it lies: codex reads the checkout, and nothing else.
            sink.write(_fetch(document["fetch"]) if document.get("fetch") else
                       f"Not fetched: the source is the file {document['file']} the ledger links. Read it in the "
                       f"checkout; a file outside the checkout is not readable here, so its claims are unchecked.\n"
                       if document.get("file") else
                       "Not fetched: the ledger row has no link, so the claims are unchecked against the source.\n")
    argv = ["sh", str(GATE), "deep" if document["kind"] == "deep" else "run", "--lens", document["kind"]]
    for key, value in document["sets"].items():
        argv += ["--set", f"{key}={value}"]
    argv += ["--repo", document["checkout"], "--out", document["result"], "--model", document["model"],
             "--effort", document["effort"], "--timeout", str(document["timeout"])]
    log = record.with_suffix(".log")
    with open(log, "w", encoding="utf-8") as sink:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                                 start_new_session=True, pass_fds=(lock,) if lock is not None else ())
        try:
            code = child.wait(timeout=document["timeout"] + GRACE)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:  # EPERM: macOS for an exiting group, or a sandbox (sd:7900)
                child.kill()
            child.wait()
            code = None
    said = log.read_text(encoding="utf-8", errors="replace")
    result = Path(document["result"])
    if code is None:
        reason = f"the gate did not end within {document['timeout'] + GRACE} s; its process group was killed"
    elif code != 0:
        reason = _reason(said, code, document)
    elif not result.is_file() or not result.read_text(encoding="utf-8", errors="replace").strip():
        reason = f"{_engine(document)} exited 0 but wrote no result"
    else:
        reason = None
    return reason


if __name__ == "__main__":
    # `run <state file> <lock descriptor>`: the descriptor stays open, unread, until this process and the gate exit.
    if len(sys.argv) != 4 or sys.argv[1] != "run":
        sys.exit("usage: adversarial.py run <state file> <lock descriptor>")
    sys.exit(run(Path(sys.argv[2]), int(sys.argv[3])))
