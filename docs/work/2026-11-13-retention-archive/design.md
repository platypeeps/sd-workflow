---
title: Archive old checkpoint rows before the prune drops them, then VACUUM
created: 2026-11-13
item: sd:8655
branch: build/bundle-8655
---

# Design — retention-archive

Write this page only for a change that moves state in steps.
The item's row holds status, progress and decisions; this page holds the shape.

## Problem

The workflow database was 978 MB on the 2026-11-13 snapshot.
Checkpoint rows held 717 MB of it.
One key, `contribution-refresh-queue`, held 408 rows and 305 MB.
The `lane:` keys held 3565 keys and 98 MB; each key's first and newest row hold 37 MB.
Retention kept both whole (`KEPT_CHECKPOINTS`), and it deleted the rows it
did compact with no copy outside the database.
A deleted row also leaves free pages, so the file never shrinks.

The operator asked on 2026-11-13: clean up old unused data automatically,
export it first in case it is needed, and keep the database small.

## Approach

One victim query, three rules, all on `kind='checkpoint'`:

- `NEWEST_ONLY_CHECKPOINTS` (unchanged): every row but the newest, once the
  key's newest row is older than `CHECKPOINT_IDLE_AGE` (7 days).
- `NEWEST_AND_RESOLVED_CHECKPOINTS = ("contribution-refresh-queue",)`: every
  row but the newest and the newest resolved one, at any age.
  `contribution_sync._queue` and `removal._malformed` read only the newest
  resolved row; the newest row stays as every other rule keeps it.
  No age gate: the key is written every run, so its newest row is never idle.
- `FIRST_AND_NEWEST_CHECKPOINTS = ("lane:",)`: every row but the first and the
  newest, once the newest is older than `LANE_IDLE_AGE` (30 days).
  `enqueue_order`, `today` and `build` read `MIN(id)`; `stored` and
  `reads.lane_entries` read `MAX(id)`; `entry_times` reads the middle rows
  only for a key written in its 7-day window.

Archive, then delete, for every victim:

1. Read the victim ids (no lock).
2. Stream those rows into `<bulk root>/sd-workflow/db-archive/.<name>.<pid>.part`
   as gzip JSON lines; `F_FULLFSYNC` (or `fsync`) the file, rename it to
   `<name>`, fsync the folder.
   `<name>` is `<UTC date>-<backup run id>.jsonl.gz`: one file per prune.
   One file per run, not an append: a torn append would spoil the rows an
   earlier night verified, and the run id ties the file to its prune report.
3. Read the file back: the ids, timestamps, `resolved_at` and a SHA-256 of
   each row's raw key and body bytes must equal what step 2 wrote.
4. One `BEGIN IMMEDIATE`: read the victims again, and delete an archived row
   only if it is still a victim and its timestamp and `resolved_at` are the
   archived ones. No writer updates a checkpoint body.

A row's key and body are read as `CAST(... AS BLOB)` with `typeof(body)`:
a body that is not valid UTF-8 goes as `body_b64`, every other as `body`.

The bulk root is `sd.bulk_storage_root`, read by `bin/sd_lib.core_setting`,
the resolver `sd_operations.log_root` uses for build logs; no new key.
Unset: the prune deletes no checkpoint row and its report says so.
Malformed, not a folder, or an archive that fails: it deletes no checkpoint
row, and the report asks for a person (`attention`).

VACUUM: the backup job runs `retention.vacuum` after the prune passed.
It runs when the free pages hold 50 MiB or more, not when this prune freed
50 MiB: a VACUUM a held lock skipped then runs the next night.
It waits up to 60 s for the write lock (`busy_timeout`), never inside a
transaction, then runs `PRAGMA wal_checkpoint(TRUNCATE)`, and reports the
logical size (`page_count * page_size`) before and after.

Rejected: holding the write lock from the read to the delete. The first
night streams about 400 MB to gzip, and every other writer would wait out
its 5 s `busy_timeout` and fail. The second read in step 4 gives the same
answer without the hold.

## Non-goals

- No restore verb: `gzip -dc <file> | jq` reads the archive; a restore is an
  `INSERT` by hand.
- No archive pruning: the bulk root is the operator's store.
- No change to heartbeat, exec, report or park rules.
- No change to `operator-action`, `operations:` or backup run id rows.

## Failure table

- Step: read victims. State moved: none. Failure: a query error.
  Recovery: the prune fails before any delete, as before.
  Test: `test_a_failed_archive_write_deletes_nothing_and_the_report_says_why`.
- Step: write the part file. State moved: a part file in the archive folder.
  Failure: a write error, a full disk, a TERM.
  Recovery: nothing deleted; the part file is removed; the next run removes
  any part file left. Tests:
  `test_a_failed_archive_write_deletes_nothing_and_the_report_says_why`,
  `test_a_stale_part_file_is_removed_by_the_next_run`.
- Step: rename to the final name. State moved: an unverified archive.
  Failure: a stop before the read-back. Recovery: nothing deleted; the next
  run archives the same rows under its own run id and deletes them.
  Test: `test_a_stop_after_the_archive_before_the_delete_is_finished_by_the_next_run`.
- Step: read the archive back. State moved: none. Failure: a short file or
  a changed row. Recovery: the file is removed, nothing deleted, the report
  asks for a person. Test:
  `test_an_archive_that_reads_back_short_deletes_nothing`.
- Step: delete in one transaction. State moved: the victim rows.
  Failure: a stop mid-delete. Recovery: the rollback keeps every row; the
  next run archives them again and deletes them. Test:
  `test_a_stop_inside_the_delete_rolls_back_and_the_next_run_finishes`.
- Step: delete, with a key written since step 1. State moved: none for that
  key. Failure: a revived key would lose its middle rows. Recovery: the
  second read finds it is no victim; the archive holds a copy only.
  Test: `test_a_key_revived_between_archive_and_delete_keeps_its_rows`.
- Step: delete, with a row resolved since step 1. State moved: none for
  that row. Failure: the archive would hold an older `resolved_at`.
  Recovery: the row stays for the next night. Test:
  `test_a_row_resolved_after_its_archive_is_kept`.
- Step: no bulk root. State moved: none. Failure: unset, malformed, or not
  a folder. Recovery: no checkpoint row deleted; the report says why.
  Tests: `test_no_bulk_root_deletes_no_checkpoint_row_and_the_report_says_so`,
  `test_a_bulk_root_that_is_not_a_folder_deletes_nothing_and_asks_for_a_person`.
- Step: the prune report. State moved: the report item. Failure: a stop
  after the delete. Recovery: the rerun deletes nothing more and files the
  report, as before. Test:
  `test_a_prune_stopped_after_the_compaction_is_finished_by_the_next_run`.
- Step: VACUUM. State moved: the whole file, atomically. Failure: another
  writer holds the lock past the wait. Recovery: VACUUM changed nothing;
  the job exits 1 naming it; the free pages stay and the next night runs it.
  Test: `test_a_writer_holding_the_lock_skips_it_and_the_next_night_runs_it`.
- Step: WAL checkpoint. State moved: the WAL into the file. Failure: a
  reader holds an older snapshot. Recovery: the job says the file shrinks
  at a later checkpoint; SQLite's own checkpoints finish it. Test:
  `test_a_reader_holding_a_snapshot_leaves_the_checkpoint_unfinished`.
- Step: older state. State moved: none. Failure: a store from before this
  change, with whole `lane:` and queue keys. Recovery: the first night
  archives them like any other victim. Test:
  `test_the_job_archives_to_the_bulk_root_and_vacuums`.

## Acceptance criteria

- `python -m unittest tests.sd_db.test_retention tests.sd_db.test_retention_archive`
  passes with 0 failures.
- A pruned row's archive line decodes to the row's exact key and body bytes.

## Risks

- VACUUM holds the write lock while it runs: about the time to copy the file.
  A writer without `database.Patient` that waits past its 5 s `busy_timeout`
  fails. The job runs at night, and only when 50 MiB are free.
- VACUUM needs free disk of about twice the file: a temporary copy and a WAL
  as large as the file until the checkpoint.
- A hung bulk root hangs the archive write; the backup job's time bound
  stops it, and nothing is deleted before the read-back passed.
- The read-back reads the file through the page cache, so it proves the
  bytes, and the full fsync before it is what proves they are on the disk.
- Evidence: the delete is tied to the archive by row id, timestamp and
  `resolved_at`, and to the prune by the backup run id in the file name.
