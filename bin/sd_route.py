"""Review routing: how deeply a change is read, and why.

One pure function. ``route`` takes the paths a change touches, how many lines it
moves, whether the pull request is a draft, and a policy dictionary the caller
loaded from JSON. It returns a :class:`Plan`. It reads no files, runs no
subprocesses, and asks no network -- everything it decides is a function of its
arguments, so a fixture is a dict and a plan is comparable with ``==``.

The policy is data on purpose. Which categories exist, which paths are sensitive
and which tier each category starts at are per-repository choices; the rules for
combining them are not.

**It does not say who reviews.** The registry names the ordered provider chain;
the tier records the change's risk classification. Every reviewing tier asks
two independent local reviews, the first two readers in the chain run at once
(sd:7519), cut to the eligible readers by `reader_depth`; skip requires none.

Policy shape (every key optional unless noted)::

    {
      "tier_order": ["skip", "cheap", "standard", "deep"],
      "default_tier": "standard",
      "categories": [
        {"name": "tooling", "required": true, "paths": ["bin/**"],
         "tier": "deep"}
      ],
      "docs_skip": ["docs/**", "*.md"],
      "never_skip": ["docs/spec/**"],
      "sensitive": [".github/workflows/**"],
      "large_change_lines": 800
    }
"""

from __future__ import annotations

import re
from typing import Any, Mapping, NamedTuple, Sequence

DEFAULT_TIER_ORDER: tuple[str, ...] = ("skip", "cheap", "standard", "deep")
DEFAULT_LARGE_CHANGE_LINES = 800

#: Every reviewing tier asks one reader, the chain's first eligible one:
#: codex, or claude when codex is capped, metered out or unready. Codex raised
#: 120 of 129 blockers in the 7 days to 2026-11-23, claude 2 codex missed
#: (operator 2026-11-23; replaces sd:7239's two readers). Unknown tiers still
#: require a local review rather than silently skipping it.
TIER_DEPTH: dict[str, int] = {"skip": 0, "cheap": 1, "standard": 1, "deep": 1}
DEFAULT_DEPTH = 1


def reader_depth(asked: int, eligible: int) -> int:
    """Readers to request: the asked depth cut to the eligible ones (#43332).

    A machine with one eligible reader still reviews. Zero eligible keeps one
    requested, so readiness refuses an empty chain as it did before sd:7519.
    """
    return min(asked, max(eligible, 1)) if asked else 0


def review_shortfall(asked: int, eligible: int) -> dict[str, int] | None:
    """The result's record of a chain shorter than the asked depth (#43332)."""
    return {"requested": asked, "eligible": eligible} if eligible < asked else None


def shortfall_line(shortfall: Mapping[str, int] | None) -> str:
    """The text output's line for `review_shortfall`, empty when there is none."""
    if not shortfall:
        return ""
    return f"  shortfall   requested {shortfall['requested']}, eligible {shortfall['eligible']}\n"


class Plan(NamedTuple):
    """The routing decision for one change."""

    tier: str
    depth: int
    category: str | None
    reason: str


def route(
    paths: Sequence[str],
    lines: int,
    draft: bool,
    policy: Mapping[str, Any],
) -> Plan:
    """Decide which review tier a change gets, and how many readers that buys.

    The order below is the whole contract, and each step can only be reached
    when the one before it did not settle the question:

    1. Categories match required-first, so a repository can guarantee that
       "touches the installer" outranks "is mostly documentation" no matter
       which order the two appear in the file. The matched category names the
       starting tier; without a match the policy default starts it. A category
       that lowers the tier below that default needs *every* path to be in it;
       one that holds or raises the tier needs only one. See
       ``_match_category`` -- "is mostly documentation" has to mean mostly.
    2. A change whose every path is in the ``docs_skip`` allow-list plans
       ``skip`` -- unless any path is in the ``never_skip`` deny-list, which
       always wins. That deny-list is why the allow-list is safe to widen: a
       repository can say "documentation is free" and still keep one directory
       inside it reviewed.
    3. A sensitive path or a change past the line threshold escalates one tier
       each, up to the top of the chain.
    4. A draft asks for the cheapest tier that still reviews something: drafts
       are re-reviewed when they open, so paying the deep chain twice buys
       nothing. A plan that already reached ``skip`` stays there, being cheaper.
    """

    order = _tier_order(policy)
    ordered_paths = tuple(paths)

    default_tier = _clamp(str(policy.get("default_tier") or order[-1]), order)
    category, category_tier = _match_category(ordered_paths, policy, order, default_tier)
    tier = category_tier or default_tier
    reasons: list[str] = []
    if category:
        reasons.append(f"category {category} starts at tier {tier}")
    else:
        reasons.append(f"no category matched; default tier {tier}")

    blocked = _first_match(ordered_paths, policy.get("never_skip"))
    skippable = _all_match(ordered_paths, policy.get("docs_skip"))

    if skippable and blocked is None and "skip" in order:
        tier = "skip"
        reasons.append("every path is in the docs-skip allow-list")
    else:
        if skippable and blocked is not None:
            reasons.append(f"{blocked} is in the never-skip deny-list, so skip is not available")

        sensitive = _first_match(ordered_paths, policy.get("sensitive"))
        if sensitive is not None:
            tier = _escalate(tier, order)
            reasons.append(f"{sensitive} is a sensitive path, escalated to {tier}")

        threshold = _threshold(policy)
        if lines > threshold:
            tier = _escalate(tier, order)
            reasons.append(f"{lines} lines is above the {threshold}-line threshold, escalated to {tier}")

    if draft and tier != "skip":
        cheapest = _cheapest_reviewing_tier(order)
        if cheapest != tier:
            tier = cheapest
            reasons.append(f"draft pull request, reduced to the cheapest reviewing tier {tier}")
        else:
            reasons.append("draft pull request, already at the cheapest reviewing tier")

    return Plan(
        tier=tier,
        depth=TIER_DEPTH.get(tier, DEFAULT_DEPTH),
        category=category,
        reason="; ".join(reasons),
    )


def _tier_order(policy: Mapping[str, Any]) -> tuple[str, ...]:
    declared = policy.get("tier_order")
    if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
        order = tuple(str(name) for name in declared)
        if order:
            return order
    return DEFAULT_TIER_ORDER


def _threshold(policy: Mapping[str, Any]) -> int:
    declared = policy.get("large_change_lines")
    if isinstance(declared, bool) or not isinstance(declared, int):
        return DEFAULT_LARGE_CHANGE_LINES
    return declared


def _clamp(tier: str, order: tuple[str, ...]) -> str:
    return tier if tier in order else order[-1]


def _escalate(tier: str, order: tuple[str, ...]) -> str:
    try:
        index = order.index(tier)
    except ValueError:
        return order[-1]
    return order[min(index + 1, len(order) - 1)]


def _cheapest_reviewing_tier(order: tuple[str, ...]) -> str:
    for name in order:
        if name != "skip":
            return name
    return order[-1]


def _match_category(
    paths: Sequence[str],
    policy: Mapping[str, Any],
    order: tuple[str, ...],
    baseline: str,
) -> tuple[str | None, str | None]:
    """The category governing this change, matched by the direction it moves.

    One rule, applied by which way the category's tier points relative to the
    policy default:

      * A category that **holds or raises** the tier matches on **any** path.
        One touched installer file makes the whole change an installer change,
        which is the point of `required` ordering.
      * A category that **lowers** the tier matches only when **every** path is
        in it -- the same unanimity `docs_skip` already requires, and for the
        same reason.

    The asymmetry is not a special case for documentation, it is the safe
    direction in each case. Matching a lowering category on any path meant one
    markdown file could drop a source change from `standard` to `cheap` and
    take a reviewer off it; because every work item lives under `docs/work/`,
    that silently under-reviewed nearly every change made through this
    framework. Escalating on any path has no such failure mode: the worst it
    costs is a review nobody needed.
    """

    declared = policy.get("categories") or ()
    categories = [item for item in declared if isinstance(item, Mapping)]
    baseline_index = order.index(baseline) if baseline in order else len(order) - 1
    for required in (True, False):
        for category in categories:
            if bool(category.get("required")) is not required:
                continue
            declared_tier = category.get("tier")
            resolved = _clamp(str(declared_tier), order) if declared_tier else None
            lowers = resolved is not None and order.index(resolved) < baseline_index
            if lowers:
                matched = _all_match(paths, category.get("paths"))
            else:
                matched = _first_match(paths, category.get("paths")) is not None
            if not matched:
                continue
            name = str(category.get("name") or "")
            return (name or None, resolved)
    return (None, None)


def _first_match(paths: Sequence[str], patterns: Any) -> str | None:
    """The first path matching any pattern, in the order the caller gave them."""
    compiled = _compile(patterns)
    if not compiled:
        return None
    for path in paths:
        if any(pattern.match(path) for pattern in compiled):
            return path
    return None


def _all_match(paths: Sequence[str], patterns: Any) -> bool:
    """True when there is at least one path and every one of them matches."""
    compiled = _compile(patterns)
    if not compiled or not paths:
        return False
    return all(any(pattern.match(path) for pattern in compiled) for path in paths)


def _compile(patterns: Any) -> list[re.Pattern[str]]:
    if not isinstance(patterns, Sequence) or isinstance(patterns, (str, bytes)):
        return []
    return [_glob_to_regex(str(pattern)) for pattern in patterns]


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob, giving ``**`` and ``*`` their usual different reach.

    ``fnmatch`` cannot be used here: its ``*`` crosses directory separators, so
    ``docs/*`` would match ``docs/spec/index.md`` and a deny-list written one
    level deep would silently cover the whole tree.
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**", index):
                index += 2
                if pattern.startswith("/", index):
                    index += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile(f"^{''.join(out)}$")
