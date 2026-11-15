"""Health's cleanup writes (sd:3393, sd:5105): prune registrations, delete merged branches, remove build output and
remove merged worktrees.

The page posts `/api/health/cleanup` with the row it showed and that row's `revision`. The row names the action:

  gone:<checkout>   `git -C <checkout> worktree prune -v`, for a checkout under the fleet root whose registrations
                    point at a directory that is gone. Its subject is every registration that is not live.
  br:<registered>   `git -C <repo> branch -d <name>`, once per merged branch, for a registered repository.
                    Its subject is each deletable branch by name and tip.
  build:merged      removes `target/`, `node_modules/` and `.venv/` from registered worktrees that are merged into
                    origin/HEAD and clean. Its subject is each such worktree's path, head, status and folders.
  wt:merged         removes the registered worktrees at a head the lane merged (sd:5105), with their branch here
                    and on origin. Its subject is each such worktree's path, head, branches and whether it can go.

A merged worktree goes in four steps, each checked again just before it, in an order that leaves the tree on disk
until the last one, so a stop between any two leaves it in the next read, which finishes the rest:
`push --force-with-lease=refs/heads/<b>:<head> origin --delete <b>` when origin's branch is at the merged head,
`checkout --detach` in the tree, `update-ref -d refs/heads/<b> <head>`, and `git worktree remove <path>` without
`--force`, which refuses uncommitted or untracked files on its own. A branch that moved here or on origin, a process
working in the tree, a live session on this machine that holds it (`sd_db.sessions`, pack sd:6024), or files that are
not build output keep the tree, and the answer says why. A held tree, or one a process works in (pack sd:8818), keeps
its build output too.

Each write reads its subject again first and refuses a digest that differs from the page's (409): evidence from
another row, or a subject that changed since the read, writes nothing. Then each step keeps git's own guard:
`worktree prune` leaves a locked or present directory's registration, and `branch -d` refuses a branch that is not
merged into its upstream or HEAD. Build output is removed only after the worktree is read again, just before its
folders go: the same head, still in origin/HEAD, and `git status --porcelain` empty.

Outcomes follow `queue_screen.move`: a refusal that wrote nothing is a `ValueError` (400) or a `StaleItem` (409), and
so is a write where every step was refused. A step whose result is not known (a git call that did not answer, a folder
removal that stopped part way, a check before or after a step that could not be read) answers 502 with
`outcome: unknown` and what did land; the page reads Health again. When some steps landed, a step git or a check
refused is a definite answer: the write answers 200 and names it, with its reason.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from sd_db import reads

from . import health_collectors
from .health_collectors import Walk

__all__ = ["request"]

#: A write's budget for the git reads and calls around it; removing a folder is not bounded by it.
WRITE_SECONDS = 30.0


def _unknown(error: str, **landed) -> tuple[int, dict]:
    return 502, {"error": error, "outcome": "unknown", **landed}


class _Unread(Exception):
    """A check before a step that could not be read: the step's outcome is not known, never a refusal."""


def _done(row: str, done: list, refused: list, verb: str) -> dict:
    """200 when a step landed; a write where every step was refused wrote nothing, so it is a 400 naming why."""
    if not done and refused:
        raise ValueError(f"Nothing was {verb}: " + "; ".join(f"{x['name']} ({x['why']})" for x in refused))
    return {"ok": True, "row": row, "done": done, "refused": refused}


def _stale(what: str):
    from sd_db import workflow

    return workflow.StaleItem(f"{what} changed since the page read it. Nothing was changed. Read Health again, then retry.")


def _checkout(name: str) -> Path:
    """The checkout `gone:<name>` names: one the fleet lists under its root, never an arbitrary path."""
    from . import fleet

    root = fleet._collectors().REPO_ROOT
    for group, repo in fleet.checkouts(root):
        if (repo.name if group == "." else f"{group}/{repo.name}") == name:
            return repo
    raise ValueError(f"{name} is not a checkout under {root}. Nothing was pruned.")


def prune(name: str, expected: str):
    from . import fleet

    try:
        path = _checkout(name)
        trees = fleet.read_worktrees(".", path)
    except OSError as error:
        raise ValueError(f"The registrations of {name} were not read: {error}. Nothing was pruned.") from None
    if health_collectors.registrations_revision(name, trees) != expected:
        raise _stale(f"The registrations of {name}")
    gone = [tree["name"] for tree in trees if tree["state"] == "abandoned"]
    unread = [tree["name"] for tree in trees if tree["state"] == "unknown"]
    if unread:
        # git judges an unreadable registration by its own rules and may prune it; the page never showed it as gone.
        raise ValueError(f"{len(unread)} registrations of {name} could not be read, and git worktree prune would judge "
                         f"them itself: {', '.join(unread)}. Nothing was pruned.")
    if not gone:
        raise _stale(f"The registrations of {name}")
    try:
        answer = Walk(WRITE_SECONDS, "git worktree prune").git(path, "worktree", "prune", "-v")
    except reads.OverBudget:
        answer = None
    try:
        after = {tree["name"] for tree in fleet.read_worktrees(".", path)}
    except OSError as error:
        return _unknown(f"git worktree prune ran, but the registrations were not read after it: {error}")
    pruned, kept = [n for n in gone if n not in after], [n for n in gone if n in after]
    if answer is None and kept:
        # git did not answer, so why it kept these, and whether it finished, is not known.
        return _unknown("git worktree prune gave no answer in time", done=pruned, refused=[{"name": n, "why": "not known"}
                                                                                          for n in kept])
    why = (answer.stderr.strip() if answer is not None else "") or "git kept it"
    return _done(f"gone:{name}", pruned, [{"name": n, "why": why} for n in kept], "pruned")


def delete_branches(connection, key: str, expected: str):
    from sd_db import paths, repos

    if key not in {row["path"] for row in repos.registered(connection)}:
        raise ValueError(f"{key} is not a registered repository. Nothing was deleted.")
    path = paths.disk(key)
    walk = Walk(WRITE_SECONDS, "the merged-branch delete")
    state, deletable, _ = health_collectors.merged_branches(walk, path)
    if state != "ok":
        raise ValueError(f"The merged branches of {key} were not read ({state}). Nothing was deleted.")
    if health_collectors.branches_revision(key, deletable) != expected:
        raise _stale(f"The merged branches of {key}")
    done, refused = [], []
    for name, _, _ in deletable:
        if name.startswith("-"):
            refused.append({"name": name, "why": "a branch name that starts with - is not passed to git"})
            continue
        step = Walk(WRITE_SECONDS, f"git branch -d {name}")
        try:
            answer = step.git(path, "branch", "-d", name)
            # The ref, not the exit, says what happened: a timed-out or failed call may still have deleted it.
            there = step.git(path, "rev-parse", "-q", "--verify", f"refs/heads/{name}")
        except reads.OverBudget:
            there = None
        if there is None or there.returncode not in (0, 1):
            return _unknown(f"git branch -d {name} ran, but the branch was not read after it", done=done, refused=refused)
        if there.returncode == 1:
            done.append(name)
        else:
            said = answer.stderr.strip().splitlines() if answer is not None else ["git gave no answer in time"]
            # git's reason is its `error:` line; the hints after it say how to force, which the page never does.
            refused.append({"name": name, "why": next((line for line in said if line.startswith("error:")), said[-1] if said
                                                      else "git refused it")})
    return _done(f"br:{key}", done, refused, "deleted")


def _held(connection, path: str) -> str:
    """'' when no live session on this machine holds the worktree; else why it stays. An unread registry is `_Unread`."""
    from sd_db import sessions
    from sd_db.ship import this_host

    try:
        return sessions.describe(sessions.holders(connection, path, machine=this_host()))
    except Exception as error:  # noqa: BLE001 -- not knowing who holds it is not a reason to remove it
        raise _Unread(f"the session registry was not read: {error}") from None


def _still_removable(walk: Walk, tree: dict) -> str:
    """'' when the worktree is still at its head, in origin/HEAD and clean; otherwise why it is kept.

    A check that could not be read raises `_Unread`: not knowing is not a reason to keep, nor to remove.
    """
    where = Path(tree["path"])
    head = walk.git(where, "rev-parse", "HEAD")
    if head is None or head.returncode != 0:
        raise _Unread("its head was not read")
    if head.stdout.strip() != tree["head"]:
        return "its head moved since the read"
    ancestor = walk.git(Path(tree["repo_disk"]), "merge-base", "--is-ancestor", tree["head"], "refs/remotes/origin/HEAD")
    if ancestor is None or ancestor.returncode not in (0, 1):
        raise _Unread("whether it is in origin/HEAD was not read")
    if ancestor.returncode == 1:
        return "it is no longer in origin/HEAD"
    status = walk.git(where, "status", "--porcelain")
    if status is None or status.returncode != 0:
        raise _Unread("its status was not read")
    return "it has uncommitted changes" if status.stdout else ""


def remove_build_output(connection, expected: str):
    from sd_db import paths, repos

    keys = [row["path"] for row in repos.registered(connection)]
    try:
        merged = health_collectors.build_output(Walk(WRITE_SECONDS, "the build output read"), keys)["merged"]
    except reads.OverBudget as error:
        raise ValueError(f"The worktrees were not read: {error}. Nothing was removed.") from None
    if health_collectors.build_revision(merged) != expected:
        raise _stale("The merged worktrees with build output")
    done, refused = [], []
    for tree in merged:
        try:
            # Read again just before this worktree's folders go: a step that moved since the read above keeps them.
            # A process working in the tree may be using them, as a merged worktree's removal reads (pack sd:8818).
            walk = Walk(WRITE_SECONDS, "the worktree check")
            kept = ("it has uncommitted changes" if tree["status"] else _held(connection, tree["path"])
                    or health_collectors.busy(tree["path"], health_collectors._cwds(walk)) or _still_removable(
                        walk, {**tree, "repo_disk": str(paths.disk(tree["repo"]))}))
        except (reads.OverBudget, _Unread) as error:
            return _unknown(f"{tree['path']} was not checked again before its folders went ({error}); it and the "
                            "worktrees after it were left", done=done, refused=refused)
        if kept:
            refused.append({"name": tree["path"], "why": kept})
            continue
        for name in tree["dirs"]:
            folder = Path(tree["path"]) / name
            if name not in health_collectors.BUILD_DIRS or folder.is_symlink() or not folder.is_dir():
                refused.append({"name": str(folder), "why": "not a build output folder"})
                continue
            try:
                shutil.rmtree(folder)
            except OSError as error:
                return _unknown(f"{folder} was removed only in part: {error}", done=done, refused=refused)
            done.append(str(folder))
    return _done("build:merged", done, refused, "removed")


def _ref(walk: Walk, path: Path, *args: str) -> str:
    """The object a `rev-parse -q --verify` or `ls-remote` names, '' for none; no answer is `_Unread`."""
    found = walk.git(path, *args)
    if found is None or found.returncode not in ((0,) if args[0] == "ls-remote" else (0, 1)):
        raise _Unread(f"git {' '.join(args)} gave no answer")
    return found.stdout.split()[0] if found.returncode == 0 and found.stdout.split() else ""


def _remove_tree(walk: Walk, tree: dict, done: list) -> str:
    """Remove one merged worktree, its branch and origin's: '' when it went, or why it stays.

    Each step that lands goes on `done` as it lands (`origin/<branch>`, then the tree), so a refusal or a stop after
    the remote delete still names it, and the write is never a 400 that says nothing was written.
    """
    where, repo, branch, head = Path(tree["path"]), Path(tree["repo_path"]), tree["lane_branch"], tree["head"]
    if branch.startswith("-"):
        return "a branch name that starts with - is not passed to git"
    if _ref(walk, where, "rev-parse", "-q", "--verify", "HEAD") != head:
        return "its head moved since the read"
    current = walk.git(where, "symbolic-ref", "-q", "--short", "HEAD")
    if current is None or current.returncode not in (0, 1):
        raise _Unread("its branch was not read")
    kept = health_collectors.merged_tree_kept(walk, {**tree, "branch": current.stdout.strip()},
                                              health_collectors._cwds(walk))
    if kept:
        return kept
    local = remote = ""
    if branch:
        local = _ref(walk, repo, "rev-parse", "-q", "--verify", f"refs/heads/{branch}")
        if local and local != head:
            return f"{branch} moved to {local[:12]} since the merge"
        holder = walk.git(repo, "for-each-ref", "--format=%(worktreepath)", f"refs/heads/{branch}")
        if holder is None or holder.returncode != 0:
            raise _Unread(f"where {branch} is checked out was not read")
        held = holder.stdout.strip()
        if held and Path(held).resolve() != where.resolve():
            return f"{branch} is checked out in {held}"
        remote = _ref(walk, repo, "ls-remote", "origin", f"refs/heads/{branch}")
        if remote and remote != head:
            return f"origin/{branch} is at {remote[:12]}, not the merged head"
    if remote:
        pushed = walk.git(repo, "push", "-q", f"--force-with-lease=refs/heads/{branch}:{head}", "origin", "--delete", branch)
        # origin's ref, not the exit, says what happened: a push that timed out may have landed.
        if _ref(walk, repo, "ls-remote", "origin", f"refs/heads/{branch}"):
            said = (pushed.stderr.strip().splitlines() or ["git refused it"])[-1] if pushed is not None else "no answer"
            return f"origin/{branch} was not deleted: {said}"
        done.append(f"origin/{branch}")
    if branch and current.stdout.strip() == branch:
        walk.git(where, "checkout", "-q", "--detach")
        detached = walk.git(where, "symbolic-ref", "-q", "HEAD")
        if detached is None or detached.returncode != 1:
            raise _Unread("its checkout of the merged head was not confirmed detached")
    if local:
        deleted = walk.git(repo, "update-ref", "-d", f"refs/heads/{branch}", head)
        if _ref(walk, repo, "rev-parse", "-q", "--verify", f"refs/heads/{branch}"):
            if deleted is None:
                raise _Unread("git update-ref -d gave no answer")
            return f"{branch} moved since the read, so update-ref kept it"
    removed = walk.git(repo, "worktree", "remove", str(where))
    if where.exists():
        said = removed.stderr.strip().splitlines() if removed is not None else []
        if removed is None:
            raise _Unread("git worktree remove gave no answer")
        return next((line for line in said if line.startswith("fatal:")), said[-1] if said else "git kept it")
    done.append(str(where))
    return ""


def remove_merged_worktrees(connection, expected: str):
    from sd_db import repos

    keys = [row["path"] for row in repos.registered(connection)]
    try:
        found = health_collectors.merged_worktrees(Walk(WRITE_SECONDS, "the merged worktree read"), keys,
                                                   health_collectors.merged_heads(connection))
    except reads.OverBudget as error:
        raise ValueError(f"The merged worktrees were not read: {error}. Nothing was removed.") from None
    if found["revision"] != expected:
        raise _stale("The merged worktrees")
    done, refused = [], []
    for tree in found["trees"]:
        if tree["kept"]:
            refused.append({"name": tree["path"], "why": tree["kept"]})
            continue
        try:
            kept = _held(connection, tree["path"]) or _remove_tree(
                Walk(WRITE_SECONDS, f"the removal of {tree['path']}"), tree, done)
        except (reads.OverBudget, _Unread) as error:
            return _unknown(f"{tree['path']}: {error}; it and the worktrees after it were left where that step stopped. "
                            "Read Health again: a tree still on disk is offered again", done=done, refused=refused)
        if kept:
            refused.append({"name": tree["path"], "why": kept})
    return _done("wt:merged", done, refused, "removed")


def request(payload: dict):
    """Check the page's request, then return the write: `action_route`'s contract."""
    values = dict(payload)
    if (set(values) != {"row", "revision"} or not isinstance(values["row"], str) or len(values["row"]) > 1000
            or not isinstance(values["revision"], str) or not re.fullmatch(r"[0-9a-f]{64}", values["revision"])):
        raise ValueError("Name the Health row and the revision the page showed for it.")
    row, expected = values["row"], values["revision"]
    if row.startswith("gone:") and row[5:]:
        return lambda connection: prune(row[5:], expected)
    if row.startswith("br:") and row[3:]:
        return lambda connection: delete_branches(connection, row[3:], expected)
    if row == "build:merged":
        return lambda connection: remove_build_output(connection, expected)
    if row == "wt:merged":
        return lambda connection: remove_merged_worktrees(connection, expected)
    raise ValueError("Only a registrations, merged branches, build output or merged worktrees row has a cleanup.")
