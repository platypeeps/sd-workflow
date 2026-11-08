"""The collector that keeps `shadow` from freezing on the day of the switch.

A one-time import of `index.sqlite` would leave the table correct at midnight
and wrong by lunch. These assert the two rules a windowed collector cannot get
wrong: an hour of overlap, and a watermark that moves only on success.
"""

import json
import os
import socket
import socketserver
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sd_db import connect
from sd_db.migrate import initialise
from sd_db.progress import tracker_freshness
from sd_db.shadow_sync import (
    FIRST_RUN_WINDOW,
    NO_AUTH,
    OVERLAP,
    TRACKER,
    SyncBusy,
    _sync,
    iso,
    parse_iso,
    read_watermark,
    search,
    sync,
    sync_lock,
    window_start,
    write_watermark,
)

from .test_shadow_jira import (
    BROKEN_ANSWERS,
    JIRA_ENV,
    NON_ASCII_HOST,
    NON_ASCII_HOST_REASON,
    NOT_A_HOST,
    NOT_A_HOST_REASON,
    SECRET,
    UNSPLITTABLE,
    RawServer,
    jira_issue,
    jira_transport,
    no_socket,
    without_jira_environment,
)


def node(number, *, url=None, kind="Issue", state="OPEN", title="a title"):
    return {
        "__typename": kind,
        "number": number,
        "title": title,
        "url": url or f"https://github.com/example-org/benchmark/issues/{number}",
        "state": state,
        "updatedAt": "2026-09-05T10:00:00Z",
        "author": {"login": "octocat"},
        "repository": {"nameWithOwner": "example-org/benchmark"},
    }


class Gh:
    """A `gh` seam: it records every argv and answers from a script.

    Not a patch of `subprocess`. A patched suite proves a call was made; this
    proves it was made and spelled correctly, which is the harness's own rule.
    """

    def __init__(self, *, nodes=None, auth=True, fail=None, count=None):
        self.calls: list[list[str]] = []
        self.nodes = nodes if nodes is not None else []
        self.auth = auth
        self.fail = fail
        self.count = count

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["gh", "auth"]:
            return (0, "", "") if self.auth else (1, "", "not logged in")
        if self.fail:
            return 1, "", self.fail
        payload = {
            "data": {
                "search": {
                    "issueCount": self.count if self.count is not None else len(self.nodes),
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": self.nodes,
                }
            }
        }
        return 0, json.dumps(payload), ""

    @property
    def queries(self):
        out = []
        for argv in self.calls:
            for index, value in enumerate(argv):
                if value == "-f" and argv[index + 1].startswith("q="):
                    out.append(argv[index + 1][2:])
        return out


class SyncCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        database = Path(self.tmp.name) / "sd.db"
        initialise(database)
        self.connection = connect(database)
        self.addCleanup(self.connection.close)
        self.now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    def shadow(self):
        return {row["url"]: row for row in self.connection.execute("SELECT * FROM shadow")}


class TheWindow(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    def test_a_first_run_reads_ninety_days(self):
        self.assertEqual(window_start(None, self.now), self.now - FIRST_RUN_WINDOW)

    def test_a_watermark_is_re_read_with_an_hour_of_overlap(self):
        watermark = iso(self.now - timedelta(days=1))
        self.assertEqual(
            window_start(watermark, self.now),
            self.now - timedelta(days=1) - OVERLAP,
        )

    def test_a_watermark_it_cannot_parse_widens_the_window(self):
        """Narrowing on garbage is how a windowed collector stops collecting."""
        self.assertEqual(
            window_start("not a timestamp", self.now), self.now - FIRST_RUN_WINDOW
        )


class TheWatermark(SyncCase):
    def test_a_first_run_has_none(self):
        self.assertIsNone(read_watermark(self.connection))

    def test_only_a_resolved_row_counts(self):
        """A cursor written half-way is a cursor nothing may resume from."""
        from sd_db.writes import record_state

        record_state(
            self.connection, "watermark", key=TRACKER,
            body={"collected_at": "2026-09-01T00:00:00Z"},
        )
        self.assertIsNone(read_watermark(self.connection))
        write_watermark(self.connection, TRACKER, "2026-09-02T00:00:00Z", "x")
        self.assertEqual(read_watermark(self.connection), "2026-09-02T00:00:00Z")

    def test_the_newest_resolved_row_wins(self):
        write_watermark(self.connection, TRACKER, "2026-09-02T00:00:00Z", "x")
        write_watermark(self.connection, TRACKER, "2026-09-03T00:00:00Z", "x")
        self.assertEqual(read_watermark(self.connection), "2026-09-03T00:00:00Z")

    def test_a_watermark_row_leaves_no_unresolved_state_behind(self):
        """`sd-db.sh status` prints unresolved records; a nightly cursor is
        not something the operator has to reconcile."""
        write_watermark(self.connection, TRACKER, "2026-09-02T00:00:00Z", "x")
        rows = list(self.connection.execute(
            "SELECT * FROM state WHERE resolved_at IS NULL"
        ))
        self.assertEqual(rows, [])


class TheSync(SyncCase):
    def test_collected_issues_land_as_shadow_rows(self):
        from .test_contribution_github import URL, Api

        runner = Gh(nodes=[node(11), node(7, kind="PullRequest", url=URL)])
        result = sync(self.connection, now=self.now, runner=runner,
                      contribution_client=Api().client(), notifier=lambda claim: "sent")
        self.assertTrue(result.ok)
        rows = self.shadow()
        self.assertEqual(len(rows), 2)
        kinds = {row["kind"] for row in rows.values()}
        self.assertEqual(kinds, {"issue", "pull"})
        self.assertEqual({row["tracker"] for row in rows.values()}, {TRACKER})

    def test_the_watermark_moves_on_success(self):
        sync(self.connection, now=self.now, runner=Gh(nodes=[node(11)]))
        self.assertEqual(read_watermark(self.connection), iso(self.now))

    def test_a_failed_collect_holds_the_watermark(self):
        """A failed run costs a repeated window, never a skipped one."""
        write_watermark(self.connection, TRACKER, "2026-09-01T00:00:00Z", "x")
        result = sync(
            self.connection, now=self.now, runner=Gh(fail="the API said no")
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(read_watermark(self.connection), "2026-09-01T00:00:00Z")

    def test_a_missing_credential_is_reported_by_name_and_not_by_secret(self):
        result = sync(self.connection, now=self.now, runner=Gh(auth=False))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, NO_AUTH)
        self.assertEqual(self.shadow(), {})

    def test_the_four_buckets_are_searched_separately(self):
        """`involves:@me` would collapse *why* an issue reached you."""
        runner = Gh(nodes=[])
        sync(self.connection, now=self.now, runner=runner)
        self.assertEqual(len(runner.queries), 4)
        self.assertTrue(any("review-requested:@me" in one for one in runner.queries))
        expected = f"updated:{iso(self.now - FIRST_RUN_WINDOW)}..{iso(self.now)}"
        self.assertTrue(all(expected in one for one in runner.queries))

    def test_a_second_sync_updates_the_row_rather_than_adding_one(self):
        sync(self.connection, now=self.now, runner=Gh(nodes=[node(11)]))
        result = sync(
            self.connection,
            now=self.now + timedelta(hours=1),
            runner=Gh(nodes=[{**node(11, state="CLOSED"),
                              "updatedAt": iso(self.now + timedelta(hours=1))}]),
        )
        self.assertTrue(result.ok, result.reason)
        rows = self.shadow()
        self.assertEqual(len(rows), 1)
        self.assertEqual(list(rows.values())[0]["state"], "closed")

    def test_first_seen_survives_and_last_seen_moves(self):
        sync(self.connection, now=self.now, runner=Gh(nodes=[node(11)]))
        first = list(self.shadow().values())[0]
        result = sync(
            self.connection,
            now=self.now + timedelta(hours=1),
            runner=Gh(nodes=[{**node(11, title="renamed"),
                              "updatedAt": iso(self.now + timedelta(hours=1))}]),
        )
        self.assertTrue(result.ok, result.reason)
        second = list(self.shadow().values())[0]
        self.assertEqual(first["first_seen"], second["first_seen"])
        self.assertEqual(second["title"], "renamed")

    def test_a_truncated_bucket_is_reported_rather_than_swallowed(self):
        runner = Gh(nodes=[node(11)], count=900)
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertEqual(len(result.truncated), 4)
        self.assertFalse(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertIsNone(read_watermark(self.connection))
        self.assertIn("truncated buckets", "\n".join(result.report()))


class WindowedGh:
    """A capped, paginated search with real timestamp filtering and URL identities."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv == ["gh", "auth", "status"]:
            return 0, "", ""
        query = next(value[2:] for value in argv if value.startswith("q="))
        lower, upper = map(parse_iso, query.split("updated:", 1)[1].split(".."))
        selected = [value for value in self.nodes
                    if lower <= parse_iso(value["updatedAt"]) <= upper]
        if not query.startswith("author:@me "):
            selected = []
        offset = next((int(value[6:]) for value in argv if value.startswith("after=")), 0)
        visible = selected[:1000]
        page = visible[offset:offset + 100]
        more = offset + len(page) < len(visible)
        block = {"issueCount": len(selected), "nodes": page,
                 "pageInfo": {"hasNextPage": more, "endCursor": str(offset + len(page)) if more else None}}
        return 0, json.dumps({"data": {"search": block}}), ""


class CompleteCoverage(SyncCase):
    def heartbeat(self):
        row = self.connection.execute(
            "SELECT body FROM state WHERE kind='heartbeat' AND key='tracker-sync:github' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return json.loads(row[0])

    def test_more_than_one_thousand_results_cover_each_boundary_and_page(self):
        start = self.now - timedelta(seconds=1100)
        nodes = [{**node(number), "updatedAt": iso(start + timedelta(seconds=number))}
                 for number in range(1101)]
        runner = WindowedGh(nodes)

        result = sync(self.connection, now=self.now, since=start, runner=runner)

        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.watermark_moved)
        self.assertEqual(result.truncated, [])
        self.assertEqual(set(self.shadow()), {value["url"] for value in nodes})
        self.assertEqual(result.written, 1101)
        self.assertEqual(result.requests, len(runner.calls) - 1)
        self.assertLess(result.requests, 20)
        self.assertTrue(any(value.startswith("after=") for call in runner.calls for value in call))
        for bucket in ("assigned", "mentioned", "review-requested", "author"):
            windows = sorted((entry for entry in result.coverage if entry["bucket"] == bucket),
                             key=lambda entry: entry["start"])
            cursor = start
            for entry in windows:
                self.assertTrue(entry["complete"])
                self.assertEqual(parse_iso(entry["start"]), cursor)
                cursor = parse_iso(entry["end"])
            self.assertEqual(cursor, self.now)
        # One shared endpoint is counted in both leaves but stored once.
        self.assertEqual(sum(entry["count"] for entry in result.coverage if entry["bucket"] == "author"), 1102)
        self.assertEqual(self.heartbeat()["coverage"], result.coverage)

    def test_fractional_timestamp_between_boundary_seconds_is_not_lost(self):
        start = self.now - timedelta(seconds=4)
        nodes = [{**node(number), "updatedAt": iso(start if number < 550 else self.now)}
                 for number in range(1100)]
        fractional = {**node(1100), "updatedAt": (start + timedelta(seconds=2.5)).isoformat()}
        result = sync(self.connection, now=self.now, since=start,
                      runner=WindowedGh([*nodes, fractional]))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.written, 1101)
        self.assertIn(fractional["url"], self.shadow())

    def test_a_late_page_error_preserves_observed_rows_but_holds_the_cursor(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            if argv == ["gh", "auth", "status"]:
                return 0, "", ""
            if any(value.startswith("after=") for value in argv):
                return 1, "", "fixture second-page failure"
            block = {"issueCount": 2, "nodes": [node(11)],
                     "pageInfo": {"hasNextPage": True, "endCursor": "next"}}
            return 0, json.dumps({"data": {"search": block}}), ""

        result = sync(self.connection, now=self.now, runner=runner)
        self.assertFalse(result.ok)
        self.assertEqual(result.written, 1)
        self.assertIn(node(11)["url"], self.shadow())
        self.assertIn("second-page failure", result.reason)
        self.assertIsNone(read_watermark(self.connection))

    def test_a_result_outside_the_frozen_interval_refuses_coverage(self):
        result = sync(self.connection, now=self.now, since=self.now, runner=Gh(nodes=[node(11)]))
        self.assertFalse(result.ok)
        self.assertIn("outside the requested interval", result.reason)
        self.assertIsNone(read_watermark(self.connection))

    def test_same_second_saturation_holds_cursor_and_marks_heartbeat_failed(self):
        previous = iso(self.now - timedelta(days=1))
        write_watermark(self.connection, TRACKER, previous, "earlier")
        nodes = [{**node(number), "updatedAt": iso(self.now)} for number in range(1001)]
        result = sync(self.connection, now=self.now, since=self.now, runner=WindowedGh(nodes))
        self.assertFalse(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(result.truncated, ["author"])
        self.assertIn("within second", result.reason)
        self.assertEqual(read_watermark(self.connection), previous)
        self.assertGreater(result.written, 0)
        self.assertFalse(self.heartbeat()["ok"])
        self.assertEqual(self.heartbeat()["truncated"], ["author"])

    def test_request_limit_preserves_partial_rows_without_advancing(self):
        runner = WindowedGh([{**node(number), "updatedAt": iso(self.now)} for number in range(1001)])
        result = sync(self.connection, now=self.now, runner=runner, max_requests=4)
        self.assertFalse(result.ok)
        self.assertIn("request limit exhausted", result.reason)
        self.assertEqual(result.requests, 4)
        self.assertEqual(len(runner.calls), 5)
        self.assertGreater(result.written, 0)
        self.assertIsNone(read_watermark(self.connection))

    def test_time_limit_stops_before_another_external_request(self):
        clock = [0.0]
        calls = []

        def runner(argv):
            calls.append(argv)
            if argv == ["gh", "auth", "status"]:
                return 0, "", ""
            clock[0] = 2.0
            return Gh(nodes=[])(argv)

        with patch("sd_db.shadow_sync.time.monotonic", side_effect=lambda: clock[0]):
            result = sync(self.connection, now=self.now, runner=runner, max_seconds=1)
        self.assertFalse(result.ok)
        self.assertIn("time limit exhausted", result.reason)
        self.assertEqual(result.requests, 1)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(read_watermark(self.connection))

    def test_historical_recovery_does_not_lower_a_later_cursor(self):
        later = iso(self.now + timedelta(days=1))
        write_watermark(self.connection, TRACKER, later, "earlier")
        result = sync(self.connection, now=self.now, since=self.now - timedelta(days=2), runner=Gh())
        self.assertTrue(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(read_watermark(self.connection), later)
        self.assertEqual(result.window_end, iso(self.now))

    def test_complete_recovery_cannot_skip_an_uncovered_prefix(self):
        previous = iso(self.now - timedelta(days=8))
        write_watermark(self.connection, TRACKER, previous, "earlier")
        result = sync(self.connection, now=self.now, since=self.now - timedelta(days=1), runner=Gh())
        self.assertTrue(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(read_watermark(self.connection), previous)

    def test_overlapping_collector_cannot_replace_a_newer_completed_cursor(self):
        other = connect(Path(self.tmp.name) / "sd.db")
        self.addCleanup(other.close)
        newer = self.now + timedelta(hours=1)
        completed = []

        # `_sync`, under the lock `sync` takes: the cursor recheck still guards a collect the lock does not cover.
        def runner(argv):
            if not completed:
                completed.append(_sync(other, now=newer, runner=Gh()))
            return Gh()(argv)

        result = _sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(completed[0].ok)
        self.assertTrue(completed[0].watermark_moved)
        self.assertTrue(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(read_watermark(self.connection), iso(newer))

    def test_historical_attempt_health_uses_actual_time_and_keeps_coverage_separate(self):
        recent = self.now + timedelta(days=3)
        with patch("sd_db.shadow_sync.time.time", return_value=recent.timestamp()):
            sync(self.connection, now=recent, runner=Gh())
        attempt = recent + timedelta(minutes=1)
        with patch("sd_db.shadow_sync.time.time", return_value=attempt.timestamp()):
            result = sync(self.connection, now=self.now, since=self.now - timedelta(days=1),
                          runner=Gh(fail="historical recovery failed"))
        self.assertFalse(result.ok)
        health = tracker_freshness(self.connection, now=attempt)
        self.assertEqual(health["state"], "degraded")
        self.assertEqual(parse_iso(health["last_attempt_at"]), attempt)
        self.assertEqual(health["last_success_at"], iso(recent))
        self.assertIn("historical recovery failed", health["reason"])
        self.assertEqual(self.heartbeat()["window_end"], iso(self.now))
        attempt += timedelta(minutes=1)
        with patch("sd_db.shadow_sync.time.time", return_value=attempt.timestamp()):
            result = sync(self.connection, now=self.now, since=self.now - timedelta(days=1), runner=Gh())
        self.assertTrue(result.ok)
        self.assertFalse(result.watermark_moved)
        health = tracker_freshness(self.connection, now=attempt)
        self.assertEqual(health["state"], "fresh")
        self.assertEqual(parse_iso(health["last_attempt_at"]), attempt)
        self.assertEqual(health["last_success_at"], iso(recent))
        self.assertEqual(self.heartbeat()["window_end"], iso(self.now))

    def test_changed_parent_count_during_splitting_refuses_coverage(self):
        start = self.now - timedelta(seconds=2)
        base = WindowedGh([{**node(number), "updatedAt": iso(start if number < 550 else self.now)}
                           for number in range(1100)])

        def runner(argv):
            code, out, error = base(argv)
            if argv == ["gh", "auth", "status"]:
                return code, out, error
            query = next(value[2:] for value in argv if value.startswith("q="))
            if f"updated:{iso(start)}..{iso(self.now)}" in query:
                payload = json.loads(out)
                payload["data"]["search"]["issueCount"] += 1
                out = json.dumps(payload)
            return code, out, error

        result = sync(self.connection, now=self.now, since=start, runner=runner)
        self.assertFalse(result.ok)
        self.assertIn("count changed while splitting", result.reason)
        self.assertIn("author", result.truncated)
        self.assertIsNone(read_watermark(self.connection))

    def test_timestamp_offset_overflow_is_invalid(self):
        for value in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"):
            with self.subTest(value=value):
                self.assertIsNone(parse_iso(value))
                result = sync(self.connection, now=self.now,
                              runner=Gh(nodes=[{**node(1), "updatedAt": value}]))
                self.assertFalse(result.ok)
                self.assertIsNone(read_watermark(self.connection))

    def test_invalid_bounds_and_limits_refuse_before_any_call_or_write(self):
        before = self.connection.total_changes
        for options in ({"since": self.now + timedelta(seconds=1)}, {"since": datetime(2026, 1, 1)},
                        {"max_requests": 0}, {"max_requests": True}, {"max_requests": 1.5},
                        {"max_seconds": 0}, {"max_seconds": float("nan")}, {"max_seconds": float("inf")}):
            with self.subTest(options=options):
                runner = Gh()
                with self.assertRaises(ValueError):
                    sync(self.connection, now=self.now, runner=runner, **options)
                self.assertEqual(runner.calls, [])
                self.assertEqual(self.connection.total_changes, before)


class TheLock(SyncCase):
    """One collect at a time per database, whichever caller starts first (sd:2207 review)."""

    def other(self):
        other = connect(Path(self.tmp.name) / "sd.db")
        self.addCleanup(other.close)
        return other

    def test_a_sync_while_another_caller_holds_the_lock_is_refused_before_it_collects(self):
        gh = Gh()
        with sync_lock(self.other()):
            with self.assertRaisesRegex(SyncBusy, "already running"):
                sync(self.connection, now=self.now, runner=gh)
        self.assertEqual(gh.calls, [])
        self.assertIsNone(read_watermark(self.connection))

    def test_a_second_caller_during_a_sync_is_refused_and_the_first_completes(self):
        other, refused = self.other(), []

        def runner(argv):
            if not refused:
                with self.assertRaises(SyncBusy) as busy:
                    sync(other, now=self.now + timedelta(hours=1), runner=Gh())
                refused.append(str(busy.exception))
            return Gh()(argv)

        result = sync(self.connection, now=self.now, runner=runner)
        self.assertIn("already running", refused[0])
        self.assertTrue(result.ok)
        self.assertEqual(read_watermark(self.connection), iso(self.now))

    def test_the_lock_is_free_again_after_a_sync_ends_well_or_badly(self):
        self.assertFalse(sync(self.connection, now=self.now, runner=Gh(fail="boom")).ok)
        self.assertTrue(sync(self.other(), now=self.now, runner=Gh()).ok)
        with sync_lock(self.connection):
            pass

    def test_a_satellite_refuses_before_it_locks_or_collects(self):
        from sd_db.remote import HubOnly

        gh = Gh()
        with patch("sd_db.database.served_by", return_value="hub.example.test:8770"):
            with self.assertRaises(HubOnly):
                sync(self.connection, now=self.now, runner=gh)
        self.assertEqual(gh.calls, [])
        self.assertFalse((Path(self.tmp.name) / "operation-locks").exists())

    def test_an_in_memory_database_takes_no_lock(self):
        import sqlite3

        memory = sqlite3.connect(":memory:")
        self.addCleanup(memory.close)
        with patch("sd_db.runner_journal.lock") as lock, sync_lock(memory):
            pass
        lock.assert_not_called()


class TheTrackerDispatch(SyncCase):
    """`tracker` chooses the collector, and each tracker keeps its own cursor.

    The Jira transport is the seam `shadow_jira` already takes, so nothing
    here opens a socket; the environment is replaced whole, so the operator's
    own `JIRA_*` values never reach a test.
    """

    def environment(self, *, without=()):
        values = {**os.environ, **JIRA_ENV}
        for name in ("JIRA_JQL", *without):
            values.pop(name, None)
        return patch.dict(os.environ, values, clear=True)

    def jira_heartbeats(self):
        return [json.loads(row["body"]) for row in self.connection.execute(
            "SELECT body FROM state WHERE kind = 'heartbeat' AND key = 'tracker-sync:jira' ORDER BY id"
        )]

    def watermark_keys(self):
        return [row["key"] for row in self.connection.execute(
            "SELECT key FROM state WHERE kind = 'watermark' ORDER BY id"
        )]

    def test_each_tracker_moves_only_its_own_cursor(self):
        """Seeded, not empty: on an empty database a cross-tracker overwrite
        would leave the other cursor non-`None` and pass."""
        seeded = "2026-09-01T00:00:00Z"
        write_watermark(self.connection, "github", seeded, "earlier")
        transport = jira_transport([{"issues": [jira_issue("ABC-1")], "isLast": True}])
        with self.environment():
            result = sync(self.connection, tracker="jira", now=self.now, runner=transport)
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.configured)
        self.assertTrue(result.watermark_moved)
        self.assertEqual(result.written, 1)
        self.assertEqual({row["tracker"] for row in self.shadow().values()}, {"jira"})
        self.assertEqual(self.watermark_keys(), ["github", "jira"])
        self.assertEqual(read_watermark(self.connection, "jira"), iso(self.now))
        self.assertEqual(read_watermark(self.connection, "github"), seeded)
        self.assertTrue(self.jira_heartbeats()[-1]["ok"])

        later = self.now + timedelta(hours=1)
        result = sync(self.connection, now=later, runner=Gh(nodes=[node(11)]))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(read_watermark(self.connection, "github"), iso(later))
        self.assertEqual(read_watermark(self.connection, "jira"), iso(self.now))

    def test_a_custom_jql_stores_its_rows_and_holds_the_cursor(self):
        """pack sd:5376: `JIRA_JQL` has no window, so its run covers none.

        Moving the cursor would let a later default sync, once `JIRA_JQL` is
        unset, resume from it and skip every ticket touched in between. Both
        the first run and a run over an existing cursor must hold it.
        """
        seeded = "2026-09-01T00:00:00Z"
        for previous in (None, seeded):
            with self.subTest(previous=previous):
                self.connection.execute("DELETE FROM state")
                self.connection.execute("DELETE FROM shadow")
                if previous:
                    write_watermark(self.connection, "jira", previous, "earlier")
                transport = jira_transport([{"issues": [jira_issue("ABC-1")], "isLast": True}])
                with self.environment(), patch.dict(os.environ, {"JIRA_JQL": "project = ABC"}):
                    result = sync(self.connection, tracker="jira", now=self.now, runner=transport)
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(result.written, 1)
                self.assertFalse(result.watermark_moved)
                self.assertEqual(read_watermark(self.connection, "jira"), previous)
                self.assertEqual(result.window_start, "")
                heartbeat = self.jira_heartbeats()[-1]
                self.assertTrue(heartbeat["ok"])
                self.assertEqual((heartbeat["window_start"], heartbeat["windowed"]), ("", False))
                self.assertEqual(tracker_freshness(self.connection, "jira", now=self.now)["state"], "fresh")

    def test_an_unconfigured_jira_is_not_collected_and_writes_only_its_heartbeat(self):
        transport = jira_transport([{"issues": [jira_issue("ABC-1")], "isLast": True}])
        with self.environment(without=("JIRA_BASE_URL",)):
            result = sync(self.connection, tracker="jira", now=self.now, runner=transport)
        self.assertFalse(result.ok)
        self.assertFalse(result.configured)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(result.reason, "JIRA_BASE_URL not set")
        self.assertEqual(transport.seen["calls"], 0, "a request was made with no host configured")
        self.assertEqual(self.shadow(), {})
        self.assertEqual(self.watermark_keys(), [])
        heartbeats = self.jira_heartbeats()
        self.assertEqual(len(heartbeats), 1)
        self.assertFalse(heartbeats[0]["ok"])
        self.assertEqual(heartbeats[0]["reason"], "JIRA_BASE_URL not set")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM state").fetchone()[0], 1)

    def test_a_secret_in_the_jira_base_url_reaches_no_heartbeat(self):
        """Before sd:771 `http.client.InvalidURL` and `urllib.parse`'s
        `ValueError` left `sync` quoting it, with no heartbeat written."""
        cases = {
            f"https://user:{SECRET}@jira.example.invalid": "JIRA_BASE_URL must not carry a user name or password",
            **{base: "JIRA_BASE_URL is not a URL urllib can split" for base in UNSPLITTABLE},
            **{base: NOT_A_HOST_REASON for base in NOT_A_HOST},
            **{base: NON_ASCII_HOST_REASON for base in NON_ASCII_HOST},
        }
        for count, (base, reason) in enumerate(cases.items(), start=1):
            with self.subTest(base=base), self.environment(), no_socket() as connect, patch.dict(
                    os.environ, {"JIRA_BASE_URL": base}):
                result = sync(self.connection, tracker="jira", now=self.now, runner=None)
                self.assertFalse(result.ok)
                self.assertFalse(result.configured)
                self.assertEqual(result.reason, reason)
                self.assertEqual((result.requests, connect.call_count), (0, 0))
                heartbeats = self.jira_heartbeats()
                self.assertEqual(len(heartbeats), count)
                self.assertEqual((heartbeats[-1]["ok"], heartbeats[-1]["requests"]), (False, 0))
                self.assertNotIn(SECRET, json.dumps(heartbeats))

    def test_an_answering_resolver_or_an_echoing_proxy_gets_no_secret_from_the_host(self):
        """Before sd:784 `[v1.user:password]` and its kin reached the resolver.

        Here the resolver answers every name with a loopback server that hangs
        up, and then the only proxy is a loopback one that echoes its CONNECT
        target in its reason phrase, which the heartbeat kept. Nothing leaves
        127.0.0.1 in either mode.
        """
        asked, tunnels = [], []
        real = socket.getaddrinfo

        class Loopback(socketserver.TCPServer):
            allow_reuse_address = True

            def __init__(self, handle):
                super().__init__(("127.0.0.1", 0), type("Handler", (socketserver.StreamRequestHandler,),
                                                        {"handle": handle}))
                threading.Thread(target=self.serve_forever, daemon=True).start()

        def hang_up(inner):
            pass

        def echo_target(inner):
            line = inner.rfile.readline().decode("latin-1").strip()
            tunnels.append(line)
            while inner.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            target = line.split(" ")[1] if " " in line else line
            inner.wfile.write(f"HTTP/1.1 502 cannot reach {target}\r\n\r\n".encode("latin-1"))

        closer, proxy = Loopback(hang_up), Loopback(echo_target)
        for server in (closer, proxy):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

        def resolver(host, port, *rest, **named):
            name = host.decode() if isinstance(host, bytes) else host
            if name in ("127.0.0.1", "::1", "localhost"):
                return real(host, port, *rest, **named)
            asked.append(name)
            return real("127.0.0.1", closer.server_address[1], *rest, **named)

        proxied = {"https_proxy": f"http://127.0.0.1:{proxy.server_address[1]}", "no_proxy": "", "NO_PROXY": ""}
        for mode, extra in (("resolver", {}), ("proxy", proxied)):
            for base in NOT_A_HOST:
                with self.subTest(mode=mode, base=base), without_jira_environment(), patch.dict(
                        os.environ, {**JIRA_ENV, "JIRA_BASE_URL": base, **extra}), patch(
                        "socket.getaddrinfo", side_effect=resolver):
                    del asked[:], tunnels[:]
                    result = sync(self.connection, tracker="jira", now=self.now, runner=None)
                    self.assertFalse(result.ok)
                    self.assertNotIn(SECRET, json.dumps(self.jira_heartbeats()[-1]))
                    self.assertNotIn(SECRET, " ".join(asked + tunnels))

    def test_a_broken_jira_answer_writes_the_heartbeat_and_its_count(self):
        """The real transport against loopback servers; proxies are removed."""
        from sd_db import shadow_jira

        for name, reply in BROKEN_ANSWERS.items():
            with self.subTest(answer=name):
                server = RawServer(reply)
                self.addCleanup(server.close)
                config = {**shadow_jira.settings(JIRA_ENV), "base": server.url}
                with without_jira_environment(), patch("sd_db.shadow_jira.settings", return_value=config):
                    result = sync(self.connection, tracker="jira", now=self.now, runner=None)
                self.assertFalse(result.ok)
                self.assertTrue(result.configured)
                self.assertFalse(result.watermark_moved)
                self.assertEqual((result.requests, server.connections), (1, 1))
                heartbeat = self.jira_heartbeats()[-1]
                self.assertEqual(heartbeat["reason"], f"the request to Jira failed: {name}")
                self.assertEqual((heartbeat["ok"], heartbeat["requests"]), (False, 1))
                self.assertNotIn(SECRET, json.dumps(heartbeat))

    def test_a_truncated_jira_walk_stores_its_rows_and_holds_the_cursor(self):
        transport = jira_transport([{"issues": [jira_issue("ABC-1")], "isLast": False}])
        with self.environment():
            result = sync(self.connection, tracker="jira", now=self.now, runner=transport)
        self.assertFalse(result.ok)
        self.assertTrue(result.configured)
        self.assertEqual(result.truncated, ["jql"])
        self.assertIn("https://example.invalid/browse/ABC-1", self.shadow())
        self.assertFalse(result.watermark_moved)
        self.assertIsNone(read_watermark(self.connection, "jira"))
        self.assertEqual(self.jira_heartbeats()[-1]["truncated"], ["jql"])

    def test_a_tracker_it_does_not_know_is_refused_before_any_call_or_write(self):
        """Before this, an unknown name ran GitHub's buckets under that key."""
        runner = Gh()
        before = self.connection.total_changes
        with self.assertRaises(ValueError) as raised:
            sync(self.connection, tracker="nope", now=self.now, runner=runner)
        self.assertIn("jira", str(raised.exception))
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.connection.total_changes, before)

    def test_a_recovery_window_is_refused_for_jira(self):
        """`since` is GitHub's; accepting it silently would promise a re-walk."""
        transport = jira_transport([])
        before = self.connection.total_changes
        with self.environment(), self.assertRaises(ValueError):
            sync(self.connection, tracker="jira", now=self.now, runner=transport,
                 since=self.now - timedelta(days=1))
        self.assertEqual(transport.seen["calls"], 0)
        self.assertEqual(self.connection.total_changes, before)


class BucketGh(Gh):
    """A `gh` that answers each bucket's qualifier with its own nodes, or fails it."""

    def __init__(self, buckets, *, failing=()):
        super().__init__()
        self.buckets = buckets
        self.failing = set(failing)

    def __call__(self, argv):
        if argv[:2] == ["gh", "auth"]:
            return super().__call__(argv)
        self.calls.append(argv)
        query = self.queries[-1]
        qualifier = query.split(" ", 1)[0]
        if qualifier in self.failing:
            return 1, "", "the API said no"
        nodes = self.buckets.get(qualifier, [])
        payload = {"data": {"search": {"issueCount": len(nodes),
                                       "pageInfo": {"hasNextPage": False, "endCursor": None},
                                       "nodes": nodes}}}
        return 0, json.dumps(payload), ""


class TheReason(SyncCase):
    """sd:4636: each synced row stores why it is the operator's, so Today and
    Tasks can show the assigned and filed ones and hide the watched ones."""

    def reasons(self):
        return {row["url"].rsplit("/", 1)[1]: row["reason"] for row in self.shadow().values()}

    def environment(self):
        values = {**os.environ, **JIRA_ENV}
        values.pop("JIRA_JQL", None)
        return patch.dict(os.environ, values, clear=True)

    def test_github_rows_store_the_strongest_bucket(self):
        runner = BucketGh({"assignee:@me": [node(1), node(4)], "author:@me": [node(2), node(4)],
                           "mentions:@me": [node(3)], "review-requested:@me": [node(5)]})
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.reasons(), {"1": "assigned", "2": "filed", "3": "watched", "4": "assigned",
                                          "5": "watched"})

    def test_a_later_collect_moves_the_reason_with_the_row(self):
        sync(self.connection, now=self.now, runner=BucketGh({"assignee:@me": [node(1)]}))
        later = self.now + timedelta(hours=1)
        moved = {**node(1), "updatedAt": iso(later)}
        result = sync(self.connection, now=later, runner=BucketGh({"mentions:@me": [moved]}))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.reasons(), {"1": "watched"})

    def test_a_failed_bucket_never_lowers_a_reason(self):
        """A failed `assignee:` search is unknown, not "not assigned": a row seen
        only by another bucket keeps what it had, and a new one stores none."""
        sync(self.connection, now=self.now, runner=BucketGh({"assignee:@me": [node(1)]}))
        later = self.now + timedelta(hours=1)
        moved = {**node(1), "updatedAt": iso(later)}
        fresh = {**node(2), "updatedAt": iso(later)}
        result = sync(self.connection, now=later,
                      runner=BucketGh({"mentions:@me": [moved, fresh]}, failing={"assignee:@me"}))
        self.assertFalse(result.ok)
        self.assertEqual(self.reasons(), {"1": "assigned", "2": None})

    def test_a_failed_author_bucket_leaves_a_mention_unknown_but_an_assignment_known(self):
        later = self.now
        result = sync(self.connection, now=later,
                      runner=BucketGh({"assignee:@me": [node(1)], "mentions:@me": [node(2)]},
                                      failing={"author:@me"}))
        self.assertFalse(result.ok)
        self.assertEqual(self.reasons(), {"1": "assigned", "2": None})

    def test_jira_rows_store_assigned_filed_or_watched(self):
        me = "acct-1"
        mine = jira_issue("ABC-1")
        filed = jira_issue("ABC-2", assignee=None, reporter={"accountId": me, "displayName": "Alex"})
        watched = jira_issue("ABC-3", assignee={"accountId": "acct-other"}, watches={"isWatching": True})
        transport = jira_transport([{"issues": [mine, filed, watched], "isLast": True}], me=me)
        with self.environment():
            result = sync(self.connection, tracker="jira", now=self.now, runner=transport)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.reasons(), {"ABC-1": "assigned", "ABC-2": "filed", "ABC-3": "watched"})

    def test_jira_rows_store_when_jira_last_updated_them_in_utc(self):
        """pack sd:5357: `updated` is stored as `iso` writes it; one that does not read keeps the stored time."""
        dated = jira_issue("ABC-1", updated="2026-10-05T10:12:33.123-0600")
        transport = jira_transport([{"issues": [dated], "isLast": True}])
        with self.environment():
            self.assertTrue(sync(self.connection, tracker="jira", now=self.now, runner=transport).ok)
        [row] = self.shadow().values()
        self.assertEqual(row["updated_at"], "2026-10-05T16:12:33Z")
        transport = jira_transport([{"issues": [jira_issue("ABC-1", updated="not a time")], "isLast": True}])
        with self.environment():
            self.assertTrue(sync(self.connection, tracker="jira", now=self.now + timedelta(hours=1),
                                 runner=transport).ok)
        [row] = self.shadow().values()
        self.assertEqual(row["updated_at"], "2026-10-05T16:12:33Z")

    def test_the_backfill_walk_is_the_custom_jql_and_dates_the_rows_it_reads(self):
        """pack sd:5357 backfill: `JIRA_JQL` replaces the window, so one run re-reads older rows."""
        jql = ("(assignee = currentUser() OR watcher = currentUser() OR reporter = currentUser()) "
               "AND updated >= -90d ORDER BY updated DESC")
        transport = jira_transport([{"issues": [jira_issue("ABC-9", updated="2026-08-28T09:00:00.000+0000")],
                                     "isLast": True}])
        with self.environment(), patch.dict(os.environ, {"JIRA_JQL": jql}):
            self.assertTrue(sync(self.connection, tracker="jira", now=self.now, runner=transport).ok)
        self.assertEqual(transport.seen["jql"], [jql])
        [row] = self.shadow().values()
        self.assertEqual(row["updated_at"], "2026-08-28T09:00:00Z")

    def test_a_writer_that_names_no_reason_keeps_the_stored_one(self):
        """The importers (`sources.issues`, `sources.index_cache`) call
        `upsert_shadow` without a reason; a refresh from them is not a
        collect and must not erase one."""
        from sd_db.writes import upsert_shadow

        sync(self.connection, now=self.now, runner=BucketGh({"author:@me": [node(1)]}))
        upsert_shadow(self.connection, tracker=TRACKER, url=node(1)["url"], title="imported")
        self.assertEqual(self.reasons(), {"1": "filed"})


class TheFiledColumns(SyncCase):
    """pack sd:5496: each row stores when the tracker created it, whether the operator filed it apart from `reason`,
    Jira's status name and the assignee, so the dashboard can list what the operator filed this year. A value a
    collect cannot read is None and keeps the stored one."""

    def environment(self, **extra):
        values = {**os.environ, **JIRA_ENV, **extra}
        if "JIRA_JQL" not in extra:
            values.pop("JIRA_JQL", None)
        return patch.dict(os.environ, values, clear=True)

    def fields(self):
        return {row["url"].rsplit("/", 1)[1]: (row["created_at"], row["filed"], row["status"], row["assignee"])
                for row in self.shadow().values()}

    @staticmethod
    def gh(number, *, created="2026-02-03T04:05:06Z", assignees=("octocat",)):
        return {**node(number), "createdAt": created, "assignees": {"nodes": [{"login": who} for who in assignees]}}

    def test_github_rows_store_created_filed_apart_from_reason_and_assignees(self):
        runner = BucketGh({"assignee:@me": [self.gh(1)], "author:@me": [self.gh(1), self.gh(2, assignees=())],
                           "mentions:@me": [self.gh(3, assignees=("octocat", "hubot"))]})
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.fields(), {"1": ("2026-02-03T04:05:06Z", 1, None, "octocat"),
                                         "2": ("2026-02-03T04:05:06Z", 1, None, ""),
                                         "3": ("2026-02-03T04:05:06Z", 0, None, "octocat, hubot")})
        self.assertEqual(self.shadow()[self.gh(1)["url"]]["reason"], "assigned")
        query = next(argv[index + 1] for argv in runner.calls for index, value in enumerate(argv)
                     if value == "-f" and argv[index + 1].startswith("query="))
        self.assertEqual(query.count("createdAt"), 2)
        self.assertEqual(query.count("assignees(first: 10) { nodes { login } }"), 2)

    def test_a_failed_author_bucket_leaves_filed_unknown_and_keeps_the_stored_answer(self):
        sync(self.connection, now=self.now, runner=BucketGh({"author:@me": [self.gh(1)]}))
        later = self.now + timedelta(hours=1)
        moved = {**self.gh(1), "updatedAt": iso(later)}
        fresh = {**self.gh(2), "updatedAt": iso(later)}
        result = sync(self.connection, now=later,
                      runner=BucketGh({"mentions:@me": [moved, fresh]}, failing={"author:@me"}))
        self.assertFalse(result.ok)
        self.assertEqual({key: value[1] for key, value in self.fields().items()}, {"1": 1, "2": None})

    def test_a_github_value_that_does_not_read_keeps_the_stored_one(self):
        sync(self.connection, now=self.now, runner=BucketGh({"author:@me": [self.gh(1)]}))
        later = self.now + timedelta(hours=1)
        unread = {key: value for key, value in self.gh(1).items() if key != "assignees"} | {
            "createdAt": "not a time", "updatedAt": iso(later)}
        self.assertTrue(sync(self.connection, now=later, runner=BucketGh({"author:@me": [unread]})).ok)
        self.assertEqual(self.fields(), {"1": ("2026-02-03T04:05:06Z", 1, None, "octocat")})

    def test_jira_rows_store_created_filed_status_and_assignee(self):
        me = "acct-1"
        both = jira_issue("ABC-1", created="2026-01-05T10:12:33.123-0600",
                          status={"name": "In Review", "statusCategory": {"name": "In Progress"}},
                          reporter={"accountId": me, "displayName": "Alex"})
        other = jira_issue("ABC-2", assignee=None, watches={"isWatching": True}, created="2025-12-30T00:00:00.000+0000")
        transport = jira_transport([{"issues": [both, other], "isLast": True}], me=me)
        with self.environment():
            result = sync(self.connection, tracker="jira", now=self.now, runner=transport)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.fields(), {"ABC-1": ("2026-01-05T16:12:33Z", 1, "In Review", "Alex"),
                                         "ABC-2": ("2025-12-30T00:00:00Z", 0, "Open", "")})
        # Filed and assigned: `reason` names the stronger, `filed` still says the operator filed it.
        self.assertEqual(self.shadow()["https://example.invalid/browse/ABC-1"]["reason"], "assigned")

    def test_a_jira_value_that_does_not_read_keeps_the_stored_one(self):
        transport = jira_transport([{"issues": [jira_issue("ABC-1", created="2026-01-05T00:00:00.000+0000")],
                                     "isLast": True}])
        with self.environment():
            self.assertTrue(sync(self.connection, tracker="jira", now=self.now, runner=transport).ok)
        bare = jira_issue("ABC-1", created="not a time")
        del bare["fields"]["status"]["name"]
        transport = jira_transport([{"issues": [bare], "isLast": True}])
        with self.environment():
            self.assertTrue(sync(self.connection, tracker="jira", now=self.now + timedelta(hours=1), runner=transport).ok)
        self.assertEqual(self.fields(), {"ABC-1": ("2026-01-05T00:00:00Z", 0, "Open", "Alex")})

    def test_a_jira_reporter_or_assignee_that_cannot_be_read_keeps_the_stored_answer(self):
        """Codex review: a reporter with no id or email is not "someone else", and an assignee with no name is not
        "unassigned"; both are unknown. An explicit null reporter or assignee is a known no."""
        filed = jira_issue("ABC-1", reporter={"accountId": "acct-1", "displayName": "Alex"})
        transport = jira_transport([{"issues": [filed], "isLast": True}])
        with self.environment():
            self.assertTrue(sync(self.connection, tracker="jira", now=self.now, runner=transport).ok)
        later = self.now + timedelta(hours=1)
        unread = jira_issue("ABC-1", reporter={"displayName": "Alex"}, assignee={"accountId": "acct-1"})
        transport = jira_transport([{"issues": [unread], "isLast": True}])
        with self.environment():
            self.assertTrue(sync(self.connection, tracker="jira", now=later, runner=transport).ok)
        self.assertEqual(self.fields()["ABC-1"][1:4:2], (1, "Alex"))
        nobody = jira_issue("ABC-1", reporter=None, assignee=None)
        transport = jira_transport([{"issues": [nobody], "isLast": True}])
        with self.environment():
            self.assertTrue(sync(self.connection, tracker="jira", now=later + timedelta(hours=1), runner=transport).ok)
        self.assertEqual(self.fields()["ABC-1"][1:4:2], (0, ""))

    def test_the_jira_search_asks_for_the_created_field(self):
        from sd_db import shadow_jira

        self.assertIn("created", shadow_jira.FIELDS)

    def test_the_jira_backfill_is_the_custom_jql_of_what_the_operator_filed_this_year(self):
        """pack sd:5496 backfill: one run with `JIRA_JQL` reads every ticket the operator filed this year, whatever its
        age; the cursor holds, so the nightly window is untouched."""
        jql = "reporter = currentUser() AND created >= startOfYear() ORDER BY created DESC"
        old = jira_issue("ABC-7", created="2026-01-02T09:00:00.000+0000", updated="2026-01-03T09:00:00.000+0000",
                         reporter={"accountId": "acct-1", "displayName": "Alex"}, assignee=None)
        transport = jira_transport([{"issues": [old], "isLast": True}])
        with self.environment(JIRA_JQL=jql):
            result = sync(self.connection, tracker="jira", now=self.now, runner=transport)
        self.assertTrue(result.ok, result.reason)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(transport.seen["jql"], [jql])
        self.assertEqual(self.fields(), {"ABC-7": ("2026-01-02T09:00:00Z", 1, "Open", "")})

    def test_the_github_backfill_is_a_recovery_window_from_the_start_of_the_year(self):
        """pack sd:5496 backfill: an issue created this year was updated this year too, so `--since` the first of
        January reads every one the operator filed, through the same four buckets."""
        runner = BucketGh({"author:@me": [self.gh(4, created="2026-01-02T00:00:00Z")]})
        result = sync(self.connection, now=self.now, runner=runner, since=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(all(" updated:2026-01-01T00:00:00Z.." in query for query in runner.queries))
        self.assertEqual(self.fields(), {"4": ("2026-01-02T00:00:00Z", 1, None, "octocat")})

    def test_an_importer_that_names_none_keeps_every_stored_value(self):
        from sd_db.writes import upsert_shadow

        sync(self.connection, now=self.now, runner=BucketGh({"author:@me": [self.gh(1)]}))
        upsert_shadow(self.connection, tracker=TRACKER, url=self.gh(1)["url"], title="imported")
        self.assertEqual(self.fields(), {"1": ("2026-02-03T04:05:06Z", 1, None, "octocat")})

    def test_tracker_items_carries_the_new_fields(self):
        from sd_db.progress import tracker_items

        sync(self.connection, now=self.now, runner=BucketGh({"author:@me": [self.gh(1)]}))
        [row] = tracker_items(self.connection, state=None)
        self.assertEqual({key: row[key] for key in ("created_at", "filed", "status", "assignee")},
                         {"created_at": "2026-02-03T04:05:06Z", "filed": 1, "status": None, "assignee": "octocat"})


class TheSettleDates(SyncCase):
    """sd:5303. Each row keeps when GitHub merged or closed it, read from the same search page."""

    def dates(self):
        return {row["url"].rsplit("/", 1)[1]: (row["state"], row["merged_at"], row["closed_at"])
                for row in self.shadow().values()}

    def test_a_merged_pull_and_a_closed_issue_keep_their_dates_at_no_extra_request(self):
        pull = {**node(2, kind="PullRequest", state="MERGED",
                       url="https://github.com/example-org/benchmark/pull/2"),
                "mergedAt": "2026-09-05T09:00:00Z", "closedAt": "2026-09-05T09:00:00Z"}
        closed = {**node(3, state="CLOSED"), "closedAt": "2026-09-05T08:00:00+00:00"}
        # The assigned bucket: an authored, asked or mentioning pull starts a detail read (sd:8142).
        runner = QueryGh(lambda query: [{**node(1), "closedAt": None}, pull, closed] if "assignee:@me" in query else [])
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.dates(), {"1": ("open", None, None),
                                        "2": ("merged", "2026-09-05T09:00:00Z", "2026-09-05T09:00:00Z"),
                                        "3": ("closed", None, "2026-09-05T08:00:00Z")})
        self.assertEqual(len(runner.queries), 4)
        query = next(value for argv in runner.calls for value in argv if value.startswith("query="))
        self.assertIn("closedAt", query)
        self.assertIn("mergedAt", query)

    def test_a_reopened_issue_loses_its_close_date(self):
        sync(self.connection, now=self.now, runner=Gh(nodes=[{**node(1, state="CLOSED"), "closedAt": "2026-09-05T08:00:00Z"}]))
        later = self.now + timedelta(hours=1)
        result = sync(self.connection, now=later,
                      runner=Gh(nodes=[{**node(1), "closedAt": None, "updatedAt": iso(later)}]))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.dates(), {"1": ("open", None, None)})

    def test_an_unreadable_or_absent_date_is_unknown_and_keeps_the_stored_one(self):
        """A date that does not parse is not "never closed"; nor is a writer that names none."""
        from sd_db.writes import upsert_shadow

        sync(self.connection, now=self.now, runner=Gh(nodes=[{**node(1, state="CLOSED"), "closedAt": "2026-09-05T08:00:00Z"}]))
        unreadable: tuple[object, ...] = ("yesterday", "", 7, None, [], "2026-09-05T08:00:00", "9999-99-99T00:00:00Z")
        for value in unreadable:
            with self.subTest(value=value):
                again = {**node(1, state="CLOSED"), "closedAt": value, "updatedAt": iso(self.now)}
                result = sync(self.connection, now=self.now, runner=Gh(nodes=[again]))
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(self.dates(), {"1": ("closed", None, "2026-09-05T08:00:00Z")})
        # An importer names no state either, and the row takes that as it always has; the date stays.
        upsert_shadow(self.connection, tracker=TRACKER, url=node(1)["url"], title="imported")
        self.assertEqual(self.dates(), {"1": (None, None, "2026-09-05T08:00:00Z")})


class TheLinks(SyncCase):
    """sd:5315. A pull request's closing issues and branch, from the same search page; Jira keys joined, not fetched."""

    BASE = "https://github.com/example-org/benchmark"

    def pull(self, number, *, branch, title="a title", closes=()):
        return {**node(number, kind="PullRequest", url=f"{self.BASE}/pull/{number}", title=title),
                "headRefName": branch, "closingIssuesReferences": {"nodes": [{"url": url} for url in closes]}}

    def links(self):
        return {row["url"].rsplit("/", 1)[1]: (row["branch"], row["linked_issues"]) for row in self.shadow().values()}

    def test_three_pull_requests_and_their_links(self):
        from sd_db.shadow_sync import jira_links
        from sd_db.writes import upsert_shadow

        for key in ("LOG-12", "LOG-120", "OPS-7"):
            upsert_shadow(self.connection, tracker="jira", url=f"https://jira.example.test/browse/{key}",
                          kind="issue", title=f"ticket {key}", state="open")
        closes = (f"{self.BASE}/issues/1", "https://github.com/example-org/other/issues/2")
        pulls = [self.pull(10, branch="fix/two-issues", closes=closes),
                 self.pull(11, branch="feature/LOG-12-retry"),
                 self.pull(12, branch="ABC-9-nothing", title="Handle UTF-8 names")]
        # The assigned bucket: an authored, asked or mentioning pull starts a detail read (sd:8142).
        runner = QueryGh(lambda query: pulls if "assignee:@me" in query else [])
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(runner.queries), 4)
        query = next(value for argv in runner.calls for value in argv if value.startswith("query="))
        self.assertIn("closingIssuesReferences(first: 10) { nodes { url } }", query)
        self.assertIn("headRefName", query)
        rows = {key: value for key, value in self.links().items() if key.isdigit()}
        self.assertEqual(rows, {"10": ("fix/two-issues", json.dumps(list(closes))),
                                "11": ("feature/LOG-12-retry", "[]"), "12": ("ABC-9-nothing", "[]")})
        self.assertEqual(jira_links(self.connection, "a title", "fix/two-issues"), [])
        self.assertEqual(jira_links(self.connection, "a title", "feature/LOG-12-retry"),
                         [{"key": "LOG-12", "url": "https://jira.example.test/browse/LOG-12",
                           "title": "ticket LOG-12", "state": "open"}])
        self.assertEqual(jira_links(self.connection, "Handle UTF-8 names", "ABC-9-nothing"), [])

    def test_jira_rows_reads_any_number_of_keys(self):
        """Lifetime history names thousands of keys; SQLite's expression depth (1000) must not bound them."""
        from sd_db.shadow_sync import jira_rows
        from sd_db.writes import upsert_shadow

        upsert_shadow(self.connection, tracker="jira", url="https://jira.example.test/browse/LOG-2500",
                      kind="issue", title="ticket", state="open")
        found = jira_rows(self.connection, [f"LOG-{n}" for n in range(1, 3001)])
        self.assertEqual(list(found), ["LOG-2500"])

    def test_a_key_is_a_whole_key(self):
        """`LOG-1` is not `LOG-12`; a key inside a longer word or number is no key; each key once, in order."""
        from sd_db.shadow_sync import jira_keys

        self.assertEqual(jira_keys("LOG-12 and LOG-1", "x/LOG-12-y", None, 7), ["LOG-12", "LOG-1"])
        for text in ("log-12", "LOG-", "-12", "L-12", "LOG_12", "aLOG-12", "1LOG-12", "", "-" * 10000):
            with self.subTest(text=text[:20]):
                self.assertEqual(jira_keys(text), [])
        self.assertEqual(jira_keys("LOG-123"), ["LOG-123"])

    def test_an_unreadable_field_is_unknown_and_keeps_the_stored_value(self):
        later = self.now + timedelta(hours=1)
        first = self.pull(10, branch="fix/one", closes=(f"{self.BASE}/issues/1",))
        sync(self.connection, now=self.now, runner=QueryGh(lambda query: [first] if "assignee:@me" in query else []))
        broken: tuple[object, ...] = (None, [], {"nodes": None}, {"nodes": [{"url": ""}]}, {"nodes": [7]}, "x")
        for value in broken:
            with self.subTest(value=value):
                again = {**first, "closingIssuesReferences": value, "headRefName": None, "updatedAt": iso(later)}
                result = sync(self.connection, now=later,
                              runner=QueryGh(lambda query, again=again: [again] if "assignee:@me" in query else []))
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(self.links()["10"], ("fix/one", json.dumps([f"{self.BASE}/issues/1"])))

    def merge(self):
        return {row["url"].rsplit("/", 1)[1]: (row["review_decision"], row["checks"], row["mergeable"])
                for row in self.shadow().values()}

    def test_a_pull_requests_review_checks_and_mergeable_are_stored_and_an_unread_one_is_null(self):
        """pack sd:6711: the Ready to merge filter reads these; GitHub's null review or rollup is `none`, never unread."""
        rollup = {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}
        pulls = [{**self.pull(10, branch="a"), "reviewDecision": "APPROVED", "mergeable": "MERGEABLE", "commits": rollup},
                 {**self.pull(11, branch="b"), "reviewDecision": None, "mergeable": "CONFLICTING",
                  "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}},
                 self.pull(12, branch="c")]
        runner = QueryGh(lambda query: pulls if "assignee:@me" in query else [])
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        query = next(value for argv in runner.calls for value in argv if value.startswith("query="))
        self.assertIn("reviewDecision mergeable commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }", query)
        rows = {key: value for key, value in self.merge().items() if key.isdigit()}
        self.assertEqual(rows, {"10": ("approved", "success", "mergeable"), "11": ("none", "none", "conflicting"),
                                "12": (None, None, None)})

    def test_an_issue_row_has_no_branch_and_no_links(self):
        sync(self.connection, now=self.now, runner=QueryGh(lambda query: [node(1)] if "mentions:@me" in query else []))
        self.assertEqual(self.links(), {"1": (None, None)})


class QueryGh(Gh):
    """A `gh` that answers each search from `answer(query)`, or fails the ones `failing(query)` names."""

    def __init__(self, answer, *, failing=lambda query: False, count=None, read=None):
        super().__init__()
        self.answer = answer
        self.failing = failing
        self.counted = count
        self.read = read
        self.paths: list[str] = []

    def __call__(self, argv):
        if argv[:2] == ["gh", "auth"]:
            return super().__call__(argv)
        if argv[:3] == ["gh", "api", "--include"] and ("/issues/" in argv[-1] or "/pulls/" in argv[-1]):
            # A direct issue or pull read: `read(path)` answers `(status, payload)`, or None for a gh failure.
            self.paths.append(argv[-1])
            answer = self.read(argv[-1]) if self.read else None
            if answer is None:
                return 1, "", "gh: connection reset"
            return 0, f"HTTP/2.0 {answer[0]} X\nContent-Type: application/json\n\n{json.dumps(answer[1])}", ""
        self.calls.append(argv)
        query = self.queries[-1]
        if self.failing(query):
            return 1, "", "the API said no"
        nodes = self.answer(query)
        count = self.counted(query) if self.counted else None
        payload = {"data": {"search": {"issueCount": len(nodes) if count is None else count,
                                       "pageInfo": {"hasNextPage": False, "endCursor": None},
                                       "nodes": nodes}}}
        return 0, json.dumps(payload), ""


def sweep(query):
    return "is:open" in query and "updated:" not in query


def issue(number, *, state="open", url=None, **extra):
    """A direct issue read's 200 answer."""
    return 200, {"html_url": url or node(number)["url"], "number": number, "state": state, **extra}


class TheIntakeSearch(SyncCase):
    """sd:4880. A repository with `policy.issues` ask has every open issue synced,
    not only the operator's: an unwindowed sweep lists them, older than the
    watermark too, such as a repository switched on today, and a stored open
    issue a complete sweep did not list is closed."""

    REPO = "example-org/benchmark"

    def setUp(self):
        super().setUp()
        from sd_db.writes import upsert_repo

        upsert_repo(self.connection, "/srv/example/benchmark", remote=f"git@github.com:{self.REPO}.git")
        upsert_repo(self.connection, "/srv/example/other", remote="https://github.com/example-org/other")
        self.connection.execute("UPDATE repo SET declarations = '{\"policy\":{\"issues\":\"ask\"}}' WHERE path = '/srv/example/benchmark'")

    def reasons(self):
        return {row["url"].rsplit("/", 1)[1]: (row["state"], row["reason"]) for row in self.shadow().values()}

    def test_an_on_repository_adds_one_open_sweep(self):
        old = {**node(2), "updatedAt": "2025-01-01T00:00:00Z"}
        runner = QueryGh(lambda query: [old] if sweep(query) else [node(1)] if "mentions:@me" in query else [])
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(runner.queries), 5)
        self.assertEqual(runner.queries[-1], f"is:issue is:open repo:{self.REPO}")
        self.assertFalse(any("example-org/other" in query for query in runner.queries))
        self.assertEqual(self.reasons(), {"1": ("open", "watched"), "2": ("open", None)})
        self.assertTrue(result.watermark_moved)

    def test_an_issue_closed_since_the_last_sweep_closes_its_row(self):
        sync(self.connection, now=self.now, runner=QueryGh(lambda query: [node(1)] if sweep(query) else []))
        runner = QueryGh(lambda query: [], read=lambda path: issue(1, state="closed"))
        result = sync(self.connection, now=self.now + timedelta(hours=1), runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(runner.paths, ["repos/example-org/benchmark/issues/1"])
        self.assertEqual(self.reasons(), {"1": ("closed", None)})

    def test_a_closure_the_direct_read_finds_keeps_its_close_date(self):
        """sd:5303. The read that closes the row already carries GitHub's `closed_at`."""
        sync(self.connection, now=self.now, runner=QueryGh(lambda query: [node(1)] if sweep(query) else []))
        runner = QueryGh(lambda query: [], read=lambda path: issue(1, state="closed", closed_at="2026-09-06T12:30:00Z"))
        result = sync(self.connection, now=self.now + timedelta(hours=1), runner=runner)
        self.assertTrue(result.ok, result.reason)
        [row] = self.shadow().values()
        self.assertEqual((row["state"], row["closed_at"]), ("closed", "2026-09-06T12:30:00Z"))

    def test_an_open_issue_a_bucket_saw_but_the_sweep_missed_stays_open(self):
        """The search index can lag: what the window saw open this run is open."""
        result = sync(self.connection, now=self.now,
                      runner=QueryGh(lambda query: [node(1)] if "mentions:@me" in query else []))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.reasons(), {"1": ("open", "watched")})

    def test_a_row_only_the_sweep_saw_keeps_its_stored_reason(self):
        """The sweep reads outside the window the operator's buckets covered, so it
        cannot say an old issue is not the operator's: it names no reason."""
        sync(self.connection, now=self.now, runner=QueryGh(lambda query: [node(1)] if "author:@me" in query else []))
        later = self.now + timedelta(hours=1)
        result = sync(self.connection, now=later, runner=QueryGh(lambda query: [node(1)] if sweep(query) else []))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.reasons(), {"1": ("open", "filed")})

    def test_a_failed_or_capped_sweep_is_reported_and_holds_no_cursor(self):
        """The sweep is not windowed, so the next run repeats it whole: its
        failure or cap cannot hold the cursor, or one large repository would
        freeze every bucket. It is unknown, so it is named, and closes nothing."""
        for name, runner in (
                ("failed", QueryGh(lambda query: [node(1)] if "mentions:@me" in query else [], failing=sweep)),
                ("capped", QueryGh(lambda query: [node(1)] if "mentions:@me" in query or sweep(query) else [],
                                   count=lambda query: 1001 if sweep(query) else None))):
            with self.subTest(name):
                self.connection.execute("DELETE FROM shadow")
                self.connection.execute("DELETE FROM state")
                self.connection.execute(
                    "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, first_seen, last_seen) "
                    "VALUES ('github', ?, ?, 9, 'issue', 'old', 'open', 't1', 't1')",
                    (self.REPO, node(9)["url"]))
                result = sync(self.connection, now=self.now, runner=runner)
                self.assertTrue(result.ok, result.reason)
                self.assertTrue(result.watermark_moved)
                self.assertEqual(self.reasons(), {"1": ("open", "watched"), "9": ("open", None)})
                self.assertTrue(any("intake sweep incomplete" in line for line in result.report()), result.report())

    def test_a_complete_sweep_closes_a_stored_open_issue_it_did_not_see(self):
        """Older state: a row stored open while the repository was off, or
        before this change, whose closure no window saw. A complete sweep lists
        every open issue, so a stored open one it and the window both missed
        is not open. Another repository's rows and pull requests stay as they were."""
        for number, repo, kind in ((7, "Example-Org/Benchmark", "issue"), (8, self.REPO, "pull"),
                                   (9, "example-org/other", "issue")):
            self.connection.execute(
                "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, first_seen, last_seen) "
                "VALUES ('github', ?, ?, ?, ?, 'old', 'open', 't1', 't1')",
                (repo, f"https://github.com/{repo.lower()}/issues/{number}", number, kind))
        fresh = node(1)
        result = sync(self.connection, now=self.now,
                      runner=QueryGh(lambda query: [node(2)] if sweep(query) else [fresh] if "mentions:@me" in query else [],
                                     read=lambda path: issue(7, state="closed", url=f"https://github.com/{path[6:]}")))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.reasons(), {"1": ("open", "watched"), "2": ("open", None), "7": ("closed", None),
                                          "8": ("open", None), "9": ("open", None)})

    def test_an_issue_reopened_after_a_bucket_saw_it_closed_is_stored_open(self):
        """The sweep runs after the buckets: its newer open answer wins."""
        closed = {**node(1), "state": "CLOSED", "updatedAt": "2026-09-06T11:00:00Z"}
        reopened = {**node(1), "updatedAt": "2026-09-06T11:30:00Z"}
        result = sync(self.connection, now=self.now,
                      runner=QueryGh(lambda query: [reopened] if sweep(query) else
                                     [closed] if "mentions:@me" in query else []))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.reasons(), {"1": ("open", "watched")})

    def test_an_open_read_keeps_every_spelling_of_the_url_open(self):
        """Round 2 of the lane review: REST answers a canonical `html_url`, so
        a stored url in another spelling is no sign of a closure. Mixed case,
        a trailing slash, an old repository name: the read says open, and the
        row stays open. Only the read's state, a pull request or another
        number closes a row."""
        for name, url in (
                ("mixed case", "https://github.com/Example-Org/Benchmark/issues/1"),
                ("trailing slash", "https://github.com/example-org/benchmark/issues/1/"),
                ("renamed repository", "https://github.com/example-org/old-benchmark/issues/1")):
            with self.subTest(name):
                self.connection.execute("DELETE FROM shadow")
                self.connection.execute(
                    "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, first_seen, last_seen) "
                    "VALUES ('github', ?, ?, 1, 'issue', 'old', 'open', 't1', 't1')", (self.REPO, url))
                runner = QueryGh(lambda query: [], read=lambda path: issue(1))
                result = sync(self.connection, now=self.now, runner=runner)
                self.assertEqual(len(runner.paths), 1)
                self.assertEqual({row["url"]: row["state"] for row in self.shadow().values()}, {url: "open"})
                self.assertFalse(any("intake" in line for line in result.report()), result.report())

    def test_an_open_issue_a_lagging_index_omits_stays_open(self):
        """Lane review of d22fba47: a complete search can omit an open issue
        while GitHub's index lags. Absence is not a closure; the direct read
        says open, so the row stays open."""
        sync(self.connection, now=self.now, runner=QueryGh(lambda query: [node(1)] if sweep(query) else []))
        runner = QueryGh(lambda query: [], read=lambda path: issue(1))
        result = sync(self.connection, now=self.now + timedelta(hours=1), runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(runner.paths, ["repos/example-org/benchmark/issues/1"])
        self.assertEqual(self.reasons(), {"1": ("open", None)})
        self.assertFalse(any("intake" in line for line in result.report()), result.report())

    def test_an_absent_issue_the_read_cannot_settle_stays_open_and_is_named(self):
        """A failed, refused, malformed or over-budget read is unknown, not closed."""
        cases: tuple = (
            ("gh failure", dict(read=None), {}),
            ("not found", dict(read=lambda path: (404, {"message": "Not Found"})), {}),
            ("forbidden", dict(read=lambda path: (403, {"message": "Resource not accessible"})), {}),
            ("redirect not followed", dict(read=lambda path: (301, {"url": "elsewhere"})), {}),
            ("no number", dict(read=lambda path: (200, {"state": "open"})), {}),
            ("text number", dict(read=lambda path: (200, {"state": "open", "number": "1"})), {}),
            ("odd state", dict(read=lambda path: issue(1, state="locked")), {}),
            ("not an object", dict(read=lambda path: (200, ["closed"])), {}),
            ("budget spent", dict(read=lambda path: issue(1, state="closed")), {"max_requests": 5}),
        )
        for name, seam, limits in cases:
            with self.subTest(name):
                self.connection.execute("DELETE FROM shadow")
                self.connection.execute("DELETE FROM state")
                self.connection.execute(
                    "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, first_seen, last_seen) "
                    "VALUES ('github', ?, ?, 1, 'issue', 'old', 'open', 't1', 't1')", (self.REPO, node(1)["url"]))
                result = sync(self.connection, now=self.now, runner=QueryGh(lambda query: [], **seam), **limits)
                self.assertEqual(self.reasons(), {"1": ("open", None)})
                self.assertTrue(result.watermark_moved)
                report = [line for line in result.report() if "intake sweep incomplete" in line]
                self.assertTrue(report and node(1)["url"] in report[0], result.report())

    def test_the_read_closes_a_row_only_on_its_own_answer(self):
        """A closed state, a pull request, or another number closes the row. A
        transferred or converted issue that still answers open under the
        stored number stays open: it is an open issue somewhere, and Today
        offers it to be filed."""
        answers = (
            ("closed", issue(1, state="closed"), "closed"),
            ("pull request", issue(1, url="https://github.com/example-org/benchmark/pull/1", pull_request={}),
             "closed"),
            ("another number", issue(40, url="https://github.com/example-org/other/issues/40"), "closed"),
            ("transferred, same number", issue(1, url="https://github.com/example-org/other/issues/1"), "open"),
            ("discussion url", issue(1, url="https://github.com/example-org/benchmark/discussions/1"), "open"),
        )
        for name, answer, state in answers:
            with self.subTest(name):
                self.connection.execute("DELETE FROM shadow")
                self.connection.execute(
                    "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, first_seen, last_seen) "
                    "VALUES ('github', ?, ?, 1, 'issue', 'old', 'open', 't1', 't1')", (self.REPO, node(1)["url"]))
                result = sync(self.connection, now=self.now,
                              runner=QueryGh(lambda query: [], read=lambda path, answer=answer: answer))
                self.assertEqual(self.reasons(), {"1": (state, None)})
                self.assertFalse(any("intake" in line for line in result.report()), result.report())

    def test_a_gone_answer_closes_the_row_with_no_close_date(self):
        """Pack sd:5415. GitHub answers 410 for a deleted issue and for every
        issue of a repository whose Issues are disabled: no open issue is
        there, and no later read will say otherwise. A 404 stays unknown
        (above): a token without access to a private repository gets one too."""
        for message in ("Issues are disabled for this repo", "This issue was deleted"):
            with self.subTest(message):
                self.connection.execute("DELETE FROM shadow")
                self.connection.execute(
                    "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, first_seen, last_seen) "
                    "VALUES ('github', ?, ?, 1, 'issue', 'old', 'open', 't1', 't1')", (self.REPO, node(1)["url"]))
                result = sync(self.connection, now=self.now,
                              runner=QueryGh(lambda query: [], read=lambda path, message=message: (410, {"message": message})))
                [row] = self.shadow().values()
                self.assertEqual((row["state"], row["closed_at"]), ("closed", None))
                self.assertFalse(any("intake" in line for line in result.report()), result.report())

    def test_an_incomplete_sweep_reads_and_closes_nothing(self):
        """A failed, capped or short sweep is no listing: no absent issue is read or closed."""
        for name, runner in (
                ("failed", QueryGh(lambda query: [], failing=sweep, read=lambda path: issue(9, state="closed"))),
                ("capped", QueryGh(lambda query: [], count=lambda query: 1001 if sweep(query) else None,
                                   read=lambda path: issue(9, state="closed"))),
                ("short page", QueryGh(lambda query: [node(1)] if sweep(query) else [],
                                       count=lambda query: 2 if sweep(query) else None,
                                       read=lambda path: issue(9, state="closed")))):
            with self.subTest(name):
                self.connection.execute("DELETE FROM shadow")
                self.connection.execute(
                    "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, first_seen, last_seen) "
                    "VALUES ('github', ?, ?, 9, 'issue', 'old', 'open', 't1', 't1')", (self.REPO, node(9)["url"]))
                result = sync(self.connection, now=self.now, runner=runner)
                self.assertEqual(runner.paths, [])
                self.assertEqual(self.reasons()["9"], ("open", None))
                self.assertTrue(any("intake sweep incomplete" in line for line in result.report()), result.report())

    def test_each_intake_sweep_names_at_most_five_repositories(self):
        from sd_db.writes import upsert_repo

        for number in range(6):
            upsert_repo(self.connection, f"/srv/example/r{number}", remote=f"https://github.com/example-org/r{number}")
        self.connection.execute("UPDATE repo SET declarations = '{\"policy\":{\"issues\":\"ask\"}}'")
        runner = QueryGh(lambda query: [])
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        intake = [query for query in runner.queries if "repo:" in query]
        self.assertEqual(sorted(query.count("repo:") for query in intake), [3, 5])
        self.assertEqual(len({part for query in intake for part in query.split() if part.startswith("repo:")}), 8)

    def test_a_repository_switched_off_is_searched_no_more(self):
        self.connection.execute("UPDATE repo SET declarations = NULL")
        runner = QueryGh(lambda query: [])
        sync(self.connection, now=self.now, runner=runner)
        self.assertEqual(len(runner.queries), 4)
        self.assertFalse(any("repo:" in query for query in runner.queries))


def scoped(qualifier):
    """The unwindowed `qualifier` search a `mine` intake repository gets (pack sd:7672)."""
    return lambda query: qualifier in query and "is:open" in query and "updated:" not in query


class TheMineScopeSearch(SyncCase):
    """pack sd:7672 lane review. A repository whose intake scope is `mine` decides
    membership from `shadow.reason`, so the sync must give that reason to every
    open issue there, not only the ones its window saw, and take back one that
    no longer holds. Class: membership derived from a windowed reason."""

    REPO = "example-org/benchmark"
    OLD = "2025-01-01T00:00:00Z"

    def setUp(self):
        super().setUp()
        from sd_db.writes import upsert_repo

        upsert_repo(self.connection, "/srv/example/benchmark", remote=f"git@github.com:{self.REPO}.git")
        upsert_repo(self.connection, "/srv/example/other", remote="https://github.com/example-org/other")
        self.connection.execute(
            "UPDATE repo SET declarations = '{\"policy\":{\"issues\":\"ask\",\"issues_scope\":\"mine\"}}' "
            "WHERE path = '/srv/example/benchmark'")
        self.connection.execute(
            "UPDATE repo SET declarations = '{\"policy\":{\"issues\":\"ask\"}}' WHERE path = '/srv/example/other'")

    def old(self, number):
        return {**node(number), "updatedAt": self.OLD}

    def reasons(self):
        return {row["url"].rsplit("/", 1)[1]: row["reason"] for row in self.shadow().values()}

    def intake(self):
        from sd_db.repos import intake_issues

        return [row["number"] for row in intake_issues(self.connection).get("/srv/example/benchmark", [])]

    def test_an_issue_assigned_or_mentioning_before_the_window_gets_its_reason(self):
        """Issues 1 and 2 were last updated before the 90-day window, so no
        windowed bucket returns them; the scoped searches do, for this
        repository alone. Issue 3 reached nobody and stays without one."""
        answer = {"assignee:@me": [self.old(1)], "mentions:@me": [self.old(2)]}
        runner = QueryGh(lambda query: (
            next((nodes for word, nodes in answer.items() if scoped(word)(query)), None)
            or ([self.old(1), self.old(2), self.old(3)] if sweep(query) else [])))
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.reasons(), {"1": "assigned", "2": "watched", "3": None})
        self.assertEqual(sorted(self.intake()), [1, 2])
        mine = [query for query in runner.queries if "updated:" not in query and "@me" in query]
        self.assertEqual(sorted(mine), [f"is:issue is:open assignee:@me repo:{self.REPO}",
                                        f"is:issue is:open mentions:@me repo:{self.REPO}"])

    def test_a_reason_that_no_longer_holds_is_taken_back(self):
        """Older state: issue 1 was assigned and issue 2 mentioned the operator;
        both since lost it. Issue 4 the operator opened keeps `filed`. A
        repository outside the scope keeps its stored reason."""
        self.connection.execute(
            "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, reason, filed, first_seen, "
            "last_seen) VALUES ('github', 'example-org/other', 'https://github.com/example-org/other/issues/9', 9, "
            "'issue', 'other', 'open', 'assigned', 0, 't1', 't1')")
        first = QueryGh(lambda query: [self.old(1)] if scoped("assignee:@me")(query)
                        else [self.old(2)] if scoped("mentions:@me")(query)
                        else [self.old(1), self.old(2), self.old(4)] if sweep(query) else [])
        sync(self.connection, now=self.now, runner=first)
        self.connection.execute("UPDATE shadow SET reason = 'assigned', filed = 1 WHERE number = 4")
        self.assertEqual(self.reasons(), {"1": "assigned", "2": "watched", "4": "assigned", "9": "assigned"})
        later = QueryGh(lambda query: [self.old(1), self.old(2), self.old(4)]
                        if sweep(query) and not scoped("@me")(query) else [],
                        read=lambda path: issue(9))
        result = sync(self.connection, now=self.now + timedelta(hours=1), runner=later)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.reasons(), {"1": None, "2": None, "4": "filed", "9": "assigned"})
        self.assertEqual(self.intake(), [])

    def test_a_failed_scoped_search_takes_back_nothing_and_is_named(self):
        """A failed or capped search is unknown, not "no longer the operator's"."""
        first = QueryGh(lambda query: [self.old(1)] if scoped("assignee:@me")(query)
                        else [self.old(1)] if sweep(query) else [])
        sync(self.connection, now=self.now, runner=first)
        for name, runner in (
                ("failed", QueryGh(lambda query: [self.old(1)] if sweep(query) and not scoped("@me")(query) else [],
                                   failing=scoped("mentions:@me"))),
                ("capped", QueryGh(lambda query: [self.old(1)] if sweep(query) and not scoped("@me")(query) else [],
                                   count=lambda query: 1001 if scoped("assignee:@me")(query) else None))):
            with self.subTest(name):
                result = sync(self.connection, now=self.now + timedelta(hours=1), runner=runner)
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(self.reasons(), {"1": "assigned"})
                self.assertTrue(any("intake sweep incomplete" in line and "@me" in line
                                    for line in result.report()), result.report())


class TheStaleRead(SyncCase):
    """Pack sd:5415. An open issue row of a repository outside intake came
    from the operator's windowed buckets, and a closure no window saw, such as
    one after the operator was unassigned, or a repository whose Issues were
    disabled, left it open for good. A row no search has seen for a week is
    read directly, oldest first, on the search budget, by the intake rules."""

    INTAKE = "example-org/benchmark"
    OTHER = "example-org/other"
    STALE = "2026-08-01T00:00:00+00:00"

    def setUp(self):
        super().setUp()
        from sd_db.writes import upsert_repo

        upsert_repo(self.connection, "/srv/example/benchmark", remote=f"git@github.com:{self.INTAKE}.git")
        self.connection.execute("UPDATE repo SET declarations = '{\"policy\":{\"issues\":\"ask\"}}'")

    def url(self, number, repo=OTHER):
        return f"https://github.com/{repo}/issues/{number}"

    def stored(self, number, last_seen=STALE, repo=OTHER):
        self.connection.execute(
            "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, first_seen, last_seen) "
            "VALUES ('github', ?, ?, ?, 'issue', 'old', 'open', ?, ?)",
            (repo, self.url(number, repo), number, last_seen, last_seen))

    def row(self, number, repo=OTHER):
        return self.shadow()[self.url(number, repo)]

    def read(self, number, **extra):
        """The read's answer under the stored url: another url folds the row (sd:6091)."""
        return issue(number, url=self.url(number), **extra)

    def test_a_stale_row_outside_intake_is_read_and_closed(self):
        answers = (
            ("closed", self.read(3, state="closed", closed_at="2026-08-20T09:00:00Z"), "2026-08-20T09:00:00Z"),
            ("issues disabled", (410, {"message": "Issues are disabled for this repo"}), None),
        )
        for name, answer, closed_at in answers:
            with self.subTest(name):
                self.connection.execute("DELETE FROM shadow")
                self.stored(3)
                runner = QueryGh(lambda query: [], read=lambda path, answer=answer: answer)
                result = sync(self.connection, now=self.now, runner=runner)
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(runner.paths, [f"repos/{self.OTHER}/issues/3"])
                self.assertEqual((self.row(3)["state"], self.row(3)["closed_at"]), ("closed", closed_at))

    def test_a_not_found_answer_stays_open_and_is_named(self):
        """A 404 can be a token that lost access, not a closure."""
        self.stored(3)
        result = sync(self.connection, now=self.now,
                      runner=QueryGh(lambda query: [], read=lambda path: (404, {"message": "Not Found"})))
        self.assertEqual(self.row(3)["state"], "open")
        self.assertTrue(any(self.url(3) in line for line in result.report()), result.report())

    def test_an_open_answer_is_a_sighting_and_is_not_read_again_at_once(self):
        self.stored(3)
        result = sync(self.connection, now=self.now, runner=QueryGh(lambda query: [], read=lambda path: self.read(3)))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.row(3)["state"], "open")
        self.assertGreater(self.row(3)["last_seen"], self.STALE)
        runner = QueryGh(lambda query: [], read=lambda path: self.read(3))
        sync(self.connection, now=self.now + timedelta(hours=1), runner=runner)
        self.assertEqual(runner.paths, [])

    def test_a_fresh_row_or_one_a_bucket_saw_this_run_is_not_read(self):
        self.stored(4, last_seen="2026-09-05T00:00:00+00:00")
        self.stored(5)
        seen = node(5, url=self.url(5))
        runner = QueryGh(lambda query: [seen] if "mentions:@me" in query else [], read=lambda path: issue(5))
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(runner.paths, [])

    def test_the_budget_reads_the_oldest_first_and_names_the_rest(self):
        """Four bucket pages, the intake sweep and one read fit in six requests."""
        self.stored(6, last_seen="2026-08-02T00:00:00+00:00")
        self.stored(7, last_seen="2026-08-01T00:00:00+00:00")
        runner = QueryGh(lambda query: [], read=lambda path: self.read(7, state="closed"))
        result = sync(self.connection, now=self.now, runner=runner, max_requests=6)
        self.assertEqual(runner.paths, [f"repos/{self.OTHER}/issues/7"])
        self.assertEqual((self.row(6)["state"], self.row(7)["state"]), ("open", "closed"))
        self.assertTrue(any(self.url(6) in line for line in result.report()), result.report())

    def test_an_intake_row_or_a_failed_collect_reads_no_stale_row(self):
        """An intake row is read only after its complete sweep; a run whose
        buckets failed reads no stale row, since its reads may fail the same way."""
        self.stored(8, repo=self.INTAKE)
        runner = QueryGh(lambda query: [], failing=sweep, read=lambda path: issue(8, state="closed"))
        sync(self.connection, now=self.now, runner=runner)
        self.assertEqual(runner.paths, [])
        self.connection.execute("DELETE FROM shadow")
        self.stored(9)
        runner = QueryGh(lambda query: [], failing=lambda query: "mentions:@me" in query,
                         read=lambda path: issue(9, state="closed"))
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertFalse(result.ok)
        self.assertEqual(runner.paths, [])
        self.assertEqual(self.row(9)["state"], "open")


def pull(number, *, state="open", merged=False, url=None, **extra):
    """A direct pull read's 200 answer."""
    return 200, {"html_url": url or f"https://github.com/example-org/other/pull/{number}", "number": number,
                 "state": state, "merged": merged, **extra}


class Offline:
    """A contribution client whose every read fails, so `refresh` observes nothing."""

    def __init__(self):
        self.budget = None
        self.floor = 0

    def get(self, path, **_):
        from sd_db.contribution_github import Unavailable

        raise Unavailable("offline")


class TheRevisit(SyncCase):
    """Sd:6091. A pull request no search returns any more, or one whose
    repository was transferred or renamed, kept state open for good: the
    window stops returning a row once it leaves the operator's searches, and
    no read named the old url again. A run reads at most `REVISIT_LIMIT`
    stale rows, oldest first; a closed or merged answer settles the row, and
    an answer under another url folds the row into it."""

    REPO = "example-org/other"
    OLD = "example-user/prism-research"
    NEW = "example-org/prism-research"
    STALE = "2026-08-01T00:00:00+00:00"

    def stored(self, number, *, kind="pull", repo=REPO, last_seen=STALE):
        url = f"https://github.com/{repo}/{'pull' if kind == 'pull' else 'issues'}/{number}"
        self.connection.execute(
            "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, first_seen, last_seen) "
            "VALUES ('github', ?, ?, ?, ?, 'old', 'open', ?, ?)", (repo, url, number, kind, last_seen, last_seen))
        return url

    def states(self):
        return {url: (row["state"], row["closed_at"], row["merged_at"]) for url, row in self.shadow().items()}

    def test_a_stale_pull_is_read_and_settled_by_its_own_answer(self):
        url = f"https://github.com/{self.REPO}/pull/3"
        answers = (
            ("merged", pull(3, state="closed", merged=True, closed_at="2026-08-20T09:00:00Z",
                            merged_at="2026-08-20T09:00:00Z"), ("merged", "2026-08-20T09:00:00Z", "2026-08-20T09:00:00Z")),
            ("closed", pull(3, state="closed", closed_at="2026-08-21T09:00:00Z"), ("closed", "2026-08-21T09:00:00Z", None)),
            ("open", pull(3), ("open", None, None)),
        )
        for name, answer, expected in answers:
            with self.subTest(name):
                self.connection.execute("DELETE FROM shadow")
                self.stored(3)
                runner = QueryGh(lambda query: [], read=lambda path, answer=answer: answer)
                result = sync(self.connection, now=self.now, runner=runner)
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(runner.paths, [f"repos/{self.REPO}/pulls/3"])
                self.assertEqual(self.states(), {url: expected})
                self.assertEqual(self.shadow()[url]["last_seen"] > self.STALE, name == "open")

    def test_a_pull_read_that_cannot_settle_stays_open_and_is_named(self):
        for name, answer in (("not found", (404, {"message": "Not Found"})), ("no merged flag", (200, {
                "html_url": f"https://github.com/{self.REPO}/pull/3", "number": 3, "state": "closed"})),
                             ("odd state", pull(3, state="draft"))):
            with self.subTest(name):
                self.connection.execute("DELETE FROM shadow")
                url = self.stored(3)
                result = sync(self.connection, now=self.now,
                              runner=QueryGh(lambda query: [], read=lambda path, answer=answer: answer))
                self.assertEqual(self.states(), {url: ("open", None, None)})
                self.assertTrue(any(url in line for line in result.report()), result.report())

    def test_a_redirect_folds_the_row_into_the_new_url(self):
        """A transferred repository answers the old url under the new one."""
        self.stored(6, repo=self.OLD)
        new = f"https://github.com/{self.NEW}/pull/6"
        runner = QueryGh(lambda query: [], read=lambda path: pull(6, state="closed", url=new,
                                                                  closed_at="2026-09-24T10:00:00Z"))
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(runner.paths, [f"repos/{self.OLD}/pulls/6"])
        self.assertEqual(self.states(), {new: ("closed", "2026-09-24T10:00:00Z", None)})
        self.assertEqual((self.shadow()[new]["repo"], self.shadow()[new]["number"]), (self.NEW, 6))

    def test_a_redirect_to_a_stored_url_drops_the_old_row(self):
        """The search already found the pull under its new url: one row stays."""
        self.stored(6, repo=self.OLD)
        new = f"https://github.com/{self.NEW}/pull/6"
        found = {**node(6, url=new, kind="PullRequest"), "repository": {"nameWithOwner": self.NEW}}
        runner = QueryGh(lambda query: [found] if "assignee:@me" in query else [],
                         read=lambda path: pull(6, url=new.replace("example-org", "Example-Org")))
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.states(), {new: ("open", None, None)})

    def test_a_redirect_to_a_stored_url_settles_that_row(self):
        """Lane review of 135950aae: the read's lifecycle reaches the row that
        holds the new url before the old row goes."""
        self.stored(6, repo=self.OLD)
        new = self.stored(6, repo=self.NEW, last_seen="2026-09-06T00:00:00+00:00")
        runner = QueryGh(lambda query: [], read=lambda path: pull(
            6, url=new, state="closed", merged=True, closed_at="2026-09-01T10:00:00Z", merged_at="2026-09-01T10:00:00Z"))
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.states(), {new: ("merged", "2026-09-01T10:00:00Z", "2026-09-01T10:00:00Z")})

    def test_another_spelling_of_the_stored_url_is_no_move(self):
        """Round 2 of the sd:4880 lane review: REST answers a canonical
        `html_url`, so case and a trailing slash name the same row."""
        stored = "https://github.com/Example-Org/Other/pull/3/"
        self.connection.execute(
            "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, first_seen, last_seen) "
            "VALUES ('github', ?, ?, 3, 'pull', 'old', 'open', ?, ?)", (self.REPO, stored, self.STALE, self.STALE))
        result = sync(self.connection, now=self.now,
                      runner=QueryGh(lambda query: [], read=lambda path: pull(3, state="closed")))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.states(), {stored: ("closed", None, None)})

    def test_a_transferred_issue_folds_into_its_new_number(self):
        self.stored(9, kind="issue")
        new = "https://github.com/example-org/elsewhere/issues/40"
        result = sync(self.connection, now=self.now,
                      runner=QueryGh(lambda query: [], read=lambda path: issue(40, url=new)))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.states(), {new: ("open", None, None)})
        self.assertEqual(self.shadow()[new]["number"], 40)

    def test_a_run_reads_at_most_the_limit_oldest_first(self):
        from sd_db import shadow_sync

        urls = [self.stored(number, last_seen=f"2026-08-{number:02d}T00:00:00+00:00") for number in range(1, 26)]
        runner = QueryGh(lambda query: [], read=lambda path: pull(int(path.rsplit("/", 1)[1])))
        result = sync(self.connection, now=self.now, runner=runner)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(shadow_sync.REVISIT_LIMIT, 20)
        self.assertEqual(runner.paths, [f"repos/{self.REPO}/pulls/{number}" for number in range(1, 21)])
        self.assertEqual([url for url in urls if self.shadow()[url]["last_seen"] > "2026-09"], urls[:20])

    def test_a_row_whose_read_failed_goes_after_the_rest(self):
        """Twenty rows that always fail must not starve the others; a read the
        budget never reached is no failure and keeps its place."""
        from sd_db import shadow_sync

        first = self.stored(1, last_seen="2026-08-01T00:00:00+00:00")
        self.stored(2, last_seen="2026-08-02T00:00:00+00:00")
        with patch.object(shadow_sync, "REVISIT_LIMIT", 1):
            runner = QueryGh(lambda query: [], read=lambda path: (404, {"message": "Not Found"}))
            sync(self.connection, now=self.now, runner=runner, max_requests=4)
            self.assertEqual(runner.paths, [])
            runner = QueryGh(lambda query: [], read=lambda path: (404, {"message": "Not Found"}))
            sync(self.connection, now=self.now + timedelta(hours=1), runner=runner)
            self.assertEqual(runner.paths, [f"repos/{self.REPO}/pulls/1"])
            runner = QueryGh(lambda query: [], read=lambda path: pull(int(path.rsplit("/", 1)[1])))
            sync(self.connection, now=self.now + timedelta(hours=2), runner=runner)
            self.assertEqual(runner.paths, [f"repos/{self.REPO}/pulls/2"])
            self.assertEqual(self.shadow()[first]["state"], "open")

    def observed(self, url, *, observed_at="2026-08-01T00:00:00Z", acknowledged=()):
        """An observed contribution, last read completely at `observed_at`."""
        from sd_db.database import transaction
        from sd_db.writes import record_state

        with transaction(self.connection):
            record_state(self.connection, "checkpoint", key="contribution:github:" + url, body={
                "observation": {"observed_at": observed_at, "state": "open", "title": "old",
                                "repo": "/".join(url.split("/")[3:5])},
                "attention": [], "notifications": {}, "acknowledged": list(acknowledged)})

    def queued(self):
        from sd_db import contribution_sync

        return [row["key"] for row in contribution_sync._queue(self.connection)["pending"]]

    def test_a_redirect_folds_an_observed_contribution(self):
        from sd_db import contributions

        old = f"https://github.com/{self.OLD}/pull/6"
        new = f"https://github.com/{self.NEW}/pull/6"
        self.observed(old, acknowledged=["event:1"])
        runner = QueryGh(lambda query: [], read=lambda path: pull(6, url=new))
        sync(self.connection, now=self.now, runner=runner, contribution_client=Offline())
        self.assertEqual(runner.paths, [f"repos/{self.OLD}/pulls/6"])
        self.assertEqual([row["url"] for row in contributions.projection(self.connection)], [new])
        self.assertEqual(contributions.snapshot(self.connection, "github:" + new)["acknowledged"], ["event:1"])
        self.assertEqual(contributions.snapshot(self.connection, "github:" + old)["moved_to"], new)
        self.assertEqual(self.queued(), ["github:" + new])

    def test_a_moved_key_a_queue_kept_is_dropped(self):
        """Steps: a run that stopped after the fold, before `refresh` saved
        the queue, leaves the old key queued. The next plan drops it."""
        from sd_db import contribution_sync, contributions

        old = f"https://github.com/{self.OLD}/pull/6"
        new = f"https://github.com/{self.NEW}/pull/6"
        self.observed(old)
        planned = contribution_sync.plan(self.connection)
        contribution_sync._save_queue(self.connection, planned["pending"], planned["revision"])
        contributions.fold(self.connection, "github:" + old, new)
        self.assertEqual(self.queued(), ["github:" + old])
        self.assertEqual([row["key"] for row in contribution_sync.plan(self.connection)["pending"]], ["github:" + new])

    def test_a_contribution_read_that_moved_nothing_goes_after_the_rest(self):
        """The queue's own read settles a contribution; one whose url still
        answers must not take a revisit slot every run."""
        from sd_db import contributions, shadow_sync

        first = f"https://github.com/{self.REPO}/pull/1"
        self.observed(first, observed_at="2026-08-01T00:00:00Z")
        self.observed(f"https://github.com/{self.REPO}/pull/2", observed_at="2026-08-02T00:00:00Z")
        with patch.object(shadow_sync, "REVISIT_LIMIT", 1):
            for hours, number in ((0, 1), (1, 2), (2, 1)):
                runner = QueryGh(lambda query: [], read=lambda path: pull(int(path.rsplit("/", 1)[1])))
                sync(self.connection, now=self.now + timedelta(hours=hours), runner=runner,
                     contribution_client=Offline())
                self.assertEqual(runner.paths, [f"repos/{self.REPO}/pulls/{number}"])
        self.assertNotIn("moved_to", contributions.snapshot(self.connection, "github:" + first))

    def test_a_fold_back_into_a_moved_key_reactivates_it(self):
        """Lane review of 4658ee59d: a repository renamed A to B and back to A
        must leave one live row, not two tombstones."""
        from sd_db import contribution_sync, contributions

        first = f"https://github.com/{self.OLD}/pull/6"
        second = f"https://github.com/{self.NEW}/pull/6"
        self.observed(first, acknowledged=["event:1"])
        contributions.fold(self.connection, "github:" + first, second)
        contributions.fold(self.connection, "github:" + second, first)
        self.assertEqual([row["url"] for row in contributions.projection(self.connection)], [first])
        self.assertNotIn("moved_to", contributions.snapshot(self.connection, "github:" + first))
        self.assertEqual(contributions.snapshot(self.connection, "github:" + first)["acknowledged"], ["event:1"])
        self.assertEqual([row["key"] for row in contribution_sync.plan(self.connection)["pending"]],
                         ["github:" + first])

    def test_a_shadow_row_moved_and_moved_back_stays_one_open_row(self):
        """The shadow fold renames the row and leaves no tombstone: A to B and back to A is one row at A."""
        first = self.stored(6, repo=self.OLD)
        second = f"https://github.com/{self.NEW}/pull/6"
        for hours, target in ((0, second), (1, first)):
            sync(self.connection, now=self.now + timedelta(hours=hours),
                 runner=QueryGh(lambda query: [], read=lambda path, target=target: pull(6, url=target)))
            self.connection.execute("UPDATE shadow SET last_seen = ?", (self.STALE,))
        self.assertEqual(self.states(), {first: ("open", None, None)})

    def test_an_item_set_to_a_moved_url_is_scheduled(self):
        """A sibling of the fold: the operator points an item at a url a fold
        left a tombstone on. The item's key must be read, not skipped."""
        from sd_db import contribution_sync, contributions

        first = f"https://github.com/{self.OLD}/pull/6"
        second = f"https://github.com/{self.NEW}/pull/6"
        self.observed(first)
        contributions.fold(self.connection, "github:" + first, second)
        contributions.capture(self.connection, title="Fix", changes={"pull_url": first}, who="test")
        self.assertNotIn("moved_to", contributions.snapshot(self.connection, "github:" + first))
        self.assertIn("github:" + first, [row["key"] for row in contribution_sync.plan(self.connection)["pending"]])

    def test_rows_that_always_fail_take_turns_with_every_other_row(self):
        """Lane review of 4658ee59d: deferred rows stay deferred until every
        other stale row has had a turn, then the oldest deferral goes first."""
        from sd_db import shadow_sync

        for number in range(1, 7):
            self.stored(number, last_seen=f"2026-08-{number:02d}T00:00:00+00:00")
        with patch.object(shadow_sync, "REVISIT_LIMIT", 2):
            for hours, numbers in ((0, [1, 2]), (1, [3, 4]), (2, [5, 6]), (3, [1, 2]), (4, [3, 4])):
                runner = QueryGh(lambda query: [], read=lambda path: (404, {"message": "Not Found"}))
                sync(self.connection, now=self.now + timedelta(hours=hours), runner=runner)
                self.assertEqual(runner.paths, [f"repos/{self.REPO}/pulls/{number}" for number in numbers], hours)

    def test_a_refused_fold_goes_after_the_rest(self):
        """Lane review of 135950aae: a fold an item refuses leaves the row stale,
        so it is deferred like a failed read, or twenty such rows hold every slot."""
        from sd_db import contributions, shadow_sync

        old = f"https://github.com/{self.OLD}/pull/6"
        contributions.capture(self.connection, title="Fix", changes={"pull_url": old}, who="test")
        self.observed(old, observed_at="2026-08-01T00:00:00Z")
        self.observed(f"https://github.com/{self.REPO}/pull/2", observed_at="2026-08-02T00:00:00Z")
        with patch.object(shadow_sync, "REVISIT_LIMIT", 1):
            for hours, path in ((0, f"repos/{self.OLD}/pulls/6"), (1, f"repos/{self.REPO}/pulls/2")):
                runner = QueryGh(lambda query: [], read=lambda path: pull(
                    int(path.rsplit("/", 1)[1]), url=f"https://github.com/{self.NEW}/pull/6" if self.OLD in path else None))
                sync(self.connection, now=self.now + timedelta(hours=hours), runner=runner,
                     contribution_client=Offline())
                self.assertEqual(runner.paths, [path], hours)

    def test_a_settled_row_with_a_stale_contribution_is_deferred(self):
        """The shadow row settles, but the contribution of the same url stays
        stale until its queue read completes: the url is deferred, whatever
        spelling each store keeps."""
        from sd_db import shadow_sync

        stored = "https://github.com/Example-Org/Other/pull/1"
        self.connection.execute(
            "INSERT INTO shadow (tracker, repo, url, number, kind, title, state, first_seen, last_seen) "
            "VALUES ('github', ?, ?, 1, 'pull', 'old', 'open', ?, ?)", (self.REPO, stored, self.STALE, self.STALE))
        self.observed(f"https://github.com/{self.REPO}/pull/1", observed_at="2026-08-01T00:00:00Z")
        self.observed(f"https://github.com/{self.REPO}/pull/2", observed_at="2026-08-02T00:00:00Z")
        with patch.object(shadow_sync, "REVISIT_LIMIT", 1):
            for hours, number in ((0, 1), (1, 2)):
                runner = QueryGh(lambda query: [], read=lambda path: pull(int(path.rsplit("/", 1)[1]), state="closed"))
                sync(self.connection, now=self.now + timedelta(hours=hours), runner=runner,
                     contribution_client=Offline())
                self.assertEqual([path.lower() for path in runner.paths],
                                 [f"repos/{self.REPO}/pulls/{number}"], hours)
        self.assertEqual(self.states()[stored][0], "closed")

    def test_a_stale_shadow_row_goes_before_older_contributions(self):
        """Sd:7265. A shadow row has no other read that closes it; a
        contribution's own queue read refreshes it. On the hub 1,750 older
        contribution observations held every slot, and merged pulls read
        open on Trackers for weeks. A deferred shadow row goes after every
        undeferred row, as any deferred row does."""
        from sd_db import shadow_sync

        failing = self.stored(1, last_seen="2026-08-10T00:00:00+00:00")
        merged = self.stored(2, last_seen="2026-08-11T00:00:00+00:00")
        for number in (3, 4):
            self.observed(f"https://github.com/{self.REPO}/pull/{number}",
                          observed_at=f"2026-08-0{number}T00:00:00Z")

        def read(path):
            number = int(path.rsplit("/", 1)[1])
            if number == 1:
                return 404, {"message": "Not Found"}
            return pull(number, **({"state": "closed", "merged": True} if number == 2 else {}))

        with patch.object(shadow_sync, "REVISIT_LIMIT", 1):
            for hours, number in ((0, 1), (1, 2), (2, 3), (3, 4), (4, 1)):
                runner = QueryGh(lambda query: [], read=read)
                sync(self.connection, now=self.now + timedelta(hours=hours), runner=runner,
                     contribution_client=Offline())
                self.assertEqual(runner.paths, [f"repos/{self.REPO}/pulls/{number}"], hours)
        self.assertEqual(self.states()[merged][0], "merged")
        self.assertEqual(self.states()[failing][0], "open")

    def test_failing_shadow_rows_do_not_starve_a_contribution(self):
        """Lead review of 599e62606: only a revisit folds a contribution
        whose repository moved. Twenty shadow rows that always fail take
        one run's slots, then go after the fresh contribution."""
        from sd_db import shadow_sync

        for number in range(1, shadow_sync.REVISIT_LIMIT + 1):
            self.stored(number, last_seen=f"2026-08-{number + 1:02d}T00:00:00+00:00")
        contribution = f"repos/{self.REPO}/pulls/{shadow_sync.REVISIT_LIMIT + 1}"
        self.observed(f"https://github.com/{self.REPO}/pull/{shadow_sync.REVISIT_LIMIT + 1}",
                      observed_at="2026-08-01T00:00:00Z")

        def read(path):
            if path == contribution:
                return pull(int(path.rsplit("/", 1)[1]))
            return 404, {"message": "Not Found"}

        for hours, expected in ((0, False), (1, True)):
            runner = QueryGh(lambda query: [], read=read)
            sync(self.connection, now=self.now + timedelta(hours=hours), runner=runner,
                 contribution_client=Offline())
            self.assertEqual(contribution in runner.paths, expected, hours)
            self.assertEqual(len(runner.paths), shadow_sync.REVISIT_LIMIT, hours)

    def test_a_fold_into_a_live_key_keeps_that_key_state(self):
        """The search already observed the pull under its new url: its state wins, the old key is a tombstone."""
        from sd_db import contributions

        old = f"https://github.com/{self.OLD}/pull/6"
        new = f"https://github.com/{self.NEW}/pull/6"
        self.observed(old, acknowledged=["event:old"])
        self.observed(new, observed_at="2026-09-06T00:00:00Z", acknowledged=["event:new"])
        contributions.fold(self.connection, "github:" + old, new)
        self.assertEqual(contributions.snapshot(self.connection, "github:" + new)["acknowledged"], ["event:new"])
        self.assertEqual([row["url"] for row in contributions.projection(self.connection)], [new])

    def test_an_item_that_names_the_old_url_is_not_folded_and_is_named(self):
        """The item's url is the operator's configuration: the fold is named, not done."""
        from sd_db import contributions

        old = f"https://github.com/{self.OLD}/pull/6"
        new = f"https://github.com/{self.NEW}/pull/6"
        item = contributions.capture(self.connection, title="Fix", changes={"pull_url": old}, who="test")["item"]["id"]
        self.observed(old)
        result = sync(self.connection, now=self.now, contribution_client=Offline(),
                      runner=QueryGh(lambda query: [], read=lambda path: pull(6, url=new)))
        self.assertNotIn("moved_to", contributions.snapshot(self.connection, "github:" + old))
        self.assertEqual([row["item_id"] for row in contributions.projection(self.connection)], [item])
        self.assertTrue(any("an item names the old url" in line for line in result.report()), result.report())


class PaginationIntegrity(unittest.TestCase):
    def pages(self, blocks):
        calls = []

        def runner(argv):
            calls.append(argv)
            block = blocks[min(len(calls) - 1, len(blocks) - 1)]
            return 0, json.dumps({"data": {"search": block}}), ""

        return runner, calls

    def block(self, numbers, *, count=2, more=False, cursor=None):
        return {"issueCount": count, "nodes": [node(number) for number in numbers],
                "pageInfo": {"hasNextPage": more, "endCursor": cursor}}

    def test_duplicate_pages_cannot_satisfy_the_advertised_identity_count(self):
        runner, calls = self.pages([self.block([1], more=True, cursor="next"), self.block([1])])
        nodes, cut, error = search("author:@me", runner)
        self.assertEqual(len(nodes), 1)
        self.assertTrue(cut)
        self.assertFalse(error)
        self.assertEqual(len(calls), 2)

    def test_missing_or_repeated_cursors_refuse_without_looping(self):
        for cursor in (None, "same"):
            with self.subTest(cursor=cursor):
                runner, calls = self.pages([self.block([1], more=True, cursor=cursor)])
                _, _, error = search("author:@me", runner)
                self.assertIn("pagination cursor", error)
                self.assertLessEqual(len(calls), 2)

    def test_changed_counts_and_malformed_pages_cannot_claim_complete(self):
        for blocks in ([self.block([1], more=True, cursor="next"), self.block([2], count=3)],
                       [{"nodes": [], "pageInfo": {"hasNextPage": False}}],
                       [self.block([1], count=True)],
                       [{"issueCount": 1, "nodes": [None], "pageInfo": {"hasNextPage": False}}]):
            with self.subTest(blocks=blocks):
                runner, _ = self.pages(blocks)
                _, _, error = search("author:@me", runner)
                self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
