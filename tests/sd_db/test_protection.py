"""The fleet's branch protection: classified as sd-status classifies it,
collected on the tracker's budget, and never `protected` without a 200 on
classic protection or a ruleset whose rules gate a merge."""

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, initialise, protection, upsert_repo
from sd_db.contribution_github import Client, Response
from sd_db.shadow_sync import _SearchBudget, read_watermark
from sd_db.shadow_sync import sync as tracker_sync

from .test_shadow_sync import Gh, node

AT = "2026-09-11T02:45:00Z"
MAIN = "main"


def full_protection(**changes):
    """enforce_admins on, strict on, one approval, one required context."""
    payload = {
        "enforce_admins": {"enabled": True},
        "required_status_checks": {"strict": True, "contexts": ["CI Result"]},
        "required_pull_request_reviews": {"required_approving_review_count": 1},
    }
    payload.update(changes)
    return payload


def repo_payload(**changes):
    """The pack's clean merge settings: PR_TITLE / PR_BODY, rebase disallowed;
    `admin`, so a 404 on classic protection is GitHub saying there is none."""
    payload = {"default_branch": MAIN, "squash_merge_commit_title": "PR_TITLE",
               "squash_merge_commit_message": "PR_BODY", "allow_rebase_merge": False,
               "permissions": {"admin": True}}
    payload.update(changes)
    return payload


def ids(result):
    return [gap["id"] for gap in result["gaps"]]


class Classify(unittest.TestCase):
    def test_a_fully_enforced_branch_has_no_gaps(self):
        result = protection.classify(full_protection(), repo_payload(), MAIN, {"CI Result"}, [])
        self.assertEqual(result["status"], "protected")
        self.assertEqual(ids(result), [])
        self.assertEqual([flag["flagged"] for flag in result["merge_settings"]], [False, False])
        self.assertEqual(result["detail"]["required_contexts"], ["CI Result"])
        self.assertEqual(result["detail"]["required_approving_review_count"], 1)

    def test_the_platypeeps_shape_names_strict_produced_not_required_and_reviews(self):
        """enforce_admins on, one aggregate context, strict off, a reviews
        object asking for 0 approvals, and three produced checks."""
        payload = full_protection(
            required_status_checks={"strict": False, "contexts": ["CI Result"]},
            required_pull_request_reviews={"required_approving_review_count": 0},
        )
        repo = repo_payload(squash_merge_commit_title="COMMIT_OR_PR_TITLE",
                            squash_merge_commit_message="COMMIT_MESSAGES", allow_rebase_merge=True)
        result = protection.classify(payload, repo, MAIN, {"CI Result", "test", "lint"}, [])
        self.assertEqual(result["status"], "protected")
        self.assertEqual(ids(result), ["strict", "produced_not_required", "reviews"])
        self.assertEqual(result["detail"]["produced_not_required"], ["lint", "test"])
        self.assertEqual(result["detail"]["required_not_produced"], [])
        self.assertIn("lint, test", result["gaps"][1]["gap"])
        flags = {flag["id"]: flag for flag in result["merge_settings"]}
        self.assertEqual(sorted(flags), ["rebase_merge", "squash_message"])
        self.assertTrue(flags["squash_message"]["flagged"])
        self.assertEqual(flags["squash_message"]["value"], "COMMIT_OR_PR_TITLE / COMMIT_MESSAGES")
        self.assertTrue(flags["rebase_merge"]["flagged"])
        self.assertEqual(flags["rebase_merge"]["value"], "allowed")

    def test_a_404_is_unprotected_and_its_gaps_say_so(self):
        result = protection.classify(None, repo_payload(), MAIN, {"CI Result"}, [])
        self.assertEqual(result["status"], "unprotected")
        self.assertEqual(ids(result), ["unprotected", "required_checks", "reviews"])
        self.assertIn("no branch protection at all", result["gaps"][0]["gap"])
        self.assertIn("a red PR still merges", result["gaps"][1]["gap"])
        self.assertEqual(result["detail"]["required_approving_review_count"], 0)

    def test_fewer_approvals_than_the_policy_asks_is_the_reviews_gap(self):
        def reviews(count, human):
            return [gap["gap"] for gap in protection.classify(
                full_protection(required_pull_request_reviews={"required_approving_review_count": count}),
                repo_payload(), MAIN, {"CI Result"}, [], human=human)["gaps"]]
        self.assertEqual(reviews(0, 0), [])
        self.assertEqual(len(reviews(0, 1)), 1)
        self.assertEqual(reviews(1, 2), ["a pull request is required but 1 approval is fewer than the 2 "
                                         "the repository policy asks for"])
        self.assertEqual(reviews(2, 2), [])

    def test_an_absent_reviews_object_and_zero_approvals_are_worded_apart(self):
        absent = protection.classify(full_protection(required_pull_request_reviews=None),
                                     repo_payload(), MAIN, {"CI Result"}, [])
        zero = protection.classify(
            full_protection(required_pull_request_reviews={"required_approving_review_count": 0}),
            repo_payload(), MAIN, {"CI Result"}, [])
        self.assertEqual(ids(absent), ["reviews"])
        self.assertEqual(ids(zero), ["reviews"])
        self.assertEqual(absent["gaps"][0]["gap"], "no pull-request review is required on main")
        self.assertEqual(zero["gaps"][0]["gap"],
                         "a pull request is required but no approving review is: "
                         "0 approvals, so one with green CI self-merges")

    def test_enforce_admins_off_is_the_first_gap_and_the_doctrine_sentence(self):
        result = protection.classify(full_protection(enforce_admins={"enabled": False}),
                                     repo_payload(), MAIN, {"CI Result"}, [])
        self.assertEqual(ids(result), ["enforce_admins"])
        self.assertIn("prose, not authority", result["gaps"][0]["gap"])
        self.assertFalse(result["detail"]["enforce_admins"])

    def test_a_required_context_nothing_produces_is_named(self):
        result = protection.classify(full_protection(), repo_payload(), MAIN, {"test"}, [])
        self.assertEqual(ids(result), ["required_not_produced", "produced_not_required"])
        self.assertIn("CI Result", result["gaps"][0]["gap"])

    def test_an_unreadable_checkout_reports_neither_comparison_gap(self):
        note = "checkout /nowhere is absent"
        result = protection.classify(full_protection(), repo_payload(), MAIN, None, [note])
        self.assertEqual(ids(result), [])
        self.assertIsNone(result["detail"]["produced_contexts"])
        self.assertEqual(result["detail"]["workflow_notes"], [note])

    def test_the_platypeeps_shape_has_no_produced_contexts_and_no_contexts_gap(self):
        """No `.github/workflows` produces nothing, and then the only
        comparison gap is the required context nothing produces."""
        result = protection.classify(full_protection(), repo_payload(), MAIN, set(), ["none"])
        self.assertEqual(ids(result), ["required_not_produced"])


class ProducedContexts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def workflow(self, name, text):
        directory = self.root / ".github" / "workflows"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text, encoding="utf-8")

    def test_job_names_matrix_names_and_a_non_pr_workflow_left_out(self):
        self.workflow("ci.yml", "name: CI\non:\n  pull_request:\n  push:\njobs:\n"
                      "  unit:\n    name: unittest\n    runs-on: ubuntu-latest\n"
                      "  lint:\n    runs-on: ubuntu-latest\n"
                      "  native:\n    name: system-native\n    strategy:\n      matrix:\n"
                      "        os: [macos-14, macos-15]\n    runs-on: ${{ matrix.os }}\n"
                      "  result:\n    name: CI Result\n    if: always()\n    runs-on: ubuntu-latest\n")
        self.workflow("nightly.yml", "on:\n  schedule:\n    - cron: '0 2 * * *'\njobs:\n"
                      "  sweep:\n    runs-on: ubuntu-latest\n")
        produced, notes = protection.produced_contexts(self.root)
        self.assertEqual(produced, {"unittest", "lint", "system-native (macos-14)",
                                    "system-native (macos-15)", "CI Result"})
        self.assertEqual(len(notes), 1)
        self.assertIn("result is conditional", notes[0])

    def test_a_reusable_workflow_is_a_note_not_a_name(self):
        self.workflow("ci.yml", "on: [pull_request]\njobs:\n  shared:\n    uses: org/repo/.github/workflows/x.yml@main\n")
        produced, notes = protection.produced_contexts(self.root)
        self.assertEqual(produced, set())
        self.assertIn("reusable workflow", notes[0])

    def test_no_workflows_directory_is_an_empty_set_with_a_note(self):
        produced, notes = protection.produced_contexts(self.root)
        self.assertEqual(produced, set())
        self.assertIn("produces no checks", notes[0])

    def test_an_absent_checkout_is_none_and_says_so(self):
        produced, notes = protection.produced_contexts(self.root / "gone")
        self.assertIsNone(produced)
        self.assertIn("absent on this machine", notes[0])

    def test_a_local_mode_repository_produces_the_local_gate_even_without_its_checkout(self):
        """sd:1992. The local gate's context comes from `repo.ci`, not from
        files, so an absent checkout still has an answer to compare."""
        self.workflow("ci.yml", PR_WORKFLOW)
        produced, notes = protection.produced_contexts(self.root, ci="local")
        self.assertEqual(produced, {"sd/local-gate"})
        self.assertIn("repo.ci is local", notes[-1])
        produced, notes = protection.produced_contexts(self.root / "gone", ci="local")
        self.assertEqual((produced, len(notes)), ({"sd/local-gate"}, 1))
        self.assertIn("repo.ci is local", notes[0])

    def test_an_inline_comment_is_not_part_of_the_value(self):
        """sd:1204 3d279a13628a. `name: lint # fast path` named the context
        `lint # fast path`; a `#` inside quotes is still text."""
        self.workflow("ci.yml", "on: [pull_request] # CI\njobs:\n"
                      "  a:\n    name: lint # fast path\n    runs-on: x\n"
                      "  b:\n    name: 'tag #1'\n    runs-on: x\n")
        produced, _ = protection.produced_contexts(self.root)
        self.assertEqual(produced, {"lint", "tag #1"})

    def test_an_excluded_matrix_combination_is_not_produced(self):
        """sd:1204 7000d0fd28dd. `exclude:` was skipped, so a combination GitHub
        never runs was reported as a check the repository produces."""
        self.workflow("ci.yml", "on: [pull_request]\njobs:\n  t:\n    name: test\n"
                      "    strategy:\n      matrix:\n        os: [a, b]\n        py: ['1', '2']\n"
                      "        exclude:\n          - os: a\n            py: '1'\n")
        produced, _ = protection.produced_contexts(self.root)
        self.assertEqual(produced, {"test (a, 2)", "test (b, 1)", "test (b, 2)"})

    def test_a_scalar_trigger_is_read_as_that_one_event(self):
        """sd:1204 26c900e54e2b. `on: schedule` read as "could not tell", so a
        schedule-only workflow's jobs counted as pull-request checks."""
        self.workflow("nightly.yml", "on: schedule\njobs:\n  sweep:\n    runs-on: x\n")
        self.workflow("ci.yml", "on: pull_request\njobs:\n  lint:\n    runs-on: x\n")
        produced, _ = protection.produced_contexts(self.root)
        self.assertEqual(produced, {"lint"})

    def test_a_job_whose_names_are_not_derived_leaves_required_contexts_unjudged(self):
        """sd:1204 96d8568ce317. A reusable workflow can report the required
        context; reported as one nothing produces, it was a false gap."""
        self.workflow("ci.yml", "on: [pull_request]\njobs:\n  lint:\n    runs-on: x\n"
                      "  shared:\n    uses: org/repo/.github/workflows/x.yml@main\n")
        produced, notes = protection.produced_contexts(self.root)
        result = protection.classify(full_protection(), repo_payload(), MAIN, produced, notes)
        self.assertEqual(ids(result), ["produced_not_required"])
        self.assertEqual(result["detail"]["required_not_produced"], [])


class Transport:
    """A canned GitHub: `{path: payload | Response}`; a missing path is a 500."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, path, fields, timeout):
        self.calls.append(path)
        value = self.rows.get(path, Response(500, {}, ""))
        if isinstance(value, Response):
            return value
        return Response(200, {}, json.dumps(value))


class SyncCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        initialise(home / "sd.db")
        self.db = connect(home / "sd.db")
        self.addCleanup(self.db.close)
        self.home = home

    def checkout(self, name, workflow=None):
        root = self.home / name
        root.mkdir()
        if workflow is not None:
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "ci.yml").write_text(workflow, encoding="utf-8")
        return str(root)

    def register(self, name, remote, workflow=None, *, ci=None):
        path = self.checkout(name, workflow)
        upsert_repo(self.db, path, remote=remote, ci=ci)
        return path

    def client(self, rows, *, requests=100, seconds=30):
        transport = Transport(rows)
        return Client(_SearchBudget(requests, time.monotonic() + seconds), transport=transport), transport

    def stored(self):
        return {row["repo"]: dict(row) for row in self.db.execute("SELECT * FROM repo_protection")}


PR_WORKFLOW = "on: [pull_request]\njobs:\n  result:\n    name: CI Result\n    runs-on: ubuntu-latest\n"


class Sync(SyncCase):
    def test_three_answers_land_as_three_statuses_and_a_foreign_remote_writes_nothing(self):
        guarded = self.register("guarded", "git@github.com:platypeeps/guarded.git", PR_WORKFLOW)
        bare = self.register("bare", "https://github.com/platypeeps/bare.git", PR_WORKFLOW)
        private = self.register("private", "ssh://git@github.com/octocat/private", PR_WORKFLOW)
        elsewhere = self.register("elsewhere", "https://gitlab.example.invalid/x/y.git")
        client, transport = self.client({
            "repos/platypeeps/guarded": repo_payload(),
            f"repos/platypeeps/guarded/branches/{MAIN}/protection": full_protection(),
            protection.rules_path("platypeeps", "guarded", MAIN): [],
            "repos/platypeeps/bare": repo_payload(default_branch="trunk"),
            "repos/platypeeps/bare/branches/trunk/protection": Response(404, {}, "{}"),
            protection.rules_path("platypeeps", "bare", "trunk"): [],
            "repos/octocat/private": Response(403, {}, ""),
        })
        summary = protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual(summary, {"attempted": 3, "protected": 1, "unprotected": 1, "unknown": 1,
                                   "requests": 7,
                                   "errors": ["octocat/private: API HTTP 403; retry on a later collection"]})
        rows = self.stored()
        self.assertEqual(sorted(rows), sorted([guarded, bare, private]))
        self.assertNotIn(elsewhere, rows)
        self.assertEqual(rows[guarded]["status"], "protected")
        self.assertEqual(rows[guarded]["default_branch"], MAIN)
        self.assertIsNone(rows[guarded]["reason"])
        self.assertEqual(json.loads(rows[guarded]["body"])["gaps"], [])
        self.assertEqual(rows[bare]["status"], "unprotected")
        self.assertEqual(rows[bare]["default_branch"], "trunk")
        self.assertEqual([gap["id"] for gap in json.loads(rows[bare]["body"])["gaps"]],
                         ["unprotected", "required_checks", "reviews"])
        self.assertEqual(rows[private]["status"], "unknown")
        self.assertEqual(rows[private]["reason"], "API HTTP 403; retry on a later collection")
        self.assertEqual(rows[private]["observed_at"], AT)
        # Three GETs per repository that answered -- the rules are read
        # whatever classic protection answered (sd:1430) -- one for the 403.
        self.assertEqual(transport.calls, [
            "repos/platypeeps/bare", "repos/platypeeps/bare/branches/trunk/protection",
            protection.rules_path("platypeeps", "bare", "trunk"),
            "repos/platypeeps/guarded", f"repos/platypeeps/guarded/branches/{MAIN}/protection",
            protection.rules_path("platypeeps", "guarded", MAIN),
            "repos/octocat/private"])

    def test_a_404_from_a_token_without_admin_is_unknown_with_the_reason(self):
        """GitHub answers 404 `Not Found` on classic protection to a caller
        without `admin` on the repository whether or not the branch is
        protected -- measured 2026-09-22 on home-assistant/core and
        gohugoio/hugo, both protected. Only an admin's 404 says "no
        protection"; any other 404 is `unknown`, with the reason naming the
        permission, and never `unprotected`. `permissions` absent from the
        repository object is the same unknown."""
        hidden = self.register("hidden", "https://github.com/upstream/hidden.git", PR_WORKFLOW)
        blank = self.register("blank", "https://github.com/upstream/blank.git", PR_WORKFLOW)
        own = self.register("own", "https://github.com/platypeeps/own.git", PR_WORKFLOW)
        without = repo_payload()
        without.pop("permissions")
        client, transport = self.client({
            "repos/upstream/hidden": repo_payload(permissions={"admin": False, "push": True}),
            f"repos/upstream/hidden/branches/{MAIN}/protection": Response(404, {}, '{"message": "Not Found"}'),
            protection.rules_path("upstream", "hidden", MAIN): [],
            "repos/upstream/blank": without,
            f"repos/upstream/blank/branches/{MAIN}/protection": Response(404, {}, '{"message": "Not Found"}'),
            protection.rules_path("upstream", "blank", MAIN): [],
            "repos/platypeeps/own": repo_payload(),
            f"repos/platypeeps/own/branches/{MAIN}/protection": Response(404, {}, '{"message": "Branch not protected"}'),
            protection.rules_path("platypeeps", "own", MAIN): [],
        })
        summary = protection.sync(self.db, client=client, observed_at=AT)
        rows = self.stored()
        self.assertEqual(rows[own]["status"], "unprotected")
        for path in (hidden, blank):
            with self.subTest(path=path):
                self.assertEqual(rows[path]["status"], "unknown")
                self.assertIn("admin", rows[path]["reason"])
                self.assertEqual(json.loads(rows[path]["body"])["gaps"], [])
        self.assertEqual(summary["unprotected"], 1)
        self.assertEqual(summary["unknown"], 2)
        self.assertEqual(len(summary["errors"]), 2)
        self.assertTrue(all("admin" in error for error in summary["errors"]))
        # Three GETs for each: the classic 404 is read, the rules are read
        # and answer nothing, and only then is the 404 judged by the token.
        self.assertEqual(len(transport.calls), 9)

    def test_a_ruleset_that_gates_the_branch_is_protected_whatever_the_classic_endpoint_says(self):
        """A branch a ruleset protects answers 404 on the classic endpoint to
        its admin as well, so admin + 404 is not "no protection" either: the
        rules are read first, and a `pull_request` or `required_status_checks`
        rule is protection -- for the admin and for a token without admin
        alike, the rules endpoint being readable without it (home-assistant/core
        answers its rules to a token that gets 404 on classic)."""
        own = self.register("own", "https://github.com/platypeeps/own.git", PR_WORKFLOW)
        upstream = self.register("upstream", "https://github.com/upstream/core.git", PR_WORKFLOW)
        review = {"type": "pull_request", "ruleset_id": 7,
                  "parameters": {"required_approving_review_count": 1}}
        checks = {"type": "required_status_checks", "ruleset_id": 7,
                  "parameters": {"strict_required_status_checks_policy": True,
                                 "required_status_checks": [{"context": "CI Result"}]}}
        ruleset = {"id": 7, "name": "main", "enforcement": "active", "bypass_actors": []}
        client, _ = self.client({
            "repos/platypeeps/own": repo_payload(),
            f"repos/platypeeps/own/branches/{MAIN}/protection": Response(404, {}, '{"message": "Branch not protected"}'),
            protection.rules_path("platypeeps", "own", MAIN): [review, checks, {"type": "deletion", "ruleset_id": 7}],
            "repos/platypeeps/own/rulesets/7": ruleset,
            "repos/upstream/core": repo_payload(permissions={"admin": False}),
            f"repos/upstream/core/branches/{MAIN}/protection": Response(404, {}, '{"message": "Not Found"}'),
            protection.rules_path("upstream", "core", MAIN): [review, checks],
            "repos/upstream/core/rulesets/7": ruleset,
        })
        summary = protection.sync(self.db, client=client, observed_at=AT)
        rows = self.stored()
        for path in (own, upstream):
            with self.subTest(path=path):
                self.assertEqual(rows[path]["status"], "protected")
                self.assertIsNone(rows[path]["reason"])
                body = json.loads(rows[path]["body"])
                self.assertEqual(body["gaps"], [])
                self.assertEqual(body["detail"]["source"], "ruleset")
                self.assertEqual(body["detail"]["required_contexts"], ["CI Result"])
                self.assertEqual(body["detail"]["required_approving_review_count"], 1)
                self.assertTrue(body["detail"]["enforce_admins"])
                self.assertEqual(body["requests"], 4)
        self.assertEqual((summary["protected"], summary["unprotected"], summary["unknown"]), (2, 0, 0))

    def test_a_bypass_list_not_shown_leaves_enforce_admins_unknown_not_enforced(self):
        """GitHub withholds `bypass_actors` from a caller who cannot edit the
        ruleset, and may withhold the ruleset object itself. Either is an
        `enforce_admins` gap that says unknown and names the ruleset, never
        the empty list that would read as nobody bypasses; a list that names
        organization admins is the classic `enforce_admins is off` gap."""
        withheld = self.register("withheld", "https://github.com/upstream/withheld.git", PR_WORKFLOW)
        gone = self.register("gone", "https://github.com/upstream/gone.git", PR_WORKFLOW)
        open_ = self.register("open", "https://github.com/upstream/open.git", PR_WORKFLOW)
        review = {"type": "pull_request", "ruleset_id": 7, "parameters": {"required_approving_review_count": 1}}
        rows = {}
        for name, ruleset in (("withheld", {"id": 7, "name": "guard", "enforcement": "active"}),
                              ("gone", Response(404, {}, "{}")),
                              ("open", {"id": 7, "name": "guard", "bypass_actors": [
                                  {"actor_type": "OrganizationAdmin", "actor_id": 1, "bypass_mode": "always"}]})):
            rows[f"repos/upstream/{name}"] = repo_payload(permissions={"admin": False})
            rows[f"repos/upstream/{name}/branches/{MAIN}/protection"] = Response(404, {}, "{}")
            rows[protection.rules_path("upstream", name, MAIN)] = [review]
            rows[f"repos/upstream/{name}/rulesets/7"] = ruleset
        client, _ = self.client(rows)
        protection.sync(self.db, client=client, observed_at=AT)
        stored = self.stored()
        for path, named in ((withheld, "guard (#7)"), (gone, "7 (#7)")):
            with self.subTest(path=path):
                self.assertEqual(stored[path]["status"], "protected")
                body = json.loads(stored[path]["body"])
                self.assertIsNone(body["detail"]["enforce_admins"])
                admins = [gap for gap in body["gaps"] if gap["id"] == "enforce_admins"]
                self.assertEqual(len(admins), 1)
                self.assertIn("is unknown", admins[0]["gap"])
                self.assertIn(named, admins[0]["gap"])
                self.assertNotIn("enforce_admins is off", admins[0]["gap"])
        body = json.loads(stored[open_]["body"])
        self.assertIs(body["detail"]["enforce_admins"], False)
        words = next(gap for gap in body["gaps"] if gap["id"] == "enforce_admins")["gap"]
        self.assertIn("enforce_admins is off", words)
        self.assertIn("guard (#7) [pull_request] by OrganizationAdmin 1 (always)", words)
        self.assertIn("Every rule below", words)  # the one ruleset is the only one, so every rule is exempted
        self.assertEqual(body["detail"]["admin_bypass"], ["guard (#7) [pull_request]: OrganizationAdmin 1 (always)"])

    def test_a_bypass_for_one_app_is_its_own_gap_and_administrators_stay_subject(self):
        """`enforce_admins` means administrators are themselves subject to the
        rules. A ruleset that lets one GitHub App bypass it leaves them so;
        reporting that as `enforce_admins is off` is a false security finding
        on a correctly configured repository (sd:1327 review, Codex finding
        2). It is the `bypass` gap, naming the actor and when it applies;
        only `OrganizationAdmin` reaches administrators, and a
        `RepositoryRole` -- a numeric id nothing here resolves to admin or
        not -- is `enforce_admins` unknown, naming the role, in neither
        sentence: a guessed admin id would print "administrators stay
        subject" on a branch they may walk past."""
        app = self.register("app", "https://github.com/platypeeps/app.git", PR_WORKFLOW)
        role = self.register("role", "https://github.com/platypeeps/role.git", PR_WORKFLOW)
        review = {"type": "pull_request", "ruleset_id": 7, "parameters": {"required_approving_review_count": 1}}
        rows = {}
        for name, actors in (("app", [{"actor_type": "Integration", "actor_id": 77, "bypass_mode": "pull_request"},
                                      {"actor_type": "Team", "actor_id": 9, "bypass_mode": "always"}]),
                             ("role", [{"actor_type": "RepositoryRole", "actor_id": 5,
                                        "bypass_mode": "always"}])):
            rows[f"repos/platypeeps/{name}"] = repo_payload()
            rows[f"repos/platypeeps/{name}/branches/{MAIN}/protection"] = Response(404, {}, "{}")
            rows[protection.rules_path("platypeeps", name, MAIN)] = [review]
            rows[f"repos/platypeeps/{name}/rulesets/7"] = {"id": 7, "name": "guard", "bypass_actors": actors}
        protection.sync(self.db, client=self.client(rows)[0], observed_at=AT)
        stored = self.stored()
        body = json.loads(stored[app]["body"])
        self.assertIs(body["detail"]["enforce_admins"], True)
        self.assertEqual([gap["id"] for gap in body["gaps"] if gap["id"] in ("enforce_admins", "bypass")], ["bypass"])
        words = next(gap["gap"] for gap in body["gaps"] if gap["id"] == "bypass")
        self.assertIn("guard (#7) [pull_request] by Integration 77 (pull_request)", words)
        self.assertIn("Team 9 (always)", words)
        self.assertIn("administrators stay subject to guard (#7) [pull_request]", words)
        self.assertEqual(body["detail"]["bypass"],
                         ["guard (#7) [pull_request]: Integration 77 (pull_request)",
                          "guard (#7) [pull_request]: Team 9 (always)"])
        self.assertEqual(body["detail"]["admin_bypass"], [])
        body = json.loads(stored[role]["body"])
        self.assertIsNone(body["detail"]["enforce_admins"])  # unknown, as a withheld list is
        self.assertEqual([gap["id"] for gap in body["gaps"] if gap["id"] in ("enforce_admins", "bypass")],
                         ["enforce_admins"])
        words = next(gap["gap"] for gap in body["gaps"] if gap["id"] == "enforce_admins")
        self.assertIn("guard (#7) [pull_request] lets RepositoryRole 5 (always) bypass it", words)
        self.assertIn("not confirmed here", words)
        self.assertNotIn("exempts the admins", words)
        self.assertNotIn("stay subject", words)
        self.assertEqual(body["detail"]["bypass"], [])

    def test_a_bypass_on_one_ruleset_leaves_the_other_rulesets_rules_binding(self):
        """GitHub layers rulesets: a bypass on the review ruleset exempts
        its holder from the review rule and from nothing the checks ruleset
        requires. Folding every ruleset's bypass into one `enforce_admins`
        boolean said "every rule below ... exempts the admins" of a branch
        whose CI requirement still bound them (sd:1327 review, Codex :730
        and :710). Now the sentence names each exempting ruleset with its
        actor and the rules it reaches, then the rulesets still binding
        administrators, then the ones not known either way; "every rule
        below" is said only when no ruleset is left in either. The boolean
        stays `False` -- administrators are not subject to every rule --
        and `admin_bypass` in the detail carries the same scope."""
        layered = self.register("layered", "https://github.com/platypeeps/layered.git", PR_WORKFLOW)
        exempt = self.register("exempt", "https://github.com/platypeeps/exempt.git", PR_WORKFLOW)
        half = self.register("half", "https://github.com/platypeeps/half.git", PR_WORKFLOW)
        review = {"type": "pull_request", "ruleset_id": 7, "parameters": {"required_approving_review_count": 1}}
        checks = {"type": "required_status_checks", "ruleset_id": 8,
                  "parameters": {"strict_required_status_checks_policy": True,
                                 "required_status_checks": [{"context": "CI Result"}]}}
        admin = {"actor_type": "OrganizationAdmin", "actor_id": 1, "bypass_mode": "always"}
        rows = {}
        for name, actors in (("layered", []), ("exempt", [admin]), ("half", None)):
            rows[f"repos/platypeeps/{name}"] = repo_payload()
            rows[f"repos/platypeeps/{name}/branches/{MAIN}/protection"] = Response(404, {}, "{}")
            rows[protection.rules_path("platypeeps", name, MAIN)] = [review, checks]
            rows[f"repos/platypeeps/{name}/rulesets/7"] = {"id": 7, "name": "review", "bypass_actors": [admin]}
            rows[f"repos/platypeeps/{name}/rulesets/8"] = {"id": 8, "name": "checks", "bypass_actors": actors}
        protection.sync(self.db, client=self.client(rows)[0], observed_at=AT)
        stored = self.stored()
        for path in (layered, exempt, half):
            with self.subTest(path=path):
                self.assertEqual(stored[path]["status"], "protected")
                body = json.loads(stored[path]["body"])
                self.assertIs(body["detail"]["enforce_admins"], False)
                self.assertEqual(body["requests"], 5)  # three basic, one a cited ruleset
                self.assertEqual([gap["id"] for gap in body["gaps"] if gap["id"] in ("enforce_admins", "bypass")],
                                 ["enforce_admins"])
                self.assertEqual(body["detail"]["rulesets"][0]["rules"], ["pull_request"])
                self.assertEqual(body["detail"]["rulesets"][1]["rules"], ["required_status_checks"])
        words = next(gap["gap"] for gap in json.loads(stored[layered]["body"])["gaps"] if gap["id"] == "enforce_admins")
        self.assertIn("enforce_admins is off on main: review (#7) [pull_request] by OrganizationAdmin 1 (always)", words)
        self.assertIn("Still binding them: checks (#8) [required_status_checks].", words)
        self.assertNotIn("very rule below", words)
        self.assertNotIn("checks (#8) [required_status_checks] by", words)
        self.assertEqual(json.loads(stored[layered]["body"])["detail"]["admin_bypass"],
                         ["review (#7) [pull_request]: OrganizationAdmin 1 (always)"])
        words = next(gap["gap"] for gap in json.loads(stored[exempt]["body"])["gaps"] if gap["id"] == "enforce_admins")
        self.assertIn("review (#7) [pull_request] by OrganizationAdmin 1 (always); "
                      "checks (#8) [required_status_checks] by OrganizationAdmin 1 (always). Every rule below", words)
        self.assertNotIn("Still binding", words)
        words = next(gap["gap"] for gap in json.loads(stored[half]["body"])["gaps"] if gap["id"] == "enforce_admins")
        self.assertIn("review (#7) [pull_request] by OrganizationAdmin 1 (always)", words)
        self.assertIn("Not known either way: checks (#8) [required_status_checks]", words)
        self.assertNotIn("Still binding", words)
        self.assertNotIn("very rule below", words)

    def test_a_bypass_lookup_never_takes_the_next_repository_its_observation(self):
        """The reservation is three requests a repository; a gating ruleset
        costs a fourth for its bypass list. Spent in one pass, that fourth
        came out of the last repository's three and filed it `unknown` with
        `budget exhausted` on nothing (sd:1327 review, Codex finding 1). Now
        every repository gets its basic observation first, and the bypass
        lists take what is left: a short budget costs bypass detail, never a
        row. With one more request the bypass is read."""
        review = {"type": "pull_request", "ruleset_id": 7, "parameters": {"required_approving_review_count": 1}}
        rows = {
            "repos/platypeeps/a": repo_payload(),
            f"repos/platypeeps/a/branches/{MAIN}/protection": Response(404, {}, "{}"),
            protection.rules_path("platypeeps", "a", MAIN): [review],
            "repos/platypeeps/a/rulesets/7": {"id": 7, "name": "guard", "bypass_actors": []},
            "repos/platypeeps/b": repo_payload(),
            f"repos/platypeeps/b/branches/{MAIN}/protection": Response(404, {}, "{}"),
            protection.rules_path("platypeeps", "b", MAIN): [],
        }
        first = self.register("a", "https://github.com/platypeeps/a.git", PR_WORKFLOW)
        second = self.register("b", "https://github.com/platypeeps/b.git", PR_WORKFLOW)
        for budget, admins, spent in ((6, None, 3), (7, True, 4)):
            with self.subTest(budget=budget):
                client, transport = self.client(rows, requests=budget)
                summary = protection.sync(self.db, client=client, observed_at=AT)
                stored = self.stored()
                self.assertEqual((stored[second]["status"], stored[second]["reason"]), ("unprotected", None))
                self.assertEqual(json.loads(stored[second]["body"])["requests"], 3)
                self.assertEqual(stored[first]["status"], "protected")
                body = json.loads(stored[first]["body"])
                self.assertIs(body["detail"]["enforce_admins"], admins)
                self.assertEqual(body["requests"], spent)
                self.assertEqual(summary["requests"], budget)
                self.assertEqual(summary["unknown"], 0)
                # The bypass list, when read, is read after both observations.
                self.assertEqual(transport.calls[-1],
                                 "repos/platypeeps/a/rulesets/7" if budget == 7 else protection.rules_path("platypeeps", "b", MAIN))

    def test_rules_that_cannot_be_read_are_unknown_not_absence(self):
        """The rules endpoint answers `[]` for a branch no ruleset touches, so
        a 403 from it is a repository this token could not see into: the row
        is `unknown` with the client's reason, not `unprotected` on the
        classic 404 alone -- for an admin as for anyone."""
        path = self.register("dark", "https://github.com/platypeeps/dark.git", PR_WORKFLOW)
        client, _ = self.client({
            "repos/platypeeps/dark": repo_payload(),
            f"repos/platypeeps/dark/branches/{MAIN}/protection": Response(404, {}, "{}"),
            protection.rules_path("platypeeps", "dark", MAIN): Response(403, {}, "{}"),
        })
        summary = protection.sync(self.db, client=client, observed_at=AT)
        row = self.stored()[path]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["reason"], "API HTTP 403; retry on a later collection")
        self.assertEqual(summary["unprotected"], 0)

    def test_every_page_of_rules_is_read_before_the_404_is_judged(self):
        """Thirty rules fill the endpoint's first page; the rule that gates
        sits on the second. A reader that stopped at page one would file an
        admin's 404 as `unprotected` beside a ruleset that requires a review."""
        path = self.register("deep", "https://github.com/platypeeps/deep.git", PR_WORKFLOW)
        filler = [{"type": "deletion", "ruleset_id": 1}] * protection.RULES_PAGE_SIZE
        gating = {"type": "pull_request", "ruleset_id": 2, "parameters": {"required_approving_review_count": 2}}
        client, transport = self.client({
            "repos/platypeeps/deep": repo_payload(),
            f"repos/platypeeps/deep/branches/{MAIN}/protection": Response(404, {}, "{}"),
            protection.rules_path("platypeeps", "deep", MAIN, 1): filler,
            protection.rules_path("platypeeps", "deep", MAIN, 2): [gating],
            "repos/platypeeps/deep/rulesets/2": {"id": 2, "name": "review", "bypass_actors": []},
        })
        protection.sync(self.db, client=client, observed_at=AT)
        row = self.stored()[path]
        self.assertEqual(row["status"], "protected")
        self.assertEqual(json.loads(row["body"])["detail"]["required_approving_review_count"], 2)
        self.assertIn(protection.rules_path("platypeeps", "deep", MAIN, 2), transport.calls)

    def test_the_approvals_gap_follows_the_policy_review_human(self):
        """Pack sd:8823: the standard sets 0 approvals, and `policy.review.human` (0 when undeclared) is
        the count a repository asks for; only fewer than that is a `reviews` gap."""
        path = self.register("solo", "git@github.com:platypeeps/solo.git", PR_WORKFLOW)
        rows = {"repos/platypeeps/solo": repo_payload(),
                f"repos/platypeeps/solo/branches/{MAIN}/protection": full_protection(
                    required_pull_request_reviews={"required_approving_review_count": 0})}
        for declarations, expected in ((None, []), ('{"policy":{"review":{"human":1}}}', ["reviews"])):
            with self.subTest(declarations=declarations):
                self.db.execute("UPDATE repo SET declarations = ? WHERE path = ?", (declarations, path))
                protection.sync(self.db, client=self.client(rows)[0], observed_at=AT)
                gaps = json.loads(self.stored()[path]["body"])["gaps"]
                self.assertEqual([gap["id"] for gap in gaps if gap["id"] == "reviews"], expected)

    def test_a_second_sync_replaces_the_row_rather_than_adding_one(self):
        path = self.register("one", "git@github.com:platypeeps/one.git", PR_WORKFLOW)
        rows = {"repos/platypeeps/one": repo_payload(),
                f"repos/platypeeps/one/branches/{MAIN}/protection": full_protection()}
        protection.sync(self.db, client=self.client(rows)[0], observed_at=AT)
        rows[f"repos/platypeeps/one/branches/{MAIN}/protection"] = Response(404, {}, "{}")
        rows[protection.rules_path("platypeeps", "one", MAIN)] = []
        protection.sync(self.db, client=self.client(rows)[0], observed_at="2026-09-12T02:45:00Z")
        stored = self.stored()
        self.assertEqual(list(stored), [path])
        self.assertEqual(stored[path]["status"], "unprotected")
        self.assertEqual(stored[path]["observed_at"], "2026-09-12T02:45:00Z")

    def test_a_malformed_body_is_unknown_never_protected(self):
        path = self.register("odd", "git@github.com:platypeeps/odd.git", PR_WORKFLOW)
        client, _ = self.client({"repos/platypeeps/odd": repo_payload(),
                                 f"repos/platypeeps/odd/branches/{MAIN}/protection": Response(200, {}, "not json")})
        protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual(self.stored()[path]["status"], "unknown")
        self.assertIn("not bounded valid JSON", self.stored()[path]["reason"])

    def test_a_well_typed_body_classify_cannot_read_is_unknown_and_sync_goes_on(self):
        """sd:1359. The docstring files a malformed body as unknown; the guard
        in `observe` closed before `classify` ran, so a JSON object carrying
        a `contexts` that is not a list raised through `sync` and took every
        repository after it with it. Through `sync`, as GitHub would answer."""
        odd = self.register("a-odd", "git@github.com:platypeeps/odd.git", PR_WORKFLOW)
        fine = self.register("b-fine", "git@github.com:platypeeps/fine.git", PR_WORKFLOW)
        client, _ = self.client({
            "repos/platypeeps/odd": repo_payload(),
            f"repos/platypeeps/odd/branches/{MAIN}/protection":
                dict(full_protection(), required_status_checks={"strict": True, "contexts": 5}),
            protection.rules_path("platypeeps", "odd", MAIN): [],
            "repos/platypeeps/fine": repo_payload(),
            f"repos/platypeeps/fine/branches/{MAIN}/protection": full_protection(),
            protection.rules_path("platypeeps", "fine", MAIN): [],
        })
        protection.sync(self.db, client=client, observed_at=AT)
        rows = self.stored()
        self.assertEqual(rows[odd]["status"], "unknown")
        self.assertEqual(rows[odd]["reason"], "malformed API response (TypeError)")
        self.assertEqual(rows[fine]["status"], "protected")

    def test_an_enforce_admins_that_is_not_a_boolean_is_unknown_never_enforced(self):
        """Read by truthiness, `enforce_admins: [1]` was "enforced" and the row
        printed no gap: a malformed body presented as safety (sd:1359)."""
        for index, value in enumerate(([1], "no", {"enabled": "yes"})):
            with self.subTest(enforce_admins=value):
                name = f"odd{index}"
                path = self.register(name, f"git@github.com:platypeeps/{name}.git", PR_WORKFLOW)
                client, _ = self.client({
                    f"repos/platypeeps/{name}": repo_payload(),
                    f"repos/platypeeps/{name}/branches/{MAIN}/protection": dict(full_protection(), enforce_admins=value),
                    protection.rules_path("platypeeps", name, MAIN): [],
                })
                protection.sync(self.db, client=client, observed_at=AT)
                self.assertEqual(self.stored()[path]["status"], "unknown")
                self.assertEqual(self.stored()[path]["reason"], "malformed API response (TypeError)")

    def test_a_review_count_that_is_not_a_number_is_unknown(self):
        """sd:1204 213f98f6d416. `int("oops")` raised after the guard closed."""
        path = self.register("odd", "git@github.com:platypeeps/odd.git", PR_WORKFLOW)
        client, _ = self.client({
            "repos/platypeeps/odd": repo_payload(),
            f"repos/platypeeps/odd/branches/{MAIN}/protection": full_protection(
                required_pull_request_reviews={"required_approving_review_count": "oops"}),
            protection.rules_path("platypeeps", "odd", MAIN): [],
        })
        protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual(self.stored()[path]["reason"], "malformed API response (ValueError)")

    def test_a_repository_without_a_default_branch_is_unknown_not_main(self):
        """sd:1204 5d353c81e9b6. A missing `default_branch` was read as `main`,
        and a 200 there stored `protected` for a branch nobody named."""
        for index, value in enumerate((None, "", 7)):
            with self.subTest(default_branch=value):
                name = f"odd{index}"
                path = self.register(name, f"git@github.com:platypeeps/{name}.git", PR_WORKFLOW)
                client, transport = self.client({
                    f"repos/platypeeps/{name}": repo_payload(default_branch=value),
                    f"repos/platypeeps/{name}/branches/{MAIN}/protection": full_protection(),
                    protection.rules_path("platypeeps", name, MAIN): [],
                })
                protection.sync(self.db, client=client, observed_at=AT)
                self.assertEqual(self.stored()[path]["status"], "unknown")
                self.assertEqual(self.stored()[path]["reason"], "repository response has no default branch")
                self.assertEqual([call for call in transport.calls if name in call], [f"repos/platypeeps/{name}"])

    def test_a_default_branch_with_a_slash_is_encoded_in_the_request(self):
        """sd:1204 9042eeb5cf72. `release/2.x` raw in the path is another route,
        and its 404 read as an unprotected branch."""
        path = self.register("rel", "git@github.com:platypeeps/rel.git", PR_WORKFLOW)
        client, transport = self.client({
            "repos/platypeeps/rel": repo_payload(default_branch="release/2.x"),
            "repos/platypeeps/rel/branches/release%2F2.x/protection": full_protection(),
            "repos/platypeeps/rel/rules/branches/release%2F2.x?per_page=30&page=1": [],
        })
        protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual(self.stored()[path]["status"], "protected", transport.calls)

    def test_a_null_or_empty_protection_body_is_unknown_not_a_404(self):
        """sd:1204 8db19e0dd8c9. JSON `null` came back as the 404's None, and
        `{}` passed as protection nobody configured."""
        for index, body in enumerate(("null", "{}")):
            with self.subTest(body=body):
                name = f"odd{index}"
                path = self.register(name, f"git@github.com:platypeeps/{name}.git", PR_WORKFLOW)
                client, _ = self.client({
                    f"repos/platypeeps/{name}": repo_payload(),
                    f"repos/platypeeps/{name}/branches/{MAIN}/protection": Response(200, {}, body),
                    protection.rules_path("platypeeps", name, MAIN): [],
                })
                protection.sync(self.db, client=client, observed_at=AT)
                self.assertEqual(self.stored()[path]["status"], "unknown")

    def test_a_budget_that_runs_out_mid_fleet_leaves_the_rest_unknown_and_does_not_raise(self):
        first = self.register("a-first", "git@github.com:platypeeps/first.git", PR_WORKFLOW)
        second = self.register("b-second", "git@github.com:platypeeps/second.git", PR_WORKFLOW)
        third = self.register("c-third", "git@github.com:platypeeps/third.git", PR_WORKFLOW)
        client, transport = self.client({
            "repos/platypeeps/first": repo_payload(),
            f"repos/platypeeps/first/branches/{MAIN}/protection": full_protection(),
            protection.rules_path("platypeeps", "first", MAIN): [],
            "repos/platypeeps/second": repo_payload(),
            f"repos/platypeeps/second/branches/{MAIN}/protection": full_protection(),
            "repos/platypeeps/third": repo_payload(),
            f"repos/platypeeps/third/branches/{MAIN}/protection": full_protection(),
        }, requests=4)
        summary = protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual((summary["protected"], summary["unknown"], summary["requests"]), (1, 2, 4))
        rows = self.stored()
        self.assertEqual(rows[first]["status"], "protected")
        self.assertEqual(rows[second]["status"], "unknown")
        self.assertEqual(rows[second]["reason"], protection.REQUESTS_EXHAUSTED)
        self.assertEqual(rows[second]["default_branch"], MAIN)
        self.assertEqual(rows[third]["status"], "unknown")
        self.assertEqual(rows[third]["reason"], protection.REQUESTS_EXHAUSTED)
        self.assertEqual(len(transport.calls), 4)

    def fleet(self, names, *, managed=()):
        """Registered repositories that each answer protected in three GETs."""
        rows, paths = {}, {}
        for name in names:
            paths[name] = self.register(name, f"git@github.com:platypeeps/{name}.git", PR_WORKFLOW)
            if name in managed:
                # The column `repo managed PATH yes` sets; the temporary home
                # sits under /var, which `set_managed` resolves to /private/var.
                with self.db:
                    self.db.execute("UPDATE repo SET managed = 1 WHERE path = ?", (paths[name],))
            rows[f"repos/platypeeps/{name}"] = repo_payload()
            rows[f"repos/platypeeps/{name}/branches/{MAIN}/protection"] = full_protection()
            rows[protection.rules_path("platypeeps", name, MAIN)] = []
        return rows, paths

    def test_a_sweep_cut_short_resumes_where_it_stopped_and_keeps_what_it_observed(self):
        """sd:1663: a sweep the budget cut short rewrote every repository it
        did not reach as `budget exhausted`, observed ones included, and the
        next sweep began at the same first path, so the tail of the fleet
        was never reached. Now the repositories not reached go first next
        time, and a repository not reached keeps its earlier observation."""
        rows, paths = self.fleet(["a", "b", "c"])
        client, _ = self.client(rows, requests=6)
        protection.sync(self.db, client=client, observed_at=AT)
        stored = self.stored()
        self.assertEqual([stored[paths[n]]["status"] for n in "abc"], ["protected", "protected", "unknown"])
        self.assertEqual(stored[paths["c"]]["reason"], protection.REQUESTS_EXHAUSTED)

        later = "2026-09-12T02:45:00Z"
        client, transport = self.client(rows, requests=3)
        summary = protection.sync(self.db, client=client, observed_at=later)
        self.assertEqual(transport.calls[0], "repos/platypeeps/c")
        stored = self.stored()
        self.assertEqual((stored[paths["c"]]["status"], stored[paths["c"]]["observed_at"]), ("protected", later))
        for name in "ab":
            with self.subTest(name=name):
                self.assertEqual((stored[paths[name]]["status"], stored[paths[name]]["observed_at"],
                                  stored[paths[name]]["reason"]), ("protected", AT, None))
        self.assertEqual((summary["protected"], summary["unknown"]), (3, 0))
        self.assertEqual(summary["errors"], [
            f"platypeeps/a: {protection.REQUESTS_EXHAUSTED}; kept the observation of {AT}",
            f"platypeeps/b: {protection.REQUESTS_EXHAUSTED}; kept the observation of {AT}"])

        # The oldest observation goes next.
        client, transport = self.client(rows, requests=3)
        protection.sync(self.db, client=client, observed_at="2026-09-13T02:45:00Z")
        self.assertEqual(transport.calls[0], "repos/platypeeps/a")

    def test_managed_repositories_are_swept_first(self):
        rows, paths = self.fleet(["a", "b", "m"], managed=("m",))
        client, transport = self.client(rows, requests=3)
        protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual(transport.calls[0], "repos/platypeeps/m")
        self.assertEqual(self.stored()[paths["m"]]["status"], "protected")

    def test_a_deadline_is_named_as_the_limit_that_stopped_the_sweep(self):
        rows, paths = self.fleet(["a"])
        client, transport = self.client(rows, requests=100, seconds=-1)
        summary = protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.stored()[paths["a"]]["reason"], protection.TIME_EXHAUSTED)
        self.assertEqual(summary["errors"], [f"platypeeps/a: {protection.TIME_EXHAUSTED}"])

    def test_reserve_seconds_covers_the_fleet_and_never_more_than_half_the_run(self):
        self.fleet([f"r{n}" for n in range(19)])
        self.register("local", "/srv/git/local.git")
        self.assertEqual(protection.reserve(self.db), 19 * 4)
        self.assertEqual(protection.reserve_seconds(self.db, 600), 19 * protection.SECONDS_PER_REPO)
        self.assertEqual(protection.reserve_seconds(self.db, 60), 30)

    def test_an_absent_checkout_is_still_observed_without_the_comparison_gaps(self):
        gone = str(self.home / "gone")
        upsert_repo(self.db, gone, remote="git@github.com:platypeeps/gone.git")
        client, _ = self.client({"repos/platypeeps/gone": repo_payload(),
                                 f"repos/platypeeps/gone/branches/{MAIN}/protection":
                                 full_protection(required_status_checks={"strict": True, "contexts": ["Other"]})})
        protection.sync(self.db, client=client, observed_at=AT)
        body = json.loads(self.stored()[gone]["body"])
        self.assertEqual([gap["id"] for gap in body["gaps"]], [])
        self.assertIn("absent on this machine", body["detail"]["workflow_notes"][0])


INTEGRATION = {"actor_id": 77, "actor_type": "Integration", "bypass_mode": "pull_request"}
ORG_ADMIN = {"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}
RULESET_42 = {"id": 42, "name": "main", "enforcement": "active", "bypass_actors": []}
BOTH_RULES = "main (#42) [pull_request, required_status_checks]"


def gating_rules(approvals=1, strict=True, checks=True):
    """Ruleset 42's merge-gating rules: a review and, unless `checks` is
    false, the `CI Result` check -- the same asks as `full_protection`."""
    rules = [{"type": "pull_request", "ruleset_id": 42,
              "parameters": {"required_approving_review_count": approvals}},
             {"type": "deletion", "ruleset_id": 42}]
    if checks:
        rules.append({"type": "required_status_checks", "ruleset_id": 42,
                      "parameters": {"strict_required_status_checks_policy": strict,
                                     "required_status_checks": [{"context": "CI Result"}]}})
    return rules


class ClassicAndRulesets(SyncCase):
    """Classic protection answers 200 *and* a ruleset gates the branch.

    Before sd:1430 a 200 was the whole answer and the rulesets were read
    only after a 404, so a ruleset beside classic protection was invisible
    to the fleet screen. They are read beside it now and layered the way
    GitHub layers them, strictest per rule, under the operator's decisions
    of 2026-09-24 that the pack's sd:1419 implements: a bypass is a finding
    only when it removes a rule's last firm source (Q2), a stricter source
    a bypass can skip is advisory (Q3), administrators are enforced on a
    rule when any source imposing it binds them (Q1), and more than one
    source reads `source: combined` with a `sources` map (Q4).
    """

    def observe(self, name, classic, rules, ruleset=RULESET_42, **client):
        path = self.register(name, f"https://github.com/platypeeps/{name}.git", PR_WORKFLOW)
        rows = {f"repos/platypeeps/{name}": repo_payload(),
                f"repos/platypeeps/{name}/branches/{MAIN}/protection": classic,
                protection.rules_path("platypeeps", name, MAIN): rules,
                f"repos/platypeeps/{name}/rulesets/42": ruleset}
        client, transport = self.client(rows, **client)
        protection.sync(self.db, client=client, observed_at=AT)
        row = self.stored()[path]
        return row, json.loads(row["body"]), transport

    def test_classic_alone_reports_exactly_what_it_did(self):
        for name, rules in (("bare", []), ("moves", [{"type": "deletion", "ruleset_id": 42}])):
            with self.subTest(rules=rules):
                row, body, transport = self.observe(name, full_protection(), rules)
                self.assertEqual(row["status"], "protected")
                self.assertEqual(body["gaps"], [])
                for key in ("source", "sources", "rulesets", "bypass", "bypass_info", "advisory",
                            "rules_read_error"):
                    self.assertNotIn(key, body["detail"])
                # The rules are read whatever classic answered: three GETs.
                self.assertEqual(body["requests"], 3)
                self.assertIn(protection.rules_path("platypeeps", name, MAIN), transport.calls)

    def test_the_same_rules_in_both_are_one_combined_requirement(self):
        row, body, _ = self.observe("same", full_protection(), gating_rules())
        self.assertEqual(row["status"], "protected")
        self.assertEqual(body["gaps"], [])
        detail = body["detail"]
        self.assertEqual(detail["source"], "combined")
        self.assertEqual(detail["sources"], {"pull_request": ["classic", "ruleset:42"],
                                             "required_status_checks": ["classic", "ruleset:42"]})
        self.assertIs(detail["enforce_admins"], True)
        self.assertEqual((detail["required_contexts"], detail["strict"], detail["required_approving_review_count"]),
                         (["CI Result"], True, 1))
        self.assertEqual((detail["advisory"], detail["bypass_info"], detail["bypass"]), ([], [], []))
        self.assertEqual(body["requests"], 4)  # three basic, one the cited ruleset

    def test_a_ruleset_bypass_beside_firm_classic_is_information_not_the_bypass_gap(self):
        """Classic enforces both rules on everyone, so the app that walks past
        the ruleset walks into classic: nothing it removes is the last
        enforcement. A bypass list GitHub did not show is the same."""
        for name, ruleset, words in (
                ("app", dict(RULESET_42, bypass_actors=[INTEGRATION]), f"{BOTH_RULES}: Integration 77 (pull_request)"),
                ("hidden", {"id": 42, "name": "main", "enforcement": "active"}, f"{BOTH_RULES}: bypass_actors not shown")):
            with self.subTest(name=name):
                _, body, _ = self.observe(name, full_protection(), gating_rules(), ruleset)
                self.assertEqual(body["gaps"], [])
                self.assertEqual(body["detail"]["bypass"], [])
                self.assertEqual(body["detail"]["bypass_info"], [words])
                self.assertIs(body["detail"]["enforce_admins"], True)

    def test_a_ruleset_bypass_that_removes_the_last_enforcement_is_the_bypass_gap(self):
        """Classic requires reviews only; the checks come from the ruleset
        alone, and an app can bypass it: the checks rule has no firm source."""
        classic = full_protection()
        del classic["required_status_checks"]
        _, body, _ = self.observe("decisive", classic, gating_rules(), dict(RULESET_42, bypass_actors=[INTEGRATION]))
        self.assertEqual(body["detail"]["source"], "combined")
        self.assertEqual(body["detail"]["bypass"], [f"{BOTH_RULES}: Integration 77 (pull_request)"])
        self.assertEqual(body["detail"]["bypass_info"], [])
        self.assertEqual(ids(body), ["bypass"])

    def test_a_stricter_bypassable_ruleset_is_advisory_and_leaves_the_gap_open(self):
        """The ruleset asks for strict checks and two approvals, and an app can
        bypass it; classic asks for lax checks and one approval. The
        requirement is classic's, so `strict` stays a gap, and the stricter
        asks are named as advisory. Firm, the same ruleset is the requirement."""
        lax = full_protection(required_status_checks={"strict": False, "contexts": ["CI Result"]})
        _, body, _ = self.observe("lax", lax, gating_rules(approvals=2), dict(RULESET_42, bypass_actors=[INTEGRATION]))
        self.assertEqual(ids(body), ["strict"])
        self.assertIs(body["detail"]["strict"], False)
        self.assertEqual(body["detail"]["required_approving_review_count"], 1)
        self.assertEqual(len(body["detail"]["advisory"]), 2)
        self.assertIn("main (#42) [pull_request] asks for more than the firm sources", body["detail"]["advisory"][0])
        self.assertIn("main (#42) [required_status_checks] asks for more than the firm sources",
                      body["detail"]["advisory"][1])
        _, firm, _ = self.observe("firm", lax, gating_rules(approvals=2))
        self.assertEqual(ids(firm), [])
        self.assertIs(firm["detail"]["strict"], True)
        self.assertEqual(firm["detail"]["required_approving_review_count"], 2)
        self.assertEqual(firm["detail"]["advisory"], [])

    def test_admins_exempt_on_classic_are_enforced_when_a_firm_ruleset_carries_every_rule(self):
        exempt = full_protection(enforce_admins={"enabled": False})
        _, body, _ = self.observe("covered", exempt, gating_rules())
        self.assertIs(body["detail"]["enforce_admins"], True)
        self.assertEqual(ids(body), [])
        _, body, _ = self.observe("partial", exempt, gating_rules(checks=False))
        self.assertIs(body["detail"]["enforce_admins"], False)
        words = next(gap["gap"] for gap in body["gaps"] if gap["id"] == "enforce_admins")
        self.assertIn("enforce_admins is off on main for required_status_checks", words)
        self.assertIn("classic protection (enforce_admins off)", words)
        self.assertIn("Still binding them: pull_request.", words)

    def test_an_admin_bypass_beside_classic_enforcing_admins_is_information(self):
        _, body, _ = self.observe("orgadmin", full_protection(), gating_rules(),
                                  dict(RULESET_42, bypass_actors=[ORG_ADMIN]))
        self.assertIs(body["detail"]["enforce_admins"], True)
        self.assertEqual(body["detail"]["admin_bypass"], [])
        self.assertEqual(body["detail"]["bypass_info"], [f"{BOTH_RULES}: OrganizationAdmin 1 (always)"])
        self.assertEqual(ids(body), [])
        # Both sources exempting administrators is enforce_admins off.
        _, body, _ = self.observe("both-off", full_protection(enforce_admins={"enabled": False}), gating_rules(),
                                  dict(RULESET_42, bypass_actors=[ORG_ADMIN]))
        self.assertIs(body["detail"]["enforce_admins"], False)
        self.assertEqual(body["detail"]["admin_bypass"], [f"{BOTH_RULES}: OrganizationAdmin 1 (always)"])
        self.assertEqual(ids(body), ["enforce_admins"])

    def test_a_rules_read_that_fails_keeps_the_classic_result_and_names_the_fault(self):
        row, body, _ = self.observe("dark", full_protection(), Response(403, {}, "{}"))
        self.assertEqual((row["status"], row["reason"]), ("protected", None))
        self.assertEqual(body["gaps"], [])
        self.assertNotIn("source", body["detail"])
        self.assertEqual(body["detail"]["rules_read_error"], "API HTTP 403; retry on a later collection")


#: The fleet baseline's ruleset (sd:1741, design "Target rulesets"): the four
#: rules, `ci` pinned to GitHub Actions, strict, no bypass.
CI_RULES = [
    {"type": "deletion", "ruleset_id": 42},
    {"type": "non_fast_forward", "ruleset_id": 42},
    {"type": "pull_request", "ruleset_id": 42, "parameters": {"required_approving_review_count": 0}},
    {"type": "required_status_checks", "ruleset_id": 42,
     "parameters": {"strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": "ci", "integration_id": 15368}]}},
]
#: The step of the design's `ci` template: fail unless every need succeeded.
PROPAGATE = ("    steps:\n      - name: Every needed job succeeded\n        env:\n"
             "          RESULTS: ${{ join(needs.*.result, ' ') }}\n        run: |\n"
             "          for result in $RESULTS; do\n"
             "            [ \"$result\" = success ] || { echo \"a needed job ended: $result\"; exit 1; }\n"
             "          done\n")
#: An aggregate `ci` that needs every other job in its file.
CI_WORKFLOW = ("on: [pull_request]\njobs:\n  lint:\n    runs-on: x\n"
               "  unittest:\n    strategy:\n      matrix:\n        py: ['3.14']\n    runs-on: x\n"
               "  ci:\n    name: ci\n    needs: [lint, unittest]\n    if: ${{ !cancelled() }}\n    runs-on: x\n"
               + PROPAGATE)
NO_CLASSIC = Response(404, {}, '{"message": "Branch not protected"}')
#: The same ruleset for a `repo.ci = local` repository (sd:1992): the local
#: gate's status, which no app id pins.
LOCAL_RULES = CI_RULES[:3] + [
    {"type": "required_status_checks", "ruleset_id": 42,
     "parameters": {"strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": "sd/local-gate"}]}},
]


class Baseline(SyncCase):
    """The two fleet-baseline flags and the `needs` closure (sd:1741, S1 and S2)."""

    def observe(self, name, classic, rules, *, owner="platypeeps", workflow=CI_WORKFLOW, ci=None):
        path = self.register(name, f"https://github.com/{owner}/{name}.git", workflow, ci=ci)
        rows = {f"repos/{owner}/{name}": repo_payload(owner={"login": owner}),
                f"repos/{owner}/{name}/branches/{MAIN}/protection": classic,
                protection.rules_path(owner, name, MAIN): rules,
                f"repos/{owner}/{name}/rulesets/42": RULESET_42}
        client, _ = self.client(rows)
        protection.sync(self.db, client=client, observed_at=AT)
        row = self.stored()[path]
        body = json.loads(row["body"])
        return row, body, {flag["id"]: flag for flag in body["merge_settings"]}

    def test_a_classic_only_branch_requiring_ci_clears_both_flags(self):
        """sd:5406: the fleet baseline is classic protection and no repository ruleset."""
        classic = full_protection(required_status_checks={"strict": True, "contexts": ["ci"]})
        row, body, flags = self.observe("target", classic, [])
        self.assertEqual(row["status"], "protected")
        self.assertNotIn("source", body["detail"])
        self.assertEqual((flags["protection_source"]["flagged"], flags["protection_source"]["value"]),
                         (False, "classic"))
        self.assertEqual((flags["required_check"]["flagged"], flags["required_check"]["value"]), (False, "ci"))

    def test_ruleset_and_combined_protection_flag_the_source(self):
        classic = full_protection(required_status_checks={"strict": True, "contexts": ["ci"]})
        for name, branch, source in (("ruleset", NO_CLASSIC, "ruleset"), ("combined", classic, "combined")):
            with self.subTest(source=source):
                _, _, flags = self.observe(name, branch, CI_RULES)
                self.assertTrue(flags["protection_source"]["flagged"])
                self.assertEqual(flags["protection_source"]["value"], source)
                self.assertIn("classic protection and no repository ruleset", flags["protection_source"]["gap"])
                self.assertFalse(flags["required_check"]["flagged"])

    def test_classic_beside_a_ruleset_that_gates_nothing_flags_the_source(self):
        """`_combine` keeps the classic object when no rule gates a merge, so
        `source` reads classic; the active ruleset still stands beside it."""
        classic = full_protection(required_status_checks={"strict": True, "contexts": ["ci"]})
        rules = [{"type": "deletion", "ruleset_id": 42}, {"type": "non_fast_forward", "ruleset_id": 42}]
        _, body, flags = self.observe("beside", classic, rules)
        self.assertNotIn("source", body["detail"])
        self.assertTrue(flags["protection_source"]["flagged"])
        self.assertEqual(flags["protection_source"]["value"], "classic, beside a ruleset")

    def test_classic_with_a_failed_rules_read_flags_the_source(self):
        """A rules read that failed is no evidence that no ruleset stands: not a pass."""
        classic = full_protection(required_status_checks={"strict": True, "contexts": ["ci"]})
        _, body, flags = self.observe("dark", classic, Response(403, {}, "{}"))
        self.assertIn("rules_read_error", body["detail"])
        self.assertTrue(flags["protection_source"]["flagged"])
        self.assertEqual(flags["protection_source"]["value"], "classic, rulesets unread")
        self.assertIn("unknown", flags["protection_source"]["gap"])

    def test_a_classic_object_that_gates_nothing_still_flags_the_source(self):
        """`_combine` reads such a branch as `ruleset`, and a ruleset is not the baseline."""
        _, body, flags = self.observe("leftover", {"url": "x", "allow_deletions": {"enabled": False}}, CI_RULES)
        self.assertEqual(body["detail"]["source"], "ruleset")
        self.assertTrue(flags["protection_source"]["flagged"])
        self.assertEqual(flags["protection_source"]["value"], "ruleset, beside a classic object")

    def test_required_ci_result_flags_the_check_and_ci_beside_route_does_not(self):
        """Decision D5: `ci` present, not sole."""
        result = [dict(rule) for rule in CI_RULES]
        result[3] = {"type": "required_status_checks", "ruleset_id": 42,
                     "parameters": {"strict_required_status_checks_policy": True,
                                    "required_status_checks": [{"context": "CI Result"}]}}
        _, _, flags = self.observe("aggregate", NO_CLASSIC, result)
        self.assertTrue(flags["required_check"]["flagged"])
        self.assertEqual(flags["required_check"]["value"], "CI Result")
        both = [dict(rule) for rule in CI_RULES]
        both[3] = {"type": "required_status_checks", "ruleset_id": 42,
                   "parameters": {"strict_required_status_checks_policy": False,
                                  "required_status_checks": [{"context": "ci"}, {"context": "route"}]}}
        _, _, flags = self.observe("system", NO_CLASSIC, both)
        self.assertFalse(flags["required_check"]["flagged"])
        self.assertEqual(flags["required_check"]["value"], "ci, route")

    def test_an_unprotected_branch_flags_both(self):
        _, _, flags = self.observe("bare", NO_CLASSIC, [])
        self.assertEqual({key: (flag["flagged"], flag["value"]) for key, flag in flags.items()
                          if key in protection.BASELINE_FLAG_IDS},
                         {"protection_source": (True, "none"), "required_check": (True, "none")})

    def test_another_owners_repository_carries_neither_flag(self):
        """Decision D1: the example-corp repositories are an exception."""
        _, _, flags = self.observe("corp_benchmark", full_protection(), [], owner="example-corp")
        self.assertEqual(sorted(flags), ["rebase_merge", "squash_message"])

    def test_the_repository_objects_owner_wins_over_the_remotes(self):
        result = protection.classify(full_protection(), repo_payload(owner={"login": "example-corp"}), MAIN,
                                     {"CI Result"}, [], owner="platypeeps")
        self.assertEqual([flag["id"] for flag in result["merge_settings"]], ["squash_message", "rebase_merge"])
        owned = protection.classify(full_protection(), repo_payload(), MAIN, {"CI Result"}, [], owner="Platypeeps")
        self.assertEqual([flag["id"] for flag in owned["merge_settings"]],
                         ["squash_message", "rebase_merge", "protection_source", "required_check"])

    def test_an_aggregate_whose_needs_cover_every_job_reports_no_produced_not_required(self):
        _, body, _ = self.observe("covered", NO_CLASSIC, CI_RULES)
        self.assertEqual(body["detail"]["produced_contexts"], ["ci", "lint", "unittest (3.14)"])
        self.assertEqual(body["detail"]["produced_not_required"], [])
        self.assertNotIn("produced_not_required", [gap["id"] for gap in body["gaps"]])

    def test_a_job_outside_the_closure_still_reports_it(self):
        workflow = CI_WORKFLOW + "  docs:\n    runs-on: x\n"
        _, body, _ = self.observe("outside", NO_CLASSIC, CI_RULES, workflow=workflow)
        self.assertEqual(body["detail"]["produced_not_required"], ["docs"])

    def test_a_local_mode_repository_requiring_the_local_gate_is_not_flagged(self):
        """sd:1992. With `repo.ci = local` nothing posts `ci`; `sd-ship merge`
        posts `sd/local-gate`, so that is the check the baseline asks for,
        and the one context the repository produces. Its workflow files do
        not run, so their jobs are neither produced nor unrequired."""
        _, body, flags = self.observe("gated", NO_CLASSIC, LOCAL_RULES, ci="local")
        self.assertEqual((flags["required_check"]["flagged"], flags["required_check"]["value"]),
                         (False, "sd/local-gate"))
        self.assertEqual(body["detail"]["produced_contexts"], ["sd/local-gate"])
        self.assertEqual(body["detail"]["required_not_produced"], [])
        self.assertEqual(body["detail"]["produced_not_required"], [])
        # No policy asks for an approval, so its 0 is no `reviews` gap (pack sd:8823).
        self.assertEqual(ids(body), [])
        self.assertIn("repo.ci is local", body["detail"]["workflow_notes"][-1])

    def test_a_local_mode_repository_requiring_ci_is_flagged_and_named_the_local_gate(self):
        """Nothing posts `ci` while Actions is off, so requiring it blocks
        every merge; the flag names the check that would clear it."""
        _, body, flags = self.observe("stale", NO_CLASSIC, CI_RULES, ci="local")
        self.assertTrue(flags["required_check"]["flagged"])
        self.assertIn("`sd/local-gate` is not a required check", flags["required_check"]["gap"])
        self.assertEqual(body["detail"]["required_not_produced"], ["ci"])

    def test_a_github_mode_repository_requiring_the_local_gate_is_still_flagged(self):
        _, _, flags = self.observe("actions", NO_CLASSIC, LOCAL_RULES, ci="github")
        self.assertTrue(flags["required_check"]["flagged"])
        self.assertIn("`ci` is not a required check", flags["required_check"]["gap"])


class NeedsClosure(unittest.TestCase):
    """`produced_contexts` records which jobs reach each name through `needs`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".github" / "workflows").mkdir(parents=True)

    def produced(self, **files):
        for name, text in files.items():
            (self.root / ".github" / "workflows" / f"{name}.yml").write_text(text, encoding="utf-8")
        return protection.produced_contexts(self.root)[0]

    def classify(self, produced, notes=()):
        return protection.classify(full_protection(required_status_checks={"strict": True, "contexts": ["ci"]}),
                                   repo_payload(), MAIN, produced, list(notes))

    def test_scalar_block_and_transitive_needs_and_no_reach_across_files(self):
        produced = self.produced(
            a="on: [pull_request]\njobs:\n  build:\n    runs-on: x\n"
              "  test:\n    needs: build\n    if: always()\n    runs-on: x\n" + PROPAGATE +
              "  gate:\n    name: ci\n    needs:\n      - test\n    if: ${{ !cancelled() }}\n    runs-on: x\n"
              + PROPAGATE,
            b="on: [pull_request]\njobs:\n  other:\n    runs-on: x\n")
        self.assertEqual(produced.needed_by, {"build": {"test", "ci"}, "test": {"ci"}})
        self.assertEqual(self.classify(produced)["detail"]["produced_not_required"], ["other"])

    def test_a_job_that_only_needs_another_does_not_gate_it(self):
        """sd:1741 review: `ci: needs: test` skips when `test` fails, and a
        skipped required check passes, so `test` is not gated by `ci`. Nor
        is a job that runs after the failure but never reads the result."""
        for name, extra in (("plain", ""), ("ignores", "    if: ${{ !cancelled() }}\n    steps:\n      - run: 'true'\n")):
            with self.subTest(name=name):
                (self.root / ".github" / "workflows" / "a.yml").write_text(
                    "on: [pull_request]\njobs:\n  test:\n    runs-on: x\n"
                    "  ci:\n    needs: test\n    runs-on: x\n" + extra, encoding="utf-8")
                produced, notes = protection.produced_contexts(self.root)
                self.assertEqual(produced.needed_by, {})
                self.assertEqual(self.classify(produced, notes)["detail"]["produced_not_required"], ["test"])
                self.assertIn("a.yml: job ci needs test but is not the aggregate", " ".join(notes))

    def test_a_per_need_result_check_is_not_the_aggregate(self):
        """sd:1741 review rounds 2 and 6: a step that checks `needs.lint.result`
        says nothing of `test`, and no textual check is taken as proof."""
        (self.root / ".github" / "workflows" / "a.yml").write_text(
            "on: [pull_request]\njobs:\n  lint:\n    runs-on: x\n  test:\n    runs-on: x\n"
            "  ci:\n    needs: [lint, test]\n    if: always()\n    runs-on: x\n"
            "    steps:\n      - run: test ${{ needs.lint.result }} = success || exit 1\n", encoding="utf-8")
        produced, notes = protection.produced_contexts(self.root)
        self.assertEqual(produced.needed_by, {})
        self.assertEqual(self.classify(produced, notes)["detail"]["produced_not_required"], ["lint", "test"])
        self.assertIn("a.yml: job ci needs lint, test but is not the aggregate", " ".join(notes))

    def test_a_condition_that_skips_on_the_failure_does_not_gate(self):
        """sd:1741 review round 3: `always() && needs.test.result == 'success'`
        skips `ci` when `test` fails. Only a whole `!cancelled()` or
        `always()` runs it, and a step's `if:` reading a result proves nothing."""
        step_if = PROPAGATE.replace("      - name:", "      - if: needs.test.result == 'failure'\n        name:")
        for name, condition, steps in (
                ("conjunction", "${{ always() && needs.test.result == 'success' }}", PROPAGATE),
                ("failure", "failure()", PROPAGATE),
                ("step-if", "${{ !cancelled() }}", step_if)):
            with self.subTest(name=name):
                (self.root / ".github" / "workflows" / "a.yml").write_text(
                    "on: [pull_request]\njobs:\n  test:\n    runs-on: x\n"
                    f"  ci:\n    needs: test\n    if: {condition}\n    runs-on: x\n" + steps, encoding="utf-8")
                produced, notes = protection.produced_contexts(self.root)
                self.assertEqual(produced.needed_by, {})
                self.assertEqual(self.classify(produced, notes)["detail"]["produced_not_required"], ["test"])
        for condition in ("always()", "'${{ always() }}'", "${{ ! cancelled() }}"):
            with self.subTest(condition=condition):
                (self.root / ".github" / "workflows" / "a.yml").write_text(
                    "on: [pull_request]\njobs:\n  test:\n    runs-on: x\n"
                    f"  ci:\n    needs: test\n    if: {condition}\n    runs-on: x\n" + PROPAGATE, encoding="utf-8")
                self.assertEqual(protection.produced_contexts(self.root)[0].needed_by, {"test": {"ci"}})

    def test_a_job_with_no_step_that_can_fail_does_not_gate(self):
        """sd:1741 review round 4: `echo ${{ needs.test.result }}` reads the
        result and passes whatever it prints."""
        produced = self.produced(a="on: [pull_request]\njobs:\n  test:\n    runs-on: x\n"
                                   "  ci:\n    needs: test\n    if: always()\n    runs-on: x\n"
                                   "    steps:\n      - run: echo ${{ needs.test.result }}\n")
        self.assertEqual(produced.needed_by, {})
        self.assertEqual(self.classify(produced)["detail"]["produced_not_required"], ["test"])

    def test_continue_on_error_gates_nothing(self):
        """sd:1741 review round 5: a tolerated failure passes the job."""
        for name, job_level, step_level in (("step", "", "        continue-on-error: true\n"),
                                            ("job", "    continue-on-error: ${{ true }}\n", "")):
            with self.subTest(name=name):
                (self.root / ".github" / "workflows" / "a.yml").write_text(
                    "on: [pull_request]\njobs:\n  test:\n    runs-on: x\n"
                    "  ci:\n    needs: test\n    if: always()\n    runs-on: x\n" + job_level
                    + PROPAGATE + step_level, encoding="utf-8")
                self.assertEqual(protection.produced_contexts(self.root)[0].needed_by, {})
        (self.root / ".github" / "workflows" / "a.yml").write_text(
            "on: [pull_request]\njobs:\n  test:\n    runs-on: x\n"
            "  ci:\n    needs: test\n    if: always()\n    runs-on: x\n" + PROPAGATE
            + "        continue-on-error: false\n", encoding="utf-8")
        self.assertEqual(protection.produced_contexts(self.root)[0].needed_by, {"test": {"ci"}})

    def test_only_the_design_aggregate_gates(self):
        """sd:1741 review round 6: `echo "${{ needs.test.result }}; exit 1"`
        shows a result read and an exit, and passes. Textual signs are not
        proof; only the aggregate `design.md` prescribes is credited."""
        head = "on: [pull_request]\njobs:\n  test:\n    runs-on: x\n  ci:\n    needs: test\n    if: always()\n    runs-on: x\n"
        loop = '[ "$result" = success ] || { echo "a needed job ended: $result"; exit 1; }'
        cases = {
            "echoed exit": head + '    steps:\n      - run: echo "${{ needs.test.result }}; exit 1"\n',
            "loop that exits 0": head + PROPAGATE.replace("exit 1;", "exit 0;"),
            "loop then true": head + PROPAGATE + "          true\n",
            "one-line run": head + PROPAGATE.replace("run: |", "run: exit 1").replace(loop, "echo"),
            "another shell": head + PROPAGATE + "        shell: python\n",
            "another step": head + PROPAGATE + "      - uses: actions/checkout@v4\n",
            "one need's result": head + PROPAGATE.replace("needs.*.result", "needs.test.result"),
            "workflow defaults": "defaults:\n  run:\n    shell: python\n" + head + PROPAGATE,
            # Round 7: the join at job level, overridden in the step.
            "overridden in the step": head + "    env:\n      RESULTS: ${{ join(needs.*.result, ' ') }}\n"
            + PROPAGATE.replace("RESULTS: ${{ join(needs.*.result, ' ') }}", "RESULTS: success"),
            "join at job level only": head + "    env:\n      RESULTS: ${{ join(needs.*.result, ' ') }}\n"
            + PROPAGATE.replace("        env:\n          RESULTS: ${{ join(needs.*.result, ' ') }}\n", ""),
        }
        for name, workflow in cases.items():
            with self.subTest(name=name):
                (self.root / ".github" / "workflows" / "a.yml").write_text(workflow, encoding="utf-8")
                produced, notes = protection.produced_contexts(self.root)
                self.assertEqual(produced.needed_by, {})
                self.assertEqual(self.classify(produced, notes)["detail"]["produced_not_required"], ["test"])
        (self.root / ".github" / "workflows" / "a.yml").write_text(head + PROPAGATE, encoding="utf-8")
        self.assertEqual(protection.produced_contexts(self.root)[0].needed_by, {"test": {"ci"}})

    def test_only_plain_workflow_keys_leave_the_aggregate_gating(self):
        """sd:1741 extra review pass on dd81109: a quoted `"defaults":` sets
        every step's shell, and the `defaults:` line match missed it. A
        workflow's top-level keys must each be a known key, plain or quoted."""
        head = "jobs:\n  test:\n    runs-on: x\n  ci:\n    needs: test\n    if: always()\n    runs-on: x\n"
        shell = "\n  run:\n    shell: bash -n {0}\n"
        cases = {
            "double-quoted defaults": '"defaults":' + shell,
            "single-quoted defaults": "'defaults':" + shell,
            "explicit key": "? defaults\n:" + shell,
            "merge key": "<<: *base\n",
            "unknown key": "x-defaults:" + shell,
            # The re-review of 923fdcf: the whole workflow indented two spaces.
            "indented workflow": "  defaults:" + shell.replace("\n", "\n  ").rstrip(" "),
        }
        for name, prefix in cases.items():
            with self.subTest(name=name):
                text = "on: [pull_request]\n" + prefix + head + PROPAGATE
                if name == "indented workflow":
                    text = "".join("  " + line for line in ("on: [pull_request]\n" + head + PROPAGATE)
                                   .splitlines(keepends=True))
                    text = prefix + text
                (self.root / ".github" / "workflows" / "a.yml").write_text(text, encoding="utf-8")
                produced, notes = protection.produced_contexts(self.root)
                self.assertEqual(produced.needed_by, {})
                self.assertEqual(self.classify(produced, notes)["detail"]["produced_not_required"], ["test"])
        plain = ("---\nname: CI\nrun-name: ci\n\"on\":\n  pull_request:\npermissions: {}\n"
                 "concurrency: ci\nenv:\n  A: 1\n")
        (self.root / ".github" / "workflows" / "a.yml").write_text(plain + head + PROPAGATE, encoding="utf-8")
        self.assertEqual(protection.produced_contexts(self.root)[0].needed_by, {"test": {"ci"}})

    def test_only_the_template_keys_in_plain_form_gate(self):
        """sd:1741 extra review pass: `"continue-on-error": true` is the key
        a line match for `continue-on-error:` misses, and YAML reads it as
        the plain key. Every line outside the loop must be a template key in
        plain form, so any other spelling reads as not gating."""
        head = "on: [pull_request]\njobs:\n  test:\n    runs-on: x\n  ci:\n    needs: test\n    if: always()\n    runs-on: x\n"
        cases = {
            "double-quoted key": head + PROPAGATE + '        "continue-on-error": true\n',
            "single-quoted key": head + PROPAGATE + "        'continue-on-error': true\n",
            "quoted key at job level": head + "    \"continue-on-error\": true\n" + PROPAGATE,
            "flow mapping": head + PROPAGATE + "      - {run: 'true', continue-on-error: true}\n",
            "merge key": head + "    <<: *tolerant\n" + PROPAGATE,
            "anchor": head + "    runs-on: &os x\n" + PROPAGATE,
            "tag": head + PROPAGATE.replace("RESULTS: ${{", "RESULTS: !!str ${{"),
            "unknown job key": head + "    strategy:\n      fail-fast: false\n" + PROPAGATE,
            "explicit key": head + PROPAGATE + "        ? continue-on-error\n        : true\n",
        }
        for name, workflow in cases.items():
            with self.subTest(name=name):
                (self.root / ".github" / "workflows" / "a.yml").write_text(workflow, encoding="utf-8")
                produced, notes = protection.produced_contexts(self.root)
                self.assertEqual(produced.needed_by, {})
                self.assertEqual(self.classify(produced, notes)["detail"]["produced_not_required"], ["test"])
        allowed = head.replace("    runs-on: x\n  ci:", "    runs-on: x\n  ci:\n    name: ci") + "    timeout-minutes: 5\n" + PROPAGATE
        (self.root / ".github" / "workflows" / "a.yml").write_text(allowed, encoding="utf-8")
        self.assertEqual(protection.produced_contexts(self.root)[0].needed_by, {"test": {"ci"}})

    def test_a_name_over_two_lines_gates_nothing(self):
        """sd:1820: YAML reads `name: ci` continued on the next line as the
        check `ci gate`, so crediting `ci` would hide the ungated `test`."""
        head = "on: [pull_request]\njobs:\n  test:\n    runs-on: x\n  ci:\n    needs: test\n    if: always()\n"
        produced = self.produced(a=head + "    name: ci\n      gate\n    runs-on: x\n" + PROPAGATE)
        self.assertEqual(produced.needed_by, {})
        produced = self.produced(a=head + "    name: ci\n    runs-on: x\n" + PROPAGATE)
        self.assertEqual(produced.needed_by, {"test": {"ci"}})

    def test_an_aggregate_with_a_non_string_name_gates_nothing(self):
        """sd:2902: a list or mapping `name:` once raised TypeError in the
        structural read. The line read (sd:3011) refuses it as no aggregate."""
        top = "on: [pull_request]\njobs:\n  test:\n    runs-on: x\n"
        job = "    needs: test\n    if: always()\n    runs-on: x\n"
        for name in ("[a, b]", "{a: b}"):
            with self.subTest(name=name):
                (self.root / ".github" / "workflows" / "a.yml").write_text(
                    top + f"  ci:\n    name: {name}\n" + job + PROPAGATE, encoding="utf-8")
                produced, notes = protection.produced_contexts(self.root)
                self.assertEqual(produced.needed_by, {})
                self.assertIn("a.yml: job ci needs test but is not the aggregate", " ".join(notes))

    def test_a_name_two_jobs_produce_is_covered_only_when_both_are(self):
        """sd:1741 review round 5: `test` gated by `ci` in a.yml says nothing
        of another `test` in b.yml."""
        gated = "on: [pull_request]\njobs:\n  test:\n    runs-on: x\n  ci:\n    needs: test\n    if: always()\n    runs-on: x\n" + PROPAGATE
        produced = self.produced(a=gated, b="on: [pull_request]\njobs:\n  test:\n    runs-on: x\n")
        self.assertEqual(produced.needed_by, {})
        self.assertEqual(self.classify(produced)["detail"]["produced_not_required"], ["test"])
        produced = self.produced(b=gated)
        self.assertEqual(produced.needed_by, {"test": {"ci"}})

    def test_a_walk_stops_at_a_job_that_does_not_propagate(self):
        """`mid` skips when `build` fails; `ci` then sees `mid` skipped, not
        `build` failed, so only `mid` is gated."""
        produced = self.produced(a="on: [pull_request]\njobs:\n  build:\n    runs-on: x\n"
                                   "  mid:\n    needs: build\n    runs-on: x\n"
                                   "  ci:\n    needs: [mid]\n    if: always()\n    runs-on: x\n" + PROPAGATE)
        self.assertEqual(produced.needed_by, {"mid": {"ci"}})
        self.assertEqual(self.classify(produced)["detail"]["produced_not_required"], ["build"])

    def test_a_cycle_ends_the_walk(self):
        produced = self.produced(a="on: [pull_request]\njobs:\n  x:\n    needs: [y]\n    if: always()\n    runs-on: r\n"
                                   + PROPAGATE + "  y:\n    needs: [x]\n    if: always()\n    runs-on: r\n" + PROPAGATE)
        self.assertEqual(produced.needed_by, {"x": {"y"}, "y": {"x"}})


class Rows(SyncCase):
    def test_a_never_observed_repository_is_unknown_and_not_yet_observed(self):
        path = self.register("new", "git@github.com:platypeeps/new.git")
        other = self.register("local", "/srv/git/local.git")
        rows = {row["repo"]: row for row in protection.rows(self.db)}
        self.assertEqual(sorted(rows), sorted([path, other]))
        self.assertEqual(rows[path]["status"], "unknown")
        self.assertEqual(rows[path]["reason"], protection.NOT_OBSERVED)
        self.assertEqual(rows[path]["slug"], "platypeeps/new")
        self.assertIsNone(rows[path]["observed_at"])
        self.assertEqual(rows[path]["gaps"], [])
        self.assertIsNone(rows[other]["slug"])

    def test_a_row_carries_the_repositorys_ci_so_the_screen_names_its_baseline_check(self):
        """sd:2509. The screen labelled the column `ci` for a repository that requires `sd/local-gate`."""
        local = self.register("gated", "git@github.com:platypeeps/gated.git", ci="local")
        actions = self.register("actions", "git@github.com:platypeeps/actions.git", ci="github")
        rows = {row["repo"]: row for row in protection.rows(self.db)}
        self.assertEqual((rows[local]["ci"], rows[actions]["ci"]), ("local", "github"))
        self.assertEqual(protection.baseline_check(rows[local]["ci"]), protection.LOCAL_GATE_CHECK)

    def test_an_observed_row_carries_its_gaps_and_flags(self):
        path = self.register("seen", "https://github.com/platypeeps/seen", PR_WORKFLOW)
        client, _ = self.client({"repos/platypeeps/seen": repo_payload(allow_rebase_merge=True),
                                 f"repos/platypeeps/seen/branches/{MAIN}/protection":
                                 full_protection(enforce_admins={"enabled": False}),
                                 protection.rules_path("platypeeps", "seen", MAIN): []})
        protection.sync(self.db, client=client, observed_at=AT)
        row = {row["repo"]: row for row in protection.rows(self.db)}[path]
        self.assertEqual(row["status"], "protected")
        self.assertEqual([gap["id"] for gap in row["gaps"]], ["enforce_admins"])
        # A platypeeps remote carries the two baseline flags (sd:1741): classic
        # protection alone is the baseline (sd:5406), but requiring `CI Result`
        # raises the check.
        self.assertEqual({flag["id"]: flag["flagged"] for flag in row["merge_settings"]},
                         {"squash_message": False, "rebase_merge": True,
                          "protection_source": False, "required_check": True})
        self.assertEqual(row["requests"], 3)
        self.assertIsNone(row["reason"])

    def observed(self, path, status, observed_at, *, branch="main", gaps=()):
        self.db.execute("INSERT INTO repo_protection (repo, observed_at, status, default_branch, body) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (path, observed_at, status, branch, json.dumps({"gaps": [{"id": gap} for gap in gaps]})))
        self.db.commit()

    def test_a_checkout_with_no_row_borrows_a_sibling_checkouts_row_and_says_so(self):
        """sd:1607. A second checkout of one GitHub repository read `not yet observed`."""
        first = self.register("widget", "git@github.com:example/widget.git", ci="local")
        second = self.register("widget-copy", "https://github.com/Example/Widget", ci="github")
        self.observed(first, "protected", "2026-10-04T01:00:00Z", gaps=("enforce_admins",))
        rows = {row["repo"]: row for row in protection.rows(self.db)}
        self.assertEqual((rows[second]["status"], rows[second]["observed_at"], rows[second]["default_branch"]),
                         ("protected", "2026-10-04T01:00:00Z", "main"))
        self.assertEqual([gap["id"] for gap in rows[second]["gaps"]], ["enforce_admins"])
        self.assertEqual(rows[second]["borrowed_from"], first)
        # Its flags were judged against the lender's `ci`, so the label names that one.
        self.assertEqual(rows[second]["ci"], "local")
        self.assertIsNone(rows[first]["borrowed_from"])

    def test_the_latest_sibling_lends_and_an_own_row_is_never_replaced(self):
        old = self.register("old", "git@github.com:example/widget.git")
        new = self.register("new", "git@github.com:example/widget.git")
        bare = self.register("bare", "git@github.com:example/widget.git")
        alone = self.register("alone", "git@github.com:example/other.git")
        self.observed(old, "unprotected", "2026-10-03T01:00:00Z")
        self.observed(new, "protected", "2026-10-04T01:00:00Z")
        rows = {row["repo"]: row for row in protection.rows(self.db)}
        self.assertEqual(rows[bare]["status"], "protected")
        self.assertEqual(rows[bare]["borrowed_from"], new)
        self.assertEqual((rows[old]["status"], rows[old]["borrowed_from"]), ("unprotected", None))
        self.assertEqual((rows[alone]["status"], rows[alone]["reason"], rows[alone]["borrowed_from"]),
                         ("unknown", protection.NOT_OBSERVED, None))


def alert(number, severity):
    return {"number": number, "security_advisory": {"severity": severity}}


def summary(number, severity):
    """`alert`'s stored summary (pack sd:7515): its number and severity, every other field None."""
    return {"number": number, "severity": severity, "package": None, "ecosystem": None, "manifest": None,
            "summary": None, "ghsa": None, "cve": None, "patched": None, "url": None, "created_at": None}


def setting(owner, name):
    """GitHub's Dependabot alerts setting: 204 when on, 404 when off."""
    return f"repos/{owner}/{name}/vulnerability-alerts"


class Alerts(SyncCase):
    """sd:2205, sd:2206: a managed repository's alerts land in its row's body, read after every basic observation."""

    def managed(self, name):
        path = self.register(name, f"git@github.com:platypeeps/{name}.git", PR_WORKFLOW)
        with self.db:
            self.db.execute("UPDATE repo SET managed = 1 WHERE path = ?", (path,))
        return path

    def protected(self, name, **repo):
        payload = repo_payload(private=False, security_and_analysis={"secret_scanning": {"status": "enabled"}})
        payload.update(repo)
        return {f"repos/platypeeps/{name}": payload,
                f"repos/platypeeps/{name}/branches/{MAIN}/protection": full_protection(),
                protection.rules_path("platypeeps", name, MAIN): []}

    def alerts(self, path):
        return json.loads(self.stored()[path]["body"]).get("alerts")

    def test_a_managed_public_repository_stores_its_open_alerts_by_severity_after_every_basic_observation(self):
        mine = self.managed("mine")
        other = self.register("other", "git@github.com:platypeeps/other.git", PR_WORKFLOW)
        next_page = {"Link": '<https://api.github.com/repositories/1/dependabot/alerts?after=x>; rel="next"'}
        rows = {**self.protected("mine"), **self.protected("other"),
                protection.alert_path("platypeeps", "mine", "dependabot"):
                    Response(200, next_page, json.dumps([alert(1, "critical"), alert(2, "low"), alert(3, "low")])),
                protection.alert_path("platypeeps", "mine", "secret-scanning"): [{"number": 7}]}
        client, transport = self.client(rows)
        protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual(self.alerts(mine), {
            "dependabot": {"open": 3, "more": True, "severity": {"critical": 1, "low": 2},
                           "summaries": [summary(1, "critical"), summary(2, "low"), summary(3, "low")]},
            "secret_scanning": {"visibility": "public", "setting": "enabled", "open": 1, "more": False}})
        self.assertIsNone(self.alerts(other), "an unmanaged repository's alerts were read")
        self.assertEqual(transport.calls[-2:], [protection.alert_path("platypeeps", "mine", "dependabot"),
                                                protection.alert_path("platypeeps", "mine", "secret-scanning")])
        self.assertEqual(len(transport.calls), 8, "alerts were read before the other repository's observation")
        row = {row["repo"]: row for row in protection.rows(self.db)}
        self.assertEqual((row[mine]["managed"], row[mine]["alerts"]["dependabot"]["open"]), (True, 3))
        self.assertEqual((row[other]["managed"], row[other]["alerts"]), (False, None))

    def test_a_failed_read_is_a_reason_never_zero_and_private_or_archived_reads_nothing(self):
        failed, private, archived, hidden, off = (self.managed(name) for name in ("failed", "private", "archived",
                                                                                   "hidden", "off"))
        rows = {**self.protected("failed"), **self.protected("private", private=True),
                **self.protected("archived", archived=True), **self.protected("hidden"),
                **self.protected("off", security_and_analysis={"secret_scanning": {"status": "disabled"}}),
                protection.alert_path("platypeeps", "failed", "dependabot"): Response(403, {}, "{}"),
                protection.alert_path("platypeeps", "failed", "secret-scanning"): Response(404, {}, "{}"),
                protection.alert_path("platypeeps", "private", "dependabot"): [],
                protection.alert_path("platypeeps", "hidden", "dependabot"): [alert(1, None)],
                protection.alert_path("platypeeps", "off", "dependabot"): []}
        rows["repos/platypeeps/hidden"].pop("security_and_analysis")
        client, transport = self.client(rows)
        protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual(self.alerts(failed), {
            "dependabot": {"reason": "API HTTP 403; retry on a later collection"},
            "secret_scanning": {"visibility": "public", "setting": "enabled",
                                "reason": "API HTTP 404; retry on a later collection"}})
        self.assertEqual(self.alerts(private)["secret_scanning"], {"visibility": "private"})
        self.assertEqual(self.alerts(archived)["dependabot"], {"archived": True})
        self.assertEqual(self.alerts(hidden), {
            "dependabot": {"open": 1, "more": False, "severity": {"unknown": 1}, "summaries": [summary(1, "unknown")]},
            "secret_scanning": {"visibility": "public", "setting": None,
                                "reason": "security_and_analysis not shown: the token does not administer the repository"}})
        self.assertEqual(self.alerts(off)["secret_scanning"], {"visibility": "public", "setting": "disabled"})
        self.assertNotIn(protection.alert_path("platypeeps", "archived", "dependabot"), transport.calls)
        for name in ("private", "hidden", "off"):
            self.assertNotIn(protection.alert_path("platypeeps", name, "secret-scanning"), transport.calls)

    def test_an_entry_that_is_not_an_alert_is_a_reason_not_a_count(self):
        mine = self.managed("mine")
        client, _ = self.client({**self.protected("mine"),
                                 protection.alert_path("platypeeps", "mine", "dependabot"): [],
                                 protection.alert_path("platypeeps", "mine", "secret-scanning"): [None, "invalid"]})
        protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual(self.alerts(mine)["secret_scanning"]["reason"], "alert list holds an entry that is not an alert")
        self.assertNotIn("open", self.alerts(mine)["secret_scanning"])

    def test_alerts_spend_only_what_the_basic_observations_left(self):
        mine = self.managed("mine")
        other = self.register("other", "git@github.com:platypeeps/other.git", PR_WORKFLOW)
        client, _ = self.client({**self.protected("mine"), **self.protected("other")}, requests=6)
        protection.sync(self.db, client=client, observed_at=AT)
        stored = self.stored()
        self.assertEqual((stored[mine]["status"], stored[other]["status"]), ("protected", "protected"))
        self.assertEqual(self.alerts(mine)["dependabot"], {"reason": protection.REQUESTS_EXHAUSTED})

    def test_a_forbidden_alert_read_probes_the_setting_and_stores_alerts_off_as_a_setting(self):
        """sd:5594. A repository with Dependabot alerts off read `dependencies not read` in Health."""
        names = ("off", "on", "failed", "other")
        off, on, failed, other = (self.managed(name) for name in names)
        rows = {**self.protected("off"), **self.protected("on"), **self.protected("failed"),
                **self.protected("other", permissions={"admin": False})}
        for name in names:
            rows[protection.alert_path("platypeeps", name, "dependabot")] = Response(403, {}, "{}")
            rows[protection.alert_path("platypeeps", name, "secret-scanning")] = []
        rows.update({setting("platypeeps", "off"): Response(404, {}, "{}"),
                     setting("platypeeps", "on"): Response(204, {}, ""),
                     setting("platypeeps", "failed"): Response(500, {}, ""),
                     setting("platypeeps", "other"): Response(404, {}, "{}")})
        forbidden = {"reason": "API HTTP 403; retry on a later collection"}
        client, transport = self.client(rows)
        protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual(self.alerts(off), {"dependabot": {"setting": "disabled"},
                                            "secret_scanning": {"visibility": "public", "setting": "enabled",
                                                                "open": 0, "more": False}})
        self.assertEqual(self.alerts(on)["dependabot"], forbidden)
        self.assertEqual(self.alerts(failed)["dependabot"], forbidden)
        # GitHub shows the setting only to an admin; without admin a 404 would say nothing.
        self.assertEqual(self.alerts(other)["dependabot"], forbidden)
        self.assertNotIn(setting("platypeeps", "other"), transport.calls)

    def test_a_probe_the_budget_cannot_pay_for_keeps_the_forbidden_reason(self):
        mine = self.managed("mine")
        rows = {**self.protected("mine"),
                protection.alert_path("platypeeps", "mine", "dependabot"): Response(403, {}, "{}"),
                setting("platypeeps", "mine"): Response(404, {}, "{}")}
        client, transport = self.client(rows, requests=4)
        protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual(self.alerts(mine)["dependabot"], {"reason": "API HTTP 403; retry on a later collection"})
        self.assertNotIn(setting("platypeeps", "mine"), transport.calls)

    def test_an_alert_read_that_fails_otherwise_spends_no_probe(self):
        mine = self.managed("mine")
        client, transport = self.client({**self.protected("mine"),
                                         protection.alert_path("platypeeps", "mine", "dependabot"):
                                             Response(404, {}, "{}"),
                                         setting("platypeeps", "mine"): Response(404, {}, "{}")})
        protection.sync(self.db, client=client, observed_at=AT)
        self.assertEqual(self.alerts(mine)["dependabot"], {"reason": "API HTTP 404; retry on a later collection"})
        self.assertNotIn(setting("platypeeps", "mine"), transport.calls)

    def test_each_dependabot_alert_on_the_page_read_is_stored_as_a_summary(self):
        """pack sd:7515: the GitHub page's Dependabot view lists each alert from the page the sync already read,
        with no further GitHub call; an odd field is None, never a failed read, and only a github.com link is kept."""
        mine = self.managed("mine")
        full = {"number": 4, "html_url": "https://github.com/platypeeps/mine/security/dependabot/4",
                "created_at": "2026-10-01T08:00:00Z",
                "dependency": {"package": {"ecosystem": "pip", "name": "widgetlib"}, "manifest_path": "requirements.txt"},
                "security_advisory": {"severity": "high", "summary": "Widgetlib reads past its buffer",
                                      "ghsa_id": "GHSA-aaaa-bbbb-cccc", "cve_id": "CVE-2026-0001"},
                "security_vulnerability": {"first_patched_version": {"identifier": "2.0.1"}}}
        odd = {"number": 5, "html_url": "javascript:alert(1)", "dependency": "not an object",
               "security_advisory": {"severity": None, "summary": "x" * 500, "cve_id": 7},
               "security_vulnerability": {"first_patched_version": None}}
        client, transport = self.client({**self.protected("mine"),
                                         protection.alert_path("platypeeps", "mine", "dependabot"): [full, odd],
                                         protection.alert_path("platypeeps", "mine", "secret-scanning"): []})
        protection.sync(self.db, client=client, observed_at=AT)
        dependabot = self.alerts(mine)["dependabot"]
        self.assertEqual({k: dependabot[k] for k in ("open", "more", "severity")},
                         {"open": 2, "more": False, "severity": {"high": 1, "unknown": 1}})
        self.assertEqual(dependabot.get("summaries"), [
            {"number": 4, "severity": "high", "package": "widgetlib", "ecosystem": "pip",
             "manifest": "requirements.txt", "summary": "Widgetlib reads past its buffer",
             "ghsa": "GHSA-aaaa-bbbb-cccc", "cve": "CVE-2026-0001", "patched": "2.0.1",
             "url": "https://github.com/platypeeps/mine/security/dependabot/4", "created_at": "2026-10-01T08:00:00Z"},
            {"number": 5, "severity": "unknown", "package": None, "ecosystem": None, "manifest": None,
             "summary": "x" * 199 + "…", "ghsa": None, "cve": None, "patched": None,
             "url": None, "created_at": None}])
        self.assertEqual(sum(1 for call in transport.calls if "dependabot" in call), 1, "a summary cost a GitHub call")

    def test_the_reserve_holds_three_alert_reads_per_managed_repository(self):
        """Dependabot alerts, the setting probe a 403 costs, and secret-scanning alerts."""
        self.managed("mine")
        self.register("other", "git@github.com:platypeeps/other.git")
        self.assertEqual(protection.reserve(self.db), 2 * 4 + 3)


class Slugs(unittest.TestCase):
    def test_the_github_spellings_and_the_others(self):
        for remote in ("git@github.com:platypeeps/system.git", "git@github.com:platypeeps/system",
                       "ssh://git@github.com/platypeeps/system.git", "https://github.com/platypeeps/system",
                       "https://github.com/platypeeps/system.git", "https://github.com/platypeeps/system/",
                       "ssh://git@github.com:443/platypeeps/system.git", "https://github.com:443/platypeeps/system"):
            self.assertEqual(protection.github_slug(remote), ("platypeeps", "system"), remote)
        for remote in (None, "", "/srv/git/x.git", "https://gitlab.com/o/r.git",
                       "git@github.com.evil:o/r.git", "https://github.com/o", "https://github.com/o/r/pull/1"):
            self.assertIsNone(protection.github_slug(remote), remote)


class InsideTheTrackerSync(SyncCase):
    """The observation rides in `shadow_sync.sync` and cannot fail it."""

    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 9, 11, 2, 45, tzinfo=timezone.utc)
        self.path = self.register("fleet", "git@github.com:platypeeps/fleet.git", PR_WORKFLOW)

    def heartbeat(self, key):
        row = self.db.execute("SELECT body FROM state WHERE kind = 'heartbeat' AND key = ? "
                              "ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        return json.loads(row["body"]) if row else None

    def test_a_403_becomes_an_unknown_row_and_the_tracker_still_moves_its_watermark(self):
        client, _ = self.client({"repos/platypeeps/fleet": Response(403, {}, "")})
        result = tracker_sync(self.db, now=self.now, runner=Gh(nodes=[node(11)]),
                              contribution_client=client)
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.watermark_moved)
        self.assertEqual(self.stored()[self.path]["status"], "unknown")
        tracker = self.heartbeat("tracker-sync:github")
        self.assertTrue(tracker["ok"])
        mine = self.heartbeat(protection.HEARTBEAT_KEY)
        self.assertFalse(mine["ok"])
        self.assertEqual(mine["unknown"], 1)
        self.assertEqual(mine["errors"], ["platypeeps/fleet: API HTTP 403; retry on a later collection"])
        # The tracker's own count is honest about what the budget spent: four
        # bucket searches, then the one protection GET the 403 answered.
        self.assertEqual(result.requests, 4 + 1)
        self.assertEqual(mine["requests"], 1)

    def test_a_collector_that_raises_is_a_heartbeat_error_and_not_a_failed_sync(self):
        client, _ = self.client({})
        with patch.object(protection, "sync", side_effect=RuntimeError("boom")):
            result = tracker_sync(self.db, now=self.now, runner=Gh(nodes=[node(11)]),
                                  contribution_client=client)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(read_watermark(self.db), "2026-09-11T02:45:00Z")
        self.assertEqual(self.heartbeat(protection.HEARTBEAT_KEY)["errors"], ["RuntimeError: boom"])
        self.assertEqual(self.stored(), {})

    def test_a_200_lands_as_protected_through_the_tracker(self):
        client, _ = self.client({"repos/platypeeps/fleet": repo_payload(),
                                 f"repos/platypeeps/fleet/branches/{MAIN}/protection": full_protection()})
        result = tracker_sync(self.db, now=self.now, runner=Gh(nodes=[node(11)]),
                              contribution_client=client)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.stored()[self.path]["status"], "protected")
        self.assertTrue(self.heartbeat(protection.HEARTBEAT_KEY)["ok"])
        self.assertEqual(self.heartbeat(protection.HEARTBEAT_KEY)["observed_at"], "2026-09-11T02:45:00Z")


if __name__ == "__main__":
    unittest.main()
