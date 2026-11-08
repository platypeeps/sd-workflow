"""Real temporary checkpoint stores; API and notification calls are canned."""

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, initialise, protection, upsert_repo
from sd_db import contribution_sync as adapter
from sd_db import contributions as core
from sd_db.contribution_github import Client
from sd_db.database import transaction
from sd_db.shadow_sync import _SearchBudget, read_watermark, sync

from . import test_contribution_github as fixtures
from .test_contribution_github import (
    AT,
    BASE,
    ISSUE_URL,
    ROOT,
    URL,
    Api,
    IssueApi,
    Response,
)


class SyncCase(unittest.TestCase):
    """One temporary store, a canned transport and a recording notifier per test."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.api = Api()
        self.now = datetime(2026, 9, 10, 1, tzinfo=timezone.utc)
        self.sent = []

    def notify(self, claim):
        self.assertFalse(self.db.in_transaction)
        other = connect(self.path)
        try:
            with transaction(other):
                self.assertEqual(core.snapshot(other, claim["key"])["notifications"][claim["event_id"]]["status"], "sending")
        finally:
            other.close()
        self.sent.append(claim)
        return "sent"

    def search(self, urls=(), *, contexts=()):
        def runner(argv):
            if argv == ["gh", "auth", "status"]:
                return 0, "", ""
            author_bucket = any(value.startswith("q=author:@me ") for value in argv)
            context_bucket = any(value.startswith(f"q={context} ") for value in argv for context in contexts)
            rows = [{"__typename": "PullRequest", "url": url, "number": int(url.rsplit("/", 1)[1]),
                     "title": "Contribution", "state": "OPEN", "updatedAt": self.now.isoformat(),
                     "author": {"login": "operator"}, "repository": {"nameWithOwner": "example/project"}} for url in urls if author_bucket or context_bucket]
            return 0, json.dumps({"data": {"search": {"issueCount": len(rows), "nodes": rows,
                                                       "pageInfo": {"hasNextPage": False, "endCursor": None}}}}), ""
        return runner

    def run_sync(self, urls=(), *, contexts=(), **options):
        runner = self.search(urls, contexts=contexts) if contexts else self.search(urls)
        result = sync(self.db, now=self.now, runner=runner, contribution_client=self.api.client(),
                      notifier=self.notify, **options)
        self.now += timedelta(minutes=1)
        return result

    def add_comment(self):
        self.api.rows[ROOT + "/issues/7/comments?per_page=100"] = [{"id": 19, "user": {"id": 2, "login": "maintainer"},
            "body": "Please address this", "author_association": "MEMBER", "updated_at": AT}]

    def observe_heartbeat(self, key):
        row = self.db.execute("SELECT body FROM state WHERE kind='heartbeat' AND key=? ORDER BY id DESC LIMIT 1",
                              ("contribution-observe:" + key,)).fetchone()
        return json.loads(row["body"])

    def tracker_heartbeat(self):
        row = self.db.execute("SELECT body FROM state WHERE kind='heartbeat' AND key='tracker-sync:github' "
                              "ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row["body"])


class ContributionSync(SyncCase):
    def test_search_collects_and_notifies_once_outside_lock(self):
        self.add_comment()
        first = self.run_sync([URL])
        self.assertTrue(first.ok, first.reason)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.run_sync().ok, True)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)

    def test_projected_context_survives_quiet_and_matching_search_refreshes(self):
        contexts = ("assignee:@me", "review-requested:@me")
        self.assertTrue(self.run_sync([URL], contexts=contexts).ok)
        key = "github:" + URL
        first = core.snapshot(self.db, key)
        self.assertEqual(set(first["observation"]["why"]), {"author", "assigned", "review-requested"})
        self.assertEqual({event["id"] for event in first["attention"]}, {"context:assigned", "context:review-requested"})
        for urls in ([], [URL]):
            with self.subTest(urls=urls):
                self.assertTrue(self.run_sync(urls, contexts=contexts).ok)
                current = core.snapshot(self.db, key)
                self.assertEqual(current["observation"]["why"], first["observation"]["why"])
                self.assertEqual(current["attention"], first["attention"])
        self.assertEqual(len(self.sent), 2)

    def test_fresh_search_updates_context_without_replacing_registered_policy(self):
        core.capture(self.db, title="Registered", changes={"pull_url": URL, "blocking_labels": ["needs-work"]}, who="operator")
        self.assertTrue(self.run_sync([URL]).ok)
        key = "github:" + URL
        event_ids = []
        for contexts, expected in ((("assignee:@me",), {"author", "assigned"}),
                                   ((), {"author"}), (("assignee:@me",), {"author", "assigned"})):
            with self.subTest(contexts=contexts):
                self.assertTrue(self.run_sync([URL], contexts=contexts).ok)
                current = core.snapshot(self.db, key)
                self.assertEqual(set(current["observation"]["why"]), expected)
                self.assertEqual(bool(current["attention"]), "assigned" in expected)
                if "assigned" in expected:
                    event_ids.append(current["attention"][0]["id"])
                queued = adapter._queue(self.db)["pending"][0]
                self.assertEqual(queued["blocking_labels"], ["needs-work"])
                self.assertTrue(queued["explicit"])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(event_ids), 2)
        self.assertNotEqual(event_ids[0], event_ids[1])

    def test_projected_observation_restores_context_without_a_queue_entry(self):
        self.api.detail["user"] = {"id": 2, "login": "maintainer"}
        observation = self.api.observation()
        observation["why"] = ["review-requested"]
        key = "github:" + URL
        before = core.observe_pull(self.db, URL, observation, expected_revision=core.snapshot(self.db, key)["revision"])
        self.assertEqual(adapter._queue(self.db)["pending"], [])
        self.assertEqual(adapter.plan(self.db)["pending"][0]["why"], ["review-requested"])
        self.assertTrue(self.run_sync().ok)
        self.assertEqual(core.snapshot(self.db, key)["attention"], before["attention"])

    def test_incomplete_detail_preserves_new_search_context_for_retry(self):
        self.assertTrue(self.run_sync([URL]).ok)
        self.api.rows[ROOT + "/pulls/7/reviews?per_page=100"] = Response(503, {}, "")
        result = self.run_sync([URL], contexts=("assignee:@me",))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.incomplete, [f"github:{URL}: API HTTP 503; retry on a later collection"])
        self.assertEqual(core.snapshot(self.db, "github:" + URL)["observation"]["why"], ["author"])
        self.assertEqual(set(adapter.plan(self.db)["pending"][0]["why"]), {"author", "assigned"})
        self.api.rows[ROOT + "/pulls/7/reviews?per_page=100"] = []
        self.assertTrue(self.run_sync().ok)
        current = core.snapshot(self.db, "github:" + URL)
        self.assertEqual(current["attention"][0]["id"], "context:assigned")
        self.assertEqual(len(self.sent), 1)

    def test_partial_search_preserves_prior_context_until_complete_search(self):
        self.assertTrue(self.run_sync([URL], contexts=("assignee:@me",)).ok)
        key = "github:" + URL
        before = core.snapshot(self.db, key)
        runner = self.search([URL])

        def partial(argv):
            if any(value.startswith("q=assignee:@me ") for value in argv):
                return 1, "", "fixture assignment search unavailable"
            return runner(argv)

        result = sync(self.db, now=self.now, runner=partial, contribution_client=self.api.client(), notifier=self.notify)
        self.now += timedelta(minutes=1)
        self.assertFalse(result.ok)
        held = core.snapshot(self.db, key)
        self.assertEqual(held["observation"]["why"], before["observation"]["why"])
        self.assertEqual(held["attention"], before["attention"])
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.run_sync([URL]).ok)
        self.assertEqual(core.snapshot(self.db, key)["attention"], [])
        self.assertEqual(len(self.sent), 1)

    def test_quiet_pr_green_own_push_pending_then_failed_current_head(self):
        self.assertTrue(self.run_sync([URL]).ok)
        self.api.detail["head"] = {"sha": BASE}
        self.api.rows[ROOT + f"/commits/{BASE}/check-runs?filter=latest&per_page=100"] = {"total_count": 1, "check_runs": [
            {"id": 92, "head_sha": BASE, "status": "in_progress", "conclusion": None}]}
        self.api.rows[ROOT + f"/commits/{BASE}/status?per_page=100"] = {"total_count": 0, "sha": BASE, "statuses": []}
        self.assertTrue(self.run_sync().ok)
        self.assertEqual(self.sent, [])
        check = self.api.rows[ROOT + f"/commits/{BASE}/check-runs?filter=latest&per_page=100"]["check_runs"][0]
        check.update(status="completed", conclusion="failure")
        self.assertTrue(self.run_sync().ok)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.run_sync().ok)
        self.assertEqual(len(self.sent), 1)

    def test_incomplete_detail_preserves_checkpoint_and_does_not_advance_attention(self):
        self.run_sync([URL])
        key = "github:" + URL
        before = core.snapshot(self.db, key)
        self.api.rows[ROOT + "/pulls/7/reviews?per_page=100"] = Response(503, {}, "")
        result = self.run_sync()
        # One contribution's failed read is held on that contribution, not on
        # the collect: the cursor moves, the tracker stays healthy, and the
        # hold heartbeat says what failed rather than just that something did.
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.watermark_moved)
        self.assertEqual(result.incomplete, [f"{key}: API HTTP 503; retry on a later collection"])
        self.assertEqual(core.snapshot(self.db, key), before)
        self.assertEqual(self.observe_heartbeat(key),
                         {"ok": False, "reason": "Incomplete GitHub observation: API HTTP 503; retry on a later collection"})
        self.assertEqual(core.projection(self.db)[0]["freshness"]["reason"],
                         "Incomplete GitHub observation: API HTTP 503; retry on a later collection")
        self.assertTrue(self.tracker_heartbeat()["ok"])
        self.assertEqual(self.tracker_heartbeat()["incomplete"], result.incomplete)
        self.assertIn("shadow sync: contribution observation incomplete: " + result.incomplete[0], result.report())
        self.assertEqual(self.sent, [])

    def test_release_dependency_without_a_tag_is_held_on_the_item_not_the_collect(self):
        # Item 245 on 2026-09-12: a release dependency configured without a
        # `tag`, whose pull then merged. The proof cannot be read, so the
        # dependency stays unknown -- by contract -- and until this change
        # that one item failed every strict run and read the tracker
        # `degraded` while the cursor had in fact moved.
        self.api.detail.update(merged=True, merge_commit_sha=fixtures.MERGED)
        dependency = {"kind": "release", "repo": "example/project", "contains_pull": URL,
                      "package": {"name": "aiounifi", "version": "96"}}
        clone = Path(self.temp.name) / "clone"
        clone.mkdir()
        for argv in (["init", "-q", "-b", "feat/unifi-wan3-latency"], ["config", "user.name", "Fixture"],
                     ["config", "user.email", "fixture@example.invalid"], ["commit", "--allow-empty", "-qm", "fixture"]):
            subprocess.run(["git", *argv], cwd=clone, check=True)
        item = core.capture(self.db, title="Unfiled", changes={
            "local_clone": str(clone), "local_branch": "feat/unifi-wan3-latency", "depends_on": [dependency]}, who="operator")["item"]["id"]
        key = f"item:{item}"
        result = self.run_sync()
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.watermark_moved)
        self.assertEqual(result.incomplete, [f"{key}: {URL}: exact release repository/tag is required"])
        self.assertEqual(self.observe_heartbeat(key)["reason"],
                         f"Incomplete dependency observation: {URL}: exact release repository/tag is required")
        self.assertEqual(core.projection(self.db)[0]["freshness"]["reason"], self.observe_heartbeat(key)["reason"])
        self.assertTrue(self.tracker_heartbeat()["ok"])
        self.assertEqual(self.tracker_heartbeat()["reason"], "")
        # Still queued, so configuring the tag is picked up by the next run.
        self.assertEqual([row["key"] for row in adapter._queue(self.db)["pending"]], [key])
        self.assertEqual(self.sent, [])

    def test_revision_read_before_network_refuses_concurrent_observer(self):
        self.run_sync([URL])
        before = core.snapshot(self.db, "github:" + URL)
        transport = self.api.__call__
        changed = []
        def concurrent(path, fields, timeout):
            if not changed:
                changed.append(True)
                core.observe_pull(self.db, URL, {**before["observation"], "observed_at": (self.now + timedelta(minutes=1)).isoformat()},
                                  expected_revision=before["revision"])
            return transport(path, fields, timeout)
        client = self.api.client()
        client.transport = concurrent
        result = sync(self.db, now=self.now, runner=self.search(), contribution_client=client, notifier=self.notify)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(result.incomplete), 1)
        self.assertIn("refused", result.incomplete[0])
        self.assertEqual(self.sent, [])

    def test_a_refused_observation_is_on_the_contributions_own_heartbeat(self):
        # sd:1219 (98e77da3d566, 3dff0970d20f): a revision another observer moved
        # was reported as incomplete, but the contribution's heartbeat still said
        # its last read was good.
        self.run_sync([URL])
        key = "github:" + URL
        before = core.snapshot(self.db, key)
        transport = self.api.__call__
        def concurrent(path, fields, timeout):
            if path == ROOT + "/pulls/7/reviews?per_page=100":
                core.observe_pull(self.db, URL, {**before["observation"], "observed_at": (self.now + timedelta(minutes=1)).isoformat()},
                                  expected_revision=before["revision"])
            return transport(path, fields, timeout)
        client = self.api.client()
        client.transport = concurrent
        result = sync(self.db, now=self.now, runner=self.search(), contribution_client=client, notifier=self.notify)
        self.assertEqual(len(result.incomplete), 1)
        self.assertTrue(result.incomplete[0].startswith(key + ": refused: "), result.incomplete)
        self.assertEqual(self.observe_heartbeat(key), {"ok": False, "reason": result.incomplete[0][len(key) + 2:]})
        self.assertEqual(core.projection(self.db)[0]["freshness"]["status"], "unknown")

    def test_a_read_the_core_holds_is_reported_with_the_cores_reason(self):
        # sd:1219 (f03e4681d0f6, 1e4c0796d882): a complete read the core held --
        # here an author with no id -- was reported as the bare key, because the
        # reason came only from the collector.
        self.run_sync([URL])
        key = "github:" + URL
        observation = core.snapshot(self.db, key)["observation"]
        held = {**observation, "observed_at": (self.now + timedelta(minutes=1)).isoformat(), "author": {"id": None}}
        with patch.object(adapter.github, "pull", return_value=held):
            result = self.run_sync()
        self.assertEqual(result.incomplete, [f"{key}: GitHub actor identity is unknown"])
        self.assertEqual(self.tracker_heartbeat()["incomplete"], result.incomplete)

    def test_the_identity_read_is_not_spent_on_a_free_item_entry_budget(self):
        # sd:1219 (c1bb03157cda, 2e2e55d8e012): with a free `item` entry at the
        # head, the identity read was priced against that entry's zero cost and
        # spent a request held for the collectors that follow.
        self.run_sync([URL])
        planned = adapter.plan(self.db)
        item = {"key": "item:999", "item": 999, "depends_on": [], "explicit": True}
        planned["pending"].insert(0, item)
        planned["snapshots"][item["key"]] = core.snapshot(self.db, item["key"])
        client = self.api.client(requests=3)
        result = adapter.refresh(self.db, planned, [], client=client, observed_at=self.now.isoformat(), reserve=3)
        self.assertEqual(client.budget.remaining, 3)
        self.assertNotIn("/user", [call[0] for call in self.api.calls[-3:]])
        self.assertEqual(result["queued"], 1)

    def test_a_pull_that_pages_past_its_price_stops_at_the_reserve(self):
        # sd:1219 (76ef6f5d1cfb, 2e2e55d8e012): `_cost` prices one page per
        # list, so a pull with more pages read on into the requests reserved for
        # the collectors that follow. The client now stops at the reserve, and
        # the pull is held on its own key.
        self.run_sync([URL])
        comments = ROOT + "/issues/7/comments?per_page=100"
        def comment(number):
            return {"id": number, "user": {"id": 2, "login": "maintainer"}, "body": "More", "author_association": "MEMBER", "updated_at": AT}
        def page(number):
            return f'<https://api.github.com{ROOT}/issues/7/comments?per_page=100&page={number}>; rel="next"'
        self.api.rows[comments] = Response(200, {"Link": page(2)}, json.dumps([comment(19)]))
        self.api.rows[comments + "&page=2"] = Response(200, {"Link": page(3)}, json.dumps([comment(20)]))
        self.api.rows[comments + "&page=3"] = [comment(21)]
        planned = adapter.plan(self.db)
        # The identity 1 and the pull's price 9 fit beside a reserve of 3; the
        # pull's two extra pages do not.
        client = self.api.client(requests=1 + 9 + 3)
        result = adapter.refresh(self.db, planned, [], client=client, observed_at=self.now.isoformat(), reserve=3)
        self.assertEqual(client.budget.remaining, 3)
        self.assertEqual(result["incomplete"], ["github:" + URL + ": contribution collection budget exhausted"])
        self.assertEqual(client.floor, 0)

    def clone_pull(self, numbers):
        urls = [URL.rsplit("/", 1)[0] + f"/{number}" for number in numbers]
        original = list(self.api.rows.items())
        for number in numbers[1:]:
            for path, value in original:
                if "/7" in path:
                    self.api.rows[path.replace("/7", f"/{number}")] = copy.deepcopy(value)
        return urls

    def test_details_run_until_the_request_budget_and_the_rest_stage_durably(self):
        urls = self.clone_pull(range(7, 15))
        # 40 requests: the search spends 4, the identity 1, and a pull 8 of
        # the 9 it is priced at, so four details fit and the fifth is stopped
        # before it starts.
        first = self.run_sync(urls, max_requests=40)
        self.assertTrue(first.ok, first.reason)
        self.assertEqual(first.queued, 4)
        self.assertEqual(first.incomplete, [])
        self.assertIn("shadow sync: contribution detail backlog: 4 queued", first.report())
        self.assertTrue(first.watermark_moved)
        self.assertEqual(len(core.projection(self.db)), 4)
        saved = adapter._queue(self.db)
        # All eight are open and never read, so they go first, the newest
        # queued first (pack sd:7844, sd:8116): 14 to 11 are read and 7 heads
        # the queue.
        self.assertEqual(saved["pending"][0]["url"], urls[0])
        self.assertLessEqual(first.requests, 40)
        second = self.run_sync(max_requests=40)
        self.assertEqual(second.queued, 4)
        self.assertEqual(len(core.projection(self.db)), 8)
        self.assertEqual(len(adapter._queue(self.db)["pending"]), 8)
        self.assertIsNotNone(read_watermark(self.db))
        # The default budget clears the same backlog in one run.
        self.assertEqual(self.run_sync().queued, 0)

    def test_details_stop_short_of_the_reserve_for_the_collectors_that_follow(self):
        urls = self.clone_pull(range(7, 10))
        self.assertTrue(self.run_sync(urls, max_requests=100).ok)
        planned = adapter.plan(self.db)
        # The identity spends 1 and each pull 8 of its 9; the third pull would
        # reach into the 3 held back, so it waits with 4 unspent.
        client = self.api.client(requests=21)
        result = adapter.refresh(self.db, planned, [], client=client, observed_at=self.now.isoformat(), reserve=3)
        self.assertEqual((result["attempted"], result["queued"], result["errors"]), (2, 1, []))
        self.assertEqual(client.budget.remaining, 4)

    def test_the_detail_backlog_leaves_the_protection_collector_its_requests(self):
        upsert_repo(self.db, str(Path(self.temp.name) / "fleet"), remote="git@github.com:example/fleet.git")
        upsert_repo(self.db, str(Path(self.temp.name) / "local"), remote="/srv/git/local.git")
        self.assertEqual(protection.reserve(self.db), 4)
        self.api.rows["repos/example/fleet"] = {"default_branch": "main", "permissions": {"admin": True}}
        self.api.rows["repos/example/fleet/branches/main/protection"] = Response(404, {}, "")
        self.api.rows[protection.rules_path("example", "fleet", "main")] = []
        urls = self.clone_pull(range(7, 10))
        # 30 requests: the search spends 4 and the identity 1, two pulls 16,
        # and 9 are left -- enough for a third pull on its own, not with the
        # four protection requests reserved behind it. The pull waits;
        # protection is read.
        result = self.run_sync(urls, max_requests=30)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual((result.queued, result.incomplete), (1, []))
        self.assertEqual(len(core.projection(self.db)), 2)
        row = self.db.execute("SELECT status, reason FROM repo_protection").fetchone()
        self.assertEqual((row["status"], row["reason"]), ("unprotected", None))
        mine = self.db.execute("SELECT body FROM state WHERE kind='heartbeat' AND key=? ORDER BY id DESC LIMIT 1",
                               (protection.HEARTBEAT_KEY,)).fetchone()
        self.assertEqual(json.loads(mine["body"])["requests"], 3)
        self.assertEqual(result.requests, 4 + 1 + 16 + 3)

    def test_a_detail_backlog_that_spends_the_clock_still_leaves_the_fleet_observed(self):
        """sd:1663: the nightly's detail backlog ran the budget to its
        deadline with requests to spare, and the protection sweep, which
        reads the clock before each GET, filed every repository `budget
        exhausted` on zero requests. A stubbed GitHub that answers in the
        measured ~0.5 s a request, 40 queued pulls (320 requests, 160 s) and
        a 19-repository fleet on a 120 s run: every repository is observed."""
        fleet = []
        for index in range(19):
            name = f"repo{index:02d}"
            path = str(Path(self.temp.name) / name)
            upsert_repo(self.db, path, remote=f"git@github.com:example/{name}.git")
            fleet.append(path)
            self.api.rows[f"repos/example/{name}"] = {"default_branch": "main", "permissions": {"admin": True}}
            self.api.rows[f"repos/example/{name}/branches/main/protection"] = {
                "enforce_admins": {"enabled": True},
                "required_status_checks": {"strict": True, "contexts": ["CI Result"]},
                "required_pull_request_reviews": {"required_approving_review_count": 1}}
            self.api.rows[protection.rules_path("example", name, "main")] = []
        urls = self.clone_pull(range(7, 47))
        clock = [1000.0]

        def ticking(path, fields, timeout):
            clock[0] += 0.5
            return self.api(path, fields, timeout)

        client = Client(_SearchBudget(1000, clock[0] + 120), transport=ticking)
        with patch("time.monotonic", lambda: clock[0]):
            result = sync(self.db, now=self.now, runner=self.search(urls), contribution_client=client,
                          notifier=self.notify, max_seconds=120)
        self.assertTrue(result.ok, result.reason)
        stored = {row["repo"]: (row["status"], row["reason"])
                  for row in self.db.execute("SELECT repo, status, reason FROM repo_protection")}
        self.assertEqual(stored, {path: ("protected", None) for path in fleet})
        mine = json.loads(self.db.execute(
            "SELECT body FROM state WHERE kind='heartbeat' AND key=? ORDER BY id DESC LIMIT 1",
            (protection.HEARTBEAT_KEY,)).fetchone()["body"])
        self.assertEqual((mine["protected"], mine["unknown"], mine["requests"]), (19, 0, 57))
        # The details that did not fit wait in the queue for the next run.
        self.assertGreater(result.queued, 0)

    def queue_behind(self, settled):
        """`settled` queued as authored pulls no run has read, as the hub's queue held 6,400 merged ones."""
        planned = adapter._queue(self.db)
        adapter._save_queue(self.db, [{"key": "github:" + url, "url": url, "why": ["author"], "blocking_labels": [],
                                       "explicit": False} for url in settled], planned["revision"])

    def test_a_new_open_pull_is_read_before_a_backlog_of_settled_ones(self):
        """pack sd:7844: authored pulls ai-governance-service#147 and control-logdna#15528 joined the queue at
        6,620 of 6,632, behind merged pulls no run had read, at about 47 details a run. Contributions lists a
        pull only once read, so a new one waited weeks. A pull GitHub has open that no run has read goes first."""
        urls = self.clone_pull(range(7, 11))
        self.queue_behind(urls[1:])
        # 14 requests: the search spends 4, the identity 1, so one pull fits.
        result = self.run_sync([urls[0]], max_requests=14)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual([row["url"] for row in core.projection(self.db)], [urls[0]])

    def test_an_open_pull_queued_by_an_earlier_run_is_read_first_too(self):
        """The queue an older run left: the open pull sits behind settled ones and this run's search does not
        return it. Its open shadow row still puts it first."""
        from sd_db.shadow_sync import store

        urls = self.clone_pull(range(7, 11))
        self.queue_behind([*urls[1:], urls[0]])
        with transaction(self.db):
            store(self.db, [{"tracker": "github", "url": urls[0], "repo": "example/project", "number": 7,
                             "kind": "pull", "title": "Contribution", "state": "open", "author": "operator",
                             "updated_at": self.now.isoformat(), "why": ["author"]}])
        result = self.run_sync(max_requests=14)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual([row["url"] for row in core.projection(self.db)], [urls[0]])

    def test_an_open_pull_whose_read_failed_is_not_read_first_again(self):
        """sd:7840's rule holds for open pulls too: a failed read leaves a heartbeat, the key is no longer fresh,
        so it rotates and cannot take a first slot every run (pack sd:7844 with sd:7840)."""
        found = [{"kind": "pull", "url": URL, "state": "open"}]
        self.assertEqual(adapter._unread_open(self.db, found, {"github:" + URL}), {"github:" + URL})
        self.assertEqual(adapter._unread_open(self.db, found, set()), set())

    def test_failed_first_record_rotates_and_explicit_work_retains_fairness(self):
        pending = [{"key": f"item:{n}", "explicit": True} for n in range(5)]
        pending += [{"key": f"github:{n}", "explicit": False} for n in range(5)]
        ordered = adapter._order(pending)
        self.assertEqual([row["explicit"] for row in ordered[:5]], [True, True, True, False, False])
        self.assertEqual(len(ordered), 10)
        self.api.rows[ROOT + "/pulls/7"] = Response(503, {}, "")
        self.run_sync([URL])
        self.assertEqual(adapter._queue(self.db)["pending"][0]["url"], URL)
        self.assertEqual(core.projection(self.db), [])

    def test_stale_open_rows_come_before_the_unobserved_backlog(self):
        # sd:7864: thousands of authored search rows buried the few last
        # observed open, so a closed PR with no item read open for weeks.
        now = "2026-10-18T08:00:00+00:00"
        keys = [f"github:backlog/{n}" for n in range(8)] + ["github:merged", "github:fresh", "github:stale"]
        pending = [{"key": key, "explicit": False} for key in keys]
        snapshots: dict[str, dict] = {key: {"observation": None} for key in keys}
        snapshots["github:merged"] = {"observation": {"state": "merged", "observed_at": "2026-10-15T08:00:00+00:00"}}
        snapshots["github:fresh"] = {"observation": {"state": "open", "observed_at": "2026-10-18T07:00:00+00:00"}}
        snapshots["github:stale"] = {"observation": {"state": "open", "observed_at": "2026-10-15T08:00:00+00:00"}}
        ordered = [row["key"] for row in adapter._order(pending, snapshots=snapshots, observed_at=now)]
        # sd:8116 (a): every row last read open is read each run, the oldest reading first, not only a day-old one.
        self.assertEqual(ordered[2:4], ["github:stale", "github:fresh"])
        self.assertEqual(sorted(ordered), sorted(keys))

    def stale_rows(self, count):
        """`count` rows last observed open three days before 2026-10-18 08:00."""
        rows = [{"key": f"github:stale/{n}", "explicit": False} for n in range(count)]
        old = {"observation": {"state": "open", "observed_at": "2026-10-15T08:00:00+00:00"}}
        return rows, {row["key"]: old for row in rows}

    def test_failing_stale_pulls_rotate_and_the_backlog_keeps_its_turn(self):
        # Review of sd:7864: a pull whose reads keep failing leaves only a
        # heartbeat, so its observation stays stale; ahead of the rest, enough
        # of them went ahead of the whole backlog every run. A held one rotates.
        stale, snapshots = self.stale_rows(12)
        backlog = [{"key": f"github:backlog/{n}", "explicit": False} for n in range(4)]
        held = {row["key"] for row in stale[10:]}
        keys = [row["key"] for row in adapter._order(
            [*backlog, *stale], snapshots=snapshots, observed_at="2026-10-18T08:00:00+00:00", held=held)]
        # The two head slots, then every open pull whose last read held, then queue order.
        self.assertEqual(keys[2:12], [row["key"] for row in stale[:10]])
        self.assertEqual(keys[12:], ["github:backlog/2", "github:backlog/3", *(row["key"] for row in stale[10:])])

    def test_unread_open_pulls_are_read_newest_queued_first(self):
        """sd:8116 (b): an authored pull opened today queued at 6,450 of 6,493 behind 60 older unread open ones,
        and the unread tier took the first ten in queue order, so no run reached it."""
        pending = [{"key": f"github:open/{n}", "explicit": False} for n in range(60)]
        unread = {row["key"] for row in pending}
        keys = [row["key"] for row in adapter._order(pending, unread=unread)]
        self.assertEqual(keys[:adapter.UNREAD_SLOTS], [f"github:open/{n}" for n in range(59, 49, -1)])

    def test_explicit_work_past_the_first_three_stays_ahead_of_stale_pulls(self):
        # Review of sd:7864: explicit rows past the first three, `item:`
        # dependency entries included, sorted after every stale pull.
        explicit = [{"key": f"item:{n}", "explicit": True} for n in range(5)]
        stale, snapshots = self.stale_rows(3)
        keys = [row["key"] for row in adapter._order(
            [*stale, *explicit], snapshots=snapshots, observed_at="2026-10-18T08:00:00+00:00")]
        self.assertEqual(keys, ["item:0", "item:1", "item:2", "github:stale/0", "github:stale/1",
                                "item:3", "item:4", "github:stale/2"])

    def test_every_key_is_ordered_once_whatever_tiers_it_falls_in(self):
        # Review of 6037f294a: explicit rows past the first three went on
        # again after the fresh tier had taken them, so a run read a key
        # twice and the second read failed its revision check.
        explicit = [{"key": f"item:{n}", "explicit": True} for n in range(5)]
        keys = [row["key"] for row in adapter._order(explicit, {row["key"] for row in explicit})]
        self.assertEqual(keys, ["item:0", "item:1", "item:2", "item:4", "item:3"])
        # Every tier at once: each row explicit or not, and fresh, unread
        # and stale together, with more rows than any tier's cap.
        for explicit_every in (1, 2, 3):
            stale, snapshots = self.stale_rows(30)
            pending = [{**row, "explicit": n % explicit_every == 0} for n, row in enumerate(stale)]
            every = {row["key"] for row in pending}
            keys = [row["key"] for row in adapter._order(pending, every, every, snapshots=snapshots,
                                                         observed_at="2026-10-18T08:00:00+00:00")]
            self.assertEqual(sorted(keys), sorted(every), explicit_every)

    def test_a_stale_open_pull_with_no_item_reads_closed_next_run(self):
        urls = self.clone_pull(range(7, 20))
        self.assertTrue(self.run_sync(urls[:1]).ok)
        self.assertEqual(core.projection(self.db)[0]["external_state"], "open")
        # Twelve never-read pulls an old search left queue ahead of it,
        # neither open in the shadow store nor explicit, as the authored
        # backlog does.
        saved = adapter._queue(self.db)
        backlog = [{"key": "github:" + url, "url": url, "why": ["author"], "blocking_labels": [],
                    "explicit": False} for url in urls[1:]]
        adapter._save_queue(self.db, backlog + saved["pending"], saved["revision"])
        self.now += timedelta(days=2)
        self.api.detail.update(state="closed")
        # Eight reads: the two head slots, the five newest never-read, then
        # the stale open pull ahead of the other five.
        self.assertTrue(self.run_sync(max_requests=4 + 1 + 9 * 8).ok)
        rows = {row["url"]: row for row in core.projection(self.db)}
        self.assertEqual(rows[urls[0]]["external_state"], "closed")
        self.assertEqual(rows[urls[0]]["lane"], "closed")
        self.assertNotIn(urls[3], rows)

    def backlog(self, numbers):
        """Stage never-read pulls in the queue, as an old search left them."""
        urls = self.clone_pull([7, *numbers])[1:]
        pending = [{"key": "github:" + url, "url": url, "why": ["author"], "blocking_labels": [],
                    "explicit": False} for url in urls]
        adapter._save_queue(self.db, pending, adapter._queue(self.db)["revision"])
        return urls

    def test_a_pull_found_tonight_is_read_ahead_of_a_never_read_backlog(self):
        # sd:7840: the queue read in discovery order, so a pull found tonight
        # waited behind thousands of old never-read pulls for weeks.
        old = self.backlog(range(8, 14))
        planned = adapter.plan(self.db)
        found = [{"kind": "pull", "url": URL, "why": ["author"]}]
        # The identity and three pulls: the two head slots, then the new one.
        client = self.api.client(requests=1 + 3 * adapter.PULL_REQUESTS)
        result = adapter.refresh(self.db, planned, found, client=client, observed_at=self.now.isoformat())
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["completed_keys"], ["github:" + old[0], "github:" + old[1], "github:" + URL])

    def test_a_never_read_pull_that_fails_loses_its_place_ahead(self):
        old = self.backlog(range(8, 13))
        rows: dict = self.api.rows
        rows[ROOT + "/pulls/7"] = Response(503, {}, "")
        planned = adapter.plan(self.db)
        found = [{"kind": "pull", "url": URL, "why": ["author"]}]
        client = self.api.client(requests=1 + 3 * adapter.PULL_REQUESTS)
        first = adapter.refresh(self.db, planned, found, client=client, observed_at=self.now.isoformat())
        # Two head slots, the new pull failing on one request, then the
        # newest old pull on what is left.
        self.assertEqual(first["completed_keys"], ["github:" + url for url in (old[0], old[1], old[4])])
        self.assertTrue(first["incomplete"][0].startswith("github:" + URL + ": "), first["incomplete"])
        # Its heartbeat marks it read: it rotates like any other key and
        # does not jump the queue again.
        planned = adapter.plan(self.db)
        fresh = adapter._fresh(self.db, planned["pending"], planned["snapshots"])
        self.assertEqual(fresh, {"github:" + old[2], "github:" + old[3]})
        ordered = adapter._order(planned["pending"], fresh)
        self.assertEqual([row["url"] for row in ordered], [old[2], old[3], old[0], old[1], URL, old[4]])

    def test_a_never_read_backlog_takes_a_few_slots_and_observed_work_keeps_its_turn(self):
        # Review of sd:7840: every never-read key went ahead of the rotation,
        # the ~4,900 backlog included, so explicit work past the first three
        # and every observed open pull waited weeks behind it.
        explicit = [{"key": f"item:{n}", "explicit": True} for n in range(4)]
        head = [{"key": f"github:head{n}", "explicit": False} for n in range(2)]
        observed = {"key": "github:observed", "explicit": False}
        backlog = [{"key": f"github:old{n}", "explicit": False} for n in range(30)]
        new = {"key": "github:new", "explicit": False}
        pending = [*explicit, *head, observed, *backlog, new]
        fresh = {row["key"] for row in [*backlog, new]}
        keys = [row["key"] for row in adapter._order(pending, fresh)]
        # The new pull is read first after the head slots, then the newest
        # few of the backlog; the rest follow the rotation.
        self.assertEqual(keys[5:10], ["github:new", "github:old29", "github:old28", "github:old27", "github:old26"])
        self.assertEqual(keys[10:12], ["item:3", "github:observed"])
        self.assertEqual(sorted(keys), sorted(row["key"] for row in pending))
        self.assertEqual(adapter.FRESH_SLOTS, 5)

    def test_registered_work_refreshes_without_search_match(self):
        item = core.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")["item"]["id"]
        self.add_comment()
        self.assertTrue(self.run_sync().ok)
        projected = core.projection(self.db)
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["item_id"], item)
        self.assertEqual(len(self.sent), 1)

    def test_notification_error_and_crash_hold_without_blind_resend(self):
        self.add_comment()
        self.run_sync([URL])
        state = core.snapshot(self.db, "github:" + URL)
        event = state["attention"][0]["id"]
        # Use a second real event, keeping the successful first receipt intact.
        self.api.rows[ROOT + "/issues/7/comments?per_page=100"][0]["id"] = 20
        sync(self.db, now=self.now, runner=self.search(), contribution_client=self.api.client(), notifier=lambda claim: "uncertain")
        self.now += timedelta(minutes=1)
        self.run_sync()
        self.assertEqual(len(self.sent), 1)
        notices = core.snapshot(self.db, "github:" + URL)["notifications"]
        self.assertEqual(notices[event]["status"], "sent")
        self.assertIn("uncertain", [row["status"] for row in notices.values()])
        self.api.rows[ROOT + "/issues/7/comments?per_page=100"][0]["id"] = 21
        with self.assertRaises(SystemExit):
            sync(self.db, now=self.now, runner=self.search(), contribution_client=self.api.client(),
                 notifier=lambda claim: (_ for _ in ()).throw(SystemExit("fixture crash")))
        self.now += timedelta(minutes=1)
        self.run_sync()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("sending", [row["status"] for row in core.snapshot(self.db, "github:" + URL)["notifications"].values()])

    def test_proved_no_process_start_can_retry_but_nonzero_timeout_are_uncertain(self):
        claim = {"title": "Attention", "message": "Fix", "url": URL}
        with patch("sd_db.contribution_sync.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(adapter._notify(claim), "not_started")
        with patch("sd_db.contribution_sync.subprocess.run", side_effect=subprocess.TimeoutExpired("notify", 60)):
            self.assertEqual(adapter._notify(claim), "uncertain")
        with patch("sd_db.contribution_sync.subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
            self.assertEqual(adapter._notify(claim), "uncertain")

    def test_collection_and_notifier_refuse_inside_writer_transaction(self):
        with transaction(self.db):
            with self.assertRaisesRegex(ValueError, "active transaction"):
                adapter.plan(self.db)
            with self.assertRaisesRegex(ValueError, "writer transaction"):
                adapter.dispatch_pending(self.db, notifier=self.notify)

    def test_search_and_detail_reads_share_the_request_ceiling(self):
        result = self.run_sync([URL], max_requests=5)
        # The four bucket searches leave one request, fewer than one detail
        # needs, so no detail is started -- and nothing is held for a read
        # that could never have finished. The pull is staged for the next run.
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.requests, 4)
        self.assertEqual(self.api.calls, [])
        self.assertEqual((result.queued, result.incomplete), (1, []))
        self.assertEqual(adapter._queue(self.db)["pending"][0]["url"], URL)

    def test_a_pull_whose_check_names_repeat_is_not_started_on_eight_requests(self):
        # A rerun leaves two `tests` runs, so the pull also reads its head's
        # workflow runs: nine detail requests (sd:1776). The search spends 4
        # and the identity 1; with eight left the pull waits, with nine it lands.
        head = fixtures.HEAD
        run = {"head_sha": head, "name": "tests", "app": {"id": 7}, "status": "completed"}
        self.api.rows[ROOT + f"/commits/{head}/check-runs?filter=latest&per_page=100"] = {"total_count": 2, "check_runs": [
            {**run, "id": 91, "conclusion": "failure", "check_suite": {"id": 1}},
            {**run, "id": 95, "conclusion": "success", "check_suite": {"id": 2}}]}
        self.api.rows[ROOT + f"/actions/runs?head_sha={head}&per_page=100"] = {"total_count": 2, "workflow_runs": [
            {"id": 1, "check_suite_id": 1, "workflow_id": 10, "event": "pull_request", "pull_requests": [{"number": 5, "base": {"ref": "main"}}]}, {"id": 2, "check_suite_id": 2, "workflow_id": 10, "event": "pull_request", "pull_requests": [{"number": 5, "base": {"ref": "main"}}]}]}
        result = self.run_sync([URL], max_requests=4 + 1 + 8)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual((result.queued, result.incomplete), (1, []))
        result = self.run_sync(max_requests=4 + 1 + 9)
        self.assertEqual((result.queued, result.incomplete), (0, []))
        self.assertEqual(core.snapshot(self.db, "github:" + URL)["observation"]["ci"], "success")

    def test_complete_release_proof_reaches_core_and_unblocks_once(self):
        fixture = fixtures.Dependencies()
        fixture.setUp()
        self.api = fixture.api
        merge = {"kind": "merge", "url": URL}
        task = core.capture(self.db, title="Package dependency", changes={"pull_url": URL, "depends_on": [merge, fixture.dep]}, who="operator")
        item = task["item"]["id"]
        self.assertTrue(self.run_sync().ok)
        observation = core.snapshot(self.db, f"item:{item}")["observation"]
        self.assertTrue(observation["satisfied"])
        self.assertEqual(len(observation["dependencies"]), 2)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.run_sync().ok)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(core.projection(self.db)[0]["local_status"], "planning")

    def test_queue_revision_conflict_does_not_overwrite_pending_work(self):
        self.run_sync([URL])
        planned = adapter.plan(self.db)
        queue = adapter._queue(self.db)
        adapter._save_queue(self.db, queue["pending"], queue["revision"])
        result = adapter.refresh(self.db, planned, [], client=self.api.client(), observed_at=self.now.isoformat())
        self.assertFalse(result["staged"])
        self.assertEqual(result["completed_keys"], [])
        self.assertIn("checkpoint changed", result["errors"][0])

    def test_closed_pull_pending_notices_drain_across_bounded_dispatches(self):
        self.add_comment()
        row = self.api.rows[ROOT + "/issues/7/comments?per_page=100"][0]
        self.api.rows[ROOT + "/issues/7/comments?per_page=100"] = [{**row, "id": n} for n in range(10, 17)]
        self.api.detail["state"] = "closed"
        self.assertTrue(self.run_sync([URL]).ok)
        self.assertEqual(len(self.sent), adapter.MAX_NOTIFICATIONS)
        self.assertEqual(len(adapter._queue(self.db)["pending"]), 1)
        self.assertTrue(self.run_sync().ok)
        self.assertEqual(len(self.sent), 7)
        self.assertTrue(self.run_sync().ok)
        self.assertEqual(adapter._queue(self.db)["pending"], [])

    def test_pending_work_receives_reserved_budget_even_when_search_fails(self):
        self.run_sync([URL])
        self.add_comment()
        def unavailable_search(argv):
            if argv == ["gh", "auth", "status"]:
                return 0, "", ""
            return 1, "", "fixture search unavailable"
        result = sync(self.db, now=self.now, runner=unavailable_search,
                      contribution_client=self.api.client(), notifier=self.notify, max_requests=20)
        self.assertFalse(result.ok)
        self.assertFalse(result.watermark_moved)
        self.assertEqual(len(self.sent), 1)
        self.assertLessEqual(result.requests, 20)


class IssueSync(SyncCase):
    """An `issue:` entry is routed by its key prefix, never by the presence of a URL."""

    def setUp(self):
        super().setUp()
        self.api = IssueApi()

    def draft(self, **changes):
        body = Path(self.temp.name) / "draft.md"
        body.write_text("# Bug\n")
        return {"target_repo": "example/project", "draft_title": "Bug", "draft_path": {
            "path": str(body), "sha256": hashlib.sha256(body.read_bytes()).hexdigest()}, **changes}

    def test_plan_queues_an_open_registered_issue_and_drops_a_closed_one(self):
        core.capture(self.db, title="Filed", changes={"issue_url": ISSUE_URL, "blocking_labels": ["needs-work"]}, who="operator")
        core.capture(self.db, title="Draft", changes=self.draft(), who="operator")
        planned = adapter.plan(self.db)
        key = "issue:" + ISSUE_URL
        self.assertEqual(planned["pending"], [{"key": key, "url": ISSUE_URL, "why": ["author"], "explicit": True,
                                               "blocking_labels": ["needs-work"]}])
        self.assertEqual(set(planned["snapshots"]), {key})
        alias = "https://github.com/Example/Project/issues/9/"
        self.assertEqual(adapter._merge(planned["pending"], [{"key": "issue:" + alias, "url": alias, "why": ["author"]}]),
                         [{"key": key, "url": ISSUE_URL, "why": ["author"]}])
        observation = self.api.observation()
        core.observe_issue(self.db, ISSUE_URL, {**observation, "state": "closed", "state_reason": "completed", "blocking_labels": ["needs-work"]},
                           expected_revision=core.snapshot(self.db, key)["revision"])
        self.assertEqual(core.projection(self.db)[1]["lane"], "closed")
        self.assertEqual(adapter.plan(self.db)["pending"], [])

    def test_refresh_routes_an_issue_entry_to_the_issue_collector_and_observe_issue(self):
        core.capture(self.db, title="Filed", changes={"issue_url": ISSUE_URL}, who="operator")
        self.api.rows[ROOT + "/issues/9/comments?per_page=100"] = [{"id": 19, "user": {"id": 2, "login": "maintainer"},
            "body": "Please address this", "author_association": "MEMBER", "updated_at": AT}]
        result = self.run_sync()
        self.assertTrue(result.ok, result.reason)
        key = "issue:" + ISSUE_URL
        self.assertEqual(result.incomplete, [])
        paths = [call[0] for call in self.api.calls]
        self.assertEqual(paths.count("/user"), 1)
        self.assertEqual(paths.count(ROOT + "/issues/9"), 2)
        self.assertFalse(any("/pulls/" in path for path in paths))
        state = core.snapshot(self.db, key)
        self.assertEqual((state["observation"]["state"], state["observation"]["repo"]), ("open", "example/project"))
        self.assertEqual([event["reason"] for event in state["attention"]], ["Maintainer comment"])
        self.assertEqual([claim["key"] for claim in self.sent], [key])
        self.assertEqual(self.sent[0]["url"], ISSUE_URL)
        self.assertEqual(self.observe_heartbeat(key), {"ok": True})
        self.assertEqual([row["key"] for row in adapter._queue(self.db)["pending"]], [key])
        # Closing it as completed is one more event; once its notice is sent the entry is terminal.
        self.api.issue.update(state="closed", state_reason="completed")
        self.api.timeline["nodes"] = [self.api.closed("COMPLETED")]
        self.assertTrue(self.run_sync().ok)
        self.assertEqual([claim["message"] for claim in self.sent], ["Maintainer comment", "Closed as completed"])
        self.assertEqual([row["key"] for row in adapter._queue(self.db)["pending"]], [key])
        self.assertTrue(self.run_sync().ok)
        self.assertEqual(adapter._queue(self.db)["pending"], [])
        self.assertEqual(len(self.sent), 2)
        row = core.projection(self.db)[0]
        self.assertEqual((row["lane"], row["external_state"], row["needs_you"]), ("awaiting_you", "closed", True))

    def test_incomplete_issue_read_is_held_on_the_issue_not_the_collect(self):
        core.capture(self.db, title="Filed", changes={"issue_url": ISSUE_URL}, who="operator")
        self.api.rows[ROOT + "/issues/9/comments?per_page=100"] = Response(503, {}, "")
        result = self.run_sync()
        key = "issue:" + ISSUE_URL
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.watermark_moved)
        self.assertEqual(result.incomplete, [f"{key}: API HTTP 503; retry on a later collection"])
        self.assertEqual(self.observe_heartbeat(key)["reason"], "Incomplete GitHub observation: API HTTP 503; retry on a later collection")
        self.assertIsNone(core.snapshot(self.db, key)["observation"])
        self.assertEqual(self.sent, [])

    def test_issue_dependency_reaches_the_collector_and_unblocks_the_item(self):
        dependency = {"kind": "issue", "url": ISSUE_URL}
        item = core.capture(self.db, title="Draft", changes=self.draft(depends_on=[dependency]), who="operator")["item"]["id"]
        key = f"item:{item}"
        self.api.rows["graphql"] = {"data": {"repository": {"issue": {"timelineItems": {
            "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}}}}
        result = self.run_sync()
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.incomplete, [f"{key}: {ISSUE_URL}: issue cross-references exceed one page"])
        self.assertEqual(core.projection(self.db)[0]["lane"], "awaiting_them")
        self.api.issue.update(state="closed", state_reason="completed")
        self.api.calls.clear()
        self.assertTrue(self.run_sync().ok)
        self.assertEqual([call[0] for call in self.api.calls], [ROOT + "/issues/9"])
        observation = core.snapshot(self.db, key)["observation"]
        self.assertTrue(observation["satisfied"])
        self.assertEqual(observation["dependencies"][0]["proof"], {"url": ISSUE_URL, "closed": True, "state_reason": "completed"})
        self.assertEqual(core.projection(self.db)[0]["lane"], "newly_unblocked")
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.run_sync().ok)
        self.assertEqual(len(self.sent), 1)

    def test_cost_prices_each_key_kind_from_measurement_and_refuses_an_unpriced_dependency(self):
        self.assertEqual(adapter._cost({"key": "issue:" + ISSUE_URL, "url": ISSUE_URL}), adapter.ISSUE_REQUESTS)
        self.assertEqual(adapter._cost({"key": "github:" + URL, "url": URL}), adapter.PULL_REQUESTS)
        self.api.observation()
        self.assertEqual(len(self.api.calls), adapter.ISSUE_REQUESTS)
        self.api.calls.clear()
        self.assertEqual(fixtures.dependency(self.api.client(), {"kind": "issue", "url": ISSUE_URL})["state"], "pending")
        self.assertEqual(len(self.api.calls), adapter.DEPENDENCY_REQUESTS["issue"])
        entry = {"key": "item:1", "depends_on": [{"kind": "item", "item": 2}, {"kind": "issue", "url": ISSUE_URL}, {"kind": "merge", "url": URL}]}
        self.assertEqual(adapter._cost(entry), adapter.DEPENDENCY_REQUESTS["issue"] + adapter.DEPENDENCY_REQUESTS["merge"])
        with self.assertRaisesRegex(ValueError, "unpriced dependency kind"):
            adapter._cost({"key": "item:1", "depends_on": [{"kind": "merge", "url": URL}, {"kind": "closed", "url": URL}]})
        self.assertEqual(set(adapter.DEPENDENCY_REQUESTS), {"item", "merge", "release", "issue"})


class LaneNotePass(SyncCase):
    """sd:7859: each sync notes every registered pull request's lane, so a merged one the queue left is noted too.

    The pass is a side observation (review round 2): what it cannot do is said in `lanes`, never a failed sync or a
    held watermark, as the protection collector's is.
    """

    def observed(self, state):
        snapshot = core.snapshot(self.db, "github:" + URL)
        snapshot["observation"] = {"state": state, "observed_at": AT}
        with transaction(self.db):
            core._save(self.db, snapshot)

    def shipped(self):
        from sd_db import ship
        from sd_db.workflow import capture_task

        item = capture_task(self.db, title="Shipped", priority=3, who="operator")["item"]["id"]
        ship.save(self.db, ship.receipt_key("example/project", "feat/x", item), 0,
                  {"item": item, "repository": "example/project", "branch": "feat/x", "phase": "ready_to_send",
                   "pull_request": {"url": URL}})
        return item

    def bad_receipts(self):
        with transaction(self.db):
            self.db.execute("INSERT INTO state(kind,key,timestamp,body) VALUES ('checkpoint','ship:bad',?,'{}')", (AT,))
            self.db.execute("INSERT INTO state(kind,key,timestamp,body) VALUES ('checkpoint','ship:odd',?,?)",
                            (AT, json.dumps({"protocol": 1, "item": 1, "pull_request": "not an object"})))

    def test_the_pass_notes_a_registered_pull_requests_lane(self):
        item = core.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")["item"]["id"]
        self.observed("merged")
        self.assertEqual(core.lane_notes(self.db, item), [])
        self.assertEqual(adapter.note_lanes(self.db), [])
        self.assertEqual([(n["lane"], n["state"]) for n in core.lane_notes(self.db, item)], [("merged", "merged")])

    def test_bad_receipts_are_said_and_the_good_one_is_linked_and_noted(self):
        item = self.shipped()
        self.observed("open")
        self.bad_receipts()
        self.assertEqual(adapter.note_lanes(self.db), [
            "ship receipt ship:bad: unreadable ship receipt: unsupported receipt protocol",
            "ship receipt ship:odd: pull_request is not an object"])
        self.assertEqual([(n["lane"], n["url"]) for n in core.lane_notes(self.db, item)], [("awaiting_them", URL)])

    def test_a_pass_that_raises_anything_is_said_and_the_other_pass_still_runs(self):
        item = core.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")["item"]["id"]
        self.observed("open")
        with patch.object(core, "link_shipped", side_effect=AttributeError("'str' object has no attribute 'get'")):
            self.assertEqual(adapter.note_lanes(self.db),
                             ["pull-request links were not read: AttributeError: 'str' object has no attribute 'get'"])
        self.assertEqual(len(core.lane_notes(self.db, item)), 1)
        from sd_db.errors import SdDbError
        with patch.object(core, "record_lanes", side_effect=SdDbError("'2026-09-10T00:00:00' has no zone")):
            self.assertEqual(adapter.note_lanes(self.db),
                             ["pull-request lane notes were not written: SdDbError: '2026-09-10T00:00:00' has no zone"])

    def test_a_bad_receipt_fails_no_sync_and_holds_no_watermark(self):
        self.run_sync([URL])
        self.bad_receipts()
        result = self.run_sync()
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.watermark_moved)
        self.assertTrue(self.tracker_heartbeat()["ok"])
        self.assertEqual(len(result.lanes), 2)
        self.assertIn("shadow sync: pull-request lanes: " + "; ".join(result.lanes), result.report())
