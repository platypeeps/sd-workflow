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
2. Gate the top (`gate_top`) on the landing under it, as its prepare
   will gate it. The gate's own receipt is the top's. No result is
   replayed under another binding, so a pass whose receipt the gate
   withheld leaves none.
3. The stack is built in the runner's thread before the gate thread
   starts, where `predict` runs a speculation's git work, so a TERM or
   the deadline ends it. The thread runs only the gate, and a gate
   cancelled before it starts runs none. The train gate runs as a held
   gate beside the claimed entry's prepare, as a speculation does. The speculation skips the top, so no
   entry is gated twice. A member below the top that does not merge
   cancels the train gate (`settle_gates`).
4. Each member below the top gates its own tree, by its speculation or
   its prepare. A pass above does not show that a lower tree passes: A
   can break a check that B fixes. Members land one at a time, so a
   lower member can merge while the top stops.
5. Each member then ships through the existing prepare and merge. The
   merge gate posts `sd/local-gate` on each head.

What the train adds over the speculation: the top is gated past a
conflicting member, and up to N-1 landings deep where the speculation
stops at `DEPTH`. With N=3 and no conflict it gates what the speculation
would. It saves no gate run: members land one at a time, so every tree
that merges must pass its own gate. A saving needs the members to land
together, which needs one push of main (below).

## Acceptance criteria

- The top of a train of 3 is gated once, on the landings below it, and
  no entry is gated twice.
- The claimed entry's prepare runs beside the train gate.
- One red entry of 3 lands the other 2 and reports the red one.
- A stack conflict requeues only the conflicting entry.
- Every receipt the train leaves is the gate's own.

## Not in this change

- One combined review. Each entry keeps its own review. The review
  receipt is per pull request and per head; a combined review needs a
  new receipt kind.
- One push of main. Each pull request lands through GitHub's squash
  merge API, which branch protection and `Delivers:` trailers rely on.
  A local push of stacked merge commits would bypass both. Without it
  the train cannot save a gate run.

## Failure table

```text
| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 1 members read | none | hub fault on the read | no train; the entry ships alone | start_train catches LaneError |
| 1 members read | none | top already gated in this run | no second train | test_the_top_is_gated_on_the_landings_below_it_and_nothing_twice |
| 2 fetch base | origin/HEAD ref | fetch fails | members marked skipped; each ships alone | train_stack |
| 3 stack | scratch commits, unreferenced | a member conflicts | it drops, stays pending, the rest stack | test_a_stack_conflict_requeues_only_the_conflicting_entry |
| 3 stack | none | no tree key declared | members marked skipped; no gate | train_stack |
| 4 train gate | the gate's own receipt | receipt withheld | none recorded; prepare gates | test_a_pass_whose_receipt_the_gate_withheld_leaves_none |
| 4 train gate | none | gate fails | top marked failed; its prepare gates | test_one_red_entry_of_three_lands_the_other_two_and_reports_the_red_one |
| 3 stack | scratch commits | TERM or deadline mid-stack | stack runs in the runner's thread; it stops | test_the_stack_is_built_before_the_train_thread_starts |
| 4 train gate | none | cancelled before the gate starts | no gate; top marked cancelled | test_a_train_cancelled_before_its_gate_starts_runs_no_gate |
| 4 train gate | none | gate raises | top marked error; it ships alone | gate_top |
| 4 train gate | none | member below does not merge, TERM, deadline | gate cancelled | settle_gates, cancel_gate |
| 4 train gate | none | slow gate | prepare runs beside it | test_the_claimed_entry_prepares_beside_the_train_gate |
| 5 settle | none | a dropped member is still first | it is claimed beside the top's gate | test_a_dropped_entry_still_first_is_claimed_beside_the_top_gate |
| 5 settle | none | the dropped member fails | the top's gate is waited for | test_a_dropped_entry_that_fails_leaves_the_top_its_gate |
| 5 settle | none | a dropped member merges outside the top chain | the top gate is cancelled | test_a_merge_outside_the_train_chain_cancels_its_gate |
| 5 settle | none | queue read fails | no wait; the next prepare gates anew | test_an_unread_queue_after_a_merge_waits_for_no_gate |
| 5 settle | none | a move puts the gated entry ahead of a pending prerequisite | gate cancelled; no wait | test_a_follower_moved_ahead_of_a_pending_prerequisite_is_waited_for_by_no_one |
| 5 settle | none | a prerequisite is held | gate cancelled; no wait | test_a_prerequisite_that_cannot_land_first_ends_the_gate_unwaited [held] |
| 5 settle | none | a prerequisite is cancelled, failed, skipped or prepared | gate cancelled; no wait | test_a_prerequisite_that_cannot_land_first_ends_the_gate_unwaited [cancelled, failed, skipped, prepared] |
| 5 settle | none | a prerequisite merged outside this run | gate cancelled; no wait | test_a_prerequisite_that_cannot_land_first_ends_the_gate_unwaited [merged elsewhere] |
| 5 settle | none | a prerequisite left the queue (removed) | gate cancelled; no wait | test_a_prerequisite_that_cannot_land_first_ends_the_gate_unwaited [removed] |
| 5 settle | none | a prerequisite still runs | gate kept; no wait | test_a_running_prerequisite_keeps_the_gate_unwaited |
| 5 settle | none | a prerequisite is pending ahead of the gated entry | gate kept; no wait | test_a_pending_prerequisite_ahead_keeps_the_gate_unwaited |
| 5 settle | none | prerequisites merge out of the predicted order | gate cancelled; no wait | test_a_prerequisite_merged_out_of_order_ends_the_gate |
| 5 settle | none | every prerequisite merged in order | the gate is waited for | test_every_prerequisite_landed_in_order_is_waited_for |
| 2-3 fetch, stack | scratch commits, unreferenced | the deadline passes | no further member, no train gate | test_a_train_whose_fetch_crosses_the_deadline_starts_no_gate, test_a_stack_that_crosses_the_deadline_stacks_no_further |
| 1 members read | none | `DEPTH` gates still run | no train; members marked skipped | test_a_train_waits_while_depth_gates_still_run |
| 1-2 read, fetch | none | the members' read crosses the deadline | no fetch, no train | test_a_train_whose_queue_read_crosses_the_deadline_fetches_nothing |
| speculation setup | none | the queue read or a prediction crosses the deadline | no further prediction or scratch worktree | test_a_speculation_whose_queue_read_crosses_the_deadline_predicts_nothing, test_a_prediction_that_crosses_the_deadline_starts_no_further_setup |
| speculation setup | none | the deadline passes | no speculative gate | test_a_speculation_whose_fetch_crosses_the_deadline_starts_no_gate |
| 3 stack, landings | scratch commits, unreferenced | a git step crosses the deadline | no further git step but cleanup; setup skipped | test_a_member_whose_checkout_crosses_the_deadline_runs_no_more_git, test_a_speculation_whose_worktree_add_crosses_the_deadline_runs_no_more_git |
| launch | none | setup ends in time; the thread starts past the deadline | no gate; marked skipped | test_a_train_launched_past_the_deadline_runs_no_gate, test_a_speculation_launched_past_the_deadline_runs_no_gate |
| 5 lower member | its own receipt | A fails alone, A+B passes | A gates its own tree | test_the_top_is_gated_on_the_landings_below_it_and_nothing_twice |
| config | none | sd.lane_train invalid | reads 3 | test_lane_train_reads_the_machine_setting |
```

The train writes no push, no pull-request state and no item state. Its
only writes are the `train` field on members and the gate's own receipt.
