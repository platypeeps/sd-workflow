"""Contribution lifecycle uses real temporary stores and no external calls."""

import copy
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sd_db import connect, initialise
from sd_db import contributions as c
from sd_db.migrate import migrate
from sd_db.schema import migrations
from sd_db.workflow import StaleItem, WorkflowError, item_state
from sd_db.writes import (
    add_note,
    create_item,
    set_item_fields,
    transition,
    upsert_shadow,
)

URL = "https://github.com/upstream/project/pull/12"
ISSUE = "https://github.com/upstream/project/issues/56"
DEPENDENCY = "https://github.com/upstream/library/pull/34"
T0 = "2026-09-10T00:00:00+00:00"
T1 = "2026-09-10T00:01:00+00:00"
T2 = "2026-09-10T00:02:00+00:00"
T3 = "2026-09-10T00:03:00+00:00"


class ContributionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "sd.db"
        initialise(self.path)
        self.db = connect(self.path)
        self.addCleanup(self.db.close)

    def observation(self, **changes):
        value = {"complete": True, "observed_at": T0, "operator": {"id": "1", "login": "author"},
                 "author": {"id": "1", "login": "author"}, "repo": "upstream/project", "title": "Contribution",
                 "state": "open", "head": "a" * 40, "base": "b" * 40, "draft": False,
                 "mergeable": "mergeable", "ci": "success", "ci_head": "a" * 40, "ci_ids": ["check:1"],
                 "why": ["author"], "blocking_labels": [], "labels": ["blocked"], "reviews": [], "events": []}
        value.update(changes)
        return value

    def observe(self, **changes):
        return c.observe_pull(self.db, URL, self.observation(**changes),
                              expected_revision=c.snapshot(self.db, "github:" + URL)["revision"])

    def event(self, kind="comment", **changes):
        value = {"id": "event:1", "at": T0, "actor_id": "2", "kind": kind,
                 "maintainer": True, "mentions_operator": False, "url": URL + "#issuecomment-1"}
        value.update(changes)
        return value

    def metadata(self):
        clone = self.root / "clone"
        clone.mkdir()
        for argv in (["init", "-q", "-b", "contribution"], ["config", "user.name", "Fixture"],
                     ["config", "user.email", "fixture@example.invalid"], ["commit", "--allow-empty", "-qm", "fixture"]):
            subprocess.run(["git", *argv], cwd=clone, check=True)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=clone, text=True).strip()
        log = self.root / "acceptance.log"
        log.write_text("Ran 4 tests\nOK\n")
        return {"local_clone": str(clone), "local_branch": "contribution", "tested_commit": commit,
                "evidence": [{"argv": ["never-execute-this-stored-command"], "cwd": str(clone), "exit_code": 0,
                              "commit": commit, "artifact": str(log), "sha256": hashlib.sha256(log.read_bytes()).hexdigest()}],
                "blocked_on": "Waiting for the library release", "depends_on": []}

    def draft(self, **changes):
        body = self.root / "draft.md"
        body.write_text("# Bug\n\nSteps to reproduce.\n")
        value = {"target_repo": "upstream/project", "draft_title": "Project loses the second argument",
                 "draft_path": {"path": str(body), "sha256": hashlib.sha256(body.read_bytes()).hexdigest()}}
        value.update(changes)
        return value

    def issue_observation(self, **changes):
        # The shape Slice B's collector produces: no reviews, ci, head, base, mergeable or draft.
        value = {"complete": True, "observed_at": T0, "operator": {"id": "1", "login": "author"},
                 "author": {"id": "1", "login": "author"}, "repo": "upstream/project", "title": "Issue",
                 "state": "open", "state_reason": None, "why": ["author"], "blocking_labels": [],
                 "labels": [], "events": []}
        value.update(changes)
        return value

    def store_issue(self, url=ISSUE, attention=(), **changes):
        # Slice C's observe_issue stores this; until it lands the checkpoint is written the way it will be.
        state = c.snapshot(self.db, "issue:" + url)
        state["observation"] = self.issue_observation(**changes)
        c._publish_attention(self.db, state, list(attention))
        return c._save(self.db, state)


class MetadataTests(ContributionCase):
    def test_unfiled_capture_preserves_real_evidence_and_never_executes_argv(self):
        values = self.metadata()
        result = c.capture(self.db, title="Unfiled contribution", changes=values, who="operator")
        row = result["item"]
        self.assertEqual(row["kind"], "task")
        self.assertEqual(row["status"], "planning")
        self.assertEqual(json.loads(row["fields"])["contribution"], values)
        self.assertEqual(self.db.execute("SELECT count(*) FROM shadow").fetchone()[0], 0)
        projected = c.projection(self.db)[0]
        self.assertEqual(projected["local_branch"], "contribution")
        self.assertTrue(projected["evidence_verified"])

    def test_invalid_proof_does_not_leave_half_a_capture(self):
        values = self.metadata()
        values["evidence"][0]["sha256"] = "0" * 64
        with self.assertRaises(WorkflowError):
            c.capture(self.db, title="Bad proof", changes=values, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)

    def test_edit_preserves_unrelated_fields_and_refuses_stale_revision(self):
        state = c.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")
        item = state["item"]["id"]
        fields = json.loads(state["item"]["fields"])
        fields["unrelated"] = {"keep": True}
        set_item_fields(self.db, item, fields=fields)
        before = item_state(self.db, item)
        after = c.configure(self.db, item, {"blocked_on": "maintainer"}, expected_revision=before["revision"], who="operator")
        self.assertEqual(json.loads(after["item"]["fields"])["unrelated"], {"keep": True})
        with self.assertRaises(WorkflowError):
            c.configure(self.db, item, {"blocked_on": "overwrite"}, expected_revision=before["revision"], who="operator")
        self.assertEqual(item_state(self.db, item), after)

    def test_task_dependencies_reject_cycles(self):
        first = c.capture(self.db, title="first", changes={"pull_url": URL}, who="operator")["item"]["id"]
        second = c.capture(self.db, title="second", changes={"pull_url": URL.replace("12", "13")}, who="operator")["item"]["id"]
        c.configure(self.db, first, {"depends_on": [{"kind": "item", "item": second}]}, who="operator")
        with self.assertRaisesRegex(WorkflowError, "cycle"):
            c.configure(self.db, second, {"depends_on": [{"kind": "item", "item": first}]}, who="operator")

    def test_missing_or_failed_test_evidence_is_trackable_but_never_verified(self):
        values = self.metadata()
        values.pop("tested_commit")
        values.pop("blocked_on")
        values["evidence"] = []
        captured = c.capture(self.db, title="Unverified", changes=values, who="operator")
        self.assertFalse(c.projection(self.db)[0]["evidence_verified"])
        self.assertEqual(c.projection(self.db)[0]["lane"], "awaiting_you")
        values["tested_commit"] = subprocess.check_output(["git", "-C", values["local_clone"], "rev-parse", "HEAD"], text=True).strip()
        values["evidence"] = [{"artifact": str(self.root / "acceptance.log"),
                               "sha256": hashlib.sha256((self.root / "acceptance.log").read_bytes()).hexdigest(),
                               "exit_code": None}]
        c.configure(self.db, captured["item"]["id"], values, who="operator")
        self.assertFalse(c.projection(self.db)[0]["evidence_verified"])

    def test_duplicate_registered_url_refuses_without_creating_an_item(self):
        c.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")
        with self.assertRaises(WorkflowError):
            c.capture(self.db, title="Duplicate", changes={"pull_url": URL}, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 1)

    def test_github_case_aliases_share_one_identity(self):
        c.capture(self.db, title="Filed", changes={"pull_url": URL.replace("upstream/project", "Upstream/Project")}, who="operator")
        with self.assertRaises(WorkflowError):
            c.capture(self.db, title="Duplicate", changes={"pull_url": URL}, who="operator")
        self.assertEqual(c.snapshot(self.db, "github:" + URL)["revision"],
                         c.snapshot(self.db, "github:" + URL.replace("upstream/project", "Upstream/Project"))["revision"])

    def test_duplicate_local_branch_and_path_alias_refuse_even_when_filed(self):
        metadata = self.metadata()
        c.capture(self.db, title="Local", changes=metadata, who="operator")
        for changes in (metadata, {**metadata, "pull_url": URL},
                        {**metadata, "local_clone": str(Path(metadata["local_clone"]) / ".." / "clone")}):
            with self.assertRaisesRegex(WorkflowError, "clone and branch"):
                c.capture(self.db, title="Duplicate", changes=changes, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 1)


class ActivityTests(ContributionCase):
    def test_changes_requested_is_actionable_without_registering_a_task(self):
        result = self.observe(reviews=[{"id": "review:1", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}])
        self.assertTrue(result["attention"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)
        self.assertTrue(c.projection(self.db)[0]["needs_you"])

    def test_approval_supersedes_the_same_reviewers_old_changes_request(self):
        reviews = [{"id": "review:1", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}]
        self.observe(reviews=reviews)
        reviews.append({"id": "review:2", "actor_id": "2", "state": "APPROVED", "at": T1})
        result = self.observe(observed_at=T1, reviews=reviews)
        self.assertEqual(result["attention"], [])

    def test_external_comment_mention_label_draft_and_close_trigger(self):
        cases = [(self.event(), {}), (self.event(maintainer=False, mentions_operator=True), {}),
                 (self.event("label_added", label="blocked"), {"blocking_labels": ["blocked"]}),
                 (self.event("converted_to_draft"), {"draft": True}),
                 (self.event("closed"), {"state": "closed"})]
        for index, (event, changes) in enumerate(cases):
            url = URL + str(index)
            result = c.observe_pull(self.db, url, self.observation(events=[event], **changes),
                                    expected_revision=c.snapshot(self.db, "github:" + url)["revision"])
            self.assertTrue(result["attention"], event)

    def test_own_activity_and_unrelated_bot_labels_never_trigger(self):
        events = [self.event(actor_id="1"), self.event("label_added", actor_id="1", label="blocked"),
                  self.event("converted_to_draft", actor_id="1"), self.event("label_added", label="unrelated", actor_type="Bot")]
        result = self.observe(events=events, draft=True)
        self.assertEqual(result["attention"], [])

    def test_green_push_pending_then_new_head_failure_notifies_once(self):
        self.observe()
        pending = self.observe(observed_at=T1, head="c" * 40, ci="pending", ci_head="c" * 40, ci_ids=["check:2"])
        self.assertEqual(pending["attention"], [])
        failed = self.observe(observed_at=T2, head="c" * 40, ci="failure", ci_head="c" * 40, ci_ids=["check:2"])
        self.assertEqual(len(failed["attention"]), 1)
        repeated = self.observe(observed_at=T3, head="c" * 40, ci="failure", ci_head="c" * 40, ci_ids=["check:2"])
        self.assertEqual(failed["attention"], repeated["attention"])
        self.assertEqual(len(repeated["notifications"]), 1)

    def test_stale_head_checks_and_unknown_mergeability_never_invent_activity(self):
        before = self.observe()
        with self.assertRaises(WorkflowError):
            self.observe(observed_at=T1, head="c" * 40, ci="failure", ci_head="a" * 40)
        self.assertEqual(c.snapshot(self.db, "github:" + URL)["revision"], before["revision"])
        self.assertEqual(self.observe(observed_at=T1, mergeable="unknown")["attention"], [])

    def test_confirmed_base_conflict_is_actionable(self):
        self.observe()
        result = self.observe(observed_at=T1, base="d" * 40, mergeable="conflicting")
        self.assertEqual(len(result["attention"]), 1)

    def test_incomplete_and_stale_observations_preserve_attention_and_notifications(self):
        before = self.observe(observed_at=T1, events=[self.event()])
        result = c.observe_pull(self.db, URL, {"complete": False, "reason": "gh timed out", "observed_at": T2},
                                expected_revision=before["revision"])
        for key in ("revision", "observation", "attention", "notifications"):
            self.assertEqual(result[key], before[key])
        with self.assertRaises(WorkflowError):
            c.observe_pull(self.db, URL, self.observation(observed_at=T2), expected_revision="stale")
        with self.assertRaises(WorkflowError):
            self.observe(observed_at=T0)

    def test_label_removal_ready_and_reopen_clear_condition_attention(self):
        events = [self.event("label_added", id="label", label="blocked"),
                  self.event("converted_to_draft", id="draft"), self.event("closed", id="closed")]
        before = self.observe(events=events, draft=True, state="closed", blocking_labels=["blocked"])
        self.assertEqual(len(before["attention"]), 3)
        after = self.observe(observed_at=T1, events=events, labels=[], draft=False, state="open")
        self.assertEqual(after["attention"], [])
        self.assertEqual(after["notifications"], before["notifications"])

    def test_missing_identity_holds_and_does_not_reannounce_previous_event(self):
        before = self.observe(events=[self.event()])
        after = self.observe(observed_at=T1, operator={"id": None})
        self.assertEqual(after, before)
        self.assertEqual(c.projection(self.db)[0]["freshness"]["status"], "unknown")
        again = self.observe(observed_at=T2, events=[self.event()])
        self.assertEqual(again["notifications"], before["notifications"])

    def test_superseded_review_resolves_registered_followup_without_erasing_notice(self):
        c.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")
        before = self.observe(reviews=[{"id": "one", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}])
        note = next(iter(before["notifications"].values()))["note_id"]
        after = self.observe(observed_at=T1, reviews=[{"id": "two", "actor_id": "2", "state": "APPROVED", "at": T1}])
        self.assertFalse(after["attention"])
        self.assertIsNotNone(self.db.execute("SELECT resolved_at FROM note WHERE id=?", (note,)).fetchone()[0])
        self.assertEqual(after["notifications"], before["notifications"])

    def test_foreign_authors_activity_is_not_the_operators_attention(self):
        result = self.observe(author={"id": "42"}, why=["watching"], events=[self.event()],
                              reviews=[{"id": "review", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}])
        self.assertEqual(result["attention"], [])
        self.assertFalse(c.projection(self.db)[0]["needs_you"])
        assigned = self.observe(observed_at=T1, author={"id": "42"}, why=["review-requested"])
        self.assertEqual(assigned["attention"][0]["id"], "context:review-requested")

    def test_latest_own_lifecycle_action_cannot_reactivate_historical_external_action(self):
        events = []
        for index, (kind, reset) in enumerate((("label_added", "label_removed"), ("converted_to_draft", "ready_for_review"), ("closed", "reopened"))):
            events += [self.event(kind, id=f"old{index}", label="blocked"),
                       self.event(reset, id=f"reset{index}", label="blocked", at=T1),
                       self.event(kind, id=f"own{index}", label="blocked", actor_id="1", at=T2)]
        result = self.observe(observed_at=T3, events=events, blocking_labels=["blocked"], draft=True, state="closed")
        self.assertEqual(result["attention"], [])

    def test_blocking_policy_change_refuses_both_old_revision_and_old_policy(self):
        item = c.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")["item"]["id"]
        before = c.snapshot(self.db, "github:" + URL)
        c.configure(self.db, item, {"blocking_labels": ["blocked"]}, who="operator")
        with self.assertRaises(StaleItem):
            c.observe_pull(self.db, URL, self.observation(), expected_revision=before["revision"])
        with self.assertRaises(StaleItem):
            self.observe()
        result = self.observe(blocking_labels=["blocked"], events=[self.event("label_added", label="blocked")])
        self.assertEqual(len(result["attention"]), 1)

    def test_equal_timestamp_opinions_and_events_follow_source_order_not_lexical_ids(self):
        result = self.observe(reviews=[
            {"id": "review:9", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0},
            {"id": "review:10", "actor_id": "2", "state": "APPROVED", "at": T0}],
            events=[self.event("closed", id="event:9"), self.event("closed", id="event:10", actor_id="1")], state="closed")
        self.assertEqual(result["attention"], [])
        opened = self.observe(observed_at=T1, reviews=[
            {"id": "review:9", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0},
            {"id": "review:10", "actor_id": "2", "state": "APPROVED", "at": T0}])
        self.assertEqual(opened["attention"], [])

    def test_persistent_conflict_and_head_change_do_not_reannounce_after_ack(self):
        before = self.observe(mergeable="conflicting")
        key = "github:" + URL
        after = c.acknowledge(self.db, key, [before["attention"][0]["id"]], expected_revision=before["revision"], who="operator")
        unknown = self.observe(observed_at=T1, mergeable="unknown")
        repeated = self.observe(observed_at=T2, mergeable="conflicting", head="c" * 40, ci_head="c" * 40)
        self.assertEqual(unknown["attention"], [])
        self.assertEqual(repeated["attention"], [])
        self.assertEqual(repeated["notifications"], after["notifications"])


class DependencyTests(ContributionCase):
    def test_merge_and_published_release_with_package_are_separate_conditions(self):
        merge = {"kind": "merge", "url": DEPENDENCY}
        release = {"kind": "release", "repo": "upstream/library", "tag": "v1.2.3", "contains_pull": DEPENDENCY,
                   "package": {"name": "library", "version": "1.2.3"}}
        task = c.capture(self.db, title="Blocked", changes={"pull_url": URL, "depends_on": [merge, release]}, who="operator")
        item = task["item"]["id"]
        key = f"item:{item}"
        merged = {"dependency": merge, "state": "satisfied", "proof": {"url": DEPENDENCY, "merged": True,
                                                                                 "merge_commit_sha": "d" * 40}}
        waiting = {"dependency": release, "state": "pending", "proof": {}}
        first = c.observe_dependencies(self.db, item, [merged, waiting], observed_at=T0,
                                        expected_revision=c.snapshot(self.db, key)["revision"])
        self.assertEqual(first["attention"], [])
        ready = copy.deepcopy(waiting)
        ready.update(state="satisfied", proof={"repo": "upstream/library", "tag": "v1.2.3", "published": True, "merged": True,
            "tag_commit": "e" * 40, "merge_commit_sha": "d" * 40, "contains_pull": DEPENDENCY,
            "ancestor": True, "package": {"name": "library", "version": "1.2.3", "files": [{"filename": "library.whl", "sha256": "1" * 64, "yanked": False}]}})
        second = c.observe_dependencies(self.db, item, [merged, ready], observed_at=T1, expected_revision=first["revision"])
        self.assertEqual(len(second["attention"]), 1)
        self.assertEqual(c.projection(self.db)[0]["lane"], "newly_unblocked")
        self.assertEqual(item_state(self.db, item)["item"]["status"], "planning")
        self.assertEqual(self.db.execute("SELECT count(*) FROM note WHERE kind='followup'").fetchone()[0], 1)
        repeated = c.observe_dependencies(self.db, item, [merged, ready], observed_at=T2, expected_revision=second["revision"])
        self.assertEqual(second["notifications"], repeated["notifications"])

    def test_unknown_dependency_does_not_clear_a_known_state(self):
        task = c.capture(self.db, title="Blocked", changes={"pull_url": URL, "depends_on": [{"kind": "merge", "url": DEPENDENCY}]}, who="operator")
        item = task["item"]["id"]
        key = f"item:{item}"
        before = c.snapshot(self.db, key)
        result = c.observe_dependencies(self.db, item, [{"dependency": {"kind": "merge", "url": DEPENDENCY}, "state": "unknown"}],
                                        observed_at=T0, expected_revision=before["revision"])
        self.assertEqual(result["revision"], before["revision"])

    def test_item_dependency_uses_actual_status_and_ignores_supplied_success(self):
        prerequisite = c.capture(self.db, title="First", changes={"pull_url": DEPENDENCY}, who="operator")["item"]["id"]
        dependency = {"kind": "item", "item": prerequisite}
        item = c.capture(self.db, title="Second", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        key = f"item:{item}"
        first = c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "satisfied"}], observed_at=T0,
                                       expected_revision=c.snapshot(self.db, key)["revision"])
        self.assertFalse(first["observation"]["satisfied"])
        transition(self.db, prerequisite, "done", who="fixture")
        second = c.observe_dependencies(self.db, item, [], observed_at=T1, expected_revision=first["revision"])
        self.assertTrue(second["observation"]["satisfied"])
        self.assertEqual(c.projection(self.db)[0]["lane"], "newly_unblocked")

    def test_unproven_release_and_wrong_package_cannot_unblock(self):
        dependency = {"kind": "release", "repo": "upstream/library", "tag": "v1", "contains_pull": DEPENDENCY,
                      "package": {"name": "library", "version": "1"}}
        item = c.capture(self.db, title="Waiting", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        key = f"item:{item}"
        before = c.snapshot(self.db, key)
        proof = {"repo": "upstream/library", "tag": "v1", "contains_pull": DEPENDENCY, "published": True,
                 "ancestor": True, "merged": True, "merge_commit_sha": "d" * 40, "tag_commit": "e" * 40,
                 "package": {"name": "library", "version": "1", "files": [{"filename": "library.whl", "sha256": "f" * 64, "yanked": False}]}}
        for change in ({"ancestor": False}, {"merged": False}, {"tag": "v2"},
                       {"package": {"name": "library", "version": "2", "files": proof["package"]["files"]}}):
            result = c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "satisfied", "proof": {**proof, **change}}],
                                            observed_at=T0, expected_revision=before["revision"])
            self.assertEqual(result["revision"], before["revision"])

    def test_configuration_change_invalidates_inflight_dependency_proof(self):
        dependency = {"kind": "merge", "url": DEPENDENCY}
        item = c.capture(self.db, title="Waiting", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        before = c.snapshot(self.db, f"item:{item}")
        c.configure(self.db, item, {"depends_on": []}, who="operator")
        with self.assertRaises(StaleItem):
            c.observe_dependencies(self.db, item, [], observed_at=T0, expected_revision=before["revision"])

    def test_dependency_reconfiguration_resolves_the_old_unblock_followup(self):
        dependency = {"kind": "merge", "url": DEPENDENCY}
        item = c.capture(self.db, title="Waiting", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        key = f"item:{item}"
        ready = c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "satisfied",
                    "proof": {"url": DEPENDENCY, "merged": True, "merge_commit_sha": "a" * 40}}],
                    observed_at=T0, expected_revision=c.snapshot(self.db, key)["revision"])
        note = next(iter(ready["notifications"].values()))["note_id"]
        c.configure(self.db, item, {"depends_on": [{"kind": "merge", "url": DEPENDENCY.replace("34", "35")}]}, who="operator")
        self.assertFalse(c.snapshot(self.db, key)["attention"])
        self.assertIsNotNone(self.db.execute("SELECT resolved_at FROM note WHERE id=?", (note,)).fetchone()[0])

    def test_package_filename_must_be_a_nonempty_string(self):
        dependency = {"kind": "release", "repo": "upstream/library", "tag": "v1", "contains_pull": DEPENDENCY,
                      "package": {"name": "library", "version": "1"}}
        item = c.capture(self.db, title="Waiting", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        before = c.snapshot(self.db, f"item:{item}")
        proof = {"repo": "upstream/library", "tag": "v1", "contains_pull": DEPENDENCY, "published": True,
                 "merged": True, "ancestor": True, "tag_commit": "e" * 40, "merge_commit_sha": "d" * 40,
                 "package": {"name": "library", "version": "1", "files": [{"filename": True, "sha256": "f" * 64, "yanked": False}]}}
        result = c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "satisfied", "proof": proof}],
                                        observed_at=T0, expected_revision=before["revision"])
        self.assertEqual(result["revision"], before["revision"])


class NotificationTests(ContributionCase):
    def test_returning_context_has_a_new_episode_after_sent_or_acknowledged(self):
        for index, (why, author, acknowledged) in enumerate(
                (why, author, acknowledged) for why in ("assigned", "review-requested")
                for author in ("1", "2") for acknowledged in (False, True)):
            with self.subTest(why=why, author=author, acknowledged=acknowledged):
                url = URL + str(index)
                item = c.capture(self.db, title="Filed", changes={"pull_url": url}, who="operator")["item"]["id"]
                key = "github:" + url

                def observe(*, url=url, author=author, key=key, **changes):
                    return c.observe_pull(self.db, url, self.observation(
                        author={"id": author, "login": "author"}, **changes),
                        expected_revision=c.snapshot(self.db, key)["revision"])

                first = observe(why=[why])
                old_id = first["attention"][0]["id"]
                old_note = first["notifications"][old_id]["note_id"]
                claim = c.claim_notification(self.db, key, old_id, owner="first")
                sent = c.finish_notification(self.db, key, old_id, claim["token"], outcome="sent")
                if acknowledged:
                    sent = c.acknowledge(self.db, key, [old_id], expected_revision=sent["revision"], who="operator")
                repeated = observe(why=[why])
                self.assertEqual(repeated["notifications"], sent["notifications"])
                self.assertIsNone(c.claim_notification(self.db, key, old_id, owner="duplicate"))
                self.assertEqual(observe(why=[], observed_at=T1)["attention"], [])
                returned = observe(why=[why], observed_at=T2)
                self.assertEqual(len(returned["attention"]), 1)
                new_id = returned["attention"][0]["id"]
                self.assertNotEqual(new_id, old_id)
                self.assertEqual(returned["notifications"][old_id], sent["notifications"][old_id])
                new_note = returned["notifications"][new_id]["note_id"]
                self.assertNotEqual(new_note, old_note)
                notes = dict(self.db.execute("SELECT id,resolved_at FROM note WHERE item=? AND kind='followup'", (item,)))
                self.assertIsNotNone(notes[old_note])
                self.assertIsNone(notes[new_note])
                claim = c.claim_notification(self.db, key, new_id, owner="second")
                self.assertIsNotNone(claim)
                c.finish_notification(self.db, key, new_id, claim["token"], outcome="sent")
                unchanged = observe(why=[why], observed_at=T3)
                self.assertEqual(unchanged["attention"], returned["attention"])
                self.assertEqual(len(unchanged["notifications"]), 2)
                self.assertIsNone(c.claim_notification(self.db, key, new_id, owner="duplicate"))

    def test_legacy_acknowledged_context_keeps_identity_until_confirmed_absence(self):
        from sd_db.writes import record_state
        key = "github:" + URL
        first = self.observe(why=["review-requested"])
        event = first["attention"][0]["id"]
        acknowledged = c.acknowledge(self.db, key, [event], expected_revision=first["revision"], who="operator")
        legacy = {k: v for k, v in acknowledged.items() if k not in {"key", "revision", "context_events"}}
        record_state(self.db, "checkpoint", key="contribution:" + key, body=legacy)
        self.assertEqual(self.observe(why=["review-requested"])["attention"], [])
        self.observe(why=[], observed_at=T1)
        returned = self.observe(why=["review-requested"], observed_at=T2)
        self.assertEqual(len(returned["attention"]), 1)
        self.assertNotEqual(returned["attention"][0]["id"], event)
        self.assertEqual(returned["notifications"][event], acknowledged["notifications"][event])

    def test_context_episodes_are_independent(self):
        first = self.observe(why=["assigned", "review-requested"])
        old = {event["reason"]: event["id"] for event in first["attention"]}
        self.observe(why=["assigned"], observed_at=T1)
        returned = self.observe(why=["assigned", "review-requested"], observed_at=T2)
        current = {event["reason"]: event["id"] for event in returned["attention"]}
        self.assertEqual(current["Assigned"], old["Assigned"])
        self.assertNotEqual(current["Review requested"], old["Review requested"])

    def test_incomplete_context_observation_does_not_restart_acknowledged_episode(self):
        first = self.observe(why=["review-requested"])
        key = "github:" + URL
        event = first["attention"][0]["id"]
        acknowledged = c.acknowledge(self.db, key, [event], expected_revision=first["revision"], who="operator")
        held = c.observe_pull(self.db, URL, {"complete": False, "why": []},
                              expected_revision=acknowledged["revision"])
        self.assertEqual(held["revision"], acknowledged["revision"])
        repeated = self.observe(why=["review-requested"], observed_at=T1)
        self.assertEqual(repeated["attention"], [])
        self.assertEqual(repeated["notifications"], acknowledged["notifications"])

    def test_repeated_collect_and_concurrent_claims_send_at_most_once(self):
        before = self.observe(events=[self.event()])
        event = before["attention"][0]["id"]
        key = "github:" + URL
        claim = c.claim_notification(self.db, key, event, owner="worker-one")
        second = connect(self.path)
        try:
            self.assertIsNone(c.claim_notification(second, key, event, owner="worker-two"))
        finally:
            second.close()
        c.finish_notification(self.db, key, event, claim["token"], outcome="sent")
        self.observe(observed_at=T1, events=[self.event()])
        self.assertIsNone(c.claim_notification(self.db, key, event, owner="worker-two"))

    def test_ambiguous_delivery_is_held_and_ack_is_separate(self):
        current = self.observe(events=[self.event()])
        event = current["attention"][0]["id"]
        key = "github:" + URL
        claim = c.claim_notification(self.db, key, event, owner="worker")
        current = c.finish_notification(self.db, key, event, claim["token"], outcome="uncertain")
        self.assertIsNone(c.claim_notification(self.db, key, event, owner="retry"))
        self.assertTrue(current["attention"])
        acknowledged = c.acknowledge(self.db, key, [event], expected_revision=current["revision"], who="operator")
        self.assertEqual(acknowledged["attention"], [])
        repeated = self.observe(observed_at=T1, events=[self.event()])
        self.assertEqual(repeated["attention"], [])
        self.assertEqual(repeated["notifications"][event]["status"], "uncertain")

    def test_registered_pr_is_one_row_and_closed_unmerged_remains_visible(self):
        task = c.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")
        upsert_shadow(self.db, tracker="github", url=URL, repo="upstream/project", kind="pull", state="closed")
        self.observe(state="closed", events=[self.event("closed")])
        rows = c.projection(self.db, repo="upstream/project")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["item_id"], task["item"]["id"])
        self.assertEqual(rows[0]["lane"], "awaiting_you")
        self.assertTrue(rows[0]["needs_you"])

    def test_unstarted_notification_can_retry_but_wrong_token_cannot_finish(self):
        state = self.observe(events=[self.event()])
        event = state["attention"][0]["id"]
        key = "github:" + URL
        claim = c.claim_notification(self.db, key, event, owner="first")
        with self.assertRaises(WorkflowError):
            c.finish_notification(self.db, key, event, "wrong", outcome="sent")
        c.finish_notification(self.db, key, event, claim["token"], outcome="not_started")
        self.assertIsNotNone(c.claim_notification(self.db, key, event, owner="second"))

    def test_ack_revision_refuses_stale_and_resolves_only_rendered_source(self):
        first = self.observe(events=[self.event()])
        current = self.observe(observed_at=T1, events=[self.event(), self.event(id="two")])
        with self.assertRaises(StaleItem):
            c.acknowledge(self.db, "github:" + URL, [first["attention"][0]["id"]], expected_revision=first["revision"], who="operator")
        result = c.acknowledge(self.db, "github:" + URL, [first["attention"][0]["id"]], expected_revision=current["revision"], who="operator")
        self.assertEqual(len(result["attention"]), 1)


class ProjectionTests(ContributionCase):
    def test_projection_reads_in_a_fixed_number_of_statements(self):
        # Each row read its own checkpoint and heartbeat, so a satellite paid
        # one round trip per source: ~9,300 per sd-status (sd:2678).
        added = []

        def add(n):
            for _ in range(n):
                for registered in (True, False):
                    url = URL.replace("12", str(100 + len(added)))
                    added.append(url)
                    if registered:
                        c.capture(self.db, title="Filed", changes={"pull_url": url}, who="operator")
                    c.observe_pull(self.db, url, self.observation(),
                                   expected_revision=c.snapshot(self.db, "github:" + url)["revision"])

        def counted():
            statements = []
            self.db.set_trace_callback(statements.append)
            try:
                rows = c.projection(self.db)
            finally:
                self.db.set_trace_callback(None)
            return len(rows), len(statements)

        add(1)
        rows, few = counted()
        add(5)
        self.assertEqual(counted(), (rows + 10, few))
        self.assertLessEqual(few, 10)

    def test_projection_repo_identities_keep_local_work_in_shared_order(self):
        metadata = self.metadata()
        metadata.pop("blocked_on")
        c.capture(self.db, title="Local", changes=metadata, who="operator")
        self.observe(state="merged")
        result = c.projection(self.db, repo=[metadata["local_clone"], "upstream/project"])
        self.assertEqual([r["lane"] for r in result], ["awaiting_you", "merged"])

    def test_unfiled_explicit_dependency_stays_awaiting_them_until_proven_unblock(self):
        metadata = self.metadata()
        dependency = {"kind": "merge", "url": DEPENDENCY}
        metadata["depends_on"] = [dependency]
        item = c.capture(self.db, title="Local", changes=metadata, who="operator")["item"]["id"]
        self.assertEqual(c.projection(self.db)[0]["lane"], "awaiting_them")
        key = f"item:{item}"
        c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "satisfied",
                    "proof": {"url": DEPENDENCY, "merged": True, "merge_commit_sha": "a" * 40}}],
                    observed_at=T0, expected_revision=c.snapshot(self.db, key)["revision"])
        self.assertEqual(c.projection(self.db)[0]["lane"], "newly_unblocked")

    def test_closed_unmerged_pull_takes_the_closed_lane(self):
        # The operator closed their own pull request: no attention, nobody left to wait on.
        self.observe(state="closed", events=[self.event("closed", actor_id="1")])
        [observed] = c.projection(self.db)
        self.assertIsNone(observed["item_id"])
        self.assertEqual((observed["lane"], observed["external_state"], observed["needs_you"]), ("closed", "closed", False))
        # A registered pull request a maintainer closed asks for the operator first, then closes.
        filed = URL.replace("12", "13")
        item = c.capture(self.db, title="Filed pull", changes={"pull_url": filed}, who="operator")["item"]["id"]
        key = "github:" + filed
        closed = c.observe_pull(self.db, filed, self.observation(state="closed", events=[self.event("closed")]),
                                expected_revision=c.snapshot(self.db, key)["revision"])
        by_url = {row["url"]: row for row in c.projection(self.db)}
        self.assertEqual((by_url[filed]["item_id"], by_url[filed]["lane"]), (item, "awaiting_you"))
        c.acknowledge(self.db, key, [e["id"] for e in closed["attention"]], who="operator", expected_revision=closed["revision"])
        by_url = {row["url"]: row for row in c.projection(self.db)}
        self.assertEqual({url: row["lane"] for url, row in by_url.items()}, {URL: "closed", filed: "closed"})
        # A reopen is live work again.
        self.observe(observed_at=T1, state="open", events=[self.event("reopened", actor_id="1", at=T1)])
        self.assertEqual({row["url"]: row["lane"] for row in c.projection(self.db)}, {URL: "awaiting_them", filed: "closed"})

    def test_each_row_carries_when_github_settled_it(self):
        """sd:5303. The shadow collector's merge and close dates reach the row, matched in any url spelling;
        a row the collector holds no date for, or never saw, reads None."""
        self.observe(state="merged")
        self.store_issue(state="closed")
        c.capture(self.db, title="Local", changes={"pull_url": URL.replace("12", "14")}, who="operator")
        upsert_shadow(self.db, tracker="github", url="https://github.com/Upstream/Project/pull/12/", kind="pull",
                      state="merged", merged_at="2026-09-10T00:01:00Z", closed_at="2026-09-10T00:01:00Z")
        upsert_shadow(self.db, tracker="github", url=ISSUE, kind="issue", state="closed",
                      closed_at="2026-09-10T00:02:00Z")
        upsert_shadow(self.db, tracker="jira", url=URL.replace("12", "14"), state="closed",
                      closed_at="2026-09-10T00:03:00Z")
        by_url = {row["url"]: (row["merged_at"], row["closed_at"]) for row in c.projection(self.db)}
        self.assertEqual(by_url, {URL: ("2026-09-10T00:01:00Z", "2026-09-10T00:01:00Z"),
                                  ISSUE: (None, "2026-09-10T00:02:00Z"), URL.replace("12", "14"): (None, None)})

    def test_shadow_facts_read_only_the_projected_urls_in_one_statement(self):
        """sd:5339. Thousands of github shadow rows the projection does not show stay unread, and thousands
        of projected urls stay under SQLite's expression depth (1000)."""
        other = [f"https://github.com/Other/Lib/pull/{n}" for n in range(1, 3001)]
        wanted = [f"https://github.com/upstream/project/pull/{n}" for n in range(1, 3001)]
        stored = [url.replace("upstream/project", "Upstream/Project") + "/" for url in wanted]
        self.db.executemany("INSERT INTO shadow (tracker, url, kind, state, merged_at, first_seen, last_seen) "
                            "VALUES ('github', ?, 'pull', 'merged', ?, ?, ?)",
                            [(url, T1, T0, T0) for url in other + stored])
        statements: list[str] = []
        self.db.set_trace_callback(statements.append)
        try:
            facts = c._shadow_facts(self.db, wanted + [None])
        finally:
            self.db.set_trace_callback(None)
        self.assertEqual(set(facts), set(wanted))
        self.assertEqual(facts[wanted[0]]["merged_at"], T1)
        self.assertEqual(len(statements), 1)
        self.assertEqual(c._shadow_facts(self.db, [None]), {})

    def test_projection_reads_no_shadow_row_it_does_not_show(self):
        """sd:5339. The projection canonicalises only the shadow rows of its own urls, not lifetime history."""
        self.observe(state="merged")
        self.db.executemany("INSERT INTO shadow (tracker, url, kind, state, first_seen, last_seen) "
                            "VALUES ('github', ?, 'pull', 'merged', ?, ?)",
                            [(f"https://github.com/Other/Lib/pull/{n}", T0, T0) for n in range(1, 3001)])
        upsert_shadow(self.db, tracker="github", url=URL, kind="pull", state="merged", merged_at=T1)
        with mock.patch.object(c, "_url", wraps=c._url) as canonical:
            [row] = c.projection(self.db)
        self.assertEqual(row["merged_at"], T1)
        self.assertEqual([call for call in canonical.call_args_list if "Other/Lib" in str(call)], [])

    def test_each_row_carries_its_branch_its_closing_issues_and_its_jira_links(self):
        """sd:5315. One statement for the Jira keys of every row: a known key links, an unknown key does not."""
        self.observe(title="Retry on LOG-12")
        c.capture(self.db, title="Local", changes={"pull_url": URL.replace("12", "14")}, who="operator")
        upsert_shadow(self.db, tracker="github", url=URL, kind="pull", state="open", title="Retry on LOG-12",
                      branch="feature/OPS-7-retry", linked_issues=[ISSUE])
        upsert_shadow(self.db, tracker="github", url=URL.replace("12", "14"), kind="pull", state="open",
                      title="Local", branch="ABC-9", linked_issues=[])
        for key in ("LOG-12", "OPS-7"):
            upsert_shadow(self.db, tracker="jira", url=f"https://jira.example.test/browse/{key}", state="open",
                          title=f"ticket {key}")
        rows = {row["url"]: row for row in c.projection(self.db)}
        self.assertEqual(rows[URL]["branch"], "feature/OPS-7-retry")
        self.assertEqual(rows[URL]["linked"], [
            {"url": ISSUE, "kind": "github", "key": "upstream/project#56"},
            {"url": "https://jira.example.test/browse/LOG-12", "kind": "jira", "key": "LOG-12"},
            {"url": "https://jira.example.test/browse/OPS-7", "kind": "jira", "key": "OPS-7"}])
        local = rows[URL.replace("12", "14")]
        self.assertEqual((local["branch"], local["linked"]), ("ABC-9", []))

    def test_an_automation_pull_with_no_item_is_left_out(self):
        """sd:8379. A job opened it and a job merges it, so no person waits on it; an item's pull stays."""
        self.observe()
        tracked = URL.replace("12", "14")
        c.capture(self.db, title="Tracked", changes={"pull_url": tracked}, who="operator")
        for url in (URL, tracked):
            upsert_shadow(self.db, tracker="github", url=url, kind="pull", state="open",
                          branch="automation/vault-cleanup/f8e3")
        self.assertEqual([row["url"] for row in c.projection(self.db)], [tracked])

    def test_a_store_before_38_reads_no_settle_date(self):
        """Older state: the new library over a store not yet migrated reads its rows, each with no date."""
        old = self.root / "old.db"
        with mock.patch("sd_db.migrate.migrations", return_value=[m for m in migrations() if m[0] < 38]):
            migrate(old)
        raw = sqlite3.connect(old, isolation_level=None)
        raw.row_factory = sqlite3.Row
        self.addCleanup(raw.close)
        c.observe_pull(raw, URL, self.observation(state="merged"), expected_revision=c.snapshot(raw, "github:" + URL)["revision"])
        db = connect(old, write=False)
        self.addCleanup(db.close)
        [row] = c.projection(db)
        self.assertEqual((row["lane"], row["merged_at"], row["closed_at"]), ("merged", None, None))
        self.assertEqual((row["branch"], row["linked"]), (None, []))

    def test_tracker_context_preserves_observed_match_reasons_without_task_changes(self):
        from sd_db.progress import tracker_items
        upsert_shadow(self.db, tracker="github", url=URL, repo="upstream/project", kind="pull", state="open")
        self.observe(reviews=[{"id": "review", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}])
        result = tracker_items(self.db)[0]
        self.assertEqual(result["why"], ["author"])
        self.assertTrue(result["needs_you"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)

    def test_tracker_context_reads_an_observed_issue_by_its_issue_key(self):
        # An issue URL under the `github:` prefix is refused by `_key`, so
        # every tracker read with one observed issue raised (sd:1205).
        from sd_db.progress import tracker_items
        upsert_shadow(self.db, tracker="github", url=ISSUE, repo="upstream/project", kind="issue", state="open")
        self.store_issue(attention=[c._event("event:1", "New comment")])
        result = tracker_items(self.db)[0]
        self.assertEqual(result["why"], ["author"])
        self.assertTrue(result["needs_you"])
        self.assertEqual(result["attention"], ["New comment"])


class IssueTests(ContributionCase):
    def test_issue_url_has_its_own_validator_and_key_prefix(self):
        self.assertEqual(c._issue_url("https://github.com/Upstream/Project/issues/56/"), ISSUE)
        for value in (URL, ISSUE.replace("56", "0"), ISSUE + "x", None, 56):
            with self.assertRaisesRegex(WorkflowError, "issue_url must be a canonical GitHub issue URL"):
                c._issue_url(value)
        self.assertIsNone(c.PULL.fullmatch(ISSUE))
        with self.assertRaisesRegex(WorkflowError, "canonical GitHub pull-request URL"):
            c._url(ISSUE)
        self.assertEqual(c._key("issue:" + ISSUE.replace("upstream/project", "Upstream/Project")), "issue:" + ISSUE)
        for key in ("issues:" + ISSUE, "item:0", ISSUE):
            with self.assertRaisesRegex(WorkflowError, r"item:234\), github:PR-URL or issue:ISSUE-URL"):
                c._key(key)
        with self.assertRaisesRegex(WorkflowError, "canonical GitHub issue URL"):
            c._key("issue:" + URL)
        with self.assertRaisesRegex(WorkflowError, "canonical GitHub pull-request URL"):
            c._key("github:" + ISSUE)

    def test_a_row_carries_exactly_one_identity(self):
        metadata = self.metadata()
        draft = self.draft()
        cases = [({"pull_url": URL, "issue_url": ISSUE}, "pull request or an issue, not both"),
                 ({**draft, **metadata}, "an issue draft has no clone or branch"),
                 ({**draft, "local_branch": "contribution"}, "an issue draft has no clone or branch"),
                 ({"target_repo": "upstream/project"}, "needs target_repo, draft_title and draft_path"),
                 ({**draft, "draft_path": None}, "needs target_repo, draft_title and draft_path"),
                 ({**metadata, "draft_title": "half a draft"}, "needs target_repo, draft_title and draft_path"),
                 ({}, "unfiled work needs local_clone and local_branch")]
        for changes, refusal in cases:
            with self.assertRaisesRegex(WorkflowError, refusal, msg=changes):
                c.capture(self.db, title="Refused", changes=changes, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)
        for changes in ({"pull_url": URL}, {"issue_url": ISSUE.replace("56", "57")}, draft, metadata):
            c.capture(self.db, title="Accepted", changes=changes, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 4)

    def test_filing_a_draft_keeps_its_item_and_draft_fields(self):
        draft = self.draft()
        item = c.capture(self.db, title="Draft", changes=draft, who="operator")["item"]["id"]
        before = c.projection(self.db)[0]
        self.assertIsNone(before["url"])
        self.assertEqual(before["repo"], "upstream/project")
        self.assertEqual(before["reasons"], ["Issue draft has not been filed"])
        self.assertEqual(before["lane"], "awaiting_you")
        after = c.configure(self.db, item, {"issue_url": ISSUE.replace("upstream/project", "Upstream/Project")}, who="operator")
        self.assertEqual(after["item"]["id"], item)
        stored = json.loads(after["item"]["fields"])["contribution"]
        self.assertEqual(stored, {**draft, "issue_url": ISSUE})
        rows = c.projection(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], ISSUE)
        self.assertEqual(rows[0]["issue_url"], ISSUE)
        self.assertEqual(rows[0]["draft_title"], draft["draft_title"])
        self.assertEqual(rows[0]["draft_path"], draft["draft_path"])
        self.assertTrue(rows[0]["draft_verified"])
        self.assertEqual(rows[0]["reasons"], [])
        self.assertEqual(rows[0]["lane"], "awaiting_them")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 1)

    def test_duplicate_issue_and_draft_refuse_without_creating_an_item(self):
        c.capture(self.db, title="Filed", changes={"issue_url": ISSUE}, who="operator")
        with self.assertRaisesRegex(WorkflowError, "this issue already belongs to another contribution"):
            c.capture(self.db, title="Duplicate", changes={"issue_url": ISSUE.replace("upstream/project", "Upstream/Project")}, who="operator")
        draft = self.draft()
        c.capture(self.db, title="Draft", changes=draft, who="operator")
        alias = {**draft, "draft_path": {**draft["draft_path"], "path": str(self.root / ".." / self.root.name / "draft.md")}}
        for changes in (draft, {**draft, "target_repo": "Upstream/Project"}, alias,
                        {**draft, "issue_url": ISSUE.replace("56", "57")}):
            with self.assertRaisesRegex(WorkflowError, "this issue draft already belongs to another contribution"):
                c.capture(self.db, title="Duplicate", changes=changes, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 2)
        c.capture(self.db, title="Same body, other project", changes={**draft, "target_repo": "upstream/library"}, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 3)

    def test_target_repo_must_be_a_repo_and_agree_with_issue_url(self):
        for changes, refusal in (({"issue_url": ISSUE, "target_repo": "other/repo"}, "target_repo does not match issue_url"),
                                 ({"issue_url": ISSUE, "target_repo": "not a repo"}, "target_repo must be owner/repo"),
                                 ({**self.draft(), "target_repo": "upstream"}, "target_repo must be owner/repo")):
            with self.assertRaisesRegex(WorkflowError, refusal):
                c.capture(self.db, title="Refused", changes=changes, who="operator")
        state = c.capture(self.db, title="Filed", changes={"issue_url": ISSUE, "target_repo": "Upstream/Project"}, who="operator")
        self.assertEqual(json.loads(state["item"]["fields"])["contribution"]["target_repo"], "Upstream/Project")
        self.assertEqual(c.projection(self.db)[0]["target_repo"], "Upstream/Project")

    def test_draft_digest_is_verified_on_write_and_rechecked_on_read(self):
        draft = self.draft()
        path = draft["draft_path"]["path"]
        missing = str(self.root / "missing.md")
        cases = [({**draft["draft_path"], "sha256": "0" * 64}, "issue draft hash differs from the recorded digest"),
                 ({**draft["draft_path"], "path": missing}, "issue draft could not be read"),
                 ({**draft["draft_path"], "path": str(self.root)}, "issue draft could not be read"),
                 ({**draft["draft_path"], "path": "draft.md"}, "absolute file path with a SHA256 digest"),
                 ({**draft["draft_path"], "sha256": "abc"}, "absolute file path with a SHA256 digest"),
                 ({**draft["draft_path"], "body": "inline"}, "absolute file path with a SHA256 digest"),
                 (path, "absolute file path with a SHA256 digest")]
        for value, refusal in cases:
            with self.assertRaisesRegex(WorkflowError, refusal, msg=value):
                c.capture(self.db, title="Refused", changes={**draft, "draft_path": value}, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)
        item = c.capture(self.db, title="Draft", changes=draft, who="operator")["item"]["id"]
        row = c.projection(self.db)[0]
        self.assertTrue(row["draft_verified"])
        self.assertEqual(row["draft_path"], draft["draft_path"])
        self.assertEqual(row["reasons"], ["Issue draft has not been filed"])
        Path(path).write_text("# Bug\n\nEdited after review.\n")
        row = c.projection(self.db)[0]
        self.assertEqual(row["item_id"], item)
        self.assertFalse(row["draft_verified"])
        self.assertEqual(row["reasons"], ["Issue draft has not been filed; draft body is unverified"])
        self.assertTrue(row["needs_you"])
        with self.assertRaisesRegex(WorkflowError, "hash differs"):
            c.configure(self.db, item, {"draft_title": "Retitled"}, who="operator")
        Path(path).unlink()
        self.assertFalse(c.projection(self.db)[0]["draft_verified"])

    def test_a_nul_in_a_draft_or_evidence_path_is_a_refusal_not_a_crash(self):
        # `Path.stat` raises ValueError, not OSError, on an embedded NUL (sd:1205).
        draft = self.draft()
        nul = {**draft["draft_path"], "path": str(self.root / "draft\0.md")}
        with self.assertRaisesRegex(WorkflowError, "issue draft could not be read"):
            c.capture(self.db, title="Refused", changes={**draft, "draft_path": nul}, who="operator")
        metadata = self.metadata()
        metadata["evidence"][0]["artifact"] = str(self.root / "acceptance\0.log")
        with self.assertRaisesRegex(WorkflowError, "evidence artifact could not be read"):
            c.capture(self.db, title="Refused", changes=metadata, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)
        # A stored NUL path, written before this check, reads as unverified.
        item = c.capture(self.db, title="Draft", changes=draft, who="operator")["item"]["id"]
        fields = json.loads(item_state(self.db, item)["item"]["fields"])
        fields["contribution"]["draft_path"] = nul
        set_item_fields(self.db, item, fields=fields)
        row = c.projection(self.db)[0]
        self.assertFalse(row["draft_verified"])
        self.assertEqual(row["reasons"], ["Issue draft has not been filed; draft body is unverified"])

    def test_a_present_identity_field_of_the_wrong_type_is_refused_even_when_falsy(self):
        # Truthiness let `draft_path=[]` and friends through unvalidated (sd:1205).
        draft = self.draft()
        cases = [({"issue_url": ISSUE, "draft_path": []}, "absolute file path with a SHA256 digest"),
                 ({"pull_url": URL, "draft_path": {}}, "absolute file path with a SHA256 digest"),
                 ({"pull_url": URL, "target_repo": ""}, "target_repo must be owner/repo"),
                 ({"issue_url": ISSUE, "target_repo": 0}, "target_repo must be owner/repo"),
                 ({"pull_url": URL, "draft_title": ""}, "draft_title must not be blank"),
                 ({"issue_url": ISSUE, "draft_title": False}, "draft_title must be text"),
                 ({**draft, "pull_url": ""}, "canonical GitHub pull-request URL"),
                 ({**draft, "issue_url": []}, "canonical GitHub issue URL"),
                 ({"pull_url": URL, "issue_url": ""}, "canonical GitHub issue URL")]
        for changes, refusal in cases:
            with self.assertRaisesRegex(WorkflowError, refusal, msg=changes):
                c.capture(self.db, title="Refused", changes=changes, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)
        # None stays the absent value.
        c.capture(self.db, title="Filed", changes={"pull_url": URL, "issue_url": None, "draft_path": None}, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 1)

    def test_closed_issue_takes_the_terminal_lane_after_every_open_row(self):
        self.assertNotIn(c.LANES["closed"], {c.LANES["merged"], c.LANES["awaiting_them"]})
        self.assertGreater(c.LANES["closed"], max(v for k, v in c.LANES.items() if k not in {"merged", "closed"}))
        registered = c.capture(self.db, title="Filed issue", changes={"issue_url": ISSUE}, who="operator")["item"]["id"]
        self.store_issue(state="closed", state_reason="completed")
        self.store_issue(url=ISSUE.replace("56", "57"), state="closed", state_reason="not_planned")
        self.store_issue(url=ISSUE.replace("56", "58"))
        c.capture(self.db, title="Filed pull", changes={"pull_url": URL}, who="operator")
        for suffix, changes in (("1", {"state": "merged"}), ("2", {"state": "closed", "events": [self.event("closed")]}),
                                ("3", {"reviews": [{"id": "review:1", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}]})):
            url = URL + suffix
            c.observe_pull(self.db, url, self.observation(**changes), expected_revision=c.snapshot(self.db, "github:" + url)["revision"])
        metadata = self.metadata()
        dependency = {"kind": "merge", "url": DEPENDENCY}
        metadata.update(blocked_on="", depends_on=[dependency])
        unblocked = c.capture(self.db, title="Local", changes=metadata, who="operator")["item"]["id"]
        c.observe_dependencies(self.db, unblocked, [{"dependency": dependency, "state": "satisfied",
                    "proof": {"url": DEPENDENCY, "merged": True, "merge_commit_sha": "a" * 40}}],
                    observed_at=T0, expected_revision=c.snapshot(self.db, f"item:{unblocked}")["revision"])
        rows = c.projection(self.db)
        lanes = [row["lane"] for row in rows]
        self.assertEqual(sorted(lanes, key=c.LANES.__getitem__), lanes)
        self.assertEqual(lanes.count("closed"), 2)
        self.assertEqual(lanes.count("merged"), 1)
        last_open = max(i for i, lane in enumerate(lanes) if lane not in {"merged", "closed"})
        self.assertTrue(all(i > last_open for i, lane in enumerate(lanes) if lane == "closed"))
        by_url = {row["url"]: row for row in rows}
        self.assertEqual(by_url[ISSUE]["item_id"], registered)
        self.assertEqual(by_url[ISSUE]["lane"], "closed")
        self.assertEqual(by_url[ISSUE]["external_state"], "closed")
        self.assertFalse(by_url[ISSUE]["needs_you"])
        self.assertEqual(by_url[ISSUE.replace("56", "57")]["lane"], "closed")
        self.assertEqual(by_url[ISSUE.replace("56", "58")]["lane"], "awaiting_them")
        self.assertEqual(by_url[URL + "2"]["lane"], "awaiting_you")
        self.assertEqual(by_url[URL + "1"]["lane"], "merged")
        self.assertEqual(lanes[0], "newly_unblocked")
        self.assertEqual(len(rows), len(by_url))

    def test_unregistered_issue_checkpoint_is_listed_once_and_registered_one_not_twice(self):
        self.store_issue(title="Observed issue", labels=["bug"])
        rows = c.projection(self.db)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["item_id"])
        self.assertEqual(rows[0]["url"], ISSUE)
        self.assertEqual(rows[0]["key"], "issue:" + ISSUE)
        self.assertEqual(rows[0]["title"], "Observed issue")
        self.assertEqual(rows[0]["repo"], "upstream/project")
        self.assertEqual(rows[0]["lane"], "awaiting_them")
        self.assertEqual(rows[0]["observed_at"], T0)
        self.assertEqual(c.projection(self.db, repo="upstream/project"), rows)
        self.assertEqual(c.projection(self.db, repo="other/repo"), [])
        item = c.capture(self.db, title="Registered", changes={"issue_url": ISSUE}, who="operator")["item"]["id"]
        rows = c.projection(self.db)
        self.assertEqual([row["item_id"] for row in rows], [item])
        self.assertEqual(rows[0]["key"], "issue:" + ISSUE)
        self.assertEqual(rows[0]["title"], "Registered")
        self.assertIn("issue:" + ISSUE, rows[0]["notification_state"])

    def test_issue_attention_claims_fall_back_to_the_issue_url(self):
        state = self.store_issue(attention=[c._event("event:1", "New comment")])
        event = state["attention"][0]["id"]
        claim = c.claim_notification(self.db, "issue:" + ISSUE, event, owner="worker")
        self.assertEqual(claim["url"], ISSUE)
        self.assertEqual(claim["key"], "issue:" + ISSUE)
        row = c.projection(self.db)[0]
        self.assertEqual(row["lane"], "awaiting_you")
        self.assertEqual(row["reasons"], ["New comment"])
        finished = c.finish_notification(self.db, "issue:" + ISSUE, event, claim["token"], outcome="sent")
        acknowledged = c.acknowledge(self.db, "issue:" + ISSUE, [event], expected_revision=finished["revision"], who="operator")
        self.assertEqual(acknowledged["attention"], [])

    def test_blocking_label_change_invalidates_the_issue_snapshot(self):
        item = c.capture(self.db, title="Filed", changes={"issue_url": ISSUE}, who="operator")["item"]["id"]
        before = c.snapshot(self.db, "issue:" + ISSUE)
        c.configure(self.db, item, {"blocking_labels": ["blocked"]}, who="operator")
        self.assertNotEqual(c.snapshot(self.db, "issue:" + ISSUE)["revision"], before["revision"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM state WHERE key LIKE 'contribution:github:%'").fetchone()[0], 0)
        draft = c.capture(self.db, title="Draft", changes=self.draft(), who="operator")["item"]["id"]
        c.configure(self.db, draft, {"blocking_labels": ["blocked"]}, who="operator")
        self.assertEqual(self.db.execute("SELECT count(*) FROM state WHERE key LIKE 'contribution:issue:%'").fetchone()[0], 1)


class IssueActivityTests(ContributionCase):
    def observe_issue(self, url=ISSUE, **changes):
        return c.observe_issue(self.db, url, self.issue_observation(**changes),
                               expected_revision=c.snapshot(self.db, "issue:" + url)["revision"])

    def test_an_unrelated_malformed_item_stops_no_observation(self):
        # sd:7939: observe_pull and observe_issue read every item strictly, so one malformed item failed each.
        broken = create_item(self.db, kind="task", title="Broken", status="ready")
        set_item_fields(self.db, broken, fields={"contribution": "not an object"})
        self.db.commit()
        pull = c.capture(self.db, title="Pull", changes={"pull_url": URL}, who="operator")["item"]["id"]
        c.capture(self.db, title="Issue", changes={"issue_url": ISSUE}, who="operator")
        self.observe()
        self.observe_issue()
        for key in ("github:" + URL, "issue:" + ISSUE):
            self.assertIsNotNone(c.snapshot(self.db, key)["observation"], key)
        self.assertTrue(self.db.execute("SELECT 1 FROM note WHERE item = ? AND body LIKE 'PR lane: %'",
                                        (pull,)).fetchone(), "the pull request's item got no lane note")

    def issue_event(self, kind="comment", **changes):
        # A plain comment: neither a maintainer's nor a mention, which raises nothing on a pull.
        return self.event(kind, **{"maintainer": False, "mentions_operator": False, "url": ISSUE + "#issuecomment-1", **changes})

    def test_every_issue_event_kind_states_its_own_reason(self):
        cases = [(self.issue_event(maintainer=True), {}, "Maintainer comment"),
                 (self.issue_event(mentions_operator=True), {}, "Mentioned you"),
                 (self.issue_event(), {}, "New comment"),
                 (self.issue_event("label_added", label="blocked"), {"blocking_labels": ["blocked"], "labels": ["blocked"]}, "Blocking label: blocked"),
                 (self.issue_event("closed_completed"), {"state": "closed", "state_reason": "completed"}, "Closed as completed"),
                 (self.issue_event("closed_not_planned"), {"state": "closed", "state_reason": "not_planned"}, "Closed as not planned"),
                 (self.issue_event("closed"), {"state": "closed"}, "Closed"),
                 (self.issue_event("reopened"), {}, "Reopened")]
        for index, (event, changes, reason) in enumerate(cases):
            url = ISSUE.replace("56", str(60 + index))
            result = self.observe_issue(url, events=[event], **changes)
            self.assertEqual([(e["reason"], e["url"]) for e in result["attention"]], [(reason, event["url"])], event)
            self.assertNotIn("merge", reason)
            self.assertEqual(c.snapshot(self.db, "issue:" + url)["observation"]["events"], [event])
        self.assertEqual(self.db.execute("SELECT count(*) FROM item").fetchone()[0], 0)
        rows = c.projection(self.db)
        self.assertEqual(len(rows), len(cases))
        self.assertTrue(all(row["needs_you"] and row["lane"] == "awaiting_you" for row in rows))
        self.assertNotIn("Closed without merge", [reason for row in rows for reason in row["reasons"]])

    def test_own_actions_removed_labels_and_unblocking_labels_never_fire(self):
        events = [self.issue_event(actor_id="1"), self.issue_event(actor_id="1", maintainer=True, id="two"),
                  self.issue_event("closed_completed", actor_id="1", id="closed"),
                  self.issue_event("label_added", id="label", label="unrelated"),
                  self.issue_event("label_added", id="stale", label="blocked"),
                  self.issue_event("label_removed", id="removed", label="blocked")]
        result = self.observe_issue(events=events, state="closed", state_reason="completed",
                                    blocking_labels=["blocked"], labels=["unrelated"])
        self.assertEqual(result["attention"], [])
        self.assertEqual(result["notifications"], {})

    def test_lifecycle_families_collapse_to_the_latest_event(self):
        reopened = [self.issue_event("closed_completed", id="closed"), self.issue_event("reopened", id="reopened", actor_id="1", at=T1)]
        self.assertEqual(self.observe_issue(events=reopened)["attention"], [])
        reclosed = [self.issue_event("closed_not_planned", id="first"), self.issue_event("closed_completed", id="second", at=T1)]
        result = self.observe_issue(observed_at=T2, events=reclosed, state="closed", state_reason="completed")
        self.assertEqual([e["reason"] for e in result["attention"]], ["Closed as completed"])
        labels = [self.issue_event("label_added", id="added", label="blocked"),
                  self.issue_event("label_removed", id="removed", label="blocked", at=T1)]
        self.assertEqual(self.observe_issue(observed_at=T3, events=labels, blocking_labels=["blocked"], labels=["blocked"])["attention"], [])

    def test_closed_completed_issue_takes_the_closed_lane_once_acknowledged(self):
        item = c.capture(self.db, title="Filed", changes={"issue_url": ISSUE}, who="operator")["item"]["id"]
        result = self.observe_issue(events=[self.issue_event("closed_completed")], state="closed", state_reason="completed")
        row = c.projection(self.db)[0]
        self.assertEqual((row["item_id"], row["lane"], row["reasons"]), (item, "awaiting_you", ["Closed as completed"]))
        note = next(iter(result["notifications"].values()))["note_id"]
        self.assertEqual(self.db.execute("SELECT item, kind, resolved_at FROM note WHERE id=?", (note,)).fetchone()[:3], (item, "followup", None))
        c.acknowledge(self.db, "issue:" + ISSUE, [result["attention"][0]["id"]], expected_revision=result["revision"], who="operator")
        row = c.projection(self.db)[0]
        self.assertEqual((row["lane"], row["external_state"], row["needs_you"], row["reasons"]), ("closed", "closed", False, []))
        self.assertEqual(item_state(self.db, item)["item"]["status"], "planning")

    def test_unchanged_recollection_never_announces_twice(self):
        first = self.observe_issue(events=[self.issue_event()])
        second = self.observe_issue(observed_at=T1, events=[self.issue_event()])
        self.assertEqual(second["attention"], first["attention"])
        self.assertEqual(second["notifications"], first["notifications"])
        self.assertEqual(len(second["notifications"]), 1)
        held = c.observe_issue(self.db, ISSUE, {"complete": False, "reason": "gh timed out", "observed_at": T2},
                               expected_revision=second["revision"])
        self.assertEqual(held["revision"], second["revision"])
        self.assertEqual(c.projection(self.db)[0]["freshness"]["reason"], "Incomplete GitHub observation: gh timed out")
        with self.assertRaises(StaleItem):
            c.observe_issue(self.db, ISSUE, self.issue_observation(observed_at=T2), expected_revision="stale")
        with self.assertRaisesRegex(WorkflowError, "older than the last complete"):
            self.observe_issue(observed_at=T0)
        unknown = self.observe_issue(observed_at=T3, operator={"id": None})
        self.assertEqual(unknown["revision"], second["revision"])

    def test_issue_observation_refuses_pull_shapes_and_the_pull_vocabulary(self):
        before = c.snapshot(self.db, "issue:" + ISSUE)
        for changes, refusal in ((self.observation(), "pull-request fields"), ({"reviews": []}, "pull-request fields"),
                                 ({"state": "merged"}, "invalid issue lifecycle"), ({"state_reason": "duplicate"}, "invalid issue lifecycle"),
                                 ({"repo": "other/project"}, "differs from issue URL"), ({"events": None}, "events must be a list"),
                                 ({"events": [self.issue_event("converted_to_draft")]}, "unsupported contribution event kind"),
                                 ({"events": [self.issue_event("ready_for_review")]}, "unsupported contribution event kind")):
            with self.subTest(changes=changes), self.assertRaisesRegex(WorkflowError, refusal):
                self.observe_issue(**changes)
        with self.assertRaisesRegex(WorkflowError, "canonical GitHub issue URL"):
            c.observe_issue(self.db, URL, self.issue_observation(), expected_revision=before["revision"])
        self.assertEqual(c.snapshot(self.db, "issue:" + ISSUE), before)
        self.assertEqual(c.projection(self.db), [])
        # The pull path gains the two close reasons in its vocabulary and stays otherwise unchanged.
        result = self.observe(events=[self.event("closed_completed"), self.event("closed")], state="closed")
        self.assertEqual([e["reason"] for e in result["attention"]], ["Closed without merge"])

    def test_blocking_policy_change_refuses_the_old_issue_observation(self):
        item = c.capture(self.db, title="Filed", changes={"issue_url": ISSUE}, who="operator")["item"]["id"]
        before = c.snapshot(self.db, "issue:" + ISSUE)
        c.configure(self.db, item, {"blocking_labels": ["blocked"]}, who="operator")
        with self.assertRaises(StaleItem):
            c.observe_issue(self.db, ISSUE, self.issue_observation(), expected_revision=before["revision"])
        with self.assertRaises(StaleItem):
            self.observe_issue()
        result = self.observe_issue(blocking_labels=["blocked"], labels=["blocked"],
                                    events=[self.issue_event("label_added", label="blocked")])
        self.assertEqual([e["reason"] for e in result["attention"]], ["Blocking label: blocked"])


class IssueDependencyTests(ContributionCase):
    def test_issue_dependency_is_validated_and_a_cycle_through_it_is_still_refused(self):
        dependency = {"kind": "issue", "url": ISSUE}
        self.assertEqual(c._dependency(dependency), dependency)
        for bad in ({"kind": "issue", "url": URL}, {"kind": "issue"}, {"kind": "issue", "url": ISSUE, "tag": "v1"}):
            with self.subTest(bad=bad), self.assertRaises(WorkflowError):
                c._dependency(bad)
        first = c.capture(self.db, title="first", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        second = c.capture(self.db, title="second", changes={"pull_url": URL.replace("12", "13"),
                                                              "depends_on": [{"kind": "item", "item": first}]}, who="operator")["item"]["id"]
        with self.assertRaisesRegex(WorkflowError, "cycle"):
            c.configure(self.db, first, {"depends_on": [dependency, {"kind": "item", "item": second}]}, who="operator")
        stored = json.loads(item_state(self.db, first)["item"]["fields"])["contribution"]["depends_on"]
        self.assertEqual(stored, [dependency])
        self.assertEqual(c.projection(self.db)[0]["lane"], "awaiting_them")

    def test_issue_is_satisfied_by_a_completed_close_or_a_merged_pull_not_by_not_planned(self):
        dependency = {"kind": "issue", "url": ISSUE}
        completed = {"url": ISSUE, "closed": True, "state_reason": "completed"}
        merged = {"url": ISSUE, "closed": False, "state_reason": None, "merged_pull": DEPENDENCY}
        self.assertTrue(c._satisfied(dependency, completed))
        self.assertTrue(c._satisfied(dependency, merged))
        self.assertTrue(c._satisfied(dependency, {**completed, "merged_pull": DEPENDENCY}))
        for proof in ({"url": ISSUE, "closed": True, "state_reason": "not_planned"},
                      {"url": ISSUE, "closed": True, "state_reason": None},
                      {"url": ISSUE, "closed": False, "state_reason": "completed"},
                      {**completed, "url": ISSUE.replace("56", "57")},
                      {**merged, "merged_pull": DEPENDENCY.replace("pull", "issues")},
                      {**merged, "merged_pull": DEPENDENCY.replace("upstream", "Upstream")},
                      {**merged, "merged_pull": True}, {}, None):
            self.assertFalse(c._satisfied(dependency, proof), proof)
        item = c.capture(self.db, title="Waiting", changes={"pull_url": URL, "depends_on": [dependency]}, who="operator")["item"]["id"]
        key = f"item:{item}"
        before = c.snapshot(self.db, key)
        for row in ({"dependency": dependency, "state": "unknown"},
                    {"dependency": dependency, "state": "satisfied", "proof": {"url": ISSUE, "closed": True, "state_reason": "not_planned"}}):
            self.assertEqual(c.observe_dependencies(self.db, item, [row], observed_at=T0,
                                                    expected_revision=before["revision"])["revision"], before["revision"])
        pending = c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "pending", "proof": merged | {"merged_pull": None}}],
                                         observed_at=T0, expected_revision=before["revision"])
        self.assertFalse(pending["observation"]["satisfied"])
        self.assertEqual(c.projection(self.db)[0]["lane"], "awaiting_them")
        ready = c.observe_dependencies(self.db, item, [{"dependency": dependency, "state": "satisfied", "proof": merged}],
                                       observed_at=T1, expected_revision=pending["revision"])
        self.assertEqual([e["kind"] for e in ready["attention"]], ["newly_unblocked"])
        self.assertEqual(c.projection(self.db)[0]["lane"], "newly_unblocked")
        # An issue depending on an issue: the row's own identity does not matter to the kind.
        filed = c.capture(self.db, title="Filed issue", changes={"issue_url": ISSUE.replace("56", "57"), "depends_on": [dependency]}, who="operator")["item"]["id"]
        self.assertEqual([row["lane"] for row in c.projection(self.db) if row["item_id"] == filed], ["awaiting_them"])

    def test_issue_dependency_neither_triggers_nor_breaks_the_release_cross_proof(self):
        merge = {"kind": "merge", "url": DEPENDENCY}
        release = {"kind": "release", "repo": "upstream/library", "tag": "v1", "contains_pull": DEPENDENCY}
        issue = {"kind": "issue", "url": ISSUE}
        item = c.capture(self.db, title="Blocked", changes={"pull_url": URL, "depends_on": [merge, release, issue]}, who="operator")["item"]["id"]
        key = f"item:{item}"
        merged = {"dependency": merge, "state": "satisfied", "proof": {"url": DEPENDENCY, "merged": True, "merge_commit_sha": "d" * 40}}
        released = {"dependency": release, "state": "satisfied", "proof": {
            "repo": "upstream/library", "tag": "v1", "contains_pull": DEPENDENCY, "published": True, "ancestor": True,
            "merged": True, "merge_commit_sha": "d" * 40, "tag_commit": "e" * 40}}
        closed = {"dependency": issue, "state": "satisfied", "proof": {"url": ISSUE, "closed": True, "state_reason": "completed"}}
        before = c.snapshot(self.db, key)
        other = copy.deepcopy(released)
        other["proof"]["merge_commit_sha"] = "f" * 40
        held = c.observe_dependencies(self.db, item, [merged, other, closed], observed_at=T0, expected_revision=before["revision"])
        self.assertEqual(held["revision"], before["revision"])
        self.assertEqual(c.projection(self.db)[0]["freshness"]["reason"], "Release proof does not contain the configured merged commit")
        ready = c.observe_dependencies(self.db, item, [merged, released, closed], observed_at=T1, expected_revision=before["revision"])
        self.assertTrue(ready["observation"]["satisfied"])
        self.assertEqual(c.projection(self.db)[0]["lane"], "newly_unblocked")


class DismissTests(ContributionCase):
    """The operator's Dismiss for a row with no item (sd:3432): a repository re-created upstream
    left a pull request URL that 404s, so every observation holds and the row read open forever."""

    def lanes(self):
        return {row["key"]: row["lane"] for row in c.projection(self.db)}

    def test_dismiss_moves_a_row_with_no_item_out_of_the_open_lanes(self):
        key = "github:" + URL
        observed = self.observe()
        self.assertEqual(self.lanes(), {key: "awaiting_them"})
        dismissed = c.dismiss(self.db, key, who="operator", expected_revision=observed["revision"])
        self.assertEqual((dismissed["dismissed"]["by"], dismissed["dismissed"]["state"]), ("operator", "open"))
        [row] = c.projection(self.db)
        self.assertEqual((row["lane"], row["needs_you"], row["external_state"]), ("closed", False, "open"))
        self.assertEqual(row["dismissed"], dismissed["dismissed"])
        # A second dismissal is a no-op, so a double click cannot rewrite who dismissed it.
        again = c.dismiss(self.db, key, who="someone-else", expected_revision=dismissed["revision"])
        self.assertEqual(again["dismissed"], dismissed["dismissed"])

    def test_dismiss_acknowledges_active_attention_and_its_pending_notifications(self):
        key = "github:" + URL
        observed = self.observe(events=[self.event()])
        event = observed["attention"][0]["id"]
        self.assertEqual(self.lanes(), {key: "awaiting_you"})
        dismissed = c.dismiss(self.db, key, who="operator", expected_revision=observed["revision"])
        self.assertEqual(dismissed["attention"], [])
        self.assertIn(event, dismissed["acknowledged"])
        self.assertEqual(dismissed["notifications"][event]["acknowledged_by"], "operator")
        self.assertIsNone(c.claim_notification(self.db, key, event, owner="worker"))
        self.assertEqual(self.lanes(), {key: "closed"})

    def test_a_repeat_dismiss_acknowledges_attention_that_arrived_after_the_first(self):
        # pack sd:7398: the repeat returned early, reported success and left the row in awaiting_you.
        key = "github:" + URL
        first = c.dismiss(self.db, key, who="operator", expected_revision=self.observe()["revision"])
        noticed = self.observe(observed_at=T1, events=[self.event()])
        event = noticed["attention"][0]["id"]
        self.assertEqual(self.lanes(), {key: "awaiting_you"})
        again = c.dismiss(self.db, key, who="someone-else", expected_revision=noticed["revision"])
        self.assertEqual(self.lanes(), {key: "closed"})
        self.assertEqual(again["attention"], [])
        self.assertIn(event, again["acknowledged"])
        self.assertEqual(again["notifications"][event]["acknowledged_by"], "someone-else")
        self.assertIsNone(c.claim_notification(self.db, key, event, owner="worker"))
        # The first dismissal's record stays: who closed the row and from which state.
        self.assertEqual(again["dismissed"], first["dismissed"])
        # The repeat ran under the revision the operator saw, so a stale one still refuses.
        with self.assertRaises(StaleItem):
            c.dismiss(self.db, key, who="operator", expected_revision=noticed["revision"])

    def test_dismiss_refuses_an_item_row_an_unobserved_key_and_another_rows_revision(self):
        other = URL.replace("12", "13")
        registered = c.capture(self.db, title="Filed", changes={"pull_url": other}, who="operator")["item"]["id"]
        filed = c.observe_pull(self.db, other, self.observation(), expected_revision=c.snapshot(self.db, "github:" + other)["revision"])
        for key, revision in (("github:" + other, filed["revision"]),
                              (f"item:{registered}", c.snapshot(self.db, f"item:{registered}")["revision"]),
                              ("github:" + URL, c.snapshot(self.db, "github:" + URL)["revision"])):
            with self.subTest(key=key), self.assertRaises(WorkflowError):
                c.dismiss(self.db, key, who="operator", expected_revision=revision)
        observed = self.observe()
        with self.assertRaises(StaleItem):
            c.dismiss(self.db, "github:" + URL, who="operator", expected_revision=filed["revision"])
        stale = observed["revision"]
        self.observe(observed_at=T1)
        with self.assertRaises(StaleItem):
            c.dismiss(self.db, "github:" + URL, who="operator", expected_revision=stale)
        self.assertNotIn("dismissed", c.snapshot(self.db, "github:" + URL))

    def test_dismissal_holds_across_held_and_same_state_observations_and_ends_on_a_state_change(self):
        key = "github:" + URL
        c.dismiss(self.db, key, who="operator", expected_revision=self.observe()["revision"])
        held = c.observe_pull(self.db, URL, {"complete": False, "reason": "Not Found"},
                              expected_revision=c.snapshot(self.db, key)["revision"])
        self.assertIn("dismissed", held)
        self.observe(observed_at=T1)
        self.assertEqual(self.lanes(), {key: "closed"})
        # New attention still reaches the operator; once acknowledged the row is dismissed again.
        noticed = self.observe(observed_at=T2, events=[self.event()])
        self.assertEqual(self.lanes(), {key: "awaiting_you"})
        c.acknowledge(self.db, key, [noticed["attention"][0]["id"]], who="operator", expected_revision=noticed["revision"])
        self.assertEqual(self.lanes(), {key: "closed"})
        # A lifecycle change is new information: the dismissal ends, and the row follows its real state.
        merged = self.observe(observed_at=T3, state="merged")
        self.assertNotIn("dismissed", merged)
        self.assertEqual(self.lanes(), {key: "merged"})

    def test_an_issue_dismissal_ends_when_the_issue_reopens(self):
        key = "issue:" + ISSUE

        def observe(**changes):
            return c.observe_issue(self.db, ISSUE, self.issue_observation(**changes),
                                   expected_revision=c.snapshot(self.db, key)["revision"])

        c.dismiss(self.db, key, who="operator", expected_revision=observe(state="closed", state_reason="completed")["revision"])
        observe(observed_at=T1, state="closed", state_reason="completed")
        self.assertIn("dismissed", c.snapshot(self.db, key))
        observe(observed_at=T2)
        self.assertNotIn("dismissed", c.snapshot(self.db, key))
        self.assertEqual(self.lanes(), {key: "awaiting_them"})

    def test_a_task_made_for_a_dismissed_row_shows_it_again(self):
        key = "github:" + URL
        c.dismiss(self.db, key, who="operator", expected_revision=self.observe()["revision"])
        c.capture(self.db, title="Tracked after all", changes={"pull_url": URL}, who="operator")
        [row] = c.projection(self.db)
        self.assertEqual((row["lane"], row["dismissed"]), ("awaiting_them", None))

    def test_a_checkpoint_never_dismissed_keeps_the_revision_it_had(self):
        # A revision read before this change must still match afterwards: the body gains no default key.
        observed = self.observe()
        self.assertNotIn("dismissed", observed)
        row = self.db.execute("SELECT id FROM state WHERE kind='checkpoint' AND key=? ORDER BY id DESC LIMIT 1",
                              ("contribution:github:" + URL,)).fetchone()
        body = {k: v for k, v in observed.items() if k not in {"key", "revision"}}
        self.assertEqual(observed["revision"], c._digest({"key": "github:" + URL, "id": row["id"], "body": body}))


class DraftWaitsOnYou(ContributionCase):
    """Pack sd:7868: a build's draft pull request, linked on its item, waits on the operator until it is ready."""

    def linked(self):
        return c.capture(self.db, title="Built in a third-party repository", changes={"pull_url": URL},
                         who="build")["item"]["id"]

    def lane(self, url=URL):
        return next(row for row in c.projection(self.db) if row["url"] == url)

    def test_an_items_open_draft_reads_awaiting_you_until_marked_ready(self):
        item = self.linked()
        self.observe(draft=True)
        row = self.lane()
        self.assertEqual((row["item_id"], row["lane"], row["needs_you"]), (item, "awaiting_you", True))
        self.assertIn(c.DRAFTED, row["reasons"])
        self.observe(draft=False, observed_at=T1)
        row = self.lane()
        self.assertEqual((row["lane"], row["needs_you"]), ("awaiting_them", False))
        self.assertNotIn(c.DRAFTED, row["reasons"])

    def test_a_draft_with_no_item_waits_on_you_and_another_authors_keeps_its_lane(self):
        """sd:8116 (d): the operator's open draft waits on them, linked or not; another author's waits on them."""
        self.observe(draft=True)
        row = self.lane()
        self.assertEqual((row["item_id"], row["lane"]), (None, "awaiting_you"))
        self.assertIn(c.DRAFTED, row["reasons"])
        self.linked()
        self.observe(draft=True, observed_at=T1, author={"id": "2", "login": "other"})
        self.assertEqual(self.lane()["lane"], "awaiting_them")

    def test_a_merged_draft_is_merged(self):
        self.linked()
        self.observe(draft=True, state="merged")
        self.assertEqual(self.lane()["lane"], "merged")

    def test_the_lane_note_reads_the_draft_as_awaiting_you(self):
        """sd:7859's lane notes read `_lane`, as the projection does: the draft waits on the operator there too."""
        item = self.linked()
        self.observe(draft=True)
        c.record_lane(self.db, item)
        self.assertEqual([note["lane"] for note in c.lane_notes(self.db, item)][-1:], ["awaiting_you"])


class UnlinkedWaits(ContributionCase):
    """sd:8116: the operator's observed pull requests with no item are waits too, read from their reading alone."""

    NOW = "2026-09-10T06:00:00+00:00"

    def pull(self, number, **changes):
        url = f"https://github.com/upstream/project/pull/{number}"
        c.observe_pull(self.db, url, self.observation(**changes),
                       expected_revision=c.snapshot(self.db, "github:" + url)["revision"])
        return url

    def waits(self, now=None):
        now = now or self.NOW
        return {w["url"]: w for w in [*c.pr_waits(self.db, now=now), *c.unlinked_pr_waits(self.db, now=now)]}

    def test_an_unlinked_authored_pull_waits_from_its_newest_event_and_a_strangers_does_not(self):
        other = {"id": "2", "login": "someone"}
        mine = self.pull(40, observed_at=T3, events=[self.event(id="e1", at=T1, maintainer=False), self.event(
            id="e2", at=T2, actor_id="1")], reviews=[{"id": "review:1", "actor_id": "2", "state": "COMMENTED", "at": T0}])
        asked = self.pull(41, observed_at=T3, author=other, requested_reviewers=["author"])
        self.pull(42, observed_at=T3, author=other, requested_reviewers=[])
        self.pull(43, observed_at=T3, state="merged")
        waits = self.waits()
        self.assertEqual(sorted(waits), [mine, asked])
        wait = waits[mine]
        self.assertEqual({k: wait[k] for k in ("item", "repo", "open", "on", "since", "approximate", "stale", "draft", "mine")},
                         {"item": None, "repo": "upstream/project", "open": True, "on": "reviewers", "since": T2,
                          "approximate": True, "stale": False, "draft": False, "mine": True})
        self.assertEqual((wait["on_reviewers"], wait["on_you"], wait["waited"]), (21480, 0, 21480))

    def test_with_no_event_the_wait_starts_at_created_at(self):
        url = self.pull(44, observed_at=T3)
        upsert_shadow(self.db, tracker="github", url=url, repo="upstream/project", number=44, kind="pull",
                      title="Contribution", state="open", created_at=T1)
        self.db.commit()
        self.assertEqual(self.waits()[url]["since"], T1)

    def test_a_force_push_or_review_request_dates_the_wait(self):
        url = self.pull(48, observed_at=T3, events=[self.event("pushed", id="p", at=T2, actor_id="1", maintainer=None)])
        self.assertEqual(self.waits()[url]["since"], T2)

    def test_an_unlinked_operator_draft_waits_on_you_marked_draft(self):
        url = self.pull(46, observed_at=T3, draft=True)
        wait = self.waits()[url]
        self.assertEqual((wait["on"], wait["draft"], wait["lane"]), ("you", True, "awaiting_you"))

    def test_a_stale_reading_is_no_current_wait_and_says_so(self):
        """sd:8116 (a): a pull request last read two days ago may have merged since; it is not counted as waiting."""
        url = self.pull(47, observed_at=T3)
        item = c.capture(self.db, title="Linked", changes={"pull_url": URL}, who="operator")["item"]["id"]
        self.observe(observed_at=T3)
        later = "2026-09-12T00:03:00+00:00"
        waits = self.waits(later)
        for key in (url, URL):
            self.assertEqual((waits[key]["stale"], waits[key]["open"], waits[key]["on"], waits[key]["waited"]),
                             (True, False, None, None), key)
        self.assertEqual(waits[URL]["item"], item)
        self.assertEqual(waits[URL]["on_reviewers"], 0, "its span ends at the reading, not two days on")
        self.assertEqual(waits[url]["on_reviewers"], 0, "an unlinked one's span ends at the reading too")

    def test_a_reading_that_does_not_parse_reads_stale_and_fails_no_row(self):
        """sd:8116 (a), class Failures: a bad time is an unknown reading, not a current wait and not a failed read."""
        self.assertTrue(c._stale({"observed_at": "not a time"}, self.NOW))
        self.assertFalse(c._stale({}, self.NOW), "no reading at all says nothing about its age")

    def test_a_pull_left_with_no_context_leaves_the_projection(self):
        """sd:8377: a team-only request read before sd:8360 kept a row with no reason, item or authorship."""
        other = {"id": "2", "login": "someone"}
        team = self.pull(50, author=other, requested_reviewers=[], why=["review-requested"])
        quiet = self.pull(51, author=other, requested_reviewers=[], why=[])
        kept = {self.pull(52, author=other, requested_reviewers=[], why=["mentioned"]),
                self.pull(53, author=other, requested_reviewers=["Author"], why=["review-requested"]),
                self.pull(54, author=other, requested_reviewers=[], why=[],
                          reviews=[{"id": "review:1", "actor_id": "1", "state": "APPROVED", "at": T0}]),
                self.pull(55, why=[]), self.pull(56, author=other, why=[])}
        listed = {row["url"] for row in c.projection(self.db)}
        self.assertEqual(listed & {team, quiet}, set())
        self.assertEqual(kept - listed, set(), "a mention, a named request, a review, authorship or an old read stays")
        item = c.capture(self.db, title="Tracked", changes={"pull_url": quiet}, who="operator")["item"]["id"]
        self.assertIn(item, {row["item_id"] for row in c.projection(self.db)}, "an item's pull stays listed")


class ReRequestedReview(ContributionCase):
    """sd:8116 (c): changes requested, the author pushes and asks that reviewer again: it waits on them."""

    def test_a_re_request_after_changes_requested_waits_on_reviewers(self):
        reviews = [{"id": "review:1", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}]
        events = [self.event(id="comment:1", at=T0), self.event(id="comment:2", at=T1, actor_id="1"),
                  self.event("pushed", id="push:1", at=T1, actor_id="1", maintainer=None),
                  self.event("review_requested", id="ask:1", at=T2, actor_id="1", reviewer_id="2", maintainer=None)]
        result = self.observe(observed_at=T3, head="c" * 40, ci_head="c" * 40, reviews=reviews, events=events)
        self.assertEqual(result["attention"], [])
        self.assertEqual(c.projection(self.db)[0]["lane"], "awaiting_them")

    def test_a_request_to_another_reviewer_leaves_the_changes_request_on_you(self):
        reviews = [{"id": "review:1", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}]
        events = [self.event("review_requested", id="ask:1", at=T2, actor_id="1", reviewer_id="3", maintainer=None)]
        result = self.observe(observed_at=T3, reviews=reviews, events=events)
        self.assertEqual([a["reason"] for a in result["attention"]], ["Changes requested"])

    def test_a_team_request_answers_no_reviewer(self):
        reviews = [{"id": "review:1", "actor_id": "2", "state": "CHANGES_REQUESTED", "at": T0}]
        events = [self.event("review_requested", id="ask:1", at=T2, actor_id="1", reviewer_id=None, maintainer=None)]
        result = self.observe(observed_at=T3, reviews=reviews, events=events)
        self.assertEqual([a["reason"] for a in result["attention"]], ["Changes requested"])

    def test_the_authors_own_comment_is_never_a_maintainer_comment(self):
        result = self.observe(events=[self.event(id="comment:1", actor_id="1", maintainer=True)])
        self.assertEqual(result["attention"], [])

    def test_a_comment_after_the_re_request_is_still_on_you(self):
        events = [self.event("review_requested", id="ask:1", at=T1, actor_id="1", reviewer_id="2", maintainer=None),
                  self.event(id="comment:9", at=T2)]
        result = self.observe(observed_at=T3, events=events)
        self.assertEqual([a["reason"] for a in result["attention"]], ["Maintainer comment"])


class LaneNoteTests(ContributionCase):
    """sd:7859: each lane change of an item's pull request is a `comment` note; checkpoints get pruned, notes stay."""

    def item(self):
        return c.capture(self.db, title="Filed", changes={"pull_url": URL}, who="operator")["item"]["id"]

    def lanes(self, item):
        return [(n["lane"], n["state"], n["backfilled"]) for n in c.lane_notes(self.db, item)]

    def test_an_observed_pull_writes_one_lane_note_and_a_rerun_writes_none(self):
        item = self.item()
        self.assertEqual(self.lanes(item), [], "not yet observed: no lane to record")
        self.observe()
        self.observe(observed_at=T1)
        self.assertEqual(self.lanes(item), [("awaiting_them", "open", False)])
        body = self.db.execute("SELECT kind, session, body FROM note WHERE id = ?",
                               (c.lane_notes(self.db, item)[0]["id"],)).fetchone()
        self.assertEqual(tuple(body), ("comment", "contribution-observer", f"PR lane: awaiting_them · open · {URL}"))
        self.assertEqual(c.record_lanes(self.db), ([], []), "a refresh pass after it writes nothing either")

    def test_a_maintainer_comment_is_on_you_and_acknowledging_it_is_on_reviewers_again(self):
        item = self.item()
        self.observe()
        result = self.observe(observed_at=T1, events=[self.event()])
        c.acknowledge(self.db, "github:" + URL, [result["attention"][0]["id"]], expected_revision=result["revision"],
                      who="operator")
        self.observe(observed_at=T2, state="merged")
        self.assertEqual(self.lanes(item), [("awaiting_them", "open", False), ("awaiting_you", "open", False),
                                            ("awaiting_them", "open", False), ("merged", "merged", False)])

    def test_an_unrelated_malformed_item_does_not_roll_back_an_acknowledgement(self):
        # Review round 2 of sd:7859: acknowledge reads the registered items to note the lane; another item's bad
        # metadata must not undo the operator's acknowledgement.
        item = self.item()
        self.observe()
        result = self.observe(observed_at=T1, events=[self.event()])
        broken = self.task("Broken")
        set_item_fields(self.db, broken, fields={"contribution": "not an object"})
        self.db.commit()
        acknowledged = c.acknowledge(self.db, "github:" + URL, [result["attention"][0]["id"]],
                                     expected_revision=result["revision"], who="operator")
        self.assertEqual(acknowledged["attention"], [])
        self.assertEqual(self.lanes(item)[-1], ("awaiting_them", "open", False))

    def test_a_lane_note_that_fails_on_its_data_does_not_roll_back_the_observation(self):
        # Review round 2: the lane note is a side observation of the observation. A data refusal (SdDbError) is left
        # for the sync's lane pass to say; a store failure (sqlite3) still undoes both, as the next test shows.
        item = self.item()
        with mock.patch.object(c, "record_lane", side_effect=c.WorkflowError("contribution checkpoint is malformed")):
            self.observe()
        self.assertEqual(c.snapshot(self.db, "github:" + URL)["observation"]["state"], "open")
        self.assertEqual(self.lanes(item), [])
        self.assertEqual(c.record_lanes(self.db)[1], [])
        self.assertEqual(self.lanes(item), [("awaiting_them", "open", False)])

    def test_a_failed_lane_note_rolls_back_the_observation(self):
        item = self.item()
        before = c.snapshot(self.db, "github:" + URL)
        with mock.patch.object(c, "add_note", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                self.observe()
        self.assertEqual(c.snapshot(self.db, "github:" + URL)["revision"], before["revision"])
        self.assertEqual(self.lanes(item), [])
        self.observe()
        self.assertEqual(self.lanes(item), [("awaiting_them", "open", False)], "the next observation writes both")

    def shipped(self, item, phase="ready_to_send", url="https://github.com/Upstream/Project/pull/12"):
        """A receipt `sd-ship prepare` left: the pull request it opened for `item` (sd:7859)."""
        from sd_db import ship

        key = ship.receipt_key("upstream/project", f"feat/sd-{item}", item)
        ship.save(self.db, key, ship.read(self.db, key)[0],
                  {"item": item, "repository": "upstream/project", "branch": f"feat/sd-{item}", "phase": phase,
                   "pull_request": {"number": 12, "repository": "upstream/project", "base": "main", "url": url}})

    def task(self, title="Shipped"):
        from sd_db.workflow import capture_task

        return capture_task(self.db, title=title, priority=3, who="operator")["item"]["id"]

    def test_a_pull_request_prepare_opened_is_linked_to_its_item_and_noted_on_reviewers(self):
        self.observe()
        item, bundled, merged = self.task(), self.task("Bundled"), self.task("Merged")
        self.shipped(item)
        self.shipped(bundled)
        self.shipped(merged, phase="merged", url="https://github.com/upstream/project/pull/13")
        self.assertEqual(c.link_shipped(self.db), ([item], []), "one item per pull request; a merged receipt is history")
        self.assertEqual(c._metadata(item_state(self.db, item)["item"]), {"pull_url": URL})
        self.assertEqual(self.lanes(item), [("awaiting_them", "open", False)])
        self.assertEqual(c.link_shipped(self.db), ([], []), "a rerun links nothing")
        self.assertIsNone(c._metadata(item_state(self.db, bundled)["item"]))

    def test_every_kind_the_lane_delivers_is_linked_and_waits_on_its_pull_request(self):
        """`sd-ship --deliver` closes work, task and followup items; an idea rides only as an associated slice."""
        import ast

        script = (Path(__file__).resolve().parents[2] / "bin" / "sd-ship").read_text()
        delivering = next(ast.literal_eval(node.value) for node in ast.parse(script).body
                          if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "DELIVERING_KINDS")
        self.observe()
        made = {kind: create_item(self.db, kind=kind, title=kind, status="in_progress", priority=3)
                for kind in ("work", "followup", "task", "idea")}
        for number, kind in enumerate(made):
            self.shipped(made[kind], url=URL if kind == "work" else f"https://github.com/upstream/project/pull/{20 + number}")
        self.assertEqual(c.link_shipped(self.db), (sorted([made["work"], made["followup"], made["task"]]), []))
        self.assertIsNone(c._metadata(item_state(self.db, made["idea"])["item"]))
        self.assertEqual(self.lanes(made["work"]), [("awaiting_them", "open", False)])
        self.assertEqual([(w["item"], w["on"]) for w in c.pr_waits(self.db, now=T3)], [(made["work"], "reviewers")])
        self.assertEqual(c.link_shipped(self.db), ([], []), "a rerun links nothing")
        self.assertEqual(set(c.KINDS), set(delivering), "contributions take what the lane delivers")

    def test_the_operator_can_name_a_work_items_pull_request(self):
        work = create_item(self.db, kind="work", title="Slice", status="in_progress", priority=3)
        c.configure(self.db, work, {"pull_url": URL}, who="operator")
        self.observe()
        self.assertEqual(self.lanes(work), [("awaiting_them", "open", False)])
        self.assertEqual([w["item"] for w in c.pr_waits(self.db)], [work])

    def test_a_wait_is_the_operators_when_they_wrote_it_or_were_asked_by_name(self):
        """sd:8084: a team request names no user, and a pull request by someone else is not the operator's."""
        other = {"id": "2", "login": "someone"}
        cases: dict[str, dict] = {"authored": {}, "asked": {"author": other, "requested_reviewers": ["author"]},
                 "team only": {"author": other, "requested_reviewers": []},
                 "reviewed": {"author": other, "requested_reviewers": [],
                              "reviews": [{"id": "review:1", "actor_id": "1", "state": "APPROVED", "at": T0}]},
                 "unknown": {"author": other}}
        items = {}
        for number, (name, changes) in enumerate(cases.items(), start=30):
            url = f"https://github.com/upstream/project/pull/{number}"
            items[c.capture(self.db, title=name, changes={"pull_url": url}, who="operator")["item"]["id"]] = name
            c.observe_pull(self.db, url, self.observation(**changes),
                           expected_revision=c.snapshot(self.db, "github:" + url)["revision"])
        self.assertEqual({items[w["item"]]: w["mine"] for w in c.pr_waits(self.db)},
                         {"authored": True, "asked": True, "team only": False, "reviewed": True, "unknown": None})

    def test_an_item_that_names_a_pull_request_or_is_done_keeps_what_it_has(self):
        named = self.item()
        done = self.task("Done")
        transition(self.db, done, "done", who="operator")
        self.shipped(named, url="https://github.com/upstream/project/pull/14")
        self.shipped(done, url="https://github.com/upstream/project/pull/15")
        self.assertEqual(c.link_shipped(self.db), ([], []))
        self.assertEqual((c._metadata(item_state(self.db, named)["item"]) or {}).get("pull_url"), URL)

    def history(self, item):
        upsert_shadow(self.db, tracker="github", url=URL, repo="upstream/project", number=12, kind="pull",
                      title="Contribution", state="merged", created_at=T0, merged_at=T3)
        note = add_note(self.db, item, "followup", "Changes requested", session="contribution-observer", timestamp=T1)
        self.db.execute("UPDATE note SET resolved_at = ? WHERE id = ?", (T2, note))
        # The pull request's own followup: its checkpoint names the note (sd:7908).
        state = c.snapshot(self.db, "github:" + URL)
        state["notifications"]["review:1"] = {"status": "done", "note_id": note}
        c._save(self.db, state)
        self.db.commit()

    def test_backfill_reads_the_shadow_and_the_followups(self):
        item = self.item()
        self.history(item)
        self.observe(observed_at=T3, state="merged")
        notes = c.lane_notes(self.db, item)
        self.assertEqual([(n["at"], n["lane"], n["backfilled"]) for n in notes],
                         [(T0, "awaiting_them", True), (T1, "awaiting_you", True), (T2, "awaiting_them", True),
                          (T3, "merged", True)], "the current lane equals the last backfilled one: no extra note")
        wait = c.pr_waits(self.db, now=T3)[0]
        self.assertEqual((wait["on_reviewers"], wait["on_you"], wait["to_merge"], wait["open"]), (120, 60, 180, False))

    def test_a_backfill_that_fails_writes_no_note(self):
        item = self.item()
        self.history(item)
        real = c.add_note
        calls = []

        def second_fails(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real(*args, **kwargs)
        with mock.patch.object(c, "add_note", side_effect=second_fails):
            with self.assertRaises(sqlite3.OperationalError):
                self.observe(observed_at=T3, state="merged")
        self.assertEqual(self.lanes(item), [])
        self.assertIsNone(c.snapshot(self.db, "github:" + URL)["observation"], "the observation rolled back too")
        self.observe(observed_at=T3, state="merged")
        self.assertEqual(len(self.lanes(item)), 4, "the next observation backfills it all")

    def test_record_lanes_backfills_a_merged_item_the_queue_no_longer_observes(self):
        item = self.item()
        self.observe(observed_at=T3, state="merged")
        self.db.execute("DELETE FROM note WHERE body LIKE 'PR lane: %'")
        self.db.commit()
        self.history(item)
        c.record_lanes(self.db)
        self.assertEqual(self.lanes(item)[0], ("awaiting_them", "open", True))
        self.assertEqual(self.lanes(item)[-1], ("merged", "merged", True))

    def test_waits_split_on_you_and_on_reviewers_and_an_open_one_runs_to_now(self):
        item = self.item()
        for at, lane in ((T0, "awaiting_them"), (T1, "awaiting_you"), (T2, "awaiting_them")):
            add_note(self.db, item, "comment", f"PR lane: {lane} · open · {URL}", session="contribution-observer",
                     timestamp=at)
        wait = c.pr_waits(self.db, now="2026-09-10T00:05:00+00:00")[0]
        self.assertEqual({k: wait[k] for k in ("item", "open", "on", "since", "waited", "on_you", "on_reviewers", "to_merge")},
                         {"item": item, "open": True, "on": "reviewers", "since": T2, "waited": 180, "on_you": 60,
                          "on_reviewers": 240, "to_merge": None})

    def notes(self, item, *rows):
        for at, lane, state in rows:
            add_note(self.db, item, "comment", f"PR lane: {lane} · {state} · {URL}", session="contribution-observer",
                     timestamp=at)

    def test_a_close_then_a_later_acknowledgement_adds_no_wait(self):
        # Review of sd:7859: a maintainer closes without merging (awaiting_you, closed); acknowledging it later is on
        # reviewers again. The closed span is no wait.
        item = self.item()
        self.notes(item, (T0, "awaiting_them", "open"), (T1, "awaiting_you", "closed"), (T2, "awaiting_them", "closed"))
        wait = c.pr_waits(self.db, now=T3)[0]
        self.assertEqual((wait["on_you"], wait["on_reviewers"], wait["open"]), (0, 60, False))

    def test_attention_after_a_merge_adds_no_wait(self):
        item = self.item()
        self.notes(item, (T0, "awaiting_them", "open"), (T1, "merged", "merged"), (T2, "awaiting_you", "merged"),
                   (T3, "merged", "merged"))
        wait = c.pr_waits(self.db, now="2026-09-10T00:05:00+00:00")[0]
        self.assertEqual((wait["on_you"], wait["on_reviewers"], wait["to_merge"]), (0, 60, 60))

    def test_a_corrupt_receipt_is_reported_and_the_good_one_is_still_linked(self):
        self.observe()
        item = self.task()
        self.db.execute("INSERT INTO state(kind,key,timestamp,body) VALUES ('checkpoint','ship:bad',?, 'not json')", (T0,))
        self.db.commit()
        self.shipped(item)
        odd = self.task("Odd url")
        self.shipped(odd, url="https://example.test/pull/1")
        linked, errors = c.link_shipped(self.db)
        self.assertEqual(linked, [item])
        self.assertEqual(len(errors), 2, errors)
        self.assertIn("ship receipt ship:bad: unreadable ship receipt", errors[0])
        self.assertIn("pull_url must be a canonical GitHub pull-request URL", errors[1])
        self.assertEqual(self.lanes(item), [("awaiting_them", "open", False)])

    def test_an_item_with_malformed_metadata_is_reported_and_stops_neither_pass(self):
        broken, item = self.task("Broken"), self.item()
        self.observe()
        self.db.execute("DELETE FROM note WHERE body LIKE 'PR lane: %'")
        set_item_fields(self.db, broken, fields={"contribution": "not an object"})
        self.db.commit()
        written, errors = c.record_lanes(self.db)
        self.assertEqual((len(written), errors), (1, [f"item {broken}: item {broken} has malformed contribution metadata"]))
        self.assertEqual(self.lanes(item), [("awaiting_them", "open", False)])
        self.assertEqual(c.link_shipped(self.db)[1], errors)

    def test_one_item_that_fails_its_lane_note_is_reported_and_the_rest_are_noted(self):
        first = self.item()
        second = c.capture(self.db, title="Other", changes={"pull_url": DEPENDENCY}, who="operator")["item"]["id"]
        self.observe()
        c.observe_pull(self.db, DEPENDENCY, self.observation(repo="upstream/library"),
                       expected_revision=c.snapshot(self.db, "github:" + DEPENDENCY)["revision"])
        self.db.execute("DELETE FROM note WHERE body LIKE 'PR lane: %'")
        self.db.commit()
        real = c.record_lane

        def first_fails(connection, item):
            if item == first:
                raise c.WorkflowError("contribution checkpoint is malformed")
            return real(connection, item)
        with mock.patch.object(c, "record_lane", side_effect=first_fails):
            _, errors = c.record_lanes(self.db)
        self.assertEqual(errors, [f"item {first}: contribution checkpoint is malformed"])
        self.assertEqual((self.lanes(first), self.lanes(second)), ([], [("awaiting_them", "open", False)]))

    def test_a_backfill_event_with_no_zone_is_skipped_and_said_on_the_item(self):
        item = self.item()
        self.history(item)
        self.db.execute("UPDATE shadow SET created_at = '2026-09-10T00:00:00' WHERE url = ?", (URL,))
        self.db.commit()
        self.observe(observed_at=T3, state="merged")
        self.assertEqual(self.lanes(item), [("awaiting_you", "open", True), ("awaiting_them", "open", True),
                                            ("merged", "merged", True)])
        skipped = self.db.execute("SELECT body FROM note WHERE item = ? AND body LIKE 'PR lane backfill skipped%'",
                                  (item,)).fetchone()
        self.assertIn("'2026-09-10T00:00:00'", skipped["body"])

    def test_without_a_shadow_row_the_wait_starts_at_the_first_note(self):
        item = self.item()
        self.observe()
        self.assertEqual(self.lanes(item), [("awaiting_them", "open", False)])
        self.assertTrue(c.pr_waits(self.db, now=T3)[0]["open"])

    def test_a_bundles_pull_request_goes_to_the_lowest_item_number(self):
        # sd:7863: receipts sorted by str(item), so item 10 took the bundle's pull request before item 9.
        self.observe()
        items = [self.task(f"Item {n}") for n in range(10)]
        nine, ten = next(i for i in items if i == 9), next(i for i in items if i == 10)
        self.shipped(ten)
        self.shipped(nine)
        self.assertEqual(c.link_shipped(self.db), ([nine], []))

    def receipt(self, item, branch, url):
        """A second receipt for `item`, on another branch: a re-prepare after its first pull request closed."""
        from sd_db import ship

        key = ship.receipt_key("upstream/project", branch, item)
        ship.save(self.db, key, ship.read(self.db, key)[0],
                  {"item": item, "repository": "upstream/project", "branch": branch, "phase": "ready_to_send",
                   "pull_request": {"number": 13, "repository": "upstream/project", "base": "main", "url": url}})

    def test_the_newest_receipt_of_an_item_is_the_one_linked(self):
        newer = "https://github.com/upstream/project/pull/13"
        item = self.task()
        self.receipt(item, "feat/sd-z-old", URL)
        self.receipt(item, "feat/sd-a-new", newer)
        self.assertEqual(c.link_shipped(self.db), ([item], []))
        self.assertEqual(c._metadata(item_state(self.db, item)["item"]), {"pull_url": newer})

    def test_an_item_tied_to_a_closed_pull_request_is_relinked_to_its_newer_receipt(self):
        # sd:7863: a receipt has no closed phase, so an item re-prepared after its PR closed stayed on the stale PR.
        newer = "https://github.com/upstream/project/pull/13"
        item = self.task()
        self.receipt(item, "feat/sd-first", URL)
        self.assertEqual(c.link_shipped(self.db), ([item], []))
        self.observe(state="closed")
        self.receipt(item, "feat/sd-second", newer)
        self.assertEqual(c.link_shipped(self.db), ([item], []))
        self.assertEqual(c._metadata(item_state(self.db, item)["item"]), {"pull_url": newer})
        self.assertEqual(c.link_shipped(self.db), ([], []), "a rerun links nothing")

    def test_a_hand_linked_pull_request_that_closes_is_not_replaced_by_an_older_receipt(self):
        # sd:7931: a closed held link was swapped for the newest receipt, though the operator linked theirs after it.
        older = "https://github.com/upstream/project/pull/11"
        item = self.task()
        self.receipt(item, "feat/sd-first", older)
        self.assertEqual(c.link_shipped(self.db), ([item], []))
        c.configure(self.db, item, {"pull_url": URL}, who="operator")
        self.observe(state="closed")
        self.assertEqual(c.link_shipped(self.db), ([], []), "an older receipt replaced the operator's own link")
        self.assertEqual(c._metadata(item_state(self.db, item)["item"]), {"pull_url": URL})

    def test_a_hand_link_back_to_an_older_receipt_is_not_replaced_when_it_closes(self):
        # sd:7939: receipts A then B, a hand link back to A, A closes: B replaced it, the hand link being newer.
        newer = "https://github.com/upstream/project/pull/13"
        item = self.task()
        self.receipt(item, "feat/sd-first", URL)
        self.receipt(item, "feat/sd-second", newer)
        self.assertEqual(c.link_shipped(self.db), ([item], []))
        c.configure(self.db, item, {"pull_url": URL}, who="operator")
        self.observe(state="closed")
        self.assertEqual(c.link_shipped(self.db), ([], []), "the newest receipt replaced the operator's newer link")
        self.assertEqual(c._metadata(item_state(self.db, item)["item"]), {"pull_url": URL})

    def test_a_closed_hand_link_to_an_older_receipt_gives_way_to_a_receipt_written_after_it(self):
        newer = "https://github.com/upstream/project/pull/13"
        item = self.task()
        self.receipt(item, "feat/sd-first", URL)
        c.configure(self.db, item, {"pull_url": URL}, who="operator")
        self.db.execute("UPDATE note SET timestamp = '2026-01-01T00:00:00+00:00' WHERE item = ? AND body LIKE ?",
                        (item, "%" + c.LINKED + URL))
        self.db.commit()
        self.receipt(item, "feat/sd-second", newer)
        self.observe(state="closed")
        self.assertEqual(c.link_shipped(self.db), ([item], []))
        self.assertEqual(c._metadata(item_state(self.db, item)["item"]), {"pull_url": newer})

    def test_a_closed_hand_link_gives_way_to_a_receipt_whose_pull_request_is_open(self):
        newer = "https://github.com/upstream/project/pull/13"
        item = self.task()
        c.configure(self.db, item, {"pull_url": URL}, who="operator")
        self.observe(state="closed")
        self.receipt(item, "feat/sd-second", newer)
        c.observe_pull(self.db, newer, {**self.observation(), "url": newer},
                       expected_revision=c.snapshot(self.db, "github:" + newer)["revision"])
        self.assertEqual(c.link_shipped(self.db), ([item], []))
        self.assertEqual(c._metadata(item_state(self.db, item)["item"]), {"pull_url": newer})

    def test_an_open_linked_pull_request_is_not_replaced_by_a_newer_receipt(self):
        item = self.task()
        self.receipt(item, "feat/sd-first", URL)
        c.link_shipped(self.db)
        self.observe()
        self.receipt(item, "feat/sd-second", "https://github.com/upstream/project/pull/13")
        self.assertEqual(c.link_shipped(self.db), ([], []))
        self.assertEqual(c._metadata(item_state(self.db, item)["item"]), {"pull_url": URL})

    def test_an_unrelated_malformed_item_stops_no_receipts_link(self):
        # sd:7863 (note 46123): configure rescanned every item strictly, so one malformed item failed every link.
        self.observe()
        broken, item = self.task("Broken"), self.task()
        set_item_fields(self.db, broken, fields={"contribution": "not an object"})
        self.db.commit()
        self.shipped(item)
        self.assertEqual(c.link_shipped(self.db),
                         ([item], [f"item {broken}: item {broken} has malformed contribution metadata"]))

    def test_the_lane_note_reads_the_issue_source_the_projection_reads(self):
        # sd:7863: record_lane read the item and pull sources only; the projection reads the issue too.
        item = self.item()
        fields = json.loads(item_state(self.db, item)["item"]["fields"])
        fields["contribution"]["issue_url"] = ISSUE
        set_item_fields(self.db, item, fields=fields)
        self.store_issue(attention=[c._event("event:1", "New comment")])
        self.db.commit()
        self.observe()
        row = next(r for r in c.projection(self.db) if r["item_id"] == item)
        self.assertEqual((row["lane"], self.lanes(item)[-1][0]), ("awaiting_you", "awaiting_you"))

    def test_an_observed_issue_notes_the_lane_of_the_item_that_holds_it(self):
        item = self.item()
        self.observe()
        fields = json.loads(item_state(self.db, item)["item"]["fields"])
        fields["contribution"]["issue_url"] = ISSUE
        set_item_fields(self.db, item, fields=fields)
        self.db.commit()
        c.observe_issue(self.db, ISSUE, self.issue_observation(events=[self.event(url=ISSUE + "#issuecomment-1")]),
                        expected_revision=c.snapshot(self.db, "issue:" + ISSUE)["revision"])
        self.assertEqual(self.lanes(item), [("awaiting_them", "open", False), ("awaiting_you", "open", False)])

    def test_a_pull_request_already_merged_at_first_sync_has_no_time_to_merge(self):
        # sd:7908: with no open note before the merge, to_merge read 0.
        item = self.item()
        self.notes(item, (T1, "merged", "merged"))
        self.assertIsNone(c.pr_waits(self.db, now=T3)[0]["to_merge"])

    def test_a_merged_pull_request_with_no_merge_time_backfills_no_open_span(self):
        # sd:7908: a shadow with created_at and no merged_at left the backfilled span open until the sync wrote merged.
        item = self.item()
        upsert_shadow(self.db, tracker="github", url=URL, repo="upstream/project", number=12, kind="pull",
                      title="Contribution", state="merged", created_at=T0)
        self.db.commit()
        self.observe(observed_at=T3, state="merged")
        self.assertEqual(self.lanes(item), [("merged", "merged", False)])
        wait = c.pr_waits(self.db)[0]
        self.assertEqual((wait["on_reviewers"], wait["on_you"], wait["to_merge"]), (0, 0, None))

    def test_backfill_reads_only_the_pull_requests_followups_from_its_creation(self):
        # sd:7908: every observer followup on the item started the wait, one from before the PR was made too.
        item = self.item()
        upsert_shadow(self.db, tracker="github", url=URL, repo="upstream/project", number=12, kind="pull",
                      title="Contribution", state="merged", created_at=T1, merged_at=T3)
        other = add_note(self.db, item, "followup", "All contribution dependencies are satisfied",
                         session="contribution-observer", timestamp=T0)
        early = add_note(self.db, item, "followup", "Changes requested", session="contribution-observer",
                         timestamp=T0)
        self.db.execute("UPDATE note SET resolved_at = ? WHERE id = ?", (T2, early))
        self.db.execute("UPDATE note SET resolved_at = ? WHERE id = ?", (T3, other))
        state = c.snapshot(self.db, "github:" + URL)
        state["notifications"] = {"review:1": {"status": "done", "note_id": early}}
        c._save(self.db, state)
        self.db.commit()
        self.observe(observed_at=T3, state="merged")
        self.assertEqual([(n["at"], n["lane"]) for n in c.lane_notes(self.db, item)],
                         [(T1, "awaiting_you"), (T2, "awaiting_them"), (T3, "merged")])
        wait = c.pr_waits(self.db, now=T3)[0]
        self.assertEqual((wait["on_you"], wait["on_reviewers"], wait["to_merge"]), (60, 60, 120))

    def test_the_projection_lane_is_the_one_rule_the_note_reads(self):
        item = self.item()
        self.observe(events=[self.event()])
        row = next(r for r in c.projection(self.db) if r["item_id"] == item)
        self.assertEqual((row["lane"], self.lanes(item)[-1][0]), ("awaiting_you", "awaiting_you"))


class CloseOnMerge(ContributionCase):
    """sd:8375: the first record of a linked pull request as merged closes its open item, whoever records it."""

    MERGE = "c" * 40

    def item(self, **changes):
        item = c.capture(self.db, title="Shipped", changes={"pull_url": URL, **changes}, who="operator")
        return item["item"]["id"]

    def status(self, item):
        return self.db.execute("SELECT status FROM item WHERE id = ?", (item,)).fetchone()["status"]

    def prerequisite(self):
        from sd_db.workflow import capture_task

        return capture_task(self.db, title="Prerequisite", priority=3, who="operator")["item"]["id"]

    def notes(self, item):
        return [row["body"] for row in self.db.execute("SELECT body FROM note WHERE item = ? ORDER BY id", (item,))]

    def test_the_first_merged_record_closes_the_item_with_the_pull_and_merge_commit(self):
        item = self.item()
        self.observe()
        self.assertEqual(self.status(item), "planning", "an open pull request closes nothing")
        self.observe(observed_at=T1, state="merged", merge_commit=self.MERGE)
        self.assertEqual(self.status(item), "done")
        self.assertIn(f"PR {URL} merged at {self.MERGE}", "\n".join(self.notes(item)))

    def test_a_closed_unmerged_pull_closes_nothing(self):
        item = self.item()
        self.observe(state="closed", events=[self.event("closed")])
        self.assertEqual(self.status(item), "planning")

    def test_an_item_waiting_on_another_open_pull_stays_open(self):
        item = self.item(depends_on=[{"kind": "merge", "url": DEPENDENCY}])
        other = self.observation(repo="upstream/library")
        c.observe_pull(self.db, DEPENDENCY, other, expected_revision=c.snapshot(self.db, "github:" + DEPENDENCY)["revision"])
        self.observe(state="merged", merge_commit=self.MERGE)
        self.assertEqual(self.status(item), "planning")
        self.assertIn(f"PR {URL} merged at {self.MERGE}; not closed: merge dependency {DEPENDENCY} is not satisfied",
                      "\n".join(self.notes(item)))

    def test_an_item_with_an_unmet_item_issue_or_release_dependency_stays_open(self):
        release = {"kind": "release", "repo": "upstream/project", "tag": "v1.0.0", "contains_pull": URL}
        for dependency in ({"kind": "item"}, {"kind": "issue", "url": ISSUE}, release):
            with self.subTest(kind=dependency["kind"]):
                self.setUp()
                if dependency["kind"] == "item":
                    dependency = {"kind": "item", "item": self.prerequisite()}
                item = self.item(depends_on=[dependency])
                self.observe(state="merged", merge_commit=self.MERGE)
                self.assertEqual(self.status(item), "planning")
                self.assertIn(f"PR {URL} merged at {self.MERGE}; not closed: {dependency['kind']} dependency",
                              "\n".join(self.notes(item)))

    def test_an_item_whose_dependencies_are_recorded_satisfied_closes(self):
        prerequisite = self.prerequisite()
        dependency = {"kind": "item", "item": prerequisite}
        item = self.item(depends_on=[dependency])
        transition(self.db, prerequisite, "done", who="operator", reason="finished")
        self.db.commit()
        self.observe(state="merged", merge_commit=self.MERGE)
        self.assertEqual(self.status(item), "done")

    def satisfy(self, item, release):
        proof = {"repo": "upstream/project", "tag": "v1.0.0", "contains_pull": URL, "published": True, "merged": True,
                 "ancestor": True, "merge_commit_sha": self.MERGE, "tag_commit": "e" * 40}
        c.observe_dependencies(self.db, item, [{"dependency": release, "state": "satisfied", "proof": proof}],
                               observed_at=T1, expected_revision=c.snapshot(self.db, f"item:{item}")["revision"])

    def test_a_merge_refused_for_a_dependency_is_not_retried_when_it_is_satisfied(self):
        release = {"kind": "release", "repo": "upstream/project", "tag": "v1.0.0", "contains_pull": URL}
        item = self.item(depends_on=[release])
        self.observe(state="merged", merge_commit=self.MERGE)
        self.assertEqual(self.status(item), "planning", "the release is not out yet")
        self.satisfy(item, release)
        self.assertEqual(self.status(item), "planning", "only the open -> merged record closes")

    def test_a_reopened_item_stays_open_when_a_dependency_is_then_satisfied(self):
        release = {"kind": "release", "repo": "upstream/project", "tag": "v1.0.0", "contains_pull": URL}
        item = self.item(depends_on=[release])
        self.observe(state="merged", merge_commit=self.MERGE)
        transition(self.db, item, "done", who="operator", reason="finished by hand")
        transition(self.db, item, "in_progress", who="operator", reason="not finished")
        self.db.commit()
        self.satisfy(item, release)
        self.assertEqual(self.status(item), "in_progress")

    def test_a_reopened_item_stays_open_when_the_merge_is_recorded_again(self):
        item = self.item()
        self.observe(state="merged", merge_commit=self.MERGE)
        self.assertEqual(self.status(item), "done")
        transition(self.db, item, "in_progress", who="operator", reason="not finished")
        self.db.commit()
        self.observe(observed_at=T1, state="merged", merge_commit=self.MERGE)
        self.assertEqual(self.status(item), "in_progress")

    def test_a_work_item_stays_open_and_its_note_names_the_merge(self):
        item = create_item(self.db, kind="work", title="Work", status="in_progress")
        c.configure(self.db, item, {"pull_url": URL}, who="operator")
        result = self.observe(state="merged", merge_commit=self.MERGE)
        self.assertEqual(result["observation"]["state"], "merged", "the merge is recorded all the same")
        self.assertEqual(self.status(item), "in_progress")
        self.assertIn(f"PR {URL} merged at {self.MERGE}; not closed: ", "\n".join(self.notes(item)))

    # Round 6: a merge is not proof the item is done; one guard keeps each such item open with a note.
    def refused(self, item, why):
        self.assertIn(f"PR {URL} merged at {self.MERGE}; not closed: {why}", "\n".join(self.notes(item)))

    def test_an_item_blocked_on_something_stays_open(self):
        item = self.item(blocked_on="production validation")
        self.observe(state="merged", merge_commit=self.MERGE)
        self.assertEqual(self.status(item), "planning")
        self.refused(item, "it is blocked on: production validation")

    def test_an_item_a_build_or_teammate_run_holds_stays_open(self):
        from sd_db.writes import create_assignment

        for status in ("queued", "running", "ending", "in_session"):
            with self.subTest(status=status):
                self.setUp()
                item = self.item()
                held = create_assignment(self.db, role="builder", status=status, item=item)
                self.db.commit()
                self.observe(state="merged", merge_commit=self.MERGE)
                self.assertEqual(self.status(item), "planning")
                self.refused(item, f"assignment {held} holds it")

    def test_an_item_a_retained_build_run_holds_stays_open(self):
        """Round 7 class pass: `build._held`, the third holding guard a teammate start checks, keeps a merged item
        open while an unreleased build run holds it, as lead or bundle member, though its assignment is blocked."""
        from sd_db.writes import create_assignment, upsert_repo

        for role in ("lead", "member"):
            with self.subTest(role=role):
                self.setUp()
                upsert_repo(self.db, "/example/repo", managed=1)
                linked, other = self.item(), self.prerequisite()
                lead_item, member_item = (linked, other) if role == "lead" else (other, linked)
                lead = create_assignment(self.db, item=lead_item, role="author", status="blocked")
                member = create_assignment(self.db, item=member_item, role="author", status="blocked", parent=lead)
                self.db.execute("UPDATE assignment SET scope = 'build' WHERE id IN (?, ?)", (lead, member))
                self.db.execute(
                    "INSERT INTO runner_run (id, assignment, run, repo, branch, owner, work_path, retained_path, "
                    "start_step, step, outcome, created_at, updated_at) VALUES ('kept', ?, 1, '/example/repo', "
                    "'build/sd-1', 'fixture', '/w/b', '/w/b', 'claimed', 'building', 'interrupted', ?, ?)",
                    (lead, T1, T1))
                self.db.commit()
                self.observe(state="merged", merge_commit=self.MERGE)
                self.assertEqual(self.status(linked), "planning")
                self.refused(linked, f"item {linked} is held by build run kept")

    def test_an_item_a_lane_entry_owes_its_close_stays_open(self):
        """Round 7: a lane entry that delivers the item owns its close (`sd-ship`'s close_task), even when the sync
        records the merge first; `teammate.lane_holding` names it, as a teammate start checks."""
        for status in ("pending", "running", "prepared", "merged"):
            with self.subTest(status=status):
                self.setUp()
                item = self.item()
                entry = {"id": "e1", "item": item, "status": status, "claim": "deliver", "closes": [],
                         "merge_commit": self.MERGE}
                self.db.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('checkpoint', ?, ?, ?)",
                                ("lane:v1:example/repo:e1", T1, json.dumps(entry)))
                self.db.commit()
                self.observe(state="merged", merge_commit=self.MERGE)
                self.assertEqual(self.status(item), "planning")
                self.refused(item, f"item {item} ")
                self.assertIn("lane entry e1", "\n".join(self.notes(item)))

    def test_an_item_reopened_after_the_merge_stays_open(self):
        item = self.item()
        transition(self.db, item, "done", who="operator", reason="finished by hand")
        transition(self.db, item, "in_progress", who="operator", reason="not finished")
        self.db.commit()
        self.observe(state="merged", merge_commit=self.MERGE, merged_at="2000-01-01T00:00:00Z")
        self.assertEqual(self.status(item), "in_progress")
        self.refused(item, "it was reopened after the merge")

    def test_an_item_reopened_before_the_merge_closes(self):
        item = self.item()
        transition(self.db, item, "done", who="operator", reason="finished by hand")
        transition(self.db, item, "in_progress", who="operator", reason="not finished")
        self.db.commit()
        self.observe(state="merged", merge_commit=self.MERGE, merged_at="2999-01-01T00:00:00Z")
        self.assertEqual(self.status(item), "done")

    def test_an_item_waiting_outside_the_open_statuses_stays_open(self):
        item = self.item()
        transition(self.db, item, "blocked", who="operator", reason="waiting on a vendor")
        self.db.commit()
        self.observe(state="merged", merge_commit=self.MERGE)
        self.assertEqual(self.status(item), "blocked")
        self.refused(item, "its status is blocked")

    def test_a_work_item_names_the_delivery_evidence_it_needs(self):
        item = create_item(self.db, kind="work", title="Work", status="in_progress")
        c.configure(self.db, item, {"pull_url": URL}, who="operator")
        self.observe(state="merged", merge_commit=self.MERGE)
        self.assertEqual(self.status(item), "in_progress")
        self.refused(item, "a work item closes only with delivery evidence")
