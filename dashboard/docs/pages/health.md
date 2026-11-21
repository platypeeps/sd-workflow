# Health

Health is at `/fleet-health` (sd:2115); `/health` stays the service's own
check. `GET /api/health` (`health_screen.document`) lists eight of the
design's nine areas in its order, then Doctor (sd:3393); it leaves out
Attribution (sd:3011). Each has a reader. Three read what the dashboard had: Worktrees (registrations whose
directory is gone, from the fleet child Sessions reads), Ports (`ports_screen`'s reader, which the retired Operations > Ports used, with
its counts and warnings) and Protection (`protection.rows`, drawn as a matrix
with one column per repository and a table carrying the same cells; an unread
repository shows no cell).

The page shows one view at a time, each its own read, `GET /api/health/<view>`
(`health_screen.part`): Checkouts (Worktrees, Branches), Machine (Disk,
Services, Ports, Containers, Doctor), GitHub (Dependencies, Security, Protection) and Jobs
(Credentials, Vault mirror). A view whose read fails marks its own areas unknown
with the reason; no other view is touched. The lamps on top come from
`GET /api/health/summary` (`health_screen.summary`), which is also the rail
badge: it reads the database-only areas now and shows every walker's last
answer with its time, so it never waits on one. A walker with no answer younger
than 60 seconds (the doctor's 10 minutes) gets a scan started in the background,
which the next view read joins. If that scan fails, the area keeps its last rows
marked stale, with the failure and their time; with no rows, the failure is its
error. Worktrees reads the server's fleet snapshot: its
time is when that snapshot's read began, and after a failed refresh the area is
stale with the reason. A stale area's lamp is unknown unless a row is worse.
`GET /api/health` is still every area in one document.

The page declares its views in `window.PAGE_VIEWS` for the shell's view switch,
which owns `#view=<id>`; each view's reader starts the first time it opens. Each
view counts the rows that want you, from the summary; a view with an area not
read shows no count. With no view named the page opens the view an `?area=`
value names, else Checkouts. A lamp of another view opens that view with its
area alone. The filters are Activity's bar (sd:5504): Show and State as
segments, Area as a pick, the picked areas and the query as tokens, then Clear
all; an area's title line counts its rows while a filter hides some. State
starts on warning, caution and unknown, so each area leaves out its ok and
queued rows and says how many; with no State pressed every row shows, and
Clear all returns State to its start (sd:7207). A State other than the start
goes in `?state=`; an old `?show=all` link opens with no State pressed. Show
ignored (`?show=ignored`) lists the ignored rows instead of the areas. A
finding's detail is one line in the table; Details carries the row in full.

Dependencies, Security and Credentials read rows nightly jobs stored; the page
calls no service and reads no credential.

- Dependencies (sd:2205) and Security (sd:2206) read the `alerts` the nightly
  `sd shadow sync` stores in each managed repository's `repo_protection` row.
  Dependencies is a row per repository with open Dependabot alerts, a warning
  when one is critical or high or the list ran past its first page; an
  archived repository is left out, and one with Dependabot alerts off is
  allowed and listed in an ok row (sd:5595). Security
  is a row per public repository with open secret-scanning alerts (warning) or
  with scanning off (caution); a private repository is not scanned, by policy.
  A repository whose alerts were not re-read in 48 hours is a caution row.
  The sync reads one page of 100 alerts, so a longer list shows as `100+`.
- Credentials (sd:2203) reads the latest `credentials:nightly` heartbeat that
  `sd-db credentials` writes from `credentials-nightly.job`: the GitHub PAT
  (present, accepted, expiry), `gh auth status`, `HA_TOKEN` against `HA_URL`,
  and `claude mcp list`. An expiry lights caution 30 days ahead and warning 7
  days ahead; a heartbeat older than 48 hours is a caution row.
- In all three, a read that failed is an unknown row naming the reason, never
  a count of zero.

Disk and Branches (sd:2202, sd:2204) are read by `health_collectors`, each
inside an 8-second budget; past it the area says it stopped rather than
waited on.

The readers that wait on subprocesses (Disk, Worktrees, Branches,
Ports, Containers, Doctor) run at once, so the slowest decides how long the page
waits, not the sum of their budgets. The registry they walk is read first,
on the request's thread, and handed to them as paths. `PAGE_SECONDS` (13)
bounds the whole document, and the page does not wait for a reader still
running at it. Each area runs one scan at a time: a later request joins a
scan still running instead of starting another, so a stalled mount holds one
thread, not one per refresh. Meanwhile the area shows its last answer under
"Not re-read", or is its error if it has none.

- Branches lists, per registered repository, the local branches
  `origin/HEAD` already contains, the default branch left out. A merged
  branch a worktree has checked out is its own queued row, since
  `git branch -d` refuses it. A repository with no `origin/HEAD`, or one git
  cannot read, is named in an unknown row, never counted as clean.
  Merged worktrees (sd:5105) is one row: the registered worktrees whose
  `HEAD` is the head a merged lane entry names for that worktree. The lane
  squash-merges, so only its entry ties a tree to a merge; a fresh worktree
  at `origin/HEAD` is not one. Each tree says why it stays, if it does: a
  process works in it (`lsof -a -d cwd -Fn`, and no answer keeps every
  tree), uncommitted or untracked files, ignored files other than build
  output and tool caches, or a branch other than the merged one.
  A repository whose `git worktree list` failed is named in an unknown row
  (sd:5120), never read as having no merged worktree.
- Disk draws a bar per volume from `df -kPl` (the data volume and each volume
  under `/Volumes`; local file systems only), with a row for each volume past
  80% (caution) or 90% (warning). It lists the three biggest subfolders of
  each folder a `storage|<path>` line of the `dashboard.disk_storage` row
  names (Settings, Dashboard), one `du -k -d 1` per folder in what is left of
  the budget. A local folder `du` cannot finish shows its drive's used and
  free from one `df -kP`, read before the `du`, at the volume thresholds
  (sd:5879): `du` on a 2.1 TB drive does not finish in the 8 s budget. A
  folder `du` cannot finish with no `df` answer, or that is not mounted, is
  an unknown row, and so are no folder and a row that was not read. It finds registered worktrees
  whose `HEAD` is in `origin/HEAD` and that still hold `target/`,
  `node_modules/` or `.venv/`, the rule of 2026-09-25; it reads their
  presence, not their size, and `git status --porcelain` in each: a worktree
  with uncommitted changes keeps its folders.
- Services lists each long-running user LaunchAgent from `sd services
  list`, with Restart and Start. A run-once or scheduled user agent shows
  only when its last run failed (a non-zero exit or a failure signal), as a
  warning with its last exit and no controls (sd:5882); a healthy one stays
  off the page.
- Containers (sd:5840) lists each running container: its name, image,
  status and uptime, compose project and working directory, and its bind
  mounts under the checkout root (`REPO_ROOT`, default `~/repos`), so a
  container that holds a checkout shows. It reads the `CONTAINER` lines of
  the one `docker ps` that `machine-setup.sh candidates service` runs for
  Ports; Ports and Containers share that one collection per request. A
  Docker that is not installed, not running or not fully read makes the
  area unknown with the reason, never empty. Like Services it has no lamp,
  only its chip and section. It has no start or stop control.
- Doctor runs `local-machine-setup/machine-setup.sh doctor` in a child process
  with a 180-second deadline and reads its `ok`, `WARN` and `FAIL` lines: a row
  per WARN (caution) and FAIL (warning), then one row with the rest. A run
  without the doctor's closing line, or whose exit disagrees with it, is the
  area's error, never clean. An answer stands for 10 minutes; a failed run
  drops it. The page waits 2 seconds on a run in flight, then says it is still
  running.

Machine has a chooser (sd:9757): this machine, the hub, first, then each
other machine with a stored reading, by host name. The viewer's pick is kept
in the browser. Each satellite's scheduled job runs
`sd services list --record SECONDS`, which stores its Disk volumes and
folders and its Services as the `machine-health:<host>` heartbeat; the hub
reads that row and never reaches the machine. A chosen satellite shows only
Disk and Services, read at the reading's time, with no controls and no
build-output rule; a reading older than its SECONDS is stale, and a host
with no reading is unknown. `GET /api/health/machine?host=<name>` answers it.

Prune registrations, Delete merged and Remove build output run from the page
(sd:3393), behind a confirm that names the registrations, branches or folders.
Each posts `/api/health/cleanup` with the row and its `revision`, a digest of
what the page showed. The server reads that subject again and answers 409 when
it differs, so nothing is written. Then git's own guards hold:
`git worktree prune` keeps a locked registration, and `git branch -d` refuses
an unmerged branch. A registration git could not read refuses the prune. Build
output goes only from a worktree read again just before: the same head, still
in `origin/HEAD`, and clean; a symlink is never followed. A step whose result
is not known answers 502 with `outcome: unknown` and what landed, and the page
reads Health again; a check before a step that could not be read is unknown
too, never a refusal. A write where git or a check refused every step wrote
nothing and answers 400. None of them runs in the shell's bulk. `health_cleanup`
holds the writes.

Remove merged (sd:5105) checks each tree again, then deletes origin's branch
with a lease on the merged head, detaches the tree, deletes the local branch
with `update-ref -d` on that head, and runs `git worktree remove` without
`--force`, which takes its build output with it. The tree stays on disk until
the last step, so a stop between steps leaves it in the next read, and the
next run finishes it. A branch that moved, here or on origin, keeps the tree.
Clean all safe heads Checkouts: its confirm lists every row whose own cleanup
would run, with what each removes, then it posts them in turn, each with its
own revision. A refused row wrote nothing and the run goes on; an unknown
outcome stops it.

Vault mirror (sd:3977) reads the last `records-mirror` heartbeat: ok when it says so, a warning with its reason
and refused files, and unknown before any render. The row is read-only: Health writes no vault note, and
`/api/records/render` is gone (sd:5134).

Attribute, Show biggest, Show use and Inspect listener are CLI lines for Copy,
and Re-check reads the document again. Re-run collector posts `/api/shadow/sync`, as
Contributions does, and reads Health again when the run ends (sd:2894).

Snooze, on every row that is not ok, hides the row until the next 08:00, for
1 hour or for 1 week, chosen in its dialog (sd:1896, sd:3450). It posts `/api/snooze`, as Today does, and the
row waits under Snoozed with its time and an Unsnooze until then. A snooze
holds only while the row reads as it did: more alerts, a worse state or another
count shows it again. A snooze read that fails hides nothing and says so.

Ignore, beside Snooze, is a snooze with no end (sd:5470): it posts `until`
`forever`, so only a changed problem or Unignore shows the row again. An
ignored row is not under Snoozed; Show ignored lists it, with its Unignore.
