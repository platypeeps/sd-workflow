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
   each member merged at its `M_i` (fact 3). Once the PUT is sent,
   only proof decides: the PUT's own `merged: true`, or a read of the
   pull request merged, is the landing; a 4xx answer to the PUT, or a
   merge onto another base, is no landing. Anything else is unknown:
   the entry goes back at `step: train` and never merges alone.
7. Reconcile: each member runs `sd-ship reconcile`, which sends no
   merge. It reads `merged: true` and checks that `merge_commit_sha`
   is the receipt's `train_commit`. `Delivers:` on `M_i` closes the
   item; cleanup runs as today. A pending member's next claim
   reconciles it as well: `prepare` on a `train_joined` receipt whose
   pull request merged reconciles instead. While the train merge is
   in the fetched base and GitHub still reads the pull request open,
   prepare, join and merge refuse `train_landed` and write nothing.
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
| 1 mark | row train_preparing = queued head | the write fails | no prepare; the member drops, stays pending | lane: mark cannot be written |
| 1 mark | none | the worktree moved before the train (an amend) | no mark, no prepare, row not moved; its claim skips head_moved | lane: member the train never prepared |
| 1 prepare | a pending member's branch: a catch-up merge, or a rebase policy's rewrite, pushed | the train does not land, or stops | the row moves to the published head right after the prepare, mark dropped; its own claim starts there | lane: member caught up / rebased in a train |
| 1 follow | none (branch pushed, row at the old head) | the queue write after the push fails | the mark stays; the member's claim moves the row to the origin head that is the catch-up or rebase of the marked head | lane: follow write fails after a rebase push |
| 1 prepare | a pending member's worktree, rewritten, not pushed | its prepare refused after the rewrite | the row keeps the published head; its claim skips head_moved, as the lone lane does | lane: unpublished rewrite never queued |
| 1 prepare | none | the member was enqueued again or cancelled during its prepare | that write wins; the row is not moved; the member drops | lane: member enqueued again during its prepare |
| 1 prepare | a pending member's branch, rewritten | the run dies inside that prepare | the mark stays; its claim settles it as for a failed follow write | lane: follow write fails after a rebase push |
| 2 join | an unreferenced commit | a member conflicts | refused train_conflict; it drops, stays pending | ship: train conflict |
| 2 join | receipt train_joined | run twice | a second join writes the same M_i | ship: join twice |
| 2 join | none | allow_merge_commit off | refused; each ships alone | ship: no merge commits |
| 3 re-read | none | a member was cancelled | no landing; each ships alone | lane: cancelled member |
| 4 branch | sd-train ref | push fails | no gate, no PR; each ships alone | lane: train land fails |
| 5 gate | sd/local-gate on M_N | the gate fails | no PR; branch deleted; each ships alone | land_train: gate fails |
| 6 train PR | PR merged, main at final merge | answer lost | read the PR back; merged counts | land_train: lost answer |
| 6 train PR | PR merged | PUT says merged; the read after fails, breaks or lags | the PUT's merge is the landing | land_train: put merged, read-back fails / breaks / stale |
| 6 train PR | PR may be merged | PUT answer lost; the read after fails or says unmerged | unknown; back at step train; no lone merge | land_train: put answer lost, read-back stale / lost |
| 6 fence | lead row: step train, expected_head = the published head its prepare left, train_sent = train branch | runner dies after the PUT | reclaim puts it back pending at that head; its next prepare reconciles | lane: lead reclaimed after its rebase |
| 6 train PR | lead row back pending at its prepared head, train_sent kept | answer unknown | the next claim reads the train PR first: merged, prepare reconciles; open, the lane deletes its branch and waits; closed or none, train_sent goes, then a lone merge may run | lane: lead put back after its rebase; unsettled train sends no lone merge |
| 6 fence | each member row: train_sent, as the lead | a member is claimed before its lead settles | its own claim settles the train PR first; it joins no train until then | lane: fence marks each member; member of an unsettled train |
| 6 put-back | none: a fenced row keeps the fenced head | a put-back, a move or a hub-stop record names the head the claim read | back_to_pending keeps the row's head while train_sent is set; the claim's copy follows the fence | lane: hub-stop record of the old head; put-back from the claim's copy; lone merge put back after a failed train |
| 6 retry | train_sent moves to the item's new entry | the lead failed or was cancelled, then retried or enqueued again | add_entry takes the mark over in its own write | lane: new entry takes over its unsettled train |
| 6 cancel | none | a cancel or hold of a row whose train is not settled | refused train_merging until its claim settles it | lane: cancel or hold in an unsettled train |
| 6 settle | none: the mark stays | the train PR reads absent, or one element has another shape | unknown; the entry waits at its place | lane: train state by shape |
| 6 settle | none: the mark stays | the mark names no train branch | the entry fails; nothing clears the mark | lane: mark that names no train branch |
| 6 train PR | none | GitHub refuses the merge (4xx) | PR closed, branch deleted; ship alone | land_train: refused |
| 7 reconcile | none | train merge in the base, GitHub reads the PR open | prepare, join, merge refuse train_landed; no push, no PUT | ship: landed member GitHub reads open |
| 7 reconcile | none | git cannot say whether the train merge is in the base | retryable refusal; train record kept | ship: git that cannot answer |
| 7 reconcile | items done, PRs merged | stopped midway | next claim's prepare reconciles | ship: prepare reconciles |
| 7 reconcile | none | merge_commit_sha is not M_i | refused train_merge_mismatch | ship: mismatch |
| 7 reconcile | none | stale train_joined, lone merge | merge_dispatch clears train_commit | ship: lone merge after join |
| 8 cleanup | ref deleted | delete fails | stale ref; noted, nothing else | land_train |
```

## Not in this change

- One combined review: each member keeps its own (sd:9899).
- Landing across repositories: a train is one repository's queue.
- A claimed entry put back in a rebase-policy repository keeps its
  pre-prepare head: the lone lane's rule (sd:10223). A train lead put
  back at `step: train` follows its published prepared head.
