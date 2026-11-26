---
name: sd-ship
description: Take committed work from a verified branch to a merged pull request, committing only enumerated paths; `/sd-ship open-prs` sweeps every open pull request.
disable-model-invocation: true
---

# sd-ship

Deliver only the enumerated scope.
Invocation authorizes its scoped commits, branch push, and merge, subject to execution permissions and existing gates.
Post-merge closeout removes the merged PR's safe branches, stashes, refs and stale worktrees without asking (operator ruling 2026-10-02).
Invocation alone authorizes no checkout deletion.
Use STE-Concise; report delivery state, decisive checks, blockers, and retained worktrees.

On `/sd-ship open-prs`, or "check open PRs" routed from sd-next, read `skills/sd-ship/references/open-prs.md` in the sd-workflow checkout and follow it: every open pull request, what holds it, and the lane action.

## Standing permission

Read `repo.runner_merge` on the repository row (`bin/sd-db repo list`, last field) before relying on standing permission.
`auto` means merge active, in-scope PR work without asking, unless the operator explicitly says wait.
Merge only through `sd-ship prepare` then `sd-ship merge`; the review lane and required CI are part of those gates.
`auto` never permits a merge that skips the review lane, such as a raw `gh pr merge` or a web squash.
When the value is `auto` and the gates pass, merge; do not ask the operator first.
`manual`, no row, or an unreadable database means ask the operator first.
Installation grants no permission.
Shared contributors do not revoke permission; existing ownership, protection, review, and CI gates still apply.
A refusal stops execution.
`prepare` takes no ship lock: it reviews, pushes, binds the pull request and writes the `ship:` row, each save checked against the row's revision (sd:1938).
A second `prepare` for an item whose review still runs refuses as `review_running`, naming the pid; after that process dies, `--retry-review` continues the pass.
When another ship operation owns the repository, rerun `merge`, or `prepare` on a merged record, with `--wait <seconds>`; do not write a retry loop.
`reconcile` and `adjudicate` also take the lock but cannot wait for it; rerun them once the holder is gone.
The refusal names the holder; a file under `ship-locks/` is not a hold.
A merge lane that ships one item across several commands holds the lane: `sd-ship hold --item ID --holder NAME [--for SECONDS]`.
The hold is taken under the ship lock and lasts `--for` seconds, 3600 by default and 86400 at most; rerun it to renew.
While it stands, `prepare` and `merge` for any other item refuse as `lane_held`, naming the item, holder and expiry.
The held item's merge ends it; `sd-ship release --item ID` ends it sooner, and only for the held item.
On an sd satellite, `merge`, and `prepare` on a merged record, refuse as hub-only; the hub's lane merges (sd:2679).
`sd-ship lane run` leaves a merged worktree and its branch in place: removal can race a live builder's open file handles.
`sd-ship lane run` merges an entry when `repo.runner_merge` is `auto`, or when it carries `--manual` (sd:3132).
Otherwise, or when the setting cannot be read, the entry stops `prepared` with `runner_merge_manual` or `runner_merge_unknown`.
Stop the builder, then run the entry's `remove` command, also in the item note.
Do not change gates to obtain a merge.
An existing manual operator path needs separate authorization; a gate refusal does not grant it.
Standing permission starts no background work and does not enable `runner_merge: auto` on the repository row.

Read `sd config get sd.external_reviews` separately.
Apply the sd-workflow checkout's `.claude/rules/sd-operator-defaults.md`.
Inspect the complete reviewer chain before expensive checks.
Do not substitute an unrequested provider.

## The sequence

Nine steps coordinate the executable operations below.
First verify the actual scope and inspect its check output.
A slice may ship with later item criteria open.
Only `--deliver` claims the whole item; for a work item, every acceptance criterion then needs evidence.
The first prepare of an item names its claim: `--deliver` on the item's last PR, `--associate-only` on an earlier one.
Without either flag, the first prepare refuses; a reprepare keeps the stored claim.
Small changes need no placeholder work item.
The checkout holds one writer: this session in its own worktree, or the runner in its clone.
The sd-workflow checkout's `WORKFLOW.md`, section **Parallel work**, is the rule; do not ship from a checkout another session is writing in.

1. **Commit enumerated paths only.**
   In the pack, system repository, and writing repository, follow `WORKFLOW.md` for the `Needed-by:` trailer.
   Its forms are `Needed-by: <item id>`, `Needed-by: cost`, `Needed-by: efficiency`, and `Needed-by: visibility`.
   A present trailer produces no warning.
   A missing trailer warns; the sequence continues with the commit.
2. **Review locally before publication.**
   Before the review, `sd-ship prepare` warns about open PRs and origin branches that name the item or change the same files (sd:1151).
   The warning refuses nothing; read it before the review spends a pass.
   Use `sd-review --scope branch --challenge` for the *code, before merge* point.
   A branch of only plain prose (not `skills/`, `bin/`, `tests/`, hooks, `docs/work/`, any dot-path or agent-instruction file such as `AGENTS.md`) or only `.sd-pack-rev` gets the gate alone; prepare says `review skipped: docs-only` or `pin-bump` (sd:2998).
   A code branch whose head a clean or advisory plain `sd-review --scope branch` passed (sd:9767), or a build's verify round after it (sd:9769), or reached since by clean far merges of `origin/main`, gets the gate alone; prepare says `lead review reused` or why not (sd:5988).
   Read its cap on that row in the sd-workflow checkout's `.claude/rules/sd-planning-adversarial-review.md`.
   Run `sd-docs-lint` against the built PR body.
   With no work root, use `--body-only`; do not create planning files to satisfy tree checks.
   Dispose every blocker.
   Commit fixes and verify the diff since the preceding reviewed head, including current source for prior findings.
   An incomplete review needs full-branch coverage.
   An unchanged complete review can clear through recorded rebuttals (`sd-ship adjudicate`).
3. **Push only the cleared head.**
   Push refuses unless the current sha matches the head the local review cleared.
   A changed head returns to review.
   The remote merge also checks the expected head.
4. **Open or reconcile the pull request.**
   Include `Work:` only when an associated item exists.
   The line is absent otherwise; it is not a completion claim.
   After exact-head confirmation, request Copilot when the effective policy selects the reviewed tier: `deep` (what unset reads) on deep-tier changes only, `always` on every reviewing tier, `never` on none.
   A `skip` tier is not requested under any policy.
   `--copilot-review request` permits one explicit request.
   `--copilot-review skip` needs explicit task direction and suppresses automatic selection.
   Suppression persists for the task until a later explicit request replaces it.
   Automatic selection applies only to full-owned, non-draft pull requests.
   Never repeat an automatic request on later pushes.
5. **Wait for CI once.**
   Start `gh pr checks <N> --watch --fail-fast` once in the background, or use the adapter's bounded watch.
   Do not start both.
   A failed check ends the run; a fix returns to step 1.
6. **Check dependent pull requests.**
   Run `gh pr list --base <this branch> --state open` before merging.
   A failed query or non-empty answer stops the run.
   The executable merge does not perform this query.
   Retargeting metadata alone leaves inherited commits in child diffs.
   Repair requires `git rebase --onto <base> <this branch> <child branch>`, an authorized force-push, and renewed review.
   Stop and obtain permission before that history rewrite.
7. **Merge through the authorized adapter.**
   It uses `gh pr merge --squash --match-head-commit <the reviewed sha> -t "<title> (#N)" -b "<body>"`.
   `<title>` is the pull request's current title, not the last commit subject.
   Keep the explicit title and body; exclude `wip:` subjects from main.
   Put contiguous trailers in the body's final paragraph.
   Never use the CLI's branch-deletion option.
   Missing protection remains a refusal; this workflow adds no exception.
8. **Record verified delivery.**
   An associated merge carries `Item:`; only whole-item delivery adds `Delivers:`.
   Each later `--item` on `sd-ship lane enqueue` names a co-delivered item; each gets `Delivers:` and closes on the merge (sd:3704).
   A body `Closes:` line is refused, and the title and commit `Delivers:` lines close nothing.
   A delivering enqueue adds each open item of this repository that a branch commit's `Closes: sd:N` trailer, in its last paragraph, or a `Lane review fix, <repo> sd:N` line of the body or a commit, names; a subject or prose names nothing, and a commit the branch reverts closes nothing (sd:5999). On a build bundle's branch, a member the operator cancelled on its own is never delivered: a citation of it is dropped and listed under `## Left open`, a title or Summary heading naming it is not demanded, and naming it as the item or with `--item`, or retrying a saved entry that closes it, is refused `member_cancelled_alone`; a member cancelled after the enqueue fails the entry with that code before prepare or before the merge; `sd-ship merge` refuses it right before it publishes, whatever path led there, and those earlier checks only refuse sooner (sd:9222). The body gets a `## Closed by citation` list, and each such item a note on how to reopen it.
   A delivering enqueue refuses a title or `## Summary` heading `<repo> sd:N` naming an open item of this repository missing from `--item` (sd:5375).
   Enqueue refuses `line_too_long` for a title over 63 characters, or a body or branch commit line over 72, outside a code fence or a bare URL (sd:5793).
   Git's default merge subject names the branch and can pass 72; merge main with `git merge --no-ff -m "chore: merge origin/main" origin/main`, and the refusal says so (sd:6342).
   Enqueue refuses `title_not_conventional` for a title that is not a Conventional Commits header, `<type>[(<scope>)][!]: <summary>`, with a `@commitlint/config-conventional` type (sd:6059).
   Enqueue refuses `head_moved` for a head the worktree or `origin/<branch>` moved past, and a docs lint failure the branch adds, before it pushes (sd:5989).
   An `--associate-only` claim closes nothing (sd:3365).
   A body `Refs: sd:N` line names a related or partial item; the merge leaves it open.
   Record the row only after the remote confirms the merge.
   Read `skills/sd-ship/references/delivery.md` in the sd-workflow checkout before delivery, cancellation, or completion reporting.
   A slice leaves its item open.
   No-item changes create no row or placeholder trailer.
9. **Complete post-merge closeout.**
   After every confirmed in-scope merge, read `skills/sd-ship/references/post-merge-closeout.md` in the sd-workflow checkout.
   Run `git fetch -p`; repository `delete_branch_on_merge` removes the remote branch first.
   Review remaining findings and inventory refs, branches, stashes, and worktrees.
   Remove the merged PR's local and remote branches, stashes, refs and stale worktrees when the reference's safety conditions hold, without asking.
   Record each target's full object ID or path in an `sd task note` before its removal; keep any target that fails a condition.
   Do not move another checkout's main branch.
   Report the branch, worktree, merge, and installation state separately.

A spec update is outside this sequence.
Update `docs/spec/` on the PR branch when documented behavior changes and the operator requests it.

## Executable interface

The Git repository enclosing cwd determines the target; `-C <dir>` before the
subcommand changes cwd first, as `git -C` does (R10-D6 names it).
Use the matching installed `sd_db` library.
`--database PATH` selects an explicitly provisioned receipt/provider database; otherwise use operator HOME.
The runner uses its provisioned interpreter.
Prepare's gate is the one full run (sd:5800); it reuses a pass only at the same head and binding (R15-D1).

For itemless work, reuse its stable review ID.
For genuinely new work, allocate it with `sd-ship review --no-item --create-record --assert-new-work --json`.
Use `--no-item --review-id ID` instead of `--item ID` for prepare, merge, observe, and reconcile.
Prepare reuses complete exact-head review evidence and recorded dispositions; otherwise it follows the same review gates.
Itemless merge requires `--manual` and `--expected-head SHA`.
After the merge, run itemless `reconcile` and `review --close-record REASON` from the default branch; the deleted feature branch needs no recreation.
Itemless publication rejects commit flags and whole-item delivery flags.
It creates no task row, `Work:` line, `Item:`, or `Delivers:` trailer.
A branch commit's `Delivers:` does not reach the squash; prepare warns and suggests `--item N`.
Never allocate another review ID to reset spent passes or discard history.

- `sd-ship prepare --item ID --deliver|--associate-only --json` reviews, pushes, and opens or reconciles the PR.
  It returns `ready_to_send` and never merges.
  `--title` and `--body-file` supply the PR description.
  Without `--title` or a stored title, a one-commit branch uses its subject; a longer branch is refused.
  Without `--body-file`, an open PR's live body is the description, found by branch when no receipt names one; reprepare preserves the delivery claim.
  Only an open PR binds: a merged or closed one that used the branch name is ignored, and prepare opens a new PR (sd:2656).
  The sd-workflow checkout's `WORKFLOW.md`, section **The path for a change**, lists the body lines sd-ship owns.
- `sd-ship body --item ID [--body-file FILE] [--pr N]` prints the body prepare would publish and its body lint.
  With `--pr N` and without `--body-file`, it lints that pull request's live body.
  It reads no sd state, calls GitHub only for `--pr`, and exits non-zero on a refusal or a lint failure.
- Optional commits require `--path FILE` for each file and `--message-file FILE`.
  Directories and a pre-populated index are invalid.
- `sd-ship merge --item ID --expected-head SHA --manual --json` performs an explicit operator merge; `--manual` is the one merge authority.
- Merge may run from any checkout of the repository, such as a lane's checkout on the default branch.
  When the checkout's own branch holds no receipt for the item, the item's one receipt names the branch.
  `--branch BRANCH` names it when the item has more than one receipt.
  From another checkout, `--expected-head` must equal the branch's tip fetched from origin; no checkout moves.
  Matching item/head and `runner_merge: auto` on the repository row are also required.
  An author assignment cannot use this authority.
- Merge requires fresh ownership, enforcing protection, current default branch, exact reviewed head, and passing required checks.
  `runner_merge: auto` on the repository row answers the sole-operator question, and only that one.
  Admin you do not hold, a fork, and a remote that cannot answer still refuse.
  A merge the row allowed records that on its receipt as `row_authorized_merge`.
  A ruleset bypass refuses unless every actor is a `DeployKey` and the repository declares that exact bypass list.
  Checks that are not strict refuse unless the repository declares that exact state.
  Each declaration is an `accepted_gaps` entry in the repo row's `status` declaration, read at merge time (sd:5109), id `bypass` or `strict`, pinning its own fact; a `strict` entry also pins `bypass`.
  Every fact it pins must equal the live state, read as the status report reads it.
  An app, a team, a role or an admin bypass still refuses, and `sd-ship` never accepts a declaration for a missing `pull_request` rule (R14-D1).
  The receipt's `protection.accepted_gaps` names the entries a merge honoured.
  GitHub's merge rules must also pass: `mergeable` true, and `mergeable_state` `clean` or `unstable`.
  `unstable` means a check the protection does not require is pending or failed; the required ones are still read.
  Under `repo.ci = local`, merge reads GitHub's answer up to five times over 30 seconds after posting `sd/local-gate`.
  A pull request GitHub reports BEHIND refuses as `base_moved`, before the local gate runs.
  Under `repo.ci = local`, prepare runs its check as the local gate does, and its pass leaves a gate receipt.
  The merge gate at the same head and binding, within 30 minutes, reuses it, and the status says `(reused)`.
  Inputs outside the repository (external makefiles, tool files, machine state) are not bound; that window is the accepted residual risk.
  The gate's bound is 3600 s in both, unless `--review-timeout` names one for prepare.
  A refusal returns `manualRequired: true`; it changes no protection and requests no reviewer.
- `--watch --wait-seconds 900` starts one bounded fail-fast CI watcher.
  Its persisted start prevents another automatic watch on rerun.
  Under `repo.ci = local` it starts none: the local gate runs inside the merge and is the wait.
- `sd-ship observe --item ID --json` reads receipt and remote state without changing files, refs, or database.
  Its `receipt` field holds the last review, a gate failure, and the local gate report; before a PR exists it reads the receipt alone.
- `sd-ship reconcile --item ID --json` fetches merge evidence in an owned clone.
  Observation alone does not establish ancestry.
  Reconciliation removes no branch, worktree, or clone.

Add `--reuse-check` to prepare or standalone review only when explicitly reusing eligible deterministic-check evidence.
Before opting in, read `skills/sd-check/references/check-receipts.md` in the sd-workflow checkout.
Reuse requires complete local-only dependencies and unchanged before-and-after identity; legacy receipts rerun.
The flag never reuses in sd-workflow itself: the pack tracks no reuse declaration (`.github/sd-check-reuse.json`), so its gate runs.
This flag does not reuse incomplete reviews or bypass source, policy, or receipt validation.

Add `--provider NAME` to prepare or standalone review only for an explicitly requested reviewer.
The selection applies to this invocation, without fallback or changes to the automatic order.
Inspect `sd-review --explain --json` with the same scope, database, history inputs, review modifiers, and `--provider NAME`.
Existing independence, capability, availability, consent, and spending checks still apply.
A conflicting selection cannot replace a completed receipt or grant another pass.
Omitting the flag preserves automatic selection for new dispatches and permits reuse of existing valid evidence.
Results report `review_selection.requested_provider` and the actual `reviewed_by` list from the retained report.
Read the recovery reference before retries, fix verification, or additional reviews with an explicit provider.

`--copilot-review auto` is the prepare default.
It resolves the decision at dispatch: the machine's `sd.copilot_review` as it stands then (`deep` when unset), which wins when it opts out (sd:1444) and is otherwise overridden by the repository's `copilot_review.automatic_deep` as the latest retained report naming it recorded it, applied to the tiers every retained pass recorded, so a later push's delta pass does not hide the branch's.
The report's own `automatic` verdict is what the policy said at review time, not the decision.
A setting changed after the review takes effect on the next prepare without another local review.
Each request binds one exact head and persists before merge.
Merge waits for a submitted review, stable review material, and verified finding dispositions.
An automatic request occurs once per pull request.
Later pushes still require a local review for the exact head and exact-head CI.
The completed Copilot review must cover the merge head, or an ancestor of it.
An ancestor clears only when the diff from it to the merge head touches nothing outside `docs/`.
Each such path must also be one the repository's policy lets skip: in `docs_skip` and not in `never_skip`.
`tests/` stays inside that surface, because a green suite cannot say a test still asserts what the reviewer approved.
A cleared ancestor is recorded as a merge warning naming it.
Use `--copilot-review request` when a later push makes the automatic review stale.
The adapter permits at most three Copilot requests per pull request.
At that cap, use existing history or a separately authorized manual abandonment.
Use `--abandon-copilot-review REASON` only during merge.
It stops waiting for the request basis on the current pull request and reviewed head.
The basis contains exact-head receipts when present.
Otherwise, it contains the latest receipt for that pull request.
No local receipt means there is nothing to abandon, and the command refuses.
It preserves request history and records a separate abandonment.
Only submitted, non-pending reviews mark matching request heads complete.
The merge refuses while any review thread is unresolved, outdated or not; a fix or a rebuttal reply, then a resolve, answers it (R14-D2, sd:3098).

The additive `workflow` object reports `schema_version`, `phase`, `state`, `blocker`, and `next_action`.
States are `success`, `retryable_failure`, `operator_decision`, and `policy_block`.
Blockers identify `code`, `boundary`, `retryable`, and `approval_required`.
Existing result fields and exit meanings remain authoritative; the new object does not grant permission.

Receipts bind repository, branch, item or review identity, exact head, tools, policy, and review history.
The tools bound are the `verdict` class in `bin/sd_ship_bindings.py`: the code `sd-review` runs, compared without comments or docstrings.
Gate and check code runs again on every `prepare` and `merge`, so a change there does not void a receipt.
When only review code moved, and none of it parses or disposes findings (`FINDING_FILES`), `sd-ship` replays `sd-review --explain` for the stored pass first.
An unchanged `request_sha256` keeps the receipt, spends no pass, and appends to `review_binding_kept`.
A moved binding refuses with "review tools or repository policy changed after review:" and names each changed file and its class.
Run `prepare` again; it re-reviews the same head in full.
`--expected-head` compares evidence; it does not replace evidence.
There is no `--reviewed-head` override.
An interrupted review retains its reserved pass.

## What a rerun reconciles

Resume the existing receipt rather than starting another review or PR.
A lost create or merge response requires reconciliation with the same remote operation.
An empty search after uncertain creation does not authorize another PR.
A merged PR is evidence to inspect, not permission to merge again.
Before interruption recovery, read `skills/sd-ship/references/recovery.md` in the sd-workflow checkout.
Read its review-retry section only when a review stopped or exhausted its automatic allowance.

## Recorded rebuttals

A blocking local review refuses with code `review_blocking`, naming each blocking finding in the error and in `findings`.
Its `next_action` names the `sd-ship adjudicate` command that prints each finding in full.
Record dispositions only for a complete review of the exact clean head with passing deterministic checks.
They cannot waive missing depth, incomplete transport, failed checks, or changed source.
Fixes still require verification on their new head.
A rejection that still stands after one review pass is recorded with `sd-ship adjudicate` before the next fix is pushed.
A rebuttal needs a reason a reader can check, not operator acceptance.
Before recording dispositions, read `skills/sd-ship/references/adjudication.md` in the sd-workflow checkout.

## Remote findings

Complete local review before any Copilot request.
Request automatically as the effective policy selects: `deep` (what unset reads) on deep-tier changes only, `always` on every reviewing tier, `never` on none, and a `skip` tier under none of them.
Honor repository restrictions and avoid repeated automatic requests after later pushes.
Read and disposition findings posted independently.

After push, the adapter can record `fixed <commit>` for findings supported by new changes to the named file.
Unchanged files remain unanswered; existing acknowledgements remain intact.
A person supplies `dismissed <reason>`.
Acknowledgement failures produce warnings, not review clearance.

## Never

- Never `git add -A`, `git add .`, or `git commit -a`.
- Never push a head the local lane has not seen.
- Never weaken checks, permissions, review depth, or ownership/protection gates to reach green.
- Never treat a written reason as executable clearance.
- Never delete a checkout, or a worktree, branch, stash or ref that fails a closeout safety condition or belongs to another PR.
- Never force a worktree removal.
- Never accept a repository path; cwd determines the checkout, and `-C <dir>` only changes cwd first (R10-D6).
- Never post reviews or labels in a guest upstream repository.
- Make no further change after settled-green.
  A new finding requires a new branch, not an amend or force-push.

A remote mode reduction records a reason once per item and remote.
That note grants no permission; the push or merge still stops.
Refresh commands and the matching shared library together before claiming installed behavior.
