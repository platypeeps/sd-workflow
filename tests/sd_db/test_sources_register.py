"""The register migration: the open set read from the register, not from here.

Three entries on 2026-09-05. The list of ids is the register's own header,
because a list compiled into a migration is wrong the first time somebody
adds one.

And read from the committed tree on `origin/main`, not from whatever the
checkout has on disk. On 2026-09-10 the simulator sat on a feature branch and
O30 landed as a row from a header sentence `main` had never seen; the fixture
here commits and pushes the register, because a register that is only written
to disk is the exact thing the reader no longer reads.
"""

import json
import tempfile
import unittest
from pathlib import Path

from sd_db import connect
from sd_db.migrate import initialise
from sd_db.repos import add
from sd_db.sources import MigrationRefused, run, verify
from sd_db.sources.register import SOURCE, Reader, open_ids

from . import support

REGISTER = """\
# Open questions — decisions and follow-through

Date: 2026-09-03 (extended 2026-09-04: O27, O28, O29)
Status: O1–O26 decided, follow-through open; O27–O29 are open work rather than
decisions — O28 part-executed 2026-09-04, O27 not started, O29 parked by owner
decision and awaiting reactivation

---

**O1 — a decision that is not open work.** Decided.

**O27 — R12's gate has never been run, and `fabric up` is why.** Be precise.

**O28 — Both Vendor-view follow-ups rest on inference, and one build retires
that.** One build retires it.

**O29 — The `mock-mcp-service` fix is committed, unpushed, and parked.** Parked.
"""

#: The register as a feature branch extends it: a fourth open entry that
#: `main` has never seen. This is the 2026-09-10 shape.
EXTENDED = REGISTER.replace("O27–O29 are open work", "O27–O30 are open work") + """
**O30 — Only on the branch.** Not yet on main.
"""

RELATIVE = "00-overview/open-questions.md"


class RegisterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        database = self.root / "sd.db"
        initialise(database)
        self.connection = connect(database)
        self.addCleanup(self.connection.close)
        self.checkout = support.repository(
            self.root / "simulator", bare=self.root / "simulator.git"
        )
        self.path = self.checkout / RELATIVE
        self.main_commit = self.land(REGISTER)

    def land(self, text: str, message: str = "the register") -> str:
        """Write the register, commit it on the current branch, push it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")
        commit = support.commit(self.checkout, message)
        support.push(self.checkout, support.git(self.checkout, "branch", "--show-current"))
        return commit

    def reader(self):
        return Reader.at(self.path)

    def items(self):
        return {
            row["external_id"]: row
            for row in self.connection.execute(
                "SELECT * FROM item WHERE source = ?", (SOURCE,)
            )
        }


class TheOpenSet(RegisterCase):
    def test_the_ids_come_from_the_register_s_own_header(self):
        found, sentence = open_ids(REGISTER)
        self.assertEqual(found, ["O27", "O28", "O29"])
        self.assertIn("are open work", sentence)

    def test_a_header_with_no_open_range_refuses(self):
        with self.assertRaises(MigrationRefused) as caught:
            open_ids(REGISTER.replace("O27–O29 are open work", "everything is fine"))
        self.assertIn("does not name an open range", str(caught.exception))

    def test_a_file_with_no_status_header_refuses(self):
        with self.assertRaises(MigrationRefused) as caught:
            open_ids(REGISTER.replace("Status:", "State:"))
        self.assertIn("`Status:` header", str(caught.exception))

    def test_a_header_naming_an_entry_the_file_does_not_have_refuses(self):
        self.land(REGISTER.replace("O27–O29", "O27–O30"), "a header ahead of its entries")
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader())
        self.assertIn("O30", str(caught.exception))


class ThePath(unittest.TestCase):
    def test_an_exported_empty_register_beats_the_file(self):
        """sd:3970, the same class: `st_source_env` keeps an exported empty value."""
        from sd_db.sources.register import REGISTER_RELATIVE, register_path
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / "sd-db"; conf.mkdir()
            (conf / ".env").write_text("SD_REGISTER=/elsewhere/register.md\n")
            env = {"SYSTEM_TOOLS_CONFIG": directory, "SD_REPO_ROOT": "/repos"}
            self.assertEqual(register_path(env), Path("/elsewhere/register.md"))
            self.assertEqual(register_path({**env, "SD_REGISTER": ""}), Path("/repos") / REGISTER_RELATIVE)


class TheImport(RegisterCase):
    def setUp(self):
        super().setUp()
        self.registered_path = add(self.connection, self.checkout, home=self.home)

    def test_only_the_open_entries_land(self):
        sitting = run(self.connection, self.reader())
        self.assertTrue(sitting.clean)
        self.assertEqual(sorted(self.items()), ["O27", "O28", "O29"])

    def test_the_title_is_the_entry_s_own_heading(self):
        run(self.connection, self.reader())
        self.assertEqual(
            self.items()["O27"]["title"],
            "R12's gate has never been run, and `fabric up` is why",
        )

    def test_a_heading_that_wraps_across_two_lines_is_read_whole(self):
        """The register hard-wraps at eighty columns.

        Two of the three open entries carry a heading that spans two lines,
        and a line-anchored pattern found O27 and missed them. Measured
        against the real register on 2026-09-06.
        """
        run(self.connection, self.reader())
        self.assertEqual(
            self.items()["O28"]["title"],
            "Both Vendor-view follow-ups rest on inference, and one build "
            "retires that",
        )

    def test_the_status_is_planning_and_the_prose_is_kept_verbatim(self):
        """The register writes state as English; a guess would read as a fact."""
        run(self.connection, self.reader())
        row = self.items()["O28"]
        self.assertEqual(row["status"], "planning")
        self.assertIn(
            "O28 part-executed 2026-09-04",
            json.loads(row["fields"])["register_status"],
        )

    def test_the_open_entries_land_as_tasks(self):
        """`work` is retiring (pack sd:7064): an open entry is a task."""
        run(self.connection, self.reader())
        self.assertEqual({row["kind"] for row in self.items().values()}, {"task"})

    def test_the_row_names_the_repository_it_came_from(self):
        run(self.connection, self.reader())
        self.assertEqual(self.items()["O29"]["repo"], self.registered_path)

    def test_a_second_run_reports_the_same_counts_with_zero_new_rows(self):
        first = run(self.connection, self.reader())
        second = run(self.connection, self.reader())
        self.assertEqual(first.counts.seen, second.counts.seen)
        self.assertEqual(second.counts.inserted, 0)
        self.assertEqual(second.counts.updated, 0)

    def test_a_reimport_keeps_an_area_the_operator_set(self):
        """The import owns `register_status`, not the whole `fields` (sd:8036)."""
        run(self.connection, self.reader())
        row = self.items()["O27"]
        held = {**json.loads(row["fields"]), "area": "home"}
        self.connection.execute(
            "UPDATE item SET fields = ? WHERE id = ?",
            (json.dumps(held, sort_keys=True), row["id"]),
        )
        second = run(self.connection, self.reader())
        self.assertEqual(second.counts.updated, 0)
        self.assertEqual(json.loads(self.items()["O27"]["fields"])["area"], "home")


class TheCommittedTreeOnOriginMain(RegisterCase):
    """What the checkout has on disk is not what lands; `origin/main` is.

    Each case builds the 2026-09-10 shape one way -- a feature branch, a
    dirty file, an unpushed `main` -- and asserts the same two things: the
    open set is `main`'s, and the row names the commit it was read from.
    """

    def setUp(self):
        super().setUp()
        self.registered_path = add(self.connection, self.checkout, home=self.home)

    def test_the_row_records_the_commit_it_was_read_from_and_no_branch(self):
        """`item.branch` is the branch the runner works on; `origin/main` is
        a remote-tracking name and not one (sd:462), so it stays NULL."""
        run(self.connection, self.reader())
        row = self.items()["O27"]
        self.assertEqual(row["source_commit"], self.main_commit)
        self.assertIsNone(row["branch"])
        self.assertEqual(row["path"], RELATIVE)

    def test_the_sitting_names_the_commit_it_read(self):
        sitting = run(self.connection, self.reader())
        self.assertIn(
            f"{SOURCE}: read {RELATIVE} from origin/main at {self.main_commit[:12]}",
            sitting.notes,
        )
        self.assertEqual(sitting.frozen.commits["O28"], self.main_commit)

    def test_a_feature_branch_checkout_does_not_change_what_lands(self):
        """The 2026-09-10 incident: O30 landed from `feat/worldgen-gate1000`."""
        support.git(self.checkout, "checkout", "-q", "-b", "feat/worldgen-gate1000")
        self.land(EXTENDED, "O30 on the branch")
        self.assertEqual(self.path.read_text(encoding="utf-8"), EXTENDED)
        sitting = run(self.connection, self.reader())
        self.assertTrue(sitting.clean)
        self.assertEqual(sorted(self.items()), ["O27", "O28", "O29"])
        self.assertEqual(self.items()["O27"]["source_commit"], self.main_commit)

    def test_a_dirty_working_copy_does_not_change_what_lands(self):
        self.path.write_text(EXTENDED, encoding="utf-8")
        sitting = run(self.connection, self.reader())
        self.assertTrue(sitting.clean)
        self.assertEqual(sorted(self.items()), ["O27", "O28", "O29"])
        self.assertEqual(self.items()["O29"]["source_commit"], self.main_commit)

    def test_an_unpushed_main_does_not_change_what_lands(self):
        """Local `main` ahead of `origin/main` is a branch nobody else can see."""
        self.path.write_text(EXTENDED, encoding="utf-8")
        ahead = support.commit(self.checkout, "O30, committed and unpushed")
        self.assertNotEqual(ahead, self.main_commit)
        run(self.connection, self.reader())
        self.assertEqual(sorted(self.items()), ["O27", "O28", "O29"])
        self.assertEqual(self.items()["O27"]["source_commit"], self.main_commit)

    def test_a_pushed_main_is_read_at_its_new_commit(self):
        """The counterpart: once O30 reaches `origin/main`, it lands, and the
        row moves to the commit that carried it."""
        run(self.connection, self.reader())
        moved = self.land(EXTENDED, "O30 on main")
        sitting = run(self.connection, self.reader())
        self.assertTrue(sitting.clean)
        self.assertEqual(sorted(self.items()), ["O27", "O28", "O29", "O30"])
        self.assertEqual(self.items()["O30"]["source_commit"], moved)
        self.assertEqual(self.items()["O27"]["source_commit"], moved)

    def test_origin_head_names_the_default_branch_when_it_is_not_main(self):
        """`default_branch` is the docs/work helper: `origin/HEAD`'s target."""
        support.git(self.checkout, "branch", "-m", "main", "trunk")
        support.push(self.checkout, "trunk")
        support.git(self.checkout, "remote", "set-head", "origin", "trunk")
        support.git(self.checkout, "checkout", "-q", "-b", "feat/elsewhere")
        self.land(EXTENDED, "O30 on the branch")
        sitting = run(self.connection, self.reader())
        self.assertEqual(sorted(self.items()), ["O27", "O28", "O29"])
        self.assertIn(
            f"{SOURCE}: read {RELATIVE} from origin/trunk at {self.main_commit[:12]}",
            sitting.notes,
        )

    def test_a_checkout_with_no_origin_reads_its_own_committed_head(self):
        """No remote to defer to; still the tree, never the working copy."""
        alone = support.repository(self.root / "alone")
        path = alone / RELATIVE
        path.parent.mkdir(parents=True)
        path.write_text(REGISTER, encoding="utf-8")
        head = support.commit(alone, "the register")
        path.write_text(EXTENDED, encoding="utf-8")
        add(self.connection, alone, home=self.home)
        sitting = run(self.connection, Reader.at(path))
        self.assertEqual(sorted(self.items()), ["O27", "O28", "O29"])
        self.assertIn(f"{SOURCE}: read {RELATIVE} from HEAD at {head[:12]}", sitting.notes)
        self.assertEqual(self.items()["O27"]["source_commit"], head)
        self.assertIsNone(self.items()["O27"]["branch"])


    def test_a_checkout_with_no_origin_and_no_commit_is_not_told_to_fetch(self):
        """No `origin` to fetch from: the remedy is a commit (sd:1226)."""
        alone = self.root / "empty"
        support.git(self.root, "init", "-q", "-b", "main", "empty")
        path = alone / RELATIVE
        path.parent.mkdir(parents=True)
        path.write_text(REGISTER, encoding="utf-8")
        with self.assertRaises(MigrationRefused) as caught:
            Reader.at(path).freeze()
        self.assertNotIn("git fetch origin", str(caught.exception))
        self.assertIn("commit the register", str(caught.exception))

    def test_a_row_whose_path_drifted_is_a_difference(self):
        """`land` writes `path`, so the verify must compare it (sd:1226)."""
        run(self.connection, self.reader())
        self.connection.execute(
            "UPDATE item SET path = ? WHERE source = ?", (str(self.path), SOURCE)
        )
        reader = self.reader()
        differences = verify(self.connection, reader, reader.freeze())
        self.assertEqual({one.what for one in differences}, {"path"})
        self.assertEqual(len(differences), 3)

class TheRepositoryMustBeRegistered(RegisterCase):
    def test_an_unregistered_repository_refuses_naming_the_command(self):
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, self.reader())
        self.assertIn("repo add", str(caught.exception))
        self.assertEqual(self.items(), {})

    def test_an_absent_register_refuses_rather_than_importing_zero(self):
        """On disk but never committed is absent: the tree is what is read."""
        gone = self.checkout / "gone.md"
        gone.write_text(REGISTER, encoding="utf-8")
        reader = Reader(path=gone, repo=str(self.checkout))
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, reader)
        self.assertIn("no register at origin/main:gone.md", str(caught.exception))

    def test_a_register_outside_its_repository_refuses(self):
        reader = Reader(path=self.root / "gone.md", repo=str(self.checkout))
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, reader)
        self.assertIn("is not inside", str(caught.exception))

    def test_a_repository_that_is_not_a_checkout_refuses(self):
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "register.md").write_text(REGISTER, encoding="utf-8")
        reader = Reader(path=plain / "register.md", repo=str(plain))
        with self.assertRaises(MigrationRefused) as caught:
            run(self.connection, reader)
        self.assertIn("is not a git checkout", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
