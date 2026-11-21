"""pack sd:9570: an item that waits on others is built once they are done.

`edit_item` records `fields.blocked_by`; `build._item`, the check every
build path runs, refuses the item while one it names is not done. Nothing
runs on a done. Design: docs/work/2026-11-19-auto-unblock/design.md.
"""

import ast
import json
from pathlib import Path

from sd_db import build
from sd_db.workflow import WorkflowError, change_status, edit_item, item_state
from sd_db.writes import set_item_fields

from .test_build import IDLE, Case

NOW = "2026-11-20T20:00:00+00:00"


class DependencyCase(Case):
    def wait(self, item, *on):
        return edit_item(self.db, item, {"blocked_by": list(on)}, who="operator")

    def fields(self, item):
        return json.loads(item_state(self.db, item)["item"]["fields"] or "null")

    def serial(self, count, **options):
        from sd_db import runner
        items = [self.item() for _ in range(count)]
        for index, item in enumerate(items):
            self.db.execute("UPDATE item SET branch = ? WHERE id = ?", (f"work/{index}", item))
        return items, [row["id"] for row in runner.enqueue(self.db, items, who="operator", **options)]

    def lone(self, branch, **options):
        from sd_db import runner
        item = self.item()
        self.db.execute("UPDATE item SET branch = ? WHERE id = ?", (branch, item))
        return item, runner.enqueue(self.db, [item], who="operator", **options)[0]["id"]


class TheRecord(DependencyCase):
    def test_a_dependency_is_recorded_sorted_with_a_note_and_kept_beside_other_fields(self):
        first, second = self.item(status="planning"), self.item(status="planning")
        item = self.item(fields={"area": "home"})
        state = self.wait(item, second, first, second)
        self.assertEqual(self.fields(item), {"area": "home", "blocked_by": [first, second]})
        self.assertEqual(state["notes"][-1]["body"], f"Waits on sd:{first}, sd:{second}; set by operator")
        self.assertEqual(self.wait(item, first, second)["revision"], state["revision"], "the same set writes nothing")
        cleared = edit_item(self.db, item, {"blocked_by": None}, who="operator")
        self.assertEqual(self.fields(item), {"area": "home"})
        self.assertEqual(cleared["notes"][-1]["body"], "Waits on nothing; cleared by operator")

    def test_a_refused_dependency_writes_nothing(self):
        other, done, item = self.item(status="planning"), self.item(status="done"), self.item(status="planning")
        self.wait(other, item)
        before = item_state(self.db, item)["revision"]
        cases = {
            "itself": ([item], f"item {item} cannot wait on itself"),
            "missing": ([999999], "no item 999999"),
            "cycle": ([other], f"sd:{other} already waits on sd:{item}"),
            "not an id": (["sd:5"], "blocked_by must be a list of item ids"),
            "bool": ([True], "blocked_by must be a list of item ids"),
            "too large": ([2**63], "blocked_by must be a list of item ids"),
        }
        for name, (on, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(WorkflowError, message):
                edit_item(self.db, item, {"blocked_by": on}, who="operator")
        self.assertEqual(item_state(self.db, item)["revision"], before)
        with self.assertRaisesRegex(WorkflowError, f"item {done} is done; a done item waits on nothing"):
            self.wait(done, other)

    def test_a_done_items_stale_set_makes_no_cycle(self):
        """pack sd:9591: a done item that still names X holds nothing, so X may wait on it."""
        later, item = self.item(status="planning"), self.item(status="planning")
        self.wait(later, item)
        change_status(self.db, later, "done", who="operator")
        self.wait(item, later)
        self.assertEqual(self.fields(item), {"blocked_by": [later]})


class TheReopen(DependencyCase):
    def test_a_reopen_that_would_close_a_cycle_drops_the_reopened_items_stale_entry(self):
        """Class pass: the edit refuses a cycle among open items, and a reopen reached one around it. A waits on B,
        A done, B waits on A, A reopened: A's stale entry goes, with a note, so B builds after A."""
        first, second = self.item(), self.item()
        self.wait(first, second)
        change_status(self.db, first, "done", who="operator")
        self.wait(second, first)
        change_status(self.db, first, "ready", who="operator")
        self.assertEqual(self.fields(first), None)
        self.assertIn(f"Waits on nothing: sd:{second} waits on it", item_state(self.db, first)["notes"][-1]["body"])
        self.assertEqual(build.waiting_on(self.db, item_state(self.db, first)["item"]), "")
        self.assertEqual(build.waiting_on(self.db, item_state(self.db, second)["item"]), f"waits on sd:{first}")

    def test_a_reopen_keeps_an_entry_that_closes_no_cycle(self):
        first, second, third = self.item(), self.item(), self.item(status="planning")
        self.wait(first, second, third)
        change_status(self.db, first, "done", who="operator")
        self.wait(second, first)
        change_status(self.db, first, "ready", who="operator")
        self.assertEqual(self.fields(first), {"blocked_by": [third]})


class TheOtherStarts(DependencyCase):
    """Class pass: every path that starts work on an item reads what it waits on, as the Build press does."""

    def test_a_teammate_start_refuses_an_item_that_waits(self):
        from sd_db import teammate
        first, item = self.item(status="planning"), self.item(status="in_progress")
        self.wait(item, first)
        with self.assertRaisesRegex(teammate.TeammateRefused, f"item {item} waits on sd:{first}"):
            teammate.start(self.db, [item], worktree="/w/x", branch="feat/x", budget_minutes=30, owner="lead")
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment"), 0)

    def test_a_runner_enqueue_refuses_an_author_run_of_an_item_that_waits(self):
        from sd_db import runner
        first, item = self.item(status="planning"), self.item()
        self.db.execute("UPDATE item SET branch = 'work/x' WHERE id = ?", (item,))
        self.wait(item, first)
        with self.assertRaisesRegex(runner.RunnerRefused, f"item {item} waits on sd:{first}"):
            runner.enqueue(self.db, [item], who="operator")
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment"), 0)

    def test_a_runner_claim_holds_an_author_run_whose_dependency_reopened(self):
        """Round 5: the enqueue read the set, and a reopen before the claim started the run anyway."""
        from sd_db import runner
        first, item = self.item(status="done"), self.item()
        self.db.execute("UPDATE item SET branch = 'work/x' WHERE id = ?", (item,))
        self.wait(item, first)
        queued = runner.enqueue(self.db, [item], who="operator")[0]["id"]
        change_status(self.db, first, "ready", who="operator")
        roots = {"work_root": Path("/w/work"), "retention_root": Path("/w/retained")}
        self.assertIsNone(runner.claim(self.db, queued, owner="fixture", **roots), "a reopen holds the run")
        self.assertEqual(runner.queue_state(self.db, queued)["status"], "queued")
        change_status(self.db, first, "done", who="operator")
        started = runner.claim(self.db, queued, owner="fixture", **roots)
        self.assertEqual(started and started["status"], "running", "a done dependency lets it start")

    def test_a_serial_run_that_waits_on_one_behind_it_yields_its_place(self):
        """Lane review: A queued before B, then A set to wait on B; A held for B, B held behind A."""
        from sd_db import runner
        (first, second), (ahead, behind) = self.serial(2)
        self.wait(first, second)
        roots = {"work_root": Path("/w/work"), "retention_root": Path("/w/retained")}
        self.assertIsNone(runner.claim(self.db, ahead, owner="fixture", **roots), "A waits on B")
        started = runner.claim(self.db, behind, owner="fixture", **roots)
        self.assertEqual(started and started["status"], "running", "A yields its place to B")

    def test_a_run_yields_to_one_two_places_behind_it(self):
        """Class pass: A waits on C with B between them. C, the one A needs, passes A and B; B plays no
        part in the wait, so it keeps its place behind A (the queue graph, third lane blocker)."""
        from sd_db import runner
        (first, _, third), (ahead, middle, last) = self.serial(3)
        self.wait(first, third)
        roots = {"work_root": Path("/w/work"), "retention_root": Path("/w/retained")}
        self.assertIsNone(runner.claim(self.db, middle, owner="fixture", **roots), "B waits for A")
        self.assertIsNone(runner.claim(self.db, ahead, owner="fixture", **roots), "A waits on C")
        started = runner.claim(self.db, last, owner="fixture", **roots)
        self.assertEqual(started and started["status"], "running", "C passes A and B")

    def test_a_run_yields_to_one_its_dependency_waits_on(self):
        """Review: A, B, C queued; A waits on C, B waits on A. B waits on C through A, so B yields too."""
        from sd_db import runner
        (first, second, third), (ahead, middle, last) = self.serial(3)
        self.wait(first, third)
        self.wait(second, first)
        roots = {"work_root": Path("/w/work"), "retention_root": Path("/w/retained")}
        self.assertIsNone(runner.claim(self.db, ahead, owner="fixture", **roots))
        self.assertIsNone(runner.claim(self.db, middle, owner="fixture", **roots))
        started = runner.claim(self.db, last, owner="fixture", **roots)
        self.assertEqual(started and started["status"], "running", "C passes A and B")

    def test_a_run_that_plays_no_part_in_the_wait_stays_behind(self):
        """Review: B after A, A waits on a later C queued apart; B does not unblock A, so B waits for A."""
        from sd_db import runner
        (first, _), (_, behind) = self.serial(2)
        third, later = self.lone("work/c")
        self.wait(first, third)
        roots = {"work_root": Path("/w/work"), "retention_root": Path("/w/retained")}
        self.assertIsNone(runner.claim(self.db, behind, owner="fixture", **roots), "B waits for A's merge")
        started = runner.claim(self.db, later, owner="fixture", **roots)
        self.assertEqual(started and started["status"], "running", "C, the one A waits on, runs")

    def test_an_unrelated_parallel_run_stays_behind_a_waiting_serial_run(self):
        """Review: the broad check let any parallel run pass A; only the one A waits on may."""
        from sd_db import runner
        (first,), _ = self.serial(1)
        _, parallel = self.lone("work/p", parallel=True)
        third, _ = self.lone("work/c")
        self.wait(first, third)
        roots = {"work_root": Path("/w/work"), "retention_root": Path("/w/retained")}
        self.assertIsNone(runner.claim(self.db, parallel, owner="fixture", **roots))

    def test_a_parallel_prerequisite_passes_the_serial_batch_behind_its_dependent(self):
        """Review: A, B one serial batch, C parallel apart, A waits on C. B waits for A's merge, and the
        repository rule held C behind B; C, the one A needs, runs first."""
        from sd_db import runner
        (first, _), (ahead, behind) = self.serial(2)
        third, parallel = self.lone("work/c", parallel=True)
        self.wait(first, third)
        roots = {"work_root": Path("/w/work"), "retention_root": Path("/w/retained")}
        self.assertIsNone(runner.claim(self.db, ahead, owner="fixture", **roots))
        self.assertIsNone(runner.claim(self.db, behind, owner="fixture", **roots))
        started = runner.claim(self.db, parallel, owner="fixture", **roots)
        self.assertEqual(started and started["status"], "running", "C runs first")

    def test_a_serial_run_that_waits_outside_the_queue_holds_no_parallel_run(self):
        """Review of build/sd-1306: a serial run waits on an open item with no queued run. The graph saw no
        wait, so the run stood as a barrier: it could not start, and every parallel run in its repository
        waited behind it."""
        from sd_db import runner
        (first,), _ = self.serial(1)
        _, parallel = self.lone("work/p", parallel=True)
        self.wait(first, self.item(status="planning"))
        roots = {"work_root": Path("/w/work"), "retention_root": Path("/w/retained")}
        started = runner.claim(self.db, parallel, owner="fixture", **roots)
        self.assertEqual(started and started["status"], "running", "the parallel run starts")

    def test_a_run_that_waits_outside_the_queue_keeps_its_place(self):
        """A waits on an item with no queued run: nothing behind it can unblock it, so the order holds."""
        from sd_db import runner
        (first, _), (_, behind) = self.serial(2)
        self.wait(first, self.item(status="planning"))
        roots = {"work_root": Path("/w/work"), "retention_root": Path("/w/retained")}
        self.assertIsNone(runner.claim(self.db, behind, owner="fixture", **roots))

    def test_a_parallel_run_passes_an_earlier_serial_run_that_waits_on_it(self):
        """Class pass: a parallel author run waits for every eligible earlier serial run in its repository."""
        from sd_db import runner
        (first,), _ = self.serial(1)
        second = self.item()
        self.db.execute("UPDATE item SET branch = 'work/p' WHERE id = ?", (second,))
        parallel = runner.enqueue(self.db, [second], who="operator", parallel=True)[0]["id"]
        self.wait(first, second)
        roots = {"work_root": Path("/w/work"), "retention_root": Path("/w/retained")}
        started = runner.claim(self.db, parallel, owner="fixture", **roots)
        self.assertEqual(started and started["status"], "running", "the held serial run does not hold it")



class TheQueueGraph(DependencyCase):
    """A property test over random batches and waits: every queue drains (third lane blocker)."""

    def drain(self):
        """Claim every queued run that may start and finish it, merged, until none is left or none starts;
        return the runs still queued."""
        from sd_db import runner

        roots = {"work_root": Path("/w/work"), "retention_root": Path("/w/retained")}
        while queued := [row["id"] for row in runner.queued(self.db)]:
            started = [ident for ident in queued if runner.claim(self.db, ident, owner="fixture", **roots)]
            if not started:
                return queued
            for ident in started:
                self.finish(ident)
        return []

    def finish(self, ident):
        """End the claimed run `ident` done, its item merged and done."""
        item = self.db.execute("SELECT item FROM assignment WHERE id = ?", (ident,)).fetchone()[0]
        self.db.execute("UPDATE assignment SET status = 'done' WHERE id = ?", (ident,))
        self.db.execute("UPDATE runner_lease SET released_at = ? WHERE run IN "
                        "(SELECT id FROM runner_run WHERE assignment = ?)", (NOW, ident))
        self.db.execute("INSERT INTO assignment (item, role, status, phase, lane) "
                        "VALUES (?, 'merge', 'done', 'merged', 'serial')", (item,))
        self.db.execute("UPDATE item SET status = 'done' WHERE id = ?", (item,))

    def test_a_prerequisite_merged_out_of_order_releases_nothing_behind_its_dependent(self):
        """Review of build/sd-1306: A, B, C serial; A waits on B and an open item. B passed A and merged;
        the walk stopped at B, so C started before A."""
        from sd_db import runner

        (first, second, _), (_, middle, last) = self.serial(3)
        self.wait(first, second, self.item(status="planning"))
        roots = {"work_root": Path("/w/work"), "retention_root": Path("/w/retained")}
        self.assertTrue(runner.claim(self.db, middle, owner="fixture", **roots), "B passes A")
        self.finish(middle)
        self.assertIsNone(runner.claim(self.db, last, owner="fixture", **roots), "C stays behind A")

    def test_a_wait_cycle_across_a_serial_batch_and_a_parallel_run_drains(self):
        """Third lane blocker: A, B, C serial, D parallel; A waits on C, B on D. Each pairwise yield held."""
        (first, second, third), _ = self.serial(3)
        fourth, _ = self.lone("work/d", parallel=True)
        self.wait(first, third)
        self.wait(second, fourth)
        self.assertEqual(self.drain(), [], "every queued run ran")


    def test_every_queue_drains(self):
        import random

        from sd_db import runner

        for seed in range(400):
            with self.subTest(seed=seed):
                if seed:
                    self.setUp()  # a fresh store per seed; each one's cleanups run at the end
                rng = random.Random(seed)
                items, branch = [], 0
                for _ in range(rng.randint(1, 4)):
                    batch = [self.item() for _ in range(rng.randint(1, 3))]
                    for item in batch:
                        branch += 1
                        self.db.execute("UPDATE item SET branch = ? WHERE id = ?", (f"work/{branch}", item))
                    runner.enqueue(self.db, batch, who="operator", parallel=rng.random() < 0.4)
                    items += batch
                # Waits follow a random order of the items, so none closes an item cycle.
                order = rng.sample(items, len(items))
                for index, item in enumerate(order):
                    later = order[index + 1:]
                    named = [other for other in later if rng.random() < 0.3]
                    if named:
                        self.wait(item, *named)
                self.assertEqual(self.drain(), [], f"seed {seed}: the queue stalled")


class TheBuild(DependencyCase):
    def test_an_item_without_the_key_builds_as_before(self):
        item = self.item()
        self.assertEqual(build.waiting_on(self.db, item_state(self.db, item)["item"]), "")
        self.assertEqual([row["item"] for row in build.queue(self.db, [item], who="operator")], [item])

    def test_a_press_refuses_while_a_dependency_is_open(self):
        first, second, item = self.item(status="planning"), self.item(status="done"), self.item()
        self.wait(item, first, second)
        with self.assertRaisesRegex(build.BuildRefused, f"item {item} waits on sd:{first}$"):
            build.queue(self.db, [item], who="operator")
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment"), 0)

    def test_unreadable_fields_refuse_the_build(self):
        item = self.item()
        for fields, why in (("{not json", "its fields cannot be read"),
                            (json.dumps({"blocked_by": "sd:5"}), "blocked_by is not a list of item ids")):
            self.db.execute("UPDATE item SET fields = ? WHERE id = ?", (fields, item))
            with self.subTest(fields), self.assertRaisesRegex(build.BuildRefused, why):
                build.queue(self.db, [item], who="operator")

    def test_a_missing_dependency_refuses_and_names_it(self):
        item = self.item()
        set_item_fields(self.db, item, fields=json.dumps({"blocked_by": [424242]}))
        with self.assertRaisesRegex(build.BuildRefused, "waits on sd:424242, which no longer exists"):
            build.queue(self.db, [item], who="operator")

    def test_a_done_dependency_lets_the_next_tick_pick_the_item(self):
        first, item = self.item(status="planning"), self.item()
        self.wait(item, first)
        build.set_pickup(self.db, True, now=NOW)
        available = build.room(self.db, **IDLE)
        self.assertEqual(build.pickup(self.db, available, now=NOW), [], "a waiting item is not picked")
        change_status(self.db, first, "done", who="operator")
        self.assertEqual(item_state(self.db, item)["item"]["status"], "ready", "the done wrote nothing on it")
        self.assertEqual(build.pickup(self.db, available, now=NOW), [item])
        self.assertEqual(build.pickup(self.db, available, now=NOW), [], "picked once")

    def test_a_reopened_dependency_holds_a_queued_build(self):
        first, item = self.item(status="done"), self.item()
        self.wait(item, first)
        queued = build.queue(self.db, [item], who="operator")[0]["id"]
        change_status(self.db, first, "ready", who="operator")
        self.assertIsNone(self.claim(), "the start reads the set again")
        row = self.db.execute("SELECT status, result FROM assignment WHERE id = ?", (queued,)).fetchone()
        self.assertEqual((row["status"], row["result"]), ("blocked", f"item {item} waits on sd:{first}; not built"))
        self.assertEqual(item_state(self.db, item)["item"]["status"], "ready")

    def test_dependency_waits_at_the_claim_leave_auto_pickup_on(self):
        """Review: a shared prerequisite reopened after pickup queued two dependents; the claim refused
        both before any builder started, and the next tick read two blocked builds and turned pickup off."""
        shared, one, two = self.item(status="done"), self.item(), self.item()
        self.wait(one, shared)
        self.wait(two, shared)
        build.set_pickup(self.db, True, now="2026-01-01T00:00:00+00:00")
        build.queue(self.db, [one], who=build.PICKUP)
        build.queue(self.db, [two], who=build.PICKUP)
        change_status(self.db, shared, "ready", who="operator")
        self.assertIsNone(self.claim())
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment WHERE status = 'blocked'"), 2)
        self.assertIsNone(build.stop_on_blocks(self.db), "a refusal at the claim is no build failure")
        self.assertTrue(build.pickup_state(self.db)["on"])

    def blocked_bundle(self, *items):
        """A bundle of `items` claimed, ended blocked and released: one Retry takes back to its branch."""
        build.queue(self.db, list(items), who="operator")
        run = self.claim()
        build.write(self.db, run["id"], step="building")
        build.end(self.db, run["id"], "blocked", "the builder stopped")
        self.db.execute("UPDATE runner_run SET released_at = ? WHERE id = ?", (NOW, run["id"]))
        self.db.execute("UPDATE runner_lease SET released_at = ? WHERE run = ?", (NOW, run["id"]))
        return run

    def test_a_reopened_dependency_of_a_retried_lead_holds_the_whole_bundle(self):
        """Review of build/sd-1306: blocking only the lead let the next claim promote a member to a new branch and
        worktree. The whole retried bundle ends blocked, so the next Retry resumes the original branch."""
        first, lead, member = self.item(status="done"), self.item(), self.item()
        run = self.blocked_bundle(lead, member)
        self.wait(lead, first)
        build.retry(self.db, run["assignment"], who="operator")
        change_status(self.db, first, "ready", who="operator")
        self.assertIsNone(self.claim())
        self.assertIsNone(self.claim(), "no member leads a new branch")
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment WHERE status = 'blocked'"), 2)
        change_status(self.db, first, "done", who="operator")
        build.retry(self.db, run["assignment"], who="operator")
        self.assertEqual(self.claim()["branch"], run["branch"])

    def test_a_member_its_held_lead_waits_on_builds_on_its_own(self):
        """Review of build/sd-1306: a retried bundle whose lead came to wait on its own member parked both, a
        Build of the member retried the same bundle, and a subset Retry needs the lead. The member the lead
        waits on leaves the bundle at the hold, so a Build takes it fresh on its own branch."""
        lead, member = self.item(), self.item()
        run = self.blocked_bundle(lead, member)
        build.retry(self.db, run["assignment"], who="operator")
        self.wait(lead, member)
        self.assertIsNone(self.claim())
        self.assertIsNone(item_state(self.db, member)["item"]["parked_at"], "the prerequisite stays free")
        self.assertIsNotNone(item_state(self.db, lead)["item"]["parked_at"], "the lead stays held")
        build.queue_again(self.db, [member], who="operator")
        started = self.claim()
        self.assertEqual([m["item"] for m in started["members"]], [member])
        self.assertNotEqual(started["branch"], run["branch"])

    def test_a_prerequisite_with_its_own_prerequisite_leaves_the_held_bundle(self):
        """Review of build/sd-1306: A waits on B, B on C. The claim blocked B as a waiting row before it
        took B out, and the removal touched only queued rows, so B stayed in the bundle and its Build
        retried the whole bundle."""
        lead, member, third = self.item(), self.item(), self.item(status="planning")
        run = self.blocked_bundle(lead, member)
        build.retry(self.db, run["assignment"], who="operator")
        self.wait(member, third)
        self.wait(lead, member)
        self.assertIsNone(self.claim())
        change_status(self.db, third, "done", who="operator")
        build.queue_again(self.db, [member], who="operator")
        started = self.claim()
        self.assertEqual(started and [m["item"] for m in started["members"]], [member])
        self.assertNotEqual(started["branch"], run["branch"])

    def lead_waits_on_its_member(self, pressed):
        """Review of build/sd-1306: the lead of an ended bundle waits on its member. A Build of either item
        retried the whole bundle and refused on the lead. The member leaves and builds on its own branch."""
        lead, member = self.item(), self.item()
        run = self.blocked_bundle(lead, member)
        self.wait(lead, member)
        build.queue_again(self.db, [member if pressed == "member" else lead], who="operator")
        started = self.claim()
        self.assertEqual(started and [m["item"] for m in started["members"]], [member])
        self.assertNotEqual(started["branch"], run["branch"])
        self.assertEqual(item_state(self.db, lead)["item"]["status"], "blocked", "the lead stays")

    def test_a_failed_bundle_whose_lead_waits_on_its_member_builds_the_member_pressed(self):
        self.lead_waits_on_its_member("member")

    def test_a_failed_bundle_whose_lead_waits_on_its_member_builds_it_on_the_leads_press(self):
        self.lead_waits_on_its_member("lead")

    def test_a_failed_bundle_whose_lead_waits_through_a_chain_builds_the_runnable_member(self):
        """Review of build/sd-1306: lead waits on B, B on C, all one ended bundle. Retry freed B and C and
        queued both; the queue refused B, which waits, and rolled back both. C builds; B stays free."""
        lead, second, third = self.item(), self.item(), self.item()
        run = self.blocked_bundle(lead, second, third)
        self.wait(second, third)
        self.wait(lead, second)
        build.queue_again(self.db, [third], who="operator")
        started = self.claim()
        self.assertEqual(started and [m["item"] for m in started["members"]], [third])
        self.assertNotEqual(started["branch"], run["branch"])
        self.assertIsNone(item_state(self.db, second)["item"]["parked_at"], "B builds once C is done")

    def test_a_failed_bundle_whose_member_waits_on_its_lead_retries_the_lead(self):
        """Review of build/sd-1306: the member waits on the lead. A Build of the lead retried both and
        refused on the member. The member leaves the Retry, and the lead resumes its branch."""
        lead, member = self.item(), self.item()
        run = self.blocked_bundle(lead, member)
        self.wait(member, lead)
        build.queue_again(self.db, [lead], who="operator")
        started = self.claim()
        self.assertEqual(started and [m["item"] for m in started["members"]], [lead])
        self.assertEqual(started["branch"], run["branch"])

    def test_auto_pickup_does_not_split_a_held_retried_bundle(self):
        """Review of build/sd-1306: the held bundle's items stayed ready and unparked, so the next pickup built the
        free member on a new branch. The hold parks them; Retry unparks them onto the original branch."""
        first, lead, member = self.item(status="done"), self.item(), self.item()
        run = self.blocked_bundle(lead, member)
        self.wait(lead, first)
        build.retry(self.db, run["assignment"], who="operator")
        change_status(self.db, first, "planning", who="operator")
        self.assertIsNone(self.claim())
        build.set_pickup(self.db, True, now=NOW)
        self.assertEqual(build.pickup(self.db, build.room(self.db, **IDLE), now=NOW), [], "no member built alone")
        self.assertEqual(self.count("SELECT COUNT(*) FROM assignment"), 2)
        change_status(self.db, first, "done", who="operator")
        build.retry(self.db, run["assignment"], who="operator")
        self.assertEqual([m["item"] for m in self.claim()["members"]], [lead, member])

    def test_every_queue_path_runs_item(self):
        """Failure table row 5: each function in `build` that writes a build assignment or starts one reads the set,
        through `_item` or `waiting_on`. A new path that does neither fails here."""
        source = Path(build.__file__).read_text()
        writers = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef):
                text = ast.get_source_segment(source, node) or ""
                if "create_assignment(" in text or "SET status = 'running'" in text:
                    writers.append(node.name)
                    with self.subTest(node.name):
                        self.assertTrue("_item(connection" in text or "waiting_on(connection" in text)
        self.assertEqual(sorted(writers), ["claim", "queue"])
