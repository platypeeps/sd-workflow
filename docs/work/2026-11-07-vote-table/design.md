---
title: Board votes in the workflow database, one vote table
created: 2026-11-07
item: sd:8050
branch: feat/sd-8050-vote-table
---

# Design — vote-table

Write this page only for a change that moves state in steps.
The item's row holds status, progress and decisions; this page holds the shape.

## Problem

The HOA board's votes live in a CSV in the hoa checkout (hoa sd:5783).
No session or dashboard view can read or update them from the database.
The operator ruled on 2026-11-07 that votes go in a vote table, not in items.

## Approach

Migration 048 adds one table, `vote`: one row per vote or decision.

- `repo` (a repo key, foreign key) and `key` (such as `CI-1`) are unique together.
- Text columns follow the CSV: `date`, `topic`, `motion`, `type`, `moved`, `seconded`,
  `votes`, `outcome`, `follow_up`, `source` and `notes`. Each holds at most 10,000 characters.
- `votes` keeps the cell as written. `tally` is the per-director answer parsed from it:
  a JSON object of name to `yes`, `no`, `abstain`, `absent` or `none`.
  A cell that does not parse, or gives no names, leaves `tally` NULL; the text stays.
- `state` is `open` or `closed`. When a write gives no state, an outcome starting
  with `pending` gives `open`, as the hoa workbook reads it.
- `item` optionally links an item. `revision` is an integer that each change raises by one.
  An edit with `--if-revision N` refuses a row at another revision.

`sd_db.votes` holds `add`, `edit`, `rows`, `get` and `import_rows`.
The import takes rows shaped like `votes.csv`: its `id` column is the key.
It runs in one transaction. A file with a duplicate key, an unknown, missing or
repeated column, a short row or a bad row writes nothing and names the row.
A missing column or cell would otherwise blank the stored text on update. A key already stored is updated in place,
so a second import of the same file changes nothing.

The CLI is one noun, `sd votes add|edit|list|show|import`, with `--json` on reads.
No existing verb fits: `sd store` reads plugin note kinds, and `sd task` writes items,
which the ruling rejected. The checkout comes from cwd or `--belongs-to`, as `sd task add`.

The HOA page gets a read-only Votes view, `/api/hoa/votes`, through `place_views`.
It draws with the places table: open votes first, then closed ones, newest first.

Rejected: a hash revision, as items have. An integer compared in the `UPDATE`'s
`WHERE` clause is enough for one row with no notes, and a reader can see it.

## Non-goals

- No change to the hoa repository; its import and switch-over is hoa sd:5783.
- No write controls on the dashboard in this slice; that is a follow-up item.
- No vote deletion verb.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 048 migrate | `vote` table, two indexes, version 48 | write fails mid-file | `migrate` wraps the file: 47 stays, rerun | `test_a_failure_in_048_leaves_47_and_no_table` |
| 048 rollback | pack back at 47, store at 48 | `SchemaTooNew` on open | hand reverse in the 048 header | `test_the_048_reverse_returns_the_47_shape` |
| import | many rows in one transaction | a bad row late in the file | validate every row first: nothing lands | `test_a_bad_row_late_in_the_file_writes_nothing` |
| import | many rows in one transaction | a stop between two row writes | one transaction: nothing lands; rerun finishes | `test_a_stop_between_two_row_writes_lands_nothing_and_a_rerun_finishes` |
| import again | same keys | duplicate rows | upsert by `(repo, key)`; same text is no write | `test_rows_shaped_like_votes_csv_land_and_a_second_import_changes_nothing` |
| import update | stored text columns | a short row or a header without a column blanks them | refuse the file before any write | `test_a_short_row_refuses_the_file_and_keeps_the_stored_vote`, `test_a_header_missing_a_column_refuses_naming_it_and_keeps_the_stored_vote` |
| edit | one row, revision + 1 | another write between read and write | `WHERE revision = ?`; refuses stale | `test_a_stale_revision_is_refused` |
| repo removal | a vote's parent | vote orphaned | removal refuses P7 | `test_a_repo_with_votes_is_refused` |
| item removal | a vote's link | vote orphaned | removal refuses I11 | `test_an_item_a_vote_links_is_refused` |
| older store | none: store at 47 | preview reads a missing table | `_votes` reads no table as no votes | `test_a_removal_preview_reads_a_47_store_with_no_vote_table` |
| dashboard read | none | store or library without the table | view is `error` with the reason | `test_a_store_or_library_without_the_vote_table_is_an_error_not_an_empty_list` |

## Acceptance criteria

- `make check CHANGED=<touched paths>` passes.
- `sd votes import` of a CSV twice leaves the same rows and revisions.
- `/hoa` shows a Votes view with open votes first.

## Risks

- The tally parser reads a few shapes only. A cell it misreads is worse than none,
  so a cell with any segment it cannot read gives no tally at all.
- `import` lets the file win over an edit made since. Hoa imports once, then edits rows.
