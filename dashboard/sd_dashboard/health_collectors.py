"""The Disk and Branches readers behind Health (sd:2202, sd:2204).

Both read this machine at request time and write nothing, as the other
Health readers do. Each runs inside one overall budget: a command that would
end past it is stopped, and the reader raises `reads.OverBudget`, which Health shows as the
area stopped rather than waited on. A partial walk is not the answer.

**Branches** (sd:2204) is the design's "merged but not deleted": the local
branches of each registered repository that `origin/HEAD` already contains,
`git for-each-ref --merged=refs/remotes/origin/HEAD refs/heads/`. The default
branch itself is left out. A merged branch checked out in a worktree is its
own list, because `git branch -d` refuses it. A repository with no
`origin/HEAD`, or one git cannot read, is named rather than read as clean.

**Merged worktrees** (sd:5105) are part of the Branches reading: a registered repository's linked worktree whose
`HEAD` is the head a lane entry merged from that worktree (`head`, or an older entry's `expected_head`). The lane
squash-merges, so `origin/HEAD` does not contain that head, and a fresh worktree at `origin/HEAD` is no evidence
of a merge: only the lane entry ties the tree to a merge. Each tree carries why it stays, or nothing when it can
go: a process with its working folder inside (one `lsof -a -d cwd -Fn`), uncommitted or untracked files, ignored
files other than build output and tool caches, or a branch other than the one the lane merged. A probe that gave
no answer is a reason to stay, never a pass.

**Disk** (sd:2202) is the design's "where the space went", in three parts:

- volume use from `df -kPl`, local file systems only, so a network mount
  that does not answer cannot hold the page: the data volume (`/` where
  there is no `/System/Volumes/Data`) and every volume under `/Volumes`;
- the folder sizes under each `storage|<path>` line of the
  `dashboard.disk_storage` setting row (sd:5109; `disk.conf` answers only
  while the database has never held the row), one `du -k -d 1` per folder
  with what is left of the budget; a folder `du` could not finish is a row
  that says so, not a refusal of the area. A folder on a network mount
  (sd:5726: smbfs, nfs, afp, webdav, by one `mount`) gets no `du`, which
  ran past 120 s on an SMB share: one `df -kP <folder>` gives the share's
  use instead. A local folder's `df -kP` runs before its `du`, and its
  drive's use stands in when `du` does not finish (sd:5879). A folder whose
  `du` ran out at least half a read's time gets no `du` for
  `DU_RETRY_SECONDS`, which spent 6 s of each read, and its row says until
  when (sd:7898); one that could not start or was cut short is tried again
  on the next read. A mount table
  that did not answer measures no folder: none is known to be local;
- registered worktrees whose `HEAD` is in `origin/HEAD` and that still hold
  `target/`, `node_modules/` or `.venv/` (the 2026-09-25 rule: delete build
  output once the PR merges). Presence only: sizing build output needs a
  `du` of every tree, which the budget does not hold. Each such tree carries
  its `git status --porcelain`: only a clean one is offered for removal.

**Doctor** (sd:3393) is `machine-setup.sh doctor`, run as a child under the
collectors' `Budget`, which kills its whole process group at the deadline.
Its `ok`, `WARN`, `FAIL` and `--` lines are parsed as they are printed; an
exit or a last line the doctor does not end with is the area's error, so a
run that failed or was stopped never reads as clean.

**Revisions.** Each subject a cleanup acts on has a digest, `revision`, of
what the page showed: a checkout's registrations whose directory is gone,
a repository's merged branches and their tips, and the merged worktrees
with build output, each with its head and status. `health_cleanup` reads
the subject again at the write and refuses a digest that changed.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

from sd_db import paths, reads, repos
from sd_db.config import system_checkout

from . import documents

__all__ = ["BRANCH_SECONDS", "DISK_SECONDS", "DOCTOR_SECONDS", "DU_RETRY_SECONDS", "STORAGE_KEY", "branch_scan", "disk_scan", "doctor_scan",
           "merged_heads", "merged_tree_kept", "merged_worktrees", "parse_doctor", "parse_storage", "revision",
           "storage_list"]

#: Each reader's overall budget. Branches is two git calls per registered repository; Disk is df, a few git calls per repository and the du walks, which get what is left.
BRANCH_SECONDS = 8.0
DISK_SECONDS = 8.0
#: One command's own ceiling inside the budget.
COMMAND_SECONDS = 20.0
#: How long a storage folder whose du ran past the Disk budget is read by its drive's use alone (sd:7898). On a 2.1 TB
#: drive du never finished inside the budget, so each Disk read spent about 6 of its 8 seconds on a du it then killed.
DU_RETRY_SECONDS = 600.0
#: Each such folder's path, and the `time.monotonic()` reading when its du last ran past the budget.
_DU_OVERRAN: dict[str, float] = {}
#: The build output folders the 2026-09-25 rule names.
BUILD_DIRS = ("target", "node_modules", ".venv")
#: Ignored folders and files a merged worktree may hold and still be removed: build output and tool caches.
DISPOSABLE = (*BUILD_DIRS, "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store")
#: A full object name, SHA-1 or SHA-256.
OBJECT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class Walk:
    """Commands run inside one overall budget; past it, `reads.OverBudget` names what was stopped."""

    def __init__(self, within: float, noun: str):
        self.within = within
        self.stop = time.monotonic() + within
        self.refusal = f"{noun} ran past its budget of {within:g} seconds"
        #: The timeout the last `run` used up, or None when it answered or could not start.
        self.expired: float | None = None

    def left(self) -> float:
        return self.stop - time.monotonic()

    def run(self, argv: list[str], *, spare: bool = False) -> subprocess.CompletedProcess | None:
        """The command's answer, or None when it could not answer.

        `spare` is for a command whose overrun is its own row: its timeout
        returns None and leaves the walk to say what it did not finish.
        """
        timeout, self.expired = min(COMMAND_SECONDS, self.left()), None
        if timeout <= 0:
            raise reads.OverBudget(self.refusal)
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            self.expired = timeout
            if not spare and self.left() <= 0:
                raise reads.OverBudget(self.refusal) from None
            return None
        except (OSError, subprocess.SubprocessError):
            return None

    def git(self, path: Path, *args: str) -> subprocess.CompletedProcess | None:
        return self.run(["git", "-C", str(path), *args])


def revision(subject) -> str:
    """The digest of what a row showed, the one the page sends back with a cleanup."""
    return hashlib.sha256(json.dumps(subject, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def registrations_revision(name: str, trees: list[dict]) -> str:
    """The `gone:<name>` row's subject: the checkout's registrations that are not live (gone or unreadable)."""
    return revision([f"gone:{name}", sorted([tree.get("name") or "", tree.get("path") or "", tree.get("branch") or "",
                                             tree["state"]] for tree in trees if tree["state"] != "live")])


def build_revision(merged: list[dict]) -> str:
    """Merged worktrees with build output: each path, head, status digest and folders."""
    return revision(sorted([tree["path"], tree["head"], hashlib.sha256(tree["status"].encode("utf-8")).hexdigest(),
                            list(tree["dirs"])] for tree in merged))


def _registered(connection: sqlite3.Connection, repo_paths: list[str] | None) -> list[str]:
    """The paths the caller read, or the registry's. Health reads it on its own thread and passes the paths."""
    return list(repo_paths) if repo_paths is not None else [row["path"] for row in repos.registered(connection)]


def _default(walk: Walk, path: Path) -> tuple[str, str | None]:
    """('ok', branch name) for a repository with origin/HEAD; ('no_default', None) or ('unread', None) otherwise."""
    found = walk.git(path, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if found is None or found.returncode not in (0, 1):
        return "unread", None
    if found.returncode == 1 or not found.stdout.strip():
        return "no_default", None
    return "ok", found.stdout.strip().split("/", 1)[-1]


def merged_branches(walk: Walk, path: Path) -> tuple[str, list[tuple[str, str, str]], list[tuple[str, str]]]:
    """('ok', deletable, checked out) for a repository with origin/HEAD, or ('no_default' | 'unread', [], []).

    A deletable branch is (name, committer date, tip); a checked-out one is (name, worktree path).
    """
    state, default = _default(walk, path)
    if state != "ok":
        return state, [], []
    refs = walk.git(path, "for-each-ref", "--merged=refs/remotes/origin/HEAD",
                    "--format=%(refname)%09%(committerdate:short)%09%(worktreepath)%09%(objectname)", "refs/heads/")
    if refs is None or refs.returncode != 0:
        return "unread", [], []
    deletable, checked_out = [], []
    for line in refs.stdout.splitlines():
        ref, date, tree, tip = (line.split("\t") + ["", "", ""])[:4]
        # The exact branch name: %(refname:short) prints heads/foo or refs/heads/foo when a tag or branch shares it.
        name = ref.removeprefix("refs/heads/")
        if not name or name == default:
            continue
        (checked_out.append((name, tree)) if tree else deletable.append((name, date, tip)))
    return "ok", deletable, checked_out


def branches_revision(key: str, deletable: list) -> str:
    """The `br:<key>` row's subject: the repository's deletable merged branches by name and tip.

    The row id is part of it: two clones with the same branches at the same tips must not share a revision.
    """
    return revision([f"br:{key}", sorted([name, tip] for name, _, tip in deletable)])


def branch_scan(connection: sqlite3.Connection, *, within: float = BRANCH_SECONDS,
                repo_paths: list[str] | None = None, heads: dict | None = None) -> dict:
    """Each registered repository's local branches already in `origin/HEAD`, the repositories not read, and the
    worktrees the lane merged when `heads`, `merged_heads`' answer, is given: Health reads it on its own thread, since
    this walk runs on another and a sqlite connection belongs to the thread that opened it."""
    walk = Walk(within, "the merged-branch walk")
    # A repository registered for another machine has no folder here: nothing to read, not a git error.
    keys = [key for key in _registered(connection, repo_paths) if paths.disk(key).is_dir()]
    scan: dict = {"repos": 0, "merged": [], "no_default": [], "unread": [],
                  "worktrees": None if heads is None else merged_worktrees(walk, keys, heads)}
    for key in keys:
        path = paths.disk(key)
        state, deletable, checked_out = merged_branches(walk, path)
        if state != "ok":
            scan[state].append(key)
            continue
        scan["repos"] += 1
        if deletable or checked_out:
            scan["merged"].append({"repo": key, "path": str(path), "deletable": [(name, date) for name, date, _ in deletable],
                                   "checked_out": checked_out, "revision": branches_revision(key, deletable)})
    return scan


def merged_heads(connection: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """(resolved worktree, merged head) to the lane entry's item and branch, for each entry the lane merged.

    A body that is not a lane entry, or that names no full head, names no merge and is passed over. `held` says
    which live session on this machine holds the tree (pack sd:6683): the removal refuses such a tree, so the read
    keeps it too, on the caller's thread since the walk has no connection.
    """
    from sd_db import sessions
    from sd_db.ship import this_host

    host = this_host()
    live = [session for session in sessions.live(connection) if session.get("machine") == host]
    found = {}
    for _, _, body in reads.lane_entries(connection):
        try:
            entry = json.loads(body)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("status") != "merged":
            continue
        head, tree, branch = entry.get("head") or entry.get("expected_head"), entry.get("worktree"), entry.get("branch")
        if isinstance(head, str) and OBJECT.fullmatch(head) and isinstance(tree, str) and tree.startswith("/"):
            key = sessions._key(tree)
            held = [session for session in live if any(isinstance(held, dict) and held.get("path") == key
                                                       for held in session.get("worktrees") or [])]
            found[(os.path.realpath(tree), head)] = {"item": entry.get("item"), "held": sessions.describe(held),
                                                     "branch": branch if isinstance(branch, str) else ""}
    return found


def _cwds(walk: Walk) -> list[str] | None:
    """Every process's working folder `lsof` can see, or None when it gave no answer."""
    done = walk.run(["lsof", "-a", "-d", "cwd", "-Fn"])
    if done is None or done.returncode not in (0, 1) or not done.stdout.strip():
        return None
    return [line[1:] for line in done.stdout.splitlines() if line.startswith("n")]


def _disposable(name: str) -> bool:
    return any(part in DISPOSABLE or part.endswith(".egg-info") for part in name.rstrip("/").split("/"))


def busy(path: str, cwds: list[str] | None) -> str:
    """'' when no process `lsof` saw works in the tree at `path`; else why it stays. No answer keeps it."""
    if cwds is None:
        return "not known whether a process works in it: lsof gave no answer"
    real = os.path.realpath(path)
    found = next((cwd for cwd in cwds if cwd == real or cwd.startswith(real + os.sep)), None)
    return f"a process works in {found}" if found else ""


def merged_tree_kept(walk: Walk, tree: dict, cwds: list[str] | None) -> str:
    """'' when a merged worktree can go; otherwise why it stays. A probe that gave no answer keeps it."""
    if tree["branch"] not in ("", tree["lane_branch"]):
        return f"it is on {tree['branch']}, not the merged branch {tree['lane_branch'] or '(none)'}"
    if kept := busy(tree["path"], cwds):
        return kept
    status = walk.git(Path(tree["path"]), "status", "--porcelain", "-z", "--ignored")
    if status is None or status.returncode != 0:
        return "its status was not read"
    for entry in filter(None, status.stdout.split("\0")):
        # The first entry that is not ignored answers, before a rename's second path could be read as an entry.
        if not entry.startswith("!! "):
            return "it has uncommitted or untracked files"
        if not _disposable(entry[3:]):
            return f"it holds ignored files that are not build output: {entry[3:]}"
    return ""


def worktrees_revision(trees: list[dict]) -> str:
    """The `wt:merged` row's subject: each merged worktree's path, head, branches and whether it can go."""
    return revision(["wt:merged", sorted([tree["path"], tree["head"], tree["branch"], tree["lane_branch"],
                                          not tree["kept"]] for tree in trees)])


def merged_worktrees(walk: Walk, keys: list[str], heads: dict) -> dict:
    """The registered repositories' linked worktrees at a head the lane merged, each with why it stays ('' to go)."""
    trees, unread = [], []
    cwds = _cwds(walk) if heads else None
    for key in keys if heads else []:
        path = paths.disk(key)
        listed = _read_worktrees(walk, path)
        if listed is None:
            unread.append(key)
            continue
        for tree in listed:
            entry = heads.get((os.path.realpath(tree["path"]), tree["head"]))
            if entry is None or not os.path.isdir(tree["path"]):
                continue
            tree = {**tree, "repo": key, "repo_path": str(path), "item": entry["item"], "lane_branch": entry["branch"]}
            trees.append({**tree, "kept": entry.get("held") or merged_tree_kept(walk, tree, cwds)})
    return {"trees": trees, "unread": unread, "revision": worktrees_revision(trees)}


#: The storage folders' setting row (sd:5109). The file it replaced is read only while the database has never held it.
STORAGE_KEY = "dashboard.disk_storage"


def _storage_config() -> Path:
    """The old `<config>/project-dashboard/disk.conf`, at call time, so a test can point the config root elsewhere."""
    return documents._config_dir() / "disk.conf"


def storage_list(connection, config: Path | None = None) -> tuple[str | None, str, str]:
    """`setting_rows.text` for the storage folders: their text, its source and why it was not read.

    On the caller's thread: a connection belongs to the thread that opened it, so Health reads this before the walk.
    """
    from . import setting_rows

    return setting_rows.text(connection, STORAGE_KEY, config or _storage_config())


def parse_storage(text: str) -> tuple[list[Path], list[str]]:
    """The `storage|<path>` folders, and one problem per line that is not one.

    `~` and `$HOME` expand; any other line is named, never guessed at.
    """
    roots, refused = [], []
    for number, line, _ in documents.pipe_lines(text):
        kind, _, value = line.partition("|")
        value = value.strip().replace("$HOME", "~", 1) if value.strip().startswith("$HOME") else value.strip()
        if kind.strip() != "storage" or not value:
            refused.append(f"line {number}: {line}")
            continue
        roots.append(Path(value).expanduser())
    return roots, refused


def _volumes(walk: Walk) -> list[dict]:
    done = walk.run(["df", "-kPl"])
    if done is None or done.returncode not in (0, 1) or not done.stdout.strip():
        raise ValueError("df -kPl gave no volume table")
    rows = []
    for line in done.stdout.splitlines()[1:]:
        fields = line.split()
        # A pseudo file system can print "-" for its sizes or its capacity: it is not a volume.
        if len(fields) < 6 or not all(field.isdigit() for field in (*fields[1:4], fields[4].rstrip("%"))):
            continue
        mount = " ".join(fields[5:])
        rows.append({"filesystem": fields[0], "size_kb": int(fields[1]), "used_kb": int(fields[2]),
                     "avail_kb": int(fields[3]), "capacity": int(fields[4].rstrip("%")), "mount": mount})
    data = any(row["mount"] == "/System/Volumes/Data" for row in rows)
    kept = [row for row in rows if row["mount"] == ("/System/Volumes/Data" if data else "/")
            or row["mount"].startswith("/Volumes/")]
    if not kept:
        raise ValueError("df -kPl listed no data volume")
    return kept


def _worktrees(walk: Walk, path: Path) -> list[dict]:
    return _read_worktrees(walk, path) or []


def _read_worktrees(walk: Walk, path: Path) -> list[dict] | None:
    """The repository's linked worktrees (the first entry is the checkout itself) with their HEAD and branch.

    None when git gave no list: that is not a repository with no linked worktrees.
    """
    listed = walk.git(path, "worktree", "list", "--porcelain")
    if listed is None or listed.returncode != 0:
        return None
    trees, current = [], {}
    for line in listed.stdout.splitlines() + [""]:
        if not line:
            if current:
                trees.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return [{"path": tree["worktree"], "head": tree.get("HEAD", ""), "branch": tree.get("branch", "").removeprefix("refs/heads/")}
            for tree in trees[1:] if tree.get("worktree")]


def build_output(walk: Walk, keys: list[str]) -> dict:
    """Registered worktrees merged into origin/HEAD that keep build output, each with its `git status --porcelain`."""
    checked, merged, unread = 0, [], []
    for key in keys:
        path = paths.disk(key)
        state, _ = _default(walk, path)
        if state != "ok":
            continue
        for tree in _worktrees(walk, path):
            if not os.path.isdir(tree["path"]):
                continue
            checked += 1
            found = [name for name in BUILD_DIRS if os.path.isdir(os.path.join(tree["path"], name))]
            if not found or not tree["head"]:
                continue
            ancestor = walk.git(path, "merge-base", "--is-ancestor", tree["head"], "refs/remotes/origin/HEAD")
            if ancestor is None or ancestor.returncode not in (0, 1):
                unread.append(tree["path"])
            elif ancestor.returncode == 0:
                status = walk.git(Path(tree["path"]), "status", "--porcelain")
                if status is None or status.returncode != 0:
                    unread.append(tree["path"])
                else:
                    merged.append({**tree, "repo": key, "dirs": found, "status": status.stdout})
    return {"checked": checked, "merged": merged, "unread": unread, "revision": build_revision(merged)}


#: The file system types `mount` names for a network share (sd:5726): macOS's, then Linux's.
NETWORK_TYPES = frozenset({"smbfs", "nfs", "afpfs", "webdav", "cifs", "nfs4"})
#: One `mount` line: macOS's "<device> on <point> (<type>, ...)" or Linux's "<device> on <point> type <type> (...)".
MOUNT_LINE = re.compile(r"^.+? on (.+?) (?:type (\S+) \(|\((\w+)[,)])")


def _mounts(walk: Walk) -> dict[str, str] | None:
    """Each mount point's file system type, or None when `mount` gave no table: then no type is known."""
    done = walk.run(["mount"], spare=True)
    if done is None or done.returncode != 0:
        return None
    found = {}
    for line in done.stdout.splitlines():
        m = MOUNT_LINE.match(line)
        if m:
            # Linux writes a space or a tab in a mount point as an octal escape (\040); macOS writes it as it is.
            found[re.sub(r"\\([0-7]{3})", lambda o: chr(int(o.group(1), 8)), m.group(1))] = m.group(2) or m.group(3)
    return found or None


def _network(mounts: dict[str, str] | None, root: Path) -> tuple[str, str] | None:
    """(mount point, type) of the network mount that holds `root`, by its innermost mount point; None for a local one."""
    path = str(root)
    held = [point for point in (mounts or {}) if path == point or path.startswith(point.rstrip("/") + "/")]
    point = max(held, key=len, default=None)
    return (point, mounts[point]) if point and mounts[point] in NETWORK_TYPES else None


def _df(walk: Walk, root: Path, where: str) -> tuple[dict | None, str]:
    """The use of the file system that holds `root`, from one `df -kP`, or None and why it was not read."""
    done = walk.run(["df", "-kP", str(root)], spare=True)
    if done is None:
        return None, f"df did not finish inside the Disk budget on {where}"
    fields = (done.stdout.strip().splitlines() or [""])[-1].split()
    if done.returncode != 0 or len(fields) < 6 or not all(field.isdigit() for field in fields[1:4]):
        return None, (done.stderr.strip().splitlines() or [f"df exited {done.returncode} on {where}"])[-1]
    return {"mount": " ".join(fields[5:]), "size_kb": int(fields[1]), "used_kb": int(fields[2]),
            "avail_kb": int(fields[3])}, ""


def _share(walk: Walk, root: Path, mount: tuple[str, str]) -> dict:
    """A network folder's entry: the share's use from one `df -kP`, and no folders."""
    use, error = _df(walk, root, f"the {mount[1]} share")
    return {"root": str(root), "folders": [], "error": error, "network": use and {"type": mount[1], **use}}


def _storage(walk: Walk, roots: list[Path]) -> list[dict]:
    """One entry per configured folder: its subfolders by size, its share's use, or the reason they were not measured."""
    out = []
    mounts = _mounts(walk) if roots and walk.left() > 0 else None
    for root in roots:
        entry = {"root": str(root), "folders": [], "error": ""}
        mount = _network(mounts, root)
        # A stat (is_dir) or du on a share that does not answer is the hang sd:5726 ends, so neither runs on a folder
        # not known to be local; df is bounded, and names a missing folder.
        if walk.left() <= 0:
            entry["error"] = "not measured: the Disk budget was spent before this folder"
        elif mounts is None:
            entry["error"] = "not measured: mount gave no table, so a network folder cannot be told from a local one"
        elif mount:
            entry = _share(walk, root, mount)
        elif not root.is_dir():
            entry["error"] = "the folder does not exist or is not mounted"
        else:
            # sd:5879: du on a 2.1 TB local drive cannot finish inside the budget, so the drive's use, read first
            # while the budget holds, stands in when it does not.
            drive, _ = _df(walk, root, "the drive")
            overran = _DU_OVERRAN.get(str(root))
            again = overran is None or time.monotonic() - overran >= DU_RETRY_SECONDS
            done = walk.run(["du", "-k", "-d", "1", str(root)], spare=True) if again else None
            if done is None:
                if not again:
                    until = time.time() + overran + DU_RETRY_SECONDS - time.monotonic()
                    entry["du"] = (f"du paused after an earlier overrun until "
                                   f"{datetime.datetime.fromtimestamp(until).astimezone():%H:%M %Z}")
                elif walk.expired is None:
                    entry["du"] = "du could not run"
                else:
                    entry["du"] = "du did not finish inside the Disk budget"
                    # Only a du that had most of a read's time shows the folder is too big; one cut short by a
                    # spent walk may finish on the next read (sd:7898 review).
                    if walk.expired >= min(COMMAND_SECONDS, walk.within) / 2:
                        _DU_OVERRAN[str(root)] = time.monotonic()
                entry["error"] = entry["du"]
                if drive:
                    entry.update(error="", drive=drive)
            else:
                _DU_OVERRAN.pop(str(root), None)
                for line in done.stdout.splitlines():
                    size, _, name = line.partition("\t")
                    if size.isdigit() and name and Path(name) != root:
                        entry["folders"].append({"path": name, "kb": int(size)})
                entry["folders"].sort(key=lambda folder: -folder["kb"])
                if not entry["folders"] and done.returncode != 0:
                    entry["error"] = (done.stderr.strip().splitlines() or [f"du exited {done.returncode}"])[-1]
        out.append(entry)
    return out


def disk_scan(connection: sqlite3.Connection, *, within: float = DISK_SECONDS, listed: tuple | None = None,
              repo_paths: list[str] | None = None) -> dict:
    """Volume use, each configured storage folder's sizes and merged worktrees that keep build output.

    `listed` is `storage_list`'s answer, which Health reads on its own thread; None reads it here. `problem` says why
    the folders were not read: then they are unknown, not "none configured".
    """
    keys = _registered(connection, repo_paths)
    text, source, problem = listed if listed is not None else storage_list(connection)
    walk = Walk(within, "the Disk reading")
    volumes = _volumes(walk)
    build = build_output(walk, keys)
    roots, refused = parse_storage(text or "")
    return {"volumes": volumes, "build": build, "storage": _storage(walk, roots), "refused": refused,
            "config": source, "problem": problem}


#: The heartbeat each machine's `sd services list --record` writes (sd:9757): its Disk and Services readings, keyed by
#: host, so the hub's Health reads a satellite from the database and never reaches the machine.
MACHINE_PREFIX = "machine-health:"
#: How often the job that records a reading runs, unless it names its own: a reading older than this is stale.
MACHINE_INTERVAL = 3600


def this_host() -> str:
    """This machine's `hostname -s`, lower-cased: the name the cron jobs and the lane hosts use."""
    return socket.gethostname().split(".")[0].lower()


def record_reading(connection: sqlite3.Connection, *, interval: int = MACHINE_INTERVAL, disk=None, services=None) -> dict:
    """This machine's Disk and Services readings, stored as its `machine-health:<host>` heartbeat.

    Disk is `disk_scan` with no registered worktree: the build-output rule reads the hub's registry, whose paths are
    not this machine's. `disk` and `services` are seams a test fills; unset, they read this machine.
    """
    from sd_db import services as service_store
    from sd_db.writes import record_state

    disk = disk if disk is not None else disk_scan(connection, repo_paths=[])
    found = services if services is not None else service_store.inventory(connection)
    body = {"interval_seconds": int(interval), "disk": disk, "services": found["services"]}
    record_state(connection, "heartbeat", key=MACHINE_PREFIX + this_host(), body=body)
    return body


#: The doctor's deadline: it runs `brew doctor`, which takes tens of seconds on a quiet machine.
DOCTOR_SECONDS = 180.0
MACHINE_SETUP = system_checkout() / "local-machine-setup" / "machine-setup.sh"
#: One finding: two or four spaces, a marker, then the text. A line indented past the marker column continues the one
#: before; a heading such as "== doctor" or "  folders without a filled .env" is context, kept with the passing lines.
DOCTOR_LINE = re.compile(r"^ {2,4}(ok|WARN|FAIL|SKIP|--) +(\S.*)$")
DOCTOR_MORE = re.compile(r"^ {6,}(\S.*)$")
DOCTOR_FAILS = re.compile(r"^  (\d+) hard failure\(s\)$")


def parse_doctor(text: str, code: int) -> dict:
    """The doctor's findings, in order, and its count of hard failures.

    The doctor ends with "no hard failures" and exit 0, or "N hard failure(s)" and exit 1. Any other end is a run
    that did not finish, a `die` or a doctor this parser does not know: a `ValueError`, never an empty clean list.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    counted = DOCTOR_FAILS.match(last)
    if not ((code == 0 and last == "  no hard failures") or (code == 1 and counted)):
        raise ValueError(f"machine-setup.sh doctor exited {code} without its closing line"
                         + (f": {last.strip()}" if last.strip() else ""))
    checks, other = [], []
    for line in lines[:-1]:
        found, more = DOCTOR_LINE.match(line), DOCTOR_MORE.match(line)
        if found:
            checks.append({"mark": found[1], "text": found[2], "more": []})
        elif more and checks:
            checks[-1]["more"].append(more[1])
        elif not line.startswith("== "):
            other.append(line.strip())
    return {"checks": checks, "other": other, "fails": int(counted[1]) if counted else 0}


def doctor_scan(*, script: Path | None = None, within: float = DOCTOR_SECONDS) -> dict:
    """`machine-setup.sh doctor` in a child process group, killed at `within` seconds, and its parsed lines."""
    from . import fleet

    collectors = fleet._collectors()
    budget = collectors.Budget(within)
    try:
        done = budget.run([str(script or MACHINE_SETUP), "doctor"], label="doctor")
    except collectors.OverBudget as error:
        raise reads.OverBudget(f"machine-setup.sh doctor was stopped: {error}") from None
    except OSError as error:
        raise ValueError(f"machine-setup.sh doctor did not start: {error}") from None
    try:
        return parse_doctor(done.stdout, done.returncode)
    except ValueError as error:
        why = (done.stderr or "").strip().splitlines()
        raise ValueError(f"{error}{f' ({why[-1]})' if why else ''}") from None
