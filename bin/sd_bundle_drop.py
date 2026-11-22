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

import sd_lib
import sd_ship_body

sd_lib.import_sd_db()
from sd_db import paths  # noqa: E402 - after the helper puts the checkout's lib/ first (sd:745)

#: A fetch, a worktree add or a cherry-pick outlasts `sd_lib`'s 15 s read bound.
GIT_SECONDS = 300


class DropRefused(Exception):
    """Why the drop wrote nothing."""


def _git(*argv: str, cwd: str | Path, check: bool = True) -> subprocess.CompletedProcess:
    # `-C` alone names the repository: a hook's `GIT_DIR` would point every call at its own (round 26).
    try:
        done = subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True, text=True, check=False,
                              stdin=subprocess.DEVNULL, env=paths.git_env(), timeout=GIT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        # Unchecked, a git that never answered reads as a failed exit: undo goes on to its next step.
        done = subprocess.CompletedProcess(argv, -1, "", str(error))
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


def drop_sides(work: str | Path, start: str, tip: str, item: int, aside: set[int],
               built: set[int]) -> tuple[list[str], list[str]]:
    """The non-merge commits of `start..tip`, oldest first, split into (kept, dropped) for dropping `item`. A commit
    citing an item of `aside`, `item` and the members cancelled out of the bundle before, is not kept (round 3).
    A commit that cites no item refuses the drop: paths cannot say whose it is, since a follow-up of the dropped
    item may edit only a file a kept commit touched (round 6). So does one citing an item the bundle did not build
    (`built`): replayed on the kept branch, its `Closes:` would close that item at merge (round 15). A done member
    is in `aside`: reopened before the lane reads it, a replayed commit would close it (pack sd:9863 review)."""
    shas = _out("rev-list", "--reverse", "--no-merges", f"{start}..{tip}", cwd=work).split()
    kept: list[str] = []
    dropped: list[str] = []
    for sha in shas:
        message = _out("log", "-1", "--format=%B", sha, cwd=work)
        # A cherry-picked revert still names the old sha, so the lane would not pair it with the replayed commit,
        # whose `Closes:` would close an item the revert undid (round 9).
        if sd_ship_body._REVERTS.search(message):
            raise DropRefused(f"commit {sha[:12]} reverts another commit; split it by hand")
        cited = set(sd_ship_body.cited_ids("", [(sha, message)]))
        if not cited:
            raise DropRefused(f"commit {sha[:12]} cites no item, so nothing says whose it is; split it by hand")
        if cited - built:
            raise DropRefused(f"commit {sha[:12]} cites {', '.join(f'sd:{n}' for n in sorted(cited - built))}, "
                              f"which this bundle did not build; split it by hand")
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
    kept, dropped = drop_sides(work, fork, tip, item, aside, set(plan["built"]))
    new_work, new_branch = Path(plan["new_work"]), plan["new_branch"]
    if new_work.exists() or _exists(repo, f"refs/heads/{new_branch}"):
        raise DropRefused(f"{new_work} or branch {new_branch} exists already")
    done.update({"plan": plan, "tip": tip, "made": True, "base": start, "fork": fork, "kept": kept,
                 "dropped": dropped})
    # Marked made first: a failed add may leave a half-made worktree or branch; undo removes both either way.
    _git("worktree", "add", "-q", "-b", new_branch, str(new_work), start, cwd=repo)
    done["onto"] = _replay(new_work, start, fork, kept)
    check_branch(new_work, done["onto"], kept, aside, fork)
    done["head"] = _out("rev-parse", "HEAD", cwd=new_work)
    return done


def _replay(work: str | Path, start: str, fork: str, kept: list[str]) -> str:
    """Cherry-pick `kept` onto the base, else onto the fork; the commit it applied on. The base first: a helper the
    dropped commit added and main landed too is there for a kept edit (lane review). The fork when main moved a
    kept file, where the kept commits were made, so each applies; the lane merges the base (pack sd:9863)."""
    failed = ""
    for onto in dict.fromkeys((_out("rev-parse", start, cwd=work), fork)):
        _git("reset", "-q", "--hard", onto, cwd=work)
        for sha in kept:
            done = _git("cherry-pick", "--allow-empty", sha, cwd=work, check=False)
            if done.returncode != 0:
                failed = f"cherry-pick {sha[:12]} onto {onto[:12]}: {(done.stderr or done.stdout).strip()}"
                _git("cherry-pick", "--abort", cwd=work, check=False)
                break
        else:
            return onto
    raise DropRefused(f"the kept commits apply on neither {start} nor the fork {fork[:12]}; {failed}")


def _renames(work: str | Path, old: str, new: str) -> dict[str, str]:
    """The paths `old..new` renamed, old to new: a replay onto the base lands a kept edit on the new name."""
    fields = _git("diff", "--find-renames", "--name-status", "-z", old, new, cwd=work).stdout.split("\0")
    renamed: dict[str, str] = {}
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        if status.startswith(("R", "C")):
            if status.startswith("R"):
                renamed[fields[index + 1]] = fields[index + 2]
            index += 3
        else:
            index += 2
    return renamed


def check_branch(work: str | Path, start: str, kept: list[str], aside: set[int], fork: str) -> None:
    """The new branch holds the kept commits alone: one commit each, no path past theirs, no `aside` citation.
    A kept path main renamed since `fork` counts under its new name too (note #71874)."""
    shas = _out("rev-list", "--reverse", f"{start}..HEAD", cwd=work).split()
    if len(shas) != len(kept):
        raise DropRefused(f"the new branch holds {len(shas)} commits, not the {len(kept)} kept")
    commits = [(sha, _out("log", "-1", "--format=%B", sha, cwd=work)) for sha in shas]
    cited = aside & set(sd_ship_body.cited_ids("", commits))
    if cited:
        raise DropRefused(f"the new branch still cites {', '.join(f'sd:{n}' for n in sorted(cited))}")
    allowed = set().union(*(_paths(work, sha) for sha in kept)) if kept else set()
    if kept:
        renamed = _renames(work, fork, start)
        allowed |= {renamed[path] for path in allowed if path in renamed}
    changed = _names("diff", "--name-only", "-z", start, "HEAD", cwd=work)
    if changed - allowed:
        raise DropRefused(f"the new branch changes {', '.join(sorted(changed - allowed))}, which no kept commit does")


def undo_split(done: dict) -> str:
    """Remove the new worktree and branch `split_branch` made; the old ones it never moved. What it could not
    remove, read back from disk and git, or "" once both are gone."""
    plan = done["plan"]
    repo, work = Path(plan["repo"]), Path(plan["new_work"])
    if not done.get("made"):
        return ""
    _git("cherry-pick", "--abort", cwd=work, check=False)
    # By name alone: `--force` drops a registration whose folder is gone, where a repository-wide `worktree prune`
    # would also drop another worktree's metadata, its index included, while its volume is away (round 24).
    _git("worktree", "remove", "--force", str(work), cwd=repo, check=False)
    _git("branch", "-D", plan["new_branch"], cwd=repo, check=False)
    left = [str(work)] if work.exists() else []
    try:
        left += [plan["new_branch"]] if _exists(repo, f"refs/heads/{plan['new_branch']}") else []
    except DropRefused:
        left.append(f"{plan['new_branch']} (unread)")
    return " and ".join(left)


def kept_note(done: dict) -> str:
    """The finding the kept items' next builder reads."""
    plan = done["plan"]
    return (f"Bundle drop: sd:{plan['item']} left this bundle. {plan['new_branch']} now holds only the kept items' "
            f"{len(done['kept'])} commits, cherry-picked onto {done['onto'][:12]} "
            f"({done['base']} or where it forked from it). Check the kept work still stands "
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
    except BaseException as error:
        # A COMMIT that failed outside the helper's rollback leaves the transaction open, its writes visible to
        # this connection alone: roll it back before reading whether they committed (round 11).
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        # Ctrl-C after the commit: the queued builds read the split branch, so it stays (round 7).
        if not build.drop_committed(connection, plan):
            # The reserved id names the new worktree and branch: released while either stays, SQLite would give it
            # to the next build, which would take them by name, with the wrong commits (round 18).
            left = undo_split(done) if done else ""
            if left:
                raise DropRefused(f"the drop failed and could not remove {left}; assignment {plan['new_lead']} "
                                  f"stays reserved so no build reuses them: remove them by hand") from error
            build.drop_release(connection, plan)
        raise
