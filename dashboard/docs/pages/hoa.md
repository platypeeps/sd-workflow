# HOA

HOA is at `/hoa` (sd:2116): the water system's map, Mission export trends,
alarm events and the checkout's open items. `GET /api/hoa` (`hoa_screen.document`) reads
the checkout the repo table holds under the folder name `hoa` (sd:3985): the
export is in `water/metrics/mission`, the map in `water/map/assets.geojson` and
`water/map/base.geojson`. Two repos with that folder name are an error that
names both. With none, the page says "No HOA checkout" and every cell is
unknown.

Each source is guarded on its own: a file that is missing or does not parse is
named, its cells and charts are unknown with that reason, and the other parts
still answer. The trends carry every row of the export: 7d, 30d, 6m, 1y or All. Mission data is
caution when the newest row is more than two days old (`STALE_DAYS`). Alarms
today is unknown until the export holds a row for today. The Tasks cell and the
Tasks view hold the checkout's open items of every kind but report (sd:4959),
and any item tagged hoa with `sd task edit N --area hoa` (pack sd:8010), less one tagged home:
overdue is a warning, due within a week is caution (`SOON_DAYS`), the rest are
queued. Water has no reader: the export holds daily rows, not the pump's state now.

Lots (sd:4964): each lot carries its situs address and owner of record from
the checkout's gitignored `downloads/parcels/lots.json`, which the hoa repo's
`tools/fetch-lot-parcels.py` writes from the county parcel layer. The join is by
the lot's index among `base.geojson`'s lots, and only against the base file the
lot data names by sha256. The map titles each lot with its address and, zoomed
in, draws it; a tap shows the address, owner and parcel in Details. Missing or
stale lot data makes the read partial and each lot unknown, naming the script.
The data names homeowners, so it is read at request time and never copied; this
repo's tests use synthetic names.

Each trend shows its window's min (lowest daily low), median (of the daily
values) and max (highest daily high), with faint marks at the min and max
(sd:4969). A problem level is a dashed line, and a day beyond it is a warning.
The levels are the `dashboard.hoa_thresholds` setting, edited on Settings
(sd:5109): the hoa checkout states its levels in prose only. Without it a
trend says "no problem level set".

Acknowledge on an alarm event (sd:4967) posts `/api/hoa/alarm`, the workflow
database's `view` state that Designs' Hide writes, keyed
`hoa-alarm/<datetime> <event>`. Undo writes it back. Acknowledged events hide
behind the Show acknowledged chip and leave the Alarms today lamp. The write
reads the export again, so an event it no longer holds is refused (409) by
name. Nothing reaches the Mission portal.

Acknowledge all (sd:5621) is the Alarms toolbar's button, right of the Open |
Acknowledged | All views, and a palette entry. It asks first, naming the count
("Acknowledge all: 7 alarms?"), then posts every unacknowledged key once to
`/api/hoa/alarms` (`{"keys", "acked"}`), which writes each as the single route
does, in one transaction with the export read once. The answer lists the keys
that landed (`acked`) and those that did not (`failed`, each with its reason: an
event the export dropped, or a refused write); the toast says how many landed
and names the rest, and HOA reads again. Undo posts back only the landed keys.
When no key is listed any more the route answers 409 and writes nothing. The
button is off, with its reason, when no event is unacknowledged or the store did
not read.

Data sources (sd:4965) shows one age lamp per source the checkout's
`python3 tools/source-ages.py --json` lists (Mission export, CORE,
TMT), run with a 10-second bound (`SOURCE_SECONDS`). A script that is missing,
times out, exits past 1 or prints no source list makes the panel unknown with
that reason. A row hides after its `ends` date. Refresh now starts the source's
manual cron job, the row's `job` (each `JOB_SCHEDULE="manual"`; sd:5280), so a
new management company's pull needs no dashboard change. A row from an older
checkout names none, and `hoa_screen.REFRESH_JOBS` stands in. It starts through `/api/jobs/<name>/retry`
with the job's revision, the route Management's Retry posts. The jobs
inventory gives the job's state, its off reason (not installed, running, or a
start launchd refuses) and the last line of its log; while it runs, the page
reads again every 15 seconds (`SRC_POLL`).

Download from Mission (sd:5626), the panel head's button and palette entry, posts
`/api/hoa/download {"source"}`. That starts `sd_dashboard/hoa_download.py` detached through the sweep's
`spawn`, holding `<state>/system/hoa-download/<source>/lock`: a second post while it runs is a 409. The run opens
the portal in a Claude Chrome session (`claude -p --chrome`, Chrome tools only, no form tool). On a login page the
source row shows "waiting for you to log in" while the session reads the page every 30 seconds, up to 15 minutes; it
never types. Once signed in, it runs the checkout's own download,
`tools/mission/mission.sh export`; its prompt names the portal address (section 2). `/api/hoa`'s `downloads`
part reads each source's `last.json`: running and its phase, or done, unavailable or failed with the reason. A
checkout without the script or prompt turns that button off, naming the file. A button shows only while its source
row does.
On a row with a Download, Run now runs that Download (sd:5678), and Show log puts the end of its `start.log` in
the row's Details, or why there is none (sd:5680); a row without one keeps its job's Run now and Show log.

The last Data sources row is the HOA sweep (sd:5095). Run sweep asks first,
then posts `/api/hoa/sweep`, which starts the checkout's `tools/sweep.py run`
detached, holding `<state>/system/hoa-sweep/lock` (`sd_dashboard/sweep.py`): a second
post while it runs is a 409. That script runs one `claude -p` with an exact
allowlist: it reads both Drives and the alias mail, updates the status and the
VP brief, saves followups as Gmail drafts (never sent) and hands its commit to
the lane. `/api/hoa` carries its `sweep` part, also at
`/api/hoa/sweep/status`: `tools/sweep.py status --json`, bounded at 10
seconds, and whether the lock is held. A run that ended without a record of
its own reads failed, naming `start.log`.

Gaps from the design: the map is one SVG of the files' vectors with Fit, zoom
and drag, not Leaflet, so there are no asset clusters and no aerial button.

Views (`places.js`, `places.py`): Overview is the page above. Tasks is one
table of `/api/hoa`'s items, ranked by urgency: the id is its own first column,
every column sorts (a third click returns to the rank), and Kind narrows the rows.
A row is the Tasks page's row, and its Details and commands are Tasks' (`task-details.js`, sd:5671);
a followup's first action is Draft email, which saves a Gmail draft and never sends.
Tasks shows the open items; All adds the week's done ones (`items.done`,
sd:5752), which nothing counts, and the address keeps it as `?tasks=all`.
Any other kind's row act is Done, its acknowledge, with Undo.
Briefs and Reports each read their own document, `GET /api/hoa/briefs` and
`/reports`, only when shown, and the address keeps the view as `#view=<id>`.
Each opens on its open rows, with Open | All and Seen or Acknowledge as on Home.
A brief is hoa's when its series, lower-cased with spaces as hyphens,
names a job of the `hoa` family in `dashboard.report_families`, or it is filed under the hoa checkout; a report
is hoa's when its job is in that family. A missing checkout, family or file
makes the view unknown with the reason and the line to add, never an empty list.
Votes (pack sd:8050), right of Alarms, reads `GET /api/hoa/votes`: the hoa
checkout's rows of the `vote` table, open first, then newest first. Each row
shows the motion, type, mover and seconder, the per-director tally and the
outcome and source. Open | All keeps `?votes=all`. Add vote and a row's
Edit (pack sd:8058) post `/api/hoa/vote`, as `sd votes add` and
`sd votes edit --if-revision` write: Edit sends only the fields changed,
with the revision the view read, and a vote changed since is refused with
nothing written. Each write reads the view again. A store without the
table makes it an error.
The views are `window.PAGE_VIEWS`, set from the markup before `shell.js`; the shell's view switch draws
them in `#viewswitch`, and until that shell lands `places.js` draws a segmented control there (`viewSwitch`).

HOA-owned tracts (sd:5623): every `tract` outline whose county land use (`luc`)
is `TRCT` is the association's common land, drawn in the accent colour; the road
parcel (`ROAD`) is not. A tract whose longest side is under 12 px at the current
zoom gets a ring marker, which goes once the zoom shows the tract. The legend
counts them. No parcel number is kept in this repository; the outlines and their
land use come from the checkout's `base.geojson`.
