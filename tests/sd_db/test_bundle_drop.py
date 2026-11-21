"""Bundle drop (pack sd:9709): one item out of an ended bundle, its lead included, on real git branches.

The revert recipe it replaces failed twice on one bundle (sd:9651): a subset Retry must name the lead, and a
reverted commit keeps its `Closes: sd:N`, so the merge would close the dropped item undone.
"""
import importlib
import subprocess
import sys
from pathlib import Path
from unittest import mock

from sd_db import build, runner, writes
from sd_db.writes import create_assignment, upsert_repo

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

    def files(self, work):
        return sorted(git(work, "ls-files").split())

    def closes(self, work):
        return git(work, "log", "--format=%B", "origin/main..HEAD")

class Drop(Repo):
    def test_dropping_a_member_keeps_the_others_commits_alone_and_builds_it_alone(self):
        items, (lead, second, third), run = self.bundle()
        work = Path(run["work_path"])
        old = git(work, "rev-parse", "HEAD")
        queued = DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(self.files(work), ["README", "a.txt", "c.txt"])
        self.assertEqual((work / "a.txt").read_text(), "lead fixed\n")
        self.assertNotIn(f"Closes: sd:{items[1]}", self.closes(work))
        self.assertEqual(git(work, "rev-list", "--count", "origin/main..HEAD"), "3")
        self.assertEqual(git(self.repo, "rev-parse", f"{run['branch']}-drop-sd-{items[1]}"), old)
        self.assertEqual([(entry["id"], entry["item"]) for entry in queued[:2]], [(lead, items[0]), (third, items[2])])
        self.assertEqual(queued[2]["item"], items[1])
        self.assertNotIn(queued[2]["id"], (lead, second, third))
        self.assertEqual(runner.queue_state(self.db, second)["status"], "cancelled")
        again = self.claim()
        self.assertEqual([member["item"] for member in build.run(self.db, again["id"])["members"]],
                         [items[0], items[2]])
        self.assertEqual((again["work_path"], again["branch"]), (run["work_path"], run["branch"]))

    def test_dropping_the_lead_makes_a_kept_item_the_lead_on_a_fresh_branch(self):
        items, (lead, second, third), run = self.bundle()
        work = Path(run["work_path"])
        old = git(work, "rev-parse", "HEAD")
        DROP.drop_item(self.db, build, items[0], who="operator")
        self.assertEqual(git(work, "rev-parse", "HEAD"), old, "the old branch keeps the dropped item's work")
        again = self.claim()
        self.assertEqual([member["id"] for member in build.run(self.db, again["id"])["members"]], [second, third])
        new = Path(again["work_path"])
        self.assertEqual((new, again["branch"]), (work.parent / f"repo-build-{second}", f"build/sd-{second}"))
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
        with mock.patch.object(build, "drop_writes", side_effect=build.BuildRefused("Drop refused: changed")), \
                self.assertRaisesRegex(build.BuildRefused, "changed"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual((git(work, "rev-parse", "HEAD"), git(work, "branch", "--show-current")),
                         (old, run["branch"]))
        self.assertEqual(git(self.repo, "branch", "--list", f"{run['branch']}-drop-*"), "")
        with mock.patch.object(build, "drop_writes", side_effect=build.BuildRefused("Drop refused: changed")), \
                self.assertRaises(build.BuildRefused):
            DROP.drop_item(self.db, build, items[0], who="operator")
        self.assertFalse((work.parent / f"repo-build-{ids[1]}").exists())
        self.assertEqual(git(self.repo, "branch", "--list", f"build/sd-{ids[1]}"), "")

    def test_a_bundle_whose_branch_is_on_origin_refuses_a_member_drop(self):
        items, ids, run = self.bundle()
        git(Path(run["work_path"]), "push", "-q", "origin", f"HEAD:refs/heads/{run['branch']}")
        with self.assertRaisesRegex(DROP.DropRefused, "is on origin"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(self.statuses(items), ["blocked"] * 3)

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
        DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(self.files(work), ["README", "a.txt", "c.txt", "d.txt"])

    def test_an_ignored_file_the_base_tracks_refuses_and_stays(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        exclude = Path(git(self.repo, "rev-parse", "--git-common-dir"))
        exclude = (exclude if exclude.is_absolute() else self.repo / exclude) / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text("gen.txt\n")
        (work / "gen.txt").write_text("local\n")
        self.main_moves("gen.txt", "tracked on main\n")
        with self.assertRaisesRegex(DROP.DropRefused, "ignored .*gen.txt"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual((work / "gen.txt").read_text(), "local\n")

    def failing(self, *words):
        """`_git` that fails each call whose argv holds every one of `words`."""
        real = DROP._git

        def fake(*argv, cwd, check=True):
            if all(word in argv for word in words):
                if check:
                    raise DROP.DropRefused(f"git {' '.join(argv)} failed")
                return subprocess.CompletedProcess(argv, 1, "", "failed")
            return real(*argv, cwd=cwd, check=check)
        return mock.patch.object(DROP, "_git", side_effect=fake)

    def test_a_failed_checkout_leaves_no_backup_and_a_retry_drops(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        old = git(work, "rev-parse", "HEAD")
        with self.failing("checkout", "-B"), self.assertRaises(DROP.DropRefused):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(git(self.repo, "branch", "--list", f"{run['branch']}-drop-*"), "")
        self.assertEqual(git(work, "rev-parse", "HEAD"), old)
        DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(self.files(work), ["README", "a.txt", "c.txt"])

    def test_a_failed_restore_keeps_the_backup_and_says_so(self):
        items, ids, run = self.bundle()
        with mock.patch.object(build, "drop_writes", side_effect=build.BuildRefused("Drop refused: changed")), \
                self.failing("checkout", "-f"), self.assertRaisesRegex(DROP.DropRefused, "kept on .*-drop-sd-"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertNotEqual(git(self.repo, "branch", "--list", f"{run['branch']}-drop-*"), "")

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


    def test_a_leading_space_ignored_file_the_base_tracks_refuses(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        exclude = Path(git(self.repo, "rev-parse", "--git-common-dir"))
        exclude = (exclude if exclude.is_absolute() else self.repo / exclude) / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text("*local.txt\n")
        (work / " local.txt").write_text("local\n")
        self.main_moves(" a.txt", "first\n")
        self.main_moves(" local.txt", "tracked on main\n")
        with self.assertRaisesRegex(DROP.DropRefused, "ignored .* local.txt"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual((work / " local.txt").read_text(), "local\n")

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
        self.assertEqual(git(self.repo, "branch", "--list", f"{run['branch']}-drop-*"), "")

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
        self.assertEqual(git(self.repo, "branch", "--list", f"{run['branch']}-drop-*"), "")


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
        DROP.drop_item(self.db, build, items[1], who="operator")
        self.assertEqual(self.files(Path(run["work_path"])), ["README", "a.txt", "c.txt"])

    def test_a_home_key_repo_drops_the_lead(self):
        self.home_is_root()
        items, (lead, second, third), run = self.bundle()
        DROP.drop_item(self.db, build, items[0], who="operator")
        self.assertEqual(self.files(Path(run["work_path"]).parent / f"repo-build-{second}"),
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
    """Review round 5 of pack sd:9709, class pass: a git exit code read as an answer. Only `ls-remote` exit 2 says a
    ref is absent, only `rev-parse -q --verify` exit 1 says a ref is unset; any other failure refuses unwritten."""

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
        self.assertEqual(git(self.repo, "branch", "--list", f"{run['branch']}-drop-*"), "")
        self.assertEqual(self.statuses(items), ["blocked"] * 3)

    def test_a_failed_ls_remote_refuses_an_in_place_drop(self):
        items, ids, run = self.bundle()
        old = git(Path(run["work_path"]), "rev-parse", "HEAD")
        with self.failing("ls-remote"), self.assertRaisesRegex(DROP.DropRefused, "ls-remote"):
            DROP.drop_item(self.db, build, items[1], who="operator")
        self.assert_unwritten(items, run, old)

    def test_a_failed_rev_parse_refuses_the_drop(self):
        items, ids, run = self.bundle()
        old = git(Path(run["work_path"]), "rev-parse", "HEAD")
        for item in (items[1], items[0]):
            with self.subTest(item=item), self.failing("rev-parse", "-q", "--verify"), \
                    self.assertRaisesRegex(DROP.DropRefused, "rev-parse"):
                DROP.drop_item(self.db, build, item, who="operator")
            self.assert_unwritten(items, run, old)
            self.assertFalse((Path(run["work_path"]).parent / f"repo-build-{ids[1]}").exists())


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
        self.assertEqual(git(self.repo, "branch", "--list", f"{run['branch']}-drop-*"), "")
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
        DROP.drop_item(self.db, build, items[2], who="operator")
        # The drop's split proved the left-out member's work off the branch, so its next run must not hold it.
        self.assertIn(build.split(self.db, lead), ("", "clear"))
        self.assertEqual(self.files(Path(run["work_path"])), ["README", "a.txt"])

    def test_an_interrupt_after_the_writes_committed_keeps_the_split(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
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
        DROP.drop_item(self.db, build, items[1], who="operator")
        work = Path(run["work_path"])
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

    def test_a_kept_member_moved_during_the_split_refuses_the_writes(self):
        items, ids, run = self.bundle()
        work = Path(run["work_path"])
        old = git(work, "rev-parse", "HEAD")
        real = DROP.split_branch

        def moved(plan):
            done = real(plan)
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
