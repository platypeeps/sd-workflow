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


def _has(*argv: str, cwd: str | Path, absent: int) -> bool:
    """Whether a lookup found its ref: exit 0 yes, exit `absent` no; any other exit is a failed lookup, which
    refuses rather than reads as no (round 5: a failed `ls-remote` let the drop rewrite a pushed branch)."""
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


def _ignored(work: str | Path, start: str, kept: list[str]) -> None:
    """Refuse an ignored file where the base or a kept commit tracks a path: the in-place checkout would overwrite
    it, and no branch holds its contents."""
    ignored = _names("ls-files", "-z", "--others", "--ignored", "--exclude-standard", cwd=work)
    if not ignored:
        return
    tracked = _names("ls-tree", "-r", "-z", "--name-only", start, cwd=work)
    for sha in kept:
        tracked |= _paths(work, sha)
    dirs = {"/".join(path.split("/")[:n]) for path in tracked for n in range(1, path.count("/") + 1)}
    clash = sorted(path for path in ignored if path in tracked or path in dirs
                   or any("/".join(path.split("/")[:n]) in tracked for n in range(1, path.count("/") + 1)))
    if clash:
        raise DropRefused(f"ignored local files the new branch tracks: {', '.join(clash[:5])}; move them first")


def drop_sides(work: str | Path, start: str, tip: str, item: int, aside: set[int]) -> tuple[list[str], list[str]]:
    """The non-merge commits of `start..tip`, oldest first, split into (kept, dropped) for dropping `item`. A commit
    citing an item of `aside`, `item` and the members cancelled out of the bundle before, is not kept (round 3)."""
    shas = _out("rev-list", "--reverse", "--no-merges", f"{start}..{tip}", cwd=work).split()
    side: dict[str, str] = {}
    touched: dict[str, set[str]] = {"kept": set(), "dropped": set()}
    loose, named = [], False
    for sha in shas:
        cited = set(sd_ship_body.cited_ids("", [(sha, _out("log", "-1", "--format=%B", sha, cwd=work))]))
        if cited & aside and cited - aside:
            raise DropRefused(f"commit {sha[:12]} cites {', '.join(f'sd:{n}' for n in sorted(cited & aside))} and "
                              f"{', '.join(f'sd:{n}' for n in sorted(cited - aside))}: split it by hand")
        if not cited:
            loose.append(sha)
            continue
        side[sha] = "dropped" if cited & aside else "kept"
        named = named or item in cited
        touched[side[sha]] |= _paths(work, sha)
    # Paths prove whose an uncited commit is only against both sides' cited commits: with one side uncited, every
    # such commit would fall to the other side, the dropped item's change shipping with the kept (round 2).
    # The dropped item's own: a commit of a member cancelled out before says nothing of whose this is (round 3).
    for found, who in ((named, f"sd:{item}"), ("kept" in side.values(), "a kept item")):
        if loose and not found:
            raise DropRefused(f"no commit cites {who}, so nothing says whose commit {loose[0][:12]} is; "
                              f"split it by hand")
    side.update({sha: _whose(work, sha, touched) for sha in loose})
    return [sha for sha in shas if side[sha] == "kept"], [sha for sha in shas if side[sha] == "dropped"]


def _whose(work: str | Path, sha: str, touched: dict[str, set[str]]) -> str:
    """The side of the uncited commit `sha`: the one whose cited commits touch its paths, when only one does."""
    paths = _paths(work, sha)
    hits = [name for name in ("kept", "dropped") if paths & touched[name]]
    if len(hits) != 1:
        raise DropRefused(f"commit {sha[:12]} cites no item and touches "
                          f"{'paths of both sides' if hits else 'no path a citing commit touched'}: "
                          f"no message says whose it is")
    return hits[0]


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
    _merges(work, fork, tip)
    aside = set(plan["aside"])
    kept, dropped = drop_sides(work, fork, tip, item, aside)
    in_place = plan["new_work"] == plan["work"]
    saved = f"{branch}-drop-sd-{item}"
    new_work, new_branch = Path(plan["new_work"]), plan["new_branch"]
    if in_place:
        # The lane pushes without force: a rewritten branch already on origin would not push.
        if _has("ls-remote", "--exit-code", "origin", f"refs/heads/{branch}", cwd=repo, absent=2):
            raise DropRefused(f"{branch} is on origin; drop it only before the lane pushed it")
        if _exists(repo, f"refs/heads/{saved}"):
            raise DropRefused(f"branch {saved} exists: an earlier drop kept it")
        _ignored(work, start, kept)
    elif new_work.exists() or _exists(repo, f"refs/heads/{new_branch}"):
        raise DropRefused(f"{new_work} or branch {new_branch} exists already")
    # Each step is recorded once it moved something, so a failure undoes exactly what moved (`undo_split`).
    done = {"plan": plan, "tip": tip, "saved": None, "made": False, "moved": False, "base": start, "kept": kept,
            "dropped": dropped}
    try:
        if in_place:
            done["saved"] = saved  # first: an interrupt after the branch is made still deletes it
            _git("branch", saved, tip, cwd=repo)
            done["moved"] = True  # a failed checkout may still have moved files: restore the tip either way
            _git("checkout", "-q", "-B", branch, start, cwd=work)
        else:
            done["made"] = True  # a failed add may leave a half-made worktree or branch: remove both either way
            _git("worktree", "add", "-q", "-b", new_branch, str(new_work), start, cwd=repo)
        for sha in kept:
            _git("cherry-pick", "--allow-empty", sha, cwd=new_work)
        check_branch(new_work, start, kept, aside)
        done["head"] = _out("rev-parse", "HEAD", cwd=new_work)
    except BaseException:  # Ctrl-C too: the branch moved, and nothing else would put it back (round 2)
        undo_split(done)
        raise
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
    """Put the branches and worktrees back as `split_branch` found them, only what it moved. The backup branch goes
    only once the branch reads back at its old tip; otherwise it stays, and the refusal names it."""
    plan = done["plan"]
    repo, work = Path(plan["repo"]), Path(plan["new_work"])
    if done.get("moved") or done.get("made"):
        _git("cherry-pick", "--abort", cwd=work, check=False)
    if done.get("moved"):
        _git("checkout", "-q", "-f", "-B", plan["branch"], done["tip"], cwd=work, check=False)
        back = _git("rev-parse", "HEAD", cwd=work, check=False).stdout.strip() == done["tip"] and \
            _git("branch", "--show-current", cwd=work, check=False).stdout.strip() == plan["branch"]
        if not back:
            raise DropRefused(f"the drop failed and could not put {plan['branch']} back in {work}; its old tip "
                              f"{done['tip'][:12]} is kept on {done['saved']}")
    if done.get("saved"):
        _git("branch", "-D", done["saved"], cwd=repo, check=False)
    if done.get("made"):
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
    except BaseException:
        undo_split(done)
        raise
