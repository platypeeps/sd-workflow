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
