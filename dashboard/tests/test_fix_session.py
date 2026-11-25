"""Fix (sd:3456): a task starts a builder session and a report a debug session, through herdr.sh launch.

Every test runs the suite's fake `local-herdr/herdr.sh` against the fake of that tool's `herdr` double
(`local-herdr/tests/doubles`, sd:6044), first on PATH, and reads the argv lines it journals. No test reaches the installed herdr or starts claude:
`test_the_double_is_the_herdr_the_launch_finds` asserts which `herdr` the server's PATH resolves.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import fix_session
from sd_db import workflow
from test_now_screen import JobsBackend, fleet_document, repo, tree
from test_workflow_actions import BrowserSession

DOUBLES = fix_session.CHECKOUT / "local-herdr" / "tests" / "doubles"
SERVER_ERROR = "Error: herdr server could not bind its socket"
#: herdr.sh launch's words when the down session's headless server did not start (system sd:7847).
DOWN = f"session sd is not running and did not start: herdr --session sd server exited 1: {SERVER_ERROR}"
RUNNING = {"sessions": [{"name": "sd", "running": True, "socket_path": "/tmp/sd/herdr.sock", "default": False}]}


class FixCase(BrowserSession):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.fixture, self.journal, self.work = root / "fixture", root / "journal.tsv", root / "checkout"
        self.fixture.mkdir()
        self.work.mkdir()
        environment = patch.dict(os.environ, {"PATH": f"{DOUBLES}{os.pathsep}{os.environ.get('PATH', '')}",
                                              "HERDR_DOUBLE_FIXTURE": str(self.fixture), "HERDR_DOUBLE_JOURNAL": str(self.journal),
                                              "HERDR_STATE_DIR": str(root / "state"), "PYTHON": sys.executable})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("HERDR_SESSION", None)
        os.environ.pop("HERDR_SOCKET_PATH", None)

    def running(self):
        (self.fixture / "session-list.json").write_text(json.dumps(RUNNING))

    def down(self):
        """The session is down and its headless server does not start (system sd:7847): launch's one refusal for it."""
        (self.fixture / "server-fail").write_text(f"{SERVER_ERROR}\n")

    def calls(self):
        return [line.split("\t")[2:] for line in self.journal.read_text().splitlines()] if self.journal.exists() else []

    def revision(self, item):
        return workflow.item_state(self.connection, item)["revision"]

    def fix(self, item, **body):
        return self.post(f"/api/items/{item}/fix", {"revision": self.revision(item), **body})

    def fix_notes(self, item):
        """The Fix notes on the item: a started session's, or a failed launch's (pack sd:7756)."""
        return [note["body"] for note in workflow.item_state(self.connection, item)["notes"]
                if note["kind"] == "comment" and note["body"].startswith("Fix: ")]

    def task(self, title="Make the thing", **columns):
        columns.setdefault("repo", self.repo(str(self.work)))
        return self.item(title, kind="task", **columns)


class TheLaunch(FixCase):
    def test_the_double_is_the_herdr_the_launch_finds(self):
        self.assertEqual(Path(shutil.which("herdr")).parent, DOUBLES)

    def test_a_task_starts_a_builder_session_in_its_checkout_and_records_a_note(self):
        self.running()
        item = self.task()
        status, _, state = self.fix(item)
        self.assertEqual(status, 200, state)
        self.assertEqual(self.calls()[1:], [
            ["workspace", "create", "--cwd", str(self.work), "--label", f"sd-{item}", "--no-focus"],
            ["agent", "start", "sd-w9-p1", "--kind", "claude", "--pane", "w9:p1"],
            ["agent", "prompt", "sd-w9-p1", fix_session.prompt("task", item)],
        ])
        self.assertIn(f"Build sd:{item}.", fix_session.prompt("task", item))
        self.assertIn(f"`sd task show {item}`", fix_session.prompt("task", item))
        self.assertEqual(state["fix"], {"session": "builder", "pane": "w9:p1", "checkout": str(self.work)})
        note = state["notes"][-1]
        self.assertEqual((note["kind"], note["session"]), ("comment", "dashboard"))
        self.assertEqual(note["body"], f"Fix: started a builder session (claude) in herdr workspace sd-{item}, pane w9:p1, at {self.work}.")
        self.assertEqual(state["revision"], self.revision(item))

    def test_a_down_session_starts_headless_then_the_launch_goes_on(self):
        """system sd:7847: herdr.sh launch starts a down session as herdr's headless server, so Fix needs no
        `herdr.sh start` first; only a server that did not start is a 503 (TheRefusals)."""
        item = self.task()
        status, _, state = self.fix(item)
        self.assertEqual(status, 200, state)
        self.assertEqual([call[:3] for call in self.calls()[:4]], [
            ["session", "list", "--json"], ["--session", "sd", "server"], ["session", "list", "--json"],
            ["workspace", "create", "--cwd"]])
        self.assertEqual(self.fix_notes(item), [fix_session.NOTE.format(session="builder", item=item, pane="w9:p1",
                                                                        where=self.work)])

    def test_a_report_without_a_repo_starts_a_debug_session_in_this_checkout(self):
        self.running()
        item = self.item("nightly: needs attention", kind="report")
        status, _, state = self.fix(item)
        self.assertEqual(status, 200, state)
        self.assertEqual(self.calls()[1][:4], ["workspace", "create", "--cwd", str(fix_session.CHECKOUT)])
        sent = self.calls()[3]
        self.assertEqual(sent[:3], ["agent", "prompt", "sd-w9-p1"])
        self.assertIn(f"sd-debug skill on report sd:{item}", sent[3])
        self.assertIn(f"`sd reports acknowledge {item}`) only after a clean run", sent[3])
        self.assertEqual(state["fix"]["session"], "debug")

    def test_a_report_with_a_repo_starts_in_that_repo(self):
        self.running()
        item = self.item("nightly: needs attention", kind="report", repo=self.repo(str(self.work)))
        self.assertEqual(self.fix(item)[0], 200)
        self.assertEqual(self.calls()[1][3], str(self.work))

    def test_the_prompt_carries_the_id_and_never_the_title(self):
        self.running()
        item = self.task(title="$(touch pwned) `id`; rm -rf ~")
        self.assertEqual(self.fix(item)[0], 200)
        self.assertEqual(self.calls()[3][3], fix_session.prompt("task", item))
        self.assertNotIn("pwned", "".join(sum(self.calls(), [])))


class TheFailedLaunch(FixCase):
    """pack sd:7756: Fix on a Dependabot row filed example-site sd:7661, then its launch answered 503 (herdr's session sd
    was not running). The cause went to a toast and the journal's holder item only, so the item said nothing. A launch
    that fails leaves one comment note on the item naming the cause; the item's revision moves, the answer keeps its
    status and its error."""

    def notes(self, item):
        return [(note["kind"], note["session"], note["body"]) for note in workflow.item_state(self.connection, item)["notes"]]

    def test_a_dependabot_item_filed_then_a_launch_with_no_session_notes_the_cause(self):
        self.down()
        status, _, filed = self.post("/api/items", {"title": "Dependabot: 3 open alerts in example/anvil",
                                                    "repo": self.repo(str(self.work)), "ref": "dependabot:example/anvil"})
        self.assertEqual(status, 201, filed)
        item = filed["item"]["id"]
        before = self.notes(item)
        status, _, answer = self.post(f"/api/items/{item}/fix", {"revision": filed["revision"]})
        self.assertEqual(status, 503)
        self.assertEqual(answer["error"], f"Fix failed: herdr.sh launch: {DOWN}. A note on sd:{item} keeps the cause.")
        self.assertEqual(self.notes(item), [*before, ("comment", "dashboard", f"Fix: launch failed: herdr.sh launch: {DOWN}")])

    def test_a_launch_with_no_answer_notes_that_the_outcome_is_not_known(self):
        self.running()
        hook = self.fixture / "on-agent-start"
        hook.write_text("#!/bin/sh\nexec sleep 5\n")
        hook.chmod(0o755)
        item = self.task()
        with patch.object(fix_session, "TIMEOUT", 1):
            status, _, answer = self.fix(item)
        self.assertEqual(status, 504)
        kind, who, body = self.notes(item)[-1]
        self.assertEqual((kind, who), ("comment", "dashboard"))
        self.assertEqual(f"{body} A note on sd:{item} keeps the cause.", f"Fix: {answer['error']}")
        self.assertIn("whether a session started is not known", body)

    def test_a_failure_note_that_cannot_be_written_is_said_in_the_answer(self):
        self.down()
        item = self.task()
        before = self.unjournalled()
        with patch.object(workflow, "add_item_note", side_effect=sqlite3.OperationalError("database is locked")):
            status, _, answer = self.fix(item)
        self.assertEqual(status, 503)
        self.assertEqual(answer["error"], f"Fix failed: herdr.sh launch: {DOWN}. The item has no note of it: database is locked")
        self.assertEqual(self.unjournalled(), before)

    def test_the_failure_note_is_no_started_session_to_working_now(self):
        # Today's Working now reads the pane back from NOTE; a failure note must not read as a started session.
        self.down()
        item = self.task()
        self.fix(item)
        body = self.notes(item)[-1][2]
        self.assertTrue(body.startswith("Fix: "), body)
        self.assertFalse(body.startswith(fix_session.NOTE.split("{", 1)[0]), body)


class TheRefusals(FixCase):
    def test_a_session_that_did_not_start_is_a_503_in_herdr_sh_words_and_notes_the_cause(self):
        self.down()
        item = self.task()
        status, _, answer = self.fix(item)
        self.assertEqual(status, 503)
        self.assertEqual(answer["error"], f"Fix failed: herdr.sh launch: {DOWN}. A note on sd:{item} keeps the cause.")
        self.assertEqual([call[:3] for call in self.calls()],
                         [["session", "list", "--json"], ["--session", "sd", "server"], ["session", "list", "--json"]])
        self.assertEqual(self.fix_notes(item), [f"Fix: launch failed: herdr.sh launch: {DOWN}"])
        self.assertEqual(self.journalled(), [(f"Fix session · sd:{item}", 1)])  # sd:5541

    def test_herdr_not_installed_is_a_503_naming_herdr_sh_start_and_notes_the_cause(self):
        bare = Path(self.tmp.name) / "bin"
        bare.mkdir()
        for tool in ("sh", "dirname"):
            (bare / tool).symlink_to(shutil.which(tool))
        os.environ["PATH"] = str(bare)
        item = self.task()
        status, _, answer = self.fix(item)
        self.assertEqual(status, 503)
        self.assertEqual(answer["error"], "Fix failed: herdr.sh launch: herdr is not installed; install it, then run herdr.sh start."
                                          f" A note on sd:{item} keeps the cause.")
        self.assertEqual(self.fix_notes(item), ["Fix: launch failed: herdr.sh launch: herdr is not installed; install it, then run herdr.sh start"])
        self.assertEqual(self.journalled(), [(f"Fix session · sd:{item}", 1)])  # sd:5541

    def test_a_refused_start_is_a_503_that_says_the_workspace_is_left(self):
        self.running()
        (self.fixture / "agent-start-fail").write_text("")
        item = self.task()
        status, _, answer = self.fix(item)
        self.assertEqual(status, 503)
        self.assertIn("w9:p1: claude did not start", answer["error"])
        self.assertIn("left at its shell prompt", answer["error"])
        [noted] = self.fix_notes(item)
        self.assertIn("w9:p1: claude did not start", noted)
        self.assertEqual(self.journalled(), [(f"Fix session · sd:{item}", 1)])  # sd:5541

    def test_no_answer_in_time_is_a_504_that_says_the_outcome_is_not_known(self):
        self.running()
        hook = self.fixture / "on-agent-start"
        hook.write_text("#!/bin/sh\nexec sleep 5\n")
        hook.chmod(0o755)
        item = self.task()
        with patch.object(fix_session, "TIMEOUT", 1):
            status, _, answer = self.fix(item)
        self.assertEqual(status, 504)
        self.assertIn("whether a session started is not known", answer["error"])
        self.assertIn(f"workspace sd-{item}", answer["error"])
        self.assertEqual(len(self.fix_notes(item)), 1)
        self.assertEqual(self.journalled(), [(f"Fix session · sd:{item}", -1)])  # sd:5541

    def test_a_note_that_cannot_be_written_after_the_launch_names_the_pane(self):
        self.running()
        item = self.task()
        with patch.object(workflow, "add_item_note", side_effect=sqlite3.OperationalError("database is locked")):
            status, _, answer = self.fix(item)
        self.assertEqual(status, 503)
        self.assertIn("claude started in herdr pane w9:p1, but the note was not recorded: database is locked", answer["error"])
        self.assertIn(f"Record it with: sd task note {item} '", answer["error"])
        self.assertNotIn("--body", answer["error"])

    def test_a_second_click_from_the_same_page_is_stale_and_launches_nothing(self):
        self.running()
        item = self.task()
        revision = self.revision(item)
        self.assertEqual(self.post(f"/api/items/{item}/fix", {"revision": revision})[0], 200)
        launched = len(self.calls())
        status, _, answer = self.post(f"/api/items/{item}/fix", {"revision": revision})
        self.assertEqual(status, 409)
        self.assertTrue(answer["reload"])
        self.assertEqual(len(self.calls()), launched)

    def test_a_done_item_a_task_with_no_repo_and_a_missing_checkout_launch_nothing(self):
        self.running()
        done = self.task(status="done")
        bare = self.item("No repo", kind="task")
        gone = self.item("Moved", kind="task", repo=self.repo(str(self.work / "gone")))
        for item, says in ((done, "is done"), (bare, "names no repository"), (gone, "is not a directory on this machine")):
            with self.subTest(item=item):
                status, _, answer = self.fix(item)
                self.assertEqual(status, 400)
                self.assertIn(says, answer["error"])
        self.assertEqual(self.calls(), [])

    def test_a_task_that_waits_on_an_open_item_launches_nothing(self):
        """pack sd:9570 class pass: Fix starts a builder; like the Build press it refuses while a dependency is open."""
        self.running()
        first, item = self.task("First"), self.task()
        workflow.edit_item(self.connection, item, {"blocked_by": [first]}, who="test")
        status, _, answer = self.fix(item)
        self.assertEqual(status, 400)
        self.assertIn(f"waits on sd:{first}", answer["error"])
        self.assertEqual(self.calls(), [])

    def test_a_bad_body_a_missing_item_and_a_bad_origin_launch_nothing(self):
        self.running()
        item = self.task()
        good = {"revision": self.revision(item)}
        for payload in ({}, {"revision": "x"}, {**good, "who": "me"}, {**good, "prompt": "anything"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(f"/api/items/{item}/fix", payload)[0], 400)
        self.assertEqual(self.post("/api/items/999999/fix", good)[0], 404)
        self.assertEqual(self.post("/api/items/0/fix", good)[0], 404)
        self.assertEqual(self.post(f"/api/items/{item}/fix", good, **{"X-SD-CSRF": "0" * 64})[0], 403)
        self.assertEqual(self.post(f"/api/items/{item}/fix", good, Origin="http://example.test")[0], 403)
        self.assertEqual(self.calls(), [])


class TheNowRow(FixCase):
    """sd:5652: Fix on a Today Now row no command addresses starts claude in herdr on that row, through the same launch.

    The row is the page's id; the server reads the fleet again (the `fleet_backend` seam) and launches only for a row
    that read still raises, in the checkout that read names. Nothing is noted: a Now row has no item.
    """

    def setUp(self):
        self.repos, self.trees, self.dark = [], [], set()
        self.fleet_backend = self.read_fleet
        # A dark row is confirmed by Today's own read, which lists the jobs: a double, never launchd.
        jobs = tempfile.TemporaryDirectory()
        self.addCleanup(jobs.cleanup)
        self.backend = JobsBackend(jobs.name)
        super().setUp()

    def read_fleet(self, area):
        if area in self.dark:
            raise ValueError("fleet collector exited 1 without a reason")
        return fleet_document(area, repos=self.repos, trees=self.trees)

    def checkout(self, name="pushy", **counts):
        self.repos.append({**repo(name, **{k: v for k, v in counts.items() if k in ("ahead", "dirty")}),
                           "path": str(self.work), **{k: v for k, v in counts.items() if k not in ("ahead", "dirty")}})

    def fix_row(self, row):
        return self.post("/api/now/fix", {"row": row})

    def test_a_repo_row_starts_a_session_in_its_checkout_on_the_kinds_prompt(self):
        self.running()
        self.checkout(dirty=2)
        before = self.unjournalled()
        status, _, answer = self.fix_row("dirty:pushy:2")
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.calls()[1:], [
            ["workspace", "create", "--cwd", str(self.work), "--label", "fix-dirty-pushy", "--no-focus"],
            ["agent", "start", "sd-w9-p1", "--kind", "claude", "--pane", "w9:p1"],
            ["agent", "prompt", "sd-w9-p1", fix_session.row_prompt("dirty")],
        ])
        self.assertEqual(answer, {"fix": {"pane": "w9:p1", "checkout": str(self.work), "workspace": "fix-dirty-pushy"}})
        self.assertEqual(self.unjournalled(), before, "a Now row has no item, so nothing is noted")
        self.assertEqual(self.journalled(), [("Fix session · dirty:pushy:2", 0)])

    def test_each_repo_kind_has_its_own_fixed_prompt(self):
        self.running()
        self.checkout(ahead=1, pinned_behind=3, default="main")
        for row, kind, says in (("ahead:pushy:1", "ahead", "commits its remote does not have"),
                                ("pinned:pushy:3", "pinned", f"{fix_session.CHECKOUT}/local-repo-sync/repo-sync.sh refresh")):
            with self.subTest(row=row):
                status, _, answer = self.fix_row(row)
                self.assertEqual(status, 200, answer)
                self.assertEqual(self.calls()[-1][3], fix_session.row_prompt(kind))
                self.assertIn(says, fix_session.row_prompt(kind))
                self.assertEqual(answer["fix"]["workspace"], f"fix-{kind}-pushy")

    def test_the_prompt_never_carries_the_checkout_name(self):
        self.running()
        self.checkout(name="$(touch pwned)", dirty=1)
        self.assertEqual(self.fix_row("dirty:$(touch pwned):1")[0], 200)
        self.assertNotIn("pwned", self.calls()[-1][3])

    def test_abandoned_worktrees_and_a_dark_collector_start_in_this_checkout(self):
        self.running()
        self.trees.append(tree("gone", live=False))
        self.dark.add("repos")
        for row, kind in (("worktrees:1", "worktree"), ("dark:repos", "dark")):
            with self.subTest(row=row):
                status, _, answer = self.fix_row(row)
                self.assertEqual(status, 200, answer)
                self.assertEqual(self.calls()[-3][3], str(fix_session.CHECKOUT))
                self.assertEqual(self.calls()[-1][3], fix_session.row_prompt(kind, source="repos"))
        self.assertIn("its repos collector could not be read", fix_session.row_prompt("dark", source="repos"))

    def test_a_row_the_read_no_longer_raises_is_stale_and_launches_nothing(self):
        self.running()
        self.checkout(dirty=3)
        for row in ("dirty:pushy:2", "ahead:pushy:3", "worktrees:1", "dark:repos", "dark:sessions"):
            with self.subTest(row=row):
                status, _, answer = self.fix_row(row)
                self.assertEqual(status, 409, answer)
                self.assertTrue(answer["reload"])
        self.assertEqual(self.calls(), [])

    def test_a_fleet_read_that_failed_is_not_known_and_launches_nothing(self):
        """A read that failed is no answer: the row is neither confirmed nor gone."""
        self.running()
        self.checkout(dirty=2)
        self.dark.add("repos")
        status, _, answer = self.fix_row("dirty:pushy:2")
        self.assertEqual(status, 503)
        self.assertIn("the repos were not read, so whether dirty:pushy:2 is still a Now row is not known", answer["error"])
        self.assertEqual(self.calls(), [])

    def test_two_checkouts_with_the_row_launch_nothing(self):
        self.running()
        self.checkout(dirty=2)
        self.checkout(dirty=2)
        status, _, answer = self.fix_row("dirty:pushy:2")
        self.assertEqual(status, 400)
        self.assertIn("2 checkouts are named pushy", answer["error"])
        self.assertEqual(self.calls(), [])

    def test_a_bad_body_and_a_row_another_route_fixes_launch_nothing(self):
        self.running()
        self.checkout(dirty=2)
        for payload in ({}, {"row": 7}, {"row": "dirty:pushy:2", "prompt": "anything"}, {"row": "job:nightly:7"},
                        {"row": "pr:example/project#4:4"}, {"row": "dark:prs"}, {"row": "item:7"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post("/api/now/fix", payload)[0], 400)
        self.assertEqual(self.post("/api/now/fix", {"row": "dirty:pushy:2"}, Origin="http://example.test")[0], 403)
        self.assertEqual(self.calls(), [])

    def test_every_dark_row_today_raises_starts_a_session_while_the_read_still_raises_it(self):
        """sd:5954: Today raised dark lane, gate, reports, mail and live rows and a partial repos row, and Fix on each was a
        400 or a 409. Each is confirmed by Today's own read now and starts claude in this checkout on the dark prompt."""
        from sd_dashboard import active_sessions, now_screen, today_mail, today_reports
        from sd_db import reads

        def lane_down(connection, now):
            raise OSError("sd-ship gave no answer")

        def gate_down(connection, now):
            return {"lanes": [], "gates": {"error": "sd gate status gave no answer"}, "problems": []}

        def reports(connection, **_):
            return {"problems": [], "problems_more": 0, "problems_capped": False, "problems_error": "database is locked",
                    "briefs": [], "briefs_more": 0, "briefs_error": "", "briefs_skipped": 0}

        def mail(connection, **_):
            return {"mail": [], "mail_more": 0, "mail_unread": [{"repo": "hoa", "reason": "no alias"}], "mail_error": ""}

        def live(connection, **_):
            return {"rows": [], "sources": {"herdr": "herdr did not answer"}, "error": ""}

        self.running()
        cases = (("dark:lane", mock_lanes(lane_down)), ("dark:gate", mock_lanes(gate_down)),
                 ("dark:reports", patch.object(today_reports, "document", reports)),
                 ("dark:mail", patch.object(today_mail, "document", mail)),
                 ("dark:live", patch.object(active_sessions, "document", live)),
                 # pack sd:7706: the flaky heads' lane read raises its own partial row, and ROW_ID refused it.
                 ("dark:flaky", patch.object(reads, "lane_entries", side_effect=sqlite3.OperationalError("locked"))))
        for row, raised in cases:
            with self.subTest(row=row):
                with raised:
                    status, _, answer = self.fix_row(row)
                self.assertEqual(status, 200, answer)
                source = row.split(":")[1]
                self.assertEqual(answer["fix"]["workspace"], f"fix-dark-{source}")
                self.assertEqual(self.calls()[-3][3], str(fix_session.CHECKOUT))
                self.assertEqual(self.calls()[-1][3], fix_session.row_prompt("dark", source=source))
                # Without the failure the read raises no such row: the click is stale.
                self.assertEqual(self.fix_row(row)[0], 409)
        self.repos.append({**repo("gone", error="git did not answer"), "path": str(self.work)})
        status, _, answer = self.fix_row("dark:repos")
        self.assertEqual(status, 200, answer)
        self.assertNotIn("gone", self.calls()[-1][3], "no fleet value enters the prompt")
        self.assertIn("in whole or in part", fix_session.row_prompt("dark", source="repos"))
        self.assertTrue(all(fix_session.ROW_ID.fullmatch(f"dark:{source}") for source in now_screen.LAMP if source != "prs"),
                        "a source Today lamps can go dark, so Fix takes its dark row")

    def test_a_today_read_that_raised_is_not_known_and_launches_nothing(self):
        from sd_dashboard import now_screen

        self.running()
        self.dark.add("repos")
        with patch.object(now_screen, "document", side_effect=sqlite3.OperationalError("database is locked")):
            status, _, answer = self.fix_row("dark:repos")
        self.assertEqual(status, 503)
        self.assertIn("Today was not read, so whether dark:repos is still a Now row is not known", answer["error"])
        self.assertEqual(self.calls(), [])

    def test_a_session_that_did_not_start_is_a_503_in_herdr_sh_words(self):
        self.down()
        self.checkout(dirty=2)
        status, _, answer = self.fix_row("dirty:pushy:2")
        self.assertEqual(status, 503)
        self.assertEqual(answer["error"], f"Fix failed: herdr.sh launch: {DOWN}")


def mock_lanes(read):
    """Today's lane read, as support.ScreenCase stands it in, failing as `read` does."""
    from sd_dashboard import now_screen

    return patch.object(now_screen, "_lanes", read)


class TheApplyReview(FixCase):
    """pack sd:6699: Apply review on a piece starts claude on the fixed apply prompt, in the piece's repository."""

    def piece(self, report="current"):
        """A piece whose adversarial.md is `current` (stamped with its draft's digest), `unstamped`, or None: absent."""
        from sd_db import upsert_repo, writing

        folder = self.work / "content" / "2026" / "fixture"
        folder.mkdir(parents=True)
        (folder / "index.md").write_text("---\ntitle: Fixture piece\nstatus: review\n---\n\n## Draft\n\nAn argument.\n")
        upsert_repo(self.connection, str(self.work), pieces_source="file")
        state = writing.import_piece(self.connection, str(self.work), "2026/fixture", who="operator")
        upsert_repo(self.connection, str(self.work), pieces_source="row")
        stamp = f"<!-- reconciled-with-draft: {state['writing']['gates']['digest']} on 2026-09-20 gen=0 -->\n\n"
        if report == "layout":
            # sd:10719: the stamp names this draft, but index.md bytes other than the ones on disk.
            stamp = stamp.replace(" -->", f" index={'0' * 64} -->")
        if report:
            (folder / "adversarial.md").write_text((stamp if report in ("current", "layout") else "")
                                                   + "Adversarial fixture report\n")
        return state["item"]["id"]

    def test_apply_review_starts_the_apply_prompt_in_the_pieces_repository(self):
        self.running()
        item = self.piece()
        status, _, state = self.fix(item, apply="adversarial")
        self.assertEqual(status, 200, state)
        self.assertEqual(self.calls()[1][:4], ["workspace", "create", "--cwd", str(self.work)])
        sent = self.calls()[3][3]
        self.assertEqual(sent, fix_session.prompt("idea", item, apply=True))
        for said in (f"adversarial review of piece sd:{item}", "sd writing reconcile", "--artifact adversarial",
                     "Commit nothing", "publish nothing"):
            self.assertIn(said, sent)
        self.assertEqual(state["fix"]["session"], "apply-review")

    def test_apply_review_needs_a_piece_with_its_report(self):
        self.running()
        for item in (self.task(), self.piece(report=False)):
            status, _, said = self.fix(item, apply="adversarial")
            self.assertEqual(status, 400, said)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.fix(self.task(), apply="other")[0], 400)

    def test_apply_review_refuses_a_report_on_other_index_bytes(self):
        """sd:10719: a report whose draft stamp is current but whose index.md bytes changed since is stale."""
        self.running()
        item = self.piece(report="layout")
        status, _, said = self.fix(item, apply="adversarial")
        self.assertEqual(status, 400, said)
        self.assertIn("adversarial.md is stale", said["error"])
        self.assertEqual(self.calls(), [])

    def test_apply_review_refuses_a_report_that_answers_no_current_draft(self):
        """pack sd:6977: an adversarial.md that exists but carries no stamp of this draft is not a current review."""
        self.running()
        item = self.piece(report="unstamped")
        status, _, said = self.fix(item, apply="adversarial")
        self.assertEqual(status, 400, said)
        self.assertIn("adversarial.md is unstamped", said["error"])
        self.assertIn("run Refresh review first", said["error"])
        self.assertEqual(self.calls(), [])


class TheGrill(FixCase):
    """pack sd:6700: Grill on a piece starts claude on the fixed grill prompt; Apply answers on the answers prompt."""

    def piece(self):
        from sd_db import upsert_repo, writing

        folder = self.work / "content" / "2026" / "fixture"
        folder.mkdir(parents=True)
        (folder / "index.md").write_text("---\ntitle: Fixture piece\nstatus: review\n---\n\n## Draft\n\nAn argument.\n")
        upsert_repo(self.connection, str(self.work), pieces_source="file")
        item = writing.import_piece(self.connection, str(self.work), "2026/fixture", who="operator")["item"]["id"]
        upsert_repo(self.connection, str(self.work), pieces_source="row")
        return item

    def ask(self, item, body="Who is this for?\nOption: Engineers\nOption: Managers\nRecommendation: Engineers"):
        return workflow.add_item_note(self.connection, item, kind="question", who="claude", body=body)["note"]

    def test_grill_starts_the_grill_prompt_in_the_pieces_repository(self):
        self.running()
        item = self.piece()
        status, _, state = self.fix(item, grill="ask")
        self.assertEqual(status, 200, state)
        self.assertEqual(self.calls()[1][3], str(self.work))
        self.assertEqual(self.calls()[3][3], fix_session.prompt("idea", item, grill="ask"))
        self.assertIn(f"`sd task note {item} ", self.calls()[3][3])
        self.assertIn("--kind question", self.calls()[3][3])
        self.assertEqual(state["fix"]["session"], "grill")

    def test_apply_answers_waits_for_every_question_and_one_ruling(self):
        self.running()
        item = self.piece()
        for says in ("answer one grill question first", "1 grill question is open"):
            with self.subTest(says=says):
                if says.startswith("1"):
                    self.ask(item)
                status, _, answer = self.fix(item, grill="apply")
                self.assertEqual(status, 400, answer)
                self.assertIn(says, answer["error"])
        self.assertEqual(self.calls(), [])

    def test_apply_answers_starts_the_answers_prompt_once_answered(self):
        from sd_dashboard import decisions

        self.running()
        item = self.piece()
        note = self.ask(item)
        decisions.answer(self.connection, item, note["id"], "Engineers", expected_revision=self.revision(item), who="dashboard")
        status, _, state = self.fix(item, grill="apply")
        self.assertEqual(status, 200, state)
        self.assertEqual(self.calls()[3][3], fix_session.prompt("idea", item, grill="apply"))
        self.assertIn("Ruling on note #", self.calls()[3][3])
        self.assertIn("Commit nothing", self.calls()[3][3])
        self.assertEqual(state["fix"]["session"], "apply-answers")

    def test_grill_on_a_task_and_a_bad_grill_value_launch_nothing(self):
        self.running()
        task = self.task()
        status, _, answer = self.fix(task, grill="ask")
        self.assertEqual(status, 400)
        self.assertIn("is not a writing piece", answer["error"])
        self.assertEqual(self.fix(task, grill="other")[0], 400)
        self.assertEqual(self.calls(), [])
