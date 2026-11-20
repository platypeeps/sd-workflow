"""Bundle drop (pack sd:9709): take one item out of an ended build bundle, its lead included.

`sd assignments build ITEM --drop` cherry-picks the kept items' commits onto a fresh branch from the base, then
queues the kept items under a kept lead and the dropped item alone. It replaces the revert recipe: a subset Retry
must name the lead (ruling #64210), and a reverted commit keeps its `Closes: sd:N`, so the merge closed the item
it dropped (sd:9651).

A commit is the dropped item's when its message cites it (`sd_ship_body.cited_ids`), the kept items' when it
cites only others. One that cites nothing goes with the side whose cited commits touched its paths; one that
touches both sides' paths, or neither, refuses the drop: no message says whose it is.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import sd_ship_body


class DropRefused(Exception):
    """Why the drop wrote nothing."""


def _git(*argv: str, cwd: str | Path, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True, text=True, check=False,
                          stdin=subprocess.DEVNULL)
    if check and done.returncode != 0:
        raise DropRefused(f"git {' '.join(argv)} failed in {cwd}: {(done.stderr or done.stdout).strip()}")
    return done


def _out(*argv: str, cwd: str | Path) -> str:
    return _git(*argv, cwd=cwd).stdout.strip()


def drop_base(repo: str | Path) -> str:
    """The branch a build starts from, as `build_run.Supervisor.base` reads it."""
    done = _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=repo, check=False)
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else "origin/main"


def _paths(work: str | Path, sha: str) -> set[str]:
    return set(filter(None, _out("diff-tree", "--no-commit-id", "-r", "--name-only", "-z", sha, cwd=work).split("\0")))


def drop_sides(work: str | Path, start: str, tip: str, item: int) -> tuple[list[str], list[str]]:
    """The non-merge commits of `start..tip`, oldest first, split into (kept, dropped) for dropping `item`."""
    shas = _out("rev-list", "--reverse", "--no-merges", f"{start}..{tip}", cwd=work).split()
    side: dict[str, str] = {}
    touched: dict[str, set[str]] = {"kept": set(), "dropped": set()}
    loose = []
    for sha in shas:
        cited = set(sd_ship_body.cited_ids("", [(sha, _out("log", "-1", "--format=%B", sha, cwd=work))]))
        if item in cited and cited - {item}:
            raise DropRefused(f"commit {sha[:12]} cites sd:{item} and "
                              f"{', '.join(f'sd:{n}' for n in sorted(cited - {item}))}: split it by hand")
        if not cited:
            loose.append(sha)
            continue
        side[sha] = "dropped" if item in cited else "kept"
        touched[side[sha]] |= _paths(work, sha)
    for sha in loose:
        paths = _paths(work, sha)
        hits = [name for name in ("kept", "dropped") if paths & touched[name]]
        if len(hits) != 1:
            raise DropRefused(f"commit {sha[:12]} cites no item and touches "
                              f"{'paths of both sides' if hits else 'no path a citing commit touched'}: "
                              f"no message says whose it is")
        side[sha] = hits[0]
    return [sha for sha in shas if side[sha] == "kept"], [sha for sha in shas if side[sha] == "dropped"]


def _clean(work: str | Path, branch: str) -> None:
    if _out("branch", "--show-current", cwd=work) != branch:
        raise DropRefused(f"worktree {work} is not on {branch}")
    if _out("status", "--porcelain", "--untracked-files=all", cwd=work):
        raise DropRefused(f"worktree {work} is not clean")
    for ref in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD"):
        if _git("rev-parse", "-q", "--verify", ref, cwd=work, check=False).returncode == 0:
            raise DropRefused(f"worktree {work} has an unfinished {ref.split('_')[0].lower()}")


def _exists(repo: str | Path, ref: str) -> bool:
    return _git("rev-parse", "-q", "--verify", ref, cwd=repo, check=False).returncode == 0


def split_branch(plan: dict) -> dict:
    """The kept commits on `plan["new_branch"]`, fresh from the base, in `plan["new_work"]`; the old tip kept on
    `saved` when the branch is rewritten in place. Undone on any failure, and `undo` undoes it after."""
    repo, work, branch, item = Path(plan["repo"]), Path(plan["work"]), plan["branch"], plan["item"]
    if not work.is_dir():
        raise DropRefused(f"worktree {work} is not on this machine: drop it on the host that built it")
    _clean(work, branch)
    _git("fetch", "-q", "origin", cwd=repo)
    start = drop_base(repo)
    tip = _out("rev-parse", "HEAD", cwd=work)
    fork = _out("merge-base", start, tip, cwd=work)
    kept, dropped = drop_sides(work, fork, tip, item)
    in_place = plan["new_work"] == plan["work"]
    saved = f"{branch}-drop-sd-{item}"
    new_work, new_branch = Path(plan["new_work"]), plan["new_branch"]
    if in_place:
        # The lane pushes without force: a rewritten branch already on origin would not push.
        if _git("ls-remote", "--exit-code", "origin", f"refs/heads/{branch}", cwd=repo, check=False).returncode == 0:
            raise DropRefused(f"{branch} is on origin; drop it only before the lane pushed it")
        if _exists(repo, f"refs/heads/{saved}"):
            raise DropRefused(f"branch {saved} exists: an earlier drop kept it")
        _git("branch", saved, tip, cwd=repo)
        _git("checkout", "-q", "-B", branch, start, cwd=work)
    else:
        if new_work.exists() or _exists(repo, f"refs/heads/{new_branch}"):
            raise DropRefused(f"{new_work} or branch {new_branch} exists already")
        _git("worktree", "add", "-q", "-b", new_branch, str(new_work), start, cwd=repo)
    done = {"plan": plan, "tip": tip, "saved": saved if in_place else None, "base": start, "kept": kept, "dropped": dropped}
    try:
        for sha in kept:
            _git("cherry-pick", "--allow-empty", sha, cwd=new_work)
        check_branch(new_work, start, kept, item)
    except DropRefused:
        undo_split(done)
        raise
    done["head"] = _out("rev-parse", "HEAD", cwd=new_work)
    return done


def check_branch(work: str | Path, start: str, kept: list[str], item: int) -> None:
    """The new branch holds the kept commits alone: one commit each, no path past theirs, no citation of `item`."""
    shas = _out("rev-list", "--reverse", f"{start}..HEAD", cwd=work).split()
    if len(shas) != len(kept):
        raise DropRefused(f"the new branch holds {len(shas)} commits, not the {len(kept)} kept")
    commits = [(sha, _out("log", "-1", "--format=%B", sha, cwd=work)) for sha in shas]
    if item in sd_ship_body.cited_ids("", commits):
        raise DropRefused(f"the new branch still cites sd:{item}")
    allowed = set().union(*(_paths(work, sha) for sha in kept)) if kept else set()
    changed = set(filter(None, _out("diff", "--name-only", "-z", start, "HEAD", cwd=work).split("\0")))
    if changed - allowed:
        raise DropRefused(f"the new branch changes {', '.join(sorted(changed - allowed))}, which no kept commit does")


def undo_split(done: dict) -> None:
    """Put the branches and worktrees back as `split` found them."""
    plan = done["plan"]
    repo, work = Path(plan["repo"]), Path(plan["new_work"])
    _git("cherry-pick", "--abort", cwd=work, check=False)
    if done["saved"]:
        _git("checkout", "-q", "-f", "-B", plan["branch"], done["tip"], cwd=work, check=False)
        _git("branch", "-D", done["saved"], cwd=repo, check=False)
    else:
        _git("worktree", "remove", "--force", str(work), cwd=repo, check=False)
        _git("branch", "-D", plan["new_branch"], cwd=repo, check=False)


def kept_note(done: dict) -> str:
    """The finding the kept items' next builder reads."""
    plan = done["plan"]
    return (f"Bundle drop: sd:{plan['item']} left this bundle. {plan['new_branch']} now holds only the kept items' "
            f"{len(done['kept'])} commits, cherry-picked onto {done['base']}. Check the kept work still stands "
            f"without sd:{plan['item']}, fix what it needs, and enqueue.")


def dropped_note(done: dict) -> str:
    """Where the dropped item's earlier work stays, for its own fresh build."""
    plan = done["plan"]
    where = done["saved"] or plan["branch"]
    shas = ", ".join(sha[:12] for sha in done["dropped"]) or "none"
    return (f"Bundle drop: sd:{plan['item']} left build {plan['lead']} and builds alone from {done['base']}. "
            f"Its earlier commits stay on {where}: {shas}.")


def drop_item(connection, build, item: int, *, who: str) -> list[dict]:
    """The drop end to end: plan, branch, then the writes; a refused write puts the branch back."""
    try:
        plan = build.drop_plan(connection, item)
    except build.BuildRefused as refusal:
        raise DropRefused(str(refusal)) from None
    done = split_branch(plan)
    try:
        return build.drop_writes(connection, plan, who=who, note=kept_note(done), left=dropped_note(done))
    except Exception:
        undo_split(done)
        raise
