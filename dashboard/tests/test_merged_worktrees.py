"""Merged worktrees (sd:5105): the Branches reading lists the worktrees the lane merged, and `wt:merged` removes them.

What this slice promises: only a lane entry the lane merged ties a worktree to a merge; a fresh worktree at
origin/HEAD is not one. A tree with uncommitted, untracked or non-build ignored files, a process working in it, a
branch that moved here or on origin, or a process probe that gave no answer stays, and the answer says why. The write
reads the trees again and refuses a revision that differs (409). It moves the remote branch, the checkout, the local
branch and the tree in that order, so a stop between any two steps leaves the tree in the next read, and a second run
finishes it.

Every test runs real git against fixture repositories under a temporary folder, with a local bare repository as
origin and a stub `lsof` first on PATH. No test touches a real checkout, a real remote or the lane.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import health_collectors, health_screen
from sd_dashboard.health_collectors import Walk
from sd_db import sessions, workflow
from sd_db.ship import this_host
from support import ScreenCase
from test_health_cleanup import Isolated, git


class Merged(Isolated, ScreenCase):
    def setUp(self):
        super().setUp()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self.cwds = self.root / "cwds"
        self.cwds.write_text("/\n")
        lsof = bin_dir / "lsof"
        lsof.write_text(f'#!/bin/sh\n[ -f "{self.root}/lsof-fails" ] && exit 2\n'
                        f'while read -r cwd; do printf "p1\\nn%s\\n" "$cwd"; done < "{self.cwds}"\n')
        lsof.chmod(0o755)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        self.origin = self.root / "origin.git"
        git("init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.root / "seed"
        git("init", "-q", "-b", "main", str(seed))
        (seed / ".gitignore").write_text("target/\n.env\n")
        git("-C", str(seed), "add", ".gitignore")
        git("-C", str(seed), "commit", "-q", "-m", "first")
        git("-C", str(seed), "push", "-q", str(self.origin), "main")
        self.alpha = self.root / "repos" / "alpha"
        git("clone", "-q", str(self.origin), str(self.alpha))
        self.repo(str(self.alpha))
        self.entries = 0

    def tree(self, name: str, *, push: bool = True, status: str = "merged", key: str = "head") -> Path:
        """A worktree on feat/<name> with one commit, pushed, and a lane entry that names its head."""
        tree = self.root / f"wt-{name}"
        git("-C", str(self.alpha), "worktree", "add", "-q", "-b", f"feat/{name}", str(tree), "origin/main")
        git("-C", str(tree), "commit", "-q", "--allow-empty", "-m", name)
        if push:
            git("-C", str(tree), "push", "-q", "origin", f"feat/{name}")
        self.entries += 1
        body = {"id": f"e{self.entries}", "item": 5105, "worktree": str(tree), "branch": f"feat/{name}",
                "status": status, "expected_head": "0" * 40, key: git("-C", str(tree), "rev-parse", "HEAD"),
                "repository": "example/alpha"}
        self.connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                                (f"lane:v1:example/alpha:{body['id']}", "2026-10-18T20:00:00Z", json.dumps(body)))
        self.connection.commit()
        return tree

    def row(self) -> dict | None:
        rows = health_screen._branch_rows(health_collectors.branch_scan(
            self.connection, repo_paths=[str(self.alpha)], heads=health_collectors.merged_heads(self.connection)))
        return next((row for row in rows if row["id"] == "wt:merged"), None)

    def kept(self) -> dict:
        return {Path(x["name"]).name: x["why"] for x in self.row()["kept"]}

    def gone(self, tree: Path, branch: str) -> None:
        self.assertFalse(tree.exists())
        self.assertEqual(git("-C", str(self.alpha), "branch", "--list", branch), "")
        self.assertEqual(git("ls-remote", str(self.origin), f"refs/heads/{branch}"), "")
        self.assertNotIn(str(tree), git("-C", str(self.alpha), "worktree", "list"))


class TheReading(Merged):
    def test_only_a_tree_the_lane_merged_is_listed_and_a_fresh_one_at_origin_is_not(self):
        one = self.tree("one")
        git("-C", str(self.alpha), "worktree", "add", "-q", "-b", "fresh", str(self.root / "wt-fresh"), "origin/main")
        # The entry names wt-one: another tree at the same head is not evidence of its own merge.
        git("-C", str(self.alpha), "worktree", "add", "-q", "--detach", str(self.root / "wt-copy"), "feat/one")
        self.tree("open", status="running")
        row = self.row()
        self.assertEqual((row["removable"], row["kept"], row["state"]), ([str(one)], [], "caution"))
        self.assertEqual(row["list"], [f"{one} · sd:5105 · feat/one"])
        self.assertIn(f"worktree remove {one} && git -C {self.alpha} update-ref -d refs/heads/feat/one", row["cli"])

    def test_an_older_entry_with_only_its_expected_head_still_names_the_merge(self):
        one = self.tree("one", key="expected_head")
        self.assertEqual(self.row()["removable"], [str(one)])

    def test_a_tree_with_work_or_a_process_in_it_stays_and_says_why(self):
        self.tree("untracked").joinpath("notes.txt").write_text("mine")
        self.tree("secret").joinpath(".env").write_text("TOKEN=change-me")
        busy = self.tree("busy")
        built = self.tree("built")
        (built / "target").mkdir()
        (built / "target" / "out").write_text("x")
        self.cwds.write_text(f"/\n{busy}/sub\n")
        self.assertEqual(self.kept(), {"wt-untracked": "it has uncommitted or untracked files",
                                       "wt-secret": "it holds ignored files that are not build output: .env",
                                       "wt-busy": f"a process works in {busy}/sub"})
        self.assertEqual(self.row()["removable"], [str(built)])

    def test_a_tree_a_live_session_holds_reads_as_kept_not_removable(self):
        """pack sd:6683: the read keeps a held tree, so Remove merged is not offered for a tree it would refuse."""
        held = self.tree("held")
        sessions.beat(self.connection, "session-a", machine=this_host(), cwd=None,
                      found=sessions.worktrees([str(held)]))
        row = self.row()
        self.assertEqual((row["removable"], row["state"]), ([], "queued"))
        self.assertIn("a live session holds it: session-", self.kept()["wt-held"])

    def test_a_process_probe_that_gave_no_answer_keeps_every_tree(self):
        self.tree("one")
        (self.root / "lsof-fails").write_text("")
        row = self.row()
        self.assertEqual((row["removable"], row["state"]), ([], "queued"))
        self.assertEqual(self.kept(), {"wt-one": "not known whether a process works in it: lsof gave no answer"})

    def test_a_repository_whose_worktree_list_failed_is_named_not_read_as_having_none(self):
        """sd:5120: `git worktree list` failing is an unknown answer, not an empty list."""
        one = self.tree("one")
        broken = self.root / "repos" / "broken"
        broken.mkdir()
        self.repo(str(broken))
        scan = health_collectors.branch_scan(
            self.connection, repo_paths=[str(self.alpha), str(broken)], heads=health_collectors.merged_heads(self.connection))
        rows = {row["id"]: row for row in health_screen._branch_rows(scan)}
        self.assertEqual(rows["wt:merged"]["removable"], [str(one)])
        self.assertEqual((rows["wt:unread"]["state"], rows["wt:unread"]["list"]), ("unknown", [str(broken)]))
        self.assertIn("merged worktrees were not listed", rows["wt:unread"]["detail"])
        self.assertEqual(scan["worktrees"]["revision"], health_collectors.worktrees_revision(scan["worktrees"]["trees"]))

    def test_a_registered_repository_not_cloned_on_this_machine_is_not_a_git_error(self):
        """A repository registered for another machine has no folder here: nothing to read, nothing failed."""
        absent = str(self.root / "repos" / "elsewhere")
        self.repo(absent)
        scan = health_collectors.branch_scan(
            self.connection, repo_paths=[str(self.alpha), absent], heads=health_collectors.merged_heads(self.connection))
        self.assertEqual((scan["unread"], scan["worktrees"]["unread"], scan["repos"]), ([], [], 1))


class TheWrite(Merged):
    def test_remove_takes_the_tree_its_branch_origins_branch_and_its_build_output(self):
        one = self.tree("one")
        (one / "target").mkdir()
        row = self.row()
        self.assertEqual(self.write(row["id"], row["revision"]),
                         {"ok": True, "row": "wt:merged", "done": ["origin/feat/one", str(one)], "refused": []})
        self.gone(one, "feat/one")
        self.assertIsNone(self.row())

    def test_a_tree_a_live_session_holds_stays(self):
        """pack sd:6024: a session on this machine that holds the tree keeps it, its branch and origin's."""
        one, held = self.tree("one"), self.tree("held")
        row = self.row()
        sessions.beat(self.connection, "session-a", machine=this_host(), cwd=None,
                      found=sessions.worktrees([str(held)]))
        # pack sd:6683: the hold moves the row, so the page's revision is stale and nothing goes.
        with self.assertRaises(workflow.StaleItem):
            self.write(row["id"], row["revision"])
        self.assertTrue(one.is_dir())
        row = self.row()
        answer = self.write(row["id"], row["revision"])
        self.assertEqual(answer["done"], ["origin/feat/one", str(one)])
        self.assertEqual([entry["name"] for entry in answer["refused"]], [str(held)])
        self.assertIn("a live session holds it", answer["refused"][0]["why"])
        self.assertTrue(held.is_dir())
        self.assertNotEqual(git("ls-remote", str(self.origin), "refs/heads/feat/held"), "")

    def test_a_revision_from_another_row_or_a_tree_moved_since_the_read_is_stale(self):
        one = self.tree("one")
        row = self.row()
        with self.assertRaises(workflow.StaleItem):
            self.write("wt:merged", health_collectors.branches_revision(str(self.alpha), []))
        git("-C", str(one), "commit", "-q", "--allow-empty", "-m", "since the read")
        with self.assertRaises(workflow.StaleItem):
            self.write(row["id"], row["revision"])
        self.assertTrue(one.is_dir())

    def test_origins_branch_moved_past_the_merge_keeps_the_tree_and_both_branches(self):
        one = self.tree("one")
        other = self.root / "other"
        git("clone", "-q", "-b", "feat/one", str(self.origin), str(other))
        git("-C", str(other), "commit", "-q", "--allow-empty", "-m", "more")
        git("-C", str(other), "push", "-q", "origin", "feat/one")
        row = self.row()
        with self.assertRaisesRegex(ValueError, r"origin/feat/one is at [0-9a-f]{12}, not the merged head"):
            self.write(row["id"], row["revision"])
        self.assertTrue(one.is_dir())
        self.assertNotEqual(git("-C", str(self.alpha), "branch", "--list", "feat/one"), "")

    def test_a_file_written_after_the_read_is_kept_by_the_check_before_the_steps(self):
        one, two = self.tree("one"), self.tree("two")
        row = self.row()
        real, seen = health_collectors.merged_tree_kept, []

        def then_written(walk, tree, cwds):
            # The write's own read sees two clean; the file lands before the check just ahead of its steps.
            seen.append(tree["path"])
            if seen.count(str(two)) == 2:
                (two / "late.txt").write_text("late")
            return real(walk, tree, cwds)

        with patch.object(health_collectors, "merged_tree_kept", then_written):
            answer = self.write(row["id"], row["revision"])
        self.assertEqual(answer["done"], ["origin/feat/one", str(one)])
        self.assertEqual(answer["refused"], [{"name": str(two), "why": "it has uncommitted or untracked files"}])
        self.assertTrue((two / "late.txt").exists())
        self.assertEqual(git("ls-remote", str(self.origin), "refs/heads/feat/two").split()[1], "refs/heads/feat/two")

    def test_a_refusal_after_origins_branch_went_names_that_step_and_is_not_a_400(self):
        one = self.tree("one")
        row = self.row()
        real = Walk.git

        def late(walk, path, *args):
            if args[:2] == ("worktree", "remove"):
                (one / "late.txt").write_text("late")
            return real(walk, path, *args)

        with patch.object(Walk, "git", late):
            answer = self.write(row["id"], row["revision"])
        self.assertEqual(answer["done"], ["origin/feat/one"])
        self.assertEqual([x["name"] for x in answer["refused"]], [str(one)])
        self.assertRegex(answer["refused"][0]["why"], r"^fatal: .*untracked")
        self.assertTrue((one / "late.txt").exists())
        self.assertEqual(self.kept(), {"wt-one": "it has uncommitted or untracked files"})

    def test_a_stop_between_any_two_steps_is_unknown_and_the_next_run_finishes(self):
        for step in (("checkout", "-q"), ("update-ref", "-d"), ("worktree", "remove")):
            with self.subTest(step=step):
                name = f"{step[0]}-stop"
                one = self.tree(name)
                row = self.row()
                real = Walk.git

                def stopped(walk, path, *args, step=step, real=real):
                    return None if args[:2] == step else real(walk, path, *args)

                with patch.object(Walk, "git", stopped):
                    status, answer = self.write(row["id"], row["revision"])
                self.assertEqual((status, answer["outcome"], answer["done"]), (502, "unknown", [f"origin/feat/{name}"]))
                self.assertTrue(one.is_dir())
                # The remote delete landed before the stop; the tree is still read as merged and can go.
                self.assertEqual(git("ls-remote", str(self.origin), f"refs/heads/feat/{name}"), "")
                again = self.row()
                self.assertEqual(again["removable"], [str(one)])
                # origin's branch is gone already, so the second run deletes only what is left.
                self.assertEqual(self.write(again["id"], again["revision"])["done"], [str(one)])
                self.gone(one, f"feat/{name}")


class TheRequest(ScreenCase):
    def test_wt_merged_has_a_cleanup(self):
        from sd_dashboard import health_cleanup

        self.assertTrue(callable(health_cleanup.request({"row": "wt:merged", "revision": "0" * 64})))


class Disposable(Merged):
    def test_a_unity_project_or_solution_keeps_the_tree_whatever_it_holds(self):
        """Class pass of build/sd-1782: a stamp, a shape or a tag list read from an ignored Unity file cannot prove
        nobody edited it, so cleanup would delete the edit. No such file counts as build output."""
        seed = self.root / "seed"
        (seed / "ProjectSettings").mkdir()
        (seed / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.0\n")
        (seed / ".gitignore").write_text("*.csproj\n*.slnx\n")
        git("-C", str(seed), "add", "-A")
        git("-C", str(seed), "commit", "-q", "-m", "unity")
        git("-C", str(seed), "push", "-q", str(self.origin), "main")
        git("-C", str(self.alpha), "fetch", "-q", "origin")
        stamp = ("<Project>\n  <!-- Generated file, do not modify, your changes will be overwritten "
                 "(use AssetPostprocessor.OnGeneratedCSProject) -->\n{0}</Project>\n")
        cases = {"stamped": ("Assembly-CSharp.csproj", stamp.format("")),
                 "edited": ("Assembly-CSharp.csproj", stamp.format(
                     "  <PropertyGroup><DefineConstants>LOCAL_FIX</DefineConstants></PropertyGroup>\n")),
                 "solution": ("wt-solution.slnx", '<Solution>\n  <Project Path="A.csproj" />\n</Solution>\n')}
        for name, (file, text) in cases.items():
            (self.tree(name) / file).write_text(text)
        (self.root / "wt-solution" / "A.csproj").write_text(stamp.format(""))
        why = "it holds ignored files that are not build output: "
        self.assertEqual(self.kept(), {"wt-stamped": why + "Assembly-CSharp.csproj",
                                       "wt-edited": why + "Assembly-CSharp.csproj", "wt-solution": why + "A.csproj"})
        self.assertEqual(self.row()["removable"], [])
