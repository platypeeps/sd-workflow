---
title: Build dispatch per lane host
created: 2026-11-06
item: sd:7933
branch: feat/sd-7933-build-per-host
---

# Design — build-per-host

The item's row holds status, progress and decisions; this page holds the shape.
The rulings are note #46608 on system sd:7932.

## Problem

The dashboard's Build refuses an item whose lane runs on a satellite:
"item N's lane runs on <host>; builds run on the hub only" (`build._repo_refusal`).
The operator wants to start those builds from the hub dashboard.
The satellite must build them itself, as only it runs that lane.
The database is the one channel: no SSH and no remote session.

Today one dispatcher, the hub's `build-dispatch` job, acts on every build run.
A second dispatcher on a satellite would reap, kill, watch and sweep the hub's runs,
and the hub's would probe a satellite pid on the hub and `kill_group` a hub pgid.

## Approach

A build run belongs to one host, read from `runner_run.owner` (ruling Q1).
A hub-lane run keeps `owner = "build-dispatch"`, the role, so a failover keeps it.
A satellite-lane run gets `owner = "build-dispatch@<lane_host>"`.
`build.run_host(owner)` is the part after `@`, or None for the hub.

`build.lane_hosts(connection)` is the set of lane hosts this machine builds for:
`{this_host()}` on a satellite, and `{None, this_host()}` on the hub.
A satellite is a machine with a `hub.json` whose connection the hub serves.
The file decides, not the transport: the wire harness runs the hub's tests
over a served connection, and they stay the hub's. A run is mine when its host is in that set.
That mirrors `ship._lane`: NULL is the hub's lane, a name is that machine's.
`build.lane_of(connection, host)` is the one rule for a single lane or run:
None (the hub's) for NULL and for this machine's own name on the hub, as
`sd_lane.ended_kind` reads it; the host's name otherwise. A satellite cannot
tell the hub's name, so it reads a lane named for the hub as another host's.

The hub cannot see another host's processes, so it never releases that
host's lease. A tick beats first, with `why` in its body: the reason it
claims nothing (no log folder, an unread load, a restore pending), else "".
`build.dispatcher_refusal` refuses a press when that host has no beat
under `STALE_SECONDS` (`build.dispatcher_gone`) or its newest beat names a
`why`. The hub's claim blocks a queued row only on `dispatcher_gone`: a
`why` may last one tick, such as an unread load, so the row stays queued,
and the Builds panel shows "<host> claims nothing: <why>" on it.
The hub's reap ends an open run blocked, with no probe or signal, only
when no beat comes from that host or from the run for `STALE_SECONDS`.
The run keeps its lease, a pending cancel named in its reason, so Retry
and the claim refuse its items until that host's own reap reads its groups
gone and releases it. A held run stays held. A run at a lane step waits on
that host's lane. A beat that names a `why` still counts for the reap,
since the tick reaps before it claims.

1. Press: `_repo_refusal` no longer refuses a lane host. A press queues for a
   satellite lane only while that host's `build-dispatch@<host>` beat is under
   `STALE_SECONDS` old; otherwise nothing there claims it and the item stays locked.
2. Claim: takes only queued rows whose repository's `lane_host` is in `lane_hosts`.
   It skips the others and blocks none; the claim writes the owner from the lane host.
3. Room: counts only this host's build and draft runs against `MACHINE_CAP`
   and `REPO_CAP` (ruling Q3). The interactive builders are this machine's already.
4. Dispatch, reap, held release, watch, sweep and the spawn loop act only on mine.
5. Drafts stay on the hub (ruling Q2): `draft` refuses a satellite lane, and a
   satellite tick runs no draft claim or reap and no stop-on-blocks. Auto-pickup
   runs on each lane host for its own lanes (sd:8393): `eligible` keeps this
   host's lanes, and the host's own room caps what it queues.
6. The failure push runs on the hub only, over every host's runs:
   one pusher sends each failure once, and the hub holds the notifier.
7. The tick's heartbeat key is `build-dispatch` on the hub and
   `build-dispatch@<host>` on a satellite, so one does not hide the other.
8. The Build dialog names "runs on <host>" for a satellite bundle, not the hub's room.
   The Builds panel row names the host and the log path, and reads no log
   on the hub's disk for it (ruling Q5).

Rejected: a `runner_run.host` column. It needs a migration, and the owner already
travels with every retry (`_settle`, `_health_unknown` copy it).

## Non-goals

- Drafts for satellite lanes (ruling Q2).
- The system half: the job on the work satellite, its `SD_HUB_ONLY_AGENTS` entry,
  its log root (ruling Q4). That lands in the system repository after this.
- Live log reads of a satellite run from the hub.

## Failure table

```text
Step | State moved | Failure | Recovery | Test
Press | build assignment queued for a satellite-lane item | the old refusal | none needed: the press queues for any lane; other refusals still refuse the whole selection | test_build.Queue.test_press_queues_a_satellite_lane_item
Press, no dispatcher there | nothing: the press refuses | a press queues and locks items no dispatcher claims | the press and the plan refuse, naming the host, without a beat under STALE_SECONDS; the Build dialog shows the beat's age and the toast names the host that claims each row | test_build.Queue.test_a_press_for_a_satellite_whose_dispatcher_does_not_beat_refuses_and_locks_nothing, test_build_panel.BuildRoutes.test_a_satellite_press_names_its_host_and_starts_no_hub_tick
Claim on the hub | hub-lane rows claimed, owner build-dispatch | the hub claims or blocks a satellite row | the claim skips a row whose lane host is not its own; the row stays queued | test_build.Hosts.test_the_hub_claim_skips_a_satellite_row
Claim on a satellite | its rows claimed, owner build-dispatch@<host> | it claims a hub row or another satellite's | the same filter; the hub's rows stay queued | test_build.Hosts.test_a_satellite_claims_only_its_lane
Claim, lane moved before it | the row goes to the host that holds the lane at claim time | a stale host from the press | the claim reads lane_host in its own transaction | test_build.Hosts.test_a_lane_move_before_the_claim_moves_the_claim
Claim, load unread | nothing | gate_load() is None on a host | no claim; the tick's why names the unread load; the next tick reads again | test_build_run.Tick.test_an_unread_load_says_why
Room | builders counted per host | the hub's runs fill the satellite's cap, or the reverse | room counts only runs whose host is in lane_hosts | test_build.Hosts.test_room_counts_only_this_hosts_runs
Spawn | a supervisor started for an open run with none | the hub spawns a satellite run's supervisor on the hub | the spawn loop takes mine only | test_build_run.Hosts.test_each_tick_spawns_only_its_own_runs
Worktree, build, review, fix | the satellite's supervisor walks its steps on its disk | supervisor lost | the satellite's reap retries once; the retry keeps the owner | test_build.Hosts.test_a_retry_keeps_the_satellite_owner
Reap and kill | outcome, kill, settle | the hub probes a satellite pid or kills a hub pgid by its number | reap and held release take mine only; the satellite reaps its own | test_build.Hosts.test_the_hub_never_signals_or_reaps_a_satellite_run
Cancel from the hub | cancel_requested on the run | no satellite tick runs | the run stays open with the request; the satellite supervisor's beat or its reap ends it cancelled | test_build.Hosts.test_a_hub_cancel_lands_on_the_satellite_reap
Enqueue | lane entry on the satellite | the lane moved after the claim: sd-ship refuses (LaneElsewhere) | the run ends blocked with the refusal; Retry queues it, and the next claim goes to the new lane host | test_build_run.Supervise.test_a_body_the_lane_refuses_blocks_with_its_reason (the same refusal path)
Lane watch | enqueued to cleaning or lane_failed | the hub runs lane list for a satellite repo and probes its pids | watch takes mine only | test_build_run.Hosts.test_each_tick_watches_only_its_own_runs
Sweep | a done bundle's worktree removed | the hub removes a path that names a satellite worktree | sweep takes mine only | test_build_run.Hosts.test_sweep_leaves_another_hosts_worktree
Report back | notes, run row, cost rows in the one database | the hub stats a satellite log path | the panel row names host and log path and reads no log for it | test_build_panel.Panel.test_a_satellite_run_names_its_host
Push failure | one ntfy push per failed run or entry | hub and satellite both push one failure | push_failures returns nothing on a satellite; the hub pushes every host's | test_build_run.Hosts.test_a_satellite_pushes_nothing
Drafts and pickup | draft runs, auto-pickup queue | a satellite tick claims a draft or picks hub items | the satellite tick skips pickup, stop-on-blocks, draft claim and draft reap; eligible skips a satellite lane | test_build.Hosts.test_auto_pickup_skips_a_satellite_lane, test_build_run.Hosts.test_a_satellite_tick_runs_no_draft_or_pickup
Pickup, satellite press queued | nothing | a satellite press the hub never claims holds hub pickup, forever with no satellite dispatcher | pickup holds only for a queued press of this host's lanes | test_build.Hosts.test_a_satellite_press_leaves_the_hub_pickup_running
Host identity | which lanes this tick acts on | a served connection on the hub reads as a satellite: the wire run claims, reaps and pushes nothing | hub.json and a served connection make a satellite; no hub.json is the hub | test_build.Hosts.test_the_hub_over_the_wire_is_still_the_hub, test_build_run.Hosts.test_the_hub_over_the_wire_still_pushes, the wire run of test_build, test_build_run, test_draft
Heartbeat | state heartbeat row | the satellite's beat hides a dead hub job | the satellite beats build-dispatch@<host> | test_build_run.Hosts.test_a_satellite_tick_beats_its_own_key
Older state | runs from before this change | an owner with no @ | reads as the hub's, as before | the existing test_build and test_build_run suites
Hub-named lane, press | assignment queued | a lane named for the hub needs a build-dispatch@hub beat the hub never writes | _item reads lane_of: the hub's lane needs no beat | test_build.Hosts.test_a_lane_named_for_the_hub_takes_a_press_pickup_and_draft_on_the_hub
Hub-named lane, pickup and drafts | auto-pickup queue, draft runs | hub_only refuses the hub's own lane | _repo_refusal hub_only reads lane_of | the same test (eligible, draft.queue_if_eligible)
Hub-named lane, pickup hold | nothing | a hub-named press is not counted as this host's | the hold reads lane_of | test_build.Hosts.test_a_satellite_press_leaves_the_hub_pickup_running (hosts filter now via lane_of)
Hub-named lane, claim | run owner | the owner becomes build-dispatch@hub, so the panel hides its log | claim's filter and run_owner read lane_of: the bare role | test_build.Hosts.test_a_lane_named_for_the_hub_claims_with_the_hubs_bare_owner
Hub-named lane, dashboard | plan, press answer, panel rows | the plan shows no room and runs on hub, the answer skips the kickstart, the panel reads no log, for build-dispatch@hub rows from before too | plan, answer, open and ended rows read lane_of | test_build_panel.BuildsPanel.test_a_lane_named_for_the_hub_is_the_hubs_on_the_panel
Hub-named lane, run sets | room, reap, watch, sweep, spawn, push | none: lane_hosts on the hub holds None and its own name | unchanged | test_build.Hosts.test_lane_of_reads_this_hubs_own_name_as_the_hub
Foreign queued row, host quiet | the assignment blocked with why, item free | the hub skipped it forever with no note | the hub's claim blocks it when dispatcher_gone | test_build.Hosts.test_the_hub_blocks_a_quiet_satellites_queued_row_with_why
Foreign queued row, lane moved after the press | the same | moved to a host that never beat | the same check: no beat is gone | test_build.Hosts.test_the_hub_blocks_a_row_whose_lane_moved_to_a_host_that_never_beat
Foreign open run, host quiet | outcome blocked with the reason, items blocked; run and lease kept | a missing beat read as stopped processes: a release lets Retry start a second builder in the same worktree | _strand blocks it, keeps the lease; that host's own reap releases it | test_build.Hosts.test_the_hub_blocks_a_quiet_satellites_run_and_keeps_its_lease, test_build_panel.BuildsPanel.test_retry_refuses_a_run_the_hub_blocked_for_a_quiet_satellite
Foreign open run, cancel asked | outcome blocked, the cancel named in the reason; lease kept | a release while that host's builder may still run | the same: blocked, not cancelled | test_build.Hosts.test_a_cancel_on_a_quiet_satellite_blocks_and_keeps_the_lease
Foreign held run, host quiet | nothing | a release on a probe that answers gone for anything | it stays held until that host reads its groups gone | test_build.Hosts.test_the_hub_leaves_a_quiet_satellites_held_run_held
Foreign run, supervisor still beats | nothing | the dispatcher died but its supervisor runs | the run's own beat keeps it | test_build.Hosts.test_the_hub_blocks_a_quiet_satellites_run_and_keeps_its_lease (after=60)
Satellite beats but claims nothing, press | its beat body names why | a fresh beat read as a dispatcher that claims: the press locks items nothing claims | the tick writes why in its beat; the press and the plan refuse with it | test_build_run.Hosts.test_a_tick_that_cannot_claim_says_why_in_its_beat, test_build.Hosts.test_a_press_refuses_a_satellite_whose_beat_says_it_claims_nothing
Satellite beats but claims nothing, queued row | nothing: the row stays queued | one tick's why (an unread load) blocks every queued row, and the operator presses again | the hub's claim blocks on dispatcher_gone only; the Builds panel names the why on the queued row; a stale beat still blocks | test_build.Hosts.test_a_queued_row_whose_satellite_claims_nothing_stays_queued, test_build_panel.BuildsPanel.test_a_queued_satellite_row_names_why_its_dispatcher_claims_nothing, test_v2_queue.TheView.test_a_queued_satellite_build_names_why_its_dispatcher_claims_nothing
Satellite beats, claims nothing, runs open | nothing | the hub blocks runs the satellite still reaps | _strand reads only staleness: the tick reaps before it claims | test_build.Hosts.test_a_satellite_that_beats_and_claims_nothing_still_owns_its_runs
Foreign run at a lane step | nothing | its branch waits on that host's lane | stays; that host's watch ends it when it returns | none: no state to settle here
Satellite returns after the hub ended its run | its supervisor's next write | it acts on a run the hub ended | build.write changes no row, so the supervisor stops; its reap releases the run once its groups read gone | test_build_run.Supervise.test_a_lost_run_stops_before_the_next_step
Rollback | the previous pack on the hub | its reap acts on every run and reads a live satellite pid as gone | before a rollback, cancel each open satellite run on the Builds panel and let the satellite end it | none: a procedure
```

## Acceptance criteria

- `python3 -m unittest tests.sd_db.test_build tests.sd_db.test_build_run tests.sd_db.test_draft` passes with 0 failures.
- `python3 -m unittest dashboard.tests.test_build_panel` passes with 0 failures.
- On `origin/main`, each new test above fails.

## Risks

- A satellite with no `build-dispatch` job leaves its rows queued for ever.
  The Build dialog names the host, and the system half installs the job.
- Two machines whose `hostname -s` agree would both claim one lane.
  `ship._lane` has the same rule; the lane host names are unique in the fleet.
- A rollback of the hub's pack while a satellite run is open is unsafe (last table row).
