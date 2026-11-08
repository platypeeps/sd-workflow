"""pack sd:8050: board votes in the `vote` table (migration 048) and `sd_db.votes`.

The failure table in `docs/work/2026-11-07-vote-table/design.md` names the
tests here: the migration's failure and reverse, the import's one
transaction and its second run, and the stale revision. The fixtures use
synthetic directors and motions; nothing here is a real board's record.
"""

from __future__ import annotations

import csv
import io
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import create_item, initialise, removal, upsert_repo, votes
from sd_db import schema as schema_module
from sd_db.database import connect, schema_version, tables
from sd_db.migrate import migrate

VERSION = 48
HOA = "/repos/hoa"
OTHER = "/repos/other"

#: One row as `votes.csv` spells it, with its twelve columns.
CSV_ROW = {
    "id": "CI-1", "date": "2026-10-02", "topic": "Crime policy", "motion": "Buy the crime policy.",
    "type": "email vote", "moved": "Ada", "seconded": "Ben",
    "votes": "Ada, Ben, Cy Yes; Dee No; Eve none", "outcome": "adopted",
    "follow_up": "Send the binder", "source": "mail 1", "notes": "",
}


def row(**changes):
    return {**CSV_ROW, **changes}


def written(*rows):
    """`rows` as CSV text, with the first row's keys as the header."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.path = self.home / "sd.db"
        initialise(self.path)
        self.db = connect(self.path, write=True)
        self.addCleanup(self.db.close)
        upsert_repo(self.db, HOA)
        upsert_repo(self.db, OTHER)

    def count(self):
        return self.db.execute("SELECT count(*) FROM vote").fetchone()[0]

    def vote(self, key, repo=HOA):
        found = votes.get(self.db, repo, key)
        assert found is not None, key
        return found


class MigrationStore(unittest.TestCase):
    """A store migrated to a chosen version; no tests of its own, so a subclass reruns none (sd:8063)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "sd.db"

    def through(self, version):
        found = [m for m in schema_module.migrations() if m[0] <= version]
        with mock.patch("sd_db.migrate.migrations", return_value=found):
            return migrate(self.path)

    def dump(self):
        raw = sqlite3.connect(self.path)
        try:
            return [line for line in raw.iterdump() if "vote" not in line]
        finally:
            raw.close()


class TheMigration(MigrationStore):
    def test_048_adds_an_empty_vote_table_and_moves_nothing_else(self):
        self.through(VERSION - 1)
        before = self.dump()
        result = self.through(VERSION)
        self.assertEqual((result.before, result.applied), (VERSION - 1, [VERSION]))
        raw = sqlite3.connect(self.path)
        self.addCleanup(raw.close)
        columns = [r[1] for r in raw.execute("PRAGMA table_info(vote)")]
        self.assertEqual(columns, ["id", "repo", "key", "date", "topic", "motion", "type", "moved", "seconded",
                                   "votes", "tally", "outcome", "state", "follow_up", "source", "notes", "item",
                                   "revision", "created_at", "updated_at"])
        self.assertEqual(raw.execute("SELECT count(*) FROM vote").fetchone()[0], 0)
        self.assertIn("vote", schema_module.TABLES)
        self.assertEqual(sorted(tables(raw)), sorted(schema_module.TABLES))
        self.assertEqual(self.dump(), before)

    def test_a_failure_in_048_leaves_47_and_no_table(self):
        """Failure table, row "048 migrate": the second index cannot be made."""
        self.through(VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.execute("CREATE TABLE stuck (a)")
        raw.execute("CREATE INDEX vote_by_state ON stuck (a)")
        raw.commit()
        raw.close()
        with self.assertRaises(sqlite3.OperationalError):
            self.through(VERSION)
        raw = sqlite3.connect(self.path)
        self.assertEqual(schema_version(raw), VERSION - 1)
        self.assertNotIn("vote", tables(raw))
        raw.execute("DROP TABLE stuck")
        raw.commit()
        raw.close()
        self.assertEqual(self.through(VERSION).applied, [VERSION])

    def test_the_048_reverse_returns_the_47_shape(self):
        self.through(VERSION - 1)
        before = self.dump()
        self.through(VERSION)
        text = dict(schema_module.migrations())[VERSION].read_text(encoding="utf-8")
        raw = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(raw.close)
        raw.executescript("\n".join(line[4:] for line in text.splitlines() if line.startswith("--   ")))
        self.assertEqual(schema_version(raw), VERSION - 1)
        self.assertNotIn("vote", tables(raw))
        self.assertEqual(self.dump(), before)


class TheTally(unittest.TestCase):
    def test_the_shapes_the_register_writes(self):
        cases = {
            "Ada, Ben, Cy Yes; Dee No; Eve none": {"Ada": "yes", "Ben": "yes", "Cy": "yes", "Dee": "no", "Eve": "none"},
            "5-0: Ada, Ben, Cy, Dee, Eve": dict.fromkeys(("Ada", "Ben", "Cy", "Dee", "Eve"), "yes"),
            "2-3: Ada, Ben Yes; Cy, Dee, Eve No": {"Ada": "yes", "Ben": "yes", "Cy": "no", "Dee": "no", "Eve": "no"},
            "unanimous: Ada, Ben, Cy, Dee; Eve absent": {"Ada": "yes", "Ben": "yes", "Cy": "yes", "Dee": "yes", "Eve": "absent"},
            "Ada, Ben Yes; Cy agreed; Dee agreed (9/29)": {"Ada": "yes", "Ben": "yes", "Cy": "yes", "Dee": "yes"},
            "Ada and Ben agreed": {"Ada": "yes", "Ben": "yes"},
            "Ada abstained; Ben Yes": {"Ada": "abstain", "Ben": "yes"},
            # sd:8063: `and` splits names in any case, never joins them into one.
            "Ada And Ben agreed": {"Ada": "yes", "Ben": "yes"},
            "Ada AND Ben Yes; Cy No": {"Ada": "yes", "Ben": "yes", "Cy": "no"},
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(votes.parse_votes(text), want)

    def test_a_cell_without_names_or_with_one_unread_segment_has_no_tally(self):
        for text in ("", "unanimous", "5-0", "not recorded", "majority; tally not recorded", "none",
                     "Ada Yes; only some directors replied", "Ada Yes; Ada No", "3-0: Ada, Ben Yes",
                     "Ada and Ben agreed by text", "Ada Yes;", "see motion", "x" * 5000,
                     # sd:8063: a name holding a verdict word is no director's: a `,` for a `;`.
                     "Ada Yes, Ben No", "Ada, Ben No Yes"):
            with self.subTest(text=text[:40]):
                self.assertIsNone(votes.parse_votes(text))


class TheWrites(Store):
    def test_add_stores_the_row_with_its_tally_state_and_revision_one(self):
        vote = votes.add(self.db, HOA, "CI-1", {k: v for k, v in CSV_ROW.items() if k != "id"})
        self.assertEqual((vote["key"], vote["repo"], vote["state"], vote["revision"]), ("CI-1", HOA, "closed", 1))
        self.assertEqual(vote["tally"]["Dee"], "no")
        self.assertEqual(votes.get(self.db, HOA, "CI-1"), vote)

    def test_a_pending_outcome_is_open_and_an_explicit_state_wins(self):
        self.assertEqual(votes.add(self.db, HOA, "A-1", {"outcome": "pending: not sent"})["state"], "open")
        self.assertEqual(votes.add(self.db, HOA, "A-2", {"outcome": "Pending vote"})["state"], "open")
        self.assertEqual(votes.add(self.db, HOA, "A-3", {"outcome": "pending"}, state="closed")["state"], "closed")
        self.assertEqual(votes.add(self.db, HOA, "A-4", {})["state"], "closed")

    def test_a_key_is_unique_per_repo_only(self):
        votes.add(self.db, HOA, "CI-1", {})
        with self.assertRaises(votes.VoteRefused) as caught:
            votes.add(self.db, HOA, "CI-1", {})
        self.assertIn("CI-1", str(caught.exception))
        self.assertEqual(votes.add(self.db, OTHER, "CI-1", {})["repo"], OTHER)

    def test_an_unregistered_repo_a_bad_key_date_state_or_field_is_refused_by_name(self):
        bad = [(("/repos/nowhere", "K-1", {}), "not a registered repo"),
               ((HOA, "", {}), "key"), ((HOA, "a key", {}), "key"), ((HOA, "CI-é", {}), "key"),
               ((HOA, "K" * 65, {}), "key"),
               ((HOA, "K-1", {"date": "Oct 2"}), "date"), ((HOA, "K-1", {"date": "2026-02-30"}), "date"),
               ((HOA, "K-1", {"colour": "red"}), "colour"),
               ((HOA, "K-1", {"motion": "m" * (votes.MAX_TEXT + 1)}), "motion")]
        for (repo, key, fields), word in bad:
            with self.subTest(key=key[:10], fields=list(fields)), self.assertRaises(votes.VoteRefused) as caught:
                votes.add(self.db, repo, key, fields)
            self.assertIn(word, str(caught.exception))
        with self.assertRaises(votes.VoteRefused):
            votes.add(self.db, HOA, "K-1", {}, state="maybe")
        self.assertEqual(self.count(), 0)

    def test_edit_raises_the_revision_and_rereads_the_tally_and_state(self):
        votes.add(self.db, HOA, "CI-1", {"outcome": "pending: not voted"})
        vote = votes.edit(self.db, HOA, "CI-1", {"outcome": "adopted", "votes": "Ada, Ben Yes"}, expected_revision=1)
        self.assertEqual((vote["revision"], vote["state"], vote["tally"]), (2, "closed", {"Ada": "yes", "Ben": "yes"}))
        # The same values again change nothing, so the revision holds.
        self.assertEqual(votes.edit(self.db, HOA, "CI-1", {"outcome": "adopted"})["revision"], 2)
        self.assertEqual(votes.edit(self.db, HOA, "CI-1", {"state": "open"})["state"], "open")

    def test_a_stale_revision_is_refused(self):
        """Failure table, row "edit": a write between a reader's read and its write."""
        votes.add(self.db, HOA, "CI-1", {})
        votes.edit(self.db, HOA, "CI-1", {"notes": "theirs"})
        with self.assertRaises(votes.StaleVote):
            votes.edit(self.db, HOA, "CI-1", {"notes": "mine"}, expected_revision=1)
        self.assertEqual(self.vote("CI-1")["notes"], "theirs")
        with self.assertRaises(votes.VoteRefused):
            votes.edit(self.db, HOA, "NO-1", {"notes": "x"})

    def test_an_item_link_is_set_and_cleared(self):
        item = create_item(self.db, kind="task", title="a vote follow-up", repo=HOA)
        self.assertEqual(votes.add(self.db, HOA, "CI-1", {}, item=item)["item"], item)
        self.assertIsNone(votes.edit(self.db, HOA, "CI-1", {"item": None})["item"])
        with self.assertRaises(votes.VoteRefused):
            votes.edit(self.db, HOA, "CI-1", {"item": 999999})

    def test_rows_are_open_first_then_newest_first(self):
        votes.add(self.db, HOA, "OLD", {"date": "2025-01-01"})
        votes.add(self.db, HOA, "NEW", {"date": "2026-01-01"})
        votes.add(self.db, HOA, "OPEN-OLD", {"date": "2024-01-01", "outcome": "pending"})
        votes.add(self.db, HOA, "NODATE", {})
        votes.add(self.db, OTHER, "ELSE", {"date": "2026-06-01"})
        self.assertEqual([r["key"] for r in votes.rows(self.db, repo=HOA)], ["OPEN-OLD", "NEW", "OLD", "NODATE"])
        self.assertEqual([r["key"] for r in votes.rows(self.db, repo=HOA, state="open")], ["OPEN-OLD"])


class TheImport(Store):
    def test_rows_shaped_like_votes_csv_land_and_a_second_import_changes_nothing(self):
        """Failure table, row "import again"."""
        file = [row(), row(id="PG-1", outcome="pending: not sent", votes="none", date="")]
        self.assertEqual(votes.import_rows(self.db, HOA, file), {"added": 2, "updated": 0, "unchanged": 0})
        before = [dict(r) for r in self.db.execute("SELECT * FROM vote ORDER BY id")]
        self.assertEqual(votes.import_rows(self.db, HOA, file), {"added": 0, "updated": 0, "unchanged": 2})
        self.assertEqual([dict(r) for r in self.db.execute("SELECT * FROM vote ORDER BY id")], before)
        self.assertEqual(self.vote("PG-1")["state"], "open")
        self.assertIsNone(self.vote("PG-1")["date"])

    def test_a_changed_row_updates_in_place_and_keeps_its_item_link(self):
        item = create_item(self.db, kind="task", title="follow", repo=HOA)
        votes.import_rows(self.db, HOA, [row()])
        votes.edit(self.db, HOA, "CI-1", {"item": item})
        self.assertEqual(votes.import_rows(self.db, HOA, [row(outcome="rescinded")]),
                         {"added": 0, "updated": 1, "unchanged": 0})
        vote = self.vote("CI-1")
        self.assertEqual((vote["outcome"], vote["item"], vote["revision"], self.count()), ("rescinded", item, 3, 1))

    def test_a_bad_row_late_in_the_file_writes_nothing(self):
        """Failure table, row "import": every row is read before the first write."""
        file = [row(id=f"K-{n}") for n in range(20)] + [row(id="K-20", date="soon")]
        with self.assertRaises(votes.VoteRefused) as caught:
            votes.import_rows(self.db, HOA, file)
        self.assertIn("row 22", str(caught.exception))
        self.assertIn("K-20", str(caught.exception))
        self.assertEqual(self.count(), 0)

    def test_a_stop_between_two_row_writes_lands_nothing_and_a_rerun_finishes(self):
        """Failure table, row "import": the process stops after some rows are written."""
        file = [row(id=f"K-{n}") for n in range(5)]
        real, seen = votes._write, []

        def stop(*args, **kwargs):
            seen.append(1)
            if len(seen) == 3:
                raise KeyboardInterrupt
            return real(*args, **kwargs)

        with mock.patch.object(votes, "_write", stop), self.assertRaises(KeyboardInterrupt):
            votes.import_rows(self.db, HOA, file)
        self.assertEqual(self.count(), 0)
        self.assertEqual(votes.import_rows(self.db, HOA, file)["added"], 5)

    def test_duplicate_keys_an_unknown_column_or_an_extra_cell_refuse_the_file(self):
        for file, word in (([row(), row(motion="other")], "CI-1"), ([{**row(), "colour": "red"}], "colour"),
                           ([{**row(), None: ["spill"]}], "more cells"), ([{"date": "2026-01-01"}], "id")):
            with self.subTest(word=word), self.assertRaises(votes.VoteRefused) as caught:
                votes.import_rows(self.db, HOA, file)
            self.assertIn(word, str(caught.exception))
        self.assertEqual(self.count(), 0)

    def test_a_short_row_refuses_the_file_and_keeps_the_stored_vote(self):
        """Lead review of sd:8050: csv.DictReader gives a short row's missing cells as None, which must not
        become '' and erase the stored votes, outcome and notes, nor flip an open vote to closed."""
        votes.import_rows(self.db, HOA, [row(outcome="pending: not sent", notes="keep me")])
        before = self.vote("CI-1")
        short = ",".join(votes.CSV_COLUMNS) + "\nCI-1,2026-10-02,Replacement topic\n"
        with self.assertRaises(votes.VoteRefused) as caught:
            votes.import_rows(self.db, HOA, csv.DictReader(io.StringIO(short)))
        self.assertIn("row 2 (CI-1): the row has fewer cells than the header", str(caught.exception))
        self.assertEqual(self.vote("CI-1"), before)

    def test_a_header_missing_a_column_refuses_naming_it_and_keeps_the_stored_vote(self):
        """Lead review of sd:8050: a header without a votes.csv column would blank that column on update."""
        votes.import_rows(self.db, HOA, [row(outcome="pending: not sent", notes="keep me")])
        before = self.vote("CI-1")
        for text, missing in (("id,motion,votes,outcome,notes\nCI-1,Replacement motion\n",
                               "date, topic, type, moved, seconded, follow_up, source"),
                              (written({c: v for c, v in row().items() if c not in ("source", "notes")}),
                               "source, notes")):
            with self.subTest(missing=missing):
                with self.assertRaises(votes.VoteRefused) as caught:
                    votes.import_rows(self.db, HOA, csv.DictReader(io.StringIO(text)))
                self.assertIn(f"the header has no {missing} column", str(caught.exception))
        self.assertEqual(self.vote("CI-1"), before)

    def test_extreme_rows_either_land_whole_or_refuse_with_nothing_written(self):
        """A property over extreme cells: empty, very long, unicode, control characters, duplicate keys, cells
        that do not parse. Each file either lands every row as written, or refuses and writes nothing."""
        pick = random.Random(8050)
        cells = ["", " ", "x" * votes.MAX_TEXT, "x" * (votes.MAX_TEXT + 1), "Ünïcödé — 理事会 🗳️", "a\nb\r\nc",
                 "\t", "NUL\x00inside", "'; DROP TABLE vote; --", "pending", "PENDING: later", "Ada Yes; ???",
                 "5-0: Ada", "Ada, Ada Yes", ";;;", "(only a note)", "0" * 64]
        keys = ["K-1", "K-2", "k.3", "K-1", "", "K 4", "Κ-5", "K-" + "9" * 70, "A_6"]
        dates = ["", "2026-11-07", "1900-01-01", "9999-12-31", "2026-13-01", "26-11-07", "2026-11-07T00:00"]
        for attempt in range(60):
            file = []
            for _ in range(pick.randint(0, 6)):
                one = {column: pick.choice(cells) for column in votes.FIELDS}
                one["id"], one["date"] = pick.choice(keys), pick.choice(dates)
                file.append(one)
            self.db.execute("DELETE FROM vote")
            with self.subTest(attempt=attempt):
                try:
                    counts = votes.import_rows(self.db, HOA, file)
                except votes.VoteRefused:
                    self.assertEqual(self.count(), 0)
                    continue
                self.assertEqual(counts["added"], len(file))
                for one in file:
                    stored = self.vote(one["id"].strip())
                    for column in votes.FIELDS:
                        if column != "date":
                            self.assertEqual(stored[column], one[column].strip(), column)
                    self.assertEqual(stored["state"], votes.state_of(one["outcome"]))
                    self.assertEqual(stored["tally"], votes.parse_votes(one["votes"].strip()))


class TheRemoval(Store):
    def test_a_repo_with_votes_is_refused(self):
        """Failure table, row "repo or item removal"."""
        votes.add(self.db, OTHER, "CI-1", {})
        plan = removal.plan_repo(self.db, OTHER, with_items=True, home=self.home)
        self.assertEqual([r["key"] for r in plan["refusals"] if r["code"] == "P7"], ["CI-1"])
        self.assertNotIn("G7", {r["code"] for r in plan["refusals"]})

    def test_an_item_a_vote_links_is_refused(self):
        item = create_item(self.db, kind="task", title="linked", repo=HOA, status="done")
        votes.add(self.db, HOA, "CI-1", {}, item=item)
        plan = removal.plan_item(self.db, item, home=self.home)
        self.assertEqual([r["key"] for r in plan["refusals"] if r["code"] == "I11"], ["CI-1"])


class TheOlderStore(MigrationStore):
    def test_a_removal_preview_reads_a_47_store_with_no_vote_table(self):
        """Older state: `sd-db repo remove` without --apply reads the store, which may predate 048."""
        self.through(VERSION - 1)
        raw = sqlite3.connect(self.path)
        raw.execute("INSERT INTO repo (path, created_at, updated_at) VALUES ('/repos/old', 't', 't')")
        raw.execute("INSERT INTO item (kind, title, status, repo, created_at, updated_at)"
                    " VALUES ('task', 'kept', 'done', '/repos/old', 't', 't')")
        raw.commit()
        raw.close()
        db = connect(self.path, write=False)
        self.addCleanup(db.close)
        home = self.path.parent
        codes = {r["code"] for r in removal.plan_repo(db, "/repos/old", with_items=True, home=home)["refusals"]}
        self.assertFalse(codes & {"P7", "I11"})
        self.assertNotIn("I11", {r["code"] for r in removal.plan_item(db, 1, home=home)["refusals"]})


if __name__ == "__main__":
    unittest.main()
