"""`sd fleet standard`: bring the repository the caller stands in to the managed-repo standard (sd:5610).

The standard is what the managed fleet holds today, read from its rows and
from GitHub (design: system `docs/work/2026-10-21-manage-standard/design.md`):

  * the sd-db row: `managed` yes, `runner_merge` auto, `ci` local, and the
    review declaration's `copilot_review.automatic_deep` false;
  * GitHub, for an owner in `sd.fleet_owners` only: squash commits built from
    PR_TITLE / PR_BODY, rebase merging off, head branches deleted on merge,
    Issues only where `policy.issues` reads or asks, no Wiki or Projects,
    secret scanning and push protection on a public repository, classic
    protection on the default branch (`sd/local-gate` strict, admins
    enforced, a pull request with 0 approvals), and Actions as `sd ci local`
    leaves them;
  * the checkout's untracked fleet files, as `sd fleet stamp` renders them.

Dependabot alerts are read and never written: alerts off is the operator's
kept choice (sd:5611), so it reads `ok` as "off", not as drift.

The run is a dry run unless `--apply` is given. The plan reads everything
first and gives each setting one step: `ok`, `change`, `pending` (a pull
request carries it), `skip` (with the reason) or `unknown` (a read failed;
nothing writes it). Every `change` step's write was decided by its own read,
so a second run finds `ok` where the first wrote. The steps run in order and
each in its own `try`, so one failure stops nothing after it. The row's
`managed` and `runner_merge` come last: the lane takes a repository up once
both say so, and a run that stops part way leaves it outside the lane.

Nothing here deletes: no ruleset is written or removed, no file is removed,
and a classic protection object that exists is changed only through its
sub-object endpoints, never rewritten whole from a read.

`--digest` binds an apply to the preview the operator saw: the plan is
rebuilt, and a different digest refuses with the new plan. The digest covers
the repository's identity (the row's path and remote, the checkout's origin,
GitHub's `full_name`, the default branch), which must name one repository,
and every step's key, state, before and after.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import hashlib
import json
import pathlib
import sys
from typing import Any, Callable
from urllib.parse import quote

import sd_ci
import sd_fleet
import sd_lib

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3

PREFIX = sd_lib.REPOSITORY_QUERY
CONTEXT = sd_lib.LOCAL_GATE_CONTEXT
#: The title of the one sd item that carries a repository's tracked fleet files.
STAMP_TITLE = "Stamp the fleet files"
WHO = "sd fleet standard"
#: The squash commit fields the standard sets (sd:1669).
SQUASH = {"squash_merge_commit_title": "PR_TITLE", "squash_merge_commit_message": "PR_BODY"}
ISSUE_PAGE = 100
#: The row steps that hand the repository to the lane: they run last, and not at all after a failure.
LAST = ("D1", "D2")
#: The body that creates classic protection where none stands: every field the
#: endpoint requires, `restrictions` included.
PROTECTION_BODY = {
    "required_status_checks": {"strict": True, "checks": [{"context": CONTEXT}]},
    "enforce_admins": True,
    "required_pull_request_reviews": {"required_approving_review_count": 0},
    "restrictions": None,
}


class StandardRefusal(Exception):
    """The plan cannot be built or applied; the message names why."""


@dataclasses.dataclass
class Step:
    key: str
    group: str  # "github", "checkout" or "row"
    subject: str
    state: str  # ok, change, pending, skip, unknown
    detail: str
    before: Any = None
    after: Any = None
    write: Callable[[], None] | None = None
    result: str | None = None

    def step_json(self) -> dict[str, Any]:
        out = {"key": self.key, "group": self.group, "subject": self.subject, "state": self.state,
               "detail": self.detail, "before": self.before, "after": self.after}
        return out | ({"result": self.result} if self.result is not None else {})


@dataclasses.dataclass
class Plan:
    path: str
    slug: str
    branch: str
    private: bool
    owned: bool
    managed: bool
    used_before: bool
    identity: dict[str, str]
    steps: list[Step]

    @property
    def plan_digest(self) -> str:
        body = {"identity": self.identity, "steps": [
            [step.key, step.subject, step.state, step.before, step.after] for step in self.steps]}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def plan_json(self, *, applied: bool = False) -> dict[str, Any]:
        return {"path": self.path, "slug": self.slug, "branch": self.branch, "private": self.private,
                "owned": self.owned, "managed": self.managed, "used_before": self.used_before,
                "digest": self.plan_digest, "applied": applied, "steps": [step.step_json() for step in self.steps]}


@dataclasses.dataclass
class Sources:
    """What the plan reads through: GitHub, the database, the stamp planner, the owners."""

    call: sd_ci.Call
    connection: Any
    owners: tuple[str, ...]
    #: `(root, remote) -> sd_fleet.Plan`, the stamp's plan of the checkout as it stands.
    stamp: Callable[[pathlib.Path, str | None], Any]
    #: The checkout's branch, and its default branch's name.
    branches: Callable[[pathlib.Path], tuple[str | None, str | None]]


def _said(status: int, body: Any) -> str:
    return sd_fleet._said(status, body)


def _write(call: sd_ci.Call, method: str, path: str, body: Any, what: str) -> Callable[[], None]:
    def write_setting() -> None:
        status, answer = call(method, path, body)
        if status != 200:
            raise StandardRefusal(f"{what}: {_said(status, answer)}")
    return write_setting


# --- GitHub ------------------------------------------------------------------


def _field(call: sd_ci.Call, key: str, subject: str, repo: dict, want: dict[str, Any]) -> Step:
    before = {name: repo.get(name) for name in want}
    if before == want:
        return Step(key, "github", subject, "ok", ", ".join(f"{k} {v}" for k, v in want.items()), before, want)
    return Step(key, "github", subject, "change", "set " + ", ".join(f"{k} {v}" for k, v in want.items()),
                before, want, _write(call, "PATCH", PREFIX, want, subject))


def open_issues(call: sd_ci.Call) -> int:
    """Open issues, pull requests left out; raises when the list does not read."""
    count = 0
    for page in range(1, 11):
        status, listing = call("GET", f"{PREFIX}/issues?state=open&per_page={ISSUE_PAGE}&page={page}", None)
        if status != 200 or not isinstance(listing, list):
            raise StandardRefusal(f"the open issues could not be listed: {_said(status, listing)}")
        count += sum(1 for entry in listing if isinstance(entry, dict) and "pull_request" not in entry)
        if len(listing) < ISSUE_PAGE:
            return count
    raise StandardRefusal("the open issues ran past ten pages")


def issues_step(call: sd_ci.Call, repo: dict, takes_issues: bool) -> Step:
    want = {"has_issues": takes_issues}
    step = _field(call, "G5", "Issues", repo, want)
    if step.state != "change" or takes_issues:
        return step
    try:
        count = open_issues(call)
    except StandardRefusal as error:
        return Step("G5", "github", "Issues", "unknown", str(error), step.before, want)
    if count:
        return Step("G5", "github", "Issues", "skip", f"{count} open issue(s); convert them to sd items first "
                    "(sd:1670), then turn Issues off", step.before, want)
    return step


def alerts_step(call: sd_ci.Call) -> Step:
    """Read only: alerts off is a kept choice (sd:5611). `gh_call` answers any success as 200."""
    status, body = call("GET", f"{PREFIX}/vulnerability-alerts", None)
    if status == 200:
        return Step("G7", "github", "Dependabot alerts", "ok", "on", "on", "on")
    if status == 404:
        return Step("G7", "github", "Dependabot alerts", "ok", "off: a kept choice (sd:5611); never turned on here",
                    "off", "off")
    return Step("G7", "github", "Dependabot alerts", "unknown", f"not read: {_said(status, body)}")


def scanning_step(call: sd_ci.Call, repo: dict) -> Step:
    if repo.get("private") is True:
        return Step("G8", "github", "Secret scanning", "skip", "private: the standard sets it on public repositories")
    analysis = repo.get("security_and_analysis")
    if not isinstance(analysis, dict):
        return Step("G8", "github", "Secret scanning", "unknown", "the repository answer has no security_and_analysis")
    names = ("secret_scanning", "secret_scanning_push_protection")
    before = {name: (analysis.get(name) or {}).get("status") for name in names}
    want = dict.fromkeys(names, "enabled")
    if before == want:
        return Step("G8", "github", "Secret scanning", "ok", "scanning and push protection enabled", before, want)
    body = {"security_and_analysis": {name: {"status": "enabled"} for name in names}}
    return Step("G8", "github", "Secret scanning", "change", "enable scanning and push protection", before, want,
                _write(call, "PATCH", PREFIX, body, "secret scanning"))


def protection_steps(call: sd_ci.Call, branch: str, settings: str) -> tuple[list[Step], list[dict]]:
    """G1: classic protection alone. Returns the steps and the active rulesets (for G9's guard)."""
    try:
        classic, active = sd_ci.read_protection(call, branch)
    except sd_ci.CiRefusal as error:
        return [Step("G1", "github", f"protection on {branch}", "unknown", str(error))], []
    subject = f"protection on {branch}"
    if active:
        names = ", ".join(f"{ruleset.get('name')} (#{ruleset.get('id')})" for ruleset in active)
        return [Step("G1", "github", subject, "skip", f"a ruleset protects this branch ({names}); the standard is "
                     "classic protection alone; remove the ruleset by hand, then check again")], active
    base = f"{PREFIX}/branches/{quote(branch, safe='')}/protection"
    if classic is None:
        return [Step("G1", "github", subject, "change", f"create classic protection: {CONTEXT} strict, admins "
                     "enforced, a pull request with 0 approvals", "none", "classic",
                     _write(call, "PUT", base, PROTECTION_BODY, f"classic protection on {branch}"))], active
    steps: list[Step] = []
    if classic.get("required_status_checks"):
        found = sd_ci.classic_step(call, branch, classic)
        write = found.write
        steps.append(Step("G1", "github", f"{subject}: required checks", found.state, found.detail,
                          sd_ci.classic_contexts(classic), [CONTEXT], write))
    else:
        steps.append(Step("G1", "github", f"{subject}: required checks", "skip",
                          f"no required checks, and GitHub offers no write for that part alone; add {CONTEXT} "
                          f"as a required check, strict, in the branch settings ({settings})"))
    admins = (classic.get("enforce_admins") or {}).get("enabled") is True
    steps.append(Step("G1", "github", f"{subject}: enforce admins", "ok" if admins else "change",
                      "on" if admins else "turn on", admins, True,
                      None if admins else _write(call, "POST", f"{base}/enforce_admins", None, "enforce admins")))
    reviews = classic.get("required_pull_request_reviews")
    if not isinstance(reviews, dict):
        steps.append(Step("G1", "github", f"{subject}: pull request", "skip",
                          "no pull request requirement, and GitHub offers no write for that part alone; require a "
                          f"pull request with 0 approvals in the branch settings ({settings})"))
    else:
        count = reviews.get("required_approving_review_count") or 0
        steps.append(Step("G1", "github", f"{subject}: approvals", "ok" if count == 0 else "change",
                          "0 required" if count == 0 else f"{count} -> 0: one operator cannot approve their own "
                          "pull request", count, 0,
                          None if count == 0 else _write(call, "PATCH", f"{base}/required_pull_request_reviews",
                                                         {"required_approving_review_count": 0}, "approvals")))
    return steps, active


def standard_actions(call: sd_ci.Call, branch: str, private: bool, active: list[dict]) -> list[Step]:
    """G9, as `sd ci local` decides it, behind the same guard: a check another source requires stops it."""
    try:
        ours = [ruleset for ruleset in active if ruleset.get("source_type", "Repository") == "Repository"]
        stranded = sd_ci.organization_requirements(active, ours) + sd_ci.other_branch_requirements(call, branch, set())
        found = sd_ci.actions_steps(call, private)
    except sd_ci.CiRefusal as error:
        return [Step("G9", "github", "Actions", "unknown", str(error))]
    if stranded:
        return [Step("G9", "github", "Actions", "skip", "these required checks would never report once the "
                     f"workflows are off: {'; '.join(stranded)}; change them to {CONTEXT} first")]
    return [Step("G9", "github", found_step.subject, found_step.state, found_step.detail,
                 write=found_step.write) for found_step in found if found_step.state in ("ok", "change")]


def github_steps(call: sd_ci.Call, repo: dict, takes_issues: bool) -> list[Step]:
    branch = str(repo["default_branch"])
    private = repo.get("private") is True
    steps = [
        _field(call, "G2", "Squash commits", repo, dict(SQUASH)),
        _field(call, "G3", "Rebase merging", repo, {"allow_rebase_merge": False}),
        _field(call, "G4", "Delete head branches", repo, {"delete_branch_on_merge": True}),
        issues_step(call, repo, takes_issues),
        _field(call, "G6", "Wiki and Projects", repo, {"has_wiki": False, "has_projects": False}),
        alerts_step(call),
        scanning_step(call, repo),
    ]
    protection, active = protection_steps(call, branch, f"{repo.get('html_url') or 'the repository'}/settings/branches")
    # G9 waits on G1: workflows go off only once G1 makes sd/local-gate the required check, so a ruleset G1 leaves
    # alone, or protection it could not read, holds them on with their checks still required.
    if active or any(step.state == "unknown" for step in protection):
        return steps + protection + [Step("G9", "github", "Actions", "skip", f"held until protection on {branch} "
                                          "holds (G1); the workflows stay on")]
    return steps + protection + standard_actions(call, branch, private, active)


# --- the checkout -------------------------------------------------------------


def open_stamp_item(connection: Any, path: str) -> int | None:
    sd_lib.import_sd_db()
    from sd_db import paths  # noqa: PLC0415

    # Every stored form of the path, as repos.row_for probes: an item written before 014 holds the absolute one.
    probe = paths.keys(path)
    found = connection.execute(f"SELECT id FROM item WHERE repo IN ({paths.placeholders(probe)}) AND title = ? AND status != 'done' "  # nosec B608 - only ? placeholders are interpolated
                               "ORDER BY id", (*probe, STAMP_TITLE)).fetchone()
    return int(found[0]) if found else None


def checkout_steps(root: pathlib.Path, row: Any, sources: Sources) -> list[Step]:
    try:
        stamped = sources.stamp(root, row["remote"])
    except (sd_fleet.FleetRefusal, OSError, ValueError) as error:
        return [Step("F1", "checkout", "fleet files", "unknown", f"not read: {error}")]
    steps: list[Step] = [Step("F1", "checkout", "fleet files", "skip", reason) for reason in stamped.refused]
    branch, default = sources.branches(root)
    on_default = branch is not None and branch == default
    tracked = [change for change in stamped.changes if change.where == "tracked"]
    for change in stamped.changes:
        if change.where == "tracked":
            continue
        after = "directory" if change.directory else hashlib.sha256(change.after.encode()).hexdigest()[:12]
        if not on_default:
            steps.append(Step("F1", "checkout", change.path, "skip", f"the checkout is on {branch or 'no branch'}, "
                              f"not its default branch {default or '(unknown)'}; nothing is written there",
                              None, after))
            continue
        steps.append(Step("F1", "checkout", change.path, "change",
                          "create" if change.before is None else "update", None, after,
                          functools.partial(sd_fleet.apply, _only(stamped, change), root)))
    if tracked:
        paths = ", ".join(change.path for change in tracked)
        steps.append(Step("F1", "checkout", "tracked fleet files", "pending",
                          f"{paths}: a pull request carries them; run `sd fleet stamp` in a worktree on a feature "
                          "branch", None, paths))
        item = open_stamp_item(sources.connection, row["path"])
        if item is not None:
            steps.append(Step("F1", "checkout", "stamp item", "ok", f"sd:{item} carries them"))
        else:
            steps.append(Step("F1", "checkout", "stamp item", "change", f"file the sd item \"{STAMP_TITLE}\"",
                              None, STAMP_TITLE, lambda: _file_stamp_item(sources.connection, row["path"], paths)))
    if not steps:
        steps.append(Step("F1", "checkout", "fleet files", "ok", "as rendered"))
    return steps


def _only(stamped: Any, change: Any) -> Any:
    return dataclasses.replace(stamped, changes=[change])


def _file_stamp_item(connection: Any, path: str, paths: str) -> None:
    sd_lib.import_sd_db()
    from sd_db import workflow  # noqa: PLC0415

    if open_stamp_item(connection, path) is None:
        workflow.capture_task(connection, title=STAMP_TITLE, repo=path, who=WHO, priority=3,  # sd:6726
                              body=f"`sd fleet standard` found these tracked fleet files out of date: {paths}. "
                                   "Run `sd fleet stamp` in a worktree on a feature branch and ship the result.")


# --- the row ------------------------------------------------------------------


def row_steps(connection: Any, row: Any) -> list[Step]:
    """D4, D3, then D1 and D2 last: the lane takes the repository up only once both are set."""
    sd_lib.import_sd_db()
    from sd_db import repos  # noqa: PLC0415

    path = row["path"]
    steps: list[Step] = []
    try:
        held = repos.declarations(connection, path) or {}
    except Exception as error:  # noqa: BLE001 - a row that does not read is this step's unknown
        steps.append(Step("D4", "row", "review declaration", "unknown", f"not read: {error}"))
    else:
        review = held.get("review")
        copilot = review.get("copilot_review") if isinstance(review, dict) else None
        deep = copilot.get("automatic_deep") if isinstance(copilot, dict) else None
        if review is not None and not isinstance(review, dict):
            steps.append(Step("D4", "row", "review declaration", "skip", "the review declaration is not an object"))
        elif deep is False:
            steps.append(Step("D4", "row", "copilot_review.automatic_deep", "ok", "false", False, False))
        else:
            merged = json.loads(json.dumps(review or {}))
            merged.setdefault("copilot_review", {})
            if not isinstance(merged["copilot_review"], dict):
                merged["copilot_review"] = {}
            merged["copilot_review"]["automatic_deep"] = False
            problems = sd_lib.declaration_problems("review", merged)
            if problems:
                steps.append(Step("D4", "row", "copilot_review.automatic_deep", "skip", "; ".join(problems)))
            else:
                steps.append(Step("D4", "row", "copilot_review.automatic_deep", "change",
                                  "set false, the review object's other keys kept", deep, False,
                                  lambda: repos.set_declaration(connection, path, "review", merged)))
    ci = repos.policy_ci(row["declarations"])  # pack sd:7366: `policy.ci.provider`
    if ci == "jenkins":
        steps.append(Step("D3", "row", "ci", "skip", "the policy declares jenkins checks"))
    else:
        steps.append(Step("D3", "row", "ci", "ok" if ci == "local" else "change", "local", ci, "local",
                          None if ci == "local" else lambda: repos.set_ci(connection, path, "local")))
    managed = bool(row["managed"])
    steps.append(Step("D1", "row", "managed", "ok" if managed else "change", "yes", "yes" if managed else "no", "yes",
                      None if managed else lambda: repos.set_managed(connection, path, "yes", bypass_standard=True)))
    merge = repos.policy_merge(row["declarations"])
    steps.append(Step("D2", "row", "runner_merge", "ok" if merge == "auto" else "change", "auto", merge, "auto",
                      None if merge == "auto" else lambda: repos.set_runner_merge(connection, path, "auto")))
    return steps


def used_before(connection: Any, path: str) -> bool:
    """Lane history for `path`: a runner run, or an item `sd-ship` moved. It picks a default button, nothing more."""
    sd_lib.import_sd_db()
    from sd_db import paths  # noqa: PLC0415

    probe, marks = paths.keys(path), paths.placeholders(paths.keys(path))
    found = connection.execute(
        f"SELECT 1 FROM runner_run WHERE repo IN ({marks}) UNION ALL SELECT 1 FROM note JOIN item ON item.id = note.item "  # nosec B608 - only ? placeholders are interpolated
        f"WHERE item.repo IN ({marks}) AND note.kind = 'status_change' AND note.body LIKE '%by sd-ship%' LIMIT 1",
        (*probe, *probe)).fetchone()
    return found is not None


# --- the plan -----------------------------------------------------------------


def build_plan(root: pathlib.Path, sources: Sources) -> Plan:
    sd_lib.import_sd_db()
    from sd_db import repos  # noqa: PLC0415

    origin = sd_lib.git_output(["config", "--get", "remote.origin.url"], root)
    target = repos.registered_for(sources.connection, str(root.resolve()), origin)
    row = sd_lib.repo_row(sources.connection, target)
    if row is None:
        raise StandardRefusal(f"{target} is not a registered repository; run `bin/sd-db repo add {target}`")
    status, repo = sources.call("GET", PREFIX, None)
    if status != 200 or not isinstance(repo, dict) or not repo.get("default_branch"):
        raise StandardRefusal(f"the repository could not be read: {_said(status, repo)}")
    names = {"row": sd_fleet.owner_slug(row["remote"]), "origin": sd_fleet.owner_slug(origin),
             "github": str(repo.get("full_name") or "").lower()}
    if not names["github"] or len(set(names.values())) != 1:
        raise StandardRefusal("the row's remote, the checkout's origin and GitHub name different repositories ("
                              + ", ".join(f"{k} {v or 'none'}" for k, v in names.items()) + "); fix the row first")
    slug = names["github"]
    owned = slug.split("/", 1)[0] in sources.owners
    if owned and not (isinstance(repo.get("permissions"), dict) and repo["permissions"].get("admin") is True):
        raise StandardRefusal(f"{slug}: this token has no admin on the repository, so its settings cannot be "
                              "changed")
    takes_issues = repos.issues_policy(sources.connection, row["path"]) != "off"
    steps = (github_steps(sources.call, repo, takes_issues) if owned else
             [Step("G", "github", "GitHub settings", "skip", "employer repository: its owner sets them")])
    steps += checkout_steps(root, row, sources) + row_steps(sources.connection, row)
    identity = {"path": str(row["path"]), "remote": str(row["remote"]), "slug": slug,
                "branch": str(repo["default_branch"])}
    return Plan(str(row["path"]), slug, str(repo["default_branch"]), repo.get("private") is True, owned,
                bool(row["managed"]), used_before(sources.connection, str(row["path"])), identity, steps)


def run_steps(plan: Plan) -> int:
    """Run every `change` step in order, each on its own; returns how many failed.

    A failure stops nothing but `LAST`, and G9 after a G1 failure: the repository joins the lane only after every other
    step held, and its workflows go off only once protection requires sd/local-gate. A G1 that was not read holds
    `LAST` too, and each step it holds counts as failed (pack sd:8823).
    """
    failed, failed_keys = 0, set()
    unread = any(step.key == "G1" and step.state == "unknown" for step in plan.steps)
    for step in plan.steps:
        if step.state != "change" or step.write is None:
            continue
        if step.key in LAST and failed:
            step.result = "held: an earlier step failed, so the repository stays outside the lane; apply again"
            continue
        if step.key in LAST and unread:
            failed += 1
            step.result = "held: protection (G1) was not read, so the repository stays outside the lane; apply again"
            continue
        if step.key == "G9" and "G1" in failed_keys:
            step.result = "held: protection (G1) failed, so the workflows stay on while their checks are required; apply again"
            continue
        try:
            step.write()
        except Exception as error:  # noqa: BLE001 - one step's failure is its own, never the run's
            failed += 1
            failed_keys.add(step.key)
            step.result = f"failed: {error}"
        else:
            step.result = "changed"
    return failed


def reread(plan: Plan, fresh: Plan) -> None:
    """After a failure, name each failed step's live state from a fresh read."""
    live = {(step.key, step.subject): step for step in fresh.steps}
    for step in plan.steps:
        now = live.get((step.key, step.subject))
        if step.result and step.result.startswith("failed") and now is not None:
            step.result += f"; live: {now.state} ({now.detail})"


# --- the verb -----------------------------------------------------------------


def _branches(root: pathlib.Path) -> tuple[str | None, str | None]:
    branch = sd_lib.git_output(["symbolic-ref", "--quiet", "--short", "HEAD"], root)
    named = sd_lib.git_output(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], root)
    return branch or None, named.removeprefix("origin/") if named else None


def live_sources(root: pathlib.Path, connection: Any) -> Sources:
    installer = sd_fleet._installer()
    pin = sd_fleet.pack_pin(None)
    owners = sd_fleet.configured_owners()

    def stamp_checkout(checkout: pathlib.Path, remote: str | None) -> Any:
        return sd_fleet.plan_repo(checkout, remote, pin=pin, tree=sd_fleet.Tree(checkout, None),
                                  local_block=lambda text: installer.local_block_text(text)[0],
                                  tracked=installer.path_is_tracked, ci="local", owners=owners)

    return Sources(sd_ci.gh_call(root), connection, owners, stamp_checkout, _branches)


def print_plan(plan: Plan, stream: Any, *, applied: bool) -> None:
    mode = "applied" if applied else "dry run; --apply writes"
    stream.write(f"sd fleet standard: {plan.slug}, default branch {plan.branch} -- {mode}\n")
    for step in plan.steps:
        word = step.result or step.state
        stream.write(f"  {word:<8} {step.key:<3} {step.subject}: {step.detail}\n")
    stream.write(f"digest {plan.plan_digest}\n")


def standard(args: argparse.Namespace, *, sources: Sources | None = None, stream: Any = None,
             root: pathlib.Path | None = None, lock: Callable[[str], Any] | None = None) -> int:
    """Plan, and with `--apply` run, the standard for `root`; `sources` and `lock` are the tests' seams."""
    stream = stream or sys.stdout
    root = root or sd_lib.repo_root(None)
    if root is None:
        raise StandardRefusal("the working directory is not inside a Git repository")
    if sd_lib.import_sd_db().module is None:
        raise StandardRefusal("the sd_db library is not importable")
    from sd_db import ship  # noqa: PLC0415
    from sd_db.database import connect, default_path, refuse_hub_only  # noqa: PLC0415

    if args.digest and not args.apply:
        raise StandardRefusal("--digest binds an apply; give --apply with it")
    database = pathlib.Path(args.database or default_path())
    owned_connection = sources is None
    connection = connect(database) if sources is None else sources.connection
    try:
        sources = sources or live_sources(root, connection)
        plan = build_plan(root, sources)
        failed = 0
        if args.apply:
            refuse_hub_only(connection, "fleet standard")
            # A ship of this repository moves the same settings: none runs while the standard does.
            held = lock(plan.slug) if lock else ship.repository_flock(database, plan.slug, holder={"command": WHO})
            with held:
                plan = build_plan(root, sources)
                if args.digest and args.digest != plan.plan_digest:
                    if args.json:
                        stream.write(json.dumps(plan.plan_json(), indent=2) + "\n")
                    raise StandardRefusal(f"the plan changed since the preview (digest {plan.plan_digest}); "
                                          "read it again and confirm the new one")
                failed = run_steps(plan)
                if failed:
                    reread(plan, build_plan(root, sources))
        if args.json:
            stream.write(json.dumps(plan.plan_json(applied=bool(args.apply)), indent=2) + "\n")
        else:
            print_plan(plan, stream, applied=bool(args.apply))
        return EXIT_FAILED if failed else EXIT_OK
    finally:
        if owned_connection:
            connection.close()


def run_standard(args: argparse.Namespace) -> int:
    try:
        return standard(args)
    except (StandardRefusal, sd_fleet.FleetRefusal, sd_ci.CiRefusal) as refusal:
        print(f"sd fleet standard: {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as error:  # noqa: BLE001 - a library refusal (HubOnly, a held ship lock) names itself
        if type(error).__module__.startswith("sd_db"):
            print(f"sd fleet standard: {error}", file=sys.stderr)
            return EXIT_REFUSED
        raise


def register_standard(verbs: Any) -> None:
    standard_parser = verbs.add_parser(
        "standard",
        help="bring this repository to the managed-repo standard: sd-db row, GitHub settings, fleet files; "
             "dry run by default")
    standard_parser.add_argument("--apply", action="store_true", help="make the changes; without it nothing is written")
    standard_parser.add_argument("--digest", metavar="SHA256",
                                 help="apply only if the plan still has this digest, as a preview printed it")
    standard_parser.add_argument("--json", action="store_true", help="emit one machine-readable object")
    standard_parser.add_argument("--database", type=pathlib.Path, help="the sd database; defaults to the operator HOME")
    standard_parser.set_defaults(handler=run_standard)
