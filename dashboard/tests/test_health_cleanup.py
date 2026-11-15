"""Health's cleanups (sd:3393): prune registrations, delete merged branches and remove build output.

What this slice promises: each write reads its subject again and refuses a revision that differs from the page's,
including a revision taken from another row (409, nothing changed). Then git's own guard holds for each step: a locked
registration is kept by `worktree prune`, and `branch -d` refuses a branch that stopped being merged. Build output goes
only from a worktree still at its head, still merged and clean, read again just before its folders go. A step whose
result is not known answers 502 with `outcome: unknown` and what did land; the next read finds what is left and a
second run finishes it.

Every test runs real git against fixture repositories under a temporary folder, in an isolated environment: a
synthetic HOME, no global or system git config and no inherited GIT_* variable. No test touches a real checkout.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import fleet, health_cleanup, health_collectors, health_screen
from sd_dashboard.health_collectors import Walk
from sd_db import sessions, workflow
from sd_db.ship import this_host
from support import ScreenCase
from test_workflow_actions import BrowserSession

FIXTURE_GIT = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.test", "GIT_COMMITTER_NAME": "Test",
               "GIT_COMMITTER_EMAIL": "test@example.test", "GIT_AUTHOR_DATE": "2026-09-05T10:00:00Z",
               "GIT_COMMITTER_DATE": "2026-09-05T10:00:00Z"}


def git(*args) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **FIXTURE_GIT}).stdout.strip()


class Isolated:
    """A temporary root with REPO_ROOT under it, and an environment that reads no git config of the machine."""

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="cleanup-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "home").mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(HOME=str(self.root / "home"), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   REPO_ROOT=str(self.root / "repos"))
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cloned(self, name: str) -> Path:
        """A repository on main with origin/HEAD at its tip, and build output folders ignored."""
        path = self.root / "repos" / name
        git("init", "-q", "-b", "main", str(path))
        git("-C", str(path), "commit", "-q", "--allow-empty", "-m", "first")
        git("-C", str(path), "update-ref", "refs/remotes/origin/main", "HEAD")
        git("-C", str(path), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        (path / ".git" / "info" / "exclude").write_text("target\nnode_modules\n.venv\n")
        return path

    def write(self, row: str, revision: str, connection=None):
        return health_cleanup.request({"row": row, "revision": revision})(connection or self.connection)


class Prune(Isolated, ScreenCase):
    def gone(self, repo: Path, *names: str) -> None:
        for name in names:
            tree = self.root / f"wt-{name}"
            git("-C", str(repo), "worktree", "add", "-q", "-b", f"feat/{name}", str(tree))
            shutil.rmtree(tree)

    def row(self, name: str) -> dict:
        """The page's row for the checkout, from the real fleet child over the fixture root."""
        rows = health_screen._worktree_rows(fleet.collect("sessions"))
        return next(row for row in rows if row["id"] == f"gone:{name}")

    def registered(self, repo: Path) -> list[str]:
        return sorted(entry.name for entry in (repo / ".git" / "worktrees").iterdir())

    def test_prune_removes_the_registrations_the_page_showed(self):
        alpha = self.cloned("alpha")
        self.gone(alpha, "a", "b")
        row = self.row("alpha")
        self.assertEqual(self.write(row["id"], row["revision"]),
                         {"ok": True, "row": "gone:alpha", "done": ["wt-a", "wt-b"], "refused": []})
        self.assertFalse((alpha / ".git" / "worktrees").exists() and self.registered(alpha))

    def test_a_revision_from_another_checkout_is_refused_and_nothing_is_pruned(self):
        alpha, beta = self.cloned("alpha"), self.cloned("beta")
        self.gone(alpha, "a")
        self.gone(beta, "b")
        with self.assertRaises(workflow.StaleItem):
            self.write("gone:alpha", self.row("beta")["revision"])
        self.assertEqual(self.registered(alpha), ["wt-a"])

    def test_a_registration_gone_since_the_read_makes_the_revision_stale(self):
        alpha = self.cloned("alpha")
        self.gone(alpha, "a")
        row = self.row("alpha")
        self.gone(alpha, "b")
        with self.assertRaises(workflow.StaleItem):
            self.write(row["id"], row["revision"])
        self.assertEqual(self.registered(alpha), ["wt-a", "wt-b"])

    def test_a_locked_registration_is_kept_by_git_and_the_next_read_lists_it(self):
        alpha = self.cloned("alpha")
        self.gone(alpha, "a", "b")
        git("-C", str(alpha), "worktree", "lock", "--reason", "held", "wt-b")
        row = self.row("alpha")
        answer = self.write(row["id"], row["revision"])
        self.assertEqual((answer["done"], [x["name"] for x in answer["refused"]]), (["wt-a"], ["wt-b"]))
        again = self.row("alpha")
        self.assertEqual((again["facts"]["Registered"], again["list"]), ("1", [f"{self.root}/wt-b (feat/b)"]))

    def test_a_prune_where_git_keeps_every_registration_wrote_nothing_and_is_refused(self):
        alpha = self.cloned("alpha")
        self.gone(alpha, "a")
        git("-C", str(alpha), "worktree", "lock", "--reason", "held", "wt-a")
        row = self.row("alpha")
        with self.assertRaisesRegex(ValueError, r"^Nothing was pruned: wt-a \(git kept it\)$"):
            self.write(row["id"], row["revision"])
        self.assertEqual(self.registered(alpha), ["wt-a"])

    def test_a_prune_git_did_not_answer_is_unknown_not_a_refusal(self):
        alpha = self.cloned("alpha")
        self.gone(alpha, "a")
        row = self.row("alpha")
        real = Walk.git
        with patch.object(Walk, "git", lambda walk, path, *args: None if args[:1] == ("worktree",) else real(walk, path, *args)):
            status, answer = self.write(row["id"], row["revision"])
        self.assertEqual((status, answer["outcome"], answer["done"]), (502, "unknown", []))
        self.assertEqual(answer["refused"], [{"name": "wt-a", "why": "not known"}])

    def test_registrations_not_read_after_the_prune_are_unknown(self):
        alpha = self.cloned("alpha")
        self.gone(alpha, "a")
        row = self.row("alpha")
        real, reads = fleet.read_worktrees, []

        def second_fails(group, path):
            reads.append(path)
            if len(reads) == 2:
                raise OSError("read failed")
            return real(group, path)

        with patch.object(fleet, "read_worktrees", second_fails):
            status, answer = self.write(row["id"], row["revision"])
        self.assertEqual((status, answer["outcome"]), (502, "unknown"))
        self.assertIn("not read after it", answer["error"])

    def test_an_unreadable_registration_refuses_the_prune(self):
        alpha = self.cloned("alpha")
        self.gone(alpha, "a", "b")
        (alpha / ".git" / "worktrees" / "wt-b" / "gitdir").unlink()
        row = self.row("alpha")
        self.assertEqual(row["unreadable"], 1)
        with self.assertRaisesRegex(ValueError, "could not be read.*Nothing was pruned"):
            self.write(row["id"], row["revision"])
        self.assertEqual(self.registered(alpha), ["wt-a", "wt-b"])

    def test_a_checkout_outside_the_fleet_root_is_refused(self):
        with self.assertRaisesRegex(ValueError, "is not a checkout under"):
            self.write("gone:../elsewhere", "0" * 64)


class DeleteMerged(Isolated, ScreenCase):
    def setUp(self):
        super().setUp()
        self.alpha = self.cloned("alpha")
        for name in ("one", "two"):
            git("-C", str(self.alpha), "branch", f"feat/{name}")
        self.repo(str(self.alpha))

    def row(self, path: Path) -> dict:
        rows = health_screen._branch_rows(health_collectors.branch_scan(self.connection, repo_paths=[str(path)]))
        return next(row for row in rows if row["id"] == f"br:{path}")

    def branches(self) -> list[str]:
        return git("-C", str(self.alpha), "for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()

    def test_delete_removes_the_merged_branches_the_page_showed(self):
        row = self.row(self.alpha)
        self.assertEqual(row["branches"], ["feat/one", "feat/two"])
        self.assertEqual(self.write(row["id"], row["revision"]),
                         {"ok": True, "row": f"br:{self.alpha}", "done": ["feat/one", "feat/two"], "refused": []})
        self.assertEqual(self.branches(), ["main"])

    def test_a_tag_named_like_a_branch_deletes_that_branch_and_never_a_heads_branch(self):
        # With a tag feat/one, %(refname:short) prints heads/feat/one; the delete must still name refs/heads/feat/one
        # and leave the distinct, unmerged branch heads/feat/one alone.
        repo = str(self.alpha)
        git("-C", repo, "tag", "feat/one")
        side = git("-C", repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "unmerged")
        git("-C", repo, "update-ref", "refs/heads/heads/feat/one", side)
        row = self.row(self.alpha)
        self.assertEqual(row["branches"], ["feat/one", "feat/two"])
        self.assertEqual(self.write(row["id"], row["revision"]),
                         {"ok": True, "row": f"br:{self.alpha}", "done": ["feat/one", "feat/two"], "refused": []})
        self.assertEqual(git("-C", repo, "for-each-ref", "--format=%(refname)", "refs/heads/").splitlines(),
                         ["refs/heads/heads/feat/one", "refs/heads/main"])

    def test_a_tip_moved_since_the_read_is_stale_and_nothing_is_deleted(self):
        row = self.row(self.alpha)
        git("-C", str(self.alpha), "commit", "-q", "--allow-empty", "-m", "main moves")
        git("-C", str(self.alpha), "update-ref", "refs/remotes/origin/main", "HEAD")
        git("-C", str(self.alpha), "branch", "-f", "feat/one", "HEAD")
        with self.assertRaises(workflow.StaleItem):
            self.write(row["id"], row["revision"])
        self.assertEqual(self.branches(), ["feat/one", "feat/two", "main"])

    def test_a_revision_from_another_repository_is_refused(self):
        # beta has the same branches at the same tips: only the repository tells the two rows apart.
        beta = self.cloned("beta")
        for name in ("one", "two"):
            git("-C", str(beta), "branch", f"feat/{name}")
        self.repo(str(beta))
        with self.assertRaises(workflow.StaleItem):
            self.write(f"br:{self.alpha}", self.row(beta)["revision"])
        self.assertEqual(self.branches(), ["feat/one", "feat/two", "main"])

    def test_git_refuses_a_branch_that_stopped_being_merged_after_the_check(self):
        row = self.row(self.alpha)
        real = health_collectors.merged_branches

        def then_unmerged(walk, path):
            found = real(walk, path)
            # Between the server's check and the delete, feat/two gains a commit no other branch has.
            git("-C", str(self.alpha), "checkout", "-q", "feat/two")
            git("-C", str(self.alpha), "commit", "-q", "--allow-empty", "-m", "unmerged")
            git("-C", str(self.alpha), "checkout", "-q", "main")
            return found

        with patch.object(health_collectors, "merged_branches", then_unmerged):
            answer = self.write(row["id"], row["revision"])
        self.assertEqual(answer["done"], ["feat/one"])
        self.assertEqual([x["name"] for x in answer["refused"]], ["feat/two"])
        self.assertIn("not fully merged", answer["refused"][0]["why"])
        self.assertEqual(self.branches(), ["feat/two", "main"])

    def test_a_branch_not_read_after_its_delete_is_unknown_and_names_what_landed(self):
        row = self.row(self.alpha)
        real = Walk.git

        def blind(walk, path, *args):
            return None if args[:1] == ("rev-parse",) and args[-1].endswith("feat/two") else real(walk, path, *args)

        with patch.object(Walk, "git", blind):
            status, answer = self.write(row["id"], row["revision"])
        self.assertEqual((status, answer["outcome"], answer["done"]), (502, "unknown", ["feat/one"]))

    def test_an_unregistered_repository_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not a registered repository"):
            self.write(f"br:{self.root}/nowhere", "0" * 64)


class RemoveBuildOutput(Isolated, ScreenCase):
    def setUp(self):
        super().setUp()
        self.alpha = self.cloned("alpha")
        self.repo(str(self.alpha))
        # No real `lsof`: each test names the working folders it reads.
        self.cwds = patch.object(health_collectors, "_cwds", return_value=[])
        self.cwds.start()
        self.addCleanup(self.cwds.stop)

    def tree(self, name: str, *dirs: str) -> Path:
        tree = self.root / f"wt-{name}"
        git("-C", str(self.alpha), "worktree", "add", "-q", "-b", f"feat/{name}", str(tree))
        for folder in dirs:
            (tree / folder / "deep").mkdir(parents=True)
            (tree / folder / "deep" / "file").write_text("x")
        return tree

    def row(self) -> dict:
        build = health_collectors.build_output(Walk(8, "test"), [str(self.alpha)])
        rows, _ = health_screen._disk_rows({"volumes": [], "storage": [{"root": "/x", "folders": [], "error": "n/a"}],
                                            "refused": [], "config": "/x", "build": build})
        return next(row for row in rows if row["id"] == "build:merged")

    def test_remove_deletes_the_folders_of_merged_clean_worktrees_only(self):
        one, dirty = self.tree("one", "target", "node_modules"), self.tree("dirty", "target")
        (dirty / "notes.txt").write_text("uncommitted")
        row = self.row()
        self.assertEqual((row["remove"], row["kept"]), ([f"{one}/target", f"{one}/node_modules"], [str(dirty)]))
        answer = self.write(row["id"], row["revision"])
        self.assertEqual(answer["done"], [f"{one}/target", f"{one}/node_modules"])
        self.assertEqual(answer["refused"], [{"name": str(dirty), "why": "it has uncommitted changes"}])
        self.assertFalse((one / "target").exists() or (one / "node_modules").exists())
        self.assertTrue((dirty / "target" / "deep" / "file").exists())

    def test_a_worktree_a_process_works_in_keeps_its_build_output(self):
        """pack sd:8818: a merged, clean worktree a build or a test run works in keeps the folders it is using, as
        a merged worktree does; an `lsof` with no answer keeps every one."""
        one, two = self.tree("one", "target"), self.tree("two", "node_modules")
        row = self.row()
        with patch.object(health_collectors, "_cwds", return_value=["/elsewhere", os.path.realpath(two / "src")]):
            answer = self.write(row["id"], row["revision"])
        self.assertEqual(answer["done"], [f"{one}/target"])
        self.assertEqual(answer["refused"], [{"name": str(two), "why": f"a process works in {two}/src"}])
        self.assertTrue((two / "node_modules" / "deep" / "file").exists())
        with patch.object(health_collectors, "_cwds", return_value=None), \
                self.assertRaisesRegex(ValueError, "lsof gave no answer"):
            self.write(row["id"], self.row()["revision"])
        self.assertTrue((two / "node_modules").exists())

    def test_a_status_changed_since_the_read_is_stale_and_nothing_is_removed(self):
        one = self.tree("one", "target")
        row = self.row()
        (one / "new.txt").write_text("since the read")
        with self.assertRaises(workflow.StaleItem):
            self.write(row["id"], row["revision"])
        self.assertTrue((one / "target").exists())

    def test_a_head_moved_after_the_check_keeps_that_worktrees_folders(self):
        one, two = self.tree("one", "target"), self.tree("two", "target")
        row = self.row()
        real = health_collectors.build_output

        def then_moved(walk, keys):
            found = real(walk, keys)
            git("-C", str(two), "commit", "-q", "--allow-empty", "-m", "after the check")
            return found

        with patch.object(health_collectors, "build_output", then_moved):
            answer = self.write(row["id"], row["revision"])
        self.assertEqual(answer["done"], [f"{one}/target"])
        self.assertEqual(answer["refused"], [{"name": str(two), "why": "its head moved since the read"}])
        self.assertTrue((two / "target").exists())

    def test_a_removal_stopped_between_worktrees_is_unknown_and_the_next_run_finishes(self):
        one, two = self.tree("one", "target"), self.tree("two", "target")
        row = self.row()
        real, calls = shutil.rmtree, []

        def stops_second(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, *args, **kwargs)

        with patch.object(health_cleanup.shutil, "rmtree", stops_second):
            status, answer = self.write(row["id"], row["revision"])
        self.assertEqual((status, answer["outcome"], answer["done"]), (502, "unknown", [f"{one}/target"]))
        again = self.row()
        self.assertEqual(again["remove"], [f"{two}/target"])
        self.assertEqual(self.write(again["id"], again["revision"])["done"], [f"{two}/target"])
        self.assertFalse((two / "target").exists())

    def test_a_symlinked_build_folder_is_not_followed(self):
        one = self.tree("one")
        outside = self.root / "outside"
        (outside / "keep").mkdir(parents=True)
        (one / "target").symlink_to(outside)
        row = self.row()
        with self.assertRaisesRegex(ValueError, rf"^Nothing was removed: {one}/target \(not a build output folder\)$"):
            self.write(row["id"], row["revision"])
        self.assertTrue((outside / "keep").is_dir())

    def test_a_check_that_could_not_be_read_is_unknown_not_a_refusal(self):
        one, two = self.tree("one", "target"), self.tree("two", "target")
        row = self.row()
        real_read, real_git, checking = health_collectors.build_output, Walk.git, []

        def then_checking(walk, keys):
            found = real_read(walk, keys)
            checking.append(True)
            return found

        def blind(walk, path, *args):
            return None if checking and args[:1] == ("status",) and Path(path) == two else real_git(walk, path, *args)

        with patch.object(health_collectors, "build_output", then_checking), patch.object(Walk, "git", blind):
            status, answer = self.write(row["id"], row["revision"])
        self.assertEqual((status, answer["outcome"], answer["done"]), (502, "unknown", [f"{one}/target"]))
        self.assertIn("its status was not read", answer["error"])
        self.assertTrue((two / "target").exists())

    def test_a_worktree_a_live_session_holds_keeps_its_build_output(self):
        """pack sd:6024: a session working in a merged worktree keeps its target/; the write names the session."""
        one, held = self.tree("one", "target"), self.tree("held", "target")
        row = self.row()
        sessions.beat(self.connection, "session-a", machine=this_host(), cwd=None,
                      found=sessions.worktrees([str(held)]))
        answer = self.write(row["id"], row["revision"])
        self.assertEqual(answer["done"], [f"{one}/target"])
        self.assertEqual([entry["name"] for entry in answer["refused"]], [str(held)])
        self.assertIn("a live session holds it: session-", answer["refused"][0]["why"])
        self.assertTrue((held / "target" / "deep" / "file").exists())

    def test_an_unread_registry_keeps_the_tree(self):
        """Not knowing who holds a worktree is unknown (502), never a removal."""
        one = self.tree("one", "target")
        row = self.row()
        with patch.object(sessions, "holders", side_effect=sqlite3.OperationalError("database is locked")):
            status, answer = self.write(row["id"], row["revision"])
        self.assertEqual((status, answer["outcome"], answer["done"]), (502, "unknown", []))
        self.assertIn("the session registry was not read", answer["error"])
        self.assertTrue((one / "target").exists())


class TheRequest(ScreenCase):
    def test_a_request_without_a_row_and_a_full_revision_is_refused(self):
        for payload in ({}, {"row": "build:merged"}, {"row": "build:merged", "revision": "abc"},
                        {"row": "build:merged", "revision": "0" * 64, "extra": 1}):
            with self.assertRaises(ValueError, msg=payload):
                health_cleanup.request(payload)

    def test_a_row_with_no_cleanup_is_refused(self):
        for row in ("brs:/x", "unread:beta", "doc:ok", "gone:", "br:"):
            with self.assertRaisesRegex(ValueError, "has a cleanup", msg=row):
                health_cleanup.request({"row": row, "revision": "0" * 64})


class TheRoute(Isolated, BrowserSession):
    def test_a_refusal_answers_400_and_a_stale_revision_409(self):
        status, _, body = self.post("/api/health/cleanup", {"row": "br:/nowhere", "revision": "0" * 64})
        self.assertEqual((status, body["error"]), (400, "/nowhere is not a registered repository. Nothing was deleted."))
        repo = self.cloned("alpha")
        git("-C", str(repo), "branch", "feat/one")
        self.repo(str(repo))
        status, _, body = self.post("/api/health/cleanup", {"row": f"br:{repo}", "revision": "0" * 64})
        self.assertEqual((status, body["reload"]), (409, True))
        self.assertIn("feat/one", git("-C", str(repo), "branch", "--list", "feat/one"))
