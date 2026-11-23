"""Every write is a named function, and a status change carries its note."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sd_db import (
    active_trials,
    add_note,
    connect,
    create_assignment,
    create_item,
    end_trial,
    freeze_window,
    record_cost,
    record_skill_use,
    record_state,
    resolve_state,
    set_freeze,
    set_item_fields,
    set_view,
    skill_use_since,
    snooze,
    snoozed,
    start_trial,
    transition,
    trials,
    unresolved_state,
    update_assignment,
    upsert_repo,
    upsert_shadow,
    views,
)
from sd_db.errors import SdDbError
from sd_db.migrate import initialise
from sd_db.writes import SNOOZE_FOREVER, TransitionRefused, now, stamp


class WriteCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.connection = connect(self.path)
        self.addCleanup(self.connection.close)

    def raw_state(self, kind, key, body, timestamp="2026-10-10T12:00:00Z"):
        """A row as a store may hold it, body verbatim: `record_state` serialises every body (sd:3578)."""
        self.connection.execute("INSERT INTO state (kind, key, timestamp, body) VALUES (?, ?, ?, ?)",
                                (kind, key, timestamp, body))

    def notes(self, item, kind=None):
        if kind is None:
            rows = self.connection.execute(
                "SELECT * FROM note WHERE item = ? ORDER BY id", (item,)
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM note WHERE item = ? AND kind = ? ORDER BY id", (item, kind)
            )
        return list(rows)


class TheStatusRule(WriteCase):
    def test_opening_an_item_writes_the_first_status_note(self):
        item = create_item(self.connection, kind="work", title="A thing")
        history = self.notes(item, "status_change")
        self.assertEqual(len(history), 1)
        self.assertIn("planning", history[0]["body"])

    def test_a_transition_writes_the_row_and_the_note_together(self):
        item = create_item(self.connection, kind="work", title="A thing")
        previous = transition(self.connection, item, "in_progress", who="sd-ship")
        self.assertEqual(previous, "planning")
        row = self.connection.execute(
            "SELECT status FROM item WHERE id = ?", (item,)
        ).fetchone()
        self.assertEqual(row["status"], "in_progress")
        history = self.notes(item, "status_change")
        self.assertEqual(len(history), 2)
        self.assertIn("planning -> in_progress by sd-ship", history[1]["body"])

    def test_a_transition_to_the_same_status_writes_nothing(self):
        item = create_item(self.connection, kind="work", title="A thing")
        transition(self.connection, item, "planning", who="nobody")
        self.assertEqual(len(self.notes(item, "status_change")), 1)

    def test_status_cannot_be_written_any_other_way(self):
        item = create_item(self.connection, kind="work", title="A thing")
        with self.assertRaises(TransitionRefused) as raised:
            set_item_fields(self.connection, item, status="done")
        self.assertIn("transition", str(raised.exception))

    def test_a_status_change_note_cannot_be_written_on_its_own(self):
        item = create_item(self.connection, kind="work", title="A thing")
        with self.assertRaises(TransitionRefused):
            add_note(self.connection, item, "status_change", "planning -> done")

    def test_an_unknown_status_is_refused_naming_the_vocabulary(self):
        item = create_item(self.connection, kind="work", title="A thing")
        with self.assertRaises(TransitionRefused) as raised:
            transition(self.connection, item, "shipped", who="me")
        self.assertIn("ready_to_send", str(raised.exception))

    def test_done_cancels_the_items_blocked_assignments_only(self):
        """sd:6996. A done item's blocked assignment has nothing left to do."""
        item = create_item(self.connection, kind="task", title="A thing")
        other = create_item(self.connection, kind="task", title="Another")
        blocked = create_assignment(self.connection, role="author", status="blocked", item=item)
        finished = create_assignment(self.connection, role="author", status="done", item=item)
        elsewhere = create_assignment(self.connection, role="author", status="blocked", item=other)
        transition(self.connection, item, "done", who="me")
        status = {row["id"]: row for row in self.connection.execute("SELECT id, status, ended, result FROM assignment")}
        self.assertEqual(status[blocked]["status"], "cancelled")
        self.assertIsNotNone(status[blocked]["ended"])
        self.assertIn("done", status[blocked]["result"])
        self.assertEqual(status[finished]["status"], "done")
        self.assertEqual(status[elsewhere]["status"], "blocked")

    def test_planning_cancels_the_items_blocked_assignments_too(self):
        """sd:8846. An item returned to planning wants no build, so its blocked one ends."""
        item = create_item(self.connection, kind="task", title="A thing", status="in_progress")
        blocked = create_assignment(self.connection, role="author", status="blocked", item=item)
        transition(self.connection, item, "ready", who="me")
        self.assertEqual(
            self.connection.execute("SELECT status FROM assignment WHERE id = ?", (blocked,)).fetchone()[0], "blocked")
        transition(self.connection, item, "planning", who="me")
        row = self.connection.execute("SELECT status, result FROM assignment WHERE id = ?", (blocked,)).fetchone()
        self.assertEqual((row["status"], row["result"]), ("cancelled", f"item {item} moved to planning by me"))

    def test_done_leaves_a_blocked_assignment_a_run_still_holds(self):
        """sd:6996. The lease keeps the row the runner's; `build._release` cancels it later."""
        item = create_item(self.connection, kind="task", title="A thing")
        held = create_assignment(self.connection, role="author", status="blocked", item=item)
        upsert_repo(self.connection, "/fixture/repo")
        self.connection.execute(
            "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, created_at, "
            "updated_at) VALUES ('r1', ?, 1, '/fixture/repo', 'b', 'o', '/w', '/r', 'now', 'now')", (held,))
        transition(self.connection, item, "done", who="me")
        self.assertEqual(
            self.connection.execute("SELECT status FROM assignment WHERE id = ?", (held,)).fetchone()[0], "blocked")

    def test_done_cancels_the_items_newest_failed_lane_entries_only(self):
        """pack sd:8850. Closing an item clears its failed or skipped lane entries newer than its last merge, as
        `lane cancel` does (pack sd:5838), so Landing drops it; an older one, another item's, and one beside a
        pending entry stay."""
        item = create_item(self.connection, kind="task", title="A thing")
        other = create_item(self.connection, kind="task", title="Another")
        held = create_item(self.connection, kind="task", title="Held")
        key = "lane:v1:example/repo:{}".format
        for ident, owner, status in (("a", item, "failed"), ("b", item, "merged"), ("c", item, "skipped"),
                                     ("d", item, "failed"), ("e", other, "failed"), ("f", held, "failed"),
                                     ("g", held, "pending")):
            self.raw_state("checkpoint", key(ident), json.dumps(
                {"id": ident, "item": owner, "status": status, "reason": f"{ident} broke"}))
        self.raw_state("checkpoint", "lane:v1:example/other:h", "not json")
        for each in (item, other, held):
            transition(self.connection, each, "done" if each != other else "blocked", who="operator")
        latest = {row[0][len("lane:v1:example/repo:"):]: json.loads(row[1]) for row in self.connection.execute(
            "SELECT key, body FROM state WHERE id IN (SELECT MAX(id) FROM state WHERE key LIKE 'lane:v1:example/"
            "repo:%' GROUP BY key)")}
        self.assertEqual({ident: entry["status"] for ident, entry in latest.items()},
                         {"a": "failed", "b": "merged", "c": "cancelled", "d": "cancelled", "e": "failed",
                          "f": "failed", "g": "pending"})
        self.assertIn("moved to done by operator; it was skipped: c broke", latest["c"]["reason"])
        self.assertTrue(latest["d"]["finished_at"].endswith("Z"))

    def test_a_malformed_lane_entry_never_blocks_a_move_to_done(self):
        """pack sd:8850 review round 2: any lane row in any repository is read on the way to done; one whose
        `item`, `closes` or `id` is not the shape the lane writes is skipped, never a TypeError."""
        from sd_db.reads import item_lane_entries

        item = create_item(self.connection, kind="task", title="A thing")
        for ident, body in (("m1", {"id": "m1", "item": 1, "closes": 7}), ("m2", {"id": "m2", "item": 1, "closes": [{}]}),
                            ("m3", {"id": ["x"], "item": item, "status": "failed"}),
                            ("m4", {"id": "m4", "item": [item], "status": "failed"}),
                            ("m5", {"id": "m5", "item": item, "closes": "7", "status": "failed"}),
                            ("ok", {"id": "ok", "item": item, "status": "failed"})):
            self.raw_state("checkpoint", f"lane:v1:example/repo:{ident}", json.dumps(body))
        self.assertEqual([entry["id"] for _, _, entry in item_lane_entries(self.connection, item)], ["ok"])
        transition(self.connection, item, "done", who="operator")
        status = self.connection.execute(
            "SELECT body FROM state WHERE key = 'lane:v1:example/repo:ok' ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(json.loads(status)["status"], "cancelled")

    def test_a_failed_transition_leaves_no_note_behind(self):
        """The note and the row are one transaction, in both directions."""
        with self.assertRaises(TransitionRefused):
            transition(self.connection, 999, "done", who="me")
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM note").fetchone()[0], 0
        )


class TheOtherWrites(WriteCase):
    def test_a_repository_updates_only_the_fields_given(self):
        upsert_repo(self.connection, "/repos/one", remote="git@example:one", mode="full")
        upsert_repo(self.connection, "/repos/one")
        row = self.connection.execute("SELECT * FROM repo").fetchone()
        self.assertEqual(row["remote"], "git@example:one")
        self.assertEqual(row["runner_merge"], "manual")

    def test_an_exec_note_carries_the_run_and_others_may_not(self):
        item = create_item(self.connection, kind="task", title="Run it")
        add_note(self.connection, item, "exec", "ran", started="a", ended="b", exit_code=0)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK constraint failed"):
            add_note(self.connection, item, "comment", "no", exit_code=1)

    def test_a_question_carries_options_and_a_pick(self):
        """pack sd:6708: a bare question, one option, or none recommended is refused, naming the shape."""
        item = create_item(self.connection, kind="task", title="Ask")
        for body in ("Which store?", "Which store?\nOption: Rows (Recommended)",
                     "Which store?\nOption: Rows\nOption: Files"):
            with self.subTest(body=body), self.assertRaisesRegex(SdDbError, "two or more `Option:` lines"):
                add_note(self.connection, item, "question", body)
        add_note(self.connection, item, "question", "Which store?\nOption: Rows (Recommended)\nOption: Files")
        add_note(self.connection, item, "question", "Label it.\nOption: Yes\nOption: No", labelling=True)
        self.assertEqual(len(self.notes(item, "question")), 2)

    def test_one_tracker_re_reading_a_url_updates_its_own_row(self):
        upsert_shadow(self.connection, tracker="github", url="u", title="first")
        upsert_shadow(self.connection, tracker="github", url="u", title="second")
        rows = list(self.connection.execute("SELECT title FROM shadow"))
        self.assertEqual([row["title"] for row in rows], ["second"])

    def test_two_trackers_may_hold_the_same_url(self):
        """The key is `(tracker, url)`, so neither tracker can adopt the other's
        row. Under the url-alone key this collector's write reassigned the row's
        `tracker` column in silence: no error, no constraint violation, and the
        first tracker's identity simply gone."""
        url = "https://github.com/o/r/issues/1"
        upsert_shadow(self.connection, tracker="github", url=url, title="from github")
        upsert_shadow(self.connection, tracker="jira", url=url, title="from jira")
        rows = list(self.connection.execute(
            "SELECT tracker, title FROM shadow ORDER BY tracker"))
        self.assertEqual(
            [(row["tracker"], row["title"]) for row in rows],
            [("github", "from github"), ("jira", "from jira")],
        )

    def test_an_assignment_updates_through_its_named_function(self):
        item = create_item(self.connection, kind="work", title="A thing")
        assignment = create_assignment(self.connection, role="reviewer", status="queued", item=item)
        update_assignment(self.connection, assignment, status="blocked", phase="requested")
        row = self.connection.execute(
            "SELECT status, phase FROM assignment WHERE id = ?", (assignment,)
        ).fetchone()
        self.assertEqual((row["status"], row["phase"]), ("blocked", "requested"))

    def test_an_unknown_column_is_refused_rather_than_ignored(self):
        item = create_item(self.connection, kind="work", title="A thing")
        with self.assertRaises(SdDbError):
            set_item_fields(self.connection, item, headline="oops")

    def test_a_cost_row_records_what_kind_of_number_it_is(self):
        record_cost(self.connection, source="meter", bill=None, used_percent=42.0)
        row = self.connection.execute("SELECT source, used_percent FROM cost").fetchone()
        self.assertEqual((row["source"], row["used_percent"]), ("meter", 42.0))


class TheStateTable(WriteCase):
    def test_a_kind_with_no_home_is_refused_naming_the_kinds(self):
        with self.assertRaises(SdDbError) as raised:
            record_state(self.connection, "sighting", key="x")
        self.assertIn("checkpoint", str(raised.exception))

    def test_a_record_is_open_until_it_is_resolved(self):
        row = record_state(self.connection, "restore", key="2026-09-06")
        self.assertEqual(len(unresolved_state(self.connection, "restore")), 1)
        resolve_state(self.connection, row)
        self.assertEqual(unresolved_state(self.connection, "restore"), [])

    def test_every_body_is_stored_as_json_a_string_included(self):
        """A `str` body was written raw, so 442 backup rows were not JSON (sd:3578)."""
        for body in ("2026-10-12T15:50:05+00:00", "{not json", {"at": "now"}, ["a"], 3):
            with self.subTest(body=body):
                row = record_state(self.connection, "checkpoint", key="body-test", body=body)
                stored = self.connection.execute("SELECT body FROM state WHERE id=?", (row,)).fetchone()[0]
                self.assertEqual(json.loads(stored), body)


class TheFreeze(WriteCase):
    """A repository's freeze window (sd:6879): a `freeze` state row per
    write, keyed by owner/name, the latest the answer, the reader's clock
    the end."""

    NOW = "2026-10-10T12:00:00Z"

    def test_a_window_holds_until_its_end_and_a_lift_ends_it(self):
        set_freeze(self.connection, "Example/Pack", "2026-10-11T08:00:00-06:00", "release", now=self.NOW)
        held = {"until": "2026-10-11T14:00:00+00:00", "reason": "release"}
        self.assertEqual(freeze_window(self.connection, "example/pack", now=self.NOW), held)
        self.assertIsNone(freeze_window(self.connection, "example/other", now=self.NOW))
        self.assertIsNone(freeze_window(self.connection, "example/pack", now="2026-10-11T14:00:00Z"))
        set_freeze(self.connection, "example/pack", None)
        self.assertIsNone(freeze_window(self.connection, "example/pack", now=self.NOW))

    def test_a_bad_window_is_refused_and_an_unreadable_row_freezes(self):
        for target, until, reason in (("pack", "2026-10-11T00:00:00Z", "r"),
                                      ("example/pack", "2026-10-09T00:00:00Z", "r"),
                                      ("example/pack", "2026-12-30T00:00:00Z", "r"),
                                      ("example/pack", "2026-10-11T00:00:00Z", " ")):
            with self.subTest(target=target, until=until), self.assertRaises(SdDbError):
                set_freeze(self.connection, target, until, reason, now=self.NOW)
        record_state(self.connection, "freeze", key="example/pack", body={"until": "soon"})
        self.assertEqual((freeze_window(self.connection, "example/pack", now=self.NOW) or {}).get("until"), "unknown")


class TheSnooze(WriteCase):
    """A row hidden until a time (sd:1896): a `snooze` state row per write,
    the latest per key the answer, and the reader's clock the expiry. Each
    snooze carries `seen`, what the row showed, so the page can tell a row
    whose problem changed under the same id."""

    NOW = "2026-10-10T12:00:00Z"

    def test_a_snooze_holds_until_its_time_and_then_lapses(self):
        snooze(self.connection, "today:job:nightly:1", "2026-10-11T08:00:00-06:00", seen="0f0f", now=self.NOW)
        held = {"today:job:nightly:1": {"until": "2026-10-11T14:00:00+00:00", "seen": "0f0f"}}
        self.assertEqual(snoozed(self.connection, now=self.NOW), held)
        self.assertEqual(snoozed(self.connection, now="2026-10-11T13:59:59Z"), held)
        self.assertEqual(snoozed(self.connection, now="2026-10-11T14:00:00Z"), {})
        row = self.connection.execute("SELECT kind, key, body, resolved_at FROM state").fetchone()
        self.assertEqual(tuple(row), ("snooze", "today:job:nightly:1",
                                      '{"seen": "0f0f", "until": "2026-10-11T14:00:00+00:00"}', None))

    def test_the_latest_write_for_a_key_wins_and_a_cleared_one_shows_the_row(self):
        snooze(self.connection, "health:br:system", "2026-10-17T12:00:00Z", seen="a1", now=self.NOW)
        snooze(self.connection, "health:br:system", "2026-10-10T13:00:00Z", seen="a2", now=self.NOW)
        snooze(self.connection, "today:ahead:system:2", "2026-10-11T12:00:00Z", seen="b1", now=self.NOW)
        self.assertEqual(snoozed(self.connection, now=self.NOW),
                         {"health:br:system": {"until": "2026-10-10T13:00:00+00:00", "seen": "a2"},
                          "today:ahead:system:2": {"until": "2026-10-11T12:00:00+00:00", "seen": "b1"}})
        snooze(self.connection, "health:br:system", None, now=self.NOW)
        self.assertEqual(list(snoozed(self.connection, now=self.NOW)), ["today:ahead:system:2"])
        # Clearing a key nobody snoozed writes the same row and shows nothing new.
        snooze(self.connection, "today:dark:jobs", None, now=self.NOW)
        self.assertEqual(len(snoozed(self.connection, now=self.NOW)), 1)

    def test_a_time_in_the_past_too_far_ahead_or_without_a_zone_is_refused(self):
        for until, said in (("2026-10-10T11:59:59Z", "already past"), ("2026-10-10T12:00:00Z", "already past"),
                            ("2026-11-10T12:00:01Z", "31 days"), ("2026-10-11T08:00:00", "no timezone"),
                            ("tomorrow", "not an ISO-8601")):
            with self.subTest(until=until), self.assertRaises(SdDbError) as raised:
                snooze(self.connection, "today:job:nightly:1", until, seen="0f0f", now=self.NOW)
            self.assertIn(said, str(raised.exception))
        for key in ("", None, 7, "x" * 513):
            with self.subTest(key=key), self.assertRaises(SdDbError):
                snooze(self.connection, key, "2026-10-11T08:00:00Z", seen="0f0f", now=self.NOW)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM state").fetchone()[0], 0)

    def test_a_snooze_without_what_the_row_showed_is_refused(self):
        """An id names a resource, not its problem: a snooze bound to no `seen` would hide whatever the row reads next."""
        for seen in (None, "", "  ", 7, "f" * 129):
            with self.subTest(seen=seen), self.assertRaises(SdDbError) as raised:
                snooze(self.connection, "health:dep:/c/alpha", "2026-10-11T08:00:00Z", seen=seen, now=self.NOW)
            self.assertIn("seen", str(raised.exception))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM state").fetchone()[0], 0)

    def test_a_snooze_with_no_end_holds_past_any_time_and_clears_like_any_other(self):
        """Health's Ignore (system sd:5470): `SNOOZE_FOREVER` has no time to judge, still binds to `seen`, and clears with None."""
        snooze(self.connection, "health:dep:/c/alpha", SNOOZE_FOREVER, seen="0f0f", now=self.NOW)
        held = {"health:dep:/c/alpha": {"until": SNOOZE_FOREVER, "seen": "0f0f"}}
        self.assertEqual(snoozed(self.connection, now=self.NOW), held)
        self.assertEqual(snoozed(self.connection, now="2036-10-10T12:00:00Z"), held)
        self.assertEqual(self.connection.execute("SELECT body FROM state").fetchone()[0], '{"seen": "0f0f", "until": "forever"}')
        with self.assertRaises(SdDbError) as raised:
            snooze(self.connection, "health:dep:/c/beta", SNOOZE_FOREVER, seen=None, now=self.NOW)
        self.assertIn("seen", str(raised.exception))
        snooze(self.connection, "health:dep:/c/alpha", None, now=self.NOW)
        self.assertEqual(snoozed(self.connection, now=self.NOW), {})

    def test_a_body_the_reader_cannot_parse_shows_the_row(self):
        """An unreadable snooze is not a snooze: the row it would hide shows. So is one an earlier build wrote
        with no `seen`: it binds to no problem."""
        for key, body in (("today:a", "not json"), ("today:b", '["2026-10-11T08:00:00Z"]'),
                          ("today:c", '{"until": "soon", "seen": "0f0f"}'), ("today:d", None),
                          ("today:e", '{"until": "2026-10-11T08:00:00Z"}'),
                          ("today:f", '{"until": "2026-10-11T08:00:00Z", "seen": 7}'),
                          ("today:g", '{"until": "2026-10-11T08:00:00Z", "seen": ""}')):
            self.raw_state("snooze", key, body, timestamp=self.NOW)
        self.assertEqual(snoozed(self.connection, now=self.NOW), {})


class TheView(WriteCase):
    """A Documents file pinned, hidden or tagged (sd:3430): a `view` state row
    per write, keyed `<root key>/<file>`, the latest per key the answer. A
    field the call leaves out keeps its value, so Undo is the same call with
    the value the page showed before. Nothing expires a row."""

    KEY = "system/index.html"

    def test_each_field_is_set_alone_and_the_others_keep_their_value(self):
        self.assertEqual(set_view(self.connection, self.KEY, pinned=True),
                         {"pinned": True, "hidden": False, "tags": []})
        self.assertEqual(set_view(self.connection, self.KEY, tags=["plan", " review ", "plan"]),
                         {"pinned": True, "hidden": False, "tags": ["plan", "review"]})
        self.assertEqual(set_view(self.connection, self.KEY, hidden=True),
                         {"pinned": True, "hidden": True, "tags": ["plan", "review"]})
        self.assertEqual(views(self.connection), {self.KEY: {"pinned": True, "hidden": True, "tags": ["plan", "review"]}})
        row = self.connection.execute("SELECT kind, key, body, resolved_at FROM state ORDER BY id DESC").fetchone()
        self.assertEqual(tuple(row), ("view", self.KEY,
                                      '{"hidden": true, "pinned": true, "tags": ["plan", "review"]}', None))

    def test_unpin_unhide_and_untag_clear_and_a_cleared_key_is_not_listed(self):
        set_view(self.connection, self.KEY, pinned=True, hidden=True, tags=["plan"])
        set_view(self.connection, "pack/report.html", tags=["ship"])
        self.assertEqual(set_view(self.connection, self.KEY, pinned=False),
                         {"pinned": False, "hidden": True, "tags": ["plan"]})
        self.assertEqual(set_view(self.connection, self.KEY, hidden=False, tags=[]),
                         {"pinned": False, "hidden": False, "tags": []})
        self.assertEqual(views(self.connection), {"pack/report.html": {"pinned": False, "hidden": False, "tags": ["ship"]}})
        # Undo of the clear is the same call with what the page showed before.
        set_view(self.connection, self.KEY, pinned=True)
        self.assertEqual(views(self.connection)[self.KEY], {"pinned": True, "hidden": False, "tags": []})

    def test_a_bad_key_flag_or_tag_is_refused_and_writes_nothing(self):
        for key in ("", "  ", None, 7, "x" * 513):
            with self.subTest(key=key), self.assertRaises(SdDbError):
                set_view(self.connection, key, pinned=True)
        for fields in ({"pinned": 1}, {"hidden": "yes"}, {"tags": "plan"}, {"tags": [7]}, {"tags": [" "]},
                       {"tags": ["x" * 65]}, {"tags": [f"t{n}" for n in range(17)]}, {}):
            with self.subTest(fields=fields), self.assertRaises(SdDbError):
                set_view(self.connection, self.KEY, **fields)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM state").fetchone()[0], 0)

    def test_a_body_the_reader_cannot_parse_is_no_state_and_a_write_starts_from_clear(self):
        """An unreadable view is not a view: it pins, hides and tags nothing, and the next write replaces it."""
        for key, body in (("r/a", "not json"), ("r/b", '["pinned"]'), ("r/c", None),
                          ("r/d", '{"pinned": 1, "hidden": false, "tags": []}'),
                          ("r/e", '{"pinned": true, "hidden": false, "tags": "plan"}'),
                          ("r/f", '{"pinned": true, "hidden": false}'),
                          ("r/g", '{"pinned": true, "hidden": false, "tags": [""]}'),
                          ("r/h", '{"pinned": true, "hidden": false, "tags": ["%s"]}' % ("x" * 65)),
                          ("r/i", json.dumps({"pinned": True, "hidden": False, "tags": [f"t{n}" for n in range(17)]}))):
            self.raw_state("view", key, body)
        self.assertEqual(views(self.connection), {})
        self.assertEqual(set_view(self.connection, "r/f", hidden=True), {"pinned": False, "hidden": True, "tags": []})
        # A partial write never carries tags the writer itself would refuse.
        for key in ("r/g", "r/h", "r/i"):
            self.assertEqual(set_view(self.connection, key, pinned=True), {"pinned": True, "hidden": False, "tags": []})


if __name__ == "__main__":
    unittest.main()


class OneShapeOnDisk(WriteCase):
    """Two spellings of one instant are two instants to a text comparison.

    `now()` writes `+00:00`. `...Z` is the same moment and sorts *after* it,
    because `Z` is 0x5A and `+` is 0x2B. Every stamp a caller hands in is
    normalized at the write, so this is a property of the rows rather than a
    rule callers are asked to remember.
    """

    def test_the_two_spellings_really_do_order_differently(self):
        """The premise. Without it every test below passes for no reason."""
        plus = "2026-09-06T19:11:39+00:00"
        zulu = "2026-09-06T19:11:39Z"
        self.assertNotEqual(plus, zulu)
        self.assertGreater(zulu, plus)
        self.assertEqual(stamp(zulu), plus)

    def test_now_writes_the_shape_stamp_normalizes_to(self):
        """One clock read, three assertions. Two reads is a race, not a test.

        `stamp()` is what every write puts a caller's value through, and
        `now()` is what it puts its own through. If those two disagreed on
        shape the normalization would be normalizing to something the default
        does not use, and every comparison in this file would still be
        between two shapes.
        """
        moment = now()
        self.assertTrue(moment.endswith("+00:00"))
        self.assertEqual(stamp(moment), moment)
        self.assertEqual(stamp(moment.replace("+00:00", "Z")), moment)

    def test_an_offset_stamp_is_converted_rather_than_truncated(self):
        self.assertEqual(stamp("2026-09-06T14:11:39-05:00"), "2026-09-06T19:11:39+00:00")

    def test_a_trial_expiry_lands_normalized(self):
        start_trial(self.connection, "sd-example-a", "2099-01-01T00:00:00Z")
        self.assertEqual(trials(self.connection)[0]["expires"], "2099-01-01T00:00:00+00:00")

    def test_a_use_timestamp_lands_normalized(self):
        record_skill_use(self.connection, "sd-example-a", timestamp="2026-01-01T00:00:00Z")
        row = self.connection.execute("SELECT timestamp FROM skill_use").fetchone()
        self.assertEqual(row["timestamp"], "2026-01-01T00:00:00+00:00")

    def test_the_boundary_holds_across_the_two_spellings(self):
        """The bug this closes, stated as the question the installer asks.

        A trial started at one instant and a use recorded at that same
        instant, spelled the other way. `>=` must count it. Before the
        normalization it did not, because `+00:00` < `Z` put the use before
        its own trial.
        """
        start_trial(self.connection, "sd-example-a", "2099-01-01T00:00:00Z")
        started = trials(self.connection)[0]["started"]
        zulu = started.replace("+00:00", "Z")
        self.assertNotEqual(zulu, started)
        record_skill_use(self.connection, "sd-example-a", timestamp=zulu)
        self.assertEqual(skill_use_since(self.connection, "sd-example-a", started), 1)

    def test_active_trials_compares_a_passed_moment_in_the_same_shape(self):
        start_trial(self.connection, "sd-example-a", "2026-10-06T00:00:00+00:00")
        # The same instant as the expiry, spelled the other way. Neither
        # spelling may make an expired trial look active.
        self.assertEqual(active_trials(self.connection, "2026-10-06T00:00:00Z"), [])
        self.assertEqual(active_trials(self.connection, "2026-10-05T23:59:59Z")[0]["skill"], "sd-example-a")

    def test_a_naive_stamp_is_refused_and_says_why(self):
        with self.assertRaises(SdDbError) as caught:
            start_trial(self.connection, "sd-example-a", "2099-01-01T00:00:00")
        self.assertIn("carries no timezone", str(caught.exception))
        self.assertEqual(trials(self.connection), [])

    def test_something_that_is_not_a_timestamp_is_refused(self):
        with self.assertRaises(SdDbError) as caught:
            record_skill_use(self.connection, "sd-example-a", timestamp="thursday")
        self.assertIn("is not an ISO-8601 timestamp", str(caught.exception))


class TrialsAndUse(WriteCase):
    """The two reads the installer makes, and the one removal it performs.

    Criterion 24 renders "the union of the paths plus active trials" and
    criterion 25 removes an expired trial that earned no use. Both are reads
    the library did not have when the trials were first written, which is why
    they arrive here rather than beside `start_trial`.
    """

    def test_an_active_trial_is_the_one_that_has_not_expired(self):
        start_trial(self.connection, "sd-example-a", "2099-01-01T00:00:00Z")
        start_trial(self.connection, "sd-example-b", "2001-01-01T00:00:00Z")
        self.assertEqual(
            [row["skill"] for row in active_trials(self.connection)], ["sd-example-a"]
        )
        # Both are still rows. The installer needs the expired one to remove it,
        # so the unfiltered read has to keep answering with it.
        self.assertEqual(
            [row["skill"] for row in trials(self.connection)], ["sd-example-a", "sd-example-b"]
        )

    def test_the_moment_is_the_callers_to_choose(self):
        start_trial(self.connection, "sd-example-a", "2026-10-06T00:00:00Z")
        self.assertEqual(len(active_trials(self.connection, "2026-09-06T00:00:00Z")), 1)
        self.assertEqual(len(active_trials(self.connection, "2026-11-06T00:00:00Z")), 0)

    def test_a_trial_starting_again_replaces_its_expiry_and_adds_no_row(self):
        start_trial(self.connection, "sd-example-a", "2026-10-06T00:00:00Z")
        start_trial(self.connection, "sd-example-a", "2026-12-06T00:00:00Z")
        rows = trials(self.connection)
        self.assertEqual(len(rows), 1)
        # Normalized on the way in, so the row is the shape `now()` writes.
        self.assertEqual(rows[0]["expires"], "2026-12-06T00:00:00+00:00")

    def test_use_is_counted_from_the_trials_own_start_and_not_before_it(self):
        # Stamped rather than clocked: `now()` has one-second resolution, and a
        # use written in the same second as the trial began cannot be told from
        # one written after it. The parse of `~/.codex/sessions` needs the same
        # parameter for a real reason, so the test uses the real API.
        record_skill_use(self.connection, "sd-example-a", timestamp="2026-01-01T00:00:00Z")
        start_trial(self.connection, "sd-example-a", "2099-01-01T00:00:00Z")
        row = trials(self.connection)[0]
        # The use above happened before the trial began. Counted from the
        # trial's own start it is not evidence the trial earned anything.
        self.assertEqual(skill_use_since(self.connection, "sd-example-a", row["started"]), 0)
        record_skill_use(self.connection, "sd-example-a")
        self.assertEqual(skill_use_since(self.connection, "sd-example-a", row["started"]), 1)

    def test_use_by_another_skill_is_not_this_skills_use(self):
        start_trial(self.connection, "sd-example-a", "2099-01-01T00:00:00Z")
        record_skill_use(self.connection, "sd-example-b")
        row = trials(self.connection)[0]
        self.assertEqual(skill_use_since(self.connection, "sd-example-a", row["started"]), 0)

    def test_ending_a_trial_says_whether_there_was_one(self):
        start_trial(self.connection, "sd-example-a", "2099-01-01T00:00:00Z")
        self.assertTrue(end_trial(self.connection, "sd-example-a"))
        self.assertEqual(trials(self.connection), [])
        # The second call has nothing to remove, and says so rather than
        # letting a caller print "removed" twice.
        self.assertFalse(end_trial(self.connection, "sd-example-a"))
