"""`sd fleet stamp`: lay the auto-merge fleet's shared files, one repository at a time.

Every repository whose `repo.runner_merge` row says `auto` is merged by the
runner through `sd-ship`, and `sd-ship merge` under a declared gap needs four
things the repository itself carries (sd:1326, decision D5 on sd:1334):

  * the review-route workflow at the pack's current pin, and its Dependabot
    guard -- the same text `sd-review setup-github` writes, from
    `bin/sd_setup_github.py`, not a second copy;
  * a check workflow, because `every_check` refuses a head nothing validated
    and a repository with no `pull_request` workflow has nothing to run;
  * the `unprotected` entry in the repo row's `status` declaration, in the
    plain "no protection by decision" form, only where GitHub positively
    reports the default branch unprotected (see `branch_protection`). The
    stamp writes no row: it prints the `sd-db repo declare` command (sd:5109);
  * the `CLAUDE.local.md` block, rendered by `bin/sd_install.py`, and
    `docs/dashboard/` ignored and present for generated HTML;
  * the Claude Code settings baseline, `SECRET_READ_DENY` (sd:1661), per
    repository class: an owned or co-owned repository carries it in a
    tracked `.claude/settings.json`, and a guest one, which takes no tracked
    file of ours, in the untracked `.claude/settings.local.json`.

The verb is idempotent: it renders what each file should hold and diffs that
against what the file holds, so a second run over a stamped repository finds
nothing, and the same run after a pin moves is the pin bump. `--dry-run`
prints the diff per repository and writes nothing anywhere.

A dry run walks the auto rows read-only. Tracked files are read at each
checkout's `origin/HEAD` -- the branch a stamp pull request would target, not
whatever the operator has checked out -- and the untracked two
(`CLAUDE.local.md`, the `docs/dashboard/` directory) from the checkout itself.
Nothing here fetches: a stale `origin/HEAD` gives a stale diff, and the header
names the commit it read so the reader can tell.

A write takes no path (R10-D6): it stamps the checkout the caller stands in,
which must be a checkout of an auto repository. Tracked files are written only
on a feature branch; on the default branch the write lays the untracked files
and says the rest needs a worktree.

Employer repositories -- any owner not in `configured_owners` -- are adapted, never
changed in their settings: they keep their protection, so they get no
`unprotected` declaration, and the dry run says so rather than going quiet.

A repository whose `repo.ci` row says `local` gets no workflow at all
(sd:1843): neither the route workflow and its guard nor the check workflow.
`sd-ship merge` runs `sd-check` at the head and posts `sd/local-gate` in their
place, and the plan says so as an adapted line rather than going quiet.
A route workflow still tracked there is named with `sd-review setup-github
--remove`; the stamp lays files and deletes none.

A repository declines a stamped file once, in a tracked `.github/sd-fleet.json`
holding `{"exempt": [<path>, ...]}` (sd:1797). The stamp proposes no exempt
path and says so as an adapted line, so a declined file is not proposed on
every run. A path must be one the stamp writes, and a file that does not read
refuses the repository rather than guessing which files were declined. The
fleet audit is `--dry-run`, so it honours the same list.

An owned repository gets the declaration only where GitHub says, live, that
its default branch has no protection (sd:1655). Before, every owned auto repo
was assumed unprotected, and the dry run proposed the declaration in
repositories whose `main` a ruleset or classic protection guards -- some of
which had deleted the file on purpose. Protection is read both ways GitHub
offers it, through `sd_protection`, the reader `sd-ship` and `sd-status`
share, and the answer takes one of the three states `sd_db.protection` files
per repository: `protected`, `unprotected`, `unknown`. Only `unprotected`
lays the entry; `unknown` never does, and the summary names it.
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import json
import pathlib
import re
import shlex
import sys
from typing import Any, Callable, Iterable
from urllib.parse import quote

import sd_lib
import sd_protection
import sd_setup_github
import sd_setup_guard

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3

#: The owners whose repositories the operator owns outright, when
#: `sd.fleet_owners` names none: the system collector's default,
#: `sd_db.protection.BASELINE_OWNERS`, so the stamp and `sd-status` agree with
#: the dashboard. Any other owner is an employer's, and its protection stands
#: (sd:1334). Read through `configured_owners` (sd:2502).
DEFAULT_OWNERS = ("platypeeps",)
#: A GitHub login: letters, digits and single inner hyphens.
LOGIN = re.compile(r"[A-Za-z0-9](?:-?[A-Za-z0-9])*")
#: The pack's own remote. The repository is `platypeeps/sd-workflow` since
#: sd:6027; a clone made before then still names the old slug, which GitHub
#: redirects, so a remote reads as the pack under either.
PACK_SLUGS = sd_setup_guard.ACTION_SLUGS

ROUTE_PATH = str(sd_setup_github.WORKFLOW_RELATIVE_PATH)
DEPENDABOT_PATH = str(sd_setup_guard.DEPENDABOT_RELATIVE_PATH)
CHECK_PATH = ".github/workflows/sd-check.yml"
#: How the plan names the repo row's `status` declaration (sd:5109).
STATUS_DECLARATION = "status declaration"
#: The file the declaration lived in before migration 37; an exempt list that still names it is ignored.
RETIRED_EXEMPT = (".github/sd-status.json",)
GITIGNORE_PATH = ".gitignore"
LOCAL_BLOCK_PATH = "CLAUDE.local.md"
DASHBOARD_DIR = "docs/dashboard"
FLEET_PATH = ".github/sd-fleet.json"
SETTINGS_PATH = ".claude/settings.json"
LOCAL_SETTINGS_PATH = ".claude/settings.local.json"
#: Every path the stamp writes, so every path `.github/sd-fleet.json` may exempt.
EXEMPTABLE = (ROUTE_PATH, DEPENDABOT_PATH, CHECK_PATH, GITIGNORE_PATH, SETTINGS_PATH,
              LOCAL_SETTINGS_PATH, LOCAL_BLOCK_PATH, DASHBOARD_DIR)

#: The Claude Code settings baseline (sd:1661, ruling #6991): reads an agent
#: must not make, in every repository class. Deny rules only, so the file
#: grants nothing a collaborator's session did not already have. Named files
#: rather than `.env.*`, because a committed `.env.example` is documentation an
#: agent has to read. Each rule is anchored: `//` at the filesystem root, so a
#: file-name rule covers the project and every file outside it (`~/.netrc`, a
#: config folder's `.env`), and `~/` at home. A bare `**/` binds at the
#: session's current directory, and a single `/` at the project (sd:2982), so
#: neither reaches a secret outside the checkout. The rules bind Claude Code's
#: file tools; a shell command can still read the file, which the operator's
#: sandbox settles.
SECRET_READ_DENY = (
    "Read(//**/.env)",
    "Read(//**/.env.local)",
    "Read(//**/.env.*.local)",
    "Read(//**/.env.development)",
    "Read(//**/.env.staging)",
    "Read(//**/.env.production)",
    "Read(//**/secrets/**)",
    "Read(//**/*.key)",
    "Read(//**/*-key.pem)",
    "Read(//**/*.p12)",
    "Read(//**/*.pfx)",
    "Read(//**/id_rsa)",
    "Read(//**/id_ecdsa)",
    "Read(//**/id_ed25519)",
    "Read(//**/.netrc)",
    "Read(//**/.pypirc)",
    "Read(~/.ssh/**)",
    "Read(~/.aws/credentials)",
    "Read(~/.config/gh/hosts.yml)",
)
#: Each project-anchored rule the stamp laid before sd:2982, mapped to the rule
#: that replaced it. The stamp rewrites one in place, so a repository stamped
#: before then reads as drifted and its file keeps no rule that misses `~/.netrc`.
RETIRED_READ_DENY = {rule.replace("Read(//", "Read(/", 1): rule
                     for rule in SECRET_READ_DENY if rule.startswith("Read(//")}
DASHBOARD_IGNORES = frozenset({
    "docs/dashboard", "docs/dashboard/", "/docs/dashboard", "/docs/dashboard/",
    "docs/dashboard/*", "/docs/dashboard/*",
})

#: The one `unprotected` acceptance the fleet carries. Plain on purpose: a
#: `because` that cites a ruleset goes false when the ruleset is deleted, which
#: is how example-website's came to name one D2 had removed (sd:1326 note).
UNPROTECTED_ENTRY: dict[str, Any] = {
    "id": "unprotected",
    "state": {"branch_protection": False},
    "because": (
        "No protection by decision 2026-09-22 (sd:1334 D2). One operator owns this repository; "
        "sd-ship merge requires every check run, every status and a pull_request run of every "
        "workflow at the reviewed head in place of a required-checks list."
    ),
    "since": "2026-09-22",
    "until": "a second account with push or merge rights on this repository exists",
}


def check_workflow_text() -> str:
    """The check workflow a repository with no `pull_request` workflow gets.

    The floor, and it says so: it proves a `pull_request` run happened at the
    head and that the diff carries no whitespace error or conflict marker. It
    runs no repository's tests, because a stamp that guessed a toolchain would
    go red on every repository it guessed wrong. A repository with its own
    `pull_request` workflow never gets this one.
    """

    return f"""\
# Laid by `sd fleet stamp` (sd-workflow) in a repository that had no
# pull_request workflow. `sd-ship merge` under a declared gap requires a
# successful pull_request run of every workflow at the head, and refuses a head
# nothing validated; this is the run.
#
# What it checks: the pull request's diff against its base has no whitespace
# error and no conflict marker (`git diff --check`). It runs none of this
# repository's own tests. Add a workflow that does, and this one can go.
name: sd check

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read

concurrency:
  group: sd-check-${{{{ github.event.pull_request.number }}}}
  cancel-in-progress: true

jobs:
  check:
    name: check
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Check out the pull request
        uses: {sd_setup_github.CHECKOUT_ACTION} # {sd_setup_github.CHECKOUT_VERSION}
        with:
          ref: refs/pull/${{{{ github.event.pull_request.number }}}}/head
          fetch-depth: 0
          persist-credentials: false
{sd_setup_guard.HEAD_CHECK_STEP}      - name: The diff has no whitespace errors or conflict markers
        env:
          BASE_REF: ${{{{ github.base_ref }}}}
        run: git diff --check "origin/${{BASE_REF}}...HEAD"
"""


#: The states a repository's protection takes, as `sd_db.protection` names them.
PROTECTED, UNPROTECTED, UNKNOWN = "protected", "unprotected", "unknown"

#: The repository, as `gh api` fills it in from the checkout's origin.
REPO_PREFIX = sd_lib.REPOSITORY_QUERY


@dataclasses.dataclass(frozen=True)
class Protection:
    """What GitHub said about the default branch: a state, and why."""

    state: str
    reason: str


def asked_fetch(ask: sd_lib.Asker, root: pathlib.Path) -> sd_protection.Fetch:
    """`ask` in the `(status, body)` shape `sd_protection` reads.

    `gh api` ends its error line with `(HTTP nnn)`; the status is lifted
    back out so a 404 reads apart from a 403. An error with no status -- no
    `gh`, no token, no network -- is status 0, which nothing reads as an
    answer.
    """

    def answered(path: str) -> tuple[int, Any]:
        payload, error = ask(path, root)
        if not error:
            return 200, payload
        match = re.search(r"^(?:gh: )?(.*?)\s*\(HTTP (\d{3})\)$", error)
        if match:
            return int(match[2]), {"message": match[1]}
        return 0, {"message": error}

    return answered


def _said(status: int, body: Any) -> str:
    message = body.get("message") if isinstance(body, dict) else None
    return f"{message or 'no message'}" + (f" (HTTP {status})" if status else "")


def branch_protection(root: pathlib.Path, *, ask: sd_lib.Asker = sd_lib.gh_api) -> Protection:
    """Whether the default branch of `root`'s origin is protected, read live.

    `unprotected` only on a positive answer from both mechanisms: classic
    protection answers 404 to an administrator (to anyone else GitHub answers
    404 whether or not the branch is protected), or 403 saying the plan does
    not offer it; and the branch's rules read as a whole, with no active
    ruleset rule that gates a merge -- the same test `sd-ship`'s gate applies
    before it honours the declaration. A classic object, or a gating
    ruleset, is `protected`. The rules are read on a non-admin's 404 too, as
    `sd_db.protection` reads them: the rules endpoint answers without admin,
    so a ruleset settles what the hidden classic answer cannot. Every other
    answer, a failed read included, is `unknown`, never `unprotected`.
    """
    fetch = asked_fetch(ask, root)
    status, repo = fetch(REPO_PREFIX)
    branch = repo.get("default_branch") if status == 200 and isinstance(repo, dict) else None
    if not isinstance(branch, str) or not branch:
        return Protection(UNKNOWN, f"the remote did not name its default branch: {_said(status, repo)}")
    rights = repo.get("permissions")
    admin = isinstance(rights, dict) and rights.get("admin") is True

    status, classic = fetch(f"{REPO_PREFIX}/branches/{quote(branch, safe='')}/protection")
    if status == 200:
        if isinstance(classic, dict):
            return Protection(PROTECTED, f"GitHub reports {branch} protected by classic branch protection")
        return Protection(UNKNOWN, f"classic protection on {branch} answered something other than an object")
    plan_limited = status == 403 and sd_protection.plan_limited(_said(status, classic))
    if status != 404 and not plan_limited:
        return Protection(UNKNOWN, f"classic protection on {branch} could not be read: {_said(status, classic)}")

    read = sd_protection.read_rulesets(fetch, REPO_PREFIX, branch)
    if read["error"]:
        return Protection(UNKNOWN, f"the rulesets on {branch} could not be read: {read['error']}")
    synthesized = sd_protection.synthesize(read["rules"], read["rulesets"])
    if synthesized is not None:
        names = ", ".join(f"ruleset {entry['name']} (#{entry['id']})" for entry in synthesized["rulesets"])
        return Protection(PROTECTED, f"GitHub reports {branch} protected by {names}")
    if status == 404 and not admin:
        return Protection(UNKNOWN, f"classic protection on {branch} is hidden from a token without admin, "
                                   "and no active ruleset gates a merge")
    classic_word = "the plan offers no classic protection" if plan_limited else "no classic protection"
    return Protection(UNPROTECTED, f"{branch} has {classic_word} and no active ruleset gates a merge")


class FleetRefusal(Exception):
    """The invocation cannot go ahead; the message names why."""


@dataclasses.dataclass
class Change:
    path: str
    where: str  # "tracked" (a pull request carries it) or "local" (the checkout's own)
    before: str | None
    after: str
    directory: bool = False

    def diff(self) -> str:
        if self.directory:
            return f"+ directory {self.path}/ (local, untracked)\n"
        return "".join(difflib.unified_diff(
            (self.before or "").splitlines(keepends=True), self.after.splitlines(keepends=True),
            fromfile="/dev/null" if self.before is None else f"a/{self.path}",
            tofile=f"b/{self.path}"))


class Undecodable(UnicodeDecodeError):
    """A file the stamp read that is not UTF-8, naming its path (pack sd:7812).

    A UnicodeDecodeError, so a caller that already refuses one, as
    `settings_change` does, still does; `plan_repo` refuses the rest.
    """

    def __init__(self, path: str, error: UnicodeDecodeError) -> None:
        super().__init__(error.encoding, error.object, error.start, error.end, error.reason)
        self.path = path


class Unread(Exception):
    """A file at the stamped commit that git could not read, naming its path (pack sd:7837).

    Read as missing, it let the plan propose a template over the
    repository's own file; `plan_repo` refuses the repository instead.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


@dataclasses.dataclass
class Plan:
    root: str
    slug: str
    base: str
    changes: list[Change] = dataclasses.field(default_factory=list)
    adapted: list[str] = dataclasses.field(default_factory=list)
    refused: list[str] = dataclasses.field(default_factory=list)
    #: `branch_protection`'s state, or "" where it was not asked.
    protection: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"repo": self.root, "slug": self.slug, "base": self.base, "protection": self.protection or None,
                "changes": [{"path": c.path, "where": c.where,
                             "action": "create" if c.before is None else "update",
                             "diff": c.diff()} for c in self.changes],
                "adapted": self.adapted, "refused": self.refused}


class Tree:
    """Tracked files as one tree holds them: a commit, or a worktree on disk."""

    def __init__(self, root: pathlib.Path, commit: str | None) -> None:
        self.root, self.commit = root, commit

    def text_at(self, rel: str) -> str | None:
        """`rel`'s text, or None when no file is there.

        Undecodable when it is not UTF-8, and Unread when git could not read
        it or could not say whether it is there: neither is a missing file.
        An entry that is no blob, a directory or a submodule's gitlink, is
        no file there (pack sd:7850).
        """
        try:
            if self.commit is None:
                path = self.root / rel
                return path.read_text(encoding="utf-8") if path.is_file() else None
            kind = sd_lib.git_output(["cat-file", "-t", f"{self.commit}:{rel}"], self.root)
            if kind is None:
                # Missing, a gitlink (its commit is not in this repository),
                # or git failed: the entry ls-tree lists, by its type, says which.
                listed = sd_lib.git_output(["ls-tree", "-z", self.commit, "--", rel], self.root)
                if listed is None:
                    raise Unread(rel)
                entry = listed.split("\0", 1)[0]
                if not entry:
                    return None
                kind = entry.split("\t", 1)[0].split(" ")[1]
                if kind == "blob":  # listed as a file, yet cat-file failed
                    raise Unread(rel)
            if kind != "blob":
                return None
            text = _git_show(self.root, f"{self.commit}:{rel}")
            if text is None:
                raise Unread(rel)
            return text
        except UnicodeDecodeError as error:
            raise Undecodable(rel, error) from error

    def workflows(self) -> list[str]:
        """Every workflow path; Unread when git could not list them (pack sd:7850).

        A failed listing is not a folder with no workflows: read as one, the
        plan laid the check workflow beside a workflow it did not see.
        """
        directory = ".github/workflows"
        if self.commit is None:
            found = sorted(p.name for p in (self.root / directory).glob("*") if p.is_file())
        else:
            listing = sd_lib.git_output(["ls-tree", "--name-only", "-z", self.commit, f"{directory}/"],
                                        self.root)
            if listing is None:
                raise Unread(f"{directory}/")
            found = sorted(name.rsplit("/", 1)[-1] for name in listing.split("\0") if name)
        return [f"{directory}/{name}" for name in found if name.endswith((".yml", ".yaml"))]

    def workflow_texts(self, besides: str) -> dict[str, str]:
        """Every workflow but `besides`, by path.

        A read that failed is not a workflow naming no slug: counted as one,
        the stamp lifted the guard that workflow's pin still needs (sd:7633).
        It raises Unread, which refuses the repository (`plan_repo`). A
        listed entry that is no file, such as a gitlink, is no workflow.
        """
        texts = {rel: self.text_at(rel) for rel in self.workflows() if rel != besides}
        return {rel: text for rel, text in texts.items() if text is not None}


#: The unstripped `git show`: file bytes are the diff.
_git_show = sd_lib.git_show


def configured_owners() -> tuple[str, ...]:
    """The owner logins in `sd.fleet_owners`, lower-cased (sd:2502).

    Unset, the machine config's `fleet.owners` list is read with a
    deprecation warning, so a machine set up under sd:2324 keeps its owners;
    absent too, `DEFAULT_OWNERS`. A value that does not read refuses:
    ownership decides the `unprotected` declaration, so a guess in either
    direction is the wrong answer.
    """
    try:
        setting = sd_lib.core_setting("fleet_owners")
    except sd_lib.ConfigError as error:
        raise FleetRefusal(f"cannot read sd.fleet_owners: {error}") from None
    if setting is not None:
        return tuple(login.lower() for login in setting.split(","))
    path = sd_lib.machine_config_path()
    fleet = sd_lib.machine_config(path).get("fleet")
    if fleet is None or (isinstance(fleet, dict) and "owners" not in fleet):
        return DEFAULT_OWNERS
    owners = fleet.get("owners") if isinstance(fleet, dict) else None
    if (not isinstance(owners, list) or not owners
            or not all(isinstance(login, str) and LOGIN.fullmatch(login) for login in owners)):
        raise FleetRefusal(f"fleet.owners in {path} must be a non-empty list of GitHub logins")
    print(f"sd fleet: fleet.owners in {path} is deprecated; run `sd config set sd.fleet_owners "
          f"{','.join(owners)}` and remove it", file=sys.stderr)
    return tuple(login.lower() for login in owners)


def exemptions(tree: "Tree") -> tuple[frozenset[str], str]:
    """The paths `.github/sd-fleet.json` exempts, and `""`; or nothing and the reason it does not read."""
    text = tree.text_at(FLEET_PATH)
    if text is None:
        return frozenset(), ""
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as error:
        return frozenset(), f"not valid JSON ({error})"
    if not isinstance(loaded, dict) or set(loaded) - {"exempt"}:
        return frozenset(), 'must be an object whose one key is "exempt"'
    listed = loaded.get("exempt", [])
    if not isinstance(listed, list) or not all(isinstance(path, str) for path in listed):
        return frozenset(), '"exempt" must be a list of paths'
    unknown = [path for path in listed if path not in EXEMPTABLE + RETIRED_EXEMPT]
    if unknown:
        return frozenset(), f"exempts {', '.join(unknown)}, which the stamp does not write ({', '.join(EXEMPTABLE)})"
    return frozenset(path for path in listed if path in EXEMPTABLE), ""


def owner_slug(remote: str | None) -> str:
    """`owner/name` for a github.com origin, lower-cased; "" for anything else."""
    value = (remote or "").strip()
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if value.lower().startswith(prefix):
            return value[len(prefix):].strip("/").removesuffix(".git").lower()
    return ""


def default_ref(root: pathlib.Path) -> str | None:
    """The commit `origin/HEAD` names, falling back to `origin/main` then `origin/master`."""
    for ref in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/remotes/origin/master"):
        commit = sd_lib.git_output(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], root)
        if commit:
            return commit
    return None


def pack_pin(given: str | None) -> str:
    """The pin consumers get: `--pin`, else the pack checkout's `origin/HEAD` commit.

    Not the pack's `HEAD`, which `sd-review setup-github` uses for one
    repository: a fleet stamp run from a feature branch would pin every
    repository to a commit no consumer can fetch.
    """
    if given:
        return given
    commit = default_ref(sd_setup_github.pack_root())
    if not commit:
        raise FleetRefusal("cannot read the pack checkout's origin/HEAD; pass --pin <sha>")
    return commit


def declares_unprotected(gaps: list[dict[str, Any]]) -> bool:
    """Whether `gaps`, a row's accepted gaps, hold the entry `sd-ship merge` takes as the declared gap."""
    return any(gap.get("id") == "unprotected" and gap.get("state") == {"branch_protection": False} for gap in gaps)


def status_command(root: pathlib.Path, gaps: list[dict[str, Any]]) -> str:
    """The `sd-db repo declare` line that adds `UNPROTECTED_ENTRY` to the row's `gaps`, kept as written."""
    value = json.dumps({"accepted_gaps": [*gaps, dict(UNPROTECTED_ENTRY)]}, ensure_ascii=False)
    return f"sd-db repo declare {shlex.quote(str(root))} status {shlex.quote(value)}"


def settings_text(current: str | None) -> str:
    """Claude Code settings with every `SECRET_READ_DENY` rule in `permissions.deny`.

    Additive only: every other key and rule stays, except that a
    `RETIRED_READ_DENY` rule becomes its replacement in place and a second
    copy of a baseline rule goes. A file that already carries every rule once
    and no retired one comes back byte for byte. Raises ValueError on a file
    that is not a settings object.
    """
    data = {} if current is None else json.loads(current)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    permissions = data.setdefault("permissions", {})
    if not isinstance(permissions, dict) or not isinstance(permissions.setdefault("deny", []), list):
        raise ValueError("permissions.deny is not a list")
    deny = permissions["deny"]
    kept: list = []
    for rule in deny:
        rule = RETIRED_READ_DENY.get(rule, rule) if isinstance(rule, str) else rule
        if rule not in kept or rule not in SECRET_READ_DENY:
            kept.append(rule)
    missing = [rule for rule in SECRET_READ_DENY if rule not in kept]
    if current is not None and kept == deny and not missing:
        return current
    permissions["deny"] = kept + missing
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def settings_change(plan: Plan, path: str, read: Callable[[], str | None], propose: Callable[[str], None]) -> None:
    """Propose the settings baseline at `path`, or refuse a file that does not read.

    The read happens here, inside the refusal: a file that does not decode,
    or cannot be opened, refuses this repository rather than the whole run.
    """
    try:
        after = settings_text(read())
    except (ValueError, OSError) as error:
        plan.refused.append(f"{path}: unreadable ({error}); fix it by hand, then re-run")
    else:
        propose(after)


#: How a repository says it runs no CI. The declaration is structured and
#: sd-ship reads it, so it is the signal; CLAUDE.md's rule line is the
#: second, for a repository that says so there and nowhere else.
NO_CI_DECLARED = re.compile(r"\bforbids (?:adding )?CI\b", re.IGNORECASE)
NO_CI_RULE = re.compile(r"^\s*(?:-\s*)?(?:No CI\b|(?:Do not|Don't) add (?:CI|workflows)\b)", re.IGNORECASE | re.MULTILINE)


def forbids_ci(tree: Tree, gaps: list[dict[str, Any]]) -> str | None:
    """Where `tree`, or the row's accepted `gaps`, says it takes no CI, or None."""
    for gap in gaps:
        if NO_CI_DECLARED.search(str(gap.get("because", ""))):
            return f"{STATUS_DECLARATION} entry {gap.get('id')!r} says CI is forbidden"
    if NO_CI_RULE.search(tree.text_at("CLAUDE.md") or ""):
        return "CLAUDE.md forbids adding CI"
    return None


def additive_block(current: str | None, rendered: str) -> tuple[str, int]:
    """`rendered`'s new block lines added to `current`, with nothing removed.

    Only lines the template inserts inside the pack's markers are taken; a
    line the refresh would change or drop stays as written, and text outside
    the markers is never touched. Returns the text and how many current
    lines the full refresh would have dropped.
    """
    if not current or sd_lib.LOCAL_BLOCK_START not in current:
        return rendered, 0
    old = current.splitlines(keepends=True)
    new = rendered.splitlines(keepends=True)
    begin = next(i for i, line in enumerate(old) if sd_lib.LOCAL_BLOCK_START in line)
    end = next((i for i, line in enumerate(old) if sd_lib.LOCAL_BLOCK_END in line and i > begin), len(old))
    merged: list[str] = []
    kept = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        merged.extend(old[i1:i2])
        if tag == "insert" and begin < i1 <= end:
            merged.extend(new[j1:j2])
        elif tag in ("delete", "replace"):
            kept += i2 - i1
    return "".join(merged), kept


def gitignore_text(current: str | None) -> str:
    """`.gitignore` with `docs/dashboard/` ignored; unchanged when a line already does."""
    text = current or ""
    if any(line.strip() in DASHBOARD_IGNORES for line in text.splitlines()):
        return text
    separator = "" if not text or text.endswith("\n") else "\n"
    return f"{text}{separator}# Generated HTML the local dashboard serves; never committed.\n{DASHBOARD_DIR}/\n"


def pull_request_workflows(tree: Tree, *, besides: Iterable[str]) -> list[str]:
    """Workflows in `tree` that run on `pull_request`, other than the ones named."""
    skip = set(besides)
    found = []
    for path in tree.workflows():
        if path in skip:
            continue
        text = tree.text_at(path)
        if text is None:  # a listed entry that is no file, such as a gitlink
            continue
        if "pull_request" in sd_lib.workflow_triggers(sd_lib.yaml_lines(text)):
            found.append(path)
    return found


def tracked_changes(plan: Plan, tree: Tree, propose: Callable[[str, str], None], *, pin: str,
                    owned: bool, self_install: bool, protection: Protection | None = None,
                    ci: str = "github") -> None:
    """The tracked half of a plan: route, guard, check, declaration, ignore line.

    The row's `status` declaration says whether CI is forbidden and whether the
    gap is declared. A declaration that does not read refuses the tracked half:
    an unknown answer neither forbids CI nor permits proposing it.
    """
    gaps, problems = sd_lib.load_acknowledgements(tree.root)
    if problems:
        plan.refused.append(f"tracked files: the {STATUS_DECLARATION} does not read ({'; '.join(problems)}); "
                            "fix it, then re-run")
        return
    no_ci = forbids_ci(tree, gaps)
    if ci == "local":
        plan.adapted.append(f"workflows: none laid ({ROUTE_PATH}, {DEPENDABOT_PATH}, {CHECK_PATH}); repo.ci is "
                            f"local, so sd-ship merge runs sd-check at the head and posts "
                            f"{sd_lib.LOCAL_GATE_CONTEXT} instead")
        if tree.text_at(ROUTE_PATH) is not None:
            plan.adapted.append(f"{ROUTE_PATH}: still tracked and never runs under repo.ci local; the stamp "
                                "does not delete it, run `sd-review setup-github --remove` there")
    elif no_ci:
        plan.adapted.append(f"workflows: none laid ({ROUTE_PATH}, {DEPENDABOT_PATH}, {CHECK_PATH}); {no_ci}")
    else:
        workflow_changes(plan, tree, propose, pin=pin, self_install=self_install)

    # The declared gap, on owned repositories only, only where GitHub says
    # the branch is unprotected, and only where none is declared.
    if not owned:
        plan.adapted.append(f"{STATUS_DECLARATION}: not declared; {plan.slug or 'this remote'} is an employer's "
                            "repository or others may push to it, so its protection stands")
    elif protection is None or protection.state == UNKNOWN:
        why = protection.reason if protection else "it was not read"
        plan.adapted.append(f"{STATUS_DECLARATION}: not declared; protection is unknown ({why}), and only a "
                            "positive \"unprotected\" from GitHub lays the declaration")
    elif protection.state == PROTECTED:
        plan.adapted.append(f"{STATUS_DECLARATION}: not declared; {protection.reason}")
    elif declares_unprotected(gaps):
        plan.adapted.append(f"{STATUS_DECLARATION}: kept as declared; it already accepts the unprotected gap")
    else:
        # The stamp writes files, not rows (sd:5109): the operator runs the line.
        plan.adapted.append(f"{STATUS_DECLARATION}: GitHub reports the branch unprotected and the row declares no "
                            f"unprotected gap; run: {status_command(tree.root, gaps)}")

    propose(GITIGNORE_PATH, gitignore_text(tree.text_at(GITIGNORE_PATH)))
    settings_change(plan, SETTINGS_PATH, lambda: tree.text_at(SETTINGS_PATH), lambda after: propose(SETTINGS_PATH, after))


def is_route_template(text: str, *, self_install: bool) -> bool:
    """Whether `text` is the route template at some pin, differing at most in the pin."""
    pin = None if self_install else sd_setup_guard.read_pin(text)
    if pin is None and not self_install:
        return False
    return text in sd_setup_guard.known_texts(sd_setup_github.workflow_text(sd_setup_github.action_reference(pin)))


def guard_change(plan: Plan, tree: Tree, propose: Callable[[str, str], None], route: str | None) -> None:
    """The Dependabot guard beside `route`, the route workflow's text after the stamp.

    A guard stays while any workflow names its slug: a refused rewrite leaves
    the old pin, and with it the old guard. A reworded guard, under either
    slug, is refused as setup-github refuses it without --force. A workflow
    the stamp could not read refuses the repository whole (sd:7633, pack
    sd:7850): `workflow_texts` raises Unread.
    """
    others = tree.workflow_texts(besides=ROUTE_PATH)
    current = tree.text_at(DEPENDABOT_PATH)
    differing = sd_setup_guard.differing(current, sd_setup_guard.DEFAULT_ACTION)
    if differing:
        plan.refused.append(f"{DEPENDABOT_PATH}: the guard for {', '.join(differing)} differs from the template, so "
                            "the stamp keeps it; run `sd-review setup-github --force` there to replace it")
        return
    try:
        slugs = sd_setup_guard.pinned_slugs([route or "", *others.values()])
        propose(DEPENDABOT_PATH, sd_setup_guard.rendered(current, slugs=slugs))
    except sd_setup_guard.GuardError as error:
        plan.refused.append(f"{DEPENDABOT_PATH}: {error}")


def workflow_changes(plan: Plan, tree: Tree, propose: Callable[[str, str], None], *, pin: str,
                     self_install: bool) -> None:
    """Route workflow, its Dependabot guard, and the check workflow where none validates."""
    legacy = [rel for rel in sd_setup_github.LEGACY_ROUTER_PATHS if tree.text_at(rel) is not None]
    if legacy:
        plan.refused.append(f"{ROUTE_PATH}: the sd-github-review footprint is still here ({', '.join(legacy)}); "
                            "run `sd-review setup-github --remove-legacy` in this repository first")
    else:
        current = route = tree.text_at(ROUTE_PATH)
        if current is None or is_route_template(current, self_install=self_install):
            route = sd_setup_github.workflow_text(sd_setup_github.action_reference(None if self_install else pin))
            propose(ROUTE_PATH, route)
        else:
            # The stamp moves a pin; it does not take over a file the
            # repository changed. setup-github refuses the same without
            # --force, and a routine stamp must not be the way around that
            # (#1169 review: an added security job was dropped).
            plan.refused.append(f"{ROUTE_PATH}: differs from the template beyond its pin, so the stamp keeps it; "
                                "run `sd-review setup-github --force` there to replace it")
        if not self_install:
            guard_change(plan, tree, propose, route)

    # Check workflow, only where nothing else validates a pull request, and
    # create-only: a file already at the path is the repository's, whatever
    # it holds, so an operator's own jobs there are never replaced. The one
    # exception is the text the stamp laid before the head check (sd:1818):
    # byte for byte the pack's, so moving it forward replaces nobody's job.
    others = pull_request_workflows(tree, besides=(ROUTE_PATH, CHECK_PATH))
    current = tree.text_at(CHECK_PATH)
    if current is not None and current in sd_setup_guard.known_texts(check_workflow_text()):
        propose(CHECK_PATH, check_workflow_text())
    elif current is not None:
        plan.adapted.append(f"{CHECK_PATH}: kept as written; the stamp only creates it")
    elif others:
        plan.adapted.append(f"{CHECK_PATH}: not laid; {', '.join(others)} already run on pull_request")
    else:
        propose(CHECK_PATH, check_workflow_text())


def plan_repo(root: pathlib.Path, remote: str | None, **kwargs: Any) -> Plan:
    """`_plan_repo`, with a file that is not UTF-8, or did not read, refusing this repository whole.

    A traceback would stop the run for every repository after it, and a
    plan cut short at the bad read is not a plan to write (pack sd:7812).
    """
    try:
        return _plan_repo(root, remote, **kwargs)
    except (Undecodable, Unread) as error:
        slug = owner_slug(remote)
        tree = kwargs["tree"]
        plan = Plan(root=str(root), slug=slug or (remote or ""), base=tree.commit or f"worktree {tree.root}")
        why = f"not UTF-8 ({error.reason})" if isinstance(error, Undecodable) else f"could not read it at {tree.commit}"
        plan.refused.append(f"{error.path}: {why}; the stamp leaves this repository as it is")
        return plan


def _plan_repo(root: pathlib.Path, remote: str | None, *, pin: str, tree: Tree,
               local_block: Callable[[str], str], tracked: Callable[[pathlib.Path, str], bool],
               ask: sd_lib.Asker = sd_lib.gh_api, ci: str = "github",
               owners: tuple[str, ...] = DEFAULT_OWNERS) -> Plan:
    """What stamping `root` would change, rendered and diffed; writes nothing.

    `local_block` renders the `CLAUDE.local.md` text from the current text,
    and `tracked` answers whether a path is tracked in `root`; both are
    `bin/sd_install.py`'s, handed in so the planner holds no second copy.
    """
    slug = owner_slug(remote)
    plan = Plan(root=str(root), slug=slug or (remote or ""), base=tree.commit or f"worktree {tree.root}")
    owned = slug.split("/", 1)[0] in owners if slug else False
    self_install = slug in PACK_SLUGS

    def propose(path: str, after: str, where: str = "tracked", before: str | None = None) -> None:
        current = tree.text_at(path) if where == "tracked" else before
        if current != after:
            plan.changes.append(Change(path, where, current, after))

    # A guest or minimal repository is stamped with none of the framework's
    # tracked files, so nothing tracked is proposed there at all (a minimal
    # one may still install the routing lane by hand, R10-D5); its
    # untracked block is still the operator's to keep current. Otherwise the
    # remote is asked the three questions, as `setup-github` asks them, and
    # the one no a row can override is sd-ship's: co-ownership (sd:1347).
    # Only a remote that answers `full` -- nobody else may push -- gets the
    # `unprotected` declaration.
    # The settings baseline follows the class: an owned or co-owned
    # repository carries it tracked, from `tracked_changes`; a guest one takes
    # no tracked file, so it gets the untracked local settings instead.
    guest = True
    try:
        mode = sd_lib.written_mode(root)
    except sd_lib.ConfigError as error:
        plan.refused.append(f"tracked files: {LOCAL_BLOCK_PATH} is unreadable ({error})")
        guest = False
    else:
        if mode in sd_lib.SETTLED_MODES:
            plan.refused.append(f"tracked files: the local block says mode {mode}; a {mode} repository "
                                "carries none of the framework's files")
        else:
            answer = sd_lib.remote_permits_full(root, ask=ask)
            if answer.full or sd_lib.coownership_only(answer):
                guest = False
                protection = branch_protection(root, ask=ask) if owned and answer.full else None
                plan.protection = protection.state if protection else ""
                tracked_changes(plan, tree, propose, pin=pin, owned=owned and answer.full,
                                self_install=self_install, protection=protection, ci=ci)
            else:
                plan.refused.append(f"tracked files: {answer.reason}; the stamp writes only where the "
                                    "remote permits full mode, or where co-ownership alone says no")
    if guest:
        guest_settings(plan, root, tracked, propose)

    # The checkout's own, untracked files.
    if tracked(root, LOCAL_BLOCK_PATH):
        plan.refused.append(f"{LOCAL_BLOCK_PATH}: tracked in this repository; the block goes only into an "
                            "untracked file")
    else:
        current = _local_text(root)
        try:
            after, kept = additive_block(current, local_block(current or ""))
            if kept:
                plan.adapted.append(f"{LOCAL_BLOCK_PATH}: kept {kept} line(s) the template would change or drop")
            propose(LOCAL_BLOCK_PATH, after, where="local", before=current)
        except SystemExit as error:  # the installer's refusal for a half-open block
            plan.refused.append(f"{LOCAL_BLOCK_PATH}: {str(error.code).removeprefix('error: ')}")
    if not (root / DASHBOARD_DIR).is_dir():
        plan.changes.append(Change(DASHBOARD_DIR, "local", None, "", directory=True))
    apply_exemptions(plan, tree)
    return plan


def _local_text(root: pathlib.Path) -> str | None:
    """The untracked local block's text, or None; Undecodable when not UTF-8 (pack sd:7812)."""
    target = root / LOCAL_BLOCK_PATH
    try:
        return target.read_text(encoding="utf-8") if target.is_file() else None
    except UnicodeDecodeError as error:
        raise Undecodable(LOCAL_BLOCK_PATH, error) from error


def guest_settings(plan: Plan, root: pathlib.Path, tracked: Callable[[pathlib.Path, str], bool],
                   propose: Callable[..., None]) -> None:
    """The settings baseline in a guest checkout's untracked `.claude/settings.local.json`."""
    if tracked(root, LOCAL_SETTINGS_PATH):
        plan.refused.append(f"{LOCAL_SETTINGS_PATH}: tracked in this repository; the guest baseline goes "
                            "only into an untracked file")
        return
    read = Tree(root, None).text_at
    settings_change(plan, LOCAL_SETTINGS_PATH, lambda: read(LOCAL_SETTINGS_PATH),
                    lambda after: propose(LOCAL_SETTINGS_PATH, after, where="local",
                                          before=read(LOCAL_SETTINGS_PATH)))


def apply_exemptions(plan: Plan, tree: Tree) -> None:
    """Drop what `.github/sd-fleet.json` exempts from `plan`, naming each path; refuse it when it does not read."""
    exempt, unreadable = exemptions(tree)
    if unreadable:
        plan.refused.append(f"{FLEET_PATH}: {unreadable}; nothing is written until it reads")
    for path in sorted(exempt):
        plan.changes = [change for change in plan.changes if change.path != path]
        plan.refused = [line for line in plan.refused if not line.startswith(f"{path}:")]
        plan.adapted.append(f"{path}: exempt by {FLEET_PATH}; not proposed")


def auto_rows() -> list[tuple[pathlib.Path, str | None]]:
    """`(checkout, remote)` for every managed repository row whose `runner_merge` is `auto` (sd:1620)."""
    import sd_handoff_rows  # noqa: PLC0415 - the library is optional until this verb runs

    sd_db = sd_handoff_rows.library()
    from sd_db.repos import policy_merge, registered  # noqa: PLC0415

    connection = sd_handoff_rows.connect(sd_db, write=False)
    try:
        rows = registered(connection)
    finally:
        connection.close()
    return [(sd_lib.repo_disk(row["path"]), row["remote"]) for row in sd_lib.managed_rows(rows)
            if policy_merge(row["declarations"]) == "auto"]


def _installer() -> Any:
    import sd_install  # noqa: PLC0415 - large, and only this verb needs it

    return sd_install


def selected(rows: list[tuple[pathlib.Path, str | None]], wanted: list[str]) -> list[tuple[pathlib.Path, str | None]]:
    """The rows `--only` names by `owner/name`; every row when it names none."""
    if not wanted:
        return rows
    chosen = []
    for name in wanted:
        match = [row for row in rows if owner_slug(row[1]) == name.lower()]
        if not match:
            raise FleetRefusal(f"{name} is not a managed runner_merge=auto repository row; the stamp covers only those")
        chosen += match
    return chosen


def here(rows: list[tuple[pathlib.Path, str | None]], cwd: pathlib.Path) -> tuple[pathlib.Path, str | None, bool]:
    """The checkout a write lands in: the one the caller stands in (R10-D6).

    It must be a checkout of an auto row's repository -- matched by origin,
    so a worktree of a registered checkout qualifies. The checkout's own row
    is read first, path before origin as `sd_ci.ci_step` reads it, and an
    unmanaged one refuses: two checkouts can share an origin, and management
    belongs to the row, not to the origin (the sd:1620 lane review). The third
    value says whether its HEAD is a feature branch, the only place tracked
    files go.
    """
    top = sd_lib.repo_root(cwd)
    if top is None:
        raise FleetRefusal(f"{cwd} is not inside a git repository")
    refusal = sd_lib.unmanaged(top, warn=False)
    if refusal:
        raise FleetRefusal(refusal)
    slug = owner_slug(sd_lib.git_output(["config", "--get", "remote.origin.url"], top))
    match = [row for row in rows if slug and owner_slug(row[1]) == slug]
    if not match:
        raise FleetRefusal(f"{top} is not a checkout of a runner_merge=auto repository; the stamp covers only those")
    branch = sd_lib.git_output(["symbolic-ref", "--quiet", "--short", "HEAD"], top)
    # An unknown default is treated as both names `default_ref` falls back to,
    # so a `master` repository without `origin/HEAD` is not a feature branch.
    named = sd_lib.git_output(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], top)
    defaults = {named.removeprefix("origin/")} if named else {"main", "master"}
    return top, match[0][1], bool(branch) and branch not in defaults


def render_plans(plans: list[Plan], stream: Any, *, dry_run: bool) -> None:
    write = stream.write
    for plan in plans:
        count = len(plan.changes)
        write(f"== {plan.root} ({plan.slug}) at {plan.base[:12]}: "
              f"{count} change{'s' if count != 1 else ''}\n")
        for change in plan.changes:
            write(change.diff())
        for line in plan.adapted:
            write(f"   adapted  {line}\n")
        for line in plan.refused:
            write(f"   refused  {line}\n")
    changing = [plan for plan in plans if plan.changes]
    unknown = sum(1 for plan in plans if plan.protection == UNKNOWN)
    write(f"\nsd fleet stamp{' --dry-run' if dry_run else ''}: {len(plans)} repositories, "
          f"{len(changing)} with changes, {len(plans) - len(changing)} unchanged, "
          f"{sum(len(plan.refused) for plan in plans)} refused"
          + (f", {unknown} protection unknown" if unknown else "") + "\n")
    for plan in plans:
        files = ", ".join(change.path for change in plan.changes) or "none"
        flag = f"  refused: {len(plan.refused)}" if plan.refused else ""
        flag += f"  protection: {UNKNOWN}" if plan.protection == UNKNOWN else ""
        write(f"  {plan.slug or plan.root}: {files}{flag}\n")
    if dry_run:
        write("dry run: nothing was written\n")


def apply(plan: Plan, root: pathlib.Path) -> None:
    """Write one plan into `root`, the checkout the caller stands in."""
    for change in plan.changes:
        target = root / change.path
        if change.directory:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change.after, encoding="utf-8")


Rows = Callable[[], list[tuple[pathlib.Path, str | None]]]
Planner = Callable[[pathlib.Path, str | None, Tree], Plan]


def dry_run_plans(rows: list[tuple[pathlib.Path, str | None]], planned: Planner) -> list[Plan]:
    """Every row read at its `origin/HEAD`; a row with nothing to read is refused, not skipped."""
    plans = []
    for root, remote in rows:
        commit = default_ref(root) if (root / ".git").exists() else None
        if commit is None:
            plan = Plan(root=str(root), slug=owner_slug(remote), base="-")
            plan.refused.append(f"no checkout at {root}" if not (root / ".git").exists()
                                else "no origin/HEAD, origin/main or origin/master to diff against")
            plans.append(plan)
            continue
        plans.append(planned(root, remote, Tree(root, commit)))
    return plans


def write_here(rows: list[tuple[pathlib.Path, str | None]], cwd: pathlib.Path, planned: Planner) -> Plan:
    """Plan and write the checkout the caller stands in; tracked files on a feature branch only."""
    root, remote, feature = here(rows, cwd)
    plan = planned(root, remote, Tree(root, None))
    if not feature:
        held = [change.path for change in plan.changes if change.where == "tracked"]
        plan.changes = [change for change in plan.changes if change.where != "tracked"]
        if held:
            plan.adapted.append(f"tracked files not written ({', '.join(held)}): this checkout is on its "
                                "default branch; run from a worktree on a feature branch")
    if not plan.refused:
        apply(plan, root)
    return plan


def fleet_stamp(args: argparse.Namespace, *, rows: Rows = auto_rows, stream: Any = None,
                cwd: pathlib.Path | None = None, ask: sd_lib.Asker = sd_lib.gh_api,
                ci: Callable[[pathlib.Path], str] = sd_lib.ci_mode) -> int:
    """Plan every selected repository, print the plans, and write only on a write run.

    A dry run walks the auto rows and reads each at its `origin/HEAD`. A
    write acts on the checkout the caller stands in and nowhere else: no
    option names another checkout (R10-D6). On the default branch it writes
    the untracked files only and says the tracked ones need a feature branch.
    """
    stream = stream or sys.stdout
    if not args.dry_run and args.only:
        raise FleetRefusal("--only selects for a dry run; a write stamps the checkout you stand in")
    pin = pack_pin(args.pin)
    installer = _installer()
    owners = configured_owners()

    def planned(root: pathlib.Path, remote: str | None, tree: Tree) -> Plan:
        return plan_repo(root, remote, pin=pin, tree=tree,
                         local_block=lambda text: installer.local_block_text(text)[0],
                         tracked=installer.path_is_tracked, ask=ask, ci=ci(root), owners=owners)

    if args.dry_run:
        plans = dry_run_plans(selected(rows(), args.only), planned)
    else:
        plans = [write_here(rows(), cwd or pathlib.Path.cwd(), planned)]
    if args.json:
        stream.write(json.dumps({"dry_run": bool(args.dry_run), "pin": pin,
                                 "repos": [plan.as_json() for plan in plans]}, indent=2) + "\n")
    else:
        render_plans(plans, stream, dry_run=args.dry_run)
    return EXIT_REFUSED if any(plan.refused for plan in plans) else EXIT_OK


def run_fleet_stamp(args: argparse.Namespace) -> int:
    try:
        return fleet_stamp(args)
    except FleetRefusal as refusal:
        print(f"sd fleet stamp: {refusal}", file=sys.stderr)
        return EXIT_USAGE


def register_fleet(groups: Any) -> None:
    fleet = groups.add_parser("fleet", help="lay the runner_merge=auto fleet's shared files, or bring one repository to the managed standard")
    verbs = fleet.add_subparsers(dest="verb", required=True)
    stamper = verbs.add_parser(
        "stamp",
        help="route and check workflows, the sd-status declaration, the Claude Code settings baseline and the "
             "CLAUDE.local.md block, per auto repo")
    stamper.add_argument("--dry-run", action="store_true",
                         help="print each repository's diff against its origin/HEAD; write nothing")
    stamper.add_argument("--only", action="append", default=[], metavar="OWNER/NAME",
                         help="dry run: one auto repository by its GitHub name (repeatable); default: every one")
    stamper.add_argument("--pin", metavar="SHA", help="pin the route action here instead of the pack's origin/HEAD")
    stamper.add_argument("--json", action="store_true", help="emit one machine-readable object")
    stamper.set_defaults(handler=run_fleet_stamp)
    import sd_standard  # noqa: PLC0415 - it imports this module, so it registers itself here

    sd_standard.register_standard(verbs)
