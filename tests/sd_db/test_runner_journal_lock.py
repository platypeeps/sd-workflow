"""One lock opener, hardened: `runner_journal.lock` is the only flock in both packages.

The lock file is opened without following a link, checked on the open
descriptor (a regular file owned by this user, with one link), and the flock
is taken on that same descriptor. A link, a second link, a FIFO or a foreign
file at the path refuses, and a link swapped in during any open of the path,
not only the first, never has its target created. A link is a link under
either spelling a kernel gives it, ELOOP here or EMLINK on a BSD. Every other
failure to open the path names the system error it was, never the file's
owner.
"""

import ast
import errno
import io
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from sd_db import initialise, runner_journal, ship
from sd_db.runner import RunnerRefused
from sd_db.testing.wire import hub_only
from sd_db.workflow import WorkflowError

from . import support

REPO_ROOT = support.PACK

#: The one opener, and the package tree that may take a lock.
OPENER = "lib/sd_db/runner_journal.py"
PACKAGES = ("lib/sd_db",)


def foreign(details):
    """`details` as another user would own it: st_uid moved by one."""
    values = list(details[:10])
    values[4] = details.st_uid + 1
    return os.stat_result(tuple(values))


class HardenedLock(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.lock = self.root / "runner.lock"
        self.target = self.root / "lock-target"

    def tearDown(self):
        shutil.rmtree(self.root)

    @contextmanager
    def swapped_at_open(self, replacement, nth):
        """Put `replacement` at the lock path for exactly the n-th open of it."""
        aside = self.lock.with_name(self.lock.name + ".aside")
        seen = []

        def around(real):
            def opener(file, *args, **kwargs):
                if not isinstance(file, (str, os.PathLike)) or os.fspath(file) != os.fspath(self.lock):
                    return real(file, *args, **kwargs)
                seen.append(True)
                if len(seen) != nth:
                    return real(file, *args, **kwargs)
                os.rename(self.lock, aside)
                os.rename(replacement, self.lock)
                try:
                    return real(file, *args, **kwargs)
                finally:
                    os.rename(self.lock, replacement)
                    os.rename(aside, self.lock)
            return opener

        with patch("os.open", around(os.open)), patch("io.open", around(io.open)), \
             patch("builtins.open", around(open)):
            yield seen

    def test_the_lock_file_is_a_private_regular_file_and_excludes_a_second_holder(self):
        with runner_journal.lock(self.lock):
            details = self.lock.stat()
            self.assertTrue(stat.S_ISREG(details.st_mode))
            self.assertEqual(stat.S_IMODE(details.st_mode), 0o600)
            with self.assertRaisesRegex(RunnerRefused, "runner ownership lock held"):
                with runner_journal.lock(self.lock, blocking=False):
                    self.fail("second owner")
        with runner_journal.lock(self.lock, blocking=False):
            pass

    def test_a_link_at_the_lock_path_refuses_and_creates_no_target(self):
        self.lock.symlink_to(self.target)
        with self.assertRaisesRegex(RunnerRefused, "runner lock has unsafe ownership or type"):
            with runner_journal.lock(self.lock):
                self.fail("locked through a link")
        self.assertFalse(os.path.lexists(self.target))

    def test_a_link_present_only_while_a_later_open_creates_no_target(self):
        """Swap during the n-th open of the lock path, not only the first."""
        for nth in (1, 2, 3):
            with self.subTest(nth=nth):
                link = self.root / "staged-link"
                for leftover in (self.target, link, self.lock):
                    if os.path.lexists(leftover):
                        os.unlink(leftover)
                self.lock.write_bytes(b"")
                link.symlink_to(self.target)
                with self.swapped_at_open(link, nth) as seen:
                    try:
                        with runner_journal.lock(self.lock):
                            pass
                    except RunnerRefused:
                        pass
                self.assertFalse(os.path.lexists(self.target), f"target created through a link swapped in at open {nth}")
                self.assertTrue(seen)

    def test_a_second_link_present_only_while_the_lock_opens_refuses(self):
        self.lock.write_bytes(b"")
        staged = self.root / "staged-file"
        staged.write_bytes(b"")
        os.link(staged, self.root / "staged-file-second")
        with self.swapped_at_open(staged, 1) as seen:
            with self.assertRaisesRegex(RunnerRefused, "unsafe ownership or type"):
                with runner_journal.lock(self.lock):
                    self.fail("locked a file with a second name")
        self.assertEqual(seen, [True])
        self.assertEqual((self.lock.stat().st_nlink, staged.stat().st_nlink), (1, 2))

    def test_a_fifo_at_the_lock_path_refuses(self):
        os.mkfifo(self.lock)
        with self.assertRaisesRegex(RunnerRefused, "unsafe ownership or type"):
            with runner_journal.lock(self.lock):
                self.fail("locked a FIFO")
        self.assertTrue(stat.S_ISFIFO(self.lock.lstat().st_mode))

    def test_a_lock_file_owned_by_another_user_refuses(self):
        """No second user is at hand: the descriptor's stat is answered as another user's."""
        self.lock.write_bytes(b"")
        identity = (self.lock.stat().st_dev, self.lock.stat().st_ino)
        real = os.fstat

        def fstat(descriptor):
            details = real(descriptor)
            return foreign(details) if (details.st_dev, details.st_ino) == identity else details

        with patch("os.fstat", fstat):
            with self.assertRaisesRegex(RunnerRefused, "unsafe ownership or type"):
                with runner_journal.lock(self.lock):
                    self.fail("locked a file another user owns")
        with runner_journal.lock(self.lock, blocking=False):
            pass

    def test_the_refusal_names_the_caller_and_carries_its_error(self):
        self.lock.symlink_to(self.target)
        with self.assertRaisesRegex(WorkflowError, "prune lock has unsafe ownership or type"):
            with runner_journal.lock(self.lock, noun="prune", error=WorkflowError):
                self.fail("locked through a link")
        with runner_journal.lock(self.root / "held.lock"):
            with self.assertRaisesRegex(WorkflowError, "^someone else$"):
                with runner_journal.lock(self.root / "held.lock", blocking=False, error=WorkflowError, held="someone else"):
                    self.fail("second owner")

    @hub_only
    def test_the_ship_repository_lock_is_the_shared_lock(self):
        database = self.root / "sd.db"
        initialise(database)  # sd:3075: the lock reads the lane host first
        locks = self.root / "ship-locks"
        with ship.repository_lock(database, "fixture/repo"):
            names = [entry for entry in locks.iterdir()]
            self.assertEqual(len(names), 1)
            self.assertTrue(stat.S_ISREG(names[0].stat().st_mode))
            with self.assertRaisesRegex(WorkflowError, "another ship operation owns this repository"):
                with ship.repository_lock(database, "fixture/repo"):
                    self.fail("second owner")
        names[0].unlink()
        names[0].symlink_to(self.target)
        with self.assertRaisesRegex(WorkflowError, "ship lock has unsafe ownership or type"):
            with ship.repository_lock(database, "fixture/repo"):
                self.fail("locked through a link")
        self.assertFalse(os.path.lexists(self.target))

    def test_an_unopenable_lock_path_names_the_system_error_not_its_owner(self):
        """An unwritable lock directory is not a verdict about the file's owner."""
        closed = self.root / "closed"
        closed.mkdir(mode=0o500)
        try:
            with self.assertRaises(RunnerRefused) as refused:
                with runner_journal.lock(closed / "runner.lock"):
                    self.fail("locked in an unwritable directory")
        finally:
            closed.chmod(0o700)
        self.assertIn("runner lock cannot be opened", str(refused.exception))
        self.assertIn(os.strerror(errno.EACCES), str(refused.exception))
        self.assertNotIn("unsafe ownership", str(refused.exception))
        self.assertEqual(refused.exception.__cause__.errno, errno.EACCES)

    def test_a_directory_at_the_lock_path_names_the_system_error(self):
        self.lock.mkdir()
        with self.assertRaisesRegex(WorkflowError, "prune lock cannot be opened.*" + os.strerror(errno.EISDIR)):
            with runner_journal.lock(self.lock, noun="prune", error=WorkflowError):
                self.fail("locked a directory")

    def test_a_read_only_lock_directory_names_the_read_only_filesystem(self):
        """No read-only mount is at hand: the open is answered as one would."""
        real = os.open

        def refusing(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and os.fspath(file) == os.fspath(self.lock):
                raise OSError(errno.EROFS, os.strerror(errno.EROFS), os.fspath(file))
            return real(file, *args, **kwargs)

        with patch("os.open", refusing):
            with self.assertRaisesRegex(RunnerRefused, "runner lock cannot be opened.*" + os.strerror(errno.EROFS)):
                with runner_journal.lock(self.lock):
                    self.fail("locked on a read-only filesystem")

    def test_the_bsd_spelling_of_a_link_under_o_nofollow_is_still_a_link_verdict(self):
        """No BSD kernel is at hand: the open is answered as one would.

        FreeBSD answers O_NOFOLLOW on a symlink with EMLINK where this machine
        answers ELOOP (62), and `man 2 open` here does not list EMLINK at all.
        So no real link can walk that arm of the opener on the platform the
        suite runs on, and without this it is an unexercised arm in a refusal:
        deleting `errno.EMLINK` from the opener left all 1149 tests green
        (sd:857). A link is a link whatever the kernel calls it, so the verdict
        is the link verdict and never the system error.
        """
        real = os.open

        def bsd(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and os.fspath(file) == os.fspath(self.lock):
                raise OSError(errno.EMLINK, os.strerror(errno.EMLINK), os.fspath(file))
            return real(file, *args, **kwargs)

        with patch("os.open", bsd):
            with self.assertRaises(RunnerRefused) as refused:
                with runner_journal.lock(self.lock):
                    self.fail("locked through a link a BSD kernel reported as EMLINK")
        self.assertIn("runner lock has unsafe ownership or type", str(refused.exception))
        self.assertNotIn("cannot be opened", str(refused.exception))
        self.assertNotIn(os.strerror(errno.EMLINK), str(refused.exception))
        self.assertEqual(refused.exception.__cause__.errno, errno.EMLINK)

    def test_a_held_lock_keeps_the_holder_that_refused_it(self):
        """control_gate reads this cause to tell contention from an unopenable lock."""
        with runner_journal.lock(self.lock):
            with self.assertRaises(RunnerRefused) as refused:
                with runner_journal.lock(self.lock, blocking=False):
                    self.fail("second owner")
        self.assertIsInstance(refused.exception.__cause__, BlockingIOError)

    def test_a_lock_directory_the_opener_creates_is_private(self):
        deeper = self.root / "made" / "runner.lock"
        with runner_journal.lock(deeper):
            self.assertEqual(stat.S_IMODE(deeper.parent.stat().st_mode), 0o700)

    def test_a_lock_directory_that_cannot_be_made_names_the_system_error(self):
        """A file where the lock directory goes made `mkdir` raise a bare
        `OSError` past the refusal every caller catches (sd:1219)."""
        (self.root / "made").write_text("a file, not a directory\n")
        with self.assertRaises(RunnerRefused) as refused:
            with runner_journal.lock(self.root / "made" / "runner.lock"):
                self.fail("locked under a file")
        self.assertIn("runner lock cannot be opened", str(refused.exception))
        self.assertIsInstance(refused.exception.__cause__, OSError)

    def test_a_journal_directory_that_is_a_file_or_a_link_is_refused(self):
        """`persist` made the journal directory with a bare `mkdir`: a file
        there escaped as `FileExistsError`, and a link was followed and
        written through, though `records` refuses that same link (sd:1219)."""
        database = self.root / "sd.db"
        record = {"id": "a" * 32}
        journal = runner_journal.directory(database)
        journal.write_text("a file, not a directory\n")
        with self.assertRaisesRegex(RunnerRefused, "runner journal directory"):
            runner_journal.persist(database, record)
        journal.unlink()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        journal.symlink_to(elsewhere)
        with self.assertRaisesRegex(RunnerRefused, "runner journal directory"):
            runner_journal.persist(database, record)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_every_lock_in_the_package_goes_through_the_one_opener(self):
        """Code, not prose: only the opener may reach fcntl, under any spelling.

        Imports, not call sites: `from fcntl import flock` and an aliased
        import are locks too. Enumerated from the filesystem, so this needs a
        source tree and not a git checkout.
        """
        trees = [REPO_ROOT / package for package in PACKAGES]
        for tree in trees:
            self.assertTrue(tree.is_dir(), f"{tree} is part of this checkout; the walk cannot cover it")
        importers = set()
        for tree in trees:
            for source in tree.rglob("*.py"):
                for node in ast.walk(ast.parse(source.read_text(), filename=str(source))):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [node.module or ""] if not node.level else []
                    else:
                        continue
                    if any(name == "fcntl" or name.startswith("fcntl.") for name in names):
                        importers.add(str(source.relative_to(REPO_ROOT)))
        self.assertEqual(sorted(importers), [OPENER])


class FullSync(unittest.TestCase):
    """`full_sync` asks the drive to flush its cache, where the platform can (sd:8657)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.descriptor = os.open(self.root / "file", os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, self.descriptor)

    def synced(self, fault=None):
        """The (F_FULLFSYNC calls, fsync calls) one `full_sync` makes, its fcntl failing with `fault`."""
        calls: list[tuple[int, int]] = []
        plain: list[int] = []

        class Fcntl:
            F_FULLFSYNC = 51

            @staticmethod
            def fcntl(descriptor, command):
                calls.append((descriptor, command))
                if fault is not None:
                    raise OSError(fault, os.strerror(fault))
                return 0
        with patch.object(runner_journal, "fcntl", Fcntl), patch.object(runner_journal.os, "fsync", plain.append):
            runner_journal.full_sync(self.descriptor)
        return calls, plain

    def test_it_takes_f_fullfsync_where_the_platform_has_it(self):
        self.assertEqual(self.synced(), ([(self.descriptor, 51)], []))

    def test_a_file_system_without_it_gets_fsync(self):
        for fault in (errno.ENOTSUP, errno.EINVAL, errno.ENOTTY):
            with self.subTest(errno.errorcode[fault]):
                self.assertEqual(self.synced(fault), ([(self.descriptor, 51)], [self.descriptor]))

    def test_any_other_fault_is_raised_not_downgraded(self):
        with self.assertRaises(OSError) as raised:
            self.synced(errno.EIO)
        self.assertEqual(raised.exception.errno, errno.EIO)

    def test_a_platform_without_it_gets_fsync(self):
        plain: list[int] = []
        with patch.object(runner_journal, "fcntl", object()), patch.object(runner_journal.os, "fsync", plain.append):
            runner_journal.full_sync(self.descriptor)
        self.assertEqual(plain, [self.descriptor])
