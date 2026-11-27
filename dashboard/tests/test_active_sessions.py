"""Today's Working now (sd:3466): each open item's live sessions, when each started and how long it has run.

The join is `active_sessions.document`: lane entries and Fix's launch notes from the database, live herdr agents, and
the fleet's worktrees and `sd-*` processes. Every source is a seam here; nothing runs the real herdr or `ps`.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import test_v2_queue
import test_v2_today_reader
from fleet_support import FleetCase
from sd_dashboard import active_sessions, fix_session, fleet, now_screen
from support import NOW, ScreenCase
from test_now_screen import JobsBackend, fleet_document


def ARABIC(number):
    """`number` in Arabic-Indic digits: `str.isdigit` and `int` take them, an item number does not."""
    return str(number).translate(str.maketrans("0123456789", "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"))


def lane(connection, item, *, worktree, status="running", started="2026-09-06T11:00:00Z",
         enqueued="2026-09-06T10:30:00Z", step="prepare", host="hub", entry=None):
    body = {"id": entry or f"e{item}", "item": item, "worktree": worktree, "branch": f"b{item}", "status": status,
            "enqueued_at": enqueued, "repository": "example/project"}
    if status == "running":
        body.update(started_at=started, step=step, holder={"host": host, "pid": 1, "token": "t"})
    connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                       (f"lane:v1:example/project:{body['id']}", NOW, json.dumps(body)))
    connection.commit()


def tree(path, branch, *, created="2026-09-06T09:00:00Z", live=True):
    return {"repo": "project", "name": Path(path).name, "path": path, "branch": branch, "live": live,
            "state": "live" if live else "abandoned", "created": created}


def sessions(trees=(), procs=()):
    return {"root": "/repos", "rootExists": True, "worktrees": list(trees), "processes": list(procs),
            "processes_error": "", "processes_truncated": False}


def agent(pane, cwd, status="working"):
    return {"agent": "claude", "pane_id": pane, "cwd": cwd, "foreground_cwd": cwd, "agent_status": status}


class Join(ScreenCase):

    def setUp(self):
        super().setUp()
        self.wt = os.path.realpath(self.tmp.name) + "/wt"

    def read(self, *, fleet_doc=None, fleet_error="", herdr=list):
        return active_sessions.document(self.connection, now=NOW, sessions=fleet_doc or sessions(),
                                        sessions_error=fleet_error, herdr=herdr)

    def launch(self, item, pane, when):
        body = fix_session.NOTE.format(session="builder", item=item, pane=pane, where="~/repos/project")
        note = self.note(item, body, kind="comment")
        self.connection.execute("UPDATE note SET timestamp = ? WHERE id = ?", (when, note))
        self.connection.commit()

    def test_a_running_lane_entry_ties_its_worktree_and_a_process_to_its_item(self):
        item = self.item("lane work", status="in_progress")
        lane(self.connection, item, worktree=f"{self.wt}/a")
        found = self.read(fleet_doc=sessions(
            [tree(f"{self.wt}/a", "unrelated-branch")],
            [{"pid": "77", "elapsed": "05:00", "command": f"/pack/bin/sd-ship -C {self.wt}/a prepare"}]))
        self.assertEqual({key: "" for key in ("fleet", "lane", "launch", "herdr")}, found["sources"])
        rows = [(row["item"], row["kind"], row["started"], row["elapsed"]) for row in found["rows"]]
        self.assertEqual([(item, "worktree", "2026-09-06T09:00:00Z", "3h 0m"),
                          (item, "lane", "2026-09-06T11:00:00Z", "1h 0m"),
                          (item, "process", "2026-09-06T11:55:00Z", "5m")], rows)
        self.assertTrue(all(row["title"] == "lane work" for row in found["rows"]))
        self.assertIn("lane entry", found["rows"][0]["via"])
        self.assertEqual(f"-C {self.wt}/a", found["rows"][2]["via"])
        self.assertEqual("Lane prepare on hub", found["rows"][1]["session"])

    def test_each_row_carries_its_items_repo(self):
        # pack sd:7222: In motion groups its rows by repo, read from the item already read here; none is None.
        item = self.item("repo work", status="in_progress", repo=self.repo("/repos/alpha"))
        bare = self.item("no repo work", status="in_progress")
        lane(self.connection, item, worktree=f"{self.wt}/a")
        lane(self.connection, bare, worktree=f"{self.wt}/b")
        found = self.read()
        self.assertEqual({(item, "/repos/alpha"), (bare, None)}, {(row["item"], row["repo"]) for row in found["rows"]})

    def test_a_branch_number_ties_a_worktree_and_an_item_flag_ties_a_process(self):
        branch_item = self.item("by branch")
        flag_item = self.item("by flag")
        found = self.read(fleet_doc=sessions(
            [tree(f"{self.wt}/b", f"sessions-view-{branch_item}"), tree(f"{self.wt}/c", "no-number-here")],
            [{"pid": "8", "elapsed": "1-00:00:00", "command": f"sd-ship merge --item {flag_item}"},
             {"pid": "9", "elapsed": "00:01", "command": "sd today"}]))
        # Longest-running item first: the process has run a day, the worktree three hours.
        self.assertEqual([(flag_item, "process", f"--item {flag_item}"),
                          (branch_item, "worktree", f"branch sessions-view-{branch_item}")],
                         [(row["item"], row["kind"], row["via"]) for row in found["rows"]])
        self.assertEqual("1d 0h", found["rows"][0]["elapsed"])
        self.assertEqual("sd-ship merge (pid 8)", found["rows"][0]["session"])

    def test_a_launch_note_is_a_session_while_herdr_lists_its_pane(self):
        live, ended = self.item("live fix"), self.item("ended fix")
        self.launch(live, "w9:p1", "2026-09-06T11:30:00+00:00")
        self.launch(ended, "w9:p2", "2026-09-06T10:00:00+00:00")
        found = self.read(herdr=lambda: [agent("w9:p1", "/somewhere")])
        self.assertEqual([(live, "agent", "2026-09-06T11:30:00Z", "30m", True)],
                         [(row["item"], row["kind"], row["started"], row["elapsed"], row["verified"]) for row in found["rows"]])
        self.assertIn("w9:p1", found["rows"][0]["session"])

    def test_herdr_that_did_not_answer_leaves_launches_unverified_not_ended(self):
        item = self.item("fix")
        self.launch(item, "w9:p1", "2026-09-06T11:30:00+00:00")

        def broken():
            raise active_sessions.Unread("herdr agent list gave no answer in 5 s")
        found = self.read(herdr=broken)
        self.assertEqual("herdr agent list gave no answer in 5 s", found["sources"]["herdr"])
        self.assertEqual([(item, False)], [(row["item"], row["verified"]) for row in found["rows"]])

    def test_herdr_is_not_asked_when_nothing_could_join_it(self):
        asked = []
        self.read(herdr=lambda: asked.append(1) or [])
        # A worktree whose branch names no item in this database ties nothing either.
        self.read(fleet_doc=sessions([tree(f"{self.wt}/h", "sd99999-elsewhere")]), herdr=lambda: asked.append(1) or [])
        self.assertEqual([], asked)

    def test_an_agent_in_a_joined_worktree_is_that_items_session(self):
        item = self.item("worked in a worktree")
        found = self.read(fleet_doc=sessions([tree(f"{self.wt}/d", f"sd{item}-thing")]),
                          herdr=lambda: [agent("w2:p1", f"{self.wt}/d/sub")])
        self.assertEqual([("worktree", item), ("agent", item)], [(row["kind"], row["item"]) for row in found["rows"]])
        self.assertEqual("", found["rows"][1]["started"])

    def test_a_done_item_has_no_sessions(self):
        item = self.item("finished")
        # The status alone: the pack's delivery evidence is not what this join reads.
        self.connection.execute("UPDATE item SET status = 'done' WHERE id = ?", (item,))
        self.connection.commit()
        found = self.read(fleet_doc=sessions([tree(f"{self.wt}/e", f"fix-{item}")]))
        self.assertEqual([], found["rows"])

    def test_a_clean_worktree_whose_head_landed_is_not_a_live_session(self):
        """sd:6710: an open item's clean worktree at the head a Landed note names is cleanup, not work; a dirty one
        is still live."""
        import subprocess

        def git(path, *args):
            return subprocess.run(["git", "-C", path, *args], check=True, capture_output=True, text=True,
                                  env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()

        landed, dirty = self.item("landed", status="in_progress"), self.item("dirty", status="in_progress")
        for item, name in ((landed, "m"), (dirty, "n")):
            path = f"{self.wt}/{name}"
            os.makedirs(path)
            git(path, "init", "-q")
            git(path, "commit", "-q", "--allow-empty", "-m", "x")
            self.note(item, f"Landed: merged at 0123456789ab (head {git(path, 'rev-parse', 'HEAD')[:12]}). Cleanup.",
                      kind="comment")
        Path(f"{self.wt}/n/new.txt").write_text("x")
        found = self.read(fleet_doc=sessions([tree(f"{self.wt}/m", f"sd{landed}-a"), tree(f"{self.wt}/n", f"sd{dirty}-b")]))
        self.assertEqual([(dirty, "worktree")], [(row["item"], row["kind"]) for row in found["rows"]])

    def test_a_fleet_that_could_not_be_read_is_a_reason_and_the_lane_still_answers(self):
        item = self.item("lane only")
        lane(self.connection, item, worktree=f"{self.wt}/f", status="pending")
        found = self.read(fleet_doc={}, fleet_error="fleet collection was stopped at its budget")
        self.assertEqual("fleet collection was stopped at its budget", found["sources"]["fleet"])
        self.assertEqual([(item, "lane", "Lane queue (pending)", "2026-09-06T10:30:00Z")],
                         [(row["item"], row["kind"], row["session"], row["started"]) for row in found["rows"]])

    def test_a_lane_entry_of_the_wrong_shape_marks_the_lane_unread(self):
        item = self.item("beside a bad entry")
        lane(self.connection, item, worktree=f"{self.wt}/l", status="pending")
        for body in (None, "[1]", '{"id": "x", "item": "7"}', '{"id": "x", "item": 7, "worktree": 7}',
                     '{"id": "x", "item": 7, "status": "running", "holder": 7}'):
            with self.subTest(body=body):
                self.connection.execute("INSERT INTO state(kind, key, timestamp, body) VALUES "
                                        "('checkpoint', 'lane:v1:example/project:x', ?, ?)", (NOW, body))
                self.connection.commit()
                found = self.read()
                self.assertIn("lane entry", found["sources"]["lane"])
                self.assertEqual([], found["rows"])

    def test_a_fleet_document_of_the_wrong_shape_marks_the_fleet_unread(self):
        item = self.item("fleet shape")
        lane(self.connection, item, worktree=f"{self.wt}/m", status="pending")
        good = tree(f"{self.wt}/m", f"sd{item}-m")
        for doc in (7, {"worktrees": 7, "processes": []}, sessions([7]), sessions([{**good, "path": 7}]),
                    sessions([{**good, "branch": None}]), sessions([{**good, "live": "yes"}]),
                    sessions([{**good, "created": 7}])):
            with self.subTest(doc=doc):
                found = self.read(fleet_doc=doc)
                self.assertTrue(found["sources"]["fleet"], found["sources"])
                # The lane still answers: one source's shape is not the others'.
                self.assertEqual([(item, "lane")], [(row["item"], row["kind"]) for row in found["rows"]])

    def test_a_process_row_of_the_wrong_shape_marks_the_process_table_unread_and_worktrees_still_join(self):
        item = self.item("process shape")
        good = {"pid": "5", "elapsed": "01:00", "command": f"sd-ship merge --item {item}"}
        for procs in ([7], [{**good, "pid": 5}], [{**good, "command": ["sd-ship"]}], [{**good, "elapsed": None}]):
            with self.subTest(procs=procs):
                doc = sessions([tree(f"{self.wt}/n", f"sd{item}-n")], [good, *procs])
                found = self.read(fleet_doc=doc)
                self.assertIn("the process table", found["sources"]["fleet"])
                self.assertEqual([(item, "worktree")], [(row["item"], row["kind"]) for row in found["rows"]])
        found = self.read(fleet_doc={**sessions(), "processes_error": 7})
        self.assertIn("the process table", found["sources"]["fleet"])

    def test_a_process_with_no_command_word_is_the_process_tables_reason_not_a_crash(self):
        item = self.item("empty command")
        good = {"pid": "5", "elapsed": "01:00", "command": f"sd-ship merge --item {item}"}
        for command in ("", "   ", f"sd-ship -C {self.wt}/n\x00x prepare"):
            with self.subTest(command=command):
                found = self.read(fleet_doc=sessions([tree(f"{self.wt}/n", f"sd{item}-n")],
                                                     [good, {**good, "command": command}]))
                self.assertIn("the process table", found["sources"]["fleet"])
                self.assertEqual([(item, "worktree")], [(row["item"], row["kind"]) for row in found["rows"]])

    def test_an_item_flag_of_digits_that_are_not_ascii_names_no_item(self):
        item = self.item("superscript")
        for command in ("sd-ship --item \u00b2 prepare", "sd-ship --item=\u00b2 prepare",
                        f"sd-ship --item {ARABIC(item)} prepare", f"sd-ship --item={ARABIC(item)} prepare"):
            with self.subTest(command=command):
                found = self.read(fleet_doc=sessions(procs=[{"pid": "5", "elapsed": "01:00", "command": command}]))
                self.assertEqual("", found["sources"]["fleet"])
                self.assertEqual([], found["rows"])

    def test_a_branch_number_of_digits_that_are_not_ascii_names_no_item(self):
        item = self.item("arabic branch")
        found = self.read(fleet_doc=sessions([tree(f"{self.wt}/o", f"sd{ARABIC(item)}-thing")]))
        self.assertEqual([], found["rows"])

    def test_a_worktree_path_with_a_nul_marks_the_fleet_unread_not_a_crash(self):
        item = self.item("nul path")
        lane(self.connection, item, worktree=f"{self.wt}/p", status="pending")
        found = self.read(fleet_doc=sessions([tree(f"{self.wt}/p\x00x", f"sd{item}-p")]))
        self.assertIn("worktree", found["sources"]["fleet"])
        self.assertEqual([(item, "lane")], [(row["item"], row["kind"]) for row in found["rows"]])

    def test_a_lane_worktree_with_a_nul_marks_the_lane_unread_not_a_crash(self):
        item = self.item("nul lane")
        lane(self.connection, item, worktree=f"{self.wt}/q\x00x", status="pending")
        found = self.read()
        self.assertIn("lane entry", found["sources"]["lane"])
        self.assertEqual([], found["rows"])

    def test_an_item_flag_past_nine_ascii_digits_names_no_item(self):
        self.item("one item")
        for command in (f"sd-ship --item {'9' * 4301} prepare", "sd-ship --item=9999999999999999999 prepare",
                        "sd-ship --item 1234567890 prepare"):
            with self.subTest(command=command[:40]):
                found = self.read(fleet_doc=sessions(procs=[{"pid": "5", "elapsed": "01:00", "command": command}]))
                self.assertEqual("", found["sources"]["fleet"])
                self.assertEqual([], found["rows"])

    def test_a_branch_number_past_nine_digits_names_no_item(self):
        self.item("one item")
        found = self.read(fleet_doc=sessions([tree(f"{self.wt}/s", "sd9999999999999999999-thing")]))
        self.assertEqual({key: "" for key in ("fleet", "lane", "launch", "herdr")}, found["sources"])
        self.assertEqual([], found["rows"])

    def test_a_lane_item_past_nine_digits_marks_the_lane_unread(self):
        lane(self.connection, 9999999999999999999, worktree=f"{self.wt}/t", status="pending")
        found = self.read()
        self.assertIn("lane entry", found["sources"]["lane"])
        self.assertEqual([], found["rows"])

    def test_a_source_that_raises_anything_is_unread_by_its_class_and_now_still_answers(self):
        item = self.item("guarded")
        self.launch(item, "w9:p1", "2026-09-06T11:30:00+00:00")
        fleet_doc = sessions([tree(f"{self.wt}/u", f"sd{item}-u")])
        failures = (ValueError("v"), OverflowError("o"), TypeError("t"), KeyError("k"), IndexError("i"),
                    sqlite3.OperationalError("database is locked"))

        def now(herdr=list):
            return now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name), herdr=herdr,
                                       fleet=lambda area: fleet_doc if area == "sessions" else {"repos": []})

        for source, target in (("fleet", "_fleet"), ("lane", "_lane"), ("launch", "_launches"), ("herdr", None)):
            for failure in failures:
                with self.subTest(source=source, failure=type(failure).__name__):
                    def boom(*_, failure=failure):
                        raise failure
                    if target:
                        with patch.object(active_sessions, target, boom):
                            doc = now()
                    else:
                        doc = now(herdr=boom)
                    self.assertTrue(doc["active"]["sources"][source].startswith(type(failure).__name__),
                                    doc["active"]["sources"])
                    self.assertEqual({other: "" for other in ("fleet", "lane", "launch", "herdr") if other != source},
                                     {other: reason for other, reason in doc["active"]["sources"].items() if other != source})
                    self.assertIn("rows", doc)
        with patch.object(active_sessions, "_sessions", lambda *_: [][1]):
            doc = now()
        self.assertTrue(doc["active"]["error"].startswith("the join failed: IndexError"), doc["active"])
        self.assertEqual([], doc["active"]["rows"])

    def test_now_carries_the_sessions(self):
        item = self.item("on today")
        doc = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name),
                                  fleet=lambda area: sessions([tree(f"{self.wt}/g", f"x-{item}")]) if area == "sessions"
                                  else {"repos": []})
        self.assertEqual([item], [row["item"] for row in doc["active"]["rows"]])


class WorktreeStart(FleetCase):

    def test_a_worktree_carries_when_git_registered_it(self):
        pack = self.checkout("pack")
        self.worktree(pack, "lane-a", "fix/sd-1-lane-a")
        self.abandoned(pack, "lane-b", "fix/sd-2-lane-b")
        stamp = time.mktime((2026, 9, 6, 9, 0, 0, 0, 0, -1))
        os.utime(pack / ".git" / "worktrees" / "lane-a" / "commondir", (stamp, stamp))
        rows = {row["name"]: row for row in fleet.read_worktrees(".", pack)}
        self.assertEqual(datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), rows["lane-a"]["created"])
        self.assertEqual("", rows["lane-b"]["created"])


class HerdrReader(ScreenCase):
    """`herdr_agents` against local-herdr's double on PATH, never the real binary."""

    def setUp(self):
        super().setUp()
        self.fixture = Path(self.tmp.name) / "herdr"
        self.fixture.mkdir()
        self.journal = Path(self.tmp.name) / "journal"
        doubles = fix_session.CHECKOUT / "local-herdr" / "tests" / "doubles"
        environment = patch.dict(os.environ, {"PATH": f"{doubles}{os.pathsep}/usr/bin:/bin", "HERDR_SESSION": "sd",
                                              "HERDR_DOUBLE_FIXTURE": str(self.fixture),
                                              "HERDR_DOUBLE_JOURNAL": str(self.journal)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_a_running_session_answers_its_agents_through_its_socket(self):
        (self.fixture / "session-list.json").write_text(json.dumps(
            {"sessions": [{"name": "sd", "running": True, "socket_path": "/tmp/example.sock"}]}))
        (self.fixture / "agent-list.json").write_text(json.dumps(
            {"result": {"agents": [agent("w1:p1", "/somewhere")], "type": "agent_list"}}))
        self.assertEqual(["w1:p1"], [row["pane_id"] for row in active_sessions.herdr_agents()])
        self.assertIn("sd\t/tmp/example.sock\tagent\tlist", self.journal.read_text())

    def test_a_session_that_is_not_running_has_no_agents_and_is_not_asked(self):
        self.assertEqual([], active_sessions.herdr_agents())
        self.assertNotIn("agent", self.journal.read_text())

    def test_an_answer_that_is_not_json_is_unknown_not_empty(self):
        (self.fixture / "session-list.json").write_text("not json")
        with self.assertRaisesRegex(ValueError, "herdr session list --json: exit 0, no JSON answer"):
            active_sessions.herdr_agents()


    def test_an_answer_of_the_wrong_shape_is_unknown_not_empty(self):
        running = {"name": "sd", "running": True, "socket_path": "/tmp/example.sock"}
        good = {"sessions": [running]}
        for listed, agents in (({"sessions": 7}, None), ({"sessions": [7]}, None),
                               ({"sessions": [{**running, "running": "yes"}]}, None),
                               ({"sessions": [{**running, "socket_path": 7}]}, None),
                               (good, {"result": [1]}), (good, {"result": {"agents": 7}}),
                               (good, {"result": {"agents": [7]}}), (good, {"result": {"agents": [agent(7, "/x")]}}),
                               (good, {"result": {"agents": [agent(["w1:p1"], "/x")]}}),
                               (good, {"result": {"agents": [agent("w1:p1", 7)]}})):
            with self.subTest(listed=listed, agents=agents):
                (self.fixture / "session-list.json").write_text(json.dumps(listed))
                if agents is not None:
                    (self.fixture / "agent-list.json").write_text(json.dumps(agents))
                with self.assertRaisesRegex(ValueError, "^herdr "):
                    active_sessions.herdr_agents()

    def test_a_wrong_shape_marks_herdr_unread_and_leaves_launches_unverified(self):
        item = self.item("fix")
        note = self.note(item, fix_session.NOTE.format(session="builder", item=item, pane="w9:p1", where="~/x"), kind="comment")
        self.connection.execute("UPDATE note SET timestamp = ? WHERE id = ?", ("2026-09-06T11:30:00+00:00", note))
        self.connection.commit()
        for listed in ({"sessions": 7}, {"sessions": [{"name": "sd", "running": True}]}):
            with self.subTest(listed=listed):
                (self.fixture / "session-list.json").write_text(json.dumps(listed))
                (self.fixture / "agent-list.json").write_text(json.dumps({"result": [1]}))
                found = active_sessions.document(self.connection, now=NOW, sessions=sessions())
                self.assertTrue(found["sources"]["herdr"].startswith("herdr "), found["sources"])
                self.assertEqual([(item, False)], [(row["item"], row["verified"]) for row in found["rows"]])

    def test_an_agent_cwd_with_a_nul_marks_herdr_unread_not_a_crash(self):
        item = self.item("nul cwd")
        (self.fixture / "session-list.json").write_text(json.dumps({"sessions": [{"name": "sd", "running": True}]}))
        (self.fixture / "agent-list.json").write_text(json.dumps({"result": {"agents": [agent("w1:p1", "/x\x00y")]}}))
        found = active_sessions.document(self.connection, now=NOW,
                                         sessions=sessions([tree(os.path.realpath(self.tmp.name), f"sd{item}-r")]))
        self.assertTrue(found["sources"]["herdr"].startswith("herdr "), found["sources"])
        self.assertEqual([(item, "worktree")], [(row["item"], row["kind"]) for row in found["rows"]])


class TodayDraws(ScreenCase):
    """today.js draws the live items in its Queue view (pack sd:7234), under JavaScriptCore with the Today stand-in
    (`test_v2_today_reader`)."""

    run_page = test_v2_today_reader.TheReader.run_page

    def setUp(self):
        super().setUp()
        self.doc = now_screen.document(self.connection, now=NOW, jobs=JobsBackend(self.tmp.name), fleet=fleet_document)

    def session(self, item, kind, session, *, verified=True, started="2026-09-06T09:00:00Z", elapsed="3h 0m", repo=None):
        return {"item": item, "title": f"Item {item}", "status": "in_progress", "kind": kind, "session": session,
                "started": started, "elapsed": elapsed, "seconds": 10800, "via": f"via {kind}", "verified": verified, "repo": repo}

    def grouped(self):
        """Live rows in two repos and one with none, and ready items: one in alpha, four in beta, one in a third repo."""
        self.doc["active"] = {"rows": [self.session(7, "worktree", "Worktree wt-7", repo="/repos/alpha"),
                                       self.session(11, "lane", "Lane queue (pending)", repo=None),
                                       self.session(9, "lane", "Lane queue (running)", repo="/repos/beta")],
                              "sources": {}, "error": ""}
        def ready(n, repo):
            return {"queue": "ready", "id": n, "repo": repo, "title": f"Ready {n}", "status": "ready",
                    "detail": "P2", "since": "2026-09-06T08:00:00Z"}
        self.doc["queues"] = {"rows": [ready(21, "/repos/alpha"), *[ready(n, "/repos/beta") for n in (31, 32, 33, 34)],
                                       ready(41, "/repos/gamma"),
                                       {**ready(51, "/repos/alpha"), "queue": "build", "status": "queued"}], "error": ""}
        self.doc["queues"]["suggested"] = test_v2_queue.as_suggested(self.doc["queues"]["rows"])

    #: The Queue view's first show (pack sd:7234), which folded In motion in; its other reads answer 404 here.
    SHOW = ("document.dispatchEvent(new CustomEvent('shell:view', { detail: { id: 'queue', first: true } }));\n"
            "await flush(); await flush();\n"
            # sd:9258: a row's key is its repo row's path; `open` takes the key or the shown name, `repos` the names.
            "const keyOf = k => ([...String(ELS.queue.html || '').matchAll(/class=\"pick\" data-open=\"([^\"]*)\">([^<]*)</g)]"
            ".find(m => m[2] === k) || [0, k])[1];\n"
            "const open = k => ELS.queue.listeners.click[0]({ target: { closest: s => s === 'tr.repo' ? { dataset: { repo: keyOf(k) } } : null } });\n"
            "const press = v => ELS['queue-filters'].listeners.click[0]({ target: { closest: s => s === 'button[data-f=\"qshow\"]'"
            " ? { dataset: { v } } : null } });\n"
            "const repos = () => [...ELS.queue.html.matchAll(/<tr class=\"repo\" data-repo=\"([^\"]*)\"[\\s\\S]*?class=\"pick\""
            " data-open=\"[^\"]*\">([^<]*)</g)].map(m => m[1] && m[2]);\n")
    AFTER = "\nANSWER = p => p === '/api/now' ? [200, DOC0] : [404, { error: 'not served here' }];\n"

    def draw(self, body, search="?show="):
        # pack sd:7314: Suggested is on with no `show` key; these draws start from every filter off, as `?show=` keeps it.
        return self.run_page(self.SHOW + body, search=search, after=self.AFTER)

    def test_rows_from_two_repos_form_two_groups_and_a_no_repo_row_lands_in_unassociated_last(self):
        # pack sd:7222, now the Queue view's: one parent row per repo; an item with no repo is Unassociated, last.
        self.grouped()
        out = self.draw("R.repos = repos(); open('alpha'); R.alpha = ELS.queue.html; open(''); R.none = ELS.queue.html;")
        # pack sd:7679: both work one; beta's four suggested rows (counted while hidden) put it first.
        self.assertEqual(out["R"]["repos"], ["beta", "alpha", ""])
        self.assertIn('data-type="working" data-item="7"', out["R"]["alpha"])
        self.assertNotIn('data-item="9"', out["R"]["alpha"])
        self.assertIn("Unassociated", out["R"]["none"])
        self.assertIn('data-type="working" data-item="11"', out["R"]["none"])

    def test_suggested_adds_each_repos_ready_items_that_the_default_hides(self):
        # pack sd:7222's Live + next is the Suggested filter: each repo's ready items, all of them (sd:10760), in order; a repo
        # with only ready items gets its row too. Off (`?show=` empty) shows none.
        self.grouped()
        out = self.draw("R.before = repos(); press('suggested'); R.after = repos(); R.bar = ELS['queue-filters'].html;"
                        " open('beta'); R.beta = ELS.queue.html; open('alpha'); R.alpha = ELS.queue.html;")
        r = out["R"]
        self.assertEqual(r["before"], ["beta", "alpha", ""])
        self.assertEqual(r["after"], ["beta", "alpha", "gamma", ""])
        self.assertIn('data-f="qshow" data-v="suggested" aria-pressed="true"', r["bar"])
        self.assertEqual([f"#{n} Ready {n}" in r["beta"] for n in (31, 32, 33, 34)], [True, True, True, True])
        self.assertIn("#21 Ready 21", r["alpha"])
        self.assertNotIn("#51 Ready 51", r["alpha"], "a build-queue row is no suggested item")

    def test_auto_pickup_shows_its_state_and_on_posts_the_value_read(self):
        """pack sd:6097, sd:8930: the Queue view draws auto-pickup off with Turn on, no end, and On posts the value read."""
        self.grouped()
        builds = {"runs": [], "recent": [], "error": "", "read": NOW, "show": "all",
                  "pickup": {"on": False, "value": None, "error": ""}}
        answer = (f"ANSWER = (p, b) => p === '/api/now' ? [200, DOC0] : p.startsWith('/api/queue/builds/pickup')"
                  f" ? [200, {{ on: true, value: 'on' }}] : p.startsWith('/api/queue/builds')"
                  f" ? [200, {json.dumps(builds)}] : [404, {{ error: 'not served here' }}];\n")
        out = self.run_page(answer + self.SHOW + "R.line = ELS['queue-pickup'].html;"
                            " ELS['queue-pickup'].listeners.click[0]({ target: { closest: s => s === 'button[data-pickup]'"
                            " ? { dataset: { pickup: 'on' }, disabled: false } : null } }); await flush(); await flush();"
                            " R.said = ELS['queue-said'].textContent;",
                            search="?show=", after="")
        self.assertIn("Auto-pickup is off", out["R"]["line"])
        self.assertIn('data-pickup="on"', out["R"]["line"])
        self.assertIn(">Turn on</button>", out["R"]["line"])
        self.assertNotIn("until", out["R"]["line"], "On has no end")
        self.assertEqual(out["R"]["said"], "Auto-pickup is on.")
        self.assertEqual([(path, body) for path, body, _ in out["posts"] if "pickup" in path],
                         [("/api/queue/builds/pickup", {"on": True, "value": None})])

    def test_auto_pickup_on_shows_no_end(self):
        """pack sd:8930: on reads as plain on with Turn off; it names no 09:00 end."""
        self.grouped()
        builds = {"runs": [], "recent": [], "error": "", "read": NOW, "show": "all",
                  "pickup": {"on": True, "value": "on", "error": ""}}
        answer = (f"ANSWER = (p, b) => p === '/api/now' ? [200, DOC0] : p.startsWith('/api/queue/builds')"
                  f" ? [200, {json.dumps(builds)}] : [404, {{ error: 'not served here' }}];\n")
        out = self.run_page(answer + self.SHOW + "R.line = ELS['queue-pickup'].html;", search="?show=", after="")
        self.assertIn("Auto-pickup is on:", out["R"]["line"])
        self.assertIn('data-pickup="off"', out["R"]["line"])
        self.assertNotIn("until", out["R"]["line"], "On has no end")

    def test_ready_items_that_could_not_be_read_say_so(self):
        self.grouped()
        self.doc["queues"] = {"rows": [], "suggested": [], "error": "database is locked"}
        out = self.draw("press('suggested'); R.note = ELS['queue-note'].textContent;")
        self.assertIn("Suggested items not read: database is locked", out["R"]["note"])

    def test_an_items_sessions_are_one_row_and_an_unread_source_is_said(self):
        self.doc["active"] = {"rows": [self.session(7, "worktree", "Worktree wt-7"),
                                       self.session(7, "agent", "herdr pane w1:p1", verified=False, started="", elapsed=""),
                                       self.session(9, "lane", "Lane queue (pending)", elapsed="5m")],
                              "sources": {"fleet": "", "lane": "", "launch": "", "herdr": "herdr agent list gave no answer in 5 s"},
                              "error": ""}
        out = self.draw("open(''); R.html = ELS.queue.html; R.note = ELS['queue-note'].textContent; R.hidden = ELS['queue-note'].hidden;")
        self.assertEqual(2, out["R"]["html"].count('<tr class="child" data-type="working" data-item='))
        self.assertIn('href="/tasks?row=7"', out["R"]["html"])
        self.assertIn("Worktree wt-7", out["R"]["html"])
        self.assertIn("▨", out["R"]["html"])
        self.assertIn("herdr agents not read: herdr agent list gave no answer in 5 s", out["R"]["note"])
        self.assertFalse(out["R"]["hidden"])

    def test_sessions_that_could_not_be_read_say_so_and_are_not_an_empty_list(self):
        self.doc["active"] = {"rows": [], "sources": {}, "error": "database is locked"}
        out = self.draw("R.html = ELS.queue.html; R.note = ELS['queue-note'].textContent;")
        self.assertIn("Sessions not read: database is locked", out["R"]["note"])
        self.assertNotIn("Nothing is working", out["R"]["html"])
