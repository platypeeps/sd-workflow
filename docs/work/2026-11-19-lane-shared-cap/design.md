---
title: Two hosted lane-run jobs share one lane cap
created: 2026-11-19
item: sd:9332
branch: fix/sd-9332-starvation-only
---

# Design — lane-shared-cap

Parked at 911a76c89 after four review rounds (operator 2026-11-18).
The next round starts from this page, not from the branch's last
blocker.

## Problem

launchd starts no instance of a label while one lives. A hosted
`lane run --hosted` stops starting lanes at `RUN_BUDGET_SECONDS`
(40 min), but it lives until its last lane ends, up to the job's
120 min limit. Every `*/5` tick of `lane-run` in that window starts
nothing. On 2026-11-17 one lane ran from 22:45 to about 00:45 MST
while four other lanes sat pending.

## Approach

The operator chose a second job (2026-11-18 ~18:40 MST): system
sd:9516 adds `lane-run-b`, the same command on minutes 2, 7, ... 57.
While one job's run waits on a long lane, the other job's run starts
the idle lanes. The pack's part is admission: two hosted runs on one
machine must share one `sd.lane_parallel` cap and one gate-room rule.

Admission, one step per lane, in `run_parallel`:

1. Take `<lane root>/hosted-start.lock`, host-wide, blocking. It is
   held only for steps 2-3, which run no git, no hub read and no
   waiting lock.
2. Take the lane's claim, `<lane root>/hosted-<key>.lock`, one per
   checkout, with `LOCK_NB`; a claim not taken is a busy row.
3. Count every claim in the lane root another run holds, for this
   run's lanes or not (a claim that cannot be read counts as held). Decide: this run's running lanes
   plus those below the cap and, inside the budget while anything
   runs, gate room (`lane_room`, the gate's queue lock read with
   `LOCK_NB`). No room releases the claim.
4. Release the start lock. Read the lane's pending ids, then start
   its worker, which takes its runner lock as on main.
5. Hold the claim until that worker ended, post-merge included; the
   run's finally releases every claim, and the kernel drops them when
   the run dies.

Past the budget only the cap counts. A run waits only for its own
lanes, which end in time; with the cap full of the other run's, the
lane gets a busy row and the next run settles it.

No runner lock is probed and none is handed to the worker: a probe
of a runner lock made a starting worker, `other_lanes_idle` or a
manual `lane run` read the lane busy (rounds 48d7c28c4, b6202f00b),
and the hand-off fd needed four rows of its own. A manual `lane run`
does not count toward the cap, as on main.

Rejected:

- Hand-off past the budget (rounds 1-8 of the old branch): the run
  exits and detached lane children outlive it. Every round found
  another way a detached group or its lock outlived its owner. That
  work moved to sd:9392.
- Releasing a run-level lock at the budget: there is none. launchd's
  one-instance rule, not a lock, blocks the next tick.
- Holding the runner lock through post-merge: post-merge's repo-sync
  refresh waits on every lane's runner lock, so it would deadlock
  (sd:3566).

## Non-goals

- Ending lane process groups that outlive their worker: sd:9392.
- More than two hosted jobs, or a cap shared across machines.
- Installing `lane-run-b`: by hand on the hub, after this lands.

## Failure table

Round 911a76c89 left six rows open. This round closed each one,
removed the runner-lock hand-off and the queue reads from admission,
and added a row for each instance the class pass found. Each fixed
row's test failed on 8c15d6f70, which is 911a76c89 merged with main.

Classes from the blocking rounds, with every instance in the code:

- Work under the start lock: the claim take, the claim probes and
  the room read; rows 1-3.
- Unknown read as an answer: the claim probe, the claim take, the
  room read, the queue read, and `new_work`'s queue lookup and hub
  read; rows 4-7 and 44.
- A wait without a bound, or for nothing: the first pass and the
  rescan; rows 8-10, 25-26 and 46 (an end with no last scan).
- An "is there work" read that misses recovery: every `pending` call
  (first pass, claim's busy branch, begin, rescan) reads `new_work`,
  which reads `work_ids` and the queue file; rows 27-36, enumerated
  from `run_lane`: `settle_checkout`, `next_claim`, `release_chain`
  and `post_merge`.
- A count that ends before its worker, or misses one: post-merge, a
  dead run, a worker that outlives its run and a lane not in this
  run's list; rows 11-13 and 23.
- Seen ids: the first pass, a busy worker, the rescan and a busy
  claim; rows 14-16, 24 and 45.
- Past the budget: the cap and the room; rows 17-18.
- Probe side effects: runner-lock probes and a probe that a forked
  child keeps; rows 19-20.
- Steps: the claim, then the start; rows 21-22.

| # | Step | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 1 | 1-3 | two runs both read room before either worker locks its lane (round 48d7c28c4) | count, decide and take under one host lock | test_two_runs_choosing_at_once_never_pass_the_cap |
| 2 | 1-3 | `queue_for` (git) ran under the start lock (round b6202f00b) | admission reads no queue | test_no_queue_lookup_runs_under_the_start_lock |
| 3 | 3 | `lane_room` took the gate's queue lock blocking under the start lock (round b095ecb60) | `machine_snapshot(wait=False)`: a held lock is no room | test_a_held_gate_admission_lock_stops_no_run_under_the_start_lock |
| 4 | 3 | a claim probe raises OSError | counts as held | test_a_claim_that_cannot_be_read_counts_as_held |
| 5 | 2 | was open: the probe read the lane free, the take failed, and the claim waited with no deadline check (round 911a76c89 :4565) | a take that fails is a busy row, inside the budget and past it; a rescan retries it | test_a_lane_whose_lock_cannot_be_taken_after_a_free_probe_gets_a_busy_row |
| 6 | 3 | the room cannot be read | no room inside the budget; not read past it | test_no_free_gate_slot_defers_the_next_lane_until_one_ends |
| 7 | 1 | was open: an unreadable queue counted as held on every pass and took a cap slot all run (advisory 911a76c89 :4475) | admission reads no queue; the worker refuses with its own error | test_a_lane_whose_queue_cannot_be_read_takes_no_cap_slot_and_says_why |
| 8 | 3 | the first pass waits for room | bounded: inside the budget by the deadline, past it by this run's own lanes | test_past_the_budget_a_run_starts_no_lane_over_the_shared_cap |
| 9 | 3 | the start lock's holder stalls | its holders wait on nothing; the kernel drops it when one dies | test_a_held_gate_admission_lock_stops_no_run_under_the_start_lock |
| 10 | 3 | was open: an idle run's rescan found work with no room, and the run ended without a row (advisory b6202f00b :4605) | the lane waits for room inside the budget, as in the first pass | test_an_idle_run_waits_inside_its_budget_for_room_for_a_lane_with_new_work |
| 11 | 5 | was open: a worker frees its runner lock before post-merge, so the other run read the lane free (advisory 911a76c89 :4550) | the run holds the lane's claim until its worker ended | test_a_lane_in_post_merge_still_counts_toward_the_shared_cap |
| 12 | 5 | the run dies with claims held | the kernel drops them; its children die with the job's group, as on main | none: a kernel rule |
| 13 | 5 | a worker outlives its run: its lane counts no more | after sd:9392, which ends lane groups that outlive their run | none: sd:9392 |
| 14 | 4 | was open: the first pass read a lane's ids before it waited for room; an entry enqueued meanwhile started a second worker (advisory b6202f00b :4589) | the ids are read once the lane is claimed, just before its worker starts | test_an_entry_enqueued_while_a_lane_waits_for_room_starts_no_second_worker |
| 15 | 5 | a worker meets its runner lock held (a manual `lane run`, repo-sync, a probe) and answers busy | its ids stay seen, as on main, so the run ends once nothing new remains; the next scheduled run retries the lane. Clearing them restarted it every poll (lane review 1c8fb401b :4549) | test_a_lane_whose_worker_answers_busy_starts_once_and_the_run_ends |
| 16 | 2 | a lane gets a busy row before any worker ran | its seen ids are empty; a later scan starts it | test_a_second_run_counts_the_lanes_the_first_holds_toward_the_cap |
| 17 | 3 | past the budget a run started a zero-budget worker over the shared cap (round b095ecb60) | the cap holds past the budget | test_past_the_budget_a_run_starts_no_lane_over_the_shared_cap |
| 18 | 3 | was open: past the budget a run waited for gate room beside its own long lane, and a millisecond queue-lock hold was final there (advisories b6202f00b :4314, 911a76c89 :4409) | past the budget only the cap counts; the room is not read | test_past_the_budget_a_lane_starts_beside_the_run_s_own_lane_without_gate_room |
| 19 | 3 | was open: the cap probe took every free lane's runner lock each poll (advisories 48d7c28c4 :4398, b6202f00b :4472) | the cap reads only claims, which only hosted runs take | test_a_run_waiting_for_the_cap_takes_no_lane_s_runner_lock |
| 20 | 2 | a probe's flock lives on in a child forked meanwhile, so the take after it failed (found in this round, flaky under threaded lanes) | the chosen lane is taken first, never probed | test_a_probe_s_lock_left_in_a_forked_child_does_not_make_the_chosen_lane_busy |
| 21 | 2-4 | the start raises after the claim | the run's finally releases every claim | test_a_start_that_raises_after_the_claim_frees_the_claim |
| 22 | any | a hung lock-holder helper hung the two-run test (round 48d7c28c4 :5982) | the helper's line is read under a 30 s select | test_a_second_run_counts_the_lanes_the_first_holds_toward_the_cap |
| 23 | 3 | the cap counted only claims for this run's own lanes, so a lane its host read dropped, or one moved here, went uncounted while the other run held it (lead review 1b5580736 :4515, sd:9611) | count every live claim in the lane root | test_a_claim_for_a_lane_this_run_does_not_list_counts_toward_the_cap |
| 24 | 2 | a lane the other run claimed kept no seen ids, so every rescan wrote another busy row, about 80 a budget (advisory 1b5580736 :4537) | a busy claim records the lane's pending ids as seen; only a new entry tries it again | test_a_lane_the_other_run_claimed_gets_one_busy_row_across_rescans |
| 25 | 3 | rescans only added to the lanes waiting for room, so a lane whose only new entry was held, cancelled or drained kept an idle run, and its launchd label, to the deadline (lane review 08130e1f :4592, sd:9611) | each scan rebuilds the waiting lanes | test_a_waiting_lane_whose_new_entry_went_away_lets_an_idle_run_end |
| 26 | 3 | an idle run's first pass waited for room for a lane with nothing pending while the other run filled the gate (class pass on :4592) | an idle run moves on; a scan starts the lane once work arrives | test_an_idle_run_waits_for_no_room_for_a_lane_with_nothing_pending |
| 27 | 3 | `new_work` read runnable entries only, so the first pass skipped a lane whose only work a worker settles, tick after tick (lead review 6300d9049 :4629); rows 31-36 list each state | each state `run_lane` acts on is work, once a run (`work_ids`) | test_each_state_a_worker_acts_on_is_new_work_once_a_run, test_an_idle_run_waits_for_room_for_a_lane_with_only_recovery_work |
| 28 | 3 | the rescan read the same runnable-only ids, so a lane whose worker left its post-merge owed got no second worker (class pass on :4629) | the scan reads `work_ids` too | test_a_scan_starts_an_idle_lane_whose_worker_left_recovery_work |
| 29 | 3 | an owed release counted only beside an earlier chain, so a repository's first one, left by a worker that died first, waited for a later merge (lead review 74dc3f8e4 :4282) | the policy decides, read as `release_chain` reads it; a failed read counts | test_an_owed_release_is_work_only_where_the_policy_declares_release |
| 30 | 3 | `new_work` probed each lane's runner lock every 5 s poll, and the other run's starting worker answered busy (advisory :4603) | no probe; a lane another runner holds answers busy and keeps its ids seen; the first pass reads the hub once a rescan | test_new_work_probes_no_runner_lock |
| 31 | 3 | state: a runnable entry (`claim_entry`) | work | test_each_state_a_worker_acts_on_is_new_work_once_a_run [runnable] |
| 32 | 3 | state: a running entry whose holder on this machine died (`reclaim_dead`; `resume_stopped` and the note after it ride on it) | work | same test [dead holder] |
| 33 | 3 | state: an owed `make post-merge` (`owes_post_merge`) | work | same test [owed post-merge] |
| 34 | 3 | state: `release: owed` or an unfinished chain where the policy declares `release` (`release_chain`) | work | same test [first owed release], [unfinished release chain] |
| 35 | 3 | state: an earlier version's queue file, which only a run imports (`import_file_queue`; lead review 74dc3f8e4 :4282) | work | test_a_queue_file_an_earlier_version_left_is_new_work_once_a_run |
| 36 | 3 | states a worker leaves or only tidies: a held entry, a live holder, another host's holder past its lease (`claim_entry` refuses), a hub-stop record no running entry holds and a body copy no row names (deleted, nothing reads them) | no work: counted, each would start a worker every run for ever, or for nothing | test_what_a_worker_leaves_alone_or_only_tidies_is_no_work |
| 37 | 3 | a stored policy that was a non-empty array, string or number raised in the scan, and the hosted run crashed (lead review ec5ef47ae :4306) | a policy that is not an object counts as a failed read: work, once a run; None and a non-object `release` are no release, as `release_chain` reads them | test_a_malformed_policy_counts_as_work_and_never_raises |
| 38 | 3 | stored read: a running entry's `holder` that is not an object raised in `work_ids`, the run's finally TERMed every lane (lead review d2c34d435 :4288) | narrowed to no holder | test_bad_stored_json_on_the_admission_path_stays_its_own_lane_s [holder] |
| 39 | 3 | stored read: `release` and `post_merge` that are not objects (`work_ids`, `owes_post_merge`) | already narrowed: compared, then `isinstance` | same test [release], [post_merge] |
| 40 | 3 | stored read: the policy (`declares_release`) | row 37 | same test [policy] |
| 41 | 3 | stored read: an entry body that is no object, in `new_work`'s `stored` | that lane has work, once a run (`UNREADABLE_WORK`); its worker meets the row | same test [body] |
| 42 | 3 | stored read: the same body in `longest_waiting_first`, before any lane starts | the lane keeps its path order after the rest | same test [body] |
| 43 | 3 | any other error from one lane's `work_ids`, policy read or queue-file check | contained in `new_work`: that lane has work, the run goes on | same test, every case |
| 44 | 3 | the first pass's idle check read a lane whose queue lookup or hub read failed as nothing pending: it skipped the lane with an empty row, and its worker's refusal never showed (lead review ec5ef47ae :4672, sd:9611) | unknown is work, once a run (`UNREADABLE_WORK`): the worker starts and its refusal is the row; a hub fault in a rescan starts each idle lane once | test_a_lane_whose_queue_or_hub_cannot_be_read_is_work_once_a_run |
| 45 | 2 | a busy claim marked the lane's pending ids seen, so once the other run freed it with an entry left pending, this run never started it while its own lanes ran (lead review ec5ef47ae :4643, sd:9611) | the lane keeps its seen ids and joins `elsewhere`: each scan tries its claim, writes no second busy row, and starts it once free | test_a_lane_the_other_run_frees_starts_again_while_this_run_lasts, test_a_lane_the_other_run_claimed_gets_one_busy_row_across_rescans |
| 46 | 3 | a lane waiting for room got a busy row as the run's last lane ended, the waiting set emptied, and the idle run ended with no scan read after that lane ended (lead review ec5ef47ae :4695, sd:9611) | inside the budget an idle run ends only on a pass that scanned | test_a_run_that_goes_idle_reads_a_last_scan_before_it_ends |
| 47 | 2 | the busy-claim branch read `pending`, which took the runner lock, so a worker starting then answered busy (lead review ef961a008 :4543, sd:9611) | fixed at 74dc3f8e4 (row 30); the busy branch reads no `pending` now (row 45) | test_new_work_probes_no_runner_lock |
| 48 | 3 | every merge leaves `release: owed`, which a repository with no release policy keeps for ever, so each scan read that policy for nearly every lane (lead review d2c34d435 :4349, sd:9611) | the hosted run keeps each lane's declaration for the run; a failed read is work and is not kept | test_the_hosted_run_reads_each_lane_s_policy_once_a_run |
| 49 | tests | two tests passed when their run or callback never ran (lead reviews ef961a008 :842, ec5ef47ae :6141, :6219, sd:9611) | each asserts the run's rows | test_a_held_gate_admission_lock_stops_no_run_under_the_start_lock, test_a_lane_in_post_merge_still_counts_toward_the_shared_cap; each fails on a copy that skips the run |
| 50 | 4 | a read that flipped between unknown (`UNREADABLE_WORK`, a slow `git` in a queue lookup) and the lane's ids read as new work on every flip: each start replaced the seen ids with its own read, so the run restarted the lane to its deadline (gate on c91ddb47c, test_bad_stored_json_on_the_admission_path_stays_its_own_lane_s) | seen ids only grow in a run and take the ids the scan found: each flip starts the lane once | test_a_lane_whose_reads_flap_starts_again_at_most_once_for_them |
| 51 | tests | `bounded_nap` patched `time.sleep` for every module, so under load `subprocess`'s wait for that `git` spent the poll budget inside a queue lookup | only `sd_lane`'s sleeps count; others sleep for real | test_a_run_s_poll_budget_counts_only_the_lane_runner_s_sleeps |

Rows removed with the hand-off: the fd passed to a child, a child
that never starts, an fd that names another file, the env var seen
by post-merge (round b095ecb60 :63), and the `HANDED` entries. The
queue-cache rows went with the queue reads. The review point at
48d7c28c4 :4397 (a failed queue read retried every poll) went too.

## Slices

1. Pack sd:9332: the admission above, with every open row fixed
   fail-first. Alone, it changes nothing for one job: a run alone
   finds no claim held elsewhere.
2. System sd:9516: the `lane-run-b` example job and bulkmove holding
   its lock (fd 9). It lands any time; the job is installed by hand
   on the hub only after slice 1 lands.

## Acceptance criteria

- [x] Every open row above has a fix and a test that failed on
      8c15d6f70 (911a76c89 merged with main).
- [ ] `test_sd_lane` in parts, `test_sd_lib` and
      `test_sd_gate_slots` pass with 0 failures.
- [ ] With both jobs installed on the hub, one day of both jobs'
      lane-run logs never shows more lanes running at once than
      `sd.lane_parallel`.

## Risks

- A worker that is not a hosted run's (a plain `lane run`) takes
  no claim and does not count toward the cap, as on main; its runner
  lock still keeps one worker per lane.
- Hold on doubt can idle a whole run: every lane unreadable for one
  run means nothing merges until the next tick. Accepted: a missed
  tick costs 5 minutes; a cap overrun costs a gate.
- Two jobs cover one long lane, not two: with one long lane in each
  job's run, every tick of both starts nothing and the rest starve.
  Accepted by the operator's choice of the second job.
