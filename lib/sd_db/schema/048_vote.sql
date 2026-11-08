-- `vote`: one row per board vote or decision (pack sd:8050, for hoa
-- sd:5783; design docs/work/2026-11-07-vote-table/design.md). The operator
-- ruled on 2026-11-07 that votes live in their own table, not as items.
--
-- `repo` and `key` (such as `CI-1`) are unique together. The text columns
-- follow the hoa checkout's `votes.csv`; `votes` keeps the cell as written
-- and `tally` the per-director answer `sd_db.votes.parse_votes` read from it,
-- or NULL. `state` is `open` or `closed`. `item` optionally links an item.
-- `revision` starts at 1 and each change raises it by one; an edit naming
-- another revision is refused. `sd_db.votes` is the one writer.
--
-- `repo` and `item` restrict a delete: `removal` refuses a repo or an item a
-- vote names (P7, I11), so no vote loses its parent.
--
-- A new table, so no row moves and no other table is touched. One
-- transaction (`migrate` wraps the file): a failure leaves 47 with no table.
-- The reverse, run by hand with the runner, the dashboard and the serve agent
-- stopped, after `bin/sd-db backup`. It drops every vote.
--
--   BEGIN;
--   DROP TABLE vote;
--   PRAGMA user_version = 47;
--   COMMIT;

CREATE TABLE vote (
    id              INTEGER PRIMARY KEY,
    repo            TEXT NOT NULL REFERENCES repo(path) ON DELETE RESTRICT,
    key             TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 64),
    -- YYYY-MM-DD, or NULL when the record gives no date.
    date            TEXT,
    topic           TEXT NOT NULL DEFAULT '',
    motion          TEXT NOT NULL DEFAULT '',
    type            TEXT NOT NULL DEFAULT '',
    moved           TEXT NOT NULL DEFAULT '',
    seconded        TEXT NOT NULL DEFAULT '',
    votes           TEXT NOT NULL DEFAULT '',
    tally           TEXT CHECK (tally IS NULL OR json_valid(tally)),
    outcome         TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL CHECK (state IN ('open', 'closed')),
    follow_up       TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    item            INTEGER REFERENCES item(id) ON DELETE RESTRICT,
    revision        INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX vote_by_key ON vote (repo, key);
CREATE INDEX vote_by_state ON vote (repo, state, date);
