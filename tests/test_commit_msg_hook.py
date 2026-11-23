"""The commit-msg tier of sd:1931: `hooks/commit-msg`, installed by `make hooks`.

Git reads trailers from a message's final paragraph only. b0c440ad on sd:1910
put `Needed-by: sd:1910` in a paragraph of its own above the `Authored-with:`
block, `git log --format='%(trailers:key=Needed-by,valueonly)'` printed
nothing, and the pre-commit hook passed it because it reads staged files, not
the message. Three things are held here.

`sd_lib.unread_trailers` agrees with git. Each fixture is committed in a
throwaway repository and read back with `git log --format=%(trailers)`, the
form every sd reader uses; a line the function calls read must come back, and
a line it calls stray must not.

The hook refuses a message whose checked trailer git will not read, names the
line, says how to fix it, and lands no commit. A message that states no
checked trailer commits as before, and `SD_SKIP_HOOKS=1` does not skip it.

`make hooks` links it beside the pre-commit tier, relatively, one per clone.
"""

from __future__ import annotations

import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_lib  # noqa: E402

from tests.clean_env import clean_environment  # noqa: E402

HOOK = REPO_ROOT / "hooks" / "commit-msg"

CONTIGUOUS = (
    "fix: a thing\n\nBody.\n\n"
    "Needed-by: sd:1910\nDelivers: sd:7\n"
    "Co-Authored-By: Claude <noreply@example.test>\n"
)
# The b0c440ad shape: one blank line above the delivery block.
SPLIT = (
    "fix: a thing\n\nBody.\n\nNeeded-by: sd:1910\n\n"
    "Delivers: sd:7\nCo-Authored-By: Claude <noreply@example.test>\n"
)
# `Delivers:` alone, above the harness lines.
DELIVERS_SPLIT = (
    "fix: a thing\n\nDelivers: sd:7\n\n"
    "Co-Authored-By: Claude <noreply@example.test>\nClaude-Session: abc\n"
)
# A final paragraph that is prose with one trailer in it is not a trailer
# block: git reads no `Delivers:` there, though a line reader would (sd:3014).
PROSE_LAST = (
    "fix: a thing\n\nDelivers: sd:7\n"
    "and then a sentence that is not a trailer\nand another one\nand a third\n"
)
PLAIN = "fix: a thing\n\nNo trailers at all.\n"
QUOTED = "docs: explain\n\n    Delivers: sd:7\n\nsays who wrote it.\n"
FOLDED = (
    "fix: a thing\n\nDelivers: sd:7\n"
    "Co-Authored-By: Claude\n  <noreply@example.test>\n"
)
DIVIDER = (
    "fix: a thing\n\nBody.\n---\nmore body\n\n"
    "Needed-by: sd:3\nDelivers: sd:7\n"
)
REPEATED = (
    "fix: a thing\n\nNeeded-by: sd:4\n\n"
    "Needed-by: sd:4\nDelivers: sd:7\n"
)

FIXTURES = {
    "CONTIGUOUS": (CONTIGUOUS, ()),
    "SPLIT": (SPLIT, ("Needed-by: sd:1910",)),
    "DELIVERS_SPLIT": (DELIVERS_SPLIT, ("Delivers: sd:7",)),
    "PROSE_LAST": (PROSE_LAST, ("Delivers: sd:7",)),
    "PLAIN": (PLAIN, ()),
    "QUOTED": (QUOTED, ()),
    "FOLDED": (FOLDED, ()),
    "DIVIDER": (DIVIDER, ()),
    "REPEATED": (REPEATED, ("Needed-by: sd:4",)),
}


def git(*args: str, cwd: pathlib.Path, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, input=stdin, capture_output=True, text=True, check=True,
        env=clean_environment(),
    ).stdout


def scratch_repository(prefix: str) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "hook@example.invalid", cwd=root)
    git("config", "user.name", "hook", cwd=root)
    return root


def parse(root: pathlib.Path, message: str) -> str:
    return git("interpret-trailers", "--parse", "--no-divider", cwd=root, stdin=message)


class UnreadTrailersAgreeWithGit(unittest.TestCase):
    """The function's verdict, held against git's own reading of a real commit."""

    def setUp(self):
        self.root = scratch_repository("sd-1931-parse-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_each_fixture_names_exactly_the_lines_git_does_not_read(self):
        for name, (message, expected) in FIXTURES.items():
            with self.subTest(name):
                self.assertEqual(expected, sd_lib.unread_trailers(message, parse(self.root, message)))

    def test_a_line_called_read_comes_back_from_git_log_and_a_stray_one_does_not(self):
        for name, (message, _) in FIXTURES.items():
            with self.subTest(name):
                git("commit", "-q", "--allow-empty", "--no-verify", "-F", "-",
                    cwd=self.root, stdin=message)
                logged = git("log", "-1", "--format=%(trailers:unfold)", cwd=self.root)
                stray = sd_lib.unread_trailers(message, parse(self.root, message))
                read = [line for line in sd_lib.unread_trailers(message, "") if line not in stray]
                for line in read:
                    self.assertTrue(any(got.startswith(line) for got in logged.splitlines()),
                                    f"{line!r} was called read, and git log does not return it")
                if stray and name != "REPEATED":
                    for line in stray:
                        self.assertNotIn(line, logged.splitlines(),
                                         f"{line!r} was called stray, and git log returns it")

    def test_the_split_block_is_the_failure_the_item_reports(self):
        """Sanity for the fixture: git log really does lose the demoted key."""
        git("commit", "-q", "--allow-empty", "--no-verify", "-F", "-", cwd=self.root, stdin=SPLIT)
        self.assertEqual(
            "", git("log", "-1", "--format=%(trailers:key=Needed-by,valueonly)", cwd=self.root).strip()
        )


class HookFixture(unittest.TestCase):
    """`hooks/commit-msg` through a real `git commit`, installed by `make hooks`."""

    def setUp(self):
        self.assertTrue(HOOK.is_file(), f"{HOOK} is missing")
        self.root = scratch_repository("sd-1931-hook-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "hooks").mkdir()
        shutil.copy2(HOOK, self.root / "hooks" / "commit-msg")
        # The pre-commit tier is not under test here; a stub stands in for it
        # so `make hooks` has both files to link.
        stub = self.root / "hooks" / "pre-commit"
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
        # The hook imports from the pack's bin/ beside its real path, as an installed pack has it whole.
        shutil.copytree(REPO_ROOT / "bin", self.root / "bin", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(REPO_ROOT / "Makefile", self.root / "Makefile")
        made = subprocess.run(["make", "hooks"], cwd=self.root, env=clean_environment(),
                              capture_output=True, text=True, check=False)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        self.made = made

    def commit(self, message: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-F", "-"], cwd=self.root, input=message,
            env=clean_environment(**extra), capture_output=True, text=True, check=False,
        )

    def commits(self) -> int:
        result = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=self.root,
                                env=clean_environment(), capture_output=True, text=True,
                                check=False)
        return int(result.stdout.strip()) if result.returncode == 0 else 0


class TheHook(HookFixture):
    """The trailer check: a checked trailer git will not read is refused."""

    def test_make_hooks_links_the_commit_msg_hook_relatively(self):
        link = self.root / ".git" / "hooks" / "commit-msg"
        self.assertTrue(link.is_symlink(), f"{link} is not a symlink")
        self.assertEqual(os.readlink(link), "../../hooks/commit-msg")
        self.assertIn("commit-msg -> ../../hooks/commit-msg", self.made.stdout)
        self.assertIn("pre-commit -> ../../hooks/pre-commit", self.made.stdout)

    def test_a_split_trailer_block_is_refused_by_line_and_lands_nothing(self):
        result = self.commit(SPLIT)
        self.assertNotEqual(result.returncode, 0, "the commit went through")
        self.assertIn("commit-msg: refused", result.stderr)
        self.assertIn("    Needed-by: sd:1910", result.stderr)
        self.assertIn("final paragraph", result.stderr)
        self.assertIn("%(trailers:key=Delivers,valueonly)", result.stderr)
        self.assertEqual(0, self.commits(), "a commit landed")

    def test_a_delivers_line_in_a_prose_paragraph_is_refused(self):
        """sd:3014: git reads no trailer there, yet a line reader would call sd:7 delivered."""
        result = self.commit(PROSE_LAST)
        self.assertNotEqual(result.returncode, 0, "the commit went through")
        self.assertIn("    Delivers: sd:7", result.stderr)
        self.assertEqual(0, self.commits(), "a commit landed")

    def test_a_contiguous_block_commits_and_git_reads_the_delivery(self):
        result = self.commit(CONTIGUOUS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual("sd:7", git(
            "log", "-1", "--format=%(trailers:key=Delivers,valueonly)", cwd=self.root).strip())

    def test_a_line_over_72_characters_is_refused_at_commit_time(self):
        """pack sd:10212: 27 lane refusals in 7 days named a commit line the hook let through (operator: hard limit)."""
        result = self.commit("fix: a subject\n\n" + "x" * 73 + "\n")
        self.assertNotEqual(result.returncode, 0, "the commit went through")
        self.assertIn("commit-msg: refused, a line is 73 characters (limit 72): xxxx", result.stderr)
        self.assertEqual(0, self.commits(), "a commit landed")

    def verbose(self, *config: str) -> subprocess.CompletedProcess[str]:
        (self.root / "wide.txt").write_text("w" * 90 + "\n", encoding="utf-8")
        git("add", "wide.txt", cwd=self.root)
        flags = [arg for pair in config for arg in ("-c", pair)]
        return subprocess.run(["git", *flags, "commit", "-q", "-v", "-e", "-m", "fix: a", "-m", "#hash body"],
                              cwd=self.root, env=clean_environment(GIT_EDITOR="true"), capture_output=True,
                              text=True, check=False)

    def test_a_verbose_commit_with_an_auto_comment_char_commits(self):
        """Review round 4: `auto` picks another char when a line starts with `#`; the scissors move with it."""
        result = self.verbose("core.commentChar=auto")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_verbose_commit_with_a_comment_string_commits(self):
        """Review round 4: core.commentString (git 2.45+) is not core.commentChar."""
        result = self.verbose("core.commentString=//")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_comment_string_with_leading_spaces_is_read_whole(self):
        """Review round 6: git_output strips stdout, so `  //` read as `//` and missed the editor's comments."""
        draft = self.root.parent / f"{self.root.name}-spaced"
        draft.write_text("fix: a subject\n\n  // " + "c" * 80 + "\n", encoding="utf-8")
        self.addCleanup(draft.unlink)
        result = subprocess.run(["git", "-c", "core.commentString=  //", "commit", "-q", "--allow-empty", "-e",
                                 "-m", "x"], cwd=self.root,
                                env=clean_environment(GIT_EDITOR=f"cp {shlex.quote(str(draft))}"),
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_fixup_of_a_long_subject_commits(self):
        """Review round 4: `fixup! <subject>` is git's line, and autosquash removes it."""
        self.assertEqual(self.commit("fix: " + "f" * 67 + "\n").returncode, 0)
        result = subprocess.run(["git", "commit", "-q", "--allow-empty", "--fixup", "HEAD"], cwd=self.root,
                                env=clean_environment(), capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_squash_merge_commit_commits(self):
        """Review round 4: `git merge --squash` then `git commit` keeps git's SQUASH_MSG."""
        self.assertEqual(self.commit("fix: base\n").returncode, 0)
        git("checkout", "-q", "-b", "side", cwd=self.root)
        (self.root / "s.txt").write_text("s\n", encoding="utf-8")
        git("add", "s.txt", cwd=self.root)
        self.assertEqual(self.commit("fix: " + "s" * 67 + "\n").returncode, 0)
        git("checkout", "-q", "-", cwd=self.root)
        git("merge", "-q", "--squash", "side", cwd=self.root)
        result = subprocess.run(["git", "commit", "-q", "--no-edit"], cwd=self.root,
                                env=clean_environment(), capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_verbose_commit_is_measured_above_the_scissors_only(self):
        """`git commit -v` appends the diff below a scissors line, and git drops it after the hook runs."""
        (self.root / "wide.txt").write_text("w" * 90 + "\n", encoding="utf-8")
        git("add", "wide.txt", cwd=self.root)
        result = subprocess.run(["git", "commit", "-q", "-v", "-e", "-m", "fix: a subject"], cwd=self.root,
                                env=clean_environment(GIT_EDITOR="true"), capture_output=True, text=True,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_trailing_spaces_git_strips_do_not_count(self):
        """Review round 2: git keeps the line without its trailing spaces, so 72 + 3 spaces is 72."""
        result = self.commit("fix: a subject\n\n" + "t" * 72 + "   \n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_refusal_quotes_the_line_it_measured(self):
        result = self.commit("fix: a subject\n\n\n\n" + "q" * 73 + "\n")
        self.assertIn("qqqqqqqqqq", result.stderr)

    def test_a_merge_commit_is_not_measured(self):
        """Review round 2: git runs commit-msg for `git merge`; sd-ship's catch-up merge must not abort."""
        self.assertEqual(self.commit("fix: base\n").returncode, 0)
        git("checkout", "-q", "-b", "side", cwd=self.root)
        self.assertEqual(self.commit("fix: side\n").returncode, 0)
        git("checkout", "-q", "-", cwd=self.root)
        result = subprocess.run(["git", "merge", "-q", "--no-ff", "-m", "Merge " + "m" * 80, "side"],
                                cwd=self.root, env=clean_environment(), capture_output=True, text=True,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_editor_trailing_spaces_do_not_count(self):
        """Review round 3: on the editor path the hook sees the spaces git strips after it."""
        editor = "sh -c 'printf \"fix: a subject\\n\\n%s   \\n\" " + "e" * 72 + " > \"$1\"' -"
        result = subprocess.run(["git", "commit", "-q", "--allow-empty", "-e", "-m", "x"], cwd=self.root,
                                env=clean_environment(GIT_EDITOR=editor), capture_output=True, text=True,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_verbose_commit_with_another_comment_char_commits(self):
        """Review round 3: core.commentChar moves both the comment lines and the scissors."""
        (self.root / "wide.txt").write_text("w" * 90 + "\n", encoding="utf-8")
        git("add", "wide.txt", cwd=self.root)
        result = subprocess.run(["git", "-c", "core.commentChar=;", "commit", "-q", "-v", "-e", "-m", "fix: a"],
                                cwd=self.root, env=clean_environment(GIT_EDITOR="true"), capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_revert_of_a_long_subject_commits(self):
        """Review round 3: `Revert "<subject>"` is git's own line; a 70-character subject makes 80."""
        (self.root / "undo.txt").write_text("u\n", encoding="utf-8")
        git("add", "undo.txt", cwd=self.root)
        self.assertEqual(self.commit("fix: " + "r" * 65 + "\n").returncode, 0)
        # `git revert` itself runs no commit-msg; a revert that stops (-n, or a conflict) and is then
        # finished by `git commit` does, with REVERT_HEAD set.
        git("revert", "-n", "HEAD", cwd=self.root)
        result = subprocess.run(["git", "commit", "-q", "--no-edit"], cwd=self.root,
                                env=clean_environment(), capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_kept_heading_does_not_unfence_a_code_line(self):
        """Review round 5: under -F git keeps `# H`, which keeps the line fenced; dropping it unfenced it."""
        result = self.commit("fix: a subject\n\n- item\n# H\n  ```\n" + "i" * 80 + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_edited_heading_git_drops_does_not_close_a_fence(self):
        """The editor path drops `# H`, which keeps the list's fence open; keeping it closed the fence."""
        draft = self.root.parent / f"{self.root.name}-draft"
        draft.write_text("fix: a subject\n\n- item\n  ```\n# H\n  " + "i" * 80 + "\n", encoding="utf-8")
        self.addCleanup(draft.unlink)
        result = subprocess.run(["git", "commit", "-q", "--allow-empty", "-e", "-m", "x"], cwd=self.root,
                                env=clean_environment(GIT_EDITOR=f"cp {shlex.quote(str(draft))}"), capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_long_bare_url_or_fenced_line_still_commits(self):
        url = "https://example.test/" + "a" * 80
        result = self.commit(f"fix: a subject\n\n{url}\n\n```\n{'y' * 90}\n```\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_make_hooks_replaces_a_dangling_link(self):
        """pack sd:10212: 19 clones linked a retired pack path, and git skipped the hook silently."""
        link = self.root / ".git" / "hooks" / "commit-msg"
        link.unlink()
        link.symlink_to(self.root / "gone" / "sd-ai-command-pack" / "hooks" / "commit-msg")
        made = subprocess.run(["make", "hooks"], cwd=self.root, env=clean_environment(),
                              capture_output=True, text=True, check=False)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        self.assertEqual(os.readlink(link), "../../hooks/commit-msg")

    def test_a_message_with_no_checked_trailer_commits_as_before(self):
        for message in (PLAIN, QUOTED):
            with self.subTest(message=message.splitlines()[0]):
                result = self.commit(message)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("", result.stderr)

    def test_the_pre_commit_bypass_does_not_skip_the_trailer_check(self):
        result = self.commit(SPLIT, SD_SKIP_HOOKS="1")
        self.assertNotEqual(result.returncode, 0, "SD_SKIP_HOOKS skipped the trailer check")
        self.assertEqual(0, self.commits(), "a commit landed")


if __name__ == "__main__":
    unittest.main()
