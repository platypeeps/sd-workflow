# Today

Today is the first page, at `/` and `/today`: the same
`/api/now` rows the classic Now drew, as an annunciator and a ranked ledger. Its
capture is the shell's, as on every page: a task or followup through `POST /api/items`,
a note on the selected item through `POST /api/items/<id>/notes`.
It reads `/api/now` through the shell's reader (`read.js`): of two refreshes
only the newer one draws, and a failed read clears every row and its commands.
Status mail, backups, HOA water and Wants you wait for collectors.
Snooze hides a row until the next 08:00, for 1 hour or for 1 week, chosen in its
dialog (sd:3450); it posts
`/api/snooze`, and the row waits under Snoozed with an Unsnooze (sd:1896).
A row whose problem reads otherwise shows again before its time.
Your work lists the items `sd today` lists, from the same `/api/now` document; each row has the same Snooze,
keyed `today:item:<id>`, and the item shows again early when its status or due date changes (sd:3271).
`sd today` still lists a snoozed item.
The decision queues are one queued Needs you line (sd:8013, `now_screen.triage_rows`): how many store rows wait
in Blog Ideas, Tips, Market Watch and Topics, each a link to the page that decides it. It is apart from Decisions.
Retry on a failed or interrupted job posts `/api/jobs/<job>/retry` with the job's revision from the same read, as
Management does (sd:3386); a job that changed since is refused and Now is read again.
After exit 127, command not found, Retry asks first and names the exit (sd:3392).
A blocked lane entry's Retry and Approve post `/api/queue/move` as Queue's do. A refused or unanswered one reads
Today again, then a line above Needs you says why until the next press: for a 409, that the queue changed, when it
was read again, and that a press now acts on that read (pack sd:7315). Nothing retries by itself.
A job row also has Show log, which opens the job on Management > Schedules, and Print, the `launchctl print` line to
copy; Print is the row's act after exit 127. Both are the declarations Management and Activity use (sd:3449).
A flaky head is a look row on the Lane lamp (pack sd:7663; Metrics' Flaky tests until then): a head the
gate failed at, then passed at, in the last 7 days (`metrics_screen.flaky`). Two or more fold into one group
row that opens to them. A head's Details name its repo, head, failures and pass, with Open item and Snooze;
the group's Snooze all 1w writes one snooze per head, and a new failure at a head shows it again. A lane read
that failed is a partial lane row, so the lamp reads unknown.

## Queue view

The Queue view (`#view=queue`, key q, pack sd:7234) holds what Queue's Overview, Today's Queues and In motion
showed. `/queue` answers 301 to it; `/queue?show=all` opens it with Ended builds on. An old `#view=queues` or
`#view=motion` link opens it too.

It has one row per repo, all closed. A click opens a repo's rows under it and closes the one that was open; a click
on the open repo closes it. The rows, in order:

- working: the live items of `/api/now` (a worktree, a lane entry, a running `sd-*` command, a herdr agent or a Fix
  note, pack sd:3466), the open build runs of `GET /api/queue/builds?show=all` (`builds.panel`: working, waiting,
  stalled or lost), the lead's teammate runs with worktree, branch, due time and last beat (pack sd:7221),
  a builder's gate in the lane, and the runner's queued and running assignments no run holds
  (`GET /api/queue/runner`, pack sd:7115 and sd:7217);
- merging, next and blocked: the lane entries of `GET /api/queue` (`queue_screen.document`);
- suggested: what sd-next offers next, at most nine per repo (pack sd:7222, sd:7314, sd:8008): `/api/now`'s
  `queues.suggested`, ranked by `sd_db.today.suggested` (ready items in the backlog's order, then planned items
  soonest first, then unscheduled ones highest priority first), less any item that is live, in the lane or held by a
  build. Each row says why: `ready`, `planned for <date>` or `unscheduled P<n>`; one that is not ready shows its
  status and offers its next status, not Build. Its task is its `/api/tasks` row, read while Suggested shows, so
  that act is on before its Details are read (pack sd:7334);
- ended: ended build runs, and the cancelled and landed lane entries.

Each row's marker is its state glyph and its type in words; a repo's glyph is its worst row's.
A repo row's detail holds six boxes: merging, working, next and blocked, as Queue's lane ledger drew them
(pack sd:7272), then review (pack sd:8083) and suggested (pack sd:7314). They count all the repo's rows of those
types, so a filter hides no count. Review counts the pull requests waiting on a review, and blocked the other
blocked rows, so no row counts twice. A box is lit only when its count is not zero; blocked lights the warning
lamp, review the caution lamp when a PR waits on you; the cell's label says all six in words. A repo whose lane was not read says
"lane not read" in place of the boxes, not 0.
Suggested is on by default (pack sd:7314); All items and Ended builds are off. On, a repo that has only those rows
shows too. `?show=` keeps the choice: no key is the default, and an empty `show=` keeps every filter off.
All items lists no idea kind (pack sd:7699): ideas live on Writing.
The view reads its three documents when it first shows, on each Today read after, and every 30 s while it shows.
A source that was not read is named under the table.
Read again on this view first posts `/api/queue/prs/check` with the items its PR rows show, at most 20 (sd:8375):
the server reads each linked pull request on GitHub and records it as the collector does.
The record that first sees a pull request merged closes its item, so the nightly sync closes it too.
A pull request it could not read, or an item left open after its merge, is named under the table.
The 30 s poll reads only the database.

A live row's click shows its Details with Open item, Show assignment and Fix (sd:6714).
Up, down, top, hold, release and cancel, a blocked row's Retry and Approve, a cancelled row's Re-queue and a running
row's Release post `/api/queue/move`, which runs `sd-ship lane move|hold|release|cancel|retry` and nothing else.
Each write carries the revision, head or claim the view showed, and the verb checks it again under the queue's lock,
so a stale write is refused (409). A failure that is not a refusal answers 502 with an unknown outcome, and the view
reads the lane again. The line above the table says the outcome in the words Needs you uses (pack sd:7315). The full rules are in `queue_screen`'s docstring.
A build run's Cancel asks first and posts `/api/assignments/<id>/cancel`; an ended blocked or cancelled bundle's Retry
and Send to builder post `/api/queue/builds/retry` (pack sd:6020).
Queue's Last 7 days moved to Metrics' Lane times view (pack sd:7233).

The Timeline view is one chart on one time axis (pack sd:7681): Today's
Landed, Jobs, Briefs and Live lanes from `/api/now`'s `track`, which carries 7
days of marks, then one lane per Activity kind (Merges, Runs, Reviews,
Commands) from `GET /api/activity?range=7d`, read when the view first shows
and on each Read again after. The 24h and 7d tiles pick the window the axis,
the marks and the table draw; `?range=7d` keeps the longer one. A kind whose
read failed draws a hatched lane with the reason, even in a window with no
mark. A tick links to its event on Activity, at 7 days when the tile says 7d. The key's Landed counts the last 24 hours.
