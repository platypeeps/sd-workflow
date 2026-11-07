-- `home_asset` (pack sd:8053): what is on the home lot, for the Home page's
-- property map and, from sd:8054, the koi pond and its fish. One row per
-- thing: a `kind` from `sd_db.assets.KINDS`, a name, an optional GeoJSON
-- `Point` or `LineString` in EPSG:2232 (Colorado Central, US feet), the
-- kind's `fields` as a JSON object, and an optional photo address.
--
-- A care task is an sd item that names the row in `item.fields.asset`; it
-- recurs with the item (`workflow._next_occurrence`), so no link table.
--
-- A new table, so nothing existing moves. The reverse, run by hand with the
-- runner, the dashboard and the serve agent stopped, after
-- `bin/sd-db backup`; it drops every asset:
--
--   BEGIN;
--   DROP TABLE home_asset;
--   PRAGMA user_version = 47;
--   COMMIT;

CREATE TABLE home_asset (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    geometry    TEXT,
    fields      TEXT NOT NULL DEFAULT '{}',
    photo       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
