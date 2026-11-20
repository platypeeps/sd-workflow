"""Bundle drop (pack sd:9709): one item out of an ended bundle, its lead included, on real git branches.

The revert recipe it replaces failed twice on one bundle (sd:9651): a subset Retry must name the lead, and a
reverted commit keeps its `Closes: sd:N`, so the merge would close the dropped item undone.
"""
import importlib
import subprocess
import sys
from pathlib import Path
from unittest import mock

from sd_db import build, runner
from sd_db.writes import upsert_repo

from .test_build import START, Case, Kill, Probe

BIN = Path(__file__).resolve().parents[2] / "bin"
with mock.patch("sys.path", [str(BIN), *sys.path]):
    DROP = importlib.import_module("sd_bundle_drop")

IDENTITY = {"GIT_AUTHOR_NAME": "operator", "GIT_AUTHOR_EMAIL": "operator@example.test",
            "GIT_COMMITTER_NAME": "operator", "GIT_COMMITTER_EMAIL": "operator@example.test",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *argv):
    return subprocess.run(["git", "-C", str(cwd), *argv], check=True, capture_output=True, text=True).stdout.strip()


class Drop(Case):
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

    def bundle(self):
        """Three items built on one branch, ended blocked: the lead's commit, the second's, an uncited fix of the
        lead's file, the third's. (items, assignment ids, run)."""
        items = [self.item(repo=str(self.repo)) for _ in range(3)]
        build.queue(self.db, items, who="operator")
        run = self.claim()
        work = Path(run["work_path"])
        git(self.repo, "worktree", "add", "-q", "-b", run["branch"], str(work), "origin/main")
        self.commit(work, "a.txt", "lead\n", f"feat: lead\n\nCloses: sd:{items[0]}")
        self.commit(work, "b.txt", "second\n", f"feat: second\n\nCloses: sd:{items[1]}")
        self.commit(work, "a.txt", "lead fixed\n", "fix: review round")
        self.commit(work, "c.txt", "third\n", f"feat: third\n\nCloses: sd:{items[2]}")
        build.write(self.db, run["id"], supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START)
        build.end(self.db, run["id"], "blocked", "3 review rounds left blockers: a.txt:1 lead")
        build.reap(self.db, probe=Probe(alive=False, group=False), kill=Kill(self.db))
        ids = [member["id"] for member in build.run(self.db, run["id"])["members"]]
        return items, ids, run

    def files(self, work):
        return sorted(git(work, "ls-files").split())

    def closes(self, work):
        return git(work, "log", "--format=%B", "origin/main..HEAD")

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
        with self.assertRaisesRegex(DROP.DropRefused, "paths of both sides"):
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
