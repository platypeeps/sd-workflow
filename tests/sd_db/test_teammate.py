"""Teammate runs: a lead's in-session builder on the build run's rows (pack sd:7221).

The failure table in `docs/work/2026-11-02-builder-heartbeat/design.md` names
these tests. The merge guard's and the backup's are in `test_ship.py` and
`test_build.py`, the CLI's in `tests/test_sd_operations.py`, and the
readers' in the dashboard's `test_build_panel.py` and `test_now_screen.py`.
"""
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

from sd_db import (
    build,
    operations,
    progress,
    reads,
    reporting,
    retention,
    runner,
    ship,
    skills_catalog,
    teammate,
    today,
    workflow,
)
from sd_db.database import connect
from sd_db.migrate import initialise
from sd_db.writes import (
    create_assignment,
    create_item,
    record_state,
    transition,
    upsert_repo,
)
from sd_db.writes import now as clock

from .test_build import later

build_run = import_module("sd_db.build_run")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
sd_lane = import_module("sd_lane")
A, B = "/fixture/a", "/fixture/b"
REMOTE = "git@github.com:Fixture/Repo.git"


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / "sd.db"
        initialise(self.database)
        self.db = connect(self.database)
        self.addCleanup(self.db.close)
        for repo in (A, B):
            upsert_repo(self.db, repo, remote=REMOTE if repo == A else "/fixture/remote-b", managed=1)

    def item(self, repo=A, status="in_progress"):
        return create_item(self.db, kind="task", title="Teammate builds me", repo=repo, status=status)

    def start(self, items, budget=90):
        return teammate.start(self.db, items, worktree="/w/pack-1", branch="feat/x", budget_minutes=budget,
                              owner="lead")

    def run_row(self, ident):
        return dict(self.db.execute("SELECT * FROM runner_run WHERE id = ?", (ident,)).fetchone())

    def assignments(self, ident):
        lead = self.run_row(ident)["assignment"]
        return [tuple(row) for row in self.db.execute(
            "SELECT item, status, scope, budget_minutes, parent IS NULL FROM assignment WHERE id = ? OR parent = ? "
            "ORDER BY id", (lead, lead))]


class Start(Case):
    def test_a_start_writes_one_run_and_one_assignment_per_item_lead_first(self):
        first, second = self.item(), self.item()
        run = self.start([first, second])
        self.assertEqual([member["item"] for member in run["members"]], [first, second])
        self.assertEqual(self.assignments(run["id"]), [(first, "in_session", "teammate", 90, 1),
                                                        (second, "in_session", "teammate", 90, 0)])
        row = self.run_row(run["id"])
        self.assertEqual((row["repo"], row["branch"], row["work_path"], row["step"], row["outcome"], row["released_at"]),
                         (A, "feat/x", "/w/pack-1", "building", None, None))
        self.assertEqual(run["due"], later(row["created_at"], 90 * 60))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM runner_lease").fetchone()[0], 0)

    def test_a_start_refuses_and_writes_nothing(self):
        held, done, other = self.item(), self.item(status="done"), self.item(repo=B)
        self.start([held])
        record = self.item()
        cases = {"held": ([held], "held by teammate run"), "done": ([done], "is done"),
                 "two repos": ([record, other], "share one repository"), "none": ([], "distinct items"),
                 "twice": ([record, record], "distinct items"), "no item": ([99999], "no item 99999")}
        before = self.db.execute("SELECT COUNT(*) FROM assignment").fetchone()[0]
        for case, (items, why) in cases.items():
            with self.subTest(case=case), self.assertRaisesRegex(teammate.TeammateRefused, why):
                self.start(items)
        with self.assertRaisesRegex(teammate.TeammateRefused, "positive number"):
            self.start([record], budget=0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assignment").fetchone()[0], before)

    def test_a_start_refuses_an_item_a_build_holds(self):
        item = self.item(status="ready")
        build.queue(self.db, [item], who="operator")
        with self.assertRaisesRegex(teammate.TeammateRefused, "queued build assignment"):
            self.start([item])

    def test_a_start_refuses_an_item_a_retained_build_run_holds(self):
        """A blocked build whose run is not released still owns its items (`build._held`), bundle members too."""
        lead_item, member_item = self.item(status="ready"), self.item(status="ready")
        lead = create_assignment(self.db, item=lead_item, role="author", status="blocked", lane="parallel")
        member = create_assignment(self.db, item=member_item, role="author", status="blocked", lane="parallel",
                                   parent=lead)
        self.db.execute("UPDATE assignment SET scope = 'build' WHERE id IN (?, ?)", (lead, member))
        self.db.execute("INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, "
                        "start_step, step, outcome, created_at, updated_at) VALUES ('kept', ?, 1, ?, 'build/sd-1', "
                        "'fixture', '/w/a-build-1', '/w/a-build-1', 'claimed', 'building', 'interrupted', ?, ?)", (lead, A, clock(), clock()))
        before = self.db.execute("SELECT COUNT(*) FROM assignment").fetchone()[0]
        for item in (lead_item, member_item):
            with self.subTest(item=item), self.assertRaisesRegex(teammate.TeammateRefused,
                                                                 f"item {item} is held by build run kept"):
                self.start([self.item(status="ready"), item])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assignment WHERE scope = 'teammate'").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assignment").fetchone()[0], before)

    def lane(self, ident, body):
        """One lane entry as `sd_lane.store` writes it: the newest `checkpoint` row of its key."""
        self.db.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                        (f"lane:v1:Fixture/Repo:{ident}", clock(), body if isinstance(body, str) else json.dumps(body)))

    def test_a_start_refuses_an_item_a_lane_entry_still_delivers(self):
        """pack sd:7288: a pending or running entry delivers its item and its closes; a merged delivering entry
        owes each a close until a note records `delivered at` its merge commit. Other entries hold nothing."""
        merge = "a" * 40
        pending, running, closed_by, owed, owed_close = (self.item(status="ready") for _ in range(5))
        paid, associated, failed = (self.item(status="ready") for _ in range(3))
        self.lane("e1", {"id": "e1", "item": pending, "status": "pending", "claim": "deliver", "closes": []})
        self.lane("e2", {"id": "e2", "item": running, "status": "running", "claim": "deliver", "closes": [closed_by]})
        self.lane("e3", {"id": "e3", "item": owed, "status": "merged", "claim": "deliver", "closes": [owed_close],
                         "merge_commit": merge})
        self.lane("e4", {"id": "e4", "item": paid, "status": "merged", "claim": "deliver", "closes": [],
                         "merge_commit": merge})
        transition(self.db, paid, "done", who="sd-ship", reason=f"delivered at {merge} on origin/main")
        transition(self.db, paid, "ready", who="operator", reason="reopened")
        self.lane("e5", {"id": "e5", "item": associated, "status": "merged", "claim": "associate-only", "closes": [],
                         "merge_commit": merge})
        self.lane("e6", {"id": "e6", "item": failed, "status": "failed", "claim": "deliver", "closes": []})
        before = self.db.execute("SELECT COUNT(*) FROM assignment").fetchone()[0]
        cases = {pending: "pending lane entry e1", running: "running lane entry e2",
                 closed_by: "running lane entry e2", owed: f"close owed by merged lane entry e3 of sd:{owed} at {merge}",
                 owed_close: f"close owed by merged lane entry e3 of sd:{owed} at {merge}"}
        for item, why in cases.items():
            with self.subTest(item=item), self.assertRaisesRegex(teammate.TeammateRefused, f"item {item}.*{why}"):
                self.start([self.item(status="ready"), item])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assignment").fetchone()[0], before)
        self.start([paid, associated, failed])

    def test_a_start_refuses_an_item_a_prepared_lane_entry_delivers(self):
        """A prepared entry waits for a hand merge: still a delivery in flight, item and closes (pack sd:7288)."""
        item, close = self.item(status="ready"), self.item(status="ready")
        self.lane("e7", {"id": "e7", "item": item, "status": "prepared", "claim": "deliver", "closes": [close]})
        for number in (item, close):
            with self.subTest(item=number), self.assertRaisesRegex(
                    teammate.TeammateRefused, f"item {number} is delivered by prepared lane entry e7 of sd:{item}"):
                self.start([number])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assignment WHERE scope = 'teammate'").fetchone()[0], 0)

    def test_a_start_refuses_a_pending_entrys_explicit_and_cited_closes_in_any_position(self):
        """Enqueue stores each cited close in `closes`; a start checks every requested item (pack sd:7288)."""
        item, explicit, cited = (self.item(status="ready") for _ in range(3))
        self.lane("e8", {"id": "e8", "item": item, "status": "pending", "claim": "deliver",
                         "closes": [explicit, cited], "cited": [{"item": cited, "source": "commit abc"}]})
        for number in (item, explicit, cited):
            for items in ([number], [self.item(status="ready"), number]):
                with self.subTest(item=number, lead=items[0] == number), self.assertRaisesRegex(
                        teammate.TeammateRefused, f"item {number} is delivered by pending lane entry e8 of sd:{item}"):
                    self.start(items)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assignment WHERE scope = 'teammate'").fetchone()[0], 0)

    def test_a_start_refuses_an_item_a_failed_entry_may_have_published(self):
        """pack sd:7295: a failed delivering entry whose ship receipt reached `merge_dispatch` may have published, so
        it owes the close as a merged entry does; one that never dispatched holds nothing."""
        merge = "b" * 40
        dispatched, merged, paid, unread, unsent, associated = (self.item(status="ready") for _ in range(6))

        def failed(ident, item, receipt, claim="deliver"):
            branch = f"feat/{ident}"
            self.lane(ident, {"id": ident, "item": item, "status": "failed", "step": "merge", "claim": claim,
                              "closes": [], "repository": "Fixture/Repo", "branch": branch})
            if receipt is not None:
                self.db.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                                (ship.receipt_key("Fixture/Repo", branch, item), clock(),
                                 receipt if isinstance(receipt, str) else json.dumps({"protocol": 1, **receipt})))
        failed("f1", dispatched, {"phase": "merge_dispatch"})
        failed("f2", merged, {"phase": "merged", "merge_commit": merge})
        failed("f3", paid, {"phase": "merged", "merge_commit": merge})
        transition(self.db, paid, "done", who="sd-ship", reason=f"delivered at {merge} on origin/main")
        transition(self.db, paid, "ready", who="operator", reason="reopened")
        failed("f4", unread, "{not json")
        failed("f5", unsent, {"phase": "ready_to_send"})
        failed("f6", associated, {"phase": "merged", "merge_commit": merge}, claim="associate-only")
        cases = {dispatched: "close owed by failed lane entry f1 of sd:{n} at an unrecorded commit",
                 merged: f"close owed by failed lane entry f2 of sd:{{n}} at {merge}",
                 unread: "failed lane entry f4 of sd:{n} cannot be read"}
        for item, why in cases.items():
            with self.subTest(item=item), self.assertRaisesRegex(teammate.TeammateRefused,
                                                                 f"item {item}.*{why.format(n=item)}"):
                self.start([item])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assignment WHERE scope = 'teammate'").fetchone()[0], 0)
        self.start([paid, unsent, associated])

    def test_a_start_refuses_when_a_lane_entry_is_unreadable(self):
        """An entry that does not parse may deliver the item: unknown is not "no" (pack sd:7288)."""
        self.lane("bad", "{not json")
        with self.assertRaisesRegex(teammate.TeammateRefused, "lane entry lane:v1:Fixture/Repo:bad is unreadable"):
            self.start([self.item(status="ready")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assignment WHERE scope = 'teammate'").fetchone()[0], 0)

    def test_a_restore_reimport_holds_a_start(self):
        record_state(self.db, "restore", key="fixture", body={})
        with self.assertRaisesRegex(teammate.TeammateRefused, "restore"):
            self.start([self.item()])

    def test_the_session_still_moves_its_items(self):
        """A running assignment refuses every status write (criterion 7.19); a teammate's does not."""
        item = self.item(status="ready")
        self.start([item])
        workflow.change_status(self.db, item, "in_progress", who="builder")
        self.assertEqual(workflow.item_state(self.db, item)["item"]["status"], "in_progress")


class SecondBranch(Case):
    """pack sd:7737: one open bundle branch per repository; a second needs `into` or a reason."""

    def second(self, items, **extra):
        return teammate.start(self.db, items, worktree="/w/pack-2", branch="feat/y", budget_minutes=90,
                              owner="lead", **extra)

    def notes(self, item):
        return [row[0] for row in self.db.execute("SELECT body FROM note WHERE item = ? AND kind = 'comment' "
                                                  "ORDER BY id", (item,))]

    def test_a_second_branch_of_the_repo_is_refused_naming_the_open_branch_and_worktree(self):
        self.start([self.item()])
        later = self.item()
        before = self.db.execute("SELECT COUNT(*) FROM assignment").fetchone()[0]
        with self.assertRaisesRegex(teammate.TeammateRefused, r"open branch feat/x in /w/pack-1 .*--reason state-move"):
            self.second([later])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assignment").fetchone()[0], before)
        with self.assertRaisesRegex(teammate.TeammateRefused, "state-move, in-lane or p1"):
            self.second([later], reason="because")

    def test_a_p1_item_opens_a_second_branch_alone_or_with_its_area(self):
        """sd:9700, operator ruling #64744: `--reason p1` opens a second branch for a P1 item and its area's."""
        self.start([self.item()])
        urgent = create_item(self.db, kind="task", title="Urgent", repo=A, status="in_progress", priority=1,
                             fields={"area": "home"})
        kin = create_item(self.db, kind="task", title="Kin", repo=A, status="in_progress", priority=3,
                          fields={"area": "home"})
        ordinary = self.item()
        for items in ([ordinary], [urgent, ordinary]):
            with self.assertRaisesRegex(teammate.TeammateRefused, "P1"):
                self.second(items, reason="p1")
        self.second([urgent, kin], reason="p1")
        self.assertIn("beside feat/x: p1", self.notes(kin)[-2])

    def test_a_headless_build_run_holds_the_repositorys_one_branch(self):
        """sd:8821: a live build run of the repository refuses a teammate start on another branch."""
        built = self.item(status="ready")
        build.queue(self.db, [built], who="operator")
        run = build.claim(self.db, owner="fixture", root=self.root / "worktrees", load=1.0, load_cap=40.0,
                          interactive=[])
        self.assertIsNotNone(run)
        branch = self.db.execute("SELECT branch FROM runner_run").fetchone()["branch"]
        later = self.item()
        with self.assertRaisesRegex(teammate.TeammateRefused, rf"open branch {branch} .*\(build run \w+\)"):
            self.second([later])
        self.second([later], reason="in-lane")
        self.assertIn(f"beside {branch}: in-lane", self.notes(later)[-2])

    def test_the_same_branch_another_repo_and_an_ended_run_are_not_a_second_branch(self):
        runs = [self.start([self.item()]), self.start([self.item()])]
        self.second([self.item(repo=B)])
        for run in runs:
            teammate.end(self.db, run["members"][0]["item"])
        self.second([self.item()])

    def test_into_the_open_branch_or_a_reason_allows_it_and_the_reason_is_a_note(self):
        self.start([self.item()])
        side, moving = self.item(), self.item()
        self.second([side], into="feat/x")
        self.assertIn("merges into feat/x", self.notes(side)[-1])
        teammate.start(self.db, [moving], worktree="/w/pack-3", branch="feat/z", budget_minutes=90, owner="lead",
                       reason="state-move")
        self.assertIn("Second open branch feat/z of /fixture/a beside feat/x, feat/y: state-move", self.notes(moving)[-2])
        with self.assertRaisesRegex(teammate.TeammateRefused, "open branch"):
            teammate.start(self.db, [self.item()], worktree="/w/pack-4", branch="feat/w", budget_minutes=90,
                           owner="lead", into="feat/q")

    def test_side_branches_into_one_bundle_are_not_beside_each_other(self):
        """pack sd:7748: a second side branch into the bundle starts; one into another bundle still refuses."""
        bundle = self.start([self.item()])
        self.second([self.item()], into="feat/x")
        teammate.start(self.db, [self.item()], worktree="/w/pack-3", branch="feat/z", budget_minutes=90,
                       owner="lead", into="feat/x")
        teammate.end(self.db, bundle["members"][0]["item"])
        with self.assertRaisesRegex(teammate.TeammateRefused,
                                    r"open branch feat/[yz] in /w/pack-[23] .*side branch of feat/x.*--into feat/x "):
            teammate.start(self.db, [self.item()], worktree="/w/pack-5", branch="feat/w", budget_minutes=90,
                           owner="lead", into="feat/q")
        teammate.start(self.db, [self.item()], worktree="/w/pack-4", branch="feat/x", budget_minutes=90,
                       owner="lead")
        with self.assertRaisesRegex(teammate.TeammateRefused, "--into names a branch"):
            self.second([self.item()], into="feat/x extra")

    def test_a_run_without_its_start_note_is_its_own_bundle(self):
        """pack sd:7748: the side branch's target lives in its start note; a missing note refuses, not passes."""
        side = self.second([self.item()], into="feat/x")
        self.db.execute("DELETE FROM note WHERE item = ? AND body LIKE 'Teammate run %'", (side["members"][0]["item"],))
        with self.assertRaisesRegex(teammate.TeammateRefused, r"open branch feat/y in /w/pack-2 \(teammate run \w+\)"):
            teammate.start(self.db, [self.item()], worktree="/w/pack-3", branch="feat/z", budget_minutes=90,
                           owner="lead", into="feat/x")


class Ownership(Case):
    """An open teammate run owns its items against every pickup; the session's own writes still pass.

    The design's failure table, "Ownership readers", names each reader and
    whether it reads `in_session`; these tests pin each decision.
    """

    def held(self, status="ready", **fields):
        item = self.item(status=status)
        if fields:
            self.db.execute("UPDATE item SET " + ", ".join(f"{key} = ?" for key in fields) + " WHERE id = ?",
                            (*fields.values(), item))
        return item, self.start([item])

    def test_auto_pickup_and_the_build_press_skip_a_teammates_item(self):
        item, _ = self.held()
        free = self.item(status="ready")
        self.assertEqual([row["id"] for row in build.eligible(self.db)], [free])
        with self.assertRaisesRegex(build.BuildRefused, f"item {item} has in_session assignment"):
            build.queue(self.db, [item], who="operator")

    def test_a_queued_build_of_a_teammates_item_is_not_claimed(self):
        item, _ = self.held()
        queued = create_assignment(self.db, item=item, role="author", status="queued", lane="parallel")
        self.db.execute("UPDATE assignment SET scope = 'build', queued_at = ? WHERE id = ?", (clock(), queued))
        self.assertIsNone(build.claim(self.db, owner="fixture", root=self.root / "worktrees", load=1.0,
                                      load_cap=40.0, interactive=[]))
        row = self.db.execute("SELECT status, result FROM assignment WHERE id = ?", (queued,)).fetchone()
        self.assertEqual(row["status"], "blocked")
        self.assertIn("a teammate builds it", row["result"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM runner_run").fetchone()[0], 1, "no second builder")

    def test_the_runner_queue_and_its_delivery_skip_a_teammates_item(self):
        item, _ = self.held(branch="feat/x")
        with self.assertRaisesRegex(runner.RunnerRefused, f"item {item} already has queued work"):
            runner.enqueue(self.db, [item], who="operator")
        sent, _ = self.held(status="ready_to_send", branch="feat/x")
        self.assertNotIn(sent, [row["id"] for row in runner.delivery_candidates(self.db)])

    def test_unscheduled_and_the_report_clean_skip_a_teammates_item(self):
        item, _ = self.held()
        self.assertNotIn(item, [row["id"] for row in today.unscheduled(self.db)])
        report = create_item(self.db, kind="report", title="A clean report", repo=A, status="planning",
                             fields={"attention": 0})
        self.db.execute("UPDATE item SET created_at = '2026-01-01T00:00:00Z' WHERE id = ?", (report,))
        self.start([report])
        found = reporting.clean_reports(self.db, before=reporting.cutoff("2026-02-01"))
        self.assertEqual([row["id"] for row in found["selected"]], [])
        self.assertEqual(found["declined"], [{"id": report, "why": "work on it is queued, running or ending"}])

    def test_retention_leaves_a_clean_report_a_runner_or_teammate_owns(self):
        """pack sd:7284: `clean_candidates` reads `runner.OWNING`, as `clean_reports` does; an unowned report still settles."""
        def report():
            ident = create_item(self.db, kind="report", title="A clean report", repo=A, status="planning",
                                fields={"attention": 0})
            self.db.execute("UPDATE item SET created_at = '2026-01-01T00:00:00Z' WHERE id = ?", (ident,))
            return ident
        teamed, running, free = report(), report(), report()
        self.start([teamed])
        create_assignment(self.db, item=running, role="author", status="running")
        before = reporting.cutoff("2026-02-01")
        self.assertEqual(reporting.clean_candidates(self.db, before=before), [free])
        self.assertEqual(retention.settle_clean_reports(self.db, now=datetime(2026, 2, 1, tzinfo=UTC)), 1)
        status = {row["id"]: row["status"] for row in self.db.execute(
            "SELECT id, status FROM item WHERE id IN (?, ?, ?)", (teamed, running, free))}
        self.assertEqual(status, {teamed: "planning", running: "planning", free: "done"})

    def test_a_skill_review_a_teammate_holds_takes_no_proposals(self):
        review = create_item(self.db, kind="task", title="Review a skill", repo=A, status="ready",
                             source="skill-request", fields={"skill_review": {"action": "review", "review_result": "x"}})
        self.start([self.item(status="ready"), review])  # a member: the lead's item has the run's row
        revision = workflow.item_state(self.db, review)["revision"]
        with self.assertRaisesRegex(workflow.WorkflowError, "wait for the reviewer"):
            skills_catalog.apply_proposals(self.db, review, [1], expected_revision=revision, who="operator")

    def test_the_lane_keeps_a_teammates_worktree(self):
        item, _ = self.held()
        self.db.execute("UPDATE item SET status = 'done' WHERE id = ?", (item,))
        self.assertIn(f"of item {item} is active", sd_lane.builder_busy(self.db, self.root / "w", item))

    def test_the_sessions_own_status_cancel_and_edit_writes_pass(self):
        """Criterion 7.19 guards the runner's rows; a teammate's are the session's, so it reads `ACTIVE_ASSIGNMENTS`."""
        item, _ = self.held()
        self.assertIn("in_progress", workflow.allowed_statuses(self.db, item))
        self.assertEqual(progress.task_guard(self.db, workflow.item_state(self.db, item)["item"]), "")
        workflow._refuse_active_assignment(self.db, item)
        self.assertNotIn(teammate.STATUS, workflow.ACTIVE_ASSIGNMENTS)
        self.assertEqual([row["item"] for row in reads.assignment_ledger(self.db, live=True)], [],
                         "the Management page lists the lane's order; the Queue view lists teammate runs")


class Beat(Case):
    def test_a_beat_by_any_item_refreshes_the_run(self):
        first, second = self.item(), self.item()
        run = self.start([first, second])
        self.db.execute("UPDATE runner_run SET heartbeat_at = '2026-11-02T00:00:00+00:00' WHERE id = ?", (run["id"],))
        self.assertEqual(teammate.beat(self.db, second), run["id"])
        self.assertGreater(self.run_row(run["id"])["heartbeat_at"], "2026-11-02T00:00:00+00:00")

    def test_a_beat_with_no_open_run_refuses(self):
        item = self.item()
        with self.assertRaisesRegex(teammate.TeammateRefused, f"no open teammate run holds item {item}"):
            teammate.beat(self.db, item)
        run = self.start([item])
        teammate.end(self.db, item)
        with self.assertRaisesRegex(teammate.TeammateRefused, "no open teammate run"):
            teammate.beat(self.db, item)
        self.assertEqual(self.run_row(run["id"])["outcome"], "done")


class End(Case):
    def test_an_end_releases_the_run_and_leaves_the_items_as_they_are(self):
        first, second = self.item(), self.item(status="blocked")
        run = self.start([first, second])
        self.assertEqual(teammate.end(self.db, first, reason="reported"), run["id"])
        row = self.run_row(run["id"])
        self.assertEqual((row["outcome"], row["detail"]), ("done", "reported"))
        self.assertIsNotNone(row["released_at"])
        self.assertEqual([status for _, status, *_ in self.assignments(run["id"])], ["done", "done"])
        self.assertEqual([workflow.item_state(self.db, item)["item"]["status"] for item in (first, second)],
                         ["in_progress", "blocked"])
        self.assertEqual(teammate.open_runs(self.db), [])
        self.start([first])  # ended, the items take a new run


class Release(Case):
    def test_a_quiet_run_lives_past_the_build_stale_limit(self):
        """The build reap ends a run after `build.STALE_SECONDS`; a builder beats only at checkpoints."""
        run = self.start([self.item()])
        self.db.execute("UPDATE runner_run SET heartbeat_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (run["id"],))
        self.assertEqual(self.tick()["reaped"], [])
        self.assertIsNone(self.run_row(run["id"])["outcome"])

    def test_a_run_silent_past_its_budget_and_grace_stays_open_and_reads_overdue(self):
        """A builder beats only at commits and reports, so silence is no proof it stopped: expiry ends nothing."""
        item = self.item(status="ready")
        run = self.start([item])
        edge = later(run["created_at"], (90 + teammate.GRACE_MINUTES) * 60)
        self.assertEqual(self.tick()["reaped"], [])
        row = self.run_row(run["id"])
        self.assertEqual((row["outcome"], row["released_at"]), (None, None))
        self.assertEqual([status for _, status, *_ in self.assignments(run["id"])], ["in_session"])
        open_run = teammate.open_runs(self.db)[0]
        self.assertEqual(teammate.overdue(open_run, later(edge, -1)), "")
        self.assertIn("overdue: no beat for 120 min", teammate.overdue(open_run, edge))
        with self.assertRaisesRegex(build.BuildRefused, f"item {item} has in_session assignment"):
            build.queue(self.db, [item], who="operator")

    def test_a_beat_restarts_the_silence(self):
        run = self.start([self.item()])
        beat = later(run["created_at"], 100 * 60)
        self.db.execute("UPDATE runner_run SET heartbeat_at = ? WHERE id = ?", (beat, run["id"]))
        open_run = teammate.open_runs(self.db)[0]
        self.assertEqual(teammate.overdue(open_run, later(run["created_at"], 150 * 60)), "")
        self.assertNotEqual(teammate.overdue(open_run, later(beat, 120 * 60)), "")

    def test_a_beat_stamped_far_ahead_reads_overdue_and_a_small_lead_does_not(self):
        """sd:7986: a beat from a clock far ahead never read overdue; `build._lead_age` rules."""
        run = self.start([self.item()])
        open_run = teammate.open_runs(self.db)[0]
        now = later(run["created_at"], 10 * 60)
        open_run["heartbeat_at"] = later(now, build.STALE_SECONDS + 1)
        self.assertIn("ahead of this clock", teammate.overdue(open_run, now))
        open_run["heartbeat_at"] = later(now, 60)
        self.assertEqual(teammate.overdue(open_run, now), "")

    def test_a_run_whose_items_are_all_done_stays_held_until_its_lead_ends_it(self):
        """An item done is no proof its builder stopped (round 4): only the lead's `--end` releases the run."""
        first, second = self.item(), self.item()
        run = self.start([first, second])
        self.db.execute("UPDATE item SET status = 'done' WHERE id IN (?, ?)", (first, second))
        result = self.tick()
        self.assertNotIn(run["id"], [entry["run"] for entry in result["reaped"]])
        self.assertEqual(result["spawned"], [])
        self.assertEqual(teammate.holding(self.db, second), {"id": run["id"]})
        self.assertEqual((self.run_row(run["id"])["outcome"], self.run_row(run["id"])["released_at"]), (None, None))
        self.assertEqual([status for _, status, *_ in self.assignments(run["id"])], ["in_session", "in_session"])
        teammate.end(self.db, first)
        self.assertIsNone(teammate.holding(self.db, second))

    def test_an_assignment_cancel_does_not_release_a_teammate_run(self):
        """A cancelled row would leave `runner.OWNING` while the builder still writes: only `end` releases."""
        item = self.item()
        run = self.start([item])
        state = operations.assignment_state(self.db, run["assignment"])
        self.assertFalse(state["capabilities"]["cancel"]["allowed"])
        with self.assertRaises(workflow.WorkflowError):
            operations.cancel_assignment(self.db, run["assignment"], expected_revision=state["revision"], who="op")
        self.assertEqual(teammate.holding(self.db, item), {"id": run["id"]})

    def tick(self):
        return build_run.dispatch(self.db, owner="fixture", load=None, load_cap=None, interactive=None,
                                  spawn=lambda ident: None, lane=lambda row, items: None, why="fixture")

if __name__ == "__main__":
    unittest.main()
