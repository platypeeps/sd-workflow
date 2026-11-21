"""Bundle drop (pack sd:9709): take one item out of an ended build bundle, its lead included.

`sd assignments build ITEM --drop` cherry-picks the kept items' commits onto a fresh branch from the base, then
queues the kept items under a kept lead and the dropped item alone. It replaces the revert recipe: a subset Retry
must name the lead (ruling #64210), and a reverted commit keeps its `Closes: sd:N`, so the merge closed the item
it dropped (sd:9651).

A commit is the dropped item's when its message cites it (`sd_ship_body.cited_ids`), the kept items' when it
cites only others. One that cites nothing refuses the drop: no message says whose it is.
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


def _has(*argv: str, cwd: str | Path, absent: int) -> bool:
    """Whether a lookup found its ref: exit 0 yes, exit `absent` no; any other exit is a failed lookup, which
    refuses rather than reads as no (round 5)."""
    done = _git(*argv, cwd=cwd, check=False)
    if done.returncode not in (0, absent):
        raise DropRefused(f"git {' '.join(argv)} failed in {cwd}: {(done.stderr or done.stdout).strip()}")
    return done.returncode == 0


def _out(*argv: str, cwd: str | Path) -> str:
    return _git(*argv, cwd=cwd).stdout.strip()


def _names(*argv: str, cwd: str | Path) -> set[str]:
    """The paths a `-z` git command lists, every byte kept: a stripped read lost a leading space (round 2)."""
    return set(filter(None, _git(*argv, cwd=cwd).stdout.split("\0")))


def drop_base(repo: str | Path) -> str:
    """The branch a build starts from, as `build_run.Supervisor.base` reads it."""
    done = _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=repo, check=False)
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else "origin/main"


def _paths(work: str | Path, sha: str) -> set[str]:
    return _names("diff-tree", "--no-commit-id", "-r", "--name-only", "-z", sha, cwd=work)


def _merges(work: str | Path, start: str, tip: str) -> None:
    """Refuse a merge in `start..tip` whose tree is not what git merges its parents to: its resolution is in no
    commit the split cherry-picks, so the kept branch would lose it. A clean merge brings only what the base has."""
    for line in _out("rev-list", "--merges", "--parents", f"{start}..{tip}", cwd=work).splitlines():
        sha, *parents = line.split()
        if len(parents) != 2:
            raise DropRefused(f"merge {sha[:12]} has {len(parents)} parents; split it by hand")
        tree = _git("merge-tree", "--write-tree", *parents, cwd=work, check=False)
        if tree.returncode != 0 or tree.stdout.split()[0] != _out("rev-parse", f"{sha}^{{tree}}", cwd=work):
            raise DropRefused(f"merge {sha[:12]} resolves by hand what no cherry-pick carries; split it by hand")


def drop_sides(work: str | Path, start: str, tip: str, item: int, aside: set[int]) -> tuple[list[str], list[str]]:
    """The non-merge commits of `start..tip`, oldest first, split into (kept, dropped) for dropping `item`. A commit
    citing an item of `aside`, `item` and the members cancelled out of the bundle before, is not kept (round 3).
    A commit that cites no item refuses the drop: paths cannot say whose it is, since a follow-up of the dropped
    item may edit only a file a kept commit touched (round 6)."""
    shas = _out("rev-list", "--reverse", "--no-merges", f"{start}..{tip}", cwd=work).split()
    kept, dropped = [], []
    for sha in shas:
        message = _out("log", "-1", "--format=%B", sha, cwd=work)
        # A cherry-picked revert still names the old sha, so the lane would not pair it with the replayed commit,
        # whose `Closes:` would close an item the revert undid (round 9).
        if sd_ship_body._REVERTS.search(message):
            raise DropRefused(f"commit {sha[:12]} reverts another commit; split it by hand")
        cited = set(sd_ship_body.cited_ids("", [(sha, message)]))
        if not cited:
            raise DropRefused(f"commit {sha[:12]} cites no item, so nothing says whose it is; split it by hand")
        if cited & aside and cited - aside:
            raise DropRefused(f"commit {sha[:12]} cites {', '.join(f'sd:{n}' for n in sorted(cited & aside))} and "
                              f"{', '.join(f'sd:{n}' for n in sorted(cited - aside))}: split it by hand")
        (dropped if cited & aside else kept).append(sha)
    return kept, dropped


def _clean(work: str | Path, branch: str) -> None:
    if _out("branch", "--show-current", cwd=work) != branch:
        raise DropRefused(f"worktree {work} is not on {branch}")
    if _out("status", "--porcelain", "--untracked-files=all", cwd=work):
        raise DropRefused(f"worktree {work} is not clean")
    for ref in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD"):
        if _exists(work, ref):
            raise DropRefused(f"worktree {work} has an unfinished {ref.split('_')[0].lower()}")


def _exists(repo: str | Path, ref: str) -> bool:
    return _has("rev-parse", "-q", "--verify", ref, cwd=repo, absent=1)


def split_branch(plan: dict, done: dict) -> dict:
    """The kept commits on `plan["new_branch"]`, fresh from the base, in the new worktree `plan["new_work"]`; the
    old branch and worktree stay as they were, with the dropped item's work and files. Each step is recorded in the
    caller's `done` once it moved something, so the caller's one handler undoes exactly what moved (`undo_split`),
    whenever it is interrupted: a Ctrl-C between this return and the writes undid nothing before (round 11)."""
    repo, work, branch, item = Path(plan["repo"]), Path(plan["work"]), plan["branch"], plan["item"]
    if not work.is_dir():
        raise DropRefused(f"worktree {work} is not on this machine: drop it on the host that built it")
    _clean(work, branch)
    _git("fetch", "-q", "origin", cwd=repo)
    start = drop_base(repo)
    tip = _out("rev-parse", "HEAD", cwd=work)
    fork = _out("merge-base", start, tip, cwd=work)
    _merges(work, fork, tip)
    aside = set(plan["aside"])
    kept, dropped = drop_sides(work, fork, tip, item, aside)
    new_work, new_branch = Path(plan["new_work"]), plan["new_branch"]
    if new_work.exists() or _exists(repo, f"refs/heads/{new_branch}"):
        raise DropRefused(f"{new_work} or branch {new_branch} exists already")
    done.update({"plan": plan, "tip": tip, "made": True, "base": start, "kept": kept, "dropped": dropped})
    # Marked made first: a failed add may leave a half-made worktree or branch; undo removes both either way.
    _git("worktree", "add", "-q", "-b", new_branch, str(new_work), start, cwd=repo)
    for sha in kept:
        _git("cherry-pick", "--allow-empty", sha, cwd=new_work)
    check_branch(new_work, start, kept, aside)
    done["head"] = _out("rev-parse", "HEAD", cwd=new_work)
    return done


def check_branch(work: str | Path, start: str, kept: list[str], aside: set[int]) -> None:
    """The new branch holds the kept commits alone: one commit each, no path past theirs, no `aside` citation."""
    shas = _out("rev-list", "--reverse", f"{start}..HEAD", cwd=work).split()
    if len(shas) != len(kept):
        raise DropRefused(f"the new branch holds {len(shas)} commits, not the {len(kept)} kept")
    commits = [(sha, _out("log", "-1", "--format=%B", sha, cwd=work)) for sha in shas]
    cited = aside & set(sd_ship_body.cited_ids("", commits))
    if cited:
        raise DropRefused(f"the new branch still cites {', '.join(f'sd:{n}' for n in sorted(cited))}")
    allowed = set().union(*(_paths(work, sha) for sha in kept)) if kept else set()
    changed = _names("diff", "--name-only", "-z", start, "HEAD", cwd=work)
    if changed - allowed:
        raise DropRefused(f"the new branch changes {', '.join(sorted(changed - allowed))}, which no kept commit does")


def undo_split(done: dict) -> None:
    """Remove the new worktree and branch `split_branch` made; the old ones it never moved."""
    plan = done["plan"]
    repo, work = Path(plan["repo"]), Path(plan["new_work"])
    if done.get("made"):
        _git("cherry-pick", "--abort", cwd=work, check=False)
        _git("worktree", "remove", "--force", str(work), cwd=repo, check=False)
        _git("worktree", "prune", cwd=repo, check=False)
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
    shas = ", ".join(sha[:12] for sha in done["dropped"]) or "none"
    return (f"Bundle drop: sd:{plan['item']} left build {plan['lead']} and builds alone from {done['base']}. "
            f"Its earlier commits stay on {plan['branch']}: {shas}.")


def drop_item(connection, build, item: int, *, who: str) -> list[dict]:
    """The drop end to end: plan, branch, then the writes; a refused write puts the branch back."""
    try:
        plan = build.drop_reserve(connection, build.drop_plan(connection, item))
    except build.BuildRefused as refusal:
        raise DropRefused(str(refusal)) from None
    done: dict = {}
    try:  # one handler from the split's first move to the writes' commit, Ctrl-C included (rounds 2 and 11)
        split_branch(plan, done)
        return build.drop_writes(connection, plan, who=who, note=kept_note(done), left=dropped_note(done))
    except BaseException:
        # A COMMIT that failed outside the helper's rollback leaves the transaction open, its writes visible to
        # this connection alone: roll it back before reading whether they committed (round 11).
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        # Ctrl-C after the commit: the queued builds read the split branch, so it stays (round 7).
        if not build.drop_committed(connection, plan):
            try:
                if done:
                    undo_split(done)
            finally:
                build.drop_release(connection, plan)
        raise
