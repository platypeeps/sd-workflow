#!/usr/bin/env python3
"""Register a research repo with the dashboard, and queue its outward mirrors.

The publication contract is
`skills/_shared/references/publication-contract.md`; this is the half of it a
script can carry out. Two jobs, both run at the end of `render`:

1. **Register.** Write a `root|key|label|directory` line to the dashboard's
   `documents.conf` for a directory the dashboard cannot find on disk. The
   file is in the machine's config directory, not the dashboard checkout;
   `dashboard_config` says where. The dashboard finds `docs/dashboard/` by
   itself and takes its name from the workflow database, so that default
   location gets no line. The dashboard reads that file and never writes it,
   so nothing here touches the dashboard's own source.

2. **Enqueue.** For each document designated for a destination in
   `DESTINATIONS`, write a sync request naming the document, its container, the
   page or file to update and the source revision. Rendering runs in a git hook
   and in CI, neither of which can reach an MCP server, and the pack holds no
   credential for any destination -- so a render never calls one and does not
   try. An agent session drains the queue.

One queue for every destination rather than one queue each, and one request
file per document *per destination*: a document designated for two places is
two independent pieces of outstanding work, and either can drain while the
other waits. The filename carries the repo, the document and the destination,
so a re-render overwrites the pending request rather than queueing a second --
and a re-render of an unchanged document writes nothing at all, pending or
drained, because a receipt beside the queue remembers what was last queued.

A request that is never drained stays on disk: the queue does not expire,
because an expiring queue reports a mirror as done that was never written.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote, unquote, urlsplit

from sd_lib import git_output, git_show, import_sd_db, main_worktree_root


def dashboard_config(environ: Mapping[str, str]) -> Path:
    """The directory the dashboard reads `documents.conf` from.

    `<config>/project-dashboard`, `<config>` being `$SYSTEM_TOOLS_CONFIG`, else
    `$XDG_CONFIG_HOME/system`, else `~/.config/system`. This mirrors `root` and
    `config_dir` in the system repository's `lib/system_tools_config.py`, which
    `sd_dashboard/documents.py` reads `documents.conf` through since system
    350553a; change the two together. Mirrored rather than imported: a render
    runs on machines with no system checkout. Before that commit the file sat
    in the dashboard checkout, and looking there reported every listed
    repository as not registered (sd:2010).
    """
    named = environ.get("SYSTEM_TOOLS_CONFIG")
    if named:
        return Path(os.path.expanduser(named)) / "project-dashboard"
    base = environ.get("XDG_CONFIG_HOME") or os.path.join(
        environ.get("HOME") or os.path.expanduser("~"), ".config")
    return Path(os.path.expanduser(base)) / "system" / "project-dashboard"


#: Where the dashboard's per-machine configuration lives, `documents.conf`
#: among it. Read once, when the module loads, as the dashboard reads it; not
#: discovered by searching, because a search that found two would have to
#: guess which one the browser is serving.
DASHBOARD_CONFIG = dashboard_config(os.environ)

#: Durable, outside any repo, and beside the vault-write queue that already
#: works this way. `~/.claude/` and not `/tmp`: a request that a reboot deletes
#: is a mirror nobody knows went missing. One queue for every destination:
#: `bin/sd-status` reads it, and a queue per destination would mean a producer
#: and a status class per destination, which `actionable_inventory` already
#: refuses for the same reason (C-3, one producer and not three).
QUEUE = Path(
    os.environ.get("SD_MIRROR_QUEUE", "~/.claude/pending-mirror-syncs")
).expanduser()

#: What `QUEUE` was called before one queue carried every destination. A
#: machine that rendered between the queue landing and the rename holds
#: requests here, and nothing has read this path since -- so the upgrade left
#: them durable and invisible, which is the one thing a durable queue must
#: never be. `migrate_legacy_queue` empties it; nothing writes it.
LEGACY_QUEUE = Path(
    os.environ.get("SD_NOTION_QUEUE", "~/.claude/pending-notion-syncs")
).expanduser()


#: The vault the Obsidian copy is written into, and the folder inside it that
#: holds briefs. `$OBSIDIAN_VAULT` is deliberately not defaulted here: an unset
#: one is refused rather than guessed, and a second spelling of that path is how
#: a write lands in somebody else's vault layout.
VAULT = os.environ.get("OBSIDIAN_VAULT", "")
BRIEFS = "Briefs"

#: The home whose workflow database an item's published URL is recorded in
#: (sd:3979). `None` is `$HOME`, read at call time as `sd_db` reads it; on a
#: satellite that database is the hub's. A test points it at a fixture.
DATABASE_HOME: Path | None = None

#: How long, in milliseconds, recording a URL waits for the database's lock.
#: Short: the render runs in a git hook, and a record that does not land is
#: retried by the next render or drain.
DATABASE_BOUND = 5000

#: Where the dashboard's HTML is written, relative to the repo. Gitignored:
#: it is generated on every commit, and a generated tree in history turns each
#: render into a diff nobody reads. It replaces `build/`, which was both the
#: rendering scratch space and the published directory -- one of which belongs
#: in the repo's own layout and the other of which is a publication surface.
DASHBOARD_DIR = Path("docs") / "dashboard"


class Destination(NamedTuple):
    """One outward place a document may be designated for.

    The dashboard and Obsidian are deliberately absent: both are local writes a
    render performs itself, so neither takes a queue, a connector or a drain.
    This table is what leaves the machine.

    `what` is optional: absent, the drain creates the page or file and writes
    the id it got back into the designation, which the contract's drain step 4
    requires; present, the drain updates that one rather than creating a second
    copy on every render.
    """

    #: The DOCS key the user writes, and the `destination` field of a request.
    name: str
    #: The field naming the container the mirror is written into.
    where: str
    #: The optional field: the existing page or file to update in place.
    what: str
    #: How a report and a status row name the container, in words.
    noun: str
    #: The container a designation that names none falls back to, as a format
    #: string over the repo name. Every destination has one, so no designation
    #: has to name a container to be valid. Notion resolves its own per scope
    #: in `notion_target`, so its entry here is empty.
    default: str = ""


#: Adding a destination is a row here plus a drain step in the contract. It is
#: deliberately not open-ended: an unknown key in a DOCS entry is a typo, and a
#: table that accepted anything would queue a mirror to a place nothing drains.
DESTINATIONS = (
    # `space` is the key a designation writes, and the label a request carries.
    # The container a Notion drain resolves is `space_id`, set by
    # `notion_target`; this row never resolves one.
    Destination("notion", "space", "page", "Notion folder"),
    Destination("drive", "folder", "file", "Drive folder",
                default=BRIEFS + "/%s"),
)

#: The row a request's `destination` field names. Derived rather than written
#: twice, so a destination added to the table above is reachable from a queued
#: request without anything else being remembered.
BY_NAME = {dest.name: dest for dest in DESTINATIONS}

class NotionScope(NamedTuple):
    """One Notion default destination: the setting that names it, in words.

    The folder itself is neither a name nor an id written here. A name cannot
    be: a lookup that finds nothing returns an empty result rather than an
    error, so a rename would move every default mirror to nowhere and tell no
    one -- and both of this maintainer's folders have been renamed once
    already. An id cannot be either: a page id belongs to one Notion account,
    and a default shipped in this file would send another operator's brief to
    a page they do not own. So the id is the operator's to configure, and this
    table holds the key it is configured under.
    """

    #: `private` or `team`: which Notion space the mirror may reach.
    scope: str
    #: The `sd config` key holding this operator's folder page id, a
    #: workflow database row (`publish_setting`).
    setting: str
    #: How a report and a status row name the folder, over the repo name. The
    #: configured folder's own name is not knowable here, and guessing one
    #: would print a label no operator's Notion has to agree with.
    phrase: str


#: Where a Notion mirror goes when the designation names no folder.
#:
#: Private is the default and the team space is the opt-in, because the two
#: mistakes are not symmetric. A brief the team cannot see is repaired by
#: adding `team=True` and draining again. A private brief in a shared team
#: space has already been seen by the team, and deleting it does not undo that.
#: So the direction that is recoverable is the one that happens by accident.
NOTION_SCOPES = {
    False: NotionScope("private", "sd.notion_private_folder",
                       "your private Notion briefs folder, under %s"),
    True: NotionScope("team", "sd.notion_team_folder",
                      "your team Notion briefs folder, under %s"),
}

#: The variable each publish setting was read from before it was a row
#: (sd:5141). It answers only while the database holds no row for the key, so
#: a machine publishes as before until its values are imported.
SETTING_VARIABLES = {
    "sd.notion_private_folder": "SD_NOTION_PRIVATE_FOLDER",
    "sd.notion_team_folder": "SD_NOTION_TEAM_FOLDER",
    "sd.notion_experiments": "SD_NOTION_EXPERIMENTS",
    "sd.notion_experiment_tasks": "SD_NOTION_EXPERIMENT_TASKS",
}

#: The environment the old setting variables, `SD_MIRROR_REQUEUE`, `SD_MIRROR_DRY_RUN`,
#: `SD_PUBLISH_FROM_WORKTREE` and `SD_SKIP_MIRROR` are read out of. A module attribute for the same
#: reason `QUEUE` and `VAULT` are: a test points it at a fixture rather than
#: at the machine running the test.
ENVIRON: dict[str, str] = dict(os.environ)

#: A Notion page id: 32 hex digits, dashed or not, alone or ending a page's
#: URL path. `space=` may be written either way, and so may the configured
#: default, so naming a folder never costs the designation its id resolution.
NOTION_ID = re.compile(
    r"(?:^|[/-])([0-9a-fA-F]{8}-?(?:[0-9a-fA-F]{4}-?){3}[0-9a-fA-F]{12})$")


def notion_id(value: str) -> str:
    """The Notion page id `value` names, or `""` when it names none.

    Read off the URL *path*, so a query string and a fragment are both gone
    before the id is looked for. A copied link often carries `#<block id>`,
    and matching against the whole link found no id at all -- which read as
    "this is a folder name" and sent the drain looking for a folder called
    `https://...`.

    Returned verbatim rather than normalised: the id goes to a connector, and
    rewriting the operator's spelling of it is a second thing that can be wrong.
    """
    found = NOTION_ID.search(urlsplit(value.strip()).path.rstrip("/"))
    return found.group(1) if found else ""

#: The dashboard builds a URL from the key, so the key is what a URL may carry.
#: Mirrored from `sd_dashboard/documents.py`, which rejects anything else.
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def documents_conf() -> Path:
    return DASHBOARD_CONFIG / "documents.conf"


#: Where a render by hand records the sha256 of the research.conf.py it
#: executed, under the git directory. Every hook trigger renders only a config
#: whose digest this holds, and carries the same name as `TRUSTED` because an
#: installed hook imports nothing from here (sd:1376).
TRUSTED_CONF = "sd-research-conf.sha256"

#: What the hook sets, to its trigger's name, for the render it runs. The hook
#: spells it again for the same reason as `TRUSTED`.
TRIGGER_ENV = "SD_RESEARCH_TRIGGER"


def automatic_render() -> str | None:
    """What ran this render, when it was not the operator: None by hand.

    The hook says so in `TRIGGER_ENV`. An earlier hook body, still installed
    until `init-hook` replaces it, runs the render and says nothing, so a render
    git runs counts too: git sets `GIT_EXEC_PATH` for every hook it runs, and a
    shell does not.
    """
    return os.environ.get(TRIGGER_ENV) or ("git" if os.environ.get("GIT_EXEC_PATH") else None)


def conf_trusted(repo: Path, source: bytes) -> bool:
    """Whether a render by hand has recorded `source` as read."""
    record = git_output(["rev-parse", "--git-path", TRUSTED_CONF], repo)
    if not record:
        return False
    try:
        recorded = (Path(repo) / record).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return recorded == hashlib.sha256(source).hexdigest()


def trust_conf(repo: Path, source: bytes) -> None:
    """Record `source`, the research.conf.py a render by hand executed, as read.

    A render by hand is the operator's own command, so what it executed is
    what they vouch for. Nothing automatic calls this: a commit, a pull or a
    checkout says nothing about a config nobody opened. Outside a git checkout
    there is no hook to read the record, and nothing is written.
    """
    record = git_output(["rev-parse", "--git-path", TRUSTED_CONF], repo)
    if record:
        (Path(repo) / record).write_text(hashlib.sha256(source).hexdigest() + "\n", encoding="utf-8")


#: The namespace key `execute_conf` sets to the sha256 of the bytes it
#: executed: render's manifest says which config it describes.
CONF_DIGEST = "__conf_sha256__"


def execute_conf(repo: Path, verb: str) -> dict[str, Any] | None:
    """The namespace `repo`'s research.conf.py builds, or None when it has none.

    The one place a verb executes the config: `render`, `publish` and
    `review` all did, and only `render` checked the record (sd:1376), so a
    `publish` after a pull ran a config nobody read (sd:3513). A run nobody
    typed executes only a recorded config, checked on the bytes it executes,
    and records nothing; a run by hand records what it executed.
    """
    conf = Path(repo) / "research.conf.py"
    if not conf.is_file():
        return None
    source = conf.read_bytes()
    trigger = automatic_render()
    if trigger and not conf_trusted(repo, source):
        raise SystemExit(
            "%s: no command by hand has executed this research.conf.py, and "
            "`sd-research-kit %s` executes it; read it, then run `sd-research-kit "
            "render`. docs/dashboard/ is stale until then" % (trigger, verb))
    ns: dict[str, Any] = {}
    # nosec B102 - research.conf.py is a file in the repo being rendered, at the
    # same trust level as this script, and the format needs execution: at least
    # one live config builds its DOCS list with a loop.
    exec(compile(source, str(conf), "exec"), ns)  # nosec B102
    # Set after the run, so the config cannot name a digest of its own.
    ns[CONF_DIGEST] = hashlib.sha256(source).hexdigest()
    if not trigger:
        trust_conf(repo, source)
    return ns


def project_label(repo: Path, project: str | None) -> str:
    """The research badge and dashboard label: `PROJECT`, or one fallback.

    One fallback for every verb: `render` and `publish` each had their own,
    so alternating them relabelled the dashboard row on every run (sd:3513).
    The repository's name, not the checkout's: a worktree is called after
    its branch, and its name is nobody's project.
    """
    return project or repo_home(repo).name.split("-")[0].upper()


def repo_home(repo: Path) -> Path:
    """The checkout that names this repository: `repo`, unless it is a linked
    worktree, in which case the main checkout it was added from.

    Everything derived from a repository's *name* -- the dashboard key, the
    vault folder, a default Notion or Drive container, the queue filename --
    reads it from here and never from the directory the render ran in. A
    worktree is a scratch checkout with a scratch name, and a render from one
    called `tc-pins` wrote `label|tc-pins|TRACE-CLASSIFIER` into the
    dashboard's tracked `documents.conf` and a `Briefs/tc-pins/` folder into
    the vault, both naming a directory that would be gone within the day.

    The files a render reads and writes stay the worktree's own: what was
    rendered is that checkout's content, and the request carries its paths.
    Only the identity comes from the main checkout.
    """
    return main_worktree_root(repo)


def linked_worktree(repo: Path) -> bool:
    """Whether `repo` is a linked worktree of some other checkout."""
    return repo_home(repo).resolve() != Path(repo).resolve()


#: The switch that lets a linked worktree publish. On the invocation and
#: named for what it does, so an operator who sets it is asking, in words,
#: for branch content to become the canonical copy.
PUBLISH_FROM_WORKTREE = "SD_PUBLISH_FROM_WORKTREE"


#: The switch that renders without queueing a mirror, set to the reason. The
#: hook sets it for a branch checkout that lands behind its upstream: that
#: tree is about to be replaced by a pull, and a request queued from it is
#: stale text a drain may publish before the pull's render replaces it
#: (sd:1991). The dashboard still wants the render; the queue does not.
SKIP_MIRROR = "SD_SKIP_MIRROR"

#: The switch that reports what a render would queue and writes no request
#: and no receipt (sd:3515). It writes no vault copy either and says what it
#: would write (sd:5897): a dry run writes nothing outside the render's own
#: `docs/dashboard/` output.
MIRROR_DRY_RUN = "SD_MIRROR_DRY_RUN"


def publish_refusal(repo: Path, copy: str) -> str:
    """Why this checkout may not publish `copy`, or `""` when it may.

    A linked worktree renders locally and publishes nothing by itself. The
    dashboard key and the vault folder are the *repository's*, which is what
    stopped a worktree called `tc-pins` registering itself -- but the same
    identity would let an unmerged branch overwrite the main checkout's
    pending request, inherit the page id a drain recorded for it, and put
    branch content into the shared vault folder as the canonical document.
    Publication is the main checkout's, and a worktree that wants it says so
    with `SD_PUBLISH_FROM_WORKTREE=1` on the invocation.

    The main checkout queues a mirror from its default branch only (sd:2019).
    The hook renders on post-checkout, so a branch switch there rewrote the
    pending request with that branch's text, and a drain in that window
    published unmerged content. A detached HEAD is off the branch too. The
    vault copy is local and the next render replaces it, so it is not held.
    The default branch is `canonical_branch`'s, whatever HEAD tracks.
    """
    if ENVIRON.get(PUBLISH_FROM_WORKTREE):
        return ""
    if linked_worktree(repo):
        return ("%s: not written from a linked worktree; run `sd-research-kit "
                "render` in %s, or set %s=1 to publish this branch's content"
                % (copy, repo_home(repo), PUBLISH_FROM_WORKTREE))
    if copy != "mirror" or git_output(["rev-parse", "--git-dir"], repo) is None:
        return ""
    branch = git_output(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
    default = canonical_branch(repo)
    if branch and branch == default:
        return ""
    where = "branch %s" % branch if branch else "a detached HEAD"
    if default is None:
        return ("%s: not queued from %s; no default branch resolves (origin/HEAD "
                "is unset): run `git remote set-head origin --auto`, or set %s=1 "
                "to publish this branch's content"
                % (copy, where, PUBLISH_FROM_WORKTREE))
    return ("%s: not queued from %s; the main checkout queues from %s only, "
            "or set %s=1 to publish this branch's content"
            % (copy, where, default, PUBLISH_FROM_WORKTREE))


def canonical_branch(repo: Path) -> str | None:
    """The branch the main checkout publishes from, or None when none resolves.

    Read from `origin` and never from HEAD's tracking remote: a feature branch
    that tracks a fork whose default is that branch would otherwise name
    itself canonical and publish unmerged content. With an `origin`, only
    `origin/HEAD` names it: an `origin/main` or a local `main` can exist
    beside a different default, so a conventional name proves nothing. Only a
    checkout with no `origin` falls back to a local `main` or `master`. None
    holds the mirror: no branch then matches, and the refusal names
    `git remote set-head origin --auto`.
    """
    named = git_output(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo)
    if named and named.startswith("origin/"):
        return named[len("origin/"):]
    if git_output(["remote", "get-url", "origin"], repo) is not None:
        return None
    for name in ("main", "master"):
        if git_output(["rev-parse", "--verify", "--quiet", "refs/heads/" + name], repo) is not None:
            return name
    return None


def repo_key(repo: Path) -> str:
    """A short lowercase key from the repository's directory name.

    Derived and not configured: a key the repo chose for itself could collide
    with another repo's, and the collision would surface as one repo's
    documents silently replacing the other's in the listing. The name is the
    main checkout's, whichever worktree is rendering; see `repo_home`.
    """
    stem = repo_home(repo).name.lower()
    stem = re.sub(r"[^a-z0-9._-]+", "-", stem).strip("-.")
    return stem or "docs"


def _shown(directory: Path) -> str:
    """`~` and not the absolute path: `documents.conf` is shared across
    machines, and an absolute path under one machine's home is wrong on the
    next."""
    home = str(Path.home())
    shown = str(directory)
    if shown == home or shown.startswith(home + os.sep):
        shown = "~" + shown[len(home) :]
    return shown


def _own_row(repo: Path, path: str) -> bool:
    """Whether a row's directory is inside this repository, so ours to rewrite.

    A row naming another repository's directory is a collision to report, not
    a row to take. `build/` and `docs/dashboard/` are both inside this one.
    """
    try:
        return Path(path).expanduser().is_relative_to(repo)
    except (OSError, ValueError):
        return False


def register_root(repo: Path, label: str, directory: Path) -> str:
    """Register this repo with the dashboard, once.

    The dashboard finds `docs/dashboard/` by itself, so the default location
    needs no row at all: its name is the workflow database's `view` row, set
    on the Documents page, and a `label|` line is no longer read (sd:3986,
    sd:4631). Writing one on every render only filled the file with lines
    the page then lists as stale. `root|<key>|<label>|<directory>` stays for
    a directory the dashboard cannot find -- somewhere outside
    `docs/dashboard/` -- which is the only thing a row can say that the disk
    does not.

    A `root|` row this repository wrote for a directory it has since left
    (`build/`, or the default written down as a path) is retired, because it
    outranks the directory the dashboard would find. A `label|` row is left
    where it is: the page names it, and Rename replaces it.

    Returns a one-line report. Never raises on a missing dashboard: a machine
    without the dashboard checked out still renders its documents, and failing
    the render would make the reading form hostage to the listing.
    """
    conf = documents_conf()
    found = directory == repo / DASHBOARD_DIR
    key = repo_key(repo)
    if not conf.is_file():
        if found:
            return "dashboard: finds %s by itself; no row to write" % key
        return ("dashboard not registered: no %s; create it, or set SYSTEM_TOOLS_CONFIG "
                "to the folder that holds project-dashboard/" % conf)

    if not KEY.fullmatch(key):
        return "dashboard not registered: %r is not a usable key" % key

    shown = _shown(directory)
    line = None if found else "root|%s|%s|%s" % (key, label, shown)

    text = conf.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    for index, existing in enumerate(lines):
        parts = [p.strip() for p in existing.strip().split("|")]
        if not (parts[0] == "root" and len(parts) == 4 and parts[1] == key):
            continue
        # A row naming a directory outside this repository belongs to
        # something else, whatever it calls itself.
        if not _own_row(repo, parts[3]):
            return ("dashboard: %s already names %s; left alone. The key is this "
                    "checkout's name: rename one checkout, or remove that row from %s"
                    % (key, parts[3], conf))
        if existing.strip() == line:
            return "dashboard: already registered as %s" % key
        # Ours, and saying the wrong thing: rewritten in place, or retired
        # when the dashboard finds the directory itself, because two rows
        # for one key is the drift twice over.
        if line is None:
            del lines[index]
            conf.write_text("".join(lines), encoding="utf-8")
            return "dashboard: retired the row for %s; it finds docs/dashboard by itself" % key
        lines[index] = line + "\n"
        conf.write_text("".join(lines), encoding="utf-8")
        return "dashboard: moved %s off %s -> %s" % (key, parts[3], shown)

    if line is None:
        return "dashboard: finds %s by itself; no row to write" % key
    with conf.open("a", encoding="utf-8") as handle:
        if not text.endswith("\n"):
            handle.write("\n")
        handle.write(line + "\n")
    return "dashboard: registered %s -> %s" % (key, shown)


def revision(repo: Path) -> str:
    """The commit the render was made from, or `unknown` outside a checkout.

    Through `sd_lib.git_output` rather than a subprocess call of its own:
    `tests/test_git_policy.py` allows one git policy in `bin/`, and a second
    would mean a second timeout and a second answer to what a failure means.
    """
    return git_output(["rev-parse", "HEAD"], repo) or "unknown"


#: The three spellings of a github.com `origin` this pack reads elsewhere
#: (`sd_ship_remote.slug`), with no credential in them. Anything else -- no
#: origin, another host, a token in the URL -- names no GitHub repository.
GITHUB_ORIGIN = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?")


def source_url(repo: Path, src: str) -> str | None:
    """The GitHub URL of `src` for a mirror's pointer line, or None.

    Read from `origin`, so the drain never composes an owner itself: it did,
    and a moved repository's pages named its old owner for six days
    (sd:1999). `blob/HEAD/` is GitHub's name for the
    default branch, whatever it is called: the link is where to edit, and a
    commit permalink would change the request on every commit.
    """
    match = GITHUB_ORIGIN.fullmatch(git_output(["remote", "get-url", "origin"], repo) or "")
    if not match or not src:
        return None
    try:
        path = (repo / src).resolve().relative_to(repo.resolve())
    except ValueError:
        return None
    return "https://github.com/%s/%s/blob/HEAD/%s" % (
        match[1], match[2], quote(path.as_posix(), safe="/"))


#: A Markdown link or image whose target has no scheme and is no anchor or
#: absolute path: one a mirror cannot follow. A target may hold balanced
#: parentheses, `report(2026).md`, one level deep, as CommonMark allows.
#: The `code` branch matches an inline code span first, so a link written
#: inside one stays as written: its backtick run closes only on a run as
#: long (sd:6279).
RELATIVE_LINK = re.compile(
    r"(?P<code>(?<!`)(?P<ticks>`+)(?!`).*?[^`](?P=ticks)(?!`))"
    r"|(?P<bang>!?)\[(?P<label>[^\]\n]*)\]\((?![A-Za-z][A-Za-z0-9+.-]*:|#|/)"
    r"(?P<path>(?:[^()\s#]|\([^()\s#]*\))+)(?P<anchor>#(?:[^()\s]|\([^()\s]*\))*)?\)")


def repo_links(text: str, repo: Path, src: str) -> str:
    """`text` with each repo-relative link pointing at its file on GitHub.

    A mirror has no repository beside it: Notion read `[x](DESIGN.md)` as
    `https://DESIGN.md` (sd:6156). Without a GitHub origin the link keeps
    its text only. Fenced code and code spans are left as written; a fence
    closes only on a run of its own character at least as long (sd:6279).
    """
    folder = Path(src).parent

    def linked(match: re.Match[str]) -> str:
        if match["code"]:
            return match["code"]
        url = source_url(repo, (folder / unquote(match["path"])).as_posix())
        if not url:
            return match["label"]
        return "%s[%s](%s%s)" % (match["bang"], match["label"], url, match["anchor"] or "")

    lines: list[str] = []
    closer: re.Pattern[str] | None = None
    for line in text.split("\n"):
        if closer:
            closer = None if closer.match(line) else closer
        elif fence := FENCE.match(line):
            mark = fence.group(1)
            closer = re.compile(r"^\s*%s{%d,}\s*$" % (re.escape(mark[0]), len(mark)))
        else:
            line = RELATIVE_LINK.sub(linked, line)
        lines.append(line)
    return "\n".join(lines)


def notion_target(
    raw: dict[str, Any], repo: Path, folder: str = ""
) -> tuple[dict[str, str] | None, str]:
    """One Notion designation resolved to a container, or what was missing.

    `team=True` is the only way a brief reaches the shared space; see
    `NOTION_SCOPES` for why that direction is the one that must be asked for.
    An explicit `space=` overrides the folder but never the scope: naming a
    folder says where inside a space, not which space.

    The container is carried as `space_id`, a Notion page id, and `resolve`
    says how it was arrived at. `id` is every configured default and any
    `space=` written as an id or a page URL; a rename cannot touch those.
    `name` is a `space=` written as a plain name, which no id can be derived
    from -- there the drain does look the folder up by name, and the contract
    makes an empty lookup a failure to report rather than a folder to create.

    A default also carries `subfolder`, the repo's own page under that folder,
    the way a Drive default carries `Briefs/<repo>`. The repo's `channels`
    setting may name that page instead (`folder`, sd:3462). An explicit
    `space=` carries none: naming a container says where the document goes,
    and appending to what the user named would put it somewhere they did not
    ask for.

    A default with nothing configured returns no target and says so, naming
    the setting to set. Falling back would mirror the document into whichever
    page this file happened to name, which is another account's page on every
    machine but one. A setting that is not a page id is not configured: an
    operator who pasted a folder's name has not named a folder this can reach.
    A setting that cannot be read returns no target either, naming the cause.
    """
    scope, setting, phrase = NOTION_SCOPES[bool(raw.get("team"))]
    named = str(raw.get("space", "")).strip()
    if named:
        override = notion_id(named)
        return {
            "destination": "notion",
            "scope": scope,
            "space_id": override,
            "resolve": "id" if override else "name",
            "space": "" if override else named,
            "subfolder": "",
            "page": str(raw.get("page", "")).strip(),
            "target": "the %s folder in your %s space" % (named, scope),
        }, ""

    value, unknown = publish_setting(setting)
    if unknown:
        return None, "notion= not queued: %s" % unknown
    configured = notion_id(value)
    if not configured:
        return None, (
            "notion= has no %s folder configured: run `sd config set %s <page id>` "
            "with the Notion page id of that folder, or name one with space=" % (scope, setting)
        )
    return {
        "destination": "notion",
        "scope": scope,
        "space_id": configured,
        "resolve": "id",
        "space": "",
        "subfolder": folder or repo.name,
        "page": str(raw.get("page", "")).strip(),
        "target": phrase % (folder or repo.name),
    }, ""


def mirror_targets(
    cfg: dict[str, Any], repo: Path, channels: dict[str, dict[str, str]] | None = None
) -> tuple[list[dict[str, str]], list[str]]:
    """The outward destinations this DOCS entry designates, and what was wrong.

    A document publishes to the dashboard and to Obsidian whatever this
    returns; these are the copies that leave the machine. Nothing here infers a
    destination from a title, a path or a neighbour, and nothing promotes a
    document because it looks finished.

    Returns a list because a document may be designated for several places at
    once. `notion=` and `drive=` on one entry are two mirrors, not a choice.

    A designation that names no container gets its destination's default, and
    both defaults name the repo: `Briefs/<repo>` for Drive, and for Notion the
    repo's own page under the configured briefs folder. That is the shape the
    vault already uses, so a reader who knows where one brief is knows where
    all of them are, whichever copy they found first. Notion's default used to
    stop at the folder, which dropped every repo's briefs into one page
    alongside the per-repo pages already there.

    Problems are returned rather than raised, and returned *per destination*.
    A malformed designation is the user's to fix, and one bad key must not cost
    the other destination its request: the local copies are written either way,
    so withholding a valid mirror would punish the destination that was fine.

    `channels` is the repo's setting (sd:3462), None for none. A destination
    it leaves out is refused by name, and the others still publish. A folder
    it names stands in for the default, and a document's own container wins
    over it. A pinned page or file wins over both, and is named, so the
    operator knows that document did not move.
    """
    targets: list[dict[str, str]] = []
    problems: list[str] = []
    for dest in DESTINATIONS:
        raw = cfg.get(dest.name)
        if raw is None or raw is False:
            continue
        # `notion=dict()` is a designation, and an empty dict is falsy -- so
        # presence is what counts here, not truth. `notion=True` is the same
        # designation written shorter.
        if raw is True:
            raw = {}
        if not isinstance(raw, dict):
            problems.append(
                "%s= must be a dict, not %s" % (dest.name, type(raw).__name__)
            )
            continue
        if channels is not None and dest.name not in channels:
            problems.append("%s: not a channel of this repo; not queued" % dest.name)
            continue
        folder = (channels or {}).get(dest.name, {}).get("folder", "")
        pinned = str(raw.get(dest.what, "")).strip()
        if folder and pinned and not str(raw.get(dest.where, "")).strip():
            problems.append("%s: %s= pins the %s, which stays where it is and does not move "
                            "to %s; delete %s= to create it there"
                            % (dest.name, dest.what, dest.what, folder, dest.what))
        if dest.name == "notion":
            target, problem = notion_target(raw, repo, folder)
            if target is None:
                problems.append(problem)
            else:
                targets.append(target)
            continue
        # Notion returned above, so this is the destination whose default is
        # a path the drain resolves rather than a container it is handed.
        where = str(raw.get(dest.where, "")).strip() or folder or dest.default % repo.name
        target = {
            "destination": dest.name,
            dest.where: where,
            dest.what: str(raw.get(dest.what, "")).strip(),
            # Worded here rather than by whoever reads the request. `sd-status`
            # prints this verbatim, so it never has to carry a second copy of
            # the destination table to say which place a row is waiting on.
            "target": "the %s %s" % (where, dest.noun),
        }
        # A writing piece names the Google account its realm picks (pack
        # sd:7243); the drain writes with it. Part of the identity, so a copy
        # in one account is never carried into the other. A document that
        # names none keeps the request it had, byte for byte.
        account = str(raw.get("account", "")).strip()
        if account:
            target["account"] = account
        targets.append(target)
    return targets, problems


def vault_folder(repo: Path, channels: dict[str, dict[str, str]] | None = None) -> Path | None:
    """`<vault>/Briefs/<repo>`, or None when no vault is configured.

    None rather than a raise: a machine with no vault still renders and still
    publishes to the dashboard, the same way one with no dashboard checkout
    does. The copy that is missing is reported by name.

    The repo's `channels` setting may name another folder under the vault,
    or leave `obsidian` out, which is None too (sd:3462).
    """
    if not VAULT or (channels is not None and "obsidian" not in channels):
        return None
    folder = (channels or {}).get("obsidian", {}).get("folder")
    if folder:
        return Path(VAULT).expanduser() / folder
    return Path(VAULT).expanduser() / BRIEFS / repo_home(repo).name


def left_behind(repo: Path, folder: Path | None) -> list[str]:
    """Each vault copy this checkout wrote to `Briefs/<repo>` before its
    `channels` setting moved the folder or left `obsidian` out (sd:3462).

    Named on every render and deleted by nobody but the operator: a copy
    in the vault may be linked from notes this tool cannot see. A copy
    another checkout or an earlier version wrote names no checkout of
    this one, and is not named.
    """
    if not VAULT:
        return []
    default = Path(VAULT).expanduser() / BRIEFS / repo_home(repo).name
    if folder == default:
        return []
    checkout = _shown(repo_home(repo))
    return ["obsidian: %s is from before this repo's channels setting moved its vault "
            "folder; delete it by hand" % copy
            for copy in sorted(default.glob("*.md")) if vault_owner(copy) == checkout]


def vault_owner(target: Path) -> str | None:
    """The checkout a vault copy's frontmatter names: "" when it names none,
    None when the copy cannot be read.

    The folder is keyed on the checkout's name, so two checkouts with one name
    share it (sd:3513). A missing copy, or one an earlier version wrote, names
    no checkout and is taken over. An unreadable one is unknown, not ours.
    """
    try:
        lines = target.read_text(encoding="utf-8").split("\n")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError):
        return None
    if not lines or lines[0] != "---":
        return ""
    for line in lines[1:]:
        if line == "---":
            break
        if line.startswith("source_checkout: "):
            return line[len("source_checkout: "):].strip()
    return ""


def vault_repository(repo: Path) -> bool:
    """Whether this checkout is the vault itself (system sd:5500).

    The vault's own repository mirrors the whole vault, so every one of its
    entries is written as before. Every other repository writes only the
    entries that designate `obsidian=`, as it queues only those that
    designate `notion=` or `drive=`.
    """
    if not VAULT:
        return False
    try:
        return repo_home(repo).resolve() == Path(VAULT).expanduser().resolve()
    except OSError:
        return False


def obsidian_designated(cfg: dict[str, Any]) -> bool:
    """An entry's `obsidian=`: any value but None or False designates, as for
    `notion=` and `drive=`; `obsidian=dict()` is the form the dashboard writes."""
    raw = cfg.get("obsidian")
    return raw is not None and raw is not False


def vault_entries(repo: Path, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entries `write_obsidian` writes: every one in the vault's own
    repository, else those that designate `obsidian=`."""
    if vault_repository(repo):
        return list(docs)
    return [cfg for cfg in docs if obsidian_designated(cfg)]


def trash_obsidian(repo: Path, document: str) -> tuple[str, str]:
    """Move this checkout's vault copy of `document` into the vault's
    `.trash` (system sd:5500): `(state, line)`.

    The dashboard's untick of a document's Obsidian box, a local move like
    the write it undoes, so nothing is queued. `state` is `trashed`,
    `nothing` when no copy is there, or "" when it refused with nothing
    moved: no vault, a channels setting that cannot be read, a copy that is
    not this checkout's by the rule `write_obsidian` writes by, a link, or a
    move that failed. `.trash` is Obsidian's own trash folder, so the copy
    can be restored from there; a name already taken there gets ` 1`, ` 2`
    and so on.
    """
    if not VAULT:
        return "", "obsidian: OBSIDIAN_VAULT is not set; nothing trashed"
    channels, unknown = read_channels(repo)
    if unknown:
        return "", "obsidian: cannot read this repo's channels setting (%s); nothing trashed" % unknown
    vault = Path(VAULT).expanduser()
    folder = vault_folder(repo, channels)
    if folder is None:
        return "", ("obsidian: not a channel of this repo, so which folder holds the copy is not known; nothing "
                    "trashed. Turn it on, or move the copy by hand")
    if Path(document).is_absolute() or ".." in Path(document).parts:
        return "", "obsidian: %r is not a document name under the vault folder; nothing trashed" % document
    target = folder / (document + ".md")
    if not (target.exists() or target.is_symlink()):
        return "nothing", "obsidian: %s: no vault copy at %s; nothing to trash" % (document, target)
    owner = vault_owner(target)
    takeover = not (channels or {}).get("obsidian", {}).get("folder")
    try:
        inside = target.resolve().is_relative_to(folder.resolve()) and folder.resolve().is_relative_to(vault.resolve())
    except OSError:
        inside = False
    if target.is_symlink() or not inside or owner is None or (owner and owner != _shown(repo_home(repo))) \
            or (not owner and not takeover):
        return "", "obsidian: %s is not this checkout's copy; left alone, nothing trashed" % target
    return _into_trash(target, vault / ".trash")


def _into_trash(target: Path, trash: Path) -> tuple[str, str]:
    """Move `target` into `trash`, under a name not taken there yet."""
    try:
        trash.mkdir(exist_ok=True)
        place, taken = trash / target.name, 0
        while place.exists() or place.is_symlink():
            taken += 1
            place = trash / ("%s %d.md" % (target.stem, taken))
        os.rename(target, place)
    except OSError as problem:
        return "", "obsidian: cannot move %s into %s (%s); nothing trashed" % (target, trash, problem)
    return "trashed", "obsidian: trashed %s -> %s" % (target, place)


def committed_source(repo: Path, src: str, rev: str) -> tuple[str | None, str]:
    """`(text, revision)`: the file as commit `rev` holds it, or `(None,
    "uncommitted")` when that commit holds no regular file there.

    The vault copy names its revision, so it carries that commit's bytes,
    not a working-tree edit made since (sd:10629). A file the commit lacks,
    or holds as a link or submodule, is written from disk and names none.
    """
    if rev == "unknown":  # not a checkout: nothing to record, as before
        return None, rev
    listed = git_output(["ls-tree", rev, "--", src], repo)
    if listed and "\n" not in listed and listed.split(" ", 1)[0] in ("100644", "100755"):
        try:
            text = git_show(repo, "%s:./%s" % (rev, src))
        except UnicodeDecodeError:
            text = None
        if text is not None:
            return text, rev
    return None, "uncommitted"


def write_obsidian(repo: Path, docs: list[dict[str, Any]],
                   wrote: list[dict[str, Any]] | None = None,
                   channels: dict[str, dict[str, str]] | None = None) -> list[str]:
    """Write every document's Markdown into the vault, under its repo folder.

    Each entry whose copy was written is appended to `wrote`, when given: the
    render records the vault link of those alone (sd:3979). A skipped copy is
    not there, so nothing points an item at a copy that was not written.

    Obsidian is where these documents live, so this is not a mirror of the
    published form -- it is the Markdown itself, carried across with a
    frontmatter block naming where it came from. Markdown and not HTML: the
    vault's whole value is that a note is editable and linkable, and an HTML
    blob in it is neither.

    A local filesystem write, so a render performs it rather than queueing it.
    Nothing here reaches the network and nothing needs a credential, which is
    what lets the primary copy be the one that never waits for a drain.

    The repo's `channels` setting (sd:3462) may move the folder, or leave
    Obsidian out, which writes no copy and says so.

    Outside the vault's own repository only an entry that designates
    `obsidian=` is written (system sd:5500); a copy of one that does not is
    left where it is, and the dashboard's untick moves it to the trash.
    """
    refused = publish_refusal(repo, "obsidian")
    if refused:
        return [refused]
    folder = vault_folder(repo, channels)
    if channels is not None and "obsidian" not in channels:
        return ["obsidian: not a channel of this repo; no vault copy written"] + left_behind(repo, folder)
    if folder is None:
        return ["obsidian: OBSIDIAN_VAULT is not set; no vault copy written"]

    rev = revision(repo)
    home = repo_home(repo)
    checkout = _shown(home)
    # Only the default folder is this tool's own. A folder the setting names
    # is the operator's, even one spelled as the default, so a note there
    # that names no checkout is theirs, never an earlier version's copy to
    # take over.
    takeover = not (channels or {}).get("obsidian", {}).get("folder")
    reports: list[str] = []
    written = 0
    for cfg in vault_entries(repo, docs):
        src = str(cfg.get("src", "")).strip()
        out = str(cfg.get("out", "")).strip()
        if not src or not out:
            continue
        body, stamp = committed_source(repo, src, rev)
        if body is None:
            try:
                body = (repo / src).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as problem:
                reports.append("obsidian: cannot read %s (%s)" % (src, problem))
                continue
        # Frontmatter, not a prose header: Obsidian reads these as properties,
        # so the provenance is queryable in the vault rather than being a line
        # a reader has to notice. Written by this tool on every render, so it
        # is replaced wholesale and never merged with a hand-edited one --
        # which is also why the vault copy is not the place to edit.
        front = "\n".join([
            "---",
            "source_repo: %s" % home.name,
            "source_checkout: %s" % checkout,
            "source_path: %s" % src,
            "revision: %s" % stamp,
            "rendered_by: sd-research-kit",
            "---",
            "",
        ])
        target = folder / (out + ".md")
        refused = vault_refusal(target, checkout, takeover)
        if refused:
            reports.append(refused)
            continue
        if ENVIRON.get(MIRROR_DRY_RUN):
            # A dry run writes nothing, the vault copy included (sd:5897).
            reports.append("obsidian: would write %s.md to %s (%s)" % (out, folder, MIRROR_DRY_RUN))
            continue
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target.write_text(front + body, encoding="utf-8")
        except OSError as problem:
            reports.append("obsidian: cannot write %s.md (%s)" % (out, problem))
            continue
        written += 1
        if wrote is not None:
            wrote.append(cfg)
    if written:
        reports.append("obsidian: wrote %d document(s) to %s" % (written, folder))
    return reports + left_behind(repo, folder)


def vault_refusal(target: Path, checkout: str, takeover: bool) -> str:
    """Why the vault copy at `target` is left alone, or "" to write it.

    A copy another checkout wrote is that checkout's; one that names no
    checkout is the operator's unless `takeover` says the folder is this
    tool's own; one that cannot be read is nobody's to overwrite.
    """
    owner = vault_owner(target)
    if owner is None:
        return "obsidian: cannot read %s to see whose copy it is; left alone" % target
    if owner and owner != checkout:
        return ("obsidian: %s in %s belongs to %s; left alone. Rename one checkout, or "
                "delete that copy to move it here" % (target.name, target.parent, owner))
    if not owner and not takeover and (target.exists() or target.is_symlink()):
        return ("obsidian: %s names no checkout, so this tool did not write it; left alone. "
                "Move that note, or rename the document" % target)
    return ""


def ignore_dashboard(repo: Path) -> str:
    """Keep `docs/dashboard/` out of the repo's history, idempotently.

    The folder is a publication surface, regenerated on every commit. Tracking
    it would put a generated diff in front of a reader on every render, and the
    hook that re-renders after a commit would make the working tree dirty the
    moment it finished.
    """
    entry = "docs/dashboard/"
    path = repo / ".gitignore"
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError as problem:
        return "gitignore: cannot read .gitignore (%s)" % problem
    if any(line.strip().rstrip("/") == entry.rstrip("/") for line in text.splitlines()):
        return "gitignore: docs/dashboard/ already ignored"
    try:
        with path.open("a", encoding="utf-8") as handle:
            if text and not text.endswith("\n"):
                handle.write("\n")
            handle.write("%s\n" % entry)
    except OSError as problem:
        return "gitignore: cannot write .gitignore (%s)" % problem
    return "gitignore: added docs/dashboard/"


def migrate_legacy_queue() -> list[str]:
    """Move any request left under the queue's former name into `QUEUE`.

    Migration rather than reading both for ever. Two directories mean every
    reader has to know both names, and a reader written later knows one -- the
    defect being closed here is exactly that. Moving once leaves one queue,
    and this function then finds nothing and says nothing on every later run.

    Nothing is created by looking. A machine that never used the old name has
    no legacy directory, and this neither makes one nor makes `QUEUE` to
    receive an empty migration.

    The emptied directory is left standing. Removing it would buy tidiness at
    the price of a new deletion path, and `tests/test_archive_untouched.py`
    enumerates and freezes every one under `bin/` precisely so that no new
    sweep appears for a reason that small. An empty directory strands nothing:
    the requests were what was stranded, and they have moved.

    The current queue wins a name collision. Both directories hold one request
    per document per destination under the same filename, and the rename is
    what stopped the old name being written -- so a file in `QUEUE` was
    written after its legacy twin, and keeping the older one would replace a
    current request with a stale revision.

    The requests move verbatim. An old request may predate a field a reader
    now expects; `bin/sd-status` already falls back for the one it reads, and
    rewriting somebody's queued work to a schema it was not written under is a
    worse answer than handing it over as it stands.
    """
    if not LEGACY_QUEUE.is_dir() or LEGACY_QUEUE == QUEUE:
        return []
    try:
        found = sorted(LEGACY_QUEUE.glob("*.json"))
    except OSError as problem:
        return ["mirror: cannot read %s (%s)" % (LEGACY_QUEUE, problem)]

    reports: list[str] = []
    moved = 0
    for path in found:
        target = QUEUE / path.name
        try:
            if target.exists():
                path.unlink()
                continue
            QUEUE.mkdir(parents=True, exist_ok=True)
            path.replace(target)
        except OSError as problem:
            reports.append(
                "mirror: cannot migrate %s (%s)" % (path.name, problem))
            continue
        moved += 1
    if moved:
        reports.append(
            "mirror: migrated %d request(s) from %s" % (moved, LEGACY_QUEUE))
    return reports


def mirror_identity(
    request: dict[str, Any], target: dict[str, str], what: str
) -> dict[str, Any]:
    """What a recorded page or file id belongs to.

    The repository and the document, because the queue filename does not say
    either one: it is keyed on the repo's *basename*, so two checkouts both
    called `research` share a path, and both may designate a document called
    `report` into the same default folder. Adopting across that would hand one
    repository's brief the other's page and overwrite it on the next drain.

    Then the container the id was created under, taken from the target so that
    a destination added to `DESTINATIONS` carries its own container fields
    here without anything being remembered.

    Two fields of the target are left out. `what` is the id itself, which is
    what is being looked up. `target` is display text: a Notion folder written
    as a page URL and the same folder written as its bare id resolve to one
    `space_id` and differ only in the wording, and dropping a recorded id over
    a spelling is the duplicate this function exists to prevent. `revision`
    and `title` are not here either -- the revision changes on every render,
    and a renamed document keeps its page, which is exactly when the recorded
    id is the only thing that still finds it.
    """
    fields: dict[str, Any] = {
        field: request.get(field) for field in ("repo", "document")
    }
    fields.update({field: value for field, value in target.items()
                   if field not in (what, "target")})
    return fields


def recorded(path: Path, wanted: dict[str, Any], what: str) -> str:
    """The page or file id a request already sitting at `path` records.

    The contract calls the request the durable record, and the drain's step 4
    is what makes it one: the id goes into the request the moment the
    destination returns it, before the designation is amended. A render
    between those two steps overwrote the request, and the id was then written
    nowhere -- so the next drain enqueued a create and left a second page
    carrying the same title. That is the duplicate steps 4 and 5 exist to
    prevent, arriving by the one route neither of them watches.

    So a render carries the recorded id forward, while the request still names
    the same document in the same repository in the same container -- see
    `mirror_identity`. It never invents one: an absent, unreadable,
    half-written or differently-addressed request records nothing, and the
    drain falls back to adopting by title, which is what it does for a page it
    has never seen.
    """
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(pending, dict):
        return ""
    # A trash request's id names a copy that may already be in the trash
    # (sd:5492): a re-tick adopts the live one by its pointer, under step 3.
    if pending.get("action") == TRASH:
        return ""
    if any(pending.get(field) != value for field, value in wanted.items()):
        return ""
    return str(pending.get(what, "")).strip()


def carried_identity(identity: dict[str, Any], cfg: dict[str, Any], where: str) -> dict[str, Any]:
    """The part of `identity` a recorded id must match to be carried: all of
    it, but for an entry that `moves` (a writing piece's file, pack sd:7243),
    whose copy the drain moves to the new container `where` names."""
    return {field: value for field, value in identity.items()
            if not (cfg.get("moves") is True and field == where)}


def delivered_url(receipt: Path, wanted: dict[str, Any], cfg: dict[str, Any]) -> str:
    """The URL drain step 6 left on `receipt`, for a routine brief's entry
    `cfg` (one carrying its `content`), or "".

    A brief has no step 5 (sd:5102): no designation records the copy, so the
    receipt is what lets the next generation update it. Like `recorded`, it
    is reused only in the container it went to -- the receipt's
    `delivered_to` equal to `wanted` -- and only for the same item. A
    receipt that names no container, from before it was recorded or for an
    older generation, reuses nothing, and the drain adopts by title in the
    named container (contract step 3).
    """
    if not isinstance(cfg.get("content"), str):
        return ""
    record = load_record(receipt)
    went = record.get("delivered_to")
    if not isinstance(went, dict) or record.get("item") != cfg.get("item"):
        return ""
    # `wanted` lacks the folder for an entry that moves (`carried_identity`).
    channel = BY_NAME.get(str(went.get("destination")))
    if carried_identity(went, cfg, channel.where if channel else "") != wanted:
        return ""
    url = record.get("url")
    return url.strip() if isinstance(url, str) else ""


def receipts() -> Path:
    """Where the last-queued fingerprint of every request is kept.

    Beside the queue and not in it: `bin/sd-status` globs `*.json` in the
    queue, and a receipt inside it would read as one more pending mirror.
    Derived from `QUEUE` rather than configured separately, so a test or an
    operator who moves the queue moves the receipts with it and nothing here
    can write beside the machine's real queue by mistake. One file per
    request, under the request's own name, so two repositories rendering at
    once never contend for one file.
    """
    return QUEUE.with_name(QUEUE.name + "-receipts")


def queue_lock_path() -> Path:
    """The sidecar every writer of the queue takes, beside it.

    Beside the queue and not in it, for the reason `receipts()` gives; and
    created once, never removed, because unlinking it would let a writer
    that holds the lock be joined by one that creates a fresh inode and
    locks that instead.
    """
    return QUEUE.with_name(QUEUE.name + ".lock")


@contextlib.contextmanager
def queue_locked() -> Iterator[None]:
    """Hold the queue for one read-compare-write, against writers anywhere.

    A render's enqueue and a drain's acknowledgement are each a read, a
    comparison and a write of the same request file, and nothing serialised
    them: an acknowledgement read generation A, a render wrote B, and the
    acknowledgement's unlink then removed B with A on the receipt -- the
    request lost until somebody rendered again. Under one lock the render
    lands wholly before the acknowledgement's read, where the fingerprint
    comparison keeps it, or wholly after its unlink, where nothing is left to
    remove.

    Blocking: the holder is a render or a drain's last step, nothing a
    session start waits on, and each holds it for one document's worth of
    work. An `OSError` here is the queue being unwritable, which is the caller's to report.
    """
    path = queue_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def mirror_fingerprint(request: dict[str, Any], target: dict[str, str], what: str) -> str:
    """What a drain would write, as one digest: the same digest means the
    same mirror.

    The request's `content`, because that is what the drain mirrors -- the
    Markdown as it stood at enqueue, stored in the request, and not the file
    at `source`, which may have changed by the time a drain reads it. With
    the path hashed instead: queued as A, edited to B before any drain, the
    drain read the path, published B and acknowledged A's fingerprint, and
    restoring A then read as delivered (`MUTABLE_SOURCE: remote=B, source=A,
    queue=False`). Hashing what the request stores keeps the fingerprint and
    the payload from disagreeing. Then the title, because it is what the
    mirror is called; the identity `mirror_identity` compares, because a
    document moved to another container is a new mirror; the designation's
    own page or file id, because a `page=` the user wrote is an instruction
    the drain has to see; and the source path and `source_url`, because the
    request carries them and the mirror's pointer line names them; and the
    `item`, because `delivered` records the URL on it. A request
    with no `source_url` hashes exactly what it did before the field existed,
    so a repository without a GitHub origin re-queues nothing on upgrade; one
    with it re-queues each document once, which is what corrects a pointer a
    drain composed with the wrong owner (sd:1999).

    Not the revision: a commit that touches nothing the mirror is made of
    still moves HEAD, and re-queueing on every commit is the defect this
    exists to close. Not a recorded id either: the drain writes one into the
    request after the fact, and it must not make the next render see a
    change.
    """
    named = mirror_identity(request, target, what)
    named.update(title=request.get("title", ""), source=request.get("source", ""),
                 designated=request.get(what, ""))
    if request.get("source_url"):
        named.update(source_url=request["source_url"])
    # The item the drain's `delivered` records the URL on (sd:3979): a change
    # re-queues once, so the URL reaches the new item. Absent, the digest is
    # what it was before the field joined it.
    if request.get("item") is not None:
        named.update(item=request["item"])
    # The Archify diagram the Notion page shows (sd:6066): `content` holds
    # only its marker, so a changed drawing changes the digest here.
    if request.get("image"):
        named.update(image=request["image"].get("sha256"))
    digest = hashlib.sha256(json.dumps(named, sort_keys=True).encode("utf-8"))
    digest.update(str(request.get("content", "")).encode("utf-8"))
    return digest.hexdigest()


def source_text(path: Path) -> str:
    """The Markdown a request carries: the file's text as it stands now.

    Read once, at enqueue, and stored in the request, so the drain mirrors
    what was queued and not what the path holds when the drain gets to it.
    Decoded with replacement rather than refused: a stray byte in a brief is
    a brief with one odd character, not a brief that does not publish.
    A source that cannot be read raises (sd:8825): read as "", it queued an
    empty replacement of the mirror.
    """
    return path.read_text(encoding="utf-8", errors="replace")


#: A fence opener at any indent; its closer is the same character, at least
#: as many times.
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
LIST_ITEM = re.compile(r"^\s*([-*+]|\d{1,9}[.)])(\s+|$)")
RULE = re.compile(r"^\s*(=+|-+|(\*\s*){3,}|(_\s*){3,}|(-\s*){3,})\s*$")


class BlockKind(NamedTuple):
    """A kind of block, read on the text after a line's indent and list
    markers, with an example the tests put at every position (sd:5889)."""

    name: str
    pattern: re.Pattern[str]
    example: str


#: Every block that is its own line, so nothing joins onto it. A list item
#: is not one: its text is a paragraph that may wrap. Indented code is not
#: read here, since it depends on the indent the other kinds strip.
BLOCK_KINDS = (
    BlockKind("ATX heading", re.compile(r"#{1,6}(\s|$)"), "# Heading"),
    BlockKind("setext underline", re.compile(r"=+\s*$"), "==="),
    BlockKind("rule, asterisks", RULE, "* * *"),
    BlockKind("rule, underscores", RULE, "___"),
    BlockKind("rule, hyphens", RULE, "---"),
    BlockKind("quote", re.compile(r">"), "> quoted"),
    BlockKind("table row", re.compile(r".*\|"), "| a | b |"),
    BlockKind("link definition", re.compile(r"\[[^\]]+\]:"), "[ref]: https://example.test/"),
    BlockKind("backtick fence", FENCE, "```python"),
    BlockKind("tilde fence", FENCE, "~~~"),
    BlockKind("HTML pre", re.compile(r"<"), "<pre>"),
    BlockKind("HTML comment", re.compile(r"<"), "<!-- note"),
    BlockKind("HTML processing instruction", re.compile(r"<"), "<?x"),
    BlockKind("HTML declaration", re.compile(r"<"), "<!X"),
    BlockKind("HTML CDATA", re.compile(r"<"), "<![CDATA["),
    BlockKind("HTML block tag", re.compile(r"<"), "<div>"),
    BlockKind("HTML other tag", re.compile(r"<"), "<span>"),
)
#: Indented code, raw until the next blank line like HTML; it cannot
#: interrupt a paragraph.
CODE = re.compile(r"^(    |\t)")
#: The HTML blocks a blank line does not end, each with the text that does:
#: CommonMark's types 1 to 5 (sd:5889).
HTML_RUNS = (
    (re.compile(r"<(pre|script|style|textarea)(\s|>|$)", re.I),
     re.compile(r"</(pre|script|style|textarea)>", re.I)),
    (re.compile(r"<!--"), re.compile(r"-->")),
    (re.compile(r"<\?"), re.compile(r"\?>")),
    (re.compile(r"<![A-Za-z]"), re.compile(r">")),
    (re.compile(r"<!\[CDATA\["), re.compile(r"\]\]>")),
)
#: Two trailing spaces or a backslash: a line break the author asked for.
HARD_BREAK = re.compile(r"(  |\\)\r?$")


def front_matter_end(lines: list[str]) -> int:
    """How many leading lines are YAML front matter, fences included; 0 for none."""
    if lines[0].strip() != "---":
        return 0
    return next((i + 1 for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), 0)


def opens(text: str, code: bool) -> tuple[re.Pattern[str] | None, bool] | None:
    """`(closer, raw)` for the block `text` opens, or None when it opens none.

    `closer` ends a fence, or an HTML block a blank line does not end, on
    the line it is found in; it is None for such a block closed on its own
    line. `raw` is any other HTML block, or indented code where `code`
    allows it, which a blank line ends. A fence opens at any indent, since
    inside a list item one sits deeper than three spaces.
    """
    body = text.lstrip()
    fence = FENCE.match(text)
    if fence:
        mark = fence.group(1)
        return re.compile(r"^\s*%s{%d,}\s*$" % (re.escape(mark[0]), len(mark))), False
    for start, end in HTML_RUNS:
        found = start.match(body)
        if found:
            return (None if end.search(body, found.end()) else end), False
    if body.startswith("<") or (code and CODE.match(text)):
        return None, True
    return None


def verbatim(line: str, closer: re.Pattern[str] | None, raw: bool,
             code: bool) -> tuple[re.Pattern[str] | None, bool] | None:
    """`(closer, raw)` after `line` when it is copied as it stands, else None.

    `closer` ends the open fence or HTML block, and the line it is found
    in is the block's last; `raw` is a block a blank line ends.
    """
    if closer:
        return (None if closer.search(line) else closer), False
    if not line.strip() or raw:
        return None, raw and bool(line.strip())
    return opens(line, code)


def item_text(line: str) -> str:
    """What follows `line`'s list markers, nested ones too; "" for no item.

    Five or more spaces after a marker open indented code (sd:5889), so
    the text keeps four of them for `opens` to read.
    """
    text = line
    while (item := LIST_ITEM.match(text)) and not RULE.match(text):
        rest = text[item.end():]
        if len(item.group(2)) > 4 and rest:
            return "    " + rest
        text = rest
    return "" if text is line else text


def starts_block(line: str) -> bool:
    """Whether `line` opens a block, whatever its indent: one of
    `BLOCK_KINDS`, or a list item."""
    body = line.lstrip()
    return bool(LIST_ITEM.match(line)) or any(kind.pattern.match(body) for kind in BLOCK_KINDS)


def wrap_column(line: str) -> int | None:
    """The indent a soft-wrapped line after `line` must have to join it.

    A list item's content column, or a paragraph line's own indent. None
    when nothing may join `line`: it is a block other than a list item, or
    it ends in a hard break, which is checked on the line as written.
    """
    if HARD_BREAK.search(line) or "|" in line or RULE.match(line):
        return None
    item = LIST_ITEM.match(line)
    if item:
        # The item's text is read as a line start: `- # Heading` takes
        # no continuation (sd:5889).
        inner = item_text(line)
        return None if inner and starts_block(inner) else item.end()
    return None if starts_block(line) else len(line) - len(line.lstrip())


def soft_wraps_joined(text: str) -> str:
    """`text` with each soft-wrapped paragraph or list item on one line.

    Markdown reads a single newline inside a paragraph as a space, but a
    Notion write keeps it as a `<br />`, so a source wrapped at 100 columns
    arrived as broken lines (sd:5872). Fenced code, YAML front matter,
    HTML, indented code, headings, quotes, tables, rules and definitions
    are kept line for line, at the top level and inside a list item, as is
    a hard break. A line joins only a paragraph or list item whose text
    starts at the line's own indent. Where a line's kind is in doubt it is
    kept, which costs a `<br />` and never a changed meaning.
    """
    lines = text.split("\n")
    start = front_matter_end(lines)
    out = lines[:start]
    closer: re.Pattern[str] | None = None
    raw = False
    column: int | None = None
    for line in lines[start:]:
        kept = verbatim(line, closer, raw, column is None)
        if kept:
            out.append(line)
            closer, raw = kept
            column = None
            continue
        body = line.lstrip()
        if column == len(line) - len(body) and not starts_block(line):
            # Kept as written after its indent, so a hard break it ends in stays.
            out[-1] = out[-1].rstrip() + " " + body
            column = None if HARD_BREAK.search(line) else column
            continue
        out.append(line)
        # A list item's text can itself open a block (sd:5889).
        inner = item_text(line)
        kept = opens(inner, True) if inner else None
        closer, raw = kept or (None, False)
        column = None if kept else wrap_column(line)
    return "\n".join(out)


def load_record(path: Path) -> dict[str, Any]:
    """The object at `path`, or `{}` for anything that is not one.

    A missing file is the ordinary case -- no request queued, no receipt yet
    -- and is answered without raising; the `try` is for the file that is
    there and unreadable or not JSON.
    """
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def already_queued(path: Path, receipt: Path, wanted: str,
                   identity: dict[str, Any], located: bool = True) -> str:
    """`pending` when the request at `path` already asks for exactly this,
    `delivered` when nothing is pending and the destination already holds
    it, or `""` when the request must be written.

    The queue is read before the receipt, and a request that asks for a
    different generation is overwritten whatever the receipt says. The
    receipt describes the destination; the queue describes what the next
    drain will do to it, and a request left asking for something the source
    no longer says is a drain about to write it: deliver title A, queue the
    rename to B, revert to A before any drain ran -- with the receipt read
    first, A read as delivered and B stayed queued, and the next drain
    renamed the page the source called A. Overwriting costs one rewrite of a
    mirror that already matches; leaving it costs a wrong one.

    Two records, and neither is inferred from the other. `delivered` is what
    a drain wrote, recorded by `mirror_delivered()` at the moment it finished; the
    request's absence is not that record. A drain reads a request, writes
    the mirror, and deletes the file -- and a render that queued newer content
    between the read and the delete had its request deleted too, with nothing
    written for it. Treating the empty slot as "delivered" then suppressed
    that content for ever: remote A, source B, queue empty. So absence says
    nothing, and content the receipt does not name as delivered is queued.

    `pending` is read off the request itself, which carries its own
    fingerprint: a readable file naming the same identity and the same
    content is already asking for exactly this. A truncated or hand-edited
    file is not a request anyone will drain, and is rewritten.

    A receipt that names no URL is not a delivery when the request names no
    page or file either (`located` false): nothing says where a copy went,
    so a drain that acknowledged without `--url` left the document
    "designated, no copy yet" for good (sd:5897). It is queued again, and
    the next drain records where it wrote.
    """
    if path.exists():
        pending = load_record(path)
        if pending.get("fingerprint") != wanted:
            return ""
        if any(pending.get(field) != value for field, value in identity.items()):
            return ""
        return "pending"
    record = load_record(receipt)
    if record.get("delivered") == wanted and (located or record.get("url")):
        return "delivered"
    return ""


def mirror_delivered(name: str, fingerprint: str, url: str = "") -> tuple[int, str]:
    """Record that the drain wrote the generation `fingerprint` of request
    `name`, and remove the request only while it is still that generation.

    The drain's last step, replacing a hand deletion. Deleting the file
    acknowledged whatever it held at that moment, and after a render that
    ran during the drain that was content the drain had not written. This
    acknowledges the generation the drain read -- the `fingerprint` field
    of the request, as it stood in step 1 -- and leaves a newer request in
    place for the next drain.

    Returns the exit status and a one-line report: 0 when every step
    finished, 2 when a request naming an item came with no URL (nothing
    written), 1 when any step did not finish (sd:3979). Recording comes
    before removing: a receipt that says `delivered` with the request still
    present costs one skipped re-render at worst, while a removed request
    with nothing recorded is the defect this exists to close.

    The read, the comparison and the unlink happen under the queue lock. The
    comparison against a record read before the lock, or before the receipt
    write, is the window a render fits in: it wrote B after the read, and
    the unlink took B with A's fingerprint on the receipt.

    `url` is where the drain's mirror landed. It goes on the receipt, and on
    the item the drained generation names (sd:3514, sd:3979).
    """
    try:
        with queue_locked():
            return acknowledge_request(name, fingerprint, url)
    except OSError as problem:
        return 1, "delivered: cannot lock the queue at %s (%s); request left in place" % (
            queue_lock_path(), problem)


def record_item_url(item: Any, repo: str, document: str, title: str, channel: str,
                    url: str) -> tuple[str, str]:
    """Record `url` on `item` in the workflow database: `(answer, "")` with
    `recorded` or `unchanged`, or `("", reason)` when it was not recorded.

    A failed import, open or write is a reason, never a pass: the caller
    leaves its state for the next run. A machine with no database is one too.
    """
    connection, problem = open_database()
    if connection is None:
        return "", problem
    try:
        from sd_db import channels  # noqa: PLC0415
        return channels.record_url(connection, item, repo, document, title, channel, url,
                                   "sd-research-kit"), ""
    except Exception as problem:  # refused or failed: the row is as it was
        return "", str(problem) or type(problem).__name__
    finally:
        connection.close()


def item_named(item: Any, repo: Any) -> str:
    """`system sd:5103`, as a person reads an item (sd:5124); bare `sd:<n>` only when `sd_db` will not import."""
    imported = import_sd_db()
    return imported.module.repos.item_ref(item, repo) if imported.module is not None else "sd:%s" % item


def open_database() -> tuple[Any, str]:
    """The workflow database at `DATABASE_HOME`: `(connection, "")`, or `(None, reason)`.

    A failed import or open is a reason, never a pass; a machine with no
    database is one too.
    """
    imported = import_sd_db()
    if imported.module is None:
        return None, imported.problem
    try:
        return imported.module.connect(home=DATABASE_HOME, busy_timeout=DATABASE_BOUND), ""
    except Exception as problem:  # an unknown is not a pass: every failure is the reason
        return None, "cannot open the workflow database: %s" % (problem or type(problem).__name__)


def record_item_row(item: Any, fingerprint: str, url: str) -> tuple[str, str]:
    """Confirm the row plan of generation `fingerprint` on `item` (sd:3982):
    `(answer, "")` with `recorded` or `unchanged`, or `("", reason)`.
    """
    connection, problem = open_database()
    if connection is None:
        return "", problem
    try:
        from sd_db import projection  # noqa: PLC0415
        return projection.record_row(connection, item, fingerprint, url, "sd-research-kit"), ""
    except Exception as problem:  # refused or failed: the row is as it was
        return "", str(problem) or type(problem).__name__
    finally:
        connection.close()


def acknowledge_request(name: str, fingerprint: str, url: str = "") -> tuple[int, str]:
    """The acknowledgement itself, for a caller holding the queue lock.

    Read here, under the lock, and compared here: the record this reads is
    the one the unlink acts on, because nothing can write the file between
    the two while the lock is held.

    The item record comes first, then the receipt, then the removal, and the
    item is recorded for the generation the drain read only: a newer pending
    request names its own item, which the next drain records. A stop after
    the record leaves the request, and the rerun's record answers `unchanged`.
    """
    path = QUEUE / name
    pending = load_record(path)
    if not pending:
        return 1, "delivered: no readable request at %s" % path
    trashed = trash_answer(name, pending, fingerprint)
    if trashed:
        return trashed
    drained = pending.get("fingerprint") == fingerprint
    item = pending.get("item") if drained else None
    if pending.get("destination") == ROW_DESTINATION:
        # A row's facts are recorded for the generation its plan bound them
        # to, even under a newer request: `record_row` checks that binding on
        # the item, and the file name keeps one item per request (sd:3982).
        item = pending.get("item")
        if not url:
            return 2, ("delivered: %s is a row request; give --url <row url>; nothing "
                       "written" % name)
        _, problem = record_item_row(item, fingerprint, url)
        if problem:
            return 1, ("delivered: %s not recorded (%s); request and receipt left in "
                       "place" % (item_named(item, pending.get("repo")), problem))
    elif item is not None:
        if not url:
            return 2, ("delivered: %s names %s; give --url <url> so the item can link "
                       "it; nothing written" % (name, item_named(item, pending.get("repo"))))
        _, problem = record_item_url(
            item, str(pending.get("repo", "")), str(pending.get("document", "")),
            str(pending.get("title", "")), str(pending.get("destination", "")), url)
        if problem:
            return 1, ("delivered: %s not recorded (%s); request and receipt left in "
                       "place" % (item_named(item, pending.get("repo")), problem))
    receipt = receipts() / name
    record = load_record(receipt)
    record.update(delivered=fingerprint)
    if url:
        record.update(url=url)
    # Where this URL went (sd:5122): the container `enqueue` recorded for this
    # generation, so `delivered_url` reuses it there only. A newer generation
    # queued since names its own container, not this URL's: none is recorded.
    if url and record.get("queued") == fingerprint and isinstance(record.get("identity"), dict):
        record.update(delivered_to=record["identity"])
    else:
        record.pop("delivered_to", None)
    # The item of the generation delivered, or none: a newer request's item
    # is not where this URL went.
    if item is not None:
        record.update(item=item)
    else:
        record.pop("item", None)
    try:
        receipts().mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except OSError as problem:
        return 1, "delivered: cannot write receipt for %s (%s); request left in place" % (
            name, problem)
    if not drained:
        return 0, ("delivered: recorded %s for %s; a newer request is pending and "
                   "stays" % (fingerprint[:12], name))
    try:
        path.unlink()
    except OSError as problem:
        return 1, "delivered: recorded %s for %s; cannot remove the request (%s)" % (
            fingerprint[:12], name, problem)
    return 0, "delivered: recorded %s for %s; request removed" % (fingerprint[:12], name)


def delivered_main(argv: list[str]) -> int:
    """The `delivered` verb: `<request file name> <fingerprint> [--url <url>]`.

    The one verb that takes arguments. R10-D6 forbids a *checkout* argument,
    because a command that can be pointed at a repository can act on one the
    caller is not standing in; this verb acts on the machine's one queue and
    names a request in it, which no working directory could resolve.
    """
    url = ""
    if len(argv) == 4 and argv[2] == "--url":
        url = argv[3]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            print("delivered: --url must be the http(s) address of the mirror, not %r" % url,
                  file=sys.stderr)
            return 2
        argv = argv[:2]
    if len(argv) != 2 or not argv[0] or not argv[1]:
        print("usage: sd-research-kit delivered <request file name> <fingerprint> [--url <url>]",
              file=sys.stderr)
        return 2
    name = Path(argv[0]).name
    status, said = mirror_delivered(name, argv[1], url)
    print(said, file=sys.stdout if status == 0 else sys.stderr)
    return status


#: A request's `action` when it asks the drain to move a copy to the trash,
#: not to mirror one (sd:5492). A mirror request carries no `action`.
TRASH = "trash"
#: What a trash request drops from the mirror request it replaces: the text
#: and the paths a mirror reads. The container stays, for a drain that finds
#: the copy by its pointer.
TRASH_DROPS = ("content", "image", "rendered", "markdown", "revision", "fingerprint", "action")


def enqueue_trash(repo: Path, document: str, destination: str, *, designated: str = "",
                  item: int | None = None, url: str = "") -> tuple[str, str]:
    """Queue the trash of `document`'s `destination` copy, before its
    designation is dropped (sd:5492): `(state, line)`.

    `state` is `queued` (a trash request sits in the slot), `nothing` (no
    request was queued and no copy delivered, so nothing is written), or `""`
    when it refused, with nothing written. The request names the copy by
    everything that records it: `designated` (the `page=` or `file=` the
    designation holds), an id the pending request carries, `url` (the item's
    `fields.published`) and the URL the receipt says was delivered. Where none
    does but a request was pending, a drain may have created the page before
    its step 4, so the request keeps that request's container and the drain
    looks for the copy by its pointer.

    It replaces whatever request sits in the slot: a pending mirror of a
    document no longer designated is not one to drain. A re-tick replaces
    the trash in turn, before any drain, and carries no id from it
    (`recorded`). The receipt notes the trash's generation first, so an
    acknowledgement that finds a newer request in the slot still knows which
    trash it answers (`acknowledge_trash`).
    """
    dest = BY_NAME.get(destination)
    if dest is None:
        return "", "trash: %r is not a destination; expected %s" % (destination, " or ".join(BY_NAME))
    home = repo_home(repo)
    name = "%s.%s.%s.json" % (repo_key(home), document, destination)
    try:
        with queue_locked():
            request, said = trash_request(QUEUE / name, receipts() / name, str(home), document,
                                          dest, designated=designated, item=item, url=url)
            if request:
                write_trash(name, request)
    except OSError as problem:
        return "", "trash: %s not queued (%s)" % (name, problem)
    return ("queued" if request else said[0]), said[1]


def trash_request(path: Path, receipt_path: Path, home: str, document: str, dest: Destination, *,
                  designated: str, item: int | None, url: str) -> tuple[dict[str, Any], tuple[str, str]]:
    """The trash request `enqueue_trash` writes, from the pending request and
    the receipt, and its report; `{}` with the state when it writes none."""
    pending, receipt = load_record(path), load_record(receipt_path)
    refused = trash_unknown(path, pending, receipt_path, receipt, home)
    if refused:
        return {}, ("", refused)
    mine = receipt.get("repo") == home and receipt.get("document") == document
    shipped = receipt.get("url") if mine and receipt.get("delivered") else ""
    copy_id = (designated or str(pending.get(dest.what) or "")).strip()
    url = (url or (shipped if isinstance(shipped, str) else "")).strip()
    if not (copy_id or url or pending):
        return {}, ("nothing", "trash: %s: no %s copy was queued or delivered; nothing to "
                               "trash" % (document, dest.name))
    request = {k: v for k, v in pending.items() if k not in TRASH_DROPS}
    request.update(action=TRASH, repo=home, document=document, destination=dest.name, url=url)
    request[dest.what] = copy_id
    if item is not None:
        request["item"] = item
    request["fingerprint"] = hashlib.sha256(json.dumps(request, sort_keys=True).encode("utf-8")).hexdigest()
    return request, ("queued", "trash: queued %s -> %s" % (
        document, copy_id or url or "the copy its pointer names"))


def trash_unknown(path: Path, pending: dict[str, Any], receipt_path: Path, receipt: dict[str, Any],
                  home: str) -> str:
    """Why `trash_request` cannot tell which copy exists, or "": an unreadable request or receipt is unknown, not none."""
    if path.exists() and not pending:
        return "trash: %s is not a readable request, so whether a copy exists is not known; nothing queued" % path
    if pending and pending.get("repo") != home:
        return "trash: %s holds another checkout's request (%s); nothing queued" % (path, pending.get("repo"))
    if receipt_path.exists() and not receipt:
        return ("trash: %s is not a readable receipt, so whether a copy was delivered is not known; "
                "nothing queued" % receipt_path)
    return ""


def write_trash(name: str, request: dict[str, Any]) -> None:
    """The receipt's note of the trash's generation, then the request itself, atomically."""
    receipt = load_record(receipts() / name)
    receipt.update({field: request[field] for field in ("repo", "document", "destination")})
    receipt.update(trashing={"fingerprint": request["fingerprint"], "item": request.get("item")})
    # An earlier trash of the same copy left its mark: this one is not done until its own drain says so.
    receipt.pop("trashed", None)
    receipts().mkdir(parents=True, exist_ok=True)
    (receipts() / name).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    QUEUE.mkdir(parents=True, exist_ok=True)
    path = QUEUE / name
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, path)


def drop_item_url(item: Any, repo: str, document: str, channel: str) -> tuple[str, str]:
    """`record_item_url`'s inverse, for a trashed copy: `(answer, "")` with
    `dropped` or `unchanged`, or `("", reason)`."""
    connection, problem = open_database()
    if connection is None:
        return "", problem
    try:
        from sd_db import channels  # noqa: PLC0415
        return channels.drop_url(connection, item, repo, document, channel, "sd-research-kit"), ""
    except Exception as problem:  # refused or failed: the row is as it was
        return "", str(problem) or type(problem).__name__
    finally:
        connection.close()


def trash_answer(name: str, pending: dict[str, Any], fingerprint: str) -> tuple[int, str] | None:
    """`delivered`'s answer when `fingerprint` is a trash's generation, or None for a mirror's."""
    held = load_record(receipts() / name)
    trashing = held.get("trashing")
    if pending.get("action") != TRASH and held.get("trashed") == fingerprint:
        # A rerun of a trash already acknowledged, with a newer request in the slot: nothing more to record.
        return 0, "delivered: trashed %s for %s already; a newer request is pending and stays" % (
            fingerprint[:12], name)
    if pending.get("action") == TRASH or (isinstance(trashing, dict)
                                          and trashing.get("fingerprint") == fingerprint):
        return acknowledge_trash(name, pending, fingerprint)
    return None


def acknowledge_trash(name: str, pending: dict[str, Any], fingerprint: str) -> tuple[int, str]:
    """`delivered` for a trash request, under the queue lock (sd:5492).

    The drain moved the copy to the trash; this takes its URL off the item,
    then off the receipt, then removes the request while it is still that
    generation. The copy is gone whatever is pending now, so a newer request
    a re-tick queued meanwhile stays, and the URL still goes: its drain finds
    no page and creates one. A step that does not finish leaves the request
    and exits 1; the rerun's drop answers `unchanged`.
    """
    path = QUEUE / name
    receipt = load_record(receipts() / name)
    trashing = receipt.get("trashing")
    if not isinstance(trashing, dict):
        trashing = {}
    drained = pending.get("action") == TRASH and pending.get("fingerprint") == fingerprint
    if not drained and trashing.get("fingerprint") != fingerprint:
        return 1, ("delivered: %s is a trash request of another generation than %s; nothing "
                   "recorded" % (name, fingerprint[:12]))
    item = pending.get("item") if drained else trashing.get("item")
    repo, document = (str(pending.get(k) or receipt.get(k) or "") for k in ("repo", "document"))
    destination = str(pending.get("destination") or receipt.get("destination") or "")
    if item is not None:
        _, problem = drop_item_url(item, repo, document, destination)
        if problem:
            return 1, ("delivered: the trashed copy's URL is still on %s (%s); request and "
                       "receipt left in place" % (item_named(item, repo), problem))
    for field in ("delivered", "url", "delivered_to", "item", "trashing"):
        receipt.pop(field, None)
    receipt.update(trashed=fingerprint)
    try:
        receipts().mkdir(parents=True, exist_ok=True)
        (receipts() / name).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    except OSError as problem:
        return 1, "delivered: cannot write receipt for %s (%s); request left in place" % (name, problem)
    if not drained:
        return 0, ("delivered: trashed %s for %s; a newer request is pending and stays"
                   % (fingerprint[:12], name))
    try:
        path.unlink()
    except OSError as problem:
        return 1, "delivered: trashed %s for %s; cannot remove the request (%s)" % (
            fingerprint[:12], name, problem)
    return 0, "delivered: trashed %s for %s; request removed" % (fingerprint[:12], name)


#: The destination of an item's row request (sd:3982). Not a row in
#: `DESTINATIONS`: that table is the keys a DOCS entry may name, and an item is
#: not a document, so a `notion-row=` key on a document stays a typo.
ROW_DESTINATION = "notion-row"

#: Where rows go: each target's data source id, a setting beside the two
#: Notion folder ids. Never defaulted: an id belongs to one workspace.
ROW_DATABASES = (("experiments", "sd.notion_experiments"), ("tasks", "sd.notion_experiment_tasks"))


def row_databases() -> tuple[dict[str, str], str]:
    """Each target's database id, hyphenated lowercase, or `({}, reason)` naming the setting.

    A bare id, a hyphenated one and `collection://<id>` are accepted; a name
    is not, for the reason `NotionScope` gives for folders.
    """
    found: dict[str, str] = {}
    for target, setting in ROW_DATABASES:
        value, unknown = publish_setting(setting)
        if unknown:
            return {}, unknown
        named = notion_id(value.removeprefix("collection://")) if value else ""
        if not named:
            return {}, ("%s %s; run `sd config set %s <data source id>` with the %s database's id"
                        % (setting, "holds no id" if value else "is unset", setting, target))
        found[target] = str(uuid.UUID(named))
    return found, ""


def row_request_name(item: Any) -> str:
    """The queue file of item `item`'s row request: one per item, whatever its target."""
    return "sd-item-%s.%s.json" % (item, ROW_DESTINATION)


def project() -> tuple[int, list[str]]:
    """Queue one `notion-row` request per designated item whose row would change (sd:3982).

    Reads every item first and writes after: a read that fails (no database,
    a hub that does not answer, a build mismatch, a restore open) writes
    nothing and leaves earlier requests as they were. An item whose
    designation lists a `yielded` field is queued again whatever the receipt
    says, so each drain sees a hand-back. A request pending for an item no
    longer designated is named with its path, never deleted. Exit 1 when
    anything was not queued; the requests written still stand.
    """
    databases, problem = row_databases()
    if problem:
        return 1, ["project: %s; nothing queued" % problem]
    connection, problem = open_database()
    if connection is None:
        return 1, ["project: %s; nothing queued, and requests queued earlier stay" % problem]
    problems: list[str] = []
    requests: list[tuple[dict[str, Any], dict[str, Any]]] = []
    try:
        from sd_db import projection  # noqa: PLC0415
        from sd_db.writes import unresolved_state  # noqa: PLC0415
        if unresolved_state(connection, "restore"):
            return 1, ["project: a restore is unresolved, so the item rows may be older than the "
                       "receipts; nothing queued, and requests queued earlier stay"]
        for row, held in projection.designated_rows(connection, problems):
            if held["as"] == "off":
                continue
            try:
                requests.append((projection.request_for(connection, row, held, databases), held))
            except projection.ProjectionRefusal as refusal:
                problems.append("%s: %s" % (item_named(row["id"], row["repo"]), refusal))
    except Exception as error:  # an unknown is not a pass: nothing is written
        return 1, ["project: cannot read the designated items (%s); nothing queued, and requests "
                   "queued earlier stay" % (error or type(error).__name__)]
    finally:
        connection.close()
    reports = ["project: %s; not queued" % line for line in problems]
    try:
        QUEUE.mkdir(parents=True, exist_ok=True)
        with queue_locked():
            reports += write_row_requests(requests)
    except OSError as error:
        return 1, reports + ["project: cannot write the queue at %s (%s)" % (QUEUE, error)]
    return (1 if problems else 0), reports


def write_row_requests(requests: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[str]:
    """Write each request that is neither pending nor delivered, for a caller holding the queue lock."""
    reports: list[str] = []
    named = set()
    for request, held in requests:
        name = row_request_name(request["item"])
        named.add(name)
        path = QUEUE / name
        state = already_queued(path, receipts() / name, request["fingerprint"], {})
        if state == "delivered" and held.get("yielded"):
            state = ""
        if state == "pending":
            reports.append("project: %s: already queued for %s (unchanged): %s"
                           % (item_named(request["item"], request["repo"]), request["target"], path))
            continue
        if state == "delivered":
            reports.append("project: %s: unchanged since it was last projected; not queued again"
                           % item_named(request["item"], request["repo"]))
            continue
        path.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        reports += record_queued(name, dict(queued=request["fingerprint"], repo=request["repo"],
                                            document=request["document"], destination=ROW_DESTINATION))
        held_back = " (sd holds no claim on %s until handed back)" % ", ".join(held["yielded"]) \
            if held.get("yielded") else ""
        reports.append("project: queued %s -> %s%s" % (item_named(request["item"], request["repo"]),
                                                         request["target"], held_back))
    for path in sorted(QUEUE.glob(row_request_name("*"))):
        if path.name not in named:
            reports.append("project: %s is pending for an item no longer designated; --plan refuses it "
                           "as stale. Delete it by hand with no drain running" % path)
    return reports


def row_request(path: Path, verb: str) -> tuple[dict[str, Any], str]:
    """The `notion-row` request at `path`, or `({}, reason)`.

    The request must sit under its own item's name, so the mark and the
    `delivered` that follow act on the request planned.
    """
    request = load_record(path)
    if request.get("destination") != ROW_DESTINATION:
        return {}, "%s: %s is not a readable %s request" % (verb, path, ROW_DESTINATION)
    if path.name != row_request_name(request.get("item")):
        return {}, "%s: %s names sd:%s, whose request is %s" % (
            verb, path, request.get("item"), row_request_name(request.get("item")))
    return request, ""


def plan_request(path: Path, live_path: Path | None) -> tuple[int, str]:
    """`project --plan`: the guard's answer for one request, as JSON, or `(1, reason)`.

    `live_path` None is `--missing`.
    """
    request, problem = row_request(path, "plan")
    if problem:
        return 1, problem
    live: Any = None
    if live_path is not None:
        try:
            live = json.loads(live_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return 1, "plan: cannot read the live fetch %s (%s)" % (live_path, error)
        if not isinstance(live, dict):
            return 1, "plan: the live fetch %s is not an object" % live_path
    databases, problem = row_databases()
    if problem:
        return 1, "plan: %s" % problem
    connection, problem = open_database()
    if connection is None:
        return 1, "plan: %s" % problem
    try:
        from sd_db import projection  # noqa: PLC0415
        planned = projection.plan(connection, request, live, databases=databases, who="sd-research-kit")
    except Exception as error:  # refused or failed: nothing was recorded
        return 1, "plan: %s" % (str(error) or type(error).__name__)
    finally:
        connection.close()
    return 0, json.dumps(planned, indent=2, sort_keys=True)


def mark_request(path: Path) -> tuple[int, str]:
    """`project --writing`: the drain's mark just before its create call, or `(1, reason)` (sd:4742).

    The next plan reads the mark as "the create may have landed" and searches
    for the row; without it, a plan-only run reads as nothing written.
    """
    request, problem = row_request(path, "writing")
    if problem:
        return 1, problem
    connection, problem = open_database()
    if connection is None:
        return 1, "writing: %s" % problem
    try:
        from sd_db import projection  # noqa: PLC0415
        projection.mark_writing(connection, request["item"], request.get("fingerprint"))
    except Exception as error:  # refused or failed: nothing was recorded
        return 1, "writing: %s" % (str(error) or type(error).__name__)
    finally:
        connection.close()
    return 0, "writing: %s's create is marked; create the row now, in one call, then run delivered" % (
        item_named(request["item"], request.get("repo")))


def project_main(argv: list[str]) -> int:
    """The `project` verb: `project`, `project --plan <request> --live <file>|--missing`, or
    `project --writing <request>`."""
    if not argv:
        status, reports = project()
        for line in reports:
            print(line, file=sys.stdout if status == 0 else sys.stderr)
        return status
    if argv[:1] == ["--plan"] and len(argv) in (3, 4) and argv[1]:
        if argv[2:] == ["--missing"]:
            status, said = plan_request(Path(argv[1]), None)
        elif len(argv) == 4 and argv[2] == "--live" and argv[3]:
            status, said = plan_request(Path(argv[1]), Path(argv[3]))
        else:
            status, said = 2, ""
        if status != 2:
            print(said, file=sys.stdout if status == 0 else sys.stderr)
            return status
    if argv[:1] == ["--writing"] and len(argv) == 2 and argv[1]:
        status, said = mark_request(Path(argv[1]))
        print(said, file=sys.stdout if status == 0 else sys.stderr)
        return status
    print("usage: sd-research-kit project [--plan <request> --live <file>|--missing] [--writing <request>]",
          file=sys.stderr)
    return 2


def enqueue(repo: Path, docs: list[dict[str, Any]],
            channels: dict[str, dict[str, str]] | None = None) -> list[str]:
    """Write one sync request per document per designated destination, for
    every document whose mirror would differ from the one last queued.

    Unchanged documents are skipped, and said to be. Every render used to
    re-queue every designated document -- eight requests per commit in one
    repository -- and each drain then rewrote eight mirrors nobody had
    touched. `SD_MIRROR_REQUEUE=1` queues them all regardless, for a mirror
    that was lost on the far side and has to be written again.

    A linked worktree queues nothing unless `SD_PUBLISH_FROM_WORKTREE=1`
    says to; see `publish_refusal`. Nothing is queued, and nothing pending is
    touched, while `SD_SKIP_MIRROR` holds a reason; see `SKIP_MIRROR`.
    `SD_MIRROR_DRY_RUN=1` reports each request it would write and writes
    none; see `MIRROR_DRY_RUN`.

    `channels` is the repo's setting (sd:3462): see `mirror_targets` for the
    folders, and `stale_requests` for a pending request it now refuses.
    """
    held = str(ENVIRON.get(SKIP_MIRROR, "")).strip()
    if held:
        return ["mirror: not queued: %s (%s)" % (held, SKIP_MIRROR)]
    refused = publish_refusal(repo, "mirror")
    if refused:
        return [refused]
    home = repo_home(repo)
    wanted, reports = wanted_targets(docs, home, channels)
    if not wanted:
        return reports

    dry = bool(ENVIRON.get(MIRROR_DRY_RUN))
    if not dry:
        QUEUE.mkdir(parents=True, exist_ok=True)
    rev = revision(repo)
    key = repo_key(home)
    vault = vault_folder(home, channels)
    force = bool(ENVIRON.get("SD_MIRROR_REQUEUE"))
    with queue_locked():
        for cfg, target in wanted:
            out = str(cfg.get("out", "")).strip()
            try:
                request = mirror_request(repo, home, vault, cfg, target, rev)
                name = "%s.%s.%s.json" % (key, out or "doc", target["destination"])
                path = QUEUE / name
                what = BY_NAME[target["destination"]].what
                identity = mirror_identity(request, target, what)
                digest = mirror_fingerprint(request, target, what)
                state = "" if force else already_queued(path, receipts() / name, digest, identity,
                                                        bool(request[what]))
                if state:
                    reports.append(unchanged_report(state, out, target["target"], rev))
                    continue
                # The designation is the answer where it gives one; the queue records
                # what a drain did, and never overrides what the document says.
                # A writing piece's file keeps its copy across its two folders
                # (pack sd:7243): the drain moves it from Drafts to Published.
                carry = carried_identity(identity, cfg, BY_NAME[target["destination"]].where)
                carried = "" if request[what] else (recorded(path, carry, what)
                                                    or delivered_url(receipts() / name, carry, cfg))
                request[what] = request[what] or carried
                reverted = not force and reverts_to_delivered(path, name, digest)
                if dry:
                    reports.append(queued_report(out, target["target"], what, carried, reverted,
                                                 "would queue") + " (%s)" % MIRROR_DRY_RUN)
                    continue
                failed = drawn(request)
                if failed:
                    reports.append("mirror: %s: %s; not queued" % (out, failed))
                    continue
                write_request(path, request, digest)
                reports += record_queued(name, dict(
                    queued=digest, revision=rev, repo=str(home), document=out,
                    destination=target["destination"], identity=identity if isinstance(cfg.get("content"), str) else None))
                reports.append(queued_report(out, target["target"], what, carried, reverted))
            except OSError as problem:  # one file's request held, named; the rest still queue (pack sd:7243 review)
                reports.append("mirror: %s: the request was not written: %s; not queued"
                               % (out, problem.strerror or problem))
        if not dry:
            prune_images()
    return reports


def wanted_targets(docs: list[dict[str, Any]], home: Path, channels: dict[str, dict[str, str]] | None
                   ) -> tuple[list[tuple[dict[str, Any], dict[str, str]]], list[str]]:
    """Each `(document, target)` pair `enqueue` writes, and its reports so far."""
    reports = stale_requests(home, channels)
    wanted: list[tuple[dict[str, Any], dict[str, str]]] = []
    for cfg in docs:
        targets, problems = mirror_targets(cfg, home, channels)
        problems += designated_item(cfg)[1]
        reports += ["mirror: %s in %s" % (p, cfg.get("out", "?")) for p in problems]
        for target in targets:
            out = str(cfg.get("out", "")).strip()
            name = "%s.%s.%s.json" % (repo_key(home), out or "doc", target["destination"])
            owner = foreign_owner(name, home)
            if owner:
                reports.append("mirror: %s: %s belongs to %s, a repo of the same name; not queued"
                               % (out, name, owner))
            else:
                wanted.append((cfg, target))
    return wanted, reports


def unchanged_report(state: str, out: str, target: str, rev: str) -> str:
    """What `enqueue` says of a document `already_queued` found `pending` or `delivered`."""
    if state == "pending":
        return "mirror: %s: already queued -> %s (unchanged since %s)" % (out, target, rev[:12])
    return "mirror: %s: unchanged since it was last mirrored; not queued again" % out


def drawn(request: dict[str, Any]) -> str:
    """Take the SVG off the request's `image` and draw its PNG if it is not
    there yet: "" when the request may be written, else why not (sd:6066).

    Drawn before the request is written, so no request names a PNG that is
    not there; a failed draw leaves the slot as it was.
    """
    image = request.get("image")
    if not image:
        return ""
    svg, png = image.pop("svg"), Path(image["path"])
    return "" if png.is_file() else render_png(svg, png)


def write_request(path: Path, request: dict[str, Any], digest: str) -> None:
    """Write `request` as the generation `digest`: what `mirror_delivered()` acknowledges."""
    request["fingerprint"] = digest
    # Beside it and renamed into place (sd:8825): a failed write keeps the last
    # request whole. A short dot name: no `*.json` glob reads it as a request,
    # and a request name near the 255-byte bound still fits.
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".part")
    os.close(handle)
    part = Path(name)
    try:
        part.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)


def foreign_owner(name: str, home: Path) -> str:
    """The other repo that owns request `name`, or "" (sd:8825).

    `repo_key` is the directory's name, so two repos of one name share each
    request name; the request or receipt already there keeps its owner.
    A pending request keeps its owner even when that checkout is gone: it
    holds content no drain has published, and writing over it loses that.
    Only a drained receipt, its request removed, of an owner that is no
    directory any more owns nothing: it would refuse this repo for good
    (sd:8990).
    """
    owner = load_record(QUEUE / name).get("repo")
    if owner and owner != str(home):
        return str(owner)
    owner = load_record(receipts() / name).get("repo")
    if owner and owner != str(home) and Path(owner).is_dir():
        return str(owner)
    return ""


def stale_requests(home: Path, channels: dict[str, dict[str, str]] | None) -> list[str]:
    """Each pending request of this repo for a channel its setting leaves out.

    Refused, never deleted (sd:3462): drain step 2 reads the setting and
    leaves such a request unwritten, and a deletion here could race a drain
    that passed step 2 before the setting changed. Named on every render,
    with its path, until the operator deletes it with no drain running.
    """
    if channels is None:
        return []
    reports = []
    for path in sorted(QUEUE.glob("%s.*.json" % repo_key(home))):
        request = load_record(path)
        channel = request.get("destination")
        if request.get("repo") == str(home) and channel in BY_NAME and channel not in channels:
            reports.append("mirror: %s: not a channel of this repo; the pending request %s stays "
                           "and drain step 2 refuses it. Delete it by hand with no drain running"
                           % (channel, path))
    return reports


def mirror_request(repo: Path, home: Path, vault: Path | None, cfg: dict[str, Any],
                   target: dict[str, str], rev: str) -> dict[str, Any]:
    """The request `enqueue` writes for one document and one destination,
    before its fingerprint and any carried id."""
    out = str(cfg.get("out", "")).strip()
    src = str(cfg.get("src", "")).strip()
    # A routine brief (sd:5102, `brief_docs`) is a row, not a file: it carries
    # its text, and its source is the item. A writing piece's file (pack
    # sd:7243, `piece_docs`) carries its text too, and names its own source,
    # `sd:<item>/<file>`; it has no vault copy.
    brief = isinstance(cfg.get("content"), str)
    piece = brief and isinstance(cfg.get("source"), str)
    request = {
        # The repository, not the checkout: this is what `sd-status`
        # resolves a row against and what a carried id is compared on.
        # The three paths below are the checkout's, because that is where
        # the rendered files are.
        "repo": str(home),
        "revision": rev,
        "document": out,
        "title": cfg.get("title", out),
        "source": cfg["source"] if piece else "sd:%s" % cfg.get("item") if brief else str(repo / src) if src else "",
        # The pointer line's link, read from `origin` and never
        # composed by the drain; None where origin names no GitHub repo.
        "source_url": source_url(repo, src),
        "rendered": "" if brief else str(repo / DASHBOARD_DIR / (out + ".html")),
        "markdown": str(vault / (out + ".md")) if vault and not piece else "",
        # What the drain mirrors, read once here. The three paths
        # above say where it came from and where the copies sit;
        # none of them is what gets published. Joined here, once, for
        # every destination and so for the fingerprint too (sd:5872).
        "content": cfg["content"] if brief else repo_links(source_text(repo / src), repo, src) if src else "",
    }
    # A Notion page shows the Archify diagram as an image, and only it
    # (sd:6066): Drive and the served page keep the text as written.
    if not brief and target.get("destination") == "notion":
        request["content"], image = notion_diagram(request["content"])
        if image:
            request["image"] = image
    request["content"] = soft_wraps_joined(request["content"])
    request.update(target)
    request.update(designated_item(cfg)[0])
    if cfg.get("moves") is True:  # the drain moves a carried copy into the folder (pack sd:7243)
        request["moves"] = True
    return request


#: The line a Notion request's `content` holds where the Archify diagram was:
#: the drain replaces it with the image line `upload` prints (sd:6066).
IMAGE_MARKER = "{{archify-image}}"
#: The 05-architecture template's diagram: the `diagram` div, then the plain
#: diagram in the fenced `text` block right after it, if one is there.
ARCHIFY_DIAGRAM = re.compile(
    r'^<div class="diagram">[ \t]*\n(?P<body>.*?)\n</div>[ \t]*$'
    r'(?P<plain>(?:\n[ \t]*)*\n```text[ \t]*\n.*?\n```[ \t]*$)?', re.M | re.S)
SVG = re.compile(r"<svg\b.*</svg>", re.S)
SVG_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.S)


def images() -> Path:
    """Where `enqueue` keeps the PNG of each Notion request's diagram.

    Beside the queue, as `receipts()` is, and named by the SVG's digest: a
    PNG never changes once written, so a pending request's image is the one
    it was queued with, whatever a later render writes.
    """
    return QUEUE.with_name(QUEUE.name + "-images")


#: What `drawn` leaves in `images()`: a PNG, or one a killed draw left partial.
IMAGE_FILE = re.compile(r"[0-9a-f]{32}\.png(?:\.partial)?")


def prune_images() -> None:
    """Remove each PNG in `images()` no pending request names (sd:6081).

    Called under the queue lock, so no render writes or draws meanwhile,
    and after a render's draws, so an unchanged diagram is not drawn again.
    A request that cannot be read may name any PNG, so then nothing goes.
    """
    named = set()
    for path in QUEUE.glob("*.json"):
        try:
            image = json.loads(path.read_text(encoding="utf-8")).get("image")
        except (OSError, ValueError, AttributeError):
            return
        if isinstance(image, dict) and isinstance(image.get("path"), str):
            named.add(Path(image["path"]).name)
    try:
        found = list(images().iterdir())
    except OSError:
        return
    for png in found:
        if IMAGE_FILE.fullmatch(png.name) and png.name not in named:
            png.unlink(missing_ok=True)


def notion_diagram(text: str) -> tuple[str, dict[str, str]]:
    """`text` for a Notion page, and the image to upload, or `{}`.

    The first `diagram` div that holds an `<svg>` becomes `IMAGE_MARKER`,
    and the plain `text` block right after it goes: Notion drops an inline
    SVG, and the operator ruled the image the page's one diagram (sd:6066).
    A div with no SVG, the template's comment, leaves the text as it is, so
    a document drawn without Archify keeps its plain diagram.
    """
    for match in ARCHIFY_DIAGRAM.finditer(text):
        svg = SVG.search(match["body"])
        if not svg:
            continue
        digest = hashlib.sha256(svg.group(0).encode("utf-8")).hexdigest()
        title = SVG_TITLE.search(svg.group(0))
        caption = " ".join(html.unescape(title.group(1)).split()) if title else ""
        image = {"marker": IMAGE_MARKER, "sha256": digest, "path": str(images() / ("%s.png" % digest[:32])),
                 "caption": caption or "Architecture diagram (Archify)", "svg": svg.group(0)}
        return text[:match.start()] + IMAGE_MARKER + text[match.end():], image
    return text, {}


def chrome() -> str:
    """The browser `render_png` runs, or "": Chrome draws the SVG the way the
    served page does; librsvg missed its CSS variables and drew it black."""
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    app = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return str(app) if app.is_file() else ""


def svg_size(svg: str) -> tuple[int, int] | None:
    """The SVG's width and height in pixels, from its attributes or its viewBox."""
    tag = svg[:svg.find(">") + 1]
    width, height = (re.search(r'\s%s="([0-9.]+)(?:px)?"' % name, tag) for name in ("width", "height"))
    if width and height:
        return round(float(width.group(1))), round(float(height.group(1)))
    box = re.search(r'\sviewBox="\s*[-0-9.]+[\s,]+[-0-9.]+[\s,]+([0-9.]+)[\s,]+([0-9.]+)\s*"', tag)
    return (round(float(box.group(1))), round(float(box.group(2)))) if box else None


def light_svg(svg: str) -> str:
    """`svg` with the light theme on its own tag: an Archify export keeps
    `data-theme="dark"` there, and its `svg[data-theme="dark"]` rules beat
    the page's light theme, drawing dark labels on a white page."""
    end = svg.find(">") + 1
    tag = re.sub(r'\sdata-theme="[^"]*"', "", svg[:end])
    return tag.replace("<svg", '<svg data-theme="light"', 1) + svg[end:]


def render_png(svg: str, png: Path) -> str:
    """Draw `svg` into `png` at twice its size, in the light theme: "" when
    done, else why not. The PNG lands whole or not at all."""
    size = svg_size(svg)
    if not size or not all(size):
        return "the Archify SVG names no size (width and height, or a viewBox)"
    browser = chrome()
    if not browser:
        return "no Chrome found to draw the Archify diagram as a PNG"
    page = ('<!doctype html><html data-theme="light"><head><meta charset="utf-8"><style>'
            "html,body{margin:0;background:#fff}svg{display:block}</style></head><body>%s</body></html>"
            % light_svg(svg))
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        (work / "page.html").write_text(page, encoding="utf-8")
        shot = work / "shot.png"
        try:
            ran = subprocess.run(
                [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                 "--no-default-browser-check", "--user-data-dir=%s" % (work / "profile"),
                 "--force-device-scale-factor=2", "--window-size=%d,%d" % size,
                 "--screenshot=%s" % shot, (work / "page.html").as_uri()],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as problem:
            return "Chrome did not draw the Archify diagram: %s" % problem
        try:
            data = shot.read_bytes()
        except OSError:
            data = b""
        if not data.startswith(b"\x89PNG"):
            return "Chrome wrote no PNG of the Archify diagram (exit %d)" % ran.returncode
        png.parent.mkdir(parents=True, exist_ok=True)
        partial = png.with_name(png.name + ".partial")
        partial.write_bytes(data)
        os.replace(partial, png)
    return ""


#: Where Notion's `notion-create-file-upload` says to send the bytes.
NOTION_UPLOAD = re.compile(r"/v1/(?:mcp/)?file_uploads/([0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?"
                           r"[0-9a-f]{4}-?[0-9a-f]{12})/send")


def upload_image(name: str, url: str, authorization: str, *,
                 opener: Any = None) -> tuple[int, str]:
    """Send request `name`'s PNG to the upload Notion opened at `url`:
    `(0, image line)`, `(1, reason)` for a send that failed, or `(2, reason)`
    for one refused before anything was sent.

    The file is the request's own image, in `images()`, never a path the
    request was edited to name; the address is Notion's upload endpoint, and
    the answer must say that upload holds the file. Anything else is a
    failure, not an image: the drain leaves the request (sd:6066).
    """
    opener = opener or urllib.request.urlopen
    parts = urlsplit(url)
    sent_to = NOTION_UPLOAD.fullmatch(parts.path)
    if parts.scheme != "https" or parts.netloc != "api.notion.com" or not sent_to or parts.query:
        return 2, "upload: %r is not a Notion file upload address" % url
    if not authorization.strip() or any(ch in authorization for ch in "\r\n"):
        return 2, "upload: the authorization is empty or spans lines"
    request = load_record(QUEUE / name)
    data, refused = upload_file(name, request.get("image"))
    if refused:
        return 2, refused
    image = request["image"]
    boundary = uuid.uuid4().hex
    body = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s.png"\r\n'
            "Content-Type: image/png\r\n\r\n" % (boundary, request.get("document") or "diagram")).encode("utf-8")
    body += data + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    send = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": authorization.strip(), "Content-Type": "multipart/form-data; boundary=%s" % boundary})
    try:
        with contextlib.closing(opener(send, timeout=60)) as answer:
            said = json.loads(answer.read().decode("utf-8"))
    except (OSError, ValueError) as problem:
        return 1, "upload: the send to Notion failed (%s); the request stays" % problem
    upload_id = str(said.get("file_upload_id", "")) if isinstance(said, dict) else ""
    # A list, a string or null confirms nothing (sd:6082).
    if (not isinstance(said, dict) or said.get("status") != "uploaded"
            or upload_id.replace("-", "") != sent_to.group(1).replace("-", "")):
        return 1, "upload: Notion did not confirm upload %s (%r); the request stays" % (sent_to.group(1), said)
    caption = html.escape(str(image.get("caption") or ""), quote=False)
    return 0, '<image source="file-upload://%s">%s</image>' % (upload_id, caption)


def upload_file(name: str, image: Any) -> tuple[bytes, str]:
    """The PNG request `name`'s `image` names, or why it is not sent: only
    a PNG in `images()`, never a path the request was edited to name."""
    if not isinstance(image, dict) or not isinstance(image.get("path"), str):
        return b"", "upload: %s names no image to upload" % name
    png = Path(image["path"])
    if png.parent.resolve() != images().resolve() or png.suffix != ".png":
        return b"", "upload: %s names an image outside %s" % (name, images())
    try:
        data = png.read_bytes()
    except OSError as problem:
        return b"", "upload: %s cannot be read (%s); render again" % (png, problem)
    if not data.startswith(b"\x89PNG"):
        return b"", "upload: %s is not a PNG; render again" % png
    return data, ""


def upload_main(argv: list[str]) -> int:
    """The `upload` verb: `<request file name> <upload_url> <authorization>`."""
    if len(argv) != 3 or not all(argv):
        print("usage: sd-research-kit upload <request file name> <upload_url> <authorization>",
              file=sys.stderr)
        return 2
    status, said = upload_image(Path(argv[0]).name, argv[1], argv[2])
    print(said, file=sys.stdout if status == 0 else sys.stderr)
    return status


def designated_item(cfg: dict[str, Any]) -> tuple[dict[str, int], list[str]]:
    """`{"item": <n>}` for the sd item a DOCS entry names, and what was wrong.

    Where `delivered --url` and the render's vault link land the URL
    (sd:3514, sd:3979). Part of the fingerprint when present, so naming or
    changing an item queues the document once and the next drain records
    its URL there. A bad value is reported and not carried.
    """
    item = cfg.get("item")
    if item is None:
        return {}, []
    if type(item) is not int or item < 1:
        return {}, ["item= must be an sd item number such as 3465, not %r" % (item,)]
    return {"item": item}, []


def brief_docs(
    connection: Any, items: set[int] | None = None
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Each repository's DOCS entries for the routine briefs their items
    designate, and what was wrong (sd:5102).

    A brief is a row (`sd_db.routine_briefs`), so its item's `fields.publish`
    holds the designation, with the `notion` and `drive` keys a
    research.conf.py entry takes. The entry carries the row's text as
    `content` and its item; `mirror_request` names the item as the source.

    Nothing here pins a copy: `enqueue` carries the URL drain step 6 left
    on the receipt, and only into the container it went to (`delivered_url`).
    Fields that are not an object, or a `publish` that is not one, are
    reported and not queued.
    """
    docs: dict[str, list[dict[str, Any]]] = {}
    problems: list[str] = []
    rows = connection.execute(
        "SELECT id, repo, title, body, fields FROM item "
        "WHERE source = 'brief' AND kind = 'report' ORDER BY id")
    for row in rows:
        if items is not None and row["id"] not in items:
            continue
        try:
            fields = json.loads(row["fields"] or "{}")
        except ValueError:
            fields = None
        designation = fields.get("publish") if isinstance(fields, dict) else None
        if designation is None and isinstance(fields, dict):
            continue
        if not isinstance(designation, dict) or not row["repo"]:
            problems.append("mirror: sd:%s: its fields.publish is not an object on "
                            "a brief with a repository; not queued" % row["id"])
            continue
        try:
            text = json.loads(row["body"] or "{}").get("text")
        except (ValueError, AttributeError):
            text = None
        if not isinstance(text, str):
            problems.append("mirror: sd:%s: the brief stores no text; not queued"
                            % row["id"])
            continue
        entry: dict[str, Any] = {"out": row["title"], "title": row["title"],
                                 "item": row["id"], "content": text}
        entry.update({destination: dict(designation[destination]) for destination in ("notion", "drive")
                      if isinstance(designation.get(destination), dict)})
        docs.setdefault(row["repo"], []).append(entry)
    return docs, problems


def enqueue_briefs(connection: Any, items: set[int] | None = None) -> list[str]:
    """Queue every designated routine brief (`brief_docs`) through `enqueue`,
    with its repository's channels setting.

    The same request, fingerprint and receipt as a document's, so an
    unchanged brief is not queued again. A setting that cannot be read holds
    that repository's briefs, as it holds a render's queue.
    """
    return enqueue_rows(connection, *brief_docs(connection, items), "brief")


#: A writing piece's Drive folder by its stage, the same two in each account
#: (pack sd:7243, note #40909). A stage not here mirrors nothing.
PIECE_FOLDERS = {"review": "Writing/Drafts", "ready": "Writing/Drafts", "published": "Writing/Published"}
#: The realm whose pieces go to the work account; every other realm's go to
#: the personal one (note #40909).
WORK_REALM = "work"


def piece_accounts(connection: Any) -> Callable[[str], tuple[str, str]]:
    """A function from a piece's repo to its Drive account, `(address, "")`,
    or `("", why not)` (pack sd:7243).

    The realm is the dashboard's `places.realm_of`, the rule the Writing page
    and Tasks use; the address is `realm_sweep.account` of `work` for the work
    realm and of `personal` for any other. Both are read once, here. A realm
    list that did not read whole, unset, or a setting that did not read is
    unknown: the piece is held, never sent to the personal Drive by default.
    """
    import importlib  # noqa: PLC0415
    dashboard = str(Path(__file__).resolve().parent.parent / "dashboard")
    if dashboard not in sys.path:
        sys.path.insert(0, dashboard)
    # By name, as `sd_controls` loads the dashboard: it is not on bin/'s lint path.
    places = importlib.import_module("sd_dashboard.places")
    sweep = importlib.import_module("sd_dashboard.realm_sweep")
    import_sd_db()
    from sd_db import writes  # noqa: PLC0415 - the checkout's `lib/`

    found = places.realms(connection)
    # Only a list read whole picks an account: a skipped line may be the one
    # that defines `work`, and `realm_of` would send its repos to personal
    # (lead review). No list set is unknown too: which realm is work is the
    # operator's to say.
    rules = found["list"] if found["state"] == "read" else None
    unread = "" if rules is not None else "no realm list is set (dashboard.realms)" \
        if found["state"] == "missing" else "the realm list read in part (%s)" % (
            "; ".join(found["problems"])) if found["state"] == "partial" \
        else "the realm list did not read (%s)" % ("; ".join(found["problems"]) or found["state"])
    try:
        rows = writes.read_settings(connection, [sweep.ACCOUNT.format(realm=realm)
                                                 for realm in (WORK_REALM, "personal")])
    except Exception as problem:  # unknown, never unset
        rows, unread = {}, unread or "the account settings did not read: %s" % (problem or type(problem).__name__)

    def account(repo: str) -> tuple[str, str]:
        realm = places.realm_of(repo, "idea", rules)
        if realm is None or unread:
            return "", unread or "its realm is unknown"
        return sweep.account(WORK_REALM if realm == WORK_REALM else "personal", rows)

    return account


def piece_docs(
    connection: Any, items: set[int] | None = None
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Each repository's DOCS entries for the files of its writing pieces at
    review or later, and what was wrong (pack sd:7243).

    No one designates a piece (note #40909): one that is not parked, at a
    stage `PIECE_FOLDERS` names, mirrors each file `writing.MIRRORED` names
    that is there, to that stage's Drive folder, in the account its realm
    picks (`piece_accounts`). An entry carries the file's text as `content`,
    so it takes the brief's route through `mirror_request`, and
    `sd:<item>/<file>` as its `source`, which tells drain step 5 that no
    research.conf.py records the copy. `moves` lets `enqueue` carry the
    file id from Drafts to Published, so the drain moves the copy.
    """
    import_sd_db()  # the caller holds a connection, so this is the one it has
    from sd_db import writing  # noqa: PLC0415 - the checkout's `lib/`

    docs: dict[str, list[dict[str, Any]]] = {}
    problems: list[str] = []
    rows = [row for row in connection.execute(
        "SELECT * FROM item WHERE kind = 'idea' AND piece IS NOT NULL AND parked_at IS NULL "
        "AND repo IS NOT NULL ORDER BY id")
            if (items is None or row["id"] in items) and row["stage"] in PIECE_FOLDERS]
    if not rows:  # no realm or account read for nothing to queue
        return docs, problems
    from sd_db import paths  # noqa: PLC0415 - the checkout's `lib/`

    account = piece_accounts(connection)
    for row in rows:
        address, why = account(row["repo"])
        if not address:
            problems.append("mirror: sd:%s: no Drive account: %s; not queued" % (row["id"], why))
            continue
        try:
            texts, unread = writing.mirror_texts(row)
        except Exception as problem:  # a piece not read is named, never empty
            problems.append("mirror: sd:%s: %s; not queued" % (row["id"], problem or type(problem).__name__))
            continue
        problems += ["mirror: sd:%s: %s; not queued" % (row["id"], why) for why in unread]
        # The queue file's key, as `enqueue` names it, bounds the name with it (pack sd:7565).
        key = repo_key(paths.expand(row["repo"]))
        for part, (name, text) in texts.items():
            docs.setdefault(row["repo"], []).append({
                "out": writing.piece_out(row["piece"], part, key=key), "title": "%s · %s" % (row["title"], part),
                "item": row["id"], "content": text, "source": "sd:%s/%s" % (row["id"], name),
                "moves": True, "drive": {"folder": PIECE_FOLDERS[row["stage"]], "account": address}})
    return docs, problems


def enqueue_pieces(connection: Any, items: set[int] | None = None) -> list[str]:
    """Queue the files of every writing piece at review or later (`piece_docs`), as
    `enqueue_briefs` queues briefs: only a file whose text changed is queued
    again (pack sd:7243)."""
    return enqueue_rows(connection, *piece_docs(connection, items), "piece")


def enqueue_rows(connection: Any, docs: dict[str, list[dict[str, Any]]], reports: list[str],
                 what: str) -> list[str]:
    """Queue each repository's `docs` through `enqueue` with its channels
    setting; a setting that cannot be read holds that repository's `what`s."""
    import_sd_db()  # the caller holds a connection, so this is the one it has
    from sd_db import channels, paths, repos  # noqa: PLC0415 - the checkout's `lib/`

    for repo, entries in docs.items():
        try:
            setting = channels.outbound(repos.repo_channels(connection, repo))
        except Exception as problem:  # unknown is not "no setting": none queued
            reports.append("%s: %s: %s; no %s of it queued"
                           % (HELD, repo, problem or type(problem).__name__, what))
            continue
        try:
            reports += enqueue(paths.expand(repo), entries, setting)
        except Exception as problem:  # one repository's failure holds it, not the next (pack sd:7243 review)
            reports.append("%s: %s: %s; no %s of it queued"
                           % (HELD, repo, problem or type(problem).__name__, what))
    return reports


def reverts_to_delivered(path: Path, name: str, digest: str) -> bool:
    """Whether writing `digest` over the request at `path` returns it to the
    generation a drain last delivered.

    Rewritten then and not withdrawn (sd:1991). A drain reads its request
    outside the queue lock, so the one being replaced may already be on the
    destination with its acknowledgement still to come -- or never coming,
    from a drain that stopped after writing. Withdrawn, that acknowledgement
    finds nothing and records nothing, every later render reads the receipt's
    old `delivered`, and the replaced text stays on the destination for good.
    Only a request left standing puts the delivered text back, at the cost of
    one rewrite of a mirror that may already match; this says that is why.
    """
    return path.exists() and load_record(receipts() / name).get("delivered") == digest


def record_queued(name: str, fields: dict[str, Any]) -> list[str]:
    """Record on the receipt what was just queued, or say why it could not.

    The request is written by then; only the memory of it is not, and the cost
    of that is one more queue of this document next time.
    """
    try:
        receipts().mkdir(parents=True, exist_ok=True)
        record = load_record(receipts() / name)
        record.update(fields)
        record.pop("fingerprint", None)
        # Only a routine brief records its container (sd:5122, `delivered_url`):
        # a document's receipt keeps the shape it had.
        if record.get("identity") is None:
            record.pop("identity", None)
        (receipts() / name).write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except OSError as problem:
        return ["mirror: cannot write receipt for %s (%s)" % (fields["document"], problem)]
    return []


def queued_report(out: str, target: str, what: str, carried: str, reverted: bool,
                  verb: str = "queued") -> str:
    """The line a render prints for a request it wrote, or would write."""
    notes = ""
    if carried:
        notes += ", updating the %s already recorded" % what
    if reverted:
        notes += ("; back to the delivered version, replacing a pending request "
                  "a drain may already hold")
    return "mirror: %s %s -> %s%s" % (verb, out, target, notes)


def read_channels(repo: Path) -> tuple[dict[str, dict[str, str]] | None, str]:
    """The repo's `channels` setting (sd:3462): `(setting, "")`, `(None, "")`
    for no setting, or `(None, cause)` when it cannot be read.

    No setting is a NULL value, no row, or a machine with no workflow
    database (no database file and no `hub.json`): each publishes as it did
    before the setting existed. Anything else is unknown, and unknown is not
    "no setting": a hub that does not answer, a build mismatch, a database
    older than the column, a stored value the validator refuses, and an
    unresolved restore, whose row may be older than the operator's last
    edit. Falling back to the defaults would publish into folders the
    operator moved away from.
    """
    connection, problem = existing_database()
    if connection is None:
        return None, problem
    try:
        from sd_db import channels, repos  # noqa: PLC0415 - the checkout's `lib/`
        # The inbound feed is not a publish channel (sd:3980).
        return channels.outbound(repos.repo_channels(connection, repo_home(repo))), ""
    except Exception as problem:  # refused or failed: the setting is unknown
        return None, str(problem) or type(problem).__name__
    finally:
        connection.close()


def existing_database() -> tuple[Any, str]:
    """The workflow database at `DATABASE_HOME`, opened to read: `(connection, "")`,
    `(None, "")` on a machine with none (no database file and no `hub.json`),
    or `(None, cause)`. A failed import or open is a cause, never "none".
    """
    imported = import_sd_db()
    if imported.module is None:
        return None, imported.problem
    try:
        from sd_db import hub  # noqa: PLC0415 - the checkout's `lib/`
        if not (imported.module.database.local_path(DATABASE_HOME).exists()
                or hub.configured(DATABASE_HOME)):
            return None, ""
        return imported.module.connect(home=DATABASE_HOME, write=False, busy_timeout=DATABASE_BOUND), ""
    except Exception as problem:  # an unknown is not "no setting": every failure is the cause
        return None, "cannot open the workflow database: %s" % (problem or type(problem).__name__)


def publish_setting(key: str) -> tuple[str, str]:
    """`(value, "")` for the publish setting `key`, `""` when unset, or `("", cause)` when unknown (sd:5141).

    Row wins: once the database holds a row for `key`, the variable in
    `SETTING_VARIABLES` is not read, and a cleared row reads unset. The
    variable answers only while no row exists, and on a machine with no
    database. A read that fails is unknown, never the variable's value: a
    stale variable would send a mirror where the operator no longer points.
    """
    variable = str(ENVIRON.get(SETTING_VARIABLES[key], "")).strip()
    connection, problem = existing_database()
    if connection is None:
        return ("", "%s is unknown: %s" % (key, problem)) if problem else (variable, "")
    try:
        from sd_db import writes  # noqa: PLC0415 - the checkout's `lib/`
        held = writes.read_settings(connection, [key])
    except Exception as problem:  # a failed read is unknown, never the variable's value
        return "", "%s is unknown: %s" % (key, str(problem) or type(problem).__name__)
    finally:
        connection.close()
    return ((held[key] or "").strip(), "") if key in held else (variable, "")


#: The first words of the line a render prints when it cannot read the
#: setting. `exit_status` reads them, so a render by hand exits 1.
HELD = "channels: held"


def exit_status(reports: list[str]) -> int:
    """1 for a held render run by hand, else 0.

    The git hook's render exits 0: the commit is made, and the hook reports
    a failed render as a stale dashboard, which this is not.
    """
    held = any(line.startswith(HELD) for line in reports)
    return 1 if held and automatic_render() is None else 0


def publish(repo: Path, project: str | None, docs: list[dict[str, Any]]) -> list[str]:
    """Every job, in the order the contract states them.

    Local copies first, outward queue last. The order is the contract's: the
    copies that cannot leak are written before the ones that can, so a run that
    stops halfway has published locally and disclosed nothing.

    The repo's `channels` setting is read once, here (sd:3462). A linked
    worktree publishes nothing, so it reads none. A setting that cannot be
    read holds the vault copy, the queue and the item records for this
    render: requests queued earlier stay, the legacy queue is not moved
    (the next render moves it), and the dashboard copy is written.
    """
    channels, unknown = (None, "") if publish_refusal(repo, "obsidian") else read_channels(repo)
    if unknown:
        return [
            "%s: cannot read this repo's channels setting (%s); no vault copy written and "
            "nothing queued, and requests queued earlier stay. Fix it, then render again"
            % (HELD, unknown),
            ignore_dashboard(repo),
            register_root(repo, project_label(repo, project), repo / DASHBOARD_DIR),
        ]
    wrote: list[dict[str, Any]] = []
    reports = write_obsidian(repo, docs, wrote, channels)
    reports.append(ignore_dashboard(repo))
    reports.append(register_root(
        repo, project_label(repo, project), repo / DASHBOARD_DIR))
    # Before enqueueing, and here rather than inside `enqueue`: a repo that
    # designates nothing still runs on the machine holding the stranded queue,
    # and `enqueue` returns before touching the directory when it has no
    # request of its own to write.
    reports += migrate_legacy_queue()
    reports += enqueue(repo, docs, channels)
    reports += record_obsidian(repo, wrote, channels)
    return reports


def record_obsidian(repo: Path, wrote: list[dict[str, Any]],
                    channels: dict[str, dict[str, str]] | None = None) -> list[str]:
    """Record each written vault copy's link on the item its entry names (sd:3979).

    Only the copies `write_obsidian` wrote, and only where the render would
    queue a mirror: the main checkout on its default branch, with no
    `SD_SKIP_MIRROR` and no `SD_MIRROR_DRY_RUN`. A record that does not land
    is reported and never fails the render or its hook; the next render
    tries again, and an equal URL costs one read.
    """
    named = [(cfg, designated_item(cfg)[0]["item"]) for cfg in wrote if designated_item(cfg)[0]]
    if not named:
        return []
    held = str(ENVIRON.get(SKIP_MIRROR, "")).strip()
    why = ("%s (%s)" % (held, SKIP_MIRROR) if held
           else "%s=1" % MIRROR_DRY_RUN if ENVIRON.get(MIRROR_DRY_RUN)
           else publish_refusal(repo, "mirror"))
    if why:
        return ["item: obsidian link for %s not recorded (%s)" % (item_named(item, repo), why) for _, item in named]
    home = repo_home(repo)
    vault = Path(VAULT).expanduser()
    folder = vault_folder(repo, channels)
    reports = []
    for cfg, item in named:
        out = str(cfg.get("out", "")).strip()
        relative = (folder / out).relative_to(vault).as_posix() if folder else out
        url = "obsidian://open?vault=%s&file=%s" % (quote(vault.name, safe=""), quote(relative, safe=""))
        answer, problem = record_item_url(item, str(home), out, str(cfg.get("title", out)),
                                          "obsidian", url)
        if problem:
            reports.append("item: obsidian link for %s not recorded (%s)" % (item_named(item, repo), problem))
        elif answer != "unchanged":
            reports.append("item: obsidian link for %s recorded: %s" % (item_named(item, repo), url))
    return reports


#: The git triggers the re-render hook installs under. A commit is not the only
#: way a document changes: a pull rewrites the working tree, a branch switch
#: replaces it outright, and `git checkout <rev> -- doc.md` rewrites one
#: document in place without moving HEAD at all. A `docs/dashboard/` left from
#: the old tree is the stale render this hook exists to prevent. One source under three
#: names, because three copies of one script drift; the script reads its own
#: `argv[0]` to know which trigger fired and how to ask git what moved.
HOOK_TRIGGERS = ("post-commit", "post-merge", "post-checkout")

#: The hook that keeps `docs/dashboard/` current, as text rather than a file beside
#: the skill. Two rules put it here, both pinned by
#: `tests/test_no_shipped_shell.py`: `skills/` is the render surface and is
#: markdown only, because a file there is payload copied onto a platform home;
#: and the pack ships no shell outside `.github/scripts/`. So the hook is
#: Python, stdlib only, and it lives in the module that installs it. It carries
#: `#` comments rather than docstrings because it is itself a string literal
#: here, and a `"""` inside one would end it.
HOOK = '''#!/usr/bin/env python3
# Re-render this research repo when git changes a document.
#
# Installed by `sd-research-kit init-hook` under three names at once:
# post-commit, post-merge and post-checkout. A pull, a branch switch and a
# checkout of one path change the documents as surely as a commit does, and
# a `docs/dashboard/` left from the old tree is the stale render this exists
# to prevent. Post-commit and not pre-commit: `docs/dashboard/` is generated
# and not committed, so there is nothing to stage, and the commit is the
# revision a queued mirror should name.
#
# One source, three names, and the trigger is read from `argv[0]`, because each
# one has to ask git a different question. `diff-tree ... HEAD` is
# commit-shaped: it prints nothing at all for a merge commit, and a checkout's
# HEAD says nothing about what the checkout moved. So a copy of the
# post-commit body under the other two names would never render.
#
# Rendered output goes stale the moment its source changes, and a stale page is
# worse than a missing one because it looks current. This is what keeps the
# dashboard's Documents tab a statement about the render rather than about who
# remembered to run it.
#
# Never fails the git command. The commit, the merge or the checkout is already
# made when this runs, so exiting non-zero would report a failure for work that
# succeeded. A render that cannot run says so and leaves git alone.
#
# `SD_SKIP_RENDER=1` skips it, for all three.
#
# A render executes research.conf.py, and after a pull or a checkout it can
# be one nobody here has read. A commit does not vouch for it either: after a
# declined pull, a commit of doc.md alone leaves the incoming config in the
# tree. So all three render only a config a render by hand has executed here,
# and otherwise say how to render once it is read. The render they run is told
# it is automatic, checks the bytes it executes again, and records nothing
# (sd:1376). Whether a repository may instruct this tool at all is sd:1375.
#
# A branch checkout that lands behind its upstream renders and queues no
# mirror (sd:1991). Checking out a stale `main` and pulling it is the usual
# way to update, and the checkout's render queued the stale text for the
# shared mirror a moment before the pull's render replaced it: a drain in
# between published it. The render is told why in `SD_SKIP_MIRROR`. Only
# the local tracking ref is read, so nothing here fetches, and a branch with
# no upstream renders and queues as before.

import hashlib
import os
import shutil
import subprocess
import sys

# What `diff_argv` answers for a trigger that renders without asking git
# anything. Not an empty list: "git named nothing" is a statement about the
# tree, and this is the refusal to make one.
ALWAYS = "always"


def diff_argv(trigger, argv):
    # The git command that names what this trigger moved; ALWAYS to render
    # without asking; None for an invocation that renders nothing. Three
    # answers and not two, because "ask git nothing" and "git answered
    # nothing" are different, and only the second is a statement about the
    # tree.
    if trigger == "post-commit":
        # Only when the commit carried a document. A commit touching nothing
        # but the config or a script still renders: both change the output.
        return ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]
    if trigger == "post-merge":
        # HEAD is the merge commit and `diff-tree` prints nothing for one, so
        # the post-commit question is unusable here. ORIG_HEAD is where this
        # branch stood before the merge, which is what the pull changed from.
        return ["diff", "--name-only", "ORIG_HEAD", "HEAD"]
    if trigger == "post-checkout":
        # git passes the previous HEAD, the new HEAD, and 1 for a branch
        # checkout or 0 for a file checkout.
        #
        # All zeros for the previous HEAD is a fresh clone or a
        # `git worktree add`: nothing is rendered there yet and nothing
        # has been committed to it, so a render would publish a checkout
        # nobody has worked in. Said here rather than left to the `git
        # diff 0000000 <new>` below failing into the caller's error arm:
        # that arm returns 0 for any failure of that command, so the day
        # something else throws in it a fresh clone stops rendering and
        # the test that pins this goes on passing.
        if len(argv) >= 2 and argv[1] and set(argv[1]) <= {"0"}:
            return None
        # A branch checkout has a range, and the range is the only thing
        # that names what it moved.
        if len(argv) >= 4 and argv[3] == "1" and argv[1] != argv[2]:
            return ["diff", "--name-only", argv[1], argv[2]]
        # Everything else this trigger fires for moved no branch, so its two
        # revisions are equal and there is no range -- and it renders anyway,
        # without a question, because there is no question worth asking here.
        #
        # Do not reintroduce `diff --name-only HEAD` as an optimisation. It
        # looks like the right guard and it is the wrong one: it asks whether
        # the tree differs from HEAD, and what a mirror needs to know is
        # whether the tree differs from what was published. Those agree only
        # while the published copy tracks HEAD, and a file checkout is exactly
        # what breaks that. `git checkout <rev> -- doc.md` publishes the older
        # text; `git checkout HEAD -- doc.md` then restores a clean tree, the
        # guard sees no diff, and the published copy keeps the reverted text
        # for good. Comparing against what was published instead would be a
        # second record of the same fact, which is the failure this contract
        # exists to close.
        #
        # So it renders on an unchanged tree too. That is the price, and it is
        # small: a render that finds nothing changed is cheap, and a mirror
        # holding text nobody can see is not.
        return ALWAYS
    # An unrecognised name is not this hook's trigger. Guessing would run a
    # render on a git event nobody installed it for.
    return None


# Where `sd-research-kit render`, run by hand, records the sha256 of the
# research.conf.py it executed: under the git directory, per checkout, never
# in the tree.
TRUSTED = "sd-research-conf.sha256"

# What this sets, to the trigger's name, for the render it runs. The render
# then executes only a recorded config and records nothing itself.
TRIGGER_ENV = "SD_RESEARCH_TRIGGER"

# What this sets, to a reason, for a render that must queue no mirror.
SKIP_MIRROR = "SD_SKIP_MIRROR"


def behind_upstream(root):
    # How many commits this branch's upstream has that HEAD lacks, read off
    # the local tracking ref; 0 when there is no upstream to be behind. No
    # fetch: a hook that reached the network would make every checkout wait
    # on it, and the ref a pull is about to fast-forward to is already local
    # whenever the pull is `--ff-only` after a fetch.
    counted = subprocess.run(
        ["git", "rev-list", "--count", "HEAD..@{u}"],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    if counted.returncode != 0:
        return 0
    try:
        return int(counted.stdout.strip() or 0)
    except ValueError:
        return 0


def conf_trusted(root):
    # Whether the tree's research.conf.py is one a render by hand executed
    # here, after the operator read it. Asking git whether this operation
    # changed the config is not enough, and neither is a commit: a declined
    # pull leaves the incoming config in the tree, and the next file checkout
    # or commit of doc.md alone would execute it. No record means no render
    # has vouched for it, which declines too.
    path = subprocess.run(
        ["git", "rev-parse", "--git-path", TRUSTED],
        cwd=root, capture_output=True, text=True, timeout=30, check=True,
    ).stdout.strip()
    try:
        with open(os.path.join(root, path), encoding="utf-8") as handle:
            recorded = handle.read().strip()
        with open(os.path.join(root, "research.conf.py"), "rb") as handle:
            current = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return False
    return recorded == current


def main(argv):
    if os.environ.get("SD_SKIP_RENDER"):
        return 0
    trigger = os.path.basename(argv[0]) if argv else ""
    query = diff_argv(trigger, argv)
    if query is None:
        return 0
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if not os.path.isfile(os.path.join(root, "research.conf.py")):
            return 0
        if query != ALWAYS:
            changed = subprocess.run(
                ["git"] + query,
                cwd=root, capture_output=True, text=True, timeout=30, check=True,
            ).stdout.split()
            if not any(name.endswith((".md", ".py")) for name in changed):
                return 0
        env = dict(os.environ, **{TRIGGER_ENV: trigger})
        # A branch checkout is the only trigger with a range and no pull of
        # its own; post-merge is the pull, and renders the tree it brought.
        behind = behind_upstream(root) if (
            trigger == "post-checkout" and query != ALWAYS) else 0
        if behind:
            env[SKIP_MIRROR] = ("%s: HEAD is %d commit%s behind its upstream; "
                                "pull, and that render queues the mirrors"
                                % (trigger, behind, "" if behind == 1 else "s"))
        if not conf_trusted(root):
            print("%s: no render by hand has executed this research.conf.py, and "
                  "a render executes it; read it, then run `sd-research-kit "
                  "render`. docs/dashboard/ is stale until then" % trigger,
                  file=sys.stderr)
            return 0
    except (OSError, subprocess.SubprocessError):
        return 0

    kit = shutil.which("sd-research-kit")
    if kit is None:
        print("%s: sd-research-kit not on PATH; docs/dashboard/ is now stale"
              % trigger, file=sys.stderr)
        return 0
    try:
        subprocess.run([kit, "render"], cwd=root, timeout=600, check=True,
                       env=env)
    except (OSError, subprocess.SubprocessError):
        print("%s: render failed; docs/dashboard/ is now stale" % trigger,
              file=sys.stderr)
    # A renamed document leaves every link to it pointing nowhere, and the
    # render does not notice (sd:1840). Reported, never fatal, like the render.
    try:
        checked = subprocess.run([kit, "checklinks"], cwd=root, timeout=600,
                                 env=env).returncode
    except (OSError, subprocess.SubprocessError):
        checked = None
    if checked:
        print("%s: checklinks found broken links; fix them before publishing"
              % trigger, file=sys.stderr)
    elif checked is None:
        print("%s: checklinks could not run" % trigger, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''

#: Every earlier body this module released, byte for byte, so that an
#: installation made by one of them can be recognised and replaced rather than
#: refused. Without this the fix above reaches nobody: every repository that
#: has the hook has one of these, `init-hook` would read it as somebody else's
#: file, and the only installations that could ever get three triggers are the
#: ones that do not exist yet.
#:
#: Exact bodies and not a "looks like ours" test, because the whole value of
#: refusing a foreign file is that the refusal cannot be talked out of. A
#: single byte that this pack did not write is still foreign and still refused.
#:
#: The list is the whole population, read out of this file's own history --
#: `git rev-list --all --full-history -- bin/sd_research_publish.py`, every
#: blob parsed for its `HOOK` assignment. Two bodies were released: the one
#: `05d4b9d3` shipped, and the one `fa5f5576` replaced it with and `47d4d147`
#: still carried. They differ in one comment word.
#:
#: The other two are this branch's own. A branch owns the predecessors it
#: creates: each of its commits is a revision somebody can check out and run
#: `init-hook` from, and each is history the moment the branch lands, so a
#: body left off here is an installation this verb refuses for the rest of its
#: life. "Released as of today" is the wrong reading and does not survive its
#: own criterion -- it is the reading that left `4d20574b`'s body off, and the
#: review that caught it ran the installer against the body of the very
#: revision it was reviewing. A body from a branch that has not landed belongs
#: to that branch, which adds it when it does -- and the fifth body below is
#: sd:1352's, added by the merge that brought that branch in. The rule is the
#: same one read from the other side: a merge inherits the predecessors of
#: what it merges, because those revisions are checkouts here too. The sixth
#: is this branch's merge commit's own body, superseded by the commit that
#: gave the hook `docs/dashboard/` and an explicit all-zeros guard.
#:
#: Measured rather than assumed, because "released" is the tempting reading
#: and it is not this one: `4d20574b` and `dd14d501` are in here and neither
#: ever reached `main`. What puts a body on a machine is a checkout, and an
#: ancestor commit is a checkout.
#:
#: Downward only in one direction: a body leaves this tuple when no machine can
#: still be carrying it, which is not a thing this repository can know. Assume
#: it never happens, and add to it whenever `HOOK` changes.
SUPERSEDED_HOOKS = (
    # `05d4b9d3`, the body the hook shipped with.
    '''#!/usr/bin/env python3
# Re-render this research repo after a commit that touched a document.
#
# Installed by `sd-research-kit init-hook`. Post-commit and not pre-commit:
# `build/` is generated and not committed, so there is nothing to stage, and
# the commit is the revision a queued Notion sync should name.
#
# Rendered output goes stale the moment its source changes, and a stale page is
# worse than a missing one because it looks current. This is what keeps the
# dashboard's Documents tab a statement about the render rather than about who
# remembered to run it.
#
# Never fails the commit. The commit is already made when this runs, so exiting
# non-zero would report a failure for work that succeeded. A render that cannot
# run says so and leaves the commit alone.
#
# `SD_SKIP_RENDER=1 git commit` skips it.

import os
import shutil
import subprocess
import sys


def main():
    if os.environ.get("SD_SKIP_RENDER"):
        return 0
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if not os.path.isfile(os.path.join(root, "research.conf.py")):
            return 0
        # Only when the commit carried a document. A commit touching nothing
        # but the config or a script still renders: both change the output.
        changed = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=30, check=True,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return 0
    if not any(name.endswith((".md", ".py")) for name in changed):
        return 0

    kit = shutil.which("sd-research-kit")
    if kit is None:
        print("post-commit: sd-research-kit not on PATH; build/ is now stale",
              file=sys.stderr)
        return 0
    try:
        subprocess.run([kit, "render"], cwd=root, timeout=600, check=True)
    except (OSError, subprocess.SubprocessError):
        print("post-commit: render failed; build/ is now stale", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
''',
    # `fa5f5576`, one comment word later: the queue stopped being Notion's.
    '''#!/usr/bin/env python3
# Re-render this research repo after a commit that touched a document.
#
# Installed by `sd-research-kit init-hook`. Post-commit and not pre-commit:
# `build/` is generated and not committed, so there is nothing to stage, and
# the commit is the revision a queued mirror should name.
#
# Rendered output goes stale the moment its source changes, and a stale page is
# worse than a missing one because it looks current. This is what keeps the
# dashboard's Documents tab a statement about the render rather than about who
# remembered to run it.
#
# Never fails the commit. The commit is already made when this runs, so exiting
# non-zero would report a failure for work that succeeded. A render that cannot
# run says so and leaves the commit alone.
#
# `SD_SKIP_RENDER=1 git commit` skips it.

import os
import shutil
import subprocess
import sys


def main():
    if os.environ.get("SD_SKIP_RENDER"):
        return 0
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if not os.path.isfile(os.path.join(root, "research.conf.py")):
            return 0
        # Only when the commit carried a document. A commit touching nothing
        # but the config or a script still renders: both change the output.
        changed = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=30, check=True,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return 0
    if not any(name.endswith((".md", ".py")) for name in changed):
        return 0

    kit = shutil.which("sd-research-kit")
    if kit is None:
        print("post-commit: sd-research-kit not on PATH; build/ is now stale",
              file=sys.stderr)
        return 0
    try:
        subprocess.run([kit, "render"], cwd=root, timeout=600, check=True)
    except (OSError, subprocess.SubprocessError):
        print("post-commit: render failed; build/ is now stale", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
''',
    # `4d20574b`, this branch: one source under three names, with the
    # trigger read from `argv[0]`.
    '''#!/usr/bin/env python3
# Re-render this research repo when git changes a document.
#
# Installed by `sd-research-kit init-hook` under three names at once:
# post-commit, post-merge and post-checkout. A pull and a branch switch change
# the documents as surely as a commit does, and a `build/` left from the old
# tree is the stale render this exists to prevent. Post-commit and not
# pre-commit: `build/` is generated and not committed, so there is nothing to
# stage, and the commit is the revision a queued mirror should name.
#
# One source, three names, and the trigger is read from `argv[0]`, because each
# one has to ask git a different question. `diff-tree ... HEAD` is
# commit-shaped: it prints nothing at all for a merge commit, and a checkout's
# HEAD says nothing about what the checkout moved. So a copy of the
# post-commit body under the other two names would never render.
#
# Rendered output goes stale the moment its source changes, and a stale page is
# worse than a missing one because it looks current. This is what keeps the
# dashboard's Documents tab a statement about the render rather than about who
# remembered to run it.
#
# Never fails the git command. The commit, the merge or the checkout is already
# made when this runs, so exiting non-zero would report a failure for work that
# succeeded. A render that cannot run says so and leaves git alone.
#
# `SD_SKIP_RENDER=1` skips it, for all three.

import os
import shutil
import subprocess
import sys


def diff_argv(trigger, argv):
    # The git command that names what this trigger moved, or None for an
    # invocation that renders nothing. None rather than an empty list: "ask
    # git nothing" and "git answered nothing" are different, and only the
    # second is a statement about the tree.
    if trigger == "post-commit":
        # Only when the commit carried a document. A commit touching nothing
        # but the config or a script still renders: both change the output.
        return ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]
    if trigger == "post-merge":
        # HEAD is the merge commit and `diff-tree` prints nothing for one, so
        # the post-commit question is unusable here. ORIG_HEAD is where this
        # branch stood before the merge, which is what the pull changed from.
        return ["diff", "--name-only", "ORIG_HEAD", "HEAD"]
    if trigger == "post-checkout":
        # git passes the previous HEAD, the new HEAD, and 1 for a branch
        # checkout or 0 for a file checkout. A file checkout restores paths
        # inside one tree and moves no branch, and `git checkout <current>`
        # moves nothing at all; neither can have changed a document.
        if len(argv) < 4 or argv[3] != "1" or argv[1] == argv[2]:
            return None
        return ["diff", "--name-only", argv[1], argv[2]]
    # An unrecognised name is not this hook's trigger. Guessing would run a
    # render on a git event nobody installed it for.
    return None


def main(argv):
    if os.environ.get("SD_SKIP_RENDER"):
        return 0
    trigger = os.path.basename(argv[0]) if argv else ""
    query = diff_argv(trigger, argv)
    if query is None:
        return 0
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if not os.path.isfile(os.path.join(root, "research.conf.py")):
            return 0
        changed = subprocess.run(
            ["git"] + query,
            cwd=root, capture_output=True, text=True, timeout=30, check=True,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return 0
    if not any(name.endswith((".md", ".py")) for name in changed):
        return 0

    kit = shutil.which("sd-research-kit")
    if kit is None:
        print("%s: sd-research-kit not on PATH; build/ is now stale" % trigger,
              file=sys.stderr)
        return 0
    try:
        subprocess.run([kit, "render"], cwd=root, timeout=600, check=True)
    except (OSError, subprocess.SubprocessError):
        print("%s: render failed; build/ is now stale" % trigger, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
''',
    # `dd14d501`, this branch: a file checkout measured against the
    # working tree, which `8b575695` replaced with an unconditional
    # render.
    '''#!/usr/bin/env python3
# Re-render this research repo when git changes a document.
#
# Installed by `sd-research-kit init-hook` under three names at once:
# post-commit, post-merge and post-checkout. A pull, a branch switch and a
# checkout of one path change the documents as surely as a commit does, and a
# `build/` left from the old tree is the stale render this exists to prevent.
# Post-commit and not pre-commit: `build/` is generated and not committed, so
# there is nothing to stage, and the commit is the revision a queued mirror
# should name.
#
# One source, three names, and the trigger is read from `argv[0]`, because each
# one has to ask git a different question. `diff-tree ... HEAD` is
# commit-shaped: it prints nothing at all for a merge commit, and a checkout's
# HEAD says nothing about what the checkout moved. So a copy of the
# post-commit body under the other two names would never render.
#
# Rendered output goes stale the moment its source changes, and a stale page is
# worse than a missing one because it looks current. This is what keeps the
# dashboard's Documents tab a statement about the render rather than about who
# remembered to run it.
#
# Never fails the git command. The commit, the merge or the checkout is already
# made when this runs, so exiting non-zero would report a failure for work that
# succeeded. A render that cannot run says so and leaves git alone.
#
# `SD_SKIP_RENDER=1` skips it, for all three.

import os
import shutil
import subprocess
import sys


def diff_argv(trigger, argv):
    # The git command that names what this trigger moved, or None for an
    # invocation that renders nothing. None rather than an empty list: "ask
    # git nothing" and "git answered nothing" are different, and only the
    # second is a statement about the tree.
    if trigger == "post-commit":
        # Only when the commit carried a document. A commit touching nothing
        # but the config or a script still renders: both change the output.
        return ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]
    if trigger == "post-merge":
        # HEAD is the merge commit and `diff-tree` prints nothing for one, so
        # the post-commit question is unusable here. ORIG_HEAD is where this
        # branch stood before the merge, which is what the pull changed from.
        return ["diff", "--name-only", "ORIG_HEAD", "HEAD"]
    if trigger == "post-checkout":
        # git passes the previous HEAD, the new HEAD, and 1 for a branch
        # checkout or 0 for a file checkout. A branch checkout has a range,
        # and the range is the only thing that names what it moved.
        if len(argv) >= 4 and argv[3] == "1" and argv[1] != argv[2]:
            return ["diff", "--name-only", argv[1], argv[2]]
        # Everything else this trigger fires for moved no branch, so its two
        # revisions are equal and there is no range to ask about --  but
        # `git checkout <rev> -- doc.md` replaces a document's contents all
        # the same, which is exactly the render this hook exists for. The
        # honest question is then the working tree against HEAD, which names
        # what the checkout just wrote. It also names unrelated uncommitted
        # edits, so this renders slightly more often than it must; a render
        # that finds nothing changed is cheap, and a mirror that silently
        # keeps the superseded text is not.
        return ["diff", "--name-only", "HEAD"]
    # An unrecognised name is not this hook's trigger. Guessing would run a
    # render on a git event nobody installed it for.
    return None


def main(argv):
    if os.environ.get("SD_SKIP_RENDER"):
        return 0
    trigger = os.path.basename(argv[0]) if argv else ""
    query = diff_argv(trigger, argv)
    if query is None:
        return 0
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if not os.path.isfile(os.path.join(root, "research.conf.py")):
            return 0
        changed = subprocess.run(
            ["git"] + query,
            cwd=root, capture_output=True, text=True, timeout=30, check=True,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return 0
    if not any(name.endswith((".md", ".py")) for name in changed):
        return 0

    kit = shutil.which("sd-research-kit")
    if kit is None:
        print("%s: sd-research-kit not on PATH; build/ is now stale" % trigger,
              file=sys.stderr)
        return 0
    try:
        subprocess.run([kit, "render"], cwd=root, timeout=600, check=True)
    except (OSError, subprocess.SubprocessError):
        print("%s: render failed; build/ is now stale" % trigger, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
''',
    # `e65d1ec4`, sd:1352's `rewrite/research-publish-loop`, which this
    # branch merges: the same three triggers reached independently, with
    # `HOOK_NAMES` for the tuple and an upgrade keyed on a marker line.
    # Its resolution of a file checkout -- render nothing -- is the one
    # this branch replaces, and its shape is gone from the merged tree;
    # the body stays, because those revisions are now ancestors here and
    # a checkout of any of them can install it. A branch owns the
    # predecessors it creates, and a merge inherits the other branch's.
    '''#!/usr/bin/env python3
# Re-render this research repo after git changes a document in it.
#
# Installed by `sd-research-kit init-hook` as post-commit, post-merge and
# post-checkout: one file under three names, and the name git called it by
# says what to compare. A commit is compared with its parent; a merge -- and
# so a pull, which is a fetch and a merge -- compares ORIG_HEAD with HEAD; a
# branch checkout compares the two revisions git hands it. Post-commit alone
# missed every pulled change, and the page then went stale while looking
# current, until a review demanded a render nobody had been told to run.
#
# After the fact and not before: `docs/dashboard/` is generated and not
# committed, so there is nothing to stage, and the new HEAD is the revision a
# queued mirror should name.
#
# Rendered output goes stale the moment its source changes, and a stale page is
# worse than a missing one because it looks current. This is what keeps the
# dashboard's Documents tab a statement about the render rather than about who
# remembered to run it.
#
# Never fails the git command. It is already done when this runs, so exiting
# non-zero would report a failure for work that succeeded. A render that cannot
# run says so and leaves the repository alone.
#
# `SD_SKIP_RENDER=1 git commit` (or pull, or checkout) skips it.

import os
import shutil
import subprocess
import sys


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        timeout=30, check=True,
    ).stdout.split()


def changed(root, hook, args):
    # The paths this git command changed. Every branch here compares two
    # revisions, so an uncommitted edit is never what triggers a render.
    if hook == "post-commit":
        return git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
    if hook == "post-merge":
        return git(root, "diff", "--name-only", "ORIG_HEAD", "HEAD")
    if hook == "post-checkout":
        # <previous HEAD> <new HEAD> <1 for a branch checkout, 0 for a file>.
        # A file checkout moves no ref, so there is nothing to compare; and a
        # previous HEAD of all zeros is a fresh clone or `git worktree add`,
        # which has nothing rendered yet and nothing committed to it, so a
        # render there would publish a checkout nobody has worked in.
        if len(args) < 3 or args[2] != "1" or args[0] == args[1]:
            return []
        if set(args[0]) <= {"0"}:
            return []
        return git(root, "diff", "--name-only", args[0], args[1])
    return []


def main(argv):
    if os.environ.get("SD_SKIP_RENDER"):
        return 0
    hook = os.path.basename(argv[0]) if argv else "post-commit"
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if not os.path.isfile(os.path.join(root, "research.conf.py")):
            return 0
        # Only when a document moved. A change touching nothing but the
        # config or a script still renders: both change the output.
        paths = changed(root, hook, argv[1:])
    except (OSError, subprocess.SubprocessError):
        return 0
    if not any(name.endswith((".md", ".py")) for name in paths):
        return 0

    kit = shutil.which("sd-research-kit")
    if kit is None:
        print("%s: sd-research-kit not on PATH; docs/dashboard/ is now stale"
              % hook, file=sys.stderr)
        return 0
    try:
        subprocess.run([kit, "render"], cwd=root, timeout=600, check=True)
    except (OSError, subprocess.SubprocessError):
        print("%s: render failed; docs/dashboard/ is now stale" % hook,
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
''',
    # This branch's merge of sd:1352, as it stood before the commit that
    # added the body above: `build/` still in its comments and its two
    # stderr lines, and an all-zeros previous HEAD left to the error arm.
    # One commit changed both, so one body is superseded rather than two.
    '''#!/usr/bin/env python3
# Re-render this research repo when git changes a document.
#
# Installed by `sd-research-kit init-hook` under three names at once:
# post-commit, post-merge and post-checkout. A pull, a branch switch and a
# checkout of one path change the documents as surely as a commit does, and a
# `build/` left from the old tree is the stale render this exists to prevent.
# Post-commit and not pre-commit: `build/` is generated and not committed, so
# there is nothing to stage, and the commit is the revision a queued mirror
# should name.
#
# One source, three names, and the trigger is read from `argv[0]`, because each
# one has to ask git a different question. `diff-tree ... HEAD` is
# commit-shaped: it prints nothing at all for a merge commit, and a checkout's
# HEAD says nothing about what the checkout moved. So a copy of the
# post-commit body under the other two names would never render.
#
# Rendered output goes stale the moment its source changes, and a stale page is
# worse than a missing one because it looks current. This is what keeps the
# dashboard's Documents tab a statement about the render rather than about who
# remembered to run it.
#
# Never fails the git command. The commit, the merge or the checkout is already
# made when this runs, so exiting non-zero would report a failure for work that
# succeeded. A render that cannot run says so and leaves git alone.
#
# `SD_SKIP_RENDER=1` skips it, for all three.

import os
import shutil
import subprocess
import sys

# What `diff_argv` answers for a trigger that renders without asking git
# anything. Not an empty list: "git named nothing" is a statement about the
# tree, and this is the refusal to make one.
ALWAYS = "always"


def diff_argv(trigger, argv):
    # The git command that names what this trigger moved; ALWAYS to render
    # without asking; None for an invocation that renders nothing. Three
    # answers and not two, because "ask git nothing" and "git answered
    # nothing" are different, and only the second is a statement about the
    # tree.
    if trigger == "post-commit":
        # Only when the commit carried a document. A commit touching nothing
        # but the config or a script still renders: both change the output.
        return ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]
    if trigger == "post-merge":
        # HEAD is the merge commit and `diff-tree` prints nothing for one, so
        # the post-commit question is unusable here. ORIG_HEAD is where this
        # branch stood before the merge, which is what the pull changed from.
        return ["diff", "--name-only", "ORIG_HEAD", "HEAD"]
    if trigger == "post-checkout":
        # git passes the previous HEAD, the new HEAD, and 1 for a branch
        # checkout or 0 for a file checkout. A branch checkout has a range,
        # and the range is the only thing that names what it moved.
        if len(argv) >= 4 and argv[3] == "1" and argv[1] != argv[2]:
            return ["diff", "--name-only", argv[1], argv[2]]
        # Everything else this trigger fires for moved no branch, so its two
        # revisions are equal and there is no range -- and it renders anyway,
        # without a question, because there is no question worth asking here.
        #
        # Do not reintroduce `diff --name-only HEAD` as an optimisation. It
        # looks like the right guard and it is the wrong one: it asks whether
        # the tree differs from HEAD, and what a mirror needs to know is
        # whether the tree differs from what was published. Those agree only
        # while the published copy tracks HEAD, and a file checkout is exactly
        # what breaks that. `git checkout <rev> -- doc.md` publishes the older
        # text; `git checkout HEAD -- doc.md` then restores a clean tree, the
        # guard sees no diff, and the published copy keeps the reverted text
        # for good. Comparing against what was published instead would be a
        # second record of the same fact, which is the failure this contract
        # exists to close.
        #
        # So it renders on an unchanged tree too. That is the price, and it is
        # small: a render that finds nothing changed is cheap, and a mirror
        # holding text nobody can see is not.
        return ALWAYS
    # An unrecognised name is not this hook's trigger. Guessing would run a
    # render on a git event nobody installed it for.
    return None


def main(argv):
    if os.environ.get("SD_SKIP_RENDER"):
        return 0
    trigger = os.path.basename(argv[0]) if argv else ""
    query = diff_argv(trigger, argv)
    if query is None:
        return 0
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if not os.path.isfile(os.path.join(root, "research.conf.py")):
            return 0
        if query != ALWAYS:
            changed = subprocess.run(
                ["git"] + query,
                cwd=root, capture_output=True, text=True, timeout=30, check=True,
            ).stdout.split()
            if not any(name.endswith((".md", ".py")) for name in changed):
                return 0
    except (OSError, subprocess.SubprocessError):
        return 0

    kit = shutil.which("sd-research-kit")
    if kit is None:
        print("%s: sd-research-kit not on PATH; build/ is now stale" % trigger,
              file=sys.stderr)
        return 0
    try:
        subprocess.run([kit, "render"], cwd=root, timeout=600, check=True)
    except (OSError, subprocess.SubprocessError):
        print("%s: render failed; build/ is now stale" % trigger, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
''',
    # `91d26285`, sd:1353's three triggers with `docs/dashboard/`, replaced by
    # sd:1376: the post-merge and post-checkout arms then rendered a
    # research.conf.py the operation had just brought in.
    '''#!/usr/bin/env python3
# Re-render this research repo when git changes a document.
#
# Installed by `sd-research-kit init-hook` under three names at once:
# post-commit, post-merge and post-checkout. A pull, a branch switch and a
# checkout of one path change the documents as surely as a commit does, and
# a `docs/dashboard/` left from the old tree is the stale render this exists
# to prevent. Post-commit and not pre-commit: `docs/dashboard/` is generated
# and not committed, so there is nothing to stage, and the commit is the
# revision a queued mirror should name.
#
# One source, three names, and the trigger is read from `argv[0]`, because each
# one has to ask git a different question. `diff-tree ... HEAD` is
# commit-shaped: it prints nothing at all for a merge commit, and a checkout's
# HEAD says nothing about what the checkout moved. So a copy of the
# post-commit body under the other two names would never render.
#
# Rendered output goes stale the moment its source changes, and a stale page is
# worse than a missing one because it looks current. This is what keeps the
# dashboard's Documents tab a statement about the render rather than about who
# remembered to run it.
#
# Never fails the git command. The commit, the merge or the checkout is already
# made when this runs, so exiting non-zero would report a failure for work that
# succeeded. A render that cannot run says so and leaves git alone.
#
# `SD_SKIP_RENDER=1` skips it, for all three.

import os
import shutil
import subprocess
import sys

# What `diff_argv` answers for a trigger that renders without asking git
# anything. Not an empty list: "git named nothing" is a statement about the
# tree, and this is the refusal to make one.
ALWAYS = "always"


def diff_argv(trigger, argv):
    # The git command that names what this trigger moved; ALWAYS to render
    # without asking; None for an invocation that renders nothing. Three
    # answers and not two, because "ask git nothing" and "git answered
    # nothing" are different, and only the second is a statement about the
    # tree.
    if trigger == "post-commit":
        # Only when the commit carried a document. A commit touching nothing
        # but the config or a script still renders: both change the output.
        return ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]
    if trigger == "post-merge":
        # HEAD is the merge commit and `diff-tree` prints nothing for one, so
        # the post-commit question is unusable here. ORIG_HEAD is where this
        # branch stood before the merge, which is what the pull changed from.
        return ["diff", "--name-only", "ORIG_HEAD", "HEAD"]
    if trigger == "post-checkout":
        # git passes the previous HEAD, the new HEAD, and 1 for a branch
        # checkout or 0 for a file checkout.
        #
        # All zeros for the previous HEAD is a fresh clone or a
        # `git worktree add`: nothing is rendered there yet and nothing
        # has been committed to it, so a render would publish a checkout
        # nobody has worked in. Said here rather than left to the `git
        # diff 0000000 <new>` below failing into the caller's error arm:
        # that arm returns 0 for any failure of that command, so the day
        # something else throws in it a fresh clone stops rendering and
        # the test that pins this goes on passing.
        if len(argv) >= 2 and argv[1] and set(argv[1]) <= {"0"}:
            return None
        # A branch checkout has a range, and the range is the only thing
        # that names what it moved.
        if len(argv) >= 4 and argv[3] == "1" and argv[1] != argv[2]:
            return ["diff", "--name-only", argv[1], argv[2]]
        # Everything else this trigger fires for moved no branch, so its two
        # revisions are equal and there is no range -- and it renders anyway,
        # without a question, because there is no question worth asking here.
        #
        # Do not reintroduce `diff --name-only HEAD` as an optimisation. It
        # looks like the right guard and it is the wrong one: it asks whether
        # the tree differs from HEAD, and what a mirror needs to know is
        # whether the tree differs from what was published. Those agree only
        # while the published copy tracks HEAD, and a file checkout is exactly
        # what breaks that. `git checkout <rev> -- doc.md` publishes the older
        # text; `git checkout HEAD -- doc.md` then restores a clean tree, the
        # guard sees no diff, and the published copy keeps the reverted text
        # for good. Comparing against what was published instead would be a
        # second record of the same fact, which is the failure this contract
        # exists to close.
        #
        # So it renders on an unchanged tree too. That is the price, and it is
        # small: a render that finds nothing changed is cheap, and a mirror
        # holding text nobody can see is not.
        return ALWAYS
    # An unrecognised name is not this hook's trigger. Guessing would run a
    # render on a git event nobody installed it for.
    return None


def main(argv):
    if os.environ.get("SD_SKIP_RENDER"):
        return 0
    trigger = os.path.basename(argv[0]) if argv else ""
    query = diff_argv(trigger, argv)
    if query is None:
        return 0
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if not os.path.isfile(os.path.join(root, "research.conf.py")):
            return 0
        if query != ALWAYS:
            changed = subprocess.run(
                ["git"] + query,
                cwd=root, capture_output=True, text=True, timeout=30, check=True,
            ).stdout.split()
            if not any(name.endswith((".md", ".py")) for name in changed):
                return 0
    except (OSError, subprocess.SubprocessError):
        return 0

    kit = shutil.which("sd-research-kit")
    if kit is None:
        print("%s: sd-research-kit not on PATH; docs/dashboard/ is now stale"
              % trigger, file=sys.stderr)
        return 0
    try:
        subprocess.run([kit, "render"], cwd=root, timeout=600, check=True)
    except (OSError, subprocess.SubprocessError):
        print("%s: render failed; docs/dashboard/ is now stale" % trigger,
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
''',
    # `fbd69cd5`, sd:1376's first body: it asked git whether the pull or
    # checkout changed research.conf.py, and the next file checkout then
    # executed the config a declined pull had left in the tree.
    '''#!/usr/bin/env python3
# Re-render this research repo when git changes a document.
#
# Installed by `sd-research-kit init-hook` under three names at once:
# post-commit, post-merge and post-checkout. A pull, a branch switch and a
# checkout of one path change the documents as surely as a commit does, and
# a `docs/dashboard/` left from the old tree is the stale render this exists
# to prevent. Post-commit and not pre-commit: `docs/dashboard/` is generated
# and not committed, so there is nothing to stage, and the commit is the
# revision a queued mirror should name.
#
# One source, three names, and the trigger is read from `argv[0]`, because each
# one has to ask git a different question. `diff-tree ... HEAD` is
# commit-shaped: it prints nothing at all for a merge commit, and a checkout's
# HEAD says nothing about what the checkout moved. So a copy of the
# post-commit body under the other two names would never render.
#
# Rendered output goes stale the moment its source changes, and a stale page is
# worse than a missing one because it looks current. This is what keeps the
# dashboard's Documents tab a statement about the render rather than about who
# remembered to run it.
#
# Never fails the git command. The commit, the merge or the checkout is already
# made when this runs, so exiting non-zero would report a failure for work that
# succeeded. A render that cannot run says so and leaves git alone.
#
# `SD_SKIP_RENDER=1` skips it, for all three.
#
# A render executes research.conf.py. After a commit that is the operator's
# own config; after a pull or a checkout it can be one nobody here has read.
# So the post-merge and post-checkout arms decline to render when the
# operation changed research.conf.py, and say how to render once it is read
# (sd:1376). Whether a repository may instruct this tool at all is sd:1375.

import os
import shutil
import subprocess
import sys

# What `diff_argv` answers for a trigger that renders without asking git
# anything. Not an empty list: "git named nothing" is a statement about the
# tree, and this is the refusal to make one.
ALWAYS = "always"


def diff_argv(trigger, argv):
    # The git command that names what this trigger moved; ALWAYS to render
    # without asking; None for an invocation that renders nothing. Three
    # answers and not two, because "ask git nothing" and "git answered
    # nothing" are different, and only the second is a statement about the
    # tree.
    if trigger == "post-commit":
        # Only when the commit carried a document. A commit touching nothing
        # but the config or a script still renders: both change the output.
        return ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]
    if trigger == "post-merge":
        # HEAD is the merge commit and `diff-tree` prints nothing for one, so
        # the post-commit question is unusable here. ORIG_HEAD is where this
        # branch stood before the merge, which is what the pull changed from.
        return ["diff", "--name-only", "ORIG_HEAD", "HEAD"]
    if trigger == "post-checkout":
        # git passes the previous HEAD, the new HEAD, and 1 for a branch
        # checkout or 0 for a file checkout.
        #
        # All zeros for the previous HEAD is a fresh clone or a
        # `git worktree add`: nothing is rendered there yet and nothing
        # has been committed to it, so a render would publish a checkout
        # nobody has worked in. Said here rather than left to the `git
        # diff 0000000 <new>` below failing into the caller's error arm:
        # that arm returns 0 for any failure of that command, so the day
        # something else throws in it a fresh clone stops rendering and
        # the test that pins this goes on passing.
        if len(argv) >= 2 and argv[1] and set(argv[1]) <= {"0"}:
            return None
        # A branch checkout has a range, and the range is the only thing
        # that names what it moved.
        if len(argv) >= 4 and argv[3] == "1" and argv[1] != argv[2]:
            return ["diff", "--name-only", argv[1], argv[2]]
        # Everything else this trigger fires for moved no branch, so its two
        # revisions are equal and there is no range -- and it renders anyway,
        # without a question, because there is no question worth asking here.
        #
        # Do not reintroduce `diff --name-only HEAD` as an optimisation. It
        # looks like the right guard and it is the wrong one: it asks whether
        # the tree differs from HEAD, and what a mirror needs to know is
        # whether the tree differs from what was published. Those agree only
        # while the published copy tracks HEAD, and a file checkout is exactly
        # what breaks that. `git checkout <rev> -- doc.md` publishes the older
        # text; `git checkout HEAD -- doc.md` then restores a clean tree, the
        # guard sees no diff, and the published copy keeps the reverted text
        # for good. Comparing against what was published instead would be a
        # second record of the same fact, which is the failure this contract
        # exists to close.
        #
        # So it renders on an unchanged tree too. That is the price, and it is
        # small: a render that finds nothing changed is cheap, and a mirror
        # holding text nobody can see is not.
        return ALWAYS
    # An unrecognised name is not this hook's trigger. Guessing would run a
    # render on a git event nobody installed it for.
    return None


def conf_argv(trigger, argv):
    # The git command that names research.conf.py when this operation changed
    # it, or None for the post-commit arm, whose commit the operator made. A
    # file checkout has no range, so its question is whether the tree's config
    # is still HEAD's; an uncommitted edit of one's own is declined with it,
    # which costs one command typed by hand.
    if trigger == "post-merge":
        return ["diff", "--name-only", "ORIG_HEAD", "HEAD", "--", "research.conf.py"]
    if trigger == "post-checkout":
        if len(argv) >= 4 and argv[3] == "1" and argv[1] != argv[2]:
            return ["diff", "--name-only", argv[1], argv[2], "--", "research.conf.py"]
        return ["diff", "--name-only", "HEAD", "--", "research.conf.py"]
    return None


def main(argv):
    if os.environ.get("SD_SKIP_RENDER"):
        return 0
    trigger = os.path.basename(argv[0]) if argv else ""
    query = diff_argv(trigger, argv)
    if query is None:
        return 0
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if not os.path.isfile(os.path.join(root, "research.conf.py")):
            return 0
        if query != ALWAYS:
            changed = subprocess.run(
                ["git"] + query,
                cwd=root, capture_output=True, text=True, timeout=30, check=True,
            ).stdout.split()
            if not any(name.endswith((".md", ".py")) for name in changed):
                return 0
        conf = conf_argv(trigger, argv)
        if conf is not None and subprocess.run(
                ["git"] + conf,
                cwd=root, capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip():
            print("%s: this changed research.conf.py, which a render executes; "
                  "read it, then run `sd-research-kit render`. docs/dashboard/ "
                  "is stale until then" % trigger, file=sys.stderr)
            return 0
    except (OSError, subprocess.SubprocessError):
        return 0

    kit = shutil.which("sd-research-kit")
    if kit is None:
        print("%s: sd-research-kit not on PATH; docs/dashboard/ is now stale"
              % trigger, file=sys.stderr)
        return 0
    try:
        subprocess.run([kit, "render"], cwd=root, timeout=600, check=True)
    except (OSError, subprocess.SubprocessError):
        print("%s: render failed; docs/dashboard/ is now stale" % trigger,
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
''',
    # `c2b10b20`, sd:1376's second body: it read the render record after a
    # pull or a checkout and exempted post-commit, so an unrelated commit
    # after a declined pull executed the incoming config.
    '''#!/usr/bin/env python3
# Re-render this research repo when git changes a document.
#
# Installed by `sd-research-kit init-hook` under three names at once:
# post-commit, post-merge and post-checkout. A pull, a branch switch and a
# checkout of one path change the documents as surely as a commit does, and
# a `docs/dashboard/` left from the old tree is the stale render this exists
# to prevent. Post-commit and not pre-commit: `docs/dashboard/` is generated
# and not committed, so there is nothing to stage, and the commit is the
# revision a queued mirror should name.
#
# One source, three names, and the trigger is read from `argv[0]`, because each
# one has to ask git a different question. `diff-tree ... HEAD` is
# commit-shaped: it prints nothing at all for a merge commit, and a checkout's
# HEAD says nothing about what the checkout moved. So a copy of the
# post-commit body under the other two names would never render.
#
# Rendered output goes stale the moment its source changes, and a stale page is
# worse than a missing one because it looks current. This is what keeps the
# dashboard's Documents tab a statement about the render rather than about who
# remembered to run it.
#
# Never fails the git command. The commit, the merge or the checkout is already
# made when this runs, so exiting non-zero would report a failure for work that
# succeeded. A render that cannot run says so and leaves git alone.
#
# `SD_SKIP_RENDER=1` skips it, for all three.
#
# A render executes research.conf.py. After a commit that is the operator's
# own config; after a pull or a checkout it can be one nobody here has read.
# So the post-merge and post-checkout arms render only a config that a render
# here has executed before, and otherwise say how to render once it is read
# (sd:1376). Whether a repository may instruct this tool at all is sd:1375.

import hashlib
import os
import shutil
import subprocess
import sys

# What `diff_argv` answers for a trigger that renders without asking git
# anything. Not an empty list: "git named nothing" is a statement about the
# tree, and this is the refusal to make one.
ALWAYS = "always"


def diff_argv(trigger, argv):
    # The git command that names what this trigger moved; ALWAYS to render
    # without asking; None for an invocation that renders nothing. Three
    # answers and not two, because "ask git nothing" and "git answered
    # nothing" are different, and only the second is a statement about the
    # tree.
    if trigger == "post-commit":
        # Only when the commit carried a document. A commit touching nothing
        # but the config or a script still renders: both change the output.
        return ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]
    if trigger == "post-merge":
        # HEAD is the merge commit and `diff-tree` prints nothing for one, so
        # the post-commit question is unusable here. ORIG_HEAD is where this
        # branch stood before the merge, which is what the pull changed from.
        return ["diff", "--name-only", "ORIG_HEAD", "HEAD"]
    if trigger == "post-checkout":
        # git passes the previous HEAD, the new HEAD, and 1 for a branch
        # checkout or 0 for a file checkout.
        #
        # All zeros for the previous HEAD is a fresh clone or a
        # `git worktree add`: nothing is rendered there yet and nothing
        # has been committed to it, so a render would publish a checkout
        # nobody has worked in. Said here rather than left to the `git
        # diff 0000000 <new>` below failing into the caller's error arm:
        # that arm returns 0 for any failure of that command, so the day
        # something else throws in it a fresh clone stops rendering and
        # the test that pins this goes on passing.
        if len(argv) >= 2 and argv[1] and set(argv[1]) <= {"0"}:
            return None
        # A branch checkout has a range, and the range is the only thing
        # that names what it moved.
        if len(argv) >= 4 and argv[3] == "1" and argv[1] != argv[2]:
            return ["diff", "--name-only", argv[1], argv[2]]
        # Everything else this trigger fires for moved no branch, so its two
        # revisions are equal and there is no range -- and it renders anyway,
        # without a question, because there is no question worth asking here.
        #
        # Do not reintroduce `diff --name-only HEAD` as an optimisation. It
        # looks like the right guard and it is the wrong one: it asks whether
        # the tree differs from HEAD, and what a mirror needs to know is
        # whether the tree differs from what was published. Those agree only
        # while the published copy tracks HEAD, and a file checkout is exactly
        # what breaks that. `git checkout <rev> -- doc.md` publishes the older
        # text; `git checkout HEAD -- doc.md` then restores a clean tree, the
        # guard sees no diff, and the published copy keeps the reverted text
        # for good. Comparing against what was published instead would be a
        # second record of the same fact, which is the failure this contract
        # exists to close.
        #
        # So it renders on an unchanged tree too. That is the price, and it is
        # small: a render that finds nothing changed is cheap, and a mirror
        # holding text nobody can see is not.
        return ALWAYS
    # An unrecognised name is not this hook's trigger. Guessing would run a
    # render on a git event nobody installed it for.
    return None


# Where `sd-research-kit render` records the sha256 of the research.conf.py
# it executed: under the git directory, per checkout, never in the tree.
TRUSTED = "sd-research-conf.sha256"


def conf_trusted(root):
    # Whether the tree's research.conf.py is one a render executed here
    # before: by hand, after the operator read it, or after the operator's own
    # commit. Asking git whether this operation changed the config is not
    # enough. A declined pull leaves the incoming config in the tree, and the
    # next file checkout finds it equal to HEAD and would execute it. No
    # record means no render has vouched for it, which declines too.
    path = subprocess.run(
        ["git", "rev-parse", "--git-path", TRUSTED],
        cwd=root, capture_output=True, text=True, timeout=30, check=True,
    ).stdout.strip()
    try:
        with open(os.path.join(root, path), encoding="utf-8") as handle:
            recorded = handle.read().strip()
        with open(os.path.join(root, "research.conf.py"), "rb") as handle:
            current = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return False
    return recorded == current


def main(argv):
    if os.environ.get("SD_SKIP_RENDER"):
        return 0
    trigger = os.path.basename(argv[0]) if argv else ""
    query = diff_argv(trigger, argv)
    if query is None:
        return 0
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if not os.path.isfile(os.path.join(root, "research.conf.py")):
            return 0
        if query != ALWAYS:
            changed = subprocess.run(
                ["git"] + query,
                cwd=root, capture_output=True, text=True, timeout=30, check=True,
            ).stdout.split()
            if not any(name.endswith((".md", ".py")) for name in changed):
                return 0
        if trigger != "post-commit" and not conf_trusted(root):
            print("%s: no render here has executed this research.conf.py, and "
                  "a render executes it; read it, then run `sd-research-kit "
                  "render`. docs/dashboard/ is stale until then" % trigger,
                  file=sys.stderr)
            return 0
    except (OSError, subprocess.SubprocessError):
        return 0

    kit = shutil.which("sd-research-kit")
    if kit is None:
        print("%s: sd-research-kit not on PATH; docs/dashboard/ is now stale"
              % trigger, file=sys.stderr)
        return 0
    try:
        subprocess.run([kit, "render"], cwd=root, timeout=600, check=True)
    except (OSError, subprocess.SubprocessError):
        print("%s: render failed; docs/dashboard/ is now stale" % trigger,
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
''',
    # `e81f7fc0`, sd:1376's third body, replaced by sd:1991's: a branch
    # checkout behind its upstream rendered and queued the stale tree.
    '''#!/usr/bin/env python3
# Re-render this research repo when git changes a document.
#
# Installed by `sd-research-kit init-hook` under three names at once:
# post-commit, post-merge and post-checkout. A pull, a branch switch and a
# checkout of one path change the documents as surely as a commit does, and
# a `docs/dashboard/` left from the old tree is the stale render this exists
# to prevent. Post-commit and not pre-commit: `docs/dashboard/` is generated
# and not committed, so there is nothing to stage, and the commit is the
# revision a queued mirror should name.
#
# One source, three names, and the trigger is read from `argv[0]`, because each
# one has to ask git a different question. `diff-tree ... HEAD` is
# commit-shaped: it prints nothing at all for a merge commit, and a checkout's
# HEAD says nothing about what the checkout moved. So a copy of the
# post-commit body under the other two names would never render.
#
# Rendered output goes stale the moment its source changes, and a stale page is
# worse than a missing one because it looks current. This is what keeps the
# dashboard's Documents tab a statement about the render rather than about who
# remembered to run it.
#
# Never fails the git command. The commit, the merge or the checkout is already
# made when this runs, so exiting non-zero would report a failure for work that
# succeeded. A render that cannot run says so and leaves git alone.
#
# `SD_SKIP_RENDER=1` skips it, for all three.
#
# A render executes research.conf.py, and after a pull or a checkout it can
# be one nobody here has read. A commit does not vouch for it either: after a
# declined pull, a commit of doc.md alone leaves the incoming config in the
# tree. So all three render only a config a render by hand has executed here,
# and otherwise say how to render once it is read. The render they run is told
# it is automatic, checks the bytes it executes again, and records nothing
# (sd:1376). Whether a repository may instruct this tool at all is sd:1375.

import hashlib
import os
import shutil
import subprocess
import sys

# What `diff_argv` answers for a trigger that renders without asking git
# anything. Not an empty list: "git named nothing" is a statement about the
# tree, and this is the refusal to make one.
ALWAYS = "always"


def diff_argv(trigger, argv):
    # The git command that names what this trigger moved; ALWAYS to render
    # without asking; None for an invocation that renders nothing. Three
    # answers and not two, because "ask git nothing" and "git answered
    # nothing" are different, and only the second is a statement about the
    # tree.
    if trigger == "post-commit":
        # Only when the commit carried a document. A commit touching nothing
        # but the config or a script still renders: both change the output.
        return ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]
    if trigger == "post-merge":
        # HEAD is the merge commit and `diff-tree` prints nothing for one, so
        # the post-commit question is unusable here. ORIG_HEAD is where this
        # branch stood before the merge, which is what the pull changed from.
        return ["diff", "--name-only", "ORIG_HEAD", "HEAD"]
    if trigger == "post-checkout":
        # git passes the previous HEAD, the new HEAD, and 1 for a branch
        # checkout or 0 for a file checkout.
        #
        # All zeros for the previous HEAD is a fresh clone or a
        # `git worktree add`: nothing is rendered there yet and nothing
        # has been committed to it, so a render would publish a checkout
        # nobody has worked in. Said here rather than left to the `git
        # diff 0000000 <new>` below failing into the caller's error arm:
        # that arm returns 0 for any failure of that command, so the day
        # something else throws in it a fresh clone stops rendering and
        # the test that pins this goes on passing.
        if len(argv) >= 2 and argv[1] and set(argv[1]) <= {"0"}:
            return None
        # A branch checkout has a range, and the range is the only thing
        # that names what it moved.
        if len(argv) >= 4 and argv[3] == "1" and argv[1] != argv[2]:
            return ["diff", "--name-only", argv[1], argv[2]]
        # Everything else this trigger fires for moved no branch, so its two
        # revisions are equal and there is no range -- and it renders anyway,
        # without a question, because there is no question worth asking here.
        #
        # Do not reintroduce `diff --name-only HEAD` as an optimisation. It
        # looks like the right guard and it is the wrong one: it asks whether
        # the tree differs from HEAD, and what a mirror needs to know is
        # whether the tree differs from what was published. Those agree only
        # while the published copy tracks HEAD, and a file checkout is exactly
        # what breaks that. `git checkout <rev> -- doc.md` publishes the older
        # text; `git checkout HEAD -- doc.md` then restores a clean tree, the
        # guard sees no diff, and the published copy keeps the reverted text
        # for good. Comparing against what was published instead would be a
        # second record of the same fact, which is the failure this contract
        # exists to close.
        #
        # So it renders on an unchanged tree too. That is the price, and it is
        # small: a render that finds nothing changed is cheap, and a mirror
        # holding text nobody can see is not.
        return ALWAYS
    # An unrecognised name is not this hook's trigger. Guessing would run a
    # render on a git event nobody installed it for.
    return None


# Where `sd-research-kit render`, run by hand, records the sha256 of the
# research.conf.py it executed: under the git directory, per checkout, never
# in the tree.
TRUSTED = "sd-research-conf.sha256"

# What this sets, to the trigger's name, for the render it runs. The render
# then executes only a recorded config and records nothing itself.
TRIGGER_ENV = "SD_RESEARCH_TRIGGER"


def conf_trusted(root):
    # Whether the tree's research.conf.py is one a render by hand executed
    # here, after the operator read it. Asking git whether this operation
    # changed the config is not enough, and neither is a commit: a declined
    # pull leaves the incoming config in the tree, and the next file checkout
    # or commit of doc.md alone would execute it. No record means no render
    # has vouched for it, which declines too.
    path = subprocess.run(
        ["git", "rev-parse", "--git-path", TRUSTED],
        cwd=root, capture_output=True, text=True, timeout=30, check=True,
    ).stdout.strip()
    try:
        with open(os.path.join(root, path), encoding="utf-8") as handle:
            recorded = handle.read().strip()
        with open(os.path.join(root, "research.conf.py"), "rb") as handle:
            current = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return False
    return recorded == current


def main(argv):
    if os.environ.get("SD_SKIP_RENDER"):
        return 0
    trigger = os.path.basename(argv[0]) if argv else ""
    query = diff_argv(trigger, argv)
    if query is None:
        return 0
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if not os.path.isfile(os.path.join(root, "research.conf.py")):
            return 0
        if query != ALWAYS:
            changed = subprocess.run(
                ["git"] + query,
                cwd=root, capture_output=True, text=True, timeout=30, check=True,
            ).stdout.split()
            if not any(name.endswith((".md", ".py")) for name in changed):
                return 0
        if not conf_trusted(root):
            print("%s: no render by hand has executed this research.conf.py, and "
                  "a render executes it; read it, then run `sd-research-kit "
                  "render`. docs/dashboard/ is stale until then" % trigger,
                  file=sys.stderr)
            return 0
    except (OSError, subprocess.SubprocessError):
        return 0

    kit = shutil.which("sd-research-kit")
    if kit is None:
        print("%s: sd-research-kit not on PATH; docs/dashboard/ is now stale"
              % trigger, file=sys.stderr)
        return 0
    try:
        subprocess.run([kit, "render"], cwd=root, timeout=600, check=True,
                       env=dict(os.environ, **{TRIGGER_ENV: trigger}))
    except (OSError, subprocess.SubprocessError):
        print("%s: render failed; docs/dashboard/ is now stale" % trigger,
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
''',
    # `8b2b4f07`, sd:1991's body, replaced by sd:1840's: it rendered and
    # checked no links.
    '''#!/usr/bin/env python3
# Re-render this research repo when git changes a document.
#
# Installed by `sd-research-kit init-hook` under three names at once:
# post-commit, post-merge and post-checkout. A pull, a branch switch and a
# checkout of one path change the documents as surely as a commit does, and
# a `docs/dashboard/` left from the old tree is the stale render this exists
# to prevent. Post-commit and not pre-commit: `docs/dashboard/` is generated
# and not committed, so there is nothing to stage, and the commit is the
# revision a queued mirror should name.
#
# One source, three names, and the trigger is read from `argv[0]`, because each
# one has to ask git a different question. `diff-tree ... HEAD` is
# commit-shaped: it prints nothing at all for a merge commit, and a checkout's
# HEAD says nothing about what the checkout moved. So a copy of the
# post-commit body under the other two names would never render.
#
# Rendered output goes stale the moment its source changes, and a stale page is
# worse than a missing one because it looks current. This is what keeps the
# dashboard's Documents tab a statement about the render rather than about who
# remembered to run it.
#
# Never fails the git command. The commit, the merge or the checkout is already
# made when this runs, so exiting non-zero would report a failure for work that
# succeeded. A render that cannot run says so and leaves git alone.
#
# `SD_SKIP_RENDER=1` skips it, for all three.
#
# A render executes research.conf.py, and after a pull or a checkout it can
# be one nobody here has read. A commit does not vouch for it either: after a
# declined pull, a commit of doc.md alone leaves the incoming config in the
# tree. So all three render only a config a render by hand has executed here,
# and otherwise say how to render once it is read. The render they run is told
# it is automatic, checks the bytes it executes again, and records nothing
# (sd:1376). Whether a repository may instruct this tool at all is sd:1375.
#
# A branch checkout that lands behind its upstream renders and queues no
# mirror (sd:1991). Checking out a stale `main` and pulling it is the usual
# way to update, and the checkout's render queued the stale text for the
# shared mirror a moment before the pull's render replaced it: a drain in
# between published it. The render is told why in `SD_SKIP_MIRROR`. Only
# the local tracking ref is read, so nothing here fetches, and a branch with
# no upstream renders and queues as before.

import hashlib
import os
import shutil
import subprocess
import sys

# What `diff_argv` answers for a trigger that renders without asking git
# anything. Not an empty list: "git named nothing" is a statement about the
# tree, and this is the refusal to make one.
ALWAYS = "always"


def diff_argv(trigger, argv):
    # The git command that names what this trigger moved; ALWAYS to render
    # without asking; None for an invocation that renders nothing. Three
    # answers and not two, because "ask git nothing" and "git answered
    # nothing" are different, and only the second is a statement about the
    # tree.
    if trigger == "post-commit":
        # Only when the commit carried a document. A commit touching nothing
        # but the config or a script still renders: both change the output.
        return ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]
    if trigger == "post-merge":
        # HEAD is the merge commit and `diff-tree` prints nothing for one, so
        # the post-commit question is unusable here. ORIG_HEAD is where this
        # branch stood before the merge, which is what the pull changed from.
        return ["diff", "--name-only", "ORIG_HEAD", "HEAD"]
    if trigger == "post-checkout":
        # git passes the previous HEAD, the new HEAD, and 1 for a branch
        # checkout or 0 for a file checkout.
        #
        # All zeros for the previous HEAD is a fresh clone or a
        # `git worktree add`: nothing is rendered there yet and nothing
        # has been committed to it, so a render would publish a checkout
        # nobody has worked in. Said here rather than left to the `git
        # diff 0000000 <new>` below failing into the caller's error arm:
        # that arm returns 0 for any failure of that command, so the day
        # something else throws in it a fresh clone stops rendering and
        # the test that pins this goes on passing.
        if len(argv) >= 2 and argv[1] and set(argv[1]) <= {"0"}:
            return None
        # A branch checkout has a range, and the range is the only thing
        # that names what it moved.
        if len(argv) >= 4 and argv[3] == "1" and argv[1] != argv[2]:
            return ["diff", "--name-only", argv[1], argv[2]]
        # Everything else this trigger fires for moved no branch, so its two
        # revisions are equal and there is no range -- and it renders anyway,
        # without a question, because there is no question worth asking here.
        #
        # Do not reintroduce `diff --name-only HEAD` as an optimisation. It
        # looks like the right guard and it is the wrong one: it asks whether
        # the tree differs from HEAD, and what a mirror needs to know is
        # whether the tree differs from what was published. Those agree only
        # while the published copy tracks HEAD, and a file checkout is exactly
        # what breaks that. `git checkout <rev> -- doc.md` publishes the older
        # text; `git checkout HEAD -- doc.md` then restores a clean tree, the
        # guard sees no diff, and the published copy keeps the reverted text
        # for good. Comparing against what was published instead would be a
        # second record of the same fact, which is the failure this contract
        # exists to close.
        #
        # So it renders on an unchanged tree too. That is the price, and it is
        # small: a render that finds nothing changed is cheap, and a mirror
        # holding text nobody can see is not.
        return ALWAYS
    # An unrecognised name is not this hook's trigger. Guessing would run a
    # render on a git event nobody installed it for.
    return None


# Where `sd-research-kit render`, run by hand, records the sha256 of the
# research.conf.py it executed: under the git directory, per checkout, never
# in the tree.
TRUSTED = "sd-research-conf.sha256"

# What this sets, to the trigger's name, for the render it runs. The render
# then executes only a recorded config and records nothing itself.
TRIGGER_ENV = "SD_RESEARCH_TRIGGER"

# What this sets, to a reason, for a render that must queue no mirror.
SKIP_MIRROR = "SD_SKIP_MIRROR"


def behind_upstream(root):
    # How many commits this branch's upstream has that HEAD lacks, read off
    # the local tracking ref; 0 when there is no upstream to be behind. No
    # fetch: a hook that reached the network would make every checkout wait
    # on it, and the ref a pull is about to fast-forward to is already local
    # whenever the pull is `--ff-only` after a fetch.
    counted = subprocess.run(
        ["git", "rev-list", "--count", "HEAD..@{u}"],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    if counted.returncode != 0:
        return 0
    try:
        return int(counted.stdout.strip() or 0)
    except ValueError:
        return 0


def conf_trusted(root):
    # Whether the tree's research.conf.py is one a render by hand executed
    # here, after the operator read it. Asking git whether this operation
    # changed the config is not enough, and neither is a commit: a declined
    # pull leaves the incoming config in the tree, and the next file checkout
    # or commit of doc.md alone would execute it. No record means no render
    # has vouched for it, which declines too.
    path = subprocess.run(
        ["git", "rev-parse", "--git-path", TRUSTED],
        cwd=root, capture_output=True, text=True, timeout=30, check=True,
    ).stdout.strip()
    try:
        with open(os.path.join(root, path), encoding="utf-8") as handle:
            recorded = handle.read().strip()
        with open(os.path.join(root, "research.conf.py"), "rb") as handle:
            current = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return False
    return recorded == current


def main(argv):
    if os.environ.get("SD_SKIP_RENDER"):
        return 0
    trigger = os.path.basename(argv[0]) if argv else ""
    query = diff_argv(trigger, argv)
    if query is None:
        return 0
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        if not os.path.isfile(os.path.join(root, "research.conf.py")):
            return 0
        if query != ALWAYS:
            changed = subprocess.run(
                ["git"] + query,
                cwd=root, capture_output=True, text=True, timeout=30, check=True,
            ).stdout.split()
            if not any(name.endswith((".md", ".py")) for name in changed):
                return 0
        env = dict(os.environ, **{TRIGGER_ENV: trigger})
        # A branch checkout is the only trigger with a range and no pull of
        # its own; post-merge is the pull, and renders the tree it brought.
        behind = behind_upstream(root) if (
            trigger == "post-checkout" and query != ALWAYS) else 0
        if behind:
            env[SKIP_MIRROR] = ("%s: HEAD is %d commit%s behind its upstream; "
                                "pull, and that render queues the mirrors"
                                % (trigger, behind, "" if behind == 1 else "s"))
        if not conf_trusted(root):
            print("%s: no render by hand has executed this research.conf.py, and "
                  "a render executes it; read it, then run `sd-research-kit "
                  "render`. docs/dashboard/ is stale until then" % trigger,
                  file=sys.stderr)
            return 0
    except (OSError, subprocess.SubprocessError):
        return 0

    kit = shutil.which("sd-research-kit")
    if kit is None:
        print("%s: sd-research-kit not on PATH; docs/dashboard/ is now stale"
              % trigger, file=sys.stderr)
        return 0
    try:
        subprocess.run([kit, "render"], cwd=root, timeout=600, check=True,
                       env=env)
    except (OSError, subprocess.SubprocessError):
        print("%s: render failed; docs/dashboard/ is now stale" % trigger,
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
''',
)


def init_hook_main() -> int:
    """Install the re-render hook in this repo, under every trigger at once.

    Written and not symlinked: a research repo is not a worktree of the pack,
    and a symlink into a checkout the repo does not own breaks the moment the
    pack moves. A file already at one of the paths is refused by name and left
    alone -- the same rule `make hooks` follows for the pack's own hook --
    unless it is byte-for-byte a body this pack released, in which case it is
    this hook and is replaced. Every existing installation is an older body on
    `post-commit` alone; without that reading, the one verb that upgrades them
    would refuse every repository that already has the hook and serve only the
    ones that do not exist yet.

    All or nothing. Every target is inspected before any is written, so a
    refusal leaves the repository exactly as it found it. A repo installed on
    one trigger and not the others renders on a commit and silently not on a
    pull, which is worse than no hook at all: `docs/dashboard/` then looks
    maintained.
    A partial install from an earlier run is completed rather than refused.
    """
    repo = Path.cwd()
    if not (repo / "research.conf.py").is_file():
        print("no research.conf.py in %s" % repo, file=sys.stderr)
        return 2
    # The common directory, not `.git`: a linked worktree's hooks live in the
    # main checkout's, and installing into the worktree's own would give one
    # branch a hook every other branch does not have.
    common = git_output(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo)
    if not common:
        print("not a git repository: %s" % repo, file=sys.stderr)
        return 1

    targets = [Path(common) / "hooks" / name for name in HOOK_TRIGGERS]
    held = {t: t.read_text(encoding="utf-8", errors="replace")
            for t in targets if t.exists()}
    ours = (HOOK,) + SUPERSEDED_HOOKS
    foreign = [t for t, text in held.items() if text not in ours]
    if foreign:
        for target in foreign:
            print("error: %s exists and is not this hook; move it aside first"
                  % target, file=sys.stderr)
        return 1
    write = [t for t in targets if held.get(t) != HOOK]
    if not write:
        print("hook: already installed at %s"
              % ", ".join(str(t) for t in targets))
        return 0
    for target in write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(HOOK, encoding="utf-8")
        target.chmod(0o755)
        print("hook: %s %s"
              % ("upgraded" if target in held else "installed", target))
    return 0


def publish_main() -> int:
    """The `publish` verb. Named like `init_hook_main` and
    `sd_research_review.init_main`, not `main`: every `bin/` module carrying a
    `main` puts one more name the dead-code check cannot speak for into
    `tests/test_code_health.py`'s ambiguous set, and that ceiling is downward
    only.
    """
    repo = Path.cwd()
    ns = execute_conf(repo, "publish")
    if ns is None:
        print("no research.conf.py in %s" % repo, file=sys.stderr)
        return 2
    reports = publish(repo, ns.get("PROJECT"), ns.get("DOCS", []))
    for line in reports:
        print(line)
    return exit_status(reports)


if __name__ == "__main__":
    sys.exit(publish_main())
