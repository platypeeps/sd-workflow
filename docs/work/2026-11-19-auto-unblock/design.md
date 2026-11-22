---
title: An item that waits on another is built when that one is done
created: 2026-11-19
revised: 2026-11-20
item: sd:9570
branch: build/sd-1306
---

# Design — auto-unblock, read-time

Write this page only for a change that moves state in steps.
The item's row holds status, progress and decisions; this page holds the shape.

## Problem

An item that waits on another item says so in a free-text note: "waits on sd:N".
Nothing reads that note. When sd:N is done, the waiting item stays blocked
until someone notices, so a free builder never picks it up.
The operator approved a dependency field on 2026-11-19; it replaces those notes.

## Why a second design

The first design (379e5312f, ten review rounds) moved the dependent's
status inside the done's transaction. Every closer then had to say whether
it delivered, and each round found one more closer, pickup path or close
that skipped the rule (note #64476). The class: a write-time release must
be taught to every path that writes a done, and there are more than twenty.

This design writes nothing when a dependency finishes. The dependency is
read where a build is chosen, in one function the build paths call.

## Approach

- The dependency is data: `item.fields.blocked_by`, a sorted list of item
  ids, beside `fields.area` and `fields.asset`. Ids are global, so a
  dependency in another repository works. Kept from the first design.
- `sd task edit N --blocked-by M` (repeat for several) replaces the set;
  `--clear-blocked-by` clears it. It writes a note "Waits on sd:M".
  No new verb. The edit refuses, writing nothing, when the item is done,
  names itself, names a missing item, or makes a cycle among open items.
  A done item's stale set takes no part in the cycle check (folds in
  pack sd:9591).
- `build._item`, the check `build.queue` and `build.eligible` both run,
  refuses an item while `blocked_by` names an item that is not done:
  `BuildRefused("waits on sd:M")`. That one refusal gives:
  - auto-pickup skips it (`eligible` already drops what `_item` refuses);
  - a press `sd assignments build N` refuses and names what it waits on;
  - Retry, Fix and dispatch's start must reach `_item` too; where one
    does not today, the rebuild routes it there (row 5's test finds them).
- Nothing runs on a done. When the last named item is done, the next
  auto-pickup tick takes the dependent like any ready item, under
  dispatch's caps and load limit (ruling #63834).
- Status stays the operator's. To have an item built on release, set it
  `ready` when the dependency is recorded; the dependency holds it.
  A parked or planning item is never picked, as today.
- A done is a done: a merge, a hand close and a cancel all release.
  No delivered flag, no closer classification, no `fields.undelivered`.
- A reopened dependency holds its dependents again: the read sees it open.
- The dashboard's task Details list "Waits on": each id, title and status,
  read-only. The Queues ready row shows "waits on sd:M" in Detail.

Rejected: the write-time release of 379e5312f (above); a new
`item_dependency` table (a migration and a reverse script for one list).

## What the rebuild keeps and drops

Rebuild on main; reuse from the branch:

- keep: the `--blocked-by` edit, its validation and tests; the dashboard
  "Waits on" Details. The status-reason 400 fix (row 3i of the first
  table) is not needed: main already refuses a reason that is not text.
- drop: `writes.release`, the `_transition` hook, `delivered=` on every
  closer, `fields.undelivered`, `TheClosers`, the `writes.closing` change
  in `sd-ship`, and the unblock press.

## Non-goals

- No release while auto-pickup is off: a released item waits for a press,
  which then succeeds. The pickup switch stays the operator's.
- No dashboard control to set or clear a dependency; the CLI does it.
- A removed dependency stays a refusal ("sd:M no longer exists");
  clear it by hand.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 1. Edit records `blocked_by` | `item.fields`, one note | A named item is missing, done, the item itself, or makes a cycle among open items | The edit refuses and writes nothing | `test_a_refused_dependency_writes_nothing`, `test_a_done_items_stale_set_makes_no_cycle` |
| 2. `_item` reads the set | Nothing | `fields` cannot be read, or an id has no row | Refuse and name it: fail closed, never build | `test_unreadable_fields_refuse_the_build`, `test_a_missing_dependency_refuses_and_names_it` |
| 3. A dependency is done | Nothing | None: no write happens on the done | The next pickup tick reads it | `test_a_done_dependency_lets_the_next_tick_pick_the_item` |
| 4. A dependency reopens after a build was queued | Nothing | The queued build starts on a held item | Dispatch's start runs `_item`; the assignment ends refused with the note | `test_a_reopened_dependency_holds_a_queued_build` |
| 5. Every build path | Nothing | A path queues or starts without `_item` | A source-reading test lists every `assignment` insert and start and fails an unrouted one | `test_every_queue_path_runs_item` |
| 6. Satellite reads | Nothing | The hub connection drops mid-read | The read raises; the tick or press fails and runs again | `tests/sd_db/test_outcome.py` |
| 7. Older rows | Nothing | No row carries `blocked_by` | A row without the key reads as no dependency | `test_an_item_without_the_key_builds_as_before` |

## Acceptance criteria

- [x] `tests/sd_db/test_dependencies.py` passes the rows above.
- [x] `tests/test_sd_work.py` passes `--blocked-by` and `--clear-blocked-by`.
- [x] `dashboard/tests/test_v2_tasks.py` passes the "Waits on" Details case.
- [ ] pack sd:9591 closes with this change.

## Risks

- A released item builds only while auto-pickup is on. Accepted: the
  switch already decides that for every ready item.
- An item set `ready` with a dependency shows in Queues' ready list while
  it waits. The Detail column names the wait.
- `_item` runs once per ready row per tick; the set read is one `json_each`.
