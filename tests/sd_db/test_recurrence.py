"""Tasks that recur: the RRULE subset, and the next occurrence at completion.

`docs/work/2026-09-19-tasks-that-recur/` is the plan. The engine is a stdlib
subset of RFC 5545 RRULE -- FREQ, INTERVAL, BYMONTH, BYMONTHDAY, and BYDAY on
WEEKLY (sd:3453) -- and every other part is refused by name at write time
(sd:1099, note 3728).
"""
import importlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, reads, today, upsert_repo
from sd_db.migrate import initialise, migrate
from sd_db.schema import SCHEMA_VERSION, migrations
from sd_db.workflow import (
    WorkflowError,
    capture_task,
    change_status,
    edit_item,
    item_state,
    next_occurrence_due,
)
from sd_db.writes import set_item_fields


def engine():
    """The engine module, imported per test so a missing one fails each test."""
    return importlib.import_module("sd_db.recurrence")


class TheRuleSubset(unittest.TestCase):
    def refused(self, text, *words):
        rec = engine()
        with self.assertRaises(rec.RecurrenceError) as caught:
            rec.parse(text)
        for word in words:
            self.assertIn(word, str(caught.exception))

    def test_a_rule_is_stored_in_one_canonical_spelling(self):
        rec = engine()
        self.assertEqual(rec.parse("rrule:bymonthday=15;freq=monthly;interval=1").text(),
                         "FREQ=MONTHLY;BYMONTHDAY=15")
        self.assertEqual(rec.parse("FREQ=YEARLY;BYMONTHDAY=30;BYMONTH=4;INTERVAL=2").text(),
                         "FREQ=YEARLY;INTERVAL=2;BYMONTH=4;BYMONTHDAY=30")
        self.assertEqual(rec.parse("FREQ=MONTHLY;BYMONTHDAY=15,-1").text(),
                         "FREQ=MONTHLY;BYMONTHDAY=-1,15")

    def test_every_part_outside_the_subset_is_refused_by_name(self):
        for part in ("COUNT=3", "UNTIL=20261231", "WKST=MO", "BYSETPOS=1",
                     "BYYEARDAY=100", "BYWEEKNO=1", "BYHOUR=9", "X-NAME=1"):
            name = part.split("=")[0]
            with self.subTest(part=part):
                self.refused(f"FREQ=WEEKLY;{part}", name)

    def test_byday_takes_plain_weekdays_on_weekly(self):
        # sd:3453: the vault's weekly rules name their weekday. The spelling
        # lists the days Monday first, as weeks start on Monday (WKST=MO).
        rec = engine()
        self.assertEqual(rec.parse("freq=weekly;byday=fr,mo,mo;interval=2").text(),
                         "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,FR")
        cases = {
            "FREQ=WEEKLY;BYDAY=1MO": "BYDAY",
            "FREQ=WEEKLY;BYDAY=-1FR": "BYDAY",
            "FREQ=WEEKLY;BYDAY=XX": "BYDAY",
            "FREQ=WEEKLY;BYDAY=MO,,TU": "BYDAY",
            "FREQ=DAILY;BYDAY=MO": "WEEKLY",
            "FREQ=MONTHLY;BYDAY=MO": "WEEKLY",
            "FREQ=YEARLY;BYMONTH=5;BYDAY=MO": "WEEKLY",
        }
        for text, word in cases.items():
            with self.subTest(text=text):
                self.refused(text, word)

    def test_a_sub_day_frequency_is_refused(self):
        for freq in ("HOURLY", "MINUTELY", "SECONDLY", "FORTNIGHTLY"):
            with self.subTest(freq=freq):
                self.refused(f"FREQ={freq}", f"FREQ={freq}")

    def test_malformed_rules_are_refused(self):
        cases = {
            "": "RRULE",
            "INTERVAL=2": "FREQ",
            "FREQ=DAILY;FREQ=WEEKLY": "FREQ",
            "FREQ=DAILY;INTERVAL=": "INTERVAL",
            "FREQ=DAILY;INTERVAL=0": "INTERVAL",
            "FREQ=DAILY;INTERVAL=-1": "INTERVAL",
            "FREQ=DAILY;INTERVAL=x": "INTERVAL",
            "FREQ=YEARLY;BYMONTH=13": "BYMONTH",
            "FREQ=YEARLY;BYMONTH=0": "BYMONTH",
            "FREQ=MONTHLY;BYMONTHDAY=0": "BYMONTHDAY",
            "FREQ=MONTHLY;BYMONTHDAY=32": "BYMONTHDAY",
            "FREQ=MONTHLY;BYMONTHDAY=-32": "BYMONTHDAY",
            "FREQ=MONTHLY;BYMONTHDAY=1,,2": "BYMONTHDAY",
            "FREQ=WEEKLY;BYMONTHDAY=1": "WEEKLY",
            "FREQ=DAILY;": "empty",
            "FREQ": "NAME=VALUE",
        }
        for text, word in cases.items():
            with self.subTest(text=text):
                self.refused(text, word)

    def test_a_rule_that_never_occurs_is_refused(self):
        rec = engine()
        with self.assertRaises(rec.RecurrenceError):
            rec.next_after(rec.parse("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30"), date(2026, 1, 1))


class TheNextOccurrence(unittest.TestCase):
    def after(self, text, start):
        rec = engine()
        return rec.next_after(rec.parse(text), date.fromisoformat(start)).isoformat()

    def test_daily_and_weekly_step_from_the_start(self):
        self.assertEqual(self.after("FREQ=DAILY;INTERVAL=10", "2026-03-05"), "2026-03-15")
        self.assertEqual(self.after("FREQ=WEEKLY", "2026-01-01"), "2026-01-08")
        self.assertEqual(self.after("FREQ=WEEKLY;INTERVAL=2", "2026-12-24"), "2027-01-07")

    def test_weekly_byday_lands_on_the_named_weekdays(self):
        # The two vault shapes, from a due date on the rule's weekday.
        self.assertEqual(self.after("FREQ=WEEKLY;BYDAY=MO", "2026-04-27"), "2026-05-04")
        self.assertEqual(self.after("FREQ=WEEKLY;INTERVAL=2;BYDAY=FR", "2026-09-11"), "2026-09-25")
        # A start off the weekday snaps to it in the start's own week.
        self.assertEqual(self.after("FREQ=WEEKLY;BYDAY=FR", "2026-09-15"), "2026-09-18")
        self.assertEqual(self.after("FREQ=WEEKLY;INTERVAL=2;BYDAY=FR", "2026-09-15"), "2026-09-18")
        # Several days expand each week; INTERVAL counts weeks from the start's.
        self.assertEqual(self.after("FREQ=WEEKLY;BYDAY=MO,TH", "2026-01-05"), "2026-01-08")
        self.assertEqual(self.after("FREQ=WEEKLY;BYDAY=MO,TH", "2026-01-08"), "2026-01-12")
        self.assertEqual(self.after("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH", "2026-01-08"), "2026-01-19")
        # BYMONTH limits the weeks' days.
        self.assertEqual(self.after("FREQ=WEEKLY;BYMONTH=3;BYDAY=MO", "2026-01-05"), "2026-03-02")
        rec = engine()
        rule = rec.parse("FREQ=WEEKLY;INTERVAL=2;BYDAY=FR")
        self.assertEqual(rec.next_after(rule, date(2026, 9, 11), date(2026, 10, 1)), date(2026, 10, 9))

    def test_month_end_skips_a_month_without_the_day(self):
        # Skip, not clamp: RFC 5545 drops an invalid date. February and April
        # have no 31st, so the rule moves on to the next month that has one.
        self.assertEqual(self.after("FREQ=MONTHLY;BYMONTHDAY=31", "2026-01-31"), "2026-03-31")
        self.assertEqual(self.after("FREQ=MONTHLY;BYMONTHDAY=31", "2026-03-31"), "2026-05-31")
        self.assertEqual(self.after("FREQ=MONTHLY;BYMONTHDAY=31", "2026-04-10"), "2026-05-31")
        # Without BYMONTHDAY the day comes from the start, and skips the same way.
        self.assertEqual(self.after("FREQ=MONTHLY", "2026-01-31"), "2026-03-31")
        self.assertEqual(self.after("FREQ=MONTHLY;BYMONTHDAY=30", "2026-01-30"), "2026-03-30")

    def test_minus_one_is_the_last_day_of_every_month(self):
        self.assertEqual(self.after("FREQ=MONTHLY;BYMONTHDAY=-1", "2026-01-31"), "2026-02-28")
        self.assertEqual(self.after("FREQ=MONTHLY;BYMONTHDAY=-1", "2028-01-31"), "2028-02-29")
        self.assertEqual(self.after("FREQ=MONTHLY;BYMONTHDAY=-1", "2026-02-28"), "2026-03-31")

    def test_monthly_expands_bymonthday_and_limits_by_bymonth(self):
        self.assertEqual(self.after("FREQ=MONTHLY;BYMONTHDAY=1,15", "2026-01-01"), "2026-01-15")
        self.assertEqual(self.after("FREQ=MONTHLY;BYMONTHDAY=1,15", "2026-01-15"), "2026-02-01")
        self.assertEqual(self.after("FREQ=MONTHLY;BYMONTH=1,7;BYMONTHDAY=15", "2026-01-15"), "2026-07-15")
        self.assertEqual(self.after("FREQ=MONTHLY;INTERVAL=3", "2026-01-15"), "2026-04-15")

    def test_yearly(self):
        self.assertEqual(self.after("FREQ=YEARLY", "2026-04-30"), "2027-04-30")
        self.assertEqual(self.after("FREQ=YEARLY;BYMONTH=4;BYMONTHDAY=30", "2026-01-10"), "2026-04-30")
        self.assertEqual(self.after("FREQ=YEARLY;INTERVAL=3", "2026-06-01"), "2029-06-01")
        self.assertEqual(self.after("FREQ=YEARLY", "2028-02-29"), "2032-02-29")
        # BYMONTHDAY without BYMONTH expands over every month, as RFC 5545 does.
        self.assertEqual(self.after("FREQ=YEARLY;BYMONTHDAY=15", "2026-01-20"), "2026-02-15")

    def test_a_later_threshold_moves_the_search_and_not_the_phase(self):
        rec = engine()
        rule = rec.parse("FREQ=DAILY;INTERVAL=10")
        self.assertEqual(rec.next_after(rule, date(2026, 2, 20), date(2026, 3, 5)), date(2026, 3, 12))
        # A threshold before the start is the start.
        self.assertEqual(rec.next_after(rule, date(2026, 2, 20), date(2026, 1, 1)), date(2026, 3, 2))
        rule = rec.parse("FREQ=YEARLY;INTERVAL=4;BYMONTH=2;BYMONTHDAY=29")
        self.assertEqual(rec.next_after(rule, date(2028, 2, 29), date(2031, 6, 1)), date(2032, 2, 29))
        rule = rec.parse("FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY=31")
        self.assertEqual(rec.next_after(rule, date(2026, 1, 31), date(2026, 5, 1)), date(2026, 7, 31))

    def test_daily_limits_by_month_and_day(self):
        self.assertEqual(self.after("FREQ=DAILY;BYMONTHDAY=1", "2026-01-15"), "2026-02-01")
        self.assertEqual(self.after("FREQ=DAILY;BYMONTH=3", "2026-01-15"), "2026-03-01")


class TheSchema(unittest.TestCase):
    def test_migration_012_adds_both_columns(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 12)
        self.assertEqual(dict(migrations())[12].name, "012_recurrence.sql")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sd.db"
            initialise(path)
            raw = sqlite3.connect(path)
            try:
                columns = {row[1] for row in raw.execute("PRAGMA table_info(item)")}
            finally:
                raw.close()
        self.assertLessEqual({"recurrence", "recurrence_anchor"}, columns)

    def test_migration_041_adds_the_lead_and_a_series_from_before_reads_null(self):
        # pack sd:5648. Built to 40 by hand with a recurring row on it; the
        # reads run on the 40 store first, as a reader does before `migrate`.
        self.assertGreaterEqual(SCHEMA_VERSION, 41)
        self.assertEqual(dict(migrations())[41].name, "041_recurrence_lead.sql")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sd.db"
            through = [found for found in migrations() if found[0] <= 40]
            with patch("sd_db.migrate.migrations", return_value=through):
                migrate(path)
            raw = sqlite3.connect(path)
            try:
                raw.execute("INSERT INTO item (id, kind, title, status, due, recurrence, recurrence_anchor, "
                            "created_at, updated_at) VALUES (7, 'task', 'Water', 'planning', '2026-01-10', "
                            "'FREQ=WEEKLY', 'schedule', 't', 't')")
                raw.commit()
            finally:
                raw.close()
            old = connect(path, write=False)
            try:
                rows = {row["id"]: row for row in reads.backlog_items(old, now="2026-01-09T12:00:00Z")}
                self.assertEqual((rows[7]["opens_on"], rows[7]["scheduled"]), ("2026-01-10", 1))
            finally:
                old.close()
            result = migrate(path)
            self.assertEqual((result.before, result.applied), (40, list(range(41, SCHEMA_VERSION + 1))))
            raw = sqlite3.connect(path)
            try:
                columns = {row[1]: row[2] for row in raw.execute("PRAGMA table_info(item)")}
                kept = raw.execute("SELECT recurrence, recurrence_lead FROM item WHERE id = 7").fetchone()
            finally:
                raw.close()
        self.assertEqual(columns["recurrence_lead"], "INTEGER")
        self.assertEqual(kept, ("FREQ=WEEKLY", None))


class RecurringTasks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)

    def capture(self, **values):
        values.setdefault("due", "2026-01-01")
        return capture_task(self.db, title="File the periodic report", who="operator", **values)["item"]

    def complete(self, item, on):
        with patch("sd_db.workflow._today", return_value=date.fromisoformat(on)):
            return change_status(self.db, item, "done", who="operator")

    def items(self):
        return self.db.execute("SELECT COUNT(*) FROM item").fetchone()[0]

    def row(self, item):
        return item_state(self.db, item)["item"]

    def test_the_late_completion_separates_the_anchors(self):
        schedule = self.capture(recurrence="FREQ=WEEKLY")
        completion = self.capture(recurrence="FREQ=WEEKLY", recurrence_anchor="completion")
        self.assertEqual(schedule["recurrence_anchor"], "schedule")
        on_schedule = self.complete(schedule["id"], "2026-01-22")["next_occurrence"]
        on_completion = self.complete(completion["id"], "2026-01-22")["next_occurrence"]
        self.assertEqual(self.row(on_schedule)["due"], "2026-01-08")
        self.assertEqual(self.row(on_completion)["due"], "2026-01-29")

    def test_the_next_occurrence_keeps_the_area_and_drops_other_fields(self):
        # pack sd:8010: a recurring Home or HOA task stays in its area; the
        # producer-owned fields of one instance still stay behind.
        for area in ("home", "hoa"):
            with self.subTest(area=area):
                item = self.capture(recurrence="FREQ=MONTHLY")
                set_item_fields(self.db, item["id"], fields={"area": area, "score": 3})
                spawned = self.complete(item["id"], "2026-01-01")["next_occurrence"]
                self.assertEqual(json.loads(self.row(spawned)["fields"] or "{}"), {"area": area})
        plain = self.capture(recurrence="FREQ=MONTHLY")
        set_item_fields(self.db, plain["id"], fields={"score": 3})
        spawned = self.complete(plain["id"], "2026-01-01")["next_occurrence"]
        self.assertIsNone(self.row(spawned)["fields"])

    def test_the_next_occurrence_keeps_the_home_asset_it_tends(self):
        # pack sd:8053: a plant's care task names its asset in fields.asset;
        # the next occurrence tends the same asset, so the map's overdue mark
        # follows the series.
        item = self.capture(recurrence="FREQ=WEEKLY")
        set_item_fields(self.db, item["id"], fields={"area": "home", "asset": 7, "score": 3})
        spawned = self.complete(item["id"], "2026-01-01")["next_occurrence"]
        self.assertEqual(json.loads(self.row(spawned)["fields"] or "{}"), {"area": "home", "asset": 7})

    def test_a_completion_anchor_restarts_the_series_at_the_completion(self):
        # The PRD's example: `every 10 days`, completed 2026-03-05, next falls
        # due 2026-03-15 whatever the due date was. Drifts by design.
        item = self.capture(due="2026-02-20", recurrence="FREQ=DAILY;INTERVAL=10",
                            recurrence_anchor="completion")
        spawned = self.complete(item["id"], "2026-03-05")["next_occurrence"]
        self.assertEqual(self.row(spawned)["due"], "2026-03-15")

    def test_the_cistern_shape_is_accepted_and_counts_from_the_completion(self):
        item = self.capture(due="2026-05-01", recurrence="FREQ=YEARLY;INTERVAL=3",
                            recurrence_anchor="completion")
        spawned = self.complete(item["id"], "2027-02-01")["next_occurrence"]
        self.assertEqual(self.row(spawned)["due"], "2030-02-01")

    def test_an_early_completion_moves_past_the_current_due(self):
        # Done a week early, the next one is due a week after the closed
        # row's due date, never on it again (pack sd:6230).
        item = self.capture(due="2026-09-30", recurrence="FREQ=WEEKLY", recurrence_anchor="completion")
        spawned = self.complete(item["id"], "2026-09-23")["next_occurrence"]
        self.assertEqual(self.row(spawned)["due"], "2026-10-07")

    MONDAY = "FREQ=WEEKLY;BYDAY=MO"

    def test_a_weekly_monday_done_the_sunday_before_is_due_the_next_monday(self):
        item = self.capture(due="2026-10-26", recurrence=self.MONDAY, recurrence_anchor="completion")
        self.assertEqual(next_occurrence_due(self.row(item["id"]), today=date(2026, 10, 25)), "2026-11-02")
        spawned = self.complete(item["id"], "2026-10-25")["next_occurrence"]
        self.assertEqual(self.row(spawned)["due"], "2026-11-02")

    def test_a_weekly_monday_done_late_is_due_the_monday_after_the_completion(self):
        item = self.capture(due="2026-10-26", recurrence=self.MONDAY, recurrence_anchor="completion")
        self.assertEqual(next_occurrence_due(self.row(item["id"]), today=date(2026, 10, 28)), "2026-11-02")
        spawned = self.complete(item["id"], "2026-10-28")["next_occurrence"]
        self.assertEqual(self.row(spawned)["due"], "2026-11-02")

    def test_a_weekly_monday_done_on_the_due_day_is_due_a_week_later(self):
        item = self.capture(due="2026-10-26", recurrence=self.MONDAY, recurrence_anchor="completion")
        self.assertEqual(next_occurrence_due(self.row(item["id"]), today=date(2026, 10, 26)), "2026-11-02")
        spawned = self.complete(item["id"], "2026-10-26")["next_occurrence"]
        self.assertEqual(self.row(spawned)["due"], "2026-11-02")

    # PR #542 reviews: restarted at the completion date, these rules can have
    # no occurrence at all, so they are refused when written. The first two
    # lose their phase to INTERVAL; the rest take a day or a month from the
    # completion date that the rule's own parts rule out. Each due date is
    # one the rule accepts, so the schedule anchor keeps them.
    BARREN = (("FREQ=YEARLY;INTERVAL=4;BYMONTH=2;BYMONTHDAY=29", "2028-02-29"),
              ("FREQ=MONTHLY;INTERVAL=2;BYMONTH=1", "2027-01-15"),
              ("FREQ=YEARLY;BYMONTH=2", "2027-02-10"),
              ("FREQ=MONTHLY;BYMONTH=2", "2027-02-10"),
              ("FREQ=YEARLY;BYMONTH=4", "2027-04-10"),
              ("FREQ=MONTHLY;BYMONTH=4,6,9,11", "2027-04-10"))

    def test_a_barren_rule_cannot_be_completion_anchored_at_add(self):
        before = self.items()
        for rule, due in self.BARREN:
            with self.subTest(rule=rule):
                with self.assertRaisesRegex(
                        WorkflowError, r"cannot be completion-anchored: completed on a date like "
                                       r"20\d\d-\d\d-\d\d .*recurrence_anchor schedule"):
                    self.capture(due=due, recurrence=rule, recurrence_anchor="completion")
                # The schedule anchor is unaffected.
                self.assertEqual(self.capture(due=due, recurrence=rule)["recurrence_anchor"], "schedule")
        self.assertEqual(self.items(), before + len(self.BARREN))

    def test_a_barren_rule_cannot_be_completion_anchored_at_edit(self):
        for rule, due in self.BARREN:
            with self.subTest(rule=rule):
                item = self.capture(due=due, recurrence=rule)
                with self.assertRaisesRegex(WorkflowError, "cannot be completion-anchored"):
                    edit_item(self.db, item["id"], {"recurrence_anchor": "completion"}, who="operator")
                plain = self.capture(due=due, recurrence="FREQ=YEARLY",
                                     recurrence_anchor="completion")
                with self.assertRaisesRegex(WorkflowError, "cannot be completion-anchored"):
                    edit_item(self.db, plain["id"], {"recurrence": rule}, who="operator")
                self.assertEqual(self.row(item["id"])["recurrence_anchor"], "schedule")
                self.assertEqual(self.row(plain["id"])["recurrence"], "FREQ=YEARLY")

    def test_a_repro_completion_really_had_no_next_occurrence(self):
        # The review's repro dates, straight at the engine: what the refusal
        # above now keeps from being written.
        rules = engine()
        for text, done in (("FREQ=YEARLY;BYMONTH=2", "2026-09-30"),
                           ("FREQ=MONTHLY;BYMONTH=2", "2026-10-30"),
                           ("FREQ=YEARLY;BYMONTH=4", "2026-10-31"),
                           ("FREQ=MONTHLY;BYMONTH=4,6,9,11", "2026-10-31")):
            with self.subTest(rule=text):
                with self.assertRaises(rules.RecurrenceError):
                    rules.next_after(rules.parse(text), date.fromisoformat(done))

    def test_sound_rules_are_accepted_on_the_completion_anchor(self):
        # YEARLY with BYMONTHDAY and no BYMONTH expands over every month, so
        # a 31st always exists; MONTHLY;INTERVAL=2;BYMONTHDAY=1 was refused
        # by the INTERVAL rule the sweep replaced.
        for rule, due, done, following in (
                ("FREQ=YEARLY;INTERVAL=3", "2026-05-01", "2027-02-01", "2030-02-01"),
                ("FREQ=MONTHLY;BYMONTHDAY=-1", "2026-09-30", "2026-10-05", "2026-10-31"),
                ("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", "2028-02-29", "2028-03-01", "2032-02-29"),
                ("FREQ=WEEKLY", "2026-09-30", "2026-10-31", "2026-11-07"),
                ("FREQ=YEARLY;BYMONTHDAY=31", "2026-10-31", "2027-04-15", "2027-05-31"),
                ("FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=1", "2026-10-01", "2026-10-31", "2026-12-01")):
            with self.subTest(rule=rule):
                item = self.capture(due=due, recurrence=rule, recurrence_anchor="completion")
                spawned = self.complete(item["id"], done)["next_occurrence"]
                self.assertEqual(self.row(spawned)["due"], following)

    def ended(self, item, on):
        """Complete `item` on `on`, assert the recurrence ended, return the reason."""
        before = self.items()
        result = self.complete(item, on)
        self.assertEqual(result["item"]["status"], "done")
        self.assertIsNone(result["next_occurrence"])
        reason = result["next_occurrence_reason"]
        self.assertIsNotNone(reason)
        self.assertEqual(self.items(), before)
        row = self.row(item)
        self.assertEqual((row["recurrence"], row["recurrence_anchor"]), (None, None))
        notes = [n["body"] for n in item_state(self.db, item)["notes"]]
        self.assertIn(f"Recurrence ended: {reason}. No next occurrence was created; completed by operator",
                      notes)
        return reason

    # Final review of PR #542: 2100 is not a leap year, and the 100-year
    # horizon crosses it, so these pass the one-leap-cycle sweep and still
    # find no occurrence from the completion date given.
    PAST_2100 = (("FREQ=YEARLY;INTERVAL=17;BYMONTH=2;BYMONTHDAY=29", "2032-03-01"),
                 ("FREQ=YEARLY;INTERVAL=13;BYMONTH=2;BYMONTHDAY=29", "2048-02-29"),
                 ("FREQ=YEARLY;INTERVAL=15;BYMONTH=2;BYMONTHDAY=29", "2040-02-29"),
                 ("FREQ=YEARLY;INTERVAL=19;BYMONTH=2;BYMONTHDAY=29", "2043-01-01"),
                 ("FREQ=YEARLY;INTERVAL=21;BYMONTH=2;BYMONTHDAY=29", "2037-01-01"),
                 ("FREQ=YEARLY;INTERVAL=25;BYMONTH=2;BYMONTHDAY=29", "2050-01-01"),
                 ("FREQ=DAILY;INTERVAL=17;BYMONTH=2;BYMONTHDAY=29", "2034-03-01"),
                 ("FREQ=YEARLY;INTERVAL=17", "2032-02-29"))

    def test_a_rule_past_2100_ends_the_recurrence_and_still_completes(self):
        rules = engine()
        # The sweep accepts the first one, so it is written the ordinary way.
        accepted = self.capture(due="2028-02-29", recurrence=self.PAST_2100[0][0],
                                recurrence_anchor="completion")
        self.assertIsNone(rules.first_barren_start(rules.parse(self.PAST_2100[0][0])))
        reason = self.ended(accepted["id"], self.PAST_2100[0][1])
        self.assertIn("has no occurrence after 2032-03-01 within 100 years", reason)
        self.assertIn("completion anchor, searched from 2032-03-01", reason)
        for rule, done in self.PAST_2100[1:]:
            with self.subTest(rule=rule):
                self.assertIsNone(rules.first_barren_start(rules.parse(rule)))
                item = self.capture(recurrence="FREQ=WEEKLY")
                self.db.execute("UPDATE item SET recurrence = ?, recurrence_anchor = 'completion' WHERE id = ?",
                                (rule, item["id"]))
                reason = self.ended(item["id"], done)
                self.assertIn(f"recurrence {rule} has no occurrence after {done} within 100 years", reason)

    def test_a_row_written_around_the_checks_ends_its_recurrence(self):
        # Raw SQL can write what `_recurring` refuses; completing it still
        # lands, and the note says why nothing followed.
        cases = (("UPDATE item SET due = NULL WHERE id = ?", "no due date"),
                 ("UPDATE item SET recurrence = 'FREQ=HOURLY' WHERE id = ?", "FREQ=HOURLY is not supported"),
                 ("UPDATE item SET recurrence_anchor = 'whenever' WHERE id = ?", "'whenever' is not one of"))
        for statement, why in cases:
            with self.subTest(why=why):
                item = self.capture(recurrence="FREQ=WEEKLY")
                self.db.execute(statement, (item["id"],))
                self.assertIn(why, self.ended(item["id"], "2026-09-23"))

    def test_an_ended_recurrence_is_one_transaction_with_the_completion(self):
        import sd_db.workflow as workflow

        item = self.capture(recurrence="FREQ=WEEKLY")
        self.db.execute("UPDATE item SET recurrence = ?, recurrence_anchor = 'completion' WHERE id = ?",
                        (self.PAST_2100[0][0], item["id"]))
        notes = self.db.execute("SELECT COUNT(*) FROM note").fetchone()[0]
        real = workflow.add_note

        def failing(connection, target, kind, body, **options):
            if body.startswith("Recurrence ended"):
                raise RuntimeError("the disk filled")
            return real(connection, target, kind, body, **options)

        with patch("sd_db.workflow.add_note", side_effect=failing):
            with self.assertRaisesRegex(RuntimeError, "disk filled"):
                self.complete(item["id"], "2032-03-01")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM note").fetchone()[0], notes)
        row = self.row(item["id"])
        self.assertEqual((row["status"], row["recurrence"], row["recurrence_anchor"]),
                         ("planning", self.PAST_2100[0][0], "completion"))

    def test_cancel_and_other_moves_keep_the_rule_and_create_nothing(self):
        # `progress.cancel_work` is the only cancel verb. Its default guard
        # refuses every kind but `work`, which cannot recur, and `task_guard`
        # (sd:1005) refuses a recurring task, since a cancel spawns nothing.
        # Moves to any status but `done` keep the rule where it is.
        from sd_db import progress

        item = self.capture(recurrence="FREQ=WEEKLY")
        before = self.items()
        with self.assertRaisesRegex(WorkflowError, "work items"):
            progress.cancel_work(self.db, item["id"], reason="not needed", who="operator")
        with self.assertRaisesRegex(WorkflowError, "recurring"):
            progress.cancel_work(self.db, item["id"], reason="not needed", who="operator",
                                 guard=progress.task_guard)
        for status in ("in_progress", "blocked", "ready", "planning"):
            with self.subTest(status=status):
                self.assertNotIn("next_occurrence",
                                 change_status(self.db, item["id"], status, who="operator"))
        self.assertEqual(self.items(), before)
        row = self.row(item["id"])
        self.assertEqual((row["recurrence"], row["recurrence_anchor"]), ("FREQ=WEEKLY", "schedule"))

    def test_a_failing_completion_rolls_back_the_spawn(self):
        import sd_db.workflow as workflow

        item = self.capture(recurrence="FREQ=WEEKLY")
        before = self.items()
        notes = self.db.execute("SELECT COUNT(*) FROM note").fetchone()[0]
        real = workflow.add_note

        def failing(connection, target, kind, body, **options):
            if body.startswith("Recurs from"):
                raise RuntimeError("the disk filled")
            return real(connection, target, kind, body, **options)

        with patch("sd_db.workflow.add_note", side_effect=failing):
            with self.assertRaisesRegex(RuntimeError, "disk filled"):
                self.complete(item["id"], "2026-01-01")
        self.assertEqual(self.items(), before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM note").fetchone()[0], notes)
        row = self.row(item["id"])
        self.assertEqual((row["status"], row["recurrence"], row["recurrence_anchor"]),
                         ("planning", "FREQ=WEEKLY", "schedule"))

    def test_two_connections_completing_at_once_create_one_next_row(self):
        import sd_db.workflow as workflow

        item = self.capture(recurrence="FREQ=WEEKLY")
        before = self.items()
        results, errors = {}, []
        real = workflow._next_occurrence
        racing_started = threading.Event()

        def second():
            other = connect(self.path)
            try:
                racing_started.set()
                results["second"] = change_status(other, item["id"], "done", who="other")
            except BaseException as error:  # reported below, not swallowed
                errors.append(error)
            finally:
                other.close()

        thread = threading.Thread(target=second)

        def racing(connection, row, *, who):
            # The first completion holds its write transaction here, between
            # the status write and the spawn, while the second one starts.
            thread.start()
            racing_started.wait(5)
            time.sleep(0.5)
            return real(connection, row, who=who)

        with patch("sd_db.workflow._next_occurrence", side_effect=racing):
            results["first"] = change_status(self.db, item["id"], "done", who="operator")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.items(), before + 1)
        self.assertIn("next_occurrence", results["first"])
        self.assertNotIn("next_occurrence", results["second"])
        self.assertEqual(results["second"]["item"]["status"], "done")

    def test_a_weekly_byday_rule_is_stored_and_walked(self):
        item = self.capture(due="2026-09-11", recurrence="freq=weekly;interval=2;byday=fr")
        self.assertEqual(item["recurrence"], "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR")
        spawned = self.complete(item["id"], "2026-09-11")["next_occurrence"]
        self.assertEqual(self.row(spawned)["due"], "2026-09-25")
        # Completion-anchored, a Wednesday completion moves to the next Monday.
        item = self.capture(due="2026-09-21", recurrence="FREQ=WEEKLY;BYDAY=MO",
                            recurrence_anchor="completion")
        spawned = self.complete(item["id"], "2026-09-23")["next_occurrence"]
        self.assertEqual(self.row(spawned)["due"], "2026-09-28")

    def test_month_end_skips_through_a_completion(self):
        item = self.capture(due="2026-01-31", recurrence="FREQ=MONTHLY;BYMONTHDAY=31")
        spawned = self.complete(item["id"], "2026-01-31")["next_occurrence"]
        self.assertEqual(self.row(spawned)["due"], "2026-03-31")

    def test_the_rule_moves_to_the_new_row(self):
        upsert_repo(self.db, "/repos/assoc")
        item = self.capture(recurrence="FREQ=YEARLY", recurrence_anchor="schedule",
                            priority=2, repo="/repos/assoc", body="Secretary of State")
        result = self.complete(item["id"], "2026-01-02")
        spawned = self.row(result["next_occurrence"])
        parent = result["item"]
        self.assertEqual(parent["status"], "done")
        self.assertIsNone(parent["recurrence"])
        self.assertIsNone(parent["recurrence_anchor"])
        self.assertEqual(
            {key: spawned[key] for key in ("kind", "title", "status", "priority", "repo", "due",
                                           "recurrence", "recurrence_anchor")},
            {"kind": "task", "title": "File the periodic report", "status": "planning",
             "priority": 2, "repo": "/repos/assoc", "due": "2027-01-01",
             "recurrence": "FREQ=YEARLY", "recurrence_anchor": "schedule"})
        self.assertEqual(json.loads(spawned["body"]), {"text": "Secretary of State"})
        self.assertTrue(any(f"item {spawned['id']}" in note["body"] for note in result["notes"]))
        notes = item_state(self.db, spawned["id"])["notes"]
        self.assertTrue(any(f"item {item['id']}" in note["body"] for note in notes))

    def test_completing_twice_creates_one_occurrence(self):
        item = self.capture(recurrence="FREQ=WEEKLY")
        before = self.items()
        first = self.complete(item["id"], "2026-01-01")
        second = self.complete(item["id"], "2026-01-01")
        self.assertEqual(self.items(), before + 1)
        self.assertIn("next_occurrence", first)
        self.assertNotIn("next_occurrence", second)

    def test_reopening_and_completing_again_creates_nothing_more(self):
        item = self.capture(recurrence="FREQ=WEEKLY")
        before = self.items()
        self.complete(item["id"], "2026-01-01")
        change_status(self.db, item["id"], "ready", who="operator")
        again = self.complete(item["id"], "2026-01-02")
        self.assertEqual(self.items(), before + 1)
        self.assertNotIn("next_occurrence", again)

    def test_a_plain_completion_returns_the_shape_it_always_did(self):
        item = self.capture()
        result = self.complete(item["id"], "2026-01-01")
        self.assertNotIn("next_occurrence", result)
        self.assertEqual(set(result), {"item", "notes", "revision"})

    def test_a_dateless_rule_is_refused(self):
        before = self.items()
        with self.assertRaisesRegex(WorkflowError, "due date"):
            self.capture(due=None, recurrence="FREQ=WEEKLY")
        self.assertEqual(self.items(), before)
        item = self.capture(recurrence="FREQ=WEEKLY")
        with self.assertRaisesRegex(WorkflowError, "due date"):
            edit_item(self.db, item["id"], {"due": None}, who="operator")
        plain = self.capture(due=None)
        with self.assertRaisesRegex(WorkflowError, "due date"):
            edit_item(self.db, plain["id"], {"recurrence": "FREQ=DAILY"}, who="operator")

    def test_a_part_outside_the_subset_is_refused_at_write_time(self):
        before = self.items()
        for rule, name in (("FREQ=WEEKLY;BYSETPOS=1", "BYSETPOS"), ("FREQ=DAILY;COUNT=3", "COUNT")):
            with self.subTest(rule=rule):
                with self.assertRaisesRegex(WorkflowError, name):
                    self.capture(recurrence=rule)
        self.assertEqual(self.items(), before)
        item = self.capture()
        with self.assertRaisesRegex(WorkflowError, "UNTIL"):
            edit_item(self.db, item["id"], {"recurrence": "FREQ=YEARLY;UNTIL=20301231"}, who="operator")
        self.assertIsNone(self.row(item["id"])["recurrence"])

    def test_an_anchor_needs_a_rule_and_a_known_value(self):
        with self.assertRaisesRegex(WorkflowError, "recurrence_anchor"):
            self.capture(recurrence_anchor="completion")
        with self.assertRaisesRegex(WorkflowError, "recurrence_anchor"):
            self.capture(recurrence="FREQ=DAILY", recurrence_anchor="whenever")

    def test_edit_sets_canonicalises_and_clears(self):
        item = self.capture()
        edited = edit_item(self.db, item["id"], {"recurrence": "freq=monthly;bymonthday=-1"},
                           who="operator")["item"]
        self.assertEqual((edited["recurrence"], edited["recurrence_anchor"]),
                         ("FREQ=MONTHLY;BYMONTHDAY=-1", "schedule"))
        edited = edit_item(self.db, item["id"], {"recurrence_anchor": "completion"}, who="operator")["item"]
        self.assertEqual(edited["recurrence_anchor"], "completion")
        cleared = edit_item(self.db, item["id"], {"recurrence": None}, who="operator")["item"]
        self.assertEqual((cleared["recurrence"], cleared["recurrence_anchor"]), (None, None))
        # With the rule gone, the due date may go too.
        edit_item(self.db, item["id"], {"due": None}, who="operator")

    # pack sd:5648: `recurrence_lead`, the days before `due` an occurrence shows.

    def test_a_lead_is_a_whole_number_of_days_in_range(self):
        # Extreme magnitudes and shapes: each is refused by name, before a write.
        before = self.items()
        for lead in (-1, -(2 ** 63), 3661, 2 ** 63, 2 ** 64, 1.0, 1.5, float("inf"), float("-inf"),
                     float("nan"), 5e-324, True, False, "3", "", [3], {"days": 3}):
            with self.subTest(lead=lead):
                with self.assertRaisesRegex(WorkflowError, "recurrence_lead must be a whole number of days"):
                    self.capture(recurrence="FREQ=WEEKLY", recurrence_lead=lead)
        self.assertEqual(self.items(), before)
        for lead in (0, 1, 3660):
            with self.subTest(lead=lead):
                self.assertEqual(self.capture(recurrence="FREQ=WEEKLY", recurrence_lead=lead)["recurrence_lead"],
                                 lead)
        item = self.capture(recurrence="FREQ=WEEKLY", recurrence_lead=2)
        with self.assertRaisesRegex(WorkflowError, "recurrence_lead must be"):
            edit_item(self.db, item["id"], {"recurrence_lead": -3}, who="operator")
        self.assertEqual(self.row(item["id"])["recurrence_lead"], 2)

    def test_a_lead_needs_a_rule(self):
        before = self.items()
        for lead in (0, 3):
            with self.subTest(lead=lead):
                with self.assertRaisesRegex(WorkflowError, "recurrence_lead needs a recurrence rule"):
                    self.capture(recurrence_lead=lead)
        self.assertEqual(self.items(), before)
        plain = self.capture()
        with self.assertRaisesRegex(WorkflowError, "recurrence_lead needs a recurrence rule"):
            edit_item(self.db, plain["id"], {"recurrence_lead": 3}, who="operator")
        series = self.capture(recurrence="FREQ=WEEKLY")
        with self.assertRaisesRegex(WorkflowError, "recurrence_lead needs a recurrence rule"):
            edit_item(self.db, series["id"], {"recurrence": None, "recurrence_lead": 3}, who="operator")
        self.assertEqual(self.row(series["id"])["recurrence"], "FREQ=WEEKLY")

    def test_the_edit_route_sets_the_lead_and_folds_its_note(self):
        from sd_db.workflow import is_audit_note

        item = self.capture(recurrence="FREQ=MONTHLY")
        state = edit_item(self.db, item["id"], {"recurrence_lead": 5}, who="operator")
        self.assertEqual(state["item"]["recurrence_lead"], 5)
        self.assertEqual(state["notes"][-1]["body"], "Updated recurrence_lead by operator")
        self.assertTrue(is_audit_note(state["notes"][-1]))

    def test_clearing_the_rule_clears_the_lead(self):
        item = self.capture(recurrence="FREQ=WEEKLY", recurrence_lead=4)
        cleared = edit_item(self.db, item["id"], {"recurrence": None}, who="operator")["item"]
        self.assertEqual((cleared["recurrence"], cleared["recurrence_anchor"], cleared["recurrence_lead"]),
                         (None, None, None))

    def test_the_lead_moves_to_the_next_row(self):
        item = self.capture(recurrence="FREQ=WEEKLY", recurrence_lead=3)
        result = self.complete(item["id"], "2026-01-01")
        self.assertIsNone(result["item"]["recurrence_lead"])
        spawned = self.row(result["next_occurrence"])
        self.assertEqual((spawned["due"], spawned["recurrence_lead"]), ("2026-01-08", 3))
        # And on again: the lead follows the series, not one row.
        again = self.row(self.complete(spawned["id"], "2026-01-08")["next_occurrence"])
        self.assertEqual((again["due"], again["recurrence_lead"]), ("2026-01-15", 3))

    def test_an_ended_recurrence_clears_the_lead(self):
        item = self.capture(recurrence="FREQ=WEEKLY", recurrence_lead=3)
        self.db.execute("UPDATE item SET due = NULL WHERE id = ?", (item["id"],))
        self.ended(item["id"], "2026-01-01")
        self.assertIsNone(self.row(item["id"])["recurrence_lead"])

    def test_a_failing_completion_keeps_the_lead_on_the_open_row(self):
        # Steps: the spawn, the clear and the notes are one transaction. A
        # failure after the spawn and the clear leaves the row as it was.
        import sd_db.workflow as workflow

        item = self.capture(recurrence="FREQ=WEEKLY", recurrence_lead=3)
        before = self.items()
        real = workflow.add_note

        def failing(connection, target, kind, body, **options):
            if body.startswith("Recurs from"):
                raise RuntimeError("the disk filled")
            return real(connection, target, kind, body, **options)

        with patch("sd_db.workflow.add_note", side_effect=failing):
            with self.assertRaisesRegex(RuntimeError, "disk filled"):
                self.complete(item["id"], "2026-01-01")
        self.assertEqual(self.items(), before)
        row = self.row(item["id"])
        self.assertEqual((row["status"], row["recurrence"], row["recurrence_lead"]),
                         ("planning", "FREQ=WEEKLY", 3))
        # The next completion finishes what the failed one started.
        spawned = self.row(self.complete(item["id"], "2026-01-01")["next_occurrence"])
        self.assertEqual(spawned["recurrence_lead"], 3)

    def test_a_kind_the_task_controls_cannot_complete_cannot_recur(self):
        item = self.capture(recurrence="FREQ=WEEKLY")
        with self.assertRaisesRegex(WorkflowError, "cannot recur"):
            edit_item(self.db, item["id"], {"kind": "work-idea"}, who="operator")
        self.assertEqual(self.row(item["id"])["kind"], "task")


class ScheduledOccurrences(unittest.TestCase):
    """pack sd:5648: an open occurrence is scheduled until `due - recurrence_lead`.

    The boundary is the open day itself: on `due - lead` the row is open.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)

    def capture(self, title, **values):
        return capture_task(self.db, title=title, who="operator", **values)["item"]["id"]

    def views(self, day):
        now = f"{day}T12:00:00Z"
        backlog = {row["id"]: (row["opens_on"], row["scheduled"]) for row in reads.backlog_items(self.db, now=now)}
        planned = {row["id"] for row in today.planned(self.db, now=now)}
        work = {row["id"] for row in reads.today_items(self.db, now=now)}
        work_ahead = {row["id"] for row in reads.today_items(self.db, now=now, horizon_days=30)}
        summary = today.sections(self.db, now=now)["summary"]
        return backlog, planned, work, work_ahead, summary

    def test_the_lead_moves_the_open_day_and_the_open_day_is_open(self):
        led = self.capture("Pay the dues", due="2026-01-10", recurrence="FREQ=MONTHLY", recurrence_lead=3)
        backlog, planned, work, ahead, summary = self.views("2026-01-06")
        self.assertEqual(backlog[led], ("2026-01-07", 1))
        self.assertNotIn(led, planned | work | ahead)
        self.assertEqual((summary["planned"], summary["planned_this_week"]), (0, 0))
        backlog, planned, work, ahead, summary = self.views("2026-01-07")
        self.assertEqual(backlog[led], ("2026-01-07", 0))
        self.assertIn(led, planned)
        self.assertNotIn(led, work)
        self.assertIn(led, ahead)
        self.assertEqual((summary["planned"], summary["planned_this_week"]), (1, 1))
        backlog, planned, work, _, _ = self.views("2026-01-10")
        self.assertEqual(backlog[led], ("2026-01-07", 0))
        self.assertIn(led, work)

    def test_one_item_read_names_the_day_the_list_names(self):
        # Details read one item; its open day is the list's, so the dashboard issues no SQL of its own.
        led = self.capture("Pay the dues", due="2026-01-10", recurrence="FREQ=MONTHLY", recurrence_lead=3)
        plain = self.capture("Plain", due="2026-01-10")
        self.assertEqual([reads.item_opens_on(self.db, item) for item in (led, plain, 999)], ["2026-01-07", None, None])
        # The read is its own: the revision `item_state` hashes is the one `revisions` gives.
        self.assertEqual(reads.revisions(self.db, [led])[led], item_state(self.db, led)["revision"])

    def test_no_lead_opens_on_the_due_date(self):
        series = self.capture("Water", due="2026-01-10", recurrence="FREQ=WEEKLY")
        backlog, planned, work, ahead, _ = self.views("2026-01-09")
        self.assertEqual(backlog[series], ("2026-01-10", 1))
        self.assertNotIn(series, planned | work | ahead)
        backlog, planned, work, _, _ = self.views("2026-01-10")
        self.assertEqual(backlog[series], ("2026-01-10", 0))
        self.assertIn(series, work)

    def test_a_plain_row_and_a_done_row_are_never_scheduled(self):
        plain = self.capture("Plain", due="2026-01-10")
        done = self.capture("Done", due="2026-01-10", recurrence="FREQ=WEEKLY", recurrence_lead=2)
        with patch("sd_db.workflow._today", return_value=date(2026, 1, 1)):
            spawned = change_status(self.db, done, "done", who="operator")["next_occurrence"]
        backlog, planned, _, _, _ = self.views("2026-01-02")
        self.assertEqual(backlog[plain], (None, 0))
        self.assertIn(plain, planned)
        self.assertEqual(backlog[done], (None, 0))
        # The spawned row, due 2026-01-17 with the lead it carried, is scheduled.
        self.assertEqual(backlog[spawned], ("2026-01-15", 1))
        self.assertNotIn(spawned, planned)

    def test_a_lead_written_around_the_checks_shows_the_row(self):
        # Failures: a lead the library would refuse is unknown, and an
        # unknown never hides work.
        rows = {}
        for lead in (-2, "abc", 3661, 2.5):
            rows[lead] = self.capture(f"Lead {lead!r}", due="2026-01-10", recurrence="FREQ=WEEKLY")
            self.db.execute("UPDATE item SET recurrence_lead = ? WHERE id = ?", (lead, rows[lead]))
        backlog, planned, _, _, _ = self.views("2026-01-02")
        for lead, item in rows.items():
            with self.subTest(lead=lead):
                self.assertEqual(backlog[item], (None, 0))
                self.assertIn(item, planned)


if __name__ == "__main__":
    unittest.main()
