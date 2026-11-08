"""The Build queue, its claim and its reap on the runner tables (pack sd:6093).

The design's failure table names most of these tests: system
`docs/work/2026-10-24-build-dispatcher/design.md`.
"""
import contextlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest import mock

from sd_db import build, draft, operations, recovery, remote, runner, workflow
from sd_db.database import connect
from sd_db.errors import BackupError
from sd_db.migrate import initialise
from sd_db.workflow import item_state
from sd_db.writes import (
    create_assignment,
    create_item,
    read_settings,
    record_state,
    set_setting,
    transition,
    upsert_repo,
)

#: `sd_db.backup` the module: the package exports the function under that name.
backup = import_module("sd_db.backup")
A, B = "/fixture/a", "/fixture/b"
START = "Sat Oct 24 09:00:00 2026"
IDLE: dict[str, Any] = {"load": 1.0, "load_cap": 40.0, "interactive": []}


def later(stamp: str, seconds: int) -> str:
    return (datetime.fromisoformat(stamp) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


class Probe:
    """Every pid answers `alive` and `started`, every group `group`; `pids` maps a pid to its own (alive, started)."""

    def __init__(self, alive=True, started="Sat Oct 24 09:00:00 2026", group=False, pids=None):
        self.answer, self.start, self.members, self.pids = alive, started, group, pids or {}

    def alive(self, pid):
        return self.pids.get(pid, (self.answer, self.start))[0]

    def started(self, pid):
        return self.pids.get(pid, (self.answer, self.start))[1]

    def group(self, pgid):
        return self.members


class Kill:
    """Records each group kill; `gone` is what each answers, or a map of group to answer (True when unnamed)."""

    def __init__(self, connection, gone=True):
        self.connection, self.gone, self.calls = connection, gone, []

    def __call__(self, pgid):
        open_runs = self.connection.execute("SELECT COUNT(*) FROM runner_run WHERE released_at IS NULL").fetchone()[0]
        self.calls.append((pgid, open_runs))
        return self.gone.get(pgid, True) if isinstance(self.gone, dict) else self.gone


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # The fixtures name a satellite lane `work`, which a real machine may be called too: each test runs as the
        # hub `hub` unless it says otherwise (review round 3). A real `hub.json` in this HOME would make it a
        # satellite under the wire harness, so it reads none (pack sd:7955).
        for pinned in (mock.patch.object(build, "this_host", return_value="hub"),
                       mock.patch("sd_db.hub.configured", return_value=False)):
            pinned.start()
            self.addCleanup(pinned.stop)
        self.database = self.root / "sd.db"
        initialise(self.database)
        self.db = connect(self.database)
        self.addCleanup(self.db.close)
        for repo in (A, B):
            upsert_repo(self.db, repo, remote=f"/fixture/remote{repo[-2:]}", managed=1)

    def item(self, repo=A, status="ready", kind="task", priority=None, fields=None):
        return create_item(self.db, kind=kind, title="Build me", repo=repo, status=status, priority=priority,
                           fields=fields)

    def claim(self, db=None, **inputs):
        return build.claim(db or self.db, owner="fixture", root=self.root / "worktrees", **{**IDLE, **inputs})

    def count(self, sql, *args):
        return self.db.execute(sql, args).fetchone()[0]

    def statuses(self, items):
        return [item_state(self.db, item)["item"]["status"] for item in items]


class Queue(Case):
    def test_queue_refuses_whole_selection(self):
        """Failure table, Press: one refusal refuses every item, and nothing is written."""
        ready, planning = self.item(), self.item(status="planning")
        busy = self.item()
        build.queue(self.db, [busy], who="operator")
        upsert_repo(self.db, "/fixture/off", remote="/fixture/r-off", managed=0)
        upsert_repo(self.db, "/fixture/gh", remote="https://github.com/owner/third-party.git", managed=0)
        upsert_repo(self.db, "/fixture/none", managed=1)
        stale = self.item()
        revision = item_state(self.db, stale)["revision"]
        self.db.execute("UPDATE item SET title = 'moved' WHERE id = ?", (stale,))
        cases = {
            "is planning; a build takes a ready item": ([ready, planning], None),
            "has queued assignment": ([ready, busy], None),
            "is not managed and its remote is not on GitHub": ([ready, self.item(repo="/fixture/off")], None),
            "takes task items only": ([ready, self.item(repo="/fixture/gh", kind="report")], None),
            "has no remote": ([ready, self.item(repo="/fixture/none")], None),
            "is a followup item": ([ready, self.item(kind="followup")], None),
            "needs a registered repository": ([ready, self.item(repo=None)], None),
            "changed; refresh the selection": ([stale], {stale: revision}),
        }
        before = self.count("SELECT COUNT(*) FROM assignment")
        for reason, (items, revisions) in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(build.BuildRefused, reason):
                    build.queue(self.db, items, who="operator", expected_revisions=revisions)
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment"), before)
        self.assertEqual(self.statuses([ready]), ["ready"])

    def test_press_queues_a_satellite_lane_item(self):
        """Pack sd:7933, failure table Press: a lane on a satellite takes a press; its host builds it."""
        upsert_repo(self.db, "/fixture/sat", remote="/fixture/r-sat", managed=1)
        self.db.execute("UPDATE repo SET lane_host = 'work' WHERE path = '/fixture/sat'")
        satellite = self.item(repo="/fixture/sat")
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={})
        queued = build.queue(self.db, [self.item(), satellite], who="operator")
        self.assertEqual([row["status"] for row in queued], ["queued", "queued"])

    def test_a_press_for_a_satellite_whose_dispatcher_does_not_beat_refuses_and_locks_nothing(self):
        """Review round 2: with no fresh build-dispatch@<host> beat nothing there claims the press, and its items
        would sit locked; the press refuses, naming the host, under the reap's own STALE_SECONDS."""
        upsert_repo(self.db, "/fixture/sat", remote="/fixture/r-sat", managed=1)
        self.db.execute("UPDATE repo SET lane_host = 'work' WHERE path = '/fixture/sat'")
        hub, satellite = self.item(), self.item(repo="/fixture/sat")
        stale = (datetime.fromisoformat(build.clock()) - timedelta(seconds=build.STALE_SECONDS + 1)).isoformat()
        for case, stamp in (("no beat", None), ("a stale beat", stale), ("a beat that does not read", "soon")):
            with self.subTest(case=case):
                if stamp is not None:
                    record_state(self.db, "heartbeat", key="build-dispatch@work", timestamp=stamp, body={})
                with self.assertRaisesRegex(build.BuildRefused, "lane runs on work, and no build-dispatch@work beat"):
                    build.queue(self.db, [hub, satellite], who="operator")
                self.assertEqual(self.count("SELECT COUNT(*) FROM assignment"), 0)
                self.assertEqual(self.statuses([hub, satellite]), ["ready", "ready"])
        # Pack sd:7955: a beat further ahead than STALE_SECONDS names the skew, not a dead dispatcher.
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={},
                     timestamp=later(build.clock(), build.STALE_SECONDS + 60))
        with self.assertRaises(build.BuildRefused) as skewed:
            build.queue(self.db, [hub, satellite], who="operator")
        self.assertIn("lane runs on work, and the build-dispatch@work beat is", str(skewed.exception))
        self.assertIn("s ahead of this clock", str(skewed.exception))
        self.assertNotIn("no dispatcher", str(skewed.exception))
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment"), 0)
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={})
        self.assertEqual(len(build.queue(self.db, [hub, satellite], who="operator")), 2, "a fresh beat takes it")

    def test_a_press_queues_one_build_per_item_and_locks_its_status(self):
        items = [self.item(), self.item()]
        queued = build.queue(self.db, items, who="operator",
                             expected_revisions={item: item_state(self.db, item)["revision"] for item in items})
        self.assertEqual([(row["item"], row["status"], row["role"], row["lane"], row["scope"], row["budget_minutes"])
                          for row in queued],
                         [(item, "queued", "author", "parallel", "build", 60) for item in items])
        with self.assertRaisesRegex(workflow.TransitionRefused, f"cancel queued assignment {queued[0]['id']} first"):
            workflow.change_status(self.db, items[0], "blocked", who="operator")

    def test_a_restore_reimport_holds_the_press_and_the_claim(self):
        item = self.item()
        build.queue(self.db, [item], who="operator")
        record_state(self.db, "restore", body={})
        with self.assertRaisesRegex(build.BuildRefused, "restore reimport"):
            build.queue(self.db, [self.item()], who="operator")
        with self.assertRaisesRegex(build.BuildRefused, "restore reimport"):
            self.claim()
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run"), 0)

    def test_eligible_is_the_ready_items_a_press_takes_by_priority(self):
        low, high = self.item(priority=3), self.item(priority=1)
        self.item(status="planning")
        self.item(kind="followup")
        held = self.item()
        build.queue(self.db, [held], who="operator")
        self.assertEqual([row["id"] for row in build.eligible(self.db)], [high, low])


class Claim(Case):
    def test_a_bundle_is_one_run_one_lease_and_its_items_in_progress(self):
        items = [self.item(), self.item()]
        queued = build.queue(self.db, items, who="operator")
        run = self.claim()
        lead = queued[0]["id"]
        self.assertEqual((run["assignment"], run["branch"], run["step"], run["run"], run["model"]),
                         (lead, f"build/sd-{lead}", "claimed", 1, "opus"))
        self.assertEqual(run["work_path"], str(self.root / "worktrees" / f"a-build-{lead}"))
        self.assertEqual([(m["id"], m["status"]) for m in run["members"]],
                         [(row["id"], "running") for row in queued])
        self.assertEqual(self.db.execute("SELECT parent FROM assignment WHERE id = ?", (queued[1]["id"],)).fetchone()[0], lead)
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL AND branch = ?",
                                    f"build/sd-{lead}"), 1)
        self.assertEqual(self.statuses(items), ["in_progress", "in_progress"])
        self.assertIsNone(self.claim(), "nothing is left queued")

    def test_a_claim_records_the_build_branch_on_every_member_row(self):
        """pack sd:6756: an empty `item.branch` failed sd-docs-lint on a design-only build."""
        items = [self.item(), self.item()]
        self.db.execute("UPDATE item SET branch = 'docs/old' WHERE id = ?", (items[1],))
        lead = build.queue(self.db, items, who="operator")[0]["id"]
        self.claim()
        self.assertEqual([item_state(self.db, item)["item"]["branch"] for item in items],
                         [f"build/sd-{lead}"] * 2)

    def test_a_bundle_of_mechanical_items_runs_sonnet(self):
        build.queue(self.db, [self.item(fields={"work_kind": "mechanical"})], who="operator")
        self.assertEqual(self.claim()["model"], "sonnet")

    def test_a_bundle_takes_at_most_three_and_the_best_repository_first(self):
        build.queue(self.db, [self.item(repo=B, priority=2)], who="operator")
        build.queue(self.db, [self.item(priority=1) for _ in range(4)], who="operator")
        first = self.claim()
        self.assertEqual((first["repo"], len(first["members"])), (A, 3))
        second = self.claim()
        self.assertEqual((second["repo"], len(second["members"])), (B, 1), "A's fourth waits for its running bundle")
        self.assertIsNone(self.claim())
        build.end(self.db, first["id"], "cancelled", "the operator cancelled it")
        third = self.claim()
        self.assertEqual((third["repo"], len(third["members"])), (A, 1), "the next bundle takes the fourth")

    def test_one_running_bundle_fills_its_repository_not_another(self):
        """sd:8158: one bundle per repository at a time, so later items wait for it and the next bundle takes them."""
        build.queue(self.db, [self.item()], who="operator")
        self.claim()
        available = build.room(self.db, **IDLE)
        self.assertEqual(build.room_for(available, A), 0)
        self.assertEqual(build.room_for(available, B), 1)

    def test_claim_race_respects_repo_cap(self):
        """Failure table, Claim: `BEGIN IMMEDIATE` serializes claims; the later ones see the earlier leases."""
        items = [self.item() for _ in range(15)]
        build.queue(self.db, items, who="operator")
        barrier, results, errors = threading.Barrier(6), [], []

        def race():
            connection = connect(self.database)
            try:
                barrier.wait()
                results.append(self.claim(connection))
            except Exception as error:  # noqa: BLE001 - the test reports any failure
                errors.append(error)
            finally:
                connection.close()

        threads = [threading.Thread(target=race) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        runs = [run for run in results if run is not None]
        self.assertEqual(len(runs), build.REPO_CAP)
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), build.REPO_CAP)
        members = [member["item"] for run in runs for member in run["members"]]
        self.assertEqual(len(members), len(set(members)))
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment WHERE status = 'running'"),
                         build.REPO_CAP * build.BUNDLE_MAX)

    def test_claim_rolls_back_moved_item(self):
        """Failure table, Claim: an item moved after the press fails the re-check; nothing else changes."""
        items = [self.item(), self.item()]
        queued = build.queue(self.db, items, who="operator")
        transition(self.db, items[1], "in_progress", who="hand")
        self.assertIsNone(self.claim())
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease"), 0)
        rows = self.db.execute("SELECT status, result FROM assignment ORDER BY id").fetchall()
        self.assertEqual(rows[0]["status"], "queued")
        self.assertEqual(rows[1]["status"], "blocked")
        self.assertIn(f"item {items[1]} moved to in_progress after the press", rows[1]["result"])
        self.assertEqual(self.statuses(items), ["ready", "in_progress"])
        run = self.claim()
        self.assertEqual([m["id"] for m in run["members"]], [queued[0]["id"]])

    def ineligible_after_press(self, change: str, reason: str):
        """A repository that turns ineligible between press and claim: its builds end blocked, the next repo runs.

        `change` is the SQL that turns repository A ineligible; `reason` the
        refusal `queue` gives for it. The claim re-checks the repository in
        its transaction (pack sd:6103), so A's queued builds end `blocked`
        with that reason, their items stay `ready` for a later press, and the
        same claim goes on to B.
        """
        items = [self.item(priority=1), self.item(priority=1)]
        queued = build.queue(self.db, items, who="operator")
        other = build.queue(self.db, [self.item(repo=B, priority=2)], who="operator")
        self.db.execute(change, (A,))
        run = self.claim()
        self.assertIsNotNone(run, "the ineligible repository does not stall the claim")
        self.assertEqual((run["repo"], [m["id"] for m in run["members"]]), (B, [other[0]["id"]]))
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run WHERE repo = ?", A), 0)
        rows = self.db.execute("SELECT status, result FROM assignment WHERE id IN (?, ?) ORDER BY id",
                               tuple(row["id"] for row in queued)).fetchall()
        self.assertEqual([row["status"] for row in rows], ["blocked", "blocked"])
        for row in rows:
            self.assertRegex(row["result"], reason)
        self.assertEqual(self.statuses(items), ["ready", "ready"])
        self.assertIsNone(self.claim(), "nothing of A is left queued")

    def test_a_requeued_bundle_claims_alone_under_its_lead_and_gets_its_worktree_back(self):
        """Pack sd:6020: Send to builder queues a bundle again; its claim keeps the lead, so branch and worktree match."""
        items = [self.item(), self.item()]
        queued = build.queue(self.db, items, who="operator")
        lead = queued[0]["id"]
        first = self.claim()
        build.end(self.db, first["id"], "cancelled", "the operator cancelled it")
        newer = self.item()
        build.queue(self.db, [newer], who="operator")  # queued before the requeue, so it ranks first
        for row in reversed(queued):
            state = runner.queue_state(self.db, row["id"])
            runner.requeue(self.db, row["id"], expected_revision=state["revision"], who="operator")
        with mock.patch.object(build, "REPO_CAP", 2):  # two bundles at once: their leads are this test's subject
            runs = [self.claim(), self.claim()]
        self.assertEqual([len(run["members"]) if run else 0 for run in runs].count(0), 0, "two bundles, not one")
        claims = {run["assignment"]: run for run in runs}
        again = claims.pop(lead)
        self.assertEqual((again["branch"], again["work_path"], again["run"]), (first["branch"], first["work_path"], 2))
        self.assertEqual(sorted(member["item"] for member in again["members"]), sorted(items))
        self.assertEqual([member["item"] for member in claims.popitem()[1]["members"]], [newer])

    def test_claim_skips_a_repository_unmanaged_after_the_press(self):
        self.ineligible_after_press("UPDATE repo SET managed = 0 WHERE path = ?",
                                    "is not managed and its remote is not on GitHub")

    def test_a_press_takes_a_task_in_an_unmanaged_github_repository(self):
        """Pack sd:7868: an unmanaged repository's task builds to a draft pull request, by press and by pickup."""
        upsert_repo(self.db, "/fixture/gh", remote="git@github.com:owner/third-party.git", managed=0)
        item = self.item(repo="/fixture/gh")
        self.assertEqual([row["id"] for row in build.eligible(self.db)], [item])
        queued = build.queue(self.db, [item], who="operator")
        self.assertEqual([row["status"] for row in queued], ["queued"])
        taken = self.claim()
        self.assertEqual(([m["item"] for m in taken["members"]], self.statuses([item])), ([item], ["in_progress"]))

    LINKED = {"issue_url": ("https://github.com/owner/third-party/issues/3", "already links the issue"),
              "pull_url": ("https://github.com/owner/third-party/pull/4", "already links the pull request")}

    def test_a_press_refuses_an_unmanaged_task_whose_contribution_is_already_linked(self):
        """Review of 3072db937: a task linked to an issue or another pull request takes no build that would push and
        open a draft, then fail to link it, on every Retry."""
        from sd_db import contributions

        upsert_repo(self.db, "/fixture/gh", remote="git@github.com:owner/third-party.git", managed=0)
        for name, (url, reason) in self.LINKED.items():
            with self.subTest(link=name):
                item = self.item(repo="/fixture/gh")
                contributions.configure(self.db, item, {name: url}, who="operator")
                self.assertNotIn(item, [row["id"] for row in build.eligible(self.db)])
                with self.assertRaisesRegex(build.BuildRefused, reason):
                    build.queue(self.db, [item], who="operator")

    def test_a_claim_refuses_an_unmanaged_task_linked_after_the_press(self):
        """Review of 3072db937: the claim reads the same rule, so a link made after the press pushes nothing."""
        from sd_db import contributions

        upsert_repo(self.db, "/fixture/gh", remote="git@github.com:owner/third-party.git", managed=0)
        for name, (url, reason) in self.LINKED.items():
            with self.subTest(link=name):
                item = self.item(repo="/fixture/gh")
                (queued,) = build.queue(self.db, [item], who="operator")
                contributions.configure(self.db, item, {name: url}, who="operator")
                self.assertIsNone(self.claim())
                row = self.db.execute("SELECT status, result FROM assignment WHERE id = ?", (queued["id"],)).fetchone()
                self.assertEqual(row["status"], "blocked")
                self.assertIn(reason, row["result"])

    def test_a_kill_whose_descendants_did_not_read_passes_once_its_group_is_gone(self):
        """Review of 3072db937: a failed `ps` read with the recorded group gone blocked every reap, managed too."""
        gone = subprocess.Popen(["true"], start_new_session=True)
        gone.wait()
        with mock.patch.object(build, "descendant_groups", return_value=None):
            self.assertTrue(build.kill_group(gone.pid))

    def test_pickup_queues_an_unmanaged_task(self):
        """Pack sd:7868: auto-pickup, once on, picks what a press takes."""
        upsert_repo(self.db, "/fixture/gh", remote="https://github.com/owner/third-party", managed=0)
        item = self.item(repo="/fixture/gh")
        build.set_pickup(self.db, True)
        available = build.room(self.db, **IDLE)
        self.assertEqual(build.pickup(self.db, available), [item])

    def test_claim_skips_a_repository_without_a_remote_after_the_press(self):
        self.ineligible_after_press("UPDATE repo SET remote = NULL WHERE path = ?", "has no remote")

    def test_claim_skips_a_repository_moved_to_a_satellite_lane_after_the_press(self):
        """Pack sd:7933: the hub's claim leaves a live satellite's rows queued for it, blocks none, and goes on to B.
        Once that satellite's dispatcher goes quiet the hub blocks them (Hosts, review round 3)."""
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={})
        items = [self.item(priority=1)]
        queued = build.queue(self.db, items, who="operator")
        other = build.queue(self.db, [self.item(repo=B, priority=2)], who="operator")
        self.db.execute("UPDATE repo SET lane_host = 'work' WHERE path = ?", (A,))
        run = self.claim(hosts={None, "hub"})
        self.assertEqual((run["repo"], [m["id"] for m in run["members"]]), (B, [other[0]["id"]]))
        self.assertEqual(self.count("SELECT status FROM assignment WHERE id = ?", queued[0]["id"]), "queued")
        self.assertEqual(self.statuses(items), ["ready"])
        self.assertIsNone(self.claim(hosts={None, "hub"}))

    def test_claim_respects_exclusive_lease(self):
        """Guard: an exclusive lease of any other kind in the repository leaves it no room."""
        self.db.execute("INSERT INTO assignment (item, role, status, scope) VALUES (?, 'author', 'blocked', 'item')",
                        (self.item(status="planning"),))
        old = self.db.execute("SELECT MAX(id) FROM assignment").fetchone()[0]
        self.db.execute("INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, "
                        "created_at, updated_at) VALUES ('old', ?, 1, ?, 'work/x', 'runner', '/w', '/r', '', '')", (old, A))
        self.db.execute("INSERT INTO runner_lease (run, repo, branch, exclusive, acquired_at) VALUES ('old', ?, 'work/x', 1, '')", (A,))
        build.queue(self.db, [self.item()], who="operator")
        self.assertIsNone(self.claim())
        build.queue(self.db, [self.item(repo=B)], who="operator")
        self.assertEqual(self.claim()["repo"], B)

    def test_an_unread_load_or_builder_count_is_no_room(self):
        build.queue(self.db, [self.item()], who="operator")
        for inputs in ({"load": None}, {"load_cap": float("nan")}, {"interactive": None}, {"load": 41.0}):
            with self.subTest(inputs=inputs):
                self.assertIsNone(self.claim(**inputs))
                self.assertTrue(build.room(self.db, **{**IDLE, **inputs})["why"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run"), 0)

    def test_the_gates_off_limit_is_no_load_cap(self):
        """Review round 1: the gate reports `load_max` 0 when its load rule is off, its default; that is no cap."""
        with mock.patch("sys.path", [str(Path(__file__).resolve().parents[2] / "bin"), *sys.path]):
            gate = import_module("sd_gate_slots")
        off = gate.load_rule({}).limit
        build.queue(self.db, [self.item()], who="operator")
        self.assertEqual(build.room(self.db, **{**IDLE, "load": 2.5, "load_cap": off})["why"], "")
        self.assertIsNotNone(self.claim(load=2.5, load_cap=off))

    def test_interactive_builders_count_against_their_repository_and_the_machine(self):
        build.queue(self.db, [self.item()], who="operator")
        busy = [{"repo": A, "session_id": f"s{n}"} for n in range(build.REPO_CAP)]
        self.assertIsNone(self.claim(interactive=busy))
        elsewhere = [{"repo": None, "session_id": f"s{n}"} for n in range(6)]
        self.assertIsNone(self.claim(interactive=elsewhere))
        self.assertIn("6 builders run already", build.room(self.db, **{**IDLE, "interactive": elsewhere})["why"])
        run = self.claim(interactive=busy[:build.REPO_CAP - 1])
        build.write(self.db, run["id"], session_id="headless")
        available = build.room(self.db, **{**IDLE, "interactive": [{"repo": A, "session_id": "headless"}]})
        self.assertEqual(available["used"], {A: 1}, "a headless run's own session counts once")
        self.assertEqual(build.room_for(available, A), build.REPO_CAP - 1)


class Writes(Case):
    def setUp(self):
        super().setUp()
        self.items = [self.item(), self.item()]
        build.queue(self.db, self.items, who="operator")
        self.bundle = self.claim()

    def test_late_run_writes_nothing(self):
        """Failure table, Enqueued: a late old supervisor's compare-and-set names its own ended run; no row changes."""
        self.db.execute("UPDATE runner_run SET outcome = 'interrupted' WHERE id = ?", (self.bundle["id"],))
        before = {table: self.db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                  for table in ("runner_run", "runner_lease", "assignment", "item")}
        self.assertFalse(build.write(self.db, self.bundle["id"], step="building", heartbeat_at="x"))
        self.assertFalse(build.beat(self.db, self.bundle["id"]))
        self.assertFalse(build.enqueued(self.db, self.bundle["id"]))
        after = {table: self.db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in before}
        self.assertEqual([[tuple(row) for row in rows] for rows in after.values()],
                         [[tuple(row) for row in rows] for rows in before.values()])

    def test_enqueued_releases_the_lease_and_ends_the_members_done(self):
        self.assertTrue(build.write(self.db, self.bundle["id"], step="enqueuing"))
        self.assertTrue(build.enqueued(self.db, self.bundle["id"]))
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 0)
        self.assertEqual([m["status"] for m in build.run(self.db, self.bundle["id"])["members"]], ["done", "done"])
        self.assertEqual(self.statuses(self.items), ["in_progress", "in_progress"])
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((row["step"], row["outcome"], row["released_at"]), ("enqueued", None, None),
                         "the run stays open for steps 7 and 8")
        self.assertFalse(build.enqueued(self.db, self.bundle["id"]), "enqueued once")

    def test_an_enqueued_bundle_keeps_its_repository_slot_until_the_run_ends(self):
        """Pack sd:8158: the lease goes at `enqueued`, but its pull request is open; a second bundle is a second PR."""
        build.write(self.db, self.bundle["id"], step="enqueuing")
        build.enqueued(self.db, self.bundle["id"])
        build.queue(self.db, [self.item(), self.item(B)], who="operator")
        available = build.room(self.db, **IDLE)
        self.assertEqual(build.room_for(available, A), 0, "the open pull request holds A's one bundle")
        self.assertGreater(build.room_for(available, B), 0, "another repo still has room")
        self.assertEqual(available["machine"], build.MACHINE_CAP, "no builder runs: the machine slot is free")
        self.assertEqual(self.claim()["repo"], B)
        self.assertIsNone(self.claim(), "A's item waits for the next bundle")
        build.end(self.db, self.bundle["id"], "done", "merged")
        self.assertEqual(self.claim()["repo"], A, "the slot frees once the run ends")

    def enqueue_draft(self):
        url = "https://github.com/example/project/pull/5"
        build.write(self.db, self.bundle["id"], step="enqueuing", detail=build.DRAFT_PULL + url)
        self.assertTrue(build.enqueued(self.db, self.bundle["id"], held_by=build.DRAFT_PULL + url))

    def test_an_unmanaged_draft_pull_request_frees_its_repository_slot(self):
        """Pack sd:8158, blocker #49312: its owners may leave the draft open for weeks; no reap or cancel ends the run."""
        self.enqueue_draft()
        build.queue(self.db, [self.item()], who="operator")
        available = build.room(self.db, **IDLE)
        self.assertEqual(build.room_for(available, A), 1, "an open upstream draft stops no later build")
        self.assertNotIn(A, available["used"])
        self.assertEqual(self.claim()["repo"], A)

    def test_an_ended_run_at_a_lane_step_holds_no_slot(self):
        """Pack sd:8158, class pass: a reaped run stays unreleased until its groups read gone, which may be never."""
        build.write(self.db, self.bundle["id"], step="enqueuing")
        build.enqueued(self.db, self.bundle["id"])
        self.db.execute("UPDATE runner_run SET step = 'cleaning', outcome = 'blocked' WHERE id = ?", (self.bundle["id"],))
        self.assertEqual(build.room_for(build.room(self.db, **IDLE), A), 1)

    def test_a_managed_run_keeps_its_slot_at_every_lane_step(self):
        """Pack sd:8158: only a draft pull request frees the slot early; `reap` bounds the two supervised steps."""
        build.write(self.db, self.bundle["id"], step="enqueuing")
        build.enqueued(self.db, self.bundle["id"])
        for step in build.LANE_STEPS:
            with self.subTest(step=step):
                self.db.execute("UPDATE runner_run SET step = ? WHERE id = ?", (step, self.bundle["id"]))
                self.assertEqual(build.room_for(build.room(self.db, **IDLE), A), 0)

    def test_a_draft_run_past_enqueued_holds_its_slot_until_it_cleans_up(self):
        """Pack sd:8158: after the owners merge, `cleaning` runs under a supervisor in that worktree, so it counts."""
        self.enqueue_draft()
        self.db.execute("UPDATE runner_run SET step = 'cleaning' WHERE id = ?", (self.bundle["id"],))
        self.assertEqual(build.room_for(build.room(self.db, **IDLE), A), 0)

    def test_reap_skips_enqueued_run(self):
        """Failure table, Enqueued: no supervisor while the run waits on the lane; reap leaves it open."""
        build.write(self.db, self.bundle["id"], step="enqueuing", supervisor_pid=4242, supervisor_start=START)
        build.enqueued(self.db, self.bundle["id"])
        kill = Kill(self.db)
        self.assertEqual(build.reap(self.db, now=later(self.bundle["created_at"], 3600), probe=Probe(alive=False),
                                    kill=kill), [])
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((row["outcome"], row["released_at"], kill.calls), (None, None, []))

    def test_an_abandoned_enqueued_run_releases_once_gone(self):
        """Failure table, Restore: an enqueued run abandoned by a restore is held, then released once it reads gone."""
        build.write(self.db, self.bundle["id"], step="enqueuing")
        build.enqueued(self.db, self.bundle["id"])
        recovery.prepare_restore(self.db)
        build.reap(self.db, probe=Probe(alive=False))
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((row["step"], row["outcome"], row["released_at"] is not None), ("enqueued", "abandoned", True))
        self.assertIsNone(build._held(self.db, self.items[0]), "no held run left")

    def test_reap_judges_a_checking_run(self):
        """Failure table, Checking: a fresh supervisor lost after the merge; gone, so settled and retried at that step."""
        build.write(self.db, self.bundle["id"], step="enqueuing")
        build.enqueued(self.db, self.bundle["id"])
        build.write(self.db, self.bundle["id"], step="checking", supervisor_pid=4242, supervisor_pgid=4242,
                    supervisor_start=START)
        settled = build.reap(self.db, now=later(self.bundle["created_at"], 3600), probe=Probe(alive=False))
        self.assertEqual([(row["run"], row["outcome"], row["blocked"]) for row in settled],
                         [(self.bundle["id"], "interrupted", None)])
        retry = build.run(self.db, settled[0]["retry"])
        self.assertEqual((retry["step"], retry["run"], retry["released_at"]), ("checking", 2, None))
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 0,
                         "a lane step takes no builder slot")
        self.assertEqual(self.statuses(self.items), ["in_progress", "in_progress"])

    def test_enqueued_refuses_a_requested_cancel(self):
        """Failure table, Cancel: a cancel committed before `enqueued` wins; the run never reaches the lane."""
        build.write(self.db, self.bundle["id"], step="reviewing")
        state = operations.assignment_state(self.db, self.bundle["assignment"])
        operations.cancel_assignment(self.db, self.bundle["assignment"], expected_revision=state["revision"],
                                     who="operator")
        self.assertFalse(build.enqueuing(self.db, self.bundle["id"]), "the enqueuing compare-and-set loses")
        build.write(self.db, self.bundle["id"], step="enqueuing")
        self.assertFalse(build.enqueued(self.db, self.bundle["id"]))
        self.assertEqual(build.run(self.db, self.bundle["id"])["step"], "enqueuing")
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 1)

    def test_enqueuing_first_refuses_cancel(self):
        """Failure table, Cancel: after the enqueuing compare-and-set a cancel is refused, naming the lane entry."""
        build.write(self.db, self.bundle["id"], step="reviewing")
        self.assertTrue(build.enqueuing(self.db, self.bundle["id"]))
        state = operations.assignment_state(self.db, self.bundle["assignment"])
        self.assertIn("sd-ship lane cancel", state["capabilities"]["cancel"]["reason"])
        with self.assertRaisesRegex(workflow.WorkflowError, "cancel its lane entry"):
            operations.cancel_assignment(self.db, self.bundle["assignment"], expected_revision=state["revision"],
                                         who="operator")
        self.assertIsNone(build.run(self.db, self.bundle["id"])["cancel_requested"])

    def test_an_unmanaged_run_past_enqueuing_names_its_draft_not_the_lane(self):
        """sd:7909: `sd-ship lane cancel` refuses an unmanaged repository; the refusal names its draft instead."""
        upsert_repo(self.db, A, remote="https://github.com/owner/third-party.git", managed=0)
        build.write(self.db, self.bundle["id"], step="reviewing")
        state = operations.assignment_state(self.db, self.bundle["assignment"])
        self.assertTrue(build.enqueuing(self.db, self.bundle["id"]))
        reason = operations.assignment_state(self.db, self.bundle["assignment"])["capabilities"]["cancel"]["reason"]
        self.assertIn("close its draft pull request", reason)
        self.assertNotIn("sd-ship lane cancel", reason)
        with mock.patch.object(operations, "assignment_state", lambda connection, assignment: state):
            with self.assertRaisesRegex(workflow.WorkflowError, "close its draft pull request"):
                operations.cancel_assignment(self.db, self.bundle["assignment"], expected_revision=state["revision"],
                                             who="operator")

    def test_cancel_write_checks_the_step(self):
        """A cancel read before the enqueuing compare-and-set and written after it changes nothing (one winner)."""
        build.write(self.db, self.bundle["id"], step="reviewing")
        state = operations.assignment_state(self.db, self.bundle["assignment"])
        real = operations.assignment_state

        def raced(connection, assignment):
            build.enqueuing(self.db, self.bundle["id"])
            return state

        with mock.patch.object(operations, "assignment_state", raced):
            with self.assertRaisesRegex(workflow.WorkflowError, "moved past the steps a cancel may stop"):
                operations.cancel_assignment(self.db, self.bundle["assignment"], expected_revision=state["revision"],
                                             who="operator")
        self.assertIsNone(build.run(self.db, self.bundle["id"])["cancel_requested"])
        self.assertEqual(real(self.db, self.bundle["assignment"])["build_run"], self.bundle["id"])

    def test_a_write_outside_the_run_fields_is_refused(self):
        for fields in ({"outcome": "done"}, {"released_at": "x"}, {"step": "nowhere"}):
            with self.subTest(fields=fields), self.assertRaises(build.BuildRefused):
                build.write(self.db, self.bundle["id"], **fields)


class Reap(Case):
    def setUp(self):
        super().setUp()
        self.items = [self.item(), self.item()]
        build.queue(self.db, self.items, who="operator")
        self.bundle = self.claim()

    def stale(self):
        return later(self.bundle["created_at"], build.STALE_SECONDS + 1)

    def test_reap_unstarted_run(self):
        """Failure table, Spawn: no beat for `STALE_SECONDS` after the claim: reaped, a fresh row at `worktree`."""
        self.assertEqual(build.reap(self.db, now=later(self.bundle["created_at"], 60)), [])
        settled = build.reap(self.db, now=self.stale())
        self.assertEqual([(row["run"], row["outcome"]) for row in settled], [(self.bundle["id"], "interrupted")])
        retry = build.run(self.db, settled[0]["retry"])
        self.assertEqual((retry["step"], retry["run"], retry["branch"], retry["work_path"], retry["released_at"]),
                         ("worktree", 2, self.bundle["branch"], self.bundle["work_path"], None))
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 1)
        self.assertEqual([m["status"] for m in retry["members"]], ["running", "running"])

    def test_a_second_loss_blocks_the_items_with_an_action_note(self):
        build.write(self.db, self.bundle["id"], step="building", log_path="/logs/one.jsonl")
        retry = build.reap(self.db, now=self.stale())[0]["retry"]
        row = build.run(self.db, retry)
        settled = build.reap(self.db, now=later(row["created_at"], build.STALE_SECONDS + 1))
        self.assertEqual(settled[0]["retry"], None)
        self.assertEqual([m["status"] for m in build.run(self.db, retry)["members"]], ["blocked", "blocked"])
        self.assertEqual(self.statuses(self.items), ["blocked", "blocked"])
        note = self.db.execute("SELECT body FROM note WHERE item = ? AND kind = 'followup'", (self.items[0],)).fetchone()
        self.assertIn("lost at step building", note["body"])
        self.assertIn(row["work_path"], note["body"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 0)

    def lose(self, **fields):
        """Lose the current run at a stale beat; the fresh row the reap opened."""
        if fields:
            build.write(self.db, self.bundle["id"], **fields)
        settled = build.reap(self.db, now=later(self.bundle["created_at"], build.STALE_SECONDS + 1), probe=Probe())
        self.bundle = build.run(self.db, settled[0]["retry"]) if settled[0]["retry"] else self.bundle
        return settled[0]

    def test_resume_fixing_reviews_again(self):
        """Failure table, Recovery: a loss at fixing resumes at reviewing, round and session kept, in one fresh row."""
        old = self.bundle["id"]
        self.lose(step="fixing", review_round=2, session_id="session-1")
        self.assertEqual((self.bundle["step"], self.bundle["review_round"], self.bundle["session_id"],
                          self.bundle["outcome"], self.bundle["work_path"], self.bundle["branch"]),
                         ("reviewing", 2, "session-1", None, build.run(self.db, old)["work_path"],
                          build.run(self.db, old)["branch"]))
        self.assertEqual(build.run(self.db, old)["outcome"], "interrupted")
        self.assertFalse(build.write(self.db, old, step="building"), "a late old supervisor writes nothing")

    def test_second_loss_same_step(self):
        """Failure table, Recovery: losses at two steps each retry; a second loss at one step blocks."""
        self.assertIsNotNone(self.lose(step="building")["retry"])
        self.assertIsNotNone(self.lose(step="reviewing")["retry"], "a first loss at reviewing")
        self.assertIsNone(self.lose(step="reviewing")["retry"])
        self.assertEqual(self.statuses(self.items), ["blocked", "blocked"])

    def at_lane_step(self, step):
        build.write(self.db, self.bundle["id"], step="enqueuing")
        build.enqueued(self.db, self.bundle["id"])
        build.write(self.db, self.bundle["id"], step=step)

    def test_a_second_loss_at_checking_turns_off_and_files(self):
        """pack sd:6094 (c): health unknown after a second loss, so auto-pickup off, an item filed, then cleaning."""
        set_setting(self.db, "build.auto_pickup_until", "2026-10-25T13:00:00+00:00")
        self.at_lane_step("checking")
        items = self.count("SELECT COUNT(*) FROM item")
        self.assertIsNotNone(self.lose()["retry"], "a first loss at checking retries")
        settled = self.lose()
        self.assertEqual(read_settings(self.db, ["build.auto_pickup_until"]), {"build.auto_pickup_until": None})
        filed = self.db.execute("SELECT * FROM item WHERE id > ? ORDER BY id", (max(self.items),)).fetchall()
        self.assertEqual((self.count("SELECT COUNT(*) FROM item"), len(filed)), (items + 1, 1))
        self.assertIn("health unknown", filed[0]["title"])
        retry = build.run(self.db, settled["retry"])
        self.assertEqual((retry["step"], retry["outcome"], retry["released_at"]), ("cleaning", None, None))
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 0)
        self.assertEqual(self.statuses(self.items), ["in_progress", "in_progress"], "the lane owns the items")

    def test_a_second_loss_at_cleaning_ends_done(self):
        """pack sd:6094 (c): a second loss at cleaning ends the run done; the worktree stays, with a note."""
        self.at_lane_step("cleaning")
        self.assertIsNotNone(self.lose()["retry"])
        settled = self.lose()
        row = build.run(self.db, settled["run"])
        self.assertEqual((settled["retry"], row["outcome"], row["released_at"] is not None), (None, "done", True))
        self.assertEqual(self.statuses(self.items), ["in_progress", "in_progress"])
        note = self.db.execute("SELECT body FROM note WHERE item = ? ORDER BY id DESC", (self.items[0],)).fetchone()
        self.assertIn(f"worktree {row['work_path']} kept", note["body"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run WHERE released_at IS NULL"), 0)

    def test_a_live_supervisor_is_left_alone(self):
        build.write(self.db, self.bundle["id"], supervisor_pid=4242, supervisor_pgid=4242,
                    supervisor_start="Sat Oct 24 09:00:00 2026", heartbeat_at=self.bundle["created_at"])
        soon = later(self.bundle["created_at"], 60)
        self.assertEqual(build.reap(self.db, now=soon, probe=Probe()), [])
        self.assertEqual(build.reap(self.db, now=soon, probe=Probe(alive=None)), [], "an unknown probe is no verdict")
        self.assertEqual(len(build.reap(self.db, now=soon, probe=Probe(started="Sat Oct 24 08:00:00 2026"))), 1,
                         "a reused pid is not the supervisor")

    def test_a_lost_compare_and_set_leaves_a_run_that_beat(self):
        build.write(self.db, self.bundle["id"], heartbeat_at=self.bundle["created_at"])

        class Beating(Probe):
            def alive(probe, pid):
                build.write(self.db, self.bundle["id"], heartbeat_at=later(self.bundle["created_at"], 30))
                return False

        build.write(self.db, self.bundle["id"], supervisor_pid=4242)
        self.assertEqual(build.reap(self.db, now=later(self.bundle["created_at"], 60), probe=Beating()), [])
        self.assertIsNone(build.run(self.db, self.bundle["id"])["outcome"])

    def test_reap_resumes_kill(self):
        """Failure table, Reap ours: a tick killed between the compare-and-set and the kill; the next kills, then retries."""
        self.ended(builder_pgid=777, builder_start=START)
        kill = Kill(self.db)
        settled = build.reap(self.db, probe=Probe(group=True), kill=kill)
        self.assertEqual(kill.calls, [(777, 1)], "the kill came before the retry's new run")
        self.assertIsNotNone(settled[0]["retry"])

    def test_a_group_alive_after_the_kill_ends_blocked(self):
        """Failure table, Reap ours: the group still has a process after SIGKILL: blocked, lease released, no retry."""
        self.ended(builder_pgid=777, builder_start=START)
        settled = build.reap(self.db, probe=Probe(group=True), kill=Kill(self.db, gone=False))
        self.assertEqual([(row["retry"], "777" in row["blocked"]) for row in settled], [(None, True)])
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run"), 1)
        self.assertEqual(self.statuses(self.items), ["blocked", "blocked"])

    def test_a_survivor_keeps_its_lease(self):
        """Failure table, Blocked: a group outlives SIGKILL; the run stays unreleased with its lease, a slot short."""
        free = build.room(self.db, **IDLE)["machine"]
        self.ended(builder_pgid=777, builder_start=START)
        build.reap(self.db, probe=Probe(group=True), kill=Kill(self.db, gone=False))
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((row["outcome"], row["released_at"]), ("blocked", None))
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 1)
        self.assertEqual(build.room(self.db, **IDLE)["machine"], free, "the survivor still holds its slot")

    def test_third_unsure_keeps_the_lease(self):
        """Failure table, Blocked: the third unsure reap blocks the run but keeps it unreleased with its lease."""
        self.ended(builder_pgid=777, builder_start=START)
        for _ in range(build.DOUBTS):
            build.reap(self.db, probe=Probe(started=None, group=True), kill=Kill(self.db))
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((row["outcome"], row["released_at"]), ("blocked", None))
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 1)
        self.assertEqual(self.statuses(self.items), ["blocked", "blocked"])

    def test_blocked_run_releases_once_groups_are_gone(self):
        """Failure table, Blocked: later ticks re-probe; alive or unsure changes nothing, ESRCH releases; never a retry."""
        self.ended(builder_pgid=777, builder_start=START, supervisor_pid=4242, supervisor_pgid=4242,
                   supervisor_start=START)
        build.reap(self.db, probe=Probe(group=True), kill=Kill(self.db, gone=False))
        notes = self.count("SELECT COUNT(*) FROM note WHERE kind = 'followup'")
        for probe in (Probe(group=True), Probe(alive=None, group=None)):
            kill = Kill(self.db)
            self.assertEqual(build.reap(self.db, probe=probe, kill=kill), [])
            self.assertEqual(kill.calls, [], "never killed again")
            self.assertIsNone(build.run(self.db, self.bundle["id"])["released_at"])
        kill = Kill(self.db)
        build.reap(self.db, probe=Probe(alive=False, group=False), kill=kill)
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((row["outcome"], row["released_at"] is not None, kill.calls), ("blocked", True, []))
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run"), 1, "no retry")
        self.assertEqual(self.count("SELECT COUNT(*) FROM note WHERE kind = 'followup'"), notes, "no new followup")

    def held(self):
        """The bundle's run held (a survivor of SIGKILL), and both items moved back to ready by the operator."""
        self.ended(builder_pgid=777, builder_start=START)
        build.reap(self.db, probe=Probe(group=True), kill=Kill(self.db, gone=False))
        for item in self.items:
            workflow.change_status(self.db, item, "ready", who="operator")
        self.assertEqual(self.statuses(self.items), ["ready", "ready"])

    def test_queue_refuses_an_item_a_held_run_holds(self):
        """Failure table, Held: no press of the lead's or a member's item while its held run keeps the lease."""
        self.held()
        for item in self.items:
            with self.subTest(item=item):
                with self.assertRaisesRegex(build.BuildRefused,
                                            f"held by build run {self.bundle['id']} .*releases once"):
                    build.queue(self.db, [item], who="operator")

    def test_claim_skips_an_item_a_held_run_holds(self):
        """Failure table, Held: a queued build of a held item is not claimed; it ends blocked naming the run."""
        self.held()
        queued = []
        for item in self.items:
            ident = create_assignment(self.db, item=item, role="author", status="queued", lane="parallel")
            self.db.execute("UPDATE assignment SET scope = 'build', queued_at = ? WHERE id = ?",
                            (self.bundle["created_at"], ident))
            queued.append(ident)
        with mock.patch.object(build, "REPO_CAP", 2):  # room beside the held run, so the claim reaches the hold
            self.assertIsNone(self.claim())
        rows = self.db.execute(f"SELECT status, result FROM assignment WHERE id IN ({queued[0]}, {queued[1]})")
        for row in rows:
            self.assertEqual(row["status"], "blocked")
            self.assertIn(f"held by build run {self.bundle['id']}", row["result"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run"), 1, "no second builder")

    def ended(self, **fields):
        """The run as a reap that stopped after its compare-and-set left it."""
        build.write(self.db, self.bundle["id"], **fields)
        self.db.execute("UPDATE runner_run SET outcome = 'interrupted' WHERE id = ?", (self.bundle["id"],))

    def unsure(self, probe, **fields):
        """Failure table, Reap unsure: nothing killed, the run and its lease kept, nothing retried."""
        self.ended(builder_pgid=777, **fields)
        kill = Kill(self.db)
        self.assertEqual(build.reap(self.db, probe=probe, kill=kill), [])
        self.assertEqual(kill.calls, [])
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run WHERE released_at IS NULL"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 1)
        self.assertEqual(self.statuses(self.items), ["in_progress", "in_progress"])

    def supervised(self):
        """A stale run whose supervisor (pid 4242, group 4242) and builder (group 777) both still answer."""
        build.write(self.db, self.bundle["id"], supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START,
                    builder_pgid=777, builder_start=START)
        return later(self.bundle["created_at"], build.STALE_SECONDS + 1)

    def test_ours_with_live_supervisor_kills_both_before_retry(self):
        """Failure table, Reap ours: a paused supervisor and its builder are killed, both ESRCH, then the retry."""
        kill = Kill(self.db)
        settled = build.reap(self.db, now=self.supervised(), probe=Probe(group=True), kill=kill)
        self.assertEqual(kill.calls, [(4242, 1), (777, 1)], "both groups die before the retry's new run")
        self.assertEqual([row["blocked"] for row in settled], [None])
        self.assertIsNotNone(settled[0]["retry"])

    def test_ours_supervisor_survives_kill_ends_blocked(self):
        """Failure table, Reap ours: the supervisor's group outlives SIGKILL: blocked, no retry."""
        kill = Kill(self.db, gone={4242: False})
        settled = build.reap(self.db, now=self.supervised(), probe=Probe(group=True), kill=kill)
        self.assertEqual([(row["retry"], "4242" in row["blocked"]) for row in settled], [(None, True)])
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run"), 1)
        self.assertEqual(self.statuses(self.items), ["blocked", "blocked"])

    def test_ours_supervisor_unproven_is_unsure(self):
        """Failure table, Reap unsure: a live supervisor whose start is unread, or EPERM, or no group: no kill."""
        for name, probe, fields in (("start unread", Probe(started=None), {}),
                                    ("EPERM", Probe(alive=None), {}),
                                    ("no group recorded", Probe(), {"supervisor_pgid": None})):
            with self.subTest(name):
                self.supervised()
                build.write(self.db, self.bundle["id"], builder_pgid=None, **fields)
                self.db.execute("UPDATE runner_run SET outcome = 'interrupted', doubts = NULL WHERE id = ?",
                                (self.bundle["id"],))
                kill = Kill(self.db)
                self.assertEqual(build.reap(self.db, probe=probe, kill=kill), [])
                self.assertEqual(kill.calls, [])
                self.db.execute("UPDATE runner_run SET outcome = NULL WHERE id = ?", (self.bundle["id"],))

    def test_a_live_builder_whose_start_is_unread_is_unsure(self):
        """The round-2 repro: before, this settled and retried into the live builder's worktree."""
        self.unsure(Probe(started=None, group=True), builder_start=START)

    def test_a_reused_builder_pid_with_group_members_is_unsure(self):
        self.unsure(Probe(started="Sat Oct 24 08:00:00 2026", group=True), builder_start=START)

    def test_a_dead_leader_with_a_live_child_is_unsure(self):
        self.unsure(Probe(alive=False, group=True), builder_start=START)

    def test_a_group_the_probe_cannot_read_is_unsure(self):
        self.unsure(Probe(alive=None, group=None), builder_start=START)

    def test_a_builder_without_a_recorded_start_is_unsure(self):
        self.unsure(Probe(group=True))

    def test_a_supervisor_the_probe_cannot_read_is_unsure(self):
        self.unsure(Probe(pids={4242: (None, None), 777: (False, None)}), supervisor_pid=4242,
                    supervisor_start=START, builder_start=START)

    def test_three_unsure_reaps_end_the_run_blocked_naming_the_group(self):
        """Failure table, Reap unsure: the third unsure reap in a row ends it blocked with an action note, no retry."""
        self.ended(builder_pgid=777, builder_start=START)
        kill, probe = Kill(self.db), Probe(started=None, group=True)
        self.assertEqual(build.reap(self.db, probe=probe, kill=kill), [])
        self.assertEqual(build.reap(self.db, probe=probe, kill=kill), [])
        settled = build.reap(self.db, probe=probe, kill=kill)
        self.assertEqual(kill.calls, [])
        self.assertEqual([row["retry"] for row in settled], [None])
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run"), 1)
        self.assertEqual(self.statuses(self.items), ["blocked", "blocked"])
        note = self.db.execute("SELECT body FROM note WHERE item = ? AND kind = 'followup'", (self.items[0],)).fetchone()
        self.assertIn("builder group 777", note["body"])

    def test_an_empty_group_and_a_gone_supervisor_are_gone(self):
        """Failure table, Reap gone: no process in the group, the supervisor gone or its pid reused: settled, retried."""
        for name, pids in {"gone": {4242: (False, None)}, "reused": {4242: (True, "Sat Oct 24 08:00:00 2026")}}.items():
            with self.subTest(name):
                self.ended(builder_pgid=777, builder_start=START, supervisor_pid=4242, supervisor_pgid=4242,
                           supervisor_start=START)
                kill = Kill(self.db)
                settled = build.reap(self.db, probe=Probe(alive=False, pids=pids), kill=kill)
                self.assertEqual(kill.calls, [])
                self.assertEqual([row["blocked"] for row in settled], [None])
                self.bundle = build.run(self.db, settled[0]["retry"]) if settled[0]["retry"] else self.bundle
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run"), 3,
                         "a loss at claimed resumes at worktree; one there is a first loss too")

    def test_a_restore_abandons_every_open_build_run_unretried(self):
        """Failure table, Restore: runs end abandoned, unretried, leases kept; a reap releases each once it reads gone."""
        self.ended(builder_pgid=777, builder_start=START)
        build.queue(self.db, [self.item(repo=B)], who="operator")
        other = self.claim()
        recovery.prepare_restore(self.db)
        rows = {row["id"]: (row["outcome"], row["released_at"]) for row in build.open_runs(self.db)}
        self.assertEqual(rows, {self.bundle["id"]: ("abandoned", None), other["id"]: ("abandoned", None)})
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 2)
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment WHERE status = 'blocked'"), 3)
        kill = Kill(self.db)
        self.assertEqual(build.reap(self.db, now=self.stale(), probe=Probe(group=True), kill=kill), [])
        self.assertEqual(kill.calls, [], "an abandoned run is never signalled")
        self.assertEqual([row["id"] for row in build.open_runs(self.db)], [self.bundle["id"]],
                         "the run with no process is released; the one whose builder answers keeps its lease")
        build.reap(self.db, now=self.stale(), probe=Probe(alive=False), kill=kill)
        self.assertEqual((build.open_runs(self.db), kill.calls), ([], []))
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run"), 2, "no retry")

    def test_dead_supervisor_with_live_group_is_not_gone(self):
        """Failure table, Reap unsure: the supervisor pid is dead but its group (a git child) lives: no settle."""
        self.ended(supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START)
        kill = Kill(self.db)
        self.assertEqual(build.verdict(build.run(self.db, self.bundle["id"]), probe=Probe(alive=False, group=True)),
                         (build.UNSURE, []))
        self.assertEqual(build.reap(self.db, probe=Probe(alive=False, group=True), kill=kill), [])
        self.assertEqual((kill.calls, self.count("SELECT COUNT(*) FROM runner_run")), ([], 1))

    def test_a_supervisor_with_no_recorded_group_is_unsure(self):
        """Failure table, Reap unsure: a dead supervisor pid with no group recorded may have left children."""
        self.ended(supervisor_pid=4242, supervisor_start=START)
        self.assertEqual(build.reap(self.db, probe=Probe(alive=False), kill=Kill(self.db)), [])
        self.assertIsNone(build.run(self.db, self.bundle["id"])["released_at"])

    def test_a_cancel_waits_for_the_supervisor_group(self):
        """Failure table, Cancel: a cancelled run whose dead supervisor's group lives is not settled; items stay."""
        build.write(self.db, self.bundle["id"], supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START)
        state = operations.assignment_state(self.db, self.bundle["assignment"])
        operations.cancel_assignment(self.db, self.bundle["assignment"], expected_revision=state["revision"],
                                     who="operator")
        self.assertEqual(build.reap(self.db, now=self.stale(), probe=Probe(alive=False, group=True)), [])
        self.assertEqual(self.statuses(self.items), ["in_progress", "in_progress"])
        settled = build.reap(self.db, now=self.stale(), probe=Probe(alive=False))
        self.assertEqual([(row["outcome"], row["retry"]) for row in settled], [("cancelled", None)])

    def test_a_held_run_waits_for_the_supervisor_group(self):
        """Failure table, Blocked: `_release` asks `verdict`, so a live supervisor group keeps the lease."""
        self.ended(supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START, builder_pgid=777,
                   builder_start=START)
        build.reap(self.db, probe=Probe(group=True), kill=Kill(self.db, gone=False))
        build.reap(self.db, probe=Probe(alive=False, group=True), kill=Kill(self.db))
        self.assertIsNone(build.run(self.db, self.bundle["id"])["released_at"])

    def test_promotion_keeps_build_leases(self):
        """Failure table, Promote: the runner's leases are released; a build run's stays until a reap reads it gone."""
        self.assertEqual(backup._release_leases(self.db), [])
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 1)

    def test_a_reimport_pending_holds_the_reap(self):
        """Failure table, Restore: a run the restore did not see is left alone until the reimport ends."""
        record_state(self.db, "restore", body={})
        kill = Kill(self.db)
        self.assertEqual(build.reap(self.db, now=self.stale(), probe=Probe(), kill=kill), [])
        self.assertIsNone(build.run(self.db, self.bundle["id"])["outcome"])

    def test_a_bundle_with_no_running_member_is_released_not_retried(self):
        """Failure table, Retry: members blocked by hand under an open run: released, no retry, no duplicate run."""
        self.db.execute("UPDATE assignment SET status = 'blocked' WHERE scope = 'build'")
        settled = build.reap(self.db, now=self.stale(), probe=Probe())
        self.assertEqual([(row["outcome"], row["retry"]) for row in settled], [("interrupted", None)])
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run WHERE released_at IS NULL"), 0)

    def test_a_retry_numbers_after_the_leads_last_run(self):
        """Failure table, Retry: a run count behind the lead's runs still gives the retry a fresh number."""
        self.db.execute("UPDATE assignment SET run_count = 0 WHERE scope = 'build'")
        retry = build.run(self.db, build.reap(self.db, now=self.stale(), probe=Probe())[0]["retry"])
        self.assertEqual(retry["run"], 2)
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment WHERE scope = 'build' AND run_count = 2"), 2)

    def test_cancel_dead_supervisor(self):
        """Failure table, Cancel: the supervisor is dead at cancel; the reap ends the bundle cancelled, not retried."""
        lead = self.bundle["assignment"]
        member = self.bundle["members"][1]["id"]
        state = operations.assignment_state(self.db, member)
        self.assertTrue(state["capabilities"]["cancel"]["allowed"])
        operations.cancel_assignment(self.db, member, expected_revision=state["revision"], who="operator")
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment WHERE status = 'running'"), 2,
                         "the cancel is asked, not done")
        settled = build.reap(self.db, now=self.stale())
        self.assertEqual([(row["outcome"], row["retry"]) for row in settled], [("cancelled", None)])
        self.assertEqual([m["status"] for m in build.run(self.db, self.bundle["id"])["members"]], ["cancelled", "cancelled"])
        self.assertEqual(self.statuses(self.items), ["ready", "ready"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run WHERE assignment = ?", lead), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_lease WHERE released_at IS NULL"), 0)

    def test_a_running_build_names_its_cancel_in_the_status_refusal(self):
        with self.assertRaisesRegex(workflow.TransitionRefused,
                                    f"cancel running build assignment {self.bundle['assignment']} first"):
            workflow.change_status(self.db, self.items[0], "blocked", who="operator")


class Park(Case):
    """pack sd:6132 (operator ruling #32107): a build run that ends blocked parks its items once its lease is
    released, as the nightly prune parks an idle item (sd:6008); a run that ends done or retried parks nothing."""

    def setUp(self):
        super().setUp()
        self.items = [self.item(), self.item()]
        build.queue(self.db, self.items, who="operator")
        self.bundle = self.claim()

    def parked(self):
        return [item_state(self.db, item)["item"]["parked_at"] is not None for item in self.items]

    def lose(self, row):
        return build.reap(self.db, now=later(row["created_at"], build.STALE_SECONDS + 1))[0]

    def test_a_second_loss_parks_the_blocked_items_and_a_retry_parks_none(self):
        build.write(self.db, self.bundle["id"], step="building")
        retry = self.lose(self.bundle)["retry"]
        self.assertEqual(self.parked(), [False, False], "a retried run parks nothing")
        self.assertIsNone(self.lose(build.run(self.db, retry))["retry"])
        self.assertEqual((self.statuses(self.items), self.parked()), (["blocked", "blocked"], [True, True]))
        note = self.db.execute("SELECT body FROM note WHERE item = ? AND kind = 'comment' ORDER BY id DESC",
                               (self.items[0],)).fetchone()
        self.assertEqual(note["body"], build.PARKED.format(run=retry))

    def test_a_held_blocked_run_parks_its_items_only_when_its_lease_is_released(self):
        """A supervisor that ends blocked holds the lease until its groups read gone; the park waits for that, and an
        item the operator moved on in the meantime stays unparked."""
        build.write(self.db, self.bundle["id"], supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START,
                    builder_pgid=777, builder_start=START)
        build.end(self.db, self.bundle["id"], "blocked", "a permission denial")
        self.assertEqual((self.statuses(self.items), self.parked()), (["blocked", "blocked"], [False, False]))
        workflow.change_status(self.db, self.items[1], "ready", who="operator")
        build.reap(self.db, probe=Probe(alive=False, group=False), kill=Kill(self.db))
        self.assertIsNotNone(build.run(self.db, self.bundle["id"])["released_at"])
        self.assertEqual(self.parked(), [True, False])

    def test_a_held_blocked_run_cancels_a_done_items_assignment_when_its_lease_is_released(self):
        """sd:6996. An item merged by hand while its blocked run still held the lease keeps the row blocked until the
        reap releases it; the release then cancels it, and the open item's row stays blocked."""
        build.write(self.db, self.bundle["id"], supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START,
                    builder_pgid=777, builder_start=START)
        build.end(self.db, self.bundle["id"], "blocked", "a permission denial")
        transition(self.db, self.items[0], "done", who="operator")
        rows = "SELECT status FROM assignment WHERE item = ? AND scope = 'build'"
        self.assertEqual(self.db.execute(rows, (self.items[0],)).fetchone()[0], "blocked")
        build.reap(self.db, probe=Probe(alive=False, group=False), kill=Kill(self.db))
        self.assertEqual(self.db.execute(rows, (self.items[0],)).fetchone()[0], "cancelled")
        self.assertEqual(self.db.execute(rows, (self.items[1],)).fetchone()[0], "blocked")

    def test_a_run_that_ends_done_parks_nothing(self):
        build.end(self.db, self.bundle["id"], "done", "the lane merged it")
        self.assertEqual(self.parked(), [False, False])

    def test_a_park_never_unparks_or_restamps_an_item(self):
        """An item parked already keeps its first stamp and gets no second note."""
        build.write(self.db, self.bundle["id"], step="building")
        retry = self.lose(self.bundle)["retry"]
        workflow.edit_item(self.db, self.items[0], {"parked_at": "2026-10-01T00:00:00+00:00"}, who="operator")
        self.lose(build.run(self.db, retry))
        self.assertEqual(item_state(self.db, self.items[0])["item"]["parked_at"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(self.count("SELECT COUNT(*) FROM note WHERE item = ? AND body LIKE 'Parked: build run%'",
                                    self.items[0]), 0)


class Race(Case):
    """Race table: a writer runs between the reap's read and its compare-and-set; the reap must do nothing then."""

    def setUp(self):
        super().setUp()
        self.items = [self.item(), self.item()]
        build.queue(self.db, self.items, who="operator")
        self.bundle = self.claim()
        self.stale = later(self.bundle["created_at"], build.STALE_SECONDS + 1)

    def reap_after(self, writer, probe=None):
        """One reap whose `open_runs` read is followed by `writer`, as a supervisor or an operator landing then."""
        read = build.open_runs

        def snapshot(connection, hosts=None):
            rows = read(connection, hosts)
            writer()
            return rows

        kill = Kill(self.db)
        with mock.patch.object(build, "open_runs", snapshot):
            settled = build.reap(self.db, now=self.stale, probe=probe or Probe(alive=False), kill=kill)
        return settled, kill

    def test_enqueued_between_read_and_reap_wins(self):
        build.write(self.db, self.bundle["id"], step="enqueuing")
        settled, kill = self.reap_after(lambda: build.enqueued(self.db, self.bundle["id"]))
        row = build.run(self.db, self.bundle["id"])
        self.assertEqual((settled, kill.calls, row["step"], row["outcome"], row["released_at"]),
                         ([], [], "enqueued", None, None))
        self.assertEqual(self.count("SELECT COUNT(*) FROM runner_run"), 1, "no retry")

    def test_cancel_between_read_and_reap_wins(self):
        def cancel():
            state = operations.assignment_state(self.db, self.bundle["assignment"])
            operations.cancel_assignment(self.db, self.bundle["assignment"], expected_revision=state["revision"],
                                         who="operator")

        settled, _ = self.reap_after(cancel)
        self.assertEqual(settled, [])
        settled = build.reap(self.db, now=self.stale, probe=Probe(alive=False))
        self.assertEqual([(row["outcome"], row["retry"]) for row in settled], [("cancelled", None)])
        self.assertEqual(self.statuses(self.items), ["ready", "ready"])

    def test_cancel_after_the_compare_and_set_still_wins(self):
        """The cancel lands on the ended, unreleased run: its settle ends it cancelled, no fresh row."""
        self.db.execute("UPDATE runner_run SET outcome = 'interrupted' WHERE id = ?", (self.bundle["id"],))
        state = operations.assignment_state(self.db, self.bundle["assignment"])
        operations.cancel_assignment(self.db, self.bundle["assignment"], expected_revision=state["revision"],
                                     who="operator")
        settled = build.reap(self.db, now=self.stale, probe=Probe(alive=False))
        self.assertEqual([row["retry"] for row in settled], [None])
        self.assertEqual(self.statuses(self.items), ["ready", "ready"])

    def test_supervisor_registration_between_read_and_reap_wins(self):
        settled, kill = self.reap_after(lambda: build.write(
            self.db, self.bundle["id"], supervisor_pid=4242, supervisor_pgid=4242, supervisor_start=START))
        self.assertEqual((settled, kill.calls, build.run(self.db, self.bundle["id"])["outcome"]), ([], [], None))

    def test_builder_registration_between_read_and_reap_wins(self):
        settled, kill = self.reap_after(lambda: build.write(
            self.db, self.bundle["id"], builder_pgid=777, builder_start=START))
        self.assertEqual((settled, kill.calls, build.run(self.db, self.bundle["id"])["outcome"]), ([], [], None))

    def test_a_step_between_read_and_reap_wins(self):
        settled, _ = self.reap_after(lambda: build.write(self.db, self.bundle["id"], step="building"))
        self.assertEqual((settled, build.run(self.db, self.bundle["id"])["outcome"]), ([], None))

    def test_a_beat_between_read_and_reap_wins(self):
        settled, _ = self.reap_after(lambda: build.beat(self.db, self.bundle["id"]))
        self.assertEqual((settled, build.run(self.db, self.bundle["id"])["outcome"]), ([], None))


class Backup(Case):
    """Guard: the backup's journal check covers the runner's runs; a build run keeps no journal."""

    def snapshot(self) -> Path:
        """The store's copy in a backup directory, as `_snapshot` leaves it: WAL pages included."""
        directory = self.root / "backup"
        directory.mkdir()
        copy = sqlite3.connect(directory / self.database.name)
        self.db.backup(copy)
        copy.close()
        return directory

    def test_a_build_run_needs_no_journal(self):
        build.queue(self.db, [self.item()], who="operator")
        self.claim()
        directory = self.snapshot()
        self.assertFalse(backup._copy_runner_journal(self.database, directory, {"runner_run": 1}))
        backup._check_runner_records(self.db, directory)

    def test_a_teammate_run_needs_no_journal(self):
        """pack sd:7221: a lead's teammate run, open or released, has no runner journal, and the backup passes."""
        item = self.item(status="in_progress")
        for released in (None, "2026-11-02T11:00:00+00:00"):
            self.db.execute("INSERT INTO assignment (item, role, status, scope) VALUES (?, 'author', 'in_session', "
                            "'teammate')", (item,))
            lead = self.db.execute("SELECT MAX(id) FROM assignment").fetchone()[0]
            self.db.execute("INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, "
                            "retained_path, created_at, updated_at, released_at) "
                            "VALUES (?, ?, 1, ?, 'feat/x', 'lead', '/w', '/w', '', '', ?)",
                            (f"teammate{lead}", lead, A, released))
        directory = self.snapshot()
        self.assertFalse(backup._copy_runner_journal(self.database, directory, {"runner_run": 2}))
        backup._check_runner_records(self.db, directory)

    def test_a_draft_run_needs_no_journal(self):
        """pack sd:7282: a draft run, running or released, has no runner journal, and the backup passes."""
        item = self.item(status="planning")
        for status, released in (("running", None), ("done", "2026-11-02T11:00:00+00:00")):
            self.db.execute("INSERT INTO assignment (item, role, status, scope) VALUES (?, 'author', ?, 'draft')",
                            (item, status))
            draft = self.db.execute("SELECT MAX(id) FROM assignment").fetchone()[0]
            self.db.execute("INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, "
                            "retained_path, created_at, updated_at, released_at) "
                            "VALUES (?, ?, 1, ?, ?, 'build-dispatch', ?, ?, '', '', ?)",
                            (f"draft{draft}", draft, A, f"draft/sd-{item}", A, A, released))
        directory = self.snapshot()
        self.assertFalse(backup._copy_runner_journal(self.database, directory, {"runner_run": 2}))
        backup._check_runner_records(self.db, directory)

    def test_the_unjournaled_scopes_are_the_build_draft_and_teammate_scopes(self):
        """pack sd:7282: one exemption, read by the backup and the merge guard, names each scope no runner supervises."""
        from sd_db import draft, teammate
        self.assertEqual(set(runner.UNSUPERVISED_SCOPES), {build.SCOPE, draft.SCOPE, teammate.SCOPE})

    def test_a_runner_run_still_needs_its_journal(self):
        self.db.execute("INSERT INTO assignment (item, role, status, scope) VALUES (?, 'author', 'blocked', 'item')",
                        (self.item(status="planning"),))
        old = self.db.execute("SELECT MAX(id) FROM assignment").fetchone()[0]
        self.db.execute("INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, "
                        "created_at, updated_at, released_at) VALUES ('old', ?, 1, ?, 'w', 'runner', '/w', '/r', '', '', 'x')",
                        (old, A))
        directory = self.snapshot()
        with self.assertRaisesRegex(BackupError, "durable journal is missing"):
            backup._copy_runner_journal(self.database, directory, {"runner_run": 1})


class Pickup(Case):
    """pack sd:6097, design "Auto-pickup": timed pickup of ready items, and the rules that turn it off."""

    NOW = "2026-11-03T20:00:00+00:00"

    def on(self):
        return build.set_pickup(self.db, True, now=self.NOW)

    def room(self, **inputs):
        return build.room(self.db, **{**IDLE, **inputs})

    def test_on_ends_at_the_next_nine_in_denver(self):
        """pack sd:7698: On runs until the next 09:00 America/Denver."""
        self.assertEqual(build.morning("2026-11-05T15:00:00+00:00"), "2026-11-05T16:00:00+00:00",
                         "On at 08:00 MST ends 09:00 the same day")
        self.assertEqual(build.morning("2026-11-05T16:30:00+00:00"), "2026-11-06T16:00:00+00:00",
                         "On at 09:30 MST ends 09:00 the next day")
        self.assertEqual(build.morning("2026-11-05T16:00:00+00:00"), "2026-11-06T16:00:00+00:00", "09:00 itself is past")
        self.assertEqual(build.morning("2026-11-03T20:00:00+00:00"), "2026-11-04T16:00:00+00:00", "MST: 09:00 is 16:00Z")
        self.assertEqual(build.morning("2026-07-01T20:00:00+00:00"), "2026-07-02T15:00:00+00:00", "MDT: 15:00Z")
        state = self.on()
        self.assertEqual((state["on"], state["until"]), (True, "2026-11-04T16:00:00+00:00"))
        self.assertFalse(build.pickup_state(self.db, now="2026-11-04T16:00:00+00:00")["on"], "the end is off")
        self.assertEqual(build.set_pickup(self.db, False, now=self.NOW)["until"], None)
        set_setting(self.db, build.AUTO_PICKUP, "tomorrow")
        self.assertFalse(build.pickup_state(self.db, now=self.NOW)["on"], "a value that does not read is off")

    def test_pickup_fills_the_room_by_priority_while_on(self):
        a = [self.item(priority=n) for n in (1, 2, 3, 4)]
        b = self.item(repo=B, priority=5)
        self.item(status="planning", priority=0)
        self.assertEqual(build.pickup(self.db, self.room(), now=self.NOW), [], "off picks nothing")
        self.on()
        self.assertEqual(build.pickup(self.db, self.room(), now=self.NOW), [*a[:build.BUNDLE_MAX], b],
                         "one bundle a repository: A's fourth waits for the next")
        self.assertTrue(all(build.picked(self.db, row["id"]) for row in
                            self.db.execute("SELECT id FROM assignment WHERE scope = 'build'")))
        self.assertEqual(build.pickup(self.db, self.room(), now=self.NOW), [], "a queued build is claimed first")
        self.assertEqual([len(build.run(self.db, self.claim()["id"])["members"]) for _ in range(2)], [3, 1])

    def test_pickup_keeps_to_the_room(self):
        items = [self.item(priority=n) for n in range(5)]
        self.on()
        self.assertEqual(build.pickup(self.db, self.room(load=50.0), now=self.NOW), [], "no room, no pickup")
        self.assertEqual(build.pickup(self.db, self.room(interactive=[{"repo": B, "session_id": None}] * 5),
                                      now=self.NOW), items[:3], "one slot left: one bundle")
        self.assertFalse(build.picked(self.db, build.queue(self.db, [items[3]], who="operator")[0]["id"]))

    def test_a_press_waiting_on_a_full_repository_holds_no_other_repository(self):
        """Pack sd:8158: at one bundle per repo a press can wait for a whole bundle; only a press with room holds."""
        build.queue(self.db, [self.item()], who="operator")
        b = self.item(repo=B)
        self.on()
        busy = self.room(interactive=[{"repo": A, "session_id": "s1"}])
        self.assertEqual(build.room_for(busy, A), 0)
        self.assertEqual(build.pickup(self.db, busy, now=self.NOW), [b], "A's waiting press holds nothing in B")
        self.assertEqual(build.pickup(self.db, self.room(), now=self.NOW), [], "a press with room is claimed first")

    def end(self, status):
        item = self.item()
        lead = build.queue(self.db, [item], who=build.PICKUP)[0]["id"]
        self.db.execute("UPDATE assignment SET status = ?, ended = ? WHERE id = ?",
                        (status, later(build.clock(), self.count("SELECT COUNT(*) FROM assignment")), lead))
        return item

    def test_two_blocked_runs_turn_off(self):
        """Failure table, Two blocked: the next tick counts the last two auto-pickup outcomes and writes off."""
        self.on()
        self.end("blocked")
        self.end("done")
        self.end("blocked")
        self.assertIsNone(build.stop_on_blocks(self.db, now=self.NOW), "a done between breaks the row")
        operator = self.item()
        lead = build.queue(self.db, [operator], who="operator")[0]["id"]
        self.db.execute("UPDATE assignment SET status = 'blocked', ended = ? WHERE id = ?",
                        (later(build.clock(), 90), lead))
        self.assertIsNone(build.stop_on_blocks(self.db, now=self.NOW), "a pressed build is not auto-pickup's")
        last = self.end("blocked")
        self.assertIn("ended blocked in a row", build.stop_on_blocks(self.db, now=self.NOW) or "")
        self.assertFalse(build.pickup_state(self.db, now=self.NOW)["on"])
        note = self.db.execute("SELECT body FROM note WHERE item = ? ORDER BY id DESC", (last,)).fetchone()
        self.assertIn("Auto-pickup is off", note["body"])
        self.on()
        self.db.execute("UPDATE state SET timestamp = ? WHERE id = (SELECT MAX(id) FROM state)",
                        (later(build.clock(), 3600),))
        self.assertIsNone(build.stop_on_blocks(self.db, now=self.NOW), "On again counts only what ends after it")

    def picked_run(self):
        item = self.item()
        set_setting(self.db, build.AUTO_PICKUP, "2026-11-04T14:00:00+00:00")
        build.queue(self.db, [item], who=build.PICKUP)
        bundle = self.claim()
        build.write(self.db, bundle["id"], step="enqueuing")
        build.enqueued(self.db, bundle["id"])
        return item, build.run(self.db, bundle["id"])

    def test_an_auto_pickup_merge_goes_to_checking(self):
        item, run = self.picked_run()
        self.assertTrue(build.merged(self.db, run))
        self.assertEqual(build.run(self.db, run["id"])["step"], "checking")

    def test_health_failure_turns_off_first(self):
        """Failure table, Checking: off and the filed item land together; a check run again files nothing twice."""
        item, run = self.picked_run()
        filed = build.health_failed(self.db, run["id"], "`dashboard.sh health` exit 1")
        self.assertEqual(read_settings(self.db, [build.AUTO_PICKUP]), {build.AUTO_PICKUP: None})
        row = self.db.execute("SELECT title, repo FROM item WHERE id = ?", (filed,)).fetchone()
        self.assertEqual((row["title"], row["repo"]), (build.HEALTH + run["branch"], A))
        self.assertEqual(build.health_failed(self.db, run["id"], "again"), filed)
        self.assertEqual(self.count("SELECT COUNT(*) FROM item WHERE title LIKE 'Auto-pickup off%'"), 1)


SAT = "/fixture/sat"
HUB, WORK = frozenset({None, "hub"}), frozenset({"work"})


class Hosts(Case):
    """Pack sd:7933: each host's dispatcher claims, counts, reaps and releases only its own lanes' runs."""

    def setUp(self):
        super().setUp()
        upsert_repo(self.db, SAT, remote="/fixture/remote-sat", managed=1)
        self.db.execute("UPDATE repo SET lane_host = 'work' WHERE path = ?", (SAT,))
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={})

    def satellite_run(self):
        build.queue(self.db, [self.item(repo=SAT)], who="operator")
        run = self.claim(hosts=WORK)
        assert run is not None
        return run

    def test_lane_hosts_is_the_hub_and_this_name_or_this_name_alone(self):
        with mock.patch.object(build, "this_host", return_value="work"), \
                mock.patch("sd_db.hub.configured", return_value=True):
            with mock.patch.object(build, "served_by", return_value=None):
                self.assertEqual(build.lane_hosts(self.db), {None, "work"})
            with mock.patch.object(build, "served_by", return_value="hub.example.test:7000"):
                self.assertEqual(build.lane_hosts(self.db), {"work"})
        self.assertEqual([build.run_host(owner) for owner in ("build-dispatch", "build-dispatch@work", None, "x@")],
                         [None, "work", None, None])

    def test_the_hub_over_the_wire_is_still_the_hub(self):
        """Review round 1: hub.json decides, not the transport; the wire harness serves the hub's own tests."""
        wire = mock.Mock(spec=remote.Connection, host="127.0.0.1", port=7000)
        with mock.patch.object(build, "this_host", return_value="hub"):
            with mock.patch("sd_db.hub.configured", return_value=False):
                self.assertEqual(build.lane_hosts(wire), {None, "hub"}, "no hub.json: the hub, over the wire too")
            with mock.patch("sd_db.hub.configured", return_value=True):
                self.assertEqual(build.lane_hosts(wire), {"hub"}, "hub.json and a served connection: a satellite")
                self.assertEqual(build.lane_hosts(mock.Mock(spec=sqlite3.Connection)), {None, "hub"},
                                 "a local database is a hub's")

    def test_a_satellite_press_leaves_the_hub_pickup_running(self):
        """Review round 1: the hub never claims a satellite's press, so it must not hold the hub's pickup."""
        build.queue(self.db, [self.item(repo=SAT)], who="operator")
        ready = self.item()
        build.set_pickup(self.db, True, now=Pickup.NOW)
        with mock.patch.object(build, "this_host", return_value="hub"):
            self.assertEqual(build.pickup(self.db, build.room(self.db, **IDLE, hosts=HUB), now=Pickup.NOW), [ready])

    def test_the_hub_claim_skips_a_satellite_row(self):
        build.queue(self.db, [self.item(repo=SAT)], who="operator")
        self.assertIsNone(self.claim(hosts=HUB))
        self.assertEqual(self.count("SELECT status FROM assignment"), "queued", "skipped, not blocked")
        self.assertEqual(self.count("SELECT COUNT(*) FROM note WHERE body LIKE '%blocked%'"), 0)

    def test_a_satellite_claims_only_its_lane(self):
        hub = build.queue(self.db, [self.item()], who="operator")
        run = self.satellite_run()
        self.assertEqual((run["repo"], run["owner"]), (SAT, "fixture@work"))
        self.assertIsNone(self.claim(hosts=WORK), "the hub's row is not the satellite's")
        self.assertEqual(self.count("SELECT status FROM assignment WHERE id = ?", hub[0]["id"]), "queued")
        self.assertEqual(self.claim(hosts=HUB)["owner"], "fixture", "the hub's run keeps the role as its owner")

    def test_a_lane_move_before_the_claim_moves_the_claim(self):
        build.queue(self.db, [self.item(repo=SAT)], who="operator")
        self.db.execute("UPDATE repo SET lane_host = NULL WHERE path = ?", (SAT,))
        self.assertIsNone(self.claim(hosts=WORK))
        self.assertEqual(self.claim(hosts=HUB)["owner"], "fixture")

    def test_room_counts_only_this_hosts_runs(self):
        self.satellite_run()
        build.queue(self.db, [self.item()], who="operator")
        self.claim(hosts=HUB)
        for hosts, repo in ((HUB, A), (WORK, SAT)):
            with self.subTest(hosts=hosts):
                room = build.room(self.db, **IDLE, hosts=hosts)
                self.assertEqual((room["machine"], room["used"]), (build.MACHINE_CAP - 1, {repo: 1}))

    def test_auto_pickup_skips_a_satellite_lane(self):
        hub, satellite = self.item(), self.item(repo=SAT)
        self.assertEqual([row["id"] for row in build.eligible(self.db)], [hub])
        with self.assertRaisesRegex(build.BuildRefused, "auto-pickup and drafts run on the hub only"):
            build._item(self.db, satellite, hub_only=True)

    def test_the_hub_never_signals_or_reaps_a_satellite_run(self):
        """The hazard: the hub would probe pid 4242 here and kill group 777, which name a satellite's processes."""
        run = self.satellite_run()
        build.register(self.db, run["id"], pid=4242, pgid=4242, start=START)
        build.write(self.db, run["id"], builder_pgid=777, builder_start=START)
        stale = later(run["created_at"], build.STALE_SECONDS + 1)

        class Asked(Probe):
            calls: list = []

            def alive(self, pid):
                self.calls.append(("alive", pid))
                return False

            def group(self, pgid):
                self.calls.append(("group", pgid))
                return False

        probe, kill = Asked(), Kill(self.db)
        self.assertEqual(build.reap(self.db, now=stale, probe=probe, kill=kill, hosts=HUB), [])
        self.db.execute("UPDATE runner_run SET outcome = 'blocked' WHERE id = ?", (run["id"],))
        self.assertEqual(build.reap(self.db, now=stale, probe=probe, kill=kill, hosts=HUB), [])
        self.assertEqual((probe.calls, kill.calls), ([], []), "nothing of the satellite's is probed or killed")
        self.assertIsNone(build.run(self.db, run["id"])["released_at"])
        build.reap(self.db, now=stale, probe=Probe(alive=False), kill=kill, hosts=WORK)
        self.assertIsNotNone(build.run(self.db, run["id"])["released_at"], "the satellite releases its own")

    def test_a_retry_keeps_the_satellite_owner(self):
        run = self.satellite_run()
        stale = later(run["created_at"], build.STALE_SECONDS + 1)
        retry = build.reap(self.db, now=stale, probe=Probe(alive=False), hosts=WORK)[0]["retry"]
        self.assertEqual(build.run(self.db, retry)["owner"], "fixture@work")

    # Review round 3, class A: a lane named for the hub is the hub's, by one rule (`build.lane_of`).

    def as_hub(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(build, "this_host", return_value="hub"))
        stack.enter_context(mock.patch("sd_db.hub.configured", return_value=False))
        return stack

    def hub_named(self, status="ready"):
        upsert_repo(self.db, "/fixture/hubnamed", remote="/fixture/remote-hubnamed", managed=1)
        self.db.execute("UPDATE repo SET lane_host = 'hub' WHERE path = '/fixture/hubnamed'")
        return self.item(repo="/fixture/hubnamed", status=status)

    def test_lane_of_reads_this_hubs_own_name_as_the_hub(self):
        with self.as_hub():
            self.assertEqual([build.lane_of(self.db, host) for host in (None, "hub", "work")], [None, None, "work"])
        with self.as_satellite():
            self.assertEqual([build.lane_of(self.db, host) for host in (None, "work")], [None, "work"],
                             "a satellite's own lane is a satellite's")

    def as_satellite(self, names="Hub.example.test"):
        """This machine as the satellite `work`, served by the hub its own `hub.json` names (pack sd:7955)."""
        home = self.root / "satellite-home"
        (home / ".config/sd").mkdir(parents=True, exist_ok=True)
        (home / ".config/sd/hub.json").write_text(json.dumps({"hub": names, "port": 7000}))
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict("os.environ", {"HOME": str(home)}))
        stack.enter_context(mock.patch.object(build, "this_host", return_value="work"))
        stack.enter_context(mock.patch("sd_db.hub.configured", return_value=True))
        stack.enter_context(mock.patch.object(build, "served_by", return_value=f"{names}:7000"))
        return stack

    def test_a_satellite_reads_the_lane_named_for_its_hub_as_the_hubs(self):
        """Pack sd:7955: the hub's name is the first label of the host `hub.json` names, so a satellite press
        and a satellite session's draft take a lane named for the hub, and the satellite claims none of it."""
        item = self.hub_named()
        with self.as_satellite():
            self.assertEqual([build.lane_of(self.db, host) for host in (None, "hub", "work", "other")],
                             [None, None, "work", "other"])
            self.assertEqual(build.item_host(self.db, item), None)
            self.assertEqual(len(build.queue(self.db, [item], who="operator")), 1, "no build-dispatch@hub beat needed")
            self.assertIsNotNone(draft.queue_if_eligible(self.db, self.hub_named(status="planning"), who="dashboard"))
            self.assertIsNone(self.claim(), "the satellite claims none of the hub's lane")
        with self.as_satellite(names="192.0.2.10"):
            self.assertEqual(build.lane_of(self.db, "hub"), "hub", "an address names no lane: another host's")

    def test_a_lane_named_for_the_hub_takes_a_press_pickup_and_draft_on_the_hub(self):
        with self.as_hub():
            item = self.hub_named()
            self.assertIn(item, [row["id"] for row in build.eligible(self.db)], "auto-pickup takes it")
            self.assertEqual(len(build.queue(self.db, [item], who="operator")), 1, "no build-dispatch@hub beat needed")
            self.assertIsNotNone(draft.queue_if_eligible(self.db, self.hub_named(status="planning"), who="dashboard"))

    def test_a_lane_named_for_the_hub_claims_with_the_hubs_bare_owner(self):
        with self.as_hub():
            build.queue(self.db, [self.hub_named()], who="operator")
            run = self.claim()
        assert run is not None
        self.assertEqual(run["owner"], "fixture", "the role, so the panel reads its log and a failover keeps it")

    # Review round 3, class B: once a satellite's dispatcher goes quiet, the hub settles its rows and runs.

    def quiet(self, host="work"):
        stamp = (datetime.fromisoformat(build.clock()) - timedelta(seconds=build.STALE_SECONDS + 1)).isoformat()
        record_state(self.db, "heartbeat", key=f"build-dispatch@{host}", timestamp=stamp, body={})

    def test_the_hub_blocks_a_quiet_satellites_queued_row_with_why(self):
        item = self.item(repo=SAT)
        lead = build.queue(self.db, [item], who="operator")[0]["id"]
        self.assertIsNone(self.claim(hosts=HUB))
        self.assertEqual(self.count("SELECT status FROM assignment WHERE id = ?", lead), "queued", "a live beat waits")
        self.quiet()
        self.assertIsNone(self.claim(hosts=HUB))
        self.assertEqual(self.count("SELECT status FROM assignment WHERE id = ?", lead), "blocked")
        self.assertIn("lane runs on work, and no build-dispatch@work beat",
                      self.count("SELECT body FROM note WHERE item = ? ORDER BY id DESC LIMIT 1", item))
        self.assertEqual(self.statuses([item]), ["ready"], "the item is free again")

    def test_the_hub_blocks_a_row_whose_satellite_beat_comes_from_a_clock_ahead(self):
        """Pack sd:7955: a beat stamped more than STALE_SECONDS after now has no age to trust, so the claim blocks
        the row as for a stale beat, and its note names the skew, not a dead dispatcher."""
        item = self.item(repo=SAT)
        lead = build.queue(self.db, [item], who="operator")[0]["id"]
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={},
                     timestamp=later(build.clock(), build.STALE_SECONDS + 60))
        self.assertIsNone(build.dispatcher_beat(self.db, "work"), "no negative age")
        self.assertIsNone(self.claim(hosts=HUB))
        self.assertEqual(self.count("SELECT status FROM assignment WHERE id = ?", lead), "blocked")
        note = self.count("SELECT body FROM note WHERE item = ? ORDER BY id DESC LIMIT 1", item)
        self.assertIn("lane runs on work, and the build-dispatch@work beat is", note)
        self.assertIn("s ahead of this clock", note)
        self.assertNotIn("no dispatcher", note)

    def test_a_beat_ahead_by_up_to_stale_seconds_is_fresh_for_the_press_and_the_claim(self):
        """Reviews of e36844044 and dfd937c37: beats are stamped on the satellite's clock, so a lead up to
        `STALE_SECONDS`, as long as staleness allows behind, is clock skew and reads as age 0."""
        for lead in (1, 5, 60, build.STALE_SECONDS):
            with self.subTest(lead=lead):
                record_state(self.db, "heartbeat", key="build-dispatch@work", timestamp=later(build.clock(), lead),
                             body={})
                self.assertEqual(build.dispatcher_beat(self.db, "work"), 0)
                item = self.item(repo=SAT)
                queued = build.queue(self.db, [item], who="operator")[0]["id"]
                self.assertIsNone(self.claim(hosts=HUB))
                self.assertEqual(self.count("SELECT status FROM assignment WHERE id = ?", queued), "queued",
                                 "the hub's claim leaves a healthy satellite's row queued")

    def test_the_hub_blocks_a_row_whose_lane_moved_to_a_host_that_never_beat(self):
        item = self.item()
        lead = build.queue(self.db, [item], who="operator")[0]["id"]
        self.db.execute("UPDATE repo SET lane_host = 'gone' WHERE path = ?", (A,))
        self.assertIsNone(self.claim(hosts=HUB))
        self.assertEqual(self.count("SELECT status FROM assignment WHERE id = ?", lead), "blocked")

    # Review round 4: a fresh beat that names why its tick claims nothing is no dispatcher that would claim.

    UNSET = "sd.bulk_storage_root is unset, so a build run has no log folder"

    def test_a_press_refuses_a_satellite_whose_beat_says_it_claims_nothing(self):
        item = self.item(repo=SAT)
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={"pid": 1, "why": self.UNSET})
        with self.assertRaisesRegex(build.BuildRefused, f"lane runs on work, and build-dispatch@work claims "
                                                        f"nothing: {self.UNSET}"):
            build.queue(self.db, [item], who="operator")
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment WHERE item = ?", item), 0, "nothing locked")
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={"pid": 1, "why": ""})
        self.assertEqual(len(build.queue(self.db, [item], who="operator")), 1, "a beat that claims takes the press")

    def test_a_press_queues_on_a_fresh_beat_whose_count_did_not_read(self):
        """sd:8055: an unread load or builder count lasts one tick (15 of 265 beats under load), so the press
        queues and the satellite claims at its next counted beat; the Builds panel names the reason."""
        for why in build.UNREAD:
            with self.subTest(why=why):
                item = self.item(repo=SAT)
                record_state(self.db, "heartbeat", key="build-dispatch@work", body={"pid": 1, "why": why})
                lead = build.queue(self.db, [item], who="operator")[0]["id"]
                self.assertEqual(build.unclaimed(self.db), {str(lead): f"work claims nothing: {why}"})
                self.db.execute("UPDATE assignment SET status = 'cancelled' WHERE id = ?", (lead,))

    def test_a_queued_row_whose_satellite_claims_nothing_stays_queued(self):
        """Review round 5: a `why` may last one tick (an unread load), so the hub's claim blocks only on a stale beat."""
        item = self.item(repo=SAT)
        lead = build.queue(self.db, [item], who="operator")[0]["id"]
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={"pid": 1, "why": self.UNSET})
        self.assertIsNone(self.claim(hosts=HUB))
        self.assertEqual(self.count("SELECT status FROM assignment WHERE id = ?", lead), "queued")
        self.assertEqual(build.dispatcher_why(self.db, "work"), self.UNSET)
        self.quiet()
        self.assertIsNone(self.claim(hosts=HUB))
        self.assertEqual(self.count("SELECT status FROM assignment WHERE id = ?", lead), "blocked", "a stale beat blocks")

    def test_a_queued_row_blocks_once_its_satellite_claims_nothing_tick_after_tick(self):
        """sd:8081: a read that fails every tick would leave the row queued with only `unclaimed` to show it."""
        item = self.item(repo=SAT)
        lead = build.queue(self.db, [item], who="operator")[0]["id"]
        for _ in range(build.STUCK_TICKS - 1):
            record_state(self.db, "heartbeat", key="build-dispatch@work", body={"pid": 1, "why": build.UNREAD[0]})
        self.assertIsNone(self.claim(hosts=HUB))
        self.assertEqual(self.count("SELECT status FROM assignment WHERE id = ?", lead), "queued",
                         "fewer ticks than STUCK_TICKS leave it queued")
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={"pid": 1, "why": build.UNREAD[0]})
        self.assertIsNone(self.claim(hosts=HUB))
        self.assertEqual(self.count("SELECT status FROM assignment WHERE id = ?", lead), "blocked")
        self.assertIn(f"claimed nothing for {build.STUCK_TICKS} ticks: {build.UNREAD[0]}",
                      self.count("SELECT result FROM assignment WHERE id = ?", lead))

    def test_a_clean_beat_between_unread_ticks_keeps_the_row_queued(self):
        item = self.item(repo=SAT)
        lead = build.queue(self.db, [item], who="operator")[0]["id"]
        for why in [build.UNREAD[0]] * (build.STUCK_TICKS - 1) + ["", build.UNREAD[1]]:
            record_state(self.db, "heartbeat", key="build-dispatch@work", body={"pid": 1, "why": why})
        self.assertIsNone(self.claim(hosts=HUB))
        self.assertEqual(self.count("SELECT status FROM assignment WHERE id = ?", lead), "queued")

    def test_a_satellite_that_beats_and_claims_nothing_still_owns_its_runs(self):
        """Its tick reaps before it claims, so the hub leaves its runs: the beat says it is there."""
        run = self.satellite_run()
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={"pid": 1, "why": self.UNSET})
        self.assertEqual(self.strand(run), [])
        self.assertIsNone(build.run(self.db, run["id"])["outcome"])

    def strand(self, run, *, after=build.STALE_SECONDS + 1):
        class Untouched(Probe):
            def alive(self, pid):
                raise AssertionError(f"probed pid {pid} of another machine")

            def group(self, pgid):
                raise AssertionError(f"probed group {pgid} of another machine")

        kill = Kill(self.db)
        settled = build.reap(self.db, now=later(run["created_at"], after), probe=Untouched(), kill=kill, hosts=HUB)
        self.assertEqual(kill.calls, [], "the hub kills nothing of another machine")
        return settled

    # Review round 4: a missing beat does not prove another machine's processes stopped. The hub marks the run
    # blocked with a reason and keeps its lease; only that host's own probe releases it.

    def lease_held(self, run):
        return self.count("SELECT COUNT(*) FROM runner_lease WHERE run = ? AND released_at IS NULL", run["id"]) == 1

    def test_the_hub_blocks_a_quiet_satellites_run_and_keeps_its_lease(self):
        run = self.satellite_run()
        item = run["members"][0]["item"]
        build.register(self.db, run["id"], pid=4242, pgid=4242, start=START)
        self.assertEqual(self.strand(run), [], "a live dispatcher there keeps it")
        self.quiet()
        self.assertEqual(self.strand(run, after=60), [], "a run that beat lately keeps it")
        [settled] = self.strand(run)
        self.assertEqual((settled["run"], settled["outcome"]), (run["id"], "blocked"))
        ended = build.run(self.db, run["id"])
        self.assertIn("no build-dispatch@work beat", ended["detail"])
        self.assertIsNone(ended["released_at"], "the hub cannot see work's processes stop")
        self.assertTrue(self.lease_held(run))
        self.assertEqual(self.statuses([item]), ["blocked"])
        self.assertIn(f"held by build run {run['id']} (blocked)", build._held(self.db, item) or "")
        self.assertEqual(self.strand(run), [], "a second hub reap leaves it as it is")
        self.assertTrue(self.lease_held(run))
        build.reap(self.db, now=later(run["created_at"], build.STALE_SECONDS + 1), probe=Probe(alive=False),
                   hosts=WORK)
        self.assertIsNotNone(build.run(self.db, run["id"])["released_at"], "work's own probe releases it")
        self.assertFalse(self.lease_held(run))

    def test_the_hub_strands_a_run_whose_beat_leads_past_stale_seconds(self):
        """Pack sd:7970: a run beat is stamped on its satellite's clock, so it takes the dispatcher beat's lead
        rule. A lead up to `STALE_SECONDS` is skew and reads as age 0; one further has no age to trust, so a
        quiet dispatcher strands the run, not a wall clock that has yet to catch up."""
        run = self.satellite_run()
        now = later(run["created_at"], build.STALE_SECONDS + 1)
        self.quiet()
        for lead in (60, build.STALE_SECONDS):
            with self.subTest(lead=lead):
                self.db.execute("UPDATE runner_run SET heartbeat_at = ? WHERE id = ?", (later(now, lead), run["id"]))
                self.assertEqual(self.strand(run), [], f"a beat {lead} s ahead is a fresh beat")
                self.assertIsNone(build.run(self.db, run["id"])["outcome"])
        self.db.execute("UPDATE runner_run SET heartbeat_at = ? WHERE id = ?", (later(now, 3600), run["id"]))
        [settled] = self.strand(run)
        self.assertEqual((settled["run"], settled["outcome"]), (run["id"], "blocked"))
        self.assertIsNone(build.run(self.db, run["id"])["released_at"], "the lease stays until work reads it gone")
        self.assertTrue(self.lease_held(run))

    def test_the_hub_leaves_the_runs_of_a_satellite_whose_clock_runs_fast(self):
        """Review of 563ab61fd: a dispatcher beat too far ahead names skew, not a dead dispatcher. That host's
        ticks still beat and reap its own runs, so the hub strands none of them, run beat ahead or not."""
        run = self.satellite_run()
        now = later(run["created_at"], build.STALE_SECONDS + 1)
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={},
                     timestamp=later(build.clock(), 3600))
        self.assertIsNotNone(build.dispatcher_gone(self.db, "work"), "the press and the claim still refuse")
        for beat in (later(now, 3600), None):
            with self.subTest(beat=beat):
                self.db.execute("UPDATE runner_run SET heartbeat_at = ? WHERE id = ?", (beat, run["id"]))
                self.assertEqual(self.strand(run), [])
                self.assertIsNone(build.run(self.db, run["id"])["outcome"], "the run stays open")
        self.assertNotIn("went quiet", self.count("SELECT COALESCE(detail, '') FROM runner_run WHERE id = ?",
                                                  run["id"]))

    def test_the_hub_strands_a_fast_satellites_run_once_its_beat_stops(self):
        """Pack sd:8001: the skew exemption holds only while the fast beat moves. The hub reads its age by its
        own record: a satellite beat with no newer row than the hub's own beat `STALE_SECONDS` old has stopped,
        whatever stamp it carries, so its dead dispatcher's runs strand without waiting for the clock."""
        run = self.satellite_run()
        ahead = later(build.clock(), 3600)
        self.db.execute("UPDATE runner_run SET heartbeat_at = ? WHERE id = ?", (ahead, run["id"]))
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={}, timestamp=ahead)
        record_state(self.db, "heartbeat", key="build-dispatch", body={}, timestamp=run["created_at"])
        self.assertEqual(self.strand(run, after=build.STALE_SECONDS - 1), [], "the hub beat is not stale yet")
        record_state(self.db, "heartbeat", key="build-dispatch@work", body={}, timestamp=ahead)
        self.assertEqual(self.strand(run), [], "a fast beat recorded since still moves")
        record_state(self.db, "heartbeat", key="build-dispatch", body={}, timestamp=later(run["created_at"], 1))
        [settled] = self.strand(run)
        self.assertEqual((settled["run"], settled["outcome"]), (run["id"], "blocked"))
        self.assertIn("recorded no beat", build.run(self.db, run["id"])["detail"])
        self.assertTrue(self.lease_held(run))

    def test_a_cancel_on_a_quiet_satellite_blocks_and_keeps_the_lease(self):
        run = self.satellite_run()
        lead = run["assignment"]
        operations.cancel_assignment(self.db, lead, expected_revision=operations.assignment_state(
            self.db, lead)["revision"], who="dashboard")
        self.quiet()
        [settled] = self.strand(run)
        self.assertEqual(settled["outcome"], "blocked")
        ended = build.run(self.db, run["id"])
        self.assertIn("cancelled by dashboard", ended["detail"])
        self.assertIsNone(ended["released_at"])
        self.assertTrue(self.lease_held(run))

    def test_the_hub_leaves_a_quiet_satellites_held_run_held(self):
        run = self.satellite_run()
        build.end(self.db, run["id"], "blocked", "the review left blockers")
        self.quiet()
        self.assertEqual(self.strand(run), [])
        self.assertIsNone(build.run(self.db, run["id"])["released_at"], "held until work reads its groups gone")
        self.assertTrue(self.lease_held(run))

    def test_a_hub_cancel_lands_on_the_satellite_reap(self):
        run = self.satellite_run()
        lead = run["assignment"]
        operations.cancel_assignment(self.db, lead, expected_revision=operations.assignment_state(
            self.db, lead)["revision"], who="dashboard")
        stale = later(run["created_at"], build.STALE_SECONDS + 1)
        self.assertEqual(build.reap(self.db, now=stale, probe=Probe(alive=False), hosts=HUB), [])
        self.assertEqual(build.run(self.db, run["id"])["cancel_requested"], "cancelled by dashboard")
        settled = build.reap(self.db, now=stale, probe=Probe(alive=False), hosts=WORK)
        self.assertEqual(settled[0]["outcome"], "cancelled")
        self.assertEqual(self.statuses([run["members"][0]["item"]]), ["ready"])


if __name__ == "__main__":
    unittest.main()
