# Contributions

Contributions is at `/contributions` (sd:2113): the open rows of the
contributions projection v1 renders, in lanes by who acts next, with each
row's repository scope (internal when sd's repo table holds it) and settled
rows counted per repository. Each open row names its repository in a
sortable Repo column (`local` for none), and its title leads with its
number (sd:6696). `GET /api/contributions/page`
(`contribution_screen.document`) carries at most `OPEN_LIMIT` open rows and
says when it cut some; its per-scope counts, which the badge, lamps and tallies
read, cover every open row. It sends no local path: a checkout reads
`local: <folder>`, and a draft is the flag `has_draft`. The dashboard reads nothing from GitHub, so the design's
"GitHub now" state and settled-per-day chart say they are not read.
Acknowledge and Make task ask first, since no verb reverses either.
Acknowledge posts to the v1 route. Make task posts `/api/contributions/task`,
which files the task `sd task contribution add` files, with the row's URL as
its identity: the projection links the two, and a second task for the URL is
refused. A write's toast reports the write; the reread after it is separate.
The page reads through the shell's reader, `read.js` (sd:2488): only the
newest read draws, a reread waits for a read that started before its write
landed, and a row a read no longer lists loses its pick and its commands. A
failed reread keeps the rows and says the write landed; a failed load clears
the page. A redraw keeps keyboard focus on the replaced lamp or command, or on
the row's next action when that command went off (sd:2426). Settled rows count per stored repository, so two
checkouts with one folder name stay two bars. Draft nudge is copy only; Open on
GitHub opens the row on GitHub in a new tab (sd:5628). Re-run collector, the
collector's command, heads the Open view after its tally (sd:7211), as
Clean all safe heads Health's Checkouts; it posts `/api/shadow/sync` and
reads again when the sync ends. Read again runs that same sync (sd:8380);
while one runs, or before the collector row exists, it only reads. Hide and Unhide (sd:5486) post
`/api/contributions/view`, `{"key", "hidden"}`, which writes the view store
Designs' Hide writes, keyed `contribution:<key>`; each offers Undo. A key the
projection no longer lists is refused with 409. A hidden open row stays in the
document with `hidden`, after every shown row, and leaves the lanes, tallies
and badge; Show hidden, a toggle in the Open table's filter bar, lists it
with Unhide (sd:5753; it was the Hidden view, and a `#view=hidden` link lands
on it as `?hidden=1`). Unlike
Dismiss, new activity does not unhide it, and settled rows keep their counts.
A view store that does not read hides nothing: `store.reason` says why, and
Hide is off. The Filed view (sd:5488) lists the Jira tickets and GitHub issues
the operator filed this local year, from the document's `filed`
(`contribution_screen.filed_view`): `shadow` rows through
`progress.tracker_items`, filed by the `filed` flag (pack sd:5496) or, before
it syncs, by a `reason` of filed. A filed row with no created date yet is
listed and says so; a row whose filer is not synced is counted per tracker.
Each tracker shows its sync state, and one that does not read, or a pack with
no `filed` column, is named with its reason, never an empty list. Edit opens
the row's https link in a new tab and posts nothing. Details shows each row's dependencies, test
evidence and notification delivery (sd:3394); the document cuts each local path
in a recorded check to its last part, and a section it cannot read says
"Not read" with the reason. The old screen moved to `/classic/contributions`.
