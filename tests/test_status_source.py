"""Criterion 13's readers: the row, and the two words for the tree.

Two questions, and the suite is organised by them.

**What does a row that cannot be read mean?** Five defects on this item shared
one shape: a guard that answered a question about form where the question was
about substance. Here the shape is a missing database or a missing row read as
"no row, so the item is open", which hands delivered work back to
`sd-review --scope planning` and planning to be done again. The two are given
two different meanings on purpose, and each one is asserted:

    no database at all      the designed database-free case -- ask git alone
    a database with no row   the item is lost; `unknown`, and named

**What does the tree still say?** A `status:` line that survived on a retained
worktree is *stale* when it disagrees with the row, and a row that says `done`
while no commit carries a closing trailer is *unmarked*. Both words are the
criterion's and both come out of `sd-status`.

Nothing here mocks git or sqlite: the repositories are real, and the rows are
written through `sd_db` with the same calls the migration uses.
"""

from __future__ import annotations

import datetime
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_db  # noqa: E402 - installed into this virtualenv by `make setup`
import sd_lib  # noqa: E402


def _load(name: str, module_name: str) -> Any:
    """Import a `bin/` executable, which has no `.py` suffix to import by name."""
    path = REPO_ROOT / "bin" / name
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_file_location(module_name, str(path), loader=loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


status_tool = _load("sd-status", "sd_status_for_status_source")
review_tool = _load("sd-review", "sd_review_for_status_source")
lint = _load("sd-docs-lint", "sd_docs_lint_for_status_source")

ITEM = "2026-09-05-a-thing"
OTHER = "2026-09-04-another-thing"


def prd(status: str | None, *, branch: str = "", criteria: bool = True) -> str:
    """An item's `prd.md`. `status=None` is the file the retire step leaves."""
    lines = ["---", "title: A thing"]
    if status is not None:
        lines.append(f"status: {status}")
    lines.append("created: 2026-09-05")
    if branch:
        lines.append(f"branch: {branch}")
    lines += ["---", "", "# PRD", ""]
    if criteria:
        lines += ["## Acceptance criteria", "", "- [x] it works", ""]
    return "\n".join(lines)


def merge_message(*trailers: str) -> str:
    """A merge message shaped as `sd-ship` writes one: subject, body, trailers."""
    return "feat: the slice (#1)\n\nWhat this merge settles.\n\n" + "\n".join(trailers)


class Fixture(unittest.TestCase):
    """One real repository, one real item, and a `HOME` nothing else shares."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        # The database is found through `$HOME`, read at call time. Without
        # this the suite would read the operator's own rows.
        patched = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)

        self.root = self.tmp / "repo"
        self.work = self.root / "docs" / "work"
        self.work.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test User")
        self.git("config", "commit.gpgsign", "false")
        self.item = self.work / ITEM
        self.item.mkdir()
        self.write(prd("planning"))
        self.commit("chore: the item")

    # ---------------------------------------------------------------- helpers

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=str(self.root), check=True, capture_output=True, text=True
        )
        return done.stdout.strip()

    def write(self, text: str) -> None:
        (self.item / "prd.md").write_text(text, encoding="utf-8")

    def commit(self, subject: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject)

    def identity(self, name: str = ITEM) -> str:
        """The row key, spelled out here rather than asked of the code."""
        return f"{self.root}::docs/work/{name}/prd.md"

    def database(self) -> Any:
        sd_db.initialise(home=self.home)
        connection = sd_db.connect(home=self.home)
        self.addCleanup(connection.close)
        sd_db.upsert_repo(connection, str(self.root))
        return connection

    def seed(self, status: str, name: str = ITEM, *, branch: str | None = "main") -> Any:
        connection = getattr(self, "_connection", None) or self.database()
        self._connection = connection
        sd_db.upsert_item(
            connection,
            source=sd_lib.ITEM_ROW_SOURCE,
            external_id=self.identity(name),
            kind="work",
            title="A thing",
            status=status,
            who="the test",
            created_at="2026-09-05T00:00:00+00:00",
            repo=str(self.root),
            path=f"docs/work/{name}/prd.md",
            branch=branch,
        )
        return connection

    def only(self, root: pathlib.Path | None = None) -> sd_lib.WorkItem:
        found = [i for i in sd_lib.work_items(root or self.root) if i.slug == "a-thing"]
        self.assertEqual(len(found), 1, "the fixture holds exactly one item")
        return found[0]

    def picked(self, root: pathlib.Path | None = None) -> list[str]:
        """What `source:bin/sd-review::resolve_subject` picks, spelled its way."""
        return [
            i.path.name
            for i in sd_lib.work_items(root or self.root)
            if i.status in ("planning", "in_progress")
        ]

    def remote(self, name: str = "origin.git") -> pathlib.Path:
        """A bare repository the original pushes `main` to, as `origin`.

        The URL is the bare path itself, so `git remote get-url origin` in a
        clone answers with exactly the string the `repo` row will carry.
        """
        bare = self.tmp / name
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(bare)],
            check=True, capture_output=True,
        )
        self.git("remote", "add", name.removesuffix(".git"), str(bare))
        self.git("push", "-q", name.removesuffix(".git"), "main")
        return bare

    def clone(self, origin: pathlib.Path, name: str = "clone") -> pathlib.Path:
        """A checkout of `origin` at a path nothing ever registered."""
        where = self.tmp / name
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(where)],
            check=True, capture_output=True,
        )
        return where

    def unreadable(self, root: pathlib.Path) -> list[dict[str, Any]]:
        """`sd-status`'s `status-unreadable` rows for `root`, GitHub unread."""
        sections = {
            "work": status_tool.work_section(root),
            "pull_requests": {"repo": "acme/widget", "pull_requests": []},
            "merged_pull_requests": {"repo": "acme/widget", "pull_requests": []},
            "protection": {"default_branch": "main", "gaps": [], "detail": {},
                           "available": False, "reason": "gh is not installed"},
            "issues": {"available": False, "needs_you": [], "other": []},
        }
        inventory = status_tool.actionable_inventory(
            root, sections, datetime.date(2026, 9, 17))
        return [row for row in inventory.rows if row["check"] == "status-unreadable"]

    def rendered(self) -> str:
        stream = io.StringIO()
        status_tool._render_work(status_tool.work_section(self.root), stream.write)
        return stream.getvalue()


class TheRowDecides(Fixture):
    """Requirement 2, on a machine that has the database."""

    def test_explicit_relink_preserves_status_and_followups(self) -> None:
        import sd_db.progress as progress
        import sd_handoff_rows

        connection = self.seed("in_progress")
        row = sd_db.writes.item_by_external(connection, sd_lib.ITEM_ROW_SOURCE, self.identity())
        note = sd_db.add_note(connection, row["id"], "followup", "Keep the original history")
        moved = self.work / "2026-09-08-renamed"
        self.item.rename(moved)
        progress.relink_artifact(connection, row["id"], "docs/work/2026-09-08-renamed/prd.md", who="user")
        self.assertEqual(sd_lib.status_report(moved).status, "in_progress")
        linked = sd_handoff_rows.item_for(connection, sd_db, self.root, moved)
        self.assertEqual(linked["id"], row["id"])
        self.assertEqual(linked["external_id"], self.identity())
        self.assertIn(note, [entry["id"] for entry in sd_db.reads.item_notes(connection, row["id"])])

    def test_archive_location_does_not_override_a_live_row(self) -> None:
        self.seed("in_progress")
        archived = self.work / "archive" / "2026-09" / ITEM
        archived.parent.mkdir(parents=True)
        self.item.rename(archived)
        report = sd_lib.status_report(archived)
        self.assertTrue(report.archived)
        self.assertEqual(report.status, "in_progress")

    def test_verified_cancellation_needs_no_status_only_commit(self) -> None:
        import sd_db.progress as progress

        self.write(prd(None))
        connection = self.seed("in_progress")
        row = sd_db.writes.item_by_external(connection, sd_lib.ITEM_ROW_SOURCE, self.identity())
        progress.cancel_work(connection, row["id"], reason="No longer needed", who="user")
        with mock.patch.object(sd_lib, "delivered", side_effect=AssertionError("asked Git")):
            item = self.only()
        self.assertEqual(item.status, "done")
        self.assertEqual(item.inconsistencies, ())

    def test_the_row_answers_and_the_line_does_not(self) -> None:
        self.seed("ready")
        self.write(prd("planning"))
        self.assertEqual(self.only().status, "ready")

    def test_a_word_the_file_vocabulary_never_had_passes_through(self) -> None:
        """`ready_to_send` and `blocked` are rows, not lines. Folding either
        into one of the four words is how `blocked` becomes workable."""
        for word in ("ready_to_send", "blocked"):
            with self.subTest(word=word):
                self.seed(word)
                self.assertEqual(self.only().status, word)
                self.assertEqual(self.picked(), [])

    def test_the_row_key_is_the_registered_checkout_and_the_prd_path(self) -> None:
        rows = sd_lib.Rows(self.root)
        self.addCleanup(rows.close)
        self.assertEqual(rows.external_id(self.item), self.identity())

    def test_a_linked_worktree_reads_the_main_checkout_s_row(self) -> None:
        """The migration enumerates the `repo` table, and a worktree was never
        added to it -- so its rows are keyed by the checkout it was cut from."""
        self.git("branch", "side")
        kept = self.tmp / "kept"
        self.git("worktree", "add", "-q", str(kept), "side")
        rows = sd_lib.Rows(kept)
        self.addCleanup(rows.close)
        self.assertEqual(
            rows.external_id(kept / "docs" / "work" / ITEM), self.identity()
        )

    def registered_with_a_clone(self, status: str = "planning") -> pathlib.Path:
        """The original, registered with its `origin`, and a clone of that origin.
        """
        bare = self.remote()
        connection = self.seed(status)
        sd_db.upsert_repo(connection, str(self.root), remote=str(bare))
        return self.clone(bare)

    def test_a_clone_of_the_registered_remote_reads_the_registered_row(self) -> None:
        """sd:981. The row was written under the registered path; a runner
        clone at another path has the same origin, and `sd_db.repos.registered_for`
        already resolves a checkout by that origin. The readers key by the
        path of the checkout they run in, build a key nothing ever wrote,
        and report the item `unknown` -- which is how an unattended run's
        pages go to the send box unreviewed."""
        clone = self.registered_with_a_clone("planning")
        rows = sd_lib.Rows(clone)
        self.addCleanup(rows.close)
        self.assertEqual(
            rows.external_id(clone / "docs" / "work" / ITEM), self.identity()
        )
        self.assertEqual(self.only(clone).status, "planning")
        self.assertEqual(self.picked(clone), [ITEM])

    def test_sd_status_from_a_clone_reports_the_item_readable(self) -> None:
        clone = self.registered_with_a_clone("planning")
        self.assertEqual(self.unreadable(clone), [])

    def test_a_clone_of_a_remote_nobody_registered_keys_by_its_own_path(self) -> None:
        """Requirement 3: the fix widens what a clone of the registered
        remote can read; a checkout of some other remote still speaks for
        nobody but itself, and says so with its own path in the key."""
        self.registered_with_a_clone("planning")
        other = self.remote("other.git")
        clone = self.clone(other, "foreign")
        rows = sd_lib.Rows(clone)
        self.addCleanup(rows.close)
        own = f"{clone}::docs/work/{ITEM}/prd.md"
        self.assertEqual(rows.external_id(clone / "docs" / "work" / ITEM), own)
        item = self.only(clone)
        self.assertEqual(item.status, "unknown")
        self.assertTrue(
            any(f"holds no docs/work row for {own}" in problem
                for problem in item.inconsistencies),
            item.inconsistencies,
        )
        self.assertEqual(self.picked(clone), [])
        found = self.unreadable(clone)
        self.assertEqual(len(found), 1, found)
        self.assertIn(own, found[0]["detail"])

    def test_a_checkout_with_no_origin_keys_by_its_own_path(self) -> None:
        clone = self.registered_with_a_clone("planning")
        subprocess.run(
            ["git", "-C", str(clone), "remote", "remove", "origin"],
            check=True, capture_output=True,
        )
        rows = sd_lib.Rows(clone)
        self.addCleanup(rows.close)
        own = f"{clone}::docs/work/{ITEM}/prd.md"
        self.assertEqual(rows.external_id(clone / "docs" / "work" / ITEM), own)
        item = self.only(clone)
        self.assertEqual(item.status, "unknown")
        self.assertTrue(
            any(f"holds no docs/work row for {own}" in problem
                for problem in item.inconsistencies),
            item.inconsistencies,
        )

    def test_a_database_with_no_row_for_the_item_is_unknown_and_not_open(self) -> None:
        """The fail-open this whole class is written against.

        A row that is not there is an item the database has lost, and reading
        it as "open" is how a delivered item is picked again. The registered
        repository exists; only the item's row does not.
        """
        self.database()
        item = self.only()
        self.assertEqual(item.status, "unknown")
        self.assertTrue(
            any(self.identity() in problem for problem in item.inconsistencies)
        )
        self.assertEqual(self.picked(), [])

    def test_a_row_whose_status_is_not_a_word_is_unknown_and_not_open(self) -> None:
        """The schema's own CHECK stops the six-word list being widened by a
        write, so the guard is exercised where a widened schema would reach
        it: a row that comes back saying something this version has no word
        for. It must not be read as an item nobody has started."""
        self.seed("planning")
        rows = sd_lib.Rows(self.root)
        self.addCleanup(rows.close)
        rows._artifact_read = lambda *_: {"status": "shipped"}
        word, problem = rows.status(self.item)
        self.assertEqual(word, "")
        self.assertIn("'shipped'", problem)

    def test_a_row_the_library_refuses_is_unknown_and_not_open(self) -> None:
        self.seed("planning")
        rows = sd_lib.Rows(self.root)
        self.addCleanup(rows.close)

        def refuse(*_: Any) -> Any:
            raise RuntimeError("the database is locked")

        rows._artifact_read = refuse
        word, problem = rows.status(self.item)
        self.assertEqual(word, "")
        self.assertIn("the database is locked", problem)

    def test_a_checkout_that_declares_the_row_says_when_the_library_is_absent(
        self,
    ) -> None:
        """Two interpreters, one machine, two different answers, and no sign.

        `sd_db` is installed into this pack's virtualenv, so the same
        repository read with the system `python3` cannot import it, falls
        through to git, and prints a different word for the same item -- which
        is how this was found: an item whose row has said `planning` since it
        was opened was reported `in_progress`, confidently, off git.

        The fallback itself is correct and stays: git is the designed reader
        for a checkout with no database, and `GitAloneWhenThereIsNoDatabase`
        below asserts it. What is wrong is that it happens in silence. The
        row is the authority; an answer that came from somewhere else has to
        say so.

        """
        self.seed("planning")
        with mock.patch.dict(sys.modules, {"sd_db": None}):
            item = self.only()
        self.assertTrue(
            any("sd_db is not installed here" in problem
                for problem in item.inconsistencies),
            f"nothing named the absent library: {item.inconsistencies}",
        )

    def test_an_interpreter_without_the_library_reads_the_provisioned_copy(
        self,
    ) -> None:
        """The two answers become one.

        `make setup` provisions `sd_db` into the pack's virtualenv and every
        entrypoint runs under `#!/usr/bin/env python3`, so on the machine this
        was found on -- `python3` 3.14, virtualenv 3.13 -- `./bin/sd-status`
        answered off git while `.venv/bin/python bin/sd-status` answered off
        the row, and the two printed different words for the same item. The
        library is pure Python, so the interpreter that found no `sd_db` reads
        the pinned copy rather than a different source of truth.

        Built here rather than borrowed from this checkout: a test that asks
        the real `.venv` for a copy has to skip where there is none, and a
        skipped test in CI asserts nothing at all. The tree is a whole pack --
        `bin/sd_lib.py` and a provisioned `sd_db` beside it -- so the helper
        resolves it from its own `__file__` exactly as it does in a real one.

        Run under `-S`: no `site`, so no installed `sd_db` on any path can
        answer the first import, and the retry is the only thing that can.
        That is also why patching `sys.modules` cannot express this -- a name
        bound to `None` defeats the retry as well as the first try, which is
        what the two tests above rely on and why they still pass.

        The stub is empty on purpose. `installed` is set the moment the import
        succeeds, before `connect` is reached, so a package with no `connect`
        proves the import and nothing further -- which is the whole claim.
        """
        pack = self.tmp / "pack"
        (pack / "bin").mkdir(parents=True)
        (pack / "bin" / "sd_lib.py").write_bytes(
            (REPO_ROOT / "bin" / "sd_lib.py").read_bytes())
        stub = pack / "lib" / "sd_db"
        stub.mkdir(parents=True)
        (stub / "__init__.py").write_text("", encoding="utf-8")
        script = (
            "import sd_lib, sys;"
            " rows = sd_lib.Rows(sys.argv[1]);"
            " print(int(rows.installed))"
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", script, str(self.root)],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(pack / "bin")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "1",
            f"an interpreter without the library did not find the provisioned"
            f" copy: {result.stdout!r} {result.stderr!r}",
        )

    def test_a_provisioned_copy_that_will_not_import_says_so(self) -> None:
        """Two faults, two sentences. Reporting the first error hides the second.

        A `lib/` holding an `sd_db` that raises on import is not a
        machine without the library, and "No module named 'sd_db'" -- the
        first attempt's error -- describes the wrong problem entirely. Where
        nothing was offered the first error is still the only one there is,
        which the test above this one covers.
        """
        pack = self.tmp / "pack"
        (pack / "bin").mkdir(parents=True)
        (pack / "bin" / "sd_lib.py").write_bytes(
            (REPO_ROOT / "bin" / "sd_lib.py").read_bytes())
        stub = pack / "lib" / "sd_db"
        stub.mkdir(parents=True)
        (stub / "__init__.py").write_text(
            "raise ImportError('the provisioned copy is broken')", encoding="utf-8")
        script = (
            "import sd_lib, sys;"
            " print(sd_lib.Rows(sys.argv[1]).problem)"
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", script, str(self.root)],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(pack / "bin")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the provisioned copy is broken", result.stdout)
        self.assertNotIn("No module named", result.stdout)
        # The prefix is half the message and the half a reader acts on:
        # "not installed" sends them to `make setup` for a package that is
        # already there. Pinned separately because the first fix corrected
        # the detail and left the prefix saying the opposite of it.
        self.assertNotIn("is not installed here", result.stdout)
        self.assertIn("will not import", result.stdout)

    def test_an_incompatible_copy_earlier_on_the_path_does_not_keep_the_answer(
        self,
    ) -> None:
        """The retry has to go in front, or the copy that broke it wins again.

        The second attempt only happens because the first one failed, and one
        of the two ways it fails is an `sd_db` already on `sys.path` that
        raises -- a stale checkout on `PYTHONPATH`, a half-removed install, a
        package of that name belonging to something else. Offered at the
        *end* of the path the pack's copy is behind that one, the finder walks
        the path in order and reaches the incompatible package a second time,
        and the retry fails for the same reason the first try did.

        The message is what makes it a defect rather than a missed
        opportunity: it names the provisioned path and attributes to it an
        error raised somewhere else entirely, so the one reader who could fix
        this is sent to inspect a copy that was never imported. Both halves
        are asserted, because prepending is invisible in the first one alone.
        """
        pack = self.tmp / "pack"
        (pack / "bin").mkdir(parents=True)
        (pack / "bin" / "sd_lib.py").write_bytes(
            (REPO_ROOT / "bin" / "sd_lib.py").read_bytes())
        provisioned = pack / "lib" / "sd_db"
        provisioned.mkdir(parents=True)
        (provisioned / "__init__.py").write_text("", encoding="utf-8")
        earlier = self.tmp / "earlier"
        (earlier / "sd_db").mkdir(parents=True)
        (earlier / "sd_db" / "__init__.py").write_text(
            "raise ImportError('the earlier copy is incompatible')", encoding="utf-8")
        script = (
            "import sd_lib, sys;"
            " rows = sd_lib.Rows(sys.argv[1]);"
            " print(int(rows.installed), rows.problem)"
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", script, str(self.root)],
            capture_output=True, text=True,
            env={**os.environ,
                 "PYTHONPATH": f"{pack / 'bin'}{os.pathsep}{earlier}"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split()[0], "1",
            f"the provisioned copy did not get to answer: {result.stdout!r}",
        )
        self.assertNotIn("the earlier copy is incompatible", result.stdout)

    def test_the_absent_library_does_not_stop_git_answering(self) -> None:
        """The report gains a problem, not a refusal.

        A fresh clone before `make setup` is the ordinary case, and turning it
        into `unknown` for every item would make the fix worse than the defect
        it repairs.
        """
        self.seed("planning")
        with mock.patch.dict(sys.modules, {"sd_db": None}):
            self.assertNotEqual(self.only().status, "unknown")

    def test_the_database_is_opened_once_for_a_whole_enumeration(self) -> None:
        self.seed("ready")
        self.seed("ready", name=OTHER)
        (self.work / OTHER).mkdir()
        (self.work / OTHER / "prd.md").write_text(prd(None), encoding="utf-8")
        opened = []
        real = sd_lib.Rows.__init__

        def counted(instance: Any, root: Any) -> None:
            opened.append(root)
            real(instance, root)

        with mock.patch.object(sd_lib.Rows, "__init__", counted):
            self.assertEqual(len(sd_lib.work_items(self.root)), 2)
        self.assertEqual(len(opened), 1, "one enumeration, one connection")


class GitAloneWhenThereIsNoDatabase(Fixture):
    """A's round forty-one: git alone answers, once there is no database.

    This is also the criterion's recording double -- in a checkout with no
    database, every reader that picks an item asks `sd_lib.delivered` and
    nothing else.
    """

    def setUp(self) -> None:
        super().setUp()
        self.write(prd(None))
        self.commit("chore: the retire")
        self.calls: list[tuple[Any, ...]] = []

    def recorder(self, answer: sd_lib.Answer):
        def record(root: Any, item: str) -> sd_lib.Answer:
            self.calls.append((pathlib.Path(root), item))
            return answer

        return mock.patch.object(sd_lib, "delivered", record)

    def test_a_delivered_item_is_done_and_the_line_is_not_consulted(self) -> None:
        """"and nothing else": the line says `planning` and the answer is
        `done`, so no second source contributed to it."""
        self.write(prd("planning"))
        with self.recorder(sd_lib.Answer(sd_lib.YES)):
            self.assertEqual(self.only().status, "done")
            self.assertEqual(self.picked(), [])
        self.assertEqual(self.calls[0], (self.root, ITEM))

    def test_an_undelivered_item_is_still_picked(self) -> None:
        with self.recorder(sd_lib.Answer(sd_lib.NO)):
            self.assertEqual(self.picked(), [ITEM])

    def test_what_the_retire_left_says_which_kind_of_open(self) -> None:
        self.write(prd(None, branch="wip/a-thing"))
        with self.recorder(sd_lib.Answer(sd_lib.NO)):
            self.assertEqual(self.only().status, "in_progress")
        self.write(prd(None))
        with self.recorder(sd_lib.Answer(sd_lib.NO)):
            self.assertEqual(self.only().status, "planning")

    def test_unknown_is_not_no(self) -> None:
        """A shallow clone and an unreachable remote cannot see the trailer.
        Reading either as "not delivered" reopens finished work, so neither is
        allowed to produce a status any reader picks."""
        with self.recorder(sd_lib.Answer(sd_lib.UNKNOWN, "git fetch --unshallow")):
            item = self.only()
            self.assertEqual(self.picked(), [])
        self.assertEqual(item.status, "unknown")
        self.assertTrue(
            any("git fetch --unshallow" in p for p in item.inconsistencies)
        )

    def test_the_question_is_asked_once_per_item_and_of_git_only(self) -> None:
        (self.work / OTHER).mkdir()
        (self.work / OTHER / "prd.md").write_text(prd(None), encoding="utf-8")
        with self.recorder(sd_lib.Answer(sd_lib.NO)):
            sd_lib.work_items(self.root)
        self.assertEqual(self.calls, [(self.root, OTHER), (self.root, ITEM)])

    def test_the_real_answer_comes_off_a_real_trailer(self) -> None:
        """No double at all: the trailer is written and `sd_lib.delivered`
        reads it, so the recorder above is standing in for something real."""
        self.assertEqual(self.picked(), [ITEM])
        self.git("commit", "-q", "--allow-empty", "-m",
                 merge_message(f"Item: {ITEM}", f"Delivers: {ITEM}"))
        self.assertEqual(self.only().status, "done")
        self.assertEqual(self.picked(), [])


class WhatStatusNames(Fixture):
    """Requirement 4. Both words are the criterion's."""

    def test_a_surviving_line_that_disagrees_is_named_stale(self) -> None:
        """A worktree kept across the retire, on a branch that still carries
        its line, after a status change made in the database."""
        self.git("branch", "-q", "before-the-retire")
        self.write(prd(None))
        self.commit("chore: the retire")
        kept = self.tmp / "kept"
        self.git("worktree", "add", "-q", str(kept), "before-the-retire")
        self.seed("ready")

        found = [i for i in sd_lib.work_items(kept) if i.slug == "a-thing"][0]
        self.assertEqual(found.status, "ready")
        stale = [p for p in found.inconsistencies if "stale" in p]
        self.assertEqual(len(stale), 1, found.inconsistencies)
        self.assertIn("'planning'", stale[0])
        self.assertIn("'ready'", stale[0])

    def test_a_line_that_agrees_is_not_called_stale(self) -> None:
        """The check is what the line says, not that there is one. A line the
        retire has not reached yet is rule 1's finding, not a disagreement."""
        self.seed("planning")
        self.assertEqual(
            [p for p in self.only().inconsistencies if "stale" in p], []
        )

    def test_a_done_row_no_commit_closed_is_named_unmarked(self) -> None:
        """The hand merge through the remote carried `Item:` and no closing
        trailer; `deliver` on the item screen then wrote the row `done`."""
        self.write(prd(None))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", merge_message(f"Item: {ITEM}"))
        self.seed("done")
        item = self.only()
        self.assertEqual(item.status, "done")
        unmarked = [p for p in item.inconsistencies if "unmarked" in p]
        self.assertEqual(len(unmarked), 1, item.inconsistencies)
        self.assertIn(ITEM, unmarked[0])

    def test_a_done_row_a_commit_did_close_is_not_named_unmarked(self) -> None:
        self.write(prd(None))
        self.git("add", "-A")
        self.git("commit", "-q", "-m",
                 merge_message(f"Item: {ITEM}", f"Closes: {ITEM}"))
        self.seed("done")
        self.assertEqual(
            [p for p in self.only().inconsistencies if "unmarked" in p], []
        )

    def test_sd_status_prints_both_words(self) -> None:
        self.git("commit", "-q", "--allow-empty", "-m", merge_message(f"Item: {ITEM}"))
        self.seed("done")
        printed = self.rendered()
        self.assertIn("stale", printed)
        self.assertIn("unmarked", printed)
        self.assertIn(ITEM, printed)

    def test_a_finding_is_never_elided_past_the_listed_items(self) -> None:
        """`done` items sort into the tail that stops at twenty, and a finding
        printed nowhere a human looks is what this section exists against."""
        fillers = []
        for number in range(status_tool.LISTED_ITEMS + 5):
            name = f"2026-08-{number % 28 + 1:02d}-filler-{number}"
            (self.work / name).mkdir()
            (self.work / name / "prd.md").write_text(prd(None), encoding="utf-8")
            self.seed("done", name=name)
            fillers.append(name)
        # Every filler is delivered and clean, so only the one item with a
        # finding stands between the elision and the reader.
        self.git("add", "-A")
        self.git("commit", "-q", "-m", merge_message(
            f"Item: {ITEM}", *(f"Closes: {name}" for name in fillers)))
        self.seed("done")
        printed = self.rendered()
        self.assertIn("more not being worked", printed)
        self.assertIn("unmarked", printed)
        self.assertIn(ITEM, printed)


class RuleTwoReadsTheRow(Fixture):
    """Requirement 5. `check_ready` returns early on a word it cannot find."""

    def lint(self) -> list[str]:
        """Rule 2's findings alone.

        Rule 1 fails an active `status:` line, and
        `tests/test_sd_docs_lint.py` pins each cell of that table. The filter
        stays so this class keeps asserting the thing it is about whichever
        way a fixture is written.
        """
        report = lint.run(self.root, "docs/work", "docs/spec", "docs/decisions", None)
        return [f for f in report.failures if "is not one of" not in f]

    def test_the_three_checks_survive_the_retire(self) -> None:
        """Without the row, `check_ready` finds no workable item once the
        lines are gone and its three checks stop running everywhere at once.

        The row names no branch here: a branch on the row satisfies the
        branch check as the `branch:` line does (sd:3489)."""
        self.write(prd(None, criteria=False))
        self.seed("in_progress", branch=None)
        failures = self.lint()
        self.assertTrue(
            any("states acceptance criteria" in f for f in failures), failures
        )
        self.assertTrue(
            any("records the branch it lives on" in f for f in failures), failures
        )

    def test_a_branch_on_the_row_records_the_branch(self) -> None:
        """sd:3489. A design.md carries no `branch:` line; the row's column answers."""
        self.write(prd(None))
        self.seed("in_progress", branch="feat/the-thing")
        self.assertEqual([f for f in self.lint() if "records the branch" in f], [])

    def test_a_blocked_row_is_not_workable(self) -> None:
        self.write(prd(None, criteria=False))
        self.seed("blocked")
        self.assertEqual(self.lint(), [])


class RuleTwoComparesTheBranchLine(Fixture):
    """sd:1476. A `branch:` line that disagrees with the row is named.

    The row's `branch` column is what the build records,
    and the frontmatter line is a second copy nothing reconciled. Many items
    still carry `branch: main`, so this is a note and never a failure: a lint
    that turned every such item red would be switched off, not read.
    """

    def notes(self) -> tuple[list[str], list[str]]:
        report = lint.run(self.root, "docs/work", "docs/spec", "docs/decisions", None)
        # A skip ("not compared") is a warning since sd:3491; the comparisons are notes.
        found = [n for n in report.notes + report.warnings if n.startswith("rule 2 branch line")]
        return found, report.failures

    def test_a_line_naming_another_branch_is_named(self) -> None:
        self.write(prd(None, branch="main"))
        self.seed("in_progress", branch="feat/the-thing")
        found, failures = self.notes()
        named = [n for n in found if ITEM in n]
        self.assertEqual(len(named), 1, found)
        self.assertIn("'main'", named[0])
        self.assertIn("'feat/the-thing'", named[0])
        self.assertIn("rule 2 branch line: 1 of 1 active item(s) with a branch: line "
                      "disagree with the row; advisory, no failure", found)
        self.assertEqual([f for f in failures if "branch" in f], [])

    def test_a_line_where_the_row_names_no_branch_is_named(self) -> None:
        self.write(prd(None, branch="main"))
        self.seed("planning", branch=None)
        found, _ = self.notes()
        named = [n for n in found if ITEM in n]
        self.assertEqual(len(named), 1, found)
        self.assertIn("the row names no branch", named[0])

    def test_a_line_that_agrees_is_not_named(self) -> None:
        self.write(prd(None, branch="feat/the-thing"))
        self.seed("in_progress", branch="feat/the-thing")
        found, _ = self.notes()
        self.assertEqual([n for n in found if ITEM in n], [])
        self.assertIn("rule 2 branch line: 0 of 1 active item(s) with a branch: line "
                      "disagree with the row; advisory, no failure", found)

    def test_an_item_with_no_line_is_not_compared(self) -> None:
        self.write(prd(None))
        self.seed("in_progress", branch="feat/the-thing")
        found, _ = self.notes()
        self.assertIn("rule 2 branch line: 0 of 0 active item(s) with a branch: line "
                      "disagree with the row; advisory, no failure", found)

    def test_an_archived_line_is_history_and_not_compared(self) -> None:
        self.write(prd(None))
        self.seed("in_progress", branch="feat/the-thing")
        archived = self.work / "archive" / "2026-08" / OTHER
        archived.mkdir(parents=True)
        (archived / "prd.md").write_text(prd("done", branch="main"), encoding="utf-8")
        self.seed("done", name=OTHER, branch="feat/other")
        found, _ = self.notes()
        self.assertEqual([n for n in found if OTHER in n], [])

    def test_a_checkout_under_a_directory_named_archive_is_still_compared(self) -> None:
        # Review of #1177: only `archive/` below the work root is history. A
        # checkout at `/archive/repo` skipped every item, `0 of 0`, silently.
        moved = self.tmp / "archive" / "repo"
        moved.parent.mkdir()
        self.root.rename(moved)
        self.root, self.work = moved, moved / "docs" / "work"
        self.item = self.work / ITEM
        self.write(prd(None, branch="main"))
        self.seed("in_progress", branch="feat/the-thing")
        found, _ = self.notes()
        self.assertEqual(len([n for n in found if ITEM in n]), 1, found)
        self.assertIn("rule 2 branch line: 1 of 1 active item(s) with a branch: line "
                      "disagree with the row; advisory, no failure", found)

    def test_without_the_row_nothing_is_compared_and_it_says_so(self) -> None:
        self.write(prd("in_progress", branch="main"))
        found, _ = self.notes()
        self.assertEqual(len(found), 1, found)
        self.assertIn("not compared", found[0])


class ATaskKeyedFolder(Fixture):
    """sd:994. A folder written for a task or followup row names it.

    Seven pack folders were written for rows `sd task add` created, whose
    `kind` is `task` or `followup` and whose `source`, `external_id` and
    `path` are NULL. `item_for_artifact` selects `kind = 'work'` alone, so
    each read as "no row" and `sd-status` reported all seven
    `status-unreadable`. The frontmatter line `item: sd:<id>` names the row
    instead, and only when the path finds none: a work row is found by its
    path, and the key does not reach it.
    """

    def named(self, value: str, status: str | None = None) -> None:
        """`prd.md` with `item: <value>` after `created:`, the way the pages carry it."""
        text = prd(status).replace(
            "created: 2026-09-05\n", f"created: 2026-09-05\nitem: {value}\n"
        )
        self.write(text)

    def row(self, kind: str = "followup", status: str = "planning") -> int:
        connection = getattr(self, "_connection", None) or self.database()
        self._connection = connection
        return sd_db.writes.create_item(
            connection, kind=kind, title="A thing, as a row", status=status,
            repo=str(self.root),
        )

    def test_a_folder_naming_a_followup_row_reads_its_status(self) -> None:
        number = self.row("followup", "planning")
        self.named(f"sd:{number}")
        item = self.only()
        self.assertEqual(item.status, "planning")
        self.assertEqual(item.inconsistencies, ())
        self.assertEqual(self.picked(), [ITEM])
        self.assertEqual(self.unreadable(self.root), [])

    def test_a_folder_naming_a_task_row_reads_its_status(self) -> None:
        number = self.row("task", "in_progress")
        self.named(f"sd:{number}")
        self.assertEqual(self.only().status, "in_progress")
        self.assertEqual(self.unreadable(self.root), [])

    def test_the_named_row_supplies_the_activity_stamp(self) -> None:
        number = self.row("followup", "planning")
        self.named(f"sd:{number}")
        rows = sd_lib.Rows(self.root)
        self.addCleanup(rows.close)
        self.assertTrue(rows.activity(self.item), "the named row's stamps were not read")

    def test_a_named_row_that_does_not_exist_is_unreadable_and_names_the_key(self) -> None:
        self.database()
        self.named("sd:424242")
        item = self.only()
        self.assertEqual(item.status, "unknown")
        self.assertTrue(
            any("item: sd:424242" in problem for problem in item.inconsistencies),
            item.inconsistencies,
        )
        self.assertEqual(self.picked(), [])
        found = self.unreadable(self.root)
        self.assertEqual(len(found), 1, found)
        self.assertIn("item: sd:424242", found[0]["detail"])

    def test_a_malformed_key_is_unreadable_and_names_its_value(self) -> None:
        number = self.row("followup", "planning")
        for value in (str(number), "sd:x", "sd:"):
            with self.subTest(value=value):
                self.named(value)
                item = self.only()
                self.assertEqual(item.status, "unknown")
                self.assertTrue(
                    any(f"item: {value}" in problem for problem in item.inconsistencies),
                    item.inconsistencies,
                )

    def test_a_named_work_row_is_refused_and_the_key_is_named(self) -> None:
        """A work row is found by its path. Reaching one through the key would
        let a folder borrow another item's status, so the key refuses it."""
        self.seed("in_progress", OTHER)
        other = sd_db.writes.item_by_external(
            self._connection, sd_lib.ITEM_ROW_SOURCE, self.identity(OTHER))
        self.named(f"sd:{other['id']}")
        item = self.only()
        self.assertEqual(item.status, "unknown")
        self.assertTrue(
            any(f"item: sd:{other['id']}" in problem and "work" in problem
                for problem in item.inconsistencies),
            item.inconsistencies,
        )
        self.assertEqual(self.picked(), [])

    def test_the_row_found_by_path_wins_over_the_key(self) -> None:
        self.seed("in_progress")
        number = self.row("followup", "planning")
        self.named(f"sd:{number}")
        self.assertEqual(self.only().status, "in_progress")

    def test_a_key_too_large_for_a_row_id_is_unreadable_and_never_binds(self) -> None:
        """A value the database cannot bind is refused before it is bound.

        `sqlite3` raises `OverflowError` on an integer past 2**63-1, and the
        readers that take this key are not all inside a `try`: the dashboard's
        is not, so an unbounded id read as a traceback rather than a finding.
        """
        self.database()
        self.named("sd:99999999999999999999999999")
        item = self.only()
        self.assertEqual(item.status, "unknown")
        self.assertTrue(
            any("99999999999999999999999999" in problem and "row id" in problem
                for problem in item.inconsistencies),
            item.inconsistencies,
        )
        self.assertEqual(self.picked(), [])

    def test_a_key_of_thousands_of_digits_is_refused_before_int(self) -> None:
        """`int()` raises `ValueError` past `sys.get_int_max_str_digits()`.

        The ceiling in `named_row` is reached through `int()`, so a value long
        enough never got there. The refusal is on the digits, before the
        conversion, and `named_item` is where the key becomes a number.
        """
        self.database()
        self.named("sd:" + "9" * 5000)
        item = self.only()
        self.assertEqual(item.status, "unknown")
        self.assertTrue(
            any("row id" in problem for problem in item.inconsistencies),
            item.inconsistencies,
        )
        self.assertEqual(self.picked(), [])

    def test_a_prd_that_is_not_utf_8_is_unreadable_and_names_the_file(self) -> None:
        """`read_text` raises `UnicodeDecodeError`, which is not an `OSError`."""
        self.database()
        (self.item / "prd.md").write_bytes(b"---\ntitle: A thing\nitem: sd:1\n---\n\xca\xfe\n")
        rows = sd_lib.Rows(self.root)
        self.addCleanup(rows.close)
        said, trouble = rows.status(self.item)
        self.assertEqual(said, "")
        self.assertIn("prd.md", trouble)
        self.assertIn("UTF-8", trouble)

    def test_an_installation_without_the_reader_keeps_the_old_answer(self) -> None:
        """`item_by_id` is optional, like the other reads: a library without
        it says what it said before the key existed."""
        number = self.row("followup", "planning")
        self.named(f"sd:{number}")
        rows = sd_lib.Rows(self.root)
        self.addCleanup(rows.close)
        rows._id_read = None
        said, trouble = rows.status(self.item)
        self.assertEqual(said, "")
        self.assertIn(f"holds no docs/work row for {self.identity()}", trouble)


class ADesignOnlyFolder(Fixture):
    """sd:3255. sd:3000 lets a folder hold `design.md` alone.

    The readers took the frontmatter and the `item:` key from `prd.md` only,
    so such a folder read `status-unreadable` with no title, though its key
    named a live row. `design.md` answers when `prd.md` is absent; an older
    folder holding both keeps reading `prd.md`. The row key stays
    `docs/work/<item>/prd.md`: `sd_db` registers no other.
    """

    def setUp(self) -> None:
        super().setUp()
        (self.item / "prd.md").unlink()

    def row(self, status: str) -> int:
        self._connection = self.database()
        return sd_db.writes.create_item(
            self._connection, kind="task", title="A thing, as a row", status=status,
            repo=str(self.root),
        )

    def design(self, value: str) -> None:
        text = ("---\ntitle: A thing, designed\ncreated: 2026-09-05\n"
                f"item: {value}\n---\n\n# Design\n")
        (self.item / "design.md").write_text(text, encoding="utf-8")

    def test_a_design_only_folder_reads_its_named_row_and_title(self) -> None:
        number = self.row("planning")
        self.design(f"sd:{number}")
        item = self.only()
        self.assertEqual(item.status, "planning")
        self.assertEqual(item.title, "A thing, designed")
        self.assertEqual(item.created, "2026-09-05")
        self.assertEqual(item.inconsistencies, ())
        self.assertEqual(sd_lib.named_item(self.item), (number, ""))

    def test_prd_md_stays_first_when_both_are_there(self) -> None:
        number = self.row("in_progress")
        self.design("sd:424242")
        self.write(prd(None).replace("created: 2026-09-05\n", f"created: 2026-09-05\nitem: sd:{number}\n"))
        item = self.only()
        self.assertEqual(item.status, "in_progress")
        self.assertEqual(item.title, "A thing")
        self.assertEqual(sd_lib.named_item(self.item), (number, ""))

    def test_a_folder_with_neither_names_prd_md_as_missing(self) -> None:
        self.database()
        item = self.only()
        self.assertEqual(item.status, "unknown")
        self.assertTrue(any("prd.md is missing" in problem for problem in item.inconsistencies),
                        item.inconsistencies)

    def test_the_row_key_stays_prd_md(self) -> None:
        self.design("sd:1")
        self.assertEqual(sd_lib.external_id(self.root, self.item), self.identity())


class TheUnmarkedCheckAsksTheIdEveryWriterWrites(Fixture):
    """sd:1113. The `unmarked` clause asked git for a string nobody writes.

    `bin/sd-ship` puts `Delivers: sd:<id>` on the squash and `sd work deliver`
    refuses a commit carrying anything else, so the id is the pack's spelling.
    The clause asked by the folder's name instead, so on a checkout with a row
    it could never clear and the warning was permanent. The folder name is
    still asked beside it: `main` carries two trailers in that form and a
    database-free checkout has no id to ask by.
    """

    def row_id(self, name: str = ITEM) -> int:
        row = sd_db.writes.item_by_external(
            self._connection, sd_lib.ITEM_ROW_SOURCE, self.identity(name)
        )
        self.assertIsNotNone(row, "the fixture seeded a row")
        return int(row["id"])

    def delivering(self, *trailers: str) -> None:
        self.write(prd(None))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", merge_message(*trailers))

    def unmarked(self) -> list[str]:
        return [p for p in self.only().inconsistencies if "unmarked" in p]

    def test_the_id_form_clears_the_check(self) -> None:
        """The trailer `sd-ship` actually writes. This is the whole defect:
        before the fix the item was reported unmarked with the trailer there."""
        self.seed("done")
        number = self.row_id()
        self.delivering(f"Item: sd:{number}", f"Delivers: sd:{number}")
        self.assertEqual(self.only().status, "done")
        self.assertEqual(self.unmarked(), [])

    def test_the_folder_name_still_clears_the_check(self) -> None:
        """Two commits on `main` carry that form, and it is the only spelling
        a database-free checkout can resolve."""
        self.seed("done")
        self.delivering(f"Closes: {ITEM}")
        self.assertEqual(self.unmarked(), [])

    def test_the_message_names_both_values_it_looked_for(self) -> None:
        """A reader told to search for a string that is not there searches for
        an hour. The finding spells each value it asked git about."""
        self.seed("done")
        number = self.row_id()
        self.delivering(f"Item: sd:{number}")
        found = self.unmarked()
        self.assertEqual(len(found), 1, self.only().inconsistencies)
        self.assertIn(f"Delivers: sd:{number}", found[0])
        self.assertIn(f"Delivers: {ITEM}", found[0])
        self.assertIn("Closes:", found[0])

    def test_a_near_miss_id_closes_nothing(self) -> None:
        """`sd:78` must not close `sd:788`: the several spellings are matched
        whole, never by membership in a string."""
        self.seed("done")
        number = self.row_id()
        self.delivering(f"Delivers: sd:{number}9")
        self.assertEqual(len(self.unmarked()), 1, self.only().inconsistencies)


class AFollowupRowIsClearedByItsTrailerAlone(Fixture):
    """sd:1113. A row whose `kind` is not `work` has no other way to clear.

    `completion_record` is read off the row `item_for_artifact` finds by path,
    and `sd work relink` refuses to give a `followup` row a path -- "this
    operation is for work items". So the trailer clause is the only clause
    that can ever mark such an item, and asking it by the folder's name left
    the item permanently unmarked. Row 788 is the live instance.
    """

    named = ATaskKeyedFolder.named
    row = ATaskKeyedFolder.row

    def delivering(self, number: int, *trailers: str) -> None:
        self.named(f"sd:{number}", status=None)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", merge_message(*trailers))

    def unmarked(self) -> list[str]:
        return [p for p in self.only().inconsistencies if "unmarked" in p]

    def test_a_done_followup_row_is_marked_by_the_id_trailer(self) -> None:
        number = self.row("followup", "done")
        self.delivering(number, f"Delivers: sd:{number}")
        item = self.only()
        self.assertEqual(item.status, "done")
        self.assertEqual(self.unmarked(), [], item.inconsistencies)

    def test_without_the_trailer_the_followup_row_is_still_unmarked(self) -> None:
        """The clause has to keep finding the real case: no trailer, no mark."""
        number = self.row("followup", "done")
        self.delivering(number, f"Item: sd:{number}")
        found = self.unmarked()
        self.assertEqual(len(found), 1, self.only().inconsistencies)
        self.assertIn(f"Delivers: sd:{number}", found[0])


class AFollowupRowIsClearedByItsDeliveryReceipt(Fixture):
    """sd:3346. A followup closed by `sd work deliver --associated` is marked.

    A task or followup has no work receipt (sd:1913): its `Item:` merge is
    verified, and the move to `done` records `delivered at <sha> on <ref>`
    with the reason. The `unmarked` clause read trailers alone, so the row
    warned forever. The receipt now counts while its commit is on the default
    branch and its trailer block names the item.
    """

    named = ATaskKeyedFolder.named
    row = ATaskKeyedFolder.row
    unmarked = AFollowupRowIsClearedByItsTrailerAlone.unmarked

    def merged(self, number: int, *trailers: str) -> str:
        """A merge carrying `trailers`, on whatever branch is checked out; its sha."""
        self.named(f"sd:{number}", status=None)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", merge_message(*trailers))
        return self.git("rev-parse", "HEAD")

    def close(self, number: int, reason: str) -> None:
        """The status move `sd work deliver --associated` makes for a followup."""
        sd_db.workflow.change_status(self._connection, number, "done", who="the test", reason=reason)

    def receipt(self, number: int, commit: str) -> None:
        self.close(number, f"delivered at {commit} on origin/main (after the fact: the merge was the item)")

    def test_a_receipt_on_the_default_branch_clears_the_check(self) -> None:
        number = self.row("followup", "planning")
        commit = self.merged(number, f"Item: sd:{number}")
        self.remote()
        self.receipt(number, commit)
        item = self.only()
        self.assertEqual(item.status, "done")
        self.assertEqual(self.unmarked(), [], item.inconsistencies)

    def test_a_receipt_off_the_default_branch_still_warns_and_names_it(self) -> None:
        number = self.row("followup", "planning")
        self.named(f"sd:{number}", status=None)
        self.commit("chore: the folder names its row")
        self.git("checkout", "-q", "-b", "side")
        commit = self.merged(number, f"Item: sd:{number}")
        self.git("checkout", "-q", "main")
        self.receipt(number, commit)
        found = self.unmarked()
        self.assertEqual(len(found), 1, self.only().inconsistencies)
        self.assertIn(commit, found[0])
        self.assertIn("not reachable from main", found[0])

    def test_a_receipt_naming_a_commit_for_another_item_still_warns(self) -> None:
        """The sentence is free text on `task status --reason`, so the commit is
        held to the item: one whose trailers name `sd:<id>9` delivers nothing here."""
        number = self.row("followup", "planning")
        commit = self.merged(number, f"Item: sd:{number}9")
        self.receipt(number, commit)
        found = self.unmarked()
        self.assertEqual(len(found), 1, self.only().inconsistencies)
        self.assertIn(f"carries no Item: sd:{number}", found[0])

    def test_a_later_close_by_hand_drops_the_receipt(self) -> None:
        """Only the latest move to `done` speaks: a reopened row closed by hand
        was not delivered by the earlier receipt."""
        number = self.row("followup", "planning")
        commit = self.merged(number, f"Item: sd:{number}")
        self.receipt(number, commit)
        sd_db.workflow.change_status(self._connection, number, "planning", who="the test")
        self.close(number, "closed by hand")
        self.assertEqual(len(self.unmarked()), 1, self.only().inconsistencies)


class AnItemOfAnotherRepoIsAskedOfThatRepo(Fixture):
    """sd:5371. A folder here can name a row whose repo is another checkout.

    The design for one repo's item may live in a second repo's `docs/work`.
    Its receipt names a commit on the item's repo, so asking this checkout's
    `origin/main` warned "not reachable" on delivered work. The item's repo
    answers; a repo with no checkout here leaves the item unchecked, not unmarked.
    """

    named = ATaskKeyedFolder.named
    unmarked = AFollowupRowIsClearedByItsTrailerAlone.unmarked
    close = AFollowupRowIsClearedByItsDeliveryReceipt.close
    receipt = AFollowupRowIsClearedByItsDeliveryReceipt.receipt

    def setUp(self) -> None:
        super().setUp()
        self.remote()
        self.other = self.tmp / "other"
        self.other.mkdir()
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "test@example.com"),
                     ("config", "user.name", "Test User"), ("config", "commit.gpgsign", "false")):
            self.git_in(self.other, *args)

    def git_in(self, root: pathlib.Path, *args: str) -> str:
        return self.git("-C", str(root), *args)

    def foreign(self) -> tuple[int, str]:
        """A followup row of the other repo, named here, with its receipt on that repo's main."""
        connection = getattr(self, "_connection", None) or self.database()
        self._connection = connection
        sd_db.upsert_repo(connection, str(self.other))
        number = sd_db.writes.create_item(
            connection, kind="followup", title="A thing, as a row", status="planning",
            repo=str(self.other),
        )
        self.named(f"sd:{number}", status=None)
        self.commit("chore: the folder names its row")
        self.git("push", "-q", "origin", "main")
        self.git_in(self.other, "commit", "-q", "--allow-empty", "-m", merge_message(f"Item: sd:{number}"))
        bare = self.tmp / "other.git"
        self.git("init", "-q", "--bare", "-b", "main", str(bare))
        self.git_in(self.other, "remote", "add", "origin", str(bare))
        self.git_in(self.other, "push", "-q", "origin", "main")
        commit = self.git_in(self.other, "rev-parse", "HEAD")
        self.receipt(number, commit)
        return number, commit

    def test_a_receipt_on_the_item_repo_main_clears_the_check(self) -> None:
        self.foreign()
        item = self.only()
        self.assertEqual(item.status, "done")
        self.assertEqual(self.unmarked(), [], item.inconsistencies)

    def test_a_receipt_off_the_item_repo_main_still_warns(self) -> None:
        number, _ = self.foreign()
        self.git_in(self.other, "checkout", "-q", "-b", "side")
        self.git_in(self.other, "commit", "-q", "--allow-empty", "-m", merge_message(f"Item: sd:{number}"))
        side = self.git_in(self.other, "rev-parse", "HEAD")
        sd_db.workflow.change_status(self._connection, number, "planning", who="the test")
        self.receipt(number, side)
        found = self.unmarked()
        self.assertEqual(len(found), 1, self.only().inconsistencies)
        self.assertIn(f"{side}, which is not reachable from origin/main", found[0])

    def test_a_receipt_on_this_checkout_main_does_not_clear_another_repo_item(self) -> None:
        """Evidence from the wrong subject: the commit names the item but sits on
        this checkout's main, not on the item repo's, so it delivers nothing."""
        number, _ = self.foreign()
        self.git("commit", "-q", "--allow-empty", "-m", merge_message(f"Item: sd:{number}"))
        self.git("push", "-q", "origin", "main")
        here = self.git("rev-parse", "HEAD")
        sd_db.workflow.change_status(self._connection, number, "planning", who="the test")
        self.receipt(number, here)
        found = self.unmarked()
        self.assertEqual(len(found), 1, self.only().inconsistencies)
        self.assertIn(f"{here}, which is not reachable from origin/main", found[0])

    def test_an_item_repo_with_no_checkout_here_is_unchecked_not_unmarked(self) -> None:
        number, _ = self.foreign()
        shutil.rmtree(self.other)
        item = self.only()
        self.assertEqual(item.status, "done")
        self.assertEqual(self.unmarked(), [], item.inconsistencies)
        self.assertEqual(len(item.inconsistencies), 1, item.inconsistencies)
        self.assertIn(f"sd:{number} belongs to {self.other}", item.inconsistencies[0])
        self.assertIn("its delivery is unchecked", item.inconsistencies[0])


class TheRowNamesTheBranch(Fixture):
    """sd:1382. The row's `branch` column is the maintained copy.

    The build writes the column and `sd-status` prints it;
    the frontmatter `branch:` line is written once and never reconciled. `sd-review --scope planning` picked
    its item by the line, so a stale line both refused a branch the row names
    and picked a different item whose line named the branch.
    """

    def other(self, *, branch: str) -> None:
        (self.work / OTHER).mkdir()
        (self.work / OTHER / "prd.md").write_text(prd(None, branch=branch), encoding="utf-8")

    def picked_on(self, branch: str) -> list[str]:
        self.git("checkout", "-q", "-b", branch)
        subject = review_tool.resolve_subject(self.root, "planning")
        return sorted({pathlib.Path(path).parts[2] for path in subject.paths})

    def test_the_row_branch_replaces_a_stale_line(self) -> None:
        self.write(prd(None, branch="main"))
        self.seed("in_progress", branch="feat/the-thing")
        self.assertEqual(self.only().branch, "feat/the-thing")

    def test_a_row_with_no_branch_is_no_branch_whatever_the_line_says(self) -> None:
        self.write(prd(None, branch="main"))
        self.seed("planning", branch=None)
        self.assertEqual(self.only().branch, "")

    def test_planning_review_picks_the_item_the_row_puts_on_this_branch(self) -> None:
        """Wrongly-pass: the other item's line still names a branch its row
        no longer carries, and a new item is started on that branch name."""
        self.write(prd(None, branch="main"))
        self.other(branch="feat/the-thing")
        self.seed("in_progress", branch="feat/the-thing")
        self.seed("planning", name=OTHER, branch=None)
        self.commit("chore: two items")
        self.assertEqual(self.picked_on("feat/the-thing"), [ITEM])

    def test_planning_review_is_not_refused_on_the_branch_the_row_names(self) -> None:
        """Wrongly-fail: both lines say `branch: main`, and the row puts one
        of the items on the branch this checkout is on."""
        self.write(prd(None, branch="main"))
        self.other(branch="main")
        self.seed("in_progress", branch="feat/the-thing")
        self.seed("planning", name=OTHER, branch=None)
        self.commit("chore: two items")
        self.assertEqual(self.picked_on("feat/the-thing"), [ITEM])


class TheMakeCheckLintReadsNoHistory(Fixture):
    """`make check` runs `sd-docs-lint` on the checkout, and asks git nothing about history.

    Both readers below fetch the remote and run `git log --grep` once per
    item: `_from_git` for every open item of a checkout with no database, and
    `_from_row` for every `done` row no completion recorded. Inside the gate
    that made the verdict depend on the remote and on commit messages, so a
    tree-keyed receipt could not stand for it (sd:2606). Delivery is still
    asked where it belongs, by the lint `sd-ship` runs with the pull request
    body. The recipe is read from the Makefile, so the test runs the form
    `make check` runs and not a copy of it.
    """

    HISTORY = ("fetch", "log", "ls-remote")

    def setUp(self) -> None:
        super().setUp()
        self.write(prd(None, branch="wip/a-thing"))
        self.commit("chore: the retire")
        self.remote()

    def recipe(self) -> list[str]:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        found = re.search(r"^docs-lint:\n\t(.+)$", makefile, re.MULTILINE)
        assert found is not None, "the Makefile has a docs-lint recipe"
        argv = shlex.split(found.group(1).replace("$(VENV_PYTHON)", sys.executable))
        return [str(REPO_ROOT / word) if word == "bin/sd-docs-lint" else word for word in argv]

    def traced(self, argv: list[str]) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        """One run in the fixture checkout, and the name of every git command it ran."""
        trace = self.tmp / "trace"
        trace.mkdir(exist_ok=True)
        for old in trace.iterdir():
            old.unlink()
        done = subprocess.run(
            argv, cwd=str(self.root), capture_output=True, text=True,
            env={**os.environ, "GIT_TRACE2_EVENT": str(trace)},
        )
        names = []
        for path in trace.iterdir():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                event = json.loads(line)
                if event.get("event") == "cmd_name":
                    names.append(event["name"])
        return done, names

    def assert_reads_no_history(self) -> subprocess.CompletedProcess[str]:
        done, names = self.traced(self.recipe())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual([name for name in names if name in self.HISTORY], [], names)
        self.assertIn("rule 2 delivery: not asked", done.stderr)  # a skip goes to stderr (sd:3491)
        return done

    def test_the_control_without_the_flag_does_fetch_and_log(self) -> None:
        """Without this, a fixture no reader reaches would pass the two below."""
        _, names = self.traced([sys.executable, str(REPO_ROOT / "bin" / "sd-docs-lint")])
        self.assertIn("fetch", names)
        self.assertIn("log", names)

    def test_an_open_item_with_no_database_is_not_asked_of_git(self) -> None:
        done = self.assert_reads_no_history()
        self.assertIn("every open item reads as unknown", done.stdout)

    def test_a_done_row_nothing_recorded_is_not_asked_of_git(self) -> None:
        self.seed("done")
        self.assert_reads_no_history()
