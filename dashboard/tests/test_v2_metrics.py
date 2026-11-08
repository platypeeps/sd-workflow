"""The v2 Metrics page (sd:2119).

What this slice promises: `/metrics` answers under the shared policy and loads
its script before `shell.js`; `/api/metrics` carries the month exactly as
`sd usage` reads it, the 7-day window's last meter reading of each day for a
week, the week's numbers, the provider scorecard, age in
status and observed activity. Each part is guarded
on its own, so one that fails is that part's error and the page shows it as
unknown with the reason. By model has no reader: the document names it with
its reason, and the page never draws it empty. CI health reads the merge
lane's gate runs (sd:5473); the flaky heads moved to Today (pack sd:7663).

Cap and Configure (sd:3387) post `/api/bills/<bill>/cap` and
`/api/providers/configure` with the registry revision `/api/metrics` read. A
400 or 409 wrote nothing; any other failure leaves the outcome unknown.

`metrics.js` runs under JavaScriptCore (osascript) against the stand-in page
and shell `test_v2_tasks` defines, with the document above as its fetch answer.
The browser half -- the look at 375 px -- is a manual check recorded on the
pull request.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import re
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from sd_dashboard import metrics_screen, server, v2
from sd_db import provider_controls, reads, transition, upsert_repo, usage, workflow
from support import NOW, ScreenCase, before_5648, record_cost
from test_v2_queue import RealLane
from test_v2_read import READ_SHELL
from test_v2_registry import Registers
from test_v2_tasks import SHELL, STAND_IN
from test_v2_today import OSASCRIPT, Refused
from test_workflow_actions import BrowserSession

V2 = Path(v2.__file__).resolve().parent
METRICS_JS = (V2 / "static" / "metrics.js").read_text(encoding="utf-8")
MARKUP_JS = (V2 / "static" / "markup.js").read_text(encoding="utf-8")


class Fixture(ScreenCase):
    """A month on a capped company bill and a plan bill, a week of window readings and three open items.

    NOW is Sunday 2026-09-06; its week starts Monday 08-31.
    """

    def setUp(self):
        super().setUp()
        for name in ("kimi", "claude"):
            self.provider(name)
        self.bill("capped", 100, "company")
        self.bill("plan", None, "plan")
        self.cost("run", "2026-09-02T10:00:00Z", provider="kimi", bill="capped", role="author", usd=80)
        self.cost("run", "2026-09-03T10:00:00Z", provider="claude", bill="plan", role="reviewer", usd=0)
        for at, window, pct in (("2026-08-30T10:00:00Z", 10080, 10),   # the day before the trend's seven: not carried
                                ("2026-09-01T08:00:00Z", 10080, 20),
                                ("2026-09-01T20:00:00Z", 10080, 30),   # the day's last reading wins
                                ("2026-09-05T20:00:00Z", 300, 99),     # another window: not on the 7-day trend
                                ("2026-09-06T09:00:00Z", 10080, 80)):
            self.cost("meter", at, provider="claude", bill="plan", window_minutes=window, used_percent=pct)
        repo = self.repo()
        self.ready = self.item("Ready to send", status="planning", repo=repo)
        transition(self.connection, self.ready, "ready_to_send", who="sd-ship")
        self.age(self.ready, 4)
        self.item("Open and new", kind="task", repo=repo)
        done = self.item("Finished", kind="task", repo=repo)
        workflow.change_status(self.connection, done, "done", who="test")
        self.connection.commit()

    def bill(self, name, cap, basis):
        self.connection.execute("INSERT OR IGNORE INTO bill VALUES (?, ?, ?)", (name, basis, cap))
        self.connection.commit()

    def cost(self, source, at, **columns):
        row = record_cost(self.connection, source=source, **columns)
        self.connection.execute("UPDATE cost SET timestamp = ? WHERE id = ?", (at, row))
        self.connection.commit()

    def doc(self):
        return metrics_screen.document(self.connection, now=NOW)


#: `kimi` calls a URL on the capped company bill; `claude` is a `start` entry on `sub`, so `sub` takes no cap.
REGISTRY = """bills:
  capped: {cost: company}
  plan: {cost: plan}
  sub: {cost: subscription}
providers:
  claude: {start: "claude -p", vendor: anthropic, bill: sub, roles: [author, reviewer], reader: claude-json}
  kimi: {url: "https://example.invalid/v1", model: fixture, vendor: moonshot, bill: capped, roles: [author, reviewer]}
roles:
  author: [claude, kimi]
  reviewer: [kimi, claude]
"""


class Controlled(Fixture):
    """The fixture with a provider registry beside its database, as the server finds it."""

    def setUp(self):
        super().setUp()
        (self.path.parent / "providers.yaml").write_text(REGISTRY)


class TheDocument(Fixture):
    def test_the_month_is_sd_usage_s_own_document(self):
        doc = self.doc()["usage"]
        self.assertEqual(doc, {"error": "", **usage.read(self.connection, month=None, now=NOW).document()})
        self.assertEqual({b["name"]: (b["spent"], b["cap"]) for b in doc["bills"]}, {"capped": (80.0, 100.0), "plan": (0.0, None)})

    def test_a_month_asked_is_that_months_read_and_the_badge_keeps_this_month(self):
        """pack sd:7524: ?month= reads any month, as Operations Usage's picker did; the badge stays on this month."""
        self.cost("run", "2026-08-10T10:00:00Z", provider="kimi", bill="capped", role="author", usd=120)
        doc = metrics_screen.document(self.connection, now=NOW, month="2026-08")
        past = usage.read(self.connection, month="2026-08", now=NOW).document()
        self.assertEqual({k: v for k, v in doc["usage"].items() if k != "meter"},
                         {"error": "", **{k: v for k, v in past.items() if k != "meter"}})
        self.assertEqual(({b["name"]: b["spent"] for b in doc["usage"]["bills"]}, doc["usage"]["today"]),
                         ({"capped": 120.0, "plan": 0.0}, None))
        self.assertEqual(doc["attention"], self.doc()["attention"])
        # August's own meter would light other lamps; the badge stays on this month's.
        self.assertNotEqual(metrics_screen._attention({**doc["usage"], "meter": past["meter"]}, doc["trend"]),
                            doc["attention"])
        self.assertEqual(metrics_screen.document(self.connection, now=NOW, month="soon")["usage"],
                         {"error": "SdDbError: a month is YYYY-MM, not 'soon'"})

    def test_a_past_month_keeps_the_live_budget_windows(self):
        """Lead review of 960414d82: the week lamps and Budget used, now read `usage.meter`; a past month had swapped
        them for that month's last readings. Only the history follows ?month=."""
        now_meter = self.doc()["usage"]["meter"]
        self.assertTrue(now_meter)
        # The fixture's 2026-08-30 reading is August's last; the live one is 2026-09-06's.
        self.assertEqual(metrics_screen.document(self.connection, now=NOW, month="2026-08")["usage"]["meter"], now_meter)
        with mock.patch.object(metrics_screen, "slow", side_effect=sqlite3.OperationalError("database is locked")):
            failed = metrics_screen.document(self.connection, now=NOW, month="2026-08")["usage"]
        self.assertEqual((failed["error"], failed["meter"]), ("", []))
        self.assertIn("database is locked", failed["meter_error"])

    def test_a_past_month_carries_the_stale_flag_of_the_kept_meter(self):
        """pack sd:7556: a past month shows the kept snapshot's meter, so a stale snapshot makes it stale too, and
        metrics.js lights no week lamp from it. A fresh snapshot leaves the past month unflagged."""
        live = metrics_screen.slow(self.connection, "usage", now=NOW)
        for answer, flags in ((dict(live, observed="2026-09-06T11:31:00Z", stale="database is locked"),
                               {"observed": "2026-09-06T11:31:00Z", "stale": "database is locked"}),
                              (dict(live, observed="2026-09-06T11:59:00Z"), {})):
            kept = lambda name, a=answer: a if name == "usage" else metrics_screen.slow(self.connection, name, now=NOW)  # noqa: E731
            past = metrics_screen.document(self.connection, now=NOW, kept=kept, month="2026-08")["usage"]
            self.assertEqual(past["meter"], live["meter"])
            self.assertEqual({key: past[key] for key in ("observed", "stale") if key in past}, flags)

    def test_the_trend_is_the_last_7_day_window_reading_of_each_of_seven_days(self):
        trend = self.doc()["trend"]
        self.assertEqual((trend["days"][0], trend["days"][-1], len(trend["days"])), ("2026-08-31", "2026-09-06", 7))
        self.assertEqual([(s["provider"], [p[:2] for p in s["points"]]) for s in trend["series"]],
                         [("claude", [["2026-09-01", 30.0], ["2026-09-06", 80.0]])])

    def test_age_counts_open_items_and_splits_out_ready_to_send(self):
        buckets = self.doc()["age"]["buckets"]
        self.assertEqual(sum(b["ready_to_send"] + b["other"] for b in buckets), 2)
        self.assertEqual([(b["label"], b["ready_to_send"]) for b in buckets if b["ready_to_send"]], [("3-6d", 1)])
        self.assertEqual([b["key"] for b in buckets], [b.key for b in reads.age_histogram([])])

    def test_age_counts_no_scheduled_occurrence_until_it_opens(self):
        """sd:5670: Tasks lists a scheduled occurrence (pack sd:5648) once it opens, so age counts it only then."""
        self.schedule(self.item("Water the plants", kind="task", repo=self.repo()), "2026-09-09")
        count = lambda: sum(b["ready_to_send"] + b["other"] for b in self.doc()["age"]["buckets"])  # noqa: E731
        self.assertEqual(count(), 2)
        with before_5648():
            self.assertEqual(count(), 3)

    def test_age_counts_no_idea(self):
        """pack sd:6743: the histogram's bars open Tasks, which lists no idea kind, so age counts none."""
        for kind in ("idea", "work-idea", "personal-idea"):
            self.item(f"A {kind}", kind=kind, **({} if kind != "idea" else {"repo": self.repo()}))
        self.assertEqual(sum(b["ready_to_send"] + b["other"] for b in self.doc()["age"]["buckets"]), 2)

    def test_a_part_that_fails_is_its_error_and_the_rest_still_answer(self):
        with mock.patch.object(reads, "scorecard", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.doc()
        self.assertEqual(doc["scorecard"], {"error": "OperationalError: database is locked"})
        self.assertEqual(doc["trend"]["error"], "")
        self.assertEqual(doc["usage"]["error"], "")

    def test_the_slow_parts_come_from_the_kept_reads_with_when_they_were_observed(self):
        """sd:4955: the month and the week numbers take seconds; the server keeps them, so the request reads none."""
        kept = {name: metrics_screen.slow(self.connection, name, now=NOW) for name in metrics_screen.SLOW}
        answers = {"usage": dict(kept["usage"], observed="2026-09-06T11:59:00Z"),
                   "numbers": dict(kept["numbers"], observed="2026-09-06T11:58:00Z", stale="git did not answer")}
        with mock.patch.object(usage, "read", side_effect=AssertionError("usage.read ran")), \
                mock.patch.object(reads, "weekly_numbers", side_effect=AssertionError("weekly_numbers ran")):
            doc = metrics_screen.document(self.connection, now=NOW, kept=answers.__getitem__)
        self.assertEqual(doc["usage"], answers["usage"])
        self.assertEqual(doc["numbers"], answers["numbers"])
        self.assertEqual(kept["usage"], self.doc()["usage"])
        self.assertEqual(kept["numbers"], self.doc()["numbers"])

    def test_a_kept_part_with_no_reading_is_its_error_and_a_stale_month_lights_no_lamp(self):
        def kept(name):
            raise sqlite3.OperationalError("database is locked")

        doc = metrics_screen.document(self.connection, now=NOW, kept=kept)
        self.assertEqual((doc["usage"], doc["numbers"]), ({"error": "OperationalError: database is locked"},) * 2)
        stale = dict(self.doc()["usage"], observed="2026-09-06T11:31:00Z", stale="database is locked")
        self.assertEqual(metrics_screen._attention(stale, self.doc()["trend"]),
                         {"state": "unknown", "n": 0, "what": "readings not read"})

    def test_the_panels_no_reader_covers_are_named_with_their_reason(self):
        unread = {u["id"]: u["reason"] for u in self.doc()["unread"]}
        self.assertEqual(sorted(unread), ["model"])
        self.assertTrue(all(unread.values()))

    def test_no_registry_is_the_controls_error_and_the_rest_still_answer(self):
        doc = self.doc()
        self.assertEqual(doc["controls"], {"error": f"RegistryError: no provider registry at {self.path.parent.resolve() / 'providers.yaml'}"})
        self.assertEqual(doc["usage"]["error"], "")


#: sd-ship's refusal texts, as the lane keeps them in an entry's `reason` (sd:5473).
GATE = "the repository gate failed after the review cleared; the review pass is kept: sd-check fail (suite leg-shared)"
MERGE_GATE = "repo.ci is local and sd/local-gate is failure on 0123abcd: sd-check failed"
REVIEW = "local review blocking: 1/1 completed; the gate did not run, so no test has passed at this head: high x.py:1"
CONFLICT = "merging origin/main into b1 conflicts; the branch is unchanged"


def lane_run(connection, entry, status, finished, *, repository="example/alpha", item=1, head=None, expected=None,
             reason=None, step="prepare", enqueued=None, code=None):
    """One lane entry's latest checkpoint, as sd-ship's lane writes it: a failed entry names no `head`."""
    body = {"id": entry, "item": item, "title": f"Item {item}", "repository": repository, "status": status, "step": step,
            "expected_head": expected or head or "e" * 40, "head": head, "finished_at": finished, "reason": reason,
            "enqueued_at": enqueued, **({"code": code} if code else {})}
    connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                       (f"lane:v1:{repository}:{entry}", finished or NOW, json.dumps(body)))
    connection.commit()


SHARD = ("the repository gate failed again at a9f4ee0d700b; its review pass stays kept: check (exit 2): "
         "failed shard dashboard.tests.test_source_intake: 44s exit=1")
TWO_SHARDS = ("sd-check fail (precheck pass, check fail: shard tests.sd_db.test_runner_exec: 10s exit=1; "
              "shard dashboard.tests.test_source_intake: 24s exit=1) " + "the repository gate failed after the review")
TARGET = "the repository gate failed after the review cleared: sd-check fail (check fail: make target test-workspace)"


class ThePrWaits(Fixture):
    """pack sd:7859: the PR waits part, from each item's PR lane notes."""

    def pr(self, n, *lanes, repo="example/system", author="1", requested=(), observed=True):
        from sd_db import contributions
        from sd_db.writes import add_note, record_state

        url = f"https://github.com/{repo}/pull/{n}"
        item = contributions.capture(self.connection, title=f"PR {n}", changes={"pull_url": url}, who="test")["item"]["id"]
        for at, lane, state in lanes:
            add_note(self.connection, item, "comment", f"PR lane: {lane} · {state} · {url}",
                     session="contribution-observer", timestamp=at)
        if observed:
            record_state(self.connection, "checkpoint", key="contribution:github:" + url, body={"observation": {
                "operator": {"id": "1", "login": "operator"}, "author": {"id": author, "login": "someone"},
                "requested_reviewers": list(requested), "reviews": []}})
            self.connection.commit()
        return item

    def test_totals_split_on_you_and_on_reviewers_with_the_median_first_pr_to_merge_and_the_slowest(self):
        merged = self.pr(1, ("2026-09-06T08:00:00Z", "awaiting_them", "open"), ("2026-09-06T09:00:00Z", "awaiting_you", "open"),
                         ("2026-09-06T09:30:00Z", "merged", "merged"))
        waiting = self.pr(2, ("2026-09-06T10:00:00Z", "awaiting_you", "open"))
        prs = self.doc()["prs"]
        self.assertEqual({k: prs[k] for k in ("error", "items", "open", "on_you", "on_reviewers", "merged", "to_merge")},
                         {"error": "", "items": 2, "open": 1, "on_you": 1800 + 7200, "on_reviewers": 3600, "merged": 1,
                          "to_merge": 5400})
        self.assertEqual([(w["item"], w["on_you"], w["on_reviewers"], w["to_merge"], w["open"], w["on"])
                          for w in prs["repos"][0]["prs"]],
                         [(waiting, 7200, 0, None, True, "you"), (merged, 1800, 3600, 5400, False, None)])

    def test_only_the_operators_pull_requests_count_and_an_unread_one_is_named(self):
        """sd:8084: authored or asked by name counts; a team-only request, someone else's or an unread one does not."""
        at = ("2026-09-06T10:00:00Z", "awaiting_them", "open")
        mine = self.pr(1, at)
        asked = self.pr(2, at, repo="example/other", author="2", requested=["Operator"])
        self.pr(3, at, author="2")  # a team request leaves requested_reviewers empty
        unread = self.pr(4, at, author="2", observed=False)
        prs = self.doc()["prs"]
        self.assertEqual(prs["items"], 2)
        self.assertEqual({r["repo"]: [w["item"] for w in r["prs"]] for r in prs["repos"]},
                         {"example/system": [mine], "example/other": [asked]})
        self.assertEqual(prs["unknown"], [f"sd:{unread}: no reading of its author and requested reviewers"])

    def test_repos_carry_their_own_totals_most_wait_first(self):
        """sd:8084 (note 48655): one group per repository, with its totals, ordered by total wait."""
        self.pr(1, ("2026-09-06T11:00:00Z", "awaiting_you", "open"))
        self.pr(2, ("2026-09-06T08:00:00Z", "awaiting_them", "open"), ("2026-09-06T09:00:00Z", "merged", "merged"),
                repo="example/other")
        self.pr(3, ("2026-09-06T10:00:00Z", "awaiting_them", "open"), repo="example/other")
        repos = self.doc()["prs"]["repos"]
        self.assertEqual([(r["repo"], r["items"], r["open"], r["on_you"], r["on_reviewers"], r["merged"], r["to_merge"])
                          for r in repos],
                         [("example/other", 2, 1, 0, 3600 + 7200, 1, 3600), ("example/system", 1, 1, 3600, 0, 0, None)])

    def test_a_failed_read_is_the_parts_error(self):
        from sd_db import contributions

        with mock.patch.object(contributions, "pr_waits", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(self.doc()["prs"], {"error": "OperationalError: database is locked"})


class TheHealth(Fixture):
    """The weekly KISS audit's numbers (pack sd:6744). NOW is 2026-09-06T12:00Z; the window is 14 days, and last
    week's is the 14 days that ended 2026-08-30T12:00Z."""

    def build_run(self, ident, created, outcome, detail=None, ended=None):
        item = self.item(f"Build {ident}", kind="task")
        assignment = self.assignment(item, status="running")
        self.connection.execute("UPDATE assignment SET scope = 'build' WHERE id = ?", (assignment,))
        self.connection.execute(
            "INSERT INTO runner_run (id, assignment, run, branch, owner, work_path, retained_path, created_at,"
            " updated_at, outcome, detail) VALUES (?, ?, 1, 'build/sd-1', 'fixture', '/wt', '/wt', ?, ?, ?, ?)",
            (ident, assignment, created, ended or created, outcome, detail))
        self.connection.commit()

    def test_the_lane_splits_each_repositorys_failures_by_class(self):
        c = self.connection
        lane_run(c, "a1", "merged", "2026-09-06T10:00:00Z", enqueued="2026-09-06T09:50:00Z")
        lane_run(c, "a2", "failed", "2026-09-05T10:00:00Z", reason=REVIEW)
        lane_run(c, "a3", "failed", "2026-09-04T10:00:00Z", reason=SHARD)
        lane_run(c, "a4", "failed", "2026-09-03T10:00:00Z", reason=CONFLICT)
        lane_run(c, "a5", "cancelled", "2026-08-25T10:00:00Z")      # in both windows: other
        lane_run(c, "a6", "running", None)                           # not ended: not counted
        lane_run(c, "a7", "merged", "2026-08-20T10:00:00Z")          # last week's window only
        found = metrics_screen.health(c, now=NOW)
        self.assertEqual((found["days"], found["now"]["since"], found["last_week"]["until"]),
                         (14, "2026-08-23T12:00:00Z", "2026-08-30T12:00:00Z"))
        self.assertEqual(found["now"]["lane"], [{"repository": "example/alpha", "entries": 5, "review": 1, "gate": 1,
                                                 "other": 2, "failed_percent": 80}])
        self.assertEqual(found["last_week"]["lane"], [{"repository": "example/alpha", "entries": 2, "review": 0,
                                                       "gate": 0, "other": 1, "failed_percent": 50}])

    def test_gate_failures_count_each_named_suite_in_its_repository(self):
        c = self.connection
        lane_run(c, "a1", "failed", "2026-09-05T10:00:00Z", reason=SHARD)
        lane_run(c, "a2", "failed", "2026-09-05T11:00:00Z", reason=TWO_SHARDS)
        lane_run(c, "a3", "failed", "2026-09-05T12:00:00Z", reason=TARGET, repository="example/beta")
        lane_run(c, "a4", "failed", "2026-09-05T13:00:00Z", reason=MERGE_GATE)
        lane_run(c, "a5", "failed", "2026-09-05T14:00:00Z", reason=REVIEW + " shard x.y: 1s exit=1")
        suites = metrics_screen.health(c, now=NOW)["now"]["gate_suites"]
        self.assertEqual([(s["repository"], s["suite"], s["failures"]) for s in suites],
                         [("example/alpha", "dashboard.tests.test_source_intake", 2),
                          ("example/alpha", "tests.sd_db.test_runner_exec", 1),
                          ("example/beta", "test-workspace", 1),
                          ("example/alpha", "unnamed", 1)])

    def test_builds_count_done_over_ended_and_group_the_reap_reasons(self):
        self.build_run("r1", "2026-09-05T10:00:00+00:00", "done")
        self.build_run("r2", "2026-09-04T10:00:00+00:00", "blocked", "build run r2 ended at step reviewing: the review "
                       "gave no answer twice at fec49dba2b14; see /x")
        self.build_run("r3", "2026-09-03T10:00:00+00:00", "blocked", "build run r3 ended at step reviewing: the review "
                       "gave no answer twice at b9bade984589; see /y")
        self.build_run("r4", "2026-09-02T10:00:00+00:00", "timed_out", "a child ran past 3600 s; its group was ended")
        self.build_run("r5", "2026-09-02T11:00:00+00:00", None)                    # still running: not ended
        self.build_run("r6", "2026-08-01T10:00:00+00:00", "interrupted")           # before both windows
        builds = metrics_screen.health(self.connection, now=NOW)["now"]["builds"]
        self.assertEqual(builds, {"done": 1, "total": 4, "reasons": [
            {"reason": "reviewing: the review gave no answer twice at N", "runs": 2},
            {"reason": "a child ran past N s", "runs": 1}]})

    def test_lead_time_is_enqueue_to_merge_median_and_p90(self):
        for n, minutes in enumerate((10, 20, 30, 40, 100)):
            finished = datetime.datetime(2026, 9, 5, 10, n, tzinfo=datetime.timezone.utc)
            enqueued = finished - datetime.timedelta(minutes=minutes)
            lane_run(self.connection, f"m{n}", "merged", finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     enqueued=enqueued.strftime("%Y-%m-%dT%H:%M:%SZ"))
        lane_run(self.connection, "f1", "failed", "2026-09-05T11:00:00Z", reason=REVIEW,
                 enqueued="2026-09-01T11:00:00Z")                            # not merged: no lead time
        lead = metrics_screen.health(self.connection, now=NOW)["now"]["lead_minutes"]
        self.assertEqual(lead, {"merged": 5, "median": 30, "p90": 100})

    def test_a_build_run_counts_in_the_window_it_ended_in(self):
        # pack sd:7005: a run that started before the window and ended in
        # it was left out; one that ended after the window was counted.
        # Last week's window is 2026-08-16T12:00Z to 2026-08-30T12:00Z.
        self.build_run("r1", "2026-08-10T10:00:00+00:00", "done", ended="2026-08-17T10:00:00+00:00")
        self.build_run("r2", "2026-08-29T10:00:00+00:00", "blocked", ended="2026-08-31T10:00:00+00:00")
        builds = metrics_screen.health(self.connection, now=NOW)["last_week"]["builds"]
        self.assertEqual((builds["done"], builds["total"]), (1, 1))

    def test_an_entry_with_a_bad_stamp_is_unreadable_not_fatal(self):
        # pack sd:7005: a null finished_at or a bad enqueued_at raised out
        # of health instead of skipping the one entry.
        lane_run(self.connection, "a1", "merged", "2026-09-05T10:00:00Z", enqueued="not a time")
        lane_run(self.connection, "a2", "failed", None, reason=REVIEW)
        lane_run(self.connection, "a3", "merged", "2026-09-05T11:00:00Z", enqueued="2026-09-05T10:50:00Z")
        found = metrics_screen.health(self.connection, now=NOW)
        self.assertEqual(found["unreadable"], 2)
        self.assertEqual(found["now"]["lead_minutes"], {"merged": 1, "median": 10, "p90": 10})

    def test_sd_reports_health_prints_the_document(self):
        with mock.patch.object(sys, "path", [str(V2.parents[2] / "bin"), *sys.path]):
            import sd_controls
        found = {"days": 14, "unreadable": 0, "now": {}, "last_week": {}}
        parser = argparse.ArgumentParser()
        sd_controls.register(parser.add_subparsers(dest="group"))
        arguments = parser.parse_args(["reports", "health", "--json"])
        output = io.StringIO()
        with mock.patch.object(metrics_screen, "health", return_value=found), \
                mock.patch.object(sd_controls.sd_handoff_rows, "connect"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(arguments.handler(arguments), 0)
        self.assertEqual(json.loads(output.getvalue()), found)


class TheGateRuns(Fixture):
    """CI health and the flaky heads read the lane's entries (sd:5473). NOW is 2026-09-06T12:00Z; the range is 7 days.
    Today lists the flaky heads (pack sd:7663); Metrics carries none."""

    def test_ci_health_counts_each_repositorys_gate_runs_in_the_range(self):
        c = self.connection
        lane_run(c, "a1", "merged", "2026-09-06T10:00:00Z", head="a" * 40)
        lane_run(c, "a2", "prepared", "2026-09-05T10:00:00Z", head="b" * 40)
        lane_run(c, "a3", "failed", "2026-09-04T10:00:00Z", expected="c" * 40, reason=GATE, item=3)
        lane_run(c, "a4", "failed", "2026-09-06T11:00:00Z", reason=REVIEW, item=4)
        lane_run(c, "a5", "failed", "2026-09-03T10:00:00Z", reason=CONFLICT)
        lane_run(c, "a6", "failed", "2026-09-02T10:00:00Z", reason=None, step="runner")
        lane_run(c, "a7", "failed", "2026-09-01T10:00:00Z", reason=MERGE_GATE, step="merge", item=7)
        lane_run(c, "a8", "merged", "2026-08-29T10:00:00Z", head="d" * 40)       # before the range
        lane_run(c, "a9", "running", None)                                       # not ended
        lane_run(c, "a10", "cancelled", "2026-09-05T10:00:00Z")                  # no gate ran
        lane_run(c, "b1", "merged", "2026-09-05T09:00:00Z", repository="example/beta", head="f" * 40)
        ci = self.doc()["ci"]
        self.assertEqual((ci["error"], ci["days"], ci["since"], ci["unreadable"]), ("", 7, "2026-08-30T12:00:00Z", 0))
        rows = {r["repository"]: r for r in ci["repos"]}
        self.assertEqual([r["repository"] for r in ci["repos"]], ["example/alpha", "example/beta"])
        alpha = rows["example/alpha"]
        self.assertEqual({k: alpha[k] for k in ("passed", "gate", "review", "other", "unknown")},
                         {"passed": 2, "gate": 2, "review": 1, "other": 1, "unknown": 1})
        self.assertEqual(alpha["rate"], 0.5)
        self.assertEqual({k: alpha["newest"][k] for k in ("item", "at", "kind")}, {"item": 4, "at": "2026-09-06T11:00:00Z", "kind": "review"})
        self.assertEqual({k: alpha["newest_gate"][k] for k in ("item", "at", "step")}, {"item": 3, "at": "2026-09-04T10:00:00Z", "step": "prepare"})
        self.assertEqual((rows["example/beta"]["passed"], rows["example/beta"]["rate"], rows["example/beta"]["newest"]), (1, 1.0, None))

    def test_a_head_that_failed_its_gate_and_later_passed_at_that_head_is_flaky(self):
        c, h = self.connection, "1" * 40
        lane_run(c, "f1", "failed", "2026-09-04T10:00:00Z", expected=h, reason=GATE, item=9)
        lane_run(c, "f2", "failed", "2026-09-04T12:00:00Z", expected=h, reason=GATE, item=9)
        lane_run(c, "f3", "merged", "2026-09-04T14:00:00Z", head=h, item=9)
        # Not flaky: a pass in another repository, a new commit, a pass before the failure, a review stop.
        lane_run(c, "g1", "merged", "2026-09-04T15:00:00Z", repository="example/beta", head=h, item=9)
        lane_run(c, "g2", "failed", "2026-09-03T10:00:00Z", expected="2" * 40, reason=GATE, item=10)
        lane_run(c, "g3", "merged", "2026-09-03T12:00:00Z", head="3" * 40, expected="2" * 40, item=10)
        lane_run(c, "g4", "merged", "2026-09-02T10:00:00Z", head="4" * 40, item=11)
        lane_run(c, "g5", "failed", "2026-09-02T12:00:00Z", expected="4" * 40, reason=GATE, item=11)
        lane_run(c, "g6", "failed", "2026-09-01T10:00:00Z", expected="5" * 40, reason=REVIEW, item=12)
        lane_run(c, "g7", "merged", "2026-09-01T12:00:00Z", head="5" * 40, item=12)
        self.assertEqual(metrics_screen.flaky(c, NOW)[0], [{"repository": "example/alpha", "item": 9, "title": "Item 9", "head": h,
                                                            "failed": ["2026-09-04T10:00:00Z", "2026-09-04T12:00:00Z"],
                                                            "passed": "2026-09-04T14:00:00Z", "status": "merged"}])

    def test_metrics_carries_no_flaky_head_and_its_badge_counts_none(self):
        """pack sd:7663: the flaky heads moved to Today's Needs you; Metrics keeps CI health alone."""
        c, h = self.connection, "1" * 40
        lane_run(c, "f1", "failed", "2026-09-04T10:00:00Z", expected=h, reason=GATE, item=9)
        lane_run(c, "f2", "merged", "2026-09-04T14:00:00Z", head=h, item=9)
        doc = self.doc()
        self.assertNotIn("flaky", doc["ci"])
        self.assertEqual(doc["attention"], metrics_screen._attention(doc["usage"], doc["trend"]))
        page = (V2 / "metrics.html").read_text(encoding="utf-8")
        self.assertNotIn('id="flaky-', page)
        self.assertNotIn(".flaky", METRICS_JS)

    def test_a_lane_read_that_fails_is_its_error_and_never_zeros(self):
        with mock.patch.object(reads, "lane_entries", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.doc()
        self.assertEqual(doc["ci"], {"error": "OperationalError: database is locked"})
        self.assertEqual(doc["usage"]["error"], "")

    def test_an_entry_that_is_not_a_lane_entry_is_counted_unreadable_not_a_run(self):
        lane_run(self.connection, "a1", "merged", "2026-09-06T10:00:00Z", head="a" * 40)
        for key, body in (("x", "not json"), ("y", "[1]"), ("z", json.dumps({"status": "merged", "finished_at": "yesterday"}))):
            self.connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                                    (f"lane:v1:example/alpha:{key}", NOW, body))
        self.connection.commit()
        ci = self.doc()["ci"]
        self.assertEqual((ci["unreadable"], [r["passed"] for r in ci["repos"]]), (3, [1]))

    def test_an_empty_range_has_no_repository_and_no_flaky_head(self):
        lane_run(self.connection, "old", "merged", "2026-08-01T10:00:00Z", head="a" * 40)
        ci = self.doc()["ci"]
        self.assertEqual((ci["error"], ci["repos"], ci["unreadable"]), ("", [], 0))
        self.assertEqual(metrics_screen.flaky(self.connection, NOW), ([], 0))

    def test_the_gate_phrases_and_codes_are_the_pinned_packs_refusals(self):
        # A pack that rewords a gate refusal would file every gate failure as "other": the pinned source must say each.
        bin_dir = Path(reads.__file__).resolve().parents[2] / "bin"
        source = "\n".join(p.read_text(encoding="utf-8") for p in sorted(bin_dir.glob("sd_*.py")))
        self.assertIn('raise Refusal(f"the repository gate failed', source)
        self.assertIn('raise Refusal(f"repo.ci is local and {CONTEXT} is failure on', source)
        self.assertIn('LOCAL_GATE_CONTEXT = "sd/local-gate"', source)
        for phrase in ("the repository gate failed", "sd/local-gate is failure on"):
            self.assertRegex(phrase, metrics_screen.GATE_FAILED)
        # pack sd:7412: a code is a refusal's literal `code="..."` or a named constant, as `gate_failed` now is.
        for code in (*metrics_screen.GATE_CODES, metrics_screen.SLOT_WAIT):
            self.assertRegex(source, rf'(?m)code="{code}"|^[A-Z_]+ = "{code}"$')

    def test_a_slot_wait_that_ran_out_ran_no_gate(self):
        """pack sd:7412: a lane before it fails a `gate_slot_wait` entry with the gate's refusal text, but no check
        ran, so it is no gate run: it does not lower the pass rate."""
        lane_run(self.connection, "a1", "merged", "2026-09-06T10:00:00Z", head="a" * 40)
        lane_run(self.connection, "a2", "failed", "2026-09-05T10:00:00Z", reason=GATE, code="gate_slot_wait")
        alpha = self.doc()["ci"]["repos"][0]
        self.assertEqual((alpha["gate"], alpha["other"], alpha["rate"]), (0, 1, 1.0))


class TheSixMonths(Fixture):
    """pack sd:7208: the 6mo range. NOW is 2026-09-06, so its months are 2026-04 to 2026-09, this one to date."""

    def setUp(self):
        super().setUp()
        self.cost("run", "2026-07-15T10:00:00Z", provider="kimi", bill="capped", role="author", usd=5)
        self.cost("meter", "2026-05-20T10:00:00Z", provider="claude", bill="plan", window_minutes=10080, used_percent=50)
        self.cost("run", "2026-08-20T10:00:00Z", provider="claude", bill=None, role="author", usd=7)

    def ranged(self):
        return metrics_screen.document(self.connection, now=NOW, span="6mo")

    def test_spend_is_each_months_dollars_by_bill_and_a_month_with_no_rows_is_marked(self):
        spend = self.ranged()["ranged"]["spend"]
        self.assertEqual(spend["bills"], [{"name": "capped", "cost_basis": "company"}, {"name": "plan", "cost_basis": "plan"}])
        self.assertEqual([(m["month"], m["rows"], m["spent"], m["unbilled"], m["bills"]) for m in spend["months"]], [
            ("2026-04", False, 0.0, None, {"capped": 0.0, "plan": 0.0}),
            ("2026-05", False, 0.0, None, {"capped": 0.0, "plan": 0.0}),   # a meter reading is not a dollar row
            ("2026-06", False, 0.0, None, {"capped": 0.0, "plan": 0.0}),
            ("2026-07", True, 5.0, None, {"capped": 5.0, "plan": 0.0}),
            ("2026-08", True, 7.0, 7.0, {"capped": 0.0, "plan": 0.0}),    # a row on no bill is a row and its dollars
            ("2026-09", True, 80.0, None, {"capped": 80.0, "plan": 0.0})])

    def test_an_open_hold_on_no_bill_is_no_row_in_an_empty_month(self):
        """pack sd:7229: the read's roles carry every open hold whatever its month, so one on no bill made each empty
        month read $0 spent, not no rows."""
        self.cost("reserved", "2026-05-02T10:00:00Z", provider="claude", bill=None, role="author", usd=2, call_id="old")
        months = self.ranged()["ranged"]["spend"]["months"]
        self.assertEqual([(m["month"], m["rows"], m["unbilled"]) for m in months], [
            ("2026-04", False, None), ("2026-05", False, None), ("2026-06", False, None),
            ("2026-07", True, None), ("2026-08", True, 7.0), ("2026-09", True, None)])

    def test_each_month_is_sd_usage_s_own_read_and_there_are_six(self):
        months = []
        real = usage.read

        def counted(connection, *, month=None, now=None):
            months.append(month)
            return real(connection, month=month, now=now)

        with mock.patch.object(usage, "read", side_effect=counted):
            spend = self.ranged()["ranged"]["spend"]
        self.assertEqual([m for m in months if m], ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"])
        for month in spend["months"]:
            read = real(self.connection, month=month["month"], now=NOW).document()
            self.assertEqual((month["spent"], month["unbilled"]), (read["spent"], read["unbilled"]))

    def test_the_months_cross_a_year(self):
        doc = metrics_screen.document(self.connection, now="2027-02-10T12:00:00Z", span="6mo")["ranged"]
        self.assertEqual((doc["months"], doc["since"]), (["2026-09", "2026-10", "2026-11", "2026-12", "2027-01", "2027-02"],
                                                          "2026-09-01T00:00:00Z"))

    def test_providers_and_ci_read_the_six_months_and_the_lamps_keep_7_days(self):
        lane_run(self.connection, "old", "failed", "2026-08-01T10:00:00Z", expected="a" * 40, reason=GATE)
        lane_run(self.connection, "again", "merged", "2026-08-02T10:00:00Z", head="a" * 40)
        doc = self.ranged()
        week = self.doc()
        scorecard = {r["provider"]: r["usd"] for r in doc["ranged"]["scorecard"]["rows"]}
        self.assertEqual(scorecard["kimi"], 85.0)
        self.assertEqual({r["provider"]: r["usd"] for r in doc["scorecard"]["rows"]}["kimi"], 80.0)
        ci = doc["ranged"]["ci"]
        self.assertEqual((ci["since"], [(r["repository"], r["passed"], r["gate"]) for r in ci["repos"]]),
                         ("2026-04-01T00:00:00Z", [("example/alpha", 1, 1)]))
        self.assertEqual((doc["ci"], doc["attention"], doc["range"], week["range"]), (week["ci"], week["attention"], "6mo", "7d"))
        self.assertNotIn("ranged", week)

    def test_a_range_part_that_fails_is_its_error_and_the_rest_still_answer(self):
        with mock.patch.object(reads, "scorecard", side_effect=sqlite3.OperationalError("database is locked")):
            doc = self.ranged()
        self.assertEqual(doc["ranged"]["scorecard"], {"error": "OperationalError: database is locked"})
        self.assertEqual((doc["ranged"]["spend"]["error"], doc["ranged"]["ci"]["error"]), ("", ""))

    def test_an_unknown_range_is_refused(self):
        with self.assertRaises(ValueError):
            metrics_screen.document(self.connection, now=NOW, span="30d")


class TheControls(Controlled):
    def test_the_controls_carry_the_revision_the_routes_check_and_each_bills_write(self):
        controls = self.doc()["controls"]
        self.assertEqual(controls["revision"], provider_controls.snapshot(self.connection)["revision"])
        self.assertEqual(controls["orders"], {"author": ["claude", "kimi"], "reviewer": ["kimi", "claude"]})
        self.assertEqual([(p["name"], p["enabled"]) for p in controls["providers"]], [("claude", True), ("kimi", True)])
        self.assertEqual({b["name"]: (b["cap_usd_month"], b["write"], b["why"]) for b in controls["bills"]},
                         {"capped": (100.0, "set", ""), "plan": (None, "set", ""),
                          "sub": (None, "", "a 'start' entry is billed to it, so no cap can be set")})

    def test_a_legacy_cap_on_a_start_bill_takes_the_clear_alone(self):
        self.doc()  # the first read seeds the `sub` row
        with self.connection:
            self.connection.execute("UPDATE bill SET cap_usd_month = 5.0 WHERE name = 'sub'")
        controls = self.doc()["controls"]
        sub = next(b for b in controls["bills"] if b["name"] == "sub")
        self.assertEqual((sub["cap_usd_month"], sub["write"]), (None, "clear"))
        self.assertIn("holds a cap of $5.00 that nothing enforces", sub["why"])
        self.assertEqual(len(controls["warnings"]), 1)

    def test_a_bill_name_the_route_does_not_take_has_no_write(self):
        self.assertEqual(metrics_screen._writes("a/b", set(), {}), ("", "its name is outside what the cap route accepts"))


class TheWrites(BrowserSession):
    """The revision the served `/api/metrics` read, posted back to the routes: the page's writes end to end."""

    def setUp(self):
        super().setUp()
        (self.path.parent / "providers.yaml").write_text(REGISTRY)

    def controls(self):
        status, _, body = self.request("/api/metrics", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        return json.loads(body)["controls"]

    def test_the_cap_lands_on_the_read_revision_and_a_second_post_of_it_is_stale(self):
        read = self.controls()
        status, _, answer = self.post("/api/bills/capped/cap", {"revision": read["revision"], "cap_usd_month": 50})
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.connection.execute("SELECT cap_usd_month FROM bill WHERE name = 'capped'").fetchone()[0], 50.0)
        before = self.snapshot()
        self.assertEqual(self.post("/api/bills/capped/cap", {"revision": read["revision"], "cap_usd_month": 60})[0], 409)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.controls()["revision"], answer["revision"])

    def test_configure_lands_on_the_read_revision_and_a_refusal_writes_nothing(self):
        read = self.controls()
        before = self.snapshot()
        refused = {"revision": read["revision"], "enabled": {"claude": True, "kimi": True}, "orders": {"author": ["kimi"], "reviewer": ["kimi"]}}
        self.assertEqual(self.post("/api/providers/configure", refused)[0], 400)
        self.assertEqual(self.snapshot(), before)
        status, _, answer = self.post("/api/providers/configure", {**refused, "orders": {"author": ["kimi", "claude"], "reviewer": ["claude", "kimi"]}})
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.controls()["orders"], {"author": ["kimi", "claude"], "reviewer": ["claude", "kimi"]})


class ThePage(BrowserSession):
    def test_the_route_answers_under_the_shared_policy(self):
        status, headers, body = self.request("/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], server.CSP)
        self.assertIn("<title>Metrics · system</title>", body)
        self.assertEqual(Refused(body).found, [])
        scripts = re.findall(r'<script src="/ui/([^"?]+)', body)
        self.assertEqual(scripts, ["theme.js", "markup.js", "annunciator-strip.js", "icons.js", "sections.js", "read.js", "metrics.js", "shell.js"])
        for path in re.findall(r'(?:src|href)="(/ui/[^"]+)"', body):
            self.assertEqual(self.request(path)[0], 200, path)

    def test_the_data_route_needs_a_session_and_takes_a_range_alone(self):
        self.assertEqual(self.request("/api/metrics")[0], 403)
        cookie = {"Cookie": self.cookie}
        for query in ("range=30", "range=6mo&x=1", "range=6mo&range=7d", "x=1"):
            self.assertEqual(self.request(f"/api/metrics?{query}", headers=cookie)[0], 400, query)
        status, _, body = self.request("/api/metrics", headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["numbers"]["error"], "")
        self.assertNotIn("ranged", json.loads(body))
        status, _, body = self.request("/api/metrics?range=6mo", headers=cookie)
        self.assertEqual((status, json.loads(body)["range"], len(json.loads(body)["ranged"]["months"])), (200, "6mo", 6))

    def test_the_data_route_takes_one_month(self):
        """pack sd:7524: the month Operations Usage's picker read, as ?month=YYYY-MM, alone or with the range."""
        cookie = {"Cookie": self.cookie}
        for query in ("month=soon", "month=2026-08&month=2026-07", "month=", "month=2026-8"):
            self.assertEqual(self.request(f"/api/metrics?{query}", headers=cookie)[0], 400, query)
        for query in ("month=2026-08", "range=6mo&month=2026-08"):
            status, _, body = self.request(f"/api/metrics?{query}", headers=cookie)
            self.assertEqual((status, json.loads(body)["usage"]["month"]), (200, "2026-08"), query)

    def test_the_rail_opens_metrics_at_its_page(self):
        self.assertEqual(v2.SECTIONS["Metrics"], "/metrics")
        self.assertNotIn("Metrics", v2.CLASSIC)


METRICS_SHELL = r"""
window.shell.row = () => null;
window.shell.pages = { Tasks: '/tasks' };
OUT.shown = []; window.shell.view = { show: id => { OUT.shown.push(id); return true; }, current: () => 'spend', count() {} };
"""


class TheScript(Controlled):
    """metrics.js against the document `metrics_screen` builds from the fixture database."""

    def run_page(self, body, doc=None, status=200, before=""):
        doc = doc if doc is not None else self.doc()
        script = (STAND_IN + before + MARKUP_JS + "\nconst mk = window.markup.html;\n" + SHELL + METRICS_SHELL + READ_SHELL
                  + f"\nvar DOC = {json.dumps(doc)}, STATUS = {status};\n"
                  + "ANSWER = (path, body) => path === '/api/metrics' ? [STATUS, DOC] : [404, { error: 'no answer' }];\n"
                  + METRICS_JS + "\nvar R = {};\n(async () => { try {\n(WIN_LISTENERS.DOMContentLoaded || []).forEach(f => f());\n"
                  + "(DOC_LISTENERS.DOMContentLoaded || []).forEach(f => f());\nawait flush();\n"
                  + body + "\n} catch (e) { OUT.error = String(e) + ' ' + e.stack; } })();\n"
                  + "function run() { OUT.R = R; OUT.toasts = OUT.toasts.map(t => [t.msg, !!t.undo]); return JSON.stringify(OUT); }\n")
        result = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", script],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertIsNone(out["error"])
        return out

    def test_every_command_is_registered_with_the_design_id_risk_and_key(self):
        out = self.run_page("R.reg = REG.map(c => [c.id, c.on, c.risk, c.key, c.executes]);")
        self.assertEqual(out["R"]["reg"], [["providers.configure", "provider", "undo", "f", True],
                                           ["bill.cap", "bill", "undo", "p", True]])

    def test_the_badge_rule_lights_what_the_lamps_light(self):
        # sd:3446: the rule is the server's, and every page's rail shows it; it must count the cells metrics.js lights.
        def lit(doc):
            lamps = self.run_page("R.lamps = ELS.annunciator.html;", doc)["R"]["lamps"]
            states = re.findall(r'data-cell="[\w-]+"[^>]*data-state="(\w+)"', lamps)
            worst = "warning" if "warning" in states else "caution" if "caution" in states else "ok"
            return worst, states.count(worst) if worst != "ok" else 0

        counted = []
        for used, bound in ((80, False), (100, True), (40, False), (None, True)):
            doc = self.doc()
            for meter in doc["usage"]["meter"]:
                meter["used_percent"] = used
            doc["usage"]["bound"] = [{"bill": "example"}] if bound else []
            doc["attention"] = metrics_screen._attention(doc["usage"], doc["trend"])
            self.assertEqual(lit(doc), (doc["attention"]["state"], doc["attention"]["n"]), (used, bound))
            counted.append(doc["attention"]["state"])
        self.assertEqual(counted, ["caution", "warning", "ok", "caution"])
        self.assertEqual(metrics_screen._attention({"error": "OSError: locked"}, {}),
                         {"state": "unknown", "n": 0, "what": "readings not read"})

    def test_each_cell_is_a_reading_or_unknown_with_its_reason(self):
        out = self.run_page("R.lamps = ELS.annunciator.html;")
        self.assertEqual(out["gets"], ["/api/metrics"])
        lamps = out["R"]["lamps"]
        self.assertRegex(lamps, r'data-cell="spend" data-ev="spend" data-view="spend" data-state="ok".*?<b>\$80.00</b>')
        self.assertRegex(lamps, r'data-cell="w-claude" data-ev="win:claude:10080" data-view="spend" data-state="caution".*?<b>80%</b> used')
        self.assertRegex(lamps, r'data-cell="ci" data-ev="ci" data-view="ci" data-state="unknown".*?no gate run')
        self.assertEqual(out["attention"][-1], {"state": "caution", "n": 1, "what": "readings past a threshold"})
        self.assertIsNone(out["states"][-1])

    def test_the_spend_lamp_counts_a_row_on_no_bill(self):
        """pack sd:7219: a start session's row may name no bill; the month lamp read it as $0.00."""
        self.cost("run", "2026-09-04T10:00:00Z", provider="claude", bill=None, role="author", usd=3)
        out = self.run_page("R.lamps = ELS.annunciator.html; open('spend'); R.det = ELS.details.html; R.note = ELS['bills-note'].textContent;")
        self.assertRegex(out["R"]["lamps"], r'data-cell="spend" data-ev="spend" data-view="spend" data-state="ok".*?<b>\$83.00</b>')
        self.assertIn("<dt>No bill</dt><dd>$3.00</dd>", out["R"]["det"])
        self.assertTrue(out["R"]["note"].startswith("Total $83.00 spent ($3.00 on no bill) and $0.00 held in 2026-09."), out["R"]["note"])

    def test_the_panels_are_the_shells_views(self):
        """sd:5080: Spend (spend, the week, usage), Providers, CI and Age are PAGE_VIEWS panels; Skills moved (sd:5881)."""
        views = re.search(r"window\.PAGE_VIEWS = (\[.*?\]);", METRICS_JS, re.S).group(1)
        self.assertEqual(re.findall(r"id: '([a-z]+)'", views), ["spend", "providers", "ci", "times", "age"])
        page = (V2 / "metrics.html").read_text(encoding="utf-8")
        panels = {m.group(1): re.findall(r'<section class="panel" id="([a-z-]+)"', m.group(2))
                  for m in re.finditer(r'<div id="view-([a-z]+)"(.*?)(?=<div id="view-|</main>)', page, re.S)}
        self.assertEqual(panels, {"spend": ["spend", "week", "usage"], "providers": ["scorecard"], "ci": ["ci"],
                                  "times": ["times", "prs"], "age": ["age"]})  # pack sd:7859: PR waits beside lane times

    def test_a_lamp_shows_the_view_that_holds_its_reading(self):
        out = self.run_page("""const lamp = (cell, ev, view) => ({ dataset: { cell, ev, view } });
for (const el of [lamp('ci', 'ci', 'ci'), lamp('spend', '', 'spend')])
  DOC_LISTENERS.click.forEach(f => f({ target: { closest: s => s === '[data-ev]' ? el : null } }));""")
        self.assertEqual(out["shown"], ["ci", "spend"], "a spend lamp that did not read still shows its view")

    def test_the_panels_draw_the_readings_and_name_what_is_not_read(self):
        out = self.run_page("""R.bills = ELS.bills.html; R.trends = ELS.trends.html; R.model = ELS.model.html;
R.sc = ELS['sc-body'].html; R.hist = ELS.hist.html;""")
        r = out["R"]
        self.assertRegex(r["bills"], r'data-ev="bill:capped".*?<rect class="fill caution" width="80.0".*?<span class="g-caution" aria-hidden="true">▲</span>\$80.00')
        self.assertRegex(r["bills"], r'data-ev="bill:plan".*?class="track hatch".*?▨ window')
        self.assertEqual(re.findall(r'data-ev="(t:[^"]+)"', r["trends"]), ["t:claude:2026-09-01", "t:claude:2026-09-06"])
        self.assertIn("<b>▨ Not read.</b> the cost ledger records bill and provider, not model", r["model"])
        self.assertEqual(re.findall(r'data-ev="sc:([^"]+)"', r["sc"]), ["claude", "kimi"])
        self.assertIn('aria-label="3-6d: 1 items, 1 ready to send"', r["hist"])
        # sd:5474: a bar links to Tasks' list at its bucket, open items only, under Realm All (sd:5753): the scope the bar counts.
        self.assertIn('<a class="hbar" href="/tasks?view=list&age=3&active=1&realm=all" aria-label="3-6d', r["hist"])

    def lane(self):
        c, h = self.connection, "1" * 40
        lane_run(c, "f1", "failed", "2026-09-04T10:00:00Z", expected=h, reason=GATE, item=9)
        lane_run(c, "f2", "merged", "2026-09-04T14:00:00Z", head=h, item=9)
        lane_run(c, "f3", "failed", "2026-09-05T10:00:00Z", reason=REVIEW, item=8)
        lane_run(c, "b1", "merged", "2026-09-05T09:00:00Z", repository="example/beta", head="f" * 40, item=7)

    def test_ci_health_draws_each_repositorys_gate_runs(self):
        """sd:5473: the lane's gate runs per repository; a flaky head lights no Metrics lamp (pack sd:7663)."""
        self.lane()
        doc = self.doc()
        self.assertEqual(doc["attention"], {"state": "caution", "n": 1, "what": "readings past a threshold"})
        out = self.run_page("""R.ci = ELS['ci-body'].html; R.lamps = ELS.annunciator.html;""", doc)
        r = out["R"]
        self.assertEqual(re.findall(r'<tr data-ev="([^"]+)"', r["ci"]), ["ci:example/alpha", "ci:example/beta"])
        alpha = re.search(r'<tr data-ev="ci:example/alpha".*?</tr>', r["ci"]).group(0)
        self.assertEqual(re.findall(r'<td class="num[^"]*">(.*?)</td>', alpha), ["1", "1", "50%", "1"])
        self.assertIn("<td class=\"mono hide-sm\">sd:8 · review</td>", alpha)
        self.assertRegex(r["ci"], r'data-ev="ci:example/alpha"[^>]*><td class="g g-ok">')
        self.assertRegex(r["ci"], r'data-ev="ci:example/beta".*?<td class="num">100%</td>')
        self.assertRegex(r["lamps"], r'data-cell="ci" data-ev="ci" data-view="ci" data-state="ok".*?<b>67%</b> passed.*?3 gate runs<')

    def test_an_empty_range_says_no_run_ended_and_its_lamp_is_unknown(self):
        out = self.run_page("R.ci = ELS['ci-body'].html; R.lamps = ELS.annunciator.html;")
        r = out["R"]
        self.assertIn("No lane entry ended in the last 7 days.", r["ci"])
        self.assertRegex(r["lamps"], r'data-cell="ci" data-ev="ci" data-view="ci" data-state="unknown".*?no gate run')

    def test_a_ci_read_that_failed_is_unknown_with_its_reason_and_no_zeros(self):
        self.lane()
        doc = self.doc()
        doc["ci"] = {"error": "OperationalError: database is locked"}
        out = self.run_page("R.state = ELS['ci-state'].html; R.ci = ELS['ci-body'].html; R.lamps = ELS.annunciator.html;", doc)
        r = out["R"]
        self.assertIn("<b>▨ Not read.</b> OperationalError: database is locked", r["state"])
        self.assertNotIn("data-ev", r["ci"])
        self.assertNotIn("No lane entry", r["ci"])
        self.assertRegex(r["lamps"], r'data-cell="ci" data-ev="ci" data-view="ci" data-state="unknown".*?not read')
        self.assertIn("ci: OperationalError: database is locked", out["states"][-1]["text"])

    def test_unreadable_lane_entries_are_named_not_hidden(self):
        doc = self.doc()
        doc["ci"]["unreadable"] = 2
        out = self.run_page("R.state = ELS['ci-state'].html;", doc)
        self.assertIn("2 lane entries are not readable and are not counted", out["R"]["state"])

    def test_without_a_registry_cap_and_configure_are_off_with_the_reason(self):
        (self.path.parent / "providers.yaml").unlink()
        out = self.run_page("""R.cap = cmd('bill.cap').when(OBJ.get('bill:capped')); R.cf = cmd('providers.configure').when(OBJ.get('sc:kimi'));
C.run(cmd('bill.cap'), OBJ.get('bill:capped')); await flush();""")
        self.assertTrue(out["R"]["cap"].startswith("the provider registry was not read: RegistryError: no provider registry at "))
        self.assertEqual(out["R"]["cf"], out["R"]["cap"])
        self.assertEqual(out["posts"], [])
        self.assertIn("controls: RegistryError", out["states"][-1]["text"])

    # Pack sd:7233: Lane times, Queue's Last 7 days moved here (pack sd:6006).
    TIMES = {"error": "", "lanes": [
        {"repo": "system", "path": "/repos/system", "times": {"days": 7, "entries": 4, "wait": 4500.0, "gate": 610.0,
                                                              "over": True, "slowest": {"item": 42, "title": "A slow one", "total": 9000.0}}},
        {"repo": "quiet", "path": "/repos/quiet", "times": {"days": 7, "entries": 0, "wait": None, "gate": None, "over": False,
                                                            "slowest": None}},
        {"repo": "locked", "path": "/repos/locked", "times": {"error": "the lane times were not read: database is locked"}}]}

    def test_lane_times_shows_each_lanes_medians_and_slowest_entry_and_folds_the_quiet_ones(self):
        """Pack sd:7233: one row per lane with an entry in 7 days; a median wait past an hour is ▲ caution and says the
        question files; a lane with no entry folds into the note; a selected row shows its facts and CLI line."""
        doc = self.doc()
        doc["lanes"] = self.TIMES
        out = self.run_page("R.body = ELS['times-body'].html; R.note = ELS['times-note'].textContent;"
                            " open('lane:/repos/system'); R.det = ELS.details.html;", doc)
        body = out["R"]["body"]
        self.assertEqual(re.findall(r'<tr data-ev="([^"]+)"', body), ["lane:/repos/system", "lane:/repos/locked"])
        system = re.search(r'<tr data-ev="lane:/repos/system".*?</tr>', body).group(0)
        self.assertIn('<td class="g g-caution">▲<span class="sr">caution: median wait past 1 h</span></td>', system)
        self.assertEqual(re.findall(r'<td class="num">(.*?)</td>', system), ["4", "1h 15m", "10m 10s"])
        self.assertIn("sd:42 A slow one · 2h 30m", system)
        self.assertIn('<td class="g g-unknown">▨<span class="sr">not read</span></td>', body)
        self.assertIn("▨ not read: the lane times were not read: database is locked", body)
        self.assertEqual(out["R"]["note"], "▲ Median wait past 1 h: system. The lane run files the parallel-lanes question."
                                           " No entry in 7 days: 1 lane.")
        self.assertIn("sd-ship -C /repos/system lane list", out["R"]["det"])
        self.assertIn("Median wait past 1 h: the lane run files the parallel-lanes question.", out["R"]["det"])

    def test_lane_times_with_no_entry_says_so_and_a_failed_read_is_unknown(self):
        doc = self.doc()
        doc["lanes"] = {"error": "", "lanes": [self.TIMES["lanes"][1]]}
        quiet = self.run_page("R.body = ELS['times-body'].html;", doc)["R"]["body"]
        self.assertIn("No lane ran an entry in the last 7 days.", quiet)
        doc["lanes"] = {"error": "OperationalError: database is locked"}
        out = self.run_page("R.body = ELS['times-body'].html;", doc)
        self.assertIn("<b>▨ Not read.</b> OperationalError: database is locked", out["R"]["body"])
        self.assertIn("lanes: OperationalError: database is locked", out["states"][-1]["text"])

    PRS = {"error": "", "items": 3, "open": 1, "on_you": 9000, "on_reviewers": 4200, "merged": 1, "to_merge": 5400,
           "unknown": [], "repos": [
               {"repo": "example/system", "items": 2, "open": 1, "on_you": 9000, "on_reviewers": 3600, "merged": 1,
                "to_merge": 5400, "prs": [
                    {"item": 2, "title": "Waiting", "url": "https://github.com/example/system/pull/2", "open": True,
                     "on": "you", "on_you": 7200, "on_reviewers": 0, "to_merge": None},
                    {"item": 1, "title": "Landed", "url": "https://github.com/example/system/pull/1", "open": False,
                     "on": None, "on_you": 1800, "on_reviewers": 3600, "to_merge": 5400}]},
               {"repo": "example/other", "items": 1, "open": 0, "on_you": 0, "on_reviewers": 600, "merged": 0,
                "to_merge": None, "prs": [
                    {"item": 3, "title": "Closed", "url": "https://github.com/example/other/pull/3", "open": False,
                     "on": None, "on_you": 0, "on_reviewers": 600, "to_merge": None}]}]}
    OPEN_SYSTEM = """{ const tr = { dataset: { repo: 'example/system' } };
DOC_LISTENERS.click.forEach(f => f({ target: { closest: s => s === '#prs-body tr.repo' ? tr : null } })); }
await flush();"""

    def test_pr_waits_shows_the_totals_then_one_closed_row_per_repo(self):
        """pack sd:7859, sd:8084: the totals row, then each repository's row with its own totals, closed by default."""
        doc = self.doc()
        doc["prs"] = self.PRS
        out = self.run_page("R.body = ELS['prs-body'].html; R.note = ELS['prs-note'].textContent;", doc)
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", out["R"]["body"], re.S)
        self.assertEqual(len(rows), 3, "a closed repository shows no pull request rows")
        self.assertIn("All 3 items", rows[0])
        self.assertEqual(re.findall(r'<td class="num[^"]*">(.*?)</td>', rows[0]), ["2h 30m", "1h 10m", "median 1h 30m"])
        self.assertIn('<td class="g g-caution">▲<span class="sr">open on you</span></td>', rows[1])
        self.assertIn('class="disc" data-open="example/system" aria-expanded="false" aria-label="Open example/system"',
                      rows[1])
        self.assertIn("2 pull requests, 1 open", rows[1])
        self.assertEqual(re.findall(r'<td class="num[^"]*">(.*?)</td>', rows[1]), ["2h 30m", "1h 0m", "median 1h 30m"])
        self.assertIn('data-open="example/other"', rows[2])
        self.assertEqual(re.findall(r'<td class="num[^"]*">(.*?)</td>', rows[2]), ["0m 0s", "10m 0s", "none merged"])
        self.assertEqual(out["R"]["note"], "1 pull request open now; waiting in all: 3h 40m.")

    def test_a_press_on_a_repo_row_opens_its_pull_requests_slowest_first(self):
        doc = self.doc()
        doc["prs"] = self.PRS
        out = self.run_page(self.OPEN_SYSTEM + "\nR.body = ELS['prs-body'].html;", doc)
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", out["R"]["body"], re.S)
        self.assertEqual(len(rows), 5)
        self.assertIn('aria-expanded="true" aria-label="Close example/system"', rows[1])
        self.assertIn('<td class="g g-caution">▲<span class="sr">open, on you</span></td>', rows[2])
        self.assertIn('href="https://github.com/example/system/pull/2"', rows[2])
        self.assertEqual(re.findall(r'<td class="num[^"]*">(.*?)</td>', rows[2]), ["2h 0m", "0m 0s", "not merged"])
        self.assertIn('<span class="sr">ended</span>', rows[3])
        self.assertIn('data-open="example/other"', rows[4])
        again = self.run_page(self.OPEN_SYSTEM + "\n" + self.OPEN_SYSTEM + "\nR.body = ELS['prs-body'].html;", doc)
        self.assertEqual(len(re.findall(r"<tr[^>]*>", again["R"]["body"])), 3, "a second press closes it")

    def test_a_pull_request_not_read_is_named_not_counted(self):
        doc = self.doc()
        doc["prs"] = {**self.PRS, "unknown": ["sd:9: no reading of its author and requested reviewers"]}
        note = self.run_page("R.note = ELS['prs-note'].textContent;", doc)["R"]["note"]
        self.assertIn("▨ Not counted, not known whether yours: sd:9: no reading of its author and requested reviewers.",
                      note)

    def test_pr_waits_with_none_says_so_and_a_failed_read_is_unknown(self):
        doc = self.doc()
        doc["prs"] = {**self.PRS, "items": 0, "open": 0, "repos": []}
        self.assertIn("No pull request of yours has a lane note yet.",
                      self.run_page("R.b = ELS['prs-body'].html;", doc)["R"]["b"])
        doc["prs"] = {"error": "OperationalError: database is locked"}
        out = self.run_page("R.b = ELS['prs-body'].html;", doc)
        self.assertIn("<b>▨ Not read.</b> OperationalError: database is locked", out["R"]["b"])
        self.assertIn("prs: OperationalError: database is locked", out["states"][-1]["text"])

    def test_a_part_that_failed_is_a_partial_read_and_its_panel_is_unknown(self):
        doc = self.doc()
        doc["scorecard"] = {"error": "OperationalError: database is locked"}
        out = self.run_page("R.sc = ELS['sc-body'].html; R.bills = ELS.bills.html;", doc)
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("scorecard: OperationalError: database is locked", out["states"][-1]["text"])
        self.assertIn("<b>▨ Not read.</b> OperationalError: database is locked", out["R"]["sc"])
        self.assertIn('data-ev="bill:capped"', out["R"]["bills"])

    def test_a_stale_month_lights_no_lamp_and_the_clock_shows_the_oldest_reading(self):
        doc = self.doc()
        doc["usage"] = dict(doc["usage"], observed="2026-09-06T11:31:00Z", stale="database is locked")
        doc["numbers"] = dict(doc["numbers"], observed="2026-09-06T11:58:00Z")
        doc["attention"] = metrics_screen._attention(doc["usage"], doc["trend"])
        out = self.run_page("R.lamps = ELS.annunciator.html;", doc)
        lamps = out["R"]["lamps"]
        states = dict(re.findall(r'data-cell="([\w-]+)"[^>]*data-state="(\w+)"', lamps))
        self.assertEqual({k: v for k, v in states.items() if k != "ci"}, {"spend": "unknown", "w-claude": "unknown", "bound": "unknown"})
        self.assertEqual(out["states"][-1]["kind"], "partial")
        self.assertIn("usage: stale since 11:31 UTC: database is locked", out["states"][-1]["text"])

    def test_a_document_that_does_not_arrive_leaves_no_reading_and_says_so(self):
        out = self.run_page("""R.before = ELS.annunciator.html;
DOC = { error: 'Open a dashboard page before reading Metrics.' }; STATUS = 403;
window.PAGE_AGAIN.run(); await flush();
R.lamps = ELS.annunciator.html; R.bills = ELS.bills.html;""")
        self.assertEqual(out["gets"], ["/api/metrics", "/api/metrics"])
        self.assertIn("data-cell=", out["R"]["before"])
        self.assertEqual(out["states"][-1]["kind"], "error")
        self.assertNotIn("data-cell=", out["R"]["lamps"])
        self.assertNotIn("data-ev=", out["R"]["bills"])
        self.assertEqual(out["attention"][-1], {"state": "unknown", "n": 0, "what": "Metrics not read"})

    def six(self):
        self.cost("run", "2026-07-15T10:00:00Z", provider="kimi", bill="capped", role="author", usd=5)
        self.cost("run", "2026-07-16T10:00:00Z", provider="claude", bill="plan", role="reviewer", usd=0)
        self.cost("run", "2026-07-17T10:00:00Z", provider="claude", bill=None, role="author", usd=2)
        return metrics_screen.document(self.connection, now=NOW, span="6mo")

    SIX = """var SIX = %s;
ANSWER = path => path === '/api/metrics?range=6mo' ? [200, SIX] : path === '/api/metrics' ? [STATUS, DOC] : [404, { error: 'no answer' }];
OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
const press = (range, off) => ELS.range.listeners.click[0]({ target: { closest: s => s === 'button[data-range]'
  ? { dataset: { range }, title: off || '', getAttribute: k => k === 'aria-disabled' && off ? 'true' : null } : null } });
"""

    def test_6mo_is_live_reads_the_six_months_and_keeps_the_choice_in_the_address(self):
        """pack sd:7208: 6mo reads ?range=6mo; Spend draws each month by bill, a month with no rows says so, never $0."""
        out = self.run_page(self.SIX % json.dumps(self.six()) + """R.before = ELS.range.html; R.hidden = ELS['months-block'].hidden;
press('6mo'); await flush();
R.after = ELS.range.html; R.shown = ELS['months-block'].hidden; R.head = ELS['months-head'].html; R.months = ELS['months-body'].html;
R.note = ELS['months-note'].textContent; R.sc = ELS['sc-span'].textContent; R.ci = ELS['ci-body'].html; R.lamps = ELS.annunciator.html;
open('mo:2026-04'); R.empty = ELS.details.html; open('mo:2026-07'); R.july = ELS.details.html;
press('30d', 'off'); press('7d'); await flush(); R.back = ELS['months-block'].hidden;""")
        r = out["R"]
        self.assertRegex(r["before"], r'data-range="7d" aria-pressed="true">7d</button><button type="button" data-range="30d" aria-pressed="false" aria-disabled="true"')
        self.assertIn('<button type="button" data-range="6mo" aria-pressed="false">6mo</button>', r["before"])
        self.assertIn('<button type="button" data-range="6mo" aria-pressed="true">6mo</button>', r["after"])
        self.assertEqual(out["gets"], ["/api/metrics", "/api/metrics?range=6mo", "/api/metrics"])
        self.assertEqual(out["urls"], [[["range", "6mo"]], []])
        self.assertEqual((r["hidden"], r["shown"], r["back"]), (True, False, True))
        self.assertEqual(re.findall(r'<th scope="col"[^>]*>([^<]+)</th>', r["head"]), ["Month", "Total", "capped", "plan", "no bill"])
        rows = dict(re.findall(r'<tr data-ev="mo:([\d-]+)".*?>(.*?)</tr>', r["months"]))
        self.assertEqual(list(rows), ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"])
        for month in ("2026-04", "2026-05", "2026-06", "2026-08"):
            self.assertIn('<td class="why" colspan="4">no rows</td>', rows[month], month)
            self.assertNotIn("$", rows[month], month)
        self.assertEqual(re.findall(r'<td class="num">(.*?)</td>', rows["2026-07"]), ["$7.00", "$5.00", "▨ window", "$2.00"])
        self.assertEqual(re.findall(r'<td class="num">(.*?)</td>', rows["2026-09"]), ["$80.00", "$80.00", "▨ window", "—"])
        self.assertIn("<small>to date</small>", rows["2026-09"])
        self.assertEqual(r["note"], "Total $87.00 over 2026-04 to 2026-09; 4 months with no ledger row. ▨ marks a bill that spends a vendor window, not a zero.")
        self.assertEqual(r["sc"], "2026-04 to 2026-09.")
        self.assertIn("No lane entry ended since 2026-04-01.", r["ci"])
        self.assertIn('data-cell="ci" data-ev="ci" data-view="ci" data-state="unknown"', r["lamps"])
        self.assertIn("<h2>2026-04 · no rows</h2>", r["empty"])
        self.assertIn("<code>sd-db usage --month 2026-07</code>", r["july"])
        self.assertIn("<dt>plan</dt><dd>window, not dollars</dd><dt>No bill</dt><dd>$2.00</dd>", r["july"])
        self.assertEqual(out["toasts"], [["off", False]], "a range that is off says why and reads nothing")

    def test_spend_draws_each_capped_bills_burn_its_cap_and_the_projection(self):
        """pack sd:7524: Operations Usage's burn line, ported. 80 by day 6 is 13.33 a day: 400 by day 30, and the
        cap of 100 is passed on day 7.5, so day 8. A plan bill draws its windows and no burn."""
        burns = self.run_page("R.burns = ELS.burns.html;")["R"]["burns"]
        self.assertEqual(re.findall(r'data-burn="([^"]+)"', burns), ["capped"])
        self.assertIn('class="cap" data-cap="100.00"', burns)
        self.assertIn('data-points="2:80.00"', burns)
        self.assertIn('class="proj" data-end="400.00" data-cross="8"', burns)
        self.assertIn("passes its cap on day 8", burns)

    def test_the_projection_meets_the_cap_rule_on_the_day_it_crosses(self):
        """Usage's own case (test_usage_screen, PR #430 review): 40 by day 8 is 5 a day, 100 is passed on day 20, and the
        drawn line stops there on the cap's height; a day earlier the rate is 40/7 and 17.5 is day 18. A past month
        draws no projection."""
        def burns(today, month="2026-09"):
            doc = self.doc()
            doc["usage"].update(today=today, days=30, month=month)
            doc["usage"]["bills"][0].update(burn=[[1, 10.0], [5, 40.0]], cap=100.0, spent=40.0)
            return self.run_page("R.burns = ELS.burns.html;", doc)["R"]["burns"]

        drawn = burns(8)
        self.assertIn('class="proj" data-end="150.00" data-cross="20"', drawn)
        cap = re.search(r'<line class="cap" data-cap="[\d.]+" x1="([\d.]+)" x2="([\d.]+)" y1="([\d.]+)"', drawn)
        line = re.search(r'<line class="proj" data-end="[\d.]+" data-cross="\d+" x1="[\d.]+" y1="[\d.]+" x2="([\d.]+)" y2="([\d.]+)"', drawn)
        self.assertIsNotNone(cap, drawn)
        self.assertIsNotNone(line, drawn)
        left, right = float(cap.group(1)), float(cap.group(2))
        self.assertAlmostEqual(float(line.group(1)), left + (right - left) * 19 / 29, places=1)
        self.assertEqual(line.group(2), cap.group(3))
        self.assertIn('data-end="171.43" data-cross="18"', burns(7))
        self.assertNotIn('class="proj"', burns(None, "2026-08"))

    def test_the_month_picker_reads_that_month_and_keeps_it_in_the_address(self):
        """pack sd:7524: Operations Usage's ?month= picker, on Spend. A past month's lamp is that month, not to date."""
        august = metrics_screen.document(self.connection, now=NOW, month="2026-08")
        out = self.run_page("""var AUG = %s;
ANSWER = path => path === '/api/metrics?month=2026-08' ? [200, AUG] : path === '/api/metrics' ? [STATUS, DOC] : [404, { error: 'no answer' }];
OUT.urls = []; window.shell.url = q => OUT.urls.push(q);
R.before = ELS.month.value; R.head = ELS['bills-h'].textContent;
ELS.month.value = '2026-08'; ELS.month.listeners.change[0]({ target: ELS.month }); await flush();
R.after = ELS['bills-h'].textContent; R.lamps = ELS.annunciator.html;
ELS.month.value = ''; ELS.month.listeners.change[0]({ target: ELS.month }); await flush();""" % json.dumps(august))
        r = out["R"]
        self.assertEqual((r["before"], r["head"], r["after"]), ("2026-09", "Dollars in 2026-09, by bill", "Dollars in 2026-08, by bill"))
        self.assertEqual(out["gets"], ["/api/metrics", "/api/metrics?month=2026-08", "/api/metrics"])
        self.assertEqual(out["urls"], [[["month", "2026-08"]], []])
        self.assertRegex(r["lamps"], r'data-cell="spend".*?<small>2026-08 · 2 bills</small>')
        self.assertNotIn("/operations", METRICS_JS, "Metrics opens no Operations area")

    def test_a_past_month_draws_the_live_windows_and_an_unread_meter_as_unknown(self):
        """Lead review of 960414d82: Budget used, now and the week lamps stay on the live meter for a past month."""
        august = metrics_screen.document(self.connection, now=NOW, month="2026-08")
        r = self.run_page("R.windows = ELS.windows.html; R.lamps = ELS.annunciator.html;", august)["R"]
        self.assertIn("80%", r["windows"])
        self.assertRegex(r["lamps"], r'data-cell="w-claude".*?<b>80%</b> used')
        august["usage"] = {**august["usage"], "meter": [], "meter_error": "OperationalError: database is locked"}
        r = self.run_page("R.windows = ELS.windows.html;", august)["R"]
        self.assertIn("database is locked", r["windows"])
        self.assertNotIn("No meter reading yet", r["windows"])

    def test_a_week_number_with_no_rows_is_unknown_not_a_measured_zero(self):
        """Operations Usage's rule (test_controls_actions, retired in pack sd:7524): no cost row is not $0.00."""
        doc = self.doc()
        for row in doc["numbers"]["rows"]:
            row["value"] = None
        week = self.run_page("R.week = ELS['week-body'].html;", doc)["R"]["week"]
        self.assertEqual(len(re.findall(r'<td class="g g-unknown">', week)), len(doc["numbers"]["rows"]))
        self.assertNotIn("$0.00", week)

    def test_the_address_opens_the_page_at_its_month_and_range(self):
        out = self.run_page("", before="location.search = '?range=6mo&month=2026-08';\n")
        self.assertEqual(out["gets"], ["/api/metrics?range=6mo&month=2026-08"])

    def test_the_address_opens_the_page_at_6mo(self):
        out = self.run_page("R.range = ELS.range.html;", before="location.search = '?range=6mo';\n")
        self.assertEqual(out["gets"], ["/api/metrics?range=6mo"])
        self.assertIn('data-range="6mo" aria-pressed="true"', out["R"]["range"])

    def test_a_selected_reading_shows_its_facts_and_cli_line(self):
        out = self.run_page("open('sc:kimi'); R.det = ELS.details.html;")
        self.assertIn("<h2>kimi</h2>", out["R"]["det"])
        self.assertIn("sd providers configure --file CONFIG.json", out["R"]["det"])

    def test_the_page_keeps_the_shells_keys_and_reads_through_the_shared_reader(self):
        self.assertIn("S.read(spec)", METRICS_JS)
        self.assertNotIn("innerHTML", METRICS_JS)
        self.assertNotRegex(METRICS_JS, r"\.style\.")
        self.assertNotRegex(METRICS_JS, r"e\.key !?== '[jk]'")
        self.assertIn("window.PAGE_LIST", METRICS_JS)


WRITE = """
var POSTED = (path, body) => [200, { revision: 'after-' + OUT.posts.length }];
ANSWER = (path, body) => path === '/api/metrics' ? [STATUS, DOC] : POSTED(path, body);
const said = () => (ELS.details.html.match(/data-said="([a-z]+)"[^>]*>(?:<span class="g-unknown" aria-hidden="true">▨<\\/span> )?(.*?)<\\/p>/) || []).slice(1);
"""


class TheControlsScript(Controlled):
    """Cap and Configure in metrics.js: what each posts, and what the page says for each answer."""

    run_page = TheScript.run_page

    def write(self, body, doc=None):
        return self.run_page(WRITE + body, doc)

    def test_cap_posts_the_typed_number_with_the_read_revision_and_undo_posts_the_old_cap(self):
        revision = self.doc()["controls"]["revision"]
        out = self.write("""open('bill:capped'); FIELD_VALUES = { cap: '50' }; C.run(cmd('bill.cap'), OBJ.get('bill:capped')); await flush();
R.said = said(); lastUndo().undo(); await flush();""")
        self.assertEqual(out["posts"][0][:2], ["/api/bills/capped/cap", {"revision": revision, "cap_usd_month": 50}])
        self.assertEqual(out["posts"][1][:2], ["/api/bills/capped/cap", {"revision": "after-1", "cap_usd_month": 100.0}])
        self.assertEqual(out["confirms"], ["bill.cap"])
        self.assertEqual(out["toasts"][:1], [["Cap set to $50.00 · capped bill", True]])
        self.assertEqual(out["toasts"][-1], ["Cap set to $50.00 undone · capped bill", False])
        self.assertEqual(out["R"]["said"], ["landed", "Cap set to $50.00."])

    def test_none_clears_the_cap_and_a_start_bill_is_off_with_the_reason(self):
        self.doc()  # the first read seeds the `sub` row the cost below names
        self.cost("run", "2026-09-04T10:00:00Z", provider="claude", bill="sub", role="author", usd=1)
        out = self.write("""FIELD_VALUES = { cap: 'none' }; C.run(cmd('bill.cap'), OBJ.get('bill:capped')); await flush();
R.sub = cmd('bill.cap').when(OBJ.get('bill:sub'));""")
        self.assertEqual(out["posts"][0][1]["cap_usd_month"], None)
        self.assertEqual(out["toasts"][0], ["Cap cleared · capped bill", True])
        self.assertEqual(out["R"]["sub"], "a 'start' entry is billed to it, so no cap can be set")

    def test_a_legacy_clear_asks_first_posts_null_and_offers_no_undo(self):
        self.doc()
        self.cost("run", "2026-09-04T10:00:00Z", provider="claude", bill="sub", role="author", usd=1)
        with self.connection:
            self.connection.execute("UPDATE bill SET cap_usd_month = 5.0 WHERE name = 'sub'")
        out = self.write("""R.fields = cmd('bill.cap').fields(OBJ.get('bill:sub')); R.ask = cmd('bill.cap').askFirst;
C.run(cmd('bill.cap'), OBJ.get('bill:sub')); await flush();""")
        self.assertEqual((out["R"]["fields"], out["R"]["ask"]), ([], True))
        self.assertEqual(out["posts"][0][:2], ["/api/bills/sub/cap", {"revision": self.doc()["controls"]["revision"], "cap_usd_month": None}])
        self.assertEqual(out["toasts"], [["Cap cleared · sub bill", False]])

    def test_a_400_or_a_409_says_nothing_was_written_and_reads_again(self):
        for status, error in ((400, "a cap must be a finite number"), (409, "provider configuration changed; reload before saving")):
            with self.subTest(status=status):
                out = self.write(f"""POSTED = () => [{status}, {{ error: '{error}' }}];
open('bill:capped'); FIELD_VALUES = {{ cap: '50' }}; C.run(cmd('bill.cap'), OBJ.get('bill:capped')); await flush(); R.said = said();""")
                text = f"capped bill not changed: {error}. Nothing was written."
                self.assertEqual(out["toasts"], [[text, False]])
                self.assertEqual(out["R"]["said"], ["refused", text])
                self.assertEqual(out["gets"], ["/api/metrics", "/api/metrics"])

    def test_any_other_failure_or_no_answer_leaves_the_outcome_unknown(self):
        for answer, error in (("[503, { error: 'database is locked' }]", "database is locked"), ("(() => { throw new Error('connection reset'); })()", "connection reset"),
                              ("[403, { error: 'Open the dashboard again before saving.' }]", "Open the dashboard again before saving.")):
            with self.subTest(error=error):
                out = self.write(f"""POSTED = () => {answer};
open('bill:capped'); FIELD_VALUES = {{ cap: '50' }}; C.run(cmd('bill.cap'), OBJ.get('bill:capped')); await flush(); R.said = said(); R.det = ELS.details.html;""")
                text = f"Not known whether capped bill changed: {error.rstrip('.')}. Metrics was read again; check it before you act again."
                self.assertEqual(out["toasts"], [[text, False]])
                self.assertEqual(out["R"]["said"], ["unknown", text])
                self.assertIn('<span class="g-unknown" aria-hidden="true">▨</span>', out["R"]["det"])
                self.assertNotIn("not changed", json.dumps(out["toasts"]))

    def test_an_undo_that_fails_unknown_is_not_called_not_undone(self):
        out = self.write("""FIELD_VALUES = { cap: '50' }; C.run(cmd('bill.cap'), OBJ.get('bill:capped')); await flush();
POSTED = () => [502, { error: 'bad gateway' }]; lastUndo().undo(); await flush();""")
        self.assertEqual(out["toasts"][-1], ["Not known whether the undo landed on capped bill: bad gateway. Metrics was read again; check it before you act again.", False])

    def test_configure_posts_the_whole_configuration_with_blank_fields_kept(self):
        revision = self.doc()["controls"]["revision"]
        out = self.write("""FIELD_VALUES = { enabled: 'kimi, claude', reviewer: 'claude, kimi' }; C.run(cmd('providers.configure'), OBJ.get('sc:kimi')); await flush();
lastUndo().undo(); await flush();""")
        self.assertEqual(out["posts"][0][:2], ["/api/providers/configure", {"revision": revision, "enabled": {"claude": True, "kimi": True},
                                                                         "orders": {"author": ["claude", "kimi"], "reviewer": ["claude", "kimi"]}}])
        self.assertEqual(out["posts"][1][:2], ["/api/providers/configure", {"revision": "after-1", "enabled": {"claude": True, "kimi": True},
                                                                         "orders": {"author": ["claude", "kimi"], "reviewer": ["kimi", "claude"]}}])
        self.assertEqual(out["toasts"][0], ["Providers configured · kimi", True])

    def test_configure_disables_the_providers_left_out_and_refuses_an_unknown_name_before_posting(self):
        out = self.write("""FIELD_VALUES = { enabled: 'kimi' }; C.run(cmd('providers.configure'), OBJ.get('sc:kimi')); await flush();
FIELD_VALUES = { enabled: 'kimi, opus' }; C.run(cmd('providers.configure'), OBJ.get('sc:kimi')); await flush();""")
        self.assertEqual(out["posts"][0][1]["enabled"], {"claude": False, "kimi": True})
        self.assertEqual(len(out["posts"]), 1)
        self.assertEqual(out["toasts"][-1], ["kimi not changed: no provider opus; the registry names claude, kimi.", False])

    def test_a_providers_details_name_its_place_in_each_order(self):
        out = self.write("open('sc:kimi'); R.det = ELS.details.html;")
        self.assertIn("<dt>Author order</dt><dd>2 of 2</dd>", out["R"]["det"])
        self.assertIn("<dt>Reviewer order</dt><dd>1 of 2</dd>", out["R"]["det"])


class TheLaneTimes(RealLane):
    """Pack sd:7233: Metrics' `lanes`, each managed lane's last 7 days, read by the pinned pack's `sd_lane.lane_times`
    from the entries' history rows (pack sd:6006); Queue no longer reads them."""

    def lanes(self):
        return metrics_screen.document(self.connection, now=NOW)["lanes"]

    def test_each_lane_carries_its_wait_and_gate_times_from_the_history_rows(self):
        self.store([{**self.seeds()[0], "id": "seed-timed", "item": 7, "status": "running", "step": "prepare",
                     "position": 7, "enqueued_at": "2026-09-06T10:00:00Z", "started_at": "2026-09-06T11:30:00Z"}])
        lanes = self.lanes()
        self.assertEqual((lanes["error"], [(lane["repo"], lane["path"]) for lane in lanes["lanes"]]),
                         ("", [("system", str(self.checkout))]))
        times = lanes["lanes"][0]["times"]
        self.assertEqual((times["entries"], times["wait"], times["gate"], times["over"]), (1, 5400, None, True))

    def test_a_failed_times_read_is_named_on_its_lane(self):
        lane_module = metrics_screen.pack_module("sd_lane")
        with mock.patch.object(lane_module, "lane_times", side_effect=sqlite3.OperationalError("database is locked")):
            times = self.lanes()["lanes"][0]["times"]
        self.assertEqual(times, {"error": "the lane times were not read: OperationalError: database is locked"})

    def test_a_lane_whose_remote_names_no_github_repository_says_its_times_were_not_read(self):
        upsert_repo(self.connection, "/elsewhere/other", remote="git@example.invalid:x.git", managed=1)
        self.connection.commit()
        lanes = {lane["repo"]: lane["times"] for lane in self.lanes()["lanes"]}
        self.assertEqual(lanes["other"], {"error": "its remote names no GitHub repository, so it has no lane history"})

    def test_queue_reads_no_lane_times(self):
        from sd_dashboard import queue_screen

        self.assertFalse(hasattr(queue_screen, "times"))
        self.assertNotIn("times", queue_screen.document(self.connection, now=NOW)["lanes"][0])


class TheRegistration(Registers, unittest.TestCase):
    page, section, route, api = "metrics", "Metrics", "/metrics", ("/api/metrics",)


if __name__ == "__main__":
    unittest.main()
