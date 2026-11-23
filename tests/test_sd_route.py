"""Table-driven fixtures for the pure review router in bin/sd_route.py."""

from __future__ import annotations

import pathlib
import sys
import unittest
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "bin"))

import sd_route  # noqa: E402

POLICY: dict[str, Any] = {
    "tier_order": ["skip", "cheap", "standard", "deep"],
    "default_tier": "standard",
    "categories": [
        {"name": "docs", "required": False, "paths": ["docs/**", "*.md"], "tier": "cheap"},
        {"name": "installer", "required": True, "paths": ["installer/**"], "tier": "standard"},
    ],
    "docs_skip": ["docs/**", "*.md"],
    "never_skip": ["docs/spec/**"],
    "sensitive": [".github/workflows/**", "installer/**"],
    "large_change_lines": 800,
}


class RouteTests(unittest.TestCase):
    def assert_plan(
        self,
        paths: list[str],
        lines: int,
        draft: bool,
        tier: str,
        depth: int,
        category: str | None,
        policy: dict[str, Any] | None = None,
    ) -> sd_route.Plan:
        plan = sd_route.route(paths, lines, draft, policy or POLICY)
        self.assertEqual(plan.tier, tier, plan.reason)
        self.assertEqual(plan.depth, depth, plan.reason)
        self.assertEqual(plan.category, category, plan.reason)
        self.assertTrue(plan.reason.strip(), "every plan explains itself")
        return plan

    def test_documentation_only_change_plans_skip(self) -> None:
        plan = self.assert_plan(
            ["docs/work/2026-08-29-x/prd.md", "README.md"], 40, False, "skip", 0, "docs"
        )
        self.assertIn("docs-skip allow-list", plan.reason)

    def test_never_skip_path_inside_a_docs_only_change_is_not_skipped(self) -> None:
        plan = self.assert_plan(
            ["docs/work/2026-08-29-x/prd.md", "docs/spec/backend/index.md"],
            40,
            False,
            "cheap",
            1,
            "docs",
        )
        self.assertIn("never-skip deny-list", plan.reason)

    def test_a_change_past_the_line_threshold_escalates(self) -> None:
        self.assert_plan(["src/app.py"], 801, False, "deep", 1, None)

    def test_a_change_at_the_line_threshold_does_not_escalate(self) -> None:
        self.assert_plan(["src/app.py"], 800, False, "standard", 1, None)

    def test_a_sensitive_glob_escalates(self) -> None:
        plan = self.assert_plan(
            [".github/workflows/ci.yml"], 10, False, "deep", 1, None
        )
        self.assertIn("sensitive path", plan.reason)

    def test_sensitive_and_large_escalate_twice_but_stop_at_the_top(self) -> None:
        self.assert_plan(
            [".github/workflows/ci.yml"], 5000, False, "deep", 1, None
        )

    def test_a_draft_plans_the_cheapest_reviewing_tier(self) -> None:
        plan = self.assert_plan(
            [".github/workflows/ci.yml"], 5000, True, "cheap", 1, None
        )
        self.assertIn("draft pull request", plan.reason)

    def test_a_draft_documentation_change_stays_at_skip(self) -> None:
        self.assert_plan(["README.md"], 10, True, "skip", 0, "docs")

    def test_a_lowering_category_needs_every_path_to_be_in_it(self) -> None:
        """One markdown file must not take a reviewer off a source change.

        The bug this guards was found by the step-3 end-to-end, not by review:
        `_match_category` matched on *any* path, so `docs` (tier `cheap`,
        below the `standard` default) fired on a change that was mostly source.
        Because every work item lives under `docs/work/`, that quietly routed
        nearly every change made through this framework one tier cheaper than
        the same code alone.
        """

        plan = self.assert_plan(
            ["src/greet.py", "README.md"], 50, False, "standard", 1, None
        )
        self.assertIn("no category matched", plan.reason)
        # The same code alone routes identically -- adding documentation to a
        # change is what must not move it.
        alone = sd_route.route(["src/greet.py"], 50, False, POLICY)
        self.assertEqual(alone.tier, plan.tier)
        self.assertEqual(alone.depth, plan.depth)
        # And a work item, the shape this framework produces on every change.
        item = sd_route.route(["src/greet.py", "docs/work/x/prd.md"], 50, False, POLICY)
        self.assertEqual(item.tier, "standard", item.reason)

    def test_a_lowering_category_still_matches_when_every_path_is_in_it(self) -> None:
        self.assert_plan(["docs/a.md", "docs/b.md"], 40, False, "skip", 0, "docs")

    def test_an_escalating_category_matches_on_one_path(self) -> None:
        """The other direction keeps any-match, and that is the whole rule.

        `installer` sits above the `cheap` default in this policy, so one
        touched installer file makes the change an installer change even though
        the rest of it is documentation. Escalating on one path can only cost a
        review nobody needed; lowering on one path costs a review someone did.
        """

        policy = dict(POLICY, default_tier="cheap")
        plan = self.assert_plan(
            ["docs/guide.md", "installer/registry.py"],
            10,
            False,
            "deep",
            1,
            "installer",
            policy=policy,
        )
        self.assertIn("category installer", plan.reason)

    def test_required_categories_match_before_optional_ones(self) -> None:
        plan = self.assert_plan(
            ["docs/guide.md", "installer/registry.py"],
            10,
            False,
            "deep",
            1,
            "installer",
        )
        self.assertIn("category installer", plan.reason)

    def test_an_unmatched_change_falls_back_to_the_default_tier(self) -> None:
        plan = self.assert_plan(["src/app.py"], 10, False, "standard", 1, None)
        self.assertIn("no category matched", plan.reason)

    def test_a_star_does_not_reach_across_directory_separators(self) -> None:
        policy = dict(POLICY, never_skip=["docs/*"])
        self.assert_plan(
            ["docs/work/2026-08-29-x/prd.md"], 10, False, "skip", 0, "docs", policy=policy
        )

    def test_an_empty_policy_still_produces_a_usable_plan(self) -> None:
        plan = sd_route.route(["src/app.py"], 10, False, {})
        self.assertEqual(plan.tier, "deep")
        # Missing policy still requires review: two readers, as every reviewing tier (sd:7519).
        self.assertEqual(plan.depth, 1)
        self.assertIsNone(plan.category)

    def test_an_empty_path_list_is_never_skippable(self) -> None:
        plan = sd_route.route([], 0, False, POLICY)
        self.assertEqual(plan.tier, "standard")

    def test_route_is_pure_and_repeatable(self) -> None:
        paths = ["docs/spec/backend/index.md"]
        first = sd_route.route(paths, 10, False, POLICY)
        second = sd_route.route(paths, 10, False, POLICY)
        self.assertEqual(first, second)
        self.assertEqual(paths, ["docs/spec/backend/index.md"])


if __name__ == "__main__":
    unittest.main()
