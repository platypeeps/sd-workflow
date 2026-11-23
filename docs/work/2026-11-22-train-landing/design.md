---
title: Land a ready train in one train pull request, one gate run
created: 2026-11-22
item: sd:9923
branch: build/sd-1543
---

# Design — train-landing

## Problem

The train of sd:9899 gates the top of up to 3 queued entries once, but
each member still lands alone through GitHub's merge API. Every tree
that becomes main must pass its own gate, so the train saves no gate
run. A saving needs the members to land together, with one gate run
for the joined tree (operator 2026-11-21).

## Facts measured

The hub measured these on a scratch repository with the pack's `main`
protection (decision note on sd:9923, 2026-11-22):

1. A direct push of a train tip to `main` is refused (`GH006`: changes
   must go through a pull request), even with `sd/local-gate` success
   on the tip. Only an admin bypass accepts it, so the train does not
   push `main`.
2. A train landed as one train pull request merges inside protection:
   its head is the train tip, `sd/local-gate` success sits on that tip,
   and the merge API takes it with `merge_method=merge`.
3. GitHub then marks every member pull request merged and closed. A
   member's `merge_commit_sha` is the train merge commit that brought
   its head in; the train pull request's is the final merge.

## Approach

The runner lands a train of N ready members, 2 to `sd.lane_train`, in
these steps. Each step reuses a reader or writer that exists today.

1. Prepare: the claimed entry, then each pending member after it, runs
   `sd-ship prepare` as today: its own review, pull request and
   receipt. A member whose prepare stops leaves the train.
2. Join: `sd-ship merge --train-onto L` runs the member's merge checks,
   composes the message `sd-ship merge` lands today (title with `(#N)`,
   body, `Item:`, `Delivers:`, `Reviewed-base:`), and writes `M_i`, a
   merge of its reviewed head `H_i` onto L, with `git merge-tree` and
   `git commit-tree`: no worktree, no checkout, no push. L is the
   fetched `origin/<base>` F for the first member and `M_(i-1)` after.
   The receipt records `train_joined` with `train_commit`. A member that
   conflicts or refuses drops out and rides the next train.
3. Re-read: each joined member must still be pending at its queued
   head; one that was cancelled or moved ends the landing.
4. Branch: `M_N` goes to `refs/heads/sd-train/<M_N's first 12 hex>`.
5. Gate: the local gate runs once, on `M_N`, and posts `sd/local-gate`
   there (`sd_local_gate.local_gate`).
6. Train PR: one pull request, head the train branch, base `<base>`,
   opened and merged through `sd-ship`'s GitHub client (`land_train`),
   at `sha=M_N` with `merge_method=merge`. GitHub marks
   each member merged at its `M_i` (fact 3).
7. Reconcile: each member runs `sd-ship merge` again. It reads
   `merged: true` and runs the existing `reconcile`, which also checks
   that `merge_commit_sha` is the receipt's `train_commit`. `Delivers:`
   on `M_i` closes the item; cleanup runs as today. A pending member's
   next claim reconciles it as well: `prepare` on a `train_joined`
   receipt whose pull request merged reconciles instead.
8. Cleanup: the train branch is deleted; a missed delete leaves a ref
   the next train with the same tip pushes up to date.

The train lands only where the repository allows merge commits
(`allow_merge_commit`, read by the join's merge checks) and the runner
may merge each member (`merge_refusal`). Otherwise, and whenever a step
from 2 to 6 fails, each member ships alone, as after sd:9899: a later
`sd-ship merge` composes and records its own merge, so a stale
`train_joined` receipt is replaced, not reused.

## Rulings

1. Shape: merge commits (operator 2026-11-22, note #73823). Each member
   lands as `M_i`, so GitHub marks it merged and `reconcile` is reused.
2. Hold: the joined-tree gate releases the hold (#73823). `M_i`'s
   `Reviewed-base:` names L, the landing it merged onto, which the one
   gate covered as part of `M_N`; `held_at_merge` then reads it as not
   held. Main never sits at an `M_i` alone: the train PR moves it from
   F to the final merge in one step.
3. Members below the top keep their own prepare gate. A prepare without
   a gate needs a new flag; that saving is a later item.

## Acceptance criteria

- A train of 3 green members lands through one train pull request and
  one gate run on the joined tree; each member pull request reads
  merged and each item closes by its own `Delivers:`.
- A train PR GitHub does not merge lands nothing; each member ships
  alone.
- A member that conflicts drops out and stays pending.
- A repository that allows no merge commit starts no train landing.
- A run stopped after step 6 reconciles each member on the next run.

## Failure table

```text
| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 1 prepare | each member's PR and receipt | a member's prepare stops | it leaves the train; the rest join | lane: a member that fails prepare |
| 2 join | an unreferenced commit | a member conflicts | refused train_conflict; it drops, stays pending | ship: train conflict |
| 2 join | receipt train_joined | run twice | a second join writes the same M_i | ship: join twice |
| 2 join | none | allow_merge_commit off | refused; each ships alone | ship: no merge commits |
| 3 re-read | none | a member was cancelled | no landing; each ships alone | lane: cancelled member |
| 4 branch | sd-train ref | push fails | no gate, no PR; each ships alone | lane: train land fails |
| 5 gate | sd/local-gate on M_N | the gate fails | no PR; branch deleted; each ships alone | land_train: gate fails |
| 6 train PR | PR merged, main at final merge | answer lost | read the PR back; merged counts | land_train: lost answer |
| 6 train PR | none | GitHub refuses the merge | PR closed, branch deleted; ship alone | land_train: refused |
| 7 reconcile | items done, PRs merged | stopped midway | next claim's prepare reconciles | ship: prepare reconciles |
| 7 reconcile | none | merge_commit_sha is not M_i | refused train_merge_mismatch | ship: mismatch |
| 7 reconcile | none | stale train_joined, lone merge | merge_dispatch clears train_commit | ship: lone merge after join |
| 8 cleanup | ref deleted | delete fails | stale ref; noted, nothing else | land_train |
```

## Not in this change

- One combined review: each member keeps its own (sd:9899).
- Landing across repositories: a train is one repository's queue.
