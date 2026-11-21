"""Bundle drop (pack sd:9709): one item out of an ended bundle, its lead included, on real git branches.

The revert recipe it replaces failed twice on one bundle (sd:9651): a subset Retry must name the lead, and a
reverted commit keeps its `Closes: sd:N`, so the merge would close the dropped item undone.
"""
import importlib
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

from sd_db import build, build_run, operations, runner, writes
from sd_db.writes import create_assignment, record_state, upsert_repo

from .test_build import START, Case, Kill, Probe

BIN = Path(__file__).resolve().parents[2] / "bin"
with mock.patch("sys.path", [str(BIN), *sys.path]):
    DROP = importlib.import_module("sd_bundle_drop")

IDENTITY = {"GIT_AUTHOR_NAME": "operator", "GIT_AUTHOR_EMAIL": "operator@example.test",
            "GIT_COMMITTER_NAME": "operator", "GIT_COMMITTER_EMAIL": "operator@example.test",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *argv):
    return subprocess.run(["git", "-C", str(cwd), *argv], check=True, capture_output=True, text=True).stdout.strip()


class Repo(Case):
    """A real repository with an origin, and a three-item bundle built on it."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict("os.environ", IDENTITY)
        env.start()
        self.addCleanup(env.stop)
        origin, self.repo = self.root / "origin.git", self.root / "repo"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(self.repo)], check=True, capture_output=True)
        (self.repo / "README").write_text("base\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "chore: base")
        git(self.repo, "push", "-q", "origin", "HEAD:main")
        git(self.repo, "remote", "set-head", "origin", "main")
        upsert_repo(self.db, str(self.repo), remote=str(origin), managed=1)

    def next_lead(self):
        """The id a lead drop reserves for its new lead: the next assignment id (round 10)."""
        return self.db.execute("SELECT MAX(id) + 1 FROM assignment").fetchone()[0]

    def drop(self, run, item):
        """Drop `item`; the kept bundle's new worktree, named by the lead the drop reserves (round 13)."""
        lead = self.next_lead()
        DROP.drop_item(self.db, build, item, who="operator")
        return Path(run["work_path"]).parent / f"repo-build-{lead}"

    def unmade(self, run):
        """No new worktree or branch is left for the lead a drop would reserve."""
        self.assertFalse((Path(run["work_path"]).parent / f"repo-build-{self.next_lead()}").exists())
        self.assertEqual(git(self.repo, "branch", "--list", f"build/sd-{self.next_lead()}"), "")

    def commit(self, work, name, text, message):
        (Path(work) / name).write_text(text)
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", message)

    def bundle(self, cite_second=True):
        """Three items built on one branch, ended blocked: the lead's commit, the second's, a review fix citing the
        lead, the third's. (items, assignment ids, run). `cite_second` False: the second's commit is an
        uncited edit of the lead's file instead."""
        items = [self.item(repo=str(self.repo)) for _ in range(3)]
        build.queue(self.db, items, who="operator")
        run = self.claim()
        work = Path(run["work_path"])
        git(self.repo, "worktree", "add", "-q", "-b", run["branch"], str(work), "origin/main")
        self.commit(work, "a.txt", "lead\n", f"feat: lead\n\nCloses: sd:{items[0]}")
        if cite_second:
            self.commit(work, "b.txt", "second\n", f"feat: second\n\nCloses: sd:{items[1]}")
        else:
            self.commit(work, "a.txt", "second\n", "fix: second")
        self.commit(work, "a.txt", "lead fixed\n", f"fix: review round\n\nLane review fix, repo sd:{items[0]}")
        self.commit(work, "c.txt", "third\n", f"feat: third\n\nCloses: sd:{items[2]}")
        build.write(self.db, run["id"], supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START)
        build.end(self.db, run["id"], "blocked", "3 review rounds left blockers: a.txt:1 lead")
        build.reap(self.db, probe=Probe(alive=False, group=False), kill=Kill(self.db))
        ids = [member["id"] for member in build.run(self.db, run["id"])["members"]]
        return items, ids, run

    def main_moves(self, name, text):
        """origin/main gains a commit on `name`, made in a second clone."""
        other = self.root / "other"
        if not other.exists():
            subprocess.run(["git", "clone", "-q", str(self.root / "origin.git"), str(other)], check=True,
                           capture_output=True)
        git(other, "pull", "-q", "origin", "main")
        (other / name).write_text(text)
        git(other, "add", "-f", "-A")
        git(other, "commit", "-q", "-m", f"chore: main moves {name}")
        git(other, "push", "-q", "origin", "HEAD:main")
        git(self.repo, "fetch", "-q", "origin")

    def move(self, item):
        """`item` moves to another managed repository."""
        other = self.root / "other"
        upsert_repo(self.db, str(other), remote="https://example.test/other.git", managed=1)
        self.db.execute("UPDATE item SET repo = ? WHERE id = ?", (str(other), item))
        return other

    def files(self, work):
        return sorted(git(work, "ls-files").split())

    def closes(self, work):
        return git(work, "log", "--format=%B", "origin/main..HEAD")

class Drop(Repo):
    def test_dropping_a_member_keeps_the_others_commits_alone_and_builds_it_alone(self):
        items, (lead, second, third), run = self.bundle()
        work = Path(run["work_path"])
        old = git(work, "rev-parse", "HEAD")
        lead_id = self.next_lead()
        queued = DROP.drop_item(self.db, build, items[1], who="operator")
        new = work.parent / f"repo-build-{lead_id}"
        self.assertEqual(self.files(new), ["README", "a.txt", "c.txt"])
        self.assertEqual((new / "a.txt").read_text(), "lead fixed\n")
        self.assertNotIn(f"Closes: sd:{items[1]}", self.closes(new))
        self.assertEqual(git(new, "rev-list", "--count", "origin/main..HEAD"), "3")
        self.assertEqual(git(work, "rev-parse", "HEAD"), old, "the old branch keeps the dropped item's work")
        self.assertEqual([entry["item"] for entry in queued], [items[0], items[2], items[1]])
        self.assertEqual(queued[0]["id"], lead_id)
        self.assertFalse({entry["id"] for entry in queued} & {lead, second, third})
        self.assertEqual({runner.queue_state(self.db, ident)["status"] for ident in (lead, second, third)},
                         {"cancelled"})
        again = self.claim()
        self.assertEqual([member["item"] for member in build.run(self.db, again["id"])["members"]],
                         [items[0], items[2]])
        self.assertEqual((Path(again["work_path"]), again["branch"]), (new, f"build/sd-{lead_id}"))

    def test_dropping_the_lead_makes_a_kept_item_the_lead_on_a_fresh_branch(self):
        items, (lead, second, third), run = self.bundle()
        work = Path(run["work_path"])
        old = git(work, "rev-parse", "HEAD")
        lead_id = self.next_lead()
        DROP.drop_item(self.db, build, items[0], who="operator")
        self.assertEqual(git(work, "rev-parse", "HEAD"), old, "the old branch keeps the dropped item's work")
        again = self.claim()
        self.assertEqual([member["item"] for member in build.run(self.db, again["id"])["members"]], items[1:])
        self.assertEqual(again["assignment"], lead_id)
        new = Path(again["work_path"])
        self.assertEqual((new, again["branch"]), (work.parent / f"repo-build-{lead_id}", f"build/sd-{lead_id}"))
        self.assertEqual(git(new, "branch", "--show-current"), again["branch"])
        self.assertEqual(self.files(new), ["README", "b.txt", "c.txt"])
        self.assertNotIn(f"sd:{items[0]}", self.closes(new))
        self.assertEqual(runner.queue_state(self.db, lead)["status"], "cancelled")
        fresh = self.db.execute("SELECT status FROM assignment WHERE item = ? ORDER BY id DESC LIMIT 1",
                                (items[0],)).fetchone()
        self.assertEqual(fresh["status"], "queued")

    def test_an_uncited_commit_of_both_sides_refuses_and_writes_nothing(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        (work / "b.txt").write_text("both\n")
        self.commit(work, "a.txt", "both\n", "fix: touches both")
        old = git(work, "rev-parse", "HEAD")
        with self.assertRaisesRegex(DROP.DropRefused, "cites no item"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(git(work, "rev-parse", "HEAD"), old)
        self.assertEqual(self.statuses(items), ["blocked"] * 3)
        self.assertEqual({runner.queue_state(self.db, ident)["status"] for ident in ids}, {"blocked"})

    def test_a_refused_write_puts_the_branch_back(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        old = git(work, "rev-parse", "HEAD")
        for item in (items[1], items[0]):
            with self.subTest(item=item), \
                    mock.patch.object(build, "drop_writes", side_effect=build.BuildRefused("Drop refused: changed")), \
                    self.assertRaisesRegex(build.BuildRefused, "changed"):
                DROP.drop_item(self.db, build, item, who="operator")
            self.assertEqual((git(work, "rev-parse", "HEAD"), git(work, "branch", "--show-current")),
                             (old, run["branch"]))
            self.unmade(run)

    def test_a_drop_refuses_a_running_bundle(self):
        items = [self.item(repo=str(self.repo)) for _ in range(2)]
        build.queue(self.db, items, who="operator")
        self.claim()
        with self.assertRaisesRegex(DROP.DropRefused, "Drop refused: the run still holds its lease"):
            DROP.drop_item(self.db, build, items[1], who="operator")


class DropReview(Repo):
    """Review round 1 of pack sd:9709, class pass. Git state: a merge's resolution, ignored files, rollback order.
    Requeue: a kept item's newer build, and the dropped item bundled with fresh rows."""

    def test_a_merge_with_a_hand_resolution_refuses(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        self.commit(work, "README", "branch side\n", f"fix: second readme\n\nCloses: sd:{items[1]}")
        self.main_moves("README", "main side\n")
        subprocess.run(["git", "-C", str(work), "merge", "-q", "origin/main"], capture_output=True)
        (work / "README").write_text("resolved by hand\n")
        git(work, "add", "README")
        git(work, "commit", "-q", "--no-edit")
        old = git(work, "rev-parse", "HEAD")
        with self.assertRaisesRegex(DROP.DropRefused, "merge .* resolves"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(git(work, "rev-parse", "HEAD"), old)

    def test_a_clean_merge_of_main_still_drops(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        self.main_moves("d.txt", "main\n")
        git(work, "merge", "-q", "--no-edit", "origin/main")
        self.assertEqual(self.files(self.drop(run, items[1])), ["README", "a.txt", "c.txt", "d.txt"])

    def test_a_kept_item_with_a_newer_build_refuses(self):
        items, ids, run = self.bundle()
        newer = create_assignment(self.db, role="author", status="blocked", item=items[2])
        self.db.execute("UPDATE assignment SET scope = ? WHERE id = ?", (build.SCOPE, newer))
        work = Path(run["work_path"])
        old = git(work, "rev-parse", "HEAD")
        with self.assertRaisesRegex(DROP.DropRefused, f"sd:{items[2]} has a newer build, assignment {newer}"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(git(work, "rev-parse", "HEAD"), old)

    def test_the_dropped_item_builds_alone_beside_fresh_rows(self):
        items, ids, run = self.bundle()
        DROP.drop_item(self.db, build, items[1], who="operator")
        other = self.item(repo=str(self.repo))
        build.queue(self.db, [other], who="operator")
        bundles = []
        while (claimed := self.claim()) is not None:
            bundles.append([member["item"] for member in build.run(self.db, claimed["id"])["members"]])
            build.write(self.db, claimed["id"], supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START)
            build.end(self.db, claimed["id"], "blocked", "fixture")
            build.reap(self.db, probe=Probe(alive=False, group=False), kill=Kill(self.db))
        self.assertIn([items[1]], bundles)
        self.assertEqual(sorted(sum(bundles, [])), sorted([*items, other]))


class DropRound2(Repo):
    """Review round 2 of pack sd:9709, class pass. Paths read as NUL-delimited lists keep every byte; a split no
    commit proves refuses; the lead must be one Retry would take; any interrupt after a ref moved rolls back."""


    def test_a_done_lead_refuses_a_member_drop(self):
        items, ids, run = self.bundle()
        self.db.execute("UPDATE item SET status = 'done' WHERE id = ?", (items[0],))
        with self.assertRaisesRegex(DROP.DropRefused, f"its lead sd:{items[0]} is done"):
            DROP.drop_item(self.db, build, items[1], who="operator")

    def test_a_dropped_item_no_commit_cites_refuses_an_uncited_commit(self):
        items, ids, run = self.bundle(cite_second=False)
        work = Path(run["work_path"])
        old = git(work, "rev-parse", "HEAD")
        with self.assertRaisesRegex(DROP.DropRefused, "cites no item"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(git(work, "rev-parse", "HEAD"), old)

    def interrupted(self, when):
        """`_git` that raises `when(argv, check)`'s exception, or runs the real git."""
        real = DROP._git

        def fake(*argv, cwd, check=True):
            fault = when(argv, check)
            if fault:
                raise fault
            return real(*argv, cwd=cwd, check=check)
        return mock.patch.object(DROP, "_git", side_effect=fake)

    def test_ctrl_c_during_cherry_pick_puts_the_branch_back(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        old = git(work, "rev-parse", "HEAD")
        def when(argv, check):
            return KeyboardInterrupt() if argv[0] == "cherry-pick" and "--abort" not in argv else None
        with self.interrupted(when), self.assertRaises(KeyboardInterrupt):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual((git(work, "rev-parse", "HEAD"), git(work, "branch", "--show-current")), (old, run["branch"]))
        self.unmade(run)

    def test_a_failed_head_read_after_the_picks_puts_the_branch_back(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        old = git(work, "rev-parse", "HEAD")
        picked = []

        def when(argv, check):
            picked.extend(["x"] if argv[0] == "cherry-pick" else [])
            return DROP.DropRefused("read failed") if picked and argv == ("rev-parse", "HEAD") and check else None
        with self.interrupted(when), self.assertRaisesRegex(DROP.DropRefused, "read failed"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(git(work, "rev-parse", "HEAD"), old)
        self.unmade(run)


class DropRound3(Repo):
    """Review round 3 of pack sd:9709, class pass. The drop read stored bundle state raw, not through the readers the
    build uses: the repo key as a disk path (`paths.expand`, as `Supervisor`), and the branch's commits without the
    bundle's exclusions (`_in_group`, as `left`)."""

    def home_is_root(self):
        """HOME is the test root, so the repo is stored as the `~/repo` key, as on a real machine."""
        env = mock.patch.dict("os.environ", {"HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.db.execute("UPDATE repo SET path = '~/repo' WHERE path = ?", (str(self.repo),))

    def test_a_home_key_repo_drops_a_member(self):
        self.home_is_root()
        items, ids, run = self.bundle()
        self.assertTrue(self.db.execute("SELECT repo FROM runner_run WHERE id = ?",
                                        (run["id"],)).fetchone()["repo"].startswith("~/"))
        self.assertEqual(self.files(self.drop(run, items[1])), ["README", "a.txt", "c.txt"])

    def test_a_home_key_repo_drops_the_lead(self):
        self.home_is_root()
        items, (lead, second, third), run = self.bundle()
        lead_id = self.next_lead()
        DROP.drop_item(self.db, build, items[0], who="operator")
        self.assertEqual(self.files(Path(run["work_path"]).parent / f"repo-build-{lead_id}"),
                         ["README", "b.txt", "c.txt"])

    def test_a_member_left_out_earlier_stays_off_the_kept_branch(self):
        items, (lead, second, third), run = self.bundle()
        build.retry(self.db, lead, who="operator", only=[items[0], items[2]])
        again = self.claim()
        self.assertEqual(again["branch"], run["branch"])
        build.write(self.db, again["id"], supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START)
        build.end(self.db, again["id"], "blocked", "blockers")
        build.reap(self.db, probe=Probe(alive=False, group=False), kill=Kill(self.db))
        DROP.drop_item(self.db, build, items[0], who="operator")
        new = self.claim()
        self.assertEqual([member["item"] for member in build.run(self.db, new["id"])["members"]], [items[2]])
        self.assertEqual(self.files(Path(new["work_path"])), ["README", "c.txt"])
        self.assertNotIn(f"sd:{items[1]}", self.closes(Path(new["work_path"])))


class DropRound5(Repo):
    """Review round 5 of pack sd:9709, class pass: a git exit code read as an answer. Only `rev-parse -q --verify`
    exit 1 says a ref is unset; any other failure refuses unwritten."""

    def failing(self, *prefix, code=128):
        real = DROP._git

        def fake(*argv, cwd, check=True):
            if argv[:len(prefix)] == prefix:
                return subprocess.CompletedProcess(["git", *argv], code, "", "fatal: example failure")
            return real(*argv, cwd=cwd, check=check)
        return mock.patch.object(DROP, "_git", side_effect=fake)

    def assert_unwritten(self, items, run, old):
        work = Path(run["work_path"])
        self.assertEqual((git(work, "rev-parse", "HEAD"), git(work, "branch", "--show-current")),
                         (old, run["branch"]))
        self.unmade(run)
        self.assertEqual(self.statuses(items), ["blocked"] * 3)

    def test_a_failed_rev_parse_refuses_the_drop(self):
        items, ids, run = self.bundle()
        old = git(Path(run["work_path"]), "rev-parse", "HEAD")
        for item in (items[1], items[0]):
            with self.subTest(item=item), self.failing("rev-parse", "-q", "--verify"), \
                    self.assertRaisesRegex(DROP.DropRefused, "rev-parse"):
                DROP.drop_item(self.db, build, item, who="operator")
            self.assert_unwritten(items, run, old)


class DropRound6(Repo):
    """Review round 6 of pack sd:9709: paths cannot say whose an uncited commit is. A follow-up of the dropped
    item that edits only a file a kept commit touched would ship with the kept items, and the branch check took it,
    since the follow-up supplied its own allowed path. Every uncited commit now refuses the drop."""

    def test_an_uncited_follow_up_of_the_dropped_item_on_a_kept_file_refuses(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        self.commit(work, "a.txt", "second's follow-up\n", "fix: second follow-up")
        old = git(work, "rev-parse", "HEAD")
        with self.assertRaisesRegex(DROP.DropRefused, "cites no item"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual((git(work, "rev-parse", "HEAD"), git(work, "branch", "--show-current")),
                         (old, run["branch"]))
        self.unmade(run)
        self.assertEqual(self.statuses(items), ["blocked"] * 3)


class DropRound7(Repo):
    """Review round 7 of pack sd:9709: a drop after a subset Retry whose next run never read the branch, and an
    interrupt after the drop's writes committed."""

    def test_a_member_drop_after_an_unread_subset_retry_leaves_the_bundle_unheld(self):
        items, (lead, second, third), run = self.bundle()
        build.retry(self.db, lead, who="operator", only=[items[0], items[2]])
        again = self.claim()
        build.write(self.db, again["id"], supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START)
        build.end(self.db, again["id"], "cancelled", "operator cancelled")
        build.reap(self.db, probe=Probe(alive=False, group=False), kill=Kill(self.db))
        self.assertEqual(build.split(self.db, lead), "unread")
        lead_id = self.next_lead()
        new = self.drop(run, items[2])
        # The kept bundle is a new lead on a new branch no subset Retry touched: its next run holds nothing.
        self.assertEqual(build.split(self.db, lead_id), "")
        self.assertEqual(self.files(new), ["README", "a.txt"])

    def test_an_interrupt_after_the_writes_committed_keeps_the_split(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"]).parent / f"repo-build-{self.next_lead()}"
        real = build.drop_writes

        def committed_then_interrupted(*args, **kwargs):
            real(*args, **kwargs)
            raise KeyboardInterrupt

        with mock.patch.object(build, "drop_writes", side_effect=committed_then_interrupted), \
                self.assertRaises(KeyboardInterrupt):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(self.files(work), ["README", "a.txt", "c.txt"])
        self.assertNotIn(f"sd:{items[1]}", self.closes(work))


class DropRound8(Repo):
    """Review round 8 of pack sd:9709: the drop set aside only the dropped item and members marked `ALONE`. A
    member moved back to planning (its assignment cancelled, `UNBUILT`) fell outside both, so its cited commits
    reached the kept branch, and the next merge would close it in planning. Every item the bundle built that is
    not kept, and not done, is now set aside, as Retry reads who leaves from the run's membership."""

    def test_a_member_moved_to_planning_stays_off_the_kept_branch(self):
        items, ids, run = self.bundle()
        writes.transition(self.db, items[2], "planning", who="operator")
        work = self.drop(run, items[1])
        self.assertEqual(self.files(work), ["README", "a.txt"])
        self.assertNotIn(f"sd:{items[2]}", self.closes(work))


class DropRound9(Repo):
    """Review round 9 of pack sd:9709: a cherry-picked revert still names the original sha, so on the new branch
    the lane no longer pairs it with the replayed commit, whose `Closes:` then closes an item the revert undid.
    The drop refuses a range that holds a revert."""

    def test_a_kept_revert_refuses_the_drop(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        lead = git(work, "log", "--format=%H", "-1", "--grep", f"Closes: sd:{items[0]}")
        git(work, "rm", "-q", "a.txt")
        git(work, "commit", "-q", "-m",
            f"Revert \"feat: lead\"\n\nThis reverts commit {lead}.\n\nLane review fix, repo sd:{items[2]}")
        head = git(work, "rev-parse", "HEAD")
        with self.assertRaisesRegex(DROP.DropRefused, "reverts"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(git(work, "rev-parse", "HEAD"), head)
        self.assertEqual(self.statuses(items), ["blocked"] * 3)


class DropRound10(Repo):
    """Review round 10 of pack sd:9709: a lead drop reparented the kept members, so the ended run lost them and the
    sweeper could remove its worktree while their items stayed open; and the writes rechecked only the lead's
    revision, so a kept member moved to planning during the split was queued again."""

    def test_a_lead_drop_keeps_the_ended_run_membership(self):
        items, ids, run = self.bundle()
        before = [member["item"] for member in build.run(self.db, run["id"])["members"]]
        DROP.drop_item(self.db, build, items[0], who="operator")
        self.assertEqual([member["item"] for member in build.run(self.db, run["id"])["members"]], before)
        new = self.claim()
        self.assertEqual([member["item"] for member in build.run(self.db, new["id"])["members"]], items[1:])
        self.assertEqual(self.files(Path(new["work_path"])), ["README", "b.txt", "c.txt"])

    def test_a_kept_member_moved_during_the_split_refuses_the_writes(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        old = git(work, "rev-parse", "HEAD")
        real = DROP.split_branch

        def moved(plan, done):
            real(plan, done)
            # Planning cancels its assignment; back to ready passes the press's own item check.
            writes.transition(self.db, items[2], "planning", who="operator")
            writes.transition(self.db, items[2], "ready", who="operator")
            return done

        with mock.patch.object(DROP, "split_branch", side_effect=moved), \
                self.assertRaisesRegex(build.BuildRefused, "changed since the drop read it"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(git(work, "rev-parse", "HEAD"), old)
        self.assertEqual(self.db.execute("SELECT status FROM assignment WHERE id = ?", (ids[2],)).fetchone()[0],
                         "cancelled")


class DropRound11(Repo):
    """Review round 11 of pack sd:9709: a COMMIT that fails and leaves its transaction open, and Ctrl-C between
    the split and the writes."""

    def unsplit(self, items, run, old):
        work = Path(run["work_path"])
        self.assertEqual((git(work, "rev-parse", "HEAD"), git(work, "branch", "--show-current")),
                         (old, run["branch"]))
        self.unmade(run)
        self.assertEqual(self.statuses(items), ["blocked"] * 3)

    def test_a_failed_commit_left_open_undoes_the_split(self):
        items, ids, run = self.bundle()
        old = git(Path(run["work_path"]), "rev-parse", "HEAD")

        def commit_fails(connection, plan, **_):
            # The writes ran, then COMMIT failed outside the helper's rollback: the transaction stays open.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE assignment SET status = 'cancelled', result = ? WHERE id = ?",
                               (f"{build.ALONE}operator: {build.DROPPED}{plan['run']}", plan["assignment"]))
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(build, "drop_writes", side_effect=commit_fails), \
                self.assertRaises(sqlite3.OperationalError):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertFalse(self.db.in_transaction)
        self.unsplit(items, run, old)

    def test_an_interrupt_between_the_split_and_the_writes_undoes_the_split(self):
        items, ids, run = self.bundle()
        old = git(Path(run["work_path"]), "rev-parse", "HEAD")
        real = DROP.split_branch

        def split_then_interrupted(*args, **kwargs):
            real(*args, **kwargs)
            raise KeyboardInterrupt

        with mock.patch.object(DROP, "split_branch", side_effect=split_then_interrupted), \
                self.assertRaises(KeyboardInterrupt):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.unsplit(items, run, old)


class DropRound12(Repo):
    """Review round 12 of pack sd:9709, class pass: an identity checked by its stored spelling, not by what the
    system resolves it to. A host compared as the repo's current lane, where the worktree is on the machine that
    ran the run (a lead drop, a member drop, and the machine that runs the drop)."""

    def on_satellite(self, run, lane):
        """This machine is satellite `sat-a`, which ran `run`; the repo's lane is now `lane`."""
        self.db.execute("UPDATE runner_run SET owner = 'fixture@sat-a' WHERE id = ?", (run["id"],))
        self.db.execute("UPDATE repo SET lane_host = ? WHERE path = ?", (lane, str(self.repo)))
        for host in ("sat-a", "sat-b"):
            record_state(self.db, "heartbeat", key=f"build-dispatch@{host}", body={})
        return mock.patch.multiple(build, lane_hosts=mock.Mock(return_value=frozenset({"sat-a"})),
                                   hub_host=mock.Mock(return_value="hub"))

    def moved_lane_refuses(self, index):
        items, ids, run = self.bundle()
        old = git(Path(run["work_path"]), "rev-parse", "HEAD")
        with self.on_satellite(run, "sat-b"), self.assertRaisesRegex(DROP.DropRefused, "sat-a"):
            DROP.drop_item(self.db, build, items[index], who="operator")
        self.assertEqual(git(Path(run["work_path"]), "rev-parse", "HEAD"), old)
        self.assertEqual(self.statuses(items), ["blocked"] * 3)

    def test_a_lead_drop_refuses_when_the_lane_moved_off_the_host_that_ran_it(self):
        self.moved_lane_refuses(0)

    def test_a_member_drop_refuses_when_the_lane_moved_off_the_host_that_ran_it(self):
        self.moved_lane_refuses(1)

    def test_a_drop_on_the_host_that_ran_it_still_drops(self):
        items, ids, run = self.bundle()
        with self.on_satellite(run, "sat-a"):
            new = self.drop(run, items[1])
        self.assertEqual(self.files(new), ["README", "a.txt", "c.txt"])

    def test_a_drop_refuses_on_a_machine_that_did_not_run_it(self):
        items, ids, run = self.bundle()
        self.db.execute("UPDATE runner_run SET owner = 'fixture@sat-a' WHERE id = ?", (run["id"],))
        self.db.execute("UPDATE repo SET lane_host = 'sat-a' WHERE path = ?", (str(self.repo),))
        record_state(self.db, "heartbeat", key="build-dispatch@sat-a", body={})
        with self.assertRaisesRegex(DROP.DropRefused, "drop it on sat-a"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(self.statuses(items), ["blocked"] * 3)


class DropRound13(Repo):
    """Review round 13 of pack sd:9709: an in-place member drop left the dropped commit's ignored files in the
    worktree the kept bundle resumes: a generated file its ignore rule hid turned untracked there, for the next
    builder to use or stage. A member drop now builds the kept items on a new branch and worktree, as a lead drop
    does; the old worktree keeps the dropped item's work and files."""

    def test_a_member_drop_leaves_the_dropped_items_ignored_files_behind(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        self.commit(work, ".gitignore", "gen.cfg\n", f"chore: ignore gen.cfg\n\nCloses: sd:{items[1]}")
        (work / "gen.cfg").write_text("generated\n")
        lead_id = self.next_lead()
        DROP.drop_item(self.db, build, items[1], who="operator")
        again = self.claim()
        new = Path(again["work_path"])
        self.assertEqual([member["item"] for member in build.run(self.db, again["id"])["members"]],
                         [items[0], items[2]])
        self.assertEqual((new, again["branch"]), (work.parent / f"repo-build-{lead_id}", f"build/sd-{lead_id}"))
        self.assertFalse((new / "gen.cfg").exists())
        self.assertEqual(git(new, "status", "--porcelain", "--ignored", "--untracked-files=all"), "")
        self.assertEqual(self.files(new), ["README", "a.txt", "c.txt"])
        self.assertEqual((work / "gen.cfg").read_text(), "generated\n", "the old worktree keeps the dropped work")


class DropRound14(Repo):
    """Review round 14 of pack sd:9709: the new lead's id was predicted (`MAX(id) + 1`), not reserved, so a build
    queued during the split took it, and the refused writes' undo removed that build's branch and worktree; the
    old branch's dropped lead read as still in the bundle (`left`); and the kept bundle's first brief lost the
    drop's finding, written before the new lead's first run."""

    def test_a_build_queued_during_the_split_does_not_take_the_new_lead(self):
        items, ids, run = self.bundle()
        other = self.item(repo=str(self.repo))
        real, taken = DROP.split_branch, list[int]()

        def queued_meanwhile(plan, done):
            taken.extend(row["id"] for row in build.queue(self.db, [other], who="operator"))
            return real(plan, done)

        with mock.patch.object(DROP, "split_branch", side_effect=queued_meanwhile):
            queued = DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertNotIn(taken[0], [entry["id"] for entry in queued[:2]])
        new = Path(run["work_path"]).parent / f"repo-build-{queued[0]['id']}"
        self.assertEqual(self.files(new), ["README", "a.txt", "c.txt"])

    def test_a_refused_drop_releases_its_reserved_lead(self):
        items, ids, run = self.bundle()
        before = self.db.execute("SELECT COUNT(*) FROM assignment").fetchone()[0]
        with mock.patch.object(build, "drop_writes", side_effect=build.BuildRefused("Drop refused: changed")), \
                self.assertRaises(build.BuildRefused):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assignment").fetchone()[0], before)

    def test_the_old_branch_leaves_the_dropped_lead_out(self):
        items, ids, run = self.bundle()
        DROP.drop_item(self.db, build, items[0], who="operator")
        self.assertEqual(build.left(self.db, run["branch"], items), set(items))

    def test_the_kept_bundles_first_brief_carries_the_drop_finding(self):
        items, ids, run = self.bundle()
        DROP.drop_item(self.db, build, items[1], who="operator")
        again = self.claim()
        # The claim opens the new lead's first run a while after the drop: at test speed both share one second.
        self.db.execute("UPDATE runner_run SET created_at = '2999-01-01T00:00:00+00:00' WHERE id = ?", (again["id"],))
        supervisor = build_run.Supervisor(self.db, again["id"], tools=mock.Mock(), log_root=self.root / "logs",
                                          cited=mock.Mock(), environ={})
        findings = supervisor.brief()["findings"]
        self.assertIn("already holds the commits of an earlier run", findings)
        self.assertIn(f"sd:{items[1]} left this bundle", findings)


class DropRound15(Repo):
    """Review round 15 of pack sd:9709: a commit citing only an item outside the bundle read as the kept items'
    and would close that item at merge; and the kept bundle's split work is on the new lead's branch alone, so a
    claim after that lead left the queue started a member on its own fresh branch."""

    def test_a_commit_citing_only_an_item_outside_the_bundle_refuses(self):
        items, ids, run = self.bundle()
        stranger = self.item(repo=str(self.repo))
        work = Path(run["work_path"])
        self.commit(work, "d.txt", "other\n", f"fix: other\n\nCloses: sd:{stranger}")
        old = git(work, "rev-parse", "HEAD")
        with self.assertRaisesRegex(DROP.DropRefused, f"sd:{stranger}"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(git(work, "rev-parse", "HEAD"), old)
        self.assertEqual(self.statuses(items), ["blocked"] * 3)

    def test_a_kept_member_whose_lead_left_the_queue_does_not_start_alone(self):
        items, ids, run = self.bundle()
        queued = DROP.drop_item(self.db, build, items[2], who="operator")
        writes.transition(self.db, items[0], "planning", who="operator")
        for _ in range(4):  # the first claim refuses the planning lead and ends empty; the next ones go on
            claimed = self.claim()
            if claimed is None:
                continue
            self.assertNotIn(items[1], [member["item"] for member in build.run(self.db, claimed["id"])["members"]])
            build.write(self.db, claimed["id"], supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START)
            build.end(self.db, claimed["id"], "blocked", "fixture")
            build.reap(self.db, probe=Probe(alive=False, group=False), kill=Kill(self.db))
        member = self.db.execute("SELECT status, result FROM assignment WHERE id = ?", (queued[1]["id"],)).fetchone()
        self.assertEqual(member["status"], "blocked")
        self.assertIn(f"build/sd-{queued[0]['id']}", member["result"])


class DropRound16(Repo):
    """Review round 16 of pack sd:9709: a Retry after the kept bundle's first run moved `queued_at` past the
    drop's finding, so the next brief lost it; and a lane moved after the drop sent the new lead, which has no run
    of its own, to a host that holds none of its work."""

    def test_a_retry_after_the_first_run_keeps_the_drop_finding(self):
        items, ids, run = self.bundle()
        lead = DROP.drop_item(self.db, build, items[1], who="operator")[0]["id"]
        # The drop's notes are older than the first claim, as on a real machine.
        self.db.execute("UPDATE note SET timestamp = '2000-01-01T00:00:00+00:00' WHERE item IN (?, ?)",
                        (items[0], items[2]))
        first = self.claim()
        build.write(self.db, first["id"], supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START)
        build.end(self.db, first["id"], "blocked", "fixture")
        build.reap(self.db, probe=Probe(alive=False, group=False), kill=Kill(self.db))
        build.retry(self.db, lead, who="operator")
        again = self.claim()
        self.assertEqual(again["assignment"], lead)
        supervisor = build_run.Supervisor(self.db, again["id"], tools=mock.Mock(), log_root=self.root / "logs",
                                          cited=mock.Mock(), environ={})
        self.assertIn(f"sd:{items[1]} left this bundle", supervisor.brief()["findings"])

    def test_a_lane_moved_after_the_drop_does_not_claim_the_kept_bundle_elsewhere(self):
        items, ids, run = self.bundle()
        self.db.execute("UPDATE runner_run SET owner = 'fixture@sat-a' WHERE id = ?", (run["id"],))
        self.db.execute("UPDATE repo SET lane_host = 'sat-a' WHERE path = ?", (str(self.repo),))
        for host in ("sat-a", "sat-b"):
            record_state(self.db, "heartbeat", key=f"build-dispatch@{host}", body={})
        with mock.patch.multiple(build, lane_hosts=mock.Mock(return_value=frozenset({"sat-a"})),
                                 hub_host=mock.Mock(return_value="hub")):
            lead = DROP.drop_item(self.db, build, items[1], who="operator")[0]["id"]
        self.db.execute("UPDATE repo SET lane_host = 'sat-b' WHERE path = ?", (str(self.repo),))
        with mock.patch.object(build, "hub_host", return_value="hub"):
            for _ in range(3):
                claimed = self.claim(hosts=frozenset({"sat-b"}))
                if claimed is not None:
                    self.assertNotEqual(claimed["assignment"], lead)
        row = self.db.execute("SELECT status, result FROM assignment WHERE id = ?", (lead,)).fetchone()
        self.assertEqual(row["status"], "blocked")
        self.assertIn("sat-a", row["result"])


class DropRound17(Repo):
    """Review round 17 of pack sd:9709, class pass: the new lead's host was kept in `result`, which every writer of
    a queued row's outcome overwrites before the lead's first run: a claim's refusal (`_refuse`), the quiet
    dispatcher's recovery (`_requeue_beat_blocked`, which clears it) and a requeue. The drop's queue note holds it
    now, which no writer changes."""

    def on_sat_a(self, items, run):
        self.db.execute("UPDATE runner_run SET owner = 'fixture@sat-a' WHERE id = ?", (run["id"],))
        self.db.execute("UPDATE repo SET lane_host = 'sat-a' WHERE path = ?", (str(self.repo),))
        for host in ("sat-a", "sat-b"):
            record_state(self.db, "heartbeat", key=f"build-dispatch@{host}", body={})
        with mock.patch.multiple(build, lane_hosts=mock.Mock(return_value=frozenset({"sat-a"})),
                                 hub_host=mock.Mock(return_value="hub")):
            return DROP.drop_item(self.db, build, items[1], who="operator")[0]["id"]

    def test_each_result_writer_keeps_the_kept_host(self):
        writers = {"refused then recovered": "UPDATE assignment SET status = 'queued', result = NULL WHERE id = ?",
                   "a claim's refusal": "UPDATE assignment SET result = 'item 1 refused; not built' WHERE id = ?",
                   "a requeue": "UPDATE assignment SET result = 'requeued by operator' WHERE id = ?"}
        for name, sql in writers.items():
            with self.subTest(name):
                self.setUp()
                items, ids, run = self.bundle()
                lead = self.on_sat_a(items, run)
                self.db.execute(sql, (lead,))
                self.db.execute("UPDATE repo SET lane_host = 'sat-b' WHERE path = ?", (str(self.repo),))
                with mock.patch.object(build, "hub_host", return_value="hub"):
                    claimed = [self.claim(hosts=frozenset({"sat-b"})) for _ in range(3)]
                self.assertNotIn(lead, [one["assignment"] for one in claimed if one])


class DropRound18(Repo):
    """Review round 18 of pack sd:9709: a rollback whose git cleanup failed still released the reserved id, which
    SQLite reuses, so a later build would take the stranded worktree by its branch name; and the host guard ended
    at the new lead's first claim, though a run that never started left the work where it was."""

    def test_a_failed_cleanup_keeps_the_reserved_id(self):
        items, ids, run = self.bundle()
        lead = self.next_lead()
        real = DROP._git

        def stuck(*argv, cwd, check=True):
            if argv[:2] in (("worktree", "remove"), ("branch", "-D")):
                return subprocess.CompletedProcess(["git", *argv], 1, "", "error: example failure")
            return real(*argv, cwd=cwd, check=check)

        with mock.patch.object(build, "drop_writes", side_effect=build.BuildRefused("Drop refused: changed")), \
                mock.patch.object(DROP, "_git", side_effect=stuck), \
                self.assertRaisesRegex(DROP.DropRefused, f"repo-build-{lead}"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM assignment WHERE id = ?", (lead,)).fetchone())
        self.assertNotEqual(self.next_lead(), lead)

    def test_a_retry_after_a_claim_still_builds_only_where_the_work_is(self):
        items, ids, run = self.bundle()
        self.db.execute("UPDATE runner_run SET owner = 'fixture@sat-a' WHERE id = ?", (run["id"],))
        self.db.execute("UPDATE repo SET lane_host = 'sat-a' WHERE path = ?", (str(self.repo),))
        for host in ("sat-a", "sat-b"):
            record_state(self.db, "heartbeat", key=f"build-dispatch@{host}", body={})
        with mock.patch.multiple(build, lane_hosts=mock.Mock(return_value=frozenset({"sat-a"})),
                                 hub_host=mock.Mock(return_value="hub")):
            lead = DROP.drop_item(self.db, build, items[1], who="operator")[0]["id"]
            first = self.claim(hosts=frozenset({"sat-a"}))
            self.assertEqual(first["assignment"], lead)
            # The run ends before its supervisor ever starts: nothing was built.
            build.end(self.db, first["id"], "blocked", "supervisor did not start")
            build.reap(self.db, probe=Probe(alive=False, group=False), kill=Kill(self.db))
            build.retry(self.db, lead, who="operator")
        self.db.execute("UPDATE repo SET lane_host = 'sat-b' WHERE path = ?", (str(self.repo),))
        with mock.patch.object(build, "hub_host", return_value="hub"):
            claimed = [self.claim(hosts=frozenset({"sat-b"})) for _ in range(3)]
        self.assertNotIn(lead, [one["assignment"] for one in claimed if one])


class DropRound19(Repo):
    """Review round 19 of pack sd:9709: a kept member moved to another repository passed the lead check, as its
    lead was still queued, and then led a fresh build there without the prepared commits."""

    def test_a_kept_member_moved_to_another_repository_does_not_lead_there(self):
        items, ids, run = self.bundle()
        queued = DROP.drop_item(self.db, build, items[2], who="operator")
        other = self.root / "other"
        upsert_repo(self.db, str(other), remote="https://example.test/other.git", managed=1)
        # The member, moved while it waits, sorts first so the claim picks its new repository.
        self.db.execute("UPDATE item SET repo = ?, priority = 0 WHERE id = ?", (str(other), items[1]))
        claimed = self.claim()
        # Since round 21 the kept lead holds too, so the dropped item's own build may start; the member does not.
        self.assertNotEqual((claimed or {}).get("assignment"), queued[1]["id"], claimed)
        member = self.db.execute("SELECT status, result FROM assignment WHERE id = ?", (queued[1]["id"],)).fetchone()
        # Since round 20 it leaves the bundle, as it no longer shares its lead's repository.
        self.assertEqual(member["status"], "cancelled")
        self.assertTrue(member["result"].startswith(runner.ALONE), member["result"])


class DropRound20(Repo):
    """Review round 20 of pack sd:9709, a class pass: the claim read a bundle row's repository from its item as it
    reads now, though the bundle's work sits on a branch in the repository of the run that made it. Each row of a
    bundle that has work (a retry, a drop's kept bundle) is checked against that repository."""

    def test_a_kept_lead_moved_to_another_repository_is_not_built_there(self):
        items, ids, run = self.bundle()
        queued = DROP.drop_item(self.db, build, items[2], who="operator")
        self.move(items[0])
        claimed = self.claim()  # the dropped item's own fresh build may start; the kept bundle does not
        self.assertNotEqual((claimed or {}).get("assignment"), queued[0]["id"])
        lead = self.db.execute("SELECT status, result FROM assignment WHERE id = ?", (queued[0]["id"],)).fetchone()
        self.assertEqual(lead["status"], "blocked")
        self.assertIn(str(self.repo), lead["result"])

    def test_a_retried_lead_moved_to_another_repository_is_not_built_there(self):
        items, ids, run = self.bundle()
        build.retry(self.db, ids[0], who="operator")
        self.move(items[0])
        self.assertIsNone(self.claim())
        lead = self.db.execute("SELECT status, result FROM assignment WHERE id = ?", (ids[0],)).fetchone()
        self.assertEqual(lead["status"], "blocked")
        self.assertIn(str(self.repo), lead["result"])

    def test_a_kept_member_moved_to_another_repository_leaves_the_bundle(self):
        items, ids, run = self.bundle()
        queued = DROP.drop_item(self.db, build, items[2], who="operator")
        self.move(items[1])
        claimed = [self.claim() for _ in range(3)]
        member = self.db.execute("SELECT status, result FROM assignment WHERE id = ?", (queued[1]["id"],)).fetchone()
        self.assertEqual(member["status"], "cancelled")
        self.assertTrue(member["result"].startswith(runner.ALONE), member["result"])
        # Its commits stay on the kept branch: since round 21 the kept lead does not build with them.
        self.assertNotIn(queued[0]["id"], [one["assignment"] for one in claimed if one])


class DropRound21(Repo):
    """Review round 21 of pack sd:9709, a class pass: a member that leaves a bundle with work between its queue and
    its claim (moved, cancelled, refused at a claim) leaves its commits on the lead's branch. The lead must not
    build with them; a done member's may stay, as the merge closes nothing open."""

    def kept(self):
        """A drop of the third item: (items, the kept bundle's queued rows, lead first)."""
        items, ids, run = self.bundle()
        queued = DROP.drop_item(self.db, build, items[2], who="operator")
        return items, queued[:2]

    def claims(self):
        return [one["assignment"] for one in (self.claim() for _ in range(3)) if one]

    def blocked_naming(self, lead, item):
        row = self.db.execute("SELECT status, result FROM assignment WHERE id = ?", (lead,)).fetchone()
        self.assertEqual(row["status"], "blocked", row["result"])
        self.assertIn(f"sd:{item} left the bundle", row["result"])

    def test_a_kept_member_cancelled_while_queued_holds_the_lead(self):
        items, (lead, member) = self.kept()
        current = runner.queue_state(self.db, member["id"])
        runner.request_cancel(self.db, member["id"], expected_revision=current["revision"], who="operator")
        self.assertNotIn(lead["id"], self.claims())
        self.blocked_naming(lead["id"], items[1])

    def test_a_kept_member_moved_to_planning_holds_the_lead(self):
        items, (lead, member) = self.kept()
        self.db.execute("UPDATE item SET status = 'planning' WHERE id = ?", (items[1],))
        self.assertNotIn(lead["id"], self.claims())
        self.blocked_naming(lead["id"], items[1])

    def test_a_kept_member_moved_away_holds_the_lead_and_build_takes_it_fresh(self):
        items, (lead, member) = self.kept()
        self.move(items[1])
        self.assertNotIn(lead["id"], self.claims())
        self.blocked_naming(lead["id"], items[1])
        fresh = build.queue_again(self.db, [items[0]], who="operator")
        self.assertNotEqual(fresh[0]["id"], lead["id"], "Build takes the kept item fresh, on a new branch")

    def test_a_kept_member_whose_repository_is_cleared_holds_the_lead(self):
        """Round 22: the claim reads only rows with a repository, so a kept member whose item lost its repository
        stayed queued, out of sight of the stray check, and the lead shipped its commits."""
        items, (lead, member) = self.kept()
        self.db.execute("UPDATE item SET repo = NULL WHERE id = ?", (items[1],))
        self.assertNotIn(lead["id"], self.claims())
        self.blocked_naming(lead["id"], items[1])
        row = self.db.execute("SELECT status, result FROM assignment WHERE id = ?", (member["id"],)).fetchone()
        self.assertEqual(row["status"], "blocked", "no claim would take it queued with no repository")
        self.assertIn("no repository", row["result"])

    def test_a_kept_member_moved_to_another_hosts_repository_holds_the_lead(self):
        """Round 23: the claim drops a row whose lane runs on another host before it checks moves, so a member moved
        to a repository a live satellite serves stayed queued, out of sight, and the lead shipped its commits."""
        items, (lead, member) = self.kept()
        other = self.move(items[1])
        self.db.execute("UPDATE repo SET lane_host = 'sat-b' WHERE path = ?", (str(other),))
        with mock.patch.object(build, "dispatcher_gone", return_value=None), \
                mock.patch.object(build, "dispatcher_stuck", return_value=None):
            self.assertNotIn(lead["id"], self.claims())
        self.blocked_naming(lead["id"], items[1])
        row = self.db.execute("SELECT status, result FROM assignment WHERE id = ?", (member["id"],)).fetchone()
        self.assertEqual(row["status"], "blocked", row["result"])
        self.assertIn(str(other), row["result"])

    def test_a_kept_member_done_lets_the_lead_build(self):
        items, (lead, member) = self.kept()
        self.db.execute("UPDATE item SET status = 'done' WHERE id = ?", (items[1],))
        self.assertIn(lead["id"], self.claims())

    def test_a_retried_member_moved_away_is_left_out_for_the_split_check(self):
        items, ids, run = self.bundle()
        build.retry(self.db, ids[0], who="operator")
        self.move(items[1])
        claimed = self.claim()
        self.assertEqual(claimed["assignment"], ids[0])
        self.assertEqual(build.left_out(self.db, ids[0]), [(items[1], 1)])
        self.assertEqual(build.split(self.db, ids[0]), "unread", "the run's first step reads the branch")

    def test_a_retried_member_cancelled_on_its_own_is_left_out_for_the_split_check(self):
        """The sd:9222 lone cancel: the lead builds without it, and its next run reads the branch for its work."""
        items, ids, run = self.bundle()
        build.retry(self.db, ids[0], who="operator")
        state = operations.assignment_state(self.db, ids[1])
        operations.cancel_assignment(self.db, ids[1], expected_revision=state["revision"], who="operator")
        self.assertEqual(self.claim()["assignment"], ids[0])
        self.assertEqual(build.left_out(self.db, ids[0]), [(items[1], 1)])
        self.assertEqual(build.split(self.db, ids[0]), "unread")

    def test_a_retried_member_cancelled_while_queued_holds_the_lead(self):
        items, ids, run = self.bundle()
        build.retry(self.db, ids[0], who="operator")
        current = runner.queue_state(self.db, ids[1])
        runner.request_cancel(self.db, ids[1], expected_revision=current["revision"], who="operator")
        self.assertNotIn(ids[0], self.claims())
        self.blocked_naming(ids[0], items[1])
