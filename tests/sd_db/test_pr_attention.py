"""Pull requests that need the operator, filed as report alerts after a shadow sync (sd:6712)."""

import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sd_db import connect, initialise, pr_attention
from sd_db.shadow_sync import _sync
from sd_db.writes import add_note, resolve_note

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)


def pull(number, *, decision=None, mergeable="MERGEABLE", rollup="SUCCESS", repo="example/widget", draft=False,
         created="2026-10-29T14:00:00Z", reviews=0):
    return {"url": f"https://github.com/{repo}/pull/{number}", "number": number, "title": f"Pull {number}",
            "repository": {"nameWithOwner": repo}, "reviewDecision": decision, "mergeable": mergeable,
            "isDraft": draft, "createdAt": created, "reviews": {"totalCount": reviews},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": rollup} if rollup else None}}]}}


RED_REQUIRED = [{"conclusion": "FAILURE", "isRequired": True}]


class Gh:
    """`gh`: the two attention searches answer `author` and `requested`; every other search answers nothing.

    The checks query (no `q=`) answers each pull's head contexts from `contexts`, by number; a pull not named
    there has one red required check, so a red rollup still lights `checks_failing`. A None leaves the pull out of
    the answer; a dict is the whole `contexts` block. `checks_fail` fails the call with no data; `checks_code`
    exits with that code beside the data, as gh does for an answer that carries GraphQL errors.
    """

    def __init__(self, author=(), requested=(), fail=None, count=None, contexts=None, checks_fail=None,
                 checks_code=0, mentioned=(), reviewed=()):
        self.author, self.requested, self.fail, self.count = list(author), list(requested), fail, count
        self.mentioned, self.reviewed = list(mentioned), set(reviewed)
        self.contexts, self.checks_fail, self.checks_code, self.checks_asked = contexts or {}, checks_fail, \
            checks_code, 0

    def __call__(self, argv):
        if argv[:2] == ["gh", "auth"]:
            return 0, "", ""
        query = next((value[2:] for value in argv if value.startswith("q=")), None)
        if query is None:
            return self.checks(next(value[6:] for value in argv if value.startswith("query=")))
        nodes = self.mentioned if "mentions:@me" in query else \
            self.author if "author:@me" in query and "is:pr is:open" in query else \
            self.requested if "user-review-requested:@me" in query else []
        if self.fail and nodes is self.requested:
            return 1, "", self.fail
        if "-reviewed-by:@me" in query:  # GitHub leaves out the pulls the operator reviewed
            nodes = [node for node in nodes if node["number"] not in self.reviewed]
        block = {"issueCount": self.count or len(nodes), "pageInfo": {"hasNextPage": False, "endCursor": None},
                 "nodes": nodes}
        return 0, json.dumps({"data": {"search": block}}), ""

    def checks(self, query):
        self.checks_asked += 1
        if self.checks_fail:
            return 1, "", self.checks_fail
        data = {}
        for alias, number in re.findall(r"(p\d+): repository\([^)]*\) \{ pullRequest\(number: (\d+)\)", query):
            nodes = self.contexts.get(int(number), RED_REQUIRED)
            if nodes is None:
                continue
            block = nodes if isinstance(nodes, dict) else {"pageInfo": {"hasNextPage": False}, "nodes": nodes}
            rollup = {"contexts": block}
            data[alias] = {"pullRequest": {"commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]}}}
        return self.checks_code, json.dumps({"data": data}), "GraphQL: Could not resolve" if self.checks_code else ""


class Alerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        initialise(Path(self.tmp.name) / "sd.db")
        self.db = connect(Path(self.tmp.name) / "sd.db")
        self.addCleanup(self.db.close)

    def run_(self, gh, now=NOW):
        found, complete, reason = pr_attention.read(gh, now=now)
        return pr_attention.alert(self.db, found, complete, now=now), reason

    def reports(self):
        rows = self.db.execute("SELECT title, status, fields FROM item WHERE kind = 'report' ORDER BY id").fetchall()
        return [(row["title"], row["status"], json.loads(row["fields"])["report"]["attention_basis"]) for row in rows]

    def test_a_review_request_alerts_once_and_its_clearing_acknowledges_it(self):
        url = "https://github.com/example/widget/pull/7"
        done, _ = self.run_(Gh(requested=[pull(7)]))
        self.assertEqual(done["filed"], [f"review_requested {url}"])
        self.assertEqual(self.reports(), [("pr-attention: example/widget#7: review requested of you", "planning",
                                           f"review_requested {url}")])
        done, _ = self.run_(Gh(requested=[pull(7)]))
        self.assertEqual((done["filed"], done["cleared"]), ([], []))
        self.assertEqual(len(self.reports()), 1)
        done, _ = self.run_(Gh())
        self.assertEqual(done["cleared"], [f"review_requested {url}"])
        self.assertEqual(self.reports()[0][1], "done")

    def test_a_cleared_state_keeps_an_alert_with_an_operator_followup_open(self):
        url = "https://github.com/example/widget/pull/7"
        self.run_(Gh(author=[pull(7, rollup="FAILURE")]))
        item = self.db.execute("SELECT id FROM item WHERE kind = 'report'").fetchone()["id"]
        mine = add_note(self.db, item, "followup", "find why checks flake", session="operator")
        done, _ = self.run_(Gh())
        self.assertEqual(done["cleared"], [], "checks turning green is not the operator's work done")
        self.assertEqual(self.reports()[0][1], "planning")
        open_ = {row["id"]: row["session"] for row in self.db.execute(
            "SELECT id, session FROM note WHERE item = ? AND kind = 'followup' AND resolved_at IS NULL", (item,))}
        self.assertEqual(open_, {mine: "operator"}, "the alert's own followup closes; the operator's stays open")
        resolve_note(self.db, mine)
        done, _ = self.run_(Gh())
        self.assertEqual(done["cleared"], [f"checks_failing {url}"])
        self.assertEqual(self.reports()[0][1], "done")

    def test_a_scheduled_agents_cron_followup_survives_a_cleared_state(self):
        """Session `cron` alone is no proof ingest wrote a followup: its text must match too, as reporting.py rules."""
        url = "https://github.com/example/widget/pull/7"
        self.run_(Gh(author=[pull(7, rollup="FAILURE")]))
        item = self.db.execute("SELECT id FROM item WHERE kind = 'report'").fetchone()["id"]
        agents = add_note(self.db, item, "followup", "rerun the flaky check by hand", session="cron")
        done, _ = self.run_(Gh())
        self.assertEqual(done["cleared"], [])
        self.assertEqual(self.reports()[0][1], "planning")
        open_ = [row["id"] for row in self.db.execute(
            "SELECT id FROM note WHERE item = ? AND kind = 'followup' AND resolved_at IS NULL", (item,))]
        self.assertEqual(open_, [agents], "the alert's own followup closes; the scheduled agent's stays open")
        resolve_note(self.db, agents)
        done, _ = self.run_(Gh())
        self.assertEqual(done["cleared"], [f"checks_failing {url}"])

    def test_an_acknowledged_state_that_persists_files_nothing_new(self):
        self.run_(Gh(author=[pull(3, rollup="FAILURE")]))
        self.db.execute("UPDATE item SET status = 'done' WHERE kind = 'report'")
        done, _ = self.run_(Gh(author=[pull(3, rollup="FAILURE")]))
        self.assertEqual(done["filed"], [])
        done, _ = self.run_(Gh())
        done, _ = self.run_(Gh(author=[pull(3, rollup="FAILURE")]))
        self.assertEqual(len(done["filed"]), 1, "a state that cleared and came back is new")

    def test_your_pulls_light_by_review_checks_and_mergeability(self):
        cases = {1: pull(1, decision="CHANGES_REQUESTED"), 2: pull(2, rollup="ERROR"), 3: pull(3, mergeable="CONFLICTING"),
                 4: pull(4, decision="APPROVED"), 5: pull(5, decision="APPROVED", rollup=None),
                 6: pull(6, decision="APPROVED", rollup="PENDING"), 7: pull(7, decision="REVIEW_REQUIRED"),
                 8: pull(8, decision="APPROVED", mergeable="UNKNOWN")}
        self.assertEqual({n: pr_attention.states(node, "author") for n, node in cases.items()}, {
            1: ["changes_requested"], 2: ["checks_failing"], 3: ["conflict"], 4: ["ready"], 5: ["ready"],
            6: [], 7: [], 8: []})

    def test_a_red_check_that_is_not_required_raises_no_checks_failing(self):
        """Only a red required check blocks the merge (sd:7862): an optional red one, as Snyk, is no alert."""
        optional = [{"conclusion": "FAILURE", "isRequired": False}, {"state": "SUCCESS", "isRequired": True}]
        done, reason = self.run_(Gh(author=[pull(7, rollup="FAILURE")], contexts={7: optional}))
        self.assertEqual((done["filed"], reason), ([], ""))
        waiting = [{"conclusion": "FAILURE", "isRequired": False}, {"conclusion": None, "isRequired": True}]
        done, _ = self.run_(Gh(author=[pull(7, rollup="FAILURE")], contexts={7: waiting}))
        self.assertEqual(done["filed"], [], "a required check still running is not failing")

    def test_a_red_rollup_is_never_ready_to_merge(self):
        """`isRequired` covers only checks that reported: green ones prove no required check is missing (sd:7862)."""
        optional = {"conclusion": "FAILURE", "isRequired": False}
        for contexts in ([optional], [optional, {"state": "SUCCESS", "isRequired": True}]):
            with self.subTest(contexts=contexts):
                found, complete, reason = pr_attention.read(
                    Gh(author=[pull(7, decision="APPROVED", rollup="FAILURE")], contexts={7: contexts}), now=NOW)
                self.assertEqual((found, complete, reason), ({}, True, ""))

    def test_a_red_required_check_still_raises_checks_failing(self):
        url = "https://github.com/example/widget/pull/7"
        for red in ({"state": "ERROR", "isRequired": True}, {"conclusion": "TIMED_OUT", "isRequired": True}):
            with self.subTest(red=red):
                gh = Gh(author=[pull(7, rollup="FAILURE")],
                        contexts={7: [{"conclusion": "FAILURE", "isRequired": False}, red]})
                found, complete, reason = pr_attention.read(gh, now=NOW)
                self.assertEqual((sorted(found), complete, reason), ([f"checks_failing {url}"], True, ""))

    def test_an_alert_for_an_optional_red_check_clears(self):
        """The Snyk alert filed before this fix clears on the next complete read."""
        url = "https://github.com/example/widget/pull/7"
        self.run_(Gh(author=[pull(7, rollup="FAILURE")]))
        optional = [{"conclusion": "FAILURE", "isRequired": False}]
        done, _ = self.run_(Gh(author=[pull(7, rollup="FAILURE")], contexts={7: optional}))
        self.assertEqual(done["cleared"], [f"checks_failing {url}"])

    def test_an_unread_verdict_falls_back_to_the_whole_rollup_for_that_pull_alone(self):
        """A checks answer that cannot say lights `checks_failing` as before sd:7862, and keeps the read complete.

        One large or malformed pull must not cut every run's read: then no alert would ever clear.
        """
        seven, eight = (f"https://github.com/example/widget/pull/{n}" for n in (7, 8))
        optional = [{"conclusion": "FAILURE", "isRequired": False}]
        bad = [{"conclusion": "FAILURE"}, {"conclusion": "FAILURE", "isRequired": "false"}, {"isRequired": True},
               {"conclusion": ["FAILURE"], "isRequired": True}, {"state": {"x": 1}, "isRequired": True},
               {"conclusion": 1, "isRequired": False}, "x", None, {}]
        unread = [None, {"pageInfo": {"hasNextPage": True}, "nodes": optional}, {"nodes": optional},
                  {"pageInfo": {"hasNextPage": False}, "nodes": {}}, *([node] for node in bad)]
        for contexts in unread:
            for code in (0, 1):
                with self.subTest(contexts=contexts, code=code):
                    found, complete, reason = pr_attention.read(Gh(
                        author=[pull(7, decision="APPROVED", rollup="FAILURE"), pull(8, rollup="FAILURE")],
                        contexts={7: contexts, 8: optional}, checks_code=code), now=NOW)
                    self.assertEqual((sorted(found), complete, reason), ([f"checks_failing {seven}"], True, ""),
                                     "pull 8's verdict still holds")
        found, complete, reason = pr_attention.read(Gh(author=[pull(7, rollup="FAILURE"), pull(8, rollup="FAILURE")],
                                                       contexts={8: optional}, checks_fail="HTTP 502"), now=NOW)
        self.assertEqual((sorted(found), complete, reason),
                         ([f"checks_failing {seven}", f"checks_failing {eight}"], True, ""))

    def test_an_answer_short_of_its_head_checks_says_nothing(self):
        """A pull GitHub nulled (an alias error), or a head that moved to one with no checks, has no verdict."""
        def head(rollup):
            return {"pullRequest": {"commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]}}}
        shorts = [None, "x", [], {}, {"pullRequest": None}, {"pullRequest": {"commits": {"nodes": []}}},
                  {"pullRequest": {"commits": {"nodes": [None]}}}, head(None), head({}), head({"contexts": None})]
        for answer in shorts:
            with self.subTest(answer=answer):
                self.assertIsNone(pr_attention._verdict(answer))
        optional = {"pageInfo": {"hasNextPage": False}, "nodes": [{"conclusion": "FAILURE", "isRequired": False}]}
        self.assertIs(pr_attention._verdict(head({"contexts": optional})), False)

    def test_an_unread_verdict_clears_other_alerts(self):
        url = "https://github.com/example/widget/pull/3"
        self.run_(Gh(requested=[pull(3)]))
        huge = {"pageInfo": {"hasNextPage": True}, "nodes": []}
        done, reason = self.run_(Gh(author=[pull(7, rollup="FAILURE")], contexts={7: huge}))
        self.assertEqual((done["cleared"], reason), ([f"review_requested {url}"], ""))

    def test_a_pull_with_no_red_rollup_asks_no_checks_query(self):
        gh = Gh(author=[pull(1), pull(2, rollup="PENDING"), pull(3, rollup=None)], requested=[pull(4, rollup="FAILURE")])
        pr_attention.read(gh, now=NOW)
        self.assertEqual(gh.checks_asked, 0, "the extra call is made only for an authored pull with a red rollup")

    def test_a_failed_or_cut_read_files_what_it_read_and_clears_nothing(self):
        self.run_(Gh(requested=[pull(7)]))
        done, reason = self.run_(Gh(author=[pull(4, decision="APPROVED")], fail="HTTP 502"))
        self.assertEqual((len(done["filed"]), done["cleared"]), (1, []))
        self.assertEqual(reason, "requested: HTTP 502")
        done, reason = self.run_(Gh(author=[pull(4, decision="APPROVED")], count=150))
        self.assertEqual((done["filed"], done["cleared"]), ([], []))
        self.assertIn("more than 100 open pulls", reason)
        self.assertEqual([status for _, status, _ in self.reports()], ["planning", "planning"])

    def test_a_malformed_search_is_cut_and_keeps_every_alert(self):
        """A search block or node of the wrong shape is no read: it neither raises nor clears an open alert."""
        self.run_(Gh(requested=[pull(7)]))
        bad_nodes = [{**pull(4), "repository": "example/widget"}, {**pull(4), "commits": []},
                     {**pull(4), "commits": {"nodes": [None, "x"]}}, {**pull(4), "url": 4}, {"number": 4}, "x"]
        blocks = [{}, {"issueCount": 0}, {"nodes": []}, {"issueCount": "1", "nodes": []},
                  {"issueCount": True, "nodes": []}, {"issueCount": 0, "nodes": {}},
                  *({"issueCount": 1, "nodes": [node]} for node in bad_nodes)]
        for block in blocks:
            with self.subTest(block=block):
                def gh(argv, block=block):
                    return 0, json.dumps({"data": {"search": block}}), ""
                done, reason = self.run_(gh)
                self.assertEqual(done["cleared"], [])
                self.assertIn("malformed", reason)
        self.assertEqual([status for _, status, _ in self.reports()], ["planning"])

    def test_a_node_missing_a_queried_field_is_no_evidence_a_state_cleared(self):
        """Absence is not null: a node short of a field the query asks for clears no alert (sd:6712)."""
        self.run_(Gh(author=[pull(7, rollup="FAILURE", mergeable="CONFLICTING")]))
        url = "https://github.com/example/widget/pull/7"
        bare = pull(7)
        shorts = [{"url": url}, *({k: v for k, v in bare.items() if k != key} for key in bare if key != "url"),
                  {**bare, "repository": {}}, {**bare, "commits": {}}, {**bare, "commits": {"nodes": []}},
                  {**bare, "commits": {"nodes": [None]}}, {**bare, "commits": {"nodes": [{}]}},
                  {**bare, "commits": {"nodes": [{"commit": {}}]}},
                  {**bare, "commits": {"nodes": [{"commit": {"statusCheckRollup": {}}}]}},
                  {**bare, "repository": None}, {**bare, "mergeable": None}, {**bare, "commits": None}]
        for node in shorts:
            with self.subTest(node=node):
                done, reason = self.run_(Gh(author=[node]))
                self.assertEqual(done["cleared"], [])
                self.assertIn("malformed", reason)
        self.assertEqual([status for _, status, _ in self.reports()], ["planning", "planning"])
        done, reason = self.run_(Gh(author=[pull(7, rollup=None)]))
        self.assertEqual((sorted(done["cleared"]), reason), ([f"checks_failing {url}", f"conflict {url}"], ""),
                         "a present commit with no rollup is a read: its checks cleared")

    def test_anothers_pull_that_mentions_you_alerts_once_and_a_review_request_wins(self):
        # sd:8142 (operator 2026-11-08): another's open pull naming you by @mention is an alert; one per pull, so a
        # pull that also asks your review files only that.
        other = "https://github.com/example/other/pull/9"
        both = "https://github.com/example/other/pull/10"
        done, reason = self.run_(Gh(mentioned=[pull(9, repo="example/other"), pull(10, repo="example/other")],
                                    requested=[pull(10, repo="example/other")]))
        self.assertEqual((sorted(done["filed"]), reason), (sorted([f"mentioned {other}", f"review_requested {both}"]), ""))
        self.assertIn(("pr-attention: example/other#9: you are mentioned", "planning", f"mentioned {other}"),
                      self.reports())
        done, _ = self.run_(Gh(requested=[pull(10, repo="example/other")]))
        self.assertEqual(done["cleared"], [f"mentioned {other}"])

    def test_a_mention_you_answered_with_a_review_clears(self):
        # sd:8142 class pass (#49159): `mentions:@me` holds for a pull's life, so a review must end the alert.
        url = "https://github.com/example/other/pull/9"
        done, _ = self.run_(Gh(mentioned=[pull(9, repo="example/other")]))
        self.assertEqual(done["filed"], [f"mentioned {url}"])
        done, reason = self.run_(Gh(mentioned=[pull(9, repo="example/other")], reviewed=[9]))
        self.assertEqual((done["cleared"], reason), ([f"mentioned {url}"], ""))

    def test_your_pull_with_no_review_after_24_hours_waits_on_reviewers(self):
        # sd:8142: your open, non-draft pull with no review 24 h after it opened is one alert; a review clears it.
        url = "https://github.com/example/widget/pull/5"
        day = "2026-10-28T15:00:00Z"
        quiet = [pull(5, created="2026-10-28T15:00:01Z"), pull(6, created=day, draft=True), pull(7, created=day, reviews=1)]
        done, _ = self.run_(Gh(author=quiet))
        self.assertEqual(done["filed"], [], "under 24 h, a draft, or a reviewed pull waits on no one yet")
        done, _ = self.run_(Gh(author=[pull(5, created=day)]))
        self.assertEqual(done["filed"], [f"waiting_on_reviewers {url}"])
        self.assertEqual(self.reports()[-1][0], "pr-attention: example/widget#5: waiting on reviewers, no review in 24 h")
        done, _ = self.run_(Gh(author=[pull(5, created=day, reviews=1)]))
        self.assertEqual(done["cleared"], [f"waiting_on_reviewers {url}"])

    def test_the_sync_reports_its_alerts_and_an_alert_failure_never_fails_it(self):
        result = _sync(self.db, now=NOW, runner=Gh(requested=[pull(7)]), alerts=True)
        self.assertTrue(result.ok, result.reason)
        self.assertIn("shadow sync: pull request alerts: 1 filed, 0 cleared, 1 state(s) open", result.report())
        quiet = _sync(self.db, now=NOW, runner=Gh(requested=[pull(7)]))
        self.assertEqual(quiet.attention, [], "a run that does not ask files nothing")
        self.db.execute("CREATE TRIGGER refuse BEFORE INSERT ON state WHEN NEW.key = 'pr-attention:seen' "
                        "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        result = _sync(self.db, now=NOW, runner=Gh(requested=[pull(8)]), alerts=True)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.attention, ["pull request alerts not filed: disk full"])


if __name__ == "__main__":
    unittest.main()
