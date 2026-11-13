"""The Build press on Tasks and the Builds panel on Queue (pack sd:6095), over real HTTP on a disposable store.

`builds.kickstart` is replaced, so nothing reaches launchctl; the room's gate and sessions are seams of `plan`.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sd_dashboard import builds
from sd_db import build, runner, ship, upsert_repo
from sd_db.workflow import item_state
from sd_db.writes import now as clock
from sd_db.writes import record_state, transition
from support import NOW
from test_workflow_actions import BrowserSession

REPO = "/fixture/repos/alpha"
IDLE = {"load": 1.0, "load_cap": 40.0, "interactive": []}


class Fixture(BrowserSession):
    def setUp(self):
        super().setUp()
        upsert_repo(self.connection, REPO, remote="/fixture/remote-alpha", managed=1)
        self.kicks = []
        kick = patch.object(builds, "kickstart", lambda: self.kicks.append(1) or {"ok": True, "service": "x", "why": ""})
        kick.start()
        self.addCleanup(kick.stop)
        # The fixtures name a satellite lane `work`, which a real machine may be called too: run as the hub `hub`,
        # reading no real `hub.json` in this HOME (pack sd:7955).
        for pinned in (patch.object(build, "this_host", return_value="hub"),
                       patch("sd_db.hub.configured", return_value=False)):
            pinned.start()
            self.addCleanup(pinned.stop)

    def ready(self, status="ready", **columns):
        return self.item("Build me", kind="task", status=status, repo=REPO, **columns)

    def revision(self, item):
        return item_state(self.connection, item)["revision"]

    def builds_of(self, item):
        return self.connection.execute("SELECT COUNT(*) FROM assignment WHERE item = ? AND scope = 'build'",
                                       (item,)).fetchone()[0]



class BuildRoutes(Fixture):
    def test_build_refuses_a_non_ready_item_with_409_and_writes_nothing(self):
        """Slice 4's fail-first: a press with a planning item is a 409, and no item of the selection is queued."""
        ready, planning = self.ready(), self.ready(status="planning")
        status, _, body = self.post("/api/tasks/build", {"items": [ready, planning], "revisions": {
            str(ready): self.revision(ready), str(planning): self.revision(planning)}})
        self.assertEqual(status, 409, body)
        self.assertIn(f"item {planning} is planning", body["error"])
        self.assertEqual((self.builds_of(ready), self.builds_of(planning), self.kicks), (0, 0, []))

    def test_build_refuses_a_stale_revision_with_409(self):
        item = self.ready()
        before = self.revision(item)
        self.connection.execute("UPDATE item SET title = 'moved' WHERE id = ?", (item,))
        status, _, body = self.post("/api/tasks/build", {"items": [item], "revisions": {str(item): before}})
        self.assertEqual((status, self.builds_of(item)), (409, 0), body)

    def test_build_queues_each_item_and_kickstarts_the_dispatcher(self):
        first, second = self.ready(), self.ready()
        status, _, body = self.post("/api/tasks/build", {"items": [first, second], "revisions": {
            str(first): self.revision(first), str(second): self.revision(second)}})
        self.assertEqual(status, 200, body)
        self.assertEqual([(row["item"], row["status"]) for row in body["queued"]], [(first, "queued"), (second, "queued")])
        self.assertEqual((body["kickstart"]["ok"], len(self.kicks)), (True, 1))
        self.assertEqual((self.builds_of(first), self.builds_of(second)), (1, 1))

    def test_a_satellite_press_names_its_host_and_starts_no_hub_tick(self):
        """Review round 2: the hub's kickstart claims nothing on a satellite, so a press of only satellite items
        starts no hub tick, and each queued row names the host whose dispatcher claims it."""
        upsert_repo(self.connection, "/fixture/repos/sat", remote="/fixture/remote-sat", managed=1)
        self.connection.execute("UPDATE repo SET lane_host = 'work' WHERE path = '/fixture/repos/sat'")
        record_state(self.connection, "heartbeat", key="build-dispatch@work", body={})
        sat = self.item("Build me there", kind="task", status="ready", repo="/fixture/repos/sat")
        status, _, body = self.post("/api/tasks/build", {"items": [sat], "revisions": {str(sat): self.revision(sat)}})
        self.assertEqual(status, 200, body)
        self.assertEqual(([(row["item"], row["host"]) for row in body["queued"]], body["kickstart"], self.kicks),
                         ([(sat, "work")], None, []))
        hub = self.ready()
        status, _, body = self.post("/api/tasks/build", {"items": [hub], "revisions": {str(hub): self.revision(hub)}})
        self.assertEqual(([(row["item"], row["host"]) for row in body["queued"]], len(self.kicks)), ([(hub, "")], 1))

    def test_build_refuses_a_malformed_body_with_400(self):
        item = self.ready()
        for payload in ({"items": [item]}, {"items": [], "revisions": {}}, {"items": [item, item], "revisions": {}},
                        {"items": ["1"], "revisions": {}}):
            status, _, body = self.post("/api/tasks/build", payload)
            self.assertEqual(status, 400, (payload, body))
        self.assertEqual(self.builds_of(item), 0)

    def test_the_plan_bundles_by_repository_and_names_each_refusal(self):
        items = [self.ready() for _ in range(4)]
        planning = self.ready(status="planning")
        out = builds.plan(self.connection, items + [planning], now=NOW,
                          sessions=lambda connection, *, now: {"sessions": [], "builders": {"running": 0, "why": ""}},
                          gate=lambda: {"load": [1.0, 1.0, 1.0], "load_max": 40.0})
        self.assertEqual([[row["id"] for row in bundle["items"]] for bundle in out["bundles"]], [items[:3], items[3:]])
        self.assertEqual({bundle["model"] for bundle in out["bundles"]}, {"opus"})
        self.assertEqual([row["id"] for row in out["refused"]], [planning])
        self.assertEqual((out["room"]["machine"], out["room"]["why"], out["bundles"][0]["room"]), (6, "", build.REPO_CAP))

    def test_the_plan_names_the_satellite_that_runs_a_bundle(self):
        """Pack sd:7933: a satellite lane's bundle names its host and none of the hub's room."""
        hub = self.ready()
        upsert_repo(self.connection, "/fixture/repos/sat", remote="/fixture/remote-sat", managed=1)
        self.connection.execute("UPDATE repo SET lane_host = 'work' WHERE path = '/fixture/repos/sat'")
        sat = self.item("Build me there", kind="task", status="ready", repo="/fixture/repos/sat")
        plan = lambda: builds.plan(  # noqa: E731
            self.connection, [hub, sat], now=NOW,
            sessions=lambda connection, *, now: {"sessions": [], "builders": {"running": 0, "why": ""}},
            gate=lambda: {"load": [1.0, 1.0, 1.0], "load_max": 40.0})
        out = plan()
        self.assertEqual([row["id"] for row in out["refused"]], [sat], "no dispatcher beats there")
        self.assertIn("lane runs on work, and no build-dispatch@work beat", out["refused"][0]["why"])
        fixed = build.clock()  # one clock for stamp and read: a busy gate let a second pass between them
        record_state(self.connection, "heartbeat", key="build-dispatch@work",
                     timestamp=(datetime.fromisoformat(fixed) - timedelta(seconds=42)).isoformat(), body={})
        with patch.object(build, "clock", return_value=fixed):
            out = plan()
        self.assertEqual(out["refused"], [])
        self.assertEqual([(bundle["name"], bundle["host"], bundle["room"], bundle["beat"])
                          for bundle in out["bundles"]], [("alpha", "", build.REPO_CAP, None), ("sat", "work", 0, 42)])

    def test_the_plan_shows_no_room_when_the_load_was_not_read(self):
        out = builds.plan(self.connection, [self.ready()], now=NOW,
                          sessions=lambda connection, *, now: {"sessions": [], "builders": {"running": 0, "why": ""}},
                          gate=lambda: {})
        self.assertEqual((out["room"]["machine"], out["room"]["why"], out["bundles"][0]["room"]),
                         (0, "the load was not read", 0))

    def test_the_room_counts_the_slots_the_session_count_holds(self):
        """pack sd:8606: the Build dialog counted a stalled builder that `claude_sessions.builders` frees (pack
        sd:6400), and an unread session made its whole room unread. Both read `builders`' holders now: one slot per
        holder, against the repository its cwd lies in."""
        from sd_dashboard import claude_sessions

        def session(ident, status, agents, why=()):
            return {"id": ident, "name": ident, "cwd": f"{REPO}/src", "status": status, "builder": False, "age": 5,
                    "quiet": 5, "safe": {"state": "unknown", "why": list(why)},
                    "agents": [{"name": f"{ident}-b", "builder": True, "state": state, "age": 5} for state in agents]}

        listed = [session("s1", "busy", ["stalled"]),
                  session("s2", "busy", [], why=["no transcript was found, so its last action and builders were not read"])]
        document = {"sessions": listed, "builders": claude_sessions.builders(listed, "")}
        found = builds.interactive(self.connection, now=NOW, sessions=lambda connection, *, now: document)
        self.assertEqual(found, [{"repo": REPO, "session_id": "s2"}])

    def test_the_plan_route_reads_its_items(self):
        item = self.ready()
        with patch.object(builds, "_room", lambda connection, **_: {"machine": 6, "used": {}, "held": [], "why": ""}):
            status, _, body = self.request(f"/api/tasks/build/plan?items={item}", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        self.assertIn('"bundles"', body)
        status, _, _ = self.request("/api/tasks/build/plan?items=abc", headers={"Cookie": self.cookie})
        self.assertEqual(status, 400)


class BuildsPanel(Fixture):
    def claimed(self, items):
        build.queue(self.connection, items, who="fixture")
        return build.claim(self.connection, owner="fixture", root=self.path.parent / "worktrees", **IDLE)

    def blocked(self, items):
        """A run lost twice at one step: blocked, released, its items blocked (the reap's second loss)."""
        run = self.claimed(items)
        build.write(self.connection, run["id"], step="building")
        retry = build.reap(self.connection, now="2099-01-01T00:00:00Z")[0]["retry"]
        build.reap(self.connection, now="2099-01-02T00:00:00Z")
        return build.run(self.connection, retry)

    def panel(self, now="2099-01-02T01:00:00Z", show="all"):
        return builds.panel(self.connection, now=now, show=show)

    def test_an_open_run_is_a_row_with_its_items_step_state_and_cancel(self):
        items = [self.ready(), self.ready()]
        run = self.claimed(items)
        out = builds.panel(self.connection, now=run["created_at"])
        self.assertEqual(out["error"], "")
        row = out["runs"][0]
        self.assertEqual((row["run"], [m["item"] for m in row["items"]], row["step"], row["state"]),
                         (run["id"], items, "claimed", "working"))
        self.assertEqual((row["cancel"]["assignment"], row["cancel"]["allowed"]), (run["assignment"], True))
        status, _, body = self.post(f"/api/assignments/{run['assignment']}/cancel",
                                    {"revision": row["cancel"]["revision"]})
        self.assertEqual(status, 200, body)
        self.assertIsNotNone(build.run(self.connection, run["id"])["cancel_requested"])
        again = builds.panel(self.connection, now=run["created_at"])["runs"][0]["cancel"]
        self.assertFalse(again["allowed"])

    def test_a_satellite_run_names_its_host_and_reads_no_log_here(self):
        """Pack sd:7933, failure table Report back: the hub reads no log path a satellite's run names."""
        run = self.claimed([self.ready()])
        log = self.path.parent / "sat.jsonl"
        log.write_text("{}\n")
        self.connection.execute("UPDATE runner_run SET owner = 'build-dispatch@work', log_path = ? WHERE id = ?",
                                (str(log), run["id"]))
        row = builds.panel(self.connection, now="2099-01-02T01:00:00Z")["runs"][0]
        self.assertEqual((row["host"], row["log"], row["quiet"]), ("work", str(log), None))
        hub = self.claimed([self.ready()])
        self.connection.execute("UPDATE runner_run SET log_path = ? WHERE id = ?", (str(log), hub["id"]))
        rows = {row["run"]: row for row in builds.panel(self.connection, now="2099-01-02T01:00:00Z")["runs"]}
        self.assertEqual(rows[hub["id"]]["host"], "")
        self.assertIsNotNone(rows[hub["id"]]["quiet"], "the hub's own run's log is read")

    def test_a_lane_named_for_the_hub_is_the_hubs_on_the_panel(self):
        """Review round 3, class A: a lane named for the hub plans with the hub's room, kicks the hub's tick, and its
        run's log is read here, as `build.lane_of` reads it."""
        upsert_repo(self.connection, "/fixture/repos/own", remote="/fixture/remote-own", managed=1)
        self.connection.execute("UPDATE repo SET lane_host = 'hub' WHERE path = '/fixture/repos/own'")
        own = self.item("Build me here", kind="task", status="ready", repo="/fixture/repos/own")
        with patch.object(build, "this_host", return_value="hub"):
            out = builds.plan(self.connection, [own], now=NOW,
                              sessions=lambda connection, *, now: {"sessions": [], "builders": {"running": 0, "why": ""}},
                              gate=lambda: {"load": [1.0, 1.0, 1.0], "load_max": 40.0})
            self.assertEqual((out["refused"], [(b["host"], b["room"]) for b in out["bundles"]]), ([], [("", build.REPO_CAP)]))
            status, _, body = self.post("/api/tasks/build", {"items": [own], "revisions": {str(own): self.revision(own)}})
            self.assertEqual((status, [row["host"] for row in body["queued"]], len(self.kicks)), (200, [""], 1), body)
            run = self.claimed([self.ready()])
            log = self.path.parent / "own.jsonl"
            log.write_text("{}\n")
            self.connection.execute("UPDATE runner_run SET owner = 'build-dispatch@hub', log_path = ? WHERE id = ?",
                                    (str(log), run["id"]))
            row = {row["run"]: row for row in builds.panel(self.connection, now="2099-01-02T01:00:00Z")["runs"]}[run["id"]]
        self.assertEqual(row["host"], "", "an older run owned build-dispatch@hub is the hub's")
        self.assertIsNotNone(row["quiet"], "its log is read here")

    def test_retry_refuses_a_run_the_hub_blocked_for_a_quiet_satellite(self):
        """Review round 4: the hub blocks a quiet satellite's run but keeps its lease, so Retry cannot start a
        second builder in its worktree while work's processes may still run there."""
        item = self.ready()
        run = self.claimed([item])
        self.connection.execute("UPDATE runner_run SET owner = 'build-dispatch@work' WHERE id = ?", (run["id"],))
        [settled] = build.reap(self.connection, now="2099-01-02T00:00:00Z")
        self.assertEqual(settled["outcome"], "blocked")
        self.assertEqual(item_state(self.connection, item)["item"]["status"], "blocked")
        revision = runner.queue_state(self.connection, run["assignment"])["revision"]
        status, _, body = self.post("/api/queue/builds/retry", {"assignment": run["assignment"], "revision": revision})
        self.assertEqual(status, 400, body)
        self.assertIn("the run still holds its lease", body["error"])
        self.assertEqual(self.builds_of(item), 1, "nothing queued again")

    def test_a_queued_satellite_row_names_why_its_dispatcher_claims_nothing(self):
        """Review round 5: the hub's claim leaves the row queued on a fresh beat with a `why`, and the panel names it."""
        upsert_repo(self.connection, "/fixture/repos/sat", remote="/fixture/remote-sat", managed=1)
        self.connection.execute("UPDATE repo SET lane_host = 'work' WHERE path = '/fixture/repos/sat'")
        record_state(self.connection, "heartbeat", key="build-dispatch@work", body={"why": ""})
        sat = self.item("Build me there", kind="task", status="ready", repo="/fixture/repos/sat")
        [row] = build.queue(self.connection, [sat], who="fixture")
        self.assertEqual(self.panel()["unclaimed"], {})
        record_state(self.connection, "heartbeat", key="build-dispatch@work", body={"why": "the load was not read"})
        self.assertIsNone(build.claim(self.connection, owner="fixture", root=self.path.parent / "worktrees", **IDLE))
        self.assertEqual(runner.queue_state(self.connection, row["id"])["status"], "queued")
        self.assertEqual(self.panel()["unclaimed"], {str(row["id"]): "work claims nothing: the load was not read"})

    def test_a_teammate_run_is_a_working_row_with_its_worktree_due_time_and_no_cancel(self):
        """pack sd:7221: a lead's in-session builder shows beside the headless runs; past its budget it is stalled."""
        from sd_db import teammate

        items = [self.ready(status="in_progress"), self.ready(status="in_progress")]
        run = teammate.start(self.connection, items, worktree="/w/pack-7221", branch="feat/x", budget_minutes=90,
                             owner="lead")
        [row] = self.panel(run["created_at"])["runs"]
        self.assertEqual((row["run"], row["teammate"], [m["item"] for m in row["items"]], row["state"], row["worktree"],
                          row["branch"], row["due"], row["budget"]),
                         (run["id"], True, items, "working", "/w/pack-7221", "feat/x", run["due"], 90))
        self.assertFalse(row["cancel"]["allowed"])
        self.assertIn(f"sd assignments beat {items[0]} --end", row["cancel"]["reason"])
        late = self.panel(run["due"])["runs"][0]
        self.assertEqual((late["state"], late["why"]), ("stalled", "past its budget"))
        edge = (datetime.fromisoformat(run["created_at"]) + timedelta(minutes=90 + teammate.GRACE_MINUTES)).isoformat()
        overdue = self.panel(edge)["runs"][0]
        self.assertEqual(overdue["state"], "stalled")
        self.assertTrue(overdue["why"].startswith("overdue: no beat for 120 min"), overdue["why"])
        teammate.end(self.connection, items[0])
        self.assertEqual(self.panel(run["created_at"])["runs"], [])

    def test_a_silent_supervisor_is_lost_and_a_quiet_builder_log_is_stalled(self):
        run = self.claimed([self.ready()])
        self.assertEqual(self.panel()["runs"][0]["state"], "lost")
        log = self.path.parent / "run.jsonl"
        log.write_text("{}\n")
        build.write(self.connection, run["id"], step="building", log_path=str(log),
                    heartbeat_at="2099-01-02T00:59:30Z")
        import os
        os.utime(log, (0, 0))
        self.assertEqual(self.panel()["runs"][0]["state"], "stalled")

    def test_a_beat_stamped_far_ahead_is_lost_and_a_small_lead_works(self):
        """sd:7986: a satellite clock far ahead read as a live run; `build._lead_age` rules."""
        run = self.claimed([self.ready()])
        build.write(self.connection, run["id"], heartbeat_at="2099-01-02T01:10:01Z")
        self.assertEqual((self.panel()["runs"][0]["state"], self.panel()["runs"][0]["beat"]), ("lost", None))
        build.write(self.connection, run["id"], heartbeat_at="2099-01-02T01:01:00Z")
        self.assertEqual((self.panel()["runs"][0]["state"], self.panel()["runs"][0]["beat"]), ("working", 0))

    def test_an_open_question_makes_a_run_wait_on_the_operator(self):
        item = self.ready()
        run = self.claimed([item])
        build.write(self.connection, run["id"], heartbeat_at="2099-01-02T00:59:30Z")
        self.note(item, "Which option?", kind="question")
        row = self.panel()["runs"][0]
        self.assertEqual(row["state"], "waiting")
        self.assertIn("question note", row["why"])

    def test_an_unmanaged_run_past_enqueuing_says_to_close_its_draft(self):
        """sd:7909: `sd-ship lane cancel` refuses an unmanaged repository, so the row names the draft instead."""
        items = [self.ready()]
        run = self.claimed(items)
        upsert_repo(self.connection, REPO, remote="https://github.com/example/alpha.git", managed=0)
        build.write(self.connection, run["id"], step="enqueuing")
        cancel = self.panel(run["created_at"])["runs"][0]["cancel"]
        self.assertFalse(cancel["allowed"])
        self.assertIn("close its draft pull request", cancel["reason"])
        self.assertNotIn("sd-ship lane cancel", cancel["reason"])

    def test_a_run_whose_branch_shipped_carries_its_pull_request_from_the_ship_receipt(self):
        """Pack sd:6020 ruling #31553: the row's Open PR reads `sd-ship`'s receipt for the branch; nothing asks GitHub."""
        items = [self.ready(), self.ready()]
        run = self.claimed(items)
        self.assertEqual(self.panel(run["created_at"])["runs"][0]["pull"], "")  # a remote that is not GitHub's
        upsert_repo(self.connection, REPO, remote="https://github.com/example/alpha.git", managed=1)
        self.assertEqual(self.panel(run["created_at"])["runs"][0]["pull"], "")  # never shipped
        url = "https://github.com/example/alpha/pull/7"
        # The lane names an entry by the first member the branch delivers, which need not be the lead.
        ship.save(self.connection, ship.receipt_key("example/alpha", run["branch"], items[1]), 0, {
            "repository": "example/alpha", "branch": run["branch"], "item": items[1],
            "pull_request": {"number": 7, "repository": "example/alpha", "base": "main", "url": url}})
        self.assertEqual(self.panel(run["created_at"])["runs"][0]["pull"], url)
        build.end(self.connection, run["id"], "done", "landed")
        self.assertEqual(self.panel(clock())["recent"][0]["pull"], url)

    def test_the_pull_request_lookup_matches_every_stored_form_of_the_repository(self):
        """A repository registered by its `~/` key still finds its remote from the absolute form a run may hold."""
        absolute = str(Path.home() / "repos" / "example-beta")
        upsert_repo(self.connection, "~/repos/example-beta", remote="https://github.com/example/beta.git", managed=1)
        url = "https://github.com/example/beta/pull/9"
        ship.save(self.connection, ship.receipt_key("example/beta", "build-1", 1), 0, {
            "repository": "example/beta", "branch": "build-1", "item": 1,
            "pull_request": {"number": 9, "repository": "example/beta", "base": "main", "url": url}})
        self.assertEqual(build.pull_url(self.connection, absolute, "build-1", [1]), url)

    def test_retry_on_a_blocked_run_queues_its_items_again(self):
        items = [self.ready(), self.ready()]
        run = self.blocked(items)
        self.assertEqual([item_state(self.connection, i)["item"]["status"] for i in items], ["blocked", "blocked"])

        def parked():
            return [item_state(self.connection, i)["item"]["parked_at"] is not None for i in items]
        self.assertEqual(parked(), [True, True], "pack sd:6132: a blocked run parks its items")
        recent = self.panel(clock())["recent"]
        self.assertEqual((recent[0]["outcome"], recent[0]["retry"]["allowed"]), ("interrupted", True))
        status, _, body = self.post("/api/queue/builds/retry", {"assignment": run["assignment"],
                                                               "revision": recent[0]["retry"]["revision"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(row["item"] for row in body["queued"]), items)
        self.assertEqual([item_state(self.connection, i)["item"]["status"] for i in items], ["ready", "ready"])
        self.assertEqual(parked(), [False, False], "Retry unparks what the block parked")
        queued = self.connection.execute("SELECT COUNT(*) FROM assignment WHERE scope = 'build' AND status = 'queued'")
        self.assertEqual(queued.fetchone()[0], 2)
        self.assertEqual(len(self.kicks), 1)
        status, _, body = self.post("/api/queue/builds/retry", {"assignment": run["assignment"],
                                                               "revision": recent[0]["retry"]["revision"]})
        self.assertEqual(status, 409, body)

    def test_a_parked_run_ended_days_ago_shows_under_all_with_retry_not_under_active(self):
        """Pack sd:6834: the parked note points to Retry on the Builds panel, so All lists every ended run, however
        old, with its Retry and assignment; Active lists the open runs only."""
        run = self.blocked([self.ready()])
        later = "2099-01-09T00:00:00Z"
        everything = self.panel(later)
        newest = everything["recent"][0]
        self.assertEqual((newest["run"], newest["assignment"], newest["retry"]["allowed"]),
                         (run["id"], run["assignment"], True))
        self.assertEqual(everything["show"], "all")
        active = self.panel(later, show="active")
        self.assertEqual((active["recent"], active["show"]), ([], "active"))
        status, _, body = self.request("/api/queue/builds?show=all", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["recent"][0]["run"], run["id"])

    def test_retry_refuses_a_run_that_did_not_block(self):
        run = self.claimed([self.ready()])
        build.end(self.connection, run["id"], "done", "landed")
        recent = self.panel(clock())["recent"][0]
        self.assertEqual((recent["outcome"], recent["retry"]["allowed"]), ("done", False))
        status, _, body = self.post("/api/queue/builds/retry", {"assignment": run["assignment"],
                                                               "revision": recent["retry"]["revision"]})
        self.assertEqual(status, 400, body)
        self.assertIn("Retry takes a blocked or cancelled one", body["error"])

    def test_retry_with_a_stale_revision_is_a_409_and_writes_nothing(self):
        items = [self.ready()]
        run = self.blocked(items)
        status, _, body = self.post("/api/queue/builds/retry", {"assignment": run["assignment"], "revision": "0" * 64})
        self.assertEqual(status, 409, body)
        self.assertEqual(item_state(self.connection, items[0])["item"]["status"], "blocked")
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM assignment WHERE scope = 'build' AND status = 'queued'").fetchone()[0], 0)


class SendToBuilder(Fixture):
    claimed, panel = BuildsPanel.claimed, BuildsPanel.panel

    def cancelled(self, items):
        run = self.claimed(items)
        build.end(self.connection, run["id"], "cancelled", "cancelled by the operator")
        return run

    def test_send_to_builder_notes_the_finding_and_queues_the_bundle_for_its_worktree(self):
        """Pack sd:6020: a cancelled bundle takes a finding; each item gets the note, the bundle queues again under
        its lead, and the next claim names the same branch and worktree."""
        items = [self.ready(), self.ready()]
        run = self.cancelled(items)
        recent = self.panel(clock())["recent"][0]
        self.assertEqual((recent["outcome"], recent["retry"]["allowed"]), ("cancelled", True))
        status, _, body = self.post("/api/queue/builds/retry", {"assignment": run["assignment"],
                                                               "revision": recent["retry"]["revision"],
                                                               "finding": "  The label reads wrong on phones "})
        self.assertEqual(status, 200, body)
        for item in items:
            notes = [row["body"] for row in self.connection.execute("SELECT body FROM note WHERE item = ?", (item,))]
            self.assertIn("Send to builder: The label reads wrong on phones", notes)
        again = build.claim(self.connection, owner="fixture", root=self.path.parent / "worktrees", **IDLE)
        self.assertEqual((again["assignment"], again["branch"], again["work_path"]),
                         (run["assignment"], run["branch"], run["work_path"]))
        self.assertEqual(len(self.kicks), 1)

    def test_send_to_builder_refuses_an_empty_or_long_finding_and_writes_nothing(self):
        run = self.cancelled([self.ready()])
        revision = self.panel(clock())["recent"][0]["retry"]["revision"]
        for finding in ("", "   ", 7, "x" * (builds.FINDING_MAX + 1)):
            status, _, body = self.post("/api/queue/builds/retry", {"assignment": run["assignment"],
                                                                   "revision": revision, "finding": finding})
            self.assertEqual(status, 400, (finding, body))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM assignment WHERE scope = 'build' AND status = 'queued'").fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM note WHERE body LIKE 'Send to builder:%'").fetchone()[0], 0)


class LaneFix(Fixture):
    """pack sd:8584: Fix on a failed lane entry sends the build run that enqueued it back to its builder with the
    lane's finding, through Send to builder's route; a Retry with no finding still refuses that run."""

    claimed = BuildsPanel.claimed

    def lane_failed(self, items, reason="local review blocking: 1/1 completed", ident="e-failed"):
        run = self.claimed(items)
        build.write(self.connection, run["id"], step="enqueuing")
        self.assertTrue(build.enqueued(self.connection, run["id"]))
        self.entry(run, "failed", items[0], ident)
        self.assertTrue(build.lane_failed(self.connection, build.run(self.connection, run["id"]), reason))
        return run

    def fix(self, fix, finding="fix it", run=None):
        """Fix's press as today.js posts it: the lead, its revision, the finding and the run the page read."""
        return self.post("/api/queue/builds/retry", {"assignment": fix["assignment"], "revision": fix["revision"],
                                                     "finding": finding, "run": run or fix["run"]})

    def entry(self, run, status, item, ident):
        """A lane entry on `run`'s branch as `sd_lane.store` writes it: a new `checkpoint` row of its key."""
        with self.connection:
            self.connection.execute(
                "INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                (f"lane:v1:example/repo:{ident}", clock(), json.dumps(
                    {"id": ident, "item": item, "branch": run["branch"], "status": status, "expected_head": "a" * 40,
                     "reason": "local review blocking: 1/1 completed"})))

    def lane_entry(self, ident):
        found = self.connection.execute("SELECT body FROM state WHERE key = ? ORDER BY id DESC LIMIT 1",
                                        (f"lane:v1:example/repo:{ident}",)).fetchone()
        return json.loads(found[0])

    def queued(self):
        return self.connection.execute("SELECT COUNT(*) FROM assignment WHERE scope = 'build' AND status = 'queued'"
                                       ).fetchone()[0]

    def test_fix_sends_a_lane_failed_bundle_back_to_its_builder_with_the_finding(self):
        items = [self.ready(), self.ready()]
        run = self.lane_failed(items)
        fix = builds.lane_fix_state(self.connection, run["branch"])
        self.assertEqual((fix["allowed"], fix["assignment"]), (True, run["assignment"]), fix)
        status, _, body = self.fix(fix, "The lane's review blocked a.py:1")
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(row["item"] for row in body["queued"]), items)
        self.assertEqual([item_state(self.connection, i)["item"]["status"] for i in items], ["ready", "ready"])
        for item in items:
            notes = [row["body"] for row in self.connection.execute("SELECT body FROM note WHERE item = ?", (item,))]
            self.assertIn("Send to builder: The lane's review blocked a.py:1", notes)
        # Round 2, row 5: the bundle is queued again before a builder claims it, so the entry's Fix is off.
        after = builds.lane_fix_state(self.connection, run["branch"])
        self.assertEqual((after["allowed"], after["reason"]), (False, "the bundle is queued again"), after)
        self.assertEqual(len(self.kicks), 1)
        again = build.claim(self.connection, owner="fixture", root=self.path.parent / "worktrees", **IDLE)
        self.assertEqual((again["assignment"], again["branch"], again["work_path"]),
                         (run["assignment"], run["branch"], run["work_path"]))
        # The brief quotes the finding: the note is inside the window the next run reads (`build_run.findings`).
        first = self.connection.execute("SELECT MIN(created_at) FROM runner_run WHERE assignment = ?",
                                        (run["assignment"],)).fetchone()[0]
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM note WHERE body LIKE 'Send to builder:%' AND timestamp >= ? AND timestamp <= ?",
            (first, again["created_at"])).fetchone()[0], 2)
        status, _, body = self.fix(fix, "again")
        self.assertEqual(status, 409, body)
        # The failed entry is handed back to the builder: cancelled, so the lane runs it no more (round 1).
        handed = self.lane_entry("e-failed")
        self.assertEqual((handed["status"], handed["handed_back"]["run"]), ("cancelled", run["id"]), handed)
        # Row 6: a builder holds the bundle again.
        held = builds.lane_fix_state(self.connection, run["branch"])
        self.assertEqual((held["allowed"], held["reason"]), (False, f"build run {again['id']} still holds its lease"))

    def test_fix_is_off_and_refused_when_a_rebuilt_bundle_ended_blocked_or_cancelled(self):
        """Round 2, rows 6 and 7: a conflict rebuild queued the bundle, its next run ended blocked or cancelled, and
        run 1's entry is still a Queue row. Fix there is off, and its press is refused. The rebuild handed that
        entry back itself (pack sd:8601), so it is not retryable."""
        for outcome in ("blocked", "cancelled"):
            with self.subTest(outcome=outcome):
                items = [self.ready()]
                first = self.lane_failed(items, reason="merging origin/main into build/x conflicts", ident=f"e-{outcome}")
                rerun = build.claim(self.connection, owner="fixture", root=self.path.parent / "worktrees", **IDLE)
                self.assertEqual((rerun["assignment"], rerun["branch"]), (first["assignment"], first["branch"]))
                build.end(self.connection, rerun["id"], outcome, "ended by the fixture")
                with self.connection:  # a blocked run is held until a reap reads its supervisor gone
                    for table, column in (("runner_run", "id"), ("runner_lease", "run")):
                        self.connection.execute(f"UPDATE {table} SET released_at = ? WHERE {column} = ? "
                                                "AND released_at IS NULL", (clock(), rerun["id"]))
                fix = builds.lane_fix_state(self.connection, first["branch"])
                self.assertEqual((fix["allowed"], fix["run"]), (False, rerun["id"]), fix)
                self.assertIn(f"ended {outcome}, not lane_failed", fix["reason"])
                for run in (rerun["id"], first["id"]):
                    status, _, body = self.fix(fix, run=run)
                    self.assertEqual(status, 400, body)
                    self.assertIn("Fix refused", body["error"])
                handed = self.lane_entry(f"e-{outcome}")
                self.assertEqual((self.queued(), handed["status"], handed["handed_back"]["by"]), (0, "cancelled", "build"))

    def test_fix_refuses_while_the_lane_holds_the_branch_again_or_holds_no_entry_and_writes_nothing(self):
        """Round 1: a Retry from another page put the branch back in the lane; Fix would hand its worktree to a
        builder while the lane prepares or merges it. No entry read is unknown, not failed."""
        items = [self.ready()]
        run = self.lane_failed(items)
        fix = builds.lane_fix_state(self.connection, run["branch"])
        for status, said in (("pending", f"the lane holds branch {run['branch']} again (pending)"),
                             ("running", f"the lane holds branch {run['branch']} again (running)"),
                             ("cancelled", f"branch {run['branch']}'s newest lane entry is cancelled"),
                             ("merged", f"branch {run['branch']}'s newest lane entry is merged")):
            with self.subTest(status=status):
                self.entry(run, status, items[0], "e-retried")
                state = builds.lane_fix_state(self.connection, run["branch"])
                self.assertEqual((state["allowed"], said in state["reason"]), (False, True), state)
                status_code, _, body = self.fix(fix)
                self.assertEqual(status_code, 400, body)
                self.assertIn(said, body["error"])
                self.assertEqual((self.queued(), self.lane_entry("e-failed")["status"]), (0, "failed"))
        with self.connection:
            self.connection.execute("DELETE FROM state WHERE key LIKE 'lane:v1:%'")
        status_code, _, body = self.fix(fix)
        self.assertEqual(status_code, 400, body)
        self.assertIn(f"no lane entry holds branch {run['branch']}", body["error"])
        self.assertEqual((self.queued(), self.connection.execute(
            "SELECT COUNT(*) FROM note WHERE body LIKE 'Send to builder:%'").fetchone()[0]), (0, 0))

    def test_an_unreadable_lane_entry_of_another_repository_leaves_fix_on(self):
        """pack sd:8636: Fix read every repository's lane and refused on any row not JSON. It reads the lane of the
        entry the watch named in the run's detail, as the conflict rebuild does (pack sd:8615)."""
        items = [self.ready()]
        run = self.lane_failed(items, reason="lane entry e-failed failed: local review blocking: 1/1 completed")
        bad = "INSERT INTO state(kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, 'not json')"
        with self.connection:  # its own repository's lane is this branch's: an unread row there is unknown
            self.connection.execute(bad, ("lane:v1:example/repo:bad", clock()))
        state = builds.lane_fix_state(self.connection, run["branch"])
        self.assertEqual((state["allowed"], "lane:v1:example/repo:bad was not read" in state["reason"]), (False, True))
        with self.connection:
            self.connection.execute("DELETE FROM state WHERE key = 'lane:v1:example/repo:bad'")
            self.connection.execute(bad, ("lane:v1:example/other:bad", clock()))
        fix = builds.lane_fix_state(self.connection, run["branch"])
        self.assertEqual((fix["allowed"], fix["assignment"]), (True, run["assignment"]), fix)
        status, _, body = self.fix(fix)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.lane_entry("e-failed")["status"], "cancelled")

    def test_retry_without_a_finding_still_refuses_a_lane_failed_run_and_writes_nothing(self):
        run = self.lane_failed([self.ready()])
        fix = builds.lane_fix_state(self.connection, run["branch"])
        for extra in ({}, {"finding": "a plain Send to builder"}):
            status, _, body = self.post("/api/queue/builds/retry", {"assignment": fix["assignment"],
                                                                   "revision": fix["revision"], **extra})
            self.assertEqual(status, 400, body)
            self.assertIn("Retry takes a blocked or cancelled one", body["error"])
            self.assertEqual(self.queued(), 0)

    def test_fix_is_off_for_a_branch_no_build_run_holds_and_for_a_lead_that_moved_on(self):
        self.assertEqual(builds.lane_fix_state(self.connection, "build/night"),
                         {"allowed": False, "reason": "no build run holds branch build/night; send the finding to the "
                                                      "session that enqueued it"})
        items = [self.ready()]
        run = self.lane_failed(items)
        transition(self.connection, items[0], "planning", who="operator", reason="moved on")
        fix = builds.lane_fix_state(self.connection, run["branch"])
        self.assertEqual((fix["allowed"], fix["reason"]),
                         (False, "its lead item moved on since the lane failed; build it from Tasks"))
        status, _, body = self.fix(fix)
        self.assertEqual(status, 400, body)
        self.assertEqual((self.queued(), self.connection.execute(
            "SELECT COUNT(*) FROM note WHERE body LIKE 'Send to builder:%'").fetchone()[0]), (0, 0))


class BlockedBuildRows(Fixture):
    """system sd:8651: Queue lists each blocked build assignment `sd today` lists, with its reason, Cancel and Retry.

    The Builds panel reads runs, so a member with no run of its own, or an assignment that never ran, was no row."""

    claimed = BuildsPanel.claimed
    blocked = BuildsPanel.blocked

    def runner(self):
        status, _, body = self.request("/api/queue/runner", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200, body)
        return {row["item"]: row for row in json.loads(body)["blocked"]}

    def test_each_blocked_build_assignment_is_a_row_with_its_reason_cancel_and_retry(self):
        from sd_db import today

        items = [self.ready(), self.ready()]
        run = self.blocked(items)
        lone = self.ready()
        build.queue(self.connection, [lone], who="fixture")
        with self.connection:  # what the claim writes when a satellite's dispatcher does not beat
            self.connection.execute("UPDATE assignment SET status = 'blocked', result = ? WHERE item = ? AND scope = 'build'",
                                    (f"item {lone}'s lane runs on host-b, and no beat is under 600 s old; not built", lone))
        rows = self.runner()
        listed = sorted(row["id"] for row in today.queues(self.connection)
                        if row["queue"] == "build" and row["status"] == "blocked")
        self.assertEqual(sorted(rows), listed)
        self.assertEqual(sorted(rows), sorted(items + [lone]))
        lead, member, alone = rows[items[0]], rows[items[1]], rows[lone]
        for row in (lead, member):
            self.assertEqual((row["retry"]["allowed"], row["retry"]["assignment"]), (True, run["assignment"]), row)
            self.assertTrue(row["why"], row)
        self.assertTrue(alone["why"].startswith(f"item {lone}'s lane runs on host-b"), alone)
        self.assertEqual((alone["retry"]["allowed"], alone["retry"]["reason"]),
                         (False, "it never ran; Cancel clears it, or build the item again from Tasks"))
        self.assertTrue(all(row["cancel"]["allowed"] for row in rows.values()), rows)
        status, _, body = self.post(f"/api/assignments/{alone['id']}/cancel", {"revision": alone["cancel"]["revision"]})
        self.assertEqual(status, 200, body)
        status, _, body = self.post("/api/queue/builds/retry", {"assignment": lead["retry"]["assignment"],
                                                               "revision": lead["retry"]["revision"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.runner(), {})


class AutoPickup(Fixture):
    """pack sd:6097: the Builds panel shows auto-pickup and its On and Off write it, bound to the value read."""

    def test_on_and_off_write_the_setting_against_the_value_read(self):
        state = builds.panel(self.connection, now=clock())["pickup"]
        self.assertEqual((state["on"], state["until"], state["error"]), (False, None, ""))
        status, _, body = self.post("/api/queue/builds/pickup", {"on": True, "until": None})
        self.assertEqual((status, body["on"], body["until"]), (200, True, build.morning()), body)
        self.assertEqual(len(self.kicks), 1, "On starts a tick now")
        status, _, body = self.post("/api/queue/builds/pickup", {"on": False, "until": None})
        self.assertEqual(status, 409, body)
        self.assertTrue(build.pickup_state(self.connection)["on"], "a stale Off writes nothing")
        status, _, body = self.post("/api/queue/builds/pickup", {"on": False, "until": build.morning()})
        self.assertEqual((status, body["on"]), (200, False), body)
        for payload in ({"on": "yes", "until": None}, {"on": True}):
            self.assertEqual(self.post("/api/queue/builds/pickup", payload)[0], 400, payload)
