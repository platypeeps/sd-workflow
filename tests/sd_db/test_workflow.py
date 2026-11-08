"""The user-facing task workflow is local, atomic and revision checked."""

import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, contributions, create_assignment, create_item, upsert_repo
from sd_db.database import transaction
from sd_db.migrate import initialise
from sd_db.reads import item_by_id
from sd_db.workflow import (
    HAND_KINDS,
    REPO_LESS_KINDS,
    TASK_STATUSES,
    MissingItem,
    MissingNote,
    StaleItem,
    WorkflowError,
    add_item_note,
    allowed_statuses,
    areas,
    capture_task,
    change_status,
    edit_item,
    is_audit_note,
    item_area,
    item_state,
    resolve_item_note,
    schema_kinds,
)
from sd_db.writes import (
    TransitionRefused,
    add_note,
    set_item_fields,
    set_setting,
    transition,
)


class WorkflowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)

    def capture(self, **values):
        return capture_task(self.db, title="A small task", who="operator", **values)

    def counts(self):
        return tuple(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("repo", "item", "note", "shadow"))


class OrdinaryTasks(WorkflowCase):
    def test_capture_edit_complete_needs_no_repository_or_external_issue(self):
        state = self.capture(body="Do this", priority=2, due="2026-09-10")
        item = state["item"]
        self.assertEqual((item["kind"], item["status"], item["repo"], item["external_id"]),
                         ("task", "planning", None, None))
        self.assertEqual(json.loads(item["body"]), {"text": "Do this"})
        edited = edit_item(self.db, item["id"], {"title": "Done today", "priority": 1},
                           expected_revision=state["revision"], who="operator")
        finished = change_status(self.db, item["id"], "done", who="user",
                                 expected_revision=edited["revision"])
        self.assertEqual(finished["item"]["status"], "done")
        self.assertEqual(finished["item"]["title"], "Done today")
        self.assertEqual(self.counts()[0::3], (0, 0))
        history = [n for n in finished["notes"] if n["kind"] == "status_change"]
        self.assertEqual(len(history), 2)
        self.assertIn("planning -> done by user", history[-1]["body"])
        self.assertTrue(history[-1]["timestamp"])

    def test_an_unknown_status_points_to_the_cancel_action(self):
        # sd:2741: 'dropped' listed the statuses and never named what closes; sd:3503 names the action.
        item = self.capture()["item"]["id"]
        with self.assertRaises(TransitionRefused) as refused:
            change_status(self.db, item, "dropped", who="operator")
        self.assertIn("no status 'dropped'", str(refused.exception))
        self.assertIn(f"to close item {item} that nobody will do, cancel it with a reason",
                      str(refused.exception))

    def test_optional_repository_must_already_be_registered(self):
        with self.assertRaisesRegex(WorkflowError, "registered"):
            self.capture(repo="/repos/unknown")
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        upsert_repo(self.db, "/repos/known")
        state = self.capture(repo="/repos/known")
        cleared = edit_item(self.db, state["item"]["id"], {"repo": None}, who="operator")
        self.assertIsNone(cleared["item"]["repo"])

    def test_invalid_user_fields_and_internal_metadata_cannot_be_written(self):
        state = self.capture()
        for changes in ({"status": "done"}, {"kind": "work"}, {"fields": {}},
                        {"external_id": "https://example.test/issue/1"},
                        {"title": "  "}, {"priority": True}, {"priority": 5},
                        {"due": "2026-02-30"}, {"due": "20260901"},
                        {"body": {"status_change": "done"}}):
            with self.subTest(changes=changes), self.assertRaises(WorkflowError):
                edit_item(self.db, state["item"]["id"], changes, who="operator")
            self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_missing_item_errors_are_exact_and_never_silent_successes(self):
        for call in (lambda: item_state(self.db, 999),
                     lambda: edit_item(self.db, 999, {"title": "x"}, who="operator"),
                     lambda: change_status(self.db, 999, "done", who="operator"),
                     lambda: add_item_note(self.db, 999, body="x", who="operator")):
            with self.assertRaisesRegex(MissingItem, "^no item 999$"):
                call()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_stale_edit_is_rejected_even_when_both_changes_share_a_second(self):
        with patch("sd_db.writes.now", return_value="2026-09-08T12:00:00+00:00"):
            state = self.capture()
            first = edit_item(self.db, state["item"]["id"], {"title": "First"},
                              expected_revision=state["revision"], who="operator")
            self.assertEqual(state["item"]["updated_at"], first["item"]["updated_at"])
            with self.assertRaises(StaleItem):
                edit_item(self.db, state["item"]["id"], {"title": "Lost update"},
                          expected_revision=state["revision"], who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), first)

    def test_failed_history_write_rolls_back_the_edited_row(self):
        state = self.capture()
        with patch("sd_db.workflow.add_note", side_effect=RuntimeError("disk error")):
            with self.assertRaisesRegex(RuntimeError, "disk error"):
                edit_item(self.db, state["item"]["id"], {"title": "Must roll back"}, who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_noop_status_does_not_add_history_or_change_revision(self):
        state = self.capture()
        self.assertEqual(change_status(self.db, state["item"]["id"], "planning", who="operator"), state)

    def test_readback_is_one_snapshot_while_another_connection_writes(self):
        state = self.capture()
        other = connect(self.path)
        self.addCleanup(other.close)

        def concurrent_edit(connection, item):
            row = item_by_id(connection, item)
            set_item_fields(other, item, title="Concurrent edit")
            add_note(other, item, "comment", "Concurrent note")
            return row

        with patch("sd_db.workflow.item_by_id", side_effect=concurrent_edit):
            observed = item_state(self.db, state["item"]["id"])
        self.assertEqual(observed, state)
        self.assertEqual(item_state(self.db, state["item"]["id"])["item"]["title"],
                         "Concurrent edit")


class ItemArea(WorkflowCase):
    """pack sd:8010: an item's own area, `fields.area`, set and cleared through `edit_item`."""

    def fields(self, item):
        return json.loads(item_state(self.db, item)["item"]["fields"] or "{}")

    def test_area_sets_home_or_hoa_keeps_other_fields_and_clears(self):
        state = self.capture()
        item = state["item"]["id"]
        set_item_fields(self.db, item, fields={"score": 3})
        state = item_state(self.db, item)
        tagged = edit_item(self.db, item, {"area": "home"}, who="operator", expected_revision=state["revision"])
        self.assertEqual(self.fields(item), {"score": 3, "area": "home"})
        self.assertEqual(item_area(tagged["item"]), "home")
        self.assertEqual(tagged["notes"][-1]["body"], "Updated area by operator")
        self.assertTrue(is_audit_note(tagged["notes"][-1]))
        self.assertNotEqual(tagged["revision"], state["revision"])
        moved = edit_item(self.db, item, {"area": "hoa"}, who="operator")
        self.assertEqual(item_area(moved["item"]), "hoa")
        cleared = edit_item(self.db, item, {"area": None}, who="operator")
        self.assertEqual(self.fields(item), {"score": 3})
        self.assertIsNone(item_area(cleared["item"]))
        # A clear of no area writes nothing: no note, the same revision.
        again = edit_item(self.db, item, {"area": None}, who="operator")
        self.assertEqual(again, cleared)

    def test_an_area_other_than_home_hoa_personal_or_a_realm_is_refused_and_writes_nothing(self):
        state = self.capture()
        for area in ("work", "acme", "all", "Home", "", 1, True):
            with self.subTest(area=area), self.assertRaisesRegex(
                    WorkflowError, "area must be one of home, hoa, personal, or null"):
                edit_item(self.db, state["item"]["id"], {"area": area}, who="operator")
            self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_a_realm_id_of_the_dashboard_realms_setting_is_an_area(self):
        """pack sd:8062: the areas are home, hoa, personal and the realm ids the
        dashboard.realms row names, read when the edit runs, never a fixed list."""
        self.assertEqual(areas(self.db), ("home", "hoa", "personal"))
        set_setting(self.db, "dashboard.realms", "realm|work|Work|/repos/*\nrealm|work|Work|/w/*\n"
                    "realm|lab|Lab|/lab\nrealm|hoa|Clash|/x\nnot a line\n")
        self.assertEqual(areas(self.db), ("home", "hoa", "personal", "work", "lab"))
        item = self.capture()["item"]["id"]
        for area in ("work", "lab", "personal"):
            self.assertEqual(item_area(edit_item(self.db, item, {"area": area}, who="operator")["item"]), area)
        with self.assertRaisesRegex(WorkflowError, "area must be one of home, hoa, personal, work, lab, or null"):
            edit_item(self.db, item, {"area": "acme"}, who="operator")

    def test_a_stale_revision_or_unreadable_fields_refuse_the_area(self):
        state = self.capture()
        item = state["item"]["id"]
        edit_item(self.db, item, {"title": "Moved on"}, who="operator")
        with self.assertRaises(StaleItem):
            edit_item(self.db, item, {"area": "home"}, who="operator", expected_revision=state["revision"])
        set_item_fields(self.db, item, fields="[1, 2]")
        held = item_state(self.db, item)
        with self.assertRaisesRegex(WorkflowError, "unreadable fields"):
            edit_item(self.db, item, {"area": "home"}, who="operator")
        self.assertEqual(item_state(self.db, item), held)

    def test_item_area_reads_only_a_known_area(self):
        for fields, area in ((None, None), ("", None), ("{}", None), ('{"area": "home"}', "home"),
                             ('{"area": "hoa"}', "hoa"), ('{"area": "work"}', "work"),
                             ('{"area": "Work"}', None), ('{"area": ""}', None), ("[1]", None),
                             ("not json", None), ('{"area": ["home"]}', None)):
            with self.subTest(fields=fields):
                self.assertEqual(item_area({"fields": fields}), area)


class StatusGuards(WorkflowCase):
    def test_unknown_status_is_refused_without_history(self):
        state = self.capture()
        with self.assertRaises(TransitionRefused):
            change_status(self.db, state["item"]["id"], "shipped", who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_work_done_cannot_bypass_delivery_checks(self):
        upsert_repo(self.db, "/repos/work")
        item = create_item(self.db, kind="work", title="Code delivery", repo="/repos/work")
        with self.assertRaisesRegex(TransitionRefused, "delivery"):
            change_status(self.db, item, "done", who="operator")
        self.assertNotIn("done", allowed_statuses(self.db, item))

    def test_the_row_owns_every_work_items_status(self):
        """sd:3231: no repository's files own status any more, so every
        registered repository's work moves through the row."""
        upsert_repo(self.db, "/repos/work")
        item = create_item(self.db, kind="work", title="Work", repo="/repos/work")
        self.assertIn("ready", allowed_statuses(self.db, item))
        self.assertEqual(change_status(self.db, item, "ready", who="operator")["item"]["status"], "ready")

    def test_work_repository_cannot_be_changed_away_from_its_stable_source(self):
        upsert_repo(self.db, "/repos/one")
        upsert_repo(self.db, "/repos/two")
        item = create_item(self.db, kind="work", title="Work", repo="/repos/one",
                           source="docs/work", external_id="/repos/one::docs/work/x/prd.md")
        before = item_state(self.db, item)
        for repo in (None, "/repos/two"):
            with self.subTest(repo=repo), self.assertRaisesRegex(WorkflowError, "identity"):
                edit_item(self.db, item, {"repo": repo}, who="operator")
            self.assertEqual(item_state(self.db, item), before)

    def test_other_kinds_cannot_bypass_their_specialized_workflow(self):
        item = create_item(self.db, kind="idea", title="Writing")
        with self.assertRaises(TransitionRefused):
            change_status(self.db, item, "ready", who="operator")
        self.assertEqual(allowed_statuses(self.db, item), [])

    def vault_row(self):
        item = create_item(self.db, kind="idea", title="Example idea", source="vault",
                           external_id="blog-idea:System/Databases/Blog Ideas/Example idea.md",
                           stage="inbox", fields={"kind": "blog-idea"})
        self.db.commit()
        return item

    def test_a_vault_row_moves_by_stage_only(self):
        """sd:3972: its status follows its stage through the plugin's map, so no status write may move it."""
        item = self.vault_row()
        self.assertEqual(allowed_statuses(self.db, item), [])
        before, counts = item_state(self.db, item), self.counts()
        for target in ("done", "ready", "planning"):
            with self.subTest(target=target), \
                    self.assertRaisesRegex(TransitionRefused, "blog-idea row from the vault.*by stage only"):
                change_status(self.db, item, target, who="operator")
        self.assertEqual(item_state(self.db, item), before)
        self.assertEqual(self.counts(), counts)

    def test_the_public_status_writer_refuses_a_vault_row_too(self):
        """sd:3972: `sd_db.transition` is the library's public status write; only the import moves a base row."""
        item = self.vault_row()
        before, counts = item_state(self.db, item), self.counts()
        for target in ("done", "planning"):
            with self.subTest(target=target), \
                    self.assertRaisesRegex(TransitionRefused, "blog-idea row from the vault.*by stage only"):
                transition(self.db, item, target, who="operator")
        self.assertEqual(item_state(self.db, item), before)
        self.assertEqual(self.counts(), counts)

    def test_allowed_statuses_takes_the_state_the_caller_read(self):
        # sd:2380: a caller that read the item's state passes it, and the history is not read again.
        item = create_item(self.db, kind="task", title="Listed")
        other = create_item(self.db, kind="idea", title="Another")
        state, others = item_state(self.db, item), item_state(self.db, other)
        with patch("sd_db.workflow.item_state", side_effect=AssertionError("the history was read again")):
            self.assertEqual(allowed_statuses(self.db, item, state=state), list(TASK_STATUSES))
            with self.assertRaisesRegex(WorkflowError, f"item {other}'s, not item {item}'s"):
                allowed_statuses(self.db, item, state=others)

    def assert_finishes_with_history(self, kind):
        item = create_item(self.db, kind=kind, title=f"A {kind} item")
        finished = change_status(self.db, item, "done", who="operator")
        self.assertEqual(finished["item"]["status"], "done")
        history = [n for n in finished["notes"] if n["kind"] == "status_change"]
        self.assertIn("planning -> done by operator", history[-1]["body"])

    def test_followup_item_moves_from_planning_to_done_with_history(self):
        # sd:768: a followup item used to have no status control at all.
        self.assert_finishes_with_history("followup")

    def test_followup_offers_exactly_the_task_statuses(self):
        for kind in ("followup",):
            item = create_item(self.db, kind=kind, title=f"A {kind} item")
            with self.subTest(kind=kind):
                self.assertEqual(allowed_statuses(self.db, item), list(TASK_STATUSES))
                self.assertEqual(change_status(self.db, item, "blocked", who="operator")["item"]["status"],
                                 "blocked")

    def test_a_done_task_status_item_reopens_to_planning_with_history(self):
        # sd:772: done is not terminal for these kinds; only work completion is.
        for kind in ("task", "followup"):
            item = create_item(self.db, kind=kind, title=f"A {kind} item")
            change_status(self.db, item, "done", who="operator")
            with self.subTest(kind=kind):
                self.assertEqual(allowed_statuses(self.db, item), list(TASK_STATUSES))
                reopened = change_status(self.db, item, "planning", who="operator")
                self.assertEqual(reopened["item"]["status"], "planning")
                history = [n for n in reopened["notes"] if n["kind"] == "status_change"]
                self.assertIn("done -> planning by operator", history[-1]["body"])

    def test_a_stale_revision_on_a_followup_item_leaves_its_status(self):
        for kind in ("followup",):
            item = create_item(self.db, kind=kind, title=f"A {kind} item")
            seen = item_state(self.db, item)["revision"]
            finished = change_status(self.db, item, "done", who="operator", expected_revision=seen)
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(StaleItem, f"^item {item} changed"):
                    change_status(self.db, item, "planning", who="operator", expected_revision=seen)
                self.assertEqual(item_state(self.db, item), finished)
                self.assertEqual(finished["item"]["status"], "done")

    def test_kinds_with_their_own_workflow_still_refuse_status_controls(self):
        for kind in ("idea", "work-idea", "personal-idea", "report"):
            item = create_item(self.db, kind=kind, title=f"A {kind} item")
            before = item_state(self.db, item)
            with self.subTest(kind=kind), self.assertRaisesRegex(TransitionRefused, "task controls"):
                change_status(self.db, item, "done", who="operator")
            self.assertEqual(allowed_statuses(self.db, item), [])
            self.assertEqual(item_state(self.db, item), before)

    def test_a_report_refusal_names_acknowledgement_as_the_way_out(self):
        """sd:5065: the library names the action; the CLI and dashboard render it."""
        item = create_item(self.db, kind="report", title="A report item")
        with self.assertRaisesRegex(TransitionRefused, f"acknowledge report {item}"):
            change_status(self.db, item, "done", who="operator")

    def test_followup_with_active_assignment_refuses_until_it_ends(self):
        item = create_item(self.db, kind="followup", title="Come back to this")
        for status in ("queued", "running"):
            assignment = create_assignment(self.db, item=item, role="author", status=status)
            before = item_state(self.db, item)
            with self.subTest(status=status), self.assertRaisesRegex(TransitionRefused, "assignment"):
                change_status(self.db, item, "done", who="operator")
            self.assertEqual(allowed_statuses(self.db, item), [])
            self.assertEqual(item_state(self.db, item), before)
            self.db.execute("UPDATE assignment SET status = 'failed' WHERE id = ?", (assignment,))
            self.db.commit()
        self.assertEqual(change_status(self.db, item, "done", who="operator")["item"]["status"], "done")

    def test_queued_or_running_assignment_blocks_status_changes(self):
        for status in ("queued", "running"):
            state = self.capture()
            item = state["item"]["id"]
            create_assignment(self.db, item=item, role="author", status=status)
            with self.subTest(status=status), self.assertRaisesRegex(TransitionRefused, "assignment"):
                change_status(self.db, item, "done", who="operator")
            self.assertEqual(allowed_statuses(self.db, item), [])
            self.assertEqual(item_state(self.db, item), state)

    def test_work_body_cannot_be_replaced_by_task_editor(self):
        upsert_repo(self.db, "/repos/work")
        item = create_item(self.db, kind="work", title="Work", repo="/repos/work", body={"Goals": "Keep"})
        with self.assertRaisesRegex(WorkflowError, "task"):
            edit_item(self.db, item, {"body": "Replace"}, who="operator")
        self.assertEqual(json.loads(item_state(self.db, item)["item"]["body"]), {"Goals": "Keep"})


class BranchItemClose(WorkflowCase):
    """sd:2570: a row worked on its own branch needs its merge, or a reason,
    to close; the pack's `sd task status` rule (sd:1990), held by the library."""

    SHA = "a" * 40

    def on_branch(self, branch):
        item = self.capture()["item"]["id"]
        self.db.execute("UPDATE item SET branch = ? WHERE id = ?", (branch, item))
        self.db.commit()
        return item

    def test_a_reasonless_close_with_no_merge_recorded_is_refused_and_leaves_the_row(self):
        item = self.on_branch("fleet/fixture-sd1")
        before = item_state(self.db, item)
        with self.assertRaisesRegex(TransitionRefused,
                                    "worked on branch fleet/fixture-sd1, and no merge of it is recorded"):
            change_status(self.db, item, "done", who="dashboard")
        self.assertEqual(item_state(self.db, item), before)

    def test_a_reason_closes_it_and_the_transition_records_it(self):
        item = self.on_branch("fleet/fixture-sd1")
        finished = change_status(self.db, item, "done", who="dashboard", reason="superseded by another merge")
        self.assertEqual(finished["item"]["status"], "done")
        self.assertIn("superseded by another merge", finished["notes"][-1]["body"])

    def test_a_recorded_merge_closes_it_plainly(self):
        merged = self.on_branch("fleet/fixture-sd1")
        add_note(self.db, merged, "comment", f"Code delivery https://github.example.test/o/r/pull/7 at {self.SHA}")
        self.assertEqual(change_status(self.db, merged, "done", who="dashboard")["item"]["status"], "done")
        delivered = self.on_branch("fleet/fixture-sd2")
        change_status(self.db, delivered, "done", who="sd-ship", reason=f"delivered at {self.SHA} on refs/heads/main")
        change_status(self.db, delivered, "planning", who="operator")
        self.assertEqual(change_status(self.db, delivered, "done", who="dashboard")["item"]["status"], "done")

    def test_a_row_on_the_default_branch_or_none_still_closes_plainly(self):
        for branch in ("main", "origin/master", None):
            with self.subTest(branch=branch):
                item = self.on_branch(branch)
                self.assertEqual(change_status(self.db, item, "done", who="dashboard")["item"]["status"], "done")

    def test_other_moves_of_a_branch_row_are_unchanged(self):
        item = self.on_branch("fleet/fixture-sd1")
        self.assertEqual(change_status(self.db, item, "blocked", who="dashboard")["item"]["status"], "blocked")


class DoneResolvesOpenNotes(WorkflowCase):
    """sd:3505: a move to done resolves the item's open followup notes, the
    ones a brief reads, in the transaction that moves it, and the status note
    names them. An open question stays open: the close is no answer to it
    (pack sd:6329)."""

    def notes(self, item):
        rows = self.db.execute("SELECT id, kind, resolved_at FROM note WHERE item = ? AND kind != 'status_change' "
                               "ORDER BY id", (item,)).fetchall()
        return {row["id"]: (row["kind"], row["resolved_at"] is None) for row in rows}

    def test_done_resolves_open_followups_and_names_them(self):
        item = self.capture()["item"]["id"]
        followup = add_note(self.db, item, "followup", "Check the backup")
        second = add_note(self.db, item, "followup", "Check the restore")
        comment = add_note(self.db, item, "comment", "Context only")
        settled = add_note(self.db, item, "followup", "Already done")
        resolve_item_note(self.db, settled, who="operator")
        finished = change_status(self.db, item, "done", who="operator")
        self.assertEqual(self.notes(item), {followup: ("followup", False), second: ("followup", False),
                                            comment: ("comment", True), settled: ("followup", False)})
        self.assertEqual(finished["notes"][-1]["body"],
                         f"planning -> done by operator: resolved open notes {followup}, {second}")

    def test_done_leaves_an_unanswered_question_open(self):
        item = self.capture()["item"]["id"]
        followup = add_note(self.db, item, "followup", "Check the backup")
        question = add_note(self.db, item, "question", "Move all 45?\nOption: Approve all (Recommended)\nOption: Videos only")
        finished = change_status(self.db, item, "done", who="sd-ship", reason="delivered at " + "a" * 40)
        self.assertEqual(self.notes(item), {followup: ("followup", False), question: ("question", True)})
        self.assertEqual(finished["notes"][-1]["body"],
                         f"planning -> done by sd-ship: delivered at {'a' * 40}; resolved open note {followup}")

    def test_a_reason_keeps_its_place_before_the_resolved_notes(self):
        item = self.capture()["item"]["id"]
        followup = add_note(self.db, item, "followup", "Check the backup")
        finished = change_status(self.db, item, "done", who="operator", reason="shipped by hand")
        self.assertEqual(finished["notes"][-1]["body"],
                         f"planning -> done by operator: shipped by hand; resolved open note {followup}")

    def test_another_status_resolves_nothing(self):
        item = self.capture()["item"]["id"]
        followup = add_note(self.db, item, "followup", "Check the backup")
        change_status(self.db, item, "blocked", who="operator")
        self.assertEqual(self.notes(item), {followup: ("followup", True)})

    def test_a_refused_done_leaves_the_notes_open(self):
        item = self.capture()["item"]["id"]
        self.db.execute("UPDATE item SET branch = 'fleet/fixture-sd1' WHERE id = ?", (item,))
        self.db.commit()
        followup = add_note(self.db, item, "followup", "Check the backup")
        with self.assertRaises(TransitionRefused):
            change_status(self.db, item, "done", who="operator")
        self.assertEqual(self.notes(item), {followup: ("followup", True)})

    def test_a_failed_transition_rolls_the_resolution_back(self):
        item = self.capture()["item"]["id"]
        followup = add_note(self.db, item, "followup", "Check the backup")
        with patch("sd_db.workflow.transition", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                change_status(self.db, item, "done", who="operator")
        self.assertEqual(self.notes(item), {followup: ("followup", True)})
        self.assertEqual(item_state(self.db, item)["item"]["status"], "planning")


class BranchClearing(WorkflowCase):
    """sd:2818, sd:7659: `edit_item` clears a stale branch, or sets one."""

    def work_on(self, branch, status="done"):
        upsert_repo(self.db, "/repos/work")
        item = create_item(self.db, kind="work", status=status, title="Work",
                           repo="/repos/work", branch=branch)
        return item_state(self.db, item)

    def test_a_cleared_branch_is_written_noted_and_revision_checked(self):
        state = self.work_on("feat/gone")
        item = state["item"]["id"]
        with self.assertRaises(StaleItem):
            edit_item(self.db, item, {"branch": None}, who="operator", expected_revision="0" * 64)
        self.assertEqual(item_state(self.db, item), state)
        cleared = edit_item(self.db, item, {"branch": None}, who="operator",
                            expected_revision=state["revision"])
        self.assertIsNone(cleared["item"]["branch"])
        self.assertEqual(cleared["notes"][-1]["body"], "Updated branch by operator")

    def test_a_runner_owned_row_keeps_its_branch(self):
        """The runner reads the branch to claim and restore; `configure_item`'s guards apply."""
        for status in ("queued", "running", "ending"):
            with self.subTest(status=status):
                state = self.work_on("feat/held", status="planning")
                create_assignment(self.db, item=state["item"]["id"], role="author", status=status)
                state = item_state(self.db, state["item"]["id"])
                with self.assertRaisesRegex(WorkflowError, f"has {status} assignment"):
                    edit_item(self.db, state["item"]["id"], {"branch": None}, who="operator")
                self.assertEqual(item_state(self.db, state["item"]["id"]), state)
        state = self.work_on("feat/leased", status="planning")
        assignment = create_assignment(self.db, item=state["item"]["id"], role="author", status="done")
        self.db.execute(
            "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path,"
            " created_at, updated_at) VALUES ('r1', ?, 1, '/repos/work', 'feat/leased', 'o', '/w', '/r',"
            " 'now', 'now')", (assignment,))
        self.db.commit()
        state = item_state(self.db, state["item"]["id"])
        with self.assertRaisesRegex(WorkflowError, "retained runner lease"):
            edit_item(self.db, state["item"]["id"], {"branch": None}, who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_an_in_progress_row_keeps_a_branch(self):
        """sd:7659. Lint rule 2 wants a branch on an in_progress row: a clear is refused."""
        state = self.work_on("feat/live", status="in_progress")
        with self.assertRaisesRegex(WorkflowError, "in_progress item records its branch"):
            edit_item(self.db, state["item"]["id"], {"branch": None}, who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_a_branch_is_set_noted_and_replaces_the_old_one(self):
        """sd:7659. A row that lost its branch, or names a stale one, takes a new name."""
        for status, held in (("planning", None), ("in_progress", "feat/stale")):
            with self.subTest(status=status):
                state = self.work_on(held, status=status)
                set_to = edit_item(self.db, state["item"]["id"], {"branch": " feat/new "},
                                   who="operator", expected_revision=state["revision"])
                self.assertEqual(set_to["item"]["branch"], "feat/new")
                self.assertEqual(set_to["notes"][-1]["body"], "Updated branch by operator")

    def test_a_blank_or_spaced_branch_is_refused_and_leaves_the_row(self):
        state = self.work_on("feat/gone", status="planning")
        for value in ("", "  ", "feat/a b", 7):
            with self.subTest(value=value):
                with self.assertRaisesRegex(WorkflowError, "branch"):
                    edit_item(self.db, state["item"]["id"], {"branch": value}, who="operator")
                self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_a_runner_owned_row_keeps_its_branch_against_a_new_name(self):
        state = self.work_on("feat/held", status="planning")
        create_assignment(self.db, item=state["item"]["id"], role="author", status="running")
        state = item_state(self.db, state["item"]["id"])
        with self.assertRaisesRegex(WorkflowError, "has running assignment"):
            edit_item(self.db, state["item"]["id"], {"branch": "feat/new"}, who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), state)


class Unparking(WorkflowCase):
    """sd:3007: `edit_item` clears `parked_at`, which the nightly prune sets."""

    def parked(self, piece=None, **columns):
        item = create_item(self.db, title="Old idea", priority=4, **{"kind": "task", **columns})
        set_item_fields(self.db, item, parked_at="2026-10-09T08:10:00+00:00", piece=piece)
        self.db.commit()
        return item_state(self.db, item)

    def test_an_unpark_is_written_noted_and_revision_checked(self):
        state = self.parked()
        item = state["item"]["id"]
        with self.assertRaises(StaleItem):
            edit_item(self.db, item, {"parked_at": None}, who="operator", expected_revision="0" * 64)
        self.assertEqual(item_state(self.db, item), state)
        unparked = edit_item(self.db, item, {"parked_at": None}, who="operator",
                             expected_revision=state["revision"])
        self.assertIsNone(unparked["item"]["parked_at"])
        self.assertEqual(unparked["notes"][-1]["body"], "Unparked by operator")

    def test_any_kind_but_a_writing_piece_unparks(self):
        upsert_repo(self.db, "/repos/work")
        for kind, columns in (("work-idea", {}), ("work", {"repo": "/repos/work"})):
            with self.subTest(kind=kind):
                state = self.parked(kind=kind, **columns)
                self.assertIsNone(edit_item(self.db, state["item"]["id"], {"parked_at": None},
                                            who="operator")["item"]["parked_at"])
        state = self.parked(kind="idea", piece="drafts/old-idea.md")
        with self.assertRaisesRegex(WorkflowError, "revives through its own park control"):
            edit_item(self.db, state["item"]["id"], {"parked_at": None}, who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_an_unparked_item_is_left_alone(self):
        open_item = item_state(self.db, create_item(self.db, kind="task", title="Open"))
        self.assertEqual(edit_item(self.db, open_item["item"]["id"], {"parked_at": None}, who="operator"), open_item)


class Parking(WorkflowCase):
    """sd:5905: `edit_item` parks an item by hand when given an aware
    timestamp, which the dashboard's Park control and `sd task edit --park`
    send; the nightly prune is no longer the only parker."""

    def test_a_park_is_written_in_one_shape_and_noted(self):
        state = item_state(self.db, create_item(self.db, kind="task", title="Later"))
        item = state["item"]["id"]
        with self.assertRaises(StaleItem):
            edit_item(self.db, item, {"parked_at": "2026-10-23T18:00:00Z"}, who="operator",
                      expected_revision="0" * 64)
        parked = edit_item(self.db, item, {"parked_at": "2026-10-23T18:00:00Z"}, who="operator",
                           expected_revision=state["revision"])
        self.assertEqual(parked["item"]["parked_at"], "2026-10-23T18:00:00+00:00")
        self.assertEqual(parked["notes"][-1]["body"], "Parked by operator")
        self.assertTrue(is_audit_note(parked["notes"][-1]))

    def test_a_value_that_is_not_an_aware_timestamp_is_refused(self):
        state = item_state(self.db, create_item(self.db, kind="task", title="Later"))
        for value in ("2026-10-10", "2026-10-10T08:00:00", "soon", True, 1):
            with self.subTest(value=value), self.assertRaisesRegex(WorkflowError, "parked_at"):
                edit_item(self.db, state["item"]["id"], {"parked_at": value}, who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_a_done_item_is_refused(self):
        item = create_item(self.db, kind="task", title="Shipped")
        self.db.execute("UPDATE item SET status = 'done' WHERE id = ?", (item,))
        self.db.commit()
        state = item_state(self.db, item)
        with self.assertRaisesRegex(WorkflowError, "is done"):
            edit_item(self.db, item, {"parked_at": "2026-10-23T18:00:00+00:00"}, who="operator")
        self.assertEqual(item_state(self.db, item), state)

    def test_an_already_parked_item_keeps_its_stamp_and_gets_no_note(self):
        item = create_item(self.db, kind="task", title="Old idea", priority=4)
        set_item_fields(self.db, item, parked_at="2026-10-09T08:10:00+00:00")
        self.db.commit()
        state = item_state(self.db, item)
        self.assertEqual(edit_item(self.db, item, {"parked_at": "2026-10-23T18:00:00+00:00"},
                                   who="operator"), state)

    def test_a_writing_piece_parks_through_its_own_control(self):
        item = create_item(self.db, kind="idea", title="Draft")
        set_item_fields(self.db, item, piece="drafts/draft.md")
        self.db.commit()
        state = item_state(self.db, item)
        with self.assertRaisesRegex(WorkflowError, "its own park control"):
            edit_item(self.db, item, {"parked_at": "2026-10-23T18:00:00+00:00"}, who="operator")
        self.assertEqual(item_state(self.db, item), state)

class KindEditing(WorkflowCase):
    """sd:743 -- a row's kind changes through `edit_item`, attributed and noted."""

    OLD = "2000-01-01T00:00:00+00:00"

    def backdate(self, item):
        self.db.execute("UPDATE item SET updated_at = ? WHERE id = ?", (self.OLD, item))
        self.db.commit()

    def refused(self, item, changes, pattern, **options):
        before, counts = item_state(self.db, item), self.counts()
        with self.assertRaisesRegex(WorkflowError, pattern):
            edit_item(self.db, item, changes, who="operator", **options)
        self.assertEqual(item_state(self.db, item), before)
        self.assertEqual(self.counts(), counts)

    def test_hand_kinds_are_kinds_the_schema_allows(self):
        self.assertTrue(set(HAND_KINDS) <= set(schema_kinds(self.db)))
        self.assertIn("report", schema_kinds(self.db))

    def test_every_kind_list_names_only_kinds_the_schema_allows(self):
        """sd:7004: `personal` and `skill-review` left the CHECK, so no list
        the library or the dashboard keys on may name either."""
        from sd_db import priority_sweep, progress, runner, workflow

        lists = {"HAND_KINDS": workflow.HAND_KINDS, "REPO_LESS_KINDS": workflow.REPO_LESS_KINDS,
                 "DETAIL_KINDS": workflow.DETAIL_KINDS, "TASK_STATUS_KINDS": workflow.TASK_STATUS_KINDS,
                 "RUNNABLE_KINDS": runner.RUNNABLE_KINDS, "priority_sweep.KINDS": priority_sweep.KINDS,
                 "CANCELLABLE_TASK_KINDS": progress.CANCELLABLE_TASK_KINDS}
        allowed = set(schema_kinds(self.db))
        for name, kinds in lists.items():
            with self.subTest(name=name):
                self.assertEqual(set(kinds) - allowed, set())
        self.assertEqual(HAND_KINDS, ("task", "followup", "work-idea", "personal-idea"))
        self.assertEqual(REPO_LESS_KINDS, frozenset({"work-idea", "personal-idea"}))

    def test_a_legal_change_writes_kind_updated_at_and_a_note_naming_both_kinds_and_who(self):
        state = self.capture()
        item = state["item"]["id"]
        self.backdate(item)
        state = item_state(self.db, item)
        edited = edit_item(self.db, item, {"kind": "work-idea"}, who="alex",
                           expected_revision=state["revision"])
        self.assertEqual(edited["item"]["kind"], "work-idea")
        self.assertNotEqual(edited["item"]["updated_at"], self.OLD)
        note = edited["notes"][-1]
        self.assertEqual((note["kind"], note["body"], note["session"]),
                         ("comment", "Changed kind task -> work-idea by alex", "alex"))
        # A row outside task and work is reclassified by the same verb, kind only.
        back = edit_item(self.db, item, {"kind": "task"}, who="alex")
        self.assertEqual(back["item"]["kind"], "task")
        self.assertEqual(back["notes"][-1]["body"], "Changed kind work-idea -> task by alex")
        with self.assertRaisesRegex(WorkflowError, "own editing workflow"):
            edit_item(self.db, create_item(self.db, kind="work-idea", title="Errand"),
                      {"kind": "task", "title": "Renamed"}, who="alex")

    def test_an_idea_without_a_piece_can_be_reclassified(self):
        item = create_item(self.db, kind="idea", title="Book idea")
        edited = edit_item(self.db, item, {"kind": "personal-idea"}, who="alex")
        self.assertEqual(edited["item"]["kind"], "personal-idea")
        self.assertEqual(edited["notes"][-1]["body"], "Changed kind idea -> personal-idea by alex")

    def test_a_vault_base_row_keeps_its_kind_because_its_plugin_lists_it_by_that_kind(self):
        # The vault lands its rows with `piece` NULL, so the piece rule
        # misses them; `records.list_rows` selects them by item kind idea.
        item = create_item(self.db, kind="idea", title="Vault note", source="vault",
                           external_id="notes/vault-note.md")
        self.refused(item, {"kind": "personal-idea"}, "base row from the vault")

    def test_an_unknown_kind_is_refused_and_nothing_is_written(self):
        item = self.capture()["item"]["id"]
        for kind in ("nonsense", "", None, 3):
            with self.subTest(kind=kind):
                self.refused(item, {"kind": kind}, "no item kind")

    def test_an_idea_carrying_a_piece_keeps_its_kind(self):
        item = create_item(self.db, kind="idea", title="An article")
        set_item_fields(self.db, item, piece="pieces/an-article")
        self.db.commit()
        self.refused(item, {"kind": "work-idea"}, "writing piece")

    def test_a_produced_kind_cannot_be_set_or_left_by_hand(self):
        produced = [kind for kind in schema_kinds(self.db) if kind not in HAND_KINDS]
        self.assertTrue(produced)
        item = self.capture()["item"]["id"]
        for kind in produced:
            with self.subTest(target=kind):
                self.refused(item, {"kind": kind}, "own producer")
        upsert_repo(self.db, "/repos/work")
        for kind in ("work", "report"):
            source = create_item(self.db, kind=kind, title=kind,
                                 repo="/repos/work" if kind == "work" else None)
            with self.subTest(source=kind):
                self.refused(source, {"kind": "task"}, "owned by their producer")

    def test_a_repository_less_kind_needs_the_repository_cleared_in_the_same_edit(self):
        upsert_repo(self.db, "/repos/one")
        for kind in sorted(REPO_LESS_KINDS):
            with self.subTest(kind=kind):
                item = self.capture(repo="/repos/one")["item"]["id"]
                self.refused(item, {"kind": kind}, "carries no repository")
                edited = edit_item(self.db, item, {"kind": kind, "repo": None}, who="alex")
                self.assertEqual((edited["item"]["kind"], edited["item"]["repo"]), (kind, None))
                self.assertEqual(edited["notes"][-1]["body"],
                                 f"Changed kind task -> {kind} by alex\nUpdated repo by alex")

    def test_the_repository_less_kinds_are_the_two_ideas(self):
        """sd:809. `followup` left the set; a followup is about one repository.
        `personal` left with its kind (sd:7004)."""
        self.assertEqual(REPO_LESS_KINDS, frozenset({"work-idea", "personal-idea"}))
        self.assertLess(REPO_LESS_KINDS, frozenset(HAND_KINDS))

    def test_a_queued_running_or_ending_assignment_blocks_a_kind_change(self):
        for status in ("queued", "running", "ending"):
            item = self.capture()["item"]["id"]
            create_assignment(self.db, item=item, role="author", status=status)
            with self.subTest(status=status):
                self.refused(item, {"kind": "work-idea"}, f"{status} assignment")

    def test_a_contribution_task_keeps_its_kind_so_a_round_trip_cannot_skip_its_checks(self):
        """Review of #332, B1: `contributions._registered` reads only `kind='task'`.

        Moving a contribution away and back let a second row take its pull
        request and let a dependency cycle form. The first move is refused.
        """
        pull = "https://github.com/o/r/pull/1"
        first = contributions.capture(self.db, title="A", changes={"pull_url": pull}, who="operator")["item"]["id"]
        self.refused(first, {"kind": "work-idea"}, "contribution")
        with self.assertRaisesRegex(WorkflowError, "already belongs"):
            contributions.capture(self.db, title="B", changes={"pull_url": pull}, who="operator")

        x = contributions.capture(self.db, title="X", changes={"pull_url": "https://github.com/o/r/pull/3"},
                                  who="operator")["item"]["id"]
        y = contributions.capture(self.db, title="Y", changes={"pull_url": "https://github.com/o/r/pull/4"},
                                  who="operator")["item"]["id"]
        contributions.configure(self.db, y, {"depends_on": [{"kind": "item", "item": x}]}, who="operator")
        self.refused(y, {"kind": "followup"}, "contribution")
        with self.assertRaisesRegex(WorkflowError, "cycle"):
            contributions.configure(self.db, x, {"depends_on": [{"kind": "item", "item": y}]}, who="operator")

    def test_a_skill_review_task_keeps_its_kind(self):
        item = create_item(self.db, kind="task", title="Apply proposals",
                           fields={"skill_review": {"action": "apply"}})
        self.refused(item, {"kind": "work-idea"}, "skill_review")

    def test_unreadable_fields_refuse_a_kind_change(self):
        item = self.capture()["item"]["id"]
        self.db.execute("UPDATE item SET fields = 'not json' WHERE id = ?", (item,))
        self.db.commit()
        self.refused(item, {"kind": "work-idea"}, "unreadable fields")

    def test_schema_kinds_reads_the_check_this_store_carries(self):
        narrow = sqlite3.connect(":memory:")
        self.addCleanup(narrow.close)
        narrow.row_factory = sqlite3.Row
        narrow.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, "
                       "kind TEXT NOT NULL CHECK (kind IN ('work', 'task')))")
        self.assertEqual(schema_kinds(narrow), ("work", "task"))
        narrow.execute("DROP TABLE item")
        narrow.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, kind TEXT NOT NULL)")
        with self.assertRaisesRegex(WorkflowError, "no CHECK"):
            schema_kinds(narrow)

    def test_an_unchanged_kind_beside_another_field_writes_only_that_field_s_note(self):
        item = self.capture()["item"]["id"]
        edited = edit_item(self.db, item, {"kind": "task", "title": "Renamed"}, who="alex")
        self.assertEqual((edited["item"]["kind"], edited["item"]["title"]), ("task", "Renamed"))
        self.assertEqual(edited["notes"][-1]["body"], "Updated title by alex")

    def test_an_idea_with_a_repository_reaches_a_repository_less_kind_in_one_edit(self):
        upsert_repo(self.db, "/repos/one")
        item = create_item(self.db, kind="idea", title="Side project", repo="/repos/one")
        # The clear rides only on a real reclassification.
        self.refused(item, {"kind": "idea", "repo": None}, "own editing workflow")
        self.refused(item, {"kind": "work-idea", "repo": "/repos/one"}, "own editing workflow")
        edited = edit_item(self.db, item, {"kind": "personal-idea", "repo": None}, who="alex")
        self.assertEqual((edited["item"]["kind"], edited["item"]["repo"]), ("personal-idea", None))
        self.assertEqual(edited["notes"][-1]["body"],
                         "Changed kind idea -> personal-idea by alex\nUpdated repo by alex")

    def test_an_idea_moves_between_repositories_and_keeps_its_kind(self):
        """sd:5142: a base row moves to the repository it belongs to and stays in its base and page."""
        upsert_repo(self.db, "/repos/one")
        item = create_item(self.db, kind="idea", title="Blog idea", source="vault",
                           external_id="blog-idea:Blog Ideas/Blog idea.md", fields={"kind": "blog-idea"})
        moved = edit_item(self.db, item, {"repo": "/repos/one"}, who="alex")
        self.assertEqual((moved["item"]["kind"], moved["item"]["repo"], moved["item"]["source"]),
                         ("idea", "/repos/one", "vault"))
        self.assertEqual(moved["notes"][-1]["body"], "Updated repo by alex")
        back = edit_item(self.db, item, {"repo": None}, who="alex")
        self.assertEqual((back["item"]["kind"], back["item"]["repo"]), ("idea", None))
        # Every other field keeps its own editing workflow, a move beside it too.
        self.refused(item, {"title": "Renamed"}, "own editing workflow")
        self.refused(item, {"repo": "/repos/one", "priority": 1}, "own editing workflow")
        self.refused(item, {"repo": "/repos/unregistered"}, "is not registered")
        personal = create_item(self.db, kind="personal-idea", title="Mine")
        self.refused(personal, {"repo": "/repos/one"}, "own editing workflow")

    def test_a_writing_piece_keeps_its_repository_because_its_file_lives_there(self):
        upsert_repo(self.db, "/repos/one")
        upsert_repo(self.db, "/repos/two")
        item = create_item(self.db, kind="idea", title="Draft", repo="/repos/one")
        set_item_fields(self.db, item, piece="pieces/draft")
        self.refused(item, {"repo": "/repos/two"}, "writing piece")

    def test_who_is_required_and_has_no_default(self):
        item = self.capture()["item"]["id"]
        before = item_state(self.db, item)
        with self.assertRaises(TypeError):
            edit_item(self.db, item, {"kind": "work-idea"})
        self.assertEqual(item_state(self.db, item), before)
        self.assertEqual(edit_item(self.db, item, {"kind": "work-idea"}, who="alex")["item"]["kind"],
                         "work-idea")

    def test_a_stale_revision_is_refused_and_a_current_one_applies(self):
        state = self.capture()
        item = state["item"]["id"]
        edit_item(self.db, item, {"title": "Moved on"}, who="operator")
        before, counts = item_state(self.db, item), self.counts()
        with self.assertRaises(StaleItem):
            edit_item(self.db, item, {"kind": "work-idea"}, who="operator",
                      expected_revision=state["revision"])
        self.assertEqual((item_state(self.db, item), self.counts()), (before, counts))
        edited = edit_item(self.db, item, {"kind": "work-idea"}, who="operator",
                           expected_revision=before["revision"])
        self.assertEqual(edited["item"]["kind"], "work-idea")


class FollowupRepository(WorkflowCase):
    """sd:809. A followup item carries a repository and edits like a task.

    Before this a followup was one of the repository-less kinds: a move into
    it had to clear `repo`, and `edit_item` refused its every field but `kind`,
    so a followup filed off its checkout could not be moved onto it.
    """

    def setUp(self):
        super().setUp()
        upsert_repo(self.db, "/repos/one")
        upsert_repo(self.db, "/repos/two")

    def refused(self, item, changes, pattern):
        before, counts = item_state(self.db, item), self.counts()
        with self.assertRaisesRegex(WorkflowError, pattern):
            edit_item(self.db, item, changes, who="operator")
        self.assertEqual(item_state(self.db, item), before)
        self.assertEqual(self.counts(), counts)

    def test_a_task_with_a_repository_becomes_a_followup_and_keeps_it(self):
        item = self.capture(repo="/repos/one")["item"]["id"]
        edited = edit_item(self.db, item, {"kind": "followup"}, who="alex")
        self.assertEqual((edited["item"]["kind"], edited["item"]["repo"]), ("followup", "/repos/one"))
        self.assertEqual(edited["notes"][-1]["body"], "Changed kind task -> followup by alex")

    def test_a_followup_takes_a_repository_title_priority_and_due(self):
        item = create_item(self.db, kind="followup", title="Chase the review finding")
        state = item_state(self.db, item)
        edited = edit_item(self.db, item, {"repo": "/repos/one", "title": "Fix the finding",
                                           "priority": 2, "due": "2026-09-20"},
                           who="alex", expected_revision=state["revision"])
        row = edited["item"]
        self.assertEqual((row["kind"], row["repo"], row["title"], row["priority"], row["due"]),
                         ("followup", "/repos/one", "Fix the finding", 2, "2026-09-20"))
        self.assertEqual(edited["notes"][-1]["body"], "Updated due, priority, repo, title by alex")

        moved = edit_item(self.db, item, {"repo": "/repos/two"}, who="alex")
        self.assertEqual(moved["item"]["repo"], "/repos/two")
        cleared = edit_item(self.db, item, {"repo": None, "priority": None, "due": None}, who="alex")
        self.assertEqual((cleared["item"]["repo"], cleared["item"]["priority"], cleared["item"]["due"]),
                         (None, None, None))
        self.assertEqual(cleared["notes"][-1]["body"], "Updated due, priority, repo by alex")
        self.assertEqual(item_by_id(self.db, item)["repo"], None)

    def test_a_followup_refuses_an_unregistered_repository(self):
        item = create_item(self.db, kind="followup", title="Somewhere else")
        self.refused(item, {"repo": "/repos/unregistered"}, "not registered")

    def test_a_followup_with_a_repository_leaves_it_only_for_a_repository_less_kind(self):
        item = create_item(self.db, kind="followup", title="About one repository", repo="/repos/one")
        self.refused(item, {"kind": "work-idea"}, "carries no repository")
        back = edit_item(self.db, item, {"kind": "task"}, who="alex")
        self.assertEqual((back["item"]["kind"], back["item"]["repo"]), ("task", "/repos/one"))
        again = edit_item(self.db, item, {"kind": "followup"}, who="alex")
        idea = edit_item(self.db, item, {"kind": "work-idea", "repo": None}, who="alex",
                             expected_revision=again["revision"])
        self.assertEqual((idea["item"]["kind"], idea["item"]["repo"]), ("work-idea", None))

    def test_a_work_idea_becomes_a_followup_before_it_takes_a_repository(self):
        item = create_item(self.db, kind="work-idea", title="Turned out to be about a repository")
        self.refused(item, {"kind": "followup", "repo": "/repos/one"}, "work-idea items use their own editing workflow")
        followup = edit_item(self.db, item, {"kind": "followup"}, who="alex")
        moved = edit_item(self.db, item, {"repo": "/repos/one"}, who="alex", expected_revision=followup["revision"])
        self.assertEqual((moved["item"]["kind"], moved["item"]["repo"]), ("followup", "/repos/one"))

    def test_the_repository_less_kinds_still_refuse_their_other_fields(self):
        for kind in sorted(REPO_LESS_KINDS):
            item = create_item(self.db, kind=kind, title=f"A {kind}")
            with self.subTest(kind=kind):
                self.refused(item, {"title": "Renamed"}, "own editing workflow")
                self.refused(item, {"repo": "/repos/one"}, "own editing workflow")
                self.refused(item, {"priority": 1}, "own editing workflow")

    def test_a_followup_body_is_edited_as_a_task_body_is(self):
        item = create_item(self.db, kind="followup", title="Correct the finding")
        edited = edit_item(self.db, item, {"body": "The finding is at line 12"}, who="alex")
        self.assertEqual(json.loads(edited["item"]["body"]), {"text": "The finding is at line 12"})
        self.assertEqual(edited["notes"][-1]["body"], "Updated body by alex")
        # An equal submission writes nothing, as for a task.
        self.assertEqual(edit_item(self.db, item, {"body": "The finding is at line 12"}, who="alex"),
                         edited)
        for kind in ("work-idea", "work"):
            if kind == "work":
                upsert_repo(self.db, "/repos/work")
            other = create_item(self.db, kind=kind, title=f"A {kind}",
                                repo="/repos/work" if kind == "work" else None)
            with self.subTest(kind=kind):
                self.refused(other, {"body": "New text"},
                             "own editing workflow" if kind == "work-idea" else "task and followup items")


class TaskNotes(WorkflowCase):
    def test_add_and_resolve_note_are_reflected_in_revision_and_readback(self):
        state = self.capture()
        added = add_item_note(self.db, state["item"]["id"], body="Follow through",
                              kind="followup", who="user", expected_revision=state["revision"])
        self.assertNotEqual(added["revision"], state["revision"])
        self.assertEqual(added["note"]["session"], "user")
        resolved = resolve_item_note(self.db, added["note"]["id"], expected_revision=added["revision"], who="operator")
        self.assertTrue(resolved["note"]["resolved_at"])
        self.assertNotEqual(resolved["revision"], added["revision"])
        self.assertEqual(resolve_item_note(self.db, added["note"]["id"], who="operator"), resolved)

    def test_status_and_execution_history_cannot_be_injected_or_resolved(self):
        state = self.capture()
        for kind in ("status_change", "exec", "unknown"):
            with self.subTest(kind=kind), self.assertRaises(WorkflowError):
                add_item_note(self.db, state["item"]["id"], kind=kind, body="forged", who="operator")
        with self.assertRaises(WorkflowError):
            resolve_item_note(self.db, state["notes"][0]["id"], who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"]), state)

    def test_missing_note_is_an_exact_error(self):
        with self.assertRaisesRegex(MissingNote, "^no note 999$"):
            resolve_item_note(self.db, 999, who="operator")

    def test_stale_note_resolution_leaves_note_unresolved(self):
        state = self.capture()
        added = add_item_note(self.db, state["item"]["id"], body="First", who="operator")
        current = add_item_note(self.db, state["item"]["id"], body="Second", who="operator")
        with self.assertRaises(StaleItem):
            resolve_item_note(self.db, added["note"]["id"], expected_revision=added["revision"], who="operator")
        self.assertEqual(item_state(self.db, state["item"]["id"])["revision"], current["revision"])


class NestedTransactions(WorkflowCase):
    def test_outer_failure_rolls_back_successful_inner_write(self):
        with self.assertRaises(RuntimeError):
            with transaction(self.db):
                create_item(self.db, kind="task", title="Rolled back")
                raise RuntimeError("abort outer")
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_inner_failure_can_be_caught_without_discarding_outer_work(self):
        with transaction(self.db):
            create_item(self.db, kind="task", title="Kept")
            with self.assertRaises(RuntimeError):
                with transaction(self.db):
                    create_item(self.db, kind="task", title="Discarded")
                    raise RuntimeError("abort inner")
            create_item(self.db, kind="task", title="Also kept")
        self.assertEqual([row[0] for row in self.db.execute("SELECT title FROM item ORDER BY id")],
                         ["Kept", "Also kept"])


class BinaryColumns(WorkflowCase):
    """sd:874 -- `item_state` hashes a row a BLOB column makes unserialisable.

    `item.fields` and `item.body` are declared `TEXT`. TEXT affinity converts
    every numeric storage class to text before storing, so an INTEGER, a REAL,
    a CAST and a bound Python int or float all come back as `str` and
    `json.loads` never receives a number from these columns. BLOB is the one
    storage class affinity leaves alone: it comes back as `bytes`.

    `item_state` json.dumps-es the readback to compute the revision, and
    `json.JSONEncoder` has no rule for `bytes`. A BLOB carrying perfectly valid
    JSON died there too, before any caller's parse guard, with
    `TypeError: Object of type bytes is not JSON serializable`. The dashboard's
    `do_GET` catches neither, so the reader got no response at all.

    The fix belongs here rather than in a `CHECK` on the column: a constraint
    would reject the rows the store already holds and would not tell an
    existing reader why its revision changed. Encoding bytes for the hash
    leaves every text row's revision byte-identical, which the last test here
    is what proves.
    """

    def stored(self, value, column="fields"):
        item = create_item(self.db, kind="task", title="Binary")
        self.db.execute(f"UPDATE item SET {column} = ? WHERE id = ?", (value, item))
        self.db.commit()
        return item

    def test_affinity_leaves_only_a_blob_as_bytes(self):
        for value in (12345, 1.5, "12345"):
            with self.subTest(value=value):
                item = self.stored(value)
                self.assertIsInstance(item_by_id(self.db, item)["fields"], str)
        item = self.stored(b'{"report": {}}')
        self.assertIsInstance(item_by_id(self.db, item)["fields"], bytes)

    def test_a_blob_column_still_yields_a_revision(self):
        for column in ("fields", "body"):
            for value in (b'{"report": {}}', b"\xff\xfe not json"):
                with self.subTest(column=column, value=value):
                    state = item_state(self.db, self.stored(value, column))
                    self.assertRegex(state["revision"], r"\A[a-f0-9]{64}\Z")
                    self.assertEqual(state["item"][column], value)

    def swap(self, item, value, column="fields"):
        """Rewrite one column on one row and nothing else.

        The same row, so the revision cannot differ by `id` or `created_at`;
        a plain `UPDATE` rather than `set_item_fields`, so it cannot differ by
        `updated_at` either. What is left is the value, which is the point.
        """
        self.db.execute(f"UPDATE item SET {column} = ? WHERE id = ?", (value, item))
        self.db.commit()
        return item_state(self.db, item)["revision"]

    def test_different_blobs_do_not_share_a_revision(self):
        item = self.stored(b'{"a": 1}')
        before = item_state(self.db, item)["revision"]
        self.assertNotEqual(self.swap(item, b'{"a": 2}'), before)

    def test_a_blob_and_the_same_bytes_as_text_do_not_share_a_revision(self):
        item = self.stored(b'{"a": 1}')
        before = item_state(self.db, item)["revision"]
        self.assertNotEqual(self.swap(item, '{"a": 1}'), before)

    def test_a_text_row_revision_is_the_sha256_of_the_plain_dump(self):
        """The encoder runs only for a type `json.dumps` refuses, so no row
        that works today gets a new revision and no open edit goes stale."""
        item = self.stored('{"text": "ordinary"}', "body")
        state = item_state(self.db, item)
        plain = {"item": state["item"], "notes": state["notes"]}
        expected = hashlib.sha256(
            json.dumps(plain, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(state["revision"], expected)


if __name__ == "__main__":
    unittest.main()


class RefusalsNameActionsNotVerbs(unittest.TestCase):
    """sd:3503. A library refusal is the dashboard's error text too, so it names
    the action ("resolve followup 12 first"), never a CLI verb ("sd note resolve 12").

    Every `raise` of a `WorkflowError` class or `TransitionRefused` in
    `lib/sd_db` is read: the strings inside the call, and the strings a name
    used in the call was assigned in the same function.
    """

    COMMAND = __import__("re").compile(r"\bsd [a-z]")

    def refusal_classes(self):
        import importlib
        import pkgutil

        import sd_db
        from sd_db.workflow import WorkflowError

        for module in pkgutil.iter_modules(sd_db.__path__):
            importlib.import_module(f"sd_db.{module.name}")
        found, todo = {TransitionRefused.__name__}, [WorkflowError]
        while todo:
            cls = todo.pop()
            found.add(cls.__name__)
            todo.extend(cls.__subclasses__())
        return found

    def test_no_refusal_names_a_cli_verb(self):
        import ast

        import sd_db

        names = self.refusal_classes()
        self.assertIn("MissingNote", names, "the class walk missed a class that exists")
        root = Path(sd_db.__file__).parent
        offenders = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in [tree, *(node for node in ast.walk(tree)
                                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))]:
                assigned: dict[str, list[str]] = {}
                for node in ast.walk(function):
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                assigned.setdefault(target.id, []).extend(self.strings(node.value))
                for node in ast.walk(function):
                    if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                        continue
                    func = node.exc.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                    if name not in names:
                        continue
                    texts = self.strings(node.exc)
                    for used in ast.walk(node.exc):
                        if isinstance(used, ast.Name):
                            texts.extend(assigned.get(used.id, []))
                    if any(self.COMMAND.search(text) for text in texts):
                        offenders.append(f"{path.relative_to(root)}:{node.lineno}")
        self.assertEqual(sorted(set(offenders)), [])

    @staticmethod
    def strings(node):
        import ast

        return [found.value for found in ast.walk(node)
                if isinstance(found, ast.Constant) and isinstance(found.value, str)]


class TheRetiredWorkProducers(unittest.TestCase):
    """Nothing files a `work` row any more (pack sd:7064, slice 1).

    The kind is retiring: migration 047 moves its rows to `task` and drops it
    from the CHECK, so every producer goes first. A keyword argument or a
    payload key naming the kind is what a producer writes; a reader compares,
    which this does not match.
    """

    PRODUCER = re.compile(r"""\bkind=["']work["']|["']kind["']:\s*["']work["']""")

    def test_no_producer_names_kind_work(self):
        root = Path(__file__).resolve().parents[2]
        sources = [*(root / "lib" / "sd_db").rglob("*.py"), *(root / "bin").iterdir(),
                   *(root / "dashboard" / "sd_dashboard").rglob("*.py")]
        offenders = []
        for path in sorted(sources):
            if not path.is_file() or path.suffix not in ("", ".py"):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self.PRODUCER.search(line):
                    offenders.append(f"{path.relative_to(root)}:{number}")
        self.assertEqual(offenders, [])
