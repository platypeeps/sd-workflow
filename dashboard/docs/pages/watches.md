# Watches

Watches is at `/watches` (sd:6264): the company and product watches, the
store's `sdw.market` rows, which the `market-watch` job refreshes three a
run, daily at 03:00. `GET /api/watches` (`watches_screen.document`) lists
every row with its status (its stage: candidate, active, paused or
declined), its kind, area and first source link, and its last refresh: the
newest `Last full refresh:` date the routine wrote into the profile. An
active watch refreshed more than 14 days ago reads stale, one with no date
reads "no profile yet", and a watch that is not active reads "not
refreshed": the routine refreshes active watches only. The page never shows
a date as fresh past that bound. The document also carries the job's
launchd state, its last run's stamp and the last line the run printed, so a
failed run says why.

Bulk Decline (sd:8014) on the candidates moves those older than 30 days
(a market row has no score) to declined, after it asks. A declined row
says when and by which control, from its stage note; Restore brings one
back.

Refresh all is Run now on the job: it posts `/api/jobs/market-watch/retry`
with the job's revision, as Management and HOA run a job. Refresh, on one
active watch, runs `watch.refresh` through `/api/actions/run`: claude starts
in a herdr workspace in the vault on the routine's per-entity refresh for
that one row. Add watch takes a URL and an optional name and runs
`watch.add`: `sd store add sdw.market` files a candidate row with the URL as
its first source. Details shows each answer, a refusal or a failed step
with its output. Activate (on a candidate), Pause (on an active
watch) and Unpause (on a paused one) move the status through
`/api/items/<n>/stage` with the row's revision, as Tasks moves a stage;
Undo moves it back, and a declined watch gets none (sd:6694). Remove
(`watch.remove`, sd:6703) moves a row to declined, which market-watch
skips; its profile and Sources stay. Restore (`watch.restore`) moves a
declined row back to candidate. Delete (`watch.delete`) runs `sd-db item
remove` on the row, preview then apply, which files a removal record and
backs the store up first. All three ask first. The page reads
through the shell's reader, `read.js`.
