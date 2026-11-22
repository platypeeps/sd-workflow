---
title: Merge train, gate up to 3 queued entries as one stack
created: 2026-11-21
item: sd:9899
branch: build/sd-1456
---

# Design — lane-train

## Problem

The lane gates each entry alone. Prepare took a median 52 min in the
pack and 35 min in system over the last 30 entries. Three entries
queued for one repository run three full gates in a row.

## Approach

`sd.lane_train` sets the train size N, 1 to 9; unset reads 3. N=1 is
the lane before trains; nothing is replaced.

When the runner claims an entry, it takes that entry and the next
pending ones, up to N (`train_members`). Each member needs its worktree
on this host. An entry the runner may not merge ends the train.

1. Stack (`stack`): in a scratch worktree, merge each member onto the
   landing before it, from the fetched base, as `landings` predicts a
   speculation. A member that conflicts drops out of this train. It
   stays pending at its place and carries `train.status: dropped`.
2. Gate once: `sd gate check`'s gate at the top of the stack, against
   the fetched base, so its scope is the whole train.
3. On a pass, cover the top only (`default_cover`).
   `sd_gate_run.check_in_worktree` binds the top at its own catch-up
   merge and the landing under it, as its prepare will. It replays the
   train's `sd-check` report in place of a run. The report names the
   train. If the top's own scope differs from the report, the top gets
   no receipt, so its prepare gates it. A member below the top is
   `stacked` and gets no receipt. A pass above it does not show that it
   passes alone: A can break a check that B fixes. It can also merge
   while the top stops. So its speculation and prepare gate it.
4. On a fail, gate the first half again, down to one member. The second
   half carries `train.status: left`. A half of one runs no train gate:
   its own prepare gates it and reports the failure as today.
5. Each member then ships through the existing prepare and merge, one at
   a time. The top's prepare and merge gate read its receipt. Each
   lower member's tree runs its own gate, at most `DEPTH` of them early
   as speculations. The merge gate posts `sd/local-gate` on each head.

A speculation skips the top its train covered on the same chain
(`covered`). A claimed member whose train record still holds rides no
new train.
A cover holds while each member ahead of it merged. A landing that went
otherwise makes another tree, the receipt misses, and prepare gates.

## Not in this change

- One combined review. Each entry keeps its own review. The review
  receipt is per pull request and per head; a combined review needs a
  new receipt kind. File it as its own item if the gate saving is not
  enough.
- One push of main. Each pull request lands through GitHub's squash
  merge API, which branch protection and `Delivers:` trailers rely on.
  A local push of stacked merge commits would bypass both. The gate is
  the cost this change removes.

## Failure table

```text
| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 1 members read | none | hub fault on the read | no train; the entry ships alone | start_train catches LaneError |
| 2 fetch base | origin/HEAD ref | fetch fails | members marked skipped; each ships alone | ride: fork check |
| 3 stack | scratch commits, unreferenced | a member conflicts | it drops, stays pending, the rest stack | test_a_stack_conflict_requeues_only_the_conflicting_entry |
| 3 stack | none | no tree key declared | members marked skipped; no gate | ride: keyed check |
| 4 train gate | gate receipt at the top on the fork | gate fails | bisect the first half; the rest left | test_one_red_entry_of_three_lands_the_other_two_and_reports_the_red_one |
| 4 train gate | none | gate raises | members marked error; each ships alone | start_train run_train except |
| 4 train gate | none | run budget or TERM | thread cancelled; members marked cancelled | cancel_gate, as a speculative gate |
| 5 cover | one receipt, the top's | scope differs, binding moved | receipt_skipped; prepare gates | test_the_cover_replays_the_train_report_as_a_passing_check |
| 5 cover | none below the top | A fails alone, A+B passes | no receipt for A; A gates alone | test_a_pass_at_the_top_covers_no_lower_tree_it_never_gated |
| 6 ship members | PR merged, item delivered | a member ahead fails | its followers' receipts miss; each gates | covered(): after must be merged |
| 6 ship members | PR merged | all pass | one train gate for the stack | test_three_green_entries_land_with_one_gate_run_for_the_stack |
| config | none | sd.lane_train invalid | reads 3 | test_lane_train_reads_the_machine_setting |
```

The train writes no push, no pull-request state and no item state. Its
only writes are the `train` field on each member and gate receipts. A
receipt names a tree, so a wrong one is never read for another tree.
