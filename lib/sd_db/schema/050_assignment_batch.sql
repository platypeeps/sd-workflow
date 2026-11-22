-- `assignment.batch` (pack sd:9570): the id of the first run `runner.enqueue`
-- queued in the same call. Within one batch, `after` is batch order, which a
-- run that waits on a later one may pass (`runner._graph`); any other `after`
-- is an explicit prerequisite, kept always.
--
-- Rows queued before this file read NULL: no batch, so a saved `after` of
-- the same role stays a prerequisite and is never dropped (review of
-- build/sd-1306). Nothing else moves.
--
-- The reverse, run by hand with the runner, the dashboard and the serve agent
-- stopped, after `bin/sd-db backup`:
--
--   BEGIN;
--   ALTER TABLE assignment DROP COLUMN batch;
--   PRAGMA user_version = 49;
--   COMMIT;

ALTER TABLE assignment ADD COLUMN batch INTEGER;
