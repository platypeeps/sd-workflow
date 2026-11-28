"""Which repositories the system knows about, and how they get into the table.

Criterion 6 enumerates `docs/work/*/prd.md` across "every repository the
`repo` table holds". Nothing in requirement 2 said how a row gets into that
table, and an unpopulated table makes the criterion pass over nothing: zero
repositories, zero files, zero rows with no file. A criterion that passes
vacuously is worse than one that fails, because it reports a green.

So the table is populated two ways, and only two:

* **By hand**, `sd-db repo add <path>`. One repository, deliberately, with
  its remote read from the checkout rather than typed.
* **From repo-sync's `repos.common.conf` and `repos.<profile>.conf`**,
  `sd-db repo seed`. That pair lives in `<config>/repo-sync/` (see
  `config.py`) and is what `repo-sync` clones on this machine's profile,
  one `<subdir> <owner/repo>` per line, and it is maintained because
  the sync breaks when it is wrong. Deriving from it beats a second list that
  only drifts. Reading the personal file alone missed every entry that lives
  in common.

Seeding registers only checkouts that exist on disk. A line naming a
repository this machine has not cloned is not an error -- the file is shared
across machines -- and registering it would put a row in the table for a
path nothing can enumerate, which criterion 6 counts as a row with no file.

**The table is the bound, not the disk.** Item D's runner clones a registered
repository's whole working tree into the worktrees directory, so a clone
carries its own `docs/work/*/prd.md` files. Those files are on the machine
and have no `item` row. They are never enumerated because enumeration reads
this table and a clone was never added to it -- there is no exclusion list to
keep in step. `add` refuses a path under the worktrees directory anyway, so
an operator cannot put one there by hand and discover the consequence at the
next sitting.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import config, paths
from .database import transaction
from .errors import SdDbError
from .writes import declarations_text as _declarations_text
from .writes import now, policy_ci, policy_merge, upsert_repo

#: `<subdir> <owner/repo>`, with `#` comments and blank lines. The format is
#: the first line of the file itself, and this is the whole grammar.
CONF_LINE = re.compile(r"^(?P<group>[A-Za-z0-9._-]+)\s+(?P<slug>[^\s/]+/[^\s/]+)\s*$")

#: The config folder `repo-sync` reads its lists from, under the config root.
CONF_TOOL = "repo-sync"

#: The profile `repo-sync` falls back to, and the one read here unless
#: `REPO_SYNC_PROFILE` names another.
DEFAULT_PROFILE = "personal"

#: The file `repo-sync` layers under every profile but terra. It sits next to
#: the profile conf, and is found from it rather than from here.
COMMON_CONF_NAME = "repos.common.conf"

#: Where item D's runner puts a clone. Not read to exclude anything -- the
#: `repo` table is the bound -- only to refuse registering one.
WORKTREES_RELATIVE = Path(".local/share/sd/worktrees")


class RepoRefusal(SdDbError):
    """A path that cannot be a registered repository, and why."""


class ConfMissing(FileNotFoundError):
    """The profile conf a default seed reads is not in the config folder.

    A `FileNotFoundError`, so a caller that already treats an absent conf as
    "nothing to seed" keeps doing so; the message names the remedy.
    """


@dataclass(frozen=True)
class Checkout:
    """One line of the conf, resolved against the disk."""

    group: str
    slug: str
    path: Path
    present: bool

    @property
    def remote(self) -> str:
        return f"https://github.com/{self.slug}"


def repo_root(environ: dict[str, str] | None = None) -> Path:
    """Where the checkouts live. `SD_REPO_ROOT`, else `~/repos`.

    The same variable the pack's dashboard reads, spelled the same way, so a
    machine that moved its checkouts moves both at once.
    """
    env = os.environ if environ is None else environ
    return Path(os.path.expanduser(env.get("SD_REPO_ROOT") or "~/repos"))


def worktrees_root(home: Path | str | None = None) -> Path:
    base = Path(home) if home is not None else Path(os.environ.get("HOME", "~")).expanduser()
    return base / WORKTREES_RELATIVE


def profile(environ: dict[str, str] | None = None) -> str:
    """`REPO_SYNC_PROFILE`, else `personal`: the list `repo-sync` clones here."""
    env = os.environ if environ is None else environ
    return env.get("REPO_SYNC_PROFILE") or DEFAULT_PROFILE


def conf_path(root: Path | None = None, environ: dict[str, str] | None = None) -> Path:
    """`<config>/repo-sync/repos.<profile>.conf`.

    `conf_paths` adds the common conf beside it; this names the profile one.
    `root` names the folder that holds the lists instead of the config dir.

    Resolved from the environment rather than from the working directory:
    the seed runs from cron, whose working directory is the operator's home.
    """
    base = Path(root) if root is not None else config.config_dir(CONF_TOOL, environ)
    return base / f"repos.{profile(environ)}.conf"


def conf_paths(root: Path | None = None, environ: dict[str, str] | None = None) -> list[Path]:
    """The pair `repo-sync` reads: common first, then the profile conf.

    The same order `repo-sync.sh` iterates, so a list reads in conf order.
    Terra reads its profile conf alone, as `repo-sync.sh` does.
    """
    chosen = conf_path(root, environ)
    if profile(environ) == "terra":
        return [chosen]
    return [chosen.with_name(COMMON_CONF_NAME), chosen]


def missing_conf(path: Path) -> ConfMissing:
    """The st_missing-style refusal for an absent profile conf."""
    return ConfMissing(
        f"{path.name} is not set. Copy local-repo-sync/{path.name}.example "
        f"(or another profile's example) to {path} and fill it in, or name a "
        f"conf: `bin/sd-db repo seed <conf>`."
    )


def read_conf(path: Path | str) -> list[tuple[str, str]]:
    """Every `<group> <owner/repo>` line, in file order.

    A line this grammar does not match is a refusal and not a skip. The file
    drives the clone; a line nobody parses is a repository nobody syncs, and
    discovering that from a missing directory months later is the expensive
    way to find out.
    """
    text = Path(path).read_text(encoding="utf-8")
    entries: list[tuple[str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        # `repo-sync` cuts `#...` off every line before it reads one; so does this.
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        match = CONF_LINE.match(stripped)
        if match is None:
            raise RepoRefusal(
                f"{path}:{number}: {stripped!r} is not `<subdir> <owner/repo>`; "
                f"the format is the file's own first line"
            )
        entries.append((match.group("group"), match.group("slug")))
    return entries


def checkouts(
    path: Path | str | None = None,
    *,
    root: Path | str | None = None,
    environ: dict[str, str] | None = None,
) -> list[Checkout]:
    """The conf resolved against the disk, present flag and all.

    A named `path` is read alone. Without one it is the common and personal
    pair; an entry both name is one checkout, and a missing common file is
    read as empty -- the profile file is the one a seed cannot do without,
    and its absence raises `ConfMissing` naming where to put it.
    """
    base = Path(root) if root is not None else repo_root(environ)
    if path is not None:
        entries = read_conf(path)
    else:
        *common, chosen = conf_paths(environ=environ)
        if not chosen.exists():
            raise missing_conf(chosen)
        entries = [entry for extra in common if extra.is_file() for entry in read_conf(extra)]
        entries = list(dict.fromkeys(entries + read_conf(chosen)))
    found = []
    for group, slug in entries:
        name = slug.split("/", 1)[1]
        target = base / group / name
        found.append(
            Checkout(group=group, slug=slug, path=target, present=(target / ".git").exists())
        )
    return found


def _git(path: Path, *args: str) -> str:
    """Read-only git against a named checkout. Empty on any failure."""
    try:
        done = subprocess.run(  # nosec B603 - fixed argv, shell=False
            ["git", "-C", str(path), *args],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def detect(path: Path | str) -> tuple[str | None, str | None]:
    """`(remote, mode)` read from the checkout, never typed by the operator.

    `mode` is `bare` or `work`: item D's runner cares, and a typed answer
    would be wrong the first time somebody converted one. It is the
    checkout's shape, not the pack's `full|minimal|guest` workflow mode;
    that one lives in the repository's `CLAUDE.local.md` and the table does
    not copy it (sd:1666).

    `remote` is stored as the checkout wrote it: it is the transport the
    runner's `ls-remote` and clone use, so an ssh origin stays ssh. Rows
    are compared through `remote_identity`, never by spelling (sd:1666).
    """
    target = paths.expand(path) if str(path).startswith("~") else Path(path)
    remote = _git(target, "remote", "get-url", "origin") or None
    bare = _git(target, "rev-parse", "--is-bare-repository")
    mode = {"true": "bare", "false": "work"}.get(bare)
    return remote, mode


def add(
    connection: sqlite3.Connection,
    path: Path | str,
    *,
    remote: str | None = None,
    mode: str | None = None,
    home: Path | str | None = None,
) -> str:
    """Register one repository. The row is keyed by `paths.key` of the path:
    `~/...` under `$HOME`, the resolved absolute path elsewhere."""
    target = paths.expand(paths.store(path)).resolve()
    if not (target / ".git").exists() and _git(target, "rev-parse", "--git-dir") == "":
        raise RepoRefusal(f"{target} is not a git checkout")
    # Both sides resolved: on macOS a temporary directory reaches this
    # function as `/var/folders/...` and resolves to `/private/var/...`, and
    # an unresolved comparison would let a clone through under exactly the
    # path the refusal exists for.
    worktrees = worktrees_root(home).resolve()
    if worktrees in target.parents or target == worktrees:
        raise RepoRefusal(
            f"{target} is under the worktrees directory {worktrees}; a runner's "
            f"clone is not a registered repository, and registering one would "
            f"give its `docs/work` files rows the sitting cannot keep"
        )
    found_remote, found_mode = detect(target)
    return upsert_repo(
        connection,
        paths.key(str(target)),
        remote=remote or found_remote,
        mode=mode or found_mode,
    )


@dataclass
class Seeded:
    """What a seed run did, so the caller reports it and a rerun matches."""

    registered: list[str]
    absent: list[str]
    mismatched: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {"registered": len(self.registered), "absent": len(self.absent)}


def seed(
    connection: sqlite3.Connection,
    path: Path | str | None = None,
    *,
    root: Path | str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | str | None = None,
) -> Seeded:
    """Register every checkout the conf names that this machine actually has.

    Idempotent by construction: `upsert_repo` leaves a row it already wrote
    alone but for the fields it was given, so a second seed registers the same
    set and changes nothing else.
    """
    registered: list[str] = []
    absent: list[str] = []
    mismatched: list[str] = []
    for checkout in checkouts(path, root=root, environ=environ):
        if not checkout.present:
            absent.append(checkout.slug)
            continue
        # The checkout's own origin first, the conf's rendering of it second.
        # `Checkout.remote` always renders `https://github.com/<slug>`, because
        # the conf names a slug and not a URL, and passing that as `remote`
        # overrode what `add` had just read off the checkout. A machine that
        # clones over ssh then carried an https remote on every seeded row,
        # and before sd:1436 `same_remote` did not treat the two as one
        # repository -- so `registered_for` stopped resolving a runner clone
        # by origin for exactly the repositories a seed had touched last.
        # The row still records what the checkout says, not a rendering.
        # The conf value stays as the fallback: it is the only answer for a
        # checkout whose origin has been removed.
        found_remote, _ = detect(checkout.path)
        # The line names a repository and the path only where it sits: an
        # origin that names another one is not the line's (sd:4043 review).
        if found_remote and not same_remote(found_remote, checkout.remote):
            mismatched.append(_elsewhere(checkout, [("origin", found_remote)]))
            continue
        registered.append(
            add(connection, checkout.path,
                remote=found_remote or checkout.remote, home=home)
        )
    return Seeded(registered=sorted(set(registered)), absent=sorted(set(absent)), mismatched=mismatched)


def _elsewhere(checkout: Checkout, held: list[tuple[str, str | None]]) -> str:
    """A line whose path holds another repository, naming both."""
    found = ", ".join(f"its {what} is {value}" if value else f"it has no {what}" for what, value in held)
    return (f"{checkout.slug} at {checkout.path}: {found}; nothing was written for this line. Fix the line, "
            f"or the checkout or row at that path")


@dataclass
class SeededClones:
    """What `seed_clones` did: rows it made, rows that gained the profile, rows that held it, lines it could not
    place, and lines whose path holds another repository."""

    registered: list[str]
    added: list[str]
    held: list[str]
    unplaced: list[str]
    mismatched: list[str] = field(default_factory=list)


def seed_clones(
    connection: sqlite3.Connection,
    profile: str,
    *,
    root: Path | str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | str | None = None,
) -> SeededClones:
    """Add `profile` to the row of every line `repo-sync` clones on it here.

    The lines are `repos.common.conf` and `repos.<profile>.conf` in the config
    folder, or the terra conf alone (`conf_paths`); the profile is an argument,
    because a satellite need not set `REPO_SYNC_PROFILE`. A line with no row
    gets one when its checkout is on disk, as `seed` registers it, and is
    named in `unplaced` when it is not: no row can be keyed to a path nothing
    holds, so the machine that holds it runs its own seed. It only adds, and
    each line is its own transaction, so a run stopped at any line is
    finished by running it again (design sd:3983, slice P2).

    A path does not identify a repository: the line's slug does. The
    checkout's origin, when the checkout is here, and the row's remote, when
    there is a row, must each name the line's repository, or that line writes
    nothing and is named in `mismatched`. No origin is an unknown answer, not
    an agreeing one. Unlike `seed`, the conf's slug is never the fallback:
    the profile tells other machines what to clone.
    """
    if profile not in CLONE_PROFILES:
        raise RepoRefusal(f"{profile!r} is not a profile; expected one of {', '.join(CLONE_PROFILES)}")
    env = {**(os.environ if environ is None else environ), "REPO_SYNC_PROFILE": profile}
    result = SeededClones([], [], [], [])
    for checkout in checkouts(root=root, environ=env):
        where = checkout.path.resolve()
        origin = detect(checkout.path)[0] if checkout.present else None
        with transaction(connection):
            row = row_for(connection, where)
            if row is None and not checkout.present:
                result.unplaced.append(checkout.slug)
                continue
            answers = ([("origin", origin)] if checkout.present else []) + \
                ([("remote in its row", row["remote"])] if row is not None else [])
            wrong = [(what, value) for what, value in answers if not same_remote(value, checkout.remote)]
            if wrong:
                result.mismatched.append(_elsewhere(checkout, wrong))
                continue
            if row is None:
                path, held = add(connection, where, remote=origin, home=home), None
                result.registered.append(path)
            else:
                path, held = row["path"], row["clone_profiles"]
            value = clone_profiles(f"+{profile}", held)
            if value == held:
                result.held.append(path)
                continue
            connection.execute("UPDATE repo SET clone_profiles = ?, updated_at = ? WHERE path = ?",
                               (value, now(), path))
            result.added.append(path)
    return result


#: `scheme://[user[:password]@]host[:port]/path`, the URL form of a remote.
#: A `?` or `#` ends the authority, so it may not hide in the user part:
#: `https://a.example#@github.com/o/r` reaches `a.example`, not GitHub.
_URL_REMOTE = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?:(?P<user>[^@/?#]*)@)?(?P<host>[^/:?#]+)"
    r"(?::(?P<port>\d*))?(?P<path>/.*)?$")

#: `[user@]host:path`, git's scp-like form. A colon before any slash is what
#: separates it from a local path; `C:/...` is excluded by the one-letter host.
_SCP_REMOTE = re.compile(r"^(?:(?P<user>[^@/:]+)@)?(?P<host>[^@/:]{2,}):(?P<path>[^/].*)$")

#: One owner or repository name in a github.com path.
_GITHUB_NAME = re.compile(r"[A-Za-z0-9_.-]+")

#: The transports a github.com remote is read from; `git://`, `git+ssh://`
#: and `file://` are not, and stay unknown rather than become GitHub.
_GITHUB_SCHEMES = frozenset({"https", "http", "ssh"})

#: The transports another host is recognised over when the text mentions
#: github.com; any other scheme leaves such a remote unknown.
_OTHER_SCHEMES = frozenset({"https", "http", "ssh", "git"})

#: A host name another host must be spelled with before it can clear a
#: remote whose text mentions github.com.
_HOST_NAME = re.compile(r"[A-Za-z0-9.-]+")


@dataclass(frozen=True)
class RemoteIdentity:
    """What an origin URL names, under the one contract ownership and delivery share (sd:1025).

    `kind` is one of three outcomes:

    - `github`: a github.com remote naming exactly one `owner/name`, over
      https, http, ssh or git's scp-like form. `key` is
      `github.com/<owner>/<name>` lower-cased; `owner` and `name` keep the
      remote's case.
    - `other`: a remote that names something else -- another host, a local
      path or a `file://` URL. `key` is the remote with the host lower-cased
      and a trailing slash and `.git` removed; the rest compares as written.
    - `unknown`: no remote, or text that mentions github.com without naming
      one GitHub repository or one other host. `key` is "", and an unknown
      identity matches nothing, itself included.
    """

    kind: str
    key: str = ""
    owner: str = ""
    name: str = ""


_UNKNOWN = RemoteIdentity("unknown")


def parse_remote(remote: str | None) -> RemoteIdentity:
    """The repository an origin URL names; ownership and delivery both read this.

    On github.com the scheme, the ssh user, a port, a trailing slash, a
    `.git` suffix and letter case are spelling, not identity: GitHub serves
    one repository at every one of them. So `git@github.com:o/r.git`,
    `ssh://git@github.com/o/r` and `https://github.com/o/r` all answer
    `github.com/o/r` -- the reduction `local-repo-sync/repo-sync.sh` makes
    before it compares an origin with its conf.

    Any other host is not assumed to work that way. Two ports can be two
    servers, and `alice@host:r` and `bob@host:r` resolve against two home
    directories (sd:1436 review). There only the host's case, a trailing
    slash and a `.git` suffix are removed, and the rest compares as written.
    A local path or `file://` URL, which test fixtures clone from, is the
    same case with no host.

    Text that mentions github.com and is neither is `unknown`: a GitHub
    path that is not exactly `owner/name`, a transport GitHub is not read
    over, a GitHub-looking host, or a local path such as `github.com/o/r`.
    It is never `other`, so a manual merge cannot skip it as unrelated, and
    never `github`, so delivery cannot take it for a repository.
    """
    value = (remote or "").strip()
    if not value:
        return _UNKNOWN
    found = _URL_REMOTE.match(value) or _SCP_REMOTE.match(value)
    scheme = (found.groupdict().get("scheme") or "").lower() if found else ""
    if found is not None and found["host"].lower() == "github.com":
        return _github(found, scheme)
    if "github.com" in value.lower() and not _clear_other_host(found, scheme, value):
        return _UNKNOWN
    if found is not None:
        value = value[:found.start("host")] + found["host"].lower() + value[found.end("host"):]
    return RemoteIdentity("other", value.rstrip("/").removesuffix(".git").rstrip("/"))


def _github(found: re.Match, scheme: str) -> RemoteIdentity:
    """A github.com match as `owner/name`, or unknown when it names no one repository."""
    if scheme and scheme not in _GITHUB_SCHEMES:
        return _UNKNOWN
    if found["user"] == "" or found.groupdict().get("port") == "":
        return _UNKNOWN
    path = (found["path"] or "").removeprefix("/") if scheme else found["path"]
    parts = path.removesuffix("/").removesuffix(".git").split("/")
    if len(parts) != 2 or not all(_GITHUB_NAME.fullmatch(part) for part in parts):
        return _UNKNOWN
    owner, name = parts
    return RemoteIdentity("github", f"github.com/{owner}/{name}".lower(), owner, name)


def _clear_other_host(found: re.Match | None, scheme: str, value: str) -> bool:
    """Whether a remote that mentions github.com still names one other host.

    The host decides, never user or path text: `github.com-bot@gitlab.com:o/r`
    is another host. A malformed port, a backslash, white space, a host that
    itself contains github.com, or a path that carries a `user@github.com`
    authority (`user:token@github.com/o/r` is scp-like for host `user`)
    leaves the answer unknown.
    """
    if found is None or "\\" in value or any(char.isspace() for char in value):
        return False
    if scheme and scheme not in _OTHER_SCHEMES:
        return False
    port = found.groupdict().get("port")
    if port and (len(port) > 5 or int(port) > 65535):
        return False
    host = found["host"]
    if "@github.com" in (found["path"] or "").lower():
        return False
    return bool(_HOST_NAME.fullmatch(host)) and "github.com" not in host.lower()


def remote_identity(remote: str | None) -> str:
    """`parse_remote(remote).key`: `github.com/owner/name` on GitHub, "" when unknown."""
    return parse_remote(remote).key


def same_remote(left: str | None, right: str | None) -> bool:
    """Whether two origin URLs name one repository.

    Both sides reduce through `parse_remote`, so an ssh clone speaks for a
    row that recorded the https spelling (sd:1436). Only spelling is removed:
    a different host, port, user, owner or name is still a different
    repository wherever it can be one, and an absent or unknown remote is
    never one with anything (sd:1025).
    """
    found, other = parse_remote(left), parse_remote(right)
    return found.kind != "unknown" and (found.kind, found.key) == (other.kind, other.key)


def registered_for(connection: sqlite3.Connection, root: str, origin: str | None) -> str:
    """The registered repository this checkout *is*, which need not be where it sits.

    R10-D6 says the working directory selects the repository. It does not say
    the working directory *is* the repository's recorded path, and the runner
    is the case that separates the two: it clones from the repository's remote
    into a work path under `/Volumes/sd-work/worktrees/...`, so anything
    resolving by path alone refuses every run the runner makes -- after the
    command has already been queued, dispatched and cloned.

    The path wins when it is itself registered, so an ordinary checkout is
    unaffected and costs one indexed lookup. Otherwise the origin decides, and
    an unrecognised checkout resolves to itself so the caller's own "not
    registered" refusal is what the user sees. So does an origin two
    registered paths share: the path key is not unique by remote, and the
    first in path order is a guess about which one a clone was made from
    (sd:1219).
    """
    probe = paths.keys(root)
    found = connection.execute(
        f"SELECT path FROM repo WHERE path IN ({paths.placeholders(probe)}) ORDER BY path",
        probe).fetchone()
    if found:
        return str(found["path"])
    if origin:
        matches = [str(row["path"]) for row in connection.execute(
            "SELECT path, remote FROM repo WHERE remote IS NOT NULL ORDER BY path")
            if same_remote(row["remote"], origin)]
        if len(matches) == 1:
            return matches[0]
    return root


def row_for(connection: sqlite3.Connection, path: Path | str) -> sqlite3.Row | None:
    """The `repo` row for a disk path or a key, whichever form the row holds.

    The probe goes through `paths.keys`, so a caller that passes an absolute
    path under the home finds the `~/` row, a caller that passes the key finds
    it too, and a row written before migration 014 still matches (sd:1439).
    """
    probe = paths.keys(path)
    return connection.execute(
        f"SELECT * FROM repo WHERE path IN ({paths.placeholders(probe)}) ORDER BY path",
        probe).fetchone()


#: The columns migration 014 rewrites, each as a query for its stored values
#: and a function from a value to the path it holds (sd:1439).
_KEY_COLUMNS = (
    ("repo.path", "SELECT path FROM repo", lambda value: value),
    ("item.repo", "SELECT repo FROM item WHERE repo IS NOT NULL", lambda value: value),
    ("repo_protection.repo", "SELECT repo FROM repo_protection", lambda value: value),
    ("runner_run.repo", "SELECT repo FROM runner_run", lambda value: value),
    ("runner_lease.repo", "SELECT repo FROM runner_lease", lambda value: value),
    ("item.external_id",
     "SELECT external_id FROM item WHERE source IN ('docs/work', 'writing-piece') "
     "AND instr(external_id, '::') > 0",
     lambda value: value.split("::", 1)[0]),
    ("state.key",
     "SELECT key FROM state WHERE kind = 'verified' "
     "AND (key LIKE '%:status_source' OR key LIKE '%:pieces_source')",
     lambda value: value[:-14]),
    ("cost.repo", "SELECT repo FROM cost WHERE repo IS NOT NULL", lambda value: value),
    ("skill_use.cwd", "SELECT cwd FROM skill_use WHERE cwd IS NOT NULL", lambda value: value),
    ("judgment.location", "SELECT location FROM judgment WHERE location IS NOT NULL",
     lambda value: value),
)


def absolute_under_home(connection: sqlite3.Connection) -> dict[str, int]:
    """How many values in each key column are absolute under this home.

    Migration 014 leaves none, and the library writes none, so a count above
    0 names a writer that bypassed the conversion, or a store not yet at 14.
    A column whose table this store lacks counts 0.
    """
    counts = {}
    for name, query, held in _KEY_COLUMNS:
        try:
            rows = connection.execute(query).fetchall()
        except sqlite3.OperationalError:
            rows = []
        counts[name] = sum(
            1 for (value,) in rows
            if isinstance(value, str) and value.startswith("/")
            and paths.home_relative(held(value)) != held(value))
    return counts


def registered(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every repository the table holds, in path order. The enumeration bound."""
    return list(connection.execute("SELECT * FROM repo ORDER BY path"))


#: The repository word of an item that belongs to none, such as a fleet-wide report (sd:5124).
NO_REPO = "fleet"


def short(repo: Any) -> str | None:
    """A repository key's last part: `~/repos/system` is `system`, `owner/name` is `name`; None for none."""
    return (str(repo).rstrip("/").rpartition("/")[2] or None) if repo else None


def item_ref(item: Any, repo: Any) -> str:
    """How a person reads an item: `system sd:5103`, or `fleet sd:5103` for an item in no repository (sd:5124).

    Machine-read text (trailers, `--ref` keys, `--json`) keeps the bare `sd:<n>`.
    """
    return f"{short(repo) or NO_REPO} sd:{item}"


def item_refs(connection: sqlite3.Connection, items: Iterable[Any], *, fallback: Any = None) -> dict[int, str]:
    """`item_ref` for each id, the repository read from its row; an id with no row takes `fallback`'s."""
    wanted = sorted({int(item) for item in items if str(item).isdigit()})
    held: dict[int, Any] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        held.update((row[0], row[1]) for row in connection.execute(
            f"SELECT id, repo FROM item WHERE id IN ({', '.join('?' for _ in chunk)})", chunk))
    return {item: item_ref(item, held[item] if item in held else fallback) for item in wanted}


#: What `repo.runner_merge` accepts, in the order the CHECK constraint lists
#: them. Named here so the refusal can print them rather than an IntegrityError.
RUNNER_MERGE_VALUES = ("manual", "auto")


def set_runner_merge(
    connection: sqlite3.Connection,
    path: Path | str,
    value: str,
) -> tuple[str, str]:
    """Set one registered repository's `runner_merge`. Returns `(path, before)`.

    The column had four readers and no caller that set it, so every row kept
    the schema default and the only way to change one was to import the
    library and call `upsert_repo` by hand (sd:1131). This is that caller.

    It refuses an unregistered path rather than creating a row: `add` and
    `seed` are the two ways a row gets into the table and this is not a third.
    The value is checked here so an operator typing `automatic` reads a
    sentence naming the two words, not the CHECK constraint's text.
    """
    if value not in RUNNER_MERGE_VALUES:
        accepted = " or ".join(RUNNER_MERGE_VALUES)
        raise RepoRefusal(f"{value!r} is not a merge setting; expected {accepted}")
    return _set_row_setting(connection, path, runner_merge=value)


def _set_row_setting(connection: sqlite3.Connection, path: Path | str, **setting: Any) -> tuple[str, str]:
    """`set_runner_merge` or `set_ci`: one row's column and policy key, read and written in one transaction."""
    given = str(Path(path).expanduser().resolve())
    probe = paths.keys(given)
    with transaction(connection):
        row = connection.execute(
            f"SELECT path, declarations FROM repo WHERE path IN ({paths.placeholders(probe)})",
            probe).fetchone()
        if row is None:
            raise RepoRefusal(
                f"{given} is not a registered repository; run `bin/sd-db repo add {given}`")
        before = (policy_merge if "runner_merge" in setting else policy_ci)(row["declarations"])
        try:
            upsert_repo(connection, row["path"], **setting)
        except SdDbError as error:
            raise RepoRefusal(f"{row['path']}: {error}") from None
    return row["path"], before


def runner_merge(connection: sqlite3.Connection, path: Path | str) -> str | None:
    """One repository's merge setting, `auto` or `manual`, from its `policy.merge` (pack sd:7366); None for no row.

    `manual` for every value but `auto` on a managed policy: a doubt merges nothing.
    """
    row = row_for(connection, str(Path(path).expanduser().resolve()))
    return None if row is None else policy_merge(row["declarations"])


def issue_sources(connection: sqlite3.Connection, path: Path | str) -> dict[str, str]:
    """One repository's issue sources: tracker to the `shadow.repo` its rows carry (sd:3461).

    `{"jira": "EXAMPLE", "github": "example/project"}`, either left out when
    unset; `{}` for no setting. It reads through `repo_channels`, so what that
    cannot read raises here too. A managed repository's `github` reads as
    none, whenever it was set: managed repositories use no GitHub issues.
    """
    from . import channels

    setting = repo_channels(connection, path) or {}
    sources = {name: setting[name][key] for name, key in channels.ISSUE_KEYS.items() if name in setting}
    if "github" in sources:
        probe = paths.keys(str(Path(path).expanduser().resolve()))
        row = connection.execute(
            f"SELECT managed FROM repo WHERE path IN ({paths.placeholders(probe)})", probe).fetchone()
        if row is None or row["managed"]:
            del sources["github"]
    return sources


def mail_aliases(connection: sqlite3.Connection, path: Path | str) -> list[str]:
    """One repository's mail alias labels, sorted; `[]` for none (sd:4191).

    Each names an `alias|<label>|<address>` line in `local-mail-intake`'s
    conf, whose arrivals Today shows under this repository. It reads through
    `repo_channels`, so what that cannot read raises here too, never `[]`.
    """
    from . import channels

    setting = repo_channels(connection, path) or {}
    return list(setting.get("email", {}).get(channels.ALIASES, []))


def companions(repo: str, value: str | None) -> list[str]:
    """The repositories `sd.status_companions` lists beside `repo`: `<repo>=<other>[,<other>]`, `;` between.

    `sd-status` and `sd today --here` read it (sd:8844).
    """
    for entry in (value or "").split(";"):
        name, _, others = entry.partition("=")
        if name == repo:
            return [other for other in others.split(",") if other and other != repo]
    return []


#: A research repository's tracked sources (the research-repo standard); `watched` reads it.
WATCHLIST = Path("20-map") / "MAP-source-watchlist.md"
_WATCHED = re.compile(r"(?:github\.com/|`)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")


def watched(connection: sqlite3.Connection, root: Path | str) -> list[str]:
    """The registered repositories `root`'s watchlist names, by `owner/name`, in its order; `[]` without one.

    A research repository's companions for `sd today --here` (sd:8844): no setting row, the file says.
    """
    try:
        text = (Path(root) / WATCHLIST).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    from .today import repo_keys

    keys = repo_keys(connection)
    named = (keys.get(slug.lower().removesuffix(".git")) for slug in _WATCHED.findall(text))
    return list(dict.fromkeys(path for path in named if path))


#: The `shadow.reason` values that make an issue the operator's (sd:4636).
#: `watched` and NULL (unknown) rows stay synced and stay off Today and Tasks.
MINE = ("assigned", "filed")


def mine_issues(connection: sqlite3.Connection) -> dict[str, list[dict]]:
    """Each repository's open issues that are the operator's, for Today and Tasks (sd:4636).

    `{path: [row, ...]}` for every registered repository whose issue sources
    match at least one open issue `assigned` to or `filed` by the operator,
    newest `last_seen` first. A row's tracker and `shadow.repo` must match one
    of `issue_sources`; GitHub's `owner/name` matches in any case, as GitHub
    reads it. Pull requests stay out: they are contributions, not issues.
    """
    found: dict[str, list[dict]] = {}
    for (path,) in connection.execute("SELECT path FROM repo WHERE channels IS NOT NULL ORDER BY path").fetchall():
        sources = issue_sources(connection, path)
        if not sources:
            continue
        match = " OR ".join("(tracker = ? AND repo = ? COLLATE NOCASE)" for _ in sources)
        rows = connection.execute(
            "SELECT tracker, repo, url, number, title, state, reason, last_seen FROM shadow "
            f"WHERE ({match}) AND kind = 'issue' AND state = 'open' "
            f"AND reason IN ({', '.join('?' * len(MINE))}) ORDER BY last_seen DESC, id DESC",
            [value for pair in sources.items() for value in pair] + list(MINE)).fetchall()
        if rows:
            found[path] = [dict(row) for row in rows]
    return found


def intake_repos(connection: sqlite3.Connection) -> dict[str, str]:
    """Each repository whose `policy.issues` is `read` or `ask`, to the GitHub `owner/name` its remote names
    (sd:4880, pack sd:6849).

    The shadow sync searches these for every issue, not only the operator's,
    and `intake_issues` reads what it stored. A repository whose remote is not
    on github.com has no GitHub issues to take, so it is left out.
    """
    found: dict[str, str] = {}
    for row in connection.execute(
            "SELECT path, remote FROM repo WHERE json_valid(declarations) "
            "AND json_extract(declarations, '$.policy.issues') IN ('read', 'ask') ORDER BY path"):
        remote = parse_remote(row["remote"])
        if remote.kind == "github":
            found[row["path"]] = f"{remote.owner}/{remote.name}"
    return found


def intake_issues(connection: sqlite3.Connection) -> dict[str, list[dict]]:
    """Each intake repository's open third-party issues that no sd item carries (sd:4880).

    `{path: [row, ...]}`, newest `last_seen` first. Third-party is an author
    who is not the operator, and the operator's login is the author of every
    `filed` row, which only an `author:@me` search writes. The reason cannot
    say it: `assigned` outranks `filed`, and a row only the open sweep saw has
    none. An author GitHub no longer names is third-party. Before the first
    `filed` row every author reads third-party, and the operator maps their
    own issue too, as `on` asks for every open issue. An issue is carried
    once an item holds `sd task add --ref github:<owner>/<repo>#N`, in any
    status and any letter case. Pull requests stay out, as in `mine_issues`.

    A repository whose `issues_scope` is `mine` (pack sd:7672) keeps only the
    rows whose reason is in `SCOPE_MINE`. The windowed buckets alone would
    leave an older issue without one, so `sd shadow sync` searches such a
    repository unwindowed and restates every open row's reason
    (`shadow_sync.MINE_SWEEPS`, `writes.restate_reasons`).
    """
    from .review import REF_SOURCE

    found: dict[str, list[dict]] = {}
    for path, slug in intake_repos(connection).items():
        scoped = issues_scope(connection, path) == "mine"
        rows = connection.execute(
            "SELECT tracker, repo, url, number, title, state, reason, author, last_seen FROM shadow "
            "WHERE tracker = 'github' AND repo = ? COLLATE NOCASE AND kind = 'issue' AND state = 'open' "
            "AND (author IS NULL OR author COLLATE NOCASE NOT IN (SELECT mine.author FROM shadow AS mine "
            "WHERE mine.tracker = 'github' AND mine.reason = 'filed' AND mine.author IS NOT NULL)) "
            f"AND (? = 0 OR reason IN ({', '.join('?' * len(SCOPE_MINE))})) "
            "AND NOT EXISTS (SELECT 1 FROM item WHERE item.source = ? "
            "AND item.external_id = 'github:' || shadow.repo || '#' || shadow.number COLLATE NOCASE) "
            "ORDER BY last_seen DESC, id DESC",
            (slug, int(scoped), *SCOPE_MINE, REF_SOURCE)).fetchall()
        if rows:
            found[path] = [dict(row) for row in rows]
    return found


#: The words `repo managed` takes, mapped to what the column holds. Words and
#: not `1`/`0` so the verb reads like `runner-merge`'s `manual|auto`.
MANAGED_VALUES = {"yes": 1, "no": 0}

def set_managed(
    connection: sqlite3.Connection,
    path: Path | str,
    value: str,
    *,
    bypass_standard: bool = False,
) -> tuple[str, str]:
    """Set one registered repository's `managed` flag. Returns `(path, before)`.

    `before` is the word, `yes` or `no`. Same refusals as `set_runner_merge`:
    a value that is not one of the two words, and a path no row holds.
    sd:7710: `no` to `yes` refuses unless `bypass_standard`, so a repository
    does not come up managed with GitHub's defaults: `sd fleet standard`
    (sd:5610) writes this flag as its last step, and the dashboard's Manage
    only chooses it alone; the CLI has no such flag (sd:7732).
    """
    if value not in MANAGED_VALUES:
        raise RepoRefusal(f"{value!r} is not a managed setting; expected yes or no")
    given = str(Path(path).expanduser().resolve())
    probe = paths.keys(given)
    row = connection.execute(
        f"SELECT path, managed FROM repo WHERE path IN ({paths.placeholders(probe)})",
        probe).fetchone()
    if row is None:
        raise RepoRefusal(
            f"{given} is not a registered repository; run `bin/sd-db repo add {given}`")
    target = row["path"]
    before = "yes" if row["managed"] else "no"
    if before == "no" and value == "yes" and not bypass_standard:
        raise RepoRefusal(
            f"{target} turns managed through the standard setup (sd:5610): run `sd fleet standard` "
            f"in {target} to preview it, then `sd fleet standard --apply --digest <sha256>`, which "
            "writes managed last")
    upsert_repo(connection, target, managed=MANAGED_VALUES[value])
    return target, before



#: The machine kinds `repo-sync` clones for, in the order `repo.clone_profiles`
#: spells a set: one spelling per set keeps the column's CHECK a closed list
#: (migration 033, design sd:3983).
CLONE_PROFILES = ("personal", "work", "terra")


def clone_profiles(words: str, held: str | None) -> str | None:
    """The `clone_profiles` value `words` makes of `held`; None is no profile.

    `words` is a set (`personal,work`), `none`, `+P` to add one or `-P` to
    take one away. A word outside `CLONE_PROFILES` or a profile named twice
    refuses in a sentence, not in the CHECK's text.
    """
    current = set(held.split(",")) if held else set()
    if words == "none":
        chosen: set[str] = set()
    elif words[:1] in ("+", "-") and words[1:] in CLONE_PROFILES:
        chosen = current | {words[1:]} if words[0] == "+" else current - {words[1:]}
    else:
        named = words.split(",")
        if not all(name in CLONE_PROFILES for name in named) or len(set(named)) != len(named):
            raise RepoRefusal(
                f"{words!r} is not a clone setting; expected none, +P, -P or a comma list of "
                f"{', '.join(CLONE_PROFILES)}")
        chosen = set(named)
    return ",".join(name for name in CLONE_PROFILES if name in chosen) or None


def set_clones(connection: sqlite3.Connection, path: Path | str, words: str) -> tuple[str, str, str]:
    """Set the profiles one registered repository is cloned on. Returns `(path, before, after)`.

    `before` and `after` are the column's spelling, `none` for NULL. The read
    and the write hold one transaction, so a `+P` and a `-P` on one row land
    in one order or the other and neither is lost. Same path refusal as
    `set_runner_merge`.
    """
    given = str(Path(path).expanduser().resolve())
    probe = paths.keys(given)
    with transaction(connection):
        row = connection.execute(
            f"SELECT path, clone_profiles FROM repo WHERE path IN ({paths.placeholders(probe)})",
            probe).fetchone()
        if row is None:
            raise RepoRefusal(
                f"{given} is not a registered repository; run `bin/sd-db repo add {given}`")
        value = clone_profiles(words, row["clone_profiles"])
        connection.execute("UPDATE repo SET clone_profiles = ?, updated_at = ? WHERE path = ?",
                           (value, now(), row["path"]))
    return row["path"], row["clone_profiles"] or "none", value or "none"


def clone_lines(
    connection: sqlite3.Connection,
    profile: str,
    *,
    root: Path | str | None = None,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """`<subdir> <owner/repo>` for every row cloned on `profile`, in path order.

    The conf grammar `repo-sync` reads (design sd:3983); `.` is the subdir of
    a checkout straight under `root`. Every row must give back its own path:
    under `root` at depth 1 or 2, a GitHub remote, and a folder named as the
    repository. One that does not refuses the whole list, naming every such
    row, because a partial list would read as complete. A store not yet at 33
    raises `SchemaTooOld`, never an empty list.
    """
    from .database import schema_version
    from .errors import SchemaTooOld
    from .schema import SCHEMA_VERSION

    if profile not in CLONE_PROFILES:
        raise RepoRefusal(f"{profile!r} is not a profile; expected one of {', '.join(CLONE_PROFILES)}")
    base = Path(root) if root is not None else repo_root(environ)
    bases = (base, base.resolve())
    try:
        rows = connection.execute(
            "SELECT path, remote, clone_profiles FROM repo WHERE clone_profiles IS NOT NULL ORDER BY path"
        ).fetchall()
    except sqlite3.OperationalError as error:
        if "no such column" in str(error):
            raise SchemaTooOld(schema_version(connection), SCHEMA_VERSION) from error
        raise
    lines, refused = [], []
    for row in rows:
        if profile not in row["clone_profiles"].split(","):
            continue
        where = paths.expand(row["path"])
        relative = next((where.relative_to(each) for each in bases if where.is_relative_to(each)), None)
        found = parse_remote(row["remote"])
        parts = relative.parts if relative is not None else ()
        line = f"{'.' if len(parts) == 1 else parts[0]} {found.owner}/{found.name}" if parts else ""
        if len(parts) not in (1, 2) or found.kind != "github" or parts[-1] != found.name \
                or CONF_LINE.match(line) is None:
            refused.append(f"{row['path']} ({row['remote'] or 'no remote'})")
            continue
        lines.append(line)
    if refused:
        raise RepoRefusal(
            f"{len(refused)} row(s) cloned on {profile} are not a `<subdir> <owner/repo>` line under {base}: "
            f"{'; '.join(refused)}. Each must sit one or two folders below it, named as its GitHub "
            f"repository; fix the row or clear it with `bin/sd-db repo clones PATH -{profile}`")
    return lines


def set_dependabot(connection: sqlite3.Connection, path: Path | str, value: str) -> tuple[str, str]:
    """Set whether the Dependabot sweep may take one repository's pull requests. Returns `(path, before)`.

    The owner rule in the sweep's routine still decides first; this flag is
    the list outside it (design sd:3984). Same words and refusals as
    `set_managed`.
    """
    if value not in MANAGED_VALUES:
        raise RepoRefusal(f"{value!r} is not a dependabot setting; expected yes or no")
    given = str(Path(path).expanduser().resolve())
    probe = paths.keys(given)
    with transaction(connection):
        row = connection.execute(
            f"SELECT path, dependabot FROM repo WHERE path IN ({paths.placeholders(probe)})",
            probe).fetchone()
        if row is None:
            raise RepoRefusal(
                f"{given} is not a registered repository; run `bin/sd-db repo add {given}`")
        connection.execute("UPDATE repo SET dependabot = ?, updated_at = ? WHERE path = ?",
                           (MANAGED_VALUES[value], now(), row["path"]))
    return row["path"], "yes" if row["dependabot"] else "no"


#: The words `repo github-issues` takes: the `policy` declaration's `issues` key (pack sd:6849), which
#: migration 45 moved the `github_issues` column into. `off` is no key; there is no `auto` (note #34749).
ISSUES = ("off", "read", "ask")


def set_github_issues(connection: sqlite3.Connection, path: Path | str, value: str) -> tuple[str, str]:
    """Set one repository's `policy.issues`, keeping its other policy keys. Returns `(path, before)`.

    `off` removes the key, and a policy left empty goes with it. Same
    refusals as `set_runner_merge`: a value that is not one of `ISSUES`,
    exactly, and a path no row holds.
    """
    return _set_policy_word(connection, path, "issues", value, ISSUES, "github-issues")


def _set_policy_word(connection: sqlite3.Connection, path: Path | str, key: str, value: str,
                     words: tuple[str, ...], verb: str) -> tuple[str, str]:
    """Set `policy.<key>` to one of `words`; the first word is the default, which no key holds."""
    if value not in words:
        raise RepoRefusal(f"{value!r} is not a {verb} setting; expected {', '.join(words)}")
    given = str(Path(path).expanduser().resolve())
    probe = paths.keys(given)
    with transaction(connection):
        row = connection.execute(
            f"SELECT path, declarations FROM repo WHERE path IN ({paths.placeholders(probe)})",
            probe).fetchone()
        if row is None:
            raise RepoRefusal(
                f"{given} is not a registered repository; run `bin/sd-db repo add {given}`")
        held = _declarations(row["declarations"], row["path"])
        policy = dict(held.get("policy") or {})
        before = policy.pop(key, words[0])
        if value != words[0]:
            policy[key] = value
        held = {**held, "policy": policy} if policy else {k: v for k, v in held.items() if k != "policy"}
        connection.execute("UPDATE repo SET declarations = ?, updated_at = ? WHERE path = ?",
                           (_declarations_text(held) if held else None, now(), row["path"]))
    return row["path"], before


def issues_policy(connection: sqlite3.Connection, path: Path | str) -> str:
    """One repository's `policy.issues`: `off`, `read` or `ask` (pack sd:6849).

    `read`: agents read its third-party issues and map each to an sd item.
    `ask`: also, each write an agent drafts waits for the operator. No row
    and no key read `off`. A declaration that does not read raises.
    """
    held = declarations(connection, path) or {}
    value = (held.get("policy") or {}).get("issues", "off")
    return value if value in ISSUES else "off"


#: The words `repo issues-scope` takes: the `policy` declaration's `issues_scope` key (pack sd:7672).
#: `all` is no key. It says which intake issues a repository lists; `issues` alone governs writes.
ISSUES_SCOPES = ("all", "mine")
#: The `shadow.reason` words a `mine` intake keeps: on GitHub, `watched` is a mention or a review request.
SCOPE_MINE = ("assigned", "filed", "watched")


def set_issues_scope(connection: sqlite3.Connection, path: Path | str, value: str) -> tuple[str, str]:
    """Set one repository's `policy.issues_scope`, keeping its other policy keys. Returns `(path, before)`.

    `all` removes the key. Same refusals as `set_github_issues`.
    """
    return _set_policy_word(connection, path, "issues_scope", value, ISSUES_SCOPES, "issues-scope")


def issues_scope(connection: sqlite3.Connection, path: Path | str) -> str:
    """One repository's `policy.issues_scope`: `all` or `mine` (pack sd:7672).

    `mine`: its intake lists only issues assigned to the operator, mentioning
    them or asking their review. No row and no key read `all`. A declaration
    that does not read raises.
    """
    held = declarations(connection, path) or {}
    value = (held.get("policy") or {}).get("issues_scope", "all")
    return value if value in ISSUES_SCOPES else "all"


#: A Jira project key as `repo.jira_project` holds it: the spelling `channels`
#: takes for an issue source, up to Jira's 255 characters (sd:5103).
JIRA_PROJECT = re.compile(r"[A-Z][A-Z0-9_]{1,254}")
#: The word `repo jira-project` takes for no project; the column holds NULL.
JIRA_OFF = "off"


def set_jira_project(connection: sqlite3.Connection, path: Path | str, value: str) -> tuple[str, str]:
    """Set the Jira project one repository files issues in. Returns `(path, before)`.

    `value` is a key such as `EXAMPLE`, or `off` for none. `before` is the key
    replaced, or `off`. Same refusals as `set_runner_merge`: a value that is
    neither, exactly, and a path no row holds.
    """
    if value != JIRA_OFF and not JIRA_PROJECT.fullmatch(value):
        raise RepoRefusal(f"{value!r} is not a jira-project setting; expected a Jira project key "
                          f"such as EXAMPLE (A-Z, then A-Z, 0-9 or _), or {JIRA_OFF}")
    given = str(Path(path).expanduser().resolve())
    probe = paths.keys(given)
    with transaction(connection):
        row = connection.execute(
            f"SELECT path, jira_project FROM repo WHERE path IN ({paths.placeholders(probe)})",
            probe).fetchone()
        if row is None:
            raise RepoRefusal(
                f"{given} is not a registered repository; run `bin/sd-db repo add {given}`")
        connection.execute("UPDATE repo SET jira_project = ?, updated_at = ? WHERE path = ?",
                           (None if value == JIRA_OFF else value, now(), row["path"]))
    return row["path"], row["jira_project"] or JIRA_OFF


def jira_project(connection: sqlite3.Connection, path: Path | str) -> str | None:
    """The Jira project key one repository files issues in, or None for off (sd:5103).

    No row reads None, the column default. A store not yet at 36 raises
    `SchemaTooOld` rather than reading as off, so a caller decides what an
    unread setting means.
    """
    from .database import schema_version
    from .errors import SchemaTooOld
    from .schema import SCHEMA_VERSION

    probe = paths.keys(str(Path(path).expanduser().resolve()))
    try:
        row = connection.execute(
            f"SELECT jira_project FROM repo WHERE path IN ({paths.placeholders(probe)}) ORDER BY path",
            probe).fetchone()
    except sqlite3.OperationalError as error:
        if "no such column" in str(error):
            raise SchemaTooOld(schema_version(connection), SCHEMA_VERSION) from error
        raise
    return None if row is None else row["jira_project"]


#: The kinds `repo.declarations` holds: the first four each the object a `.github/sd-<kind>.json`
#: file held before migration 37 (sd:5109), and `experiment` a research repository's Notion
#: experiment page (sd:5302), and `policy` the repository's rules for agents (sd:6715,
#: `bin/sd_repo_policy.py`), and `generated` the paths the repository's own tools write, which a merged
#: worktree's cleanup deletes (sd:10752). The pack's readers validate each value.
DECLARATION_KINDS = ("review", "gate_reuse", "check_scope", "status", "experiment", "policy", "generated")
#: The longest declarations object, as compact JSON: the largest file moved was under 1 KiB.
DECLARATION_BYTES = 16384
#: The word `repo declare` takes to remove a kind; the kind leaves the object.
DECLARATION_OFF = "off"
#: A Notion id, dashed or not, spelled as `bin/sd_lib.NOTION_ID` spells it for the publish settings.
_NOTION_ID = r"[0-9a-fA-F]{8}-?(?:[0-9a-fA-F]{4}-?){3}[0-9a-fA-F]{12}"
#: The `experiment` declaration's required fields and their spelling (sd:5302): the Notion page,
#: by id or URL, and the data source that page is a row of, bare or as `collection://<id>`.
EXPERIMENT_FIELDS = {"page": rf"(?:https://[^\s/]+/(?:[^\s?#]*[/-])?)?{_NOTION_ID}/?(?:[?#]\S*)?",
                     "data_source": rf"(?:collection://)?{_NOTION_ID}"}
#: The optional fields, each text: `url` an https link to the page, `title` its name for display.
EXPERIMENT_OPTIONAL = {"url": r"https://[^\s/]+(?:/\S*)?", "title": r"(?s).*\S.*"}
#: The longest text any field may hold: a bound before any pattern runs.
EXPERIMENT_TEXT = 2000


def experiment_problems(value: Any) -> list[str]:
    """Why `value` is not an `experiment` declaration; empty when it is one (sd:5302).

    `set_declaration` refuses a value with any problem, so a row holds only
    this shape whoever writes it: `bin/sd-db repo declare` or the dashboard.
    """
    where = "the experiment declaration"
    if not isinstance(value, dict):
        return [f"{where} is a JSON object, not {type(value).__name__}"]
    extra = sorted(set(value) - set(EXPERIMENT_FIELDS) - set(EXPERIMENT_OPTIONAL))
    missing = sorted(set(EXPERIMENT_FIELDS) - set(value))
    problems = [f"{where}: unknown key(s) {', '.join(extra)}"] if extra else []
    problems += [f"{where}: missing {', '.join(missing)}"] if missing else []
    spelled = {"page": "a Notion page id or URL", "data_source": "a Notion data source id",
               "url": "an https URL", "title": "text"}
    for key, pattern in {**EXPERIMENT_FIELDS, **EXPERIMENT_OPTIONAL}.items():
        text = value.get(key)
        if key in value and not (isinstance(text, str) and len(text) <= EXPERIMENT_TEXT
                                 and re.fullmatch(pattern, text)):
            problems.append(f"{where}: {key} must be {spelled[key]} of at most {EXPERIMENT_TEXT} characters")
    return problems


#: The `generated` declaration's bounds (sd:10752): entries, and characters in one.
GENERATED_MAX, GENERATED_TEXT = 64, 200


def generated_problems(value: Any) -> list[str]:
    """Why `value` is not a `generated` declaration, `{"paths": [...]}`; empty when it is one (sd:10752).

    Each path is relative to the checkout's root, with `/` between names: a
    file (`reports/junit.xml`), a folder ending in `/` (`.venv-check/`), and
    `*`, `?` or `[...]` matching within one name (`*.csproj`, root files
    alone). Refused: an absolute path, `.` or `..`, a `.git` start, a
    backslash or a control character, which could name a file outside the
    root or one git owns.
    """
    where = "the generated declaration"
    if not isinstance(value, dict) or set(value) != {"paths"}:
        return [f'{where} is {{"paths": [PATH, ...]}}']
    named = value["paths"]
    if not isinstance(named, list) or not 0 < len(named) <= GENERATED_MAX:
        return [f"{where}: paths is a list of 1 to {GENERATED_MAX} paths"]
    problems = []
    for entry in named:
        names = entry.removesuffix("/").split("/") if isinstance(entry, str) else []
        if (not isinstance(entry, str) or not 0 < len(entry) <= GENERATED_TEXT or entry.startswith("/")
                or "\\" in entry or any(ord(char) < 32 or ord(char) == 127 for char in entry)
                or any(name in ("", ".", "..") for name in names) or names[0] == ".git"):
            problems.append(f"{where}: {entry!r} is no relative path of at most {GENERATED_TEXT} characters "
                            f"inside the checkout and outside .git")
    return problems


def generated_paths(connection: sqlite3.Connection, path: Path | str) -> tuple[str, ...]:
    """The paths `path`'s `generated` declaration names; none when it declares none or does not read."""
    try:
        held = declarations(connection, path) or {}
    except SdDbError:
        return ()
    value = held.get("generated")
    return () if value is None or generated_problems(value) else tuple(value["paths"])


def set_declaration(connection: sqlite3.Connection, path: Path | str, kind: str,
                    value: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
    """Set or remove (`None`) one registered repository's `kind` declaration. Returns `(path, before)`.

    The one writer of `repo.declarations` (sd:5109). It checks the shape only:
    a known kind, a JSON object, the whole column within `DECLARATION_BYTES`.
    What a kind's object may say is its reader's rule, which `bin/sd-db` runs
    before calling this; `experiment`, which the dashboard writes directly,
    is checked here too (`experiment_problems`). Same path refusal as `set_runner_merge`. Writing the
    value a kind already holds changes nothing but `updated_at`.
    """
    if kind not in DECLARATION_KINDS:
        raise RepoRefusal(f"{kind!r} is not a declaration kind; expected {', '.join(DECLARATION_KINDS)}")
    if value is not None and not isinstance(value, dict):
        raise RepoRefusal(f"a {kind} declaration is a JSON object, not {type(value).__name__}")
    if kind == "experiment" and value is not None and (problems := experiment_problems(value)):
        raise RepoRefusal("; ".join(problems))
    if kind == "generated" and value is not None and (problems := generated_problems(value)):
        raise RepoRefusal("; ".join(problems))
    given = str(Path(path).expanduser().resolve())
    probe = paths.keys(given)
    with transaction(connection):
        row = connection.execute(
            f"SELECT path, declarations FROM repo WHERE path IN ({paths.placeholders(probe)})",
            probe).fetchone()
        if row is None:
            raise RepoRefusal(f"{given} is not a registered repository; run `bin/sd-db repo add {given}`")
        held = _declarations(row["declarations"], row["path"])
        before = held.get(kind)
        if kind == "policy":
            value = _carried(before, value, row["path"])
        if value is None:
            held.pop(kind, None)
        else:
            held[kind] = value
        try:
            text = _declarations_text(held) if held else None
        except (TypeError, ValueError, RecursionError) as error:
            raise RepoRefusal(f"the {kind} declaration is not JSON: {error}") from None
        if text is not None and len(text.encode("utf-8")) > DECLARATION_BYTES:
            raise RepoRefusal(f"the declarations would take more than {DECLARATION_BYTES} bytes")
        # pack sd:7366: the columns follow the policy in the same statement, so the two agree until slice 2.
        connection.execute("UPDATE repo SET declarations = ?, runner_merge = ?, ci = ?, updated_at = ? WHERE path = ?",
                           (text, policy_merge(text), "local" if policy_ci(text) == "local" else "github", now(),
                            row["path"]))
    return row["path"], before


#: The policy keys a row setting writes (pack sd:7366): `repo runner-merge` and `repo ci` set them.
ROW_SETTINGS = ("merge", "ci")


def _carried(before: Any, value: dict[str, Any] | None, where: str) -> dict[str, Any] | None:
    """`value` with the row's `merge` and `ci` kept where it leaves them out (ruling Q2, note #41768).

    A preset that names neither must not stop the lane merging or move its
    checks; `off` keeps them too. A `ci` without a `provider` takes the old
    one's keys under its own. A kept `auto` on an org or external policy is
    refused, so the operator says `"merge": "ask"` rather than losing it unread.
    """
    old = before if isinstance(before, dict) else {}
    out = dict(value or {})
    for key in ROW_SETTINGS:
        if key not in old:
            continue
        if key not in out:
            out[key] = old[key]
        elif key == "ci" and isinstance(out["ci"], dict) and isinstance(old["ci"], dict) and "provider" not in out["ci"]:
            out["ci"] = {**old["ci"], **out["ci"]}
    if (out.get("merge") == "auto" and "merge" not in (value or {})
            and out.get("mode") is not None and out.get("mode") != "managed"):
        raise RepoRefusal(f"{where} merges auto, which a {out['mode']} policy refuses (note #34749); "
                          f'add "merge": "ask" to the policy, or run `sd-db repo runner-merge {where} manual` first')
    return out if value is not None else (out or None)


def _declarations(text: Any, where: str) -> dict[str, Any]:
    """The column's object; `{}` for NULL. Anything else raises: an unread declaration is unknown."""
    if text is None:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        value = None
    if not isinstance(value, dict):
        raise SdDbError(f"{where}: repo.declarations does not read as a JSON object; set it again")
    return value


def declarations(connection: sqlite3.Connection, path: Path | str) -> dict[str, Any] | None:
    """One repository's declarations by kind; `{}` when it declares none, None when no row holds `path`.

    A store before 37 has no column and so holds no declaration: `{}` for a
    row, a known answer and not a failed read, so code ahead of the hub's
    `sd-db migrate` reads what the store holds (sd:5109). A column that does
    not read raises `SdDbError`, never `{}`.
    """
    probe = paths.keys(str(Path(path).expanduser().resolve()))
    where = f"FROM repo WHERE path IN ({paths.placeholders(probe)}) ORDER BY path"
    try:
        row = connection.execute(f"SELECT path, declarations {where}", probe).fetchone()
    except sqlite3.OperationalError as error:
        if "no such column: declarations" not in str(error):
            raise
        return None if connection.execute(f"SELECT path {where}", probe).fetchone() is None else {}
    return None if row is None else _declarations(row["declarations"], row["path"])


#: What `repo.ci` accepts, in the order the CHECK constraint lists them:
#: `github` runs checks in GitHub Actions, `local` runs `sd-check` on this
#: machine and posts an `sd/local-gate` status instead (sd:1843).
CI_MODES = ("github", "local")


def set_ci(
    connection: sqlite3.Connection,
    path: Path | str,
    value: str,
) -> tuple[str, str]:
    """Set one registered repository's `ci`. Returns `(path, before)`.

    Same refusals as `set_runner_merge`: a value that is not one of
    `CI_MODES`, and a path no row holds.
    """
    if value not in CI_MODES:
        accepted = " or ".join(CI_MODES)
        raise RepoRefusal(f"{value!r} is not a ci setting; expected {accepted}")
    return _set_row_setting(connection, path, ci=value)


def repo_ci(connection: sqlite3.Connection, path: Path | str) -> str:
    """Where one repository's checks run: its `policy.ci.provider`, `github`, `local` or `jenkins` (pack sd:7366).

    An unregistered path reads `github`, the default: a repository the table
    does not hold has not been switched to local checks, so a caller that
    ships it keeps waiting for GitHub Actions. So does a provider that does not read.
    """
    row = row_for(connection, str(Path(path).expanduser().resolve()))
    return CI_MODES[0] if row is None else policy_ci(row["declarations"])


def set_channels(
    connection: sqlite3.Connection,
    path: Path | str,
    value: dict | None,
    *,
    before: str | None = None,
) -> tuple[str, str]:
    """Set one registered repository's publish channels. Returns `(path, before)`.

    The one writer of `repo.channels` (sd:3462, design sd:3471): the verb and
    the dashboard both call it. `value` passes `channels.validate` and is
    stored as its canonical text; `None` writes NULL, no setting. `before` is
    the value the caller read, canonical JSON or `none`; given, a row that
    reads otherwise raises `StaleItem`, compared inside the write transaction
    so two forms opened on one NULL row cannot both write. The answer is the
    value replaced, in the same spelling. Same path refusal as
    `set_runner_merge`. A direct UPDATE, not `upsert_repo`, whose `None`
    means "leave it" and so cannot write NULL.

    It does not refuse during an unresolved restore: the restore's recovery
    is to set each repository's channels again before resolving it.

    The outbound channels are one replacement, and each inbound key (`feed`,
    `jira`, `github`, `email`) is its own (sd:3980, sd:3461, sd:4191;
    `channels.kept`). A value naming no channel keeps the stored channels, so
    the dashboard form never deletes a source, and a value leaving an inbound key out keeps it, so
    `feed=URL` never deletes the channels or the issue sources. `None` clears
    all of it. A stored value this build refuses (a key a newer build wrote,
    sd:4191) is replaced only by a caller that names it as `before`; with no
    `before`, a set refuses rather than lose the keys it cannot read. A value
    naming `github` refuses on a managed repository, which uses no GitHub
    issues; one kept from before `repo managed PATH yes` rides along.
    """
    import json

    from . import channels
    from .workflow import StaleItem

    value = None if value is None else channels.validate(value)
    given = str(Path(path).expanduser().resolve())
    probe = paths.keys(given)
    with transaction(connection):
        row = connection.execute(
            f"SELECT path, channels, managed FROM repo WHERE path IN ({paths.placeholders(probe)})",
            probe).fetchone()
        if row is None:
            raise RepoRefusal(
                f"{given} is not a registered repository; run `bin/sd-db repo add {given}`")
        if value and "github" in value and row["managed"]:
            raise RepoRefusal(f"{row['path']} is managed, and managed repositories use no GitHub issues; "
                              f"name github only on a repository outside the managed list")
        was = channels.canonical(row["channels"])
        if before is not None and channels.canonical(before) != was:
            raise StaleItem(f"channels for {row['path']} is {was} now, not {before}; read the page again")
        if value is not None and before is None and row["channels"] is not None:
            try:
                channels.validate(json.loads(row["channels"]))
            except (ValueError, channels.ChannelRefusal) as error:
                raise channels.ChannelRefusal(
                    f"the stored channels of {row['path']} cannot be read by this build ({error}), so a set "
                    f"would lose the keys it does not name; clear it with `none`, then set it again") from error
        text = None if value is None else channels.dumps(channels.kept(value, row["channels"]))
        connection.execute("UPDATE repo SET channels = ?, updated_at = ? WHERE path = ?",
                           (text, now(), row["path"]))
    return row["path"], was


def repo_channels(connection: sqlite3.Connection, path: Path | str) -> dict | None:
    """One repository's publish channels, validated, or None for no setting.

    None means the row holds NULL or no row holds the path: either way the
    repository publishes as it did before the setting existed. Anything this
    cannot read raises, and never reads as None: a stored value
    `channels.validate` refuses raises `ChannelRefusal` naming the
    repository, and a store not yet at 29 raises `SchemaTooOld`. Falling back
    to the defaults would publish into folders the operator moved away from.

    An unresolved restore raises too, so every reader holds: the render, the
    drain through `repo channels`, and the routine-brief copies. The row may
    be older than the operator's last edit. `set_channels` still writes,
    which is the restore's recovery.
    """
    import json

    from . import channels
    from .database import schema_version
    from .errors import SchemaTooOld
    from .schema import SCHEMA_VERSION
    from .writes import unresolved_state

    if unresolved_state(connection, "restore"):
        raise channels.ChannelRefusal(
            "a restore is unresolved, so the repo row may be older than its channels setting; set it "
            "again with `bin/sd-db repo channels PATH obsidian=... notion=... drive=...`, then resolve "
            "the restore")
    given = str(Path(path).expanduser().resolve())
    probe = paths.keys(given)
    try:
        row = connection.execute(
            f"SELECT path, channels FROM repo WHERE path IN ({paths.placeholders(probe)})",
            probe).fetchone()
    except sqlite3.OperationalError as error:
        if "no such column" in str(error):
            raise SchemaTooOld(schema_version(connection), SCHEMA_VERSION) from error
        raise
    if row is None or row["channels"] is None:
        return None
    try:
        return channels.validate(json.loads(row["channels"]))
    except (ValueError, channels.ChannelRefusal) as error:
        raise channels.ChannelRefusal(
            f"the channels setting of {row['path']} cannot be used ({error}); set it again with "
            f"`bin/sd-db repo channels {row['path']} obsidian=... notion=... drive=...`") from error


#: The word that names the hub for `repo.lane_host`; the column holds NULL for it.
LANE_HUB = "hub"
#: A lane host is a `hostname -s`, lower-cased: the `local-cron-jobs` folder rule.
LANE_HOST_NAME = re.compile(r"[a-z0-9-]+")


def set_lane_host(
    connection: sqlite3.Connection,
    path: Path | str,
    value: str,
    *,
    before: str | None = None,
) -> tuple[str, str]:
    """Set the machine that runs one repository's lane. Returns `(path, before)`.

    The one writer of `repo.lane_host` (sd:3075): the verb and the dashboard
    both call it. `hub` writes NULL, the default every row starts at; any
    other value is a host name and must match `[a-z0-9-]+`. `before` is
    `hub` or the name; given, a row that reads otherwise raises `StaleItem`.
    Same path refusal as `set_runner_merge`. A direct UPDATE, not
    `upsert_repo`, whose `None` means "leave it" and so cannot write NULL.

    The lane belongs to the remote, so every clone row of the remote moves.
    The move holds the ship flock of the machine that hosts the lane now,
    from before its read to after its commit: every ship takes that flock, so
    holding it proves none runs, and a ship that waited reads the new host
    once it gets the flock and refuses. A busy flock refuses naming its
    holder. Part 1 moves a lane only on the hub, and only while the hub hosts
    it: another machine's flock is out of reach (sd:3003 part 2).
    """
    from contextlib import ExitStack

    from . import ship
    from .database import refuse_hub_only
    from .protection import github_slug
    from .workflow import StaleItem

    if value != LANE_HUB and not LANE_HOST_NAME.fullmatch(value):
        raise RepoRefusal(
            f"{value!r} is not a lane host; expected hub or a short host name "
            f"of lower-case letters, digits and dashes, as `hostname -s` gives it lower-cased")
    refuse_hub_only(connection, "repo lane-host")
    if connection.in_transaction:
        raise RepoRefusal("a lane move commits while it holds the ship lock; call it outside a transaction")
    database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    given = str(Path(path).expanduser().resolve())
    probe = paths.keys(given)

    def remote_of(remote):
        slug = github_slug(remote)
        return "/".join(slug) if slug else None

    # The stack releases the flock after the transaction commits, never before.
    with ExitStack() as held, transaction(connection):
        row = connection.execute(
            f"SELECT path, remote, lane_host FROM repo WHERE path IN ({paths.placeholders(probe)})",
            probe).fetchone()
        if row is None:
            raise RepoRefusal(
                f"{given} is not a registered repository; run `bin/sd-db repo add {given}`")
        was = row["lane_host"] or LANE_HUB
        if before is not None and was != before:
            raise StaleItem(f"lane_host for {row['path']} is {was} now, not {before}; read the page again")
        repository = remote_of(row["remote"])
        clones = [row] if repository is None else [
            other for other in connection.execute("SELECT path, remote, lane_host FROM repo ORDER BY path")
            if (remote_of(other["remote"]) or "").lower() == repository.lower()]
        away = sorted({clone["lane_host"] for clone in clones} - {None, ship.this_host()})
        if away:
            raise RepoRefusal(
                f"the lane for {row['path']} runs on {', '.join(away)}; run the move on {away[0]} "
                f"(sd:3003 part 2): this machine cannot see that machine's ship lock")
        if repository is not None:
            # No GitHub remote, no ship: nothing can hold a lock for it.
            held.enter_context(ship.repository_flock(
                database, repository, holder={"command": f"bin/sd-db repo lane-host {row['path']} {value}"}))
        paths_of = [clone["path"] for clone in clones]
        connection.execute(
            f"UPDATE repo SET lane_host = ?, updated_at = ? WHERE path IN ({paths.placeholders(paths_of)})",
            (None if value == LANE_HUB else value, now(), *paths_of))
    return row["path"], was
