"""Pack tools stay out of repositories the sd database does not mark managed (sd:1620).

The operator sets `repo.managed` by hand on their own repositories (sd:1619);
the rest must not use any pack capability. A fleet walk skips an unmanaged
row, and a direct call in an unmanaged checkout refuses, naming the flag. Not
knowing -- no library, no database, no row -- is no reason to deny.

A real database, as `test_sd_issue_guard` uses: the claim is about which row
decides, and a double would only restate the answer.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess  # nosec B404 - fixed argv, running git
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_ci  # noqa: E402
import sd_db  # noqa: E402 - installed into this virtualenv by `make setup`
import sd_fleet  # noqa: E402
import sd_lane  # noqa: E402
import sd_lib  # noqa: E402

BIN = REPO_ROOT / "bin"


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name).resolve()
        patched = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)

    def checkout(self, name: str, origin: str | None = None) -> pathlib.Path:
        root = self.home / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)  # nosec B603 B607
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin",  # nosec B603 B607
                        f"https://github.com/example/{origin or name}"], check=True)
        return root

    def register(self, root: pathlib.Path, origin: str | None = None, **columns: object) -> None:
        sd_db.initialise(home=self.home)
        connection = sd_db.connect(home=self.home)
        try:
            key = sd_db.add_repo(connection, root, home=self.home)
            sd_db.writes.upsert_repo(connection, key, remote=f"https://github.com/example/{origin or root.name}",
                                     **columns)
            connection.commit()
        finally:
            connection.close()


class ADirectCall(Fixture):
    def test_an_unmanaged_checkout_refuses_naming_the_flag(self) -> None:
        root = self.checkout("theirs")
        self.register(root, managed=0)
        said = sd_lib.unmanaged(root)
        self.assertIsNotNone(said, "an unmanaged checkout was let through")
        self.assertIn("repo.managed = no", said)
        self.assertIn("`sd fleet standard` in ~/theirs", said)

    def test_a_managed_checkout_proceeds(self) -> None:
        root = self.checkout("mine")
        self.register(root, managed=1)
        self.assertIsNone(sd_lib.unmanaged(root))

    def test_no_database_proceeds_in_silence(self) -> None:
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.assertIsNone(sd_lib.unmanaged(self.checkout("unregistered")))
        self.assertEqual(said.getvalue(), "")

    def test_no_row_proceeds_with_one_warning_naming_the_flag(self) -> None:
        root = self.checkout("unregistered")
        self.register(self.checkout("other"), managed=0)
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.assertIsNone(sd_lib.unmanaged(root))
        self.assertEqual(said.getvalue().count("\n"), 1, said.getvalue())
        self.assertIn("(repo.managed); proceeding", said.getvalue())
        self.assertIn("then `sd fleet standard` in ", said.getvalue())


class TheDirectCommands(Fixture):
    """sd:2566. sd-ship, sd-check and sd-status refuse an unmanaged
    checkout before any network call, check or write; `--explain` and
    `--dry-run` included. sd-review runs there (pack sd:5953). A checkout
    with no row proceeds without a word."""

    COMMANDS = {
        "sd-ship": ["observe", "--item", "1", "--json"],
        "sd-review": ["--scope", "branch", "--explain", "--json"],
        "sd-check": ["--dry-run", "--json"],
        "sd-status": ["--json"],
    }

    def run_command(self, name: str, root: pathlib.Path, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(BIN / name), *self.COMMANDS[name], *extra],  # nosec B603
                              cwd=root, capture_output=True, text=True, timeout=120, check=False)

    def test_each_refuses_an_unmanaged_checkout_naming_the_flag(self) -> None:
        root = self.checkout("theirs")
        self.register(root, managed=0)
        for name in ("sd-ship",):  # sd-check and sd-status run there since sd:9100
            with self.subTest(name):
                done = self.run_command(name, root)
                self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
                said = done.stdout + done.stderr
                self.assertIn("repo.managed = no", said)
                self.assertIn("`sd fleet standard` in ~/theirs", said)
        refused = json.loads(self.run_command("sd-ship", root).stdout)
        self.assertEqual(refused["workflow"]["blocker"]["code"], "unmanaged_repository")
        # pack sd:7732: `repo managed PATH yes` refuses once sd:7710 lands; the standard sets the flag.
        self.assertIn("sd fleet standard", refused["workflow"]["next_action"])

    def test_sd_review_reviews_an_unmanaged_checkout_and_sd_ship_still_refuses(self) -> None:
        """pack sd:5953: sd-review reads a diff and writes no repository file."""
        root = self.checkout("theirs")
        self.register(root, managed=0)
        git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.test"]
        for step in (["checkout", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "base"],
                     ["checkout", "-q", "-b", "work"]):
            subprocess.run([*git, *step], check=True)  # nosec B603 B607
        (root / "a.txt").write_text("a\n")
        subprocess.run([*git, "add", "a.txt"], check=True)  # nosec B603 B607
        subprocess.run([*git, "commit", "-q", "-m", "change"], check=True)  # nosec B603 B607
        done = self.run_command("sd-review", root)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("repo.managed", done.stdout + done.stderr)
        self.assertEqual(json.loads(done.stdout)["scope"], "branch")
        self.assertEqual(subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True,  # nosec B603 B607
                                        check=True).stdout, "", "the review wrote into the checkout")
        self.assertIn("repo.managed = no", (refused := self.run_command("sd-ship", root)).stdout)
        self.assertEqual(refused.returncode, 3)

    def test_each_passes_a_managed_checkout_and_one_with_no_row_in_silence(self) -> None:
        mine, unregistered = self.checkout("mine"), self.checkout("unregistered")
        self.register(mine, managed=1)
        for root in (mine, unregistered):
            for name in self.COMMANDS:
                with self.subTest(name, checkout=root.name):
                    self.assertNotIn("repo.managed", (done := self.run_command(name, root)).stdout + done.stderr)

    def test_sd_check_runs_an_unmanaged_checkout_and_refuses_only_a_receipt(self) -> None:
        """sd:9100: a plain local run, no receipt and no `sd/local-gate` status there."""
        root = self.checkout("theirs")
        self.register(root, managed=0)
        done = self.run_command("sd-check", root)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("repo.managed", done.stdout + done.stderr)
        refused = self.run_command("sd-check", root, "--record-receipt")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("repo.managed = no", refused.stderr)
        self.assertIn("records no receipt", refused.stderr)

    def test_sd_status_reads_an_unmanaged_checkout(self) -> None:
        """sd:9100: sd-status is read-only, so it reports there as anywhere."""
        root = self.checkout("theirs")
        self.register(root, managed=0)
        done = self.run_command("sd-status", root)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("repo.managed", done.stdout + done.stderr)
        self.assertIn("setup", json.loads(done.stdout))

    def test_sd_ship_reads_the_database_it_is_given(self) -> None:
        root = self.checkout("theirs")
        self.register(root, managed=1)
        other = self.home / "other"
        other.mkdir()
        sd_db.initialise(home=other)
        connection = sd_db.connect(home=other)
        try:
            sd_db.writes.upsert_repo(connection, sd_db.add_repo(connection, root, home=self.home),
                                     remote="https://github.com/example/theirs", managed=0)
            connection.commit()
            database = connection.execute("PRAGMA database_list").fetchone()[2]
        finally:
            connection.close()
        self.assertIsNone(sd_lib.unmanaged(root, warn=False))
        self.assertIn("repo.managed = no", sd_lib.unmanaged(root, warn=False, database=database) or "")
        done = self.run_command("sd-ship", root, "--database", database)
        self.assertIn("repo.managed = no", done.stdout, done.stderr)

    def test_lane_enqueue_refuses_before_queueing(self) -> None:
        root = self.checkout("theirs")
        self.register(root, managed=0)
        body = self.home / "body.md"
        body.write_text("Item: sd:1\n")
        environ = {"HOME": str(self.home), sd_lane.ROOT_VARIABLE: str(self.home / "lanes")}
        with self.assertRaises(sd_lane.LaneError) as refused:
            sd_lane.enqueue_entry(root, 1, "A title", body, environ, claim="deliver")
        self.assertIn("repo.managed = no", str(refused.exception))
        self.assertFalse((self.home / "lanes").exists(), "an unmanaged entry reached the queue folder")


class SdCiLocal(Fixture):
    def test_an_unmanaged_checkout_refuses_before_any_github_write(self) -> None:
        theirs = self.checkout("theirs")
        self.register(theirs, managed=0)
        with self.assertRaises(sd_ci.CiRefusal) as refused:
            sd_ci.ci_step(theirs, None)
        self.assertIn("repo.managed = no", str(refused.exception))

    def test_a_managed_checkout_reads_its_ci_row(self) -> None:
        mine = self.checkout("mine")
        self.register(mine, managed=1)
        self.assertEqual(sd_ci.ci_step(mine, None).subject, "repo.ci")


class AFleetWalk(Fixture):
    def test_the_stamp_skips_an_unmanaged_auto_row(self) -> None:
        mine, theirs = self.checkout("mine"), self.checkout("theirs")
        self.register(mine, managed=1, runner_merge="auto")
        self.register(theirs, managed=0, runner_merge="auto")
        self.assertEqual([checkout.name for checkout, _ in sd_fleet.auto_rows()], ["mine"])

    def test_a_write_in_an_unmanaged_checkout_refuses_naming_the_flag(self) -> None:
        theirs = self.checkout("theirs")
        self.register(theirs, managed=0, runner_merge="auto")
        with self.assertRaises(sd_fleet.FleetRefusal) as refused:
            sd_fleet.here(sd_fleet.auto_rows(), theirs)
        self.assertIn("repo.managed = no", str(refused.exception))

    def test_an_unmanaged_checkout_sharing_a_managed_rows_origin_refuses(self) -> None:
        """The lane review of sd:1620: `here` matched by origin first, so an
        unmanaged checkout of the same repository as a managed auto row took
        that row and wrote. Management is per row; the checkout's own row decides."""
        mine, theirs = self.checkout("mine"), self.checkout("theirs", origin="mine")
        self.register(mine, managed=1, runner_merge="auto")
        self.register(theirs, origin="mine", managed=0, runner_merge="auto")
        with self.assertRaises(sd_fleet.FleetRefusal) as refused:
            sd_fleet.here(sd_fleet.auto_rows(), theirs)
        self.assertIn("repo.managed = no", str(refused.exception))
        self.assertEqual(sd_fleet.here(sd_fleet.auto_rows(), mine)[0], mine)

    def test_an_unregistered_worktree_still_resolves_by_origin(self) -> None:
        mine, worktree = self.checkout("mine"), self.checkout("worktree", origin="mine")
        self.register(mine, managed=1, runner_merge="auto")
        self.assertEqual(sd_fleet.here(sd_fleet.auto_rows(), worktree)[0], worktree)

    def test_a_row_without_the_column_stays_in(self) -> None:
        class Row(dict):
            keys = dict.keys

        self.assertEqual(sd_lib.managed_rows([Row(path="old"), Row(path="no", managed=0)]), [{"path": "old"}])


if __name__ == "__main__":
    unittest.main()
