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

1. Outside any lock: read each lane's queue (`queue_for` runs git)
   and the pending ids (`new_work`, a hub read).
2. Take `<lane root>/hosted-start.lock`, host-wide, blocking. It is
   held only for steps 3-5, which do no git and no waiting lock.
3. Count lanes another worker holds: a lane whose runner lock
   cannot be taken, whose queue could not be read, or whose lock
   cannot be probed (hold on doubt).
4. Decide: own running lanes plus held lanes below the cap, and,
   when anything runs, gate room (`lane_room`, the gate's queue lock
   read with `LOCK_NB`).
5. Take the chosen lane's runner lock and keep the open handle.
6. Release the start lock. Start the worker, which takes the
   handle over: a child through an inherited fd named by
   `SD_LANE_HANDED_LOCK` (checked by inode), an in-process worker
   through `HANDED`. The run closes its copy.
7. At lane end, or the run's end, close any handle no worker took.

Past the budget the same rule holds. A run waits only for its own
lanes, which end in time; with none running and still no room, the
lane gets a busy row and the next run settles it.

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

Rows marked open are not fixed at 911a76c89. Each blocker and
advisory from the four rounds is a row; its round head is in the
Test column when no test exists yet.

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 2-5 | none | two runs both read room before either worker locks its lane (round 48d7c28c4) | count, decide and take under one host lock | test_two_runs_choosing_at_once_never_pass_the_cap |
| 1 | queue cache | `queue_for` (git) ran under the start lock (round b6202f00b) | read every queue before the lock | test_no_queue_lookup_runs_under_the_start_lock |
| 1, 3 | queue cache | a failed queue read was cached and read as a free lane (round b6202f00b) | unknown counts as held; not cached; read again next pass | test_a_lane_whose_queue_cannot_be_read_counts_as_held_and_is_read_again |
| 3 | none | the runner-lock probe raises OSError | counts as held | none |
| 3 | none | open: an unreadable queue counts as held on every pass, so it holds a cap slot for the whole run (advisory 911a76c89 :4475) | proposed: a ConfigError or LaneError (not a lane here) counts zero and starts nothing; an OSError or git timeout counts as held | none: 911a76c89 |
| 3 | none | open: a lane's worker frees its runner lock before post-merge, so the lane reads free while post-merge runs and the cap undercounts (advisory 911a76c89 :4550) | proposed: post-merge holds a second per-lane flock that `held_elsewhere` also probes; not the runner lock (sd:3566) | none: 911a76c89 |
| 4 | none | `lane_room` took the gate's queue lock blocking under the start lock; a stalled gate stopped both jobs (round b095ecb60) | `machine_snapshot(wait=False)`: a held lock is no room | test_a_held_gate_admission_lock_stops_no_run_under_the_start_lock |
| 4 | none | open: `LOCK_NB` turns a gate's millisecond queue-lock hold into no room; past the budget that is final for the run (advisory 911a76c89 :4409) | proposed: retry `LOCK_NB` for up to 2 s before no room | none: 911a76c89 |
| 4 | none | past the budget a run started a zero-budget worker over the shared cap (round b095ecb60) | the same cap and room rule past the budget; wait only for own lanes | test_past_the_budget_a_run_starts_no_lane_over_the_shared_cap |
| 4 | none | a lane waiting past the budget, with this run's lanes running and the gate full | waits for its own lanes, then starts alone and only settles | test_a_lane_waiting_past_the_budget_settles_and_claims_nothing |
| 5 | runner lock taken | open: the probe found the lock free, `take_lane` then failed (a `new_work` probe or a plain `lane run` held it for an instant, or open failed); `claim_lane` returns None without the deadline check, so a take that keeps failing polls forever (round 911a76c89 :4565) | proposed: a failed take is a busy row for this pass, inside the budget and past it | none: 911a76c89 |
| 5-6 | runner lock taken | the run dies before its worker starts | the kernel drops the flock with the process | none: a kernel rule |
| 6 | fd passed to the child | the child fails to start | `child_lane`'s finally closes the run's copy | test_a_lane_child_that_never_starts_frees_the_lock_its_run_took |
| 6 | child holds the lock | the run's copy stays open | closed right after `Popen` | test_a_lane_child_takes_over_the_lock_its_run_took |
| 6 | fd names another file | the lock file was replaced | the child closes the fd and takes the lock as before | test_a_handed_fd_for_another_file_is_not_taken_for_the_lock |
| 6 | env carries the fd | post-merge saw `SD_LANE_HANDED_LOCK` (round b095ecb60) | `lane_main` drops it from its environ copy | test_a_lane_child_drops_the_handed_lock_from_its_environment |
| 6 | `HANDED` entry | an in-process worker never runs | released at lane end and in the run's finally | test_two_runs_choosing_at_once_never_pass_the_cap |
| 7 | `HANDED` entry | a run's finally closed another in-process run's handle (round b095ecb60) | `release_lane` closes only a lock this run took | test_two_runs_choosing_at_once_never_pass_the_cap |
| 1 | seen ids | open: the first pass reads a lane's ids before it waits for room; an entry enqueued meanwhile that its worker leaves pending starts a second worker at a rescan (round b6202f00b :4589) | proposed: read the ids inside the wait loop, before each claim | none: b6202f00b |
| any | test harness | a hung lock-holder helper hung the two-run test (round 48d7c28c4 :5982) | the helper's line is read under a 30 s select | test_a_second_run_counts_the_lanes_the_first_holds_toward_the_cap |
| any | none | open: review points 48d7c28c4 :4398 and b6202f00b :4314, :4605, :4472, left for later by the lead; their text is in the lead's review rounds, not on this branch | read them before the next round; add a row each | none |

## Slices

1. Pack sd:9332: the admission above, with every open row fixed
   fail-first. Alone, it changes nothing for one job: a run alone
   reads no lane held elsewhere.
2. System sd:9516: the `lane-run-b` example job and bulkmove holding
   its lock (fd 9). It lands any time; the job is installed by hand
   on the hub only after slice 1 lands.

## Acceptance criteria

- [ ] Every open row above has a fix and a test that failed on
      911a76c89.
- [ ] `test_sd_lane` in parts, `test_sd_lib` and
      `test_sd_gate_slots` pass with 0 failures.
- [ ] With both jobs installed on the hub, one day of both jobs'
      lane-run logs never shows more lanes running at once than
      `sd.lane_parallel`.

## Risks

- The shared cap is only as good as the lock probe. A worker that
  is not a hosted run's (a plain `lane run`) holds its runner lock
  and counts, but post-merge does not until the post-merge row is
  fixed.
- Hold on doubt can idle a whole run: every lane unreadable for one
  run means nothing merges until the next tick. Accepted: a missed
  tick costs 5 minutes; a cap overrun costs a gate.
- Two jobs cover one long lane, not two: with one long lane in each
  job's run, every tick of both starts nothing and the rest starve.
  Accepted by the operator's choice of the second job.
