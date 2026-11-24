"""Identity, completion evidence and tracker freshness cross real boundaries."""

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, create_item, upsert_repo
from sd_db.migrate import initialise
from sd_db.progress import (
    cancel_work,
    completion_record,
    deliver_associated_work,
    deliver_work,
    item_for_artifact,
    relink_artifact,
    task_guard,
    tracker_freshness,
    tracker_items,
)
from sd_db.shadow_sync import write_watermark
from sd_db.workflow import (
    StaleItem,
    WorkflowError,
    allowed_statuses,
    change_status,
    edit_item,
    item_state,
)
from sd_db.writes import TransitionRefused, record_state, transition, upsert_shadow


class ProgressCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Workflow test")
        self.git("config", "user.email", "workflow@example.test")
        self.git("commit", "--allow-empty", "-m", "Initial")
        self.path = self.root / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        upsert_repo(self.db, str(self.repo))
        self.item = create_item(self.db, kind="work", title="Stable work", repo=str(self.repo),
                                path="docs/work/original/prd.md", source="docs/work",
                                external_id=f"{self.repo}::docs/work/original/prd.md")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], text=True, capture_output=True,
                              check=True).stdout.strip()

    def commit(self, trailer=None):
        message = "A slice" + (f"\n\n{trailer}" if trailer else "")
        self.git("commit", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def remote(self):
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True,
                       capture_output=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-u", "origin", "main")
        return remote


class ArtifactIdentity(ProgressCase):
    def test_relink_preserves_stable_identity_and_history(self):
        artifact = self.repo / "docs/work/archive/2026-09/renamed/prd.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# Same work, moved\n")
        before = item_state(self.db, self.item)
        result = relink_artifact(self.db, self.item, str(artifact.relative_to(self.repo)),
                                 expected_revision=before["revision"], who="operator")
        for name in ("id", "source", "external_id", "repo", "status"):
            self.assertEqual(result["item"][name], before["item"][name])
        self.assertEqual(result["notes"][:len(before["notes"])], before["notes"])
        self.assertEqual(result["item"]["path"], "docs/work/archive/2026-09/renamed/prd.md")
        self.assertIn("Relinked artifact", result["notes"][-1]["body"])
        self.assertEqual(item_for_artifact(self.db, str(self.repo), result["item"]["path"])["id"], self.item)
        self.assertIsNone(item_for_artifact(self.db, str(self.repo), "docs/work/original/prd.md"))

    def test_archive_resolution_uses_original_identity_without_title_matching(self):
        found = item_for_artifact(self.db, str(self.repo), "docs/work/archive/2026-09/original/prd.md")
        self.assertEqual(found["id"], self.item)
        self.assertIsNone(item_for_artifact(self.db, str(self.repo), "docs/work/same-title/prd.md"))

    def test_ambiguous_exact_artifact_refuses_instead_of_selecting_one_row(self):
        create_item(self.db, kind="work", title="Duplicate path", repo=str(self.repo),
                    path="docs/work/original/prd.md")
        with self.assertRaisesRegex(WorkflowError, "multiple"):
            item_for_artifact(self.db, str(self.repo), "docs/work/original/prd.md")

    def test_relink_refuses_missing_escaping_or_claimed_paths(self):
        (self.root / "outside.md").write_text("outside")
        linked = self.repo / "link.md"
        linked.symlink_to(self.root / "outside.md")
        for path in ("missing.md", "../outside.md", str(self.root / "outside.md"), "link.md"):
            before = item_state(self.db, self.item)
            with self.subTest(path=path), self.assertRaises(WorkflowError):
                relink_artifact(self.db, self.item, path, who="operator")
            self.assertEqual(item_state(self.db, self.item), before)
        (self.repo / "other.md").write_text("other")
        create_item(self.db, kind="work", title="Other", repo=str(self.repo), path="other.md")
        with self.assertRaisesRegex(WorkflowError, "already"):
            relink_artifact(self.db, self.item, "other.md", who="operator")

    def test_relink_refusals_name_the_path_and_the_next_step(self):
        # sd:3515: each refusal needed a second command to learn what to do.
        with self.assertRaises(WorkflowError) as missing:
            relink_artifact(self.db, self.item, "docs/work/missing/prd.md", who="operator")
        self.assertIn("docs/work/missing/prd.md", str(missing.exception))
        self.assertIn(str(self.repo.resolve()), str(missing.exception))
        (self.repo / "other.md").write_text("other")
        other = create_item(self.db, kind="work", title="Other", repo=str(self.repo), path="other.md")
        with self.assertRaises(WorkflowError) as claimed:
            relink_artifact(self.db, self.item, "other.md", who="operator")
        self.assertIn(f"relink item {other}", str(claimed.exception))


class WorkCompletion(ProgressCase):
    def test_public_transition_cannot_bypass_work_completion(self):
        with self.assertRaisesRegex(TransitionRefused, "deliver_work|completion"):
            transition(self.db, self.item, "done", who="dashboard")

    def test_cancellation_needs_reason_and_never_marks_shipped(self):
        before = item_state(self.db, self.item)
        with self.assertRaises(WorkflowError):
            cancel_work(self.db, self.item, reason=" ", who="operator")
        self.assertEqual(item_state(self.db, self.item), before)
        result = cancel_work(self.db, self.item, reason="No longer needed", who="operator")
        self.assertEqual(result["item"]["status"], "done")
        self.assertIsNone(result["item"]["shipped_at"])
        self.assertEqual(json.loads(result["item"]["fields"])["completion"]["outcome"], "cancelled")
        self.assertIn("cancelled", result["notes"][-1]["body"])
        self.assertEqual(cancel_work(self.db, self.item, reason="No longer needed", who="operator"), result)
        self.assertEqual(allowed_statuses(self.db, self.item), [])
        with self.assertRaisesRegex(TransitionRefused, "terminal"):
            change_status(self.db, self.item, "planning", who="operator")

    def test_delivery_verifies_real_remote_commit_and_is_idempotent(self):
        self.remote()
        commit = self.commit(f"Delivers: sd:{self.item}")
        self.git("push", "origin", "main")
        result = deliver_work(self.db, self.item, commit, who="operator")
        self.assertEqual(result["item"]["status"], "done")
        self.assertTrue(result["item"]["shipped_at"])
        evidence = json.loads(result["item"]["fields"])["completion"]
        self.assertEqual((evidence["outcome"], evidence["commit"]), ("delivered", commit))
        self.assertEqual(deliver_work(self.db, self.item, commit, who="operator"), result)
        with self.assertRaises(WorkflowError):
            cancel_work(self.db, self.item, reason="Cannot rewrite delivery", who="operator")

    def test_a_delivered_work_item_keeps_its_open_followup_in_today(self):
        """sd:9869 round 9: a merge marked work done without `kept_followups`, so Today hid
        a followup the delivery does not answer. sd-ship's primary and co-delivered work both
        deliver here."""
        from sd_db import reads
        from sd_db.writes import add_note
        waiting = add_note(self.db, self.item, "followup", "Waits on an outside reply")
        self.remote()
        commit = self.commit(f"Delivers: sd:{self.item}")
        self.git("push", "origin", "main")
        deliver_work(self.db, self.item, commit, who="sd-ship")
        self.assertEqual([row["id"] for row in reads.open_followups(self.db)], [waiting])

    def test_a_delivery_resolves_the_machines_followups(self):
        """sd:10407: a delivery kept a `Build blocked:` followup open on a done item; 642 open
        followups sat on done items. The machine's own the delivery answers; a person's stays."""
        from sd_db import reads
        from sd_db.writes import add_note
        waiting = add_note(self.db, self.item, "followup", "Waits on an outside reply")
        blocked = add_note(self.db, self.item, "followup", "Build blocked: gate red. Look at the log")
        pasted = add_note(self.db, self.item, "followup", "Build blocked: the vendor must answer", session="operator")
        self.remote()
        commit = self.commit(f"Delivers: sd:{self.item}")
        self.git("push", "origin", "main")
        result = deliver_work(self.db, self.item, commit, who="sd-ship")
        self.assertEqual([row["id"] for row in reads.open_followups(self.db)], [waiting, pasted])
        self.assertEqual(json.loads(result["item"]["fields"])["kept_followups"], [waiting, pasted])
        self.assertIsNotNone(self.db.execute("SELECT resolved_at FROM note WHERE id = ?", (blocked,)).fetchone()[0])

    def notes(self, kind="status_change"):
        return [row["body"] for row in self.db.execute(
            "SELECT body FROM note WHERE item = ? AND kind = ? ORDER BY id", (self.item, kind))]

    def test_a_waived_delivery_records_the_waiver_in_one_transaction(self):
        """sd:3704: the completion record keeps each waived criterion and its
        decision note, and the `status_change` reason names them."""
        self.remote()
        commit = self.commit(f"Delivers: sd:{self.item}")
        self.git("push", "origin", "main")
        waived = [{"criterion": "p95 under 2 s", "decision": 41}, {"criterion": "runs offline", "decision": 41}]
        result = deliver_work(self.db, self.item, commit, who="operator", waived=waived)
        self.assertEqual(json.loads(result["item"]["fields"])["completion"]["waived"], waived)
        self.assertIn(f"delivered at {commit} on ", self.notes()[-1])
        self.assertTrue(self.notes()[-1].endswith("main; waived: 2 criteria (decision #41)"), self.notes())

    def test_no_waiver_keeps_the_record_and_the_reason_unchanged(self):
        self.remote()
        commit = self.commit(f"Delivers: sd:{self.item}")
        self.git("push", "origin", "main")
        result = deliver_work(self.db, self.item, commit, who="operator", waived=[])
        self.assertNotIn("waived", json.loads(result["item"]["fields"])["completion"])
        self.assertTrue(self.notes()[-1].endswith("/main"), self.notes())
        self.assertIn(f"delivered at {commit} on ", self.notes()[-1])

    def test_a_malformed_waiver_refuses_before_any_write(self):
        before = item_state(self.db, self.item)
        commit = "0" * 40
        malformed: list = [[{"criterion": "x"}], [{"criterion": "", "decision": 1}],
                           [{"criterion": "x", "decision": "1"}], {"criterion": "x", "decision": 1}]
        for waived in malformed:
            with self.subTest(waived=waived), self.assertRaisesRegex(WorkflowError, "waived"):
                deliver_work(self.db, self.item, commit, who="operator", waived=waived)
        self.assertEqual(item_state(self.db, self.item), before)

    def test_a_failure_inside_the_waived_delivery_rolls_back_whole(self):
        self.remote()
        commit = self.commit(f"Delivers: sd:{self.item}")
        self.git("push", "origin", "main")
        before, notes = item_state(self.db, self.item), self.notes()
        with patch("sd_db.progress._transition", side_effect=WorkflowError("stopped inside")), \
                self.assertRaisesRegex(WorkflowError, "stopped inside"):
            deliver_work(self.db, self.item, commit, who="operator",
                         waived=[{"criterion": "p95 under 2 s", "decision": 41}])
        self.assertEqual(item_state(self.db, self.item), before)
        self.assertEqual(self.notes(), notes)

    def test_legacy_delivery_trailer_uses_original_source_slug_after_relink(self):
        artifact = self.repo / "new-name.md"
        artifact.write_text("moved")
        relink_artifact(self.db, self.item, "new-name.md", who="operator")
        commit = self.commit("Delivers: original")
        self.assertEqual(deliver_work(self.db, self.item, commit, who="operator")["item"]["status"], "done")

    def test_slice_cancel_trailer_wrong_item_and_unpushed_delivery_are_refused(self):
        self.remote()
        for trailer in (f"Item: sd:{self.item}", f"Closes: sd:{self.item}", "Delivers: unrelated"):
            commit = self.commit(trailer)
            self.git("push", "origin", "main")
            before = item_state(self.db, self.item)
            with self.subTest(trailer=trailer), self.assertRaises(WorkflowError):
                deliver_work(self.db, self.item, commit, who="operator")
            self.assertEqual(item_state(self.db, self.item), before)
        commit = self.commit(f"Delivers: sd:{self.item}")
        with self.assertRaisesRegex(WorkflowError, "reachable"):
            deliver_work(self.db, self.item, commit, who="operator")

    def test_remote_failure_does_not_trust_stale_tracking_ref(self):
        remote = self.remote()
        commit = self.commit(f"Delivers: sd:{self.item}")
        self.git("push", "origin", "main")
        remote.rename(self.root / "unavailable.git")
        before = item_state(self.db, self.item)
        with self.assertRaisesRegex(WorkflowError, "verify|remote|failed"):
            deliver_work(self.db, self.item, commit, who="operator")
        self.assertEqual(item_state(self.db, self.item), before)

    def test_non_commit_strings_and_stale_requests_are_refused(self):
        before = item_state(self.db, self.item)
        for commit in ("HEAD", "--help", "abcd"):
            with self.subTest(commit=commit), self.assertRaises(WorkflowError):
                deliver_work(self.db, self.item, commit, who="operator")
        with self.assertRaises(StaleItem):
            cancel_work(self.db, self.item, reason="Cancelled", expected_revision="old", who="operator")
        self.assertEqual(item_state(self.db, self.item), before)

    def test_failed_completion_history_rolls_back_status_and_evidence(self):
        before = item_state(self.db, self.item)
        with patch("sd_db.progress._transition", side_effect=RuntimeError("disk error")):
            with self.assertRaises(RuntimeError):
                cancel_work(self.db, self.item, reason="Cancel", who="operator")
        self.assertEqual(item_state(self.db, self.item), before)

    def test_completion_receipt_must_be_bound_to_this_item_and_repository(self):
        cancelled = cancel_work(self.db, self.item, reason="Cancelled", who="operator")
        row = cancelled["item"]
        self.assertEqual(completion_record(row)["outcome"], "cancelled")
        fields = json.loads(row["fields"])
        for key, value in (("item", self.item + 1), ("repo", "/another/repo"), ("at", "yesterday"),
                           ("reason", ""), ("who", ""), ("outcome", "anything")):
            corrupt = json.loads(json.dumps(fields))
            corrupt["completion"][key] = value
            with self.subTest(key=key):
                self.assertIsNone(completion_record({**row, "fields": json.dumps(corrupt)}))
        self.assertIsNone(completion_record({**row, "shipped_at": "2026-09-08T12:00:00+00:00"}))

    def test_edit_during_delivery_verification_cannot_be_overwritten(self):
        from sd_db import progress
        commit = self.commit(f"Delivers: sd:{self.item}")
        verify = progress._delivery_evidence

        def concurrent_edit(row, candidate):
            evidence = verify(row, candidate)
            edit_item(self.db, self.item, {"priority": 1}, who="operator")
            return evidence

        with patch("sd_db.progress._delivery_evidence", side_effect=concurrent_edit):
            with self.assertRaises(StaleItem):
                deliver_work(self.db, self.item, commit, who="operator")
        state = item_state(self.db, self.item)
        self.assertEqual(state["item"]["status"], "planning")
        self.assertEqual(state["item"]["priority"], 1)
        self.assertIsNone(state["item"]["shipped_at"])

    def test_remote_tip_moving_during_verification_requires_a_fresh_retry(self):
        from sd_db import progress
        self.remote()
        commit = self.commit(f"Delivers: sd:{self.item}")
        self.git("push", "origin", "main")
        run = progress._git

        def remote_moves(root, *args, **kwargs):
            if args[:1] == ("fetch",):
                self.commit("Item: another")
                self.git("push", "origin", "main")
            return run(root, *args, **kwargs)

        before = item_state(self.db, self.item)
        with patch("sd_db.progress._git", side_effect=remote_moves):
            with self.assertRaisesRegex(WorkflowError, "changed during"):
                deliver_work(self.db, self.item, commit, who="operator")
        self.assertEqual(item_state(self.db, self.item), before)


class TaskCancellation(ProgressCase):
    """sd:1005: a task or followup could only be closed as done, so a
    finding dropped on purpose read the same as one fixed. `cancel_work`
    takes its guard from the caller; `task_guard` admits task and followup
    rows, and the default stays the work guard."""

    def task(self, kind="task", **columns):
        return create_item(self.db, kind=kind, title="A carried finding", **columns)

    def test_a_task_and_a_followup_cancel_with_a_receipt_and_no_repository(self):
        for kind in ("task", "followup"):
            with self.subTest(kind=kind):
                item = self.task(kind)
                result = cancel_work(self.db, item, reason="Superseded by sd:2", who="operator", guard=task_guard)
                self.assertEqual(result["item"]["status"], "done")
                self.assertIsNone(result["item"]["shipped_at"])
                receipt = completion_record(result["item"])
                self.assertEqual((receipt["outcome"], receipt["reason"], receipt["repo"]),
                                 ("cancelled", "Superseded by sd:2", None))
                self.assertIn("cancelled: Superseded by sd:2", result["notes"][-1]["body"])
                again = cancel_work(self.db, item, reason="Superseded by sd:2", who="operator", guard=task_guard)
                self.assertEqual(again, result)

    def test_a_cancel_answers_the_open_questions_a_delivery_keeps(self):
        # sd:7992: a cancelled item's open question kept it in Decisions.
        from sd_db.writes import add_note
        cancelled, delivered = self.task(), self.task()
        asked = {item: add_note(self.db, item, "question", "Which?\nOption: A (Recommended)\nOption: B")
                 for item in (cancelled, delivered)}
        result = cancel_work(self.db, cancelled, reason="Not needed", who="operator", guard=task_guard)
        change_status(self.db, delivered, "done", who="operator", reason="landed")
        note = next(row for row in result["notes"] if row["id"] == asked[cancelled])
        self.assertIsNotNone(note["resolved_at"])
        self.assertIn(f"resolved open question {asked[cancelled]}", result["notes"][-1]["body"])
        kept = next(row for row in item_state(self.db, delivered)["notes"] if row["id"] == asked[delivered])
        self.assertIsNone(kept["resolved_at"])

    def test_the_default_guard_is_still_the_work_guard(self):
        item = self.task()
        with self.assertRaisesRegex(WorkflowError, "work items"):
            cancel_work(self.db, item, reason="Not needed", who="operator")
        with self.assertRaisesRegex(WorkflowError, "task and followup"):
            cancel_work(self.db, self.item, reason="Not needed", who="operator", guard=task_guard)
        for kind in ("work-idea", "report", "idea"):
            with self.subTest(kind=kind), self.assertRaisesRegex(WorkflowError, "task and followup"):
                cancel_work(self.db, self.task(kind), reason="Not needed", who="operator", guard=task_guard)

    def test_a_task_done_already_or_with_live_work_or_a_recurrence_is_refused(self):
        done = self.task()
        change_status(self.db, done, "done", who="operator")
        with self.assertRaisesRegex(WorkflowError, "completed"):
            cancel_work(self.db, done, reason="Too late", who="operator", guard=task_guard)
        busy = self.task()
        self.db.execute("INSERT INTO assignment (item, role, status) VALUES (?, 'author', 'queued')", (busy,))
        with self.assertRaisesRegex(WorkflowError, "assignment"):
            cancel_work(self.db, busy, reason="Not needed", who="operator", guard=task_guard)
        recurring = self.task(recurrence="FREQ=WEEKLY", due="2026-10-05")
        with self.assertRaisesRegex(WorkflowError, "recurring"):
            cancel_work(self.db, recurring, reason="Not needed", who="operator", guard=task_guard)
        self.assertEqual([item_state(self.db, item)["item"]["status"] for item in (busy, recurring)],
                         ["planning", "planning"])

    def test_a_reopened_cancelled_task_drops_its_receipt(self):
        """A task stays reopenable. Its receipt must not outlive the reopen:
        completed later as plain done, it read as cancelled."""
        item = self.task()
        cancel_work(self.db, item, reason="Not now", who="operator", guard=task_guard)
        reopened = change_status(self.db, item, "planning", who="operator")
        self.assertNotIn("completion", json.loads(reopened["item"]["fields"] or "{}"))
        finished = change_status(self.db, item, "done", who="operator")
        self.assertIsNone(completion_record(finished["item"]))


class AfterTheFactDelivery(ProgressCase):
    """A merge that carried `Item: sd:N` where `Delivers:` was meant (pack sd:1913).

    `deliver_work` refuses it and keeps refusing it; the named path accepts
    it with a reason, after the same reachability check.
    """

    REASON = "prepared without --deliver; the merge is the whole item"

    def test_an_item_merge_on_the_default_branch_delivers_with_a_reason(self):
        self.remote()
        commit = self.commit(f"Item: sd:{self.item}")
        self.git("push", "origin", "main")
        with self.assertRaisesRegex(WorkflowError, "Delivers"):
            deliver_work(self.db, self.item, commit, who="operator")
        result = deliver_associated_work(self.db, self.item, commit, who="operator", reason=self.REASON)
        self.assertEqual(result["item"]["status"], "done")
        self.assertTrue(result["item"]["shipped_at"])
        evidence = completion_record(result["item"])
        self.assertEqual((evidence["outcome"], evidence["commit"], evidence["trailer"]), ("delivered", commit, "Item"))
        self.assertEqual(evidence["after_the_fact"], self.REASON)
        # The sentence every delivery writes, so one query finds this one too.
        self.assertIn(f"delivered at {commit} on {evidence['verified_ref']}", result["notes"][-1]["body"])
        self.assertEqual(deliver_associated_work(self.db, self.item, commit, who="operator", reason=self.REASON), result)

    def test_a_blank_reason_refuses_before_any_check(self):
        commit = self.commit(f"Item: sd:{self.item}")
        before = item_state(self.db, self.item)
        for reason in ("", "  ", None):
            with self.subTest(reason=reason), self.assertRaisesRegex(WorkflowError, "reason"):
                deliver_associated_work(self.db, self.item, commit, who="operator", reason=reason)
        self.assertEqual(item_state(self.db, self.item), before)

    def test_another_trailer_or_an_unpushed_commit_is_refused(self):
        self.remote()
        for trailer in (f"Closes: sd:{self.item}", f"Item: sd:{self.item + 1}", "Item: original", None):
            commit = self.commit(trailer)
            self.git("push", "origin", "main")
            before = item_state(self.db, self.item)
            with self.subTest(trailer=trailer), self.assertRaisesRegex(WorkflowError, "Item"):
                deliver_associated_work(self.db, self.item, commit, who="operator", reason=self.REASON)
            self.assertEqual(item_state(self.db, self.item), before)
        commit = self.commit(f"Item: sd:{self.item}")
        with self.assertRaisesRegex(WorkflowError, "reachable"):
            deliver_associated_work(self.db, self.item, commit, who="operator", reason=self.REASON)

    def test_an_item_line_outside_the_trailer_block_is_refused(self):
        self.remote()
        self.git("commit", "--allow-empty", "-m", f"A slice\n\nItem: sd:{self.item}\n\nAuthored-with: human")
        commit = self.git("rev-parse", "HEAD")
        self.git("push", "origin", "main")
        with self.assertRaisesRegex(WorkflowError, "Item"):
            deliver_associated_work(self.db, self.item, commit, who="operator", reason=self.REASON)

    def test_a_cancelled_row_is_not_reclassified(self):
        self.remote()
        commit = self.commit(f"Item: sd:{self.item}")
        self.git("push", "origin", "main")
        cancel_work(self.db, self.item, reason="No longer needed", who="operator")
        before = item_state(self.db, self.item)
        with self.assertRaisesRegex(WorkflowError, "cancelled or previously completed"):
            deliver_associated_work(self.db, self.item, commit, who="operator", reason=self.REASON)
        self.assertEqual(item_state(self.db, self.item), before)


class TrackerFreshness(ProgressCase):
    def test_freshness_comes_from_successful_sync_not_row_last_seen(self):
        now = datetime(2026, 9, 8, 12, tzinfo=UTC)
        self.assertEqual(tracker_freshness(self.db, now=now)["state"], "never")
        write_watermark(self.db, "github", "2026-09-08T11:00:00+00:00", "2026-09-07T00:00:00+00:00")
        fresh = tracker_freshness(self.db, now=now)
        self.assertEqual((fresh["state"], fresh["age_seconds"]), ("fresh", 3600))
        self.assertEqual(tracker_freshness(self.db, now=now, max_age_seconds=1800)["state"], "stale")

    def test_an_unwindowed_success_counts_without_a_cursor(self):
        """pack sd:5376: a custom `JIRA_JQL` run holds the cursor but still succeeded.

        An older cursor stays the last success until a later unwindowed
        heartbeat; a windowed or failed heartbeat never counts.
        """
        now = datetime(2026, 9, 8, 12, tzinfo=UTC)
        write_watermark(self.db, "jira", "2026-09-05T11:00:00+00:00", "2026-09-04T00:00:00+00:00")
        record_state(self.db, "heartbeat", key="tracker-sync:jira", timestamp="2026-09-08T10:00:00+00:00",
                     body={"ok": False, "reason": "boom", "windowed": False})
        record_state(self.db, "heartbeat", key="tracker-sync:jira", timestamp="2026-09-08T10:30:00+00:00",
                     body={"ok": True, "reason": ""})
        self.assertEqual(tracker_freshness(self.db, "jira", now=now)["last_success_at"],
                         "2026-09-05T11:00:00+00:00")
        record_state(self.db, "heartbeat", key="tracker-sync:jira", timestamp="2026-09-08T11:00:00+00:00",
                     body={"ok": True, "reason": "", "windowed": False})
        state = tracker_freshness(self.db, "jira", now=now)
        self.assertEqual((state["state"], state["last_success_at"], state["age_seconds"]),
                         ("fresh", "2026-09-08T11:00:00+00:00", 3600))
        self.assertEqual(tracker_freshness(self.db, "github", now=now)["state"], "never")

    def test_failed_attempt_does_not_advance_successful_sync_or_appear_fresh(self):
        write_watermark(self.db, "github", "2026-09-08T11:00:00+00:00", "2026-09-07T00:00:00+00:00")
        record_state(self.db, "heartbeat", key="tracker-sync:github", timestamp="2026-09-08T11:30:00+00:00",
                     body={"ok": False, "reason": "Remote unavailable"})
        state = tracker_freshness(self.db, now=datetime(2026, 9, 8, 12, tzinfo=UTC))
        self.assertEqual(state["state"], "degraded")
        self.assertEqual(state["last_success_at"], "2026-09-08T11:00:00+00:00")
        self.assertEqual(state["reason"], "Remote unavailable")

    def test_tracker_projection_never_invents_match_reasons_or_changes_local_items(self):
        before = item_state(self.db, self.item)
        upsert_shadow(self.db, tracker="github", url="https://example.test/issues/1", state="OPEN")
        upsert_shadow(self.db, tracker="github", url="https://example.test/issues/2", state="CLOSED")
        rows = tracker_items(self.db)
        self.assertEqual([row["url"] for row in rows], ["https://example.test/issues/1"])
        self.assertEqual(rows[0]["why"], [])
        self.assertEqual(item_state(self.db, self.item), before)


if __name__ == "__main__":
    unittest.main()
