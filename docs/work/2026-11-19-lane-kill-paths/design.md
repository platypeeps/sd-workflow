---
title: Every lane KILL path ends the groups below its target
created: 2026-11-19
item: sd:9392
branch: fix/sd-9392-kill-paths
---

# Design — lane-kill-paths

## Problem

`sd-ship prepare` and `merge` run in a session of their own
(`sd_lib.run_group`), and so do their review, `sd-check` and the
speculative gate. A SIGTERM unwinds through `term_ends_group`, and each
layer ends the layer below it. A SIGKILL unwinds nothing. Each KILL
path on `origin/main` (d623828d1) signals one group or one process, so
every session below its target runs on with ppid 1. The next lane run
then reclaims the entry while the old prepare still runs.

The sd:9332 branch fixed parts of this, but none of it landed: its
`arm_term`, `end_overdue`, `stop_groups`, `lane_held` and `.left`
records are not on main. This page starts from main's code.

## Approach

Read the groups below a target before its SIGTERM, and read them again
before its SIGKILL. The KILL then reaches every group in either read:

- `sd_lib.groups_below(root)`: one bounded `ps -A` read. It walks
  parent links from every member of group `root`, or from the process
  `root` when that is an unreaped child. It returns each group found,
  apart from `root`'s and this process's own, with its leader's start.
- `sd_lib.kill_below(root, earlier)`: a fresh read, then SIGKILL to
  each group it finds and each group of `earlier` that is still the
  same group. Its leader has the start read then, or it had no leader
  then and has none now. A group whose leader start differs is a
  reused number and gets no signal.

The first read catches a session whose parent ends on the SIGTERM:
after that it hangs off pid 1, and no walk finds it. The second read
catches a session started during the grace.

The SIGTERM path stays as it is. Each layer still ends its own groups
first (sd:2978). Only the SIGKILL reaches further.

Rejected: record each ship group's pgid beside the runner lock (the
item's first idea). It is a new state file. It is only needed when an
outside SIGKILL lands before the pack's own unwind ends; see row 6.

## Non-goals

- A record of groups for a later run to end. Row 6's residual is
  the lane-run job's 30 s cron grace, system sd:9571.
- `sd_opencode` and `sd_db.build` kills. `build.kill_group` already
  reads the groups below its target.

## Failure table

Rows are KILL paths on main, then the sd:9332 advisories read against
main. "n/a" means main does not have that code.

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 1 `sd_lib.end_group` KILL (run_group: prepare, merge, post-merge refresh) | leader group signalled | a session the leader started survives a KILL of the leader's group | read below before TERM and before KILL; KILL both | test_end_group_kills_a_session_its_leader_left |
| 2 `SpeculativeGate.cancel_gate` KILL | gate group signalled | the gate's `sd-check` sessions survive | read below at the first TERM; KILL them with the group | test_a_gate_kill_ends_the_sessions_below_its_group |
| 3 `ThreadGroups.end_groups` KILL (sd-review threads) | registered groups signalled | a session below a thread's group survives | the same read before TERM and KILL | test_thread_groups_kill_ends_the_sessions_below |
| 4 `sd-ship` `review_process` KILL | review group signalled | `sd-review`'s own sessions survive | the same read before TERM and KILL | test_a_killed_review_ends_the_sessions_below_it |
| 5 `end_child` KILL (hosted run, `CHILD_STOP_SECONDS`) | lane child process killed | its ship and gate sessions survive | read below the unreaped child before TERM and KILL | test_a_killed_lane_child_leaves_no_session_below |
| 6 cron `JOB_TIMEOUT`: TERM to the job group, KILL after `CRON_JOBS_TIMEOUT_GRACE` (10 s) | lane child and hosted run | the child's unwind (5 s per group, then up to 10 s per gate after its TERM) can outlast the grace; the cron KILL then leaves the gate's sessions | a gate's wait after TERM counts from its first TERM, sent as the TERM lands; the unwind then ends in about `GROUP_CLEANUP_SECONDS` | test_a_termed_run_ends_inside_the_cron_grace |
| 6 residual | none | an outside KILL that lands before the unwind ends (grace below 6 s, a stalled `ps`) leaves sessions with no record | recovery: lane-run cron grace 30 s (system sd:9571); no group record (ruling, sd:9392 note 63749) | none: system side |
| 7 hand run, SIGHUP (pack sd:9568) | none | a terminal close killed `sd-ship lane run` with no unwind | `term_ends_group` owns SIGHUP while it is default and unwinds as for TERM; `nohup` keeps it ignored | test_a_sighup_ends_the_group_as_a_sigterm_does, test_an_ignored_sighup_stays_ignored |
| self-KILL (`arm_term`) | n/a | not on main | none | none |
| `end_overdue` | n/a | not on main | none | none |
| advisory 4667: post-budget settle skips `release_chain`/`post_merge` | n/a | main runs both after every run that does not raise | none | none |
| advisory 4402: `stop_groups` calls `answer_locked` unguarded | n/a | neither is on main | none | none |
| advisory 4456: `arm_term` tests group, not session | n/a | not on main | none | none |
| advisory 4474: pid identity read after the descendant read | read below | a reaped pid may be another process | the walk starts at an unreaped child or at group members; a group from the first read is signalled only when its leader start matches | test_a_reused_group_number_is_not_signalled |
| advisory 4336: `lane_held` hold on doubt | n/a | not on main | none | none |
| advisory 5551: tests patch `time.sleep` process-wide | n/a | main's lane tests patch `sd_lane.time` only | none | none |
| reads | none | `ps` fails or times out | unknown: the KILL still reaches the target's own group, groups from no read are signalled, and stderr says so | test_an_unread_table_still_kills_the_leaders_group |

### Class pass: unwind steps that skip the captured SIGKILL

Lead review of 5541a2221. Each kill path, read from the code. Rule 1:
groups read before the TERM always get the start-checked SIGKILL pass,
whether or not the parent ended on its own. Rule 2: no diagnostic
write in a kill path raises. Rule 3: one table read per unwind,
shared by every group.

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| `end_group` | leader group | none: its loop always runs the SIGKILL step, and `kill_below` runs whenever the first read found a group | none needed | test_end_group_kills_a_session_its_leader_left |
| `review_process` | review group | none: its loop always runs the SIGKILL step | none needed | test_a_killed_review_ends_the_sessions_below_it |
| `ThreadGroups.end_groups`, rule 1 | registered groups | a group whose leader ended on the TERM left the set, so only groups still registered were killed | every live group's read sessions get the pass at the deadline | test_end_groups_kills_what_a_term_quitter_left |
| `ThreadGroups.end_groups`, rule 3 | none | one read per group, up to 3 s each, before any TERM | one read before the TERMs, one before the KILLs, shared | test_end_groups_reads_the_table_once_for_every_group |
| `cancel_gate`, rule 1 | gate group | a gate that ended on the TERM returned True with no SIGKILL pass | `kill_left` runs on that return | test_a_gate_that_ends_on_term_still_kills_what_it_left |
| `signal_gate` SIGKILL, rule 1 | gate group | `started(None)` cleared `group` before the SIGKILL, which then skipped the pass | the pass uses the group the read was under | test_a_gate_kill_after_its_group_cleared_still_kills_what_was_read |
| `end_child`, rule 1 | lane child | a child that ended on the TERM returned after `wait` with no pass | the pass runs on the first read only; the reaped pid roots no walk | test_a_lane_child_that_ends_on_term_still_has_what_it_left_killed |
| `kill_below` stderr line, rule 2 | none | EIO or a closed stream raised before the leader's SIGKILL | the write suppresses OSError and ValueError | test_a_diagnostic_that_cannot_be_written_stops_no_kill |
| `signal_gate` read, rule 3 | none | one read per gate before its TERM, at most `DEPTH` (2) gates | kept: one bounded read per group is what rule 1 needs | none |

Rule 3 says TERM first, then read. The read stays before the TERM: a
leader that dies on the TERM at once hangs its session off pid 1
before any later read. `test_end_groups_kills_what_a_term_quitter_left`
uses such a leader and fails with the read after the TERM.

## Acceptance criteria

- [ ] Each test named above failed on d623828d1 and passes on the head.
- [ ] `tests/test_sd_lib.py`, `tests/test_process_groups.py` and the
      touched `tests/test_sd_lane.py` classes pass with 0 failures.

## Risks

- A daemon that a group starts on purpose, and that has not yet left
  its parent, dies with the group's KILL. A daemon that forked away
  hangs off pid 1 and no read finds it. Accepted: a KILL means the
  owner gave up on the whole tree.
- `ps` costs about 20-50 ms per read, twice per KILL path. Accepted.
- Evidence: each signal is tied to its subject by the leader's start
  time read in the same `ps` call; a mismatch signals nothing.
