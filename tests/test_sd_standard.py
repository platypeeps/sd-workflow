"""`sd fleet standard` (sd:5610): one repository brought to the managed standard, against an in-process GitHub.

The fake answers `sd_ci.Call` from a model of one repository and applies the
writes to it, so a second plan reads what the first apply wrote. The database
is a throwaway one and the checkout a fresh `git init`; nothing leaves the
machine.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from urllib.parse import unquote

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_fleet  # noqa: E402
import sd_standard  # noqa: E402

from tests.clean_env import clean_environment  # noqa: E402

PREFIX = "repos/{owner}/{repo}"
REMOTE = "https://github.com/example/widget.git"


def model(**changes: Any) -> dict:
    """A private owned repository that holds none of the standard, as a fresh repository does."""
    state = {
        "repo": {"full_name": "example/widget", "default_branch": "main", "private": True,
                 "html_url": "https://github.com/example/widget", "permissions": {"admin": True},
                 "squash_merge_commit_title": "COMMIT_OR_PR_TITLE", "squash_merge_commit_message": "COMMIT_MESSAGES",
                 "allow_rebase_merge": True, "delete_branch_on_merge": False, "has_issues": True,
                 "has_wiki": True, "has_projects": True},
        "classic": None, "rulesets": {}, "actions": {"enabled": True}, "workflows": [],
        "alerts": True, "issues": [], "fail": {},
    }
    state.update(changes)
    return state


class FakeGitHub:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.log: list[tuple[str, str, Any]] = []

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [entry for entry in self.log if entry[0] != "GET"]

    def __call__(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        self.log.append((method, path, copy.deepcopy(body)))
        state = self.state
        route = path[len(PREFIX):].split("?")[0]
        if f"{method} {route}" in state["fail"]:
            status, message = state["fail"][f"{method} {route}"]
            return status, {"message": message}
        branch = state["repo"]["default_branch"]
        if (method, route) == ("GET", ""):
            return 200, copy.deepcopy(state["repo"])
        if (method, route) == ("PATCH", ""):
            for key, value in body.items():
                if key == "security_and_analysis":
                    state["repo"].setdefault(key, {}).update(value)
                else:
                    state["repo"][key] = value
            return 200, state["repo"]
        if (method, route) == ("GET", "/branches"):
            return 200, [{"name": branch, "protected": state["classic"] is not None}]
        base = f"/branches/{branch}/protection"
        if route == base:
            if method == "GET":
                return (404, {"message": "Branch not protected"}) if state["classic"] is None else (200, state["classic"])
            if method == "PUT":
                state["classic"] = {
                    "required_status_checks": dict(body["required_status_checks"],
                                                   contexts=[c["context"] for c in body["required_status_checks"]["checks"]]),
                    "enforce_admins": {"enabled": body["enforce_admins"]},
                    "required_pull_request_reviews": body["required_pull_request_reviews"],
                }
                return 200, state["classic"]
        if route == f"{base}/required_status_checks" and method == "PATCH":
            state["classic"]["required_status_checks"] = dict(body, contexts=[c["context"] for c in body["checks"]])
            return 200, state["classic"]["required_status_checks"]
        if route == f"{base}/enforce_admins" and method == "POST":
            state["classic"]["enforce_admins"] = {"enabled": True}
            return 200, state["classic"]["enforce_admins"]
        if route == f"{base}/required_pull_request_reviews" and method == "PATCH":
            state["classic"]["required_pull_request_reviews"].update(body)
            return 200, state["classic"]["required_pull_request_reviews"]
        if route.startswith("/rules/branches/") and method == "GET":
            rules = []
            for key, ruleset in sorted(state["rulesets"].items()):
                rules += [dict(rule, ruleset_id=int(key), ruleset_source_type="Repository") for rule in ruleset["rules"]]
            return 200, rules
        match = re.fullmatch(r"/rulesets/(\d+)", route)
        if match and method == "GET":
            return 200, state["rulesets"][match[1]]
        if route == "/actions/permissions":
            if method == "PUT":
                state["actions"]["enabled"] = body["enabled"]
            return 200, state["actions"]
        if route == "/actions/workflows" and method == "GET":
            return 200, {"total_count": len(state["workflows"]), "workflows": state["workflows"]}
        if route == "/vulnerability-alerts":
            if method == "GET":
                return (200, None) if state["alerts"] else (404, {"message": "Vulnerability alerts are disabled."})
            state["alerts"] = method == "PUT"
            return 200, None
        if route == "/issues" and method == "GET":
            return 200, state["issues"]
        return 404, {"message": f"Not Found: {method} {unquote(path)}"}


class Stamp:
    """`sd_fleet.plan_repo`'s answer, set by each test; the apply writes through `sd_fleet.apply`."""

    def __init__(self) -> None:
        self.changes: list[sd_fleet.Change] = []
        self.refused: list[str] = []

    def __call__(self, root: pathlib.Path, remote: str | None) -> sd_fleet.Plan:
        written = [change for change in self.changes
                   if not (change.where == "local" and (root / change.path).exists())]
        return sd_fleet.Plan(root=str(root), slug="example/widget", base="worktree", changes=list(written),
                             refused=list(self.refused))


class Standard(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name).resolve()
        self.root = self.home / "widget"
        self.root.mkdir()
        subprocess.run(["git", "-C", str(self.root), "init", "-q", "-b", "main"], check=True, env=clean_environment())
        subprocess.run(["git", "-C", str(self.root), "remote", "add", "origin", REMOTE], check=True,
                       env=clean_environment())
        from sd_db import connect, initialise, upsert_repo

        self.database = self.home / "sd.db"
        initialise(self.database)
        self.connection = connect(self.database)
        self.addCleanup(self.connection.close)
        upsert_repo(self.connection, str(self.root), remote=REMOTE)
        self.github = FakeGitHub(model())
        self.stamp = Stamp()
        self.branch = ("main", "main")
        self.owners: tuple[str, ...] = ("example",)

    def sources(self) -> sd_standard.Sources:
        return sd_standard.Sources(self.github, self.connection, self.owners, self.stamp, lambda root: self.branch)

    def row(self) -> dict:
        return dict(self.connection.execute("SELECT * FROM repo").fetchone())

    def plan(self) -> sd_standard.Plan:
        return sd_standard.build_plan(self.root, self.sources())

    def run_verb(self, *, apply: bool = False, digest: str | None = None, lock=None) -> tuple[int, dict]:
        stream = io.StringIO()
        args = argparse.Namespace(apply=apply, digest=digest, json=True, database=self.database)
        code = sd_standard.standard(args, sources=self.sources(), stream=stream, root=self.root,
                                    lock=lock or (lambda slug: contextlib.nullcontext()))
        return code, json.loads(stream.getvalue())

    def states(self, plan: sd_standard.Plan) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for step in plan.steps:
            out.setdefault(step.state, []).append(f"{step.key} {step.subject}")
        return out

    # --- the preview -----------------------------------------------------------

    def test_an_unmanaged_manual_row_previews_the_whole_standard_and_writes_nothing(self) -> None:
        before = self.row()
        code, out = self.run_verb()
        self.assertEqual(code, 0)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.row(), before)
        changed = {step["key"] for step in out["steps"] if step["state"] == "change"}
        self.assertEqual(changed, {"G1", "G2", "G3", "G4", "G5", "G6", "G9", "D1", "D2", "D3", "D4"})
        self.assertFalse(out["managed"])
        self.assertFalse(out["used_before"])

    def test_apply_reaches_the_standard_and_a_second_plan_finds_nothing(self) -> None:
        code, out = self.run_verb(apply=True)
        self.assertEqual(code, 0, out)
        self.assertNotIn("change", self.states(self.plan()))
        put = [entry for entry in self.github.writes if entry[0] == "PUT" and entry[1].endswith("/protection")]
        self.assertEqual(put[0][2], sd_standard.PROTECTION_BODY)
        self.assertIn("restrictions", put[0][2])
        self.assertIsNone(put[0][2]["restrictions"])
        row = self.row()
        self.assertEqual((row["managed"], row["runner_merge"], row["ci"]), (1, "auto", "local"))

    def test_the_lane_steps_run_last(self) -> None:
        keys = [step.key for step in self.plan().steps]
        self.assertEqual(keys[-2:], ["D1", "D2"])

    # --- Dependabot alerts: off is a kept choice (sd:5611) --------------------

    def test_alerts_off_read_as_a_kept_choice_and_are_never_turned_on(self) -> None:
        self.github = FakeGitHub(model(alerts=False))
        alerts = [step for step in self.plan().steps if step.key == "G7"]
        self.assertEqual([(step.state, step.before) for step in alerts], [("ok", "off")])
        self.assertIn("kept choice", alerts[0].detail)
        self.run_verb(apply=True)
        self.assertFalse([entry for entry in self.github.writes if "vulnerability-alerts" in entry[1]])
        self.assertFalse(self.github.state["alerts"])

    def test_an_alerts_read_that_fails_is_unknown_not_off(self) -> None:
        self.github = FakeGitHub(model(fail={"GET /vulnerability-alerts": (502, "Bad Gateway")}))
        alerts = [step for step in self.plan().steps if step.key == "G7"]
        self.assertEqual(alerts[0].state, "unknown")

    # --- what it never touches -------------------------------------------------

    def test_an_employer_repository_gets_no_github_write(self) -> None:
        self.owners = ("someone-else",)
        code, out = self.run_verb(apply=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.row()["managed"], 1)

    def test_a_ruleset_skips_protection_and_is_never_written(self) -> None:
        ruleset = {"id": 7, "name": "guard", "target": "branch", "enforcement": "active",
                   "source_type": "Repository", "source": "example/widget", "bypass_actors": [],
                   "rules": [{"type": "pull_request", "parameters": {}}]}
        self.github = FakeGitHub(model(rulesets={"7": ruleset}))
        protection = [step for step in self.plan().steps if step.key == "G1"]
        self.assertEqual([step.state for step in protection], ["skip"])
        self.run_verb(apply=True)
        self.assertFalse([entry for entry in self.github.writes if "/rulesets" in entry[1] or "/protection" in entry[1]])

    # --- G9 waits on G1: workflows go off only once sd/local-gate is the required check ----------------

    def actions_writes(self) -> list:
        return [entry for entry in self.github.writes if "/actions" in entry[1]]

    def test_the_actions_filter_sees_an_actions_write(self) -> None:
        """Pack sd:8823: the log holds the full prefixed path, so a `startswith` filter saw nothing."""
        code, out = self.run_verb(apply=True)
        self.assertEqual(code, 0, out)
        self.assertTrue(self.actions_writes())

    def test_unread_protection_holds_the_lane_steps(self) -> None:
        """Pack sd:8823: a G1 that was not read is no protection known to require sd/local-gate."""
        self.github = FakeGitHub(model(fail={"GET /branches/main/protection": (502, "Bad Gateway")}))
        code, out = self.run_verb(apply=True)
        results = {step["key"]: step.get("result") for step in out["steps"] if step["key"] in sd_standard.LAST}
        self.assertTrue(all(str(result).startswith("held") for result in results.values()), results)
        self.assertEqual(self.row()["managed"], 0)
        self.assertEqual(code, sd_standard.EXIT_FAILED)

    def test_a_ruleset_holds_actions_on(self) -> None:
        ruleset = {"id": 7, "name": "guard", "target": "branch", "enforcement": "active",
                   "source_type": "Repository", "source": "example/widget", "bypass_actors": [],
                   "rules": [{"type": "required_status_checks", "parameters": {
                       "required_status_checks": [{"context": "ci/test"}]}}]}
        self.github = FakeGitHub(model(rulesets={"7": ruleset}))
        self.assertEqual([step.state for step in self.plan().steps if step.key == "G9"], ["skip"])
        self.run_verb(apply=True)
        self.assertEqual(self.actions_writes(), [])

    def test_unread_protection_holds_actions_on(self) -> None:
        self.github = FakeGitHub(model(fail={"GET /branches/main/protection": (502, "Bad Gateway")}))
        self.assertEqual([step.state for step in self.plan().steps if step.key == "G9"], ["skip"])

    def test_a_failed_protection_write_holds_actions_on(self) -> None:
        self.github = FakeGitHub(model(fail={"PUT /branches/main/protection": (422, "Validation Failed")}))
        code, out = self.run_verb(apply=True)
        self.assertEqual(code, sd_standard.EXIT_FAILED)
        results = {step["key"]: step.get("result") for step in out["steps"] if step["key"] == "G9"}
        self.assertTrue(results["G9"].startswith("held"), results)
        self.assertEqual(self.actions_writes(), [])

    def test_open_issues_keep_issues_on(self) -> None:
        self.github = FakeGitHub(model(issues=[{"number": 3}, {"number": 4, "pull_request": {}}]))
        issues = [step for step in self.plan().steps if step.key == "G5"][0]
        self.assertEqual(issues.state, "skip")
        self.assertIn("1 open issue", issues.detail)
        self.run_verb(apply=True)
        self.assertTrue(self.github.state["repo"]["has_issues"])

    def test_an_existing_classic_object_changes_only_through_its_sub_objects(self) -> None:
        classic = {"required_status_checks": {"strict": True, "contexts": [sd_standard.CONTEXT],
                                              "checks": [{"context": sd_standard.CONTEXT}]},
                   "enforce_admins": {"enabled": False},
                   "required_pull_request_reviews": {"required_approving_review_count": 2}}
        self.github = FakeGitHub(model(classic=classic))
        self.run_verb(apply=True)
        touched = sorted({(method, path.rsplit("/protection", 1)[-1]) for method, path, _ in self.github.writes
                          if "/protection" in path})
        self.assertEqual(touched, [("PATCH", "/required_pull_request_reviews"), ("POST", "/enforce_admins")])
        self.assertEqual(self.github.state["classic"]["required_pull_request_reviews"]["required_approving_review_count"], 0)

    def test_classic_protection_without_checks_or_reviews_is_a_skip_naming_the_page(self) -> None:
        self.github = FakeGitHub(model(classic={"enforce_admins": {"enabled": True}}))
        protection = {step.subject: step for step in self.plan().steps if step.key == "G1"}
        skipped = [step for step in protection.values() if step.state == "skip"]
        self.assertEqual(len(skipped), 2)
        self.assertTrue(all("example/widget/settings/branches" in step.detail for step in skipped))

    # --- evidence ties ---------------------------------------------------------

    def test_a_github_answer_for_another_repository_refuses(self) -> None:
        self.github.state["repo"]["full_name"] = "example/other"
        with self.assertRaisesRegex(sd_standard.StandardRefusal, "different repositories"):
            self.plan()

    def test_a_stale_digest_refuses_and_writes_nothing(self) -> None:
        digest = self.plan().plan_digest
        self.github.state["repo"]["allow_rebase_merge"] = False
        with self.assertRaisesRegex(sd_standard.StandardRefusal, "changed since the preview"):
            self.run_verb(apply=True, digest=digest)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.row()["managed"], 0)

    def test_the_previewed_digest_applies(self) -> None:
        _, preview = self.run_verb()
        code, out = self.run_verb(apply=True, digest=preview["digest"])
        self.assertEqual(code, 0, out)
        self.assertTrue(out["applied"])

    def test_a_held_ship_lock_refuses_before_any_write(self) -> None:
        def held(slug: str):
            raise sd_standard.StandardRefusal(f"ship lock held for {slug}")
        with self.assertRaisesRegex(sd_standard.StandardRefusal, "ship lock held for example/widget"):
            self.run_verb(apply=True, lock=held)
        self.assertEqual(self.github.writes, [])

    # --- steps and failures ----------------------------------------------------

    def test_a_failed_step_stops_nothing_but_the_lane_steps(self) -> None:
        self.github = FakeGitHub(model(fail={"PATCH ": (422, "Validation Failed")}))
        code, out = self.run_verb(apply=True)
        self.assertEqual(code, sd_standard.EXIT_FAILED)
        results = {(step["key"], step["subject"]): step.get("result") for step in out["steps"]}
        self.assertTrue(results[("G2", "Squash commits")].startswith("failed: Squash commits: Validation Failed"))
        self.assertIn("; live: change", results[("G2", "Squash commits")])
        self.assertEqual(results[("G9", "Actions")], "changed")
        self.assertEqual(results[("D3", "ci")], "changed")
        self.assertTrue(results[("D1", "managed")].startswith("held"))
        self.assertEqual(self.row()["managed"], 0)
        self.github.state["fail"] = {}
        code, out = self.run_verb(apply=True)
        self.assertEqual(code, 0, out)
        self.assertNotIn("change", self.states(self.plan()))

    def test_a_run_stopped_after_any_step_finishes_on_the_next(self) -> None:
        first = self.plan()
        changes = [(step.key, step.subject) for step in first.steps if step.state == "change"]
        for stop in range(len(changes)):
            with self.subTest(stop=changes[stop]):
                self.setUp()
                plan = self.plan()
                done = [step for step in plan.steps if step.state == "change"][:stop + 1]
                for step in done:
                    step.write()
                left = [(step.key, step.subject) for step in self.plan().steps if step.state == "change"]
                self.assertEqual(left, changes[stop + 1:])
                self.run_verb(apply=True)
                self.assertNotIn("change", self.states(self.plan()))

    # --- the checkout ----------------------------------------------------------

    def test_untracked_files_are_written_and_tracked_ones_filed_once(self) -> None:
        self.stamp.changes = [sd_fleet.Change("CLAUDE.local.md", "local", None, "block\n"),
                              sd_fleet.Change(".claude/settings.json", "tracked", None, "{}\n")]
        self.run_verb(apply=True)
        self.assertEqual((self.root / "CLAUDE.local.md").read_text(), "block\n")
        self.assertFalse((self.root / ".claude/settings.json").exists())
        plan = self.plan()
        self.assertEqual([step.state for step in plan.steps if step.subject == "tracked fleet files"], ["pending"])
        self.assertEqual([step.state for step in plan.steps if step.subject == "stamp item"], ["ok"])
        self.run_verb(apply=True)
        items = self.connection.execute("SELECT count(*) FROM item WHERE title = ?", (sd_standard.STAMP_TITLE,))
        self.assertEqual(items.fetchone()[0], 1)

    def test_a_checkout_off_its_default_branch_gets_no_file(self) -> None:
        self.branch = ("feature", "main")
        self.stamp.changes = [sd_fleet.Change("CLAUDE.local.md", "local", None, "block\n")]
        step = [step for step in self.plan().steps if step.subject == "CLAUDE.local.md"][0]
        self.assertEqual(step.state, "skip")
        self.run_verb(apply=True)
        self.assertFalse((self.root / "CLAUDE.local.md").exists())

    # --- the row ---------------------------------------------------------------

    def test_the_review_declaration_keeps_its_other_keys(self) -> None:
        from sd_db import repos

        repos.set_declaration(self.connection, str(self.root), "review", {"severity_floor": "high"})
        self.run_verb(apply=True)
        held = repos.declarations(self.connection, str(self.root))["review"]
        self.assertEqual(held, {"severity_floor": "high", "copilot_review": {"automatic_deep": False}})

    def test_lane_history_reads_as_used_before(self) -> None:
        from sd_db import workflow

        self.assertFalse(self.plan().used_before)
        workflow.capture_task(self.connection, title="shipped", repo=str(self.root), who="test")
        item = self.connection.execute("SELECT id FROM item WHERE title = 'shipped'").fetchone()[0]
        self.connection.execute("INSERT INTO note (item, timestamp, kind, body) VALUES (?, ?, 'status_change', ?)",
                                (item, "2026-10-21T00:00:00+00:00", "in_progress -> done by sd-ship: delivered"))
        self.connection.commit()
        self.assertTrue(self.plan().used_before)


if __name__ == "__main__":
    unittest.main()
