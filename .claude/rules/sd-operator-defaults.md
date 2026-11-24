# Operator defaults

These rules apply to Claude and Codex work in this pack and its companion repositories.
They record this maintainer's choices, not installation grants for another operator.
Explicit task instructions and stricter execution permissions take precedence.

## Reviewers

- Run `sd-review` and let it pick; do not pick a reviewer by hand or call a provider CLI directly.
- `sd-review` follows the reviewer order `sd providers list` shows; `sd providers configure` sets it.
- This file names no provider preference: the registry order is the only one, and `--explain` names its source.
- The registry refuses a file whose first `author` and first `reviewer` are one entry; reorder one list.
- Use a provider outside that order only after an explicit user request naming it for the task (`--provider NAME`).
- Do not select such a provider for fallback, retries, or an additional review slot.
- Availability, configured credentials, and standing transmission consent do not constitute that explicit request.
- If no permitted independent reviewer remains, report that condition once and stop the review.

Before expensive checks or external transmission, inspect `sd-review --explain --json` for the intended scope.
Check the complete fallback chain, required review count, authorship exclusions, and effective authorization.
Stop if the plan includes an unrequested provider from outside the order.
One completed local review, the first eligible reader on the order, satisfies each reviewing tier (sd:10193); risk classification adds no second.
Complete local review and fix verification before any Copilot request.
`sd.copilot_review` decides automatic requests; otherwise request one only when the operator names the pull request.
An unavailable local reviewer needs an operator decision, not an automatic remote escalation.

The pack's consent and the agent runtime's execution approval are separate boundaries.
Name the rejecting boundary and exact intended recipient when approval is missing.
Do not request blanket approval for unused fallbacks or weaken the runtime's protections.

## Reports

Treat skill report fields as a completeness check, not mandatory headings in every reply.
Include only applicable results, verification evidence, unresolved limits, and required decisions.
Link detailed evidence instead of repeating it in chat.
Do not omit failed checks or incomplete work to shorten a report.

## Enforcement boundary

These are agent instructions, not proof that generated prose or provider dispatch complies.
The executable review gate reads its registry, authorization, and tier policy.
Existing installations retain their configured reviewer order until explicitly migrated.
Report conflicts before execution; do not describe changed defaults as a completed machine migration.
