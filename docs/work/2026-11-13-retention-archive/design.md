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
transaction, then runs `PRAGMA wal_checkpoint(TRUNCATE)` with the
connection's own 5 s timeout, and reports the logical size
(`page_count * page_size`) before and after. A WAL file a reader kept at
50 MiB or more is checkpointed the next night, VACUUM or not.
A VACUUM that did not run exits the job 1, naming why.

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

All tests are in `tests/sd_db/test_retention_archive.py` unless named with `test_retention.`.

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| read victims | none | a query error | the prune fails before any write | `test_a_failed_victim_read_removes_nothing` |
| write the part file | a part file | a write error, full disk, TERM | nothing deleted; the part file is removed, and the next run removes any left | `test_a_failed_archive_write_deletes_nothing_and_the_report_says_why`, `test_a_stale_part_file_is_removed_by_the_next_run` |
| rename into place | an unverified archive | a stop before the read-back | nothing deleted; the next run archives the rows under its own name and deletes them | `test_a_stop_after_the_archive_before_the_delete_is_finished_by_the_next_run` |
| rename into place | none | the name is taken | nothing deleted, the old file kept, the report asks for a person | `test_an_archive_name_already_taken_deletes_nothing` |
| read back | none | a short file or other rows | the file is removed, nothing deleted, the report asks for a person | `test_an_archive_that_reads_back_short_deletes_nothing` |
| delete, one transaction | the victim rows | a stop mid-delete | the rollback keeps every row; the next run archives and deletes them | `test_a_stop_inside_the_delete_rolls_back_and_the_next_run_finishes` |
| delete | none for a revived key | a key written since the read would lose rows | the second read finds no victim; the archive holds a copy only | `test_a_key_revived_between_archive_and_delete_keeps_its_rows` |
| delete | none for that row | a row resolved since the archive | the row stays for the next night | `test_a_row_resolved_after_its_archive_is_kept` |
| no bulk root | none | unset | no checkpoint row deleted; the report says so | `test_no_bulk_root_deletes_no_checkpoint_row_and_the_report_says_so` |
| no bulk root | none | unreadable setting, not a folder | no checkpoint row deleted; the report asks for a person | `test_a_bulk_root_that_is_not_a_folder_deletes_nothing_and_asks_for_a_person`, `test_unset_set_and_unreadable` |
| prune report | the report item | a stop after the delete | the rerun deletes nothing more and files the report | `test_retention.test_a_prune_stopped_after_the_compaction_is_finished_by_the_next_run` |
| VACUUM | the whole file, atomically | another writer holds the lock past 60 s | nothing changed; the job exits 1 naming it; the free pages make the next night run it | `test_a_writer_holding_the_lock_skips_it_and_the_next_night_runs_it`, `test_a_vacuum_that_did_not_run_fails_the_job_and_names_why` |
| WAL checkpoint | the WAL into the file | a reader holds an older snapshot | the job says so; a WAL past 50 MiB is checkpointed the next night | `test_a_reader_holding_a_snapshot_leaves_the_checkpoint_to_the_next_night` |
| older state | none | a store with whole `lane:` and queue keys | the first night archives them like any victim | `test_the_job_archives_to_the_bulk_root_and_vacuums` |

## Acceptance criteria

- `python -m unittest tests.sd_db.test_retention tests.sd_db.test_retention_archive`
  passes with 0 failures.
- A pruned row's archive line decodes to the row's exact key and body bytes.
- On a copy of the 2026-11-13 snapshot: 2452 victims, 348 MB of body bytes
  freed, a 17.5 MB archive, and 978 MB to 615 MB after VACUUM.

## Risks

- VACUUM holds the write lock while it runs: 4.0 s on a copy of the
  978 MB snapshot. A writer without `database.Patient` that waits past its
  5 s `busy_timeout` fails. The job runs at night, and only when 50 MiB are
  free. The TRUNCATE checkpoint after it waits 5 s at most, the
  connection's own timeout, since a waiting checkpoint holds off writers.
- The lane rule frees less than the item's estimate at first: 1187 rows,
  8 MB, on the snapshot, since most `lane:` keys were written in the last
  30 days. Each key's middle rows go once it sits 30 days.
- VACUUM needs free disk of about twice the file: a temporary copy and a WAL
  as large as the file until the checkpoint.
- A hung bulk root hangs the archive write; the backup job's time bound
  stops it, and nothing is deleted before the read-back passed.
- The read-back reads the file through the page cache, so it proves the
  bytes, and the full fsync before it is what proves they are on the disk.
- Evidence: the delete is tied to the archive by row id, timestamp and
  `resolved_at`, and to the prune by the backup run id in the file name.
