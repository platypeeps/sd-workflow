"""`sd usage` and the read behind it (sd:234 slice 8d, criterion 15).

The verb is `sd-db.sh usage`, run through the entrypoint on the store the
`LedgerCase` fixture writes under its own `HOME`, so what is asserted is
what a person sees. The read is `reads.usage_month`; `--json` is held to
its bytes, which the Usage screen's `/api/usage` serves too.
"""

import os
import sqlite3
import subprocess
import sys

from sd_db import connect, provider_controls, reads, usage, writes
from sd_db.errors import SdDbError
from sd_db.ledger import claim, lose, reserve, settle
from sd_db.testing.wire import hub_only

from . import support
from .test_ledger import ME, REGISTRY, LedgerCase, dead_pid

REGISTRY_WITH_EXTRA_BILL = REGISTRY.replace("  open:   { cost: subscription }\n", "  open:   { cost: subscription }\n  extra:  { cost: subscription }\n")

ENTRYPOINT = support.SD_DB


class UsageCase(LedgerCase):
    def sd_db(self, *args, expect=0):
        environment = {**os.environ, "HOME": str(self.home), "PYTHONPATH": str(support.LIB)}
        environment.pop("XDG_STATE_HOME", None)
        completed = subprocess.run([sys.executable, str(ENTRYPOINT), *args], capture_output=True, text=True,
                                   input="", env=environment)
        self.assertEqual(completed.returncode, expect, completed.stdout + completed.stderr)
        return completed

    def month(self):
        """A lost call on the capped bill, a settled one on the open bill, and a
        `sending` row whose owner is dead: two bills, one `bound` row, one orphan."""
        row = self.assignment()
        reserve(self.db, bill="capped", bound=2.0, call_id="lost", owner_pid=ME, assignment=row,
                provider="kimi", role="author", now="2026-09-10T10:00:00+00:00")
        claim(self.db, "lost", now="2026-09-10T10:00:01+00:00")
        lose(self.db, "lost")
        reserve(self.db, bill="open", bound=1.0, call_id="ok", owner_pid=ME, provider="mini",
                role="reviewer", now="2026-09-11T10:00:00+00:00")
        claim(self.db, "ok", now="2026-09-11T10:00:01+00:00")
        settle(self.db, "ok", usd=0.5, tokens_in=10, tokens_out=20)
        reserve(self.db, bill="capped", bound=3.0, call_id="orphan", owner_pid=dead_pid(),
                provider="kimi", role="author", now="2026-09-12T10:00:00+00:00")
        claim(self.db, "orphan", now="2026-09-12T10:00:01+00:00")
        return row


class TheVerb(UsageCase):
    @hub_only
    def test_it_prints_the_four_numbers_lists_the_bound_row_and_binds_the_orphan(self):
        row = self.month()
        before = reads.usage_month(self.db, month="2026-09")
        self.assertEqual([(bill.name, bill.spent, bill.estimated, bill.held, bill.cap) for bill in before.bills],
                         [("capped", 2.0, 2.0, 3.0, 10.0), ("open", 0.5, 0.0, 0.0, None)])
        completed = self.sd_db("usage", "--month", "2026-09")
        for line in ("usage 2026-09: $5.50 spent, $0.00 held",
                     "capped (company): spent $5.00, estimated $5.00, held $0.00, cap $10.00, room $5.00",
                     "open (subscription): spent $0.50, estimated $0.00, held $0.00, cap —",
                     "capped / kimi / author: 2 call(s), spent $5.00",
                     "bound rows (2)",
                     f"2026-09-10T10:00:01+00:00 lost capped kimi author assignment {row}: $2.00",
                     "orphan capped kimi author assignment -: $3.00"):
            self.assertIn(line, completed.stdout)
        self.assertIn("bound 1 sending row(s)", completed.stderr)
        # The orphan is `bound` afterwards, and the item screen's read agrees
        # with the bill's estimated share for the assignment it carried.
        self.assertEqual(self.row("orphan")["source"], "bound")
        after = reads.usage_month(self.db, month="2026-09")
        self.assertEqual([bound["call_id"] for bound in after.bound], ["lost", "orphan"])
        self.assertEqual(after.bills[0].held, 0.0)
        assignments = reads.item_assignments(self.db, self.item)
        self.assertEqual((assignments[0]["usd"], assignments[0]["estimated"]), (2.0, 1))

    @hub_only
    def test_it_reports_token_use_by_area(self):
        """Builder runs (role author) are coding, reviewer calls review (sd:10307)."""
        self.month()
        found = reads.usage_month(self.db, month="2026-09")
        self.assertEqual([(area["area"], area["calls"], area["tokens_out"]) for area in found.areas],
                         [("coding", 2, 0), ("review", 1, 20)])
        completed = self.sd_db("usage", "--month", "2026-09")
        self.assertIn("areas:\n  coding: 2 call(s), spent $5.00, tokens 0 in / 0 out\n"
                      "  review: 1 call(s), spent $0.50, tokens 10 in / 20 out\n", completed.stdout)

    def test_a_draft_runs_tokens_are_design_not_coding(self):
        """A specification draft is an author run on a `draft` assignment;
        its tokens are not coding's (sd:10307 review)."""
        draft = self.assignment()
        self.db.execute("UPDATE assignment SET scope = 'draft' WHERE id = ?", (draft,))
        writes.record_cost(self.db, source="run", role="author", assignment=draft, tokens_in=7, tokens_out=900,
                           usd=4.0)
        writes.record_cost(self.db, source="run", role="author", assignment=self.assignment(), tokens_in=3,
                           tokens_out=50, usd=1.0)
        found = reads.usage_month(self.db)
        self.assertEqual([(area["area"], area["calls"], area["spent"], area["tokens_in"], area["tokens_out"])
                          for area in found.areas],
                         [("coding", 1, 1.0, 3, 50), ("design", 1, 4.0, 7, 900)])

    def test_area_totals_past_sqlite_integers_keep_the_report(self):
        """Two providers' author rows whose token sum passes SQLite's
        64-bit range: each provider group holds, so the area total
        must too (sd:10307 lane review)."""
        big = 2**63 - 1
        for provider, tokens in (("kimi", big), ("mini", 1)):
            writes.record_cost(self.db, source="run", provider=provider, role="author",
                               assignment=self.assignment(), tokens_in=tokens, tokens_out=tokens, usd=1.0)
        found = reads.usage_month(self.db)
        self.assertEqual([(area["area"], area["calls"], area["spent"], area["tokens_in"], area["tokens_out"])
                          for area in found.areas],
                         [("coding", 2, 2.0, big + 1, big + 1)])

    @hub_only
    def test_json_is_the_read_byte_for_byte(self):
        self.month()
        completed = self.sd_db("usage", "--month", "2026-09", "--json")
        self.assertEqual(completed.stdout, usage.json_text(reads.usage_month(self.db, month="2026-09")))
        self.assertTrue(completed.stdout.endswith("}\n"))

    def test_a_fresh_store_prints_zeros_and_no_rows(self):
        completed = self.sd_db("usage", "--month", "2026-09")
        self.assertIn("usage 2026-09: $0.00 spent, $0.00 held", completed.stdout)
        self.assertIn("capped (company): spent $0.00, estimated $0.00, held $0.00, cap $10.00, room $10.00", completed.stdout)
        self.assertIn("bound rows (0)", completed.stdout)
        self.assertNotIn("roles:", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_it_refuses_a_month_that_is_not_one(self):
        completed = self.sd_db("usage", "--month", "2026-13", expect=1)
        self.assertIn("a month is YYYY-MM, not '2026-13'", completed.stderr)
        self.assertIn("takes --month", self.sd_db("usage", "--week", expect=1).stderr)

    def test_the_last_month_datetime_has_is_refused_and_not_an_overflow(self):
        """`9999-12` parses, and measuring its length steps into year 10000.

        `_usage_reads` takes that step (`first.replace(day=28) + timedelta
        (days=4)`) after `month_of` had already passed the month, so the verb
        died with an uncaught `OverflowError` and served a traceback instead
        of the refusal every other unusable month gets.
        """
        with self.assertRaises(SdDbError) as refused:
            reads.month_of("9999-12")
        self.assertIn("a month is YYYY-MM, not '9999-12'", str(refused.exception))
        with self.assertRaises(SdDbError):
            reads.usage_month(self.db, month="9999-12")
        completed = self.sd_db("usage", "--month", "9999-12", expect=1)
        self.assertIn("a month is YYYY-MM, not '9999-12'", completed.stderr)
        self.assertNotIn("OverflowError", completed.stderr)

    @hub_only
    def test_a_month_that_is_not_one_sweeps_nothing(self):
        """The month is checked before the sweep: a refused invocation
        leaves the dead owner's `sending` row as it found it."""
        self.month()
        self.assertIn("a month is YYYY-MM", self.sd_db("usage", "--month", "2026-13", expect=1).stderr)
        self.assertEqual(self.row("orphan")["source"], "sending")
        with self.assertRaises(SdDbError):
            usage.report(self.db, month="soon", is_alive=lambda pid: pid == ME)
        self.assertEqual(self.row("orphan")["source"], "sending")


@hub_only
class TheRead(UsageCase):
    def test_the_month_boundary(self):
        """A row settled at 23:59 on the last day and one at 00:00 the next
        are in different months, the old month's total never holding the new
        month's row (`prd.md:1557-1561`); the sweep is not the read."""
        for call_id, moment in (("late", "2026-09-30T23:59:00+00:00"), ("early", "2026-10-01T00:00:00+00:00")):
            reserve(self.db, bill="capped", bound=1.0, call_id=call_id, owner_pid=ME, now=moment)
            claim(self.db, call_id, now=moment)
            lose(self.db, call_id)
        september = reads.usage_month(self.db, month="2026-09", now="2026-10-02T00:00:00Z")
        october = reads.usage_month(self.db, month="2026-10", now="2026-10-02T00:00:00Z")
        self.assertEqual([row["call_id"] for row in september.bound], ["late"])
        self.assertEqual([row["call_id"] for row in october.bound], ["early"])
        self.assertEqual((september.bills[0].spent, october.bills[0].spent), (1.0, 1.0))
        self.assertEqual((september.today, october.today, october.days, september.days), (None, 2, 31, 30))
        self.assertEqual(october.bills[0].burn, ((1, 1.0),))
        self.assertEqual(reads.usage_month(self.db, now="2026-09-16T12:00:00Z").month, "2026-09")

    def test_the_read_writes_nothing_and_report_sweeps_first(self):
        self.month()
        before = tuple(self.db.iterdump())
        reads.usage_month(self.db, month="2026-09")
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.assertEqual(usage.report(self.db, month="2026-09", sweep=False)[1].bound, ())
        self.assertEqual(self.row("orphan")["source"], "sending")
        found, released = usage.report(self.db, month="2026-09", is_alive=lambda pid: pid == ME)
        self.assertEqual(released.bound, ("orphan",))
        self.assertEqual(found.bills[0].estimated, 5.0)

    def test_a_session_row_without_a_total_and_two_meter_samples_at_one_moment(self):
        """A start-session `run` row may carry `usd` NULL (the runner records
        one when the session reports no total); the burn counts it as
        nothing and does not fall over. Two meter samples at one timestamp
        resolve to the later row, not to whichever SQLite picks."""
        self.month()
        writes.record_cost(self.db, source="run", provider="kimi", bill="capped", role="author", usd=None)
        self.db.execute("UPDATE cost SET timestamp = '2026-09-13T10:00:00+00:00' WHERE usd IS NULL")
        for percent in (30, 70):
            row = writes.record_cost(self.db, source="meter", provider="kimi", bill="capped",
                                     window_minutes=300, used_percent=percent)
            self.db.execute("UPDATE cost SET timestamp = '2026-09-13T12:00:00+00:00' WHERE id = ?", (row,))
        self.db.commit()
        found = reads.usage_month(self.db, month="2026-09")
        self.assertEqual(found.bills[0].burn, ((10, 2.0), (13, 2.0)))
        self.assertEqual([(row["window_minutes"], row["used_percent"]) for row in found.meter], [(300, 70)])
        self.assertIn("capped (company): spent $5.00", self.sd_db("usage", "--month", "2026-09").stdout)

    def test_a_row_on_no_bill_is_in_the_months_spend(self):
        """pack sd:7219: a start session's row may name no bill. The bill list
        leaves it out, so the month's total read it as nothing."""
        self.month()
        for usd, at in ((4.0, "2026-09-13T10:00:00+00:00"), (9.0, "2026-08-13T10:00:00+00:00")):
            row = writes.record_cost(self.db, source="run", provider="claude", bill=None, role="author", usd=usd)
            self.db.execute("UPDATE cost SET timestamp = ? WHERE id = ?", (at, row))
        self.db.commit()
        found = usage.read(self.db, month="2026-09")
        self.assertEqual(found.spent, 6.5)
        self.assertEqual(found.unbilled, 4.0)
        self.assertEqual(found.document()["unbilled"], 4.0)
        self.assertIsNone(usage.read(self.db, month="2026-07").unbilled)
        # The verb binds the $3.00 orphan first, as it does for every month.
        printed = self.sd_db("usage", "--month", "2026-09").stdout
        self.assertIn("usage 2026-09: $9.50 spent", printed)
        self.assertIn("  no bill: spent $4.00", printed)

    def test_the_projection_is_one_snapshot(self):
        """A `run` row committed by another connection between two of the
        read's SELECTs is in all of the projection or in none of it: the
        burn line's last point is the bill's `spent`."""
        self.month()
        other = connect(home=self.home)
        self.addCleanup(other.close)
        inner = self.db

        class Interleaved:
            """`self.db`, with a write on `other` before the fourth SELECT."""
            selects = 0

            def execute(this, sql, parameters=()):
                if sql.lstrip().upper().startswith("SELECT"):
                    this.selects += 1
                    if this.selects == 4:
                        writes.record_cost(other, source="run", provider="kimi", bill="capped",
                                           role="author", usd=1.0)
                        other.execute("UPDATE cost SET timestamp = '2026-09-14T10:00:00+00:00' "
                                      "WHERE usd = 1.0 AND source = 'run'")
                return inner.execute(sql, parameters)

            def __getattr__(this, name):
                return getattr(inner, name)

        found = reads.usage_month(Interleaved(), month="2026-09")
        self.assertEqual(found.bills[0].burn[-1][1], found.bills[0].spent)
        self.assertFalse(inner.in_transaction)
        # After the read, the row is there for the next one.
        self.assertEqual(reads.usage_month(self.db, month="2026-09").bills[0].spent, 3.0)


class TheMergedCaps(UsageCase):
    """#435's residue (sd:234 slice 12h). A legacy row cap on a bill a `start`
    entry is billed to is what `registry.merge` leaves out of the merged view
    and reports in `warnings` (#433); `provider_controls.snapshot` shows that
    view. The tile's read and the month's read take the same caps, `usage.caps`,
    so neither prints a cap the reader refuses; the row itself is what the
    panel's clear form reads, and it stays the row."""

    def legacy_cap(self):
        """The bare `UPDATE` #433's test seeds, on `open`, the bill `claude`,
        a `start` entry, is billed to; the reader then carries the sentence."""
        self.assertEqual(self.db.execute("UPDATE bill SET cap_usd_month = 5.0 WHERE name = 'open'").rowcount, 1)
        state = provider_controls.snapshot(self.db)
        self.assertEqual(len(state["warnings"]), 1)
        self.assertIn("provider 'claude' is a 'start' entry on the capped bill 'open'", state["warnings"][0])
        return state

    @hub_only
    def test_a_warned_bill_has_no_cap_on_the_tile_or_the_month_and_the_url_bill_keeps_its_own(self):
        self.month()
        state = self.legacy_cap()
        self.assertEqual(usage.caps(self.db), {bill["name"]: bill["cap_usd_month"] for bill in state["bills"]})
        found = usage.read(self.db, month="2026-09")
        self.assertEqual([(bill.name, bill.cap, bill.room) for bill in found.bills],
                         [("capped", 10.0, 5.0), ("open", None, None)])
        self.assertEqual(usage.report(self.db, month="2026-09", sweep=False)[0], found)
        # The tile sums the current month, so it reads at a fixed time in the rows' month (sd:2288).
        now = "2026-09-16T12:00:00Z"
        tile = usage.bills(self.db, now=now)
        self.assertEqual([(row["name"], row["cap_usd_month"], row["spent"]) for row in tile],
                         [("capped", 10.0, 2.0), ("open", None, 0.5)])
        # The overlay is the statement's: the rows are still the module's `sqlite3.Row`s.
        self.assertIsInstance(tile[0], sqlite3.Row)
        self.assertEqual(tile, reads.cost_by_bill(self.db, now=now, caps=usage.caps(self.db)))
        # Without `caps` the read is the row, which is what the panel's clear form asks for.
        self.assertEqual([row["cap_usd_month"] for row in reads.cost_by_bill(self.db)], [10.0, 5.0])
        self.assertEqual(reads.usage_month(self.db, month="2026-09").bills[1].cap, 5.0)

    @hub_only
    def test_the_verb_prints_the_merged_caps_and_its_json_is_the_merged_read(self):
        self.month()
        self.legacy_cap()
        completed = self.sd_db("usage", "--month", "2026-09")
        self.assertIn("  open (subscription): spent $0.50, estimated $0.00, held $0.00, cap \u2014\n", completed.stdout)
        self.assertIn("  capped (company): spent $5.00, estimated $5.00, held $0.00, cap $10.00, room $5.00\n",
                      completed.stdout)
        printed = self.sd_db("usage", "--month", "2026-09", "--json").stdout
        self.assertEqual(printed, usage.json_text(usage.read(self.db, month="2026-09")))
        self.assertNotEqual(printed, usage.json_text(reads.usage_month(self.db, month="2026-09")))

    @hub_only
    def test_the_clear_leaves_the_row_as_the_cap_and_a_cap_set_on_the_url_bill_prints(self):
        self.month()
        state = self.legacy_cap()
        cleared = provider_controls.set_cap(self.db, "open", None, expected_revision=state["revision"], who="test")
        self.assertEqual(cleared["warnings"], [])
        self.assertEqual(usage.caps(self.db), {"capped": 10.0, "open": None})
        self.assertEqual(usage.read(self.db, month="2026-09"), reads.usage_month(self.db, month="2026-09"))
        provider_controls.set_cap(self.db, "capped", 20.0, expected_revision=cleared["revision"], who="test")
        found = usage.read(self.db, month="2026-09")
        self.assertEqual([(bill.name, bill.cap, bill.room) for bill in found.bills],
                         [("capped", 20.0, 15.0), ("open", None, None)])
        self.assertEqual(usage.bills(self.db)[0]["cap_usd_month"], 20.0)

    def test_without_a_registry_beside_the_store_the_row_is_the_cap(self):
        """A store with no file is what every other read gets: the rows."""
        self.registry_path.unlink()
        self.assertEqual(self.db.execute("UPDATE bill SET cap_usd_month = 5.0 WHERE name = 'open'").rowcount, 1)
        self.assertIsNone(usage.caps(self.db))
        found = usage.read(self.db, month="2026-09")
        self.assertEqual(found, reads.usage_month(self.db, month="2026-09"))
        self.assertEqual([(bill.name, bill.cap) for bill in found.bills], [("capped", 10.0), ("open", 5.0)])
        self.assertEqual(reads.cost_by_bill(self.db, caps=None)[1]["cap_usd_month"], 5.0)

    def test_a_registry_that_cannot_be_opened_or_parsed_leaves_the_row_as_the_cap(self):
        """#438's review: the dashboard's provider controls catch `SdDbError`,
        `OSError` and `ValueError` and print the trouble; the tile and the card fall
        back to the row under the same three, and the page still renders."""
        self.assertEqual(self.db.execute("UPDATE bill SET cap_usd_month = 5.0 WHERE name = 'open'").rowcount, 1)
        self.registry_path.unlink()
        self.registry_path.mkdir()  # `read_text` raises `IsADirectoryError`, an `OSError`
        self.assertIsNone(usage.caps(self.db))
        self.assertEqual(usage.read(self.db, month="2026-09").bills[1].cap, 5.0)
        self.assertEqual(usage.bills(self.db)[1]["cap_usd_month"], 5.0)
        self.registry_path.rmdir()
        self.registry_path.write_text("bills: [not, a, mapping]\n", encoding="utf-8")  # `RegistryError`
        self.assertIsNone(usage.caps(self.db))
        self.assertEqual(usage.read(self.db, month="2026-09").bills[1].cap, 5.0)

    def test_a_registry_that_parses_with_a_bad_cap_or_a_list_section_leaves_the_row_as_the_cap(self):
        """#438's verification round. `registry.parse` does not type a cap,
        so `cap_usd_month: bad` parses and `float` of it is the boundary's
        to catch -- on a bill with no row, since a row's cap wins in the
        merge and a seeded bill never reaches the file's; and a section
        that is a list is `parse`'s own `RegistryError` now, not an
        `AttributeError` out of `.items()` -- the earlier fixture's
        `bills: [not, a, mapping]` alone never got there, `no 'providers'
        section` came first."""
        self.assertEqual(self.db.execute("UPDATE bill SET cap_usd_month = 5.0 WHERE name = 'open'").rowcount, 1)
        for text in (REGISTRY_WITH_EXTRA_BILL.replace("extra:  { cost: subscription }", "extra:  { cost: subscription, cap_usd_month: bad }"),
                     REGISTRY_WITH_EXTRA_BILL.replace("extra:  { cost: subscription }", "extra:  { cost: subscription, cap_usd_month: [10] }"),
                     # An integer cap too large for a float: `float` raises `OverflowError`,
                     # which is neither `ValueError` nor `TypeError` (sd:1219).
                     REGISTRY_WITH_EXTRA_BILL.replace("extra:  { cost: subscription }", "extra:  { cost: subscription, cap_usd_month: 1" + "0" * 400 + " }"),
                     "bills: [not, a, mapping]\nproviders: {}\nroles: {author: [], reviewer: []}\n"):
            with self.subTest(text=text[-60:]):
                self.assertNotEqual(text, REGISTRY_WITH_EXTRA_BILL)
                self.registry_path.write_text(text, encoding="utf-8")
                self.assertIsNone(usage.caps(self.db))
                self.assertEqual(usage.read(self.db, month="2026-09").bills[1].cap, 5.0)
                self.assertEqual(usage.bills(self.db)[1]["cap_usd_month"], 5.0)

    def test_the_registry_read_seeds_nothing(self):
        """`registry.read` writes a file entry's missing row on first open;
        `caps` must not, so the verb's "writes nothing but the sweep" holds.
        A bill the file names and the table lacks is in the mapping and
        stays out of the table."""
        self.registry_path.write_text(REGISTRY_WITH_EXTRA_BILL, encoding="utf-8")
        before = tuple(self.db.iterdump())
        self.assertEqual(usage.caps(self.db), {"capped": 10.0, "open": None, "extra": None})
        usage.read(self.db, month="2026-09")
        usage.bills(self.db)
        self.assertEqual(tuple(self.db.iterdump()), before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bill WHERE name = 'extra'").fetchone()[0], 0)

    def interleaved(self, *, on_select, write):
        """`self.db`, with `write` run on another connection before the
        `on_select`-th SELECT, the shape `test_the_projection_is_one_snapshot` uses."""
        other = connect(home=self.home)
        self.addCleanup(other.close)
        inner = self.db

        class Interleaved:
            selects = 0

            def execute(this, sql, parameters=()):
                if sql.lstrip().upper().startswith("SELECT"):
                    this.selects += 1
                    if this.selects == on_select:
                        write(other)
                return inner.execute(sql, parameters)

            def __getattr__(this, name):
                return getattr(inner, name)

        return Interleaved()

    @hub_only
    def test_the_caps_and_the_spend_are_one_snapshot(self):
        """#438's review: a cap committed by another connection between the
        registry's rows and the cost rows is in all of the read or in none
        of it, for the month and for the tile alike."""
        self.month()

        def raise_cap(other):
            other.execute("UPDATE bill SET cap_usd_month = 20.0 WHERE name = 'capped'")

        found = usage.read(self.interleaved(on_select=2, write=raise_cap), month="2026-09")
        self.assertEqual((found.bills[0].cap, found.bills[0].room), (10.0, 5.0))
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(usage.read(self.db, month="2026-09").bills[0].cap, 20.0)

        def lower_cap(other):
            other.execute("UPDATE bill SET cap_usd_month = 15.0 WHERE name = 'capped'")

        tile = usage.bills(self.interleaved(on_select=2, write=lower_cap))
        self.assertEqual(tile[0]["cap_usd_month"], 20.0)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(usage.bills(self.db)[0]["cap_usd_month"], 15.0)
