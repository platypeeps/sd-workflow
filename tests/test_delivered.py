"""`sd_lib.delivered`: the one question a checkout with no database asks git.

Nothing here mocks git. Every case builds real repositories on disk -- a bare
fixture remote, real clones, a real linked worktree, a real `--depth 1`, and an
unreachable remote made by pointing the remote URL at a path that does not
exist -- because the whole function is about what a *particular kind of
checkout* can and cannot see, and a mocked `git fetch` would have agreed with
every wrong answer about that.

The distinction the suite exists to defend, stated once here because it is not
visible in any single assertion:

    `no` is a positive finding from history the checkout actually has.
    `unknown` is the answer when the checkout cannot see far enough to say.

Both a shallow clone and an unreachable remote produce `unknown`, never `no`.
A checkout with no remote produces `no`, because it has all the history there
is. Getting that backwards does not fail loudly: a `no` where the answer is
`unknown` silently reopens a delivered item, and every reader that picks work
-- `sd-review --scope planning`, a planning skill -- picks it again and redoes it. So
three of the cases below assert `no` is *never* the answer, as well as
asserting what the answer is; an implementation that answered `no` on an
unreachable remote would pass a bare `assertEqual(..., "unknown")` suite the
day someone reordered two branches, and fail these.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_lib  # noqa: E402

#: A real item directory name, punctuation and all, rather than "item-1": the
#: trailer is matched by string equality against whatever `sd-ship` wrote.
ITEM = "2026-09-05-the-pack-runs-a-team-process-for-one-person"
OTHER = "2026-09-04-some-other-item"


def merge_message(subject: str, *trailers: str) -> str:
    """A merge message shaped the way `sd-ship` writes one.

    Subject, a body paragraph, then the trailer block. The body matters: a
    reader that scanned the whole message rather than the last paragraph would
    still pass every case here if the trailers were the only lines, so the
    fixture always puts a paragraph between them.
    """
    body = "What this merge settles, said in the body where a person reads it."
    return f"{subject}\n\n{body}\n\n" + "\n".join(trailers)


class GitFixture(unittest.TestCase):
    """Real repositories under a throwaway directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name).resolve()

    def git(self, cwd: pathlib.Path, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
        )
        return done.stdout.strip()

    def configure(self, root: pathlib.Path) -> None:
        # Per-repository, not global: the suite must not depend on, or disturb,
        # whatever the machine running it has configured.
        self.git(root, "config", "user.email", "test@example.com")
        self.git(root, "config", "user.name", "Test User")
        self.git(root, "config", "commit.gpgsign", "false")

    def fixture_remote(self, name: str = "origin.git") -> pathlib.Path:
        path = self.tmp / name
        self.git(self.tmp, "init", "--quiet", "--bare", "-b", "main", str(path))
        return path

    def seeded_remote(self, name: str = "origin.git") -> pathlib.Path:
        """A bare remote holding one commit on `main`.

        Seeded through a throwaway checkout rather than by plumbing, so the
        remote's `HEAD` is a real symbolic ref and a clone of it sets
        `refs/remotes/origin/HEAD` the way a clone of a real repository does --
        which is where `_upstream` reads the default branch from.
        """
        remote = self.fixture_remote(name)
        root = self.tmp / f"{name}-seed"
        root.mkdir()
        self.git(root, "init", "--quiet", "-b", "main")
        self.configure(root)
        (root / "README.md").write_text("seed\n", encoding="utf-8")
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", "chore: seed")
        self.git(root, "remote", "add", "origin", str(remote))
        self.git(root, "push", "--quiet", "-u", "origin", "main")
        return remote

    def clone(self, remote: pathlib.Path, name: str) -> pathlib.Path:
        path = self.tmp / name
        self.git(self.tmp, "clone", "--quiet", str(remote), str(path))
        self.configure(path)
        return path

    def ship(self, root: pathlib.Path, branch: str, message: str) -> str:
        """One slice as `sd-ship` leaves it: a branch, a commit on it, a no-ff
        merge into the default branch carrying `message`, and a push."""
        self.git(root, "checkout", "--quiet", "-b", branch)
        (root / "work.txt").write_text(f"{branch}\n", encoding="utf-8")
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", f"feat({branch}): a change")
        self.git(root, "checkout", "--quiet", "main")
        self.git(root, "merge", "--no-ff", "--quiet", "-m", message, branch)
        self.git(root, "push", "--quiet", "origin", "main")
        return self.git(root, "rev-parse", "HEAD")

    def cut_the_remote(self, root: pathlib.Path) -> str:
        """Make the remote unreachable, and return the URL to put back.

        A path that does not exist rather than a hostname: no DNS, no timeout,
        no network at all, and `git fetch` fails the same way it fails when the
        remote is down -- which is the condition the case is about.
        """
        was = self.git(root, "remote", "get-url", "origin")
        self.git(root, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        return was


class TrailerBlockTests(GitFixture):
    """What counts as a closing trailer, decided on the message alone."""

    def test_only_the_last_paragraph_closes_the_item(self) -> None:
        """A commit describing a trailer must not act as one.

        The failure this guards is not hypothetical for this repository: the
        merge that adds `delivered` itself has to quote `Delivers: <item>` in
        its body to explain what it does, and a reader scanning the whole
        message would then close the item that merge was only describing.
        """
        quoted = f"docs: explain the trailer\n\nWrite `Delivers: {ITEM}` on the merge.\n\nItem: {ITEM}"
        self.assertFalse(sd_lib._closes(quoted, ITEM))

    def test_a_last_paragraph_that_is_not_all_trailers_closes_nothing(self) -> None:
        """Lane review of pack sd:5999, round 6: git reads a trailer block only when every line is one.

        A fenced example, an HTML comment or a prose example ends the message
        as its last paragraph, and `git interpret-trailers --parse` returns
        nothing for it; a real block still closes, continuation lines and all.
        """
        for last in (f"```\nDelivers: {ITEM}\n```", f"<!--\nDelivers: {ITEM}\n-->",
                     f"Write the trailer as\nDelivers: {ITEM}", f"Delivers: {ITEM}\n \nRefs: sd:6"):
            with self.subTest(last=last):
                self.assertFalse(sd_lib._closes(f"docs: the trailer\n\nWords.\n\n{last}\n", ITEM))
        self.assertTrue(sd_lib._closes(merge_message("feat: a slice", f"Delivers: {ITEM}", "Co-Authored-By: a",
                                                     "  b <b@example.test>"), ITEM))

    def test_item_alone_closes_nothing(self) -> None:
        """`Item:` ties a merge to an item; it is not a closure. A slice
        shipped without `--deliver` carries exactly this and stays open."""
        self.assertFalse(sd_lib._closes(merge_message("feat: a slice", f"Item: {ITEM}"), ITEM))

    def test_a_trailer_naming_another_item_closes_nothing(self) -> None:
        message = merge_message("feat: a slice", f"Item: {OTHER}", f"Delivers: {OTHER}")
        self.assertFalse(sd_lib._closes(message, ITEM))
        self.assertTrue(sd_lib._closes(message, OTHER))

    def test_a_squash_held_at_merge_closes_nothing(self) -> None:
        """A `Reviewed-base:` the landed parent differs from is the hold reconcile
        records; git readers keep the item open with it (#1179)."""
        message = merge_message("feat: a slice", f"Item: {ITEM}", f"Delivers: {ITEM}", "Reviewed-base: aaa")
        self.assertFalse(sd_lib._closes(message, ITEM, "bbb"))
        self.assertTrue(sd_lib._closes(message, ITEM, "aaa ccc"))
        self.assertTrue(sd_lib.held_at_merge(message, "bbb"))
        self.assertIsNone(sd_lib.held_at_merge(merge_message("feat: a slice", f"Delivers: {ITEM}"), "bbb"))

    def test_a_rebase_tip_is_held_against_its_nth_ancestor(self) -> None:
        """sd:6790. A rebase merge lands N commits; the tip's parent is the PR's own
        commit, so `Reviewed-base: <sha> ~N` is read against the tip's Nth ancestor.
        A reader that cannot name that ancestor holds the tip."""
        message = merge_message("fix: last", f"Item: {ITEM}", f"Delivers: {ITEM}", "Reviewed-base: aaa ~3")
        self.assertFalse(sd_lib.held_at_merge(message, "ppp", {3: "aaa"}.get))
        self.assertTrue(sd_lib.held_at_merge(message, "ppp", {3: "bbb"}.get))
        self.assertTrue(sd_lib.held_at_merge(message, "aaa"))
        self.assertTrue(sd_lib._closes(message, ITEM, "ppp", {3: "aaa"}.get))
        self.assertFalse(sd_lib._closes(message, ITEM, "ppp"))

    def test_both_closing_trailers_close(self) -> None:
        for trailer in (sd_lib.DELIVERS_TRAILER, sd_lib.CLOSES_TRAILER):
            with self.subTest(trailer=trailer):
                message = merge_message("feat: a slice", f"Item: {ITEM}", f"{trailer} {ITEM}")
                self.assertTrue(sd_lib._closes(message, ITEM))


class AnswerTests(GitFixture):
    """The three words, and the repair that rides on an `unknown`."""

    def test_the_answer_is_the_word(self) -> None:
        """A caller that only wants the word compares it to the word. The
        repair is an attribute, so no caller has to know it is there."""
        self.assertEqual(sd_lib.Answer(sd_lib.YES), "yes")
        self.assertEqual(sd_lib.Answer(sd_lib.YES).repair, "")
        self.assertEqual(sd_lib.Answer(sd_lib.UNKNOWN, "git fetch origin main").repair,
                         "git fetch origin main")


class DeliveredTests(GitFixture):
    """The nine cases of criterion 13, each on a checkout that really is one."""

    # -- 1, 2, 3: what a merge on the default branch says ------------------

    def test_a_merge_carrying_delivers_answers_yes(self) -> None:
        """Case 1. `Item:` ties the merge to the item, `Delivers:` closes it."""
        work = self.clone(self.seeded_remote(), "work")
        self.ship(work, "slice", merge_message(
            "feat: the slice (#1)", f"Item: {ITEM}", f"Delivers: {ITEM}"))
        self.assertEqual(sd_lib.delivered(work, ITEM), "yes")

    def test_a_rebase_landing_delivers_at_its_tip(self) -> None:
        """sd:6790. A rebase merge lands each commit on main; the tip carries
        `Reviewed-base: <base> ~2`, read against its second ancestor."""
        work = self.clone(self.seeded_remote(), "work")
        base = self.git(work, "rev-parse", "HEAD")
        for name in ("one", "two"):
            (work / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
            self.git(work, "add", "-A")
            trailers = ([f"Item: {ITEM}", f"Delivers: {ITEM}", f"Reviewed-base: {base} ~2"]
                        if name == "two" else ["Refs: sd:1"])
            self.git(work, "commit", "--quiet", "-m", merge_message(f"feat: {name}", *trailers))
        self.git(work, "push", "--quiet", "origin", "main")
        self.assertEqual(sd_lib.delivered(work, ITEM), "yes")

    def test_a_merged_branch_commit_closes_nothing(self) -> None:
        """sd:6790 review. A merge commit lands its branch's own commits on
        main's history; only main's first-parent commits speak for it, so a
        branch commit's `Closes:` the merge never validated closes nothing."""
        work = self.clone(self.seeded_remote(), "work")
        self.git(work, "checkout", "--quiet", "-b", "slice")
        (work / "work.txt").write_text("slice\n", encoding="utf-8")
        self.git(work, "add", "-A")
        self.git(work, "commit", "--quiet", "-m", merge_message("feat: a change", f"Closes: {ITEM}"))
        self.git(work, "checkout", "--quiet", "main")
        self.git(work, "merge", "--no-ff", "--quiet", "-m", merge_message("feat: the slice (#1)", f"Item: {OTHER}"), "slice")
        self.git(work, "push", "--quiet", "origin", "main")
        self.assertEqual(sd_lib.delivered(work, ITEM), "no")

    def test_a_merge_carrying_item_alone_answers_no(self) -> None:
        """Case 2. A slice shipped without `--deliver` is exactly this, and the
        item is still open. `no` and not `unknown`: the checkout is a whole,
        current clone, so it has seen everything there is to see."""
        work = self.clone(self.seeded_remote(), "work")
        self.ship(work, "slice", merge_message("feat: the slice (#1)", f"Item: {ITEM}"))
        answer = sd_lib.delivered(work, ITEM)
        self.assertEqual(answer, "no")
        self.assertEqual(answer.repair, "")

    def test_a_later_merge_closing_the_item_answers_yes_and_not_before(self) -> None:
        """Case 3. A delivery whose own merge went out without the trailer is
        marked by the next pull request in that repository, whose message
        carries `Closes:` for it. The `and not before` half is the point: the
        item is picked in a database-free checkout until that merge lands."""
        work = self.clone(self.seeded_remote(), "work")
        self.ship(work, "slice", merge_message("feat: the slice (#1)", f"Item: {ITEM}"))
        self.assertEqual(sd_lib.delivered(work, ITEM), "no")
        self.ship(work, "next", merge_message(
            "feat: something else (#2)", f"Item: {OTHER}", f"Closes: {ITEM}"))
        self.assertEqual(sd_lib.delivered(work, ITEM), "yes")

    # -- 4: a checkout that cannot see far enough --------------------------

    def test_a_shallow_clone_answers_unknown_until_it_is_unshallowed(self) -> None:
        """Case 4. The delivering merge sits one commit past the boundary, so
        the clone holds no trailer and every byte it holds is consistent with
        the item being open. `no` here restarts delivered work; `unknown` names
        the boundary and `git fetch --unshallow` as the repair."""
        remote = self.seeded_remote()
        work = self.clone(remote, "work")
        self.ship(work, "slice", merge_message(
            "feat: the slice (#1)", f"Item: {ITEM}", f"Delivers: {ITEM}"))
        (work / "after.txt").write_text("after\n", encoding="utf-8")
        self.git(work, "add", "-A")
        self.git(work, "commit", "--quiet", "-m", "chore: one more commit")
        self.git(work, "push", "--quiet", "origin", "main")

        shallow = self.tmp / "shallow"
        # `file://` because git ignores `--depth` on a plain local path clone,
        # and a clone that is not shallow tests nothing here.
        self.git(self.tmp, "clone", "--quiet", "--depth", "1", "--branch", "main",
                 f"file://{remote}", str(shallow))
        self.configure(shallow)
        self.assertEqual(self.git(shallow, "rev-parse", "--is-shallow-repository"), "true")

        answer = sd_lib.delivered(shallow, ITEM)
        self.assertEqual(answer, "unknown")
        self.assertIn("--unshallow", answer.repair)

        self.git(shallow, "fetch", "--quiet", "--unshallow")
        self.assertEqual(sd_lib.delivered(shallow, ITEM), "yes")

    # -- 5: a whole clone that is behind -----------------------------------

    def test_a_clone_retained_across_another_machines_delivery(self) -> None:
        """Case 5. The clone is whole -- nothing is past a boundary -- and it
        still holds no trailer, because the delivery happened somewhere else.
        Whole is not current, and only the fetch tells them apart. `no` never:
        this checkout's own history is a complete and completely stale account
        of an item that is finished."""
        remote = self.seeded_remote()
        retained = self.clone(remote, "retained")
        elsewhere = self.clone(remote, "elsewhere")
        self.ship(elsewhere, "slice", merge_message(
            "feat: the slice (#1)", f"Item: {ITEM}", f"Delivers: {ITEM}"))

        was = self.cut_the_remote(retained)
        unreachable = sd_lib.delivered(retained, ITEM)
        self.git(retained, "remote", "set-url", "origin", was)
        reached = sd_lib.delivered(retained, ITEM)

        self.assertEqual(unreachable, "unknown")
        self.assertIn("origin", unreachable.repair)
        self.assertIn("git fetch", unreachable.repair)
        self.assertEqual(reached, "yes")
        self.assertNotIn("no", (unreachable, reached))

    # -- 6: a checkout with nothing to be behind ---------------------------

    def test_a_checkout_with_no_remote_answers_no_from_its_own_history(self) -> None:
        """Case 6. `no`, not `unknown`, and this is the case that keeps the two
        apart from being the same word. There is no remote, so there is nothing
        this checkout could be behind: its own history is all the history there
        is, and a positive finding from it is a real finding."""
        root = self.tmp / "alone"
        root.mkdir()
        self.git(root, "init", "--quiet", "-b", "main")
        self.configure(root)
        (root / "README.md").write_text("seed\n", encoding="utf-8")
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", "chore: seed")
        self.git(root, "checkout", "--quiet", "-b", "slice")
        (root / "work.txt").write_text("work\n", encoding="utf-8")
        self.git(root, "add", "-A")
        self.git(root, "commit", "--quiet", "-m", "feat: a change")
        self.git(root, "checkout", "--quiet", "main")
        self.git(root, "merge", "--no-ff", "--quiet", "-m",
                 merge_message("feat: the slice (#1)", f"Item: {ITEM}"), "slice")

        self.assertEqual(self.git(root, "remote"), "")
        self.assertEqual(sd_lib.delivered(root, ITEM), "no")

    # -- 7, 8: a mark that lives on the item's own branch ------------------

    def branch_mark_case(self, remote_name: str, branch: str, subject: str) -> None:
        """A retained clone sitting on `branch` while a second clone puts the
        closing mark on that branch and pushes it there.

        One body for cases 7 and 8, because the two differ only in which
        repository and which branch: a branch-only cancel and a guest delivery
        both leave one empty commit on the item's branch and nothing at all on
        the default branch. The assertion that the default branch is unchanged
        is what proves the answer came from the branch fetch -- without it, an
        implementation that only ever read the default branch could pass by
        accident if the fixture had leaked the mark onto it.
        """
        remote = self.seeded_remote(remote_name)
        seed = self.clone(remote, "seed-of-the-branch")
        self.git(seed, "checkout", "--quiet", "-b", branch)
        (seed / "work.txt").write_text("work\n", encoding="utf-8")
        self.git(seed, "add", "-A")
        self.git(seed, "commit", "--quiet", "-m", "feat: the work so far")
        self.git(seed, "push", "--quiet", "-u", "origin", branch)

        retained = self.clone(remote, "retained")
        self.git(retained, "checkout", "--quiet", branch)
        default_before = self.git(retained, "rev-parse", "origin/main")

        elsewhere = self.clone(remote, "elsewhere")
        self.git(elsewhere, "checkout", "--quiet", branch)
        self.git(elsewhere, "commit", "--allow-empty", "--quiet", "-m",
                 merge_message(subject, f"Item: {ITEM}", f"Closes: {ITEM}"))
        self.git(elsewhere, "push", "--quiet", "origin", branch)

        was = self.cut_the_remote(retained)
        unreachable = sd_lib.delivered(retained, ITEM)
        self.git(retained, "remote", "set-url", "origin", was)
        reached = sd_lib.delivered(retained, ITEM)

        self.assertEqual(unreachable, "unknown")
        self.assertEqual(reached, "yes")
        self.assertNotIn("no", (unreachable, reached))
        self.assertEqual(self.git(retained, "rev-parse", "origin/main"), default_before)

    def test_a_clone_on_the_items_branch_across_a_cancel_answers_yes(self) -> None:
        """Case 7. A cancel closes the item as surely as a delivery does, and
        where the triad never left the item's branch the mark is one empty
        commit on that branch, pushed there and nowhere else."""
        self.branch_mark_case("origin.git", "item/the-pack",
                              "chore(cancel): the item is cancelled")

    def test_a_guest_clone_on_the_forks_integration_branch_answers_yes(self) -> None:
        """Case 8. Under `mode: guest` the delivery's mark goes on the fork's
        integration branch, with no pull request and nothing upstream, so the
        default branch is again the wrong place to look."""
        self.branch_mark_case("fork.git", "sd/integration",
                              "chore(deliver): the item shipped upstream")

    # -- 9: a branch the remote no longer has ------------------------------

    def deleted_branch_case(self, *trailers: str) -> sd_lib.Answer:
        """A retained worktree on a branch the fixture remote deleted at its
        merge -- what `delete_branch_on_merge` leaves behind everywhere.

        The branch fetch this checkout would otherwise wait on cannot succeed
        and never will again. A branch that is gone is nothing to be behind,
        so the answer comes from the default branch as fetched and from `HEAD`,
        and it is a real answer either way -- never `unknown`, which would
        strand every kept worktree in the repository permanently.
        """
        remote = self.seeded_remote()
        work = self.clone(remote, "work")
        self.git(work, "checkout", "--quiet", "-b", "slice")
        (work / "work.txt").write_text("work\n", encoding="utf-8")
        self.git(work, "add", "-A")
        self.git(work, "commit", "--quiet", "-m", "feat: a change")
        self.git(work, "push", "--quiet", "-u", "origin", "slice")
        self.git(work, "checkout", "--quiet", "main")

        kept = self.tmp / "kept"
        self.git(work, "worktree", "add", "--quiet", str(kept), "slice")
        self.git(work, "merge", "--no-ff", "--quiet", "-m",
                 merge_message("feat: the slice (#1)", *trailers), "slice")
        self.git(work, "push", "--quiet", "origin", "main")
        self.git(work, "push", "--quiet", "origin", "--delete", "slice")
        self.assertEqual(self.git(work, "ls-remote", "--heads", "origin", "slice"), "")
        return sd_lib.delivered(kept, ITEM)

    def test_a_worktree_on_a_deleted_branch_answers_yes_after_a_delivering_merge(self) -> None:
        """Case 9, first half."""
        answer = self.deleted_branch_case(f"Item: {ITEM}", f"Delivers: {ITEM}")
        self.assertEqual(answer, "yes")

    def test_a_worktree_on_a_deleted_branch_answers_no_after_a_slices_merge(self) -> None:
        """Case 9, second half. The one place a failed fetch is not `unknown`,
        and the reason is that the failure is git naming a ref that is gone,
        not a remote that could not be reached -- the default-branch fetch
        immediately before it succeeded."""
        answer = self.deleted_branch_case(f"Item: {ITEM}")
        self.assertEqual(answer, "no")


if __name__ == "__main__":
    unittest.main()
