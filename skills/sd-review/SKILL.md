---
name: sd-review
description: Review the exact current diff with local providers and dispose of the findings locally, never posting them.
disable-model-invocation: true
---

# sd-review

Review locally; report findings without posting them to GitHub.
Read the sd-workflow checkout's `.claude/rules/sd-operator-defaults.md` before selecting recipients.
Use STE-Concise for findings and reports.
Report only the result, decisive evidence, unresolved findings, and next action.

When the deliverable is a finished document, apply
`references/publication-contract.md`: it publishes to the Obsidian
vault and the dashboard's Documents tab by default, and reaches an
outward destination — Notion, Google Drive — only where the user designated that
document for it.

## Before execution

Inspect `sd-review --explain --json` with the intended scope and modifiers.
Check recipients, every automatic fallback, review depth, author exclusions, consent, and spending limits.
Stop before checks or transmission when that plan conflicts with the operator's instructions.
Name the rejecting boundary: selection, review count, pack consent, or runtime approval.
Pack consent does not grant runtime execution approval.

Explanation keeps `status: explained` and adds a `readiness` object.
Its `status` is `ready` or `blocked`; blockers name `code`, `boundary`, `provider`, and `next_action`.
Explanation makes no check, review, or quota call and performs no durable write.
On a ready `codex-json` lane it starts one local process, the skill-suppression probe below.
`runtime_approval: not_observable` means the agent must still satisfy its execution permissions.
A ready result does not prove remote service health or OS isolation.

Read `sd config get sd.external_reviews`.
`configured` permits scoped review context to eligible configured recipients.
Machine `deny` overrides local consent.
A present repository `reviewers` list restricts recipients; an empty list denies all.
An absent key inherits standing policy; malformed local consent stops execution.
Without a grant, stop before transmission.

`remote_reviews.copilot` in `--explain --json` names the effective Copilot policy (`deep`, `never`, `always`) and its source.
The source is `machine config` when `sd.copilot_review` opts out (sd:1444), then `repository` when the review declaration names `copilot_review.automatic_deep`, `machine config` when `sd.copilot_review` is set, else `machine default` (`deep`).
Installation does not grant consent.

## Scopes

| `--scope` | Subject |
|---|---|
| `worktree` (default) | Uncommitted changes against `HEAD`, including untracked files. |
| `branch` | `merge-base(HEAD, base)..HEAD`; base defaults to `origin/HEAD`, then `main` or `master`. |
| `pr` | Same local resolution as `branch`; no PR-state lookup. Supply `--draft` when applicable. |
| `planning` | The active item's `prd.md`, `design.md`, and `implement.md`. Use `--item NAME` to disambiguate. |

Planning reviews use the *prd and design* point.
Branch review before publication uses *code, before merge*.
Their caps are on those rows in the sd-workflow checkout's `.claude/rules/sd-planning-adversarial-review.md`.
Do not substitute another diff or copy a cap into this procedure.

## Ordinary review

1. Use the routing decision from `sd_route.route`.
   Repository policy determines risk and depth; the registry determines eligible reviewers.
2. Review the exact resolved subject.
   Only consented, enabled, supported, independent reviewers qualify.
   The first `depth` eligible readers run at once; a failed one's slot takes the next entry in ranked registry order.
   Rate limits, missing executables, authentication failures, failed runs, and timeouts can advance that chain.
   An exhausted chain with insufficient completed coverage fails.
3. Disposition findings locally against the repository's severity floor; unset, it is `high`.
   A finding at the floor blocks, cited or not; only a repeat of a finding rebutted on this branch needs `evidence` (a check run, or `path:line` and a quote of the current code), and one without posts as advisory, marked `[downgraded: repeats a rebutted finding, no evidence cited]`.
   A finding whose `trigger` is `synthetic`, input no caller or recorded data produces, posts as advisory, marked `[advisory: no caller or recorded data carries the failing input]`, outside planning reviews and the lane's own blockers.
   Input from outside the operator's control (a network reply, another person's file or text, user-typed text) and a data-loss or secret-leak path still block on a synthetic repro (sd:10288).
   A finding takes the newest recorded disposition at its exact path, whole summary (case and spacing folded) and exact line; it posts as advisory only when that one is a listed rebuttal whose file is unchanged since its receipt's head (a failed diff counts as a change), so a finding that moved or whose file changed blocks again, and the lane's own blockers are never rebutted. The prompt lists the rebuttals recorded from 14 days before the base's commit on, at a receipt head that is the base or in base..head, at most 10, each with its line; a reused branch name inherits none, and a retry at the same head reads the receipt recorded after it.
   Preserve each provider's findings and attempt outcome.
   A completed adverse review counts; it does not trigger replacement.
   A clean fallback cannot erase earlier findings.
4. Run the repository's deterministic gate through `sd-check`, after a review that does not block.
   A blocking or incomplete review runs no gate and records `check.status: not_run`; no test passed.
   Branch and `pr` scope run it in a clean detached worktree at the reviewed head, so an edit in the checkout during the run is not what passes.
   Explicit `--reuse-check` can reuse eligible full-check evidence; a reused receipt runs no gate.
   A failing gate fails the review.

Reviewing tiers ask one local review: the first eligible entry on the reviewer order (sd:10193).
The `claude-json` reader's prompt adds the ranked instruction: trace callers and config readers, findings in production-impact order.
Skip requires none; planning, lens and challenge reviews ask the same one reader.
Enabled reviewers outside the order `sd providers list` shows require explicit `--provider NAME` selection and the same eligibility checks.
Copilot never fills the local slot.
Shipping may request it afterward through the configured `deep` tier or explicit task direction.

The registry uses read-only database controls when available.
A missing database permits file defaults without creating a database.
An unreadable database stops execution instead of ignoring operator controls.

### The builder chain

A builder runs one chain per round, in its own worktree, at HEAD.
Make no edit while it runs; a new commit starts the next round.
`LOG` is the lane log folder, `<lane root>/<repository>/lane/`.
The lane root is `SD_LANE_ROOT`, else `sd.lane_root`, else `$XDG_STATE_HOME/sd/lanes`.
`<item>` is the sd item number; `N` is the round, from 1.

```
sd-review --scope branch > "$LOG/review-<item>-rN.log" 2>&1; echo "review exit $?" >> "$LOG/review-<item>-rN.log"
```

1. Read the verdict from the `sd-review: <status>` line, which comes just before `review exit N`.
   Report it as one line: `review-<item>-rN clean`.
2. Run the line in the foreground, or as a background command that wakes the agent on exit.
   Never wait with `Monitor` or `sleep`: `Monitor` stops at 30 minutes and leaves the agent idle.
3. Use this name and no other, so each round stays findable in the lane log folder.
   To wait on lane items or their running builds, run `sd-ship lane watch --item <id>`: a bare watch never exits.
4. Run no `sd gate check` before the review: the lane's prepare gate is the one full run (sd:5800).
   A pre-enqueue pass is reused only while main has not moved, so it doubles gate load.

The chain adds no verb and no PR body check.

### A blocking finding class

Trigger: the first blocking round (sd:10903).
Fix no single finding; run one class pass:

1. Name the class.
2. Enumerate every instance from the code, not from the findings.
3. Put each instance in a table in `design.md` or the PR body: step, state moved, failure, recovery, test.
4. Fix each row that lacks a recovery or a test, with a test that fails first.
5. Send the next round with the table.

Keep the pass within the change's own scope: a new reader, audit or entry kind becomes its own item.
If the table shows the PR does too much, split it.

## Flags and results

| Flag | Meaning |
|---|---|
| `--challenge` | Apply an adversarial design-challenge stance. |
| `--lens NAME` | Re-aim the prompt at a non-code subject (`research-brief`); the chain is unchanged, and a docs-only change is still read. |
| `--item NAME` | Select the active planning directory. |
| `--provider NAME` | Select one registry entry, without fallback. |
| `--explain` | Explain routing and eligibility without dispatch. |
| `--dry-run` | Print invocations without execution. |
| `--draft` | Apply draft routing. |
| `--json` | Emit structured results. |
| `--reuse-check` | Explicitly reuse eligible full-check evidence; otherwise run the deterministic gate. Never reuses in sd-workflow itself, which tracks no reuse declaration (`.github/sd-check-reuse.json`). |
| `--gate-check BRANCH` | Branch scope: run the gate as the local gate does against `origin/BRANCH`, leaving a gate receipt for the merge gate; `sd-ship prepare` passes it under `repo.ci = local`. |
| `--timeout SECONDS` | Set the per-phase timeout for the gate and each provider; unset, the gate gets 3600 and each provider 1800. |

| Exit | Meaning |
|---|---|
| 0 | Successful operation; inspect its operation and coverage before claiming completed review. |
| 1 | A finding meets the severity floor. |
| 2 | Invalid invocation or policy. |
| 3 | Preflight refusal prevents required coverage. |
| 4 | Rate limits exhaust the eligible chain. |
| 5 | Deterministic gate failure or insufficient completed reviews. |

Report completed versus requested coverage, `reviewed_by`, and every failed attempt.
One clean standalone pass is weak evidence.
Reviewers sample a change, so another pass at the same head can find what this one missed.
In sd:1986, two clean `--scope branch` passes preceded a real medium finding from the `sd-ship` review at that head.
Report a clean pass as one clean pass, not as proof that the head has no defect.
Keep `rate_limited` distinct from `unavailable`.
Malformed or oversized responses do not count as completed reviews.
Retain usable findings, including unknown locations and blockers beyond output limits.

Oversized input returns measured bytes, the existing limit, and the complete path inventory before checks or provider dispatch.
Its suggested split-branch groups are advisory.
They run no review partitions and provide no aggregate approval.
Do not truncate input or count the advisory plan as completed review coverage.

## Local conventions reach the prompt

The ordinary review prompt includes this checkout's `CLAUDE.local.md` block through `local_conventions` (R10-D7).
The receipt reports `local_block_prepended`.
A `threat-model:` line there gets its own paragraph: a finding outside it is advisory at most.
The prompt also lists this branch's rebutted findings from `sd-ship adjudicate`, newest first, capped; a reviewer raises one again only with new evidence in either form: a check run, or `path:line` and a quote of the current code.
Synthetic provider preflight excludes that block.
When findings contradict a convention, check the supplied wording before assigning blame.
Use `sd-rules --for <path>` for applicable repository rules.

A code review prompt also names up to 8 blocker classes this repository's ship receipts hold (pack sd:6028).
They come from the 14 days before the reviewed base, grouped by family and path, with the newest summary.
An unreadable database prints one stderr line, and the review runs without them.

## The `codex-json` entry is subscription-only (R10-D4)

`codex_preflight` requires `auth_mode == chatgpt` and no stored `OPENAI_API_KEY` field (R10-D4).
The child environment removes `CODEX_API_KEY` and `CODEX_ACCESS_TOKEN`.
Never export credentials or invoke the provider directly to bypass this refusal.
Fallback requires separately authorized, eligible entries.

Children receive only `PATH`, `HOME`, `LANG`, `TERM`, `TMPDIR`, actual OS `USER`, and registry-declared variables.
A `codex-json` entry receives the exact `CODEX_HOME` that passed authentication checks.
Environment filtering is not filesystem isolation.
Providers still run as the operator and can read accessible files.

The `claude-json` reader permits only Read, Grep, and Glob tools, with safe mode and no custom MCP servers.
It saves no session.
The `opencode-json` reader attaches that review file with `--file`, runs as a private agent whose permission map is default-deny with a read-only allow-list, so inherited MCP tools and every write, command and fetch are refused without being named, and takes its model from the registry entry, which must pin one.
Its temporary review file contains the exact diff and required untracked or planning contents.
URL providers receive equivalent material in the request.
Neither these arguments nor protocol fixtures prove OS read confinement.

## The skill-suppression key is measured, not assumed

The `codex-json` argv carries `-c skills.include_instructions=false`.
A build that does not know the key ignores it and exits 0.
So `sd-review` probes the binary with its `debug prompt-input` subcommand, offline, at about 1.4 seconds.
Read `codex_skill_suppression` in `--explain --json`, beside `codex_preflight`.
Its `state` is `suppressed`, `unsuppressed`, `unknown`, or `not_probed`.
The probe keeps the entry's start line whole and replaces its final `exec`.
Only a render in the measured shape, a list of `message` items, counts as an answer.
`unknown` means the binary could not be asked; never read it as either answer.
`not_probed` means no `codex-json` process started, and the `reason` says why.
An actual `codex-json` run carries its own measurement in that outcome's `diagnostic.skill_suppression`.
Its `version` is the first line `--version` printed, empty when the binary could not say; an `opencode-json` outcome's `diagnostic.version` is its probe's (sd:3499).
An inert key warns and never refuses; decide whether to review on that binary.

## Policy

Read the repo row's review declaration (`sd-db repo declare PATH review`), or use the built-in policy when it has none.
A database that does not answer refuses; it never falls back to the default (sd:5109).
Policy names paths, risk, and severity; it names no provider chain.
Retired `tiers`, `challenge_providers`, and `planning_providers` keys require removal.
The `never_skip` deny-list overrides docs-skip rules.

## setup-github

This is a separate, explicitly requested installation operation, not part of local review.
Mode `guest` cannot install the routing workflow (R10-D5); `full` and `minimal` can.
Before installation or drift checks, read `skills/sd-review/references/setup-github.md` in the sd-workflow checkout.
The workflow reports routing only; it requests no reviewer and posts no comment.

## Conditional procedures

Read only the applicable reference in the sd-workflow checkout:

- Before `--reuse-check`, read `skills/sd-check/references/check-receipts.md`.
  Reuse requires a tracked complete local-only declaration and a clean committed checkout.
  Legacy receipts always rerun; an unchanged HEAD alone is insufficient evidence.
- For URL-provider diagnostics, read `skills/sd-review/references/provider-recovery.md`.
  Synthetic preflight is a provider call, not a code review or an authorization shortcut.
- When judging regression evidence, read `skills/sd-review/references/test-evidence.md`.
  A passing test must fail when its target defect is introduced.

Ship fix verification uses `--scope branch --base <full ancestor SHA> --verify-report <prior JSON>`.
It reviews the committed fix range and current source for prior blockers.
Each prior blocker gets an id (`B1`, ...); it clears only when every completed reader returns `fixed` for that id with evidence.
The report must name that exact base.
Changed default-branch code remains within the reviewed tree diff.
This evidence does not authorize replacing a ship receipt with a claimed reviewed head.

## Never

- Never post findings, comments, labels, reviews, or check updates.
- Never review a different subject than the scope selected.
- Never invoke providers directly to bypass routing, authentication, consent, or severity checks.
- Never count an incomplete attempt as completed coverage.
- Never discard adverse findings during fallback.
- Never accept a repository path; cwd determines the checkout, and `-C <dir>` only changes cwd first (R10-D6).
