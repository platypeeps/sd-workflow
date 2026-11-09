"""Issues that need the operator, filed as report alerts after a shadow sync (sd:8142)."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sd_db import connect, initialise, issue_attention
from sd_db.shadow_sync import _sync

NOW = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
ME = "operator-login"
ACCOUNT = "account-me"
BASE = "https://jira.example.test"
ENV = {"JIRA_BASE_URL": BASE, "JIRA_EMAIL": "me@example.test", "JIRA_API_TOKEN": "token"}


def issue(number, *, body="", author="someone", comments=()):
    return {"url": f"https://github.com/example/widget/issues/{number}", "number": number, "title": f"Issue {number}",
            "body": body, "author": {"login": author}, "repository": {"nameWithOwner": "example/widget"},
            "comments": {"nodes": [{"body": text, "author": {"login": who}} for who, text in comments]}}


def gh(*nodes, login=ME, count=None, code=0, out=None):
    def run(argv):
        if out is not None or code:
            return code, out or "", "gh broke"
        return 0, json.dumps({"data": {"viewer": {"login": login}, "search": {
            "issueCount": len(nodes) if count is None else count, "nodes": list(nodes)}}}), ""
    return run


def mention(account):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "see "}, {"type": "mention", "attrs": {"id": account}}]}]}


def plain(text="plain"):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


#: When a fixture comment was written, unless it names its own: inside the window.
TODAY = "2026-11-08T10:00:00.000+0000"


def jira(issues, *, search_error=None, comment_error=None):
    """Jira's three calls. `issues` is `{key: [(account, body[, created]), ...]}`, comments newest first."""
    asked = []

    def transport(url, headers, body):
        asked.append(url)
        if url.endswith("/myself"):
            return {"accountId": ACCOUNT}, ""
        if url.endswith("/search/jql"):
            if search_error:
                return None, search_error
            return {"isLast": True, "issues": [
                {"key": key, "fields": {"summary": f"Ticket {key}", "project": {"key": "OPS"}}} for key in issues]}, ""
        if comment_error:
            return None, comment_error
        key = url.split("/issue/")[1].split("/")[0]
        return {"comments": [{"author": {"accountId": who}, "body": text, "created": (created or [TODAY])[0]}
                             for who, text, *created in issues[key]]}, ""
    transport.asked = asked
    return transport


class Base(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        initialise(Path(directory.name) / "sd.db")
        self.connection = connect(Path(directory.name) / "sd.db")
        self.addCleanup(self.connection.close)

    def shadow(self, url, *, tracker="github", repo="example/widget", number=7, kind="issue", state="open",
               reason="assigned"):
        with self.connection:
            self.connection.execute(
                "INSERT INTO shadow (tracker, url, repo, number, kind, title, state, author, updated_at, first_seen, "
                "last_seen, reason) VALUES (?, ?, ?, ?, ?, 'A title', ?, 'someone', '2026-11-08', '2026-11-08', "
                "'2026-11-08', ?)", (tracker, url, repo, number, kind, state, reason))

    def run_once(self, *nodes, now=NOW, transport=None, environ=None, runner=None):
        """Both trackers' runs, GitHub then Jira, as one scheduled sync; a state is unread only by the run that reads it."""
        found_g, unread_g, reason_g = issue_attention.read_github(self.connection, runner or gh(*nodes))
        done_g = issue_attention.alert(self.connection, found_g, unread_g, now=now)
        found_j, unread_j, reason_j = issue_attention.read_jira(
            self.connection, transport=transport or jira({}), environ=ENV if environ is None else environ, now=now)
        done_j = issue_attention.alert(self.connection, found_j, unread_j, now=now)
        done = {key: done_g[key] + done_j[key] for key in done_g}
        return done, {**found_g, **found_j}, (unread_g - {"jira_mentioned"}) | (unread_j - {"mentioned"}), reason_g or reason_j

    def alerts(self):
        return [json.loads(row["fields"])["report"]["attention_basis"] for row in self.connection.execute(
            "SELECT fields FROM item WHERE kind = 'report' AND status != 'done' ORDER BY id")]


GH_URL = "https://github.com/example/widget/issues/"


class Assigned(Base):
    def test_a_later_assignment_files_one_alert_and_the_first_read_files_none(self):
        self.shadow(GH_URL + "1")
        self.assertEqual(self.run_once()[0]["filed"], [], "an assignment older than the first read is not new")
        self.shadow(GH_URL + "2", number=2)
        self.shadow(f"{BASE}/browse/OPS-3", tracker="jira", repo="OPS", number=None)
        self.assertEqual(self.run_once(now=LATER)[0]["filed"],
                         [f"assigned {GH_URL}2", f"assigned {BASE}/browse/OPS-3"])
        self.assertEqual(self.run_once(now=LATER + timedelta(hours=1))[0]["filed"], [], "one alert per issue and state")

    def test_a_watched_filed_closed_or_pull_row_is_not_assigned(self):
        self.run_once()
        self.shadow(GH_URL + "4", reason="watched", number=4)
        self.shadow(GH_URL + "5", reason="filed", number=5)
        self.shadow(GH_URL + "6", state="closed", number=6)
        self.shadow("https://github.com/example/widget/pull/8", kind="pr", number=8)
        self.assertEqual(self.run_once(now=LATER)[1], {})

    def test_a_closed_issue_clears_its_alert(self):
        self.run_once()
        self.shadow(GH_URL + "1", number=1)
        self.run_once(now=LATER)
        self.assertEqual(self.alerts(), [f"assigned {GH_URL}1"])
        with self.connection:
            self.connection.execute("UPDATE shadow SET state = 'closed'")
        done = self.run_once(now=LATER + timedelta(hours=1))[0]
        self.assertEqual(done["cleared"], [f"assigned {GH_URL}1"])
        self.assertEqual(self.alerts(), [])


class GithubMentions(Base):
    def test_a_mention_in_a_comment_files_an_alert(self):
        found = self.run_once(issue(1, comments=[("other", f"cc @{ME} please")]))[1]
        self.assertEqual(list(found), [f"mentioned {GH_URL}1"])

    def test_a_mention_in_the_body_counts_until_a_reply(self):
        node = issue(1, body=f"@{ME} look")
        self.assertEqual(list(self.run_once(node)[1]), [f"mentioned {GH_URL}1"])
        replied = issue(1, body=f"@{ME} look", comments=[(ME, "done")])
        self.assertEqual(self.run_once(replied, now=LATER)[1], {})

    def test_a_reply_clears_and_a_later_mention_counts_again(self):
        answered = issue(1, comments=[("other", f"@{ME} ?"), (ME.upper(), "yes")])
        self.assertEqual(self.run_once(answered)[1], {}, "GitHub logins match in any case")
        asked = issue(1, comments=[("other", f"@{ME} ?"), (ME, "yes"), ("other", f"@{ME.upper()} again")])
        self.assertEqual(list(self.run_once(asked, now=LATER)[1]), [f"mentioned {GH_URL}1"])

    def test_a_team_or_longer_login_is_not_a_mention_of_the_operator(self):
        for text in (f"@{ME}/team", f"@{ME}-bot", f"x@{ME}", "plain"):
            with self.subTest(text):
                self.assertEqual(self.run_once(issue(1, comments=[("other", text)]))[1], {})

    def test_a_failed_search_clears_no_open_mention(self):
        self.run_once(issue(1, comments=[("other", f"@{ME}")]))
        for runner in (gh(code=1), gh(out="not json"), gh(issue(2), count=9), gh({"url": 3})):
            with self.subTest():
                done, _, unread, reason = self.run_once(now=LATER, runner=runner)
                self.assertEqual(unread, {"mentioned"})
                self.assertTrue(reason)
                self.assertEqual(done["cleared"], [])
        self.assertEqual(self.alerts(), [f"mentioned {GH_URL}1"])
        self.assertEqual(self.run_once(issue(1, comments=[("other", f"@{ME}")]), now=LATER)[0]["filed"], [],
                         "a failed read keeps the seen list")


class JiraMentions(Base):
    KEY = f"{BASE}/browse/OPS-1"

    def test_a_comment_mention_since_the_last_reply_files_an_alert(self):
        found = self.run_once(transport=jira({"OPS-1": [("other", mention(ACCOUNT)), (ACCOUNT, plain())]}))[1]
        self.assertEqual(list(found), [f"jira_mentioned {self.KEY}"])
        self.assertEqual(found[f"jira_mentioned {self.KEY}"]["name"], "OPS-1")

    def test_a_reply_after_the_mention_clears_it(self):
        self.run_once(transport=jira({"OPS-1": [("other", mention(ACCOUNT))]}))
        done, found, _, _ = self.run_once(now=LATER, transport=jira({"OPS-1": [(ACCOUNT, plain()),
                                                                               ("other", mention(ACCOUNT))]}))
        self.assertEqual(found, {})
        self.assertEqual(done["cleared"], [f"jira_mentioned {self.KEY}"])

    def test_a_mention_of_someone_else_is_not_a_wait(self):
        self.assertEqual(self.run_once(transport=jira({"OPS-1": [("other", mention("account-b"))]}))[1], {})

    def test_the_comment_read_asks_newest_first(self):
        transport = jira({"OPS-1": [("other", plain())]})
        self.run_once(transport=transport)
        self.assertIn(f"{BASE}/rest/api/3/issue/OPS-1/comment?orderBy=-created&maxResults=100", transport.asked)

    def test_a_failed_read_clears_no_open_mention(self):
        self.run_once(transport=jira({"OPS-1": [("other", mention(ACCOUNT))]}))
        for transport in (jira({}, search_error="HTTP 503"), jira({"OPS-1": []}, comment_error="HTTP 500")):
            with self.subTest():
                done, _, unread, reason = self.run_once(now=LATER, transport=transport)
                self.assertEqual((unread, done["cleared"]), ({"jira_mentioned"}, []))
                self.assertTrue(reason)
        self.assertEqual(self.alerts(), [f"jira_mentioned {self.KEY}"])

    def test_unconfigured_jira_asks_nothing_says_nothing_and_clears_nothing(self):
        self.run_once(transport=jira({"OPS-1": [("other", mention(ACCOUNT))]}))
        transport = jira({})
        done, _, unread, reason = self.run_once(now=LATER, transport=transport, environ={"PATH": "/usr/bin"})
        self.assertEqual((transport.asked, unread, reason, done["cleared"]), ([], {"jira_mentioned"}, "", []))

    def test_a_mention_older_than_the_window_files_nothing(self):
        """sd:8200: six alerts for mentions from 2021-2022; none needed action."""
        old = jira({"OPS-1": [("other", mention(ACCOUNT), "2021-03-04T10:00:00.000+0000")]})
        done, found, unread, _ = self.run_once(transport=old)
        self.assertEqual((found, done["filed"], unread), ({}, [], set()))
        done = self.run_once(now=LATER, transport=jira({"OPS-1": [("other", mention(ACCOUNT))]}))[0]
        self.assertEqual(done["filed"], [f"jira_mentioned {self.KEY}"])

    def test_the_window_is_the_days_asked_for(self):
        ten_days = jira({"OPS-1": [("other", mention(ACCOUNT), "2026-10-29T10:00:00.000+0000")]})
        for days, found in ((30, [f"jira_mentioned {self.KEY}"]), (7, [])):
            with self.subTest(days=days):
                rows = issue_attention.read_jira(self.connection, transport=ten_days, environ=ENV, now=NOW,
                                                 days=days)[0]
                self.assertEqual(list(rows), found)

    def test_more_issues_than_the_read_cap_is_a_cut_read(self):
        many = {f"OPS-{n}": [("other", plain())] for n in range(issue_attention.shadow_jira.MENTION_READS + 1)}
        _, _, unread, reason = self.run_once(transport=jira(many))
        self.assertEqual(unread, {"jira_mentioned"})
        self.assertIn("more than", reason)


class Wiring(Base):
    def test_a_jira_sync_files_the_mentions_it_reads(self):
        with mock.patch.dict(os.environ, ENV):
            result = _sync(self.connection, now=NOW, tracker="jira", alerts=True,
                           runner=jira({"OPS-1": [("other", mention(ACCOUNT))]}))
        self.assertIn("shadow sync: issue alerts: 1 filed, 0 cleared, 1 state(s) open", result.report())

    def test_a_jira_sync_reads_mentions_inside_the_days_it_is_given(self):
        with mock.patch.dict(os.environ, ENV):
            result = _sync(self.connection, now=NOW, tracker="jira", alerts=True, mention_days=7,
                           runner=jira({"OPS-1": [("other", mention(ACCOUNT), "2026-10-29T10:00:00.000+0000")]}))
        self.assertIn("shadow sync: issue alerts: 0 filed, 0 cleared, 0 state(s) open", result.report())

    def test_a_jira_sync_that_does_not_ask_files_nothing(self):
        with mock.patch.dict(os.environ, ENV):
            result = _sync(self.connection, now=NOW, tracker="jira",
                           runner=jira({"OPS-1": [("other", mention(ACCOUNT))]}))
        self.assertEqual(self.alerts(), [], result.report())


if __name__ == "__main__":
    unittest.main()
