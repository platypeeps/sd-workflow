"""Canned API contracts: no real network, credentials, or notification channels."""

import copy
import json
import time
import unittest
from typing import Any
from unittest.mock import patch

from sd_db.contribution_github import (
    Client,
    Response,
    Unavailable,
    canonical_issue_url,
    dependency,
    issue,
    issue_parts,
    pull,
)
from sd_db.shadow_sync import _SearchBudget

URL = "https://github.com/example/project/pull/7"
ISSUE_URL = "https://github.com/example/project/issues/9"
ROOT = "/repos/example/project"
HEAD, BASE, MERGED, RELEASE = (char * 40 for char in "abcd")
AT = "2026-09-10T01:00:00Z"
OPERATOR = {"id": "1", "login": "operator"}


class Api:
    def __init__(self):
        self.calls = []
        self.detail = {"user": {"id": 1, "login": "operator"}, "head": {"sha": HEAD},
                       "base": {"sha": BASE}, "state": "open", "merged": False,
                       "draft": False, "title": "Contribution", "updated_at": AT,
                       "mergeable": True, "labels": [], "requested_reviewers": []}
        self.rows: dict[str, Any] = {
            "/user": {"id": 1, "login": "operator"},
            ROOT + "/pulls/7": self.detail,
            ROOT + "/pulls/7/reviews?per_page=100": [],
            ROOT + "/issues/7/comments?per_page=100": [],
            ROOT + "/pulls/7/comments?per_page=100": [],
            ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100": {"total_count": 1, "check_runs": [
                {"id": 91, "head_sha": HEAD, "status": "completed", "conclusion": "success"}]},
            ROOT + f"/commits/{HEAD}/status?per_page=100": {"sha": HEAD, "total_count": 0, "statuses": []},
            "graphql": {"data": {"repository": {"pullRequest": {"timelineItems": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}},
        }

    def __call__(self, path, fields, timeout):
        self.calls.append((path, fields, timeout))
        value = self.rows[path]
        if callable(value):
            value = value()
        if isinstance(value, Response):
            return value
        return Response(200, {}, json.dumps(value))

    def client(self, *, requests=100, seconds=30):
        return Client(_SearchBudget(requests, time.monotonic() + seconds), transport=self)

    def observation(self):
        return pull(self.client(), URL, OPERATOR, observed_at=AT)


class PullCollection(unittest.TestCase):
    def test_full_snapshot_keeps_actor_identity_reviews_and_current_ci(self):
        api = Api()
        api.rows[ROOT + "/pulls/7/reviews?per_page=100"] = [
            {"id": 2, "user": {"id": 2, "login": "maintainer"}, "state": "CHANGES_REQUESTED", "submitted_at": AT,
             "body": "Please fix this", "author_association": "MEMBER"},
            {"id": 3, "user": {"id": 2, "login": "maintainer"}, "state": "APPROVED", "submitted_at": AT}]
        api.rows[ROOT + "/issues/7/comments?per_page=100"] = [
            {"id": 5, "user": {"id": 3, "login": "reviewer"}, "body": "@operator please look", "updated_at": AT,
             "author_association": "NONE"},
            {"id": 6, "user": {"id": 1, "login": "operator"}, "body": "own update", "updated_at": AT}]
        result = api.observation()
        self.assertTrue(result["complete"])
        self.assertEqual([row["state"] for row in result["reviews"]], ["CHANGES_REQUESTED", "APPROVED"])
        self.assertEqual([row["actor_id"] for row in result["events"]], ["2", "3", "1"])
        self.assertTrue(result["events"][0]["maintainer"])
        self.assertTrue(result["events"][1]["mentions_operator"])
        self.assertEqual((result["ci"], result["ci_head"], result["ci_ids"]), ("success", HEAD, ["check-run:91"]))
        self.assertEqual([row[0] for row in api.calls].count(ROOT + "/pulls/7"), 2)

    def test_bot_comment_is_never_a_maintainer_comment(self):
        # sd:8148: a MEMBER bot's status comment is no maintainer ask.
        api = Api()
        comments: list[dict] = [
            {"id": 5, "user": {"id": 3, "login": "scanner[bot]", "type": "Bot"},
             "body": "scan passed", "updated_at": AT, "author_association": "MEMBER"},
            {"id": 6, "user": {"id": 4, "login": "scanner-app", "type": "User"},
             "body": "scan passed", "updated_at": AT, "author_association": "MEMBER",
             "performed_via_github_app": {"id": 9, "slug": "scanner"}},
            {"id": 7, "user": {"id": 2, "login": "maintainer", "type": "User"},
             "body": "Please rebase", "updated_at": AT, "author_association": "MEMBER"}]
        api.rows[ROOT + "/issues/7/comments?per_page=100"] = comments
        result = api.observation()
        self.assertEqual([row["maintainer"] for row in result["events"]], [False, False, True])

    def test_a_listed_login_is_no_maintainer_on_every_comment_path(self):
        # pack sd:8148: the issue thread, the review thread and a review's own body all carry the association.
        api = Api()
        def row(number, login):
            return {"id": number, "user": {"id": number, "login": login, "type": "User"}, "body": "Scan passed",
                    "updated_at": AT, "author_association": "MEMBER"}
        api.rows[ROOT + "/issues/7/comments?per_page=100"] = [row(5, "scan-account"), row(6, "maintainer")]
        api.rows[ROOT + "/pulls/7/comments?per_page=100"] = [row(7, "scan-account")]
        api.rows[ROOT + "/pulls/7/reviews?per_page=100"] = [
            {**row(8, "scan-account"), "state": "COMMENTED", "submitted_at": AT}]
        unlisted = api.observation()
        result = pull(api.client(), URL, OPERATOR, observed_at=AT, automation=frozenset({"scan-account"}))
        comments = [event for event in result["events"] if event["kind"] == "comment"]
        self.assertEqual(sorted(event["maintainer"] for event in comments), [False, False, False, True])
        self.assertEqual([event["maintainer"] for event in unlisted["events"] if event["kind"] == "comment"].count(False), 0)

    def test_requested_reviewers_keep_user_logins_not_teams(self):
        # sd:8084: a team request does not name the operator.
        api = Api()
        api.detail["requested_reviewers"] = [{"id": 1, "login": "operator"}, {"id": 4, "login": "peer"}]
        api.detail["requested_teams"] = [{"id": 9, "slug": "core"}]
        self.assertEqual(api.observation()["requested_reviewers"], ["operator", "peer"])
        del api.detail["requested_reviewers"]
        with self.assertRaises(Unavailable):
            api.observation()

    def test_review_requested_why_needs_the_operator_asked_by_name(self):
        # sd:8360: a pull stored from a team-only request, or one the operator
        # has reviewed, keeps its search why; the read drops it.
        api = Api()
        why = ["mentioned", "review-requested"]
        api.detail["requested_teams"] = [{"id": 9, "slug": "core"}]

        def observe():
            return pull(api.client(), URL, OPERATOR, observed_at=AT, why=why)["why"]

        self.assertEqual(observe(), ["mentioned"])
        api.detail["requested_reviewers"] = [{"id": 1, "login": "Operator"}]
        self.assertEqual(observe(), why)

    def test_typed_timeline_uses_originator_not_mentioned_actor(self):
        api = Api()
        api.rows["graphql"]["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"] = [
            {"__typename": "MentionedEvent", "actor": {"databaseId": 1, "login": "operator"}},
            {"__typename": "ConvertToDraftEvent", "id": "draft:1", "actor": {"databaseId": 2, "login": "owner"}, "createdAt": AT},
            {"__typename": "LabeledEvent", "id": "label:1", "actor": {"databaseId": 2, "login": "owner"}, "createdAt": AT, "label": {"name": "needs-author"}}]
        result = api.observation()
        self.assertEqual([row["kind"] for row in result["events"]], ["converted_to_draft", "label_added"])
        self.assertEqual(result["blocking_labels"], [])

    def test_a_review_request_names_the_asked_user_and_a_force_push_is_read(self):
        """sd:8116 (c): the author asking a reviewer again, and a force push, date the pull request's last turn."""
        api = Api()
        graphql: dict = api.rows["graphql"]  # type: ignore[assignment]
        graphql["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"] = [
            {"__typename": "HeadRefForcePushedEvent", "id": "push:1", "actor": {"databaseId": 1, "login": "operator"}, "createdAt": AT},
            {"__typename": "ReviewRequestedEvent", "id": "ask:1", "actor": {"databaseId": 1, "login": "operator"}, "createdAt": AT,
             "requestedReviewer": {"databaseId": 2}},
            {"__typename": "ReviewRequestedEvent", "id": "ask:2", "actor": {"databaseId": 1, "login": "operator"}, "createdAt": AT,
             "requestedReviewer": {}}]
        events = api.observation()["events"]
        self.assertEqual([(row["kind"], row.get("reviewer_id")) for row in events],
                         [("pushed", None), ("review_requested", "2"), ("review_requested", None)])

    def test_null_actor_refuses_complete_snapshot(self):
        api = Api()
        api.rows[ROOT + "/issues/7/comments?per_page=100"] = [{"id": 5, "user": None, "body": "hello", "updated_at": AT}]
        with self.assertRaisesRegex(Unavailable, "actor identity"):
            api.observation()

    def test_null_mergeability_is_unknown_and_invalid_value_refuses(self):
        api = Api()
        api.detail["mergeable"] = None
        self.assertEqual(api.observation()["mergeable"], "unknown")
        api.detail["mergeable"] = "false"
        with self.assertRaisesRegex(Unavailable, "mergeability"):
            api.observation()

    def test_head_drift_and_old_head_checks_refuse(self):
        api = Api()
        count = []
        def detail():
            count.append(True)
            return api.detail if len(count) == 1 else {**api.detail, "head": {"sha": BASE}}
        api.rows[ROOT + "/pulls/7"] = detail
        with self.assertRaisesRegex(Unavailable, "changed while"):
            api.observation()
        api = Api()
        api.rows[ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"]["check_runs"][0]["head_sha"] = BASE
        with self.assertRaisesRegex(Unavailable, "stale head"):
            api.observation()
        api = Api()
        api.rows[ROOT + f"/commits/{HEAD}/status?per_page=100"]["sha"] = BASE
        with self.assertRaisesRegex(Unavailable, "stale head"):
            api.observation()

    def test_pending_empty_ci_and_legacy_failure_are_not_green(self):
        api = Api()
        api.rows[ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"] = {"total_count": 0, "check_runs": []}
        self.assertEqual(api.observation()["ci"], "pending")
        api.rows[ROOT + f"/commits/{HEAD}/status?per_page=100"] = {"sha": HEAD, "total_count": 1, "statuses": [{"id": 8, "state": "failure"}]}
        self.assertEqual(api.observation()["ci"], "failure")

    def test_newest_run_of_each_check_decides(self):
        # Close/reopen or a body edit reruns a check on the same head (sd:1776).
        api = Api()
        key = ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"
        def run(id, conclusion, name="tests", app=7, status="completed", suite=None):
            return {"id": id, "name": name, "app": {"id": app}, "head_sha": HEAD, "status": status,
                    "conclusion": conclusion, "check_suite": {"id": suite or id}}
        # Each check suite above is one run of workflow 10.
        api.rows[ROOT + f"/actions/runs?head_sha={HEAD}&per_page=100"] = lambda: {
            "total_count": 2, "workflow_runs": [{"id": 1, "check_suite_id": 91, "workflow_id": 10, "event": "pull_request", "pull_requests": [{"number": 5, "base": {"ref": "main"}}]},
                                                {"id": 2, "check_suite_id": 95, "workflow_id": 10, "event": "pull_request", "pull_requests": [{"number": 5, "base": {"ref": "main"}}]}]}
        api.rows[key] = {"total_count": 2, "check_runs": [run(95, "success"), run(91, "failure")]}
        self.assertEqual(api.observation()["ci"], "success")
        self.assertEqual(api.observation()["ci_ids"], ["check-run:95"])
        api.rows[key] = {"total_count": 2, "check_runs": [run(91, "success"), run(95, "failure")]}
        self.assertEqual(api.observation()["ci"], "failure")
        api.rows[key] = {"total_count": 2, "check_runs": [run(91, "failure"), run(95, None, status="in_progress")]}
        self.assertEqual(api.observation()["ci"], "pending")
        # The suite decides, not the run id: a later run in the older suite
        # still loses to the newer suite.
        api.rows[key] = {"total_count": 2, "check_runs": [run(99, "failure", suite=91), run(96, "success", suite=95)]}
        self.assertEqual(api.observation()["ci"], "success")
        self.assertEqual(api.observation()["ci_ids"], ["check-run:96"])
        # Another app's check of the same name is its own check, not a rerun.
        api.rows[key] = {"total_count": 2, "check_runs": [run(95, "success", app=8), run(91, "failure")]}
        self.assertEqual(api.observation()["ci"], "failure")

    def test_a_skipped_newer_suite_does_not_replace_a_completed_older_one(self):
        # A job conditioned on the payload (`if: github.event.action == 'opened'`)
        # runs on open and is skipped on the edit that reruns the workflow.
        # The skipped suite proves nothing about the head; letting it replace
        # the older suite read a failure as green (sd:1824).
        api = Api()
        key = ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"
        def run(id, conclusion, suite, name="tests", status="completed"):
            return {"id": id, "name": name, "app": {"id": 7}, "head_sha": HEAD, "status": status,
                    "conclusion": conclusion, "check_suite": {"id": suite}}
        pulls = [{"number": 5, "base": {"ref": "main"}}]
        api.rows[ROOT + f"/actions/runs?head_sha={HEAD}&per_page=100"] = {"total_count": 2, "workflow_runs": [
            {"id": 1, "check_suite_id": 1, "workflow_id": 10, "event": "pull_request", "pull_requests": pulls},
            {"id": 2, "check_suite_id": 2, "workflow_id": 10, "event": "pull_request", "pull_requests": pulls}]}
        api.rows[key] = {"total_count": 2, "check_runs": [run(91, "failure", 1), run(95, "skipped", 2)]}
        self.assertEqual(api.observation()["ci"], "failure")
        self.assertEqual(api.observation()["ci_ids"], ["check-run:91"])
        api.rows[key] = {"total_count": 2, "check_runs": [run(91, "success", 1), run(95, "skipped", 2)]}
        self.assertEqual(api.observation()["ci"], "success")
        self.assertEqual(api.observation()["ci_ids"], ["check-run:91"])
        # A suite that ran anything is a real rerun and still decides, whole.
        api.rows[key] = {"total_count": 3, "check_runs": [
            run(91, "failure", 1), run(95, "skipped", 2), run(96, "success", 2, name="lint")]}
        self.assertEqual(api.observation()["ci"], "success")
        self.assertEqual(api.observation()["ci_ids"], ["check-run:95", "check-run:96"])
        # A newer suite still in progress is not skipped: it replaces.
        api.rows[key] = {"total_count": 2, "check_runs": [run(91, "failure", 1), run(95, None, 2, status="in_progress")]}
        self.assertEqual(api.observation()["ci"], "pending")
        # When every suite was skipped, the newest decides as before.
        api.rows[key] = {"total_count": 2, "check_runs": [run(91, "skipped", 1), run(95, "skipped", 2)]}
        self.assertEqual(api.observation()["ci"], "success")
        self.assertEqual(api.observation()["ci_ids"], ["check-run:95"])

    def test_same_named_checks_of_different_workflows_both_count(self):
        # Two workflows can each publish a job named `tests` through the same app.
        api = Api()
        key = ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"
        def run(id, conclusion, suite):
            return {"id": id, "name": "tests", "app": {"id": 7}, "head_sha": HEAD, "status": "completed",
                    "conclusion": conclusion, "check_suite": {"id": suite}}
        runs = ROOT + f"/actions/runs?head_sha={HEAD}&per_page=100"
        api.rows[key] = {"total_count": 2, "check_runs": [run(91, "failure", 1), run(95, "success", 2)]}
        api.rows[runs] = {"total_count": 2, "workflow_runs": [{"id": 1, "check_suite_id": 1, "workflow_id": 10},
                                                              {"id": 2, "check_suite_id": 2, "workflow_id": 11}]}
        self.assertEqual(api.observation()["ci"], "failure")
        self.assertEqual(api.observation()["ci_ids"], ["check-run:91", "check-run:95"])
        # A suite no workflow run names (another CI app) proves no rerun either.
        api.rows[runs] = {"total_count": 1, "workflow_runs": [{"id": 1, "check_suite_id": 1, "workflow_id": 10}]}
        self.assertEqual(api.observation()["ci"], "failure")
        # A single run per name needs no workflow lookup.
        api.rows[key] = {"total_count": 1, "check_runs": [run(95, "success", 2)]}
        del api.rows[runs]
        self.assertEqual(api.observation()["ci"], "success")

    def test_two_jobs_of_one_name_in_one_workflow_both_count(self):
        # A workflow can hold two jobs that display one name; neither is a rerun.
        api = Api()
        key = ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"
        def run(id, conclusion, suite):
            return {"id": id, "name": "tests", "app": {"id": 7}, "head_sha": HEAD, "status": "completed",
                    "conclusion": conclusion, "check_suite": {"id": suite}}
        api.rows[key] = {"total_count": 2, "check_runs": [run(91, "failure", 1), run(95, "success", 1)]}
        api.rows[ROOT + f"/actions/runs?head_sha={HEAD}&per_page=100"] = {
            "total_count": 1, "workflow_runs": [{"id": 1, "check_suite_id": 1, "workflow_id": 10}]}
        self.assertEqual(api.observation()["ci"], "failure")
        self.assertEqual(api.observation()["ci_ids"], ["check-run:91", "check-run:95"])

    def test_one_workflow_on_two_events_keeps_both_results(self):
        # A push run and a workflow_dispatch run of one workflow on one head
        # are two executions; the newer does not replace the older.
        api = Api()
        key = ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"
        def run(id, conclusion, suite):
            return {"id": id, "name": "tests", "app": {"id": 7}, "head_sha": HEAD, "status": "completed",
                    "conclusion": conclusion, "check_suite": {"id": suite}}
        api.rows[key] = {"total_count": 2, "check_runs": [run(91, "failure", 1), run(95, "success", 2)]}
        api.rows[ROOT + f"/actions/runs?head_sha={HEAD}&per_page=100"] = {"total_count": 2, "workflow_runs": [
            {"id": 1, "check_suite_id": 1, "workflow_id": 10, "event": "push"},
            {"id": 2, "check_suite_id": 2, "workflow_id": 10, "event": "workflow_dispatch"}]}
        self.assertEqual(api.observation()["ci"], "failure")
        self.assertEqual(api.observation()["ci_ids"], ["check-run:91", "check-run:95"])

    def test_two_dispatches_of_one_workflow_keep_both_results(self):
        # Two workflow_dispatch runs can carry different inputs: neither
        # replaces the other, whatever their order.
        api = Api()
        key = ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"
        def run(id, conclusion, suite):
            return {"id": id, "name": "tests", "app": {"id": 7}, "head_sha": HEAD, "status": "completed",
                    "conclusion": conclusion, "check_suite": {"id": suite}}
        api.rows[key] = {"total_count": 2, "check_runs": [run(91, "failure", 1), run(95, "success", 2)]}
        api.rows[ROOT + f"/actions/runs?head_sha={HEAD}&per_page=100"] = {"total_count": 2, "workflow_runs": [
            {"id": 1, "check_suite_id": 1, "workflow_id": 10, "event": "workflow_dispatch"},
            {"id": 2, "check_suite_id": 2, "workflow_id": 10, "event": "workflow_dispatch"}]}
        self.assertEqual(api.observation()["ci"], "failure")
        self.assertEqual(api.observation()["ci_ids"], ["check-run:91", "check-run:95"])

    def test_one_push_workflow_on_two_branches_keeps_both_results(self):
        # One head pushed to two branches runs the workflow twice with two
        # refs; the newer branch's success does not hide the other's failure.
        api = Api()
        key = ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"
        def run(id, conclusion, suite):
            return {"id": id, "name": "tests", "app": {"id": 7}, "head_sha": HEAD, "status": "completed",
                    "conclusion": conclusion, "check_suite": {"id": suite}}
        api.rows[key] = {"total_count": 2, "check_runs": [run(91, "failure", 1), run(95, "success", 2)]}
        api.rows[ROOT + f"/actions/runs?head_sha={HEAD}&per_page=100"] = {"total_count": 2, "workflow_runs": [
            {"id": 1, "check_suite_id": 1, "workflow_id": 10, "event": "push", "head_branch": "release"},
            {"id": 2, "check_suite_id": 2, "workflow_id": 10, "event": "push", "head_branch": "feature"}]}
        self.assertEqual(api.observation()["ci"], "failure")
        self.assertEqual(api.observation()["ci_ids"], ["check-run:91", "check-run:95"])

    def _two_pull_request_runs(self, first, second):
        api = Api()
        key = ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"
        def run(id, conclusion, suite):
            return {"id": id, "name": "tests", "app": {"id": 7}, "head_sha": HEAD, "status": "completed",
                    "conclusion": conclusion, "check_suite": {"id": suite}}
        api.rows[key] = {"total_count": 2, "check_runs": [run(91, "failure", 1), run(95, "success", 2)]}
        api.rows[ROOT + f"/actions/runs?head_sha={HEAD}&per_page=100"] = {"total_count": 2, "workflow_runs": [
            {"id": 1, "check_suite_id": 1, "workflow_id": 10, "event": "pull_request", "head_branch": "feature", **first},
            {"id": 2, "check_suite_id": 2, "workflow_id": 10, "event": "pull_request", "head_branch": "feature", **second}]}
        return api.observation()

    def test_two_pull_requests_from_one_branch_keep_both_results(self):
        # One head branch opened against two bases runs twice with two merge
        # refs; the newer PR's success does not hide the other PR's failure.
        seen = self._two_pull_request_runs({"pull_requests": [{"number": 5, "base": {"ref": "main"}}]},
                                           {"pull_requests": [{"number": 6, "base": {"ref": "release"}}]})
        self.assertEqual((seen["ci"], seen["ci_ids"]), ("failure", ["check-run:91", "check-run:95"]))

    def test_pull_request_runs_without_pull_request_data_keep_both_results(self):
        # A fork's run lists no pull requests, so nothing proves it replays the other.
        seen = self._two_pull_request_runs({"pull_requests": []}, {})
        self.assertEqual((seen["ci"], seen["ci_ids"]), ("failure", ["check-run:91", "check-run:95"]))

    def test_an_unavailable_workflow_lookup_keeps_every_run(self):
        for status in (403, 503):
            with self.subTest(status=status):
                api = Api()
                key = ROOT + f"/commits/{HEAD}/check-runs?filter=latest&per_page=100"
                def run(id, conclusion, suite):
                    return {"id": id, "name": "tests", "app": {"id": 7}, "head_sha": HEAD,
                            "status": "completed", "conclusion": conclusion, "check_suite": {"id": suite}}
                api.rows[key] = {"total_count": 2, "check_runs": [run(91, "failure", 1), run(95, "success", 2)]}
                api.rows[ROOT + f"/actions/runs?head_sha={HEAD}&per_page=100"] = Response(status, {}, "{}")
                self.assertEqual(api.observation()["ci"], "failure")

    def test_closed_unmerged_is_collected_without_open_filter(self):
        api = Api()
        api.detail["state"] = "closed"
        self.assertEqual(api.observation()["state"], "closed")
        api.detail["merged"] = True
        self.assertEqual(api.observation()["state"], "merged")


class BoundedTransport(unittest.TestCase):
    def test_rest_pagination_follows_all_pages_and_refuses_duplicate(self):
        api = Api()
        api.rows["/rows?per_page=100"] = Response(200, {"Link": '<https://api.github.com/rows?per_page=100&page=2>; rel="next"'}, '[{"id":1}]')
        api.rows["/rows?per_page=100&page=2"] = [{"id": 2}]
        self.assertEqual(len(api.client().pages("/rows")), 2)
        api.rows["/rows?per_page=100&page=2"] = [{"id": 1}]
        with self.assertRaisesRegex(Unavailable, "repeated identities"):
            api.client().pages("/rows")

    def test_truncated_counts_repeated_cursor_and_cross_origin_refuse(self):
        api = Api()
        api.rows["/rows?per_page=100"] = {"total_count": 2, "rows": [{"id": 1}]}
        with self.assertRaisesRegex(Unavailable, "truncated"):
            api.client().pages("/rows", member="rows")
        api.rows["/rows?per_page=100"] = Response(200, {"Link": '<https://evil.example/rows?page=2>; rel="next"'}, '[]')
        with self.assertRaisesRegex(Unavailable, "escaped"):
            api.client().pages("/rows")
        info = api.rows["graphql"]["data"]["repository"]["pullRequest"]["timelineItems"]["pageInfo"]
        info.update(hasNextPage=True, endCursor="again")
        with self.assertRaisesRegex(Unavailable, "incomplete GitHub timeline"):
            api.client().timeline("example", "project", 7)

    def test_http_partial_graphql_bad_json_and_budget_refuse(self):
        api = Api()
        for value in (Response(429, {}, ""), Response(200, {}, "not JSON"), {"errors": [{"message": "partial"}], "data": {}}):
            api.rows["/failure"] = value
            with self.subTest(value=value), self.assertRaises(Unavailable):
                api.client().get("/failure")
        for options in ({"requests": 0}, {"seconds": -1}):
            with self.assertRaisesRegex(Unavailable, "budget"):
                api.client(**options).get("/user")

    def test_gh_fixed_get_argv_parses_headers_without_saving_stderr(self):
        calls = []
        def runner(argv):
            calls.append(argv)
            return 0, 'HTTP/2.0 200 OK\nContent-Type: application/json\n\n{"id":1}', "secret must not be retained"
        client = Client(_SearchBudget(1, time.monotonic() + 30), runner=runner)
        self.assertEqual(client.get("/user"), {"id": 1})
        self.assertEqual(calls, [["gh", "api", "--include", "--method", "GET", "/user"]])


class Dependencies(unittest.TestCase):
    def setUp(self):
        self.api = Api()
        self.api.detail.update(merged=True, merge_commit_sha=MERGED)
        self.dep = {"kind": "release", "repo": "example/project", "tag": "v1.2.3", "contains_pull": URL,
                    "package": {"name": "aiounifi", "version": "1.2.3"}}
        self.api.rows.update({
            ROOT + "/releases/tags/v1.2.3": {"id": 21, "tag_name": "v1.2.3", "draft": False, "published_at": AT},
            ROOT + "/git/ref/tags/v1.2.3": {"object": {"type": "tag", "sha": BASE}},
            ROOT + f"/git/tags/{BASE}": {"object": {"type": "commit", "sha": RELEASE}},
            ROOT + f"/compare/{MERGED}...{RELEASE}": {"status": "ahead", "merge_base_commit": {"sha": MERGED}},
            "https://pypi.org/pypi/aiounifi/1.2.3/json": {"info": {"name": "aiounifi", "version": "1.2.3"}, "urls": [
                {"filename": "aiounifi.whl", "yanked": False, "digests": {"sha256": "e" * 64}, "upload_time_iso_8601": AT}]},
        })

    def test_merge_tag_ancestry_and_exact_package_all_have_separate_proof(self):
        result = dependency(self.api.client(), self.dep)
        self.assertEqual(result["state"], "satisfied")
        self.assertEqual(result["dependency"], self.dep)
        self.assertEqual(result["proof"]["merge_commit_sha"], MERGED)
        self.assertEqual(result["proof"]["tag_commit"], RELEASE)
        self.assertEqual(result["proof"]["package"]["version"], "1.2.3")
        self.assertNotEqual(result["proof"]["merge_commit_sha"], HEAD)

    def test_merge_alone_does_not_require_release(self):
        result = dependency(self.api.client(), {"kind": "merge", "url": URL})
        self.assertEqual(result["state"], "satisfied")
        self.assertEqual(len(self.api.calls), 1)

    def test_unmerged_unreleased_not_contained_unpublished_and_yanked_stay_pending(self):
        for path, replacement in (
            (ROOT + "/pulls/7", {"merged": False}),
            (ROOT + "/releases/tags/v1.2.3", Response(404, {}, "")),
            (ROOT + f"/compare/{MERGED}...{RELEASE}", {"status": "diverged", "merge_base_commit": {"sha": BASE}}),
            ("https://pypi.org/pypi/aiounifi/1.2.3/json", Response(404, {}, "")),
            ("https://pypi.org/pypi/aiounifi/1.2.3/json", {"info": {"name": "aiounifi", "version": "1.2.3"}, "urls": []}),
        ):
            with self.subTest(path=path, replacement=replacement):
                old = self.api.rows[path]
                self.api.rows[path] = replacement
                self.assertEqual(dependency(self.api.client(), self.dep)["state"], "pending")
                self.api.rows[path] = old
        self.api.rows["https://pypi.org/pypi/aiounifi/1.2.3/json"]["urls"][0]["yanked"] = True
        self.assertEqual(dependency(self.api.client(), self.dep)["state"], "pending")

    def test_missing_version_transient_failure_and_changed_tag_are_unknown(self):
        dep = copy.deepcopy(self.dep)
        del dep["package"]["version"]
        self.assertEqual(dependency(self.api.client(), dep)["state"], "unknown")
        self.api.rows[ROOT + "/releases/tags/v1.2.3"] = Response(503, {}, "")
        self.assertEqual(dependency(self.api.client(), self.dep)["state"], "unknown")
        self.setUp()
        calls = []
        def changing_ref():
            calls.append(True)
            return {"object": {"type": "commit", "sha": RELEASE if len(calls) == 1 else HEAD}}
        self.api.rows[ROOT + "/git/ref/tags/v1.2.3"] = changing_ref
        self.assertEqual(dependency(self.api.client(), self.dep)["state"], "unknown")

    def test_tag_peeling_and_response_size_are_bounded(self):
        self.api.rows[ROOT + f"/git/tags/{BASE}"] = {"object": {"type": "tag", "sha": BASE}}
        self.assertEqual(dependency(self.api.client(), self.dep)["state"], "unknown")
        self.assertLessEqual(len(self.api.calls), 11)
        with patch("sd_db.contribution_github.MAX_BYTES", 3), self.assertRaisesRegex(Unavailable, "size limit"):
            self.api.client().get("/user")


class IssueApi(Api):
    """The same transport answering for issue 9: detail, comments and an issue-rooted timeline."""

    def __init__(self):
        super().__init__()
        self.issue = {"user": {"id": 1, "login": "operator"}, "state": "open", "state_reason": None,
                      "title": "Bug report", "updated_at": AT, "labels": []}
        self.timeline = {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        self.rows.update({
            ROOT + "/issues/9": self.issue,
            ROOT + "/issues/9/comments?per_page=100": [],
            "graphql": {"data": {"repository": {"issue": {"timelineItems": self.timeline}}}},
        })

    def observation(self):
        return issue(self.client(), ISSUE_URL, OPERATOR, observed_at=AT)

    def closed(self, reason, id="closed:1"):
        return {"__typename": "ClosedEvent", "id": id, "actor": {"databaseId": 2, "login": "owner"},
                "createdAt": AT, "stateReason": reason}


class IssueUrls(unittest.TestCase):
    def test_a_listed_login_is_no_maintainer_on_an_issue(self):
        api = IssueApi()
        api.rows[ROOT + "/issues/9/comments?per_page=100"] = [
            {"id": 5, "user": {"id": 5, "login": "Scan-Account", "type": "User"}, "body": "Scan passed",
             "updated_at": AT, "author_association": "MEMBER"}]
        self.assertEqual([event["maintainer"] for event in api.observation()["events"] if event["kind"] == "comment"], [True])
        result = issue(api.client(), ISSUE_URL, OPERATOR, observed_at=AT, automation=frozenset({"scan-account"}))
        self.assertEqual([event["maintainer"] for event in result["events"] if event["kind"] == "comment"], [False])

    def test_parts_lower_case_the_repository_and_drop_the_trailing_slash(self):
        self.assertEqual(issue_parts("https://github.com/Example/Project/issues/9/"), ("example", "project", 9))
        self.assertEqual(canonical_issue_url("https://github.com/Example/Project/issues/9/"), ISSUE_URL)

    def test_pull_urls_zero_numbers_and_suffixes_are_refused(self):
        for url in (URL, "https://github.com/example/project/issues/0", ISSUE_URL + "#issuecomment-1",
                    ISSUE_URL + "?x=1", "http://github.com/example/project/issues/9", ""):
            with self.subTest(url=url), self.assertRaisesRegex(Unavailable, "invalid GitHub issue URL"):
                issue_parts(url)


class IssueCollection(unittest.TestCase):
    def test_snapshot_keeps_comments_labels_and_every_close_reason(self):
        api = IssueApi()
        api.issue["labels"] = [{"name": "bug"}]
        api.rows[ROOT + "/issues/9/comments?per_page=100"] = [
            {"id": 5, "user": {"id": 2, "login": "owner"}, "body": "Thanks", "updated_at": AT, "author_association": "OWNER"},
            {"id": 6, "user": {"id": 3, "login": "passerby"}, "body": "@operator same here", "updated_at": AT,
             "author_association": "NONE"}]
        api.timeline["nodes"] = [
            {"__typename": "MentionedEvent", "actor": {"databaseId": 1, "login": "operator"}},
            {"__typename": "LabeledEvent", "id": "label:1", "actor": {"databaseId": 2, "login": "owner"}, "createdAt": AT,
             "label": {"name": "bug"}},
            {"__typename": "UnlabeledEvent", "id": "label:2", "actor": {"databaseId": 2, "login": "owner"}, "createdAt": AT,
             "label": {"name": "triage"}},
            api.closed("COMPLETED", "closed:1"),
            {"__typename": "ReopenedEvent", "id": "reopened:1", "actor": {"databaseId": 1, "login": "operator"}, "createdAt": AT},
            api.closed("NOT_PLANNED", "closed:2"),
            api.closed("DUPLICATE", "closed:3"),
            api.closed(None, "closed:4")]
        result = api.observation()
        self.assertTrue(result["complete"])
        self.assertEqual((result["repo"], result["title"], result["state"], result["state_reason"], result["labels"]),
                         ("example/project", "Bug report", "open", None, ["bug"]))
        self.assertEqual([row["kind"] for row in result["events"]],
                         ["comment", "comment", "label_added", "label_removed", "closed_completed", "reopened",
                          "closed_not_planned", "closed_not_planned", "closed"])
        self.assertEqual([row["actor_id"] for row in result["events"]], ["2", "3", "2", "2", "2", "1", "2", "2", "2"])
        self.assertTrue(result["events"][0]["maintainer"])
        self.assertTrue(result["events"][1]["mentions_operator"])
        self.assertEqual((result["events"][2]["label"], result["events"][3]["label"]), ("bug", "triage"))
        self.assertEqual(result["events"][4]["id"], "closed:1")
        for key in ("reviews", "ci", "ci_head", "ci_ids", "head", "base", "mergeable", "draft"):
            self.assertNotIn(key, result)
        self.assertEqual([row[0] for row in api.calls].count(ROOT + "/issues/9"), 2)
        self.assertEqual(len(api.calls), 4)

    def test_rest_state_reason_maps_to_the_two_the_core_knows(self):
        api = IssueApi()
        for given, expected in (("completed", "completed"), ("not_planned", "not_planned"), ("duplicate", "not_planned"),
                                ("reopened", None), (None, None), ("something-new", None)):
            with self.subTest(given=given):
                api.issue.update(state="closed" if expected else "open", state_reason=given)
                result = api.observation()
                self.assertEqual((result["state"], result["state_reason"]), (api.issue["state"], expected))
        api.timeline["nodes"] = [api.closed("SOMETHING_NEW")]
        self.assertEqual(api.observation()["events"][0]["kind"], "closed")

    def test_pull_request_at_issue_url_refuses(self):
        api = IssueApi()
        api.issue["pull_request"] = {"url": "https://api.github.com/repos/example/project/pulls/9"}
        with self.assertRaisesRegex(Unavailable, "issue URL names a pull request"):
            api.observation()
        self.assertEqual(len(api.calls), 1)

    def test_change_between_the_two_reads_refuses(self):
        for field, changed in (("updated_at", "2026-09-10T02:00:00Z"), ("state", "closed"), ("state_reason", "completed"),
                               ("labels", [{"name": "bug"}])):
            api = IssueApi()
            count: list[bool] = []
            def detail(api=api, count=count, field=field, changed=changed):
                count.append(True)
                return api.issue if len(count) == 1 else {**api.issue, field: changed}
            api.rows[ROOT + "/issues/9"] = detail
            with self.subTest(field=field), self.assertRaisesRegex(Unavailable, "changed while"):
                api.observation()

    def test_unknown_state_null_actor_and_missing_labels_refuse(self):
        api = IssueApi()
        api.issue["state"] = "merged"
        with self.assertRaisesRegex(Unavailable, "issue state is incomplete"):
            api.observation()
        api = IssueApi()
        api.timeline["nodes"] = [{"__typename": "ReopenedEvent", "id": "reopened:1", "actor": None, "createdAt": AT}]
        with self.assertRaisesRegex(Unavailable, "actor identity"):
            api.observation()
        api = IssueApi()
        api.issue["labels"] = None
        with self.assertRaisesRegex(Unavailable, "labels are unavailable"):
            api.observation()

    def test_issue_timeline_is_rooted_at_the_issue_and_pages_like_the_pull_one(self):
        api = IssueApi()
        pages = []
        def graphql():
            pages.append(True)
            nodes = [{"__typename": "ReopenedEvent", "id": f"reopened:{len(pages)}",
                      "actor": {"databaseId": 2, "login": "owner"}, "createdAt": AT}]
            info = {"hasNextPage": len(pages) == 1, "endCursor": "next" if len(pages) == 1 else None}
            return {"data": {"repository": {"issue": {"timelineItems": {"nodes": nodes, "pageInfo": info}}}}}
        api.rows["graphql"] = graphql
        result = api.observation()
        self.assertEqual([row["id"] for row in result["events"]], ["reopened:1", "reopened:2"])
        queries = [fields for path, fields, _ in api.calls if path == "graphql"]
        self.assertEqual(len(queries), 2)
        self.assertIn("issue(number:$number)", queries[0]["query"])
        self.assertIn("ClosedEvent { id createdAt actor", queries[0]["query"])
        self.assertIn("stateReason", queries[0]["query"])
        self.assertEqual(len(api.calls), 5)
        api = Api()
        api.client().timeline("example", "project", 7)
        self.assertIn("pullRequest(number:$number)", api.calls[0][1]["query"])
        self.assertNotIn("stateReason", api.calls[0][1]["query"])


class IssueDependencies(unittest.TestCase):
    def setUp(self):
        self.api = IssueApi()
        self.dep = {"kind": "issue", "url": ISSUE_URL}

    def references(self, *sources, more=False):
        nodes = [{"__typename": "CrossReferencedEvent", "source": source} for source in sources]
        self.api.rows["graphql"] = {"data": {"repository": {"issue": {"timelineItems": {
            "nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": "next" if more else None}}}}}}

    def test_closed_as_completed_is_satisfied_from_one_read(self):
        self.api.issue.update(state="closed", state_reason="completed")
        result = dependency(self.api.client(), self.dep)
        self.assertEqual(result["state"], "satisfied")
        self.assertEqual(result["dependency"], self.dep)
        self.assertEqual(result["proof"], {"url": ISSUE_URL, "closed": True, "state_reason": "completed"})
        self.assertEqual(len(self.api.calls), 1)

    def test_open_issue_with_a_merged_referencing_pull_is_satisfied(self):
        self.references({}, {"url": "https://github.com/example/project/pull/3", "merged": False},
                        {"url": "https://github.com/Example/Project/pull/7", "merged": True})
        result = dependency(self.api.client(), self.dep)
        self.assertEqual(result["state"], "satisfied")
        self.assertEqual(result["proof"], {"url": ISSUE_URL, "closed": False, "state_reason": None, "merged_pull": URL})
        self.assertEqual(len(self.api.calls), 2)

    def test_open_and_closed_not_planned_without_a_merged_pull_stay_pending(self):
        self.references({}, {"url": "https://github.com/example/project/pull/3", "merged": False})
        result = dependency(self.api.client(), self.dep)
        self.assertEqual((result["state"], result["proof"]), ("pending", {"url": ISSUE_URL, "closed": False, "state_reason": None}))
        self.api.issue.update(state="closed", state_reason="not_planned")
        result = dependency(self.api.client(), self.dep)
        self.assertEqual((result["state"], result["proof"]),
                         ("pending", {"url": ISSUE_URL, "closed": True, "state_reason": "not_planned"}))
        self.assertNotIn("reason", result)

    def test_a_malformed_reference_node_is_unknown_not_pending(self):
        # sd:1219 (ec1942399b93): a node that is not an event, or an event whose
        # source is not an object, was filtered out, so the page read as "no
        # merged reference" and the dependency as pending.
        for nodes in (["not an event"], [{"__typename": "CrossReferencedEvent", "source": "not an object"}]):
            with self.subTest(nodes=nodes):
                self.api.rows["graphql"] = {"data": {"repository": {"issue": {"timelineItems": {
                    "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}}
                result = dependency(self.api.client(), self.dep)
                self.assertEqual((result["state"], result["reason"]), ("unknown", "incomplete GitHub timeline"))

    def test_unavailable_malformed_truncated_and_misnamed_are_unknown_with_a_reason(self):
        self.api.rows[ROOT + "/issues/9"] = Response(503, {}, "")
        result = dependency(self.api.client(), self.dep)
        self.assertEqual((result["state"], result["proof"]), ("unknown", {}))
        self.assertIn("HTTP 503", result["reason"])
        self.api = IssueApi()
        self.api.rows["graphql"] = {"data": {"repository": {"issue": None}}}
        self.assertEqual(dependency(self.api.client(), self.dep)["reason"], "incomplete GitHub timeline")
        self.references({"url": "https://github.com/example/project/pull/3", "merged": False}, more=True)
        self.assertEqual(dependency(self.api.client(), self.dep)["reason"], "issue cross-references exceed one page")
        self.references({"url": "not a pull", "merged": True})
        self.assertEqual(dependency(self.api.client(), self.dep)["reason"], "invalid GitHub pull URL")
        self.api.issue["pull_request"] = {}
        self.assertEqual(dependency(self.api.client(), self.dep)["reason"], "issue URL names a pull request")
        for bad in ({"kind": "issue", "url": URL}, {"kind": "issue"}):
            with self.subTest(bad=bad):
                self.assertEqual(dependency(self.api.client(), bad)["reason"], "invalid GitHub issue URL")

    def test_other_kinds_are_still_refused(self):
        self.api.detail.update(merged=True, merge_commit_sha=MERGED)
        result = dependency(self.api.client(), {"kind": "closed", "contains_pull": URL})
        self.assertEqual((result["state"], result["reason"]), ("unknown", "unsupported remote dependency kind"))
